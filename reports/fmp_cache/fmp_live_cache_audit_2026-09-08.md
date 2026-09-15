# FMP live price-history cache audit

**Snapshot and subsequent changes:** this report records the investigation before
commit `317276ba` on September 8. That separately committed change now shares
screener history by symbol across batches/lookbacks and remembers empty daily
history responses for ten minutes. It addresses the repeated-fetch mechanisms in
findings 2 and 4 below; their original measurements describe the earlier code.
The retained probes assert that earlier behavior and are historical evidence,
not regression tests expected to pass after those fixes. Source line references
also refer to the audit snapshot. No post-fix production bandwidth measurement
has been made as part of this audit.

**Conclusion: the ordinary warm OHLCV cache works, but the platform does not have
one consistent live price-history cache. Several important callers bypass the
disk history, and other caches can retain stale prices. I would not treat roughly
1 GB/day as an acceptable baseline for routine live price refreshes without
fixing and measuring these paths.**

The strongest explanation for the historical dev traffic is the combination of
two Senate experts requesting full histories outside the disk-cache path, a much
larger dev workload, and repeated process/instance recreation. Screener batch
fetches add avoidable traffic. These mechanisms are confirmed in code and mocked
reproductions; their exact shares of the daily bandwidth are **not measured**.

This is an audit, not a deployed fix. No production settings, real price caches
or application code were changed for this investigation. No real FMP requests
were made. All new files are under `reports/fmp_cache/`.

## Evidence and attribution limits

The supplied screenshot shows **20.21 GB used out of 70 GB over 30 days**, with
`All Keys` selected and large daily bars attributed to `/v3/historical-price-full`.
It supports the reported symptom, but does not distinguish live dev, live prod,
backtest warmups or other processes sharing those keys.

I checked the desktop `ba2.bat`, editable-package paths, current source, relevant
Git history, retained logs, cache files, current dev/prod databases and the dev
backup taken before its September 4 reset. Database connections were read-only.

| Instance/snapshot | Observed workload or cache |
|---|---|
| Dev before reset | 26 expert records, 25 enabled: 8 Rating, 6 FactorRanker, 5 EarningsDrift, 4 Insider and **2 Senate** |
| Dev now | No expert records |
| Prod now | 8 expert records, 6 enabled: 2 DeterministicScorer, 2 EarningsDrift, 1 Insider, 1 Rating |
| Dev/shared OHLCV cache | 26,373 Parquet files, approximately 17.01 GB; also 986 legacy CSV files |
| Dev/shared Senate full-history JSON | 4,002 files, approximately **4.68 GB**, mostly last written July 27 |
| Prod OHLCV cache | 49 Parquet files, approximately 4.01 MB; also 338 legacy CSV files |

Enabled records are configuration evidence, not a count of simultaneous workers.
Analysis records independently show dev performing 99, 97 and 95 analyses on
September 1–3, versus four per day in prod. Dev's Senate experts were active on
each of those days. Their configured disclosure and execution windows were 270
and 300 days, with a six-month trader-skill lookback.

The launcher gives dev the shared `ba2/common/cache` folder and prod its separate
`ba2_trade_platform-prod/cache` folder. This is intentional separation, but warming
the shared folder does not warm prod. The installed packages resolve to this
repository, rather than the archived sibling repositories.

Retained logs are incomplete, sometimes overlap between sinks, and lack a
consistent process ID and response-byte counter. Counts below deduplicate exact
log lines and describe **observed events**, not a complete HTTP billing ledger:

- August 31 dev: 22 completed screens and 42 logical bulk-history operations,
  covering 9,837 symbol/window requests. Same-day prod: two completed screens and
  1,324 symbol/window requests. A bulk-operation log can represent a memory hit,
  so these totals must not be called actual HTTP request counts.
- The retained provider-level dev trace on August 31 shows eight daily fetch
  invocations, all shorter than a deep-history fill. That trace does **not** cover
  the Senate and screener direct HTTP paths.
- Prod September 7: 21 repeated SPY fetch invocations for the same September
  5–8 window; September 8: 13 for September 5–9. This corroborates redundant tail
  refreshes, although the small windows do not explain gigabytes of full history.
- Dev logs contain startup and Senate-instance recreation events during the
  high-usage period. The older Senate memo was lost when an instance was recreated.

Evidence: [database snapshots](db_evidence_2026-09-08.json),
[log aggregates and source locations](logs_evidence_2026-09-08.json),
[cache inventory](cache_evidence_2026-09-08.json).

## Findings

### 1. Senate live price lookups ignore both warmed history stores

**Priority: high for the historical bandwidth investigation. Confirmed mechanism;
likely contributor, not an exact attribution of 1 GB/day.**

[FMPSenateTraderWeight](../../packages/experts/ba2_experts/FMPSenateTraderWeight.py)
lines 1430–1487 obtains execution prices through `historical-price-full`, with
`from=1990-01-01`. It does not consult the ordinary OHLCV Parquet store.
Its call to `fmp_history_disk_cached` also bypasses JSON disk files in live mode:
[fmp_common.py](../../packages/providers/ba2_providers/fmp_common.py), lines 256–281,
returns `fetch_fn()` when the backtest freeze flag is off.

The helper therefore sounds more broadly cached than it actually is. Existing
multi-gigabyte history files do not protect live Senate execution-price lookups.
These lookups can cover symbols in congressional trade/skill histories, beyond
the small set of positions the expert ultimately holds.

Before commit `eb4d4c71` on **September 5**, the price map belonged to each expert
instance. I executed that historical method with two fresh instances and a warmed
disk cache: it made **two full-history fetches** for the same symbol. Repeated
lookups within one surviving instance were cached; it was not necessarily a
download on every analysis. Restarting/recreating instances lost that saving.

Current code shares the price map across instances, reducing duplication within
one process. However, it still loses the map on restart and has **no refresh
policy**: a subsequently available trade date can remain absent. A reproduction
returned `None` for the newly published date without asking the provider again.

**Recommended fix:** reuse durable per-symbol daily history for these lookups,
preserving the required **open** price rather than substituting a close. Fetch
missing historical ranges or a small overlapping tail; refresh the live memo's
right edge. Do not enable backtest freeze globally in the live app as a shortcut.

### 2. The screener has a separate, coarse batch cache

**Priority: high for reducing avoidable live traffic. Confirmed.**

[StockScreener.py](../../packages/providers/ba2_providers/StockScreener.py),
lines 390–488, calls the history endpoint directly. Its six-hour memory-cache key
includes the **ordered batch of tickers and exact from/to dates**. It neither
reads nor populates the ordinary per-symbol Parquet cache.

Identical batches within the TTL are reused correctly. Reordered or overlapping
batches, different lookbacks, a new date, TTL expiry or restart fetch again.
The pipeline can request roughly 35 calendar days for volume/RVOL, 255 for the
Weinstein filter, and separate price-drop windows for overlapping symbols.

Reproduction with prewarmed Parquet: four logical calls produced three HTTP calls
when one repeated the exact batch, another reversed its order, and another changed
the lookback. Only the exact repeat was reused.

**Recommended fix:** obtain screener history from a per-symbol range-aware cache;
share overlapping history across screeners and indicators. Preserve the completed
session rules for volume/RVOL. Merely extending the six-hour TTL does not solve
batch fragmentation or restart downloads.

### 3. Simultaneous cache misses are not coalesced

**Priority: medium. Confirmed traffic multiplier.**

[TTLCache.get_or_call](../../packages/providers/ba2_providers/fmp_common.py),
lines 399–408, releases its lock before fetching. Six simultaneous callers missing
one key all execute the same fetch; the audit reproduced **six fetches for one
payload**. The lock protects the dictionary, not the in-flight request.

**Recommended fix:** coordinate one fetch per key and let other callers await its
result. Preserve exception propagation and remove failed in-flight entries so a
later attempt can recover. This coordinates threads; sharing across processes
requires a separate design.

### 4. Empty daily refreshes can repeat indefinitely

**Priority: medium for request volume; usually small payloads. Confirmed.**

[MarketDataProviderInterface.py](../../packages/common/ba2_common/core/interfaces/MarketDataProviderInterface.py),
lines 525–594 and 850–868, decides freshness from the last bar and file mtime.
When a weekend/holiday refresh returns no new rows, it records no successful
check and does not update the file. The next read attempts the same refresh.
[FMPOHLCVProvider](../../packages/providers/ba2_providers/ohlcv/FMPOHLCVProvider.py)
also retries empty daily responses three times.

The mocked weekend case produced **nine HTTP calls from three reads**. Repeated
SPY tail windows in the actual prod logs are consistent with this path.

**Recommended fix:** use the last completed exchange session and track the last
attempt/check separately from price-file mtime. Apply a bounded retry delay for
empty responses and failures; never manufacture a candle or cache a transient
failure as permanent absence.

### 5. DeterministicScorer can keep stale daily prices indefinitely

**Priority: high for trading correctness. Confirmed; two enabled prod instances.**

[DeterministicScorer/data.py](../../packages/experts/ba2_experts/DeterministicScorer/data.py),
lines 87–119, reuses `_OHLCV_COVERAGE` whenever the requested **start** is not
earlier than its cached start. It does not check expiry or whether the cached
**end** reaches the new live date. The declared `_OHLCV_CACHE` TTL does not govern
these frames.

Two live reads two days apart made one provider call; the second returned the
first day's bar. The live `/api/reload` route clears expert instances, not this
module-level map, so its reset is not guaranteed by reloading settings.

**Recommended fix:** give live coverage a refresh time and right-edge/session
requirement. Retain immutable historical behavior for backtests. This fix may
increase the number of legitimate tiny tail refreshes while correcting signals.

### 6. A partial daily candle can become permanent history

**Priority: high for trading correctness. Confirmed with synthetic candles.**

The shared top-up begins at `last_bar + interval`. A partial daily candle fetched
during market hours makes the file fresh for 24 hours. A later refresh begins on
the **following day**, so it never requests that earlier candle's final OHLCV.
The refresh merge also keeps the earlier row on duplicate timestamps.

The reproduction cached a morning close, served it that evening, and retained it
after the following day's refresh. Fewer requests here do not establish a correct
cache.

**Recommended fix:** distinguish partial and finalized candles, overlap the tail
by at least the last possibly incomplete session, and replace overlapping rows
with finalized provider values. Keep entry-time signal access causal.

### 7. Legacy stores and aliases still create inconsistent reads

**Priority: medium; secondary to the daily-bandwidth explanation. Confirmed.**

The older `get_data()` method still uses a separate CSV/mtime cache. A warmed
Parquet file did not prevent a fetch of **1,125 calendar days** for a 30-day request
in the reproduction. The Smart Risk Manager has a `get_data()` call site; this
audit does not establish that it caused the observed historical traffic.

The shared cache also contains **36 `_5m`/`_5min` pairs**. Readers prefer `_5m`.
For AMC, a 596-row June 2026 short file shadows a 126,618-row file beginning in
January 2020. PLUG and SOUN have the same pattern. The August 24 code fix prevents
some new alias mistakes but did not repair these existing pairs.

**Recommended fix:** make both public OHLCV APIs use the same store. Audit and
merge existing alias pairs with explicit timestamp/conflict handling before
retiring a duplicate. Do not delete the short files blindly. Prod's separate
cache and legacy CSVs also explain some legitimate first Parquet fills.

Evidence: [alias samples](alias_samples_2026-09-08.json).

## What already works, and what to do first

The ordinary fresh daily Parquet path made zero HTTP calls for three repeated
reads. The **47 existing cache/FMP tests pass**, including historical as-of reuse,
the earlier empty-slice fix, merge/alias protections, freshness and HTTP retry
behavior. Their passing result does not cover the nine additional audit scenarios
above. [Validation record](validation_2026-09-08.txt),
[reproductions and measured call counts](reproductions_2026-09-08.json).

I would prioritize the work in this order:

1. Fix live price correctness: DeterministicScorer refresh coverage, finalized
   daily-tail updates, and the Senate memo's missing-date refresh.
2. Route Senate and screener price history through durable per-symbol caching;
   deduplicate simultaneous fetches and avoid empty-session retry loops.
3. Consolidate legacy cache readers and repair the documented alias conflicts.
4. Add per-instance/caller/endpoint request and response-size metrics, cache hit,
   miss and refresh reasons, and requested date spans. Redact API keys in errors;
   some existing raw request exceptions contain them. Do not log response bodies.
5. Verify in paper/dev with a warmed cache across a market session, an overnight
   transition, a weekend and a restart. Measure actual daily bytes before setting
   a budget; the available evidence cannot support a precise savings percentage.

The audit collectors and reproductions are retained beside this report. They
query existing data or use temporary stores and fake HTTP responses; they do not
start the live app, backtests or cache warmup jobs.
