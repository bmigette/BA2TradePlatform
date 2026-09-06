---
name: project-open-tasks-2026-09-06
description: "Open task list as of 2026-09-06 — branch fix/audit-2026-09-06 waiting on the grid, plus remaining audit findings and follow-ups"
metadata: 
  node_type: memory
  type: project
  originSessionId: 0a8e0c7d-7633-40aa-8bbf-71d8d726806a
  modified: 2026-09-06T13:21:32.688Z
---

Everything below is OWED as of 2026-09-06. Delete lines as they land; delete the file when empty.

## Blocked on the goal2020 grid finishing (81/116 done, 2 running, 30 failed)

1. **Merge `fix/audit-2026-09-06`** (3 commits: ATR gene swap, delta-at-entry, ML leakage).
   Then in ONE step: bump `TEST_APP_VERSION`, commit, push, and run
   `tools/migrate_atr_budget_swap.py --apply`. The code fix and the DB migration MUST land
   together — a migrated genome only reproduces on the fixed wiring, so either alone leaves the
   two disagreeing. The tool has `--revert` for that hazard (it was applied and reverted once on
   2026-09-06 for exactly this reason).
2. **Re-run Senate opt 457** (`sen-S2-goal2020-risk_atr`). It is running now on the swapped
   genes, so its genomes will not mean what they say. Same for opt 455.
3. **New ATR grid with the fix** — the agreed plan, since the current one finishes on the old
   wiring.
4. **Rename the 5 collapsed cells** whose `risk_atr` label is a lie (they produced results
   byte-identical to their notional twin, i.e. the size blew past the cap): opts **342, 348,
   361, 366, 439**. Renaming also frees the name so a post-fix grid re-runs them properly.
5. **Triage the 30 failed goal2020 jobs.**

## Static-balance ("ok1000") screen — AFTER the ATR fix merges

Requested 2026-09-06, to run once the goal2020 grid finishes.

1. From the top results, select those whose **average equity usage is < 50%**. That is
   `capitalUsage.ts`'s `avgPct` (open notional over equity AT THE TIME); port it to Python and
   run it server-side over the completed rows — read-only, so it can be done the moment the grid
   lands.
2. Re-run those settings with the static-balance feature at **$1000** and label them `ok1000`.
   Remote150 or local, operator's choice.

**MUST come after the ATR fix merges.** Run on today's code and every ok1000 result inherits the
swapped stop/size genes, so a bad result could not be attributed to the static-balance feature
rather than the defect.

Prep already done:
* The feature is `--equity-cap` (`equity_cap` in the run config), exposed on `optimize` and
  `optimize-batch` only. The payload supports it generally but there is no plain
  `backtest --equity-cap` CLI — settle the invocation before the batch.
* Never used: 0 rows in `backtests.strategy_params` or
  `strategy_optimizations.parameter_ranges` mention it. The operator's warning that it may be
  buggy is well founded on usage, though **81 tests pass** (`test_equity_cap.py`,
  `test_equity_cap_e2e.py`).
* Design is sound where it matters: `deployed_equity()` is capped and is what the sizer, buying
  power, margin and option rails see, while `scoring_curve()` divides REAL equity returns by the
  fixed cap — so a strategy does not appear to stop earning once it exceeds the cap.
* Do ONE smoke run before any batch, and assert the sizer actually sees $1000. Today proved this
  codebase can carry a knob that looks wired and is not (see the ATR gene).

## Audit findings still open (see docs/plans/2026-09-06-audit-fix-design.md)

- Options, free to fix (nothing optimized yet): opt #6 breaker state surviving
  `reset_thread_state`, #7 `select_wing` returning the centre strike, #8 premium floor skipped
  when mark is None, #9 negative weights making missing = best, #11 `structure_metrics`
  hardcoding multiplier 100; code #7 no-arb gate failing open, code #9 valuation falling back to
  entry price.
- Measured LATENT and therefore free: code #8 (`quantity=None`, 0 of 96,415 trades), #12 (cache
  error dropping a symbol, 0 of 165 preloads), #6 (tz strip, clock is UTC at both construction
  sites), #11 (insider: 0 of 42,470 sales with null value; 39 null on the buy side, 0.023%).
- **#1a** — stamp the fill with the bar its price came from. Free: trade dates become truthful,
  metrics bit-identical. (#1b, deferring the cash impact, was REJECTED — sub-bp cosmetic noise
  for a full re-run.)
- **F6** — live vs backtest protective-stop policy differ; the shared helper reportedly
  documents this as intentional, so check the pinning tests before touching it.

## ML residuals (found by the agent, outside the audit's four findings)

- **Multi-dataset targets cross ticker boundaries**: `job_handler.py:1315-1321` concatenates
  datasets and sorts by Date, so `Close.shift(-h)` compares one ticker's bar to another's
  whenever >1 dataset is selected. Affects every shift-based target.
- `_calculate_single_target` zero-fills its own tail (`darts_models.py:762`) — same class as F5;
  needs an availability mask because three UI preview callers cannot emit NaN.
- `prepare_multi_series_split` overwrites `self.scaler` per dataset, so only the last symbol's
  survives for `inverse_transform`.

## Other

- **Option backfill recovery pass**: 16 symbols / 5,079 partitions were abandoned on a vendor
  `StatusCode.INTERNAL`. The classifier is fixed (`b0629626`); re-run the identical
  `warm_options_history.py --wide` command after the current pass to pick them up.
- **Per-expert strategy overlay design doc** — 10 overlays in
  `reports/audit-strategy-design-2026-09-06.md`. NOT blocked: `capitalUsage.ts` already derives
  utilisation from the trades JSON + equity_curve. See [[feedback-stackable-capital-light-strategies]].
- **`capitalUsage.ts` / `.test.ts` are untracked** (2026-09-04) — commit them.
- **32 of 54 ForwardTest-tagged backtests** sit on divergent ATR genomes (15 risk_atr, 17
  notional — the stop gene affects BOTH modes). They must be migrated or re-run before any
  deploy. See [[reference-concentration-check-before-deploy]].
