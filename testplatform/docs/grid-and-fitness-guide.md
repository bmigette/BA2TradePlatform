# Screener Grid & Fitness Guide (living reference)

How to launch/resume the cap-band optimization grid, what each fitness metric and knob does,
and how to filter results afterward. Unlike the dated planning docs in this folder (e.g.
`optimization-jobs-plan-2026-06-17.md`), this file is meant to be kept up to date as the grid
driver and fitness module change — update it in the same commit as any CLI/knob change.

Source of truth for the actual flag list is always `--help`:
```bash
ba2-venvs/test/Scripts/python.exe tools/run_screener_capband_matrix.py --help
ba2-venvs/test/Scripts/ba2-test.exe optimize --help
```
This doc explains *what the flags mean and when to use them*; it doesn't duplicate every default.

---

## 1. Running the grid

The grid driver (`tools/run_screener_capband_matrix.py`) runs one `ba2-test optimize` job per
(cap-band × expert × strategy) combination, SEQUENTIALLY — each 5min job preloads 500-1100
symbols, so parallel jobs would blow memory. Jobs are named `scr-<band>-<expert>[-<S?>]` and are
**idempotent/resumable**: a job whose `StrategyOptimization` row is already `completed` is
skipped, so killing and re-running the driver just continues where it left off.

```bash
ba2-venvs/test/Scripts/python.exe tools/run_screener_capband_matrix.py \
    --bands large,mid,small --strategies S1,S2,S3,S4,S5,S6,S7 \
    --fitness consistent_annual_return \
    --name-suffix=-goal6 \
    --workers remote150 \
    --fitness-trade-scale --fitness-win-rate-factor
```

Key args:
- `--bands` / `--strategies` — which cap bands and strategy variants to sweep. Default order is
  large → mid → small (fastest first), FMPRating before the other experts.
- `--name-suffix` — appended to every job name (e.g. `-goal6`). **Always use a fresh suffix when
  re-launching after a code/fitness change** — job names double as the resume key, so an old
  suffix would just skip everything as "already completed" even though the underlying code
  changed. It also becomes part of the auto-label (see §3).
- `--workers` — comma-separated remote worker names to distribute GA trials to (must be
  registered + cache-synced first).
- `--dry-run` — print the job list without running anything; use this to sanity-check
  `--bands`/`--strategies`/`--name-suffix` before committing to a multi-day run.
- `--include-no-data` — also run FMPEarningsDrift/FMPInsiderClusterBuy on the large-cap band
  (skipped by default — FMP has no usable large-cap data for either).
- `--skip-experts` — comma list of expert class names to exclude entirely (e.g. defer
  `FMPInsiderClusterBuy`, which is much slower per-backtest than the others).

**Restarting after a code change to the risk/exit engine (e.g. TradeActions.py):** the grid
driver process holds already-imported Python modules in memory — editing source on disk does
NOT get picked up by a running process. If you fix something in the shared engine (SL/TP logic,
fitness formula, etc.) while a grid is running, you must **kill and restart** the driver process
for the fix to take effect on any job that hasn't started yet; jobs that already completed under
the old code are NOT automatically redone. Always re-launch with a fresh `--name-suffix` (see
above) rather than editing code and letting the old process keep going.

---

## 2. Fitness metrics

Set via `--fitness <metric>` (grid driver) / `ba2-test optimize --fitness <metric>`. The
authoritative list is `app/services/strategy_fitness.py::METRICS_CATALOG` (also served at
`GET /api/optimization/fitness-options` for the UI) — a metric added there without catalog
metadata fails a drift-guard test, so the catalog can never silently drift from what
`compute_fitness` actually accepts.

| Metric | Aliases | What it optimizes |
|---|---|---|
| `sharpe_ratio` | `sharpe` | Risk-adjusted return (mean/stdev). |
| `total_return` | `return` | Raw total return over the run. |
| `profit_factor` | | Gross profit / gross loss. |
| `win_rate` | | Share of winning trades. |
| `sortino_ratio` | `sortino` | Downside-risk-adjusted return. |
| `calmar_ratio` | `calmar` | Annualized return / max drawdown. |
| `sqn` | | Van Tharp System Quality Number. |
| `max_drawdown` | `max_dd`, `drawdown` | Negated (minimized): fitness = `-max_drawdown`. |
| `consistent_annual_return` | `car`, `goal` | See below — the current default "goal" metric. |
| `option_consistent_annual_return` | `option_car`, `ocar` | OPTION-only: the goal metric with a **superlinear** `(20/max(dd,5))²` drawdown penalty. The auto-default for pure-option strategy kinds. |
| `option_car_over_risk` | | OPTION-only: **~50%/yr WITH a drawdown tolerance** — `annualized_return / sqrt(max(dd,10%))`, full credit up to a 40% drawdown then a `(40/dd)^1.5` penalty, × consistency × the same trade gate. Never a default; name it explicitly. |
| `option_car_target` | | OPTION-only: **CAR > 35%/yr AND CAR > drawdown** — `annualized_return × min(CAR/35, 1) × min((CAR/DD)/1, 1) × the same (40/dd)^1.5 penalty past 40% dd`, × consistency × the same trade gate. Both ramps are **soft** (a hard gate would zero a whole fresh population and leave the GA no gradient) and neither pays anything above its target, so over-safety earns nothing. The only one of the three that prices the CAR/DD **ratio**. Never a default; name it explicitly. |
| `option_convex` | | CONVEX-HARVEST only: end-of-window total return, drawdown free below 50%, behind a breadth floor (≥30 tickets/yr **and** ≥20 underlyings). `O_CONVEX`-only and refused for anything else. |

**The four option metrics are mutually non-comparable, and not comparable with
`consistent_annual_return` either.** They are different objectives, not rescalings of one:
`option_consistent_annual_return` ranks a 25%-CAR/10%-DD genome ABOVE a 50%-CAR/30%-DD one,
`option_car_over_risk` ranks them the other way round (that inversion is the whole point of it —
the first gated stage-1 job converged on 10.6%-CAR/8.9%-DD grinders under the former and was
stopped), and `option_car_target` ranks on **distance to two targets** rather than on a risk
preference at all — it is the only one that prices the CAR/DD ratio, which `option_car_over_risk`
cannot express (dividing by `sqrt(dd)` scores a 40%-CAR/40%-DD genome and a 20%-CAR/10%-DD one
identically at 6.32). Never put two of them in one table, and give a run that changes metric a
fresh `--name-suffix` — job names are the resume key.

### `consistent_annual_return` ("car" / "goal")

The metric behind the `-goal*` grid runs. Targets **~30%/yr EVERY year**, not one 50% year
propped up by a 10% year:

```
fitness = base × dd_guard × consistency × trade_gate
```
- **base** — (adjusted) annualized return %/yr.
- **trade_gate** — proportional ramp to `avg_trades_per_year / 30` (capped at 1.0); a
  15-trades/yr config scores 0.5x, not a hard cliff. Below-floor configs still get a gradient
  the GA can climb.
- **dd_guard** — 1.0 while `|max_drawdown| <= 20%`; a soft `20/|dd|` penalty beyond that.
- **consistency** — `clamp(worst_year / mean_year, 0.25, 1.0)` over calendar-year returns; an
  even (30/30/30) profile scores 1.0, an uneven (50/10/50) profile scores ~0.27.
- A **negative base is returned unfactored** — multiplying a loss by any of the above factors
  (all `<= 1.0`) would improve it, flipping the penalty's sign.

`--fitness-trade-scale` is a **structural no-op** for this metric (its own `trade_gate` already
covers trade frequency, and the trade-scale's linear ramp-to-100/yr would triple-penalize the
30-40/yr target zone). `--fitness-win-rate-factor` is **not** a no-op here — win rate isn't part
of the CAR formula, so it still applies.

---

## 3. Optional fitness knobs

All are `store_true` flags (default OFF) plus companion value args where noted -- **except
`--robust-fitness`, which is ON by default since 2026-09-17** (see below). They ride in
`optimization_config.backtest` and are threaded per-trial by `strategy_optimization_handler.py`.

### `--robust-fitness` / `--no-robust-fitness` -- **DEFAULT: ON** (since 2026-09-17)
Multiplies the metric by three factors, so a genome must clear all of them: a **concentration**
factor (how much of net P&L came from the top 1/5 trades), a **Monte-Carlo** factor (1000-path
bootstrap of the trade sequence; penalises a genome whose 5th-percentile path loses money) and a
**spread** factor (fraction of profit surviving a wider spread). A big winner that will not repeat
therefore stops being rewarded.

It was opt-in for a year and no grid driver ever passed it, so the option stage-1 discovery run
ranked its whole search on the raw metric -- an elite at 43-58%/yr on 61-94% drawdown with nothing
asking whether that was an edge or two trades carrying the book. Hence the flip.

- **Both numbers are always stored**: `fitness_raw`, `fitness_robust` (explicitly `None` when off)
  and every `robustness` component, per trial. The score is always decomposable.
- **Scores are NOT comparable across the setting.** Never rank a robust run against a raw one.
- **Resuming across the setting is REFUSED.** A GA checkpoint records the setting it was scored
  under; resuming it under the other one raises, naming both values and the job. A checkpoint
  written before 2026-09-17 has no such key and is read as **off** -- which is what those runs
  actually did -- so an old checkpoint refuses loudly instead of silently changing objective.
  Two ways out: `--no-robust-fitness` to match the checkpoint, or a new job name to start fresh.
- Cost: ~3 ms per trial at 300 trades, ~10 ms at 1000 (measured 2026-09-17) -- immaterial against
  a 100-200 s trial.
- `--no-robust-fitness` is the opt-out (the pre-2026-09-17 behaviour). `tools/stage1_run.sh`
  exposes it as `STAGE1_ROBUST=0` and refuses it without its own `STAGE1_SUFFIX`, for the same
  reason the `STAGE1_FITNESS` guard exists: job names are the resume key.

### `--fitness-trade-scale` [`--fitness-trade-scale-cap N`, default 100]
Multiplies a **positive** fitness by `min(avg_trades_per_year, cap) / 100`, down-weighting
statistically thin (few-trade) configs so a 16-trade lottery winner can't top the search on raw
calmar/sharpe. `~100 trades/yr` is break-even (factor 1.0); raising the cap allows some
up-weighting above that rate. **No-op for `consistent_annual_return`** (see above). Never
applied to a negative fitness (would wrongly reward a thin loser by nudging it toward zero).

### `--fitness-win-rate-factor`
Multiplies a **positive** fitness by `2 × win_rate_fraction`: 0% win → 0x, 50% win → 1.0x
(break-even, no change), 100% win → 2x. Rewards configs that win more often, penalizes ones that
win less than half the time. Applies to every metric **including CAR** (win rate isn't part of
its formula). Like trade-scale, never applied to a negative fitness.

Added after discovering the archived S4 "177% return / 32% win rate" result rode almost
entirely on a handful of big winners hitting a wide +48% TP cap while most losers round-tripped
to a flat -4% floor — this knob lets a future grid explicitly reward a more consistent,
higher-win-rate exit style instead of an all-or-nothing one, without hand-tuning the exit rules.

### `--profit-cap-pct N` [default 2000] / `--profit-share-cap-pct N` [default 25]
Cap a single trade's contribution to the **adjusted** return-based metrics (total_return,
profit_factor, calmar_ratio, sqn — CAR too, via `adjusted_annualized_return`), so one lucky
non-reproducible mega-winner (a sub-$1 stock that 90x'd) or one trade dominating the whole book
can't win the search on a fluke. `profit_cap_pct` bounds gain as % of a trade's own cost basis;
`profit_share_cap_pct` bounds it as % of the run's total net profit (a trade can pass the first
cap yet still be 60% of the book). Pass `0` to disable either.

---

## 4. Labels — filtering grid output

Every top-N `Backtest` row a job persists carries a `labels` list (JSON array of free-form
strings, e.g. `["goal6", "S4"]`) — the grid driver auto-sets **one label for the grid/batch id**
(from `--name-suffix`, stripped of its leading `-`) **and one for the strategy** (or the expert
name, for the bypass `FactorRanker` job which has no strategy variant). This is independent of
`optimization_id`/cap-band, so it survives across bands: every S4 result from the `-goal6` run
carries `"S4"` regardless of whether it ran on `large`, `mid`, or `small`.

Filter via the list API:
```
GET /api/backtests?label=goal6      # every backtest from that grid run, any strategy/band
GET /api/backtests?label=S4         # every S4 result, from any grid run that used --labels
GET /api/backtests?label=S4&expert=FMPRating   # combine with the existing filters
```
`label` matches if the tag is **anywhere** in a backtest's `labels` array (SQLite `json_each`
containment check), not an exact match on the whole list — so `label=S4` returns rows tagged
`["goal6", "S4"]` as well as `["goal4", "S4"]`.

Manual `ba2-test optimize` runs can set labels directly with `--labels goal6,S4` (comma-separated,
free-form — there's no fixed vocabulary). Runs predating this feature (or that don't pass
`--labels`) have `labels: []`.

---

## 5. Where things live

- Grid driver: `tools/run_screener_capband_matrix.py`
- CLI arg parsing + per-trial config assembly: `testplatform/ba2test_launcher.py`
  (`optimize` subcommand, `_persist_top_backtests`)
- Fitness catalog + `compute_fitness`: `testplatform/backend/app/services/strategy_fitness.py`
- Per-trial config threading (`_build_daily_trial_config`):
  `testplatform/backend/app/services/strategy_optimization_handler.py`
- Results dict assembly (where `avg_trades_per_year`/`win_rate`/knob flags land before
  `compute_fitness` reads them): `testplatform/backend/app/services/backtest/results.py`
- `Backtest` model (`labels` column etc.): `testplatform/backend/app/models/backtest.py`
- List/filter API: `testplatform/backend/app/api/backtests.py`
- UI metadata endpoint (catalog + knobs, single source of truth for the frontend):
  `GET /api/optimization/fitness-options` in `testplatform/backend/app/api/strategies.py`

---

## 6. Options entry conditions: the signal-strength gate

Every pure-option member's entry rule carries one gate on how strong the expert's call is,
`{m}-exp_profit` (`expected_profit_target_percent > X`, searched over 2–20 % with its own
`enabled` gene). `ExpertRecommendation.expected_profit_percent` is **non-nullable**, so every
expert produces it; `target_price` is nullable and derives from it when absent, so the two are
the same signal.

> **Removed 2026-08-27: the four `price_vs_target_*` gates.** Members used to also gate on where
> price sat inside FMPRating's analyst target range, via `{m}-price_low_above` /
> `{m}-price_low_below` / `{m}-price_high_above` / `{m}-price_high_below`. Those are gone.
> `PriceVsTargetLowCondition` / `PriceVsTargetHighCondition` read
> `expert_recommendation.data["FMPRating"]["target_low"|"target_high"]`, and **only FMPRating
> writes that key** — so under any other expert all four failed CLOSED, and any genome that
> switched one on traded nothing while spending 8 of ~28 genes per structure. The single
> expert-independent `exp_profit` gate replaces them. The condition classes still exist in
> `packages/common/ba2_common/core/TradeConditions.py` and can be wired into a rule by hand;
> they are just no longer part of any grid's genome.

The other entry gates on the same rule are `{m}-signal` (the expert's bullish/bearish flag),
`{m}-flat` (`has_no_position` — a correctness guard, and the one leaf with no `enabled` gene),
`{m}-iv_rank`, `{m}-iv_rv`, plus the two **shared** gates `shared-gate_confidence` and
`shared-rel_volume`. `iv_rank` and `iv_rv` are built **per member** with the operator flipped
between the debit and credit halves, because the GA's gene-space collector only ever optimizes
a condition's `value` (via `value_min`/`value_max`) and its `enabled` flag (via
`toggle_optimize`), never its `op` — one shared leaf could express only one of the two theses.

`gate_confidence` and `rel_volume` carry a `shared-` id instead of a member prefix, so a whole
group searches **one** threshold for each rather than one per structure (OS1: 20 genes collapse
to 4). They are shared because their semantics do not vary by structure — expert conviction in
the symbol, and real participation behind the signal, read the same whichever way the premium
flows. The shared id is used in a **single-structure** job too, so a stage-1 winner's gene keys
are still known in a stage-2 group space (`encode_params` drops unknown keys silently).
Sharing is safe on both paths: `decode_params` builds one `cond_by_id` map for the whole
strategy and substitutes by node id, so the single gene lands on every member's leaf, and
`triggers_from_condition_tree` keys the seeded EventAction triggers by position within one rule
(`cond_0`, `cond_1`, …), never by condition id, so duplicate ids across sibling rules cannot
collide.

Gate wiring: `_option_entry_rule()` in `testplatform/ba2test_launcher.py`.
