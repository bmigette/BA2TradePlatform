# packages/common/tests/test_short_strangle_reserve.py
"""Review 2026-08-30 F10: the short strangle's entry reserve priced ONLY the put leg.

The backtest's maintenance model (``BacktestAccount.maintenance_margin_requirement``)
loops over EVERY held short option lot and charges ``naked_margin_per_contract`` for
each — a strangle carries a short call AND a short put, so maintenance demands the SUM
of both legs' Reg-T naked margins. Entry, however, reserved (and sized off) the PUT
side alone, so a just-opened strangle held roughly HALF the margin its own maintenance
check demands and was force-unwound at unguarded marks on the next margin sweep.

The fix: entry reserves the sum of BOTH legs' naked margins — exactly the maintenance
model — so a just-opened position can never instantly breach (entry == maintenance at
open, and entry >= the Reg-T greater-leg-plus-other-premium convention a fortiori).
The short STRADDLE shared the defect through the same ``option_reserve_required``
branch chain (it took max(call, put) at the one strike where maintenance charges the
sum), so it is fixed and pinned here too.
"""
import pytest

from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.types import OptionRight

acct = OptionsAccountInterface

# One asymmetric-margin fixture used throughout, so every number below is checkable by
# hand.  spot=250, put strike 240, call strike 275, NAKED_MARGIN_FRACTION=0.20,
# floor 0.10:
#   put  leg: OTM = 250-240 = 10 -> max(0.20*250 - 10, 0.10*250) = 40   -> $4,000/ct
#   call leg: OTM = 275-250 = 25 -> max(0.20*250 - 25, 0.10*250) = 25   -> $2,500/ct
SPOT = 250.0
PUT_STRIKE = 240.0
CALL_STRIKE = 275.0
PUT_LEG_MARGIN = 4_000.0
CALL_LEG_MARGIN = 2_500.0


def test_short_strangle_reserve_prices_both_short_legs():
    """Pinned dollars: reserve = put-leg margin + call-leg margin, per contract."""
    assert acct.naked_margin_per_contract(
        PUT_STRIKE, option_type=OptionRight.PUT, spot=SPOT) == pytest.approx(PUT_LEG_MARGIN)
    assert acct.naked_margin_per_contract(
        CALL_STRIKE, option_type=OptionRight.CALL, spot=SPOT) == pytest.approx(CALL_LEG_MARGIN)
    reserve = acct.option_reserve_required(
        "short_strangle", 1, strike=PUT_STRIKE, call_strike=CALL_STRIKE, spot=SPOT)
    assert reserve == pytest.approx(PUT_LEG_MARGIN + CALL_LEG_MARGIN)  # 6,500 not 4,000
    # And it scales linearly in quantity.
    assert acct.option_reserve_required(
        "short_strangle", 3, strike=PUT_STRIKE, call_strike=CALL_STRIKE, spot=SPOT,
    ) == pytest.approx(3 * (PUT_LEG_MARGIN + CALL_LEG_MARGIN))


def test_short_strangle_entry_reserve_covers_the_maintenance_model():
    """Entry >= maintenance at open: the maintenance loop charges naked margin for EACH
    short lot, so the entry reserve must be at least the per-leg SUM (the old put-only
    reserve was strictly below it whenever a call leg exists)."""
    maintenance_sum = (
        acct.naked_margin_per_contract(PUT_STRIKE, option_type=OptionRight.PUT, spot=SPOT)
        + acct.naked_margin_per_contract(CALL_STRIKE, option_type=OptionRight.CALL, spot=SPOT)
    )
    reserve = acct.option_reserve_required(
        "short_strangle", 1, strike=PUT_STRIKE, call_strike=CALL_STRIKE, spot=SPOT)
    assert reserve >= maintenance_sum
    # The old put-only reserve is strictly below the maintenance demand — this is the
    # breach the fix removes, and what makes this test bite under the F10 mutation.
    assert PUT_LEG_MARGIN < maintenance_sum


def test_short_strangle_reserve_requires_the_call_strike():
    """Without the call strike the call leg's margin is UNKNOWN, and an unknown
    requirement must not be silently priced as put-leg-only (that was the bug)."""
    with pytest.raises(ValueError, match="call_strike"):
        acct.option_reserve_required(
            "short_strangle", 1, strike=PUT_STRIKE, call_strike=None, spot=SPOT)
    with pytest.raises(ValueError, match="call_strike"):
        acct.option_reserve_required(
            "short_strangle", 1, strike=PUT_STRIKE, call_strike=0.0, spot=SPOT)


def test_short_straddle_reserve_prices_both_short_legs():
    """Same defect, same strike: the straddle took max(call, put) where the maintenance
    loop charges call + put. Pinned: strike 225, spot 250 -> call leg is ITM
    (max(0.20*250 - 0, 25) = 50 -> $5,000/ct), put leg OTM 25 (-> $2,500/ct)."""
    reserve = acct.option_reserve_required("short_straddle", 1, strike=225.0, spot=250.0)
    assert reserve == pytest.approx(5_000.0 + 2_500.0)  # 7,500 not max() = 5,000
    maintenance_sum = (
        acct.naked_margin_per_contract(225.0, option_type=OptionRight.CALL, spot=250.0)
        + acct.naked_margin_per_contract(225.0, option_type=OptionRight.PUT, spot=250.0)
    )
    assert reserve >= maintenance_sum
    assert acct.option_reserve_required(
        "short_straddle", 2, strike=225.0, spot=250.0) == pytest.approx(15_000.0)


def test_short_strangle_builder_sizes_and_reserves_both_legs():
    """The builder must pass BOTH strikes through: with FakeAccount's chain (spot=100,
    10% OTM -> put 90 / call 110, both legs $1,000/ct naked margin) a 10% sizing on
    $100k affords 10,000 / 2,000 = 5 contracts — not the 10 the put-only reserve
    bought — and persists option_reserve = 2,000 x quantity."""
    from tests.test_new_option_actions import _mk

    acct_fake, act = _mk("open_short_strangle", strike_method="percent_otm",
                         strike_param=10.0, dte_min=20, dte_max=40, sizing=10.0)
    act.submit_to_broker = False  # informational result carries the reserve in data
    res = act.execute()
    assert res["success"], res["message"]
    data = res["data"]
    strikes = {leg["strike"] for leg in data["legs"]}
    assert strikes == {90.0, 110.0}
    per_contract = (
        OptionsAccountInterface.naked_margin_per_contract(
            90.0, option_type=OptionRight.PUT, spot=100.0)
        + OptionsAccountInterface.naked_margin_per_contract(
            110.0, option_type=OptionRight.CALL, spot=100.0)
    )
    assert per_contract == pytest.approx(2_000.0)
    assert data["quantity"] == 5
    assert data["option_reserve"] == pytest.approx(per_contract * data["quantity"])
