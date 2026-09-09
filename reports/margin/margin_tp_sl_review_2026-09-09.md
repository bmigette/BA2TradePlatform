# Margin merge: expert sizing and percentage TP/SL review

Reviewed September 9, 2026. Margin merge: `a6014e18` (`feat/margin-trading`).
Current checkout reviewed and probed: `512345311d06a3c26aa7c6784ec3b6c98433865a`.
This includes the subsequent header/buying-power UI changes.

## Confirmed requirement and implementation plan

**Leverage support is live-only. Backtests stay unlevered and provide the strategy
reference.** Live sizing must use current equity multiplied by effective leverage
so an equivalent unlevered backtest produces the same strategy behavior. The user
confirmed current-state equivalence:

| Current real equity | Broker multiplier | Configured factor | Effective stock capital | Equivalent unlevered account |
|---|---:|---:|---:|---:|
| $2,000 | 2x | 2x | $4,000 | $4,000 |
| $1,900 | 2x | 2x | $3,800 | $3,800 |
| $2,100 | 2x | 2x | $4,200 | $4,200 |

For equal effective capital and the same market data, settings, positions,
pending orders and applicable broker constraints, strategy decisions, quantities
and TP/SL levels must match. After the same $100 loss, an initially $4,000
unlevered account sizes on $3,900; an initially $2,000/2x account sizes on $3,800.
That difference is intended: the correct comparison is now $3,800 unlevered.
Do not create a fixed starting-capital baseline to force equal P&L trajectories.

The [detailed implementation plan](../../docs/plans/2026-09-09-margin-live-backtest-parity.md)
sets the following order and acceptance gates:

1. Freeze existing unlevered backtest outputs as the compatibility reference.
2. Add three-way tests: backtest $4,000, live $4,000/1x and live $2,000/2x;
   compare quantities, budgets and protections through matched current states.
3. Normalize live effective capital at the account boundary and retain shared
   strategy/rule/sizing code. Apply the live leverage factor exactly once.
4. Preserve real broker checks and make additional live exposure safeguards
   explicit; do not modify historical sizing behavior incidentally.
5. Keep raw live equity separate from its effective strategy allocation and
   record the mapping from unlevered backtest capital to live capital.
6. Resolve existing parity defects, including finding 6, separately from the
   leverage feature. They cannot be hidden by a passing first-entry test.
7. Gate release on unchanged backtest reference outputs and matched-state live
   sizing/bracket parity. Do not introduce leveraged backtest accounts.

**Status: plan only, not implemented.** The user's final scope explicitly keeps
the backtest multiplier at 1.0 and its cash-only execution model. No simulated
borrowing, margin calls or financing engine is planned. Existing passing tests
do not establish the stronger sizing parity guarantee.

## Answer to the TP/SL question

**The basic margin-to-expert sizing connection works. Percentage TP/SL prices
remain percentages of the chosen reference price; their dollar effect increases
through the larger share quantity. They are not percentages of account equity.**

For stocks, the intended calculation is:

```text
effective leverage = min(configured margin factor, broker multiplier)
expert virtual balance = account equity × effective leverage × expert allocation %
position risk budget = expert virtual balance × sizing risk budget %
shares = floor(position risk budget / abs(entry price - stop price))
```

Margin disabled gives effective leverage 1.0. Cash accounts are treated as 1.0.
Available funds, per-instrument limits, commissions and lot rounding can reduce
the resulting share count. The issues below concern those surrounding limits.

For the ordinary percentage actions, a long TP of +10% and SL of -5%, both
relative to a $100 entry, remain **$110 and $95** at either leverage setting.
The short equivalents are $90 and $105. Minimum-distance enforcement and the
separate regime overlay still apply; the margin factor is not a price-distance
multiplier.

Actual mocked-method results with a $10,000 account, a 100% expert allocation,
a 1% sizing risk budget and nonbinding position/buying-power caps:

| Margin | Virtual balance | Shares | Long TP / SL | Short TP / SL | Loss at stop, before execution costs |
|---|---:|---:|---|---|---:|
| Off | $10,000 | 20 | $110 / $95 | $90 / $105 | $100 |
| On, factor 1.8 | $18,000 | 36 | $110 / $95 | $90 / $105 | $180 |

Thus **1% risk against the margin-adjusted virtual balance is 1.8% of the real
equity allocated to that expert** at 1.8x. This is consistent with the merge's
design and the user's confirmed requirement. Risk sizing must continue to use
effective virtual capital; using an unlevered risk denominator would break the
$4,000 versus $2,000-at-2x equivalence.

Code paths checked:

- [MarketExpertInterface](../../packages/common/ba2_common/core/interfaces/MarketExpertInterface.py),
  lines 863–873: virtual balance uses account tradable balance and expert allocation.
- [TradeActions](../../packages/common/ba2_common/core/TradeActions.py), lines
  1025–1033 and 1159–1163: direction-aware percentage-to-price calculations.
  Lines 1539 and 1750: increase/decrease allocation targets use virtual balance.
- [TradeRiskManagement](../../packages/common/ba2_common/core/TradeRiskManagement.py),
  lines 1320–1358: classic risk sizing uses the margin-adjusted virtual balance.
- [AccountInterface](../../packages/common/ba2_common/core/interfaces/AccountInterface.py),
  lines 1318–1330: the final position-size validator also scales the equity base.
- [TransactionHelper](../../packages/common/ba2_common/core/TransactionHelper.py),
  lines 275–303: ordinary protective quantities follow entry quantity; explicitly
  fixed partial-exit quantities retain their intended exception. Existing tests
  for protective quantity synchronization and entry brackets passed.
- Option entries deliberately use **option** tradable balance at TradeActions
  line 2324. All currently supported adapters use option multiplier 1.0, so
  enabling stock leverage does not multiply option-entry budgets by 1.8.

## Findings

### 1. [P1] Smart RM ignores the separate sizing risk-budget setting

**Scope: existing mismatch, amplified by leverage; not introduced by this merge.**

[SmartRiskManagerToolkit](../../ba2_trade_platform/core/SmartRiskManagerToolkit.py)
line 1885 reads `risk_per_trade_pct` for sizing. The classic manager instead
prefers `atr_risk_budget_pct` at TradeRiskManagement lines 1330–1332. The expert's
settings explicitly distinguish the sizing budget from the stop-distance gene,
and describe sizing mode as applying to both managers.

Reproduction: virtual balance $18,000, price $100, SL $95,
`atr_risk_budget_pct=0.5`, `risk_per_trade_pct=5`, a 100% per-instrument cap,
and sufficient buying power. Classic sizes **18 shares**, risking **$90**;
Smart RM sizes **180 shares**, risking **$900** at the same stop. Smaller
position caps can mask or reduce this difference; it requires the separate
budget to be set and the Smart RM auto-sizing path to run.

**Recommendation:** share the sizing-budget resolver between classic and Smart
RM, retain the stop-distance setting for stop synthesis, and test both managers
with unequal budget/stop settings under margin. Review Smart RM's explicit-size
stop synthesis too: it also uses `risk_per_trade_pct` as the dollar-risk budget
at lines 2011–2015.

### 2. [P1] Profitable positions overstate the remaining exposure allowance

**Scope: existing used-balance accounting, incompatible with a strict current
notional ceiling. Reproduced with one expert and no external positions.**

[MarketExpertInterface](../../packages/common/ba2_common/core/interfaces/MarketExpertInterface.py)
lines 1122–1124 charge profitable positions at entry cost. Virtual balance,
however, is derived from current account equity, which already includes the
unrealized profit. Subtracting historical cost fails to account for the increase
in the existing position's current notional value.

Reproduction: buy 180 shares at $100 on a $10,000 account, then price rises to
$110. Equity becomes $11,800 and the 1.8x ceiling is **$21,240**. Existing marked
exposure is **$19,800**, leaving **$1,440** of ceiling headroom. The expert instead
reports **$3,240** available: an overstatement of **$1,800**. A broker BP clamp
of $3,800 does not correct it. Subsequent purchases in other instruments can
therefore exceed the platform's stated ceiling even when expert allocations sum
to exactly 100%.

**Recommendation:** compute exposure headroom from current gross position value
and pending entry commitments. Preserve any deliberately conservative loss
reserve as a separate constraint instead of treating entry cost as current
notional exposure. Test profitable longs, shorts, partial fills and multiple
symbols.

### 3. [P2] The account margin ceiling is a warning, not an aggregate entry gate

**Scope: an explicit design limitation in the margin feature, not an accidental
change to the allocator. It also matters with oversubscribed expert allocations.**

[ReadOnlyAccountInterface](../../packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py)
lines 479–490 only log when broker BP indicates over-exposure; they still return
the normal tradable amount. MarketExpertInterface lines 936–951 subtract only
the current expert's positions and clamp against remaining broker BP. They do
not subtract other experts' or manual positions from the platform-wide ceiling.

Reproduction: equity $10,000, configured ceiling $18,000, other positions already
worth $18,000, broker remaining BP $2,000. An empty expert still reports
**$2,000 available**, although the configured account ceiling has **zero**
headroom. A new order can cross 1.8x while remaining within the broker's 2x
limit. This is a capacity-read reproduction, not a submitted broker order.

The design already leaves the portfolio allocator outside the margin factor.
That scope decision should remain visible; the general claim that the account
can deploy "at most balance × factor" is stronger than the enforcement delivered.

**Recommendation:** add a shared account exposure/reservation gate before new
entries and additions, separately from expert allocation and broker BP. Include
all relevant positions and outstanding entries, and coordinate concurrent
submissions. Do not make reducing/closing positions depend on spare entry capacity.

### 4. [P2] Classic RM's percentage instrument ceiling uses remaining funds

**Scope: pre-existing sizing inconsistency; conservative under-allocation rather
than an oversized trade. Relevant to comparisons with optimized rules.**

[TradeRiskManagement](../../packages/common/ba2_common/core/TradeRiskManagement.py)
lines 347–353 assign `get_available_balance()` to `total_virtual_balance`, then
multiply that by `max_virtual_equity_per_instrument_percent`. This differs from
allocation actions and final validation, which use the full virtual balance.

Reproduction: $18,000 virtual balance, $9,000 already invested, 10% per-instrument
limit. Classic RM computes a **$900** ceiling and buys **9 shares** of a new
$100 stock. A 10% ceiling on the expert's virtual allocation is **$1,800**,
independently constrained by the $9,000 remaining funds. The effective cap
shrinks as other positions consume the sleeve or broker buying power falls.

**Recommendation:** use virtual balance for the per-instrument denominator and
available balance for the separate affordability constraint. Because the old
behavior is shared with backtests, changing this requires rerunning affected
strategy results; it should not be presented as a numerically neutral cleanup.

### 5. [P2] A non-finite broker multiplier can authorize the configured leverage

**Scope: validation gap in the new margin code; requires an invalid broker value.**

[ReadOnlyAccountInterface](../../packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py)
lines 349–353 reject missing, zero and negative multipliers, but accept `NaN`.
The `min`/`max` expression in `effective_factor_for` then returns the configured
factor for this argument order.

Reproduction: a snapshot multiplier of **NaN**, factor 1.8 and equity $10,000
produce **$18,000 tradable balance** instead of refusing to size. This does not
establish that a supported broker currently sends NaN; it establishes that the
new guard does not reject all invalid numeric values. The stored margin factor
already has a finite-number check, which the broker multiplier lacks.

**Recommendation:** validate finiteness and the permitted range of broker
multipliers before using them. Apply corresponding finite checks to equity and
buying power, while preserving legitimate zero/negative-balance semantics.

### 6. [P1] Backtest cash and live equity feed the same virtual-balance method

**Scope: existing live/backtest contract mismatch, confirmed during the planning
follow-up. A blocker for the user's stronger parity requirement.**

[BacktestAccount](../../testplatform/backend/app/services/backtest/backtest_account.py)
lines 1821–1833 return remaining cash from `get_balance()`; live Alpaca and
TastyTrade return account equity. The shared virtual-balance method obtains
tradable balance, whose `_plain_balance()` still calls that ambiguous accessor.
The expert then subtracts its positions from the result.

Reproduction at zero P&L after buying $1,000 of stock from a $4,000 account:

| Value | Live equity accessor | Backtest cash accessor |
|---|---:|---:|
| Actual equity | $4,000 | $4,000 |
| Cash | $3,000 | $3,000 |
| Expert virtual balance | $4,000 | **$3,000** |
| Expert available balance after subtracting the position | $3,000 | **$2,000** |

This calls the real BacktestAccount cash accessor and shared expert methods with
mocked state. The position is effectively charged twice in the backtest path,
affecting later entry sizes, risk budgets and balance conditions.

Two other backtest behaviors were inspected and must remain unchanged under the
user's final scope:
`get_account_info()` publishes multiplier 1.0 at line 1878, and `_apply_fill()`
enforces cash-only long entries at lines 4665–4693. These are intentional in the
unlevered reference simulator, not defects to remove for this live-only feature.

**Recommendation:** pin the cash/equity discrepancy as a failing parity fixture
and address it as a separate existing compatibility defect. Do not change the
backtest getter or copy double subtraction into live as an incidental leverage
fix. A fix that changes the backtest's numerical outputs must be versioned and
reviewed separately; otherwise retain the baseline and report the parity blocker.

## Validation and limits

**244 existing targeted tests passed**, across margin accessors/settings,
expert sizing and buying-power clamps, option reserves, TP/SL checks, protective
quantity synchronization, classic candidate sizing, backtest account contracts,
equity caps and entry brackets. See the [validation record](validation_2026-09-09.txt).

The [audit probe](reproduce_margin_review.py) exercises production methods with
mocked broker/store ports. Four margin/direction combinations verify the main
TP/SL behavior; six issue scenarios reproduce the findings above. Three further
cases confirm the accepted current-equity equivalence at $2,000, $1,900 and
$2,100 with 2x leverage. The
[recorded results](reproductions_2026-09-09.json) contain the exact amounts and
reviewed commit. Its assertions document current behavior, including defects;
they are not regression tests asserting that those defects have been fixed.

Git blame confirms the Smart RM budget reader, classic remaining-funds
denominator and profitable-position accounting predate the margin merge. The
new leverage base makes their consequences relevant to this release; this review
does not claim the merge introduced those old lines.

No broker orders, external data requests, production DB writes or application
code changes were made. Tests used isolated data and existing dependencies via
bundled Python. This is a targeted code review, not a full application test run
or a live broker exercise.

The backtest adapter remains unlevered by user decision. Its effective starting
capital is the comparison amount: $4,000 backtest versus $2,000-at-2x live at that
state. It does not model the leveraged account's actual return denominator,
financing costs or broker constraints; the plan adds no such simulation.
IBKR currently publishes no stock multiplier through this interface, so enabling
margin there refuses stock sizing; that is a documented unsupported path.

The price-percentage implementation does not need to multiply TP/SL distances
by leverage. The priorities are consistent risk budgets and enforceable exposure
headroom, followed by denominator consistency and finite-value validation.
