"""Live funded entry records the stop it was SIZED on as the transaction's max-loss stop (B3).

The write lands in ``TradeManager._submit_funded_entry_with_retry``, right after a submit that
returned an order, with ``sl_price`` (the RM safeguard the candidate was sized off). It must:

  * happen once, after the submit, and never during a DB-lock retry;
  * never make the retry loop re-send an order (it sits outside the retried try);
  * record the safeguard even when the reconciled (submitted) stop is a tighter ruleset stop.
"""
from datetime import datetime, timezone

import pytest

from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.types import OrderDirection


class _FakeOrder:
    id, symbol, quantity = 460, "CVS", 1.0
    transaction_id, side = None, OrderDirection.BUY


def _tm(monkeypatch):
    import logging
    tm = TradeManager.__new__(TradeManager)
    tm.logger = logging.getLogger("t")
    monkeypatch.setattr(TradeManager, "_ENTRY_SUBMIT_BACKOFF_S", 0.0, raising=False)
    return tm


@pytest.fixture
def recorded(monkeypatch):
    """Capture every call to the shared recorder (the TradeManager imports it lazily)."""
    from ba2_common.core import trade_cycle
    calls = []
    monkeypatch.setattr(trade_cycle, "record_max_loss_stop",
                        lambda order, sl: calls.append((order, sl)))
    return calls


def test_recorded_once_after_a_successful_submit(monkeypatch, recorded):
    order = _FakeOrder()

    class _Acct:
        def submit_order(self, o, sl_price=None):
            return o

    assert _tm(monkeypatch)._submit_funded_entry_with_retry(_Acct(), order, sl_price=82.29) is order
    assert recorded == [(order, 82.29)]


def test_recorded_once_after_a_lock_retry_not_per_attempt(monkeypatch, recorded):
    attempts = []

    class _Acct:
        def submit_order(self, o, sl_price=None):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("database is locked")
            return o

    order = _FakeOrder()
    _tm(monkeypatch)._submit_funded_entry_with_retry(_Acct(), order, sl_price=82.29)
    assert len(attempts) == 2
    assert recorded == [(order, 82.29)]


def test_nothing_recorded_when_the_submit_returns_nothing(monkeypatch, recorded):
    class _Acct:
        def submit_order(self, o, sl_price=None):
            return None

    assert _tm(monkeypatch)._submit_funded_entry_with_retry(_Acct(), _FakeOrder(), sl_price=82.29) is None
    assert recorded == []


def test_nothing_recorded_when_the_broker_rejects(monkeypatch, recorded):
    class _Acct:
        def submit_order(self, o, sl_price=None):
            raise ValueError("rejected")

    with pytest.raises(ValueError):
        _tm(monkeypatch)._submit_funded_entry_with_retry(_Acct(), _FakeOrder(), sl_price=82.29)
    assert recorded == []


def test_a_failing_record_can_never_re_send_the_order(monkeypatch):
    """Belt and braces: the recorder never raises by contract, but the call also sits OUTSIDE the
    retried try, so even an error that reads like a DB lock cannot trigger a second submit."""
    from ba2_common.core import trade_cycle

    def _boom(order, sl):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(trade_cycle, "record_max_loss_stop", _boom)
    submits = []

    class _Acct:
        def submit_order(self, o, sl_price=None):
            submits.append(1)
            return o

    with pytest.raises(RuntimeError):
        _tm(monkeypatch)._submit_funded_entry_with_retry(_Acct(), _FakeOrder(), sl_price=82.29)
    assert len(submits) == 1, "a metadata failure re-sent a live order"


def _real_entry(stop_loss):
    from ba2_trade_platform.core.db import add_instance
    from ba2_trade_platform.core.models import TradingOrder, Transaction
    from ba2_trade_platform.core.types import OrderStatus, OrderType, TransactionStatus

    txn_id = add_instance(Transaction(
        symbol="CVS", quantity=1.0, side=OrderDirection.BUY, open_price=100.0,
        stop_loss=stop_loss, status=TransactionStatus.WAITING,
        created_at=datetime.now(timezone.utc)))
    order = TradingOrder(
        symbol="CVS", quantity=1.0, side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.PENDING, transaction_id=txn_id, created_at=datetime.now(timezone.utc))
    return order, txn_id


@pytest.mark.parametrize("ruleset_sl, submitted_sl", [
    (97.0, 97.0),    # tighter ruleset stop is what protects the position...
    (85.0, 92.0),    # ...or the safeguard is the tighter one
    (None, 92.0),    # no ruleset stop at all
])
def test_the_safeguard_is_the_recorded_max_loss_stop(monkeypatch, ruleset_sl, submitted_sl):
    """Against the real DB: whatever stop the reconcile submits, the position was sized on the
    $92 safeguard, and that is what ends up on the transaction."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import Transaction
    from ba2_common.core.position_sizing import max_loss_stop_of

    order, txn_id = _real_entry(ruleset_sl)
    sent = []

    class _Acct:
        def submit_order(self, o, sl_price=None):
            sent.append(sl_price)
            return o

    _tm(monkeypatch)._submit_funded_entry_with_retry(_Acct(), order, sl_price=92.0)
    assert sent == [submitted_sl]
    assert max_loss_stop_of(get_instance(Transaction, txn_id)) == 92.0


def test_without_a_safeguard_the_ruleset_stop_is_recorded(monkeypatch):
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import Transaction
    from ba2_common.core.position_sizing import max_loss_stop_of

    order, txn_id = _real_entry(95.0)

    class _Acct:
        def submit_order(self, o, sl_price=None):
            assert sl_price is None, "only a ruleset stop: it is already its own leg"
            return o

    _tm(monkeypatch)._submit_funded_entry_with_retry(_Acct(), order, sl_price=None)
    assert max_loss_stop_of(get_instance(Transaction, txn_id)) == 95.0
