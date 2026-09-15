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

import math
from contextlib import contextmanager
from dataclasses import InitVar, dataclass
from dataclasses import field as dc_field
from types import MappingProxyType
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

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
    return f"index {i} ({', '.join(labels)})"


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
              ui_name="Underlying trend slope"),
    FieldSpec(name=FIELD_ADX, kind="numeric", short="adx", searched=True,
              value_min=10.0, value_max=40.0, value_step=5.0, anchor_op="<", anchor_value=25.0,
              ui_name="Underlying trend strength"),
    FieldSpec(name=FIELD_RV_RATIO, kind="numeric", short="rv", searched=True,
              value_min=0.50, value_max=2.00, value_step=0.25, anchor_op="<", anchor_value=1.0,
              ui_name="Realized volatility expansion"),
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
