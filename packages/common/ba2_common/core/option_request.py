"""Value objects carrying an option proposal from a rule action to the option risk manager.

THE SHAPE OF THE HANDOFF. A rule action produces an ``OptionStructureRequest``: boundaries, never
a decision. The risk manager turns each one into either a ``ResolvedStructure`` (a concrete,
priced structure, everything except how many) or a ``StructureRefusal`` (a reason, never a silent
drop). Both outcomes are returned to the caller, because a refusal nobody can see is
indistinguishable from a structure that was never proposed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ba2_common.core.option_payoff import PayoffLeg
from ba2_common.core.option_terms import OptionTerm
from ba2_common.core.option_types import OptionLeg

# --- refusal phrases ------------------------------------------------------------------------
#
# STABLE, GREPPABLE STRINGS. Logs, the UI and tests all key on these, so they are constants and
# not inline literals — the same discipline as ``option_economics.ARC_FLOOR_REFUSAL`` and
# ``OptionsAccountInterface.ASSIGNMENT_CAPACITY_REFUSAL``, which exist so that three refusals
# with three different remedies can be told apart by a caller that only sees the message.

UNDEFINED_RISK_REFUSAL = "structure carries unbounded loss and undefined risk is not allowed"
MAX_LOSS_UNMEASURABLE_REFUSAL = "maximum loss is unmeasurable"
CONFIDENCE_UNMEASURABLE_REFUSAL = "recommendation carries no confidence"
TARGET_UNMEASURABLE_REFUSAL = "no target price, so payoff at target cannot be evaluated"
NEGATIVE_EXPECTANCY_REFUSAL = "payoff at the recommendation's own target is negative"
BUYING_POWER_REFUSAL = "reserve exceeds available buying power"
BUDGET_EXHAUSTED_REFUSAL = "instrument or book budget exhausted"
EMPTY_BOX_REFUSAL = "no selectable contract in the requested box"

# The eight above were derived from the design document. These five were derived from the CODE:
# a survey of all 17 entry builders (2026-08-27) catalogued every `_result(False, ...)` they
# emit, and these kinds had no registered phrase -- so `StructureRefusal` would have RAISED on
# any of them, which is the opposite of the "a reason, never a silent drop" contract.
EMPTY_CHAIN_REFUSAL = "the option chain came back empty"
NO_LIQUID_CONTRACT_REFUSAL = "no contract survived the liquidity gates"
MISSING_QUOTE_REFUSAL = "the selected contract carries no usable quote"
NON_POSITIVE_NET_REFUSAL = "the structure prices to a non-positive net"
SELECTION_CONFIG_REFUSAL = "a selection parameter can never select anything"

# THIS IS NOT ``BUDGET_EXHAUSTED_REFUSAL`` AND THE TWO MUST NEVER BE MERGED. They are one word
# apart in English and opposite in what an operator should DO. Exhausted means there is nothing
# left to spend, so the remedy is to wait or to close something. This one means there IS budget
# and it is smaller than the cheapest contract in the box, so the remedy is to widen the box
# toward cheaper strikes or to raise the per-structure cap. Reporting the first when the second
# is true tells the operator to wait for room that was there the whole time.
#
# NOR IS IT ``EMPTY_BOX_REFUSAL``: the box was NOT empty. It held contracts and the budget
# ceiling excluded them, and the phrase says "the cheapest contract in the box" precisely because
# there IS one. Those two are the pair a single "nothing survived the filters" would collapse,
# and telling them apart is the whole reason this constant exists.
BUDGET_CEILING_REFUSAL = "the cheapest contract in the box exceeds the max-loss ceiling"

#: Every phrase above. ``StructureRefusal`` validates against this so a free-text reason cannot
#: creep in — the phrases are only useful if they are exhaustive and stable.
REFUSAL_PHRASES = (
    UNDEFINED_RISK_REFUSAL,
    MAX_LOSS_UNMEASURABLE_REFUSAL,
    CONFIDENCE_UNMEASURABLE_REFUSAL,
    TARGET_UNMEASURABLE_REFUSAL,
    NEGATIVE_EXPECTANCY_REFUSAL,
    BUYING_POWER_REFUSAL,
    BUDGET_EXHAUSTED_REFUSAL,
    EMPTY_BOX_REFUSAL,
    EMPTY_CHAIN_REFUSAL,
    NO_LIQUID_CONTRACT_REFUSAL,
    MISSING_QUOTE_REFUSAL,
    NON_POSITIVE_NET_REFUSAL,
    SELECTION_CONFIG_REFUSAL,
    BUDGET_CEILING_REFUSAL,
)


def validate_refusal_phrase(phrase: str) -> None:
    """Raise unless ``phrase`` is registered above.

    A FUNCTION RATHER THAN A COPY OF THE CHECK, because ``StructureRefusal`` is no longer the
    only refusal object: ``option_selection_policy.SelectionRefusal`` reports the same phrases
    from a layer that has no ``OptionStructureRequest`` to attach them to. Two copies of the rule
    is how one of them ends up accepting free text -- and a reason nobody can grep for is a
    reason nobody reads, which is the failure the registry exists to abolish.
    """
    if phrase not in REFUSAL_PHRASES:
        raise ValueError(
            f"Unregistered refusal phrase {phrase!r}. Callers grep for these, so a "
            f"free-text reason is invisible to them; add it to REFUSAL_PHRASES.")


@dataclass(frozen=True)
class OptionStructureRequest:
    """What a rule action PROPOSES. Boundaries only — it never decides a contract or a size.

    Frozen on purpose: the risk manager holds several of these at once while triaging, and
    resolving one must not be able to mutate a proposal another candidate is still measured
    against.

    ``term`` wins over ``dte_min``/``dte_max`` when set. Both survive because fourteen live rules
    still carry the raw window and must keep working unchanged.

    ``resolver`` is the ``_OptionEntryAction`` instance that produced this request. Typed ``Any``
    to keep this module free of a ``TradeActions`` import (which would be circular). Carrying the
    instance is deliberate: it already holds the account, the recommendation and the gates, so
    the risk manager can resolve without reconstructing any of it.
    """

    structure: str                              # ExpertActionType value
    symbol: str
    expert_recommendation_id: int
    term: Optional[OptionTerm] = None
    dte_min: Optional[int] = None
    dte_max: Optional[int] = None
    strike_method: Optional[str] = None         # delta | percent_otm | consensus_target
    box_min: Optional[float] = None
    box_max: Optional[float] = None
    wing_width_pct: Optional[float] = None
    min_open_interest: Optional[int] = None
    max_spread_pct: Optional[float] = None
    min_volume: Optional[int] = None
    min_arc: Optional[float] = None
    sizing_pct: Optional[float] = None
    resolver: Any = None


#: The three ways a structure's size is decided today. `_size` divides the budget by
#: `premium * 100`; `_size_by_reserve` divides it by the collateral; the two overlays divide
#: held shares by 100 and ignore the budget entirely. Naming the family on the resolution is
#: what lets ONE shared tail size all three without a chain of isinstance checks.
SIZING_BASES = ("premium", "reserve", "held_shares")


@dataclass(frozen=True)
class ResolvedStructure:
    """A concrete, priced structure — everything a SINGLE action can know.

    Deliberately carries no ``score``, no ``payoff_at_target`` and no ``max_loss_per_contract``.
    A score depends on the other candidates on the bar and a payoff-at-target depends on the
    recommendation's target price; an action resolving one structure for one symbol can produce
    neither, and giving it fields it cannot fill would mean every builder inventing a number.
    Those three live on ``ScoredStructure``, which the risk manager builds in Phase 3.

    ``cost_per_contract`` is the dollars ONE contract consumes of the sizing budget. It is the
    common denominator of the two existing sizers -- ``premium * 100`` for ``_size`` and the
    collateral for ``_size_by_reserve`` -- so expressing it once here is what allows a single
    shared sizing tail.
    """

    request: OptionStructureRequest
    legs: List[OptionLeg]                       # what the broker is asked for
    payoff_legs: List[PayoffLeg]                # what the payoff evaluator measures
    limit_price: float                          # the net, signed as _submit_option_order wants
    option_strategy: str                        # reserve-table strategy name
    dte: int
    reserve_per_contract: float
    cost_per_contract: float
    sizing_basis: str
    #: The EXACT "insufficient budget" message this structure used to emit from its own tail.
    #: Carried rather than derived because it is not derivable: the label differs per structure
    #: (`premium=` for singles, `net_debit=` for debit multi-leg, `strike=` and `max_loss=` for
    #: the credit builders in 2b), several structures have no parenthetical at all, and the
    #: butterfly names itself "butterfly" where its option_strategy is "call_butterfly".
    #:
    #: It matters because `_result` PERSISTS this into `TradeActionResult.message` and the UI
    #: renders it as the reason an entry did not fire. A refactor advertised as behaviour-neutral
    #: that quietly rewords five of seven user-visible strings is not behaviour-neutral.
    budget_refusal_message: Optional[str] = None
    reserve_kwargs: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.sizing_basis not in SIZING_BASES:
            raise ValueError(
                f"Unknown sizing_basis {self.sizing_basis!r}; expected one of "
                f"{list(SIZING_BASES)}. Phase 2a's shared tail does NOT yet dispatch on this — "
                f"it sizes every structure off cost_per_contract, which is correct only for the "
                f"7 premium-sized builders that exist today. The field is validated now so that "
                f"2b's reserve builders and 2c's held-shares overlays cannot be routed through "
                f"a tail that has no branch for them.")


@dataclass(frozen=True)
class ScoredStructure:
    """A resolved structure plus the numbers only the risk manager can compute.

    Separate from ``ResolvedStructure`` because these three need inputs an action does not
    have: the recommendation's target price, and the rest of the bar's candidates.
    """

    resolved: ResolvedStructure
    max_loss_per_contract: float
    payoff_at_target: float
    score: float


@dataclass(frozen=True)
class StructureRefusal:
    """Why one proposal produced no order. Always returned, never swallowed."""

    request: OptionStructureRequest
    phrase: str
    detail: str

    def __post_init__(self):
        validate_refusal_phrase(self.phrase)
