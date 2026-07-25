"""Daily-volume liquidity gate on option selection (2026-07-25).

WHY: the historical cache has NO open_interest (0 of 958,024 chain rows populated), so the
pre-existing ``min_open_interest`` gate cannot fire on cached data at all. Traded volume IS
populated for every bar, and it is the signal that matters, because the backtest FILL engine
independently caps an order at ~10% of the bar's volume -- so a contract trading 1-3
contracts/day yields an order that can never fill and just sits pending. Gating at SELECTION
makes the selector agree with the filler.
"""
from datetime import date

import pytest

from ba2_common.core.option_selector import passes_liquidity, select_single
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

TODAY = date(2024, 6, 3)
EXP = date(2024, 7, 19)


def _c(strike, *, volume=None, oi=None, last=1.25, delta=-0.30):
    return OptionContract(
        symbol=f"XYZ240719P{int(strike * 1000):08d}", underlying="XYZ",
        option_type=OptionRight.PUT, strike=strike, expiry=EXP,
        bid=None, ask=None, last=last, delta=delta, open_interest=oi, volume=volume)


# --------------------------------------------------------------------------- #
# gate semantics
# --------------------------------------------------------------------------- #
def test_gate_is_off_by_default_so_existing_callers_are_unaffected():
    assert passes_liquidity(_c(100.0, volume=None), None, None) is True
    assert passes_liquidity(_c(100.0, volume=1), None, None) is True


@pytest.mark.parametrize("volume,expected", [(0, False), (3, False), (24, False),
                                             (25, True), (500, True)])
def test_min_volume_rejects_contracts_below_the_threshold(volume, expected):
    assert passes_liquidity(_c(100.0, volume=volume), None, None, 25) is expected


def test_unknown_volume_fails_closed_when_the_gate_is_on():
    """Unknown liquidity must not be assumed good -- the fill engine treats a missing volume
    as 0 and refuses to fill, so the selector must not hand it that candidate."""
    assert passes_liquidity(_c(100.0, volume=None), None, None, 25) is False


def test_volume_gate_is_independent_of_the_open_interest_gate():
    """OI is NULL throughout the cache; the volume gate must work without it."""
    c = _c(100.0, volume=500, oi=None)
    assert passes_liquidity(c, None, None, 25) is True
    assert passes_liquidity(c, 10, None, 25) is False  # OI gate still rejects when asked


def test_premium_floor_still_applies_alongside_the_volume_gate():
    assert passes_liquidity(_c(100.0, volume=9999, last=0.04), None, None, 25) is False


# --------------------------------------------------------------------------- #
# threaded through the selectors
# --------------------------------------------------------------------------- #
def test_select_single_skips_thin_strikes_and_picks_a_tradeable_one():
    """The thin contract is the BEST delta match; without the gate it wins. With the gate the
    selector falls to the liquid one instead of returning something unfillable."""
    thin = _c(95.0, volume=2, delta=-0.30)     # exact delta match, untradeable
    liquid = _c(90.0, volume=400, delta=-0.25)
    chain = [thin, liquid]

    ungated = select_single(chain, method="delta", strike_param=0.30, spot=100.0,
                            option_type=OptionRight.PUT, dte_min=30, dte_max=60, today=TODAY)
    assert ungated is thin

    gated = select_single(chain, method="delta", strike_param=0.30, spot=100.0,
                          option_type=OptionRight.PUT, dte_min=30, dte_max=60, today=TODAY,
                          min_volume=25)
    assert gated is liquid


def test_select_single_returns_none_when_every_candidate_is_too_thin():
    """Fail loud (no selection) rather than returning a contract that cannot fill."""
    chain = [_c(95.0, volume=2), _c(90.0, volume=1)]
    assert select_single(chain, method="delta", strike_param=0.30, spot=100.0,
                         option_type=OptionRight.PUT, dte_min=30, dte_max=60, today=TODAY,
                         min_volume=25) is None
