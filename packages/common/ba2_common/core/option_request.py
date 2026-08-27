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
)


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


@dataclass(frozen=True)
class ResolvedStructure:
    """A concrete, priced structure — everything except HOW MANY.

    Quantity is deliberately absent. It is the risk manager's decision and depends on the other
    candidates on the bar, so a resolved structure that already carried one would be making a
    portfolio decision from inside a single symbol's evaluation.
    """

    request: OptionStructureRequest
    legs: List[OptionLeg]                       # what the broker is asked for
    payoff_legs: List[PayoffLeg]                # what the payoff evaluator measures
    limit_price: float
    option_strategy: str                        # reserve-table strategy name
    dte: int
    max_loss_per_contract: float
    reserve_per_contract: float
    payoff_at_target: float
    score: float
    reserve_kwargs: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StructureRefusal:
    """Why one proposal produced no order. Always returned, never swallowed."""

    request: OptionStructureRequest
    phrase: str
    detail: str

    def __post_init__(self):
        if self.phrase not in REFUSAL_PHRASES:
            raise ValueError(
                f"Unregistered refusal phrase {self.phrase!r}. Callers grep for these, so a "
                f"free-text reason is invisible to them; add it to REFUSAL_PHRASES.")
