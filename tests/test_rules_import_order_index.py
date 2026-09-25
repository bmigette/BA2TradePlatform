"""Rule order survives export -> import (order_index IS precedence).

The live evaluator is FIRST-MATCH: it walks a ruleset's rules in
``RulesetEventActionLink.order_index`` order and stops after the first rule whose conditions
pass (unless that rule sets ``continue_processing``). So the link order decides WHICH RULE FIRES.

Every importer in ``ba2_common.core.rules_export_import`` used to write
``rule_data.get('order_index', 0)``: a payload without ``order_index`` (an old export, a
hand-written file) landed every rule of a ruleset at 0, and precedence became whatever SQLite
returned for the tie -- silently. The doors covered here:

* ``RulesImporter.import_ruleset``              (UI: import one ruleset)
* ``RulesImporter.import_multiple_rulesets``    (UI import; ``tools/import_deploy_payload.py``
  after ``trade_rules_to_live_export``)
* ``RulesImporter.import_rulesets_reusing_by_name`` (``expert_batch_export_import``)
"""
from __future__ import annotations

import pytest
from sqlmodel import select

from ba2_common.core.db import get_db, ruleset_event_actions
from ba2_common.core.models import RulesetEventActionLink
from ba2_common.core.rules_convert import trade_rules_to_live_export
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import EventAction, Ruleset
from ba2_trade_platform.core.rules_export_import import RulesExporter, RulesImporter
from ba2_trade_platform.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_trade_platform.core.types import (
    ExpertActionType, ExpertEventRuleType, ExpertEventType, OrderRecommendation,
)
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_expert_instance, create_recommendation,
    link_rule_to_ruleset,
)

RULE_TYPE = ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value
BULLISH = {"t0": {"event_type": ExpertEventType.F_BULLISH.value}}


def _rule(name, action=ExpertActionType.BUY.value, order_index=None, triggers=None):
    r = {"name": name, "type": RULE_TYPE, "subtype": "enter_market",
         "triggers": triggers if triggers is not None else dict(BULLISH),
         "actions": {"a0": {"action_type": action}},
         "extra_parameters": {}, "continue_processing": False}
    if order_index is not None:
        r["order_index"] = order_index
    return r


def _ruleset(name, rules):
    return {"name": name, "description": None, "type": RULE_TYPE,
            "subtype": "enter_market", "rules": rules}


def _links(ruleset_id):
    """{rule name: order_index} for a ruleset, read through its own session."""
    with get_db() as s:
        rows = s.exec(
            select(RulesetEventActionLink, EventAction)
            .join(EventAction, RulesetEventActionLink.eventaction_id == EventAction.id)
            .where(RulesetEventActionLink.ruleset_id == ruleset_id)).all()
        return {ea.name: link.order_index for link, ea in rows}


def _live_order(ruleset_id):
    """Rule names in the order the LIVE engine walks them."""
    return [ea.name for ea in ruleset_event_actions(ruleset_id)]


def _seed_three_rule_ruleset():
    """3 rules whose precedence is NOT their id order: ids r_a < r_b < r_c, precedence c, a, b,
    with non-contiguous indices so an importer that renumbers would be caught too."""
    rs_id = add_instance(Ruleset(name="Order RT Source", type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE))
    ids = {}
    for name in ("r_a", "r_b", "r_c"):
        ids[name] = add_instance(EventAction(
            name=name, type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
            triggers=dict(BULLISH), actions={"a0": {"action_type": ExpertActionType.BUY.value}},
            continue_processing=False))
    link_rule_to_ruleset(rs_id, ids["r_c"], order_index=3)
    link_rule_to_ruleset(rs_id, ids["r_a"], order_index=7)
    link_rule_to_ruleset(rs_id, ids["r_b"], order_index=12)
    return rs_id


# ----------------------------------------------------------------------------- round trip

class TestRoundTrip:
    def test_export_carries_order_index_in_precedence_order(self):
        rs_id = _seed_three_rule_ruleset()
        rules = RulesExporter.export_ruleset(rs_id)["ruleset"]["rules"]
        assert [(r["name"], r["order_index"]) for r in rules] == \
               [("r_c", 3), ("r_a", 7), ("r_b", 12)]

    def test_import_ruleset_restores_order_index_exactly(self):
        rs_id = _seed_three_rule_ruleset()
        exported = RulesExporter.export_ruleset(rs_id)
        new_id, _ = RulesImporter.import_ruleset(exported)
        assert new_id != rs_id
        assert _links(new_id) == {"r_c": 3, "r_a": 7, "r_b": 12}
        assert _live_order(new_id) == ["r_c", "r_a", "r_b"]

    def test_import_multiple_rulesets_restores_order_index_exactly(self):
        rs_id = _seed_three_rule_ruleset()
        exported = RulesExporter.export_multiple_rulesets([rs_id])
        (new_id,), _ = RulesImporter.import_multiple_rulesets(exported)
        assert _links(new_id) == {"r_c": 3, "r_a": 7, "r_b": 12}
        assert _live_order(new_id) == ["r_c", "r_a", "r_b"]

    def test_import_reusing_by_name_restores_order_index_exactly(self):
        """The expert batch import door (replace-in-place)."""
        rs_id = _seed_three_rule_ruleset()
        exported = RulesExporter.export_multiple_rulesets([rs_id])
        (same_id,), _ = RulesImporter.import_rulesets_reusing_by_name(exported)
        assert same_id == rs_id
        assert _links(same_id) == {"r_c": 3, "r_a": 7, "r_b": 12}
        assert _live_order(same_id) == ["r_c", "r_a", "r_b"]


# --------------------------------------------------------------------- old export (no index)

class TestExportWithoutOrderIndex:
    @pytest.mark.parametrize("door", ["import_ruleset", "import_multiple_rulesets",
                                      "import_rulesets_reusing_by_name"])
    def test_file_order_becomes_0_to_n_minus_1(self, door):
        rules = [_rule("old_first"), _rule("old_second"), _rule("old_third")]
        rs = _ruleset(f"Old Export {door}", rules)
        if door == "import_ruleset":
            new_id, warnings = RulesImporter.import_ruleset({"ruleset": rs})
        else:
            (new_id,), warnings = getattr(RulesImporter, door)({"rulesets": [rs]})
        assert _links(new_id) == {"old_first": 0, "old_second": 1, "old_third": 2}
        assert _live_order(new_id) == ["old_first", "old_second", "old_third"]
        assert any("order_index" in w and "file order" in w for w in warnings), warnings

    def test_tied_indices_are_broken_by_file_order(self):
        """An export of a ruleset that an earlier import had already collapsed (every rule at 0):
        the file lists them in the order the source DB returned, which is the only order there
        is -- keep it instead of reproducing the tie."""
        rules = [_rule("tie_first", order_index=0), _rule("tie_second", order_index=0),
                 _rule("tie_third", order_index=0)]
        new_id, warnings = RulesImporter.import_ruleset({"ruleset": _ruleset("Tied", rules)})
        assert _links(new_id) == {"tie_first": 0, "tie_second": 1, "tie_third": 2}
        assert any("tie" in w.lower() for w in warnings), warnings

    def test_mixed_presence_puts_indexed_rules_first_then_file_order(self):
        """Same rule ``live_export_to_trade_rules`` applies on the backtest side."""
        rules = [_rule("mix_unindexed_a"), _rule("mix_idx_5", order_index=5),
                 _rule("mix_idx_2", order_index=2), _rule("mix_unindexed_b")]
        new_id, warnings = RulesImporter.import_ruleset({"ruleset": _ruleset("Mixed", rules)})
        assert _live_order(new_id) == ["mix_idx_2", "mix_idx_5", "mix_unindexed_a",
                                       "mix_unindexed_b"]
        assert sorted(_links(new_id).values()) == [0, 1, 2, 3]
        assert warnings

    def test_non_integer_order_index_is_refused(self):
        rules = [_rule("bad_idx", order_index="first")]
        with pytest.raises(ValueError, match="order_index"):
            RulesImporter.import_ruleset({"ruleset": _ruleset("BadIdx", rules)})


# ---------------------------------------------------------------------------- deploy path

class TestDeployPayloadPath:
    """``tools/import_deploy_payload.py`` = ``trade_rules_to_live_export`` then
    ``RulesImporter.import_multiple_rulesets`` (the tool configures the LIVE DB at import time,
    so its pipeline is reproduced here, as the other deploy-path tests do)."""

    def test_trade_rule_list_order_is_the_live_precedence(self):
        entry_rules = [
            {"id": f"e{i}", "name": f"deploy_{n}", "conditions": {
                "id": "g", "operator": "AND", "conditions": [
                    {"id": "l", "field": "confidence", "operator": ">=", "value": v}]},
             "actions": [{"action_type": ExpertActionType.BUY.value}],
             "continue_processing": False}
            for i, (n, v) in enumerate((("hi", 80.0), ("mid", 60.0), ("lo", 40.0)))
        ]
        live_export = trade_rules_to_live_export(entry_rules, [], name="OrderDeploy")
        (enter_id,), _ = RulesImporter.import_multiple_rulesets(live_export)
        assert _links(enter_id) == {"deploy_hi": 0, "deploy_mid": 1, "deploy_lo": 2}
        assert _live_order(enter_id) == ["deploy_hi", "deploy_mid", "deploy_lo"]


# ------------------------------------------------------------------------- live evaluation

class TestFirstMatchWinsAfterImport:
    def _evaluate(self, ruleset_id):
        acct_def = create_account_definition()
        ei = create_expert_instance(account_id=acct_def.id)
        rec = create_recommendation(instance_id=ei.id, recommended_action=OrderRecommendation.BUY)
        evaluator = TradeActionEvaluator(account=MockAccount(acct_def.id))
        evaluator.evaluate("AAPL", rec, ruleset_id)
        return [r["rule_name"] for r in evaluator.rule_evaluations]

    def test_old_export_first_rule_in_file_wins(self):
        """Both rules match every bullish recommendation, so only precedence decides. The
        file's SECOND rule already exists in the DB (an earlier import), so it is reused and has
        the LOWER id -- with every link tied at 0 it is the one SQLite hands back first."""
        second = _rule("fm_second", action=ExpertActionType.SELL.value)
        add_instance(EventAction(
            name="fm_second", type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
            subtype="enter_market", triggers=dict(BULLISH),
            actions={"a0": {"action_type": ExpertActionType.SELL.value}},
            extra_parameters={}, continue_processing=False))
        first = _rule("fm_first", action=ExpertActionType.BUY.value)
        new_id, _ = RulesImporter.import_ruleset(
            {"ruleset": _ruleset("First Match", [first, second])})
        assert self._evaluate(new_id) == ["fm_first"]

    def test_round_tripped_ruleset_fires_the_same_rule(self):
        rs_id = _seed_three_rule_ruleset()
        new_id, _ = RulesImporter.import_ruleset(RulesExporter.export_ruleset(rs_id))
        assert self._evaluate(rs_id) == ["r_c"]
        assert self._evaluate(new_id) == ["r_c"]
