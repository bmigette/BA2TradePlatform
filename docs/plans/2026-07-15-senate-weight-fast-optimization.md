# FMPSenateTraderWeight Fast-Optimization Plan

> **For Claude:** implementation tasks here are small and mostly operational; execute
> top-to-bottom. Code tasks (1, 4) follow the usual test-first + commit cadence.

> **Status update (2026-07-19) — partially superseded, see
> [`docs/plans/2026-07-18-senate-basket-dispatch.md`](2026-07-18-senate-basket-dispatch.md):**
> That plan gave `FMPSenateTraderWeight` a **basket-dispatch** mode
> (`instrument_selection_method=expert`, `should_expand_instrument_jobs=False`) — the expert
> now derives its own working symbol set **per bar**, directly from the live Senate/House
> disclosure feed plus a tradable-stock filter, rather than depending on Phase 1's
> periodically-regenerated static `tools/senate_universe.txt`. Practically:
> - **Phase 1 (`tools/build_senate_universe.py` / `tools/senate_universe.txt`) is NOT
>   deleted and still has a job**: OHLCV/FMP-history **PREWARM scoping** (Phases 2-4 below)
>   still benefits from a bounded, pre-vetted symbol list rather than trying to prewarm the
>   disclosure feed's unbounded discovered-symbol set ahead of time. It is simply no longer
>   the expert's *runtime dispatch* mechanism — basket mode does not read
>   `senate_universe.txt` to decide what to score.
> - The tool's ticker-classification helper moved: what was
>   `build_senate_universe.py`'s local `_FUND_TICKER_RE`/`_is_junk_ticker` is now the shared,
>   public `is_tradable_stock_ticker()` in `packages/common/ba2_common/core/utils.py`
>   (senate-basket-dispatch plan, Task 4) — same regex (`^[A-Z]{4,5}X$`), inverted sense
>   (True = keep). `build_senate_universe.py` imports it instead of defining its own copy.
> - Three correctness fixes landed alongside basket dispatch (all outside this plan's
>   original scope, but load-bearing for anyone relying on basket-mode backtests): a
>   lookahead-bias fix so trade-age filters have an upper bound (commit `d20c2a3` — both
>   `FMPSenateTraderWeight` and `FMPSenateTraderCopy` were previously including
>   future-disclosed/future-executed trades); deep-pagination for the unscoped
>   congress-trades feed, which previously only fetched ~4 months of history via
>   `full_history=True` (commit `d8862f1`); and per-symbol OHLCV cache-miss isolation so one
>   un-prewarmed discovered symbol no longer aborts the whole bar (commit `42753ff`). With
>   those in place, basket mode was verified end-to-end at **4.6x real speedup (543s→117s)**
>   on a full 42-month/498-symbol backtest, with identical trade output to the old
>   per-symbol path — this is the actual delivery of the "fast optimization" goal this plan
>   was written for, just via a different mechanism (once-per-bar basket scan) than Phase 3
>   originally assumed (once-per-symbol calls over a fixed universe).
> - **Known, accepted operational gap (not a bug, not yet automated):** because basket mode
>   discovers symbols live from the disclosure feed, it can name a symbol whose OHLCV was
>   never prewarmed. A targeted one-time prewarm covered the gap observed as of this writing
>   (roughly 560 symbols the disclosure feed named across a 2023-2026 window that were
>   missing from the platform's broader ~11,064-symbol OHLCV cache), but this is a
>   point-in-time fix, not a maintained/automatic one — newly-disclosed, never-before-seen
>   symbols will keep needing occasional re-prewarms. See `_gather_all`'s docstring in
>   `packages/experts/ba2_experts/FMPSenateTraderWeight.py` for the mechanics (a missing
>   symbol is now silently skipped for that bar, not a hard failure).

**Goal:** make FMPSenateTraderWeight optimizable like the screener-grid experts — a
resumable GA matrix over a data-appropriate universe, running hermetic (0-fetch)
backtests from pre-warmed caches, distributable to remote workers.

**Why it isn't yet:** (a) no universe — the signal is sparse per symbol, so NDQ30/top-100
by market cap miss most congressional activity; (b) the warm surface is bigger than other
experts (per-symbol trades + per-trader histories + per-skill-symbol price histories);
(c) no matrix driver entry. The 2026-07-15 scoring upgrade (commit b0e2412) already made
the model GA-searchable (15 optimizable expert params) and made the prewarm+hermetic
path complete (all-buy-symbols skill warm, `[]` sentinel for no-data symbols).

**Key facts locked in by prior work:**
- Every senate data path rides `fmp_history_disk_cached` → hermetic BT raises
  `FMPHistoryCacheMiss` on any miss (0-fetch guarantee). Warm once, run forever.
- `historical_price_full` is fetched from 1990 → any as_of back to 2022+ resolves.
- Prewarm runs with `persist_empty_sentinel()` → delisted/no-data symbols cache as `[]`
  and the skill scorer skips them cleanly.
- `CACHE_FOLDER/fmp_history` is inside the cache-sync tree → workers inherit the warm
  cache automatically via `push_cache` (no allowlist to touch).
- Daily interval is the right clock: disclosures lag execution by 30-45 days; 5min adds
  cost, no signal.

---

## Phase 1 — Universe discovery (the actual bottleneck)

### Task 1: `tools/build_senate_universe.py`

**Files:** Create `tools/build_senate_universe.py`, output `tools/senate_universe.txt`.

Derive the universe FROM the disclosure data itself instead of guessing a list:

1. Walk the paginated `stable/senate-latest` + `stable/house-latest` endpoints
   (`page=0..N`, `limit=1000`, via `fmp_http_get`) back until `transactionDate` <
   `--start` minus 90 days slack (or the feed ends). Collect every row.
2. Count disclosures per symbol inside `[--start, --end]`.
3. Keep symbols with `>= --min-disclosures` (default 8 — roughly "tradeable signal
   frequency"; report the count distribution so the threshold can be tuned).
4. Drop non-equity tickers (5-letter `*X` funds, tickers with `.`/`-` suffixes except
   known classes) and anything without FMP daily price data at `--end` (one
   `historical-price-full` probe per candidate, disk-cached — doubles as a warm).
5. Write one symbol per line + print summary (symbols, total disclosures, top-20).

**Verify:** `python tools/build_senate_universe.py --start 2023-01-01 --end 2026-06-30
--min-disclosures 8` produces a plausible list (expect ~150-400 symbols, heavy on
mega-caps + defense + energy). Commit the script AND the generated
`tools/senate_universe.txt` (pin the universe like `options_universe_top100.txt`).

## Phase 2 — Cache warm (one-time, then incremental)

### Task 2: OHLCV parquet warm (engine bars + current-price leg)

```
ba2-test fetch-cache --symbols @tools/senate_universe.txt --timeframes 1d \
    --start 2022-10-01 --end 2026-06-30 --provider fmp --workers 5
```
(90d pre-window buffer covers ATR-14 sizing warmup; senate has no indicator warmup.)

### Task 3: FMP-history warm (trades + traders + skill symbols)

```
ba2-test prewarm --symbols @tools/senate_universe.txt \
    --experts FMPSenateTraderWeight --end 2026-06-30 --workers 5
```
The senate prewarm fetcher (post-b0e2412) warms, per symbol: the senate+house trade
lists, the symbol's full price history, every discovered trader's full history, and the
price history of EVERY unique symbol those traders ever bought (globally deduped).

**Cost estimate (one-time):** ~2 calls/symbol (trades) + ~1/trader (histories; a few
hundred unique traders) + ~1/skill-symbol (price histories; expect 1-3k unique — this is
the big block). All rate-limit-gated by `fmp_http_get`; budget 30-90 min cold. Re-runs
are near-free (disk hits). If `--symbols @file` isn't supported by prewarm yet, add it
(it already is for fetch-options; copy the `@file` idiom).

**Verify hermeticity before any long run:** one single-symbol smoke BT with the hermetic
flag active must complete with ZERO `FMPHistoryCacheMiss`:
```
ba2-test backtest --expert FMPSenateTraderWeight --symbols <top-symbol> \
    --start 2024-01-01 --end 2024-03-01 --interval 1d
```
Then grep the run log for `FMPHistoryCacheMiss` / any FMP HTTP line — both must be absent.

### Task 4 (small code): prewarm `--symbols @file` support *(skip if already present)*

**Files:** `testplatform/ba2test_launcher.py` (prewarm arg parsing).
Mirror fetch-options' `@file` handling; test: `prewarm --symbols @tools/senate_universe.txt --dry-run`-style check or unit test on the arg parser helper.

## Phase 3 — Fast-BT sanity (before burning GA compute)

### Task 5: profile one full-window backtest

Run one full-universe, full-window daily BT and record wall time:
```
ba2-test backtest --expert FMPSenateTraderWeight --symbols @tools/senate_universe.txt \
    --start 2023-01-01 --end 2026-06-30 --interval 1d --run-schedule weekly
```
**Budget:** minutes, not tens of minutes (daily bars, warm caches, in-memory
`_HISTORY_MEM_CACHE`). If the skill scorer shows up hot in a profile (it recomputes per
trader per bar from in-memory maps — normally negligible dict work), add a per-run memo
keyed `(trader, as_of.date())` in `_gather`. **Do not add the memo without profiling
evidence first.**

## Phase 4 — Matrix driver + GA run

### Task 6: `tools/run_senate_matrix.py`

**Files:** Create `tools/run_senate_matrix.py` (mirror `tools/run_options_matrix.py`).

- Jobs: `FMPSenateTraderWeight × {S2, S3, S5, S6}` (S1 is the FMPRating live-ruleset
  replica — not applicable; S7 is an FMPRating refinement). Names `sen-<strategy>[suffix]`,
  idempotent/resumable via `strategy_optimizations.status='completed'`.
- Universe: `tools/senate_universe.txt` (static; no screener).
- Defaults: `--interval 1d`, `--run-schedule weekly` (the schedule:<day> genes search the
  day), `--fitness calmar_ratio`, profit caps on (`--profit-cap-pct 2000
  --profit-share-cap-pct 25`), `--initial-capital 10000`.
- **Population:** the expert now has 15 optimizable params (7 legacy + 8 new) plus
  strategy/cond genes — size like FMPRating's bumped jobs: `--population 60
  --generations 8` default.
- `--workers` passthrough for remote distribution (cache-sync ships `fmp_history`
  + parquet automatically; run `tools/cache_health_check.py` against workers first).

**Verify:** `--dry-run` lists 4 jobs; then run ONE job at `--population 8 --generations 2`
end-to-end before launching the full matrix.

### Task 7: full run + robustness follow-up

- Launch the matrix (sequential jobs; each persists its top-5 as tagged Backtests).
- After winners land: replay the top configs' saved backtests on a worker
  (`/run-trial-full`) to confirm reproducibility, then apply the planned Monte-Carlo
  trade-resample check before trusting single-path calmar ranking (see
  `project-monte-carlo-robustness-followup` note).
- Expect the GA to tell us about the new knobs: `sell_signal_weight` (does ignoring
  sells help?), `skill_signal_weight`/`skill_confidence_weight` (is trader skill real?),
  `min_trade_amount` (do small disclosures add noise?). These answers are the point of
  the run — report them explicitly, not just the fitness winner.

## Risks / notes

- **FMP pagination depth on `-latest`:** if the latest-feeds don't paginate back to
  2022, Task 1 falls back to per-symbol `-trades` fetches over a broad candidate list
  (screener store symbols) — slower discovery (2 calls/symbol × ~5k) but same output.
  Check depth FIRST (one probe run) before choosing the path.
- **Skill-symbol warm size:** unique-buy-symbol count is unbounded in theory; if it
  explodes (>5k), bound per-trader warm to buys executed after `start - horizon - 2y`
  and mirror the same bound in `_calculate_trader_skill`'s candidate selection so warm
  and scorer stay consistent.
- **Live parity:** all new knobs default to legacy-compatible values except the
  consensus bonus (+2/extra trader) — the live instance (currently disabled) will see
  mildly higher confidence when re-enabled; revisit its ruleset thresholds then.
