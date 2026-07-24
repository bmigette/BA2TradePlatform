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
