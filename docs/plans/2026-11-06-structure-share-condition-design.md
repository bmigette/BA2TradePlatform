# Structure share condition (per style member) — design

Status: validated with the owner 2026-11-06. Implementation not started.

## Goal

Let the GA explore UNEVEN capital shares between the structures of one option style expert
(e.g. job P: wheel / CSP / bull put spread / ...). Today nothing caps one member's share: the
first passing rule (order_index precedence) can take the whole `max_deployment_pct` budget, and in
job P the GA ended up CSP-only.

## Decision: a rule CONDITION, not a risk rail

A rail refusal happens after the rule matched, so the next member's rule is never tried for that
symbol. A false condition lets the next rule (member) be evaluated. Conditions short-circuit at the
first false one, so the new condition goes LAST in each member's entry rule and only runs on
candidates that passed every other gate.

## Behaviour

`StructureShareCondition` (CompareCondition, `ba2_common/core/TradeConditions.py`):

    share = committed(member) / sleeve_equity * 100      ->  passes when share < X

- `member` = the entry tag of the rule's own entry action (`option_entry_tag`, `_STYLE_MEMBER_TAGS`
  in `ba2test_launcher.py`: O_CSP 11, O_WHEEL 14, ...). The condition receives it from its rule's
  entry action; no new stamp on Transaction is needed (the tag is already on the parent entry order,
  `entry_tag.order_entry_tag`, and assigned stock inherits it).
- `sleeve_equity` = the same capped equity the option rails use (`OptionRiskManagement.sleeve_equity`).
- The candidate's own size is NOT known at condition time (strike/qty/max_loss are computed later in
  `_OptionEntryAction._submit_option_order`). Accepted: a member may overshoot X by one entry,
  bounded by its option_sizing gene.

### What counts toward a member

| Holding | Committed capital |
|---|---|
| open option structure tagged with the member | `option_book._structure_totals` committed (same measure as `max_deployment_pct`) |
| assigned stock (`meta_data.origin == "csp_assignment"`, inherited tag) | cost basis (assignment strike x 100 x qty) — today the option book ignores it, so the wheel would read 0% after assignment |
| covered call written on that stock | its measured covered risk (`_structure_totals`); VERIFY the overlay order carries the wheel's tag |
| entries submitted this cycle, not yet in the book | `_PendingCharge.candidate.max_loss` — `_PendingCharge` gains the entry tag |

Unmeasurable book (any member structure without a measurable committed) -> condition FALSE and an
ERROR log naming the transaction. Never read as zero (no silent failure).

## Cache (performance)

- Per sleeve key (`_sleeve_key(expert_id)`: `(None, expert)` live, `(thread_id, expert)` backtest)
  and decision time: a `{entry_tag: committed}` map built ONCE per cycle from `sleeve_structures` +
  assigned stock + pending charges.
- `record_submitted` increments the cached total of the admitted entry's tag, so the next symbol in
  the same cycle sees it. Both runtimes evaluate symbol by symbol (live `TradeManager` loop;
  backtest `daily_engine._run_expert_bar` -> `_stage_recommendation_candidate`; option entries are
  not deferred), so the counter is current.
- Rebuilt from positions at the next cycle (no drift); cleared with the existing run-boundary resets
  (`reset_state`, `reset_thread_state`, `reset_pending_charges`).
- One shared function for live and backtest.

## Wiring

- OPT-IN per style: a new style field `structure_share` `{"min": .., "max": .., "step": ..}` (added to
  `_STYLE_FIELDS` and validated: 0 < min <= max <= 100, step > 0). A style without it builds exactly
  as today (no leaf, no genes), so jobs A/P and their stored rows are unchanged.
- First user (owner 2026-11-06): job C, style `long` (O_CBS, O_LEAP, O_PMCC, O_CONVEX) with
  `{"min": 20, "max": 60, "step": 10}`. The cap is always ON for an opted-in style (no `:enabled`
  toggle gene: the GA cannot switch it off), only the value is searched.
- Leaf `<member>-structure_share` appended LAST in `_option_entry_rule` (after the benchmark regime
  gates), `optimize=True`, value range from the style field -> gene `cond:<id>:value`. Every
  option-entry member of the opted-in style gets it (VERIFY the O_CONVEX member goes through
  `_option_entry_rule`; if not, wire it in its own builder).
- The share is of the capped sleeve equity, so it only binds below the style's `max_deployment_pct`
  (job C searches 30..80): e.g. deployment 40 + shares 60 lets one member take the whole 40.
- Registration (template: `entry_tag` / `N_ENTRY_TAG`): `ExpertEventType` + `get_numeric_event_values`
  (`types.py`), `CONDITION_MAP`, `rule_builders.FIELD_EVENT` (a miss silently drops the leaf),
  `rules_export_import._FIELD_ABBR`, `trigger_catalog`, `rules_documentation`; enforced by
  `tests/test_condition_registry_coverage.py`.

## Tests

- cache: build, same-cycle increment, prune, run-boundary reset, per-thread isolation;
- BT/live parity (same inputs -> same share and verdict);
- wheel share holds through assignment and covered call;
- unmeasurable -> false + error;
- style gene space contains the leaf, last in each member's entry rule;
- registry coverage.

## Release

GA-affecting: `ba2_common` PACKAGE_VERSION bump and minimum raised in
`required_package_versions.py`; merge only at a job boundary (not while P50k / any master is mid-job).
Live instances get the condition only where a deployed rule contains the leaf.
