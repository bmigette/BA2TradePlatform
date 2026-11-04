"""Adding an account needs a stored FMP API key; nothing else is gated.

The platform depends on FMP (screeners, price feed, market regime), so an account added on an
instance without the key cannot trade correctly. ``require_fmp_key_for_account_creation`` is the
single rule; the Settings UI calls it in ``save_account`` (new account) and ``show_dialog`` (new
account). Saving app settings with an empty FMP key, and editing an existing account, stay allowed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlmodel import select

import ba2_trade_platform.ui.pages.settings as settings_page
from ba2_trade_platform.core.account_creation_guard import (
    FmpKeyRequired, fmp_key_is_configured, require_fmp_key_for_account_creation)
from ba2_trade_platform.core.db import add_instance, get_db
from ba2_trade_platform.core.models import AccountDefinition, AppSetting
from tests.test_settings_dialog_missing_defaults import (   # noqa: F401  (fixtures + helpers)
    _account_tab, _app_tab, _stored_alpaca_account, _account_edit_form, fake_ui)


def _set_fmp(value):
    add_instance(AppSetting(key="FMP_API_KEY", value_str=value))


def _accounts():
    with get_db() as s:
        return s.exec(select(AccountDefinition)).all()


def _new_account_form():
    t = _account_tab()
    t.dialog = SimpleNamespace(close=lambda: None, clear=lambda: None, open=lambda: None)
    t._update_table_rows = lambda: None
    t._render_dynamic_settings("Alpaca", None)
    t.type_select = SimpleNamespace(value="Alpaca")
    t.name_input = SimpleNamespace(value="new-acc")
    t.desc_input = SimpleNamespace(value="")
    t.settings_inputs["api_key"].value = "k"
    t.settings_inputs["api_secret"].value = "s"
    t.settings_inputs["paper_account"].value = True
    return t


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    # Creating the real AlpacaAccount would try to connect.
    monkeypatch.setattr(settings_page.providers["Alpaca"], "__init__", lambda self, id: None)


# --------------------------------------------------------------------------- the rule
def test_no_key_row_is_refused():
    assert not fmp_key_is_configured()
    with pytest.raises(FmpKeyRequired) as e:
        require_fmp_key_for_account_creation()
    assert "FMP API key" in e.value.message


@pytest.mark.parametrize("value", ["", "   ", "\t\n", None])
def test_blank_key_is_refused(value):
    _set_fmp(value)
    with pytest.raises(FmpKeyRequired):
        require_fmp_key_for_account_creation()


def test_a_key_passes():
    _set_fmp("abc123")
    require_fmp_key_for_account_creation()


# --------------------------------------------------------------------------- the UI
@pytest.mark.parametrize("value", [None, "", "  "])
def test_ui_add_without_key_creates_nothing(fake_ui, value):
    if value is not None:
        _set_fmp(value)
    t = _new_account_form()
    t.save_account(None)
    assert fake_ui.notes and fake_ui.notes[-1][1].get("type") == "negative"
    assert "FMP API key" in fake_ui.notes[-1][0]
    assert _accounts() == []


def test_ui_add_dialog_does_not_open_without_key(fake_ui):
    t = _account_tab()
    t.dialog = SimpleNamespace()   # touching it would raise: the refusal comes first
    t.show_dialog()
    assert fake_ui.notes[-1][1].get("type") == "negative"
    assert "FMP API key" in fake_ui.notes[-1][0]


def test_ui_add_with_key_creates_the_account(fake_ui):
    _set_fmp("abc123")
    t = _new_account_form()
    t.save_account(None)
    assert not any(kw.get("type") == "negative" for _, kw in fake_ui.notes)
    accounts = _accounts()
    assert [a.name for a in accounts] == ["new-acc"]


def test_editing_an_existing_account_needs_no_key(fake_ui, monkeypatch):
    monkeypatch.setattr(settings_page, "get_account_instance_from_id", lambda *a, **k: None)
    acc = _stored_alpaca_account()
    t = _account_edit_form("Alpaca", acc)
    t.desc_input.value = "edited"
    t.save_account(acc)
    assert not any(kw.get("type") == "negative" for _, kw in fake_ui.notes)
    with get_db() as s:
        assert s.get(AccountDefinition, acc.id).description == "edited"


def test_saving_app_settings_with_an_empty_fmp_key_still_works(fake_ui):
    _set_fmp("abc123")
    t = _app_tab()
    t.fmp_input.value = ""
    t.save_settings()
    assert not any(kw.get("type") == "negative" for _, kw in fake_ui.notes)
    with get_db() as s:
        row = s.exec(select(AppSetting).where(AppSetting.key == "FMP_API_KEY")).first()
    assert row.value_str == ""
