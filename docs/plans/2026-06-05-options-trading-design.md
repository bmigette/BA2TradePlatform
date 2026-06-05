# Options trading — design & requirements

Date: 2026-06-05
Status: Approved design, implementation deferred to a later session
Author: brainstormed with the team

## Goal

Add options trading (high-risk / high-reward) driven by the existing signal +
rule stack. Thesis: select instruments that **dipped** and sit **far below
analyst consensus**, then express the view with options, with the **rule-based
risk manager** (rulesets) deciding entries/exits. Three strategies:

1. **Long calls** — directional, leveraged, uncapped upside, premium-at-risk.
2. **Bull call (debit) spreads** — directional, defined risk (max loss = debit).
3. **Covered calls** — yield overlay written against equity longs we already
   hold; expressed as an **OPEN_POSITIONS rule action**, not a standalone entry.

Alpaca paper accounts support options at Level 3 by default (single + multi-leg),
so paper validation is viable. Keep to **liquid** contracts so paper fills (which
ignore quoted size and model no slippage) stay realistic.

## Key decisions (locked)

- **Architecture:** extend the existing rule engine + account layer. No new
  expert for entries — reuse `FMPRating` (analyst consensus / target) + the
  screener as the signal source, and rulesets as the risk manager. Options are
  new **conditions** + **actions** in the rule engine, executed through a new
  options account capability.
- **Options capability = a separate interface.** Add `OptionsAccountInterface`
  (sibling to `AccountInterface`). `AlpacaAccount` implements **both** the equity
  and the options interface. This isolates option mechanics and lets other
  brokers add options later without touching equity code.
- **Strike selection is configurable per rule action**: `delta`, `percent_otm`,
  or `consensus_target`.
- **Expiry/DTE is configurable per rule action** (min/max DTE window).

## Architecture overview

```
FMPRating + screener  →  ExpertRecommendation (consensus target, confidence)
        │
        ▼
Ruleset (ENTER_MARKET / OPEN_POSITIONS)
   conditions: existing + new (dip, IV, option-state, DTE, moneyness)
   actions:    existing equity + new option actions
        │
        ▼
TradeActionEvaluator → option action → OptionContractSelector (chain + filters)
        │
        ▼
OptionsAccountInterface.submit_option_order(legs)   ← AlpacaAccount implements
        │
        ▼
Option position persisted (see "Position model") and managed by OPEN_POSITIONS rules
```

## Components to build

### 1. `OptionsAccountInterface` (new)
Capability interface, implemented by `AlpacaAccount` (and future brokers).
Proposed surface:

- `supports_options: bool`
- `get_option_chain(underlying, expiry_min, expiry_max, option_type, strike_range=None) -> list[OptionContract]`
  - Each `OptionContract`: contract symbol (OCC), type (call/put), strike, expiry,
    bid, ask, last, **implied_volatility**, **delta/gamma/theta/vega**,
    **open_interest**, volume.
- `get_option_quote(contract_symbol) -> OptionQuote`
- `get_option_positions() -> list[OptionPosition]`
- `submit_option_order(legs: list[OptionLeg], quantity, order_type, limit_price=None, ...)`
  - single-leg (long call, covered call) and multi-leg (bull call spread) via
    Alpaca `OptionLegRequest` / mleg order class.
- `close_option_position(position, ...)`
- `get_iv_rank(underlying) -> float` (0–100; 1y IV percentile) — see Data section.

Notes: reuse the existing price-cache pattern; respect the bid/ask (never mid)
for fills/PnL; expose a single source of truth for option quotes.

### 2. Option data (Alpaca)
- Chains, quotes, Greeks, IV: Alpaca options market data.
- **IV rank** (IV percentile over ~1y) likely needs to be computed from a history
  of ATM IV (Alpaca may not provide it directly). Phase-1 fallback: use absolute
  IV or a short trailing IV percentile; refine later. **Open question** flagged.
- Liquidity fields (open_interest, bid-ask width) come from the chain.

### 3. Position model (option holdings)
Recommended: **reuse `TradingOrder` / `Transaction`** with option metadata rather
than new tables, to inherit the existing lifecycle, wash-lock, and rule plumbing.
Add to `TradingOrder` (nullable, equity rows leave them null):
- `asset_class` ("equity" | "option")
- `contract_symbol` (OCC), `option_type`, `strike`, `expiry`, `underlying_symbol`
- multi-leg via the existing parent/`legs_broker_ids` mechanism (already used for OCO).

A **covered call** links to the **underlying equity `Transaction`** (the long it
is written against) so OPEN_POSITIONS rules can see "this long has a call written".
Alternative considered: dedicated `OptionPosition`/`OptionOrder` models (cleaner
separation, but duplicates lifecycle/rules). **Decision pending** at
implementation; default to extending `TradingOrder`.

### 4. New rule conditions (fills the gaps from the audit)
Add to `ExpertEventType` + `TradeConditions` + `rules_documentation`:

Entry-signal gaps:
- `N_PERCENT_BELOW_RECENT_HIGH` — drawdown from N-day high ("had a dip").
- `N_RSI` (optional) — momentum/oversold confirmation.
- `N_IV_RANK` — implied-vol regime (covered calls want high; long calls want low).
- (`N_IMPLIED_VOLATILITY` absolute, optional.)

Option position-state gaps:
- `F_HAS_OPTION_POSITION`, `F_HAS_COVERED_CALL` — avoid double-writing / manage legs.

Option-leg management (Phase 2 — confirm scope):
- `N_DAYS_TO_EXPIRY` (DTE of the held option leg).
- `N_UNDERLYING_DISTANCE_TO_STRIKE_PERCENT` (moneyness, for rolling a short call).

Reused as-is: `percent_to_new_target`, `percent_open_to_new_target`,
`expected_profit_target_percent`, `confidence`, `current_rating_*`,
`rating_upgraded`, `has_position`, `has_buy_position`, `profit_loss_percent`,
`days_opened`, `instrument_account_share`.

### 5. New rule actions
Add to `ExpertActionType` + `TradeActions` + the evaluator:
- `BUY_CALL`
- `OPEN_BULL_CALL_SPREAD`
- `SELL_COVERED_CALL`
- `CLOSE_OPTION`
- (`ROLL_OPTION` — Phase 2.)

Each action carries configurable parameters in its JSON (mirroring how
`adjust_take_profit` carries `value`/`reference_value`):
- `strike_method`: `delta` | `percent_otm` | `consensus_target`
- `strike_param`: target delta, or % OTM (spreads take long+short params)
- `dte_min`, `dte_max`: expiry window
- `sizing`: `pct_equity` (premium budget); spreads also `max_risk_pct`
- liquidity filters: `min_open_interest`, `max_spread_pct` (default from global
  settings, overridable per action)

### 6. `OptionContractSelector` (new helper, pure + testable)
Given chain + method + params + DTE window + liquidity filters → pick contract(s):
- delta: nearest to target delta; spread: long ~0.45 / short ~0.25 (configurable)
- percent_otm: nearest strike at the configured % OTM
- consensus_target: strike nearest (or just below/above) the analyst target
- always enforce liquidity filters (OI, spread); prefer standard monthly/weekly.
Returns `None` (skip, log) if nothing passes — never force an illiquid fill.

## Risk / sizing (proposed defaults, tunable)
- Long call: premium budget = 1–2% of expert virtual equity per trade (premium is
  the max loss).
- Bull spread: net debit ≤ 2% of equity (debit = max loss); size by debit.
- Covered call: 1 contract per 100 held shares; only against an OPENED long;
  short strike OTM and ≥ cost basis.
- Portfolio caps: max aggregate option premium-at-risk (e.g. 10% of equity), max
  concurrent option positions. Enforced as defense-in-depth in the options account
  layer + expressible via rules.

## Exits (OPEN_POSITIONS option rulesets, proposed)
- Long call / spread: take-profit at +75–100% premium; stop at −50% premium;
  time-stop when `days_to_expiry <= 7–10`; or close on `rating_downgraded` /
  `current_rating` deterioration.
- Covered call: buy-to-close at ~50% of credit captured; manage near expiry / when
  ITM (roll = Phase 2); drop the overlay if the underlying thesis flips.

## Testing strategy
- Pure unit tests for `OptionContractSelector` (method × params × liquidity).
- Condition evaluators tested like existing `TradeConditions` tests.
- Action → order construction tested against a **mock options account** (extend
  the test `MockAccount` to implement `OptionsAccountInterface` with canned
  chains/quotes).
- End-to-end: rule eval → option action → mock order, incl. covered-call-on-held-
  long and bull-spread multi-leg.
- Paper validation on liquid underlyings (large-cap/ETF, near-the-money, monthly).

## Phasing
1. `OptionsAccountInterface` + `AlpacaAccount` impl (chain/quote/IV/order/close) +
   option fields on `TradingOrder` + position handling.
2. New conditions (dip, IV rank, has_option/has_covered_call) + tests + docs.
3. New actions + `OptionContractSelector` + sizing/liquidity + tests.
4. Wire into evaluator/executor; ship example rulesets; paper-trade validation.
5. Leg management: `ROLL_OPTION`, DTE/moneyness conditions, assignment/expiry
   handling refinements.

## Open questions (resolve during implementation)
- **IV rank source:** does Alpaca expose enough IV history, or do we compute/store
  a trailing ATM-IV series ourselves?
- **Position model:** extend `TradingOrder` (default) vs dedicated option models —
  confirm once multi-leg lifecycle is prototyped.
- **Assignment & expiry:** covered-call assignment (shares called away) and
  long-option expiration/auto-exercise — how Alpaca reports them and how we
  reconcile `Transaction` state on refresh.
- **Multi-leg order/PnL accounting:** how spreads are stored, filled, and P&L'd as
  a unit vs per leg.
- **Buying power / margin** for spreads in paper vs live.
- Wash-trade lock interaction: confirm option orders are out of scope for the
  equity wash-trade gate (different instrument symbols), or extend if needed.

## Out of scope (for now)
- Puts / protective puts / bearish structures, iron condors, calendars.
- Index options (Alpaca "coming soon").
- 0DTE.
