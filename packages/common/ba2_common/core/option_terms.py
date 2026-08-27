"""The finite option TERM vocabulary and its days-to-expiry windows.

A term is what a rule REQUESTS ("one month"); the window is what the selector filters expiries
with. Keeping the vocabulary finite and central is what turns term into a single categorical
gene. The ``dte_min``/``dte_max`` pair it replaces is two correlated integers that can express an
inverted, unsatisfiable window — ``TradeActions._expiry_window`` exists largely to raise for
exactly that case.

THE WINDOWS ARE HARD. Nothing here or downstream widens one: a term whose window contains no
selectable expiry is a refusal with a reason, never a silent substitution. Widening would make
the gene partly meaningless — a GA result could not distinguish "ONE_MONTH worked" from
"ONE_MONTH quietly became TWO_MONTHS half the time".
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, Tuple


class OptionTerm(str, Enum):
    """A requested holding term. The VALUES are the wire format in rule-action JSON.

    NB for anyone later persisting this on a model: this codebase stores str-enums by NAME,
    not value, so a migration would have to backfill ``"ONE_MONTH"``, not ``"1m"``.
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
#: The gaps between windows (19-20, 116-149, 211-299) are DELIBERATE. Two adjacent terms that
#: shared a boundary could select the same expiry on a chain with sparse expiries, collapsing two
#: distinct gene values into one behaviour — the GA would then spend budget distinguishing
#: options that are not different. ONE_MONTH is 21-45 because that contains the 25-45 window
#: every strategy in ``ba2test_launcher`` currently defaults to.
_WINDOWS: Dict["OptionTerm", Tuple[int, int]] = {
    OptionTerm.ZERO_DTE: (0, 0),
    OptionTerm.ONE_WEEK: (1, 9),
    OptionTerm.TWO_WEEKS: (10, 18),
    OptionTerm.ONE_MONTH: (21, 45),
    OptionTerm.TWO_MONTHS: (46, 75),
    OptionTerm.THREE_MONTHS: (76, 115),
    OptionTerm.SIX_MONTHS: (150, 210),
    OptionTerm.LEAPS: (300, 450),
}


def dte_window(term) -> Tuple[int, int]:
    """The INCLUSIVE ``(dte_min, dte_max)`` window for ``term``.

    Accepts an ``OptionTerm`` or its string value (rule-action JSON carries the string).

    Raises ``ValueError`` for anything else. Returning a default window instead would silently
    trade a term nobody asked for, which is the worst available outcome: the backtest would
    report results for a strategy that was never configured.
    """
    valid = [t.value for t in OptionTerm]
    # OptionTerm is a str subclass, so plain isinstance(term, str) is True for members too.
    if isinstance(term, str) and not isinstance(term, OptionTerm):
        try:
            term = OptionTerm(term)
        except ValueError:
            raise ValueError(
                f"Unknown option term {term!r}; expected one of {valid}") from None
    try:
        return _WINDOWS[term]
    except (KeyError, TypeError):
        raise ValueError(
            f"Unknown option term {term!r}; expected one of {valid}") from None
