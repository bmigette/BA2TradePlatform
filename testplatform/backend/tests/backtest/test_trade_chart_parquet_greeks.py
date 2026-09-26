"""Trade popup contract detail for runs that read a PARQUET option store.

Parquet stores hold no greeks: the run's ``ParquetOptionsProvider`` inverts each bar at read
time against the run's underlying close. The popup must show exactly those numbers -- rebuilt
through the run's own factory and inputs -- or say why it cannot.

Fixture: the real NFLX ThetaData chain of ``test_option_split_basis`` (2024-04-24..05-01)
against NFLX's FMP closes, back-adjusted for the 10:1 split of 2025-11-17 -- so an as-traded
spot (~$551) and a split-adjusted one ($55.17) give visibly different greeks, and the E4
parity check can tell them apart.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.backtest.test_option_split_basis import (
    _NFLX_CALENDAR, _chain_store, _write_fmp_daily,
)

CONTRACT = "NFLX240621C00580000"   # bars 2024-04-24 .. 2024-05-01, OI 123/124
START, END, WARMUP = datetime(2024, 4, 24), datetime(2024, 6, 28), 2

LEG = {
    "symbol": "NFLX", "underlying_symbol": "NFLX", "contract_symbol": CONTRACT,
    "option_type": "call", "strike": 580.0, "expiry": "2024-06-21", "multiplier": 100,
    "direction": "buy", "size": 1,
    # 13:30 on Monday 04-29 -> the last completed session before it is Friday 04-26.
    "entry_time": "2024-04-29T13:30:00", "exit_time": "2024-05-02T19:45:00",
    "entry_price": 16.45, "exit_price": 14.77, "pnl": -168.0, "pnl_pct": -0.1,
    "exit_reason": "stop_loss", "transaction_id": 1,
}

#: What an as-traded run records (results.option_basis_guard exists iff it had a split basis).
AS_TRADED = {"option_basis_guard": {"checks": 3, "unevaluable": 0, "sampled_out": 9,
                                    "outliers_passed": 0, "seconds": 0.1,
                                    "unevaluable_symbols": {}}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A cache root holding the FMP daily file, the split calendar and the ThetaData tree."""
    import ba2_common.config as cfg
    from ba2_common.core import native_cache

    from app.services.backtest.option_basis_guard import clear_basis_guard_cache
    from app.services.backtest.option_split_basis import clear_split_basis_memo
    from app.services.backtest.parquet_options_provider import clear_worker_parquet_options_cache

    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    monkeypatch.delenv("BACKTEST_OPTIONS_RISK_FREE_RATE", raising=False)
    monkeypatch.delenv("BACKTEST_OPTIONS_PARQUET_ROOT", raising=False)
    _write_fmp_daily(tmp_path / "FMPOHLCVProvider" / "NFLX_1d.parquet")
    calendar = tmp_path / "fmp_history" / "mc_stock_split__NFLX.json"
    calendar.parent.mkdir(parents=True, exist_ok=True)
    calendar.write_text(json.dumps(_NFLX_CALENDAR))
    root = str(tmp_path / "ThetaDataOptionsProvider")
    _chain_store(root)
    clear_split_basis_memo()
    clear_worker_parquet_options_cache()
    clear_basis_guard_cache()
    yield root
    clear_split_basis_memo()
    clear_worker_parquet_options_cache()
    clear_basis_guard_cache()


@pytest.fixture
def published(env):
    """The option arrays published on this host, as any earlier run here would have left them;
    the worker caches then dropped, so the popup maps the published set itself."""
    from app.services.backtest import parquet_options_provider as pq

    pq._load_raw_underlying(env, "NFLX")
    pq.clear_worker_parquet_options_cache()
    return env


def _params(root, **overrides):
    params = {"options_store": "thetadata", "options_parquet_root": root,
              "options_risk_free_rate": 0.045, "execution_interval": "1d",
              "warmup_days": WARMUP, "enabled_instruments": ["NFLX", "AAPL"]}
    params.update(overrides)
    return {k: v for k, v in params.items() if v is not None}


def _backtest(params, results=AS_TRADED):
    return SimpleNamespace(id=1, engine_type="daily_expert", strategy_params=params,
                           optimization_id=None, results=results, trades=[dict(LEG)],
                           start_date=START, end_date=END)


def _reader(backtest):
    from app.services.backtest_trade_chart import option_store_provenance
    from app.services.parquet_contract_detail import open_parquet_contract_reader

    reader, problem = open_parquet_contract_reader(option_store_provenance(backtest), backtest)
    assert problem is None, problem
    return reader


def _run_provider(root):
    """The option reader exactly as ``run_daily_backtest`` wires it: preload + build_options_run."""
    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

    from app.services.backtest.options_store import build_options_run
    from app.services.backtest.price_source import AsOfPriceSource, MemoizedOHLCVProvider

    config = {"options_cache_db": "flag", "options_store": "thetadata",
              "options_parquet_root": root, "options_risk_free_rate": 0.045,
              "execution_interval": "1d", "enabled_instruments": ["NFLX"],
              "start_date": START, "end_date": END, "warmup_days": WARMUP}
    ohlcv = MemoizedOHLCVProvider(FMPOHLCVProvider(api_key="unused-cache-only"),
                                  START - timedelta(days=WARMUP), END, interval="1d",
                                  cached_only=True)
    ps = AsOfPriceSource(ohlcv_provider=ohlcv, interval="1d")
    ps.preload(["NFLX"], START, END, warmup_days=WARMUP)
    provider, basis = build_options_run(config, price_source=ps, ohlcv_provider=ohlcv)
    assert basis is not None
    return provider


# --------------------------------------------------------------------------- provenance
class TestParquetProvenance:
    def test_the_store_block_carries_the_reader_inputs(self):
        from app.services.backtest_trade_chart import option_store_provenance

        provenance = option_store_provenance(SimpleNamespace(strategy_params={
            "other": {"execution_interval": "1h"},          # a different block: not read
            "backtest": {"options_store": "thetadata", "options_parquet_root": "/t/Theta",
                         "options_risk_free_rate": 0.05, "execution_interval": "1d",
                         "warmup_days": 387}}))

        assert provenance.is_parquet and not provenance.is_sqlite
        assert provenance.parquet_root == "/t/Theta"
        assert provenance.risk_free_rate == 0.05
        assert provenance.execution_interval == "1d"
        assert provenance.warmup_days == 387

    def test_unrecorded_inputs_stay_none_and_camel_case_is_read(self):
        from app.services.backtest_trade_chart import option_store_provenance

        provenance = option_store_provenance(SimpleNamespace(strategy_params={
            "options_store": "parquet", "executionInterval": "1d", "warmupDays": 30}))

        assert provenance.is_parquet                     # the superseded name of tastytrade
        assert provenance.parquet_root is None and provenance.risk_free_rate is None
        assert provenance.execution_interval == "1d" and provenance.warmup_days == 30

    def test_an_optimization_row_supplies_them_through_a_real_session(self, db):
        from app.models.strategy_optimization import StrategyOptimization
        from app.services.backtest_trade_chart import option_store_provenance

        optimization = StrategyOptimization(
            strategy_id=1, name="theta-run", fitness_metric="sharpe",
            optimization_type="genetic",
            optimization_config={"backtest": {
                "options_store": "thetadata", "execution_interval": "1d", "warmup_days": 387,
                "enabled_instruments": ["NFLX"]}},
        )
        db.add(optimization)
        db.commit()
        db.refresh(optimization)

        provenance = option_store_provenance(
            SimpleNamespace(strategy_params={"model:x": 1}, optimization_id=optimization.id), db)

        assert provenance.store == "thetadata"
        assert provenance.execution_interval == "1d" and provenance.warmup_days == 387
        assert provenance.risk_free_rate is None          # not recorded -> not invented
        assert provenance.source == f"optimization_config#{optimization.id}"


# --------------------------------------------------------------------------- the adapter
class TestOnOrBefore:
    def test_latest_row_on_or_before_never_a_later_one(self, published):
        reader = _reader(_backtest(_params(published)))

        assert reader.latest_bar_on_or_before(CONTRACT, "2024-04-28")["date"] == "2024-04-26"
        assert reader.latest_bar_on_or_before(CONTRACT, "2024-05-01")["date"] == "2024-05-01"
        assert reader.latest_bar_on_or_before(CONTRACT, "2024-06-30")["date"] == "2024-05-01"
        assert reader.latest_bar_on_or_before(CONTRACT, "2024-04-23") is None
        assert reader.latest_bar_on_or_before("NFLX240621C09990000", "2024-05-01") is None

    def test_the_greeks_are_the_run_providers_own(self, published):
        # The run's reader, built the way run_daily_backtest builds it (preload +
        # build_options_run); the popup's, rebuilt from the saved result alone.
        provider = _run_provider(published)
        expected_bar = provider.get_bar(CONTRACT, date(2024, 5, 1))
        quote = provider.get_quote(CONTRACT, date(2024, 5, 1), data_session=date(2024, 5, 1))

        from app.services.backtest.parquet_options_provider import clear_worker_parquet_options_cache
        clear_worker_parquet_options_cache()   # the popup must not lean on the run's overlay
        row = _reader(_backtest(_params(published))).latest_bar_on_or_before(CONTRACT, "2024-05-01")

        for field in ("iv", "delta", "gamma", "theta", "vega", "open_interest", "volume", "date"):
            assert row[field] == expected_bar[field], field
        assert row["delta"] == quote.delta and row["iv"] == quote.implied_volatility
        # and they are the AS-TRADED ones: a ~5% OTM 51-DTE call, not a deep-OTM one vs $55
        assert 0.2 < row["delta"] < 0.5

    def test_the_setup_loads_the_underlying_once_per_request(self, published, monkeypatch):
        from app.services import parquet_contract_detail as m

        reader = _reader(_backtest(_params(published)))
        calls = []
        real = m.ParquetContractReader._build_context
        monkeypatch.setattr(m.ParquetContractReader, "_build_context",
                            lambda self, u: calls.append(u) or real(self, u))
        for day in ("2024-04-26", "2024-04-30", "2024-05-01"):
            reader.latest_bar_on_or_before(CONTRACT, day)
        assert calls == ["NFLX"]


# --------------------------------------------------------------------------- the popup
class TestPopup:
    def test_entry_and_exit_carry_the_runs_greeks(self, published):
        from app.services.backtest_trade_chart import build_trade_chart_context

        context = build_trade_chart_context(_backtest(_params(published)), 1)
        leg = context["legs"][0]
        entry, exit_ = leg["entryContract"], leg["exitContract"]

        assert entry["asOf"] == "2024-04-26" and exit_["asOf"] == "2024-05-01"
        assert entry["quality"] == exit_["quality"] == "approximate_prior_session"
        assert entry["iv"] is not None and 0.2 < exit_["delta"] < 0.5
        assert exit_["openInterest"] == 124                  # the parquet store has it
        assert "derived by the run's own reader" in exit_["reason"]
        assert "not recorded" not in exit_["reason"]         # the rate WAS recorded
        assert "option_store_unresolved" not in [n["code"] for n in context["notices"]]

    def test_an_unrecorded_rate_is_the_platform_default_and_says_so(self, published):
        from app.services.backtest_trade_chart import build_trade_chart_context

        context = build_trade_chart_context(
            _backtest(_params(published, options_risk_free_rate=None)), 1)
        exit_ = context["legs"][0]["exitContract"]

        assert exit_["delta"] is not None
        assert "r=0.045 (not recorded on the run" in exit_["reason"]

    def _fred_run(self, published, tmp_path, monkeypatch, identity=None):
        from ba2_providers.macro import fred_series

        from tests.backtest.fixtures.fred_rate import dgs3mo_rate, install_dgs3mo

        monkeypatch.setattr(fred_series, "CACHE_FOLDER", str(tmp_path))
        install_dgs3mo(tmp_path)
        record = dgs3mo_rate(START - timedelta(days=WARMUP), END).describe()
        if identity is not None:
            record["identity"] = identity
        results = {**AS_TRADED, "options_risk_free_rate_source": record}
        from app.services.backtest_trade_chart import build_trade_chart_context
        return build_trade_chart_context(
            _backtest(_params(published, options_risk_free_rate=None), results=results), 1)

    def test_a_fred_rate_run_is_rebuilt_from_the_same_cached_series(
            self, published, tmp_path, monkeypatch):
        exit_ = self._fred_run(published, tmp_path, monkeypatch)["legs"][0]["exitContract"]
        assert exit_["delta"] is not None
        assert "as-of FRED DGS3MO rate on" in exit_["reason"]
        assert "r=0.0" in exit_["reason"]          # DGS3MO was ~5.4% in spring 2024

    def test_a_fred_rate_run_on_a_different_cache_is_refused(
            self, published, tmp_path, monkeypatch):
        exit_ = self._fred_run(published, tmp_path, monkeypatch,
                               identity="fred-dgs3mo:0000000000000000")["legs"][0]["exitContract"]
        assert exit_["delta"] is None and exit_["quality"] == "unavailable"
        assert "not the one the run priced with" in exit_["reason"]

    def test_a_run_before_the_split_basis_that_was_off_basis_is_withheld(self, published):
        # No option_basis_guard in the results: the run inverted against $55.17 while the
        # chain trades at ~$551. Those greeks were wrong; they are not shown.
        from app.services.backtest_trade_chart import build_trade_chart_context

        context = build_trade_chart_context(_backtest(_params(published), results={}), 1)
        exit_ = context["legs"][0]["exitContract"]

        assert exit_["quality"] == "unavailable"
        assert exit_["iv"] is None and exit_["delta"] is None
        assert "predates the as-traded split basis" in exit_["reason"]


class TestRefusals:
    def _notice(self, backtest):
        from app.services.backtest_trade_chart import build_trade_chart_context

        context = build_trade_chart_context(backtest, 1)
        leg = context["legs"][0]
        assert leg["entryContract"]["iv"] is None and leg["exitContract"]["delta"] is None
        return next(n["message"] for n in context["notices"]
                    if n["code"] == "option_store_unresolved")

    def test_an_unrecorded_interval_is_refused_naming_the_store(self, env):
        message = self._notice(_backtest(_params(env, execution_interval=None)))
        assert "'thetadata'" in message and "execution_interval" in message

    def test_an_unrecorded_warmup_is_refused(self, env):
        assert "warmup_days" in self._notice(_backtest(_params(env, warmup_days=None)))

    def test_a_process_rate_override_with_no_recorded_rate_is_refused(self, env, monkeypatch):
        monkeypatch.setenv("BACKTEST_OPTIONS_RISK_FREE_RATE", "0.03")
        message = self._notice(_backtest(_params(env, options_risk_free_rate=None)))
        assert "BACKTEST_OPTIONS_RISK_FREE_RATE" in message

    def test_a_guardless_parquet_result_is_refused(self, env):
        message = self._notice(_backtest(_params(env), results={"option_basis_guard": None}))
        assert "split-basis guard" in message

    def test_unpublished_arrays_are_refused_and_nothing_is_created(self, env, tmp_path):
        from app.services.backtest_trade_chart import build_trade_chart_context

        context = build_trade_chart_context(_backtest(_params(env)), 1)
        exit_ = context["legs"][0]["exitContract"]

        assert exit_["quality"] == "unavailable" and exit_["delta"] is None
        assert "not built on this host" in exit_["reason"]
        assert not (tmp_path / "_derived" / "ThetaDataOptionsProvider").exists()

    def test_a_missing_split_calendar_refuses_the_as_traded_spot(self, published, tmp_path):
        (tmp_path / "fmp_history" / "mc_stock_split__NFLX.json").unlink()
        from app.services.backtest_trade_chart import build_trade_chart_context

        exit_ = build_trade_chart_context(_backtest(_params(published)), 1)["legs"][0]["exitContract"]

        assert exit_["delta"] is None
        assert "split basis" in exit_["reason"] and "cannot be rebuilt" in exit_["reason"]
