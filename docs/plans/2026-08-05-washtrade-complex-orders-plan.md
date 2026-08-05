# Wash-trade deadlock — implementation plan

Date: 2026-08-05
Background and evidence: **`docs/WASHTRADE-LOCK.md`** (read it first — this plan
assumes the probe table there).

**Goal:** stop entries deadlocking behind protective stops that never clear.

**Scope note:** this is live-trading code (`AccountInterface`, `AlpacaAccount`,
`TradeManager`). The testplatform grid does not use it. Every change is additive
and gated on the branch that today would lock, so an uncontended order takes the
byte-identical path it takes now.

Originally scoped as three fixes. **Fix 1 (phantom-order reconciliation) was
withdrawn** — it rested on a 404 that turned out to be a probe run with the wrong
account's credentials. `refresh_orders` Step 4 is correct. See the "Probe trap"
note in the reference doc.

---

## Task 1 — Complex-order entry on the contended branch

**Problem.** `AccountInterface.submit_order` locks any primary order that has an
opposing market/stop order working. When that blocker is a protective stop on an
open position, it never clears and the entry is stuck forever (13 orders, oldest
9 days).

**Fix.** On that branch, if we have a TP and/or SL for the order, submit it as a
complex order instead of locking. Alpaca exempts `BRACKET`/`OTO`/`OCO` from the
wash-trade check (probe rows 4, 6).

**Files:**
- `packages/common/ba2_common/core/interfaces/AccountInterface.py` — the gate.
- `ba2_trade_platform/modules/accounts/AlpacaAccount.py` — `_submit_order_impl`
  complex-order request construction.
- `tests/test_washtrade_lock.py` — new cases.

**Design:**

1. Add `use_complex_order: bool = False` to the `_submit_order_impl` signature
   (default False keeps every other broker and every other call site unchanged;
   `IBKRAccount`/`TastyTradeAccount` ignore it).
2. In `submit_order`, replace the current lock-and-return with:
   - blocker found **and** (`tp_price` or `sl_price`) → call
     `_submit_order_impl(..., use_complex_order=True)` and skip the lock.
   - blocker found and neither price → lock as today (nothing else is possible).
3. In `AlpacaAccount._submit_order_impl`, when `use_complex_order` is set, build
   the same `MarketOrderRequest`/`LimitOrderRequest` as now but add:
   - both prices → `order_class=BRACKET`, `take_profit=TakeProfitRequest(...)`,
     `stop_loss=StopLossRequest(...)`
   - one price → `order_class=OTO` with the one leg present.
   Prices go through `_round_price` exactly like the existing OCO path.
4. **Do not** let the post-submit `adjust_tp_sl`/`adjust_tp`/`adjust_sl` block in
   `submit_order` run for a complex submission — the broker already created the
   legs and a second call would duplicate them. Return a marker on the order (or
   skip via the same `use_complex_order` condition) so the bracket block is
   bypassed.
5. Adopt the broker's legs into our own rows. `alpaca_order_to_tradingorder`
   already extracts `legs_broker_ids` for OCO/MLEG; extend the same handling to
   `BRACKET`/`OTO` so `_insert_oco_legs_from_broker_ids` creates the TP/SL
   `TradingOrder` rows with the Alpaca-generated ids. This is the part that can
   hurt if rushed — legs carry broker ids, not our numeric `client_order_id`s.

**Tests:**
- blocked + both prices → `_submit_order_impl` called with
  `use_complex_order=True`, status is not `WASHTRADE_LOCKED`.
- blocked + only SL → same, OTO shape.
- blocked + no prices → still `WASHTRADE_LOCKED` (fallback preserved).
- not blocked → `use_complex_order=False`, request shape unchanged from today
  (regression guard for the uncontended path).
- complex submission does not also call `adjust_tp_sl`.

---

## Task 2 — Lock expiry

**Problem.** `_check_all_washtrade_locked_orders` retries forever and logs
still-blocked orders at `debug`. The 06-03 design said "no expiry" and set the
revisit trigger "locks surviving across sessions"; that fired and nothing
alarmed. Task 1 removes the common cause but not the residual one (entries with
no TP and no SL).

**Fix.** In `ba2_trade_platform/core/TradeManager.py:322`:

1. Compute the lock's age from `created_at`.
2. Past a threshold — new account setting `washtrade_lock_max_age_hours`,
   default 24 (one trading session) — cancel instead of retrying: set the order
   terminal, close its transaction as rejected, and log at `warning` naming the
   blocker id. A market entry signal a day stale should not fill.
3. Below the threshold but past ~1 hour, log the still-blocked message at
   `warning` rather than `debug`, so the next deadlock is visible without a DB
   query.

**Tests:**
- fresh lock, still blocked → left locked, retried.
- lock older than the threshold, still blocked → canceled, transaction closed.
- lock older than the threshold but now clear → submits normally (clearing wins
  over expiry).

---

## Task 3 — Clear the current backlog — NOT NEEDED

Originally scoped as a one-off cancel script. Task 2 makes it redundant: all 13
stuck orders are 2–9 days old, so the first refresh after the platform restarts
on this build expires them automatically — cancelled, protective legs cancelled,
`WAITING` transactions marked `FAILED`, each with a `WARNING` naming the blocker.

Nothing to run. Just restart the live platform and read the log.

---

## Status (2026-08-05)

- **Task 1 — done.** `use_complex_order` added to the `_submit_order_impl`
  contract and all four implementations; gate rewritten; adjust-block skipped on
  the complex path; `BRACKET`/`OTO` legs adopted via the existing OCO leg reader.
- **Task 2 — done.** `_washtrade_lock_age_hours` + `_expire_washtrade_locked_order`
  in `TradeManager`, with `_WASHTRADE_LOCK_MAX_AGE_HOURS = 24.0` and
  `_WASHTRADE_LOCK_WARN_AGE_HOURS = 1.0` as module constants (not account
  settings — a safety backstop should not be per-account configurable, and it
  avoids a migration).
- **Tests:** 12 new in `tests/test_washtrade_lock.py` (27 total, all green).
  Suites: main 1178 passed; ba2_common 317 passed + 5 known order-dependent
  option failures (pending task #143, pass in isolation); ba2_experts 349;
  ba2_providers 140; testplatform 1199 passed + 3 pre-existing unrelated
  failures (Senate remote-slot cap, `3 == 4`).
- Version bumped to `2026.08.1019`.

**Verified against the broker, not just unit-tested:** the exact request shape
this code builds (`MarketOrderRequest` + `order_class=BRACKET` + legs) was
submitted to the live paper API on 2026-08-05 against a real standing blocker and
was ACCEPTED and FILLED, where the same order without `order_class` was rejected
40310000. Post-construction mutation of the alpaca-py request object (how the
code attaches the legs) was separately confirmed to work.

**Not yet done:** restart the live platform on this build. Until then the 13
locked orders stay locked.
