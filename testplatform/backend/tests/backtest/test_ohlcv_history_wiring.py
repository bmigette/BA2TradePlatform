"""The OHLCV history source (fmp | alpaca) reaches every backtest switch point (plan Task 7).

Pinned here, in the order a wrong history would bite:

* a trial built from a job stamped ``alpaca`` asks the registry for ``alpaca_hist`` (never ``fmp``),
  and the stamp survives ``_build_daily_trial_config`` (a WHITELIST: a key missing there is dropped);
* a stored block / row WITHOUT the stamp ran before the switch and re-runs on ``fmp``;
* an unknown stamp refuses;
* an OPTION job stamped ``alpaca`` refuses before any trial (launcher, trial config, run);
* the job name, the checkpoint fingerprint and the in-process bar caches differ between the sources;
* the what-if, the chart and the error texts follow the row's / job's source.

The provider class is NOT imported: ``get_provider`` is monkeypatched and the registry key asserted.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TOOLS = os.path.normpath(os.path.join(_BACKEND, "..", "..", "tools"))

from ba2_common.core.ohlcv_history import (  # noqa: E402
    OHLCV_HISTORY_SOURCE_KEY,
    UnknownHistorySource,
)

RUN_START = datetime(2024, 3, 4)
RUN_END = datetime(2024, 3, 8)


class _Stop(Exception):
    """Raised by the recording ``get_provider`` once the key under test has been asked for."""


def _block(**extra):
    block = {
        "backtest_id": "hs", "name": "hs-trial",
        "start_date": RUN_START.isoformat(), "end_date": RUN_END.isoformat(),
        "enabled_instruments": ["AAA", "BBB"],
        "experts": [{"class": "FactorRanker", "settings": {"universe_source": "static"}}],
        "initial_capital": 100_000.0,
        "account_settings": {"starting_cash": 100_000.0, "commission_per_trade": 0.0,
                             "slippage_bps": 0.0, "fill_model": "next_bar_open"},
        "warmup_days": 30, "seed": 7, "subtype": "daily_expert", "execution_interval": "1d",
    }
    block.update(extra)
    return block


def _trial(block, decoded=None):
    from app.models.strategy import Strategy
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params

    decoded = decoded if decoded is not None else decode_params(
        Strategy(name="hs", entry_rules=[], exit_rules=[]), {"model:top_n": 5})
    return _build_daily_trial_config(block, decoded, None, option_trade_records=False)


def _record_keys(monkeypatch):
    import ba2_providers

    seen = []

    def fake(category, name, **kwargs):
        seen.append((category, name))
        if category == "ohlcv":
            raise _Stop((category, name))
        raise AssertionError(f"unexpected provider request before the OHLCV one: {(category, name)}")

    monkeypatch.setattr(ba2_providers, "get_provider", fake)
    return seen


# ---------------------------------------------------------------- the stamp reaches the trial


@pytest.mark.parametrize("stamp,expected", [("alpaca", "alpaca"), ("fmp", "fmp"), (None, "fmp")])
def test_stamp_survives_the_trial_config_whitelist(stamp, expected):
    block = _block() if stamp is None else _block(**{OHLCV_HISTORY_SOURCE_KEY: stamp})
    assert _trial(block)[OHLCV_HISTORY_SOURCE_KEY] == expected


def test_unknown_stamp_refuses():
    with pytest.raises(UnknownHistorySource):
        _trial(_block(**{OHLCV_HISTORY_SOURCE_KEY: "yahoo"}))


@pytest.mark.parametrize("stamp,key", [("alpaca", "alpaca_hist"), ("fmp", "fmp"), (None, "fmp")])
def test_a_trial_asks_the_registry_for_its_jobs_history(monkeypatch, stamp, key):
    from app.services.backtest import daily_backtest_handler as H
    from tests.backtest.fixtures.e2e_support import ensure_host_schema

    ensure_host_schema()
    seen = _record_keys(monkeypatch)
    block = _block() if stamp is None else _block(**{OHLCV_HISTORY_SOURCE_KEY: stamp})
    with pytest.raises(_Stop):
        H.run_daily_backtest(_trial(block))
    assert ("ohlcv", key) in seen
    assert [k for c, k in seen if c == "ohlcv"] == [key]     # and never the other history


def test_run_refuses_an_unknown_stamp_before_reading_anything(monkeypatch):
    from app.services.backtest import daily_backtest_handler as H
    from tests.backtest.fixtures.e2e_support import ensure_host_schema

    ensure_host_schema()
    seen = _record_keys(monkeypatch)
    config = {**_trial(_block()), OHLCV_HISTORY_SOURCE_KEY: "yahoo"}
    with pytest.raises(UnknownHistorySource):
        H.run_daily_backtest(config)
    assert seen == []


# ---------------------------------------------------------------- option jobs


def test_option_trial_stamped_alpaca_is_accepted_since_task_12():
    block = _block(options_cache_db="x.sqlite", **{OHLCV_HISTORY_SOURCE_KEY: "alpaca"})
    assert _trial(block)[OHLCV_HISTORY_SOURCE_KEY] == "alpaca"


def test_option_trial_on_a_source_without_a_split_basis_refuses_before_any_trial(monkeypatch):
    from app.services.backtest import history_source as _hs
    from app.services.backtest.history_source import OptionHistorySourceUnsupported

    monkeypatch.setattr(_hs, "OPTION_SUPPORTED_SOURCES", ("fmp",))
    block = _block(options_cache_db="x.sqlite", **{OHLCV_HISTORY_SOURCE_KEY: "alpaca"})
    with pytest.raises(OptionHistorySourceUnsupported):
        _trial(block)


def test_option_run_on_a_source_without_a_split_basis_refuses_before_reading_bars(monkeypatch):
    from app.services.backtest import daily_backtest_handler as H
    from app.services.backtest import history_source as _hs
    from app.services.backtest.history_source import OptionHistorySourceUnsupported
    from tests.backtest.fixtures.e2e_support import ensure_host_schema

    ensure_host_schema()
    seen = _record_keys(monkeypatch)
    monkeypatch.setattr(_hs, "OPTION_SUPPORTED_SOURCES", ("fmp",))
    config = {**_trial(_block()), "options_cache_db": "x.sqlite", OHLCV_HISTORY_SOURCE_KEY: "alpaca"}
    with pytest.raises(OptionHistorySourceUnsupported):
        H.run_daily_backtest(config)
    assert seen == []


def test_option_trial_on_fmp_is_unchanged():
    block = _block(options_cache_db="x.sqlite", **{OHLCV_HISTORY_SOURCE_KEY: "fmp"})
    # the window check of an option run needs a 2024-02+ start; this block's 2024-03 start passes
    assert _trial(block)[OHLCV_HISTORY_SOURCE_KEY] == "fmp"


# ---------------------------------------------------------------- launcher


@pytest.fixture(scope="module")
def launcher():
    """The launcher module tests/backtest/conftest.py already imported (and verified to be THIS checkout's):
    loading a second copy re-runs its DB bootstrap and unseeds the session's credentials."""
    import ba2test_launcher
    return ba2test_launcher


def _args(source=None):
    return SimpleNamespace(ohlcv_history_source=source)


def test_launcher_defaults_equity_to_alpaca_and_options_to_fmp(launcher):
    equity = launcher._EXPERT_OPT["FactorRanker"]
    assert launcher._resolve_history_source(_args(), "optimize", equity, None) == "alpaca"
    assert launcher._resolve_history_source(_args("fmp"), "optimize", equity, None) == "fmp"
    # an option STRATEGY on a classic expert, and an options expert: both default to fmp
    classic = next(s for k, s in launcher._EXPERT_OPT.items() if not s.get("options") and not s.get("bypass"))
    assert launcher._resolve_history_source(_args(), "optimize", classic, "O_LC") == "fmp"
    options_expert = next((s for s in launcher._EXPERT_OPT.values() if s.get("options")), None)
    if options_expert is not None:
        assert launcher._resolve_history_source(_args(), "optimize", options_expert, None) == "fmp"


def test_launcher_accepts_an_option_job_stated_as_alpaca(launcher):
    classic = next(s for s in launcher._EXPERT_OPT.values() if not s.get("options") and not s.get("bypass"))
    assert launcher._resolve_history_source(_args("alpaca"), "optimize", classic, "O_LC") == "alpaca"


def test_launcher_refuses_an_option_job_on_a_source_without_a_split_basis(launcher, monkeypatch):
    from app.services.backtest import history_source as _hs
    monkeypatch.setattr(_hs, "OPTION_SUPPORTED_SOURCES", ("fmp",))
    classic = next(s for s in launcher._EXPERT_OPT.values() if not s.get("options") and not s.get("bypass"))
    with pytest.raises(SystemExit) as e:
        launcher._resolve_history_source(_args("alpaca"), "optimize", classic, "O_LC")
    assert "REFUSED" in str(e.value) and "option job" in str(e.value)


@pytest.mark.parametrize("command", ["optimize", "optimize-batch"])
def test_every_job_creating_subcommand_has_the_flag(launcher, capsys, command, _seed_backtest_credentials):
    from ba2_common.core import db as common_db

    cwd = os.getcwd()
    try:
        with pytest.raises(SystemExit):
            launcher.main([command, "--help"])      # --help exits after argparse built the parser
    finally:
        # main() re-points the shared DB engine and the cwd at the test platform's; put the
        # session's seeded throwaway DB back (see conftest._seed_backtest_credentials)
        os.chdir(cwd)
        common_db.configure_db(str(_seed_backtest_credentials))
    out = capsys.readouterr().out
    assert "--ohlcv-history-source" in out and "OPTION jobs" in out


def test_single_backtest_cli_stamps_its_config():
    from scripts.run_daily_backtest import _build_real_config, _parse_args

    args = _parse_args(["--start", "2024-03-04", "--end", "2024-03-08"])
    assert _build_real_config(args)[OHLCV_HISTORY_SOURCE_KEY] == "alpaca"
    args = _parse_args(["--start", "2024-03-04", "--end", "2024-03-08", "--ohlcv-history-source", "fmp"])
    assert _build_real_config(args)[OHLCV_HISTORY_SOURCE_KEY] == "fmp"


# ---------------------------------------------------------------- job identity


def test_job_name_differs_between_the_sources():
    if _TOOLS not in sys.path:
        sys.path.insert(0, _TOOLS)
    from matrix_flags import history_source_tokens, job_name_with_digest, with_history_source_name

    base = "scr-large-FMPRating-S2"
    fmp = with_history_source_name(base, "fmp")
    alpaca = with_history_source_name(base, "alpaca")
    assert fmp == base                      # a legacy job keeps its name (and so its checkpoint)
    assert alpaca != fmp and alpaca == with_history_source_name(alpaca, "alpaca")
    # and the digest, which hashes the optimize argv, moves with the stated source
    cmd = ["python", "optimize", "--expert", "X"]
    assert (job_name_with_digest("n", cmd + history_source_tokens("fmp"))
            != job_name_with_digest("n", cmd + history_source_tokens("alpaca")))


def test_checkpoint_fingerprint_differs_but_legacy_is_unchanged():
    from app.services.strategy_optimization_handler import checkpoint_fingerprint

    space = {"a": {"optimize": True, "min": 0, "max": 1}}
    ga = {"populationSize": 4, "generations": 2}
    legacy = checkpoint_fingerprint(space, ga)
    assert checkpoint_fingerprint(space, ga, None, None, None, None, "fmp") == legacy
    assert checkpoint_fingerprint(space, ga, None, None, None, None, None) == legacy
    assert checkpoint_fingerprint(space, ga, None, None, None, None, "alpaca") != legacy


# ---------------------------------------------------------------- stored rows


def _row(strategy_params=None, optimization_id=None):
    return SimpleNamespace(strategy_params=strategy_params, optimization_id=optimization_id)


def test_unstamped_stored_row_is_fmp_and_a_stamped_one_follows_its_stamp():
    from app.services.backtest.history_source import stored_history_source

    assert stored_history_source(_row({"enabled_instruments": ["A"]})) == "fmp"
    assert stored_history_source(_row(None)) == "fmp"
    assert stored_history_source(_row({"nested": {OHLCV_HISTORY_SOURCE_KEY: "alpaca"}})) == "alpaca"
    with pytest.raises(UnknownHistorySource):
        stored_history_source(_row({OHLCV_HISTORY_SOURCE_KEY: "yahoo"}))


def test_stored_row_reads_its_optimizations_stamp():
    from app.models.strategy_optimization import StrategyOptimization
    from app.services.backtest.history_source import stored_history_source

    class _Session:
        def __init__(self, config):
            self._opt = SimpleNamespace(optimization_config=config)

        def get(self, model, key):
            assert model is StrategyOptimization
            return self._opt

    assert stored_history_source(_row(None, 5), _Session({"backtest": {OHLCV_HISTORY_SOURCE_KEY: "alpaca"}})) == "alpaca"
    assert stored_history_source(_row(None, 5), _Session({"backtest": {"start_date": "2024"}})) == "fmp"
    with pytest.raises(ValueError):
        stored_history_source(_row(None, 5))          # cannot read the stamp: never assume fmp


def test_unstamped_stored_block_reruns_on_fmp(monkeypatch):
    """rerun_handler / top-N persist feed a stored block back through the trial config."""
    from app.services.backtest import daily_backtest_handler as H
    from tests.backtest.fixtures.e2e_support import ensure_host_schema

    ensure_host_schema()
    seen = _record_keys(monkeypatch)
    with pytest.raises(_Stop):
        H.run_daily_backtest(_trial(_block()))
    assert seen == [("ohlcv", "fmp")]


def test_whatif_reads_the_rows_history_folder(monkeypatch):
    from ba2_common.core import native_cache
    from app.services.backtest.whatif import recompute_curves

    asked = []
    monkeypatch.setattr(native_cache, "find_timeseries_path",
                        lambda provider, symbol, interval: asked.append(provider))
    d1, d2, d3 = (datetime(2024, 3, d) for d in (4, 5, 6))
    kwargs = dict(
        initial_capital=100_000.0,
        trades=[{"symbol": "AAA", "entry_time": d1.isoformat(), "exit_time": d3.isoformat(),
                 "direction": "buy", "entry_price": 10.0, "size": 5.0, "pnl": 100.0, "pnl_pct": 1.0,
                 "bars_held": 2}],
        equity_curve=[{"date": d.isoformat(), "equity": 100_000.0} for d in (d1, d2, d3)])
    recompute_curves(history_source="alpaca", **kwargs)
    recompute_curves(history_source="fmp", **kwargs)
    assert asked == ["AlpacaHistoryOHLCVProvider", "FMPOHLCVProvider"]


# ---------------------------------------------------------------- seam, caches, error texts


def test_resolver_serves_the_runs_history_when_no_override_is_installed(monkeypatch):
    import ba2_providers
    from ba2_common.core import TradeConditions
    from app.services.backtest import seam_wiring as sw

    asked = []
    monkeypatch.setattr(ba2_providers, "get_provider",
                        lambda category, name, **kw: asked.append((category, name)) or object())
    original = TradeConditions.get_provider_resolver()
    try:
        sw._wire_provider_resolver()      # a resolver bound to THIS test's get_provider
        sw.set_backtest_ohlcv_override(None)
        sw.set_backtest_history_source("alpaca")
        TradeConditions._get_provider("ohlcv", "fmp")          # what LiveProviderBundle.ohlcv() asks
        sw.make_indicator_provider()
        sw.set_backtest_history_source(None)
        TradeConditions._get_provider("ohlcv", "fmp")          # outside a run: the name as asked
    finally:
        sw.set_backtest_history_source(None)
        if original is not None:
            TradeConditions.set_provider_resolver(original)
    assert asked[0] == ("ohlcv", "alpaca_hist")
    assert ("ohlcv", "alpaca_hist") in asked[1:3]
    assert asked[-1] == ("ohlcv", "fmp")


def test_history_source_setter_refuses_an_unknown_name():
    from app.services.backtest import seam_wiring as sw

    with pytest.raises(UnknownHistorySource):
        sw.set_backtest_history_source("yahoo")


def test_bundle_names_its_ohlcv_provider():
    from ba2_common.core.backtest_context import LiveProviderBundle

    seen = []
    LiveProviderBundle(lambda c, n, **kw: seen.append((c, n))).ohlcv()
    LiveProviderBundle(lambda c, n, **kw: seen.append((c, n)), ohlcv_name="alpaca_hist").ohlcv()
    assert seen == [("ohlcv", "fmp"), ("ohlcv", "alpaca_hist")]


class AlpacaHistoryOHLCVProvider:
    """Stands in for the real class: only its NAME matters (it is the cache folder)."""

    def get_provider_name(self):
        return type(self).__name__


class FMPOHLCVProvider(AlpacaHistoryOHLCVProvider):
    pass


def _miss_message(provider, tmp_path, monkeypatch):
    import ba2_common.config as cfg
    from ba2_common.core import native_cache
    from app.services.backtest.price_source import (
        AsOfPriceSource, BacktestCacheMiss, MemoizedOHLCVProvider)

    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    ohlcv = MemoizedOHLCVProvider(provider, RUN_START, RUN_END, interval="1d", cached_only=True)
    ps = AsOfPriceSource(ohlcv_provider=ohlcv, interval="1d")
    with pytest.raises(BacktestCacheMiss) as e:
        ps.preload(["ZZZ"], RUN_START, RUN_END, warmup_days=5)
    return str(e.value)


def test_cache_miss_text_names_the_jobs_source_not_fmp(tmp_path, monkeypatch):
    text = _miss_message(AlpacaHistoryOHLCVProvider(), tmp_path, monkeypatch)
    assert "CACHE_FOLDER/AlpacaHistoryOHLCVProvider/" in text
    assert "FMPOHLCVProvider" not in text and "--provider fmp" not in text
    legacy = _miss_message(FMPOHLCVProvider(), tmp_path, monkeypatch)
    assert "CACHE_FOLDER/FMPOHLCVProvider/" in legacy and "fetch-cache --provider fmp" in legacy


def test_in_process_series_caches_are_keyed_by_the_history_folder():
    from app.services.backtest.price_source import MemoizedOHLCVProvider, _history_cache_dirname_of

    a = MemoizedOHLCVProvider(AlpacaHistoryOHLCVProvider(), RUN_START, RUN_END)
    b = MemoizedOHLCVProvider(FMPOHLCVProvider(), RUN_START, RUN_END)
    assert _history_cache_dirname_of(a) == "AlpacaHistoryOHLCVProvider"
    assert _history_cache_dirname_of(b) == "FMPOHLCVProvider"


def _chart_backtest(strategy_params):
    leg = {"symbol": "AAA", "underlying_symbol": "AAA", "direction": "buy", "size": 1,
           "entry_time": "2024-03-04T14:30:00", "exit_time": "2024-03-06T14:30:00",
           "entry_price": 10.0, "exit_price": 11.0, "pnl": 1.0, "pnl_pct": 0.1, "transaction_id": 1}
    return SimpleNamespace(id=1, engine_type="daily_expert", strategy_params=strategy_params,
                           optimization_id=None, results={}, trades=[leg],
                           start_date=RUN_START, end_date=RUN_END)


@pytest.mark.parametrize("params,folder", [
    ({OHLCV_HISTORY_SOURCE_KEY: "alpaca"}, "AlpacaHistoryOHLCVProvider"),
    ({OHLCV_HISTORY_SOURCE_KEY: "fmp"}, "FMPOHLCVProvider"),
    ({}, "FMPOHLCVProvider"),                       # a row from before the switch
])
def test_trade_chart_reads_the_rows_history_folder(params, folder):
    from app.services.backtest_trade_chart import build_trade_chart_context

    assert build_trade_chart_context(_chart_backtest(params), 1)["underlying"]["provider"] == folder
