# Review: stalled-trial recovery and grid continuation

**Latest recheck:** [Synced review through `7f2f58a5`](synced_grid_option_ui_review_2026-09-21.md). The original G1/G2 and subsequent H1–H5 cases are corrected locally; remaining findings and the active grid checkout's version mismatch are documented there. This document preserves the earlier review and its evidence.

Date: 2026-09-21. Reviewed **`96cb472f5f275d43a4d0033a4f9b52d9c92ce04c`**, initially on the grid server's `stage1-2020` branch at `/home/debian/ba2-grid/repo`. Remote HEAD was `9645f7e9` (the restart note). During this review another session merged it into local `dev` as `e0c5a623` and bumped TEST to `0074` at `49eb23f3`. The handler, fitness module and new test file are identical to the reviewed remote commit; **G1/G2 remain present after that merge**.

**Verdict: the core recovery works, but two important gaps remain.** The actual dispatcher successfully abandons stalled test workers, replaces their slots, and completes subsequent work. However, stalled records are now eligible for Top-N export, and a run containing only stalled trials is incorrectly finalized as completed. Both can undermine the intended grid-continuation behavior.

This is a review, not a deployment or restart. Application code, the running grid, its checkpoints and its database were not modified.

## What happened and what the patch changes

The server's incident record describes the 2026-09-20 **21:54:45 UTC** abort at job 1/16: one pending individual triggered `TimeoutError("local pool stalled")`. The matrix driver correctly propagated that nonzero exit and stopped launching jobs 2–16. Its detached launch had no automatic restart. The exact reason the trial was slow/stuck was not established; the available logs did not identify its working stack or genome.

The operator's recorded choice was to skip the stalled individual, assign a recognizable fitness, and continue. This patch implements that choice for the **local per-slot process-pool path**:

- `STALLED_SENTINEL = -3e9`, distinct from wipeout/zero-trade/low-trade outcomes.
- Terminate the affected worker and replace its single-worker pool without a blocking shutdown.
- Yield a stalled result instead of raising the local-pool timeout.
- Memoize and record that result; call the incremental-result callback.
- Leave measured fitness formulas and backtest execution logic unchanged.

The timeout is **90 minutes without any pending trial completing**, not a 90-minute maximum runtime per individual. A long trial can run much longer while other trials keep completing. The patch does not add equivalent handling to distributed evaluation or the later Top-N rerun phase.

## Findings

### G1 — P1: Top-N export can rerun the very trial the guard abandoned

Locations at the reviewed commit: `strategy_optimization_handler.py:1835–1844` and `ba2test_launcher.py:6531–6550, 6682–6700`.

The new failure branch inserts stalled genomes into the same `all_results` list used to select saved Top-N backtests. `_persist_top_backtests` sorts and deduplicates fitness values without excluding `STALLED_SENTINEL` or requiring a measured result.

The exact remote ranking code selected these five rows in the isolated probe:

```text
TOP1            0
TOP2   -100000000   (low trade)
TOP3  -1000000000   (zero trade)
TOP4  -2000000000   (wipeout)
TOP5  -3000000000   (STALLED — never measured)
```

This occurs when fewer than five distinct measured fitness values are available; all-stalled searches also select a stalled candidate. Such candidates have no buffered successful result, so export reruns them. That phase uses `as_completed(futs)` without a timeout, or a direct synchronous rerun for a single remaining candidate. It does not use the new stall guard. **The same trial can therefore hang the grid again after the GA successfully recovered.**

**Correction:** exclude non-measurements from both Top-N selection and its `best_params` fallback. Keep stalled rows for diagnostics, with explicit status/reason rather than relying only on their numeric score. Return fewer saved candidates when necessary. Add a regression with fewer than five valid candidates plus a stall, and another with only stalls. Independently bound the export workers' execution and teardown so an otherwise valid candidate that hangs on rerun cannot stop the matrix indefinitely.

### G2 — P1: an all-stalled search reports success and deletes its checkpoint

Locations at the reviewed commit: `strategy_optimization_handler.py:1840–1844, 2121–2148`.

The existing trust guard interprets nonempty `all_results` as evidence of a successful trial. The patch adds unsuccessful stalled records to that list without updating the guard.

Executing the actual finalization code with one stalled record reproduced:

```text
status = completed
best_fitness = -3000000000
checkpoint cleared = true
successful measurements = 0
```

The matrix then sees a completed experiment and can skip it on later runs, despite having measured no candidate. G1 can also cause an immediate rerun of its supposed winner.

**Correction:** track measured-result count independently from diagnostic records. With no measured results, preserve a visibly unsuccessful/no-measurement outcome and do not export a winner or clear useful recovery state. Define the matrix's continuation policy for that outcome so correcting the success flag does not simply reintroduce the original whole-grid stop. Fatal data/configuration errors must retain their distinct handling.

## Remaining operational limits

- **Resume does not preserve the stall blacklist/count in the new run.** The memo is in memory; checkpoint result history carries only the top 20 elites. The probe confirmed that a stalled row drops out once there are 20 better results. A resumed optimization can therefore retry the same genome and its new `all_results` count excludes earlier stalls. An archived optimization row can still retain the earlier record. If campaign-wide counts and no repeated timeouts across restarts are required, persist stalled keys and attempt metadata separately from elites.
- **The incident note's counting SQL is not executable as written.** `all_results` is a JSON column on `strategy_optimizations`, not a table. A read-only SQLite query for the count in one row is:

  ```sql
  SELECT count(*) AS stalled_count
  FROM strategy_optimizations AS o,
       json_each(o.all_results) AS r
  WHERE o.id = :optimization_id
    AND json_extract(r.value, '$.fitness') = -3000000000;
  ```

- **A timeout does not prove that a genome is intrinsically invalid.** This policy intentionally skips unmeasured work to keep the campaign moving. Keep the label distinct from real zero-return results; the current stored row sets trades/return/drawdown to zero and drops the timeout reason. Persisting `status`, elapsed time, slot, key and reason would make diagnosis and later controlled retries clearer.
- **Worker termination is best effort.** `abandon_slot` calls `terminate` and does not verify exit or escalate to a bounded kill. The ordinary sleeping-worker cases passed; this review did not establish recovery from an unkillable OS/I/O state or blocked executor teardown. Do not read the passing tests as that broader guarantee.

## Validation

| Check | Result |
|---|---|
| Patch's six tests plus existing pool-recycling regressions, temporary source overlay | **21 passed** |
| Actual dispatcher with one stalled test worker, then a normal trial | Recovered in **2.109 s** with a 2 s test timeout; normal trial completed; old worker exited. |
| Actual dispatcher with two stalled test workers, then a normal trial | Recovered in **2.125 s**; both old workers exited; normal trial completed. |
| Actual batch-fitness callback/memo path | Same stalled genome requested twice; only one dispatch, one diagnostic record. |
| Actual finalization with only stalled results | Incorrectly completed and cleared checkpoint: **G2 reproduced**. |
| Exact remote Top-N selection | Chose stalled candidate for rerun: **G1 reproduced**. |
| Checkpoint elite carry-over | Stalled record excluded from resumed `all_results` once out of top 20. |

The probes execute unchanged source AST nodes with fixture dependencies; no backtest or market-data provider is invoked. Worker processes run only small sleep/return functions created by the probe. The full trading/backtest suite was not rerun for this review.

At **08:11 Paris / 06:11 UTC**, the read-only server snapshot showed optimization **11 running**, no newly recorded result yet, and checkpoint generation **6**, `partial=true`. The driver PID was present in the preceding process check. The restart record says it resumed at generation 7; this check establishes that the process/recovery state exists, **not** that a real production stall has already passed through the new recovery code.

## Evidence and follow-up

- [Exact reviewed patch](../review_evidence/grid-stall-2026-09-21/96cb472f.patch).
- [Read-only server status](../review_evidence/grid-stall-2026-09-21/grid-status.json).
- [Probe results](../review_evidence/grid-stall-2026-09-21/probe-results.json).
- [Dispatcher, memo, finalization and export probes](../../test_files/review_grid_stall_20260921.py).
- [Isolated test-overlay runner](../../test_files/run_grid_stall_review_tests_20260921.py).

Fix G1/G2 before considering the merged patch the complete solution. Add end-to-end optimizer/export fixtures for those two cases; the patch's new source-string assertions do not exercise either boundary. The merge and version bump were observed, not performed by this review; no worker deployment or restart was requested or attempted here.
