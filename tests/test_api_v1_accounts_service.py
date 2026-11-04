"""Accounts service: masked credentials, live snapshot, Unmeasurable instead of zeros."""
import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import AccountSetting
from ba2_trade_platform.services import accounts as svc
from ba2_trade_platform.services.errors import BrokerUnreachable, NotFound, Unmeasurable
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


# --- review fixes: one broker-read helper, honest orders, stale balance, list survives anything -------------
import time

from ba2_trade_platform.core.types import OrderStatus
from ba2_trade_platform.services.errors import BrokerRefused, Invalid


class APIError(Exception):          # same class NAME as the Alpaca SDK error
    pass


@pytest.mark.parametrize("exc,expected", [
    (OSError("secret-detail"), BrokerUnreachable),
    (TimeoutError("secret-detail"), BrokerUnreachable),
    (ConnectionError("secret-detail"), BrokerUnreachable),
    (APIError("secret-detail"), BrokerRefused),
    (ValueError("secret-detail"), Unmeasurable),
])
def test_broker_read_maps_exceptions_and_keeps_only_the_class_name(exc, expected):
    def fn():
        raise exc
    with pytest.raises(expected) as ei:
        svc.broker_read(5, "positions", fn)
    assert type(exc).__name__ in ei.value.message and "secret-detail" not in ei.value.message


def test_broker_read_lets_programming_errors_propagate():
    def fn():
        raise KeyError("bug")
    with pytest.raises(KeyError):
        svc.broker_read(5, "positions", fn)


def test_pending_entries_and_positions_go_through_the_helper(fake):
    fake.capital["pending_entries"] = None

    def boom(exclude_order_id=None):
        raise OSError("down")
    fake._pending_stock_entry_notional = boom
    d = create_account_definition()
    with pytest.raises(BrokerUnreachable):
        svc.account_snapshot(d.id)
    fake.get_positions = lambda: (_ for _ in ()).throw(TimeoutError("x"))
    with pytest.raises(BrokerUnreachable):
        svc.broker_positions(d.id)


def test_list_survives_any_single_accounts_exception(monkeypatch):
    a = create_account_definition(name="ok")
    b = create_account_definition(name="net")
    c = create_account_definition(name="bug")
    good = FakeAccount(a.id)

    def resolver(account_id):
        if account_id == b.id:
            raise OSError("down")
        if account_id == c.id:
            raise RuntimeError("anything at all")
        return good
    monkeypatch.setattr(svc, "get_account_instance_from_id", resolver)
    by_name = {i["name"]: i for i in svc.list_accounts(limit=10, offset=0)["items"]}
    assert by_name["ok"]["snapshot"]["equity"] == 10000.0
    assert by_name["net"]["snapshot"] is None and by_name["net"]["snapshot_error"]["error"] == "broker_unreachable"
    assert by_name["bug"]["snapshot"] is None and by_name["bug"]["snapshot_error"]["error"] == "internal"
    assert "anything at all" not in str(by_name["bug"])


def test_stale_cached_balance_is_unmeasurable_and_age_is_reported_otherwise(fake):
    d = create_account_definition()
    fake._balance_cache = 10000.0
    fake._balance_cache_time = time.time() - 400
    with pytest.raises(Unmeasurable) as ei:
        svc.account_snapshot(d.id)
    assert "stale" in ei.value.message
    fake._balance_cache_time = time.time() - 2
    snap = svc.account_snapshot(d.id)
    assert 1 <= snap["balance_age_s"] < 60


def test_balance_age_is_null_when_the_adapter_exposes_none(fake):
    assert svc.account_snapshot(create_account_definition().id)["balance_age_s"] is None


class FakeAlpaca(FakeAccount):
    client = object()

    def __init__(self):
        super().__init__(1)
        self.raw = ["o1", "o2"]
        self.error = None
        self.calls = []

    def _fetch_raw_alpaca_orders(self, status=None, fetch_all=False, full_history=False):
        self.calls.append((status, fetch_all, full_history))
        if self.error is not None:
            raise self.error
        return self.raw

    def alpaca_order_to_tradingorder(self, raw):
        return {"id": raw, "status": "filled" if raw == "o1" else "new"}

    def get_orders(self, status=None):
        raise AssertionError("must use the raising fetch path")


def test_alpaca_orders_use_the_raising_fetch_and_report_fetch_ok(monkeypatch):
    acct = FakeAlpaca()
    monkeypatch.setattr(svc, "get_account_instance_from_id", lambda i: acct)
    out = svc.broker_orders(1, None)
    assert [o["id"] for o in out["items"]] == ["o1", "o2"]
    assert out["fetch_ok"] is True and out["best_effort"] is False
    assert acct.calls == [(OrderStatus.ALL, False, True)]       # full_history=True => raises instead of []
    acct.raw = []
    assert svc.broker_orders(1, "open")["fetch_ok"] is True      # a verified empty book
    assert acct.calls[-1][0] == OrderStatus.OPEN


def test_alpaca_specific_status_is_filtered_in_the_service(monkeypatch):
    acct = FakeAlpaca()
    monkeypatch.setattr(svc, "get_account_instance_from_id", lambda i: acct)
    out = svc.broker_orders(1, "filled")
    assert [o["id"] for o in out["items"]] == ["o1"] and acct.calls[-1][0] == OrderStatus.ALL


def test_alpaca_fetch_failure_is_an_error_not_an_empty_list(monkeypatch):
    acct = FakeAlpaca()
    monkeypatch.setattr(svc, "get_account_instance_from_id", lambda i: acct)
    acct.error = APIError("rejected")
    with pytest.raises(BrokerRefused):
        svc.broker_orders(1, None)
    acct.error = OSError("dns")
    with pytest.raises(BrokerUnreachable):
        svc.broker_orders(1, None)
    acct.error = None
    acct.client = None                                          # not authenticated: _fetch returns [] silently
    with pytest.raises(BrokerUnreachable):
        svc.broker_orders(1, None)


def test_best_effort_adapter_empty_answer_is_not_presented_as_no_orders(fake):
    d = create_account_definition()
    fake.orders = []
    out = svc.broker_orders(d.id, None)
    assert out["items"] == [] and out["fetch_ok"] is False and out["best_effort"] is True and "note" in out
    fake.orders = [{"id": "b-1", "status": "new"}]
    out = svc.broker_orders(d.id, None)
    assert out["fetch_ok"] is True and out["best_effort"] is True


def test_orders_status_is_validated():
    with pytest.raises(Invalid):
        svc.broker_orders(1, "bogus")
