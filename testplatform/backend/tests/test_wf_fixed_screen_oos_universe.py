"""A FIXED screen (``optimize --screener --screen-preset <name>``, no screener genes) survives the walk-forward
out-of-sample re-run.

The re-run recomputes a screener job's static universe for the TEST window (``_test_window_static_universe`` ->
``rerun_handler.recompute_static_universe``). That function derived the loosest values from the optimization's
``screener:*`` gene ranges and REFUSED when there were none -- which is every fixed-screen job, by construction. So the
goal2027 ``sqm-xl`` cell (DeterministicScorer SQM on ``--screen-preset xl``) would have ERRORed right after its fold-1
training: "stores no screener gene ranges (parameter_ranges has no 'screener:*' keys)".

Pinned here, on a job stored by the REAL launcher (``ba2test_launcher.main(["optimize", ..., "--screen-preset", ...])``)
in the synthetic daily-panel world of ``test_screener_launch_shared``:

* the test-window universe of a fixed screen is the preset's criteria applied to the TEST window -- equal to what a fresh
  launch of the same preset on that window computes (the panel's visible scans of the window only: no look-ahead), and
  NOT the train window's list;
* it reaches the trial config through ``_persist_top_backtests(window=...)`` -- plain and with a two-phase
  ``data_rebase`` -- with the preset's settings as the per-bar gate (the selection a live deployment of the preset makes);
* a block from before the declaration (no ``declared_ranges``) with no gene ranges still refuses, and a block that
  claims a fixed screen while its optimization searched screener genes refuses as contradictory.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_screener_launch_shared as S  # noqa: E402

from app.services.backtest import screener_launch as sl  # noqa: E402
from ba2_providers.screener import universe_superset as us  # noqa: E402

env = S.env
L = S.L

#: A preset with the shape of ``xl`` (every selection key pinned, no genes), floors scaled to the synthetic world so the
#: screen admits some names and its membership MOVES between the train and the test window.
PRESET = "wf-oos-test"
PRESET_KEYS = {"market_cap_min": 4e9, "market_cap_max": 0, "price_min": 10, "volume_min": 300000,
               "dollar_volume_min": 0, "float_min": 10000000, "relative_volume_min": 0, "price_drop_pct": 0,
               "price_drop_days": 5, "weinstein_stage2_only": False, "max_stocks": 5}


@pytest.fixture()
def preset(monkeypatch):
    monkeypatch.setitem(L._SCREEN_PRESETS, PRESET, dict(PRESET_KEYS))
    return PRESET


def _windows(env):
    s = env["sessions"]
    start, end = S._window(env)
    i0, i1 = s.index(start), s.index(end)
    mid = (i0 + i1) // 2
    return (start, s[mid]), (s[mid + 1], end)


_N = iter(range(1, 10_000))


def _launch_fixed_screen(env, monkeypatch, preset):
    """The train job as the launcher stores it, on the TRAIN window; returns (opt_id, stored backtest block,
    parameter_ranges as the optimization handler computes them for this job)."""
    name = f"xl-like-{next(_N)}-wf1"        # one test DB per session: a launch never reuses a stored job's name
    from app.models.database import SessionLocal
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.services import strategy_optimization_handler as H
    from app.services.task_queue import get_task_queue
    (tr_start, tr_end), _ = _windows(env)
    monkeypatch.setattr(get_task_queue(), "queue_task", lambda *a, **kw: "stub-task", raising=False)
    argv = ["optimize", "--expert", "FMPRating", "--strategy", "S1", "--universe", "AAPL", "--start", tr_start,
            "--end", tr_end, "--interval", "1d", "--screener", "--screener-store", env["store"],
            "--screen-preset", preset, "--name", name, "--submit", "--run-schedule", "daily", "--population", "4",
            "--generations", "1", "--seed", "7", "--ohlcv-history-source", "fmp", "--screener-exclude-uncached"]
    assert L.main(argv) == 0
    db = SessionLocal()
    try:
        opt = (db.query(StrategyOptimization).filter(StrategyOptimization.name == name)
               .order_by(StrategyOptimization.id.desc()).first())
        strat = db.query(Strategy).filter(Strategy.id == opt.strategy_id).first()
        cfg = copy.deepcopy(opt.optimization_config)
        # what the handler stores as ``parameter_ranges`` when the job runs (the --submit launch does not run it)
        ranges = H.job_param_space(strat, cfg.get("expert_params"), H._is_bypass_expert(cfg["backtest"]))
        opt.parameter_ranges = ranges
        db.commit()
        return opt.id, cfg["backtest"], ranges
    finally:
        db.close()


def _fresh_launch_universe(env, preset, start, end):
    """The static universe a NEW launch of the same preset on [start, end] computes (the launch's own function)."""
    from types import SimpleNamespace
    base, scr_opt, cap = L._screener_launch_inputs(SimpleNamespace(screen_preset=preset, screener=True,
                                                                   screener_cap_band=None, screener_base_json=None))
    req = sl.ScreenerLaunchRequest(store=env["store"], start=start, end=end, interval="1d", history_source="fmp",
                                   base_settings=base, scr_opt=scr_opt, cadence_days=7, bypass=False,
                                   panel=env["panel"], exclude_uncached=True, cap_band=cap)
    return [x for x in sl.prepare_screener_launch(req, emit=lambda m: None).enabled if x != S.UNCACHED]


def test_the_launcher_stores_a_fixed_screen_with_no_gene_ranges(env, monkeypatch, preset):
    _, blk, ranges = _launch_fixed_screen(env, monkeypatch, preset)
    so = blk["screener_opt"]
    assert so["declared_ranges"] == {} and so["screen_preset"] == preset
    assert blk["screener_universe_rule"] == us.RULE_ID
    assert not [k for k in ranges if k.startswith("screener:")]           # nothing to loosen: the old refusal's trigger
    assert {k: so["base_settings"][k] for k in PRESET_KEYS} == PRESET_KEYS   # the preset's criteria, stored


def test_test_window_universe_is_the_preset_on_the_test_window(env, monkeypatch, preset, capsys):
    opt_id, blk, ranges = _launch_fixed_screen(env, monkeypatch, preset)
    (tr_start, tr_end), (te_start, te_end) = _windows(env)
    test_blk = {**blk, "start_date": te_start, "end_date": te_end}
    out = L._test_window_static_universe(test_blk, ranges, opt_id, "WF1-OOS-R")
    fresh = _fresh_launch_universe(env, preset, te_start, te_end)
    assert out["enabled_instruments"] == fresh and fresh                 # the preset, point-in-time, on the TEST window
    assert out["enabled_instruments"] != blk["enabled_instruments"]      # not the train window's list
    assert blk["enabled_instruments"] == _fresh_launch_universe(env, preset, tr_start, tr_end)
    assert out["screener_universe_rule"] == us.RULE_ID
    assert out["screener_opt"]["base_settings"] == blk["screener_opt"]["base_settings"]   # the criteria are the run's
    assert "recomputed for the test window" in capsys.readouterr().out


def _persist_capturing(L, monkeypatch, opt_id, window, data_rebase=None):
    from concurrent.futures import ThreadPoolExecutor
    from app.services import strategy_optimization_handler as soh
    from app.services import sync_client
    monkeypatch.setattr(sync_client, "push_backtest", lambda bt, db: None)
    seen = []

    def worker(cfg):                       # the trial config the re-run would execute; stop before the engine
        seen.append(cfg)
        return {"ok": False, "error": "stop here"}
    monkeypatch.setattr(soh, "_persist_trial_worker", worker)
    monkeypatch.setattr(L, "_new_local_pool", lambda n: ThreadPoolExecutor(max_workers=n))
    L._persist_top_backtests(opt_id, "FMPRating", n=1, parallel=1, candidates=[({}, "k", 1.0)], ranks=[1],
                             name_prefix="WF1-OOS-R", use_remote_workers=False, persisted_ids=[],
                             window=window, data_rebase=data_rebase)
    assert len(seen) == 1
    return seen[0]


@pytest.mark.parametrize("deferred", [False, True], ids=["oos", "deferred-two-phase"])
def test_persist_top_backtests_runs_the_test_on_the_preset_universe(env, monkeypatch, preset, deferred):
    opt_id, blk, _ = _launch_fixed_screen(env, monkeypatch, preset)
    _, (te_start, te_end) = _windows(env)
    rebase = None
    if deferred:     # the phase-B command: the same optimize args (no --wf-data-end), its data choices
        rebase = L._data_rebase_from_optimize_args(["--screener", "--screener-store", env["store"],
                                                    "--screen-preset", preset, "--screener-exclude-uncached"])
    cfg = _persist_capturing(L, monkeypatch, opt_id, (te_start, te_end), rebase)
    fresh = _fresh_launch_universe(env, preset, te_start, te_end)
    assert set(cfg["enabled_instruments"]) <= set(fresh) and cfg["enabled_instruments"]
    rt = cfg["screener_runtime"]
    # the per-bar gate selects with the preset's own keys (max_stocks cut included): the live preset's selection
    for k in ("market_cap_min", "price_min", "volume_min", "float_min", "max_stocks"):
        assert rt["settings"][k] == PRESET_KEYS[k], k
    assert S.UNCACHED in [s.upper() for s in rt["excluded_symbols"]]


def test_a_block_from_before_the_declaration_still_refuses(env, monkeypatch, preset):
    from app.services.backtest import rerun_handler as RH
    opt_id, blk, ranges = _launch_fixed_screen(env, monkeypatch, preset)
    legacy = copy.deepcopy(blk)
    del legacy["screener_opt"]["declared_ranges"]
    with pytest.raises(us.ScreenerUniverseError, match="stores no screener gene ranges"):
        RH.recompute_static_universe(legacy, ranges, row_id=1)


def test_a_fixed_screen_whose_optimization_searched_screener_genes_refuses(env, monkeypatch, preset):
    from app.services.backtest import rerun_handler as RH
    _, blk, ranges = _launch_fixed_screen(env, monkeypatch, preset)
    searched = {**ranges, "screener:screener_market_cap_min": dict(us.SCREENER_OPT["screener_market_cap_min"])}
    with pytest.raises(us.ScreenerUniverseError, match="contradicts itself"):
        RH.recompute_static_universe(blk, searched, row_id=2)


def test_is_fixed_screen_reads_only_an_explicit_empty_declaration():
    from app.services.backtest.rerun_handler import is_fixed_screen
    assert is_fixed_screen({"declared_ranges": {}})
    assert not is_fixed_screen({})                                        # legacy: unknown ranges
    assert not is_fixed_screen({"declared_ranges": None})
    assert not is_fixed_screen({"declared_ranges": {"market_cap_min": {"min": 1, "max": 2}}})


def test_in_place_rerun_with_recompute_universe_of_a_fixed_screen_oos_row(env, monkeypatch, preset):
    """The sibling path: ``rerun_handler`` re-running a stored WF out-of-sample row in place with
    ``--recompute-universe`` (the row's own window = the test window) uses the same recompute."""
    from datetime import datetime
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest import rerun_handler as RH
    opt_id, _, _ = _launch_fixed_screen(env, monkeypatch, preset)
    _, (te_start, te_end) = _windows(env)
    db = SessionLocal()
    try:
        bt = Backtest(name=f"WF1-OOS-R1-xl-like-{opt_id}", expert_name="FMPRating", engine_type="daily_expert",
                      strategy_params={}, start_date=datetime.fromisoformat(te_start),
                      end_date=datetime.fromisoformat(te_end), initial_capital=10_000.0, optimization_id=opt_id)
        db.add(bt)
        db.commit()
        db.refresh(bt)
        cfg = RH._build_optimization_rerun_config(db, bt, recompute_universe=True)
    finally:
        db.close()
    fresh = _fresh_launch_universe(env, preset, te_start, te_end)
    assert cfg["screener_universe_recompute_note"]["static_universe_size"] == len(fresh)
    assert set(cfg["enabled_instruments"]) <= set(fresh) and cfg["enabled_instruments"]
    assert cfg["screener_runtime"]["settings"]["max_stocks"] == PRESET_KEYS["max_stocks"]
