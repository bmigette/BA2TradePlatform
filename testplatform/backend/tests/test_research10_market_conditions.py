"""Condition-aware follow-up driver: isolated caches, no jobs/brokers/provider requests."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.strategy_research import profiles as P, runtime as R, market_conditions as MC
from tools.strategy_research import run_goal2020_followups as D
from app.services.strategy_param_space import collect_param_space, decode_params
from ba2_common.core.market_conditions import PROFILES, STATUS_VALID, STATUS_MISSING_SESSION, STATUS_INSUFFICIENT_HISTORY
from ba2_common.core.market_condition_store import MarketConditionStore
from ba2_common.core.market_condition_rules import iter_market_condition_leaves

PINS = {"ohlcv-v1": "a" * 64, "ta-structure-v1": "b" * 64}


def campaign(profile="ohlcv-v1", **kwargs):
    pins = ",".join(f"{p}={PINS[p]}" for p in profile.split(","))
    return P.build_manifest(search="genetic", market_condition_profile=profile,
                            market_condition_manifest=pins, **kwargs)


def space(job):
    return collect_param_space(SimpleNamespace(**job["strategy"]), job["optimization_config"]["expert_params"])


def test_disabled_default_is_the_exact_original_manifest():
    # Frozen before changing profiles.py, rather than comparing two new implementations.
    assert P.fingerprint(P.build_manifest()) == "798c8787f90a6e1215f964edc453d3c58a29138fec845cefab1f7d2873bb2fa9"


@pytest.mark.parametrize("profile,genes", [("ohlcv-v1", 6), ("ta-structure-v1", 9),
                                           ("ohlcv-v1,ta-structure-v1", 15)])
def test_all_35_jobs_gate_every_opening_tier_and_all_off_decodes_identically(profile, genes):
    base = P.build_manifest(search="genetic")["jobs"]
    gated = campaign(profile)["jobs"]
    assert len(gated) == 35
    for original, job in zip(base, gated):
        D.verify_job(job)
        assert original["name"] != job["name"]
        assert original["strategy"]["exit_rules"] == job["strategy"]["exit_rules"]
        assert not list(iter_market_condition_leaves(job["strategy"]["exit_rules"]))
        extra = {g: v for g, v in space(job).items() if "-market-" in g}
        assert len(extra) == genes * len(job["strategy"]["entry_rules"])
        assert job["optimization_type"] == "genetic" and not job["fixed"]
        off = {g: "off" for g in extra if g.endswith(":mode")}
        decoded = decode_params(SimpleNamespace(**job["strategy"]), off)
        expected = decode_params(SimpleNamespace(**original["strategy"]), {})
        assert decoded["entry_rules"] == expected["entry_rules"], job["family"]
        assert decoded["exit_rules"] == expected["exit_rules"], job["family"]
        bt, old = (j["optimization_config"]["backtest"] for j in (job, original))
        for key in ("account_settings", "initial_capital", "run_schedule_override", "manage_schedule_override"):
            assert bt[key] == old[key]
        assert bt["market_condition"]["gene_count"] == len(extra)
        assert bt["experts"][0]["settings"]["market_condition_profile"] == profile
        # Deployment conversion must recognize every new field, not just retain template JSON.
        from ba2_common.core.rule_builders import triggers_from_condition_tree
        modes = {g: ("bull" if "-structure:" in g else "above")
                 for g in extra if g.endswith(":mode")}
        concrete = decode_params(SimpleNamespace(**job["strategy"]), modes)
        for r in concrete["entry_rules"]:
            events = {t["event_type"] for t in P.walk(triggers_from_condition_tree(r["conditions"]))
                      if "event_type" in t}
            assert {leaf["field"] for _, leaf in iter_market_condition_leaves(r)} <= events


def test_explicit_all_off_keeps_original_trees_and_parameter_space():
    for old, off in zip(P.build_manifest(search="genetic")["jobs"],
                        campaign(market_condition_mode="all-off")["jobs"]):
        assert old["strategy"] == off["strategy"]
        assert space(old) == space(off)
        assert off["optimization_type"] == old["optimization_type"]
        assert off["optimization_config"]["backtest"]["market_condition"]["gene_count"] == 0
        assert off["name"] != old["name"]


def test_profile_order_is_canonical_and_digest_or_mode_changes_identity():
    a = campaign("ohlcv-v1,ta-structure-v1")
    assert a == campaign("ta-structure-v1,ohlcv-v1")
    assert campaign() != campaign(market_condition_mode="all-off")
    b = P.build_manifest(search="genetic", market_condition_profile="ohlcv-v1",
                         market_condition_manifest="c" * 64)
    assert b["jobs"][0]["name"] != campaign()["jobs"][0]["name"]


@pytest.mark.parametrize("profile,pin,mode", [
    ("none", "a" * 64, "search"), ("none", None, "all-off"),
    ("ohlcv-v1", None, "search"), ("typo", "a" * 64, "search"),
    ("ohlcv-v1", "bad", "search"), ("ohlcv-v1,ohlcv-v1", "a" * 64, "search"),
    ("ohlcv-v1,ta-structure-v1", "a" * 64, "search"),
    ("ohlcv-v1", "ta-structure-v1=" + "a" * 64, "search"),
    ("ohlcv-v1", "ohlcv-v1=" + "a" * 64 + ",ohlcv-v1=" + "b" * 64, "search"),
])
def test_malformed_profile_and_pin_combinations_fail_before_cache_access(profile, pin, mode):
    with pytest.raises(ValueError):
        MC.selection(profile, pin, mode)


def test_gated_exhaustive_grid_is_refused():
    with pytest.raises(ValueError, match="--search genetic"):
        P.build_manifest(market_condition_profile="ohlcv-v1", market_condition_manifest=PINS["ohlcv-v1"])


def test_or_tree_is_wrapped_and_management_actions_are_not_changed():
    job = {"family": "test", "strategy": {"entry_rules": [
        {"conditions": {"type": "OR", "conditions": [
            {"id": "bull", "field": "bullish", "op": "is_true"}]}, "actions": [{"action_type": "buy"}]}],
        "exit_rules": []}}
    original = deepcopy(job)
    bt = {"experts": [{"settings": {}}]}
    MC.attach(job, bt, ("ohlcv-v1",), {"ohlcv-v1": PINS["ohlcv-v1"]}, "search")
    tree = job["strategy"]["entry_rules"][0]["conditions"]
    assert tree["type"] == "AND"
    assert tree["conditions"][0] == original["strategy"]["entry_rules"][0]["conditions"]
    assert job["strategy"]["entry_rules"][0]["actions"] == original["strategy"]["entry_rules"][0]["actions"]


#: The rows the job's 2024-03-26..2024-03-28 BARS read. BT/live parity (plan 2026-09-22 A3):
#: bar D reads D itself (the live decision it stands for is labelled the NEXT session). Before
#: the fix this was 03-25..03-27, the session BEFORE each bar.
SESSIONS = [date(2024, 3, 26), date(2024, 3, 27), date(2024, 3, 28)]


def publish(root, profile="ohlcv-v1", sessions=SESSIONS, status=None, source="fmp-daily-split-adjusted-v1",
            symbols=("AAA",)):
    store = MarketConditionStore(root)
    spec = PROFILES[profile]
    rows = []
    for day in sessions:
        st = status(day) if status else STATUS_VALID
        rows.append({"session": day, "values": [0.2 if st == STATUS_VALID else None] * len(spec.fields),
                     "status": [st] * len(spec.fields), "reasons": ["fixture"] * len(spec.fields),
                     "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "",
                     "raw_row_lo": 0, "raw_row_hi": 0})
    objects = []
    for symbol in symbols:
        for month in sorted({r["session"].strftime("%Y-%m") for r in rows}):
            obj, _ = store.write_feature_object(spec, symbol,
                [r for r in rows if r["session"].strftime("%Y-%m") == month])
            objects.append(obj)
    manifest = store.make_manifest(spec, source_profile=source, timing_policy="prior_session_v1",
        objects=objects, raw_objects=[], coverage={s: {"rows": len(rows)} for s in symbols}, universe=symbols,
        sessions=sessions, window_start=min(sessions), window_end=max(sessions))
    return store, store.write_manifest(manifest), obj


def cache_job(tmp_path, profile="ohlcv-v1", **publish_kwargs):
    root = tmp_path / "cache"
    price_dir = root / "FMPOHLCVProvider"
    price_dir.mkdir(parents=True)
    pd.DataFrame({"Date": pd.bdate_range("2022-01-01", "2024-03-28")}).to_parquet(price_dir / "AAA_1d.parquet")
    pd.DataFrame({"Date": pd.bdate_range("2024-03-01", "2024-03-28")}).to_parquet(price_dir / "AAA_5min.parquet")
    store, digest, obj = publish(root, profile=profile, **publish_kwargs)
    job = P.build_manifest(families=["etf_trend"], start="2024-03-26", end="2024-03-28",
                          etf_symbols=["AAA"], search="genetic", market_condition_profile=profile,
                          market_condition_manifest=digest)["jobs"][0]
    return job, root, store, obj


def test_preflight_verifies_and_reports_the_entire_window(tmp_path, monkeypatch):
    job, root, _, _ = cache_job(tmp_path)
    original = deepcopy(job)
    monkeypatch.setattr(R, "code_signature", lambda: "test-source")
    ready = R.preflight(job, root / "FMPOHLCVProvider")
    report = ready["preflight"]["market_conditions"]["ohlcv-v1"]
    assert report["sessions"] == 3 and report["first_session"] == "2024-03-26"
    assert report["last_session"] == "2024-03-28"
    assert report["status_counts"]["underlying_adx_14"][STATUS_VALID] == 3
    assert job == original
    assert "preflight" not in job
    D.verify_job(ready)
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    decoded = decode_params(SimpleNamespace(**ready["strategy"]),
                            {g: "off" for g in space(ready) if g.endswith(":mode")})
    trial = _build_daily_trial_config(ready["optimization_config"]["backtest"], decoded, option_trade_records=False)
    assert trial["market_condition_manifests"] == job["optimization_config"]["backtest"]["market_condition_manifests"]
    assert trial["experts"][0]["settings"]["market_condition_profile"] == "ohlcv-v1"


@pytest.mark.parametrize("sessions", [[date(2023, 3, 27)], [SESSIONS[0], SESSIONS[-1]]])
def test_wrong_window_and_internal_holes_are_refused(tmp_path, sessions):
    job, root, _, _ = cache_job(tmp_path, sessions=sessions)
    with pytest.raises(ValueError, match="missing required bar-session"):
        MC.preflight(job["optimization_config"]["backtest"], root)


def test_negative_missing_row_after_source_start_is_refused(tmp_path):
    job, root, _, _ = cache_job(tmp_path, status=lambda d: STATUS_MISSING_SESSION if d == SESSIONS[1] else STATUS_VALID)
    with pytest.raises(ValueError, match="unexplained invalid/missing"):
        MC.preflight(job["optimization_config"]["backtest"], root)


def test_source_identity_and_corruption_are_refused(tmp_path):
    job, root, _, _ = cache_job(tmp_path, source="different-provider")
    with pytest.raises(ValueError, match="source/timing/calendar"):
        MC.preflight(job["optimization_config"]["backtest"], root)


def test_corrupt_object_is_not_mapped(tmp_path):
    job, root, store, obj = cache_job(tmp_path)
    path = Path(store.abspath(obj.path))
    data = bytearray(path.read_bytes()); data[-1] ^= 1; path.write_bytes(data)
    with pytest.raises(ValueError, match="verification failed"):
        MC.preflight(job["optimization_config"]["backtest"], root)


def test_initial_history_is_reported_but_zero_usable_symbol_is_refused(tmp_path):
    job, root, _, _ = cache_job(tmp_path, status=lambda d: STATUS_INSUFFICIENT_HISTORY)
    pd.DataFrame({"Date": pd.bdate_range("2024-03-01", "2024-03-28")}).to_parquet(root / "FMPOHLCVProvider/AAA_1d.parquet")
    with pytest.raises(ValueError, match="no usable feature"):
        MC.preflight(job["optimization_config"]["backtest"], root)


def test_explicit_prior_listing_row_is_allowed_when_other_sessions_are_usable(tmp_path):
    # The source starts one session AFTER the first required row (was 03-26 vs row 03-25; both
    # moved by one with the parity clock), so that row's missing_session is a listing boundary.
    job, root, _, _ = cache_job(tmp_path, status=lambda d: STATUS_MISSING_SESSION if d == SESSIONS[0] else STATUS_VALID)
    pd.DataFrame({"Date": pd.bdate_range("2024-03-27", "2024-03-28")}).to_parquet(root / "FMPOHLCVProvider/AAA_1d.parquet")
    report = MC.preflight(job["optimization_config"]["backtest"], root)
    assert report["ohlcv-v1"]["initial_history_sessions"]["AAA"] == 3


def test_export_universe_and_profile_flags_are_available(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "resolve_universe", lambda bt: (["AAA", "BBB"], []))
    output = tmp_path / "universe.txt"
    assert D.main(["--families", "etf_trend", "--export-universe", str(output),
                   "--output-dir", str(tmp_path / "preview")]) == 0
    assert output.read_text().splitlines() == ["AAA", "BBB"]
    for flag in ("--market-condition-profile", "--market-condition-manifest", "--market-condition-mode"):
        assert flag in D.parser().format_help()


def test_condition_driver_dry_run_never_opens_database_or_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "preflight", lambda *a: pytest.fail("cache read in dry-run"))
    monkeypatch.setattr(D, "check_database", lambda *a: pytest.fail("database in dry-run"))
    assert D.main(["--families", "etf_trend", "--search", "genetic", "--dry-run",
                   "--market-condition-profile", "ohlcv-v1", "--market-condition-manifest", PINS["ohlcv-v1"],
                   "--output-dir", str(tmp_path)]) == 0


def test_real_equity_engine_all_off_parity_and_active_entry_veto(tmp_path, monkeypatch):
    """Actual ETF fills/quantities/P&L match; an inverted ADX condition vetoes those entries."""
    import logging
    import ba2_common.config as bc
    from app.services.backtest.daily_backtest_handler import run_daily_backtest
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from tests.backtest.fixtures.e2e_support import hermetic_providers
    from tests.backtest.fixtures import hermetic_providers as fixtures
    from ba2_common.core.market_calendar import regular_sessions_ending_at

    monkeypatch.setattr(fixtures, "_BASE_START", date(2023, 1, 2))
    monkeypatch.setattr(fixtures, "_N_BARS", 400)
    monkeypatch.setattr(fixtures, "_PRICE_ROWS", {s: fixtures._build_price_rows(s) for s in ("AAPL", "MSFT")})
    _, digest, _ = publish(tmp_path, sessions=regular_sessions_ending_at(date(2024, 2, 16), 150),
                           symbols=("AAPL", "MSFT"))
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(tmp_path))
    results = {}
    for arm in ("none", "all-off", "pass", "fail"):
        extra = {} if arm == "none" else {
            "market_condition_profile": "ohlcv-v1", "market_condition_manifest": digest,
            "market_condition_mode": "all-off" if arm == "all-off" else "search"}
        job = P.build_manifest(families=["etf_trend"], search="genetic", **extra)["jobs"][0]
        bt = job["optimization_config"]["backtest"]
        bt.update(backtest_id="equity-market-parity", start_date="2024-02-01", end_date="2024-02-16",
                  warmup_days=90, enabled_instruments=["AAPL", "MSFT"], execution_interval="1d",
                  run_schedule_override=None, manage_schedule_override=None)
        bt["experts"][0]["settings"].update(universe_symbols=["AAPL", "MSFT"], momentum_bars=5,
                                            trend_bars=5, top_n=1)
        params = {g: "off" for g in space(job) if g.endswith(":mode")}
        if arm in ("pass", "fail"):
            adx = next(g for g in params if "-market-adx:" in g)
            params[adx] = "below" if arm == "pass" else "above"
            params[adx.removesuffix("mode") + "value"] = 25.0
        config = _build_daily_trial_config(bt, decode_params(SimpleNamespace(**job["strategy"]), params), option_trade_records=False)
        before = logging.root.manager.disable
        try:
            logging.disable(logging.INFO)
            with hermetic_providers():
                results[arm] = run_daily_backtest(config)
        finally:
            logging.disable(before)
    assert results["none"]["total_trades"] >= 1
    for arm in ("all-off", "pass"):
        for key in ("final_equity", "total_trades", "max_drawdown", "total_return", "equity_curve"):
            assert results[arm][key] == results["none"][key], (arm, key)
        def economic_trades(result):
            return [{k: v for k, v in trade.items() if k != "entry_state"} for trade in result["trades"]]
        assert economic_trades(results[arm]) == economic_trades(results["none"])
    assert results["fail"]["total_trades"] == 0
