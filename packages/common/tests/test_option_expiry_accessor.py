"""``option_expiry`` — the ONE shared answer to "which expiry does this structure mean?".

Per-leg expiries were never missing from this platform: ``TradingOrder.expiry`` has been a
real column since alembic ``08de6c7b6eed`` and ``submit_option_order`` writes one child row
per leg carrying its own date. What was missing is a RULE for reading them when the legs
disagree — the reason ``Transaction.expiry``'s single value was defensible only while every
supported structure sat on one expiry.

This module supplies that rule, and these tests pin it. The governing properties:

* **A single-expiry structure is unaffected.** Both named rules return the same one date and
  say no rule was applied. That is what keeps all 16 existing structures byte-identical.
* **Undeclared disagreement is still a contradiction**, exactly as before — never ``min()``,
  never ``max()``, never a guess.
* **Declared disagreement gets a NAMED answer**: the SHORT leg for roll-window questions, the
  LONG leg for roll-floor/structure-exit questions. Ambiguity was the guard's whole reason to
  exist, so the side is spelled out in the call, not inferred.
* **Fail-closed on the side that is missing**: a declared structure asked for a side it does
  not have is unresolved, never quietly answered from the other side.
"""
from __future__ import annotations

from datetime import date

import pytest

from ba2_common.core.option_expiry import (
    EXPIRY_RULE_ROLL_WINDOW,
    EXPIRY_RULE_STRUCTURE_EXIT,
    MULTI_EXPIRY_OPTION_STRATEGIES,
    ExpiryLeg,
    is_multi_expiry_strategy,
    resolve_structure_expiry,
)

# Frozen real monthlies. Never "today": a date equal to the wall clock has let a mutation
# survive in this repo before.
NEAR = date(2026, 9, 18)
FAR = date(2027, 1, 15)

BOTH_RULES = [EXPIRY_RULE_ROLL_WINDOW, EXPIRY_RULE_STRUCTURE_EXIT]


def _short(expiry, qty=1.0):
    """A held SHORT leg: signed net quantity is negative."""
    return ExpiryLeg(expiry=expiry, net_qty=-abs(qty))


def _long(expiry, qty=1.0):
    """A held LONG leg: signed net quantity is positive."""
    return ExpiryLeg(expiry=expiry, net_qty=abs(qty))


# ---------------------------------------------------------------------------
# the declaration
# ---------------------------------------------------------------------------
def test_pmcc_is_the_declared_multi_expiry_strategy():
    assert "pmcc" in MULTI_EXPIRY_OPTION_STRATEGIES
    assert is_multi_expiry_strategy("pmcc") is True


def test_calendar_spread_is_not_declared_while_o_cal_is_phase_gated():
    """O_CAL is phase-2. Declaring it here would relax the guard for a structure with no
    builder and no lifecycle — and would flip the existing guard suite red."""
    assert "calendar_spread" not in MULTI_EXPIRY_OPTION_STRATEGIES
    assert is_multi_expiry_strategy("calendar_spread") is False


@pytest.mark.parametrize("strategy", [None, "", "   ", "bull_call_spread", "iron_condor",
                                      "covered_call", "diagonal_spread", "spread", "single"])
def test_everything_undeclared_is_not_multi_expiry(strategy):
    """Fail-closed: membership is opt-in. Absent, blank and unknown are all "no"."""
    assert is_multi_expiry_strategy(strategy) is False


def test_the_declaration_is_immutable():
    """A frozenset, so no caller can widen the relaxation at runtime."""
    assert isinstance(MULTI_EXPIRY_OPTION_STRATEGIES, frozenset)


# ---------------------------------------------------------------------------
# single-expiry structures: no behaviour change, under EITHER rule
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rule", BOTH_RULES)
def test_a_single_expiry_structure_answers_the_same_under_either_rule(rule):
    """The whole byte-identical claim rests on this: asking a per-leg question of a
    single-expiry structure returns the single expiry."""
    res = resolve_structure_expiry([_long(NEAR), _short(NEAR)],
                                   strategy="bull_call_spread", rule=rule)

    assert res.expiry == NEAR
    assert res.conflict == ()
    assert res.missing is False
    assert res.rule_applied is None, "no leg rule was needed, so none may be claimed"


@pytest.mark.parametrize("rule", BOTH_RULES)
def test_the_declared_structure_expiry_alone_answers(rule):
    """Historical rows carry the date on the transaction/parent only."""
    res = resolve_structure_expiry([], strategy="long_call", rule=rule, declared_expiry=NEAR)
    assert res.expiry == NEAR and res.rule_applied is None


@pytest.mark.parametrize("rule", BOTH_RULES)
def test_a_leg_with_no_expiry_does_not_veto_the_legs_that_have_one(rule):
    """UNKNOWN is not a second expiry — the flatten path rebuilds legs with expiry=None."""
    res = resolve_structure_expiry([_long(NEAR), _short(None)],
                                   strategy="bull_call_spread", rule=rule)
    assert res.expiry == NEAR and res.conflict == ()


@pytest.mark.parametrize("rule", BOTH_RULES)
def test_a_closed_leg_contributes_nothing(rule):
    """net_qty 0 is a leg bought back to close; its stale date must not manufacture a
    permanent contradiction."""
    res = resolve_structure_expiry([_long(NEAR), ExpiryLeg(expiry=FAR, net_qty=0.0)],
                                   strategy="bull_call_spread", rule=rule)
    assert res.expiry == NEAR and res.conflict == ()


@pytest.mark.parametrize("rule", BOTH_RULES)
def test_no_candidate_anywhere_is_missing_not_zero_and_not_infinity(rule):
    res = resolve_structure_expiry([_long(None)], strategy="long_call", rule=rule)

    assert res.missing is True
    assert res.expiry is None, "unknown is never a value"
    assert res.conflict == ()


# ---------------------------------------------------------------------------
# undeclared disagreement: the pre-existing contradiction, unchanged
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rule", BOTH_RULES)
def test_an_undeclared_structure_with_two_expiries_is_a_conflict(rule):
    """Today's behaviour, preserved: never min(), never max(), never a guess."""
    res = resolve_structure_expiry([_long(FAR), _short(NEAR)],
                                   strategy="bull_call_spread", rule=rule)

    assert res.expiry is None
    assert res.missing is False
    assert res.conflict == (NEAR, FAR), "the candidates are reported sorted, for the message"
    assert res.rule_applied is None


@pytest.mark.parametrize("rule", BOTH_RULES)
def test_an_UNNAMED_strategy_with_two_expiries_is_a_conflict(rule):
    """Fail-closed on the absent declaration, which is what a stale/legacy row carries."""
    res = resolve_structure_expiry([_long(FAR), _short(NEAR)], strategy=None, rule=rule)
    assert res.expiry is None and res.conflict == (NEAR, FAR)


@pytest.mark.parametrize("rule", BOTH_RULES)
def test_a_declared_structure_disagreeing_with_its_own_declared_expiry_is_a_conflict(rule):
    """Legs agree on NEAR, the row says FAR. Two dates, and the legs cannot adjudicate a
    structure-level value — unresolved, not silently leg-first."""
    res = resolve_structure_expiry([_long(NEAR), _short(NEAR)], strategy="bull_call_spread",
                                   rule=rule, declared_expiry=FAR)
    assert res.expiry is None and res.conflict == (NEAR, FAR)


# ---------------------------------------------------------------------------
# THE NAMED RULES — a declared multi-expiry structure, read from both sides
# ---------------------------------------------------------------------------
def test_the_roll_window_rule_reads_the_SHORT_leg():
    """A PMCC rolls when the OVERLAY expires. Design 2026-08-31 §4: "roll loop at short
    expiry". Reading the long here would make the roll unreachable for a year."""
    res = resolve_structure_expiry([_long(FAR), _short(NEAR)], strategy="pmcc",
                                   rule=EXPIRY_RULE_ROLL_WINDOW)

    assert res.expiry == NEAR, "the roll window is the SHORT leg's expiry"
    assert res.rule_applied == EXPIRY_RULE_ROLL_WINDOW
    assert res.conflict == () and res.missing is False


def test_the_structure_exit_rule_reads_the_LONG_leg():
    """The roll FLOOR asks "is there still life to roll into?" — that is the LEAPS.
    Design §4: "Structure exit: long-leg DTE floor". Reading the short here would exit the
    whole structure every time the overlay approached its own expiry."""
    res = resolve_structure_expiry([_long(FAR), _short(NEAR)], strategy="pmcc",
                                   rule=EXPIRY_RULE_STRUCTURE_EXIT)

    assert res.expiry == FAR, "the structure exit is the LONG leg's expiry"
    assert res.rule_applied == EXPIRY_RULE_STRUCTURE_EXIT


def test_the_two_rules_disagree_on_a_real_diagonal():
    """The point of naming them. If these ever coincide the test above proved nothing."""
    legs = [_long(FAR), _short(NEAR)]
    roll = resolve_structure_expiry(legs, strategy="pmcc", rule=EXPIRY_RULE_ROLL_WINDOW)
    exit_ = resolve_structure_expiry(legs, strategy="pmcc", rule=EXPIRY_RULE_STRUCTURE_EXIT)

    assert roll.expiry != exit_.expiry
    assert (roll.expiry, exit_.expiry) == (NEAR, FAR)


def test_the_nearest_leg_on_the_requested_side_binds():
    """Two shorts (a roll in flight): the SOONEST is the one that binds the roll window."""
    soon, later = date(2026, 9, 18), date(2026, 10, 16)
    res = resolve_structure_expiry([_long(FAR), _short(later), _short(soon)],
                                   strategy="pmcc", rule=EXPIRY_RULE_ROLL_WINDOW)
    assert res.expiry == soon


def test_a_closed_short_does_not_answer_the_roll_window():
    """Netted to zero = bought back. The live short is the one that binds."""
    res = resolve_structure_expiry(
        [_long(FAR), ExpiryLeg(expiry=date(2026, 8, 21), net_qty=0.0), _short(NEAR)],
        strategy="pmcc", rule=EXPIRY_RULE_ROLL_WINDOW)
    assert res.expiry == NEAR


# --- fail-closed on the missing side ---------------------------------------
def test_a_declared_structure_with_no_SHORT_leg_does_not_answer_the_roll_window():
    """Never fall through to the long. An unresolved roll window is a loud unknown; a roll
    window silently answered from the LEAPS would schedule a roll a year out."""
    res = resolve_structure_expiry([_long(NEAR), _long(FAR)], strategy="pmcc",
                                   rule=EXPIRY_RULE_ROLL_WINDOW)

    assert res.expiry is None
    assert res.conflict == (NEAR, FAR)
    assert res.rule_applied is None


def test_a_declared_structure_with_no_LONG_leg_does_not_answer_the_structure_exit():
    """The inverse, and the more dangerous one: answering from the short would report a
    healthy floor for a structure holding nothing but a naked overlay."""
    res = resolve_structure_expiry([_short(NEAR), _short(FAR)], strategy="pmcc",
                                   rule=EXPIRY_RULE_STRUCTURE_EXIT)

    assert res.expiry is None
    assert res.conflict == (NEAR, FAR)


def test_a_declared_structure_ignores_its_stale_declared_expiry_when_the_legs_span_two():
    """A real two-expiry structure records NULL on the transaction. Should a legacy value
    survive, the LEGS are the record — the named rule reads them, and the stale scalar
    cannot become the answer."""
    stale = date(2026, 7, 17)
    res = resolve_structure_expiry([_long(FAR), _short(NEAR)], strategy="pmcc",
                                   rule=EXPIRY_RULE_ROLL_WINDOW, declared_expiry=stale)
    assert res.expiry == NEAR


# ---------------------------------------------------------------------------
# the rule argument itself
# ---------------------------------------------------------------------------
def test_an_unknown_rule_is_a_defect_not_a_default():
    """A typo'd rule name must not silently become "whichever branch fell through"."""
    with pytest.raises(ValueError, match="expiry rule"):
        resolve_structure_expiry([_long(FAR), _short(NEAR)], strategy="pmcc",
                                 rule="whenever_feels_right")


def test_the_two_rule_names_are_distinct_constants():
    assert EXPIRY_RULE_ROLL_WINDOW != EXPIRY_RULE_STRUCTURE_EXIT
