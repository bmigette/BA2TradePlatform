# Live-only margin: preserve the unlevered backtest reference

Date: September 9, 2026. Status: **plan only; no implementation changes made**.
Source: [margin TP/SL review](../../reports/margin/margin_tp_sl_review_2026-09-09.md).
Reviewed baseline: 512345311d06a3c26aa7c6784ec3b6c98433865a.

## Final user requirement

**Leverage support is live-only. Backtests stay unlevered and provide the existing
strategy reference.** Live $2,000 with broker multiplier 2 and configured factor
2 must behave like the equivalent $4,000 unlevered account, given the same other
decision inputs and applicable constraints.

The user confirmed current-equity sizing:

| Live real equity | Broker multiplier | Factor | Effective capital | Unlevered comparison |
|---|---:|---:|---:|---:|
| $2,000 | 2 | 2 | $4,000 | $4,000 |
| $1,900 | 2 | 2 | $3,800 | $3,800 |
| $2,100 | 2 | 2 | $4,200 | $4,200 |

After the same $100 loss, an initially $4,000 unlevered account has $3,900,
whereas an initially $2,000/2x live account has $3,800 effective capital. That
difference is accepted: compare matched **current** capital. Do not introduce
a fixed starting-capital anchor or synthetic P&L ledger to force the two
differently funded accounts to retain identical compounded trajectories.

This replaces the previous draft proposing backtest leverage. Specifically:

- Keep BacktestAccount's multiplier at 1.0 and its cash-only fill safeguards.
- Add no simulated borrowing, financing engine or margin-call model.
- Use a normal $4,000 backtest as the reference for $2,000-at-2x live at that state.
- Preserve existing backtest numerical outputs as compatibility baselines.
- Honor actual live broker restrictions; the backtest is not permission to bypass them.

## Strategy behavior to preserve

At the live account boundary:

    E = current real account equity, including marked P&L
    L = 1 when margin is off; otherwise min(configured factor, valid broker multiplier)
    C = E × L

Expose effective capital to the existing expert allocation/sizing path once.
Apply expert allocation once. Keep real equity, cash, effective capital,
committed exposure and broker buying power separate. Never multiply quantity or
TP/SL distance again after capital was scaled.

The unlevered backtest remains the reference for strategy/ruleset semantics:
entry conditions, risk-budget resolution, percentage meanings, allocation logic,
stop synthesis, costs and rounding. Use the shared algorithms with normalized
inputs rather than adding a second live implementation.

A $100 long entry with TP +10% and SL -5% retains $110/$95 at either funding
level; shorts retain $90/$105. Quantities and protective quantities must also
match when the relevant capital and remaining inputs match. Actual return
reporting still uses real equity: a $40 loss is 1% of $4,000 and 2% of $2,000.

Stock leverage does not authorize borrowing for option premiums. Preserve the
option-specific buying-power/reserve rules. The $4,000/$2,000×2 comparison assumes
the traded instrument is eligible for the capacity being compared.

## 1. Freeze the existing unlevered backtest reference

Capture deterministic current-code outputs with fixed data, expert settings,
rules, schedules, fill assumptions and costs. Include existing capped sizing
recipes and funded/unfunded entries. Record code/settings/cache fingerprints.

Compare semantic outputs: orders, quantities, TP/SL, exits, account state and
score/equity results. Ignore generated IDs and wall-clock log times. Rerun the
same fixtures after the live leverage changes and require no unexplained change.
Do not regenerate goldens simply because a changed implementation fails.

**Done when:** reproducible reference outputs exist before modifying code.

## 2. Add three-way sizing parity tests

Compare ordinary **BacktestAccount $4,000**, **live-shaped $4,000 margin off**,
and **live-shaped $2,000 with multiplier/factor 2**. Exercise real shared/adapter
methods; do not stub virtual balance to the expected answer.

Use identical market data, clock, rules, recommendations/tool intents, positions
and pending orders. Freeze Smart RM tool intents so model randomness does not
replace the deterministic sizing test. Compare budgets, instrument limits,
available capacity, action/refusal reason, quantity, TP/SL and protective quantity.

Also compare the two live paths directly. This separates a new leverage defect
from a pre-existing live/backtest discrepancy.

| Scenario | Required check |
|---|---|
| Flat $4,000 reference versus $2,000×2 live | Same sizing inputs/orders; factor applied once |
| $1,900×2 versus $3,800 reference | Recompute current capital without a starting anchor |
| $2,100×2 versus $4,200 reference | Same strategy behavior at the updated capital |
| 25%, 50%, 100% expert allocations | Apply allocation once with the reference semantics |
| Already invested; multiple entries; profitable/losing longs/shorts | No hidden divergence after the first entry |
| Distinct stop and sizing-risk settings | Reference budget resolver used consistently |
| Notional sizing and increase/decrease actions | Same percentage meanings and rounding |
| TP/SL, trailing changes and fixed partial exits | Same prices and correct protective quantities |
| Pending entry, partial fill, cancel/retry/restart | Reserve/release once, no double counting |
| Margin off; lower multiplier than factor | Correct factor and unchanged unlevered behavior |
| Lower BP or ineligible instrument | Explicit real constraint, never a fabricated capacity |
| Several experts/manual holdings/concurrent entries | The same live capacity cannot be spent twice |
| Missing/non-finite input; zero capacity | Explicit refusal; risk-reducing exits remain possible |
| Existing capped recipes | No silent redefinition or multiplication of the cap |

For P&L tests, construct matched-current-state references at $3,800/$4,200 as
appropriate. Do not assert that a single fixed $4,000 backtest trajectory stays
identical to $2,000/2x live after identical dollar P&L; the user chose otherwise.

**Done when:** quantities and post-entry capital are tested, with baseline
discrepancies separated from margin-specific regressions. Side/HOLD tests alone
do not establish parity.

## 3. Normalize live capital at the account boundary

Complete the effective-capital calculation in the live account-facing contract,
using shared pure helpers where useful. Retain real meanings for broker equity
and cash. Supply a coherent as-of snapshot of capital, positions, pending
reservations and settings; avoid changing equity/factor halfway through a decision.

Validate finite inputs centrally. Missing equity/multiplier fails visibly; zero
BP is a measured zero. Preserve margin-off behavior. Feed the existing shared
strategy/rule code normalized inputs rather than adding leverage branches to
individual experts or TP/SL actions.

Trace all consumers: MarketExpertInterface, classic RM, SmartRiskManagerToolkit,
allocation actions, balance/share conditions and final validation. Changing
get_virtual_balance alone is insufficient if other readers see different bases.

**Done when:** equivalent live $4,000/1x and $2,000/2x states agree, and the
leverage changes do not modify the unlevered backtest reference outputs.

## 4. Preserve real live capacity enforcement

Keep strategy intent distinct from broker execution permission. Enforce the live
expert/account ceiling and relevant broker BP with current positions and pending
commitments counted once. Coordinate concurrent account entries and include
other experts/manual positions. Permit reductions and protective exits when no
new-entry capacity remains.

A tighter real broker limit may restrict a backtest-sized intent. Log the
intended quantity, binding constraint and allowed/refused quantity. Different
constraints are different inputs; do not bypass a broker limit for a green test.

Keep audit recommendations that change shared historical sizing separate from
this result-neutral leverage feature. Do not silently revise allocation formulas
under the claim that historical results are unchanged.

**Done when:** equivalent nonbinding constraints yield equal strategy sizing,
while actual restrictions are enforced and explained.

## 5. Keep the reference settings and expose the capital mapping

Keep backtest payloads/mode unlevered. Live margin remains an account setting:
no leverage gene or simulated-broker configuration is added to research drivers
or backtests for this task.

Show the deployment mapping: reference capital, current raw live equity, factor,
broker multiplier, C, expert allocation and remaining capacity. Copy expert
rules/settings/schedules unchanged and validate account capital separately.

Do not redefine the existing backtest equity cap in this feature. A fixed-cap
reference needs an equivalent effective sizing limit live if capped parity is
claimed. If that limit is unsupported live, record a separate parity gap instead
of silently compounding or multiplying the cap. Real equity remains visible to
financial reporting and broker controls.

**Done when:** the original strategy remains identifiable and unchanged, and a
live decision explains how its equivalent capital was derived.

## 6. Resolve existing parity blockers explicitly

Finding 6 already reproduces a mismatch without leverage: after a $1,000 purchase
from $4,000 at zero P&L, shared expert methods report **$4,000 virtual/$3,000 free**
through live equity but **$3,000 virtual/$2,000 free** through the backtest cash
accessor. This is existing compatibility debt, not a reason to add backtest leverage.

Pin it as a failing parity fixture. Do not silently change the backtest getter,
copy double subtraction into live or claim full parity while the fixture fails.
Prefer a live-only compatibility correction where it preserves correct accounting
and reference behavior. If resolution requires changing the shared/backtest
capital contract, isolate it as a separately reviewed/versioned correction with
result comparisons. Until resolved, the full parity claim remains blocked;
unchanged baselines and truthful accounting both remain requirements.

Handle the other findings with the same discipline:

- Smart RM can use the classic/backtest risk-budget resolver on the live side
  without altering classic backtest outputs; test unequal risk/stop settings.
- Switching classic RM's instrument denominator from remaining funds to full
  virtual capital changes historical sizing. Keep it outside this feature.
- Correcting used-exposure accounting needs explicit compatibility evidence and
  scope because it can change position sizes.
- Rejecting a non-finite live broker multiplier is directly in scope and should
  not affect valid unlevered fixtures.

**Done when:** each pre-existing discrepancy has a documented resolution or is
an explicit release blocker, with no hidden historical result changes.

## 7. Strengthen the release gate

Extend the existing parity-and-coverage CI workflow with the unchanged unlevered
references and three-way matched-state tests. Its current equity golden fixture
checks funded side/HOLD behavior on a flat account; add quantity, bracket and
post-entry capital assertions.

Run affected shared/live tests and the existing backtest suite. Follow with
deterministic paper-account replay before live rollout, retaining trace comparisons
and explicit broker-constraint differences.

**Complete only when:** backtest reference outputs are unchanged by the leverage
feature; equal effective capital and equal remaining inputs yield equal rules,
sizes and protections; no unresolved parity defect is described as passing.
Backtests remain unlevered throughout.

## Evidence available now

The audit recorded 244 existing targeted tests passing. Its extended hermetic
probe covers 13 scenarios: four TP/SL/risk combinations, six issue scenarios and
three current-equity equivalence cases. See the
[recorded results](../../reports/margin/reproductions_2026-09-09.json).
These are baseline observations, not evidence that this plan is implemented or
that the new three-way gate passes. No application or production state was changed.
