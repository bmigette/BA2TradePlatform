# Options Trading — Base Phases 2–4 Implementation Plan (rule conditions + actions + selector)

> **For Claude:** REQUIRED SUB-SKILL: superpowers:executing-plans / subagent-driven-development.

**Goal:** Make options tradeable through the existing rule engine: add option **conditions** (dip / IV-rank / option-state), an **`OptionContractSelector`**, option **actions** (buy_call / open_bull_call_spread / sell_covered_call / close_option) that select contracts and submit via the Phase-1 `OptionsAccountInterface`, wire them into the evaluator + settings UI + docs, and ship example rulesets.

**Architecture:** Builds entirely on Phase 1 (merged to `dev`). Conditions read OHLCV via the data-provider registry and IV-rank via `account.get_iv_rank`. Actions compute strikes/DTE/size from per-action JSON params, pick liquid contracts with the pure `OptionContractSelector`, and self-submit via `account.submit_option_order(...)` (gated on `self.submit_to_broker`). No RiskManager changes (option sizing happens in the action).

**Tech Stack:** Python 3.12, SQLModel, NiceGUI, alpaca-py 0.43.2, pytest. venv is `venv/` (run `venv/bin/python -m pytest`).

---

## Environment notes
- venv is `venv/` (NOT `.venv/`). Tests: `venv/bin/python -m pytest`.
- Pre-existing full-suite session-leak flakiness (order-dependent, `no such table: …`) — judge by running the specific test FILES; they're deterministic in isolation.
- Branch: `feature/options-trading-phase2` (off `dev`). Bump `version.py` before any push. Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Key design decisions (locked)
- **Option actions self-submit** via `account.submit_option_order(...)` when `self.submit_to_broker` is True; when False (manual-review mode) they record a `TradeActionResult` describing the intended trade but do not hit the broker (manual option staging is a later refinement). They are bucketed as **order-creating** actions (run in evaluator Phase 1).
- **Sizing:** `sizing=pct_equity` → premium budget = `pct_equity% × expert virtual equity`; contracts = `floor(budget / (premium × 100))`, min 1 if budget ≥ one contract else skip. Spread sizing by net debit. Covered call: 1 contract per 100 held shares of the underlying long (capped by `instrument` holdings).
- **Strike selection** via `OptionContractSelector` (pure): `delta` (nearest to target |delta|), `percent_otm` (strike nearest the % OTM from spot), `consensus_target` (strike nearest/just-below(call)/above(put) the analyst target). Spreads: long + short legs (configurable deltas/OTM). Always enforce `min_open_interest` + `max_spread_pct`; return `None` (skip+log) if nothing passes.
- **consensus_target source:** add a one-line `data={"FMPRating": {...targets...}}` write to `FMPRating._create_expert_recommendation`; selector reads `rec.data["FMPRating"]["target_consensus"]` if present, else reconstructs `price_at_date*(1±expected_profit_percent/100)`.
- **DTE window:** `dte_min`/`dte_max` filter chain expiries; selector prefers standard monthly when available.

---

## Task 1 — New enums + classifier helpers
**Files:** `ba2_trade_platform/core/types.py`; test `tests/test_option_rule_enums.py`.
- Add to `ExpertEventType` (~line 287): `N_PERCENT_BELOW_RECENT_HIGH = "percent_below_recent_high"`, `N_PERCENT_ABOVE_RECENT_LOW = "percent_above_recent_low"` (advanced-ready), `N_IV_RANK = "iv_rank"`, `F_HAS_OPTION_POSITION = "has_option_position"`, `F_HAS_COVERED_CALL = "has_covered_call"`.
- Add to `ExpertActionType` (~line 337): `BUY_CALL = "buy_call"`, `OPEN_BULL_CALL_SPREAD = "open_bull_call_spread"`, `SELL_COVERED_CALL = "sell_covered_call"`, `CLOSE_OPTION = "close_option"`.
- Add the three numeric events to `get_numeric_event_values()` (~line 405).
- Add `get_option_action_values()` returning the 4 option action `.value`s and `is_option_action(action_value)` mirroring `is_share_adjustment_action`.
- Tests: assert enum `.value`s; `is_numeric_event("iv_rank")` True; `is_option_action("buy_call")` True; `is_option_action("buy")` False.

## Task 2 — `OptionContractSelector` (pure helper)
**Files:** Create `ba2_trade_platform/core/option_selector.py`; test `tests/test_option_selector.py`.
Pure functions over `list[OptionContract]` (Phase-1 dataclass). API:
```python
def passes_liquidity(c: OptionContract, min_open_interest: int|None, max_spread_pct: float|None) -> bool
def filter_dte(chain, today: date, dte_min: int|None, dte_max: int|None) -> list[OptionContract]
def select_single(chain, *, method, strike_param, spot, target_price=None,
                  option_type, dte_min, dte_max, today,
                  min_open_interest=None, max_spread_pct=None) -> OptionContract | None
def select_vertical_spread(chain, *, long_param, short_param, method, spot, option_type,
                           dte_min, dte_max, today, min_open_interest=None,
                           max_spread_pct=None) -> tuple[OptionContract, OptionContract] | None
```
- `method="delta"`: choose contract whose `abs(delta)` is nearest `strike_param`.
- `method="percent_otm"`: target strike = spot*(1+param/100) for calls (OTM above), spot*(1-param/100) for puts; choose nearest available strike.
- `method="consensus_target"`: choose strike nearest `target_price` (for calls prefer ≤ target; configurable) — require `target_price` not None.
- All candidates must pass DTE window + liquidity; pick within the same (nearest-to-min-DTE within window) expiry for spreads; both legs same expiry. Return None if empty.
- Tests (TDD, pure, deterministic): build a synthetic chain; assert delta-nearest pick; percent_otm strike; consensus_target pick; liquidity filter drops low-OI/wide-spread; DTE window filter; spread returns long<short strike for a debit call spread; None when nothing passes.

## Task 3 — Conditions (classes + registration)
**Files:** `ba2_trade_platform/core/TradeConditions.py` (+ `condition_map` ~1579); test `tests/test_option_conditions.py`.
- `PercentBelowRecentHighCondition(CompareCondition)` (`N_PERCENT_BELOW_RECENT_HIGH`): fetch OHLCV via `get_provider("ohlcv","yfinance").get_ohlcv_data(self.instrument_name, interval="1d", lookback_days=max(value_window,40))`; `recent_high = df.tail(N)["High"].max()`; `calculated_value = (recent_high - current_price)/recent_high*100`; compare to threshold. N default 20 (document). Guard empty df → False.
- `PercentAboveRecentLowCondition(CompareCondition)` (`N_PERCENT_ABOVE_RECENT_LOW`): mirror with `["Low"].min()` (advanced-ready; ship now, cheap).
- `IVRankCondition(CompareCondition)` (`N_IV_RANK`): `if not isinstance(self.account, OptionsAccountInterface): return False`; `rank = self.account.get_iv_rank(self.instrument_name)`; `calculated_value = rank`; `None → False`; else compare.
- `HasOptionPositionCondition(FlagCondition)` (`F_HAS_OPTION_POSITION`): True if this expert has an OPENED Transaction whose orders include an `asset_class==OPTION` order for the underlying (query `TradingOrder` join `Transaction` by `expert_id` + `underlying_symbol==instrument_name` + status). 
- `HasCoveredCallCondition(FlagCondition)` (`F_HAS_COVERED_CALL`): True if a short call (`asset_class==OPTION`, `option_type==CALL`, side==SELL, `option_strategy in ("covered_call","single")`) is open for the underlying for this expert.
- Register all in `condition_map`. Implement `get_actual_value_display`.
- Tests: with MockAccount + seeded OHLCV (monkeypatch `get_provider`/the provider's `get_ohlcv_data` to a canned DataFrame) for dip; IV-rank via mock_account.record_atm_iv then condition; has_option_position/has_covered_call by seeding option TradingOrders/Transactions.

## Task 4 — rules_documentation for conditions
**Files:** `ba2_trade_platform/core/rules_documentation.py` `get_event_type_documentation()`.
- Add entries (with `type` "numeric"/"boolean", `name`, `description`, `example`) for the 5 new events. Note real scales (iv_rank 0–100; percent values). Test: `tests/test_rules_documentation.py` asserts each new event/action value has a doc entry and actions have `use_cases`.

## Task 5 — Actions (classes + registration)
**Files:** `ba2_trade_platform/core/TradeActions.py` (+ `action_map` ~1498); test `tests/test_option_actions.py`.
Each action `__init__` accepts kwargs: `strike_method, strike_param, dte_min, dte_max, sizing, min_open_interest, max_spread_pct` (+ spread params via strike_param dict, e.g. `{"long":0.45,"short":0.25}`). Shared base `_OptionEntryAction(TradeAction)` with helpers: `_spot()`, `_expert_virtual_equity()` (from expert instance + account balance), `_chain(option_type)` via `self.account.get_option_chain(...)`, `_consensus_target()`, `_size_contracts(premium)`.
- `BuyCallAction` (`BUY_CALL`): select single call via selector; size by pct_equity; `submit_option_order([OptionLeg(BUY, buy_to_open, call)], qty, "limit", limit_price=ask, option_strategy="long_call")`.
- `OpenBullCallSpreadAction` (`OPEN_BULL_CALL_SPREAD`): select_vertical_spread (long lower strike BUY, short higher strike SELL); net debit = long.ask - short.bid; size by max_risk; `submit_option_order([long_leg, short_leg], qty, "limit", limit_price=net_debit, option_strategy="bull_call_spread")`.
- `SellCoveredCallAction` (`SELL_COVERED_CALL`): OPEN_POSITIONS overlay; require a held equity long for the underlying (this expert); 1 contract per 100 shares; select OTM call (strike ≥ cost basis); `submit_option_order([OptionLeg(SELL, sell_to_open, call)], qty, "limit", limit_price=bid, option_strategy="covered_call")`.
- `CloseOptionAction` (`CLOSE_OPTION`): resolve the held option position from `existing_order`/`get_option_positions`; `account.close_option_position(position, "limit", limit_price=<bid for long / ask for short>)`.
- All gate on `self.submit_to_broker` (else record an informational `TradeActionResult`, no broker). Register in `action_map`.
- Tests: via MockAccount (canned chain) assert each action builds the right legs/quantity/strategy and calls submit_option_order (monkeypatch/capture) with buy@ask / sell@bid; selector returns None → action records skip, no submit; covered call with no underlying long → skip.

## Task 6 — Evaluator wiring
**Files:** `ba2_trade_platform/core/TradeActionEvaluator.py`.
- `_create_trade_action` (~914): add an `elif action_type in (BUY_CALL, OPEN_BULL_CALL_SPREAD, SELL_COVERED_CALL, CLOSE_OPTION):` branch pulling `strike_method/strike_param/dte_min/dte_max/sizing/min_open_interest/max_spread_pct` from `action_config` into kwargs.
- `_get_action_type_from_action` (~966): map the 4 new class names → enum members.
- `execute()` bucket (~243): add the 4 to `order_creating_actions`.
- `_sort_actions_by_priority` (~1000): BUY_CALL/OPEN_BULL_CALL_SPREAD/SELL_COVERED_CALL priority 1, CLOSE_OPTION priority 2.
- Extend the dedup hash (~826) to include strike params so two option actions differing only by params don't collide.
- Test: `tests/test_option_evaluator.py` — an EventAction with an option action + matching trigger → evaluator builds + executes it against MockAccount (end-to-end: trigger passes → action submits a mock option order).

## Task 7 — rules_documentation for actions
**Files:** `rules_documentation.py` `get_action_type_documentation()` — add the 4 actions with `name/description/use_cases(list)/parameters/example`. (Covered by the Task 4 doc test.)

## Task 8 — Settings UI (rule editor) for option-action params
**Files:** `ba2_trade_platform/ui/pages/settings.py`.
- Import `is_option_action` (line 12).
- `update_action_inputs()` (~after 4818): new `elif is_option_action(selected_type):` branch rendering widgets for strike_method (select), strike_param, dte_min, dte_max, sizing (pct_equity), min_open_interest, max_spread_pct — each pre-filled from `action_config.get(...)`; register lambda refs at ~4825.
- `_save_rule` (~after 4903): new `elif is_option_action(action_type):` branch writing those keys into `action_config`.
- Verify round-trip (load saved option rule → widgets populated). This task has no unit test (NiceGUI UI) — verify by a focused import/smoke check + manual note; OR a light test that constructs the action_config dict shape the save handler would produce and asserts the evaluator parses it (ties UI shape to evaluator).

## Task 9 — FMPRating persists analyst targets
**Files:** `ba2_trade_platform/modules/experts/FMPRating.py` (`_create_expert_recommendation` ~497); test `tests/test_experts/test_fmp_rating.py` (extend).
- Pass `data={"FMPRating": {"target_consensus":…, "target_high":…, "target_low":…, "target_median":…}}` from the already-computed values. Test asserts the recommendation persists `data["FMPRating"]["target_consensus"]`.

## Task 10 — Example rulesets + end-to-end
**Files:** `test_files/setup_option_rulesets.py` (manual seeder, like existing setup_*_rulesets.py); test `tests/test_option_end_to_end.py`.
- Seeder builds: ENTER_MARKET ruleset (dip + iv_rank-low + bullish → BUY_CALL or OPEN_BULL_CALL_SPREAD) and OPEN_POSITIONS ruleset (profit_loss_percent / days_to_expiry-style + rating flip → CLOSE_OPTION; covered-call overlay → SELL_COVERED_CALL). 
- End-to-end test: construct a Ruleset+EventAction in the test DB, a recommendation + MockAccount with a canned chain, run `TradeActionEvaluator.evaluate()`+`execute()`, assert a mock option order was submitted.

## Task 11 — Full verification + version bump
- Run all new option test files green (list them). Run the broad suite (default order) and report honestly (pre-existing flakiness).
- Bump `version.py`. Commit. Do not push/merge (controller presents options).

---

## Definition of done (base Phases 2–4)
- New conditions + actions registered everywhere (enum, map, evaluator buckets, priority, docs, UI) and unit-tested.
- `OptionContractSelector` pure-tested across method × liquidity × DTE.
- End-to-end: a ruleset with an option action evaluates and submits a (mock) option order; covered-call requires a held long; close resolves a held option.
- FMPRating exposes consensus target for `consensus_target`.
- Example ruleset seeder provided. All option test files green; version bumped.

## Carry-forward (advanced plan A–D, separate)
Long puts / bear put + bear call spreads / CSP / protective puts / straddle-strangle; short-premium core (assignment/expiry reconciliation + cash/BP reserve); `days_to_earnings`; manual-review option staging; per-leg fill P&L source-of-truth.
