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
