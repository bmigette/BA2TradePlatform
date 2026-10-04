# Allocator per-symbol TP/SL protection (TastyTrade) -- design (2026-10-04)

Status: design, branch `feat/alloc-tp-sl` (from origin/dev f6466171). LIVE real-money order
placement: nothing here has ever been run against TastyTrade. Everything is built and tested
against a fake of the SDK complex-order API; the first real use is a supervised test (checklist
in the final report).

## 0. Operator decisions (2026-10-04, fixed input)

* OFF by default. Set PER SYMBOL from an icon ("Set TP/SL") on the allocator symbol row
  (desktop table) and the phone card. No label or account defaults.
* ONE stop-loss PRICE and one or MORE take-profit targets (price + share of the position per
  target, e.g. 3 targets x 1/3). Implementation: one TastyTrade OCO complex order per TP
  target, each covering its slice of shares: `[TP limit sell slice_i @ tp_i] OCO [stop sell
  slice_i @ SL]`. Slices sum to the protected quantity; the stop is split across the OCOs at the
  same SL price. Plain price stop, no dividend adjustment, same for income ETFs.
* After ANY TP or SL fill the symbol is HELD: excluded from allocator rebalancing (no buys, no
  sells) until the operator re-enables it. Visible badge "Held after TP/SL fill on <date>" and
  a re-enable action.
* Order lifetime GTC.

Decision taken here on the one ambiguity: a TP-slice fill ALSO holds the symbol ("any
protective fill holds"). The remaining OCO slices stay live and keep being reconciled.

## 1. Facts established from the code and SDK (tastytrade 12.4.1, read locally)

| Fact | Source | Consequence |
|---|---|---|
| `Account.place_complex_order(session, NewComplexOrder, dry_run=True)`; `dry_run` DEFAULTS TO TRUE | `account.py:917` | always pass `dry_run=False` explicitly; use `dry_run=True` once first as a validation pass |
| `NewComplexOrder(orders=[NewOrder, NewOrder], type=OCO)`; `type` defaults to OCO | `order.py:316` | no trigger order; two plain `NewOrder`s |
| `PlacedComplexOrder` has NO top-level status: `orders: list[PlacedOrder]`, `terminal_at`, `id` | `order.py:395` | complex-order state is DERIVED from the member orders' statuses and fills |
| `OrderStatus` includes `EXPIRED`, `CANCELLED`, `REMOVED`, `PARTIALLY_REMOVED`, `CONTINGENT`, `CANCEL_REQUESTED`, `REJECTED` | `order.py:58` | a lost protection is observable as EXPIRED/CANCELLED members |
| `NewOrder.gtc_date`, `PlacedOrder.gtc_date` exist | `order.py:281,354` | the broker states a GTC end date; recorded on every slice |
| `get_live_complex_orders` returns "complex orders placed TODAY" | `account.py:755` | NOT usable to find a GTC order placed last week. Reconcile reads each placed id with `get_complex_order(id)` |
| `delete_complex_order(id)` returns None; nothing says the cancel completed | `account.py:792` | cancel is a REQUEST: poll `get_complex_order` until every member is terminal before treating the shares as released |
| Fractional quantity is refused on EVERY priced order type (`fractional_market_orders_only`, 2026-08-21 dry-run) | `TastyTradeAccount._refuse_fractional_priced_order` | protect only `floor(position)` whole shares; the fractional remainder is unprotected and the UI says so |
| TIF on a MARKET order must be DAY/IOC (`tif_market_orders_not_supported`, 2026-10-02, 47f67180) | `_tt_market_time_in_force` | does not apply to the limit/stop legs (GTC is fine for priced orders); the allocator's own market sells keep using DAY |
| TastyTrade does not give a resting-order reservation on the position read | `get_positions` sets `qty_available = qty` | the platform itself must never sell shares an OCO reserves: cancel-before-trade |
| `refresh_orders` walks `get_order_history`, which also lists OCO member orders; members carry no platform `external_identifier` row | `TastyTradeAccount.refresh_orders` | they match no `TradingOrder` and are skipped (`continue`): protection orders never pollute the order table. Members are tagged `external_identifier="ba2prot:<protection_id>:<slice>"` (non-numeric, so the integer lookup raises ValueError and is already tolerated) for human traceability on the TT site |

### GTC lifetime (could not be verified locally)

Nothing in the SDK, the repo docs or `docs/` states how long a TastyTrade GTC order lives. The
SDK only proves orders CAN end as `EXPIRED` and that a `gtc_date` exists. My recollection is that
TastyTrade GTC orders end after about 90 days and are also cancelled on some corporate actions
(splits, symbol changes); that is NOT verified. Therefore the design never relies on a number:

1. Every placement stores the broker-reported `gtc_date` per slice (from the placed order). If
   the broker returns none, an ASSUMED expiry `placed_at + ASSUMED_GTC_LIFETIME_DAYS (90)` is
   stored and flagged `gtc_date_assumed`.
2. The reconcile classifies an EXPIRED/CANCELLED/REJECTED member as a LOST protection and alerts
   loudly (section 6). It does not matter why it ended.
3. A WARNING alert fires `GTC_EXPIRY_WARN_DAYS (7)` days before the stored date, once per slice.
4. A "Re-place protection" action (cancel remaining + place fresh) renews. NOT automatic (see
   open question 3).

Supervised-test item: read `gtc_date` on the placed order on tastytrade.com and compare to the
stored one.

## 2. Scope boundaries

* Allocator-owned protection layer. `TastyTradeAccount.adjust_tp/adjust_sl/adjust_tp_sl/
  modify_order` KEEP raising NotImplementedError / refusing: the expert paths must still be
  refused (`supports_protective_legs` stays False). The new methods have different, explicit
  names.
* Long equity positions only. A short position, an option, or a symbol with no position is
  refused with a reason.
* Nothing under `packages/`. The model, store, service, pure helpers and UI live in-tree
  (`ba2_trade_platform/core/allocator_protection*.py`) because they touch a broker and the
  live DB: "live-only code belongs in-tree". Consequence: NO `PACKAGE_VERSION` bump and NO
  `ga_neutral_package_paths` change are needed, and the backtest/GA path cannot import any of
  it. (APP_VERSION is bumped by the operator at ship time; this branch bumps nothing.)
* The table model is declared in-tree. `init_db()` lives in `ba2_common` and cannot import it,
  so `main.initialize_system()` imports the module BEFORE `init_db()` (create_all then builds
  the table on any DB), `alembic/env.py` imports it so autogenerate sees it, and an Alembic
  revision (idempotent: `has_table` guards, same pattern as `f1c8a24b7e05`) exists for
  databases managed by `migrate.py upgrade`.

## 3. Data model

Two tables (Alembic revision, never raw SQL).

`allocator_protection` -- one row per (account_id, symbol), unique:

| column | meaning |
|---|---|
| id, account_id (FK accountdefinition, CASCADE, indexed), symbol (upper) | identity |
| enabled | operator intent. False = switched off (numbers kept, no orders live) |
| sl_price | the one stop price (> 0) |
| tp_targets_json | `[{"price": float, "fraction": float}]`, fractions sum to 1, prices distinct and ascending |
| held_at, held_reason | set by a fill; NULL = not held. `held_reason` e.g. "TP target 2 filled (slice 2: 4 sh @ 61.50)" |
| pending_replace | True while WE cancelled the orders (rebalance/resize) and still owe the re-placement |
| alert_code, alert_message, alerted_at | last loud alert (dedupe: same code is not re-logged every refresh) |
| last_error | last refusal text from the broker/validation, shown in the UI |
| protected_quantity | whole shares covered by LIVE slices at last reconcile (display cache) |
| created_at, updated_at | |

`allocator_protection_order` -- one row per OCO slice:

| column | meaning |
|---|---|
| id, protection_id (FK, CASCADE) | |
| slice_index, target_index | slice number; which TP target it serves |
| quantity (int whole shares), tp_price, sl_price | what was sent |
| complex_order_id (int, nullable until the broker answers), tp_order_id, sl_order_id | broker ids |
| state | `LIVE` / `CANCELLING` / `CANCELLED_BY_US` / `FILLED_TP` / `FILLED_SL` / `LOST_EXPIRED` / `LOST_CANCELLED` / `LOST_REJECTED` / `UNKNOWN` |
| filled_qty, fill_price | from the member fills |
| gtc_date, gtc_date_assumed | see 1 |
| placed_at, closed_at | |

Terminal states other than LIVE/CANCELLING are history; `LOST_*` and `UNKNOWN` are the alarms.

### Status shown on the page (derived, never stored)

`protection_status(config, slices, position_qty)` -> one of:

* `OFF` -- no row, or `enabled` False. (Icon outline.)
* `HELD` -- `held_at` set. Badge "Held after TP/SL fill on <date>"; if slices are still live the
  tooltip lists them. Takes precedence over everything except it still shows an alert marker if
  a remaining slice was lost.
* `NO_POSITION` -- enabled, whole-share position 0 (nothing to protect yet; not an alarm).
* `PROTECTED` -- live slice quantity == floor(position). Tooltip notes any fractional remainder
  ("protecting 10 of 10.4 shares: fractional part cannot carry a stop on TastyTrade").
* `PARTIAL` -- 0 < live slice quantity < floor(position) (position grew, or a slice was lost).
* `UNPROTECTED` -- enabled, floor(position) > 0, live slice quantity 0, or `pending_replace`
  stuck, or any LOST_/UNKNOWN slice. Red, with the reason.
* `REPLACING` -- `pending_replace` and not yet stale (we are mid-rebalance; amber).

## 4. Pure helpers (`allocator_protection.py`, no IO, fully unit-tested)

* `validate_protection(sl, tps, last_price, position_qty, tick)` -> errors list: SL > 0 and
  strictly below the current price; every TP strictly above the current price; TP prices distinct
  (after tick rounding); at least one TP; fractions each in (0,1] and sum to 1.0 within 1e-6;
  at least `n_targets` whole shares or the target count is reported as unplaceable.
* `round_price_to_tick(price, tick, mode)`: a TP limit sell rounds to the NEAREST tick; an SL
  stop trigger rounds DOWN (never closer to the market than asked). Tick from `Equity.tick_sizes` when present (threshold-aware),
  else $0.01 (>= $1) / $0.0001 (< $1).
* `split_quantity(whole_shares, fractions)` -> `[int]`, largest-remainder, sum == whole_shares,
  zero-share slices dropped (their fraction folds into the next slice up) and reported.
* `plan_slices(whole_shares, targets, sl)` -> `[SlicePlan(index, target_index, qty, tp, sl)]`.
* `remaining_targets(targets, consumed_target_indexes)` -> targets renormalised to sum 1, for
  re-placement after a TP slice filled.
* `classify_complex_order(placed, tp_price, sl_price)` -> `SliceObservation(state, filled_qty,
  fill_price, kind)`; the rules are in section 5.3.
* `protection_status(...)` as above; `preview_orders(...)` for the dialog.

## 5. Lifecycle

All broker IO is in `TastyTradeAccount` (explicit names, below); orchestration and DB are in
`allocator_protection_service.py`. One per-account RLock (`_submission_lock` is reused: the
allocator run already holds it, and the service RLock is re-entrant) serialises every
mutation.

### 5.1 TastyTradeAccount surface

`supports_allocator_protection = True` (class attr; False on every other account).

* `place_protective_oco(symbol, quantity, tp_price, sl_price, tag)` -> `ProtectiveOcoResult`.
  Refuses: not authenticated, fractional/zero/negative quantity, tp <= sl, equity not found.
  Builds `NewOrder(LIMIT, GTC, price=+tp, SELL_TO_CLOSE)` and `NewOrder(STOP, GTC,
  stop_trigger=sl, SELL_TO_CLOSE)` (plain STOP = market stop; decision: a stop-limit can be
  jumped by a gap and never fill, which the repo's own `_force_close_breached_stops` exists to
  patch; a market stop trades through the gap. Open question 5), wraps them in
  `NewComplexOrder(orders=[tp, sl], type=OCO)`, runs `place_complex_order(dry_run=True)` first
  and refuses on any `errors`, then `dry_run=False`. Reads the placed complex order back with
  `get_complex_order` and raises `ProtectionRefused` unless every member is live-ish. Returns
  the ids, statuses and `gtc_date`.
* `get_complex_order_state(complex_order_id)` -> the `PlacedComplexOrder` (or raises).
* `cancel_complex_order(complex_order_id)` -> `delete_complex_order`, then polls
  `get_complex_order` (2s steps, 30s cap) until every member is terminal. Returns
  `CancelOutcome(confirmed: bool, final: PlacedComplexOrder)`. `confirmed=False` is a LOUD
  failure, never "assume cancelled".
* `list_live_complex_orders()` -> `get_live_complex_orders` (today's only: used as a
  cross-check/diagnostic, not as the source of truth).

### 5.2 Operations (service)

* `save_protection(account, symbol, sl, tps)` (UI Save): validate against a FRESH price and
  position; write the config row (`enabled=True`, `held_at=NULL` is NOT cleared here -- a held
  symbol must be re-enabled explicitly); if no live slices exist and the symbol is not held,
  place them (`place_slices`). On a broker refusal: nothing is left half-placed (any slice
  placed earlier in the same call is cancelled; if THAT cancel is unconfirmed the state is
  UNPROTECTED+alert and the unconfirmed ids are recorded), `last_error` is set, activity-log
  FAILURE, and the status shows UNPROTECTED with the reason.
* `place_slices(account, protection)`: quantity = floor(broker position) (fresh), minus the
  quantity already covered by LIVE slices (normally 0); `plan_slices`; place sequentially,
  recording each slice row BEFORE the network call as `state=PLACING`... (see 5.5) and
  updating it after.
* `disable_protection(account, symbol)`: cancel every live slice with confirmation; `enabled=
  False`; numbers kept. If any cancel is unconfirmed the slice stays CANCELLING and an alert is
  raised (the orders may still be live at the broker).
* `delete_protection`: disable then delete the config rows (history in the activity log).
* `reenable_symbol(account, symbol)`: clears `held_at/held_reason` only. If the hold came from
  an SL fill the config is switched `enabled=False` (the stop already fired; protecting a
  re-bought position at stale prices unasked would be wrong), if from a TP fill the consumed
  targets are removed and the rest renormalised; live slices stay (they already match the
  reduced position) and the next reconcile verifies that.
* `replace_protection(account, symbol)` (UI "Re-place protection", also the rebalance
  re-placement): cancel live slices with confirmation, then `place_slices` with the
  remaining targets. Renews GTC.

### 5.3 Reconcile (`reconcile_account(account, repair=False)`)

Called (a) from `TradeManager`'s account refresh loop after the order refresh, (b) from the
page's Refresh, (c) by the allocator service before a run. For every enabled-or-held protection
with non-terminal slices:

1. One broker read per live slice: `get_complex_order(id)` (cheap; `get_live_complex_orders`
   only lists today's).
2. `classify_complex_order`:
   * any member with fills > 0 or status FILLED -> `FILLED_TP` (limit member) or `FILLED_SL`
     (stop member), `filled_qty`, `fill_price` from the fills. A partially filled live member
     counts as a fill (any protective fill holds).
   * all members LIVE/RECEIVED/CONTINGENT/ROUTED/IN_FLIGHT -> `LIVE`.
   * CANCEL_REQUESTED anywhere -> `CANCELLING`.
   * any EXPIRED -> `LOST_EXPIRED`; any REJECTED -> `LOST_REJECTED`; all CANCELLED/REMOVED/
     PARTIALLY_REMOVED -> `CANCELLED_BY_US` if we had asked, else `LOST_CANCELLED` (somebody
     cancelled it on the TT site or TT cancelled it).
   * anything else (mixed, unmapped) -> `UNKNOWN` = alarm, never treated as live.
3. A fill: set `held_at=now`, `held_reason`, ActivityLog (SUCCESS, "TP/SL filled") and an
   ALERT-level log so the operator sees the position changed under them. Remaining live slices
   keep resting and are still reconciled.
4. A LOST/UNKNOWN slice: alert (6). The slice is NOT silently re-placed in the background:
   the operator may have cancelled it deliberately on the TT site. The UI shows UNPROTECTED and
   offers "Re-place protection". (Exception: `pending_replace`, which is our own unfinished
   operation -- reconcile completes it when no working allocator order remains on the symbol.)
5. Fetch failure is reported (`reconcile` returns `ReconcileReport.failed_symbols`) and logged
   at ERROR; it never flips a state to LIVE or to LOST on a failed read.
6. GTC expiry warning at `gtc_date - 7d`, once per slice.

### 5.4 Interplay with the allocator rebalance (`run_allocation`)

Inside `_run_allocation_locked`, after the existing block gates pass (so a blocked attempt
changes nothing) and before `record_allocation_run`:

1. HELD symbols are removed from the plan (`filter_plan_rows` over the rest) and re-emitted as
   SKIPPED outcomes "Held after TP/SL fill on <date>: excluded from rebalancing". The dry run
   dialog shows the same rows as skipped (page loader passes the held set), but the enforcement
   is here, at the boundary that writes -- a stale dialog cannot trade a held symbol.
2. For every remaining row that WILL trade (`side` and `delta_quantity`) on a symbol with LIVE
   slices: reconcile it first (a fill since the last look makes it HELD -> step 1 applies, row
   dropped), then `cancel_complex_order` each slice and require `confirmed`. A symbol whose
   cancel is unconfirmed has its row DROPPED from the plan and reported FAILED
   ("protective orders could not be cancelled: not traded"), and stays alerted -- the allocator
   never sells shares an OCO still reserves. Protection flagged `pending_replace=True` BEFORE the
   first cancel (so a crash mid-way leaves a durable "we owe a re-placement" marker, which the
   next reconcile completes).
3. The remaining plan is recorded and submitted as today.
4. After `measure_run_fills`, `resume_protection(account, symbols)`: for each symbol with
   `pending_replace`: if any of the run's orders on it is still working, leave it
   `pending_replace` (REPLACING); otherwise read the fresh position and `place_slices` with the
   remaining targets. A failure leaves UNPROTECTED + alert. This also runs in a `finally`, so a
   raise mid-submission still attempts to re-protect, and never itself raises.
5. The re-placement is reported in the run's activity-log description.

Sells vs reserved shares, belt and braces: `_stale_plan_block` already re-reads positions; a
SELL row additionally asserts `sum(live slice qty on symbol) == 0` at submit time (after step
2) and fails the row loudly otherwise.

### 5.5 Crash safety / idempotence

Each slice row is inserted `state=PLACING` BEFORE the broker call (carrying the
`external_identifier` tag) and updated with the ids after. A crash in between leaves a PLACING
row; reconcile treats a PLACING row older than 60s as UNKNOWN (alarm) and, because the order
may exist at the broker, the supervised checklist says to look at the TT site. (The tag lets the
operator find it; automatic adoption by tag is future work.)

## 6. Failure policy (never silent)

Every failure path does all three: `logger.error`, an `ActivityLog` entry (severity FAILURE,
type `TP_SL_ADJUSTED`, `data.kind="allocator_protection"`, `data.code`), and a persisted
`alert_code/alert_message` on the protection row that the page renders as a red banner above the
label list ("N position(s) UNPROTECTED: AAPL (stop order expired) ...") and as a red badge on
the symbol. The same code is not re-logged on every 5-minute refresh (`alerted_at` + code
dedupe; it re-fires if the code changes). There is no notification channel in the platform
(no email/telegram); the UI banner and the activity log are the channels.

Codes: `PLACEMENT_REFUSED`, `CANCEL_UNCONFIRMED`, `LOST_EXPIRED`, `LOST_CANCELLED`,
`LOST_REJECTED`, `UNKNOWN_STATE`, `REPLACE_FAILED`, `QUANTITY_MISMATCH` (PARTIAL),
`RECONCILE_FETCH_FAILED` (warning), `GTC_EXPIRING` (warning), `HELD_FILL` (info/success).

## 7. UI

* Desktop: a `protect` column (TIER_HEAD on phone) with a shield icon button + a status chip,
  one Vue fragment (`PROTECT_TEMPLATE`) shared by the table cell and the card header, like
  `SYMBOL_CHIPS_TEMPLATE`. Emits `protectClick(symbol)`. Shown only when the account class
  declares `supports_allocator_protection`; other brokers never see the column.
* Dialog (`allocator_protection_dialog.py`): current price and held quantity (whole shares +
  fractional note); SL price; a list of TP targets (price, % of position), add/remove; live
  validation messages; PREVIEW table "Order 1: sell 4 sh, limit 61.50 OCO stop 55.00 ..." with
  the whole-share note; Save / Switch off / Re-place protection / Re-enable after hold. Phone:
  the dialog uses the page's responsive classes (`ui/utils/responsive.py`).
* Styles are installed in `_install_page_styles` (before the first await); the new CSS rides
  `page_phone_css()` and the generated static file.
* Held rows in the dry-run/wizard show as skipped with the badge text.

## 8. Tests (fake SDK, no network)

`tests/test_allocator_protection_*.py`: pure helpers (tick rounding, slicing, fractions,
validation); a `FakeTastyBroker` (place complex order incl. dry-run, errors/rejection, statuses,
partial fills, cancel with a delay and a cancel/fill race, expiry, position changes) exercised
through the REAL `TastyTradeAccount` methods; service lifecycle (place, partial TP fill -> hold,
SL fill -> hold, expiry -> unprotected alert, cancel detected, rebalance cancel/replace, held
skip, unconfirmed cancel drops the row); UI construction tests (dialog builds, card template has
the protect cell, `check_card_columns`).

## 9. What is NOT verified without a broker

Real TT behaviour of: OCO acceptance for sell-to-close equity with a GTC plain stop; whether
`get_complex_order` shows member statuses as I model them (esp. which status the surviving OCO
leg takes when its partner fills, and what a TT-side expiry looks like); `gtc_date` population;
cancel latency; whether two live OCOs on one position with summed quantity == position are
accepted (should be); behaviour around corporate actions. The fake encodes my best reading of
the SDK; the supervised test exists to correct it.

## 10. Open questions for the operator

1. SL fill then re-enable: I switch protection OFF (numbers kept) rather than re-arming a stop on
   whatever is bought next. OK, or should re-enable keep it armed?
2. After a TP slice fills the Transaction row for the symbol is stale (broker holds less than
   the transaction says). Do you want the reconcile to shrink the transaction quantity on a TP
   fill, or is the hold + manual re-enable enough? (Out of scope as built; a re-enable
   writes a warning to the activity log when broker qty != tracked qty.)
3. GTC expiry: alert only (built), or auto-renew (cancel + re-place ~7 days before the date)?
   Auto-renew leaves a short unprotected gap and fights a deliberate manual cancel.
4. Background reconcile never re-places a lost slice (could be a deliberate manual cancel on
   the TT site). Confirm, or allow auto-repair for a lost STOP only.
5. Plain market STOP (built) vs stop-LIMIT. Plain stop is a market order once triggered: it
   fills through a gap, at a worse price. Stop-limit can be jumped and not fill. Preference?
6. Extended-hours: GTC (not "GTC Ext"): protection triggers in the regular session only.
   Is that acceptable?
7. Does the 5-minute-ish account refresh interval give fast enough fill detection for the
   hold? A rebalance fired between a fill and the next reconcile is covered by the per-run
   reconcile in 5.4 step 2 (only for symbols the run trades). The wizard's dry run for OTHER
   symbols is unaffected.
