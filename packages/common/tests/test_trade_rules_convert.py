"""Lossless live<->TradeRule converters: multi-action rules, per-tier brackets,
continue_processing, stop_processing guards, and full round-trip fidelity."""
from ba2_common.core.rule_builders import (  # canonical import point (rules_convert is cyclic)
    live_export_to_trade_rules,
    trade_rules_to_live_export,
)


def _tier_rule(name, order, conf, tp, sl):
    """One live enter_market rule shaped like the prod FMPSenateTraderWeight per-tier pattern:
    buy + adjust_take_profit(expert_target_price) + adjust_stop_loss(order_open_price)."""
    return {
        "name": name, "subtype": "enter_market", "order_index": order,
        "triggers": {
            "t0": {"event_type": "bullish"},
            "t1": {"event_type": "confidence", "operator": ">=", "value": conf},
            "t2": {"event_type": "has_no_position"},
        },
        "actions": {
            "a0": {"action_type": "buy"},
            "a1": {"action_type": "adjust_take_profit",
                   "reference_value": "expert_target_price", "value": tp},
            "a2": {"action_type": "adjust_stop_loss",
                   "reference_value": "order_open_price", "value": sl},
        },
        "continue_processing": False,
    }


def _payload(rules, subtype="enter_market", extra_rulesets=()):
    return {"export_type": "rulesets", "rulesets": [
        {"name": "rs", "subtype": subtype, "rules": rules}, *extra_rulesets]}


def test_per_tier_brackets_survive_import():
    """4 buy tiers with DIFFERENT TP/SL each — the pattern the legacy importer flattened
    (ignored_initial_brackets). Each rule keeps its own bracket now."""
    tiers = [(-5, -12), (-5, -10), (0, -8), (-8, -10)]
    rules = [_tier_rule(f"BUY_T{i}", i, 60 + i * 10, tp, sl)
             for i, (tp, sl) in enumerate(tiers)]
    got = live_export_to_trade_rules(_payload(rules))
    assert len(got["entry_rules"]) == 4 and got["exit_rules"] == []
    for i, (tp, sl) in enumerate(tiers):
        acts = got["entry_rules"][i]["actions"]
        assert [a["action_type"] for a in acts] == ["buy", "adjust_take_profit", "adjust_stop_loss"]
        assert acts[1]["action_value"] == tp and acts[1]["reference_value"] == "expert_target_price"
        assert acts[2]["action_value"] == sl and acts[2]["reference_value"] == "order_open_price"
        # adjust values are optimizable by default
        assert acts[2]["action_value_optimize"] is True


def test_continue_processing_and_multi_action_exit_survive():
    op_rules = [
        {"name": "ratchet", "subtype": "open_positions", "order_index": 0,
         "triggers": {"t0": {"event_type": "profit_loss_percent", "operator": ">", "value": 10}},
         "actions": {"a0": {"action_type": "adjust_stop_loss",
                            "reference_value": "order_open_price", "value": 2},
                     "a1": {"action_type": "adjust_take_profit",
                            "reference_value": "order_open_price", "value": 30}},
         "continue_processing": True},
        {"name": "close-bearish", "subtype": "open_positions", "order_index": 1,
         "triggers": {"t0": {"event_type": "bearish"}},
         "actions": {"a0": {"action_type": "close"}},
         "continue_processing": False},
    ]
    got = live_export_to_trade_rules(_payload(op_rules, subtype="open_positions"))
    assert got["entry_rules"] == []
    r0, r1 = got["exit_rules"]
    assert r0["continue_processing"] is True   # was silently dropped by the legacy importer
    assert len(r0["actions"]) == 2             # legacy took "first usable one" only
    assert r1["continue_processing"] is False
    assert r1["actions"][0]["action_type"] == "close"


def test_stop_processing_guard_survives():
    rules = [
        {"name": "guard", "subtype": "enter_market", "order_index": 0,
         "triggers": {"t0": {"event_type": "high_volatility"}},
         "actions": {"a0": {"action_type": "stop_processing"}},
         "continue_processing": False},
        _tier_rule("BUY", 1, 70, -5, -10),
    ]
    got = live_export_to_trade_rules(_payload(rules))
    # guard has no open action -> it routes as an ENTRY rule (subtype says so) and keeps its
    # stop_processing action instead of being skipped like the legacy importer did.
    kinds = [[a["action_type"] for a in r["actions"]] for r in got["entry_rules"]]
    assert kinds[0] == ["stop_processing"]
    assert kinds[1][0] == "buy"


def test_order_index_orders_rules():
    rules = [_tier_rule("B", 1, 80, -5, -10), _tier_rule("A", 0, 60, 0, -8)]
    got = live_export_to_trade_rules(_payload(rules))
    assert [r["name"] for r in got["entry_rules"]] == ["A", "B"]


def test_full_round_trip_is_lossless():
    tiers = [(-5, -12), (0, -8)]
    entry = [_tier_rule(f"BUY_T{i}", i, 60 + i * 20, tp, sl) for i, (tp, sl) in enumerate(tiers)]
    exits = [{"name": "ratchet", "subtype": "open_positions", "order_index": 0,
              "triggers": {"t0": {"event_type": "profit_loss_percent", "operator": ">", "value": 16}},
              "actions": {"a0": {"action_type": "adjust_stop_loss",
                                 "reference_value": "order_open_price", "value": 4}},
              "continue_processing": True}]
    src = {"export_type": "rulesets", "rulesets": [
        {"name": "e", "subtype": "enter_market", "rules": entry},
        {"name": "o", "subtype": "open_positions", "rules": exits},
    ]}
    rules1 = live_export_to_trade_rules(src)
    exported = trade_rules_to_live_export(rules1["entry_rules"], rules1["exit_rules"], name="s")
    rules2 = live_export_to_trade_rules(exported)

    def strip(rs):  # ids/names are synthesized; compare structure + semantics
        return [
            {
                "conds": sorted((l["field"], l.get("comparison"), l.get("value"))
                                for l in (r["conditions"]["conditions"] if r["conditions"] else [])),
                "actions": [(a["action_type"], a.get("reference_value"), a.get("action_value"))
                            for a in r["actions"]],
                "cont": r["continue_processing"],
            }
            for r in rs
        ]

    assert strip(rules1["entry_rules"]) == strip(rules2["entry_rules"])
    assert strip(rules1["exit_rules"]) == strip(rules2["exit_rules"])
    # continue_processing really landed in the export file itself
    op = next(rs for rs in exported["rulesets"] if rs["subtype"] == "open_positions")
    assert op["rules"][0]["continue_processing"] is True
