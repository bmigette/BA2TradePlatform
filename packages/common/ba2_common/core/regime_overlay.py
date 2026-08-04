"""Regime scaling overlay: turn ``market_regime`` into multipliers the strategy can apply.

``market_regime`` classifies the benchmark; this module is the thin layer that decides WHAT a
stressed market should change. Kept separate so the classifier stays a pure, testable function
with no knowledge of orders or settings.

WHAT IT SCALES (all 1.0 = exact no-op, all active ONLY while the benchmark is stressed):

    regime_risk_scale   position SIZE      (risk_per_trade_pct)
    regime_stop_scale   stop-loss DISTANCE
    regime_tp_scale     take-profit DISTANCE

WHY TAKE-PROFIT MATTERS MOST HERE. The stop is already volatility-aware per symbol (ATR), but
the target is a fixed percent offset -- so reward:risk silently drifts with each symbol's
volatility and the GA can only pick one compromise TP%. That asymmetry is the defect this was
written for. It is also the cleanest of the three to implement: unlike the stop, a take-profit
has no tighter-wins precedence that could discard a widened value, and no ATR path it could be
redundant with.

DELTA MULTIPLIER, NOT REPLACEMENT. The scale multiplies the percent OFFSET a rule already
specifies; it never replaces it. "TP at +10%" becomes +15% at scale 1.5 in a stressed market and
stays +10% otherwise. Every persisted ruleset and genome therefore keeps working untouched --
absent settings resolve to 1.0 and the arithmetic is bit-for-bit what it is today.

TWO-SIDED RANGE (0.5-2.0) ON PURPOSE. A de-risk-only range would presuppose that stress means
"take less". Two-sided lets the GA express the opposite -- widen into stress -- so the gene tests
the hypothesis instead of encoding it. See
docs/plans/2026-07-29-regime-risk-scaling-overlay.md.

CACHING IS THE CALLER'S JOB. ``is_stressed`` walks a 504-bar rank window; the regime is
market-wide and does NOT vary per symbol, so the caller must resolve it ONCE PER BAR and pass the
result in. Computing it inside a per-symbol loop is the mistake that made still-held
O(feed x symbols x bars) on 2026-07-28.
"""
import bisect
from datetime import date, datetime
from typing import Any, List, Optional, Sequence, Tuple

# Neutral value for every scale. Inside the GA range, so `enabled=1, scale=1.0` must score
# identically to `enabled=0` -- that equality doubles as a leak check (see the design doc).
NEUTRAL_SCALE = 1.0

_SCALE_SETTINGS = ("regime_risk_scale", "regime_stop_scale", "regime_tp_scale")

# ---------------------------------------------------------------------------------------------
# Per-bar regime state (the "computed once, read many" seam).
#
# The regime is MARKET-WIDE: it does not vary per symbol, and classifying it walks a 504-bar rank
# window. The consumers, however, are spread across objects that never meet -- TradeRiskManagement
# (sizing/stop) and TradeActions (take-profit) -- so threading a value through every call chain
# would touch a lot of signatures for one scalar.
#
# Instead the HOST resolves it once per bar and publishes it here:
#   backtest -> the daily engine, at the top of each bar, from the SIMULATED clock
#   live     -> TradeManager, on each refresh
#
# Process-global is sound precisely BECAUSE the value is global: one market, one regime, and each
# GA trial runs in its own process. Default None = "not classified" = neutral, so anything that
# forgets to publish degrades to today's behaviour rather than to a wrong multiplier.
# ---------------------------------------------------------------------------------------------
_STRESSED_NOW: Optional[bool] = None


def set_stressed(value: Optional[bool]) -> None:
    """Publish this bar's regime. ``None`` means unclassified (insufficient benchmark history)."""
    global _STRESSED_NOW
    _STRESSED_NOW = None if value is None else bool(value)


def get_stressed() -> Optional[bool]:
    """This bar's published regime, or None when nothing has been published."""
    return _STRESSED_NOW


def reset_stressed() -> None:
    """Clear the published regime. Call between runs so one trial cannot inherit another's bar."""
    global _STRESSED_NOW
    _STRESSED_NOW = None


def _setting(expert, name: str, default: float) -> float:
    """Read a numeric setting off an expert, tolerating absence (old genomes have no such key)."""
    if expert is None:
        return default
    try:
        raw = expert.get_setting_with_interface_default(name, log_warning=False)
    except Exception:  # noqa: BLE001 -- a missing/!unreadable setting must degrade to neutral
        return default
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def overlay_enabled(expert) -> bool:
    """True only when the genome explicitly turned the overlay on. Absent -> False, so every
    pre-existing expert/genome behaves exactly as before."""
    if expert is None:
        return False
    try:
        return bool(expert.get_setting_with_interface_default(
            "regime_overlay_enabled", log_warning=False))
    except Exception:  # noqa: BLE001
        return False


def regime_scale(expert, setting_name: str, is_stressed_now: Optional[bool]) -> float:
    """The multiplier to apply for ``setting_name`` right now.

    Returns NEUTRAL_SCALE (1.0, an exact no-op) unless ALL of:
      * the overlay is enabled on this expert,
      * ``is_stressed_now`` is exactly True -- ``None`` means the regime could not be classified
        (insufficient benchmark history) and is treated as NOT stressed, matching
        ``market_regime.is_stressed``'s own fail-closed contract: an unclassifiable market must
        not silently trigger scaling,
      * the setting resolves to a usable number.
    """
    if setting_name not in _SCALE_SETTINGS:
        raise ValueError(f"unknown regime scale setting: {setting_name!r}")
    if is_stressed_now is not True:
        return NEUTRAL_SCALE
    if not overlay_enabled(expert):
        return NEUTRAL_SCALE
    return _setting(expert, setting_name, NEUTRAL_SCALE)


def _as_date(value: Any) -> date:
    """Coerce a bar key (date / datetime / ISO string) to a plain date for calendar lookup."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:19]).date()


class StressedCalendar:
    """``bar date -> is the market stressed?``, precomputed once for a whole run.

    WHY PRECOMPUTE. ``market_regime.classify_volatility_regime`` recomputes a 504-long realized-vol
    series on every call, each element a 20-return window -- ~10k float ops. Calling it per bar
    costs seconds per trial on a daily clock and is unusable on a 5-minute one, where the answer
    would be recomputed ~78x per day for a value that only changes daily. Building the vol series
    ONCE and ranking each day within its own trailing window gives the identical answer for
    O(days x lookback) total (sub-second over 2020-2026).

    "Identical" is load-bearing, not an aspiration: this reimplements the classifier's arithmetic
    for speed, so ``tests/test_regime_overlay.py`` asserts the calendar equals
    ``market_regime.is_stressed(closes[:i+1])`` for EVERY i. Change one and that test fails.

    CAUSAL BY CONSTRUCTION. Day *i* is classified from ``closes[:i+1]`` only. Feeding the full
    series would be lookahead -- the classifier's own docstring warns about exactly this.
    """

    __slots__ = ("_dates", "_flags")

    def __init__(self, dates: Sequence[Any], closes: Sequence[Optional[float]]):
        self._dates, self._flags = self._build(dates, closes)

    @staticmethod
    def _build(dates: Sequence[Any], closes: Sequence[Optional[float]]
               ) -> Tuple[List[date], List[bool]]:
        from ba2_common.core import market_regime as mr

        window, lookback = mr._VOL_WINDOW, mr._VOL_RANK_LOOKBACK
        vals: List[float] = []
        keys: List[date] = []
        for d, c in zip(dates, closes):
            if c is None:
                continue                      # mirrors the classifier's own None filtering
            vals.append(float(c))
            keys.append(_as_date(d))

        # rv[i] = realized vol of the window ENDING at day i (None until enough history), which
        # is exactly mr._realized_vol(vals[:i+1], window).
        rv: List[Optional[float]] = [mr._realized_vol(vals[:i + 1], window) for i in range(len(vals))]

        flags: List[bool] = []
        for i in range(len(vals)):
            # The classifier needs window+lookback closes before it will answer at all; below
            # that it returns regime=None, which is_stressed reports as False (fail closed).
            if i + 1 < window + lookback:
                flags.append(False)
                continue
            # Its rank series is the rv values for ends in [len-lookback, len] -- i.e. the
            # lookback+1 windows ending at days i-lookback .. i -- with the Nones dropped.
            series = [v for v in rv[i - lookback:i + 1] if v is not None]
            if len(series) < 2:
                flags.append(False)
                continue
            current = series[-1]
            if max(series) - min(series) <= 0:
                flags.append(False)           # no dispersion -> NORMAL, not stressed
                continue
            rank = sum(1 for v in series if v <= current) / len(series) * 100.0
            flags.append(rank > mr._STRESSED_PCTL)
        return keys, flags

    def at(self, as_of: Any) -> Optional[bool]:
        """Stress flag for the most recent benchmark day at or before ``as_of``.

        ``None`` when ``as_of`` precedes every benchmark day (nothing to classify from) -- which
        ``regime_scale`` treats as neutral, never as stressed.
        """
        if not self._dates:
            return None
        key = _as_date(as_of)
        idx = bisect.bisect_right(self._dates, key) - 1
        return self._flags[idx] if idx >= 0 else None


def scale_percent(base_percent: Optional[float], scale: float) -> Optional[float]:
    """Apply a regime scale to a percent OFFSET, preserving sign and None.

    Sign is preserved rather than assumed positive because the same offset convention carries
    negative values (an entry stop is written as e.g. -9.0). Multiplying by a positive scale
    keeps the direction and only changes the DISTANCE, which is the whole intent.
    """
    if base_percent is None:
        return None
    return float(base_percent) * float(scale)
