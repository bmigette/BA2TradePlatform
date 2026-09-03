# warm_options_history: chunked (streamed) plan — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop `tools/warm_options_history.py` from holding the contract metadata of the entire
universe in memory: plan and fetch the symbols in small chunks so the process stays at tens of MB
of plan state instead of 10+ GB, without changing what gets written or the tool's tested behaviour.

**Architecture:** `build_plan` already works on any symbol sequence; `run_units_concurrent` already
works on any `Plan`. `main()` therefore iterates the symbol list in chunks of
`--plan-chunk-symbols` (default 8): build the chunk's plan → run it → merge its counts into an
aggregate `Plan` (which never keeps units) → next chunk. `--limit` becomes a global budget across
chunks. Dry-run streams the same way and prints the aggregate. Nothing changes below `main()`.

**Tech Stack:** Python, dataclasses, the existing `tests/test_warm_options_history.py` fixtures
(`provider`, `store`, `_run`).

---

## 0. Context (zero-context implementer — read first)

* Measured 2026-09-03 on the live 857-symbol ThetaData backfill: the process reached **6 GB private
  after 10 min and 10.3 GB a few minutes later while still in `build_plan`** (no bar fetched yet), and
  17.9 GB after 14 h of fetching at `--concurrency 6`. The two GA grids sharing the box were starved
  (0.9 % free RAM). Fetched bars are NOT retained — `run_units` writes each unit with
  `store.write_partition` and drops it — the memory is `Plan.units`: one `WorkUnit` per pending
  (underlying, expiry) with its full `List[OptionContractMeta]` (every strike × right), ~49,640 units
  for the universe, built eagerly for ALL symbols before the first fetch (`build_plan`, ~line 448).
* Files: `tools/warm_options_history.py` (`WorkUnit`/`Plan` ~line 72-100, `build_plan` ~448,
  `run_units` ~551, `run_units_concurrent` ~631, `print_plan` ~702, `main` ~739). Tests:
  `tests/test_warm_options_history.py` (60 tests; fixtures `provider`/`store`, helper `_run(provider,
  store, argv)` returning `(rc, lines)`; `warm.last_plan()` is asserted in the dry-run test (~line 158:
  `units_pending == 2`, `units_done == 1`) and the discovery-failure test (~line 653:
  `discovery_failed`, `per_symbol`)).
* Run tests from the repo root with the TRADE venv: `.venv\Scripts\python.exe -m pytest
  tests/test_warm_options_history.py -q` (all 60 must stay green). Also run
  `packages/providers/tests/test_options_data_providers.py -q` with
  `C:\Users\basti\ba2-venvs\test\Scripts\python.exe` (unaffected, sanity).
* This is a `tools/` + `tests/` change only → **no version bump**. Work in a worktree off `dev`:
  `git worktree add ../BA2-warm-chunks -b fix/warm-options-chunked-plan dev`. Commit with the
  attribution footer your session instructs.

---

## Task 1: Failing tests

**File:** `tests/test_warm_options_history.py` (append; reuse the module's existing fixtures and
`_run`; look at how existing tests seed `provider` contracts and `store` partitions — e.g. the dry-run
tests ~134-180 and the `--limit` tests ~494-510 — and seed the same way).

Add, with these exact names and intents:

1. `test_main_plans_in_symbol_chunks_and_never_holds_more_than_one_chunk_of_units(provider, store, monkeypatch)`
   — seed 5 symbols with 2 pending expiries each; monkeypatch `warm.run_units_concurrent` with a
   wrapper that records `plan.units_pending` per call and then delegates to the real function; run
   `main` with `--plan-chunk-symbols 2`; assert it was called 3 times (2+2+1 symbols) with
   `[4, 4, 2]` units, every partition got written (10 manifests), and `warm.last_plan().units == []`.
2. `test_last_plan_aggregates_counts_and_per_symbol_across_chunks(provider, store)` — same seeding
   plus one already-complete partition and one already-empty; `--plan-chunk-symbols 2`; assert
   `last_plan().units_pending`, `.units_done`, `.units_empty`, `.contracts_pending` equal the totals
   and `.per_symbol` has all 5 symbols.
3. `test_limit_is_a_global_budget_across_chunks(provider, store)` — 3 symbols × 2 pending units,
   `--plan-chunk-symbols 1 --limit 3`; assert exactly 3 partitions were written and discovery for
   the 3rd symbol was NOT needed beyond the budget (i.e. `last_plan().units_pending == 3`).
4. `test_dry_run_across_chunks_reports_totals_without_retaining_units(provider, store)` — 3 symbols,
   `--plan-chunk-symbols 1 --dry-run`; assert the TOTAL line reports the summed units/contracts, the
   "would write:" line still names the FIRST unit's file, and `last_plan().units` holds at most that
   one unit.
5. `test_discovery_failure_in_one_chunk_does_not_abort_later_chunks(provider, store)` — mirror the
   existing BADCO test (~line 640-660) but with `--plan-chunk-symbols 1` and a good symbol AFTER the
   bad one; assert the good symbol's partitions were written and `discovery_failed` is merged.

Run: `.venv\Scripts\python.exe -m pytest tests/test_warm_options_history.py -q -k "chunk or budget_across"`
Expected: FAIL (unknown `--plan-chunk-symbols`; `last_plan().units` not empty; etc.). Commit
`test(warm-options): pin chunked planning behaviour`.

## Task 2: Implementation

**File:** `tools/warm_options_history.py`

1. `Plan`: replace the two `@property`s with plain counter fields `units_pending: int = 0` and
   `contracts_pending: int = 0`, maintained by `build_plan` when it appends a unit (`+1`,
   `+len(contracts)`). Add `def absorb(self, other: "Plan") -> None` that sums the four counters,
   updates `per_symbol` and `discovery_failed`, and keeps `self.units` as-is (the aggregate never
   copies units; it keeps at most the first unit it ever saw, for `print_plan`'s "would write" line).
   Grep for every use of `units_pending`/`contracts_pending` (print_plan, main, tests) — they keep
   working as attributes.
2. `build_plan(..., budget: Optional[int] = None)`: take the budget as a parameter (default: derived
   from `ns.limit` exactly as today so single-chunk callers are unchanged) so `main` can pass the
   REMAINING global budget per chunk; stop early when it is spent (existing `break`).
3. CLI: `--plan-chunk-symbols` (int, default 8, `>= 1`) with a help string that states the WHY (plan
   memory ~10+ GB for 857 symbols when built eagerly; each chunk's contract lists are released after
   it is fetched).
4. `main()`: replace the single `build_plan` + `run_units_concurrent` with a loop over
   `[symbols[i:i+n] for i in range(0, len(symbols), n)]`: build the chunk plan with the remaining
   budget; `aggregate.absorb(chunk_plan)`; if not dry-run: log
   `plan chunk k/K: <first>..<last> — U units pending (cumulative: done D, written W, failed F)` then
   `stats_total.merge(run_units_concurrent(chunk_plan, ...))` (add a tiny `RunStats.merge`); stop
   the loop when the budget is exhausted. Set `_LAST_PLAN = aggregate` (dry-run: after the loop;
   also before `print_plan`). Keep the existing `store root`/`window` lines, the discovery-failed
   summary (now from the aggregate, after the loop), and the final `done:` line and return codes
   byte-identical in format.
5. Dry-run: same loop without running; `print_plan(aggregate, ...)` — it must not touch
   `plan.units[0]` when `units` is empty (already guarded by `if plan.units`).

Run: `.venv\Scripts\python.exe -m pytest tests/test_warm_options_history.py -q` → **65 passed**.
Then `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest packages/providers/tests/test_options_data_providers.py -q` → green.
Commit `perf(warm-options): plan and fetch in symbol chunks so the universe never sits in RAM`.

## Task 3: Prove it on the real universe (read-only)

From the repo root with the TEST venv (thetadata is installed there):
`C:\Users\basti\ba2-venvs\test\Scripts\python.exe tools/warm_options_history.py --provider thetadata --db C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite --symbols-file tools/options_universe_full.txt --start 2020-01-01 --dry-run --plan-chunk-symbols 8`
while sampling the process tree's private bytes every 30 s (PowerShell `Get-CimInstance Win32_Process`
on the python children — the venv `python.exe` is a stub, measure its child). Expected: the dry run
completes with the same TOTAL units as the plan phase used to report and the peak stays **well under
1 GB**. Paste peak and total in your report. Do NOT run without `--dry-run` — the operator relaunches.

## Task 4: Merge

Ask the user before merging into `dev` (the operator relaunches the backfill afterwards under the
existing watchdog, which then acts only as a safety net).
