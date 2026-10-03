# Interactive Brokers (IBKR) support: gap report and design

Date: 2026-10-03. Branch `feat/ibkr-support` (worktree `BA2-ibkr`, based on origin/dev 47f67180).
Goal (operator): "IBKR support: manual, automated and options." An IBKR account must work
wherever Alpaca and TastyTrade accounts work today.

Nothing in this document was verified against a live Gateway: there is no IBKR account to test
against. Every IBKR-side fact that is not in the `ib_async` 2.1.0 source or the platform code is
marked **UNVERIFIED** and is checked by `tools/ibkr_paper_smoke.py` when the operator runs it on a
paper Gateway. Where a fact is unverified the implementation takes the conservative reading
(refuse, under-report, floor to whole shares); each such choice is listed in section 9.

## 1. Where the code stands

`ba2_trade_platform/modules/accounts/IBKRAccount.py` (652 lines) is a read-mostly stub that was
disabled by audit finding A1 (2026-07-01): `submit_order` overrides the `AccountInterface`
template instead of implementing `_submit_order_impl`, so every TradeManager call with the
interface kwargs raised `TypeError`, and a plain call bypassed validation, transaction creation and
protective legs. Beyond that it is defective in ways that make it unusable even for reads:

* it is **abstract** (cannot be instantiated; the settings dialog hides it): it lacks
  `get_balance`, `get_order`, `get_orders`, `refresh_positions`, `symbols_exist`, `adjust_tp`,
  `adjust_sl`, `adjust_tp_sl`, `_submit_order_impl`, `_get_instrument_current_price_impl`;
* it uses names that do not exist: `OrderStatus.CANCELLED` (it is `CANCELED`),
  `TradingOrder.filled_quantity` (it is `filled_qty`), `Position` keyword fields that the model
  does not have, `order.quantity`-style access on the wrong object;
* it overrides `get_instrument_current_price`, which bypasses the shared price cache and the
  per-symbol lock, and it calls the blocking `ib.sleep()` inside the request path;
* `self.ib.connect(...)` (the synchronous ib_async wrapper) is called from whatever thread asks.
  ib_async's sync wrappers drive `asyncio.get_event_loop()`, which does not exist on a worker
  thread, and on the NiceGUI loop thread they would either raise or nest a loop;
* `get_account_info` reports `BuyingPower` raw, with no leverage semantics, and
  `settings.get("account", "")` falls back to an empty account string (a multi-account login
  would then address the wrong account).

The existing test `tests/test_ibkr_get_positions_contract.py` pins one good rule (tri-state
`get_positions`) against a `MagicMock` that bypasses `__init__`; it is rewritten onto the new fake
and keeps every assertion.

## 2. Contract: what the interfaces require, what exists, what is missing

`Req` = who requires it. `A` = `AccountInterface`, `R` = `ReadOnlyAccountInterface`,
`O` = `OptionsAccountInterface`. "Now" is the state of `IBKRAccount.py` at the branch point.
"Plan" is where it is implemented.

### 2.1 Abstract methods (class cannot be built without them)

| Method | Req | Now | Plan |
|---|---|---|---|
| `_submit_order_impl(order, tp, sl, is_closing, use_complex)` | A | wrong override of `submit_order`, raises | stage 2: MKT/LMT/STP/STP LMT, DAY rules, orderRef, ack wait; OCO order type places an OCA pair |
| `cancel_order(order_id)` | A | wrong signature (takes a row), `CANCELLED` | stage 2: db id or broker id, sets `PENDING_CANCEL`, OCO cancels both legs |
| `modify_order(order_id, trading_order)` | A | wrong signature | stage 2: true in-place modify (same `orderId`), only for orders this client placed |
| `adjust_tp`, `adjust_sl`, `adjust_tp_sl` | A | missing | stage 2: shared `ProtectiveLegsMixin` (section 6) |
| `get_balance()` | R | missing | stage 2: `NetLiquidation` or `None` |
| `get_account_info()` | R | partial, broken keys | stage 2: dict with `buying_power`, `equity`, `cash`, margin fields (section 5) |
| `get_positions()` | R | tri-state OK, wrong fields | stage 2: STK only, tri-state kept, marks from portfolio, no fabricated prices |
| `get_orders(status)` | R | missing | stage 2: open + completed trades mapped to unsaved `TradingOrder`s |
| `get_order(order_id)` | R | missing | stage 2 |
| `symbols_exist(symbols)` | R | missing | stage 2: batched `reqContractDetails`, US STK/USD only |
| `_get_instrument_current_price_impl(sym_or_list, price_type)` | R | missing (it overrode the public method) | stage 2: batched snapshots, delayed data refused |
| `refresh_positions()` | R | missing | stage 2: `get_positions() is not None` |
| `refresh_orders()` | R | basic status sync, wrong fields | stage 2: orderRef match, partial fills, OCO legs, PENDING_CANCEL rules, dependents |
| `get_dividends()` | R | stub `[]` | stage 2: Flex Web Service when configured, else `[]` with a one-time WARNING (section 7) |
| `get_filled_trades()` | R | stub `[]` | stage 2: `reqExecutions` (today to 7 days, see 7), else documented gap |
| `get_balance_history()` | R | stub `[]` | stage 2: Flex `EquitySummaryByReportDateInBase` when configured, else `[]` + WARNING |

### 2.2 Optional overrides that Alpaca and/or TastyTrade provide

| Method | Alpaca | Tasty | IBKR plan |
|---|---|---|---|
| `get_account_snapshot()` | yes | yes | stage 2, sole source of margin multiplier / buying power |
| `get_symbol_margin_info(symbols)` | yes | yes | stage 2: fractionable from `ContractDetails.minSize`, shortable-aware; omitted when unknown |
| `preview_order_impact()` | no | yes (dry run) | stage 2: `whatIfOrder` |
| `get_broker_floating_pl()` | no | yes | stage 2: portfolio `unrealizedPNL` sum (STK) or `None` |
| `get_available_position_quantity()` | yes | base | base derives from `get_positions()`; IBKR has no per-order hold figure, so base behaviour (`abs(qty)`) |
| `get_cash_transfers()` | yes | yes | stage 2: Flex when configured, else `[]` + WARNING |
| `_get_market_hours_impl()` | yes | yes | not overridden: the shared offline NYSE calendar answers; see open question Q9 |
| `_classify_order_error()` | yes | yes | stage 2: IB error-code table (section 8) |
| `_is_washtrade_lock_candidate()` | n/a | n/a | overridden `False`: IB has no wash-trade rejection (section 4.6) |
| `modify_order` real implementation | yes (replace) | no | stage 2 (in-place) |

### 2.3 Options (`OptionsAccountInterface`)

| Method | Req | Alpaca | IBKR plan (stage 3) |
|---|---|---|---|
| `get_option_chain(underlying, expiry_min, expiry_max, option_type, strike_min, strike_max)` | abstract | snapshots + contract meta | `reqSecDefOptParams` for the grid, then streaming ticks for the filtered contracts, bounded lines |
| `get_option_quote(occ)` | abstract | snapshot | one qualified `Option`, streaming snapshot with model greeks |
| `get_atm_implied_volatility(underlying)` | abstract | nearest-strike IV in 20-45 DTE | same rule on the IBKR chain |
| `get_option_positions()` | abstract | tri-state | `OPT` portfolio rows, tri-state, `avgCost / multiplier` |
| `_submit_option_order_impl(order, legs, leg_orders)` | abstract | MLEG request | single `Option` order, or `BAG` combo with `ComboLeg`s |
| `close_option_position(...)` | abstract | single-leg close riding the open transaction | same body (broker-neutral) |
| `get_option_activities(after)` + `reconcile_option_assignments(acts)` | hasattr hooks in `TradeManager._reconcile_account_option_activities` | OPASN/OPEXC/OPEXP/OPCSH | **not claimed**: see 7.3. Without them the lifecycle falls back to `reconcile_externally_closed_option_transactions` (position vanished => close), exactly as for a broker with no activity feed |
| `OPTION_GREEKS_SOURCE` | class constant | `"broker"` | `"broker"` (IB model greeks) |
| `option_modelled_half_spread` etc. | backtest only | n/a | n/a |

Everything else on `OptionsAccountInterface` (cover guard, reserve pool, assignment capacity,
`submit_option_order` persistence) is concrete and broker-neutral; it needs only the six abstract
methods above plus a truthful `get_option_positions`/`get_positions` (the cover and
assignment maths read positions and orders, never IBKR).

The historical option-data **ingest** interface (`OptionsDataProviderInterface`,
`ba2_providers/options/*`) feeds the *backtest* cache. IBKR has no bulk historical option-chain
endpoint (its historical bars are per-contract, with a 60-request/10-minute pacing limit and no
expired contracts), so no IBKR provider is added; this is not a gap in live trading.

## 3. Connection lifecycle and threading

### 3.1 The facts that drive the design

`ib_async` is asyncio-based. Its synchronous wrappers (`IB.connect`, `IB.positions` is a pure read,
`IB.reqContractDetails`, ...) call `util.run()` against `asyncio.get_event_loop()`. This platform
is NiceGUI (one asyncio loop on the main thread) plus thread pools (`JobManager`, `WorkerQueue`,
`run.io_bound`). Calling the sync wrappers directly from a pool thread fails ("no current event
loop") and from the NiceGUI thread would nest into a running loop. `TastyTradeAccount` solved the
same problem with a persistent background loop (`_run_async`); this design copies that shape and
tightens it.

### 3.2 Design

* Each `IBKRAccount` instance owns one **daemon thread running its own asyncio loop**
  (`_IBLoopThread`) and one `ib_async.IB` instance created *on that loop*. No ib_async call is ever
  made from another thread. The NiceGUI loop and the worker threads only ever submit coroutines to
  it with `asyncio.run_coroutine_threadsafe(...).result(timeout)`.
* Only `*Async` ib_async methods are awaited inside those coroutines; the non-blocking calls
  (`placeOrder`, `cancelOrder`, `reqMktData`, `cancelMktData`) are also made from coroutines, so
  they run on the IB loop thread. ib_async's own state (`ib.trades()`, `ib.positions()`,
  `ib.portfolio()`) is read inside coroutines too, so it is never read mid-mutation from another
  thread and needs no lock.
* **Cannot block the UI loop:** the IB socket reader and every ib_async callback run on the
  account's private loop thread, never on the NiceGUI loop. A facade call made *from* the NiceGUI
  thread blocks that thread for at most the call's bounded timeout (reads 20 s default, order ack
  10 s, connect 15 s). The same is already true of the Alpaca/TastyTrade facades; `_run` logs a
  WARNING with the call site whenever it is entered from a thread that has a running asyncio loop,
  so the offending UI call is findable (and `run.io_bound` is the remedy).
* **Cannot deadlock:** `_run` raises `RuntimeError` when called *from the IB loop thread itself*
  (a coroutine waiting on the loop it runs on would deadlock); the only per-account lock is an
  `asyncio.Lock` (connect) that is never held across a facade call; the cross-thread submit lock
  is `AccountInterface._submit_lock()` (an `RLock` keyed by account id) and the IB loop never takes
  it; every wait has a timeout, after which the pending future is cancelled and a `TimeoutError`
  naming the account and operation is raised. Tests pin all four properties against a fake that
  blocks on purpose.
* `__init__` validates settings and attaches to the account's runtime but does **not** connect (a
  Gateway that is down at startup must not make the account unusable). The first call connects; a
  failed connect raises `IBKRConnectionError` and arms a 15 s cooldown so a Gateway outage does not
  turn every price lookup into a connect storm.
* **One runtime (thread + loop + TWS session) per account DEFINITION, shared by every
  `IBKRAccount` object** (`ibkr_runtime.get_runtime`). This is not an optimisation: `TradeManager`
  builds a fresh `account_class(id)` per call, and the instance cache is dropped by `/api/reload`; a
  connection per object would put several sessions on one `clientId`, which TWS refuses (error 326) or
  resolves by dropping the older one. Caches (contract details, market rules, previous closes) live on
  the shared runtime for the same reason. Dropping an object never disconnects; `close()` (app
  shutdown, tests) does. A changed host/port/client id/account/flags replaces the runtime and closes
  the old session, so a settings edit needs no restart.

### 3.3 Reconnects, Gateway/TWS restarts, client ids

* IB Gateway restarts itself once a day (configurable, default around 23:45-00:45 New York; **UNVERIFIED**
  window, operator-configurable) and requires a weekly re-login (Sunday) unless the operator uses
  IBC or the "auto restart" token. During that window `isConnected()` is false and a connect
  attempt is refused or times out. Policy: reads report **failure** (`None` for `get_positions`,
  `get_option_positions`, `get_balance`; `[]`+ERROR log where the interface has no tri-state, as
  Alpaca/Tasty do; an all-`None` snapshot), writes raise (order row -> `ERROR` with the connection
  message, never queued and replayed). The next call after the cooldown reconnects.
* `connectAsync` is called with ib_async's default startup fetch (positions, open orders, completed
  orders, account updates, executions), so a reconnect re-reads open orders and fills; there is
  no manual replay of events missed while down, because `refresh_orders` reconciles from the
  broker's open/completed lists by `orderRef`, which is the recovery path.
* `disconnectedEvent` marks the connection down immediately (so a half-open socket is not trusted).
* **Client id collisions:** TWS/Gateway allows one session per `clientId`. Error 326 ("client id
  already in use") is raised as `IBKRConnectionError` with the remedy text; the code never
  auto-increments the client id (that would silently open a second API session, and orders placed
  under client A cannot be modified or cancelled under client B).
  Client id 0 is the "master" client: it alone can see/cancel orders typed in TWS by hand. Default
  stays 1. Two accounts on one Gateway need two different ids.
* **Ports:** TWS live 7496, TWS paper 7497, Gateway live 4001, Gateway paper 4002. Default 4002
  with `paper_account=True`.
* **Paper/live guard (`paper_account` defaults to checked, matching the 4002 default port and the
  already-merged settings-dialog default):** after connecting, the account id must exist in `managedAccounts()`, and
  `paper_account=True` requires an id starting `DU` while `paper_account=False` requires one that
  does not (paper accounts are `DU...`). A mismatch refuses to trade (`IBKRConnectionError`), so a
  paper-configured row can never address a live account through a wrongly forwarded port, and the
  reverse.
* **Read-only:** `read_only=True` is passed to `connectAsync(readonly=True)`; the facade also
  refuses writes itself (`IBKRReadOnlyError`, classified `UNAUTHORIZED`) before a message is sent.
* **Market-data type:** `reqMarketDataType(2)` (frozen: live while open, last values when closed).
  Any ticker reporting `marketDataType` 3 or 4 (delayed) is treated as **no price**: delayed data
  must never reach a sizing decision. A missing market-data subscription (error 354/10089) therefore
  surfaces as "no price", not as a stale one.

## 4. Orders

### 4.1 Contract resolution

* Equity: `Stock(symbol, "SMART", "USD")`, qualified once through `qualifyContractsAsync` and cached
  per symbol (`conId`, 24 h). Share classes: `BRK.B` -> IB symbol `BRK B` and back
  (`modules/accounts/ibkr_mapping.to_ib_symbol/from_ib_symbol`). An ambiguous or unknown symbol
  raises (`error 200`/ambiguity), never guessing a listing: the candidate list is filtered to
  secType STK, currency USD, a US primary exchange; more than one survivor is an error naming them.
* Options: OCC symbol <-> (`root`, expiry, right, strike) in the pure mapping module; the
  contract is `Option(symbol=root, lastTradeDateOrContractMonth="YYYYMMDD", strike, right="C"/"P",
  exchange="SMART", currency="USD", multiplier="100")`, qualified (which fills `conId` and
  `tradingClass`). Non-standard OCC roots (adjusted contracts) and a non-100 multiplier are
  refused with the same OPT-L7 rule Alpaca applies.
* Combos: `Contract(secType="BAG", symbol=<underlying>, exchange="SMART", currency="USD",
  comboLegs=[ComboLeg(conId, ratio, action, exchange="SMART")...])`.

### 4.2 Order ids and correlation (`client_order_id`)

Three ids exist at IBKR: `orderId` (an int, per `clientId`, unique only for orders this client
placed), `permId` (global, permanent, but assigned only after TWS acknowledges, so it can be 0 for
a moment) and `orderRef` (free text, echoed back on every status/exec, up to 128 chars).

* **Correlation key = `orderRef`** (the analogue of Alpaca's `client_order_id`):
  `ba2:<account_def_id>:<trading_order_id>:<nonce>`, an OCO stop leg `...:<nonce>:SL`. The nonce is an
  8-hex random token stored in the row's `data["ibkr_nonce"]`: SQLite recycles `tradingorder` ids and
  instances share ids, so an id alone must never identify an IB order or execution.
  `refresh_orders` matches on it first and then falls back to `broker_order_id`.
* **`broker_order_id` = `str(permId)`** once known (stable across reconnects and daily restarts,
  and what TWS shows). If TWS has not yet assigned a `permId` when the submit ack arrives,
  `o<orderId>` is stored and upgraded to the `permId` on the next refresh. Matching understands both.
* A submit that gets neither an acknowledgement nor an error within the ack timeout is **not
  retried and not failed**: the row keeps `PENDING_NEW` with the `o<orderId>` id and
  `refresh_orders` resolves it (by `orderRef`) from the broker's open/completed lists. A resend
  would risk a duplicate order, which is worse than a late one.

### 4.3 Order types and time in force

| Our type | IB `orderType` | price fields |
|---|---|---|
| `MARKET` | `MKT` | none; TIF **DAY** (or IOC/OPG/FOK if the row says so). Never GTC (the 2026-10-02 TastyTrade lesson; IB would hold a GTC market order for the next session) |
| `BUY_LIMIT`/`SELL_LIMIT` | `LMT` | `lmtPrice` |
| `BUY_STOP`/`SELL_STOP` | `STP` | `auxPrice` = stop |
| `BUY_STOP_LIMIT`/`SELL_STOP_LIMIT` | `STP LMT` | `auxPrice` = stop, `lmtPrice` = limit |
| `OCO` | two orders in one OCA group (4.4) | TP = limit, SL = stop-limit with `OCO_STOP_LIMIT_CUSHION` |
| `TRAILING_STOP`, `OTO` | refused (`ValueError`) | not produced by any current writer |

`good_for` maps `day/gtc/ioc/fok/opg/gtd` -> `DAY/GTC/IOC/FOK/OPG/GTD`; unrecognised values are
logged and become DAY (a resting limit order that silently became GTC is the worse error).
`outsideRth` is always False. Prices are rounded to the contract's price increment using the
market rule (`reqMarketRule` via `ContractDetails.marketRuleIds`, pure rounding function in the
mapping module), never to a fixed number of decimals, so options (0.01/0.05) and sub-$1 stocks
(0.0001) both pass IB's min-tick check (error 110).

### 4.4 Protective legs, brackets, OCO (so TradeManager needs no broker special case)

The platform's protection model is not "bracket attached to the entry". It is: the entry is a plain
order; `submit_order` then calls `adjust_tp_sl(transaction, tp, sl, source="initial_setup")`, which
maintains **one standing exit order per transaction** as a `TradingOrder` row: both set -> an `OCO`
row, TP only -> a limit row, SL only -> a stop row. While the entry is unfilled that row is
`WAITING_TRIGGER` and DB-only; `TradeManager._check_all_waiting_trigger_orders` submits it when the
entry reaches `FILLED`. `TransactionHelper.is_resting_protection`, `refresh_transactions`, the
exposure maths and the UI all read that structure (an OCO parent row plus one `parent_order_id`
child for the second leg).

IBKR therefore follows the same contract:

* `adjust_tp/sl/tp_sl` run the **same** exit-order maintenance code as Alpaca. It is broker-neutral
  (it only creates `TradingOrder` rows and calls `self.cancel_order` / `self.submit_order`), so it
  is lifted into `modules/accounts/ibkr_protective_legs.ProtectiveLegsMixin` for IBKR. AlpacaAccount is
  deliberately **not** migrated onto it in this change (live prod code, no way to regression-test
  against Alpaca's API here); migrating it is a follow-up, listed in section 9.
* An `OCO` row submits as **two IB orders in one OCA group**: the TP leg (`LMT`) and the SL leg
  (`STP LMT`, limit = stop x (1 -/+ `OCO_STOP_LIMIT_CUSHION`), the platform's standing decision for
  Alpaca). `ocaType=2` (remaining orders proportionately reduced *with block*): a partial fill of
  one leg shrinks the other instead of cancelling it, so a part-filled take-profit never leaves the
  rest of the position stop-less. **Both legs are transmitted, the stop FIRST** (`transmit=False` is a parent/child bracket device and
  holds nothing back in an OCA group, so the pair is not atomic at the broker); if the take-profit
  fails the stop is cancelled and the failure raised. A leg already carrying its orderRef (a retry) is
  adopted, not re-placed. (Superseded: see "Review round" below.)
* DB shape mirrors Alpaca: the OCO **parent** row is the TP leg (`broker_order_id` = its id); the SL
  leg is a child row (`parent_order_id` = parent, comment `<ts>-OCO-SL-[PARENT:..]`). The parent's
  status follows the TP leg, the child's the SL leg, so "stop fired" is a FILLED child + CANCELED
  parent, as on Alpaca, and the quantity maths (`refresh_transactions` sums filled child quantity,
  never both) is unchanged.
* **Price changes use IB's real modify** (re-`placeOrder` with the same `orderId`): the stop is never
  absent between cancel and replace, which Alpaca cannot offer. `_handle_filled_entry_exit` first
  tries `_modify_exit_in_place` (same structure, same quantity, only prices differ); on any doubt it
  falls back to the shared cancel-then-chained-replacement path. The in-place path is only taken for
  orders placed by this `clientId`.
* Cancelling the OCO parent cancels both legs; `PENDING_CANCEL` -> `CANCELED` only when IB confirms
  (same rule as Alpaca/Tasty), so a chained replacement waits for the real release.
* A `use_complex_order` request cannot occur (4.6). Should one arrive it raises, never ignores it.

### 4.5 Fractional shares and short selling

* **Fractional:** IBKR accepts fractional quantities through the API for eligible stocks and for
  MKT/LMT-type orders only (**UNVERIFIED** exact restrictions; TWS also needs "fractional" enabled
  for the account). Conservative rule, identical in spirit to TastyTrade: a fractional quantity on a
  stop/stop-limit/OCO is refused; a fractional MKT/LMT goes out as DAY. Eligibility is read from
  `ContractDetails.minSize`/`sizeIncrement` (< 1 => fractionable). If the fields are absent the
  answer is `None` (unknown) and sizing floors to whole shares, never guessed. `get_symbol_margin_info`
  reports `fractionable` tri-state accordingly.
* **Short selling:** "sell is sell" holds. A SELL that is not closing and whose transaction side is
  SELL opens a short; `AccountInterface._validate_trading_order` already enforces the expert's
  `enable_sell` permission and the netting rules, so the broker layer adds only the broker's own
  fact: shortability. `_require_shortable` reads the shortable-shares tick (generic tick 236,
  `Ticker.shortableShares`): `> 2.5` is accepted ("at least 1000 shares available" in IB's own
  scale = shortable *and* easy to borrow, the Alpaca rule), anything lower or unreadable is refused.
  The scale is **UNVERIFIED** in detail. Protective legs of a long are SELLs with a `depends_on_order`
  and are never gated (as in Alpaca). A cash account cannot short at all; IB's rejection (201) is
  classified `INSUFFICIENT_QTY`.

### 4.6 Wash trades

Alpaca rejects an order that opposes a working market/stop order on the same symbol (40310000), and
the platform builds its `WASHTRADE_LOCKED` / `use_complex_order` machinery around that. IBKR has no
such rule for ordinary orders (it has self-cross prevention, which rejects only an order that would
trade against the account's own resting order at a marketable price). Therefore
`IBKRAccount._is_washtrade_lock_candidate` returns `False`: orders are not locked by an opposing
protective stop, and `use_complex_order` never reaches `_submit_order_impl`. A self-cross rejection
text ("cross") is classified `WASH_TRADE` so it is visible if it ever happens. **UNVERIFIED**: if the
paper run shows IB rejecting a buy while a protective sell stop rests, flip the override.

## 5. Account state: buying power, margin, PnL

IBKR account-summary tags used (all USD; a non-USD base currency raises `IBKRUnsupportedCurrency`):

| Tag | Meaning (IBKR) | Used for |
|---|---|---|
| `NetLiquidation` | total value | `equity`, `net_liquidation`, `get_balance()` |
| `TotalCashValue` | cash incl. margin loans (can be negative) | `cash` |
| `SettledCash` | settled cash | `non_marginable_buying_power` (raw, may be negative) |
| `AvailableFunds` | equity-with-loan minus **initial** margin requirement | the buying-power base (below) |
| `BuyingPower` | broker's intraday figure, ~4x `AvailableFunds` for a margin account, 1x for a cash account | kept in `raw`, **never** the sizing number |
| `ExcessLiquidity` | equity-with-loan minus **maintenance** margin (cushion against liquidation) | `raw`, informational |
| `InitMarginReq`, `MaintMarginReq`, `Cushion`, `EquityWithLoanValue`, `GrossPositionValue` | | `raw` |

**Which feeds `get_account_info` / `get_account_snapshot`:**

* `buying_power` = `min(AvailableFunds x m, SMA x m [only when published and > 0], ExcessLiquidity x m)` (section
  12/13; `None` when it cannot be derived, and `get_account_info` then raises). `m` = `margin_multiplier`, where `margin_multiplier` = **2.0 when
  `BuyingPower / AvailableFunds >= 1.9`** (a Reg-T margin account: IB's `BuyingPower` is 4x
  `AvailableFunds` intraday, which is the day-trading figure we deliberately do not trade on) and
  **1.0 otherwise**. This is the Reg-T *overnight* leverage, the number the platform already uses for
  Alpaca (`regt_buying_power`) and TastyTrade (2.0 for margin). When `AvailableFunds` is 0 or
  missing the multiplier is 1.0 (cannot tell, so no leverage). The ratio heuristic is
  **UNVERIFIED** and is printed by the smoke script; the failure direction is under-reporting.
* Portfolio-margin accounts are **not supported** as such: their requirement is lower than Reg-T, so
  the same formula under-reports (safe) rather than over-reports.
* `option_buying_power` = `AvailableFunds` (IB publishes no separate derivative pool; long options
  need full premium, short ones draw from the same margin pool).
* `long_market_value` / `short_market_value`: sums of `PortfolioItem.marketValue` by sign over **all**
  positions (options included, as `AccountSnapshot` requires), short negative.
* `margin_multiplier` is published (so `margin_enabled` accounts work); `supports_fractional` is
  False in the snapshot (per-symbol truth lives in `get_symbol_margin_info`).
* Nothing is defaulted: a missing tag leaves the field `None`; a failed fetch is an all-`None`
  snapshot.

`get_broker_floating_pl`: sum of equity `unrealizedPNL` over the portfolio when every STK row has
a mark, else `None` (caller falls back to the per-position sum).
`preview_order_impact`: `whatIfOrder`; `bp_cost` = `initMarginChange x margin_multiplier`,
`margin_requirement` = `|initMarginChange|`, `estimated_fees` = `commission`; IB `warningText`
becomes a warning; a rejected what-if is `accepted=False`. **UNVERIFIED** sign/scale on a real account.

## 6. Order refresh, partial fills, fills history

* `refresh_orders` reads `reqAllOpenOrders` + `reqCompletedOrders` + the session's `fills` once,
  matches each trade to a row by `orderRef` then `broker_order_id`, and applies: mapped status,
  `filled_qty` (from `orderStatus.filled`), `open_price` (`avgFillPrice`, only when > 0). A
  `Submitted` order with `0 < filled < totalQuantity` is `PARTIALLY_FILLED`; with `filled == total`
  it is `FILLED` even if IB's last status line lags.
* `PENDING_CANCEL` advances only on a final broker state (`OrderStatus.resolve_pending_cancel`).
* A cancel that raced a fill leaves `CANCELED` with `filled_qty > 0`: the shared
  `TransactionHelper.reconcile_canceled_partial_fill` is invoked, as for Alpaca.
* A row with a broker id that is in neither list is marked CANCELED only when **both** lists were
  fetched successfully, the row is older than 5 minutes, and a by-id check in `fills` finds nothing;
  otherwise it is left alone (absence is not evidence, the Tasty/Alpaca rule).
* After a pass, `_check_and_submit_dependent_orders` (PENDING dependents on a cancelled parent)
  runs exactly as in Alpaca, and `WAITING_TRIGGER` ones stay `TradeManager`'s.
* `get_filled_trades`: `reqExecutionsAsync` returns today's executions, and up to 7 days if the TWS
  Trade-Log setting allows; a request starting earlier logs a WARNING that the window is partial.
  For long history use Flex (below).

## 7. Things the IBKR API does not give, and what is done about each

1. **Dividends, deposits/withdrawals, NAV history.** The TWS API has no cash-transaction history.
   The only IBKR source is the Flex Web Service (an HTTPS report: token + query id). Optional settings
   `flex_token` and `flex_query_id` enable it (`modules/accounts/ibkr_flex`: pure XML parsing +
   an injectable fetcher; `CashTransaction` rows -> dividends/cash transfers net of withholding,
   `EquitySummaryByReportDateInBase` -> balance history). Unset: `get_dividends`,
   `get_cash_transfers`, `get_balance_history` return `[]` and log **one** WARNING per process that
   names the missing setting. The Flex attribute names are from IBKR's published schema and are
   **UNVERIFIED** against a real statement.
2. **Fills older than ~7 days:** see Flex above (`Trade` rows). Not implemented beyond the executions window.
3. **Option assignment / exercise / expiry feed.** Alpaca exposes `OPASN/OPEXC/OPEXP/OPCSH`
   activities; the TWS API has no equivalent (assignments show up as a position change and, in the
   Trades window, as executions with no `orderId`, **UNVERIFIED** in the API). IBKR therefore does
   not define `get_option_activities`/`reconcile_option_assignments`, which makes
   `TradeManager._reconcile_account_option_activities` a no-op for it (its documented `hasattr`
   gate). What still works: `reconcile_externally_closed_option_transactions` closes a transaction
   whose contract the broker no longer reports; the assigned stock arrives through
   `get_positions()`. What is **missing**: the exact bookkeeping Alpaca does for assignment
   (synthetic settlement orders, called-away accounting, OptionActivity audit rows). That needs the
   assignment execution shape, which only a paper account can show; `tools/ibkr_paper_smoke.py
   --dump-executions` prints the raw executions so the operator can supply a sample.
4. **Per-symbol margin rates:** IB publishes no per-symbol initial-margin rate over the API (the
   what-if does it per order). `get_symbol_margin_info` reports `bp_factor` neutral 1.0 for an unheld
   symbol (the Tasty rule: do not invent a penalty) and the real rate when a what-if has measured it.
5. **Per-order held quantity** (Alpaca `qty_available`): not published; base behaviour (`abs(qty)`).

## 8. Pacing and error codes

* ib_async's `Client` throttles outgoing messages (45/s). Our own limits: market-data **lines** are
  capped (default 90 concurrent streaming lines of the 100 a basic account gets) with a semaphore, so a
  full chain pull cannot starve the price lookups; chain pulls are chunked; `reqContractDetails`
  results are cached; `reqHistoricalData` is not used on a hot path (pacing 60/10 min).
* Errors arrive on `errorEvent(reqId, code, message, contract)`. They are recorded per `reqId`
  (= `orderId` for orders) for the life of the connection and drive both submit failure and
  classification.

| IB code | Meaning | Handling |
|---|---|---|
| 2100-2110, 2119, 2137, 2158, 399 (info text) | farm connection status, order-event warnings | INFO/DEBUG, never an error |
| 1100 / 1101 / 1102 / 1300 | connectivity lost / restored | connection flag; 1100 marks down |
| 326 | client id in use | `IBKRConnectionError` with remedy |
| 502, 504 | not connected / cannot connect | connection error |
| 200 | no security definition | `INVALID_SYMBOL` |
| 201 | order rejected (text decides) | "insufficient"/"margin"/"funds" -> `INSUFFICIENT_FUNDS`; "short"+("shares"/"locate") -> `INSUFFICIENT_QTY`; "cross" -> `WASH_TRADE`; "stop price"+"market" -> `STOP_THROUGH_MARKET` (UNVERIFIED text); else `UNKNOWN` with the text kept verbatim |
| 103, 110, 135, 136, 161, 10147, 10148 | duplicate id, min-tick, order not found, cannot cancel/modify | surfaced verbatim (`UNKNOWN`) |
| 202 | order cancelled | status path, not an error |
| 321 | API read-only / validation | `UNAUTHORIZED` when the text says read-only |
| 354, 10089, 10090, 10167, 10168 | no market-data subscription / delayed | "no price", never a stale price |
| 162 | historical pacing | logged, request fails |
| 434 | order size invalid | `UNKNOWN`, verbatim |

The mapping is a pure function in `modules/accounts/ibkr_mapping` and is table-tested; an
unrecognised **order status string** raises (`UnknownIBOrderStatus`), an unrecognised **error code**
is `UNKNOWN` with the broker text kept (the comment is what the Pending Orders UI shows, same as
the TastyTrade empty-error lesson).

## 9. Options design (stage 3 preview)

* Chain: `reqSecDefOptParamsAsync(underlying, "", "STK", conId)` -> expirations x strikes per
  exchange/tradingClass; choose the SMART (else first) entry whose `multiplier == "100"` and whose
  `tradingClass == underlying` (the standard class); filter by the requested expiry/strike window,
  build qualified `Option`s (chunked), stream ticks with generic tick `101` (open interest) for
  at most ~3 s per chunk, read `modelGreeks` (IV, delta, gamma, vega, theta), bid/ask/last, then cancel the
  lines. Rows with no two-sided quote keep `None` bid/ask. A contract that fails to qualify is
  excluded with an ERROR naming it.
* `OptionContract.volume`: IB's streamed `volume` is today's partial volume, not the prior completed
  session Alpaca reports (BT/live option parity B3). It is therefore left **`None`** in v1 rather
  than reported on the wrong basis; see Q4.
* Multi-leg: a `BAG` combo, `action="BUY"`, signed `lmtPrice` (+debit/-credit, the interface's own
  convention), TIF DAY, `SMART` routing. Children (`parent_order_id`) mirror the parent's status and
  take per-leg fill price/quantity from `Trade.fills` grouped by `conId`.
* Positions: `OPT` rows, `avgCost / multiplier` is the per-share premium; tri-state.
* `OPTION_GREEKS_SOURCE = "broker"`.

## 10. Open questions for the operator

Each has a conservative default already implemented; answering changes the default, not the structure.

1. **Stop leg type.** The OCO stop leg is a stop-limit with the same 0.5 % cushion Alpaca uses (a
   gap through the limit leaves the position unprotected). A plain `STP` (market on trigger)
   guarantees the exit but not the price. Keep parity (default) or switch IBKR to `STP`?
2. **Margin.** Reg-T 2x is published when the heuristic detects a margin account; `margin_enabled`
   is still the operator's switch. Is the account Reg-T or portfolio margin? Is `margin_factor`
   to be used on this account at all?
3. **Dividends / balance history.** Only available via a Flex query (token + query id with a Cash
   Transactions and an Equity Summary section). Will you create one? Until then these three seams
   return `[]` with a warning.
4. **Option volume basis.** Left `None` (unknown) because IB streams today's partial volume. A
   volume-gated strategy would then reject every IBKR contract. Accept per-contract historical-bar
   lookups (60 per 10 minutes pacing) for the final candidate set only?
5. **Option assignment bookkeeping.** Needs a sample of a real assignment (paper can generate one).
   Until then assignment/exercise is seen only as a vanished option position (see 7.3).
6. **Fractional shares.** Is the paper/live account enabled for fractional trading? Until the smoke
   script shows `minSize < 1` for a symbol, whole shares only.
7. **Client id and Gateway.** Gateway or TWS? Which port and `clientId`? Is IBC (auto re-login)
   used for the weekly re-authentication? Without it the Sunday re-login is manual.
8. **Wash trades.** Assumed absent at IBKR (4.6). Confirm on paper by placing a buy while a sell
   stop rests.
9. **Market hours.** Not overridden (offline NYSE calendar). IBKR's `ContractDetails.tradingHours`
   could be the source instead; worth it only if you trade outside the regular session.
10. **Alpaca consolidation.** The exit-order maintenance now exists twice (Alpaca's own and the
    shared mixin). Migrate Alpaca onto the mixin later, behind its existing test suite?
11. **Read-only mode** is honoured by refusing writes before they are sent; do you also want the
    Gateway itself set read-only for the first runs? (Recommended.)

## 11. Implementation record and assumptions (appended per stage)

### Stage 2 (equity), as built

Files: `modules/accounts/IBKRAccount.py` (adapter), `modules/accounts/ibkr_runtime.py` (loop thread,
connection policy, shared runtime), `modules/accounts/ibkr_mapping.py` (pure rules),
`modules/accounts/ibkr_protective_legs.py` (shared exit-order mixin), `modules/accounts/ibkr_flex.py` (Flex
parsing). Tests: `tests/ibkr_fakes.py` (behavioural fake of `ib_async.IB` over the real ib_async value
types), `tests/test_ibkr_*.py`, `packages/common/tests/test_ibkr_{mapping,flex}.py`.

Deviations from the sections above, and why:

* `paper_account` has a declared default (True). An existing test pins the dialog behaviour "a new
  IBKR account shows paper checked", and the connect-time paper/live guard makes the default fail-safe.
* **Flex is implemented**, not just designed (7.1): `flex_token` / `flex_query_id` enable
  `get_dividends`, `get_cash_transfers` and `get_balance_history`, cached 10 minutes; unset they return
  `[]` with one WARNING per process. Schema names are UNVERIFIED.
* **Account values come from the account-updates stream** (`ib.accountValues`, which ib_async subscribes at
  connect and IBKR pushes on every change), with `reqAccountSummary` only as the fallback for the first
  reads: the summary subscription refreshes about every three minutes, long enough to over-state buying
  power after a fill (UNVERIFIED cadence; the smoke script prints both sources side by side).
* **Market-data requests share one lock per connection** (price snapshots, option streaming batches, the
  shortable tick), so their lines can never add up past IBKR's ~100-line budget when a chain pull and a
  price lookup overlap.
* The in-place exit modification stores the **tick-rounded** price IB was sent, not the requested one.
* A `cancel_order` on an order IB already reports Cancelled returns True (goal met, the refresh
  records the final status); on a Filled one it returns False and says so.
* `get_positions` marks come from IB's own portfolio (`marketPrice`, `unrealizedPNL`); a missing mark
  is fetched as a snapshot and, if that is empty too, the whole fetch is reported as FAILED (`None`)
  rather than fabricating a price. `lastday_price`/intraday fields (display only) use the snapshot
  close and fall back to the mark (zero intraday change) when no close is available.
* `refresh_orders` never cancels a row on absence alone: the open and completed lists AND the execution
  list must all have been read, the row must be older than 5 minutes, and an execution by `orderRef` /
  `permId` settles it as FILLED instead. Older than 7 days with no evidence: left alone, loudly.

Assumptions taken (all conservative; each is the documented default, not a hidden one):

1. `Inactive` is a rejection (REJECTED). TWS also uses it for a user-deactivated order; a refresh that
   later sees the order working revives the row.
2. `PreSubmitted` = accepted/resting (ACCEPTED), never `WAITING_TRIGGER` (reserved for DB-only rows).
3. Market orders are DAY; resting orders with no `good_for` are GTC (Alpaca's default; a protective
   stop must survive the close). An unrecognised `good_for` is an error.
4. The OCO stop leg is a stop-limit with `OCO_STOP_LIMIT_CUSHION` (imported from `AlpacaAccount`, the
   single constant `TradeManager._force_close_breached_stops` also reads). Q1 asks whether to change it.
5. Margin: `margin_multiplier` 2.0 only when `BuyingPower / AvailableFunds >= 1.9`, else 1.0.
6. `ocaType=2` (reduce with block) for the OCO pair.
7. No wash-trade lock (`_is_washtrade_lock_candidate` is False).
8. Shortable means `shortableShares > 2.5`; unknown means refuse.
9. Fractional only when `ContractDetails.minSize`/`sizeIncrement` publish a sub-share step; otherwise
   whole shares, floored (never rounded up); a floor to zero is a CANCELED skip, not an ERROR.
10. Delayed market data is never a price (`marketDataType` 3/4), and `reqMarketDataType(2)` is requested.

### Stage 3 (options), as built

File: `modules/accounts/ibkr_options.py` (`IBKROptionsMixin`, mixed into `IBKRAccount`, which now also
inherits `OptionsAccountInterface`). Tests: `tests/test_ibkr_options.py` (46), fake extensions in
`tests/ibkr_fakes.py` (`add_option`, `add_option_chain`, `add_leg_fills`, line accounting).

* **Chain:** `reqSecDefOptParams` picks the standard class (100-share, trading class = the underlying's
  own root, SMART first) and supplies the expirations; **one `reqContractDetails` call per expiry with a
  wildcard strike/right** returns the ladder with conIds (not one call per strike); quotes stream in
  batches of 90 market-data lines (`_QUOTE_BATCH`), 3 s wait per batch, every line is cancelled in a
  `finally`. Rows: bid/ask/last (IB's negative "no quote" becomes `None`, a real zero bid is kept), model
  greeks, open interest from `callOpenInterest`/`putOpenInterest` by right, `volume=None`, `rho=None`,
  `greeks_source="broker"`. Delayed rows are excluded with an ERROR. A request spanning more than 1500
  contracts raises instead of truncating.
* **Contracts:** an OCC symbol resolves to exactly one `Option` (root as trading class, multiplier 100);
  a non-standard root or non-100 multiplier is refused (OPT-L7, as Alpaca). `localSymbol` with the spaces
  removed IS our OCC symbol.
* **Orders:** single leg = one `Option` order; 2-4 legs = one `BAG` order (`action=BUY`, legs carry their
  own sides, **signed limit: negative = credit**, ticked to 0.01), TIF DAY, `orderRef` = our parent id.
  The broker id is persisted before anything else can raise; a rejection raises, and
  `OptionsAccountInterface._unwind_failed_option_submission` terminalises the parent and its children
  (ERROR only when nothing reached IBKR); a missing acknowledgement leaves the rows open (`PENDING_NEW`)
  for the refresh to adopt, never resent.
* **Refresh:** the BAG is ONE IB order, so its children follow the parent's status; each leg's filled
  quantity is the parent's fill x the leg's ratio; a leg's price is set only when an execution for that
  contract was reported (matched by `(permId, OCC)`; shape UNVERIFIED), otherwise it stays NULL.
* **Positions:** `OPT` portfolio rows, `avgCost / multiplier` is the per-share premium, signed qty ->
  side, tri-state, malformed rows skipped. `get_positions` no longer includes options.
* **ATM IV:** the Alpaca rule (nearest strike, 20-45 DTE) on a +/-10 % strike band to keep the pull small.
* **Shared gates verified on this adapter:** assignment capacity (measured on `TotalCashValue`, not
  equity), short-put delivery exposure, cover guard, close-rides-the-transaction.

What is **missing**, exactly (nothing is faked):

1. `get_option_activities` / `reconcile_option_assignments`: not defined, so `TradeManager`'s option
   reconciliation hook is a no-op for IBKR. Assignment/exercise/expiry are seen only as a vanished option
   position (`reconcile_externally_closed_option_transactions`) plus the assigned shares arriving through
   `get_positions()`. The Alpaca-style settlement bookkeeping (synthetic settlement orders, called-away
   accounting, `OptionActivity` audit rows) needs the real execution shape of an assignment; run
   `tools/ibkr_paper_smoke.py --dump-executions` after a paper assignment to capture it.
2. `OptionContract.volume` (Q4). 3. `rho`. 4. No IBKR historical-option ingest provider
   (`OptionsDataProviderInterface`): that interface feeds the backtest cache and IBKR offers no bulk
   historical chain.

### Stage 4 (wiring, UI, docs), as built

* **Registry / settings UI:** `providers["IBKR"]` and the `InteractiveBrokers` alias already existed. The
  settings dialog hides an *abstract* provider (`selectable_account_providers`), so IBKR became selectable
  simply by becoming concrete; `tests/test_ibkr_conformance.py` pins that. The settings it declares
  (host, port, client_id, account_id, paper_account, read_only, optional flex_token / flex_query_id) render
  through the generic dialog (str/int/bool), which already refuses an unset required value by name.
* **Docs:** `docs/IBKR-SETUP.md` (IB Gateway paper setup: enabling the API, ports, trusted IPs, read-only API,
  client ids, restart behaviour, Flex), README broker table + links, CLAUDE.md note (rules specific to the
  adapter), `docs/INDEX.md`.
* **Smoke script:** `tools/ibkr_paper_smoke.py` (tested against the fake in `tests/test_ibkr_paper_smoke.py`):
  read-only session first, always; `--place-test-order` reconnects writable only after the account id is
  confirmed to start with `DU`, places one 1-share limit at half the last price, then cancels it. Prints each
  UNVERIFIED fact as a `[CHECK]` line and a summary block to send back.
* **Pins:** `ib_async>=2.1.0,<3` in `requirements.txt` with a floor/ceiling test in
  `tests/test_broker_sdk_pins.py` (the same pattern as tastytrade/alpaca-py).
* **Versions:** none bumped (instruction). The change touches `packages/` and `ba2_trade_platform/`, so before
  the branch is pushed **both** `testplatform/version.py` (packages/ change: distributed GA workers compare
  `TEST_APP_VERSION`) **and** `ba2_trade_platform/version.py` need their build number incremented (CLAUDE.md,
  "Versioning").
* **Restarts:** the platform loads the new account class only on restart of the instance that uses it; no
  existing account (there is no IBKR account in any live DB) is affected. `ib_async` is already installed in
  the venv (2.1.0); `requirements.txt` only gained bounds.

### Known limitations of this implementation (none are hidden by a fallback)

* **Share-class option underlyings** (BRK.B, BF.B): `get_option_quote(occ)` is given only the OCC symbol,
  whose root (`BRKB`) is not IB's underlying symbol (`BRK B`), so the contract lookup fails (loudly) unless
  the leg also carries its `underlying`, which the order paths do. Chains and orders are unaffected.
* **Orders placed by another client id** (typed in TWS, or a second API session) are listed and refreshed
  but cannot be cancelled or modified by this client (IBKR allows that only for the same client id, or the
  master client 0); the adapter says so instead of pretending. Run the platform as client 0 only if you
  want it to manage manual TWS orders.
* **A BAG combo's per-leg price** exists only if IBKR reports per-leg executions (UNVERIFIED); otherwise the
  leg rows keep a NULL price and the structure's net price is on the parent.
* **Reconnect while orders are resting** is covered by refreshing from IBKR's open/completed lists by
  `orderRef`; there is no replay of events missed while disconnected.
* **TWS Read-Only API + `readonly=True` connect:** ib_async skips its startup open-order fetch in read-only
  mode; the adapter never depends on it (every order read is an explicit `reqAllOpenOrders`).

## 12. Review round (2026-10-03, items 1-9 and follow-ups)

An independent review found the adapter safe to merge disabled but unsafe to enable. Each finding was
reproduced first (probes A-D, `tests/test_ibkr_review_fixes.py`), then fixed. Where the fix rests on real-IB
behaviour that cannot be verified here, the most conservative variant was implemented and the fact was added
to `tools/ibkr_paper_smoke.py`'s `[CHECK]` list.

| # | Finding | Fix |
|---|---|---|
| 1 | `ValidationError` (set by ib_async on a WARNING 399/404/10349 while the order stays live) was a rejection | maps to `PENDING_NEW`; warning codes are their own severity (`order_warning`), logged, never fail the order; `_wait_ack` keeps waiting |
| 2 | stale errors survived reconnects; ids are reused | errors carry a sequence number, are cleared on connect, and `_wait_ack` reads only errors that arrived after the mark taken just before `placeOrder` |
| 3 | a timeout after `placeOrder` marked the row ERROR and a retry duplicated the order | before EVERY placement (equity, OCO legs, options) the orderRef is looked up in session trades, open orders and completed orders and a match is ADOPTED; after `placeOrder` has run nothing marks the row ERROR (it stays `PENDING_NEW` with `o<orderId>` for the refresh); the submit budget is split explicitly (`_submit_budget`) |
| 4 | an unsynced positions cache read as `[]` ("flat") | `connectAsync(raiseSyncErrors=True)`, and every positions read awaits an explicit `reqPositions`; failure -> `None` |
| 5 | OCO `transmit=False` TP relied on bracket semantics | both legs transmitted, STOP FIRST then take-profit; TP failure cancels the stop and raises; the fake no longer models transmit for OCA |
| 6 | a modify was never confirmed (ib_async keeps the status) | confirmed only by ib_async's `Modified` log entry; refusal = order error or final state; unconfirmed/refused rolls the shared Order object back, stores nothing, and `_modify_exit_in_place` rolls back its first leg |
| 7 | orderRef collided across recycled ids/instances | per-row nonce in the ref and on the row; refresh matches by ref only when account, id AND nonce match and the order is this client's; executions are matched by the nonce-bearing ref or permId |
| 8 | absent orders were marked CANCELED assuming 7 days of executions | never: an unlisted order with no execution is left unchanged with an `UNRESOLVED` warning (once per row) |
| 9 | `AvailableFunds x 2` can exceed the true Reg-T room | `buying_power = min(AvailableFunds x m, SMA x m [if published], ExcessLiquidity x m)`; a margin account without `ExcessLiquidity` publishes `None`; components are in `raw["bp_components"]` |

Follow-ups: `_pick_price` no longer falls back to yesterday's close (nothing live means no price; `close` only
when asked by name); a settings edit drops the object's runtime handle and the shared session is replaced
(`IBKRRuntime.close` fails waiting callers at once instead of after their timeout); an OCO leg's quantity and
every working order's prices follow IB's live order in `refresh_orders`; no assumed option multiplier
(an option position without one fails the fetch) and no assumed USD (a contract must say USD; Flex rows
without a currency are skipped); `OCO_STOP_LIMIT_CUSHION` is duplicated in `ibkr_protective_legs.py` (pinned equal
to Alpaca's by a test) instead of imported from the Alpaca adapter; `supports_trading` is `True` on the class
and `not read_only` on an instance, and a read-only account raises `IBKRReadOnlyError` BEFORE any row is
written.

Smoke-script additions (all under `[CHECK]`): OCA pair with both legs transmitted + ocaType 2 and what
cancelling one leg does to the other; order-warning codes seen and the Gateway's order precautions;
modification confirmation and refusal; completed-orders/executions survival across the nightly restart;
positions right after a reconnect (cache vs `reqPositions`); request ids vs `nextValidId`; Reg-T SMA vs
`AvailableFunds x 2`; negative BAG limit acceptance (what-if) and the per-leg combo fill shape. Partial/full
OCA fill reduction needs a real fill and is NOT exercised by the script.

## 13. Review round 2 (re-review of 4cb38821)

The first round's fixes were built on a fake that hid four real ib_async behaviours. `tests/ibkr_fakes.py`
now reproduces them (and `tests/test_ibkr_review_fixes2.py::TestRealIbAsync` pins them against the REAL
library with the socket stubbed): a `Modified` log entry only for an echoed status of exactly `Submitted`;
a rejected order turns `Cancelled` (not `Inactive`) with the error in its log; EVERY warning (105 110 165 321
329 399 404 434 492 and all 21xx) is `ValidationError` while the order lives; `openOrders` /
`completedOrders` / `positions` have ONE pending future each (a concurrent identical request steals the
answer); ids restart after a reconnect; resting stops are `PreSubmitted` outside the regular session.

| # | Finding | Fix |
|---|---|---|
| 1 | adoption picked up DEAD orders: the stop-through-market MARKET retry and the UI Retry never placed anything | adopt only an order that is working or has a fill AND matches side/type/quantity; a live order that differs is refused (no duplicate); an order that merely died has a spent ref, so the row gets a NEW nonce and is placed again; the lookup runs only for a row that already had a nonce (first attempts do none) |
| 2 | `Modified` is not logged for PreSubmitted stops, so every stop adjustment was reported failed and degraded to cancel+replace | confirm on `Modified` OR on a re-read of the open orders showing the SENT prices (after a short window); refuse on an error, a final state, or a warning plus a non-matching re-read; roll back only then |
| 3 | 21xx / 105 / 110 / 165 / 321 / 329 / 434 / 492 warnings were rejections | rejection is decided from the STATUS (Cancelled/ApiCancelled/Inactive), as ib_async decides it; a trade that already traded is a fact, not an error; one explicit exception, a 321 'read-only' warning on a new order (never placed) |
| 4 | concurrent requests stole each other's answer | one asyncio lock per request type on the runtime (openOrders, completedOrders, positions, executions, accountSummary) serialises every caller |
| 5 | orphan stop after a rejected OCO take-profit (cancel was fire-and-forget) | the stop's cancel is awaited until IB confirms; if it cannot be confirmed (or the stop filled) the live stop is recorded as a plain stop exit of the transaction and the failure is raised loudly (`ORPHAN STOP`) |
| 6 | an order that never reached IB stayed PENDING_NEW forever | a row IB never acknowledged, on none of IB's lists (a session-only `PendingSubmit` trade is local), with no execution under its ref, older than `_UNACKNOWLEDGED_GRACE_MINUTES` and inside the execution window, with a COMPLETE book, becomes ERROR 'never reached IBKR' + an Activity Log entry; an order IB DID acknowledge that vanishes stays UNRESOLVED (warning once + Activity Log entry), never CANCELED or ERROR |
| 7 | SMA <= 0 blocked trading; an unknown buying power fell back to cash in the shared clamp | SMA is a bound only when published and > 0; the binding component is logged when it changes; `get_account_info` RAISES when no buying power can be derived (the shared clamp then reaches `get_balance()`, a real net-liquidation figure, never `cash`) |
| 8 | `raiseSyncErrors=True` failed the connect on any slow startup request | startup fetches only the account-updates feed (`fetchFields=ACCOUNT_UPDATES`), `raiseSyncErrors=False`; every positions read still awaits `reqPositions` |

Follow-ups: an OCO with only the stop placed gives the PARENT no broker id and the stop its own child row (a
refresh never copies the stop's limit over the parent's take-profit); `_submit_budget(acks, reads, cancel_waits)`
is explicit and the lookup has its own `_lookup_budget`; a timeout names the read that expired (`inner wait` vs
`total budget`); a partial fill followed by Cancelled inside the acknowledgement window is recorded as the fill
(CANCELED + `filled_qty`, folded back like a refresh), never ERROR; `_runtime()` called inside a coroutine
never rebuilds (it would close the loop thread it runs on); the caller's order object carries the nonce in the
equity path too; changing the client id while orders are live logs an ERROR naming the orders the new session
cannot touch.

Known residuals: (a) the shared expert clamp falls through to `get_balance()` (net liquidation) when
`get_account_info` raises; it cannot be changed from the IBKR side. (b) A live order whose ref carries a spent
nonce after a refused retry is tracked only by its broker id. (c) After an unconfirmed stop cancel the plain stop
row protects the transaction, but nothing re-tries the cancel.

What only a paper session can answer (each is a `[CHECK]` line in `tools/ibkr_paper_smoke.py`): the status a
resting stop shows (Submitted vs PreSubmitted), whether a re-read after a modification shows the new price and
how fast; whether a 105/321/329 warning accompanies a refused modification; the 321 'read-only' text on a new
order; whether `reqAllOpenOrders` ever lists an order IB has not acknowledged (the never-reached rule assumes it
does not); how long a cancel takes to confirm outside the session (`_CANCEL_ACK_TIMEOUT` is 3 s); which of
`AvailableFunds` / `SMA` / `ExcessLiquidity` are published for cash, Reg-T margin and portfolio-margin accounts;
how long startup requests take (the connect no longer raises); completed-order / execution survival across the
Gateway's nightly restart; combo fill shape and negative-limit acceptance; partial OCA fill reduction.

## 14. Module placement (2026-10-03)

`ibkr_mapping.py`, `ibkr_flex.py` and `ibkr_protective_legs.py` (formerly `ba2_common/core/protective_legs.py`)
were first written under `packages/common`. They are imported ONLY by the IBKR files, and any change under
`packages/` forces a `TEST_APP_VERSION` bump that makes every distributed GA worker re-sync for nothing. Per
CLAUDE.md (broker-specific / live-only code is in-tree) they now live in `ba2_trade_platform/modules/accounts/`,
their tests in `tests/`, and `git diff 47f67180..HEAD -- packages testplatform` is empty (the docstring-only edit
to `OptionsAccountInterface.py` was reverted). Nothing under `packages/` imports them (checked by grep), so the CI
job that installs only `packages/*` never needs them. `tools/ibkr_paper_smoke.py` loads the two pure modules by
file path so the operator tool still imports no `ba2_trade_platform` code.
