# FMPRating: existing OK1000 runs and next candidates

## Recommendation

**None of the three completed OK1000 FMPRating runs is a compelling production addition on current evidence.** Mid-cap 1405 is the most productive of the three, but its return/drawdown trade-off and dependence on 2025 do not make it an obvious improvement to the current four-expert mix.

**Test source BT 1330 first, then 1071.** If a specifically large-cap alternative is wanted, 1054 is a useful third experiment. These are existing saved goal2020 recipes: no new strategy-engine capabilities or fresh broad optimization are needed. This review identified candidates; it did not launch reruns or change production.

## Scope

Read-only SQLite review of 93 completed FMPRating goal2020/OK1000 rows: 90 original goal2020 runs across S1/S2/S3 and three $1,000-capped runs. FMPRating's reviewed period is **2022–2025**, not the six-year period used for the other experts. Queried settings, rules, trade ledgers and metrics; additionally reconstructed the capped curves for comparison with current BTs 1407, 1420, 1423 and 1409 over their common period.

The original runs started with $10,000 and were not capped at $1,000. Their reported annualized returns and drawdowns do not predict results at the smaller cap. Capped scoring curves were inverted into period dollar P&L before combining; synthetic scoring compounding was not counted as money earned.

## Already in OK1000

| Capped BT | Parent | Configuration | Closed-trade net P&L, 2022–2025 | DD / $1,000 cap | PF | Trades |
|---|---:|---|---:|---:|---:|---:|
| 1405 | 1070 | Mid S1 ATR TOP1 | $339.97 | -16.45% | 1.84 | 124 |
| 1413 | 1178 | Large S1 notional TOP1 | $99.28 | -6.41% | 2.07 | 47 |
| 1414 | 1182 | Large S1 notional TOP3 | $163.84 | -10.12% | 2.29 | 64 |

Closed-trade P&L is labeled explicitly; it can differ from marked equity changes. The stored capped annualized scoring returns are respectively 7.97%, 2.46% and 4.09%.

**1405:** profitable but not compelling. Closed-trade P&L was -$73 in 2022, +$58 in 2023, +$87 in 2024 and +$268 in 2025. Its five biggest wins represent 29.9% of gross winning P&L and 65.6% of net P&L; removing those profits arithmetically still leaves +$117. It is not a one-winner strategy, but the combination of modest net profit and 16.45% DD merits caution.

**1413:** too little remaining evidence at this account size. Only 47 trades and roughly $99 net profit; subtracting its five biggest profits leaves -$16. That sensitivity is a concern, not a simulated rerun.

**1414:** somewhat better than 1413 but still only 64 trades and roughly $164 net profit. Its five biggest winners contribute 46.9% of gross winning P&L and 83.3% of net P&L. It is not a strong reason to add another funded expert.

## Their fit as a fifth independent sleeve

Held the existing four strategy paths fixed, added either each Rating path or zero-interest cash, and used a common $5,000 capital denominator over the aligned 2022–2025 interval. This is attribution, not a broker-account replay.

| Fifth sleeve | Combined P&L in common interval | Combined DD / $5,000 cap | Rating/core daily P&L correlation |
|---|---:|---:|---:|
| Cash | $2,452.50 | -7.70% | — |
| Mid 1405 | $2,790.68 | -8.87% | 0.326 |
| Large 1413 | $2,552.11 | -7.31% | 0.334 |
| Large 1414 | $2,616.51 | -7.32% | 0.336 |

The two large-cap runs slightly cushioned the worst historical trough, but the improvement is small and their independent samples are thin. Mid 1405 increases profits and worsens the largest drawdown. None demonstrates a transformative diversification benefit. These calculations must not be compared directly to six-year four-sleeve totals or to production's overlapping 80% budgets.

## Why the $1,000 cap hurts the tested large-cap recipes

The tested recipes have a 10% per-instrument ceiling. At a fully available $1,000 sizing balance, that is at most **$100 per name**, before remaining-balance reductions and other guards. The engine buys whole shares, so many large-cap stocks become unavailable.

Only 22.6% of parent 1178's historical entry prices fit under $100, and 29.7% of parent 1182's did. This is consistent with trades shrinking from 257 to 47 and 256 to 64 in their capped reruns. It is an explanatory constraint, not a complete causal attribution of the performance drop. A cap changes the investable subset, quantities and timing; it does not simply divide every original order by ten.

## Next $1,000 tests, in priority order

| Source BT | Exact recipe | Original annualized return | Original max DD | PF | Trades |
|---|---|---:|---:|---:|---:|
| **1330** | **TOP1-scr-small-FMPRating-S1-goal2020-notional-from2022** | **28.72%** | **-13.53%** | **2.74** | **387** |
| **1071** | **TOP3-scr-mid-FMPRating-S1-goal2020-riskatr-from2022** | **13.00%** | **-11.71%** | **2.23** | **149** |
| **1054** | **TOP2-scr-large-FMPRating-S3-goal2020-riskatr-from2022** | **11.11%** | **-8.58%** | **2.86** | **147** |

All numbers in this table are **uncapped source results**, not predictions for the proposed runs.

### 1. BT 1330: strongest first test

- Existing notional recipe, with a 15% per-name setting: prospective ceiling $150 at a fully available $1,000 balance. All of its historical filled entry prices fit below that ceiling. This checks only one-share affordability; it does not guarantee the same entries will be taken.
- Strongest return profile among this shortlist, with a substantial number of trades. Average holding time 15.6 days, median 2.0 days.
- Best five trades supply 32.3% of gross winning P&L and 50.9% of net P&L. Subtracting them still leaves about $8,530 of original net profit; one trade supplies 15.3% of net P&L. This is concentration worth testing, not evidence of total dependence on one lucky win.
- Issuer concentration is more significant: ARIS contributes approximately $5,029, or 29% of original net profit across its trades. Profits are heavily weighted to 2024–2025. Those are reasons to require a capped rerun and unused-period validation, not to dismiss the recipe outright.
- It has no active time-exit rule in the saved exit list; the observed short median is not a holding-time guarantee. It also expands small-cap exposure alongside the existing Earnings Drift expert, so diversification should be measured rather than inferred from the different expert name.

### 2. BT 1071: useful contrasting mid-cap recipe

- Lower original return than 1330, but 11.71% original drawdown and 59.7% win rate.
- Best five winners are 22.0% of gross winning P&L; largest single winner is 10.6% of net P&L. Average holding time 17.9 days.
- About 81.2% of original filled entry prices fit its prospective $100 ceiling. More compatible with small-account whole-share sizing than the tested large-cap S1 parents.
- It had positive closed-trade P&L in each of 2022–2025, including 2022; this is not a claim that marked annual returns were uniformly positive.
- **BT 1202 is an economic duplicate in the inspected source results:** its full saved trade-ledger hash exactly matches 1071, although the sizing-mode labels differ. Test one first; do not count both as independent confirmations. A future capped run could still expose sizing-path differences.

### 3. BT 1054: optional large-cap S3 experiment

- A different exit design with a 60-day time exit and profit-triggered stop adjustment, rather than another tested S1 recipe.
- Its best five trades are only 16.0% of gross winning P&L and 24.6% of net P&L; largest winner is 6.1% of net P&L.
- The existing 20% per-name setting permits $200 initially; 64.6% of original filled entry prices fit. This is better coverage than the $100 S1 parents, at the cost of more concentrated positions.
- Mean holding time is 62.9 days and median 61 days, so it is a weaker choice for rapid capital recycling. WMT, NVDA and other large-cap winners also overlap economically with the existing large DS sleeve.
- BT 1194 carries the same full saved trade ledger; it is not a separate independent opportunity.

## Candidates I would defer

BT 1329 (small S1 TOP5) is a reasonable reserve candidate: 23.58% original annualized return, 14.47% DD, PF 2.89 and 259 trades, with all historical entry prices under its prospective $150 ceiling. I would test 1330 first because it has higher original return, lower DD, more trades and less top-five gross-profit concentration. 1329's higher win rate does not by itself make it superior.

Large S2 1049/1050 have attractive original PF/DD, but only about 26–28% of original filled entry prices fit their prospective $100 per-name ceilings. They may repeat the large-cap affordability collapse. Defer them until the cap experiment establishes that enough of their intended trades survive.

## Concrete experiment specification

Create fresh backtest rows from the complete saved, migrated recipes for **1330 and 1071**, change only the equity cap to $1,000, retain the original 2022–2025 dates and costs, and label parentage explicitly. Do not optimize new thresholds or enlarge the per-name cap to rescue performance during this comparison. Optionally add 1054 as a third, different rule-family experiment.

Compare realized dollar P&L, cap-denominated drawdown, annual P&L, gross and net winner concentration, share-affordability rejections, holding-time/capital use, and common-period overlap with the four current experts. Changing dates, allocation or costs creates a separate sensitivity test. A favorable result would justify further validation, not automatically justify another 80%-virtual-equity production instance.

## Artifacts and limitations

`reports/fmprating_1000_review.py` performs the read-only extraction and calculations. `reports/fmprating_1000_review.json` contains compact settings, rules and measurements for all 93 rows. No databases, production configuration or broker state were modified; no new backtests were launched.

This is selection from already searched historical results, not independent out-of-sample evidence. The affordability percentages use historical executed entry prices as an optimistic screening diagnostic, not a replay of all candidate signals. Cap results cannot be inferred by scaling original profits. Market data provenance, current broker availability, live schedules and actual combined-account admission were not independently validated in this review.
