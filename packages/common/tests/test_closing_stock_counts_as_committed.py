"""A stock position with a resting close order is still HELD, so it still counts as committed.

2026-10-07 (audit item 6 ii): a STOCK transaction in status CLOSING used to be dropped from the
expert's ``used`` balance, so a $10k slice with 100 AAPL at $100 under a resting limit sell read
``used == 0`` and funded a $10k MSFT while the AAPL was still held. Three readers share one set of
statuses now (``CAPITAL_HOLDING_TRANSACTION_STATUSES``): the used balance, the classic RM's
per-instrument allocation and the option per-underlying commitment.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ba2_common.core.db import add_instance
from ba2_common.core.interfaces.MarketExpertInterface import capital_transactions
from ba2_common.core.models import Transaction
from ba2_common.core.types import (
    CAPITAL_HOLDING_TRANSACTION_STATUSES, AssetClass, OrderDirection, TransactionStatus)
from tests.test_bull_put_spread import _own_db  # noqa: F401  (autouse DB)


def _stock(status, *, expert=7, symbol="AAPL", qty=100.0, price=100.0):
    return add_instance(Transaction(
        symbol=symbol, quantity=qty, side=OrderDirection.BUY, open_price=price,
        asset_class=AssetClass.EQUITY, status=status, expert_id=expert))


def test_the_shared_status_set_is_waiting_opened_closing():
    assert set(CAPITAL_HOLDING_TRANSACTION_STATUSES) == {
        TransactionStatus.WAITING, TransactionStatus.OPENED, TransactionStatus.CLOSING}


def test_capital_transactions_keep_a_closing_stock_row_until_it_is_closed():
    closing = _stock(TransactionStatus.CLOSING)
    closed = _stock(TransactionStatus.CLOSED, symbol="MSFT")
    ids = {t.id for t in capital_transactions(7)}
    assert closing in ids and closed not in ids


def test_the_classic_per_instrument_cap_counts_a_closing_position():
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement
    _stock(TransactionStatus.OPENED, symbol="AAPL", qty=10, price=100.0)
    _stock(TransactionStatus.CLOSING, symbol="AAPL", qty=100, price=100.0)
    _stock(TransactionStatus.CLOSED, symbol="AAPL", qty=500, price=100.0)
    allocations = TradeRiskManagement()._get_existing_allocations(7)
    assert allocations == {"AAPL": pytest.approx(11_000.0)}


def test_the_option_per_underlying_commitment_counts_a_closing_stock_position():
    from ba2_common.core.TradeActions import BuyCallAction
    _stock(TransactionStatus.CLOSING, symbol="TSM", qty=50, price=200.0)
    action = BuyCallAction.__new__(BuyCallAction)
    action.instrument_name = "TSM"
    action.expert_recommendation = SimpleNamespace(instance_id=7)
    total, why = action._committed_to_underlying()
    assert why is None and total == pytest.approx(10_000.0)
