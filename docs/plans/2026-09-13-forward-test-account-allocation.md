# Forward-test account allocation — revision 4, 2026-09-14

**Selection complete: 26 settings across 13 retained expert/band groups, two per group.** Each setting receives **10% of its test account**. Account allocations are **100% / 80% / 80%**. Small-cap FactorRanker remains excluded.

**Labels: applied and independently verified at 2026-09-14T07:58:54.831762+00:00.** This finalizes the selection plan and research labels; deployment remains a separate action.

The completed grid has no active goal2020 jobs. The latest-name catalogue contains 135 completed jobs plus 13 cancelled and one failed historical job. Final jobs 518–521 are completed. The scheduled follow-up **Finish grid allocation** (`finish-grid-allocation`) was deleted at the user’s request; this review was performed immediately.

## Capital rule

Set `virtual_equity_pct = 10.0` for every planned instance. Its pair receives 20%; each setting may use its full sleeve under its existing optimized sizing, instrument limits and broker headroom. Keep the account and expert multipliers consistent with these equity budgets. The 10% allocation does not replace the optimized per-instrument limit.

At $10,000 account equity, a sleeve starts at $1,000. The source results below start at $10,000 **per standalone backtest**; they must not be presented as the performance of a $1,000 sleeve or of the assembled account. Accounts 2 and 3 each retain 20% unassigned cash. No account relies on unused allocations in another sleeve to fit the budget.

## Final account placement

| Account | Expert / band | Source backtests | Account equity | Status |
|---|---|---|---:|---|
| 1 | FMPRating / large | 1182 + 1434 | 20% | Selected; review 1434 |
| 1 | FMPRating / mid | 1070 + 1583 | 20% | Selected |
| 1 | FMPRating / small | 1508 + 1633 | 20% | Selected |
| 1 | FactorRanker / large | 1065 + 1064 | 20% | Selected; review 1065, 1064 |
| 1 | FactorRanker / mid | 1143 + 1146 | 20% | Selected; review 1143, 1146 |
| 2 | FMPEarningsDrift / mid | 1088 + 1607 | 20% | Selected |
| 2 | FMPEarningsDrift / small | 1367 + 1528 | 20% | Selected; review 1367, 1528 |
| 2 | FMPInsiderClusterBuy / mid | 1298 + 1619 | 20% | Selected; review 1619 |
| 2 | FMPInsiderClusterBuy / small | 1386 + 1681 | 20% | Selected; review 1681 |
| 3 | DeterministicScorer / large | 1017 + 1107 | 20% | Selected; review 1017 |
| 3 | DeterministicScorer / mid | 1030 + 1173 | 20% | Selected |
| 3 | DeterministicScorer / small | 1000 + 1358 | 20% | Selected |
| 3 | FMPSenateTraderWeight / blended | 1439 + 1649 | 20% | Selected; review 1439, 1649 |

**Totals:** account 1: 10 settings / 100%; account 2: 8 / 80%; account 3: 8 / 80%. No pending slots, no oversubscription.

## Changes after the final jobs

- **Small EarningsDrift: 1367 + 1528**, S2 notional plus S6 ATR. CAR **22.79% / 19.28%**, drawdown **19.15% / 23.12%**, average capital use **39.15% / 36.48%**. Their smaller holding set overlaps **18.0%**, same-day entry overlap is **0%**, frequency differs **1.54×**, and weekday return correlation is **0.43**. Both receive review tags: 1367 has **43.8%** top-five-trade concentration; 1528 exceeds 20% drawdown.
- **Small Insider: 1386 + 1681**, S1 notional plus S7 notional. CAR **27.41% / 21.84%**, drawdown **12.51% / 17.52%**, average capital use **10.49% / 16.53%**. Smaller-set holding overlap is **18.1%**, entry overlap **17.9%**, frequency differs **1.78×**, and return correlation is **0.29**. 1681 receives a review tag for **41.8%** top-five-trade concentration. 1386 is the notional version of 1243 with identical saved performance; only one is selected.
- The preceding 22 choices remain. All new grid results belong to the two previously reserved small-cap groups. The separate Senate $2,000 runs are provisional sizing evidence and do not displace the established 1439 + 1649 pair.
- Production provenance changed: enabled Senate instance 13 now cites **1645**, replacing 1634. The current labels already reflect 1645, so both production tags and all original/capped copies are preserved.

## Review flags and alternatives

CAR/profit is the priority at comparable capital usage. The previous 40% top-five-trade and 20% drawdown screens now flag trade-offs instead of silently excluding higher-return settings. This revision also consistently tags selected results with at least 60% of net profit from their five largest issuers. These are research diagnostics, not modifications to strategy or risk limits.

- **EarningsDrift concentration alternative:** 1364 has 18.77% CAR, 18.01% DD, 35.94% average usage and 37.8% top-five concentration. It can replace 1367 if staying below 40% is more important than the **4.02-point CAR advantage** of 1367. Do not pair them: they overlap heavily.
- **EarningsDrift drawdown alternative:** 1518 (17.94% CAR, 14.24% DD, 37.88% average usage) can replace 1528. The retained 1528 earns 1.34 CAR points more at similar usage, with lower pair correlation and less trade overlap, but **8.88 points more drawdown**. 1518 itself has 41.0% top-five concentration. The new S7 1666/1667 settings use about 47% capital for lower CAR and do not improve this CAR-first pair.
- **Insider concentration alternative:** 1683 gives 17.63% CAR, 15.47% DD and 39.5% top-five concentration at 19.40% average usage. Retaining 1681 gains **4.21 CAR points** and uses less capital, in exchange for **2.28 points more top-five concentration** and 2.05 points more DD.
- **FactorRanker large 1065 + 1064:** reasonable individual returns, but 83.9% smaller-set holding overlap and about 0.94 return correlation. They remain two related variants, both tagged for review.
- **FactorRanker mid 1143 + 1146:** only 4.43% / 6.19% CAR, 59.8% / 71.6% top-five-trade concentration, 100% smaller entry overlap. Both slots remain for review under the instruction to fill two. Small FactorRanker stays dropped.
- **Issuer dependence:** 1017, 1434, 1439, 1619 and 1649 receive review tags for top-five-issuer concentration of roughly 62–74%, despite passing the five-trade screen. Mid Insider 1619 is also weak at 7.29% CAR versus 13.22% drawdown.
- **Capital-measurement limit:** the four FactorRanker saved trade summaries aggregate multiple fills, so the single-fill cash reconstruction cannot measure their utilization. Large FactorRanker also has $36.50 / $19.37 differences between saved equity profit and ledger profit for 1064 / 1065. Capital usage is left unavailable; stored backtest metrics are preserved. This review does not establish the cause of the residuals.

## Labels, evidence and remaining validation

- `ForwardTestCandidate`: **26** selected sources. `ForwardTestReview`: **12** of them, with the concerns above. A review tag keeps the slot in the plan; it is not deployment approval.
- `ForwardTestProd`: seven current production sources **1088, 1107, 1173, 1298, 1330, 1363, 1645**. Production configuration is read only.
- The exact before/after labels, full non-label fingerprints for affected rows and the six original/capped Senate experiments, application receipt and independent verification are in `reports/strategy_research/forward_selection_2026-09-14-v4/`. Previous revisions are preserved.
- Full profit/CAR/DD, capital usage, concentration, yearly returns, pair comparisons and rejected alternatives: [final selection report](../../reports/strategy_research/forward_test_selection_2026-09-13.md).
- Before deploying, confirm actual test-account equity and multipliers, and validate separate copies at the resulting sleeve budgets. Whole-share sizing makes proportional scaling unreliable. A combined account replay is still needed to quantify aggregate drawdown and cash contention; individual CARs must not be averaged into an account forecast.
- Senate 1679/1680 remain provisional because of missing trader-history cache dependencies; see [the $2,000 comparison](../../reports/strategy_research/senate_ok2000_2026-09-14/report.md).
