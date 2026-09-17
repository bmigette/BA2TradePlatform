"""Tests for RulesExporter and RulesImporter."""
import pytest
from ba2_trade_platform.core.rules_export_import import RulesExporter, RulesImporter
from ba2_trade_platform.core.models import Ruleset, EventAction
from ba2_trade_platform.core.types import ExpertEventRuleType, ExpertEventType, ExpertActionType
from ba2_trade_platform.core.db import add_instance, get_instance
from tests.factories import link_rule_to_ruleset


def _create_ruleset_with_rules():
    """Helper: create a ruleset with 2 linked rules."""
    rs = Ruleset(
        name="Export Test Ruleset",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        description="For testing export",
    )
    rs_id = add_instance(rs)

    ea1 = EventAction(
        name="Export Rule 1",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"trigger_0": {"event_type": ExpertEventType.F_BULLISH.value}},
        actions={"action_0": {"action_type": ExpertActionType.BUY.value}},
        continue_processing=True,
    )
    ea1_id = add_instance(ea1)
    link_rule_to_ruleset(rs_id, ea1_id, order_index=0)

    ea2 = EventAction(
        name="Export Rule 2",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"trigger_0": {"event_type": ExpertEventType.F_BEARISH.value}},
        actions={"action_0": {"action_type": ExpertActionType.SELL.value}},
        continue_processing=False,
    )
    ea2_id = add_instance(ea2)
    link_rule_to_ruleset(rs_id, ea2_id, order_index=1)

    return rs_id, [ea1_id, ea2_id]


class TestRulesExporter:
    def test_export_ruleset(self):
        rs_id, _ = _create_ruleset_with_rules()
        data = RulesExporter.export_ruleset(rs_id)
        assert data["export_version"] == "1.0"
        assert data["export_type"] == "ruleset"
        assert data["ruleset"]["name"] == "Export Test Ruleset"
        assert len(data["ruleset"]["rules"]) == 2

    def test_export_rule(self):
        ea = EventAction(
            name="Single Export Rule",
            type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
            triggers={"trigger_0": {"event_type": ExpertEventType.F_BULLISH.value}},
            actions={"action_0": {"action_type": ExpertActionType.BUY.value}},
        )
        ea_id = add_instance(ea)
        data = RulesExporter.export_rule(ea_id)
        assert data["export_type"] == "rule"
        assert data["rule"]["name"] == "Single Export Rule"

    def test_export_nonexistent_ruleset_raises(self):
        with pytest.raises(Exception):
            RulesExporter.export_ruleset(99999)

    def test_export_multiple_rulesets(self):
        rs_id1, _ = _create_ruleset_with_rules()
        # Create a second ruleset
        rs2 = Ruleset(
            name="Export Multi Test 2",
            type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        )
        rs_id2 = add_instance(rs2)
        data = RulesExporter.export_multiple_rulesets([rs_id1, rs_id2])
        assert data["export_type"] == "rulesets"
        assert len(data["rulesets"]) == 2

    def test_exported_rules_preserve_order(self):
        rs_id, _ = _create_ruleset_with_rules()
        data = RulesExporter.export_ruleset(rs_id)
        rules = data["ruleset"]["rules"]
        assert rules[0]["order_index"] == 0
        assert rules[1]["order_index"] == 1


class TestRulesImporter:
    def test_import_rule(self):
        rule_data = {
            "rule": {
                "name": "Imported Rule",
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "triggers": {"trigger_0": {"event_type": ExpertEventType.F_BULLISH.value}},
                "actions": {"action_0": {"action_type": ExpertActionType.BUY.value}},
                "continue_processing": False,
            }
        }
        rule_id, warnings = RulesImporter.import_rule(rule_data)
        assert rule_id > 0
        fetched = get_instance(EventAction, rule_id)
        assert fetched.name == "Imported Rule"

    def test_import_duplicate_rule_reuses_existing(self):
        rule_data = {
            "rule": {
                "name": "Reuse Test Rule",
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "triggers": {},
                "actions": {},
                "continue_processing": False,
            }
        }
        id1, _ = RulesImporter.import_rule(rule_data)
        id2, warnings = RulesImporter.import_rule(rule_data)
        assert id1 == id2
        assert any("already exists" in w for w in warnings)

    def test_import_ruleset(self):
        ruleset_data = {
            "ruleset": {
                "name": "Imported Ruleset",
                "description": "Test import",
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "subtype": None,
                "rules": [
                    {
                        "name": "Imported RS Rule 1",
                        "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                        "triggers": {"trigger_0": {"event_type": ExpertEventType.F_BULLISH.value}},
                        "actions": {"action_0": {"action_type": ExpertActionType.BUY.value}},
                        "continue_processing": False,
                        "order_index": 0,
                    }
                ],
            }
        }
        rs_id, warnings = RulesImporter.import_ruleset(ruleset_data)
        assert rs_id > 0
        fetched = get_instance(Ruleset, rs_id)
        assert fetched.name == "Imported Ruleset"

    def test_import_ruleset_duplicate_name_gets_suffixed(self):
        ruleset_data = {
            "ruleset": {
                "name": "Dup Name Ruleset",
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "rules": [],
            }
        }
        id1, _ = RulesImporter.import_ruleset(ruleset_data)
        id2, warnings = RulesImporter.import_ruleset(ruleset_data)
        assert id1 != id2
        fetched2 = get_instance(Ruleset, id2)
        assert fetched2.name == "Dup Name Ruleset-1"

    def test_export_import_roundtrip(self):
        rs_id, _ = _create_ruleset_with_rules()
        exported = RulesExporter.export_ruleset(rs_id)
        imported_id, _ = RulesImporter.import_ruleset(exported)
        re_exported = RulesExporter.export_ruleset(imported_id)
        assert len(re_exported["ruleset"]["rules"]) == len(exported["ruleset"]["rules"])


class TestImportRulesetsReusingByName:
    """The batch-import path: a name match MEANS "that ruleset", so re-importing the same
    strategy updates it in place instead of leaving a trail of ``foo-1``, ``foo-2`` copies."""

    def _payload(self, name, rule_names):
        return {"rulesets": [{
            "name": name,
            "description": "batch",
            "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
            "subtype": None,
            "rules": [{
                "name": rn,
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "triggers": {"t0": {"event_type": ExpertEventType.F_BULLISH.value}},
                "actions": {"a0": {"action_type": ExpertActionType.BUY.value}},
                "continue_processing": False,
                "order_index": i,
            } for i, rn in enumerate(rule_names)],
        }]}

    def test_a_new_name_creates_the_ruleset(self):
        ids, warnings = RulesImporter.import_rulesets_reusing_by_name(
            self._payload("ReuseByName Fresh", ["r1", "r2"]))
        assert len(ids) == 1
        assert get_instance(Ruleset, ids[0]).name == "ReuseByName Fresh"
        assert RulesExporter.export_ruleset(ids[0])["ruleset"]["rules"].__len__() == 2

    def test_the_same_name_reuses_the_row_instead_of_suffixing_it(self):
        """THE POINT: the expert's ruleset id does not move, so every other expert pointing at
        it follows the update -- and no 'foo-1' is left behind."""
        ids1, _ = RulesImporter.import_rulesets_reusing_by_name(
            self._payload("ReuseByName Same", ["r1"]))
        ids2, warnings = RulesImporter.import_rulesets_reusing_by_name(
            self._payload("ReuseByName Same", ["r1"]))
        assert ids1 == ids2, "a second import must not create a second ruleset"
        assert get_instance(Ruleset, ids2[0]).name == "ReuseByName Same"
        assert any("reused it" in w for w in warnings)
        # and import_multiple_rulesets, the sibling, still does the opposite
        other, _ = RulesImporter.import_multiple_rulesets(self._payload("ReuseByName Same", ["r1"]))
        assert other[0] != ids1[0]
        assert get_instance(Ruleset, other[0]).name == "ReuseByName Same-1"

    def test_the_rule_list_is_replaced_not_merged(self):
        """A rule the export dropped must not survive in the reused ruleset."""
        ids1, _ = RulesImporter.import_rulesets_reusing_by_name(
            self._payload("ReuseByName Replace", ["keep", "drop"]))
        assert len(RulesExporter.export_ruleset(ids1[0])["ruleset"]["rules"]) == 2

        ids2, _ = RulesImporter.import_rulesets_reusing_by_name(
            self._payload("ReuseByName Replace", ["keep"]))
        names = [r["name"] for r in RulesExporter.export_ruleset(ids2[0])["ruleset"]["rules"]]
        assert names == ["keep"], f"expected the dropped rule to be gone, got {names}"

    @staticmethod
    def _linked_rule_ids(ruleset_id):
        """Rule ids linked to a ruleset, read in its own session (the relationship lazy-loads,
        so touching it on a detached instance raises)."""
        from ba2_common.core.db import get_db
        from ba2_common.core.models import RulesetEventActionLink
        from sqlmodel import select as _select
        with get_db() as s:
            return [l.eventaction_id for l in s.exec(
                _select(RulesetEventActionLink)
                .where(RulesetEventActionLink.ruleset_id == ruleset_id)
                .order_by(RulesetEventActionLink.order_index)).all()]

    def test_a_replaced_rule_row_survives_for_other_rulesets(self):
        """Only the LINK is removed -- the EventAction may be shared with a ruleset this import
        was never asked to touch, and deleting it would silently edit that one too."""
        shared, _ = RulesImporter.import_rulesets_reusing_by_name(
            self._payload("ReuseByName Bystander", ["shared_rule"]))
        bystander_rule_id = self._linked_rule_ids(shared[0])[0]

        owner, _ = RulesImporter.import_rulesets_reusing_by_name(
            self._payload("ReuseByName Owner", ["shared_rule"]))
        assert self._linked_rule_ids(owner[0]) == [bystander_rule_id], "the rule is shared"

        # Drop it from the owner; the bystander ruleset must still have it.
        RulesImporter.import_rulesets_reusing_by_name(
            {"rulesets": [{"name": "ReuseByName Owner", "type":
                           ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value, "rules": []}]})
        assert self._linked_rule_ids(owner[0]) == []
        assert get_instance(EventAction, bystander_rule_id) is not None
        assert self._linked_rule_ids(shared[0]) == [bystander_rule_id]

    def test_ids_come_back_in_the_input_order(self):
        """The caller maps them onto enter_market / open_positions by position."""
        payload = {"rulesets": [
            self._payload("ReuseByName OrderA", ["a"])["rulesets"][0],
            self._payload("ReuseByName OrderB", ["b"])["rulesets"][0],
        ]}
        ids, _ = RulesImporter.import_rulesets_reusing_by_name(payload)
        assert [get_instance(Ruleset, i).name for i in ids] == \
               ["ReuseByName OrderA", "ReuseByName OrderB"]


class TestContentAwareDedup:
    """Same name + identical content -> reuse; same name + DIFFERENT content -> import as a new
    variant (so the imported rule isn't silently dropped onto the pre-existing one)."""

    def _rule(self, name, event_type):
        return {"rule": {
            "name": name,
            "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
            "triggers": {"t0": {"event_type": event_type}},
            "actions": {"a0": {"action_type": ExpertActionType.BUY.value}},
            "continue_processing": False,
        }}

    def test_same_name_identical_content_reused(self):
        rd = self._rule("DedupSame", ExpertEventType.F_BULLISH.value)
        id1, _ = RulesImporter.import_rule(rd)
        id2, warnings = RulesImporter.import_rule(rd)
        assert id1 == id2
        assert any("identical content" in w for w in warnings)

    def test_same_name_different_content_creates_variant(self):
        id1, _ = RulesImporter.import_rule(self._rule("DedupDiff", ExpertEventType.F_BULLISH.value))
        id2, warnings = RulesImporter.import_rule(self._rule("DedupDiff", ExpertEventType.F_BEARISH.value))
        assert id1 != id2
        assert get_instance(EventAction, id2).name == "DedupDiff-1"
        assert any("DIFFERENT content" in w for w in warnings)
        # Re-importing the SAME different-content rule reuses the -1 variant (idempotent).
        id3, _ = RulesImporter.import_rule(self._rule("DedupDiff", ExpertEventType.F_BEARISH.value))
        assert id3 == id2


class TestNameGeneration:
    """Readable auto-names are a FALLBACK for unnamed/generic rules; real names are preserved."""

    def test_generate_numeric_token(self):
        from ba2_common.core.rules_export_import import generate_rule_name
        assert generate_rule_name({"c0": {"event_type": "days_opened", "operator": ">", "value": 10}}) == "openD_gt_10"

    def test_generate_multi_and_cap(self):
        from ba2_common.core.rules_export_import import generate_rule_name
        n = generate_rule_name({f"c{i}": {"event_type": "confidence", "operator": ">=", "value": 70 + i} for i in range(8)})
        assert len(n) <= 40 and "more" in n

    def test_is_generic_name(self):
        from ba2_common.core.rules_export_import import _is_generic_rule_name
        assert _is_generic_rule_name("cond_0") and _is_generic_rule_name("") and _is_generic_rule_name("Rule 5")
        assert not _is_generic_rule_name("BUY_Longterm_70pctConfidence_10pctProfit")

    def test_export_unnamed_rule_gets_generated_name(self):
        ea = EventAction(
            name="cond_0",
            type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
            triggers={"c0": {"event_type": ExpertEventType.N_DAYS_OPENED.value, "operator": ">", "value": 10}},
            actions={"a0": {"action_type": ExpertActionType.BUY.value}},
        )
        eid = add_instance(ea)
        data = RulesExporter.export_rule(eid)
        assert data["rule"]["name"] == "openD_gt_10"

    def test_export_named_rule_preserved(self):
        ea = EventAction(
            name="BUY_Longterm_70pctConfidence",
            type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
            triggers={"c0": {"event_type": ExpertEventType.F_BULLISH.value}},
            actions={"a0": {"action_type": ExpertActionType.BUY.value}},
        )
        eid = add_instance(ea)
        assert RulesExporter.export_rule(eid)["rule"]["name"] == "BUY_Longterm_70pctConfidence"
