"""Assignment capacity: the margin-call-shaped hole in the option risk path.

The wheel depends on being ABLE to take delivery. Nothing in this codebase checked
that cash could cover assignment of **all open short puts simultaneously**.

The buying-power reserve pool is real and wired — all seven credit builders call
``option_reserve_required`` — but it answers a *different* question ("can I set aside
what this ONE new structure needs?") and it answers it in wildly different currencies
per strategy:

===========================================  ==========================================
``cash_secured_put`` / ``jade_lizard`` /      the short put's **full** ``strike x 100``
``put_ratio_spread``
``short_strangle`` / ``short_straddle`` /     **Reg-T naked margin only**, ~20% of
naked put                                     notional floored at 10%
===========================================  ==========================================

So a book of short strangles reserves roughly one fifth of what it owes if everything
is assigned at once, and nothing anywhere summed simultaneous assignment cost. A short
put **vertical** is worse still: the short leg can be assigned while the long wing is
not exercised until expiry, so the account owes the full ``strike x 100`` overnight on
a structure whose modelled max loss is only the wing width.

Assignment capacity is a **SECOND VIEW of the same legs**, not an addition to the
reserve pool. CSP, jade lizard and put ratio already reserve the full strike; charging
them again would double-charge the same cash against the same account and wrongly block
trades. The two views are each measured against their own, independently-measured
budget and neither subtracts the other — that is the whole reason they can both be
correct at once, and it is pinned by
``test_a_csp_is_not_charged_twice_the_two_views_are_independent``.

Pure means pure here too: every per-leg input arrives on ``OptionStructure`` /
``BookTotals`` / ``CandidateStructure``, and the cash arrives as an argument. Nothing in
``option_book`` fetches anything.
"""
import dataclasses

import pytest

from ba2_common.core.option_book import (
    RAIL_ASSIGNMENT_CAPACITY,
    RAIL_MAX_CONCURRENT,
    RAIL_MAX_DEPLOYMENT,
    RAIL_MAX_NOTIONAL_LEVERAGE,
    RAIL_OK,
    RAIL_ONE_PER_UNDERLYING,
    RAIL_ORDER,
    RAIL_UNDEFINED_RISK,
    RAIL_UNKNOWN_ASSIGNMENT_CASH,
    RAIL_UNMEASURABLE_BOOK,
    RAIL_UNMEASURABLE_CANDIDATE,
    BookTotals,
    BreakerState,
    CandidateStructure,
    admit,
    book_totals,
    check_rails,
)
from ba2_common.core.option_lifecycle import (
    LifecycleLeg,
    OptionStructure,
    put_assignment_cost,
)
from ba2_common.core.types import OptionRight

EQUITY = 100_000.0


# --------------------------------------------------------------------------
# builders -- deliberately roomy rails, so ONLY the capacity rail can bite
# --------------------------------------------------------------------------
def settings(**over):
    """Rails set wide enough that nothing but assignment capacity can decline.

    The headline defect is precisely that every *other* rail is satisfied: N puts each
    individually within every configured limit, collectively unaffordable.
    """
    s = {
        "max_deployment_pct": 10_000.0,
        "undefined_risk_max_pct": 10_000.0,
        "max_notional_leverage": 1_000.0,
        "max_concurrent_structures": 100,
        "circuit_breaker_pct": 20.0,
    }
    s.update(over)
    return s


def csp_candidate(underlying="XYZ", strike=100.0, qty=1.0, credit=2.0):
    """ONE cash-secured put, as a candidate. Assignment cost is the full strike."""
    assignment = strike * 100.0 * qty
    return CandidateStructure(underlying=underlying, strategy="cash_secured_put",
                              max_loss=assignment - credit * 100.0 * qty,
                              notional=assignment,
                              short_put_assignment=assignment)


def call_candidate(underlying="XYZ"):
    """A bear call spread: short premium, but NOTHING can be put to us."""
    return CandidateStructure(underlying=underlying, strategy="bear_call_spread",
                              max_loss=350.0, notional=12_000.0,
                              short_put_assignment=0.0)


def short_strangle(txn_id=1, underlying="XYZ", qty=1.0, put=225.0, call=275.0):
    """Short the 225 put AND the 275 call. Reg-T margins this at ~20% of notional."""
    legs = [LifecycleLeg(f"{underlying}_P{put:g}", net_qty=-qty, strike=put,
                         option_type=OptionRight.PUT, underlying=underlying),
            LifecycleLeg(f"{underlying}_C{call:g}", net_qty=-qty, strike=call,
                         option_type=OptionRight.CALL, underlying=underlying)]
    return OptionStructure(transaction_id=txn_id, underlying=underlying,
                           strategy="short_strangle", legs=legs, quantity=qty,
                           multiplier=100, entry_net_premium=-6.00)


def put_vertical(txn_id=1, underlying="XYZ", qty=1.0, short=100.0, long=95.0):
    """Short the 100 put, long the 95 put. Modelled max loss: the 5-wide wing."""
    legs = [LifecycleLeg(f"{underlying}_P{short:g}", net_qty=-qty, strike=short,
                         option_type=OptionRight.PUT, underlying=underlying),
            LifecycleLeg(f"{underlying}_P{long:g}", net_qty=qty, strike=long,
                         option_type=OptionRight.PUT, underlying=underlying)]
    return OptionStructure(transaction_id=txn_id, underlying=underlying,
                           strategy="put_credit_spread", legs=legs, quantity=qty,
                           multiplier=100, entry_net_premium=-2.00)


def cash_secured_put(txn_id=1, underlying="XYZ", qty=1.0, strike=100.0):
    legs = [LifecycleLeg(f"{underlying}_P{strike:g}", net_qty=-qty, strike=strike,
                         option_type=OptionRight.PUT, underlying=underlying)]
    return OptionStructure(transaction_id=txn_id, underlying=underlying,
                           strategy="cash_secured_put", legs=legs, quantity=qty,
                           multiplier=100, entry_net_premium=-2.00)


def covered_call(txn_id=1, underlying="XYZ", qty=1.0, strike=120.0):
    """A short CALL delivers SHARES on assignment. It demands no cash."""
    legs = [LifecycleLeg(f"{underlying}_C{strike:g}", net_qty=-qty, strike=strike,
                         option_type=OptionRight.CALL, underlying=underlying)]
    return OptionStructure(transaction_id=txn_id, underlying=underlying,
                           strategy="covered_call", legs=legs, quantity=qty,
                           multiplier=100, entry_net_premium=-1.50)


def long_put(txn_id=1, underlying="XYZ", qty=1.0, strike=90.0):
    """A LONG put is our right, not our obligation: nothing can be put to us."""
    legs = [LifecycleLeg(f"{underlying}_P{strike:g}", net_qty=qty, strike=strike,
                         option_type=OptionRight.PUT, underlying=underlying)]
    return OptionStructure(transaction_id=txn_id, underlying=underlying,
                           strategy="long_put", legs=legs, quantity=qty,
                           multiplier=100, entry_net_premium=3.00)


# ==========================================================================
# THE HEADLINE
# ==========================================================================
def test_four_cash_secured_puts_each_within_every_limit_are_together_more_cash_than_the_account_has():
    """THE BUG REPORT, as a test.

    Five 100-strike cash-secured puts on five different underlyings. Each one is
    individually fine: it breaches no deployment cap, no leverage multiple, no naked
    sub-cap, no concurrency limit — the rails here are deliberately set wide enough
    that not one of them can fire. Each one is also individually affordable: $10,000
    of assignment against $45,000 of cash.

    Together they are not. Four of them owe $40,000 if assigned; the fifth takes the
    book to $50,000 against $45,000 of cash. Before this rail existed nothing anywhere
    summed that, and the account discovered it as a margin call.
    """
    cash = 45_000.0
    puts = [csp_candidate(u, strike=100.0) for u in ("AAA", "BBB", "CCC", "DDD", "EEE")]

    verdicts = admit(puts, book_totals([]), EQUITY, settings(), BreakerState(), cash)

    assert [v.allowed for v in verdicts] == [True, True, True, True, False]
    last = verdicts[-1]
    assert last.reason == RAIL_ASSIGNMENT_CAPACITY
    assert "40000.00" in last.detail and "10000.00" in last.detail
    assert "45000.00" in last.detail
    # ...and the four that DID open are the whole reason: the book carries their
    # simultaneous assignment obligation, which is what the fifth was measured against.
    assert verdicts[3].book_after.short_put_assignment == pytest.approx(40_000.0)


def test_the_same_five_puts_all_open_when_the_cash_is_actually_there():
    """The rail must cost a properly funded account nothing — a capacity gate that
    declines a solvent book is a strategy switched off by a control that never fired."""
    puts = [csp_candidate(u, strike=100.0) for u in ("AAA", "BBB", "CCC", "DDD", "EEE")]
    verdicts = admit(puts, book_totals([]), EQUITY, settings(), BreakerState(), 50_000.0)
    assert [v.allowed for v in verdicts] == [True] * 5


def test_the_capacity_rail_counts_the_puts_already_held_not_just_the_new_one():
    """The book the sleeve is already carrying is the point. One more 10k put against
    a book already owing 40k needs 50k, not 10k."""
    held = [cash_secured_put(i, u, strike=100.0)
            for i, u in enumerate(("AAA", "BBB", "CCC", "DDD"), start=1)]
    book = book_totals(held)
    assert book.short_put_assignment == pytest.approx(40_000.0)

    v = check_rails(csp_candidate("EEE", strike=100.0), book, EQUITY, settings(),
                    BreakerState(), 45_000.0)
    assert v.allowed is False
    assert v.reason == RAIL_ASSIGNMENT_CAPACITY


# ==========================================================================
# the second view charges the FULL strike, whatever the reserve pool charges
# ==========================================================================
def test_a_short_strangle_book_owes_the_full_put_strike_not_the_reg_t_reserve():
    """A short strangle reserves ~20% of notional in the buying-power pool. If its put
    is assigned the account owes 100% of the strike, in cash, that night."""
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

    book = book_totals([short_strangle(1, "XYZ", qty=1.0, put=225.0, call=275.0)])
    assert book.short_put_assignment == pytest.approx(22_500.0)

    # What the buying-power pool charges for the very same structure: Reg-T naked
    # margin on a 250 spot for BOTH legs (review 2026-08-30 F10) — each 25 points out
    # of the money, each landing exactly on the 10% floor: 2,500 + 2,500. This is not
    # wrong, it is a different question. But it was the ONLY number, and it is well
    # under a QUARTER of the delivery bill.
    reg_t = OptionsAccountInterface.option_reserve_required(
        "short_strangle", 1, strike=225.0, call_strike=275.0, spot=250.0)
    assert reg_t == pytest.approx(5_000.0)
    assert book.short_put_assignment > 4 * reg_t


def test_a_short_put_vertical_owes_the_full_short_strike_not_the_wing_width():
    """The short leg can be assigned tonight; the long wing is not exercised until
    expiry. Overnight the account owes strike x 100 on a structure whose modelled max
    loss — and whose reserve — is the 5-wide wing."""
    book = book_totals([put_vertical(1, "XYZ", qty=1.0, short=100.0, long=95.0)])
    assert book.committed == pytest.approx(500.0)          # the wing width, as modelled
    assert book.short_put_assignment == pytest.approx(10_000.0)   # what delivery costs
    assert book.short_put_assignment == 20 * book.committed


def test_a_long_put_wing_does_not_reduce_the_short_legs_assignment_bill():
    """Netting the wings would be exactly the mistake: the long put is OUR right and we
    would have to choose to exercise it, on a later day, after paying for the shares."""
    wide = book_totals([put_vertical(1, "XYZ", short=100.0, long=5.0)])
    narrow = book_totals([put_vertical(1, "XYZ", short=100.0, long=99.0)])
    assert wide.short_put_assignment == narrow.short_put_assignment == pytest.approx(10_000.0)


def test_the_bill_scales_with_contracts():
    book = book_totals([cash_secured_put(1, "XYZ", qty=7.0, strike=100.0)])
    assert book.short_put_assignment == pytest.approx(70_000.0)


def test_two_short_puts_in_one_structure_both_count():
    """A put ratio spread is 1 long / 2 short. Both shorts can be assigned."""
    legs = [LifecycleLeg("XYZ_P100", net_qty=-2.0, strike=100.0,
                         option_type=OptionRight.PUT, underlying="XYZ"),
            LifecycleLeg("XYZ_P110", net_qty=1.0, strike=110.0,
                         option_type=OptionRight.PUT, underlying="XYZ")]
    ratio = OptionStructure(transaction_id=1, underlying="XYZ",
                            strategy="put_ratio_spread", legs=legs, quantity=1.0,
                            multiplier=100, entry_net_premium=-3.0)
    assert book_totals([ratio]).short_put_assignment == pytest.approx(20_000.0)


def test_two_short_puts_at_DIFFERENT_strikes_are_both_billed():
    """A put ladder shorts two strikes at once. Both can be assigned on the same night,
    so the bill is the SUM — not the widest leg, not the last one seen."""
    legs = [LifecycleLeg("XYZ_P100", net_qty=-1.0, strike=100.0,
                         option_type=OptionRight.PUT, underlying="XYZ"),
            LifecycleLeg("XYZ_P90", net_qty=-1.0, strike=90.0,
                         option_type=OptionRight.PUT, underlying="XYZ")]
    ladder = OptionStructure(transaction_id=1, underlying="XYZ",
                             strategy="short_put", legs=legs, quantity=1.0,
                             multiplier=100, entry_net_premium=-4.0)
    assert book_totals([ladder]).short_put_assignment == pytest.approx(19_000.0)


def test_a_leg_bought_back_stops_owing_anything():
    """Netting to flat is real: the short is gone, so nothing can be assigned."""
    legs = [LifecycleLeg("XYZ_P100", net_qty=0.0, strike=100.0,
                         option_type=OptionRight.PUT, underlying="XYZ")]
    closed = OptionStructure(transaction_id=1, underlying="XYZ",
                             strategy="cash_secured_put", legs=legs, quantity=1.0,
                             multiplier=100, entry_net_premium=-2.0)
    assert book_totals([closed]).short_put_assignment == pytest.approx(0.0)


def test_a_short_leg_bought_back_leaves_the_surviving_long_wing_owing_nothing():
    """The short is netted flat but the LONG wing is still held, so the structure has
    not gone flat and the early "we saw it close" path does not apply. Only legs that
    are still SHORT can be assigned; reading the raw leg list instead of the held ones
    would keep billing for a put we have already bought back."""
    legs = [LifecycleLeg("XYZ_P100", net_qty=0.0, strike=100.0,
                         option_type=OptionRight.PUT, underlying="XYZ"),
            LifecycleLeg("XYZ_P95", net_qty=1.0, strike=95.0,
                         option_type=OptionRight.PUT, underlying="XYZ")]
    half_closed = OptionStructure(transaction_id=1, underlying="XYZ",
                                  strategy="put_credit_spread", legs=legs, quantity=1.0,
                                  multiplier=100, entry_net_premium=-2.0)
    book = book_totals([half_closed])
    assert book.is_measurable
    assert book.short_put_assignment == pytest.approx(0.0)


def test_the_bill_uses_the_structures_own_contract_multiplier():
    """strike x SHARES-PER-CONTRACT. The multiplier is the field that says how many
    shares a contract delivers, and it is not always 100 (adjusted contracts)."""
    legs = [LifecycleLeg("XYZ_P100", net_qty=-1.0, strike=100.0,
                         option_type=OptionRight.PUT, underlying="XYZ")]
    adjusted = OptionStructure(transaction_id=1, underlying="XYZ",
                               strategy="cash_secured_put", legs=legs, quantity=1.0,
                               multiplier=17, entry_net_premium=-2.0)
    assert book_totals([adjusted]).short_put_assignment == pytest.approx(1_700.0)


# ==========================================================================
# SHORT CALLS: a covered call delivers shares, it does not demand cash
# ==========================================================================
def test_a_short_call_consumes_no_put_assignment_capacity():
    """Assignment of a short CALL takes shares OUT and pays cash IN. It is the exact
    opposite cash flow, and folding it into a cash-capacity total would decline trades
    for an obligation that does not exist."""
    assert book_totals([covered_call(1, "XYZ")]).short_put_assignment == pytest.approx(0.0)


def test_a_strangles_short_call_is_not_added_to_its_short_puts_bill():
    """The strangle's 275 call must contribute nothing: 22,500, not 22,500 + 27,500."""
    book = book_totals([short_strangle(1, "XYZ", put=225.0, call=275.0)])
    assert book.short_put_assignment == pytest.approx(22_500.0)


def test_a_call_only_candidate_does_not_engage_the_rail_at_all():
    """Visibly inapplicable, not silently passing: a bear call spread against an empty
    book has no assignment question, and `evaluated` says the rail never ran."""
    v = check_rails(call_candidate("AAA"), book_totals([]), EQUITY, settings(),
                    BreakerState(), 0.0)
    assert v.allowed is True
    assert RAIL_ASSIGNMENT_CAPACITY not in v.evaluated


def test_a_call_only_candidate_still_engages_the_rail_when_the_BOOK_owes():
    """A sleeve that already cannot fund delivery of what it holds may not add more
    risk of any kind — the rail is a statement about the book, not only the candidate."""
    book = book_totals([cash_secured_put(1, "AAA", strike=100.0)])
    v = check_rails(call_candidate("BBB"), book, EQUITY, settings(), BreakerState(), 5_000.0)
    assert v.allowed is False
    assert v.reason == RAIL_ASSIGNMENT_CAPACITY
    assert RAIL_ASSIGNMENT_CAPACITY in v.evaluated


def test_a_long_put_is_a_right_not_an_obligation():
    assert book_totals([long_put(1, "XYZ")]).short_put_assignment == pytest.approx(0.0)


def test_the_rail_measures_the_put_bill_and_not_the_short_side_notional():
    """A strangle's ``notional`` is the CALL strike (the larger of the two shorts), so
    on a strangle book the two numbers come apart: 27,500 of notional against a 22,500
    delivery bill. Reusing notional as the bill would decline a sleeve that can in fact
    pay — and, on a book whose largest short is a put, would silently understate."""
    book = book_totals([short_strangle(1, "XYZ", put=225.0, call=275.0)])
    assert book.notional == pytest.approx(27_500.0)
    assert book.short_put_assignment == pytest.approx(22_500.0)

    # 23,000 of cash covers the delivery bill but not the notional.
    v = check_rails(call_candidate("AAA"), book, EQUITY, settings(), BreakerState(),
                    23_000.0)
    assert v.allowed is True
    assert RAIL_ASSIGNMENT_CAPACITY in v.evaluated


# ==========================================================================
# NO DOUBLE CHARGING -- the two views are independent and each correct
# ==========================================================================
def test_a_csp_is_not_charged_twice_the_two_views_are_independent():
    """A cash-secured put ALREADY reserves the full strike x 100 in the buying-power
    pool. Assignment capacity charges the same strike again — and that is correct,
    because the two totals are measured against two independent budgets and neither
    subtracts the other. Adding short puts to the reserve pool instead would charge the
    same cash twice against the SAME budget and wrongly refuse a funded trade."""
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

    reserve = OptionsAccountInterface.option_reserve_required(
        "cash_secured_put", 1, strike=100.0)
    book = book_totals([cash_secured_put(1, "XYZ", strike=100.0)])

    assert reserve == pytest.approx(10_000.0)
    assert book.short_put_assignment == pytest.approx(10_000.0)
    # Each view charges the strike ONCE. Neither is 20,000, and neither is the sum.
    assert reserve + book.short_put_assignment == pytest.approx(20_000.0)

    # And the reserve pool is untouched by the new field: `committed` (the deployment
    # rail's basis) still sees exactly one CSP's worth of capital at risk.
    assert book.committed == pytest.approx(10_000.0)


def test_the_capacity_total_is_not_folded_into_committed_or_notional_or_outlay():
    """Four separate money fields, four separate questions. If assignment leaked into
    `committed` the deployment rail would start refusing at a fifth of its configured
    cap, and nobody would be able to tell which rail did it."""
    plain = book_totals([put_vertical(1, "XYZ", short=100.0, long=95.0)])
    assert plain.committed == pytest.approx(500.0)
    assert plain.notional == pytest.approx(10_000.0)
    assert plain.premium_outlay == pytest.approx(0.0)
    assert plain.short_put_assignment == pytest.approx(10_000.0)


def test_admitting_a_candidate_charges_the_capacity_view_and_nothing_else_twice():
    book = book_totals([])
    v = check_rails(csp_candidate("AAA", strike=100.0, credit=2.0), book, EQUITY,
                    settings(), BreakerState(), 50_000.0)
    assert v.allowed is True
    after = v.book_after
    assert after.short_put_assignment == pytest.approx(10_000.0)
    assert after.committed == pytest.approx(9_800.0)      # max_loss, not the strike
    assert after.notional == pytest.approx(10_000.0)


def test_a_refused_candidate_is_not_charged_to_the_capacity_view():
    book = book_totals([cash_secured_put(1, "AAA", strike=100.0)])
    v = check_rails(csp_candidate("BBB", strike=100.0), book, EQUITY, settings(),
                    BreakerState(), 15_000.0)
    assert v.allowed is False
    assert v.book_after.short_put_assignment == pytest.approx(10_000.0)


# ==========================================================================
# THE BOUNDARY: cash exactly equal to the assignment bill
# ==========================================================================
def test_cash_exactly_equal_to_the_assignment_bill_is_allowed():
    """DECISION: exactly equal ADMITS.

    Two reasons. (1) Consistency: every other cap in this module admits at the line
    (`test_the_deployment_rail_admits_a_structure_exactly_at_the_cap`,
    `test_the_notional_leverage_rail_admits_exactly_at_the_multiple`), and one rail
    with a different boundary is a trap nobody will remember. (2) Truth: the money is
    there. A cash-secured put with the cash secured is the definition of the structure,
    and refusing it would make a fully funded wheel un-runnable at exactly the size it
    is funded for. The safety margin belongs in the operator's cash figure, not
    smuggled into the comparison."""
    v = check_rails(csp_candidate("AAA", strike=100.0), book_totals([]), EQUITY,
                    settings(), BreakerState(), 10_000.0)
    assert v.allowed is True


def test_one_cent_past_the_bill_declines():
    """The other side of the same line, so the boundary cannot drift unnoticed."""
    v = check_rails(csp_candidate("AAA", strike=100.0), book_totals([]), EQUITY,
                    settings(), BreakerState(), 9_999.99)
    assert v.allowed is False
    assert v.reason == RAIL_ASSIGNMENT_CAPACITY


def test_the_boundary_holds_with_a_book_already_in_place():
    book = book_totals([cash_secured_put(1, "AAA", strike=100.0)])
    assert check_rails(csp_candidate("BBB", strike=100.0), book, EQUITY, settings(),
                       BreakerState(), 20_000.0).allowed is True
    assert check_rails(csp_candidate("BBB", strike=100.0), book, EQUITY, settings(),
                       BreakerState(), 19_999.99).allowed is False


# ==========================================================================
# UNMEASURABLE: never a permissive default, and it says which input was missing
# ==========================================================================
def test_a_missing_cash_figure_declines_and_says_so():
    """Unknown cash is not infinite cash. This is the input an operator most plausibly
    fails to supply, and it must be the one that shuts the gate."""
    v = check_rails(csp_candidate("AAA"), book_totals([]), EQUITY, settings(),
                    BreakerState(), None)
    assert v.allowed is False
    assert v.reason == RAIL_UNKNOWN_ASSIGNMENT_CASH
    assert "cash" in v.detail.lower()
    assert RAIL_ASSIGNMENT_CAPACITY in v.evaluated


def test_a_candidate_with_an_unknown_assignment_cost_declines_and_says_which_input():
    v = check_rails(
        CandidateStructure(underlying="AAA", strategy="cash_secured_put",
                           max_loss=9_800.0, notional=10_000.0,
                           short_put_assignment=None),
        book_totals([]), EQUITY, settings(), BreakerState(), 1_000_000.0)
    assert v.allowed is False
    assert v.reason == RAIL_UNMEASURABLE_CANDIDATE
    assert "assignment" in v.detail.lower()
    assert "cash_secured_put on AAA" in v.detail


def test_a_short_put_with_no_strike_makes_the_book_unmeasurable_not_free():
    legs = [LifecycleLeg("XYZ_P?", net_qty=-1.0, strike=None,
                         option_type=OptionRight.PUT, underlying="XYZ")]
    blind = OptionStructure(transaction_id=77, underlying="XYZ",
                            strategy="cash_secured_put", legs=legs, quantity=1.0,
                            multiplier=100, entry_net_premium=-2.0)
    book = book_totals([blind])
    assert book.short_put_assignment is None
    assert book.unmeasurable
    v = check_rails(csp_candidate("AAA"), book, EQUITY, settings(), BreakerState(),
                    1_000_000.0)
    assert v.allowed is False
    assert v.reason == RAIL_UNMEASURABLE_BOOK
    # ...and the decline names the structure, not merely the fact that one exists.
    assert "transaction 77" in v.detail


def test_a_short_put_with_no_contract_multiplier_is_unmeasurable_not_free():
    """A multiplier of 0 says nothing about how many shares would be put to us. The
    other money fields do not catch this one — `structure_metrics` hardcodes 100 — so
    without an explicit check the bill would silently be 0."""
    legs = [LifecycleLeg("XYZ_P100", net_qty=-1.0, strike=100.0,
                         option_type=OptionRight.PUT, underlying="XYZ")]
    blind = OptionStructure(transaction_id=1, underlying="XYZ",
                            strategy="cash_secured_put", legs=legs, quantity=1.0,
                            multiplier=0, entry_net_premium=-2.0)
    book = book_totals([blind])
    assert book.short_put_assignment is None
    assert any("multiplier" in u for u in book.unmeasurable)


def test_a_short_leg_of_unknown_right_is_unmeasurable_not_a_call():
    """"We do not know whether this short is a put" must not resolve to "so it is not"."""
    legs = [LifecycleLeg("XYZ_?100", net_qty=-1.0, strike=100.0,
                         option_type=None, underlying="XYZ")]
    blind = OptionStructure(transaction_id=1, underlying="XYZ",
                            strategy="short_strangle", legs=legs, quantity=1.0,
                            multiplier=100, entry_net_premium=-2.0)
    book = book_totals([blind])
    assert book.short_put_assignment is None


def test_the_assignment_summer_refuses_a_short_of_unknown_right_on_its_own():
    """Straight at the helper, because ``structure_metrics`` currently rejects a short
    with no recorded right one line earlier and would mask this.

    The guard is kept anyway and pinned here: the two checks exist for different
    reasons (metrics cannot pair the leg with a protective long; this cannot tell
    whether the leg is a put at all), and a future reordering that drops metrics'
    check first would otherwise silently start reading "unknown right" as "not a put"
    — the exact substitution this codebase keeps having to un-make."""
    from ba2_common.core.option_book import _short_put_assignment

    legs = [LifecycleLeg("XYZ_?100", net_qty=-1.0, strike=100.0,
                         option_type=None, underlying="XYZ")]
    st = OptionStructure(transaction_id=1, underlying="XYZ", strategy="short_strangle",
                         legs=legs, quantity=1.0, multiplier=100, entry_net_premium=-2.0)
    total, why = _short_put_assignment(st)
    assert total is None
    assert "option type" in why and "unknown" in why


def test_the_unmeasurable_leg_named_is_stable_whatever_order_the_legs_arrive_in():
    """The detail reaches a log and an operator. Reading the raw leg list instead of
    the contract-symbol-ordered held ones would make the message depend on the order
    the caller happened to build the structure in."""
    def ladder(order):
        legs = [LifecycleLeg(sym, net_qty=-1.0, strike=strike,
                             option_type=OptionRight.PUT, underlying="XYZ")
                for sym, strike in order]
        # multiplier 0 gets past structure_metrics (which hardcodes 100) and lands on
        # the assignment summer, which is the code under test here.
        return OptionStructure(transaction_id=1, underlying="XYZ", strategy="short_put",
                               legs=legs, quantity=1.0, multiplier=0,
                               entry_net_premium=-4.0)

    forwards = book_totals([ladder([("XYZ_P100", 100.0), ("XYZ_P200", 200.0)])])
    backwards = book_totals([ladder([("XYZ_P200", 200.0), ("XYZ_P100", 100.0)])])
    assert forwards.unmeasurable == backwards.unmeasurable
    assert "XYZ_P100" in forwards.unmeasurable[0]


def test_a_book_that_is_unmeasurable_for_the_capacity_view_declines_on_its_own_rail():
    """A hand-built BookTotals whose money fields are fine but whose assignment total
    is unknown must not sail through: the rail that needs it has to catch it."""
    half_known = BookTotals(committed=0.0, naked_committed=0.0, notional=0.0,
                            premium_outlay=0.0, short_put_assignment=None,
                            structure_count=0, underlyings=frozenset(),
                            unmeasurable=("transaction 9: no strike on the short put",))
    v = check_rails(csp_candidate("AAA"), half_known, EQUITY, settings(),
                    BreakerState(), 1_000_000.0)
    assert v.allowed is False
    assert v.reason == RAIL_UNMEASURABLE_BOOK
    assert "assignment" in v.detail.lower()
    # ...and it forwards WHICH structure, not merely that one exists.
    assert "transaction 9" in v.detail


def test_a_negative_assignment_cost_is_refused_rather_than_buying_capacity():
    v = check_rails(
        CandidateStructure(underlying="AAA", strategy="cash_secured_put",
                           max_loss=100.0, notional=100.0,
                           short_put_assignment=-50_000.0),
        book_totals([]), EQUITY, settings(), BreakerState(), 1_000.0)
    assert v.allowed is False
    assert v.reason == RAIL_UNMEASURABLE_CANDIDATE


def test_negative_cash_cannot_fund_an_assignment():
    v = check_rails(csp_candidate("AAA", strike=100.0), book_totals([]), EQUITY,
                    settings(), BreakerState(), -1.0)
    assert v.allowed is False
    assert v.reason == RAIL_ASSIGNMENT_CAPACITY


def test_a_candidate_cannot_be_built_without_stating_its_assignment_obligation():
    """No default, for the same reason ``max_loss`` and ``notional`` have none. A
    default of 0.0 would make every un-updated caller look obligation-free, and the
    whole rail would go quietly inert on exactly the structures it exists for."""
    with pytest.raises(TypeError):
        CandidateStructure(underlying="AAA", strategy="cash_secured_put",
                           max_loss=9_800.0, notional=10_000.0)


def test_the_capacity_rail_cannot_be_skipped_by_forgetting_the_cash_argument():
    """No default. 'The caller did not say' and 'the account is flush' are different
    facts — the same substitution that let the circuit-breaker stand-down gate nothing."""
    with pytest.raises(TypeError):
        check_rails(csp_candidate(), book_totals([]), EQUITY, settings(), BreakerState())
    with pytest.raises(TypeError):
        admit([csp_candidate()], book_totals([]), EQUITY, settings(), BreakerState())


# ==========================================================================
# the shared formula, and how it fits the module's conventions
# ==========================================================================
@pytest.mark.parametrize("strike,contracts,multiplier,expected", [
    (100.0, 1.0, 100, 10_000.0),
    (225.0, 3.0, 100, 67_500.0),
    (100.0, 0.0, 100, 0.0),          # no contracts: genuinely nothing to pay for
])
def test_the_shared_formula_is_strike_times_contracts_times_multiplier(
        strike, contracts, multiplier, expected):
    assert put_assignment_cost(strike, contracts, multiplier) == pytest.approx(expected)


@pytest.mark.parametrize("strike,contracts,multiplier", [
    (None, 1.0, 100),      # no strike
    (0.0, 1.0, 100),       # a zero strike is a data error, not a free put
    (-5.0, 1.0, 100),
    (100.0, None, 100),    # no contract count
    (100.0, -1.0, 100),    # a negative count would BUY capacity
    (100.0, 1.0, None),    # no multiplier
    (100.0, 1.0, 0),
    (float("nan"), 1.0, 100),
    (float("inf"), 1.0, 100),
])
def test_the_shared_formula_answers_unknown_rather_than_zero(strike, contracts, multiplier):
    assert put_assignment_cost(strike, contracts, multiplier) is None


def test_the_capacity_rail_is_last_in_the_declared_order():
    """Appended, never inserted: the recorded reason is what a decline is attributed
    as, and re-ordering would silently re-label every historical decline."""
    assert RAIL_ORDER == (RAIL_MAX_CONCURRENT, RAIL_ONE_PER_UNDERLYING,
                          RAIL_MAX_DEPLOYMENT, RAIL_MAX_NOTIONAL_LEVERAGE,
                          RAIL_UNDEFINED_RISK, RAIL_ASSIGNMENT_CAPACITY)


def test_every_rail_runs_for_a_naked_short_put_candidate():
    """The one candidate shape that engages all six."""
    v = check_rails(
        CandidateStructure(underlying="AAA", strategy="short_put", max_loss=10_000.0,
                           notional=10_000.0, short_put_assignment=10_000.0),
        book_totals([]), EQUITY, settings(), BreakerState(), 1_000_000.0)
    assert v.allowed is True
    assert v.evaluated == RAIL_ORDER


def test_a_standing_down_sleeve_never_reaches_the_capacity_rail():
    """The breaker stand-down still outranks everything, capacity included."""
    from ba2_common.core.option_book import RAIL_BREAKER_HALTED, update_breaker

    state = update_breaker(BreakerState(), 10_000.0, settings())
    state = update_breaker(state, 7_000.0, settings())
    v = check_rails(csp_candidate("AAA"), book_totals([]), EQUITY, settings(), state, None)
    assert v.reason == RAIL_BREAKER_HALTED
    assert v.evaluated == ()


def test_an_empty_book_owes_nothing_and_says_so_measurably():
    book = book_totals([])
    assert book.short_put_assignment == 0.0
    assert book.is_measurable


def test_the_candidate_and_the_totals_stay_frozen_values():
    for value in (csp_candidate(),
                  BookTotals(0.0, 0.0, 0.0, 0.0, 0.0, 0, frozenset(), ())):
        with pytest.raises(dataclasses.FrozenInstanceError):
            value.__setattr__("short_put_assignment", 1.0)


def test_the_module_is_still_pure_after_the_new_rail():
    """The capacity rail's per-leg data arrives on the values; it is never fetched."""
    from ._leakgate import check_leak

    verdict = check_leak(
        "ba2_common.core.option_book",
        ["sqlmodel", "sqlalchemy", "ba2_common.core.db", "ba2_common.core.models",
         "ba2_common.core.trade_store", "ba2_common.core.interfaces",
         "ba2_common.core.TradeConditions", "ba2_providers", "ba2_experts",
         "ba2_trade_platform"],
    )
    assert verdict == "CLEAN", f"option_book is not pure: {verdict}"


def test_the_docstring_says_this_is_a_second_view_not_an_addition_to_the_reserve():
    """Someone WILL try to 'simplify' this by adding short puts to the reserve pool.
    That double-charges CSP/jade lizard/put ratio, which already reserve the full
    strike, and wrongly blocks funded trades. The module has to say so."""
    import ba2_common.core.option_book as mod

    doc = mod.__doc__.lower()
    assert "assignment" in doc
    assert "reserve" in doc and "double" in doc
