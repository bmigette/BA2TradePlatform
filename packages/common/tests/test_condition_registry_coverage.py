"""INVARIANT: every registered TradeCondition must be reachable from a rule leaf.

``rule_builders.triggers_from_condition_tree`` builds an EventAction's ``triggers`` from a
strategy condition tree and **silently skips any field missing from ``FIELD_EVENT`` /
``FLAG_FIELD_EVENT``** ("an unknown field is skipped"). A condition class that is registered
in ``TradeConditions.CONDITION_MAP`` -- i.e. one ``create_condition`` will happily build and
the engine will happily evaluate -- but absent from those maps is therefore a gate that:

* the UI / strategy builder offers,
* the GA emits ``cond:<id>:value`` + ``cond:<id>:enabled`` genes for and tunes for a whole
  campaign,
* and the engine NEVER SEES, because the leaf is dropped at seeding time.

The strategy then runs UNGATED while being SCORED AS IF GATED. Measured on the real built
OS1 option group before this file existed: 40 of 77 genes (52%) were dead, all four
``price_vs_target_*`` gates on every one of the five member structures.

Two prior instances of the same bug were each fixed one-field-at-a-time (the option overlay
flags, then ``days_to_expiry``). A one-field test cannot stop the next one; this file tests
the CLOSURE, so a newly registered condition fails here until it is wired.
"""
import pytest

from ba2_common.core.TradeConditions import CONDITION_MAP, RATING_CHANGE_CONDITIONS
from ba2_common.core.rule_builders import (
    FIELD_EVENT,
    FLAG_FIELD_EVENT,
    triggers_from_condition_tree,
)
from ba2_common.core.types import ExpertEventType, OrderRecommendation

#: Every event type ``create_condition`` will build a condition for.
_REGISTERED = set(CONDITION_MAP) | set(RATING_CHANGE_CONDITIONS)


def _mapped_event_types():
    return {et for et in FIELD_EVENT.values()} | {et for et in FLAG_FIELD_EVENT.values()}


def test_the_registry_covers_every_declared_event_type():
    """``_REGISTERED`` must be the WHOLE ``ExpertEventType`` surface.

    Without this, the coverage test below is trivially satisfiable by shrinking what counts
    as "registered" (that exact mutation survived the first mutation round) -- and an event
    type declared in the enum but wired to no condition class is its own bug: a rule can name
    it, the UI offers it, and ``create_condition`` raises ``Unknown event type`` at evaluation.
    """
    unregistered = sorted(et.name for et in ExpertEventType if et not in _REGISTERED)
    assert not unregistered, (
        f"declared ExpertEventType members with no entry in CONDITION_MAP / "
        f"RATING_CHANGE_CONDITIONS -- create_condition raises for them: {unregistered}"
    )


#: The 3-bucket rating word each ``rating_<from>_to_<to>`` event name uses.
_BUCKET = {
    "negative": OrderRecommendation.SELL,
    "neutral": OrderRecommendation.HOLD,
    "positive": OrderRecommendation.BUY,
}


@pytest.mark.parametrize("event_type", sorted(RATING_CHANGE_CONDITIONS, key=lambda e: e.name))
def test_rating_transition_events_build_a_rating_change_condition(event_type):
    """The six 3-bucket transitions do not live in ``CONDITION_MAP`` -- they are dispatched by
    the ``RATING_CHANGE_CONDITIONS`` branch of ``create_condition``. Nothing covered that
    branch (deleting it survived the mutation round), so the hoist that made the table a
    module constant was untested."""
    from ba2_common.core.TradeConditions import RatingChangeCondition, create_condition

    cond = create_condition(event_type, account=None, instrument_name="AAPL",
                            expert_recommendation=None)
    assert isinstance(cond, RatingChangeCondition)
    # Expectation derived from the event's own NAME, not from the table under test -- reading
    # the table back would make any swapped pair self-consistent and invisible.
    _, from_word, _, to_word = event_type.value.split("_", 3)
    assert (_BUCKET[from_word], _BUCKET[to_word]) == (cond.from_rating, cond.to_rating), (
        f"{event_type.name} dispatched to ({cond.from_rating}, {cond.to_rating}), which is "
        f"not the transition its name declares"
    )


def test_every_registered_condition_is_reachable_from_a_rule_leaf():
    """THE invariant. A registered condition with no FIELD_EVENT/FLAG_FIELD_EVENT entry is a
    gene the GA tunes and the engine cannot see."""
    missing = sorted(et.name for et in _REGISTERED - _mapped_event_types())
    assert not missing, (
        f"{len(missing)} condition(s) are registered in TradeConditions.CONDITION_MAP but "
        f"have NO rule_builders.FIELD_EVENT / FLAG_FIELD_EVENT entry, so a rule leaf naming "
        f"them is SILENTLY DROPPED by triggers_from_condition_tree and the engine never "
        f"evaluates the gate: {missing}"
    )


def test_no_field_event_entry_points_at_an_unregistered_condition():
    """The reverse direction: a mapping to an event type ``create_condition`` refuses would
    turn a seeded rule into a ``ValueError: Unknown event type`` at evaluation time."""
    orphans = sorted(et.name for et in _mapped_event_types() - _REGISTERED)
    assert not orphans, (
        f"rule_builders maps these event types but TradeConditions.create_condition raises "
        f"for them: {orphans}"
    )


@pytest.mark.parametrize("field,event_type", sorted(FIELD_EVENT.items()))
def test_numeric_map_entry_targets_a_numeric_event(field, event_type):
    """FIELD_EVENT is the NUMERIC map: its entries emit ``{event_type, operator, value}``.
    A flag event here would be given an operator/value the flag condition ignores."""
    assert event_type.name.startswith("N_"), (
        f"FIELD_EVENT[{field!r}] -> {event_type.name} is a FLAG event; it belongs in "
        f"FLAG_FIELD_EVENT (numeric triggers carry operator/value, flag triggers must not)"
    )


@pytest.mark.parametrize("field,event_type", sorted(FLAG_FIELD_EVENT.items()))
def test_flag_map_entry_targets_a_flag_event(field, event_type):
    """FLAG_FIELD_EVENT is the BOOLEAN map: its entries emit a value-less trigger. A numeric
    event here would reach ``create_condition`` with operator=None/value=None and raise."""
    assert event_type.name.startswith("F_"), (
        f"FLAG_FIELD_EVENT[{field!r}] -> {event_type.name} is a NUMERIC event; a value-less "
        f"trigger for it makes create_condition raise 'Operator and value required'"
    )


#: The only keys allowed to differ from their target's ``.value`` -- historical API/UI
#: spellings of expected_profit_target_percent. Anything else is a typo'd mapping.
_ALIASES = {"expected_profit", "expected_profit_percent"}


@pytest.mark.parametrize(
    "field,event_type",
    sorted(FIELD_EVENT.items()) + sorted(FLAG_FIELD_EVENT.items()),
)
def test_field_name_matches_its_event_type_value(field, event_type):
    """Guards the wrong-enum-member mutation: ``"iv_rank": N_DAYS_TO_EARNINGS`` is a mapping
    that exists (so the coverage test above passes) yet evaluates the WRONG condition."""
    if field in _ALIASES:
        assert event_type is ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT
        return
    assert event_type.value == field, (
        f"{field!r} is mapped to {event_type.name} (value {event_type.value!r}) -- the leaf "
        f"would silently evaluate a DIFFERENT condition than the one it names"
    )


def test_no_two_fields_share_a_numeric_event_except_the_documented_aliases():
    """``rules_tree_json`` reverses FIELD_EVENT by ``.value``; a duplicate target makes the
    reverse map ambiguous (last-one-wins) and a round-trip renames the field."""
    seen = {}
    for field, et in FIELD_EVENT.items():
        seen.setdefault(et, []).append(field)
    dupes = {et.name: sorted(fs) for et, fs in seen.items()
             if len(fs) > 1 and set(fs) - _ALIASES != {et.value}}
    assert not dupes, f"ambiguous reverse mapping: {dupes}"


@pytest.mark.parametrize("event_type", sorted(_REGISTERED, key=lambda e: e.name))
def test_a_leaf_naming_any_registered_condition_becomes_a_trigger(event_type):
    """End-to-end on the real converter: build a one-leaf tree for EVERY registered condition
    and require a trigger out. This is the test that would have caught the dropped
    ``price_vs_target_*`` gates without anybody having to name them."""
    numeric = event_type.name.startswith("N_")
    leaf = {"id": "x", "field": event_type.value}
    if numeric:
        leaf.update({"op": ">", "value": 1})
    triggers = triggers_from_condition_tree({"type": "AND", "conditions": [leaf]})
    assert triggers, (
        f"a rule leaf naming {event_type.value!r} produced NO trigger -- the gate is dropped "
        f"before the engine sees it"
    )
    (only,) = triggers.values()
    assert only["event_type"] == event_type.value
    if numeric:
        assert only == {"event_type": event_type.value, "operator": ">", "value": 1}
    else:
        assert only == {"event_type": event_type.value}


def test_every_event_type_has_a_distinct_generated_name_tag():
    """``rules_export_import._abbr_field`` falls back to a 12-char camelCase of the field name
    for anything not curated in ``_FIELD_ABBR``, and that truncation COLLIDES: all three
    ``price_vs_target_*`` events render as ``priceVsTarge``. Now that they are live rule
    fields, ``generate_rule_name`` cannot tell "price above the analyst HIGH" from "price
    below the analyst LOW" in an exported rule name."""
    from collections import defaultdict

    from ba2_common.core.rules_export_import import _abbr_field

    by_tag = defaultdict(list)
    for et in ExpertEventType:
        by_tag[_abbr_field(et.value)].append(et.value)
    collisions = {tag: sorted(vs) for tag, vs in by_tag.items() if len(vs) > 1}
    assert not collisions, (
        f"distinct event types share a generated-name tag, so exported rule names are "
        f"ambiguous: {collisions}"
    )


def test_field_abbr_keys_are_all_real_event_types():
    """``_abbr_field`` is called with an ``event_type`` VALUE, so a key that is not one is a
    curated abbreviation that can never be reached (silently dead, like the maps above)."""
    from ba2_common.core.rules_export_import import _FIELD_ABBR

    values = {et.value for et in ExpertEventType}
    stray = sorted(set(_FIELD_ABBR) - values)
    assert not stray, (
        f"_FIELD_ABBR keys that are not ExpertEventType values (unreachable): {stray}"
    )


def test_unknown_field_is_still_skipped_but_warns():
    """A genuinely unknown field must stay non-fatal (a partial/edited tree must not break
    the ruleset) -- but it must no longer be MUTE. Muteness is how this class of bug
    survived three times."""
    import logging

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    from ba2_common import logger as _logger_mod

    handler = _Capture()
    _logger_mod.logger.addHandler(handler)
    try:
        triggers = triggers_from_condition_tree(
            {"type": "AND", "conditions": [{"id": "x", "field": "frobnicate", "op": ">",
                                            "value": 1}]})
    finally:
        _logger_mod.logger.removeHandler(handler)

    assert triggers == {}, "an unknown field must still be skipped, not raise"
    assert any("frobnicate" in m for m in records), (
        "the skip was silent -- an unmapped field must be logged or the next dropped "
        "condition goes unnoticed for another campaign"
    )
