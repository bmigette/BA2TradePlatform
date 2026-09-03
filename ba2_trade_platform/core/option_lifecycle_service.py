"""The LIVE option lifecycle pass: the runner that finally wires the pure modules in.

``option_lifecycle`` (Task 6) and ``option_book`` (Task 7) are pure — positions and chain
in, decisions out, no DB, no broker, no clock. This module is the only thing that gives them
a database and a broker. Before it existed neither was called from anywhere but its own test
file: two heavily-tested modules of dead code.

What the pass does, in order, for ONE expert sleeve:

1. refuse to act against a book it cannot see (``get_positions()``);
2. load the expert's OPENED option transactions and net their legs into
   ``OptionStructure`` values;
3. fetch one option chain per (underlying, expiry) and index it by contract symbol;
4. total the sleeve (``book_totals``) and ratchet/test the drawdown breaker
   (``update_sleeve_breaker`` -- the SHARED transition the backtest's per-bar flow also
   calls), feeding its EDGE into the decision via ``breaker_signal``;
5. measure the shares still covering every ``covered_call`` (``held_shares_for_cover``),
   one position read per ticker;
6. ask ``option_lifecycle.decide`` for exactly one decision per structure;
7. dispatch EVERY decision through ``LIFECYCLE_DISPOSITIONS``: submit a close for each
   closing reason (guarded by ``has_pending_closing_order``), report the unknowns, record
   a due roll the ruleset owns, and RAISE on a reason the table does not name.


Every decision is acted on or refused loudly
--------------------------------------------
There is no path here on which ``decide`` returns a decision and nothing happens except
``LIFECYCLE_HOLD``, which is the one reason that means "nothing is due". The dispatch used
to end in ``if not decision.should_close: continue``, which is a correct reading of HOLD and
a wrong one of everything else -- and ``LIFECYCLE_ROLL_SHORT``, deliberately not a closing
reason, fell through it in silence for an entire branch (final review §1b/A1). The table is
now the loop's only guide and an unlisted reason raises, so the next reason added upstream
cannot be ignored here by omission.

The roll is the one decision this pass computes and does not perform, and that is not a
gap: ``roll_pmcc_short`` is a RULE, walked by both runtimes off the same ruleset. Rolling
here as well would give live a second roller on a different threshold. So the decision is
recorded (``roll_due``), and the case the rule cannot report on itself -- a sleeve holding a
two-expiry structure whose ruleset has no roll rule -- is raised as ``UnownedRollError``
after everything else in the pass has been acted on.


It must not invoke an expert, and that is the point
---------------------------------------------------
Roll at 21 DTE, capture 50% of a credit, defend a tested short, trip a drawdown breaker: not
one of those needs an opinion about the underlying. The ledger and the chain already answer
them. Paying for an FMP call plus an LLM analysis to discover that a spread is at 21 DTE is
precisely the cost behind the project's "options must be as fast as stocks" requirement. The
expert analyses still run afterwards, on whatever the pass left open, and still own anything
that genuinely needs a view.

Nothing in this module imports an expert, submits a ``MarketAnalysis`` or touches the
``WorkerQueue``.


Unknown is never a value
------------------------
``LIFECYCLE_UNKNOWN`` means the decision could not be made. It is NOT a hold, and this
module must never let it read as one:

* every unknown decision is collected in ``LifecyclePassResult.unknown`` and logged at
  WARNING naming the input that was missing;
* a structure whose legs cannot be netted is reported, not skipped in silence;
* a P&L we cannot compute stays ``None`` and never becomes ``0.0``.

Collapsing "we cannot measure this" into "this is fine" is what hid a dead roll-DTE gene for
an entire GA campaign.


The continuous cover monitor
----------------------------
Every ``covered_call`` in the sleeve is re-checked against the shares that actually
cover it, on every pass. The entry seam (``check_cover_for_covered_call``) refuses to
WRITE an uncovered one and the exit seam refuses to SELL shares pledged to one; neither
can see the case that actually happens, which is that a broker-side risk-manager stop —
submitted as an OCO leg by ``TradeRiskManagement`` — fills at 3am. The shares are gone,
no platform code ran, and the short call is naked until somebody looks. This pass is
what looks, and because it runs ahead of OPEN_POSITIONS (``JobManager``), a cover lost
overnight is acted on before any new entry is considered that cycle.

Two things it deliberately does NOT do:

* it does not consult ``shares_pledged_to_short_calls``. That accessor counts EVERY open
  short call on the ticker — including this very structure's own — so ``held - pledged``
  is 0 for a perfectly covered call, and the "free cover" figure the ENTRY guard needs
  would liquidate every healthy covered call here. It would also count the short leg of
  a credit spread on the same ticker, whose cover is a long call; refusing an entry over
  that is fail-safe, closing a position over it is not;
* it does not treat an UNMEASURABLE cover as a lost one. ``held_shares_for_cover`` is
  tri-state and ``None`` means the POSITION FEED did not answer. Nothing is closed on
  that, and it is logged at ERROR naming the transaction and saying so, because an
  operator seeing no action must be able to tell "the cover is fine" from "I could not
  tell". ``LifecyclePassResult.cover_unmeasurable`` carries the same fact in code.

Cover is allocated oldest-transaction-first when one ticker carries several covered
calls, so 200 shares held as two lots against two calls report one naked structure when
one lot is sold rather than two comfortable ones. KNOWN GAP, stated rather than hidden:
the allocation only sees THIS sleeve's structures, so a covered call written by another
expert on the same ticker is not charged against the shares here.


A stand-down suppresses ENTRIES, not EXITS
------------------------------------------
``OptionPortfolioManager.manage_open`` returned ``[]`` on every bar while ``self._halted``,
so the latch suppressed *exits*. That is the wrong half, and it is a real hole:

* the breaker signals the **edge** (``BreakerState.tripped``), not the latch, precisely so a
  flat book is not re-flattened every bar. A structure the flatten failed to close — the
  broker rejected it, or a manual close was already working — therefore never receives
  ``LIFECYCLE_BREAKER`` again;
* with exits also suppressed, that structure is never managed again by ANY rule: no profit
  capture, no stop, no roll. It runs to expiry unmanaged, inside the drawdown that tripped
  the breaker;
* the thing the latch was really protecting against — re-issuing a close over a working one
  — has a purpose-built, per-transaction primitive: ``has_pending_closing_order``. A
  book-wide latch is a blunt substitute that also blocks the closes that *should* happen;
* entry suppression, the half that is genuinely correct, already lives in
  ``option_book.check_rails``, which declines every candidate while ``halted`` ahead of every
  rail.

So this pass runs its exit rules on **every** evaluation regardless of ``halted``, and
``LifecyclePassResult.breaker`` carries the stand-down out to whatever opens positions.
``cover_lost`` obeys the same rule and for the same reason: it suppresses ENTRIES (the
entry seam already refuses to write a covered call whose cover it cannot find), never
EXITS. A book that has lost its cover must above all still be able to CLOSE, so a
cover-lost structure is submitted for closing like any other, an unmeasurable cover
stops nothing on any OTHER structure, and neither ever aborts the pass.
The deleted PremiumSeller expert pinned the opposite (its
``test_circuit_breaker_flattens_and_halts`` suppressed exits while halted). That expert,
its package and its tests were removed on 2026-08-31 (option-model plan Task 12) -- the
path they lived at no longer exists, so nothing here should be read as still pointing at
one -- and its behaviour was deliberately NOT treated as a specification.


The breaker peak is process state
---------------------------------
``BreakerState`` is a value and something has to carry it between evaluations. It is held in
a module-level map keyed by expert instance, exactly as ``OptionPortfolioManager`` held
``self._peak_equity`` on the manager. **A process restart forgets the peak**, and the sleeve
then measures its next drawdown from wherever equity stands at restart. That is a known
limitation of this task (persisting it needs a column, i.e. a migration) and it is stated
rather than hidden. ``rearm_breaker`` is the operator override.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import ba2_common.core.OptionRiskManagement as _rm
from ba2_common.core.option_book import (
    BookTotals, BreakerState, book_totals, breaker_signal,
)
from ba2_common.core.OptionRiskManagement import update_sleeve_breaker
from ba2_common.core.option_lifecycle import (
    COVER_REQUIREMENT_UNMEASURABLE, COVERED_CALL_STRATEGY, LIFECYCLE_BREAKER,
    LIFECYCLE_CLOSING_REASONS, LIFECYCLE_COVER_LOST, LIFECYCLE_CREDIT_STOP, LIFECYCLE_HOLD,
    LIFECYCLE_PROFIT_CAPTURE, LIFECYCLE_ROLL_SHORT, LIFECYCLE_TESTED,
    LIFECYCLE_UNKNOWN, LifecycleDecision, LifecycleLeg, OptionStructure, decide,
)
from ba2_common.core.option_types import OptionContract, OptionLeg
from ba2_common.core.rule_builders import rule_carries_action
from ba2_common.core.types import ExpertActionType

from ..logger import logger

_EPS = 1e-9

#: Thresholds ``option_lifecycle.decide`` reads on EVERY structure, plus the one
#: ``option_book.update_breaker`` needs (declared on ``MarketExpertInterface``, with no
#: defaults -- see that block). An expert missing any of these cannot be managed,
#: and that is a loud configuration error rather than a substituted risk threshold.
REQUIRED_SETTINGS: Tuple[str, ...] = (
    "profit_capture_pct", "roll_dte", "tested_delta_enabled", "dr_stop_enabled",
    "ur_stop_enabled", "circuit_breaker_pct",
)

#: Thresholds only some structures need (a strangle's capture, the stop multiples, the
#: tested threshold) plus the sleeve rails ``check_rails`` consumes. Passed through when the
#: expert declares them; a rule that needs one the expert did not declare raises inside
#: ``decide`` and is reported by name — never defaulted.
OPTIONAL_SETTINGS: Tuple[str, ...] = (
    "strangle_capture_pct", "tested_delta", "dr_stop_credit_mult", "ur_stop_credit_mult",
    "max_deployment_pct", "undefined_risk_max_pct", "max_notional_leverage",
    "max_concurrent_structures",
)


# ---------------------------------------------------------------------------
# THE TOTALITY TABLE: what this pass does with every reason ``decide`` can return
# ---------------------------------------------------------------------------
#: What the dispatch loop does with a decision. Four dispositions, and every
#: ``LIFECYCLE_*`` reason has exactly one.
DISPOSITION_CLOSE = "close"            # submit a closing order
DISPOSITION_REPORT = "report"          # no order; recorded and logged as an alarm
DISPOSITION_RULE_OWNED = "rule-owned"  # no order HERE; a ruleset action performs it
DISPOSITION_NO_ACTION = "no action"    # nothing is wrong and nothing is due

#: EVERY reason ``option_lifecycle.decide`` can return, and what happens to it. The dispatch
#: loop is driven off this table and RAISES on a reason that is not in it, so a new
#: ``LIFECYCLE_*`` constant cannot be added upstream and quietly ignored here -- which is
#: exactly how ``LIFECYCLE_ROLL_SHORT`` came to be computed and then dropped on the floor for
#: an entire branch (final review §1b/A1). Totality is pinned by
#: ``tests/test_option_lifecycle_service.py::test_every_lifecycle_reason_has_a_disposition``,
#: which enumerates the constants by REFLECTION over ``option_lifecycle`` rather than by a
#: hand-copied list, so the pin cannot go stale either.
LIFECYCLE_DISPOSITIONS: Dict[str, str] = {
    LIFECYCLE_BREAKER: DISPOSITION_CLOSE,
    LIFECYCLE_COVER_LOST: DISPOSITION_CLOSE,     # AND reported: it is an incident
    LIFECYCLE_PROFIT_CAPTURE: DISPOSITION_CLOSE,
    LIFECYCLE_CREDIT_STOP: DISPOSITION_CLOSE,
    LIFECYCLE_TESTED: DISPOSITION_CLOSE,
    # NO roll-DTE close. It was removed from ``decide`` on 2026-09-03 and the ``opt_dte``
    # RULE owns that exit in both runtimes; a disposition for it here would be a table entry
    # for a reason that can no longer arrive, and the reflection pin refuses one.
    LIFECYCLE_ROLL_SHORT: DISPOSITION_RULE_OWNED,
    LIFECYCLE_UNKNOWN: DISPOSITION_REPORT,
    LIFECYCLE_HOLD: DISPOSITION_NO_ACTION,
}

#: The ruleset action that OWNS a ``LIFECYCLE_ROLL_SHORT``. The roll is a RULE, not an engine
#: hook (``TradeActions.RollPMCCShortAction``'s own argument), and it is walked by BOTH
#: runtimes through ``TradeActionEvaluator``. This pass therefore must not roll: a second
#: roller here would be a live-only mechanism with its own threshold (``roll_dte``, a setting)
#: racing the searched one (``pmcc_roll_dte``, a ruleset param), i.e. a NEW parity gap. What
#: it must do is refuse to be silent -- see ``_report_roll_due``.
ROLL_SHORT_OWNER_ACTION: str = ExpertActionType.ROLL_PMCC_SHORT.value


class UnownedRollError(RuntimeError):
    """A maintenance roll is DUE and nothing in either runtime will perform it.

    Raised at the END of the dispatch loop, after every other decision has been acted on, so
    one misconfigured structure cannot stop the rest of the sleeve being closed. The caller
    (``JobManager._run_open_positions_analysis``) already contains a raising pass and logs it
    at ERROR before continuing with the analyses, so this surfaces as a loud, named
    configuration failure rather than either a crash or a silence.

    The configuration it names is real and reachable: a ``pmcc`` structure whose expert's
    OPEN_POSITIONS ruleset carries no ``roll_pmcc_short`` rule has an overlay that will expire
    against a LEAPS nobody bought back -- assignment, not a roll.
    """


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SubmittedClose:
    """One closing order the pass actually put on the wire."""
    transaction_id: int
    reason: str
    legs: Tuple[OptionLeg, ...]
    order: Any


@dataclass
class LifecyclePassResult:
    """Everything one pass over one sleeve decided, did, and could not do.

    Deliberately verbose. The whole point of the pass is that an option position is never
    silently unmanaged, so every outcome has a field a caller (and a test) can read:
    ``unknown`` is not folded into ``decisions``' holds, ``skipped_pending_close`` is not
    folded into "nothing to do", and ``failed`` is not folded into ``submitted``.
    """
    expert_instance_id: int
    as_of: datetime
    aborted: bool = False
    abort_reason: str = ""
    decisions: List[LifecycleDecision] = field(default_factory=list)
    #: Decisions whose reason is LIFECYCLE_UNKNOWN. A subset of ``decisions``, surfaced
    #: separately because an unknown that reads as a hold is the failure this work exists
    #: to remove.
    unknown: List[LifecycleDecision] = field(default_factory=list)
    submitted: List[SubmittedClose] = field(default_factory=list)
    #: Transactions whose close was correctly withheld: one is already working.
    skipped_pending_close: List[int] = field(default_factory=list)
    #: Transactions whose close was decided but did NOT reach the broker.
    failed: List[int] = field(default_factory=list)
    #: Transactions that could not be turned into a structure at all (no legs on record).
    unbuildable: List[int] = field(default_factory=list)
    #: Decisions whose reason is LIFECYCLE_COVER_LOST: a ``covered_call`` whose shares are
    #: MEASURABLY gone, i.e. a naked short call. A subset of ``decisions``, surfaced
    #: separately because it is an incident and not merely another exit — and because it
    #: is the fact a caller has to see to stand down from writing more of them.
    cover_lost: List[LifecycleDecision] = field(default_factory=list)
    #: Transactions whose cover could NOT be measured, so the cover rule closed nothing
    #: for them (another rule still might). Kept apart from ``cover_lost`` on purpose:
    #: "the cover is gone" and "I could not tell" are different facts, and an operator
    #: seeing no action needs to know which one he is looking at.
    cover_unmeasurable: List[int] = field(default_factory=list)
    #: Decisions whose reason is LIFECYCLE_ROLL_SHORT: a two-expiry structure whose overlay
    #: is due to be rolled. This pass does NOT roll them (``ROLL_SHORT_OWNER_ACTION`` does,
    #: in both runtimes) — but it observes them, because a decision that is computed and
    #: then dropped is the exact failure this bucket exists to make impossible.
    roll_due: List[LifecycleDecision] = field(default_factory=list)
    #: Transaction ids from ``roll_due`` whose expert has NO ``roll_pmcc_short`` rule, so
    #: the roll is due and NOTHING will perform it. An ``UnownedRollError`` is raised for
    #: these after the rest of the sleeve has been acted on.
    roll_unowned: List[int] = field(default_factory=list)
    book: Optional[BookTotals] = None
    breaker: BreakerState = field(default_factory=BreakerState)


# ---------------------------------------------------------------------------
# breaker state (process-lifetime, see the module docstring)
# ---------------------------------------------------------------------------
# ONE LATCH, TWO CONSUMERS. The store moved to ``ba2_common.core.OptionRiskManagement``
# when the entry rails were finally wired (design 2026-08-27 SS4, finding F5): this pass
# owns the TRANSITIONS and the shared entry gate READS the same map to decline every
# candidate while the sleeve stands down. Two maps would have meant a breaker that flattens
# the book here and gates nothing there, which is precisely the defect F5 recorded. These
# names stay as the module's public surface — ``JobManager`` and the tests call them — but
# they are now views on the shared store.
#
# The transition itself moved to the shared module on 2026-09-01
# (``OptionRiskManagement.update_sleeve_breaker``) so that ``daily_engine`` could call the
# SAME one per bar. This pass is no longer the only caller of it; it is still the only LIVE
# caller.
reset_breaker_states = _rm.reset_breaker_states
get_breaker_state = _rm.get_breaker_state


def rearm_breaker(expert_instance_id: int) -> BreakerState:
    """Clear a stand-down UNCONDITIONALLY (``option_book.rearm``): the operator override.

    Nothing calls this automatically — it re-risks a sleeve that has not recovered. The
    peak is deliberately KEPT, so the drawdown that caused the stand-down is not erased.
    """
    state = _rm.rearm_breaker(expert_instance_id)
    logger.warning(f"Option lifecycle: circuit-breaker stand-down for expert "
                   f"{expert_instance_id} cleared by an explicit re-arm")
    return state


# ---------------------------------------------------------------------------
# seams (patched in tests; live in ``core.utils``, imported late because
# ``core.utils`` <-> ``modules.experts`` is a genuine import cycle)
# ---------------------------------------------------------------------------
def _resolve_expert(expert_instance_id: int):
    from .utils import get_expert_instance_from_id
    return get_expert_instance_from_id(expert_instance_id)


def _resolve_account(account_id: int):
    from .utils import get_account_instance_from_id
    return get_account_instance_from_id(account_id)


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------
def run_option_lifecycle_pass(expert_instance_id: int,
                              as_of: Optional[datetime] = None) -> LifecyclePassResult:
    """Manage ONE expert's open option structures. Submits closes; never an analysis.

    :param expert_instance_id: the sleeve to manage.
    :param as_of:              the evaluation instant. Supplied by tests and by any caller
                               that already holds one; defaults to now.

    Returns a :class:`LifecyclePassResult` describing every decision and every action.
    Expected failures (an unreadable position book, an expert with no option thresholds, a
    threshold a rule needed) return an ``aborted`` result after logging; a genuine defect
    propagates to the caller's guard rather than being disguised as "nothing to do".
    """
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
    from .db import get_instance
    from .models import ExpertInstance

    as_of = as_of or datetime.now(timezone.utc)
    result = LifecyclePassResult(expert_instance_id, as_of)

    expert = _resolve_expert(expert_instance_id)
    if expert is None:
        return _abort(result, f"expert instance {expert_instance_id} could not be resolved "
                              f"— its option positions are NOT being managed")

    instance = get_instance(ExpertInstance, expert_instance_id)
    if instance is None:
        return _abort(result, f"expert instance {expert_instance_id} has no database row "
                              f"— its option positions are NOT being managed")

    account = _resolve_account(instance.account_id)
    if account is None:
        return _abort(result, f"account {instance.account_id} could not be resolved for "
                              f"expert {expert_instance_id} — its option positions are NOT "
                              f"being managed")

    if not isinstance(account, OptionsAccountInterface):
        # Not a failure: an equity-only broker has no option book to manage. Task 9 reports
        # the case that IS a failure — an expert HOLDING options on such an account.
        logger.debug(f"Option lifecycle: account {instance.account_id} does not support "
                     f"options; nothing to manage for expert {expert_instance_id}")
        return result

    # 1. NEVER act against a book we cannot see. `get_positions()` returning None is
    #    "the fetch failed", not "the account is flat" — the conflation that has
    #    force-closed real positions and duplicated others five times on this project.
    if not _broker_can_answer(account, expert_instance_id, result):
        return result

    # 2. The sleeve's holdings, as values.
    transactions = _open_option_transactions(expert_instance_id)
    structures = []
    for txn in transactions:
        structure = _build_structure(txn, result)
        if structure is not None:
            structures.append(structure)

    # 3. The thresholds. Missing ones are a configuration error — but only worth saying so
    #    when the sleeve actually holds something. A report that always warns gets ignored.
    settings, missing = _lifecycle_settings(expert)
    if missing:
        if not structures:
            logger.debug(f"Option lifecycle: expert {expert_instance_id} declares no option "
                         f"thresholds and holds no option structures — nothing to do")
            return result
        return _abort(result,
                      f"expert {expert_instance_id} holds {len(structures)} open option "
                      f"structure(s) but does not declare {', '.join(missing)} — refusing "
                      f"to substitute a default for a risk threshold. These positions are "
                      f"NOT being managed.")

    # 4. Quotes and greeks for every held leg.
    chain_by_symbol = _fetch_chain(account, structures)

    # 5. The sleeve total, and the drawdown breaker.
    result.book = book_totals(structures)
    if result.book.unmeasurable:
        logger.warning(f"Option lifecycle: expert {expert_instance_id} has a sleeve whose "
                       f"committed capital is UNMEASURABLE (entry rails will decline): "
                       + "; ".join(result.book.unmeasurable))

    # THE shared transition (2026-09-01): it reads the sleeve's equity, ratchets the peak,
    # tests the drawdown and STORES the latch where the ENTRY gate reads it, so a stand-down
    # decided here declines every option entry in ``option_book.check_rails`` on the next
    # cycle. The backtest's per-bar flow calls the SAME function — one implementation, two
    # callers — which is what makes the breaker mean the same thing in both runtimes.
    # It ratchets on EVERY evaluation, including a flat sleeve: `manage_open` returned before
    # ratcheting on `not holdings`, so a just-flattened sleeve stopped tracking its peak and
    # would re-arm against a stale one.
    # ``settings`` is not passed: the function reads ``circuit_breaker_pct`` through the same
    # accessor ``_lifecycle_settings`` just used, and ``missing`` above has already proved it
    # is declared here, so the value is identical and there is one reader of it.
    breaker = update_sleeve_breaker(expert=expert, account=account,
                                    expert_instance_id=expert_instance_id)
    if breaker is not None:
        result.breaker = breaker
    if result.breaker.blind:
        logger.warning(f"Option lifecycle: the drawdown breaker for expert "
                       f"{expert_instance_id} could not be evaluated — "
                       f"{result.breaker.detail}")
    elif result.breaker.tripped:
        logger.warning(f"Option lifecycle: expert {expert_instance_id} — "
                       f"{result.breaker.detail}")

    if not structures:
        return result

    # 6. The shares each covered_call is written against. Measured from the structures
    #    already built plus ONE position read per ticker — the continuous half of the
    #    cover guard, and the only thing in the platform that can notice a broker-side
    #    stop taking the cover away overnight.
    cover_required, cover_held = _cover_inputs(account, structures, expert_instance_id,
                                               result)

    # 7. One decision per structure. The breaker reaches LIFECYCLE_BREAKER through the
    #    state key option_lifecycle itself exports, so producer and consumer cannot drift.
    decide_settings = dict(settings)
    decide_settings.update(breaker_signal(result.breaker))
    try:
        result.decisions = decide(structures, chain_by_symbol, decide_settings, as_of,
                                  cover_shares_required=cover_required,
                                  cover_shares_held=cover_held)
    except KeyError as e:
        return _abort(result,
                      f"expert {expert_instance_id} does not declare a threshold one of its "
                      f"{len(structures)} open option structure(s) needs ({e}) — refusing to "
                      f"substitute a default. These positions are NOT being managed.")

    # 8. Act — and the dispatch is TOTAL. Every reason resolves to one of the four
    #    dispositions in LIFECYCLE_DISPOSITIONS, and an unlisted reason RAISES. The
    #    fall-through that used to sit here ("if not decision.should_close: continue") read
    #    every non-closing reason as "nothing to do", which is true of LIFECYCLE_HOLD and
    #    false of LIFECYCLE_ROLL_SHORT — so a computed roll was discarded in silence.
    by_id = {s.transaction_id: s for s in structures}
    txn_by_id = {t.id: t for t in transactions}
    for decision in result.decisions:
        disposition = LIFECYCLE_DISPOSITIONS.get(decision.reason)
        if disposition is None:
            raise ValueError(
                f"Option lifecycle: transaction {decision.transaction_id} decided "
                f"{decision.reason!r}, which this pass has no disposition for "
                f"({decision.detail}). A reason with no entry in LIFECYCLE_DISPOSITIONS "
                f"cannot be acted on, and acting on nothing while the sleeve holds the "
                f"position is the failure this refusal exists to prevent — add the reason "
                f"to the table with what should happen to it.")

        if disposition == DISPOSITION_NO_ACTION:
            continue

        if disposition == DISPOSITION_RULE_OWNED:
            # Computed here, PERFORMED by the ruleset action, in both runtimes. Observed
            # either way, and loud when nothing owns it.
            _report_roll_due(instance, decision, by_id[decision.transaction_id], result)
            continue

        if decision.reason == LIFECYCLE_UNKNOWN:
            # NOT a hold, and never silent: name the transaction and the missing input.
            result.unknown.append(decision)
            logger.warning(
                f"Option lifecycle: transaction {decision.transaction_id} is UNKNOWN — the "
                f"decision could not be made and this is NOT a hold: {decision.detail}")
            continue
        if decision.reason == LIFECYCLE_COVER_LOST:
            # An incident, not merely another exit: a short call on this account is NAKED
            # right now. Recorded before the close is attempted, so it is reported whether
            # or not the broker takes the order.
            result.cover_lost.append(decision)
            logger.error(
                f"Option lifecycle: transaction {decision.transaction_id} "
                f"({by_id[decision.transaction_id].underlying}) has LOST ITS COVER — "
                f"{decision.detail}. Closing it. Nothing in this platform sold those "
                f"shares this pass, so check for a broker-side stop or a manual sale.")
        # DISPOSITION_CLOSE and DISPOSITION_REPORT converge here: UNKNOWN has already
        # `continue`d, so every reason still standing is a closing one. The old
        # ``if not decision.should_close: continue`` guard is gone — it was the silent
        # fall-through — and the equivalence is asserted instead, because a closing
        # disposition on a reason ``should_close`` denies would submit nothing at all.
        if not decision.should_close:
            raise ValueError(
                f"Option lifecycle: transaction {decision.transaction_id} decided "
                f"{decision.reason!r}, which LIFECYCLE_DISPOSITIONS marks {disposition!r} "
                f"but LIFECYCLE_CLOSING_REASONS does not contain "
                f"({sorted(LIFECYCLE_CLOSING_REASONS)}). The two tables disagree, and the "
                f"position would be neither closed nor reported.")
        _close(account, txn_by_id[decision.transaction_id], by_id[decision.transaction_id],
               decision, result)

    logger.info(
        f"Option lifecycle for expert {expert_instance_id}: {len(structures)} structure(s), "
        f"{len(result.submitted)} closed, {len(result.skipped_pending_close)} already "
        f"closing, {len(result.failed)} failed, {len(result.unknown)} unknown, "
        f"{len(result.cover_lost)} cover-lost, {len(result.cover_unmeasurable)} with an "
        f"unmeasurable cover, {len(result.roll_due)} due to roll "
        f"({len(result.roll_unowned)} with NO rule to roll them)")

    # LAST, deliberately: every close above has already been submitted, so one misconfigured
    # structure cannot stop the rest of the sleeve being managed. See ``UnownedRollError``.
    if result.roll_unowned:
        raise UnownedRollError(
            f"expert {expert_instance_id}: transaction(s) "
            f"{sorted(result.roll_unowned)} need their short overlay rolled, and this "
            f"expert's OPEN_POSITIONS ruleset carries no {ROLL_SHORT_OWNER_ACTION!r} rule "
            f"— nothing in the platform will roll them, and the overlay will run to expiry "
            f"against a long nobody bought it back for. This pass deliberately does NOT "
            f"roll (a second roller here would race the searched rule); add the rule to the "
            f"ruleset, or close the structure.")
    return result


def _report_roll_due(instance, decision: LifecycleDecision, structure: OptionStructure,
                     result: LifecyclePassResult) -> None:
    """A two-expiry overlay is due to be rolled. Record it; never roll it here.

    THE ROLL IS A RULE. ``RollPMCCShortAction`` is walked by the live ``TradeManager`` and by
    ``daily_engine`` through the same ``TradeActionEvaluator``, off the same OPEN_POSITIONS
    ruleset — one implementation, two callers, which is the whole reason the backtest can be
    read as evidence about rolling at all. A roll issued from this pass would be a second
    mechanism that only live has, triggered by a different threshold (the ``roll_dte``
    expert setting rather than the searched ``pmcc_roll_dte`` rule parameter), and the two
    could disagree about the same bar.

    What this pass owes the operator is therefore not the roll but the FACT, and one alarm
    the rule cannot raise for itself: an expert holding a ``pmcc`` whose ruleset has no
    ``roll_pmcc_short`` rule reaches its overlay's expiry with nothing scheduled to buy it
    back. That is recorded here and raised at the end of the pass.
    """
    result.roll_due.append(decision)
    if _ruleset_carries_roll(getattr(instance, "open_positions_ruleset_id", None)):
        logger.info(
            f"Option lifecycle: transaction {decision.transaction_id} "
            f"({structure.underlying}) is due to roll its overlay — {decision.detail}. "
            f"NOT rolled here: the {ROLL_SHORT_OWNER_ACTION!r} rule in this expert's "
            f"OPEN_POSITIONS ruleset owns it, in both runtimes.")
        return
    result.roll_unowned.append(decision.transaction_id)
    logger.error(
        f"Option lifecycle: transaction {decision.transaction_id} ({structure.underlying}) "
        f"is due to roll its overlay — {decision.detail} — and this expert's OPEN_POSITIONS "
        f"ruleset carries NO {ROLL_SHORT_OWNER_ACTION!r} rule. Nothing will roll it. The "
        f"short leg will run to expiry against a long that outlives it, i.e. assignment "
        f"rather than a roll.")


def _ruleset_carries_roll(ruleset_id: Optional[int]) -> bool:
    """Does this OPEN_POSITIONS ruleset contain the action that owns the roll?

    ``None`` (no ruleset configured at all) is False, not an error: the question asked is
    "will anything roll this?", and no ruleset is the loudest possible no.
    """
    if ruleset_id is None:
        return False
    from ba2_common.core.db import ruleset_event_actions

    # The SAME ordered eager load ``TradeActionEvaluator`` walks, not a second query: the
    # question here is "is the rule the evaluator will walk present?", so reading a
    # different row set would be able to answer yes about rules the evaluator never sees.
    return any(rule_carries_action(getattr(rule, "actions", None), ROLL_SHORT_OWNER_ACTION)
               for rule in ruleset_event_actions(ruleset_id))


def _abort(result: LifecyclePassResult, reason: str) -> LifecyclePassResult:
    result.aborted = True
    result.abort_reason = reason
    logger.error(f"Option lifecycle pass aborted: {reason}")
    return result


# ---------------------------------------------------------------------------
# broker visibility
# ---------------------------------------------------------------------------
def _broker_can_answer(account, expert_instance_id: int,
                       result: LifecyclePassResult) -> bool:
    """True only when the broker DEMONSTRABLY answered about its positions.

    Three outcomes, kept apart on purpose: a list (however empty) is an answer; ``None`` is
    a failed fetch; an exception is a failed fetch. ``or []`` over this call is a recurring
    bug class here — it turns an outage into a confident "flat" and the book is then acted
    on as if every position had vanished.
    """
    try:
        positions = account.get_positions()
    except Exception as e:  # noqa: BLE001 — any broker failure is the same fact
        _abort(result, f"could not read the position book for expert {expert_instance_id} "
                       f"({e}) — refusing to manage a book this account cannot see")
        return False
    if positions is None:
        _abort(result, f"the position book for expert {expert_instance_id} came back None: "
                       f"the fetch FAILED, which is not the same as flat — refusing to "
                       f"manage a book this account cannot see")
        return False
    return True


#: THE SHARED READER of the sleeve's equity, exposed here under its old name because this
#: module documented it (2026-09-01). It used to be a SECOND implementation of
#: ``account.get_balance()`` — which is EQUITY on Alpaca and spendable CASH on
#: ``BacktestAccount``, the divergence the breaker-parity work exists to remove. There is now
#: one definition, ``AccountSnapshot.equity``, read by both runtimes through
#: ``OptionRiskManagement.sleeve_equity``.
#:
#: THIS IS THE SIZING READER, not the breaker's. ``update_sleeve_breaker`` is what this pass
#: calls, and since the 2026-09-01 review it reads ``sleeve_true_equity`` -- the account's
#: UNCAPPED equity, because ``BacktestAccount``'s cap compresses peaks without compressing
#: troughs and would hide the drawdown from the one rail whose job is to catch it. Live the
#: two readers return the same number (there is no cap to look past), so nothing about this
#: pass's behaviour changed. ``None`` from either leaves the breaker blind (it says so)
#: rather than reporting a 100% drawdown it never measured.
_sleeve_equity = _rm.sleeve_equity


# ---------------------------------------------------------------------------
# building the structures
# ---------------------------------------------------------------------------
def _open_option_transactions(expert_instance_id: int) -> List[Any]:
    """This expert's OPENED transactions whose intent is OPTION.

    Delegates to ``OptionRiskManagement.open_option_transactions``: the entry gate and this
    exit pass must total the SAME sleeve, and two readers of "what is open" is how two
    answers to "how much is deployed" come about. See that module for why OPENED (not
    WAITING) is the right set and how the dual-path accessor serves both runtimes.
    """
    return _rm.open_option_transactions(expert_instance_id)


def _executed_option_orders(transaction_id: int) -> List[Any]:
    return _rm.executed_option_orders(transaction_id)


def _build_structure(txn, result: LifecyclePassResult) -> Optional[OptionStructure]:
    """One ``OptionStructure`` from one transaction's order rows, or ``None``.

    The netting, the expiry reconciliation and the percent basis all live in
    ``OptionRiskManagement.build_structure`` — promoted there so the entry rails and this
    exit pass read one book. What stays here is this pass's own REPORTING: a transaction
    with no executed option leg is recorded in ``result.unbuildable`` and logged, because a
    position the ledger cannot describe is a fact worth seeing rather than a silent skip.
    """
    structure = _rm.build_structure(txn)
    if structure is None:
        result.unbuildable.append(txn.id)
        logger.warning(
            f"Option lifecycle: transaction {txn.id} ({txn.symbol}) is an OPENED option "
            f"position with no executed option order rows — it cannot be netted, priced or "
            f"closed by this pass. It is NOT being managed.")
        return None
    if structure.entry_net_premium is None:
        logger.warning(
            f"Option lifecycle: transaction {txn.id} ({txn.symbol}) has no usable entry "
            f"basis (an unrecorded open price, or an executed leg with no fill price) — "
            f"its P&L percent is UNMEASURABLE, not zero")
    return structure


def _cover_required(structure: OptionStructure) -> Optional[int]:
    """Shares this structure's SHORT CALLS can have called away, or ``None``.

    Derived from the NETTED legs the pass has already built rather than from the
    transaction's contract count, because the two disagree in the case that matters: a
    covered call whose short call has been bought back holds nothing that can be called
    away, and charging it for cover it no longer needs would report a flat structure as
    naked every time the shares were later sold.

    ``None`` is UNMEASURABLE — a short leg whose ``option_type`` was never recorded might
    BE the call, and a structure with no usable multiplier has an obligation of unknown
    size. Neither may quietly become "needs nothing". ``0`` is a real answer: no short
    call is held, so no share count can make this structure naked.

    Rounded UP, like ``shares_pledged_to_short_calls`` and the entry guard: under-stating
    an obligation by a share is the direction that leaves a contract uncovered.
    """
    from ba2_common.core.types import OptionRight

    multiplier = structure.multiplier
    if not multiplier or multiplier <= 0:
        return None
    contracts = 0.0
    for leg in structure.held_legs:
        if not leg.is_short:
            continue                     # a LONG leg calls nothing away
        if leg.option_type is None:
            return None                  # it might be the call
        if leg.option_type != OptionRight.CALL:
            continue                     # a short put obliges CASH, not shares
        contracts += abs(leg.net_qty)
    return int(math.ceil(round(contracts * multiplier, 6)))


def _held_cover(account, underlying: str, cache: Dict[str, Optional[int]],
                expert_instance_id: int) -> Optional[int]:
    """``held_shares_for_cover``, asked ONCE per ticker per pass. Tri-state, ``None`` = unknown.

    Cached because three covered calls on one ticker are one question, and the accessor
    re-reads ``get_positions()`` each time it is asked. It is still the accessor that is
    asked, rather than the list ``_broker_can_answer`` already fetched: that method owns
    the option-row skip, the short-side sign and the tri-state contract, and a second
    implementation of those here is exactly how two views of one book come to disagree.
    """
    if underlying in cache:
        return cache[underlying]
    try:
        held = account.held_shares_for_cover(underlying)
    except Exception as e:  # noqa: BLE001 — same treatment as `_sleeve_equity`
        # UNMEASURABLE, and deliberately not fatal: the rest of the sleeve still gets
        # managed, and an unmeasured cover closes nothing (see `_cover_lost`).
        logger.error(f"Option lifecycle: how many {underlying} shares expert "
                     f"{expert_instance_id} holds as cover could not be read ({e}) — the "
                     f"cover for its {underlying} covered call(s) is UNKNOWN this pass",
                     exc_info=True)
        held = None
    cache[underlying] = held
    return held


# ``COVER_REQUIREMENT_UNMEASURABLE`` is IMPORTED at the top of this module, not defined
# here. It belongs to ``decide``'s input contract and now lives in the pure module beside
# ``CoverInput`` (which admits it) — read its comment there for what the value means. It
# used to be defined at this spot, so the value one side sent and the shape the other side
# accepted were two independent facts that happened to line up; a sentinel like that drifts
# the first time anyone edits either end.


def _cover_inputs(account, structures: Sequence[OptionStructure], expert_instance_id: int,
                  result: LifecyclePassResult) -> Tuple[Dict[int, Any],
                                                        Dict[int, Optional[int]]]:
    """``({txn: shares required}, {txn: shares available})`` for this sleeve's covered calls.

    Only ``covered_call`` transactions appear in either map; ``decide`` does not evaluate
    the cover rule for a transaction the maps omit, so every other structure is untouched.

    Shares are allocated OLDEST TRANSACTION FIRST. The account holds one pool per ticker
    and two covered calls on that ticker are two claims on it, so comparing each against
    the raw holding would report 200 shares as covering 200 shares twice over — and the
    multi-lot case ("30 shares held as 20 + 10" is modelled explicitly elsewhere in this
    codebase) is precisely how half a cover disappears overnight. First written, first
    covered.

    AN UNSIZEABLE CLAIM IS CHARGED TO THE POOL, and the only honest way to charge an
    unknown amount is to make the REMAINDER unknown: once one covered call on a ticker
    cannot say how many shares it could have called away, every YOUNGER covered call on
    that ticker is handed ``None`` instead of a figure. Skipping it — which is what used
    to happen — measured the next call against the WHOLE pool while the unsizeable one
    was quietly eating the same shares, and reported it comfortably covered. That is
    fail-OPEN, the one direction this file must never fail in.

    It cannot cause a spurious liquidation, which is what makes the choice safe as well
    as honest: ``None`` available is UNMEASURABLE and ``_cover_lost`` never fires on it
    (it raises ``LIFECYCLE_UNKNOWN``). All that is lost is a "covered" reading nobody
    could justify. Only YOUNGER calls are affected, because allocation is oldest-first:
    an older call's claim on the pool was already settled before the unsizeable one was
    reached.

    THE CROSS-EXPERT GAP IS STILL OPEN and is recorded rather than papered over: this
    pool is walked per SLEEVE, so two experts writing covered calls on the same ticker
    each see the whole holding. Closing that means allocating cover account-wide, which
    is a different pass from this one.

    Nothing here refuses, closes or aborts: it MEASURES. Every unmeasurable answer is
    logged and recorded in ``result.cover_unmeasurable`` and then handed to ``decide`` as
    ``None``, which produces ``LIFECYCLE_UNKNOWN`` and closes nothing.
    """
    required_by_txn: Dict[int, Any] = {}
    available_by_txn: Dict[int, Optional[int]] = {}
    held_cache: Dict[str, Optional[int]] = {}
    claimed: Dict[str, int] = {}
    # Tickers where some covered call's requirement could not be sized, so the FREE part
    # of the pool is no longer a number anyone can state (see the docstring).
    unsizeable_claim: set = set()

    for structure in sorted(structures, key=lambda s: s.transaction_id):
        if (structure.strategy or "").strip().lower() != COVERED_CALL_STRATEGY:
            continue
        txn_id = structure.transaction_id
        underlying = (structure.underlying or "").strip().upper()
        required = _cover_required(structure)

        if not underlying or required is None:
            # An obligation we cannot size, or one with no ticker whose shares could
            # cover it. Reported, and passed on as unknown — never as "needs nothing".
            result.cover_unmeasurable.append(txn_id)
            logger.error(
                f"Option lifecycle: transaction {txn_id} is tagged covered_call but how "
                f"many shares it could have called away is UNKNOWN "
                f"(underlying={structure.underlying!r}, multiplier={structure.multiplier!r}, "
                f"legs={[l.contract_symbol for l in structure.held_legs]}) — its cover "
                f"cannot be checked, so NOTHING is being closed on account of it. This is "
                f"NOT 'the cover is fine': the short call may be naked and this pass "
                f"cannot tell. Repair the leg's option_type / the transaction's "
                f"multiplier.")
            # The sentinel, not a number and not None: None would mean "this caller is
            # not measuring cover here" and would skip the rule silently, while any
            # figure would be a fabrication. `decide` reads it as UNREADABLE and says so.
            # It is `decide`'s own constant (see ba2_common.core.option_lifecycle), not a
            # string invented here, so the two ends cannot drift.
            required_by_txn[txn_id] = COVER_REQUIREMENT_UNMEASURABLE
            available_by_txn[txn_id] = None
            # CHARGED to the pool, as far as an unknown amount can be: this claim takes
            # an unknown number of the ticker's shares, so what is LEFT for a younger
            # covered call is unknown too (see the docstring).
            if underlying:
                unsizeable_claim.add(underlying)
            continue

        required_by_txn[txn_id] = required
        if required <= 0:
            continue                           # nothing to cover; `decide` skips the rule

        if underlying in unsizeable_claim:
            result.cover_unmeasurable.append(txn_id)
            logger.error(
                f"Option lifecycle: transaction {txn_id} is a covered_call needing "
                f"{required} {underlying} share(s) of cover, but an OLDER covered_call on "
                f"the same ticker has an obligation of UNKNOWN size (logged above), so how "
                f"much of the {underlying} pool is LEFT for this one cannot be stated. "
                f"Reported as UNKNOWN rather than measured against the whole holding — the "
                f"older claim is eating the same shares, and calling this one covered would "
                f"be counting them twice. NOTHING is being closed on account of it. Repair "
                f"the older structure's leg option_type / multiplier and this clears "
                f"itself.")
            available_by_txn[txn_id] = None
            continue

        held = _held_cover(account, underlying, held_cache, expert_instance_id)
        if held is None:
            result.cover_unmeasurable.append(txn_id)
            logger.error(
                f"Option lifecycle: transaction {txn_id} is a covered_call needing "
                f"{required} {underlying} share(s) of cover, but how many this account "
                f"holds is UNKNOWN — the POSITION FEED did not answer (the account logged "
                f"which fetch or row failed). NOTHING is being closed on the strength of "
                f"it: an unmeasured cover is not a lost one, and liquidating a healthy "
                f"structure over a feed hiccup is a self-inflicted loss. Equally, this is "
                f"NOT 'the cover is fine' — the short call may be NAKED and this pass "
                f"cannot tell. Repair the position feed.")
            available_by_txn[txn_id] = None
            continue

        # Clamped at 0: an over-claimed pool leaves later structures nothing, not a
        # negative holding that would read as a debt in the decision's detail.
        available_by_txn[txn_id] = max(0, held - claimed.get(underlying, 0))
        claimed[underlying] = claimed.get(underlying, 0) + required

    return required_by_txn, available_by_txn


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------
def _fetch_chain(account,
                 structures: Sequence[OptionStructure]) -> Dict[str, OptionContract]:
    """``{contract symbol: OptionContract}`` for every HELD leg, from the option chain.

    One request per (underlying, expiry) rather than one per leg: a four-leg condor is one
    call, and two structures on one expiry share it. The window is the leg's own expiry when
    it has one and the structure's otherwise — a leg with neither cannot be located in any
    chain, so it is simply absent from the map.

    Quotes are NOT used as a fallback. ``get_option_quote`` carries no greeks on the live
    broker, and ``_tested`` needs a delta; a map filled from quotes would answer "no delta"
    for every short and turn the tested-delta defence into a permanent blind spot that
    looked like a working feature. A symbol that is absent here is *unknown*, and
    ``option_lifecycle`` says so by name.
    """
    wanted: Dict[Tuple[str, date], Set[str]] = {}
    for structure in structures:
        for leg in structure.held_legs:
            expiry = leg.expiry or structure.expiry
            underlying = leg.underlying or structure.underlying
            if expiry is None or not underlying:
                logger.warning(
                    f"Option lifecycle: leg {leg.contract_symbol} on transaction "
                    f"{structure.transaction_id} has no expiry/underlying on record — no "
                    f"chain can be requested for it, so its quote and greeks are UNKNOWN")
                continue
            wanted.setdefault((underlying, expiry), set()).add(leg.contract_symbol)

    out: Dict[str, OptionContract] = {}
    for (underlying, expiry), symbols in sorted(wanted.items()):
        try:
            rows = account.get_option_chain(underlying, expiry, expiry)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Option lifecycle: option chain for {underlying} {expiry} could "
                         f"not be fetched ({e}) — {len(symbols)} leg(s) are UNKNOWN this "
                         f"pass, not zero", exc_info=True)
            continue
        if rows is None:
            logger.error(f"Option lifecycle: option chain for {underlying} {expiry} came "
                         f"back None — {len(symbols)} leg(s) are UNKNOWN this pass, which "
                         f"is not the same as unquoted")
            continue
        for row in rows:
            if row.symbol in symbols:
                out[row.symbol] = row
        for missing in sorted(symbols - set(out)):
            logger.warning(f"Option lifecycle: {missing} is held but has no row in the "
                           f"{underlying} {expiry} chain — its quote and greeks are UNKNOWN")
    return out


# ---------------------------------------------------------------------------
# closing
# ---------------------------------------------------------------------------
def _close(account, txn, structure: OptionStructure, decision: LifecycleDecision,
           result: LifecyclePassResult) -> None:
    """Flatten one structure — guarded, offsetting exactly what is still held.

    ``has_pending_closing_order`` is checked HERE, immediately before submitting, and not
    while loading the book: the pass may have submitted a close for this very transaction
    moments ago on a decision it already made, and the guard has to see it. Without the
    guard the pass re-submits a close on every cycle the first one takes to fill, each
    crediting cash for contracts that may already be gone — the 2026-07-21 options-grid
    trillion-scale equity runaway.

    A MARKET order, matching the promoted ``_close_structure``. A resting limit that does
    not fill is worse than the spread it saves: the pending-close guard then blocks this
    structure from being managed at all until the next pass, and the pass is daily. An exit
    the risk rules have decided on must actually execute.
    """
    try:
        if account.has_pending_closing_order(txn.id):
            result.skipped_pending_close.append(txn.id)
            logger.info(f"Option lifecycle: transaction {txn.id} decided "
                        f"{decision.reason} ({decision.detail}) but a closing order is "
                        f"already working — not submitting a second one")
            return

        legs = _closing_legs(structure)
        if legs is None:
            result.failed.append(txn.id)
            return
        if not legs:
            logger.info(f"Option lifecycle: transaction {txn.id} decided "
                        f"{decision.reason} but every leg has already netted flat — "
                        f"nothing left to close")
            return

        order = account.submit_option_order(
            legs=list(legs), quantity=1, order_type="market",
            option_strategy="close", transaction_id=txn.id)
        if order is None:
            result.failed.append(txn.id)
            logger.error(
                f"Option lifecycle: the close for transaction {txn.id} ({decision.reason}: "
                f"{decision.detail}) was NOT accepted by the broker — the position is still "
                f"open and unmanaged for this pass")
            return
        result.submitted.append(SubmittedClose(txn.id, decision.reason, legs, order))
        logger.info(f"Option lifecycle: submitted a close for transaction {txn.id} "
                    f"({txn.symbol}) — {decision.reason}: {decision.detail}")
    except Exception as e:  # noqa: BLE001
        # One structure's failure must not silence the rest of the book. Every other
        # structure in this sleeve still gets its decision acted on.
        result.failed.append(txn.id)
        logger.error(f"Option lifecycle: closing transaction {txn.id} failed ({e}) — the "
                     f"position is still open; the rest of the sleeve is unaffected",
                     exc_info=True)


def _closing_legs(structure: OptionStructure) -> Optional[Tuple[OptionLeg, ...]]:
    """Offsetting legs for everything still held, or ``None`` if we must not submit.

    Each held contract is reversed at its netted size: a net-long leg is SOLD to close, a
    net-short leg is BOUGHT to close. ``quantity=1`` on the order with ``ratio_qty`` per leg
    is how ``submit_option_order`` sizes children (``quantity * ratio_qty``), so an
    unbalanced structure — one wing partially bought back — closes at its real remaining
    sizes rather than at the parent's original ratio.

    A net size that is not a whole number of contracts refuses the WHOLE close and returns
    ``None``. Truncating it (``int(abs(n))``) would silently submit a partial flatten and
    leave residual risk nobody asked for; options are integral, so this is a ledger defect
    to surface, not a rounding to perform.
    """
    from ba2_common.core.types import OrderDirection

    legs: List[OptionLeg] = []
    for leg in structure.held_legs:                      # contract-symbol ordered
        contracts = abs(leg.net_qty)
        rounded = int(round(contracts))
        if abs(contracts - rounded) > 1e-6 or rounded < 1:
            logger.error(
                f"Option lifecycle: transaction {structure.transaction_id} holds "
                f"{leg.net_qty} of {leg.contract_symbol}, which is not a whole number of "
                f"contracts — refusing to submit a close that would flatten only part of "
                f"the structure")
            return None
        long_leg = leg.net_qty > 0
        legs.append(OptionLeg(
            contract_symbol=leg.contract_symbol,
            side=OrderDirection.SELL if long_leg else OrderDirection.BUY,
            ratio_qty=rounded,
            position_intent="sell_to_close" if long_leg else "buy_to_close",
            option_type=leg.option_type,
            strike=leg.strike,
            expiry=leg.expiry,
            underlying=leg.underlying,
        ))
    return tuple(legs)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def _lifecycle_settings(expert) -> Tuple[Dict[str, Any], List[str]]:
    """(the sleeve's thresholds, the REQUIRED ones the expert does not declare).

    Read through ``get_setting_with_interface_default`` so a configured value wins and a
    declared default is honoured, exactly as every other expert setting is read. A key the
    expert's interface does not define at all raises ``ValueError`` there; here that means
    "this expert has no such threshold", which is reported by name and never filled in.
    """
    settings: Dict[str, Any] = {}
    missing: List[str] = []
    for key in REQUIRED_SETTINGS + OPTIONAL_SETTINGS:
        try:
            value = expert.get_setting_with_interface_default(key, log_warning=False)
        except Exception:  # noqa: BLE001 — ValueError today; any lookup failure is "absent"
            value = None
            if key in REQUIRED_SETTINGS:
                missing.append(key)
            continue
        if value is None:
            if key in REQUIRED_SETTINGS:
                missing.append(key)
            continue
        settings[key] = value
    return settings, missing
