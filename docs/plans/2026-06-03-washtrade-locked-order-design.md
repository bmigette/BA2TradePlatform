# WASHTRADE_LOCKED order status — design

Date: 2026-06-03
Status: Approved design, ready for implementation plan

## Problem

Multiple expert instances share a single Alpaca account (e.g. 10 FactorRanker
instances on account 4). Each instance only sees its own holdings, so one
instance can submit a MARKET **BUY** for a symbol while another submits a MARKET
**SELL** for the same symbol in the same rebalance cycle. Alpaca rejects the
second order:

```
code 40310000 — "potential wash trade detected. use complex orders"
reject_reason: "opposite side market/stop order exists"
```

Today the rejected order lands in `ERROR` (terminal) and is lost. 13 such
orders were found stuck in `ERROR` on account 4.

The wash-trade rule is an **account-level** constraint at the broker: an
opposite-side order *working at the broker* on the same symbol blocks a new
plain market/stop order. Bracket/OCO ("complex") orders are exempt.

## Goal

Instead of failing, hold the blocked order and submit it automatically once the
opposing order clears. Keep the logic broker-agnostic.

## Design

### 1. New order status

Add `OrderStatus.WASHTRADE_LOCKED = "washtrade_locked"`.

Semantics mirror `WAITING_TRIGGER` — an **unsent, in-DB-only, active** state:

- Add to `get_active_statuses()` (still in flight).
- Add to `get_unsent_statuses()` (never sent to broker; safe to cancel without
  broker communication).
- **Not** in `get_unfilled_statuses()` (that set means "working at the broker").
- **Not** in `get_terminal_statuses()`.

This keeps locked orders out of every "live at broker" code path and keeps the
owning transaction visible (non-terminal) in the UI.

### 2. Lock gate in submit_order (broker-agnostic)

In `AccountInterface.submit_order()`, after the order is validated and persisted
(current behaviour) but **before** `_submit_order_impl()`:

1. Determine whether the order is **subject** to the gate:
   - Subject: primary opening/closing orders — `depends_on_order is None` and a
     real entry/close type (MARKET and the plain entry LIMIT/STOP types).
   - Excluded: protective TP/SL legs (they carry `depends_on_order`, or are
     submitted as OCO/bracket "complex" orders). These are inherently
     opposite-side and are exempt from the wash-trade rule, so they must never
     be locked.
2. If subject, scan the account for any **opposite-side order working at the
   broker** on the same symbol: status in
   `get_unfilled_statuses() ∪ {PARTIALLY_FILLED}`. Account-wide — across all
   experts and transactions.
   - Locked orders are **not** counted as "working" (they are not at the
     broker), so two opposing locked orders cannot deadlock: whichever side has
     nothing live at the broker submits on the next cycle.
3. If a blocker is found: set status `WASHTRADE_LOCKED`, persist, skip
   `_submit_order_impl()`, return the order (no broker call).
4. Otherwise: submit normally.

### 3. Refresh promotion

Add a Step 3 to `TradeManager.refresh_accounts()`, a sibling of
`_check_all_waiting_trigger_orders()`, named e.g.
`_check_all_washtrade_locked_orders()`. It runs **after** Step 1 has re-synced
broker order state:

For each `WASHTRADE_LOCKED` order, re-run the opposing-order scan for its
symbol/account. If the symbol is now clear of opposite-side working orders,
re-submit it via `submit_order()` (which re-validates and sends it). If still
blocked, leave it locked.

### 4. Dependent (WAITING_TRIGGER) chains

No special handling needed. A TP/SL `WAITING_TRIGGER` order whose parent is
`WASHTRADE_LOCKED` stays waiting because the parent is not in a trigger status
(`classify_waiting_trigger` returns "wait"). Once the parent submits and fills,
the next refresh promotes the dependent normally.

### 5. Lifecycle

- **No expiry.** A locked order is retried every refresh until the opposing
  order clears. (Caveat: a locked order could in principle fire much later at a
  different price. Accepted risk — in practice these are same-session rebalance
  market orders that clear within a cycle or two. Revisit if locks are observed
  surviving across sessions.)

### 6. UI (live-trades)

- Add a distinct colour for `WASHTRADE_LOCKED` in
  `core/utils.get_order_status_color()` (amber/hold style) with a tooltip
  "waiting on opposite-side order to clear".
- No new filter chip. Because the status is active/non-terminal, locked orders
  appear nested under their transaction (locked open-buy → WAITING transaction;
  locked close-sell → OPENED transaction), both shown by default filters.

## Files touched (anticipated)

- `core/types.py` — new enum value + status-set membership.
- `core/interfaces/AccountInterface.py` — lock gate in `submit_order()`; helper
  to scan for opposing working orders.
- `core/TradeManager.py` — `_check_all_washtrade_locked_orders()` + wire into
  `refresh_accounts()`.
- `core/utils.py` — status colour.
- Tests — gate locks on opposing working order; excludes TP/SL legs; refresh
  promotes when clear; no deadlock between two locked opposing orders.

## Out of scope

- Netting deltas across FactorRanker instances (a separate, complementary fix
  that would reduce how often opposing orders are even generated).
- Migrating the existing 13 `ERROR` orders — handle separately (they are stale;
  likely just leave terminal or re-issue manually).
