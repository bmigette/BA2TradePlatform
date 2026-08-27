"""The value objects that carry a proposal from a rule action to the option risk manager.

WHY TEST DATA CLASSES AT ALL. Two things here are load-bearing rather than incidental: the
refusal PHRASES are a stable API (logs, the UI and future tests grep for them, so a typo or a
rename is a silent break), and the request must be FROZEN so that resolving one cannot mutate the
proposal another candidate is still being compared against.
"""
import dataclasses

import pytest

from ba2_common.core.option_request import (
    BUDGET_EXHAUSTED_REFUSAL, BUYING_POWER_REFUSAL, CONFIDENCE_UNMEASURABLE_REFUSAL,
    EMPTY_BOX_REFUSAL, MAX_LOSS_UNMEASURABLE_REFUSAL, NEGATIVE_EXPECTANCY_REFUSAL,
    REFUSAL_PHRASES, TARGET_UNMEASURABLE_REFUSAL, UNDEFINED_RISK_REFUSAL,
    OptionStructureRequest, StructureRefusal)


def a_request(**kw):
    base = dict(structure="buy_call", symbol="AAPL", expert_recommendation_id=1)
    base.update(kw)
    return OptionStructureRequest(**base)


def test_request_is_frozen():
    req = a_request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.symbol = "MSFT"


def test_request_defaults_leave_every_optional_boundary_unset():
    req = a_request()
    assert req.term is None and req.dte_min is None and req.dte_max is None
    assert req.box_min is None and req.box_max is None
    assert req.min_arc is None and req.sizing_pct is None


def test_all_refusal_phrases_are_registered_and_distinct():
    phrases = [UNDEFINED_RISK_REFUSAL, MAX_LOSS_UNMEASURABLE_REFUSAL,
               CONFIDENCE_UNMEASURABLE_REFUSAL, TARGET_UNMEASURABLE_REFUSAL,
               NEGATIVE_EXPECTANCY_REFUSAL, BUYING_POWER_REFUSAL,
               BUDGET_EXHAUSTED_REFUSAL, EMPTY_BOX_REFUSAL]
    assert len(set(phrases)) == len(phrases)
    assert set(phrases) == set(REFUSAL_PHRASES)


def test_refusal_carries_the_request_the_phrase_and_a_detail():
    req = a_request()
    r = StructureRefusal(request=req, phrase=EMPTY_BOX_REFUSAL,
                         detail="min_volume=100 rejected all 42 candidates")
    assert r.request is req
    assert r.phrase == EMPTY_BOX_REFUSAL
    assert "min_volume" in r.detail


def test_refusal_rejects_an_unregistered_phrase():
    # A free-text phrase defeats the point: callers grep for these.
    with pytest.raises(ValueError):
        StructureRefusal(request=a_request(), phrase="it didn't work", detail="")
