"""The PURE book layer: sleeve totals, entry rails and the drawdown circuit breaker.

Promoted out of ``OptionPortfolioManager._book_totals`` / ``_within_rails`` and the
breaker in ``manage_open`` (``ba2_experts.PremiumSeller.portfolio``) so that the live
pass and the backtest engine run **one** implementation of each rail.

These are the four things **no rule can express**. ``TradeActionEvaluator.evaluate(
instrument_name, expert_recommendation, ...)`` is per-instrument by signature, and a
rail is a statement about the whole sleeve: how much of it is deployed, how levered it
is, how much of it is naked, how many structures it holds. A per-instrument rule can
never see the other instrument.

Pure means pure: **no DB, no broker, no clock.** Equity, the held structures and the
settings all arrive as arguments; ``test_option_book_rails.py`` enforces that with an
import-leak gate.


Scope: a sleeve, not an account
-------------------------------
Every rail here is **per-expert sleeve**, which is ``PremiumSeller``'s original
semantics: the structures are one expert's holdings and ``equity`` is the balance that
expert sizes against. An **account-wide** cap across several option experts sharing one
broker account is a different feature and is explicitly **out of scope** — nothing in
this module coordinates two sleeves, and two sleeves at 40% deployment each will happily
put 80% of one account to work. Do not read a sleeve rail as an account guarantee.


Unknown is never a value
------------------------
Same rule as ``option_lifecycle``, and for the same reason. Every place the promoted
code could not measure something it produced a number that read as safe:

* ``_within_rails`` was the one place that got this right — ``equity is None`` declined
  rather than fabricating — and that behaviour is kept verbatim.
* ``_book_totals`` summed ``_txn_metrics``, which returned ``(True, 0.0, 0.0)`` whenever
  it saw no short legs. Before the leg reconciliation landed, *no* live multi-leg had
  visible legs, so an entire book totalled zero and every rail was unreachable.
* A structure we cannot total does not make the sleeve smaller; it makes the sleeve's
  size **unknown**. ``BookTotals`` therefore reports ``None`` for every money field and
  names the structures it could not measure, and the rails decline against it.

``structure_metrics`` (``option_lifecycle``) is the single source of committed capital
and notional. It is not re-derived here: its two corrections to ``_txn_metrics`` — that
netting is real, and that a call vertical's long sits *above* its short — are load
bearing for these rails, and a second copy of that arithmetic is exactly how the
short-sign divergence happened.


The debit book
--------------
One deliberate extension beyond a faithful promotion, because the option grid now has a
BUY-ONLY (net-debit) arm alongside the SELL-ONLY one.

``_txn_metrics`` bucketed by *order side* and returned ``(True, 0.0, 0.0)`` when a
transaction had no executed SELL leg (``portfolio.py:92-93``), so a pure-debit structure
reported zero deployment and zero notional and **the rails never engaged at all**. That
is not a conservative approximation; it is a rail that does not exist.

The fix is additive and it is deliberately narrow:

* a structure with **no held short leg** commits its **premium outlay** — what it paid,
  which for a long-only structure is also its maximum loss;
* a structure with **any held short leg** is measured exactly as before, on the short
  side. Its debit is not added on top, because ``structure_metrics`` already charges it
  the wing width, and charging both would double-count.

So ``committed`` = short-side committed **+** debit premium outlay, and for any
structure carrying a held short the outlay term is exactly ``0.0``. Every credit number
is therefore unchanged — pinned by
``test_the_debit_extension_changes_no_credit_structure_number`` over six credit shapes.

``notional`` stays a **short-side** measure and the leverage rail stays a short-side
rail. That is a deliberate split rather than an oversight: ``max_notional_leverage``
caps assignment exposure, and a long option cannot be assigned against you — its loss
is bounded by the debit, which the deployment rail now counts. The two rails cannot
share one number honestly, so they do not. What must never happen is a rail that is
silently inert, which is why ``RailVerdict.evaluated`` reports which rails actually ran:
``undefined_risk_max_pct`` is genuinely dead for a debit arm (a long option has no
undefined risk) and that is a readable fact rather than a silence.


The circuit breaker feeds the lifecycle; it does not re-implement the flatten
----------------------------------------------------------------------------
``option_lifecycle`` already produces ``LIFECYCLE_BREAKER`` from an optional
``circuit_breaker_tripped`` state signal. This module owns the *state*: peak-equity
ratcheting, the peak-to-trough test, and the latch. ``breaker_signal(state)`` produces
exactly the mapping ``decide()`` reads, keyed on ``option_lifecycle``'s own constant so
the two can never drift.

The latch is promoted behaviour. ``manage_open`` sets ``self._halted = True`` on the bar
it flattens and then returns ``[]`` on every later bar — no exits at all while standing
down. ``tripped`` is therefore the *edge* (flatten now) and ``halted`` is the *state*
(standing down). Signalling ``tripped`` every bar would re-issue closes against a book
that is already flat.

The stand-down gates ENTRY as well, and that is not promoted — it is the promotion of
what the promoted code *said* it did. ``manage_open``'s latch suppressed exits only;
``rebalance`` was never gated on it, so a flattened sleeve re-opened the whole book on
its next entry bar, at the bottom of the drawdown that had just flattened it, and then
flattened again. ``check_rails`` declines every candidate while ``halted``, ahead of
every rail.

Gating entry forces the question of what clears the latch, because the promoted answer
("a new entry cycle") becomes unreachable the moment entry is blocked. The answer is a
**recovery**: ``update_breaker`` lifts the stand-down once equity climbs back to within
``BREAKER_REARM_DEPTH_FRACTION`` of the trip depth. It is the only condition under which
resuming does not immediately re-trip — the peak is kept, so a sleeve re-armed on a
timer or a bar count is flattened again on its first managed bar. ``rearm()`` survives
as the unconditional operator override, and is the reason "halted forever" is never a
terminal state.

One promoted quirk is deliberately NOT reproduced, and the caller must not reintroduce
it: ``manage_open`` returned early on ``if not holdings`` **before** ratcheting the
peak, so a sleeve that had just been flattened stopped tracking its peak entirely and
would re-arm against a stale one. ``update_breaker`` here always ratchets, and it is the
caller's job to call it every evaluation — including when the sleeve is flat.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from ba2_common.core.option_lifecycle import (
    SETTING_BREAKER_TRIPPED,
    UNDEFINED_RISK_STRATEGIES,
    OptionStructure,
    structure_metrics,
)

#: Nothing declined.
RAIL_OK = "ok"
#: Declines that are "we cannot measure this", not "this breaches a limit".
RAIL_UNKNOWN_EQUITY = "unknown_equity"
RAIL_UNMEASURABLE_BOOK = "unmeasurable_book"
RAIL_UNMEASURABLE_CANDIDATE = "unmeasurable_candidate"
#: The decline that is a STATE: the drawdown breaker has stood this sleeve down.
RAIL_BREAKER_HALTED = "circuit_breaker_halted"
#: Declines that are a configured limit doing its job.
RAIL_MAX_CONCURRENT = "max_concurrent_structures"
RAIL_ONE_PER_UNDERLYING = "one_per_underlying"
RAIL_MAX_DEPLOYMENT = "max_deployment_pct"
RAIL_MAX_NOTIONAL_LEVERAGE = "max_notional_leverage"
RAIL_UNDEFINED_RISK = "undefined_risk_max_pct"

#: The order rails are evaluated in, promoted from ``rebalance`` + ``_within_rails``:
#: the two caps first (they were the loop's own guards), then equity, then the three
#: percentage rails. Fixed, because the *reason* a candidate was declined is recorded
#: and a reordering would silently re-label history.
RAIL_ORDER = (RAIL_MAX_CONCURRENT, RAIL_ONE_PER_UNDERLYING, RAIL_MAX_DEPLOYMENT,
              RAIL_MAX_NOTIONAL_LEVERAGE, RAIL_UNDEFINED_RISK)

#: How far a sleeve must climb back out of a drawdown before its stand-down lifts,
#: as a fraction of the trip depth. 0.5 with a 20% breaker means: trip at -20%,
#: re-arm at -10%.
#:
#: It is a derived constant rather than a setting because the one thing it must never
#: be is *equal to* the trip depth. Re-arming exactly on the trip line leaves no
#: hysteresis at all: a sleeve sitting on the boundary lifts its stand-down, opens,
#: and is flattened again by the next tick down — the same open/flatten cycle the
#: stand-down exists to stop, one basis point wide. Halving the depth is the smallest
#: buffer that is unambiguously a *recovery* and not rounding, and it moves in the
#: safe direction: a shallower re-arm line is harder to reach than the trip line.
BREAKER_REARM_DEPTH_FRACTION = 0.5

_EPS = 1e-9


# ---------------------------------------------------------------------------
# settings access -- explicit, no silent defaults
# ---------------------------------------------------------------------------
def _require(settings: Mapping[str, Any], key: str) -> Any:
    """Read a configured rail. A missing one is loud, never a default."""
    if key not in settings:
        raise KeyError(
            f"option_book: required setting {key!r} is missing — refusing to "
            f"substitute a default for a risk rail")
    return settings[key]


# ---------------------------------------------------------------------------
# inputs / outputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CandidateStructure:
    """ONE structure the sleeve is considering opening — ``StructureSpec``, as a value.

    ``max_loss`` is the total dollars of capital the structure puts at risk if it fills:
    for a credit structure the promoted ``spec.max_loss``, for a debit structure the
    premium paid. ``notional`` is the total **short-side** dollars (short strike x 100 x
    qty) and is genuinely ``0.0`` for a long-only structure.

    Both are ``Optional`` and ``None`` means *unknown*, which declines. They are never
    defaulted to zero: a candidate that risks nothing and controls nothing is not a free
    trade, it is a spec nobody measured.

    ``is_defined_risk`` is optional and means "declared". ``None`` = not declared, and
    the undefined-risk rail then falls back to the promoted strategy-name gate.
    """
    underlying: str
    strategy: str
    max_loss: Optional[float]
    notional: Optional[float]
    is_defined_risk: Optional[bool] = None


@dataclass(frozen=True)
class BookTotals:
    """``_book_totals``, promoted: what the sleeve currently has at work.

    Every money field is ``None`` when *any* structure could not be measured — a sum
    with a missing addend is an unknown sum, not a smaller one — and ``unmeasurable``
    then names each structure and why.

    ``committed``      total capital at risk: short-side committed + debit outlay.
    ``naked_committed`` the undefined-risk share of ``committed``.
    ``notional``       short-side stress notional only; the leverage rail's basis.
    ``premium_outlay`` the debit share of ``committed`` (long-only structures).

    ``structure_count`` and ``underlyings`` are never unknown: they come from the
    caller's own list of open structures, exactly as ``len(holdings)`` and
    ``{txn.symbol}`` did, so the two caps keep working even when the money does not.
    A structure whose legs have all netted flat still occupies its slot and its
    underlying — its transaction is OPENED until the netting resolves it — while
    contributing zero capital.
    """
    committed: Optional[float]
    naked_committed: Optional[float]
    notional: Optional[float]
    premium_outlay: Optional[float]
    structure_count: int
    underlyings: FrozenSet[str]
    unmeasurable: Tuple[str, ...] = ()

    @property
    def is_measurable(self) -> bool:
        return self.committed is not None


@dataclass(frozen=True)
class RailVerdict:
    """Whether ONE candidate may be opened, and which rail had the last word.

    ``evaluated`` lists the rails that actually ran, in ``RAIL_ORDER``. A rail that did
    not run is a fact you can assert on rather than a silence — ``undefined_risk_max_pct``
    is inert for a debit arm by design, and an inert rail nobody can see is how a gene
    that could not fire survived a whole GA campaign.

    ``book_after`` is the running sleeve *including* this candidate when it was admitted,
    and unchanged when it was not.
    """
    allowed: bool
    reason: str                       # RAIL_OK, or the rail that declined
    detail: str
    candidate: CandidateStructure
    evaluated: Tuple[str, ...]
    book_after: BookTotals


@dataclass(frozen=True)
class BreakerState:
    """The sleeve drawdown breaker, as a value. Carry it across evaluations.

    ``tripped`` is the EDGE: this evaluation is the one that flattens the book.
    ``halted`` is the STATE: the sleeve is standing down. It stays flat AND opens
    nothing (``check_rails`` declines every candidate) until the drawdown recovers past
    the re-arm line, which ``update_breaker`` tests on every evaluation, or until an
    operator calls ``rearm()``. ``blind`` means the breaker could not be evaluated at
    all — an equity we could not read, or a peak that is not a positive number — which
    is distinct from a measured "not tripped".
    """
    peak_equity: Optional[float] = None
    halted: bool = False
    tripped: bool = False
    detail: str = ""
    blind: bool = False


# ---------------------------------------------------------------------------
# book totals (promoted _book_totals)
# ---------------------------------------------------------------------------
def _premium_outlay(structure: OptionStructure) -> Tuple[Optional[float], str]:
    """(dollars this structure paid out, "") or (None, why it is unmeasurable).

    Only a structure with **no held short leg** has an outlay to charge: anything with a
    short is governed by ``structure_metrics``' short-side committed capital, and adding
    a debit on top of a wing width double-counts the same dollars.

    An all-long structure whose entry premium is a CREDIT owes nothing — that is a credit
    spread whose short wing has been bought back, leaving the long outstanding, and it is
    a common shape rather than a contradiction. An entry premium we never recorded, by
    contrast, is unknown: on a debit arm that is the whole exposure.
    """
    held = structure.held_legs
    if not held:
        return 0.0, ""
    if any(leg.is_short for leg in held):
        return 0.0, ""

    premium = structure.entry_net_premium
    if premium is None:
        return None, ("entry net premium is unknown on a long-only structure — its "
                      "premium outlay, which is its whole exposure, is unmeasurable")
    if premium <= 0:
        # Opened for a credit; nothing was paid out. Genuinely zero, not a stand-in.
        return 0.0, ""
    if structure.quantity is None or abs(structure.quantity) < _EPS:
        return None, ("structure quantity is 0 on a long-only structure — its premium "
                      "outlay is unmeasurable")
    if not structure.multiplier:
        return None, ("no contract multiplier on a long-only structure — its premium "
                      "outlay is unmeasurable")
    return premium * abs(structure.quantity) * structure.multiplier, ""


def _structure_totals(structure: OptionStructure
                      ) -> Tuple[Optional[float], Optional[float], Optional[float],
                                 Optional[float], str]:
    """(committed, naked_committed, notional, premium_outlay, why-unmeasurable)."""
    # Legs recorded, none of them still held: we SAW this structure go flat. That is a
    # measured zero, and it is a different fact from a structure whose legs we never saw
    # -- which structure_metrics rightly calls unmeasurable.
    if structure.legs and not structure.held_legs:
        return 0.0, 0.0, 0.0, 0.0, ""

    metrics = structure_metrics(structure)
    if metrics.committed is None or metrics.notional is None:
        return None, None, None, None, metrics.detail

    outlay, blind = _premium_outlay(structure)
    if outlay is None:
        return None, None, None, None, blind

    committed = metrics.committed + outlay
    naked = 0.0 if metrics.is_defined_risk else committed
    return committed, naked, metrics.notional, outlay, ""


def book_totals(structures: Iterable[OptionStructure]) -> BookTotals:
    """Total ONE sleeve's held structures. Pure: values in, values out.

    :param structures: the expert's open option structures (see ``OptionStructure``).
                       What counts as open is the caller's decision, exactly as
                       ``get_option_holdings`` was.
    """
    ordered = sorted(structures, key=lambda s: s.transaction_id)
    committed = naked = notional = outlay = 0.0
    underlyings = set()
    blind: List[str] = []

    for structure in ordered:
        underlyings.add(structure.underlying)
        c, nk, n, o, why = _structure_totals(structure)
        if why:
            blind.append(f"transaction {structure.transaction_id}: {why}")
            continue
        committed += c
        naked += nk
        notional += n
        outlay += o

    count = len(ordered)
    if blind:
        return BookTotals(None, None, None, None, count, frozenset(underlyings),
                          tuple(blind))
    return BookTotals(committed, naked, notional, outlay, count,
                      frozenset(underlyings), ())


# ---------------------------------------------------------------------------
# rails (promoted _within_rails + rebalance's two loop guards)
# ---------------------------------------------------------------------------
def _is_undefined_risk(candidate: CandidateStructure) -> bool:
    """Does the naked cap apply to this candidate?

    The promoted gate is a hardcoded ``("short_put", "short_strangle")`` tuple —
    selection by *declared strategy*, not by the measured risk of the legs — so a naked
    structure under any other name skipped the rail entirely. The tuple is kept (it is
    correctly inert for a debit arm: a long option has no undefined risk), and a
    candidate that explicitly *measures* as undefined risk is charged to the cap
    whatever it calls itself. ``is_defined_risk=None`` means "not declared" and changes
    nothing, so no credit-arm number moves.
    """
    return (candidate.strategy in UNDEFINED_RISK_STRATEGIES
            or candidate.is_defined_risk is False)


def _charge(book: BookTotals, candidate: CandidateStructure) -> BookTotals:
    """The running book after admitting ``candidate`` (``rebalance``'s ``book[0] += ...``).

    A candidate with no short notional is a long-only structure, so its max loss IS the
    premium it will pay: it is charged to ``premium_outlay`` as well as to ``committed``,
    keeping the running book's ``committed = short-side + outlay`` identity intact.
    """
    is_debit = abs(candidate.notional) < _EPS
    return BookTotals(
        committed=book.committed + candidate.max_loss,
        naked_committed=book.naked_committed + (candidate.max_loss
                                                if _is_undefined_risk(candidate) else 0.0),
        notional=book.notional + candidate.notional,
        premium_outlay=book.premium_outlay + (candidate.max_loss if is_debit else 0.0),
        structure_count=book.structure_count + 1,
        underlyings=book.underlyings | {candidate.underlying},
        unmeasurable=book.unmeasurable,
    )


def check_rails(candidate: CandidateStructure,
                book: BookTotals,
                equity: Optional[float],
                settings: Mapping[str, Any],
                breaker: BreakerState) -> RailVerdict:
    """May this ONE candidate be opened against this sleeve? Pure.

    :param candidate: the structure being considered.
    :param book:      the sleeve as it stands (see ``book_totals``).
    :param equity:    the balance the sleeve sizes against. ``None`` DECLINES — that is
                      promoted behaviour and the one thing ``_within_rails`` already got
                      right.
    :param settings:  the expert's rails. Required: ``max_concurrent_structures``,
                      ``max_deployment_pct``, ``max_notional_leverage``; plus
                      ``undefined_risk_max_pct`` when the candidate is undefined risk.
                      A missing one raises rather than defaulting.
    :param breaker:   the sleeve's drawdown breaker (``update_breaker``). REQUIRED and
                      not defaulted: "the caller did not say" and "the sleeve is
                      trading" are different facts, and treating the first as the
                      second is exactly how the stand-down came to gate nothing. A
                      sleeve with no breaker passes ``BreakerState()`` and says so.
    """
    if not isinstance(breaker, BreakerState):
        raise TypeError(
            f"check_rails requires a BreakerState, got {type(breaker).__name__} — a "
            f"missing breaker is not an un-halted one. Pass BreakerState() if this "
            f"sleeve genuinely has no drawdown breaker.")

    ran: List[str] = []

    def verdict(reason: str, detail: str, allowed: bool = False,
                after: Optional[BookTotals] = None) -> RailVerdict:
        return RailVerdict(allowed, reason, detail, candidate, tuple(ran),
                           after if after is not None else book)

    # 0. the stand-down, ahead of every rail.
    #
    # This is a statement about the SLEEVE, not about the candidate, and it outranks
    # the caps for the same reason the breaker outranks every per-structure exit: the
    # book has just been flattened *because* the sleeve is in a drawdown it should not
    # be adding to. Nothing below is even consulted, so `evaluated` stays empty — a
    # halted sleeve was not "within its rails", it was never asked.
    #
    # Without this the latch suppressed exits only, and a flattened sleeve re-opened
    # the whole book on its next entry bar at the bottom of the drawdown that flattened
    # it, tripped again, flattened again, and paid the spread every round.
    if breaker.halted:
        return verdict(RAIL_BREAKER_HALTED,
                       "the sleeve is standing down after the circuit breaker — no new "
                       "structures until the drawdown recovers: " + (breaker.detail or ""))

    # 1. the concurrent-structure cap. `len(holdings) + len(submitted) >= max`.
    max_concurrent = int(_require(settings, "max_concurrent_structures"))
    ran.append(RAIL_MAX_CONCURRENT)
    if book.structure_count >= max_concurrent:
        return verdict(RAIL_MAX_CONCURRENT,
                       f"{book.structure_count} open structures >= "
                       f"max_concurrent_structures {max_concurrent}")

    # 2. one structure per underlying.
    ran.append(RAIL_ONE_PER_UNDERLYING)
    if candidate.underlying in book.underlyings:
        return verdict(RAIL_ONE_PER_UNDERLYING,
                       f"the sleeve already holds a structure on {candidate.underlying}")

    # 3. the balance. Unknown declines rather than fabricating -- kept verbatim.
    if equity is None:
        return verdict(RAIL_UNKNOWN_EQUITY,
                       "no account balance — declining new structure")
    if equity <= 0:
        return verdict(RAIL_UNKNOWN_EQUITY,
                       f"account balance {equity:g} is not positive — declining new "
                       f"structure")

    # 4. a sleeve we cannot total is not an empty sleeve.
    if not book.is_measurable:
        return verdict(RAIL_UNMEASURABLE_BOOK,
                       "the sleeve's committed capital is unmeasurable — declining new "
                       "structure: " + "; ".join(book.unmeasurable))

    # 5. a candidate we cannot size is not a free trade.
    if candidate.max_loss is None:
        return verdict(RAIL_UNMEASURABLE_CANDIDATE,
                       f"{candidate.strategy} on {candidate.underlying} has no max_loss "
                       f"— its capital at risk is unmeasurable")
    if candidate.notional is None:
        return verdict(RAIL_UNMEASURABLE_CANDIDATE,
                       f"{candidate.strategy} on {candidate.underlying} has no notional "
                       f"— its leverage is unmeasurable")
    if candidate.max_loss < 0 or candidate.notional < 0:
        return verdict(RAIL_UNMEASURABLE_CANDIDATE,
                       f"{candidate.strategy} on {candidate.underlying} reports a "
                       f"negative max_loss/notional ({candidate.max_loss:g}/"
                       f"{candidate.notional:g}) — a negative addend would buy room "
                       f"under the caps")
    if abs(candidate.max_loss) < _EPS and abs(candidate.notional) < _EPS:
        return verdict(RAIL_UNMEASURABLE_CANDIDATE,
                       f"{candidate.strategy} on {candidate.underlying} risks nothing "
                       f"and controls nothing — that is an unmeasured spec, not a free "
                       f"trade (it is exactly the (True, 0.0, 0.0) _txn_metrics returned "
                       f"for a structure it could not see)")

    # 6. max_deployment_pct -- total capital at risk.
    deployment_pct = float(_require(settings, "max_deployment_pct"))
    ran.append(RAIL_MAX_DEPLOYMENT)
    deployment_cap = deployment_pct / 100.0 * equity
    if book.committed + candidate.max_loss > deployment_cap:
        return verdict(RAIL_MAX_DEPLOYMENT,
                       f"committed {book.committed:.2f} + {candidate.max_loss:.2f} > "
                       f"max_deployment_pct {deployment_pct:g}% of {equity:.2f} "
                       f"({deployment_cap:.2f})")

    # 7. max_notional_leverage -- SHORT-side notional over equity. A long option carries
    #    none: it cannot be assigned against you, and its loss is the debit that rail 6
    #    already counted.
    leverage = float(_require(settings, "max_notional_leverage"))
    ran.append(RAIL_MAX_NOTIONAL_LEVERAGE)
    notional_cap = leverage * equity
    if book.notional + candidate.notional > notional_cap:
        return verdict(RAIL_MAX_NOTIONAL_LEVERAGE,
                       f"short notional {book.notional:.2f} + {candidate.notional:.2f} > "
                       f"max_notional_leverage {leverage:g}x {equity:.2f} "
                       f"({notional_cap:.2f})")

    # 8. undefined_risk_max_pct -- the naked sub-cap, only for undefined-risk candidates.
    if _is_undefined_risk(candidate):
        naked_pct = float(_require(settings, "undefined_risk_max_pct"))
        ran.append(RAIL_UNDEFINED_RISK)
        naked_cap = naked_pct / 100.0 * equity
        if book.naked_committed + candidate.max_loss > naked_cap:
            return verdict(RAIL_UNDEFINED_RISK,
                           f"naked committed {book.naked_committed:.2f} + "
                           f"{candidate.max_loss:.2f} > undefined_risk_max_pct "
                           f"{naked_pct:g}% of {equity:.2f} ({naked_cap:.2f})")

    return verdict(RAIL_OK,
                   f"{candidate.strategy} on {candidate.underlying} is within every "
                   f"sleeve rail", allowed=True, after=_charge(book, candidate))


def admit(candidates: Sequence[CandidateStructure],
          book: BookTotals,
          equity: Optional[float],
          settings: Mapping[str, Any],
          breaker: BreakerState) -> List[RailVerdict]:
    """Walk ``candidates`` in order, charging each admission to the running sleeve.

    This is ``rebalance``'s gate loop with the submission removed: three 20k candidates
    do not all fit under a 40k deployment cap just because each one fits on its own, and
    two candidates on one underlying do not both open. One verdict per candidate, in the
    order given, so the caller can log every decline with its reason.

    A declined candidate is skipped and costs the sleeve nothing — the loop continues,
    exactly as ``rebalance``'s ``continue`` did. The concurrent cap ends the sleeve's
    capacity for this pass rather than just this candidate (``rebalance`` used ``break``),
    but the effect is identical: the count only rises, so nothing after it could fit.

    ``breaker`` is required for the same reason it is on ``check_rails``: a standing-down
    sleeve declines every candidate in the pass, not just the first.
    """
    verdicts: List[RailVerdict] = []
    running = book
    for candidate in candidates:
        verdict = check_rails(candidate, running, equity, settings, breaker)
        verdicts.append(verdict)
        if verdict.allowed:
            running = verdict.book_after
    return verdicts


# ---------------------------------------------------------------------------
# the sleeve circuit breaker
# ---------------------------------------------------------------------------
def update_breaker(state: BreakerState,
                   equity: Optional[float],
                   settings: Mapping[str, Any]) -> BreakerState:
    """Ratchet the peak, test peak-to-trough drawdown, return the NEW state. Pure.

    :param state:    the breaker as it stood (``BreakerState()`` on the first bar).
    :param equity:   this evaluation's sleeve equity. ``None`` leaves the breaker blind:
                     it cannot trip and it says so, rather than reporting a confident
                     "not tripped" it never measured.
    :param settings: requires ``circuit_breaker_pct``. Missing raises.

    The comparison is the promoted one, ``equity <= peak x (1 - pct/100)``, so exactly
    -20% trips and the arithmetic cannot drift at the boundary.

    **What clears a stand-down: a recovery, and only a recovery.** Once ``halted``, the
    sleeve stays flat until equity climbs back to within ``BREAKER_REARM_DEPTH_FRACTION``
    of the trip depth (``-10%`` under a 20% breaker), at which point the latch lifts on
    its own — no entry required. Three candidate rules were rejected:

    * *the next entry cycle* (what the old docstring said) cannot work now that entry is
      gated on the latch: it would be a clear that only fires on an entry that only
      happens after the clear. That is the deadlock, and the module must not ship it.
    * *a bar/time cool-off* re-arms a sleeve that is still under water. The peak is
      deliberately KEPT across a re-arm, so such a sleeve trips again on its first
      managed bar: the same open/flatten cycle, merely slower.
    * *an operator reset alone* leaves the only exit outside the system. ``rearm()``
      remains exactly that — an unconditional override — but it is the escape hatch,
      not the mechanism.

    Recovery is the only condition under which resuming does not immediately re-trip,
    which is what makes it the right one.
    """
    pct = float(_require(settings, "circuit_breaker_pct"))

    peak = state.peak_equity
    if equity is not None:
        peak = equity if peak is None else max(peak, equity)

    if equity is None:
        return BreakerState(peak, state.halted, False,
                            "sleeve equity is unknown — the drawdown breaker could not "
                            "be evaluated this bar", blind=True)

    if state.halted:
        # `_halted` short-circuits manage_open, and it now also declines every entry
        # (check_rails). It lifts when the drawdown has healed past the re-arm line.
        rearm_pct = pct * BREAKER_REARM_DEPTH_FRACTION
        if peak is not None and peak > 0:
            drawdown = (peak - equity) / peak * 100.0
            if equity >= peak * (1.0 - rearm_pct / 100.0):
                return BreakerState(peak, False, False,
                                    f"sleeve drawdown {drawdown:.2f}% (peak {peak:.2f} "
                                    f"-> {equity:.2f}) has recovered past the re-arm "
                                    f"line {rearm_pct:g}% — the circuit-breaker "
                                    f"stand-down is cleared")
            return BreakerState(peak, True, False,
                                f"sleeve is standing down after the circuit breaker — "
                                f"drawdown {drawdown:.2f}% is still worse than the "
                                f"{rearm_pct:g}% re-arm line")
        # An unusable peak makes the recovery test undefined, and undefined must not
        # read as recovered. Note the ratchet above has already run, so a peak can only
        # still be non-positive here when equity is too.
        return BreakerState(peak, True, False,
                            f"sleeve is standing down after the circuit breaker — peak "
                            f"sleeve equity {peak!r} is not positive, so the recovery "
                            f"that would clear it cannot be measured", blind=True)

    if peak is None or peak <= 0:
        return BreakerState(peak, False, False,
                            f"peak sleeve equity {peak!r} is not positive — the "
                            f"peak-to-trough drawdown is undefined", blind=True)

    drawdown = (peak - equity) / peak * 100.0
    if equity <= peak * (1.0 - pct / 100.0):
        return BreakerState(peak, True, True,
                            f"sleeve drawdown {drawdown:.2f}% (peak {peak:.2f} -> "
                            f"{equity:.2f}) >= circuit_breaker_pct {pct:g}% — flattening "
                            f"the book")
    return BreakerState(peak, False, False,
                        f"sleeve drawdown {drawdown:.2f}% (peak {peak:.2f} -> "
                        f"{equity:.2f}) < circuit_breaker_pct {pct:g}%")


def rearm(state: BreakerState) -> BreakerState:
    """Clear a circuit-breaker stand-down UNCONDITIONALLY: the operator override.

    This is no longer "what an entry cycle does" — a stand-down now declines entry
    (``check_rails``) and lifts on its own once the drawdown recovers
    (``update_breaker``), so a clear driven by entry would be a clear that can never
    fire. What is left for this function is the deliberate override: the one way out
    that does not depend on the market, for a sleeve whose equity cannot move because
    it is flat and alone in its account. Calling it re-risks a sleeve that has not
    recovered, which is why nothing calls it automatically.

    The peak is deliberately KEPT. ``rebalance`` never touched ``_peak_equity``, and
    resetting it would erase the drawdown that caused the stand-down: the sleeve would
    re-arm at its trough and need another full -20% from there before it could ever
    trip again.
    """
    return BreakerState(state.peak_equity, False, False,
                        "circuit-breaker stand-down cleared by an explicit re-arm")


def breaker_signal(state: BreakerState) -> Dict[str, bool]:
    """The mapping ``option_lifecycle.decide()`` reads, ready to merge into its settings.

    Task 7 FEEDS ``LIFECYCLE_BREAKER``; it does not re-implement the flatten. The key is
    imported from ``option_lifecycle`` rather than spelled again here, so the producer
    and the consumer cannot drift apart.

    It carries the EDGE, not the latch: a sleeve already standing down has a flat book,
    and re-signalling every bar would re-issue closes forever.
    """
    return {SETTING_BREAKER_TRIPPED: bool(state.tripped)}
