# Wash-trade locking — decision record

**Status:** living reference. Read this before changing anything in the
wash-trade path.

**Supersedes:** `docs/plans/2026-06-03-washtrade-locked-order-design.md` (still
accurate for the `WASHTRADE_LOCKED` status semantics and the UI treatment; its
lifecycle and leg-exemption sections are obsolete).

**Code:**
- `packages/common/ba2_common/core/interfaces/AccountInterface.py` —
  `_is_washtrade_lock_candidate`, `_find_opposing_working_order`,
  `_PRIMARY_ORDER_TYPES`, `_WASHTRADE_BLOCKING_ORDER_TYPES`, the gate in
  `submit_order`.
- `ba2_trade_platform/core/TradeManager.py` —
  `_check_all_washtrade_locked_orders` (the retry),
  `classify_waiting_entry` / `_check_stranded_waiting_transactions` /
  `_fail_terminal_unfilled_entry` (the give-up path, 2026-09-22).
- `packages/common/ba2_common/core/washtrade.py` — the
  `washtrade_complex_submit` / `washtrade_rejected` markers on
  `TradingOrder.data`.
- `ba2_trade_platform/modules/accounts/AlpacaAccount.py` — the OCO protective
  pair, and `_reconcile_pending_cancel_orders` (the by-id `PENDING_CANCEL`
  resolution, 2026-09-22).
- `tests/test_washtrade_lock.py`.

This area has been redesigned three times in two months, each time on evidence
that was not written down, which is why the same ground got re-litigated. If you
change it a fourth time, **add a dated section below** with the measurement that
justified it.

---

## What the broker actually does

Measured 2026-08-05 against Alpaca paper account 1 (`BA2-Test1`), live market,
using AAPL (9 sh long, one working protective `SELL STOP`) and FOX (28 sh long,
no working orders, with a `BUY STOP` staged far above market as a stable
blocker). Every row is a real submitted order, not documentation.

| New order | Opposing order working | Result |
|---|---|---|
| `BUY` MARKET | `SELL` STOP | **REJECTED** `40310000` |
| `BUY` LIMIT (marketable) | `SELL` STOP | **REJECTED** `40310000` |
| `BUY` STOP | `SELL` STOP | **REJECTED** `40310000` |
| `BUY` MARKET, `order_class=BRACKET` | `SELL` STOP | **ACCEPTED → FILLED** |
| `SELL` STOP | `BUY` STOP | **REJECTED** `40310000` |
| `SELL` OCO (TP limit + SL stop) | `BUY` STOP | **ACCEPTED** |
| `SELL` LIMIT added beside an existing `SELL` STOP | — | **ACCEPTED** |

The rejection is always:

```
code 40310000  "potential wash trade detected. use complex orders"
reject_reason: "opposite side market/stop order exists"
existing_order_id: <the blocker>
```

Four facts follow, and all four have been guessed wrong at some point:

1. **The new order's type is irrelevant.** MARKET, LIMIT and STOP were all
   rejected against the same blocker. Only `order_class` matters. Any fix that
   works by choosing a different order *type* is dead on arrival.
2. **The exemption is real and covers both `BRACKET` and `OCO`.** This is the
   escape hatch Alpaca's own error message points at.
3. **It is symmetric.** Entries block protective legs and protective legs block
   entries. Both directions have caused an incident.
4. **A complex order needs at least one leg.** An entry with neither TP nor SL
   cannot be expressed as `BRACKET`/`OTO` and therefore cannot escape. This is
   the one case that still needs a lock or an expiry.

Note that only *market/stop* orders block. A working `SELL LIMIT` (a lone TP
leg) does not block anything — hence `_WASHTRADE_BLOCKING_ORDER_TYPES` =
`{MARKET, BUY_STOP, SELL_STOP}`.

---

## How the approach changed, and why

### 2026-06-03 — introduce the lock, exempt protective legs

Multiple experts share one account and the broker nets them into one position,
so opposing orders on one symbol are routine. Rejected orders were landing in
`ERROR` (terminal) and being lost — 13 found stuck on account 4.

Introduced `OrderStatus.WASHTRADE_LOCKED`: hold the order in the DB, retry each
refresh, submit when the symbol clears. TP/SL legs were **exempt** from locking
on the reasoning that they are "inherently opposite-side and brokers accept them
as complex orders".

**What that got wrong:** the exemption conflated two different things. A
protective leg is accepted *against its own parent* (a genuine bracket pair). It
is not accepted against an unrelated order on the same symbol. The original doc
also already recorded, at line 24, that complex orders are exempt — the fix we
eventually needed was written down on day one and not acted on.

### 2026-08-03 — remove the leg exemption

Measured: 8 protective `SELL_STOP` legs rejected `40310000` by a *different*
transaction's BUY. Because the exemption skipped the lock, they went straight to
the broker and were marked `ERROR` — terminal, never retried. Order 587 died
that way and left transaction 273 (UBER, 21 shares) with **no stop at the broker
at all**. Seven of the eight survived on timing luck alone.

So protective legs became lock candidates too. The justification recorded in the
docstring was: *"a working order blocks only until it FILLS (the blocking BUY was
a MARKET order, filled seconds later), not until its position closes — so the
retry clears it promptly rather than parking a naked position indefinitely."*

**What that got wrong:** that premise is true only when the blocker is a MARKET
order, which is what we happened to measure. `SELL_STOP` is also in
`_WASHTRADE_BLOCKING_ORDER_TYPES`, and a protective stop stays `NEW` at the
broker for the **entire life of the position** — potentially months. The
"blocks only until it fills" reasoning silently fails for two thirds of the
blocking set.

### 2026-08-05 — the deadlock surfaces

13 entry orders stuck in `WASHTRADE_LOCKED`, oldest 9 days. **All 13** blocked by
a protective `SELL_STOP` guarding an open position belonging to a different
transaction. Not one blocker was transient. Symbols recur (ORCL, MTZ, FICO,
AXTI, RBLX each twice) because the expert re-signals and each new entry locks
behind the same immortal stop.

The retry in `_check_all_washtrade_locked_orders` was working exactly as
designed. It simply had nothing to wait for.

The 06-03 doc had set the revisit trigger explicitly (line 96: *"Revisit if
locks are observed surviving across sessions"*). It had been met for nine days
with no alarm, because a still-blocked order only emits a `debug` log.

### 2026-09-22 — the complex-order exemption does NOT always hold

Measured against Alpaca **paper account 1** (dev DB, expert 2), read-only
`client.get_order_by_id` against the owning account's credentials.

Four transactions were found stuck in `WAITING` forever — txn 28 (SHOP) and 29
(UNH) since 2026-09-14, txn 131 (NVDA) and 133 (AVGO) since 2026-09-21. Every
one has the same shape:

```
entry MARKET          CANCELED         filled_qty 0, has a broker_order_id
OCO                   CANCELED
protective SELL_STOP  PENDING_CANCEL   <-- never resolves
```

Timings, from the broker, for the 2026-09-21 pair:

| Fact | Measurement |
|---|---|
| Entry orders submitted | 13:42:56.489 and 13:42:58.251 |
| Cancelled by the broker, filled 0 | **92 ms** and **142 ms** later |
| Blocker ord 263 (NVDA `SELL_STOP`, another expert) submitted | 13:41:57, still `NEW` |
| Blocker ord 253 (AVGO `SELL_STOP`, another expert) submitted | 13:41:52, still `NEW` |
| `PENDING_CANCEL` legs at the broker (e.g. ord 338 NVDA) | `CANCELED`, `canceled_at` 2026-09-21T13:42:56Z, filled 0 |
| Orders ever locked in this database (`status like '%WASHTRADE%'`) | **none, ever** |

Both entries carried a `tp` and an `sl`, so both took the
`use_complex_order = True` branch added on 2026-08-05 and went out as complex
orders. **Alpaca cancelled them anyway, within ~100 ms, unfilled.**

**Premise invalidated.** The 2026-08-05 design rests on *"brokers exempt COMPLEX
orders from the wash-trade check"*, measured with a **`BUY STOP` staged far above
market** as the blocker. It does **not** hold when the blocker is **another
expert's resting protective `SELL_STOP` guarding an open position**. Probe row 4
of the table above is still true as measured; it is simply not general.

**What was NOT changed, and why.** Making a resting protective stop a hard
blocker again was rejected: locking waits for a blocker that cannot clear while
the position is open, which is the exact deadlock of 2026-08-05 that the 24-hour
expiry exists to escape. Re-litigating that would be the fourth turn of the same
circle.

**What changed instead** — three defects, three fixes, none of them a new
blocking rule:

1. **A cancelled entry now fails its transaction.**
   `TradeManager._check_stranded_waiting_transactions` +
   `_fail_terminal_unfilled_entry`. Until now `_fail_unsent_entry` was the ONLY
   path to `TransactionStatus.FAILED` and it covers only *"the entry never
   reached the broker"* — it explicitly refuses any order carrying a
   `broker_order_id`. These entries DID reach the broker, so nothing failed
   them, and `enter_market`'s safety check then retired each symbol for expert 2
   permanently. A **partial** fill is never failed: it is a real position.
2. **`PENDING_CANCEL` is resolved by direct lookup.**
   `AlpacaAccount._reconcile_pending_cancel_orders`. The old promotion ran only
   over the paginated `GetOrdersRequest` listing, and these legs are not in it —
   OCO legs are never listed separately and their parent was itself cancelled.
   `get_order_by_id` answers instantly. The promotion still goes through
   `OrderStatus.resolve_pending_cancel`, so nothing is optimistically marked
   `CANCELED`.
3. **The exemption is no longer trusted silently.** A contended complex
   submission is stamped on the order (`TradingOrder.data`,
   `washtrade_complex_submit`); when such an order ends terminal and unfilled,
   the give-up path stamps `washtrade_rejected`, logs
   `WASH-TRADE REJECTION CONFIRMED` with the blocker, and fails the transaction.
   The attempt is still made — it works against most blockers — but its failure
   is now a recorded, diagnosable event rather than an anonymous zero-fill
   cancel.

**Revisit trigger, and what alarms on it.** This design has failed if
`WASH-TRADE REJECTION CONFIRMED` starts appearing routinely for one
expert/symbol pair — i.e. the exemption is failing often enough that retrying it
is just a slower lock. That line is at `ERROR` (not `debug`, the 06-03 and
08-05 mistake) and the stamp is queryable:
`select id, symbol, data from tradingorder where data like '%washtrade_rejected%'`.
A second trigger: any order sitting in `PENDING_CANCEL` past
`_PENDING_CANCEL_WARN_AGE_HOURS`, which now logs at `WARNING` every refresh.

**Not reproduced as a probe.** The four rows above are real live orders with
real broker timestamps, which is the stronger evidence; a deliberate probe would
need another expert's stop resting on the same symbol at the same moment. If you
change this area again, submit a contended `BRACKET` against a resting
protective `SELL_STOP` on a paper account and record what comes back.

---

## Current design

**Invariant: never let a wash-trade rejection reach a terminal state, and never
park an entry behind a blocker that cannot clear.**

1. **Contended entries go out as complex orders.** `_find_opposing_working_order`
   already identifies a blocker before submission, for free. On that branch
   only, submit as `BRACKET` (TP and SL known) or `OTO` (one known) instead of
   locking. Uncontended entries keep the existing simple-order path unchanged —
   this deliberately keeps the blast radius on live trading code to one branch.
2. **Protective pairs stay `OCO`.** Already the case
   (`AlpacaAccount.py:1057`), and the probe confirms `OCO` is exempt, so the
   pair can always be placed or replaced even against an opposing entry.
3. **The lock remains, as a fallback only** — for orders that cannot form a
   complex order (no TP and no SL) and for SL-only protective legs, which need
   two legs to be an OCO and so stay simple and blockable. For that case the
   original "blocker fills in seconds" reasoning does hold: the blocker there is
   an entry MARKET order.
4. **Locks expire.** A lock that survives past one trading session is a
   deadlock, not a wait. Cancel the order, close its transaction as rejected,
   and log the blocker. A market entry signal two days stale is not one you want
   filling.
5. **A contended complex order that the broker kills anyway is recognised, not
   retried** (2026-09-22). The exemption is attempted, its use is stamped on the
   order, and a terminal zero-fill outcome is recorded as the wash-trade
   rejection it is and handed to the give-up path. No wait, anywhere in this
   area, is unbounded: the lock expires at
   `_WASHTRADE_LOCK_MAX_AGE_HOURS`, a `PENDING_CANCEL` at
   `_PENDING_CANCEL_MAX_AGE_HOURS`, and a `WAITING` transaction whose entry is
   terminal and unfilled is failed on the next refresh.

### Changing TP/SL after entry

A native bracket/OTO's child legs become **ordinary standalone orders** at the
broker once the parent fills. They are not welded to the entry and can be
cancelled and replaced individually, exactly as today.

To go from SL-only to TP+SL: **cancel the standalone SL, then submit an
`OCO(TP, SL)`.** Do *not* simply add a TP limit beside the existing stop — the
broker accepts it (probe row 7) but the two legs are then independent, so a TP
fill leaves the stop working against shares that are no longer held. That is the
whole reason the platform uses OCO. The replacement is always possible because
OCO is exempt from the block.

---

## Open items

- **Implementation cost of complex entries.** Native bracket child legs come
  back with Alpaca-generated `client_order_id`s rather than our numeric ids, so
  `_process_alpaca_order` must adopt them instead of expecting its own. This is
  the bulk of the work and the part that can hurt live trading if rushed. See
  also the `client_order_id` reuse hazard — never delete max-id `tradingorder`
  rows.
- **Observability.** A still-blocked order logs at `debug` only, which is why a
  nine-day deadlock went unnoticed. Any lock older than a session should log at
  `warning` with the blocker id.

---

## If you change this again

Add a dated section above with:

1. The **measurement** that motivated the change — an actual broker response,
   not a reading of the docs. Every wrong turn in this file came from reasoning
   about the wash-trade rule instead of probing it.
2. Which **premise of the previous design** it invalidates, stated explicitly.
   Both previous designs were correct given what had been measured and wrong
   given what had not.
3. The **revisit trigger** — the observable that would mean this design has also
   failed. Then make sure something actually alarms on it, because 06-03 wrote
   its trigger down and nothing watched it.

Reproduce the probe table with a paper account before trusting any of it; the
scripts are trivial (submit, read the error code, cancel) and the broker is the
only authority here.

**Probe trap — use the owning account's credentials.** Accounts 1/2/3 are
separate Alpaca paper accounts with separate keys. Querying an order belonging
to account 2 with account 1's `TradingClient` returns `40410000 "order not
found"`, which looks exactly like a stale/vanished order. On 2026-08-05 this
produced a false "phantom blocker" diagnosis: orders 475 (AXTI) and 478 (RBLX)
were reported as `NEW` in the DB but gone at the broker, and a whole
reconciliation fix was proposed for it. Re-queried with account 2's keys, both
were `NEW` and working exactly as recorded. Reconciliation was never broken —
`refresh_orders` Step 4 is correct, and its "not found in Alpaca" warning has
never fired in the log, which is the confirming evidence. Always join
`tradingorder.account_id` to the credentials before concluding anything about
broker state.
