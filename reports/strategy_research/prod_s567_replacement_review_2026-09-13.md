# Production S5–S7 replacement review — 2026-09-13

**2026-09-14 $2,000 comparison:** both settings now have new saved $2,000-start/$2,000-cap results: **S5 BT 1679** and **S6 BT 1680**. S6 records **12.82% CAR / $1,484.97 profit / 8.38% DD / 23.88% average capital**, versus S5's **9.59% / $1,130.87 / 9.15% / 32.38%**. This supports S6 under the CAR-first objective at this budget, provisionally: the detailed cache-miss registry identified 211 additional price histories outside the tradable universe that may affect trader calculations. Originals and $1,000 results remain saved and unchanged. See [the $2,000 comparison](senate_ok2000_2026-09-14/report.md).

**2026-09-14 capped-run update:** source 1645 was preserved and tested separately as **BT 1670** with $1,000 starting equity and a $1,000 cap. It records **$500.65 net profit / 8.52% CAR / 6.15% DD**, versus control 1665's **$521.75 / 8.91% / 6.14%**. Average capital usage is lower, **14.63% versus 22.49%**. This does not establish a CAR upgrade at the $1,000 budget. Nine historical-cache datasets were missing in the new run, so a comparison with complete, matched inputs remains outstanding. See [the capped-run report](senate_1645_ok1000_2026-09-13/report.md). The original review below concerns the uncapped $10,000 source results.

**Revised decision after the user's clarification: prioritize higher CAR/profit at similar average capital usage.** Usage of 20–30% is acceptable, and peaks that cause competing experts to miss entries are an accepted trade-off. Compare each candidate with its current source; do not impose 30% as a new universal ceiling where the original already uses more.

**Senate 1634 → 1645 is the clearest match. Small EarningsDrift 1363 → 1528 is the highest-CAR candidate at comparable usage, with materially higher drawdown; 1518 is the less severe drawdown alternative.** Keep the other five sources for now. Senate 1649 is a close alternative with the highest Senate CAR and modestly higher average usage.

This supersedes the earlier capital-release recommendations for 1108, 1536, 1656 and 1508. Those recommendations sacrificed the return the user wants to improve. The reranking includes all 200 saved results without hidden drawdown, concentration or peak-usage exclusion gates. Drawdown and concentration remain explicit selection trade-offs.

Seven production slots remain seven. No production settings, database labels, allocations or backtest results were changed, and no reruns were launched.

## Production provenance and scope

The desktop `ba2.bat` points port 8081 to `C:/Users/basti/Documents/ba2_trade_platform-prod/db.sqlite`. Read each enabled expert’s `user_description` to identify the exact original source. All seven presently belong to account 1; each has `virtual_equity_pct=50`. Account margin is enabled with configured factor 1.8. The separate 10%-per-setting test-account plan has not been applied to production. This pass verifies source provenance and the allocation settings, not every live rule/schedule field.

| Production instance | Expert / band | Original source BT | Source strategy |
|---|---|---:|---|
| 7 | DeterministicScorer / large | 1107 | S6 |
| 8 | FMPInsiderClusterBuy / mid | 1298 | S1 |
| 9 | FMPEarningsDrift / small | 1363 | S2 |
| 10 | DeterministicScorer / mid | 1173 | S6 |
| 11 | FMPEarningsDrift / mid | 1088 | S1 |
| 12 | FMPRating / small | 1330 | S1 |
| 13 | FMPSenateTraderWeight / blended | 1634 | S5 |

Reviewed **200 persisted results**: original production sources plus completed S5/S6/S7 TOP-N candidates in the same expert/band from the latest optimization of each job name. The small EarningsDrift S7 notional job 518 completed during this review and its four distinct saved candidates are included. Small Insider jobs still running are outside this production source mix.

## How capital usage was measured

- Starting capital is **$10,000 per standalone source**. Rating spans **2022–2025**; the other experts span **2020–2025**. Profit is final equity minus initial capital, including terminal position marks. CAR means stored annualized return, not the optimizer’s fitness score.
- Reconstructed cash from actual entry/exit prices, quantities, dates and the saved per-fill commission. At each persisted valuation, **invested market value = equity − reconstructed cash**; capital usage is that value divided by contemporaneous standalone equity.
- Checked every ledger for long equities, finite amounts and one-entry/one-exit fee counts (or one entry for `open_at_end`). Compressed multi-fill histories would be rejected because they hide intermediate sizing. **All 200 rows passed**, with terminal cash + marked positions and ledger P&L reconciling to final equity within two cents. No negative cash or negative invested value beyond the tolerance was accepted.
- Average and P95 usage use calendar-day end-of-day marks across the full stated window, including flat periods and weekends with the previous mark carried forward. **P95** is the level exceeded on 5% of those days. **Peak** is the highest ratio at any saved valuation point, not a guarantee about between-snapshot demand. Terminal positions remain invested; their marked profit is not counted as a cash exit.
- These figures measure capital tied up in positions. They do not include unfilled-order reservations, collateral haircuts, rejected orders, financing costs or a replay of competition between experts. The saved `exposure_time` metric is not used as a substitute for capital utilization.
- Nominal sleeve percentages are unchanged when comparing replacements. No multiplying of standalone returns by inverse average usage is treated as an executable result.

## Original production-source results

| Source | Expert / band | Profit | CAR | Max DD | Average capital | P95 capital | Peak capital | Top-five profit share |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1107 | DeterministicScorer / large | $27,168 | 24.49% | -15.59% | 39.0% | 78.4% | 99.9% | 20.9% |
| 1298 | FMPInsiderClusterBuy / mid | $14,082 | 15.79% | -9.38% | 17.3% | 46.0% | 99.9% | 26.6% |
| 1363 | FMPEarningsDrift / small | $13,552 | 15.36% | -10.84% | 37.2% | 55.8% | 64.1% | 24.9% |
| 1173 | DeterministicScorer / mid | $20,817 | 20.66% | -12.78% | 21.6% | 51.0% | 71.4% | 36.4% |
| 1088 | FMPEarningsDrift / mid | $34,698 | 28.38% | -16.28% | 13.1% | 49.8% | 100.0% | 14.5% |
| 1330 | FMPRating / small | $17,379 | 28.72% | -13.53% | 27.3% | 50.4% | 63.4% | 50.9% |
| 1634 | FMPSenateTraderWeight / blended | $9,473 | 11.76% | -10.08% | 32.5% | 53.5% | 60.1% | 39.0% |

## Candidates matching the clarified objective

All rows in this table use **$10,000 initial standalone capital over 2020–2025**. Capital is average invested value as a percentage of the setting's own equity; it is not its nominal account allocation. Profit includes terminal unrealized marks. Percentage-point changes refer to CAR or usage, whereas profit uplift is relative to the original dollar profit.

| Expert | BT | Profit | CAR | Max DD | Average capital | Top-five profit share | Trades |
|---|---|---:|---:|---:|---:|---:|---:|
| Senate current | 1634 S5 | $9,473 | 11.76% | -10.08% | 32.48% | 39.0% | 269 |
| **Senate, closest usage match** | **1645 S6** | **$12,338** | **14.35%** | **-9.65%** | **32.36%** | **19.5%** | **405** |
| Senate, highest CAR | 1649 S6 | $12,417 | 14.42% | -9.41% | 35.97% | 22.0% | 468 |
| Small EarningsDrift current | 1363 S2 | $13,552 | 15.36% | -10.84% | 37.17% | 24.9% | 471 |
| **Small EarningsDrift, highest CAR** | **1528 S6** | **$18,770** | **19.28%** | **-23.12%** | **36.48%** | **35.0%** | **365** |
| Small EarningsDrift, less drawdown | 1518 S5 | $16,887 | 17.94% | -14.24% | 37.88% | 41.0% | 368 |

**Senate 1645:** adds **$2,864.69 profit (+30.24%)** and **2.59 points of CAR**, with almost identical average capital usage and slightly better DD. Top-five concentration falls from 39.0% to 19.5%. This is the strongest historical evidence of the requested improvement. Its P95 usage rises from 53.5% to 99.7%; under the clarified objective, that is a disclosed funding pattern rather than a veto. Profit factor falls from 4.35 to 1.94, so not every metric improves.

**Senate 1649:** adds **$2,944.05 profit (+31.08%)** and **2.66 points of CAR** over the original, at 35.97% average usage. Relative to 1645, the additional six-year profit is only **$79.36**, CAR rises **0.07 points**, and average usage increases **3.61 points**. Either fits the broad aim; 1645 fits “similar capital usage” more closely, while 1649 is the literal CAR leader. Both have six positive calendar years.

**Small EarningsDrift 1528:** adds **$5,218.79 profit (+38.51%)** and **3.92 points of CAR**, with average capital usage slightly lower. It is the CAR-first candidate, and its 35.0% top-five share remains below the previous 40% concentration screen. The real trade-off is **DD more than doubling, 10.84% → 23.12%**, and uneven annual returns: +55.28% in 2024 followed by +2.09% in 2025. It has a lower PF of 1.68 versus 2.26. This is a higher-return alternative, not an improvement in every respect.

**Small EarningsDrift 1518:** adds **$3,335.59 profit (+24.61%)** and **2.58 points of CAR**, at essentially unchanged average usage. DD increases to 14.24%, considerably less than 1528. All six calendar years are positive. The weakness is concentration: top-five/net profit rises from 24.9% to **41.0%**, with the top trade alone contributing 12.9%. Keep this flagged against the user's earlier concentration preference rather than silently excluding it or calling it an unqualified upgrade.

### What changes in the strategy

| Source/candidate | Saved configuration and behavior |
|---|---|
| Senate 1634 | S5 notional sizing; 10% per-name ceiling; profit cap and break-even/stop rules; 269 trades. |
| Senate 1645 / 1649 | S6 notional sizing; 15% per-name ceiling; saved exit rules use a 15-day time condition, alongside the risk-manager safeguard; 405 / 468 trades. The capital is used in larger bursts. |
| Small EarningsDrift 1363 | S2 notional sizing; 10% per-name ceiling; earnings report age up to 15 days; signal/profit exits and a -6% ruleset stop. |
| Small EarningsDrift 1528 | S6 ATR-risk sizing; 20% per-name ceiling; report age up to 35 days; saved exit rule uses a 25-day time condition, alongside the risk-manager safeguard. |
| Small EarningsDrift 1518 | S5 ATR-risk sizing; 15% per-name ceiling; report age up to 35 days; staged profit-lock rules, profit cap and ruleset stop. |

These are full saved strategy replacements, including expert settings, rules and schedules. Copying only their per-name size or changing a strategy number would not reproduce the measured result. This review did not execute a fresh export/import parity check.

### Calendar-year returns from saved equity curves

Computed from prior year-end equity (initial capital for 2020), including open-position marks. These years are slices of the same optimization window, not independent holdout tests.

| BT | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
|---|---:|---:|---:|---:|---:|---:|
| Senate 1634 | 21.33% | 10.29% | -3.85% | 20.34% | 11.16% | 13.15% |
| Senate 1645 | 18.90% | 1.32% | 5.59% | 25.54% | 3.79% | 34.77% |
| Senate 1649 | 29.42% | 1.22% | 2.43% | 24.06% | 1.84% | 32.22% |
| Small ED 1363 | 15.46% | 21.77% | 10.04% | 8.21% | 22.20% | 15.12% |
| Small ED 1528 | 20.36% | 35.47% | -3.90% | 15.82% | 55.28% | 2.09% |
| Small ED 1518 | 34.11% | 9.69% | 0.62% | 24.22% | 15.33% | 26.78% |

## Why retain the other five production sources

The comparisons below use each group's matching dates and $10,000 starting equity. Ranking is by CAR before considering the displayed trade-offs.

| Expert / current BT | Best higher-CAR result, or highest alternative | Assessment |
|---|---|---|
| Large Scorer / 1107 | 1109 S6: CAR 24.49% → 31.23%; profit $27,168 → $40,979; average usage 39.01% → 61.68%; DD -15.59% → -26.67%. | A substantial return increase, but it needs 58% more average capital proportionally. Keep 1107 for a comparable-usage replacement objective; 1109 belongs in a higher-budget experiment. |
| Mid Insider / 1298 | 1625 S7: CAR 15.79% → 15.91%; profit $14,082 → $14,227; usage 17.26% → 14.71%; DD -9.38% → -16.25%; top five 26.6% → 57.4%. | Technically more CAR at less usage, but only $145 extra profit over six years for much worse DD/concentration. Keep 1298. |
| Mid Scorer / 1173 | Highest alternative 1175 S6: CAR 20.66% → 20.13%; usage 21.58% → 21.63%. | No S5–S7 alternative exceeds the current CAR. Keep 1173. |
| Mid EarningsDrift / 1088 | Highest alternative 1598 S5: CAR 28.38% → 27.84%; usage 13.10% → 46.89%; DD -16.28% → -25.38%. | Current source combines higher CAR with much lower average usage. Keep 1088. |
| Small Rating / 1330 | Highest alternative 1641 S6: CAR 28.72% → 17.69%; profit $17,379 → $9,145; usage 27.25% → 6.77%. | Capital-efficient, but much less profit at measured sizing. No proven higher-profit successor. Keep 1330 for this objective; its existing 50.9% top-five concentration remains a weakness. This group covers 2022–2025. |

Small EarningsDrift S7 winner **1666** earns 17.27% CAR at 46.66% average usage and 17.92% DD. Both shortlisted alternatives earn more CAR at less average usage; 1518 also has lower DD, although its concentration is worse. S6 **1662** has less CAR, greater average usage and worse concentration than 1528 (18.58%, 37.54%, 45.5%), with only a modest DD improvement (22.06% versus 23.12%). Neither displaces the shortlist.

## Stacking and the meaning of these results

Funding collisions are accepted by the user. No P95 or peak exposure ceiling was used to remove candidates. The source measurements establish standalone historical profit/CAR at comparable average deployment; they do not establish the combined account's realized CAR after refused entries. Depending on which entries are missed, the shared-account outcome can differ. This qualification does not change the CAR-first ranking.

The existing `replacement_comparison.json` records scenarios from the **superseded capital-release objective**. Its lower demand figures are not evidence of higher profit and are not the current recommended portfolio. No account-return claim is made from adding standalone profits or multiplying CAR by inverse average usage.

Capped OK1000 results require separate accounting: their compounded scoring equity is synthetic, so `final_equity - initial_capital` must not be reported as actual capped trade profit. This shortlist uses uncapped source results whose dollars reconcile to their ledgers. A production-budget comparison would need matched capped results or a shared-account replay; neither was executed here.

## Result and follow-through

For the user's stated objective, shortlist **Senate 1645** (or 1649 for the last 0.07 CAR points) and **small EarningsDrift 1528**, keeping **1518** as the lower-DD alternative with its concentration flag. Retain **1107, 1298, 1173, 1088 and 1330**. Do not replace those five with lower-CAR capital-release candidates merely because their average usage is smaller.

Before an actual switch, a focused comparison should retain the current source as control, use the exact candidate configuration at the intended production sizing budget, and record executed/refused entries. This is remaining validation rather than a claim that it has already passed. No live switch or metadata relabel was performed in this review.

Evidence is in `prod_s567_replacements_2026-09-13/`: `production_sources.json`, `candidates.json`, `daily_profiles.json`, `car_priority_comparison.json` (current ranking, all alternatives and deltas), `verification.json`, and `existing_capped_evidence.json`. The read-only reconstruction script is `test_tools/review_prod_s567_replacements_20260913.py`. All 200 source ledgers reconciled; the CAR-first reranking reuses those saved measurements, verifies matching capital/dates, and does not rerun the engine or access market APIs.
