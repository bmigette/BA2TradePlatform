# Options Backtesting — UI & Rule-Wiring Implementation Plan (Plan 2 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax. **Depends on Plan 1** (`...-options-backtest-backend.md`) — the options-capable `BacktestAccount` + provider must exist first.

**Goal:** Let a user configure **option actions** (buy call/put, covered call, protective put, cash-secured put, vertical spreads, straddle/strangle, close) as Strategy enter/exit/RM rules in the backtest UI, have the engine actually fire them via the existing `TradeAction` classes, and make their selection params (target delta, DTE, %OTM, sizing) optimizable.

**Architecture:** Extend the exit-rule model end-to-end — `ConditionBuilder` UI → run/optimize/save payloads → Pydantic `ExitCondition` → `strategy_param_space` (new `exit:<id>:option_*` genes) → the ruleset-seeding that maps an exit rule to a `TradeAction`, now mapping option-action rules to `BuyCallAction`/`SellCoveredCallAction`/etc. with their selection params. Spec: `docs/superpowers/specs/2026-06-15-options-backtest-design.md` §5.

**Tech Stack:** React 18 + TS + Vite (frontend, `npm run build` gate — `verbatimModuleSyntax` → `import type`; `src/lib`+`src/components` may need `git add -f`), FastAPI/Pydantic backend (`./venv/bin/python`), DEAP optimizer.

**Verified interfaces (do not re-derive):**
- `ExitConditionSet` (TS, `ConditionBuilder.tsx:524-534`): `{id, name, conditions, action:'close'|'adjust_tp'|'adjust_sl', actionValue?, actionValueOptimize?, actionValueMin?, actionValueMax?, actionValueStep?}`. Action `<select>` at 609-624; optimize-range panel at 657-699; `updateExitCondition` at 559-563.
- Payload serialization (`Backtesting.tsx`): run/ML 531-541 (camelCase), save 702-711 (snake_case), expert path `exit_conditions: exitConditions` at 573.
- Pydantic `ExitCondition` (`backend/app/api/strategies.py:42-51`) + `StrategyCreate.exit_conditions: Optional[List[dict]]` (54-72).
- Param-space (`backend/app/services/strategy_param_space.py`): `_collect_conditions` builds `exit:<id>:action_value` + `exit:<id>:enabled` (104-125); decode 236-241; apply 248-261; `_range_entry(min,max,step,is_int)` 27-36.
- Option `TradeAction` ctors take `strike_method`, `strike_param`, `dte_min`, `dte_max`, `sizing`, `min_open_interest`, `max_spread_pct` (`TradeActions.py:1516-1536`); selection methods are `"delta"` | `"percent_otm"` | `"consensus_target"` (`option_selector.py`). Action type values: `buy_call`, `buy_put`, `sell_covered_call`, `sell_cash_secured_put`, `buy_protective_put`, `open_bull_call_spread`, `open_bear_put_spread`, `open_bear_call_spread`, `open_straddle`, `open_strangle`, `close_option` (`types.py:381-391`).

---

## File Structure
- **Modify** `frontend/src/components/ConditionBuilder.tsx` — extend `ExitConditionSet`; add option actions to the action picker + option-param inputs + their optimize toggles.
- **Modify** `frontend/src/pages/Backtesting.tsx` — carry the new option-action fields in run/save/expert payloads.
- **Modify** `backend/app/api/strategies.py` — extend the `ExitCondition` Pydantic model.
- **Modify** `backend/app/services/strategy_param_space.py` — emit/decode/apply `exit:<id>:option_delta|option_dte|option_otm` genes.
- **Modify** the ruleset-seeding that maps exit rules → actions (find it: `grep -rn "seed_ruleset_from_tree\|adjust_tp\|adjust_sl\|action.*close" BA2TradeCommon BA2TestPlatform/backend | grep -i rule`) — map option-action rules to the option `TradeAction`s.
- **Modify** `backend/app/services/backtest/daily_backtest_handler.py` — detect `uses_options` from the strategy's rule actions; derive the options cache db + underlyings; pass to the Plan-1 validation/injection.
- **Tests:** `backend/tests/test_strategy_param_space_options.py`, `backend/tests/backtest/test_option_rule_wiring.py`, `backend/tests/backtest/test_options_rule_e2e.py`; frontend build gate.

---

## Task 1: Extend the exit-rule model (TS + Pydantic)

**Files:** Modify `frontend/src/components/ConditionBuilder.tsx` (`ExitConditionSet`); Modify `backend/app/api/strategies.py` (`ExitCondition`). Test: backend Pydantic round-trip.

New optional fields on an exit rule (kept optional → equity rules unchanged):
`optionStrategy?` (the action type value, e.g. `'buy_call'`), `optionStrikeMethod?: 'delta'|'percent_otm'|'consensus_target'`, `optionStrikeParam?: number` (delta target or OTM %), `optionDteMin?: number`, `optionDteMax?: number`, `optionSizing?: number` (% of equity), plus optimize ranges for the numeric selection params: `optionStrikeParamOptimize?/Min?/Max?/Step?`, `optionDteOptimize?/Min?/Max?/Step?`.

- [ ] **Step 1: Failing test (backend Pydantic accepts the new fields)**
```python
# backend/tests/test_strategy_param_space_options.py
from app.api.strategies import ExitCondition
def test_exitcondition_accepts_option_fields():
    ec = ExitCondition(id="x1", conditions={"logic":"AND","conditions":[]}, action="buy_call",
        option_strategy="buy_call", option_strike_method="delta", option_strike_param=0.3,
        option_dte_min=20, option_dte_max=45, option_sizing=5.0,
        option_strike_param_optimize=True, option_strike_param_min=0.2,
        option_strike_param_max=0.4, option_strike_param_step=0.05)
    assert ec.option_strategy == "buy_call" and ec.option_strike_method == "delta"
```
- [ ] **Step 2: Run it, verify it fails** — `./venv/bin/python -m pytest tests/test_strategy_param_space_options.py -q` (unexpected-kwarg / validation error).
- [ ] **Step 3: Implement** — extend `ExitCondition` (allow `action` to be any of the option action values too) with the snake_case fields:
```python
class ExitCondition(BaseModel):
    id: str
    name: Optional[str] = None
    conditions: ConditionBase
    action: str  # close | adjust_tp | adjust_sl | buy_call | buy_put | sell_covered_call | ...
    action_value: Optional[float] = None
    action_value_optimize: bool = False
    action_value_min: Optional[float] = None
    action_value_max: Optional[float] = None
    action_value_step: Optional[float] = None
    # --- option-action fields (None for equity actions) ---
    option_strategy: Optional[str] = None
    option_strike_method: Optional[str] = None      # delta | percent_otm | consensus_target
    option_strike_param: Optional[float] = None
    option_dte_min: Optional[int] = None
    option_dte_max: Optional[int] = None
    option_sizing: Optional[float] = None           # % of equity
    option_strike_param_optimize: bool = False
    option_strike_param_min: Optional[float] = None
    option_strike_param_max: Optional[float] = None
    option_strike_param_step: Optional[float] = None
    option_dte_optimize: bool = False
    option_dte_min_range: Optional[int] = None
    option_dte_max_range: Optional[int] = None
    option_dte_step: Optional[int] = None
```
Extend the TS `ExitConditionSet` with the camelCase equivalents (`action` union widened to include the option action values).
- [ ] **Step 4: Run test, verify pass**; `cd frontend && npm run build` (TS compiles).
- [ ] **Step 5: Commit** — `git commit -m "feat(options-bt): exit-rule model carries option action + selection params (TS+Pydantic)"`

## Task 2: Action picker + option-param inputs (ConditionBuilder)

**Files:** Modify `frontend/src/components/ConditionBuilder.tsx`. Build gate only.

- [ ] **Step 1:** Add the option actions to the action `<select>` (after the existing close/adjust_tp/adjust_sl options):
```tsx
<optgroup label="Options">
  <option value="buy_call">Buy Call</option>
  <option value="buy_put">Buy Put</option>
  <option value="sell_covered_call">Sell Covered Call</option>
  <option value="sell_cash_secured_put">Sell Cash-Secured Put</option>
  <option value="buy_protective_put">Buy Protective Put</option>
  <option value="open_bull_call_spread">Bull Call Spread</option>
  <option value="open_bear_put_spread">Bear Put Spread</option>
  <option value="open_bear_call_spread">Bear Call Spread</option>
  <option value="open_straddle">Straddle</option>
  <option value="open_strangle">Strangle</option>
  <option value="close_option">Close Option</option>
</optgroup>
```
- [ ] **Step 2:** When `exitCond.action` is an option action (helper `isOptionAction(a)` = the set above minus `close_option`), render the selection-param inputs (mirror the existing numeric-operand + optimize-range pattern at 626-699):
  - Strike method `<select>`: `delta` / `percent_otm` / `consensus_target`.
  - Strike param `<input number>` (label "Δ target" when delta, "% OTM" when percent_otm; hidden for consensus_target) + an Optimize checkbox + min/max/step range panel (reuse the exact range-panel JSX).
  - DTE min / DTE max `<input number>` + an Optimize checkbox + range for DTE.
  - Sizing `<input number>` ("% of equity").
  All via `updateExitCondition(index, {...})`.
- [ ] **Step 3:** `npm run build` — verify it compiles (no unused vars; `import type` where needed).
- [ ] **Step 4: Commit** — `git commit -m "feat(options-bt): option actions + selection-param inputs in the rule action picker"`

## Task 3: Carry option-action fields through the payloads (Backtesting.tsx)

**Files:** Modify `frontend/src/pages/Backtesting.tsx`. Build gate.

- [ ] **Step 1:** In the **run/ML** mapping (531-541) and **save** mapping (702-711), add the new fields (camelCase in the run payload to match the existing `actionValue*` convention; snake_case in the save payload to match `action_value*`). Example additions to the save mapping:
```ts
option_strategy: ec.optionStrategy,
option_strike_method: ec.optionStrikeMethod,
option_strike_param: ec.optionStrikeParam,
option_dte_min: ec.optionDteMin,
option_dte_max: ec.optionDteMax,
option_sizing: ec.optionSizing,
option_strike_param_optimize: ec.optionStrikeParamOptimize,
option_strike_param_min: ec.optionStrikeParamMin,
option_strike_param_max: ec.optionStrikeParamMax,
option_strike_param_step: ec.optionStrikeParamStep,
option_dte_optimize: ec.optionDteOptimize,
option_dte_min_range: ec.optionDteMinRange,
option_dte_max_range: ec.optionDteMaxRange,
option_dte_step: ec.optionDteStep,
```
(The expert path at 573 sends `exitConditions` as-is — ensure the backend create route's exit-rule normalization maps camelCase→snake_case, OR send snake_case there too; match whatever `_create_daily_expert_backtest` expects — `grep -n "exit_conditions\|exit_rules" backend/app/api/backtests.py`.)
- [ ] **Step 2:** `npm run build` → green.
- [ ] **Step 3: Commit** — `git commit -m "feat(options-bt): option-action fields in run/save/expert payloads"`

## Task 4: Optimize genes for option selection params (param-space)

**Files:** Modify `backend/app/services/strategy_param_space.py`. Test: `backend/tests/test_strategy_param_space_options.py`.

- [ ] **Step 1: Failing test**
```python
def test_option_selection_params_become_genes():
    from app.services.strategy_param_space import build_param_space  # the public entry; confirm name
    class S:  # minimal strategy stub with one option exit rule
        buy_entry_conditions = None; sell_entry_conditions = None; entry_conditions = None
        exit_conditions = [{"id":"o1","action":"buy_call","option_strategy":"buy_call",
            "option_strike_method":"delta","option_strike_param":0.3,
            "option_strike_param_optimize":True,"option_strike_param_min":0.2,
            "option_strike_param_max":0.4,"option_strike_param_step":0.05,
            "option_dte_optimize":True,"option_dte_min_range":20,"option_dte_max_range":45,"option_dte_step":5}]
        initial_tp_optimize=False; initial_sl_optimize=False
    space = build_param_space(S())
    assert "exit:o1:option_delta" in space and space["exit:o1:option_delta"]["max"] == 0.4
    assert "exit:o1:option_dte" in space and space["exit:o1:option_dte"]["type"] == "int"
```
(Confirm the real entry-point name via `grep -n "def .*param_space\|def collect\|def build" strategy_param_space.py`; adjust the import.)
- [ ] **Step 2: Run it, verify it fails**.
- [ ] **Step 3: Implement** — in `_collect_conditions`, for each option exit rule add genes when its optimize flags are set:
```python
    if eid and exit_rule.get("option_strike_param_optimize"):
        out[f"exit:{eid}:option_delta"] = _range_entry(
            exit_rule.get("option_strike_param_min"), exit_rule.get("option_strike_param_max"),
            exit_rule.get("option_strike_param_step"), is_int=False)
    if eid and exit_rule.get("option_dte_optimize"):
        out[f"exit:{eid}:option_dte"] = _range_entry(
            exit_rule.get("option_dte_min_range"), exit_rule.get("option_dte_max_range"),
            exit_rule.get("option_dte_step"), is_int=True)
```
In the **decode** (236-241) handle the new fields, and in **apply** (248-261) write them back onto the rule:
```python
        # decode:
        elif key.startswith("exit:") and field == "option_delta": exit_opt_delta_by_id[eid] = val
        elif key.startswith("exit:") and field == "option_dte":   exit_opt_dte_by_id[eid] = val
        # apply (inside the exit-rule loop):
        if eid in exit_opt_delta_by_id: rule["option_strike_param"] = exit_opt_delta_by_id[eid]
        if eid in exit_opt_dte_by_id:   rule["option_dte_min"] = rule["option_dte_max"] = int(exit_opt_dte_by_id[eid])
```
(Match the existing split/parse style; `field` parsing already does `key.split(":",2)`.)
- [ ] **Step 4: Run tests + the existing param-space suite**, verify pass.
- [ ] **Step 5: Commit** — `git commit -m "feat(options-bt): optimizable option selection params (delta/DTE genes)"`

## Task 5: Map option-action exit rules → option `TradeAction`s (ruleset seeding)

**Files:** Modify the exit-rule→action seeding (locate: `grep -rn "seed_ruleset_from_tree" BA2TradeCommon BA2TestPlatform/backend` and the place equity exit actions `close`/`adjust_tp`/`adjust_sl` become `EventAction`/`TradeAction`s). Test: `backend/tests/backtest/test_option_rule_wiring.py`.

This is the **critical wiring**: when the backtest seeds a ruleset from the strategy's exit rules, an exit rule whose `action`/`option_strategy` is an option action must produce the matching option `TradeAction` (e.g. `buy_call` → `BuyCallAction`) constructed with `strike_method=option_strike_method`, `strike_param=option_strike_param`, `dte_min=option_dte_min`, `dte_max=option_dte_max`, `sizing=option_sizing`. The `TradeActionEvaluator` then runs it, calling the now-options-capable account.

- [ ] **Step 1: Failing test** — seed a ruleset from an exit rule `{action:"buy_call", option_strike_method:"delta", option_strike_param:0.3, option_dte_min:20, option_dte_max:45, option_sizing:5}`; assert the resulting action object is a `BuyCallAction` with those ctor params. (If the seeding is data-driven via `EventAction` rows, assert the `EventAction.action_type`/params instead, then that `TradeActionEvaluator` builds a `BuyCallAction` from it.)
```python
def test_buy_call_exit_rule_builds_buycall_action():
    from <seeding_module> import build_action_for_exit_rule   # confirm/define the seam
    act = build_action_for_exit_rule({"action":"buy_call","option_strategy":"buy_call",
        "option_strike_method":"delta","option_strike_param":0.3,"option_dte_min":20,
        "option_dte_max":45,"option_sizing":5.0}, instrument="AAPL", account=FakeOptAccount())
    from ba2_common.core.TradeActions import BuyCallAction
    assert isinstance(act, BuyCallAction)
    assert act.strike_method == "delta" and act.strike_param == 0.3 and act.dte_min == 20 and act.sizing == 5.0
```
- [ ] **Step 2: Run it, verify it fails**.
- [ ] **Step 3: Implement** — add an action-type→class map for the option actions and construct the right `TradeAction` with the selection params, alongside the existing equity action mapping:
```python
_OPTION_ACTION_CLASSES = {
    "buy_call": BuyCallAction, "buy_put": BuyPutAction,
    "sell_covered_call": SellCoveredCallAction, "sell_cash_secured_put": SellCashSecuredPutAction,
    "buy_protective_put": BuyProtectivePutAction, "open_bull_call_spread": OpenBullCallSpreadAction,
    "open_bear_put_spread": OpenBearPutSpreadAction, "open_bear_call_spread": OpenBearCallSpreadAction,
    "open_straddle": OpenStraddleAction, "open_strangle": OpenStrangleAction,
    "close_option": CloseOptionAction,
}
def build_action_for_exit_rule(rule, *, instrument, account, **ctx):
    a = rule.get("option_strategy") or rule.get("action")
    cls = _OPTION_ACTION_CLASSES.get(a)
    if cls is None:
        return None  # fall through to the existing equity action handling
    return cls(instrument_name=instrument, account=account, order_recommendation=ctx.get("rec"),
        strike_method=rule.get("option_strike_method"), strike_param=rule.get("option_strike_param"),
        dte_min=rule.get("option_dte_min"), dte_max=rule.get("option_dte_max"),
        sizing=rule.get("option_sizing"), min_open_interest=rule.get("option_min_oi"),
        max_spread_pct=rule.get("option_max_spread_pct"))
```
Wire this into the existing seeding/evaluation path so option exit rules dispatch here and equity rules keep their current behavior. (Confirm the exact ctor signature for spreads/straddles — they accept the same base ctor; spread "param" maps to `strike_param`/`_spread_params()` as in `TradeActions.py`.)
- [ ] **Step 4: Run tests + suite, verify pass**.
- [ ] **Step 5: Commit** — `git commit -m "feat(options-bt): seed option exit rules into option TradeActions"`

## Task 6: `uses_options` detection + cache/underlying derivation (handler)

**Files:** Modify `backend/app/services/backtest/daily_backtest_handler.py`. Test: `backend/tests/backtest/test_option_rule_wiring.py::test_uses_options_detection`.

- [ ] **Step 1: Failing test**
```python
def test_uses_options_detection():
    from app.services.backtest.daily_backtest_handler import strategy_uses_options
    assert strategy_uses_options({"exit_conditions":[{"action":"buy_call"}]}) is True
    assert strategy_uses_options({"exit_conditions":[{"action":"close"}]}) is False
```
- [ ] **Step 2: Run it, verify it fails**.
- [ ] **Step 3: Implement** `strategy_uses_options(payload)` (True if any exit/RM rule `action`/`option_strategy` is in the option action set). When True: call the Plan-1 `validate_options_window(start, uses_options=True)`, set `config["options_cache_db"]` (default cache path; or from payload), derive the underlyings from the universe, and inject the `HistoricalOptionsProvider` (Plan 1 Task 9 seam).
- [ ] **Step 4: Run tests + suite, verify pass**.
- [ ] **Step 5: Commit** — `git commit -m "feat(options-bt): detect option strategies + wire the options cache/provider in the handler"`

## Task 7: Rule-driven e2e

**Files:** Test: `backend/tests/backtest/test_options_rule_e2e.py`.

- [ ] **Step 1: Write e2e** — full path: a strategy payload with an exit rule `buy_call` (delta 0.3, DTE 20-45, sizing 5%), a fixture options cache + underlying OHLCV (ends ITM), run the daily engine, assert: the rule fired → a call was bought (fills off the premium bar), marked per bar, and at expiry exercised to a share position; equity reflects it. Reuse the Plan-1 fixture cache + the engine harness.
- [ ] **Step 2: Run it, verify it fails** (until 1-6 land), then **make it pass**.
- [ ] **Step 3: Full suite** — `./venv/bin/python -m pytest tests/backtest -q` + `cd frontend && npm run build` → all green.
- [ ] **Step 4: Commit** — `git commit -m "test(options-bt): rule-driven option backtest e2e (UI payload -> fill -> expiry)"`

---

## Self-Review

**Spec coverage (§5 + the UI parts of §6/§8):** option actions in the rule picker → Task 2; carried through payloads → Task 3; model (TS+Pydantic) → Task 1; optimizable selection params → Task 4; rule→TradeAction seeding (the engine actually firing them) → Task 5; uses_options detection + provider wiring → Task 6; rule-driven e2e → Task 7. ✓

**Placeholder scan:** real code in every code step. Lookups the implementer must confirm against the codebase (each names its grep): the `strategy_param_space` public entry name (Task 4), the ruleset-seeding seam + whether actions are class-built or `EventAction`-row-driven (Task 5), and the create-route exit-rule normalization for the expert path (Task 3). These are integration-point confirmations, not design gaps.

**Type consistency:** the option-rule field names are consistent camelCase (TS) ↔ snake_case (Pydantic/param-space) across Tasks 1/3/4/5; action-value optimize fields reuse the existing `action_value*` convention; gene names `exit:<id>:option_delta|option_dte` match between emit (Task 4) and decode/apply (Task 4) and consume nothing the backend doesn't set.

---

## Execution Handoff

Both plans complete (`...-backend.md` + this). Execute Plan 1 fully first, then Plan 2. Choose:
1. **Subagent-Driven (recommended)** — fresh subagent per task, two-stage review.
2. **Inline Execution** — batch with checkpoints.
