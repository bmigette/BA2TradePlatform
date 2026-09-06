## 1. LOOKAHEAD

### 1. Critical — Next-bar fills are booked before the simulated clock reaches the fill bar
**Files / lines**
- `testplatform/backend/app/services/backtest/backtest_account.py`: **4426–4430, 4585–4587, 4708–4717**
- `testplatform/backend/app/services/backtest/price_source.py`: **826–836**

`_bar_for_fill()` reads the next bar, but `_apply_fill()` immediately changes cash, positions and order status and records the supplied **current** `as_of` as the fill date. Future execution results become current account state.

**Concrete failure:** At January 2, a pending buy for one share sees January 3’s open of $100. It immediately debits $100 and creates the position dated January 2. Subsequent January 2 account reads already include a position whose execution—and execution price—were not yet knowable.

### 2. High — Same-bar orders can execute retrospectively against the bar’s earlier range/open
**File / lines:** `testplatform/backend/app/services/backtest/backtest_account.py`: **4428–4429, 4495–4499, 4589–4603**

Under `same_bar_close`, limit/stop evaluation uses the entire current bar and its open, without checking whether the order existed when those prices occurred.

**Concrete failure:** A bar has open=$100, low=$90, close=$120. After observing its close, a strategy submits a BUY_LIMIT at $110. Evaluating it on that bar returns a $100 fill through `_gap_limit_fill()`: the strategy buys at an opening price that occurred before its decision.

### 3. High — Senate still-held gates use sales before their disclosure
**File / lines:** `packages/experts/ba2_experts/FMPSenateTraderWeight.py`: **1176–1186, 2059–2066, 2120–2130, 2177–2187, 2202–2203**

The still-held calculation receives the **unwindowed** feed. Its index retains execution dates but discards disclosure dates; both individual holdings and holder counts are evaluated by execution date alone.

**Concrete failure:** A politician’s purchase is publicly disclosed January 5. They sell January 10 but disclose the sale February 10. On January 15, `require_still_held=1` removes their purchase because the future disclosure feed already contains the January 10 sale. At January 15, that sale was not public.

### 4. High — Senate skill’s forward price walk can cross the as-of ceiling
**File / lines:** `packages/experts/ba2_experts/FMPSenateTraderWeight.py`: **1803–1810, 1907–1925, 1931–1935**

The eligibility cutoff constrains the **requested** horizon date, not the date ultimately found by `_price_on_or_after()`. The walk has no `now` ceiling and returns no observation date for the caller to validate.

**Concrete failure:** At July 3, 2023, a disclosed June 30 purchase qualifies with `horizon_days=3`. Its entry open is $100. The symbol has no July 3 bar, July 4 is a holiday, and July 5 opens at $120. The scorer uses July 5’s price on July 3 and records a +20% winning trade.

In this supplied revision, `_get_price_at_date()` itself is an **exact-date lookup** at line **1489**; the forward walk is in `_price_on_or_after()`.

### 5. High — Confidence admits future executions when disclosure dates are missing or malformed
**File / lines:** `packages/experts/ba2_experts/FMPSenateTraderWeight.py`: **714–720, 2418–2425, 2464–2471, 2545–2555**

The history slice explicitly keeps rows with missing/unparseable disclosure dates. Confidence then applies only a lower execution-date bound, both in its prefix-index and full-scan paths. There is no upper execution-date ceiling.

**Concrete failure:** At January 15, a trader’s known yearly buys are $1,000 in X and $99,000 elsewhere: X focus is 1%. Their cached history also contains a February 1 X purchase of $900,000 with a missing disclosure date. That row survives slicing and enters confidence’s totals, raising X focus to the configured 10% cap and changing its signal weight and confidence.

This is a confidence-input leak; the separate consensus bonus at **2980–2983** counts filtered signal trades, so those lines do not establish an independent future-disclosure leak.

### 6. Medium — OHLCV slicing drops timezone offsets rather than converting them to UTC
**File / lines:** `testplatform/backend/app/services/backtest/price_source.py`: **874–885, 1102–1106**; clamp comparison at **909–913**

`_to_utc()` returns an already-aware datetime unchanged. `_slice()` then strips its timezone with `replace(tzinfo=None)`, interpreting its local clock time as UTC.

**Concrete failure:** The simulated clock is 14:00 UTC. A caller requests `end_date=15:00+02:00`, which is 13:00 UTC. The wrapper correctly sees that this does not exceed the clock and leaves it unchanged. `_slice()` instead cuts at **15:00 UTC**, returning a cached 14:30 UTC bar—future information relative to the 14:00 simulated clock.

## 2. SILENT FAILURE

### 7. High — Option no-arbitrage gate fails open when required inputs are unavailable
**File / lines:** `testplatform/backend/app/services/backtest/backtest_account.py`: **2524–2530, 2550–2555**; acceptance at **2355–2376**

Missing contract terms or underlying spot produce only a DEBUG message and `None`, which the caller interprets as “no rejection.”

**Concrete failure:** A call entry has strike=$100, premium=$0.01 and sufficient option-bar volume, but its underlying bar is missing. Its true underlying price is $150. The guard permits the fill without establishing intrinsic value, rather than refusing or raising because it cannot validate the premium. The account can acquire an option worth at least $50 for $0.01.

### 8. High — Missing quantities become zero-quantity FILLED orders
**File / lines:** `testplatform/backend/app/services/backtest/backtest_account.py`: **4648, 4708–4714, 4729–4739**; option gates at **2475–2477, 2607–2609**

Both fill paths convert `quantity=None` to `0.0`, charge commission and mark the order FILLED. The option participation and cash-cap checks also let this missing quantity pass.

**Concrete failure:** An accepted option order has `quantity=None`, a valid $2 premium and $1 commission. It passes both gates, deducts $1, records `filled_qty=0` and sets `status=FILLED`, although its required contract count was never known.

### 9. High — Missing valuation prices are replaced with entry prices, including forced buybacks
**File / lines:** `testplatform/backend/app/services/backtest/backtest_account.py`: **486–492, 589–617, 1611–1622**

Unresolvable prices are replaced with entry prices or other estimates without rejecting the missing-price state. The liquidation path goes further and executes a cash settlement at that fallback.

**Concrete failure:** A held short option was sold for $1. During a margin breach, its premium bar, usable IV and underlying spot are all unavailable. `_liquidate_option_lot()` substitutes `lot.avg_price=$1`, debits $100 for one standard contract and closes the lot. It records a break-even premium buyback without knowing any executable current price.

### 10. High — Non-positive sizing caps are ignored
**File / lines:** `packages/common/ba2_common/core/position_sizing.py`: **125–139**

A zero notional ceiling does not enter the notional-cap branch. A negative available balance does not enter the cash-cap branch. Both computed constraints therefore disappear instead of restricting quantity.

**Concrete failure:** With equity=$10,000, price=$100, stop=$90 and risk=1%, the initial quantity is 10 shares. Supply `max_position_value=0` and `available_balance=-1`: the function still returns **10**, despite a zero permitted notional and no spendable cash.

### 11. Medium — Insider data failures become ordinary observations and recommendations
**File / lines:** `packages/experts/ba2_experts/FMPInsiderClusterBuy.py`: **51, 185–186, 233–257**

A non-dict provider response becomes an empty transaction history. Separately, missing transaction values become zero, including sales that should reduce confidence.

**Concrete failures:**
- `insider_get()` returns `None` after a data failure: the expert reports an ordinary HOLD with zero buyers rather than surfacing the unavailable history.
- Three named insiders purchase $100,000 each, and an open-market sale has `value=None`: with the default cluster thresholds, the expert emits BUY at **56.5% confidence**, treating the unmeasurable sale as $0.

### 12. Medium — Cache read errors are erased and can cause a symbol to be dropped
**File / lines:** `testplatform/backend/app/services/backtest/price_source.py`: **1023–1028, 1050–1056, 632–644**

Every parquet/path-resolution exception is converted to `None`, then reclassified as a cache miss. The preload tolerance can subsequently discard the symbol.

**Concrete failure:** A 200-symbol run has one existing parquet file that raises `PermissionError`. `_read_cached_df()` suppresses the error; preload treats the symbol as absent and, under the supplied default tolerance, continues with 199 symbols. The actual storage error is never raised, and the requested universe changes.

## 3. LIVE/BACKTEST ASYMMETRY

### 13. High — Senate confidence and skill use monthly live caches but daily backtest caches
**File / lines:** `packages/experts/ba2_experts/FMPSenateTraderWeight.py`: **1752–1757, 1788–1793**

The same date-dependent calculations receive different cache freshness rules based on `is_live`. Sliding windows and newly completed skill horizons can change without any change in history length.

**Concrete failure:** The unchanged history contains X buys of $9,000 on January 10, 2023 and $1,000 on December 1, plus $90,000 of other December buys. A live confidence calculation on January 1, 2024 caches X focus=10%. On January 11, the older X purchase has left the yearly window, but live returns the monthly cached 10%. A backtest calculation for January 11 recomputes approximately **1.10%**, changing focus-weighted decisions and confidence.

### 14. High — The Senate price-map cache freezes live execution-price coverage
**File / lines:** `packages/experts/ba2_experts/FMPSenateTraderWeight.py`: **1471–1489**

The module-level price projection has no live bypass or expiry. Once a symbol is cached, later live calls never reach the fresh-fetch path unless it is evicted or explicitly cleared.

**Concrete failure:** A live process first loads X’s history on January 2. On January 10 it receives a newly disclosed purchase executed January 8. The cached projection has no January 8 entry, so `_get_price_at_date()` returns `None` and **2302–2304** drop the trade. A historical backtest using a fully populated history includes that same purchase and can recommend BUY.

### 15. Medium — Earnings’ live-only calendar shortcut can suppress a signal that backtest produces
**File / lines:** `packages/experts/ba2_experts/FMPEarningsDrift.py`: **48–53, 81, 282–313**

Only live consults the bulk calendar and treats an absent symbol as conclusive evidence to skip the detail fetch. The calendar is cached for four hours; backtest always uses the per-symbol history.

**Concrete failure:** At 08:00, the calendar is cached without X. At 09:00, X reports a qualifying EPS beat, and the per-symbol earnings endpoint contains it. At 09:30, live still sees X absent from the cached calendar and emits HOLD. A backtest at that time takes the detail path, receives the qualifying report and emits BUY.
