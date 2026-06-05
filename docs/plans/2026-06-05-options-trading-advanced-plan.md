# Options Trading — Advanced Structures Implementation Plan (A–D)

> **For Claude:** REQUIRED SUB-SKILL: subagent-driven-development.

**Goal:** Extend the options feature with bearish/put structures, short-premium (CSP + bear call spread) with assignment/expiry reconciliation, and long-volatility (straddle/strangle), reusing ALL base infrastructure (OptionsAccountInterface, OptionContractSelector, submit_option_order multi-leg, option TradingOrder fields, conditions/actions framework).

**Depends on:** Phase 1 (account infra) + base Phase 2–4 (conditions/selector/actions/wiring), both merged to `dev`.

**Branch:** `feature/options-trading-advanced` (off `dev`). venv `venv/`. Bump version before push. Commit trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Reuse map (already built — do NOT rebuild)
- `OptionContractSelector.select_single/select_vertical_spread` already handle PUTS (delta uses abs; percent_otm direction-aware; put spread orders long>short). Straddle/strangle = two single selects (a call + a put).
- `submit_option_order(legs, qty, order_type, limit_price, option_strategy, ...)` handles 1–4 legs (MLEG). `close_option_position`. `get_option_positions`. `get_iv_rank`.
- `_OptionEntryAction` base (TradeActions.py): `_spot/_chain/_virtual_equity/_size/_consensus_target/_supports_options/_submit_option_order` + submit_to_broker gate. New actions subclass it.
- Bearish entry condition `percent_above_recent_low` already exists. `iv_rank`, `has_option_position` exist.
- Evaluator wiring pattern (5 points) + settings UI `is_option_action` branch + rules_documentation pattern — extend the existing branches/maps to include the new actions.

---

## Phase A — Long puts + Bear put spread (pure debit; highest reuse)

### Task A1 — enums + docs for BUY_PUT / OPEN_BEAR_PUT_SPREAD
- `types.py`: add `ExpertActionType.BUY_PUT="buy_put"`, `OPEN_BEAR_PUT_SPREAD="open_bear_put_spread"`; add both to `get_option_action_values()`.
- `rules_documentation.py`: add `get_action_type_documentation` entries (name/description/use_cases/parameters/example).
- Test: extend test_option_rule_enums + test_rules_documentation.

### Task A2 — BuyPutAction + OpenBearPutSpreadAction + wiring
- `TradeActions.py`: `BuyPutAction(_OptionEntryAction)` — select_single PUT (buy_to_open), buy@ask, option_strategy "long_put". `OpenBearPutSpreadAction(_OptionEntryAction)` — select_vertical_spread PUT (long higher strike BUY, short lower strike SELL), net debit = long.ask - short.bid, option_strategy "bear_put_spread". Register in action_map.
- `TradeActionEvaluator.py`: add both to the option-param branch in `_create_trade_action`, the class→enum map, the order_creating bucket, the priority map (=1), and the dedup hash list.
- `settings.py`: they're auto-covered by the existing `is_option_action` branch (no UI change beyond enum membership) — verify.
- Tests: test_option_actions (put buy@ask, bear-put-spread legs/strategy/net-debit), test_option_evaluator (wired + executes via mock), and an e2e (rally→buy_put using percent_above_recent_low). Mirror the call-side tests.

---

## Phase B — Protective puts (OPEN_POSITIONS overlay)

### Task B1 — BUY_PROTECTIVE_PUT + F_HAS_PROTECTIVE_PUT
- `types.py`: `ExpertActionType.BUY_PROTECTIVE_PUT="buy_protective_put"` (+ option_action_values); `ExpertEventType.F_HAS_PROTECTIVE_PUT="has_protective_put"`.
- `TradeConditions.py`: `HasProtectivePutCondition(FlagCondition)` — open long put (asset_class OPTION, option_type PUT, side BUY, option_strategy "protective_put") for the underlying for this expert (mirror has_covered_call). Register.
- `TradeActions.py`: `BuyProtectivePutAction(_OptionEntryAction)` — requires a held equity long for the underlying (reuse `_held_equity_shares`); 1 put per 100 shares; select OTM/ATM put; buy@ask; option_strategy "protective_put". Register + evaluator wiring.
- `rules_documentation.py` entries; settings UI auto-covered.
- Tests: condition (has_protective_put true/false), action (requires long → qty=shares/100; no long → skip), e2e overlay.

---

## Phase C — Short-premium core (the genuinely NEW infra): assignment/expiry reconciliation + cash/BP reserve, then CSP + bear call spread

### Task C1 — Cash/BP reserve check in the options account layer
- `OptionsAccountInterface`: add a concrete helper `option_buying_power_ok(strategy, legs, quantity, limit_price)` (or a reserve calc) — CSP requires `strike×100×qty` cash; credit spread requires `(width−credit)×100×qty` max loss ≤ available. Compute available from `get_balance()` minus already-reserved (sum of open short-premium reservations). Keep it a defense-in-depth check the short-premium actions call before submitting; skip+log if insufficient.
- Test: pure-ish reserve math + a Mock-backed check (sufficient vs insufficient → submit vs skip).

### Task C2 — Assignment / expiry reconciliation (AlpacaAccount + refresh)
- `AlpacaAccount`: add `get_option_activities(start=None)` calling the Trading API `GET /v2/account/activities` directly (alpaca-py TradingClient lacks it) filtered to option activity types `OPASN/OPEXC/OPEXP/OPCSH` (+ paired `OPTRD`). Parse into structured records. (Paper syncs NTAs next-day — document.)
- `AlpacaAccount.reconcile_option_assignments()` (called from refresh): for each new activity — short put assigned → open an equity `Transaction` (long shares at strike) attributed to the originating expert/transaction (match by underlying + the short option order's transaction → expert_id); short call assigned → mark shares called away (close/adjust the equity long); long expired worthless → close the option Transaction; ITM long auto-exercised → reconcile. Persist idempotently (track processed activity ids).
- Tests: with a Mock/stub activities feed, assert each scenario updates Transaction state correctly + idempotency (re-running doesn't double-apply). This is the riskiest task — test thoroughly with canned activity payloads; real paper validation deferred to the validation script.

### Task C3 — SELL_CASH_SECURED_PUT + OPEN_BEAR_CALL_SPREAD
- `types.py`: `SELL_CASH_SECURED_PUT="sell_cash_secured_put"`, `OPEN_BEAR_CALL_SPREAD="open_bear_call_spread"` (+ option_action_values).
- `TradeActions.py`: `SellCashSecuredPutAction(_OptionEntryAction)` — select put (sell_to_open), sell@bid, reserve `strike×100×qty` (call C1 check), qty = floor(reservable_cash / (strike×100)); option_strategy "cash_secured_put". `OpenBearCallSpreadAction(_OptionEntryAction)` — credit call spread (short lower strike SELL, long higher strike BUY), net credit = short.bid - long.ask (negative limit_price = credit), reserve `(width−credit)×100`, option_strategy "bear_call_spread". Register + evaluator wiring + docs.
- Tests: CSP cash-reserve sizing + assignment path (via C2); bear call spread max-loss sizing + credit limit sign; skip when reserve insufficient.

---

## Phase D — Long straddle / strangle + days_to_earnings

### Task D1 — N_DAYS_TO_EARNINGS + OPEN_STRADDLE / OPEN_STRANGLE
- `types.py`: `N_DAYS_TO_EARNINGS="days_to_earnings"` (+ numeric_event_values); `OPEN_STRADDLE="open_straddle"`, `OPEN_STRANGLE="open_strangle"` (+ option_action_values).
- Earnings data: add a source for days-to-earnings (FMP earnings calendar via the data-provider layer or FMP client). `DaysToEarningsCondition(CompareCondition)`.
- `TradeActions.py`: `OpenStraddleAction(_OptionEntryAction)` — select ATM call + ATM put (same strike, same expiry), buy both (buy_to_open), net debit = call.ask + put.ask, option_strategy "straddle". `OpenStrangleAction` — OTM call + OTM put (different strikes), option_strategy "strangle". 2-leg MLEG via submit_option_order. Register + wiring + docs.
- Tests: straddle two-leg construction (same strike), strangle (OTM call+put), days_to_earnings condition.

---

## Per-phase Definition of Done
Each new action: registered in action_map + evaluator (create/map/bucket/priority/dedup) + docs + (option params auto via is_option_action). Each new condition: condition_map + (numeric→get_numeric_event_values) + docs. Unit tests via MockAccount; e2e via evaluator. Short-premium (C) additionally: reserve check + assignment reconciliation tested with canned payloads.

## Cross-cutting carry-forwards from base (address as they bite)
- Option TP/SL routing: ensure mixed option-entry + adjust_tp/sl rulesets don't hand option orders to equity adjust_tp/sl (gate in evaluator Phase 2 or document unsupported).
- CLOSE_OPTION position resolution via get_option_positions() (needed for assignment-driven closes).
- Manual-review (submit_to_broker=False) staging.
- consensus_target strike preference (≤ target for calls / ≥ for puts).

## Out of scope (unchanged): 0DTE, iron condors/calendars, index options.
