"""Long ITM single-leg option expiry settles at INTRINSIC (review 2026-11-09).

A LONG ITM single-leg option settles at intrinsic, computed from the same underlying
settlement price and the same formula (``_expiry_intrinsic``) the defined-risk combo path
uses -- never at the expiry bar's premium close. On the expiry date there is no time value
left; the day's last print of a thin contract can still carry some, and taking it biased
long single legs upward against the combos (pure intrinsic) and the short legs (assigned at
the strike). A settle that cannot find its transaction credits NO cash.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from ba2_common.core.types import OptionRight, OrderDirection

from tests.backtest.test_option_orphan_stock_and_arb_guards import (
    CFG, _CALL105, _CALL110, _EXP_0308, _EXP_0315, _PUT180,
    _bar_row, _build, _chain_row, _engine, _leg, _submit_call,
)

_PUT150 = "AAPL240315P00150000"   # 150 put, expiry 2024-03-15 (spot 200 -> OTM)


def _expire(acct, ps, day: datetime):
    ps.set_clock(day)
    _engine(acct, ps)._apply_option_expiry(day)


def _expiry_closes(acct):
    return [o for o in acct.get_orders() if o.comment == "option_expiry_close"]


def test_long_itm_call_settles_at_intrinsic_when_the_expiry_close_is_above_it(tmp_path):
    """Expiry close of the 105 call prints 97.0; spot closes 200 -> intrinsic 95.0."""
    acct, ps, ctx = _build(
        tmp_path, "lcintr",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315),
         _bar_row(_CALL105, "2024-03-15", 97.0, "call", 105.0, _EXP_0315)],
    )
    try:
        _submit_call(acct)
        acct.refresh_orders()
        acct.refresh_transactions()
        cash_after_entry = acct._cash
        _expire(acct, ps, datetime(2024, 3, 15))

        assert acct.get_option_positions() == []
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
        assert acct._cash == pytest.approx(cash_after_entry + 95.0 * 100.0)
        closes = _expiry_closes(acct)
        assert len(closes) == 1
        assert closes[0].open_price == pytest.approx(95.0)
    finally:
        ctx.__exit__(None, None, None)


def test_long_itm_put_settles_at_intrinsic_when_the_expiry_close_is_above_it(tmp_path):
    """Long 180 put, expiry 2024-03-08: spot closes 164 -> intrinsic 16.0; the bar prints 17.5."""
    acct, ps, ctx = _build(
        tmp_path, "lpintr",
        [_chain_row(_PUT180, "put", 180.0, _EXP_0308)],
        [_bar_row(_PUT180, "2024-03-06", 21.0, "put", 180.0, _EXP_0308),
         _bar_row(_PUT180, "2024-03-08", 17.5, "put", 180.0, _EXP_0308)],
    )
    try:
        acct.submit_option_order(
            legs=[_leg(_PUT180, OrderDirection.BUY, "buy_to_open",
                       OptionRight.PUT, 180.0, _EXP_0308)],
            quantity=1, order_type="market", option_strategy="long_put")
        acct.refresh_orders()
        acct.refresh_transactions()
        cash_after_entry = acct._cash
        assert cash_after_entry == pytest.approx(100_000.0 - 2_100.0)
        _expire(acct, ps, datetime(2024, 3, 8))

        assert acct.get_option_positions() == []
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
        assert acct._cash == pytest.approx(cash_after_entry + 16.0 * 100.0)
        closes = _expiry_closes(acct)
        assert len(closes) == 1
        assert closes[0].open_price == pytest.approx(16.0)
    finally:
        ctx.__exit__(None, None, None)


def test_otm_long_put_still_expires_worthless(tmp_path):
    """Long 150 put with spot closing 200 is OTM: closed at 0 even though the bar prints 0.05."""
    acct, ps, ctx = _build(
        tmp_path, "lpotm",
        [_chain_row(_PUT150, "put", 150.0, _EXP_0315)],
        [_bar_row(_PUT150, "2024-03-06", 1.0, "put", 150.0, _EXP_0315),
         _bar_row(_PUT150, "2024-03-15", 0.05, "put", 150.0, _EXP_0315)],
    )
    try:
        acct.submit_option_order(
            legs=[_leg(_PUT150, OrderDirection.BUY, "buy_to_open",
                       OptionRight.PUT, 150.0, _EXP_0315)],
            quantity=1, order_type="market", option_strategy="long_put")
        acct.refresh_orders()
        acct.refresh_transactions()
        cash_after_entry = acct._cash
        _expire(acct, ps, datetime(2024, 3, 15))

        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(cash_after_entry)
        closes = _expiry_closes(acct)
        assert len(closes) == 1
        assert closes[0].open_price == pytest.approx(0.0)
    finally:
        ctx.__exit__(None, None, None)


def test_defined_risk_combo_still_settles_at_net_intrinsic(tmp_path):
    """Bull call spread 105/110 at spot 200: net payoff = width 5 x 100, whatever the legs print."""
    acct, ps, ctx = _build(
        tmp_path, "comboint",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315),
         _chain_row(_CALL110, "call", 110.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315),
         _bar_row(_CALL110, "2024-03-06", 52.0, "call", 110.0, _EXP_0315),
         _bar_row(_CALL105, "2024-03-15", 97.0, "call", 105.0, _EXP_0315),
         _bar_row(_CALL110, "2024-03-15", 90.5, "call", 110.0, _EXP_0315)],
    )
    try:
        long_leg = _leg(_CALL105, OrderDirection.BUY, "buy_to_open",
                        OptionRight.CALL, 105.0, _EXP_0315)
        short_leg = _leg(_CALL110, OrderDirection.SELL, "sell_to_open",
                         OptionRight.CALL, 110.0, _EXP_0315)
        acct.submit_option_order(legs=[long_leg, short_leg], quantity=1,
                                 order_type="market", option_strategy="bull_call_spread")
        acct.refresh_orders()
        acct.refresh_transactions()
        assert len(acct.get_option_positions()) == 2
        cash_after_entry = acct._cash
        _expire(acct, ps, datetime(2024, 3, 15))

        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(cash_after_entry + 5.0 * 100.0)
        by_contract = {o.symbol: float(o.open_price) for o in _expiry_closes(acct)}
        assert by_contract == {_CALL105: pytest.approx(95.0), _CALL110: pytest.approx(90.0)}
    finally:
        ctx.__exit__(None, None, None)


def test_a_failed_long_itm_settle_credits_no_cash(tmp_path, monkeypatch):
    """When the leg's transaction cannot be found the settle returns False -- and moves no cash."""
    acct, ps, ctx = _build(
        tmp_path, "lcfail",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315)],
    )
    try:
        _submit_call(acct)
        acct.refresh_orders()
        acct.refresh_transactions()
        cash_after_entry = acct._cash
        (pos,) = acct.get_option_positions()
        ps.set_clock(datetime(2024, 3, 15))
        monkeypatch.setattr(acct, "_option_transaction_for_contract", lambda _sym: None)

        assert acct.settle_single_leg_expiry(pos, 200.0) is False
        assert acct._cash == pytest.approx(cash_after_entry)
    finally:
        ctx.__exit__(None, None, None)
