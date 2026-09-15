"""Pure calculators for the option market-condition entry gates.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md``
§3 (measurements), §3.1 (exact numerical contract), §7 (failure statuses).

This module is PURE: numpy/math only, no I/O, no provider access, no logging,
no reference to any other project module.  It turns one validated window of
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

Reproducibility contract (bit-for-bit): ALL reductions -- every seed mean and
both RV sample standard deviations -- use ``math.fsum`` (correctly rounded,
independent of summation order): ``mean = fsum(xs)/n`` and
``std = sqrt(fsum((x - mean)**2 for x in xs) / (n - 1))``.  Log returns use
``math.log(C[j]/C[j-1])`` per element.  Every recurrence is an explicit
left-to-right loop over Python floats written exactly as above.  Only exact
elementwise IEEE operations (subtraction, abs, max, comparisons) are
vectorised.  A batch implementation must reproduce these results with ``==``;
``np.mean``/``np.std``/``axis=`` reductions do not (pairwise summation).

Status rules (an invalid observation never becomes 0, a neutral regime or a pass):

* Arrays shorter than ``WINDOW`` -> all three ``insufficient_history``.
  Longer, or unequal lengths -> ``ValueError``: callers must pre-slice, so
  window invariance is structural rather than a property of warm-up convergence.
* Any non-finite or non-positive o/h/l/c, ``h < l``, ``h < max(o, c)`` or
  ``l > min(o, c)`` -> all three ``invalid_prices``.  The reason is
  deterministic: the LOWEST bad index, naming every problem at that index,
  e.g. ``"invalid_prices: index 50 (close non-finite)"``.  Volume does not
  enter any v1 measurement; it is only checked to be finite and non-negative
  (failure reported the same way).
* ``ATR[127] <= 0`` -> slope and ADX ``invalid_prices`` ("atr<=0"); the RV
  ratio is still computed on its own terms.
* RV ratio: an exactly zero 20-session denominator -> ``invalid_prices``; a
  zero 5-session numerator with a positive denominator is a valid ``0.0``.
  Never substituted by 1 or infinity.  Only an EXACT zero is unknown: a pure
  geometric series has a numerically tiny-but-nonzero std and yields a number.

Edge resolved toward UNKNOWN (the contract says "ATR <= 0 is unknown, not
zero" but only pins the check for index 127): if ATR is exactly 0 at some
index inside 14..127 (possible only as a leading run of identical OHLC bars,
since Wilder smoothing of non-negative TR cannot return to 0 once positive),
DX at that index is undefined, so the ADX seed/recurrence is undefined and
ADX is reported ``invalid_prices`` even when ``ATR[127] > 0``.  The slope is
unaffected because its only ATR input is ATR[127].

``MarketConditionValues.as_row()`` intentionally drops ``reason``: the
statuses are the category downstream reports count; reasons are diagnostics.

The ``missing_session``, ``no_context`` and ``missing_replay_object``
statuses are defined here for the shared vocabulary; they are produced by the
callers that assemble the window, not by these calculators.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
_SLOPE_LAG = 5
_RV_SHORT = 5
_RV_LONG = 20

_NAN = float("nan")


@dataclass(frozen=True)
class Observation:
    """One measured field. Only ``valid`` carries a (finite, float) value."""

    value: Optional[float]
    status: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown observation status {self.status!r}")
        if self.value is not None:
            if isinstance(self.value, bool) or not isinstance(self.value, (float, np.floating)):
                raise ValueError(f"observation value must be a float, got {type(self.value).__name__}")
            object.__setattr__(self, "value", float(self.value))
        if self.status == STATUS_VALID:
            if self.value is None or not math.isfinite(self.value):
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
        """Flat row: value-or-None, ``<field>_status`` and ``calc_version`` (no reasons)."""
        row: Dict[str, Any] = {}
        for field, obs in self.by_field().items():
            row[field] = obs.value
            row[f"{field}_status"] = obs.status
        row["calc_version"] = self.calc_version
        return row


def _f64(x) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def _fsum_mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


def _fsum_sample_std(xs: Sequence[float]) -> float:
    m = _fsum_mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def ema_sma_seeded(c: np.ndarray, period: int = 50) -> np.ndarray:
    """EMA with alpha = 2/(period+1), seeded at index period-1 with the fsum mean of c[:period].

    NaN before index period-1 (and everywhere if the series is too short).
    """
    cl: List[float] = _f64(c).tolist()
    n = len(cl)
    out = [_NAN] * n
    if n >= period:
        k_new = 2.0 / (period + 1)
        k_old = (period - 1) / (period + 1)
        prev = _fsum_mean(cl[:period])
        out[period - 1] = prev
        for j in range(period, n):
            prev = k_new * cl[j] + k_old * prev
            out[j] = prev
    return np.array(out, dtype=np.float64)


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    """TR[j] = max(H-L, |H-C[j-1]|, |L-C[j-1]|) for j >= 1; NaN at index 0."""
    h, l, c = _f64(h), _f64(l), _f64(c)
    out = np.full(len(c), np.nan)
    if len(c) < 2:
        return out
    prev_c = c[:-1]
    out[1:] = np.maximum(np.maximum(h[1:] - l[1:], np.abs(h[1:] - prev_c)), np.abs(l[1:] - prev_c))
    return out


def _wilder_list(x: List[float], period: int) -> List[float]:
    """Seed at index ``period`` with fsum-mean(x[1..period]); ``((period-1)*prev + x[j]) / period`` after."""
    n = len(x)
    out = [_NAN] * n
    if n <= period:
        return out
    k_old = float(period - 1)
    denom = float(period)
    prev = _fsum_mean(x[1:period + 1])
    out[period] = prev
    for j in range(period + 1, n):
        prev = (k_old * prev + x[j]) / denom
        out[j] = prev
    return out


def atr14_wilder(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    """ATR14 per §3.1; NaN before index 14."""
    return np.array(_wilder_list(true_range(h, l, c).tolist(), _ATR_PERIOD), dtype=np.float64)


def adx14_wilder(h: np.ndarray, l: np.ndarray, c: np.ndarray, atr: Optional[np.ndarray] = None
                 ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(+DI, -DI, DX, ADX)`` per §3.1, NaN where undefined.

    ``atr`` may be a precomputed ``atr14_wilder(h, l, c)`` to avoid recomputing it.
    DI/DX are defined from index 14 wherever ATR > 0 (DX = 0 when both DIs are
    exactly 0); ADX is seeded at index 27 and is NaN if any DX it depends on is.
    """
    h, l, c = _f64(h), _f64(l), _f64(c)
    n = len(c)
    atr_arr = atr14_wilder(h, l, c) if atr is None else _f64(atr)
    if len(atr_arr) != n:
        raise ValueError(f"atr length {len(atr_arr)} != bars {n}")
    pdm = np.full(n, np.nan)
    mdm = np.full(n, np.nan)
    if n >= 2:
        up = h[1:] - h[:-1]
        down = l[:-1] - l[1:]
        pdm[1:] = np.where((up > down) & (up > 0), up, 0.0)
        mdm[1:] = np.where((down > up) & (down > 0), down, 0.0)
    s_pdm = _wilder_list(pdm.tolist(), _ATR_PERIOD)
    s_mdm = _wilder_list(mdm.tolist(), _ATR_PERIOD)
    atr_l = atr_arr.tolist()

    pdi = [_NAN] * n
    mdi = [_NAN] * n
    dx = [_NAN] * n
    for j in range(_ATR_PERIOD, n):
        a = atr_l[j]
        if not a > 0:  # ATR <= 0 (or NaN) -> unknown, never zero
            continue
        p = 100.0 * s_pdm[j] / a
        m = 100.0 * s_mdm[j] / a
        pdi[j] = p
        mdi[j] = m
        total = p + m
        dx[j] = 0.0 if total == 0.0 else 100.0 * abs(p - m) / total

    adx = [_NAN] * n
    if n > _ADX_SEED_INDEX:
        k_old = float(_ATR_PERIOD - 1)
        denom = float(_ATR_PERIOD)
        prev = _fsum_mean(dx[_ATR_PERIOD:_ADX_SEED_INDEX + 1])  # NaN if any DX there is undefined
        adx[_ADX_SEED_INDEX] = prev
        for j in range(_ADX_SEED_INDEX + 1, n):
            prev = (k_old * prev + dx[j]) / denom
            adx[j] = prev
    return (np.array(pdi, dtype=np.float64), np.array(mdi, dtype=np.float64),
            np.array(dx, dtype=np.float64), np.array(adx, dtype=np.float64))


def realized_vol_ratio(c: np.ndarray) -> Observation:
    """fsum sample std (ddof=1) of the last 5 log returns / that of the last 20."""
    c = _f64(c)
    need = _RV_LONG + 1
    if len(c) < need:
        return Observation(None, STATUS_INSUFFICIENT_HISTORY,
                           f"rv ratio needs {need} closes, got {len(c)}")
    tail = c[-need:]
    bad = np.flatnonzero(~np.isfinite(tail) | (tail <= 0))
    if bad.size:
        idx = len(c) - need + int(bad[0])
        return Observation(None, STATUS_INVALID_PRICES,
                           f"invalid_prices: index {idx} (close non-finite or non-positive)")
    tl = tail.tolist()
    r = [math.log(tl[j] / tl[j - 1]) for j in range(1, len(tl))]
    std_long = _fsum_sample_std(r[-_RV_LONG:])
    std_short = _fsum_sample_std(r[-_RV_SHORT:])
    if not std_long > 0:
        return Observation(None, STATUS_INVALID_PRICES, "zero 20-session realized volatility (denominator)")
    value = std_short / std_long
    if not math.isfinite(value):
        # Defensive: unreachable for finite positive closes (finite std / positive std).
        return Observation(None, STATUS_INVALID_PRICES, "non-finite realized volatility ratio")
    return Observation(value, STATUS_VALID)


def _all_three(status: str, reason: str) -> MarketConditionValues:
    obs = Observation(None, status, reason)
    return MarketConditionValues(trend_slope=obs, adx=obs, rv_ratio=obs)


def _invalid_bar_reason(o, h, l, c, v) -> Optional[str]:
    """Lowest bad index across every check, naming all problems at that index."""
    checks = []
    for name, arr in (("open", o), ("high", h), ("low", l), ("close", c)):
        finite = np.isfinite(arr)
        checks.append((f"{name} non-finite", ~finite))
        checks.append((f"{name} non-positive", finite & (arr <= 0)))
    checks.append(("volume non-finite", ~np.isfinite(v)))
    checks.append(("volume negative", v < 0))
    checks.append(("high < low", h < l))
    checks.append(("high < max(open, close)", h < np.maximum(o, c)))
    checks.append(("low > min(open, close)", l > np.minimum(o, c)))

    any_bad = np.zeros(len(c), dtype=bool)
    for _, mask in checks:
        any_bad |= mask
    bad = np.flatnonzero(any_bad)
    if not bad.size:
        return None
    i = int(bad[0])
    labels = [label for label, mask in checks if mask[i]]
    return f"invalid_prices: index {i} ({', '.join(labels)})"


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

    problem = _invalid_bar_reason(o, h, l, c, v)
    if problem is not None:
        return _all_three(STATUS_INVALID_PRICES, problem)

    last = WINDOW - 1
    atr = atr14_wilder(h, l, c)
    atr_last = float(atr[last])
    rv = realized_vol_ratio(c)
    if not atr_last > 0:
        bad = Observation(None, STATUS_INVALID_PRICES, f"atr<=0 at index {last}")
        return MarketConditionValues(trend_slope=bad, adx=bad, rv_ratio=rv)

    ema = ema_sma_seeded(c, _EMA_PERIOD)
    slope = (float(ema[last]) - float(ema[last - _SLOPE_LAG])) / (_SLOPE_LAG * atr_last)
    if math.isfinite(slope):
        trend = Observation(slope, STATUS_VALID)
    else:
        # Defensive: unreachable for finite positive prices with ATR > 0 barring float overflow.
        trend = Observation(None, STATUS_INVALID_PRICES, "non-finite trend slope")

    _, _, dx, adx_arr = adx14_wilder(h, l, c, atr=atr)
    adx_last = float(adx_arr[last])
    if math.isfinite(adx_last):
        adx = Observation(adx_last, STATUS_VALID)
    else:
        undefined = np.flatnonzero(~np.isfinite(dx[_ATR_PERIOD:])) + _ATR_PERIOD
        if undefined.size:
            adx = Observation(None, STATUS_INVALID_PRICES,
                              f"adx undefined: atr<=0 at index {int(undefined[0])}")
        else:
            # Defensive: unreachable for validated prices barring float overflow.
            adx = Observation(None, STATUS_INVALID_PRICES, "adx non-finite")

    return MarketConditionValues(trend_slope=trend, adx=adx, rv_ratio=rv)
