"""Payoff at expiry, checked against hand-computed values for real structures.

WHY HAND-COMPUTED AND NOT PROPERTY-ONLY. The whole point of deriving max loss from the payoff
curve is that a hand-written per-structure table drifts. That argument only holds if the curve
itself is right, so the curve is pinned to arithmetic a reader can verify in their head.
"""
import re

import pytest

from ba2_common.core.option_payoff import (
    MEASURED,
    MIN_MEASURABLE_PROFIT,
    UNBOUNDED,
    UNMEASURABLE,
    PayoffLeg,
    max_loss,
    max_profit,
    payoff_at,
    validate_legs,
)
from ba2_common.core.types import OrderDirection


def long_call(strike, premium, ratio=1):
    return PayoffLeg(kind="call", side=OrderDirection.BUY, premium=premium,
                     strike=strike, ratio=ratio)


def short_call(strike, premium, ratio=1):
    return PayoffLeg(kind="call", side=OrderDirection.SELL, premium=premium,
                     strike=strike, ratio=ratio)


def long_put(strike, premium, ratio=1):
    return PayoffLeg(kind="put", side=OrderDirection.BUY, premium=premium,
                     strike=strike, ratio=ratio)


def short_put(strike, premium, ratio=1):
    return PayoffLeg(kind="put", side=OrderDirection.SELL, premium=premium,
                     strike=strike, ratio=ratio)


def long_stock(entry):
    return PayoffLeg(kind="stock", side=OrderDirection.BUY, premium=entry, strike=None)


def reported_payoff(reason):
    """The payoff figure a refusal message quotes, as a float.

    Assertions on these messages compare the NUMBER, never a substring of it. Substring
    checks are silently one-directional here: ``"0.0" in reason`` also matches
    ``"-100.0000"``, so it cannot tell the two refusal cases apart and passes even when both
    report the same wrong figure. Extracting and comparing survives a format change too,
    which a hardcoded ``"0.0000"`` does not.
    """
    match = re.search(r"payoff (-?[\d.]+)", reason)
    assert match is not None, f"no payoff figure in refusal reason: {reason!r}"
    return float(match.group(1))


def test_long_call_below_strike_loses_exactly_the_debit():
    legs = [long_call(100, 5.0)]
    assert payoff_at(legs, 90.0) == pytest.approx(-500.0)


def test_long_call_above_breakeven_is_intrinsic_less_debit():
    legs = [long_call(100, 5.0)]
    assert payoff_at(legs, 110.0) == pytest.approx(500.0)


def test_short_put_at_zero_loses_strike_less_credit():
    legs = [short_put(100, 3.0)]
    assert payoff_at(legs, 0.0) == pytest.approx(-9700.0)


def test_short_put_expiring_worthless_keeps_the_credit():
    legs = [short_put(100, 3.0)]
    assert payoff_at(legs, 120.0) == pytest.approx(300.0)


def test_covered_call_is_capped_above_the_strike():
    # 100 shares bought at 100, short 105 call for 2. Above 105 the payoff is flat at
    # (105 - 100 + 2) * 100 = 700.
    legs = [long_stock(100.0), short_call(105, 2.0)]
    assert payoff_at(legs, 105.0) == pytest.approx(700.0)
    assert payoff_at(legs, 130.0) == pytest.approx(700.0)


def test_stock_leg_defaults_to_the_hundred_shares_backing_one_contract():
    legs = [long_stock(50.0)]
    assert payoff_at(legs, 51.0) == pytest.approx(100.0)


def test_ratio_multiplies_the_leg():
    one = payoff_at([short_put(100, 3.0)], 0.0)
    two = payoff_at([short_put(100, 3.0, ratio=2)], 0.0)
    assert two == pytest.approx(2 * one)


@pytest.mark.parametrize("legs, fragment", [
    ([], "no legs"),
    ([PayoffLeg(kind="future", side=OrderDirection.BUY, premium=1.0, strike=100)],
     "unknown leg kind"),
    ([PayoffLeg(kind="call", side=OrderDirection.BUY, premium=-1.0, strike=100)],
     "not a usable price"),
    ([PayoffLeg(kind="call", side=OrderDirection.BUY, premium=1.0, strike=None)],
     "not a usable strike"),
    ([PayoffLeg(kind="call", side=OrderDirection.BUY, premium=1.0, strike=100, ratio=0)],
     "must be positive"),
    ([PayoffLeg(kind="call", side=OrderDirection.BUY, premium=float("nan"), strike=100)],
     "not a usable price"),
])
def test_validate_legs_names_the_problem(legs, fragment):
    problem = validate_legs(legs)
    assert problem is not None and fragment in problem


def test_validate_legs_accepts_a_good_structure():
    assert validate_legs([long_call(100, 5.0), short_call(110, 2.0)]) is None


def test_a_long_call_has_unbounded_profit_and_is_not_called_unprofitable():
    """THE GUARD ORDER. A long call's payoff is NON-POSITIVE across [0, K_max] -- it is
    the debit, everywhere below the strike. A 'cannot profit anywhere' test running first
    would report every ordinary long call as UNMEASURABLE, the exact mirror of the bug
    max_loss's own ordering comment describes."""
    result = max_profit([long_call(100.0, 2.50)])
    assert result.state == UNBOUNDED
    assert result.reason is None
    # UNBOUNDED must carry NO amount. This is the "unknown reads as a number" defect that
    # MaxLossResult's docstring exists to prevent, in its profit-side form: an amount populated
    # here would be the best value on [0, K_max], i.e. the debit -- a NEGATIVE number handed
    # out under a field documented as positive dollars of profit, and a ranking signal for a
    # structure whose upside is in fact open-ended.
    assert result.amount is None


def test_a_credit_vertical_profits_at_most_its_credit():
    """Short 100c @ 3.00, long 105c @ 1.00 -> net credit 2.00/share = $200/unit."""
    legs = [short_call(100.0, 3.00), long_call(105.0, 1.00)]
    result = max_profit(legs)
    assert result.state == MEASURED
    assert result.amount == pytest.approx(200.0)


def test_a_naked_short_put_profits_at_most_its_credit():
    """Bounded ABOVE (the credit) while unbounded BELOW -- so max_profit is MEASURED on
    the very structure whose max_loss is UNBOUNDED. The two answers are independent."""
    result = max_profit([short_put(90.0, 4.00)])
    assert result.state == MEASURED
    assert result.amount == pytest.approx(400.0)


def test_a_debit_spread_bought_at_its_full_width_cannot_profit():
    """Long 100c @ 6.00, short 105c @ 1.00 = 5.00 debit for a 5.00-wide spread -- paid
    exactly AT the width, so the best outcome is exactly break-even."""
    legs = [long_call(100.0, 6.00), short_call(105.0, 1.00)]
    result = max_profit(legs)
    assert result.state == UNMEASURABLE
    assert "profit" in result.reason.lower()
    assert reported_payoff(result.reason) == 0.0


def test_paying_at_and_above_the_width_get_the_SAME_diagnosis():
    """THE MAGNITUDE OF THE SHORTFALL DOES NOT IMPLY ITS CAUSE, so the message must not
    claim it does. This is the paired assertion that used to pin a two-branch split, kept
    and inverted after review showed the split itself was the bug.

    A 5.00-wide vertical paid 5.00 (best case 0.00) and the same vertical paid 6.00 (best
    case -100.00) have the IDENTICAL fault: a builder chose strikes that cannot pay. A
    magnitude split gave them opposite diagnoses one cent apart -- the second was told the
    quote was fine, the first that the chain was stale. Both readings are in fact plausible
    for both, since a deep-ITM vertical really does quote at about its width. The refusal
    must therefore name BOTH causes and assert NEITHER.

    The mirrored split in ``max_loss`` is sound and stays: a guaranteed PROFIT cannot be
    bought in a live chain, while a guaranteed LOSS can be bought any day of the week.
    """
    at_width = max_profit([long_call(100.0, 6.00), short_call(105.0, 1.00)])
    above_width = max_profit([long_call(100.0, 7.00), short_call(105.0, 1.00)])

    assert at_width.state == UNMEASURABLE and above_width.state == UNMEASURABLE
    # Same sentence for both -- only the reported best case differs. Compared with the
    # numbers stripped out so this pins the WORDING, not the float format specifier.
    strip = lambda text: re.sub(r"-?[\d.]+", "", text)
    assert strip(at_width.reason) == strip(above_width.reason)
    # Each still reports its OWN best case. EXTRACTED AND COMPARED AS A NUMBER, not as a
    # substring: `"0.0" in ...` reads like it pins the at-width case but is vacuous, because
    # "0.0" is also a substring of "-100.0000". It therefore passed while the message
    # hardcoded -100.0000 for BOTH, i.e. it constrained exactly one of the two directions it
    # appeared to cover.
    assert reported_payoff(at_width.reason) == 0.0
    assert reported_payoff(above_width.reason) == -100.0
    # And neither asserts a single cause: both remedies are named, neither is promised.
    for reason in (at_width.reason, above_width.reason):
        assert "cannot pay" in reason
        assert "builder" in reason and "stale or crossed chain" in reason
        assert "cannot tell them apart" in reason


def test_debit_equal_to_width_is_unmeasurable_not_a_sub_cent_profit():
    """MIN_MEASURABLE_PROFIT itself, which nothing else in this file constrains.

    The mirror of ``test_credit_equal_to_width_is_unmeasurable_not_a_sub_cent_budget``, and
    it exists because of the trap that test documents: the premiums chosen for the
    break-even cases above (6.00/1.00) land on EXACT zero, so mutating the comparison to a
    bare ``best <= 0.0`` passes every one of them. Long 0.57 / short 0.07 on a half-dollar
    width is the same structure and lands at +6.217e-15 instead, which a bare ``<= 0``
    admits as MEASURED.

    A sub-cent profit is not a thin edge. It becomes the numerator of
    ``w_rr = max_profit / max_loss``, so it is a rounding artefact ranked against real
    scores -- the selection-side analogue of the absurd contract count on the loss side.
    """
    r = max_profit([long_call(100.0, 0.57), short_call(100.5, 0.07)])
    assert r.state == UNMEASURABLE
    assert r.amount is None


def test_no_measured_profit_is_ever_sub_cent_dust():
    """The general form of the case above, swept rather than sampled.

    Mirrors ``test_no_measured_loss_is_ever_small_enough_to_size_absurdly`` over the same
    grid. 120 of these 800 pairs land strictly above zero on IEEE arithmetic; every one of
    them must be refused rather than scored.
    """
    for width in (0.5, 1.0, 2.5, 5.0):
        for cents in range(1, 400):
            long_premium = round(cents * 0.01, 2)
            short_premium = round(long_premium - width, 2)
            if short_premium < 0:
                continue
            r = max_profit([long_call(100.0, long_premium),
                            short_call(100.0 + width, short_premium)])
            if r.state == MEASURED:
                assert r.amount >= MIN_MEASURABLE_PROFIT, (
                    f"long {long_premium} / short {short_premium} width {width} produced a "
                    f"MEASURED max profit of {r.amount!r}, which is floating-point dust "
                    f"ranked against real w_rr scores")


def test_empty_legs_reaches_unmeasurable_through_max_profit():
    """Coverage parity with ``max_loss``: pins the validate_legs path THROUGH this function,
    not just validate_legs in isolation."""
    r = max_profit([])
    assert r.state == UNMEASURABLE and "no legs" in r.reason
    assert r.amount is None


def test_measured_profit_amount_is_always_positive():
    """``amount`` is documented as POSITIVE dollars of profit. The mirror of the loss-side
    assertion, and the guard against a stray sign flip making it the payoff's own value."""
    r = max_profit([short_put(90.0, 4.00)])
    assert r.amount > 0


def test_every_refusal_reason_is_ascii():
    """Refusals travel to logs and consoles, and a cp1252 stream raises UnicodeEncodeError
    on an em dash -- a crash on the error path, which is the one path that must not have a
    failure mode of its own. The surrounding comments in option_payoff.py use em dashes
    freely and are safe, so copying one into a message is easy and otherwise unguarded.

    Both functions, and both the validation and the payoff-shape refusals.
    """
    unpayable = [long_call(100.0, 7.00), short_call(105.0, 1.00)]
    risk_free = [long_call(100.0, 1.00), short_call(100.0, 4.00)]
    bad_leg = [PayoffLeg(kind="call", side=OrderDirection.BUY, premium=5.0, strike=None)]

    reasons = [max_profit(unpayable).reason, max_profit(bad_leg).reason, max_profit([]).reason,
               max_loss(risk_free).reason, max_loss(bad_leg).reason, max_loss([]).reason,
               max_loss([short_call(95.0, 0.60), long_call(95.5, 0.10)]).reason]

    for reason in reasons:
        assert reason is not None
        assert reason.isascii(), f"non-ASCII characters in a refusal reason: {reason!r}"
