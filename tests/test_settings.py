"""Tests for ExtendableSettingsInterface settings management."""
import pytest
from tests.conftest import MockExpert, MockAccount
from tests.factories import create_account_definition, create_expert_instance


class TestSettingsDefinitions:
    def test_mock_expert_has_settings(self):
        defs = MockExpert.get_settings_definitions()
        assert "test_setting" in defs
        assert defs["test_setting"]["type"] == "str"

    def test_merged_settings_include_builtins(self):
        merged = MockExpert.get_merged_settings_definitions()
        assert "enable_buy" in merged
        assert "enable_sell" in merged
        assert "test_setting" in merged


class TestDetermineValueType:
    def _get_interface(self):
        acct_def = create_account_definition()
        return MockAccount(acct_def.id)

    def test_bool_detection(self):
        iface = self._get_interface()
        assert iface._determine_value_type(True) == "bool"
        assert iface._determine_value_type(False) == "bool"

    def test_float_detection(self):
        iface = self._get_interface()
        assert iface._determine_value_type(3.14) == "float"
        assert iface._determine_value_type(42) == "float"

    def test_json_detection(self):
        iface = self._get_interface()
        assert iface._determine_value_type({"key": "val"}) == "json"
        assert iface._determine_value_type([1, 2, 3]) == "json"

    def test_str_detection(self):
        iface = self._get_interface()
        assert iface._determine_value_type("hello") == "str"
