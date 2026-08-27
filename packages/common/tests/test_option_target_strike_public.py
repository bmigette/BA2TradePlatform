"""``target_strike`` is public so the selection policy reuses it instead of duplicating it.

WHY IT MATTERS: the policy's "distance from the box centre" feature must measure distance to the
SAME strike the existing selector aims at. Two copies of this arithmetic would drift, and the
symptom would be the new policy picking a different contract at DEFAULT weights — silently
breaking the no-op guarantee that lets us ship this without changing any backtest.
"""
import pytest

from ba2_common.core.option_selector import target_strike
from ba2_common.core.types import OptionRight


# ``approx`` because the percent_otm branches are binary float arithmetic: spot * (1 + pct/100)
# for the call above evaluates to 110.00000000000001, not 110.0. The put happens to land on
# exactly 90.0, but only by accident of representation — asserting it exactly would be a trap for
# whoever next changes the spot or the percentage. The tolerance is still ~1e-4 in absolute terms
# here, so a genuinely wrong formula (a flipped sign, a missing /100) is caught.
def test_percent_otm_call_is_above_spot():
    assert target_strike("percent_otm", 10.0, 100.0, None, OptionRight.CALL) == pytest.approx(110.0)


def test_percent_otm_put_is_below_spot():
    assert target_strike("percent_otm", 10.0, 100.0, None, OptionRight.PUT) == pytest.approx(90.0)


def test_consensus_target_returns_the_target_price():
    assert target_strike("consensus_target", None, 100.0, 123.0, OptionRight.CALL) == 123.0


def test_delta_method_has_no_target_strike():
    assert target_strike("delta", 0.3, 100.0, None, OptionRight.CALL) is None


def test_consensus_target_with_no_target_price_is_None():
    """The third path to None, and the one a caller is most likely to miss.

    ``select_single``'s ``target_price`` defaults to None, so this is reachable whenever a
    consensus_target rule fires on a recommendation that carries no target. The selection
    policy relies on it returning None rather than raising.
    """
    assert target_strike("consensus_target", None, 100.0, None, OptionRight.CALL) is None


def test_an_unrecognised_method_is_None_rather_than_a_guess():
    assert target_strike("nearest_round_number", 5.0, 100.0, 110.0, OptionRight.CALL) is None
