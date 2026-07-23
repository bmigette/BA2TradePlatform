"""Regression 2026-07-22: a live goal6 trade's ruleset-computed take_profit was silently
wiped back to None shortly after being set correctly (confirmed via the activity log:
"Adjusted TP none -> $36.87 ... source: ruleset", followed by no further TP_SL_ADJUSTED
entry, yet the DB row later showed take_profit=None).

Root cause: AccountInterface._recalculate_transaction_quantity (called right after the
entry order is submitted, moments after the ruleset's separate-session TP/SL commit) used
to call the generic db.update_instance(transaction) to persist a quantity change.
update_instance copies EVERY attribute from whatever Python object it's given onto a
freshly-fetched DB row -- so if that in-memory `transaction` object was constructed or
fetched before some other field (take_profit/stop_loss) was committed elsewhere, the
"unrelated" quantity-only intent silently clobbers the other field back to its stale value.

Fixed to scope the write to the `.quantity` column only (LIVE/SQLite path), leaving the
in-mem backtest store's update_instance call untouched (safe there: same-object-identity
means it's a no-op persist, see db.py's _inmem_route branch and
test_trade_actions_account_interface_inmem.py's coverage of that path).
"""
from ba2_common.core.db import add_instance, get_instance, get_db
from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus


class _StubAccount(AccountInterface):
    """Minimal concrete AccountInterface -- only _recalculate_transaction_quantity is
    under test, so every other abstract method is a no-op stub purely to satisfy ABC
    instantiation (mirrors test_trade_actions_account_interface_inmem.py's stub)."""
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


def test_recalculate_transaction_quantity_does_not_clobber_concurrent_tp_write(monkeypatch):
    txn = Transaction(symbol="PKE", quantity=0.0, side=OrderDirection.BUY,
                       status=TransactionStatus.OPENED, expert_id=1)
    txn_id = add_instance(txn)
    add_instance(TradingOrder(account_id=1, symbol="PKE", quantity=3.0, side=OrderDirection.BUY,
                               order_type=OrderType.MARKET, status=OrderStatus.FILLED,
                               transaction_id=txn_id))

    # A snapshot fetched BEFORE the ruleset's TP write below -- stands in for whatever
    # stale in-memory object _recalculate_transaction_quantity would be holding if its own
    # internal fetch raced ahead of (or otherwise missed) the concurrent TP commit. A real
    # race can't be reproduced deterministically in a single-threaded test, so it's forced
    # via monkeypatch below.
    stale_txn = get_instance(Transaction, txn_id)
    assert stale_txn.take_profit is None

    # Simulate the ruleset's separate-session TP write (mirrors
    # AlpacaAccount._adjust_tpsl_internal's own dedicated `with Session(...)` commit).
    with get_db() as session:
        db_txn = session.get(Transaction, txn_id)
        db_txn.take_profit = 36.87
        session.commit()

    # Force _recalculate_transaction_quantity to see the STALE object instead of a fresh one.
    # Resolved via sys.modules directly: the `interfaces` package's __init__ shadows its
    # `AccountInterface` submodule attribute with the class of the same name (both a plain
    # module-object reference and monkeypatch's string-target getattr-chain resolve to the
    # class, not the module), so neither works here.
    import sys
    ai_module = sys.modules["ba2_common.core.interfaces.AccountInterface"]
    monkeypatch.setattr(ai_module, "get_instance", lambda model, iid: stale_txn)

    acct = _bare_account()
    acct._recalculate_transaction_quantity(txn_id)

    updated = get_instance(Transaction, txn_id)
    assert updated.quantity == 3.0, "quantity must still be recalculated correctly"
    assert updated.take_profit == 36.87, "take_profit set by a concurrent write must survive"
