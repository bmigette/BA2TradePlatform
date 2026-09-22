"""The trigger catalog is the rule editor's whole vocabulary, so every guard here is about
something DISAPPEARING rather than about something being wrong.

The flat sixty-five-item dropdown this registry replaces had one virtue: it could not lose a
trigger, because it was the enum. A categorised menu can -- a member nobody filed lands in no
category, no category shows it, and the operator concludes the platform cannot express the rule.
These tests are the ratchet against that: every member present exactly once, every member's
categories non-empty, and a member absent from ``_CATEGORIES`` falling into the default bucket
instead of vanishing.

The second thing they pin is the market-condition MESSAGE. Fifteen fields are offered that only
work when the expert carries the right ``market_condition_profile``; the picker says which one,
and it reads that from the registry. A hardcoded field->profile table would go stale the day a
profile is renamed, and the gate it mislabels fails SILENTLY (a gate with no profile behind it
reads ``no_context`` and simply never fires), so the link to ``PROFILES`` is asserted, not the
strings.
"""
import pytest

from ba2_common.core.market_conditions import (
    OPERATORS_BY_KIND,
    PROFILES,
    FieldSpec,
    field_codes,
    field_spec,
    profile_for_field,
)
from ba2_common.core.market_condition_rules import market_condition_fields
from ba2_common.core.rules_documentation import get_event_type_documentation
from ba2_common.core.trigger_catalog import (
    CATEGORIES,
    KIND_CATEGORICAL,
    KIND_FLAG,
    KIND_NUMBER,
    TriggerEntry,
    _CATEGORIES,
    _DEFAULT_CATEGORIES,
    _categories_for,
    _kind_for,
    _market_field_description,
    _name_for,
    category_counts,
    operator_options_for,
    search_triggers,
    trigger_catalog,
    triggers_in_category,
)
from ba2_common.core.types import ExpertEventType


# --------------------------------------------------------------------------------------------
# Coverage: nothing may disappear
# --------------------------------------------------------------------------------------------

def test_every_event_type_appears_in_the_catalog_exactly_once():
    values = [e.value for e in trigger_catalog()]
    assert sorted(values) == sorted(m.value for m in ExpertEventType)
    assert len(values) == len(set(values)), "a duplicated entry would show the same trigger twice"


def test_every_event_type_is_filed_in_at_least_one_concrete_category():
    """The default bucket exists so an unfiled member is still REACHABLE, not so that filing is
    optional. A member that only ever shows up under 'Signal' because nobody categorised it is a
    mislabelled trigger, and this test is where that gets noticed."""
    unfiled = sorted(m.value for m in ExpertEventType if m.value not in _CATEGORIES)
    assert not unfiled, (
        f"ExpertEventType member(s) missing from trigger_catalog._CATEGORIES: {unfiled}. "
        f"File each one deliberately; they currently fall into {sorted(_DEFAULT_CATEGORIES)}.")


def test_a_member_with_no_category_lands_in_the_default_bucket():
    """Silent disappearance is the one failure the flat list never had."""
    assert _categories_for("a_trigger_nobody_filed") == _DEFAULT_CATEGORIES
    assert _DEFAULT_CATEGORIES, "the default bucket must not itself be empty"
    assert "all" not in _DEFAULT_CATEGORIES


def test_no_entry_is_categoryless_and_none_carries_all_or_an_unknown_category():
    known = set(CATEGORIES) - {"all"}
    for entry in trigger_catalog():
        assert entry.categories, f"{entry.value}: no category, so no tab would ever show it"
        assert "all" not in entry.categories, (
            f"{entry.value}: 'all' is the ABSENCE of a filter, never a stored category")
        assert entry.categories <= known, f"{entry.value}: unknown category {entry.categories - known}"


def test_categories_are_the_seven_the_picker_renders_in_order():
    assert CATEGORIES == ("all", "position", "signal", "targets", "options", "market", "timing")


def test_entries_are_frozen_so_the_picker_cannot_mutate_the_registry():
    entry = trigger_catalog()[0]
    with pytest.raises(Exception):
        entry.name = "mutated"  # type: ignore[misc]


# --------------------------------------------------------------------------------------------
# Counts
# --------------------------------------------------------------------------------------------

def test_category_counts_cover_every_category_and_agree_with_the_catalog():
    counts = category_counts()
    assert set(counts) == set(CATEGORIES)
    assert counts["all"] == len(trigger_catalog()) == len(list(ExpertEventType))
    for category in CATEGORIES:
        assert counts[category] == len(triggers_in_category(category)), category


def test_the_counts_the_design_agreed():
    """Pinned as NUMBERS because the chips show them: a count that drifts from the design's
    table means somebody re-filed a trigger without saying so."""
    assert category_counts() == {
        "all": 80,
        "position": 18,
        "signal": 26,
        "targets": 10,
        "options": 13,
        "market": 19,
        "timing": 12,
    }


def test_triggers_in_category_refuses_a_category_it_does_not_know():
    """An empty list for a typo'd chip would read as 'no triggers here' -- the bug wearing the
    costume of an answer."""
    with pytest.raises(ValueError, match="poistion"):
        triggers_in_category("poistion")


def test_triggers_in_category_all_is_the_whole_catalog():
    assert triggers_in_category("all") == trigger_catalog()


def test_a_multi_category_trigger_shows_up_under_each_of_its_categories():
    """Eighteen triggers sit in more than one category; the picker must not have to pick one."""
    in_options = {e.value for e in triggers_in_category("options")}
    in_timing = {e.value for e in triggers_in_category("timing")}
    assert "days_to_expiry" in in_options and "days_to_expiry" in in_timing


# --------------------------------------------------------------------------------------------
# requires_profile: read from the registry, never hardcoded
# --------------------------------------------------------------------------------------------

def test_every_market_field_names_the_profile_that_registers_it():
    by_value = {e.value: e for e in trigger_catalog()}
    registered = [f.name for prof in PROFILES.values() for f in prof.fields]
    assert registered, "no market-condition profile registered; the rest of this test is vacuous"
    for field in registered:
        assert by_value[field].requires_profile == profile_for_field(field).name, field


def test_no_non_market_trigger_claims_to_need_a_profile():
    """A spurious 'needs the ohlcv-v1 profile' note on has_position would teach the operator to
    ignore the note, which is the only thing standing between them and a gate that never fires."""
    fields = market_condition_fields()
    for entry in trigger_catalog():
        if entry.value not in fields:
            assert entry.requires_profile == "", entry.value


def test_the_market_category_holds_every_market_condition_field():
    in_market = {e.value for e in triggers_in_category("market")}
    assert market_condition_fields() <= in_market


# --------------------------------------------------------------------------------------------
# Name resolution: documentation -> ui_name -> raw key
# --------------------------------------------------------------------------------------------

def test_a_documented_trigger_uses_its_documentation_name():
    by_value = {e.value: e for e in trigger_catalog()}
    docs = get_event_type_documentation()
    assert by_value["has_position"].name == docs["has_position"]["name"]
    assert by_value["has_position"].description == docs["has_position"]["description"]


def test_a_market_field_falls_back_to_its_registry_ui_name():
    by_value = {e.value: e for e in trigger_catalog()}
    assert by_value["underlying_trend_slope_50_atr14"].name == "Underlying trend slope"


def test_an_undocumented_unregistered_trigger_renders_as_its_raw_key():
    """A trigger with no documentation entry is still OFFERED. Rendering it as its key is ugly;
    hiding it makes a deployed rule unauthorable, which is worse."""
    assert _name_for("some_new_trigger", None, "") == "some_new_trigger"


def test_the_name_chain_prefers_documentation_over_the_ui_name():
    assert _name_for("x", {"name": "Documented"}, "Registry Name") == "Documented"
    assert _name_for("x", None, "Registry Name") == "Registry Name"


def test_every_entry_has_a_non_empty_name():
    for entry in trigger_catalog():
        assert entry.name.strip(), f"{entry.value}: a blank row in the picker"


def test_every_trigger_carries_a_description_including_the_market_fields():
    """A picker row with a name and no line under it is the flat dropdown again -- and for a
    while the fifteen market fields were the ONLY rows like that, which is the opposite of what
    this feature is for: they are the vocabulary the operator has never seen. Theirs is derived
    from the FieldSpec (see :func:`_market_field_description`)."""
    undescribed = sorted(e.value for e in trigger_catalog() if not e.description.strip())
    assert not undescribed, f"trigger(s) with no description: {undescribed}"


def test_a_numeric_market_field_description_carries_its_unit_and_its_range():
    """The three facts an operator needs before typing a NUMBER into the value box: what the
    number measures, what scale it is on, and what the optimizer's own fixed reading of the field
    is. Without them the picker teaches the name and nothing else."""
    by_value = {e.value: e for e in trigger_catalog()}
    spec = field_spec("underlying_adx_14")
    description = by_value["underlying_adx_14"].description

    assert spec.unit and spec.unit in description, description
    assert "10" in description and "40" in description, "the searched range"
    assert f"{spec.anchor_op} {spec.anchor_value:g}" in description, description


def test_the_categorical_market_field_description_carries_the_code_legend():
    """``structure_state > 1`` means "bear only" and ``== 1`` means "bull". A value box with no
    legend beside it is a number the operator can only guess at."""
    description = {e.value: e for e in trigger_catalog()}["structure_state"].description

    for state, code in field_codes("structure_state").items():
        assert f"{state}={code}" in description, description


def test_the_market_field_legend_and_range_are_read_from_the_registry():
    """Never re-typed in the catalog: a literal table would go stale the day a range is retuned
    or a code added, and a WRONG legend is worse than none -- it reads as authoritative. Driven
    through an invented spec, because only the registry can supply a field the catalog has never
    heard of."""
    probe = FieldSpec(name="probe_regime", kind="categorical", short="probe", searched=False,
                      codes={"calm": 1, "storm": 2}, ui_name="Probe regime")
    description = _market_field_description(probe)
    assert "calm=1" in description and "storm=2" in description, description
    assert "Probe regime" in description

    ranged = FieldSpec(name="probe_width", kind="numeric", short="pw", searched=True,
                       value_min=1.0, value_max=8.0, value_step=0.5, anchor_op="<",
                       anchor_value=4.0, ui_name="Probe width", unit="widgets")
    numeric = _market_field_description(ranged)
    assert "widgets" in numeric and "1" in numeric and "8" in numeric, numeric
    assert "< 4" in numeric, numeric


def test_the_three_cooldown_triggers_document_the_large_sentinel():
    """The sentinel is the surprising half of the contract: with no prior close the value is a
    big number, so a '>' cooldown gate PASSES and the entry is allowed. Help text that stops at
    'days since the last close' reads as if a fresh symbol were blocked forever, which is the
    opposite of what TradeConditions.DaysSinceLastCloseCondition does."""
    by_value = {e.value: e for e in trigger_catalog()}
    for value in ("days_since_last_close", "days_since_last_profitable_close",
                  "days_since_last_losing_close"):
        description = by_value[value].description
        assert "sentinel" in description.lower(), f"{value}: does not name the sentinel"
        assert "pass" in description.lower(), (
            f"{value}: does not say that a '>' cooldown gate PASSES with no prior close")


# --------------------------------------------------------------------------------------------
# kind
# --------------------------------------------------------------------------------------------

def test_every_entry_carries_one_of_the_three_kinds():
    kinds = {KIND_FLAG, KIND_NUMBER, KIND_CATEGORICAL}
    for entry in trigger_catalog():
        assert entry.kind in kinds, f"{entry.value}: {entry.kind!r}"


def test_the_documented_type_decides_the_kind():
    assert _kind_for("F_ANYTHING", {"type": "boolean"}, "") == KIND_FLAG
    assert _kind_for("N_ANYTHING", {"type": "numeric"}, "") == KIND_NUMBER
    assert _kind_for("N_ANYTHING", {"type": "number"}, "") == KIND_NUMBER


def test_the_registry_kind_wins_for_a_market_field():
    """The documentation vocabulary has no word for 'categorical', so a doc entry added later
    for structure_state could only say 'numeric' -- and the picker would then offer a ``>``
    threshold for a regime code, a gate that compares bull(1) with bear(2) as if they were
    ordered."""
    assert _kind_for("N_STRUCTURE_STATE", {"type": "numeric"}, "categorical") == KIND_CATEGORICAL
    assert _kind_for("N_UNDERLYING_ADX", None, "numeric") == KIND_NUMBER


def test_the_enum_prefix_is_the_last_resort():
    """F_/N_ is the platform's own convention, documented on ExpertEventType itself."""
    assert _kind_for("F_SOMETHING_NEW", None, "") == KIND_FLAG
    assert _kind_for("N_SOMETHING_NEW", None, "") == KIND_NUMBER


def test_an_unreadable_kind_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="kind"):
        _kind_for("SOMETHING_NEW", None, "")
    with pytest.raises(ValueError, match="type"):
        _kind_for("N_SOMETHING", {"type": "freeform"}, "")


def test_the_categorical_market_field_is_the_only_categorical_trigger():
    categorical = {e.value for e in trigger_catalog() if e.kind == KIND_CATEGORICAL}
    assert categorical == {"structure_state"}


def test_a_flag_trigger_and_a_number_trigger_read_as_expected():
    by_value = {e.value: e for e in trigger_catalog()}
    assert by_value["has_position"].kind == KIND_FLAG
    assert by_value["days_opened"].kind == KIND_NUMBER


# --------------------------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------------------------

def test_search_matches_the_friendly_name():
    hits = {e.value for e in search_triggers("Expert Position Exists")}
    assert "has_position" in hits


def test_search_matches_the_raw_key_which_is_what_a_deployed_rule_shows():
    hits = {e.value for e in search_triggers("days_since_last_losing_close")}
    assert hits == {"days_since_last_losing_close"}


def test_search_matches_inside_the_description():
    """Finding ``days_since_last_close`` used to mean knowing it is called that."""
    hits = {e.value for e in search_triggers("cooldown")}
    assert {"days_since_last_close", "days_since_last_profitable_close",
            "days_since_last_losing_close"} <= hits


def test_search_is_case_insensitive():
    assert search_triggers("HAS_POSITION") == search_triggers("has_position")


def test_an_empty_query_returns_the_whole_category():
    assert search_triggers("") == trigger_catalog()
    assert search_triggers("   ") == trigger_catalog()
    assert search_triggers("", "options") == triggers_in_category("options")


def test_search_is_confined_to_the_category():
    assert {e.value for e in search_triggers("days_to_expiry", "options")}
    assert not search_triggers("days_to_expiry", "position")


def test_all_searches_everything():
    everywhere = {e.value for e in search_triggers("structure")}
    assert {e.value for e in search_triggers("structure", "market")} < everywhere


def test_search_refuses_an_unknown_category_like_triggers_in_category_does():
    with pytest.raises(ValueError, match="markt"):
        search_triggers("adx", "markt")


def test_search_preserves_the_catalog_order():
    hits = search_triggers("rating")
    order = [e.value for e in trigger_catalog()]
    assert [e.value for e in hits] == [v for v in order if v in {h.value for h in hits}]


# --------------------------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------------------------

def test_the_catalog_returns_a_fresh_view_of_the_live_registry():
    """Not memoised on purpose: ``market_conditions.registered_profile`` mutates PROFILES in
    place for tests and for a future profile registered at import time by a module loaded after
    this one. A cached catalog would hand the picker a profile name that is no longer true."""
    first, second = trigger_catalog(), trigger_catalog()
    assert first == second
    assert all(isinstance(e, TriggerEntry) for e in first)


# --------------------------------------------------------------------------------------------
# The operators a trigger may be compared with
# --------------------------------------------------------------------------------------------

def test_a_categorical_market_field_offers_only_equality():
    """``structure_state`` holds a regime CODE (bull=1, bear=2), so an ORDERING on it is
    meaningless: ``> 1`` reads as "bear only", which is the opposite of what an operator typing
    "1 = bull" means. The engine agrees and refuses anything else --
    ``TradeConditions._OPERATORS_BY_KIND`` is this same table -- so an editor that offered ``>``
    would be authoring a rule that raises the moment the condition is built."""
    assert operator_options_for("structure_state") == ["=="]


def test_a_numeric_market_field_offers_only_the_strict_inequalities():
    """The same table, the numeric half: a market-condition gate is a threshold, and ``==`` on a
    float measurement passes essentially never. The engine refuses the other four."""
    assert operator_options_for("underlying_adx_14") == [">", "<"]   # NUMERIC_OPERATORS order


def test_an_ordinary_numeric_trigger_still_offers_every_engine_operator():
    """Only the MARKET fields go through ``MarketConditionCompare``. Narrowing ``confidence``
    would take away ``>=``, which live rules use."""
    from ba2_common.core.types import get_operator_options

    assert operator_options_for("confidence") == get_operator_options()


def test_a_flag_trigger_offers_no_operator_at_all():
    """A flag has no threshold; an operator box beside it would invent one the engine never
    reads."""
    assert operator_options_for("has_position") == []
    assert operator_options_for("a_trigger_from_a_future_release") == []


def test_the_offered_operators_are_exactly_what_the_engine_accepts():
    """One table, two readers. Two lists would drift, and the drift is only visible when a
    deployed rule raises."""
    from ba2_common.core.TradeConditions import market_condition_condition_class

    for field in sorted(market_condition_fields() & {f.name for p in PROFILES.values()
                                                     for f in p.fields}):
        allowed = market_condition_condition_class(field).ALLOWED_OPERATORS
        assert set(operator_options_for(field)) == set(allowed), field
    assert set(OPERATORS_BY_KIND) == {"numeric", "categorical"}
