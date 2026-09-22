# Categorised trigger picker, and market conditions in the rule editor

**Date:** 2026-09-22
**Status:** agreed, ready to implement
**Branch:** `feat/trigger-picker` (worktree `BA2-triggers`)

## Why

Two problems with one fix.

**The market-condition gates are invisible.** `_authorable_trigger_types()`
(`ba2_trade_platform/ui/pages/settings.py:40`) filters all fifteen
market-condition field names out of the rule editor's Trigger Type menu. The
reasoning was sound — a gate authored with no profile behind it reads
`no_context`, never fires, and on an open-positions ruleset silently stops an
exit — but the cure removed the feature from the UI entirely. The operator
cannot see a gate that a deployed expert is running, cannot author one for an
expert whose profile IS set, and has no way to learn the vocabulary exists.

**The menu is a flat list of sixty-five.** One alphabetically-unordered column,
scrolled, with no grouping and no descriptions. Finding `days_since_last_close`
means knowing it is called that.

## Decisions taken

1. Market fields are **always offered**, with a per-entry message naming the
   profile the expert needs. No expert coupling, no profile filter, no expert
   selector in the picker — the rule editor edits a shared `EventAction` with no
   expert in scope, and pretending otherwise would be a lie about what the
   dialog knows.
2. The dropdown is replaced by a **modal picker with seven categories**: All,
   Position, Signal, Targets, Options, Market, Timing. A trigger may sit in
   several; eighteen of the eighty do.
3. The expert-dialog mismatch warning (flagging an assigned ruleset whose market
   gates the expert's profile does not serve) is **out of scope** for this pass.

## Part 1 — the registry

New pure module `packages/common/ba2_common/core/trigger_catalog.py`, beside
`rules_documentation.py` and `market_condition_rules.py`.

```python
CATEGORIES = ('all', 'position', 'signal', 'targets', 'options', 'market', 'timing')

@dataclass(frozen=True)
class TriggerEntry:
    value: str                  # the ExpertEventType value — what gets stored
    name: str                   # friendly name, or the raw value when there is none
    description: str            # one line; '' when undocumented
    kind: str                   # 'flag' | 'number' | 'categorical'
    categories: frozenset[str]  # never empty, never contains 'all'
    requires_profile: str       # '' or the market-condition profile that serves it

def trigger_catalog() -> tuple[TriggerEntry, ...]: ...
def triggers_in_category(category: str) -> tuple[TriggerEntry, ...]: ...
def search_triggers(query: str, category: str = 'all') -> tuple[TriggerEntry, ...]: ...
def category_counts() -> dict[str, int]: ...
```

`trigger_catalog()` walks `ExpertEventType` and merges three sources:

- `get_event_type_documentation()` → `name`, `description`, and `kind` from its
  `"type"` key.
- `market_conditions.PROFILES` → the fifteen market fields' `ui_name`, their
  `kind` (`numeric` → `number`, `categorical` → `categorical`), and the profile
  that registers each one, which becomes `requires_profile`.
- `_CATEGORIES` below → `categories`.

Name resolution falls back in that order and ends at the raw enum value. A
trigger with no documentation entry is still offered; it renders as its key.

`search_triggers` matches the query case-insensitively against `name`, `value`
and `description`, within `category`; on `'all'` it searches everything.
`'all'` is not stored on any entry — it is the absence of a filter.

### `_CATEGORIES`

Verified against the enum: all eighty values assigned, none unassigned,
eighteen multi-category.

- **position** (18) — `has_no_position`, `has_position`, `has_buy_position`,
  `has_sell_position`, `has_no_position_account`, `has_position_account`,
  `has_option_position`, `has_covered_call`, `has_protective_put`,
  `has_assigned_shares`, `days_opened`, `profit_loss_amount`,
  `profit_loss_percent`, `instrument_account_share`, `loss_pct_of_max_loss`,
  `profit_multiple_of_premium`, `percent_to_current_target`,
  `percent_open_to_new_target`
- **signal** (26) — `bearish`, `bullish`, the six `rating_*_to_*`,
  `rating_upgraded`, `rating_downgraded`, the five `current_rating_*`,
  `rec_direction`, `confidence`, `short_term`, `medium_term`, `long_term`,
  `highrisk`, `mediumrisk`, `lowrisk`, `expected_profit_target_percent`,
  `new_target_higher`, `new_target_lower`
- **targets** (10) — `new_target_higher`, `new_target_lower`,
  `percent_to_current_target`, `percent_to_new_target`, `new_target_percent`,
  `price_vs_target_low_percent`, `price_vs_target_high_percent`,
  `price_vs_target_consensus_percent`, `percent_open_to_new_target`,
  `expected_profit_target_percent`
- **options** (13) — `has_option_position`, `has_covered_call`,
  `has_protective_put`, `has_assigned_shares`, `days_to_expiry`,
  `short_leg_days_to_expiry`, `covered_call_days_to_expiry`,
  `loss_pct_of_max_loss`, `profit_multiple_of_premium`, `credit_decayed_pct`,
  `long_leg_delta`, `iv_rank`, `iv_to_realized_vol`
- **market** (19) — `relative_volume`, `percent_below_recent_high`,
  `percent_above_recent_low`, `iv_to_realized_vol`, plus all fifteen
  `market_condition_fields()`
- **timing** (12) — `days_opened`, `days_since_last_close`,
  `days_since_last_profitable_close`, `days_since_last_losing_close`,
  `days_to_earnings`, `rec_days_to_earnings`, `days_after_event`,
  `days_to_expiry`, `short_leg_days_to_expiry`, `covered_call_days_to_expiry`,
  `structure_bars_since_bos`, `structure_bars_since_choch`

A member absent from `_CATEGORIES` falls into `_DEFAULT_CATEGORIES =
frozenset({'signal'})` rather than vanishing from the picker. Silent
disappearance is the one failure the flat list never had, and a categorised menu
must not introduce it. A test names the omission so it is fixed deliberately.

### The eleven missing documentation entries

`rules_documentation.py` covers 54 of 80. The fifteen market fields get their
names from the registry, which leaves eleven to write, derived from the enum's
own comments in `types.py`:

`has_buy_position`, `has_sell_position`, `current_rating_overweight`,
`current_rating_underweight`, `new_target_percent`,
`price_vs_target_low_percent`, `price_vs_target_high_percent`,
`price_vs_target_consensus_percent`, `days_since_last_close`,
`days_since_last_profitable_close`, `days_since_last_losing_close`.

The three cooldown entries must state the large-sentinel behaviour: with no
prior close the value is a big number, so a `>` cooldown gate passes.

## Part 2 — the picker

The trigger row's `ui.select` becomes a button showing the current trigger —
friendly name over the raw key in mono — that opens the modal. **The stored
shape is unchanged:** `{'event_type': value}` and nothing else. The
operator/value controls beside the trigger are untouched.

```
┌ Choose a trigger ─────────────────────────────┐
│ [🔍 search…                                  ] │
│ All 80 · Position 18 · Signal 26 · Targets 10 │
│ Options 13 · Market 19 · Timing 12            │
├───────────────────────────────────────────────┤
│ Expert Position Exists              flag      │
│ has_position                                  │
│ This expert HAS an open position…             │
│ ───────────────────────────────────────────── │
│ Underlying trend slope             number     │
│ underlying_trend_slope_50_atr14               │
│ Slope of the 50-session trend, in ATRs        │
│ ⚠ Needs the ohlcv-v1 market-condition         │
│   profile on the expert                       │
└───────────────────────────────────────────────┘
```

Categories are chips with counts, not tabs — they wrap at narrow widths. The
search box takes focus on open. Clicking a row sets the value and closes; there
is no Save or Cancel, because picking *is* the action and Escape or the backdrop
cancels. The modal opens on a category holding the current value, falling back
to All.

Two deletions follow:

- `_authorable_trigger_types()` — the market filter it applied is replaced by
  the per-entry message.
- `_trigger_type_options()` — it existed because NiceGUI raises `ValueError` on
  a select value outside its options, which made a deployed gated rule impossible
  even to open. A button displays any value, so the workaround has nothing to
  work around. A value the catalog does not know renders as the raw key with a
  "not in this platform's catalog" note.

## Part 3 — what still refuses

Exposing the fields removes a menu filter, not a guard.

- **`_refuse_market_gates_on_exit_ruleset` is untouched.** It fires when a
  ruleset bound for the open-positions slot is saved carrying a market gate, and
  it is the real refusal.
- **One new guard, at the point of authoring.** If the rule's own Subtype is
  Open Positions and any trigger is a market field, the RULE save is refused with
  the same message. Today that gap exists — only assembling the rule into a
  ruleset refuses — and it matters more now that the fields are one click away.
- **The deploy importer is unchanged.** `import_deploy_payload.py` still checks
  each leaf against the expert's `market_condition_profile`.

Deliberately not blocked: a market gate on an Enter Market rule for an expert
with no profile. The gate never passes, so the rule produces no entries. The
per-entry message is the mitigation.

## Part 4 — files and tests

| File | Change |
|---|---|
| `packages/common/ba2_common/core/trigger_catalog.py` | new — the registry |
| `packages/common/ba2_common/core/rules_documentation.py` | +11 entries |
| `ba2_trade_platform/ui/pages/settings.py` | picker; delete the two helpers; add the rule-save refusal |
| `packages/common/tests/test_trigger_catalog.py` | new |
| `tests/test_rule_trigger_picker.py` | new |

**Registry tests.** Every `ExpertEventType` appears exactly once. No entry
carries `'all'` or an unknown category. A member absent from `_CATEGORIES` lands
in the default bucket rather than disappearing. Each market field's
`requires_profile` equals the profile that registers it; every non-market entry's
is `''`. Names resolve documentation → `ui_name` → raw key. Search matches name,
key and description; category filters; `'all'` searches everything.

**Picker tests.** Category filtering and counts. Picking a row writes
`{'event_type': value}` and nothing else. The modal opens on a category holding
the current value. A value the catalog does not know renders as its raw key
instead of raising — the failure `_trigger_type_options` dodged, now pinned
directly.

**Refusal tests.** A rule with subtype Open Positions carrying a market gate is
refused at rule save; the same gate on Enter Market saves. The ruleset-level
refusal still fires — a regression test, since the menu filter that used to make
it nearly unreachable is going away.

## Operational notes

- This touches `packages/`, so a push needs **both** `APP_VERSION` and
  `TEST_APP_VERSION` bumped, at a grid-job boundary — a mid-run
  `TEST_APP_VERSION` bump breaks the distributed workers' version match.
- Built on the `BA2-triggers` worktree, not on `dev`, because a grid is running.
