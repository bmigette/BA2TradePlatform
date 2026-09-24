# Market-condition entry gates in the exploration grid

Part of the [strategy exploration grid](README.md). Everything in the README's capital,
execution and launch contract applies unchanged.

## Status: driver ready, reviewed 2026-09-17

Review: [market-condition implementation and option stage 1](../../../reports/strategy_research/market_conditions_review_2026-09-16.md).
Shared design: [condition genes](../../plans/2026-09-15-option-market-condition-genes-design.md),
especially the equity placement contract in section 6.0.

The ten experiments in the README remain the original **35 jobs / 193 combinations at $10,000**.
The following is an additional, opt-in research campaign; it does not change those controls,
their saved IDs, or their backtest results. The `regime_overlay_enabled=False` setting above
remains pinned: these shared entry conditions are separate from that older overlay.

**Current implementation boundary:** the follow-up driver now exposes both profiles for its
equity and ETF entry rules. It remains opt-in and requires a pinned manifest for every selected
profile. Option stage 1 is unchanged. Stage 2 is outside the current review.

## Conditions to reuse

| Profile | Fields searched by the implemented option profile | Extra genes per entry tree |
|---|---|---:|
| `ohlcv-v1` | EMA50 slope normalized by ATR14; ADX14; 5/20-session realized-volatility ratio | 6 |
| `ta-structure-v1` | Distance to confirmed support; distance to confirmed resistance; channel position; close relative to prior 20-session high; bull/bear swing structure | 9 |
| Both | The eight optional gates above | 15 |

Numeric modes are off/below/above with a threshold; swing structure is off/bull/bear.
Reuse registry definitions, ranges, calculator versions and `prior_session_v1` timing.
These are conditions on the **traded stock or ETF**, not an additional benchmark regime.
The other seven calculated structure fields remain diagnostics for the first campaign.

## Questions for all ten families

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

## Driver implementation and launch contract

The driver implementation now does the following:

1. It factors the registry-derived market-leaf builder into the shared package and appends gates
   to **each opening rule/tier**, preserving existing OR groups and first-match behavior. Exits,
   reductions, stop updates and protective actions are never gated.
2. `run_exploration.py` accepts `--market-condition-profile none|ohlcv-v1|ta-structure-v1|ohlcv-v1,ta-structure-v1`,
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
6. `--market-exit exit,stop,tp` (a comma list, any order) appends the shared market
   exit/stop/TP templates **after** each job's existing exit rules. Every template rule is **off by
   default** behind a searched toggle gene: an all-off genome decodes to the job's original exit
   rules exactly, and only its thresholds and percents are searched (the leaves never switch off).
   The flag requires a profile, `--search genetic` and `--market-condition-mode search`.
   - `exit`: a structure close (ta-structure-v1) and a slope close (ohlcv-v1), two independent
     rules. With one profile only that profile's variant is emitted; the job's
     `market_exit.rules` lists the rules actually added.
   - `stop` needs ta-structure-v1 and `tp` needs ohlcv-v1. A kind that none of the selected
     profiles serves is refused.
   - **Single direction only.** The templates run on every open position of the expert, so the
     driver derives one direction per job: `pullback_rsi` from its `direction` setting (checked
     against its entry action), every other family must open only with `buy` (long). A job that
     both buys and sells, or whose direction cannot be determined, is refused.
   - A job whose exit list has a rule that matches every held position and stops processing (the
     `has_position` floor stops of mid_insider, small_earnings, small_rating and mid_earnings)
     is refused: templates after it could never run.
   - Condition ids must be unique across all entry and exit rules (ids share genes).
7. `--allow-sl-loosen` sets the expert setting `allow_ruleset_sl_loosen=True` on every job's
   experts: a ruleset stop may loosen down to the trade's recorded max-loss stop, never past it.
   It is independent of `--market-exit` and of the profiles; off, stops only tighten.

Both flags enter a job's fingerprint, name (`-mx_<kinds>`, `-slloosen`) and labels only when set,
so the default manifests are byte-identical. The launch preview prints both.

The live resolver now selects the subset of a host manifest needed by each expert, so different
profile sets can coexist on one host. Keep the mixed-profile resolver checks in the deployment
gate. The driver can prepare and replay a single profile or a combined pin.

## Warmup and central-cache preparation

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

## Comparison and selection

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
