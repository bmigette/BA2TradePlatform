# Option market-condition genes

Date: 2026-09-15  
Status: implementation-ready design for review; not implemented or deployed  
User direction: retain GA discovery and design additional conditions it can search.

Amendment: warmup and a central, reusable feature cache are required parts of
the first delivery, including preparation of distributed workers before trials.
Follow-up scope: the same opt-in conditions also cover Options Grid 2 and the
separate convex-harvest grid, with per-arm genes and preserved trade lifecycles.

Amendment 2 (2026-09-15, chart structure): section 3.2 adds a second feature
profile, `ta-structure-v1` — support/resistance distance, regression channel,
prior-range breakout and swing structure (BOS/CHoCH) — computed in the same
store, window and timing contract. All conditions in this document, old and
new, are available to **both option structures and the equity strategies
S1–S7**. A new equity grid using them follows; existing equity results and
the running goal2020 outputs are not re-run or re-labelled.

## 1. Intended result

Each option structure can learn whether to require a rising/falling underlying,
a strong/weak trend, and expanding/contracting realized volatility. These are
explicit strategy conditions, independent of whether FMPRating or
DeterministicScorer supplied the recommendation.

Version 1 adds **three numeric conditions and six genes per independent entry
arm**: a mode and threshold for each condition. A single-entry structure gains
six genes; the two-arm O_LEAP and O_CONVEX jobs each gain twelve. Lookbacks and
calculation conventions are fixed. Conditions combine with the existing entry
gates using AND. A condition can be disabled; no market pattern is declared
profitable in advance.

This is a new, opt-in search profile. Existing GA jobs, stored backtests,
deployed settings, fitness, sizing, TP/SL and option valuation keep their current
definitions. No leverage simulation is added to the backtest.

## 2. What the current code establishes

| Existing component | Consequence for this design |
|---|---|
| `_option_entry_rule` in `testplatform/ba2test_launcher.py` | Already searches IV rank, IV/RV, relative volume and expert-derived gates. New conditions supplement these. |
| DeterministicScorer technical/macro calculations | ADX and trend calculations exist, but are not independent condition fields. Its `macro_mode` is already searched. Do not redefine its existing outputs while adding these conditions. |
| `strategy_param_space._walk_condition_nodes` / `_apply_to_tree` | Currently collect/decode thresholds and enabled flags, not comparison-mode genes. Supporting off/below/above requires a small explicit extension. |
| `ConditionLeaf.to_canonical_dict` | Rebuilds canonical output from declared fields. New optimization metadata must be declared and serialized; `extra="allow"` alone will not preserve it. |
| `TradeConditions._as_of_fetch_end` | Uses the simulated day's end in BT and latest data in live. It is not the timing contract for these new intraday-entry conditions. Leave legacy behavior unchanged. |
| `BacktestAccount._as_of_date` | Discards time. Use a full decision timestamp/session context for the new conditions. |
| `DailyBacktestEngine._stage_recommendation_candidate` | HOLD/SKIP/ERROR do not reach entry rules. Disabling a bullish rule leaf does not create opportunities from these recommendations. |
| O_CC / O_PP builders | Use S2 equity entry and separate overlays. New entry gates must be attached deliberately, not assumed to arrive through the pure-option builder. |
| Shared market calendar and replay clock | Existing session-calendar and capture mechanisms can support a precise new data contract without process-wide clock changes. |
| `shared_arrays.DerivedArrayStore` | Already provides per-host read-only mapped arrays, atomic publication and cross-process builder coordination. Reuse it for the new feature reader. Its path/size/mtime signature is a local mapping identity, not the portable source-content identity. |
| `cache_sync.py` | Already mirrors ordinary cache buckets and deliberately excludes `_derived`. Central feature objects can be mirrored; host mappings remain local. Its default path/size comparison alone does not certify matching content. |

Source references are collected in section 12. These observations came from
source inspection and the preceding generated-gene check, not a new backtest.

## 3. Three version-1 measurements

The input is one validated window of **128 consecutive regular-session daily
OHLCV bars**, ending at the session specified in section 4. All fields use the
same window, provider, price-adjustment basis and calculator version.

| UI name | Canonical field | Measurement | Initial threshold search |
|---|---|---|---|
| Underlying trend slope | `underlying_trend_slope_50_atr14` | Change in EMA50 over five sessions, divided by five times ATR14 | -0.30 to +0.30 ATR/session, step 0.05 |
| Underlying trend strength | `underlying_adx_14` | ADX14 | 10 to 40, step 5 |
| Realized volatility expansion | `underlying_realized_vol_ratio_5_20` | Five-session realized volatility / twenty-session realized volatility | 0.50 to 2.00, step 0.25 |

Ranges are authored starting search bounds, **not estimated optimal values**.
Report winners at a boundary so a subsequent experiment can test a wider range.
Do not silently widen a running job's parameter space.

### 3.1 Exact numerical contract

Index the window from 0 through 127. Use finite float64 prices; comparisons use
unrounded calculated values. Presentation rounding is separate.

**Trend slope**

1. Seed EMA50 at index 49 with the arithmetic mean of closes 0..49.
2. For j > 49: `EMA[j] = (2/51)*C[j] + (49/51)*EMA[j-1]`.
3. True range for j >= 1 is the maximum of `H[j]-L[j]`,
   `abs(H[j]-C[j-1])`, and `abs(L[j]-C[j-1])`.
4. Seed ATR14 at index 14 with the arithmetic mean of TR[1..14]. Thereafter
   `ATR[j] = (13*ATR[j-1] + TR[j])/14`.
5. The result is `(EMA[127] - EMA[122]) / (5*ATR[127])`.

Positive means the smoothed price rose, negative means it fell. Zero is a real
flat slope when the denominator is valid. ATR <= 0 makes this field unknown.
Normalization makes the scale comparable across stock prices and account sizes.

**ADX14**

Use the same true range and ATR series. For each j >= 1:

- `up = H[j]-H[j-1]`, `down = L[j-1]-L[j]`.
- `+DM = up` only if up > down and up > 0; otherwise 0.
- `-DM = down` only if down > up and down > 0; otherwise 0.
- Smooth each DM using the same 14-observation arithmetic seed and Wilder
  recurrence as ATR. At indices >= 14, derive `+DI` and `-DI` by dividing the
  smoothed DM by ATR and multiplying by 100.
- `DX = 100*abs(+DI - -DI)/(+DI + -DI)`. If ATR > 0 and both DIs are genuinely
  zero, define DX = 0. Missing inputs or ATR <= 0 are unknown, not zero.
- Seed ADX at index 27 with the mean of DX[14..27]; apply the Wilder recurrence
  through index 127. Return ADX[127].

This is a specified calculation for the new condition. Existing DS helpers use
their own initialization; do not replace them as part of this work. ADX measures
strength, not direction, and low ADX is not a promise of a future trading range.

**Realized-volatility ratio**

Compute `r[j] = ln(C[j]/C[j-1])`. Divide the sample standard deviation
(`ddof=1`) of the last five returns by that of the last twenty returns. Using
the same annualization factor would cancel, so do not annualize either side.
The twenty-session window includes the five-session window. It is a measure
of recent expansion relative to the broader window, not an independent forecast.

A zero numerator with a positive denominator is a valid 0. A zero denominator,
nonpositive close or nonfinite input is unknown. Never substitute 1 or infinity.

**Stable initialization:** compute each session's output from exactly its last
128 eligible bars, even if a worker has ten years cached. This avoids EMA/ADX
differences caused by live and BT supplying different amounts of prehistory.
The calculation version records this finite-window convention. A faster batch
implementation must reproduce the reference results and condition decisions.

### 3.2 Chart-structure measurements — second profile `ta-structure-v1`

**Rule that governs everything in this section: every field is precomputed in
warmup, once per (symbol, session), and stored as a float in the feature store
of section 4.3. A trial never detects a pattern. A condition never touches a
DataFrame or a bar series. At evaluation time the work is one array lookup by
(symbol index, session index) followed by one numeric comparison.**

This is not an optimisation choice; it is what makes chart structure feasible
in the GA at all. Today's bar-derived conditions
(`PercentBelowRecentHighCondition` and its siblings) fetch a DataFrame and
slice it inside `evaluate()`, once per symbol, per bar, per individual, per
generation. Pivot detection, regression fitting and swing sequencing are each
O(window) per session; doing them where those conditions do their work would
be one to two orders of magnitude slower than the slowest condition we have.
Precomputed, the whole profile costs one vectorised pass per symbol at warmup
(pivots by shifted comparisons, regression by cumulative sums) and nothing per
trial. None of these fields is recursive, so — unlike EMA/ADX — a batch
implementation over full history and the 128-bar reference implementation
agree exactly, and window invariance (section 8, item 2) holds by construction.

Chart patterns are drawings; a searchable condition needs a scalar. Each
pattern below is therefore reduced to its **components**, and no composite
detector ("flag", "channel breakout") is shipped. A flag, for instance, is
what the GA can compose from an existing positive trend slope, a negative
20-session channel slope and a close beyond the prior 20-session high. Keeping
the parts separate keeps every number meaningful in a report and leaves the
pattern definition to the search rather than to us.

Same input as section 3: one validated window of 128 regular-session daily
OHLCV bars, the same provider, adjustment basis and `prior_session_v1` cutoff
(section 4), the same ATR14 series as section 3.1. Fixed conventions of this
profile: pivot span **K = 3**, channel lookback **20 sessions**, level
tolerance **0.25 ATR14**. They are part of the calculator version, not genes.

| UI name | Canonical field | Measurement | Searched in v1 | Initial threshold search |
|---|---|---|---|---|
| Distance to support | `structure_dist_support_atr` | (close − nearest confirmed pivot low below close) / ATR14 | yes | 0.0 to 5.0, step 0.5 |
| Distance to resistance | `structure_dist_resistance_atr` | (nearest confirmed pivot high above close − close) / ATR14 | yes | 0.0 to 5.0, step 0.5 |
| Support strength | `structure_support_touches` | confirmed pivot lows within ±0.25 ATR of that support level | no | 1 to 5, step 1 |
| Resistance strength | `structure_resistance_touches` | confirmed pivot highs within ±0.25 ATR of that resistance level | no | 1 to 5, step 1 |
| Channel slope | `channel_slope_20_atr` | OLS slope of close over the last 20 sessions / ATR14, per session | no | −0.30 to +0.30, step 0.05 |
| Channel width | `channel_width_20_atr` | 4 × residual standard deviation / ATR14 | no | 1.0 to 8.0, step 0.5 |
| Position in channel | `channel_pos_20` | where the close sits between the −2σ and +2σ regression bands, 0 = lower, 1 = upper, unclamped | yes | 0.0 to 1.0, step 0.1 |
| Close vs prior 20-session high | `close_vs_prior_high_20_atr` | (close − highest high of the 20 sessions before it) / ATR14, signed | yes | −3.0 to +2.0, step 0.25 |
| Close vs prior 20-session low | `close_vs_prior_low_20_atr` | (close − lowest low of the 20 sessions before it) / ATR14, signed | no | −2.0 to +3.0, step 0.25 |
| Swing structure | `structure_state` | categorical: `bull` (higher high and higher low), `bear` (lower high and lower low), `none` | yes | mode gene only: `off`, `bull`, `bear` |
| Sessions since break of structure | `structure_bars_since_bos` | sessions since the close last broke the previous swing in the direction of structure | no | 0 to 60, step 5 |
| Sessions since change of character | `structure_bars_since_choch` | sessions since the close last broke the previous swing against structure | no | 0 to 60, step 5 |

**Searched in v1** marks the five leaves the first `ta-structure-v1` launcher
profile appends to an entry arm: four numeric and one categorical, **nine
genes per arm** on top of the six from section 3. Every field in the table is
computed and stored regardless — storage is a float32 per field per row and
the marginal warmup cost of the unsearched fields is nil — so a later profile
can search the rest without re-warming. The subset is a search-space decision
(section 5 warns against widening a space silently), not a data decision, and
it can be revised before the first launch.

As in section 3, the ranges are starting bounds, not estimates of good values.
Report winners at a boundary.

### 3.3 Exact numerical contract for chart-structure fields

Index the window 0..127; C, H, L are close, high, low; ATR is the section 3.1
ATR14 series and every division below is by `ATR[127]`. ATR <= 0 makes every
field of this profile unknown. Comparisons use unrounded values.

**Confirmed pivots (the lookahead guard for the whole profile)**

A pivot high at index p (K <= p <= 127 − K) requires `H[p] > H[p−i]` and
`H[p] > H[p+i]` for every i in 1..K, strictly. A pivot low is the mirror on L
with `<`. Ties are not pivots. A pivot at p is **confirmed at index p + K**
and does not exist before that: for the row ending at session 127, the usable
pivots are exactly those with p <= 127 − K. This is how a trader sees a level
appear — three sessions after the extreme — and it is what makes these fields
free of lookahead. A full-history batch implementation must therefore compute
the row for session t from pivots with p + K <= t, never from pivots the
future confirms.

**Support and resistance**

- Resistance level R = the smallest confirmed pivot-high price strictly above
  `C[127]`; support level S = the largest confirmed pivot-low price strictly
  below `C[127]`. Ties between equal pivot prices are one level.
- `structure_dist_resistance_atr = (R − C[127]) / ATR[127]`;
  `structure_dist_support_atr = (C[127] − S) / ATR[127]`. Both are >= 0 by
  construction.
- No qualifying pivot in the window makes the corresponding field **unknown**.
  Never 0 (which would say "sitting on the level"), never a sentinel, never
  the window's extreme price: a window with no pivot above the close does not
  have a resistance at its highest bar.
- `structure_resistance_touches` = the number of confirmed pivot highs whose
  price lies within `R ± 0.25 × ATR[127]`, including the one that defined R;
  support mirrors it. Minimum 1 when the level exists; unknown when it does not.

**Regression channel over 20 sessions**

Over indices 108..127 let x = 0..19 and y = C. Fit ordinary least squares
`y = a + b·x`. Residuals `e[x] = y[x] − (a + b·x)`; σ is their sample standard
deviation with `ddof = 2` (two fitted parameters).

- `channel_slope_20_atr = b / ATR[127]` — per session, on the same scale as
  the section 3 trend slope.
- `channel_width_20_atr = 4σ / ATR[127]`.
- `channel_pos_20 = (C[127] − (a + 19b − 2σ)) / (4σ)`. 0 is the lower band,
  1 the upper band; values outside 0..1 are real and mean the close is outside
  the channel, which is information, so the value is **not clamped**.
- σ = 0 (twenty identical closes) makes the width and position unknown; the
  slope is still valid (0).

**Prior-range breakout**

- `close_vs_prior_high_20_atr = (C[127] − max(H[107..126])) / ATR[127]`.
- `close_vs_prior_low_20_atr = (C[127] − min(L[107..126])) / ATR[127]`.

Both are signed and always defined when ATR is valid. The range deliberately
excludes session 127 itself, so a close above the prior high is a genuine
breakout of the range that existed before it.

**Swing structure**

1. Take the confirmed pivots of the window in chronological order and reduce
   them to alternating swings: between two consecutive pivot lows keep only
   the highest pivot high, between two consecutive pivot highs keep only the
   lowest pivot low. The result alternates high, low, high, low.
2. Let SH1 < SH2 be the last two swing highs by time and SL1 < SL2 the last two
   swing lows. Fewer than two of either makes `structure_state = none` and both
   `bars_since` fields unknown.
3. `structure_state` is `bull` when `SH2 > SH1` and `SL2 > SL1`; `bear` when
   `SH2 < SH1` and `SL2 < SL1`; otherwise `none`. Equality is neither.
4. Walking sessions from index 127 backwards while structure is `bull`: a
   break of structure is a session whose close exceeds the swing high that was
   the most recent confirmed one at that session; a change of character is a
   session whose close falls below the most recent confirmed swing low. `bear`
   mirrors both. `structure_bars_since_bos` / `_choch` are `127 − index` of the
   most recent such session, using only pivots confirmed by that session.
   None in the window is unknown, not 0 and not 128.

**Precomputation, batch form.** For each symbol, over its full cached history
in one pass: pivots from K-shifted comparisons; the pivot-confirmation index
from `p + K`; nearest-level queries against the sorted confirmed levels;
regression via cumulative sums of x, y, x², xy over the rolling 20; prior
range via rolling max/min shifted by one. Then keep each session's row only
if its 128-session input window is valid under section 4. The expected cold
cost is on the order of ten milliseconds per symbol; a thousand-symbol
universe warms in seconds and the per-trial cost is a lookup. Report the
measured figures with the section 4.6 counters before the first launch.

**Measured (2026-09-16, `reports/strategy_research/market_conditions_bench_2026-09-16.md`
section 6b): 75 us per ROW -- 276 ms for AAPL's full 3,675-session history, 109 ms for a
6-year span -- not ~10 ms per symbol.** The estimate assumed the whole profile is
non-recursive; the twelve measurements are, but ATR14, which every one of them divides
by, is a Wilder recursion seeded inside each window (section 3.1), so it is re-run per
row and dominates the cost. The per-trial cost is still a lookup (0.37 us).

## 4. Daily information and replay contract

Version 1 uses **`prior_session_v1`**: a decision uses the immediately preceding
regular trading session's daily bar, never its own session's daily bar.

- A Monday 09:30 New York decision uses Friday's bar, adjusted for exchange
  holidays. A Monday 15:45 decision uses the same window.
- The policy remains stable for that local date, including after the close.
  Consuming the just-completed current session would be a different future
  policy; it is not silently enabled when the provider happens to update early.
- A daily BT row represents its declared trading-session label, not a literal
  UTC-midnight trade. Its feature cutoff is the previous session for that label.
  An intraday BT uses its timezone-aware decision timestamp. Both resolve to
  the same cutoff for the same trading session.
- Use the shared US regular-session calendar, including holidays and half days.
  Non-US calendars are outside this first version. Naive live timestamps or
  ambiguous bar dates are rejected, not assigned a guessed timezone.
- If the expected prior session is missing, report a missing observation. Do
  not silently use an older session or splice in today's partial bar.

The source contract requires split-adjusted, consistently scaled OHLC prices
without mixing dividend-adjusted closes and unadjusted highs/lows. Certify the
selected provider's existing cache columns against split fixtures before using
them. If the source cannot satisfy that contract, fail preflight rather than
guess an adjustment or silently select another provider.

All 128 required sessions must be present and unique, with consistent OHLC
ordering and finite positive prices. Missing sessions, insufficient listing
history and unresolved corporate-action adjustments are explicit coverage
failures. Duplicate conflicting bars are errors. Do not forward-fill missing
prices merely to make an indicator computable. Prewarm must cover **sessions**,
not assume 128 calendar days are sufficient.

### 4.1 Shared evaluation context

Introduce an immutable, evaluation-scoped `MarketConditionContext`, available
only to rules that require the new fields. It carries:

- full decision time and effective trading-session label;
- selected provider/source profile, adjustment policy and calendar;
- read-only daily-bar access and source-content identity;
- timing policy, calculator version and optional capture/replay recorder.

The BT adapter gets time from the engine and bars from its pinned offline input
store. The live adapter reads `replay_now()` once on the coordinating thread,
then passes that value into any worker fan-out. Different analyses cannot share
a mutable global clock. If a new-field condition lacks this context, it is
unknown with an explicit configuration diagnostic; it must not call the legacy
end-of-day helper as a fallback.

Pass the context through the evaluator/condition factory without changing old
condition behavior. Resolve it lazily only when a surviving new condition is
evaluated. All new gates off must require no additional clock or market-data
reads in the decision path.

### 4.2 Cache and evidence

Read one window per symbol/session and derive all three measurements together.
Memoize by source profile, symbol, final session, bar-content digest, adjustment
policy and calculator version. Keep mode/threshold out of the feature-cache key:
many genomes reuse the same measurements. Bound worker memory; disk-derived
results belong under the configured shared cache, not the repository.

Resolve source revisions at snapshot acquisition, not by hashing a whole cache
tree on each condition. Persisted research runs pin their input manifest. A live
correction creates a new content identity; it must not overwrite the original
captured observation. No negative cache entry may conceal a subsequently
completed warmup indefinitely.

Warm missing OHLCV through the existing provider cache mechanisms before the
scheduled entry analysis. The condition calculation itself performs no external
request and holds no account submission lock while waiting for data. Warmup is
targeted to missing/stale requirements; BT/replay never downloads on a miss.

Capture the exact normalized window or a durable content-addressed reference,
its availability/cutoff, the three values and per-field status. A hash without
retained bytes is not replayable. Recording an already-read window adds no API
request. Historical reconstruction with later corrected data remains a separate
comparison from replaying what live actually consumed.

### 4.3 Central cache and worker-local acceleration — required in v1

**Compute the indicators once for each distinct input window, then reuse them
across strategies, experts, GA generations, reruns and live analyses.** A Python
dictionary in each worker is not sufficient central-cache support.

| Layer | Location and content | Reuse |
|---|---|---|
| Provider data | Existing configured shared OHLCV cache plus retained immutable normalized input objects where needed for replay | Existing provider warmers remain the only fetch path. Do not create one raw-data cache per GA job. |
| Central feature store | `<CACHE_FOLDER>/market_conditions/ohlcv-v1/`: immutable compact parquet objects and content-addressed manifests | One canonical calculation result for each source window. Shared by live/test processes on a host and mirrored to remote hosts. |
| Local array store | `<CACHE_FOLDER>/_derived/market_conditions/...` through `DerivedArrayStore` | Read-only numeric arrays shared through the OS page cache across workers on that host. Never transferred to workers as pickled arrays. |
| Evaluation handle | Small bounded worker-local index/handle cache keyed by manifest and shard | Direct symbol/session lookup followed by numeric comparisons. No per-trial DataFrame construction, indicator calculation or cache-tree scan. |

`CACHE_FOLDER` here means the explicitly resolved common cache root, normally
under `BA2_HOME/common/cache`. A live instance with a separate DB/cache path must
be wired to the same configured feature store or to a verified mirror; merely
using the same class does not establish shared storage. Record the resolved
store identity at startup. Different source profiles/adjustment policies never
reuse each other's values by accident.

Central feature objects are **ordinary syncable cache data**, not `_derived`
artifacts: otherwise every remote host would repeat the expensive calculations.
Each host only converts the received small feature table into its local mapped
array representation. A central service or network-mounted disk is not required
for hot-path reads; workers run offline against a verified local snapshot.

#### Portable data identity and layout

Use immutable object paths such as
`market_conditions/ohlcv-v1/objects/<sha256>.parquet` and
`market_conditions/ohlcv-v1/manifests/<sha256>.json`. Batch rows by symbol/month
or similarly bounded shards, rather than creating a file per day or genome.

Every feature row identifies:

- symbol and final input session;
- the three unrounded float64 measurements and a status for each;
- the normalized input-window digest and durable raw-window/source references.

Retain normalized raw evidence in deduplicated immutable shards and identify
each window by shard references and row/session bounds. Do not embed another
128-bar copy in every feature row or job artifact. A captured feature row and
its retained references must reconstruct the exact input window without live
provider access.

The manifest pins the source profile, calendar and adjustment versions, timing
policy, all calculation periods, calculator/schema versions, object hashes,
required session coverage, row counts and known coverage exceptions. It lists
exact objects; readers must not glob all old and new partitions together.
Conflicting duplicate symbol/session rows are an error.

Portable identities use normalized content and definition versions, not machine
paths, mtimes, account IDs, job names, population sizes, experts, thresholds,
option strikes/DTE or equity. A wider job window/universe creates a new manifest
that references reusable objects; it does not invalidate all overlapping data.

Reuse `DerivedArrayStore` for local mapping/publication. Its key must include
the canonical feature-object/manifest identity and local layout version. Keep
portable SHA-256 verification separate from its existing filesystem signature;
do not change shared-array semantics for other readers. Pack values/statuses
into a few arrays per shard, with compact session/symbol indexes and a bounded
open-handle cache. Retain its resource-exhaustion and Windows mapped-file guards.
`BA2_SHARED_ARRAYS=0` may load the same central feature rows into private arrays;
it must not recompute indicators per trial or change values.

### 4.4 Warmup workflow — one preparation for the whole search

Provide one orchestration implementation callable from the CLI, API/job queue
and live pre-analysis preparation. Put calculator/store code in `ba2_common`,
and provider-fetch orchestration in `ba2_providers`; a backend-only warmer would
not serve live. CLI/UI wrappers supply arguments and progress reporting only.
The existing expert-history and option-data prewarm steps remain required and
distinct: these three new indicators add only an OHLCV dependency.

The warmer accepts the explicit profile, universe, decision-date window, source
profile, shared cache root, output manifest and CPU/I/O concurrency budgets.
Its phases are:

1. **Plan/inventory:** resolve the union of symbols and feature dependencies
   across all requested structures/experts, regardless of an initial genome's
   gates being off. Derive the previous session for every decision session and
   the 128-session input window for every required feature row. The earliest
   required source bar is 127 sessions before the first row's end session.
   Reuse overlaps; list missing raw bars, missing derived rows and source conflicts.
2. **Fetch if requested:** use the existing provider cache path to retrieve only
   missing/stale raw coverage, with bounded concurrency, shared rate limiting
   and deduplicated in-flight requests. An offline/cache-only invocation stops
   with an actionable inventory instead of fetching. Never fall back to a full
   history download simply because the feature store is cold.
3. **Build incrementally:** read each affected raw shard once, compute the three
   fields together, and publish only missing or invalidated rows. Reuse existing
   input objects/window digests. Build all possible v1 feature fields for the
   union, not a separate table for each structure or expert.
4. **Publish/verify:** publish immutable objects first and the final manifest
   last. Report complete versus explicitly recorded short-history/invalid-data
   rows. Unexplained cache holes, truncated files and configuration failures
   produce a nonzero failure; do not print a generic success for partial work.
5. **Prepare hosts:** mirror the manifest's referenced feature objects, source
   evidence and other existing run dependencies; verify them locally, then
   prepare the mapped arrays with bounded build concurrency before GA fan-out.

Proposed interfaces (to be implemented, not commands available today):

```text
warm-market-conditions plan --profile ohlcv-v1 --universe-file ... --start ... --end ...
warm-market-conditions build --plan ... --cache-only
warm-market-conditions build --plan ... --fetch-missing
warm-market-conditions verify --manifest ...
warm-market-conditions prepare-host --manifest ...
```

Persist progress at shard boundaries. A stopped build resumes by trusting only
complete hash-verified objects; temporary work is not a completed partition.
If a source file changes while being read, retry that snapshot rather than
publishing a mixture. Concurrent warmup requests for the same missing input or
feature shard coalesce under one coordinator/claim. Limit total host builders
as well as builders per key, so many different cold symbols cannot exhaust RAM.

Record genuine unavailable observations with their source/calculation identity
and reason. A source revision or newly available bar invalidates the associated
unknown result; an unbounded negative TTL must not make a repaired cache appear
permanently empty. Unavailability must stay visible in preflight and job reports.

### 4.5 Launch and live-readiness integration

For the new profile, matrix launch performs the union warmup/preflight **once
before dispatching any of its GA jobs** and pins the resulting manifest digest
in every job/checkpoint/distributed payload. Standalone launches resolve and
reuse an equivalent prepared snapshot or invoke that same preparation service;
they cannot warm separately inside each trial. The profile's genetic search
must not begin by lazily calculating 128-bar indicators in every consumer.

Each worker acknowledges the same manifest digest and a successful local
verification/mapping preparation before it receives trials. Roots may differ
between Windows and Linux; requests carry logical identities and relative
paths, not the master's absolute cache path. A missing or incompatible worker
snapshot makes that worker unready. It must not fetch, use another snapshot or
report a feature-cache miss as a zero-trade strategy result.

The existing cache sync can transport the central bucket. Extend preparation
to verify the referenced object hashes once after arrival: size equality alone
does not establish integrity. Use snapshot-scoped sync and preserve objects
pinned by other active jobs/replays; generic stale-file pruning must not remove
another job's retained history. `_derived` remains excluded from sync. Transfer
manifests and referenced new objects only; do not repeatedly scan/hash the
entire OHLCV/option cache for a threshold-only rerun.

In live operation, register the deployed conditions' symbol dependencies with
pre-analysis preparation. After the prior session is available, build its new
rows once and atomically select a complete manifest for the next analysis. Late
universe additions are prepared before those symbols are eligible for new-field
rules. Scheduled analysis consumes a pinned manifest and never waits on an
account lock for a background build. Unready symbols are reported and fail
their active condition; existing exits/protection continue. Later source
corrections publish a new manifest for future evaluations while recorded
analyses retain their original one.

### 4.6 Invalidation, retention and performance acceptance

| Change | Required work |
|---|---|
| Threshold/mode, expert, option parameters, capital, population, seed or another job | Reuse feature rows; no raw download or indicator recalculation. |
| Additional symbols or dates | Fetch/build only missing coverage and publish a manifest reusing the overlap. |
| One new completed session | Build that session's rows; do not recalculate the prior history. |
| A historical OHLC bar changes | Recalculate rows whose 128-session window contains it: at most 128 ending sessions per changed bar. Use the union for multi-bar/split corrections. |
| Source/adjustment/calendar/calculator semantics change | New namespace/identity and matching rebuild; old pinned manifests remain readable. |
| Only local mapped arrays are absent | Rebuild mappings from central feature objects, with no raw-data download or indicator computation. |

Treat portable immutable feature/source objects as retained research inputs.
Garbage collection is explicit and reference-aware: protect active jobs,
stored backtest/deploy manifests and captured replays. Never overwrite mapped
files; use new signature directories and defer deletion while readers exist.
Existing array sweep logic handles local disposable mappings, not decisions
about deleting canonical evidence.

Acceptance requires measured counters, not an assumed speed-up:

- A second identical warmup performs **zero provider calls and zero indicator
  recalculations**; it opens/verifies the existing snapshot.
- Changing only genes or running the other expert reuses the same rows and
  introduces no feature build, hash-tree scan or raw parquet read per trial.
- With a prepared manifest, each condition does an indexed feature lookup and
  comparison. A worker opens/maps each needed shard once per handle-cache
  residency, not once per bar/trial; configuration/source verification occurs
  at preparation or snapshot change.
- A second host receives canonical features and builds only local mappings;
  it does not repeat indicator calculations. Worker recycling reopens mappings.
- One-day extensions and injected single-bar corrections show the bounded
  rebuild behavior above, and old pinned runs retain identical outcomes.
- Report cold build time, repeat warmup time, worker prepare time, cache
  hits/misses, provider bytes/calls, computed/reused rows, P50/P95 warm lookup
  time, worker memory and open descriptors. Benchmark the intended worker count;
  set a measured runtime target before broad launch rather than inventing one.

## 5. Gene and rule representation

Each new leaf supplies:

- `cond:<id>:mode`: categorical `off`, `below`, `above`.
- `cond:<id>:value`: numeric range from section 3.

Use separate IDs per structure, e.g. `o_ic-market-adx`, `o_lc-market-adx`.
Features are shared computations, but a later composed strategy must be able to
require weak ADX for one structure and strong ADX for another. For the wheel,
use a wheel-specific new-field ID even though its existing entry is based on
the cash-secured-put builder.

Proposed optimizer-template metadata:

```json
{
  "id": "o_ic-market-adx",
  "field": "underlying_adx_14",
  "op": "<",
  "value": 25,
  "optimize": true,
  "value_min": 10,
  "value_max": 40,
  "value_step": 5,
  "mode_optimize": true,
  "mode_choices": ["off", "below", "above"]
}
```

The authored `op`/`value` are the template's explicit fixed interpretation,
not a claim about which evolved mode is best. A GA trial must supply every
declared mode gene. The all-off control supplies `mode="off"` explicitly for
each new leaf; it does not rely on an implicit fallback.

Decode behavior:

| Mode | Concrete rule |
|---|---|
| off | Remove the leaf. Do not evaluate it, even when data is missing. |
| below | Ordinary numeric leaf with `< threshold`. |
| above | Ordinary numeric leaf with `> threshold`. |
| `<choice>` (categorical fields only) | Leaf with `== choice`; no threshold gene. |

A categorical field such as `structure_state` (section 3.2) declares
`mode_choices` as `off` plus its allowed values (`["off", "bull", "bear"]`)
and **no** `value_min`/`value_max`/`value_step`; the collector emits the mode
gene only. A numeric field must not list a value choice, and a categorical
field must not carry a threshold — reject either at template load, the same
way conflicting toggle metadata is rejected below. `none` is never a choice:
a leaf that required "no structure" would pass on missing pivots, which is
exactly the unknown-passes-a-gate failure this design refuses.

Equality passes neither strict comparison. Unknown never passes an active
comparison. In OR contexts an unknown leaf is false for its branch, not a
reason to block an unrelated valid branch. The first launcher profile uses
the existing flat AND entry tree only.

Declare and preserve `mode_optimize` / `mode_choices` through Python canonical
models and TypeScript import/edit/export paths. Reject a leaf that combines
mode optimization with `toggle_optimize=True`; two independent disable controls
would create contradictory genomes. Reject unknown mode values and unsupported
choice lists. A legacy leaf without these metadata fields follows its existing
collection and decoding path unchanged.

Decode must synchronize `op` and `comparison` before normalization/export so a
stale alias cannot restore the original operator. Live deployment receives only
resolved numeric conditions, never an unresolved `mode` token. The exporter must
reject unresolved new-mode genes; old servers must reject unsupported new field
names rather than dropping them and trading ungated.

Disabled thresholds are inactive dimensions. Canonicalize them to the declared
anchor for phenotype comparison/deduplication in this profile; preserve the raw
genome as provenance. Do not redefine checkpoint or deduplication semantics for
old jobs.

## 6. Strategy integration and limits

Add an explicit launcher profile, proposed CLI:
`--market-condition-profile ohlcv-v1`. The declared existing/default profile is
`none`, which emits exactly the current rules and genes. New-profile jobs must
record the profile and all calculation/data policies in their run configuration
and configuration digest. Persist them in deployment payloads too.

Append the three leaves to the initial-entry AND tree for all 16 currently
permitted discovery structures:

- Pure-option structures: their option-entry rule.
- O_CC and O_PP: the stock-entry rule, so the condition decides when to start
  the stock-plus-option strategy. It does not delay the protective put or
  covered call after the stock was bought.
- O_WHEEL: the initial short-put entry. Subsequent covered calls on assigned
  stock retain their existing lifecycle rules.

Existing safeguards, affordability limits, signal/expected-profit gates,
scheduled analysis and the naked-short exclusions remain intact. No new entry
condition prevents exits, position reductions or protective-order maintenance.

**Opportunity boundary:** these genes filter existing eligible expert
recommendations. They do not make HOLD/SKIP/ERROR recommendations eligible or
create trades in a symbol the expert omitted. A neutral-market expert/universe
entry mode would require a separate live/BT design. It is not hidden inside an
ADX gate. Report eligible recommendations separately from condition rejections.

The three metrics do not encode all regimes. A weak ADX is an observation, not a
forecast of a quiet future; one threshold cannot encode an arbitrary interval.
Keep those limitations in report labels and the resulting expert description.

### 6.0 Equity strategies S1–S7 and the follow-on stock grid

Every condition in this document — the section 3 trio and the section 3.2
chart-structure fields — is also available to the classic equity strategies.
Placement: the leaves are appended to the **initial-entry AND tree of each
equity strategy's entry rule** (the trees `ba2test_launcher.py` builds for
S1–S7), never to the open-positions or exit rules, so no gate can prevent an
exit or a protective-order adjustment. Per-strategy IDs as in section 5, e.g.
`s1-structure-dist-support`. Separately, exit rules may now carry market-condition
leaves, as their own added rules, when every action is on the exit allow-list
(close, close_option, adjust_stop_loss, adjust_take_profit): see
[pullback_and_market_exits.md](../strategy_research/exploration/pullback_and_market_exits.md).

The profile flag is the same one, on the equity drivers:
`--market-condition-profile none|ohlcv-v1|ta-structure-v1|ohlcv-v1,ta-structure-v1`.
`none` stays the default and emits exactly today's rules and genes, so the
goal2020 grid, its 135 completed optimizations, their labels and the 26
forward-test deployments of 2026-09-14 are untouched: **a new equity grid
under a new name runs with the profile on, after the option work lands.** It
starts from the same frozen all-off control and matched seeds as the option
launches, and its results are compared against the goal2020 cells of the same
expert/band/strategy, not merged into them.

Feature rows are shared: an equity grid and an option grid on the same
universe and dates read the same manifest, and a symbol warmed for one is
warmed for the other. The equity universe is larger (the small band alone is
several hundred symbols), so the union warmup of section 4.4 is planned for
the equity universe first; the option universe is a subset.

Gene count per equity strategy with both profiles on: 6 + 9 = 15 additional
genes on top of the strategy's existing space. That is a real widening —
report population size and generation count against the wider space in the
run configuration, and keep the section 3.2 "searched in v1" subset unless a
smaller space is shown to be insufficient.

### 6.1 Follow-up grids: explicit coverage

The follow-up driver is `tools/run_options2_matrix.py`. Its phase-1 keys are
O_LEAP, O_PMCC, O_ERN, O_CBS and O_PBS. The related
`tools/run_convex_matrix.py` owns O_CONVEX and a different fitness. Both must
support the same optional `ohlcv-v1` condition profile, preparation service,
manifest contract and concrete-rule export as the stage-1 driver.

| Key | Where the new conditions apply | Added genes | Research question, not a prescribed profitable rule |
|---|---|---:|---|
| O_LEAP | Independent O_LEAPC and O_LEAPP initial-entry rules | 12 | Does entry trend direction/strength improve each long-dated arm? |
| O_PMCC | Initial opening of the complete long/short diagonal | 6 | Which underlying conditions favour starting the covered LEAPS strategy? |
| O_ERN | Initial event-trade entry, ANDed with its existing event-stamped timing gate | 6 | Does pre-event trend or volatility expansion distinguish useful earnings opportunities? |
| O_CBS | Initial call-backspread entry | 6 | Which conditions favour the upside-convex payoff? |
| O_PBS | Initial put-backspread entry | 6 | Which conditions favour the downside-convex payoff? |
| O_CONVEX | Independent O_CONVEXC and O_CONVEXP initial-entry rules in its separate grid | 12 | Can conditions improve the entry selection without removing the rare winners the strategy seeks? |

The two-arm keys are one job each, not separate launchable call/put strategy
keys. New leaf IDs use the actual member names, e.g. `o_leapc-market-adx`
and `o_leapp-market-adx`, with independent mode/threshold genes. One arm can
seek strong trends while the other disables the gate. Shared feature values
never imply shared gene IDs. Existing arm-enable genes, directional filters,
first-match order and per-instrument position guards keep their meanings.

Use the same three definitions, periods and initial ranges across these grids.
An option with a one-year expiry does not require changing the indicator to a
one-year lookback: this profile measures **entry conditions**, not the entire
holding-period regime. Report that distinction and keep the all-off control.
A slower indicator profile, if later justified, receives a separate name and
calculator identity rather than silently changing what O_LEAP's field means.

#### Lifecycle and event invariants

- **PMCC:** apply the gates before opening both legs. Do not place them on
  `pmcc_roll_dte`, `pmcc_roll_buyback`, `pmcc_delta_floor`, the short-leg
  re-selection action or ordinary closes. If market conditions change after
  entry, existing expiry-driven management must still run. A proposal to defer
  a new short leg after buying back the old one is a separate lifecycle design.
- **Earnings:** retain FMPEarningsEvent as O_ERN's paired expert, its stamped
  `rec_days_to_earnings` entry window, mandatory event exit, and the requirement
  that expiry clears the event. The new fields use only prior-session prices;
  the earnings move itself cannot leak into the pre-event volatility ratio.
  Do not attach a generic "avoid upcoming earnings" gate to this event strategy
  or replace its stamp with another calendar lookup. Later earnings-data work
  must preserve this distinction.
- **Backspreads/convexity:** keep the option leg ratios, strike selection,
  sizing and exit semantics. Losing frequency alone does not determine whether
  a tail-payoff structure is useful. The new gates remain optional and their
  effect on rare winners, frequency and issuer concentration must be reported.
- **Calendars:** O_CAL remains phase-gated. Adding reusable conditions does
  not establish that its two-expiry lifecycle/data prerequisites are satisfied.

#### Shared warmup across grid families

Existing option-history coverage and chain-depth preflight remain separate
requirements from the new indicator cache. Verify the selected option store,
run window and actual chain requirements first, then warm the union of the
retained per-strategy symbol lists once. O_LEAP/O_PMCC currently probe DTE >=
365, O_CBS/O_PBS >= 180, O_ERN >= 7 and the convex grid >= 270; a probe is a
screening requirement, not proof every entry date has an executable chain.

For overlapping symbol/session/source inputs, grid 1, grid 2 and the convex
grid reuse exactly the same feature objects and host mappings. Different
universes or windows need only additional rows and manifests; different
fitness functions need no new indicators. O_ERN still needs its independent
event-history warmup, and the other experts still need their own existing
input caches. A ready indicator cache cannot substitute for either one.

#### Driver and comparison requirements before launch

All three drivers must forward the condition profile and pinned manifest to
the actual launcher and every remote trial. Preserve each grid's explicit
expert pairing, option store/root, capital, schedule, fitness and trade floors.
Share preparation/identity helpers; keep the strategy matrices and fitness
policies distinct.

The current source inspection found these integration details to handle while
implementing the new profile (no drivers were modified during this design):

1. The grid-2 parser now defaults to 2020/ThetaData, while its introductory
   examples still describe 2023/TastyTrade. The convex driver still defaults
   to 2023/TastyTrade. Resolve and print actual configuration; do not infer a
   common data window from their headers or silently retarget an old job.
2. Both follow-up preflight functions invoke `probe_option_chain_depth.py`
   without `--root`; the probe defaults to the TastyTrade tree. Grid-2's
   ThetaData trial can therefore be screened against a different store.
   **Before launching the new profile, resolve the selected option root once
   and pass it to both the probe and trial**, recording it in the report and
   checking this wiring in a driver test.
3. Follow-up completion lookup is based on the job name. Introduce an explicit
   new-profile configuration digest covering strategy/arm schema, experts,
   feature manifest, option-source identity, dates, capital, fitness and search
   settings. Old completed rows/checkpoints must not skip or resume a changed
   search. Keep old profiles/results accessible.
4. Grid 2 and the convex driver currently declare modest population 40 /
   generation ceiling 6 defaults. Adding six or twelve genes does not turn
   that into a demonstrated thorough search. Expose/persist a deterministic
   seed, measure the resulting full genome, and compare matched-seed all-off
   controls and enabled-profile pilots before selecting the final search
   budget. Do not silently claim the old budget is sufficient or change it
   inside an existing job.

Grid 2 retains `option_car` and its key-specific cadence treatment; O_CONVEX
retains `option_convex`. Compare conditions-on versus all-off **within the
same strategy, source, window, capital and fitness**. Cross-grid fitness
numbers are not interchangeable. Additional regime breakdowns are diagnostics,
not an opportunity to change the objective or suppress losing regimes after
seeing the results.

## 7. Failure behavior and reporting

Each value has one of: valid, insufficient history, missing expected session,
invalid prices/adjustments, unavailable clock/context, or missing replay object.
Report configuration/replay failures distinctly from normal short-history cases.
An invalid required observation refuses that entry condition and records why;
it never becomes 0, a neutral regime, or an enabled-gate pass.

Add a compact result summary per job:

- winning modes, thresholds, source/timing/calculation versions;
- eligible recommendation count, evaluated/gate-rejected entries, unknown-input
  counts by reason, and actual submitted/filled structures;
- per-year profit, CAR and drawdown as already computed by the account engine;
- attribution of executed structures' net P&L and concentration to their recorded
  entry-state measurements, with issuer/date counts and explicit bin boundaries.

Trade-group attribution is not a stand-alone regime portfolio CAR. Do not
annualize a filtered subset of overlapping trades as if it were a funded account.
Store coverage diagnostics for feature-off winners too in a separate offline
report, without introducing decision-path fetches or changing those trials.

Condition arithmetic is independent of balance, leverage and allocation. Equal
source windows must produce equal values and gate outcomes for a $4k account
and a $2k account with its existing live leverage policy. This design makes no
new claim that different fills or changing account equity produce identical P&L.

## 8. Validation required before enabling the new profile

1. **Calculator fixtures:** independently checked rising/falling paths, flat
   slope with valid ATR, noisy/weak trends, expanding/contracting returns,
   zero/nonfinite denominators and a split-adjustment case. Pin intermediate
   EMA/ATR/DI/DX values, not only the final output.
2. **Window invariance:** extra history before the 128-bar window changes no
   value; adding any future bar changes no earlier value. Batch/cache results
   match the reference implementation and comparisons at threshold boundaries.
3. **Session timing:** 09:30 and afternoon use the same prior-session window;
   cover Mondays, holidays, DST changes and half days. Daily session-labelled
   BT and intraday/live adapters resolve the same cutoff for the same session.
4. **Missing/corrupt data:** active gates fail with the stated reason, disabled
   gates make no data call, missing latest session cannot fall back to an older
   one, replay misses cannot call a network provider, and retries can observe
   a repaired cache.
5. **Genes and serialization:** three leaves produce six additional genes per arm;
   above/below/off decode correctly; aliases survive UI/API/export/import;
   every active field creates a shared engine trigger. Unknown modes/fields
   and conflicting toggle metadata fail visibly.
6. **All structures:** verify CC/PP stock-entry placement, wheel entry placement,
   per-member IDs in composition, unchanged lifecycle/exits, and unchanged
   HOLD/SKIP eligibility. Confirm no safety guard can be removed by these genes.
7. **Live/BT decision parity:** same recorded window, decision context, rule and
   recommendation produce the same feature values, gate decisions and intended
   entry eligibility through the actual shared evaluator. Mock brokers only.
8. **Compatibility:** existing golden backtests remain unchanged. In the new
   profile, all modes off reproduces the corresponding frozen baseline's orders,
   trades and equity curve; additional research metadata is compared separately.
   Include representative option, CC/PP and wheel cases, plus current parity gates.
9. **Cache behavior:** multiple leaves/genomes reuse the same observation;
   corrections change identity; concurrent analyses do not share mutable clock
   or source state. Benchmark cold precompute and warm-trial overhead on a fixed
   cache subset before scheduling a larger search.
10. **Central warmup/reuse:** test the second-run zero-fetch/zero-recompute
    contract, expanded dates/universe, raw-data correction, concurrent builders,
    interrupted publication and repaired negative entries. CLI/API/live entry
    points must produce the same dependency plan and equivalent manifests.
11. **Distributed readiness:** test cross-root/OS manifest identity, same-size
    corruption rejection, snapshot-scoped sync, local mapping-only rebuild,
    worker recycling and refusal of unprepared workers. No trial may access a
    network fetcher; missing feature artifacts must not become fitness results.
12. **Retention and load:** prove old jobs/replays survive a newer publication,
    mapped files are not overwritten/deleted under readers, and the target
    worker count stays within the measured memory/descriptor budget. Record the
    performance counters in section 4.6.
13. **Follow-up matrices:** O_LEAP/O_CONVEX add twelve independent genes each;
    other covered follow-up keys add six. Prove opposite arm thresholds do not
    overwrite each other, disabled arms do not fetch, O_ERN's mandatory event
    rules survive, PMCC still rolls/closes when new entry gates become false,
    and O_CAL remains refused. Test manifest/profile/seed forwarding, selected
    option-root parity between probe and trial, fitness separation, fresh
    checkpoint identities and feature reuse across all three drivers.
14. **Chart-structure calculators (section 3.3):** fixtures for a pivot that
    is not yet confirmed (must be absent from the row K−1 sessions after the
    extreme and present K sessions after), equal-price ties (not a pivot), a
    window with no pivot above the close (unknown, not the window high),
    touch counting at the tolerance boundary, σ = 0 channel (width/position
    unknown, slope 0), a close outside the channel (position outside 0..1,
    unclamped), breakout measured against the range that excludes the
    session itself, alternating-swing reduction with two highs between lows,
    each of bull/bear/none, and BOS/CHoCH walked with pivots as confirmed at
    each session. Pin the intermediate pivot lists and swing sequences, not
    only the final fields.
15. **Chart-structure precomputation:** the batch-over-full-history
    implementation equals the 128-bar reference for every session and every
    field; adding a future bar changes no earlier row (the confirmation lag
    makes this the sharpest test in the profile — a batch that uses pivots
    the future confirms fails it). Measure and record cold build per symbol
    and per-trial lookup cost; a trial that constructs a DataFrame or calls
    a calculator for any of these fields fails.
16. **Categorical mode genes:** `structure_state` emits one gene with the
    declared choices, decodes to an equality leaf, and is rejected at
    template load when it carries a threshold or lists `none`.
17. **Equity placement:** on each of S1–S7 the leaves land on the initial
    entry tree only; `none` reproduces the goal2020 rules and genes exactly;
    an equity job and an option job over the same universe and dates resolve
    the same manifest and read identical feature rows.

## 9. Implementation order

| Step | Deliverable | Completion evidence |
|---|---|---|
| 1 | Shared pure calculators and explicit observation/context types in `packages/common/ba2_common/core/market_conditions.py` (new) | Formula/window fixtures and clear invalid-data outcomes |
| 2 | Central immutable feature store, manifests, incremental warmup service and shared source evidence; CLI/API wrappers | Repeat-run zero-fetch/zero-recompute, correction, resume and concurrent-publication tests |
| 3 | Live/BT contexts, prior-session lookup, live readiness and replay capture | Session/cutoff and offline replay tests |
| 4 | Worker manifest sync/verification and `DerivedArrayStore` feature reader; prepare-host integration | Same values across hosts, mapping-only rebuild, no trial-time computation/network and bounded resources |
| 5 | Three numeric event types, condition classes, factory/registry and rule-builder mappings | Real condition-to-trigger/evaluator tests |
| 6 | Mode-gene metadata, collection/decoding, Python/TS round trips and concrete deployment export | Six-gene and serialization tests; unsupported-field rejection |
| 7 | Opt-in profile across the stage-1, grid-2 and convex drivers; union preflight, correct option-probe root, per-arm placement, run/deploy metadata and separate identities | Prepared-worker dispatch, all three dry-run matrices and old-profile/all-off compatibility checks |
| 8 | Coverage/performance report, entry-state attribution and a small paper/offline parity pilot | Measured cache reuse and unchanged existing golden results |
| 9 | `ta-structure-v1` calculators (section 3.3) in the same `market_conditions.py`, their batch form, the twelve stored fields, the five v1 condition classes and the categorical mode gene | Section 8 items 14–16; batch equals reference on a real symbol set; measured cold-build and lookup cost |
| 10 | Equity placement on S1–S7 and the profile flag on the equity drivers; the new-name equity grid definition | Section 8 item 17; `none` reproduces a goal2020 cell's rules and genes byte for byte |

Steps 9 and 10 build on steps 1–4 and reuse them unchanged: a second profile
in the same store, not a second store.

Do not move DS calculators out of their package or change their initialization
as incidental cleanup. The new shared implementation has its own pinned contract.
Shared condition code belongs in `packages/`, not in the live re-export shims.

Fresh runs retain the existing option fitness. Use a frozen all-off control and
matched seeds; start with a debit structure, a credit structure and overlay
placement checks before expanding. If old winners seed the new profile, map by
gene name and explicitly initialize the six new genes; never load an old genome
array by position into the expanded search space. Use new names/checkpoints and
keep running jobs on their original definition.

## 10. Follow-on conditions

| Addition | Proposed purpose | Prerequisite before joining GA |
|---|---|---|
| Broad-market trend | Per-structure SPY trend filter, independent of expert macro handling | Benchmark-symbol cache/session coverage; explicit benchmark in payload, no symbol substitution |
| Earnings proximity / event inside holding horizon | Separate event risk from ordinary entries | Point-in-time scheduled-date evidence, event-time semantics and chain-horizon handling; no annual analyst-period fallback masquerading as a scheduled report |
| Skew / term structure | Compare relative option pricing across strikes/expiries | Matched tenors/deltas, quote quality, identical live/BT construction and sufficient history |
| Neutral opportunity generation | Allow a structure to act without a directional BUY/SELL recommendation | Separate explicit eligibility contract for live and BT; never turn data errors or SKIP into permission to trade |

These are intentionally separate from the first six genes. Existing IV-rank and
IV/RV comparison directions remain fixed in v1; making them bidirectional would
also be a separately identified search-space change.

## 11. Decisions made by this design

- Build conditions, not a new statistical-discovery pipeline.
- First delivery: trend slope, ADX and RV5/RV20; six additional genes per entry
  arm, twelve for O_LEAP/O_CONVEX, available across all three grid families.
- Fixed 128-session calculation window and fixed indicator periods.
- Prior-session-only inputs in live and BT, explicit handling of daily BT labels.
- Shared observations and calculators; no condition-time API fan-out.
- Mandatory union warmup, central reusable feature manifests and prepared local
  mapped arrays; no per-trial indicator rebuilds.
- New search profile and concrete resolved rules on deployment.
- Preserve existing runs and defaults; do not alter production or running grids.
- Chart structure (support/resistance, channel, breakout, swing structure) as a
  second profile in the same store: **every field precomputed at warmup, a
  trial does a lookup and a compare, never a detection**. Components, not
  composite pattern detectors. Confirmed pivots (K = 3) as the lookahead guard.
- Twelve fields stored, five searched in v1 (nine genes per arm); the rest are
  a search-space decision for a later profile, not a data change.
- Not included: Wolfe waves, harmonics and other multi-point geometric fits.
  Their reproducibility between implementations is poor and their inputs
  (pivots, converging channels) are already in the profile; the GA can find
  that shape from the components if it exists without a declared detector.
- All conditions apply to both option structures and equity strategies S1–S7;
  a new equity grid under a new name runs with the profile on. goal2020 and
  the 2026-09-14 forward-test deployments are not re-run or re-labelled.

This document completes the design request. Implementation, new GA launches and
production deployment have not occurred.

## 12. References

- [Current option entry builder](../../testplatform/ba2test_launcher.py)
- [Current gene collector/decoder](../../testplatform/backend/app/services/strategy_param_space.py)
- [Canonical rule models](../../packages/common/ba2_common/core/rule_models.py)
- [Shared condition implementation](../../packages/common/ba2_common/core/TradeConditions.py)
- [Shared evaluator](../../packages/common/ba2_common/core/TradeActionEvaluator.py)
- [Shared rule-to-trigger mapping](../../packages/common/ba2_common/core/rule_builders.py)
- [Market calendar](../../packages/common/ba2_common/core/market_calendar.py)
- [Replay clock](../../packages/common/ba2_common/core/replay/clock.py)
- [Existing shared array store](../../packages/common/ba2_common/core/shared_arrays.py)
- [Existing array preparation tool](../../tools/build_shared_arrays.py)
- [Existing cache mirroring](../../testplatform/backend/app/services/cache_sync.py)
- [Existing expert prewarm orchestration](../../testplatform/backend/app/services/prewarm_fetchers.py)
- [Live capture/replay specification](2026-09-10-live-capture-prewarm-backtest-replay-spec.md)
- [Existing DS technical calculations](../../packages/experts/ba2_experts/DeterministicScorer/technical.py)
- [Backtest entry eligibility](../../testplatform/backend/app/services/backtest/daily_engine.py)
- [Original option-grid design](../superpowers/specs/2026-08-27-option-ga-grid-design.md)
- [Grid-2 driver](../../tools/run_options2_matrix.py)
- [Convex-harvest driver](../../tools/run_convex_matrix.py)
- [Chain-depth probe](../../tools/probe_option_chain_depth.py)
- [Grid-2 design](../superpowers/specs/2026-08-31-leaps-grid-design.md)
- [Convex-harvest design](../superpowers/specs/2026-08-31-convex-harvest-grid-design.md)
