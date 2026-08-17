"""FactorRanker's rebalance exit must release its own protective stop first.

`_submit_buy` attaches a stop covering the FULL position. At the broker a working stop RESERVES
those shares, so an exit for the same quantity has nothing available:

    dev order 630 (2026-08-17): SELL 11 SPCX -> 40310000 insufficient_qty
    {"available":"0","existing_qty":"11","held_for_orders":"11","related_orders":["6ba609..."]}

where 6ba609... was the broker id of FactorRanker's OWN stop (order 608). Releasing the leg must
not, in turn, leave a partial reduce unprotected -- hence the re-protect assertions.
"""
from __future__ import annotations

import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ba2_common.core.types import (  # noqa: E402
    OrderDirection, OrderStatus, OrderType,
)
from ba2_experts.FactorRanker import portfolio as P  # noqa: E402


class _Leg:
    """Stand-in for a resting protective TradingOrder."""

    def __init__(self, oid, qty, stop_price=90.0, limit_price=None,
                 order_type=OrderType.SELL_STOP, broker_order_id="brk-1", txn_id=282):
        self.id = oid
        self.quantity = qty
        self.stop_price = stop_price
        self.limit_price = limit_price
        self.order_type = order_type
        self.side = OrderDirection.SELL
        self.broker_order_id = broker_order_id
        self.transaction_id = txn_id
        self.symbol = "SPCX"
        self.status = OrderStatus.NEW


class _Txn:
    def __init__(self, tid=282, open_qty=11.0, open_price=134.19):
        self.id = tid
        self.open_qty = open_qty
        self.open_price = open_price


class _Account:
    def __init__(self):
        self.canceled = []
        self.submitted = []

    def cancel_order(self, broker_order_id):
        self.canceled.append(broker_order_id)

    def submit_order(self, order, **kw):
        self.submitted.append((order, kw))
        if getattr(order, "id", None) is None:
            order.id = 999
        return order


def _mgr(monkeypatch, legs, account=None):
    """A FactorPortfolioManager with DB access stubbed out."""
    mgr = P.FactorPortfolioManager.__new__(P.FactorPortfolioManager)
    mgr.expert_instance_id = 14
    mgr.account_id = 1
    mgr.account = account or _Account()
    monkeypatch.setattr(mgr, "_working_protective_legs", lambda txns: list(legs), raising=False)
    monkeypatch.setattr(P, "add_instance", lambda o: setattr(o, "id", 1000) or 1000)
    monkeypatch.setattr(P, "update_instance", lambda o: o)
    return mgr


# --------------------------------------------------------------- the regression (order 630)

def test_full_exit_cancels_the_protective_leg_before_selling(monkeypatch):
    leg = _Leg(608, qty=11.0, broker_order_id="6ba6094c-322")
    acct = _Account()
    mgr = _mgr(monkeypatch, [leg], acct)

    order = mgr._submit_sell("SPCX", 11, [_Txn(open_qty=11.0)])

    assert acct.canceled == ["6ba6094c-322"], "the resting stop must be cancelled"
    assert order is not None and order.side == OrderDirection.SELL
    assert order.quantity == 11


def test_the_cancel_happens_before_the_sell_is_submitted(monkeypatch):
    """Ordering is the whole point: cancelling afterwards would not free the shares in time."""
    events = []

    class _Ordered(_Account):
        def cancel_order(self, broker_order_id):
            events.append("cancel")

        def submit_order(self, order, **kw):
            events.append("submit")
            order.id = 999
            return order

    mgr = _mgr(monkeypatch, [_Leg(608, 11.0)], _Ordered())
    mgr._submit_sell("SPCX", 11, [_Txn(open_qty=11.0)])
    assert events[0] == "cancel" and "submit" in events


def test_released_leg_is_marked_canceled_in_the_db(monkeypatch):
    leg = _Leg(608, 11.0)
    mgr = _mgr(monkeypatch, [leg])
    mgr._submit_sell("SPCX", 11, [_Txn(open_qty=11.0)])
    assert leg.status == OrderStatus.CANCELED


def test_no_protective_leg_means_no_cancel_and_a_normal_sell(monkeypatch):
    """NVO on the same live rebalance had no resting stop and filled fine — keep that path clean."""
    acct = _Account()
    mgr = _mgr(monkeypatch, [], acct)
    order = mgr._submit_sell("NVO", 30, [_Txn(open_qty=30.0)])
    assert acct.canceled == []
    assert order is not None
    assert len(acct.submitted) == 1, "only the sell — nothing to re-protect"


def test_a_stuck_cancel_does_not_block_the_exit(monkeypatch):
    """One uncancellable leg must not strand the position; the sell still goes in."""
    class _Bad(_Account):
        def cancel_order(self, broker_order_id):
            raise RuntimeError("broker rejected cancel")

    acct = _Bad()
    mgr = _mgr(monkeypatch, [_Leg(608, 11.0)], acct)
    order = mgr._submit_sell("SPCX", 11, [_Txn(open_qty=11.0)])
    assert order is not None
    assert any(o.side == OrderDirection.SELL for o, _ in acct.submitted)


# --------------------------------------------------------------- not trading one bug for another

def test_partial_reduce_reprotects_the_remainder(monkeypatch):
    """Cancelling the only stop and selling PART of the position must not leave the rest naked."""
    acct = _Account()
    mgr = _mgr(monkeypatch, [_Leg(608, qty=11.0, stop_price=90.0)], acct)

    mgr._submit_sell("SPCX", 4, [_Txn(open_qty=11.0)])

    protective = [o for o, _ in acct.submitted if o.order_type == OrderType.SELL_STOP]
    assert len(protective) == 1, "the remaining 7 shares must get a stop back"
    assert protective[0].quantity == 7
    assert protective[0].stop_price == 90.0, "price is preserved, only the quantity shrinks"


def test_reprotection_waits_for_the_reduce_to_fill(monkeypatch):
    """Submitting the replacement immediately would reserve the shares the sell still needs —
    reproducing the very rejection this fix exists to avoid."""
    acct = _Account()
    mgr = _mgr(monkeypatch, [_Leg(608, qty=11.0)], acct)
    mgr._submit_sell("SPCX", 4, [_Txn(open_qty=11.0)])

    leg = [o for o, _ in acct.submitted if o.order_type == OrderType.SELL_STOP][0]
    assert leg.depends_on_order == 999, "must hang off the reducing sell order"
    assert leg.depends_order_status_trigger == OrderStatus.FILLED


def test_full_exit_does_not_reprotect(monkeypatch):
    """Nothing is left to protect; a leftover stop would be a naked short trigger."""
    acct = _Account()
    mgr = _mgr(monkeypatch, [_Leg(608, qty=11.0)], acct)
    mgr._submit_sell("SPCX", 11, [_Txn(open_qty=11.0)])
    assert not [o for o, _ in acct.submitted if o.order_type == OrderType.SELL_STOP]


def test_reprotect_failure_is_loud_but_does_not_undo_the_reduce(monkeypatch):
    """If re-protection fails the reduce must stand, and the naked position must be shouted about.

    Asserts against the logger itself rather than caplog: ba2_common.logger installs its own
    handler and does not propagate to the root, so caplog.text is empty even though the record
    is emitted -- checking caplog here would silently pass on a regression.
    """
    class _HalfBad(_Account):
        def submit_order(self, order, **kw):
            self.submitted.append((order, kw))
            if order.order_type == OrderType.SELL_STOP:
                raise RuntimeError("broker down")
            order.id = 999
            return order

    errors: list = []
    monkeypatch.setattr(P.logger, "error", lambda msg, *a, **k: errors.append(str(msg)))

    acct = _HalfBad()
    mgr = _mgr(monkeypatch, [_Leg(608, qty=11.0)], acct)
    order = mgr._submit_sell("SPCX", 4, [_Txn(open_qty=11.0)])

    assert order is not None, "the reduce itself must still stand"
    assert any("UNPROTECTED" in e for e in errors), errors


# --------------------------------------------------------------- guards

@pytest.mark.parametrize("qty,txns", [(0, [_Txn()]), (-3, [_Txn()]), (5, [])])
def test_degenerate_inputs_submit_nothing(monkeypatch, qty, txns):
    acct = _Account()
    mgr = _mgr(monkeypatch, [_Leg(608, 11.0)], acct)
    assert mgr._submit_sell("SPCX", qty, txns) is None
    assert acct.canceled == [] and acct.submitted == []


def test_protective_types_cover_both_sides():
    """A short position's protective legs are BUYs; the lookup must not be long-only."""
    t = P.FactorPortfolioManager._PROTECTIVE_ORDER_TYPES
    for expected in (OrderType.SELL_STOP, OrderType.SELL_LIMIT,
                     OrderType.BUY_STOP, OrderType.BUY_LIMIT):
        assert expected in t
