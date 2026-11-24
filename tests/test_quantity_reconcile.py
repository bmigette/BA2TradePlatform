"""Overview "Adjust Quantities": a quantity matched to the broker STAYS matched.

``refresh_transactions`` re-derives every open transaction's quantity from its filled orders on every
cycle. The dialog used to write ``Transaction.quantity`` alone, so the next refresh wrote the old sum
straight back and the mismatch banner returned (prod 2026-11-24, TastyTrade O, transaction 253: three
buys summing 1.76027 against 1.70106 at the broker after a sale made outside the platform; adjusted
twice, rewritten twice). The correction is now recorded where the refresh reads it: as one FILLED
order row the platform did not place (``open_type = EXTERNAL``, no broker order id), the shape option
assignments already use.
"""
from types import SimpleNamespace

import pytest
from sqlmodel import select

from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_trading_order, create_transaction
from ba2_trade_platform.core.db import get_db, get_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    AssetClass, OrderDirection, OrderOpenType, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.services import trades
from ba2_trade_platform.services.errors import Conflict, Invalid, NotFound


def _position(fills=(0.67544, 0.99043, 0.0944), side=OrderDirection.BUY, symbol="O", **txn_kwargs):
    """An OPENED equity transaction whose entry fills sum to its quantity, as the allocator leaves it."""
    acct_def = create_account_definition()
    txn = create_transaction(symbol=symbol, quantity=round(sum(fills), 5), side=side, open_price=53.8255,
                             **txn_kwargs)
    for qty in fills:
        create_trading_order(account_id=acct_def.id, symbol=symbol, quantity=qty, side=side,
                             order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=qty,
                             open_price=53.8255, transaction_id=txn.id)
    return acct_def, txn


def _orders(txn_id):
    with get_db() as session:
        return list(session.exec(select(TradingOrder).where(TradingOrder.transaction_id == txn_id)
                                 .order_by(TradingOrder.id)).all())


def test_a_quantity_matched_to_the_broker_survives_the_next_refresh():
    acct_def, txn = _position()

    trades.reconcile_transaction_quantity(txn.id, 1.70106)
    assert get_instance(Transaction, txn.id).quantity == pytest.approx(1.70106)

    MockAccount(acct_def.id).refresh_transactions()

    fresh = get_instance(Transaction, txn.id)
    assert fresh.quantity == pytest.approx(1.70106)
    assert fresh.status == TransactionStatus.OPENED
    assert fresh.open_price == 53.8255          # the correction never becomes the entry price


def test_the_correction_is_one_filled_order_the_platform_did_not_place():
    acct_def, txn = _position()

    result = trades.reconcile_transaction_quantity(txn.id, 1.70106)

    row = _orders(txn.id)[-1]
    assert result.order_id == row.id
    assert (result.symbol, result.side) == ("O", OrderDirection.SELL)
    assert result.quantity_before == pytest.approx(1.76027)
    assert result.quantity_after == pytest.approx(1.70106)
    assert row.account_id == acct_def.id and row.symbol == "O"
    assert row.side == OrderDirection.SELL                      # fewer shares of a long: a sale
    assert row.quantity == row.filled_qty == pytest.approx(0.05921)
    assert row.status == OrderStatus.FILLED and row.order_type == OrderType.MARKET
    assert row.open_type == OrderOpenType.EXTERNAL
    assert row.broker_order_id is None                          # there is no broker order to point at
    assert row.depends_on_order is None                         # a filled dependent order reads as a TP/SL close
    assert row.open_price is None                               # the price it traded at was never measured
    assert row.asset_class == AssetClass.EQUITY
    assert row.data["quantity_reconciliation"] == {"quantity_before": pytest.approx(1.76027),
                                                   "quantity_after": pytest.approx(1.70106)}
    assert "1.70106" in row.comment and "no broker order" in row.comment


def test_a_short_position_is_reduced_by_a_buy_and_survives_the_refresh():
    acct_def, txn = _position(fills=(10.0,), side=OrderDirection.SELL, symbol="KO")

    result = trades.reconcile_transaction_quantity(txn.id, 7.5)

    assert _orders(txn.id)[-1].side == OrderDirection.BUY and result.side == OrderDirection.BUY
    MockAccount(acct_def.id).refresh_transactions()
    assert get_instance(Transaction, txn.id).quantity == pytest.approx(7.5)


def test_a_quantity_raised_above_the_fills_also_sticks():
    acct_def, txn = _position(fills=(10.0,), symbol="KO")

    trades.reconcile_transaction_quantity(txn.id, 12.0)

    row = _orders(txn.id)[-1]
    assert (row.side, row.filled_qty) == (OrderDirection.BUY, pytest.approx(2.0))
    MockAccount(acct_def.id).refresh_transactions()
    fresh = get_instance(Transaction, txn.id)
    assert fresh.quantity == pytest.approx(12.0)
    assert fresh.open_price == 53.8255          # a row without a price is never the entry


def test_a_second_correction_starts_from_the_first():
    acct_def, txn = _position(fills=(10.0,), symbol="KO")
    trades.reconcile_transaction_quantity(txn.id, 8.0)

    trades.reconcile_transaction_quantity(txn.id, 7.0)

    assert [o.filled_qty for o in _orders(txn.id)[1:]] == [pytest.approx(2.0), pytest.approx(1.0)]
    MockAccount(acct_def.id).refresh_transactions()
    assert get_instance(Transaction, txn.id).quantity == pytest.approx(7.0)


def test_the_ledger_not_the_stored_quantity_is_what_is_corrected():
    """A stored quantity that already differs from the fills (an earlier hand edit not yet refreshed)
    must not hide the correction: the refresh reads the orders, so the difference is taken from them."""
    acct_def, txn = _position(fills=(10.0,), symbol="KO")
    with get_db() as session:
        row = session.get(Transaction, txn.id)
        row.quantity = 8.0                      # what the old dialog wrote
        session.add(row)
        session.commit()

    result = trades.reconcile_transaction_quantity(txn.id, 8.0)

    assert result.order_id is not None and _orders(txn.id)[-1].filled_qty == pytest.approx(2.0)
    MockAccount(acct_def.id).refresh_transactions()
    assert get_instance(Transaction, txn.id).quantity == pytest.approx(8.0)


def test_a_quantity_already_matching_the_fills_writes_nothing():
    _acct_def, txn = _position(fills=(10.0,), symbol="KO")

    result = trades.reconcile_transaction_quantity(txn.id, 10.0)

    assert result.order_id is None and len(_orders(txn.id)) == 1


@pytest.mark.parametrize("quantity", [0, 0.0, -1.5, None, float("nan")])
def test_a_quantity_that_is_not_positive_is_refused_and_nothing_is_written(quantity):
    """Zero would balance the fills and close the transaction at an invented price: closing is Close."""
    _acct_def, txn = _position(fills=(10.0,), symbol="KO")

    with pytest.raises(Invalid):
        trades.reconcile_transaction_quantity(txn.id, quantity)

    assert len(_orders(txn.id)) == 1 and get_instance(Transaction, txn.id).quantity == 10.0


def test_a_transaction_that_is_not_open_is_refused():
    _acct_def, txn = _position(fills=(10.0,), symbol="KO", status=TransactionStatus.CLOSED)

    with pytest.raises(Conflict):
        trades.reconcile_transaction_quantity(txn.id, 8.0)

    assert len(_orders(txn.id)) == 1


def test_an_option_transaction_is_refused():
    """Its quantity is contracts, netted per contract from its legs; a share row would corrupt it."""
    _acct_def, txn = _position(fills=(2.0,), symbol="KO", asset_class=AssetClass.OPTION)

    with pytest.raises(Invalid):
        trades.reconcile_transaction_quantity(txn.id, 1.0)

    assert len(_orders(txn.id)) == 1


def test_an_unknown_transaction_is_not_found():
    with pytest.raises(NotFound):
        trades.reconcile_transaction_quantity(987654, 1.0)


# ------------------------------------------------------------------------------------------------------
# The dialog
# ------------------------------------------------------------------------------------------------------

def _dialog_stubs(monkeypatch):
    """The page's NiceGUI calls, recorded: no client exists in a unit test."""
    from ba2_trade_platform.ui.pages import overview as ov

    notes = []
    monkeypatch.setattr(ov, "ui", SimpleNamespace(
        notify=lambda message, **kwargs: notes.append((message, kwargs.get("type"))),
        timer=lambda *args, **kwargs: None,
        navigate=SimpleNamespace(reload=lambda: None)))
    return ov, notes, SimpleNamespace(closed=False, close=lambda: None)


def test_apply_changes_in_the_dialog_records_the_correction(monkeypatch):
    ov, notes, dialog = _dialog_stubs(monkeypatch)
    acct_def, txn = _position()

    ov.OverviewTab._apply_quantity_changes(SimpleNamespace(), [txn], {txn.id: SimpleNamespace(value=1.70106)},
                                           dialog)

    assert notes == [("Updated 1 transaction(s)", "positive")]
    MockAccount(acct_def.id).refresh_transactions()
    assert get_instance(Transaction, txn.id).quantity == pytest.approx(1.70106)


def test_the_dialog_reports_a_refused_quantity_and_leaves_the_rest_alone(monkeypatch):
    ov, notes, dialog = _dialog_stubs(monkeypatch)
    _acct_def, txn = _position(fills=(10.0,), symbol="KO")

    ov.OverviewTab._apply_quantity_changes(SimpleNamespace(), [txn], {txn.id: SimpleNamespace(value=0)}, dialog)

    assert len(notes) == 1 and notes[0][1] == "negative" and "KO" in notes[0][0]
    assert len(_orders(txn.id)) == 1


def test_the_dialog_shows_fractional_shares_in_full():
    """Two decimals rounded a broker's 1.70106 to 1.70 in the field; blurring it stored the rounded value."""
    import inspect
    from ba2_trade_platform.ui.pages import overview as ov

    source = inspect.getsource(ov.OverviewTab._show_quantity_correction_dialog)
    assert "format='%.2f'" not in source and "format='%.5f'" in source


# ------------------------------------------------------------------------------------------------------
# Review 2026-11-24: what the first version left open
# ------------------------------------------------------------------------------------------------------

def test_broker_fills_with_nine_decimals_are_matched_exactly():
    """Rounding the difference to 6 decimals left the fills up to 5e-7 off the quantity, and a partial
    close refuses a transaction whose fills and quantity differ by more than 1e-9."""
    acct_def, txn = _position(fills=(0.123456789, 1.000000001), symbol="KO")

    trades.reconcile_transaction_quantity(txn.id, 1.000000123)

    MockAccount(acct_def.id).refresh_transactions()
    assert get_instance(Transaction, txn.id).quantity == pytest.approx(1.000000123, abs=1e-9)


@pytest.mark.parametrize("quantity", [0.00005, float("inf"), "abc"])
def test_a_quantity_the_refresh_would_read_as_flat_or_cannot_read_is_refused(quantity):
    """Below 1e-4 the refresh calls the fills balanced and closes the transaction at the last fill price."""
    _acct_def, txn = _position(fills=(10.0,), symbol="KO")

    with pytest.raises(Invalid):
        trades.reconcile_transaction_quantity(txn.id, quantity)

    assert len(_orders(txn.id)) == 1


def test_an_oversold_transaction_is_refused_not_papered_over():
    """Its exits traded more than its entry; the refresh reports that loudly and leaves the quantity. A
    buy row for the excess would silence the report."""
    acct_def, txn = _position(fills=(4.0,), symbol="KO")
    create_trading_order(account_id=acct_def.id, symbol="KO", quantity=5.0, side=OrderDirection.SELL,
                         order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=5.0,
                         open_price=60.0, transaction_id=txn.id)

    with pytest.raises(Conflict):
        trades.reconcile_transaction_quantity(txn.id, 4.0)

    assert len(_orders(txn.id)) == 2


def test_a_working_stop_stays_working_and_no_close_reads_as_pending():
    acct_def, txn = _position(fills=(10.0,), symbol="KO")
    entry = _orders(txn.id)[0]
    stop = create_trading_order(account_id=acct_def.id, symbol="KO", quantity=10.0, side=OrderDirection.SELL,
                                order_type=OrderType.SELL_STOP, status=OrderStatus.NEW, stop_price=40.0,
                                broker_order_id="b-stop", depends_on_order=entry.id, transaction_id=txn.id)

    trades.reconcile_transaction_quantity(txn.id, 8.0)

    account = MockAccount(acct_def.id)
    account.refresh_transactions()
    fresh = get_instance(Transaction, txn.id)
    assert (fresh.status, fresh.quantity, fresh.close_price) == (TransactionStatus.OPENED, 8.0, None)
    assert get_instance(TradingOrder, stop.id).status == OrderStatus.NEW
    assert account.has_pending_closing_order(txn.id) is False


def test_the_dialog_leaves_a_transaction_the_user_did_not_edit_alone(monkeypatch):
    """An untouched field still holds the quantity read when the dialog opened; reconciling it against the
    orders would book a row nobody asked for (an oversold transaction, a fill that landed meanwhile)."""
    ov, notes, dialog = _dialog_stubs(monkeypatch)
    acct_def, edited = _position(fills=(10.0,), symbol="KO")
    untouched = create_transaction(symbol="KO", quantity=6.0, side=OrderDirection.BUY, open_price=50.0)
    create_trading_order(account_id=acct_def.id, symbol="KO", quantity=9.0, side=OrderDirection.BUY,
                         order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=9.0,
                         open_price=50.0, transaction_id=untouched.id)

    ov.OverviewTab._apply_quantity_changes(
        SimpleNamespace(), [edited, untouched],
        {edited.id: SimpleNamespace(value=8.0), untouched.id: SimpleNamespace(value=6.0)}, dialog)

    assert notes == [("Updated 1 transaction(s)", "positive")]
    assert len(_orders(edited.id)) == 2 and len(_orders(untouched.id)) == 1


def test_the_dialog_applies_the_rest_when_one_quantity_is_refused(monkeypatch):
    ov, notes, dialog = _dialog_stubs(monkeypatch)
    closed = []
    dialog.close = lambda: closed.append(True)
    acct_def, good = _position(fills=(10.0,), symbol="KO")
    _acct_def2, bad = _position(fills=(5.0,), symbol="PEP")

    ov.OverviewTab._apply_quantity_changes(
        SimpleNamespace(), [bad, good],
        {bad.id: SimpleNamespace(value=0), good.id: SimpleNamespace(value=8.0)}, dialog)

    assert [kind for _, kind in notes] == ["negative", "positive"] and "PEP" in notes[0][0]
    assert notes[1][0] == "Updated 1 transaction(s)"
    assert not closed                                       # stays open: one quantity still needs fixing
    assert len(_orders(good.id)) == 2 and len(_orders(bad.id)) == 1


def test_alpaca_never_maps_a_broker_order_onto_the_correction(monkeypatch):
    """Alpaca matches a broker order to the row whose id is its client_order_id. SQLite reuses row ids, so
    an OLD broker order can carry the id a correction row was given; adopting it would overwrite the
    recorded shares with that order's status and fill."""
    import importlib
    from datetime import datetime, timezone

    alpaca_mod = importlib.import_module("ba2_trade_platform.modules.accounts.AlpacaAccount")
    acct_def, txn = _position(fills=(10.0,), symbol="KO")
    row_id = trades.reconcile_transaction_quantity(txn.id, 8.0).order_id
    stale = SimpleNamespace(
        id="b-old", client_order_id=str(row_id), symbol="KO", qty="2", side="sell", type="market",
        order_class=None, status="canceled", filled_qty="0", filled_avg_price=None, limit_price=None,
        stop_price=None, created_at=datetime.now(timezone.utc), legs=None)
    acct = object.__new__(alpaca_mod.AlpacaAccount)
    acct.id = acct_def.id
    monkeypatch.setattr(acct, "_fetch_raw_alpaca_orders", lambda *a, **k: [stale])
    monkeypatch.setattr(acct, "_check_and_submit_dependent_orders", lambda: 0)

    assert acct.refresh_orders() is True

    row = get_instance(TradingOrder, row_id)
    assert (row.status, row.filled_qty, row.broker_order_id) == (OrderStatus.FILLED, 2.0, None)


def test_the_allocator_does_not_read_the_correction_as_its_own_sale():
    from ba2_trade_platform.core import allocator_protection_service as protection

    acct_def, txn = _position(fills=(10.0,), symbol="KO")
    trades.reconcile_transaction_quantity(txn.id, 8.0)

    assert protection._sale_outcome(acct_def.id, "KO", None) == "none"
