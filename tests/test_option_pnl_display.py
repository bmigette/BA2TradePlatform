"""Live OPTION transaction P&L for the Options tab (spec 2026-09-20, decision 5).

The point of these tests is the two ways the existing page gets an option row wrong: it
prices off the UNDERLYING quote, and it drops the contract multiplier. The seam functions
are injected here so the dispatch is asserted without a live broker account, and the
closed-P&L path is pure arithmetic.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ba2_trade_platform.core.option_pnl_display import (
    UNAVAILABLE_NOT_AN_OPTION, UNAVAILABLE_NO_MULTIPLIER, UNAVAILABLE_NO_PRICES,
    UNAVAILABLE_NO_QUOTE, option_closed_pnl, option_transaction_pnl,
)
from ba2_trade_platform.core.types import AssetClass, OrderDirection


def _order(**over):
    base = dict(id=1, asset_class=AssetClass.OPTION, contract_symbol=None)
    base.update(over)
    return SimpleNamespace(**base)


def _txn(**over):
    base = dict(
        id=7, asset_class=AssetClass.OPTION, side=OrderDirection.BUY,
        quantity=1, open_price=8.0, close_price=13.3, multiplier=100,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _Seam:
    """Records which seam was called and returns a canned P&L."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, account, order):
        self.calls.append(order)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class TestDispatch:
    def test_a_leg_with_a_contract_is_priced_off_that_contract(self):
        single = _Seam({'amount': 530.0, 'percent': 66.25})
        multi = _Seam({'amount': 999.0, 'percent': 1.0})

        result = option_transaction_pnl(
            account=object(), order=_order(contract_symbol='ACN260918C00095000'),
            single_leg=single, multi_leg=multi)

        assert result.amount == 530.0
        assert result.source == 'single_leg_premium'
        assert len(single.calls) == 1 and not multi.calls

    def test_a_multi_leg_parent_is_priced_off_the_structure_net_premium(self):
        # The parent of a combo carries NO contract symbol -- the contracts live on the
        # children -- which is exactly the case the equity path priced against the
        # underlying and reported as ~+4900%.
        single = _Seam({'amount': 1.0, 'percent': 1.0})
        multi = _Seam({'amount': 370.0, 'percent': 61.7})

        result = option_transaction_pnl(
            account=object(), order=_order(contract_symbol=None),
            single_leg=single, multi_leg=multi)

        assert result.amount == 370.0
        assert result.source == 'structure_net_premium'
        assert len(multi.calls) == 1 and not single.calls

    def test_an_equity_order_is_not_priced_here(self):
        result = option_transaction_pnl(
            account=object(), order=_order(asset_class=None),
            single_leg=_Seam(None), multi_leg=_Seam(None))
        assert not result.available
        assert result.reason == UNAVAILABLE_NOT_AN_OPTION


class TestNeverInventsANumber:
    def test_a_seam_that_declines_reports_unknown_not_zero(self):
        result = option_transaction_pnl(
            account=object(), order=_order(contract_symbol='X'),
            single_leg=_Seam(None), multi_leg=_Seam(None))
        assert result.amount is None
        assert result.percent is None
        assert result.reason == UNAVAILABLE_NO_QUOTE

    def test_a_raising_seam_is_unknown_not_a_crash(self):
        result = option_transaction_pnl(
            account=object(), order=_order(contract_symbol='X'),
            single_leg=_Seam(RuntimeError('no quote')), multi_leg=_Seam(None))
        assert result.amount is None
        assert result.reason == UNAVAILABLE_NO_QUOTE

    def test_a_seam_returning_no_amount_is_unknown(self):
        result = option_transaction_pnl(
            account=object(), order=_order(contract_symbol='X'),
            single_leg=_Seam({'percent': 5.0}), multi_leg=_Seam(None))
        assert not result.available


class TestClosedPnl:
    def test_a_long_spread_is_multiplied_by_the_contract_multiplier(self):
        # 1 contract, 8.00 -> 13.30, x100: the money is 530, NOT the 5.30 the equity
        # formula would print.
        result = option_closed_pnl(_txn())

        assert result.amount == pytest.approx(530.0)
        assert result.percent == pytest.approx(66.25)
        assert result.source == 'recorded_fills'

    def test_a_short_leg_profits_when_the_premium_falls(self):
        result = option_closed_pnl(
            _txn(side=OrderDirection.SELL, open_price=4.0, close_price=1.0))

        assert result.amount == pytest.approx(300.0)
        assert result.percent == pytest.approx(75.0)

    def test_an_unrecorded_multiplier_refuses_rather_than_guessing(self):
        result = option_closed_pnl(_txn(multiplier=None))

        assert result.amount is None
        assert result.reason == UNAVAILABLE_NO_MULTIPLIER

    def test_a_zero_or_negative_multiplier_is_refused(self):
        assert option_closed_pnl(_txn(multiplier=0)).reason == UNAVAILABLE_NO_MULTIPLIER
        assert option_closed_pnl(_txn(multiplier=-100)).reason == UNAVAILABLE_NO_MULTIPLIER

    def test_missing_prices_report_unknown(self):
        assert option_closed_pnl(_txn(close_price=None)).reason == UNAVAILABLE_NO_PRICES
        assert option_closed_pnl(_txn(open_price=None)).reason == UNAVAILABLE_NO_PRICES
        assert option_closed_pnl(_txn(quantity=0)).reason == UNAVAILABLE_NO_PRICES

    def test_a_genuine_multiplier_of_one_is_accepted(self):
        result = option_closed_pnl(_txn(multiplier=1, open_price=8.0, close_price=13.3))
        assert result.amount == pytest.approx(5.3)


# ---- open_structure_pnl: the ONE display rule (Options tab + Floating P/L cards) -----------
from datetime import date as _date, datetime as _datetime

from ba2_trade_platform.core.option_pnl_display import UNAVAILABLE_NO_FILLS, open_structure_pnl
from ba2_trade_platform.core.types import OrderStatus


def _gild_entry(status=OrderStatus.FILLED, filled_qty=2.0):
    """The options instance's real entry, 2026-09-24: 2 x GILD261120C00165000 at $2.95."""
    return SimpleNamespace(
        id=1, transaction_id=1, account_id=1, symbol='GILD', side=OrderDirection.BUY,
        status=status, quantity=2.0, filled_qty=filled_qty, open_price=2.95,
        asset_class=AssetClass.OPTION, contract_symbol='GILD261120C00165000',
        option_type='CALL', strike=165.0, expiry=_date(2026, 11, 20), multiplier=100,
        position_intent='buy_to_open', option_strategy='long_call', underlying_symbol='GILD',
        created_at=_datetime(2026, 9, 24, 13, 40), data=None, comment=None,
    )


def _gild_txn():
    return SimpleNamespace(id=1, symbol='GILD', side=OrderDirection.BUY, quantity=2.0,
                           open_price=2.95, multiplier=100, asset_class=AssetClass.OPTION)


def test_a_filled_long_call_is_priced_through_the_single_leg_seam():
    single, multi = _Seam({'amount': 110.0, 'percent': 18.6}), _Seam({'amount': -1.0})
    result = open_structure_pnl(object(), _gild_txn(), [_gild_entry()],
                                single_leg=single, multi_leg=multi)

    assert result.available and result.amount == 110.0
    assert result.source == 'single_leg_premium'
    assert single.calls and single.calls[0].contract_symbol == 'GILD261120C00165000'
    assert not multi.calls


def test_an_entry_that_has_not_filled_holds_nothing():
    single = _Seam({'amount': 999.0})
    result = open_structure_pnl(object(), _gild_txn(),
                                [_gild_entry(status=OrderStatus.OPEN, filled_qty=0.0)],
                                single_leg=single, multi_leg=single)

    assert not result.available and result.reason == UNAVAILABLE_NO_FILLS
    assert not single.calls


def test_the_floating_pl_cards_price_an_option_through_the_same_rule(monkeypatch):
    """The cards looked GILD up in the broker's book, which holds the CONTRACT: every option
    structure read 'no broker price for GILD' and the total said $0.00 (partial)."""
    import ba2_trade_platform.core.option_pnl_display as display
    from ba2_trade_platform.ui.components.FloatingPLPerAccountWidget import _FloatingPLWidgetBase

    seen = []

    def fake(account, transaction, orders, **kw):
        seen.append(transaction.symbol)
        return display.OptionPnlDisplay(amount=42.0, percent=7.0, source='single_leg_premium')

    monkeypatch.setattr(display, 'open_structure_pnl', fake)
    measured = _FloatingPLWidgetBase._option_transaction_pl(
        _gild_txn(), [_gild_entry()], object(), 1, {})
    assert measured == 42.0 and seen == ['GILD']

    monkeypatch.setattr(display, 'open_structure_pnl',
                        lambda *a, **k: display.unavailable_pnl(UNAVAILABLE_NO_FILLS))
    assert _FloatingPLWidgetBase._option_transaction_pl(
        _gild_txn(), [_gild_entry()], object(), 1, {}) == 0.0      # holds nothing: a zero

    monkeypatch.setattr(display, 'open_structure_pnl',
                        lambda *a, **k: display.unavailable_pnl(UNAVAILABLE_NO_QUOTE))
    assert _FloatingPLWidgetBase._option_transaction_pl(
        _gild_txn(), [_gild_entry()], object(), 1, {}) is None     # held but unquoted
