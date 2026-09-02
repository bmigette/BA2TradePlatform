"""Which expiry does an option structure MEAN? — the one shared answer.

WHY THIS MODULE EXISTS
----------------------
``Transaction.expiry`` holds ONE date and calls it "the structure's expiry". That was
honest only because all 16 supported structures — the four singles, the four verticals,
straddle/strangle and their short forms, iron condor, jade lizard, call butterfly, put
ratio spread — put every leg on one expiry. Three independent sites therefore refused, or
declined to answer for, a structure whose legs disagreed:

* ``OptionsAccountInterface.submit_option_order``'s single-expiry guard,
* ``option_lifecycle._dte``'s conflicting-expiries rule,
* ``TradeConditions.DaysToExpiryCondition``'s union check.

A diagonal (PMCC) and a calendar break that assumption on purpose. **Per-leg storage was
never the obstacle** — ``TradingOrder.expiry`` has been a real column since alembic
``08de6c7b6eed`` and ``submit_option_order`` already writes one child row per leg carrying
its own date (``OptionsAccountInterface`` line ~391), linked by ``parent_order_id``.
``Transaction.expiry`` is a denormalised SUMMARY of a set that is already recorded per leg.
What was missing is a RULE for reading those legs when they disagree, and that is all this
module supplies.

THE RULE IS NAMED, NOT INFERRED
-------------------------------
Ambiguity is the entire reason the guard existed, so a caller states which question it is
asking and the answer's side follows from the question:

* :data:`EXPIRY_RULE_ROLL_WINDOW` -> the **SHORT** leg. "When must the overlay be rolled?"
  A PMCC rolls when the short call expires; reading the LEAPS here would put the roll a
  year away and the roll branch would never fire.
* :data:`EXPIRY_RULE_STRUCTURE_EXIT` -> the **LONG** leg. "Is there still life to roll
  into?" That is the roll FLOOR, and it is the LEAPS; reading the short here would exit
  the whole structure every time the overlay approached its own expiry.

(Design ``docs/superpowers/specs/2026-08-31-leaps-grid-design.md`` §4: "Roll loop: at short
expiry"; "Structure exit: long-leg DTE floor".)

The ``opt_time`` grid exit is deliberately absent from that table: it compares
``days_opened``, elapsed time since the position opened, and reads no expiry and therefore
no leg. It is named here only so nobody goes looking for a leg rule it does not have.

WHAT IS AND IS NOT RELAXED
--------------------------
Disagreement stays a contradiction for every strategy that has not been DECLARED
multi-expiry in :data:`MULTI_EXPIRY_OPTION_STRATEGIES`. That is fail-closed by
construction: membership is opt-in, so ``None``, ``""`` and any unrecognised tag are all
"not declared", and the guard's refusal is unchanged for them.

A single-expiry structure answers identically under BOTH rules, with
``rule_applied=None`` — which is what keeps every existing builder byte-identical. No
caller of this module can change the answer for a structure that has one expiry.

WHY A RESULT AND NOT A MESSAGE
------------------------------
``resolve_structure_expiry`` returns an :class:`ExpiryResolution`, never prose. The two
readers word their "unknown because…" strings differently (one speaks of "the roll window",
the other of "remaining life" and names the instrument), those strings are asserted
verbatim by their own suites, and they are rendered into audit rows. Sharing the wording
would either move them or force a lowest-common-denominator message. What is shared is the
part that is actually risky and must not diverge: the SELECTION.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional, Tuple

#: A netted quantity this small is zero. Matches ``option_lifecycle``'s own tolerance: the
#: two modules net the same rows and must agree on which legs are still held.
_EPS = 1e-9


#: Strategies whose legs are ALLOWED to span more than one expiry.
#:
#: The single declaration, read by all three sites that used to refuse outright, so the
#: relaxation can never drift between the submit guard and the two readers.
#:
#: ``"calendar_spread"`` is deliberately NOT here. ``O_CAL`` is phase-2 in the launcher's
#: ``_PHASE_GATED_OPTION_STRATEGIES``, gated behind PMCC proving the two-expiry lifecycle
#: first, and the guard suite pins that a two-expiry submit tagged ``calendar_spread`` is
#: still refused. Adding it is the one-line change phase 2 makes.
#:
#: ``"pmcc"`` has no builder yet — that is plan Task 6. This constant opens the door; Task 6
#: walks through it. ``O_PMCC`` stays phase-gated meanwhile, so nothing in the grid can
#: reach the relaxed path until the lifecycle that manages it exists.
#: The poor-man's-covered-call tag, as ONE string rather than a literal repeated at each of
#: the sites that now read it (the submit guard, both DTE readers, the builder, the roll
#: action, the launcher's strategy row). ``OpenPMCCAction`` submits under it and
#: ``resolve_structure_expiry`` reads the legs by side because of it.
PMCC_STRATEGY = "pmcc"

MULTI_EXPIRY_OPTION_STRATEGIES: frozenset = frozenset({PMCC_STRATEGY})


#: The roll-window question -> the SHORT leg. "When must the overlay be rolled?"
EXPIRY_RULE_ROLL_WINDOW = "roll_window"

#: The roll-floor / structure-exit question -> the LONG leg. "Is there life left to roll
#: into?"
EXPIRY_RULE_STRUCTURE_EXIT = "structure_exit"

_RULES = (EXPIRY_RULE_ROLL_WINDOW, EXPIRY_RULE_STRUCTURE_EXIT)


def is_multi_expiry_strategy(strategy: Optional[str]) -> bool:
    """Has ``strategy`` been declared as legitimately spanning two expiries?

    Fail-closed: anything that is not an exact (case- and whitespace-insensitive) member is
    ``False``, including ``None`` and ``""``. A stale or absent strategy tag must never
    unlock the relaxation.
    """
    if not isinstance(strategy, str):
        return False
    return strategy.strip().lower() in MULTI_EXPIRY_OPTION_STRATEGIES


@dataclass(frozen=True)
class ExpiryLeg:
    """One leg, reduced to the only two facts an expiry question needs.

    ``net_qty`` is SIGNED in contracts, BUY positive — the same convention
    ``option_lifecycle.LifecycleLeg`` and ``DaysToExpiryCondition._held_legs`` net
    with. A leg is HELD when ``abs(net_qty) > _EPS`` and SHORT when ``net_qty < -_EPS``, so
    a leg bought back to close nets to zero and stops contributing its (now stale) date.

    Callers pass their legs unfiltered; holding is decided here, once, rather than at each
    of the two call sites.
    """

    expiry: Optional[date]
    net_qty: float

    @property
    def is_held(self) -> bool:
        return abs(self.net_qty) > _EPS

    @property
    def is_short(self) -> bool:
        return self.net_qty < -_EPS

    @property
    def is_long(self) -> bool:
        return self.net_qty > _EPS


@dataclass(frozen=True)
class ExpiryResolution:
    """The answer, or precisely why there isn't one. Never prose, never a guess.

    Exactly one of three states holds:

    * **answered** — ``expiry`` set, ``conflict == ()``, ``missing`` False.
    * **missing** — no leg and no declared value carried a date at all. ``missing`` True.
    * **conflict** — the candidates disagreed and no named rule could adjudicate them;
      ``conflict`` lists them sorted, for the caller's own message.

    ``rule_applied`` is the named rule that picked the answer, and is ``None`` whenever the
    structure had a single expiry — a per-leg rule was not needed, so none may be claimed.
    That is how a caller can tell "this is the ordinary single-expiry answer" from "a leg
    rule was exercised".
    """

    expiry: Optional[date] = None
    conflict: Tuple[date, ...] = ()
    missing: bool = False
    rule_applied: Optional[str] = None


def resolve_structure_expiry(legs: Iterable[ExpiryLeg], *, strategy: Optional[str],
                             rule: str,
                             declared_expiries: Iterable[Optional[date]] = ()
                             ) -> ExpiryResolution:
    """Resolve one structure's expiry for ONE named question.

    :param legs: the structure's legs (any iterable of :class:`ExpiryLeg`); unheld legs and
        legs with no recorded expiry are filtered here, not by the caller.
    :param strategy: the structure's strategy tag, checked against
        :data:`MULTI_EXPIRY_OPTION_STRATEGIES`.
    :param rule: :data:`EXPIRY_RULE_ROLL_WINDOW` or :data:`EXPIRY_RULE_STRUCTURE_EXIT`. An
        unrecognised value raises — a typo must not silently become a fall-through branch.
    :param declared_expiries: the STRUCTURE-LEVEL values, of which there may be more than
        one source: ``DaysToExpiryCondition`` reads both ``Transaction.expiry`` and the
        parent ``TradingOrder.expiry``, and two sources that disagree is itself a
        contradiction the caller must not lose. ``None`` entries are ignored. Historical
        rows carry these and no leg dates at all, which is what keeps them measurable.

    A leg with no expiry is UNKNOWN, not a second expiry: it adds no information and never
    vetoes the legs that do. That is load-bearing for the close paths, which rebuild legs
    from stored rows and may produce ``expiry=None``.
    """
    if rule not in _RULES:
        raise ValueError(
            f"unknown expiry rule {rule!r}: a caller must NAME the question it is asking "
            f"(one of {', '.join(repr(r) for r in _RULES)}). The side a two-expiry "
            f"structure is read from is never inferred."
        )

    held = [leg for leg in legs if leg.is_held and leg.expiry is not None]

    candidates = {leg.expiry for leg in held}
    candidates |= {d for d in declared_expiries if d is not None}

    if not candidates:
        return ExpiryResolution(missing=True)
    if len(candidates) == 1:
        # THE UNCHANGED PATH, and the one every existing structure takes. Both rules give
        # the same answer, and neither is recorded as having been applied.
        return ExpiryResolution(expiry=next(iter(candidates)))

    listed = tuple(sorted(candidates))

    if not is_multi_expiry_strategy(strategy):
        # Today's behaviour, preserved exactly: a structure whose own rows disagree about
        # when it expires has no expiry. Not min() (closes early), not max() (never
        # closes) — both would be inventing one.
        return ExpiryResolution(conflict=listed)

    # DECLARED multi-expiry: the legs ARE the record, so the named rule reads them. The
    # structure-level ``declared_expiries`` are deliberately not consulted from here — a real
    # two-expiry structure records NULL there (``submit_option_order`` writes the parent's
    # expiry only when the legs share one), so any value present is stale and must not be
    # able to become the answer.
    if rule == EXPIRY_RULE_ROLL_WINDOW:
        side = [leg for leg in held if leg.is_short]
    else:
        side = [leg for leg in held if leg.is_long]

    if not side:
        # FAIL-CLOSED. Falling through to the other side would answer a different question
        # than the one asked: a roll window taken from the LEAPS schedules the roll a year
        # out, and a structure floor taken from the overlay reports healthy life for a
        # position holding nothing but a naked short.
        return ExpiryResolution(conflict=listed)

    # The NEAREST leg on the requested side binds: with a roll in flight there can be two
    # shorts, and it is the soonest that forces the next decision.
    return ExpiryResolution(expiry=min(leg.expiry for leg in side), rule_applied=rule)
