# Earnings Drift replacement review — $1,000-capped runs

## Recommendation

**Follow-up after checking individual winners:** the current small-cap 1423 has a credible positive-skew payoff distribution. Its 71 winners average $25.22 (median $23.75), versus an average loss of $3.39 across 298 losses. The top five winners represent 19.2% of gross winning P&L, although they represent 44.1% of net P&L after losses. Removing those five profits arithmetically still leaves $435.84 net profit; this is a sensitivity calculation, not a rerun. CNR at $150.08 is a clear outlier (next winner $52.77), but profitability does not disappear without it. The earlier top-five/net statistic overemphasized fragility when presented alone. **There is no compelling winner-distribution reason to replace 1423; retain it as a credible current choice.** 1406's case is specifically lower capital occupancy, not a superior overall strategy.

**BT 1406, mid-cap Earnings Drift S1 ATR (source BT 1088), is the best available alternative for a capital-sharing portfolio. It is not an unqualified improvement over the currently deployed small-cap BT 1423.** Its advantage is rapid turnover and low average capital occupancy, not greater profit or uniformly better risk-adjusted results.

If the objective is to free capital for the two DS sleeves and Insider, shortlist 1406 for a common-account replay. If the objective is only lower drawdown, compare it with a smaller allocation to the current strategy before replacing anything. No production settings were changed.

## Scope and methodology

Read the backtest database at `C:/Users/basti/Documents/ba2/test/dl_forecasting.db` through a read-only SQLite transaction. Scanned the saved strategy parameters and identified four FMPEarningsDrift rows with `equityCap=1000`; all four are completed and cover 2020–2025. The comparison holds the other three capped backtests fixed: 1407 (large DS), 1420 (mid Insider), 1409 (mid DS).

The database's capped equity curves are synthetic scoring curves. Recovered each actual period dollar P&L as `1000 × (E[t]/E[t-1] - 1)` before combining sleeves. For all seven included runs, reconstructed P&L reconciled to the closed-trade ledger within $0.01 and reconstructed drawdown reconciled to the saved metric within 0.011 percentage points. The stored scoring return is not treated as realized cash profit.

Portfolio comparisons use four independent $1,000 capital denominators, with the fourth held as zero-interest cash for the no-Earnings benchmark. They do not simulate a common broker account, order contention, changed share rounding or altered deployment weights. The saved rows' initial-capital metadata is $10,000; a $1,000 sizing cap does not establish that every fill was feasible on a literal standalone $1,000 cash balance.

## All available candidates

| Capped BT | Source BT | Configuration | Net dollar P&L | Max DD / $1,000 cap | PF | Win rate | Trades | Mean holding days |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| **1406** | 1088 | Mid S1 ATR TOP1 | **$462.04** | **-10.00%** | 1.48 | 70.38% | 260 | **2.85** |
| 1418 | 1275 | Mid S1 notional TOP1 | $300.45 | -11.99% | 1.34 | 69.37% | 222 | 2.90 |
| **1423 — current** | 1363 | Small S2 notional TOP1 | **$779.92** | -17.39% | **1.77** | 19.24% | 369 | 57.14 |
| 1424 | 1364 | Small S2 notional TOP5 | $632.59 | -33.74% | 1.59 | 41.43% | 280 | 41.44 |

P&L is cumulative over six years, not annual. Holding durations are elapsed calendar days for closed trades.

- **1406 versus 1418:** 1406 has more profit, less standalone drawdown, higher PF and less top-trade concentration. The notional candidate has slightly lower combined portfolio drawdown, but its return sacrifice and concentration make it less compelling overall.
- **1424 versus current 1423:** lower profit, almost twice the standalone drawdown, and substantially greater dependence on a few winners. Its combined portfolio also fares worse on drawdown. I would not choose it as the replacement.
- **1406 versus current 1423:** lower profit and lower PF, but substantially shorter holding periods and less capital tied up. A 70% win rate is not intrinsically better than 19%: the payoff sizes differ.

## Fit with your other three experts

| Fourth sleeve | Combined net P&L | Combined DD / $4,000 cap | Worst daily P&L | Correlation with the other three's daily P&L |
|---|---:|---:|---:|---:|
| Cash, no Earnings | $3,487.87 | -7.20% | -$74.40 | — |
| **Mid ATR 1406** | **$3,949.91** | **-9.09%** | **-$85.77** | **0.263** |
| Mid notional 1418 | $3,788.33 | -8.80% | -$85.77 | 0.258 |
| **Small current 1423** | **$4,267.80** | **-9.62%** | **-$91.42** | **0.408** |
| Small TOP5 1424 | $4,120.46 | -10.71% | -$96.49 | 0.355 |

Swapping current 1423 for 1406 sacrifices **$317.89** of net profit over the sample and improves combined maximum drawdown by only **0.53 percentage points**. Mean P&L on the worst 5% of aligned daily observations improves from -$47.49 to -$42.99. These are historical descriptive statistics, not an out-of-sample validation or a formal tail-risk estimate.

**Lower correlation does not mean less same-name exposure.** The mid-cap ATR candidate had overlapping closed-trade holding intervals in 11 symbols with Insider and 15 with mid DS, versus none found for the current small-cap strategy. Some names occur in both overlap lists. This matters for production's shared capital and position limits. The measurements do not include unresolved-position histories, issuer/share-class consolidation or pending-order reservations.

## The actual advantage: capital efficiency

| Candidate | Average entry-cost exposure proxy | Best five trades / net P&L | Best five issuers / net P&L |
|---|---:|---:|---:|
| **1406** | **$71.48** | 27.6% | 51.2% |
| 1418 | $62.60 | 41.9% | 73.6% |
| **1423 current** | **$440.00** | 44.1% | 45.9% |
| 1424 | $414.81 | 63.5% | 68.4% |

The proxy is `sum(entry_price × shares × holding_days) / full sample days` for closed trades. It excludes mark-to-market changes, reserves and unresolved-position intervals; it is not actual daily buying-power use. The current row reports 14 end-of-run open positions, while 1406 reports none, so full historical exposure reconstruction remains a limitation.

On that proxy, 1406 retained **59.2% of current net profit while using 16.2% of current capital-days**—roughly **3.65 times the profit per dollar-year of entry-cost exposure**. That supports its use as a short-lived opportunity sleeve sharing capital with longer-lived strategies. It does not demonstrate that freed cash will actually be redeployed profitably.

The median holding time drops from 16 days to 1.125 days. This exposes the candidate more directly to precise entry/exit execution and transaction costs; a short profitable round trip may have less room for additional slippage than a long-held large winner.

## Do not confuse lower size with better strategy

A retrospective check scales the current 1423 dollar-P&L path to **59.24%**, so its total profit equals 1406's. Holding the other three paths fixed:

- Scaled current strategy: **8.44%** combined cap-denominated DD, estimated mean entry-cost exposure **$260.66**.
- Mid ATR 1406: **9.09%** combined DD, mean entry-cost exposure **$71.48**.

Therefore 1406 does **not** beat simple downsizing on maximum drawdown at matched profit in this sample. It still uses much less capital. This is an attribution check only: the scale was chosen using realized sample profit, and fractional scaling ignores whole-share constraints and different fills. It is not a tradable allocation rule.

## Consistency and rules

Standalone annual dollar P&L:

| Candidate | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
|---|---:|---:|---:|---:|---:|---:|
| **1406** | -$14.40 | $34.70 | $62.63 | $80.42 | $227.89 | $70.78 |
| 1418 | -$74.62 | $45.61 | $24.81 | $81.02 | $176.24 | $47.39 |
| **1423 current** | $171.56 | $131.80 | $65.04 | $47.29 | $198.98 | $165.25 |
| 1424 | $234.32 | $170.12 | -$119.89 | -$57.08 | $261.71 | $143.40 |

About 49% of 1406's standalone net profit occurred in 2024. Its five best issuers supply 51% of net profit. These concentration measures argue against describing it as uniformly dependable. Current 1423 has more evenly distributed annual positive P&L despite its weaker trade-count win rate and greater top-trade dependence.

1406 is an existing rules-based strategy, so it fits the user's scope without adding new engine capabilities. Its saved settings include positive surprise threshold 10%, report age up to 30 days, static expected-profit assumption 19%, ATR sizing and a 25% per-instrument cap setting. Its medium-risk entry branch uses a TP 14% below the expert target and an entry SL 16% below the entry reference; another bullish/flat branch has no explicit bracket. Management closes after >150 days and otherwise applies a -14% entry-relative stop. The RM safeguard can be tighter.

The observed 2.85-day mean is **not a three-day timeout guarantee**. The active time exit is 150 days. Where target price equals 1.19 times the reference price, a 14% target discount gives approximately `1.19 × 0.86 = 1.0234`, only 2.34% above that reference before regime effects and execution differences. Check branch-specific exits and fees when validating the short holding times.

Do not deploy 1406 blindly at the old 80% virtual-equity setting: its per-instrument setting is 25%, versus current 1423's 10%. With full virtual balance initially available, that changes the notional upper bound from roughly 8% to 20% of account equity before other constraints. Reduced average holding time does not mean smaller individual positions or less overlap.

## Decision

**Shortlist 1406 for the fourth slot if capital-sharing is the priority. Keep 1423 as the reference and compare a smaller allocation to it.** Reject 1424 as an improvement on current evidence; 1418 is a lower-return secondary alternative, not the leading choice.

The next useful test is a common-account replay of (a) current 1423, (b) 1406 and (c) a preregistered smaller 1423 allocation, using explicit weights, the same reserve, current fees, actual order priorities and issuer-conflict handling. Follow with unused-period evaluation; all four candidates and the present comparison use the same selected 2020–2025 sample. No claim is made that future returns or portfolio drawdowns will match these results.

Artifacts: `reports/earnings_drift_1000_comparison.py` reproduces the read-only extraction and calculations; `reports/earnings_drift_1000_comparison.json` stores compact metrics and the compared Earnings Drift recipes. Production and backtest databases were not modified.
