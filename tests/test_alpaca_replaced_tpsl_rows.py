"""AlpacaAccount's TP/SL price replacement writes a NEW row: it must stay recognisable as
protection and belong to the account.

``_update_broker_tp_order`` / ``_update_broker_sl_order`` copy the broker's replacement into a
fresh ``TradingOrder``. The comment came from the broker mapping (None), so a replaced root
TP/SL carried no TP/SL mark and ``has_pending_closing_order`` read it as a close still working
(the 2026-09-26 skipped-exit-rules defect). The SL replacement also dropped ``account_id``, so
the row fell out of every per-account order query.

No live API call: ``modify_order`` / ``refresh_orders`` are stubbed.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_common.core.TransactionHelper import TransactionHelper
from tests.factories import create_account_definition, create_trading_order, create_transaction


def _account(account_id):
    acct = object.__new__(AlpacaAccount)
    acct.id = account_id
    acct.client = MagicMock()
    acct._margin_info_cache = {}
    acct.modify_order = lambda broker_id, order: SimpleNamespace(
        broker_order_id="brk-replacement", status=OrderStatus.NEW, comment=None)
    acct.refresh_orders = lambda *a, **k: True
    return acct


def _replacement(account_id, txn_id):
    rows = [o for o in _all_orders() if o.broker_order_id == "brk-replacement"]
    assert len(rows) == 1
    return rows[0]


def _all_orders():
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db
    with get_db() as session:
        rows = session.exec(select(TradingOrder)).all()
        for r in rows:
            session.expunge(r)
        return rows


@pytest.fixture
def book():
    acct_def = create_account_definition()
    txn = create_transaction(symbol="AAPL", quantity=10.0)
    entry = create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=10.0,
                                 transaction_id=txn.id, status=OrderStatus.FILLED,
                                 filled_qty=10.0)
    return acct_def, txn, entry


@pytest.mark.parametrize("kind", ["TP", "SL"])
def test_a_replaced_root_leg_keeps_its_protection_mark_and_account(book, kind):
    acct_def, txn, entry = book
    leg = create_trading_order(
        account_id=acct_def.id, symbol="AAPL", quantity=10.0, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT if kind == "TP" else OrderType.SELL_STOP,
        limit_price=120.0 if kind == "TP" else None, stop_price=90.0 if kind == "SL" else None,
        transaction_id=txn.id, status=OrderStatus.NEW, broker_order_id="brk-old",
        comment=f"{kind} order (legacy text)")
    account = _account(acct_def.id)

    if kind == "TP":
        account._update_broker_tp_order(get_instance(TradingOrder, leg.id), 125.0)
    else:
        account._update_broker_sl_order(get_instance(TradingOrder, leg.id), 92.0)

    new = _replacement(acct_def.id, txn.id)
    assert new.depends_on_order is None, "a root leg's replacement is a root row"
    assert TransactionHelper.is_resting_protection(new), new.comment
    assert str(leg.id) in new.comment, "the trace to the replaced order is kept"
    assert new.account_id == acct_def.id
