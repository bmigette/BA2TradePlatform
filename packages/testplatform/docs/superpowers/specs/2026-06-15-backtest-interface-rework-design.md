# Backtest interface rework — expert-centric design

Date: 2026-06-15
Status: approved (brainstorming), pending implementation plan
Repo: BA2TestPlatform (frontend + backend), with shared seams in ba2_common / ba2_providers / ba2_experts

## Goal

Pivot the backtest UI from symbol+model+strategy-centric (ML-era) to **expert-centric**: pick
an Expert → its settings render automatically (each numeric optimizable), pick a universe
(static list or screener), attach a Strategy (enter/exit rules, optimizable + on/off), then run
a single backtest or a joint optimization. Keep the legacy ML-model backtest path working.
Add a History tab and expert / optimization-job filters.

## Locked decisions (from brainstorming)

1. **RM/sizing lives in the Expert** (built-in settings on every expert), and is **optimizable**.
   The Strategy's `rm_*` columns are retired; the optimizer's `rm:*` namespace is replaced by
   optimizing expert settings (`expert_params` / `model:*`).
2. **Rules are external to the expert** and stay separate: enter/exit condition trees + TP/SL,
   each value optimizable and each node/rule on/off-toggleable (the existing `cond:*` / `exit:*`
   wiring).
3. **Single expert per backtest/optimization** (one expert + one universe + one ruleset set).
4. **Import/Export = JSON**, reusing `ba2_common.core.rules_export_import` (already shared into the
   test engine), extended to a **v1.1** format that carries optimize ranges (`min/max/step`) and
   the on/off `enabled` flag on numeric operands. Enter and exit rulesets are imported/exported
   **separately**. A trade-platform-exported ruleset imports directly.
5. **Legacy ML backtest path preserved**: a top-level **source selector — "Expert" (default) or
   "ML model" (legacy)** — feeds the same shared Strategy section. ML keeps its model+datasets+
   symbol + `position_sizing`. The conditions engine + `decode_params` are already shared.
6. **Screener universe** resolves from the **offline screener-history cache** (built via
   `ba2-test fetch-screener`); a run whose date range isn't covered fails fast asking the user to
   build the cache (avoids the slow per-bar live screen, stays point-in-time/survivorship-free).
7. **Tabs: New · History · Saved.** History = all tracked runs; Saved = `is_saved` only. Both
   filter by **expert** + **optimization job** + free-text search.
8. Optimize ranges are **always user-entered** (no pre-fill from setting-definition hints).

## Architecture / data flow

```
[New tab]
  source = Expert | ML model
    Expert path:  GET /api/experts, GET /api/experts/{class}/settings-definitions
                  -> render settings form (numeric -> value + [Opt] min/max/step)
                  Universe: static (typed / .txt import / index helper) | screener (filters)
    ML path:      model + prediction/execution datasets + symbol + position_sizing (as today)
  shared:         Strategy = enter/exit condition trees (+per-node on/off + Opt) + TP/SL(+Opt)
                  Import/Export JSON per ruleset (enter, exit)
  Run:            POST /api/backtests            (engine = daily_expert | ml)
  Optimize:       POST /api/strategies/{id}/optimize  (joint GA over expert settings + cond/exit + tp/sl)

[History / Saved tabs]
  GET /api/backtests?expert=&optimization_id=&saved=
```

Per-trial execution is unchanged: `daily_backtest_handler.run_daily_backtest` (daily_expert) or
`backtest_handler.run_backtest` via `_run_ml_trial_backtest` (ml). The joint optimizer
(`strategy_optimization_handler.handle_strategy_optimization`) drives both.

## Components

### Frontend (`BA2TestPlatform/frontend/src`)
- `pages/Backtesting.tsx` — restructure into tabs **New / History / Saved**; New tab gets the
  source selector + expert/universe sections; reuse `components/ConditionBuilder.tsx` for the
  shared Strategy section.
- New components: `ExpertPicker`, `ExpertSettingsForm` (renders from settings-definitions, numeric
  rows get `[Opt]` + min/max/step), `UniversePicker` (static textarea + **.txt import** + index
  helper; or screener filter fields), `RuleIO` (Import/Export JSON buttons per ruleset),
  `RunHistoryTable` (shared by History/Saved with the filter bar).
- API client additions for the new endpoints.

### Backend (`BA2TestPlatform/backend/app`)
- `api/experts.py` (new): `GET /api/experts`, `GET /api/experts/{class}/settings-definitions`.
- `api/backtests.py`: extend create payload with `engine`, `expert {class, settings}`,
  `universe {mode, symbols[] | screener_settings{}}`; extend list with `expert` /
  `optimization_id` / `saved` filters.
- `api/strategies.py`: optimize payload gains `expert` + `universe` + `expert_params`
  `{<setting>:{min,max,step}}`; drop `rm_params`.
- `api/rules.py` (new): `GET /api/strategies/{id}/export-rules?which=enter|exit`,
  `POST /api/strategies/import-rules {json, which}`.
- `services/rules_tree_json.py` (new): `tree_to_ruleset_json(...)` and
  `ruleset_json_to_tree(...)` — the inverse of `seed_ruleset_from_tree`
  (`services/backtest/default_rulesets.py`), reusing `ba2_common.RulesExporter`/`RulesImporter`
  for the DB↔JSON half and the `_FIELD_EVENT` map (reversed) for event_type↔field.
- `services/strategy_param_space.py`: treat any **numeric built-in expert setting** as eligible
  for `expert_params` (`model:*`); remove the `rm:*` namespace path.
- Screener universe resolution in the daily backtest path: when `universe.mode == screener`,
  resolve symbols per bar from the screener-history cache; fail fast if the range isn't cached.

### Data model
- `models/strategy.py`: **drop** the `rm_*` columns (and their optimize/min/max/step variants);
  keep name, `buy/sell_entry_conditions`, `exit_conditions`, `initial_tp_*`, `initial_sl_*`.
- New `db_migrate/NNN_drop_strategy_rm_columns.py` migration (sqlite-safe: table rebuild).
- `Backtest` already has `expert_name` + `optimization_id` (no change) for the filters.

## JSON format (v1.1)

Extends `rules_export_import` v1.0 with two optional fields on numeric operands; v1.0 files still
import (treated as enabled, not optimized):

```jsonc
{
  "export_version": "1.1",
  "export_type": "ruleset",
  "ruleset": {
    "name": "...", "type": "...", "subtype": "ENTER_MARKET",
    "rules": [{
      "triggers": {
        "bullish":   { "event_type": "bullish", "enabled": true },
        "gate_conf": { "event_type": "confidence", "operator": ">", "value": 0.7,
                       "enabled": true, "optimize": { "min": 0.5, "max": 0.9, "step": 0.05 } }
      },
      "actions": { "buy": { "action_type": "buy" } },
      "continue_processing": false, "order_index": 0
    }]
  }
}
```

- `enabled` ↔ `cond:<id>:enabled` / `exit:<id>:enabled`.
- `optimize {min,max,step}` ↔ `cond:<id>:value` / `exit:<id>:action_value` (TP/SL action values too).
- Mapping: a ruleset's `EventAction`s = OR of rules; each rule's `triggers` = AND of nodes →
  `ConditionGroup(OR[ ConditionGroup(AND[nodes]) ])`. Fixed triggers (`bullish`, `has_no_position`)
  preserved as flag nodes and re-added on seed-back.

## Optimization wiring

- `expert_params: {<setting>: {min,max,step}}` for each Opt-on numeric expert setting → param-space
  `model:<setting>` ranges; the chosen fixed values go in `expert {settings}`.
- `cond:*` / `exit:*` value + enabled toggles and `tp`/`sl` unchanged.
- `rm:*` removed. `_build_daily_trial_config` already merges expert overrides; RM sizing now arrives
  as expert settings, so no separate RM mapping is needed for the expert path.
- Bug-fix dependencies already on `dev`: as-of indicator/ATR clamp, screener `as_of` threading,
  string-date coercion, and the 0-trial guard (so a misconfigured optimization fails loudly).

## Error handling

- Fail-early validation (per `backend/CLAUDE.md` no-defaults rule) on the create/optimize payloads:
  missing expert class, empty universe, screener range not cached, bad optimize range (min≥max,
  step≤0).
- Import: reject unknown `event_type` / `export_type`; report which triggers couldn't be mapped
  rather than silently dropping them.

## Testing

- Backend unit: experts endpoints; `ruleset_json_to_tree` round-trips `tree_to_ruleset_json`
  (and a real trade-platform export imports); param-space includes `model:*` from `expert_params`
  and no longer emits `rm:*`; screener-not-cached fails fast; backtest/optimize payload validation.
- Migration test: `rm_*` columns dropped, existing strategies still load, ML + expert backtests
  still run.
- ML regression: an `engine=ml` optimization still runs through the shared `decode_params` after
  `rm_*` removal.
- Frontend: settings form renders from definitions; .txt import parses/dedups; condition rows emit
  the right `cond:*`/`exit:*` payload; History/Saved filters hit the endpoint.

## Out of scope (YAGNI)

- Multi-expert-per-run (decided single-expert).
- Live per-bar screening (offline cache only).
- Setting-definition optimize-range hints (user enters ranges).
- Smart-RM (LLM) optimization — only classic-RM/expert settings + rules are tuned.
