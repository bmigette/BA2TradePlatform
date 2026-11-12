"""A rule's condition TREE keeps its OR / NOT meaning on the way to live EventActions.

A live ``EventAction`` has no groups: its triggers are ANDed. The converters used to flatten every
leaf of a tree into that one AND (``rule_builders.tree_leaves``), so an exit rule
``OR[profit > 10, days_opened > 5]`` was deployed as ``profit > 10 AND days_opened > 5`` -- a
position the tree closes on day 6 stayed open live. Only a TOP-level OR on the legacy entry path
was split; the unified exporter split nothing; a NOT group was coerced to AND.

The tree is now expanded to its disjunctive normal form, ONE live rule per AND-branch, same
actions, consecutive ``order_index``, the rule's own ``continue_processing`` on every branch:

* ``continue_processing`` False: the first matching branch fires and stops, exactly as the tree
  rule would have.
* ``continue_processing`` True: every matching branch builds the same action configs and the
  evaluator drops an action it already generated this pass, so the tree's actions fire ONCE and
  evaluation goes on to the next rule.

Both are proven here by running the REAL ``TradeActionEvaluator`` over the exported rules for every
combination of leaf truth values and comparing with the tree's own semantics. NOT (and any group
operator other than AND/OR) and a DNF wider than ``MAX_DNF_BRANCHES`` are refused, by name.

An AND-only tree exports byte-identically to before (pinned against output captured from the code
before this change: ``condition_tree_export_pins.json``).
"""
from __future__ import annotations

import itertools
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core import rule_builders as RB
from ba2_common.core import rules_convert as RC
from ba2_common.core.rule_builders import FIELD_EVENT, FLAG_FIELD_EVENT
from ba2_common.core.types import OrderRecommendation

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------------------------
# Leaves
# --------------------------------------------------------------------------------------------
def _num(lid, field, op, value):
    return {"id": lid, "field": field, "op": op, "value": value}


def _flag(lid, field):
    return {"id": lid, "field": field}


PROFIT = _num("p", "profit_loss_percent", ">", 10)
DAYS = _num("d", "days_opened", ">", 5)
BEAR = _flag("x", "bearish")
BULL = _flag("b", "bullish")
LONG = _flag("lt", "long_term")
CONF = _num("c", "confidence", ">=", 60)
HELD = _flag("h", "has_position")


def _and(*kids):
    return {"type": "AND", "conditions": list(kids)}


def _or(*kids):
    return {"type": "OR", "conditions": list(kids)}


def _event(field):
    et = FLAG_FIELD_EVENT.get(field) or FIELD_EVENT[field]
    return et.value


# --------------------------------------------------------------------------------------------
# AND-only fixtures pinned byte-for-byte (condition_tree_export_pins.json was captured from the
# converters BEFORE the DNF change; regenerate it only for an intended behaviour change)
# --------------------------------------------------------------------------------------------
ENTRY = [
    {"id": "buy-1", "name": "enter-buy",
     "conditions": {"type": "AND", "conditions": [
         {"id": "b", "field": "bullish"},
         {"id": "f", "field": "has_no_position"},
         {"id": "c", "field": "confidence", "op": ">=", "value": 60},
         # nested AND and a single-child OR: both are plain conjunctions
         {"type": "AND", "conditions": [
             {"id": "ep", "field": "expected_profit", "comparison": "gte", "value": 4}]},
         {"operator": "OR", "conditions": [{"id": "lt", "field": "long_term"}]},
     ]},
     "actions": [{"action_type": "buy"},
                 {"action_type": "adjust_take_profit", "reference_value": "order_open_price",
                  "action_value": 10}]},
    {"id": "sell-1", "name": "enter-sell",
     "conditions": {"type": "AND", "conditions": [
         {"id": "br", "field": "bearish"}, {"id": "f2", "field": "has_no_position"}]},
     "actions": [{"action_type": "sell"}]},
]
EXIT = [
    {"id": "tp", "conditions": {"type": "AND", "conditions": [
        {"id": "p", "field": "profit_loss_percent", "op": ">", "value": 10}]},
     "actions": [{"action_type": "close"}]},
    {"id": "trail", "conditions": {"operator": "AND", "conditions": [
        {"id": "h", "field": "has_position"},
        {"id": "d", "field": "days_opened", "comparison": "gte", "value": 5}]},
     "actions": [{"action_type": "adjust_stop_loss", "reference_value": "current_price",
                  "action_value": -5}],
     "continue_processing": True},
    {"id": "bear", "conditions": {"type": "AND", "conditions": [{"id": "x", "field": "bearish"}]},
     "actions": [{"action_type": "close"}]},
]
LEG_BUY = {"type": "OR", "conditions": [
    {"type": "AND", "conditions": [
        {"id": "b1", "field": "bullish"},
        {"id": "c1", "field": "confidence", "op": ">=", "value": 70}]},
    {"type": "AND", "conditions": [
        {"id": "b2", "field": "bullish"}, {"id": "lt2", "field": "long_term"}]},
]}
LEG_SELL = {"type": "AND", "conditions": [{"id": "s1", "field": "bearish"}]}
LEG_EXIT = [
    {"id": "tp", "action_type": "close", "conditions": {"type": "AND", "conditions": [
        {"id": "p", "field": "profit_loss_percent", "op": ">", "value": 12}]}},
    {"id": "sl", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
     "action_value": -8, "conditions": {"type": "AND", "conditions": [
         {"id": "h", "field": "has_position"}]}},
]
LEG_ENTRY_ACTIONS = [{"id": "tp0", "action_type": "adjust_take_profit",
                      "reference_value": "order_open_price", "action_value": 12}]


# --------------------------------------------------------------------------------------------
# Reference semantics of a TradeRule list, read straight off the TREE
# --------------------------------------------------------------------------------------------
def _tree_true(node, truth):
    if not node:
        return True
    if node.get("conditions"):
        op = str(node.get("operator") or node.get("type") or "AND").upper()
        vals = [_tree_true(c, truth) for c in node["conditions"]]
        assert op in ("AND", "OR"), op
        return all(vals) if op == "AND" else any(vals)
    return truth[_event(node["field"])]


def _tree_fire(rules, truth):
    """First match wins, ``continue_processing`` falls through, and an action already generated
    this pass is not generated again -- the ``TradeActionEvaluator`` contract."""
    fired, seen = [], set()
    for rule in rules:
        if not _tree_true(rule.get("conditions"), truth):
            continue
        for a in rule["actions"]:
            key = (a["action_type"], a.get("reference_value"), a.get("action_value"))
            if key in seen:
                continue
            seen.add(key)
            fired.append(key)
        if not rule.get("continue_processing"):
            break
    return fired


# --------------------------------------------------------------------------------------------
# The REAL evaluator over exported live rules (conditions and action objects stubbed)
# --------------------------------------------------------------------------------------------
class _Account:
    id = 1


def _rec():
    return SimpleNamespace(created_at=datetime(2026, 11, 9, tzinfo=timezone.utc), instance_id=1,
                           symbol="AAPL", data={}, confidence=80.0,
                           recommended_action=OrderRecommendation.BUY)


class _Cond:
    def __init__(self, result):
        self.result = result

    def evaluate(self):
        return self.result

    def get_description(self):
        return "stub"

    def get_actual_value_display(self):
        return None


def _live_fire(monkeypatch, live_rules, truth):
    from ba2_common.core import db as dbmod
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

    ordered = sorted(live_rules, key=lambda r: r["order_index"])
    eas = [SimpleNamespace(id=i + 1, name=r["name"], triggers=r["triggers"], actions=r["actions"],
                           continue_processing=r["continue_processing"])
           for i, r in enumerate(ordered)]
    monkeypatch.setattr(dbmod, "cached_ruleset_eventactions",
                        lambda rid, loader: (SimpleNamespace(id=rid, name="rs"), eas))
    monkeypatch.setattr(TradeActionEvaluator, "_create_condition_from_trigger",
                        lambda self, et, cfg, *a, **k: _Cond(truth[et.value]))
    monkeypatch.setattr(
        TradeActionEvaluator, "_create_trade_action",
        lambda self, at, cfg, *a, **k: SimpleNamespace(
            fired=(at.value, cfg.get("reference_value"), cfg.get("value")),
            get_description=lambda: at.value))
    ev = TradeActionEvaluator(account=_Account())
    ev.evaluate("AAPL", _rec(), 1, None)
    return [ta.fired for ta in ev.trade_actions]


def _leaf_events(rules):
    out = []
    for r in rules:
        for leaf in RB.tree_leaves(r.get("conditions")):
            ev = _event(leaf["field"])
            if ev not in out:
                out.append(ev)
    return out


def _assert_truth_table(monkeypatch, trade_rules, live_rules):
    events = _leaf_events(trade_rules)
    for combo in itertools.product((False, True), repeat=len(events)):
        truth = dict(zip(events, combo))
        assert _live_fire(monkeypatch, live_rules, truth) == _tree_fire(trade_rules, truth), truth


def _ruleset(export, subtype):
    (rs,) = [r for r in export["rulesets"] if r["subtype"] == subtype]
    return rs["rules"]


def _close(rid, tree, **extra):
    return {"id": rid, "name": rid, "conditions": tree, "actions": [{"action_type": "close"}],
            **extra}


def _stop(rid, tree, value, **extra):
    return {"id": rid, "name": rid, "conditions": tree,
            "actions": [{"action_type": "adjust_stop_loss", "reference_value": "current_price",
                         "action_value": value}], **extra}


# --------------------------------------------------------------------------------------------
# Unified exporter
# --------------------------------------------------------------------------------------------
def test_an_or_exit_tree_exports_one_rule_per_branch(monkeypatch):
    rules = [_close("or_exit", _or(PROFIT, DAYS)),
             _stop("after", _and(BEAR), -3)]
    live = _ruleset(RC.trade_rules_to_live_export(exit_rules=rules, name="t"), "open_positions")
    assert [r["name"] for r in live] == ["or_exit-or1", "or_exit-or2", "after"]
    assert [r["order_index"] for r in live] == [0, 1, 2]
    assert [list(r["triggers"].values()) for r in live[:2]] == [
        [{"event_type": "profit_loss_percent", "operator": ">", "value": 10}],
        [{"event_type": "days_opened", "operator": ">", "value": 5}]]
    assert all(r["continue_processing"] is False for r in live)
    _assert_truth_table(monkeypatch, rules, live)


def test_an_and_over_an_or_entry_becomes_two_rules_each_carrying_the_and_leaf(monkeypatch):
    rules = [{"id": "e", "name": "e", "conditions": _and(BULL, _or(LONG, CONF)),
              "actions": [{"action_type": "buy"}]}]
    live = _ruleset(RC.trade_rules_to_live_export(entry_rules=rules, name="t"), "enter_market")
    assert [sorted(t["event_type"] for t in r["triggers"].values()) for r in live] == [
        ["bullish", "long_term"], ["bullish", "confidence"]]
    # positional keys are the leaf's position in the WHOLE tree, so the leaf map still applies
    assert [list(r["triggers"]) for r in live] == [["cond_0", "cond_1"], ["cond_0", "cond_2"]]
    _assert_truth_table(monkeypatch, rules, live)


@pytest.mark.parametrize("cp", [False, True], ids=["stop", "continue"])
def test_a_nested_tree_matches_its_tree_semantics_on_every_input(monkeypatch, cp):
    tree = _or(_and(HELD, _or(PROFIT, DAYS)), BEAR)        # DNF: h&p | h&d | x
    rules = [_stop("trail", tree, -4, continue_processing=cp),
             _close("tail", _and(BULL))]
    live = _ruleset(RC.trade_rules_to_live_export(exit_rules=rules, name="t"), "open_positions")
    assert len(live) == 4
    assert [r["continue_processing"] for r in live[:3]] == [cp] * 3
    _assert_truth_table(monkeypatch, rules, live)


def test_continue_processing_or_rule_fires_its_action_once(monkeypatch):
    rules = [_stop("trail", _or(PROFIT, DAYS), -4, continue_processing=True),
             _close("tail", _and(BEAR))]
    live = _ruleset(RC.trade_rules_to_live_export(exit_rules=rules, name="t"), "open_positions")
    truth = {"profit_loss_percent": True, "days_opened": True, "bearish": True}
    assert _live_fire(monkeypatch, live, truth) == [
        ("adjust_stop_loss", "current_price", -4), ("close", None, None)]


@pytest.mark.parametrize("op", ["NOT", "not", "XOR", "NAND"])
def test_a_negation_or_unknown_group_is_refused(op):
    rules = [_close("neg", _and(HELD, {"type": op, "conditions": [PROFIT]}))]
    with pytest.raises(RB.UnsupportedConditionGroupError, match=rf"'neg'.*{op.upper()}"):
        RC.trade_rules_to_live_export(exit_rules=rules, name="t")


def test_a_group_whose_operator_and_type_disagree_is_refused():
    rules = [_close("two", {"operator": "OR", "type": "AND", "conditions": [PROFIT, DAYS]})]
    with pytest.raises(RB.UnsupportedConditionGroupError, match="'two'.*disagree"):
        RC.trade_rules_to_live_export(exit_rules=rules, name="t")


def test_a_tree_wider_than_the_bound_is_refused():
    wide = _and(*[_or(_num(f"a{i}", "days_opened", ">", i), _num(f"b{i}", "profit_loss_percent", ">", i))
                  for i in range(4)])                       # 2**4 = 16 branches
    with pytest.raises(RB.ConditionTreeTooWideError, match=rf"'wide'.*16.*{RB.MAX_DNF_BRANCHES}"):
        RC.trade_rules_to_live_export(exit_rules=[_close("wide", wide)], name="t")
    # exactly at the bound is fine
    ok = _and(*[_or(_num(f"a{i}", "days_opened", ">", i), _num(f"b{i}", "profit_loss_percent", ">", i))
                for i in range(3)])
    live = _ruleset(RC.trade_rules_to_live_export(exit_rules=[_close("ok", ok)], name="t"),
                    "open_positions")
    assert len(live) == RB.MAX_DNF_BRANCHES == 8


def test_an_or_branch_whose_leaves_all_drop_is_refused_not_made_always_true():
    unknown = {"id": "u", "field": "no_such_field_ever", "op": ">", "value": 1}
    with pytest.raises(ValueError, match="'br'.*ALWAYS TRUE"):
        RC.trade_rules_to_live_export(exit_rules=[_close("br", _or(unknown, DAYS))], name="t")


def test_and_only_exports_are_byte_identical_to_before():
    with open(os.path.join(HERE, "condition_tree_export_pins.json"), encoding="utf-8") as fh:
        pins = json.load(fh)
    assert RC.trade_rules_to_live_export(ENTRY, EXIT, name="pin") == pins["unified"]
    assert RC.strategy_to_live_export(LEG_BUY, LEG_SELL, LEG_EXIT, name="pin",
                                      entry_actions=LEG_ENTRY_ACTIONS) == pins["legacy"]


# --------------------------------------------------------------------------------------------
# Legacy exporter (strategy_to_live_export): same expansion, continue_processing carried
# --------------------------------------------------------------------------------------------
def _legacy_close(rid, tree, **extra):
    return {"id": rid, "action_type": "close", "conditions": tree, **extra}


def test_legacy_exit_or_tree_is_split_and_matches_the_tree(monkeypatch):
    rows = [_legacy_close("or_exit", _or(PROFIT, DAYS)),
            {"id": "after", "action_type": "adjust_stop_loss", "reference_value": "current_price",
             "action_value": -3, "conditions": _and(BEAR)}]
    live = _ruleset(RC.strategy_to_live_export(exit_rules=rows, name="t"), "open_positions")
    assert [r["order_index"] for r in live] == [0, 1, 2]
    as_trade_rules = [
        {"conditions": r["conditions"], "continue_processing": r.get("continue_processing", False),
         "actions": [{k: v for k, v in r.items() if k in ("action_type", "reference_value",
                                                           "action_value")}]}
        for r in rows]
    _assert_truth_table(monkeypatch, as_trade_rules, live)


def test_legacy_exit_carries_continue_processing():
    rows = [_legacy_close("a", _and(PROFIT), continue_processing=True),
            _legacy_close("b", _and(DAYS), continueProcessing=True),
            _legacy_close("c", _and(BEAR))]
    live = _ruleset(RC.strategy_to_live_export(exit_rules=rows, name="t"), "open_positions")
    assert [r["continue_processing"] for r in live] == [True, True, False]


def test_legacy_entry_nested_or_is_split(monkeypatch):
    buy = _and(BULL, _or(LONG, CONF))
    live = _ruleset(RC.strategy_to_live_export(buy_tree=buy, name="t"), "enter_market")
    assert [r["name"] for r in live] == ["t-buy-or1", "t-buy-or2"]
    _assert_truth_table(monkeypatch, [{"conditions": buy, "actions": [{"action_type": "buy"}]}],
                        live)


def test_legacy_entry_not_is_refused():
    with pytest.raises(RB.UnsupportedConditionGroupError, match="NOT"):
        RC.strategy_to_live_export(buy_tree=_and(BULL, {"type": "NOT", "conditions": [LONG]}),
                                   name="t")


# --------------------------------------------------------------------------------------------
# The flattening converter refuses what it cannot represent; the canonical model refuses NOT
# --------------------------------------------------------------------------------------------
def test_flatten_refuses_a_real_or_and_a_not():
    with pytest.raises(RB.ConditionTreeExportError, match="OR"):
        RB.triggers_from_condition_tree(_or(PROFIT, DAYS))
    with pytest.raises(RB.UnsupportedConditionGroupError):
        RB.triggers_from_condition_tree({"type": "NOT", "conditions": [PROFIT]})
    # a single-child OR is a plain conjunction and still flattens
    assert list(RB.triggers_from_condition_tree(_or(PROFIT)).values()) == [
        {"event_type": "profit_loss_percent", "operator": ">", "value": 10}]


def test_normalizing_a_not_group_is_refused_instead_of_coerced_to_and():
    from ba2_common.core.rule_models import normalize_trade_rules
    with pytest.raises(ValueError, match="NOT"):
        normalize_trade_rules([_close("n", {"type": "NOT", "conditions": [PROFIT]})])


# --------------------------------------------------------------------------------------------
# Reverse direction (live -> backtest): no silent skip, no invented operator
# --------------------------------------------------------------------------------------------
def _live_payload(triggers):
    return {"export_type": "ruleset", "ruleset": {"subtype": "open_positions", "rules": [
        {"name": "r", "triggers": triggers, "actions": {"a": {"action_type": "close"}},
         "order_index": 0}]}}


@pytest.mark.parametrize("convert", [RC.live_export_to_trade_rules, RC.live_export_to_strategy],
                         ids=["unified", "legacy"])
def test_an_unknown_trigger_event_type_is_refused(convert):
    payload = _live_payload({"t0": {"event_type": "from_newer_code"}})
    with pytest.raises(RC.UnknownTriggerEventTypeError, match="from_newer_code"):
        convert(payload)


@pytest.mark.parametrize("convert", [RC.live_export_to_trade_rules, RC.live_export_to_strategy],
                         ids=["unified", "legacy"])
def test_a_numeric_trigger_without_operator_is_refused(convert):
    payload = _live_payload({"t0": {"event_type": "days_opened", "value": 5}})
    with pytest.raises(RC.MissingTriggerOperatorError, match="days_opened"):
        convert(payload)


def test_known_triggers_still_convert_and_v11_op_is_read():
    out = RC.live_export_to_trade_rules(_live_payload({
        "t0": {"event_type": "days_opened", "op": ">=", "value": 5},
        "t1": {"event_type": "has_position"}}))
    leaves = out["exit_rules"][0]["conditions"]["conditions"]
    assert [(lf["field"], lf.get("comparison")) for lf in leaves] == [
        ("days_opened", ">="), ("has_position", "is_true")]
