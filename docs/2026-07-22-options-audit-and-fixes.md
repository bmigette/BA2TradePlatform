# Options Audit & Fixes — 2026-07-22

Scope: end-to-end audit of the `ba2-test` launcher, the backtest option strategies it
generates, and the option-trading implementations in `ba2_common` (actions, conditions,
evaluator, margin), the backtest stack (`options_provider` / `backtest_account` /
`daily_engine`), and the live Alpaca option paths. Method: full static trace, then
runtime proof via failing regression tests, then fixes. All fixes verified by the live
pytest suite (`tests/`, 1031 passed) and the backend backtest suite
(`testplatform/backend/tests/backtest/`, 413 passed incl. the parity gate).

## Fixed bugs

### B1 — `profit_loss_percent` / `profit_loss_amount` conditions mispriced option positions
- **Symptom:** a long call (premium 4.20, underlying 190) evaluated `profit_loss_percent > 50`
  as **True** with `calculated_value = +4423.8%` on a flat position — any option take-profit
  fired on the first manage cycle; credit strategies showed huge fake losses. Runtime proof:
  `tests/test_option_profit_loss_condition.py`.
- **Root cause:** the conditions compared `account.get_instrument_current_price(underlying)`
  against `transaction.open_price` (the option *premium*), with no contract multiplier.
- **Fix:** option-aware P&L path in `packages/common/ba2_common/core/TradeConditions.py`
  (`_get_option_pnl_via_transaction` + `_get_pnl_for_condition` dispatcher) and a new
  `TransactionHelper.calculate_option_pnl` (`packages/common/ba2_common/core/TransactionHelper.py`).
  Option positions are marked from `account.get_option_quote(contract_symbol)` — long at bid,
  short at ask, `last` fallback, decline-to-evaluate when no quote/multiplier (no fabricated
  prices). P&L uses `transaction.multiplier` (×100). Equity path is byte-for-byte unchanged.
- **Tests:** 10 in `tests/test_option_profit_loss_condition.py` (direction, bid/ask marking,
  fallback, missing-quote decline, multiplier scaling).

### B2 — O_CC / O_PP overlay rules re-fired every manage cycle (position stacking)
- **Symptom:** two manage cycles produced 2 FILLED `sell_to_open` covered calls against
  100 shares (coverage for 1 → 1 naked). Same for protective puts. Runtime proof:
  `tests/test_covered_call_overlay_stacking.py`.
- **Root cause:** `_build_strategy_covered_call` / `_build_strategy_protective_put`
  (`testplatform/ba2test_launcher.py:2135,2166`) gated the overlay only on `has_position`.
- **Fix:** each builder now emits a `stop_processing` guard rule *before* the overlay rule —
  `has_covered_call` (O_CC) / `has_protective_put` (O_PP) — the codebase's negation idiom
  (rules evaluate in order; guard halts the ruleset when an overlay already exists). Also
  registered the three option-position flags in `rule_builders.FLAG_FIELD_EVENT`
  (`packages/common/ba2_common/core/rule_builders.py`) — without it the guard field was
  silently dropped and an empty-trigger rule evaluates always-true.
- **Tests:** 4 in `tests/test_covered_call_overlay_stacking.py` (2 runtime through the real
  evaluator + engine rule conversion, 2 static AST checks on the launcher source).

### B3 — multi-leg parent `open_price` ignored leg ratios
- **Root cause:** `_fill_multi_leg_parent` (`backtest_account.py`) computed
  `parent.open_price = Σ sign×premium` without `ratio_qty` weights — a 1-2-1 butterfly got
  5.5 instead of the true 1.5 net premium.
- **Fix:** ratio-weighted net (`Σ sign × premium × ratio`, ratio from leg/parent quantity
  after the cash-cap rescale). Ratio-1 shapes (verticals, condors) numerically unchanged.
- **Tests:** `test_option_fills.py::test_butterfly_parent_open_price_is_ratio_weighted`,
  `..._put_ratio_parent_open_price_is_ratio_weighted`.

### B4 — backtest sizing/limit prices used stale start-date premiums
- **Root cause:** `HistoricalOptionsProvider` overlaid per-date greeks from premium bars but
  kept `bid/ask/last` from the chain snapshot, which `fetch_options` writes only for the
  run's start date.
- **Fix:** `options_provider.py` — new `_pit_quotes`: when a per-date bar exists,
  `last = bar.close` and the snapshot's absolute spread is preserved around it
  (`bid = close − spread/2`, `ask = close + spread/2`; missing spread → bid/ask stay None,
  nothing fabricated). No-bar dates keep chain-row quotes. `get_quote` synthesizes
  identically (O(1)-amortized chain-row lookup), so entry and close actions see consistent
  point-in-time premiums.

### B5 — option limit fills ignored `limit_price`
- **Root cause:** `_option_fill_price` (`backtest_account.py`) filled at bar open/close
  ± slippage regardless of the limit.
- **Fix:** LIMIT option orders fill only on a cross (buy: `px <= limit`; sell: `px >= limit`),
  at the bar price when better than the limit, else stay pending and retry next bar — mirroring
  the equity path. Leg children (limit held on the parent) and MARKET orders unchanged.
- **Tests:** `test_option_fills.py::test_limit_buy_above_market_does_not_fill_then_fills_on_cross`,
  `..._limit_sell_...`.

### B6 — `naked_margin_per_contract` understated ITM short margin
- **Root cause:** `max(0.20·spot − |spot−strike|, 0.10·spot)·100` used `abs()` as the
  "OTM amount", which *reduces* margin for ITM shorts. Reg-T is
  `premium + max(20%·underlying − OTM, 10%·underlying)` with OTM = 0 for ITM
  (CBOE Margin Manual / IBKR). Spot 100 / short put K150: was 1000, correct ≥ 2000.
- **Fix:** `OptionsAccountInterface.naked_margin_per_contract` gained a **required**
  `option_type: OptionRight` kwarg; OTM is direction-aware (CALL: `max(strike−spot, 0)`,
  PUT: `max(spot−strike, 0)`). `option_reserve_required` requires `option_type` for
  single-sided naked strategies (`ValueError` if missing); `short_straddle` reserves
  `max(CALL, PUT)` (Reg-T greater-leg rule). All callers updated (`TradeActions.py` short
  straddle/strangle, jade lizard, put ratio; `backtest_account.py` maintenance margin is now
  right-aware per short lot, worst-case + loud warning when the right is unresolvable).
- **Tests:** `test_new_option_actions.py::test_naked_margin_reg_t_direction_aware` + updated
  assertions (each commented) in `test_new_option_actions.py` and `test_options_review_fixes.py`.

### B7 — backtest `get_atm_iv` was a chain-wide mean, not ATM IV
- **Root cause:** `options_provider.get_atm_iv` averaged IV across all ~10–16k snapshot
  contracts (skew/expiry-contaminated), while live `AlpacaAccount.get_atm_implied_volatility`
  picks the nearest-strike contract in a 20–45 DTE window → `iv_rank` parity break.
- **Fix:** rewritten to mirror live: single near-ATM contract in the 20–45 DTE window.
  The cache stores no underlying price, so ATM is proxied by |delta| nearest 0.50 among
  calls (deterministic tie-break; documented). Also stops scanning the full chain.
- **Tests:** `test_options_provider.py` (7 new) + updated
  `test_backtest_account_options.py::test_get_atm_implied_volatility_reads_provider`.

## Known remaining findings (documented, not fixed here)

- `fetch_options.build_cache` accepts `feed` but never passes it to `OptionBarsRequest`
  (dead CLI flag); chain-row premium fallback `(on_start or bar_rows[0])` contradicts its
  docstring and mixes T (from start) with a possibly later premium in the chain-row IV.
- `record_atm_iv` has no live caller (only tests) — the Phase-2 daily recording job never
  landed, so `iv_rank` cannot fire live until a JobManager job is scheduled.
- `min_open_interest` filter is unusable in backtest (cache OI always None → rejects all).
- Contract discovery is as-of-now (mild lookahead; fills on not-yet-listed contracts raise
  `OptionsCacheMiss` instead of skipping).
- Margin-call liquidation picks the largest lot by contract count, not margin dollars freed.
- A multi-leg parent's own net `limit_price` is still not enforced in `_fill_multi_leg_parent`
  (B5 covered single-leg fills only).
- Live Trades page P&L display for option transactions still uses the equity formula
  (`calculate_pnl` with the underlying price) — conditions are fixed, the UI display is not.
- Spread parent orders (asset_class OPTION, no `contract_symbol`) deliberately fall through
  to the legacy P&L path in the conditions.
- Backend suite has 25 pre-existing, non-options failures (environment: tsai/chronos/
  websocket/migration) — unchanged by this work.

## Verification

- Live suite: `.venv/Scripts/python.exe -m pytest -q` → **1031 passed** (baseline before
  fixes: 1019 passed, 4 failed — the 4 being the bug-proof tests).
- Backend backtest suite: `cd testplatform/backend && pytest tests/backtest -q` →
  **413 passed, 1 skipped**; parity gate `test_parity_golden.py` 3/3 before and after.
- `packages/common/tests/`: 164 passed (run separately; duplicate-`conftest` import clash
  if combined with `tests/`).

---

# Follow-up: OS1 unrealistic-profit investigation (2026-07-23)

Runs 765–769 (`TOPn-optm-FMPRating-OS1-pricetarget-v2`) showed $20k → $7.4M–18.6M
(+37,000% to +92,000%). Root-caused against the DB, options cache, and external price
data (underlying closes cross-verified against Yahoo Finance — the cache was correct):

## Root causes

1. **Exercised ITM long calls became unmanaged buy-and-hold stock (67–85% of final
   equity in every run).** Expiry physically exercised supportable ITM longs → stock at
   strike; the option ruleset's `close_option` exits don't manage stock, so positions
   rode the 2024→2026 rally to end-of-run (AMD @141→581, MU @88→1154, GEV @282→1175).
   Also a reporting double-count: the exercised leg booked option P&L at intrinsic while
   the same intrinsic persisted as stock MTM (cash/equity were correct; round-trip
   reports summed it twice).
2. **Arbitrage-inconsistent option prints (indicative feed).** AZN 105C bought at $0.01
   with spot $159.85 (intrinsic $54.85) → +$1.53M fabricated (49% of the run's closed
   P&L). No engine check compared premium to intrinsic.
3. **No liquidity constraint.** 2,100 contracts filled at a V=1 print; 281 at V=12.
4. Compounding percent-of-equity sizing amplified all of the above.

## Fixes (all in `testplatform/backend/app/services/backtest/backtest_account.py` unless noted)

- **Expiry policy** — long ITM options now SELL-TO-CLOSE at expiry premium (intrinsic
  fallback), never exercise → no orphaned stock. Short-option assignment still creates
  stock at strike, but it is now *always* liquidated at the next bar open (previously
  only when cash went negative, and only partially). (`settle_single_leg_expiry`,
  `process_pending_assignment_liquidations`)
- **Arb-consistency guard** — `_ARB_FILL_TOLERANCE = 0.05`: entry fills with
  `premium < intrinsic − tol` are rejected (retry next bar; counter
  `rejected_arb_fills`); exit fills with call `premium > spot + tol` / put
  `premium > strike + tol` rejected. Spot taken from the fill-day underlying bar
  (open for next_bar_open, close for same_bar_close). Fail-open when terms/spot
  unavailable. Any failing leg blocks the whole multi-leg combo.
- **Volume participation cap** — `_OPTION_FILL_MAX_VOLUME_PARTICIPATION = 0.10`: an
  order (per leg for combos, all-or-none) fills only when required contracts ≤ 10% of
  the fill bar's volume; else retry next bar. None/0 volume → no fill. Counter
  `rejected_illiquid_fills`.
- **`--feed` flag wired** (`fetch_options.py`) — `build_cache` now actually passes the
  feed to Alpaca via `_FedOptionBarsRequest` (alpaca-py 0.43.4's `OptionBarsRequest`
  has no `feed` field and silently drops unknown kwargs — the subclass + `OptionsFeed`
  enum mapping is validated to emit `feed` in the wire params). Values: `indicative`
  (default, unchanged), `opra`. **Action needed:** rebuild the options cache with
  `--feed opra` (requires an OPRA-capable Alpaca subscription) to get trades-based
  bars + real volumes, then re-run OS1.

## Tests

- New: `test_option_orphan_stock_and_arb_guards.py` (8), `test_fetch_options_feed.py`
  (6), `test_option_volume_participation.py` (6).
- Fixtures updated where old test data was itself junk (arb-inconsistent premiums) or
  oversized vs the 10% cap — assertions kept, data made realistic.
- Backtest suite: 435 passed + parity gate 3/3. Live suite: 1031 passed.

## Note on universe

Run 765 traded 67 underlyings — all large/mega-caps (AAPL, MSFT, JPM, …). The
illiquidity was at the **option-contract** level (deep-OTM/short-dated strikes with
single-digit daily volume), not the underlying level — which is why the volume cap
matters even for a large-cap-only universe.

---

# Follow-up 2: open-at-end recorder fix + fitness/exit realignment (2026-07-23)

After the artifact removal above, the v6 OS runs (780–794, `TOPn-optm-FMPRating-OS{1,2,3}-pricetarget-v6`)
showed anemic results: OS1 TR −7.7..+18.5% on 2–27 trades, OS2 TR 3.8–18.1% (n=8–16, wr 56–87%),
OS3 TR 3.6–5.9% (n=12–27). Investigation found one more reporting bug plus a scoring/strategy
misalignment — the low profits were not (only) "the strategy has no edge".

## B8 — open-at-end option trades recorded the UNDERLYING price as exit (reporting-only)

- **Symptom:** run 782, NVDA260710C00227500 (10 contracts @ $0.50, opened 2026-06-23, still open
  at run end): recorded `exit_price=200.09` (= NVDA spot) vs the real premium close $0.10 →
  fantasy `pnl=+199,589`, `best_trade=912.95%`. The equity curve was CORRECT (final $21,442
  reconciles to the dollar, incl. the real $400 unrealized loss) — only the trades table and
  derived stats (`best_trade`/`avg_trade`/`expectancy`) were polluted.
- **Root cause:** `BacktestAccount.get_round_trip_trades()` (`backtest_account.py`) marked
  open-at-end transactions with `exit_px = self._price.close_at(opening.symbol)`; for option
  orders `symbol` is the underlying.
- **Fix:** option legs now mark at the contract's premium close via the same
  `self._options.get_bar(contract_symbol, self._as_of_date())` path the equity curve's option
  MTM uses; no premium bar → entry premium (breakeven, logged warning), never the underlying
  close. Stock path byte-for-byte unchanged.
- **Tests:** `test_round_trip_trades.py` —
  `test_option_open_at_end_marks_at_premium_close_not_underlying` (verified failing pre-fix:
  obtained 185.0, expected 1.6) and
  `test_option_open_at_end_without_premium_bar_falls_back_to_entry`.

## Fitness misalignment — why the GA converged to barely-trading configs

DB check (`strategy_optimizations`): **every** OS optimization (v1→v6) ran
`fitness_metric='calmar_ratio'`. Calmar has no trade-count floor and no consistency term, so on
honest (post-artifact) data the search minimized drawdown by *not trading* — v6 best_fitness was
0.006–0.057 with 2–27 trades over ~1.5y, and the param diff v2→v6 showed the GA disabling the
O_LC/O_LP entries outright, risk 7→2.5%, max/instrument 20→5%.

The literal "≥30%/yr every year" metric already existed but was never used for options:
`consistent_annual_return` (`app/services/strategy_fitness.py`) = annualized return ×
dd_guard (soft cap 20%) × worst-year/mean-year consistency × trade gate (ramps to ≥30
trades/yr). Changes (`testplatform/ba2test_launcher.py`):

- **`_resolve_fitness()`** — pure-option kinds (`_PURE_OPTION_STRATEGIES`: the 14 `O_*` option
  entries + `OS1`–`OS4`) now default to `consistent_annual_return` when `--fitness` is not
  passed. **Stock scope is untouched**: equity kinds keep each command's historical default
  (`sharpe_ratio` for `optimize`, `calmar_ratio` for `optimize-batch`), the equity-entry
  `O_CC`/`O_PP`/`O_STK` are explicitly excluded, and an explicit `--fitness` always wins.
- **`_option_exit_rules()` debit/credit split** — debit kinds (OS1/OS4 + members: long premium)
  get a wide take-profit band (default +100%, range 25–200%, step 25; was capped at 25–75%) —
  long premium lives off the right tail, and a ≤75% cap truncates the few winners that pay for
  the many small losers. Time-exit band widened to 10–45d (default 28). Credit kinds
  (OS2/OS3 + members) keep the tastytrade-style 25–75% TP band and gain a **toggleable
  stop-loss** (`opt_sl`: close at −100% of credit, range −200..−50, step 25) so the GA can
  manage the short-premium left tail (v6 OS2/OS3: 56–87% win rate but 3.8–18% TR = small wins
  eaten by uncapped losers).

Related, landed in the same window: `5b1632c` fixed the entry equity-gate mispricing option
positions as their underlying stock (`Transaction.open_price` seeding) — root-caused to the
same "OS1 traded almost nothing" symptom.

## On the OPRA feed (why it matters, and what it won't do)

Rebuilding the options cache with `--feed opra` is **not** expected to raise the P&L — expect
the opposite or a reshuffle. The indicative feed's quote-derived prints are where the junk
lived (B/$0.01-with-$54-intrinsic prints); the arb guard now *rejects* those, so on indicative
data the GA optimizes a market where some wanted trades never fill, and can still learn stale
prints that pass the guard — edges that cannot exist live. OPRA (consolidated trades + real
volumes, needed by the 10% volume-participation cap) makes a 30%/yr backtest claim
*believable*, not bigger. If OS1–3 can't reach 30%/yr on OPRA data, that's the honest answer
and the strategy — not the metric — needs changing. **Action still needed:** rebuild the cache
with `--feed opra` (requires the OPRA-capable Alpaca subscription) before the next OS runs.

## Verification (this round)

- Backend backtest suite: **438 passed, 1 skipped** (incl. parity gate `test_parity_golden.py`
  3/3) — 436 baseline + 2 new B8 tests.
- Live suite: **1031 passed**.
- Launcher smoke: `optimize`/`optimize-batch --help` render; `_resolve_fitness` matrix and
  `_option_exit_rules` bands verified per kind; `OS1`/`OS2` strategy builds produce
  `[opt_tp, opt_time]` / `[opt_tp, opt_time, opt_sl]`.

## Honest expectation

These changes align the *search objective* with the 30%/yr goal and stop the GA from
"winning" by not trading; they cannot manufacture edge on honest data. Next OS runs (v7, on
CAR) should show ≥30 trades/yr configs with year-consistency pressure — if the best CAR
fitness still maps to single-digit annual returns, the FMPRating→options entry signal itself
is the bottleneck, and the next lever is the entry universe/signal (e.g. event-driven
candidates), not further exit tuning.

---

# Follow-up 3: B9 — multi-leg structure P&L was garbage (2026-07-24)

The v7 runs (CAR fitness): OS1 (233) evaluated **307 individuals, 0 positive**; OS2 (234)
165/269 positive but best CAR only 0.136. Root-caused to one more shared engine bug plus an
economic reality check.

## B9 — profit_loss_* conditions on multi-leg parents fell through to the equity path

- **Root cause:** `_get_pnl_for_condition` (`packages/common/ba2_common/core/TradeConditions.py`)
  routed option orders to the option-aware path only when they had a `contract_symbol`.
  Multi-leg PARENT orders (asset_class OPTION, no contract_symbol — the contract lives on the
  child legs) fell through to the legacy equity path: `calculate_pnl(transaction,
  underlying_price)` vs the parent's NET-PREMIUM open_price (~$3.75 debit / ~$2.50 credit on a
  ~$190 stock) → ~+4,900% for BUY parents, ~−7,500% for SELL parents.
- **Runtime proof (not just code):** run 759 (OS1-v7 TOP4) had `opt_tp` enabled at **75%**,
  time exit disabled — yet 8 of 13 structures closed after **1 bar** at real net P&L
  +0.7…+11.7% (impossible legitimately). Run 758 had both exits GA-disabled, so single-leg
  longs rode to expiry (50/84 expired at ~zero). For credit structures (OS2/OS3): TP could
  never fire (−7,500% < any threshold) and the new `opt_sl` fired on the FIRST manage cycle →
  "OS2 almost nothing profitable". Affected members: O_VERT, O_BF, O_BULLCS (OS1), O_SSTG,
  O_SSTD, O_IC (OS2), all OS3 — and LIVE trading (shared packages/common code).
- **Fix:** new `_get_spread_pnl_via_transaction` — prices the STRUCTURE:
  `net_current = Σ sign×leg_premium×leg_ratio` (sign +1 BUY / −1 SELL leg; each leg marked
  from `account.get_option_quote`: long at bid, short at ask, last fallback — same as the
  single-leg path) against the parent's entry net premium (sign normalised via the parent
  side: BUY = debit paid, SELL = credit received). `percent = (net_current − open_net) /
  |open_net|` → "% of credit captured" for credit structures, debit multiple for debit
  structures. Declines (None, never fabricated) on missing leg quotes/multiplier, no filled
  legs, or ~zero entry net (even-money). Legs are fetched via a new
  `orders_where(parent_order_id=...)` filter (`trade_store.py`, both in-mem and DB paths) —
  the same parent→child link the live Alpaca account persists for MLEG orders.
- **Tests:** 7 live (`tests/test_option_spread_pnl_condition.py`: debit TP below/at threshold,
  credit TP at 50%-of-credit, credit SL at −100% incl. absolute-stored open_price, 1-2-1
  butterfly ratio weighting, no-legs/even-money decline) — **6 of 7 verified failing pre-fix**
  (git-stash TDD check). 1 backend wiring test
  (`tests/backtest/test_spread_pnl_condition.py`): the condition through BacktestAccount +
  HistoricalOptionsProvider + the trade_store DB path.
- **Known limitation:** all originally-filled legs are priced; a structure whose legs were
  individually closed mid-life (rare — legs normally close together via close_option/expiry)
  is priced with stale legs. Same class of limitation as the single-leg path.

## Economic verdict (web-checked)

- **Single-leg long premium (O_LC/O_LP) has no edge on weekly FMPRating signals** — the one
  correctly-priced path, and the GA's verdict was unanimous: 307/307 OS1 configs ≤ 0. Buying
  25–45 DTE options pays theta + the volatility risk premium that a slow analyst-rating signal
  doesn't overcome ([QuantPedia VRP](https://quantpedia.com/strategies/volatility-risk-premium-effect),
  [Andersen/Todorov, Kellogg](https://www.kellogg.northwestern.edu/faculty/todorov/htm/papers/opa.pdf)).
- **Credit selling is where the systematic premium lives** — but calibrated: the CBOE
  PutWrite benchmark did ~9.4–9.7%/yr over 37 years
  ([Cboe](https://www.cboe.com/insights/posts/generating-income-and-managing-risk-cash-secured-put-writing-in-a-low-equity-return-environment/),
  [Bondarenko](https://cdn.cboe.com/resources/education/research_publications/PutWriteCBOE19_v14_by_Prof_Oleg_Bondarenko_as_of_June_14.pdf)).
  OS2's 13.6% CAR (achieved *with* broken exits) is already above-benchmark; 30%/yr sustained
  is above the honest base rate without leverage or a real timing signal.

## Validation re-run (2026-07-24, backtest id 760)

Re-ran the best OS2-v7 individual (optimization 234) on the fixed engine via
`ba2test_launcher._persist_top_backtests(234, 'FMPRating', n=1)`. Exits: TP 75% of credit
enabled, 30-day time exit enabled, SL disabled — same exit config as pre-fix run 759.

- **TP now fires at the REAL threshold**: the four TP exits cluster at +74.4…+78.9% of credit
  captured (GOOGL strangle +75.7% after 19 bars, AAPL +74.4% after 13, MRK +75.0% after 7),
  with both legs of a paired structure closing together (GOOGL/WFC/MU/AAPL/MRK/INTC).
- **No more garbage 1-bar TP closes**: pre-fix run 759 closed 8/13 structures after 1 bar at a
  real +0.7…+11.7%; run 760 has exactly one same-day close (BAC, −30%, a signal exit at a
  loss) out of 13 structures.
- **Time exits work**: WFC/MU closed at 17–20 bars on the 30-day rule with partial credit.
- **Scoreboard**: 13 structures, +$569 on $4,353 premium (+13.1% per-premium), TR 7.42% over
  the ~2.2y window. The fix repairs exit semantics, not edge — see the economic verdict above.

### New anomaly found during validation → follow-up B10 (root-caused & fixed, 2026-07-24)

3 short-put legs (BABA240503P00067000, C240503P00057000, MRVL240503P00067000) were recorded
`open_at_end` at the entry premium (−$1 each) even though the contracts **expired 2024-05-03 —
two years before run end**. The put legs were held 551–560 bars despite the enabled 30-day
time exit AND the expiry — their lifecycle stopped entirely once the sibling call leg closed.
BABA and C were OTM at expiry (should have kept the full ~$298 credit); the engine records
≈0% instead — materially understating credit-strategy P&L (est. +$300…+870 on a +$569 run).

**Root cause (code-proven + engine-reproduced):** a three-step chain.

1. **Trigger — a per-leg close.** `maybe_margin_call_liquidation`
   (`testplatform/backend/app/services/backtest/backtest_account.py:780`) buys back ONE naked
   short leg at a time (largest |qty|, ties → the call leg). That standalone single-leg close
   has no `parent_order_id`, so it rides the structure's shared transaction.
2. **Mechanism — premature `position_balanced`.** `refresh_transactions`
   (`packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py`) computed the
   balanced check from parent-level buy/sell sums that EXCLUDE multi-leg child legs but COUNT
   standalone option closes. One leg close (BUY 1) therefore offsets the entry parent (SELL 1)
   and the whole strangle transaction is marked CLOSED with
   `close_reason="position_balanced"` — while the put leg is still open.
3. **Consequence — orphan leg.** Every consumer that would manage the surviving leg filters
   on `status == OPENED`: `get_option_positions`, `_apply_option_expiry`,
   `_option_transaction_for_contract`. The leg becomes invisible: it never expires, never
   time-exits, never contributes P&L. The same defect exists in LIVE (shared
   `packages/common` code), not just in the backtest engine.

**Fix (3 parts, all option-gated — equity/stock paths are byte-identical):**

1. `ReadOnlyAccountInterface.refresh_transactions`: when the transaction has a multi-leg
   parent (`multi_leg_parent_ids` non-empty), compute `position_balanced` by **per-contract
   netting over ALL executed option orders with a `contract_symbol`** (multi-leg children,
   standalone per-leg closes, synthetic expiry closes), and skip the mixed-unit
   `calculated_quantity` rewrite (set to 0.0). Single-leg option and equity transactions keep
   the previous parent-level logic.
2. `TradeConditions._get_spread_pnl_via_transaction` (the B9 structure-P&L path): price
   `cash_collected + flatten_cash` over the transaction's executed option orders — realized
   P&L of individually closed legs plus mark-to-flatten of the legs still held — and decline
   (return `None`) once the structure is fully flat. Reduces to the B9 formula for untouched
   structures (the 7 original B9 tests pass unchanged).
3. `TradeActions.build_closing_legs` / `_close_multi_leg`: new optional `held_qty` parameter —
   skips legs that are already flat and sizes the closing ratio from the remaining quantity;
   `_close_multi_leg` computes `held_qty` from the transaction's orders (in-memory and DB
   dual path) and returns a success no-op when nothing is left to close.

**Tests (TDD — red on the pre-fix code, green after):**

- `testplatform/backend/tests/backtest/test_spread_orphan_leg.py` (new, real engine): enter a
  strangle, close the call leg individually → pre-fix the transaction went CLOSED with
  `close_reason="position_balanced"`; post-fix it stays OPENED, the put is held to expiry,
  expires worthless, and only then does the transaction close.
- `tests/test_option_spread_pnl_condition.py`: partially-closed structure prices
  realized-plus-remainder (+70% fixture); fully-flat structure declines.
- `tests/test_option_close_multileg.py`: `held_qty` skips a flat leg; closing ratio is sized
  from the remaining quantity.

**Re-run still owed:** the exit-semantics improvement needs a fresh OS2 re-run (v8) to be
measured — expected recovery is the est. +$300…+870 leaked on run 760's 13 structures, plus
any knock-on from no longer orphaning legs mid-run.
