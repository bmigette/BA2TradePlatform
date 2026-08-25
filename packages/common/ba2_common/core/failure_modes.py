"""Broad exception handlers fail LOUD by default; only explicitly-named benign errors absorb.

WHY THIS EXISTS (2026-07-28). ``position_sizing.get_latest_atr`` wrapped its indicator call in
``except Exception -> logger.warning -> return None``. A tz naive/aware ``TypeError`` — a plain
code defect — was therefore indistinguishable from "this symbol has no ATR data", a legitimate
outcome the caller handles by falling back to ``min_stop_loss_pct``. ATR was dead for MONTHS and
``use_atr_stop``/``atr_multiplier``/``atr_period`` were inert GA genes.

WHAT ACTUALLY HID IT — CORRECTED 2026-07-29. An earlier version of this docstring blamed log
suppression, claiming "GA pool workers run at ``logging.disable(ERROR)``". That is wrong twice
over, and the claim is repeated in several commit messages, so do not trust it there either:

  * the real call is ``logging.disable(logging.INFO)``
    (``strategy_optimization_handler.py``), which ALLOWS WARNING/ERROR/CRITICAL through — its own
    comment says "Floor is INFO so WARNING+ survive";
  * ``_worker_init`` does set ``BA2_FILE_LOGGING=0``/``BA2_STDOUT_LOGGING=0``, leaving a worker
    with no ba2 handlers — but Python's ``logging.lastResort`` then writes WARNING+ to stderr,
    which a grid captures via ``2>&1``. Verified empirically: an ERROR raised inside a
    worker-shaped process does surface (bare and unformatted — the lastResort signature).

So the warning was DELIVERED and nobody could act on it. What hid the bug was signal QUALITY:
``logger.warning(...) + return None`` is indistinguishable from the legitimate "this symbol has
no ATR data", so there was nothing to distinguish a defect from routine missing data. That is
precisely the confusion this module exists to remove — and it is why the fix is to make the
caller SAY what it expects, not to shout louder.

See [[reference-atr-tz-bug-invalidated-optimizations]].

DENY BY DEFAULT. The first version of this module inverted the test: it re-raised a fixed list
of "defect" types (TypeError, AttributeError, ...) and absorbed everything else. That is
allow-by-default, and it leaks exactly the bugs that wear a data-shaped exception — a typo'd
settings key raises ``KeyError``, a bad enum string raises ``ValueError``, and both would have
been swallowed just like the ATR TypeError was. Classification by type cannot tell
``settings["max_stopp_loss"]`` (a typo) from ``payload["close"]`` (a genuinely absent field),
so the safe default is to propagate and make the caller SAY what it expects.

    except Exception as e:
        absorb_if_benign(e)                     # anything unexpected propagates
        logger.error(...); return None

    except Exception as e:
        absorb_if_benign(e, KeyError)           # THIS site legitimately sees absent fields
        logger.error(...); return None

Naming the expectation at each site is the real fix — the same discipline as writing
``except (OSError, KeyError):`` in the first place, but retrofittable to handlers nobody can
currently characterise.

MODES (env ``BA2_ERROR_MODE``), because flipping ~95 handlers from absorb-all to
propagate-unless-named is a real behaviour change in LIVE trading:

    enforce   propagate — the intended end state, and the CURRENT DEFAULT
    observe   log what WOULD have propagated, then absorb  (the staged-rollout mode)
    legacy    absorb everything, no logging — escape hatch only

ENFORCE SINCE 2026-07-28, on measurement rather than on a guess. The rollout ran in ``observe``
first, which was the right call: turning it on immediately had surfaced 5 option tests that pass
in isolation but fail in a full-suite run — pre-existing test-order pollution the old swallow was
hiding. The flip happened only once all of the following held:

  * the full suite (1072 tests) passed under ``enforce``;
  * a real Senate S3 trial (498 symbols, 3 months, 5min) absorbed NOTHING in observe mode — and
    a deliberate canary confirmed the WOULD-RAISE line does reach that run's log, because an
    empty result proves nothing on its own (exactly how the ATR tz bug survived for months);
  * the one genuine data condition found — ``InstanceNotFound`` when a Ruleset is deleted while
    a recommendation still references it — was NAMED at its call site rather than absorbed by
    the broad catch.

    set BA2_ERROR_MODE=observe      # to re-measure after adding handlers
    set BA2_ERROR_MODE=legacy       # escape hatch if enforce ever breaks live unexpectedly

To calibrate new sites: run in ``observe``, then ``python tools/summarize_would_raise.py <logs>``
for a per-site table. Name the genuine data conditions, fix the rest, return to ``enforce``.
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

_MODE_ENV = "BA2_ERROR_MODE"

# Genuinely benign EVERYWHERE: the world being uncooperative, never a statement about this
# program's correctness. OSError covers ConnectionError/TimeoutError/FileNotFoundError and the
# rest of the network+filesystem family.
#
# Deliberately NOT here: KeyError, IndexError, ValueError. Each is a COMMON data condition AND a
# common bug, and only the call site knows which it is facing — that is precisely the judgement
# this module refuses to make on the caller's behalf. Pass them per-site when they are expected.
_BENIGN_DEFAULT: tuple = (OSError,)


def _mode() -> str:
    return (os.environ.get(_MODE_ENV) or "enforce").strip().lower()


def _caller_site() -> str:
    """``file.py:lineno`` of the handler that called absorb_if_benign (2 frames up)."""
    try:
        # frame 0 = _caller_site, 1 = absorb_if_benign, 2 = the handler that called it
        f = sys._getframe(2)
        return f"{os.path.basename(f.f_code.co_filename)}:{f.f_lineno}"
    except Exception:                 # noqa: BLE001 — diagnostics must never fail the caller
        return "?"


def _raise_site(exc: BaseException) -> str:
    """``file.py:lineno`` of the deepest frame in the exception's own traceback."""
    try:
        tb = exc.__traceback__
        last = None
        while tb:
            last = tb
            tb = tb.tb_next
        if last is None:
            return "?"
        return (f"{os.path.basename(last.tb_frame.f_code.co_filename)}:{last.tb_lineno}")
    except Exception:                 # noqa: BLE001
        return "?"


def is_benign(exc: BaseException, *also_benign: type) -> bool:
    """True when *exc* is one of the globally-benign types, or one named by this call site."""
    return isinstance(exc, _BENIGN_DEFAULT + tuple(also_benign))


def absorb_if_benign(exc: BaseException, *also_benign: type) -> None:
    """Return quietly when *exc* is benign; otherwise propagate it (mode-dependent).

    Re-raises the exception being handled, so the ORIGINAL traceback survives and points at the
    real failure site rather than at this function.
    """
    if is_benign(exc, *also_benign):
        return
    mode = _mode()
    if mode == "legacy":
        return
    if mode == "observe":
        # One grep-able marker carrying BOTH locations, because the measurement is only useful
        # if it says which site to adjust:
        #   at=   the handler that would have propagated  -> the line to add a benign type to
        #   from= where the exception was actually raised -> what it really is
        # Without these a grid produces thousands of "WOULD-RAISE KeyError" lines that cannot be
        # attributed to anything.
        logger.error("WOULD-RAISE %s at=%s from=%s: %s",
                     type(exc).__name__, _caller_site(), _raise_site(exc), exc)
        return
    raise exc


# --------------------------------------------------------------------------- #
# superseded
# --------------------------------------------------------------------------- #
_DEFECTS: tuple = (
    TypeError, AttributeError, NameError, ImportError,
    IndentationError, SyntaxError, ZeroDivisionError,
)


def is_defect(exc: BaseException) -> bool:
    """Allow-by-default classifier. Superseded by :func:`is_benign` — see the module docstring
    for why type-based defect detection leaks bugs that wear a data-shaped exception."""
    return isinstance(exc, _DEFECTS)


# --------------------------------------------------------------------------- #
# Unknown reads as zero
#
# The same failure the rest of this module addresses, one layer down: not "a defect wearing a
# data-shaped exception" but "an absent number wearing a measured one". A 2026-08 audit found 25
# sites where a value nobody measured became ``0.0`` and was then read as an answer — an unknown
# option reserve FREEING buying power, ``float(o.strike or 0.0)`` making an unknown strike free
# money, a peak equity of ``0.0`` disabling a drawdown breaker, a 429 rejection reading as "this
# symbol has no data".
#
# A typed ``Measured[T]``/``Unknown`` sum type was weighed and rejected: SQLModel columns cannot
# hold one, so every DB boundary would unwrap — which is exactly where the coercions already are.
# ``or 0.0`` would become ``.unwrap_or(0.0)``.
#
# What is left is ergonomics. ``or 0.0`` keeps reappearing because it is the shortest thing to
# type. So the correct thing is made shorter:
#
#     px = must_measure(quote.last, f"{symbol} last price")      # 1 call, raises if absent
#     px = float(quote.last or 0.0)                              # 1 call, silently wrong
#
# and, for a caller that legitimately cannot measure and must say so rather than answer:
#
#     return unmeasured("HTTP 429 from the quote endpoint")
#
# ``tests/test_no_zero_coercion.py`` is the lint rule that finds the coercions; this is the
# replacement it points people at.
# --------------------------------------------------------------------------- #

class UnmeasuredValue(ValueError):
    """A number was required and the value supplied was not one that had been measured."""


class Unmeasured:
    """The tri-state absence: "I could not measure this", carrying WHY.

    Deliberately NOT a number and NOT ``None``. ``None`` is already overloaded across this
    codebase ("absent", "not applicable", "not yet computed"), and a float — of any value —
    can be summed into a total by accident. This can only be tested for, printed, or passed
    to :func:`must_measure`, which will name the reason in the raise.

    Falsy, so ``if not value:`` guards keep working; un-addable, so a caller that forgets the
    guard fails at the arithmetic rather than three screens later in a P&L column.
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"Unmeasured({self.reason!r})"

    def __str__(self) -> str:
        return f"unmeasured: {self.reason}"


def unmeasured(reason: str) -> Unmeasured:
    """Build the tri-state absence for a caller that legitimately could not measure."""
    return Unmeasured(reason)


def must_measure(value, what: str) -> float:
    """Return *value* as a float, or raise :class:`UnmeasuredValue` naming *what* was unknown.

    Accepts ``str``/``Decimal`` because broker payloads arrive that way and a helper that made
    you coerce first would lose to ``or 0.0`` on keystrokes. Rejects ``None``, NaN, infinity,
    an :class:`Unmeasured`, and anything non-numeric.

    A measured ``0.0`` passes. That is the point: zero is an ANSWER (a scratch trade's P&L, a
    flat book's quantity, a genuinely unreserved debit) and conflating it with absence is the
    bug this exists to stop, not a shortcut it may take.
    """
    if isinstance(value, Unmeasured):
        raise UnmeasuredValue(f"{what} is unmeasured: {value.reason}")
    if value is None:
        raise UnmeasuredValue(f"{what} is unmeasured: value is None")
    if isinstance(value, bool):
        raise UnmeasuredValue(f"{what} is unmeasured: got bool {value!r}, not a measurement")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise UnmeasuredValue(
            f"{what} is unmeasured: {value!r} ({type(value).__name__}) is not a number"
        ) from None
    if out != out:                       # NaN
        raise UnmeasuredValue(f"{what} is unmeasured: value is NaN")
    if out in (float("inf"), float("-inf")):
        raise UnmeasuredValue(f"{what} is unmeasured: value is {out}")
    return out


def raise_if_defect(exc: BaseException) -> None:
    """Deprecated: allow-by-default. Use :func:`absorb_if_benign`, which propagates unless the
    call site names the error as expected. Kept so an un-migrated caller still escalates the
    obvious defects rather than silently reverting to absorb-everything."""
    if is_defect(exc):
        raise exc
