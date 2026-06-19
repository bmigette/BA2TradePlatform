# Exit / Open-Positions Ruleset — UI Requirements (draft)

Status: **draft, for separate implementation.** The backend (CLI/API) support is built and
validated; this captures what the UI must let users do so they can edit + optimize the
open-positions exit ruleset without hand-writing JSON.

## Background / data model (already implemented)
- A strategy carries an **exit ruleset** = an ordered list of **exit rules** (the Strategy
  `exit_conditions` JSON; one row per rule). Each rule mirrors a live `open_positions`
  EventAction and is evaluated by the **real `TradeActionEvaluator` + RM on the analysis
  cadence** (identical to live `process_open_positions_recommendations`).
- A rule = **triggers (ANDed conditions)** + **one action**:
  - Conditions: flags (`bearish`/`bullish`, `medium_term`/`short_term`/`long_term`,
    `highrisk`/`mediumrisk`/`lowrisk`, `current_rating_negative`/`_positive`,
    `new_target_lower`/`_higher`, `has_position`/`has_no_position`) and numerics with an
    operator + value (`profit_loss_percent`, `days_opened`, `confidence`,
    `expected_profit_target_percent`, `percent_to_current_target`, `days_since_last_close/…`).
  - Actions: `close`, `sell`, `adjust_take_profit`, `adjust_stop_loss`. The two Adjust actions
    carry a **reference_value** (`order_open_price` / `current_price` / `expert_target_price`)
    and a **value %** (e.g. −10% of order open price).
- **Optimization knobs** per rule (existing gene plumbing):
  - `toggle_optimize` → the optimizer can turn the **whole rule on/off** (`exit:<id>:enabled`).
  - each numeric condition: `optimize` + `value_min/max/step` → `cond:<id>:value`.
  - adjust actions: `action_value_optimize` + `action_value_min/max/step` →
    `exit:<id>:action_value`.

## UI requirements
1. **Exit-ruleset editor** (per strategy/expert), reachable from the strategy/optimization
   screen, alongside the existing entry-condition editor.
2. **Add / remove / reorder** exit rules. Rules are ordered (evaluated top→down,
   `continue_processing=False` per rule = first matching rule's actions run).
3. **Per-rule condition builder**: AND-group of conditions; for each condition pick the field
   from the supported vocabulary above; for numerics pick operator + value. (OR-nesting is a
   later enhancement — backend currently ANDs leaves.)
4. **Per-rule action picker**: `close` / `sell` / `adjust_take_profit` / `adjust_stop_loss`;
   for the adjust actions show `reference_value` dropdown + the value % field.
5. **Optimize toggles** on every tunable: a checkbox + min/max/step inputs for each numeric
   condition value and each adjust action value, and an on/off-optimize checkbox per rule.
   These map 1:1 to the `optimize`/`value_*` / `action_value_optimize`/`action_value_*` /
   `toggle_optimize` fields.
6. **Live preview of gene count** (and rough search-space size) as the user toggles
   optimization on tunables — so they understand the population/generations they'll need.
7. **Templates / presets**: ship the default set (bearish-close, downgrade-close, break-even
   profit-lock, time-exit) as one-click presets; allow "import from a live expert's
   open_positions ruleset" (read the live trade DB ruleset and pre-fill the editor + mark
   values optimizable).
8. **Validation**: reject unknown fields/actions; warn when an adjust rule has no protective
   counterpart; warn when a rule can never trigger.
9. **Read-back**: when viewing a completed optimization's best params, render the *resolved*
   exit ruleset (toggled-off rules hidden/greyed, tuned values filled in) so users see what
   actually ran.

## Notes / open items
- The **initial TP/SL bracket** is currently applied immediately on entry (optimized via the
  `tp`/`sl` genes) so a position is never unprotected between weekly analysis runs; the exit
  rules adjust/close on top. If we instead want the initial bracket to come purely from an
  exit rule (live-style), the UI should expose an "initial bracket" rule and we run
  open-positions management on a daily `execution_schedule_open_positions` to close the gap.
- Per-expert: the available condition vocabulary is shared, but defaults/presets should be
  per-expert (e.g. rating experts surface `current_rating_negative`, drift experts surface
  `days_opened`).
