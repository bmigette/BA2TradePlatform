# Unified Rule Model (EventAction-shaped Strategy) Implementation Plan

> **For Claude:** work tasks #68-#75 in order; each lands with tests green before the next.

**Goal:** Make the test platform's `Strategy` express exactly the live model — an ordered list
of EventAction-shaped rules (conditions + one-or-more actions + `continue_processing`,
first-match evaluation) — for entry AND exit, with GA genes derived per rule/action, so
live↔test round-trips are lossless and strategy definitions stop splitting conditions from
actions.

**Why (verified findings, 2026-07-08):**
- The runtime engine is already shared and faithful (`TradeActionEvaluator`: sequential,
  first-match unless `continue_processing`, N actions per EventAction). The divergence is the
  `Strategy` schema feeding it.
- Current schema splits `buy/sell_entry_conditions` (trees) from `entry_actions` (one flat
  list copied onto every branch) — live's per-rule brackets (per-tier TP/SL) are inexpressible.
- Exit rules carry exactly ONE action; live→test import silently drops 2nd+ actions.
- `continue_processing` is not representable; import drops it, export hardcodes False.
- UI shows "any rule triggers an exit" while first-match runs; S5 showed "no entry" while the
  implicit bullish+flat base gate traded 57 times.
- S7 (58% max in 21 distinct genomes) vs S1 (179% same grid) — lossy translation of the
  archived winner into a structurally different strategy + too-narrow search. Fix = faithful
  replica on the new model.

**Canonical shape** (in `ba2_common.core.rule_models`, shared by both platforms):

```python
TradeRule = {
  "id": "r1", "name": "BUY_HighConfidence",
  "conditions": {tree | None},          # AND/OR group, cond leaves keep cond:<id>:* genes
  "actions": [                          # ordered, 1..N
    {"action_type": "buy"},
    {"action_type": "adjust_take_profit", "reference_value": "expert_target_price",
     "action_value": -2, "action_value_optimize": true, "action_value_min": -20,
     "action_value_max": 10, "action_value_step": 2, "toggle_optimize": true},
  ],
  "continue_processing": false,
  "toggle_optimize": true,              # GA may drop the whole rule
}
```

`Strategy.entry_rules` + `Strategy.exit_rules` are lists of TradeRule. The base bullish+flat
gate becomes EXPLICIT conditions on entry rules (no more engine magic). Seeding is 1:1 —
one EventAction per TradeRule, order + continue_processing + actions verbatim.

**Genes:** `cond:<id>:*` unchanged; `rule:<rid>:enabled` (when toggle_optimize);
`rule:<rid>:a<idx>:action_value` / `:enabled` per optimizable action. Legacy
`exit:<id>:*`/`entry:<id>:*` namespaces stay read-only for saved-row reconstruction.

**Compat:** `normalize_trade_rules()` lifts every legacy shape (single-action exit rows, flat
entry_actions, buy/sell trees) so saved backtests keep exporting; migration 027 converts
stored strategies and drops the three legacy columns.

## Tasks (tracked as session tasks #68-#75)

1. **#68 Canonical TradeRule** — `rule_models.py`: `ActionCfg`/`TradeRule` pydantic models +
   `normalize_trade_rules()` (new shape + all legacy lifts). Tests.
2. **#69 Converters** — `rules_convert.py`: `live_export_to_strategy` → entry_rules/exit_rules
   preserving ALL actions + continue_processing + stop_processing guards;
   `strategy_to_live_export` reverse, lossless round-trip tests (incl. 4-tier bracket pattern).
   Old signatures kept as thin adapters until #75 lands.
3. **#70 Migration 027** — Strategy: add `entry_rules`, rename/upgrade `exit_conditions` →
   `exit_rules`; convert rows (one entry rule per OR-branch, bracket replicated per rule, base
   gate explicit); drop `buy_entry_conditions`/`sell_entry_conditions`/`entry_actions`.
4. **#71 Gene space** — `strategy_param_space.py` walks rule lists; new rule/action gene
   namespaces; decode rebuilds rule lists (disabled rules/actions pruned).
5. **#72 Seeder + handlers** — `seed_ruleset_from_rules()` 1:1; delete tree-splitting seeder;
   plumb `entry_rules`/`exit_rules` through backtest/optimization handler configs.
6. **#73 Launcher strategies** — S1 = lossless live import; S2/S3/S5/S6 rewritten as explicit
   rule lists (S5/S6 base gate visible); S4 retired; matrix budgets adjusted to gene counts.
7. **#74 S7 fix** — faithful replica of the archived 186% winner (schedule-evaluated
   exit-condition TP/SL, winner's exact gates), ranges wide enough to actually search
   (>21 genomes); smoke-verify the neighborhood reaches >100% raw before grid use.
8. **#75 Frontend + export** — export/quick-load on rule lists (legacy rows lifted at read);
   live import dialog on new shape; one rule-builder component for entry+exit (actions list +
   continue flag); Strategy tab renders per-rule actions.

## Non-goals / notes
- No engine changes (`TradeActionEvaluator` untouched — it already implements the model).
- Live platform DB/UI unchanged (it already IS this model).
- Grid stays stopped until #73/#74 land and are smoke-verified.
