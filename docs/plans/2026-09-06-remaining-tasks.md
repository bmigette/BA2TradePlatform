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
