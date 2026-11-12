"""Assignment liquidation pays an equity market fill's costs (review 2026-11-09).

The next-bar sale (or buy-back) of assigned stock pays the costs of an ordinary equity
MARKET fill of that side -- ``_slip`` (slippage + half the spread, adverse) and
``commission_per_trade`` -- and the trade row charges that commission too. With every cost
at zero the result is the old one.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from ba2_common.core.types import OptionRight, OrderDirection

from tests.backtest.test_option_orphan_stock_and_arb_guards import (
    CFG, _CALL105, _EXP_0308, _EXP_0315, _PUT180,
    _bar_row, _build, _chain_row, _engine, _leg, _submit_call,
)


def _expire(acct, ps, day: datetime):
    ps.set_clock(day)
    _engine(acct, ps)._apply_option_expiry(day)


_COSTS = {"slippage_bps": 10.0, "spread_bps": 20.0, "commission_per_trade": 1.5}
#: slippage 10 bps + half of a 20 bps spread = 20 bps, adverse.
_ADVERSE = (10.0 + 20.0 / 2.0) / 10_000.0


def _equity_rows(acct):
    return [t for t in acct.get_round_trip_trades()
            if t["symbol"] == "AAPL" and t["contract_symbol"] is None]


def _assigned_short_put(tmp_path, name, cfg):
    acct, ps, ctx = _build(
        tmp_path, name,
        [_chain_row(_PUT180, "put", 180.0, _EXP_0308)],
        [_bar_row(_PUT180, "2024-03-06", 21.0, "put", 180.0, _EXP_0308)],
        cfg=cfg,
    )
    acct.submit_option_order(
        legs=[_leg(_PUT180, OrderDirection.SELL, "sell_to_open",
                   OptionRight.PUT, 180.0, _EXP_0308)],
        quantity=1, order_type="market", option_strategy="naked_put")
    acct.refresh_orders()
    acct.refresh_transactions()
    _expire(acct, ps, datetime(2024, 3, 8))
    acct.refresh_transactions()
    assert acct._pending_assignment_sells == {"AAPL": 100.0}
    return acct, ps, ctx


def _assigned_short_call(tmp_path, name, cfg):
    acct, ps, ctx = _build(
        tmp_path, name,
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315)],
        cfg=cfg,
    )
    _submit_call(acct, side=OrderDirection.SELL, intent="sell_to_open", strategy="naked_call")
    acct.refresh_orders()
    acct.refresh_transactions()
    _expire(acct, ps, datetime(2024, 3, 15))
    acct.refresh_transactions()
    assert acct._pending_assignment_sells == {"AAPL": -100.0}
    return acct, ps, ctx


def test_assigned_put_stock_is_sold_at_the_slipped_open_minus_commission(tmp_path):
    acct, ps, ctx = _assigned_short_put(tmp_path, "liqputcost", {**CFG, **_COSTS})
    try:
        cash_before = acct._cash
        ps.set_clock(datetime(2024, 3, 11))                  # open 165
        assert acct.process_pending_assignment_liquidations() is True
        acct.refresh_transactions()
        fill = 165.0 * (1.0 - _ADVERSE)                      # a SELL slips down
        assert acct._cash == pytest.approx(cash_before + 100.0 * fill - 1.5)
        (close,) = [o for o in acct.get_orders() if o.comment == "assignment_liquidation"]
        assert close.side == OrderDirection.SELL
        assert close.open_price == pytest.approx(fill)
        (row,) = _equity_rows(acct)
        # Entry: the delivery at the strike (book-keeping, no commission); exit: the sale.
        assert row["pnl"] == pytest.approx((fill - 180.0) * 100.0 - 1.5)
    finally:
        ctx.__exit__(None, None, None)


def test_assigned_call_short_stock_is_bought_back_at_the_slipped_open_plus_commission(tmp_path):
    acct, ps, ctx = _assigned_short_call(tmp_path, "liqcallcost", {**CFG, **_COSTS})
    try:
        cash_before = acct._cash
        ps.set_clock(datetime(2024, 3, 18))                  # open 200
        assert acct.process_pending_assignment_liquidations() is True
        acct.refresh_transactions()
        fill = 200.0 * (1.0 + _ADVERSE)                      # a BUY slips up
        assert acct._cash == pytest.approx(cash_before - 100.0 * fill - 1.5)
        (close,) = [o for o in acct.get_orders() if o.comment == "assignment_liquidation"]
        assert close.side == OrderDirection.BUY
        assert close.open_price == pytest.approx(fill)
        (row,) = _equity_rows(acct)
        assert row["pnl"] == pytest.approx((105.0 - fill) * 100.0 - 1.5)
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize("which", ["put", "call"])
def test_zero_costs_leave_the_assignment_liquidation_unchanged(tmp_path, which):
    """CFG has zero slippage, spread and commission: the sale is at the raw open, as before."""
    if which == "put":
        acct, ps, ctx = _assigned_short_put(tmp_path, "liqput0", CFG)
        day, px, signed = datetime(2024, 3, 11), 165.0, -100.0
    else:
        acct, ps, ctx = _assigned_short_call(tmp_path, "liqcall0", CFG)
        day, px, signed = datetime(2024, 3, 18), 200.0, 100.0
    try:
        cash_before = acct._cash
        ps.set_clock(day)
        assert acct.process_pending_assignment_liquidations() is True
        acct.refresh_transactions()
        assert acct._cash == cash_before - signed * px      # exact, not approx
        (close,) = [o for o in acct.get_orders() if o.comment == "assignment_liquidation"]
        assert close.open_price == px
        (row,) = _equity_rows(acct)
        assert row["pnl"] == pytest.approx((px - 180.0) * 100.0 if which == "put"
                                           else (105.0 - px) * 100.0)
    finally:
        ctx.__exit__(None, None, None)
