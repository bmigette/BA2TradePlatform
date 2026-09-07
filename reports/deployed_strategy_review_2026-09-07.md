# Review of the six deployed strategies

Read-only examination of production 8081's six enabled expert instances, their saved rules, their OK1000 result ledgers, and retained backtest recommendation/order rows. No trading, production configuration changes, or new backtest executions were performed.

**Assessment:** large-cap DeterministicScorer is the strongest current research baseline. Mid-cap DS and InsiderClusterBuy have useful but different strengths. Small-cap EarningsDrift has a credible asymmetric payoff, but its exit design allows positions to outlive the earnings event by years. Mid-cap EarningsDrift is actually a fast, small-target strategy. The new small-cap FMPRating has encouraging results but greater concentration and open-position dependence than its headline suggests.

These are assessments of the **recorded Monday OK1000 variants**. The [deployment parity audit](prod8081_settings_parity_review_2026-09-07.md) found screen/settings mismatches and a lost optimized schedule. Therefore none of these results certifies the current production configuration or predicts the effect of repairing it.

Reproducible calculation: [analysis script](deployed_strategy_analysis.py), [full metrics and rule evidence](deployed_strategy_analysis_2026-09-07.json). The independent-run combination uses the common interval 2022-01-03 09:30 through 2025-12-29 09:30, in the stored timestamps.

## Correct interpretation of the reported profits

The backtest `trades` list includes `exit_reason="open_at_end"` rows. Those positions were **not sold**: they are valued at the last available price and included in the result ledger. Earlier reports described some whole-ledger totals as closed-trade profit; that label was inaccurate. The totals themselves are unchanged. The table below separates realized closed-trade P&L from the ledger contribution of still-open positions, including its entry-cost accounting.

| Instance / capped BT | Period | Total P&L including end marks | Closed-trade P&L | End-mark contribution | Closed PF | Cap drawdown |
|---|---|---:|---:|---:|---:|---:|
| 7 — Large DS / 1407 | 2020–2025 | $1,708 | $1,684 | +$24 | 2.18 | 19.70% |
| 8 — Mid Insider / 1420 | 2020–2025 | $942 | $958 | −$17 | 2.47 | 14.74% |
| 9 — Small EarningsDrift / 1423 | 2020–2025 | $780 | $675 | +$105 | 1.67 | 17.39% |
| 10 — Mid DS / 1409 | 2020–2025 | $839 | $839 | $0 | 2.56 | 13.33% |
| 11 — Mid EarningsDrift / 1406 | 2020–2025 | $462 | $462 | $0 | 1.48 | 10.00% |
| 12 — Small FMPRating / 1425 | 2022–2025 | $737 | $523 | +$214 | 1.89 | 20.83% |

PF is recomputed using genuinely closed rows. Drawdown uses the recorded portfolio path, including unrealized marks, divided by the fixed $1,000 cap. Stored synthetic annualized returns are not treated as cash returns. Different historical periods in this table are deliberate and must not be ranked as if identical.

## 1. Large DeterministicScorer — keep as the reference configuration

The selected rules buy on a bullish signal when flat, attach requested +12% take-profit and −8% stop levels from entry, and close on a negative recommendation or after more than 30 days. Protective levels remain subject to the execution/risk layer, so the requested percentages are not guaranteed realized outcomes.

There are 342 actual closed trades, a 62.9% closed win rate, and a 17.2-day mean holding period. Its five largest closed winners account for only **5.2% of gross winning profit**. Closed profits are positive in every calendar year, although 2022 is weak. The results are much less dependent on a few trades than the other high-return candidates.

The largest issuer contributor is NVDA at about $251 of closed P&L; the next names include AMAT, PLTR, SHOP and AVGO. That concentration in a related set of stocks deserves portfolio-level tracking even though single-trade concentration is low.

**Next experiment:** retain this baseline and compare the targeted quality/momentum configuration described in the [strategy ideas memo](expert_strategy_ideas_2026-09-07.md). Change a small set of score weights, while fixing screening, schedule and exits. Avoid simultaneously retuning every knob and losing the ability to identify what helped.

## 2. Mid InsiderClusterBuy — productive, but much of the history came from 2020

The first entry branch requires bullish + medium term + medium risk + flat, and places its take-profit 6% below the expert target. The fallback branch buys other bullish/flat recommendations at the full target. A 210-day maximum-hold rule and a floor-stop adjustment provide exits.

All 224 entry recommendation rows in the retained capped run are medium risk and medium term. Thus the first branch captures the whole measured sample; the fallback branch has no observed contribution. The expert only emits BUY or HOLD, so its separate bearish exit is unreachable under that implementation.

Closed win rate is 77.8%, but the median winner is only $3.07. The mean winner is $9.37 against a $13.32 mean loss. About $581 of its $958 closed profit came in 2020. The median hold is 2.3 days, but the mean is 16.3 and the longest closed hold is 211 days: most trades recycle quickly while a few occupy capital much longer.

**Next experiment:** compare a 60/90/120-day timeout against the current 210-day control, and separately test a shorter insider lookback than the current 120 days. Do not assume the shorter timeout wins; it can cut recoveries. Removing the unused bearish rule or fallback branch is a simplification for this sample, not a demonstrated source of extra profit.

## 3. Small EarningsDrift — credible payoff, weak expiry of the original thesis

Entry requires a recent earnings beat, bullish signal, no existing position, confidence >45 and more than 20 days since the last close. The expert requires at least a 14% surprise within 15 calendar days of the report.

All measured entry confidences are 80–90, so the >45 gate did not discriminate between the actual entries. The deployed exit rules are bearish, negative rating, profit >52%, and an entry-price-relative floor stop requested at −6%. There is **no time exit**.

Both bearish and negative-rating conditions test for a SELL recommendation. EarningsDrift's `_process()` emits BUY or HOLD, never SELL. Once a report becomes stale, the signal changes to HOLD, which does not close the position through either rule. In this run, exits were 298 stop-losses and 57 other exits, with 14 positions still open at the end.

This is not grounds to dismiss the strategy on win rate. For actually closed positions:

- Win rate: **16.1%**.
- Average winner: **$29.57**; average loss: **$3.39** — about **8.7 times** as much per winner.
- Top five winners: **20.4% of gross winning profit**.
- Removing their profits arithmetically still leaves approximately **$331** of closed profit. This is an attribution diagnostic, not a replay that removes those trades.

The holding-period mismatch is the stronger concern. Mean closed hold is 55.5 days, the 90th percentile is about 151 days, and the largest winner, CNR, was held **1,045 days**. The report freshness gate controls entry; it does not limit how long the trade is held. Open positions at the end include HMN held for about 323 days. This behaves partly as a long-term recovery/continuation strategy entered after earnings.

**Next experiment:** a rule-only maximum hold of 60/90/120 days, each compared with the unchanged unlimited-hold control. Keep the stop and profit exit fixed. Measure released capital and lost winner P&L together; a shorter hold can improve turnover while reducing returns. An explicit `days_opened` rule needs no new expert code.

Sources: `packages/experts/ba2_experts/FMPEarningsDrift.py:327-372`, `packages/common/ba2_common/core/TradeConditions.py:462` and `:746`.

## 4. Mid DeterministicScorer — a bounded holding period, with recent gains doing most of the work

Entry requires bullish + flat + confidence >50 + expected profit >2%. The selected entry rule contains no TP/SL action; protection is supplied by the risk/execution layer. The only explicit exit rule is `days_opened >25`.

Of 142 closed trades, 110 are recorded as ordinary exits and 32 as stop-loss exits. Median holding time is 28 days; the longest is 29. Its closed PF is 2.56, with about $17.62 per winner against $8.38 per loss.

About $668 of $839 closed profit came from 2024–2025. The biggest winners include HIMS, CRDO, RKLB and ASTS. This is a useful baseline for a bounded hold, but the historical result does not establish that its recent strength persists across other regimes.

**Next experiment:** a narrow 15/20/25/30-day exit comparison, with all score settings held fixed. Separately test whether a signal-reversal exit improves outcomes; do not infer that a single explicit exit rule makes the whole strategy risk-free.

## 5. Mid EarningsDrift — fast turnover, modest payoff per winning trade

The medium-risk entry branch places a TP 14% below the expert target, with an entry stop requested at −16%. The later floor-stop rule requests −14%; an additional time exit allows 150 days. All 260 measured entry recommendations have medium risk, so the fallback buy-only branch is unused in this sample.

The expert's static expected profit is 19%. Ignoring any difference between signal and fill prices, applying the −14% target offset gives `1.19 × 0.86 − 1 = 2.34%` implied upside. Thus a setting named “19% expected profit” becomes a much smaller executable target through the entry rule. Floors, gaps and actual fills can change the realized result.

The observed strategy matches that reading: mean hold 2.85 days, median 1.13 days, 70.4% closed win rate, and mean winner $7.79 versus mean loss $12.51. No position needed the 150-day time exit; the longest closed hold was about 51 days. This is a small-target strategy triggered by earnings news, not a long earnings-drift hold.

Its historical average entry-cost exposure proxy is about $71, compared with about $440 for small EarningsDrift. These are time-weighted entry-cost estimates, not broker buying-power measurements. The two EarningsDrift return streams are therefore not interchangeable merely because their expert class is the same.

**Next experiment:** hold the signal fixed and compare a few modest target offsets, recording net expectancy after costs and larger-loss days. Do not promote this strategy solely for its higher win rate.

## 6. Small FMPRating — worth a controlled follow-up, with concentration made explicit

There are two entry branches:

- Confidence >=80: TP 14% below the expert target, requested entry stop −4%.
- Confidence >=45 after the first branch fails: TP 8% above the expert target, requested entry stop −18%.

The branches stop further rule processing when they match. Actual stops can be changed by execution floors and the stop ratchet. The only management rule requests a −18% floor stop; there is no time or negative-signal exit.

The higher-confidence branch therefore seeks a nearer target with a tighter requested stop, while the lower-confidence branch accepts more room and a more distant target. That is a genuine strategy choice, not a guarantee that “more confidence” means a more profitable or higher-win-rate trade.

The retained entry recommendations allow a direct branch attribution:

| Branch | Actually closed trades | Closed profit | Closed win rate |
|---|---:|---:|---:|
| Confidence >=80 | 154 | $396 | 24.0% |
| Confidence 45–<80 | 25 | $127 | 60.0% |

The lower-confidence sample is too small to justify discarding the higher-confidence branch; both made money in the recorded run.

Overall, $214 of $737 total P&L comes from six positions still open at the end. ARIS and SNDX contribute about $196 of those end marks. Closed-trade PF is 1.89, versus the displayed 2.24 including marks. The five largest closed winners contribute **45.2% of gross winning profit**; subtracting their profits leaves about **$20** closed net profit. CVNA alone contributes approximately $201.

This is a greater concentration concern than small EarningsDrift, even though both have low win rates. It still warrants a controlled test; it does not yet justify assuming broad robustness from the headline result.

**Next experiments:** compare the current asymmetric branches against a common bracket policy, then separately test a 60/90/120-day time exit. Keep the unchanged original as the control. Current live screening differs substantially from this capped run, so first repair/declare the screening contract rather than treating production as the same experiment.

## How the six fit together in the recorded data

On the common interval, daily P&L correlations between pairs range from about 0.11 to 0.33. The two EarningsDrift variants correlate at about 0.18. There is some historical complementarity, but it does not establish shared-account execution or future independence.

For a fair cash-control comparison, the denominator below is always **$6,000**: six independent hypothetical $1,000 allocations, with unused allocations left as cash.

| Combination | Common-window total P&L | Worst drawdown dollars | Drawdown / $6,000 |
|---|---:|---:|---:|
| Original four + two cash allocations | $2,453 | $385 | 6.42% |
| Original four + mid EarningsDrift + cash | $2,894 | $447 | 7.45% |
| Original four + small FMPRating + cash | $3,184 | $475 | 7.91% |
| All six | $3,626 | $503 | 8.39% |

Adding the two strategies increased measured profit and measured drawdown. It did not provide a free reduction in absolute risk. These sums use independent backtests, not a joint replay of production's six overlapping 60% entitlements.

Same-symbol simultaneous holdings also occur: nine symbols overlap between mid Insider and mid EarningsDrift; ten between mid DS and mid EarningsDrift; nine between small EarningsDrift and small FMPRating. A shared-account replay must include order admission, cash competition and duplicate-symbol handling.

## Recommended research order

1. Resolve the parity findings and choose explicitly between original optimized weekdays and the recorded Monday variants. Preserve historical runs.
2. Run one change at a time: small EarningsDrift timeout; FMPRating bracket symmetry/timeout; then the modest Insider/mid-DS/mid-ED variations above.
3. Compare focused quality/momentum and short-pullback configurations against large DS as the baseline. These ideas use existing score settings and rules, with new optimization profiles where necessary.
4. Validate incremental value using the intended shared-account allocations and genuinely unused data or forward paper observations. The repeatedly searched 2020–2025 sample is not a fresh holdout.

The structural findings mostly point to **better rule configurations**, not new expert implementations. A new expert is needed only for a distinct signal or portfolio behavior, such as the multi-asset trend/cash idea in the separate memo.
