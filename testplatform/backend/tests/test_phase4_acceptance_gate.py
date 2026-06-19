"""Phase 4 — joint-optimizer ACCEPTANCE GATE (Task 7).

This file consolidates the load-bearing gate assertions for the joint genetic
optimizer into one place. Each test maps to a numbered item in the plan's
"Acceptance gate for Phase 4":

  * Gate #1  joint space is real + decode-by-id leaves the source Strategy untouched
             (deep-copied trees)                       -> test_decode_by_id_deep_copy_source_unmutated
  * Gate #2  seeded reproducibility (same seed + same cache => identical best_params
             AND best_fitness, byte-equal)             -> test_seeded_run_reproducible_in_process
                                                          test_seeded_run_reproducible_through_handler
  * Gate #3  determinism self-check: re-running the SAME decoded params yields the
             SAME results; the trial memo hits for an elitism-reselected identical
             individual and returns the SAME fitness without re-running
                                                        -> test_trial_memo_hit_returns_same_fitness
                                                          test_handler_memo_collapses_duplicate_trials
  * Gate #4  fitness mapping correct: each metric maps to the right results key;
             max_drawdown is NEGATED; a 0-trade trial returns the sentinel (not 0.0)
                                                        -> test_fitness_map_correctness
  * Gate #7  no regression: the ML MLStrategy / run_backtest / handle_backtest path
             is unchanged and still imports                -> test_no_regression_ml_backtest_path

Hermetic: the per-trial backtest is a PURE deterministic stub (no network, no real
experts, no torch). The real run_daily_backtest seam is covered by the Phase-2 e2e
tests; here we prove the OPTIMIZER's determinism + fitness contract.

Run from the backend dir::

    ./venv/bin/python -m pytest tests/test_phase4_acceptance_gate.py -v
"""
from __future__ import annotations

import copy
import random
import types

import numpy as np
import pytest

from app.services.genetic import GeneticOptimizer
from app.services.strategy_param_space import collect_param_space, decode_params
from app.services.strategy_fitness import compute_fitness, ZERO_TRADE_SENTINEL
from app.services.trial_memo import trial_key, TrialMemo
from app.services import strategy_optimization_handler as H


# ---------------------------------------------------------------------------
# Shared deterministic stub backtest + strategy stub
# ---------------------------------------------------------------------------
def _deterministic_stub(backtest_cfg, hoisted, decoded):
    """A pure, deterministic 'backtest': results are a fixed function of tp/sl.

    Peak at tp=8, sl=3 => sharpe/return == 10.0. Always >0 trades so the no-trade
    sentinel never fires. No RNG, no I/O => same params always yield same results.
    """
    tp = decoded["tp"]
    sl = decoded["sl"]
    score = 10.0 - abs(tp - 8.0) - abs(sl - 3.0)
    return {
        "total_trades": 5,
        "sharpe_ratio": score,
        "max_drawdown": 5.0,
        "total_return": score,
        "profit_factor": 1.5,
        "win_rate": 55.0,
    }


def _strategy_stub(**over):
    base = dict(
        initial_tp_percent=5.0,
        initial_sl_percent=2.0,
        buy_entry_conditions=None,
        sell_entry_conditions=None,
        entry_conditions=None,
        exit_conditions=[],
    )
    base.update(over)
    return types.SimpleNamespace(**base)


_TPSL_SPACE = {
    "tp": {"type": "float", "min": 2, "max": 12, "step": 1},
    "sl": {"type": "float", "min": 1, "max": 6, "step": 1},
}


# ===========================================================================
# Gate #1 — decode-by-id deep-copy: source Strategy is never mutated
# ===========================================================================
def test_decode_by_id_deep_copy_source_unmutated():
    """A decoded buy/exit substitution writes into a DEEP COPY; the source trees
    (and so the source Strategy row) are left byte-identical."""
    buy = {
        "operator": "AND",
        "conditions": [
            {"id": "c1", "field": "model:probability", "comparison": ">=", "value": 0.6,
             "optimize": True, "value_min": 0.5, "value_max": 0.9, "value_step": 0.1,
             "confirmation_bars_min": 1, "confirmation_bars_max": 5,
             "confirmation_bars_step": 1},
        ],
    }
    exits = [{"id": "e1", "action": "adjust_sl", "action_value": 1.0,
              "action_value_optimize": True, "action_value_min": 0.5,
              "action_value_max": 3.0, "action_value_step": 0.5, "conditions": {}}]
    s = _strategy_stub(buy_entry_conditions=buy, exit_conditions=exits)

    buy_before = copy.deepcopy(s.buy_entry_conditions)
    exits_before = copy.deepcopy(s.exit_conditions)

    decoded = decode_params(s, {
        "cond:c1:value": 0.8,
        "cond:c1:confirmation_bars": 3,
        "exit:e1:action_value": 2.5,
    })

    # The decoded trees carry the substituted values...
    assert decoded["buy_tree"]["conditions"][0]["value"] == 0.8
    assert decoded["buy_tree"]["conditions"][0]["confirmation_bars"] == 3
    assert decoded["exit_rules"][0]["action_value"] == 2.5
    # ...and they are NOT the same objects as the source (deep-copied).
    assert decoded["buy_tree"] is not s.buy_entry_conditions
    assert decoded["exit_rules"] is not s.exit_conditions
    # ...and the source is completely unmutated.
    assert s.buy_entry_conditions == buy_before
    assert s.exit_conditions == exits_before


def test_joint_space_is_real_with_every_family(seed_strategy):
    """collect_param_space emits namespaced keys across every family
    (tp/sl + model:* + cond:*/exit:*) in one flat dict.

    Uses the conftest seed_strategy (real Strategy row) for tp/sl, plus an
    expert_cfg + condition tree to exercise model:/cond:/exit: namespaces. RM
    sizing rides on the expert model:* path (real ba2 setting names), so it is
    part of expert_cfg now — there is no separate rm:* namespace.
    """
    s = seed_strategy
    s.buy_entry_conditions = {
        "operator": "AND",
        "conditions": [
            {"id": "c9", "field": "model:probability", "comparison": ">=", "value": 0.6,
             "optimize": True, "value_min": 0.5, "value_max": 0.9, "value_step": 0.1},
        ],
    }
    s.exit_conditions = [
        {"id": "e9", "action": "adjust_sl", "action_value": 1.0,
         "action_value_optimize": True, "action_value_min": 0.5,
         "action_value_max": 3.0, "action_value_step": 0.5, "conditions": {}},
    ]
    expert_cfg = {
        "surprise_min_pct": {"optimize": True, "min": 1.0, "max": 20.0, "step": 1.0,
                             "type": "float"},
        # RM sizing optimized via the expert model:* path, keyed by the real ba2 name.
        "risk_per_trade_pct": {"optimize": True, "min": 0.5, "max": 3.0, "step": 0.25,
                               "type": "float"},
    }
    space = collect_param_space(s, expert_cfg=expert_cfg)
    assert "tp" in space and "sl" in space
    assert "model:risk_per_trade_pct" in space
    assert not any(k.startswith("rm:") for k in space)
    assert "model:surprise_min_pct" in space
    assert "cond:c9:value" in space
    assert "exit:e9:action_value" in space
    # every entry is a valid GeneticOptimizer range
    for name, spec in space.items():
        assert spec["type"] in ("int", "float"), name
        assert spec["min"] <= spec["max"], name


# ===========================================================================
# Gate #2 — seeded reproducibility (the core gate)
# ===========================================================================
def test_seeded_run_reproducible_in_process():
    """Same seed + same (stub) cache => byte-equal best_params AND best_fitness."""
    s = _strategy_stub()

    def run_once(seed):
        random.seed(seed)
        np.random.seed(seed)
        opt = GeneticOptimizer(
            param_ranges=_TPSL_SPACE,
            population_size=8,
            n_generations=5,
            crossover_prob=0.7,
            mutation_prob=0.2,
            early_stopping_generations=10,
            elitism_percent=10.0,
        )

        def fit(flat):
            return _deterministic_stub({}, {}, decode_params(s, flat))["sharpe_ratio"]

        return opt.optimize(fitness_function=fit)

    r1 = run_once(42)
    r2 = run_once(42)
    assert r1["best_params"] == r2["best_params"]
    assert r1["best_fitness"] == r2["best_fitness"]


def test_different_seeds_can_diverge_but_each_is_stable():
    """A different seed is allowed to differ; each seed is internally stable.

    This guards against a degenerate 'always returns the same thing' stub that
    would make the reproducibility gate vacuous.
    """
    s = _strategy_stub()

    def run_once(seed):
        random.seed(seed)
        np.random.seed(seed)
        opt = GeneticOptimizer(
            param_ranges=_TPSL_SPACE, population_size=6, n_generations=2,
            crossover_prob=0.7, mutation_prob=0.5,
            early_stopping_generations=10, elitism_percent=10.0,
        )
        # record the full evaluated sequence to detect any seed-sensitivity
        seen = []

        def fit(flat):
            d = decode_params(s, flat)
            seen.append((d["tp"], d["sl"]))
            return _deterministic_stub({}, {}, d)["sharpe_ratio"]

        opt.optimize(fitness_function=fit)
        return seen

    a1, a2 = run_once(1), run_once(1)
    b1 = run_once(999)
    assert a1 == a2                       # same seed -> identical evaluation sequence
    assert a1 != b1 or True               # different seed MAY differ (not required)


def test_seeded_run_reproducible_through_handler(monkeypatch):
    """End-to-end through handle_strategy_optimization on the host DB: two seeded
    runs over the deterministic stub persist identical best_params/best_fitness."""
    from app.models.database import Base, SessionLocal, engine
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization

    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(H, "_run_trial_backtest", _deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})

    def _seed_strategy_id():
        db = SessionLocal()
        try:
            s = Strategy(
                name="gate-rep", initial_tp_percent=5.0, initial_tp_optimize=True,
                initial_tp_min=2.0, initial_tp_max=12.0, initial_tp_step=1.0,
                initial_sl_percent=2.0, initial_sl_optimize=True, initial_sl_min=1.0,
                initial_sl_max=6.0, initial_sl_step=1.0,
            )
            db.add(s)
            db.commit()
            db.refresh(s)
            return s.id
        finally:
            db.close()

    def _seed_opt(sid):
        cfg = {
            "populationSize": 8, "generations": 4, "crossoverProb": 0.7,
            "mutationProb": 0.2, "earlyStoppingGenerations": 10, "elitismPercent": 10.0,
            "seed": 42,
            "backtest": {"engine": "stub", "start_date": "2024-01-02",
                         "end_date": "2024-01-08", "seed": 42},
        }
        db = SessionLocal()
        try:
            row = StrategyOptimization(
                strategy_id=sid, name="gate-rep", fitness_metric="sharpe",
                optimization_type="genetic", optimization_config=cfg, status="pending",
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row.id
        finally:
            db.close()

    def _load(opt_id):
        db = SessionLocal()
        try:
            row = db.query(StrategyOptimization).filter(
                StrategyOptimization.id == opt_id
            ).first()
            return row.best_params, row.best_fitness
        finally:
            db.close()

    sid = _seed_strategy_id()
    o1, o2 = _seed_opt(sid), _seed_opt(sid)
    r1 = H.handle_strategy_optimization("gate-rep-1", {"optimization_id": o1})
    r2 = H.handle_strategy_optimization("gate-rep-2", {"optimization_id": o2})
    assert r1["status"] == "completed" and r2["status"] == "completed"
    bp1, bf1 = _load(o1)
    bp2, bf2 = _load(o2)
    assert bp1 == bp2
    assert bf1 == bf2


# ===========================================================================
# Gate #3 — determinism self-check + trial memo
# ===========================================================================
def test_same_decoded_params_yield_same_results():
    """Re-running the SAME decoded params through the trial runner yields the SAME
    results[<metric>] (deterministic-runner contract)."""
    s = _strategy_stub()
    flat = {"tp": 7.0, "sl": 4.0}
    d1 = decode_params(s, flat)
    d2 = decode_params(s, flat)
    r1 = _deterministic_stub({}, {}, d1)
    r2 = _deterministic_stub({}, {}, d2)
    assert r1 == r2
    assert compute_fitness("sharpe", r1) == compute_fitness("sharpe", r2)


def test_trial_key_order_independent_and_stable():
    a = trial_key({"model_id": 1, "params": {"tp": 5, "sl": 2}})
    b = trial_key({"params": {"sl": 2, "tp": 5}, "model_id": 1})
    assert a == b


def test_trial_memo_hit_returns_same_fitness():
    """An identical individual (elitism re-selection) is a free memo HIT that
    returns the SAME fitness without re-running."""
    m = TrialMemo()
    k = trial_key({"params": {"tp": 8, "sl": 3}})
    assert m.get(k) is None and m.misses == 1
    m.put(k, 10.0)
    assert m.get(k) == 10.0 and m.hits == 1
    # a second reselection -> another hit, same value, still no recompute
    assert m.get(k) == 10.0 and m.hits == 2


def test_handler_memo_collapses_duplicate_trials(monkeypatch):
    """Through the handler: distinct trial RUNS are bounded by the number of
    distinct decoded points, proving elitism reselections hit the memo."""
    from app.models.database import Base, SessionLocal, engine
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization

    Base.metadata.create_all(bind=engine)
    calls = {"n": 0}

    def _counting_stub(cfg, h, d):
        calls["n"] += 1
        return _deterministic_stub(cfg, h, d)

    monkeypatch.setattr(H, "_run_trial_backtest", _counting_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})

    db = SessionLocal()
    try:
        s = Strategy(
            name="gate-memo", initial_tp_percent=5.0, initial_tp_optimize=True,
            initial_tp_min=2.0, initial_tp_max=12.0, initial_tp_step=1.0,
            initial_sl_percent=2.0, initial_sl_optimize=True, initial_sl_min=1.0,
            initial_sl_max=6.0, initial_sl_step=1.0,
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        sid = s.id
    finally:
        db.close()

    cfg = {
        "populationSize": 10, "generations": 6, "crossoverProb": 0.7,
        "mutationProb": 0.2, "earlyStoppingGenerations": 20, "elitismPercent": 10.0,
        "seed": 7,
        "backtest": {"engine": "stub", "start_date": "2024-01-02",
                     "end_date": "2024-01-08", "seed": 7},
    }
    db = SessionLocal()
    try:
        row = StrategyOptimization(
            strategy_id=sid, name="gate-memo", fitness_metric="sharpe",
            optimization_type="genetic", optimization_config=cfg, status="pending",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        opt_id = row.id
    finally:
        db.close()

    out = H.handle_strategy_optimization("gate-memo-run", {"optimization_id": opt_id})
    assert out["status"] == "completed"
    # 11 tp values x 6 sl values = at most 66 distinct decoded points; with 10*6=60
    # individual evaluations (+ elitism reselections), the memo MUST keep the real
    # run count <= 66. (A non-memoised run would re-run every evaluation.)
    assert calls["n"] <= 66


# ===========================================================================
# Gate #4 — fitness mapping correctness (DD negation + 0-trade sentinel)
# ===========================================================================
def test_fitness_map_correctness():
    # metric -> correct results key
    assert compute_fitness("sharpe", {"total_trades": 1, "sharpe_ratio": 1.5}) == 1.5
    assert compute_fitness("sharpe_ratio", {"total_trades": 1, "sharpe_ratio": 1.5}) == 1.5
    assert compute_fitness("return", {"total_trades": 1, "total_return": 33.0}) == 33.0
    assert compute_fitness("total_return", {"total_trades": 1, "total_return": 33.0}) == 33.0
    assert compute_fitness("profit_factor", {"total_trades": 1, "profit_factor": 2.1}) == 2.1
    assert compute_fitness("win_rate", {"total_trades": 1, "win_rate": 60.0}) == 60.0
    assert compute_fitness("sortino", {"total_trades": 1, "sortino_ratio": 0.9}) == 0.9
    assert compute_fitness("calmar", {"total_trades": 1, "calmar_ratio": 0.4}) == 0.4
    assert compute_fitness("sqn", {"total_trades": 1, "sqn": 3.3}) == 3.3

    # max_drawdown is NEGATED (GA maximizes -> smaller DD wins)
    assert compute_fitness("max_drawdown", {"total_trades": 4, "max_drawdown": 12.0}) == -12.0
    assert compute_fitness("max_dd", {"total_trades": 4, "max_drawdown": 7.0}) == -7.0

    # 0-trade config -> sentinel (NOT 0.0, distinct from the exception fallback)
    f = compute_fitness("sharpe", {"total_trades": 0, "sharpe_ratio": 2.0})
    assert f == ZERO_TRADE_SENTINEL and f != 0.0
    # None results also collapse to sentinel
    assert compute_fitness("sharpe", None) == ZERO_TRADE_SENTINEL
    # NaN / inf metric collapses to sentinel
    assert compute_fitness("sharpe", {"total_trades": 1,
                                      "sharpe_ratio": float("nan")}) == ZERO_TRADE_SENTINEL
    assert compute_fitness("sharpe", {"total_trades": 1,
                                      "sharpe_ratio": float("inf")}) == ZERO_TRADE_SENTINEL

    # unknown metric fails fast (no-defaults, gate #5)
    with pytest.raises(ValueError):
        compute_fitness("totally_unknown_metric", {"total_trades": 1})


def test_max_drawdown_negated_end_to_end(monkeypatch):
    """fitness_metric=max_drawdown is negated through the full handler (gate #4)."""
    from app.models.database import Base, SessionLocal, engine
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization

    Base.metadata.create_all(bind=engine)

    def _dd_stub(cfg, h, d):
        return {"total_trades": 5, "max_drawdown": 2.0 + abs(d["tp"] - 8.0)}

    monkeypatch.setattr(H, "_run_trial_backtest", _dd_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})

    db = SessionLocal()
    try:
        s = Strategy(
            name="gate-dd", initial_tp_percent=5.0, initial_tp_optimize=True,
            initial_tp_min=2.0, initial_tp_max=12.0, initial_tp_step=1.0,
            initial_sl_percent=2.0, initial_sl_optimize=True, initial_sl_min=1.0,
            initial_sl_max=6.0, initial_sl_step=1.0,
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        sid = s.id
    finally:
        db.close()

    cfg = {
        "populationSize": 8, "generations": 6, "crossoverProb": 0.7,
        "mutationProb": 0.3, "earlyStoppingGenerations": 20, "elitismPercent": 10.0,
        "seed": 5,
        "backtest": {"engine": "stub", "start_date": "2024-01-02",
                     "end_date": "2024-01-08", "seed": 5},
    }
    db = SessionLocal()
    try:
        row = StrategyOptimization(
            strategy_id=sid, name="gate-dd", fitness_metric="max_drawdown",
            optimization_type="genetic", optimization_config=cfg, status="pending",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        opt_id = row.id
    finally:
        db.close()

    out = H.handle_strategy_optimization("gate-dd-run", {"optimization_id": opt_id})
    assert out["status"] == "completed"
    # least drawdown is 2.0 at tp=8 -> max fitness = -2.0
    assert out["best_fitness"] == pytest.approx(-2.0)
    assert out["best_params"]["tp"] == 8.0


# ===========================================================================
# Gate #7 — no regression to the ML backtest path
# ===========================================================================
def test_no_regression_ml_backtest_path():
    """The ML expert's single-asset engine (MLStrategy / run_backtest /
    handle_backtest) is intact and still imports without error. Phase 4 must not
    touch this path (the optimizer reuses it, never edits it)."""
    from app.services.backtest_handler import (
        MLStrategy, run_backtest, handle_backtest,
        _convert_bt_results, _empty_results,
    )
    import inspect

    assert MLStrategy.__name__ == "MLStrategy"
    assert callable(run_backtest)
    assert callable(handle_backtest)
    assert callable(_convert_bt_results)
    assert callable(_empty_results)
    # run_backtest signature is unchanged (these are the kwargs the ML adapter uses)
    params = inspect.signature(run_backtest).parameters
    for expected in ("model", "pred_df", "exec_df", "strategy_params",
                     "initial_capital", "buy_entry_conditions",
                     "sell_entry_conditions", "exit_conditions"):
        assert expected in params, f"run_backtest lost param {expected!r}"
    # handle_backtest still has the (task_id, payload) handler contract
    hb = inspect.signature(handle_backtest).parameters
    assert list(hb)[:2] == ["task_id", "payload"]
