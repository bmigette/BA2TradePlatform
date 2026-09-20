# Option grid review before neutral-entry experiments

Reviewed the dedicated grid database on `debian@141.94.199.227`, under
`/home/debian/ba2-grid/home/test/dl_forecasting.db`, before the next restart.
All values below describe historical, optimized in-sample results. They are not
live returns or a completed out-of-sample validation.

## Existing results worth preserving

Both campaigns use January 2020–December 2025 and $20,000 starting equity.

| Candidate | Status | Total return | CAR | Maximum DD | Trades | Top 5 share of net profit |
|---|---|---:|---:|---:|---:|---:|
| FMPRating O_LC, optimization 1, saved BT 2 / TOP1 | Completed | +1,997.11% | 66.20% | 40.06% | 400 | 37.93% |
| DeterministicScorer O_LC, optimization 6 leader | Provisional; pool stalled | +1,545.32% | ~59.5%* | 47.41% | 863 | 38.21% |

*DS CAR is an approximate raw CAGR derived from total return over six calendar
years. A complete saved backtest was not produced for this unfinished job, so it
must not be presented as the engine's reported annualized-return field.
Implied raw profits on $20k are approximately $399,422 and $309,064 respectively.

The FMPRating leader scores 16.5128, versus 11.5335 for the provisional DS leader
under the same older `option_car_target` robust objective. FMPRating TOP4 has higher
raw CAR (68.86%) but also higher drawdown (43.12%); TOP1 wins the composite score.
FMPRating remains excluded from the current campaign per the existing operator
decision, but its saved results should remain available for comparison.

The failed DS job evaluated 814 candidates: 76 positive fitness, 66 negative,
66 zero score, 256 zero trades, 309 below the older trade floor and 41 wipeouts.
Its strongest candidate bought calls with the direction gate off, delta 0.30,
DTE 60 and 2% sizing; ADX >25, realized volatility >0.5 and channel position >0.1
were active. It used a 150% profit exit / 25-day time exit and no stop-loss exit.
The next higher-return candidate (+1,924.72%) had 71.43% drawdown and a much worse
fitness of 6.22. Higher return alone therefore does not make it preferable.

The DS leader's largest trade contributed 12.95% of net profit; the top five 38.21%.
Its trade-bootstrap 5th-percentile return was +696.88%, versus +818.37% for the FMP
leader. Those concentration/Monte Carlo checks are encouraging within this sample,
but do not test regime transfer, GA selection bias or live execution. Review the
saved trade histories and replay the unfinished DS leader before treating it as
a deployable replacement. The six-year aggregate can conceal weak individual years.

The current optimization 7 uses `option_car_target_soft30`. Early results were too
sparse for comparison. A two-trade +45.92% candidate still scored zero after the
concentration adjustment; the soft trade floor does not remove robustness guards.
The older DS winner has enough trades to saturate either count factor, so changing
the floor does not by itself improve that candidate's score.

Structured evidence, including DS leader parameters and saved Top-N summaries:
[pre-change snapshot](option_grid_pre_neutral_2026-09-20.json).

## Preservation requested by the user

Completed optimization 1 and failed optimization 6 were renamed with
`-archive20260920-opt<ID>`. The five completed backtests retain IDs 1–5 and their
TOP1–TOP5 labels, with `-archive20260920-bt<ID>` appended. Every other column was
hashed before and after the transaction and matched, including result payloads,
parameters and optimization links. No backtests were deleted or rerun.

Backup and rename receipt:
`/home/debian/ba2-grid/archive/before-neutral-rename-20260920T080720Z/`.
The backup uses SQLite's backup API; `rename_receipt.json` lists both names and
unchanged-payload hashes. Rows still labelled running (2–5 and 7) were not renamed.
Automatic approval review rejected a broader rename because of those status labels;
the narrowed completed/failed-only transaction succeeded.

## Implemented experiment change

Explore HOLD and low-confidence BUY/SELL independently for straddles, strangles
and iron condors. See [the experiment contract](../../docs/strategy_research/option_neutral_entry_experiments.md).
The default remains legacy; no live expert is opted in. The discovery plan becomes
19 jobs for DeterministicScorer, with distinct arm names and inherited Top-N names.
The 13 other structures keep their job/checkpoint identities.

## Validation

- Full backtest suite plus option driver, launcher, gene, scoring and export checks:
  **1,526 passed, 1 skipped, 1 expected failure**. Existing golden parity/fingerprint
  assertions passed unchanged.
- Live funded-entry, new neutral-entry, option evaluator and action checks:
  **69 passed**, using mock brokers and isolated databases.
- New cached two-leg tests cover HOLD, low-confidence BUY/SELL, high-confidence
  rejection and legacy HOLD rejection. Live tests additionally cover latest-signal
  precedence, duplicate prevention, automated-opening disabled and refusal to turn
  HOLD into an equity order. Export tests retain the policy after trial construction
  and when the optimization row is unavailable.
- The initial broader run caught the ORM's null value for declared-but-unsaved
  settings. The compatibility resolver now treats missing/null as legacy; all
  reported pass counts above are from the corrected implementation.

APP `2026.09.1175`; TEST `2026.09.0062`. Operational restart evidence follows separately.
