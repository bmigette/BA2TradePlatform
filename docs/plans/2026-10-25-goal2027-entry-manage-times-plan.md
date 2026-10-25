# goal2027 item 2.4: separate entry and manage times (implementation plan)

Design source: `docs/plans/2026-10-25-goal2027-grid-design.md` section 2.4. Branch
`feat/goal2027-entry-manage-times` (stacked on `feat/goal2027-sizing-gene`).

## Current same-minute order (evidence, established before any change)

Both sides run ENTRY first today. Not what the design wants (manage first).

**Backtest.** `testplatform/backend/app/services/backtest/daily_engine.py`, the per-expert loop (~l.867-937):
`entry_ok` and `manage_ok` are two gates on one bar; the entry pass (`_run_bypass_expert_bar`,
`_run_basket_expert_bar` or `_run_expert_bar`) is called first and `if manage_ok: self._manage_open_positions(...)`
last. The decision-time plan (`docs/plans/2026-10-07-decision-time-gene.md`, "Pass order at one time") records the
same: "entry runs before `_manage_open_positions`".

**Live.** Two layers, both entry first.
1. `JobManager._execute_scheduled_analysis` (OPEN_POSITIONS branch) calls
   `WorkerQueue.defer_open_positions_if_entry_in_flight`: the exit expansion is PARKED while this expert has an
   ENTER_MARKET task in flight, and `release_deferred_open_positions` submits it only after the entry orders are
   processed (hook in `_process_expert_recommendations`, commit e5e37c6a). A 15-minute safety timer releases it.
2. `ExpertPriority._next_action` picks the ready order-processing callback with `min(run.ready, key=str(case))`;
   `"AnalysisUseCase.ENTER_MARKET" < "AnalysisUseCase.OPEN_POSITIONS"`, so entry sorts first (comment: "Entry-before-manage").
Pinned today by `tests/test_open_positions_waits_for_entry.py` and `tests/test_expert_priority.py`
(`test_completed_analysis_still_parks_exit_pass_until_priority_processing`). This matches the owner's observation
(open-positions after enter-market when both share a time). Note `_execute_scheduled_group` itself only orders jobs by
(expert priority, symbol) at SUBMISSION; it does not decide the processing order.

## Decisions

- **One definition of the order.** `ba2_common.core.schedule_genes.SAME_MINUTE_PASS_ORDER = ("open_positions",
  "enter_market")` and `order_same_minute_passes(passes)`. The BT engine and `ExpertPriority` both call it (one shared
  function, two callers); a parity test pins both, plus the constant itself.
- **BT:** the per-expert loop runs the passes in that order (manage, then entry). Bypass experts have no manage pass
  (unchanged); basket and classic experts get manage first.
- **Live:** stop parking the exit pass behind the entry pass (JobManager no longer calls
  `defer_open_positions_if_entry_in_flight`, no safety timer) and make `ExpertPriority` release the OPEN_POSITIONS
  processing callback before ENTER_MARKET. Order PROCESSING is already gated behind all of the run's analysis tasks
  (a run's `ready` callbacks fire only when `run.tasks` is empty), so exits and entries are analysed in parallel and
  only their order processing is sequenced; in smart-RM mode the exit pass's SRM task joins `run.tasks`, so entry
  processing still waits for it. The group order is logged at INFO (`[ORDER] expert E batch B: same-minute pass
  order: open_positions -> enter_market`). The WorkerQueue parking methods stay (now unused by the scheduler); see
  Open questions.
- **Genes (opt-in):** `ba2-test optimize --decision-times split` (and the capband driver's `--decision-times split`)
  replaces `schedule:time` by `schedule:entry_time` and `schedule:manage_time`, each the default 7-value list
  (`DEFAULT_DECISION_TIME_CHOICES`, `choice`, stratified, appended after the day genes in that order). Mutually
  exclusive with `schedule:time` by construction (one flag value) and refused when a hand-written config carries both.
  Name token `timesplit` (launcher requires it; driver appends `-timesplit`). Old names and memo keys unchanged
  (no flag -> no new gene). FactorRanker (bypass) and option jobs are refused/ignored exactly as for `timegene`.
- **Decode / trial config:** `decode_params` returns `schedule_entry_time` / `schedule_manage_time`;
  `_schedule_times_from_gene` becomes `_schedule_times_from_decoded` and returns `{"entry": [e], "manage": [m]}`;
  entry goes to `run_schedule_override.times`, manage to `manage_schedule_override.times`.
- **Reconstruction / export:** `schedule_override_from_genes` returns the ENTRY time for a split genome;
  `manage_times_from_genes` returns the manage time; `backtest_export` adds `execution.manage_times` ONLY for a
  split genome (old payloads byte-identical). `_derive_export_payload` refuses a row whose optimization searched the
  split genes but whose genome lacks either. The re-run path (`rerun_handler`, `rebuild_config_for_backtest`) and
  `_gene_params` already pass any `schedule:*` key; they get the split times via the same helpers.
- **Deploy:** `tools/import_deploy_payload.py` writes `execution_schedule_open_positions.times = manage_times` (else
  the entry times, as before) and applies `live_deploy_time_refusal` to BOTH lists before any write.
- **Versions:** packages (`ba2_common`) change -> bump `PACKAGE_VERSION` and raise its REQUIRED minimum; testplatform
  -> `TEST_APP_VERSION` 0152; `ba2_trade_platform` change -> `APP_VERSION` +1.

## Tasks (TDD, one commit each)

1. Shared order + BT engine + live ExpertPriority/JobManager, with the parity test.
2. Genes: schedule_genes helpers, `collect_param_space`, `decode_params`, trial config, canonical/memo.
3. Launcher flag `split`, name token, driver passthrough (capband; senate refuses).
4. Export (`manage_times`, refusal) and `import_deploy_payload` (both times, both refusals).
5. Versions, `check_package_versions`, regression runs vs a detached origin/dev worktree.
