# Test platform documentation

Documents for `ba2-test`, the backtesting and GA-optimization platform. Start with the
[test platform README](../README.md) (install, run, pages, CLI, API) and the
[Quick Start](../QUICK_START.md). Dated documents record a plan or finding at that date and
are not kept up to date afterwards.

## Living references

- [grid-and-fitness-guide.md](grid-and-fitness-guide.md) — launching and resuming the cap-band optimization grid, every fitness metric and knob, filtering results
- [robustness-suite.md](robustness-suite.md) — Monte Carlo and schedule-perturbation robustness tests on a saved backtest
- [daily-expert-backtest-scope.md](daily-expert-backtest-scope.md) — scope, guardrails and caveats of the daily expert backtest engine
- [exit-ruleset-ui-requirements.md](exit-ruleset-ui-requirements.md) — requirements for editing and optimizing open-positions exit rulesets in the UI

## Plans and analyses (dated)

- [optimization-plan-2026-06.md](optimization-plan-2026-06.md) and [optimization-jobs-plan-2026-06-17.md](optimization-jobs-plan-2026-06-17.md) — the June 2026 expert-optimization plan and job list
- [2026-06-17-5min-optimization-grid-analysis.md](2026-06-17-5min-optimization-grid-analysis.md) — tests and performance of the 5-minute optimization grid
- [2026-06-13-phase5-ohlcv-resource-cutover.md](2026-06-13-phase5-ohlcv-resource-cutover.md) — the `OHLCV_SOURCE` flag for re-sourcing ML datasets through the shared cache
- [superpowers/](superpowers/) — the backtest interface rework design and backend plan, and the options backtest design

## Data

- [live_rulesets/](live_rulesets/) — live-platform rulesets exported per expert by `backend/scripts/export_live_rulesets.py`, used as optimization starting points
- [screenshots/](screenshots/) — images used by the test platform README

## Historical

The test platform began as a standalone deep-learning forecasting app built over many agent
sessions. Its original specification (`spec/`), feature checklist (`feature_list.json`) and
a few session hand-offs (`implementation/`) are kept for the record; they do not describe the
current platform.
