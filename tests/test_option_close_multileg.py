"""Tests for multi-leg option close construction (CloseOptionAction fix) and
round-lot sizing (protective put / covered call equity legs)."""

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core.TradeActions import build_closing_legs
from ba2_trade_platform.core.TradeRiskManagement import apply_lot_size
from ba2_trade_platform.core.types import OptionRight, OrderDirection


@dataclass
class _Quote:
    bid: float | None = None
    ask: float | None = None


def _child(contract, side, qty=1.0, option_type=OptionRight.CALL, strike=100.0,
           underlying="MP"):
    return SimpleNamespace(
        contract_symbol=contract,
        side=side,
        quantity=qty,
        option_type=option_type,
        strike=strike,
        expiry=date(2026, 7, 17),
        underlying_symbol=underlying,
        symbol=underlying,
    )


class TestBuildClosingLegs:
    def test_bull_call_spread_close_reverses_sides_and_nets_credit(self):
        # Entry: long 100C (BUY), short 110C (SELL). Close: sell 100C at bid,
        # buy back 110C at ask -> net negative (credit).
        children = [
            _child("MP260717C00100000", OrderDirection.BUY, strike=100.0),
            _child("MP260717C00110000", OrderDirection.SELL, strike=110.0),
        ]
        quotes = {
            "MP260717C00100000": _Quote(bid=5.0, ask=5.4),
            "MP260717C00110000": _Quote(bid=1.8, ask=2.1),
        }
        legs, net = build_closing_legs(children, parent_quantity=1, quote_fn=quotes.get)

        assert [l.side for l in legs] == [OrderDirection.SELL, OrderDirection.BUY]
        assert [l.position_intent for l in legs] == ["sell_to_close", "buy_to_close"]
        assert legs[0].contract_symbol == "MP260717C00100000"
        # net = +ask(short buyback) - bid(long sale) = 2.1 - 5.0 = -2.9 (credit)
        assert net == pytest.approx(-2.9)

    def test_missing_quote_returns_none_net(self):
        children = [
            _child("MP260717C00100000", OrderDirection.BUY),
            _child("MP260717C00110000", OrderDirection.SELL),
        ]
        quotes = {"MP260717C00100000": _Quote(bid=5.0, ask=5.4)}  # second leg unquoted
        legs, net = build_closing_legs(children, parent_quantity=1, quote_fn=quotes.get)
        assert len(legs) == 2
        assert net is None

    def test_ratio_qty_derived_from_child_quantity(self):
        children = [_child("MP260717C00100000", OrderDirection.BUY, qty=2.0)]
        quotes = {"MP260717C00100000": _Quote(bid=3.0, ask=3.2)}
        legs, net = build_closing_legs(children, parent_quantity=1, quote_fn=quotes.get)
        assert legs[0].ratio_qty == 2
        assert net == pytest.approx(-6.0)

    def test_every_leg_has_contract_symbol(self):
        # The original bug: a close was submitted with contract_symbol=None.
        children = [
            _child("PL260717P00009000", OrderDirection.BUY, option_type=OptionRight.PUT,
                   strike=9.0, underlying="PL"),
        ]
        legs, _ = build_closing_legs(children, parent_quantity=3,
                                     quote_fn=lambda s: _Quote(bid=0.8, ask=0.95))
        assert all(l.contract_symbol for l in legs)


class TestApplyLotSize:
    def test_rounds_down_to_lot(self):
        assert apply_lot_size({"lot_size": 100}, 250) == 200

    def test_below_one_lot_becomes_zero(self):
        assert apply_lot_size({"lot_size": 100}, 7) == 0

    def test_exact_lots_unchanged(self):
        assert apply_lot_size({"lot_size": 100}, 300) == 300

    def test_no_lot_size_returns_none(self):
        assert apply_lot_size({}, 7) is None
        assert apply_lot_size({"lot_size": None}, 7) is None
        assert apply_lot_size({"lot_size": 1}, 7) is None

    def test_garbage_lot_size_ignored(self):
        assert apply_lot_size({"lot_size": "abc"}, 7) is None
