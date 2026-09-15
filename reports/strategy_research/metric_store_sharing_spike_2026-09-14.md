# Screener metric store — shared-array spike

**Date:** 2026-09-14 · **Status:** research only, nothing implemented
**Box:** Windows 11, 68.4 GB RAM · **Venv:** `C:/Users/basti/ba2-venvs/test` · pandas 2.3.3, numpy 2.2.6, pyarrow 24.0.0
**Store measured:** `C:\Users\basti\Documents\ba2\common\cache\screener\metric_store`
**Related:** `docs/plans/2026-09-14-shared-arrays-across-workers.md`, `reports/strategy_research/option_array_sharing_bench_2026-09-14.md`

---

## 0. Executive summary

| Question | Answer |
|---|---|
| How big is the store per worker, really? | **655 MB** `memory_usage(deep=True)`, **~1062 MB RSS** after `load_store` — not the ~5.3 GB the trial telemetry shows (that field is the OHLCV bar cache, see §1.4). |
| Does `pd.DataFrame(dict_of_mapped_arrays, copy=False)` copy on pandas 2.3.3? | **No.** The block manager keeps one 1-row block per column and `np.shares_memory` is `True` for every column. Consolidation is lazy and **no** normal read operation triggered it (§3). |
| Can `symbol`/`date` be shared? | **Yes**, as `pd.Categorical.from_codes(mapped_int16_codes, categories)` — the codes are **not** copied when the mapped dtype already equals the dtype pandas would pick for that cardinality (int16 for 4 734 symbols / 339 dates). |
| Measured multi-worker saving | 4 workers: system available RAM drops **4 087 MB** today vs **242 MB** mapped. **≈1.0 GB saved per worker.** |
| Recommendation | **(c), in the order a → then a trimmed b.** Ship the `DerivedArrayStore` mapping first (it subsumes the windowed load and gives lazy per-column materialisation for free), then fix the two uncached per-bar consumers that re-scan the object `date` column. |

---

## 1. Measurements

### 1.1 Shape and disk

| | |
|---|---|
| Partitions | 1 351 parquet files in **78** `ym=` dirs, `ym=2020-01` .. `ym=2026-06` |
| On disk | **397.6 MB** |
| Rows | **1 284 870** |
| Columns | **45** |
| Unique symbols | 4 734 |
| Unique scan dates | 339 (weekly cadence) |
| Rows per scan date | mean 3 790, min 3 091, max 4 712 |
| `load_store()` wall time | **6.3 – 7.4 s** (cold-ish page cache), process RSS after: **1 062 MB** |

### 1.2 In-memory cost, split by dtype

| dtype group | n cols | `deep=True` | `deep=False` |
|---|---:|---:|---:|
| float64 | 42 | **431.7 MB** | 431.7 MB |
| object | 3 | **223.4 MB** | 30.8 MB |
| **Total** | **45** | **655.1 MB** | 462.6 MB |

Every float64 column is exactly `1 284 870 × 8 B = 10.28 MB`.

### 1.3 The object columns — where the 223 MB goes

| column | deep | shallow (pointers) | nunique | as `category` | **read by any consumer?** |
|---|---:|---:|---:|---:|---|
| `sector` | **80.1 MB** | 10.3 MB | 11 | 1.29 MB | **NO** — written by the build (`metric_store.py:607`), never read by any screen/consumer |
| `date` | **75.8 MB** | 10.3 MB | 339 | 2.60 MB | yes, on every hot path |
| `symbol` | **67.5 MB** | 10.3 MB | 4 734 | 2.95 MB | yes |

So the *entire* string cost of the store, 223 MB/worker, collapses to **6.8 MB** as categoricals — and **80 MB of it is a column nothing reads**.

### 1.4 Correction to the premise: the 5 333 MB telemetry is NOT this store

`strategy_optimization_handler.py:794-796` formats

```
| rss {rss_mb}MB | bars {symbols} sym {bars} bars {mb}MB | memo {symbols} sym {rows} rows {mb}MB
```

`bars …MB` is `mem["bar_cache"]` and `memo …MB` is `mem["series_memo"]`, both produced by
`testplatform/backend/app/services/backtest/price_source.py:275` — the **OHLCV** caches.
The screener metric store appears in **neither** field; it is invisible in that line. The real
per-worker metric-store figure is the 655 MB / 1 062 MB RSS above.

### 1.5 Row ordering — a per-date row-range index does NOT work as-is

| check | result |
|---|---|
| `date` monotonic non-decreasing | **False** |
| contiguous runs of equal date | **1 284 459** (would be 339 if date-grouped) |
| `symbol` sorted | **False** |

The store is **symbol-major inside each `ym=` partition** (all of NVDA's rows for the month, then
the next symbol), so rows for one scan date are scattered across ~4 700 runs. A
`searchsorted(date_codes, i)` row-range would require a one-time stable argsort by date code at
build time. That reorder is **free to do in the derived-array build** (it is a new artefact) and
costs nothing at read time — but it is *not* available on today's concat order.

### 1.6 Cost of the hot primitives (real store, 1.28 M rows)

| operation | today (object `date`) | mapped + categorical | pure array on codes |
|---|---:|---:|---:|
| `dates[dates <= as_of].max()` (the as-of resolve in `screen_universe_as_of` / `metrics_as_of`) | **129 ms** | 5.4 ms | — (bisect over 339 strings, already done in `scan_dates`) |
| `df[df["date"] == day]` | **81 ms** | 3.7 ms | `np.flatnonzero(codes == i)` **0.64 ms** |
| `metrics_as_of(df, day, [col])` (mask + `set_index` + `to_dict`) | **94 ms** | 12.6 ms | fancy-index one column: **<0.01 ms** |

### 1.7 Derived-array artefact

Building 42 `float64` `.npy` + 3 code `.npy` (`int16` symbol, `int16` date, `int8` sector) + a
`__cats.json`:

| | |
|---|---|
| Build time (from the loaded frame) | **0.8 s** |
| Artefact on disk | **438.2 MB** (vs 397.6 MB parquet — uncompressed, as expected) |
| Rebuild a full DataFrame from the maps | **0.35 s** (vs 6.3 s parquet concat, **18×**) |
| USS added by the rebuild | **+0 MB** |
| `df.memory_usage(deep=True)` of the rebuilt frame | 438.6 MB (accounting only — the bytes are not resident) |

### 1.8 The real multi-worker number

4 spawned children, each loading the store and touching one scan date, measured as the drop in
**system available memory** (the only honest metric for shared pages):

| | 1 child | 4 children | per extra worker |
|---|---:|---:|---:|
| `pd.concat(read_parquet)` — today | 962 MB | **4 087 MB** | ~1 040 MB |
| mmap `.npy` + categorical codes | **0 MB** | **242 MB** | ~60 MB |
| child RSS reported | 1 062 → 97 MB | 1 062 → 97 MB | |

**≈1.0 GB of private RAM saved per worker**, and the mapped case's 242 MB for 4 workers is
mostly interpreter/pandas import cost, not store data.

---

## 2. Consumer map

Column legend: **S** = `market_cap, price, volume, float_shares, relative_volume,
price_drop_pct[_Y], weinstein_stage` + the `sort_metric` column + `symbol`, `date`.

| # | Consumer | file:line | frequency | reads | how |
|---|---|---|---|---|---|
| 1 | `_screened_symbols_for_bar` → `screen_universe_for_day` | `testplatform/backend/app/services/backtest/daily_engine.py:106-146`, called at `:611` | **every engine bar**, but memoised per **scan date** via `self._screened_cache` → ≤339 real computations/run | **S** | `df[df["date"]==day]` boolean mask, then a chain of `d = d[d[col] >= v]` masks, `sort_values(sort_col)`, `head(n)`, `list(d["symbol"])`. As-of date already resolved by `bisect` over `scan_dates` — **this one is already fixed**. |
| 2 | `MetricStoreATRProvider.get_indicator` → `metrics_as_of` | `testplatform/backend/app/services/backtest/seam_wiring.py:258-275` | **per `get_latest_atr` call** — i.e. per position sizing, per candidate entry (`ba2_common/core/TradeRiskManagement.py:1684`). **No cache at all.** | `date`, `symbol`, one `atr_<p>` | `dates[dates <= day].max()` (**129 ms**) + `df[df["date"]==day]` (**81 ms**) + `.set_index("symbol")[[col]].to_dict("index")` builds a ~3 800-entry dict — then reads **one** symbol out of it. ≈**94 ms per ATR lookup.** Hottest un-cached consumer in the store. |
| 3 | `FactorRanker._resolve_universe` → `screen_universe_as_of` | `packages/experts/ba2_experts/FactorRanker/__init__.py:411-417` | **per analysis/rebalance bar** (no cache) | **S** | as-of resolve (129 ms) + `screen_universe_for_day` (81 ms + mask chain) |
| 4 | `FactorRanker._store_factor_inputs` → `metrics_as_of` | `packages/experts/ba2_experts/FactorRanker/__init__.py:616-618` | **per `_gather`**, i.e. per analysis bar (no cache) | `date`, `symbol`, `momentum_12_1`, `close` | same as #2, but the whole ~3 800-row dict *is* consumed (one entry per universe symbol) |
| 5 | `_resolve_enabled_instruments` → `screened_symbol_union` | `testplatform/backend/app/services/backtest/daily_backtest_handler.py:616-634` | **once per run** (standalone path) | **S** over a date *range* | `(date >= lo) & (date <= end)` mask, then the same threshold chain, then `groupby(date).head(n)` |
| 6 | trial-config build → `screened_symbol_union` | `testplatform/backend/app/services/strategy_optimization_handler.py:1976-1983` | **once per GA individual, in the MASTER process** | **S** | same. Note the master therefore also holds a full private copy. |
| 7 | memo warm-up | `strategy_optimization_handler.py:1770` | once per process | — | `_ms.load_store(...)` purely to warm `_STORE_MEMO` |
| 8 | option-grid **gate-only** path | `testplatform/ba2test_launcher.py:3792-3827` (`_screener_gate_opt_block`), wired at `:5354-5365` | store loaded **once in the launcher** for a coverage check (`set(df["symbol"].unique())`); at run time it flows through `backtest_block["screener_opt"]` → `hoisted["screener_store"]` → `screener_runtime` → consumer **#1** | gate base is most-admitting: effectively only **`price`** (`price_max`) + `symbol`, `date` | The gate-only block sets `market_cap_min=0, relative_volume_min=0, price_drop_pct=0, weinstein_stage2_only=0, max_stocks=10000` — every `_ge`/`_le` with a `0` value is skipped, so an option job pays 655 MB/worker to evaluate **one** `price <= cap` comparison on 339 dates. |

### 2.1 What this means

* **Columns actually read.** Across every consumer: `symbol`, `date`, `market_cap`, `price`,
  `volume`, `float_shares`, `relative_volume`, `weinstein_stage`, `close`, `momentum_12_1`,
  `atr_{7,14,21,28}` and **exactly one** of the 30 `price_drop_pct_N` windows (selected by the
  `price_drop_days` gene) plus the legacy `price_drop_pct`. That is ≤16 of 42 numeric columns.
  The 30 windowed drop columns are **308 MB of the 432 MB numeric total** and a single trial
  touches at most one of them. `sector` (80 MB) is read by nobody.
* **Per-date row-range index.** Viable and cheap *if* the derived arrays are written sorted by
  date code (see §1.5): `np.searchsorted(date_codes, i)` then a contiguous slice, which turns
  consumer #2's 94 ms into a slice + one fancy index. Without the sort, `np.flatnonzero(codes==i)`
  is still 0.64 ms (127× faster than the 81 ms object mask) and needs no reorder — that is the
  low-risk form.
* **Integer symbol codes** serve every consumer: the screens only ever *return* `list(d["symbol"])`
  and `metrics_as_of` only ever *keys* by symbol, so a code → string decode at the boundary
  (≤4 712 strings per scan date, from a prebuilt list) is the whole cost.
* **No consumer mutates the memoised frame.** Greping `store_df[...] = ` in `metric_store.py`
  finds only reads (`:711, :1158, :1174, :1208`). The rebuild-time helpers
  (`recompute_*_columns`, `:788+`) each do their own `pd.read_parquet` and call
  `clear_store_memo()`, so they never touch the shared object. A **read-only** mapping is safe.

---

## 3. Does pandas 2.3.3 copy? — empirical results

1 000 000-row float64 columns saved as `.npy`, opened with
`np.asarray(np.load(path, mmap_mode="r"))` (read-only, `base` is a `memmap`).

### 3.1 Construction

| construction | shares memory with the map? | resulting blocks |
|---|---|---|
| `pd.DataFrame(dict, copy=False)` | **YES (all cols)** | 3 × `(1, N)` — **not consolidated** |
| `pd.DataFrame(dict)` (default) | no | 1 × `(3, N)` |
| `pd.DataFrame._from_arrays(..., verify_integrity=False)` | **no** | 1 × `(3, N)` |
| `pd.concat([Series(copy=False)…], axis=1)` | **no** (the Series themselves do share) | 1 × `(3, N)` |
| `df[col] = mapped` assignment loop | **no** | 3 × `(1, N)` |
| `pd.DataFrame({c: pd.arrays.NumpyExtensionArray(m)}, copy=False)` | **YES** | 3 × `(1, N)`, dtype still plain `float64` |
| single-column `pd.DataFrame({"a": m}, copy=False)` | **YES** | — |

With `pd.options.mode.copy_on_write = True`: `pd.DataFrame(dict, copy=False)` still **shares**;
`pd.concat(axis=1)` becomes unconsolidated but still **copies**; `_from_arrays` still copies.
**CoW changes nothing that matters here** — the plain `copy=False` dict constructor is already
the answer, in both modes.

> **The one true construction:** `pd.DataFrame({name: mapped_array, ...}, copy=False)`.
> Everything else copies.

### 3.2 Does anything later trigger consolidation?

Built a **42 mapped float64 columns + 2 categorical** frame (44 blocks) and re-checked
`np.shares_memory` for all 42 after each operation:

| operation | blocks after | still shared? |
|---|---:|---|
| `df[df["date"] == d]` | 44 | **yes** |
| `df[df["f0"] >= v]` | 44 | **yes** |
| `df.sort_values("f1")` | 44 | **yes** |
| `df.head(10)` | 44 | **yes** |
| `df["f2"].to_numpy()` | 44 | **yes** |
| `df.set_index("symbol")` | 44 | **yes** |
| `(df["f0"] * df["f1"]) >= 1` | 44 | **yes** |
| `df.groupby("date", observed=True).head(5)` | 44 | **yes** |
| `df[["f0","f1"]].to_numpy()` | 44 | **yes** |
| `df.memory_usage(deep=True)` | 44 | **yes** |
| `df["f3"].isna().mean()` | 44 | **yes** |
| `df.loc[:, "f0"]` | 44 | **yes** |
| **`df._consolidate_inplace()` (explicit)** | **3** | **NO** — RSS +336 MB |

Every operation the screener consumers actually perform leaves the mapped columns shared.
The only observed way to lose the mapping is an explicit `_consolidate_inplace()` /
`.values` / `.to_numpy()` **on the whole frame** — none of which any consumer does. This is
worth a regression test rather than trust.

### 3.3 Accounting

`df.memory_usage(deep=False)` **still reports `nbytes` per column** (24.0 MB for 3 × 1 M float64)
regardless of mapping — it is an accounting figure, not residency. Measuring the win needs
system-available-memory or USS, never `memory_usage` and never child RSS (which counts
faulted-in shared pages).

### 3.4 Categorical from mapped codes

`pd.Categorical.from_codes(codes, categories)` **shares the codes array iff the incoming dtype is
exactly the dtype pandas would choose for that cardinality**:

| categories | mapped code dtype | codes shared? |
|---:|---|---|
| 7 | int32 | no (coerced to int8) |
| 7 | int16 | no (coerced to int8) |
| 7 | int8 | **yes** |
| **4 734** (symbols) | **int16** | **yes** |
| **339** (dates) | **int16** | **yes** |

Confirmed end-to-end on the real store: `np.shares_memory(d2["symbol"].cat.codes.to_numpy(),
mapped_codes)` is `True`. So the derived-array build must pin the code dtype to what the
cardinality implies (`int8` <128, `int16` <32 768) and the reader must assert it — an `int32`
code file would silently copy.

> **Caveat:** the plan's `DerivedArrayStore` contract says *numeric/bool 1-D arrays only, never
> object arrays*. The categorical is built **outside** the store: the store returns the `int16`
> code arrays; the category **strings** must travel as a separate small sidecar (a JSON list —
> 4 734 + 339 + 11 strings, ~60 KB) loaded per process. That is the only extension the metric
> store asks of the mechanism, and it does not violate the no-object rule.

---

## 4. Recommendation — **(c), in the order a → trimmed b**

### Why not (b) alone

Windowing to the run's `[start-warmup, end]` months is real but small and does not compose:
loading `ym=2022*..2024*` (648 of 1 351 files) gives 597 217 rows / **304 MB deep / +279 MB USS** —
a 2.2× cut that is **still fully private per worker** and still ~280 MB × N. It also breaks the
memo: `_STORE_MEMO` is keyed on `store_dir` alone, so a per-run window needs a compound key, and
a worker serving trials with different windows would hold several windows. Mapping (a) is
strictly better per worker *and* per box, and mapping makes windowing unnecessary because
untouched pages are never faulted in at all — a run that only reads 10 of 42 columns and 3 of
6.5 years pays for exactly those pages. **Do not build the windowed loader.**

### Step 1 (the recommendation) — share the store via `DerivedArrayStore`

Add a derived view of the store keyed on the store dir + its partition mtimes/sizes, built by a
`build_fn` that does today's `pd.concat(read_parquet)` **once per box** and emits:

* one `float64` array per numeric column (42 of them — 431.7 MB total, faulted in **per column,
  on demand**);
* `__symbol_codes` (`int16`), `__date_codes` (`int16`), `__sector_codes` (`int8`);
* a sidecar `__categories.json` with the three category lists (~60 KB, loaded privately).

Then a new `load_store(store_dir)` body:

```python
arrs = DerivedArrayStore.build_or_open(key, sources, _build_metric_store_arrays)
cats = json.load(...)                      # sidecar, ~60 KB
df = pd.DataFrame({
    **{c: arrs[c] for c in numeric_cols},
    "symbol": pd.Categorical.from_codes(arrs["__symbol_codes"], pd.Index(cats["symbol"])),
    "date":   pd.Categorical.from_codes(arrs["__date_codes"],   pd.Index(cats["date"]), ordered=True),
    "sector": pd.Categorical.from_codes(arrs["__sector_codes"], pd.Index(cats["sector"])),
}, copy=False)                             # <- copy=False is load-bearing (§3.1)
```

**Nothing else changes.** Every consumer in §2 keeps its exact DataFrame API; the mask chains,
`sort_values`, `groupby().head()` and `set_index("symbol")` all work on categoricals and, per
§3.2, none of them de-share the frame. `scan_dates` gets faster for free
(`df["date"].unique()` on a categorical is the category list).

**Where the changes land**

| file | change |
|---|---|
| `packages/providers/ba2_providers/screener/metric_store.py` (`load_store`, ~766) | rewrite the body as above; keep `_STORE_MEMO` as the per-process handle cache; keep `clear_store_memo()` |
| same file, new `_build_metric_store_arrays(store_dir) -> dict` | the concat + code/category factorisation (already prototyped, 0.8 s) |
| same file, `scan_dates` (~703) | unchanged, but add a categorical fast path |
| `packages/common/ba2_common/core/shared_arrays.py` | no change — codes are plain `int16`/`int8`, within the existing contract |
| `packages/providers/tests/test_screener_metric_store.py` | add: (i) rebuilt frame equals the parquet frame column-for-column; (ii) `np.shares_memory` holds for a numeric column and for `symbol.cat.codes`; (iii) it **still** holds after `screen_universe_for_day` + `screened_symbol_union` + `metrics_as_of` have run (the §3.2 regression); (iv) code dtypes are exactly int16/int16/int8 |

**Behaviour risk:** `symbol`/`date` become `category` dtype. Comparisons (`==`, `<=`, `>=`),
`.unique()`, `.max()`, `isin`, `set_index`, `to_dict("index")` and `list(d["symbol"])` all behave
identically (verified on the real store in §1.6 — same row counts, same results); the `date`
categorical must be constructed **`ordered=True`** or `dates <= as_of_day` raises. `str(d)` in
`scan_dates` and `screened_symbol_union` still works. The one thing to check in review is any
caller doing `df["symbol"].dtype == object` — grep found none.

> **Correction, found while implementing (2026-09-14, commit `20fcfe19`).** `ordered=True` is
> necessary but **not sufficient**: an ordered categorical still refuses `<=` against a scalar that
> is **not one of its categories** (`TypeError: Invalid comparison between dtype=category and
> str`), and the as-of resolve compares against a **bar** date, which on a weekly scan grid is
> almost never a scan date. So `dates[dates <= as_of_day].max()` could not stay as it was —
> `screen_universe_as_of`, `metrics_as_of` and `screened_symbol_union`'s upper bound now go through
> a new `_latest_scan_date_le`, which bisects the categories and then takes the max PRESENT code
> (an int8/int16 pass; a row-filtered frame keeps the full category set, so presence has to be
> checked rather than assumed). `==` against a non-category is fine (all-False), which is why the
> per-day mask needed nothing. `ordered=True` is also load-bearing for
> `tools/strategy_research/runtime.py:110`, where `.min()`/`.max()` on the column would otherwise
> raise. The only other consumer edit was `observed=True` on `screened_symbol_union`'s per-date
> `groupby("date").head(n)` — same rows either way, but it pins the intent and drops pandas'
> deprecation warning.

**Estimated saving:** **~1.0 GB of private RAM per worker** (measured §1.8: 4 087 MB → 242 MB for
4 workers), plus the master process (consumer #6 holds its own copy). On an 8-worker screener
grid that is **~8 GB returned to the box**, on the same order as the option-array work. Bonus:
`load_store` drops from 6.3 s to **0.35 s** per worker, and the object→categorical switch alone
makes the as-of resolve **24×** and the per-date mask **22×** faster (§1.6).

**Implementation cost:** **small — ~half a day.** One function body, one build function, one
sidecar, four tests. No consumer changes, no config, no behaviour genes touched.

### Step 2 (after step 1 lands) — fix the two uncached per-bar consumers

Mapping makes each call ~20× cheaper but does not make consumers #2 and #4 *correct-shaped*:
`metrics_as_of` builds a ~3 800-entry dict of Python floats **per call**, and consumer #2 then
reads **one** key out of it. With the code arrays in hand this becomes a `date_code` lookup plus
one fancy index (§1.6: <0.01 ms):

* `MetricStoreATRProvider` (`seam_wiring.py:258`) — memoise `{(day, col): {sym: val}}` on the
  provider instance, or better, resolve via `symbol_code` + `date_code` directly against the
  mapped `atr_<p>` array. This is the single largest remaining CPU item in a classic-RM screener
  run and it is **not** currently cached at all.
* `FactorRanker._resolve_universe` / `_store_factor_inputs`
  (`FactorRanker/__init__.py:411, 616`) — add the same per-scan-date memo that
  `daily_engine._screened_symbols_for_bar` already has (`self._screened_cache`).

Optional, cheap, and independent: **stop writing `sector`** (or stop loading it) — 80 MB/worker
today, and nothing reads it. Under step 1 it costs only the `int8` code file (1.3 MB) so it is no
longer urgent, but the build should probably drop it.

**Explicitly not recommended:** the per-date `searchsorted` row-range index. It requires
re-sorting the derived arrays by date code (doable at build time) and only buys 0.64 ms → ~0.01 ms
on a path that step 2's memo already reduces to a handful of calls per run. Revisit only if
profiling after step 2 still shows the per-date filter.

---

## Appendix — reproduction

Scripts used (scratchpad, not committed): `measure_store.py` (§1), `copy_probe.py` /
`copy_probe2.py` (§3), `real_store.py` (§1.6–1.7), `mp_test.py` (§1.8).
All run with `C:/Users/basti/ba2-venvs/test/Scripts/python.exe`; peak RSS of the heaviest
(`mp_test.py`, 4 children × parquet) was ~4.3 GB, inside the 10 GB budget.
