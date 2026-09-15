"""Source contract for the market-condition gates: window assembly and source certification.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` section 4.

**Window assembly.** A calculator receives exactly ``WINDOW`` bars whose dates are exactly the
``WINDOW`` regular sessions ending at the requested session (``regular_sessions_ending_at``).
Nothing is forward-filled, spliced or silently taken from an older session:

* fewer sessions exist before the requested one than the window needs (the series starts inside
  the window, a listing too young) -> ``insufficient_history``;
* a required session is absent inside the covered span, the requested session itself is
  absent, a bar carries a date that is not a regular session, or two bars share a date with
  different values -> ``missing_session``;
* prices that are non-finite, non-positive or mis-ordered are NOT judged here: the calculators
  own that validation (``invalid_prices``) so there is exactly one definition of a bad bar.

**Source certification.** The v1 source profile ``fmp-daily-split-adjusted-v1`` asserts that
the FMP daily cache's Open/High/Low/Close are on ONE split-adjusted basis. ``certify_source_columns``
checks it against known splits (AAPL 4:1 on 2020-08-31, NVDA 10:1 on 2024-06-10) instead of
assuming it: across the split date every price column must move like an ordinary day (not by
~1/factor), and the intrabar ratios O/C, H/C, L/C must not jump by ~factor. A cache that fails
is reported ``consistent=False``; preflight (Task 6) refuses the profile rather than guess an
adjustment.

Pure apart from ``certify_source_columns``, which reads parquet files.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, Tuple

import numpy as np

from ba2_common.core.market_calendar import regular_sessions_ending_at_tuple
from ba2_common.core.market_conditions import (
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_MISSING_SESSION,
    STATUS_VALID,
    WINDOW,
)

__all__ = [
    "SOURCE_PROFILE_FMP_DAILY",
    "FMP_OHLCV_PROVIDER_DIR",
    "FMP_DAILY_COLUMNS",
    "read_fmp_daily_cache",
    "normalized_window_bytes",
    "window_from_bytes",
    "window_digest_of_bytes",
    "WindowResult",
    "assemble_window",
    "window_digest",
    "SplitFixture",
    "CERTIFICATION_SPLITS",
    "SymbolCertification",
    "CertificationReport",
    "certify_split",
    "certify_source_columns",
    "BASIS_SPLIT_ADJUSTED",
    "BASIS_UNADJUSTED",
    "BASIS_MIXED",
    "BASIS_UNAVAILABLE",
]

#: The v1 source profile: FMP ``historical-price-full`` daily bars as cached by
#: ``FMPOHLCVProvider`` (``CACHE_FOLDER/FMPOHLCVProvider/<SYM>_1d.parquet``), split-adjusted OHLC.
SOURCE_PROFILE_FMP_DAILY = "fmp-daily-split-adjusted-v1"
#: The provider's cache directory name under ``CACHE_FOLDER`` (its class name).
FMP_OHLCV_PROVIDER_DIR = "FMPOHLCVProvider"
#: The provider cache's column layout. The ONE place the live reader and certification take it.
FMP_DAILY_COLUMNS = ("Date", "Open", "High", "Low", "Close", "Volume")

_DAY = "datetime64[D]"


# ---------------------------------------------------------------------------
# Window assembly
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WindowResult:
    """Either an assembled window (``status == "valid"``, the five float64 arrays and the
    session dates present) or a failure status with a reason and no arrays."""

    status: str
    reason: str = ""
    dates: Optional[Tuple[date, ...]] = None
    o: Optional[np.ndarray] = None
    h: Optional[np.ndarray] = None
    l: Optional[np.ndarray] = None
    c: Optional[np.ndarray] = None
    v: Optional[np.ndarray] = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_VALID

    def arrays(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        if not self.ok:
            raise ValueError(f"no window: {self.status} ({self.reason})")
        return self.o, self.h, self.l, self.c, self.v


def _fail(status: str, reason: str) -> WindowResult:
    return WindowResult(status=status, reason=reason)


def _to_day64(dates: Any) -> np.ndarray:
    """Bar dates -> ``datetime64[D]``. Accepts naive datetime64 arrays (any unit), ``date``
    objects and midnight-naive ``datetime`` objects. A timestamp carrying a time of day (in
    either form) or a timezone is an ambiguous bar date and is REFUSED, never truncated
    (design section 4)."""
    arr = np.asarray(dates)
    if np.issubdtype(arr.dtype, np.datetime64):
        days = arr.astype(_DAY)
        if arr.dtype != days.dtype:
            timed = np.flatnonzero(days.astype(arr.dtype) != arr)
            if len(timed):
                raise ValueError(f"ambiguous bar date {arr[timed[0]]!r}: carries a time of day, "
                                 "expected a session label (a date)")
        return days
    out = np.empty(len(arr), dtype=_DAY)
    for i, d in enumerate(arr):
        if isinstance(d, datetime):
            if d.tzinfo is not None or (d.hour, d.minute, d.second, d.microsecond) != (0, 0, 0, 0):
                raise ValueError(f"ambiguous bar date {d!r}: expected a session label (a date)")
            d = d.date()
        if not isinstance(d, date):
            raise ValueError(f"bar date must be a date, got {type(d).__name__}")
        out[i] = np.datetime64(d, "D")
    return out


def _day(d64: np.datetime64) -> date:
    return d64.astype(object)


@lru_cache(maxsize=4096)
def _required_sessions(session: date, n: int) -> Tuple[Tuple[date, ...], np.ndarray]:
    """The window's session dates as ``(tuple of date, read-only datetime64[D] array)``, built
    once per (session, n): the conversions were ~60% of an ``assemble_window`` call."""
    dates = regular_sessions_ending_at_tuple(session, n)
    arr = np.array(dates, dtype=_DAY)
    arr.flags.writeable = False
    return dates, arr


def assemble_window(dates: Any, o: Any, h: Any, l: Any, c: Any, v: Any, session: date,
                    n: int = WINDOW) -> WindowResult:
    """Assemble the ``n`` bars of the ``n`` regular sessions ending at ``session``.

    The inputs may hold more bars than the window (any order, bars after ``session`` are
    ignored); they must have equal lengths.
    """
    d = _to_day64(dates)
    cols = [np.asarray(x, dtype=np.float64) for x in (o, h, l, c, v)]
    if any(len(col) != len(d) for col in cols):
        raise ValueError(f"dates and OHLCV must have equal lengths, got {[len(d)] + [len(x) for x in cols]}")

    required_dates, required = _required_sessions(session, n)
    last = np.datetime64(session, "D")
    if not len(d):
        return _fail(STATUS_MISSING_SESSION, f"no bars for any of the {n} sessions ending {session}")

    order = np.argsort(d, kind="stable")
    d = d[order]
    cols = [col[order] for col in cols]

    if d[0] > last:
        return _fail(STATUS_INSUFFICIENT_HISTORY,
                     f"insufficient history: first bar {_day(d[0])} is after {session}")

    in_span = (d >= required[0]) & (d <= last)
    sd = d[in_span]
    scols = [col[in_span] for col in cols]

    # Duplicates: identical rows collapse, conflicting rows are an error (never pick one).
    uniq, first_idx, counts = np.unique(sd, return_index=True, return_counts=True)
    if (counts > 1).any():
        for day64 in uniq[counts > 1]:
            rows = np.stack([col[sd == day64] for col in scols], axis=1)
            if not all(np.array_equal(rows[0], r, equal_nan=True) for r in rows[1:]):
                return _fail(STATUS_MISSING_SESSION, f"conflicting duplicate bars on {_day(day64)}")
        scols = [col[first_idx] for col in scols]
        sd = uniq

    extra = np.setdiff1d(sd, required, assume_unique=True)
    if len(extra):
        return _fail(STATUS_MISSING_SESSION, f"bar dated {_day(extra[0])} is not a regular session")

    present = np.isin(required, sd, assume_unique=True)
    if present.all():
        return WindowResult(
            status=STATUS_VALID,
            dates=required_dates,
            o=np.ascontiguousarray(scols[0]), h=np.ascontiguousarray(scols[1]),
            l=np.ascontiguousarray(scols[2]), c=np.ascontiguousarray(scols[3]),
            v=np.ascontiguousarray(scols[4]),
        )

    if not present.any():
        # Bars exist, but all of them end before the window starts: stale, not young.
        return _fail(STATUS_MISSING_SESSION,
                     f"none of the {n} sessions ending {session} has a bar "
                     f"(last earlier bar {_day(d[d <= last][-1])})")
    k = int(present.argmax())
    if present[k:].all() and d[0] == required[k]:
        # The series simply starts inside the window: every session from its first bar on is
        # present and nothing precedes it.
        return _fail(STATUS_INSUFFICIENT_HISTORY,
                     f"insufficient history: {int(present.sum())} of {n} sessions "
                     f"(series starts {_day(d[0])})")
    missing_idx = np.flatnonzero(~present)
    # A leading gap with earlier bars present is a hole, not a young listing: name the first
    # missing session AFTER the covered start when there is one, else the first overall.
    return _fail(STATUS_MISSING_SESSION,
                 f"missing session {_day(required[missing_idx[0]])} ({len(missing_idx)} of {n} absent)")


def window_digest(o: Any, h: Any, l: Any, c: Any, v: Any) -> str:
    """``"sha256:<hex>"`` of the NORMALIZED window bytes: a C-contiguous little-endian float64
    matrix of shape (bars, 5) with columns open, high, low, close, volume. The same five arrays
    hash identically on every host."""
    return window_digest_of_bytes(normalized_window_bytes(o, h, l, c, v))


def window_digest_of_bytes(data: bytes) -> str:
    """``window_digest`` computed directly on normalized window bytes."""
    return "sha256:" + _sha256(data)


def normalized_window_bytes(o: Any, h: Any, l: Any, c: Any, v: Any) -> bytes:
    """The window as raw little-endian float64 bytes, shape (bars, 5), columns o/h/l/c/v."""
    mat = np.stack([np.asarray(x, dtype="<f8") for x in (o, h, l, c, v)], axis=1)
    return np.ascontiguousarray(mat, dtype="<f8").tobytes()


def window_from_bytes(data: bytes) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Inverse of ``normalized_window_bytes``: ``(o, h, l, c, v)`` float64 arrays."""
    if len(data) % 40:
        raise ValueError(f"window bytes must be a whole number of 5-float64 rows, got {len(data)} bytes")
    mat = np.frombuffer(data, dtype="<f8").reshape(-1, 5)
    return tuple(np.ascontiguousarray(mat[:, j], dtype=np.float64) for j in range(5))


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Source certification
# ---------------------------------------------------------------------------
BASIS_SPLIT_ADJUSTED = "split-adjusted"
BASIS_UNADJUSTED = "unadjusted"
BASIS_MIXED = "mixed"
BASIS_UNAVAILABLE = "unavailable"

#: A ratio within this factor of its target counts as matching it (ln 1.5 ~ 0.405 keeps the
#: "ordinary day" and "~1/factor" bands disjoint for the 4:1 and 10:1 fixtures).
_MATCH_TOLERANCE = math.log(1.5)
_PRICE_COLUMNS = FMP_DAILY_COLUMNS[1:5]  # Open, High, Low, Close


@dataclass(frozen=True)
class SplitFixture:
    symbol: str
    split_date: date
    factor: float


#: Known forward splits inside the cached history used to certify the profile.
CERTIFICATION_SPLITS: Tuple[SplitFixture, ...] = (
    SplitFixture("AAPL", date(2020, 8, 31), 4.0),
    SplitFixture("NVDA", date(2024, 6, 10), 10.0),
)


@dataclass(frozen=True)
class SymbolCertification:
    symbol: str
    split_date: date
    factor: float
    consistent: bool
    basis: str
    #: Close[split] / Close[previous session]: ~1 on a split-adjusted basis, ~1/factor unadjusted.
    close_ratio: Optional[float]
    #: X[split] / X[previous] per price column.
    column_ratios: Mapping[str, float]
    #: (X/C)[previous] / (X/C)[split] for X in Open/High/Low: ~factor means mixed bases.
    intrabar_jumps: Mapping[str, float]
    #: The largest |ln(Close[j]/Close[j-1])| over the ten sessions either side, split excluded.
    surrounding_max_abs_log_move: Optional[float]
    reason: str = ""


@dataclass(frozen=True)
class CertificationReport:
    source_profile: str
    cache_root: str
    symbols: Tuple[SymbolCertification, ...]

    @property
    def consistent(self) -> bool:
        return bool(self.symbols) and all(s.consistent for s in self.symbols)

    def by_symbol(self) -> Mapping[str, SymbolCertification]:
        return {s.symbol: s for s in self.symbols}


def _unavailable(fx: SplitFixture, reason: str) -> SymbolCertification:
    return SymbolCertification(
        symbol=fx.symbol, split_date=fx.split_date, factor=fx.factor, consistent=False,
        basis=BASIS_UNAVAILABLE, close_ratio=None, column_ratios=MappingProxyType({}),
        intrabar_jumps=MappingProxyType({}), surrounding_max_abs_log_move=None, reason=reason)


def _classify(ratio: float, factor: float) -> str:
    if not (math.isfinite(ratio) and ratio > 0):
        return BASIS_MIXED
    lr = math.log(ratio)
    if abs(lr) < _MATCH_TOLERANCE:
        return BASIS_SPLIT_ADJUSTED
    if abs(lr + math.log(factor)) < _MATCH_TOLERANCE:
        return BASIS_UNADJUSTED
    return BASIS_MIXED


def certify_split(dates: Any, o: Any, h: Any, l: Any, c: Any, fixture: SplitFixture) -> SymbolCertification:
    """Classify the price basis of one series across one known split."""
    d = _to_day64(dates)
    order = np.argsort(d, kind="stable")
    d = d[order]
    cols = {name: np.asarray(x, dtype=np.float64)[order]
            for name, x in zip(_PRICE_COLUMNS, (o, h, l, c))}
    split = np.datetime64(fixture.split_date, "D")
    hits = np.flatnonzero(d == split)
    if len(hits) != 1:
        return _unavailable(fixture, f"{len(hits)} bars dated {fixture.split_date} (need exactly 1)")
    i = int(hits[0])
    if i == 0:
        return _unavailable(fixture, f"no bar before the split date {fixture.split_date}")
    if (d[i] - d[i - 1]).astype(int) > 5:
        return _unavailable(fixture, f"gap before the split: previous bar {_day(d[i - 1])}")

    column_ratios = {name: float(col[i] / col[i - 1]) for name, col in cols.items()}
    close = cols["Close"]
    intrabar = {
        name: float((cols[name][i - 1] / close[i - 1]) / (cols[name][i] / close[i]))
        for name in ("Open", "High", "Low")
    }
    lo, hi = max(1, i - 10), min(len(close), i + 11)
    moves = [abs(math.log(close[j] / close[j - 1])) for j in range(lo, hi)
             if j != i and close[j] > 0 and close[j - 1] > 0]
    surrounding = max(moves) if moves else None

    column_classes = {name: _classify(r, fixture.factor) for name, r in column_ratios.items()}
    intrabar_ok = all(_classify(r, fixture.factor) == BASIS_SPLIT_ADJUSTED for r in intrabar.values())
    classes = set(column_classes.values())
    if intrabar_ok and classes == {BASIS_SPLIT_ADJUSTED}:
        basis = BASIS_SPLIT_ADJUSTED
    elif intrabar_ok and classes == {BASIS_UNADJUSTED}:
        basis = BASIS_UNADJUSTED
    else:
        basis = BASIS_MIXED
    reason = ""
    if basis == BASIS_SPLIT_ADJUSTED and surrounding is not None \
            and surrounding >= math.log(fixture.factor) - _MATCH_TOLERANCE:
        # A split-sized move elsewhere in the neighbourhood means the check cannot tell the
        # split from ordinary noise: refuse to certify rather than pass by luck.
        basis = BASIS_MIXED
        reason = f"a split-sized close move ({surrounding:.3f} log) occurs near the split"
    if basis != BASIS_SPLIT_ADJUSTED and not reason:
        reason = f"column classes {column_classes}, intrabar jumps {intrabar}"
    return SymbolCertification(
        symbol=fixture.symbol, split_date=fixture.split_date, factor=fixture.factor,
        consistent=basis == BASIS_SPLIT_ADJUSTED, basis=basis,
        close_ratio=column_ratios["Close"], column_ratios=MappingProxyType(column_ratios),
        intrabar_jumps=MappingProxyType(intrabar), surrounding_max_abs_log_move=surrounding,
        reason=reason)


def certify_source_columns(cache_root: str,
                           splits: Sequence[SplitFixture] = CERTIFICATION_SPLITS) -> CertificationReport:
    """Certify the FMP daily OHLCV cache under ``cache_root`` (a ``CACHE_FOLDER``) against the
    split fixtures. A missing parquet is ``unavailable`` and NOT consistent."""
    results = []
    for fx in splits:
        path = os.path.join(cache_root, FMP_OHLCV_PROVIDER_DIR, f"{fx.symbol.upper()}_1d.parquet")
        if not os.path.exists(path):
            results.append(_unavailable(fx, f"no cache file {path}"))
            continue
        dates, o, h, l, c, _v = read_fmp_daily_cache(path)
        results.append(certify_split(dates, o, h, l, c, fx))
    return CertificationReport(source_profile=SOURCE_PROFILE_FMP_DAILY, cache_root=str(cache_root),
                               symbols=tuple(results))


def read_fmp_daily_cache(path: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Read one FMP daily OHLCV cache parquet -> ``(dates datetime64[D], o, h, l, c, v)`` float64.

    ``Date`` must be a session LABEL: naive midnight, or tz-aware midnight in its own zone (the
    label is then that wall date). Any time of day is REFUSED (ValueError) instead of converted:
    converting a 00:00 New York stamp to UTC, or truncating a 20:00 UTC one, would silently move
    a bar to another session.
    """
    import pandas as pd

    df = pd.read_parquet(path, columns=list(FMP_DAILY_COLUMNS))
    stamps = pd.to_datetime(df[FMP_DAILY_COLUMNS[0]])
    if getattr(stamps.dt, "tz", None) is not None:
        wall = stamps.dt.tz_localize(None)
        timed = wall != wall.dt.normalize()
        if bool(timed.any()):
            raise ValueError(f"{path}: tz-aware bar date {stamps[timed].iloc[0]!r} is not midnight; "
                             "refusing to guess its session")
        stamps = wall
    dates = _to_day64(stamps.to_numpy(dtype="datetime64[ns]"))
    cols = tuple(df[name].to_numpy(dtype=np.float64) for name in FMP_DAILY_COLUMNS[1:])
    return (dates, *cols)
