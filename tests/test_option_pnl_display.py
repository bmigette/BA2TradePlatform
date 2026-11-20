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
        _gild_txn(), [_gild_entry()], object(), 1, {}, {})
    assert measured == 42.0 and seen == ['GILD']

    monkeypatch.setattr(display, 'open_structure_pnl',
                        lambda *a, **k: display.unavailable_pnl(UNAVAILABLE_NO_FILLS))
    assert _FloatingPLWidgetBase._option_transaction_pl(
        _gild_txn(), [_gild_entry()], object(), 1, {}, {}) == 0.0      # holds nothing: a zero

    monkeypatch.setattr(display, 'open_structure_pnl',
                        lambda *a, **k: display.unavailable_pnl(UNAVAILABLE_NO_QUOTE))
    assert _FloatingPLWidgetBase._option_transaction_pl(
        _gild_txn(), [_gild_entry()], object(), 1, {}, {}) is None     # held but unquoted


# ---- broker position marks: display pricing at the broker's own position price -------------
from ba2_trade_platform.core.option_pnl_display import broker_position_marks, quote_caching_account


class _MarkAccount:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        if isinstance(self._positions, Exception):
            raise self._positions
        return self._positions

    def get_option_quote(self, contract_symbol):  # pragma: no cover - must not be reached
        raise AssertionError("marks mode must not read a bid/ask quote")


def test_marks_map_every_priced_position_and_skips_unpriced():
    acct = _MarkAccount([
        {'symbol': 'XYZ_C95', 'current_price': 1.5},
        {'symbol': 'XYZ_C99', 'current_price': None},
    ])
    assert broker_position_marks(acct) == {'XYZ_C95': 1.5}


def test_failed_position_fetch_marks_nothing_never_flat():
    assert broker_position_marks(_MarkAccount(None)) == {}
    assert broker_position_marks(_MarkAccount(RuntimeError('down'))) == {}


def test_marks_mode_quotes_at_the_mark_and_unknown_when_unlisted():
    wrapped = quote_caching_account(_MarkAccount([]), {}, 1, {'XYZ_C95': 2.0})
    q = wrapped.get_option_quote('XYZ_C95')
    assert (q.bid, q.ask, q.last) == (2.0, 2.0, 2.0)
    assert wrapped.get_option_quote('XYZ_C100') is None   # not held per the broker: unknown


# ---- a broker mark of ZERO is a price, not a missing one (8082, 2026-11-20) -----------------
# Alpaca reported two far out-of-the-money calls a week from expiry (no bid) at current_price 0,
# unrealized_pl = minus the whole premium. The single-leg seam declines a premium <= 0, so both
# were dropped from the Floating P/L card as "no broker price" and the row read ~$2,170 too high.
from ba2_common.core.option_types import OptionQuote

TSM_CALL = 'TSM261127C00485000'


def _tsm_entry(side=OrderDirection.BUY):
    return SimpleNamespace(
        id=8, transaction_id=5, account_id=1, symbol='TSM', side=side,
        status=OrderStatus.FILLED, quantity=1.0, filled_qty=1.0, open_price=6.95,
        asset_class=AssetClass.OPTION, contract_symbol=TSM_CALL,
        option_type='CALL', strike=485.0, expiry=_date(2026, 11, 27), multiplier=100,
        position_intent='buy_to_open' if side == OrderDirection.BUY else 'sell_to_open',
        option_strategy='long_call', underlying_symbol='TSM',
        created_at=_datetime(2026, 10, 13, 13, 36), data=None, comment=None,
    )


def _tsm_txn(side=OrderDirection.BUY, multiplier=100):
    return SimpleNamespace(id=5, symbol='TSM', side=side, quantity=1.0, open_price=6.95,
                           multiplier=multiplier, asset_class=AssetClass.OPTION)


@pytest.fixture
def real_seam(monkeypatch):
    """The rules engine's OWN single-leg seam, with only its transaction lookup answered here."""
    import ba2_common.core.TradeConditions as conditions
    held = {}
    monkeypatch.setattr(conditions, '_get_transaction_for_order',
                        lambda order: held.get(order.transaction_id))
    return held


def _marked(marks):
    return quote_caching_account(_MarkAccount([]), {}, 1, marks)


def test_a_long_call_the_broker_marks_at_zero_has_lost_its_whole_premium(real_seam):
    txn = real_seam[5] = _tsm_txn()
    result = open_structure_pnl(_marked({TSM_CALL: 0.0}), txn, [_tsm_entry()])

    assert result.available
    assert result.amount == pytest.approx(-695.0) and result.percent == pytest.approx(-100.0)


def test_a_short_call_the_broker_marks_at_zero_has_kept_its_whole_premium(real_seam):
    txn = real_seam[5] = _tsm_txn(side=OrderDirection.SELL)
    result = open_structure_pnl(_marked({TSM_CALL: 0.0}), txn, [_tsm_entry(side=OrderDirection.SELL)])

    assert result.amount == pytest.approx(695.0) and result.percent == pytest.approx(100.0)


def test_the_zero_mark_figure_is_the_seams_own_formula_at_a_premium_of_zero(real_seam):
    from ba2_common.core.TransactionHelper import TransactionHelper
    for side in (OrderDirection.BUY, OrderDirection.SELL):
        txn = real_seam[5] = _tsm_txn(side=side)
        limit = TransactionHelper.calculate_option_pnl(txn, 1e-9, 100)
        result = open_structure_pnl(_marked({TSM_CALL: 0.0}), txn, [_tsm_entry(side=side)])
        assert result.amount == pytest.approx(limit['amount'], abs=0.01)
        assert result.percent == pytest.approx(limit['percent'], abs=0.01)


def test_a_positive_mark_is_still_priced_by_the_seam(real_seam):
    txn = real_seam[5] = _tsm_txn()
    result = open_structure_pnl(_marked({TSM_CALL: 0.24}), txn, [_tsm_entry()])
    assert result.amount == pytest.approx(-671.0) and result.source == 'single_leg_premium'


def test_a_contract_the_broker_does_not_list_is_still_unknown(real_seam):
    txn = real_seam[5] = _tsm_txn()
    result = open_structure_pnl(_marked({}), txn, [_tsm_entry()])
    assert not result.available and result.reason == UNAVAILABLE_NO_QUOTE


def test_a_zero_mark_without_a_contract_multiplier_is_still_unknown(real_seam):
    txn = real_seam[5] = _tsm_txn(multiplier=None)
    entry = _tsm_entry()
    entry.multiplier = None
    result = open_structure_pnl(_marked({TSM_CALL: 0.0}), txn, [entry])
    assert not result.available


def test_a_zero_bid_on_the_quote_path_is_still_unknown(real_seam):
    """Only the broker's POSITION mark is trusted at zero. A live quote reports an absent side as
    0 too, and that path is the rules engine's: it is deliberately left exactly as it was."""
    class _Quoted(_MarkAccount):
        def get_option_quote(self, contract_symbol):
            return OptionQuote(symbol=contract_symbol, bid=0.0, ask=2.11, last=0.24)

    txn = real_seam[5] = _tsm_txn()
    result = open_structure_pnl(quote_caching_account(_Quoted([]), {}, 1), txn, [_tsm_entry()])
    assert not result.available and result.reason == UNAVAILABLE_NO_QUOTE


def test_the_floating_pl_card_counts_a_position_the_broker_marks_at_zero(real_seam):
    from ba2_trade_platform.ui.components.FloatingPLPerAccountWidget import _FloatingPLWidgetBase
    txn = real_seam[5] = _tsm_txn()
    measured = _FloatingPLWidgetBase._option_transaction_pl(
        txn, [_tsm_entry()], _MarkAccount([]), 1, {}, {TSM_CALL: 0.0})
    assert measured == pytest.approx(-695.0)


def test_an_unpriced_option_is_named_by_its_contract_not_its_underlying():
    """'no broker price for TSM' read as a missing STOCK price; what is unpriced is the contract."""
    from ba2_trade_platform.services.floating_pl import unpriced_names
    assert unpriced_names(_tsm_txn(), [_tsm_entry()]) == [TSM_CALL]
    wing = _tsm_entry()
    wing.contract_symbol = 'TSM261127C00500000'
    parent = _tsm_entry()
    parent.contract_symbol = None
    assert unpriced_names(_tsm_txn(), [parent, _tsm_entry(), wing, _tsm_entry()]) == \
        [TSM_CALL, 'TSM261127C00500000']
    # an equity transaction has no contract: its own symbol, as before
    stock = SimpleNamespace(id=1, symbol='AAPL')
    assert unpriced_names(stock, [SimpleNamespace(contract_symbol=None)]) == ['AAPL']
    assert unpriced_names(stock, []) == ['AAPL']
