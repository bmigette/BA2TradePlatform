import inspect
import pytest
from ba2_trade_platform.core.interfaces import OptionsAccountInterface


def test_is_abstract_capability_interface():
    assert inspect.isabstract(OptionsAccountInterface)
    assert OptionsAccountInterface.supports_options is True
    with pytest.raises(TypeError):
        OptionsAccountInterface()  # abstract, cannot instantiate


def test_declares_expected_surface():
    for name in (
        "get_option_chain", "get_option_quote", "get_atm_implied_volatility",
        "get_option_positions", "submit_option_order", "_submit_option_order_impl",
        "close_option_position", "get_iv_rank",
    ):
        assert hasattr(OptionsAccountInterface, name), name


from datetime import date
from ba2_trade_platform.core.interfaces import OptionsAccountInterface as _OAI
from ba2_trade_platform.core.types import OptionRight as _OptionRight


def test_mock_account_is_option_capable(mock_account):
    assert isinstance(mock_account, _OAI)
    assert mock_account.supports_options is True


def test_mock_chain_and_quote(mock_account):
    chain = mock_account.get_option_chain(
        "AAPL", date(2026, 1, 1), date(2026, 3, 1), _OptionRight.CALL)
    assert len(chain) > 0
    c = chain[0]
    assert c.underlying == "AAPL"
    assert c.option_type == _OptionRight.CALL
    assert c.delta is not None and c.implied_volatility is not None
    q = mock_account.get_option_quote(c.symbol)
    assert q is not None and q.symbol == c.symbol


def test_mock_atm_iv(mock_account):
    iv = mock_account.get_atm_implied_volatility("AAPL")
    assert 0 < iv < 2


from datetime import date as _date
from sqlmodel import select as _select
from ba2_trade_platform.core.db import get_db as _get_db, get_instance as _get_instance
from ba2_trade_platform.core.models import TradingOrder as _TradingOrder, Transaction as _Transaction
from ba2_trade_platform.core.option_types import OptionLeg as _OptionLeg
from ba2_trade_platform.core.types import (
    OrderDirection as _Dir, OptionRight as _Right, OrderStatus as _Status,
    AssetClass as _AC, OrderType as _OT,
)


def test_single_leg_long_call_persists_order_and_txn(mock_account, sample_recommendation):
    leg = _OptionLeg(contract_symbol="AAPL260116C00150000", side=_Dir.BUY,
                     position_intent="buy_to_open", option_type=_Right.CALL,
                     strike=150.0, expiry=_date(2026, 1, 16), underlying="AAPL")
    result = mock_account.submit_option_order(
        [leg], quantity=2, order_type="limit", limit_price=5.2,
        option_strategy="long_call", expert_recommendation_id=sample_recommendation.id)
    assert result is not None
    assert result.asset_class == _AC.OPTION
    assert result.contract_symbol == "AAPL260116C00150000"
    assert result.option_type == _Right.CALL
    assert result.side == _Dir.BUY
    assert result.order_type == _OT.BUY_LIMIT
    assert result.transaction_id is not None
    # Re-fetch the PERSISTED parent row and confirm the broker fill was saved
    db_parent = _get_instance(_TradingOrder, result.id)
    assert db_parent.status == _Status.FILLED
    assert db_parent.broker_order_id is not None
    assert db_parent.multiplier == 100
    # Transaction exists in the DB
    with _get_db() as s:
        txn = s.get(_Transaction, result.transaction_id)
        assert txn is not None


def test_single_sell_leg_uses_sell_limit_order_type(mock_account, sample_recommendation):
    # Covered-call write / closing a long: a single SELL leg with a POSITIVE premium
    # must yield side=SELL AND order_type=SELL_LIMIT (not BUY_LIMIT).
    leg = _OptionLeg(contract_symbol="AAPL260116C00160000", side=_Dir.SELL,
                     position_intent="sell_to_open", option_type=_Right.CALL,
                     strike=160.0, expiry=_date(2026, 1, 16), underlying="AAPL")
    result = mock_account.submit_option_order(
        [leg], quantity=1, order_type="limit", limit_price=2.5,
        option_strategy="covered_call", expert_recommendation_id=sample_recommendation.id)
    assert result is not None
    assert result.side == _Dir.SELL
    assert result.order_type == _OT.SELL_LIMIT
    db_parent = _get_instance(_TradingOrder, result.id)
    assert db_parent.side == _Dir.SELL
    assert db_parent.order_type == _OT.SELL_LIMIT


def test_bull_call_spread_persists_parent_and_two_children(mock_account, sample_recommendation):
    long_leg = _OptionLeg(contract_symbol="AAPL260116C00150000", side=_Dir.BUY,
                          position_intent="buy_to_open", option_type=_Right.CALL,
                          strike=150.0, expiry=_date(2026, 1, 16), underlying="AAPL")
    short_leg = _OptionLeg(contract_symbol="AAPL260116C00160000", side=_Dir.SELL,
                           position_intent="sell_to_open", option_type=_Right.CALL,
                           strike=160.0, expiry=_date(2026, 1, 16), underlying="AAPL")
    parent = mock_account.submit_option_order(
        [long_leg, short_leg], quantity=1, order_type="limit", limit_price=4.0,
        option_strategy="bull_call_spread", expert_recommendation_id=sample_recommendation.id)
    assert parent is not None
    assert parent.contract_symbol is None              # parent is the strategy, not a contract
    assert parent.option_strategy == "bull_call_spread"
    assert parent.asset_class == _AC.OPTION
    db_parent = _get_instance(_TradingOrder, parent.id)
    assert db_parent.status == _Status.FILLED
    with _get_db() as s:
        children = s.exec(_select(_TradingOrder).where(
            _TradingOrder.parent_order_id == parent.id)).all()
        assert len(children) == 2
        assert {c.contract_symbol for c in children} == {
            "AAPL260116C00150000", "AAPL260116C00160000"}
        assert all(c.transaction_id == parent.transaction_id for c in children)
        assert all(c.status == _Status.FILLED for c in children)   # mock persisted the leg fills
        assert all(c.asset_class == _AC.OPTION for c in children)
