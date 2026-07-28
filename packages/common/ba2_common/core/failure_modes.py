"""Tell a DEFECT apart from an expected data-absence, so defects fail loud.

WHY THIS EXISTS (2026-07-28). ``position_sizing.get_latest_atr`` wrapped its indicator call in
``except Exception -> logger.warning -> return None``. A tz naive/aware ``TypeError`` — a plain
code defect — was therefore indistinguishable from "this symbol has no ATR data", which is a
legitimate, expected outcome the caller handles by falling back to ``min_stop_loss_pct``.

Result: ATR was dead for MONTHS across every classic-expert optimization, ``use_atr_stop`` /
``atr_multiplier`` / ``atr_period`` were inert GA genes, and NOTHING in any log said so (GA pool
workers run at ``logging.disable(ERROR)``). The bug was found only by accident, while profiling
something else. See [[reference-atr-tz-bug-invalidated-optimizations]].

The rule this encodes: **a broad ``except`` may absorb the world being uncooperative; it must
NOT absorb the code being wrong.** An empty API response, a timeout, a missing file are data
conditions. A ``TypeError`` is a bug, and a bug that returns a plausible-looking ``None`` is far
more expensive than one that crashes — the crash is found in minutes, the silent fallback took
months and invalidated a fleet of optimizations.

Usage — call it FIRST inside a broad handler::

    try:
        ...
    except Exception as e:
        raise_if_defect(e)              # TypeError/AttributeError/... propagate
        logger.warning("no data for %s: %s", symbol, e)
        return None
"""
from __future__ import annotations

# Exception types that essentially always mean "the code is wrong", not "the data is missing":
#   TypeError        — wrong types reaching an operation (the ATR tz bug)
#   AttributeError   — None/wrong object where a real one was expected
#   NameError        — typo / missing binding
#   ImportError      — broken dependency or packaging
#   IndentationError / SyntaxError — malformed code reached at import/exec time
#   ZeroDivisionError— arithmetic the caller failed to guard
#
# DELIBERATELY EXCLUDED, because each has a common legitimate data-shaped cause:
#   KeyError / IndexError — routinely mean "field absent from an API payload"
#   ValueError            — routinely means "unparseable value from a feed"
#   OSError / IOError     — network + filesystem
# Adding those would make this fire on ordinary bad-data paths and train people to bypass it.
_DEFECTS: tuple = (
    TypeError,
    AttributeError,
    NameError,
    ImportError,
    IndentationError,
    SyntaxError,
    ZeroDivisionError,
)


def is_defect(exc: BaseException) -> bool:
    """True when *exc* indicates a code defect rather than an unhelpful world."""
    return isinstance(exc, _DEFECTS)


def raise_if_defect(exc: BaseException) -> None:
    """Re-raise *exc* when it is a code defect; return quietly otherwise.

    Preserves the original traceback: a bare ``raise`` inside the active handler re-raises the
    exception being handled, so the stack still points at the real failure site rather than at
    this function.
    """
    if is_defect(exc):
        raise exc
