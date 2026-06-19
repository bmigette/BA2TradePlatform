# Exit / Open-Positions Ruleset UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. Spec: `docs/exit-ruleset-ui-requirements.md`.

**Goal:** A full exit-ruleset editor in the backtest UI so users add/remove/reorder exit rules, build per-rule AND-conditions from the supported vocabulary, pick an action (close/sell/adjust_tp/adjust_sl + reference_value/value%, and the option actions), toggle every optimizable knob (with a live gene-count preview), load presets, import a live expert's ruleset, validate, and read back the resolved ruleset from a finished optimization.

**Architecture:** The backend exit-rule data model + optimize-gene plumbing already exist (`strategy_param_space.py`); this plan adds 2 missing Pydantic fields, three small read-only endpoints (vocabulary, presets, import-from-live), and the React editor. The UI produces the existing `exit_conditions` JSON shape the backtest/optimizer already consume. Reuses the existing `ConditionBuilder.tsx` (`ConditionNode`/`ConditionGroup`/`ExitConditionSet`, `ExitConditionsBuilder`).

**Tech Stack:** FastAPI/Pydantic (`./venv/bin/python`), React 18 + TS + Vite (`cd frontend && npm run build` gate; `import type`; `git add -f` for `src/lib`/`src/components` if needed), pytest.

**Verified data model (do NOT re-derive):**
- `ExitCondition` (`backend/app/api/strategies.py`): `{id, name?, conditions: ConditionBase, action: str, action_value?, action_value_optimize=False, action_value_min/max/step?, option_*…}`. **Missing:** `toggle_optimize: bool` and `reference_value: Optional[str]`.
- `ConditionBase`: `{id, field?, field_type?, comparison?, value?, optimize/optimize_enabled, value_min/max/step?, toggle_optimize?, confirmation_*?, operator? (AND/OR), conditions?: [ConditionBase]}`. Group node = `{id, operator, conditions[]}`; numeric leaf = `{id, field, comparison, value, optimize_enabled, value_min/max/step, toggle_optimize}`; flag leaf = `{id, field}`.
- Vocabulary (`ba2_common/core/types.py`): `ExpertEventType` F_* flags + N_* numerics (lists in the spec); `ExpertActionType` (close/sell/adjust_take_profit/adjust_stop_loss + option actions); `ReferenceValue` (order_open_price/current_price/expert_target_price) + `get_reference_value_options()`.
- Genes (`strategy_param_space.py` `_walk_condition_nodes`/`_collect_conditions`): `cond:<id>:value` (numeric optimize), `cond:<id>:confirmation_bars`, `cond:<id>:enabled` (node toggle_optimize), `exit:<id>:action_value` (action_value_optimize), `exit:<id>:enabled` (rule toggle_optimize).
- TS (`frontend/src/components/ConditionBuilder.tsx`): `ConditionNode{id,field,fieldType,comparison,value,optimizeEnabled,toggleOptimize?,valueMin/Max/Step,confirmation*}`, `ConditionGroup{id,operator,conditions[]}`, `ExitConditionSet{…action,actionValue*,option*}` (already has option fields from Plan-2 T1). **Missing in TS:** `toggleOptimize` + `referenceValue` on `ExitConditionSet`.
- Endpoints: `POST/PUT /api/strategies` (accept `exit_conditions`), `GET /api/strategies/{id}/export-rules?which=exit`, `POST /api/strategies/import-rules`, `POST /api/strategies/{id}/optimize`, `POST /api/backtests` (daily_expert accepts `exit_conditions`).

**Scope decisions (locked with the user):**
- **Vocabulary/actions/reference-values/presets:** exposed via a NEW read-only API endpoint derived from the enums (single source of truth, no UI drift).
- **Import-from-live:** v1 = a new test-platform endpoint that reads the **live trade DB** open-positions ruleset for an expert IF the live DB is reachable; if not configured, the UI falls back to the existing JSON-paste import (`/api/strategies/import-rules`). (The direct live-DB read is gated on a configured live-DB path; degrade gracefully.)
- **OR-nesting** in the condition builder is deferred (backend ANDs leaves today); the editor builds an AND-group per rule (matches current backend).

---

## File Structure
- **Modify** `backend/app/api/strategies.py` — add `toggle_optimize` + `reference_value` to `ExitCondition` (+ `ConditionBase.reference_value` not needed; ref is rule-level).
- **Create** `backend/app/api/ruleset_meta.py` — `GET /api/ruleset/vocabulary`, `GET /api/ruleset/exit-presets`, `GET /api/experts/{expert_id}/open-positions-ruleset` (import-from-live, graceful). Register the router in `app/main.py`.
- **Create** `backend/app/services/ruleset_presets.py` — the 4 default exit rules in the API `ExitCondition` shape (converted from the launcher seed shape).
- **Modify** `frontend/src/lib/btApi.ts` — typed fetchers for vocabulary/presets/import-live + the gene-count types.
- **Create** `frontend/src/lib/geneCount.ts` — pure gene-count + search-space estimator (mirrors `strategy_param_space`).
- **Modify** `frontend/src/components/ConditionBuilder.tsx` — full condition-leaf editors (flag vs numeric+operator), per-node optimize+toggle, the action picker (close/sell/adjust_tp/adjust_sl + reference_value + value%, + option actions), per-rule toggle_optimize, add/remove/reorder, validation hints.
- **Create** `frontend/src/components/GeneCountPreview.tsx`, `frontend/src/components/ExitPresetPicker.tsx`, `frontend/src/components/ResolvedRulesetView.tsx`.
- **Modify** `frontend/src/pages/Backtesting.tsx` — wire presets/import/gene-preview into the strategy section; carry `toggleOptimize`/`referenceValue` in payloads; read-back panel in the optimization-results view.
- **Tests:** `backend/tests/test_ruleset_meta.py`, `backend/tests/test_exit_condition_fields.py`; frontend `frontend/src/lib/geneCount.test.ts` (vitest) + build gate.

---

## PHASE A — Backend support

### Task A1: `ExitCondition` gains `toggle_optimize` + `reference_value`
**Files:** Modify `backend/app/api/strategies.py`; also `backend/app/api/backtests.py` if its exit-rule normalization strips unknown keys (check). Test: `backend/tests/test_exit_condition_fields.py`.
- [ ] **Step 1 — failing test**
```python
# backend/tests/test_exit_condition_fields.py
from app.api.strategies import ExitCondition
_C = {"id":"g","operator":"AND","conditions":[]}
def test_toggle_optimize_and_reference_value_accepted():
    ec = ExitCondition(id="r1", conditions=_C, action="adjust_stop_loss",
        reference_value="order_open_price", toggle_optimize=True, action_value=-10.0)
    assert ec.toggle_optimize is True and ec.reference_value == "order_open_price"
def test_defaults_for_equity_rule():
    ec = ExitCondition(id="r2", conditions=_C, action="close")
    assert ec.toggle_optimize is False and ec.reference_value is None
```
- [ ] **Step 2** — `cd backend && ./venv/bin/python -m pytest tests/test_exit_condition_fields.py -q` → FAIL.
- [ ] **Step 3 — implement**: add to `ExitCondition`:
```python
    toggle_optimize: bool = False                 # -> exit:<id>:enabled gene
    reference_value: Optional[str] = None         # order_open_price|current_price|expert_target_price (adjust actions)
```
Confirm `_collect_conditions` already reads `exit_rule.get("toggle_optimize")` (it does) so the gene appears once the field round-trips. Check `backtests.py` `_create_daily_expert_backtest` forwards the full exit-rule dicts (it passes `exit_conditions`/`exit_rules` through — confirm `reference_value`/`toggle_optimize` aren't dropped; if it rebuilds dicts, add the keys).
- [ ] **Step 4** — test PASS + full suite `./venv/bin/python -m pytest tests -q -k "strategy or exit or param_space" --ignore=tests/test_per_target_weights.py` green.
- [ ] **Step 5** — commit `git add backend/app/api/strategies.py backend/tests/test_exit_condition_fields.py && git commit -m "feat(exit-ui): ExitCondition gains toggle_optimize + reference_value"`.

### Task A2: ruleset-vocabulary + presets API
**Files:** Create `backend/app/services/ruleset_presets.py`, `backend/app/api/ruleset_meta.py`; register router in `app/main.py`. Test: `backend/tests/test_ruleset_meta.py`.
- [ ] **Step 1 — failing test**
```python
# backend/tests/test_ruleset_meta.py
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)
def test_vocabulary_lists_flags_numerics_actions_refs():
    r = client.get("/api/ruleset/vocabulary"); assert r.status_code == 200
    v = r.json()
    assert "bearish" in [f["value"] for f in v["flags"]]
    assert "profit_loss_percent" in [n["value"] for n in v["numerics"]]
    assert set(["close","sell","adjust_take_profit","adjust_stop_loss"]).issubset({a["value"] for a in v["actions"]})
    assert "order_open_price" in v["reference_values"]
    assert ">" in v["operators"]
def test_exit_presets_are_valid_exitconditions():
    from app.api.strategies import ExitCondition
    r = client.get("/api/ruleset/exit-presets"); assert r.status_code == 200
    presets = r.json()["presets"]
    assert len(presets) >= 4
    for p in presets:
        ExitCondition(**p["rule"])   # each preset rule validates against the model
```
- [ ] **Step 2** — FAIL (404 / no router).
- [ ] **Step 3 — implement**:
  - `ruleset_presets.py`: `EXIT_PRESETS = [ {"key","label","rule": <ExitCondition-shaped dict>} ... ]` for bearish-close, downgrade-close, break-even-profit-lock (adjust_stop_loss, reference_value=order_open_price, action_value_optimize, a profit_loss_percent>X condition), time-exit (days_opened>N). Use the API shape: `action` (not action_type), `conditions` = `{id, operator:"AND", conditions:[ {id, field, comparison, value, optimize_enabled, value_min/max/step} | {id, field} ]}`, `toggle_optimize: True`.
  - `ruleset_meta.py`: a router with:
    - `GET /api/ruleset/vocabulary` → `{flags:[{value,label}], numerics:[{value,label}], operators:[">",">=","<","<=","==","!=","between"], actions:[{value,label,is_option,needs_reference}], reference_values:{value:label}}` derived from `ExpertEventType` (split F_*/N_* by prefix), `ExpertActionType` + `get_option_action_values()`/`is_option_action()`, `get_reference_value_options()`. Provide human labels (title-case the value as a default).
    - `GET /api/ruleset/exit-presets` → `{presets: EXIT_PRESETS}`.
    - `GET /api/experts/{expert_id}/open-positions-ruleset` → import-from-live (Task A3).
  - Register the router in `app/main.py` (mirror how other api routers are included: `app.include_router(ruleset_meta.router)`).
- [ ] **Step 4** — tests PASS; `./venv/bin/python -m pytest tests/test_ruleset_meta.py -q` green.
- [ ] **Step 5** — commit `git add backend/app/services/ruleset_presets.py backend/app/api/ruleset_meta.py backend/app/main.py backend/tests/test_ruleset_meta.py && git commit -m "feat(exit-ui): ruleset vocabulary + exit presets API"`.

### Task A3: import-from-live endpoint (graceful)
**Files:** Add the route in `ruleset_meta.py`. Test: extend `test_ruleset_meta.py`.
Reads a live expert's open_positions ruleset and returns it as a list of `ExitCondition`-shaped rules the UI can load. The live trade DB is a SEPARATE database; access is OPTIONAL.
- [ ] **Step 1 — explore**: how (if at all) the test platform can reach the live trade DB. `grep -rn "live.*db\|trade.*db\|LIVE_DB\|ba2_trade_platform\|open_positions_ruleset" backend/app | head`. Determine: is there a configured path to the live `ba2_trade_platform` sqlite + the `Ruleset`/`EventAction` models (in `ba2_common`)? If NOT reachable, the endpoint returns `503 {"detail":"live DB not configured"}` and the UI falls back to JSON paste.
- [ ] **Step 2 — failing test** (no-live-DB path): `GET /api/experts/999/open-positions-ruleset` → 404 (expert not found) or 503 (live DB not configured); assert the documented status + body.
- [ ] **Step 3 — implement**: if a live-DB path is configured (env `BA2_LIVE_DB`/config), open it read-only, load the expert's `open_positions_ruleset_id` → its `EventAction`s, and convert each EventAction (live shape: conditions tree + action_type + reference_value + value) into an `ExitCondition`-shaped dict, marking numeric conditions + adjust action_values `optimize=True` with sensible default min/max/step (e.g. value±50%, step=value/5). Return `{rules: [...]}`. If unreachable → `raise HTTPException(503, "live DB not configured; paste the ruleset JSON instead")`.
- [ ] **Step 4** — test PASS + suite green.
- [ ] **Step 5** — commit `git commit -m "feat(exit-ui): import-from-live open_positions ruleset (graceful, optional live DB)"`.

---

## PHASE B — Frontend editor

### Task B1: vocabulary/preset/import fetchers + gene-count util
**Files:** Modify `frontend/src/lib/btApi.ts`; Create `frontend/src/lib/geneCount.ts` + `geneCount.test.ts` (vitest). Build + vitest gate.
- [ ] Add `btApi` fetchers: `getRulesetVocabulary()`, `getExitPresets()`, `importLiveRuleset(expertId)` (typed). Add TS types `Vocabulary`, `ExitPreset`.
- [ ] Create `geneCount.ts` pure fn `countGenes(buyTree, sellTree, exitRules) -> {genes: string[], searchSpace: number}` mirroring `strategy_param_space`: +1 gene per numeric condition with `optimizeEnabled` (`cond:<id>:value`), +1 per node `toggleOptimize` (`cond:<id>:enabled`), +1 per exit rule `actionValueOptimize` (`exit:<id>:action_value`), +1 per exit rule `toggleOptimize` (`exit:<id>:enabled`), +1 per option `optionStrikeParamOptimize`/`optionDteOptimize`. `searchSpace` = Π over genes of `floor((max-min)/step)+1` (toggles = 2). Vitest test pins counts for a known tree.
- [ ] Build + `npm run test` (vitest) green. Commit `feat(exit-ui): vocabulary/preset/import fetchers + gene-count estimator`.

### Task B2: condition-leaf editors (flag vs numeric+operator) from vocabulary
**Files:** Modify `ConditionBuilder.tsx`. Build gate.
- [ ] Fetch the vocabulary (prop or hook). For a condition leaf, the field `<select>` is grouped Flags vs Numerics from the vocabulary. When a FLAG field is chosen → render no operator/value (flag leaf `{id, field}`). When a NUMERIC field is chosen → render operator `<select>` (from vocabulary operators) + value input; `between` shows two values. Each numeric leaf gets the existing optimize toggle + value min/max/step AND a per-node `toggleOptimize` checkbox (cond:<id>:enabled). Add/remove leaves within the rule's AND group. Keep the existing entry-condition builder behavior intact (this generalizes it).
- [ ] Build green. Commit `feat(exit-ui): vocabulary-driven condition leaves (flags + numeric operators) with per-node optimize + toggle`.

### Task B3: action picker — close/sell/adjust + reference_value/value% (+ option actions)
**Files:** Modify `ConditionBuilder.tsx`. Build gate.
- [ ] Action `<select>` from vocabulary actions, grouped Equity (close/sell/adjust_take_profit/adjust_stop_loss) vs Options (the option actions). For `adjust_take_profit`/`adjust_stop_loss`: show a `reference_value` `<select>` (order_open_price/current_price/expert_target_price) + the value% input + its optimize toggle + min/max/step (existing `actionValue*`). For option actions: the selection-param inputs (delta/%OTM/DTE/sizing) — this folds in Plan-2 T2. Persist `referenceValue` on the rule.
- [ ] Build green. Commit `feat(exit-ui): action picker with reference_value + value% (and option actions)`.

### Task B4: per-rule add/remove/reorder + toggle_optimize
**Files:** Modify `ConditionBuilder.tsx` (`ExitConditionsBuilder`). Build gate.
- [ ] Rule list: Add rule, Remove rule, Move up/Move down (reorder — order matters, first match wins). Each rule has a `toggleOptimize` checkbox (exit:<id>:enabled — the optimizer can drop the whole rule). Rule header shows name (editable). 
- [ ] Build green. Commit `feat(exit-ui): add/remove/reorder exit rules + per-rule optimize toggle`.

### Task B5: payloads carry toggleOptimize + referenceValue
**Files:** Modify `frontend/src/pages/Backtesting.tsx`. Build gate.
- [ ] In the run/save/expert exit-rule mappings add `toggle_optimize: ec.toggleOptimize` and `reference_value: ec.referenceValue` (snake_case for save/expert; keep camelCase where the run path uses it). Ensure round-trip on load (resolve a saved strategy → fill `toggleOptimize`/`referenceValue`).
- [ ] Build green. Commit `feat(exit-ui): carry toggle_optimize + reference_value through payloads`.

### Task B6: gene-count preview
**Files:** Create `GeneCountPreview.tsx`; wire into `Backtesting.tsx` strategy section. Build gate.
- [ ] A small panel using `countGenes(...)` that live-updates as the user toggles optimization: shows gene count + an estimated search-space size (e.g. "7 genes · ~3.2M combinations") with a hint about population/generations. Recompute on every rule/condition change.
- [ ] Build green. Commit `feat(exit-ui): live gene-count + search-space preview`.

### Task B7: presets
**Files:** Create `ExitPresetPicker.tsx`; wire into `Backtesting.tsx`. Build gate.
- [ ] A "Presets" control listing `getExitPresets()` (bearish-close, downgrade-close, break-even profit-lock, time-exit); clicking one APPENDS its rule (API-shaped → mapped to `ExitConditionSet`) to the exit-rule list. 
- [ ] Build green. Commit `feat(exit-ui): one-click exit-rule presets`.

### Task B8: import-from-live (graceful fallback to JSON paste)
**Files:** Modify `ConditionBuilder.tsx`/`Backtesting.tsx`; reuse `RuleIO.tsx`. Build gate.
- [ ] An "Import from live expert" button → calls `importLiveRuleset(expertId)`; on success, loads the returned rules into the editor (marked optimizable). On 503/unconfigured → fall back to the existing JSON paste (`RuleIO` import-rules), with a note. 
- [ ] Build green. Commit `feat(exit-ui): import a live expert's open_positions ruleset (with JSON-paste fallback)`.

### Task B9: validation
**Files:** Modify `ConditionBuilder.tsx`. Build gate.
- [ ] Client-side validation/warnings: reject unknown field/action (shouldn't happen with vocabulary-driven selects, but guard on load/import); warn when an adjust rule has no protective counterpart (an adjust_stop_loss with no SL ever set); warn when a rule can never trigger (e.g. contradictory flags, or an empty condition group on a non-unconditional action). Show inline warning chips; don't block running (warnings, not errors) except unknown field/action.
- [ ] Build green. Commit `feat(exit-ui): exit-rule validation + warnings`.

### Task B10: resolved-ruleset read-back
**Files:** Create `ResolvedRulesetView.tsx`; wire into the optimization-results view in `Backtesting.tsx`. Build gate.
- [ ] EXPLORE: how a finished optimization's best params are returned (the optimize result / best_params endpoint; `grep -n "best_param\|bestParams\|optimization.*result\|decode_params" backend/app/api/*.py frontend/src/...`). 
- [ ] Render the RESOLVED exit ruleset for a completed optimization: apply best params to the strategy's exit rules (toggled-off rules greyed/hidden via `exit:<id>:enabled`=0; tuned `cond:<id>:value`/`exit:<id>:action_value` filled in). If the backend already returns resolved params, use them; else apply the genes client-side using the same mapping as `geneCount`/`strategy_param_space`. 
- [ ] Build green. Commit `feat(exit-ui): read-back of the resolved exit ruleset from a finished optimization`.

---

## Self-Review
**Spec coverage (9 requirements):** (1) editor → B2-B4; (2) add/remove/reorder → B4; (3) condition builder → B2; (4) action picker + reference_value/value% → B3; (5) optimize toggles everywhere → B2 (cond value+enabled), B3 (action_value), B4 (rule enabled), A1 (fields); (6) gene-count preview → B1+B6; (7) presets + import-from-live → A2/B7 + A3/B8; (8) validation → B9; (9) read-back → B10. Backend gaps closed: A1 (fields), A2 (vocabulary/presets), A3 (import-live).
**Placeholder scan:** backend tasks have full code/tests; frontend tasks specify exact shapes, the vocabulary-driven selects, the gene-count mirror, and integration points, with explore steps where exact existing-file wiring must be confirmed (B10 results endpoint; A1 backtests normalization; A3 live-DB reachability). These are integration confirmations, not design gaps.
**Type consistency:** `toggle_optimize`/`reference_value` (snake) ↔ `toggleOptimize`/`referenceValue` (camel) consistent across A1/B3/B4/B5; gene names match `strategy_param_space` in B1/B6/B10; vocabulary field values come from the single API source (no drift).

## Execution Handoff
Subagent-driven: Phase A (3 tasks) then Phase B (10 tasks); the option-action UI from Plan 2 (T2/T3) is folded into B3/B5, so after this, Plan 2's remaining backend tasks (param-space option genes, rule→TradeAction seeding, uses_options handler, options rule-e2e) finish the options path.
