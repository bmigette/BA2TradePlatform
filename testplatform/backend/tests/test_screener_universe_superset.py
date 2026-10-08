"""Screener static universe = true superset: launcher/driver identity, trial prune, run-time guard, re-runs.

The 2026-10-07 universe mismatch: the frozen ``enabled_instruments`` of a screener job held only the 50
largest names of each weekly band (top-50 cut AFTER the filters), so a tighter genome's picks outside it
were silently untradable. See ``ba2_providers.screener.universe_superset``.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services import strategy_optimization_handler as H
from app.services.backtest import daily_engine as DE
from app.services.backtest import rerun_handler as RH
from ba2_providers.screener import metric_store as ms
from ba2_providers.screener import universe_superset as us

SCANS = ["2023-03-04", "2023-03-11", "2023-03-18", "2023-03-25"]
N = 60
GENOME_GENES = {"market_cap_min": 2e9, "relative_volume_min": 1.5, "price_drop_pct": 13.0,
                "price_drop_days": 22, "max_stocks": 20}
_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))


@pytest.fixture()
def store(tmp_path):
    """60 names in the 2-10B band, cap descending; only S55 (cap rank 56) passes an rvol>=1.5 gate."""
    rows = []
    for d in SCANS:
        for i in range(N):
            rows.append({"date": d, "symbol": f"S{i:02d}", "market_cap": 9.9e9 - i * 1e8, "price": 20.0,
                         "close": 20.0, "volume": 2e6, "sector": "T",
                         "relative_volume": 2.4 if i == 55 else 0.5,
                         "price_drop_pct": 20.0, "price_drop_pct_22": 20.0})
    path = str(tmp_path / "ms")
    ms.write_partitions(path, pd.DataFrame(rows))
    ms.clear_store_memo()
    return path


ALL = [f"S{i:02d}" for i in range(N)]
OLD_LIST = ALL[:50]            # what the top-50-by-cap rule froze


def _backtest_cfg(store, instruments, rule):
    cfg = {
        "backtest_id": 7, "start_date": "2023-03-06", "end_date": "2023-03-24",
        "enabled_instruments": list(instruments), "execution_interval": "5min",
        "experts": [{"class": "FMPEarningsDrift", "settings": {}}],
        "initial_capital": 100000.0, "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30, "seed": 42,
        "screener_opt": {"store": store, "base_settings": {"market_cap_max": 1e10}, "cadence_days": 7,
                         "apply_to_expert_settings": False},
    }
    if rule:
        cfg["screener_universe_rule"] = rule
    return cfg


def _decoded():
    return {"tp": 8.0, "sl": 3.0, "expert_overrides": {}, "buy_tree": None, "sell_tree": None,
            "exit_rules": [], "entry_rules": [],
            "screener_overrides": {f"screener_{k}": v for k, v in GENOME_GENES.items()}}


def _trial(store, instruments, rule):
    cfg = _backtest_cfg(store, instruments, rule)
    return H._build_daily_trial_config(cfg, _decoded(), H._build_hoisted_state(cfg), option_trade_records=False)


# ------------------------------------------------------------------------------------- engine harness
class _Price:
    is_intraday = False

    def bar_at(self, sym, as_of):
        return {"close": 1.0}


def _engine(config):
    """A DailyBacktestEngine with only what ``_bar_universes`` and the guard touch (no account, no DB)."""
    e = DE.DailyBacktestEngine.__new__(DE.DailyBacktestEngine)
    e.config = config
    e.price = _Price()
    e._screener_runtime = config["screener_runtime"]
    e._screened_cache = {}
    mode = config["screener_universe_guard"]
    e._su_mode = mode
    e._su_loaded = frozenset(config["enabled_instruments"]) if mode else frozenset()
    e._su_static = (frozenset(config["screener_static_universe"])
                    if mode and "screener_static_universe" in config else e._su_loaded)
    e._su = {"decisions": 0, "gate_selected": 0, "outside_static_universe": 0, "outside_pruned_only": 0,
             "first_examples": [], "_last_allowed": None}
    e._screen_gate = None
    return e


def _walk(engine):
    """Every weekday decision of the window, like the engine's lazy per-bar universe resolution."""
    out, d = [], datetime(2023, 3, 6, 9, 30)
    while d <= datetime(2023, 3, 24, 9, 30):
        if d.weekday() < 5:
            out.append(engine._bar_universes(d)[1])
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------------------- trial prune + guard
def test_trial_on_the_superset_universe_trades_only_what_its_gate_selects(store):
    t = _trial(store, ALL, us.RULE_ID)
    assert t["enabled_instruments"] == ["S55"]                  # pruned to this genome's own selections
    assert t["screener_universe_guard"] == "refuse"
    eng = _engine(t)
    assert all(u == ["S55"] for u in _walk(eng))
    eng.refuse_if_screener_universe_outside()                   # nothing outside: no refusal
    rec = eng.screener_universe_record()
    assert rec["outside_static_universe"] == 0 and rec["gate_selected"] == rec["decisions"] > 0


def test_prune_is_result_neutral_the_entry_universe_equals_the_unpruned_run(store):
    """The prune only removes symbols the gate never selects: the per-decision ENTRY universe is
    identical with the full static universe preloaded and with the pruned one."""
    pruned = _engine(_trial(store, ALL, us.RULE_ID))
    full_cfg = dict(_trial(store, ALL, us.RULE_ID))
    full_cfg["enabled_instruments"] = list(ALL)                 # no prune: the whole static universe loaded
    full = _engine(full_cfg)
    assert _walk(pruned) == _walk(full)
    assert len(full_cfg["enabled_instruments"]) == N and len(pruned.config["enabled_instruments"]) == 1


def test_a_pick_outside_the_static_universe_refuses_the_run(store):
    """The OLD frozen list (top 50 by cap) does not contain S55: the gate selects it and it could never be
    traded. Under the superset rule that is a job-fatal refusal, with the counts recorded."""
    t = _trial(store, OLD_LIST, us.RULE_ID)
    assert t["enabled_instruments"] == []                       # the silent drop: S55 not in the list
    eng = _engine(t)
    _walk(eng)
    rec = eng.screener_universe_record()
    assert rec["gate_selected"] > 0 and rec["outside_static_universe"] == rec["gate_selected"]
    assert rec["first_examples"][0]["symbol"] == "S55" and len(rec["first_examples"]) == 1
    with pytest.raises(DE.ScreenerUniverseRefusal, match="outside the job's STATIC universe") as ei:
        eng.refuse_if_screener_universe_outside()
    assert ei.value.outside == rec["outside_static_universe"] and ei.value.gate_selected == rec["gate_selected"]
    assert H.job_fatal(ei.value) and "ScreenerUniverseRefusal" in H.JOB_FATAL_ERROR_TYPES


def test_a_legacy_frozen_list_only_warns_and_counts(store, monkeypatch):
    warnings = []
    monkeypatch.setattr(DE.logger, "warning", lambda msg, *a, **k: warnings.append(msg))
    t = _trial(store, OLD_LIST, rule=None)                      # a stored block from before the rule
    assert t["screener_universe_guard"] == "warn"
    eng = _engine(t)
    _walk(eng)
    eng.refuse_if_screener_universe_outside()                   # no raise
    assert eng.screener_universe_record()["mode"] == "warn"
    assert eng.screener_universe_record()["outside_static_universe"] > 0
    assert any("SCREENER UNIVERSE (legacy frozen list" in w and "S55" in w for w in warnings)


def test_each_decision_is_counted_once_even_with_several_experts(store):
    eng = _engine(_trial(store, ALL, us.RULE_ID))
    at = datetime(2023, 3, 13, 9, 30)
    eng._bar_universes(at), eng._bar_universes(at), eng._bar_universes(at)
    assert eng.screener_universe_record()["decisions"] == 1


def test_no_guard_without_a_screener_universe_job(store):
    cfg = _trial(store, ALL, us.RULE_ID)
    cfg["screener_universe_guard"] = None
    eng = _engine(cfg)
    _walk(eng)
    assert eng.screener_universe_record() is None
    eng.refuse_if_screener_universe_outside()


def test_guard_mode_is_validated_by_the_engine_constructor():
    src = open(DE.__file__, encoding="utf-8").read()
    assert "screener_universe_guard must be 'refuse', 'warn' or None" in src


def test_gate_only_and_bypass_runs_carry_no_guard(store):
    cfg = _backtest_cfg(store, ALL, us.RULE_ID)
    cfg["screener_opt"]["gate_only"] = True
    t = H._build_daily_trial_config(cfg, _decoded(), H._build_hoisted_state(cfg), option_trade_records=False)
    assert t["screener_universe_guard"] is None and t["enabled_instruments"] == ALL


def test_a_failing_bound_computation_propagates_instead_of_loading_the_band(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store unreadable")
    monkeypatch.setattr(ms, "screened_symbol_union_visible", boom)
    with pytest.raises(RuntimeError, match="store unreadable"):
        _trial(store, ALL, us.RULE_ID)


# ------------------------------------------------------------------------------------- job identity
_GA = {"populationSize": 40, "generations": 8}
_SPACE = {"a": {"min": 0, "max": 1, "step": 1, "type": "int"}}


def test_the_universe_rule_is_part_of_the_checkpoint_identity():
    legacy = H.checkpoint_fingerprint(_SPACE, _GA)
    assert H.checkpoint_fingerprint(_SPACE, _GA, None, None, None) == legacy      # no rule: byte-identical
    new = H.checkpoint_fingerprint(_SPACE, _GA, None, None, us.RULE_ID)
    assert new != legacy                                                           # never resumed across rules
    assert H.checkpoint_fingerprint(_SPACE, _GA, None, None, "superset-v2") not in (legacy, new)


def test_driver_job_names_carry_the_rule_token_and_stay_distinct_from_legacy_names():
    sys.path.insert(0, os.path.join(_ROOT, "tools"))
    import matrix_flags as mf
    # classic screener jobs also carry the criteria token (live-simulation gate); the bypass FactorRanker does not
    assert mf.with_universe_rule_name("scr-mid-X-S1") == "scr-mid-X-S1-sup1-lds2"
    assert mf.with_universe_rule_name("scr-mid-X-S1-sup1-lds2") == "scr-mid-X-S1-sup1-lds2"      # idempotent
    assert mf.with_universe_rule_name("scr-mid-FR", simulated=False) == "scr-mid-FR-sup1"
    assert mf.UNIVERSE_RULE_NAME_TOKEN == "-sup1" and us.RULE_ID == "superset-v1"
    assert mf.with_universe_rule_name("n") != "n"                                      # a completed OLD name never matches


def test_driver_dry_run_prints_the_static_universe_size_per_job(monkeypatch):
    sys.path.insert(0, os.path.join(_ROOT, "tools"))
    import matrix_flags as mf
    monkeypatch.setattr(us, "preview_static_universe",
                        lambda *a, **k: {"size": 2135, "uncached": ["FITB-PA"]})
    note = mf.screener_dry_run_universe_note("S", "mid", "2020-01-01", "2025-12-31", "5min")
    assert "static universe 2135 symbols" in note and "FITB-PA" in note and "REFUSES" in note
    monkeypatch.setattr(us, "preview_static_universe", lambda *a, **k: (_ for _ in ()).throw(OSError("no store")))
    assert "UNAVAILABLE (OSError: no store)" in mf.screener_dry_run_universe_note("S2", "mid", "a", "b", "5min")


def test_the_driver_forwards_exclude_uncached_and_digests_it():
    src = open(os.path.join(_ROOT, "tools", "run_screener_capband_matrix.py"), encoding="utf-8").read()
    assert '"--screener-exclude-uncached"' in src and "with_universe_rule_name(name, simulated=strat is not None)" in src
    assert "screener_dry_run_universe_note(args.store, band, job_start, args.end, args.interval)" in src


# ---------------------------------------------------------------------------- launcher + stored re-runs
def test_launcher_screener_block_builds_the_superset_and_stamps_the_rule():
    import ba2test_launcher as L
    assert L._SCREENER_OPT is us.SCREENER_OPT and L._SCREENER_CAP_BANDS is us.SCREENER_CAP_BANDS
    src = open(L.__file__, encoding="utf-8").read()
    assert "_us.static_universe(" in src and 'backtest_block["screener_universe_rule"] = _us.RULE_ID' in src
    assert "screened_symbol_union(_store_df" not in src          # the old cap-ranked rule is gone


def _block(store, instruments, rule=None):
    b = {"start_date": "2023-03-06", "end_date": "2023-03-24", "execution_interval": "5min",
         "enabled_instruments": list(instruments),
         "screener_opt": {"store": store, "base_settings": {"market_cap_max": 1e10}, "cadence_days": 7}}
    if rule:
        b["screener_universe_rule"] = rule
    return b


def _ranges():
    opt, _ = us.apply_cap_band(us.SCREENER_OPT, {}, "mid")
    return {f"screener:{g}": v for g, v in opt.items()}


def test_recompute_universe_replaces_the_frozen_list_and_records_uncached(store, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    (cache / "FMPOHLCVProvider").mkdir(parents=True)
    for s in ALL:
        if s != "S10":                                           # one name has no bars
            for iv in ("5min", "1d"):
                (cache / "FMPOHLCVProvider" / f"{s}_{iv}.parquet").write_bytes(b"")
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(cache))
    blk = _block(store, OLD_LIST)
    out = RH.recompute_static_universe(blk, _ranges(), row_id=1)
    assert blk["enabled_instruments"] == OLD_LIST and "screener_universe_rule" not in blk   # input untouched
    assert out["screener_universe_rule"] == us.RULE_ID
    assert out["enabled_instruments"] == [s for s in ALL if s != "S10"]
    assert out["excluded_instruments"] == ["S10"]
    with pytest.raises(us.ScreenerUniverseError, match="no screener gene ranges"):
        RH.recompute_static_universe(blk, {}, row_id=1)
    with pytest.raises(us.ScreenerUniverseError, match="no declared role"):
        RH.recompute_static_universe(blk, {**_ranges(), "screener:screener_mystery": {"min": 0, "max": 1}}, row_id=1)


def test_a_stored_row_without_recompute_keeps_its_list_and_warns_with_the_count(store, caplog):
    blk = _block(store, OLD_LIST)
    hoisted = H._build_hoisted_state(blk)
    with caplog.at_level("WARNING", logger=RH.logger.name):     # rerun_handler uses a stdlib logger
        note = RH.legacy_universe_note(blk, hoisted, _decoded(), row_id=1088)
    assert blk["enabled_instruments"] == OLD_LIST
    assert note["static_universe_size"] == 50 and note["gate_selected"] == 4 and note["outside_static_universe"] == 4
    assert note["first_examples"][0]["symbol"] == "S55"
    assert any("LEGACY static universe (50 symbols" in r.message and "4 of 4" in r.message
               for r in caplog.records)
    # the same genome against the recomputed universe: nothing outside
    blk2 = _block(store, ALL, us.RULE_ID)
    assert RH.legacy_universe_note(blk2, H._build_hoisted_state(blk2), _decoded())["outside_static_universe"] == 0


def test_the_rerun_tools_expose_recompute_universe():
    for rel in ("tools/rerun_stored_row.py", "tools/perf_run.py"):
        assert '"--recompute-universe"' in open(os.path.join(_ROOT, rel), encoding="utf-8").read(), rel
    import inspect
    assert "recompute_universe" in inspect.signature(RH.rebuild_config_for_backtest).parameters
    bt = SimpleNamespace(engine_type="daily_expert", optimization_id=None, id=1)
    with pytest.raises(ValueError, match="only supported for optimization-derived rows"):
        RH.rebuild_config_for_backtest(bt, None, None, recompute_universe=True)


# ------------------------------------------------------------------- the REAL launcher, end to end
def _make_panel(cache):
    """A tiny DAILY criteria panel for the 60 synthetic names: every name sits in the 2-10B band all through the
    window (close 20, shares = cap / 20), so the loosest filters select all 60."""
    import numpy as np
    from datetime import date
    from ba2_providers.screener import live_sim as ls
    if ls.latest_panel(str(cache)):
        return                       # already built in this cache (a mapped panel cannot be replaced on Windows)
    sessions = [d.date().isoformat() for d in pd.bdate_range("2022-01-03", "2023-04-14")]
    T = len(sessions)
    bars, shares = {}, np.zeros((N, T))
    for i, sym in enumerate(ALL):
        idx = np.arange(0, T - 15)
        c = np.full(idx.size, 20.0)
        bars[sym] = (idx, c, c * 1.01, c * 0.99, c, np.full(idx.size, 2e6))
        shares[i] = (9.9e9 - i * 1e8) / 20.0
    arrays = ls.build_panel_arrays(bars, sessions, shares, ALL)
    ls.save_panel(ls.panel_dir_for(str(cache), "testfp"), ALL, sessions, arrays,
                  {"shares_vendor_snapshot": "t", "shares_lag_days": 45, "stale_listed_symbols": [],
                   "panel_fingerprint": "testfp", "fresh_fraction": 1.0,
                   "last_bar_date": sessions[T - 16], "source_fingerprint": "t"})


def _launch(monkeypatch, store, tmp_path, *extra, cached=None, name="sup"):
    """``ba2-test optimize --screener ...`` through ``L._cmd_optimize`` (harness as in
    test_ds_macro_short_side_launcher); returns ``(rc, persisted backtest block)``."""
    import app.services.strategy_optimization_handler as SOH
    sys.path.insert(0, os.path.join(_ROOT, "testplatform"))
    import ba2test_launcher as L
    import ba2_common.config as bcfg
    import ba2_common.core.db as bdb
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization

    cache = tmp_path / "cache"
    (cache / "FMPOHLCVProvider").mkdir(parents=True, exist_ok=True)
    for s in (ALL if cached is None else cached):
        (cache / "FMPOHLCVProvider" / f"{s}_1d.parquet").write_bytes(b"")
    monkeypatch.setattr(bcfg, "CACHE_FOLDER", str(cache))
    _make_panel(cache)
    monkeypatch.setattr(SOH, "handle_strategy_optimization", lambda task_id, payload: {"status": "completed"})
    monkeypatch.setattr(L, "_persist_top_backtests", lambda *a, **k: 0)
    captured = {}
    original = L._cmd_optimize
    monkeypatch.setattr(L, "_cmd_optimize", lambda args: (captured.__setitem__("args", args), 0)[1])
    saved = (os.getcwd(), bdb._db_file)
    try:
        assert L.main(["optimize", "--expert", "FMPRating", "--strategy", "S1", "--universe", "AAPL",
                       "--start", "2023-03-06", "--end", "2023-03-24", "--population", "2",
                       "--generations", "1", "--rerun", "--interval", "1d",
                       "--screener", "--screener-store", store, "--screener-cap-band", "mid",
                       "--name", f"{name}-{os.getpid()}", *extra]) == 0
    finally:
        os.chdir(saved[0])
        bdb._db_file = saved[1]
    monkeypatch.setattr(L, "_cmd_optimize", original)
    try:
        rc = original(captured["args"])
    finally:
        os.chdir(saved[0])
        bdb._db_file = saved[1]
    db = SessionLocal()
    try:
        row = db.query(StrategyOptimization).order_by(StrategyOptimization.id.desc()).first()
        return rc, row.optimization_config["backtest"]
    finally:
        db.close()


def test_launcher_freezes_the_superset_stamps_the_rule_and_prints_the_size(store, tmp_path, monkeypatch, capsys):
    rc, block = _launch(monkeypatch, store, tmp_path, name="supA")
    assert rc == 0
    assert block["enabled_instruments"] == ALL                  # all 60, not the top-50-by-cap 50
    assert "S55" in block["enabled_instruments"]
    assert block["screener_universe_rule"] == us.RULE_ID
    assert "screener static universe = 60 symbols" in capsys.readouterr().out


def test_launcher_refuses_when_a_superset_symbol_has_no_cached_bars_and_lists_it(store, tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as ei:
        _launch(monkeypatch, store, tmp_path, cached=[s for s in ALL if s not in ("S10", "S55")], name="supB")
    msg = str(ei.value)
    assert "REFUSED" in msg and "S10, S55" in msg and "--screener-exclude-uncached" in msg


def test_launcher_exclude_uncached_records_the_exclusion_on_the_run(store, tmp_path, monkeypatch):
    rc, block = _launch(monkeypatch, store, tmp_path, "--screener-exclude-uncached",
                        cached=[s for s in ALL if s != "S10"], name="supC")
    assert rc == 0
    assert "S10" not in block["enabled_instruments"] and len(block["enabled_instruments"]) == N - 1
    assert block["excluded_instruments"] == ["S10"]              # the gate excludes it too: guard stays at 0


def test_print_universe_dry_run_prints_the_launch_universe_and_exits(store, tmp_path, monkeypatch, capsys):
    """Preflights (the grid shell scripts) ask the launcher itself instead of re-deriving the universe."""
    with pytest.raises(SystemExit) as ei:
        _launch(monkeypatch, store, tmp_path, "--print-universe", name="supP")
    assert ei.value.code == 0
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if l.startswith("PRINT-UNIVERSE ")][0]
    import json as _json
    got = _json.loads(line[len("PRINT-UNIVERSE "):])
    assert got["static_universe_size"] == N and got["uncached"] == [] and got["rule"] == us.RULE_ID
    assert got["criteria_version"] == "live-daily-v2"
    other = tmp_path / "second"
    other.mkdir()
    with pytest.raises(SystemExit) as ei2:
        _launch(monkeypatch, store, other, "--print-universe", cached=[x for x in ALL if x != "S10"], name="supQ")
    assert ei2.value.code == 3
