# Alpaca History Source Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Backtests and GA read daily + 5-minute history from an Alpaca-derived, split-verified cache selected by ONE
setting (`fmp` | `alpaca`), shared by the test platform and (later) the live app; the FMP OHLCV cache is archived and
no longer replicated.

**Architecture:** The raw Alpaca store (`~/Documents/ba2/alpaca_store`, raw SIP bars + corporate actions, asof
2026-10-12) is turned into a native-layout cache folder `CACHE_FOLDER/AlpacaHistoryOHLCVProvider/` (same file names
and columns as `FMPOHLCVProvider/`, so every reader works unchanged once it is pointed there). Prices are
split-adjusted with OUR verified split table (Alpaca actions accepted only when the raw overnight jump proves them;
FMP splits as second opinion; disagreements listed for owner review). One resolver in `ba2_common` maps the setting
to the provider class / folder; every hard-coded FMP history site calls it. A job stamps its source in its config
(and so in its name digest); stored rows without a stamp ran on FMP.

**Tech Stack:** Python 3.11, pandas/pyarrow parquet, the existing native cache layer (`ba2_common.core.native_cache`),
`ba2_providers` provider registry, `tools/alpaca_history_download.py` (branch `feat/alpaca-history-store`).

**Owner decisions (2026-10-20):** backtests move to Alpaca history; live keeps FMP / account for CURRENT prices
(Alpaca's free SIP is delayed) but its HISTORY reads become switchable too (Phase 2); split adjustment = own verified
table; the remote227 stage-1 option grid finishes on FMP; archive the FMP cache so it is not replicated; rebuild the
derived stores; then the ED-11 job (opt 556) relaunches on Alpaca data.

**Semantics fixed by this plan**
- Adjustment = splits, reverse splits, unit splits and stock dividends (back-adjusted to the store's asof basis),
  AND spin-offs (owner 2026-10-20: the engine does not credit spun-off shares, so an unadjusted parent shows a fake
  loss; FMP adjusts many spin-offs too): factor 1 + rate x spinco close / parent close on the ex-date, prices only,
  volume unchanged. Cash dividends are NOT adjusted. Series breaks (reused tickers, bankruptcies): only the segment
  after the last break is kept.
- 5-minute files hold the regular session only (bar starts 09:30..15:55 America/New_York), NY-naive `Date`,
  `effective_date = Date`, like the FMP files. Daily files: NY-naive midnight `Date`.
- Volume is multiplied by the same factor the prices are divided by.
- A symbol-year listed in the store manifest's `days_without_intraday_bars` keeps those days empty (never filled).
- History starts 2016-01-01 for daily bars (a 2020 job preloads from ~2018-12), 2019-01-01 for 5-minute bars.
- Option backtests with `alpaca` REFUSE until their split-basis resolver is backed by the verified table (Task 12):
  ThetaData strikes are as-traded and today's converter reads FMP's split calendar.

---

## Phase 0 -- data completeness (branch `feat/alpaca-history-store`, worktree `C:\wtsuite\alpaca-store`)

### Task 0: Commit the pending downloader change
**Files:** `tools/alpaca_history_download.py`, `tests/test_alpaca_history_download.py` (uncommitted `--max-gap-days`).
1. `git -C C:/wtsuite/alpaca-store diff` -- read it; it must only be the `--max-gap-days` rule (memory: required since
   2026-10-15, owner value 5).
2. Run `.venv\Scripts\python.exe -m pytest tests/test_alpaca_history_download.py -q` (app venv, not the test venv). PASS.
3. Commit `feat(tools): --max-gap-days (store a symbol-year with <= N unexplained gap days, list them)`.

> **DONE DIFFERENTLY (2026-10-20):** Tasks 1-2 were replaced by a SECOND store with no code change:
> `~/Documents/ba2/alpaca_store_daily2016` = `--start 2016-01-01 --end 2026-10-13 --asof 2026-10-12
> --phases corporate_actions,daily` over 11,621 symbols (the 7,048 of the main store + 4,573 US daily-only tickers
> from the FMP cache; list in its `symbols.txt`). The builder (Task 4) reads DAILY bars and corporate actions from
> that store and 5-MINUTE bars from `alpaca_store` (same asof, so the same ticker mapping). Task 0 is still owed
> before the downloader is merged.

### Task 1 (superseded): Extend the store's start backwards (daily + corporate actions only)
**Files:** Modify `tools/alpaca_history_download.py` (STORE_INFO check, daily window); Test
`tests/test_alpaca_history_download.py`.
1. Failing test: a store created with start 2019-01-01, run again with `--start 2016-01-01 --extend-start
   --phases corporate_actions,daily` -> STORE_INFO.start becomes 2016-01-01 with a `start_history` list
   `[{"start": "2019-01-01", "until": <now>}]`; the daily chunk is re-fetched over the new window; intraday chunks are
   untouched; without `--extend-start` an earlier `--start` still REFUSES (store mismatch, as today).
2. Implement: `--extend-start` flag; only allowed with phases subset of `{corporate_actions, daily}`; rewrite
   STORE_INFO atomically (temp + os.replace) BEFORE fetching; the daily window key changes so the existing resume rule
   re-fetches it.
3. Tests pass; commit `feat(tools): --extend-start moves a store's daily/corporate-action start backwards`.

### Task 2: Run the extension + the daily-only symbols (operations)
1. Symbols file A = `alpaca_store/symbols.txt` (7,048).
   Symbols file B = US-style tickers that have `FMPOHLCVProvider/<S>_1d.parquet` but are NOT in A (no `.`-suffix
   exchange codes, no `^`): write `alpaca_store/symbols_daily_only.txt`.
2. Run (resume-safe, ~2 h at 150 rpm):
   `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -u C:\wtsuite\alpaca-store\tools\alpaca_history_download.py
   --store C:\Users\basti\Documents\ba2\alpaca_store --symbols-file <A> --start 2016-01-01 --extend-start
   --end 2026-10-13 --phases corporate_actions,daily --keys-db C:\Users\basti\Documents\ba2\trade\db.sqlite
   --account-id 1 --rpm 150 --min-remaining 60 --max-gap-days 5`, then the same with `--symbols-file <B>
   --phases daily`.
3. Check `state/progress.json`: `failed == 0`. Record counts in the plan's run log (bottom of this file).

## Phase 1 -- code (worktree `C:\wtsuite\alpaca-hist`, branch `feat/alpaca-history-source` from `origin/dev`, then
`git merge feat/alpaca-history-store`)

### Task 3: Verified split table (pure function + tool)
**Files:**
- Create `packages/providers/ba2_providers/ohlcv/alpaca_history/__init__.py`, `.../split_table.py`
- Create `packages/providers/ba2_providers/ohlcv/alpaca_history/split_overrides.json` (reviewed entries; same rules as
  `panel_exclusions.json`: symbol, event_date, ratio, decision accept|reject, reason, added, reviewed_by; a
  `pending` reviewer REFUSES the build)
- Create `tools/build_alpaca_split_table.py`
- Test `packages/providers/tests/test_alpaca_split_table.py`

Rule (`decide_splits(raw_daily, alpaca_events, fmp_events, overrides) -> (accepted, review_rows)`), per candidate
event (date D, factor f = new shares / old shares; reverse split f < 1):
- `jump = raw_close(D-1) / raw_open(D)`; PROVEN when `|ln(jump) - ln(f)| <= ln(1.15)` and `|ln f| >= ln(1.25)`.
- PROVEN -> accepted (whatever the source). Alpaca-only and contradicted (jump within ln(1.10) of 1 while
  `|ln f| >= ln(1.25)`) -> rejected + review row. FMP-only and proven -> accepted + review row ("Alpaca missed").
- `|ln f| < ln(1.25)` (stock dividends, small unit splits): not provable by price; accepted when BOTH vendors list it
  (ratio within 1%), else review row and NOT applied.
- An unexplained raw overnight jump > x1.8 or < x0.55 with no event within 3 sessions -> review row (possible
  missing split).
- An override entry decides its event outright (accept with its ratio / reject).
Steps: failing tests for each branch above (synthetic frames) -> implement -> pass -> tool writes
`CACHE_FOLDER/AlpacaHistoryOHLCVProvider/_meta/split_table.json` (`{symbol: [[date, factor], ...]}` + store asof +
sha256 of inputs) and `_meta/split_review.csv` -> commit.

> **Owner rule (2026-10-20): one pipeline, one runbook.** Every step is a `--steps` step of ONE CLI,
> `tools/prepare_history.py --source {alpaca,fmp}` (alpaca: `download`, `split-table`, `build`, `verify`;
> fmp: `download`, `verify`; pattern
> `tools/prepare_option_universe.py`), documented in `docs/REPRODUCE-BACKTESTS.md` §4.0. No standalone tools, no
> separate runbook, no scratchpad-only measurement: the 2026-10-20 FMP-vs-Alpaca comparison
> (`compare_fmp_alpaca.py`) becomes the `verify` step.

### Task 4: Cache builder (pipeline steps `download`, `build`, `verify`)
**Files:** Create `.../alpaca_history/build.py`, `.../alpaca_history/verify.py`; extend `tools/prepare_history.py`;
Test `packages/providers/tests/test_alpaca_history_build.py`.
- `adjust(frame, events)`: cumulative factor F(t) = product of f over events with date > t; prices / F, volume * F.
- `to_native_daily(store_daily)` / `to_native_5min(store_5min_years, manifest)`: UTC -> America/New_York naive;
  5-min keeps 09:30 <= start <= 15:55; columns `Date, Open, High, Low, Close, Volume, effective_date`.
- Writes `<SYM>_1d.parquet`, `<SYM>_5min.parquet` atomically; `_meta/build_manifest.json` = store asof, split-table
  sha256, per-symbol first/last date + bar counts + carried `days_without_intraday_bars`.
- Tests: a 2:1 split halves pre-split prices and doubles volume; a reverse 1:10 multiplies by 10; RTH filter; DST
  boundary (March/November) keeps 78 bars a day; a listed gap day stays empty; output columns/dtypes equal an FMP
  file's (`pd.read_parquet` of a fixture).
- Commit.

### Task 5: Provider class + registry key
**Files:** Create `packages/providers/ba2_providers/ohlcv/AlpacaHistoryOHLCVProvider.py`; Modify
`packages/providers/ba2_providers/__init__.py:76` (registry `"alpaca_hist"`); Test
`packages/providers/tests/test_alpaca_history_provider.py`.
- Subclass of the same cache base as `FMPOHLCVProvider`; constructible with NO keys; reads served by the base cache
  layer from `AlpacaHistoryOHLCVProvider/`; `_get_ohlcv_data_impl` raises `HistoryTopUpUnavailable` (named, loud) --
  Phase 2 replaces it. No `_split_calendar`, `WRITES_FULL_FETCH_MARKER` stays False (the basis is the build's).
- Test: a cache-only read through `MemoizedOHLCVProvider(..., cached_only=True)` returns the fixture bars; a read
  beyond the cache raises `BacktestCacheMiss`, never a network call.

### Task 6: One resolver
**Files:** Create `packages/common/ba2_common/core/ohlcv_history.py`; Test `packages/common/tests/test_ohlcv_history.py`.
```python
HISTORY_SOURCES = {"fmp": ("fmp", "FMPOHLCVProvider"), "alpaca": ("alpaca_hist", "AlpacaHistoryOHLCVProvider")}
DEFAULT_BT_HISTORY_SOURCE = "alpaca"          # what a NEW job is stamped with
LEGACY_HISTORY_SOURCE = "fmp"                  # a stored row without a stamp ran on FMP

def resolve_job_history_source(job_config: dict) -> str: ...   # stamp, else LEGACY; unknown value -> ValueError
def history_registry_key(source: str) -> str: ...
def history_cache_dirname(source: str) -> str: ...
def history_provider(source: str): ...         # get_provider("ohlcv", history_registry_key(source))
```
Tests: unknown source refuses; missing stamp -> "fmp"; round trip of both names.

### Task 7: Wire the backtest switch points
**Files (from the 2026-10-20 code map):** `testplatform/backend/app/services/backtest/daily_backtest_handler.py:713,957`;
`price_source.py:1030-1046` (error text names the job's source); `packages/common/ba2_common/core/backtest_context.py:84`;
`packages/common/ba2_common/core/seam_wiring.py:731` default; `results.py:390`; `rerun_handler.py:128`; `whatif.py:60`;
`backtest_trade_chart.py:48`; `packages/experts/ba2_experts/.../FactorRanker/data.py:180,237` (fallback through the
resolver); `testplatform/ba2test_launcher.py` (`--ohlcv-history-source {fmp,alpaca}` default
`DEFAULT_BT_HISTORY_SOURCE`, stamped as `ohlcv_history_source` in the job config AND in the job-name digest; lines
`:567,:645,:5518,:7103` and the coverage preflight `:5521/:7191`); `tools/check_window_coverage.py:52,70`
(`--source`).
- Option jobs (`fetch_options.py:256,469`, `parquet_contract_detail.py:249`, `option_split_basis.py`) with source
  `alpaca` raise `OptionHistorySourceUnsupported` at job start (until Task 12).
- Tests: a trial built from a job stamped `alpaca` reads `AlpacaHistoryOHLCVProvider/`; an unstamped stored row re-runs
  on FMP; the job name differs between the two sources; an option job stamped `alpaca` refuses before any trial.

### Task 8: Derived builders follow the source
**Files:** `packages/providers/ba2_providers/screener/live_sim_build.py:403,635`, `universe_superset.py:288`,
`live_parity.py:168`, `FMPHistoricalScreenerProvider.py:75` (OHLCV part only), metric-store build in the launcher
(`build-screener-metrics --ohlcv-history-source`), `prewarm --screener-panel --ohlcv-history-source`;
`packages/common/ba2_common/core/market_condition_source.py:83-86,631` + `market_condition_readers.py:310` +
`packages/providers/ba2_providers/market_conditions/fmp_source.py:46` (source profile
`alpaca-daily-split-verified-v1` when `alpaca`, so stored feature keys never mix).
- The panel/metric-store manifests record the history source; a gate/job whose source differs from the panel's
  REFUSES (`ScreenerGateRefusal`), as a panel fingerprint mismatch does today.
- Tests per builder: the source lands in the manifest; a mismatch refuses.

### Task 9: Cache push replicates only the selected history folder
**Files:** `testplatform/backend/app/services/cache_sync.py:81,120-160,479`; Test the existing cache_sync tests file.
- `build_manifest(..., exclude_dirnames=())`; the push of a job passes the OTHER source's folder
  (`FMPOHLCVProvider` for an `alpaca` job): it is neither copied NOR pruned on the worker (owner 2026-10-20: option
  jobs on shared workers still read FMP until Task 12).
- Test: an `alpaca` push manifest has no `FMPOHLCVProvider/` entry and prunes the worker's copy.

### Task 10: Versions, guard, full suites
- Bump `ba2_common`, `ba2_providers`, `ba2_experts` `PACKAGE_VERSION` (+ pyproject) and raise all three minimums;
  bump `TEST_APP_VERSION`; `APP_VERSION` if Phase 2 lands in the same push.
- `python tools/check_package_versions.py --base origin/dev --include-worktree` -> OK.
- Suites, one at a time, each from its own package dir (conftest clash): common, providers, experts, backend.
  Known pre-existing failures to ignore (2026-10-19 run): common `test_bool_setting_round_trip` x2,
  `test_option_risk_manager_seam` (+3 more of the 6), providers `test_metric_store_read_staleness` x2,
  `test_split_basis_refetch` (+1). Anything else is ours.
- Code review subagent on the branch diff; fix; merge to dev; push only when no master that shares workers is mid-job
  (remote227 stage 1 runs a pinned clone on its own slots and is not affected).

## Phase 2 -- live price + history sources (separate pushes, after the Phase 3 hand-off)

Owner order (2026-10-20): Phase 1 + Phase 3 first (ED-11 on Alpaca data), then Task 13 (FMP quotes), then Task 11.

### Task 13: FMP real-time quote as the account's current-price source (DEFAULT)
Measured 2026-10-20: Alpaca `delayed_sip` latest trade AND quote are 15.0 min old; the free key refuses `sip`; the
15-min move at a 09:30/10:00 decision is median 0.42%, p99 ~5%. The backtest prices at the true T.
- Alpaca account price-source setting gains `fmp` and it becomes the DECLARED DEFAULT; settings UI shows it in the
  "market data feed" dropdown (help text: fmp = real-time FMP `/quote`; delayed_sip = 15 min late; iex = real-time
  IEX-only prints, free; sip = paid). Wired in `AlpacaAccount._get_instrument_current_price_impl` (the only
  `data_feed` reader): one batched FMP `/quote` call per symbol batch; FMP failure -> no price (named error / None per
  the existing contract), NEVER a fallback to an Alpaca feed. Bid/ask callers (`price_type`): use FMP bid/ask when
  the quote carries them, else refuse loudly and list the callers in the PR.
- Option quotes keep `options_feed`. PennyMomentumTrader's `vendor_live_price` fallback to account prices is removed
  (no silent fallback).
- Ops: switch the EXISTING accounts on dev, prod and options to `fmp` (state the prod effect: real-time decision
  prices and TP/SL checks), then `/api/reload`.

**Done 2026-10-24, branch feat/fmp-price-feed** (APP 2026.09.1249, ba2_experts 2026.10.00006): `fmp` default in AlpacaAccount via `core/fmp_live_price.py` (fmpsdk.quote, 100/batch, no cache); FMP failure -> None, never Alpaca; unknown data_feed raises. FMP /quote has no bid/ask: explicit `ask` is refused (ValueError); `bid` is the interface DEFAULT price_type ("the price"), so it returns the last price. Option quotes untouched (`options_feed`). PennyMomentumTrader: missing FMP key -> ERROR + no prices (account fallback removed; `vendor_live_price=account` stays an explicit choice). Ops step (switch existing accounts + `/api/reload`) still OWED.

### Task 11 (amended): per-account HISTORY source, separate from the price source

### Task 11: Live ACCOUNT setting + top-up
- ACCOUNT setting `ohlcv_history_source` (`fmp` | `alpaca`, declared default `fmp`, owner 2026-10-20: an account
  setting, not a flag) on every account type (`get_settings_definitions` of the account interface, so Alpaca,
  TastyTrade and IBKR all carry it), resolved through `ba2_common.core.ohlcv_history` from the account of the
  expert / transaction doing the read. Account-bound live history sites: `ba2_trade_platform/core/TradeManager.py:279`,
  `modules/dataproviders/options/atm_iv_history.py:545,606`, the expert provider bundle (indicators/ATR history).
  Account-less UI pages (`ui/pages/symbol360.py:113,122`, `ui/components/option_structure_chart.py:385`,
  `modules/experts/TradingAgentsUI.py:99`) stay on FMP. Current prices are NOT touched (account quote).
- `AlpacaHistoryOHLCVProvider._get_ohlcv_data_impl`: fetch RAW finished sessions after the cache end with the
  account's market-data keys, apply the split table, append; a daily corporate-actions refresh adds new splits
  through the same `decide_splits` rule and re-bases the affected symbols' files (never a silent basis mix:
  `effective` basis date recorded per file).
- Until this task ships, an account set to `alpaca` REFUSES at startup (named error), so it cannot half-work.
- Delay: the free Alpaca plan only blocks SIP data from the last 15 minutes; the top-up fetches FINISHED sessions
  only, so the delay never touches history.

### Task 12: Option split basis from the verified table
- `split_basis.resolve_symbol_split_basis` gains a table-backed path for `alpaca` (factor = product of accepted events
  after the day), which removes the FMP-calendar overrides for that source; then lift the Task 7 refusal.
- DONE (branch `feat/option-basis-alpaca`, TEST 2026.09.0144; no `packages/` change). Implemented in
  `testplatform/backend/app/services/backtest/option_split_basis.py::build_alpaca_run_split_basis`
  (`build_run_split_basis(..., history_source="alpaca", cache_dir=...)`), not in `split_basis.py`: the FMP proof machinery does
  not apply. factor(D) = product of the factors of EVERY event the build applied dated after D, splits AND spin-offs
  (`build.adjust` divides prices by split F x spin-off F), read from `build_manifest.json` `symbols[S].events` and
  cross-checked against `split_table.json` (`build.load_split_table`; sha/run_id must match the manifest). Zero events in the
  manifest = proven 1; symbol missing from manifest / no daily file / table disagreeing / uncertified cache = REFUSE (all named).
  The digest carries table run_id + sha and the certified manifest sha. `OPTION_SUPPORTED_SOURCES = (fmp, alpaca)`; the default
  source of a NEW option job stays fmp. Also followed the source: `parquet_contract_detail` (stored run's source), trade chart
  `as_traded_bars`. Unchanged and still FMP by design: the dividend calendar (`fmp_history/mc_stock_split__` /
  `mc_dividend__`; `adjDividend` x factor, equal to the as-declared amount wherever FMP's and our table's events agree).
  `tools/option_universe_preflight.py` is an FMP-basis preflight and was not ported.

## Phase 3 -- build, archive, hand-off (operations, after Phase 1 is merged)

1. Build the split table; send `_meta/split_review.csv` to the owner; add the decisions to `split_overrides.json`
   (reviewed); rebuild until no `pending` row. **Owner review gate.**
2. Build the cache (all store symbols). Verify: coverage per symbol-year (expect ~96.5% of symbol-days, the
   2026-10-20 measurement), 5-min last close vs daily close on the same basis (<= 1% of days off by > 2%), and a
   spot check vs FMP on symbols FMP has right (ratio 1.000 after adjustment for >= 90% of symbol-years).
3. Archive: DEFERRED until Task 12 ships (owner 2026-10-20: option backtests still read FMP). Then, with no local
   master running, move `~/Documents/ba2/common/cache/FMPOHLCVProvider` -> `~/Documents/ba2/archive/FMPOHLCVProvider-<date>`.
   Until then no new equity job reads the FMP folder.
   Live apps re-fetch FMP on demand into a fresh folder, which Task 9 keeps out of `alpaca` pushes.
4. Rebuild with `--ohlcv-history-source alpaca`: the screener daily panel, a NEW metric store
   (`metric_store_alpaca_20261020`, the live store is not swapped here), market-condition stores; `_derived` shared
   arrays rebuild on first touch.
5. Hand-off to `grid2020-remote227`: dev SHA; pull `BA2-atr27-run`; launcher `--ohlcv-history-source alpaca`
   (default) and `--store <new metric store>`; push the cache to remote150 (prunes its FMP copy); expect a NEW job
   name (the source is in the digest); preflight coverage must pass; TRLV/OGC simply have no pre-2026 history in this
   source (no exclusion needed); BBBY is excluded (04a11f53).

## Phase 4 -- one runbook, few pipelines (after the Phase 3 hand-off; audit 2026-10-20)

Owner rule: every backtest-preparation step is reproducible from `docs/REPRODUCE-BACKTESTS.md` and a few `--steps`
pipelines. The 2026-10-20 audit found 35 prep scripts/commands (11 documented, 5 partly, 19 orphaned) and 9 other docs
with prep steps.
1. `tools/prepare_history.py --source fmp`: the FMP `download` (fetch-cache with recorded arguments) and `verify`
   steps, plus the daily-bar hygiene checks (partial / provisional / unfinished bars) as a `hygiene` step.
2. `tools/prepare_equity_data.py --steps prewarm,fred,screener-store,screener-panel,coverage-check`: wraps the existing
   `ba2-test` subcommands and tools (no logic rewritten), includes the Senate JSONL conversion and the FMP/FRED
   `first_release_v1` format check.
3. Merge `data/stage2-universe-repair` (31 commits, unpushed, 4 conflicts against dev as of 2026-10-20:
   `MarketDataProviderInterface.py`, `split_basis_overrides.py`, `parquet_options_provider.py`,
   `warm_options_history.py`); commit the 753-name stage-2 universe and an exclusion list with a reason per name; add
   `fred-rate`, `shared-arrays` (twice), `prepare-host` steps to `prepare_option_universe.py`. Required before stage 2.
4. Fold the other docs into REPRODUCE-BACKTESTS.md (goal2020 runbook data sections, option data runbook, screener fix
   note), add the cache-push prune rule, and a runs-of-record table generated from each pipeline's report JSON.
5. Delete dead scripts in a separate commit after a grep of CI and docs (`cache_health_check.py`,
   `grid_prepare_2020_stress.sh`, finished migrations, legacy grid scripts).

## Run log
(fill in: Task 2 counts, split-table review size, build coverage, archive time, hand-off SHA)
- 2026-10-20 Task 2 (as a second store `alpaca_store_daily2016`): 11,621 symbols, 7,984 written, 3,767 empty (no
  Alpaca daily bars: mutual funds / OTC), 0 failed, 0 refused, 12,075 requests, 425 MB, 1 h 31 min.
