from datetime import date
from ba2_trade_platform.core.types import AssetClass, OptionRight
from ba2_trade_platform.core.option_types import (
    OptionContract, OptionQuote, OptionLeg, OptionPosition,
)
from ba2_trade_platform.core.types import OrderDirection


def test_asset_class_and_right_values():
    assert AssetClass.EQUITY.value == "equity"
    assert AssetClass.OPTION.value == "option"
    assert OptionRight.CALL.value == "call"
    assert OptionRight.PUT.value == "put"


def _contract(**kw):
    base = dict(
        symbol="AAPL260116C00150000", underlying="AAPL",
        option_type=OptionRight.CALL, strike=150.0, expiry=date(2026, 1, 16),
        bid=5.0, ask=5.4, last=5.2, implied_volatility=0.32,
        delta=0.55, gamma=0.02, theta=-0.04, vega=0.10,
        open_interest=1200, volume=300,
    )
    base.update(kw)
    return OptionContract(**base)


def test_contract_mid_and_spread_pct():
    c = _contract()
    assert c.mid == 5.2
    assert round(c.spread_pct, 4) == round(0.4 / 5.2 * 100, 4)


def test_contract_mid_none_when_quote_missing():
    c = _contract(bid=None, ask=None)
    assert c.mid is None
    assert c.spread_pct is None


def test_leg_defaults_ratio_one():
    leg = OptionLeg(contract_symbol="AAPL260116C00150000", side=OrderDirection.BUY)
    assert leg.ratio_qty == 1
    assert leg.position_intent is None


def test_option_position_fields():
    p = OptionPosition(
        contract_symbol="AAPL260116C00150000", underlying="AAPL",
        option_type=OptionRight.CALL, strike=150.0, expiry=date(2026, 1, 16),
        side=OrderDirection.BUY, quantity=2, avg_entry_price=5.2,
        current_price=6.0, market_value=1200.0, unrealized_pl=160.0,
    )
    assert p.multiplier == 100
    assert p.quantity == 2
