"""The PURE option lifecycle decision function: positions + chain in, decisions out.

Promoted out of ``OptionPortfolioManager._should_close`` / ``_txn_metrics`` /
``_tested`` (``ba2_experts.PremiumSeller.portfolio``) so that the live pass and the
backtest engine run **one** implementation of each rule. Two implementations of one
rule is exactly how the short-sign divergence happened.

Pure means pure: **no DB, no broker, no clock.** Every input arrives as an argument —
including ``as_of``, so nothing here ever reads the wall clock — and the module
imports nothing but the value objects in ``option_types`` and the enums in ``types``.
``test_option_lifecycle.py`` enforces that with an import-leak gate.


Unknown is never a value
------------------------
The rule this module exists to enforce. Every place the promoted code could not
compute something, it returned something that read as safe:

* ``_should_close`` read ``parent.expiry``, which ``submit_option_order`` left NULL
  for every multi-leg, and ``expiry is not None`` then skipped the roll branch. The
  headline exit was dead for ``put_credit_spread`` and ``short_strangle`` and nobody
  knew — an entire GA campaign tuned a gene that could not fire.
* ``_tested`` documented "Missing chain rows or None deltas -> False (no action this
  bar)". A greek we could not fetch read as a short that is not tested.
* ``_txn_metrics`` returned ``(True, 0.0, 0.0)`` when it saw no short legs, so "I
  cannot see this structure's legs" and "this structure risks nothing" were the same
  answer.

Each of those is a silent hold. Here, an input that cannot be measured produces
``LIFECYCLE_UNKNOWN`` with a ``detail`` naming *which* input was missing, and
``pnl_pct`` stays ``None`` — never coerced to ``0.0``. ``LIFECYCLE_UNKNOWN`` is not a
closing reason and is not ``LIFECYCLE_HOLD``: it means "act on this yourself", and a
caller that folds it into hold has thrown away the only signal that would have caught
the dead roll gene.


Precedence
----------
Exactly one decision per structure, from the first rule that fires:

1. ``LIFECYCLE_BREAKER``     — the sleeve circuit breaker (a book-level state signal)
2. ``LIFECYCLE_COVER_LOST``  — a ``covered_call`` whose shares are gone
3. ``LIFECYCLE_PROFIT_CAPTURE``
4. ``LIFECYCLE_CREDIT_STOP``
5. ``LIFECYCLE_TESTED``
6. ``LIFECYCLE_ROLL_SHORT``  — a DECLARED two-expiry structure's overlay is due to be
   rolled. NOT a closing reason, and not performed by the decider's caller either: the
   ``roll_pmcc_short`` RULE owns it, in both runtimes.
7. ``LIFECYCLE_UNKNOWN``     — nothing fired, but some input could not be measured
8. ``LIFECYCLE_HOLD``        — nothing fired, and everything was measurable

**There is no roll-DTE CLOSE here any more** (2026-09-03). A single-expiry structure whose
expiry is upon it used to return ``LIFECYCLE_ROLL_DTE``, which the live pass closed — an
exit the backtest had no counterpart for unless the strategy's ruleset happened to carry
an ``opt_dte`` rule, so live exited more aggressively than any grid result could show. The
rule now owns that exit outright: ``days_to_expiry <= N`` -> ``DaysToExpiryCondition`` ->
``close_option``, walked by the live ``TradeManager`` and by ``daily_engine`` through the
same evaluator. One owner, two callers. A structure at its expiry therefore reaches
``LIFECYCLE_HOLD`` here — "no rule THIS module owns fired" — and the ruleset closes it.

**Why cover_lost outranks the premium rules.** A covered call that has lost its shares
is a NAKED short call, and that is true whether it is up 60% or down 200%. Ranking it
below profit capture would still close the position, but it would file the exit under
the reason the position *happened* to be at, and the sleeve's attribution would then
show a strategy quietly closing winners for no recorded cause — the same blindness
that hid the dead roll gene. The reason is the alarm.

**Why profit capture outranks the roll window** (the interesting case: a two-expiry
structure can easily be at +60% *and* inside its overlay's roll window at once). Here the
actions genuinely differ — capture CLOSES the structure, the roll keeps it and replaces the
overlay — so the precedence decides what happens, not merely what is recorded: a PMCC that
has hit its capture target is taken off rather than maintained. It also preserves
``_should_close``'s original branch order, so promoting the logic did not silently
re-label historical exits.

Breaker first, and profit/stop before tested/roll, are ``manage_open``'s and
``_should_close``'s original order, kept deliberately. Every step is pinned by a named
test — an unpinned precedence is a silent behaviour change the first time someone
reorders the branches.

**A definite action outranks an unmeasurable input.** ``LIFECYCLE_UNKNOWN`` replaces a
silent *hold*, not a decision we can actually make: an overlay at 3 DTE still rolls even
if its greeks are missing. Unknown only surfaces once every other rule has declined.


Cover that we cannot see is not cover that is gone
--------------------------------------------------
``cover_lost`` is the one rule here whose input comes from outside the option book
entirely — the SHARES a ``covered_call`` is written against, which no chain row and no
leg can report. The case it exists for is the one no other guard can see: a broker-side
stop fills at 3am, the shares leave, no platform code runs, and the short call is naked
until somebody looks.

It is therefore also the rule with the most expensive false positive, and the rule
obeys this module's central discipline exactly. ``cover_shares_held is None`` is
UNMEASURABLE — the position feed did not answer — and it NEVER fires ``cover_lost``. It
raises ``LIFECYCLE_UNKNOWN`` naming the missing input, like every other blind input
here. Liquidating a healthy covered call because a position feed hiccuped is a
self-inflicted loss, and "we could not measure the cover" and "the cover is gone" are
the two facts this codebase spends most of its effort keeping apart.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from ba2_common.core.option_expiry import (
    EXPIRY_RULE_ROLL_WINDOW,
    PMCC_STRATEGY,
    ExpiryLeg,
    is_multi_expiry_strategy,
    resolve_structure_expiry,
)
from ba2_common.core.option_types import OptionContract, OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection

LIFECYCLE_HOLD = "hold"
LIFECYCLE_PROFIT_CAPTURE = "profit_capture"
LIFECYCLE_CREDIT_STOP = "credit_stop"
#: ROLL THE OVERLAY, DO NOT CLOSE THE STRUCTURE. The two-expiry answer to the roll window,
#: and deliberately NOT a member of ``LIFECYCLE_CLOSING_REASONS``.
#:
#: ``roll_window_dte`` reads the SHORT leg, and on a declared two-expiry structure the short
#: is an OVERLAY over a long that has a year left, so its expiry approaching is a scheduled
#: maintenance event: buy the overlay back, sell the next one, keep the long. Returning a
#: CLOSING reason here would throw a LEAPS away every month, on schedule, and call it a roll.
#:
#: THE SINGLE-EXPIRY HALF OF THIS QUESTION IS NOT ASKED HERE ANY MORE (2026-09-03). It used
#: to return ``LIFECYCLE_ROLL_DTE``, a close, which is the right ACTION and was the wrong
#: OWNER: the backtest expresses it as the ``opt_dte`` rule (``days_to_expiry <= N`` ->
#: ``close_option``), so live carried a second, unsearched copy of an exit the GA was
#: simultaneously tuning. The rule owns it in both runtimes now, and this module answers only
#: the question the rule cannot: an overlay roll, which is not a close.
LIFECYCLE_ROLL_SHORT = "roll_short"
LIFECYCLE_TESTED = "tested"
LIFECYCLE_BREAKER = "circuit_breaker"
#: A ``covered_call`` whose shares are no longer there: the short call is NAKED. Only a
#: MEASURED shortfall fires it -- see the module docstring, and ``_cover_lost`` below.
LIFECYCLE_COVER_LOST = "cover_lost"
#: The decision could not be made: a missing expiry, greek or price. NOT a hold --
#: "we don't know" and "it's fine" are different facts, and collapsing them is the
#: mistake that hid the dead roll-DTE gene for an entire GA campaign.
LIFECYCLE_UNKNOWN = "unknown"

LIFECYCLE_CLOSING_REASONS = (LIFECYCLE_PROFIT_CAPTURE, LIFECYCLE_CREDIT_STOP,
                             LIFECYCLE_TESTED, LIFECYCLE_BREAKER, LIFECYCLE_COVER_LOST)

#: The ONE strategy tag that promises SHARE cover, and so the only one ``cover_lost``
#: polices. It must stay equal to ``OptionsAccountInterface.COVERED_CALL_STRATEGY`` --
#: the entry guard refuses to WRITE this tag uncovered and this module closes it once
#: the cover leaves, so a drift between the two strings would leave one half of the
#: promise unenforced. That module cannot be imported here (this one is pure; the
#: import-leak gate forbids ``core.interfaces``), so the equality is pinned by a test
#: instead: ``test_the_covered_call_tag_is_the_one_the_entry_guard_polices``.
COVERED_CALL_STRATEGY = "covered_call"

#: What a covered_call's REQUIREMENT is set to when the ledger cannot size it. It lives
#: here, in the pure module, because it is part of ``decide``'s input contract and not a
#: private detail of the caller that happens to send it (``option_lifecycle_service``
#: re-exports this name rather than defining its own). Same treatment as
#: ``COVERED_CALL_STRATEGY``, and for the same reason: a value one side invents and the
#: other side only recognises by accident is a contract that exists nowhere.
#:
#: It has to be a value that is neither ``None`` (which ``decide`` reads as "this caller
#: is not measuring cover for that structure", skipping the rule silently) nor a number
#: (which would be a fabricated obligation). ``_share_count`` cannot read it, so
#: ``_cover_lost`` reports ``LIFECYCLE_UNKNOWN`` naming it — the alarm, not a close and
#: not a hold. Pinned by ``test_the_unmeasurable_cover_sentinel_reaches_the_alarm_branch``.
COVER_REQUIREMENT_UNMEASURABLE = "unmeasurable"

#: One structure's share count as ``decide`` receives it. THREE kinds of value, and they
#: are three different facts that this module spends most of its effort keeping apart:
#:
#:   * an int/float — MEASURED, ``0`` included (``0`` held = the broker confirmed there
#:     are none, i.e. the call is naked);
#:   * ``None`` — NOT ASKED on the ``cover_shares_required`` side: the caller is not
#:     measuring cover for that structure, so ``cover_lost`` is not evaluated for it at
#:     all, and a transaction a mapping omits says exactly the same thing. On the
#:     ``cover_shares_held`` side the caller IS asking and the position feed did not
#:     answer, which is UNMEASURABLE and produces ``LIFECYCLE_UNKNOWN``. Never ``0``
#:     either way. The two ``decide`` docstrings state each side; this alias covers both
#:     and must not collapse them;
#:   * ``COVER_REQUIREMENT_UNMEASURABLE`` — ASKED, AND THE OBLIGATION COULD NOT BE SIZED.
#:     The one case a ``None`` requirement cannot express, which is why the sentinel is
#:     a ``str`` and why ``str`` is admitted here.
CoverValue = Union[int, float, str, None]

#: A share count ``decide`` is given per structure: one ``CoverValue`` for the whole
#: call, or a ``{transaction_id: CoverValue}`` mapping (what a pass over a BOOK has to
#: supply).
CoverInput = Optional[Union[int, float, str, Mapping[int, CoverValue]]]

#: WHERE THE PMCC'S OVERLAY SPEC LIVES: a dict on the ENTRY order's ``data``, written by
#: ``OpenPMCCAction`` through ``_submit_option_order(extra_entry_facts=...)`` and read back by
#: ``RollPMCCShortAction`` when it selects the NEXT overlay.
#:
#: The order row is the only thing that travels with the position, and the roll happens weeks
#: after the entry with a DIFFERENT recommendation in hand -- the same reason
#: ``earnings_stamp.ORDER_EVENT_DATE_KEY`` exists. Carrying it here rather than giving the roll
#: action its own selection genes is what keeps ONE overlay thesis per genome: a second
#: ``option_strike_delta`` on the roll rule would let a search enter at 0.15 delta and roll to
#: 0.30, and would double the overlay's gene budget at a population of 40.
#:
#: ABSENCE IS A REFUSAL, never a default. A position without this key was not opened by
#: ``open_pmcc``, and guessing which contract its overlay should be is exactly the fabricated
#: input this module exists to refuse.
ORDER_PMCC_OVERLAY_KEY = "pmcc_overlay"

#: The keys ``ORDER_PMCC_OVERLAY_KEY``'s dict carries -- the whole selection box the entry
#: picked its overlay from, so the roll re-selects under identical gates. Named here, once,
#: because the writer (``OpenPMCCAction``) and the reader (``RollPMCCShortAction``) are in a
#: different module from each other's tests.
PMCC_OVERLAY_SPEC_KEYS = ("strike_method", "strike_param", "dte_min", "dte_max",
                          "min_open_interest", "max_spread_pct", "min_volume")

#: Strategies whose loss stop is the *undefined-risk* multiple (``ur_stop_*``).
#: Everything else uses ``dr_stop_*``. Promoted verbatim from ``_should_close``:
#: the selection is by declared strategy, NOT by the measured risk of the legs.
UNDEFINED_RISK_STRATEGIES = ("short_put", "short_strangle")

#: Optional book-level state signal (not a configured threshold): when the sleeve
#: drawdown breaker has tripped, every structure is flattened with LIFECYCLE_BREAKER.
#: Absent means "not tripped"; the book layer computes and supplies it.
SETTING_BREAKER_TRIPPED = "circuit_breaker_tripped"

#: The overlay BUYBACK TRIGGER, as a percentage of the short leg's collected credit that has
#: decayed away (design §4: "roll loop at short expiry/buyback trigger"). OPTIONAL, by the
#: ``SETTING_BREAKER_TRIPPED`` idiom: present means the trigger is configured, ABSENT means
#: this caller does not use it -- not "0%", which would roll every overlay on sight.
#:
#: It is optional rather than required because the two callers ask for it differently. The
#: BACKTEST/grid path never reaches ``decide`` at all: its trigger is a ruleset leaf
#: (``credit_decayed_pct``), which is where the GA's gene has to land. The LIVE pass reaches
#: ``decide`` with an expert's settings, and requiring a key there would add a row to
#: ``option_lifecycle_service.REQUIRED_SETTINGS`` and so to every existing sleeve's settings
#: -- a migration for a knob no live expert uses yet.
SETTING_PMCC_BUYBACK_PCT = "pmcc_buyback_pct"

#: The strategy tag a ROLL order carries. Not a member of
#: ``option_expiry.MULTI_EXPIRY_OPTION_STRATEGIES``: the roll's two legs are admitted by the
#: guard because the TRANSACTION they belong to is declared, not because the roll invented its
#: own declaration. It IS a member of ``OptionsAccountInterface.NON_INTENT_STRATEGIES``, so a
#: roll can never restate what the position is.
PMCC_ROLL_STRATEGY = "pmcc_roll"

_EPS = 1e-9


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LifecycleLeg:
    """One NETTED contract of an open structure.

    ``net_qty`` is signed in contracts, BUY positive: ``+2`` is two long contracts,
    ``-1`` is one short. Netting is the caller's job and must be done per contract
    symbol over the executed orders (buy ``+``, sell ``-``), the way
    ``_tested``/``_close_structure`` already do it — a leg bought back to close nets
    to zero and stops counting, which is precisely what ``_txn_metrics`` failed to do.
    """
    contract_symbol: str
    net_qty: float
    strike: Optional[float] = None
    option_type: Optional[OptionRight] = None
    expiry: Optional[date] = None
    underlying: Optional[str] = None
    #: What this leg's OWN premium was, per share, at the fill that opened it -- positive for
    #: both sides (the magnitude; the direction is in ``net_qty``). ``None`` is UNKNOWN, never
    #: 0.0: the buyback trigger divides by it, and a zero basis is an undefined percentage
    #: rather than a total decay.
    #:
    #: The structure-level ``entry_net_premium`` cannot answer this. The buyback trigger asks
    #: how much of THIS overlay's credit has decayed, and after a roll the structure's net is a
    #: mix of a year-old LEAPS debit and several overlays' credits. Only the leg knows.
    entry_premium: Optional[float] = None

    @property
    def is_held(self) -> bool:
        return abs(self.net_qty) > _EPS

    @property
    def is_short(self) -> bool:
        return self.net_qty < -_EPS


@dataclass(frozen=True)
class OptionStructure:
    """ONE open option structure, as a value. No ORM rows, no broker handles.

    ``entry_net_premium`` is the entry net premium **per share, per structure**,
    signed the way ``TradeConditions`` signs it: positive = debit paid, negative =
    credit received. It is the percent basis, so ``None`` (unknown) and ``0``
    (even money) both leave the percentage undefined rather than zero.

    ``realized_cash`` is signed premium cash per share already banked on this
    structure from legs that are no longer held (sells ``+``, buys ``-``), scaled by
    contracts but not by the multiplier — the same units as ``entry_net_premium ×
    quantity``. It is genuinely ``0.0`` for an untouched structure, which is why it
    defaults; it is not a stand-in for an unknown.

    ``expiry`` is the structure's expiry when the parent row carries one. When it does
    not — which was every multi-leg before the parent started recording it — the legs
    are asked instead.
    """
    transaction_id: int
    underlying: str
    strategy: str
    legs: Sequence[LifecycleLeg] = field(default_factory=tuple)
    quantity: float = 1.0
    multiplier: int = 100
    entry_net_premium: Optional[float] = None
    realized_cash: float = 0.0
    expiry: Optional[date] = None

    def __post_init__(self):
        object.__setattr__(self, "legs", tuple(self.legs))

    @property
    def held_legs(self) -> Tuple[LifecycleLeg, ...]:
        """Held legs, in contract-symbol order so every derived answer is stable."""
        return tuple(sorted((l for l in self.legs if l.is_held),
                            key=lambda l: l.contract_symbol))


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LifecycleDecision:
    """What to do with ONE open option structure. Pure: no DB, no broker, no clock."""
    transaction_id: int
    reason: str                      # one of the LIFECYCLE_* constants
    detail: str                      # human-readable, e.g. "18 DTE <= roll_dte 21"
    pnl_pct: Optional[float] = None  # None means unmeasurable, never 0.0

    @property
    def should_close(self) -> bool:
        return self.reason in LIFECYCLE_CLOSING_REASONS


@dataclass(frozen=True)
class StructureMetrics:
    """``_txn_metrics``, promoted: the capital a structure ties up.

    Every field is ``None`` when it could not be measured, and ``detail`` says why.
    The promoted version returned ``(True, 0.0, 0.0)`` for "no short legs found",
    which is indistinguishable from "no legs visible" — and before the leg
    reconciliation landed, *no* live multi-leg had visible legs, so every rail that
    consumes this saw a book committing zero dollars.
    """
    is_defined_risk: Optional[bool]
    notional: Optional[float]
    committed: Optional[float]
    detail: str = ""


# ---------------------------------------------------------------------------
# settings access -- explicit, no silent defaults
# ---------------------------------------------------------------------------
def _require(settings: Mapping[str, Any], key: str) -> Any:
    """Read a configured threshold. A missing one is loud, never a default."""
    if key not in settings:
        raise KeyError(
            f"option_lifecycle: required setting {key!r} is missing — refusing to "
            f"substitute a default for a risk threshold")
    return settings[key]


def _as_date(as_of) -> date:
    return as_of.date() if isinstance(as_of, datetime) else as_of


# ---------------------------------------------------------------------------
# metrics (promoted _txn_metrics)
# ---------------------------------------------------------------------------
def structure_metrics(structure: OptionStructure) -> StructureMetrics:
    """(is_defined_risk, notional, committed) for one held structure.

    ``notional`` is the per-side stress basis kept from ``_txn_metrics``: the largest
    short strike x 100 x the largest short contract count. ``committed`` is the wing
    width for each short covered by a long of the SAME option type, plus full notional
    for every short that is not covered.

    Two corrections to the promoted formula, both of them defects rather than choices:

    * it netted nothing — a buy-to-close landed in ``longs`` while the original short
      stayed in ``shorts``, so committed capital could only ever rise;
    * width was ``min(short strikes) - max(long strikes)``, which is right only for a
      put vertical. For a call vertical the long sits *above* the short, so the width
      came out negative and the branch ``if width <= 0: return (False, ...)`` reported
      a defined-risk spread as naked. An iron condor (90/85 puts, 110/115 calls) gave
      ``90 - 115 = -25`` and was likewise booked as naked at the full 110 notional.
    """
    held = structure.held_legs
    if not held:
        return StructureMetrics(None, None, None,
                                "no held option legs — the structure's committed "
                                "capital is unmeasurable")

    shorts = [l for l in held if l.is_short]
    longs = [l for l in held if not l.is_short]

    for leg in held:
        if leg.strike is None:
            return StructureMetrics(None, None, None,
                                    f"no strike for {leg.contract_symbol} — notional "
                                    f"and committed capital are unmeasurable")

    if not shorts:
        # Every leg is long: the debit already paid is the whole risk, and there is no
        # short notional to stress. Distinct from "no legs visible", which is unknown.
        return StructureMetrics(True, 0.0, 0.0)

    for leg in shorts:
        if leg.option_type is None:
            return StructureMetrics(None, None, None,
                                    f"no option type for short {leg.contract_symbol} — "
                                    f"it cannot be paired with a protective long")

    notional = (max(l.strike for l in shorts) * 100.0
                * max(abs(l.net_qty) for l in shorts))

    committed = 0.0
    any_naked = False
    for right in sorted({l.option_type for l in shorts}, key=lambda r: str(r)):
        side_shorts = [l for l in shorts if l.option_type == right]
        side_longs = [l for l in longs if l.option_type == right]
        n_short = sum(abs(l.net_qty) for l in side_shorts)
        n_long = sum(l.net_qty for l in side_longs)
        covered = min(n_short, n_long)
        naked = max(0.0, n_short - n_long)
        if covered > 0:
            # Conservative, as the promoted version was: the widest pairing.
            width = max(abs(s.strike - l.strike) for s in side_shorts for l in side_longs)
            committed += covered * width * 100.0
        if naked > 0:
            any_naked = True
            committed += naked * max(l.strike for l in side_shorts) * 100.0

    return StructureMetrics(not any_naked, notional, committed)


# ---------------------------------------------------------------------------
# assignment cost -- ONE formula, two callers
# ---------------------------------------------------------------------------
def put_assignment_cost(strike: Optional[float],
                        contracts: Optional[float],
                        multiplier: Optional[int]) -> Optional[float]:
    """Cash to take delivery on ``contracts`` SHORT puts at ``strike``, or ``None``.

    ``strike x contracts x multiplier``, and nothing else: when a short put is assigned
    the account buys the shares at the strike, in cash, that night. No credit is netted
    off (the premium was banked at entry and is already in the balance this is measured
    against), and no long wing is netted off either — the long put is OUR right, which
    we would have to choose to exercise, on a LATER day, *after* paying for the shares.

    THE single definition of the arithmetic, deliberately shared by the two callers so
    they cannot fork the way the two IV-rank implementations did:

    * ``option_book.book_totals`` — the PURE sleeve rail, summing over ``LifecycleLeg``
      values supplied by the caller;
    * ``OptionsAccountInterface.short_put_assignment_exposure`` — the account-wide
      second view, summing over netted order rows.

    ``None`` means UNMEASURABLE and every caller must decline on it. Zero is returned
    only for a genuine zero — ``contracts == 0`` (nothing is held, so nothing can be
    assigned). A strike of ``0`` is NOT a free put: no listed equity option has one, so
    it is a missing field, and this codebase's rule is that a missing field is unknown.
    """
    if contracts is None or multiplier is None or strike is None:
        return None
    try:
        strike = float(strike)
        contracts = float(contracts)
        multiplier = float(multiplier)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(strike) and math.isfinite(contracts)
            and math.isfinite(multiplier)):
        return None
    if strike <= 0 or multiplier <= 0:
        return None
    if contracts < 0:
        # A negative contract count would BUY capacity: it is the sign of a caller that
        # passed a signed net_qty instead of its magnitude, not of an obligation we owe
        # less than nothing on.
        return None
    return strike * contracts * multiplier


# ---------------------------------------------------------------------------
# the three measurements each rule needs
# ---------------------------------------------------------------------------
def _exit_mark(leg: LifecycleLeg, row: OptionContract) -> Optional[float]:
    """What flattening this leg trades at: sell the long at the bid, buy the short
    back at the ask, ``last`` only when that side of the quote is missing. Swapping
    the two flatters every position by the width of the spread."""
    if leg.is_short:
        return row.ask if row.ask is not None else row.last
    return row.bid if row.bid is not None else row.last


def _pnl_pct(structure: OptionStructure,
             chain_by_symbol: Mapping[str, OptionContract]) -> Tuple[Optional[float], str]:
    """(% of the entry net premium captured, "") or (None, why it is unmeasurable).

    Mirrors ``TradeConditions._get_spread_pnl_via_transaction``: cash banked plus the
    cash of flattening what is still held, over the entry premium basis. For an
    untouched credit structure this reduces to "% of credit captured" — a 2.00 credit
    now costing 1.00 to close is +50%.
    """
    held = structure.held_legs
    if not held:
        return None, ("no held option legs — the structure's P&L is unmeasurable")
    if structure.entry_net_premium is None:
        return None, "entry net premium is unknown — the P&L percent basis is undefined"
    if abs(structure.entry_net_premium) < _EPS:
        return None, "entry net premium is 0 — the P&L percent basis is undefined"
    if structure.quantity is None or abs(structure.quantity) < _EPS:
        return None, "structure quantity is 0 — the P&L percent basis is undefined"
    if not structure.multiplier:
        return None, "no contract multiplier — the P&L is unmeasurable"

    flatten_cash = 0.0
    for leg in held:                       # held_legs is contract-symbol ordered
        row = chain_by_symbol.get(leg.contract_symbol)
        if row is None:
            return None, (f"no chain row for {leg.contract_symbol} — the structure's "
                          f"P&L is unmeasurable")
        mark = _exit_mark(leg, row)
        if mark is None:
            return None, (f"no usable mark for {leg.contract_symbol} (bid/ask/last all "
                          f"missing) — the structure's P&L is unmeasurable")
        # Flattening signed qty -net at the mark: a long (net>0) is sold for +net*mark,
        # a short (net<0) is bought back for net*mark (negative cash). One expression.
        flatten_cash += leg.net_qty * mark

    entry_cash = -structure.entry_net_premium * abs(structure.quantity)
    amount = (entry_cash + structure.realized_cash + flatten_cash) * structure.multiplier
    basis = abs(structure.entry_net_premium) * abs(structure.quantity) * structure.multiplier
    return round(amount / basis * 100.0, 4), ""


def _tested(structure: OptionStructure,
            chain_by_symbol: Mapping[str, OptionContract],
            threshold: float) -> Tuple[Optional[bool], str, str]:
    """(True/False/None, the contract that is tested, why it is unmeasurable).

    True iff any CURRENTLY-SHORT contract's ``|delta|`` has reached ``threshold``.
    ``None`` — not ``False`` — when a short leg has no chain row or no delta: the
    promoted version documented "Missing chain rows or None deltas -> False (no action
    this bar)", which is a silent hold dressed as an answer.

    Only net-SHORT legs are asked. A long wing's delta is meaningless here (and a deep
    long wing would otherwise close every healthy spread), so a long leg with no delta
    never makes us blind.
    """
    blind = ""
    for leg in structure.held_legs:        # contract-symbol ordered
        if not leg.is_short:
            continue
        row = chain_by_symbol.get(leg.contract_symbol)
        if row is None:
            blind = blind or (f"no chain row for short {leg.contract_symbol} — its "
                              f"|delta| is unknown")
            continue
        if row.delta is None:
            blind = blind or (f"no delta for short {leg.contract_symbol} — the "
                              f"tested-delta check is blind")
            continue
        if abs(row.delta) >= threshold:
            return True, (f"short {leg.contract_symbol} |delta| {abs(row.delta):.4f} "
                          f">= tested_delta {threshold:g}"), ""
    if blind:
        return None, "", blind
    return False, "", ""


def roll_window_dte(structure: OptionStructure, as_of: date) -> Tuple[Optional[int], str]:
    """(days to expiry, "") or (None, why it is unmeasurable).

    THE QUESTION THIS ANSWERS IS THE ROLL WINDOW. Its only consumer is the ``roll_dte``
    branch of :func:`_decide_one` — "how long until this structure must be rolled or
    closed?" — so on a two-expiry structure it reads the **SHORT** leg
    (:data:`option_expiry.EXPIRY_RULE_ROLL_WINDOW`). A PMCC rolls when its overlay expires;
    reading the LEAPS here would put the roll a year out and the roll branch would never
    fire, which is the same dead-branch failure this function was written to end.

    Its sibling reader, ``TradeConditions.DaysToExpiryCondition``, answers the OTHER
    question — the roll floor / structure exit — and therefore reads the LONG leg. The two
    disagree on purpose; see ``option_expiry`` for the rule table.

    The structure-level ``expiry`` and the held legs are all candidates. When they yield a
    single date, that date is the answer and the leg rule is not exercised at all — which is
    why every single-expiry structure behaves exactly as before. Legs that disagree are a
    contradiction for any strategy NOT declared in
    ``option_expiry.MULTI_EXPIRY_OPTION_STRATEGIES``: unknown, not ``max()`` and not
    ``min()``. A leg that simply has no expiry adds no information and does not veto the
    legs that do.
    """
    resolution = resolve_structure_expiry(
        [ExpiryLeg(expiry=l.expiry, net_qty=l.net_qty) for l in structure.legs],
        strategy=structure.strategy,
        rule=EXPIRY_RULE_ROLL_WINDOW,
        declared_expiries=(structure.expiry,),
    )
    if resolution.missing:
        return None, ("no expiry on the structure or any of its held legs — the roll "
                      "window cannot be evaluated")
    if resolution.expiry is None:
        listed = ", ".join(str(e) for e in resolution.conflict)
        return None, (f"conflicting expiries on one structure ({listed}) — its DTE is "
                      f"undefined")
    return (resolution.expiry - as_of).days, ""


#: The name this function has always carried inside ``_decide_one``. Kept so the module's own
#: suite and the per-leg-rule suite keep addressing it as they do; ``roll_window_dte`` is the
#: same function under the name a CALLER outside this module should use, and the rule-level
#: reader ``TradeConditions.ShortLegDaysToExpiryCondition`` calls it by that name so the roll
#: window has exactly one implementation for both runtimes.
_dte = roll_window_dte


# ---------------------------------------------------------------------------
# the two-expiry (PMCC / calendar) overlay: roll, restamp, and the invariant
# ---------------------------------------------------------------------------
def held_short_leg(structure: OptionStructure) -> Optional[LifecycleLeg]:
    """The overlay: the NEAREST-expiry held SHORT leg, or ``None`` when there is none.

    Nearest, because a roll in flight can transiently leave two shorts on the books and it is
    the soonest that forces the next decision -- the same tie-break
    ``option_expiry.resolve_structure_expiry`` makes for the roll window, kept identical on
    purpose. A short with no recorded expiry sorts last rather than winning by accident.
    """
    shorts = [leg for leg in structure.held_legs if leg.is_short]
    if not shorts:
        return None
    return min(shorts, key=lambda l: (l.expiry is None, l.expiry or date.max))


def held_long_leg(structure: OptionStructure) -> Optional[LifecycleLeg]:
    """The cover: the FARTHEST-expiry held LONG leg, or ``None`` when there is none.

    Farthest, and that is the mirror of :func:`held_short_leg`'s nearest for the same reason:
    the question this leg answers is "how much structure is left", and the long that decides
    it is the one that outlives the others. On a PMCC there is exactly one.
    """
    longs = [leg for leg in structure.held_legs if not leg.is_short]
    if not longs:
        return None
    return max(longs, key=lambda l: (l.expiry is not None, l.expiry or date.min))


def credit_decay_pct(entry_credit: Optional[float],
                     current_ask: Optional[float]) -> Optional[float]:
    """How much of a sold option's credit has DECAYED, as a percent. ``None`` = unmeasurable.

    ``(entry_credit - current_ask) / entry_credit x 100``: ``0`` the moment it was sold,
    ``100`` when it can be bought back for nothing, and NEGATIVE when the short has gone
    against us and costs more than it brought in. Ask, not bid or mid, because buying the
    overlay back is what the number is deciding.

    THE single definition, shared by the two callers so they cannot fork: the live pass
    (``decide`` -> :func:`pmcc_credit_decay`) and the rule-level
    ``TradeConditions.CreditDecayedPctCondition`` that backs the grid's ``credit_decayed_pct``
    gene.

    ``None`` for every input that is not a usable number, and specifically for an entry credit
    of ``0`` -- an option sold for nothing has no credit to decay, so the percentage is
    undefined rather than 100. Refusing here is what stops "we could not price the overlay"
    from reading as "the overlay is worthless, roll it".
    """
    if entry_credit is None or current_ask is None:
        return None
    try:
        credit, ask = float(entry_credit), float(current_ask)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(credit) and math.isfinite(ask)):
        return None
    if credit <= _EPS or ask < 0:
        return None
    return round((credit - ask) / credit * 100.0, 4)


def pmcc_credit_decay(structure: OptionStructure,
                      chain_by_symbol: Mapping[str, OptionContract]
                      ) -> Tuple[Optional[float], str]:
    """(% of the overlay's credit that has decayed, "") or (None, why it is unmeasurable)."""
    short = held_short_leg(structure)
    if short is None:
        return None, ("no held short leg — there is no overlay whose credit could have "
                      "decayed")
    row = chain_by_symbol.get(short.contract_symbol)
    if row is None:
        return None, (f"no chain row for the overlay {short.contract_symbol} — what it costs "
                      f"to buy back is unknown")
    ask = row.ask if row.ask is not None else row.last
    pct = credit_decay_pct(short.entry_premium, ask)
    if pct is None:
        return None, (f"the overlay {short.contract_symbol} cannot be priced against its own "
                      f"entry credit ({short.entry_premium!r} sold, {ask!r} to buy back) — "
                      f"the decayed fraction is undefined")
    return pct, ""


def intrinsic_floor_per_contract(signed_cash_per_structure: Optional[float],
                                 multiplier: int = 100) -> Optional[float]:
    """Design §3's intrinsic floor, from the cash a structure has actually PAID. ``None`` =
    unmeasurable.

    ``signed_cash_per_structure`` is the net premium outlay per structure with BUY positive:
    the LEAPS debit, less every credit the overlay has banked since. So this IS "max loss =
    LEAPS debit − net credits", stated as one number rather than as a base plus a running
    correction — and that difference is the whole point.

    WHY NOT INCREMENTAL. The obvious implementation restamps at each roll by adding that
    roll's net to the previous stamp, and it is wrong twice over: a ticket that never fills
    still moves the number (an unfilled DEBIT roll raises the floor and LOOSENS ``opt_sl_ml``
    on a position that never paid anything), and every later roll then compounds from that
    wrong base. Deriving from the EXECUTED fills instead is idempotent — recomputing it a
    hundred times gives the same answer, an unfilled ticket contributes nothing because it has
    no fill, and there is no base to drift.

    CLAMPED AT ZERO, and that is a statement rather than a guard: once the accrued credits have
    paid for the long outright there is no defined loss left, and ``loss_pct_of_max_loss``
    self-disarms on a non-positive stamp (its ``per_contract <= 0 -> None`` branch). A
    structure that cannot lose has no loss-as-a-percentage-of-its-loss, and reporting one would
    be inventing a denominator.
    """
    if signed_cash_per_structure is None:
        return None
    try:
        cash, mult = float(signed_cash_per_structure), float(multiplier)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(cash) and math.isfinite(mult)) or mult <= 0:
        return None
    return max(0.0, cash * mult)


def restamped_max_loss(previous_max_loss: Optional[float], roll_net: Optional[float],
                       multiplier: int = 100) -> Optional[float]:
    """The structure's max loss after a roll, per contract. ``None`` = do not restamp.

    NO LONGER THE PERSISTED PATH (2026-09-02 review). The stamp is DERIVED from executed fills
    by :func:`intrinsic_floor_per_contract` — see its docstring for why an incremental restamp
    written at submit time is the wrong shape. This is kept because it states the per-roll
    ARITHMETIC the derived floor must agree with, and the two are pinned against each other by
    ``test_the_derived_floor_agrees_with_the_per_roll_arithmetic``.

    ``max_loss_new = max_loss_old + roll_net x multiplier``, with ``roll_net`` SIGNED the way
    every option limit in this codebase is: positive = a net debit paid, negative = a net
    credit received. So a roll that banks a 1.20 credit lowers the floor by $120, and a roll
    that costs more to buy back than the new overlay brings in RAISES it. One expression,
    both directions, because "the credit accrued" and "the buyback cost more than the
    re-sale" are the same arithmetic with opposite signs.

    This IS design §3's "restamped as credits accrue": the intrinsic floor is the LEAPS debit
    less every credit collected since, and each roll is one more term.

    CLAMPED AT ZERO, and that is a statement rather than a guard. Once the accrued credits
    have paid for the LEAPS outright there is no defined loss left, and
    ``loss_pct_of_max_loss`` self-disarms on a non-positive stamp (its
    ``per_contract <= 0 -> None`` branch). A structure that cannot lose has no
    loss-as-a-percentage-of-its-loss, and reporting one would be inventing a denominator.

    ``None`` in either argument returns ``None``: an unmeasurable roll must leave the existing
    stamp ALONE. Overwriting it with a guess is worse than leaving a slightly stale
    conservative number, because the stamp is a risk denominator.
    """
    if previous_max_loss is None or roll_net is None:
        return None
    try:
        previous, net, mult = (float(previous_max_loss), float(roll_net), float(multiplier))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(previous) and math.isfinite(net) and math.isfinite(mult)):
        return None
    return max(0.0, previous + net * mult)


def uncovered_short_calls(legs: Iterable[LifecycleLeg]) -> Tuple[str, ...]:
    """THE INVARIANT, as a measurement: the short CALL contracts nothing is covering.

    A net-short call is covered by a net-long call of the same underlying; short contracts
    beyond the long count are naked, and a naked call is the one option position in this
    platform whose loss is genuinely unbounded (``option_payoff.max_loss`` returns UNBOUNDED
    for exactly this shape). Every mutating path on a two-expiry structure -- the roll, the
    structure close -- asks this about the leg set it would LEAVE BEHIND and refuses on a
    non-empty answer.

    CALLS ONLY, deliberately. A short PUT is bounded below (strike minus credit, at an
    underlying of zero), so it is a risk this codebase measures rather than an invariant it
    forbids; folding it in here would make the guard refuse every cash-secured put ever
    written. Short STOCK is likewise out of scope: this module sees option legs.

    Contracts come back sorted, so a refusal message is the same on every run.
    """
    shorts, longs = [], 0.0
    for leg in legs:
        if leg.option_type != OptionRight.CALL or not leg.is_held:
            continue
        if leg.is_short:
            shorts.append(leg)
        else:
            longs += leg.net_qty
    if not shorts:
        return ()
    short_qty = sum(abs(l.net_qty) for l in shorts)
    if longs + _EPS >= short_qty:
        return ()
    return tuple(sorted(l.contract_symbol for l in shorts))


def _intents(legs: Sequence[OptionLeg]) -> List[str]:
    return [(leg.position_intent or "").strip().lower() for leg in legs]


def close_legs_are_fail_closed(legs: Sequence[OptionLeg]) -> Optional[str]:
    """``None`` when this closing ticket releases the cover LAST, else why it does not.

    FAIL-CLOSED MEANS ONE THING HERE: the long is let go only after the short it covers is
    bought back. Both legs ride one multi-leg order, so neither can happen without the other
    at the account level -- but the ORDER of the legs is the statement of intent, it is what a
    broker works and what a sequential fallback would follow, and a rule nobody wrote down is
    a rule the next edit deletes.

    Applied ONLY to a declared two-expiry structure (the caller decides). On a single-expiry
    combo every leg settles on one day and the order inside the ticket carries no risk
    meaning, so imposing it there would reshuffle existing structures' leg lists for nothing.
    """
    intents = _intents(legs)
    last_buy_to_close = max((i for i, it in enumerate(intents) if it == "buy_to_close"),
                            default=None)
    first_sell_to_close = min((i for i, it in enumerate(intents) if it == "sell_to_close"),
                              default=None)
    if last_buy_to_close is None or first_sell_to_close is None:
        return None                     # nothing to order: no short, or no long
    if first_sell_to_close < last_buy_to_close:
        return ("closing legs are not fail-closed: the long is sold to close at position "
                f"{first_sell_to_close} BEFORE the short is bought back at position "
                f"{last_buy_to_close}. The long IS the cover — releasing it first leaves a "
                f"naked short call, which is the one position this structure may never hold.")
    return None


def roll_legs_are_fail_closed(legs: Sequence[OptionLeg]) -> Optional[str]:
    """``None`` when this roll ticket closes the old overlay BEFORE opening the new one.

    Exactly two legs, in exactly that order. The new short is written only behind the
    buy-back of the old one, so the ticket can never describe a moment at which the structure
    owes two overlays against one long.

    A roll that cannot be built this way is not submitted at all -- and that is the safe
    failure, because a PMCC whose overlay expired unrolled is a long call with no cover,
    which loses money slowly. A PMCC with two overlays is a naked call, which does not.
    """
    if len(legs) != 2:
        return (f"a roll ticket is exactly two legs (buy back the old overlay, sell the "
                f"next); got {len(legs)}")
    intents = _intents(legs)
    if intents[0] != "buy_to_close":
        return (f"the roll's FIRST leg must buy back the expiring overlay, but it is "
                f"{intents[0]!r}")
    if intents[1] != "sell_to_open":
        return (f"the roll's SECOND leg must sell the next overlay, but it is "
                f"{intents[1]!r}")
    if legs[0].side != OrderDirection.BUY or legs[1].side != OrderDirection.SELL:
        return ("the roll's leg SIDES contradict their intents: buying back is a BUY and "
                f"selling the next overlay is a SELL, got {legs[0].side} / {legs[1].side}")
    return None


def pmcc_roll_due(structure: OptionStructure,
                  chain_by_symbol: Mapping[str, OptionContract],
                  as_of: date, *, roll_dte: int,
                  buyback_pct: Optional[float] = None) -> Tuple[bool, str, str]:
    """(roll the overlay, why, why it is unmeasurable) for ONE two-expiry structure.

    Design §4's two triggers, in the order they are stated: the overlay's own expiry window,
    then the buyback trigger. ``buyback_pct`` of ``None`` means the second trigger is not
    configured -- NOT 0 %, which would roll every overlay the moment it was written.

    Only the roll WINDOW can make this unmeasurable in a way worth reporting: a structure
    whose overlay cannot be priced still rolls on schedule, so an unpriceable buyback merely
    leaves that trigger unasked. The same "a definite action outranks an unmeasurable input"
    rule the module applies everywhere else.
    """
    dte, dte_blind = roll_window_dte(structure, as_of)
    if dte is not None and dte <= roll_dte:
        return True, f"overlay at {dte} DTE <= roll_dte {roll_dte}", ""
    decay_blind = ""
    if buyback_pct is not None:
        decayed, decay_blind = pmcc_credit_decay(structure, chain_by_symbol)
        if decayed is not None and decayed >= float(buyback_pct):
            return True, (f"overlay credit {decayed:.2f}% decayed >= buyback trigger "
                          f"{float(buyback_pct):g}%"), ""
    return False, "", "; ".join(b for b in (dte_blind, decay_blind) if b)


# ---------------------------------------------------------------------------
# the cover a covered_call is written against
# ---------------------------------------------------------------------------
def _cover_input(supplied, transaction_id: int):
    """One structure's share of a cover argument.

    ``decide`` manages a BOOK, so each cover figure may arrive either as a per-structure
    ``{transaction_id: value}`` mapping (what a live pass over several structures has to
    supply) or as a single value, which then applies to every structure — the form a
    caller with exactly one structure naturally writes. A transaction absent from a
    mapping is simply not being tracked for cover, which is the same as ``None``.
    """
    if isinstance(supplied, Mapping):
        return supplied.get(transaction_id)
    return supplied


def _share_count(raw, *, round_up: bool) -> Optional[int]:
    """A whole number of shares, or ``None`` when the value cannot be read.

    Never coerces to ``0``: an unreadable share count is the UNMEASURABLE case, and a
    zero here would read as "the account holds nothing", which is the exact confusion
    ``cover_lost`` must not make. ``bool`` is refused because ``True`` is not one share.

    ``round_up`` picks the direction, the same way the two accessors that produce these
    numbers do: a REQUIREMENT is rounded up (``check_cover_for_covered_call`` and
    ``shares_pledged_to_short_calls`` both ceil — under-stating an obligation by a share
    is what leaves a contract uncovered) and a HOLDING is rounded down
    (``held_shares_for_cover`` floors — a fraction of a share covers nothing). Both
    inputs are already integral in practice; this only decides which way the dust falls,
    and it falls the same way at both ends of the cover ledger.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return int(math.ceil(value) if round_up else math.floor(value))


def _cover_lost(structure: OptionStructure,
                required_raw, held_raw) -> Tuple[bool, str, str]:
    """(the cover is gone, why, why it is unmeasurable) for ONE structure.

    Returns ``(False, "", "")`` — nothing to say — for everything this rule does not
    police, and that list is deliberate:

    * a structure that is not tagged ``covered_call``. Only that tag promises SHARE
      cover (the same line ``check_cover_for_covered_call`` draws at the entry seam): a
      short strangle is meant to be naked, and a bear call spread answers for its short
      call with a long call, so closing either of those on a share count would be a
      liquidation with no cause;
    * a caller that supplied no requirement for this structure (``required is None``).
      Cover is an input this module cannot derive — no chain row and no leg reports the
      shares — so a caller that does not measure it is not asking the question, and
      every pre-existing caller falls here and is unaffected;
    * a requirement of ``0``: the short call has been bought back and nothing can be
      called away, so no share count can make this structure naked. Same ``need <= 0``
      skip the entry guard performs, and it is what stops a flat structure from being
      "closed" forever on the strength of an empty position feed.

    ``held is None`` is UNMEASURABLE and comes back as the third element (an alarm), NOT
    as the first. That is the whole point of the rule — see the module docstring.
    """
    if (structure.strategy or "").strip().lower() != COVERED_CALL_STRATEGY:
        return False, "", ""
    if required_raw is None:
        return False, "", ""

    required = _share_count(required_raw, round_up=True)
    if required is None:
        return False, "", (
            f"the cover this covered_call needs is unreadable ({required_raw!r}) — "
            f"whether its short call is still covered by {structure.underlying} shares "
            f"cannot be evaluated")
    if required <= 0:
        return False, "", ""

    held = _share_count(held_raw, round_up=False)
    if held is None:
        # UNKNOWN, never cover_lost. A position feed that did not answer is not a
        # position that is gone, and closing on it would be a self-inflicted loss.
        return False, "", (
            f"how many {structure.underlying} shares cover this covered_call could not "
            f"be measured — its {required}-share cover is UNKNOWN, which is NOT the "
            f"same as gone, so nothing is being closed on the strength of it")
    if held >= required:
        return False, "", ""
    return True, (
        f"covered_call cover is GONE: {held} {structure.underlying} share(s) available "
        f"against {required} required — short by {required - held}, so the short call "
        f"is NAKED"), ""


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------
def decide(structures: Iterable[OptionStructure],
           chain_by_symbol: Mapping[str, OptionContract],
           settings: Mapping[str, Any],
           as_of,
           *,
           cover_shares_held: CoverInput = None,
           cover_shares_required: CoverInput = None) -> List[LifecycleDecision]:
    """One decision per structure, in a deterministic order.

    :param structures:      the open structures, as values (see ``OptionStructure``).
    :param chain_by_symbol: ``{OCC contract symbol: OptionContract}`` — quotes and
                            greeks for every leg. A symbol that is absent is
                            *unknown*, not zero.
    :param settings:        the expert's thresholds. Required: ``profit_capture_pct``,
                            ``roll_dte``, ``tested_delta_enabled``, ``dr_stop_enabled``,
                            ``ur_stop_enabled``; plus ``strangle_capture_pct``,
                            ``tested_delta``, ``dr_stop_credit_mult`` and
                            ``ur_stop_credit_mult`` when the rule that reads them
                            applies. A missing one raises rather than defaulting.
                            ``circuit_breaker_tripped`` is an optional book-level state
                            signal, not a threshold; absent means "not tripped".
    :param as_of:           the evaluation instant (``datetime`` or ``date``). This
                            function never reads a clock.
    :param cover_shares_required: shares a ``covered_call`` can have called away, and so
                            the cover it needs. Either one value or a
                            ``{transaction_id: value}`` mapping. ``None`` — the default,
                            and every transaction a mapping omits — means the caller is
                            not measuring cover for that structure, so ``cover_lost`` is
                            not evaluated for it at all. Existing callers are unaffected.
    :param cover_shares_held: shares AVAILABLE to cover that structure, in the same two
                            shapes. TRI-STATE, exactly as the accessor that produces it:
                            an int including ``0`` is MEASURED (``0`` = the broker
                            confirmed there are none, i.e. the call is naked); ``None``
                            is UNMEASURABLE and produces ``LIFECYCLE_UNKNOWN``, NEVER a
                            ``cover_lost`` close.

    Output is sorted by ``transaction_id``, so the same book produces the same list
    whatever order the caller iterated its holdings in.
    """
    as_of_date = _as_date(as_of)
    breaker = bool(settings[SETTING_BREAKER_TRIPPED]) if SETTING_BREAKER_TRIPPED in settings else False

    ordered = sorted(structures, key=lambda s: s.transaction_id)
    return [_decide_one(s, chain_by_symbol, settings, as_of_date, breaker,
                        _cover_input(cover_shares_required, s.transaction_id),
                        _cover_input(cover_shares_held, s.transaction_id))
            for s in ordered]


def _decide_one(structure: OptionStructure,
                chain_by_symbol: Mapping[str, OptionContract],
                settings: Mapping[str, Any],
                as_of: date,
                breaker_tripped: bool,
                cover_required=None,
                cover_held=None) -> LifecycleDecision:
    txn = structure.transaction_id
    # Priced first, and priced once: every decision carries the P&L at the moment it
    # was taken, whatever the reason -- that is what the outcome table reads back.
    # It stays None when unmeasurable; a breaker flatten of a structure we could not
    # price reports None, not a comforting 0.0.
    pnl_pct, pnl_blind = _pnl_pct(structure, chain_by_symbol)

    # 1. the sleeve circuit breaker flattens the whole book, measurable or not.
    if breaker_tripped:
        return LifecycleDecision(txn, LIFECYCLE_BREAKER,
                                 "sleeve circuit breaker tripped — flattening the book",
                                 pnl_pct)

    # 2. the cover is gone: this covered_call's short call is naked. Ahead of every
    #    premium rule, because a structure that has lost its cover must close whether it
    #    is winning or losing, and the RECORDED REASON is the alarm.
    lost, cover_detail, cover_blind = _cover_lost(structure, cover_required, cover_held)
    if lost:
        return LifecycleDecision(txn, LIFECYCLE_COVER_LOST, cover_detail, pnl_pct)

    # 3. profit capture.
    if structure.strategy == "short_strangle":
        capture_key, capture = "strangle_capture_pct", float(_require(settings, "strangle_capture_pct"))
    else:
        capture_key, capture = "profit_capture_pct", float(_require(settings, "profit_capture_pct"))
    if pnl_pct is not None and pnl_pct >= capture:
        return LifecycleDecision(
            txn, LIFECYCLE_PROFIT_CAPTURE,
            f"P&L {pnl_pct:.2f}% >= {capture_key} {capture:g}%", pnl_pct)

    # 4. the credit-multiple stop. Undefined-risk strategies use ur_stop_*, everything
    #    else dr_stop_* -- and the if/elif is deliberate: with ur_stop disabled a naked
    #    structure has NO stop even when dr_stop is on. Promoted verbatim, pinned by a
    #    test so the surprise is at least a recorded one.
    if pnl_pct is not None:
        if structure.strategy in UNDEFINED_RISK_STRATEGIES:
            label, enabled, mult_key = "ur_stop", _require(settings, "ur_stop_enabled"), "ur_stop_credit_mult"
        else:
            label, enabled, mult_key = "dr_stop", _require(settings, "dr_stop_enabled"), "dr_stop_credit_mult"
        if enabled:
            mult = float(_require(settings, mult_key))
            limit = -100.0 * mult
            if pnl_pct <= limit:
                return LifecycleDecision(
                    txn, LIFECYCLE_CREDIT_STOP,
                    f"P&L {pnl_pct:.2f}% <= {label} {mult:g}x credit ({limit:.2f}%)",
                    pnl_pct)

    # 5. the tested short.
    tested, tested_detail, tested_blind = (False, "", "")
    if _require(settings, "tested_delta_enabled"):
        tested, tested_detail, tested_blind = _tested(
            structure, chain_by_symbol, float(_require(settings, "tested_delta")))
        if tested:
            return LifecycleDecision(txn, LIFECYCLE_TESTED, tested_detail, pnl_pct)

    # 6. the OVERLAY ROLL, and only the overlay roll. "Is the short leg's expiry upon us?"
    #    has two answers, and this module now owns exactly one of them: on a DECLARED
    #    two-expiry structure the short is an overlay over a long that outlives it, so the
    #    answer is ROLL THE OVERLAY -- not a close, and not something any ruleset condition
    #    can ask, which is why it is asked here.
    #
    #    The single-expiry answer (the expiry IS the end of the position, so close) was
    #    removed on 2026-09-03: it duplicated the ``opt_dte`` rule the GA already searches,
    #    giving live an unsearched second exit the backtest could not model. The rule owns
    #    it now, in both runtimes. A single-expiry structure at its expiry falls through to
    #    the blind check and then to HOLD -- "nothing THIS module owns fired" -- and the
    #    ruleset closes it. ``dte_blind`` is still collected, because an unmeasurable roll
    #    window is still worth reporting.
    roll_dte = int(_require(settings, "roll_dte"))
    dte, dte_blind = roll_window_dte(structure, as_of)
    if is_multi_expiry_strategy(structure.strategy):
        buyback = (settings[SETTING_PMCC_BUYBACK_PCT]
                   if SETTING_PMCC_BUYBACK_PCT in settings else None)
        due, roll_detail, dte_blind = pmcc_roll_due(
            structure, chain_by_symbol, as_of, roll_dte=roll_dte, buyback_pct=buyback)
        if due:
            return LifecycleDecision(txn, LIFECYCLE_ROLL_SHORT, roll_detail, pnl_pct)

    # 7. nothing fired. If anything we needed was unmeasurable, say so -- loudly, and
    #    naming the input. A hold here would be a guess wearing a decision's clothes.
    #    Cover leads the list: it is the only input here whose absence can hide a NAKED
    #    short call, so it is the sentence an operator has to read first.
    blind = [b for b in (cover_blind, pnl_blind, tested_blind, dte_blind) if b]
    if blind:
        return LifecycleDecision(txn, LIFECYCLE_UNKNOWN, "; ".join(blind), pnl_pct)

    # 8. genuinely healthy, and every input was measurable.
    return LifecycleDecision(
        txn, LIFECYCLE_HOLD,
        f"P&L {pnl_pct:.2f}%, {dte} DTE — no exit rule fired", pnl_pct)
