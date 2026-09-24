"""pullback_rsi: the opt-in PullbackReversion exploration family (plan 2026-09-24, Task A3).

The default campaign must stay byte-identical; the extension is selected by name only.
No broker, provider request, live database or grid run.
"""
from copy import deepcopy
from datetime import date
import itertools
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.strategy_research.exploration import profiles as P, runtime as R
from tools.strategy_research.exploration import run_exploration as D
from app.services.strategy_param_space import collect_param_space, decode_params

# The default campaign's pin, the one test_research10_market_conditions pins inline (that test
# stays unmodified); test_the_default_pin_is_test_research10s keeps the two from drifting.
DEFAULT_FINGERPRINT = "798c8787f90a6e1215f964edc453d3c58a29138fec845cefab1f7d2873bb2fa9"
PINS = {"ta-structure-v1": "b" * 64}


def space(job):
    return collect_param_space(SimpleNamespace(**job["strategy"]), job["optimization_config"]["expert_params"])


def grid(job):
    """test_research6_driver.grid: every concrete candidate of an exhaustive job."""
    axes = space(job)
    values = [[s["min"] + i * s["step"] for i in range(round((s["max"] - s["min"]) / s["step"]) + 1)]
              for s in axes.values()]
    return [dict(zip(axes, values)) for values in itertools.product(*values)]


def jobs(**kwargs):
    return {j["variant"]: j for j in P.build_manifest(families=("pullback_rsi",), **kwargs)["jobs"]}


def fields(tree):
    return [(n["field"], n["op"]) for n in P.walk(tree) if "field" in n]


# --------------------------------------------------------------------------- manifest
def test_five_jobs_and_72_grid_candidates():
    selected = jobs()
    assert list(selected) == ["long_sma5", "long_choch", "long_rsi", "short_sma5", "short_spy"]
    assert {v: len(grid(j)) for v, j in selected.items()} == {
        "long_sma5": 12, "long_choch": 12, "long_rsi": 24, "short_sma5": 12, "short_spy": 12}
    assert sum(len(grid(j)) for j in selected.values()) == 72
    for job in selected.values():
        D.verify_job(job)
        assert job["family"] == "pullback_rsi" and job["expert"] == "PullbackReversion"
        assert not job["fixed"] and job["optimization_type"] == "brute_force"
        assert job["fitness_metric"] == "consistent_annual_return"


def test_default_campaign_is_unchanged_and_excludes_the_extension():
    manifest = P.build_manifest()
    assert P.fingerprint(manifest) == DEFAULT_FINGERPRINT
    assert len(manifest["jobs"]) == 35
    assert "pullback_rsi" not in P.FAMILIES and "pullback_rsi" not in {j["family"] for j in manifest["jobs"]}
    assert P.ALL_FAMILIES == P.FAMILIES + P.EXTENSION_FAMILIES == P.FAMILIES + ("pullback_rsi",)


def test_the_default_pin_is_test_research10s():
    source = Path(__file__).with_name("test_research10_market_conditions.py").read_text(encoding="utf-8")
    assert f'== "{DEFAULT_FINGERPRINT}"' in source


def test_unknown_family_and_variant_fail():
    with pytest.raises(ValueError, match="known strategy families"):
        P.build_manifest(families=("pullback_rsx",))
    with pytest.raises(ValueError, match="Unknown strategy family"):
        P.variants("pullback_rsx", {})


def test_settings_genes_and_backtest_contract():
    base = P.load_baselines()["large_ds"]["backtest"]
    expected = {"long_sma5": ("long", "sma200", "sma5"), "long_choch": ("long", "sma200", "sma5_or_choch"),
                "long_rsi": ("long", "sma200", "rsi"), "short_sma5": ("short", "sma200", "sma5"),
                "short_spy": ("short", "sma200_and_spy", "sma5")}
    from ba2_experts.PullbackReversion import PullbackReversion
    known = set(PullbackReversion.get_merged_settings_definitions())
    for variant, job in jobs().items():
        config = job["optimization_config"]
        bt, params = config["backtest"], config["expert_params"]
        settings = bt["experts"][0]["settings"]
        assert (settings["direction"], settings["trend_gate"], settings["exit_mode"]) == expected[variant]
        # Every setting is one the expert (or its interface) declares: no DeterministicScorer leftovers.
        assert set(settings) <= known and set(params) <= known
        assert params["rsi_period"] == P.numeric_range(2, 3, 1, "int")
        assert params["entry_threshold"] == P.numeric_range(5.0, 15.0, 5.0)
        assert ("rsi_exit" in params) == (variant == "long_rsi")
        if variant == "long_rsi":
            assert params["rsi_exit"] == P.numeric_range(60.0, 70.0, 10.0)
        days = [n for n in P.walk(job["strategy"]["exit_rules"]) if n.get("field") == "days_opened"]
        assert len(days) == 1 and (days[0]["value_min"], days[0]["value_max"], days[0]["value_step"]) == (5, 10, 5)
        # The point-in-time large-cap screen, re-run daily, on a daily entry schedule.
        assert bt["screener_opt"]["cadence_days"] == 1
        assert bt["screener_opt"]["base_settings"]["market_cap_min"] == 10000000000
        assert bt["run_schedule_override"] == bt["manage_schedule_override"] == base["manage_schedule_override"]
        assert settings["execution_schedule_enter_market"] == bt["run_schedule_override"]
        # New-idea costs, sizing and warmup; the warmup covers the expert's own need.
        assert bt["account_settings"]["spread_bps"] == 5.0 and bt["stress_spread_bps"] == 5.0
        assert settings["sizing_mode"] == "notional" and settings["risk_per_trade_pct"] == 10.0
        assert settings["use_atr_stop"] is False and settings["regime_overlay_enabled"] is False
        derived_days = PullbackReversion.BACKTEST_WARMUP_BARS * 1.45 + 10
        assert bt["warmup_days"] == 600 >= derived_days
        short = variant.startswith("short")
        assert bt.get("enable_short", False) is short
        assert settings["enable_sell"] is short


def test_inherited_settings_are_derived_from_the_expert():
    """Kept = every large_ds setting the expert's interface declares that the expert does not
    declare itself; then exactly the expert's own settings are added."""
    from ba2_experts.PullbackReversion import PullbackReversion
    declared = PullbackReversion.get_merged_settings_definitions()
    own = set(PullbackReversion.get_settings_definitions())
    assert own == {"direction", "trend_gate", "rsi_period", "entry_threshold", "exit_mode", "rsi_exit"}
    source = P.load_baselines()["large_ds"]["backtest"]["experts"][0]["settings"]
    inherited = {k for k in source if k in declared and k not in own}
    assert {"risk_per_trade_pct", "sizing_mode", "enable_sell", "screener_market_cap_min"} <= inherited
    assert not inherited & {"w_technical", "theta_buy", "index_symbol"}  # DeterministicScorer's own
    for job in jobs().values():
        settings = job["optimization_config"]["backtest"]["experts"][0]["settings"]
        assert set(settings) == inherited | own


def test_long_rule_shapes():
    for variant in ("long_sma5", "long_choch", "long_rsi"):
        strategy = jobs()[variant]["strategy"]
        [entry] = strategy["entry_rules"]
        assert fields(entry["conditions"]) == [
            ("bullish", "is_true"), ("has_no_position", "is_true"), ("days_since_last_close", ">")]
        assert entry["actions"] == [
            {"action_type": "buy"},
            {"action_type": "adjust_stop_loss", "reference_value": "order_open_price", "action_value": -8.0}]
        assert entry["continue_processing"] is False
        types = {a["action_type"] for a in entry["actions"]}
        assert "sell" not in types and "adjust_take_profit" not in types
        reverse, timeout = strategy["exit_rules"]
        assert reverse == P.signal_close("bearish")
        assert reverse == {"id": "research_bearish", "conditions": {"type": "AND", "conditions": [
            {"id": "research_bearish_flag", "field": "bearish", "op": "is_true"}]},
            "actions": [{"action_type": "close"}], "continue_processing": False}
        assert timeout["id"] == "research_timeout"
        assert fields(timeout["conditions"]) == [("has_position", "is_true"), ("days_opened", ">")]


def test_short_rule_shapes():
    for variant in ("short_sma5", "short_spy"):
        strategy = jobs()[variant]["strategy"]
        [entry] = strategy["entry_rules"]
        assert fields(entry["conditions"]) == [
            ("bearish", "is_true"), ("has_no_position", "is_true"), ("days_since_last_close", ">")]
        # -8.0 is 8% ABOVE a short entry: AdjustStopLossAction reads the percent in the
        # position's direction (reference * (1 - pct/100) for a short), the long's -8.0 mirrored.
        assert entry["actions"] == [
            {"action_type": "sell"},
            {"action_type": "adjust_stop_loss", "reference_value": "order_open_price", "action_value": -8.0}]
        types = {a["action_type"] for a in entry["actions"]}
        assert "buy" not in types and "adjust_take_profit" not in types
        reverse, timeout = strategy["exit_rules"]
        assert reverse == {"id": "research_bullish", "conditions": {"type": "AND", "conditions": [
            {"id": "research_bullish_flag", "field": "bullish", "op": "is_true"}]},
            "actions": [{"action_type": "close"}], "continue_processing": False}
        assert timeout["id"] == "research_timeout"


def test_market_condition_attach_gates_the_new_entries_only():
    gated = jobs(search="genetic", market_condition_profile="ta-structure-v1",
                 market_condition_manifest=f"ta-structure-v1={PINS['ta-structure-v1']}")
    plain = jobs(search="genetic")
    from ba2_common.core.market_condition_rules import iter_market_condition_leaves
    for variant, job in gated.items():
        D.verify_job(job)
        [entry] = job["strategy"]["entry_rules"]
        leaves = list(iter_market_condition_leaves(entry))
        assert leaves and all(leaf["id"].startswith("research-pullback_rsi-entry1-market") for _, leaf in leaves)
        assert entry["actions"] == plain[variant]["strategy"]["entry_rules"][0]["actions"]
        assert job["strategy"]["exit_rules"] == plain[variant]["strategy"]["exit_rules"]
        assert not list(iter_market_condition_leaves(job["strategy"]["exit_rules"]))
        extra = [g for g in space(job) if "-market-" in g]
        bt = job["optimization_config"]["backtest"]
        assert len(extra) == bt["market_condition"]["gene_count"] == 9
        assert bt["experts"][0]["settings"]["market_condition_profile"] == "ta-structure-v1"
        # All modes off decodes to the ungated rules.
        off = {g: "off" for g in extra if g.endswith(":mode")}
        assert (decode_params(SimpleNamespace(**job["strategy"]), off)["entry_rules"]
                == decode_params(SimpleNamespace(**plain[variant]["strategy"]), {})["entry_rules"])


# --------------------------------------------------------------------------- CLI
def test_cli_dry_run_selects_the_extension(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Preview attempted execution or DB access")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(D.subprocess, "run", forbidden)
    assert D.main(["--families", "pullback_rsi", "--dry-run", "--output-dir", str(tmp_path)]) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert [j["variant"] for j in manifest["jobs"]] == list(jobs())
    assert "5 jobs" in capsys.readouterr().out
    # Default CLI selection is still the ten families.
    assert D.parser().parse_args([]).families == list(P.FAMILIES)


def test_cli_rejects_an_unknown_family():
    with pytest.raises(SystemExit):
        D.parser().parse_args(["--families", "pullback_rsx"])


# --------------------------------------------------------------------------- refusing short jobs
def test_refusal_is_keyed_on_a_sell_entry_with_enable_short():
    short, long_ = jobs()["short_sma5"], jobs()["long_sma5"]
    assert R.opens_equity_short(short) and R.opens_equity_short(jobs()["short_spy"])
    assert not R.opens_equity_short(long_)
    no_flag = deepcopy(short)
    del no_flag["optimization_config"]["backtest"]["enable_short"]
    assert not R.opens_equity_short(no_flag)  # a `sell` entry without shorts only ever closes a long
    flag_only = deepcopy(long_)
    flag_only["optimization_config"]["backtest"]["enable_short"] = True
    assert not R.opens_equity_short(flag_only)  # enable_short alone opens nothing through `buy`
    R.refuse_unrunnable([long_, flag_only])
    with pytest.raises(ValueError, match=r"equity short entries cannot open yet .* 1 selected job"):
        R.refuse_unrunnable([long_, short])


def test_preflight_refuses_short_jobs_loudly(tmp_path):
    for variant in ("short_sma5", "short_spy"):
        with pytest.raises(ValueError, match="equity short entries cannot open yet"):
            R.preflight(jobs()[variant], tmp_path)


@pytest.mark.parametrize("mode", ["--preflight", "--run"])
def test_cli_refuses_a_selection_with_a_short_job_before_any_job(monkeypatch, tmp_path, capsys, mode):
    def forbidden(*args, **kwargs):
        pytest.fail("a job was prepared or started before the selection was refused")
    for name in ("preflight", "run_jobs", "check_database"):
        monkeypatch.setattr(D, name, forbidden)
    # The long job comes first: it must not run before the short one is found.
    argv = ["--families", "pullback_rsi", "--variants", "long_sma5", "short_spy", mode,
            "--output-dir", str(tmp_path)]
    assert D.main(argv) == 1
    assert "equity short entries cannot open yet" in capsys.readouterr().err

    started = []
    monkeypatch.setattr(D, "preflight", lambda job, cache_dir: started.append(job["variant"])
                        or {**job, "preflight": {"symbols": 1}})
    monkeypatch.setattr(D, "run_jobs", lambda selected, output, args: started.extend(
        j["variant"] for j in selected) or 0)
    argv = ["--families", "pullback_rsi", "--variants", "long_sma5", "long_choch", "long_rsi", mode,
            "--output-dir", str(tmp_path)]
    assert D.main(argv) == 0
    assert started == ["long_sma5", "long_choch", "long_rsi"]


# --------------------------------------------------------------------------- preflight
def _cache(tmp_path, symbols, intervals=("1d", "5min"), first="2018-01-01", last="2025-12-31"):
    for symbol in symbols:
        for interval in intervals:
            pd.DataFrame({"Date": pd.to_datetime([first, "2020-01-02", last], utc=True)}).to_parquet(
                tmp_path / f"{symbol}_{interval}.parquet")


def _screen_one(monkeypatch, tmp_path, job):
    from ba2_providers.screener import metric_store as ms
    job["optimization_config"]["backtest"]["screener_opt"]["store"] = str(tmp_path)
    monkeypatch.setattr(ms, "load_store", lambda _: pd.DataFrame({"date": ["2020-01-02", "2025-12-31"]}))
    monkeypatch.setattr(ms, "screened_symbol_union", lambda *args: ["AAA"])
    monkeypatch.setattr(R, "code_signature", lambda: "source-version")
    return job


def test_reference_symbols_come_from_the_expert_through_the_backtest_registry():
    import importlib
    module = importlib.import_module("ba2_experts.PullbackReversion")
    assert R.expert_class("PullbackReversion") is module.PullbackReversion
    assert module.PullbackReversion.REFERENCE_DAILY_SYMBOLS == (module.SPY_SYMBOL,) == ("SPY",)
    bt = jobs()["long_sma5"]["optimization_config"]["backtest"]
    assert R.reference_requirements(bt) == {"SPY": module.MAX_STALE_DAYS}
    for job in P.build_manifest()["jobs"]:  # no default family's expert declares any
        assert R.reference_requirements(job["optimization_config"]["backtest"]) == {}


def test_preflight_requires_spy_daily_for_pullback_rsi_only(monkeypatch, tmp_path):
    job = _screen_one(monkeypatch, tmp_path, jobs()["long_sma5"])
    _cache(tmp_path, ["AAA"])
    with pytest.raises(ValueError, match="SPY_1d.parquet"):
        R.preflight(job, tmp_path)
    _cache(tmp_path, ["SPY"], intervals=("1d",))  # the 5min file is NOT needed: SPY is not traded
    ready = R.preflight(job, tmp_path)
    bt = ready["optimization_config"]["backtest"]
    assert bt["enabled_instruments"] == ["AAA"]
    assert ready["preflight"]["reference_coverage"] == {"SPY": {"first": "2018-01-01", "last": "2025-12-31"}}
    D.verify_job(ready)

    # A default family on the same cache: no SPY requirement and no new evidence key.
    default = _screen_one(monkeypatch, tmp_path, next(
        j for j in P.build_manifest(families=["large_ds"])["jobs"] if j["variant"] == "control"))
    (tmp_path / "SPY_1d.parquet").unlink()
    ready = R.preflight(default, tmp_path)
    assert "reference_coverage" not in ready["preflight"]


@pytest.mark.parametrize("first,last,error", [
    ("2019-06-01", "2025-12-31", "must start by 2018-06-10"),  # 600-day warmup from 2020-01-01
    ("2018-01-01", "2025-12-20", "end by 2025-12-24"),         # MAX_STALE_DAYS (7), not 30
    ("2018-01-01", "2025-12-24", None),
])
def test_preflight_bounds_the_spy_history(monkeypatch, tmp_path, first, last, error):
    job = _screen_one(monkeypatch, tmp_path, jobs()["long_rsi"])
    _cache(tmp_path, ["AAA"])
    _cache(tmp_path, ["SPY"], intervals=("1d",), first=first, last=last)
    if error is None:
        assert R.preflight(job, tmp_path)["preflight"]["reference_coverage"]["SPY"]["last"] == last
    else:
        with pytest.raises(ValueError, match=f"Reference SPY 1d covers {first} to {last}.*{error}"):
            R.preflight(job, tmp_path)


# --------------------------------------------------------------------------- trial config and real engine
VALUES = {"model:rsi_period": 2, "model:entry_threshold": 15.0, "model:rsi_exit": 60.0,
          "cond:research_days:value": 10}


def _trial(variant, values=VALUES, **backtest):
    """The trial config the optimizer builds for one candidate of ``variant``, on a static
    one-symbol universe (no metric store is read)."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    job = jobs()[variant]
    bt = job["optimization_config"]["backtest"]
    del bt["screener_opt"]
    bt["experts"][0]["settings"]["instrument_selection_method"] = "static"
    bt.update(backtest_id=f"pullback-rsi-{variant}", enabled_instruments=["AAA"], **backtest)
    params = {gene: values[gene] for gene in space(job)}
    return _build_daily_trial_config(
        bt, decode_params(SimpleNamespace(**job["strategy"]), params), option_trade_records=False)


def test_enable_short_survives_the_trial_config_whitelist():
    from app.services.backtest.daily_backtest_handler import _run_facts
    from ba2_common.core.deploy_parity import forced_expert_settings
    for variant in jobs():
        config = _trial(variant)
        short = variant.startswith("short")
        assert config["enable_short"] is short
        [expert] = config["experts"]
        # The RM's short gate the handler forces onto the expert (daily_backtest_handler._build_experts).
        assert forced_expert_settings(_run_facts(config, expert["settings"]))["enable_sell"] is short
        [entry] = config["entry_rules"]
        assert entry["actions"][0]["action_type"] == ("sell" if short else "buy")


SIGNAL = date(2024, 3, 6)


def _sessions():
    from ba2_common.core.market_calendar import regular_sessions_ending_at
    return regular_sessions_ending_at(date(2024, 3, 28), 420)


def _series(sessions, closes):
    return [{"Date": d, "Open": round(c, 4), "High": round(c * 1.01, 4), "Low": round(c * 0.99, 4),
             "Close": round(c, 4), "Volume": 1_000_000} for d, c in zip(sessions, closes)]


def _wiggle(lo, hi, n):
    return np.linspace(lo, hi, n) + 0.2 * (-1.0) ** np.arange(n)


def _dip(after):
    """An uptrend with a three-day dip ending at bar ``i`` (RSI2 near 0, far above SMA200)."""
    def closes_for(n, i):
        closes = _wiggle(100.0, 200.0, n)
        closes[i - 2:i + 1] = closes[i - 3] - np.array([3.0, 6.0, 9.0])
        rest = np.arange(1, n - i)
        closes[i + 1:] = (closes[i - 3] + 2.0 + 0.4 * rest if after == "recovers"
                          else closes[i] * (1 - 0.02 * rest))
        return closes
    return closes_for


def _rally(n, i):
    """A downtrend with a three-day rally ending at bar ``i`` (RSI2 near 100, below SMA200)."""
    closes = _wiggle(200.0, 100.0, n)
    closes[i - 2:i + 1] = closes[i - 3] + np.array([3.0, 6.0, 9.0])
    closes[i + 1:] = closes[i] - 4.0 - 0.4 * np.arange(1, n - i)
    return closes


@pytest.fixture
def engine_caches():
    """The engine memoizes each symbol's series per process: no other test's AAA may leak in,
    and this module's fixture series must not leak out."""
    from app.services.backtest import price_source

    def clear():
        price_source.clear_ohlcv_memo()
        price_source.clear_worker_bar_cache()
    clear()
    yield
    clear()


def _run(monkeypatch, tmp_path, variants, closes_for):
    """Run each variant through the real daily engine on hermetic AAA/SPY daily bars."""
    import logging
    import ba2_common.config as bc
    from app.services.backtest.daily_backtest_handler import run_daily_backtest
    from tests.backtest.fixtures.e2e_support import hermetic_providers
    from tests.backtest.fixtures import hermetic_providers as fixtures

    sessions = _sessions()
    closes = closes_for(len(sessions), sessions.index(SIGNAL))
    spy = _wiggle(300.0, 400.0, len(sessions))
    monkeypatch.setattr(fixtures, "_PRICE_ROWS", {"AAA": _series(sessions, closes), "SPY": _series(sessions, spy)})
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(tmp_path))
    results = {}
    for variant in variants:
        config = _trial(variant, start_date="2024-03-01", end_date="2024-03-28", execution_interval="1d",
                        run_schedule_override=None, manage_schedule_override=None)
        before = logging.root.manager.disable
        try:
            logging.disable(logging.DEBUG)
            with hermetic_providers():
                results[variant] = run_daily_backtest(config)
        finally:
            logging.disable(before)
    return results


@pytest.mark.parametrize("closes_for,entering,idle", [
    (_dip("recovers"), "long_sma5", "short_sma5"), (_rally, "short_sma5", "long_sma5")])
def test_the_engine_fixtures_are_entry_signals(closes_for, entering, idle):
    """Outside the engine: at SIGNAL the expert's own decision is an entry for the job the
    fixture is built for, and not for the other. So the xfail below can only fail because the
    engine refused the short, never because the fixture gave no short signal."""
    from ba2_experts.PullbackReversion import completed_bars, pullback_signal
    sessions = _sessions()
    frame = pd.DataFrame(_series(sessions, closes_for(len(sessions), sessions.index(SIGNAL))))
    bars = completed_bars(frame, SIGNAL, "AAA")
    for variant in (entering, idle):
        settings = dict(jobs()[variant]["optimization_config"]["backtest"]["experts"][0]["settings"])
        settings.update(rsi_period=VALUES["model:rsi_period"], entry_threshold=VALUES["model:entry_threshold"])
        action = pullback_signal(bars, settings)["action"]
        assert (action == "entry") is (variant == entering), (variant, action)


@pytest.mark.parametrize("after", ["recovers", "keeps_falling"])
def test_long_job_trades_in_the_real_engine(monkeypatch, tmp_path, engine_caches, after):
    """Positive control for the harness: a dip in an uptrend opens a LONG. Recovering above
    SMA5, the bearish reverse-signal rule closes it at a profit; falling on, the -8% stop does."""
    results = _run(monkeypatch, tmp_path, ["long_sma5", "short_sma5"], _dip(after))
    assert results["short_sma5"]["total_trades"] == 0
    trades = results["long_sma5"]["trades"]
    assert trades and all(t["direction"] == "buy" and t["symbol"] == "AAA" for t in trades)
    first = trades[0]
    if after == "recovers":
        assert len(trades) == 1 and first["pnl"] > 0 and first["bars_held"] < 10
    else:
        # Stopped out, then re-entered after the cooldown while still oversold.
        assert first["exit_price"] == pytest.approx(first["entry_price"] * 0.92, rel=0.02)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Equity shorts cannot open: TradeActions.SellAction refuses a flat book ('No long position "
    "to sell'), whatever enable_short/enable_sell say, so runtime.refuse_unrunnable refuses short "
    "jobs. When this XPASSes, drop that refusal."))
def test_short_job_opens_a_short_in_the_real_engine(monkeypatch, tmp_path, engine_caches):
    results = _run(monkeypatch, tmp_path, ["short_sma5", "long_sma5"], _rally)
    assert results["long_sma5"]["total_trades"] == 0
    trades = results["short_sma5"]["trades"]
    assert len(trades) == 1, "the short job placed no trade"
    assert trades[0]["direction"] == "sell" and trades[0]["pnl"] > 0
