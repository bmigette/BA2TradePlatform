"""Option bid-ask spread model at the fill (2026-07-25).

WHY THIS EXISTS: the historical options cache carries no usable quote -- all 958,024 cached
chain rows are either bid==ask or both-NULL, and ``_option_fill_price`` reads the bar's
open/close directly. So before this model an option round trip cost only ``slippage_bps`` of
premium (~nothing), which systematically overstated exactly the multi-leg credit structures
the options grid searches (an iron condor crosses the spread 8 times per round trip).

The model is an ASSUMPTION replacing an indefensible zero, so these tests pin its SHAPE --
percent-of-premium, adverse direction, tick floor, thin-volume widening, no-op default -- not
a specific vendor's quotes.
"""
from types import SimpleNamespace

import pytest

from app.services.backtest.backtest_account import (
    BacktestAccount, _OPTION_SPREAD_LIQUID_VOLUME, _OPTION_SPREAD_THIN_MULT,
)
from ba2_common.core.types import OrderDirection


def _acct(**cfg):
    base = {"starting_cash": 20_000.0, "commission_per_trade": 0.0, "slippage_bps": 0.0,
            "fill_model": "next_bar_open"}
    base.update(cfg)
    return BacktestAccount(id=1, price_source=SimpleNamespace(), settings=base)


LIQUID = {"volume": _OPTION_SPREAD_LIQUID_VOLUME}          # no thin-widening
THIN = {"volume": 3.0}                                      # p25 of the measured distribution


# --------------------------------------------------------------------------- #
# default is an exact no-op (existing configs / results reproduce bit-for-bit)
# --------------------------------------------------------------------------- #
def test_unset_spread_settings_are_an_exact_no_op():
    a = _acct()
    assert a._option_half_spread(1.00, LIQUID) == 0.0
    assert a._option_slip(1.00, True, LIQUID) == 1.00
    assert a._option_slip(1.00, False, LIQUID) == 1.00


def test_slippage_bps_still_applies_when_the_spread_model_is_off():
    """The generic execution-slippage knob is unchanged by this feature."""
    a = _acct(slippage_bps=10.0)  # 10bps
    assert a._option_slip(1.00, True, LIQUID) == pytest.approx(1.001)
    assert a._option_slip(1.00, False, LIQUID) == pytest.approx(0.999)


# --------------------------------------------------------------------------- #
# shape: percent OF PREMIUM, charged half per fill, adverse direction
# --------------------------------------------------------------------------- #
def test_half_spread_is_percent_of_premium_not_bps_of_price():
    """The whole reason for a separate knob: 5% of a $1.00 premium is $0.05 (half = $0.025),
    whereas the equity spread_bps at any sane value would be fractions of a cent."""
    a = _acct(option_spread_pct=5.0)
    assert a._option_half_spread(1.00, LIQUID) == pytest.approx(0.025)
    # ...and it SCALES with the premium (a bps-of-price model would not track premium at all).
    assert a._option_half_spread(4.00, LIQUID) == pytest.approx(0.10)


def test_buys_pay_up_and_sells_receive_down():
    a = _acct(option_spread_pct=5.0)
    assert a._option_slip(2.00, True, LIQUID) == pytest.approx(2.05)    # +half of 5%
    assert a._option_slip(2.00, False, LIQUID) == pytest.approx(1.95)   # -half of 5%
    # The round trip (sell to open, buy to close flat) costs the FULL spread.
    assert a._option_slip(2.00, True, LIQUID) - a._option_slip(2.00, False, LIQUID) == \
        pytest.approx(2.00 * 0.05)


def test_credit_structure_pays_the_spread_on_every_leg():
    """A 4-leg iron condor crosses 4 legs in and 4 out. At 5% on a $1.00-per-leg structure
    that is $0.40 of round-trip cost per contract-set -- the cost that was previously ~0."""
    a = _acct(option_spread_pct=5.0)
    per_leg_round_trip = (a._option_slip(1.00, True, LIQUID)
                          - a._option_slip(1.00, False, LIQUID))
    assert per_leg_round_trip * 4 == pytest.approx(0.20)


# --------------------------------------------------------------------------- #
# tick floor: cheap contracts are where fabricated edge concentrates
# --------------------------------------------------------------------------- #
def test_min_tick_floor_dominates_for_cheap_contracts():
    """5% of a $0.12 premium is $0.006 -- far tighter than any real quote. The floor takes
    over so a near-worthless contract is not modeled as nearly free to trade."""
    a = _acct(option_spread_pct=5.0, option_spread_min_tick=0.02)
    # percent would give 0.003 half; the floor gives 0.01 half.
    assert a._option_half_spread(0.12, LIQUID) == pytest.approx(0.01)
    # For an expensive contract the PERCENT dominates instead.
    assert a._option_half_spread(10.00, LIQUID) == pytest.approx(0.25)


def test_min_tick_alone_still_charges_when_pct_is_zero():
    a = _acct(option_spread_min_tick=0.02)
    assert a._option_half_spread(5.00, LIQUID) == pytest.approx(0.01)


# --------------------------------------------------------------------------- #
# thin-volume widening
# --------------------------------------------------------------------------- #
def test_thin_contracts_are_charged_a_wider_spread():
    a = _acct(option_spread_pct=5.0)
    liquid = a._option_half_spread(2.00, LIQUID)
    thin = a._option_half_spread(2.00, THIN)
    assert thin == pytest.approx(liquid * _OPTION_SPREAD_THIN_MULT)


def test_unknown_volume_is_treated_as_thin_not_liquid():
    """The fill engine's participation cap already treats a missing volume as 0, so assuming
    a tight quote here would contradict it."""
    a = _acct(option_spread_pct=5.0)
    assert a._option_half_spread(2.00, {}) == pytest.approx(
        a._option_half_spread(2.00, THIN))


# --------------------------------------------------------------------------- #
# safety
# --------------------------------------------------------------------------- #
def test_sell_fill_never_goes_negative_on_a_deep_otm_contract():
    """A spread wider than the premium itself would otherwise flip the sign and pay the
    account to sell."""
    a = _acct(option_spread_pct=300.0, option_spread_min_tick=5.0)
    assert a._option_slip(0.10, False, THIN) == 0.0


def test_equity_spread_bps_is_not_double_charged_on_options():
    """_option_slip deliberately excludes spread_bps (bps of price) -- the option model
    already covers the bid-ask, and mixing the two would double-count."""
    a = _acct(option_spread_pct=5.0, spread_bps=1000.0)  # absurd equity value
    assert a._option_half_spread(1.00, LIQUID) == pytest.approx(0.025)
    assert a._option_slip(1.00, True, LIQUID) == pytest.approx(1.025)
