# PennyMomentum exit fixes — design

Date: 2026-06-01
Status: implemented (TDD) — Fix 1, Fix 2, Fix 3; partial-fill-then-expire edge case
left as-is per decision below.

Tests: `tests/test_penny_tier_tracking.py` (11), `tests/test_transaction_helper_fixed_qty.py`
(2), `tests/test_penny_exit_staging.py` (5), plus updated tier assertions in
`tests/test_penny_fixes.py`. Full suite: 330 passed / 75 failed, where all 75 failures
pre-exist on a clean tree (verified via stash) and are unrelated to this work.

## Problem

Transaction 42 (BBAI, expert TestPenny-4) closed through **9 orders**. Two distinct
defects produced this:

1. **Tier re-fire.** The exit comments show `take profit tier 1` firing six times
   (`33%` once, `50%` five times) plus a final `tier 3 (100%)`. Each cycle sold ~50%
   of the *remaining* shares, walking the position down in many small sells.

2. **Wash-trade rejection.** Order 118 (`SELL 23 BBAI`, market) was rejected by Alpaca
   with code `40310000` "potential wash trade detected" because the entry BUY order
   (order 83, broker id `2341a54f-…`, limit ~$5.64) was still open at the broker when
   the exit SELL was submitted. Alpaca blocks an open SELL while an opposing BUY is
   open on the same symbol.

These are independent and are fixed separately.

## Root causes

### Tier re-fire
`triggered_tp_tiers` is tracked **by list index**, and on every LLM exit-condition
refresh it is wiped:

```python
# PennyMomentumTrader/__init__.py ~2740
if "take_profit" in updated:
    info["exit_conditions"]["take_profit"] = updated["take_profit"]
    info["triggered_tp_tiers"] = []          # <-- wipes "already sold" memory
```

`info` (including `triggered_tp_tiers`) is persisted in
`MarketAnalysis.state["monitored_symbols"]` and carried across monitoring cycles, so
this reset permanently re-arms tiers that already fired. The LLM refresh runs every
~15–20 min and almost always returns a `take_profit` key, so tier 1 re-fires each cycle.

### Wash trade
`PennyTradeManager.execute_exit` (`trade_manager.py:285`) always builds the exit SELL as
a plain `PENDING` market order and submits it immediately, with no awareness of whether
the entry BUY is still open at the broker.

## Fix 1 — Identity-based tier tracking (Option A)

Make a take-profit tier fire **exactly once**, regardless of how the LLM rewrites the
tier list on refresh.

**Data model**
- Each take-profit tier dict gains a stable `id` field (string).
- Ids are minted from a per-symbol monotonic counter stored on `info`
  (`info["_next_tier_id"]`), keeping them JSON-safe inside `MarketAnalysis.state`.
- Triggered state moves from `info["triggered_tp_tiers"]` (list of indices) to
  `info["triggered_tp_tier_ids"]` (list of tier ids).

**Lifecycle**
- *Creation* (entry / first exit conditions): assign each tier an `id` as stored.
- *Refresh* (replace the blind reset at ~2740): merge the LLM's new tier list with the
  existing one **by position** — surviving positions keep their `id` (and therefore
  their fired status); genuinely new positions get fresh ids; removed positions drop out.
  A tier that already fired is **never re-armed**, even if its condition was rewritten
  (trailing). This eliminates the re-fire.
- *Firing* (~2321): `if tier["id"] in triggered_ids: continue`; on success append the id.
- *Migration*: stored `info` dicts have index-based `triggered_tp_tiers` and id-less
  tiers. On load, lazily assign ids and translate old indices → ids by position so live
  positions don't lose fired history on deploy.

`execute_exit`'s `exit_pct` ("% of remaining") semantics are unchanged.

**Accepted limitation:** if the LLM *removes* a fired tier and later *re-adds* a similar
one, it is treated as new and may fire again. Rare and acceptable.

## Fix 2 — Stage exit as WAITING_TRIGGER when the entry is still open

When an exit tier fires but the entry BUY is still unfilled/open, do **not** cancel the
buy and do **not** submit the SELL immediately (which wash-trades). Instead stage the
SELL as a triggered order and mark the tier triggered right away so the exit-step
calculation is not redone every cycle.

**In `execute_exit`, per transaction, before submitting the SELL:**
1. Look up the entry BUY order for the transaction (the `side == BUY` order; pick the one
   in an unfilled/open status if present).
2. **If an open BUY exists:** create the SELL with
   - `status = OrderStatus.WAITING_TRIGGER`
   - `depends_on_order = entry_buy.id`
   - `depends_order_status_trigger = OrderStatus.FILLED`

   Do **not** call the broker. `TradeManager._check_waiting_trigger_orders` submits it
   once the buy reaches `FILLED`. If the buy is already filled, the trigger fires on the
   next monitor pass ("might close right away").
3. **If no open BUY exists (normal case):** submit immediately, exactly as today.

The existing guard that skips when a pending SELL already exists is retained;
WAITING_TRIGGER is an unfilled status, so a staged sell won't be duplicated.

### CRITICAL: protect the partial-exit quantity from qty-sync

A stepped exit deliberately sells **less** than the position (e.g. entry qty 100, tier
sell 50). Two code paths touch a `WAITING_TRIGGER` dependent order's quantity — one is
safe, one is **not**:

- `TradeManager._check_waiting_trigger_orders` only copies the parent's qty when the
  dependent's qty is `0` (lines ~350-357). A staged sell carrying a real qty is left
  alone. **Safe.**
- `TransactionHelper.adjust_qty` (and its sibling `adjust_quantity_with_tpsl`) **force
  the dependent's qty to the whole transaction quantity** once the entry is executed
  (lines ~104-112). For a partial tier this turns a 50-share exit into a 100-share full
  exit. **This breaks stepped exits.** These paths are invoked by SmartRiskManager,
  risk-management, and manual UI quantity edits — so a penny position touched by any of
  them would be corrupted.

**Fix:** mark each penny stepped-exit order with a fixed-quantity flag and have the
qty-overwrite path skip it.
- Store the flag in the existing `TradingOrder.data` JSON column (no schema change),
  e.g. `data = {"fixed_quantity": True}`, set when `execute_exit` creates the order
  (both the immediate-submit and WAITING_TRIGGER cases — a partial exit must never be
  resized regardless of how it was submitted).
- In `TransactionHelper.adjust_qty`, skip the `dep_order.quantity = new_quantity`
  overwrite for any dependent order whose `data.get("fixed_quantity")` is truthy (leave
  entry-order handling unchanged). This is the only direct quantity-overwrite path.
  `adjust_quantity_with_tpsl` cancels/recreates TP/SL orders rather than overwriting a
  quantity, and both are reached only via the SmartRiskManager path (which PennyMomentum
  does not use today) — so the flag is defensive/future-proofing, kept minimal.
- The `WAITING_TRIGGER` submit path in `TradeManager` only fills qty when it is `0`, so
  a staged sell carrying a real qty is already safe there; no change needed.

**Edge case — partial fill then expiry.** A `good_for='day'` entry that partially fills
then expires ends in a terminal status that is **not** `FILLED`. A SELL staged on
`== FILLED` would then stay stuck in WAITING_TRIGGER while its tier is already marked
triggered, so that tier would never exit. `_check_waiting_trigger_orders` requires an
exact trigger status, so "any terminal status" is not available here.

**Decision: leave as-is (no safety net).** If this rare case occurs the staged SELL may
fail as a wash trade; the order's stored error + broker order states in the logs will be
enough to diagnose and decide how to handle it at that point. Not worth pre-building a
sweep for a fast-filling penny entry.

## Scope / files

- `ba2_trade_platform/modules/experts/PennyMomentumTrader/__init__.py`
  - tier id assignment, merge-on-refresh (replace reset at ~2740), firing check (~2321),
    migration on load.
- `ba2_trade_platform/modules/experts/PennyMomentumTrader/trade_manager.py`
  - `execute_exit`: detect open entry BUY, stage SELL as WAITING_TRIGGER when present;
    set `data["fixed_quantity"] = True` on every partial exit order.
- `ba2_trade_platform/core/TransactionHelper.py`
  - `adjust_qty` and `adjust_quantity_with_tpsl`: skip qty update for dependent orders
    flagged `fixed_quantity`.
- (optional) safety-net sweep — location TBD (TradeManager or PennyMomentum monitor).

No DB schema change: `triggered_tp_tier_ids` and tier `id` live in the existing JSON
`MarketAnalysis.state`; `depends_on_order` / `depends_order_status_trigger` /
`WAITING_TRIGGER` and the `data` JSON column already exist on `TradingOrder`.

## Testing

- Unit: tier merge preserves fired ids across refresh; rewritten fired tier does not
  re-fire; new tier added by LLM can fire; migration translates legacy indices.
- Unit: `execute_exit` stages WAITING_TRIGGER when an open BUY exists; submits
  immediately when none; respects the existing pending-SELL guard.
- Unit: a partial exit order is flagged `fixed_quantity`, and `adjust_qty` /
  `adjust_quantity_with_tpsl` leave its quantity untouched while still resizing the
  entry order.
- Regression: replay the transaction-42 condition sequence → expect the intended 2–3
  tier exits, not 6+.
