# Reproducing our backtest results (equity and options)

The entry point for anyone who needs to reproduce a stored backtest, a GA optimization or a whole
grid from scratch. It links to the detailed runbooks and plans rather than repeating them.

Written 2026-09-26 against dev `d8a30842` (`APP_VERSION 2026.09.1200`, `TEST_APP_VERSION
2026.09.0095`). Every command, flag, path and env var below was checked against that tree by grep
or `--help`. A number marked **measured** comes from a run; one marked **estimate** says where it
comes from. **(unverified)** marks what could not be checked from the repository.

---

## 0. Status of each result family

| Family | Where it stands (2026-09-26) | Runbook |
|---|---|---|
| Equity GA grid `goal2020` | 45 jobs, 2020-01-01..2025-12-31, scored on `consistent_annual_return` | [RUNBOOK-goal2020-grid.md](RUNBOOK-goal2020-grid.md) |
| Option stage 1 (`-st1fix`) | Running on remote227: 16 DeterministicScorer jobs on 97 names | §5.3 below, `tools/stage1_run.sh` header |
| Option stage 2 | Configured, not built. The 753-name data is prepared. A pilot is required before launch. | `docs/plans/2026-09-25-option-stage2-plan.md` (branch `plan/option-stage2`, not merged) |
| Option data preparation | One-command pipeline on branch `data/stage2-universe-repair` (not merged) | `docs/RUNBOOK-option-universe-data.md` on that branch |

**All option grid results produced before the 2026-09-24/25 engine fixes are void** (§6.3).

---

## 1. Overview

### 1.1 Terms

| Term | Meaning here |
|---|---|
| **Backtest** | One run of one expert with fixed settings and rulesets over a universe and a date window, replayed day by day through the same expert, ruleset and risk-manager code that trades live. It produces trades, an equity curve and metrics. Engine: `daily_expert` (`testplatform/backend/app/services/backtest/`). |
| **GA optimization** | A genetic search over expert settings, risk-manager/TP/SL parameters, ruleset toggles and thresholds, screener ranges and the entry weekday. One job evolves one population. The search scores every individual (a "trial", i.e. one backtest) with a fitness metric (`testplatform/backend/app/services/strategy_fitness.py`). |
| **Grid** | A driver script that runs many GA jobs one after another: `tools/run_screener_capband_matrix.py` (equity), `tools/run_options_matrix.py` (options). |
| **Hermetic** | A backtest reads **pre-warmed caches only** and makes 0 network calls (`hermetic_fmp_history()` and `cached_only=True`, `daily_backtest_handler.py:928-956`). A cache miss aborts the run rather than fetching. API keys are therefore needed to **warm** data, not to run a backtest. |

### 1.2 The two platforms

| | Trade app (`ba2_trade_platform/`) | Test platform (`testplatform/`, CLI `ba2-test`) |
|---|---|---|
| Purpose | Live and paper trading | Backtests, GA optimization, robustness |
| DB | `~/Documents/ba2/trade/db.sqlite` | `~/Documents/ba2/test/dl_forecasting.db` |
| Shared | Both import `packages/common`, `packages/providers` and `packages/experts`, and share the raw provider cache `~/Documents/ba2/common/cache` (`CACHE_FOLDER`) |
| Needed to reproduce results? | No (only to forward-test a winner) | Yes |

Everything lives under `BA2_HOME` (default `~/Documents/ba2`). See
[testplatform/README.md](../testplatform/README.md#data-and-cache-layout) for the layout and the
path overrides (`CACHE_FOLDER`, `DATABASE_URL`, `LOG_FOLDER`, ...).

### 1.3 Where results land (test DB)

| Table | Content |
|---|---|
| `strategy_optimizations` | One row per GA job: `name` (the resume key, §5.5), config, status (`running`/`completed`/`failed`), per-generation history, best params |
| `backtests` | Every persisted backtest: `trades`, `equity_curve` and `drawdown_curve` blobs, metrics, `ga_fitness`, `labels`, `optimization_id` |

At the end of a job the launcher re-runs and persists its **TOP-N** (`--save-top`, default 5) as
`TOP<rank>-<job name>` rows. The TOP-N is distinct by *fitness*, not by *behaviour*: a converged
job's top ranks are often inert-gene clones of one strategy. To get the best N that trade
differently, run `tools/persist_distinct_topn.py` after the job (rows `DTOP<rank>-<job>`, label
`TopNDistinct`; it prints per-year returns and top-1/top-5 concentration). Report the economics
(profit, CAR, max drawdown) next to fitness with `tools/report_grid_results.py`.

```bash
python tools/persist_distinct_topn.py --opt-id <id> --dry-run            # selection table only
python tools/persist_distinct_topn.py --opt-id <id> --n 10 --skip-already-persisted
python tools/report_grid_results.py --like %<job-name-fragment>% --top 3
ba2-test runs list --group <optimization_id>                             # the rows of one job
ba2-test report                                                          # HTML summary
```

`persist_distinct_topn.py` runs outside any grid memory governor. Next to a live grid keep
`--parallel 1`; it refuses below `--min-free-gb` (default 20) free RAM.

---

## 2. Required APIs and keys

### 2.1 Reproducing backtests vs trading live

| Service | Reproduce backtests | Live trading | Used for |
|---|---|---|---|
| **FMP** (Financial Modeling Prep) | **Required** to warm the cache (not at run time) | Required | OHLCV, split/dividend calendars, statements, grades, price targets, earnings, insider, Senate/House, market cap |
| **ThetaData** | **Required for option grids** (backfill only) | No | 2020+ option EOD history with greeks and open interest |
| **FRED** | Required to warm DeterministicScorer's macro series **and `DGS3MO`, the risk-free rate every option backtest prices with** (not at run time) | Required for DS live | `VIXCLS`, `UNRATE`, `BAA10Y`, `T10Y3M` (`DeterministicScorer/data.py:74`); `DGS3MO` (option Black-Scholes rate, `ba2_providers/macro/risk_free_rate.py`) |
| **Finnhub** | Only for `FinnHubRating` | Only for `FinnHubRating` | Recommendation trends |
| **Alpaca** | No (only the legacy `sqlite` option store, `ba2-test fetch-options`, floor 2024-01-18) | **Required** (the broker) | Orders, positions, live option snapshots |
| **TastyTrade / dxfeed** | No (older `tastytrade` store, floor 2022-10-01) | No | Superseded by ThetaData for 2020 windows |
| **LLM keys** (OpenAI etc.) | **No** — every backtestable expert is LLM-free | Only for TradingAgents and the AI news/overview/social providers | — |

With a warmed cache imported from another machine (§4.5), a backtest or a grid runs with **no
key at all**, because runs are hermetic. That holds as long as nothing is missing: a cache miss
aborts the run.

### 2.2 What each backtestable expert reads

Source: `_SUPPORTED_EXPERTS` in `testplatform/backend/app/services/backtest/daily_backtest_handler.py:234`
and the prewarm table `FETCHER_METHODS` in `testplatform/backend/app/services/prewarm_fetchers.py:45`.

| Expert | Data read in a backtest (all from the FMP cache unless noted) | Prewarm with `ba2-test prewarm --experts` |
|---|---|---|
| `FMPRating` | Grades history (`stable/grades-historical`, `stable/grades`), price targets (`api/v4/price-target`, `api/v4/price-target-consensus`), `api/v4/upgrades-downgrades-consensus`. **Starts 2022-01-01**: FMP's price-target history is empty before ~2021-04. | `FMPRating` |
| `FMPEarningsDrift` | `past_earnings_quarterly` (earnings calendar) | `FMPEarningsDrift` |
| `FMPInsiderClusterBuy` | Insider trades (`api/v4/insider-trading`, namespace `insider_v2`) and the price-target model inputs. No large-cap insider data. | `FMPInsiderClusterBuy` |
| `FactorRanker` | Statements, past earnings and estimates, 1d OHLCV (12-1 momentum; 252-bar warmup) | `FactorRanker` |
| `DeterministicScorer` | 1d OHLCV (260-bar warmup), annual income/balance/cash-flow statements, grades history, past earnings, price targets, **FRED** macro | `DeterministicScorer` (also refreshes FRED) |
| `FMPSenateTraderWeight` / `FMPSenateTraderCopy` | `stable/senate-trades-by-name`, `stable/house-trades-by-name`, `api/v3/historical-price-full` for ~1,800 disclosed tickers | `FMPSenateTraderWeight` |
| `FinnHubRating` | Finnhub recommendation trends (`finnhub_api_key`) | `FinnHubRating` |
| `ETFTrend`, `PullbackReversion` | 1d OHLCV only | (no prewarm entry; `fetch-cache`) |
| `FMPEarningsEvent` | Past earnings × OHLCV (620-bar warmup) | (no prewarm entry) |

Screener runs and the option gate also need the **screener metric store** (FMP historical market
cap, `api/v3/historical-market-capitalization`). The option split-basis checks need the FMP split
calendar (`api/v3/stock_split_calendar`) and dividend calendars.

**FMP plan tier: (unverified).** No document or code names the plan. The endpoints above include
v4 price-target history, insider trading, and Senate/House disclosures. Check that your plan
serves them before you warm. `fmp_common.py` rate-limits and backs off globally; it does not
encode a quota.

**ThetaData:** the provider uses the official cloud `thetadata` Python library with an API key.
**No local Theta Terminal is needed any more.** The terminal transport on `127.0.0.1:25503` was
replaced on 2026-09-02 (`packages/providers/ba2_providers/options/thetadata.py:1-9`). One
session per key, so concurrency is threads in one process. The subscription tier is
**(unverified)**. The backfill assessment
([2026-09-03-thetadata-eod-backfill-assessment.md](2026-09-03-thetadata-eod-backfill-assessment.md))
lists "4 (Standard limit)" concurrent requests and measured runs at 6 and 10. The data needed is
EOD option history with greeks (`option_history_greeks_eod`) and open interest back to 2020.

### 2.3 Where keys are configured

| Key | Test platform (reproducing) | Trade app (live) |
|---|---|---|
| FMP | AppSetting `FMP_API_KEY` in the test DB, or env `FMP_API_KEY`. The launcher mirrors the DB value into the env at startup (`ba2test_launcher.py:112-117`). Scripts that bypass the launcher need `DB_FILE`/`DATABASE_URL` pointed at the test DB, or they fail with "FMP API key not configured". | AppSetting `FMP_API_KEY` (Settings page) |
| FRED | AppSetting **`fred_api_key`** (the Settings -> API Keys page saves it under that name). Env `FRED_API_KEY` overrides it when set. Every reader in both apps goes through `ba2_common.core.fred_api_key`. | AppSetting `fred_api_key` (env `FRED_API_KEY` overrides) |
| ThetaData | `tools/warm_options_history.py --api-key`, else env `THETADATA_API_KEY`, else AppSetting `thetadata_api_key` read from `--db <sqlite>` (`warm_options_history.py:416-440`) | — |
| Finnhub | AppSetting `finnhub_api_key` | AppSetting / `.env` `FINNHUB_API_KEY` |
| Alpaca | AppSettings `alpaca_market_api_key`/`_secret` (only for `fetch-options`) | Per account: `api_key`, `api_secret`, `paper_account` in the account's settings (`AlpacaAccount.py:486-488`) |
| LLM | — | `.env` `OPENAI_API_KEY` and the Settings page |

The FRED key has one canonical name, `fred_api_key`. Before 2026-09-26 the test platform's
Settings page saved it as `FRED_API_KEY`, which nothing read (AppSetting lookups are exact). A
row left under that name is now refused with a pointer to
`python tools/migrate_fred_api_key.py --db <db>`, which moves it (idempotent). A backtest host
needs **no** FRED key: runs read the synced `<CACHE_FOLDER>/fred/*.json` files only.

**Option risk-free rate.** Every option backtest inverts its bars' greeks and prices its
Black-Scholes marks at the **as-of 3-month Treasury (FRED `DGS3MO`)**, read cache-only from
`<CACHE_FOLDER>/fred/DGS3MO.json`; a run refuses to start when that file is missing or does not
cover `[start - warmup_days, end]`, and records the source in its results
(`options_risk_free_rate_source`). `options_risk_free_rate` in the run config or env
`BACKTEST_OPTIONS_RISK_FREE_RATE` set an explicit constant instead (recorded as `explicit`).
**Option results produced before 2026-09-26 used a flat 4.5%** and do not reproduce on current
code; the trade popup still shows their greeks at the 4.5% they used.

---

## 3. Hardware

### 3.1 Memory per trial slot

| Workload | RAM per slot | Source |
|---|---|---|
| Light equity experts, large-cap band (~105 screened symbols) | ~2.5-3.5 GB | measured 2026-08-20 (DS), memory note on distributed worker sizing |
| Equity mid band (~580 symbols) | ~6.3 GB | same |
| Equity small band (~1,320 symbols) | 7.4-13.0 GB; **size on the peak child** | same |
| FMPSenateTraderWeight | ~11-12 GB steady (12.3 GB measured after the 2026-09-06 fix) | measured; runbook §3 |
| Option trial, 2020 ThetaData window, **before** shared arrays | ~15.6 GB private (OOM at 20 and at 16 slots on remote227) | measured 2026-09-14, `stage1_run.sh` header |
| Option trial **with** shared arrays (since `c608ac05`) | ~1-3 GB private per consumer. The columns are mapped once per host from `<CACHE_FOLDER>/_derived`. | measured; `stage1_run.sh` header, `docs/plans/2026-09-14-shared-arrays-across-workers.md` |
| Derived-array build transient | ~2.3x the frame; ~7-8 GB for ThetaData TSLA. Build at `--jobs 3-4`. | measured; `build_shared_arrays.py --help` |
| ThetaData backfill (`warm_options_history.py`) | ~6 GB for the plan phase alone; 17.9 GB at concurrency 6. **Run at 3.** | measured 2026-09-03 (857 symbols) |

Measure option consumers by **private** bytes (`/proc/<pid>/smaps_rollup`
Private_Clean+Private_Dirty). RSS counts shared mapped pages and overstates them.

### 3.2 Reference hosts

| Host | RAM | Use | Notes |
|---|---|---|---|
| Local Windows workstation | 64 GB (63.7 GB reported) | Data prep, equity master, 4 local slots max | Shared with the live platforms on 8080-8082 |
| remote227 (Linux) | 251 GB | Option stage 1 at **28 slots at ~65-70 % RAM**; equity grid worker | Derived cache ~58 GB. CPU count **(unverified)**. |
| remote150 | 32 GB | Equity worker, 6 slots | — |

### 3.3 Minimum and recommended specs (estimates)

These are **estimates** derived from the figures above, not tested configurations.

| Goal | Minimum | Recommended |
|---|---|---|
| Re-run single stored backtests (equity or option) | 16 GB RAM, 4 cores | 32 GB |
| Equity grid, large band | 32 GB (4 slots × ~3.5 GB + master) | 64 GB + a remote worker |
| Equity grid, mid/small bands or Senate | 64 GB at 4 slots (small band peaks 13 GB/slot) | 128-256 GB |
| Option stage 1 (97 names) | 64 GB at ~8 slots after the prewarm (**estimate**: ~3 GB private × 8 + mapped columns + master) | 256 GB at 28 slots (remote227) |
| Option stage 2 (753 names) | Unknown until the pilot measures it | — |
| Worker slots | `ba2-test worker --workers N`; default CPU count - 1 | Size N from RAM: `(total × 0.85 - server RSS) / peak child RSS` |

### 3.4 Disk

| Item | Size | Source |
|---|---|---|
| ThetaData option store, local workstation | ~15.6 GB | local measurement (operator, 2026-09) |
| ThetaData option store, remote227 (819 of the 820 large-cap names) | ~18 GB | stage-2 plan §2 |
| FMP OHLCV cache | ~16 GB | local measurement |
| `fmp_history` | ~8 GB | local measurement |
| `market_conditions` | ~2 GB | local measurement |
| Screener metric store | ~1.5 GB | local measurement |
| Derived `.npy` cache (`_derived`) | ~58 GB on remote227 | measured |
| Test DB | 24 GB on 2026-08-04, and it grows with every persisted row | goal2020 runbook §8 |
| Logs | not rotated; one `serve` log reached 20 GB | goal2020 runbook §9 |

Budget **~150-250 GB free** for a full option setup plus the test DB (**estimate**: the sum
above plus headroom).

---

## 4. Data preparation

Run everything from the repo root with the test venv (`~/ba2-venvs/test`, installed by
`install.ps1 -TestOnly -Editable` / `install.sh --test-only --editable`). `ba2-test` is that
venv's console script for `testplatform/ba2test_launcher.py`.

### 4.1 Equity (goal2020 and single equity backtests)

| Step | Command | Notes |
|---|---|---|
| 1. OHLCV | `ba2-test fetch-cache --provider fmp --timeframes 5min,1d --start 2020-01-01 --end 2025-12-31 --symbols @<universe.txt> --workers 5` | goal2020 prices on **5min** bars. `grid_goal2020.sh` refuses to start below 75 % coverage per band (`tools/check_window_coverage.py`). Fetch the warmup before `--start` for FactorRanker (252 bars) and DeterministicScorer (260 bars). |
| 2. Expert histories | `ba2-test prewarm --symbols @<universe.txt> --experts FMPRating,FMPEarningsDrift,FMPInsiderClusterBuy,FactorRanker --end 2025-12-31` | Add `FMPSenateTraderWeight` with `--start` to pre-compute Senate scores. |
| 3. Screener metric store | `ba2-test build-screener-metrics --start 2020-01-01 --end 2025-12-31 --market-cap-min <loosest cap> --cadence-days 7` | Default path `~/Documents/ba2/common/cache/screener/metric_store`. It **must reach `ym=2020-01`**; `grid_goal2020.sh` checks this. The small-band floor is $50M (`--screener-cap-band` help); the exact `--market-cap-min` of the existing store is **(unverified)**. Build time **(unverified)**. |

The screened universe of each band is derived from the store, so step 1 needs the union of
symbols the store can ever screen in. `grid_goal2020.sh` prints each band's union in its
preflight.

### 4.2 Options (stage 1: 97 names)

Universe: `tools/options_universe_top100.txt` (**97 symbols**; SPCX was removed on 2026-09-16).
Window 2020-01-01..2025-12-31 on the `thetadata` store (history floor 2018-09-14).

| # | Step | Command | Measured cost |
|---|---|---|---|
| 1 | ThetaData backfill | `python tools/warm_options_history.py --provider thetadata --wide --symbols-file tools/options_universe_top100.txt --start 2020-01-01 --concurrency 3 [--db <sqlite with thetadata_api_key>]` | 857-name projection: ~6 days (**estimate**, assessment doc §3). 97 names: **(unverified)**. Run at concurrency 3 under a memory cap. The plan phase alone is ~6 GB. |
| 2 | FMP 1d OHLCV | `ba2-test fetch-cache --provider fmp --timeframes 1d --start 2018-12-01 --end 2025-12-31 --symbols @tools/options_universe_top100.txt` | Start ~13 months early: DS needs 260 bars of warmup (×1.45 calendar days per bar, `daily_backtest_handler.py`) |
| 3 | DS prewarm | `ba2-test prewarm --symbols @tools/options_universe_top100.txt --experts DeterministicScorer --end 2025-12-31` | — |
| 3b | Risk-free rate | `python tools/refresh_fred_cache.py --series DGS3MO` (needs the FRED key), then on every grid host `python tools/refresh_fred_cache.py --check-rate-window 2020-01-01 2025-12-31` (cache only) | `stage1_run.sh` runs the check and refuses to launch without the series |
| 4 | Screener gate store | `ba2-test build-screener-metrics --start 2020-01-01 --end 2025-12-31 --market-cap-min 10000000000 --cadence-days 7` | `stage1_run.sh` refuses to launch without it |
| 5 | Market-condition manifests | `stage1_run.sh` runs `plan -> build --cache-only -> verify -> prepare-host` per profile (§4.3) | 45-70 min for both profiles cold (753 names, data runbook) |
| 6 | Shared-array prewarm, **run twice** | `python tools/build_shared_arrays.py --options-store thetadata --universe-file tools/options_universe_top100.txt --ohlcv-provider FMPOHLCVProvider --interval 1d --start 2020-01-01 --end 2025-12-31 --warmup-days 60 --jobs 4` | The second run must report **0 built** and every symbol opened. Mandatory before a cold launch: 24 cold consumers building different keys can OOM the host. |

**Split-basis repair.** ThetaData strikes are as-traded; FMP spot is split-adjusted. The option
path now uses the as-traded basis with overrides (`ba2_common.core.split_basis_overrides`) and a
guard. `warm_market_conditions.py plan` runs the split-basis preflight and refuses a name whose
basis it cannot settle. Repair it with `build --plan <plan> --fetch-missing --concurrency 3`,
which replaces the file wholesale. On 2026-09-16, 13 symbols took ~6 s and 13 provider calls.
See the goal2020 runbook, "Published snapshots".

### 4.3 Market-condition manifests

A manifest pins the immutable feature objects every trial reads (profiles `ohlcv-v1` and
`ta-structure-v1`). The **digest** is content-addressed and differs per host whenever the price
caches differ. Each host warms and verifies its own, and a job pins the digest in its name.

```bash
python tools/warm_market_conditions.py plan --profile ohlcv-v1 \
    --universe-file tools/options_universe_top100.txt --start 2020-01-01 --end 2025-12-31 --out plan.ohlcv-v1.json
python tools/warm_market_conditions.py build --plan plan.ohlcv-v1.json --cache-only --print-digest
python tools/warm_market_conditions.py verify --manifest <digest>
python tools/warm_market_conditions.py prepare-host --manifest <digest> --profile ohlcv-v1
```

Repeat for `ta-structure-v1`. Pinned digests of record:

| Universe / host | `ohlcv-v1` | `ta-structure-v1` |
|---|---|---|
| 97 names, remote227, stage 1 since 2026-09-23 | `ca4d65d4…` (full digest in the job config, **not recorded in the repo**) | `2b6e8ca8…` (same) |
| 753 names (stage 2), local, synced to remote227 | `105278717aecf5471e9c01ef365a436eeaa917248e34c44e2af10643cf2489bd` | `dbac2c668a9a398c5efe000a6c260e81ee3a32279d2b7d0087862c7f4faf494e` |

The 98-name workstation digests in the goal2020 runbook (`c9ba981f…`, `3c3020d0…`) predate the
2026-09-23 data repairs. To reproduce a specific job, read the digests from that job's
`strategy_optimizations` config.

### 4.4 Options (stage 2: 753 names) — the one-command pipeline

After branch `data/stage2-universe-repair` merges, follow
[RUNBOOK-option-universe-data.md](RUNBOOK-option-universe-data.md) (not on dev yet; read it with
`git show data/stage2-universe-repair:docs/RUNBOOK-option-universe-data.md`). The command is:

```bash
python tools/prepare_option_universe.py --universe-file <universe.txt> \
    --start 2020-01-01 --end 2025-12-31 --report-dir reports/prep-<date> [--db <sqlite with the ThetaData key>] \
    [--dry-run] [--steps a,b] [--from-step X] [--procs 2] [--theta-concurrency 1]
```

| # | Step | What it does |
|---|---|---|
| 1 | `split-check` | `warm_market_conditions.py plan --profile ohlcv-v1`: split calendars plus the split-basis check per FMP file |
| 2 | `dividends` | `warm_dividend_calendars.py`: FMP dividend calendars for the guard's carry separation |
| 3 | `fmp-repair` | Force-refetches refused names, then re-checks. **Refuses while the US session is open.** |
| 4 | `thetadata` | `warm_options_history.py --provider thetadata --wide`: missing partitions, plus partitions made stale by a `ROOT_HISTORY` alias |
| 5 | `ds-prewarm` | `prewarm --experts DeterministicScorer` |
| 6 | `gate-check` | Screener gate-store rows per name |
| 7 | `manifests` | `plan` + `build --cache-only` for both profiles; the digests go into the report |
| 8 | `preflight` | `option_universe_preflight.py`: coverage, split basis, guard replay, DS inputs, gate store -> `preflight.json` |

The pipeline stops for a human at each decision: a still-refused name, a guard refusal, and
review findings. The runbook says how to resolve each (override, `ROOT_HISTORY` entry, or
exclusion). `prepare-host` on each grid host and syncing the tree are **not** part of it, and
neither is the option risk-free rate: run `tools/refresh_fred_cache.py --series DGS3MO` and
`--check-rate-window` (step 3b above) and sync `<CACHE_FOLDER>/fred/DGS3MO.json` to every host.

The 753-name universe is the 820-name `tools/options_universe_large_cap.txt` minus 67 recorded
exclusions: 55 with no listed options 2020-2025, CBUS, BNT, BMNR, QXO, RGC and 7 ex-SPACs. **The
753-name file is not committed** (it sits in a session scratch folder as `universe_stage2.txt`).
Commit it with the data branch.

Measured on 2026-09-25/26 (local box, from the data runbook and the repair log):

| Operation | Measured |
|---|---|
| Split-basis preflight over 820 names | ~2.5 h wall-clock; 820 FMP split-calendar requests |
| Preflight over 753 names at `--procs 2` | 28 min, peak 0.56 GB |
| ThetaData alias re-fetch (7 root groups) | 9,019 requests, 3.66 M rows, 1 h 12 min; each group 20-35 min, peak RSS < 0.4 GB |
| ThetaData full tree for one symbol (XYZ) | 1,851 requests, 55 min |
| FMP forced refetch | 94 names, ~120 MB |
| Dividend calendars | 796 requests, 12.6 MB |
| Both manifests, cold, cache-only, concurrency 1 | 45-70 min |

### 4.5 Alternative: import a warmed cache

```bash
# on the source machine (skip rebuildable/retired folders)
python tools/ba2_cache_export.py export --dest-dir <dir> --scope cache --exclude _derived --exclude "_stale-*"
python tools/ba2_cache_export.py export --dest-dir <dir> --scope bt-ga    # backtests + GA jobs, no cache
# on the target
python tools/ba2_cache_export.py import --archive <cache.zip> [--cache-dir <dir>] [--db-path <db>] [--overwrite] [--force]
```

Then rebuild the host-local pieces on the target: `build_shared_arrays.py` (twice) and
`warm_market_conditions.py prepare-host` per digest. `_derived` is per host. A digest resolves
only if the imported price cache is byte-identical to the one it was built from, so run
`verify` first. Export/import time for the full cache **(unverified)**.

---

## 5. Running

### 5.1 A single backtest

```bash
# a new backtest from the CLI (args pass through to scripts/run_daily_backtest.py)
ba2-test backtest --expert FMPRating --universe AAPL,MSFT,NVDA --start 2024-01-01 --end 2024-12-31 \
    --interval 1d --run-schedule weekly --run-schedule-day monday --save
```

To reproduce a **stored** row (its full config, rulesets and genes), do not rebuild it by hand:

- UI: select the row in **Backtesting -> BT History** and rerun it in place, or call the API
  `POST /api/backtests/{id}/rerun`.
- CLI: `python tools/backtest_parity.py --bt <id>` re-runs the row twice (private and
  shared arrays), compares the two byte for byte, and compares both against the stored row. It
  writes two new `PARITY-*` rows; pass `--label` for a second pair.

An option backtest needs the §4.2 data. No standalone backtest duration is recorded
**(unverified)**; one run costs about one GA trial. A small-band equity trial is typically
~600 s, with a range of 200-6,400 s (`docs/plans/2026-08-20-mid-generation-checkpoint.md`). A
stage-1 option trial takes ~16-19 min (**estimate**: 110-130 min per generation of 200 at 28 slots
≈ 7 batches).

### 5.2 Equity GA grid: goal2020

Follow [RUNBOOK-goal2020-grid.md](RUNBOOK-goal2020-grid.md). In short:

```bash
git push origin dev                                   # workers sync by git pull
bash tools/grid_goal2020.sh --dry-run
nohup bash tools/grid_goal2020.sh > grid_goal2020.log 2>&1 &
bash tools/grid_status.sh
```

- 45 jobs in two sizing matrices (`risk_atr` 24, `notional` 21) × 3 cap bands. Fitness
  `consistent_annual_return`, window 2020-01-01..2025-12-31, FMPRating from 2022, per-band spread
  3/10/40 bps. Population 40, generations 8.
- Defaults: `WORKERS=remote227`, `PARALLEL=0` (remote-only).
- Duration: job 1 took 31.8 min/generation × 8 ≈ 4.2 h (**measured**). The runbook projects
  **5-7 days** for all 45 (**estimate**); local-only is ~2.5x slower.

### 5.3 Option stage 1

`tools/stage1_run.sh` wraps `tools/run_options_matrix.py --profile discovery`. **It is written for
remote227**: it `cd`s to `/home/debian/ba2-grid/repo`, uses `/opt/ba2worker/ba2-venvs/test/bin/python`
and reads the FMP key from that host's test DB. On another host, copy it and edit those lines, or
call the driver directly (below). Its header comments are the operational reference: parallelism,
prewarm, fitness and robustness choices, and market-condition pinning.

What the discovery profile runs:

- 16 structures: `O_LC O_LP O_VERT O_BULLCS O_BULLPS O_BEARCS O_BF O_IC O_JL O_RS O_CSP O_STRD O_STRG O_CC O_PP O_WHEEL`.
  `O_SSTG`/`O_SSTD` are refused by risk policy.
- One job per structure and expert. The discovery default is FMPRating and DeterministicScorer
  (32 jobs). The current run is **DeterministicScorer only (16 jobs)**. FMPRating produced no
  bearish signal on large caps.
- Population 200, generations 60, early stop 8, seed 42, $20k capital, 97 names.

| Setting | How it is set | Where |
|---|---|---|
| Fitness `option_car_target_soft30` (CAR > 35 %/yr and CAR > DD, trade ramp `min(structures/30, 1)`) | `STAGE1_FITNESS=option_car_target_soft30` (needs a non-default `STAGE1_SUFFIX`) | env |
| Job-name suffix | `STAGE1_SUFFIX=-st1fix` (the current run) | env |
| Robust fitness (concentration × Monte Carlo × spread) | on by default; `STAGE1_ROBUST=0` opts out and needs its own suffix | env |
| Fill-volume sizing | `--option-size-within-fill-volume` (always passed by the script) | flag |
| 1-contract floor | `option_min_one_contract=True` on every entry except `O_CONVEX*` | code default, `ba2test_launcher.py:3422` |
| Entry cross 0.75-1.0 (step 0.05) | `_OPTION_ENTRY_CROSS_BAND` gene | code default, `ba2test_launcher.py:3682` |
| DS `macro_short_side="mirror"` on option jobs | spec `option_fixed_settings` (part of the job digest) | code, `ba2test_launcher.py:1363` |
| Market-condition gates | `MARKET_CONDITION_PROFILE=ohlcv-v1,ta-structure-v1`, `MARKET_CONDITION_MANIFEST=ohlcv-v1=<d>,ta-structure-v1=<d>` | env |
| Slots | `PARALLEL=28` (script default 24) | env |
| Store | `STAGE1_STORE=thetadata` (default) | env |
| Window | `STAGE1_START`/`STAGE1_END` (default 2020-01-01..2025-12-31) | env |
| Gate store | `SCREENER_STORE` (default `$BA2_HOME/common/cache/screener/metric_store`), always `--max-stock-price 0` | env |

The remote227 launch (the exact argv of the current run is in its launch log,
`/home/debian/ba2-grid/stage1_obtfix_20260925T043250Z.log`; the `--experts` passthrough is
**(unverified)** here):

```bash
cd /home/debian/ba2-grid/repo
export MARKET_CONDITION_PROFILE="ohlcv-v1,ta-structure-v1"
export MARKET_CONDITION_MANIFEST="ohlcv-v1=<digest>,ta-structure-v1=<digest>"
export STAGE1_FITNESS=option_car_target_soft30 STAGE1_SUFFIX=-st1fix PARALLEL=28
bash tools/stage1_run.sh --experts DeterministicScorer --dry-run        # prints every command
nohup bash tools/stage1_run.sh --experts DeterministicScorer > /home/debian/ba2-grid/stage1.log 2>&1 &
```

The equivalent direct call on any host (verified with `--dry-run` on 2026-09-26: 16 jobs):

```bash
python tools/run_options_matrix.py --profile discovery --experts DeterministicScorer \
  --launcher testplatform/ba2test_launcher.py --start 2020-01-01 --end 2025-12-31 \
  --options-store thetadata --population 200 --generations 60 --early-stop 8 --parallel 28 \
  --screener-gate-store <metric_store> --max-stock-price 0 --name-suffix=-st1fix \
  --option-size-within-fill-volume --fitness option_car_target_soft30 \
  --market-condition-profile ohlcv-v1,ta-structure-v1 \
  --market-condition-manifest "ohlcv-v1=<d>,ta-structure-v1=<d>" [--dry-run]
```

Jobs are named `optm-DeterministicScorer-<structure>-spow0922-st1fix-d<12 hex>` (`-spow0922` is
the spread model). The digest (`discovery_name` in `run_options_matrix.py`) covers every
`optimize` argument except `--name/--parallel/--workers/--labels` (so the universe, window,
store, manifests, fitness and flags), the **absolute launcher path**, `BA2_HOME`, a few option
store env vars and `option_fixed_settings`. So **the same setup on another host or checkout path
yields different job names**; match runs by their config, not by name. The digest does **not**
cover code or cache content: after a code or data change at the same paths, give the run a new
`--name-suffix`.

**Linux host traps** (goal2020 runbook, "remote227 traps"): `loginctl enable-linger` and
`RemoveIPC=no` (semaphores), `LimitNOFILE=524288` (every mapped array holds an fd), cache
directory ownership, and `pgrep -f multiprocessing.spawn`, never `spawn_main`.

**Measured pace (remote227, 28 slots):** ~80-180 min per generation, rising as genomes trade more.
The stage-2 plan records 110-130 min for `-st1fix` O_LC; gen 1 took ~130 min. Job 1 (O_LC) was
past 30 h at generation 16.

**Estimate for the 16 jobs.** A job runs until generation 60 or until 8 generations pass without
improvement (at least 9 generations).

| Scenario | Assumed generations per job (average) | Minutes per generation | Per job | 16 jobs |
|---|---|---|---|---|
| Fast (many bearish jobs converge early, near zero fitness) | 12 | 90 | 18 h | ~12 days |
| Middle | 25 | 120 | 50 h | ~33 days |
| Slow | 40 | 150 | 100 h | ~67 days |
| Ceiling (every job runs all 60) | 60 | 180 | 180 h | ~120 days |

The realistic range is **~2-10 weeks** (**estimate**: the table's assumptions, not a
measurement). Re-derive it from the per-generation log lines after 3-4 jobs.

### 5.4 Distributed workers

```bash
ba2-test worker --port 8100 --password <secret> [--workers N]    # on each worker host (or $BA2_WORKER_PASSWORD)
ba2-test optimize ... --workers remote227,remote150              # on the master
```

Register workers under **Settings -> Workers** on the master. At pre-flight the master compares
the worker's **`TEST_APP_VERSION`** (not the git commit) and triggers `/update` (git pull,
reinstall, restart) on a mismatch. It then pushes missing cache files and prepares the pinned
market-condition snapshot. A worker that fails pre-flight is excluded and re-admitted if it
recovers. So:

- **Push before launching.** A worker can only reach a version that `git pull` gets.
- **Never bump `version.py` mid-run.** The master snapshots its version at job start, and a bump
  desyncs it from the workers.
- A shared-package change must bump `TEST_APP_VERSION`, or workers keep running old
  `ba2_common` while they report themselves as synced (CLAUDE.md, "Versioning").

### 5.5 Resume semantics

- **Job level:** a driver skips any job whose `strategy_optimizations` row with the same **name**
  is `completed`. The name is the resume key. A `failed` row is re-attempted on the next launch.
- **Generation level:** the GA checkpoints each generation. An interrupted job resumes at its last
  completed generation. A resumed row's `all_results` holds only post-resume trials.
- Resume **restarts from generation 0** when the gene space, population or generation count
  changed. It **fails** when the robustness setting differs from the checkpoint's.
- Changing the fitness, robustness, flags, window, manifests or code-level fixed settings needs a
  **new suffix**. The discovery digest enforces this for the digest-covered parts. Never mix
  objectives in one population.
- **Never merge into, edit or bump the checkout a grid runs from.** Merge at a job boundary.
  See the goal2020 runbook, operator checklist item 1.
- There is no pause. Stop and relaunch (`tools/grid_stop.sh`, then the same launch command).

---

## 6. Reproducibility requirements

### 6.1 Pin the code

| What | Where to read it |
|---|---|
| Commit | the dev SHA the run's checkout was on (for remote227 stage 1: job 1 on `3b3465c8`, TEST 0089; jobs 2-16 on `7fc918ee`, TEST 0092) |
| `TEST_APP_VERSION` / `APP_VERSION` | `testplatform/version.py`, `ba2_trade_platform/version.py`; also on each row's config |
| Launcher defaults | code constants such as the entry-cross band, `option_min_one_contract` and `option_fixed_settings` are part of the commit, not of the CLI |

### 6.2 Pin the data

- Market-condition manifest digests (§4.3), verified with `warm_market_conditions.py verify` on
  the host that runs.
- The universe file content (`options_universe_top100.txt` = 97 names; the 753-name file once
  committed).
- The same warmed cache: `ba2_cache_export.py` export/import (§4.5), or the §4 pipeline at the
  same code. The split-basis overrides and `ROOT_HISTORY` entries are code; see §6.1.
- The options store (`--options-store thetadata`) on the command line, never only in the env:
  distributed trials carry no environment.

### 6.3 Comparability boundaries: never compare across these

| Boundary | Effect |
|---|---|
| CAR fitness change, **2026-08-04** | `dd_guard` went from a cliff to a gradient. CAR numbers before and after are on different scales. |
| ATR fix, **app 2026.07.989** | ATR was silently dead in every classic-expert GA run before it (live unaffected; FactorRanker unaffected) |
| Senate lookahead fix, **TEST 2026.09.0018** | Earlier Senate runs read execution dates as public. They are void. |
| Commission default 1.0 -> 0.1, **2026-08-16** | Scores are not comparable across it |
| Robust fitness default ON, **2026-09-17** | Raw and robust scores are not comparable. Checkpoints record the setting. |
| BT/live option parity, **2026-09-23** (`799663e7`) | New session clock, split basis and spread model `pow-2026-09-22` |
| Option engine fixes, **2026-09-24/25** (`3b3465c8`, TEST 0089) | Split-crossing lots, DD refinement, option activity stepping, the <2-trade loophole. **Every option grid result before them is void.** |
| Option fitness family | `option_consistent_annual_return`, `option_car_over_risk`, `option_car_target`, `option_car_target_soft30` and `option_convex` are mutually incomparable |
| FMPRating window | Starts 2022; never mix it with full-window rows in per-year comparisons |

### 6.4 Determinism caveats

- The GA is seeded (`--seed`, default 42, part of the discovery digest) and each trial is
  hermetic and seeded, so the host that runs it does not matter. A different seed is a different
  search.
- **A persisted TOP-N row can differ from the GA fitness of the same genome.** 31 % of persisted
  rows contradicted it in one audit; only re-run (`specs`) rows diverged. Rows flagged
  `ga_fitness_divergence` are not used for fingerprint matching by `persist_distinct_topn.py`.
  Trust the persisted row's own metrics, and re-run it (§6.6) before relying on it.
- 12 stored rows carry `_atr_swap_migration` / `_inert_toggle_pin`. Re-running them from the GA
  genome silently uses different genes than the stored row.
- The option 2020-03-09 start is accepted for 24 names with an early-2020 ThetaData vendor gap.
  Do not "fix" it by refetching.

### 6.5 Before trusting a result

1. **Concentration:** the top-1 and top-5 trade share of net P&L, from the persisted `trades`
   JSON. `persist_distinct_topn.py` prints it. On the sen5min3 grid only S6 was clean; the others
   rode never-exited winners.
2. **Economics, not only fitness:** `tools/report_grid_results.py` (profit, CAR, max DD). For
   options, the per-year returns (2020 and 2022) as well, because the soft30 consistency floor is
   ×0.25.
3. **Spread sweep and Monte Carlo:** `POST /api/backtests/robustness`
   ([robustness suite](../testplatform/docs/robustness-suite.md)).
4. Check whether the edge is only the regime gene picking up 2022.

### 6.6 Verifying a reproduced result

The acceptance standard used for engine changes is **byte-identical**: trades, equity curve,
drawdown curve and every metric, compared as canonical JSON with only run-clock keys stripped.

- **Shared vs private arrays, and vs the stored row:** `python tools/backtest_parity.py --bt <id>`.
  It prints PASS only when the two re-runs match byte for byte and the shared path actually mapped
  data. Used on bt 1681, 1688, 1645 and 1695 (2026-09-14).
- **Old vs new code:** the 2026-09-25 engine-fix gate re-ran stored equity backtests **1225
  (DeterministicScorer) and 1578 (FMPRating)** on the old and new trees, each in a subprocess
  that put only its own tree on `sys.path`, and rebuilt each config through
  `app.services.backtest.rerun_handler.rebuild_config_for_backtest`. Both were byte-identical. The
  probe (`test_files/probe_backcompat_20260925.py`) is **not committed**; the procedure is in
  `docs/plans/2026-09-24-option-bt-engine-bug-fixes.md` ("Backward-compatibility acceptance").
- **Across machines:** re-run a stored row on the new host with the imported cache and compare
  it with the stored row. Any difference is a data or code difference to explain, never a
  tolerance to widen.

---

## 7. Estimated total time, from zero to results

| Phase | Time | Basis |
|---|---|---|
| Install venv + import a warmed cache (§4.5) | hours (copy-bound) | **(unverified)** |
| Equity data prep from scratch (5min + 1d OHLCV, prewarm, metric store to 2020) | **(unverified)**; plan in days | no measurement recorded |
| Option data prep, 97 names, from scratch | ThetaData backfill **(unverified)** for 97; the 857-name projection is ~6 days, so ~0.7 day for 97 by proportion (**estimate**). Manifests < 1 h, shared-array prewarm **(unverified)**. | assessment doc; data runbook |
| Option data prep, 753 names, repair on an existing tree | Preflight 28 min; split check over 820 ~2.5 h; alias re-fetch 72 min; manifests 45-70 min; **~5-6 h of steps plus the pass-A/pass-B wait for the US close** (**estimate**, sum of measured steps) | data runbook, repair log |
| Option data prep, 753 names, from scratch | ThetaData ~6 days (857-name projection) plus the repair above | assessment doc |
| One backtest | equity: minutes (typical trial ~600 s, range 200-6,400 s); option stage-1 trial: ~16-19 min (**estimate**) | §5.1 |
| Equity grid goal2020 (45 jobs, remote worker) | **5-7 days** (estimate from the measured job 1); local-only ~2.5x | goal2020 runbook |
| Option stage 1 (16 jobs, 28 slots, 97 names) | **~2-10 weeks** (**estimate**, §5.3) | measured pace 80-180 min/gen |
| Option stage 2 (753 names) | **Not measured. A pilot is required** (1 generation, pop 28, 2023-2025). Rough estimate: **1-3 weeks per style job**, ~8x the per-trial cost of 97 names; 2-3 style jobs in sequence. | stage-2 plan §4 |

---

## 8. Reading list

| Doc | For |
|---|---|
| [RUNBOOK-goal2020-grid.md](RUNBOOK-goal2020-grid.md) | Equity grid operations, the market-condition store, remote227 traps, backups |
| `tools/stage1_run.sh` header | Option stage-1 operations |
| `tools/run_options_matrix.py` header | Option grid operator checklist, discovery naming |
| RUNBOOK-option-universe-data.md (branch `data/stage2-universe-repair`) | Option universe data pipeline |
| `docs/plans/2026-09-25-option-stage2-plan.md` and `-build.md` (branch `plan/option-stage2`) | Stage 2 |
| [docs/superpowers/specs/2026-08-27-option-ga-grid-design.md](superpowers/specs/2026-08-27-option-ga-grid-design.md) | Option grid design (stages, fitness, universe) |
| [docs/plans/2026-09-14-shared-arrays-across-workers.md](plans/2026-09-14-shared-arrays-across-workers.md) | Derived cache, prewarm, parity tool |
| [docs/plans/2026-09-24-option-bt-engine-bug-fixes.md](plans/2026-09-24-option-bt-engine-bug-fixes.md) | The engine fixes that voided earlier option results |
| [docs/plans/2026-09-15-option-market-condition-genes-design.md](plans/2026-09-15-option-market-condition-genes-design.md) | Market-condition profiles and manifests |
| [2026-09-03-thetadata-eod-backfill-assessment.md](2026-09-03-thetadata-eod-backfill-assessment.md) | ThetaData request shapes and backfill cost |
| [testplatform/README.md](../testplatform/README.md), [grid & fitness guide](../testplatform/docs/grid-and-fitness-guide.md) | Test platform CLI, API, workers, fitness metrics |
