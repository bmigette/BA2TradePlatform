"""services.trades.cancel_order."""
import pytest

from ba2_trade_platform.core.types import OrderStatus, OrderType
from ba2_trade_platform.services import trades as svc
from ba2_trade_platform.services.errors import BrokerRefused, BrokerUnreachable, Conflict, NotFound
from tests.api_write_fakes import RecordingAccount, install_account
from tests.factories import create_account_definition, create_trading_order

pytestmark = pytest.mark.api_db


@pytest.fixture
def acct(monkeypatch):
    account = RecordingAccount()
    install_account(monkeypatch, account)
    return account


def _order(status=OrderStatus.OPEN, broker_order_id="b-1"):
    a = create_account_definition()
    return create_trading_order(a.id, order_type=OrderType.BUY_LIMIT, status=status, broker_order_id=broker_order_id)


def test_cancel_calls_the_account_with_the_platform_order_id(acct):
    o = _order()
    out = svc.cancel_order(o.id, dry_run=False)
    assert acct.calls == [("cancel_order", o.id)]
    assert out["cancel_requested"] is True and out["broker_order_id"] == "b-1"


@pytest.mark.parametrize("status", [OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED,
                                    OrderStatus.EXPIRED, OrderStatus.ERROR, OrderStatus.PENDING_CANCEL])
def test_terminal_or_filled_order_is_409(acct, status):
    o = _order(status=status)
    with pytest.raises(Conflict):
        svc.cancel_order(o.id, dry_run=False)
    with pytest.raises(Conflict):
        svc.cancel_order(o.id, dry_run=True)
    assert acct.calls == []


def test_an_order_the_broker_never_saw_is_409(acct):
    o = _order(status=OrderStatus.WAITING_TRIGGER, broker_order_id=None)
    with pytest.raises(Conflict, match="not been sent"):
        svc.cancel_order(o.id, dry_run=False)
    assert acct.calls == []


def test_unknown_order_is_404(acct):
    with pytest.raises(NotFound):
        svc.cancel_order(9999, dry_run=False)


def test_dry_run_calls_nothing(acct):
    o = _order()
    out = svc.cancel_order(o.id, dry_run=True)
    assert out["would_cancel"]["id"] == o.id and acct.calls == []


def test_broker_refusals_are_typed(acct):
    o = _order()
    acct.returns["cancel_order"] = False
    with pytest.raises(BrokerRefused):
        svc.cancel_order(o.id, dry_run=False)
    acct.returns.clear()
    acct.raises["cancel_order"] = ConnectionError("down")
    with pytest.raises(BrokerUnreachable):
        svc.cancel_order(o.id, dry_run=False)
