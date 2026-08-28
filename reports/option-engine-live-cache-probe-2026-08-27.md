# Option Backtest Engine — Live-Cache Structure Probe (babatest, 2026-08-27)

**Box:** `debian@141.94.199.227` (`babatest`) — distributed grid worker, 32 cores / 251 GB.
Grid was RUNNING throughout; this work stayed read-only on the worker's code and cache:
separate clone + worktree in `/tmp/ba2-gridtest-wt` (branch `gridtest/engine-probe`),
separate `BA2_HOME=/tmp/ba2-gridtest-home` with `common/` **symlinked** to the shared cache
(no copies, no re-downloads), scratch DB for the FMP key setting. Max 1 probe process at a time;
nothing outside my own processes was killed or modified.

**Code under test:** `f990ad8c` (the fix commit acting on the grid-prep review) + a
30-min poller for incoming commits (`/tmp/ba2-gridtest/dev-poll.sh` → `dev-poll.log`).

**Method:** single (non-GA) backtests driving the EXACT GA-trial path —
`_build_strategy(kind)` → `decode_params({})` (the authored genome, i.e. the warm-start seed) →
`_build_daily_trial_config` → `run_daily_backtest` — expert FMPRating, $20k capital,
hermetic off the on-disk caches. Probe script: `test_files/probe_option_engine_structures.py`.

---

## 1. Smoke verdict: the engine runs all launchable structures

| structure | AAPL (Feb–Dec 2024) | gates-off | INTC gates-off | conclusion |
|---|---|---|---|---|
| O_STK | 5 trades, +3.7% | — | — | ✅ equity control arm works |
| O_LC | 3 trades, +8.1% (real call contracts, fills, expiry/DAY-order lifecycle) | — | 3 trades | ✅ |
| O_LP | 0 (gates) | 1 trade | 3 trades, +16.2% | ✅ gate-blocked, not broken |
| O_VERT | 0 (gates) | 2 trades | 0 | ✅ (INTC refusal: sizing/contract fit) |
| O_BF | 0 (gates) | 0 | 0 | ⚠️ zero even gates-off — see §2 |
| O_BULLCS | 0 (gates) | 0 | 4 trades, −5.4% | ✅ |
| O_BEARCS | 0 (gates) | 10 trades, −9.3% | — | ✅ |
| O_BULLPS | 0 (gates) | 0 | 0 | ⚠️ see §2 |
| O_CSP | 0 (gates) | 0 | 0 | ⚠️ see §2 |
| O_IC | 0 (gates) | 0 (REFUSED: assignment capacity $37–76k vs $20k) | 0 | ✅ correct refusal on AAPL |
| O_JL | 0 (gates) | 0 | 6 trades | ✅ |
| O_RS | 0 (gates) | 0 | 2 trades | ✅ |
| O_SSTD | 0 (gates) | 0 | 3 trades | ✅ |
| O_SSTG | 2 trades, +2.5% | — | — | ✅ |
| O_STRD | 6 trades, −0.5% | — | — | ✅ |
| O_STRG | 6 trades, −0.6% | — | — | ✅ |
| O_CC | 5 trades (equity entry + overlay) | — | — | ✅ |
| O_PP | 5 trades | — | — | ✅ |
| O_WHEEL | **RAISES** (guard from f990ad8c, override `BA2_ALLOW_UNRUNNABLE_WHEEL=1`) | — | — | ✅ guard verified |

**0 errors across ~30 runs.** Refusal messages are precise and actionable
("ASSIGNMENT CAPACITY: ... 37,000.00 against 20,000.00 of cash", participation-cap retries,
DAY-order expiries) — the "traded nothing" diagnosis problem the grid spec worries about is
largely solved by the log vocabulary.

## 2. The remaining zero-trade structures are data-shaped, not engine-shaped

Even with `--gates-off`, O_BF / O_CSP / O_BULLPS (and O_VERT on INTC) opened nothing. The log
shows the cause class: **fill realism on thin chains** — multi-leg orders hit the 10%-of-bar-volume
participation cap ("volume 8 allows at most 0.8 — order stays pending, retries next bar") and DAY
orders expire unfilled. The structures TRY to enter; the simulated market won't give them fills.
This is exactly the liquidity constraint the grid spec §6 names as universal, and it means:
**stage 1 on the current cache will measure "can this structure fill here", not just "is it a good
strategy"** — which is fine for the large-cap band if the universe is chosen for chain depth
(SPY/QQQ dominate the bar counts by ~6x over single names).

## 3. Perf parity with the equity path — measured

**Architecture comparison** (what the stock path has vs the option path):

| equity path | option path | parity? |
|---|---|---|
| `MemoizedOHLCVProvider`: one fetch per symbol per worker, in-memory slices | per-(db, underlying) chain cache + per-(db, contract) bar cache, worker-level LRU (`_WORKER_*_CACHE`, caps 300 / 50 000, env-overridable) | ✅ same shape |
| `AsOfPriceSource.preload` loads the whole window up front | chain history loaded once per underlying on first read (`_load_chain_history`) | ✅ equivalent |
| — | `get_atm_iv` result memo (the PremiumScanner 40-min killer, fixed) | ✅ extra |
| order-cache invalidation only on event bars | same engine discipline, pinned by `test_option_run_perf.py` | ✅ |

**Indexes:** `option_bar` PK covers `occ_symbol` lookups (query plan uses the autoindex,
0.00 s per contract), `option_chain` has `idx_option_chain_underlying` (11.5k rows/symbol,
0.00 s). No missing index found.

**Measured (AAPL, Feb-2024 → Dec-2025, 1 symbol, gates-off, same box):**

| run | wall |
|---|---|
| O_STK (equity), warm | 2.1–2.2 s |
| O_LC (options), **cold process** | 7.6 s |
| O_LC (options), warm | 1.4–2.1 s |

Cold start ≈ +5–6 s (one underlying's chain: 11.5k rows + per-contract bar loads), then the
option overhead over equity is within noise (~30% on this tiny universe, less per added bar
since the expert analysis dominates). Across a GA population the worker-level caches amortize
the cold start completely — same as the equity memo. **Verdict: option backtests are at the same
performance tier as stock backtests; no structural gap found.**

## 4. Data findings (decision-relevant for the grid)

1. **The TastyTrade parquet tree is NOT read by the backtest engine.**
   `/opt/ba2worker/.../cache/TastyTradeOptionsProvider/` holds 5.7 GB / 822 underlyings with
   per-expiry partitions from 2023-01 (INTC/AMD: 190 expiries each, bars carry iv + open_interest).
   But `ba2test_launcher.py:2427` states it plainly: the backtest path reads ONLY the
   `options_history.sqlite` (3.9 GB Alpaca build). The parquet store (`parquet_store.py`) was
   built as the future superset ("the warm-up rebuilds a superset of it"); wiring it into
   `HistoricalOptionsProvider` is still out-of-scope per the options roadmap doc. Consequence:
   **the engine's effective window is 2024-02-01 → 2026-07-07, not 2023+**, and the only store
   with iv/greeks visible to the engine is the sqlite build.
2. **Chain snapshots are one-per-symbol** (`as_of` = 2024-02-01 for every underlying): Alpaca's
   chain endpoint is a current snapshot; the build documented this (`fetch_options.py:5`).
   Entry gates that read chain iv therefore evaluate against a Feb-2024 chain shape for the whole
   run — acceptable for smoke, a known limitation for regime work.
3. The sqlite build DOES carry iv/greeks columns on `option_bar` (new build), so iv-based gates
   have data; `option_chain` iv is populated for 663k of 1.44M rows.
4. FMPRating needs `FMP_API_KEY` in the `appsetting` table (not env) — the worker's own DB has it;
   the probe seeds its scratch DB with it. Backtests stay hermetic (cache hits only).

## 5. Ready-to-start assessment

- **Stages 0a/0b and stage 1 can start on this box** with the 17 launchable structures
  (O_WHEEL excluded by its own guard until lifecycle plan Task 10 lands).
- Universe choice matters more than expected: on single names, half the structures are
  fill-starved even gates-off. Prefer the deep-chain names (SPY, QQQ, IWM lead the cache by 3–6x).
- Perf needs no new work for the grid; the existing worker caches give equity-class speed.
- When the parquet-store wiring lands (roadmap Phase 1-2), re-run this probe against it — the
  script only needs the `options_cache_db` seam swapped.

## Artifacts

- Probe script: `test_files/probe_option_engine_structures.py` (repo, untracked)
- Results JSONs: `/tmp/ba2-gridtest-home/test/probe_*.json` on babatest
- Worktree: `/tmp/ba2-gridtest-wt` (branch `gridtest/engine-probe`)
- Commit poller: `/tmp/ba2-gridtest/dev-poll.sh` → `/tmp/ba2-gridtest/dev-poll.log`
- Companion doc: `reports/option-grid-prep-review-2026-08-27.md` (code/plan audit)
