# Ten-strategy research driver: completion and findings

The requested six follow-up experiments and four new ideas are implemented in
[the driver](../../tools/strategy_research/run_goal2020_followups.py). The default
campaign has **35 jobs and 193 search candidates**, each starting with **$10,000
equity and a $10,000 sizing cap**. Saving the top candidates requires additional
engine reruns. These are independent strategy accounts; this campaign does not
allocate a single $10,000 portfolio among ten experts.

The [runbook](../../docs/strategy_research/goal2020_followups.md) contains the exact
parameter grids, costs, schedules, cache requirements and launch commands.
The generated [default preview](default-10000/manifest.json) contains all jobs;
generated run folders are ignored by Git and can be recreated with `--dry-run`.

## Experiments included

| Family | Question tested |
|---|---|
| Large-cap DS | Does changing the quality/momentum blend and momentum horizon improve the deployed recipe? |
| Mid-cap Insider | Does a shorter holding period or fresher insider window help? |
| Small-cap EarningsDrift | Does a 60/90/120-day timeout improve the unlimited-hold control? |
| Mid-cap DS | Does a shorter timeout or bearish-signal exit help? |
| Mid-cap EarningsDrift | Which first-tier target offset works better with the existing signal and stop? |
| Small-cap FMPRating | Do timeouts or consistent brackets across tiers improve the asymmetric control? |
| Quality/momentum | How do blend, horizon, entry threshold, hold and stop interact in a separate liquid large-cap recipe? |
| Pullback | Which short RSI period, entry threshold and holding period suit daily entries? |
| Analyst targets | Which observation window, sample threshold and holding period suit the fixed analyst/technical blend? |
| ETF trend | Does holding the top one or two eligible funds work better with six- or twelve-month momentum? |

## Implementation findings

- Six explicit controls preserve the source rule semantics. Source backtest IDs and
  settings are recorded in the [baseline snapshot](../../tools/strategy_research/baselines_20260907.json).
  Controls use current code, migrated sizing keys and explicit schedules; they are
  not exact reproductions of historical pre-fix OK1000 equity curves.
- Old optimization flags and stale camelCase action values are removed before
  assigning new ranges. Screen thresholds and execution schedules remain fixed
  within each search. Equity and its sizing cap are explicit in saved results.
- Most ideas use existing experts and optimizable rules. ETF rotation needed a new
  shared [ETFTrend expert](../../packages/experts/ba2_experts/ETFTrend.py), registered
  with the backend. Its settings are optimizable and it uses the normal rules and
  risk-management path.
- ETF membership uses completed prior-month prices, positive momentum and SMA200.
  Missing or invalid basket data aborts backtests. Membership stays fixed during
  the month; daily execution manages entries and exits. This is membership rotation,
  not exact monthly dollar-weight rebalancing. Stops can trigger later re-entry;
  unfilled slots remain cash. No explicit cash-interest or dividend-income model
  was added.
- Preview is offline. Execution checks the test database schema before startup,
  uses per-job locks, saves concrete top-result configurations, and supports resume
  without duplicating completed top-result rows. Code and cache signatures separate
  prepared jobs when inputs change.

## Validation and remaining limitations

**127 targeted tests passed**, including parameter collection/decoding, optimization
handling, persistence/resume, shared live/backtest parity, and an ETF run through
the actual daily backtest engine and rules. See the
[validation record](validation_2026-09-08.txt). Tests use isolated databases and
hermetic fixtures.

Before committing, the same suite plus the 18 warmup tests was rerun against the
current `dev` checkout: **145 passed**. The three provider construction tests also
passed separately. The provider's optional API-key support is already present in
commit `317276ba` and is a dependency of the warmup script.

The read-only [cache preflight evidence](cache_preflight_2026-09-07.json) passes
for **nine of ten families**. Small-cap EarningsDrift fails because
`BID_5min.parquet` is missing from the configured OHLCV cache; that file was still
absent on September 8. Resolve its historical execution data before launching
that family. The driver deliberately refuses to silently remove the symbol.
The recorded checks sample coverage and apply a 75% eligibility threshold; they
do not certify every intraday gap or the experts' fundamental-data caches. A run
repeats preflight and records current signatures.

A subsequent [ETF session-level check](etf_cache_warmup_findings_2026-09-08.md)
found missing/short five-minute sessions for all four ETFs, including substantial
GLD history. The ETF family needs cache warmup despite passing this earlier,
coarser coverage gate; the linked report and warmup script cover that work.

An optional broader API test could not collect in the available validation runtime
because of a Torch/torchvision Windows dependency mismatch. Targeted validation used
bundled Python with existing dependency paths; no packages were installed or
upgraded. Use a working test-application Python environment for a real campaign.
This collection failure does not establish a production application failure.

**No research optimization campaign has been launched, and production settings
have not been changed by this implementation.** The new ideas have no measured
performance conclusion yet. The default 2020–2025 period is historical research,
not a fresh holdout; standalone rankings also do not establish portfolio
diversification or a superior ten-strategy combination.

Earlier deployment-parity and portfolio-allocator findings remain in their
respective reports under `reports/`; this driver does not fix the allocator bugs.
