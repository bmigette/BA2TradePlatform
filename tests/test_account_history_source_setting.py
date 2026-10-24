"""The ``ohlcv_history_source`` ACCOUNT setting (plan 2026-10-24-history-source-task11, Task 7)."""
import pytest
from sqlmodel import Session

from ba2_common.core.ohlcv_history import UnknownHistorySource
from ba2_trade_platform.core import account_history
from ba2_trade_platform.core.account_history import HistorySourceNeedsAlpacaKeys
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AccountSetting
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount
from ba2_trade_platform.ui.pages.settings import account_settings_error
from tests.factories import (create_account_definition, create_expert_instance, create_trading_order,
                             create_transaction)

KEY = "ohlcv_history_source"


@pytest.mark.parametrize("cls", [AlpacaAccount, TastyTradeAccount, IBKRAccount])
def test_every_account_type_declares_the_setting(cls):
    d = cls.get_merged_settings_definitions()[KEY]
    assert d["default"] == "fmp" and d["type"] == "str" and d["required"] is False
    assert set(d["valid_values"]) == {"fmp", "alpaca"} and d["tooltip"]


def test_one_definition_for_all_types():
    defs = [c.get_merged_settings_definitions()[KEY] for c in (AlpacaAccount, TastyTradeAccount, IBKRAccount)]
    assert defs[0] == defs[1] == defs[2]


@pytest.mark.parametrize("settings", [{}, {KEY: None}, {KEY: "fmp"}])
def test_unset_resolves_to_fmp(settings):
    assert account_history.validate_account_history_source(settings) == "fmp"


def test_saving_unknown_value_is_refused():
    with pytest.raises(UnknownHistorySource):
        account_history.validate_account_history_source({KEY: "yahoo"})
    assert "yahoo" in account_settings_error({KEY: "yahoo"})


def test_alpaca_without_keys_refused_with_named_error():
    with pytest.raises(HistorySourceNeedsAlpacaKeys):
        account_history.validate_account_history_source({KEY: "alpaca"})               # TastyTrade / IBKR shape
    with pytest.raises(HistorySourceNeedsAlpacaKeys):
        account_history.validate_account_history_source({KEY: "alpaca", "api_key": "k", "api_secret": ""})
    assert "Alpaca" in account_settings_error({KEY: "alpaca", "client_secret": "x"})


def test_alpaca_with_keys_is_accepted_and_the_form_saves():
    s = {KEY: "alpaca", "api_key": "k", "api_secret": "s"}
    assert account_history.validate_account_history_source(s) == "alpaca"
    assert account_settings_error(s) is None
    assert account_settings_error({"data_feed": "fmp"}) is None        # a form without the key is not judged


def _set(account_id, key, value):
    with get_db() as session:
        session.add(AccountSetting(account_id=account_id, key=key, value_str=value, value_float=None))
        session.commit()


def test_existing_accounts_without_the_row_get_the_default_not_a_crash():
    acc = create_account_definition(name="A", provider="Alpaca")
    settings = account_history.account_settings(acc.id)
    assert settings[KEY] is None
    assert account_history.validate_account_history_source(settings) == "fmp"
    assert account_history.alpaca_history_accounts() == []


def test_stored_alpaca_account_is_listed_with_its_keys_and_others_are_not():
    a = create_account_definition(name="A", provider="Alpaca")
    b = create_account_definition(name="B", provider="Alpaca")
    t = create_account_definition(name="T", provider="TastyTrade")
    for acc in (a, b):
        _set(acc.id, "api_key", "k%d" % acc.id)
        _set(acc.id, "api_secret", "s%d" % acc.id)
    _set(a.id, KEY, "alpaca")
    _set(b.id, KEY, "fmp")
    _set(t.id, KEY, "alpaca")                                           # a stored illegal value: ERROR, left out
    assert account_history.alpaca_history_accounts() == [{"id": a.id, "api_key": "k%d" % a.id, "api_secret": "s%d" % a.id}]
