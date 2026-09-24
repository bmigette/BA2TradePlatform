# Forward-test selection — final grid review, updated 2026-09-14

Updated on 2026-09-14 after grid completion. This file remains the current selection report at the user-requested path. The [previous September 13 report](forward_selection_2026-09-14-v4/previous_selection_report.md) is preserved as historical evidence.

**26 candidates selected; all four reserved slots filled.** The current [allocation plan](../../docs/plans/2026-09-13-forward-test-account-allocation.md) contains the decisions, trade-offs and exact account percentages.

**Labels: applied and independently verified at 2026-09-14T07:58:54.831762+00:00.**

## Scope and interpretation

This read-only refresh inspected **137 sources**: all 58 small EarningsDrift and 57 small Insider completed TOP-N results from their latest goal2020 jobs, plus the 22 prior selections. It computed 3,260 within-group pair comparisons. No active latest-name goal2020 job remains; final jobs 518–521 are completed. Cancelled/superseded historical jobs do not block selection. The scheduled follow-up was deleted.

Figures are saved in-sample $10,000 standalone results, 2020–2025 except Rating (2022–2025). Profit is saved final equity minus initial capital; CAR and drawdown are stored result metrics. Top-five concentration is positive net P&L of the five largest bets divided by total net ledger P&L, grouped by transaction where available. Terminal position marks are included once. Issuer concentration separately aggregates net P&L by symbol; issuer names are not normalized across ticker changes.

Average capital use is calendar-day end-of-day gross marked exposure divided by the contemporaneous equity, with carry-forward on missing days. Cash is reconstructed from saved fills/fees and reconciled against ending equity. N/A indicates compressed FactorRanker summaries that do not support this reconstruction. P95 and peak snapshots are in the JSON evidence; average usage does not limit peak funding needs.

Pair overlap uses unique symbol/direction/date entries and weekday holding sets through the final exit, including terminal marks; it is not dollar-weighted and may include exchange holidays. Return correlations align final daily equity marks, carry forward gaps and use weekdays. These metrics describe complementarity; they are not a funded combined-account replay.

## Complete selected-source results

| Account | Expert / band | BT | Profit ($) | CAR | Max DD | Avg capital | Top 5 bets / net | Top 5 issuers / net | Review |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | FMPRating / large | 1182 | 9,024.54 | 17.49% | 11.71% | 31.55% | 28.4% | 43.2% | — |
| 1 | FMPRating / large | 1434 | 9,988.59 | 18.96% | 13.24% | 37.26% | 17.9% | 61.8% | Yes |
| 1 | FMPRating / mid | 1070 | 8,511.27 | 16.69% | 15.24% | 20.54% | 31.0% | 39.3% | — |
| 1 | FMPRating / mid | 1583 | 5,241.71 | 11.14% | 15.17% | 33.97% | 27.1% | 44.3% | — |
| 1 | FMPRating / small | 1508 | 5,113.73 | 10.91% | 12.05% | 12.65% | 19.1% | 33.4% | — |
| 1 | FMPRating / small | 1633 | 3,098.04 | 7.00% | 10.66% | 33.03% | 34.4% | 37.0% | — |
| 1 | FactorRanker / large | 1065 | 10,443.19 | 12.67% | 12.04% | N/A | 14.1% | 42.1% | Yes |
| 1 | FactorRanker / large | 1064 | 9,839.09 | 12.11% | 11.67% | N/A | 16.7% | 42.4% | Yes |
| 1 | FactorRanker / mid | 1143 | 2,966.41 | 4.43% | 7.31% | N/A | 59.8% | 60.2% | Yes |
| 1 | FactorRanker / mid | 1146 | 4,329.41 | 6.19% | 12.85% | N/A | 71.6% | 76.3% | Yes |
| 2 | FMPEarningsDrift / mid | 1088 | 34,697.97 | 28.38% | 16.28% | 13.10% | 14.5% | 25.5% | — |
| 2 | FMPEarningsDrift / mid | 1607 | 12,468.30 | 14.46% | 8.06% | 18.49% | 21.8% | 28.2% | — |
| 2 | FMPEarningsDrift / small | 1367 | 24,237.13 | 22.79% | 19.15% | 39.15% | 43.8% | 45.7% | Yes |
| 2 | FMPEarningsDrift / small | 1528 | 18,770.36 | 19.28% | 23.12% | 36.48% | 35.0% | 35.0% | Yes |
| 2 | FMPInsiderClusterBuy / mid | 1298 | 14,081.83 | 15.79% | 9.38% | 17.26% | 26.6% | 37.7% | — |
| 2 | FMPInsiderClusterBuy / mid | 1619 | 5,244.71 | 7.29% | 13.22% | 6.72% | 32.9% | 73.7% | Yes |
| 2 | FMPInsiderClusterBuy / small | 1386 | 32,708.66 | 27.41% | 12.51% | 10.49% | 17.5% | 25.0% | — |
| 2 | FMPInsiderClusterBuy / small | 1681 | 22,673.31 | 21.84% | 17.52% | 16.53% | 41.8% | 42.6% | Yes |
| 3 | DeterministicScorer / large | 1017 | 16,198.15 | 17.43% | 17.24% | 31.59% | 35.4% | 63.6% | Yes |
| 3 | DeterministicScorer / large | 1107 | 27,168.29 | 24.49% | 15.59% | 39.01% | 20.9% | 52.2% | — |
| 3 | DeterministicScorer / mid | 1030 | 14,658.84 | 16.25% | 13.50% | 31.10% | 24.9% | 43.6% | — |
| 3 | DeterministicScorer / mid | 1173 | 20,816.75 | 20.66% | 12.78% | 21.58% | 36.4% | 35.9% | — |
| 3 | DeterministicScorer / small | 1000 | 13,697.01 | 15.48% | 16.14% | 38.35% | 28.1% | 33.8% | — |
| 3 | DeterministicScorer / small | 1358 | 12,880.28 | 14.81% | 12.36% | 35.60% | 22.1% | 24.7% | — |
| 3 | FMPSenateTraderWeight / blended | 1439 | 11,952.73 | 14.02% | 11.25% | 26.47% | 36.9% | 62.0% | Yes |
| 3 | FMPSenateTraderWeight / blended | 1649 | 12,417.39 | 14.42% | 9.41% | 35.97% | 22.0% | 62.1% | Yes |

## Pair behavior

| Expert / band | Sources | Bets/year | Mean hold days | Entry overlap, smaller | Holding overlap, smaller | Holding Jaccard | Return correlation |
|---|---|---:|---:|---:|---:|---:|---:|
| FMPRating / large | 1182 + 1434 | 64.0 / 95.6 | 32.7 / 17.8 | 14.5% | 18.6% | 9.2% | 0.59 |
| FMPRating / mid | 1070 + 1583 | 50.0 / 71.0 | 21.4 / 16.5 | 2.0% | 10.8% | 5.5% | 0.36 |
| FMPRating / small | 1508 + 1633 | 49.5 / 131.1 | 13.8 / 54.7 | 5.1% | 28.6% | 2.7% | 0.53 |
| FactorRanker / large | 1065 + 1064 | 213.2 / 191.9 | 9.3 / 9.3 | 81.8% | 83.9% | 65.6% | 0.94 |
| FactorRanker / mid | 1143 + 1146 | 39.3 / 31.8 | 10.7 / 12.5 | 100.0% | 84.7% | 68.8% | 0.88 |
| FMPEarningsDrift / mid | 1088 + 1607 | 93.7 / 52.5 | 2.8 / 16.3 | 24.8% | 22.6% | 7.1% | 0.45 |
| FMPEarningsDrift / small | 1367 + 1528 | 39.5 / 60.8 | 39.6 / 22.6 | 0.0% | 18.0% | 9.5% | 0.43 |
| FMPInsiderClusterBuy / mid | 1298 + 1619 | 44.8 / 26.3 | 13.1 / 5.7 | 24.1% | 28.0% | 6.9% | 0.42 |
| FMPInsiderClusterBuy / small | 1386 + 1681 | 49.8 / 28.0 | 3.9 / 14.4 | 17.9% | 18.1% | 7.1% | 0.29 |
| DeterministicScorer / large | 1017 + 1107 | 45.5 / 75.2 | 23.2 / 16.2 | 18.7% | 13.6% | 6.6% | 0.53 |
| DeterministicScorer / mid | 1030 + 1173 | 54.5 / 38.5 | 26.9 / 23.7 | 18.6% | 13.7% | 5.5% | 0.53 |
| DeterministicScorer / small | 1000 + 1358 | 43.5 / 81.4 | 62.4 / 48.3 | 0.0% | 33.3% | 15.7% | 0.62 |
| FMPSenateTraderWeight / blended | 1439 + 1649 | 29.7 / 78.0 | 43.1 / 15.7 | 2.8% | 33.8% | 20.0% | 0.61 |

## Final-group alternatives

The selected EarningsDrift pair 1367 + 1528 has the highest mean standalone CAR among different-strategy pairs satisfying the old overlap/frequency diagnostics. This is a ranking comparison, not a predicted combined CAR. The final S7 candidates are useful alternatives, but consume more average capital for lower CAR. The 40% concentration and 20% DD thresholds are disclosed trade-offs, not silent filters.

For Insider, 1386 + 1681 combines the highest-CAR S1 outcome with the strongest different-strategy S7 result. 1243 and 1386 have identical saved results and differ only in fixed sizing mode in their strategy parameters; choose the notional source 1386 once. Other high-return S1 variants substantially overlap and do not fill the diversification requirement.

| Group | BT / strategy | CAR | Max DD | Avg capital | Top 5 bets | Decision |
|---|---|---:|---:|---:|---:|---|
| FMPEarningsDrift / small | 1367 / S2 | 22.79% | 19.15% | 39.15% | 43.8% | Selected; concentration review |
| FMPEarningsDrift / small | 1528 / S6 | 19.28% | 23.12% | 36.48% | 35.0% | Selected; drawdown review |
| FMPEarningsDrift / small | 1364 / S2 | 18.77% | 18.01% | 35.94% | 37.8% | Lower-concentration substitute for 1367; -4.02 CAR points |
| FMPEarningsDrift / small | 1518 / S5 | 17.94% | 14.24% | 37.88% | 41.0% | Lower-DD substitute for 1528; -1.34 CAR points; still 41% concentration |
| FMPEarningsDrift / small | 1363 / S2 | 15.36% | 10.84% | 37.17% | 24.9% | Production reference; lower CAR with lower DD/concentration |
| FMPEarningsDrift / small | 1662 / S6 | 18.58% | 22.06% | 37.54% | 45.5% | Less CAR, slightly more capital and more concentration than 1528 |
| FMPEarningsDrift / small | 1666 / S7 | 17.27% | 17.92% | 46.66% | 37.7% | New S7; lower CAR and higher usage than selected pair |
| FMPEarningsDrift / small | 1667 / S7 | 16.48% | 17.50% | 47.31% | 27.1% | New S7; lower concentration, but higher usage and lower CAR |
| FMPInsiderClusterBuy / small | 1386 / S1 | 27.41% | 12.51% | 10.49% | 17.5% | Selected S1; 1243 is the duplicate outcome |
| FMPInsiderClusterBuy / small | 1681 / S7 | 21.84% | 17.52% | 16.53% | 41.8% | Selected S7; concentration review |
| FMPInsiderClusterBuy / small | 1683 / S7 | 17.63% | 15.47% | 19.40% | 39.5% | Lower-concentration alternative; -4.21 CAR points and higher usage |
| FMPInsiderClusterBuy / small | 1682 / S7 | 16.19% | 14.51% | 13.34% | 37.4% | Lower DD/concentration; -5.65 CAR points |
| FMPInsiderClusterBuy / small | 1677 / S6 | 14.25% | 17.57% | 13.92% | 22.1% | More dispersed winners; lower CAR and frequency too close to S1 |
| FMPInsiderClusterBuy / small | 1678 / S6 | 12.03% | 14.67% | 13.26% | 24.8% | Different frequency; materially lower CAR |
| FMPInsiderClusterBuy / small | 1671 / S5 | 8.50% | 16.62% | 32.43% | 56.9% | S5: 8.5% CAR and 56.9% concentration |
| FMPInsiderClusterBuy / small | 1674 / S5 | 10.10% | 23.67% | 65.38% | 76.6% | S5: 23.7% DD and 76.6% concentration |

## New selections: annual returns and existing robustness

| BT | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | Stored spread-stress profit retention | Net profit excluding five best bets |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1367 | 42.71% | 10.93% | 7.30% | 30.82% | 40.75% | 9.47% | 103.52% | $13,622.31 |
| 1528 | 20.36% | 35.47% | -3.90% | 15.82% | 55.28% | 2.09% | 90.51% | $12,200.05 |
| 1386 | 73.42% | 19.44% | 33.21% | 14.71% | 8.40% | 24.48% | 79.55% | $26,980.20 |
| 1681 | 23.09% | 23.71% | 42.56% | 6.56% | 29.06% | 9.44% | 94.10% | $13,202.28 |

1386 retains 79.55% of profit under the stored spread stress: transaction costs matter despite its low concentration. 1367 has stored retention above 100%; this is a path-dependent resimulation outcome, not evidence that spreads benefit the strategy. All four have stored Monte Carlo negative-outcome rates of 0%; these are existing resampling outputs, not fresh out-of-sample validation. Removing five winners is arithmetic sensitivity, not a rerun with a changed compounding path.

## Preservation and labels

The reviewed manifest selects 26 sources, flags 12 for review and preserves seven current production-source tags. Enabled production instance 13 now cites 1645, and the current production tag already reflects 1645. No production-source tag changes are needed in this transaction. The other six sources remain the same. No production database writes, deployment or account-setting changes occur.

Original and capped Senate results 1634, 1645, 1665, 1670, 1679 and 1680 are included in full non-label preservation hashes. The $2,000 results remain provisional because of missing trader-history cache dependencies and are not directly substituted for $10,000 source results. See [the comparison](senate_ok2000_2026-09-14/report.md).

Large FactorRanker equity/ledger profit differences ($36.50 for 1064, $19.37 for 1065) remain visible. All four selected FactorRanker ledgers contain compressed multiple-fill rows; capital reconstruction is unavailable rather than estimated. These are research-evidence limits; their stored results are unchanged.

Evidence is in `forward_selection_2026-09-14-v4/`: `candidates.json`, `pairs.json`, `selected_candidates.json`, `selected_pairs.json`, `daily_profiles.json`, `jobs.json`, `allocation_plan.json`, `production_sources.json`, `label_changes.json`, `preservation_baseline.json`, `label_application.json` and `final_verification.json`. The preceding plan is archived there; earlier evidence folders remain intact.

## Applied changes and account totals

The nine-row label-only transaction is applied and independently verified: **26 candidate tags, 12 review tags, seven production tags**. All 15 preserved full-row fingerprints (excluding labels) match, including the six original/capped Senate experiments. The previous 22 candidates remain and four new candidates fill the reserved slots. No backtest result or deployed account setting was changed.

| Test account | Settings | Equity per setting | Total allocated | Unassigned |
|---|---:|---:|---:|---:|
| 1 | 10 | 10% | 100% | 0% |
| 2 | 8 | 10% | 80% | 20% |
| 3 | 8 | 10% | 80% | 20% |

The **Finish grid allocation** scheduled follow-up was deleted. Selection and labels are complete; no deployment was performed. At $10,000 per test account, each sleeve starts with $1,000, so budget-matched copies and a combined-account replay are separate validation work.
