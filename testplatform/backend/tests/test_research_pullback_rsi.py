"""pullback_rsi: the opt-in PullbackReversion exploration family (plan 2026-09-24, Task A3).

The default campaign must stay byte-identical; the extension is selected by name only.
No broker, provider request, live database or grid run.
"""
from copy import deepcopy
from datetime import date
import itertools
import json
import os
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


# --------------------------------------------------------------------------- short jobs run
def test_the_short_jobs_open_shorts_and_nothing_refuses_them():
    """Equity shorts open now (plan 2026-09-24 S1-S4), so the old selection refusal is gone: a
    short job is an ordinary job. What makes it open shorts is its own config -- enable_short
    plus a `sell` entry -- which the long jobs do not carry."""
    for variant, job in jobs().items():
        bt = job["optimization_config"]["backtest"]
        sells = [a["action_type"] == "sell"
                 for rule in job["strategy"]["entry_rules"] for a in rule["actions"]]
        short = variant.startswith("short")
        assert bool(bt.get("enable_short")) is short and any(sells) is short, variant
    assert not hasattr(R, "refuse_unrunnable") and not hasattr(R, "opens_equity_short")
    assert not hasattr(D, "refuse_unrunnable")


def test_preflight_accepts_the_short_jobs(monkeypatch, tmp_path):
    _cache(tmp_path, ["AAA"])
    _cache(tmp_path, ["SPY"], intervals=("1d",))
    for variant in ("short_sma5", "short_spy"):
        ready = R.preflight(_screen_one(monkeypatch, tmp_path, jobs()[variant]), tmp_path)
        bt = ready["optimization_config"]["backtest"]
        assert bt["enable_short"] is True and bt["enabled_instruments"] == ["AAA"]
        D.verify_job(ready)


@pytest.mark.parametrize("mode", ["--preflight", "--run"])
def test_cli_runs_a_selection_with_short_jobs_in_order(monkeypatch, tmp_path, mode):
    started = []
    monkeypatch.setattr(D, "preflight", lambda job, cache_dir: started.append(job["variant"])
                        or {**job, "preflight": {"symbols": 1}})
    monkeypatch.setattr(D, "run_jobs", lambda selected, output, args: started.extend(
        j["variant"] for j in selected) or 0)
    argv = ["--families", "pullback_rsi", "--variants", "long_sma5", "short_spy", "short_sma5",
            mode, "--output-dir", str(tmp_path)]
    assert D.main(argv) == 0
    # The manifest's own order (the order jobs() lists them), every selected job, shorts included.
    assert started == ["long_sma5", "short_sma5", "short_spy"]


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


def test_reference_symbols_come_from_the_expert_class():
    import importlib
    from app.services.backtest.daily_backtest_handler import _SUPPORTED_EXPERTS
    module = importlib.import_module("ba2_experts.PullbackReversion")
    assert R.expert_class("PullbackReversion") is module.PullbackReversion
    assert module.PullbackReversion.REFERENCE_DAILY_SYMBOLS == (module.SPY_SYMBOL,) == ("SPY",)
    bt = jobs()["long_sma5"]["optimization_config"]["backtest"]
    assert R.reference_requirements(bt) == {"SPY": module.MAX_STALE_DAYS}
    for job in P.build_manifest()["jobs"] + list(jobs().values()):
        name = job["expert"]
        # expert_class's ba2_experts.<name> is the module the backtest handler runs.
        assert _SUPPORTED_EXPERTS[name] == "ba2_experts." + name
        if job["family"] != "pullback_rsi":  # no default family's expert declares any
            assert R.reference_requirements(job["optimization_config"]["backtest"]) == {}


def test_an_unknown_expert_class_is_a_clear_error():
    with pytest.raises(ValueError, match="Unknown expert class 'NoSuchExpert'"):
        R.expert_class("NoSuchExpert")
    with pytest.raises(ValueError, match="defines no class"):
        R.expert_class("settings_io")  # a real ba2_experts module, but no class of that name
    bt = deepcopy(jobs()["long_sma5"]["optimization_config"]["backtest"])
    bt["experts"][0]["class"] = "NoSuchExpert"
    with pytest.raises(ValueError, match="NoSuchExpert"):
        R.reference_requirements(bt)


_NO_BACKEND_SCRIPT = """
import sys
from pathlib import Path
import pandas as pd
root, cache = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root))
from tools.strategy_research.exploration import profiles as P, runtime as R
R.add_source_paths()
from ba2_providers.screener import metric_store as ms
ms.load_store = lambda _: pd.DataFrame({"date": ["2020-01-02", "2025-12-31"]})
ms.screened_symbol_union = lambda *args: ["AAA"]
R.code_signature = lambda: "source-version"
for symbol, intervals in (("AAA", ("1d", "5min")), ("SPY", ("1d",))):
    for interval in intervals:
        pd.DataFrame({"Date": pd.to_datetime(["2018-01-01", "2020-01-02", "2025-12-31"], utc=True)}
                     ).to_parquet(cache / f"{symbol}_{interval}.parquet")
for family, variant in (("large_ds", "control"), ("pullback_rsi", "long_sma5")):
    job = next(j for j in P.build_manifest(families=[family])["jobs"] if j["variant"] == variant)
    job["optimization_config"]["backtest"]["screener_opt"]["store"] = str(cache)
    R.reference_requirements(job["optimization_config"]["backtest"])
    R.preflight(job, cache)
loaded = sorted(m for m in sys.modules if m == "app" or m.startswith("app."))
print("BACKEND:", loaded)
"""


def test_preflight_never_imports_the_backend_database_module(tmp_path):
    """app.models.database binds its engine to DATABASE_URL at import. A preflight importing it
    (the old registry lookup did) before execute_ready applies --db-file sent every family's
    rows to the default test DB. A fresh interpreter, so this module's imports cannot mask it."""
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    done = subprocess.run([sys.executable, "-c", _NO_BACKEND_SCRIPT, str(ROOT), str(tmp_path)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-4000:]
    assert "BACKEND: []" in done.stdout, done.stdout[-2000:]


def test_run_child_binds_the_db_file_before_preflight(monkeypatch, tmp_path):
    from unittest import mock
    database = (tmp_path / "chosen.db").resolve()
    job = jobs()["long_sma5"]
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(job), encoding="utf-8")
    seen = {}

    def capture(job, cache_dir):
        seen["url"] = os.environ.get("DATABASE_URL")
        raise RuntimeError("stop after preflight")

    def forbidden(*args, **kwargs):
        pytest.fail("execute_ready ran")
    monkeypatch.setattr(D, "check_database", lambda path: Path(path).resolve())
    monkeypatch.setattr(D, "preflight", capture)
    monkeypatch.setattr(D, "execute_ready", forbidden)
    before = dict(os.environ)
    # run_child writes DATABASE_URL and CACHE_FOLDER into os.environ; patch.dict snapshots the
    # whole environment and restores it on exit, removing variables that were absent before.
    with mock.patch.dict(os.environ):
        os.environ.pop("DATABASE_URL", None)  # so the captured value can only be run_child's
        os.environ.pop("CACHE_FOLDER", None)
        assert D.main(["--job-file", str(job_file), "--db-file", str(database),
                       "--cache-dir", str(tmp_path / "FMPOHLCVProvider")]) == 1
    assert dict(os.environ) == before
    assert seen["url"] == R.database_url(database) == "sqlite:///" + database.as_posix()



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
    fixture is built for, and not for the other. So the engine tests below measure the engine,
    never a fixture that gave no signal."""
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


def test_short_job_opens_a_short_in_the_real_engine(monkeypatch, tmp_path, engine_caches):
    """The short_sma5 job on a rally in a downtrend opens ONE short in the real engine (it was a
    strict xfail until equity shorts could open): sold at the next open after the signal, with a
    stop ABOVE the entry, charged borrow (0.5%/yr default) on every session it is held, and
    covered at a profit by the bullish reverse-signal close. Its P&L is the fill arithmetic, and
    the account's own equity lands on it exactly."""
    import ba2_common.core.trade_cycle as trade_cycle
    from app.services.backtest import daily_engine
    from app.services.backtest.backtest_account import BORROW_SESSIONS_PER_YEAR, BacktestAccount
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection

    charges, snapshots, stops = [], {}, []
    real_accrue, real_snapshot = BacktestAccount.accrue_short_borrow, BacktestAccount.snapshot_equity

    def accrue(self, as_of):
        charge = real_accrue(self, as_of)
        if charge:
            charges.append((as_of.date(), charge))
        return charge

    def snapshot(self, as_of):
        snap = real_snapshot(self, as_of)
        snapshots.setdefault(id(self), []).append(snap["net_liquidating_value"])  # per run
        return snap

    def record(order, safeguard):
        txn = get_instance(Transaction, order.transaction_id)
        stops.append((order.side, txn.side, safeguard, txn.stop_loss))
        return trade_cycle.record_max_loss_stop(order, safeguard)

    monkeypatch.setattr(BacktestAccount, "accrue_short_borrow", accrue)
    monkeypatch.setattr(BacktestAccount, "snapshot_equity", snapshot)
    monkeypatch.setattr(daily_engine, "record_max_loss_stop", record)
    results = _run(monkeypatch, tmp_path, ["short_sma5", "long_sma5"], _rally)
    assert results["long_sma5"]["total_trades"] == 0
    result = results["short_sma5"]
    [trade] = result["trades"]
    assert trade["direction"] == "sell" and trade["symbol"] == "AAA"
    assert trade["exit_reason"] == "exit", "covered by the bullish reverse-signal close"

    # The fills: next-bar open, crossing half the 5 bps spread (a short sells at the bid and
    # buys back at the ask). The engine stamps a next_bar_open fill with its decision bar.
    sessions = _sessions()
    closes = dict(zip(sessions, (round(c, 4) for c in _rally(len(sessions), sessions.index(SIGNAL)))))
    opened = date.fromisoformat(trade["entry_time"][:10])
    covered = date.fromisoformat(trade["exit_time"][:10])
    next_open = lambda d: closes[sessions[sessions.index(d) + 1]]  # the fixture's open == close
    half = 5.0 / 2 / 10_000
    assert trade["entry_price"] == pytest.approx(next_open(opened) * (1 - half))
    assert trade["exit_price"] == pytest.approx(next_open(covered) * (1 + half))
    assert trade["exit_price"] < trade["entry_price"]
    commission = 0.1
    assert trade["pnl"] == pytest.approx(
        (trade["entry_price"] - trade["exit_price"]) * trade["size"] - 2 * commission)
    assert trade["pnl"] > 0

    # Its protection: a SELL entry on a SELL-side transaction, stopped ABOVE the entry.
    [(order_side, txn_side, safeguard, stop_loss)] = stops
    assert order_side == txn_side == OrderDirection.SELL
    assert safeguard > trade["entry_price"] and stop_loss > trade["entry_price"]
    # The job's -8% is taken off the decision bar's close (the pre-fill reference of a market
    # entry). The backtest keeps it there when the next open gaps; live re-bases a pending stop
    # to the actual fill (TradeManager.rebase_price_to_fill), for longs and shorts alike.
    assert stop_loss == pytest.approx(closes[opened] * 1.08)

    # Borrow (S4): charged on every session the short is on the book -- its fill bar up to, not
    # including, the bar that covers it -- on |qty| x that close x rate / 252.
    rate = result["short_borrow_rate_pa"]
    assert rate == 0.005
    held = [d for d in sessions if opened <= d < covered]
    assert [d for d, _ in charges] == held and held
    for d, charge in charges:
        assert charge == pytest.approx(trade["size"] * closes[d] * rate / BORROW_SESSIONS_PER_YEAR)
    borrow = sum(c for _, c in charges)
    assert result["short_borrow_cost"] == pytest.approx(round(borrow, 2)) and result["short_borrow_cost"] > 0

    # The account's own equity after the cover: the covered short's P&L less its borrow, exactly.
    # (results' equity_curve is the fixed-notional SCORING curve, restated; the recorded
    # snapshots are the account itself.)
    short_run = next(iter(snapshots.values()))  # short_sma5 runs first
    assert short_run[-1] == pytest.approx(10_000.0 + trade["pnl"] - borrow)
