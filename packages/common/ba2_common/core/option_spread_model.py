"""The option bid-ask spread a BACKTEST fill is charged -- one pure, versioned definition.

WHY THIS EXISTS (plan Part F, measured 2026-09-22)
--------------------------------------------------
The simulator used to charge ``max(0.02, 5% x premium)`` full width, doubled when the FILL
day's volume was under 100 contracts. Measured against ThetaData EOD NBBO (97 symbols,
2020-2025, 28.7M quotes) that shape is wrong in both tails: real spreads scale roughly as
``premium ** 0.6``, so the old model was 0.3-0.7x too cheap under $2 of premium (exactly where
fabricated edge concentrates) and 1.4-3x too expensive above $10. The thin doubling also read
the fill day's full-session volume -- a small look-ahead.

THE RULE (F1 + F2), in priority order:

1. **The as-of real quote.** When the DECISION bar -- the as-of bar of the fill ATTEMPT,
   i.e. the bar before the one the order fills on (for an order that retries after not
   filling, the bar before THAT attempt, not the bar it was first placed on) -- carries a valid
   NBBO (``bid > 0`` and ``ask > bid``), the half spread is ``(ask - bid) / 2`` from that bar
   (floored at half a tick). Causal: that closing quote is what the decision saw. Validated as
   a predictor of the next day's real spread: median ratio 1.00, total 0.99.
2. **The calibrated fallback.** Otherwise ``full_spread(premium, volume)`` below, using the
   AS-OF bar's premium and volume -- a power law fitted on 2020-2023 and validated out of
   sample on 2024-2025 (median |log error| 0.696, median model/actual 1.09):

       full = max(0.01, 0.2301 * premium ** 0.6035 * max(volume, 1) ** -0.1357)

   (``0.2301 = exp(-1.4695)`` and ``-0.1357 = -0.3124 / ln 10``: the fit's intercept and its
   ``log10(volume)`` slope, written in natural form.) A missing volume is treated as 1 -- the
   widest the model goes -- because a row with no traded volume is by definition thin.

Pure and dependency-free on purpose: the backtest account charges it, and a live account may
later call it to record what the backtest WOULD have charged beside the real fill (a comparison
record only -- live always crosses its real quote and never charges this).

``SPREAD_MODEL_VERSION`` names this definition. It is recorded in a run's config and results
and folded into the option grid's job identity, so a result priced by a different spread model
can never be mistaken for, or resume from, one priced by this one. Change ANY number here and
the version string must change with it.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Tuple

#: The name of the definition below. Bump it with ANY change to the constants or the rule.
SPREAD_MODEL_VERSION = "pow-2026-09-22"

#: The pre-2026-09-22 formula (``max(min_tick, pct% x premium)``, doubled when the fill bar's
#: volume is under 100). Still selectable, but only EXPLICITLY (``--option-spread-pct`` /
#: ``--option-spread-min-tick``), and it is what a run config WITHOUT a model key reproduces --
#: every result stored before this model existed.
LEGACY_PCT_MODEL = "legacy-pct"

#: Every value a run config's ``option_spread_model`` may carry.
SPREAD_MODELS = (SPREAD_MODEL_VERSION, LEGACY_PCT_MODEL)

_COEF = 0.2301
_PREMIUM_EXP = 0.6035
_VOLUME_EXP = -0.1357
#: Floor on the FULL modelled width: one cent, the minimum option tick.
_MIN_FULL_SPREAD = 0.01

#: Where a half spread came from -- the two answers ``as_of_half_spread`` can give.
SOURCE_QUOTE = "quote"
SOURCE_MODEL = "model"


def _finite(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def full_spread(premium: float, volume: Optional[float]) -> float:
    """The calibrated FULL bid-ask width, in premium dollars per share (>= 0.01).

    ``premium`` is taken in absolute value (a short's negative sign is not a price);
    ``volume`` None / NaN / below 1 is treated as 1 (the widest case: an untraded row is thin).
    """
    p = _finite(premium)
    if p is None:
        raise ValueError(f"option spread model needs a finite premium, got {premium!r}")
    v = _finite(volume)
    v = 1.0 if v is None or v < 1.0 else v
    return max(_MIN_FULL_SPREAD, _COEF * abs(p) ** _PREMIUM_EXP * v ** _VOLUME_EXP)


def half_spread(premium: float, volume: Optional[float]) -> float:
    """Half of ``full_spread`` -- what one fill pays, in the adverse direction."""
    return full_spread(premium, volume) / 2.0


def quoted_half_spread(bid: Any, ask: Any) -> Optional[float]:
    """Half the quoted spread for a VALID quote (both finite, ``bid > 0``, ``ask > bid``), else
    None -- floored at half the one-tick minimum (``_MIN_FULL_SPREAD / 2``).

    ``bid > 0``: a zero bid is a one-sided market (nobody is buying), whose "spread" is the
    whole ask and says nothing about what a fill would cross. ``ask > bid``: a LOCKED quote
    (``ask == bid``) is refused as well as a crossed one -- in real NBBO it is rare, but it is
    exactly what a close-proxy store writes (``bid = ask = close``), and accepting it would
    charge a zero spread. Both go to the calibrated fallback instead. The floor keeps a
    sub-tick quoted spread (a data artefact below the $0.01 option tick) from undercutting the
    model's own minimum.
    """
    b, a = _finite(bid), _finite(ask)
    if b is None or a is None or b <= 0.0 or a <= b:
        return None
    return max(a - b, _MIN_FULL_SPREAD) / 2.0


def as_of_half_spread(as_of_bar: Optional[Mapping[str, Any]],
                      premium: float) -> Tuple[float, str]:
    """The half spread a fill decided on ``as_of_bar`` is charged, and where it came from.

    ``as_of_bar`` is the contract's bar on the DECISION day (``None`` when it has none);
    ``premium`` is used only when that bar has no close to model from (the price being filled,
    which the fill observes anyway). Returns ``(half, SOURCE_QUOTE | SOURCE_MODEL)``.

    Reads ONLY ``bid``, ``ask``, ``close`` and ``volume`` of the as-of bar -- never anything
    from the fill day, which is what makes the charge causal.
    """
    if as_of_bar:
        q = quoted_half_spread(as_of_bar.get("bid"), as_of_bar.get("ask"))
        if q is not None:
            return q, SOURCE_QUOTE
        close = _finite(as_of_bar.get("close"))
        return (half_spread(close if close is not None else premium, as_of_bar.get("volume")),
                SOURCE_MODEL)
    return half_spread(premium, None), SOURCE_MODEL
