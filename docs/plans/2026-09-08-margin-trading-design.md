# Margin trading (leverage) per account — design

Date: 2026-09-08
Status: implemented on feat/margin-trading (2026-09-08), pending merge to dev at a grid job boundary
Scope: live platform only. The backtest account keeps multiplier 1.0; a backtest
that needs leverage is run with a larger starting balance instead.

## Goal

Let an account trade more than its invested balance, capped by a per-account
factor. Example: balance 10k, factor 1.8, the platform deploys at most 18k of
value across that account's experts, never more than the broker allows.

## What exists today

- `get_balance()` on `ReadOnlyAccountInterface` returns equity. Unchanged.
- `get_account_snapshot()` already carries `buying_power`, `margin_multiplier`,
  `is_margin_account` from Alpaca and TastyTrade. IBKR fills `buying_power`
  only. Backtest reports `buying_power = cash` and no multiplier.
- `get_available_balance()` is an **expert** method
  (`MarketExpertInterface.py`). It is
  `get_virtual_balance()` (balance x `virtual_equity_pct`) minus the expert's
  open positions, then clamped to the broker's spendable figure by
  `_get_actual_available_balance`. Every sizing path (classic risk manager
  `TradeRiskManagement`, `SmartRiskManagerToolkit`, PennyMomentum, order
  validation in `AccountInterface`) reads it.
- Options: `OptionsAccountInterface.available_option_buying_power` is
  `get_balance()` minus the option reserve pool.
- The portfolio allocator budgets against broker `buying_power` through its
  own snapshot path. It is **not touched** by this change.

## Settings (builtin, all accounts)

Added to `ReadOnlyAccountInterface._ensure_builtin_settings`, so every adapter
and the account settings dialog pick them up without per-adapter code:

| key | type | default | rule |
|---|---|---|---|
| `margin_enabled` | bool | False | |
| `margin_factor` | float | 1.8 | refused at save when below 1.0 |

One pair for both asset classes. Stock and option results differ through the
per-class broker multiplier below. A separate option factor is a follow-up.

## Account interface additions (`ReadOnlyAccountInterface`)

### Multipliers

- `get_stock_margin_multiplier() -> float`
  - Alpaca: the account `multiplier` it already fetches for the snapshot.
  - TastyTrade: 2.0 when `margin_or_cash` says margin, else 1.0.
  - Backtest: 1.0.
  - IBKR: raises; the broker does not report one and nothing is fabricated.
- `get_option_margin_multiplier() -> float`
  - Base default 1.0: long options are cash-settled at both brokers.
  - An adapter overrides only when its broker reports a real derivative figure.

### Buying power

- `get_buying_power() -> float`: broker stock buying power from the snapshot.
  Raises `ValueError` naming the account when the snapshot has none.
- `AccountSnapshot` gains `option_buying_power: Optional[float]`, filled from
  Alpaca `options_buying_power` and TastyTrade `derivative_buying_power`,
  None elsewhere.
- No new cache. Alpaca's snapshot keeps its 5 s TTL and its invalidation after
  every submission. TastyTrade has no snapshot cache, so every read is fresh; the
  tradable-balance path therefore takes ONE snapshot per call and derives
  multiplier and buying power from it. A TastyTrade snapshot TTL cache is a
  follow-up if the extra round trip proves costly.

### Tradable balance

- `get_tradable_balance() -> float` (stock)
  - margin off: `get_balance()`.
  - margin on: `get_balance() x min(margin_factor, get_stock_margin_multiplier())`.
- `get_option_tradable_balance() -> float`
  - same formula with `get_option_margin_multiplier()`.
- A multiplier of 1.0 with margin on yields plain balance and logs one WARNING
  per class: margin enabled on a non-marginable account.
- A missing balance or multiplier raises. Nothing sizes on a guess.

Remaining broker BP is **not** subtracted here. The expert's
`get_available_balance` subtracts the expert's own positions and then clamps to
broker BP through the existing probe, so per-order safety is unchanged.

### Over-exposure warning

Broker BP is remaining capacity. With balance 10k, stock multiplier 2 and
factor 1.8, gross capacity is 20k and the platform intends to use 18k, so once
broker BP drops under 2k something has pushed exposure past the platform's own
ceiling (allocator, manual trades, another tool). Rule, evaluated when
`get_tradable_balance()` runs with margin on:

    warn when broker_bp < balance x (multiplier - margin_factor)

Message names account id, balance, multiplier, factor and broker BP. Same test
per class: stock against `buying_power`, option against `option_buying_power`.
When the option figure is None the check is skipped with a DEBUG line, never
computed on a guess.

## Consumers switched to tradable balance

Everything an expert sees goes through tradable balance; the point is to trade
above what was invested. Stock path:

- `MarketExpertInterface.get_virtual_balance`: base becomes
  `account.get_tradable_balance()`. This alone carries the factor into
  `get_available_balance`, the classic risk manager, the Smart Risk Manager
  toolkit, PennyMomentum and order validation.
- `TradeActions._virtual_equity`: the OPTION tradable balance x pct. It sizes
  option entries, which draw on option leverage (1.0 at every supported broker
  today), so it is NOT expected to equal the expert's stock virtual balance --
  with a stock factor of 1.8 the expert sees 18k and an option entry still sizes
  off 10k.
- Risk-per-trade equity input to `compute_risk_based_quantity` (classic RM and
  Smart RM ATR autosize) and `_validate_position_size_limits` (max position %)
  read tradable balance.
- `BalanceUsagePerExpertChart`: balance x pct per expert uses tradable, so a
  margin account does not paint experts as over-allocated.

Option path:

- `OptionsAccountInterface.available_option_buying_power`: base becomes
  `get_option_tradable_balance()` minus the reserve pool.
- Assignment capacity keeps reading cash; assignment settles in cash.

Stays on real equity:

- `minimum_equity_threshold_percent` / `has_sufficient_equity_for_trading`:
  a drawdown ratio against a starting baseline; scaling both sides by the same
  factor changes nothing.
- The portfolio allocator (already manages broker BP itself).

## UI

- Header, top right (`ui/layout.py`): cached header value gains `tradable`
  and `broker_bp` per account. Paint becomes `Balance $X / BP $Y` where BP is
  the broker's REMAINING buying power (`AccountSnapshot.buying_power`, every
  broker); the tooltip names it and the per-account breakdown lists balance,
  broker BP, stock tradable and option tradable. Same hourly refresh, stale and
  partial markers as today; a None for one account keeps the partial marker.
- Floating P/L per account card (`FloatingPLPerAccountWidget`, the subclass
  with the Balance column): new `BP` column showing the broker's remaining
  buying power, this platform's stock tradable ceiling in the cell tooltip,
  fetched in the same loop as balance.
- **Operator decision, 2026-09-08 — BP means the BROKER's number, not ours.**
  Both readouts first showed the stock tradable balance, on the reasoning that
  the platform's own sizing obeys it. A TastyTrade account with margin off then
  showed `$3,997.78 / BP $3,997.78` (tradable == balance) while holding ~$8k of
  positions its manual allocator had bought at 2x: an account already levered
  outside this platform's ceiling reads as untouched, and the capacity that was
  actually running out was invisible. The tradable figures stay one hover or one
  menu away, where they explain the badge instead of standing in for it.
- Live trades page (`LiveTradesTable`, `live_trades.py`): column label
  `Value / CapReq`, cell `$value / $capreq`, capreq = value / margin_factor
  when the account has margin on, else equal to value. Sort stays on value. The
  factor is read once per render from the account settings.
- Account settings dialog: the two builtins render themselves through
  `get_merged_settings_definitions()`; add the factor >= 1.0 validator.

## Error handling

- Missing broker BP, balance or multiplier raises. Callers already treat a
  missing available balance as "reject the order"; an exception lands in the
  job log instead of silently sizing to cash.
- Warnings (non-marginable account with margin on; over-exposure) are logged
  once per call. Throttle to once per snapshot TTL only if it proves noisy.

## Tests (`tests/`)

- Stub account: margin off returns balance; margin on returns
  balance x factor; broker multiplier below factor wins; multiplier 1.0
  returns balance and warns; missing BP/multiplier raises; factor < 1.0
  refused.
- Over-exposure warning fires at exactly `balance x (mult - factor)` and not
  above it; skipped with option BP None.
- `_virtual_equity` = option tradable balance x pct (and NOT the stock virtual
  balance: reading the stock figure would size every option entry by the stock
  factor).
- `available_option_buying_power` uses the option tradable balance.
- Header combine and live-trades cell formatting, margin on and off.

## Versioning

Shared package (`ba2_common`) and trade app both change: bump
`testplatform/version.py` and `ba2_trade_platform/version.py` before push.

## Follow-ups (not in this change)

- Bring the portfolio allocator's own sizing under the margin factor. The
  account-wide *ceiling* is now ENFORCED when margin is on (commit d6c1028f):
  an expert-side clamp, an entry gate and a per-account submit lock cap total
  marked stock exposure. The allocator still computes its own sizes outside
  the factor; it is only prevented from exceeding the ceiling.
- Separate option margin factor.
- Backtest account leverage (multiplier > 1 with the Reg-T model it already has).
- TastyTrade snapshot TTL cache.
- Header / Floating P/L card / live trades: derive tradable figures from the one
  snapshot (TastyTrade: `get_balance()` is its own REST call, so today's cost is 3
  calls per account per card render, 2 per tradable-balance read).
- Memoise merged settings definitions per class (`get_merged_settings_definitions`
  rebuilds on every unset-key read; ~3.5 µs per call in the backtest sizing loop).
- `OptionRiskManagement.sleeve_equity` stays on snapshot equity; revisit if an
  adapter ever reports option leverage > 1.

Deferred items from the 2026-09-09 margin review fixes (see
`docs/plans/2026-09-09-margin-review-fixes-plan.md` for the full list):

- Live margin-on round-trip cost: one `submit_order` now takes about three
  snapshots and two pending-order queries under the per-account lock (position
  size validator, expert headroom clamp, exposure gate), and `describe_capital()`
  adds one snapshot plus one order scan per sizing decision. TastyTrade's
  snapshot is an uncached REST call. Compute the StockExposure breakdown once per
  submit and thread it through.
- `tradingorder.account_id` and `depends_on_order` are not indexed; the
  pending-entry query runs per sizing decision against the full orders table
  (live, margin on).
- The per-account submit RLock is the one piece of that work reachable with
  margin off. It serialises only; results are unchanged, and GA trial threads
  share the backtest account id's lock.
- IBKR plus `margin_enabled` remains the documented unsupported path: no stock
  multiplier, so it refuses at the multiplier step.
- `_validate_account_exposure` catches only `ValueError`; broker exceptions from
  `get_instrument_current_price` propagate, which is house style under
  `BA2_ERROR_MODE=enforce`.
