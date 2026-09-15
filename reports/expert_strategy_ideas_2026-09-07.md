# Strategy ideas using the existing experts

These are research hypotheses to test, not validated return forecasts or instructions to deploy. The deployment/configuration gaps in [the companion audit](prod8081_settings_parity_review_2026-09-07.md) should be resolved before ranking new results against the current book.

My first two experiments would be **a focused quality/momentum strategy** and **a short-horizon pullback strategy**, both using DeterministicScorer and existing rules. If the aim is broader sources of return rather than another stock-selection variant, a simple multi-asset ETF trend expert is the more distinct addition, but it requires new implementation.

## What the current expert set offers

This is a capability review of the registry and relevant implementation, not a new line-by-line audit of every expert.

| Expert | Best use for this research | Assessment |
|---|---|---|
| DeterministicScorer | Separate quality, momentum, pullback, analyst and earnings configurations | Best fit for reusable signals with optimized settings and shared entry/exit rules. Two current deployments already use it, so measure overlap before adding another. |
| FMPEarningsDrift | Fresh positive earnings-surprise signals, with varied holding/exit rules | Keep as a baseline. Another freshness variant is possible, but the two live EarningsDrift instances already cover this source of information. |
| FMPInsiderClusterBuy | Publicly observable open-market insider clusters | Useful existing event source. Test cluster freshness, breadth, and holding period before introducing elaborate new signals. |
| FMPRating / FinnHubRating | Analyst agreement, coverage and recency | Treat as related analyst-sentiment hypotheses, not two independent sources of confirmation. Prefer testing incremental information from target changes over simply duplicating consensus screens. |
| FactorRanker | A compact momentum/quality basket with periodic rebalancing | Already supports factor weights and top-N portfolios. It bypasses the classic risk manager and shared entry/exit rules, so it does not fully match the user's goal of expressing strategies through those rules. |
| FMPSenateTraderWeight / Copy | Dated public congressional-trade signals | Existing alternatives worth keeping as controls if their saved results are competitive. This review has not established that they improve the current combination. |
| FMPEarningsEvent | Upcoming-event ranking, with entry timing owned by rules | Architecturally fits the rules approach. Its implied-move feature and option execution require a separate data/fill validation; it is not the first experiment I would add for a $1,000 allocation. |
| TradingAgents / PennyMomentumTrader | LLM-driven analysis and, for PennyMomentumTrader, its own live monitoring pipeline | Lower priority for a small deterministic optimization project. They introduce additional model/pipeline behavior beyond the rules and numeric settings being searched. |

Registry: `ba2_trade_platform/modules/experts/__init__.py`. Implementations are under `packages/experts/ba2_experts`, except live-only TradingAgents.

## 1. Quality plus momentum — first existing-code experiment

**Hypothesis:** combine financially stronger companies with established price trends, while removing analyst/earnings contributions so the incremental signal is interpretable.

Use DeterministicScorer with technical and fundamental weights enabled, `w_analyst=0`, `w_earnings=0`. Within the fundamental section emphasize quality and Piotroski rather than fitting all four subcomponents simultaneously. Retain the existing distress veto. Within technical scoring emphasize medium-term momentum and the trend-SMA component; initially keep the RSI and breakout subweights fixed or zero.

Express entry with the existing bullish, confidence, expected-profit, flat-position and cooldown conditions. Use an explicit protective stop, existing take-profit/stop adjustment actions, and `days_opened` as the maximum hold. Start with weekly entries and daily management.

Suggested small search, after choosing fixed screening and capital settings:

- Technical/fundamental blend: 70/30, 50/50, 30/70.
- Momentum lookback: 126 or 252 trading bars, keeping a fixed 21-bar skip.
- Maximum hold: 30, 60 or 90 calendar days (`days_opened` uses the engine's day convention).
- One entry threshold and one risk/exit parameter; keep the other risk settings fixed initially.

These are proposed starting ranges, not estimated optima. The existing API accepts expert overrides; making these particular fields tunable may require a new optimization profile or launcher gene-space configuration, but no new signal formula.

Why test it: quality and momentum are explicit, separately measurable inputs. Quality research provides a rationale for the hypothesis, but the cited long/short factor portfolio is not evidence for the profitability of this long-only implementation. [Asness, Frazzini and Pedersen, Quality Minus Junk](https://images.aqr.com/-/media/AQR/Documents/Insights/Working-Papers/Quality-Minus-Junk.pdf).

Implementation: `DeterministicScorer/fundamental.py`, `technical.py`, `combine.py`, and `get_settings_definitions()` in `DeterministicScorer/__init__.py`.

## 2. Liquid-stock pullbacks — first experiment with a shorter holding period

**Hypothesis:** a short-term selloff in a still-healthy trend can recover faster than the existing longer-held earnings positions.

Use a separate DeterministicScorer configuration emphasizing its RSI mean-reversion component, retaining a smaller trend/quality contribution and the distress veto. The existing `adx_gate` and `adx_rsi_boost` already change the relative contribution of RSI when the market is not trending. Use explicit liquid-stock screener settings, with the same price, volume, float and market-cap limits on both sides of the backtest/live boundary.

Keep this as a daily-entry strategy with a short `days_opened` exit and a cooldown after closing. Candidate ranges: RSI period 2/3/5, maximum hold 3/5/10 calendar days, and a small entry-threshold grid. Fix the stop and transaction-cost assumptions for the first comparison. Test moderate price-drop windows; avoid selecting an extreme threshold solely because one recovery dominates the sample.

**Existing code can implement the blended-score version.** The generic rules vocabulary does not currently expose a raw RSI or SMA-distance leaf. Requiring a literal rule such as “RSI < 20 AND close > SMA200” would need those measured fields exposed to shared conditions; do not describe a weighted blend as equivalent to that hard gate.

Why test it: short-run reversal is a documented empirical phenomenon whose behavior depends on liquidity. That motivates a test with execution costs and a liquid universe, not an assumption that more turnover improves returns. [Dai, Medhat, Novy-Marx and Rizova, Reversals and the Returns to Liquidity Provision](https://www.nber.org/papers/w30917).

Implementation: `DeterministicScorer/technical.py:206`, `DeterministicScorer/__init__.py` settings `rsi_period`, `tw_rsi`, `tw_mom`, `tw_d200`, `adx_gate`, `adx_rsi_boost`; existing `days_opened` and cooldown rules.

## 3. Analyst target changes — a more focused alternative to consensus ratings

**Hypothesis:** recently raised targets plus acceptable price behavior carry more useful information than high but stale target upside alone.

The existing `price_target_drift()` component compares newer and older dated analyst targets and blends that change with current implied upside. A first experiment can use DeterministicScorer with the analyst section dominated by `aw_targets`, a smaller technical confirmation weight, and earnings/fundamental weights fixed at zero. Compare it directly with the existing FMPRating recipe under identical screen, schedule, costs and cap.

Possible parameters: target window 30/60/90 days, minimum target observations 3/5, analyst-versus-technical weight, and a 15/30/60-day time exit. Coverage counts here are target observations, not necessarily distinct analysts.

Two implementation limits matter:

- `revision_momentum()` currently weights bullish/bearish **rating-count snapshots**. Despite its name, it does not calculate a sequence of individual analyst upgrades. A pure upgrade-event strategy would need additional logic.
- `price_target_drift()` currently blends target changes with upside at fixed 50/50 weights. A strict “targets must have risen” gate or an optimized internal blend needs an exposed metric/setting. `new_target_higher` on an expert recommendation is not automatically equivalent to a newly published analyst raise.

Therefore the existing blend is testable now; a strict event-based version needs a small, explicit extension. This proposal follows the implemented signal's semantics rather than claiming support from a paper studying a different revision measure.

Implementation: `DeterministicScorer/analyst.py:44` and `:112`.

## 4. Multi-asset ETF trend plus cash — the most distinct addition

**Hypothesis:** a strategy able to allocate outside individual equities, or stay in cash, may improve the portfolio when the stock-selection experts struggle together. This needs measurement; different instruments do not guarantee useful diversification.

Use a fixed, small universe representing equities, government bonds and gold. An illustrative rule is to rank trailing 6–12-month returns, buy only instruments passing a positive-trend filter, and hold cash when none qualifies. Rebalance monthly, with one or two holdings so whole-share affordability can be tested explicitly at $1,000. Do not select particular ETFs or share-count targets until prices, broker eligibility and lot sizes are checked.

FactorRanker supplies some reusable ranking/construction code, but its current top-N construction does not enforce an absolute positive-trend gate and it bypasses shared trading rules. For a faithful rule-based version, implement a small clean expert exposing trend/momentum observations to the existing rules, plus explicit portfolio selection/cash behavior. Reusing FactorRanker unchanged would implement a different strategy.

The research motivation is time-series momentum across asset classes. The original evidence concerns futures/forwards and long/short portfolios, so it does not establish the results of this small long-only ETF adaptation. [Moskowitz, Ooi and Pedersen, original paper and data](https://www.aqr.com/Insights/Datasets/Time-Series-Momentum-Original-Paper-Data).

## How I would decide which one earns a place

First establish one complete, reproducible baseline configuration after the deployment audit. Preserve the original goal2020 results and label Monday variants distinctly. Do not call an OK1000 rerun comparable if it also changes schedule, screening or effective boolean behavior.

Run one small grid per hypothesis, with the same declared capital/cost assumptions. Compare net dollars, drawdown, gross-winning-profit concentration, capital tied up, and holding time. Then compare each candidate's incremental effect on a shared-account replay at the actual intended allocations. A separate standalone equity curve does not model six experts competing for the same cash or the same symbol.

The already searched 2020–2025 history is not a fresh holdout. Use it to diagnose and compare, then reserve genuinely unused data or forward paper results for validation. Include a one-session delay sensitivity for signals whose publication time is uncertain. Keep parameter neighborhoods and losing periods visible instead of promoting only the single best trial.

**Priority:** quality/momentum and short pullbacks for the next configuration-only experiments; the ETF trend/cash expert if adding a different portfolio behavior justifies new code. Analyst-target drift is a secondary experiment because it overlaps the new FMPRating deployment and the current mid-cap DS analyst contribution.
