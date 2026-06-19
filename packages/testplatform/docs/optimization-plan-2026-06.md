# Expert Strategy Optimization Plan (2026-06)

Status: **validated by user 2026-06-16.** Tracks the plan to optimize the dev account's expert
strategies in the backtester. Follow-up / progress table at the bottom.

## Goal
Optimize the trading strategies of the dev-account experts in the backtester, picking the best
**strategy structure** per expert (Phase 1) then the best **universe** for each (Phase 2).

- **Fitness metric: Calmar** (annualised return / max drawdown) as the primary objective.
  Report Sharpe / total return / win-rate / PF / maxDD / trades alongside for every run.
- **Window:** 2023-01-01 → 2026-01-01, **weekly** analysis cadence, daily fill clock.
- **Capital:** $10,000 (equity experts). FactorRanker uses its own weights/top-N (bypass; no
  classic RM/ruleset).
- **Parallelism:** process pool (parallel 12); population sized to each strategy's gene count.
- **HARD RULE — no static values.** EVERY threshold and every TP/SL/adjust % is an optimized
  range with a step (e.g. TP 5→25 step 2). Nothing is hardcoded (no static ±15%).

## Experts (in scope)
FMPRating, FMPEarningsDrift, FMPInsiderClusterBuy, FactorRanker.
(Out of scope for now: FinnHubRating, SenateTrader, PennyMomentum, options, TradingAgents.)

## Strategies — 3 per expert, ALL values optimized
- **S1 — Live ruleset (imported, params optimized).** Import the expert's actual dev-account
  `enter_market` + `open_positions` rulesets (e.g. FMPRating rs#10 "High Conviction" entry +
  rs#11 "Profit Protection" exit) → `buy_entry_conditions` + `exit_conditions`, with `optimize`
  flags + auto-derived min/max/step on EVERY numeric threshold and every adjust-% (centred on
  the live value, e.g. ±50% range). Conditions/actions also on/off-toggleable.
- **S2 — Bracket.** Light entry gate (confidence + expected-profit, optimized) + an optimized
  TP/SL bracket (**TP 5→25, SL 5→25** style ranges with steps — NOT static) + a minimal
  break-even/profit-lock exit (optimized). Tests "simple bracket, tuned".
- **S3 — Momentum / trailing.** Entry on confidence + expected-profit (optimized); exit =
  staged trailing-stop (profit-threshold tiers + SL offsets, all optimized) + time-exit (days
  optimized). No fixed TP — let winners run under the trail. Tests "let it run, trail it".

## Universes
- **U1 — NASDAQ-100** (static list).
- **U2 — large-cap screener** (high market-cap floor, no dip filter).
- **U3 — large-cap buy-the-dip** (large-cap + price-drop filter on).
- **U4 — mid-cap screener** (mid-cap band, no dip).
- **U5 — mid-cap buy-the-dip** (mid-cap + price-drop on).

## Phases
- **Phase 1 — strategy search (NASDAQ):** each expert × {S1, S2, S3} × **U1**.
  → 4 experts × 3 strategies = **12 jobs**. Picks the winning strategy structure per expert.
- **Phase 2 — universe search:** best (expert, strategy) from Phase 1 × {U2, U3, U4, U5}.
  → 4 experts × 4 universes = **16 jobs**. Picks the best universe per expert.
- **Phase 3 (optional, later):** re-test top combos on a held-out window / second metric for
  robustness.

## Implementation prerequisites (build before the phases run)
1. **Live-ruleset → optimizable Strategy importer** (for S1): read a live `enter_market` +
   `open_positions` ruleset from the dev DB and emit `buy_entry_conditions`/`exit_conditions`
   with optimize flags + ranges on every value. (Per-expert; FactorRanker has none → S1 = its
   factor-weight/top-N model:* params.)
2. **Entry-rule Adjust actions** in the backtest entry path: the live entry rules set TP/SL via
   Adjust actions on the BUY (referencing `expert_target_price`). The engine's entry seeding
   must apply those so S1 is faithful (today it only emits BUY + a separate initial bracket).
3. **Screener universe caches** (for Phase 2): build large-cap / mid-cap × dip-on/off caches via
   `ba2-test fetch-screener` (the as-of reconstruction is implemented + cache machinery is fast).
4. **FactorRanker S2/S3**: define equity-style alternatives or keep it factor-only (decide when
   we reach it).

## Defaults / conventions
- Each optimization tagged with `optimization_id`; top-N (distinct-fitness) persisted + report
  regenerated per expert.
- Strategy gene count drives population (≈ pop 60-100 depending on count).
- Commit per-repo to dev as pieces land; bump `ba2_trade_platform/version.py` only if that repo
  is touched.

## Progress / follow-up
| Item | Status |
|---|---|
| Plan validated (Calmar, 2 phases, no-static rule) | ✅ 2026-06-16 |
| Rule engine unified (trade+test share ba2_common.rule_builders; API action/comparison shape fixed) | ✅ 2026-06-16 (RE1/RE2/RE5) |
| Prereq 1: live-ruleset importer | ✅ enter_market→buy_entry_conditions + open_positions→exit_conditions importers, optimizable (±50% ranges), graceful live-DB read (GET /api/experts/{id}/enter-market-ruleset + /open-positions-ruleset) |
| Prereq 2: entry-rule Adjust actions | ✅ entry BUY/SELL(short) + bracket-at-open (8738c01) + expert_target_price-referenced TP bracket with expected_profit_percent fallback for all experts (RE3/RE4; tp gene = offset-from-target) |
| Prereq 3: screener caches (large/mid × dip) | ☐ (operational: run ba2-test fetch-screener for U2–U5) |
| S2 / S3 strategy definitions (all-optimized) | ☐ |
| Phase 1 — FMPRating × {S1,S2,S3} × U1 | ☐ |
| Phase 1 — FMPEarningsDrift × {S1,S2,S3} × U1 | ☐ |
| Phase 1 — FMPInsiderClusterBuy × {S1,S2,S3} × U1 | ☐ |
| Phase 1 — FactorRanker × {S1,(S2,S3?)} × U1 | ☐ |
| Phase 2 — winners × {U2..U5} | ☐ |
