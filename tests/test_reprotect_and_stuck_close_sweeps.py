"""Two live refresh sweeps that close gaps found on dev 8080 (2026-11-08).

* ``_reprotect_partially_filled_exits``: CELH transaction 72. The TP side of a 52-share OCO
  filled 29; Alpaca cancelled the stop leg and the other 23 rode with a take-profit and no
  stop. The sweep re-places the exit through ``adjust_tp_sl``.
* ``_retry_stuck_closes``: PRG transaction 258. The stop-breach forced close was refused, the
  transaction stayed CLOSING and nothing ever revisited it. The sweep retries the close.

Logs are recorded on ``TradeManager.logger`` directly (``ba2_common.logger`` does not
propagate, so ``caplog`` would see nothing).
"""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from tests.factories import create_account_definition, create_trading_order, create_transaction

pytestmark = pytest.mark.usefixtures("reset_test_db")

NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


class _Log:
    def __init__(self):
        self.warning_msgs, self.error_msgs = [], []

    def warning(self, msg, *a, **k):
        self.warning_msgs.append(str(msg))

    def error(self, msg, *a, **k):
        self.error_msgs.append(str(msg))

    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


class _Account:
    def __init__(self, account_id, *, adjust_ok=True, close_ok=True, market_open=True):
        self.id = account_id
        self.adjusts, self.closes = [], []
        self._adjust_ok, self._close_ok, self._open = adjust_ok, close_ok, market_open

    def is_market_open(self, *, now=None):
        return self._open

    def adjust_tp_sl(self, txn, tp, sl, source=""):
        self.adjusts.append((txn.id, tp, sl, source))
        return self._adjust_ok

    def close_transaction(self, txn_id):
        self.closes.append(txn_id)
        return {"success": self._close_ok, "message": "closing" if self._close_ok else "refused",
                "canceled_count": 0, "deleted_count": 0,
                "close_order_id": 99 if self._close_ok else None}


def _manager():
    tm = TradeManager()
    tm.logger = _Log()
    return tm


@pytest.fixture
def no_activity(monkeypatch):
    logged = []
    monkeypatch.setattr("ba2_common.core.utils.log_close_order_activity",
                        lambda **kw: logged.append(kw))
    monkeypatch.setattr("ba2_trade_platform.core.db.log_activity",
                        lambda *a, **kw: logged.append((a, kw)))
    return logged


# --------------------------------------------------------------------------- #
# _reprotect_partially_filled_exits
# --------------------------------------------------------------------------- #

def _celh(acct, *, leg_status=OrderStatus.CANCELED, oco_status=OrderStatus.PARTIALLY_FILLED,
          stop_loss=24.95, staged=False, with_leg=True):
    txn = create_transaction(symbol="CELH", quantity=23.0, open_price=28.35,
                             take_profit=32.52, stop_loss=stop_loss,
                             status=TransactionStatus.OPENED)
    create_trading_order(account_id=acct.id, transaction_id=txn.id, symbol="CELH",
                         side=OrderDirection.BUY, quantity=52.0, order_type=OrderType.MARKET,
                         status=OrderStatus.FILLED, filled_qty=52.0)
    oco = create_trading_order(account_id=acct.id, transaction_id=txn.id, symbol="CELH",
                               side=OrderDirection.SELL, quantity=52.0, order_type=OrderType.OCO,
                               status=oco_status, filled_qty=29.0, limit_price=32.52,
                               stop_price=24.95, broker_order_id="b-oco")
    if with_leg:
        create_trading_order(account_id=acct.id, transaction_id=txn.id, symbol="CELH",
                             side=OrderDirection.SELL, quantity=52.0,
                             order_type=OrderType.SELL_STOP_LIMIT, status=leg_status,
                             filled_qty=0.0, stop_price=24.95, parent_order_id=oco.id)
    if staged:
        create_trading_order(account_id=acct.id, transaction_id=txn.id, symbol="CELH",
                             side=OrderDirection.SELL, quantity=23.0, order_type=OrderType.OCO,
                             status=OrderStatus.WAITING_TRIGGER, depends_on_order=oco.id,
                             depends_order_status_trigger=OrderStatus.CANCELED)
    return txn


def test_partly_filled_oco_without_stop_is_replaced(no_activity):
    acct = create_account_definition()
    txn = _celh(acct)
    account, tm = _Account(acct.id), _manager()

    tm._reprotect_partially_filled_exits(account)

    assert account.adjusts == [(txn.id, 32.52, 24.95, "partial_fill_reprotect")]
    assert any("no stop" in m for m in tm.logger.warning_msgs)
    assert tm.logger.error_msgs == []


def test_partly_filled_oco_with_no_leg_row_is_replaced(no_activity):
    acct = create_account_definition()
    txn = _celh(acct, with_leg=False)
    account = _Account(acct.id)

    _manager()._reprotect_partially_filled_exits(account)

    assert [a[0] for a in account.adjusts] == [txn.id]


@pytest.mark.parametrize("case", ["stop-leg-working", "replacement-staged", "oco-not-partial",
                                  "no-stop-loss"])
def test_nothing_to_reprotect(no_activity, case):
    acct = create_account_definition()
    kw = {
        "stop-leg-working": dict(leg_status=OrderStatus.HELD),
        "replacement-staged": dict(staged=True),
        "oco-not-partial": dict(oco_status=OrderStatus.NEW),
        "no-stop-loss": dict(stop_loss=None),
    }[case]
    _celh(acct, **kw)
    account = _Account(acct.id)

    _manager()._reprotect_partially_filled_exits(account)

    assert account.adjusts == []


def test_failed_reprotect_is_loud(no_activity):
    acct = create_account_definition()
    _celh(acct)
    account, tm = _Account(acct.id, adjust_ok=False), _manager()

    tm._reprotect_partially_filled_exits(account)

    assert len(account.adjusts) == 1
    assert any("FAILED" in m for m in tm.logger.error_msgs)
    assert len(no_activity) == 1


# --------------------------------------------------------------------------- #
# _retry_stuck_closes
# --------------------------------------------------------------------------- #

def _prg(acct, *, close_status=OrderStatus.ERROR, age_minutes=60, asset_class=None,
         status=TransactionStatus.CLOSING):
    kw = {"asset_class": asset_class} if asset_class else {}
    txn = create_transaction(symbol="PRG", quantity=1.0, open_price=28.2, stop_loss=29.68,
                             status=status, **kw)
    created = NOW - timedelta(minutes=age_minutes)
    create_trading_order(account_id=acct.id, transaction_id=txn.id, symbol="PRG",
                         side=OrderDirection.BUY, quantity=15.0, order_type=OrderType.MARKET,
                         status=OrderStatus.FILLED, filled_qty=15.0,
                         created_at=created - timedelta(days=20))
    create_trading_order(account_id=acct.id, transaction_id=txn.id, symbol="PRG",
                         side=OrderDirection.SELL, quantity=15.0, order_type=OrderType.MARKET,
                         status=close_status, comment="Closing position for transaction",
                         created_at=created)
    return txn


def test_dead_close_is_retried(no_activity):
    acct = create_account_definition()
    txn = _prg(acct)
    account, tm = _Account(acct.id), _manager()

    tm._retry_stuck_closes(account, now=NOW)

    assert account.closes == [txn.id]
    assert any("Retrying the close" in m for m in tm.logger.warning_msgs)
    assert no_activity[0]["is_retry"] is True and no_activity[0]["success"] is True


@pytest.mark.parametrize("case", ["too-recent", "close-still-working", "market-closed",
                                  "option", "opened"])
def test_no_retry(no_activity, case):
    acct = create_account_definition()
    kw = {
        "too-recent": dict(age_minutes=10),
        "close-still-working": dict(close_status=OrderStatus.ACCEPTED),
        "market-closed": {},
        "option": dict(asset_class=AssetClass.OPTION),
        "opened": dict(status=TransactionStatus.OPENED),
    }[case]
    _prg(acct, **kw)
    account = _Account(acct.id, market_open=(case != "market-closed"))

    _manager()._retry_stuck_closes(account, now=NOW)

    assert account.closes == []


def test_failed_retry_is_loud(no_activity):
    acct = create_account_definition()
    _prg(acct)
    account, tm = _Account(acct.id, close_ok=False), _manager()

    tm._retry_stuck_closes(account, now=NOW)

    assert len(account.closes) == 1
    assert any("FAILED" in m and "refused" in m for m in tm.logger.error_msgs)
    assert no_activity[0]["success"] is False
