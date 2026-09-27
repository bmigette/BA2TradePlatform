"""``share_grid``: the ONE definition of which share quantities a symbol trades in.

Extracted from the portfolio allocator so the classic risk manager, the risk-based sizer and
FactorRanker round onto the same grid. What must hold:

* whole shares unless the caller allows fractions AND the broker says ``True`` -- an unknown
  (``None``) is whole shares, never a fraction nobody verified;
* always rounds DOWN, so no sizer spends more than it was given;
* a quantity that is really on the grid survives float noise instead of losing a whole step.
"""
from types import SimpleNamespace

import pytest

from ba2_common.core.share_grid import (
    DEFAULT_FRACTIONAL_UNIT,
    WHOLE_SHARE,
    floor_to_unit,
    fractional_unit,
    is_fractional_quantity,
    is_whole_grid,
    round_shares,
    tradeable_unit,
)


# ---------------------------------------------------------------------------
# Which grid
# ---------------------------------------------------------------------------

def test_fractions_need_BOTH_the_caller_and_the_broker():
    assert fractional_unit(True, allow_fractional=True) == DEFAULT_FRACTIONAL_UNIT
    assert fractional_unit(True, allow_fractional=False) == WHOLE_SHARE
    assert fractional_unit(False, allow_fractional=True) == WHOLE_SHARE


def test_an_unknown_answer_is_whole_shares():
    """``None`` is "the broker did not say". Reading it as True sends a fraction the broker
    may refuse; whole shares under-fill instead, which is the safe direction."""
    assert fractional_unit(None, allow_fractional=True) == WHOLE_SHARE


def test_a_truthy_non_bool_is_not_a_yes():
    """Only ``True`` counts. A hand-edited "yes" must not select the fractional grid."""
    assert fractional_unit("yes", allow_fractional=True) == WHOLE_SHARE
    assert fractional_unit(1, allow_fractional=True) == WHOLE_SHARE


def test_a_published_increment_is_honoured_on_the_fractional_grid_only():
    assert fractional_unit(True, allow_fractional=True, min_trade_increment=0.001) == 0.001
    # An increment on a symbol the broker will not fractionalise makes nothing tradeable.
    assert fractional_unit(False, allow_fractional=True, min_trade_increment=0.001) == WHOLE_SHARE


def test_the_default_step_divides_every_supported_brokers_grid():
    """0.0001 is a whole multiple of Alpaca's 9-decimal and TastyTrade's 5-decimal steps, so
    a quantity on it is always on the broker's own grid -- which is what lets a sizer that
    knows only the FLAG use it."""
    for broker_step in (1e-9, 1e-5):
        assert round(DEFAULT_FRACTIONAL_UNIT / broker_step, 6) == int(round(
            DEFAULT_FRACTIONAL_UNIT / broker_step))


def test_tradeable_unit_reads_a_marginInfo_shaped_object():
    margin = SimpleNamespace(fractionable=True, min_trade_increment=None)

    assert tradeable_unit(margin, allow_fractional=True) == DEFAULT_FRACTIONAL_UNIT
    assert tradeable_unit(None, allow_fractional=True) == WHOLE_SHARE


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------

def test_whole_grid_floors():
    assert floor_to_unit(3.99, WHOLE_SHARE) == 3.0


def test_fractional_grid_floors_onto_the_step():
    assert floor_to_unit(3.03037, 0.0001) == 3.0303


def test_a_value_exactly_on_the_grid_does_not_lose_a_step_to_float_noise():
    """``3 * 0.0001 / 0.0001`` is not exactly 3.0 in binary; a bare floor would turn 0.0003
    shares into 0.0002. The grid floors with a tolerance of a billionth of a step."""
    assert floor_to_unit(3 * 0.0001, 0.0001) == 0.0003
    assert floor_to_unit(0.1 + 0.2, 0.0001) == 0.3


def test_never_negative_and_never_rounds_up():
    assert floor_to_unit(-1.0, 0.0001) == 0.0
    assert floor_to_unit(0.00009, 0.0001) == 0.0
    assert floor_to_unit(None, 0.0001) == 0.0


def test_round_shares_is_the_allocator_contract():
    margin = SimpleNamespace(fractionable=True, min_trade_increment=None)

    assert round_shares(2.56789, margin, allow_fractional=True) == 2.5678
    assert round_shares(2.56789, margin, allow_fractional=False) == 2.0


def test_fractional_quantity_detection_tolerates_noise_from_both_sides():
    assert is_fractional_quantity(2.5)
    assert not is_fractional_quantity(3.0000000001)
    assert not is_fractional_quantity(2.9999999999)


def test_is_whole_grid():
    assert is_whole_grid(WHOLE_SHARE)
    assert is_whole_grid(None)
    assert not is_whole_grid(DEFAULT_FRACTIONAL_UNIT)


def test_the_allocator_still_exports_the_old_names():
    """The allocator delegates to this module; its callers import the old names from it."""
    import ba2_common.core.portfolio_allocation as pa

    assert pa.tradeable_unit is tradeable_unit
    assert pa._round_shares is round_shares
    assert pa._is_fractional_quantity is is_fractional_quantity
    assert pa.DEFAULT_FRACTIONAL_DECIMALS == 4
