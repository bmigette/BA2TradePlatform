"""Live trades 'Value / CapReq': capital requirement = value / the account's EFFECTIVE
margin factor (1.0 with margin off), i.e. the balance the position actually consumes."""
import pytest

from ba2_trade_platform.ui.utils.margin_view import (
    capital_requirement, factors_by_account, value_capreq_text)


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
    assert value_capreq_text(None, None) == ''


def test_cell_text_says_unknown_rather_than_showing_one_figure():
    # The column header promises two figures; a labelled cell spells the word.
    assert value_capreq_text(1800.0, None) == '$1,800.00 / unknown'


class _Acct:
    """An account whose effective factor is whatever the test says it is."""

    def __init__(self, factor=None, raises=None):
        self._factor = factor
        self._raises = raises

    def effective_margin_factor(self):
        if self._raises is not None:
            raise self._raises
        return self._factor


def test_factors_by_account_keeps_a_margin_off_account():
    # Margin off is factor 1.0, a perfectly readable answer -- it belongs in the map.
    accounts = {1: _Acct(factor=1.0), 2: _Acct(factor=1.8)}
    assert factors_by_account([1, 2], resolve=accounts.get) == {1: 1.0, 2: 1.8}


def test_factors_by_account_leaves_out_an_account_whose_accessor_raises():
    accounts = {1: _Acct(factor=1.8), 2: _Acct(raises=ValueError("no snapshot"))}
    assert factors_by_account([1, 2], resolve=accounts.get) == {1: 1.8}


def test_factors_by_account_leaves_out_an_unresolvable_account():
    accounts = {1: _Acct(factor=1.8)}
    assert factors_by_account([1, 2], resolve=accounts.get) == {1: 1.8}


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
def test_factors_by_account_leaves_out_a_defective_factor(bad):
    # Refused at map build, where the account is named -- not per row, where it would
    # be an empty cell with no explanation.
    accounts = {1: _Acct(factor=1.8), 2: _Acct(factor=bad)}
    assert factors_by_account([1, 2], resolve=accounts.get) == {1: 1.8}
