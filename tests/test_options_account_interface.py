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
