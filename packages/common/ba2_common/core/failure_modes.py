"""Broad exception handlers fail LOUD by default; only explicitly-named benign errors absorb.

WHY THIS EXISTS (2026-07-28). ``position_sizing.get_latest_atr`` wrapped its indicator call in
``except Exception -> logger.warning -> return None``. A tz naive/aware ``TypeError`` — a plain
code defect — was therefore indistinguishable from "this symbol has no ATR data", a legitimate
outcome the caller handles by falling back to ``min_stop_loss_pct``. ATR was dead for MONTHS,
``use_atr_stop``/``atr_multiplier``/``atr_period`` were inert GA genes, and nothing in any log
said so (GA pool workers run at ``logging.disable(ERROR)``). See
[[reference-atr-tz-bug-invalidated-optimizations]].

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

    observe   log what WOULD have propagated, then absorb  (CURRENT DEFAULT — staged rollout)
    enforce   propagate — the intended end state
    legacy    absorb everything, no logging — escape hatch only

DEFAULT IS ``observe`` ON PURPOSE, and this is a temporary staging decision, not the design.
Flipping ~95 handlers from absorb-all to propagate-unless-named is a real behaviour change in
LIVE trading, and turning it on immediately showed why: it surfaced 5 option tests that pass in
isolation but fail in a full-suite run, i.e. pre-existing test-order pollution that the old
swallow had been hiding. Those are worth fixing, but not by discovering them one production
incident at a time.

    set BA2_ERROR_MODE=enforce      # flip when the measurement says it is safe

Run a full grid in ``observe`` and grep for ``WOULD-RAISE``: that turns the benign set from a
guess into a measurement, per call site, before anything starts failing for real. Then flip.
"""
from __future__ import annotations

import logging
import os

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
    return (os.environ.get(_MODE_ENV) or "observe").strip().lower()


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
        # One grep-able marker so a full grid run yields the real per-site benign set.
        logger.error("WOULD-RAISE %s: %s", type(exc).__name__, exc, exc_info=True)
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


def raise_if_defect(exc: BaseException) -> None:
    """Deprecated: allow-by-default. Use :func:`absorb_if_benign`, which propagates unless the
    call site names the error as expected. Kept so an un-migrated caller still escalates the
    obvious defects rather than silently reverting to absorb-everything."""
    if is_defect(exc):
        raise exc
