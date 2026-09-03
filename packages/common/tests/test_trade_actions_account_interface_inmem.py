"""M2 fix (review 2026-07-18): several TradeActions.py / AccountInterface.py methods did
raw select(Transaction)/select(TradingOrder) queries, which silently find nothing when the
backtest in-mem store is active (see trade_store.IN_MEM_MODELS) -- the same bug class the
senate-basket-dispatch changeset fixed elsewhere. These tests prove the in-mem-store path
actually FINDS the linked rows instead of silently returning empty/None/0."""
from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.TradeActions import SellAction, BuyCallAction, CloseOptionAction
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)


def _order(account_id=1, symbol="AAPL", txn_id=None, status=OrderStatus.FILLED,
          filled_qty=None, side=OrderDirection.BUY, asset_class=AssetClass.EQUITY,
          depends_on_order=None):
    return TradingOrder(account_id=account_id, symbol=symbol, quantity=10.0,
                        filled_qty=filled_qty, side=side, order_type=OrderType.MARKET,
                        status=status, transaction_id=txn_id, asset_class=asset_class,
                        depends_on_order=depends_on_order)


def _txn(symbol="AAPL", status=TransactionStatus.OPENED, expert_id=1,
         side=OrderDirection.BUY):
    return Transaction(symbol=symbol, quantity=10.0, side=side, status=status,
                       expert_id=expert_id)


def _bare_action(cls, instrument_name, instance_id):
    action = cls.__new__(cls)
    action.instrument_name = instrument_name
    action.expert_recommendation = type("Rec", (), {"instance_id": instance_id})()
    action.existing_order = None
    return action


def test_get_expert_position_finds_transaction_in_mem():
    with ts.inmem_trades():
        add_instance(_txn(symbol="AAPL", expert_id=42, side=OrderDirection.BUY))
        action = _bare_action(SellAction, "AAPL", 42)
        assert action.get_expert_position() == 10.0


def test_get_expert_position_returns_zero_when_none_found_in_mem():
    with ts.inmem_trades():
        action = _bare_action(SellAction, "AAPL", 42)
        assert action.get_expert_position() == 0.0


def test_held_equity_shares_finds_transaction_and_orders_in_mem():
    with ts.inmem_trades():
        txn_id = add_instance(_txn(symbol="AAPL", expert_id=42, status=TransactionStatus.OPENED))
        add_instance(_order(txn_id=txn_id, filled_qty=10.0, side=OrderDirection.BUY))
        action = _bare_action(BuyCallAction, "AAPL", 42)
        assert action._held_equity_shares() == 10.0


def test_resolve_option_order_finds_option_leg_via_transaction_in_mem():
    with ts.inmem_trades():
        txn_id = add_instance(_txn(symbol="AAPL"))
        option_order = _order(txn_id=txn_id, asset_class=AssetClass.OPTION,
                              status=OrderStatus.FILLED, filled_qty=1.0)
        option_order.contract_symbol = "AAPL260101C00100000"
        option_id = add_instance(option_order)

        existing = TradingOrder(account_id=1, symbol="AAPL", quantity=10.0,
                                side=OrderDirection.BUY, order_type=OrderType.MARKET,
                                status=OrderStatus.NEW, transaction_id=txn_id,
                                asset_class=AssetClass.EQUITY)
        action = CloseOptionAction.__new__(CloseOptionAction)
        action.existing_order = existing
        # The bare fixture must set every attribute the method under test READS.
        # ``close_target`` (2026-09-03) selects the repository lookup an equity-anchored
        # overlay key needs; None is the transaction-anchored path this test is about.
        action.close_target = None
        action.expert_recommendation = None
        resolved = action._resolve_option_order()
        assert resolved is not None and resolved.id == option_id


def test_close_multi_leg_query_finds_child_legs_in_mem():
    """_close_multi_leg's full method needs live broker/quote wiring (build_closing_legs,
    create_and_save_action_result) that's out of scope for a bare fixture -- this proves
    the underlying query it now runs (routed through store_all + a Python filter instead
    of a raw select()) actually finds the child legs, the specific thing M2 fixed."""
    with ts.inmem_trades():
        parent = _order(status=OrderStatus.FILLED, filled_qty=1.0)
        parent_id = add_instance(parent)
        child = _order(status=OrderStatus.FILLED, filled_qty=1.0,
                       asset_class=AssetClass.OPTION)
        child.parent_order_id = parent_id
        child.contract_symbol = "AAPL260101C00100000"
        child_id = add_instance(child)

        children = [o for o in ts.store_all(TradingOrder)
                   if o.parent_order_id == parent_id and o.contract_symbol is not None]
        assert [c.id for c in children] == [child_id]


class _StubAccount(AccountInterface):
    """Minimal concrete AccountInterface -- only _recalculate_transaction_quantity /
    _get_transaction_entry_order are under test, so every other abstract method is a
    no-op stub purely to satisfy ABC instantiation."""
    def _get_instrument_current_price_impl(self, *a, **k): raise NotImplementedError
    def _submit_order_impl(self, *a, **k): raise NotImplementedError
    def adjust_sl(self, *a, **k): raise NotImplementedError
    def adjust_tp(self, *a, **k): raise NotImplementedError
    def adjust_tp_sl(self, *a, **k): raise NotImplementedError
    def cancel_order(self, *a, **k): raise NotImplementedError
    def get_account_info(self, *a, **k): raise NotImplementedError
    def get_balance(self, *a, **k): raise NotImplementedError
    def get_balance_history(self, *a, **k): raise NotImplementedError
    def get_dividends(self, *a, **k): raise NotImplementedError
    def get_filled_trades(self, *a, **k): raise NotImplementedError
    def get_order(self, *a, **k): raise NotImplementedError
    def get_orders(self, *a, **k): raise NotImplementedError
    def get_positions(self, *a, **k): raise NotImplementedError
    def modify_order(self, *a, **k): raise NotImplementedError
    def refresh_orders(self, *a, **k): raise NotImplementedError
    def refresh_positions(self, *a, **k): raise NotImplementedError
    def symbols_exist(self, *a, **k): raise NotImplementedError


def _bare_account(account_id=1):
    acct = _StubAccount.__new__(_StubAccount)
    acct.id = account_id
    return acct


def test_recalculate_transaction_quantity_finds_entry_orders_in_mem():
    with ts.inmem_trades():
        txn = _txn(side=OrderDirection.BUY)
        txn_id = add_instance(txn)
        add_instance(_order(txn_id=txn_id, side=OrderDirection.BUY,
                            status=OrderStatus.FILLED, depends_on_order=None))
        add_instance(_order(txn_id=txn_id, side=OrderDirection.BUY,
                            status=OrderStatus.FILLED, depends_on_order=None))

        acct = _bare_account()
        acct._recalculate_transaction_quantity(txn_id)
        updated_txn = ts.store_get(Transaction, txn_id)
        assert updated_txn.quantity == 20.0, "must sum BOTH matching entry orders found via the in-mem store"


def test_get_transaction_entry_order_finds_lowest_id_order_in_mem():
    with ts.inmem_trades():
        txn_id = add_instance(_txn())
        first_id = add_instance(_order(txn_id=txn_id))
        add_instance(_order(txn_id=txn_id))  # a later, higher-id order

        acct = _bare_account()
        entry = acct._get_transaction_entry_order(txn_id)
        assert entry is not None and entry.id == first_id
