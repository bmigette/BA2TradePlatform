"""The backtest seeder expands an OR condition tree EXACTLY as the live export does.

``rules_convert.trade_rules_to_live_export`` / ``strategy_to_live_export`` write one live rule
per AND-branch of a rule's tree (same actions, same ``continue_processing``, consecutive order).
The backtest seeds its per-run ruleset through ``default_rulesets``; if the two expanded
differently, a deployed strategy would trade differently from the backtest that selected it. The
truth-table proof that the expansion keeps the tree's meaning under the real evaluator lives in
``packages/common/tests/test_condition_tree_or_export.py``; this file pins seeder == export.
"""
from __future__ import annotations

import pytest

import ba2_common.core.db as cdb
from app.services.backtest import default_rulesets as dr
from app.services.backtest.backtest_db import backtest_trading_db
from app.services.backtest.seam_wiring import wire_backtest_seams
from ba2_common.core import rule_builders as RB
from ba2_common.core.rules_convert import strategy_to_live_export, trade_rules_to_live_export
from ba2_common.core.types import AnalysisUseCase

PROFIT = {"id": "p", "field": "profit_loss_percent", "op": ">", "value": 10}
DAYS = {"id": "d", "field": "days_opened", "op": ">", "value": 5}
HELD = {"id": "h", "field": "has_position"}
BEAR = {"id": "x", "field": "bearish"}
BULL = {"id": "b", "field": "bullish"}
LONG = {"id": "lt", "field": "long_term"}
CONF = {"id": "c", "field": "confidence", "op": ">=", "value": 60}


def _and(*kids):
    return {"type": "AND", "conditions": list(kids)}


def _or(*kids):
    return {"type": "OR", "conditions": list(kids)}


@pytest.fixture()
def _trading_db():
    wire_backtest_seams()
    with backtest_trading_db("or-tree-seed-parity"):
        yield


def _seeded(ruleset_id):
    return [{"name": ea.name, "triggers": ea.triggers, "actions": ea.actions,
             "continue_processing": bool(ea.continue_processing)}
            for ea in cdb.ruleset_event_actions(ruleset_id)]


def _exported(export, subtype):
    (rs,) = [r for r in export["rulesets"] if r["subtype"] == subtype]
    return [{k: r[k] for k in ("name", "triggers", "actions", "continue_processing")}
            for r in sorted(rs["rules"], key=lambda r: r["order_index"])]


EXIT_RULES = [
    {"id": "trail", "name": "trail", "conditions": _or(_and(HELD, _or(PROFIT, DAYS)), BEAR),
     "actions": [{"action_type": "adjust_stop_loss", "reference_value": "current_price",
                  "action_value": -4}], "continue_processing": True},
    {"id": "tail", "name": "tail", "conditions": _and(BULL), "actions": [{"action_type": "close"}]},
]
ENTRY_RULES = [
    {"id": "e", "name": "e", "conditions": _and(BULL, _or(LONG, CONF)),
     "actions": [{"action_type": "buy"}]},
]


def test_unified_seeder_matches_the_unified_export(_trading_db):
    rid = dr.seed_exit_ruleset_from_rules(EXIT_RULES, name="p")
    exported = _exported(trade_rules_to_live_export(exit_rules=EXIT_RULES, name="p"),
                         "open_positions")
    assert _seeded(rid) == exported
    assert [r["name"] for r in exported] == ["trail-or1", "trail-or2", "trail-or3", "tail"]

    rid = dr.seed_entry_ruleset_from_rules(ENTRY_RULES, name="p")
    assert _seeded(rid) == _exported(trade_rules_to_live_export(entry_rules=ENTRY_RULES, name="p"),
                                     "enter_market")


def test_unified_seeder_refuses_not(_trading_db):
    rule = {"id": "n", "conditions": _and(HELD, {"type": "NOT", "conditions": [PROFIT]}),
            "actions": [{"action_type": "close"}]}
    with pytest.raises(RB.UnsupportedConditionGroupError, match="'n'.*NOT"):
        dr.seed_exit_ruleset_from_rules([rule], name="p")


def test_legacy_exit_seeder_matches_the_legacy_export(_trading_db):
    rows = [{"id": "or_exit", "action_type": "close", "conditions": _or(PROFIT, DAYS),
             "continue_processing": True},
            {"id": "sl", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
             "action_value": -8, "conditions": _and(HELD)}]
    rid = dr.seed_open_positions_ruleset(rows, name="p")
    seeded = _seeded(rid)
    exported = _exported(strategy_to_live_export(exit_rules=rows, name="p"), "open_positions")
    # names differ by construction (seeder "<name>-rule-<i>", export "<name>-exit-<i>")
    strip = [{k: v for k, v in r.items() if k != "name"} for r in seeded]
    assert strip == [{k: v for k, v in r.items() if k != "name"} for r in exported]
    assert [r["continue_processing"] for r in seeded] == [True, True, False]
    assert [r["name"] for r in seeded] == ["p-rule-0-or1", "p-rule-0-or2", "p-rule-1"]


def test_legacy_entry_seeder_splits_a_nested_or_like_the_legacy_export():
    tree = _and(BULL, _or(LONG, CONF))
    gates = dr._gate_trigger_groups(tree)
    rules = _exported(strategy_to_live_export(buy_tree=tree, name="p"), "enter_market")
    assert [{k.replace("gate_", "cond_"): v for k, v in g.items()} for g in gates] == [
        r["triggers"] for r in rules]
    assert len(gates) == 2


def test_legacy_entry_seeder_top_level_or_of_groups_is_unchanged():
    tree = _or(_and(BULL, CONF), _and(BULL, LONG))
    assert dr._gate_trigger_groups(tree) == [
        {"gate_0": {"event_type": "bullish"},
         "gate_1": {"event_type": "confidence", "operator": ">=", "value": 60}},
        {"gate_0": {"event_type": "bullish"}, "gate_1": {"event_type": "long_term"}}]


def test_legacy_entry_seeder_refuses_not():
    with pytest.raises(RB.UnsupportedConditionGroupError):
        dr._gate_trigger_groups(_and(BULL, {"type": "NOT", "conditions": [LONG]}))
