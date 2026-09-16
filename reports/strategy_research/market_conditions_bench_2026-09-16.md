# Market-condition gates: performance and no-impact evidence

**Date** 2026-09-16 · **Branch** `feat/option-market-conditions` · plan Task 9, design §4.6/§7/§8.8
**Host** Windows 11 (10.0.26200), Intel 14C/20T, 68 GB RAM, Python 3.11.8, one machine, nothing
else running · **Snapshot** `ohlcv-v1` manifest `1136d489…f5cb3` (2020-01-01..2025-12-31,
85 symbols, 128,180 rows) · **Tool** `testplatform/backend/tests_scripts/bench_market_conditions.py`

The operator's requirement (2026-09-15) was *"ensure no impact on existing bt and test perf of
these new conditions"*. That splits into two separate claims, and both are measured here:

1. **No impact with the profile off.** Not "the same numbers" — the gates must never be
   *reached*. Identical results with the resolver quietly answering every leaf would be a worse
   outcome than a diff, because nothing about it would look wrong.
2. **Under 1% of trial time with the profile on.** A gate is one indexed lookup and one float
   comparison per evaluated leaf. Anything larger means a DataFrame, a calculator or a cache-tree
   scan leaked into the decision path — a defect to find, not a number to report.

---

## 1. No-impact evidence (profile off)

Two persisted genomes re-run through `tools/backtest_parity.py`, which runs the same genome in
two child processes and compares the persisted rows **byte-for-byte across every blob and metric
column**. Both children now report `market_condition_resolver_calls`, a process counter
incremented on every entry into `TradeConditions.resolve_market_condition_context`.

Run from the worktree with `PYTHONPATH` naming only the three package directories — **not**
`testplatform/backend`. With the backend on `PYTHONPATH`, `backtest_parity._bootstrap` sees
`app.models.database` as already importable and returns early, so `_enter_backend()` never runs,
`ba2_common` keeps pointing at the neutral default database, and the run dies ~160 s later on
"FMP API key not configured". That is a trap worth knowing; it is not a branch defect.

### bt 1681 — equity screener reference (`TOP1-scr-small-FMPInsiderClusterBuy-S7-goal2020-notional`)

```
PARITY_EVIDENCE={"bars_private_mb": 4374.1, "bars_shared_mb": 0.0, "market_condition_resolver_calls": 0, "options_entries": 0, "options_private_mb": 0.0, "options_provider_built": false, "options_shared_mb": 0.0, "shared_enabled": false, "total_trades": 168}
PARITY_EVIDENCE={"bars_private_mb": 729.0, "bars_shared_mb": 3645.1, "market_condition_resolver_calls": 0, "options_entries": 0, "options_private_mb": 0.0, "options_provider_built": false, "options_shared_mb": 0.0, "shared_enabled": true, "total_trades": 168}
```

* **PASS** — "the two rows are byte-identical across every blob and metric column"
* "archived vs private (informational): **identical**" — the re-run also equals the row persisted
  before this branch existed.
* 168 trades on both sides (not vacuous), 438 s / 441 s, persisted as backtests 1706 / 1707.
* **`market_condition_resolver_calls: 0`** in both children: the resolver was never called.

### bt 1688 — option reference (`TOP2-parity-probe-thetadata2020-O_LC`, ThetaData 2020)

```
PARITY_EVIDENCE={"bars_private_mb": 1.4, "bars_shared_mb": 0.0, "market_condition_resolver_calls": 0, "options_entries": 20, "options_private_mb": 2308.8, "options_provider_built": true, "options_shared_mb": 0.0, "shared_enabled": false, "total_trades": 72}
PARITY_EVIDENCE={"bars_private_mb": 0.2, "bars_shared_mb": 1.2, "market_condition_resolver_calls": 0, "options_entries": 20, "options_private_mb": 124.5, "options_provider_built": true, "options_shared_mb": 2184.3, "shared_enabled": true, "total_trades": 72}
```

* **PASS** — "the two rows are byte-identical across every blob and metric column"
* "archived vs private (informational): **identical**"
* 72 trades on both sides, 20 option underlyings loaded (the option reader was genuinely
  exercised), 256 s / 149 s, persisted as backtests 1708 / 1709.
* **`market_condition_resolver_calls: 0`** in both children.

### What the two runs establish together

Neither genome carries a market leaf, so nothing in the decision path could have called the
resolver — and the counter says it did not. The gates are not "cheap when off"; with the profile
off they are **absent**. Both re-runs also match the rows persisted before this branch, so the
equality is against the archive, not merely between two runs of the same new code.

---

## 2. Trial cost (profile on, three gates active)

30 full backtests per arm of **one fixed O_LC genome** (opt 522 / bt 1688's TOP2), 2022-01-01 ..
2023-12-31, on its own 20-symbol universe **minus the 4 the snapshot does not cover**
(`T MRK IBM HON` — the launcher would refuse a gated run over them), ThetaData option store, in
one process so the option arrays are built once and every trial after the first is warm.

* **off** — `market_condition_profile: none`, the genome exactly as persisted.
* **on** — profile `ohlcv-v1`, manifest pinned, plus **three active gates** on the entry rule,
  built by the launcher's own `_market_condition_gates` and decoded by the optimizer's own
  `_apply_mode`: `adx below 95`, `slope above −5`, `rv below 9`.

The thresholds are deliberately permissive. Both arms execute **exactly 30 trades**, so the two
arms do the *same* work and the difference is the gates and nothing else. A gated arm that
entered less would have measured the absence of trades, not the cost of a gate.

| | off | on |
|---|---|---|
| median | **35.24 s** | **34.89 s** |
| mean | 35.34 s | 35.16 s |
| min / max | 34.60 / 39.34 s | 34.43 / 37.64 s |
| stdev | 0.86 s | 0.86 s |
| median of the last 20 | 35.01 s | 34.89 s |
| trades | 30 (every trial) | 30 (every trial) |

**Measured overhead: −0.99 %** — the gated arm's median is 0.35 s *faster*. That is not a
speed-up: it is 0.4 of one standard deviation on a rig whose own spread is ±0.86 s (±2.4 %), and
the off arm ran first and paid the option-array build in its trial 1 (39.34 s). Comparing the
last 20 of each — both fully warm — leaves −0.34 %. **The gates' cost is below what this rig can
resolve.**

### The arithmetic that says the same thing without the noise

The benchmark emits this itself, as `arithmetic_bound` in its JSON, so the figure below is the
tool's own and can be re-derived from the file. It multiplies the measured per-operation costs
by the number of operations a trial can perform: the first market leaf on a bar pays a memo
**miss**, and each of the three leaves pays one `evaluate()` (which already includes its hit).

| input | value |
|---|---|
| symbols × sessions in the window | 16 × 501 = **8,016 memo misses** |
| × 3 leaves per bar | **24,048 evaluations** |
| measured `observe()` miss P50 | 14.400 µs |
| measured `evaluate()` P50 | 0.900 µs |
| gate cost per trial | 8,016 × 14.400 µs + 24,048 × 0.900 µs = **0.137 s** |
| against the measured trial median | 0.137 s / 35.242 s = **0.389 %** |

It is an **upper** bound: the entry rule short-circuits before most leaves ever run.

**Verdict: PASS.** Both the direct measurement and the arithmetic put the gates under the 1 %
bar, and the arithmetic — which does not depend on run-to-run noise — puts them at **0.39 %**.
Nothing in the decision path builds a DataFrame, calls a calculator or scans a cache tree: the
run's `computed` counter stays at 0 and every row comes from the mapped snapshot.

### The all-off arm of the acceptance criterion, and why it is not here

Task 9 also asks that "with the profile on and all modes off, the same rows are identical too".
**That is pinned, but not by `backtest_parity` and not on these two rows**, for two reasons that
are worth recording rather than working around:

1. **Coverage makes it impossible on the reference rows.** bt 1681 is a screener run over ~1,000
   small-caps and bt 1688's own universe contains 4 uncovered symbols. Turning the profile on
   pins a manifest, and the seam then *refuses* a GA-assembled config whose universe the manifest
   does not cover — correctly, since every gate on an uncovered symbol reads `missing_session`
   for the whole run. Trimming either universe to the covered subset changes which trades happen,
   so the comparison against the archived row would no longer be a comparison of the same run.
2. **The parity tool measures the wrong thing for this question.** Its row comparison walks the
   whole `results` blob byte-for-byte, and a profile-on run legitimately carries an extra
   `market_condition` block and an `entry_state` on each trade. Design §8.8 says the research
   metadata is "compared separately" — so the parity tool would have to learn to exclude it
   before it could answer this at all.

What pins it instead is stricter: `tests/backtest/test_market_condition_all_off_matches_baseline.py`
runs **one frozen option fixture through the real engine four times** — profile none; profile
`ohlcv-v1` with a pinned manifest and every mode off; a gate that is true on every session; and
the same gate inverted — and asserts that the first three produce **byte-identical orders, trades
and equity curves** (same SHA-256 fingerprint), while the fourth trades nothing and its counters
say why. That compares the actual curves rather than a metric summary, and it runs in CI on every
commit, which a 15-minute re-run of an archived row never will.

**Decided in Task 10 (2026-09-16):** they do -- as their own `_RESEARCH_KEYS` tuple, not by
being folded into `_IDENTITY_KEYS`. `compare_rows` no longer walks them (a profile-on run
carries them and a profile-off run does not, and that difference is the FEATURE, not a parity
failure), and `compare_research_metadata` compares them in a separate section: the block and
every per-trade `entry_state`, addressed as `results.market_condition` /
`trades[i].entry_state`. It is part of the verdict for the private-vs-shared pair (identical
trades explained by different measurements is a reader defect) and informational against an
archived row, which may legitimately predate the feature or its block shape.

The D(b) gate still could not be RE-RUN through the tool: reason (1) above is unchanged, and a
search of the test DB on 2026-09-16 found no persisted gated genome to re-run at all (no
`strategy_optimizations` row carries a `market_condition` block, and no `backtests` row carries
one in its results). The tool blocker is removed and pinned by
`tests/test_backtest_parity_tool.py`; the gate becomes runnable as soon as a gated job is
launched over a universe its manifest fully covers.

---

## 3. Per-observation and per-evaluation cost (real snapshot, 20 covered symbols × 20 sessions)

| measurement | P50 | P95 | n |
|---|---|---|---|
| `observe()` miss — **trial path** (`retain_windows=False`, what a backtest uses) | **14.4 µs** | 22.7 µs | 400 |
| `observe()` hit — trial path (a second leaf on the same bar) | **0.60 µs** | 0.70 µs | 2000 |
| gate `evaluate()` — the whole condition: resolver → memo → `by_field` → compare | **0.90 µs** | 1.10 µs | 400 |
| `observe()` miss — **capture path** (`retain_windows=True`, live replay capture only) | 5.61 ms | 10.52 ms | 400 |
| `observe()` hit — capture path | 0.50 µs | 0.60 µs | 2000 |

`computed = 0` and `mapped_rows = 400` on both paths: **with a manifest pinned nothing is
calculated**, which is the contract the whole feature store exists for.

**The two reader configurations are three orders of magnitude apart, and that is not a defect.**
A reader that RETAINS windows additionally fetches and re-hashes the row's raw shard, because
the replay recorder has to write down the bytes the value was computed from. `BacktestMarketConditionReader`
passes `retain_windows=False` and never pays it; only the live capture path does, once per
(symbol, session) per analysis. The first draft of this benchmark measured the retaining reader
and reported 5.8 ms as the trial cost — wrong by 390×, and in the direction that would have
failed the acceptance bar for a cost no trial pays. Both are reported here so neither can be
quoted for the other.

## 4. Worker footprint at the intended grid size

30 spawned children, each opening the mapping and reading a row:

| | value |
|---|---|
| peak RSS per child | **42.6 MB** (median 42.2 MB) |
| total RSS, 30 children | 1.27 GB |
| open handles per child | **217** (median 216) |
| time for all 30 to open and serve a row | 0.97 s |
| children that could not be measured | 0 |

The mapped array set is **5 files whatever the symbol count** (session / values / status /
reason codes / meta json), so the descriptor cost does not scale with the universe. The ~42 MB
is a bare Python + numpy process; the mapping itself is 4.88 MB of shared pages.

## 5. Build counters (design §4.6) — quoted, not re-measured

Rebuilding the real snapshot to time it again would cost 143 s and a provider bill for numbers
Tasks 6 and 7 already measured on **this exact manifest**. From those measurements (98-symbol
option universe, 2020-2025, 1508 sessions):

| counter | value |
|---|---|
| `plan` | 55 s cold / 3.7 s warm |
| `build` | 143 s for 128,180 rows — 6205 feature objects + 6344 raw shards, 106 MB |
| `verify` | 8 s |
| `prepare-host` | 10.5 s cold / 6.8 s warm |
| mapped arrays | 4.88 MB, 5 descriptors |
| `observe()` (Task 7 rig) | 13.2 µs P50 / 15.8 µs P95 miss, 0.50 µs hit |

A second identical warmup performs zero provider calls and zero indicator recalculations; it
opens and verifies the existing snapshot.

## 6. Coverage of the option universe

`tools/options_universe_top100.txt` has 98 symbols; the snapshot covers **85**. The 13 it does
not — `ASML BHP DELL GE HON IBM MRK NVS RTX SAN SCCO T WDC` — carry a split whose basis the
cached prices cannot settle and need a full provider re-fetch first. Every number in this report
was measured on covered symbols only: an uncovered symbol returns `None` in nanoseconds and would
flatter every latency figure here.

**This is a decision owed before the first gated grid** (re-fetch, or trim the universe) — see
the runbook's "Market-condition feature store" section. The launcher refuses to dispatch a run
whose pinned manifest does not cover its universe, so it cannot be skipped by accident.

## 6b. `ta-structure-v1` warmup cost (Task 10, measured 2026-09-16)

The second profile computes twelve chart-structure fields per (symbol, session) instead of three
indicator fields. All figures below are on this box, AAPL out of the local FMP daily cache, taken
as the median of five runs.

| measurement | before item 1 | **after** (rolling range, sorted-level bisect) |
|---|---|---|
| cold batch build, FULL AAPL history (3,802 bars -> 3,675 rows) | 370 ms | **276 ms** |
| per row | 101 us | **75 us** |
| cold batch build, a realistic 6-year warm span (1,512 rows) | 143 ms | **109 ms** (72 us/row) |
| per-window reference over the same 3,675 rows | 893 ms | 906 ms |
| **batch vs reference** | 2.86x | **3.28x** |
| `by_field()[field].value` on a stored row | 0.43 us | **0.37 us** |

A 100-symbol option universe over the 6-year grid window is therefore about **11 seconds** of
chart-structure compute, against roughly 36 seconds for the per-window path.

### Why this is ~0.1 ms per ROW and not section 3.3's "~10 ms per SYMBOL"

Section 3.3 reasons that "none of these fields is recursive, so a batch implementation over full
history and the 128-bar reference implementation agree exactly", and prices the batch at about
ten milliseconds per symbol. The first half is true of the twelve STRUCTURE measurements. It is
not true of **ATR14, which every one of them is divided by**: ATR is a Wilder recursion *seeded
inside each window* (section 3.1, "stable initialization" -- a session's output comes from
exactly its last 128 eligible bars, which is what makes live and BT agree whatever prehistory
each holds). A single global ATR pass would produce different numbers and would destroy window
invariance, so the batch re-runs the 114-step recursion per row. That recursion is the dominant
cost: it is ~85 % of the 75 us, and it is the entire gap against the design's estimate. The
estimate should be read as ~0.1 ms per row, i.e. ~0.3 s per fully-warmed symbol, not as a bug.

The three structures section 3.3 prescribes are implemented as prescribed, with one exception:

* **pivots from K-shifted comparisons, confirmed at `p + K`** -- found once over the history and
  sliced per window (a pivot is a local property of bars `p±K`, so a window's pivots are exactly
  the global ones with `s + K <= p <= e - K`; a future-confirmed pivot is structurally
  unreachable, pinned by `test_adding_a_future_bar_changes_no_earlier_row`).
* **nearest-level queries against the sorted confirmed levels** -- two sorted price lists carried
  across the sliding window, queried with `bisect`. Bisect is a selection, so it returns the same
  element `min`/`max` would.
* **prior range via rolling max/min shifted by one** -- one vectorised pass per array, read at
  `win[e - 20]`. Also a selection.
* **regression via cumulative sums of x, y, x^2, xy** -- **NOT used.** It was implemented and
  measured against the `math.fsum` reference over the three synthetic histories and AAPL:
  **every row differs**, by up to **4.3e-07 relative**. sigma comes out of the
  `Syy - a*Sy - b*Sxy` cancellation between quantities of order price^2 while the residuals are a
  few ticks, and the running sums span the whole history rather than twenty points. The plan is
  explicit that correctness beats the shortcut, and a 1e-7 drift in a value the GA compares
  against a threshold is a different decision rather than a rounding detail, so the channel is
  re-fitted per session. `test_cumulative_sum_ols_is_not_bit_exact_so_the_batch_fits_per_session`
  keeps that deviation as evidence rather than an omission, and fails if the drift ever becomes
  small enough to re-open the choice.

Reproduce with `packages/common/tests/test_chart_structure_batch_equals_reference.py`, whose
`test_cold_build_and_lookup_cost_are_reported` prints the cold-build and lookup figures on every
run (and refuses a per-row cost that has become absurd), and whose
`test_batch_equals_reference_for_every_session_and_field` pins the equality that makes the whole
batch legitimate -- exact `==` on every field of every session of three 600-bar synthetic
histories and 1,200 real AAPL sessions.

## 7. Reproducing

```bash
# the real snapshot (no trial phase -- no database or option cache needed)
$PY testplatform/backend/tests_scripts/bench_market_conditions.py \
    --manifest 1136d489...f5cb3 --phases observe,gate,workers \
    --symbols 20 --sessions 20 --workers 30 --out bench.json

# the trial phase as well (needs the test DB and the option store)
$PY testplatform/backend/tests_scripts/bench_market_conditions.py \
    --manifest 1136d489...f5cb3 --bt 1688 --evaluations 30 --out bench.json

# the whole harness on a fabricated store, in a second
$PY testplatform/backend/tests_scripts/bench_market_conditions.py --quick --out quick.json
```

`--quick` is exercised by `testplatform/backend/tests/test_bench_market_conditions.py`, so the
benchmark cannot rot into a script that raises on the day a performance question is urgent.
