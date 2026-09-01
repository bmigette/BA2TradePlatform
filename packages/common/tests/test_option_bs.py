"""option_bs — Black-Scholes MARK FALLBACK: gating + unit conversion over the shared
``finance_calc.black_scholes`` pricer. See ``ba2_common.core.option_bs`` module
docstring for the DTE=0 convention and why this is not a second BS implementation.
"""
import math

import pytest

from ba2_common.core.option_bs import bs_price
from ba2_common.core.types import OptionRight


# ---------------------------------------------------------------------------
# Hand-derived textbook values (S=100, K=100, T=1y, r=0.05, sigma=0.2): the SAME
# inputs test_finance_calc_portfolio_derivatives_bond.py::test_black_scholes_textbook_call_put
# pins against the shared black_scholes() directly — 365 DTE / 365.0 == 1.0 years exactly,
# so bs_price must reproduce the identical numbers through its unit conversion.
# ---------------------------------------------------------------------------

def test_hand_derived_call_price():
    price = bs_price(100.0, 100.0, 365, 0.2, OptionRight.CALL, r=0.05)
    assert price == pytest.approx(10.4506, abs=1e-3)


def test_hand_derived_put_price():
    price = bs_price(100.0, 100.0, 365, 0.2, OptionRight.PUT, r=0.05)
    assert price == pytest.approx(5.5735, abs=1e-3)


def test_put_call_parity_in_test():
    """C - P == S - K*e^-rT, derived independently of the implementation under test."""
    call = bs_price(100.0, 100.0, 365, 0.2, OptionRight.CALL, r=0.05)
    put = bs_price(100.0, 100.0, 365, 0.2, OptionRight.PUT, r=0.05)
    expected = 100.0 - 100.0 * math.exp(-0.05 * 1.0)
    assert call - put == pytest.approx(expected, abs=1e-3)


def test_put_call_parity_zero_rate_arbitrary_dte():
    """At r=0, parity collapses to C - P == S - K exactly, for ANY dte/iv."""
    call = bs_price(142.0, 150.0, 47, 0.35, OptionRight.CALL)
    put = bs_price(142.0, 150.0, 47, 0.35, OptionRight.PUT)
    assert call - put == pytest.approx(142.0 - 150.0, abs=1e-6)


def test_r_defaults_to_zero():
    explicit = bs_price(100.0, 95.0, 30, 0.25, OptionRight.CALL, r=0.0)
    default = bs_price(100.0, 95.0, 30, 0.25, OptionRight.CALL)
    assert explicit == pytest.approx(default, abs=1e-9)


# ---------------------------------------------------------------------------
# DTE=0 convention — see the module docstring. Must return None, not raise or price at 0.
# ---------------------------------------------------------------------------

def test_dte_zero_returns_none():
    assert bs_price(100.0, 100.0, 0, 0.2, OptionRight.CALL) is None


def test_dte_negative_returns_none():
    assert bs_price(100.0, 100.0, -5, 0.2, OptionRight.CALL) is None


# ---------------------------------------------------------------------------
# Missing / degenerate inputs fall through (None), never priced as a substitute like 0.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["spot", "strike", "dte_days", "iv"])
def test_missing_required_field_returns_none(field):
    kwargs = dict(spot=100.0, strike=100.0, dte_days=30, iv=0.2)
    kwargs[field] = None
    assert bs_price(kwargs["spot"], kwargs["strike"], kwargs["dte_days"], kwargs["iv"],
                     OptionRight.CALL) is None


def test_missing_iv_is_not_priced_as_zero():
    """A caller must never see a real number back when iv is absent — mutation (c)."""
    with_iv = bs_price(100.0, 100.0, 30, 0.2, OptionRight.CALL)
    without_iv = bs_price(100.0, 100.0, 30, None, OptionRight.CALL)
    assert with_iv is not None
    assert without_iv is None


@pytest.mark.parametrize("bad", [0.0, -0.2])
def test_non_positive_iv_returns_none(bad):
    assert bs_price(100.0, 100.0, 30, bad, OptionRight.CALL) is None


@pytest.mark.parametrize("bad", [0.0, -10.0])
def test_non_positive_spot_or_strike_returns_none(bad):
    assert bs_price(bad, 100.0, 30, 0.2, OptionRight.CALL) is None
    assert bs_price(100.0, bad, 30, 0.2, OptionRight.CALL) is None


def test_nan_inputs_return_none():
    assert bs_price(float("nan"), 100.0, 30, 0.2, OptionRight.CALL) is None
    assert bs_price(100.0, 100.0, 30, float("nan"), OptionRight.CALL) is None


def test_inf_inputs_return_none():
    assert bs_price(float("inf"), 100.0, 30, 0.2, OptionRight.CALL) is None
    assert bs_price(100.0, 100.0, float("inf"), 0.2, OptionRight.CALL) is None


def test_non_finite_rate_falls_back_to_zero_not_none():
    """r is not a 'requires' input — a bad rate degrades to 0.0 rather than voiding the price."""
    price = bs_price(100.0, 100.0, 30, 0.2, OptionRight.CALL, r=float("nan"))
    assert price is not None
    zero_rate = bs_price(100.0, 100.0, 30, 0.2, OptionRight.CALL, r=0.0)
    assert price == pytest.approx(zero_rate, abs=1e-9)


def test_invalid_right_returns_none():
    # OptionRight is a str Enum (its own value IS the "call"/"put" string, so those are
    # legitimately accepted elsewhere in the codebase) — an unrelated string/None is not.
    assert bs_price(100.0, 100.0, 30, 0.2, "bogus") is None
    assert bs_price(100.0, 100.0, 30, 0.2, None) is None


# ---------------------------------------------------------------------------
# Sanity: a call is worth more than a put when deep ITM the corresponding way, and prices
# are always non-negative — cheap smoke coverage beyond the two pinned textbook numbers.
# ---------------------------------------------------------------------------

def test_deep_itm_call_worth_more_than_far_otm_call():
    itm = bs_price(150.0, 100.0, 60, 0.3, OptionRight.CALL)
    otm = bs_price(80.0, 100.0, 60, 0.3, OptionRight.CALL)
    assert itm > otm > 0
