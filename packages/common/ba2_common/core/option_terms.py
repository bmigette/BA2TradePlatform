"""The finite option TERM vocabulary and its days-to-expiry windows.

A term is what a rule REQUESTS ("one month"); the window is what the selector filters expiries
with.

WHAT THE TERM GENE ACTUALLY REPLACES. Not "two integers" — the GA already emits only ONE DTE
gene. ``strategy_param_space.py`` exposes ``option_dte`` as a window CENTRE and
``_apply_option_dte`` decodes it as ``dte_min = max(0, centre - hw)``, ``dte_max = centre + hw``
with a half-width of at least 7 days, so an inverted window is structurally unreachable on the
GA path. The win is different and smaller than "one gene instead of two": a CATEGORICAL choice
among eight named terms, instead of a continuous centre carrying an implicit +/-hw span that
nobody reads off the genome. Invertibility is still a real hazard for HAND-AUTHORED rule JSON
and the settings UI, which is what ``TradeActions._expiry_window`` guards — but that is not the
GA, and this docstring used to conflate the two.

THE WINDOWS ARE HARD. Nothing here or downstream widens one: a term whose window contains no
selectable expiry is a refusal with a reason, never a silent substitution. Widening would make
the gene partly meaningless — a GA result could not distinguish "ONE_MONTH worked" from
"ONE_MONTH quietly became TWO_MONTHS half the time".

KNOWN LIMIT, AND IT BITES THE THREE LONGEST TERMS. The backtest option cache only fetches
contracts expiring within ``fetch_options._EXPIRY_TAIL_DAYS`` (60) days of the run's end date,
so from a bar at date ``d`` the largest DTE any historical chain can contain is
``run_end + 60 - d``. Under hard windows that makes ``THREE_MONTHS``, ``SIX_MONTHS`` and
``LEAPS`` refuse outright near the end of every backtest, and ``LEAPS`` unusable in any run
shorter than about fifteen months. A GA would then learn "LEAPS is bad" for a data-availability
reason with no economic content — the same failure the hard-window rule exists to prevent,
arriving through the other door. Before these terms are searched, either raise
``_EXPIRY_TAIL_DAYS`` or restrict the gene's choice set to terms the fetch window can serve.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, Tuple


class OptionTerm(str, Enum):
    """A requested holding term. The VALUES are the wire format in rule-action JSON.

    NB for anyone later persisting this on a model: this codebase stores str-enums by NAME,
    not value, so a migration would have to backfill ``"ONE_MONTH"``, not ``"1m"``.

    Like every other ``(str, Enum)`` here (``OrderStatus``, ``OptionRight``), ``str()`` and
    f-string interpolation render ``"OptionTerm.ONE_MONTH"`` rather than ``"1m"``; use
    ``.value`` in user-facing text. A custom ``__str__`` would fix that for this one enum and
    make it inconsistent with every other one, which is worse.
    """

    ZERO_DTE = "0dte"
    ONE_WEEK = "1w"
    TWO_WEEKS = "2w"
    ONE_MONTH = "1m"
    TWO_MONTHS = "2m"
    THREE_MONTHS = "3m"
    SIX_MONTHS = "6m"
    LEAPS = "leaps"


#: term -> (dte_min, dte_max), both INCLUSIVE.
#:
#: THE WINDOWS ARE CONTIGUOUS: every DTE from 0 to 1095 belongs to exactly one term. An earlier
#: draft left deliberate gaps (19-20, 116-149, 211-299) on the theory that adjacent terms "could
#: otherwise select the same expiry". That reasoning was wrong twice over. Mechanically,
#: ``option_selector.filter_dte`` is inclusive membership and the windows are pairwise disjoint,
#: so no expiry can fall in two of them however tightly they abut — the table refuted itself,
#: since 9|10, 45|46 and 75|76 were already adjacent with no gap and collided with nothing.
#: Practically, the gaps made real DTEs unreachable by ANY term, and one of them was live:
#: ``O_STRD`` and ``O_STRG`` in ``ba2test_launcher`` default to 20-40, and a ONE_MONTH starting
#: at 21 could not express their floor. Under a design whose whole promise is that windows are
#: never silently widened, a DTE no term can name is the worst kind of hole.
#:
#: ONE_MONTH spans 20-45 so it contains BOTH live defaults: the 25-45 used by fifteen of the
#: seventeen option strategies, and the 20-40 used by the other two. Containment means no
#: existing default becomes inexpressible; it does NOT mean migration is a no-op, since a 25-45
#: rule moved onto ONE_MONTH also gains 20-24.
#:
#: Since ``option_selector``'s expiry tie-break prefers the EARLIEST in-window expiry, a term's
#: LOWER bound is its operative parameter and the upper bound acts as a guard. That is why the
#: long terms can be wide without becoming vague.
_WINDOWS: Dict[OptionTerm, Tuple[int, int]] = {
    OptionTerm.ZERO_DTE: (0, 0),
    OptionTerm.ONE_WEEK: (1, 9),
    OptionTerm.TWO_WEEKS: (10, 19),
    OptionTerm.ONE_MONTH: (20, 45),
    OptionTerm.TWO_MONTHS: (46, 75),
    OptionTerm.THREE_MONTHS: (76, 149),
    OptionTerm.SIX_MONTHS: (150, 269),
    # Named for the familiar long-dated bucket rather than the CBOE definition: this admits
    # 270-364 DTE, which are not strictly LEAPS, and stops at three years.
    OptionTerm.LEAPS: (270, 1095),
}

#: Built once. ``dte_window`` is called per candidate expiry per bar, and rebuilding this list
#: on the happy path is pure waste in a module whose whole job is a dict lookup.
_VALID_VALUES = [t.value for t in OptionTerm]


def dte_window(term: object) -> Tuple[int, int]:
    """The INCLUSIVE ``(dte_min, dte_max)`` window for ``term``.

    Accepts an ``OptionTerm`` or its string value (rule-action JSON carries the string). The
    parameter is annotated ``object`` because non-term input is an EXPECTED, handled case, not
    a type error to be assumed away.

    Raises ``ValueError`` for anything else, including ``None``, the integer ``0`` (which does
    NOT resolve to ``ZERO_DTE``) and unhashable values. Returning a default window instead would silently
    trade a term nobody asked for, which is the worst available outcome: the backtest would
    report results for a strategy that was never configured.
    """
    # OptionTerm is a str subclass, so plain isinstance(term, str) is True for members too.
    if isinstance(term, str) and not isinstance(term, OptionTerm):
        try:
            term = OptionTerm(term)
        except ValueError:
            raise ValueError(
                f"Unknown option term {term!r}; expected one of {_VALID_VALUES}") from None
    try:
        return _WINDOWS[term]
    except (KeyError, TypeError):
        # TypeError catches unhashable input (a list, a dict). Without it those escape as a raw
        # TypeError from the dict lookup instead of the ValueError every caller handles.
        raise ValueError(
            f"Unknown option term {term!r}; expected one of {_VALID_VALUES}") from None
