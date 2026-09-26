"""The option backtest's risk-free rate: as-of FRED DGS3MO, one object per run, fail loud.

Replaces a flat 4.5% that every option backtest used unless an env override was set (nothing
set one). Pinned here:

  * the parquet reader inverts each bar at the rate of THAT BAR'S date;
  * the account's Black-Scholes mark reads the SAME rate object for the clock's day, so a bar
    inverted to an iv and priced back reproduces its close (the round-trip requirement);
  * overlays at different rates never share a worker cache entry;
  * the run records its rate source (``fred-dgs3mo`` or ``explicit``) in its results;
  * a reader carrying no rate refuses a Black-Scholes mark instead of choosing one;
  * the options cache BUILDER refuses on a missing key / failed fetch (it used to return {}
    and invert every bar at 4.5%).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from ba2_common.core.option_bs import bs_price
from ba2_common.core.types import OptionRight
from ba2_providers.macro import risk_free_rate as rfr

from app.services.backtest.option_greeks import compute_iv_and_greeks
from app.services.backtest.parquet_options_provider import (
    ParquetOptionsProvider, clear_worker_parquet_options_cache)
from tests.backtest.test_parquet_options_provider import (  # noqa: F401 (fixtures)
    _C100, _EXP1, _SPOT, _UNDER, _spot_source, _wide, shared_arrays_enabled, store_root)


def _stepped_rate(tmp_path):
    """1% through 2023-01-04, 3% from 01-05, 5% from 01-10 -- one rate per test bar date."""
    rows, d = [], date(2022, 12, 1)
    while d <= date(2023, 3, 31):
        if d.weekday() < 5:
            v = "1.0" if d < date(2023, 1, 5) else ("3.0" if d < date(2023, 1, 10) else "5.0")
            rows.append({"date": d.isoformat(), "value": v})
        d += timedelta(days=1)
    path = tmp_path / "DGS3MO.json"
    path.write_text(json.dumps({"series_id": "DGS3MO", "observations": rows}))
    return rfr.fred_dgs3mo_rate(date(2023, 1, 2), date(2023, 3, 31), path=str(path))


def test_each_bar_is_inverted_at_its_own_dates_rate(store_root, tmp_path):
    rate = _stepped_rate(tmp_path)
    p = ParquetOptionsProvider(store_root, spot_source=_spot_source, risk_free_rate=rate,
                               spot_scope="rate-test")
    for bar_day, close, r in ((date(2023, 1, 5), 6.2, 0.03), (date(2023, 1, 10), 7.2, 0.05)):
        row = {c.symbol: c for c in _wide(p, bar_day)}[_C100]
        expected = compute_iv_and_greeks(close, _SPOT[bar_day], 100.0,
                                         (_EXP1 - bar_day).days / 365.0, r, OptionRight.CALL)
        assert row.delta == expected["delta"] and row.implied_volatility == expected["iv"]
    # A chain read on 01-09 serves the 01-05 bar: inverted at 01-05's rate, not the clock's.
    row = {c.symbol: c for c in _wide(p, date(2023, 1, 9))}[_C100]
    expected = compute_iv_and_greeks(6.2, _SPOT[date(2023, 1, 5)], 100.0,
                                     (_EXP1 - date(2023, 1, 5)).days / 365.0, 0.03,
                                     OptionRight.CALL)
    assert row.delta == expected["delta"]


def test_overlays_at_different_rates_never_share_a_cache_entry(store_root, tmp_path):
    flat = ParquetOptionsProvider(store_root, spot_source=_spot_source, risk_free_rate=0.045,
                                  spot_scope="same")
    series = ParquetOptionsProvider(store_root, spot_source=_spot_source,
                                    risk_free_rate=_stepped_rate(tmp_path), spot_scope="same")
    d_flat = {c.symbol: c for c in _wide(flat, date(2023, 1, 10))}[_C100].delta
    d_series = {c.symbol: c for c in _wide(series, date(2023, 1, 10))}[_C100].delta
    assert d_flat != d_series
    clear_worker_parquet_options_cache()


# ------------------------------------------------------------------------------ the account
class _Reader:
    def __init__(self, rate):
        self.risk_free_rate_source = rate


def _account(reader, today):
    from app.services.backtest.backtest_account import BacktestAccount

    acct = BacktestAccount.__new__(BacktestAccount)
    acct._options = reader
    acct._as_of_date = lambda: today
    return acct


def test_the_bs_mark_reads_the_readers_rate_for_the_clocks_day_and_round_trips(tmp_path):
    rate = _stepped_rate(tmp_path)
    for day, r in ((date(2023, 1, 6), 0.03), (date(2023, 1, 11), 0.05)):
        acct = _account(_Reader(rate), day)
        assert acct._bs_mark_rate() == r
        # Invert a close at the reader's rate for that day, price it back at the mark's rate:
        # the same close, because it is the same rate object on the same day.
        close, spot, strike, dte = 6.2, 103.0, 100.0, (_EXP1 - day).days
        iv = compute_iv_and_greeks(close, spot, strike, dte / 365.0, rate.rate_on(day),
                                   OptionRight.CALL)["iv"]
        back = bs_price(spot, strike, dte, iv, OptionRight.CALL, r=acct._bs_mark_rate())
        assert back == pytest.approx(close, abs=1e-6)


def test_a_reader_without_a_rate_refuses_the_bs_mark():
    acct = _account(_Reader(None), date(2024, 3, 1))
    with pytest.raises(RuntimeError, match="risk-free rate"):
        acct._bs_mark_rate()


def test_the_run_records_its_rate_source(tmp_path):
    from app.services.backtest.options_store import apply_risk_free_rate_record

    explicit = rfr.explicit_rate(0.02, origin="env:BACKTEST_OPTIONS_RISK_FREE_RATE")
    acct = _account(_Reader(explicit), date(2024, 3, 1))
    results = {}
    apply_risk_free_rate_record(results, acct)
    assert results["options_risk_free_rate_source"] == {
        "source": "explicit", "identity": "explicit:0.02", "rate": 0.02,
        "origin": "env:BACKTEST_OPTIONS_RISK_FREE_RATE"}

    series = _account(_Reader(_stepped_rate(tmp_path)), date(2023, 1, 6))
    results = {}
    apply_risk_free_rate_record(results, series)
    rec = results["options_risk_free_rate_source"]
    assert rec["source"] == "fred-dgs3mo" and rec["series"] == "DGS3MO"
    assert rec["window"] == ["2023-01-02", "2023-03-31"]


def test_an_equity_run_records_nothing():
    from app.services.backtest.options_store import apply_risk_free_rate_record

    results = {"total_return": 1.0}
    apply_risk_free_rate_record(results, _account(None, date(2024, 3, 1)))
    assert results == {"total_return": 1.0}


def test_the_handler_stamps_the_rate_record_on_every_run():
    import inspect

    from app.services.backtest import daily_backtest_handler as h

    assert "apply_risk_free_rate_record(results, account)" in inspect.getsource(
        h.run_daily_backtest)


# ------------------------------------------------------------------------ the cache builder
@pytest.fixture
def no_fred_key(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr("ba2_common.config.get_app_setting", lambda key, default=None: None)


def test_the_builder_refuses_without_a_key(no_fred_key):
    from ba2_common.core.fred_api_key import FredApiKeyMissing

    from app.services.backtest import fetch_options as fo

    with pytest.raises(FredApiKeyMissing, match="options cache build"):
        fo.fetch_risk_free_rate_series(date(2024, 1, 2), date(2024, 6, 28))


def test_the_builder_refuses_on_a_failed_fetch(monkeypatch):
    import requests
    from ba2_providers.macro import fred_series

    from app.services.backtest import fetch_options as fo

    monkeypatch.setenv("FRED_API_KEY", "k")

    def _fail(series_id, api_key):
        raise requests.HTTPError("500 Server Error")

    monkeypatch.setattr(fred_series, "refresh_series", _fail)
    with pytest.raises(requests.HTTPError):
        fo.fetch_risk_free_rate_series(date(2024, 1, 2), date(2024, 6, 28))


def test_build_cache_refuses_up_front_without_a_key(no_fred_key, monkeypatch, tmp_path):
    """The whole build refuses -- not one symbol at a time, and not at a flat 4.5%."""
    from ba2_common.core.fred_api_key import FredApiKeyMissing

    from app.services.backtest import fetch_options as fo

    monkeypatch.setattr("alpaca.trading.client.TradingClient", lambda *a, **k: None)
    monkeypatch.setattr("alpaca.data.historical.option.OptionHistoricalDataClient",
                        lambda *a, **k: None)
    monkeypatch.setattr(fo, "discover_contracts",
                        lambda *a, **k: pytest.fail("reached contract discovery"))
    with pytest.raises(FredApiKeyMissing):
        fo.build_cache(str(tmp_path / "o.db"), ["AAPL"], date(2024, 3, 1), date(2024, 3, 8),
                       api_key="x", api_secret="y")
    assert not hasattr(fo, "_FALLBACK_RISK_FREE_RATE")
