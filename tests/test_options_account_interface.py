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
