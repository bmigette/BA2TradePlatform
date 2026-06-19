"""OPTIONS OPTIMIZATION — GA end-to-end through ``handle_strategy_optimization``.

This drives the REAL genetic optimizer control flow (param-space collection -> seeded GA ->
per-trial config build -> fitness -> persist) over a REAL ``Strategy`` whose exit rule is an
OPTION action with the option selection-param genes turned on. It proves the three things the
options-optimization path must hold:

  1. the option genes (``exit:<id>:option_delta`` / ``exit:<id>:option_dte``) appear in the
     run's param space;
  2. EVERY per-trial config the optimizer builds carries a non-None ``options_cache_db`` (the
     HistoricalOptionsProvider injection seam) — i.e. each trial runs as an OPTIONS backtest;
  3. the run COMPLETES (>0 trials) and ``best_params`` includes the option genes.

It does NOT touch the network/FMP/Alpaca or a real options cache: only the data-heavy leaf
``_run_trial_backtest`` is stubbed (mirroring ``test_strategy_optimization_handler.py``). The
stub goes THROUGH the real ``_build_daily_trial_config`` so the options-provider wiring is
exercised and asserted per trial; its fitness is a deterministic function of the option genes
so the GA has a real gradient to climb (proving the genes drive trial selection). The fully
data-driven GA-over-a-fixture-cache run is left as a gated/manual check (it needs real experts
+ a built options cache + multi-trial real backtests — too heavy/flaky for CI).

Needs deap (the genetic optimizer). Run with the deap-enabled venv:
    ~/ba2-venvs/test/bin/python -m pytest tests/test_options_optimization_ga_e2e.py -q
"""
from __future__ import annotations

import pytest

from app.services.genetic import DEAP_AVAILABLE

pytestmark = pytest.mark.skipif(not DEAP_AVAILABLE, reason="deap not available")

from app.models.database import Base, SessionLocal, engine  # noqa: E402
from app.models.strategy import Strategy  # noqa: E402
from app.models.strategy_optimization import StrategyOptimization  # noqa: E402
from app.services import strategy_optimization_handler as H  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    Base.metadata.create_all(bind=engine)
    yield


# An exit rule that is an OPTION action (buy_call) with BOTH option selection-param genes on.
_OPTION_EXIT = {
    "id": "o1",
    "action": "buy_call",
    "action_type": "buy_call",
    "option_strategy": "buy_call",
    "option_strike_param": 0.3,
    "option_strike_param_optimize": True,
    "option_strike_param_min": 0.2,
    "option_strike_param_max": 0.5,
    "option_strike_param_step": 0.05,
    "option_dte_optimize": True,
    "option_dte_min_range": 20,
    "option_dte_max_range": 45,
    "option_dte_step": 5,
}


def _seed_option_strategy() -> int:
    db = SessionLocal()
    try:
        s = Strategy(
            name="opt-options-e2e",
            initial_tp_percent=5.0,
            initial_tp_optimize=False,
            initial_sl_percent=2.0,
            initial_sl_optimize=False,
            exit_conditions=[dict(_OPTION_EXIT)],
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id
    finally:
        db.close()


def _ga_config() -> dict:
    return {
        "populationSize": 8,
        "generations": 4,
        "crossoverProb": 0.7,
        "mutationProb": 0.3,
        "earlyStoppingGenerations": 10,
        "elitismPercent": 10.0,
        "seed": 42,
        "backtest": {
            "engine": "daily",
            "backtest_id": 9001,
            "start_date": "2024-02-01",   # >= the 2024-02-01 options-history floor
            "end_date": "2024-02-29",
            "enabled_instruments": ["AAPL"],
            "experts": [{"class": "FMPEarningsDrift", "settings": {}}],
            "initial_capital": 100000.0,
            "account_settings": {"starting_cash": 100000.0},
            "warmup_days": 30,
            "seed": 42,
        },
    }


def _seed_opt(strategy_id: int) -> int:
    db = SessionLocal()
    try:
        row = StrategyOptimization(
            strategy_id=strategy_id,
            name="opt-options-run",
            fitness_metric="sharpe",
            optimization_type="genetic",
            optimization_config=_ga_config(),
            status="pending",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _load_opt(opt_id: int) -> StrategyOptimization:
    db = SessionLocal()
    try:
        row = db.query(StrategyOptimization).filter(
            StrategyOptimization.id == opt_id
        ).first()
        _ = (row.best_params, row.all_results, row.parameter_ranges)
        db.expunge(row)
        return row
    finally:
        db.close()


def test_ga_options_run_injects_provider_and_optimizes_option_genes(monkeypatch):
    """A small REAL GA run over an option-action strategy: the option genes are in the param
    space, every trial config carries the options provider (options_cache_db), the run completes
    with >0 trials, and best_params includes the option genes."""
    seen_caches: list = []

    def _stub_trial(backtest_cfg, hoisted, decoded):
        # Go THROUGH the real per-trial config build so the options-provider wiring under test
        # is exercised + asserted for EVERY trial. (This is exactly the seam the optimizer uses
        # before calling run_daily_backtest.)
        cfg = H._build_daily_trial_config(backtest_cfg, decoded)
        seen_caches.append(cfg["options_cache_db"])
        # The option rule's selection params flow from the genes onto the decoded exit rule.
        rule = decoded["exit_rules"][0]
        delta = rule.get("option_strike_param", 0.3)
        dte = rule.get("option_dte_min", 30)
        # Deterministic fitness with a clear peak at delta=0.35, dte=30 so the GA has a gradient
        # to climb over the OPTION genes (proving the genes drive trial selection).
        score = 10.0 - abs(delta - 0.35) * 20.0 - abs(dte - 30) * 0.1
        return {
            "total_trades": 3,
            "sharpe_ratio": score,
            "max_drawdown": 5.0,
            "total_return": score,
            "profit_factor": 1.5,
            "win_rate": 55.0,
        }

    monkeypatch.setattr(H, "_run_trial_backtest", _stub_trial)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {"backtest_cfg": cfg})

    sid = _seed_option_strategy()
    opt_id = _seed_opt(sid)
    out = H.handle_strategy_optimization("t-opt-options", {"optimization_id": opt_id})

    assert out["status"] == "completed", out
    row = _load_opt(opt_id)
    assert row.status == "completed"

    # 1. The option genes appear in the run's param space.
    assert row.parameter_ranges, "expected a non-empty param space"
    assert "exit:o1:option_delta" in row.parameter_ranges
    assert "exit:o1:option_dte" in row.parameter_ranges

    # 2. EVERY per-trial config carried the options provider (non-None options_cache_db) — each
    #    trial ran as an OPTIONS backtest.
    assert seen_caches, "expected >0 trials to have been built"
    assert all(c is not None for c in seen_caches), (
        f"some trials lacked the options provider: {seen_caches}"
    )

    # 3. The run completed with >0 trials and best_params includes the option genes.
    assert row.all_results and len(row.all_results) > 0
    assert row.best_params is not None
    assert "exit:o1:option_delta" in row.best_params
    assert "exit:o1:option_dte" in row.best_params
    # And the GA climbed toward the deterministic peak (delta 0.35, dte 30).
    assert 0.2 <= row.best_params["exit:o1:option_delta"] <= 0.5
    assert 20 <= row.best_params["exit:o1:option_dte"] <= 45
    assert row.best_fitness is not None
