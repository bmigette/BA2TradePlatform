# Entry-Time TP/SL Bracket Actions Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the test platform attach TP/SL at entry the same way the live platform does — as `adjust_take_profit`/`adjust_stop_loss` actions on the ENTER_MARKET rule (live example: `BUY_Longterm_70pctConfidence_10pctProfit`, eventaction id=1 in the prod DB) — reusing the existing-but-dead `tp`/`sl` GA genes and frontend fields, behind an explicit opt-in flag so no historical config changes behavior.

**Architecture:** The live platform's `TradeActionEvaluator` already implements the whole mechanism: Phase 1 creates the PENDING entry order, **Phase 1.5 eagerly creates the `Transaction`** (`TradeActionEvaluator.py:368-391` — `self.account._create_transaction_for_order(order)`), and Phase 2 computes TP/SL prices (`TradeActions.py:903 compute_price`) and calls `account.adjust_tp_sl(...)`. The backtest entry path (`daily_engine.py:826-843 _run_expert_entry`) already drives this exact evaluator — so the ONLY missing pieces are: (1) the enter-ruleset seeder never emits adjust actions, (2) the `BacktestAccount` protective leg copies `entry.quantity` at creation time (which is 0 pre-RM-sizing) and never re-syncs, (3) the RM safeguard SL at submit would silently overwrite the ruleset SL. Everything upstream (GA genes `tp`/`sl` via `initial_tp_optimize`, `decode_params`, trial-config forwarding as `initial_tp_percent`/`initial_sl_percent`/`initial_tp_reference`, and the full frontend UI) already exists and currently no-ops.

**Tech Stack:** Python (FastAPI backend, SQLModel, pytest), shared `ba2_common` package (evaluator/actions/rule-builders), React+TS frontend (Backtesting.tsx).

**Critical safety constraint:** Every saved strategy/backtest config in the DB carries `initialTpPercent: 5.0, initialSlPercent: 2.0` (dead defaults). Resurrecting them **unconditionally would clamp every re-run at +5%/-2%** and destroy existing strategies (e.g. the `-goal` grid winners). Therefore the whole feature is gated behind a NEW explicit boolean config key `entry_bracket` (default False → byte-identical behavior to today).

**Sign conventions (nail these — they come from `TradeActions.py:936-947 compute_price`):** for a long, price = reference × (1 + value/100); for a short, reference × (1 − value/100). So the seeder must emit `value = +initial_tp_percent` for the TP action and `value = −abs(initial_sl_percent)` for the SL action, and both then work for BOTH sides automatically. When `initial_tp_reference == "expert_target_price"`, the TP value passes through SIGNED as-is (offset-from-target; live's example uses −5.0 = 5% below the analyst target — and note the S4 launcher template was DESIGNED for exactly this and has been silently no-op since `_apply_initial_brackets` was removed).

**Key files (read these first):**
- `packages/common/ba2_common/core/TradeActionEvaluator.py:314-540` — Phases 1/1.5/2 (shared, DO NOT MODIFY — it already works)
- `packages/common/ba2_common/core/rule_builders.py:165-187` — `action_from_rule` (already supports adjust actions; DO NOT MODIFY)
- `testplatform/backend/app/services/backtest/default_rulesets.py:166-257` — `_entry_actions` + `seed_ruleset_from_tree` (MODIFY: emit adjust actions)
- `testplatform/backend/app/services/backtest/daily_backtest_handler.py:393-411, ~695-722` — config keys + `_build_experts` seeding call (MODIFY: plumb through)
- `testplatform/backend/app/services/backtest/backtest_account.py:1584-1609` (`WAITING_TRIGGER` promotion), `3059-3105` (`_replace_leg`) (MODIFY: qty sync)
- `testplatform/backend/app/services/backtest/daily_engine.py:~1149-1174` — RM sizing + `submit_order(order, sl_price=order.stop_price)` (MODIFY: precedence)
- `testplatform/backend/app/services/strategy_optimization_handler.py:769-786, ~904-917` — trial-config forwarding (MODIFY: forward `entry_bracket`, un-stale comments)
- `testplatform/backend/app/services/strategy_param_space.py:39-48` — `_collect_tp_sl` genes (NO CHANGE — already emits `tp`/`sl` genes when `initial_tp_optimize`/`initial_sl_optimize`)
- `testplatform/frontend/src/pages/Backtesting.tsx:691-704, 1542-1552` — existing TP/SL fields (MODIFY: add `entryBracket` toggle)

Run backend tests from `testplatform/backend` with the TEST venv:
`C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/<file> -v`

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

---### Task 4: Plumb the opt-in flag + TP/SL config through the handler chain

**Files:**
- Modify: `testplatform/backend/app/services/backtest/daily_backtest_handler.py:393-411` (config block) and `~695-722` (`_build_experts` seeding call)
- Modify: `testplatform/backend/app/services/strategy_optimization_handler.py:769-786` (decoded tp/sl block) and `~904-917` (trial-config keys)
- Modify: `testplatform/backend/app/api/backtests.py:534-552` (single-run payload forward — it already forwards `initial_tp_percent`/`initial_sl_percent`/`initial_tp_reference`; add `entry_bracket`)
- Test: extend `testplatform/backend/tests/backtest/test_entry_bracket_engine.py` (end-to-end: config in → bracketed trade out)

**Step 1: Write the failing end-to-end test**

```python
def test_entry_bracket_config_end_to_end(bt_engine_fixture):
    """config {entry_bracket: True, initial_tp_percent: 6, initial_sl_percent: 3}
    -> the seeded enter ruleset carries the adjust actions -> a filled entry's
    transaction has take_profit ~= entry*1.06 and stop_loss ~= entry*0.97 and an
    OCO leg exists -> the trade eventually closes via the TP or SL leg (exit_reason
    contains 'OCO' / tp_sl_filled)."""

def test_entry_bracket_default_off_is_noop(bt_engine_fixture):
    """Same config WITHOUT entry_bracket: transaction.take_profit/stop_loss stay
    None at entry (current behavior, byte-identical) even though
    initial_tp_percent=5/initial_sl_percent=2 are present (the dead legacy defaults
    every historical config carries)."""
```

**Step 2: Run to verify both fail** (first one: no adjust actions seeded; second one passes trivially — keep it as the regression lock).

**Step 3: Implement**

`daily_backtest_handler.py` config block (line ~396): replace the STALE-mechanism comment with the live one and add the flag:

```python
        # ENTRY BRACKET (opt-in): when entry_bracket is True the enter ruleset carries
        # adjust_take_profit/adjust_stop_loss actions built from initial_tp_percent /
        # initial_sl_percent / initial_tp_reference — attached at entry through the SAME
        # shared TradeActionEvaluator Phase 1.5/2 path live uses. Default False: every
        # historical config (which carries dead 5.0/2.0 legacy defaults) stays byte-identical.
        "entry_bracket": bool(payload.get("entry_bracket", False)),
        "initial_tp_percent": payload.get("initial_tp_percent"),
        "initial_sl_percent": payload.get("initial_sl_percent"),
        ...
```

`_build_experts` (line ~700-722): compute the seeder kwargs once:

```python
    entry_bracket = bool(config.get("entry_bracket"))
    entry_tp = config.get("initial_tp_percent") if entry_bracket else None
    entry_sl = config.get("initial_sl_percent") if entry_bracket else None
    entry_tp_ref = config.get("initial_tp_reference") if entry_bracket else None
```

and pass `entry_tp_percent=entry_tp, entry_sl_percent=entry_sl, entry_tp_reference=entry_tp_ref` into the `seed_ruleset_from_tree(...)` call. Update the "No initial TP/SL bracket is applied at transaction-OPEN" comment (line ~705-712) to describe the new opt-in path.

`strategy_optimization_handler.py`: line 769-786 — rewrite the "STALE MECHANISM" comment: the tp/sl genes are LIVE again when `entry_bracket` is on (decoded `tp`/`sl` -> `initial_tp_percent`/`initial_sl_percent` -> enter-ruleset adjust actions). Add to the trial-config dict (line ~904):

```python
        "entry_bracket": bool(backtest_cfg.get("entry_bracket", False)),
```

`api/backtests.py` (line ~548): forward `entry_bracket` from the Backtest row's strategy_params (`sp.get("entryBracket")`) into the payload, same non-None-only pattern as its neighbors. Check `rerun_handler.py:159-164` forwards it too (same shape).

**Step 4: Run** — both tests PASS; then the full backend suite:
`C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest -q` from `testplatform/backend` — expect the SAME pre-existing failures as baseline (10 as of 2026-07-03), zero NEW ones.

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/daily_backtest_handler.py testplatform/backend/app/services/strategy_optimization_handler.py testplatform/backend/app/api/backtests.py testplatform/backend/tests/backtest/test_entry_bracket_engine.py
git commit -m "feat(backtest): opt-in entry_bracket flag plumbs entry TP/SL through single-run + optimizer paths"
```

---

### Task 5: GA integration — genes only enter the space when the bracket is on

`_collect_tp_sl` (`strategy_param_space.py:39-48`) already emits `tp`/`sl` genes from `initial_tp_optimize`/`initial_sl_optimize` — but a gene that varies a DEAD parameter wastes two dimensions (that's today's silent behavior). Gate it.

**Files:**
- Modify: `testplatform/backend/app/services/strategy_param_space.py:39-48`
- Test: `testplatform/backend/tests/test_param_space_entry_bracket.py` (create)

**Step 1: Failing tests**

```python
from app.services.strategy_param_space import collect_param_space

class _Strat:  # minimal strategy shim, same style as tests/test_param_space_no_rm.py
    initial_tp_optimize = True
    initial_tp_min, initial_tp_max, initial_tp_step = 4.0, 20.0, 2.0
    initial_sl_optimize = True
    initial_sl_min, initial_sl_max, initial_sl_step = 2.0, 10.0, 1.0
    buy_entry_conditions = None; sell_entry_conditions = None
    entry_conditions = None; exit_conditions = []

def test_tp_sl_genes_absent_without_entry_bracket():
    space = collect_param_space(_Strat(), expert_cfg=None, entry_bracket=False)
    assert "tp" not in space and "sl" not in space

def test_tp_sl_genes_present_with_entry_bracket():
    space = collect_param_space(_Strat(), expert_cfg=None, entry_bracket=True)
    assert "tp" in space and "sl" in space
```

Check `collect_param_space`'s actual signature (line 172) first and match the existing kwargs (screener_cfg etc.); `entry_bracket` becomes a new keyword with default... **decide by call sites**: the optimizer handler is the only caller — pass `entry_bracket=bool(backtest_cfg.get("entry_bracket", False))` there. Default the kwarg to `True` in the function ONLY if existing tests construct spaces relying on tp/sl — run the existing param-space tests to see; otherwise default `False` (safer).

**Step 2: Run — fail** (unexpected kwarg).

**Step 3: Implement** — `_collect_tp_sl(strategy, entry_bracket)`: return `{}` unless `entry_bracket`; thread the kwarg through `collect_param_space`. Update the optimizer call site.

**Step 4: Run** new tests + `pytest tests/test_param_space_roundtrip.py tests/test_strategy_param_space_decode.py tests/test_param_space_no_rm.py -q` — no regressions.

**Step 5: Commit**

```bash
git commit -am "feat(optimizer): tp/sl genes gated behind entry_bracket (no dead gene dimensions)"
```

---

### Task 6: Launcher + S4 template — restore the intended target-anchored TP

`ba2test_launcher.py` S4 template (line ~1122-1131, 1486-1488, 1650-1653) was designed around `initial_tp_reference="expert_target_price"` + the tp gene as offset-from-target — silently dead since `_apply_initial_brackets` was removed. Wire it to the new mechanism.

**Files:**
- Modify: `testplatform/ba2test_launcher.py` — S4 strategy template: set `entry_bracket=True` in its backtest block (the same place lines 1486-1488 set `initial_tp_reference`); leave S1-S3 untouched (their exits are condition-driven by design). Also forward `entry_bracket` from the strategy/backtest block into the optimize request payload wherever `initial_tp_reference` is already forwarded (grep `initial_tp_reference` in the file — 3 sites).
- Test: none new (Task 4's end-to-end covers the mechanism; the launcher change is config plumbing verified in Step 2).

**Step 1: Make the edit** (no TDD — config template).

**Step 2: Verify by dry-run**

Run: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe test_files/run_screener_capband_matrix.py --dry-run --strategies S4 --bands large 2>&1 | head -20`
Then grep the printed/constructed command or add a temporary `--dry-run` print of the backtest block — confirm `entry_bracket: True` + `initial_tp_reference: expert_target_price` appear for S4 and NOT for S1-S3. (Check how `--dry-run` prints jobs first; adapt.)

**Step 3: Commit**

```bash
git add testplatform/ba2test_launcher.py
git commit -m "feat(launcher): S4 opts into entry_bracket — target-anchored TP restored"
```

---

### Task 7: Frontend — expose the toggle

All the fields exist (`Backtesting.tsx:691-704`: percent/optimize/min/max/step + `initialTpReference`). Add the single missing control: an "Attach TP/SL at entry" toggle mapped to `entryBracket` in strategy_params, and grey-out the TP/SL fields when off (they're inert then).

**Files:**
- Modify: `testplatform/frontend/src/pages/Backtesting.tsx`
  - state: `const [entryBracket, setEntryBracket] = useState(false);` next to line 691
  - serialize: add `entryBracket` wherever `initialTpPercent` is serialized (lines ~1542-1552, the strategy-save path ~2111+, and the optimize request payload — grep `initialTpPercent` in the file, mirror every site)
  - load: set it back in the strategy-load path (`setInitialTpPercent` call sites, ~line 2180 area)
  - UI: checkbox + explanatory tooltip ("Attaches TP/SL as entry-rule actions (live-parity). RM safeguard still applies; tighter stop wins. Off = TP/SL fields inert.") above the existing TP/SL inputs; `disabled={!entryBracket}` on those inputs
- Modify: `testplatform/backend/app/api/backtests.py` / `strategies.py` — accept `entryBracket` in the strategy_params passthrough (check whether strategy_params is schemaless JSON — if so, zero backend change; verify by grep `initialTpPercent` in `api/strategies.py`)

**Step 1: Implement** (UI change, no TDD; typecheck is the gate).

**Step 2: Verify**

Run: `cd testplatform/frontend && npx tsc --noEmit -p .` → clean.
Manual: dev server (already running, port 5173) — toggle off: fields greyed; toggle on + save strategy: saved strategy_params contain `"entryBracket": true`; run a small single backtest with bracket on and confirm trades close with OCO exit reasons and the transaction rows carry TP/SL.

**Step 3: Commit**

```bash
git add testplatform/frontend/src/pages/Backtesting.tsx
git commit -m "feat(ui): entry-bracket toggle wires the existing TP/SL fields to the new mechanism"
```

---

### Task 8: Docs/comments sweep + version bump

**Files:**
- Modify: the 4 comment sites updated on 2026-07-03 to say the fields were DEAD — they're now ALIVE behind `entry_bracket`:
  - `testplatform/backend/app/services/strategy_optimization_handler.py:769` block (done in Task 4 — verify)
  - `testplatform/backend/app/services/backtest/daily_backtest_handler.py:396` + `~705` blocks (done in Task 4 — verify)
  - `testplatform/backend/app/services/backtest/default_rulesets.py:177-186` + `200-208` docstrings (done in Task 1 — verify)
  - `testplatform/backend/app/api/backtests.py:534` comment (done in Task 4 — verify)
- Modify: `ba2_trade_platform/version.py` — bump build number by 1 (repo convention: bump before every push).

**Step 1:** Re-read each site; fix any leftover "removed/DEAD/STALE" phrasing that contradicts the new opt-in reality (keep the history note: unconditional revival was rejected because historical configs carry dead 5.0/2.0 defaults).

**Step 2:** Full test suites one last time:
- Backend: `cd testplatform/backend && C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest -q` → same pre-existing failures only.
- Root: `cd <repo root> && C:\Users\basti\ba2-venvs\trade\Scripts\python.exe -m pytest -q` → same as baseline.
- Frontend: `npx tsc --noEmit -p .` → clean.

**Step 3: Commit**

```bash
git add -A
git commit -m "docs(backtest): entry-bracket comment sweep + version bump"
```

---

## Explicitly OUT of scope (YAGNI)

- **Arbitrary actions on entry rules in the UI** (generic action list like the live rule editor): the bracket (TP+SL, one reference choice) covers the actual optimization goal; a generic entry-action builder is a separate feature.
- **Live-platform changes**: live already works (evaluator is shared, in `ba2_common`); nothing to change there. Do NOT touch `TradeActionEvaluator.py`/`TradeActions.py`/`rule_builders.py` — any behavior gap found in them is a STOP-and-report, not a local patch, since live trades through that code.
- **Migrating S1-S3 templates** to the bracket: their condition-driven exits are the design; only S4 had dead bracket intent.
- **The legacy 'ml' engine** (`backtest_handler.py` MLStrategy tp/sl): untouched, it has its own working bracket.

## Verification of live parity (manual, after Task 4)

Run one tiny backtest with `entry_bracket=True, initial_tp_percent=10, initial_sl_percent=5` and compare the seeded EventAction JSON against the live prod row (eventaction id=1 in `C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite`): same action_type strings, same reference_value vocabulary, adjust configs parse through the same `action_from_rule`/evaluator path. The transaction after entry must carry `take_profit`/`stop_loss` exactly like live's `adjust_tp_sl(source="ruleset")` writes them.
