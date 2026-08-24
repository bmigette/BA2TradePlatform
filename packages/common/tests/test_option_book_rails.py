"""Task 7 acceptance: the PURE book rails and the sleeve circuit breaker.

Promoted out of ``OptionPortfolioManager._book_totals`` / ``_within_rails`` and the
drawdown breaker in ``manage_open``. These are the four things no *rule* can express,
because ``TradeActionEvaluator.evaluate(instrument_name, expert_recommendation, ...)``
is per-instrument by signature: a rail is a statement about the whole sleeve.

Every input here is built literally -- no DB, no broker, no clock. Equity, the held
book and the settings all arrive as arguments.

Two themes recur:

* **Unknown is never a value.** An equity we cannot read, a structure whose legs we
  cannot see and a debit whose premium was never recorded each have to *decline*, and
  say which input was missing. Never "0.0, therefore fine".
* **A rail that cannot engage must be visibly inapplicable, not silently absent.**
  ``undefined_risk_max_pct`` is gated on a strategy tuple and is genuinely dead for a
  debit arm; ``RailVerdict.evaluated`` makes that a fact a test can read.
"""
import dataclasses

import pytest

from ba2_common.core.option_book import (
    RAIL_MAX_CONCURRENT,
    RAIL_MAX_DEPLOYMENT,
    RAIL_MAX_NOTIONAL_LEVERAGE,
    RAIL_OK,
    RAIL_ONE_PER_UNDERLYING,
    RAIL_UNDEFINED_RISK,
    RAIL_UNKNOWN_EQUITY,
    RAIL_UNMEASURABLE_BOOK,
    RAIL_UNMEASURABLE_CANDIDATE,
    BookTotals,
    BreakerState,
    CandidateStructure,
    RailVerdict,
    admit,
    book_totals,
    breaker_signal,
    check_rails,
    rearm,
    update_breaker,
)
from ba2_common.core.option_lifecycle import (
    SETTING_BREAKER_TRIPPED,
    UNDEFINED_RISK_STRATEGIES,
    LifecycleLeg,
    OptionStructure,
    structure_metrics,
)
from ba2_common.core.types import OptionRight

EQUITY = 100_000.0


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def settings(**over):
    """The PremiumSeller rail defaults, spelled out. Tests override what they mean."""
    s = {
        "max_deployment_pct": 40.0,       # 40k of 100k
        "undefined_risk_max_pct": 20.0,   # 20k of 100k
        "max_notional_leverage": 3.0,     # 300k of 100k
        "max_concurrent_structures": 10,
        "circuit_breaker_pct": 20.0,
    }
    s.update(over)
    return s


def candidate(underlying="XYZ", strategy="put_credit_spread", max_loss=450.0,
              notional=9_500.0, **kw):
    return CandidateStructure(underlying=underlying, strategy=strategy,
                              max_loss=max_loss, notional=notional, **kw)


def put_spread(txn_id=1, underlying="XYZ", qty=1.0, short=100.0, long=95.0):
    """Short the 100 put, long the 95 put: defined risk, 500/contract committed."""
    legs = [LifecycleLeg(f"{underlying}_P{short:g}", net_qty=-qty, strike=short,
                         option_type=OptionRight.PUT, underlying=underlying),
            LifecycleLeg(f"{underlying}_P{long:g}", net_qty=qty, strike=long,
                         option_type=OptionRight.PUT, underlying=underlying)]
    return OptionStructure(transaction_id=txn_id, underlying=underlying,
                           strategy="put_credit_spread", legs=legs, quantity=qty,
                           multiplier=100, entry_net_premium=-2.00)


def naked_put(txn_id=1, underlying="XYZ", qty=1.0, strike=100.0):
    legs = [LifecycleLeg(f"{underlying}_P{strike:g}", net_qty=-qty, strike=strike,
                         option_type=OptionRight.PUT, underlying=underlying)]
    return OptionStructure(transaction_id=txn_id, underlying=underlying,
                           strategy="short_put", legs=legs, quantity=qty,
                           multiplier=100, entry_net_premium=-2.00)


def long_call(txn_id=1, underlying="XYZ", qty=1.0, strike=105.0, debit=3.00):
    """The buy arm's bread and butter: a pure-debit, long-only structure."""
    legs = [LifecycleLeg(f"{underlying}_C{strike:g}", net_qty=qty, strike=strike,
                         option_type=OptionRight.CALL, underlying=underlying)]
    return OptionStructure(transaction_id=txn_id, underlying=underlying,
                           strategy="long_call", legs=legs, quantity=qty,
                           multiplier=100, entry_net_premium=abs(debit))


# --------------------------------------------------------------------------
# book totals -- the promoted _book_totals
# --------------------------------------------------------------------------
def test_book_totals_add_up_committed_naked_and_notional_over_the_held_book():
    book = book_totals([put_spread(1, "AAA"), naked_put(2, "BBB", strike=50.0)])
    assert book.committed == pytest.approx(500.0 + 5_000.0)
    assert book.naked_committed == pytest.approx(5_000.0)
    assert book.notional == pytest.approx(10_000.0 + 5_000.0)
    assert book.structure_count == 2
    assert book.underlyings == frozenset({"AAA", "BBB"})


def test_an_empty_book_is_zero_and_measurable_not_unknown():
    book = book_totals([])
    assert book.committed == 0.0 and book.naked_committed == 0.0
    assert book.notional == 0.0 and book.premium_outlay == 0.0
    assert book.is_measurable is True
    assert book.structure_count == 0


def test_the_naked_total_counts_only_the_undefined_risk_structures():
    book = book_totals([put_spread(1, "AAA"), put_spread(2, "BBB")])
    assert book.committed == pytest.approx(1_000.0)
    assert book.naked_committed == pytest.approx(0.0)


def test_a_leg_bought_back_stops_counting_toward_deployment():
    """_txn_metrics bucketed by ORDER SIDE and never netted, so a buy-to-close landed
    in `longs` while the original short stayed in `shorts` forever -- committed capital
    could only ever rise. Here the short is flat; only the long wing is held."""
    legs = [LifecycleLeg("PS", net_qty=0.0, strike=100.0, option_type=OptionRight.PUT),
            LifecycleLeg("PL", net_qty=1.0, strike=95.0, option_type=OptionRight.PUT)]
    st = OptionStructure(1, "XYZ", "put_credit_spread", legs, quantity=1.0,
                         multiplier=100, entry_net_premium=-2.00)
    book = book_totals([st])
    assert book.committed == pytest.approx(0.0)
    assert book.notional == pytest.approx(0.0)


def test_a_fully_closed_structure_counts_as_no_deployment_not_as_unknown():
    """Every leg netted flat: we SAW the legs and they are gone. That is a measured
    zero, unlike a structure whose legs we never saw at all."""
    legs = [LifecycleLeg("PS", net_qty=0.0, strike=100.0, option_type=OptionRight.PUT),
            LifecycleLeg("PL", net_qty=0.0, strike=95.0, option_type=OptionRight.PUT)]
    st = OptionStructure(1, "XYZ", "put_credit_spread", legs, quantity=1.0,
                         multiplier=100, entry_net_premium=-2.00)
    book = book_totals([st])
    assert book.is_measurable is True
    assert book.committed == pytest.approx(0.0)
    assert book.notional == pytest.approx(0.0)
    # It still occupies its slot and its underlying, exactly as len(holdings) and
    # {txn.symbol} did -- the transaction is OPENED until the netting resolves it.
    assert book.structure_count == 1
    assert book.underlyings == frozenset({"XYZ"})


def test_a_structure_with_no_legs_recorded_at_all_is_unknown_not_zero():
    """(True, 0.0, 0.0) for 'I cannot see any legs' is the all-zeros regime that made
    every rail unreachable on live multi-legs before the leg reconciliation."""
    st = OptionStructure(1, "XYZ", "put_credit_spread", [], quantity=1.0,
                         multiplier=100, entry_net_premium=-2.00)
    book = book_totals([st])
    assert book.is_measurable is False
    assert book.committed is None and book.notional is None
    assert book.naked_committed is None
    assert any("no held option legs" in u for u in book.unmeasurable)


def test_one_unmeasurable_structure_makes_the_whole_total_unknown():
    """A sum with a missing addend is not a smaller sum; it is an unknown sum."""
    blind = OptionStructure(2, "BBB", "short_put",
                            [LifecycleLeg("P", net_qty=-1.0, strike=None,
                                          option_type=OptionRight.PUT)])
    book = book_totals([put_spread(1, "AAA"), blind])
    assert book.is_measurable is False
    assert book.committed is None
    assert any("strike" in u for u in book.unmeasurable)
    # ...but the caps still work: the count and the underlyings were never in doubt.
    # Dropping an unmeasurable structure from them would let the sleeve open a SECOND
    # structure on the very underlying it cannot measure.
    assert book.structure_count == 2
    assert book.underlyings == frozenset({"AAA", "BBB"})
    assert check_rails(candidate("BBB"), book, EQUITY, settings(), BreakerState()).reason == \
        RAIL_ONE_PER_UNDERLYING


def test_the_same_held_book_totals_the_same_whatever_order_it_arrives_in():
    """`unmeasurable` is read back by a human and compared verbatim by the live/backtest
    parity test, so it cannot depend on the order the caller happened to iterate its
    holdings in."""
    a = OptionStructure(1, "AAA", "put_credit_spread", [])
    b = OptionStructure(2, "BBB", "short_put",
                        [LifecycleLeg("P", net_qty=-1.0, strike=None,
                                      option_type=OptionRight.PUT)])
    c = put_spread(3, "CCC")
    assert book_totals([a, b, c]) == book_totals([c, b, a])
    assert book_totals([a, b, c]).unmeasurable[0].startswith("transaction 1:")


def test_a_measurable_book_totals_the_same_whatever_order_it_arrives_in():
    held = [put_spread(1, "AAA"), naked_put(2, "BBB", strike=50.0),
            long_call(3, "CCC", debit=3.0)]
    assert book_totals(held) == book_totals(list(reversed(held)))


# --------------------------------------------------------------------------
# the three percentage rails
# --------------------------------------------------------------------------
def test_the_deployment_rail_refuses_a_structure_past_max_deployment_pct():
    """40% of 100k = 40k. 39.6k already committed + a 500 candidate = 40.1k."""
    book = dataclasses.replace(book_totals([]), committed=39_600.0)
    v = check_rails(candidate(max_loss=500.0), book, EQUITY, settings(), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_MAX_DEPLOYMENT
    assert "40" in v.detail


def test_the_deployment_rail_admits_a_structure_exactly_at_the_cap():
    """The promoted comparison is a strict `>`: landing exactly on the cap is legal."""
    book = dataclasses.replace(book_totals([]), committed=39_500.0)
    v = check_rails(candidate(max_loss=500.0, notional=0.0), book, EQUITY, settings(), BreakerState())
    assert v.allowed is True
    assert v.reason == RAIL_OK


def test_the_deployment_rail_counts_the_book_already_held():
    """The rail is about the sleeve, not about the candidate on its own: a 500 candidate
    that is fine against an empty book is refused against a nearly-full one."""
    empty = book_totals([])
    assert check_rails(candidate(max_loss=500.0, notional=0.0), empty, EQUITY,
                       settings(), BreakerState()).allowed is True
    full = dataclasses.replace(empty, committed=39_999.0)
    assert check_rails(candidate(max_loss=500.0, notional=0.0), full, EQUITY,
                       settings(), BreakerState()).allowed is False


def test_the_notional_leverage_rail_refuses_a_structure_past_the_multiple():
    """3.0 x 100k = 300k of short notional."""
    book = dataclasses.replace(book_totals([]), notional=295_000.0)
    v = check_rails(candidate(max_loss=100.0, notional=6_000.0), book, EQUITY, settings(), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_MAX_NOTIONAL_LEVERAGE


def test_the_notional_leverage_rail_admits_exactly_at_the_multiple():
    book = dataclasses.replace(book_totals([]), notional=295_000.0)
    v = check_rails(candidate(max_loss=100.0, notional=5_000.0), book, EQUITY, settings(), BreakerState())
    assert v.allowed is True


def test_the_undefined_risk_rail_refuses_a_naked_structure_past_its_own_cap():
    """20% of 100k = 20k of NAKED committed, on top of the 40% overall cap."""
    book = dataclasses.replace(book_totals([]), naked_committed=19_800.0,
                               committed=19_800.0)
    v = check_rails(candidate(strategy="short_put", max_loss=500.0, notional=10_000.0),
                    book, EQUITY, settings(), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_UNDEFINED_RISK


def test_the_undefined_risk_rail_admits_a_structure_exactly_at_the_naked_cap():
    """`>` not `>=`, the same way the other two rails are written. Landing exactly on
    20% of equity is legal."""
    book = dataclasses.replace(book_totals([]), naked_committed=19_500.0,
                               committed=19_500.0)
    v = check_rails(candidate(strategy="short_put", max_loss=500.0, notional=10_000.0),
                    book, EQUITY, settings(), BreakerState())
    assert v.allowed is True
    assert RAIL_UNDEFINED_RISK in v.evaluated


def test_the_undefined_risk_rail_leaves_a_defined_risk_candidate_alone():
    """Same naked book, same size -- a defined-risk candidate is not charged to it."""
    book = dataclasses.replace(book_totals([]), naked_committed=19_800.0,
                               committed=19_800.0)
    v = check_rails(candidate(strategy="put_credit_spread", max_loss=500.0,
                              notional=10_000.0), book, EQUITY, settings(), BreakerState())
    assert v.allowed is True
    assert RAIL_UNDEFINED_RISK not in v.evaluated


def test_the_undefined_risk_rail_covers_both_strategies_the_setting_names():
    book = dataclasses.replace(book_totals([]), naked_committed=19_800.0,
                               committed=19_800.0)
    for strategy in UNDEFINED_RISK_STRATEGIES:
        v = check_rails(candidate(strategy=strategy, max_loss=500.0, notional=10_000.0),
                        book, EQUITY, settings(), BreakerState())
        assert v.allowed is False, strategy
        assert v.reason == RAIL_UNDEFINED_RISK


def test_an_undeclared_strategy_measured_as_naked_still_hits_the_undefined_risk_rail():
    """The gate is a hardcoded ("short_put", "short_strangle") tuple, so a naked
    structure under any other name skipped the rail silently. A candidate that MEASURES
    as undefined risk is charged to the naked cap whatever it calls itself."""
    book = dataclasses.replace(book_totals([]), naked_committed=19_800.0,
                               committed=19_800.0)
    v = check_rails(candidate(strategy="ratio_spread", max_loss=500.0,
                              notional=10_000.0, is_defined_risk=False),
                    book, EQUITY, settings(), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_UNDEFINED_RISK


def test_the_rails_evaluated_are_reported_so_an_inert_rail_is_visible_not_silent():
    """`undefined_risk_max_pct` is dead for a debit arm -- correctly so, a long option
    has no undefined risk. Dead must be READABLE: a rail that never ran is a fact, not
    a silence, or it becomes the next 'gene that could not fire'."""
    book = book_totals([])
    debit = check_rails(candidate(strategy="long_call", max_loss=300.0, notional=0.0),
                        book, EQUITY, settings(), BreakerState())
    assert RAIL_UNDEFINED_RISK not in debit.evaluated
    assert RAIL_MAX_DEPLOYMENT in debit.evaluated
    assert RAIL_MAX_NOTIONAL_LEVERAGE in debit.evaluated

    naked = check_rails(candidate(strategy="short_put", max_loss=300.0, notional=1.0),
                        book, EQUITY, settings(), BreakerState())
    assert RAIL_UNDEFINED_RISK in naked.evaluated


# --------------------------------------------------------------------------
# the two caps
# --------------------------------------------------------------------------
def test_the_concurrent_cap_refuses_a_structure_past_the_limit():
    book = dataclasses.replace(book_totals([]), structure_count=10)
    v = check_rails(candidate(), book, EQUITY, settings(max_concurrent_structures=10), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_MAX_CONCURRENT


def test_the_concurrent_cap_is_a_ceiling_not_an_off_by_one():
    """max=2 means TWO open structures, not one and not three. The promoted test is
    `len(holdings) + len(submitted) >= max`, so the second admission is the last."""
    verdicts = admit([candidate("A"), candidate("B"), candidate("C")],
                     book_totals([]), EQUITY, settings(max_concurrent_structures=2), BreakerState())
    assert [v.allowed for v in verdicts] == [True, True, False]
    assert verdicts[2].reason == RAIL_MAX_CONCURRENT


def test_the_concurrent_cap_counts_the_structures_already_held():
    """One held plus one admitted reaches a cap of two; the next is refused."""
    verdicts = admit([candidate("A"), candidate("B")], book_totals([put_spread(1, "Z")]),
                     EQUITY, settings(max_concurrent_structures=2), BreakerState())
    assert [v.allowed for v in verdicts] == [True, False]


def test_the_cap_admits_the_last_slot_rather_than_refusing_it():
    verdicts = admit([candidate("A")], book_totals([put_spread(1, "Z")]), EQUITY,
                     settings(max_concurrent_structures=2), BreakerState())
    assert verdicts[0].allowed is True


def test_only_one_structure_per_underlying():
    book = book_totals([put_spread(1, "XYZ")])
    assert check_rails(candidate("XYZ"), book, EQUITY, settings(), BreakerState()).reason == \
        RAIL_ONE_PER_UNDERLYING
    assert check_rails(candidate("ABC"), book, EQUITY, settings(), BreakerState()).allowed is True


def test_the_one_per_underlying_cap_is_keyed_on_the_underlying_not_the_contract_symbol():
    """The held structure's legs are `XYZ_P100` / `XYZ_P95`; the candidate says `XYZ`.
    Keyed on the contract symbol nothing would ever match and the cap would be inert."""
    book = book_totals([put_spread(1, "XYZ")])
    assert book.underlyings == frozenset({"XYZ"})
    v = check_rails(candidate("XYZ"), book, EQUITY, settings(), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_ONE_PER_UNDERLYING


def test_two_candidates_on_one_underlying_in_a_single_pass_admit_only_the_first():
    verdicts = admit([candidate("XYZ"), candidate("XYZ")], book_totals([]), EQUITY,
                     settings(), BreakerState())
    assert [v.allowed for v in verdicts] == [True, False]
    assert verdicts[1].reason == RAIL_ONE_PER_UNDERLYING


def test_admit_charges_each_admission_to_the_running_book():
    """The promoted loop did `book[0] += spec.max_loss` after every submission -- three
    20k candidates cannot all fit under a 40k deployment cap just because each one fits
    on its own."""
    verdicts = admit([candidate("A", max_loss=20_000.0, notional=0.0),
                      candidate("B", max_loss=20_000.0, notional=0.0),
                      candidate("C", max_loss=20_000.0, notional=0.0)],
                     book_totals([]), EQUITY, settings(), BreakerState())
    assert [v.allowed for v in verdicts] == [True, True, False]
    assert verdicts[2].reason == RAIL_MAX_DEPLOYMENT


def test_a_refused_candidate_is_not_charged_to_the_book():
    """A decline must not consume the sleeve: the oversized candidate is skipped and the
    small one that follows it still fits."""
    verdicts = admit([candidate("A", max_loss=39_000.0, notional=0.0),
                      candidate("B", max_loss=2_000.0, notional=0.0),
                      candidate("C", max_loss=500.0, notional=0.0)],
                     book_totals([]), EQUITY, settings(), BreakerState())
    assert [v.allowed for v in verdicts] == [True, False, True]


def test_admitting_a_debit_candidate_charges_the_running_books_premium_outlay():
    """A candidate with no short notional is a debit structure, so its max loss IS the
    premium it will pay. Charging it to `committed` without also charging it to
    `premium_outlay` would leave the running book internally inconsistent."""
    verdicts = admit([candidate("A", strategy="long_call", max_loss=600.0, notional=0.0)],
                     book_totals([]), EQUITY, settings(), BreakerState())
    assert verdicts[0].allowed is True
    assert verdicts[0].book_after.committed == pytest.approx(600.0)
    assert verdicts[0].book_after.premium_outlay == pytest.approx(600.0)
    assert verdicts[0].book_after.notional == pytest.approx(0.0)


def test_admitting_a_credit_candidate_charges_notional_and_not_the_outlay():
    verdicts = admit([candidate("A", strategy="short_put", max_loss=9_800.0,
                                notional=10_000.0)],
                     book_totals([]), EQUITY, settings(), BreakerState())
    after = verdicts[0].book_after
    assert after.committed == pytest.approx(9_800.0)
    assert after.naked_committed == pytest.approx(9_800.0)
    assert after.notional == pytest.approx(10_000.0)
    assert after.premium_outlay == pytest.approx(0.0)
    assert after.structure_count == 1
    assert after.underlyings == frozenset({"A"})


def test_a_refusal_leaves_the_running_book_untouched():
    verdicts = admit([candidate("A", max_loss=500_000.0, notional=0.0)],
                     book_totals([]), EQUITY, settings(), BreakerState())
    assert verdicts[0].allowed is False
    assert verdicts[0].book_after.committed == pytest.approx(0.0)
    assert verdicts[0].book_after.structure_count == 0


def test_admit_returns_one_verdict_per_candidate_in_the_order_given():
    verdicts = admit([candidate("C"), candidate("A"), candidate("B")], book_totals([]),
                     EQUITY, settings(), BreakerState())
    assert [v.candidate.underlying for v in verdicts] == ["C", "A", "B"]


def test_no_candidates_is_no_verdicts():
    assert admit([], book_totals([]), EQUITY, settings(), BreakerState()) == []


# --------------------------------------------------------------------------
# unknown declines -- never assumes
# --------------------------------------------------------------------------
def test_an_unknown_account_equity_declines_rather_than_assuming():
    """PremiumSeller's rails declined when balance was unknown rather than fabricating.
    Keep it."""
    v = check_rails(candidate(), book_totals([]), None, settings(), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_UNKNOWN_EQUITY
    assert "balance" in v.detail or "equity" in v.detail


def test_a_zero_or_negative_equity_declines():
    for equity in (0.0, -1.0):
        v = check_rails(candidate(), book_totals([]), equity, settings(), BreakerState())
        assert v.allowed is False, equity
        assert v.reason == RAIL_UNKNOWN_EQUITY


def test_an_unmeasurable_book_declines_rather_than_assuming_zero():
    """A book we cannot total is not an empty book. Admitting against it is exactly the
    all-zeros regime that let a live sleeve open structures without limit."""
    blind = OptionStructure(1, "AAA", "put_credit_spread", [])
    v = check_rails(candidate("XYZ"), book_totals([blind]), EQUITY, settings(), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_UNMEASURABLE_BOOK
    assert "no held option legs" in v.detail


def test_a_candidate_with_an_unknown_max_loss_declines():
    v = check_rails(candidate(max_loss=None), book_totals([]), EQUITY, settings(), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_UNMEASURABLE_CANDIDATE
    assert "max_loss" in v.detail


def test_a_candidate_with_an_unknown_notional_declines():
    v = check_rails(candidate(notional=None), book_totals([]), EQUITY, settings(), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_UNMEASURABLE_CANDIDATE
    assert "notional" in v.detail


def test_a_candidate_that_claims_to_risk_nothing_at_all_is_refused():
    """No option structure risks nothing and controls nothing. Zero on BOTH measures is
    the signature of an unmeasured spec, and it is the exact shape `_txn_metrics`
    returned -- (True, 0.0, 0.0) -- for a debit structure it could not see."""
    v = check_rails(candidate(max_loss=0.0, notional=0.0), book_totals([]), EQUITY,
                    settings(), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_UNMEASURABLE_CANDIDATE


def test_a_negative_max_loss_is_refused_rather_than_relieving_the_book():
    """A negative addend would BUY room under the deployment cap."""
    v = check_rails(candidate(max_loss=-500.0), book_totals([]), EQUITY, settings(), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_UNMEASURABLE_CANDIDATE


# --------------------------------------------------------------------------
# the debit book -- the one deliberate extension beyond a faithful promotion
# --------------------------------------------------------------------------
def test_a_long_only_book_reports_its_premium_outlay_not_zero():
    """`_txn_metrics` returned (True, 0.0, 0.0) with no executed SELL leg, so a
    pure-debit structure reported zero deployment and the rails never engaged at all.
    A debit's exposure is what it paid, which is also its max loss."""
    book = book_totals([long_call(1, "AAA", qty=2.0, debit=3.00)])
    assert book.premium_outlay == pytest.approx(600.0)   # 3.00 x 2 x 100
    assert book.committed == pytest.approx(600.0)
    assert book.naked_committed == pytest.approx(0.0)
    assert book.notional == pytest.approx(0.0)           # nothing is short


def test_a_long_only_book_engages_the_deployment_rail():
    """The point of the extension: with the promoted numbers this candidate was always
    admitted, because the whole held debit book measured as zero."""
    held = [long_call(i, u, qty=100.0, debit=3.95)      # 39.5k of premium already out
            for i, u in enumerate(("AAA",), start=1)]
    book = book_totals(held)
    assert book.committed == pytest.approx(39_500.0)
    v = check_rails(candidate("BBB", strategy="long_call", max_loss=600.0, notional=0.0),
                    book, EQUITY, settings(), BreakerState())
    assert v.allowed is False
    assert v.reason == RAIL_MAX_DEPLOYMENT


def test_a_debit_structure_with_an_unknown_entry_premium_is_unknown_not_free():
    st = dataclasses.replace(long_call(1, "AAA"), entry_net_premium=None)
    book = book_totals([st])
    assert book.is_measurable is False
    assert book.premium_outlay is None
    assert any("premium" in u for u in book.unmeasurable)


def test_a_debit_structure_with_no_quantity_is_unknown_not_free():
    st = dataclasses.replace(long_call(1, "AAA"), quantity=0.0)
    book = book_totals([st])
    assert book.is_measurable is False
    assert any("quantity" in u for u in book.unmeasurable)


def test_a_debit_structure_with_no_multiplier_is_unknown_not_free():
    st = dataclasses.replace(long_call(1, "AAA"), multiplier=0)
    book = book_totals([st])
    assert book.is_measurable is False
    assert any("multiplier" in u for u in book.unmeasurable)


def test_a_long_wing_left_over_from_a_closed_credit_spread_owes_no_outlay():
    """All-long and held is NOT the same as bought for a debit: a credit spread whose
    short has been bought back leaves the long wing outstanding. Its entry premium is a
    CREDIT, nothing further was paid, and charging the sleeve for it would invent an
    exposure."""
    legs = [LifecycleLeg("PS", net_qty=0.0, strike=100.0, option_type=OptionRight.PUT),
            LifecycleLeg("PL", net_qty=1.0, strike=95.0, option_type=OptionRight.PUT)]
    st = OptionStructure(1, "XYZ", "put_credit_spread", legs, quantity=1.0,
                         multiplier=100, entry_net_premium=-2.00)
    book = book_totals([st])
    assert book.is_measurable is True
    assert book.premium_outlay == pytest.approx(0.0)
    assert book.committed == pytest.approx(0.0)


def test_a_debit_vertical_is_measured_on_its_short_side_not_double_counted():
    """A structure with a held short is governed by the short-side committed measure,
    exactly as before. Adding its debit on top would charge the sleeve twice."""
    legs = [LifecycleLeg("CL", net_qty=1.0, strike=100.0, option_type=OptionRight.CALL),
            LifecycleLeg("CS", net_qty=-1.0, strike=105.0, option_type=OptionRight.CALL)]
    st = OptionStructure(1, "XYZ", "call_debit_spread", legs, quantity=1.0,
                         multiplier=100, entry_net_premium=2.00)
    book = book_totals([st])
    assert book.premium_outlay == pytest.approx(0.0)
    assert book.committed == pytest.approx(500.0)     # the wing width, as promoted


@pytest.mark.parametrize("build", [
    pytest.param(lambda: naked_put(1, "XYZ"), id="naked_put"),
    pytest.param(lambda: put_spread(1, "XYZ"), id="put_credit_spread"),
    pytest.param(lambda: OptionStructure(
        1, "XYZ", "call_credit_spread",
        [LifecycleLeg("CS", net_qty=-1.0, strike=100.0, option_type=OptionRight.CALL),
         LifecycleLeg("CL", net_qty=1.0, strike=105.0, option_type=OptionRight.CALL)],
        quantity=1.0, multiplier=100, entry_net_premium=-1.50), id="call_credit_spread"),
    pytest.param(lambda: OptionStructure(
        1, "XYZ", "iron_condor",
        [LifecycleLeg("PS", net_qty=-1.0, strike=90.0, option_type=OptionRight.PUT),
         LifecycleLeg("PL", net_qty=1.0, strike=85.0, option_type=OptionRight.PUT),
         LifecycleLeg("CS", net_qty=-1.0, strike=110.0, option_type=OptionRight.CALL),
         LifecycleLeg("CL", net_qty=1.0, strike=115.0, option_type=OptionRight.CALL)],
        quantity=1.0, multiplier=100, entry_net_premium=-2.00), id="iron_condor"),
    pytest.param(lambda: OptionStructure(
        1, "XYZ", "short_strangle",
        [LifecycleLeg("PS", net_qty=-1.0, strike=90.0, option_type=OptionRight.PUT),
         LifecycleLeg("CS", net_qty=-1.0, strike=110.0, option_type=OptionRight.CALL)],
        quantity=1.0, multiplier=100, entry_net_premium=-4.00), id="short_strangle"),
    pytest.param(lambda: OptionStructure(
        1, "XYZ", "ratio_spread",
        [LifecycleLeg("PS", net_qty=-2.0, strike=100.0, option_type=OptionRight.PUT),
         LifecycleLeg("PL", net_qty=1.0, strike=95.0, option_type=OptionRight.PUT)],
        quantity=1.0, multiplier=100, entry_net_premium=-3.00), id="ratio_spread"),
])
def test_the_debit_extension_changes_no_credit_structure_number(build):
    """The evidence that this is additive. Every structure carrying a held SHORT leg
    reports exactly what `structure_metrics` alone reports -- committed, naked and
    notional -- and contributes no premium outlay at all."""
    st = build()
    m = structure_metrics(st)
    book = book_totals([st])
    assert book.committed == pytest.approx(m.committed)
    assert book.notional == pytest.approx(m.notional)
    assert book.naked_committed == pytest.approx(0.0 if m.is_defined_risk else m.committed)
    assert book.premium_outlay == pytest.approx(0.0)


def test_the_leverage_rail_stays_a_short_side_measure_on_a_mixed_book():
    """A debit structure adds outlay to the deployment rail and NOTHING to the leverage
    rail: a long option cannot be assigned against you, so it carries no short notional.
    The two rails measure different things and the split is deliberate."""
    book = book_totals([naked_put(1, "AAA", strike=50.0), long_call(2, "BBB", debit=3.0)])
    assert book.notional == pytest.approx(5_000.0)              # the naked put alone
    assert book.committed == pytest.approx(5_000.0 + 300.0)     # plus the debit paid
    assert book.premium_outlay == pytest.approx(300.0)


# --------------------------------------------------------------------------
# the sleeve circuit breaker
# --------------------------------------------------------------------------
def test_the_breaker_trips_on_peak_to_trough_sleeve_drawdown():
    state = update_breaker(BreakerState(), 10_000.0, settings())
    assert state.tripped is False and state.peak_equity == pytest.approx(10_000.0)
    state = update_breaker(state, 7_000.0, settings())     # -30% against a 20% breaker
    assert state.tripped is True
    assert state.halted is True
    assert "30" in state.detail or "drawdown" in state.detail


def test_the_breaker_measures_peak_to_trough_not_trough_to_peak():
    """10,000 -> 8,200 is an 18% peak-to-trough drawdown, under a 20% breaker. Measured
    the other way round -- (peak-trough)/trough -- it is 21.95% and would flatten a book
    that is fine. The denominator IS the rail."""
    state = update_breaker(BreakerState(), 10_000.0, settings())
    state = update_breaker(state, 8_200.0, settings())
    assert state.tripped is False
    assert state.halted is False


def test_the_breaker_is_inclusive_at_the_threshold():
    """Exactly -20% trips: the promoted comparison is `<=`."""
    state = update_breaker(BreakerState(), 10_000.0, settings())
    state = update_breaker(state, 8_000.0, settings())
    assert state.tripped is True


def test_a_hair_short_of_the_threshold_does_not_trip():
    state = update_breaker(BreakerState(), 10_000.0, settings())
    state = update_breaker(state, 8_000.01, settings())
    assert state.tripped is False


def test_the_peak_ratchets_up_and_never_down():
    state = BreakerState()
    for equity in (10_000.0, 12_000.0, 11_000.0, 9_500.0):
        state = update_breaker(state, equity, settings())
    assert state.peak_equity == pytest.approx(12_000.0)
    # 9,500 against a 12,000 peak is -20.8%, so the ratchet is what trips it.
    assert state.tripped is True


def test_the_breaker_latches_and_stands_the_sleeve_down():
    """`_halted` short-circuits manage_open entirely on later bars: the flatten happens
    once, not every bar, and no other exit runs while standing down."""
    state = update_breaker(BreakerState(), 10_000.0, settings())
    state = update_breaker(state, 7_000.0, settings())
    assert (state.tripped, state.halted) == (True, True)
    state = update_breaker(state, 7_000.0, settings())
    assert state.halted is True
    assert state.tripped is False, "the flatten edge must be reported once, not re-fired"


def test_a_full_recovery_clears_the_latch_on_its_own():
    """REVERSED, deliberately: this test used to assert that a recovery changed nothing
    because "only a new entry cycle re-arms the sleeve".

    That was coherent only while entry was ungated. Now that a stand-down declines every
    candidate (``check_rails``), an entry-triggered clear can never fire — blocked
    because halted, halted because never entered — so the clear has to be something the
    sleeve can reach while flat. A recovery is that something, and it is also the only
    clear that does not hand the sleeve straight back to the breaker: the peak is kept,
    so re-arming anywhere below the trip line re-trips on the next managed bar."""
    state = update_breaker(BreakerState(), 10_000.0, settings())
    state = update_breaker(state, 7_000.0, settings())
    assert state.halted is True
    state = update_breaker(state, 10_500.0, settings())
    assert state.halted is False
    assert state.peak_equity == pytest.approx(10_500.0)   # and the peak still ratchets


def test_re_arming_clears_the_latch_and_keeps_the_peak():
    """`rebalance` opens a new entry cycle with `self._halted = False`, and never
    touched `_peak_equity`. Resetting the peak on re-arm would hide the drawdown that
    caused the stand-down in the first place."""
    state = update_breaker(BreakerState(), 10_000.0, settings())
    state = update_breaker(state, 7_000.0, settings())
    state = rearm(state)
    assert state.halted is False and state.tripped is False
    assert state.peak_equity == pytest.approx(10_000.0)
    # ...and it re-trips immediately if the drawdown is still there.
    state = update_breaker(state, 7_000.0, settings())
    assert state.tripped is True


def test_the_peak_keeps_ratcheting_while_the_sleeve_stands_down():
    state = update_breaker(BreakerState(), 10_000.0, settings())
    state = update_breaker(state, 7_000.0, settings())
    state = update_breaker(state, 13_000.0, settings())
    assert state.peak_equity == pytest.approx(13_000.0)


def test_an_unknown_equity_cannot_trip_the_breaker_and_says_so():
    """A balance we could not read is not a balance that is fine. The breaker must not
    flatten on a guess, and must not report 'not tripped' as if it had measured."""
    state = update_breaker(BreakerState(), 10_000.0, settings())
    state = update_breaker(state, None, settings())
    assert state.tripped is False
    assert state.blind is True
    assert "equity" in state.detail or "balance" in state.detail
    assert state.peak_equity == pytest.approx(10_000.0)   # not clobbered


def test_a_first_reading_of_none_leaves_the_breaker_blind_not_armed():
    state = update_breaker(BreakerState(), None, settings())
    assert state.peak_equity is None
    assert state.blind is True
    assert state.tripped is False


def test_a_non_positive_peak_is_unmeasurable_not_a_silent_no_trip():
    """`and self._peak_equity` made a peak of 0.0 falsy, so the breaker silently could
    not fire. A drawdown percentage off a zero peak is undefined, not 'fine'."""
    state = update_breaker(BreakerState(), 0.0, settings())
    assert state.peak_equity == pytest.approx(0.0)
    assert state.tripped is False
    assert state.blind is True
    assert "peak" in state.detail


def test_a_missing_circuit_breaker_pct_is_an_error_not_a_default():
    s = settings()
    del s["circuit_breaker_pct"]
    with pytest.raises(KeyError, match="circuit_breaker_pct"):
        update_breaker(BreakerState(), 10_000.0, s)


def test_the_breaker_signal_is_exactly_what_the_lifecycle_module_reads():
    """Task 7 FEEDS `LIFECYCLE_BREAKER`; it does not re-implement the flatten. The key
    comes from option_lifecycle itself so the two can never drift apart."""
    tripped = update_breaker(update_breaker(BreakerState(), 10_000.0, settings()),
                             7_000.0, settings())
    assert breaker_signal(tripped) == {SETTING_BREAKER_TRIPPED: True}
    healthy = update_breaker(BreakerState(), 10_000.0, settings())
    assert breaker_signal(healthy) == {SETTING_BREAKER_TRIPPED: False}


def test_the_breaker_signal_reaches_the_lifecycle_decision():
    """End to end across the two pure modules: a tripped sleeve flattens every
    structure with LIFECYCLE_BREAKER."""
    from datetime import date, datetime, timezone

    from ba2_common.core.option_lifecycle import LIFECYCLE_BREAKER, decide
    from ba2_common.core.option_types import OptionContract

    as_of = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
    legs = [LifecycleLeg("XYZ_P100", net_qty=-1.0, strike=100.0,
                         option_type=OptionRight.PUT, expiry=date(2026, 4, 17))]
    st = OptionStructure(1, "XYZ", "short_put", legs, quantity=1.0, multiplier=100,
                         entry_net_premium=-2.00, expiry=date(2026, 4, 17))
    chain = {"XYZ_P100": OptionContract(symbol="XYZ_P100", underlying="XYZ",
                                        option_type=OptionRight.PUT, strike=100.0,
                                        expiry=date(2026, 4, 17), bid=1.1, ask=1.2,
                                        delta=-0.2)}
    lifecycle_settings = {
        "profit_capture_pct": 50.0, "strangle_capture_pct": 25.0,
        "tested_delta_enabled": True, "tested_delta": 0.30, "roll_dte": 21,
        "dr_stop_enabled": True, "dr_stop_credit_mult": 2.0,
        "ur_stop_enabled": True, "ur_stop_credit_mult": 2.0,
    }
    state = update_breaker(update_breaker(BreakerState(), 10_000.0, settings()),
                           7_000.0, settings())
    lifecycle_settings.update(breaker_signal(state))
    assert [d.reason for d in decide([st], chain, lifecycle_settings, as_of)] == \
        [LIFECYCLE_BREAKER]


def test_a_standing_down_sleeve_no_longer_signals_a_flatten():
    """The book is already flat. Re-signalling every bar would re-issue closes forever."""
    state = update_breaker(update_breaker(BreakerState(), 10_000.0, settings()),
                           7_000.0, settings())
    state = update_breaker(state, 7_000.0, settings())
    assert breaker_signal(state) == {SETTING_BREAKER_TRIPPED: False}


# --------------------------------------------------------------------------
# settings discipline, shape and purity
# --------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["max_deployment_pct", "max_notional_leverage",
                                 "max_concurrent_structures"])
def test_a_missing_rail_setting_is_an_error_not_a_default(key):
    s = settings()
    del s[key]
    with pytest.raises(KeyError, match=key):
        check_rails(candidate(), book_totals([]), EQUITY, s, BreakerState())


def test_a_missing_undefined_risk_setting_is_an_error_only_when_the_rail_applies():
    s = settings()
    del s["undefined_risk_max_pct"]
    # A defined-risk candidate never reads it.
    assert check_rails(candidate(), book_totals([]), EQUITY, s, BreakerState()).allowed is True
    with pytest.raises(KeyError, match="undefined_risk_max_pct"):
        check_rails(candidate(strategy="short_put"), book_totals([]), EQUITY, s, BreakerState())


def test_the_rails_are_evaluated_in_the_declared_order():
    """The recorded reason is what a decline is logged and attributed as, so the order
    cannot wobble between releases."""
    from ba2_common.core.option_book import RAIL_ORDER

    v = check_rails(candidate(strategy="short_put", max_loss=100.0, notional=1_000.0),
                    book_totals([]), EQUITY, settings(), BreakerState())
    assert v.evaluated == RAIL_ORDER
    assert [r for r in v.evaluated] == sorted(v.evaluated, key=RAIL_ORDER.index)


def test_the_values_are_frozen():
    for value in (BookTotals(0.0, 0.0, 0.0, 0.0, 0, frozenset(), ()),
                  RailVerdict(True, RAIL_OK, "", candidate(), (),
                              BookTotals(0.0, 0.0, 0.0, 0.0, 0, frozenset(), ())),
                  BreakerState(),
                  candidate()):
        with pytest.raises(dataclasses.FrozenInstanceError):
            value.__setattr__("detail", "mutated")


def test_the_rails_are_per_expert_sleeves_and_the_docstring_says_so():
    """An account-wide cap across several option experts is a different feature and is
    out of scope. Someone WILL assume otherwise; the module has to say it."""
    import ba2_common.core.option_book as mod

    doc = mod.__doc__.lower()
    assert "sleeve" in doc
    assert "account-wide" in doc and "out of scope" in doc


def test_the_module_reaches_for_no_database_and_no_broker():
    """Pure means pure: importing it must not drag in the ORM, the trade store or any
    account interface."""
    from ._leakgate import check_leak

    verdict = check_leak(
        "ba2_common.core.option_book",
        ["sqlmodel", "sqlalchemy", "ba2_common.core.db", "ba2_common.core.models",
         "ba2_common.core.trade_store", "ba2_common.core.interfaces",
         "ba2_common.core.TradeConditions", "ba2_providers", "ba2_experts",
         "ba2_trade_platform"],
    )
    assert verdict == "CLEAN", f"option_book is not pure: {verdict}"


# --------------------------------------------------------------------------
# the stand-down gates ENTRY, and what clears it
# --------------------------------------------------------------------------
def halted_state():
    """A sleeve the breaker has just flattened: -30% against a 20% breaker."""
    state = update_breaker(BreakerState(), 10_000.0, settings())
    state = update_breaker(state, 7_000.0, settings())
    assert (state.tripped, state.halted) == (True, True)
    return state


def test_a_standing_down_sleeve_may_not_open_a_new_structure():
    """THE defect: the latch suppressed exits and nothing else. A sleeve the breaker
    had just flattened could re-open the whole book on the very next entry bar, at the
    bottom of the drawdown that flattened it, and then flatten again — paying the
    spread every round. The docstring said 'stays flat until a new entry cycle'; no
    code said it."""
    from ba2_common.core.option_book import RAIL_BREAKER_HALTED

    v = check_rails(candidate(), book_totals([]), EQUITY, settings(), halted_state())
    assert v.allowed is False
    assert v.reason == RAIL_BREAKER_HALTED
    assert "stand" in v.detail or "halt" in v.detail


def test_the_halt_gate_runs_before_every_rail():
    """A standing-down sleeve is not 'within its rails' — no rail was consulted at all,
    and `evaluated` has to say so rather than implying the caps let it through."""
    v = check_rails(candidate(), book_totals([]), EQUITY, settings(), halted_state())
    assert v.evaluated == ()
    assert v.book_after.structure_count == 0     # nothing was charged to the sleeve


def test_admit_declines_every_candidate_while_the_sleeve_stands_down():
    from ba2_common.core.option_book import RAIL_BREAKER_HALTED

    verdicts = admit([candidate("A"), candidate("B"), candidate("C")], book_totals([]),
                     EQUITY, settings(), halted_state())
    assert [v.allowed for v in verdicts] == [False, False, False]
    assert {v.reason for v in verdicts} == {RAIL_BREAKER_HALTED}


def test_a_sleeve_that_is_not_standing_down_is_not_blocked_by_the_gate():
    """The gate must cost an armed sleeve nothing — a breaker that blocks entry when it
    has NOT tripped is a whole strategy switched off by a risk control that never fired."""
    healthy = update_breaker(BreakerState(), 10_000.0, settings())
    assert healthy.halted is False
    assert check_rails(candidate(), book_totals([]), EQUITY, settings(),
                       healthy).allowed is True
    assert check_rails(candidate(), book_totals([]), EQUITY, settings(),
                       BreakerState()).allowed is True


def test_check_rails_will_not_assume_a_missing_breaker_is_an_un_halted_one():
    """No default. A caller who has not said whether the sleeve is standing down has
    not said 'it is trading' — and that is precisely the substitution that let the
    stand-down be ignored for as long as it was."""
    with pytest.raises(TypeError):
        check_rails(candidate(), book_totals([]), EQUITY, settings())
    with pytest.raises(TypeError, match="BreakerState"):
        check_rails(candidate(), book_totals([]), EQUITY, settings(), None)
    with pytest.raises(TypeError):
        admit([candidate()], book_totals([]), EQUITY, settings())


def test_a_recovery_out_of_the_drawdown_clears_the_stand_down():
    """What clears a stand-down is the drawdown healing — nothing else.

    It cannot be 'the next entry cycle': entry is now blocked while standing down, so
    an entry-triggered clear is unreachable and the sleeve would be halted forever.
    It cannot be a bar count either: the peak is deliberately KEPT across a re-arm, so
    a sleeve that resumes while still under water trips again on its first managed bar
    and the open/flatten cycle simply repeats more slowly. Recovery is the only
    condition under which resuming does not immediately re-trip."""
    state = halted_state()
    state = update_breaker(state, 9_000.0, settings())    # -10%, out of the stand-down
    assert state.halted is False
    assert state.tripped is False
    assert check_rails(candidate(), book_totals([]), EQUITY, settings(),
                       state).allowed is True


def test_a_partial_recovery_still_deep_in_the_drawdown_does_not_clear_it():
    state = halted_state()
    state = update_breaker(state, 8_500.0, settings())    # -15%: better, not better enough
    assert state.halted is True
    assert check_rails(candidate(), book_totals([]), EQUITY, settings(),
                       state).allowed is False


def test_the_rearm_line_is_inclusive_at_half_the_trip_depth():
    """20% trip -> 10% re-arm. Exactly on the line clears, a hair under does not."""
    from ba2_common.core.option_book import BREAKER_REARM_DEPTH_FRACTION

    assert BREAKER_REARM_DEPTH_FRACTION == 0.5
    assert update_breaker(halted_state(), 9_000.0, settings()).halted is False
    assert update_breaker(halted_state(), 8_999.99, settings()).halted is True


def test_the_rearm_line_is_strictly_shallower_than_the_trip_line():
    """The hysteresis IS the fix. Re-arming at the trip line lets a sleeve hovering on
    the boundary flap open/flatten on rounding — the same cycle, one cent wide."""
    state = update_breaker(halted_state(), 8_000.01, settings())   # just above the trip line
    assert state.halted is True, "re-arm must need a real recovery, not one basis point"


def test_a_cleared_stand_down_does_not_immediately_re_trip():
    """The proof that recovery is the RIGHT clear condition: on the bar it resumes, the
    sleeve is by construction out of breaker territory, so it can actually hold a
    position instead of being flattened again on the next evaluation."""
    state = update_breaker(halted_state(), 9_500.0, settings())
    assert state.halted is False
    state = update_breaker(state, 9_500.0, settings())
    assert state.tripped is False and state.halted is False


def test_clearing_a_stand_down_keeps_the_peak():
    """Re-arming at the trough would erase the drawdown that caused the stand-down and
    hand the sleeve a fresh 20% to lose."""
    state = update_breaker(halted_state(), 9_000.0, settings())
    assert state.peak_equity == pytest.approx(10_000.0)


def test_an_unknown_equity_does_not_clear_a_stand_down():
    """Blind is not recovered."""
    state = update_breaker(halted_state(), None, settings())
    assert state.halted is True
    assert state.blind is True


def test_a_stand_down_clears_without_the_sleeve_ever_opening_anything():
    """NO DEADLOCK. Entry is blocked while standing down, so if the clear depended on a
    successful entry the sleeve could never re-arm: blocked because halted, halted
    because never entered. This walks the whole loop with entry refused on every bar
    and shows the halt still lifts on its own."""
    from ba2_common.core.option_book import RAIL_BREAKER_HALTED

    state = halted_state()
    for equity in (7_000.0, 7_200.0, 8_000.0, 8_900.0):
        state = update_breaker(state, equity, settings())
        v = check_rails(candidate(), book_totals([]), EQUITY, settings(), state)
        assert v.allowed is False and v.reason == RAIL_BREAKER_HALTED
    state = update_breaker(state, 9_100.0, settings())
    assert state.halted is False
    assert check_rails(candidate(), book_totals([]), EQUITY, settings(),
                       state).allowed is True


def test_the_explicit_rearm_still_overrides_without_any_recovery():
    """The operator escape hatch. `rearm` is unconditional on purpose: it is the one
    way out that does not depend on the market, and it is what keeps 'halted forever'
    off the table even for a sleeve whose equity never moves again."""
    state = rearm(halted_state())
    assert state.halted is False
    assert state.peak_equity == pytest.approx(10_000.0)
    assert check_rails(candidate(), book_totals([]), EQUITY, settings(),
                       state).allowed is True


def test_a_non_positive_peak_cannot_measure_a_recovery_so_the_stand_down_stands():
    """The re-arm line is a percentage OF the peak, so an unusable peak makes the
    recovery test undefined — and undefined must not read as recovered. Same three-way
    split as the trip side: unknown / unusable-peak / measured."""
    after = update_breaker(BreakerState(peak_equity=0.0, halted=True), 0.0, settings())
    assert after.halted is True
    assert after.blind is True
    assert "peak" in after.detail
    # a NEGATIVE peak is not a baseline a recovery can be measured against either --
    # and note that -4,000 IS an improvement on -5,000, so a rule that only looked at
    # the direction of travel would clear here.
    negative = update_breaker(BreakerState(peak_equity=-5_000.0, halted=True),
                              -4_000.0, settings())
    assert negative.halted is True
    assert negative.blind is True


def test_the_stand_down_does_not_leak_into_the_flatten_signal():
    """`tripped` stays the edge across a clear: the recovery bar must not re-signal a
    flatten at the very moment the sleeve is allowed to trade again."""
    state = update_breaker(halted_state(), 9_500.0, settings())
    assert breaker_signal(state) == {SETTING_BREAKER_TRIPPED: False}
