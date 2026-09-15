"""Pure calculators for the option market-condition entry gates.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md``
§3 (measurements), §3.1 (exact numerical contract), §7 (failure statuses).

This module is PURE: numpy only, no I/O, no provider access, no logging, no
reference to any other project module.  It turns one validated window of
exactly ``WINDOW`` (128) consecutive regular-session daily OHLCV bars into
three observations:

* ``underlying_trend_slope_50_atr14`` -- ``(EMA50[127] - EMA50[122]) / (5 * ATR14[127])``
* ``underlying_adx_14``               -- ADX14[127]
* ``underlying_realized_vol_ratio_5_20`` -- sample std (ddof=1) of the last 5
  log returns divided by that of the last 20, not annualized.

Numerical contract (§3.1), window indexed 0..127, float64, unrounded:

* EMA50 is seeded at index 49 with the arithmetic mean of closes 0..49, then
  ``EMA[j] = (2/51)*C[j] + (49/51)*EMA[j-1]``.  It is NOT pandas
  ``ewm(adjust=False)``, which seeds from the first value.
* TR[j] (j >= 1) = max(H-L, |H-C[j-1]|, |L-C[j-1]|); TR[0] is undefined.
* ATR14 is seeded at index 14 with mean(TR[1..14]), then
  ``ATR[j] = (13*ATR[j-1] + TR[j]) / 14``.  +DM/-DM use the same seed/recurrence.
* +DI/-DI = 100 * smoothed DM / ATR at indices >= 14; ``DX = 100*|+DI - -DI| / (+DI + -DI)``,
  with DX = 0 when ATR > 0 and both DIs are exactly 0.  ADX is seeded at index
  27 with mean(DX[14..27]) and follows the Wilder recurrence to index 127.
* Seed means are ``np.mean`` over the float64 slice; the recurrences are
  explicit loops written exactly as above.

Status rules (an invalid observation never becomes 0, a neutral regime or a pass):

* Arrays shorter than ``WINDOW`` -> all three ``insufficient_history``.
  Longer -> ``ValueError``: callers must pre-slice, so window invariance is
  structural rather than a property of warm-up convergence.
* Any non-finite or non-positive o/h/l/c, ``h < max(o, c)``, ``l > min(o, c)``
  or ``h < l`` -> all three ``invalid_prices`` naming the offending index.
  Volume does not enter any v1 measurement; it is only checked to be finite
  and non-negative (failure is reported as ``invalid_prices`` too).
* ``ATR[127] <= 0`` -> slope and ADX ``invalid_prices`` ("atr<=0"); the RV
  ratio is still computed on its own terms.
* RV ratio: zero 20-session denominator -> ``invalid_prices``; a zero
  5-session numerator with a positive denominator is a valid ``0.0``.
  Never substituted by 1 or infinity.

Edge resolved toward UNKNOWN (the contract says "ATR <= 0 is unknown, not
zero" but only pins the check for index 127): if ATR is exactly 0 at some
index inside 14..127 (possible only as a leading run of identical OHLC bars,
since Wilder smoothing of non-negative TR cannot return to 0 once positive),
DX at that index is undefined, so the ADX seed/recurrence is undefined and
ADX is reported ``invalid_prices`` even when ``ATR[127] > 0``.  The slope is
unaffected because its only ATR input is ATR[127].

The ``missing_session``, ``no_context`` and ``missing_replay_object``
statuses are defined here for the shared vocabulary; they are produced by the
callers that assemble the window, not by these calculators.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

WINDOW = 128
CALC_VERSION = "ohlcv-v1/calc-1"

FIELD_TREND_SLOPE = "underlying_trend_slope_50_atr14"
FIELD_ADX = "underlying_adx_14"
FIELD_RV_RATIO = "underlying_realized_vol_ratio_5_20"
FIELDS = (FIELD_TREND_SLOPE, FIELD_ADX, FIELD_RV_RATIO)

STATUS_VALID = "valid"
STATUS_INSUFFICIENT_HISTORY = "insufficient_history"
STATUS_MISSING_SESSION = "missing_session"
STATUS_INVALID_PRICES = "invalid_prices"
STATUS_NO_CONTEXT = "no_context"
STATUS_MISSING_REPLAY_OBJECT = "missing_replay_object"
STATUSES = (
    STATUS_VALID,
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_MISSING_SESSION,
    STATUS_INVALID_PRICES,
    STATUS_NO_CONTEXT,
    STATUS_MISSING_REPLAY_OBJECT,
)

_ATR_PERIOD = 14
_EMA_PERIOD = 50
_ADX_SEED_INDEX = 2 * _ATR_PERIOD - 1  # 27
_RV_SHORT = 5
_RV_LONG = 20


@dataclass(frozen=True)
class Observation:
    """One measured field. Only ``valid`` carries a (finite) value."""

    value: Optional[float]
    status: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown observation status {self.status!r}")
        if self.status == STATUS_VALID:
            if self.value is None or not np.isfinite(self.value):
                raise ValueError(f"a valid observation needs a finite value, got {self.value!r}")
        elif self.value is not None:
            raise ValueError(f"a {self.status} observation must not carry a value ({self.value!r})")


@dataclass(frozen=True)
class MarketConditionValues:
    trend_slope: Observation
    adx: Observation
    rv_ratio: Observation
    calc_version: str = CALC_VERSION

    def by_field(self) -> Dict[str, Observation]:
        return {
            FIELD_TREND_SLOPE: self.trend_slope,
            FIELD_ADX: self.adx,
            FIELD_RV_RATIO: self.rv_ratio,
        }

    def as_row(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {}
        for field, obs in self.by_field().items():
            row[field] = obs.value
            row[f"{field}_status"] = obs.status
        row["calc_version"] = self.calc_version
        return row


def _f64(x) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def ema_wilder_seeded(c: np.ndarray, period: int = 50) -> np.ndarray:
    """EMA with alpha = 2/(period+1), seeded at index period-1 with mean(c[:period]).

    NaN before index period-1 (and everywhere if the series is too short).
    """
    c = _f64(c)
    n = len(c)
    out = np.full(n, np.nan)
    if n < period:
        return out
    k_new = 2.0 / (period + 1)
    k_old = (period - 1) / (period + 1)
    out[period - 1] = np.mean(c[:period])
    for j in range(period, n):
        out[j] = k_new * c[j] + k_old * out[j - 1]
    return out


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    """TR[j] = max(H-L, |H-C[j-1]|, |L-C[j-1]|) for j >= 1; NaN at index 0."""
    h, l, c = _f64(h), _f64(l), _f64(c)
    out = np.full(len(c), np.nan)
    if len(c) < 2:
        return out
    prev_c = c[:-1]
    out[1:] = np.maximum(np.maximum(h[1:] - l[1:], np.abs(h[1:] - prev_c)), np.abs(l[1:] - prev_c))
    return out


def _wilder14(x: np.ndarray) -> np.ndarray:
    """Seed at index 14 with mean(x[1..14]); ``(13*prev + x[j]) / 14`` after."""
    n = len(x)
    out = np.full(n, np.nan)
    if n <= _ATR_PERIOD:
        return out
    out[_ATR_PERIOD] = np.mean(x[1:_ATR_PERIOD + 1])
    for j in range(_ATR_PERIOD + 1, n):
        out[j] = (13.0 * out[j - 1] + x[j]) / 14.0
    return out


def atr14_wilder(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    """ATR14 per §3.1; NaN before index 14."""
    return _wilder14(true_range(h, l, c))


def adx14_wilder(h: np.ndarray, l: np.ndarray, c: np.ndarray
                 ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(+DI, -DI, DX, ADX)`` per §3.1, NaN where undefined.

    DI/DX are defined from index 14 wherever ATR > 0 (DX = 0 when both DIs are
    exactly 0); ADX is seeded at index 27 and is NaN if any DX it depends on is.
    """
    h, l, c = _f64(h), _f64(l), _f64(c)
    n = len(c)
    atr = atr14_wilder(h, l, c)
    pdm = np.full(n, np.nan)
    mdm = np.full(n, np.nan)
    if n >= 2:
        up = h[1:] - h[:-1]
        down = l[:-1] - l[1:]
        pdm[1:] = np.where((up > down) & (up > 0), up, 0.0)
        mdm[1:] = np.where((down > up) & (down > 0), down, 0.0)
    s_pdm = _wilder14(pdm)
    s_mdm = _wilder14(mdm)

    pdi = np.full(n, np.nan)
    mdi = np.full(n, np.nan)
    dx = np.full(n, np.nan)
    for j in range(_ATR_PERIOD, n):
        a = atr[j]
        if not a > 0:  # ATR <= 0 (or NaN) -> unknown, never zero
            continue
        p = 100.0 * s_pdm[j] / a
        m = 100.0 * s_mdm[j] / a
        pdi[j] = p
        mdi[j] = m
        total = p + m
        dx[j] = 0.0 if total == 0.0 else 100.0 * abs(p - m) / total

    adx = np.full(n, np.nan)
    if n > _ADX_SEED_INDEX:
        adx[_ADX_SEED_INDEX] = np.mean(dx[_ATR_PERIOD:_ADX_SEED_INDEX + 1])
        for j in range(_ADX_SEED_INDEX + 1, n):
            adx[j] = (13.0 * adx[j - 1] + dx[j]) / 14.0
    return pdi, mdi, dx, adx


def realized_vol_ratio(c: np.ndarray) -> Observation:
    """std(last 5 log returns, ddof=1) / std(last 20 log returns, ddof=1)."""
    c = _f64(c)
    need = _RV_LONG + 1
    if len(c) < need:
        return Observation(None, STATUS_INSUFFICIENT_HISTORY,
                           f"rv ratio needs {need} closes, got {len(c)}")
    tail = c[-need:]
    bad = np.flatnonzero(~np.isfinite(tail) | ~(tail > 0))
    if bad.size:
        idx = len(c) - need + int(bad[0])
        return Observation(None, STATUS_INVALID_PRICES, f"non-finite or non-positive close at index {idx}")
    r = np.log(tail[1:] / tail[:-1])
    std_long = float(np.std(r[-_RV_LONG:], ddof=1))
    std_short = float(np.std(r[-_RV_SHORT:], ddof=1))
    if not np.isfinite(std_long) or not std_long > 0:
        return Observation(None, STATUS_INVALID_PRICES, "zero 20-session realized volatility (denominator)")
    value = std_short / std_long
    if not np.isfinite(value):
        return Observation(None, STATUS_INVALID_PRICES, "non-finite realized volatility ratio")
    return Observation(float(value), STATUS_VALID)


def _all_three(status: str, reason: str) -> MarketConditionValues:
    obs = Observation(None, status, reason)
    return MarketConditionValues(trend_slope=obs, adx=obs, rv_ratio=obs)


def _first_invalid_bar(o, h, l, c, v) -> Optional[str]:
    for name, arr in (("open", o), ("high", h), ("low", l), ("close", c)):
        bad = np.flatnonzero(~np.isfinite(arr) | ~(arr > 0))
        if bad.size:
            return f"non-finite or non-positive {name} at index {int(bad[0])}"
    bad = np.flatnonzero(~np.isfinite(v) | ~(v >= 0))
    if bad.size:
        return f"non-finite or negative volume at index {int(bad[0])}"
    checks = (
        ("high < low", h < l),
        ("high < max(open, close)", h < np.maximum(o, c)),
        ("low > min(open, close)", l > np.minimum(o, c)),
    )
    first = None
    for label, mask in checks:
        idx = np.flatnonzero(mask)
        if idx.size and (first is None or int(idx[0]) < first[0]):
            first = (int(idx[0]), label)
    if first is not None:
        return f"OHLC ordering violated ({first[1]}) at index {first[0]}"
    return None


def compute_market_conditions(o, h, l, c, v) -> MarketConditionValues:
    """Compute the three v1 observations from exactly ``WINDOW`` bars.

    Raises ``ValueError`` if the arrays differ in length or exceed ``WINDOW``.
    """
    o, h, l, c, v = _f64(o), _f64(h), _f64(l), _f64(c), _f64(v)
    lengths = {len(o), len(h), len(l), len(c), len(v)}
    if len(lengths) != 1:
        raise ValueError(f"OHLCV arrays must have equal lengths, got {sorted(lengths)}")
    n = len(c)
    if n > WINDOW:
        raise ValueError(f"expected at most {WINDOW} bars (pre-slice the window), got {n}")
    if n < WINDOW:
        return _all_three(STATUS_INSUFFICIENT_HISTORY, f"{n} of {WINDOW} bars")

    problem = _first_invalid_bar(o, h, l, c, v)
    if problem is not None:
        return _all_three(STATUS_INVALID_PRICES, problem)

    last = WINDOW - 1
    atr = atr14_wilder(h, l, c)
    atr_last = atr[last]
    rv = realized_vol_ratio(c)
    if not atr_last > 0:
        bad = Observation(None, STATUS_INVALID_PRICES, f"atr<=0 at index {last}")
        return MarketConditionValues(trend_slope=bad, adx=bad, rv_ratio=rv)

    ema = ema_wilder_seeded(c, _EMA_PERIOD)
    slope = (ema[last] - ema[last - 5]) / (5.0 * atr_last)
    if np.isfinite(slope):
        trend = Observation(float(slope), STATUS_VALID)
    else:
        trend = Observation(None, STATUS_INVALID_PRICES, "non-finite trend slope")

    _, _, dx, adx_arr = adx14_wilder(h, l, c)
    adx_last = adx_arr[last]
    if np.isfinite(adx_last):
        adx = Observation(float(adx_last), STATUS_VALID)
    else:
        undefined = np.flatnonzero(~np.isfinite(dx[_ATR_PERIOD:])) + _ATR_PERIOD
        where = f" at index {int(undefined[0])}" if undefined.size else ""
        adx = Observation(None, STATUS_INVALID_PRICES, f"adx undefined: atr<=0{where}")

    return MarketConditionValues(trend_slope=trend, adx=adx, rv_ratio=rv)
