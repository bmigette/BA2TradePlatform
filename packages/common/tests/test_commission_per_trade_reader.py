"""TradeRiskManagement._commission_per_trade must not provoke an ERROR log on accounts
whose interface does not define ``commission_per_trade``.

Measured on prod 2026-09-09: every classic-RM sizing pass on the Alpaca account logged
``Error getting interface default for setting 'commission_per_trade': Setting
'commission_per_trade' not found in AlpacaAccount interface definitions`` twice per
candidate, then correctly used 0.0. The reader asked
``get_setting_with_interface_default`` for a key the interface never declared, and that
reader raises AND logs at ERROR for an undeclared key by design. The fix asks the
interface whether it declares the key BEFORE reading it.

``ba2_common.logger`` has ``propagate=False`` so caplog sees nothing; the ERROR is
asserted by monkeypatching the ExtendableSettingsInterface module logger.
"""
from __future__ import annotations

import importlib

import pytest

from ba2_common.core.interfaces.ExtendableSettingsInterface import ExtendableSettingsInterface

# The package __init__ re-exports the CLASS under the module's name, so reach the MODULE
# (whose ``logger`` the ERROR is emitted on) through importlib.
esi_module = importlib.import_module("ba2_common.core.interfaces.ExtendableSettingsInterface")
from ba2_common.core.TradeRiskManagement import TradeRiskManagement


class _LiveShapedAccount(ExtendableSettingsInterface):
    """An account whose interface, like AlpacaAccount's, declares no commission key."""

    _builtin_settings = {}

    def __init__(self, settings=None):
        self.id = 1
        self._settings = dict(settings or {})

    @property
    def settings(self):
        return self._settings

    @classmethod
    def get_settings_definitions(cls):
        return {"api_key": {"type": "str", "required": True, "description": "key"}}


class _CommissionedAccount(_LiveShapedAccount):
    """An account that declares the key (the backtest account's shape)."""

    @classmethod
    def get_settings_definitions(cls):
        return {"commission_per_trade": {"type": "float", "required": False, "default": None,
                                         "description": "flat $ per fill"}}


class _CfgOnlyAccount:
    """No settings interface at all, only the backtest-style ``_cfg`` dict."""

    id = 3

    def __init__(self, cfg):
        self._cfg = cfg


@pytest.fixture
def error_log(monkeypatch):
    records = []
    monkeypatch.setattr(esi_module.logger, "error", lambda msg, *a, **k: records.append(str(msg)))
    return records


def test_undeclared_key_reads_as_no_commission_without_an_error_log(error_log):
    rm = TradeRiskManagement()
    assert rm._commission_per_trade(_LiveShapedAccount()) == 0.0
    assert error_log == [], error_log


def test_declared_and_configured_key_is_read():
    rm = TradeRiskManagement()
    assert rm._commission_per_trade(_CommissionedAccount({"commission_per_trade": 1.5})) == 1.5


def test_declared_but_unset_key_reads_as_no_commission(error_log):
    rm = TradeRiskManagement()
    assert rm._commission_per_trade(_CommissionedAccount()) == 0.0
    assert error_log == []


def test_cfg_only_account_reads_its_cfg_value(error_log):
    rm = TradeRiskManagement()
    assert rm._commission_per_trade(_CfgOnlyAccount({"commission_per_trade": 2.0})) == 2.0
    assert rm._commission_per_trade(_CfgOnlyAccount({})) == 0.0
    assert error_log == []
