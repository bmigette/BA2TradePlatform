"""Live trades 'Value / CapReq': capital requirement = value / the account's EFFECTIVE
margin factor (1.0 with margin off), i.e. the balance the position actually consumes."""
import pytest

from ba2_trade_platform.ui.utils.margin_view import capital_requirement, value_capreq_text


def test_capreq_equals_value_with_factor_one():
    assert capital_requirement(1800.0, effective_factor=1.0) == 1800.0


def test_capreq_is_value_over_the_effective_factor():
    assert capital_requirement(1800.0, effective_factor=1.8) == 1000.0


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
def test_capreq_refuses_a_non_positive_or_nan_factor(bad):
    with pytest.raises(ValueError):
        capital_requirement(1800.0, effective_factor=bad)


def test_cell_text():
    assert value_capreq_text(1800.0, 1000.0) == '$1,800.00 / $1,000.00'
    assert value_capreq_text(1800.0, None) == '$1,800.00'
    assert value_capreq_text(None, None) == ''
