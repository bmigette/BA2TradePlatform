# Documentation index

A map of what is in `docs/`. It lists the documents worth reading first; it does not
summarise every file. Dated documents (plans, memos, reviews) record a decision or a finding
at that date and are not kept up to date afterwards: check the code before relying on them.

## Start here (outside `docs/`)

- [../README.md](../README.md) — the live trade platform (`ba2-trade`): features, install, run, architecture
- [../testplatform/README.md](../testplatform/README.md) — the backtest and GA-optimization platform (`ba2-test`)
- [../EXPERTS.md](../EXPERTS.md) — every expert, its settings and scheduling
- [../CLAUDE.md](../CLAUDE.md) and [../AGENTS.md](../AGENTS.md) — development conventions (package vs in-tree code, config access, logging, versioning)
- [../MIGRATIONS.md](../MIGRATIONS.md) — Alembic database migrations
- [../MIGRATION.md](../MIGRATION.md) — how the separate BA2 repos became this monorepo
- Package READMEs: [common](../packages/common/README.md), [providers](../packages/providers/README.md), [experts](../packages/experts/README.md)

## Reference

- [FACTORRANKER_EXPERT.md](FACTORRANKER_EXPERT.md) — the FactorRanker expert: factors, universe, self-executing rebalance
- [WASHTRADE-LOCK.md](WASHTRADE-LOCK.md) — wash-trade locking decision record; read before changing the wash-trade path
- [features/expert_priority.md](features/expert_priority.md) — scheduled expert priority (ordering of experts that fire at the same instant)
- [FMP_BACKTEST_FEASIBILITY.md](FMP_BACKTEST_FEASIBILITY.md) — which experts can be backtested from FMP history (2026-06 survey)
- [screener_price_drop_fix.md](screener_price_drop_fix.md) — the screener metric store's `price_drop_pct` fix and why the store had to be rebuilt

## Runbooks and strategy research

- [REPRODUCE-BACKTESTS.md](REPRODUCE-BACKTESTS.md) — reproduce our equity and option backtest results from scratch: keys, hardware, data preparation, grids, verification, time budget
- [RUNBOOK-goal2020-grid.md](RUNBOOK-goal2020-grid.md) — start, watch, stop and resume the goal2020 optimization grid
- [strategy_research/exploration/README.md](strategy_research/exploration/README.md) — the strategy exploration grid (driver `tools/strategy_research/exploration/run_exploration.py`)
  - [market_conditions.md](strategy_research/exploration/market_conditions.md) — market-condition entry gates in that grid
  - [pullback_and_market_exits.md](strategy_research/exploration/pullback_and_market_exits.md) — proposed pullback expert and market-condition exits (not implemented)
- [strategy_research/options/](strategy_research/options/) — option grid experiment contracts ([soft 30-trade fitness](strategy_research/options/option_stage1_soft30.md), [HOLD vs low-confidence entries](strategy_research/options/option_neutral_entry_experiments.md))

## Plans and designs

Design documents and implementation plans live in [plans/](plans/) (dated `YYYY-MM-DD-*.md`) and
in [superpowers/](superpowers/) (`specs/` for designs, `plans/` for implementation plans,
`reviews/` for review findings). Most describe work that has since shipped. Good entry points:

- [Backtest platform roadmap](plans/2026-06-13-backtest-platform-ROADMAP.md) — the package split and backtest program, phase by phase
- [Live/backtest engine unification](plans/2026-07-02-live-backtest-engine-unification.md) and [unified rule model](plans/2026-07-08-unified-rule-model.md) — why live and backtest share one decision path
- [Options trading design](plans/2026-06-05-options-trading-design.md), [option risk manager](superpowers/specs/2026-08-27-option-risk-manager-design.md), [option model and lifecycle](superpowers/specs/2026-08-24-option-model-and-lifecycle-design.md)
- [Options data and intraday roadmap](plans/2026-07-25-options-data-and-intraday-roadmap.md)
- [Margin trading design](plans/2026-09-08-margin-trading-design.md)
- [Portfolio allocation design](superpowers/specs/2026-08-20-portfolio-allocation-design.md)
- [Live capture and backtest replay spec](plans/2026-09-10-live-capture-prewarm-backtest-replay-spec.md)
- [Forward-test account allocation](plans/2026-09-13-forward-test-account-allocation.md)
- [Shared arrays across GA workers](plans/2026-09-14-shared-arrays-across-workers.md)
- [Option market-condition genes](plans/2026-09-15-option-market-condition-genes-design.md)
- [BT/live option parity](plans/2026-09-22-bt-live-option-parity.md)
- [Trigger picker design](plans/2026-09-22-trigger-picker-design.md)

## Memos, audits and reviews (historical)

- Memos: [test-account spread/cache audit](2026-07-16-test-account-spread-cache-audit-memo.md),
  [news signal findings](2026-08-11-news-signal-findings-memo.md),
  [alias-shim import race](2026-08-17-alias-shim-race.md),
  [ThetaData EOD backfill assessment](2026-09-03-thetadata-eod-backfill-assessment.md)
- Options research: [audit and fixes](2026-07-22-options-audit-and-fixes.md),
  [index/ETF vs single-stock strategies](2026-07-25-options-stocks-vs-etf-strategies.md),
  [external research report](2026-07-25-options-strategies-automated-trading-research.md)
- Code reviews: [2026-06-10 comprehensive review](COMPREHENSIVE_REVIEW_2026-06-10.md)
  (with its [verification](COMPREHENSIVE_REVIEW_2026-06-10.claude-local.md) and [triage](COMPREHENSIVE_REVIEW_2026-06-10.triage.md)),
  [2026-08-04](code-review-2026-08-04.md) (in French), [2026-09-22](code-review-2026-09-22.md)

## Other

- [screenshots/](screenshots/) — images used by the root README
- Test-platform documentation lives in [../testplatform/docs/](../testplatform/docs/README.md)
