"""margin_enabled / margin_factor: the per-account leverage switch and ceiling.

Declared ONCE on ReadOnlyAccountInterface so every broker inherits them and the
generic account settings dialog renders/saves them with no UI code (same pattern
as manual_trading_enabled). Read through get_setting_with_interface_default,
never settings.get(key, default) -- see test_manual_trading_setting.py.
"""
import pytest

from ba2_common.core.interfaces.ReadOnlyAccountInterface import (
    ReadOnlyAccountInterface, MARGIN_FACTOR_MIN, margin_factor_error,
)
from .test_manual_trading_setting import StubAccount


def _defs():
    return ReadOnlyAccountInterface.get_merged_settings_definitions()


def test_margin_enabled_is_declared_as_bool_defaulting_false():
    d = _defs()["margin_enabled"]
    assert d["type"] == "bool" and d["default"] is False and d["required"] is False


def test_margin_factor_is_declared_as_float_defaulting_1_8():
    d = _defs()["margin_factor"]
    assert d["type"] == "float" and d["default"] == 1.8 and d["required"] is False


def test_both_carry_a_tooltip():
    for key in ("margin_enabled", "margin_factor"):
        assert _defs()[key]["tooltip"]


@pytest.mark.parametrize(
    "bad", [0.0, 0.99, -1.0, None, "abc", float("nan"), "nan", float("inf"), True])
def test_margin_factor_error_names_the_problem(bad):
    msg = margin_factor_error(bad)
    assert msg and "margin_factor" in msg


@pytest.mark.parametrize("ok", [1.0, 1.8, 4.0, "2"])
def test_margin_factor_error_is_none_for_a_valid_factor(ok):
    assert margin_factor_error(ok) is None


def test_min_is_one():
    assert MARGIN_FACTOR_MIN == 1.0


def test_unset_margin_enabled_reads_as_false_through_the_interface_default():
    acct = StubAccount({"margin_enabled": None, "margin_factor": None})
    assert acct.get_setting_with_interface_default("margin_enabled", log_warning=False) is False


def test_unset_margin_factor_reads_as_1_8_through_the_interface_default():
    acct = StubAccount({"margin_enabled": None, "margin_factor": None})
    assert acct.get_setting_with_interface_default("margin_factor", log_warning=False) == 1.8
