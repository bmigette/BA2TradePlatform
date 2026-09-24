"""``rec_direction``: the expert's direction call as a SIGNED number, and its equivalence to
the ``bullish`` / ``bearish`` flags it was introduced to replace on option entry rules.

WHY THE FIELD EXISTS. ``BullishCondition``/``BearishCondition`` are FLAG conditions: the field
is authored into the rule leaf, so an optimizer can only switch the gate OFF, never flip it. A
pure-option strategy's entry direction was therefore fixed at build time and the CONTRARIAN arm
of every structure -- a long call entered on a SELL signal -- was unreachable by construction.
As a number the same question becomes an ordering against a fixed threshold of 0, and the
optimizer's three-way mode gene (``off``/``below``/``above``) spans exactly "no direction
filter", "bearish only" and "bullish only" at the SAME gene cost as the old on/off toggle.

WHAT THIS FILE PINS, AND WHY EACH IS A REAL FAILURE MODE
--------------------------------------------------------
1. **Equivalence at threshold 0.** ``> 0`` must admit exactly the recommendations ``bullish``
   admitted and ``< 0`` exactly those ``bearish`` admitted, on the BUY/HOLD/SELL experts the
   option grid runs (DeterministicScorer, FMPRating). Anything else silently re-points every
   already-optimised option genome at a different population.
2. **The 5-grade widening is DELIBERATE and stated.** OVERWEIGHT/UNDERWEIGHT are directional
   grades, so they sit on the bullish/bearish side of 0. That is wider than the strict
   ``== BUY`` flag, and the alternative -- folding them into 0 -- would claim an expert with a
   weak view has NO view. Pinned so the choice cannot be reversed by accident.
3. **ERROR is UNEVALUABLE, never 0.** 0 already means HOLD here, so a failed analysis folded
   into it would be indistinguishable from a considered "no directional view", and both a
   ``> 0`` and a ``< 0`` gate would decline it while looking like they had an opinion. The
   ``RecommendationDaysToEarningsCondition`` discipline: fires in NEITHER direction.
4. **The field is MAPPED.** ``triggers_from_condition_tree`` DROPS an unmapped field with
   nothing but a WARNING, which would leave every option entry rule running with no direction
   gate at all while the optimizer kept tuning its mode gene.
"""
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeConditions import (
    BearishCondition,
    BullishCondition,
    CurrentRatingNeutralCondition,
    RecommendationDirectionCondition,
    create_condition,
)
from ba2_common.core.types import ExpertEventType, OrderRecommendation

#: The three grades every expert the option grid runs can emit.
THREE_GRADE = [OrderRecommendation.BUY, OrderRecommendation.HOLD, OrderRecommendation.SELL]
#: The two extra grades of the 5-grade scale (FactorRanker, FinnHubRating).
FIVE_GRADE_EXTRA = [OrderRecommendation.OVERWEIGHT, OrderRecommendation.UNDERWEIGHT]


def _rec(action):
    return SimpleNamespace(recommended_action=action, instance_id=1, symbol="MSFT", data=None)


def _cond(action, op, value=0.0):
    """Built through the FACTORY, so a missing CONDITION_MAP entry fails here too."""
    return create_condition(ExpertEventType.N_REC_DIRECTION, None, "MSFT", _rec(action),
                            operator_str=op, value=value)


# --- 1. the codes themselves ----------------------------------------------------------
@pytest.mark.parametrize("action,code", [
    (OrderRecommendation.SELL, -2),
    (OrderRecommendation.UNDERWEIGHT, -1),
    (OrderRecommendation.HOLD, 0),
    (OrderRecommendation.OVERWEIGHT, 1),
    (OrderRecommendation.BUY, 2),
])
def test_the_scale_is_the_5_grade_ordering_centred_on_hold(action, code):
    """Centred on HOLD is what makes a FIXED threshold of 0 the direction split; an
    off-centre scale would need the threshold to be searched, which would cost the gene the
    mode gene was supposed to replace."""
    cond = _cond(action, "==", float(code))
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == code


def test_the_scale_is_derived_from_the_rating_change_ordering_not_retyped():
    """One ordering for the whole platform. Two hand-typed copies is how the rating-change
    events and the direction gate would come to disagree about which way OVERWEIGHT points."""
    from ba2_common.core.TradeConditions import _RATING_RANK, _REC_DIRECTION_CODE

    hold = _RATING_RANK[OrderRecommendation.HOLD]
    assert _REC_DIRECTION_CODE == {g: r - hold for g, r in _RATING_RANK.items()}
    assert OrderRecommendation.ERROR not in _REC_DIRECTION_CODE


# --- 2. equivalence with the flags it replaces ----------------------------------------
@pytest.mark.parametrize("action", THREE_GRADE)
def test_above_zero_is_exactly_the_bullish_flag_on_a_3_grade_expert(action):
    """THE PARITY STATEMENT for the option grid's own experts, derived from the flag
    condition rather than from a hand-written expectation: reading the expectation off the
    same table the implementation uses would make any swap self-consistent."""
    flag = BullishCondition(None, "MSFT", _rec(action)).evaluate()
    assert _cond(action, ">").evaluate() is flag, action


@pytest.mark.parametrize("action", THREE_GRADE)
def test_below_zero_is_exactly_the_bearish_flag_on_a_3_grade_expert(action):
    flag = BearishCondition(None, "MSFT", _rec(action)).evaluate()
    assert _cond(action, "<").evaluate() is flag, action


def test_above_admits_only_buy_and_below_only_sell_spelled_out():
    """The same claim without the derivation, so a broken flag condition cannot make the two
    tests above agree with each other while both are wrong."""
    assert [_cond(a, ">").evaluate() for a in THREE_GRADE] == [True, False, False]
    assert [_cond(a, "<").evaluate() for a in THREE_GRADE] == [False, False, True]


@pytest.mark.parametrize("action", THREE_GRADE)
def test_zero_is_exactly_the_neutral_flag(action):
    """The third reading, for completeness: the neutral members keep their flag leaf, but a
    scale on which HOLD were not 0 would break both of the above at once."""
    flag = CurrentRatingNeutralCondition(None, "MSFT", _rec(action)).evaluate()
    assert _cond(action, "==").evaluate() is flag, action


# --- 3. the deliberate 5-grade widening -----------------------------------------------
def test_overweight_is_bullish_and_underweight_bearish_not_neutral():
    """DELIBERATELY WIDER than the ``== BUY`` flag on a 5-grade expert.

    The question this field answers is which way the expert is LEANING. An OVERWEIGHT that
    read as "not bullish" would have to read as HOLD, i.e. as no view at all -- the exact
    silent mis-reading the house rule against unmapped-enum-becomes-0 exists to prevent. The
    strict ``== BUY`` reading is still available: that is what the ``bullish`` flag is.
    """
    assert _cond(OrderRecommendation.OVERWEIGHT, ">").evaluate() is True
    assert _cond(OrderRecommendation.OVERWEIGHT, "<").evaluate() is False
    assert _cond(OrderRecommendation.UNDERWEIGHT, "<").evaluate() is True
    assert _cond(OrderRecommendation.UNDERWEIGHT, ">").evaluate() is False
    # ... and neither is neutral
    for action in FIVE_GRADE_EXTRA:
        assert _cond(action, "==").evaluate() is False, action


# --- 4. ERROR is unevaluable ----------------------------------------------------------
@pytest.mark.parametrize("op", [">", "<", ">=", "<=", "==", "!="])
def test_an_error_grade_fires_in_neither_direction(op, caplog):
    """Not 0, which is HOLD. Every operator, including ``!=`` and ``<=`` -- an "unevaluable
    means False" rule that only covered the strict operators would still let the loose ones
    pass a failed analysis through."""
    cond = _cond(OrderRecommendation.ERROR, op)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None
    assert cond.get_actual_value_display() is None


def test_an_error_grade_is_LOUD():
    """Every recommendation carries a graded action, so getting here means the expert failed.
    Silence is what let three earlier classes of dropped gate survive."""
    import logging

    cond = _cond(OrderRecommendation.ERROR, ">")
    logger = logging.getLogger("ba2_common")
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        cond.evaluate()
    finally:
        logger.removeHandler(handler)
    assert any(r.levelno >= logging.WARNING and "rec_direction" in r.getMessage()
               for r in records), [r.getMessage() for r in records]


def test_a_missing_recommendation_object_does_not_read_as_hold():
    """``None`` where a recommendation belongs is the other route to a plausible-looking 0."""
    cond = create_condition(ExpertEventType.N_REC_DIRECTION, None, "MSFT", None,
                            operator_str=">", value=0.0)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


# --- 5. the field reaches the engine ---------------------------------------------------
def test_the_field_is_mapped_so_a_leaf_naming_it_is_not_dropped():
    from ba2_common.core.rule_builders import FIELD_EVENT, triggers_from_condition_tree

    assert FIELD_EVENT["rec_direction"] is ExpertEventType.N_REC_DIRECTION
    triggers = triggers_from_condition_tree(
        {"type": "AND", "conditions": [
            {"id": "x-signal", "field": "rec_direction", "field_type": "numeric",
             "op": "<", "value": 0.0}]})
    assert list(triggers.values()) == [
        {"event_type": "rec_direction", "operator": "<", "value": 0.0}]


def test_the_condition_is_registered_for_the_event_type():
    from ba2_common.core.TradeConditions import CONDITION_MAP

    assert CONDITION_MAP[ExpertEventType.N_REC_DIRECTION] is RecommendationDirectionCondition
