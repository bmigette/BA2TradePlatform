# Stage-2 job A: seeding + per-gate blocked-entry counter (2026-11-04)

> For Claude: implement task by task (superpowers:subagent-driven-development). Worktree
> `C:\Users\basti\Documents\dev\BA2-stage2jobA`, branch `feat/stage2-seeding` (from dev b960e463).

**Goal.** Job A (`O_STYLE` style `mid`: O_STRG, O_STRD, O_BULLCS, O_BF, O_LC) starts from genomes that
are known to trade, and every trial reports WHICH entry gate refused entries.

**Why (pilot s2pilot2, opt 35 on remote227, 2026-11-04).** 21 of 28 random genomes made zero trades.
Not the gate count: traders and non-traders both had 3-8 market gates on per member. 3 had every
member's entry toggle off. For the rest, nothing records which gate said no.

**User decisions this builds on.**
- 2026-11-03: seed the genes only; gene RANGES stay as they are (no narrowing).
- 2026-11-04: every condition stays togglable and the GA explores combinations (no sparse-gate change).
- 2026-09-25: members without a seed start ON at their authored defaults.

**Seed sources (local test DB, all `st1fix`, completed).** 117 O_LC, 119 O_BULLCS, 122 O_BF,
128 O_STRD (joint), 129 O_STRG (joint). The policy file (which ranks, how many seeds) is the
user's; the tool takes it as input.

## Deltas against `docs/plans/2026-09-25-option-stage2-build.md` Tasks 10-16

The 09-25 plan is the base spec. Read its Task 10-16 text. Changes:
- **No `rangeOverrides` / `apply_range_overrides` / `narrow_ranges`** (user: ranges unchanged). Drop
  those parts of Tasks 11 and 14 and the `narrow` policy field.
- **Lattice:** `decode_individual` now snaps with `snap_to_lattice(value, config, self.lattice_anchor)`
  (Q12 fixed). Strict encode must accept exactly the values `snap_to_lattice` reproduces, using the
  optimizer's own anchor. Do not re-derive rounding.
- **Stratified choice genes** exist (`config["stratify"]`, `seeded_population`). Strict seeds must
  carry them; padding goes through `seeded_population` so the stratification still holds.
- **Out-of-range seeds clamp** (decided here): stage-1 ranges differ (O_BULLCS DTE 20-60 -> 40-90,
  O_LC 20-60 -> 25-65, per-instrument cap 45-50 -> 5-25). Policy `out_of_range` default
  `"clamp"`, every clamp recorded in the provenance and printed by `--seed-check-only`.
- **Genes the source never had** (the SPY gates `cond:<m>-spy-trend:mode` / `-spy-vol:mode`, the option-RM
  genes `model:max_deployment_pct` / `circuit_breaker_pct` / `max_notional_leverage`): never a silent
  default. The policy carries `fill: {glob: value}`, applied ONLY to target genes absent from every
  contribution; an absent gene no glob matches is refused by name. Job A policy: SPY modes `off`
  (= stage-1 behaviour), RM genes at their least restrictive value (stage 1 ran without the RM).
- **Seeded member entry toggles** (`entry:<m>-entry:enabled`, and both joint arms' toggles) are set
  ON unless the source carried them (stage-1 joint sources do).

## Tasks

### T1 Per-member neutral entry mode in the style spec (launcher)
A style member may declare `"neutral_entry_mode": "joint" | "hold" | "low_confidence"` (only for
`_NEUTRAL_ENTRY_MEMBERS`; refused elsewhere). `_build_strategy_style` applies
`_configure_neutral_entry(src, key, mode)` to that member's own built strategy BEFORE
`_style_entry_rules`, so the member's entry ids are exactly the single job's (`o_strd-entry-hold`,
`o_strd-entry-low_confidence`).
- When ANY member is non-legacy, the run sets `evaluate_entry_rules_on_hold = True` on the expert
  (as `_apply_neutral_entry_setting` does for single jobs), and EVERY directional member (not in
  `_NEUTRAL_ENTRY_MEMBERS`) gets one extra FIXED leaf `{"id": "<m>-directional", "field":
  "rec_direction", "op": "!=", "value": 0}` (no genes), so a HOLD can never reach it: its admission
  is exactly what it was with the flag off. A legacy neutral member next to a non-legacy one is
  refused (its legacy gate would change meaning under the flag).
- `--neutral-entry-mode` together with `--strategy O_STYLE` is refused (the spec owns it).
- `tools/option_styles.stage2.json` style `mid`: O_STRG and O_STRD `"neutral_entry_mode": "joint"`.
- Tests: ids/genes of a joint member inside the style equal the single joint job's member genes
  (modulo the exit prefixing); directional members gain only the fixed leaf (no new gene); the
  expert setting is set iff a member is non-legacy; refusals named.

### T2 `encode_params_strict` (genetic.py) = 09-25 Task 10 with the lattice/stratify deltas.

### T3 Handler `seedPopulation` = 09-25 Task 11 minus rangeOverrides
`ga["seedPopulation"]` (list of complete flat gene dicts) -> strict encode each -> `seeded_population`
padding. Mutually exclusive with `warmStartFromOptimizationId` and `chainSeedFromOptimizationId`
(fail the job naming both). A checkpoint still wins (resume unchanged).
**Plus `ga["requireAnEntryRule"]`** (set by the launcher for O_STYLE only): in generation 0, a RANDOM
(non-seed) individual whose every `entry:*:enabled` gene is 0 gets one of them, drawn with the GA's
own RNG, set to 1. Absent key = today's behaviour exactly (every existing job bit-identical).

### T4 `authored_genome` = 09-25 Task 12 (adapt the table to today's leaves; SPY regime and market
categorical leaves included).

### T5 `style_seeding.py`: translate + assemble = 09-25 Tasks 13-14 minus narrowing, plus `fill`.

### T6 `tools/build_style_seed_bundle.py` = 09-25 Task 15 (read-only DB extraction).

### T7 Launcher `--seed-population` / `--seed-check-only` = 09-25 Task 16 (sets `seedPopulation`,
`seedProvenance`, `requireAnEntryRule`, `backtest.seed_bundle_sha256`).

### T8 Per-gate blocked-entry counter (backtest-only, read-only)
Facts (survey 2026-11-04): `daily_engine._stage_recommendation_candidate` builds a
`TradeActionEvaluator` and calls `evaluate` (~daily_engine.py:1554-1559). `_evaluate_conditions`
(TradeActionEvaluator.py ~1030) ANDs a rule's triggers in leaf order and SHORT-CIRCUITS at the first
False leaf; `evaluator.rule_evaluations` keeps `{rule_name, rule_id, conditions[], all_conditions_met,
executed}` per evaluated rule. Trigger keys are positional (`cond_{i}`, `rule_builders.
triggers_from_condition_tree`), so the leaf id must be mapped back from the rule's tree. HOLD
recommendations never reach the evaluator unless `evaluate_entry_rules_on_hold`.
Design:
- After `evaluate` on the ENTRY pass only, read `rule_evaluations` (never re-evaluate, never change
  `evaluate_all_conditions`): per rule id count `evaluated`, `met` (all conditions true) and
  `blocked[leaf_id]` += 1 for the first False leaf. Since evaluation stops there, `reached` per leaf
  follows from the order (report it: `reached_k = evaluated - sum(blocked_j for j<k)`).
- Leaf ids: map `cond_{i}` -> leaf id with the SAME traversal that built the triggers (an optional
  return of `triggers_from_condition_tree`, or a sibling function sharing its walk). The triggers
  themselves must not change (they feed live).
- `results["entry_gate_record"]` from the engine (like `analysis_failures`), a `_entry_gate_field`
  helper in `strategy_optimization_handler.py` adding a compact `"eg"` to the worker result dict, and
  the pass-through in all THREE row builders (pooled/distributed ~2213, in-process ~1915, worker
  ~405). Absent when the engine recorded nothing.
- Zero effect on results: pinned by a test that runs the same tiny backtest with and without the
  recorder and compares trades/equity, plus a unit test of the counting on a hand-made
  `rule_evaluations` list.
- `tools/report_entry_gates.py --opt-id N [--db]`: per member rule, the reached/blocked table summed
  over a job's trials (zero-trade trials separately).

## Out of scope here
The per-year/concentration report, the dead-gene check, BETR/GH/OKLO/PS/TEVA repair, the RM refusal
counter (unless T8 covers it), version bumps and the push (after review; user go).
