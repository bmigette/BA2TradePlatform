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
``std = sqrt(fsum(d*d for d = x - mean) / (n - 1))`` -- squares are ``d*d``,
never ``pow`` (``**``), because IEEE multiplication is correctly rounded
everywhere while the C library ``pow`` is not.  Log returns use
``math.log(C[j]/C[j-1])`` per element; ``math.log`` is the one remaining
platform-library dependency and is not guaranteed correctly rounded between
MSVC and glibc (``sqrt`` is exact).  Every recurrence is an explicit
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
  e.g. ``"index 50 (close non-finite)"``.  Volume does not
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

import functools
import math
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from dataclasses import InitVar, dataclass
from dataclasses import field as dc_field
from types import MappingProxyType
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Mapping, Optional, Sequence, Tuple

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

    def to_feature_row(self) -> "FeatureRow":
        """The field-generic row the store, readers and conditions share (one calc version
        per field; for this trio all three carry ``self.calc_version``)."""
        values = self.by_field()
        return FeatureRow(values=values, calc_versions={f: self.calc_version for f in values})


@dataclass(frozen=True)
class FeatureRow:
    """A computed row for ANY registered profile: ``{field: Observation}`` plus the calculator
    version that produced each field. Satisfies ``FeatureRowLike``.

    Both mappings are copied ONCE at construction into read-only ``MappingProxyType`` views, so
    ``by_field()`` returns the stored mapping (no per-read allocation on the decision path) and
    no caller can edit a memoised row in place.
    """

    values: Mapping[str, Observation]
    calc_versions: Mapping[str, str]

    def __post_init__(self) -> None:
        values = dict(self.values)
        versions = dict(self.calc_versions)
        if not values:
            raise ValueError("a FeatureRow needs at least one field")
        for field, obs in values.items():
            if not isinstance(field, str) or not field:
                raise ValueError(f"field names must be non-empty strings, got {field!r}")
            if not isinstance(obs, Observation):
                raise ValueError(f"field {field!r} must hold an Observation, got {type(obs).__name__}")
        if set(versions) != set(values):
            raise ValueError(
                f"calc_versions keys {sorted(versions)!r} must equal the fields {sorted(values)!r}")
        for field, version in versions.items():
            if not isinstance(version, str) or not version:
                raise ValueError(f"calc version of {field!r} must be a non-empty string, got {version!r}")
        object.__setattr__(self, "values", MappingProxyType(values))
        object.__setattr__(self, "calc_versions", MappingProxyType(versions))

    __hash__ = None  # type: ignore[assignment]  # mapping fields: equality only

    def by_field(self) -> Mapping[str, Observation]:
        return self.values

    @classmethod
    def uniform(cls, profile: "ProfileSpec", status: str, reason: str) -> "FeatureRow":
        """Every field of ``profile`` with the same non-valid ``status`` (a window that could
        not be assembled fails all fields together)."""
        if status == STATUS_VALID:
            raise ValueError("a uniform row carries no values, so it cannot be valid")
        obs = Observation(None, status, reason)
        return cls(values={f.name: obs for f in profile.fields},
                   calc_versions={f.name: profile.calc_version for f in profile.fields})


def _f64(x) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def _fsum_mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


def _fsum_sample_std(xs: Sequence[float]) -> float:
    m = _fsum_mean(xs)
    total = math.fsum((x - m) * (x - m) for x in xs)  # d*d, never pow
    return math.sqrt(total / (len(xs) - 1))


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
                           f"insufficient history: {len(c)} of {need} closes")
    tail = c[-need:]
    bad = np.flatnonzero(~np.isfinite(tail) | (tail <= 0))
    if bad.size:
        idx = len(c) - need + int(bad[0])
        return Observation(None, STATUS_INVALID_PRICES,
                           f"index {idx} (close non-finite or non-positive)")
    tl = tail.tolist()
    r = [math.log(tl[j] / tl[j - 1]) for j in range(1, len(tl))]
    std_long = _fsum_sample_std(r[-_RV_LONG:])
    std_short = _fsum_sample_std(r[-_RV_SHORT:])
    if not std_long > 0:
        return Observation(None, STATUS_INVALID_PRICES, "zero 20-session volatility")
    value = std_short / std_long
    if not math.isfinite(value):
        # Defensive: unreachable for finite positive closes (finite std / positive std).
        return Observation(None, STATUS_INVALID_PRICES, "non-finite realized volatility ratio")
    return Observation(value, STATUS_VALID)


def _all_three(status: str, reason: str) -> MarketConditionValues:
    obs = Observation(None, status, reason)
    return MarketConditionValues(trend_slope=obs, adx=obs, rv_ratio=obs)


def _invalid_bar_checks(o, h, l, c, v) -> List[Tuple[str, np.ndarray]]:
    """``(label, mask)`` for every per-bar validity check, in report order.

    Factored out of :func:`_invalid_bar_reason` so a batch implementation can run the checks
    ONCE over a full history and still report the same window-local reason string.
    """
    checks: List[Tuple[str, np.ndarray]] = []
    for name, arr in (("open", o), ("high", h), ("low", l), ("close", c)):
        finite = np.isfinite(arr)
        checks.append((f"{name} non-finite", ~finite))
        checks.append((f"{name} non-positive", finite & (arr <= 0)))
    checks.append(("volume non-finite", ~np.isfinite(v)))
    checks.append(("volume negative", v < 0))
    checks.append(("high < low", h < l))
    checks.append(("high < max(open, close)", h < np.maximum(o, c)))
    checks.append(("low > min(open, close)", l > np.minimum(o, c)))

    return checks


def _invalid_bar_reason(o, h, l, c, v) -> Optional[str]:
    """Lowest bad index across every check, naming all problems at that index."""
    checks = _invalid_bar_checks(o, h, l, c, v)
    any_bad = np.zeros(len(c), dtype=bool)
    for _, mask in checks:
        any_bad |= mask
    bad = np.flatnonzero(any_bad)
    if not bad.size:
        return None
    i = int(bad[0])
    return _invalid_bar_label(checks, i, i)


def _invalid_bar_label(checks: Sequence[Tuple[str, np.ndarray]], index: int, local_index: int) -> str:
    """The reason string for the bad bar at ``index``, reported at ``local_index``."""
    labels = [label for label, mask in checks if mask[index]]
    return f"index {local_index} ({', '.join(labels)})"


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
        return _all_three(STATUS_INSUFFICIENT_HISTORY, f"insufficient history: {n} of {WINDOW} bars")

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


# ---------------------------------------------------------------------------
# Profile registry (design §3 table, amendment 2). Pure data: every later layer (store columns,
# engine event types, template leaves, launcher ids, reports) iterates the registered fields
# instead of hard-coding the ohlcv-v1 trio, so a second profile (e.g. ``ta-structure-v1`` with a
# CATEGORICAL field) is one ``register_profile`` call.
# ---------------------------------------------------------------------------
_FIELD_KINDS = ("numeric", "categorical")
_ANCHOR_OPS = ("<", ">")
_FORBIDDEN_CODE = "none"

#: The comparison operators a gate on a field of each kind may use -- the ONE table, read by
#: ``TradeConditions.market_condition_condition_class`` (which enforces it at condition
#: construction) and by ``trigger_catalog.operator_options_for`` (which is what the rule editor
#: offers). Two lists would drift, and the drift only shows up when a deployed rule raises.
#:
#: A NUMERIC gate is a threshold: ``==`` on a float measurement passes essentially never, and
#: ``>=``/``<=`` differ from the strict forms only on a measure-zero set. A CATEGORICAL field
#: holds a regime CODE (``structure_state``: bull=1, bear=2), so an ORDERING on it is not a
#: weaker condition but a meaningless one -- ``> 1`` reads as "bear only", which is the opposite
#: of what somebody who knows "1 = bull" is trying to say. ``!=`` is excluded for the same
#: reason the code ``none`` is never selectable: "not bull" silently includes the unclassified
#: state, so it is not the complement the reader expects.
OPERATORS_BY_KIND: Mapping[str, FrozenSet[str]] = MappingProxyType({
    "numeric": frozenset({"<", ">"}),
    "categorical": frozenset({"=="}),
})


@dataclass(frozen=True)
class FieldSpec:
    """One market-condition field. ``name`` is the canonical field name, which is also the
    ExpertEventType value and the store column. NUMERIC fields carry the GA threshold range and
    the template's explicit fixed interpretation (``anchor_op``/``anchor_value``); CATEGORICAL
    fields carry ``codes`` (value -> distinct positive int) and no range. ``"none"`` (no
    classification) is never a code: it must not be selectable as a regime.

    ``codes`` is accepted as any Mapping but STORED as ``_code_pairs``: a tuple of
    ``(value, code)`` pairs ordered by CODE -- hashable, picklable and deep-copyable (GA workers
    spawn on Windows and distributed payloads pickle configs), and part of ``==``/``hash``
    independent of the input dict's order. The ``codes`` property returns a fresh read-only
    ``MappingProxyType`` view iterating in code order, e.g. ``{"bull": 1, "bear": 2}`` -> bull,
    bear (the design table order). That order is a PERSISTED contract: templates build
    ``mode_choices=["off", *codes]`` from it and the choice-gene index follows it. (A
    mappingproxy attribute would make the spec unpicklable.)

    ``dataclasses.replace`` keeps ``codes`` (it reads the property); pass ``codes=None`` when
    changing a categorical spec to numeric. Use :meth:`to_dict` for serialisation --
    ``dataclasses.asdict`` exposes the private ``_code_pairs`` and omits ``codes``."""

    name: str                      # canonical field name == ExpertEventType value == store column
    kind: str                      # "numeric" | "categorical"
    short: str                     # id suffix used by the launcher: "slope" | "adx" | "rv"
    searched: bool                 # searched by the GA in this profile's grids
    value_min: Optional[float] = None
    value_max: Optional[float] = None
    value_step: Optional[float] = None
    anchor_op: Optional[str] = None      # the template's explicit fixed interpretation
    anchor_value: Optional[float] = None
    codes: InitVar[Optional[Mapping[str, int]]] = None   # categorical only; never contains "none"
    ui_name: str = ""
    #: What the number MEANS, in words ("ATR14 multiples", "sessions", "ADX index points").
    #: A market-condition measurement is not self-describing: ``structure_dist_support_atr`` and
    #: ``structure_bars_since_bos`` are both "a number around 2", and an operator who reads the
    #: first as sessions or the second as ATRs authors a gate that is off by an order of
    #: magnitude and still looks plausible. Carried HERE rather than in the rule editor because
    #: the unit is a property of the measurement, and a copy in the UI would go stale the day a
    #: field's normalisation changes. Empty for a categorical field, which counts nothing.
    unit: str = ""
    _code_pairs: Optional[Tuple[Tuple[str, int], ...]] = dc_field(default=None, init=False)

    def __post_init__(self, codes: Optional[Mapping[str, int]]) -> None:
        if not self.name or not self.short:
            raise ValueError(f"FieldSpec needs a name and a short id suffix, got {self.name!r}/{self.short!r}")
        if self.kind not in _FIELD_KINDS:
            raise ValueError(f"FieldSpec {self.name!r}: kind must be one of {_FIELD_KINDS}, got {self.kind!r}")
        ranges = (self.value_min, self.value_max, self.value_step)
        if self.kind == "categorical":
            if any(v is not None for v in ranges) or self.anchor_op is not None or self.anchor_value is not None:
                raise ValueError(f"FieldSpec {self.name!r}: a categorical field carries no threshold range/anchor")
            if not codes:
                raise ValueError(f"FieldSpec {self.name!r}: a categorical field needs non-empty codes")
            if _FORBIDDEN_CODE in codes:
                raise ValueError(f"FieldSpec {self.name!r}: {_FORBIDDEN_CODE!r} is never a code")
            vals = list(codes.values())
            if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in vals):
                raise ValueError(f"FieldSpec {self.name!r}: codes must be positive ints, got {codes!r}")
            if len(set(vals)) != len(vals):
                raise ValueError(f"FieldSpec {self.name!r}: code values must be distinct, got {codes!r}")
            object.__setattr__(self, "_code_pairs", tuple(sorted(dict(codes).items(), key=lambda kv: kv[1])))
        else:
            if codes is not None:
                raise ValueError(f"FieldSpec {self.name!r}: a numeric field carries no codes")
            if any(v is None for v in ranges):
                raise ValueError(f"FieldSpec {self.name!r}: a numeric field needs value_min/value_max/value_step")
            if not (self.value_step > 0 and self.value_max > self.value_min):
                raise ValueError(
                    f"FieldSpec {self.name!r}: need value_step > 0 and value_max > value_min, got {ranges!r}")
            if self.anchor_op not in _ANCHOR_OPS or self.anchor_value is None:
                raise ValueError(
                    f"FieldSpec {self.name!r}: a numeric field needs anchor_op in {_ANCHOR_OPS} and an "
                    f"anchor_value, got {self.anchor_op!r}/{self.anchor_value!r}")
            if not (self.value_min <= self.anchor_value <= self.value_max):
                raise ValueError(
                    f"FieldSpec {self.name!r}: anchor_value {self.anchor_value!r} outside "
                    f"[{self.value_min!r}, {self.value_max!r}]")

    def to_dict(self) -> Dict[str, Any]:
        """The public constructor fields in declaration order, ``codes`` as a plain dict in code
        order (never ``_code_pairs``); ``FieldSpec(**spec.to_dict()) == spec``."""
        return {
            "name": self.name,
            "kind": self.kind,
            "short": self.short,
            "searched": self.searched,
            "value_min": self.value_min,
            "value_max": self.value_max,
            "value_step": self.value_step,
            "anchor_op": self.anchor_op,
            "anchor_value": self.anchor_value,
            "codes": None if self._code_pairs is None else dict(self._code_pairs),
            "ui_name": self.ui_name,
            "unit": self.unit,
        }


def _field_spec_codes(self: FieldSpec) -> Optional[Mapping[str, int]]:
    """Read-only ``value -> code`` view of a categorical field (None for numeric fields)."""
    return None if self._code_pairs is None else MappingProxyType(dict(self._code_pairs))


# Attached after the dataclass decorator ran: defined in the class body, the property object
# would become the ``codes`` InitVar's default.
FieldSpec.codes = property(_field_spec_codes)  # type: ignore[assignment]


@dataclass(frozen=True)
class ProfileSpec:
    name: str
    calc_version: str
    fields: Tuple[FieldSpec, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.calc_version:
            raise ValueError(f"ProfileSpec needs a name and a calc_version, got {self.name!r}/{self.calc_version!r}")
        object.__setattr__(self, "fields", tuple(self.fields))


OHLCV_V1 = ProfileSpec(name="ohlcv-v1", calc_version=CALC_VERSION, fields=(
    FieldSpec(name=FIELD_TREND_SLOPE, kind="numeric", short="slope", searched=True,
              value_min=-0.30, value_max=0.30, value_step=0.05, anchor_op=">", anchor_value=0.0,
              ui_name="Underlying trend slope", unit="ATR14 multiples per session"),
    FieldSpec(name=FIELD_ADX, kind="numeric", short="adx", searched=True,
              value_min=10.0, value_max=40.0, value_step=5.0, anchor_op="<", anchor_value=25.0,
              ui_name="Underlying trend strength", unit="ADX index points (0-100)"),
    FieldSpec(name=FIELD_RV_RATIO, kind="numeric", short="rv", searched=True,
              value_min=0.50, value_max=2.00, value_step=0.25, anchor_op="<", anchor_value=1.0,
              ui_name="Realized volatility expansion", unit="ratio of 5-session to 20-session realized volatility"),
))
PROFILES: Dict[str, ProfileSpec] = {OHLCV_V1.name: OHLCV_V1}


def _known_fields() -> List[str]:
    return [f.name for prof in PROFILES.values() for f in prof.fields]


def _unknown_field(field: str) -> KeyError:
    return KeyError(f"unknown market-condition field {field!r}; known fields: {_known_fields()!r}")


def profile_for_field(field: str) -> ProfileSpec:
    """The registered profile owning ``field``; KeyError naming the known fields otherwise."""
    for prof in PROFILES.values():
        if any(f.name == field for f in prof.fields):
            return prof
    raise _unknown_field(field)


def field_spec(field: str) -> FieldSpec:
    """The registered FieldSpec for ``field``; KeyError naming the known fields otherwise."""
    for prof in PROFILES.values():
        for f in prof.fields:
            if f.name == field:
                return f
    raise _unknown_field(field)


@functools.lru_cache(maxsize=None)
def field_codes(field: str) -> Mapping[str, int]:
    """Memoised read-only ``value -> code`` mapping of a registered CATEGORICAL field, in code
    order -- one dict hit for per-trial callers (the GA decode) instead of a scan over every
    profile. KeyError (naming the known fields) for an unknown field, ValueError for a numeric
    one. The memo is cleared wherever the registry changes (``register_profile`` and
    ``registered_profile``'s restore)."""
    codes = field_spec(field).codes
    if codes is None:
        raise ValueError(f"market-condition field {field!r} is numeric: it has no codes")
    return MappingProxyType(dict(codes))


def register_profile(spec: ProfileSpec) -> None:
    """Add a profile. Refuses a duplicate profile name, a field repeated inside the profile, or a
    field already registered by another profile (field names are global: they are event types
    and store columns). FieldSpec consistency is validated at FieldSpec construction."""
    if spec.name in PROFILES:
        raise ValueError(f"market-condition profile {spec.name!r} is already registered")
    if not spec.fields:
        raise ValueError(f"market-condition profile {spec.name!r} has no fields")
    names = [f.name for f in spec.fields]
    if len(set(names)) != len(names):
        raise ValueError(f"market-condition profile {spec.name!r} repeats a field: {names!r}")
    taken = set(_known_fields())
    clash = [n for n in names if n in taken]
    if clash:
        raise ValueError(f"market-condition fields {clash!r} are already registered by another profile")
    # Short suffixes become launcher leaf ids (``<rule>-market-<short>``), so they are global too.
    shorts = [f.short for f in spec.fields]
    taken_shorts = {f.short for prof in PROFILES.values() for f in prof.fields}
    if len(set(shorts)) != len(shorts) or any(sh in taken_shorts for sh in shorts):
        raise ValueError(f"market-condition profile {spec.name!r}: short ids {shorts!r} repeat or are already taken")
    PROFILES[spec.name] = spec
    field_codes.cache_clear()


@contextmanager
def registered_profile(spec: ProfileSpec) -> Iterator[ProfileSpec]:
    """TEST HOOK: register ``spec`` for the duration of the ``with`` block, then restore the
    previous registry contents (also on an exception). Mutates ``PROFILES`` in place, so modules
    that imported the dict see the temporary profile too. Not for production registration."""
    saved = dict(PROFILES)
    try:
        register_profile(spec)
        yield spec
    finally:
        PROFILES.clear()
        PROFILES.update(saved)
        field_codes.cache_clear()


def _compute_ohlcv_v1(o, h, l, c, v) -> FeatureRow:
    return compute_market_conditions(o, h, l, c, v).to_feature_row()


#: profile name -> ``fn(o, h, l, c, v) -> FeatureRow`` over exactly ``WINDOW`` bars. Readers look
#: the compute function up here by profile, so a second profile (Task 10's ``ta-structure-v1``)
#: registers its calculator next to its ``ProfileSpec`` and no reader changes.
COMPUTE_BY_PROFILE: Dict[str, Callable[..., FeatureRow]] = {OHLCV_V1.name: _compute_ohlcv_v1}


# ===========================================================================================
# ta-structure-v1 -- chart-structure measurements (design 2026-09-15 sections 3.2 and 3.3)
# ===========================================================================================
# Same input as ohlcv-v1: ONE validated window of exactly WINDOW regular-session daily bars,
# indexed 0..127, and the SAME ATR14 series (``atr14_wilder``).  Every division is by ATR[127];
# ATR[127] <= 0 makes every field of this profile unknown.
#
# Fixed conventions of the profile (part of the calculator version, never genes):
# pivot span ``PIVOT_K`` = 3, channel lookback ``CHANNEL_LOOKBACK`` = 20, level tolerance
# ``LEVEL_TOL_ATR`` = 0.25 ATR.
#
# CONFIRMED PIVOTS are the lookahead guard for the whole profile: a pivot high at p needs
# ``H[p] > H[p-i]`` and ``H[p] > H[p+i]`` strictly for i in 1..K (ties are NOT pivots), and it is
# confirmed only at index p + K.  Inside one window the detection range is p in [K, 127-K], so
# every detectable pivot is already confirmed at 127; the confirmation rule bites in the BOS /
# CHoCH walk, which re-derives the swing structure AS EACH EARLIER SESSION SAW IT.
#
# UNKNOWN IS NEVER ZERO.  A window with no confirmed pivot above the close has no resistance --
# not a resistance at its highest bar, and not a distance of 0 (which would say "sitting on the
# level").  Such a field is reported ``insufficient_history`` (the 128 sessions do not contain
# the structure the measurement needs), with the vocabulary of section 7 unchanged.  A degenerate
# channel (sigma == 0, twenty identical closes) is ``invalid_prices``, mirroring ohlcv-v1's
# "zero 20-session volatility" for the RV ratio.
#
# ``structure_state`` is CATEGORICAL and always VALID when ATR is: ``bull`` -> 1.0, ``bear`` ->
# 2.0, and "no classification" -> 0.0.  ``none`` is stored but is never a gene choice, so an
# equality gate on 1.0/2.0 simply does not fire on a 0.0 row.
#
# INTERPRETATIONS TAKEN where section 3.3 does not spell the case out (each pinned by a test):
#  * a bar that is BOTH a pivot high and a pivot low (an outside bar) contributes both, and the
#    HIGH is ordered first at that index;
#  * collapsing a run of same-kind pivots to its extreme keeps the EARLIEST of equal extremes;
#  * "ties between equal pivot prices are one level" is realised by the touch count: equal-price
#    pivots are all touches of the single level they define, never separate levels;
#  * the touch tolerance is INCLUSIVE (exactly on +/- 0.25 ATR counts);
#  * ``structure_state == none`` makes BOTH ``bars_since`` fields unknown: section 3.3 item 4
#    walks "while structure is bull" (or bear), so with no direction there is no break to find.

STRUCTURE_CALC_VERSION = "ta-structure-v1/calc-1"
STRUCTURE_PROFILE = "ta-structure-v1"

PIVOT_K = 3
CHANNEL_LOOKBACK = 20
LEVEL_TOL_ATR = 0.25

FIELD_DIST_SUPPORT = "structure_dist_support_atr"
FIELD_DIST_RESISTANCE = "structure_dist_resistance_atr"
FIELD_SUPPORT_TOUCHES = "structure_support_touches"
FIELD_RESISTANCE_TOUCHES = "structure_resistance_touches"
FIELD_CHANNEL_SLOPE = "channel_slope_20_atr"
FIELD_CHANNEL_WIDTH = "channel_width_20_atr"
FIELD_CHANNEL_POS = "channel_pos_20"
FIELD_CLOSE_VS_PRIOR_HIGH = "close_vs_prior_high_20_atr"
FIELD_CLOSE_VS_PRIOR_LOW = "close_vs_prior_low_20_atr"
FIELD_STRUCTURE_STATE = "structure_state"
FIELD_BARS_SINCE_BOS = "structure_bars_since_bos"
FIELD_BARS_SINCE_CHOCH = "structure_bars_since_choch"

#: The twelve stored fields, in the order of the design 3.2 table.
STRUCTURE_FIELDS = (
    FIELD_DIST_SUPPORT, FIELD_DIST_RESISTANCE, FIELD_SUPPORT_TOUCHES, FIELD_RESISTANCE_TOUCHES,
    FIELD_CHANNEL_SLOPE, FIELD_CHANNEL_WIDTH, FIELD_CHANNEL_POS, FIELD_CLOSE_VS_PRIOR_HIGH,
    FIELD_CLOSE_VS_PRIOR_LOW, FIELD_STRUCTURE_STATE, FIELD_BARS_SINCE_BOS, FIELD_BARS_SINCE_CHOCH,
)

STATE_BULL = "bull"
STATE_BEAR = "bear"
STATE_NONE = "none"
#: ``value -> code`` for the categorical field. ``none`` is NOT here (design: never selectable).
STRUCTURE_STATE_CODES: Mapping[str, int] = MappingProxyType({STATE_BULL: 1, STATE_BEAR: 2})
STRUCTURE_STATE_NONE_CODE = 0.0

PIVOT_HIGH = "high"
PIVOT_LOW = "low"


@dataclass(frozen=True)
class Pivot:
    """One confirmed pivot: its window index, ``high``/``low`` kind and its extreme price."""

    index: int
    kind: str
    price: float


def find_pivots(h: Sequence[float], l: Sequence[float]) -> List[Pivot]:
    """Confirmed pivots of one window, chronological (a HIGH before a LOW at the same index).

    A pivot at index p requires a STRICT extreme against all ``PIVOT_K`` neighbours on both
    sides, so the detection range is ``PIVOT_K <= p <= len - 1 - PIVOT_K`` and equal-price ties
    are not pivots.

    The span is NOT a parameter. It is a fixed convention of the profile (design 3.2: "part of
    the calculator version, not genes"), and ``_break_fields`` reads ``PIVOT_K`` directly for the
    confirmation lag -- a caller that could pass a different span here would silently get a pivot
    set and a confirmation rule that disagree.
    """
    k = PIVOT_K
    n = len(h)
    out: List[Pivot] = []
    for p in range(k, n - k):
        hp = h[p]
        if all(hp > h[p - i] and hp > h[p + i] for i in range(1, k + 1)):
            out.append(Pivot(p, PIVOT_HIGH, hp))
        lp = l[p]
        if all(lp < l[p - i] and lp < l[p + i] for i in range(1, k + 1)):
            out.append(Pivot(p, PIVOT_LOW, lp))
    return out


def reduce_to_swings(pivots: Sequence[Pivot]) -> List[Pivot]:
    """Alternating swing sequence (design 3.3 item 1): collapse each maximal run of same-kind
    pivots to its extreme -- the highest of consecutive highs, the lowest of consecutive lows --
    keeping the EARLIEST of equal extremes. The result alternates high, low, high, low.

    THE REFERENCE ORACLE, not a production path: nothing in this module calls it. It is design
    3.3 item 1 written the way the design writes it -- a forward fold over the whole sequence --
    so that :func:`_recent_swings`, which the measurements actually use and which walks backwards
    and stops early, can be pinned against it
    (``tests/test_chart_structure_calculators.py::test_recent_swings_is_the_tail_of_the_full_reduction``).
    Kept here rather than in the test file because it IS the contract, and a reader comparing the
    code against section 3.3 should find it next to the code it governs."""
    out: List[Pivot] = []
    for p in pivots:
        if out and out[-1].kind == p.kind:
            last = out[-1]
            better = p.price > last.price if p.kind == PIVOT_HIGH else p.price < last.price
            if better:
                out[-1] = p
        else:
            out.append(p)
    return out


def _recent_swings(pivots: Sequence[Pivot], lo: int, hi: int, want: int
                   ) -> Tuple[List[float], List[float]]:
    """The last ``want`` swing-high and swing-low PRICES of ``pivots[lo:hi+1]``, most recent
    first. Equivalent to the tail of :func:`reduce_to_swings` (each maximal same-kind run
    collapses to its earliest extreme) but walks backwards and stops early, which is what makes
    the per-session BOS/CHoCH walk and the batch form affordable."""
    highs: List[float] = []
    lows: List[float] = []
    i = hi
    while i >= lo and (len(highs) < want or len(lows) < want):
        kind = pivots[i].kind
        j = i
        while j - 1 >= lo and pivots[j - 1].kind == kind:
            j -= 1
        ext = pivots[j].price
        for m in range(j + 1, i + 1):
            price = pivots[m].price
            if (price > ext) if kind == PIVOT_HIGH else (price < ext):
                ext = price
        (highs if kind == PIVOT_HIGH else lows).append(ext)
        i = j - 1
    return highs, lows


def fit_channel(closes: Sequence[float]) -> Tuple[float, float, float]:
    """OLS ``y = a + b*x`` over ``x = 0..n-1`` plus the residual sigma with ``ddof = 2``.

    Every reduction is ``math.fsum`` and every square is ``d*d`` (Task 1's bit-exact contract), so
    a batch implementation that re-fits the same 20 closes reproduces this bit for bit."""
    n = len(closes)
    if n < 3:
        raise ValueError(f"the channel fit needs at least 3 points (ddof=2), got {n}")
    xs = [float(i) for i in range(n)]
    xbar = _fsum_mean(xs)
    ybar = _fsum_mean(closes)
    sxy = math.fsum((x - xbar) * (y - ybar) for x, y in zip(xs, closes))
    sxx = math.fsum((x - xbar) * (x - xbar) for x in xs)
    b = sxy / sxx
    a = ybar - b * xbar
    resid = [y - (a + b * x) for x, y in zip(xs, closes)]
    sigma = math.sqrt(math.fsum(e * e for e in resid) / (n - 2))
    return a, b, sigma


@dataclass(frozen=True)
class ChartStructureValues:
    """The twelve ``ta-structure-v1`` observations of one window, in design 3.2 table order."""

    dist_support: Observation
    dist_resistance: Observation
    support_touches: Observation
    resistance_touches: Observation
    channel_slope: Observation
    channel_width: Observation
    channel_pos: Observation
    close_vs_prior_high: Observation
    close_vs_prior_low: Observation
    structure_state: Observation
    bars_since_bos: Observation
    bars_since_choch: Observation
    calc_version: str = STRUCTURE_CALC_VERSION

    def by_field(self) -> Dict[str, Observation]:
        return {
            FIELD_DIST_SUPPORT: self.dist_support,
            FIELD_DIST_RESISTANCE: self.dist_resistance,
            FIELD_SUPPORT_TOUCHES: self.support_touches,
            FIELD_RESISTANCE_TOUCHES: self.resistance_touches,
            FIELD_CHANNEL_SLOPE: self.channel_slope,
            FIELD_CHANNEL_WIDTH: self.channel_width,
            FIELD_CHANNEL_POS: self.channel_pos,
            FIELD_CLOSE_VS_PRIOR_HIGH: self.close_vs_prior_high,
            FIELD_CLOSE_VS_PRIOR_LOW: self.close_vs_prior_low,
            FIELD_STRUCTURE_STATE: self.structure_state,
            FIELD_BARS_SINCE_BOS: self.bars_since_bos,
            FIELD_BARS_SINCE_CHOCH: self.bars_since_choch,
        }

    def as_row(self) -> Dict[str, Any]:
        """Flat row: value-or-None, ``<field>_status`` and ``calc_version`` (no reasons)."""
        row: Dict[str, Any] = {}
        for field, obs in self.by_field().items():
            row[field] = obs.value
            row[f"{field}_status"] = obs.status
        row["calc_version"] = self.calc_version
        return row

    def to_feature_row(self) -> FeatureRow:
        values = self.by_field()
        return FeatureRow(values=values, calc_versions={f: self.calc_version for f in values})


def _all_structure(status: str, reason: str) -> ChartStructureValues:
    obs = Observation(None, status, reason)
    return ChartStructureValues(*([obs] * 12))


_NO_RESISTANCE = "no confirmed pivot high above the close in the window"
_NO_SUPPORT = "no confirmed pivot low below the close in the window"


def _touch_run(prices: Sequence[float], at: int, level: float, tol: float) -> int:
    """How many entries of the SORTED ``prices`` lie within ``level +/- tol``, counted by walking
    outward from ``at`` (an index holding ``level``).

    ``abs(p - level) <= tol`` describes an interval, and the slice of a sorted list inside an
    interval is contiguous, so the walk stops at the first failure in each direction and is
    complete. The PREDICATE is evaluated verbatim rather than replaced by two bisects on
    ``level +/- tol``: those bounds are rounded once each, and a price exactly on the boundary
    can fall on the other side of the rounded bound from the side ``abs(p - level) <= tol`` puts
    it. Bit-exactness is the whole contract here (design 3.3), so the comparison stays the one
    the contract names and the sorted order is used only to bound the work."""
    n = 0
    i = at
    while i >= 0 and abs(prices[i] - level) <= tol:
        n += 1
        i -= 1
    i = at + 1
    while i < len(prices) and abs(prices[i] - level) <= tol:
        n += 1
        i += 1
    return n


def levels_from_sorted(highs: Sequence[float], lows: Sequence[float], close: float,
                       atr_last: float) -> Tuple[Observation, Observation, Observation, Observation]:
    """(dist_support, dist_resistance, support_touches, resistance_touches) from the SORTED
    confirmed pivot-high and pivot-low prices of one window (design 3.3, "nearest-level queries
    against the sorted confirmed levels").

    Resistance is the first sorted high STRICTLY above the close and support the last sorted low
    strictly below it -- two bisects, no arithmetic, so the answer is the same element
    ``min``/``max`` over the unsorted prices would select. The batch form keeps these two lists
    incrementally as its window slides and calls exactly this function, which is what makes
    "batch == reference" a property of the code rather than of a coincidence."""
    tol = LEVEL_TOL_ATR * atr_last
    i = bisect_right(highs, close)
    if i < len(highs):
        r = highs[i]
        res = Observation((r - close) / atr_last, STATUS_VALID)
        res_touch = Observation(float(_touch_run(highs, i, r, tol)), STATUS_VALID)
    else:
        res = Observation(None, STATUS_INSUFFICIENT_HISTORY, _NO_RESISTANCE)
        res_touch = Observation(None, STATUS_INSUFFICIENT_HISTORY, _NO_RESISTANCE)
    j = bisect_left(lows, close)
    if j > 0:
        sup_level = lows[j - 1]
        sup = Observation((close - sup_level) / atr_last, STATUS_VALID)
        sup_touch = Observation(float(_touch_run(lows, j - 1, sup_level, tol)), STATUS_VALID)
    else:
        sup = Observation(None, STATUS_INSUFFICIENT_HISTORY, _NO_SUPPORT)
        sup_touch = Observation(None, STATUS_INSUFFICIENT_HISTORY, _NO_SUPPORT)
    return sup, res, sup_touch, res_touch


def _level_fields(pivots: Sequence[Pivot], close: float, atr_last: float
                  ) -> Tuple[Observation, Observation, Observation, Observation]:
    """(dist_support, dist_resistance, support_touches, resistance_touches) for one window."""
    return levels_from_sorted(
        sorted(p.price for p in pivots if p.kind == PIVOT_HIGH),
        sorted(p.price for p in pivots if p.kind == PIVOT_LOW),
        close, atr_last)


def _channel_fields(closes: Sequence[float], atr_last: float
                    ) -> Tuple[Observation, Observation, Observation]:
    """(slope, width, position) over the last ``CHANNEL_LOOKBACK`` closes."""
    tail = list(closes[-CHANNEL_LOOKBACK:])
    a, b, sigma = fit_channel(tail)
    slope = Observation(b / atr_last, STATUS_VALID)
    if not sigma > 0:
        degenerate = Observation(None, STATUS_INVALID_PRICES,
                                 f"zero residual dispersion over the {CHANNEL_LOOKBACK}-session channel")
        return slope, degenerate, degenerate
    width = Observation(4.0 * sigma / atr_last, STATUS_VALID)
    lower = a + (CHANNEL_LOOKBACK - 1) * b - 2.0 * sigma
    pos = Observation((tail[-1] - lower) / (4.0 * sigma), STATUS_VALID)  # deliberately UNCLAMPED
    return slope, width, pos


def swing_state(pivots: Sequence[Pivot]) -> str:
    """``bull`` / ``bear`` / ``none`` from the last two swing highs and lows (design 3.3 item 3).
    Equality is neither; fewer than two of either kind is ``none``."""
    highs, lows = _recent_swings(pivots, 0, len(pivots) - 1, 2)
    if len(highs) < 2 or len(lows) < 2:
        return STATE_NONE
    sh2, sh1 = highs[0], highs[1]
    sl2, sl1 = lows[0], lows[1]
    if sh2 > sh1 and sl2 > sl1:
        return STATE_BULL
    if sh2 < sh1 and sl2 < sl1:
        return STATE_BEAR
    return STATE_NONE


def _break_fields(pivots: Sequence[Pivot], closes: Sequence[float], state: str
                  ) -> Tuple[Observation, Observation]:
    """(bars_since_bos, bars_since_choch): ``last - index`` of the most recent session whose
    close broke the swing that was the most recent CONFIRMED one AT THAT SESSION -- with the
    structure's direction, for the break of structure; against it, for the change of character."""
    last = len(closes) - 1
    if state == STATE_NONE:
        unknown = Observation(None, STATUS_INSUFFICIENT_HISTORY,
                              "no swing structure: neither a break nor a change of character is defined")
        return unknown, unknown
    bull = state == STATE_BULL
    bos_at: Optional[int] = None
    choch_at: Optional[int] = None
    hi = len(pivots) - 1
    for t in range(last, -1, -1):
        limit = t - PIVOT_K            # pivots CONFIRMED at session t
        while hi >= 0 and pivots[hi].index > limit:
            hi -= 1
        if hi < 0:
            break
        highs, lows = _recent_swings(pivots, 0, hi, 1)
        close = closes[t]
        above = bool(highs) and close > highs[0]
        below = bool(lows) and close < lows[0]
        broke, changed = (above, below) if bull else (below, above)
        if bos_at is None and broke:
            bos_at = t
        if choch_at is None and changed:
            choch_at = t
        if bos_at is not None and choch_at is not None:
            break
    bos = (Observation(float(last - bos_at), STATUS_VALID) if bos_at is not None
           else Observation(None, STATUS_INSUFFICIENT_HISTORY,
                            "no break of structure in the window"))
    choch = (Observation(float(last - choch_at), STATUS_VALID) if choch_at is not None
             else Observation(None, STATUS_INSUFFICIENT_HISTORY,
                              "no change of character in the window"))
    return bos, choch


def _chart_structure_core(h: List[float], l: List[float], c: List[float],
                          pivots: List[Pivot], atr_last: float, *,
                          levels: Optional[Tuple[Observation, ...]] = None,
                          prior_range: Optional[Tuple[float, float]] = None
                          ) -> ChartStructureValues:
    """The measurements themselves, on already-validated window-local lists and pivots.

    Shared verbatim by :func:`compute_chart_structure` and the batch form, which is how
    "batch == reference" is a property of the code rather than of a test that happened to pass.

    ``levels`` and ``prior_range`` let the batch supply what it already holds in a rolling form
    (the incrementally maintained sorted level lists, the rolling 20-session max/min shifted by
    one). Both are SELECTIONS -- a bisect and a max/min -- never arithmetic, so a caller can only
    hand over the same values this function would compute; the batch==reference tests pin it.
    The swing and break fields have no rolling form and are always computed here.
    """
    last = len(c) - 1
    close = c[last]
    sup, res, sup_touch, res_touch = (levels if levels is not None
                                      else _level_fields(pivots, close, atr_last))
    slope, width, pos = _channel_fields(c, atr_last)
    if prior_range is None:
        prior_hi = max(h[last - CHANNEL_LOOKBACK:last])     # excludes session ``last`` itself
        prior_lo = min(l[last - CHANNEL_LOOKBACK:last])
    else:
        prior_hi, prior_lo = prior_range
    vs_high = Observation((close - prior_hi) / atr_last, STATUS_VALID)
    vs_low = Observation((close - prior_lo) / atr_last, STATUS_VALID)
    state = swing_state(pivots)
    bos, choch = _break_fields(pivots, c, state)
    # NOT ``codes.get(state, none)``: a state this function does not recognise would map to
    # the one code on which no gate ever fires, i.e. a bug would present as "the market simply
    # had no structure" for as long as it lasted. An unknown state is a programming error.
    code = (STRUCTURE_STATE_NONE_CODE if state == STATE_NONE
            else float(STRUCTURE_STATE_CODES[state]))
    return ChartStructureValues(
        dist_support=sup, dist_resistance=res,
        support_touches=sup_touch, resistance_touches=res_touch,
        channel_slope=slope, channel_width=width, channel_pos=pos,
        close_vs_prior_high=vs_high, close_vs_prior_low=vs_low,
        structure_state=Observation(code, STATUS_VALID),
        bars_since_bos=bos, bars_since_choch=choch)


def compute_chart_structure(o, h, l, c, v, atr: Optional[np.ndarray] = None) -> ChartStructureValues:
    """Compute the twelve ``ta-structure-v1`` observations from exactly ``WINDOW`` bars.

    ``atr`` may be a precomputed ``atr14_wilder(h, l, c)`` over the SAME window (the profile
    divides by ``atr[WINDOW-1]``); it is recomputed when omitted.
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
        return _all_structure(STATUS_INSUFFICIENT_HISTORY, f"insufficient history: {n} of {WINDOW} bars")

    problem = _invalid_bar_reason(o, h, l, c, v)
    if problem is not None:
        return _all_structure(STATUS_INVALID_PRICES, problem)

    last = WINDOW - 1
    atr_arr = atr14_wilder(h, l, c) if atr is None else _f64(atr)
    if len(atr_arr) != n:
        raise ValueError(f"atr length {len(atr_arr)} != bars {n}")
    atr_last = float(atr_arr[last])
    if not atr_last > 0:
        return _all_structure(STATUS_INVALID_PRICES, f"atr<=0 at index {last}")

    hl, ll, cl = h.tolist(), l.tolist(), c.tolist()
    return _chart_structure_core(hl, ll, cl, find_pivots(hl, ll), atr_last)


TA_STRUCTURE_V1 = ProfileSpec(name=STRUCTURE_PROFILE, calc_version=STRUCTURE_CALC_VERSION, fields=(
    FieldSpec(name=FIELD_DIST_SUPPORT, kind="numeric", short="dist-support", searched=True,
              value_min=0.0, value_max=5.0, value_step=0.5, anchor_op=">", anchor_value=1.0,
              ui_name="Distance to support", unit="ATR14 multiples below the close"),
    FieldSpec(name=FIELD_DIST_RESISTANCE, kind="numeric", short="dist-resistance", searched=True,
              value_min=0.0, value_max=5.0, value_step=0.5, anchor_op=">", anchor_value=1.0,
              ui_name="Distance to resistance", unit="ATR14 multiples above the close"),
    FieldSpec(name=FIELD_SUPPORT_TOUCHES, kind="numeric", short="support-touches", searched=False,
              value_min=1.0, value_max=5.0, value_step=1.0, anchor_op=">", anchor_value=2.0,
              ui_name="Support strength", unit="confirmed pivot-low touches on the level"),
    FieldSpec(name=FIELD_RESISTANCE_TOUCHES, kind="numeric", short="resistance-touches", searched=False,
              value_min=1.0, value_max=5.0, value_step=1.0, anchor_op=">", anchor_value=2.0,
              ui_name="Resistance strength", unit="confirmed pivot-high touches on the level"),
    FieldSpec(name=FIELD_CHANNEL_SLOPE, kind="numeric", short="chan-slope", searched=False,
              value_min=-0.30, value_max=0.30, value_step=0.05, anchor_op=">", anchor_value=0.0,
              ui_name="Channel slope", unit="ATR14 multiples per session"),
    FieldSpec(name=FIELD_CHANNEL_WIDTH, kind="numeric", short="chan-width", searched=False,
              value_min=1.0, value_max=8.0, value_step=0.5, anchor_op="<", anchor_value=4.0,
              ui_name="Channel width", unit="ATR14 multiples"),
    FieldSpec(name=FIELD_CHANNEL_POS, kind="numeric", short="chan-pos", searched=True,
              value_min=0.0, value_max=1.0, value_step=0.1, anchor_op="<", anchor_value=0.5,
              ui_name="Position in channel", unit="fraction of the channel, 0 = floor and 1 = ceiling (unclamped)"),
    FieldSpec(name=FIELD_CLOSE_VS_PRIOR_HIGH, kind="numeric", short="vs-prior-high", searched=True,
              value_min=-3.0, value_max=2.0, value_step=0.25, anchor_op=">", anchor_value=0.0,
              ui_name="Close vs prior 20-session high", unit="ATR14 multiples"),
    FieldSpec(name=FIELD_CLOSE_VS_PRIOR_LOW, kind="numeric", short="vs-prior-low", searched=False,
              value_min=-2.0, value_max=3.0, value_step=0.25, anchor_op=">", anchor_value=0.0,
              ui_name="Close vs prior 20-session low", unit="ATR14 multiples"),
    FieldSpec(name=FIELD_STRUCTURE_STATE, kind="categorical", short="structure", searched=True,
              codes=STRUCTURE_STATE_CODES, ui_name="Swing structure"),
    FieldSpec(name=FIELD_BARS_SINCE_BOS, kind="numeric", short="bos", searched=False,
              value_min=0.0, value_max=60.0, value_step=5.0, anchor_op="<", anchor_value=20.0,
              ui_name="Sessions since break of structure", unit="sessions"),
    FieldSpec(name=FIELD_BARS_SINCE_CHOCH, kind="numeric", short="choch", searched=False,
              value_min=0.0, value_max=60.0, value_step=5.0, anchor_op="<", anchor_value=20.0,
              ui_name="Sessions since change of character", unit="sessions"),
))


def _compute_ta_structure_v1(o, h, l, c, v) -> FeatureRow:
    return compute_chart_structure(o, h, l, c, v).to_feature_row()


register_profile(TA_STRUCTURE_V1)
COMPUTE_BY_PROFILE[TA_STRUCTURE_V1.name] = _compute_ta_structure_v1
