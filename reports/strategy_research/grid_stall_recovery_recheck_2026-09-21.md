# Recheck of the stall-recovery corrections

**Superseded status:** [Synced review through `7f2f58a5`](synced_grid_option_ui_review_2026-09-21.md) verifies H1–H5 corrections locally, records remaining edge cases and identifies that the active grid checkout still lacks these latest changes. This document preserves the previous review.

Date: 2026-09-21. Code reviewed: **`441771e852de4cb08f4196f1f0563da5614ca181`**. HEAD advanced to `50299b33` during the review; that commit only adds the implementation response.

**Verdict: the original G1/G2 cases are corrected for newly written records, but five defects remain.** The most consequential is that a timed-out remote export can still hold the launcher open and prevent the matrix from advancing. The earlier option UI defects are unchanged; none of their source files changed in this correction.

## Verified improvements

- Top-N selection excludes stalled records by both status and sentinel fitness, including the `best_params` fallback. It returns fewer candidates instead of exporting a known stalled genome.
- Newly recorded all-stalled results produce `no_measurements`; the optimization row becomes failed and the checkpoint-clearing code is bypassed.
- Stalled rows now preserve reason, elapsed time and worker slot.
- Local export runs through a process pool even for a single candidate. Its completion wait has a deadline, and local worker teardown escalates from terminate to kill.
- The matrix has an explicit continuation branch for no-measurement failures.

**48 focused tests passed** across the new review regressions, initial stall tests, pool recycling, checkpoint elites and completed-job skipping. Additional probes below execute actual source functions/blocks with controlled fixtures and reproduce cases the tests do not cover. No expert, rule, sizing or measured-fitness formula changed in this patch.

## Findings

### H1 — P1: remote export timeout still leaves the launcher alive

Locations: [`ba2test_launcher.py:6784`](../../testplatform/ba2test_launcher.py#L6784), teardown at line 6803, and `_remote_then_local` at lines 6458–6481.

Remote work runs inside a `ThreadPoolExecutor`. On timeout, `_kill_executor` only terminates `local_ex` processes; `remote_ex.shutdown(wait=False, cancel_futures=True)` cannot stop an already-running thread. Python waits for those executor threads at interpreter exit. Furthermore, after two remote failures, `_remote_then_local` executes the complete local backtest directly inside that same thread, outside the killable process pool.

The isolated probe used the **actual export block and actual fallback function**, mocked remote connection failures, and a sleeping local fallback. With a 1-second export deadline it printed that TOP2 was dropped, returned with TOP1 saved, then remained alive after 5 seconds. The parent probe had to kill its own test subprocess. A truly stuck fallback would keep the real matrix's `subprocess.run` waiting indefinitely. A slow HTTP call can also outlive a shortened export deadline.

**Fix:** make remote attempts and local fallback cancellable within the export deadline, or isolate the entire attempt in a process that can be terminated. A timed-out attempt must not start a new fallback afterward. Test the launcher process actually exiting after a timed-out remote/fallback attempt, not just the export function returning. This issue concerns remote export; the current campaign's local-only path does not enter it.

### H2 — P2: old stalled checkpoint records still count as successful measurements

Location: [`strategy_optimization_handler.py:1371`](../../testplatform/backend/app/services/strategy_optimization_handler.py#L1371).

`_count_measured` excludes only `status == "stalled"`. The immediately preceding deployed patch wrote `fitness == -3e9` **without a status field**, and those records can survive in checkpoint elites. `_final_status` reports such a record as `completed`, while Top-N correctly excludes it by sentinel. The finalization path can therefore clear an all-stalled legacy checkpoint and mark the optimization completed, making the matrix skip it later.

Reproduced: an old-format stalled record yields `completed`; the same record with `status="stalled"` yields `no_measurements`. Top-N returns zero candidates in both cases.

**Fix:** use one shared measurement predicate for counting and ranking. Recognize both the sentinel and the explicit status while continuing to accept ordinary historical measured records without a status. Add an old-format checkpoint fixture. This is separate from the previously deferred persistence of a complete stall blacklist.

### H3 — P2: queued/server optimizations turn `no_measurements` back into success

Locations: new handler return at [`strategy_optimization_handler.py:2216`](../../testplatform/backend/app/services/strategy_optimization_handler.py#L2216); caller at [`task_queue.py:730`](../../testplatform/backend/app/services/task_queue.py#L730).

The CLI understands the new status, but the main task queue only treats `status="failed"` as failure. Every other status except its special partial branch becomes completed. Strategy optimizations are registered on this inline main queue in `app/main.py`.

Executing the actual `_process_task_inline` method with the new response set the task to **completed**, progress 100%, and left its error message empty. Its optimization row is failed, so the UI and polling clients receive contradictory outcomes. `optimize-batch` polls the task status and can enter the success/export/report path.

**Fix:** keep the existing failed-status contract and add a separate failure-kind field, or update every result consumer to recognize `no_measurements`. Cover the queued/API path as well as the direct CLI.

### H4 — P2: an old failure marker can hide a new fatal launch failure

Locations: [`run_options_matrix.py:159`](../../tools/run_options_matrix.py#L159) and lines 584–585.

After any nonzero subprocess exit, the driver reads the newest row with the same job name. It does not prove that this row was created by the subprocess that just failed. If an earlier run ended with the no-measurement marker and the next launch fails before creating a new optimization row (argument/preflight/import/startup failure), the driver reuses the stale marker and continues instead of surfacing the new failure.

The probe created one old marked row, simulated a fresh failing launch that wrote nothing, and ran the actual driver `main`: job B was launched and the matrix returned 0. The new launch's error was misclassified as another stall-only result.

**Fix:** bind the decision to the exact optimization/attempt that just exited. A distinct exit code or structured subprocess result avoids a name-based historical lookup; alternatively capture the prior row ID and require a newly created matching attempt. Test both fresh marked failures and failures before row creation.

### H5 — P2: missing fitness now crashes Top-N ranking

Location: [`ba2test_launcher.py:6537`](../../testplatform/ba2test_launcher.py#L6537).

The extracted `_rank_measured_candidates` helper calls `_json.dumps` for a record with a missing/non-numeric fitness. `_json` is imported locally inside `_persist_top_backtests`, not in the helper's scope. The module imports `json` under its ordinary name.

Reproduced with `{"params": {"a": 1}, "fitness": null}`: `NameError: name '_json' is not defined`. Existing checkpoint code explicitly tolerates missing fitness, so the extraction breaks a previously handled input. The direct optimize CLI does not catch this export exception; it can stop the matrix.

**Fix:** use a valid import and explicitly decide whether missing-fitness records are exportable measurements. Add a null/missing-fitness ranking test, including mixed valid and invalid rows.

## Validation and scope

- `test_stall_review_regressions.py`, `test_local_pool_stall_recovery.py`, `test_pool_recycle.py`, `test_ga_checkpoint_carries_elites.py`, `test_launcher_skips_completed_optimizations.py`: **48 passed**, two dependency deprecation warnings.
- [Additional isolated probe](../../test_files/recheck_grid_stall_441771e8.py): all five cases above reproduced. It uses temporary fixture data, mocked remote calls, and test-owned subprocesses only. Its assertions document defects, not desired production behavior.
- [Probe evidence](../review_evidence/grid-stall-2026-09-21/recheck-441771e8.json).
- No application implementation edits, production database updates, real backtests, worker deployments, restarts or broker calls were made.
- The broad suite totals in the [implementation response](grid_stall_recovery_review_response_2026-09-21.md) were not rerun here; this recheck uses the focused tests above and independent failure-path probes.
- [Option UI recheck](../ui/option_trade_ui_recheck_2026-09-21.md): its five findings remain open. Source comparison against `80139b93` found no change in the affected UI, opening-leg normalization or chart-provenance files, so their unchanged suites were not rerun.

The ordinary local recovery path is improved. Resolve H1 before treating export as bounded across both local and remote execution; resolve H2/H3/H4 before treating the no-measurement outcome as consistently handled across resume, CLI and queued jobs. H5 is a small regression in the extracted ranking helper.
