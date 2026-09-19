# Ten strategy experiments at $10,000

Driver: [run_goal2020_followups.py](../../tools/strategy_research/run_goal2020_followups.py).
Profiles: [profiles.py](../../tools/strategy_research/profiles.py).
Sources: [six deployed-strategy reviews](../../reports/deployed_strategy_review_2026-09-07.md)
and [four new ideas](../../reports/expert_strategy_ideas_2026-09-07.md).

The default campaign contains **ten families, 35 jobs and 193 parameter combinations**.
It evaluates the complete small grids rather than relying on a genetic search to visit
their neighbours. An optional genetic mode uses the existing goal2020 optimizer and
configured remote workers. Jobs run sequentially in separate processes.

## Experiments

| Family / CLI selector | Changes searched | Jobs | Grid candidates |
|---|---|---:|---:|
| `large_ds` | Original control; quality/momentum 70/30, 50/50, 30/70 with 126/252-bar momentum. Original screen, entry thresholds and exits fixed. | 4 | 7 |
| `mid_insider` | Control; timeout 60/90/120 days; separate insider lookback 30/60/90/120 days with the 210-day control timeout. | 3 | 8 |
| `small_earnings` | Unlimited-hold control versus 60/90/120-day timeout; original stop and profit exit fixed. | 2 | 4 |
| `mid_ds` | Control; timeout 15/20/25/30 days; separate bearish exit with the original 25-day timeout. | 3 | 6 |
| `mid_earnings` | Control; first-tier target offsets −14/−12/−10/−8%; signal, stop and timeout fixed. | 2 | 5 |
| `small_rating` | Asymmetric control; 60/90/120-day timeout; both tiers using the original near bracket or original wide bracket. Existing floor stop retained. | 4 | 6 |
| `quality_momentum` | Independent liquid large-cap recipe: blends 70/30, 50/50, 30/70; momentum 126/252 bars; buy threshold 0.2/0.3/0.4; hold 30/60/90 days; requested entry stop −12/−8%. | 3 | 108 |
| `pullback` | RSI period 2/3/5 and hold 3/5/10 days in separate jobs; buy threshold 0.15/0.25/0.35. Daily entry, fixed stop and costs. | 9 | 27 |
| `analyst_targets` | Hold 15/30/60 days; target window 30/60/90 days; minimum 3/5 observations. Analyst/technical blend fixed at 80/20. | 3 | 18 |
| `etf_trend` | Prior-month momentum 126/252 bars, positive return and above SMA200; top 1/2 eligible funds. | 2 | 4 |

Six explicit controls preserve the original rule semantics. Other fixed settings are
snapshotted in [baselines_20260907.json](../../tools/strategy_research/baselines_20260907.json),
with original and capped backtest IDs. The two quality/momentum families answer different
questions: a narrow modification of the deployed recipe, and a separately defined new recipe.
Candidate totals include controls and deliberate repetitions of a control value within a grid.
Saving the top results requires additional backtest reruns, so 193 is the search count,
not the total number of engine executions.

## Capital, execution and screening

- Every job starts with **$10,000**, with a **$10,000 sizing cap**. Profits remain recorded
  but do not expand the sizing base above that cap. `--equity-cap 0` enables uncapped
  compounding. `--equity` and `--equity-cap` are separate, explicit controls.
- Each job has its own account. This does not allocate one $10,000 account across ten
  simultaneous strategies, nor establish the best portfolio combination.
- Five-minute execution and `next_bar_open` fills. Entry scans are Monday 09:30 for
  the six follow-ups, quality/momentum and analyst targets; weekdays 09:30 for pullbacks
  and ETF execution. Management runs weekdays 09:30 for all jobs.
- Original families retain their source commission, spread and additional stress-spread
  assumptions: $0.10 commission per trade, 3 bps spread for large DS, 9 bps for the mid-cap
  families, and 17 bps for the small-cap families. Their stored robustness settings travel
  with them. New ideas use $0.10 commission, 5 bps spread plus 5 bps **additional** stress
  spread, zero separate slippage and robust fitness. `--spread-bps` overrides the base
  spread for all selected families, including a deliberate zero.
- Fitness is `consistent_annual_return`, with the source 2,000% individual-profit and
  25% gross-winner-share scoring caps. These are scoring controls, not the account equity cap.
- Stock screens are fixed throughout each search. The six follow-ups use their saved
  screen thresholds. The three new stock ideas use a common large-cap universe: market
  cap ≥$10bn, price ≥$10, volume ≥500k, dollar volume ≥$10m, float ≥10m, up to 50 names
  sorted by market cap. Relative-volume, minimum price-drop and Stage-2 filters are off.
  Both the metric-store keys and live expert setting names are explicit.
- The current shared trial builder requires `use_atr_stop=False` and
  `regime_overlay_enabled=False`; both are pinned. Existing rule stops and ordinary risk
  limits still apply. Requested brackets can be affected by execution/risk floors and gaps.

The controls run on current code with explicit Monday schedules, migrated sizing parameter
names and the current boolean contract. They are **new controls**, not a claim to reproduce
the pre-fix OK1000 curves exactly. The old optimization toggle flags and stale camelCase
action values have been removed from the concrete rules before new ranges are added.

## Preview and launch

### ETFTrend cache warmup

[warm_etf_cache.py](../../tools/strategy_research/warm_etf_cache.py) prepares the
fixed ETF basket independently of the stock/fundamental warmup tools. ETFTrend
uses only OHLCV data: prior-month momentum (126/252 trading bars), SMA200, and
current completed daily prices. The research grid selects at most one or two
eligible funds from SPY, IEF, TLT and GLD; unfilled slots remain cash.

```powershell
python tools/strategy_research/warm_etf_cache.py --dry-run
python tools/strategy_research/warm_etf_cache.py --check
python tools/strategy_research/warm_etf_cache.py --run
```

The default 2020–2025 campaign needs **2018-05-11 through 2025-12-31**, including
600 calendar days of warmup. Both daily and five-minute history are prepared,
matching the engine's preload window. Override `--start`, `--end` and `--symbols`
alongside the research driver's corresponding dates and `--etf-symbols`.

The script downloads missing or short sessions in small chunks, keeps existing
history and saves each valid response atomically. Re-running resumes from the
actual cache. Empty/incomplete responses exit nonzero; partial valid data remains
available for the next attempt. NYSE holidays and early closes are accounted for.
Coverage requires one daily row, or at least the regular-session five-minute row
count per date. Extended-hours rows can mask individual intraday gaps, so this is
not a certification of every expected timestamp. Conflicting `_5m`/`_5min` files,
invalid prices and duplicate timestamps fail explicitly.

`--cache-dir` points to the same `FMPOHLCVProvider` directory as the research
driver. Credentials come from `FMP_API_KEY` in the environment or a **read-only**
query to `--settings-db` (default: the test application's database). No application
startup or DB migration is needed. Reports go under
`reports/strategy_research/etf-warmup/`. `--check` never loads credentials or fetches
data; `--run` skips already-covered pairs without requiring credentials.

This ETF script does not warm stock fundamentals or repair the separate small-cap
EarningsDrift `BID_5min.parquet` gap.

### Research driver

Run from the monorepo using the **test application's Python environment**:

```powershell
python tools/strategy_research/run_goal2020_followups.py --dry-run
python tools/strategy_research/run_goal2020_followups.py --preflight
python tools/strategy_research/run_goal2020_followups.py --run
```

Preview is the default and uses only the standard library. It opens no database and starts
no backtest. It writes the full manifest under `reports/strategy_research/<campaign hash>/`.
`--output-dir` can place artifacts elsewhere; the repository root itself is rejected.

`--preflight` reads the existing screener/OHLCV caches. It resolves the actual fixed-screen
union, requires all selected symbols' daily and execution-interval files, and samples up
to 150 symbols for warmup/start/end coverage. The default gate is 75% of eligible symbols;
late listing dates are reported separately using daily history as an existence proxy.
This is an early coverage gate, not a complete audit of every intraday gap or fundamental
record. It never fetches missing data. The engine still requires prewarmed fundamental,
earnings, analyst and other provider caches used by each expert.

`--run` repeats preflight in each job's child process, then writes only to the specified
**test database**. The default is `BA2_HOME/test/dl_forecasting.db`, or
`~/Documents/ba2/test/dl_forecasting.db` without `BA2_HOME`. `--db-file` is explicit and a
read-only schema check rejects a live-only database before backend startup. `--cache-dir`
selects the `FMPOHLCVProvider` directory; `--store` overrides the metric store.

Useful subsets and optional genetic mode:

```powershell
python tools/strategy_research/run_goal2020_followups.py --families mid_ds small_earnings --dry-run
python tools/strategy_research/run_goal2020_followups.py --families mid_ds --variants control --run
python tools/strategy_research/run_goal2020_followups.py --families etf_trend --etf-symbols SPY IEF TLT GLD --preflight
python tools/strategy_research/run_goal2020_followups.py --search genetic --population 24 --generations 4 --workers remote227 --parallel 0 --run
```

Worker names must exist in the test application's worker settings. Remote hosts must have
this code, including `ETFTrend`, and matching prewarmed data installed before launch. The
existing optimizer's remote synchronization remains responsible for its worker lifecycle.
Fixed controls and saved top-result reruns execute locally, including when the GA uses
`--parallel 0`. Exhaustive grids are local and serial; remote names are rejected in grid mode.

Each job has a `.log`, `.prepared.json` and, after successful persistence, `.result.json`
containing database IDs. The configuration, code signature, screened union and cache file
metadata distinguish prepared jobs. File metadata is not a cryptographic snapshot of all
historical data contents. Locks live beside the destination database and prevent duplicate
driver execution even with different output folders. No process sweeping is performed.

Completed jobs are reused after comparing their actual stored configuration and rules.
Partially saved top results are repaired without duplicating completed rows. A failed child
stops the campaign with a nonzero exit status. After stopping the previous runner, add
`--resume` to revisit unfinished jobs. The genetic engine can resume its checkpoint; the
existing exhaustive-grid engine restarts an interrupted small grid. The optional GA's
history after a checkpoint resume can omit pre-resume neighbours, a limitation of that handler.

## ETF and signal semantics

[ETFTrend](../../packages/experts/ba2_experts/ETFTrend.py) is a new shared expert. Its illustrative
research universe is SPY, IEF, TLT and GLD; override it with `--etf-symbols`. Instrument choices
are inputs for backtesting, not a completed broker-eligibility or affordability assessment.

The expert ranks prices from the last completed month, keeps monthly membership constant,
and emits BUY only for positive-momentum funds above SMA200. Unselected funds emit SELL;
the configured **close** rule handles held positions, while short entries remain disabled.
When none qualifies the target membership is empty. At most one or two names can qualify;
notional sizing allocates at most 90% divided by the requested number of slots, leaving
unused slots as cash. Membership changes are processed through normal daily order/fill
timing. Existing positions are not resized each month to exact equal-dollar weights.
The initial rule stop is −10%; there is no profit-target rule. A stopped selected fund can
re-enter after the one-day cooldown while it remains in the monthly selection.

This uses price momentum under the OHLCV provider's adjustment conventions. It does not add
a separate dividend or cash-interest model. The expert reads only completed daily candles;
an entry day's final close cannot affect its signals. Missing/stale basket data aborts a
backtest rather than masquerading as a decision to hold cash. Its confidence of 100 describes
deterministic rule membership, not a probability of earning a profit; expected profit is zero
because the expert does not forecast a price target.

Pullbacks use an RSI-weighted **blended score**, not a literal `RSI < 20 AND close > SMA200`
rule. Analyst-target scoring retains the existing fixed blend of target drift and implied
upside; it is not a pure analyst-upgrade event measure. Both FMPRating and the analyst-target
experiment start no earlier than **2022-01-01**. Other families default to 2020-01-01 through
2025-12-31. Those years have already been searched and are not a fresh holdout. Compare
neighbours, losses, holding time and capital use, then use genuinely unused or forward paper
results before considering a shared-account allocation.

## Market-condition extension — driver ready, reviewed 2026-09-17

Review: [market-condition implementation and option stage 1](../../reports/strategy_research/market_conditions_review_2026-09-16.md).
Shared design: [condition genes](../plans/2026-09-15-option-market-condition-genes-design.md),
especially the equity placement contract in section 6.0.

The ten experiments above remain the original **35 jobs / 193 combinations at $10,000**.
The following is an additional, opt-in research campaign; it does not change those controls,
their saved IDs, or their backtest results. The `regime_overlay_enabled=False` setting above
remains pinned: these shared entry conditions are separate from that older overlay.

**Current implementation boundary:** the follow-up driver now exposes both profiles for its
equity and ETF entry rules. It remains opt-in and requires a pinned manifest for every selected
profile. Option stage 1 is unchanged. Stage 2 is outside the current review.

### Conditions to reuse

| Profile | Fields searched by the implemented option profile | Extra genes per entry tree |
|---|---|---:|
| `ohlcv-v1` | EMA50 slope normalized by ATR14; ADX14; 5/20-session realized-volatility ratio | 6 |
| `ta-structure-v1` | Distance to confirmed support; distance to confirmed resistance; channel position; close relative to prior 20-session high; bull/bear swing structure | 9 |
| Both | The eight optional gates above | 15 |

Numeric modes are off/below/above with a threshold; swing structure is off/bull/bear.
Reuse registry definitions, ranges, calculator versions and `prior_session_v1` timing.
These are conditions on the **traded stock or ETF**, not an additional benchmark regime.
The other seven calculated structure fields remain diagnostics for the first campaign.

### Questions for all ten families

These are hypotheses for the new comparisons, not predetermined winning thresholds. Preserve
the selected expert recipe, screen, direction, schedule, exits, costs and capital while testing
entry gates. The GA can leave any gate off or choose either numerical comparison direction.

| Family | Useful condition question | What to inspect beyond CAR |
|---|---|---|
| `large_ds` | Does a positive underlying trend or confirmed bullish structure improve the quality/momentum blend? Does resistance distance distinguish useful entries from late entries? | Redundancy with the expert's momentum signal; lost large winners; capital use. |
| `mid_insider` | Are insider signals more useful with trend confirmation, or during weak-trend conditions near support? | Preserve insider lookback and timeout; separate fewer opportunities from better selection. |
| `small_earnings` | Does post-signal continuation benefit from trend/breakout confirmation, or from avoiding exceptionally expanded realized volatility? | Gap losses, missed earnings winners, initial-history coverage and concentration. |
| `mid_ds` | Does the short holding period work better on pullbacks within an uptrend or on expanding momentum? | Holding time, trade frequency and overlap with `large_ds`. |
| `mid_earnings` | Does trend/structure confirmation improve the selected target-offset recipe? | Keep profit tiers fixed in this comparison; measure profit and average/peak capital together. |
| `small_rating` | Do positive ratings work better near support, with available resistance distance, or after trend confirmation? | Preserve asymmetric brackets; watch the top-five contribution after filtering. |
| `quality_momentum` | Is a slope/ADX gate useful beyond the expert's existing momentum terms? | Incremental improvement over the identical blend; turnover and robustness across seeds. |
| `pullback` | Can positive trend plus lower channel position/support proximity distinguish recoverable pullbacks from continued declines? | Oversold recommendations rejected, stop frequency, fewer trades versus better expectancy. |
| `analyst_targets` | Does prior-session price confirmation help target revisions, or remove profitable early entries? | Keep target-window/observation settings fixed; preserve the 2022 data floor. |
| `etf_trend` | Does short-term trend/volatility or structure improve the timing of opening an already selected fund? | Time in cash, missed trend starts, turnover and CAR; membership remains the existing monthly decision. |

For ETFTrend, a failed gate leaves an eligible slot in cash. It must not select an alternative
fund, alter monthly membership, change slot sizing, or prevent the normal deselection close.
For earnings drift, these gates do not introduce a new earnings-calendar entry/exit policy.

### Driver implementation and launch contract

The driver implementation now does the following:

1. It factors the registry-derived market-leaf builder into the shared package and appends gates
   to **each opening rule/tier**, preserving existing OR groups and first-match behavior. Exits,
   reductions, stop updates and protective actions are never gated.
2. `run_goal2020_followups.py` accepts `--market-condition-profile none|ohlcv-v1|ta-structure-v1|ohlcv-v1,ta-structure-v1`,
   `--market-condition-manifest`, and `--market-condition-mode search|all-off`. `none` remains
   the default and a profile-less manifest is refused. `search` requires the genetic path;
   `all-off` is a pinned installation/parity arm with the original decoded rules.
3. Profile selection, gene layout, manifest digests, calculator/source/timing identities and the
   recipe are included in the job fingerprint. New names cannot resume an existing ungated job.
   Pins are copied into every trial and saved-top-result configuration.
4. `--preflight` verifies published objects, source/timing/calendar/calculator identity and
   every required prior-session row before a trial. It reports legitimate initial-history and
   undefined-structure observations separately and refuses wrong-window, interior-hole,
   trailing-hole and all-unusable snapshots. It never fetches data.
5. `--export-universe PATH` writes the union of selected screen symbols and the ETF basket to a
   research subdirectory so the existing warmup tool can prepare one central cache. It does not
   open the database or start jobs.

The live resolver now selects the subset of a host manifest needed by each expert, so different
profile sets can coexist on one host. Keep the mixed-profile resolver checks in the deployment
gate. The driver can prepare and replay a single profile or a combined pin.

### Warmup and central-cache preparation

Resolve the **union of possible symbols** from all selected fixed stock screens and the ETF
basket, over their research dates, before optimization. The option grid's 98-symbol universe
is not sufficient for the small/mid-cap follow-ups. Do not warm only the eventual winners.

Use the existing `tools/warm_market_conditions.py` sequence for each requested profile:

```powershell
# After producing the resolved union file; run from the repository root.
$research = "reports/strategy_research/goal2020_market_conditions"
New-Item -ItemType Directory -Force $research | Out-Null
python tools/warm_market_conditions.py plan --profile ohlcv-v1 `
  --universe-file "$research/universe.txt" --start 2020-01-01 --end 2025-12-31 `
  --out "$research/ohlcv-v1-plan.json"
python tools/warm_market_conditions.py build --plan "$research/ohlcv-v1-plan.json" --cache-only
# Repeat plan/build for ta-structure-v1. Retain each returned digest separately.
python tools/warm_market_conditions.py verify --manifest <digest>
python tools/warm_market_conditions.py prepare-host --manifest <digest> --profile <profile>
```

The placeholders above must be replaced with the actual digest/profile. The union file is created
by the driver's `--export-universe` command and is intentionally not checked into the repository.
Dates passed to `plan` are decision dates: it handles prior regular sessions and the required
128-bar prefix. Retain the ETF expert's longer warmup and existing daily/five-minute execution data,
screener, analyst, ratings, insider and earnings caches; condition features replace none of them.

`plan` also checks source/split metadata and can request missing split-calendar data.
`build --cache-only` does not fetch missing price history. Review its inventory before using
the explicit `--fetch-missing` repair path; do not assume live refresh automatically repairs
split history. Inspect negative-status coverage as well as symbol counts. Undefined support
or resistance is a legitimate unknown observation and is not solved by another download.

Reuse central content-addressed feature/raw objects for overlapping windows and symbols.
Changing a threshold, expert or fitness does not require rebuilding identical features.
Pin the resulting manifest(s), sync them to participating workers, and require successful
verification/preparation before dispatch. Read-only mapped arrays are per host, while every
trial carries the same portable digests. No price download or feature calculation belongs
inside a GA trial. A repeated unchanged build should reuse its feature computations.

### Comparison and selection

Keep the original small exhaustive grids as the first step. For each family, choose and
record a control recipe before searching gates, then compare **ungated / OHLCV / structure**
with matched seeds and fixed economic settings. Test both profiles together only where the
single-profile results justify the extra search dimensions. Do not multiply all 193 original
combinations by every threshold combination; use separate condition-focused GA jobs.

Record the added gene count and explicit search budget; the option defaults do not establish
adequate search depth for this new campaign. Explicitly evaluate the frozen all-off control;
random initialization is not a guarantee that the optimizer visits it.

Compare profit, CAR, maximum drawdown, trade count/frequency, average and peak capital usage,
top-five winner contribution, and trade overlap with the control and alternate sleeve.
Report which eligible recommendations were rejected by measured conditions versus unknown
data, and whether rare large winners disappeared. Prefer improved CAR at comparable capital
use with tolerable drawdown/concentration, rather than fewer trades alone. Keep $10,000
initial equity and sizing cap, existing costs, schedules and fitness throughout each comparison.
The already searched 2020–2025 window is research data; preserve unused/forward evaluation.

## Verification

```powershell
cd testplatform/backend
python -m pytest tests/test_research6_driver.py tests/test_research10_market_conditions.py `
  tests/test_launcher_market_condition_profile.py tests/backtest/test_etf_trend.py -q
```

Tests cover exact search dimensions, source rules, settings recognition, schedules, caps,
fixed-screen mappings, causal ETF decisions, a real-engine ETF fill, isolated database
persistence/recovery, duplicate locks, and child failure propagation. Production accounts
and market APIs are not used by these tests.

2026-09-19 pre-commit validation: the two driver suites and
`test_launcher_market_condition_profile.py` passed **193 tests** on the current branch.
