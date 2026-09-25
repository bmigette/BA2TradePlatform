"""``EntryTagCondition``: the ``entry_tag`` leaf a style expert ANDs into each member's exits.

WHY (plan 2026-09-25 option-stage2-build, Task 2). One OPEN_POSITIONS ruleset carries every
member's OWN exits; each exit is scoped to its member by ``entry_tag == <member tag>``. The tag
is read back off the entry order row (``ba2_common.core.entry_tag``, written at submit by
Task 1). Shared by live and backtest.

WHAT IS PINNED HERE.

* UNKNOWN NEVER FIRES. No order, no row data, no tag -> ``calculated_value`` None and False for
  every operator. An untagged position is not a style position, so no style exit may close it.
* THE TAG IS A LABEL, NOT A QUANTITY. Only ``==`` and ``!=`` mean anything; an ordering
  operator (``entry_tag > 3``) compares member codes as if they sat on a scale, and a
  non-integer rule value can never match any tag. Both are refused LOUDLY at construction
  rather than evaluating to a quiet, permanent False.
* A CORRUPTED STORED TAG PROPAGATES. It is a defect in the row, not an "absent" reading
  (BA2_ERROR_MODE=enforce: a ValueError is not a benign type).
* REACHABLE AND INERT FOR CLASSIFICATION. The leaf becomes a real engine trigger, is never
  dropped silently, and never changes an exit's forced/discretionary class or close reason.
"""
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeConditions import CONDITION_MAP, EntryTagCondition
from ba2_common.core.rule_builders import (
    FIELD_EVENT,
    rule_triggers_from_tree,
    triggers_from_condition_tree,
)
from ba2_common.core.types import ExpertEventType


def _cond(order, op="==", value=3.0):
    return EntryTagCondition(account=None, instrument_name="AAPL",
                             expert_recommendation=None, operator_str=op, value=value,
                             existing_order=order)


def _order(tag):
    return SimpleNamespace(id=1, data=({"entry_tag": tag} if tag is not None else {}),
                           parent_order_id=None, contract_symbol=None)


def test_matches_its_own_member():
    c = _cond(_order(3))
    assert c.evaluate() is True
    assert c.get_calculated_value() == 3.0


def test_rejects_another_member():
    assert _cond(_order(4)).evaluate() is False


def test_not_equal_selects_every_OTHER_tagged_member():
    assert _cond(_order(4), op="!=").evaluate() is True
    assert _cond(_order(3), op="!=").evaluate() is False


@pytest.mark.parametrize("op", ["==", "!="])
def test_an_untagged_position_never_fires_for_any_operator(op):
    c = _cond(_order(None), op=op)
    assert c.evaluate() is False and c.get_calculated_value() is None


@pytest.mark.parametrize("order", [
    None,
    SimpleNamespace(id=1),
    SimpleNamespace(id=1, data=None),
    SimpleNamespace(id=1, data={"max_loss_per_contract": 500.0}),
])
def test_no_tag_in_any_shape_never_fires(order):
    for op in ("==", "!="):
        c = _cond(order, op=op)
        assert c.evaluate() is False and c.get_calculated_value() is None


def test_no_existing_order_never_fires():
    assert _cond(None).evaluate() is False


@pytest.mark.parametrize("op", [">", "<", ">=", "<="])
def test_an_ordering_operator_is_refused_at_construction(op):
    with pytest.raises(ValueError, match="entry_tag"):
        _cond(_order(3), op=op)


@pytest.mark.parametrize("value", [0, -1, 2.5, "3", True, None])
def test_a_rule_value_no_tag_can_equal_is_refused_at_construction(value):
    with pytest.raises(ValueError, match="entry_tag"):
        _cond(_order(3), value=value)


def test_an_integer_rule_value_is_accepted():
    assert _cond(_order(3), value=3).evaluate() is True


def test_a_corrupted_stored_tag_propagates(monkeypatch):
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    with pytest.raises(ValueError, match="entry_tag"):
        _cond(_order("x")).evaluate()


def test_registered_everywhere_the_engine_reads_a_field():
    assert CONDITION_MAP[ExpertEventType.N_ENTRY_TAG] is EntryTagCondition
    assert ExpertEventType.N_ENTRY_TAG.value == "entry_tag"
    assert FIELD_EVENT["entry_tag"] == ExpertEventType.N_ENTRY_TAG
    tree = {"type": "AND", "conditions": [
        {"id": "x-tag", "field": "entry_tag", "op": "==", "value": 3.0}]}
    (trig,) = triggers_from_condition_tree(tree).values()
    assert trig["event_type"] == "entry_tag" and float(trig["value"]) == 3.0
    assert trig["operator"] == "=="


def test_the_engine_builds_it_from_a_trigger():
    """``create_condition`` -- the factory the evaluator calls per trigger -- builds this class
    for an ``entry_tag`` trigger (so the leaf is evaluated, not merely stored)."""
    from ba2_common.core.TradeConditions import create_condition
    c = create_condition(ExpertEventType.N_ENTRY_TAG, account=None, instrument_name="AAPL",
                         expert_recommendation=None, operator_str="==", value=3.0,
                         existing_order=_order(3))
    assert isinstance(c, EntryTagCondition) and c.evaluate() is True


def test_a_tag_leaf_without_a_value_is_refused_not_dropped():
    """A dropped tag leaf turns a member-scoped exit into an exit for EVERY position on the
    symbol (fail-OPEN across members), so the TradeRule->EventAction choke point refuses it,
    exactly as it refuses a dropped market-condition leaf."""
    tree = {"type": "AND", "conditions": [
        {"id": "tp", "field": "profit_loss_percent", "op": ">", "value": 50.0},
        {"id": "o_lc-tag", "field": "entry_tag", "op": "=="}]}
    with pytest.raises(ValueError, match="entry_tag"):
        rule_triggers_from_tree(tree, where="rule o_lc-opt_tp")


def test_a_rule_without_a_tag_leaf_converts_exactly_as_before():
    tree = {"type": "AND", "conditions": [
        {"id": "tp", "field": "profit_loss_percent", "op": ">", "value": 50.0}]}
    assert rule_triggers_from_tree(tree, where="r") == {
        "cond_0": {"event_type": "profit_loss_percent", "operator": ">", "value": 50.0}}


@pytest.mark.parametrize("base", [
    {"event_type": "profit_loss_percent", "operator": ">", "value": 50},     # TP: discretionary
    {"event_type": "profit_loss_percent", "operator": "<", "value": -50},    # SL: forced
    {"event_type": "days_to_expiry", "operator": "<=", "value": 21},         # DTE: forced
    {"event_type": "days_opened", "operator": ">=", "value": 30},            # time: discretionary
])
def test_the_tag_never_changes_an_exits_classification(base):
    """A tag leaf ANDed into an exit must not change that exit's forced/discretionary class,
    nor the close reason recorded for it."""
    from ba2_common.core.TradeActionEvaluator import forced_option_exit, option_close_trigger
    alone = SimpleNamespace(triggers={"cond_0": base})
    scoped = SimpleNamespace(triggers={
        "cond_0": base, "cond_1": {"event_type": "entry_tag", "operator": "==", "value": 3}})
    assert forced_option_exit(scoped) is forced_option_exit(alone)
    assert option_close_trigger(scoped) == option_close_trigger(alone)


def test_the_tag_is_never_a_forced_exit_trigger():
    from ba2_common.core.TradeActionEvaluator import forced_option_exit
    tp = SimpleNamespace(triggers={
        "cond_0": {"event_type": "profit_loss_percent", "operator": ">", "value": 50},
        "cond_1": {"event_type": "entry_tag", "operator": "==", "value": 3}})
    assert forced_option_exit(tp) is False


def test_the_stored_row_from_a_real_entry_is_what_the_condition_reads(tmp_path):
    """End to end across the seam: a REAL tagged entry submits, its STORED parent row is
    fetched back from the DB, and the condition reads the tag off it (the Task-1 writer and
    this reader agree on the key and the type)."""
    from ba2_common.core import db
    from tests.test_entry_tag_fact import _builder, _call_leg
    from ba2_common.core.types import ExpertActionType

    db.configure_db(str(tmp_path / "entry_tag_condition.sqlite"))
    db.init_db()
    buy_call_with = _builder(ExpertActionType.BUY_CALL, [_call_leg()], "long_call")
    stored = buy_call_with(entry_tag=14).submitted_order()
    assert _cond(stored, value=14.0).evaluate() is True
    assert _cond(stored, value=3.0).evaluate() is False
    untagged = buy_call_with().submitted_order()
    assert _cond(untagged, value=14.0).evaluate() is False


def test_it_is_a_numeric_event_the_editor_offers_only_equality_on():
    """The rule editor draws the operator box from ``operator_options_for`` -- the SAME
    ``ENTRY_TAG_OPERATORS`` table the condition enforces, so the editor cannot offer an
    operator the engine refuses."""
    from ba2_common.core.trigger_catalog import operator_options_for, trigger_catalog
    from ba2_common.core.types import get_numeric_event_values, is_numeric_event
    assert "entry_tag" in get_numeric_event_values() and is_numeric_event("entry_tag")
    assert operator_options_for("entry_tag") == ["==", "!="]
    (entry,) = [e for e in trigger_catalog() if e.value == "entry_tag"]
    assert "position" in entry.categories and entry.description


def test_the_generated_rule_name_tag_is_curated():
    from ba2_common.core.rules_export_import import _abbr_field
    assert _abbr_field("entry_tag") == "etag"


# --------------------------------------------------------------------------- #
# conversion time, EVERY path (review 2026-09-25 follow-up): the shared leaf->trigger
# converter refuses a tag leaf it cannot honour, so no path can drop it with a WARNING
# (a dropped tag leaf = an exit applying to every member's positions on the symbol).
# --------------------------------------------------------------------------- #
_TP = {"id": "tp", "field": "profit_loss_percent", "op": ">", "value": 50.0}


def _tree(tag_leaf):
    return {"type": "AND", "conditions": [dict(_TP), tag_leaf]}


_BAD_TAG_LEAVES = [
    pytest.param({"id": "t", "field": "entry_tag", "op": "=="}, id="no-value"),
    pytest.param({"id": "t", "field": "entry_tag", "op": "==", "value": None}, id="null-value"),
    pytest.param({"id": "t", "field": "entry_tag", "value": 3.0}, id="no-operator"),
    pytest.param({"id": "t", "field": "entry_tag", "op": ">", "value": 3.0}, id="ordering-op"),
    pytest.param({"id": "t", "field": "entry_tag", "op": "gte", "value": 3.0}, id="word-op"),
    pytest.param({"id": "t", "field": "entry_tag", "op": "==", "value": 2.5}, id="fraction"),
    pytest.param({"id": "t", "field": "entry_tag", "op": "==", "value": 0}, id="zero"),
    pytest.param({"id": "t", "field": "entry_tag", "op": "==", "value": "3"}, id="string"),
    pytest.param({"id": "t", "field": "entry_tag", "op": "==", "value": True}, id="bool"),
]


@pytest.mark.parametrize("leaf", _BAD_TAG_LEAVES)
def test_the_shared_converter_refuses_a_tag_leaf_it_cannot_honour(leaf):
    with pytest.raises(ValueError, match="entry_tag"):
        triggers_from_condition_tree(_tree(leaf))


@pytest.mark.parametrize("leaf", _BAD_TAG_LEAVES)
def test_the_live_export_exit_path_refuses_it(leaf):
    """rules_convert.strategy_to_live_export's exit loop (the legacy one-action-per-rule
    path) used to DROP an unmappable leaf with a WARNING."""
    from ba2_common.core.rules_convert import strategy_to_live_export
    rule = {"id": "o_lc-opt_tp", "action_type": "close_option", "conditions": _tree(leaf)}
    with pytest.raises(ValueError, match="entry_tag"):
        strategy_to_live_export(exit_rules=[rule])


@pytest.mark.parametrize("leaf", _BAD_TAG_LEAVES)
def test_the_trade_rule_path_refuses_it(leaf):
    """The TradeRule -> EventAction choke point (backtest seeder + deploy export) refuses it
    too, naming the RULE as well as the leaf."""
    with pytest.raises(ValueError, match="entry_tag") as err:
        rule_triggers_from_tree(_tree(leaf), where="rule o_lc-opt_tp")
    assert "rule o_lc-opt_tp" in str(err.value)


@pytest.mark.parametrize("op", ["==", "!=", "eq", "ne"])
def test_a_valid_tag_leaf_converts_with_an_equality_operator(op):
    triggers = triggers_from_condition_tree(_tree(
        {"id": "t", "field": "entry_tag", "op": op, "value": 3}))
    assert triggers["cond_1"]["event_type"] == "entry_tag"
    assert triggers["cond_1"]["operator"] in ("==", "!=")
    assert triggers["cond_1"]["value"] == 3


def test_a_tree_without_a_tag_leaf_converts_byte_identically():
    """No existing rule has a tag leaf: their conversion must not move at all."""
    assert triggers_from_condition_tree({"type": "AND", "conditions": [dict(_TP)]}) == {
        "cond_0": {"event_type": "profit_loss_percent", "operator": ">", "value": 50.0}}


def test_the_editor_does_not_offer_the_tag_on_an_ENTRY_rule():
    """At entry there is no position yet, so ``entry_tag`` can never match on an enter_market
    rule: offering it there is a gate that silently blocks every entry."""
    from ba2_common.core.trigger_catalog import (category_counts, search_triggers,
                                                 trigger_offered_for)
    assert trigger_offered_for("entry_tag", "open_positions") is True
    assert trigger_offered_for("entry_tag", "enter_market") is False
    assert trigger_offered_for("profit_loss_percent", "enter_market") is True
    assert "entry_tag" in {e.value for e in search_triggers("", "all", "open_positions")}
    assert "entry_tag" not in {e.value for e in search_triggers("", "all", "enter_market")}
    assert "entry_tag" not in {e.value for e in search_triggers("tag", "position", "enter_market")}
    assert "entry_tag" in {e.value for e in search_triggers("", "all")}      # no subtype: all
    entry_counts, exit_counts = category_counts("enter_market"), category_counts("open_positions")
    assert exit_counts["all"] - entry_counts["all"] == 1
    assert exit_counts["position"] - entry_counts["position"] == 1
    assert category_counts() == exit_counts
    with pytest.raises(ValueError, match="subtype"):
        trigger_offered_for("entry_tag", "enter_markt")
