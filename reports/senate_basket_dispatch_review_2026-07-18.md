# Deep Review — Senate Basket Dispatch + In-Mem Store Expansion

**Date:** 2026-07-18
**Reviewer:** AI-assisted deep review (read-only; no fixes applied)
**Scope:** the senate-basket-dispatch feature set as it exists at HEAD (`7e7ece9`):

- The originally-uncommitted perf/cache changeset, since committed as `461a3b8`
  ("backtest-only in-memory scoring cache + IN_MEM_MODELS expansion"): `trade_store.py`,
  `TradeConditions.py`, `TradeRiskManagement.py`.
- The 14 commits in `2032ec5..HEAD`: basket `_gather_all`/`_process_all` + dual-mode
  `analyze_as_of` (`e21897e`), deep-pagination unscoped fetch (`58a010d`), lookahead-bias fix
  (`d20c2a3`), `is_tradable_stock_ticker` extraction (`c39a01a`), live `"EXPERT"`-symbol wiring,
  scoring-cache `is_live` threading, `_do_senate_latest` prewarm, `unsyncable_reason`
  diagnostic, and all associated tests.
- The engine dispatch commits that complete the same feature (`9a1c86a`, `37703d0`, `ce8c276`,
  `2032ec5`): `daily_engine.py` `_run_basket_expert_bar` / `_stage_recommendation_candidate`.

Plan under review: `docs/plans/2026-07-18-senate-basket-dispatch.md`.

---

## Findings

### H1 — HIGH (live): the new trade-feed memos never refresh for a live expert instance

`FMPSenateTraderWeight._all_trades_index_cached` (`FMPSenateTraderWeight.py:~532`) memoizes the
entire unscoped senate+house disclosure feed **per expert instance, unconditionally, with no
expiry or invalidation** (`self._all_trades_memo`). The same pattern exists for the per-symbol
`_symbol_trades_memo` (`:~348`, introduced by the earlier perf commit `e999f52`).

Live call chain that hits it:

- basket: `run_analysis("EXPERT")` → `_run_basket_analysis` → `_gather_all(as_of=None)` →
  `_all_trades_index_cached` (`FMPSenateTraderWeight.py:2605`)
- per-symbol: `run_analysis(symbol)` → `_gather(as_of=None)` → `_symbol_trades_index_cached`
  (`FMPSenateTraderWeight.py:2723` → `:434`)

Live expert objects are **long-lived singletons**: `ExpertInstanceCache._cache`
(`ba2_trade_platform/core/ExpertInstanceCache.py:29-61`) hands out one instance per
`expert_instance_id` via `get_expert_instance_from_id` (`core/utils.py:77`), used by the live
`run_analysis` dispatch (`WorkerQueue.py:885`, `:1181`). Instances are only dropped on explicit
invalidation (settings change / schedule refresh) or process restart.

Before these memos, live always saw fresh data: `fmp_history_disk_cached` is a straight
passthrough when the TTL freeze flag is off (`packages/providers/ba2_providers/fmp_common.py:151-152`
— "live path: never cache to disk; always pull fresh from the API").

**Effect:** after the first live analysis cycle, the expert scores a **frozen** disclosure feed.
New congressional trades are invisible to every subsequent scheduled live cycle until the app
restarts or someone edits the instance's settings. In live, the expert silently stops doing its
job — no error, no log line, just stale input. The scoring caches (skill/hold/confidence) make
it worse: they were fed by the frozen slice, and their `_save_scoring_cache_throttled` writes
*do* run in live (`is_live=True`), so stale-derived scores are persisted to disk as a side
effect.

The companion `_day_slice_memo` self-heals (keyed by calendar day), and `_fetch_trader_history`
is freeze-gated (live passthrough, `:952-966`) — only the two **trade-feed** memos
(`_all_trades_memo`, `_symbol_trades_memo`) are unbounded.

**Failure scenario:** deploy basket-mode `FMPSenateTraderWeight` live on Monday. A senator
files a disclosure Tuesday; Wednesday's scheduled analysis still scores Monday's feed. Repeat
indefinitely.

**Confidence:** verified by reading (memo has no invalidation path; singleton lifetime; live
passthrough semantics). Not exercised against a live process. Note the memo is *correct* for
backtests (frozen/hermetic data) — the bug is that it is not gated on backtest mode.

---

### H2 — HIGH (backtest ops): prewarm doesn't cover basket-mode `FMPSenateTraderCopy`'s shallow unscoped feed

Task 3 made `FMPSenateTraderCopy` a basket expert (`analyzes_as_basket = True`,
`FMPSenateTraderCopy.py:63`). Its backtest `_gather` fetches the unscoped feed with
`symbol=None` and **no** `full_history` (`FMPSenateTraderCopy.py:150-151`, `:242-248`), which
`fmp_history_disk_cached` stores under the key `"ALL"` (`expert_mixins.py:300`).

The new prewarm `_do_senate_latest` (`testplatform/ba2test_launcher.py`, commit `58a010d`)
warms only the **deep** key `"ALL_FULL_HISTORY"`, and only when `"FMPSenateTraderWeight" in
experts` (`ba2test_launcher.py:~626`).

**Effect:** any real hermetic backtest/GA run that includes basket-mode Copy — with or without
Weight in the same run — raises `FMPHistoryCacheMiss("congress_senate_latest/ALL not
pre-warmed")` on the first bar. Loud (the engine re-raises cache misses), so not a silent
degradation, but a supported configuration that simply cannot run. The Copy engine test
(`test_fmp_senate_trader_copy_produces_recommendations_in_backtest`) passes only because it
monkeypatches the fetchers; nothing tests the real prewarm↔runtime key match for Copy.

---

### M1 — MED (latent): `TradingOrder.get_expert_id()` bypasses the in-mem store

`packages/common/ba2_common/core/models.py:507` does a raw
`session.get(ExpertRecommendation, self.expert_recommendation_id)` (plus a raw `Transaction`
get at `:500`). With `ExpertRecommendation` now in `IN_MEM_MODELS`, this returns `None`
silently whenever the backtest in-mem flag is on. Current callers are live-only
(`ui/pages/overview.py:1619`, `:1732`), so nothing breaks today — but it is exactly the bug
class this changeset fixed in `TradeConditions`/`TradeRiskManagement`, and any future
backtest-side use of `order.get_expert_id()` breaks silently instead of loudly.

### M2 — MED (pre-existing, same bug class): unfixed raw `TradingOrder`/`Transaction` queries that silently no-op in backtests

These pre-date the changeset (those two models were already in-mem) but are the identical hole
the changeset patched in two places and left in several others — now more relevant because the
pattern was explicitly blessed by expanding `IN_MEM_MODELS`:

- `TradeRiskManagement.py:950` — `_update_orders_in_database`: raw
  `session.get(Transaction, order.transaction_id)` → `None` in backtest → the order is dropped
  from `grouped_by_transaction`, so `TransactionHelper.adjust_qty` (`:975`) never runs: no
  transaction/order quantity sync after RM resizing. Reachable every bar via
  `review_and_prioritize_pending_orders` from the engine.
- `TradeRiskManagement.py:1032`, `:1044` — `_delete_unfunded_orders`: raw
  `select(TradingOrder).where(depends_on_order == ...)` and raw `session.get(Transaction, ...)`
  → linked orders/transactions are never found, never deleted in backtest.
- `TradeActions.py:135`, `:1771-1782`, `:2923`, `:2957` — raw `select(Transaction)`/
  `select(TradingOrder)` in ruleset-action helpers (position lookup, options close paths).
- `AccountInterface.py:469` (`_recalculate_transaction_quantity` silently no-ops) and `:750`
  (`_get_transaction_entry_order` → `None`, so `_validate_position_size_limits` is silently
  skipped for `submit_order` in backtest).

Each degrades silently rather than raising. Worth a dedicated pass: either route them through
`orders_where`/`transactions_where`/`get_or_none` or prove each is genuinely unreachable in
backtests.

### M3 — MED (documentation): stale comment now describes the opposite of reality

`TradeRiskManagement.py:415-417` says ExpertRecommendation rows "are still in SQLite (not an
in-mem model)" — false after this changeset. The code below it is still correct (`get_instance`
is routed), but the comment invites a future reader to "simplify" the dual-path branch into a
bug.

### M4 — MED/LOW: `_window_trades` trailing edge is not a faithful superset of `_filter_trades` (intraday runs)

`_window_trades` (`FMPSenateTraderWeight.py:388-401`) keeps rows with
`disclose_date >= now - timedelta(days=max)` (exact datetime); `_filter_trades` keeps rows with
`(now - disclose).days <= max` (integer-day truncation). For daily bars `as_of` is midnight
(`daily_engine.py:518`), so the bounds agree exactly. For **intraday** runs (`as_of` carries a
time component, `daily_engine.py:516`) a trade up to ~1 day beyond the nominal window — which
`_filter_trades` would keep — is dropped by the pre-filter. Direction is conservative (drops,
never leaks), so results only lose a little recall at the window edge; introduced by `e999f52`,
not by this range, but the new docstring's "faithful superset" claim is only exactly true for
daily runs.

---

### LOW

- **L1 — dual-mode `analyze_as_of` drops the attribute-pinning fallback.**
  Old behavior: `context.extra` without `"symbol"` fell back to `self._gather_symbol` — the
  convention `_run_expert_bar` itself uses (`daily_engine.py:821` pins the attribute, does not
  set `extra`). New behavior: symbol-less ctx returns a **list**. No production caller remains
  for Weight (the marker routes it to the basket branch), so this is latent — but the
  docstring's "byte-identical pre-Task-5 behavior" claim is inaccurate for that calling
  convention, and if the marker were ever removed, the classic path would crash on the list.
  (`FMPSenateTraderWeight.py:912-917`)

- **L2 — live basket persists HOLD recommendations in bulk.**
  `_process_all` deliberately includes HOLD/SKIP items (the engine filters them at
  `_recommendation_to_expert_recommendation`); live `_run_basket_analysis`
  (`FMPSenateTraderWeight.py:2605-2620`) persists **all** of them as `ExpertRecommendation`
  rows. Copy does the same, but only for symbols with actual copy-trades; Weight emits one row
  per qualifying symbol — potentially dozens-to-hundreds of HOLD rows per live cycle. The live
  enter loop skips HOLDs downstream, so it's DB noise, not wrong trades.

- **L3 — UI doesn't know the basket state shape.**
  `_run_basket_analysis` writes `market_analysis.state['senate_trade_basket']`
  (`:2632`), but `render_market_analysis` keys off `'senate_trade' in state` (`:2827-2832`), so
  basket analyses render the generic "no data" branch in the analysis-detail UI. Cosmetic.

- **L4 — `is_tradable_stock_ticker` drops real equities.**
  The `^[A-Z]{4,5}X$` heuristic classifies real NYSE tickers such as **MPLX** and **CEIX** as
  mutual funds, so basket mode silently discards congressional trades in them. Kept
  byte-identical to the proven heuristic per the plan — an accepted, but real and previously
  undocumented, recall loss (`packages/common/ba2_common/core/utils.py:914-938`).

- **L5 — same-day disclosures are treated as knowable (residual, by design).**
  The `d20c2a3` fix rejects strictly-future dates; a trade disclosed *on* the as-of date is
  still scored for that day's bar even though real filings often land after market close.
  Inherent to daily granularity; the boundary is codified by
  `test_weight_filter_trades_keeps_same_day_disclosure_and_exec`. Stated so the residual is a
  conscious choice, not an oversight.

---

## What was verified clean (so the scope is explicit)

- **In-mem store wiring for the 3 new models**: seeding (`seed_account_definition`,
  `seed_expert_instance`) goes through routed `add_instance` **inside** the `inmem_trades()`
  context (`backtest_db.py:141-144, 182, 229`); engine re-reads are all routed
  (`daily_engine.py:422, 911, 1127, 1167`); `build_results` consumes account state, not raw
  SQL (`daily_backtest_handler.py:689-690`); file-backed persist runs keep the flag OFF;
  the engine loop is single-threaded and parallel trials each re-enter the context.
- **`TradeConditions.get_previous_recommendations`** in-mem branch: `created_at` is always
  non-None (model `default_factory`) and tz-normalized by the store's `_coerce_enums`, so the
  Python sort/limit is equivalent to the SQL `ORDER BY ... DESC LIMIT`.
- **`TradeRiskManagement._get_orders_with_recommendations`** in-mem branch: correctly uses the
  raise-on-miss `get_instance` contract with per-id catch.
- **Engine basket dispatch**: type guard for non-list returns, per-item guard around
  `_stage_recommendation_candidate`, `raw_outputs['symbol']` required-and-logged, once-per-bar
  call count, single end-of-bar `_size_and_submit_candidates`, cache-miss re-raise semantics,
  and the `manage_ok` fall-through (regression-tested by
  `test_basket_expert_manage_open_positions_runs_when_entry_ok_false`).
- **Lookahead fix (`d20c2a3`)**: applied consistently to `_filter_trades` (both experts),
  `_window_trades`, and `_sliced_history_for_day` (already bounded `<= ceiling`); tz-aware
  throughout (`_parse_ymd_utc` returns aware; engine `as_of` is aware); boundary tests exist
  for both experts, filter and window layers.
- **`is_live` threading in the scoring caches**: all three cached getters receive
  `is_live=(as_of is None)` at every call site; the launcher's skill prewarm is unaffected
  (its final explicit `_save_scoring_cache` flush persists regardless of the new throttled-write
  skip).
- **`is_tradable_stock_ticker` extraction**: exact logical inversion of `_is_junk_ticker`;
  the `build_senate_universe.py` call site uses the positive sense correctly.
- **`unsyncable_reason` (`4dd5aad`)**: porcelain + `@{u}..HEAD` ahead-count logic and failure
  modes (no upstream, git unavailable) are handled; non-blocking by design.
- **Test quality**: the differential tests (`_gather_all` vs `_gather`, `_process_all` vs
  `_calculate_recommendation`), cache-miss isolation tests, non-cache-miss propagation test,
  empty-basket test, engine integration tests for both Copy and Weight, and the lookahead
  boundary suite are real, non-tautological coverage.

**Test gaps corresponding to the findings:** no test covers live feed-refresh across two
analysis cycles (H1 — would require defeating the singleton/memo), and no test asserts the
prewarm key set matches what each basket expert reads at runtime (H2).

## Suggested priority (for the follow-up fix pass — not applied here)

1. H1 — gate `_all_trades_memo`/`_symbol_trades_memo` on backtest mode (or add live TTL).
2. H2 — warm `congress_{chamber}_latest/ALL` for Copy (or move Copy to `full_history=True`).
3. M1/M3 — route `get_expert_id` through the store; fix the stale comment.
4. M2 — dedicated audit/fix pass on the raw order/transaction queries.
5. L1–L5 — doc corrections and deliberate-acceptance notes.
