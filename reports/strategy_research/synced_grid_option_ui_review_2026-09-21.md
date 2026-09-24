# Synced grid recovery and option UI review

Date: 2026-09-21. Local code reviewed: **`7f2f58a5` on `dev`**, including grid corrections `1adbab3d` and option UI corrections `ce0eb688`.

**Implementation follow-up, 2026-09-21:** S1–S3 below are fixed in this change. The user initially chose **implementation and testing locally only**, then requested commit and push. The grid checkout, running processes and production databases were not changed. The earlier review and its reproductions are retained below as history. Validation of the corrections is recorded at the end of this document.

The earlier ten defects are substantially addressed by the synced code. This report supersedes the current-status conclusions in the [previous grid recheck](grid_stall_recovery_recheck_2026-09-21.md) and [previous UI recheck](../ui/option_trade_ui_recheck_2026-09-21.md).

## Deployment gap: the running grid uses older files

At **11:24 UTC / 13:24 Paris**, the matrix process (PID 746315) and its optimization launcher (PID 746465) were running from `/home/debian/ba2-grid/repo`. That checkout's launcher lacks `_submit_daemon`, `_run_local_fallback_bounded` and `_new_local_pool`; its handler lacks `NO_MEASUREMENT_KIND` and the shared measurement predicate; its driver lacks the launch-ID boundary check. Normalized file hashes also differ from local.

Therefore syncing this Windows checkout has **not** put H1–H5 onto the active grid checkout. The remote checkout has modified files, so its Git HEAD alone is not an adequate version check. The current campaign uses the local execution path, so the old remote-export hang is not evidence that this particular job is currently stuck; the other recovery/status changes are still missing there.

Before calling this deployed, reconcile the grid checkout with the reviewed code and arrange process reload at an appropriate job boundary. Copying Python files does not update functions already loaded by the current launcher. No server changes or restarts were made in this review.

[Read-only server evidence](../review_evidence/synced-7f2f58a5-2026-09-21/remote-code-check.json).

## Findings at `7f2f58a5` — corrected locally

### S1 — P2: Options table sorting uses the wrong keys and can hide the largest winner on another page

Locations: [`option_trades.py:303`](../../ba2_trade_platform/ui/pages/option_trades.py#L303), [`LiveTradesTable.py:101`](../../ba2_trade_platform/ui/components/LiveTradesTable.py#L101), and [`LazyTable.py:400`](../../ba2_trade_platform/ui/components/LazyTable.py#L400).

Quasar sends the column **name**, such as `current_pnl`, `closed_pnl`, `expiry` or `legs`. The options loader tests for field names such as `current_pnl_numeric`, `expiry_display` and `leg_count`. Consequently, a normal P&L header click takes the SQL pagination branch, selects a page by creation date, and sorts only that page using formatted P&L text. The global numeric sort is never reached. Account and strategy sorting also lack a mapping; direction is only sorted within the selected page.

**Actual-class / real-SQLite reproduction:** three open transactions have P&L of $900, $200 and $100, oldest to newest. With a two-row page and descending `current_pnl`, page 1 returns **$200 and $100**, leaving the $900 winner elsewhere. Passing the internal field name instead returns the correct **$900 and $200**. Strategy sorting returns creation order instead of strategy order.

**Correction:** normalize column names to row fields/SQL expressions before deciding whether to sort in SQL or memory. Sort numeric P&L across the entire filtered result before slicing. Test using the names actually sent by the UI, with the winning row outside the initial page.

### S2 — P2: invalid-record filtering still happens after ranking has already touched invalid data

Location: [`ba2test_launcher.py:6628`](../../testplatform/ba2test_launcher.py#L6628); predicate at [`strategy_fitness.py:49`](../../testplatform/backend/app/services/strategy_fitness.py#L49).

`_rank_measured_candidates` sorts the unfiltered records before calling `is_measured_result`. A valid numeric score mixed with a string score raises `TypeError`; a non-dict diagnostic row raises `AttributeError`. These are precisely classes of input that the new predicate intends to reject. The null-score case covered by the new tests is fixed, but this broader rejection contract is not.

The fallback also bypasses the predicate: an empty result list plus `best_params` and `best_fitness=None` returns a candidate with no measured score. NaN and infinity currently pass the shared predicate as measurements.

**Scope:** reproduced with synthetic malformed/legacy result inputs; no such record was established in the running campaign. This is a remaining recovery-hardening issue, not evidence that normal finite GA results rank incorrectly.

**Correction:** filter before sorting; validate the fallback with the same finite-score predicate. Keep legitimate finite sentinel outcomes for zero trades or account wipeout intact, while continuing to reject the stalled sentinel.

### S3 — P2: six persistence regression tests are still broken by the process-pool transition

Location: [`test_persist_top_backtests.py:109`](../../testplatform/backend/tests/test_persist_top_backtests.py#L109) and the analogous worker mocks in that file.

The unchanged test run has **six failures**, all from trying to pickle locally defined lambda worker mocks after the exporter moved to a spawn process pool. These tests cover persisted results, fixed expert settings and entry rules, so leaving them red weakens useful regression coverage.

A diagnostic run replacing only this test module's pool factory with a `ThreadPoolExecutor` passes **all 14 tests in the file**, including the six failures. This supports a fixture incompatibility rather than a demonstrated production persistence bug. It does not make the unmodified suite green, and it does not replace real process-pool timeout tests.

**Correction:** adapt the persistence unit fixtures at the executor boundary; retain separate real-process coverage for spawning, timeout and teardown.

## Corrections verified locally

| Previous finding | Synced implementation / verification |
|---|---|
| Grid H1: remote export keeps launcher alive | Daemon remote attempts, deadline checks and a killable fallback pool. Existing process-exit test passes. An additional probe exited in **0.70 seconds** with a live fallback child and a 0.4-second deadline; the task would otherwise sleep for 8 seconds. |
| Grid H2: old stalled sentinel counted as measured | Counting and ranking now recognize both the sentinel and status marker. |
| Grid H3: no-measurement queue job looks completed | Handler returns `status="failed"` plus `failure_kind="no_measurements"`; real queue test passes. |
| Grid H4: stale marker masks a fresh launch failure | Driver binds marker lookup to rows created after the pre-launch maximum optimization ID. |
| Grid H5: missing score hits an undefined alias | Null-score case fixed. Broader malformed-input issue remains as S2. |
| UI N1: single-leg row empties the table | `_contract_quote` is an instance method; real tab and loader tests pass. |
| UI N2: SQLAlchemy provenance lookup fails | Uses the backend session's `get`; real SQLAlchemy provenance test passes. |
| UI N3: 500-row totals cap truncates browsing | Separate totals and browse queries; real 501-row pagination checks pass. Sorting still needs S1. |
| UI N4: incomplete spread becomes a different payoff | Missing executed fill data marks the structure incomplete; payoff and live valuation refuse it. |
| UI N5: cross-account / inconsistent quote reuse | Quote wrapper caches by account and contract and is passed into the shared pricing seam and Current column. |

The final `_new_local_pool` import-scope repair at `7f2f58a5` is present. The reviewed diff does not change expert signals, trade rules, position sizing, or numerical backtest fitness formulas.

## UI delivery gaps still open

- Historical contract detail remains **SQLite-only**; the backend explicitly reports unavailable for other recorded store types.
- The live popup still lacks the requested moneyness display, numeric breakeven/max-profit/max-loss summary and user-facing overlay/marker toggles. Builder boolean arguments alone do not provide visible controls.
- **Corrected in this follow-up:** the table now renders `pnl_reason` in a tooltip, and preserves a missing-quote reason from the shared pricing seam. Missing P&L remains unknown and sorts after measured results in either direction.
- The earlier visible-range shading, observation/adjustment context and focus-handling gaps were not addressed by this sync. The unchanged frontend browser checks were not repeated here.

## Validation and evidence

- Live option P&L, details, structure chart, lifecycle, tabs and loader: **87 passed**.
- Backend stall recheck, recovery, recycling, persistence, checkpoints, completed-job skipping and trade-chart context: **108 passed, 6 failed**. The six failures are detailed in S3.
- Diagnostic persistence run with only the executor fixture adapted: **14 passed**. Reported separately, not counted as an unmodified-suite pass.
- [Offline probe](../../test_files/recheck_synced_7f2f58a5.py) and [results](../review_evidence/synced-7f2f58a5-2026-09-21/offline-probes.json): actual options loader with an in-memory database; actual ranking helper with controlled malformed inputs.
- [Process-exit probe](../../test_files/review_bounded_fallback_7f2f58a5.py) and [results](../review_evidence/synced-7f2f58a5-2026-09-21/bounded-fallback.json): actual daemon/fallback/teardown helpers with a real spawn pool and a sleeping task, substituting only worker initialization and payload.

The original review changed documentation and isolated probes only. Its offline probes intentionally assert the old defects and are historical evidence, not tests to run against the corrected implementation.

## Local corrections

- **S1:** resolve UI column names before selecting SQL or in-memory sorting. Computed values sort over all matches before pagination; account, strategy, expiry, legs and direction use their actual row fields. SQL sorts preserve raw timestamp precision, place nulls last and break ties by transaction ID. Tests cover both P&L headers/directions, strategy order across pages and unknown P&L versus gains/losses.
- **S2:** filter diagnostic records before comparing scores. Reject nonnumeric/nonfinite/overflowing scores, invalid genomes and an unmeasured best-params fallback. Real finite zero-trade, low-trade and wipeout outcomes remain valid measurements; numerical fitness formulas are unchanged.
- **S3:** persistence tests mock the executor boundary so their lambda workers and captured calls remain in-process. Real-process tests separately exercise the export pool's actual initializer, fallback timeout and teardown.

No database migration is required. Release versions: **APP `2026.09.1184`, TEST `2026.09.0076`**. Commit/push was subsequently requested; the grid checkout and production processes are not being restarted or redeployed. The UI feature-delivery items listed separately above remain planned work rather than being included in this bug-fix patch.

### Validation after the corrections

- **94 live tests passed** across option P&L, contract details, charts, lifecycle, tabs and loader regressions.
- **1,216 backend tests passed**, including the previously failing persistence tests, malformed-result cases, actual process-pool initialization and recovery, frozen equity/option fitness scores, and the live/backtest golden parity gate.
- `git diff --check` passed. These checks use test databases and mocked/synthetic payloads; no production account or historical backtest record was modified.
