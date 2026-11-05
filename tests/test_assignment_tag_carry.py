"""LIVE: the stock an assignment delivers inherits the assigned option position's MEMBER TAG.

Plan 2026-09-25 option-stage2-build, Task E2. A style expert scopes each member's rules with an
``entry_tag == <member>`` leaf, read off the position's entry order. The wheel member's
covered-call overlay must write calls ONLY on the shares the wheel member was put -- never on a
CSP member's -- which works only if the assignment's stock entry order carries the tag of the
put that delivered it. The backtest stamps it in ``BacktestAccount._open_assigned_stock_
transaction``; live must stamp the same fact through the same shared function
(``assignment_hold.assigned_stock_entry_data``), or the overlay fires in one runtime and not in
the other.

The HOLD/LIQUIDATE decision itself has no live counterpart: live never sells assigned stock
(``deploy_parity`` ``hold_assigned_stock``: ``why_no_live_analogue``). Only the tag is carried.

Harness: the reconciler's public entry point with canned activities, no network (as
tests/test_wheel_assignment_order.py and tests/test_called_away_partial.py do it).
"""
from ba2_trade_platform.core.db import get_db, update_instance
from ba2_common.core.entry_tag import order_entry_tag
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OptionRight, OrderDirection, TransactionStatus
from sqlmodel import select

from tests.test_called_away_partial import (
    _assigned_equity_txn, _open_txns, _orders_for as _orders_of, _reconcile_called_away)
from tests.test_wheel_assignment_order import (
    PUT_OCC, _assign, _equity_txns, _make_alpaca, _orders_for, _seed_short_option)

WHEEL = 7


def _tag_the_option_entry(opt_txn_id, tag):
    """Stamp ``tag`` on the option position's entry row, as ``_OptionEntryAction`` does."""
    with get_db() as session:
        (order,) = session.exec(
            select(TradingOrder).where(TradingOrder.transaction_id == opt_txn_id)).all()
    order.data = {**(order.data or {}), "entry_tag": tag}
    update_instance(order)


def test_the_assigned_stock_entry_order_carries_the_puts_member_tag(mock_account,
                                                                    mock_expert_instance):
    acct = _make_alpaca(mock_account.id)
    opt_txn_id = _seed_short_option(mock_account.id, mock_expert_instance.id, PUT_OCC,
                                    OptionRight.PUT, strike=150.0, contracts=1)
    _tag_the_option_entry(opt_txn_id, WHEEL)

    _assign(acct, PUT_OCC, 1, "act-tag-carry")

    (txn,) = _equity_txns(mock_expert_instance.id)
    (entry,) = _orders_for(txn.id)
    assert entry.side == OrderDirection.BUY
    assert order_entry_tag(entry) == WHEEL, entry.data


def test_an_untagged_put_delivers_stock_with_NO_order_data(mock_account, mock_expert_instance):
    """Every live position on record is untagged: its assignment row must stay byte-identical."""
    acct = _make_alpaca(mock_account.id)
    _seed_short_option(mock_account.id, mock_expert_instance.id, PUT_OCC,
                       OptionRight.PUT, strike=150.0, contracts=1)
    _assign(acct, PUT_OCC, 1, "act-no-tag")

    (txn,) = _equity_txns(mock_expert_instance.id)
    (entry,) = _orders_for(txn.id)
    assert entry.data is None


def test_a_partial_called_away_remainder_keeps_the_lots_member_tag(mock_account_def,
                                                                  mock_expert_instance):
    """The remainder of a partially called-away lot is a NEW transaction with a NEW entry order;
    it is still the same member's stock, so it keeps the tag (the backtest never splits the
    lot, so its entry order -- and tag -- simply stays)."""
    acct = _make_alpaca(mock_account_def.id)
    lot_id = _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=300.0)
    _tag_the_option_entry(lot_id, WHEEL)

    _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id, contracts=1)

    (remainder,) = _open_txns(mock_expert_instance.id)
    assert remainder.id != lot_id and remainder.status == TransactionStatus.OPENED
    entries = [o for o in _orders_of(remainder.id) if o.depends_on_order is None]
    assert len(entries) == 1
    assert order_entry_tag(entries[0]) == WHEEL, entries[0].data


# --------------------------------------------------------------------------------------------- #
# review M1: a CORRUPT stored tag is a retryable refusal, read before any ledger write
# --------------------------------------------------------------------------------------------- #
def _activity_row(account_id, activity_id):
    from ba2_trade_platform.core.models import OptionActivity
    with get_db() as session:
        return session.exec(select(OptionActivity)
                            .where(OptionActivity.account_id == account_id)
                            .where(OptionActivity.activity_id == activity_id)).first()


def test_a_corrupt_tag_on_the_assigned_put_is_a_RETRYABLE_refusal_with_no_ledger_write(
        mock_account, mock_expert_instance):
    acct = _make_alpaca(mock_account.id)
    opt_txn_id = _seed_short_option(mock_account.id, mock_expert_instance.id, PUT_OCC,
                                    OptionRight.PUT, strike=150.0, contracts=1)
    _tag_the_option_entry(opt_txn_id, "seven")        # nothing an entry could have written

    (result,) = _assign(acct, PUT_OCC, 1, "act-corrupt")
    assert result["result"].startswith("unhandled:"), result
    assert not result["result"].startswith("unhandled: exception:"), (
        f"a corrupt tag must be RETRYABLE once the row is repaired: {result}")
    assert "entry_tag" in result["result"]
    assert _equity_txns(mock_expert_instance.id) == [], "a ledger write happened anyway"

    # repaired -> the next pass applies it
    _tag_the_option_entry(opt_txn_id, WHEEL)
    (again,) = _assign(acct, PUT_OCC, 1, "act-corrupt")
    assert again["result"].startswith("csp_assignment"), again
    (txn,) = _equity_txns(mock_expert_instance.id)
    (entry,) = _orders_for(txn.id)
    assert order_entry_tag(entry) == WHEEL


def test_a_corrupt_tag_on_a_partially_called_away_lot_is_a_RETRYABLE_refusal(
        mock_account_def, mock_expert_instance):
    acct = _make_alpaca(mock_account_def.id)
    lot_id = _assigned_equity_txn(mock_account_def.id, mock_expert_instance.id, shares=300.0)
    _tag_the_option_entry(lot_id, -4)

    (result,) = _reconcile_called_away(acct, mock_account_def.id, mock_expert_instance.id,
                                       contracts=1)
    assert result["result"].startswith("unhandled:"), result
    assert not result["result"].startswith("unhandled: exception:"), result
    (still,) = _open_txns(mock_expert_instance.id)
    assert still.id == lot_id and float(still.quantity) == 300.0, "the lot was split anyway"
