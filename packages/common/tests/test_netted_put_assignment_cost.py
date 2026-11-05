"""Netted short-put assignment cost (operator decision 2026-10-15).

NEW rule: a long put of the SAME underlying and SAME expiry nets a short put down to
``max(0, Ks - Kl)`` per contract (the wing width); a long at/above the short covers it
fully; pairing is highest short with highest long, contract by contract; an unpaired
short owes its full strike; an unpaired long covers nothing; legs with unknown
expiry/underlying are not matched (overstating is the safe direction).

Three layers are pinned: the pure function, the account-level book view, and the
``option_book`` per-structure view.
"""
from datetime import date

import pytest

from ba2_common.core.option_book import _short_put_assignment
from ba2_common.core.option_lifecycle import (
    LifecycleLeg, OptionStructure, netted_put_assignment_cost,
)
from ba2_common.core.types import OptionRight, OrderDirection

from tests.test_option_assignment_capacity_wiring import (  # noqa: F401
    EXPIRY, FakeAccount, _own_db, _pending, held_structure,
)

M = 100


# ==========================================================================
# the pure function
# ==========================================================================
def test_a_single_vertical_owes_its_width():
    # (100 - 95) x 1 x 100
    assert netted_put_assignment_cost([(100.0, 1)], [(95.0, 1)], M) == pytest.approx(500.0)


def test_a_long_above_the_short_covers_it_fully():
    assert netted_put_assignment_cost([(95.0, 1)], [(100.0, 1)], M) == pytest.approx(0.0)
    # at the short strike: width 0
    assert netted_put_assignment_cost([(100.0, 3)], [(100.0, 3)], M) == pytest.approx(0.0)


def test_more_shorts_than_longs_the_unpaired_short_owes_its_full_strike():
    # 2 shorts @100, 1 long @95: (100 - 95) x 100 + 100 x 100 = 500 + 10,000
    assert netted_put_assignment_cost([(100.0, 2)], [(95.0, 1)], M) == pytest.approx(10_500.0)


def test_more_longs_than_shorts_the_unpaired_long_covers_nothing():
    # 1 short @100, 3 longs @95: only one pairs -> 500; the other two longs are ignored
    assert netted_put_assignment_cost([(100.0, 1)], [(95.0, 3)], M) == pytest.approx(500.0)


def test_pairing_is_highest_short_with_highest_long():
    # shorts 100 and 90, longs 95 and 80: (100,95) = 5 and (90,80) = 10 -> 15 x 100
    # (pairing 100 with 80 and 90 with 95 would give 20 + 0 = 20 x 100: not the minimum)
    assert netted_put_assignment_cost(
        [(90.0, 1), (100.0, 1)], [(80.0, 1), (95.0, 1)], M) == pytest.approx(1_500.0)


def test_pairing_walks_across_lots_contract_by_contract():
    # shorts: 3 @100; longs: 1 @98, 1 @95, 5 @50
    # contract 1 vs 98 -> 2, contract 2 vs 95 -> 5, contract 3 vs 50 -> 50 : 57 x 100
    assert netted_put_assignment_cost(
        [(100.0, 3)], [(50.0, 5), (95.0, 1), (98.0, 1)], M) == pytest.approx(5_700.0)


def test_fractional_contracts():
    # 1.5 short @100 against 0.5 long @95: 0.5 x 5 + 1.0 x 100 = 102.5 -> x 100
    assert netted_put_assignment_cost([(100.0, 1.5)], [(95.0, 0.5)], M) == pytest.approx(10_250.0)


def test_no_longs_is_the_plain_full_strike_bill():
    assert netted_put_assignment_cost([(100.0, 2)], [], M) == pytest.approx(20_000.0)


def test_empty_shorts_is_zero():
    assert netted_put_assignment_cost([], [], M) == 0.0
    assert netted_put_assignment_cost([], [(95.0, 2)], M) == 0.0


@pytest.mark.parametrize("bad_strike", [None, 0.0])
def test_an_unpriceable_short_is_unmeasurable(bad_strike):
    assert netted_put_assignment_cost([(bad_strike, 1)], [(95.0, 1)], M) is None


@pytest.mark.parametrize("bad_strike", [None, 0.0])
def test_an_unpriceable_long_is_unmeasurable(bad_strike):
    assert netted_put_assignment_cost([(100.0, 1)], [(bad_strike, 1)], M) is None


def test_an_unknown_multiplier_is_unmeasurable():
    assert netted_put_assignment_cost([(100.0, 1)], [(95.0, 1)], None) is None


# ==========================================================================
# the account's book view: short_put_assignment_exposure
# ==========================================================================
def _acct():
    return FakeAccount(spot=100.0, balance=1_000_000.0)


def test_a_held_condors_put_side_is_charged_its_width_and_the_call_side_nothing():
    acct = _acct()
    acct.hold(held_structure(
        "iron_condor", 920.0,
        (OptionRight.PUT, 90.0, 2, OrderDirection.SELL),
        (OptionRight.PUT, 85.0, 2, OrderDirection.BUY),
        (OptionRight.CALL, 110.0, 2, OrderDirection.SELL),
        (OptionRight.CALL, 115.0, 2, OrderDirection.BUY)))
    # 2 x (90 - 85) x 100 = 1,000; a short call delivers shares and takes cash IN
    exposure = acct.short_put_assignment_exposure()
    assert exposure.cost == pytest.approx(1_000.0)
    assert exposure.contracts == pytest.approx(2.0)


def test_a_long_put_with_a_DIFFERENT_expiry_does_not_net():
    acct = _acct()
    rows = held_structure("calendar_like", 0.0,
                          (OptionRight.PUT, 100.0, 1, OrderDirection.SELL),
                          (OptionRight.PUT, 95.0, 1, OrderDirection.BUY))
    rows[2].expiry = date(2024, 7, 19)          # the long expires later: protects nothing
    acct.hold(rows)
    assert acct.short_put_assignment_exposure().cost == pytest.approx(10_000.0)


def test_a_long_put_whose_expiry_is_unrecorded_does_not_net():
    acct = _acct()
    rows = held_structure("bull_put_spread", 0.0,
                          (OptionRight.PUT, 100.0, 1, OrderDirection.SELL),
                          (OptionRight.PUT, 95.0, 1, OrderDirection.BUY))
    rows[2].expiry = None
    acct.hold(rows)
    assert acct.short_put_assignment_exposure().cost == pytest.approx(10_000.0)


def test_a_short_put_whose_expiry_is_unrecorded_pays_its_full_strike():
    acct = _acct()
    rows = held_structure("bull_put_spread", 0.0,
                          (OptionRight.PUT, 100.0, 1, OrderDirection.SELL),
                          (OptionRight.PUT, 95.0, 1, OrderDirection.BUY))
    rows[1].expiry = None
    acct.hold(rows)
    assert acct.short_put_assignment_exposure().cost == pytest.approx(10_000.0)


def test_a_long_put_on_a_DIFFERENT_underlying_does_not_net():
    acct = _acct()
    acct.hold(held_structure("cash_secured_put", 0.0,
                             (OptionRight.PUT, 100.0, 1, OrderDirection.SELL), symbol="XYZ"))
    acct.hold(held_structure("long_put", 0.0,
                             (OptionRight.PUT, 95.0, 1, OrderDirection.BUY), symbol="ABC"))
    assert acct.short_put_assignment_exposure().cost == pytest.approx(10_000.0)


def test_the_same_underlying_and_expiry_long_nets_across_separate_structures():
    """The account view groups by (underlying, expiry), not by parent structure."""
    acct = _acct()
    acct.hold(held_structure("cash_secured_put", 0.0,
                             (OptionRight.PUT, 100.0, 1, OrderDirection.SELL)))
    acct.hold(held_structure("long_put", 0.0,
                             (OptionRight.PUT, 95.0, 1, OrderDirection.BUY)))
    assert acct.short_put_assignment_exposure().cost == pytest.approx(500.0)


def test_an_in_flight_short_put_is_charged_in_full_even_when_a_long_exists():
    acct = _acct()
    acct.hold(held_structure("long_put", 0.0,
                             (OptionRight.PUT, 95.0, 1, OrderDirection.BUY)))
    assert acct.short_put_assignment_exposure().cost == pytest.approx(0.0)
    acct.hold(_pending(held_structure("cash_secured_put", 10_000.0,
                                      (OptionRight.PUT, 100.0, 1, OrderDirection.SELL))))
    # not netted against the held 95 long: it can fill at any moment
    assert acct.short_put_assignment_exposure().cost == pytest.approx(10_000.0)


def test_a_held_short_is_netted_but_an_in_flight_one_beside_it_is_not():
    acct = _acct()
    acct.hold(held_structure("bull_put_spread", 0.0,
                             (OptionRight.PUT, 100.0, 1, OrderDirection.SELL),
                             (OptionRight.PUT, 95.0, 1, OrderDirection.BUY)))
    acct.hold(_pending(held_structure("cash_secured_put", 0.0,
                                      (OptionRight.PUT, 90.0, 1, OrderDirection.SELL))))
    # held vertical 500 + in-flight 90 x 100 in full
    assert acct.short_put_assignment_exposure().cost == pytest.approx(9_500.0)


def test_the_candidate_check_nets_its_own_long_puts():
    acct = _acct()
    plain = acct.check_short_put_assignment_capacity(strike=100.0, contracts=2)
    netted = acct.check_short_put_assignment_capacity(
        strike=100.0, contracts=2, long_puts=((95.0, 2),))
    assert plain.candidate_cost == pytest.approx(20_000.0)
    assert netted.candidate_cost == pytest.approx(1_000.0)      # 2 x (100 - 95) x 100
    # a long with an unusable strike makes the candidate unmeasurable, never free
    broken = acct.check_short_put_assignment_capacity(
        strike=100.0, contracts=2, long_puts=((None, 2),))
    assert broken.ok is False


# ==========================================================================
# option_book._short_put_assignment (one structure)
# ==========================================================================
def _leg(sym, qty, strike, right=OptionRight.PUT, expiry=EXPIRY):
    return LifecycleLeg(contract_symbol=sym, net_qty=qty, strike=strike,
                        option_type=right, expiry=expiry, underlying="XYZ")


def _structure(*legs, strategy="bull_put_spread"):
    return OptionStructure(transaction_id=1, underlying="XYZ", strategy=strategy,
                           legs=legs)


def test_option_book_nets_a_same_expiry_long_put_to_the_width():
    s = _structure(_leg("XYZ100P", -2, 100.0), _leg("XYZ95P", 2, 95.0))
    total, why = _short_put_assignment(s)
    assert why == ""
    assert total == pytest.approx(1_000.0)           # 2 x 5 x 100


def test_option_book_does_not_net_across_expiries():
    other = date(2024, 7, 19)
    s = _structure(_leg("XYZ100P", -1, 100.0), _leg("XYZ95P", 1, 95.0, expiry=other),
                   strategy="put_calendar")
    total, why = _short_put_assignment(s)
    assert why == ""
    assert total == pytest.approx(10_000.0)          # full strike


def test_option_book_sums_each_expiry_group_separately():
    other = date(2024, 7, 19)
    s = _structure(_leg("A100P", -1, 100.0), _leg("A95P", 1, 95.0),
                   _leg("B90P", -1, 90.0, expiry=other),
                   _leg("B80P", 1, 80.0, expiry=other))
    total, _ = _short_put_assignment(s)
    assert total == pytest.approx(500.0 + 1_000.0)


def test_option_book_ignores_calls_and_leaves_an_unpaired_short_at_full_strike():
    s = _structure(_leg("XYZ110C", -1, 110.0, OptionRight.CALL),
                   _leg("XYZ100P", -2, 100.0), _leg("XYZ95P", 1, 95.0))
    total, _ = _short_put_assignment(s)
    assert total == pytest.approx(500.0 + 10_000.0)


def test_option_book_unknown_short_strike_is_unmeasurable():
    s = _structure(_leg("XYZ100P", -1, None), _leg("XYZ95P", 1, 95.0))
    total, why = _short_put_assignment(s)
    assert total is None and why
