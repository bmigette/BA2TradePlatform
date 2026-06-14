# Backtest Platform — Phase 4 (Joint Genetic Optimizer) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand BA2TestPlatform's **existing** DEAP `GeneticOptimizer` into **one joint search** over `[expert params + classic-RM params + enter/exit ruleset/TradeConditions params]`. Each trial sets these params *from outside*, runs the **deterministic Phase-2 daily backtest**, and scores **one fitness metric already in the `Backtest` model**. Reuse the existing optimizer, `Backtest`/metrics models, task queue, and UI — **build no new optimizer**. Preserve determinism: same cache + same decoded params ⇒ identical result; a seeded GA run reproduces an identical best individual.

**Architecture:** The expert never owns parameter ranges (design §2, §5). The optimizer owns one **flat** `param_ranges` dict in the existing `GeneticOptimizer` shape `{name: {'type','min','max','step'}}`. An individual is a flat gene list ordered by `list(param_ranges.keys())`; `decode_individual` already quantizes to step. A new `strategy_param_space` module **collects/flattens** the joint space from a `Strategy` row (+ expert + RM config) and **decodes** a flat params dict back into `(tp, sl, rm_dict, expert_overrides, buy_tree, sell_tree, exit_rules)` via deep-copied condition-tree substitution. A new `strategy_optimization_handler` registers a `strategy_optimization` task type, wraps `GeneticOptimizer.optimize(...)` with a fitness function that calls the Phase-2 deterministic backtest runner and reads `results[<fitness_metric>]`. Three determinism levers: seed `random`+`np.random` from `optimization_config.seed`; **hoist the param-independent `_gather` pass** out of the per-trial loop (computed once per run, reused every individual); a **trial memo-cache** keyed by a content hash so elitism re-selections hit the memo.

**Tech Stack:** Python ≥3.11 (BA2TestPlatform `backend/venv`), DEAP (existing), SQLAlchemy declarative `Base` (NOT SQLModel — BA2TestPlatform convention), FastAPI, pytest. Custom multi-asset daily engine + `BacktestAccount` from **Phase 2** are the per-trial evaluator. No third-party optimizer (vectorbt noted as a *future* accelerator only, design §5).

---

## Source of truth & repo locations

- **Optimizer host (where all code lands):** `BA2TestPlatform/backend/` — branch `main`. Use `./venv/bin/python` for everything (per `backend/CLAUDE.md`); never system Python.
- **Reused existing files (read-only except where a step edits them):**
  - `app/services/genetic.py` — `GeneticOptimizer` (DEAP). Reuse AS-IS for the optimize loop, elitism, early-stop, checkpoint/resume. The ONLY edit here is extending `get_checkpoint_data`/`resume_from_checkpoint` to also persist `np.random` state (Task 4).
  - `app/services/job_handler.py` — the proven GA wiring template (`required_ga_keys` validation @1795-1806, `fitness_function`/`on_generation_start`/`ga_callback` @1860-1999, `save_ga_checkpoint`/`load_ga_checkpoint` @490-531). Mirror this structure in the new handler; do not modify it.
  - `app/services/backtest_handler.py` — `handle_backtest` (@857) and `run_backtest` (@282); `MLStrategy` (@33) stays the **ML expert's** single-asset engine UNCHANGED; `_convert_bt_results`/`_safe_float`/`_safe_duration_days`/`_empty_results` define the results-dict shape the fitness reads.
  - `app/services/strategy_executor.py` — `evaluate_condition_tree` (@263), `evaluate_condition` (@194), `evaluate_comparison` (@160), `ConfirmationTracker` (@80). Tree-walk template = `traverse_conditions` (defined in `app/api/strategies.py:117` inside `extract_required_fields`).
  - `app/models/strategy.py` — `Strategy` (TP/SL `*_optimize/_min/_max/_step` columns @31-41). **Add classic-RM optimize columns here (Task 2).**
  - `app/models/strategy_optimization.py` — `StrategyOptimization` (`parameter_ranges`/`best_params`/`best_fitness`/`all_results`/`fitness_metric`/`optimization_type`/`optimization_config`/`progress`/`status` already present). Schema is sufficient; **no new columns needed there.**
  - `app/models/backtest.py` — `Backtest` (fitness metric columns: `sharpe_ratio`, `total_return`, `profit_factor`, `win_rate`, `max_drawdown`, …). The fitness map reads `results[<key>]`.
  - `app/api/strategies.py` — router (mounted at `/api/strategies`, main.py:345). **Add the POST `/optimize` route here (Task 6).**
  - `app/main.py` — lifespan registers handlers @255-257 (`register_handler('backtest', handle_backtest)`); routers included @332-345. **Register `strategy_optimization` (Task 6).**
  - `app/services/task_queue.py` — `register_handler` (@69), `update_progress` (@317), `is_task_paused` (@299), handler contract `handler(task_id, payload)->dict` (@485); `checkpoint_data` lives on the `TaskQueue` model. `max_workers=1` (main.py:247).
- **New files (created by this plan):** `app/services/strategy_param_space.py`, `app/services/strategy_optimization_handler.py`, a RM-columns alembic-style migration (or `create_all` for sqlite dev), and tests under `backend/tests/` (or `backend/scripts/` per house style — see Task 7).
- Derived from `docs/plans/2026-06-13-backtest-platform-design.md` (§5, §6 Phase 4) and the SHARED CONTRACTS `optimizer` block.

> **Re-plan checkpoint — Phase-2 trial-run API dependency.** This plan's fitness function calls the **deterministic Phase-2 daily-engine runner**. The design's phase ordering is `0→1→2→3→4`: **Phase 2 builds `BacktestAccount` + the custom daily multi-asset engine** (SHARED CONTRACTS `engine_loop`/`backtest_account`; the `per_phase_scope.phase_4` entry in the contract is the SAME engine work — phase numbers are off-by-the-design-doc; in the design §6 the engine is Phase 2 and the optimizer is Phase 4). At execution time, **confirm the exact Phase-2 trial-run entry point** before wiring Task 5. The contract names a daily-engine module registered as the `daily_backtest` task handler with `handle_daily_backtest(task_id, payload)->result dict`. The optimizer must call the **synchronous in-process runner** under that handler, NOT enqueue a sub-task (the GA evaluates hundreds of individuals; `max_workers=1` would deadlock). Confirm these at execution: (a) the function name + signature of the in-process daily runner (analogue of `run_backtest`), e.g. `run_daily_backtest(config, hoisted_gather, decoded_params) -> results_dict`; (b) that it returns the SAME results-dict keys as `_convert_bt_results` (so the fitness map below is valid); (c) where the hoisted `_gather` pass lives and how decoded `(tp, sl, rm_dict, expert_overrides, trees)` are injected per trial; (d) that the runner is deterministic given fixed cache + params (no RNG, fixed-cache predictions). If Phase 2 exposes only the async `handle_daily_backtest`, the FIRST execution step of Task 5 is to extract a pure synchronous `run_daily_backtest(...)` from it (mirror how `handle_backtest` delegates to `run_backtest`). **Until Phase 2 lands, Tasks 1-4 + 6-7 are fully implementable against the contract; Task 5 has a typed seam (`_run_trial_backtest`) with a documented adapter to be finalized when the Phase-2 API is known.**

## Decisions taken (confirm before execution)

These resolve forks the recon surfaced. Override any at approval time.

1. **One joint genetic run (no separate passes).** A single `GeneticOptimizer` tunes expert + RM + ruleset/condition params together (design §5). *Alternative rejected:* nested/sequential optimization (slower, no joint optima).
2. **Reuse `GeneticOptimizer` unmodified except the np.random checkpoint extension.** All new logic is the param-space module + the handler + the RM columns. The optimizer stays a fitness-agnostic black box (contract `optimizer.reuse`).
3. **Expert params: optimized jointly (model:<p> ranges INCLUDED) by default; the trained ML model is treated as FIXED.** Open-question resolution: for the ML expert the model weights are frozen (no retrain per trial); only the expert's numeric *decision* settings (thresholds) are searched. For ba2 experts, all per-expert numeric settings are searchable. `model:<p>` ranges are emitted only when the strategy config marks an expert numeric setting `optimize=True`. *Alternative:* freeze all expert params and search only RM+ruleset (smaller space; loses signal tuning). **Confirm at approval.**
4. **Classic RM params are NEW and must be added to schema + enforced** (contract `optimizer.classic_rm_params`): `risk_per_trade_pct`, `per_instrument_cap_pct`, `min_stop_pct`, `atr_stop_mult`, `max_concurrent_positions`. Each gets `*_optimize/_min/_max/_step` columns on `Strategy` (mirror the TP/SL pattern). Enforced in `ba2_common.core.position_sizing` + classic RM. **No smart RM (YAGNI).**
5. **Diversification RM is in-scope for the NEW multi-asset daily engine only.** `per_instrument_cap_pct` + `max_concurrent_positions` are enforceable in the Phase-2 multi-symbol engine; under the legacy `MLStrategy` single-asset path (`exclusive_orders=True`) they degrade to a re-entry cap (contract `optimizer.known_constraint`). The handler routes ba2-expert strategies to the daily engine, ML-expert strategies to `run_backtest`.
6. **Fitness from the BACKTEST results dict, not `metrics.py`** (contract `optimizer.fitness`). `metrics.py` is classification-only. Map `StrategyOptimization.fitness_metric -> results` key; `max_drawdown` is NEGATED (GA is maximize-only). 0-trade configs return a **sentinel large-negative** fitness DISTINCT from the 0.0 exception fallback at `genetic.py:357`.
7. **No-defaults rule (house style, `backend/CLAUDE.md`).** Every GA/range/metric/RM/seed config value is explicitly provided and validated fail-early (mirror `required_ga_keys`). No `.get(key, default)` for required config.
8. **Determinism via seeding + hoisting + memo** (contract `optimizer.determinism_rule`). Seed `random` AND `np.random` from `optimization_config.seed`; hoist the param-independent `_gather` pass; memo-cache keyed by content hash so elitism re-selections are free and self-checking.

## Acceptance gate for Phase 4

The phase passes when ALL hold (verified by the commands in Task 7/8):

1. **Joint space is real:** `collect_param_space(strategy, expert_cfg, rm_cfg)` emits one flat dict containing namespaced keys for every `optimize=True` field across all three families (`model:`/`rm:`/`tp`/`sl`/`cond:<id>:value`/`cond:<id>:confirmation_bars`/`exit:<id>:action_value`), and `decode_params(strategy, flat_params)` reconstructs deep-copied trees + `(tp, sl, rm_dict, expert_overrides, buy_tree, sell_tree, exit_rules)` with values substituted **by id**, leaving the source `Strategy` untouched.
2. **Reproducibility (the core gate):** a seeded GA run over a clean expert is reproducible — `optimization_config.seed=S` twice ⇒ **identical `best_params` and `best_fitness`** (byte-equal decoded best individual). Proven with a deterministic stub backtest (Task 7) and, once Phase 2 lands, with the real engine.
3. **Determinism self-check:** re-running the SAME decoded params through the trial runner yields the SAME `results[<metric>]`; the memo-cache hit for an elitism-reselected identical individual returns the SAME fitness without re-running.
4. **Fitness mapping correct:** each `fitness_metric` maps to the right `results` key; `max_drawdown` is negated; a 0-trade trial returns the sentinel (not 0.0), distinct from the exception fallback.
5. **No-defaults validation:** missing any required GA/metric/RM/seed key fails the trial fast with an explicit `error` string (mirrors `genetic_config.{key} is required`).
6. **Wiring:** `strategy_optimization` is registered in `main.py` lifespan; `POST /api/strategies/{id}/optimize` writes a `StrategyOptimization` row and enqueues the task; on completion `best_params`/`best_fitness`/`all_results` are persisted.
7. **No regression:** the ML `MLStrategy`/`run_backtest` path and `handle_backtest` are unchanged; existing backtests still run.

---

## Task 1: `strategy_param_space` — collect/flatten the joint search space

**Files (create):** `app/services/strategy_param_space.py`. **Tests:** `backend/tests/test_strategy_param_space_collect.py`.

The joint space is ONE flat dict in the `GeneticOptimizer` shape. Namespacing (contract `optimizer.joint_param_space_schema.namespacing`): `model:<p>` | `rm:<p>` | `tp` | `sl` | `cond:<condition_id>:value` | `cond:<condition_id>:confirmation_bars` | `exit:<condition_id>:action_value`. Tree-walk template = `traverse_conditions` (api/strategies.py:117).

- [ ] **Step 1: Module skeleton + the four collectors**

Create `app/services/strategy_param_space.py`:

```python
"""Joint optimization parameter space for strategy/expert/RM optimization.

Collects ONE flat param_ranges dict (the GeneticOptimizer shape
{name: {'type','min','max','step'}}) from a Strategy row + expert numeric
settings + classic-RM config, and decodes a flat decoded-params dict back into
(tp, sl, rm_dict, expert_overrides, buy_tree, sell_tree, exit_rules) by
deep-copying the condition trees and substituting node value/confirmation_bars/
action_value by id. The Strategy row is never mutated.

Namespacing (design §5):
  model:<p>                       expert numeric decision settings
  rm:<p>                          classic-RM params
  tp | sl                         initial TP/SL percent
  cond:<id>:value                 a buy/sell condition node's threshold
  cond:<id>:confirmation_bars     that node's confirmation bars
  exit:<id>:action_value          an exit rule's action value
"""
import copy
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Classic-RM params (design §5). Each is namespaced rm:<name>.
CLASSIC_RM_PARAMS = (
    "risk_per_trade_pct",
    "per_instrument_cap_pct",
    "min_stop_pct",
    "atr_stop_mult",
    "max_concurrent_positions",
)


def _range_entry(min_v, max_v, step_v, is_int: bool) -> Dict[str, Any]:
    """Build one GeneticOptimizer range entry; fail-early on missing bounds."""
    if min_v is None or max_v is None or step_v is None:
        raise ValueError(f"range requires min/max/step, got {min_v}/{max_v}/{step_v}")
    return {
        "type": "int" if is_int else "float",
        "min": int(min_v) if is_int else float(min_v),
        "max": int(max_v) if is_int else float(max_v),
        "step": int(step_v) if is_int else float(step_v),
    }


def _collect_tp_sl(strategy) -> Dict[str, Any]:
    """tp/sl ranges from Strategy.initial_{tp,sl}_{optimize,min,max,step}."""
    out: Dict[str, Any] = {}
    if getattr(strategy, "initial_tp_optimize", False):
        out["tp"] = _range_entry(strategy.initial_tp_min, strategy.initial_tp_max,
                                 strategy.initial_tp_step, is_int=False)
    if getattr(strategy, "initial_sl_optimize", False):
        out["sl"] = _range_entry(strategy.initial_sl_min, strategy.initial_sl_max,
                                 strategy.initial_sl_step, is_int=False)
    return out


def _collect_rm(rm_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """rm:<p> ranges from a classic-RM config dict.

    rm_cfg shape (per param): {name: {'optimize': bool, 'min','max','step',
    'type': 'int'|'float'}}. Only optimize=True params are emitted.
    """
    out: Dict[str, Any] = {}
    if not rm_cfg:
        return out
    for name in CLASSIC_RM_PARAMS:
        spec = rm_cfg.get(name)
        if spec and spec.get("optimize"):
            is_int = spec.get("type") == "int"
            out[f"rm:{name}"] = _range_entry(spec.get("min"), spec.get("max"),
                                             spec.get("step"), is_int=is_int)
    return out


def _collect_expert(expert_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """model:<p> ranges from per-expert numeric settings marked optimize=True.

    expert_cfg shape: {param_name: {'optimize': bool,'min','max','step','type'}}.
    For the ML expert this is typically empty (model frozen, decision thresholds
    live in the condition tree). For ba2 experts: EarningsDrift surprise_min_pct/
    max_days_since_report/expected_profit_percent; Insider lookback_days/
    min_insiders/min_total_value; Rating profit_ratio/min_analysts; FactorRanker
    factor weights/top_n/winsorize_pct.
    """
    out: Dict[str, Any] = {}
    if not expert_cfg:
        return out
    for name, spec in expert_cfg.items():
        if spec and spec.get("optimize"):
            is_int = spec.get("type") == "int"
            out[f"model:{name}"] = _range_entry(spec.get("min"), spec.get("max"),
                                                spec.get("step"), is_int=is_int)
    return out


def _walk_condition_nodes(cond: Optional[Dict[str, Any]], out: Dict[str, Any]) -> None:
    """Emit cond:<id>:value and cond:<id>:confirmation_bars for optimizable nodes.

    Mirrors api/strategies.py traverse_conditions: AND/OR nodes recurse via
    'conditions'; leaf nodes carry id + value + optimize flags.
    """
    if not isinstance(cond, dict):
        return
    # Recurse into AND/OR sub-trees
    for child in (cond.get("conditions") or []):
        _walk_condition_nodes(child, out)
    cid = cond.get("id")
    if not cid:
        return
    # value optimization
    if cond.get("optimize") or cond.get("optimize_enabled"):
        out[f"cond:{cid}:value"] = _range_entry(
            cond.get("value_min"), cond.get("value_max"), cond.get("value_step"),
            is_int=False,
        )
    # confirmation-bars optimization
    if cond.get("confirmation_bars_min") is not None:
        out[f"cond:{cid}:confirmation_bars"] = _range_entry(
            cond.get("confirmation_bars_min"), cond.get("confirmation_bars_max"),
            cond.get("confirmation_bars_step"), is_int=True,
        )


def _collect_conditions(strategy) -> Dict[str, Any]:
    """cond:<id>:* across buy + sell trees and exit:<id>:action_value across exits."""
    out: Dict[str, Any] = {}
    _walk_condition_nodes(strategy.buy_entry_conditions, out)
    _walk_condition_nodes(strategy.sell_entry_conditions, out)
    # legacy single entry tree (backwards compat)
    _walk_condition_nodes(getattr(strategy, "entry_conditions", None), out)
    for exit_rule in (strategy.exit_conditions or []):
        eid = exit_rule.get("id")
        if eid and exit_rule.get("action_value_optimize"):
            out[f"exit:{eid}:action_value"] = _range_entry(
                exit_rule.get("action_value_min"), exit_rule.get("action_value_max"),
                exit_rule.get("action_value_step"), is_int=False,
            )
        # exit rules may also carry an optimizable condition sub-tree
        _walk_condition_nodes(exit_rule.get("conditions"), out)
    return out


def collect_param_space(
    strategy,
    expert_cfg: Optional[Dict[str, Any]] = None,
    rm_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the flat joint param_ranges dict for GeneticOptimizer.

    Merges expert (model:*) + RM (rm:*) + tp/sl + condition (cond:*/exit:*) ranges.
    Key order is deterministic (model, rm, tp/sl, conditions) so the gene list is
    stable across runs — required for reproducibility.
    """
    space: Dict[str, Any] = {}
    space.update(_collect_expert(expert_cfg))
    space.update(_collect_rm(rm_cfg))
    space.update(_collect_tp_sl(strategy))
    space.update(_collect_conditions(strategy))
    if not space:
        raise ValueError(
            "No optimizable parameters found: mark at least one of expert/RM/"
            "TP/SL/condition fields optimize=True."
        )
    logger.info(f"Collected joint param space: {len(space)} params: {list(space.keys())}")
    return space
```

- [ ] **Step 2: Write failing collect tests**

`backend/tests/test_strategy_param_space_collect.py`:

```python
import types
from app.services.strategy_param_space import collect_param_space, CLASSIC_RM_PARAMS


def _strategy(**kw):
    """Minimal Strategy-like object with the columns collect_param_space reads."""
    base = dict(
        initial_tp_optimize=False, initial_tp_min=None, initial_tp_max=None, initial_tp_step=None,
        initial_sl_optimize=False, initial_sl_min=None, initial_sl_max=None, initial_sl_step=None,
        buy_entry_conditions=None, sell_entry_conditions=None, entry_conditions=None,
        exit_conditions=[],
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_collect_tp_sl_only_when_optimize():
    s = _strategy(initial_tp_optimize=True, initial_tp_min=2.0, initial_tp_max=10.0,
                  initial_tp_step=0.5)
    space = collect_param_space(s)
    assert space["tp"] == {"type": "float", "min": 2.0, "max": 10.0, "step": 0.5}
    assert "sl" not in space


def test_collect_rm_namespaced():
    s = _strategy(initial_tp_optimize=True, initial_tp_min=1, initial_tp_max=2, initial_tp_step=0.5)
    rm = {"risk_per_trade_pct": {"optimize": True, "min": 0.5, "max": 3.0, "step": 0.25, "type": "float"},
          "max_concurrent_positions": {"optimize": True, "min": 1, "max": 10, "step": 1, "type": "int"}}
    space = collect_param_space(s, rm_cfg=rm)
    assert space["rm:risk_per_trade_pct"]["type"] == "float"
    assert space["rm:max_concurrent_positions"] == {"type": "int", "min": 1, "max": 10, "step": 1}


def test_collect_expert_namespaced():
    s = _strategy(initial_sl_optimize=True, initial_sl_min=1, initial_sl_max=5, initial_sl_step=0.5)
    expert = {"surprise_min_pct": {"optimize": True, "min": 1.0, "max": 20.0, "step": 1.0, "type": "float"},
              "max_days_since_report": {"optimize": False, "min": 1, "max": 30, "step": 1, "type": "int"}}
    space = collect_param_space(s, expert_cfg=expert)
    assert "model:surprise_min_pct" in space
    assert "model:max_days_since_report" not in space  # optimize=False


def test_collect_condition_value_and_confirmation():
    buy = {"operator": "AND", "conditions": [
        {"id": "c1", "field": "model:probability", "comparison": ">=", "value": 0.6,
         "optimize": True, "value_min": 0.5, "value_max": 0.9, "value_step": 0.05,
         "confirmation_bars_min": 1, "confirmation_bars_max": 5, "confirmation_bars_step": 1},
    ]}
    s = _strategy(buy_entry_conditions=buy,
                  initial_tp_optimize=True, initial_tp_min=1, initial_tp_max=2, initial_tp_step=0.5)
    space = collect_param_space(s)
    assert space["cond:c1:value"] == {"type": "float", "min": 0.5, "max": 0.9, "step": 0.05}
    assert space["cond:c1:confirmation_bars"] == {"type": "int", "min": 1, "max": 5, "step": 1}


def test_collect_exit_action_value():
    s = _strategy(exit_conditions=[
        {"id": "e1", "action": "adjust_sl", "action_value": 1.0, "action_value_optimize": True,
         "action_value_min": 0.5, "action_value_max": 3.0, "action_value_step": 0.5,
         "conditions": {}},
    ])
    space = collect_param_space(s)
    assert space["exit:e1:action_value"]["min"] == 0.5


def test_empty_space_raises():
    import pytest
    with pytest.raises(ValueError):
        collect_param_space(_strategy())
```

- [ ] **Step 3: Run collect tests**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/test_strategy_param_space_collect.py -v
```
Expected: PASS. (If pytest collection is not configured for `tests/`, see Task 7 Step 1 — fall back to a `scripts/` harness with assertions and run it directly.)

- [ ] **Step 4: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add backend/app/services/strategy_param_space.py backend/tests/test_strategy_param_space_collect.py
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(optimizer): joint param-space collector (expert+RM+tp/sl+conditions)"
```

---

## Task 2: Classic-RM optimize columns on `Strategy` + migration

**Files:** edit `app/models/strategy.py`; create the RM-columns migration. **Tests:** `backend/tests/test_strategy_rm_columns.py`.

The five classic-RM params (Decision 4) need `value/optimize/min/max/step` columns mirroring the TP/SL pattern (`strategy.py:31-41`). These are the schema half of `_collect_rm`; the handler builds `rm_cfg` from them (Task 6).

- [ ] **Step 1: Add RM columns to the `Strategy` model**

Edit `app/models/strategy.py`, after the `initial_sl_*` block (line 41), add for each of `risk_per_trade_pct`, `per_instrument_cap_pct`, `min_stop_pct`, `atr_stop_mult`, `max_concurrent_positions` a column group. Example for one (replicate for all five; `max_concurrent_positions` is Integer):

```python
    # Classic Risk Manager params with optimization ranges (Phase 4 joint optimizer)
    rm_risk_per_trade_pct = Column(Float, default=1.0)
    rm_risk_per_trade_pct_optimize = Column(Boolean, default=False)
    rm_risk_per_trade_pct_min = Column(Float, nullable=True)
    rm_risk_per_trade_pct_max = Column(Float, nullable=True)
    rm_risk_per_trade_pct_step = Column(Float, nullable=True)

    rm_per_instrument_cap_pct = Column(Float, default=20.0)
    rm_per_instrument_cap_pct_optimize = Column(Boolean, default=False)
    rm_per_instrument_cap_pct_min = Column(Float, nullable=True)
    rm_per_instrument_cap_pct_max = Column(Float, nullable=True)
    rm_per_instrument_cap_pct_step = Column(Float, nullable=True)

    rm_min_stop_pct = Column(Float, default=2.0)
    rm_min_stop_pct_optimize = Column(Boolean, default=False)
    rm_min_stop_pct_min = Column(Float, nullable=True)
    rm_min_stop_pct_max = Column(Float, nullable=True)
    rm_min_stop_pct_step = Column(Float, nullable=True)

    rm_atr_stop_mult = Column(Float, default=2.0)
    rm_atr_stop_mult_optimize = Column(Boolean, default=False)
    rm_atr_stop_mult_min = Column(Float, nullable=True)
    rm_atr_stop_mult_max = Column(Float, nullable=True)
    rm_atr_stop_mult_step = Column(Float, nullable=True)

    rm_max_concurrent_positions = Column(Integer, default=5)
    rm_max_concurrent_positions_optimize = Column(Boolean, default=False)
    rm_max_concurrent_positions_min = Column(Integer, nullable=True)
    rm_max_concurrent_positions_max = Column(Integer, nullable=True)
    rm_max_concurrent_positions_step = Column(Integer, nullable=True)
```

Add the camelCase keys to `to_dict()` (mirror the `initialTp*` entries) so the UI can edit them. Keep the existing TP/SL block unchanged.

- [ ] **Step 2: Apply the schema change**

> Re-plan checkpoint: confirm BA2TestPlatform's migration mechanism. The recon shows plain SQLAlchemy declarative `Base` with `create_all` on startup (no alembic dir found under `backend/`). If alembic is absent, new columns land via `Base.metadata.create_all` — but SQLite `create_all` does NOT add columns to an existing table. For dev, either (a) drop/recreate the `strategies` table if empty, or (b) run an explicit `ALTER TABLE` once. Confirm and apply:

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
ls alembic* migrations 2>/dev/null || echo "no alembic — using ALTER TABLE / create_all"
# If no alembic, run the additive ALTERs (idempotent guard) — confirm DB path from app/models/database.py first:
./venv/bin/python - <<'PY'
from app.models.database import engine
from sqlalchemy import text
cols = [
 ("rm_risk_per_trade_pct","FLOAT"),("rm_risk_per_trade_pct_optimize","BOOLEAN"),
 ("rm_risk_per_trade_pct_min","FLOAT"),("rm_risk_per_trade_pct_max","FLOAT"),("rm_risk_per_trade_pct_step","FLOAT"),
 ("rm_per_instrument_cap_pct","FLOAT"),("rm_per_instrument_cap_pct_optimize","BOOLEAN"),
 ("rm_per_instrument_cap_pct_min","FLOAT"),("rm_per_instrument_cap_pct_max","FLOAT"),("rm_per_instrument_cap_pct_step","FLOAT"),
 ("rm_min_stop_pct","FLOAT"),("rm_min_stop_pct_optimize","BOOLEAN"),
 ("rm_min_stop_pct_min","FLOAT"),("rm_min_stop_pct_max","FLOAT"),("rm_min_stop_pct_step","FLOAT"),
 ("rm_atr_stop_mult","FLOAT"),("rm_atr_stop_mult_optimize","BOOLEAN"),
 ("rm_atr_stop_mult_min","FLOAT"),("rm_atr_stop_mult_max","FLOAT"),("rm_atr_stop_mult_step","FLOAT"),
 ("rm_max_concurrent_positions","INTEGER"),("rm_max_concurrent_positions_optimize","BOOLEAN"),
 ("rm_max_concurrent_positions_min","INTEGER"),("rm_max_concurrent_positions_max","INTEGER"),("rm_max_concurrent_positions_step","INTEGER"),
]
with engine.begin() as c:
    existing = {r[1] for r in c.execute(text("PRAGMA table_info(strategies)"))}
    for name, typ in cols:
        if name not in existing:
            c.execute(text(f"ALTER TABLE strategies ADD COLUMN {name} {typ}"))
            print("added", name)
print("done")
PY
```
> If alembic IS present, instead run `python -m alembic revision --autogenerate -m "strategy classic-RM optimize columns"` then `upgrade head`.

- [ ] **Step 3: Write + run the columns test**

`backend/tests/test_strategy_rm_columns.py`:

```python
from app.models.strategy import Strategy

def test_strategy_has_rm_columns():
    for p in ("risk_per_trade_pct","per_instrument_cap_pct","min_stop_pct",
              "atr_stop_mult","max_concurrent_positions"):
        for suffix in ("","_optimize","_min","_max","_step"):
            assert hasattr(Strategy, f"rm_{p}{suffix}"), f"missing rm_{p}{suffix}"
```
```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/test_strategy_rm_columns.py -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add backend/app/models/strategy.py backend/tests/test_strategy_rm_columns.py
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(optimizer): classic-RM optimize columns on Strategy + additive migration"
```

---

## Task 3: `decode_params` — flat individual → trees + tp/sl/rm/expert overrides

**Files:** extend `app/services/strategy_param_space.py`. **Tests:** `backend/tests/test_strategy_param_space_decode.py`.

`decode_individual` (genetic.py:181) already turns a gene list into a flat `{name: quantized_value}` dict. `decode_params` takes that flat dict + the source `Strategy` and produces the concrete trial config by **deep-copying** the trees and substituting node value/confirmation_bars/action_value **by id**, plus extracting `tp/sl/rm_dict/expert_overrides`. This is `apply_params_to_strategy(strategy, params)` from the contract.

- [ ] **Step 1: Add `decode_params` to `strategy_param_space.py`**

Append:

```python
def _apply_to_tree(tree: Optional[Dict[str, Any]], by_id: Dict[str, Dict[str, Any]]
                   ) -> Optional[Dict[str, Any]]:
    """Deep-copy a condition tree, substituting value/confirmation_bars by node id."""
    if tree is None:
        return None
    new = copy.deepcopy(tree)

    def _recurse(node):
        if not isinstance(node, dict):
            return
        for child in (node.get("conditions") or []):
            _recurse(child)
        cid = node.get("id")
        if cid and cid in by_id:
            sub = by_id[cid]
            if "value" in sub:
                node["value"] = sub["value"]
            if "confirmation_bars" in sub:
                node["confirmation_bars"] = sub["confirmation_bars"]

    _recurse(new)
    return new


def decode_params(strategy, flat_params: Dict[str, Any]) -> Dict[str, Any]:
    """Reconstruct a concrete trial config from a decoded flat params dict.

    Returns:
      {
        'tp': float, 'sl': float,                 # falls back to strategy defaults
        'rm': {risk_per_trade_pct,...},           # classic-RM dict (defaults + overrides)
        'expert_overrides': {param: value},       # model:* stripped of prefix
        'buy_tree': dict|None, 'sell_tree': dict|None, 'exit_rules': list,
      }
    The source Strategy is NEVER mutated (trees are deep-copied).
    """
    # Partition flat keys by namespace
    cond_by_id: Dict[str, Dict[str, Any]] = {}
    exit_action_by_id: Dict[str, float] = {}
    rm: Dict[str, Any] = {}
    expert_overrides: Dict[str, Any] = {}
    tp = strategy.initial_tp_percent
    sl = strategy.initial_sl_percent

    for key, val in flat_params.items():
        if key == "tp":
            tp = val
        elif key == "sl":
            sl = val
        elif key.startswith("rm:"):
            rm[key[len("rm:"):]] = val
        elif key.startswith("model:"):
            expert_overrides[key[len("model:"):]] = val
        elif key.startswith("cond:"):
            _, cid, field = key.split(":", 2)
            cond_by_id.setdefault(cid, {})[field] = val
        elif key.startswith("exit:"):
            _, eid, field = key.split(":", 2)  # field == 'action_value'
            exit_action_by_id[eid] = val
        else:
            raise ValueError(f"Unknown decoded param namespace: {key!r}")

    # Fill RM defaults from the Strategy columns for params NOT under optimization
    rm_full = _rm_defaults_from_strategy(strategy)
    rm_full.update(rm)

    buy_tree = _apply_to_tree(strategy.buy_entry_conditions, cond_by_id)
    sell_tree = _apply_to_tree(strategy.sell_entry_conditions, cond_by_id)

    exit_rules = copy.deepcopy(strategy.exit_conditions or [])
    for rule in exit_rules:
        eid = rule.get("id")
        if eid in exit_action_by_id:
            rule["action_value"] = exit_action_by_id[eid]
        if rule.get("conditions"):
            rule["conditions"] = _apply_to_tree(rule["conditions"], cond_by_id)

    return {
        "tp": tp, "sl": sl, "rm": rm_full,
        "expert_overrides": expert_overrides,
        "buy_tree": buy_tree, "sell_tree": sell_tree, "exit_rules": exit_rules,
    }


def _rm_defaults_from_strategy(strategy) -> Dict[str, Any]:
    """Read the non-optimized RM baseline values from the Strategy columns."""
    return {
        "risk_per_trade_pct": getattr(strategy, "rm_risk_per_trade_pct", None),
        "per_instrument_cap_pct": getattr(strategy, "rm_per_instrument_cap_pct", None),
        "min_stop_pct": getattr(strategy, "rm_min_stop_pct", None),
        "atr_stop_mult": getattr(strategy, "rm_atr_stop_mult", None),
        "max_concurrent_positions": getattr(strategy, "rm_max_concurrent_positions", None),
    }
```

- [ ] **Step 2: Write failing decode tests**

`backend/tests/test_strategy_param_space_decode.py`:

```python
import copy, types
from app.services.strategy_param_space import decode_params


def _strategy(**kw):
    base = dict(
        initial_tp_percent=5.0, initial_sl_percent=2.0,
        buy_entry_conditions=None, sell_entry_conditions=None, exit_conditions=[],
        rm_risk_per_trade_pct=1.0, rm_per_instrument_cap_pct=20.0, rm_min_stop_pct=2.0,
        rm_atr_stop_mult=2.0, rm_max_concurrent_positions=5,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_decode_tp_sl_rm_expert():
    s = _strategy()
    out = decode_params(s, {"tp": 8.0, "sl": 3.0,
                            "rm:risk_per_trade_pct": 2.5,
                            "model:surprise_min_pct": 12.0})
    assert out["tp"] == 8.0 and out["sl"] == 3.0
    assert out["rm"]["risk_per_trade_pct"] == 2.5
    assert out["rm"]["max_concurrent_positions"] == 5   # baseline preserved
    assert out["expert_overrides"] == {"surprise_min_pct": 12.0}


def test_decode_substitutes_condition_by_id_without_mutating_source():
    buy = {"operator": "AND", "conditions": [
        {"id": "c1", "field": "model:probability", "comparison": ">=", "value": 0.6},
    ]}
    s = _strategy(buy_entry_conditions=buy)
    original = copy.deepcopy(buy)
    out = decode_params(s, {"cond:c1:value": 0.8, "cond:c1:confirmation_bars": 3})
    assert out["buy_tree"]["conditions"][0]["value"] == 0.8
    assert out["buy_tree"]["conditions"][0]["confirmation_bars"] == 3
    assert s.buy_entry_conditions == original  # source untouched


def test_decode_exit_action_value():
    s = _strategy(exit_conditions=[{"id": "e1", "action": "adjust_sl",
                                    "action_value": 1.0, "conditions": {}}])
    out = decode_params(s, {"exit:e1:action_value": 2.5})
    assert out["exit_rules"][0]["action_value"] == 2.5


def test_decode_falls_back_to_strategy_defaults():
    s = _strategy()
    out = decode_params(s, {})  # nothing optimized this trial
    assert out["tp"] == 5.0 and out["sl"] == 2.0
    assert out["rm"]["risk_per_trade_pct"] == 1.0
```

- [ ] **Step 3: Run decode tests + a roundtrip with GeneticOptimizer**

`backend/tests/test_param_space_roundtrip.py`:

```python
import types
from app.services.genetic import GeneticOptimizer
from app.services.strategy_param_space import collect_param_space, decode_params


def _strategy():
    buy = {"operator": "AND", "conditions": [
        {"id": "c1", "field": "model:probability", "comparison": ">=", "value": 0.6,
         "optimize": True, "value_min": 0.5, "value_max": 0.9, "value_step": 0.1}]}
    return types.SimpleNamespace(
        initial_tp_percent=5.0, initial_sl_percent=2.0,
        initial_tp_optimize=True, initial_tp_min=2.0, initial_tp_max=10.0, initial_tp_step=1.0,
        initial_sl_optimize=False, initial_sl_min=None, initial_sl_max=None, initial_sl_step=None,
        buy_entry_conditions=buy, sell_entry_conditions=None, entry_conditions=None,
        exit_conditions=[],
        rm_risk_per_trade_pct=1.0, rm_per_instrument_cap_pct=20.0, rm_min_stop_pct=2.0,
        rm_atr_stop_mult=2.0, rm_max_concurrent_positions=5)


def test_collect_decode_through_genetic_optimizer():
    s = _strategy()
    space = collect_param_space(s)            # {'tp':..., 'cond:c1:value':...}
    opt = GeneticOptimizer(param_ranges=space, population_size=4, n_generations=1)
    ind = opt.toolbox.individual()
    flat = opt.decode_individual(ind)         # quantized {name: value}
    decoded = decode_params(s, flat)
    assert 2.0 <= decoded["tp"] <= 10.0
    assert 0.5 <= decoded["buy_tree"]["conditions"][0]["value"] <= 0.9
```
```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/test_strategy_param_space_decode.py tests/test_param_space_roundtrip.py -v
```
Expected: PASS (proves collect→GA gene→decode_individual→decode_params is consistent).

- [ ] **Step 4: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add backend/app/services/strategy_param_space.py backend/tests/test_strategy_param_space_decode.py backend/tests/test_param_space_roundtrip.py
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(optimizer): decode_params (deep-copy tree substitution + tp/sl/rm/expert overrides)"
```

---

## Task 4: Determinism — np.random seeding + checkpoint extension + fitness map + memo

**Files:** edit `app/services/genetic.py` (checkpoint np.random only); create `app/services/strategy_fitness.py` (fitness map + sentinel) and `app/services/trial_memo.py` (content-hash memo). **Tests:** `backend/tests/test_determinism_helpers.py`.

- [ ] **Step 1: Extend `GeneticOptimizer` checkpoint to persist np.random state**

In `app/services/genetic.py`, `get_checkpoint_data` (line 297-315) currently saves `random.getstate()` only. Add numpy state:

```python
        return {
            'generation': generation,
            'population': [list(ind) for ind in population],
            'best_individual': list(self.best_individual) if self.best_individual else None,
            'best_fitness': self.best_fitness,
            'history': self.history,
            'random_state': list(random.getstate()),
            'np_random_state': _np_state_to_jsonable(np.random.get_state()),
        }
```

And in `resume_from_checkpoint` (after the `random.setstate` block, ~line 292):

```python
        if 'np_random_state' in checkpoint:
            try:
                np.random.set_state(_jsonable_to_np_state(checkpoint['np_random_state']))
            except Exception as e:
                logger.warning(f"Could not restore numpy random state: {e}")
```

Add the two helpers at module scope (numpy state is a tuple `(str, ndarray[uint32], int, int, float)`; JSON needs the ndarray as a list):

```python
def _np_state_to_jsonable(state):
    name, keys, pos, has_gauss, cached = state
    return [name, keys.tolist(), int(pos), int(has_gauss), float(cached)]

def _jsonable_to_np_state(s):
    name, keys, pos, has_gauss, cached = s
    return (name, np.array(keys, dtype=np.uint32), int(pos), int(has_gauss), float(cached))
```

> This is the ONLY edit to the existing optimizer. It is backward-compatible: old checkpoints without `np_random_state` simply skip restore.

- [ ] **Step 2: Create the fitness map (`strategy_fitness.py`)**

```python
"""Map StrategyOptimization.fitness_metric -> a scalar from the BACKTEST results dict.

GA is maximize-only (FitnessMax weights=(1.0,)), so max_drawdown is negated.
0-trade configs return a sentinel LARGE-NEGATIVE fitness distinct from the 0.0
exception fallback in GeneticOptimizer.optimize (genetic.py:357).
"""
import math

# Distinct from 0.0 (the exception fallback) so a no-trade config is never
# confused with a crashed trial, and is always worse than any real config.
ZERO_TRADE_SENTINEL = -1.0e9

# results-dict keys are those produced by _convert_bt_results (backtest_handler.py).
_FITNESS_KEYS = {
    "sharpe": "sharpe_ratio",
    "sharpe_ratio": "sharpe_ratio",
    "return": "total_return",
    "total_return": "total_return",
    "profit_factor": "profit_factor",
    "win_rate": "win_rate",
    "sortino": "sortino_ratio",
    "calmar": "calmar_ratio",
    "sqn": "sqn",
    # max_drawdown handled specially (negated)
}


def compute_fitness(fitness_metric: str, results: dict) -> float:
    """Return the scalar fitness for a metric from a backtest results dict."""
    if results is None:
        return ZERO_TRADE_SENTINEL
    if int(results.get("total_trades", 0) or 0) == 0:
        return ZERO_TRADE_SENTINEL

    metric = fitness_metric.lower()
    if metric in ("max_drawdown", "max_dd", "drawdown"):
        dd = results.get("max_drawdown")
        if dd is None:
            return ZERO_TRADE_SENTINEL
        return -float(dd)  # smaller drawdown -> larger (less negative) fitness

    key = _FITNESS_KEYS.get(metric)
    if key is None:
        raise ValueError(f"Unknown fitness_metric: {fitness_metric!r}. "
                         f"Valid: {sorted(set(_FITNESS_KEYS) | {'max_drawdown'})}")
    val = results.get(key)
    if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
        return ZERO_TRADE_SENTINEL
    return float(val)
```

- [ ] **Step 3: Create the trial memo (`trial_memo.py`)**

```python
"""Content-addressed memo for backtest trials.

Key = sha256 of canonical JSON of (model_id, pred_dataset_id, exec_dataset_id,
date_range, decoded_params). Same cache + same decoded params => identical result
(determinism_rule), so an elitism-reselected identical individual is a free memo
hit AND a self-check that the run is deterministic.
"""
import hashlib
import json
from typing import Any, Dict, Optional


def trial_key(identity: Dict[str, Any]) -> str:
    """identity must fully determine the result: model/datasets/date-range/params."""
    blob = json.dumps(identity, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class TrialMemo:
    def __init__(self):
        self._store: Dict[str, float] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[float]:
        if key in self._store:
            self.hits += 1
            return self._store[key]
        self.misses += 1
        return None

    def put(self, key: str, fitness: float) -> None:
        self._store[key] = fitness
```

- [ ] **Step 4: Write + run determinism-helper tests**

`backend/tests/test_determinism_helpers.py`:

```python
import numpy as np, random
from app.services.genetic import GeneticOptimizer, _np_state_to_jsonable, _jsonable_to_np_state
from app.services.strategy_fitness import compute_fitness, ZERO_TRADE_SENTINEL
from app.services.trial_memo import trial_key, TrialMemo


def test_np_state_roundtrip():
    np.random.seed(123); _ = np.random.rand(5)
    state = np.random.get_state()
    restored = _jsonable_to_np_state(_np_state_to_jsonable(state))
    np.random.set_state(restored)
    a = np.random.rand(3)
    np.random.set_state(state)
    b = np.random.rand(3)
    assert np.allclose(a, b)


def test_fitness_max_drawdown_negated():
    assert compute_fitness("max_drawdown", {"total_trades": 4, "max_drawdown": 12.0}) == -12.0


def test_fitness_zero_trades_sentinel_distinct_from_zero():
    f = compute_fitness("sharpe", {"total_trades": 0, "sharpe_ratio": 2.0})
    assert f == ZERO_TRADE_SENTINEL and f != 0.0


def test_fitness_maps_keys():
    assert compute_fitness("sharpe", {"total_trades": 1, "sharpe_ratio": 1.5}) == 1.5
    assert compute_fitness("return", {"total_trades": 1, "total_return": 33.0}) == 33.0


def test_trial_key_stable_and_order_independent():
    a = trial_key({"model_id": 1, "params": {"tp": 5, "sl": 2}})
    b = trial_key({"params": {"sl": 2, "tp": 5}, "model_id": 1})
    assert a == b


def test_memo_hit_miss():
    m = TrialMemo(); k = trial_key({"x": 1})
    assert m.get(k) is None and m.misses == 1
    m.put(k, 0.9)
    assert m.get(k) == 0.9 and m.hits == 1


def test_seeded_population_reproducible():
    space = {"a": {"type": "float", "min": 0, "max": 1, "step": 0.1}}
    random.seed(7); np.random.seed(7)
    p1 = [list(GeneticOptimizer(param_ranges=space, population_size=5, n_generations=1)
                .toolbox.individual()) for _ in range(5)]
    random.seed(7); np.random.seed(7)
    p2 = [list(GeneticOptimizer(param_ranges=space, population_size=5, n_generations=1)
                .toolbox.individual()) for _ in range(5)]
    assert p1 == p2
```
```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/test_determinism_helpers.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add backend/app/services/genetic.py backend/app/services/strategy_fitness.py backend/app/services/trial_memo.py backend/tests/test_determinism_helpers.py
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(optimizer): np.random checkpoint, fitness map (DD negation + 0-trade sentinel), trial memo"
```

---

## Task 5: `strategy_optimization_handler` — joint GA run wired to the Phase-2 backtest

**Files (create):** `app/services/strategy_optimization_handler.py`. **Tests:** `backend/tests/test_strategy_optimization_handler.py`.

This is the keystone: it mirrors the proven `job_handler` GA template (validate → seed → hoist → fitness_function → optimize → persist), but the fitness runs the **deterministic Phase-2 daily backtest** and reads `results[<metric>]`.

> **Re-plan checkpoint (Phase-2 trial-run API).** `_run_trial_backtest` is the single seam to the Phase-2 engine. Finalize it when the Phase-2 in-process runner is known (see the top-of-file Re-plan checkpoint). The contract: it must accept the hoisted param-independent state + decoded params and return a results dict with the `_convert_bt_results` keys. Do NOT enqueue a sub-task (max_workers=1 deadlock); call the runner synchronously in-process.

- [ ] **Step 1: Handler skeleton with fail-early config validation**

Create `app/services/strategy_optimization_handler.py`:

```python
"""Strategy optimization handler — joint genetic search over
expert + classic-RM + ruleset/condition params, scored by one backtest metric.

Registered as task type 'strategy_optimization' (main.py). Mirrors the GA wiring
in job_handler.py but the fitness runs the deterministic Phase-2 daily backtest.
"""
import logging
import random
from datetime import datetime
from typing import Any, Dict, Optional

import numpy as np

from app.models.database import SessionLocal
from app.models import Strategy as StrategyModel
from app.models.strategy_optimization import StrategyOptimization
from app.services.genetic import GeneticOptimizer, DEAP_AVAILABLE
from app.services.task_queue import get_task_queue
from app.services.strategy_param_space import collect_param_space, decode_params
from app.services.strategy_fitness import compute_fitness, ZERO_TRADE_SENTINEL
from app.services.trial_memo import trial_key, TrialMemo

logger = logging.getLogger(__name__)

# Mirror job_handler.required_ga_keys (no-defaults rule, backend/CLAUDE.md).
REQUIRED_GA_KEYS = ("populationSize", "generations", "crossoverProb", "mutationProb",
                    "earlyStoppingGenerations", "elitismPercent", "seed")


def _fail(opt_id: int, db, msg: str) -> Dict[str, Any]:
    logger.error(f"strategy_optimization {opt_id} failed: {msg}")
    row = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
    if row:
        row.status = "failed"; row.error_message = msg; db.commit()
    return {"status": "failed", "error": msg}


def handle_strategy_optimization(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not DEAP_AVAILABLE:
        return {"status": "failed", "error": "DEAP not available"}
    opt_id = payload.get("optimization_id")
    if not opt_id:
        return {"status": "failed", "error": "optimization_id is required"}

    db = SessionLocal()
    try:
        opt = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
        if not opt:
            return {"status": "failed", "error": f"StrategyOptimization {opt_id} not found"}
        opt.status = "running"; opt.started_at = datetime.now(); db.commit()

        strategy = db.query(StrategyModel).filter(StrategyModel.id == opt.strategy_id).first()
        if not strategy:
            return _fail(opt_id, db, f"Strategy {opt.strategy_id} not found")

        ga = opt.optimization_config or {}
        for key in REQUIRED_GA_KEYS:
            if key not in ga:
                return _fail(opt_id, db, f"optimization_config.{key} is required")
        if not opt.fitness_metric:
            return _fail(opt_id, db, "fitness_metric is required")

        backtest_cfg = (opt.optimization_config or {}).get("backtest")
        if not backtest_cfg:
            return _fail(opt_id, db, "optimization_config.backtest is required "
                                     "(model/datasets/date-range/initial_capital/...)")
        expert_cfg = (opt.optimization_config or {}).get("expert_params")  # may be None
        rm_cfg = (opt.optimization_config or {}).get("rm_params")          # may be None

        # --- Build the joint param space (Task 1) ---
        try:
            param_space = collect_param_space(strategy, expert_cfg=expert_cfg, rm_cfg=rm_cfg)
        except ValueError as e:
            return _fail(opt_id, db, str(e))
        opt.parameter_ranges = param_space; db.commit()

        # --- DETERMINISM: seed both RNGs (Task 4 / determinism_rule) ---
        seed = int(ga["seed"])
        random.seed(seed); np.random.seed(seed)

        # --- HOIST the param-independent pass out of the trial loop ---
        # (determinism_rule lever 2: compute the as_of _gather over the cache once)
        hoisted = _build_hoisted_state(backtest_cfg)   # see Step 2 / Re-plan checkpoint

        memo = TrialMemo()
        all_results: list = []
        best = {"fitness": None, "params": None}

        def fitness_function(decoded_flat: Dict[str, Any]) -> float:
            if get_task_queue().is_task_paused(task_id):
                raise InterruptedError("paused/cancelled")
            decoded = decode_params(strategy, decoded_flat)
            key = trial_key({
                "model_id": backtest_cfg.get("model_id"),
                "pred_dataset_id": backtest_cfg.get("prediction_dataset_id"),
                "exec_dataset_id": backtest_cfg.get("execution_dataset_id"),
                "start": str(backtest_cfg.get("start_date")),
                "end": str(backtest_cfg.get("end_date")),
                "params": decoded_flat,
            })
            cached = memo.get(key)
            if cached is not None:
                return cached
            results = _run_trial_backtest(backtest_cfg, hoisted, decoded)
            fit = compute_fitness(opt.fitness_metric, results)
            memo.put(key, fit)
            all_results.append({"params": decoded_flat, "fitness": fit, "key": key,
                                "trades": results.get("total_trades") if results else 0})
            if best["fitness"] is None or fit > best["fitness"]:
                best["fitness"] = fit; best["params"] = decoded_flat
            return fit

        gen_state = {"gen": 0}

        def on_generation_start(generation: int):
            gen_state["gen"] = generation

        def ga_callback(generation: int, best_fitness: float, best_params: Dict):
            pct = ((generation + 1) / int(ga["generations"])) * 100.0
            get_task_queue().update_progress(
                task_id, pct, f"Gen {generation+1}/{ga['generations']} best={best_fitness:.4f}")
            row = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
            row.progress = pct; row.best_fitness = best_fitness
            row.best_params = best_params; row.all_results = all_results; db.commit()
            if get_task_queue().is_task_paused(task_id):
                raise InterruptedError("paused/cancelled")

        def checkpoint_cb(generation: int, population: list):
            _save_checkpoint(task_id, optimizer.get_checkpoint_data(generation, population))

        # brute_force option for tiny spaces (optimization_type)
        if (opt.optimization_type or "genetic") == "brute_force":
            return _run_brute_force(opt, db, task_id, strategy, param_space,
                                    fitness_function, all_results)

        optimizer = GeneticOptimizer(
            param_ranges=param_space,
            population_size=int(ga["populationSize"]),
            n_generations=int(ga["generations"]),
            crossover_prob=float(ga["crossoverProb"]),
            mutation_prob=float(ga["mutationProb"]),
            early_stopping_generations=int(ga["earlyStoppingGenerations"]),
            elitism_percent=float(ga["elitismPercent"]),
        )
        start_gen, init_pop = 0, None
        ckpt = _load_checkpoint(task_id)
        if ckpt:
            start_gen, init_pop = optimizer.resume_from_checkpoint(ckpt)

        result = optimizer.optimize(
            fitness_function=fitness_function,
            callback=ga_callback,
            on_generation_start=on_generation_start,
            checkpoint_callback=checkpoint_cb,
            start_generation=start_gen,
            initial_population=init_pop,
        )

        opt.status = "completed"; opt.completed_at = datetime.now(); opt.progress = 100.0
        opt.best_params = result["best_params"]; opt.best_fitness = result["best_fitness"]
        opt.all_results = all_results; db.commit()
        logger.info(f"strategy_optimization {opt_id} done: best_fitness={result['best_fitness']:.4f} "
                    f"memo hits/misses={memo.hits}/{memo.misses}")
        return {"status": "completed", "optimization_id": opt_id,
                "best_fitness": result["best_fitness"], "best_params": result["best_params"]}

    except InterruptedError:
        return {"status": "paused"}
    except Exception as e:
        logger.error(f"strategy_optimization {opt_id} crashed: {e}", exc_info=True)
        return _fail(opt_id, db, str(e))
    finally:
        db.close()
```

- [ ] **Step 2: The Phase-2 seam + brute-force + checkpoint persistence helpers**

Append the helpers. `_run_trial_backtest`/`_build_hoisted_state` are the Phase-2 seam — implement against the confirmed Phase-2 runner (Re-plan checkpoint). Provide a working default that routes to `run_backtest` for the ML expert so the handler is testable before Phase 2 lands:

```python
def _build_hoisted_state(backtest_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the param-INDEPENDENT pass once per run (determinism lever 2).

    For the ML expert: the model prediction pass over the fixed cache.
    For ba2 experts (Phase 2): the as_of _gather over the cache for the date range.
    Returns an opaque dict consumed by _run_trial_backtest.

    >>> Re-plan checkpoint: confirm the Phase-2 daily engine's hoist API and what
        it returns (predictions lookup / pre-gathered bundles). Until then, for the
        ML path we hoist nothing param-independent here (run_backtest reloads the
        model per call); record that as a known perf-todo, not a correctness issue.
    """
    return {"backtest_cfg": backtest_cfg}


def _run_trial_backtest(backtest_cfg: Dict[str, Any], hoisted: Dict[str, Any],
                        decoded: Dict[str, Any]) -> Dict[str, Any]:
    """Run ONE deterministic backtest with the decoded trial params; return results dict.

    >>> Re-plan checkpoint: wire to the Phase-2 in-process daily runner
        (run_daily_backtest(config, hoisted, decoded) -> results) for ba2 experts
        with multi-asset RM (per_instrument_cap/max_concurrent enforced).
        Below is the ML-expert adapter that works TODAY via backtest_handler.run_backtest
        so the handler + reproducibility gate are testable pre-Phase-2.
    """
    engine = backtest_cfg.get("engine", "ml")
    if engine == "daily":
        from app.services.daily_engine import run_daily_backtest  # Phase 2 module
        return run_daily_backtest(backtest_cfg, hoisted, decoded)

    # --- ML-expert adapter (existing single-asset engine) ---
    from app.services.backtest_handler import run_backtest, _empty_results
    from app.models.database import SessionLocal
    from app.models import Dataset, TrainedModel
    import pandas as pd
    db = SessionLocal()
    try:
        model = db.query(TrainedModel).filter(TrainedModel.id == backtest_cfg["model_id"]).first()
        pred = db.query(Dataset).filter(Dataset.id == backtest_cfg["prediction_dataset_id"]).first()
        exe = db.query(Dataset).filter(Dataset.id == backtest_cfg["execution_dataset_id"]).first()
        if not (model and pred and exe):
            return _empty_results(backtest_cfg.get("initial_capital", 10000.0))
        pred_df = pd.read_csv(pred.file_path); exec_df = pd.read_csv(exe.file_path)
        for df in (pred_df, exec_df):
            if "Date" in df.columns:
                df["Date"] = pd.to_datetime(df["Date"])
        strategy_params = {"initial_tp_percent": decoded["tp"], "initial_sl_percent": decoded["sl"]}
        return run_backtest(
            model=model, pred_df=pred_df, exec_df=exec_df, strategy_params=strategy_params,
            initial_capital=backtest_cfg.get("initial_capital", 10000.0),
            position_sizing_type=backtest_cfg.get("position_sizing_type", "percent"),
            position_sizing_value=backtest_cfg.get("position_sizing_value", 10.0),
            commission=backtest_cfg.get("commission", 0.0),
            slippage=backtest_cfg.get("slippage", 0.0),
            buy_entry_conditions=decoded["buy_tree"],
            sell_entry_conditions=decoded["sell_tree"],
            exit_conditions=decoded["exit_rules"],
        )
    finally:
        db.close()


def _run_brute_force(opt, db, task_id, strategy, param_space, fitness_function, all_results):
    """Exhaustive search over stepped ranges (itertools.product) for tiny spaces."""
    import itertools
    axes = {}
    for name, spec in param_space.items():
        vals, v = [], spec["min"]
        while v <= spec["max"] + 1e-9:
            vals.append(int(round(v)) if spec["type"] == "int" else round(v, 10))
            v += spec["step"]
        axes[name] = vals
    names = list(axes.keys())
    best = {"fitness": None, "params": None}
    for combo in itertools.product(*(axes[n] for n in names)):
        flat = dict(zip(names, combo))
        fit = fitness_function(flat)
        if best["fitness"] is None or fit > best["fitness"]:
            best = {"fitness": fit, "params": flat}
    opt.status = "completed"; opt.completed_at = datetime.now(); opt.progress = 100.0
    opt.best_params = best["params"]; opt.best_fitness = best["fitness"]
    opt.all_results = all_results; db.commit()
    return {"status": "completed", "optimization_id": opt.id,
            "best_fitness": best["fitness"], "best_params": best["params"]}


def _save_checkpoint(task_id: str, checkpoint_data: Dict[str, Any]) -> None:
    """Persist GA checkpoint to TaskQueue.checkpoint_data (mirror job_handler:490)."""
    from app.models.database import SessionLocal
    from app.models.task_queue import TaskQueue
    db = SessionLocal()
    try:
        t = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        if t:
            t.checkpoint_data = checkpoint_data; db.commit()
    finally:
        db.close()


def _load_checkpoint(task_id: str) -> Optional[Dict[str, Any]]:
    from app.models.database import SessionLocal
    from app.models.task_queue import TaskQueue
    db = SessionLocal()
    try:
        t = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        return t.checkpoint_data if (t and t.checkpoint_data) else None
    finally:
        db.close()
```

> Re-plan checkpoint: confirm the `TaskQueue` model's PK/lookup field (the recon shows `checkpoint_data` exists; verify it is keyed by `task_id` vs `id`) and the exact `from app.models import ...` names for `Dataset`/`TrainedModel`/`Strategy` (they are re-exported from `app.models.__init__` per backtest_handler.py:24). Adjust the imports/queries to match before running.

- [ ] **Step 3: Write the handler reproducibility test (the GATE, with a stub backtest)**

`backend/tests/test_strategy_optimization_handler.py`:

```python
import random
import numpy as np
import pytest
from app.services import strategy_optimization_handler as H


def _deterministic_stub(backtest_cfg, hoisted, decoded):
    """A pure deterministic 'backtest': fitness is a fixed function of tp/sl so
    the SAME params always produce the SAME results (no RNG, no I/O)."""
    tp = decoded["tp"]; sl = decoded["sl"]
    # peak at tp=8, sl=3; always >0 trades so sentinel not triggered
    score = 10.0 - abs(tp - 8.0) - abs(sl - 3.0)
    return {"total_trades": 5, "sharpe_ratio": score, "max_drawdown": 5.0,
            "total_return": score, "profit_factor": 1.5, "win_rate": 55.0}


def _make_opt(db_session_factory, monkeypatch):
    monkeypatch.setattr(H, "_run_trial_backtest", _deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    # ... create a Strategy with tp/sl optimize=True and a StrategyOptimization row
    # via the test DB (see conftest); return its id.


def test_seeded_run_is_reproducible(tmp_path, monkeypatch):
    """GATE: same seed + same (stub) cache => identical best_params/best_fitness."""
    monkeypatch.setattr(H, "_run_trial_backtest", _deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    # Build param space directly to avoid DB for this focused check:
    from app.services.genetic import GeneticOptimizer
    from app.services.strategy_param_space import decode_params
    import types
    s = types.SimpleNamespace(
        initial_tp_percent=5.0, initial_sl_percent=2.0,
        buy_entry_conditions=None, sell_entry_conditions=None, exit_conditions=[],
        rm_risk_per_trade_pct=1.0, rm_per_instrument_cap_pct=20.0, rm_min_stop_pct=2.0,
        rm_atr_stop_mult=2.0, rm_max_concurrent_positions=5)
    space = {"tp": {"type": "float", "min": 2, "max": 12, "step": 1},
             "sl": {"type": "float", "min": 1, "max": 6, "step": 1}}

    def run_once(seed):
        random.seed(seed); np.random.seed(seed)
        opt = GeneticOptimizer(param_ranges=space, population_size=8, n_generations=5,
                               crossover_prob=0.7, mutation_prob=0.2,
                               early_stopping_generations=10, elitism_percent=10.0)
        def fit(flat):
            d = decode_params(s, flat)
            return _deterministic_stub({}, {}, d)["sharpe_ratio"]
        return opt.optimize(fitness_function=fit)

    r1 = run_once(42); r2 = run_once(42)
    assert r1["best_params"] == r2["best_params"]
    assert r1["best_fitness"] == r2["best_fitness"]


def test_required_ga_key_validation(monkeypatch):
    """A missing GA key must fail fast (no-defaults rule)."""
    # Drive handle_strategy_optimization with optimization_config missing 'seed'
    # against a test DB row; assert status failed + 'seed is required' in error.
    pass  # implement with the conftest test DB once Step 4 fixtures exist
```

- [ ] **Step 4: Run the reproducibility gate**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/test_strategy_optimization_handler.py -v
```
Expected: `test_seeded_run_is_reproducible` PASSES (identical best individual across two seeded runs over a deterministic stub — the Phase-4 core gate). Fill in the DB-backed tests (`_make_opt`, `test_required_ga_key_validation`) using the Task 7 conftest.

- [ ] **Step 5: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add backend/app/services/strategy_optimization_handler.py backend/tests/test_strategy_optimization_handler.py
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(optimizer): strategy_optimization handler (joint GA, seeded, hoist+memo, Phase-2 seam)"
```

---

## Task 6: Register the task type + POST `/optimize` route

**Files:** edit `app/main.py` (register handler); edit `app/api/strategies.py` (POST optimize route). **Tests:** `backend/tests/test_optimize_route.py`.

- [ ] **Step 1: Register `strategy_optimization` in the lifespan**

In `app/main.py`, in the lifespan block where handlers are registered (after line 257 `register_handler('backtest', handle_backtest)`):

```python
    from app.services.strategy_optimization_handler import handle_strategy_optimization
    task_queue.register_handler('strategy_optimization', handle_strategy_optimization)
```

- [ ] **Step 2: Add the POST optimize route**

In `app/api/strategies.py`, add the request model + route (the route builds `rm_params` from the Strategy's new RM columns, writes a `StrategyOptimization` row, and enqueues the task):

```python
from app.models import StrategyOptimization  # confirm re-export name in app/models/__init__
from app.services.task_queue import get_task_queue
from app.services.strategy_param_space import CLASSIC_RM_PARAMS


class OptimizeRequest(BaseModel):
    name: Optional[str] = None
    fitness_metric: str                      # sharpe/return/profit_factor/win_rate/max_drawdown/...
    optimization_type: str = "genetic"       # genetic | brute_force
    optimization_config: dict                # MUST include populationSize/generations/.../seed + backtest{}
    expert_params: Optional[dict] = None     # {param:{optimize,min,max,step,type}}


def _rm_cfg_from_strategy(s) -> dict:
    """Build the rm_params dict collect_param_space expects from Strategy columns."""
    cfg = {}
    for p in CLASSIC_RM_PARAMS:
        is_int = (p == "max_concurrent_positions")
        cfg[p] = {
            "optimize": bool(getattr(s, f"rm_{p}_optimize", False)),
            "min": getattr(s, f"rm_{p}_min", None),
            "max": getattr(s, f"rm_{p}_max", None),
            "step": getattr(s, f"rm_{p}_step", None),
            "type": "int" if is_int else "float",
        }
    return cfg


@router.post("/{strategy_id}/optimize")
async def optimize_strategy(strategy_id: int, req: OptimizeRequest,
                            db: Session = Depends(get_db)):
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
    # Fold the per-strategy RM config into optimization_config so the handler is self-contained
    cfg = dict(req.optimization_config or {})
    cfg["rm_params"] = _rm_cfg_from_strategy(strategy)
    if req.expert_params is not None:
        cfg["expert_params"] = req.expert_params

    row = StrategyOptimization(
        strategy_id=strategy_id, name=req.name,
        fitness_metric=req.fitness_metric, optimization_type=req.optimization_type,
        optimization_config=cfg, status="pending",
    )
    db.add(row); db.commit(); db.refresh(row)

    task_id = get_task_queue().add_task(  # confirm enqueue method name + signature
        task_type="strategy_optimization", payload={"optimization_id": row.id})
    logger.info(f"Enqueued strategy_optimization {row.id} (task {task_id})")
    return {"optimizationId": row.id, "taskId": task_id, **row.to_dict()}
```

> Re-plan checkpoint: confirm the task-queue **enqueue** method name/signature (the recon found `register_handler`/`update_progress`/`is_task_paused`; locate the submit/add method — likely `add_task`/`submit_task`/`enqueue` in `task_queue.py`) and that `StrategyOptimization` is re-exported from `app/models/__init__`. Adjust both call sites to match.

- [ ] **Step 3: Write + run the route test**

`backend/tests/test_optimize_route.py` (uses FastAPI `TestClient` + the test DB from conftest):

```python
def test_optimize_route_creates_row_and_enqueues(client, seed_strategy):
    payload = {
        "fitness_metric": "sharpe", "optimization_type": "genetic",
        "optimization_config": {
            "populationSize": 8, "generations": 3, "crossoverProb": 0.7,
            "mutationProb": 0.2, "earlyStoppingGenerations": 10, "elitismPercent": 10.0,
            "seed": 42,
            "backtest": {"engine": "ml", "model_id": 1, "prediction_dataset_id": 1,
                         "execution_dataset_id": 1, "start_date": "2020-01-01",
                         "end_date": "2021-01-01", "initial_capital": 10000.0},
        },
    }
    r = client.post(f"/api/strategies/{seed_strategy.id}/optimize", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["optimizationId"] > 0
    assert body["fitnessMetric"] == "sharpe"
    assert body["optimizationConfig"]["rm_params"]  # RM cfg folded in
```
```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend && ./venv/bin/python -m pytest tests/test_optimize_route.py -v
```
Expected: PASS (row created, RM cfg folded, task enqueued — stub the queue's enqueue in conftest if it needs a running daemon).

- [ ] **Step 4: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add backend/app/main.py backend/app/api/strategies.py backend/tests/test_optimize_route.py
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(optimizer): register strategy_optimization task + POST /strategies/{id}/optimize"
```

---

## Task 7: Test harness + acceptance gate verification

**Files:** `backend/tests/conftest.py` (test DB + client fixtures), `backend/tests/__init__.py`. **Goal:** the determinism gate runs green end-to-end and the no-defaults validation is proven.

- [ ] **Step 1: Confirm the test runner + add conftest**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
cat pytest.ini setup.cfg pyproject.toml 2>/dev/null | grep -iE "testpaths|pytest" || echo "no pytest config — default discovery"
ls tests/ 2>/dev/null || mkdir -p tests
```
If there is no pytest config, default discovery picks up `tests/test_*.py`. If the house style is `scripts/` harnesses (per `backend/CLAUDE.md` which shows `scripts/test_dataset_generation.py`), ALSO provide a `scripts/test_phase4_optimizer.py` that imports the same test functions and runs them with plain asserts so it works either way.

`backend/tests/conftest.py` (isolate to a throwaway sqlite; mirror BA2TestPlatform's `SessionLocal`/`Base`):

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models.database import Base


@pytest.fixture(scope="session")
def engine(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("db") / "test.sqlite"
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine):
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.rollback(); s.close()


@pytest.fixture
def seed_strategy(db):
    from app.models.strategy import Strategy
    s = Strategy(name="opt-test", initial_tp_percent=5.0, initial_tp_optimize=True,
                 initial_tp_min=2.0, initial_tp_max=12.0, initial_tp_step=1.0,
                 initial_sl_percent=2.0, initial_sl_optimize=True,
                 initial_sl_min=1.0, initial_sl_max=6.0, initial_sl_step=1.0)
    db.add(s); db.commit(); db.refresh(s)
    return s


@pytest.fixture
def client(engine, monkeypatch):
    """FastAPI TestClient with get_db overridden to the test engine + queue stubbed."""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker
    import app.main as main
    from app.models import get_db
    Session = sessionmaker(bind=engine)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    main.app.dependency_overrides[get_db] = _get_db
    # Stub the queue enqueue so the route returns without a running daemon:
    from app.services import task_queue as tq
    monkeypatch.setattr(tq.get_task_queue(), "add_task", lambda **kw: "stub-task-id", raising=False)
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()
```

> Re-plan checkpoint: confirm `app.models.database` exposes `Base`/`SessionLocal`, `app.models` re-exports `get_db`, and the queue's enqueue method name (Task 6 Step 2). Adjust the fixtures to the real names.

- [ ] **Step 2: Run the full Phase-4 test suite**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend && ./venv/bin/python -m pytest \
  tests/test_strategy_param_space_collect.py tests/test_strategy_param_space_decode.py \
  tests/test_param_space_roundtrip.py tests/test_strategy_rm_columns.py \
  tests/test_determinism_helpers.py tests/test_strategy_optimization_handler.py \
  tests/test_optimize_route.py -v
```
Expected: ALL PASS. The load-bearing assertions: `test_seeded_run_is_reproducible` (gate #2), `test_fitness_max_drawdown_negated` + `test_fitness_zero_trades_sentinel_distinct_from_zero` (gate #4), `test_decode_substitutes_condition_by_id_without_mutating_source` (gate #1), `test_memo_hit_miss` (gate #3).

- [ ] **Step 3: Verify no regression to ML backtest path**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/backend
grep -nE "class MLStrategy|def run_backtest|def handle_backtest" app/services/backtest_handler.py
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform status --short app/services/backtest_handler.py
```
Expected: `backtest_handler.py` shows **no modifications** (the optimizer reuses it, never edits it). `MLStrategy`/`run_backtest`/`handle_backtest` intact.

- [ ] **Step 4: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add backend/tests/conftest.py backend/tests/__init__.py
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "test(optimizer): conftest + Phase-4 acceptance gate (seeded reproducibility, fitness, memo)"
```

---

## Task 8: UI surface for joint optimization (reuse, minimal)

**Files:** `frontend/src/pages/Backtesting.tsx` and/or `frontend/src/components/ConditionBuilder.tsx`. **Goal:** expose the RM/expert/condition optimize toggles + ranges and a "Run Optimization" action; reuse the existing metric cards + tabbed results contract (contract `engine_loop.reused_ba2testplatform.ui`). This is the lightest task — author-once, no new results UI.

- [ ] **Step 1: Locate the existing strategy/optimization UI surface**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/frontend
grep -rnE "optimize|fitness|StrategyOptimization|/strategies|ConditionBuilder" src/pages/Backtesting.tsx src/components/ConditionBuilder.tsx 2>/dev/null | head -40
ls src/components/ 2>/dev/null | grep -iE "condition|optimi|metric"
```
Confirm how TP/SL optimize toggles + min/max/step are already rendered (the `Strategy` model already had `initial_tp_optimize/_min/_max/_step`), and reuse that exact pattern for the five RM params and the per-condition node optimize fields.

- [ ] **Step 2: Add RM optimize controls + the optimize action**

In the strategy editor, render for each of the five RM params (`rm_*`) the same `{value, optimize checkbox, min, max, step}` group used for TP/SL (bind to the new camelCase keys added to `Strategy.to_dict()` in Task 2). Add a "Run Joint Optimization" button that POSTs to `/api/strategies/{id}/optimize` with `{fitness_metric, optimization_type, optimization_config:{populationSize,...,seed,backtest:{...}}, expert_params?}`. Reuse the existing metric cards (Total Return / Sharpe / Max DD / Win Rate / Profit Factor) and the `results.equityCurve/drawdownCurve/trades` tabbed view verbatim to display the best individual's backtest (no new chart code).

- [ ] **Step 3: Smoke-build the frontend**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TestPlatform/frontend && (npm run build 2>&1 | tail -20 || yarn build 2>&1 | tail -20)
```
Expected: build succeeds. (UI is reuse-heavy; the gate is the backend determinism test, not pixel-perfect UI.)

- [ ] **Step 4: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform add frontend/src
git -C /Users/bmigette/Documents/dev/BA2/BA2TestPlatform commit -m "feat(optimizer-ui): RM/expert/condition optimize controls + run-optimization action (reuse metric cards)"
```

---

## No Placeholders

Every code step above contains complete, runnable code — the param-space collector/decoder, the fitness map with the documented `ZERO_TRADE_SENTINEL`, the content-hash memo, the np.random checkpoint extension, the full joint-GA handler, the route, the migration, the conftest, and concrete pytest assertions. The only deliberately deferred pieces are the **two typed Phase-2 seams** (`_build_hoisted_state`, `_run_trial_backtest`), which carry a working ML-expert adapter so Tasks 1-4 and 6-7 are fully testable today, plus explicit `> Re-plan checkpoint:` notes pinpointing exactly what to confirm against the not-yet-built Phase-2 daily-engine runner and the BA2TestPlatform DB/queue API names. The "confirm name in source" notes guard against name drift across the BA2TestPlatform tree; they are verification gates, not TODOs.

---

## Self-Review

**Spec coverage (design §5, §6 Phase 4 + contract `optimizer`):**
- "Expand the existing genetic optimizer to one joint run over expert + classic-RM + ruleset/condition params" → Tasks 1 (collect), 3 (decode), 5 (handler). ✓
- "Each trial sets params from outside; expert reads self.settings; RM + ruleset injected" → `decode_params` emits `expert_overrides`/`rm`/`tp`/`sl`/trees; handler passes them to the trial runner. ✓
- "Reuse the existing optimizer + Backtest/metrics models; do NOT build a new optimizer" → `GeneticOptimizer` reused; only the np.random checkpoint extension edits it; `Backtest`/`StrategyOptimization` reused. ✓
- "Score one fitness metric already in the Backtest model" → `strategy_fitness.compute_fitness` maps to `results[<key>]` (sharpe_ratio/total_return/profit_factor/win_rate/max_drawdown negated). ✓
- "Preserve determinism (same cache + same params ⇒ identical result)" → seed random+np.random, hoist param-independent pass, content-hash memo; reproducibility gate test. ✓
- "GATE: a run over a clean expert explores the joint space and is reproducible (fixed seed + cache ⇒ identical best individual)" → Task 5/7 `test_seeded_run_is_reproducible` + acceptance gate #2. ✓
- Locked decisions honored: classic RM only (RM params, no smart RM); daily cadence (engine seam); equities-first (multi-asset RM in new engine only, Decision 5); reuse genetic + Backtest models. ✓
- "Re-plan checkpoint noting param-space wiring depends on Phase 2's concrete trial-run API" → top-of-file + Task 5 seam + Task 6/7 DB/queue-name checkpoints. ✓

**Type/name consistency:** `collect_param_space(strategy, expert_cfg, rm_cfg) -> flat dict` (Task 1) ↔ `GeneticOptimizer(param_ranges=...)` (Task 5); `decode_params(strategy, flat) -> {tp,sl,rm,expert_overrides,buy_tree,sell_tree,exit_rules}` (Task 3) consumed by `_run_trial_backtest` (Task 5); `compute_fitness(metric, results)` + `ZERO_TRADE_SENTINEL` (Task 4) used in the handler fitness; `trial_key`/`TrialMemo` (Task 4) used in the handler; `CLASSIC_RM_PARAMS` shared by Task 1 collector + Task 6 `_rm_cfg_from_strategy`; `REQUIRED_GA_KEYS` mirrors job_handler's `required_ga_keys` + adds `seed`. Namespacing (`model:`/`rm:`/`tp`/`sl`/`cond:<id>:value`/`cond:<id>:confirmation_bars`/`exit:<id>:action_value`) is identical between collector and decoder.

**Placeholder scan:** the only deferred code is the Phase-2 seam (with a working ML adapter) and DB/queue name confirmations — all flagged with `> Re-plan checkpoint:`. No "TBD"/"add later" in shipped code.

**Known reconciliation points (verify against source during execution, do not assume):** BA2TestPlatform migration mechanism (alembic vs `create_all`/ALTER); `app.models.database` exports (`Base`/`SessionLocal`) and `app.models` re-exports (`get_db`/`Dataset`/`TrainedModel`/`Strategy`/`StrategyOptimization`); the task-queue **enqueue** method name + the `TaskQueue` lookup field for `checkpoint_data`; the Phase-2 in-process daily runner name/signature and its hoist API; pytest discovery config (`tests/` vs `scripts/` house style); the frontend strategy-editor component that already renders TP/SL optimize controls.

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-06-13-backtest-platform-phase4-joint-optimizer-plan.md`. Phase 4 **depends on Phase 2** (the deterministic daily engine + `BacktestAccount`) for the real multi-asset trial runner; Tasks 1-4 and 6-7 are implementable and testable BEFORE Phase 2 via the ML-expert adapter in `_run_trial_backtest`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks (REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`). Run Task 5 LAST among the implementation tasks once the Phase-2 runner is confirmed.
2. **Inline Execution** — execute tasks in this session with checkpoints (REQUIRED SUB-SKILL: `superpowers:executing-plans`).

All work lands on BA2TestPlatform `backend/`; the live `BA2TradePlatform` is untouched by Phase 4 (its migration onto the packages is Phase 6). The acceptance gate is the seeded-reproducibility test (`test_seeded_run_is_reproducible`) plus the fitness/decode/memo tests in Task 7.
