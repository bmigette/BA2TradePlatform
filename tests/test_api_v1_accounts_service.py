"""Accounts service: masked credentials, live snapshot, Unmeasurable instead of zeros."""
import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import AccountSetting
from ba2_trade_platform.services import accounts as svc
from ba2_trade_platform.services.errors import NotFound, Unmeasurable
from tests.api_helpers import FakeAccount
from tests.factories import create_account_definition

pytestmark = pytest.mark.api_db


def _setting(account_id, key, value):
    add_instance(AccountSetting(account_id=account_id, key=key, value_str=value, value_json={},
                                value_float=None), expunge_after_flush=True)


@pytest.fixture
def fake(monkeypatch):
    acct = FakeAccount()
    monkeypatch.setattr(svc, "get_account_instance_from_id", lambda account_id: acct)
    return acct


def test_masked_settings_never_return_credentials():
    d = create_account_definition(name="Paper")
    _setting(d.id, "api_key", "AKSECRET123")
    _setting(d.id, "api_secret", "SHHH")
    _setting(d.id, "paper_account", "true")
    out = svc.account_definition(d.id)
    assert out["name"] == "Paper" and out["provider"] == "MockAccount"
    assert out["settings"]["paper_account"] == "true"
    assert out["settings"]["api_key"] == {"set": True}
    assert out["settings"]["api_secret"] == {"set": True}
    flat = str(out)
    assert "AKSECRET123" not in flat and "SHHH" not in flat


@pytest.mark.parametrize("key", ["flex_token", "login_password", "account_pin", "client_id", "passphrase",
                                 "Private_Key", "oauth_secret"])
def test_credential_looking_keys_are_masked(key):
    d = create_account_definition()
    _setting(d.id, key, "VALUE-XYZ")
    out = svc.account_definition(d.id)
    assert out["settings"][key] == {"set": True} and "VALUE-XYZ" not in str(out)


def test_definition_flags_mask_even_innocent_key_names():
    assert svc.is_secret_setting("harmless", {"secret": True})
    assert svc.is_secret_setting("harmless", {"sensitive": True})
    assert svc.is_secret_setting("harmless", {"type": "password"})
    assert not svc.is_secret_setting("harmless", {"type": "str"})
    assert not svc.is_secret_setting("paper_account", None)


def test_unset_secret_reports_set_false():
    d = create_account_definition()
    _setting(d.id, "api_key", "")
    assert svc.account_definition(d.id)["settings"]["api_key"] == {"set": False}


def test_account_detail_has_live_snapshot_and_capital(fake):
    d = create_account_definition()
    out = svc.account_detail(d.id)
    snap = out["snapshot"]
    assert snap["equity"] == 10000.0 and snap["cash"] == 2500.0 and snap["buying_power"] == 20000.0
    assert snap["broker_multiplier"] == 2.0 and snap["margin_factor"] == 1.5
    assert snap["tradable_balance"] == 15000.0 and snap["gross_exposure"] == 7500.0
    assert snap["headroom"] == 7000.0 and snap["pending_entries"] == 500.0
    assert snap["margin_enabled"] is True and "as_of" in snap


def test_missing_broker_figure_is_unmeasurable_not_zero(fake):
    fake.snapshot.cash = None
    d = create_account_definition()
    with pytest.raises(Unmeasurable) as ei:
        svc.account_detail(d.id)
    assert "cash" in ei.value.message


def test_describe_capital_refusal_is_unmeasurable(fake):
    fake.capital_error = "broker published no multiplier"
    with pytest.raises(Unmeasurable):
        svc.account_detail(create_account_definition().id)


def test_unknown_account_is_not_found():
    with pytest.raises(NotFound):
        svc.account_definition(424242)


def test_list_isolates_a_failing_snapshot(monkeypatch):
    a = create_account_definition(name="ok")
    b = create_account_definition(name="bad")
    good = FakeAccount(a.id)

    def resolver(account_id):
        if account_id == b.id:
            raise ValueError("no credentials")
        return good
    monkeypatch.setattr(svc, "get_account_instance_from_id", resolver)
    out = svc.list_accounts(limit=10, offset=0)
    by_name = {i["name"]: i for i in out["items"]}
    assert by_name["ok"]["snapshot"]["equity"] == 10000.0
    assert by_name["bad"]["snapshot"] is None
    assert by_name["bad"]["snapshot_error"]["error"] == "unmeasurable"


def test_margin_off_reports_null_margin_terms_with_flag(fake):
    fake.capital.update(margin_enabled=False, margin_factor=None, broker_multiplier=None,
                        effective_factor=1.0, tradable_balance=10000.0, broker_buying_power=None,
                        gross_exposure=None, pending_entries=None, headroom=None)
    snap = svc.account_detail(create_account_definition().id)["snapshot"]
    assert snap["margin_enabled"] is False and snap["headroom"] is None
    assert snap["pending_entries"] == 500.0   # still measured from the order store (see service comment)
