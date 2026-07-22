"""Tests for ExtendableSettingsInterface settings management."""
import pytest
from tests.conftest import MockExpert, MockAccount
from tests.factories import create_account_definition, create_expert_instance
from sqlmodel import select
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import ExpertSetting


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


class TestIntSetting:
    """Regression tests for 'int'-typed settings (SmartRiskManagerJob #27/#28).

    Settings declared with type "int" must round-trip as Python ints, since
    consumers (e.g. timedelta(hours=...)) require numeric types, not strings.
    """

    def test_default_value_is_int(self):
        acct_def = create_account_definition()
        expert_instance = create_expert_instance(account_id=acct_def.id, expert="MockExpert")
        expert = MockExpert(expert_instance.id)

        value = expert.get_setting_with_interface_default("test_int_setting")

        assert value == 24
        assert isinstance(value, int)

    def test_save_and_load_round_trip_is_int(self):
        acct_def = create_account_definition()
        expert_instance = create_expert_instance(account_id=acct_def.id, expert="MockExpert")
        expert = MockExpert(expert_instance.id)

        expert.save_setting("test_int_setting", 48, setting_type="int")
        expert._invalidate_settings_cache()

        value = expert.get_setting_with_interface_default("test_int_setting")

        assert value == 48
        assert isinstance(value, int)

    def test_legacy_string_storage_loads_as_int(self):
        """Settings saved before 'int' type handling existed are stored in
        value_str (e.g. "24"). They must still load as int, not str."""
        acct_def = create_account_definition()
        expert_instance = create_expert_instance(account_id=acct_def.id, expert="MockExpert")
        expert = MockExpert(expert_instance.id)

        with get_db() as session:
            session.add(ExpertSetting(
                instance_id=expert.id, key="test_int_setting", value_str="24"
            ))
            session.commit()

        value = expert.get_setting_with_interface_default("test_int_setting")

        assert value == 24
        assert isinstance(value, int)


class TestResetSettings:
    """Regression coverage for the live settings-import wipe (instance 6 ended up stuck on
    risk_manager_mode=smart because a prior import only wrote the keys ITS payload contained,
    silently leaving an older, unrelated setting in place)."""

    def test_reset_settings_deletes_all_rows(self):
        acct_def = create_account_definition()
        expert_instance = create_expert_instance(account_id=acct_def.id, expert="MockExpert")
        expert = MockExpert(expert_instance.id)
        expert.save_setting("test_setting", "stale_value")
        expert.save_setting("test_int_setting", 48, setting_type="int")

        expert.reset_settings()

        with get_db() as session:
            remaining = session.exec(select(ExpertSetting).filter_by(instance_id=expert.id)).all()
        assert remaining == []

    def test_reset_settings_then_apply_new_payload_has_no_leftover_keys(self):
        """Mirrors the live import flow: reset, then apply only the new payload's keys - a key
        the new payload never mentions must fall back to the class default, not a stale value."""
        acct_def = create_account_definition()
        expert_instance = create_expert_instance(account_id=acct_def.id, expert="MockExpert")
        expert = MockExpert(expert_instance.id)
        expert.save_setting("test_setting", "stale_value")

        expert.reset_settings()
        expert.save_setting("test_int_setting", 48, setting_type="int")
        expert._invalidate_settings_cache()

        assert expert.settings.get("test_setting") is None

    def test_reset_settings_on_instance_with_no_settings_is_a_noop(self):
        acct_def = create_account_definition()
        expert_instance = create_expert_instance(account_id=acct_def.id, expert="MockExpert")
        expert = MockExpert(expert_instance.id)

        expert.reset_settings()  # must not raise

        with get_db() as session:
            remaining = session.exec(select(ExpertSetting).filter_by(instance_id=expert.id)).all()
        assert remaining == []
