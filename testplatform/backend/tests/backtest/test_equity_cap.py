"""The optional fixed-notional equity cap. Pure arithmetic, no engine, no clock."""
from __future__ import annotations

import pytest

from app.services.backtest.equity_cap import (
    EquityCapError, deployed_equity, validate_equity_cap,
)


@pytest.fixture
def caplog_free_logger():
    """Named only to make the no-caplog rule visible at the call site."""
    return None


# ---------------------------------------------------------------------------
# Task 1 -- validation
# ---------------------------------------------------------------------------
def test_none_means_the_feature_is_off():
    assert validate_equity_cap(None) is None


def test_a_positive_cap_is_returned_as_a_float():
    assert validate_equity_cap(20_000) == 20_000.0
    assert isinstance(validate_equity_cap(20_000), float)


@pytest.mark.parametrize("bad", [0, 0.0, -1, -20_000.0])
def test_a_non_positive_cap_is_refused(bad):
    with pytest.raises(EquityCapError, match="must be greater than zero"):
        validate_equity_cap(bad)


@pytest.mark.parametrize("bad", ["20000", "", [], {}, object()])
def test_a_non_numeric_cap_is_refused(bad):
    with pytest.raises(EquityCapError, match="must be a number"):
        validate_equity_cap(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_cap_is_refused(bad):
    with pytest.raises(EquityCapError, match="must be finite"):
        validate_equity_cap(bad)


def test_a_bool_is_not_a_number():
    """True == 1 in Python. A boolean reaching a money field is a caller bug, not a $1 cap."""
    with pytest.raises(EquityCapError, match="must be a number"):
        validate_equity_cap(True)


def test_a_cap_above_the_initial_capital_is_allowed_and_says_so(caplog_free_logger):
    """It cannot bind YET, but the account may grow into it. Not an error."""
    msgs = []
    assert validate_equity_cap(50_000, initial_capital=20_000, log=msgs.append) == 50_000.0
    assert any("cannot bind" in m and "50,000" in m and "20,000" in m for m in msgs), msgs


def test_a_cap_at_or_below_the_initial_capital_logs_nothing():
    msgs = []
    validate_equity_cap(20_000, initial_capital=20_000, log=msgs.append)
    validate_equity_cap(5_000, initial_capital=20_000, log=msgs.append)
    assert msgs == []


# ---------------------------------------------------------------------------
# Task 2 -- deployed equity
# ---------------------------------------------------------------------------
def test_with_no_cap_the_real_equity_passes_through():
    assert deployed_equity(37_412.55, cap=None) == 37_412.55


def test_above_the_cap_only_the_cap_is_deployed():
    assert deployed_equity(40_000.0, cap=20_000.0) == 20_000.0


def test_below_the_cap_the_real_equity_is_deployed():
    """'except if account value goes below' -- a drawdown genuinely shrinks what can be deployed."""
    assert deployed_equity(15_000.0, cap=20_000.0) == 15_000.0


def test_exactly_at_the_cap():
    assert deployed_equity(20_000.0, cap=20_000.0) == 20_000.0


def test_recovery_climbs_back_to_the_cap_and_stops():
    assert deployed_equity(18_000.0, cap=20_000.0) == 18_000.0
    assert deployed_equity(20_000.0, cap=20_000.0) == 20_000.0
    assert deployed_equity(25_000.0, cap=20_000.0) == 20_000.0


def test_a_wiped_out_account_deploys_nothing_not_the_cap():
    assert deployed_equity(0.0, cap=20_000.0) == 0.0


def test_negative_equity_is_not_raised_to_zero_here():
    """The caller decides what a negative account means; this function does not invent a floor."""
    assert deployed_equity(-500.0, cap=20_000.0) == -500.0


def test_unmeasurable_equity_is_unmeasurable_not_zero():
    """None in means None out. A broker/engine that cannot state equity has not stated zero."""
    assert deployed_equity(None, cap=20_000.0) is None
    assert deployed_equity(None, cap=None) is None
