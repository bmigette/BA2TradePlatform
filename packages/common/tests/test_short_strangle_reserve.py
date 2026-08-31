# packages/common/tests/test_short_strangle_reserve.py
"""Short strangle/straddle margin: TRUE Reg-T pair formula (operator decision 2026-08-31).

History. Review 2026-08-30 F10 found the strangle's entry reserve priced ONLY the put
leg while the backtest maintenance model charged naked margin per held short lot (the
per-leg SUM), so entries opened ~2x what maintenance tolerated and were force-unwound
on the first sweep. The F10 commit (3841c49a) closed the breach by reserving the SUM of
both legs' naked margins — deliberately conservative, entry == maintenance at open.

This file now pins the operator-decided replacement (2026-08-31): TRUE Reg-T for a
short strangle/straddle is NOT the sum. It is::

    requirement = (the GREATER leg's naked margin) + (the OTHER leg's premium x 100)

per contract, where "greater" compares the two legs' Reg-T naked-margin brackets
(``naked_margin_per_contract``; premium excluded there by this codebase's convention —
the collected premium sits in cash). Both the ENTRY reserve
(``option_reserve_required``) and the backtest MAINTENANCE model
(``BacktestAccount.maintenance_margin_requirement``, pinned in
``testplatform/backend/tests/backtest/test_margin_strangle_pairing.py``) price this
same formula through ONE shared implementation
(``OptionsAccountInterface.short_pair_margin_per_contract``), so the F10 invariant —
a just-opened position never instantly breaches its own maintenance — is preserved by
construction: both sides move together.

The old SUM numbers are the mutation target: reverting either branch to the per-leg
sum must fail the pinned dollars here, and reverting the strangle to put-only must
still fail too.
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
# Premiums (per share): put 4.00, call 1.50. The PUT leg's margin is greater, so
# Reg-T = put margin + call premium = 4,000 + 1.50*100 = $4,150/ct.
SPOT = 250.0
PUT_STRIKE = 240.0
CALL_STRIKE = 275.0
PUT_LEG_MARGIN = 4_000.0
CALL_LEG_MARGIN = 2_500.0
PUT_PREMIUM = 4.00
CALL_PREMIUM = 1.50
REG_T_PAIR = 4_150.0  # 4,000 + 1.50*100


def test_short_strangle_reserve_is_greater_leg_margin_plus_other_premium():
    """Pinned dollars: reserve = greater leg's naked margin + OTHER leg's premium.

    4,000 (put margin, the greater) + 150 (call premium) = 4,150/ct — NOT the F10
    per-leg sum (6,500) and NOT the pre-F10 put-only margin (4,000)."""
    assert acct.naked_margin_per_contract(
        PUT_STRIKE, option_type=OptionRight.PUT, spot=SPOT) == pytest.approx(PUT_LEG_MARGIN)
    assert acct.naked_margin_per_contract(
        CALL_STRIKE, option_type=OptionRight.CALL, spot=SPOT) == pytest.approx(CALL_LEG_MARGIN)
    reserve = acct.option_reserve_required(
        "short_strangle", 1, strike=PUT_STRIKE, call_strike=CALL_STRIKE, spot=SPOT,
        put_premium=PUT_PREMIUM, call_premium=CALL_PREMIUM)
    assert reserve == pytest.approx(REG_T_PAIR)
    # Named non-values: the SUM model and the put-only model both die here.
    assert reserve != pytest.approx(PUT_LEG_MARGIN + CALL_LEG_MARGIN)  # 6,500 (F10 sum)
    assert reserve != pytest.approx(PUT_LEG_MARGIN)                    # 4,000 (pre-F10)
    # And it scales linearly in quantity.
    assert acct.option_reserve_required(
        "short_strangle", 3, strike=PUT_STRIKE, call_strike=CALL_STRIKE, spot=SPOT,
        put_premium=PUT_PREMIUM, call_premium=CALL_PREMIUM,
    ) == pytest.approx(3 * REG_T_PAIR)


def test_short_strangle_reserve_when_the_call_leg_is_the_greater():
    """The greater leg is chosen by MARGIN, not fixed to the put: spot 250, put 230
    (OTM 20 -> max(50-20, 25) = 30 -> $3,000), call 260 (OTM 10 -> max(50-10, 25) = 40
    -> $4,000). Call is greater, so the PUT's premium is the one added:
    4,000 + 2.00*100 = $4,200 — not 4,000 + 350 (always-call-premium) and not
    3,000 + 350 (always-put-leg)."""
    reserve = acct.option_reserve_required(
        "short_strangle", 1, strike=230.0, call_strike=260.0, spot=SPOT,
        put_premium=2.00, call_premium=3.50)
    assert reserve == pytest.approx(4_000.0 + 200.0)


def test_short_strangle_reserve_requires_both_strikes_and_both_premiums():
    """Fail closed: any missing sizing input refuses — never a partial number.

    Without the call strike the call leg's margin is UNKNOWN; without either premium
    the Reg-T other-leg term is UNKNOWN. A negative premium is an unpopulated/broken
    field, not a price."""
    with pytest.raises(ValueError, match="call_strike"):
        acct.option_reserve_required(
            "short_strangle", 1, strike=PUT_STRIKE, call_strike=None, spot=SPOT,
            put_premium=PUT_PREMIUM, call_premium=CALL_PREMIUM)
    with pytest.raises(ValueError, match="call_strike"):
        acct.option_reserve_required(
            "short_strangle", 1, strike=PUT_STRIKE, call_strike=0.0, spot=SPOT,
            put_premium=PUT_PREMIUM, call_premium=CALL_PREMIUM)
    with pytest.raises(ValueError, match="put_premium"):
        acct.option_reserve_required(
            "short_strangle", 1, strike=PUT_STRIKE, call_strike=CALL_STRIKE, spot=SPOT,
            put_premium=None, call_premium=CALL_PREMIUM)
    with pytest.raises(ValueError, match="call_premium"):
        acct.option_reserve_required(
            "short_strangle", 1, strike=PUT_STRIKE, call_strike=CALL_STRIKE, spot=SPOT,
            put_premium=PUT_PREMIUM, call_premium=None)
    with pytest.raises(ValueError, match="put_premium"):
        acct.option_reserve_required(
            "short_strangle", 1, strike=PUT_STRIKE, call_strike=CALL_STRIKE, spot=SPOT,
            put_premium=-0.5, call_premium=CALL_PREMIUM)


def test_short_straddle_reserve_is_greater_leg_margin_plus_other_premium():
    """Same formula, both legs at ONE strike: strike 225, spot 250 -> the CALL is ITM
    (OTM amount 0 -> 0.20*250*100 = $5,000), the put OTM 25 -> $2,500. Call is the
    greater leg, so the PUT premium is added: 5,000 + 0.80*100 = $5,080/ct — NOT the
    F10 sum (7,500) and NOT the pre-F10 max() (5,000; the put premium term is what
    separates them)."""
    reserve = acct.option_reserve_required(
        "short_straddle", 1, strike=225.0, spot=250.0,
        put_premium=0.80, call_premium=26.00)
    assert reserve == pytest.approx(5_080.0)
    assert reserve != pytest.approx(7_500.0)  # F10 sum
    assert reserve != pytest.approx(5_000.0)  # pre-F10 max()
    assert acct.option_reserve_required(
        "short_straddle", 2, strike=225.0, spot=250.0,
        put_premium=0.80, call_premium=26.00) == pytest.approx(2 * 5_080.0)


def test_short_straddle_margin_tie_adds_the_larger_premium():
    """Exactly ATM the two legs' margin brackets TIE (strike 250 at spot 250: both
    max(0.20*250 - 0, 25) = $5,000). 'The other leg's premium' is then ambiguous; the
    implementation must resolve the tie CONSERVATIVELY by adding the larger premium:
    5,000 + 6.00*100 = $5,600, not 5,000 + 400."""
    reserve = acct.option_reserve_required(
        "short_straddle", 1, strike=250.0, spot=250.0,
        put_premium=4.00, call_premium=6.00)
    assert reserve == pytest.approx(5_600.0)


def test_short_straddle_reserve_requires_both_premiums():
    with pytest.raises(ValueError, match="put_premium"):
        acct.option_reserve_required(
            "short_straddle", 1, strike=225.0, spot=250.0,
            put_premium=None, call_premium=26.00)
    with pytest.raises(ValueError, match="call_premium"):
        acct.option_reserve_required(
            "short_straddle", 1, strike=225.0, spot=250.0,
            put_premium=0.80, call_premium=None)


def test_entry_reserve_and_maintenance_share_one_pair_formula():
    """The F10 invariant, re-pinned for the Reg-T model: entry and maintenance must
    MOVE TOGETHER. Both route through ``short_pair_margin_per_contract`` — the entry
    reserve here, the backtest maintenance model in
    ``test_margin_strangle_pairing.py`` — so on identical inputs they are the same
    number by construction."""
    pair = acct.short_pair_margin_per_contract(
        put_strike=PUT_STRIKE, call_strike=CALL_STRIKE, spot=SPOT,
        put_premium=PUT_PREMIUM, call_premium=CALL_PREMIUM)
    assert pair == pytest.approx(REG_T_PAIR)
    assert acct.option_reserve_required(
        "short_strangle", 4, strike=PUT_STRIKE, call_strike=CALL_STRIKE, spot=SPOT,
        put_premium=PUT_PREMIUM, call_premium=CALL_PREMIUM) == pytest.approx(4 * pair)
    # The straddle is the same formula with both legs at one strike.
    straddle_pair = acct.short_pair_margin_per_contract(
        put_strike=225.0, call_strike=225.0, spot=250.0,
        put_premium=0.80, call_premium=26.00)
    assert acct.option_reserve_required(
        "short_straddle", 1, strike=225.0, spot=250.0,
        put_premium=0.80, call_premium=26.00) == pytest.approx(straddle_pair)


def test_short_strangle_builder_sizes_and_reserves_the_reg_t_pair():
    """The builder must pass both strikes AND both leg premiums through: with
    FakeAccount's chain (spot=100, 10% OTM -> put 90 / call 110, both legs' margin
    $1,000/ct — a tie — and both bids 5.0 - 0.08*10 = 4.20) the Reg-T pair is
    1,000 + 4.20*100 = $1,420/ct. A 10% sizing on $100k affords
    floor(10,000 / 1,420) = 7 contracts — not the 5 the F10 sum ($2,000/ct) bought,
    nor the 10 of the pre-F10 put-only reserve — and persists
    option_reserve = 1,420 x quantity."""
    from tests.test_new_option_actions import _mk

    acct_fake, act = _mk("open_short_strangle", strike_method="percent_otm",
                         strike_param=10.0, dte_min=20, dte_max=40, sizing=10.0)
    act.submit_to_broker = False  # informational result carries the reserve in data
    res = act.execute()
    assert res["success"], res["message"]
    data = res["data"]
    strikes = {leg["strike"] for leg in data["legs"]}
    assert strikes == {90.0, 110.0}
    per_contract = OptionsAccountInterface.short_pair_margin_per_contract(
        put_strike=90.0, call_strike=110.0, spot=100.0,
        put_premium=4.20, call_premium=4.20)
    assert per_contract == pytest.approx(1_420.0)
    assert data["quantity"] == 7
    assert data["option_reserve"] == pytest.approx(per_contract * data["quantity"])
