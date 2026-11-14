"""Phase B of an option walk-forward cell with ``--screener-gate-store``: the deferred test rebases
the train job's GATE-ONLY run block onto the current command's data.

THE DEFECT (review 2026-11-14). ``--screener-gate-store`` stores ``screener_opt = {gate_only: True,
...}`` on the run block (``ba2test_launcher._screener_gate_opt_block``) while the run universe stays
the static ``--universe``. ``_rebase_run_data`` took ANY ``screener_opt`` for a screener run and
refused the option command's static ``--universe`` ("the run is a screener run; a static --universe
cannot replace its universe"), so no deferred option test with a gate store could run. A gate-only
block is now a static run: the universe is the current one, the gate store stays the stored one
(the cell name carries its path). Real screener blocks are unchanged
(tests/test_wf_test_data_rebase.py)."""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import walk_forward as WF
from app.services import wf_chain as C
from tests.test_walk_forward import D

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))
LAUNCHER = str(_ROOT / "testplatform" / "ba2test_launcher.py")
KIND = "O_BULLCS"
SEL = {"min_trade_rows": 30, "min_return_rel_pct": 5.0, "min_dd_pts": 2.0, "min_trades_rel_pct": 5.0}


def _driver():
    spec = importlib.util.spec_from_file_location("rom_gate", _ROOT / "tools" / "run_options_matrix.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _gate_ns(store):
    """The launcher namespace a ``run_options_matrix`` job with ``--screener-gate-store`` has."""
    return SimpleNamespace(screener_gate_store=store, screener=False, max_stock_price=0.0,
                           screener_base_json=None, screener_cadence_days=7)


def _cell_spec(M, store, monkeypatch, universe="AAPL,MSFT"):
    """The phase-B cell the option driver builds (rail moved to 2027 so the 2026 test can run)."""
    monkeypatch.setattr(M._launcher_module(LAUNCHER), "_OPTION_HOLDOUT_START", date(2027, 1, 1))
    ap = M.build_parser()
    M.add_estimates_source_flag(ap)
    args = M.resolve_args(ap, [
        "--strategies", KIND, "--experts", "DeterministicScorer", "--estimates-source", "fmp",
        "--launcher", LAUNCHER, "--screener-gate-store", store, "--max-stock-price", "0",
        "--walk-forward", "--wf-defer-last-test", "--wf-report-dir", "unused", "--wf-top-n", "1",
        "--pass-min-oos-return", "0", "--pass-max-dd-mult", "2.5", "--pass-min-folds", "2"])
    return M.wf_cell_specs(args, LAUNCHER, ["DeterministicScorer"], [KIND], universe, SEL)[0]


@pytest.fixture(scope="module")
def L():
    from tests.test_walk_forward import _launcher_mod
    return _launcher_mod()


# ------------------------------------------------------------------ the rebase itself (real launcher)


def test_a_gate_only_option_block_is_rebased_as_a_static_run(L, monkeypatch):
    """End to end on the real functions: the option driver's cell args -> the rebase spec -> the
    stored gate-only block of the train job."""
    M = _driver()
    spec = _cell_spec(M, "GATE-STORE", monkeypatch, universe="AAPL,MSFT,NVDA")
    rebase = L._data_rebase_from_optimize_args(spec.optimize_args)
    assert rebase["static_universe"] == ["AAPL", "MSFT", "NVDA"] and rebase["screener_store"] is None
    gate = L._screener_gate_opt_block(_gate_ns("GATE-STORE"), KIND)
    assert gate["gate_only"] is True
    stored = {"start_date": "2026-01-01", "end_date": "2026-12-31", "screener_opt": gate,
              "enabled_instruments": ["AAPL", "OLD"], "excluded_instruments": ["MSFT"]}
    out = L._rebase_run_data(stored, rebase)
    assert out["enabled_instruments"] == ["AAPL", "NVDA"]          # current universe minus the stored exclusion
    assert out["excluded_instruments"] == ["MSFT"]
    assert out["screener_opt"] == gate                             # the gate store is KEPT
    assert stored["enabled_instruments"] == ["AAPL", "OLD"]        # a copy: the stored block is untouched


def test_a_gate_only_block_still_refuses_a_screener_store(L):
    gate = L._screener_gate_opt_block(_gate_ns("GATE-STORE"), KIND)
    with pytest.raises(ValueError, match="static-universe"):
        L._rebase_run_data({"screener_opt": gate, "enabled_instruments": ["AAPL"]},
                           {"static_universe": None, "screener_store": "OTHER", "manifests": None,
                            "exclude_symbols": []})


def test_a_real_screener_block_is_still_refused_a_static_universe(L):
    """Unchanged for the stock grid: a screener run never takes a static --universe."""
    with pytest.raises(ValueError, match="screener run"):
        L._rebase_run_data({"screener_opt": {"store": "s"}, "enabled_instruments": ["AAPL"]},
                           {"static_universe": ["AAPL"], "screener_store": None, "manifests": None,
                            "exclude_symbols": []})


# ------------------------------------------------------------------ the deferred test, real path


def test_the_deferred_option_test_reaches_the_rebase_with_the_cells_data_args(L, monkeypatch):
    """Phase B, fold 3 of a ``--screener-gate-store`` cell: wf_chain's run_fold hands the CELL's
    optimize args to RealRunner.run_oos, which rebases the stored gate-only block through the real
    ``_persist_top_backtests``. Only the engine call is replaced."""
    import app.models  # noqa: F401
    import run_walk_forward as RWF
    from app.models.database import Base, SessionLocal, engine
    from app.models.strategy_optimization import StrategyOptimization
    from app.services import strategy_optimization_handler as soh
    from app.services import sync_client
    from app.services.strategy_param_space import collect_param_space
    from tests.test_gene_to_artefact_audit import _audit_metric_store, _backtest_cfg

    Base.metadata.create_all(bind=engine)
    store = _audit_metric_store()
    M = _driver()
    spec = _cell_spec(M, store, monkeypatch, universe="AAPL,MSFT")
    plan, folds = C.make_cell_plan(spec)
    assert plan.rebase_test_data is True and folds[-1].test_start == D("2026-01-01")

    class Runner(RWF.RealRunner):
        def train_completed(self, name):      # the train jobs exist; only the test is under test
            return True

        def seed_lineage(self, name):
            k = int(name.rsplit("-wf", 1)[1])
            return WF.train_job_name(spec.name, k - 1) if k > 1 else None

    runner = Runner(SimpleNamespace(**SEL, min_free_gb=0.0, force_memory=True))
    RL = runner.launcher_module()
    monkeypatch.setattr(sync_client, "push_backtest", lambda bt, db: None)
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(RL, "_new_local_pool", lambda n: ThreadPoolExecutor(max_workers=n))
    rebased, seen = [], []
    real = RL._rebase_run_data
    monkeypatch.setattr(RL, "_rebase_run_data",
                        lambda block, s, command="walk-forward test": rebased.append(s) or real(block, s, command))

    def worker(cfg):
        seen.append(cfg)
        return {"ok": True, "results": {
            "total_trades": 4, "winning_trades": 2, "losing_trades": 2, "win_rate": 50.0,
            "total_return": 3.0, "annualized_return": 3.0, "buy_hold_return": 0.0,
            "sharpe_ratio": 0.5, "sortino_ratio": 0.6, "calmar_ratio": 0.4, "volatility": 7.0,
            "max_drawdown": -5.0, "avg_drawdown": -2.0, "max_drawdown_duration": 1.0,
            "profit_factor": 1.2, "expectancy": 0.5, "sqn": 0.2, "avg_trade": 0.5,
            "best_trade": 2.0, "worst_trade": -1.5, "avg_trade_duration": 9.0,
            "exposure_time": 20.0, "final_equity": 20_600.0, "equity_peak": 21_000.0,
            "equity_curve": [{"date": "2026-01-02", "equity": 20_000.0}],
            "drawdown_curve": [{"date": "2026-01-02", "drawdown": 0.0}], "trades": []}}
    monkeypatch.setattr(soh, "_persist_trial_worker", worker)

    built = RL._build_strategy(KIND, f"wfgate-{KIND}", "DeterministicScorer")
    genome = {g: s["max"] if s["type"] != "choice" else s["choices"][-1]
              for g, s in collect_param_space(built).items()}
    gate = RL._screener_gate_opt_block(_gate_ns(store), KIND)
    block = dict(_backtest_cfg(built, "DeterministicScorer"), start_date="2020-01-01",
                 end_date="2025-12-31", screener_opt=gate, enabled_instruments=["AAPL", "OLD"])
    db = SessionLocal()
    try:
        db.add(built); db.commit(); db.refresh(built)
        db.add(StrategyOptimization(
            strategy_id=built.id, name=WF.train_job_name(spec.name, 3),
            fitness_metric="option_car_target_soft30", optimization_type="genetic",
            optimization_config={"backtest": block},
            all_results=[{"params": genome, "fitness": 2.0, "total_return": 50.0, "max_drawdown": -15.0,
                          "trades": 90, "key": "k"}],
            best_params=genome, best_fitness=2.0, status="completed"))
        db.commit()
    finally:
        db.close()

    got = WF.run_fold(plan, runner, folds[-1], log=lambda *_: None)
    assert [g.rank for g in got] == [1] and got[0].oos_trades == 4
    assert rebased == [L._data_rebase_from_optimize_args(spec.optimize_args)]   # the cell's real args
    assert len(seen) == 1
    cfg = seen[0]
    assert (cfg["start_date"], cfg["end_date"]) == ("2026-01-01", "2026-12-31")
    assert cfg["enabled_instruments"] == ["AAPL", "MSFT"]          # the CURRENT universe, not the stored one
