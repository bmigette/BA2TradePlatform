# Entry-Action Live Parity — Findings & Proposed Approach

> Status: NOT implemented. Written for review — do not execute as a task list.
> Grid: paused after job 6/45 (`scr-large-FMPRating-S6-goal4`) per instruction; do not resume
> until this is reviewed.

## The ask (from conversation)

1. Fix the live-ruleset exporter (it silently drops entry-time TP/SL brackets).
2. Rebuild S1 to be genuinely live-parity, merge S4 into it.
3. Make entry TP/SL on/off + value searchable via GA genes, bump population accordingly.
4. Retire S4 for future jobs.
5. Re-run S1-large once fixed.

Mid-investigation, the user corrected the approach twice:
- *"the BT engine needs to be 100% on par with live engine, I've mentioned this countless times"*
  — rejects any lossy simplification (e.g. collapsing live's per-tier brackets into one flat
  representative TP/SL pair).
- *"why aren't we using the same engine"* — pushes to actually verify whether backtest and live
  share code, not just "behave similarly."

This doc captures what was found before being told to stop and write it up instead of
implementing.

## What's already correct — no engine divergence

The backtest engine **already reuses the live execution engine directly**, not a reimplementation:

- `testplatform/backend/app/services/backtest/daily_engine.py:778,871` imports and drives
  `ba2_common.core.TradeActionEvaluator.TradeActionEvaluator` — the exact class live uses for
  Phase 1.5 (eager transaction creation) + Phase 2 (adjust_tp_sl) rule execution.
- `BacktestAccount` (`app/services/backtest/backtest_account.py:127`) implements the same
  `AccountInterface` live broker accounts (`AlpacaAccount`, `IBKRAccount`) implement, which is why
  `TradeActionEvaluator` can drive it unmodified — it only calls interface methods
  (`_create_transaction_for_order`, `adjust_tp_sl`, ...).
- So a rule that reaches `TradeActionEvaluator` behaves identically whether backtest or live is
  driving it. **The divergence is entirely upstream of that** — in how the backtest engine
  *constructs* the EventAction-shaped ruleset it feeds to the evaluator.

## Where the actual gap is

`app/services/backtest/default_rulesets.py::seed_ruleset_from_tree` (line 215) builds the
backtest's synthetic enter_market ruleset from a `Strategy.buy_entry_conditions` GA condition
tree. It **already loops per OR-branch** of the tree and creates one `EventAction` per branch
(line 256-270: `groups = _gate_trigger_groups(buy_tree)` → one `_make_event_action(...)` per
group) — this is structurally the same "one rule per tier" shape live uses (confirmed against the
real live ruleset — see Data reference below, 4 separate `BUY_*` EventActions, one per
confidence/risk tier).

**The gap:** inside that per-branch loop, `entry_actions` is passed identically to every branch —
`_entry_actions("buy", entry_action, entry_actions)` (line 269) uses the SAME flat `entry_actions`
list for every group `gi`. Live's actual ruleset has a **different** TP/SL bracket attached to
each tier's own rule (see below — tier values range TP −8%..0% off target, SL −8%..−12% off
entry). So even though the branch-per-rule structure already exists, the bracket is not yet
branch-scoped — collapsing it to one flat list (which is what the interrupted implementation was
about to do) would NOT be 100% parity, it would just be a coarser average.

**Everything downstream of `Strategy.entry_actions` assumes a single flat list**, and would need
to become per-branch to reach real parity:

1. `Strategy` model (`testplatform/backend/app/models/strategy.py:37`) — `entry_actions` is one
   JSON list, no branch/group association.
2. `strategy_param_space.py` — `collect_param_space`/`decode_params`'s `entry:<id>:*` gene
   namespace assumes one global set of entry-action ids; would need to become
   `entry:<branch_id>:<action_id>:*` (multiplies gene count by number of OR-branches).
3. `default_rulesets.py::seed_ruleset_from_tree`/`_entry_actions` — needs the per-branch bracket
   threaded through the existing per-branch loop instead of the single `entry_actions` param.
4. The live-import readers (`ba2_common.core.rules_convert.py::live_export_to_strategy`,
   `testplatform/backend/app/api/ruleset_meta.py::_read_live_enter_market_trees`) — currently
   discard `adjust_take_profit`/`adjust_stop_loss` actions found on buy/sell EventActions entirely
   (`rules_convert.py:250-252`, explicitly documented as "IGNORED for the tree ... but COUNTED" —
   `ignored_initial_brackets` in the summary). These need to extract them **per originating
   branch/rule**, not merge/aggregate across branches, so the branch↔bracket association survives
   the live→backtest import.
5. Frontend entry-actions builder (`Backtesting.tsx`, added in the prior
   `2026-07-03-entry-tp-sl-bracket-actions.md` plan) — currently edits one flat list; would need a
   per-branch UI if this becomes user-editable in the test platform (may be out of scope if only
   the launcher's S1 builder needs it, not the UI).

## Data reference — the real live pattern (verified against prod DB)

Source: `C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite`, `expertinstance` id=3
(`FMPSenateTraderWeight`, alias "FMPSenateWeight"), `enter_market_ruleset_id=11`
("Optimized Entry - High Conviction-1"). Per user: this is the pattern now being adopted for
FMPRating (`expertinstance` id=1) too — not yet reflected in the DB read at investigation time
(`enter_market_ruleset_id` still 17, a plain `buy`-only rule, no bracket).

Four buy-side EventActions, each combining `buy` + `adjust_take_profit` (ref
`expert_target_price`) + `adjust_stop_loss` (ref `order_open_price`), values differing by tier:

| Rule | TP (off target) | SL (off entry) |
|---|---|---|
| BUY_HighConfidence_LowRisk_LongTerm | −5% | −12% |
| BUY_HighConfidence_MedRisk_MedTerm | −5% | −10% |
| BUY_VeryHighConfidence_ShortTerm | 0% (= target) | −8% |
| BUY_Fallback_StrongSignal | −8% | −10% |

(Mirrored for the SELL/short side with the same 4 tiers.) The `open_positions` ruleset (id=12)
for this same instance carries exit rules matching the pattern already imported for FMPRating's
S1 (`docs/live_rulesets/FMPRating.json` live-30..41: trailing SL tiers, TP raise/lower on
target-price change, time-decay closes, sentiment-reversal closes, plus
`increase_instrument_share`/`decrease_instrument_share`) — so the exit side is already
consistent; only the entry-bracket side is missing.

## Adjacent items already fixed this session (unaffected by the above)

- `_persist_top_backtests` (`ba2test_launcher.py:2009-2010`) now mirrors `decoded["entry_rules"]`
  into `strategy_params["entryActions"]` for TOP-N optimization saves — was previously silently
  dropped, breaking Strategy-tab display/export (NOT a backtest-correctness bug — committed
  `f7a57ce`, already pushed). The 9 pre-existing affected rows (opt 42, opt 64 incl. backtest
  #257) were backfilled via `test_files/backfill_entry_actions.py --apply`.
- This existing mechanism is what S4 uses today (`_build_strategy_S4`,
  `ba2test_launcher.py:1325`) — one flat, unconditional `s4_tp` entry action (TP only, no entry
  SL), which is the same flat-list limitation described above, just simpler (one action instead
  of two, no per-branch variation because S4's live source never had per-branch data to begin
  with — it was a from-scratch experiment, not a live import).

## Grid-coordination notes (for whenever this resumes)

- S4 is gated to FMPRating only (`_TARGET_PRICE_STRATEGIES`/`_TARGET_PRICE_EXPERTS` in
  `tools/run_screener_capband_matrix.py:40-41`), so in the current 45-job `-goal4` grid it only
  appears 3 times: large (done, job 4), mid (job 11, not started), small (job 18, not started).
- The matrix driver (`run_screener_capband_matrix.py`) builds its full job list **once in memory
  at startup** (`jobs = list(_jobs(...))`, line 170) — editing the script does NOT affect an
  already-running process's remaining queue. To actually drop S4/change S1's definition for
  remaining jobs, the running driver must be killed and restarted.
- Resumability is by **job NAME** (`_completed_names()` queries
  `strategy_optimizations.status='completed'` by exact name match, `run_screener_capband_matrix.py:190`)
  — a restart with the same `--name-suffix` will SKIP already-completed jobs by name, including
  old S1 runs, even though S1's definition will have changed. To force S1 to re-run under a new
  definition, either delete the old completed `StrategyOptimization`/`Backtest` rows by name first,
  or use a fresh `--name-suffix`.
- Grid was told to stop after job 6 (`scr-large-FMPRating-S6-goal4`) completes — job 7
  (S7-large) and beyond should not start until this plan is reviewed.

## Open questions for the reviewer

1. Scope of per-branch entry actions: does every strategy that uses `entry_actions` need this
   (S1, and whatever S4 becomes), or is it S1-specific? S4's `s4_tp` today is a synthetic
   experiment with no live counterpart to be "faithful" to — does it still make sense standalone,
   or does merging it into S1 fully retire it as the plan originally said?
2. Gene-count growth: per-branch entry actions multiply gene count by the number of OR-branches
   in `buy_entry_conditions` (4 for the High-Conviction pattern, times 2 actions (TP+SL) times 2
   genes each (enabled+value) = 16 new genes minimum, likely more with sell-side branches too).
   Population sizing needs to scale with this — no concrete number was decided.
3. Should the live-import readers reconstruct exact branch identity from live (matching each
   EventAction 1:1 to a GA-optimizable group), or is a coarser grouping acceptable as long as
   each group keeps its own distinct bracket (vs. today's fully flattened single list)?
4. `export_live_rulesets.py --live-db` defaults to the dev DB (`ba2_common.config.DB_FILE`),
   which currently has empty `ruleset`/`eventaction` tables on this machine — the real data is in
   the **prod** DB. Worth deciding whether dev should get its own populated ruleset data, or
   whether the default should change, or whether `--live-db` should just always be passed
   explicitly when re-exporting.
