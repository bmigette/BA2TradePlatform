"""A partial called-away must SPLIT the share lot, not erase it.

LIVE MONEY BUG (this file's reason to exist). ``AlpacaAccount._apply_option_activity``
handled an OPASN on a short CALL by closing the WHOLE held equity transaction:
``_close_txn`` has no partial-close path. One assigned contract against a 300-share
transaction therefore closed all 300 — 200 real shares, still sitting at the broker,
vanished from the ledger. Writing one covered call against a 300-share lot is an
ordinary thing to do, so this is not an exotic path. It was reported as a warning and
nothing else (``test_wheel_assignment_order.test_called_away_lot_mismatch_is_reported``
pinned that old behaviour and is updated alongside this file).

WHAT THE SPLIT MUST PRODUCE, for a 300-share lot with 1 contract called away at 160
against a 140 basis:

  * a CLOSED transaction of 100 shares, ``close_price`` = the STRIKE (160) and
    ``open_price`` = the ORIGINAL basis (140) -- realized P&L $2,000, not $6,000;
  * an OPEN transaction of 200 shares carrying the original ``open_price``,
    ``open_date``, ``expert_id`` and ``meta_data`` (``origin=csp_assignment``, or the
    remainder stops being wheel stock), plus its own filled entry ORDER -- share counts
    are read off orders, not off ``Transaction.quantity``
    (``_OptionEntryAction._held_equity_shares``, ``Transaction.get_current_open_qty``,
    ``AccountInterface.close_transaction``), so a remainder with no order row is
    invisible stock: it cannot be covered-called and a later close would sell 0.

Everything runs through the reconciler's public entry point
(``reconcile_option_assignments``) with CANNED activity dicts -- no network, no broker
client (the AlpacaAccount is built via ``__new__``, as tests/test_option_assignment.py
does it).

TIME. No assertion derives from the wall clock: the lot's ``open_date`` is a fixed 2026
timestamp written into the row, and the days-opened assertion pins both ends.
"""
from datetime import date, datetime, timezone

import pytest
from sqlmodel import select

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.db import add_instance, get_db, get_instance
from ba2_trade_platform.core.models import Transaction, TradingOrder
from ba2_trade_platform.core.TradeActions import create_action
from ba2_trade_platform.core.utils import calculate_transaction_pnl
from ba2_trade_platform.core.types import (
    AssetClass, ExpertActionType, OptionRight, OrderDirection, OrderOpenType,
    OrderRecommendation, OrderStatus, OrderType, TransactionStatus,
    TXN_ORIGIN_CSP_ASSIGNMENT,
)


PUT_OCC = "AAPL260116P00140000"          # the CSP that put the shares to us, strike 140
BASIS = 140.0                            # what the shares cost
STRIKE = 160.0                           # what the called-away shares leave at
OPENED_ON = datetime(2026, 2, 3, 14, 30, tzinfo=timezone.utc)


def _call_occ(strike: float) -> str:
    """OCC symbol for an AAPL call expiring 2026-01-16 at ``strike``."""
    return f"AAPL260116C{int(round(strike * 1000)):08d}"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def _make_alpaca(account_id):
    """An AlpacaAccount that can run the DB-backed reconciler with no network."""
    acct = AlpacaAccount.__new__(AlpacaAccount)
    acct.id = account_id
    acct._settings_cache = {"api_key": "k", "api_secret": "s",
                            "paper_account": True, "data_feed": "iex"}
    return acct


def _capture_errors(monkeypatch):
    """Collect ``logger.error`` messages from the AlpacaAccount module. NOT caplog.

    ba2_trade_platform.logger installs its own handler with propagate=False, so caplog's
    root handler never sees the record. The package __init__ rebinds the name
    ``AlpacaAccount`` to the CLASS, so the module has to come out of sys.modules.
    """
    import sys
    module = sys.modules["ba2_trade_platform.modules.accounts.AlpacaAccount"]
    messages = []
    monkeypatch.setattr(module.logger, "error",
                        lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def _capture_warnings(monkeypatch):
    """Collect ``logger.warning`` messages from the AlpacaAccount module. NOT caplog."""
    import sys
    module = sys.modules["ba2_trade_platform.modules.accounts.AlpacaAccount"]
    messages = []
    monkeypatch.setattr(module.logger, "warning",
                        lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def _assigned_equity_txn(account_id, expert_id, *, symbol="AAPL", shares=300.0,
                         basis=BASIS, opened=OPENED_ON):
    """The equity lot a cash-secured put assignment put to us.

    Shaped exactly as ``_apply_option_activity``'s csp_assignment branch shapes it: an
    OPENED equity BUY Transaction plus the filled EXTERNAL entry order that makes the
    shares visible to every order-driven consumer.
    """
    txn_id = add_instance(Transaction(
        symbol=symbol, quantity=shares, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=basis, open_date=opened,
        expert_id=expert_id, asset_class=AssetClass.EQUITY,
        meta_data={"origin": TXN_ORIGIN_CSP_ASSIGNMENT,
                   "activity_id": "act-seed-csp", "contract": PUT_OCC},
    ))
    add_instance(TradingOrder(
        account_id=account_id, symbol=symbol, quantity=shares, filled_qty=shares,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, open_price=basis, transaction_id=txn_id,
        asset_class=AssetClass.EQUITY, open_type=OrderOpenType.EXTERNAL,
        broker_order_id=None, created_at=opened,
        comment=f"csp_assignment: option assignment of {PUT_OCC}",
    ))
    return txn_id


def _reconcile_called_away(acct, account_id, expert_id, *, contracts=1, strike=STRIKE,
                           activity_id="act-called-away", underlying="AAPL"):
    """Seed the short call the shares were written against, then assign it."""
    occ = _call_occ(strike)
    opt_txn_id = add_instance(Transaction(
        symbol=occ, quantity=contracts, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, open_price=2.5,
        open_date=datetime(2026, 2, 10, tzinfo=timezone.utc), expert_id=expert_id,
    ))
    add_instance(TradingOrder(
        account_id=account_id, symbol=occ, quantity=contracts,
        side=OrderDirection.SELL, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, filled_qty=contracts, transaction_id=opt_txn_id,
        asset_class=AssetClass.OPTION, contract_symbol=occ,
        option_type=OptionRight.CALL, strike=strike, expiry=date(2026, 1, 16),
        underlying_symbol=underlying,
    ))
    return acct.reconcile_option_assignments([{
        "id": activity_id, "activity_type": "OPASN", "symbol": occ,
        "qty": str(contracts), "price": "0",
    }])


def _txns(expert_id, status, symbol="AAPL"):
    """This expert's AAPL equity transactions in ``status`` (the session-scoped test DB
    keeps rows from earlier tests; expert_id is unique per test and isolates them)."""
    with get_db() as session:
        return session.exec(
            select(Transaction)
            .where(Transaction.symbol == symbol)
            .where(Transaction.expert_id == expert_id)
            .where(Transaction.status == status)
        ).all()


def _closed_txns(expert_id, symbol="AAPL"):
    return _txns(expert_id, TransactionStatus.CLOSED, symbol)


def _open_txns(expert_id, symbol="AAPL"):
    return _txns(expert_id, TransactionStatus.OPENED, symbol)


def _orders_for(txn_id):
    with get_db() as session:
        return session.exec(
            select(TradingOrder).where(TradingOrder.transaction_id == txn_id)
        ).all()


# ---------------------------------------------------------------------------
# 1. THE HEADLINE BUG
# ---------------------------------------------------------------------------
def test_a_one_contract_called_away_leaves_the_other_200_shares_on_the_book(
        mock_account_def, mock_expert_instance):
    """300 shares, 1 contract called away. 100 leave, 200 STAY. Today all 300 vanish."""
    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=300.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE)

    closed = _closed_txns(mock_expert_instance.id)
    remaining = _open_txns(mock_expert_instance.id)
    assert sum(t.quantity for t in closed) == 100.0
    assert sum(t.quantity for t in remaining) == 200.0, (
        "200 shares are still at the broker")


def test_the_shares_that_left_are_the_100_called_away_not_the_200_kept(
        mock_account_def, mock_expert_instance):
    """The split must not be inverted: 100 out at the strike, 200 kept at the basis.

    Closing 200 and keeping 100 balances the share count and still passes a test that
    only sums quantities, while selling 100 shares the broker never took.
    """
    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=300.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE)

    closed = _closed_txns(mock_expert_instance.id)
    remaining = _open_txns(mock_expert_instance.id)
    assert len(closed) == 1 and len(remaining) == 1
    assert closed[0].quantity == 100.0
    assert remaining[0].quantity == 200.0
    # The SELL that took the shares off the book states the called-away size only.
    exits = [o for o in _orders_for(closed[0].id) if o.side == OrderDirection.SELL]
    assert len(exits) == 1
    assert exits[0].filled_qty == 100.0
    assert exits[0].open_price == pytest.approx(STRIKE, abs=0.005)   # the STRIKE
    assert exits[0].asset_class == AssetClass.EQUITY
    assert exits[0].open_type == OrderOpenType.EXTERNAL
    assert exits[0].broker_order_id is None


# ---------------------------------------------------------------------------
# 2. WHAT THE CLOSED LOT CARRIES — the realized P&L must be of 100 shares
# ---------------------------------------------------------------------------
def test_the_closed_lot_reports_the_p_and_l_of_100_shares_at_the_strike(
        mock_account_def, mock_expert_instance):
    """(160 - 140) * 100 = $2,000. Closing the whole lot reports $6,000 of profit the
    account never made; a closed lot that loses its basis reports something else again."""
    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=300.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE)

    closed = _closed_txns(mock_expert_instance.id)[0]
    assert closed.quantity == 100.0
    assert closed.close_reason == "called_away"
    assert closed.close_price == pytest.approx(STRIKE, abs=0.005)
    assert closed.open_price == pytest.approx(BASIS, abs=0.005)      # the ORIGINAL basis
    assert closed.side == OrderDirection.BUY
    assert closed.expert_id == mock_expert_instance.id
    assert calculate_transaction_pnl(closed) == pytest.approx(2000.0, abs=0.005)


# ---------------------------------------------------------------------------
# 3. WHAT THE REMAINDER CARRIES
# ---------------------------------------------------------------------------
def test_the_remainder_keeps_the_basis_the_open_date_the_expert_and_the_origin(
        mock_account_def, mock_expert_instance):
    """A remainder that loses any of these is a different position than the one held.

    ``open_price`` -> fabricated P&L (a remainder marked at the strike shows 0 profit
    forever, one with no basis shows None); ``open_date`` -> DaysOpenedCondition resets
    the clock and a 90-day exit never fires; ``expert_id`` -> nobody owns the shares;
    ``meta_data.origin`` -> the remainder stops being wheel stock.
    """
    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=300.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE)

    remainder = _open_txns(mock_expert_instance.id)[0]
    assert remainder.quantity == 200.0
    assert remainder.symbol == "AAPL"
    assert remainder.side == OrderDirection.BUY
    assert remainder.status == TransactionStatus.OPENED
    assert remainder.open_price == pytest.approx(BASIS, abs=0.005)
    assert remainder.open_date is not None
    assert remainder.open_date.replace(tzinfo=timezone.utc) == OPENED_ON
    assert remainder.expert_id == mock_expert_instance.id
    assert remainder.asset_class == AssetClass.EQUITY
    assert remainder.meta_data is not None
    assert remainder.meta_data.get("origin") == TXN_ORIGIN_CSP_ASSIGNMENT
    assert remainder.close_reason is None and remainder.close_price is None
    assert remainder.close_date is None


def test_the_two_halves_of_the_split_point_at_each_other(mock_account_def,
                                                         mock_expert_instance):
    """One position became two rows; each has to say so, or the pair is unauditable.

    Nothing else in the ledger records that a 300-share lot became a 100 and a 200:
    without the link, a reviewer sees a lot that shrank for no reason and a lot that
    appeared from nowhere, and cannot tell either from a book-keeping error.
    """
    acct = _make_alpaca(mock_account_def.id)
    held_id = _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id,
                                   shares=300.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE, activity_id="act-provenance")

    remainder = _open_txns(mock_expert_instance.id)[0]
    closed = get_instance(Transaction, held_id)

    assert remainder.meta_data.get("split_from_transaction_id") == held_id
    assert remainder.meta_data.get("split_reason") == "called_away"
    assert remainder.meta_data.get("split_activity_id") == "act-provenance"

    split = closed.meta_data.get("called_away_split")
    assert split is not None, "the closed lot does not record that it was split"
    assert split["remainder_transaction_id"] == remainder.id
    assert split["original_quantity"] == 300.0
    assert split["shares_called_away"] == 100.0
    assert split["shares_remaining"] == 200.0
    assert split["activity_id"] == "act-provenance"


def test_a_strike_with_cents_is_not_rounded_away(mock_account_def, mock_expert_instance):
    """Half-dollar strikes are ordinary. 157.50, not 157 and not 158.

    The exit price of the called shares IS the strike; rounding it to the dollar puts
    $50 per contract of fabricated P&L into the books.
    """
    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=300.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=157.5, activity_id="act-cents")

    closed = _closed_txns(mock_expert_instance.id)[0]
    assert closed.close_price == pytest.approx(157.5, abs=0.005)
    assert calculate_transaction_pnl(closed) == pytest.approx(1750.0, abs=0.005)
    exits = [o for o in _orders_for(closed.id) if o.side == OrderDirection.SELL]
    assert exits[0].open_price == pytest.approx(157.5, abs=0.005)
    # ...and the shares that stayed are unaffected by the strike.
    remainder = _open_txns(mock_expert_instance.id)[0]
    assert remainder.quantity == 200.0
    assert remainder.open_price == pytest.approx(BASIS, abs=0.005)


def test_only_one_lot_is_touched_when_the_expert_holds_several(mock_account_def,
                                                               mock_expert_instance):
    """Exactly 100 shares leave the book in total, out of one lot -- not out of each.

    ``_find_open_equity_long`` picks a single lot; an assignment must not ripple through
    every open lot the expert holds in the name.
    """
    acct = _make_alpaca(mock_account_def.id)
    first_id = _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id,
                                    shares=100.0)
    second_id = _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id,
                                     shares=300.0)
    assert sum(t.quantity for t in _open_txns(mock_expert_instance.id)) == 400.0

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE, activity_id="act-two-lots")

    assert sum(t.quantity for t in _open_txns(mock_expert_instance.id)) == 300.0
    assert sum(t.quantity for t in _closed_txns(mock_expert_instance.id)) == 100.0
    # The lot the assignment did not touch is untouched, in full.
    untouched = get_instance(Transaction, first_id)
    assert untouched.status == TransactionStatus.OPENED and untouched.quantity == 100.0
    assert get_instance(Transaction, second_id).status == TransactionStatus.CLOSED


def test_the_remaining_200_shares_are_still_visible_as_shares(
        mock_account_def, mock_expert_instance):
    """Share counts are read off ORDER rows, never off ``Transaction.quantity``.

    A remainder transaction with no filled entry order is invisible stock: the wheel's
    covered-call leg sizes it to 0 contracts, ``get_current_open_qty`` is 0, and
    ``AccountInterface.close_transaction`` would try to sell nothing.
    """
    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=300.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE)

    remainder = _open_txns(mock_expert_instance.id)[0]
    entries = [o for o in _orders_for(remainder.id) if o.side == OrderDirection.BUY]
    assert len(entries) == 1, "the remaining shares have no order row to be counted from"
    entry = entries[0]
    assert entry.filled_qty == 200.0 and entry.quantity == 200.0
    assert entry.status == OrderStatus.FILLED
    assert entry.open_price == pytest.approx(BASIS, abs=0.005)   # the basis, not the strike
    assert entry.asset_class == AssetClass.EQUITY
    assert entry.symbol == "AAPL"
    assert entry.account_id == mock_account_def.id
    assert entry.depends_on_order is None            # this IS the entry, not a close
    assert entry.open_type == OrderOpenType.EXTERNAL  # book-keeping, not a trade we placed
    assert entry.broker_order_id is None              # nothing at the broker to reconcile
    # Dated when the shares were BOUGHT, not when the call was assigned: this row stands
    # in for the un-called part of the original purchase, and it is DaysOpenedCondition's
    # fallback when a transaction has no open_date.
    assert entry.created_at.replace(tzinfo=timezone.utc) == OPENED_ON
    # ...and the transaction's own order-derived size agrees with its quantity.
    assert remainder.get_current_open_qty() == 200.0


def test_the_remainder_can_still_be_covered_called(monkeypatch, mock_account,
                                                   mock_expert_instance,
                                                   sample_recommendation):
    """The point of keeping the shares: the wheel can write 2 more calls over the 200."""
    acct = _make_alpaca(mock_account.id)
    _assigned_equity_txn(mock_account.id, mock_expert_instance.id, shares=300.0)

    _reconcile_called_away(acct, mock_account.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE)

    action = create_action(
        action_type=ExpertActionType.SELL_COVERED_CALL, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.HOLD,
        existing_order=None, expert_recommendation=sample_recommendation,
        strike_method="delta", strike_param=0.30, dte_min=20, dte_max=45,
        min_open_interest=100, max_spread_pct=20.0)

    assert action._held_equity_shares() == 200.0, (
        "the 200 shares left on the book cannot size a covered call")


def test_days_opened_on_the_remainder_counts_from_the_original_purchase(
        mock_account, mock_expert_instance, sample_recommendation):
    """DaysOpenedCondition reads the transaction's ``open_date`` through the entry order.

    Stamping the remainder with "now" would restart every time-based exit at zero on the
    day of an assignment that had nothing to do with those shares.
    """
    from ba2_trade_platform.core.TradeConditions import create_condition
    from ba2_trade_platform.core.TradeManager import resolve_entry_order
    from ba2_trade_platform.core.types import ExpertEventType

    acct = _make_alpaca(mock_account.id)
    _assigned_equity_txn(mock_account.id, mock_expert_instance.id, shares=300.0)
    _reconcile_called_away(acct, mock_account.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE)

    remainder = _open_txns(mock_expert_instance.id)[0]
    with get_db() as session:
        entry = resolve_entry_order(session, remainder)
    assert entry is not None, "TradeManager cannot resolve an entry order for the remainder"

    # Both ends pinned: opened 2026-02-03, "now" 2026-02-23 -> 20 days (minus 14:30).
    sample_recommendation.created_at = datetime(2026, 2, 23, 14, 30, tzinfo=timezone.utc)
    cond = create_condition(ExpertEventType.N_DAYS_OPENED, mock_account, "AAPL",
                            sample_recommendation, entry, ">=", 15.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# 4. THE EXACT MATCH — no zero-quantity ghost row
# ---------------------------------------------------------------------------
def test_an_exact_match_closes_the_lot_and_leaves_no_remainder_row(
        mock_account_def, mock_expert_instance):
    """100 shares, 1 contract: the whole lot goes, and NO 0-share transaction is born.

    A zero-quantity OPENED row is worse than nothing: it reads as an open position with
    no shares, ``has_assigned_shares`` stays True forever and every manage pass revisits
    a position that does not exist.
    """
    acct = _make_alpaca(mock_account_def.id)
    held_id = _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id,
                                   shares=100.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE)

    held = get_instance(Transaction, held_id)
    assert held.status == TransactionStatus.CLOSED
    assert held.quantity == 100.0
    assert held.close_price == pytest.approx(STRIKE, abs=0.005)
    assert _open_txns(mock_expert_instance.id) == []
    assert [t.id for t in _closed_txns(mock_expert_instance.id)] == [held_id], (
        "an exact match must not create a second transaction row")


def test_an_exact_match_creates_no_zero_quantity_transaction_anywhere(
        mock_account_def, mock_expert_instance):
    """Belt and braces: no row of any status with quantity 0 for this expert."""
    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=100.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE)

    with get_db() as session:
        rows = session.exec(
            select(Transaction)
            .where(Transaction.expert_id == mock_expert_instance.id)
            .where(Transaction.symbol == "AAPL")
        ).all()
    assert [r.quantity for r in rows] == [100.0]
    zero_orders = [o for r in rows for o in _orders_for(r.id)
                   if (o.filled_qty or 0.0) == 0.0]
    assert zero_orders == []


# ---------------------------------------------------------------------------
# 5. LOT SHAPES — more than one contract, odd lots, fractional shares
# ---------------------------------------------------------------------------
def test_two_contracts_called_away_take_200_and_leave_100(mock_account_def,
                                                          mock_expert_instance):
    """One activity can carry several contracts; the split is 100 x contracts."""
    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=300.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=2, strike=STRIKE)

    closed = _closed_txns(mock_expert_instance.id)
    remaining = _open_txns(mock_expert_instance.id)
    assert sum(t.quantity for t in closed) == 200.0
    assert sum(t.quantity for t in remaining) == 100.0
    exits = [o for o in _orders_for(closed[0].id) if o.side == OrderDirection.SELL]
    assert exits[0].filled_qty == 200.0


def test_an_odd_lot_keeps_its_odd_shares(mock_account_def, mock_expert_instance):
    """250 shares, 1 contract: 100 leave and 150 stay. Nothing is rounded to a lot."""
    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=250.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE)

    assert sum(t.quantity for t in _closed_txns(mock_expert_instance.id)) == 100.0
    assert sum(t.quantity for t in _open_txns(mock_expert_instance.id)) == 150.0


def test_a_fractional_lot_keeps_its_fraction(mock_account_def, mock_expert_instance):
    """Alpaca does fractional shares. 150.5 held, 1 contract -> 50.5 stay, not 50 or 0."""
    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=150.5)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE)

    assert sum(t.quantity for t in _closed_txns(mock_expert_instance.id)) == 100.0
    remaining = _open_txns(mock_expert_instance.id)
    assert sum(t.quantity for t in remaining) == pytest.approx(50.5, abs=1e-9)
    entries = [o for o in _orders_for(remaining[0].id) if o.side == OrderDirection.BUY]
    assert entries[0].filled_qty == pytest.approx(50.5, abs=1e-9)


# ---------------------------------------------------------------------------
# 6. OVER-ASSIGNMENT — more called away than this lot holds
# ---------------------------------------------------------------------------
def test_more_contracts_than_shares_never_mints_a_negative_remainder(
        monkeypatch, mock_account_def, mock_expert_instance):
    """100 shares, 2 contracts assigned. This should be impossible; say what it does.

    A blind ``held - called`` writes a -100-share OPEN transaction (a phantom short that
    every sizing and P&L path then reads), and a 200-share exit order against a lot that
    only ever held 100 drives ``get_current_open_qty`` to -100, which
    ``close_transaction`` would try to BUY back.
    """
    errors = _capture_errors(monkeypatch)
    acct = _make_alpaca(mock_account_def.id)
    held_id = _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id,
                                   shares=100.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=2, strike=STRIKE)

    held = get_instance(Transaction, held_id)
    assert held.status == TransactionStatus.CLOSED
    assert held.quantity == 100.0
    assert _open_txns(mock_expert_instance.id) == [], "a negative remainder was created"
    exits = [o for o in _orders_for(held_id) if o.side == OrderDirection.SELL]
    assert len(exits) == 1
    assert exits[0].filled_qty == 100.0, (
        "the exit order sold more shares than the lot ever held")
    assert held.get_current_open_qty() == 0.0
    assert any("over-assign" in e.lower() for e in errors), errors


def test_a_lot_that_claims_to_hold_nothing_is_closed_and_reported(
        monkeypatch, mock_account_def, mock_expert_instance):
    """An OPENED lot with quantity 0 is already broken; the split must not build on it.

    Subtracting from it (0 - 100) or capping to it (an exit order for 0 shares, a junk
    zero-fill row) would both quietly propagate the corruption. The lot is closed, the
    exit records what the broker actually took, and the state is named in the log.
    """
    errors = _capture_errors(monkeypatch)
    acct = _make_alpaca(mock_account_def.id)
    held_id = add_instance(Transaction(
        symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=BASIS, open_date=OPENED_ON,
        expert_id=mock_expert_instance.id, asset_class=AssetClass.EQUITY,
        meta_data={"origin": TXN_ORIGIN_CSP_ASSIGNMENT}))

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE, activity_id="act-empty-lot")

    assert get_instance(Transaction, held_id).status == TransactionStatus.CLOSED
    assert _open_txns(mock_expert_instance.id) == []
    exits = [o for o in _orders_for(held_id) if o.side == OrderDirection.SELL]
    assert len(exits) == 1 and exits[0].filled_qty == 100.0   # not a 0-share order row
    assert any("quantity" in e.lower() for e in errors), errors


# ---------------------------------------------------------------------------
# 7. A LEGITIMATE PARTIAL IS NOT AN ERROR, AND THE AUDIT ROW SAYS WHAT HAPPENED
# ---------------------------------------------------------------------------
def test_a_legitimate_partial_is_not_reported_as_a_mismatch(monkeypatch,
                                                            mock_account_def,
                                                            mock_expert_instance):
    """Writing one call against 300 shares is ordinary. It must not log a discrepancy."""
    warnings = _capture_warnings(monkeypatch)
    errors = _capture_errors(monkeypatch)
    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=300.0)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE)

    assert not any("mismatch" in m.lower() or "unaccounted" in m.lower()
                   for m in warnings + errors), warnings + errors


def test_the_activity_audit_row_records_the_split(mock_account_def,
                                                  mock_expert_instance):
    """``OptionActivity.result`` is the audit trail of what the reconciler did."""
    from ba2_trade_platform.core.models import OptionActivity

    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=300.0)

    results = _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                                     contracts=1, strike=STRIKE,
                                     activity_id="act-audit-split")
    assert "called_away" in results[0]["result"]

    with get_db() as session:
        row = session.exec(
            select(OptionActivity)
            .where(OptionActivity.account_id == mock_account_def.id)
            .where(OptionActivity.activity_id == "act-audit-split")
        ).first()
    assert row is not None
    assert "100" in (row.result or "") and "200" in (row.result or ""), row.result


def test_reconciling_the_same_called_away_twice_does_not_split_again(
        mock_account_def, mock_expert_instance):
    """The 7-day lookback runs every 5 minutes; a second split would erase 100 more."""
    acct = _make_alpaca(mock_account_def.id)
    _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=300.0)

    occ = _call_occ(STRIKE)
    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE, activity_id="act-twice")
    second = acct.reconcile_option_assignments([{
        "id": "act-twice", "activity_type": "OPASN", "symbol": occ,
        "qty": "1", "price": "0",
    }])

    assert second[0]["result"] == "already_processed"
    assert sum(t.quantity for t in _open_txns(mock_expert_instance.id)) == 200.0
    assert sum(t.quantity for t in _closed_txns(mock_expert_instance.id)) == 100.0


# ---------------------------------------------------------------------------
# 8. THE WHOLE WHEEL — a genuinely assigned lot, partially called away
# ---------------------------------------------------------------------------
def test_a_real_csp_assignment_of_3_contracts_survives_one_call_being_assigned(
        mock_account_def, mock_expert_instance):
    """End to end through the reconciler on both legs: no hand-built equity row.

    3 puts assigned at 140 -> 300 shares. 1 call assigned at 160 -> 100 leave, 200 stay,
    still marked as assignment stock and still carrying the 140 basis.
    """
    acct = _make_alpaca(mock_account_def.id)
    # Leg 1: the cash-secured puts are assigned.
    put_txn_id = add_instance(Transaction(
        symbol=PUT_OCC, quantity=3, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, open_price=2.0,
        open_date=datetime(2026, 1, 5, tzinfo=timezone.utc),
        expert_id=mock_expert_instance.id))
    add_instance(TradingOrder(
        account_id=mock_account_def.id, symbol=PUT_OCC, quantity=3,
        side=OrderDirection.SELL, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, filled_qty=3, transaction_id=put_txn_id,
        asset_class=AssetClass.OPTION, contract_symbol=PUT_OCC,
        option_type=OptionRight.PUT, strike=BASIS, expiry=date(2026, 1, 16),
        underlying_symbol="AAPL"))
    acct.reconcile_option_assignments([{
        "id": "act-e2e-put", "activity_type": "OPASN", "symbol": PUT_OCC,
        "qty": "3", "price": "0"}])
    assert sum(t.quantity for t in _open_txns(mock_expert_instance.id)) == 300.0

    # Leg 2: one covered call is assigned.
    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                           contracts=1, strike=STRIKE, activity_id="act-e2e-call")

    remaining = _open_txns(mock_expert_instance.id)
    assert len(remaining) == 1
    assert remaining[0].quantity == 200.0
    assert remaining[0].open_price == pytest.approx(BASIS, abs=0.005)
    assert remaining[0].meta_data.get("origin") == TXN_ORIGIN_CSP_ASSIGNMENT
    assert sum(t.quantity for t in _closed_txns(mock_expert_instance.id)) == 100.0
