# Response: G1/G2 addressed (stall-recovery review)

Date: 2026-09-21. Response to `reports/strategy_research/grid_stall_recovery_review_2026-09-21.md`
(review of `96cb472f`). Both P1 findings were reproduced by that review's probes and are now fixed;
the operational limits are addressed or explicitly deferred below.

Laptop `dev` = **`441771e8`** (pushed). Box run branch `stage1-2020` = **`f1afb676`**,
`TEST_APP_VERSION` 0065 → 0066. Tests: `test_stall_review_regressions.py` (10 new) — **2603 passed**
on the laptop, 34 of the affected set on the box's venv.

## G1 — Top-N export could re-run the trial the guard abandoned → FIXED

A stalled record carries a sentinel score and **no buffered result**, so selecting it made the export
re-run it with no timeout of its own (`as_completed` had none, and `with ProcessPoolExecutor` +
`shutdown(wait=True)` would block on the hung worker regardless of the loop). Two changes:

- Selection extracted to **`_rank_measured_candidates`** (launcher). It drops non-measurements
  (`status == "stalled"` or `fitness == STALLED_SENTINEL`), **returns fewer candidates** rather than
  padding with them, and the `best_params` fallback no longer resurrects a sentinel best — so an
  all-stalled search exports **nothing**.
- The export phase is **bounded** by `BT_LOCAL_STALL_TIMEOUT_S`: `as_completed(futs, timeout=…)`,
  unfinished candidates are dropped and reported, and teardown goes through **`_kill_executor`**
  (terminate → bounded grace → SIGKILL) instead of `shutdown(wait=True)`. The old unbounded
  synchronous single-candidate branch is gone, so every candidate is inside the bound.

Regressions: `test_topn_selection_skips_stalled_records_and_returns_fewer_candidates` (4 valid + 1
stall → exactly four) and `..._of_an_all_stalled_search_returns_nothing_not_the_sentinel`, plus a
source check that the export is bounded and never uses the `with` form.

## G2 — an all-stalled search reported success and deleted its checkpoint → FIXED

- Completion is decided by **`_final_status` / `_count_measured`**. The count is **derived from the
  records** (`status != "stalled"`) rather than hand-maintained: the first cut of this fix used a
  counter, missed the **second append site** (the local-trial path) and produced
  `"11 record(s), 0 measured"` plus an `UnboundLocalError` on the path that never incremented it.
  Both are now impossible by construction, and `test_records_without_a_status_count_as_measured`
  pins it.
- A no-measurement search is recorded `failed` with **`NO_MEASUREMENT_MARKER`** and its **checkpoint
  is PRESERVED** (the `_clear_checkpoint` call is downstream of the guard, so it is simply not
  reached).
- **Matrix continuation policy:** `tools/run_options_matrix.py` now reads the job's `error_message`
  and, on that marker, prints a SKIP line and **continues to the next job** instead of returning a
  campaign-stopping exit code — so correcting the success flag does not reintroduce the 2026-09-20
  whole-matrix stop. `test_no_measurement_marker_is_identical_in_handler_and_matrix_driver` pins the
  two literals together, and `test_the_matrix_driver_skips_a_no_measurement_job_instead_of_stopping`
  pins the behaviour.

## Operational limits from the review

| Limit | Status |
|---|---|
| Stalled rows had only a numeric score, zeros for trades/return/dd, no reason | **Fixed** — records now carry `status`, `stall_reason`, `stall_secs`, `stall_slot`. |
| Worker termination was best effort (`terminate`, no verification/escalation) | **Fixed** — `abandon_slot` waits a bounded grace, then SIGKILLs survivors; covered by a test whose worker ignores SIGTERM. |
| The counting SQL in the incident note was not executable (`all_results` is a JSON column) | **Fixed** in the docs — `json_each(o.all_results)` with `json_extract(r.value,'$.fitness') = -3e9`. |
| Resume does not preserve the stall blacklist across restarts (in-memory memo; checkpoint carries only the top-20 elites) | **Deferred, offered** — needs stalled keys persisted separately from elites. |
| A timeout does not prove a genome is intrinsically invalid | Acknowledged — the sentinel is distinct from real zero/low/wipeout outcomes and the reason is recorded, so later controlled retries are possible. |
| No end-to-end optimizer/export fixture | Partly addressed — the two boundaries now have unit-level regressions plus source assertions; a full end-to-end export fixture (running the optimizer through a stall and the export) is still worth adding. |

## Correction to the incident note (not a review finding)

The 2026-09-21 restart note said gen 7's completed individuals were lost. **They were not:** the
checkpoint stores `fitnesses` (len 200), so the resume re-ran only the **5 unresolved individuals**
(`gen 7/60 ind 4/5`). Those five are slow-but-progressing (1332 / 1435 / 1504 / 1811 s), which also
refines the diagnosis of the 21:54Z abort: a **cluster of slow tail individuals**, not a single
pathological genome.

## Deployment note

The driver that was running when this landed (pid 746315) holds the pre-fix code in memory, so the
fix applies from the **next driver start** (job 2 onward). No restart was performed, and the running
job is not exposed to either P1: it has hundreds of measured results (so no stalled candidate can
enter its top-5) and an all-stalled outcome is impossible for it.
