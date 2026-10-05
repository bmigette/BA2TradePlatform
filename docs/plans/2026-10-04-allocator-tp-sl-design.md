# Allocator per-symbol TP/SL protection and manual exclusion (TastyTrade) -- design (2026-10-04)

Status: built on branch `feat/alloc-tp-sl` (from origin/dev f6466171), tested ONLY against a fake
of the SDK's complex-order API. LIVE real-money order placement: nothing here has ever been run
against TastyTrade. The first real use is a supervised test (checklist in the final report). This
document supersedes the first draft (commit 1a9256e9): the operator changed the model three times
on 2026-10-04 (section 0).

## 0. Operator decisions (2026-10-04, in the order they arrived; the LAST word wins)

1. OFF by default; set PER SYMBOL from a shield icon ("Set TP/SL") on the allocator symbol row
   (desktop table) and the phone card. No label or account defaults.
2. ONE stop-loss PRICE and one or MORE take-profit targets (price + share of the position).
   Several OCO complex orders, one per target, each `[TP limit sell slice_i @ tp_i] OCO [stop sell
   slice_i @ SL]`; plain price stop, same for income ETFs; GTC.
3. TP fractions may sum to LESS than 100%: the uncovered remainder (the "runner") gets a plain GTC
   STOP-only sell at the same SL, so the whole position is always covered by a stop. Zero targets =
   one stop for everything. Four presets (section 5.6) fill the form from the average cost.
4. **A protection NEVER excludes a symbol from rebalancing** (supersedes "hold after a TP/SL
   fill"): not while active, not after a fill. Every protected symbol a plan touches has ALL its
   protective orders cancelled (broker-confirmed), is traded, and gets its protection re-placed at
   the NEW quantity with the SAME stop and the SAME target prices and fractions.
5. After a TP or SL fill the remaining protection stays; the filled slice is gone; a small note
   ("TP1 filled <date>: share 6% -> 3%") sits on the row; the next rebalance treats the symbol
   normally.
6. **A fill reduces the symbol's stored share of its label** in proportion to the protected
   quantity that left; all exited -> 0 (so the next rebalance does not buy it back). The freed share
   is NOT spread to the other symbols: it stays unallocated (cash) until the operator reassigns it.
   Audited.
7. **ONE exclusion mechanism**, reason = manual "disabled" only (the VST case): no buys, no sells,
   outside the label maths, weights normalised over the enabled symbols, toggle on row and card,
   '+$X excluded' on the label header. Protective orders of an excluded symbol stay as they are.

## 1. Facts established from the code and SDK (tastytrade 12.4.1, read locally)

| Fact | Source | Consequence |
|---|---|---|
| `Account.place_complex_order(session, NewComplexOrder, dry_run=True)` and `place_order(..., dry_run=True)`: `dry_run` DEFAULTS TO TRUE | `account.py:917`, `:877` | every call passes `dry_run` explicitly; a dry run goes first; `test_every_place_order_call_site_passes_dry_run_explicitly` now pins all four call sites |
| `NewComplexOrder(orders=[NewOrder, NewOrder], type=OCO)`; `type` defaults to OCO | `order.py:316` | no trigger order; two plain `NewOrder`s |
| `PlacedComplexOrder` has NO top-level status: `orders: list[PlacedOrder]`, `terminal_at`, `id` | `order.py:395` | complex-order state is DERIVED from the member orders' statuses and fills; a stop-only order is classified as a one-member complex order |
| `OrderStatus` includes `EXPIRED`, `CANCELLED`, `REMOVED`, `PARTIALLY_REMOVED`, `CONTINGENT`, `CANCEL_REQUESTED`, `REJECTED` | `order.py:58` | a lost protection is observable |
| `NewOrder.gtc_date`, `PlacedOrder.gtc_date` exist | `order.py:281,354` | the broker states a GTC end date; recorded on every slice |
| `get_live_complex_orders` returns "complex orders placed TODAY" | `account.py:755` | not usable for last week's GTC order; reconcile reads each placed id (`get_complex_order` / `get_order`) |
| `delete_complex_order` / `delete_order` return None; nothing says the cancel completed | `account.py:792` | cancel is a REQUEST: poll the state until every member is terminal before treating shares as released |
| Fractional quantity is refused on EVERY priced order type | `TastyTradeAccount._refuse_fractional_priced_order` | protect only `floor(position)`; the fractional remainder is unprotected and the UI says so |
| TIF on a MARKET order must be DAY/IOC (47f67180) | `_tt_market_time_in_force` | does not apply to the limit/stop legs (GTC); the allocator's own market sells keep DAY |
| The position read has no resting-order reservation (`qty_available = qty`) | `get_positions` | the platform itself must never sell shares an OCO reserves: cancel-before-trade |
| `refresh_orders` walks `get_order_history`, which also lists the members; they match no `TradingOrder` row | `refresh_orders` | skipped (`continue`): protection orders never pollute the order table. Members are tagged `external_identifier="ba2prot:<protection_id>:<slice>"` |
| `portfolio_allocation_symbol` weights below 100 are ADVISORY, not blocking (`WARNING_SYMBOL_UNDER_FMT`, 2026-09-05) | `ba2_common.core.portfolio_allocation.validate_symbol_weights` | a label summing under 100% already plans correctly and leaves the freed share as cash: no engine change was needed |
| `compute_allocation` multiplies the weights straight through | engine | the freed share is simply undeployed money |

### GTC lifetime (could not be verified locally)

Nothing in the SDK, the repo docs or `docs/` states how long a TastyTrade GTC order lives. The SDK
only proves orders CAN end as `EXPIRED` and that a `gtc_date` exists. My recollection is ~90 days
plus cancellation on some corporate actions: NOT verified. The design never relies on a number:
every placement stores the broker-reported `gtc_date` per slice (else an ASSUMED `placed_at + 90d`,
flagged); an EXPIRED/CANCELLED/REJECTED member is a LOST protection and alerts loudly regardless of
why; a WARNING fires 7 days before the stored date; "Resize protection" renews. Supervised-test
item: compare the stored `gtc_date` with the one on tastytrade.com.

## 2. Scope boundaries

* Allocator-owned protection layer. `TastyTradeAccount.adjust_tp/adjust_sl/adjust_tp_sl/
  modify_order` KEEP refusing (`supports_protective_legs` stays False); the new methods have
  different, explicit names: `place_protective_oco`, `place_protective_stop`,
  `cancel_complex_order`, `cancel_protective_stop`, `get_complex_order_state`,
  `get_protective_order_state`, `list_live_complex_orders`, `equity_tick_sizes`.
* Long equity positions only. Short, option or no-position symbols are refused with a reason.
* **Nothing under `packages/`** (verified by `tools/check_package_versions.py` and by a test that
  greps `packages/` for the module names). Live-only code belongs in-tree, so there is NO
  `PACKAGE_VERSION` bump and NO `ga_neutral_package_paths` entry; the backtest/GA path cannot
  import any of it. (APP_VERSION is the operator's bump at ship time; this branch bumps nothing.)
  Everything the engine needed was achievable in-tree: exclusions are applied where the allocator's
  INPUTS are built (page payload, dry-run inputs) and at the submission boundary, never inside the
  pure engine.
* `init_db()` lives in `ba2_common` and cannot import an in-tree model, so
  `main.initialize_system()` imports `core/allocator_protection_models` BEFORE `init_db()`
  (create_all then builds the tables on any DB), `alembic/env.py` imports it for autogenerate, and an
  idempotent hand-written Alembic revision `a7c3e91d5b24` (chained on head `d9e3b72a10fc`; same
  `has_table` guards as `f1c8a24b7e05`) serves databases managed by `migrate.py upgrade`. A test
  runs alembic's own comparator: zero differences vs the models.

## 3. Data model (four tables, in-tree)

`allocator_protection` -- one row per (account_id, symbol), unique: `enabled`, `sl_price`,
`tp_targets` JSON `[{"price","fraction"}]` (fractions sum to AT MOST 1; `[]` = stop only),
`pending_replace(+since)` (we cancelled and still owe the re-placement; written BEFORE the first
cancel), `last_fill_at/last_fill_note`, `alert_code/alert_message/alerted_at`, `last_error`,
`protected_quantity` (display cache).

`allocator_protection_order` -- one row per broker order: `kind` OCO|STOP, `slice_index`,
`target_index` (-1 for STOP), `quantity` (whole shares), `tp_price` (None for STOP), `sl_price`,
`complex_order_id` (OCO) / `sl_order_id` (STOP's own id), `state` (PLACING / LIVE / CANCELLING /
CANCELLED_BY_US / FILLED_TP / FILLED_SL / LOST_EXPIRED / LOST_CANCELLED / LOST_REJECTED / UNKNOWN),
`filled_qty`, `weight_applied_qty` (filled shares the weight was already reduced for), `fill_price`,
`gtc_date(+assumed, warned)`, `cancel_requested`, `closed_at`. LOST_*/UNKNOWN are the alarms.

`allocator_exclusion` -- one row per (account_id, symbol): `excluded_reason` (plain str; only
`'disabled'`), `since`, `note`.

`allocator_weight_change` -- audit: `label, symbol, reason (tp_fill|sl_fill), before_pct,
after_pct, detail, created_at`.

### Status shown on the page (derived, never stored)

OFF / NO_POSITION ("Armed, no position", not an alarm) / PROTECTED (live slice quantity ==
floor(position)) / PARTIAL ("Size mismatch": covered != held, either way) / UNPROTECTED (nothing
live, or a LOST/UNKNOWN slice, or a stale `pending_replace`) / REPLACING (we are mid-rebalance).
There is no HELD status. A part-filled slice that still rests counts as LIVE for what it reserves.

## 4. Order shapes

* One OCO per take-profit target: SELL_TO_CLOSE limit at the target + SELL_TO_CLOSE plain STOP at
  the stop, TIF GTC, same whole quantity, `NewComplexOrder(type=OCO)`.
* ONE plain STOP-only SELL_TO_CLOSE (GTC) for the runner (`1 - sum(fractions)`), or for the whole
  position when there are no targets. A plain stop (market once triggered), not a stop-limit: a
  stop-limit can be jumped by a gap and never fill (the repo's `_force_close_breached_stops` exists
  to patch exactly that).
* Slicing = largest remainder over `[targets (cheapest first)..., runner]`, summing to the whole
  shares exactly; a target that gets 0 shares is dropped and named in the notes. Prices snap to the
  broker's tick table (TP nearest, SL down; fallback $0.01 / $0.0001, the dry run still rejects an
  off-tick price). Preview example (16 shares): `3 orders: OCO 5 sh TP 12.5 / SL 8, OCO 5 sh TP 15 /
  SL 8, STOP 6 sh @ 8`.
* Placement = refuse bad input -> DRY RUN (any `errors` refuse) -> live `dry_run=False` -> read the
  order back and require it live; an accepted order that cannot be shown live is cancelled before
  the raise. A slice row is written PLACING before its broker call. If a later slice is refused,
  the placed ones are KEPT (partial protection beats none) and the symbol is flagged.

## 5. Lifecycle

All broker IO is in `TastyTradeAccount`; orchestration and DB in
`core/allocator_protection_service.py`, serialised by a per-account RLock (the allocator run takes
its submission lock first, this lock inside it).

### 5.1 Operator actions

`save_protection` (validate against a FRESH price and position, cancel what is live with
confirmation, place), `disable_protection` (cancel confirmed, keep the numbers), `delete_protection`,
`replace_protection` ("Resize protection": cancel confirmed + place at the CURRENT quantity; renews
GTC; the one-click fix for any mismatch). Validation: SL > 0 and strictly below the price; each TP
strictly above it; TP prices distinct after rounding; each fraction in (0,1] and the sum <= 1;
at least one whole share; unknown price/position is an ERROR, never a default.

### 5.2 Reconcile (`reconcile_account`, also run by `TradeManager`'s account refresh and by the
page's Refresh)

Per live slice one broker read (`get_complex_order` / `get_order`), classified by pure rules: any
fill -> FILLED_TP/FILLED_SL (partial counts); Cancel Requested -> CANCELLING; all live -> LIVE;
Expired / Rejected / cancelled-by-someone-else -> LOST_* alarm; anything else (mixed, unmapped) ->
UNKNOWN alarm, never read as live. A failed READ changes nothing and is reported (WARNING alert).

**On a protective fill** (`_on_protective_fill`, the one place): (a) the symbol's stored weight in
every label that stores one is multiplied by `remaining protected qty / protected qty before the
fill` (0 when protection exited the position) and one `allocator_weight_change` row per label is
written; (b) the platform's open `Transaction`s of the symbol are shrunk FIFO by the shares sold (a
transaction sold in full is closed at the fill price) so the next rebalance does not size against
shares that are gone; (c) a note is stored on the protection ("TP1 filled 2026-10-03: share 6% ->
3%" / "SL hit ..."); (d) an activity-log WARNING. NOTHING excludes the symbol. A fill that lands in
several reconciles reduces the weight once per share (`weight_applied_qty`). Each step's failure is
loud (`WEIGHT_FAILED`, `TXN_FAILED` activity-log FAILURE) and never masks the fill. A symbol
with no STORED weight row (it runs on the derived default, its actual share) has nothing to reduce:
its target follows its holdings by itself.

**Quantity changes outside the allocator** (decided: split by direction, justification below):
* GROWTH (whole shares nobody covers: a manual buy, a DRIP, shares bought back after the stop
  fired): the reconcile ADDS protection for exactly the uncovered shares from the same template. No
  cancel, so there is NO gap in which existing shares are unprotected. Not while an allocator run is
  in flight (the submission lock is held) or while an order on the symbol is still working (the
  position is not final), and never when an alarm slice exists (the operator may have cancelled on
  purpose: it alerts instead).
* SHRINK / any other mismatch (live orders cover MORE than is held): flagged `QUANTITY_MISMATCH`,
  status "Size mismatch", loud, with the one-click **Resize protection**. Resizing means cancelling
  live orders, i.e. a window in which the whole position is unprotected; a background job should
  not open that window by itself, and the operator may be mid-manual-trade on the TT site.
Justification: auto-adding is strictly additive and protective; auto-resizing is destructive
before it is protective. The asymmetry is the whole decision; section 12 asks the operator to
confirm it.

### 5.3 The allocator run (`run_allocation`) -- cancel, confirm, trade, re-place

Inside `_run_allocation_locked`:
1. FIRST, before any gate: rows of EXCLUDED symbols are stripped (section 6) and re-emitted as
   SKIPPED outcomes.
2. After the gates pass and before the run row is recorded (a blocked attempt changes nothing):
   `prepare_for_trade` for every symbol the plan will TRADE (side and delta): reconcile first (a fill
   discovered now is applied: weight reduced, transactions shrunk), set `pending_replace` BEFORE the
   first cancel, then cancel ALL its protective orders (OCOs and stop-only) and require the broker to
   CONFIRM each. A symbol whose cancel cannot be confirmed is DROPPED from the plan and reported FAILED
   ("the shares may still be reserved"); the allocator never sells shares an OCO still reserves.
3. The remaining plan is recorded and submitted as today (sells first).
4. After `measure_run_fills`, `resume_protection`: for each pending symbol, if one of its orders is
   still working it stays REPLACING; otherwise read the fresh position and place protection at the
   NEW quantity from the stored template (same SL, same target prices and fractions; slices and the
   stop-only remainder recomputed). Whole position sold -> nothing to place, protection is inactive
   ("Armed, no position", config kept, not an alarm) and comes back automatically when the symbol is
   bought again (next run, or the growth rule). A re-placement the broker or validation refuses
   (including a price that moved through the stop while the orders were off: refused, never traded
   through) sets `REPLACE_FAILED`, clears `pending_replace` (no endless retry) and leaves the symbol
   UNPROTECTED for the operator: red status, red page banner, activity-log FAILURE.
5. The same re-placement runs in the failure path (a raise mid-submission), and never raises
   itself. The background reconcile completes a `pending_replace` only when NO run is in flight.
6. The run's activity-log description reports `TP/SL: re-placed X of Y, blocked Z`.

### 5.4 Crash safety

Each slice row is inserted PLACING before the broker call; a PLACING row older than 60s is an UNKNOWN
alarm (an order may exist: the `external_identifier` tag lets the operator find it). `pending_replace`
older than 15 minutes shows UNPROTECTED.

### 5.5 Failure policy (never silent)

Every failure path does all three: `logger.error`, an `ActivityLog` entry (`TP_SL_ADJUSTED`,
`data.kind="allocator_protection"`, `data.code`), and a persisted `alert_code/alert_message` rendered
as a red page banner and a red chip on the row. The same code is not re-logged on every refresh. No
notification channel exists in the platform (no email/telegram): banner + activity log. Codes:
PLACEMENT_REFUSED, CANCEL_UNCONFIRMED, LOST_EXPIRED, LOST_CANCELLED, LOST_REJECTED, UNKNOWN_STATE,
REPLACE_FAILED, QUANTITY_MISMATCH, RECONCILE_FETCH_FAILED, GTC_EXPIRING; events: FILL, PLACED,
EXTENDED, WEIGHT_FAILED, TXN_FAILED.

### 5.6 Presets (pure, `apply_preset`; unit-tested; every number stays editable)

Computed from the position's AVERAGE COST, snapped to the tick grid (TP nearest, SL down); an unknown
average cost RAISES (a preset from a guessed cost would be a fabricated price) and the buttons are
disabled.

| Preset | Stop | Take-profit |
|---|---|---|
| Double-up: take half at 2x | -25% | 50% at 2.0x; the other 50% a stop-only runner |
| Ladder +25/+50/+100% | -15% | thirds at +25%, +50%, +100% (boxes read 33.33/33.33/33.34) |
| 2R scale-out | -10% | 50% at +20%; 50% stop-only runner |
| Income: stop only | -20% | none |

A preset can fail validation against the CURRENT price (a deeply under-water position has its -25%
stop above the market): that appears as the usual validation message and nothing is saved.

## 6. The ONE exclusion mechanism

**Storage decision.** A new in-tree table `allocator_exclusion`, NOT a column on
`portfolio_allocation_symbol`. That row is per LABEL, created lazily and deleted with the membership,
while "I do not want the allocator touching VST" is a standing decision about a HOLDING for the
account (a symbol in two labels is excluded from both). Also, `ba2_common.core.models` is imported by
the backtest engine: a column there is GA-relevant (package bump + raised minimum); an in-tree table
is GA-neutral. `excluded_reason` is a plain str with the single value `disabled` (manual); a TP/SL
fill no longer needs a second reason (section 0.4).

**Semantics of an excluded symbol** (the symbol is simply outside the managed money, like an
unmanaged holding):
1. The plan never emits an order for it. The inputs are built without it (page payload and
   `_load_flow_inputs`), and `run_allocation` strips its rows FIRST as the boundary guarantee (stale
   dialog, wizard re-solve over cached labels, retries); the dry run shows any such row SKIPPED
   ("excluded from allocation (disabled by you on ..., note): no buys, no sells ...").
2. The label maths ignore it. Its value is not in the label's value, the distinct managed total, the
   label P&L or its cost basis, and not in the investable base (`compute_base_notional` is called
   over the enabled symbols). Its share is ignored: the enabled symbols' shares are scaled by
   `T / N` (T = the label's total of ALL stored shares, N = the enabled ones') so the excluded
   share is spread over the rest. The scale target is the stored total T, NOT 100, so a shortfall that
   is not an excluded share (a share freed by a fill) stays unallocated: stored 40/40/VST 10 (T=90)
   solves 45/45, not 50/50. The label's target $ applies to the enabled symbols only. The table shows
   the stored share in the box and "eff. 50.00%" under it.
3. Interaction with the rest of the allocator: **label target %** -- unchanged: it is a share of the
   investable pool, which is now `(buying power + enabled managed value) x (1 - reserve)`; the label
   targets stay relative weights totalling 100. **Unallocated reserve** -- applies to the base
   WITHOUT the excluded value (an excluded holding does not shrink or grow the cash reserve).
   **Simulate base** -- replaces the measured base as before; the free-buying-power term absorbs the
   change using the enabled-only managed value, so the identity `base = managed + buying power` still
   holds on the page. An excluded position is not part of any of these numbers.
4. UI: an eye toggle on the row (desktop cell + phone card header; every broker); excluded rows grey
   with a badge "Excluded: manual" and still show quantity, value and P&L; the label header shows
   "+$X excluded". Including again is explicit (a confirm dialog). The dialog states that protective
   orders stay as they are.
5. Inline-edit helpers (Fill 100%, Even split, Wipe, Load last) keep operating on the STORED shares
   of every label symbol, excluded ones included: the stored set is the operator's own numbers and
   re-including must restore them. `labels_for_persist` stops the dry run's "Continue" (which stamps
   `previous_weight_pct`) from writing the SCALED shares back: for a label with an excluded symbol it
   writes the stored shares only.

## 7. Weight reduction on fills (rule 0.6)

`new = old x remaining protected qty / protected qty before the fill`; all exited -> 0. Applied to
every label that stores a weight for the symbol; the freed share is not given to anyone. Because
under-100 symbol weights are already advisory in the engine (`WARNING_SYMBOL_UNDER_FMT`), a label
summing below 100% plans correctly (the freed money stays cash) and blocks nothing; the label header
shows "freed X% from TP/SL fills" -- only for a label that has an audit record, so a hand-typed 60 is
not reported as freed. The row note carries "share 6% -> 3%". Interaction with the previous-weight
mechanism: `previous_weight_pct` is "what the last RUN went out with" and only
`save_allocation_targets` writes it; the automatic change never touches it, so "Last %" keeps
showing the weight of the last run next to the reduced current one, and "Load last" restores the
pre-fill share only on an explicit operator action. The audit trail is `allocator_weight_change`.

## 8. UI

Desktop: two columns (`protect`, `exclude`); phone: both in the card header (TIER_HEAD). The dialog:
presets row, SL box, TP rows (price, % of position; delete; add -- adding after a deliberate runner
gives the new row the remaining share, otherwise an even split), validation messages, the order
preview, the orders at the broker with state and GTC date, the fill note, the alert, and the
sentences "the allocator rebalances a protected symbol normally ... excluding does not cancel them".
Save / Resize protection / Switch off. Styles ride `page_phone_css()` and the generated static file
(installed before the page's first await; the dialog and the helper modules add no CSS).

## 9. Tests (fake SDK, no network)

`tests/allocator_protection_fakes.py` implements `place_complex_order`, `get_complex_order`,
`delete_complex_order`, `get_live_complex_orders`, `place_order`, `get_order`, `delete_order`,
`get_positions` with REAL SDK models, a cancel that takes N reads, a cancel that never confirms, a
fill-instead-of-cancel race, expiry, an outside cancel, a read failure. Suites: pure helpers (122),
TastyTradeAccount methods (42), service lifecycle (87), run boundary (15), exclusion (49), UI (64),
migration (15). Also fixed: `tests/conftest.py` now forgets `nicegui.Client`s a test built (a
pre-existing order-dependent failure of `test_batch_import_upload`/`test_settings_dialog_*`, exposed
the moment a client-building test file sorted before them).

## 10. What is NOT verified without a broker

Real TT behaviour of: OCO acceptance for sell-to-close equity with a GTC plain stop; the statuses of
the surviving OCO leg when its partner fills and what a TT-side expiry looks like; `gtc_date`
population; cancel latency; whether `get_order` works for a GTC order from another day;
`external_identifier` format acceptance; two or more OCOs plus a stop-only order on one position
whose quantities sum to the position (should be accepted); corporate actions; behaviour of a plain
stop in the pre-market. The fake encodes my best reading of the SDK (`BROKER_ASSUMPTIONS` in the
fakes module); the supervised test exists to correct it.

## 11. Known limitation

A `Transaction` shrunk after a partial protective sale loses the realised P&L of the sold part from
its own row (the same simplification `adjust_quantity_with_tpsl` makes for a partial close); a
transaction sold in full is closed at the fill price with its real P&L.

## 12. Revision 2026-10-05 (Opus review of b8005547 + the operator's answers)

This section SUPERSEDES anything above that contradicts it (sections 5.2, 5.3, 5.5, the GTC paragraph
of section 1, the status table of section 3 and the "armed, no position" wording).

### 12.1 Operator answers
* **Quantity changes outside the allocator: BOTH directions are automatic.** Growth adds orders
  (add-only, no cancel). A shrink cancels (broker-confirmed) and re-places at the held quantity in the
  background, rate-limited (one automatic cancel/re-place per symbol per 10 minutes), one
  activity-log entry each time, never during an allocator run, never with a trade in flight, never
  with an UNKNOWN slice or a LOST alarm, and never within 5 minutes of a protective fill (the broker's
  position read may lag it, A7). After 3 consecutive FAILED automatic attempts the automatic paths
  stop for that symbol and say so (`AUTO_STOPPED`); an operator Save / Resize re-arms them. A failed
  resize leaves LOST slices, which already block further automatic action: no endless retry.
* **Stop type: stop-MARKET** (as built).
* **GTC expiry: auto-renew.** A resting slice whose BROKER-REPORTED `gtc_date` is within 7 days is
  renewed (confirmed cancel + re-place with the same prices) by `renew_expiring`, called from the
  account refresh in `TradeManager` right after the reconcile (never on page load). One symbol per
  refresh cycle, the soonest-expiring first, so many orders sharing a date spread over cycles and each
  still renews before its date (a date already past renews first). Same safety: never during a run,
  never with `pending_replace`, an UNKNOWN slice or a LOST alarm, never with a trade in flight,
  per-symbol rate limit, one activity-log entry per renewal, a loud alert and the failure counter on
  failure. **A slice with no `gtc_date` is never renewed** (stored as NULL; the old "assumed 90 days"
  and the expiry warning are gone). The supervised test reports what TT returns for `gtc_date`.
* **After an exit the protection is DISARMED.** When the whole-share position is gone (sold by a
  rebalance, or exited by protection fills and then confirmed flat after the 5-minute settle window)
  the settings are CLEARED (`sl_price` 0, no targets, `enabled` False) and a history record is kept
  (`disarmed_at`, `disarmed_note` = "SL 45, TP 60@33%..."; also in the activity log). A later buy is
  NOT auto-protected; the operator sets TP/SL again on re-entry. A flat read while orders still rest
  is flagged as a mismatch instead (a wrong position read must not disarm a live protection).

### 12.2 Review findings and the fixes (tests in `tests/test_allocator_protection_review.py`)
* **F1 (UNKNOWN placements).** Only a broker ANSWER (a `TastytradeError`) is a refusal. Any other
  exception from the LIVE call, or an accepted order that cannot be read back, raises
  `PlacementOutcomeUnknown` (tag, kind, and the broker id when one was named). The slice becomes
  UNKNOWN with the tag and every known id and placement STOPS. UNKNOWN BLOCKS (`_blocks`): prepare,
  new placements, resize and renewal refuse while one is unresolved; it is never superseded. It is
  resolved by `find_protective_orders_by_tag` (today's live orders and complex orders plus the newest
  history pages): found -> the id is adopted and the order handled like any other (cancelled with the
  rest on a resize); not found with a successful search -> "never reached the broker", closed; a
  search that fails resolves nothing.
* **F2.** `prepare_for_trade` reports any fill it observes (its own reconcile, or one that lands
  during the cancel) in `PrepareResult.filled`; the run drops that row (SKIPPED: "re-run the dry
  run"), applies the fill and re-places protection on what is left.
* **F3.** Prepare blocks whenever ANY slice is resting/UNKNOWN, enabled or not.
* **F4.** `replace_protection` and Save refuse while `pending_replace` or a run is in flight.
* **F5.** The weight factor is `(held - sold) / held` from the broker position (`held = position now +
  sold`; fallback to covered when the read fails), computed once per reconcile in slice order.
* **F6.** A protective fill is recorded as a synthetic FILLED SELL `TradingOrder` linked to the
  transaction (FIFO; `data.source = "allocator_protection"`; comment without the word "closing"), so
  `refresh_transactions` derives the same quantity; a full exit closes through
  `close_transaction_with_logging`. A test runs the real refresh twice.
* **F7.** The single choke point is `TastyTradeAccount._submit_order_impl`: before any SELL it calls
  `before_sale` (cancel protection, confirmed; `pending_replace` owed; refuse the sale if the cancel
  cannot be confirmed). Expert exits, Live Trades manual close, Smart RM and the breached-stop force
  close all reach it through `submit_order`; the allocator's own sells find nothing resting and pass.
* **F8.** Prepare cancels only for symbols the plan SELLS; bought symbols get add-only protection
  after the run (`extend_after_buys`); cancels share ONE confirmation poll (`cancel_protective_batch`);
  the page payload is DISPLAY ONLY, with an explicit "Check TP/SL orders now" action.
* **F9.** `_working_order_symbols` counts only non-terminal orders created after the protection was
  cancelled (5 minutes of slack for the triggering sale) or, with nothing pending, within 2 hours; a
  `pending_replace` older than 15 minutes writes `REPLACE_STALE` (alert + activity-log FAILURE).
* **F10.** A warning (`GTC_EXPIRING`, `RECONCILE_FETCH_FAILED`) never overwrites a failure alert; the
  automatic paths stop after 3 failures (above).
* **F11.** The dialog's order table shows the OCO id, the stop-order id and the tag.
* Also: the migration docstring says four tables; the exclude dialog warns when excluding the symbol
  would leave a funded label with no enabled symbol (which blocks the whole rebalance).

### 12.3 Added to the assumptions list (`BROKER_ASSUMPTIONS`) and the supervised checklist
A1 TT refuses a SELL_TO_CLOSE larger than the shares held, including when a resting stop triggers on a
position that has since become smaller. A2 resting closing orders reserve shares. A3 an OCO counts its
quantity once (q, not 2q); otherwise an OCO above half the position fails its dry run. A4 the partner
leg after a partial fill stays live for the remainder. A5 a timeout after an accepted placement leaves
the order resting. A6 `get_order` works for a GTC order from an earlier day. A7 the position read can
lag a fill. A8 `external_identifier` accepts `ba2prot:<id>:<n>`. A9 status names of GTC stops outside
regular hours.

### 12.4 Open questions (remaining)
1. Weight of a symbol with no STORED weight row is never reduced by a fill (it follows holdings).
2. Re-including an excluded symbol after fills reduced others can push a label over 100%: warn?
3. The row shows only the latest fill note; the history is the audit table and the activity log.

## 13. Round 2 fixes (2026-10-05, branch fix/alloc-tp-sl-round2)

- N8: every automatic re-placement plans and validates BEFORE it cancels (`_preflight_errors`); a refused plan
  keeps the existing orders and alerts. Targets at or below the price (the one that just filled) are dropped
  (`drop_reached_targets`) and their fraction folds into the stop-only runner, so the stop always remains.
- N1: tags are `ba2prot:{id}:{index}:{nonce}`; an order found by tag is adopted only when symbol, quantity and
  received time (>= placed_at) match, otherwise UNKNOWN_STATE alert and no adoption.
- N2: UNKNOWN stays blocking until `UNKNOWN_MIN_AGE_SECONDS` (300) old, then history is searched again.
- N5: `_disarm` refuses (QUANTITY_MISMATCH, alert kept) while any slice rests.
- N6: `before_sale` looks the row up before taking the account lock.
- N7: `before_sale` stores `expected_qty` (new column, Alembic b8d1f4a29c63); `resume_protection` waits for the
  broker read to agree, up to FILL_SETTLE_SECONDS.
- Q1: a protective fill on a symbol with no stored weight row writes an explicit row (measured share x factor).
- Q2: the include dialog warns (never blocks) when a label's stored shares would exceed 100%.
- Q3: the latest fill note is enough (no change).

### 13.1 Round 3 (verification of 06530d43)

- Adoption by tag (item 2): the tag carries a nonce, so a found order is adopted on tag + symbol. A size or
  received-time disagreement only logs a WARNING (broker clock skew must never freeze a symbol). A tagged order
  that is terminal with no fill closes the slice. Broker times are converted to UTC (`_naive`). The history is
  searched newest-first page by page back to the slice's placement time (an incomplete search raises). New
  operator action "Forget unresolved order" (`forget_unknown_slices`, confirmation + activity log).
- `before_sale` (item 3) skips the account lock only for no row, or a protection that is off with nothing resting.
- Weights (item 4): the shares of unstored label members are measured BEFORE the fill (cost valuation rescales the
  already reduced basis), written as explicit rows on the first fill, and every other unstored member is PINNED at
  its pre-fill share (audit reason `pinned`), so the label total drops by exactly the freed share. A share that
  cannot be measured raises WEIGHT_FAILED (naming label and unpriced symbols) and the fill log no longer claims
  the share stays unallocated.
- Filled targets (item 5, round 4): the stored target gets `"filled": true` when its slice filled in full, or
  `"taken": ratio` after a PARTIAL fill (the rest of the target keeps its price). The marks are honoured
  EVERYWHERE (re-placement, growth, repair): re-placement drops filled targets, spreads the others over the
  remaining shares (`f * (1 - taken) / (1 - sum(f * taken))`), then folds only reached-but-unfilled targets into the
  stop. New shares added later get what is left of the plan; the operator re-saves the protection to re-arm a
  filled target. Slices keep the ORIGINAL target index. The dialog hides filled targets.
- Round 4: a site-cancelled UNKNOWN order is a LOST_CANCELLED alarm (never re-placed); an adopted order of a
  different size resizes the slice and raises QUANTITY_MISMATCH; the forget action needs age >= 300 s and a tag
  search that itself FAILS (inside the lock); plain history is read with `sort='Desc', start_at=since`, the
  complex-order history ordering is detected (ascending or indeterminate: read on to the 6-page cap, then raise);
  pinned members are listed in the fill note and log. Supervised checklist addition: history ordering (A10).
- N7 (item 6): `expected_qty` is cleared wherever `pending_replace` is set or cleared; the wait only applies when
  a platform SELL of the symbol is FILLED in the DB; it logs SALE_SETTLING once and SALE_UNSETTLED if it ends
  still mismatched.

### 13.2 Round 5

- Per-slice `taken` (M1): each slice stores, keyed by its tag inside the target's JSON (`bases`), what the target had
  already given up when it was placed; a fill updates `taken = base + (1 - base) * filled/qty`. A target is marked
  `filled` only when its slice fills in full AND no other slice of the same target still rests (a growth lot's fill is
  the lot's, not the target's).
- Tag search (M2): only the plain order endpoints are read (`sort='Desc'`, `start-at` as ISO 8601). OCO legs are found
  there by their `complex_order_id` and the complex order is read by id; the complex history is never read. An
  ascending page raises, so the caller concludes nothing.
- Add-only placement (S1): growth and repair shares fill each target's DEFICIT against the plan for the whole
  position (`plan_add_only`), so per-target totals stay near the template; surplus shares go to the stop-only runner.
- `extend_after_buys` respects an open alarm like the refresh does (S2).
- The dialog lists taken targets greyed with a Re-arm checkbox; saving keeps them marked unless re-armed, and a
  partly taken target stays partly taken while its price and share are unchanged (S3).
- Operator-facing supervised checklist: `2026-10-04-allocator-tp-sl-supervised-test.md`; read-only probe:
  `tools/tt_protection_probe.py`.

### 13.3 UI polish and the stop's buying-power effect (live findings, 2026-10-05)

Verified on prod by dry runs: TastyTrade margin-checks a STOP as if it filled AT ITS TRIGGER. The buying-power change
is about `q x (stop - p0)` (p0 = price minus the margin released; ~$215 for a $287 stock), so stops within ~25% of the
price cost nothing, deeper stops RESERVE buying power, resting stops keep it reserved, and when it is not available the
order is refused with `margin_check_failed`. `BROKER_ASSUMPTIONS`: A3 CONFIRMED (an OCO counts its quantity once),
new A11 (stop buying-power effect at the trigger price), and `gtc_date` is NULL on live GTC orders (no expiry, so no
renewal).

- Dialog: a **Check with broker** button (dry runs only, on click, never on load) lists per order the broker's verdict
  and buying-power change, the account's available buying power, and, when the stops do not fit, the sentence
  "This stop is far below the market: ... (needs ~$X, available $Y). Raise the stop to ~$Z (approx.) or free buying
  power." Z is bisected on dry runs (at most 7 rounds). **Use ~$Z** only fills the stop box; nothing is saved.
- A refused placement says the same sentence in the alert (the broker's raw text stays in the details) and, when only
  some orders were placed, "N of M protective order(s) placed; K share(s) have no stop". The placed ones are kept.
  **Use a stop the broker accepts** (explicit, confirmed) re-plans with the suggested stop
  (`change_stop_and_replace`); the operator's stop is never changed silently.
- Symbol rows: the TP/SL and exclusion controls are two icons (shield, eye) in the Symbol cell's icon group beside the
  (i), no column and no text. Shield: grey no TP/SL, green protected, amber partly protected / size mismatch /
  re-placing, red unprotected or a failure alert; the tooltip carries the status, "N of M shares protected", the last
  fill note and the alert. Eye: orange when excluded, grey when included; the row stays greyed.
- Label header: one segmented badge, total (grey) | profitable (green) | losing (red) | excluded (orange); zero
  segments are omitted except the total; the tooltip lists each meaning and the excluded value.
