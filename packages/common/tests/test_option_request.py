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
    EMPTY_BOX_REFUSAL, EMPTY_CHAIN_REFUSAL, MAX_LOSS_UNMEASURABLE_REFUSAL,
    MISSING_QUOTE_REFUSAL, NEGATIVE_EXPECTANCY_REFUSAL, NON_POSITIVE_NET_REFUSAL,
    NO_LIQUID_CONTRACT_REFUSAL, REFUSAL_PHRASES, SELECTION_CONFIG_REFUSAL,
    TARGET_UNMEASURABLE_REFUSAL, UNDEFINED_RISK_REFUSAL,
    OptionStructureRequest, ResolvedStructure, ScoredStructure, StructureRefusal)


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
    # The set equality is the point: it catches a constant that exists but was never added to
    # REFUSAL_PHRASES (invisible to StructureRefusal) as well as the reverse. Phase 2a added the
    # last five, derived from the refusals the 17 builders actually emit.
    phrases = [UNDEFINED_RISK_REFUSAL, MAX_LOSS_UNMEASURABLE_REFUSAL,
               CONFIDENCE_UNMEASURABLE_REFUSAL, TARGET_UNMEASURABLE_REFUSAL,
               NEGATIVE_EXPECTANCY_REFUSAL, BUYING_POWER_REFUSAL,
               BUDGET_EXHAUSTED_REFUSAL, EMPTY_BOX_REFUSAL,
               EMPTY_CHAIN_REFUSAL, NO_LIQUID_CONTRACT_REFUSAL, MISSING_QUOTE_REFUSAL,
               NON_POSITIVE_NET_REFUSAL, SELECTION_CONFIG_REFUSAL]
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


# --- added for Phase 2a -----------------------------------------------------------------
# (the five new phrases, ResolvedStructure and ScoredStructure are imported at the top of the
# file, because the pre-existing exhaustiveness test above also has to name the new phrases.)


def test_the_phrases_the_builders_actually_emit_are_registered():
    """Phase 1 registered eight phrases invented from the design doc. A survey of the 17
    builders found five refusal KINDS they really emit that had no phrase at all -- so a
    StructureRefusal could not have been constructed for any of them without raising."""
    for phrase in (EMPTY_CHAIN_REFUSAL, MISSING_QUOTE_REFUSAL, NON_POSITIVE_NET_REFUSAL,
                   NO_LIQUID_CONTRACT_REFUSAL, SELECTION_CONFIG_REFUSAL):
        assert phrase in REFUSAL_PHRASES
    assert len(set(REFUSAL_PHRASES)) == len(REFUSAL_PHRASES)


def test_resolved_structure_carries_no_score_and_no_max_loss():
    """`_resolve()` runs inside ONE action and cannot know the bar's other candidates, so it
    cannot produce a score; and payoff-at-target needs the recommendation's target, which is a
    risk-manager input. Keeping those fields on ResolvedStructure would force every builder to
    invent them."""
    fields = set(ResolvedStructure.__dataclass_fields__)
    assert "score" not in fields
    assert "payoff_at_target" not in fields
    assert "max_loss_per_contract" not in fields
    assert {"legs", "payoff_legs", "limit_price", "option_strategy", "dte",
            "reserve_kwargs", "reserve_per_contract", "cost_per_contract",
            "sizing_basis"} <= fields


def test_scored_structure_wraps_a_resolved_one_and_adds_the_risk_manager_numbers():
    fields = set(ScoredStructure.__dataclass_fields__)
    assert {"resolved", "max_loss_per_contract", "payoff_at_target", "score"} <= fields


def test_cost_per_contract_is_what_the_sizing_budget_is_divided_by():
    """The whole point of the field: `_size` divides by `premium * 100` and `_size_by_reserve`
    divides by the reserve. Expressing both as "dollars one contract consumes" collapses two
    sizers into one and is what lets the shared tail be shared at all."""
    req = a_request()
    r = ResolvedStructure(request=req, legs=[], payoff_legs=[], limit_price=1.25,
                          option_strategy="long_call", dte=30, reserve_kwargs={},
                          reserve_per_contract=0.0, cost_per_contract=125.0,
                          sizing_basis="premium")
    assert r.cost_per_contract == 125.0
    assert r.sizing_basis == "premium"


@pytest.mark.parametrize("basis", ["premium", "reserve", "held_shares"])
def test_sizing_basis_accepts_the_three_real_families(basis):
    req = a_request()
    r = ResolvedStructure(request=req, legs=[], payoff_legs=[], limit_price=1.0,
                          option_strategy="long_call", dte=30, reserve_kwargs={},
                          reserve_per_contract=0.0, cost_per_contract=100.0,
                          sizing_basis=basis)
    assert r.sizing_basis == basis


def test_an_unknown_sizing_basis_is_refused():
    with pytest.raises(ValueError):
        ResolvedStructure(request=a_request(), legs=[], payoff_legs=[], limit_price=1.0,
                          option_strategy="long_call", dte=30, reserve_kwargs={},
                          reserve_per_contract=0.0, cost_per_contract=100.0,
                          sizing_basis="vibes")
