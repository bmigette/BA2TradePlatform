"""OPT-S5 — the LIVE single-leg option close must RIDE the open transaction.

``AlpacaAccount.close_option_position`` called ``submit_option_order`` positionally and
omitted ``transaction_id=``, so the seam minted a BRAND NEW Transaction for the closing
leg (``AccountInterface._create_transaction_for_order`` constructs one unconditionally —
it never looks for an existing open one).

Three consequences, all live-money:

* the ORIGINAL transaction never reaches CLOSED, so whatever exit condition decided to
  close it is still true on the next pass and submits the close AGAIN — forever;
* the buying-power reserve and the short-put assignment exposure are never released,
  because ``open_option_orders_book_wide`` keeps every order whose transaction is not
  CLOSED/FAILED;
* the closing leg is booked as an OPENING position of the opposite side.

Every sibling close path already passes the id — ``CloseOptionAction._close_multi_leg``,
``option_lifecycle_service._close`` and the backtest's own ``close_option_position``
(which spells the rationale out in its docstring). The live single-leg path was the sole
omission.
"""
from datetime import date, timedelta

import pytest

from ba2_trade_platform.core.db import add_instance, get_instance
from ba2_trade_platform.core.models import Transaction, TradingOrder
from ba2_trade_platform.core.option_types import OptionPosition
from ba2_trade_platform.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from tests.factories import create_account_definition, create_trading_order, create_transaction

_CONTRACT = "AAPL260116C00150000"
_EXPIRY = date(2026, 1, 16)


def _alpaca(account_id):
    acct = AlpacaAccount.__new__(AlpacaAccount)       # bypass __init__/DB/network
    acct.id = account_id
    acct._settings_cache = {"api_key": "k", "api_secret": "s", "paper_account": True,
                            "data_feed": "iex"}
    return acct


@pytest.fixture
def open_long_call(monkeypatch):
    """A live AlpacaAccount holding ONE open long call: Transaction + FILLED option BUY.

    The broker's own view is published too (``get_option_positions``), so the double is
    not a platform-side-only holding.
    """
    acct_def = create_account_definition(provider="AlpacaAccount")
    acct = _alpaca(acct_def.id)

    txn = create_transaction(symbol="AAPL", quantity=2.0, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=5.0,
                             asset_class=AssetClass.OPTION, multiplier=100.0)
    entry = create_trading_order(
        account_id=acct.id, symbol="AAPL", quantity=2.0, side=OrderDirection.BUY,
        order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED, transaction_id=txn.id,
        filled_qty=2.0, asset_class=AssetClass.OPTION, option_type=OptionRight.CALL,
        contract_symbol=_CONTRACT, underlying_symbol="AAPL", strike=150.0, expiry=_EXPIRY,
        option_strategy="long_call", open_price=5.0, limit_price=5.0, multiplier=100.0,
        broker_order_id="entry-broker-1",
    )

    position = OptionPosition(
        contract_symbol=_CONTRACT, underlying="AAPL", option_type=OptionRight.CALL,
        strike=150.0, expiry=_EXPIRY, side=OrderDirection.BUY, quantity=2,
        avg_entry_price=5.0)

    # The broker agrees the position exists.
    monkeypatch.setattr(acct, "get_option_positions", lambda: [position], raising=False)
    monkeypatch.setattr(acct, "get_positions", lambda: [], raising=False)
    monkeypatch.setattr(acct, "get_option_quote", lambda cs: None, raising=False)

    submitted = []

    def fake_impl(trading_order, legs, leg_orders=None):
        from ba2_trade_platform.core.db import update_instance
        trading_order.status = OrderStatus.FILLED
        trading_order.filled_qty = trading_order.quantity
        trading_order.broker_order_id = f"close-broker-{trading_order.id}"
        update_instance(trading_order)
        submitted.append(trading_order)
        return trading_order

    monkeypatch.setattr(acct, "_submit_option_order_impl", fake_impl, raising=False)
    return acct, txn, entry, position, submitted


def _transaction_ids():
    from sqlmodel import Session, select
    from ba2_trade_platform.core.db import get_db
    with Session(get_db().bind) as s:
        return sorted(t.id for t in s.exec(select(Transaction)).all())


def test_live_single_leg_close_rides_the_open_transaction(open_long_call):
    """The closing order must carry the OPEN position's transaction id — not a new one."""
    acct, txn, entry, position, submitted = open_long_call
    before = _transaction_ids()

    close_order = acct.close_option_position(position, order_type="limit", limit_price=6.0)

    assert close_order is not None, "the close must reach the broker"
    assert close_order.transaction_id == txn.id, (
        f"the close was booked on transaction {close_order.transaction_id}, not the open "
        f"position's transaction {txn.id}")
    assert _transaction_ids() == before, (
        "close_option_position minted a NEW Transaction for the closing leg; the original "
        "position can therefore never reach CLOSED and the exit will re-submit forever")


def test_live_single_leg_close_does_not_leave_the_reserve_charged(open_long_call):
    """A close that rides the transaction lets the book release it when it closes.

    ``open_option_orders_book_wide`` keeps every option order whose transaction is not
    CLOSED/FAILED. With the close on its OWN fresh transaction, closing the original
    releases nothing: the orphan transaction stays open and keeps the reserve (and the
    short-put assignment exposure) charged for a position that no longer exists.
    """
    acct, txn, entry, position, submitted = open_long_call
    acct.close_option_position(position, order_type="market")

    txn.status = TransactionStatus.CLOSED
    from ba2_trade_platform.core.db import update_instance
    update_instance(txn)

    still_open = acct.open_option_orders_book_wide()
    assert still_open == [], (
        "after the position's transaction closed, these option orders are still counted as "
        f"open and keep charging buying power: "
        f"{[(o.id, o.contract_symbol, o.transaction_id, str(o.status)) for o in still_open]}")


def test_close_option_action_through_the_real_seam_rides_the_transaction(
        open_long_call, monkeypatch):
    """End to end: the CLOSE_OPTION action, the real live adapter, one transaction.

    No double stands between the action and ``submit_option_order`` here, so this covers
    the path a live exit rule actually takes.
    """
    from ba2_common.core.TradeActions import CloseOptionAction

    acct, txn, entry, position, submitted = open_long_call
    before = _transaction_ids()

    action = CloseOptionAction.__new__(CloseOptionAction)
    action.account = acct
    action.instrument_name = "AAPL"
    action.existing_order = entry
    action.submit_to_broker = True
    monkeypatch.setattr(action, "create_and_save_action_result",
                        lambda **kw: kw, raising=False)

    action.execute()
    assert len(submitted) == 1, "the close never reached the broker"
    assert submitted[0].transaction_id == txn.id
    assert _transaction_ids() == before


def _capture_errors(monkeypatch):
    """``logger.error`` as seen BY ``close_option_position`` itself. NOT caplog —
    ba2_trade_platform's logger installs its own handler with ``propagate = False``.

    Patched in the function's own ``__globals__`` rather than via ``sys.modules``: that is
    by definition the namespace the code under test resolves ``logger`` in, so it cannot be
    defeated by a re-import, by the package ``__init__`` rebinding the module name to the
    class, or by another test holding a different logger object of the same name.
    """
    module_globals = AlpacaAccount.close_option_position.__globals__
    real = module_globals["logger"]
    messages = []

    class _Tee:
        def __getattr__(self, name):
            return getattr(real, name)

        def error(self, msg, *a, **k):
            messages.append(str(msg))

    monkeypatch.setitem(module_globals, "logger", _Tee())
    return messages


def test_a_close_with_no_findable_transaction_still_reaches_the_broker(
        open_long_call, monkeypatch):
    """Flattening must never be blocked by a missing ledger link — but it must be LOUD.

    Refusing here would strand a real broker position that can no longer be exited, which
    is strictly worse than an orphan transaction row. This pins the direction of that
    trade-off so a later "just refuse" simplification cannot pass silently.
    """
    acct, txn, entry, position, submitted = open_long_call
    errors = _capture_errors(monkeypatch)
    # The book no longer holds this contract (already reconciled away, say).
    monkeypatch.setattr(acct, "open_option_transaction_id_for_contract",
                        lambda cs: None, raising=False)

    order = acct.close_option_position(position, order_type="market")

    assert order is not None and len(submitted) == 1
    assert any("no OPEN transaction holding it" in m for m in errors), (
        f"an unlinked close was submitted without saying so; errors logged: {errors}")


def test_a_contract_already_netted_flat_is_not_re_attached(open_long_call):
    """A contract whose buys and sells offset is FLAT — a close must not ride it again.

    Without the net (a bare "is this contract mentioned?" match) the SECOND close of an
    already-closed leg would attach to the same transaction and reduce a position that no
    longer exists.
    """
    acct, txn, entry, position, submitted = open_long_call
    create_trading_order(
        account_id=acct.id, symbol="AAPL", quantity=2.0, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED, transaction_id=txn.id,
        filled_qty=2.0, asset_class=AssetClass.OPTION, option_type=OptionRight.CALL,
        contract_symbol=_CONTRACT, underlying_symbol="AAPL", strike=150.0, expiry=_EXPIRY,
        option_strategy="close", open_price=6.0, multiplier=100.0,
        broker_order_id="close-broker-1",
    )
    assert acct.open_option_transaction_id_for_contract(_CONTRACT) is None
