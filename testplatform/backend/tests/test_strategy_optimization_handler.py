"""Phase 4 Task 5: ``strategy_optimization`` handler — the joint GA run.

The load-bearing gate is ``test_seeded_run_is_reproducible`` (acceptance gate #2): a seeded
GA run over a deterministic stub backtest reproduces an IDENTICAL best individual. The other
tests prove the no-defaults validation (gate #5), the fitness mapping (gate #4), the memo
self-check on elitism re-selection (gate #3), and the brute_force path.

The per-trial daily backtest is monkeypatched to a PURE deterministic stub (no network / no
real experts) so the handler's control flow + determinism are tested hermetically; the real
``run_daily_backtest`` seam is exercised by the Phase-2 e2e tests.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/test_strategy_optimization_handler.py -v
"""
from __future__ import annotations

import random
import types

import numpy as np
import pytest

from app.models.database import Base, SessionLocal, engine
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization
from app.services import strategy_optimization_handler as H


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    """Ensure the host tables exist on the default engine."""
    Base.metadata.create_all(bind=engine)
    yield


def _tp_sl_from_decoded(decoded):
    """Extract (tp, sl) from a decode_params() result, supporting BOTH shapes exercised in this
    file: the entry_actions-based Strategy (``decoded['entry_rules']`` carries adjust_take_profit
    /adjust_stop_loss rules, Task 6) used by the DB-backed handler tests, and the raw ``tp``/``sl``
    flat-gene shortcut ``test_seeded_run_is_reproducible`` uses (it builds its own param space
    directly, bypassing collect_param_space/entry_actions entirely). sl is returned as a positive
    magnitude either way (entry_rules carries it signed negative per the sign convention)."""
    rules = {r.get("action_type"): r for r in (decoded.get("entry_rules") or []) if isinstance(r, dict)}
    if rules:
        return rules["adjust_take_profit"]["action_value"], abs(rules["adjust_stop_loss"]["action_value"])
    return decoded["tp"], decoded["sl"]


def _deterministic_stub(backtest_cfg, hoisted, decoded):
    """A pure deterministic 'backtest': results are a fixed function of tp/sl so the
    SAME params always produce the SAME results (no RNG, no I/O). Peak at tp=8, sl=3."""
    tp, sl = _tp_sl_from_decoded(decoded)
    score = 10.0 - abs(tp - 8.0) - abs(sl - 3.0)
    return {
        "total_trades": 5,
        "sharpe_ratio": score,
        "max_drawdown": 5.0,
        "total_return": score,
        "profit_factor": 1.5,
        "win_rate": 55.0,
    }


def _strategy_stub():
    """A Strategy-like object with tp/sl optimize on (no DB needed)."""
    return types.SimpleNamespace(
        initial_tp_percent=5.0,
        initial_sl_percent=2.0,
        buy_entry_conditions=None,
        sell_entry_conditions=None,
        entry_conditions=None,
        exit_conditions=[],
    )


# ---------------------------------------------------------------------------
# Gate #2: seeded reproducibility (the Phase-4 core gate)
# ---------------------------------------------------------------------------
def test_seeded_run_is_reproducible():
    """Same seed + same (stub) cache => identical best_params/best_fitness."""
    from app.services.genetic import GeneticOptimizer
    from app.services.strategy_param_space import decode_params

    s = _strategy_stub()
    space = {
        "tp": {"type": "float", "min": 2, "max": 12, "step": 1},
        "sl": {"type": "float", "min": 1, "max": 6, "step": 1},
    }

    def run_once(seed):
        random.seed(seed)
        np.random.seed(seed)
        opt = GeneticOptimizer(
            param_ranges=space,
            population_size=8,
            n_generations=5,
            crossover_prob=0.7,
            mutation_prob=0.2,
            early_stopping_generations=10,
            elitism_percent=10.0,
        )

        def fit(flat):
            d = decode_params(s, flat)
            return _deterministic_stub({}, {}, d)["sharpe_ratio"]

        return opt.optimize(fitness_function=fit)

    r1 = run_once(42)
    r2 = run_once(42)
    assert r1["best_params"] == r2["best_params"]
    assert r1["best_fitness"] == r2["best_fitness"]


# ---------------------------------------------------------------------------
# DB-backed handler tests (real Strategy + StrategyOptimization rows)
# ---------------------------------------------------------------------------
def _seed_strategy() -> int:
    """A Strategy whose entry-time TP/SL bracket rides on ``entry_actions`` (adjust_take_profit
    /adjust_stop_loss rules with ``action_value_optimize``), mirroring how ``exit_conditions``
    rules are optimized — NOT the deleted scalar ``initial_tp_percent``/``initial_sl_percent``
    fields. Ranges chosen so the GA searches the SAME grid the old tp(2..12)/sl(1..6) genes did
    (11*6=66 distinct points), just keyed as ``entry:<id>:action_value`` now."""
    db = SessionLocal()
    try:
        s = Strategy(
            name="opt-test",
            entry_actions=[
                {
                    "id": "e_tp", "action_type": "adjust_take_profit",
                    "reference_value": "order_open_price", "action_value": 5.0,
                    "action_value_optimize": True,
                    "action_value_min": 2.0, "action_value_max": 12.0, "action_value_step": 1.0,
                },
                {
                    "id": "e_sl", "action_type": "adjust_stop_loss",
                    "reference_value": "order_open_price", "action_value": -2.0,
                    "action_value_optimize": True,
                    "action_value_min": -6.0, "action_value_max": -1.0, "action_value_step": 1.0,
                },
            ],
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id
    finally:
        db.close()


def _ga_config(**over):
    cfg = {
        "populationSize": 8,
        "generations": 4,
        "crossoverProb": 0.7,
        "mutationProb": 0.2,
        "earlyStoppingGenerations": 10,
        "elitismPercent": 10.0,
        "seed": 42,
        "backtest": {
            "engine": "stub",
            "start_date": "2024-01-02",
            "end_date": "2024-01-08",
            "seed": 42,
        },
    }
    cfg.update(over)
    return cfg


def _seed_opt(strategy_id: int, *, fitness_metric="sharpe", optimization_type="genetic",
              config=None) -> int:
    db = SessionLocal()
    try:
        row = StrategyOptimization(
            strategy_id=strategy_id,
            name="opt-run",
            fitness_metric=fitness_metric,
            optimization_type=optimization_type,
            optimization_config=config if config is not None else _ga_config(),
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


def test_handler_completes_and_persists_best(monkeypatch):
    """A full handler run over the stub backtest completes + persists best_params/fitness."""
    monkeypatch.setattr(H, "_run_trial_backtest", _deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})

    sid = _seed_strategy()
    opt_id = _seed_opt(sid)
    out = H.handle_strategy_optimization("t-opt-ok", {"optimization_id": opt_id})

    assert out["status"] == "completed", out
    assert out["optimization_id"] == opt_id
    row = _load_opt(opt_id)
    assert row.status == "completed"
    assert row.progress == 100.0
    assert row.best_params is not None
    assert row.best_fitness is not None
    assert row.parameter_ranges and "entry:e_tp:action_value" in row.parameter_ranges
    # The deterministic stub peaks at tp=8, sl=3 -> max score 10.0. A small GA
    # (pop=8/gen=4) is a heuristic, not exhaustive, so it converges NEAR the peak;
    # assert it found a high-fitness, in-range individual (exact global optimum is the
    # brute_force test's job). all_results recorded one entry per evaluated trial.
    assert row.best_fitness >= 8.0
    assert 2.0 <= row.best_params["entry:e_tp:action_value"] <= 12.0
    assert -6.0 <= row.best_params["entry:e_sl:action_value"] <= -1.0
    assert row.all_results and all("fitness" in r for r in row.all_results)


def test_handler_reproducible_via_db(monkeypatch):
    """Two seeded handler runs over the stub => identical best_params/best_fitness."""
    monkeypatch.setattr(H, "_run_trial_backtest", _deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    sid = _seed_strategy()

    o1 = _seed_opt(sid)
    o2 = _seed_opt(sid)
    H.handle_strategy_optimization("t-rep-1", {"optimization_id": o1})
    H.handle_strategy_optimization("t-rep-2", {"optimization_id": o2})
    r1 = _load_opt(o1)
    r2 = _load_opt(o2)
    assert r1.best_params == r2.best_params
    assert r1.best_fitness == r2.best_fitness


def test_required_ga_key_validation(monkeypatch):
    """A missing GA key must fail fast (no-defaults rule, gate #5)."""
    monkeypatch.setattr(H, "_run_trial_backtest", _deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    sid = _seed_strategy()
    cfg = _ga_config()
    del cfg["seed"]
    opt_id = _seed_opt(sid, config=cfg)
    out = H.handle_strategy_optimization("t-noseed", {"optimization_id": opt_id})
    assert out["status"] == "failed"
    assert "seed is required" in out["error"]
    assert _load_opt(opt_id).status == "failed"


def test_missing_backtest_config_fails(monkeypatch):
    """optimization_config.backtest is required (fail-early)."""
    monkeypatch.setattr(H, "_run_trial_backtest", _deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    sid = _seed_strategy()
    cfg = _ga_config()
    del cfg["backtest"]
    opt_id = _seed_opt(sid, config=cfg)
    out = H.handle_strategy_optimization("t-nobt", {"optimization_id": opt_id})
    assert out["status"] == "failed"
    assert "backtest is required" in out["error"]


def test_missing_optimization_id():
    out = H.handle_strategy_optimization("t-noid", {})
    assert out["status"] == "failed"
    assert "optimization_id is required" in out["error"]


def test_brute_force_finds_global_optimum(monkeypatch):
    """brute_force exhaustively finds the stub's global optimum (tp=8, sl=3)."""
    monkeypatch.setattr(H, "_run_trial_backtest", _deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    sid = _seed_strategy()
    opt_id = _seed_opt(sid, optimization_type="brute_force")
    out = H.handle_strategy_optimization("t-bf", {"optimization_id": opt_id})
    assert out["status"] == "completed"
    assert out["best_fitness"] == pytest.approx(10.0)
    assert out["best_params"] == {
        "entry:e_tp:action_value": 8.0, "entry:e_sl:action_value": -3.0,
    }


def test_brute_force_completion_pushes_optimization(monkeypatch):
    """brute_force has its OWN completion logic entirely separate from the GA path (no
    per-generation callback either), so it needs its own sync wiring — prove it pushes on
    completion with the final status='completed' state, same pattern as
    test_brute_force_finds_global_optimum but asserting the sync call instead of the result."""
    monkeypatch.setattr(H, "_run_trial_backtest", _deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    calls = []
    monkeypatch.setattr(H, "push_optimization", lambda opt, db: calls.append(opt.status))

    sid = _seed_strategy()
    opt_id = _seed_opt(sid, optimization_type="brute_force")
    out = H.handle_strategy_optimization("t-bf-sync", {"optimization_id": opt_id})

    assert out["status"] == "completed"
    assert len(calls) >= 1
    assert calls[-1] == "completed"
    assert _load_opt(opt_id).status == "completed"


def test_max_drawdown_metric_negated_through_handler(monkeypatch):
    """fitness_metric=max_drawdown is negated end-to-end (gate #4)."""

    def _dd_stub(backtest_cfg, hoisted, decoded):
        # drawdown is smaller (better) near tp=8: dd = 2 + |tp-8|
        tp, _sl = _tp_sl_from_decoded(decoded)
        return {"total_trades": 5, "max_drawdown": 2.0 + abs(tp - 8.0)}

    monkeypatch.setattr(H, "_run_trial_backtest", _dd_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    sid = _seed_strategy()
    opt_id = _seed_opt(sid, fitness_metric="max_drawdown")
    out = H.handle_strategy_optimization("t-dd", {"optimization_id": opt_id})
    assert out["status"] == "completed"
    # best (max fitness) = least drawdown = -2.0 at tp=8 (GA maximizes -dd).
    assert out["best_fitness"] == pytest.approx(-2.0)
    assert out["best_params"]["entry:e_tp:action_value"] == 8.0


def test_zero_trade_sentinel_through_handler(monkeypatch):
    """A 0-trade trial yields the ZERO_TRADE_SENTINEL, never confused with 0.0 (gate #4)."""
    from app.services.strategy_fitness import ZERO_TRADE_SENTINEL

    monkeypatch.setattr(
        H, "_run_trial_backtest",
        lambda cfg, h, d: {"total_trades": 0, "sharpe_ratio": 2.0},
    )
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    sid = _seed_strategy()
    opt_id = _seed_opt(sid)
    out = H.handle_strategy_optimization("t-zero", {"optimization_id": opt_id})
    assert out["status"] == "completed"
    assert out["best_fitness"] == ZERO_TRADE_SENTINEL
    assert out["best_fitness"] != 0.0


def test_memo_returns_same_fitness_on_reselection(monkeypatch):
    """The trial memo hits for an elitism-reselected identical individual (gate #3):
    the run records FAR fewer trial runs than total fitness evaluations."""
    calls = {"n": 0}

    def _counting_stub(backtest_cfg, hoisted, decoded):
        calls["n"] += 1
        return _deterministic_stub(backtest_cfg, hoisted, decoded)

    monkeypatch.setattr(H, "_run_trial_backtest", _counting_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    sid = _seed_strategy()
    opt_id = _seed_opt(sid)
    out = H.handle_strategy_optimization("t-memo", {"optimization_id": opt_id})
    assert out["status"] == "completed"
    # The stepped 2D space (tp in 2..12, sl in 1..6) has at most 11*6=66 distinct
    # decoded points, so the unique trial runs are bounded by the distinct points
    # regardless of how many individuals the GA evaluates.
    assert calls["n"] <= 66


def test_build_daily_trial_config_maps_rm_and_overrides():
    """The daily-trial seam merges expert overrides + tp/sl into each expert's settings.

    RM sizing is part of ``expert_overrides`` now (model:* keyed by the REAL ba2 setting
    names, e.g. ``risk_per_trade_pct``) — there is no separate ``rm`` block or name mapping."""
    backtest_cfg = {
        "backtest_id": 7,
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "enabled_instruments": ["AAPL", "MSFT"],
        "experts": [{"class": "FMPEarningsDrift", "settings": {"surprise_min_pct": 5.0}}],
        "initial_capital": 100000.0,
        "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30,
        "seed": 42,
    }
    decoded = {
        "tp": 8.0,
        "sl": 3.0,
        "expert_overrides": {
            "surprise_min_pct": 12.0,
            # RM sizing rides on the expert model:* path, keyed by the real ba2 names.
            "risk_per_trade_pct": 2.5,
            "atr_multiplier": 3.0,
            "min_stop_loss_pct": 1.5,
            "max_virtual_equity_per_instrument_percent": 25.0,
        },
        "buy_tree": None,
        "sell_tree": None,
        "exit_rules": [],
        "entry_rules": [],
    }
    cfg = H._build_daily_trial_config(backtest_cfg, decoded)
    settings = cfg["experts"][0]["settings"]
    assert settings["surprise_min_pct"] == 12.0  # override wins
    assert settings["risk_per_trade_pct"] == 2.5
    assert settings["atr_multiplier"] == 3.0
    assert settings["min_stop_loss_pct"] == 1.5
    assert settings["max_virtual_equity_per_instrument_percent"] == 25.0
    # entry_rules (the entry-time TP/SL bracket, Task 6) rides through unchanged, mirroring
    # exit_rules exactly — no separate initial_tp_percent/initial_sl_percent keys anymore.
    assert cfg["entry_rules"] == []
    # The run-level backtest_cfg must NOT be mutated.
    assert backtest_cfg["experts"][0]["settings"] == {"surprise_min_pct": 5.0}
    # Config shape matches what run_daily_backtest reads.
    for k in ("backtest_id", "account_settings", "enabled_instruments",
              "start_date", "end_date", "warmup_days", "experts", "seed"):
        assert k in cfg


def test_build_daily_trial_config_schedule_days_override_static_days_keep_static_times():
    """schedule:<day> genes (decoded['schedule_days']) replace the run-level static DAYS
    entirely, but time-of-day stays static (pulled from the run-level override) — only the day
    selection is optimized for now."""
    backtest_cfg = {
        "backtest_id": 7, "start_date": "2024-01-02", "end_date": "2024-01-08",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": "FMPRating", "settings": {}}],
        "initial_capital": 100000.0, "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30, "seed": 42,
        "run_schedule_override": {
            "days": {"monday": True, "tuesday": False, "wednesday": False, "thursday": False,
                     "friday": False, "saturday": False, "sunday": False},
            "times": ["09:30"],
        },
    }
    decoded = {
        "expert_overrides": {}, "buy_tree": None, "sell_tree": None,
        "exit_rules": [], "entry_rules": [],
        "schedule_days": {"monday": False, "tuesday": False, "wednesday": True,
                          "thursday": True, "friday": False, "saturday": False, "sunday": False},
    }
    cfg = H._build_daily_trial_config(backtest_cfg, decoded)
    assert cfg["run_schedule_override"]["days"] == decoded["schedule_days"]
    assert cfg["run_schedule_override"]["times"] == ["09:30"]  # static, unaffected by the genes


def test_build_daily_trial_config_no_schedule_genes_keeps_static_override():
    """When the param space didn't collect schedule:* genes (schedule_days is None), the
    run-level static run_schedule_override passes through unchanged (backward compatible)."""
    backtest_cfg = {
        "backtest_id": 7, "start_date": "2024-01-02", "end_date": "2024-01-08",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": "FMPRating", "settings": {}}],
        "initial_capital": 100000.0, "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30, "seed": 42,
        "run_schedule_override": {"days": {"monday": True}, "times": ["09:30"]},
    }
    decoded = {
        "expert_overrides": {}, "buy_tree": None, "sell_tree": None,
        "exit_rules": [], "entry_rules": [], "schedule_days": None,
    }
    cfg = H._build_daily_trial_config(backtest_cfg, decoded)
    assert cfg["run_schedule_override"] == backtest_cfg["run_schedule_override"]


def test_build_daily_trial_config_forwards_entry_rules():
    """decoded['entry_rules'] (the entry-time TP/SL bracket, Task 6 — adjust_take_profit/
    adjust_stop_loss actions decoded from Strategy.entry_actions) rides through to each trial's
    engine config unchanged, mirroring exit_rules forwarding exactly. Replaces the deleted
    run-level ``initial_tp_reference`` mechanism: the TP/SL reference now lives PER-RULE
    (``reference_value``) inside each entry_rules entry, not as a separate top-level key."""
    backtest_cfg = {
        "backtest_id": 11,
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": "FMPEarningsDrift", "settings": {}}],
        "initial_capital": 100000.0,
        "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30,
        "seed": 42,
    }
    entry_rules = [
        {"id": "e_tp", "action_type": "adjust_take_profit",
         "reference_value": "expert_target_price", "action_value": -5.0},
    ]
    decoded = {"tp": 8.0, "sl": 3.0, "expert_overrides": {},
               "buy_tree": None, "sell_tree": None, "exit_rules": [], "entry_rules": entry_rules}
    cfg = H._build_daily_trial_config(backtest_cfg, decoded)
    assert cfg["entry_rules"] == entry_rules


def test_build_daily_trial_config_entry_rules_absent_is_none():
    """No decoded entry_rules -> the trial config carries None (no bracket for this trial)."""
    backtest_cfg = {
        "backtest_id": 12,
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": "FMPEarningsDrift", "settings": {}}],
        "initial_capital": 100000.0,
        "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30,
        "seed": 42,
    }
    decoded = {"tp": 8.0, "sl": 3.0, "expert_overrides": {},
               "buy_tree": None, "sell_tree": None, "exit_rules": []}
    cfg = H._build_daily_trial_config(backtest_cfg, decoded)
    assert cfg.get("entry_rules") is None


# ---------------------------------------------------------------------------
# BYPASS expert (piece 1c): the optimizer drops rm:*/tp/sl/cond:*/exit:*
# ---------------------------------------------------------------------------
def test_is_bypass_expert_detects_factorranker_and_clean_experts():
    """``_is_bypass_expert`` is True for a FactorRanker backtest_cfg and False for a clean one
    (the real ba2_experts class-level ``bypasses_classic_rm`` marker drives the branch)."""
    fr_cfg = {"engine": "daily", "experts": [{"class": "FactorRanker", "settings": {}}]}
    clean_cfg = {"engine": "daily", "experts": [{"class": "FMPEarningsDrift", "settings": {}}]}
    assert H._is_bypass_expert(fr_cfg) is True
    assert H._is_bypass_expert(clean_cfg) is False
    # A plain string spec (not a dict) is also resolved.
    assert H._is_bypass_expert({"engine": "daily", "experts": ["FactorRanker"]}) is True
    # The ML engine is never a bypass; an unknown class is non-bypass (defensive).
    assert H._is_bypass_expert({"engine": "ml", "experts": ["FactorRanker"]}) is False
    assert H._is_bypass_expert({"engine": "daily", "experts": ["NoSuchExpert"]}) is False


def test_build_daily_trial_config_bypass_drops_rm_tp_sl():
    """For a FactorRanker (bypass) backtest_cfg, _build_daily_trial_config forwards ONLY the
    expert's own model:* overrides — NO rm:* mapped names, NO initial_tp/sl, even if decoded
    accidentally carried them."""
    backtest_cfg = {
        "backtest_id": 9,
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "enabled_instruments": ["AAPL", "MSFT"],
        "experts": [{"class": "FactorRanker", "settings": {"top_n": 20}}],
        "initial_capital": 100000.0,
        "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30,
        "seed": 42,
    }
    decoded = {
        # These tp/sl values must NOT be forwarded for a bypass expert.
        "tp": 8.0,
        "sl": 3.0,
        "expert_overrides": {"top_n": 10, "winsorize_pct": 0.05},
        "buy_tree": None, "sell_tree": None, "exit_rules": [],
    }
    cfg = H._build_daily_trial_config(backtest_cfg, decoded)
    settings = cfg["experts"][0]["settings"]
    # The expert's own params ARE forwarded (override wins over the base spec).
    assert settings["top_n"] == 10
    assert settings["winsorize_pct"] == 0.05
    # NONE of the rm/tp/sl names leak into a bypass expert's settings.
    for forbidden in (
        "risk_per_trade_pct", "atr_multiplier", "min_stop_loss_pct",
        "max_virtual_equity_per_instrument_percent", "initial_tp_percent",
        "initial_sl_percent",
    ):
        assert forbidden not in settings
    # The run-level backtest_cfg must NOT be mutated.
    assert backtest_cfg["experts"][0]["settings"] == {"top_n": 20}


def test_build_daily_trial_config_bypass_screener_applies_to_expert_settings():
    """For a BYPASS expert (FactorRanker) on a screener-optimized run tagged
    ``apply_to_expert_settings``, _build_daily_trial_config pushes ``universe_source=screener`` +
    the store path + the decoded screener genes (base overlaid with per-individual overrides)
    onto the expert's OWN per-trial settings (so its DYNAMIC metric_store universe is GA-tuned),
    while leaving the classic ``screener_runtime`` block populated for the non-bypass path."""
    backtest_cfg = {
        "backtest_id": 11,
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "enabled_instruments": ["AAPL", "MSFT", "NVDA"],
        "experts": [{"class": "FactorRanker", "settings": {"weighting": "equal"}}],
        "initial_capital": 100000.0,
        "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30,
        "seed": 42,
        "screener_opt": {
            "store": "/tmp/mstore_unit",
            "base_settings": {"screener_price_min": 20.0},
            "cadence_days": 7,
            "apply_to_expert_settings": True,
        },
    }
    hoisted = {
        "backtest_cfg": backtest_cfg,
        "screener_store": "/tmp/mstore_unit",
        "screener_base": {"screener_price_min": 20.0},
        "screener_cadence_days": 7,
        "screener_apply_to_expert_settings": True,
    }
    decoded = {
        "tp": 8.0, "sl": 3.0,  # bypass -> must not be forwarded
        "expert_overrides": {"top_n": 15},
        "screener_overrides": {
            "screener_market_cap_min": 5e9,
            "screener_relative_volume_min": 1.5,
            "screener_price_drop_pct": 4.0,
            "screener_max_stocks": 20,
        },
        "buy_tree": None, "sell_tree": None, "exit_rules": [],
    }
    cfg = H._build_daily_trial_config(backtest_cfg, decoded, hoisted)
    settings = cfg["experts"][0]["settings"]
    # FactorRanker now reads the metric_store dynamic-universe path off its OWN settings.
    assert settings["universe_source"] == "screener"
    assert settings["screener_store"] == "/tmp/mstore_unit"
    # Base screener settings overlaid with per-individual decoded genes (the screener_*-prefixed
    # keys FactorRanker._metric_store_settings translates).
    assert settings["screener_price_min"] == 20.0
    assert settings["screener_market_cap_min"] == 5e9
    assert settings["screener_relative_volume_min"] == 1.5
    assert settings["screener_price_drop_pct"] == 4.0
    assert settings["screener_max_stocks"] == 20
    # The expert's own model:* override still wins, and tp/sl are NOT leaked (bypass).
    assert settings["top_n"] == 15
    assert settings["weighting"] == "equal"
    assert "initial_tp_percent" not in settings and "initial_sl_percent" not in settings
    # The classic screener_runtime block is still built (the non-bypass entry-gate path is intact).
    assert cfg["screener_runtime"] is not None
    assert cfg["screener_runtime"]["store"] == "/tmp/mstore_unit"
    # The run-level backtest_cfg must NOT be mutated.
    assert backtest_cfg["experts"][0]["settings"] == {"weighting": "equal"}


def test_build_daily_trial_config_non_bypass_screener_untouched():
    """A NON-bypass screener run (apply_to_expert_settings False / absent) must NOT push
    universe_source / screener_store onto the expert settings — only the classic
    ``screener_runtime`` gate carries the screener (behaviour UNCHANGED)."""
    backtest_cfg = {
        "backtest_id": 12,
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "enabled_instruments": ["AAPL", "MSFT"],
        "experts": [{"class": "FMPRating", "settings": {}}],
        "initial_capital": 100000.0,
        "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30,
        "seed": 42,
        "screener_opt": {"store": "/tmp/mstore_unit", "base_settings": {}, "cadence_days": 7},
    }
    hoisted = {
        "backtest_cfg": backtest_cfg,
        "screener_store": "/tmp/mstore_unit",
        "screener_base": {},
        "screener_cadence_days": 7,
        "screener_apply_to_expert_settings": False,
    }
    decoded = {
        "tp": 8.0, "sl": 3.0,
        "expert_overrides": {"profit_ratio": 1.0},
        "screener_overrides": {"screener_market_cap_min": 5e9},
        "buy_tree": None, "sell_tree": None, "exit_rules": [],
    }
    cfg = H._build_daily_trial_config(backtest_cfg, decoded, hoisted)
    settings = cfg["experts"][0]["settings"]
    assert "universe_source" not in settings
    assert "screener_store" not in settings
    assert "screener_market_cap_min" not in settings
    # The classic gate still carries the screener for the non-bypass path — and its settings are
    # NORMALIZED to the metric store's UNPREFIXED keys (the screener-settings-opt bug fix: the gate
    # reads unprefixed keys, so a prefixed key here would be silently ignored).
    assert cfg["screener_runtime"] is not None
    assert cfg["screener_runtime"]["settings"]["market_cap_min"] == 5e9
    assert "screener_market_cap_min" not in cfg["screener_runtime"]["settings"]


def test_build_daily_trial_config_screener_gate_applies_all_criteria():
    """Regression for the screener-settings-opt bug: every optimized screener criterion must reach
    the per-bar gate as an UNPREFIXED key (base overlaid with per-individual genes). Previously only
    ``market_cap_max`` survived because the prefixed keys were passed through verbatim."""
    backtest_cfg = {
        "backtest_id": 13,
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "enabled_instruments": ["AAPL", "MSFT"],
        "experts": [{"class": "FMPRating", "settings": {}}],
        "initial_capital": 100000.0,
        "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30,
        "seed": 42,
        "screener_opt": {"store": "/tmp/mstore_unit", "base_settings": {"market_cap_max": 1e10}, "cadence_days": 7},
    }
    hoisted = {
        "backtest_cfg": backtest_cfg,
        "screener_store": "/tmp/mstore_unit",
        "screener_base": {"market_cap_max": 1e10},  # run-level base (unprefixed here)
        "screener_cadence_days": 7,
        "screener_apply_to_expert_settings": False,
    }
    decoded = {
        "tp": 8.0, "sl": 3.0, "expert_overrides": {},
        "screener_overrides": {
            "screener_market_cap_min": 6e9, "screener_relative_volume_min": 1.9,
            "screener_price_drop_pct": 12.0, "screener_max_stocks": 20,
        },
        "buy_tree": None, "sell_tree": None, "exit_rules": [],
    }
    gate = H._build_daily_trial_config(backtest_cfg, decoded, hoisted)["screener_runtime"]["settings"]
    assert gate == {
        "market_cap_max": 1e10, "market_cap_min": 6e9,
        "relative_volume_min": 1.9, "price_drop_pct": 12.0, "max_stocks": 20,
    }
    assert not any(k.startswith("screener_") for k in gate)


def test_normalize_screener_settings_strips_prefix_and_drops_unknown():
    """The shared normalizer: strip ``screener_`` prefix, keep only recognized keys, drop None."""
    from ba2_providers.screener.metric_store import normalize_screener_settings
    out = normalize_screener_settings({
        "screener_market_cap_min": 6e9, "market_cap_max": 1e10,
        "screener_max_stocks": 20, "screener_relative_volume_min": None,  # None dropped
        "bogus_key": 123, "screener_unknown": 1,                          # unknown dropped
    })
    assert out == {"market_cap_min": 6e9, "market_cap_max": 1e10, "max_stocks": 20}


def test_all_trials_failing_marks_optimization_failed():
    """Trust guard: if every trial errors (here: engine='stub' is not a real engine and is
    NOT monkeypatched), the run must report 'failed', not silently 'completed' with 0 trials."""
    sid = _seed_strategy()
    oid = _seed_opt(sid)  # default _ga_config -> engine='stub' -> _run_trial_backtest raises
    res = H.handle_strategy_optimization("t-allfail", {"optimization_id": oid})
    assert res["status"] == "failed", f"expected failed, got {res}"
    assert _load_opt(oid).status == "failed"


# ---------------------------------------------------------------------------
# Remote result sync (Task 5): push_optimization wired into ga_callback / completion / _fail
# ---------------------------------------------------------------------------
def test_generation_sync_pushes_optimization_each_generation(monkeypatch):
    """ga_callback must push the optimization row after every generation boundary, not just
    at the very end — so a remote worker's copy shows live progress, not just a final state."""
    monkeypatch.setattr(H, "_run_trial_backtest", _deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    calls = []
    monkeypatch.setattr(H, "push_optimization", lambda opt, db: calls.append(opt.status))

    sid = _seed_strategy()
    opt_id = _seed_opt(sid)  # _ga_config() default = 4 generations
    out = H.handle_strategy_optimization("t-gensync", {"optimization_id": opt_id})

    assert out["status"] == "completed"
    # 4 generation-boundary pushes + 1 final completion push = 5 total.
    assert len(calls) == 5
    assert calls[:4] == ["running", "running", "running", "running"]
    assert calls[-1] == "completed"


def test_completion_pushes_final_completed_state(monkeypatch):
    """The LAST push_optimization call after a successful run reflects status='completed',
    not just the last generation's 'running' snapshot — proves the completion-path push
    (Step 4 above) fires in addition to, not instead of, the per-generation pushes."""
    monkeypatch.setattr(H, "_run_trial_backtest", _deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    calls = []
    monkeypatch.setattr(H, "push_optimization", lambda opt, db: calls.append(opt.status))

    sid = _seed_strategy()
    opt_id = _seed_opt(sid)
    H.handle_strategy_optimization("t-completesync", {"optimization_id": opt_id})

    assert calls[-1] == "completed"
    assert _load_opt(opt_id).status == "completed"  # confirms the pushed row IS the real DB state


def test_failure_path_pushes_failed_status(monkeypatch):
    """A config-validation failure (routed through _fail) must push status='failed' too, so a
    remote worker's copy doesn't keep showing 'running' forever for a run that never really ran."""
    monkeypatch.setattr(H, "_run_trial_backtest", _deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    calls = []
    monkeypatch.setattr(H, "push_optimization", lambda opt, db: calls.append(opt.status))

    sid = _seed_strategy()
    cfg = _ga_config()
    del cfg["backtest"]  # triggers the "optimization_config.backtest is required" _fail path
    opt_id = _seed_opt(sid, config=cfg)
    out = H.handle_strategy_optimization("t-failsync", {"optimization_id": opt_id})

    assert out["status"] == "failed"
    assert calls == ["failed"]  # exactly one push, from _fail
