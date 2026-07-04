# Entry-Time TP/SL Bracket Actions Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the test platform attach TP/SL at entry the same way the live platform does — as `adjust_take_profit`/`adjust_stop_loss` **actions on the entry rule itself** (live example: `BUY_Longterm_70pctConfidence_10pctProfit`, eventaction id=1 in the prod DB: `action_0=buy`, `action_1=adjust_take_profit`) — using the SAME rule/action representation and the SAME `action_from_rule` conversion exit rules already use, NOT a bespoke set of test-platform-only scalar fields.

**Architecture:** The live platform's `TradeActionEvaluator` already implements the whole mechanism: Phase 1 creates the PENDING entry order, **Phase 1.5 eagerly creates the `Transaction`** (`TradeActionEvaluator.py:368-391` — `self.account._create_transaction_for_order(order)`), and Phase 2 computes TP/SL prices (`TradeActions.py:903 compute_price`) and calls `account.adjust_tp_sl(...)`. The backtest entry path (`daily_engine.py:826-843 _run_expert_entry`) already drives this exact evaluator.

**REVISION (2026-07-04) — unify the config surface, don't bolt on new fields:** Tasks 1-3 below are DONE and committed (`9f28fee`, `32631b8`, `e3894e1`, and Task 3's WIP) and remain valid at the engine level. But the ORIGINAL Tasks 4-7 (superseded, struck through further down) would have fed the seeder from brand-new test-platform-only `Strategy` columns (`entry_bracket` boolean + reusing the already-dead `initial_tp_percent`/`initial_sl_percent`/`initial_tp_reference`). The user explicitly rejected this: *"TP/SL should be set via rules not via another field specific to test. Rules should be unified... delete those tp/sl fields, they're leftover causing confusion."* The corrected design:

- **Delete** `Strategy.initial_tp_percent/_optimize/_min/_max/_step`, `initial_sl_percent/_optimize/_min/_max/_step` (10 columns) and every dead reference to them (frontend fields, `_collect_tp_sl`, `initial_tp_reference`/`initial_tp_ref` config keys, the `tp`/`sl` GA gene namespace). These were the "5.0/2.0 dead defaults" load-bearing on nothing since `_apply_initial_brackets` was removed; the entry-bracket feature must NOT resurrect them under a new name.
- **Add** `Strategy.entry_actions` — a JSON column, list of rule dicts, in the **EXACT SAME SHAPE** `exit_conditions` rows already use (`{id, action_type, reference_value, action_value, action_value_optimize, action_value_min, action_value_max, action_value_step, enabled, toggle_optimize}`), minus a nested `conditions` sub-tree (an entry action fires on the SAME gate as the buy/sell action — it has no condition of its own, exactly like live's `action_0`/`action_1` sharing one `triggers` dict).
- **Revise Task 1's seeder** (`_entry_actions`/`seed_ruleset_from_tree`) to accept `entry_actions: list[dict] | None` and build each adjust action via `action_from_rule(rule, key=...)` — the SAME shared function `seed_open_positions_ruleset` already calls for exit rules (`rule_builders.py:165`, whose own docstring already says "for one exit/**entry** rule" — it was built generic, just never called from the entry path). This deletes the hand-rolled sign-math `_entry_actions` grew in Task 1 (the sign convention is `action_from_rule`'s/`compute_price`'s job, same as it already is for exit rules — the GA/user supplies a correctly-signed `action_value`, exactly like S3's exit rules already do).
- **Gene collection**: extend `_collect_conditions`/`decode_params` in `strategy_param_space.py` to walk `strategy.entry_actions` with an `entry:<id>:*` namespace, MIRRORING the existing `exit:<id>:*` handling line-for-line (same field names: `action_value_optimize`/`action_value_min/max/step`/`toggle_optimize`/`enabled`). Delete `_collect_tp_sl` entirely — it's superseded by this, not run alongside it.
- **No `entry_bracket` boolean.** Presence of a non-empty `entry_actions` list on a Strategy IS the opt-in signal — exactly how a non-empty `exit_conditions` list is already the only "exit management enabled" signal (no separate flag exists for that either). Every historical Strategy has `entry_actions` absent/empty by construction (new column), so nothing changes for it — the safety goal from the original plan is met MORE simply, with no new flag.

**Tech Stack:** Python (FastAPI backend, SQLModel, pytest), shared `ba2_common` package (evaluator/actions/rule-builders), React+TS frontend (Backtesting.tsx).

**Sign convention** (unchanged fact, now enforced entirely by the EXISTING `action_from_rule`/`compute_price` path, not by bespoke seeder math): for a long, price = reference × (1 + value/100); for a short, reference × (1 − value/100) (`TradeActions.py:936-947`). A TP action's `action_value` should be positive (e.g. `8.0`), an SL action's negative (e.g. `-3.0`) — same convention the GA already applies to exit-rule `action_value`s (see S3's `trail_t1`/`exit_stoploss` values in `ba2test_launcher.py`). With `reference_value="expert_target_price"`, the value is a signed offset-from-target (live's example: `-5.0` = 5% below the analyst target).

**Key files (read these first):**
- `packages/common/ba2_common/core/TradeActionEvaluator.py:314-540` — Phases 1/1.5/2 (shared, DO NOT MODIFY — it already works)
- `packages/common/ba2_common/core/rule_builders.py:165-187` — `action_from_rule` (already supports adjust actions AND is already documented as entry-capable; DO NOT MODIFY)
- `testplatform/backend/app/models/strategy.py:30-41` — the columns to DELETE; `exit_conditions = Column(JSON, nullable=True)` (line 28) is the pattern to mirror for the new `entry_actions` column
- `testplatform/backend/db_migrate/022_drop_strategy_rm_columns.py` — the exact rebuild-table pattern to copy for the new migration (SQLite can't DROP COLUMN reliably)
- `testplatform/backend/app/services/backtest/default_rulesets.py` — `_entry_actions` + `seed_ruleset_from_tree` (REVISE Task 1's version: swap scalar kwargs for `entry_actions: list[dict]` + `action_from_rule`); `seed_open_positions_ruleset` (line 92) is the exit-side pattern to mirror
- `testplatform/backend/app/services/strategy_param_space.py:39-48 _collect_tp_sl` (DELETE), `:126-169 _collect_conditions` (EXTEND with `entry:<id>:*`, mirroring `exit:<id>:*`)
- `testplatform/backend/app/services/backtest/daily_backtest_handler.py`, `testplatform/backend/app/services/strategy_optimization_handler.py`, `testplatform/backend/app/api/backtests.py`, `testplatform/backend/app/services/backtest/rerun_handler.py` — grep-and-delete every `initial_tp_percent`/`initial_sl_percent`/`initial_tp_reference`/`initial_tp_ref` reference; wire `entry_actions`/decoded `entry_rules` through instead (mirror the existing `exit_rules` plumbing exactly)
- `testplatform/ba2test_launcher.py` — S4 template (`_build_strategy_S4`): replace `initial_tp_percent=...,initial_tp_optimize=...` with an `entry_actions=[...]` row
- `testplatform/frontend/src/pages/Backtesting.tsx:691-704 (state), 1542-1552 (serialize), ~2180 (load)` — DELETE the `initialTp*`/`initialSl*` state/UI entirely; ADD an entry-actions builder (reuse `ExitConditionsBuilder`'s pieces)
- `testplatform/backend/app/services/backtest/backtest_account.py:1584-1609` (`WAITING_TRIGGER` promotion — Task 2, DONE), `daily_engine.py` submit call site (Task 3, DONE) — unaffected by this revision, they operate on `Transaction.stop_loss`/`take_profit` regardless of how those got set

Run backend tests from `testplatform/backend` with the TEST venv:
`C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/<file> -v`

---

## Superseded tasks (kept below for history — DO NOT IMPLEMENT AS WRITTEN)

Tasks 4-7 as originally written (entry_bracket flag, `initial_tp_percent`/`initial_sl_percent`/`initial_tp_reference` plumbing, a flat toggle in the frontend) are REPLACED by Tasks 4-8 further below. Task 1's original implementation (scalar `entry_tp_percent`/`entry_sl_percent`/`entry_tp_reference` kwargs) is also being revised in new Task 4. Skip straight to "Task 4: Delete dead TP/SL fields..." below.

---

### Task 1: Seed entry TP/SL adjust actions in the enter-market ruleset

**Files:**
- Modify: `testplatform/backend/app/services/backtest/default_rulesets.py:166-257`
- Test: `testplatform/backend/tests/backtest/test_entry_bracket_seeding.py` (create)

**Step 1: Write the failing test**

```python
"""Entry-bracket seeding: seed_ruleset_from_tree emits adjust_take_profit /
adjust_stop_loss actions on the enter rule when entry TP/SL percents are given
(mirroring the live BUY_Longterm_70pctConfidence_10pctProfit pattern: one
EventAction with action_0=buy + adjust actions), and emits NOTHING extra when
they are None (backward compat: every historical config stays byte-identical)."""
import json

import pytest

from app.services.backtest.default_rulesets import seed_ruleset_from_tree
from ba2_common.core.db import get_instance
from ba2_common.core.models import EventAction, Ruleset
from ba2_common.core.types import ExpertActionType, ReferenceValue


def _actions_of_first_rule(ruleset_id: int) -> dict:
    from sqlmodel import select
    from ba2_common.core.db import get_db
    from ba2_common.core.models import RulesetEventActionLink
    with get_db() as session:
        link = session.exec(select(RulesetEventActionLink).where(
            RulesetEventActionLink.ruleset_id == ruleset_id)).first()
        ea = session.get(EventAction, link.eventaction_id)
        actions = ea.actions
        return json.loads(actions) if isinstance(actions, str) else actions


def test_no_bracket_by_default():
    rid = seed_ruleset_from_tree(None, name="t-nobracket", entry_action={"action_type": "buy"})
    actions = _actions_of_first_rule(rid)
    types = {cfg["action_type"] for cfg in actions.values()}
    assert ExpertActionType.ADJUST_TAKE_PROFIT.value not in types
    assert ExpertActionType.ADJUST_STOP_LOSS.value not in types


def test_bracket_emits_tp_and_sl_actions():
    rid = seed_ruleset_from_tree(
        None, name="t-bracket", entry_action={"action_type": "buy"},
        entry_tp_percent=8.0, entry_sl_percent=3.0,
    )
    actions = _actions_of_first_rule(rid)
    by_type = {cfg["action_type"]: cfg for cfg in actions.values()}
    tp = by_type[ExpertActionType.ADJUST_TAKE_PROFIT.value]
    sl = by_type[ExpertActionType.ADJUST_STOP_LOSS.value]
    # Sign convention per TradeActions.compute_price: long price = ref*(1+value/100),
    # so TP is +8.0 (above entry) and SL is -3.0 (below entry); shorts invert automatically.
    assert tp["value"] == 8.0
    assert sl["value"] == -3.0
    assert tp["reference_value"] == ReferenceValue.ORDER_OPEN_PRICE.value
    assert sl["reference_value"] == ReferenceValue.ORDER_OPEN_PRICE.value
    # The buy action must still be present (Phase 1 creates the order Phase 2 adjusts).
    assert "buy" in {cfg["action_type"] for cfg in actions.values()}


def test_bracket_tp_reference_expert_target_passes_value_signed():
    # S4 semantics: TP anchored on the analyst target; the value is a SIGNED
    # offset-from-target (live example: -5.0 = 5% below target), passed through as-is.
    rid = seed_ruleset_from_tree(
        None, name="t-bracket-ref", entry_action={"action_type": "buy"},
        entry_tp_percent=-5.0, entry_sl_percent=3.0,
        entry_tp_reference=ReferenceValue.EXPERT_TARGET_PRICE.value,
    )
    actions = _actions_of_first_rule(rid)
    by_type = {cfg["action_type"]: cfg for cfg in actions.values()}
    tp = by_type[ExpertActionType.ADJUST_TAKE_PROFIT.value]
    assert tp["value"] == -5.0
    assert tp["reference_value"] == ReferenceValue.EXPERT_TARGET_PRICE.value


def test_sl_only_bracket():
    rid = seed_ruleset_from_tree(
        None, name="t-slonly", entry_action={"action_type": "buy"},
        entry_sl_percent=4.0,
    )
    actions = _actions_of_first_rule(rid)
    types = {cfg["action_type"] for cfg in actions.values()}
    assert ExpertActionType.ADJUST_STOP_LOSS.value in types
    assert ExpertActionType.ADJUST_TAKE_PROFIT.value not in types
```

NOTE: check the actual link-table model name first (`grep -n "class Ruleset" packages/common/ba2_common/core/models.py` — the link model is around line 14-16, named per `ruleset_eventaction_link` table). Adjust the import in `_actions_of_first_rule` to the real class name. Also copy whatever DB fixture pattern `testplatform/backend/tests/backtest/test_screener_genes.py` uses (conftest gives each test a temp DB).

**Step 2: Run it to make sure it fails**

Run: `cd testplatform/backend && C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/backtest/test_entry_bracket_seeding.py -v`
Expected: FAIL — `seed_ruleset_from_tree() got an unexpected keyword argument 'entry_tp_percent'`

**Step 3: Implement**

In `default_rulesets.py`, extend `_entry_actions` (line 166) and `seed_ruleset_from_tree` (line 195):

```python
def _entry_actions(side: str, entry_action: "dict | None" = None,
                   entry_tp_percent: "float | None" = None,
                   entry_sl_percent: "float | None" = None,
                   entry_tp_reference: "str | None" = None) -> dict:
    """... existing docstring, then ADD:

    ENTRY BRACKET (opt-in): when ``entry_tp_percent`` / ``entry_sl_percent`` are given,
    emit adjust_take_profit / adjust_stop_loss actions ALONGSIDE the open action — the
    exact live pattern (BUY_Longterm_70pctConfidence_10pctProfit: action_0=buy +
    action_1=adjust_take_profit). The shared TradeActionEvaluator then runs its
    Phase 1.5 (eager Transaction creation) + Phase 2 (compute + adjust_tp_sl), which
    the backtest entry path already drives — so the bracket attaches at entry with
    live-identical semantics. Sign convention (TradeActions.compute_price):
    long = ref*(1+value/100), short = ref*(1-value/100) — so TP carries
    +entry_tp_percent and SL carries -abs(entry_sl_percent) and both work for both
    sides. With entry_tp_reference="expert_target_price" the TP value passes through
    SIGNED as-is (offset-from-target, negative = below target).
    """
    # ... existing open-action logic unchanged, building `out = {side: {...}}` ...
    # then before returning:
    if entry_tp_percent is not None:
        tp_ref = entry_tp_reference or ReferenceValue.ORDER_OPEN_PRICE.value
        tp_value = float(entry_tp_percent) if tp_ref == ReferenceValue.EXPERT_TARGET_PRICE.value \
            else abs(float(entry_tp_percent))
        out[f"{side}_tp"] = {
            "action_type": ExpertActionType.ADJUST_TAKE_PROFIT.value,
            "reference_value": tp_ref,
            "value": tp_value,
        }
    if entry_sl_percent is not None:
        out[f"{side}_sl"] = {
            "action_type": ExpertActionType.ADJUST_STOP_LOSS.value,
            "reference_value": ReferenceValue.ORDER_OPEN_PRICE.value,
            "value": -abs(float(entry_sl_percent)),
        }
    return out
```

`seed_ruleset_from_tree` gains the same three kwargs (all default `None`) and forwards them into BOTH `_entry_actions("buy", ...)` and `_entry_actions("sell", ...)` calls (lines 243, 254). Import `ReferenceValue` at the top if not already imported. Update the docstring paragraph that currently says "the entry seeder carries no bracket plumbing at all" — it now carries the OPT-IN bracket.

**Step 4: Run tests to verify they pass**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/backtest/test_entry_bracket_seeding.py -v`
Expected: 4 PASS

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/default_rulesets.py testplatform/backend/tests/backtest/test_entry_bracket_seeding.py
git commit -m "feat(backtest): seed opt-in entry TP/SL adjust actions in the enter ruleset (live parity)"
```

---

### Task 2: Sync protective-leg quantity when the entry fills

At entry-evaluation time the entry order is PENDING **qty=0** (RM sizes it later), and `_replace_leg` copies `quantity=entry.quantity` (`backtest_account.py:3088`) — so a bracket leg created by the entry rule would carry qty=0. The single robust fix: re-sync `leg.quantity` from the parent at WAITING_TRIGGER→ACCEPTED promotion (`backtest_account.py:1584-1609`), which runs exactly when the parent reaches FILLED.

**Files:**
- Modify: `testplatform/backend/app/services/backtest/backtest_account.py:1600-1609`
- Test: `testplatform/backend/tests/backtest/test_entry_bracket_engine.py` (create — this file grows in Task 4)

**Step 1: Write the failing test**

Find the existing BacktestAccount test fixture (look at `testplatform/backend/tests/backtest/` for tests that construct a BacktestAccount with a price stub — e.g. the OCO/fill tests; reuse their fixture). Test:

```python
def test_waiting_leg_quantity_syncs_to_parent_fill(bt_account):
    """A protective leg created while the entry was PENDING qty=0 (entry-rule bracket)
    must inherit the parent's SIZED quantity when promoted at fill — otherwise the
    bracket closes 0 shares."""
    # 1. create entry order PENDING qty=0 + its transaction (Phase 1/1.5 analog)
    # 2. adjust_tp_sl(txn, tp, sl)  -> OCO leg with quantity==0
    # 3. size the entry (quantity=7), submit + fill it on the next bar
    # 4. run the promotion pass (the account's refresh/roll that calls the
    #    WAITING_TRIGGER promotion)
    # 5. assert the leg is ACCEPTED and leg.quantity == 7
```

Write it concretely against the real fixture API (the OCO fill tests show the exact call pattern for "advance one bar / refresh"). Keep it one behavior: leg qty after promotion.

**Step 2: Run to verify it fails**

Expected: leg.quantity == 0 after promotion (assert fails).

**Step 3: Implement**

In the promotion loop (`backtest_account.py:1607-1609`), sync qty from the parent before persisting:

```python
            if parent.status == trigger:
                # Entry-rule brackets are created while the parent is still PENDING qty=0
                # (the RM sizes it afterwards) — sync the leg to the parent's real filled
                # size at promotion so the bracket closes the whole position.
                parent_qty = parent.filled_qty or parent.quantity
                if parent_qty and leg.quantity != parent_qty:
                    leg.quantity = parent_qty
                leg.status = OrderStatus.ACCEPTED
                update_instance(leg)
```

**Step 4: Run tests**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/backtest/test_entry_bracket_engine.py -v`
Expected: PASS. Also run the existing account tests to prove no regression:
`C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/backtest/ -q` — same pass/fail set as before this task.

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/backtest_account.py testplatform/backend/tests/backtest/test_entry_bracket_engine.py
git commit -m "fix(backtest): sync protective-leg qty from parent at WAITING_TRIGGER promotion"
```

---

### Task 3: RM safeguard vs ruleset SL — tighter-wins at submit

Today `daily_engine.py:~1170-1174` submits every sized entry with `sl_price=order.stop_price` (the RM safeguard). `submit_order` routes that through `adjust_sl(source="initial_setup")` (`AccountInterface.py:189-193`), and BacktestAccount's `adjust_sl` **re-issues the whole bracket** (it preserves an existing TP — see `backtest_account.py:2720-2738` — but would silently REPLACE the ruleset's SL). Rule: the TIGHTER stop wins (long: higher SL; short: lower SL) so the RM's risk guarantee (`risk_per_trade_pct` sized off the safeguard distance) is never loosened, while an intentionally tighter strategy stop is respected.

**Files:**
- Modify: `testplatform/backend/app/services/backtest/daily_engine.py:~1160-1174` (the `submit_order(order, sl_price=...)` call site inside the RM/submit pass)
- Test: extend `testplatform/backend/tests/backtest/test_entry_bracket_engine.py`

**Step 1: Write the failing tests**

```python
def test_ruleset_sl_tighter_than_safeguard_wins(bt_engine_fixture):
    """Entry bracket SL at -3% (tighter) vs RM safeguard at -8%: the submitted
    protective stop must be the -3% one (transaction.stop_loss unchanged)."""

def test_safeguard_tighter_than_ruleset_sl_wins(bt_engine_fixture):
    """Entry bracket SL at -15% (looser) vs RM safeguard at -8%: the safeguard
    replaces it (long: max of the two stop prices)."""
```

Use whichever existing engine-level test fixture runs a tiny 2-symbol backtest with a stub expert (`tests/backtest/` has engine tests — e.g. the ones added for the manage-schedule / dedup fixes; crib the smallest one). Drive one entry with the bracket enabled and a known ATR so the safeguard price is deterministic.

**Step 2: Run to verify both fail**

**Step 3: Implement**

At the `daily_engine.py` submit call site, replace the flat `sl_price=order.stop_price or None` with:

```python
                    # Effective protective stop: the RULESET entry-bracket SL (set by the
                    # entry rule's adjust action via Phase 2, carried on the transaction)
                    # vs the RM SAFEGUARD (order.stop_price, what the position was SIZED
                    # off). TIGHTER WINS — long: the higher stop; short: the lower — so
                    # realized risk can never exceed risk_per_trade_pct, while a tighter
                    # strategy stop is respected. No ruleset SL -> safeguard as before.
                    sl_price = order.stop_price or None
                    txn = get_instance(Transaction, order.transaction_id) if order.transaction_id else None
                    ruleset_sl = txn.stop_loss if txn else None
                    if ruleset_sl and sl_price:
                        is_long = order.side == OrderDirection.BUY
                        sl_price = max(ruleset_sl, sl_price) if is_long else min(ruleset_sl, sl_price)
                    elif ruleset_sl:
                        sl_price = None  # ruleset SL already attached; nothing tighter to add
                    self.account.submit_order(order, sl_price=sl_price)
```

(Import `Transaction`/`OrderDirection`/`get_instance` per the file's existing import style — most are already imported.) Note the `elif ruleset_sl: sl_price = None` branch: if the ruleset set an SL and the RM produced none, do NOT pass a safeguard — the bracket leg already exists; passing `None` skips the `adjust_sl` re-issue entirely (`AccountInterface.py:189` only fires on truthy `sl_price`).

**Step 4: Run tests** — both new tests PASS; `pytest tests/backtest/ -q` no regressions.

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/daily_engine.py testplatform/backend/tests/backtest/test_entry_bracket_engine.py
git commit -m "feat(backtest): tighter-wins precedence between entry-bracket SL and RM safeguard"
```

---

### Task 4: Delete dead TP/SL fields; add `entry_actions`; revise Task 1's seeder to consume it via `action_from_rule`

**Files:**
- Modify: `testplatform/backend/app/models/strategy.py` — delete lines 30-41 (10 columns); add `entry_actions = Column(JSON, nullable=True)` next to `exit_conditions` (line 28); update `to_dict()` (delete the 10 `initialTp*`/`initialSl*` keys, add `"entryActions": self.entry_actions or []`)
- Create: `testplatform/backend/db_migrate/026_entry_actions_replace_tpsl_fields.py` — copy the rebuild-table pattern from `022_drop_strategy_rm_columns.py` EXACTLY (SQLite can't reliably `DROP COLUMN`/`ADD COLUMN` together across versions, so rebuild): new DDL keeps `id/name/description/required_fields/entry_conditions/buy_entry_conditions/sell_entry_conditions/exit_conditions/created_at/updated_at`, ADDS `entry_actions JSON`, DROPS the 10 `initial_tp_*`/`initial_sl_*` columns. Idempotent (no-op if `initial_tp_percent` isn't a column, matching 022's `if not rm_columns: return False` pattern).
- Modify: `testplatform/backend/app/services/backtest/default_rulesets.py` — REVISE (not append to) Task 1's `_entry_actions`/`seed_ruleset_from_tree`: replace the `entry_tp_percent`/`entry_sl_percent`/`entry_tp_reference` scalar kwargs with a single `entry_actions: list[dict] | None` kwarg. For each rule in `entry_actions`, call `action_from_rule(rule, key=f"{side}_{rule['id']}")` (import from `ba2_common.core.rule_builders`, same import `seed_open_positions_ruleset` already has) and merge the returned dict into the action set alongside the buy/sell action. This DELETES the hand-rolled sign-math added in Task 1 — `action_from_rule` + `compute_price` already handle it (same as exit rules).
- Modify: `testplatform/backend/tests/backtest/test_entry_bracket_seeding.py` — REWRITE the 4 Task-1 tests to pass `entry_actions=[{...}]` instead of the 3 scalar kwargs (same assertions on the resulting `EventAction.actions` dict; the sign-convention tests keep the SAME expected values, only the call shape changes — the strategy-level author still supplies a signed `action_value` exactly as before, it just arrives via a rule dict instead of a bare float).

**Step 1: Update the seeding tests first (TDD on the revised signature)**

Rewrite the 4 tests in `test_entry_bracket_seeding.py` to the new call shape, e.g.:
```python
def test_bracket_emits_tp_and_sl_actions():
    rid = seed_ruleset_from_tree(
        None, name="t-bracket", entry_action={"action_type": "buy"},
        entry_actions=[
            {"id": "e_tp", "action_type": "adjust_take_profit", "reference_value": "order_open_price", "action_value": 8.0},
            {"id": "e_sl", "action_type": "adjust_stop_loss", "reference_value": "order_open_price", "action_value": -3.0},
        ],
    )
    actions = _actions_of_first_rule(rid)
    by_type = {cfg["action_type"]: cfg for cfg in actions.values()}
    assert by_type[ExpertActionType.ADJUST_TAKE_PROFIT.value]["value"] == 8.0
    assert by_type[ExpertActionType.ADJUST_STOP_LOSS.value]["value"] == -3.0
```
(`action_from_rule` reads `rule.get("value")` first, else `rule.get("action_value")` — use whichever the exit-rule convention already uses consistently elsewhere, check `seed_open_positions_ruleset`'s callers to match.) Keep `test_no_bracket_by_default` (now `entry_actions=None`) and `test_sl_only_bracket` (now a 1-item list). The `expert_target_price` test keeps its assertion (value passed through signed, unchanged) since that's `compute_price`'s behavior, not the seeder's.

**Step 2: Run to verify failures**, then **Step 3: implement** the seeder revision described above, **Step 4: run tests** (`pytest tests/backtest/test_entry_bracket_seeding.py tests/backtest/test_entry_ruleset_seed.py tests/backtest/test_entry_bracket_engine.py -v` — Task 2/3's tests must still pass unmodified, since they only depend on the RESULT (a `Transaction.stop_loss` being set), not on which kwarg shape produced it).

**Step 5: DB migration + model change.** Write `026_...py`, run it against a scratch copy of the dev DB first to sanity-check (`C:\Users\basti\Documents\ba2\test\dl_forecasting.db` — COPY it, don't run against the live one blind), then apply for real via however this repo's migrations are invoked (check `testplatform/backend/db_migrate/__init__.py` or a `run_migrations.py`/similar entrypoint — grep for how 025 gets invoked in CI/docs). Update `strategy.py`'s model + `to_dict()`.

**Step 6: Full regression + commit.**
```bash
cd testplatform/backend && C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/backtest/ -q
git add testplatform/backend/app/models/strategy.py testplatform/backend/db_migrate/026_entry_actions_replace_tpsl_fields.py testplatform/backend/app/services/backtest/default_rulesets.py testplatform/backend/tests/backtest/test_entry_bracket_seeding.py
git commit -m "refactor(backtest): entry TP/SL via unified entry_actions rules, not bespoke Strategy fields"
```

**Note:** the ORIGINAL Task 1 commits (`9f28fee`, `32631b8`) already exist in history with the OLD scalar-kwarg signature — that's fine, this task's commit supersedes them going forward; don't rewrite history, just land the correction as a new commit.

---

### Task 5: Gene collection for `entry_actions` — mirror the existing `exit:<id>:*` handling exactly

**Files:**
- Modify: `testplatform/backend/app/services/strategy_param_space.py` — DELETE `_collect_tp_sl` (lines 39-48) and its call site in `collect_param_space`; EXTEND `_collect_conditions` (~line 126-169) to walk `getattr(strategy, "entry_actions", None) or []` with the exact same logic already used for `exit_conditions` (action_value_optimize → `entry:<id>:action_value` range; toggle_optimize → `entry:<id>:enabled` 0/1 gene) but WITHOUT the option-selection-param branches (those are exit/option-entry specific, not needed here) and WITHOUT a nested `conditions` walk (entry actions have no sub-tree).
- Modify: `testplatform/backend/app/services/strategy_param_space.py::decode_params` (~line 249-330) — mirror the `exit:` branch: add an `entry:` branch building `entry_action_by_id`/`entry_enabled_by_id`, then build a decoded `entry_rules` list from `copy.deepcopy(getattr(strategy, "entry_actions", None) or [])` the same way `exit_rules` is built (drop a rule whose `enabled` gene decoded to 0; else apply the decoded `action_value`). Return it in the decoded dict as `entry_rules` (new key, alongside the existing `buy_tree`/`sell_tree`/`exit_rules`).
- Test: `testplatform/backend/tests/test_strategy_param_space_entry_actions.py` (create)

**Step 1: Failing tests** (mirror whatever `tests/test_strategy_param_space_decode.py`/`tests/test_param_space_roundtrip.py` already do for `exit_conditions`, just for `entry_actions`):
```python
class _Strat:
    buy_entry_conditions = None; sell_entry_conditions = None
    entry_conditions = None; exit_conditions = []
    entry_actions = [
        {"id": "e_sl", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
         "action_value": -5.0, "action_value_optimize": True,
         "action_value_min": -15.0, "action_value_max": -2.0, "action_value_step": 1.0},
    ]

def test_entry_action_value_gene_collected():
    space = collect_param_space(_Strat(), expert_cfg=None)
    assert space["entry:e_sl:action_value"] == {"type": "float", "min": -15.0, "max": -2.0, "step": 1.0}

def test_entry_rules_decoded_with_ga_value():
    decoded = decode_params(_Strat(), {"entry:e_sl:action_value": -9.0})
    rule = next(r for r in decoded["entry_rules"] if r["id"] == "e_sl")
    assert rule["action_value"] == -9.0

def test_entry_rule_dropped_when_toggled_off():
    strat = _Strat()
    strat.entry_actions[0]["toggle_optimize"] = True
    decoded = decode_params(strat, {"entry:e_sl:enabled": 0})
    assert not any(r["id"] == "e_sl" for r in decoded["entry_rules"])
```
Check the EXACT existing test file for exit-rule gene collection/decode first and match its fixture style precisely (don't guess `_Strat`'s other required attributes — copy them).

**Step 2-4:** Run (fail) → implement (mirroring exit's code, don't reinvent) → run (pass) + `pytest tests/test_param_space_roundtrip.py tests/test_strategy_param_space_decode.py tests/test_param_space_no_rm.py -q` (no regressions).

**Step 5: Commit**
```bash
git add testplatform/backend/app/services/strategy_param_space.py testplatform/backend/tests/test_strategy_param_space_entry_actions.py
git commit -m "feat(optimizer): entry_actions gene collection + decode, mirroring exit_conditions"
```

---

### Task 6: Wire `entry_rules` through the handler + optimizer chain (delete every dead `initial_tp_*`/`initial_sl_*` reference)

**Files:**
- Modify: `testplatform/backend/app/services/backtest/daily_backtest_handler.py` — grep `initial_tp_percent|initial_sl_percent|initial_tp_reference|initial_tp_ref` in this file and DELETE every hit (config-block keys AND the `_apply_initial_brackets`-history comments around them — replace with a short note that entry TP/SL is now just another rule, seeded exactly like exit rules). Add an `entry_rules` config key (list, default `[]`/`None`) forwarded into `_build_experts`'s `seed_ruleset_from_tree(..., entry_actions=config.get("entry_rules"))` call.
- Modify: `testplatform/backend/app/services/strategy_optimization_handler.py` — DELETE the `initial_tp`/`initial_sl` decoded block (~769-786, the "STALE MECHANISM" comment block) entirely — `decode_params` (Task 5) now returns `entry_rules` directly, no separate tp/sl extraction needed. Forward `decoded.get("entry_rules")` into the trial-config dict as `"entry_rules": decoded.get("entry_rules")` (mirroring the existing `"exit_rules": decoded.get("exit_rules")` line right next to it).
- Modify: `testplatform/backend/app/api/backtests.py` — grep `initial_tp_percent|initial_sl_percent|initial_tp_reference` and delete; forward `backtest.entry_actions` (renamed from whatever the Backtest model's saved-strategy-params field is — check `app/models/backtest.py` for how `exit_conditions`/`exitConditions` round-trips there and mirror it exactly for `entry_actions`/`entryActions`) into the rerun/single-run payload as `entry_rules`.
- Modify: `testplatform/backend/app/services/backtest/rerun_handler.py` — same grep-and-delete + mirror the `exit_rules` forwarding pattern for `entry_rules`.
- Modify: `testplatform/backend/app/models/backtest.py` — check whether `Backtest` has its own `initial_tp_percent`-style columns/dict keys (grep) mirroring `Strategy`'s dead ones; if so, apply the same delete + `entry_actions`/`entryActions` addition there, consistent with however `exit_conditions`/`exitConditions` already round-trips on this model.
- Test: extend `testplatform/backend/tests/backtest/test_entry_bracket_engine.py` with ONE end-to-end test: config carrying `entry_rules=[{adjust_stop_loss...}]` → `_build_experts` seeds the ruleset → a filled entry's transaction has `stop_loss` set → the RM-safeguard-precedence test from Task 3 already proves the interaction from there.

**Step 1-4:** TDD as usual. **Step 5:** full backend suite (`pytest -q` from `testplatform/backend`, expect only the pre-existing ~10 unrelated failures), then commit:
```bash
git commit -am "feat(backtest): wire entry_rules through single-run + optimizer trial-config paths; delete dead initial_tp/sl plumbing"
```

---

### Task 7: Launcher — S4 template attaches its TP via an `entry_actions` rule (delete its `initial_tp_*` usage)

`ba2test_launcher.py`'s S4 template was designed around `initial_tp_reference="expert_target_price"` + an offset-from-target tp gene — dead since `_apply_initial_brackets` was removed, now expressed as a rule instead of a field.

**Files:**
- Modify: `testplatform/ba2test_launcher.py` — grep `initial_tp_percent|initial_tp_optimize|initial_tp_reference|initial_tp_ref|initial_sl_percent|initial_sl_optimize` across the WHOLE file (not just S4 — S1/S2/S3/minimal templates all currently pass `initial_tp_percent=None, initial_tp_optimize=False` etc. per the plan's original research; these calls must be updated to match the new `Strategy` constructor, which no longer has those kwargs at all — for S1/S2/S3 that just means DELETING the now-nonexistent kwargs from the `Strategy(...)` calls). For S4 specifically, replace its `initial_tp_*` kwargs with:
```python
entry_actions=[
    {"id": "s4_tp", "action_type": "adjust_take_profit",
     "reference_value": "expert_target_price", "action_value": -5.0,
     "action_value_optimize": True, "action_value_min": -15.0, "action_value_max": 5.0,
     "action_value_step": 1.0},
],
```
(pick concrete min/max/step matching the ORIGINAL S4 tp-gene bounds — check the pre-`_apply_initial_brackets`-removal git history or the current dead `initial_tp_min/max/step` values for S4 specifically before they're deleted in Task 4, so the restored behavior matches original intent, not an arbitrary new range).

**Step 1: Make the edits** (config templates, no TDD — but re-run `pytest tests/` in `testplatform/backend` since `Strategy(...)` constructor signature changed in Task 4, so EVERY template call site in this file must compile).

**Step 2: Verify** — run the launcher's own `--dry-run` (or equivalent smallest smoke check) for S1-S4 to confirm no `TypeError: unexpected keyword argument`.

**Step 3: Commit**
```bash
git add testplatform/ba2test_launcher.py
git commit -m "feat(launcher): S4 TP restored via entry_actions rule; S1-S3 updated for the deleted initial_tp/sl kwargs"
```

---

### Task 8: Frontend — delete the dead TP/SL fields, add an entry-actions builder (reuse the exit builder)

**Files:**
- Modify: `testplatform/frontend/src/pages/Backtesting.tsx`:
  - DELETE all `initialTp*`/`initialSl*` state (lines ~691-704), every serialize site (~1542-1552, strategy-save ~2111+, optimize payload), every load site (~2180 area) — grep `initialTp|initialSl|InitialTp|InitialSl` in this file and remove all of it (the Strategy interface fields at ~138-147 too).
  - ADD `entryActions` state (`ConditionGroup`-adjacent but action-only — check exactly what shape `ExitConditionsBuilder`'s `value`/`onChange` props expect, likely `ExitConditionSet[]` per the existing `exitConditions` state at line 626) and an "Entry Actions" section inside the existing Entry Conditions modal (the one just fixed for Cancel semantics), reusing `ExitConditionsBuilder` (or a thin wrapper around it) so the SAME action-type dropdown (Close/Sell/Adjust TP/Adjust SL/…), reference-value picker, and optimize-range controls exit rules already have work for entry too — filtered to just the adjust_take_profit/adjust_stop_loss action types (entry doesn't need close/sell actions, those ARE the entry).
  - Wire `entryActions` into: the condition-modal snapshot/cancel logic just added (include it in `conditionModalSnapshot`), the strategy save/load payload (`entryActions`/`entry_actions` mirroring how `exitConditions`/`exit_conditions` round-trips), and the optimize request payload (`entry_rules` key, matching Task 6's backend field name).
- Modify: any other frontend file referencing `initialTpPercent`/`initialSlPercent` (grep the whole `testplatform/frontend/src` — check `RuleIO`/export-import components too, since strategies can be exported/imported as JSON).

**Step 1: Implement.** **Step 2: Verify** — `npx tsc --noEmit -p .` clean; manually: add an "Adjust Take Profit" entry action in the UI, save a strategy, confirm the saved payload has `entryActions` (not `initialTpPercent`), run a tiny backtest and confirm the transaction gets a real `take_profit`.

**Step 3: Commit**
```bash
git add testplatform/frontend/src/pages/Backtesting.tsx
git commit -m "feat(ui): entry-actions builder (reuses exit builder) replaces the dead initial-TP/SL fields"
```

---

### Task 9: Docs sweep + version bump

**Files:**
- Re-check every comment site touched across Tasks 1/4/6 for leftover stale phrasing (search for "entry_bracket", "STALE", "DEAD" across the touched files — none of that language should survive; the mechanism is just "entry rules can carry adjust actions" now, no separate flag concept to explain).
- `ba2_trade_platform/version.py` — bump build number by 1.

**Step 1:** sweep. **Step 2:** full suites (backend `pytest -q`, root `pytest -q`, frontend `tsc --noEmit`) — same baselines as before. **Step 3:**
```bash
git add -A
git commit -m "docs(backtest): entry-actions comment sweep + version bump"
```

---

## Explicitly OUT of scope (YAGNI)

- **A generic entry-action builder for action types other than adjust_take_profit/adjust_stop_loss** (e.g. letting an entry rule also fire a `close`/`sell`): not requested, no use case yet — `ExitConditionsBuilder` reuse should be filtered/scoped to just the two adjust actions for now; widening it is a separate ask if it comes up.
- **Live-platform changes**: live already works (evaluator is shared, in `ba2_common`); nothing to change there. Do NOT touch `TradeActionEvaluator.py`/`TradeActions.py`/`rule_builders.py` — any behavior gap found in them (e.g. `TradeManager.py:1216`'s identical unconditional-safeguard-overwrite, found during Task 3 review) is a STOP-and-report, not a local patch, since live trades through that code.
- **Migrating S1-S3 templates** to carry an entry action: their condition-driven exits are the design; only S4 had dead bracket intent (Task 7 covers exactly that).
- **The legacy 'ml' engine** (`backtest_handler.py` MLStrategy tp/sl): untouched, it has its own working bracket, unrelated to `Strategy.entry_actions`.

## Verification of live parity (manual, after Task 6)

Run one tiny backtest with an `entry_rules`/`entryActions` list carrying an `adjust_stop_loss` rule and compare the seeded EventAction JSON against the live prod row (eventaction id=1, `BUY_Longterm_70pctConfidence_10pctProfit`, in `C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite`): same action_type strings, same reference_value vocabulary, same `action_from_rule` conversion — because it's now LITERALLY the same function, not a parallel implementation. The transaction after entry must carry `take_profit`/`stop_loss` exactly like live's `adjust_tp_sl(source="ruleset")` writes them.
