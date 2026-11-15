"""A genome with ACTIVE iv-v1 genes survives the TOP-N persist and the walk-forward out-of-sample
re-run, whole (review 2026-11-15; style of ``test_option_wf_oos_rerun_genes.py``).

Both paths rebuild the trial from the STORED genome through ``decode_params`` +
``_build_daily_trial_config``; option re-runs have dropped genes before. Pinned for a launcher-built
option structure gated on ``iv-v1``: the re-run trial carries the resolved iv leaves (operator and
threshold from the genome), the profile and its manifest pin, and the persisted row carries the iv
genes. Only the engine call (``_persist_trial_worker``) is replaced; nothing is backtested.
"""
from __future__ import annotations

import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from app.services import walk_forward as WF
from tests.test_walk_forward import D

_TOOLS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

KIND = "O_BULLCS"
TRAIN = ("2022-10-03", "2023-12-29")
TEST = (D("2024-01-01"), D("2024-06-28"))
DIGEST = "f" * 64
GENOME = {"cond:o_bullcs-market-ivrv:mode": "above", "cond:o_bullcs-market-ivrv:value": 1.4,
          "cond:o_bullcs-market-ivterm:mode": "below", "cond:o_bullcs-market-ivterm:value": 1.1}
_RESULTS = {
    "total_trades": 6, "winning_trades": 4, "losing_trades": 2, "win_rate": 66.7,
    "total_return": 7.5, "annualized_return": 15.0, "buy_hold_return": 0.0,
    "sharpe_ratio": 1.0, "sortino_ratio": 1.2, "calmar_ratio": 0.8, "volatility": 9.0,
    "max_drawdown": -6.0, "avg_drawdown": -2.0, "max_drawdown_duration": 1.0,
    "profit_factor": 2.0, "expectancy": 1.0, "sqn": 0.5, "avg_trade": 1.0,
    "best_trade": 4.0, "worst_trade": -2.0, "avg_trade_duration": 9.0,
    "exposure_time": 30.0, "final_equity": 21_500.0, "equity_peak": 22_000.0,
    "equity_curve": [{"date": "2024-01-02", "equity": 20_000.0}],
    "drawdown_curve": [{"date": "2024-01-02", "drawdown": 0.0}], "trades": []}


def _find(node, leaf_id):
    if isinstance(node, list):
        for x in node:
            hit = _find(x, leaf_id)
            if hit is not None:
                return hit
    elif isinstance(node, dict):
        if node.get("id") == leaf_id:
            return node
        for v in node.values():
            hit = _find(v, leaf_id)
            if hit is not None:
                return hit
    return None


@pytest.fixture
def world(monkeypatch):
    import app.models  # noqa: F401
    from app.models.database import Base, SessionLocal, engine
    from app.models.strategy_optimization import StrategyOptimization
    from app.services import strategy_optimization_handler as soh
    from app.services import sync_client
    from tests.test_gene_to_artefact_audit import _backtest_cfg
    import run_walk_forward as RWF

    Base.metadata.create_all(bind=engine)
    runner = RWF.RealRunner(SimpleNamespace(min_trade_rows=30, min_return_rel_pct=5.0, min_dd_pts=2.0,
                                            min_trades_rel_pct=5.0, min_free_gb=0.0, force_memory=True))
    L = runner.launcher_module()
    monkeypatch.setattr(sync_client, "push_backtest", lambda bt, db: None)
    seen = []

    def worker(cfg):
        seen.append(cfg)
        return {"ok": True, "results": dict(_RESULTS)}
    monkeypatch.setattr(soh, "_persist_trial_worker", worker)
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(L, "_new_local_pool", lambda n: ThreadPoolExecutor(max_workers=n))

    monkeypatch.setattr(L, "_MARKET_CONDITION_PROFILES", ("iv-v1",))
    built = L._build_strategy(KIND, f"ivre-{KIND}", "DeterministicScorer")
    assert _find(built.entry_rules, "o_bullcs-market-ivrv") is not None
    block = dict(_backtest_cfg(built, "DeterministicScorer"), start_date=TRAIN[0], end_date=TRAIN[1],
                 labels=["ivre-test"])
    # what _apply_market_conditions writes onto a gated job (the setting live and the seam read,
    # plus the resolved plural pins every stored-config consumer reads)
    for spec in block["experts"]:
        spec.setdefault("settings", {})["market_condition_profile"] = "iv-v1"
    block["market_condition_profiles"] = ["iv-v1"]
    block["market_condition_manifests"] = {"iv-v1": DIGEST}
    name = f"ivre-test-{KIND}-{uuid.uuid4().hex[:8]}-wf1"
    db = SessionLocal()
    try:
        db.add(built); db.commit(); db.refresh(built)
        opt = StrategyOptimization(
            strategy_id=built.id, name=name, fitness_metric="option_car_target_soft30",
            optimization_type="genetic", optimization_config={"backtest": block},
            all_results=[{"params": dict(GENOME), "fitness": 2.5, "total_return": 40.0,
                          "max_drawdown": -12.0, "trades": 80, "key": "k-iv"}],
            best_params=dict(GENOME), best_fitness=2.5, status="completed")
        db.add(opt); db.commit(); db.refresh(opt)
        opt_id = opt.id
    finally:
        db.close()
    return SimpleNamespace(L=L, runner=runner, seen=seen, name=name, opt_id=opt_id)


def _assert_iv_trial(cfg):
    ivrv = _find(cfg["entry_rules"], "o_bullcs-market-ivrv")
    ivterm = _find(cfg["entry_rules"], "o_bullcs-market-ivterm")
    assert ivrv is not None and ivterm is not None, "an active iv gene was dropped by the re-run"
    assert (ivrv["field"], ivrv["op"], ivrv["value"]) == ("underlying_iv_rv_ratio_30_20", ">", pytest.approx(1.4))
    assert (ivterm["field"], ivterm["op"], ivterm["value"]) == ("underlying_iv_term_slope_30_60", "<",
                                                                 pytest.approx(1.1))
    assert cfg["market_condition_profiles"] == ["iv-v1"]
    assert cfg["market_condition_manifests"] == {"iv-v1": DIGEST}
    assert all(e["settings"]["market_condition_profile"] == "iv-v1" for e in cfg["experts"])


def _row_params(backtest_id):
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    db = SessionLocal()
    try:
        return dict(db.query(Backtest).filter_by(id=backtest_id).one().strategy_params)
    finally:
        db.close()


def test_the_walk_forward_oos_rerun_keeps_the_active_iv_genes(world):
    picks = world.runner.select(world.name, 1)
    assert picks[0].params == GENOME
    fold = WF.Fold(1, D(TRAIN[0]), D(TRAIN[1]), *TEST)
    out = world.runner.run_oos(world.name, fold, picks, WF.oos_name_prefix(1), WF.oos_labels(1), 1)
    assert len(world.seen) == 1
    cfg = world.seen[0]
    assert (cfg["start_date"], cfg["end_date"]) == (TEST[0].isoformat(), TEST[1].isoformat())
    _assert_iv_trial(cfg)
    params = _row_params(out[1].backtest_id)
    assert {g: params[g] for g in GENOME} == GENOME


def test_the_top_n_persist_keeps_the_active_iv_genes(world):
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal

    persisted = world.L._persist_top_backtests(world.opt_id, "DeterministicScorer", n=1, parallel=1,
                                               use_remote_workers=False)
    assert persisted == 1 and len(world.seen) == 1
    cfg = world.seen[0]
    assert (cfg["start_date"], cfg["end_date"]) == TRAIN        # the train window itself
    _assert_iv_trial(cfg)
    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.name.like(f"TOP1-%{world.name}%")).one()
        assert {g: bt.strategy_params[g] for g in GENOME} == GENOME
    finally:
        db.close()
