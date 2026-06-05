# Options trading — advanced structures (companion plan)

Date: 2026-06-05
Status: Approved scope, implementation deferred
Depends on: `2026-06-05-options-trading-design.md` (the base options plan). Build
this only after the base plan's phases 1–4 (OptionsAccountInterface, contract
selector, option position model, conditions/actions wiring) are in place.

This plan extends the base options feature with bearish/put structures and a
long-volatility play. It reuses **all** base infrastructure (the
`OptionsAccountInterface`, `OptionContractSelector`, multi-leg orders, the option
fields on `TradingOrder`, and the rule conditions/actions framework). Only the
new structures, conditions, and actions below are added.

## In scope

1. **Long puts** — directional bearish; mirror of long calls. High risk/reward.
2. **Protective puts** — OPEN_POSITIONS overlay: buy a put against a held long to
   cap downside (mirror of the covered-call overlay, protection instead of income).
3. **Cash-secured puts (CSP)** — sell a put to get paid to enter a long lower (or
   keep the premium). Income/bullish entry; requires cash-reserve handling and
   assignment handling (assignment → equity long).
4. **Bear put spread** (debit) and **bear call spread** (credit) — defined-risk
   bearish multi-leg.
5. **Long straddle / strangle** — buy call+put for a large move either direction
   (long volatility). High risk/reward; wants cheap IV and ideally a catalyst.

## Explicitly out of scope (and why)

- **0DTE / very-short-DTE** — removed for now. Safe management needs a dedicated
  intraday/high-frequency monitor (a new expert / fast loop); the 5-min account
  refresh is too slow. Revisit as its own project.
- **Iron condors, calendars/diagonals** — income/neutral, not the high-risk/
  high-reward mandate; deferred.
- **Index options (SPX/NDX)** — excluded entirely (European, cash-settled,
  different multipliers; Alpaca "coming soon"). Equity/ETF options only.

## New rule actions

Add to `ExpertActionType` + `TradeActions` + evaluator (same configurable-param
pattern as the base plan: `strike_method`, `strike_param`, `dte_min/max`,
`sizing`, liquidity filters):

- `BUY_PUT` (ENTER_MARKET) — bearish directional.
- `OPEN_BEAR_PUT_SPREAD` (debit, ENTER_MARKET).
- `OPEN_BEAR_CALL_SPREAD` (credit, ENTER_MARKET) — short premium; needs margin/
  buying-power + assignment handling.
- `BUY_PROTECTIVE_PUT` (OPEN_POSITIONS) — hedge a held long; 1 put per 100 shares.
- `SELL_CASH_SECURED_PUT` (ENTER_MARKET) — reserve cash = strike×100×contracts;
  assignment → open equity long.
- `OPEN_STRADDLE` / `OPEN_STRANGLE` (ENTER_MARKET) — long call+put (straddle: same
  ATM strike; strangle: OTM call + OTM put).

`CLOSE_OPTION` from the base plan closes any of these.

## New rule conditions (beyond the base plan)

The base plan adds dip / IV-rank / option-state / DTE / moneyness. This plan adds
the bearish and event mirrors:

- `N_PERCENT_ABOVE_RECENT_LOW` (or reuse RSI overbought) — the "rallied" mirror of
  the base plan's `percent_below_recent_high`; for bearish entries.
- `F_HAS_PROTECTIVE_PUT` — mirror of `has_covered_call`; avoid double-hedging /
  manage the put.
- `N_DAYS_TO_EARNINGS` (or generic catalyst proximity) — needed for straddle/
  strangle to be meaningful (enter before a catalyst, exit after). Flagged in the
  base plan as optional; **required here** for the straddle use case.

Reused for bearish entries: `current_rating_negative` / `current_rating_underweight`,
`rating_downgraded`, `bearish`, `percent_to_new_target` / `percent_open_to_new_target`
(negative when price is above target = overvalued), `iv_rank`, `confidence`.

## Cross-cutting: short-premium handling (elevated from base "open question")

CSP and bear **call** spreads are **short-premium** — they introduce obligations
the base plan's long-only structures don't. These become **required components**:

- **Cash/buying-power reserve:** CSP must reserve `strike×100×contracts` cash;
  credit spreads must reserve `(width−credit)×100` as max loss. Enforce in the
  options account layer (defense-in-depth) and surface a condition/flag if needed.
- **Assignment handling:** a short put (CSP) assigned → opens an equity
  `Transaction` (long shares at strike); a short call leg assigned → shares called
  away. Refresh/reconciliation must detect assignment from the broker and update
  `Transaction` state. (This generalizes the base plan's covered-call assignment
  question into a shared assignment-reconciliation component.)
- **Expiry:** worthless long options expire (close the position); ITM longs may be
  auto-exercised — reconcile on refresh.

## Risk / sizing (proposed defaults, tunable)

- Long put / bear put spread / straddle / strangle: debit ≤ 1–3% of expert virtual
  equity (debit = max loss).
- Bear call spread (credit): size so max loss `(width−credit)×100×contracts` ≤ 2%.
- Protective put: 1 put per 100 held shares; treat premium as insurance cost.
- CSP: only if reserved cash available; 1 contract per `strike×100` cash; premium
  is income; assignment cost already reserved.
- Same portfolio caps as base plan (aggregate premium-at-risk, max concurrent).

## Exits (OPEN_POSITIONS rulesets, proposed)

- Long put / bear spread: TP +75–100% / SL −50% premium; DTE time-stop
  (`days_to_expiry <= 7–10`); exit on `rating_upgraded` (thesis reversal).
- Protective put: hold as insurance; roll near expiry; drop if the long thesis
  strongly recovers.
- CSP: buy-to-close at ~50% of credit, or accept assignment (→ equity long, then
  base-plan equity rules + covered-call overlay apply).
- Straddle / strangle: TP at a combined-premium target; **close around the
  catalyst / before IV crush**; DTE time-stop.

## Testing strategy

Same approach as the base plan. Extend the mock options account with put chains
and assignment simulation. Add unit tests for: bearish contract selection, CSP
cash-reserve sizing, credit-spread max-loss sizing, straddle two-leg construction,
and assignment reconciliation (short put → long, short call → called away).

## Phasing (after base plan phases 1–4)

A. **Long puts + bear put spread** — pure debit, reuses base machinery directly.
B. **Protective puts** — OPEN_POSITIONS overlay + `has_protective_put`.
C. **Short-premium core** — assignment/expiry reconciliation + cash/BP reserve
   (shared component), then **cash-secured puts** and **bear call spreads**.
D. **Long straddle/strangle** — + `days_to_earnings` condition.

## Open questions

- Bearish price-action condition: dedicated `percent_above_recent_low` vs RSI —
  pick one to start.
- Earnings/catalyst data source for `days_to_earnings` (FMP earnings calendar?).
- Assignment reporting specifics from Alpaca (events vs polled position deltas) and
  how to attribute an assigned position back to the originating expert/transaction.
- Whether short-premium structures need their own portfolio risk cap separate from
  long-premium premium-at-risk.
