# Portfolio Allocation — Design

Date: 2026-08-20
Status: Approved, not yet implemented
Branch: `pf_allocation`

## Problem

The platform can only put money to work through experts. There is no way to say
"hold 40% of this account in my ARK26 basket and 60% in NASDAQ30, evenly within
each" and have the trades computed for you. Every rebalance today is manual
arithmetic followed by hand-placed orders.

Instrument labels already exist and already group symbols the way a portfolio
would (`ARK26`, `NASDAQ30`, `HighRisk`), but nothing reads them as allocation
targets. `IncreaseInstrumentShareAction` is the one existing "move this symbol to
N% of equity" implementation and it has **never worked on Alpaca** — it calls
`.get()` on `get_account_info()`, which returns a pydantic object there, and the
`AttributeError` is swallowed (`packages/common/ba2_common/core/TradeActions.py:1493`,
`except` at `:1550`).

## Goal

A `Portfolio Allocation` page for accounts flagged as manually traded. It shows
the account's current allocation — by purchase value or by market value, your
choice — grouped by the labels you choose to manage, and runs a wizard that turns
target percentages into reviewed broker orders.

## Non-goals

Options allocation. Automated or scheduled rebalancing. Multi-account
allocation. Short positions — targets are long-only, and a negative target is
clamped to zero.

## Decisions

Every one of these was chosen explicitly; none is a default that fell out of the
implementation.

| # | Decision |
|---|---|
| 1 | Allocatable base = broker `buying_power` **+** the current value of positions carrying a managed label, where "current value" follows the valuation mode. |
| 2 | Targets are **notional**. Buying power is a feasibility constraint, not the unit of allocation. |
| 3 | Managed label percentages must total exactly 100%. Submit is blocked otherwise. |
| 4 | Symbol weights are percentages **within** their label, defaulting to even, and are persisted. |
| 5 | The default view shows current allocation with two columns, `% of label` and `% of total`, measured per the valuation mode. |
| 5a | **Valuation mode** is a per-account toggle, `cost` (purchase value) or `market` (`qty × price`), defaulting to `cost`. It selects what "current value" means everywhere: the base, the displayed percentages, and every delta. |
| 6 | Only symbols carrying a managed label are listed. Unmanaged positions are invisible; they reduce `buying_power` naturally. |
| 7 | A symbol in more than one managed label gets a ⚠ and a tooltip. Targets **sum**. No enforcement. |
| 8 | The page refuses to run when the account has any enabled expert. |
| 9 | Income (deposits + dividends) is a per-event ledger, consumed oldest-first by allocation runs. |
| 10 | Dry-run is in-memory. Nothing is written until you confirm. |
| 11 | No minimum order threshold. Every non-zero delta becomes an order. |
| 12 | Fractional shares are opt-in per run, with whole-share fallback on rejection. |
| 13 | Sells submit before buys. |
| 14 | One Transaction per symbol: trims and adds adjust the existing one; a zero target closes it. |
| 15 | Broker precheck replaces the two-pass estimate wherever it is available. |
| 16 | TastyTrade gets the full trading surface, unit-tested against a mocked SDK. Live verification is the user's. |
| 17 | `Instrument.name` becomes unique. Duplicate rows are merged by a data migration, and symbol writes are normalised in the shared helpers. |
| 18 | The label selection on the Overview account-growth chart persists in `app.storage.user`. Session storage, not the database. |

## Architecture

### Where the code goes

Per CLAUDE.md, shared and pure code belongs in the sibling packages; the in-tree
`ba2_trade_platform/core/*` files are alias shims whose edits are discarded.

**`packages/common/ba2_common/` (REAL — the source of truth):**

- `core/models.py` — five new tables (below), plus `unique=True` on
  `Instrument.name`.
- `core/portfolio_allocation.py` — **new**, the allocation engine. Pure, IO-free,
  unit-tested without a DB or a broker.
- `core/utils.py` — one new helper, `get_symbols_by_label()`.
- `core/interfaces/ReadOnlyAccountInterface.py` — the `manual_trading_enabled`
  setting, plus the `get_account_snapshot()` / `get_cash_transfers()` /
  `get_symbol_margin_info()` seams.
- `core/interfaces/AccountInterface.py` — the `preview_order_impact()` seam.

**In-tree (live-only, REAL):**

- `ui/pages/portfolio_allocation.py` — **new**, the page.
- `ui/main.py`, `ui/menus.py` — route and menu entry.
- `modules/accounts/AlpacaAccount.py` — snapshot, cash transfers, margin info,
  fractional-aware submission.
- `modules/accounts/TastyTradeAccount.py` — the trading surface.
- `core/InstrumentAutoAdder.py`, `core/JobManager.py`, `ui/pages/settings.py` —
  symbol normalisation at the instrument-creation paths.

### The allocation engine

`ba2_common/core/portfolio_allocation.py` holds all the arithmetic and knows
nothing about NiceGUI, the database, or a broker SDK. It takes plain data and
returns plain data, so the whole of the maths is testable in isolation:

```python
def compute_allocation(
    base_notional: float,
    valuation_mode: str,                # 'cost' | 'market'
    available_buying_power: float,
    labels: list[LabelTarget],          # label, target_pct, symbols[SymbolTarget]
    current: dict[str, PositionState],  # symbol -> qty, cost_basis, price
    margin: dict[str, MarginInfo],      # symbol -> bp_factor, fractionable, min_qty
    *,
    allow_fractional: bool,
    default_bp_factor: float,           # conservative fallback, = account multiplier
) -> AllocationPlan
```

`AllocationPlan` carries one `AllocationRow` per symbol — target notional, target
quantity, delta quantity, estimated value, buying-power cost, the labels it came
from, and a list of reason strings — plus plan-level totals: required buying
power, buying-power usage percent, and the pro-rata scale factor applied if the
plan didn't fit.

**Buying-power cost.** `bp_cost = notional × bp_factor(symbol)`, where
`bp_factor = initial_margin_rate × account_multiplier`. A fully marginable stock
in a 2:1 account has `0.5 × 2 = 1.0` and consumes buying power dollar for dollar;
a non-marginable one has `1.0 × 2 = 2.0` and consumes double. When
`Σ bp_cost(buys) > available_buying_power`, every buy scales down pro-rata and
the plan records the scale factor. Sells never scale.

**Rounding.** With fractional off, `qty = floor(target_notional / price)`. With
fractional on, quantity is rounded to the broker's `min_trade_increment` where
one is known, otherwise to 4 decimal places.

**Valuation mode.** A position's price moves after you buy it, so "how much of
my portfolio is in this symbol" has two defensible answers. `compute_allocation`
takes a `valuation_mode` of `cost` or `market` and derives each symbol's current
value as `cost_basis` or `qty × price` accordingly. `PositionState` already
carries `qty`, `cost_basis` and `price`, so no extra broker data is needed.

The mode is not cosmetic — it selects the meaning of "current value" in three
places at once, and they must never disagree:

1. the allocatable base, `buying_power + Σ current value of managed positions`;
2. the `% of label` and `% of total` columns in the default view;
3. every `delta = target_notional − current value`.

In `market` mode a position that has doubled reads as over-weight and gets
trimmed; in `cost` mode it reads at its purchase weight and is left alone. The
page states which mode produced the numbers on screen, and switching modes
re-computes rather than silently reinterpreting.

**Degenerate inputs.** A managed label with no symbols cannot absorb its
percentage; the engine allocates it nothing and records an
`unallocatable_pct` on the plan, which the dry-run shows as cash left over. A
symbol with no price is skipped with a reason rather than sized at a guessed
price, per the platform's no-fallback rule for live data. A negative computed
target is clamped to zero.

### Per-symbol margin: precheck over estimation

The account's leverage does not apply uniformly — some symbols are not
marginable at all. Three sources, in order of preference:

1. **Order-level precheck.** `AccountInterface.preview_order_impact(order) ->
   OrderImpact | None`. TastyTrade implements it with
   `Account.place_order(session, order, dry_run=True)`, whose
   `PlacedOrderResponse.buying_power_effect` gives the exact
   `change_in_buying_power` and `isolated_order_margin_requirement`, plus
   `fee_calculation.total_fees` and any `warnings`/`errors`. The base returns
   `None`. When a precheck is available the engine solves once, prechecks the
   resulting orders, and re-solves only if the precheck disagrees.
2. **Per-asset margin metadata.** `get_symbol_margin_info(symbols) -> dict[str,
   MarginInfo]`. Alpaca derives it from `Asset.marginable`,
   `Asset.maintenance_margin_requirement`, `Asset.fractionable`,
   `Asset.min_order_size` and `Asset.min_trade_increment`, combined with
   `TradeAccount.multiplier`. Deterministic before ordering, so Alpaca also needs
   only a single solve. Results are cached for the page's lifetime.
3. **Held positions.** For symbols already held, TastyTrade's
   `Account.get_margin_requirements()` returns per-symbol
   `initial_requirement` — the data behind its Cap Req screen — so the real rate
   is `initial_requirement / position_notional`.

Only when none of the three is available does a symbol fall back to the
conservative `bp_factor = account_multiplier` (assume no leverage), which
under-deploys rather than over-committing.

### The broker-agnostic account snapshot

`get_account_info()` returns a pydantic object on Alpaca, a dict on IBKR and
TastyTrade, and `None` on Alpaca auth failure. Its five call sites split into
attribute-readers and `.get()`-readers, so each is broken on the brokers it
wasn't written for — that is the `IncreaseInstrumentShareAction` bug above.

A new concrete `get_account_snapshot() -> AccountSnapshot` on
`ReadOnlyAccountInterface` returns a dataclass with `cash`, `equity`,
`net_liquidation`, `buying_power`, `non_marginable_buying_power`,
`margin_multiplier`, `is_margin_account`, `long_market_value`,
`short_market_value`, `pending_transfer_in` and `supports_fractional`. It must be
concrete rather than abstract: adding an `@abstractmethod` would break IBKR and
TastyTrade instantiation. The base implementation reads `get_account_info()`
tolerantly, in the manner of
`MarketExpertInterface._get_actual_available_balance` (`:815`); Alpaca and
TastyTrade override it properly.

Fixing `TradeActions.py:1493` to use the snapshot is in scope — it is a two-line
change to the only other percent-of-equity code in the repo, and leaving a known
`AttributeError` in place next to a feature that depends on the same data would
be indefensible.

### Data model

Five tables in `packages/common/ba2_common/core/models.py`, following the
`OptionActivity` conventions verified there: explicit snake_case `__tablename__`,
inline `foreign_key=... ondelete="CASCADE" index=True`, `DateTime.now(timezone.utc)`
default factories, uniqueness via `__table_args__`, and **plain `str` columns
rather than enums** (matching `OptionActivity.activity_type`, and avoiding the
SQLModel str-enum-stored-by-name migration trap).

- **`portfolio_allocation_config`** — `account_id` (unique), `valuation_mode`
  (`cost` | `market`), `allow_fractional`, `updated_at`. One row per account,
  created on first use with the defaults `cost` and `False`. This is page state
  that changes money, so it belongs in a table rather than in session storage or
  in the broker's settings.
- **`portfolio_allocation_label`** — `account_id`, `label`, `target_pct`,
  `sort_order`, `comment`. Unique on `(account_id, label)`. A row's existence *is*
  the "managed" flag, so the label selection needs no separate table.
- **`portfolio_allocation_symbol`** — `account_id`, `label`, `symbol`,
  `weight_pct`, `comment`. Unique on `(account_id, label, symbol)`. Rows are
  created lazily: a symbol with no row uses the even-split default.
- **`portfolio_income_event`** — `account_id`, `event_date`, `event_type`
  (`DEPOSIT` | `DIVIDEND`), `symbol`, `amount`, `external_id`,
  `consumed_amount`, `created_at`. Unique on `(account_id, external_id)`, which
  makes re-syncing idempotent exactly as `OptionActivity` does.
- **`portfolio_allocation_run`** — `account_id`, `mode` (`REBALANCE` |
  `INVEST_LABEL`), `scope_label`, `base_notional`, `available_buying_power`,
  `plan_json`, `filled_buy_value`, `filled_sell_value`, `order_ids`,
  `created_at`. The `plan_json` snapshot makes a dry-run reproducible after the
  weights change, and `filled_buy_value` — measured from the broker's fills after
  submission, not from the plan's intent — is what drives income consumption.

Two hand-written Alembic revisions, chained off `0a3e0bd24598` (the verified
head): first the instrument merge and unique index, then the five new tables.
Keeping them separate means the destructive data migration can be run, inspected
and if necessary re-run on its own, without the schema additions riding along.
`alembic/env.py` needs no change — its import of the
`ba2_trade_platform.core.models` shim registers the new tables automatically. The
new classes go into the `tests/conftest.py` import list for consistency.

Foreign keys are declarative only: the live DB runs with `PRAGMA foreign_keys =
0`, so account deletion must clean these rows explicitly, mirroring
`ui/pages/settings.py:1027-1037`.

Because `settings_export_import.py` only knows `AppSetting`, `AccountDefinition`
+ `AccountSetting` and `ExpertInstance` + `ExpertSetting`, allocation plans are
**not** covered by settings export. Adding them is out of scope and noted as a
known limitation.

## Behaviour

### Gating

`manual_trading_enabled` is a `bool` setting declared once in
`ReadOnlyAccountInterface._ensure_builtin_settings()`, so every broker inherits
it and the existing generic settings dialog renders and saves it with no UI work.
It must be read with
`get_setting_with_interface_default('manual_trading_enabled', log_warning=False)`
— `settings.get(key, default)` returns `None` for a never-saved key, because the
settings property seeds declared keys to `None`, so the default would never
apply.

The page shows an empty state, and nothing else, when:

- the global account selector is on "All accounts" — asks you to pick one;
- the account does not have `manual_trading_enabled`;
- the account has one or more enabled experts — names them, links to Settings.

Switching the global account hard-reloads the page (`ui/layout.py:124`), so all
edits persist eagerly rather than on a Save button.

### Default view

One `ui.expansion` per managed label. Each row is a symbol in that label with
current value (per the valuation mode), `% of label`, `% of total`, quantity,
cost basis, live price and market value;
symbols with no position show zero and are still editable. Label headers carry
the label total, its current versus target percent, and the label comment. A ⚠
marks symbols in more than one managed label.

Prices come from `account.get_instrument_current_price(symbols)`, which is bulk,
cached and works for symbols with no position. Alpaca's default feed is
`delayed_sip` — 15 minutes delayed — so the page states the feed next to the
refresh time.

`get_positions()` returning `None` means the fetch **failed** and `[]` means
genuinely flat. The page must distinguish them and refuse to compute a plan on
`None`, rather than silently treating a broker outage as a flat account.

### Managing labels and symbols

Pick managed labels from `get_all_instrument_labels()`, filtered to hide the
machine tags (`auto_added`, `expert_selected`, `ai_selected`, `not_found`, and
the `penny-N` / `tradingagents-N` / `fmprating-N` families), with a "show all"
escape hatch. Add and remove symbols on any label whether or not they are held,
via `add_label_to_instruments` / `remove_label_from_instruments`. Attach a
free-text comment to any label and any symbol.

Symbol normalisation happens inside the shared helpers rather than at the page
boundary — see "Instrument uniqueness" below.

### Instrument uniqueness

`instrument.name` has no unique constraint and no index. The live database holds
2477 rows under 2353 distinct names — **124 duplicate groups**. Because
`add_label_to_instruments` and `remove_label_from_instruments` resolve a symbol
with `.first()` while `get_labels_by_symbol` keys by name so the last row wins,
label writes on those 124 symbols land on an arbitrary row and may be invisible
to the next read. An allocation engine cannot be built on that.

The merge is unusually safe: **no table has a foreign key to `instrument`**
(verified against the live schema via `pragma_foreign_key_list`, and by grepping
for `foreign_key="instrument`). Every consumer resolves instruments by `name`;
the only `Instrument.id` use is the transient settings edit dialog
(`ui/pages/settings.py:3934`). So rows can be merged without repointing
anything.

A data migration, run before the new allocation tables are created:

1. For each duplicated `name`, keep the lowest `id`.
2. Coalesce `instrument_type` and `company_name` to the first non-null value in
   the group — in the live data the conflict is always one row holding `'STOCK'`
   and the other `NULL`.
3. Union the `labels` and `categories` JSON lists, preserving order and
   de-duplicating.
4. Delete the surviving group members.
5. Create `UNIQUE INDEX uix_instrument_name ON instrument (name)`, and set
   `unique=True` on the model field so the schema and the ORM agree.

The migration must be idempotent and must log the merge count, because it runs
against a 399 MB production database.

A unique index on `name` does not by itself prevent `aapl` and `AAPL`
coexisting, so uniqueness is only real if writes are normalised. All four label
helpers in `ba2_common/core/utils.py` and every instrument-creation path
(`InstrumentAutoAdder`, `JobManager`, the two Settings paths) normalise symbols
to `.strip().upper()`. This is a behaviour change to the shared helpers that
`ui/pages/overview.py` and `tests/test_instrument_labels.py` both exercise, so
both are updated with it. All 2477 live names are already uppercase, so the
migration itself has no case collisions to resolve.

### Income ledger

`get_cash_transfers(start_date, end_date) -> list[CashTransfer]` is a new
concrete seam on `ReadOnlyAccountInterface` returning `[]` by default. Alpaca
implements it from the `CSD`/`CSW` activity endpoint already used inline by
`get_balance_history` (`AlpacaAccount.py:4376`) plus the existing
`get_dividends` (`:4290`); TastyTrade from `get_history(types=["Money
Movement"], page_offset=None)`.

Each event is upserted on `(account_id, external_id)`. The ledger syncs when the
page loads and on explicit Refresh — never on a timer, so the page never issues
broker calls in the background.

The page shows the last 30 days, the open total, and a shortcut that opens the
wizard in `INVEST_LABEL` mode pre-filled with that amount. On submit, a run
consumes open events oldest-first up to its **net buy value**, defined as
`max(0, filled_buy_value − filled_sell_value)` — a rebalance funded
entirely by its own sells consumes no income. `consumed_amount` is written per
event, so an event can be partially consumed and its remainder stays open.

### The wizard

Both modes end in the same dry-run table.

**Rebalance** — all managed labels. Step 1 sets label percentages, validated to
total exactly 100%, with an "Even split" button. Step 2 sets symbol weights
within each label, defaulting to even. Step 3 shows the base breakdown
(`buying_power` + managed cost basis), a Refresh button, and the fractional
toggle. Step 4 is the dry-run.

**Invest into one label** — pick a label and an amount, pre-filled with
unallocated income. Buys only, no sells, split by that label's symbol weights.

The base is snapshotted when the wizard opens and only re-read on Refresh, so the
numbers cannot move mid-edit.

The dry-run table has one row per non-zero delta: symbol, side, quantity,
estimated value, buying-power cost, buying-power usage percent, and reasons
(`⚠ also in HighRisk`, `fractional`, `scaled ×0.61 to fit buying power`,
`⚠ not marginable`). Each row has a checkbox. Plan-level totals show sell value,
buy value, required versus available buying power, and estimated cash after.
Nothing is written to the database until Submit.

### Submission

Sells first, then buys once the sells are acknowledged. Within a run a symbol is
only ever a buy or a sell, so the wash-trade gate is not reachable from a single
plan.

Per symbol:

| Situation | Call |
|---|---|
| Held, target > 0, delta ≠ 0 | `TransactionHelper.adjust_quantity_with_tpsl(account, txn, qty_change)` |
| Held, target = 0 | `account.close_transaction(txn.id)` |
| Not held, target > 0 | new `TradingOrder` → `add_instance` → `account.submit_order(order)` |

Multiple open Transactions on one symbol are consumed FIFO. New orders are
`OrderType.MARKET`, `open_type=OrderOpenType.MANUAL`, `expert_id=None`, with a
comment carrying the run id. The comment must **not** contain the substring
`closing`: `AccountInterface.py:1531-1536` re-detects close orders by string
match on the comment.

`submit_order` returns a **truthy** order with `status == WASHTRADE_LOCKED` when
the gate fires, and `None` on hard failure with the reason left in the row's
`.comment`. The result must be inspected via `.status`, never via truthiness.

**Fractional fallback.** With the toggle on, the order goes in with a fractional
quantity, `good_for='day'` and `OrderType.MARKET` — Alpaca rejects fractional on
GTC or on any non-market type, and the default in `AlpacaAccount.py:940` is GTC.
If the order is still rejected, it is retried once at `floor(qty)` whole shares,
and the row reports which path succeeded. A floor of zero is reported as skipped,
not as a failure.

Results are written to `portfolio_allocation_run`, logged via `log_activity`, and
shown as a per-row outcome table. Partial failure is normal and reported per row;
it never rolls back what already filled.

## Adjacent fix: growth-chart label persistence

`OverviewTab._render_growth_by_label_charts` builds its "Labels shown" selector
at `ui/pages/overview.py:5453-5455` with
`default_labels = [l for l in labels if l != 'auto_added']`, and the selection is
lost on every reload. It persists to `app.storage.user` under a
`overview_growth_labels` key, seeded from the existing default when absent and
intersected with the currently available labels so a deleted label cannot break
the chart.

`ui/pages/symbol360.py:40` and `:165-179` are the precedent, including the
constraint that matters: `app.storage.user` raises `RuntimeError` outside a UI
context, so reads and writes must be guarded and must not happen from a thread
pool. The storage secret is already configured at `ui/main.py:173`.

This is unrelated to allocation, but it is the same label machinery and the user
asked for it alongside. Session storage is sufficient; no table.

## TastyTrade

`TastyTradeAccount` subclasses only `ReadOnlyAccountInterface` with
`supports_trading = False`. Every trading method is absent rather than stubbed.
There is no TastyTrade account in the live database, so none of this can be
verified against a real broker in this branch; it ships unit-tested against a
mocked SDK 12.0.2 and the user verifies it later on a machine with an account.

**Prerequisites.** Re-parent to `AccountInterface` and pin `tastytrade` in
`requirements.txt:4` — it is currently unpinned, and 12.x is the OAuth-only async
rewrite, so an unpinned upgrade moves the API under the feature.
`supports_trading` is read from the **class** at `ui/pages/settings.py:1435` but
from the **instance** at `TradeManager.py:921` and `:1223`; all three must agree.

**Implement.** `_submit_order_impl` (never override `submit_order` — overriding
it is exactly what disabled IBKR), `cancel_order`, `refresh_orders`,
`refresh_positions`, and broker-order → `TradingOrder` mapping, all shaped after
their `AlpacaAccount` equivalents. Then the feature seams:
`preview_order_impact`, `get_account_snapshot`, `get_cash_transfers`,
`get_symbol_margin_info`, and bulk quotes via `get_market_data_by_type` (limit
100 per call, so chunked).

**Fix.** Three silent pagination truncations — `get_orders` returns only the
first 50 rows, `get_filled_trades` and `symbols_exist` only the first 250 —
because they omit the `page_offset=None` "all pages" sentinel that `:342` and
`:418` pass correctly. `get_orders` also ignores its `status` filter entirely,
and `get_order` does a bare `int(order_id)` that raises on a non-numeric broker
id. `get_account_info` publishes no `buying_power` key, so the tolerant probe in
`MarketExpertInterface` silently falls through to `cash_balance` and margin
buying power is ignored. `get_positions` returns option positions alongside
equities with a multiplier-scaled market value, which would fold option notionals
into equity weights. `_run_async`'s hardcoded 30-second timeout turns a slow
paginated call into a silent empty result.

**Two SDK traps.** `place_order`'s `dry_run` parameter **defaults to `True`** —
every real call site must pass it explicitly. And `NewOrder.price_effect` is a
computed field derived from the *sign* of `price` (negative = debit), with
`abs()` applied on serialisation, so it must never be set by hand.

Out of scope: `modify_order`, TP/SL adjustment, complex orders, and the whole of
`OptionsAccountInterface`.

## Testing

**Pure unit tests** (`packages/common/tests/test_portfolio_allocation.py`) carry
the weight, because the engine is IO-free: even split, uneven weights, a symbol
in two labels summing, sell-to-target, close-to-zero, fractional on and off,
buying-power scale-down, mixed marginable and non-marginable, zero price, an
empty label, and weights that do not total 100%.

**Helper tests** extend `tests/test_instrument_labels.py` for
`get_symbols_by_label`, including a symbol carrying several labels and a label
with no symbols, and for symbol normalisation — `add_label_to_instruments(['aapl'],
…)` must find the existing `AAPL` row rather than create a second one.
`packages/common/tests/test_utils_pure.py:52-72` asserts the exported
pure-helper list and that the module imports nothing from the live tree — the new
helper must be added there and must not break the purity assertion.

**Migration tests** cover the instrument merge on a fixture database: two rows
for one name where one holds `instrument_type` and the other `NULL`, disjoint
label lists, overlapping label lists, and a name with three rows. The merge must
be idempotent — running it twice leaves the same result — and the unique index
must exist afterwards.

**Mocked broker tests** cover `AlpacaAccount.get_account_snapshot` against a
pydantic `TradeAccount` and `TastyTradeAccount.get_account_snapshot` against a
dict, the fractional whole-share fallback path, and the TastyTrade order
submit/cancel/refresh surface.

The suite is run per-file: the full run fails non-deterministically from a
pre-existing session leak, so per-file runs are the signal.

## Risks

**The live database is two revisions behind head** (`d5e1b9a3c842` versus
`0a3e0bd24598`), and `init_db()`'s `create_all` has already materialised tables
Alembic does not know about. `migrate.py upgrade` may fail with a duplicate
column error. Check `PRAGMA table_info` and consider `alembic stamp` before
adding the new revision.

**The instrument merge is destructive and irreversible.** It deletes 124 rows
from a 399 MB production database, and its `downgrade` cannot restore them. Take
a database copy before running it, and run the merge as a reporting dry-run
first so the affected names can be eyeballed.

**`InstrumentAutoAdder.py:96-101` appends to `existing.labels` in place** on a
plain JSON column with no `MutableList` wrapper, so SQLAlchemy records no history
and the subsequent commit emits no UPDATE. Every label the auto-adder tries to
add to an existing instrument is silently lost. Out of scope: the two-line fix
would start persisting thousands of expert labels and further pollute the label
list, which deserves its own decision. It is called out here because the
uniqueness work touches the same file, and because it explains why the live
label distribution looks sparser than the code suggests it should.

**`buying_power` shrinks as buys fill**, so a plan sized against a snapshot can
run out of room mid-submission. Buys are submitted in descending value order so
that a shortfall truncates the smallest positions, and each failure is reported
per row.

**Off-hours submission.** There is no `get_clock` or `extended_hours` handling
anywhere in `AlpacaAccount`; off-hours market orders simply queue until the open,
at prices that may differ from the dry-run. The dry-run states this rather than
blocking submission.
