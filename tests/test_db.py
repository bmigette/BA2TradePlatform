"""Tests for ba2_trade_platform.core.db CRUD operations and helpers."""
import pytest
from ba2_trade_platform.core.db import (
    add_instance, get_instance, update_instance, delete_instance,
    get_all_instances, get_setting, reorder_ruleset_rules,
    move_rule_up, move_rule_down,
)
from ba2_trade_platform.core.models import (
    AccountDefinition, AppSetting, Ruleset, EventAction,
    RulesetEventActionLink,
)
from ba2_trade_platform.core.types import ExpertEventRuleType
from tests.factories import link_rule_to_ruleset


class TestAddInstance:
    def test_returns_positive_id(self):
        acct = AccountDefinition(name="Test", provider="Mock", description="Test")
        result_id = add_instance(acct)
        assert isinstance(result_id, int)
        assert result_id > 0

    def test_instance_retrievable_after_add(self):
        acct = AccountDefinition(name="Retrievable", provider="Mock", description="d")
        acct_id = add_instance(acct)
        fetched = get_instance(AccountDefinition, acct_id)
        assert fetched.name == "Retrievable"


class TestGetInstance:
    def test_not_found_raises(self):
        with pytest.raises(Exception, match="not found"):
            get_instance(AccountDefinition, 99999)


class TestUpdateInstance:
    def test_update_persists_changes(self):
        acct = AccountDefinition(name="Before", provider="Mock", description="d")
        acct_id = add_instance(acct)
        fetched = get_instance(AccountDefinition, acct_id)
        fetched.name = "After"
        update_instance(fetched)
        refetched = get_instance(AccountDefinition, acct_id)
        assert refetched.name == "After"


class TestDeleteInstance:
    def test_delete_removes_instance(self):
        acct = AccountDefinition(name="ToDelete", provider="Mock", description="d")
        acct_id = add_instance(acct)
        fetched = get_instance(AccountDefinition, acct_id)
        result = delete_instance(fetched)
        assert result is True
        with pytest.raises(Exception, match="not found"):
            get_instance(AccountDefinition, acct_id)


class TestGetAllInstances:
    def test_returns_list(self):
        before = get_all_instances(AccountDefinition)
        add_instance(AccountDefinition(name="A1", provider="M", description="d"))
        add_instance(AccountDefinition(name="A2", provider="M", description="d"))
        after = get_all_instances(AccountDefinition)
        assert len(after) >= len(before) + 2


class TestGetSetting:
    def test_setting_found(self):
        setting = AppSetting(key="test_key_found", value_str="hello")
        add_instance(setting)
        result = get_setting("test_key_found")
        assert result == "hello"

    def test_setting_not_found(self):
        result = get_setting("nonexistent_key_xyz_12345")
        assert result is None


class TestRuleOrdering:
    def _setup_ruleset_with_rules(self, n=3):
        """Create a ruleset with n linked rules, return (ruleset_id, [eventaction_ids])."""
        ruleset = Ruleset(
            name="Order Test",
            type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        )
        rs_id = add_instance(ruleset)

        ea_ids = []
        for i in range(n):
            ea = EventAction(
                name=f"Rule {i}",
                type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
                triggers={}, actions={},
            )
            ea_id = add_instance(ea)
            ea_ids.append(ea_id)
            link_rule_to_ruleset(rs_id, ea_id, order_index=i)

        return rs_id, ea_ids

    def test_reorder_ruleset_rules(self):
        rs_id, ea_ids = self._setup_ruleset_with_rules(3)
        reversed_ids = list(reversed(ea_ids))
        result = reorder_ruleset_rules(rs_id, reversed_ids)
        assert result is True

    def test_move_rule_up(self):
        rs_id, ea_ids = self._setup_ruleset_with_rules(3)
        result = move_rule_up(rs_id, ea_ids[1])
        assert result is True

    def test_move_rule_up_at_top_returns_false(self):
        rs_id, ea_ids = self._setup_ruleset_with_rules(3)
        result = move_rule_up(rs_id, ea_ids[0])
        assert result is False

    def test_move_rule_down(self):
        rs_id, ea_ids = self._setup_ruleset_with_rules(3)
        result = move_rule_down(rs_id, ea_ids[1])
        assert result is True

    def test_move_rule_down_at_bottom_returns_false(self):
        rs_id, ea_ids = self._setup_ruleset_with_rules(3)
        result = move_rule_down(rs_id, ea_ids[2])
        assert result is False
