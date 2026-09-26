"""The run's dividend source for the E4 guard's carry separation (2026-09-26).

``load_cached_dividend_calendar`` reads CACHE_FOLDER/fmp_history/mc_stock_dividend__<SYM>.json
(the payload ``tools/warm_dividend_calendars.py`` caches; a backtest never fetches) and
``options_store._dividends_of(split_basis)`` serves (ex-date, amount) in the SESSION's as-traded
basis: FMP's ``adjDividend`` is split-adjusted like its closes, so it is multiplied by the run's
as-traded factor of the session, exactly as the spot is. An absent calendar is None -- the
guard then says "dividend calendar unavailable" in any refusal, it never assumes "no dividend".
"""
from __future__ import annotations

import json
import os
from datetime import date

import pytest


@pytest.fixture
def cache(tmp_path, monkeypatch):
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    os.makedirs(tmp_path / "fmp_history")
    return tmp_path


def _write(cache, sym, rows):
    with open(cache / "fmp_history" / f"mc_stock_dividend__{sym}.json", "w") as f:
        json.dump({"symbol": sym, "historical": rows}, f)


def test_the_namespace_is_the_warmup_sources_own():
    from ba2_providers.market_conditions.fmp_source import DIVIDEND_CALENDAR_NAMESPACE
    from app.services.backtest.option_split_basis import dividend_calendar_path
    assert DIVIDEND_CALENDAR_NAMESPACE == "mc_stock_dividend"
    assert dividend_calendar_path("stla").endswith(os.path.join("fmp_history",
                                                                "mc_stock_dividend__STLA.json"))


def test_the_loader_reads_adj_dividends_and_answers_none_when_absent(cache):
    from app.services.backtest.option_split_basis import load_cached_dividend_calendar
    assert load_cached_dividend_calendar("STLA") is None
    _write(cache, "STLA", [{"date": "2022-04-19", "dividend": 1.04, "adjDividend": 1.1221},
                           {"date": "2021-04-19", "dividend": 0.3849, "adjDividend": 0.3849},
                           {"date": "2020-01-02", "dividend": None, "adjDividend": None}])
    assert load_cached_dividend_calendar("STLA") == [(date(2021, 4, 19), 0.3849),
                                                     (date(2022, 4, 19), 1.1221)]


class _Basis:
    def __init__(self, factor):
        self._f = factor

    def factor(self, symbol, day):
        return self._f(symbol, day)


def test_dividends_are_served_in_the_sessions_as_traded_basis(cache):
    from app.services.backtest.options_store import _dividends_of
    _write(cache, "X", [{"date": "2024-06-03", "adjDividend": 0.5},
                        {"date": "2023-06-01", "adjDividend": 0.4}])
    # a 4-for-1 split on 2024-07-01: before it, the as-traded price is 4x the adjusted one
    src = _dividends_of(_Basis(lambda s, d: 4.0 if d < date(2024, 7, 1) else 1.0))
    assert src("X", date(2024, 5, 1)) == [(date(2024, 6, 3), 2.0)]      # future ex-dates only
    assert src("X", date(2024, 8, 1)) == []
    assert src("Y", date(2024, 5, 1)) is None                          # no calendar cached


def test_the_run_wires_the_dividend_source_into_the_guard(cache, tmp_path, monkeypatch):
    from app.services.backtest import options_store as S
    captured = {}

    class _Provider:
        def __init__(self, root, **kw):
            captured.update(kw)

    monkeypatch.setattr("app.services.backtest.parquet_options_provider.ParquetOptionsProvider",
                        _Provider)
    monkeypatch.setattr(S, "resolve_options_store", lambda cfg: "thetadata")
    monkeypatch.setattr(S, "price_source_spot", lambda ps, sb: (lambda s, d: 1.0))
    monkeypatch.setattr(S, "spot_scope", lambda cfg, sb: "scope")
    basis = type("B", (), {"basis_of": lambda self, s: None,
                           "factor": lambda self, s, d: 1.0})()
    S.build_options_provider({"options_cache_db": "x", "options_parquet_root": str(tmp_path),
                              "options_risk_free_rate": 0.04}, price_source=None,
                             split_basis=basis)
    assert callable(captured["basis_guard_dividends"])
    assert captured["basis_guard"] is True
