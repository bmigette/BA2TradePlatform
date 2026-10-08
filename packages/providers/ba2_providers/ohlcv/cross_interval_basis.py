"""Cross-interval PRICE-LEVEL check: is a symbol's intraday cache on the same split basis as its daily cache?

THE DEFECT THIS CATCHES
-----------------------
The OHLCV cache holds, per symbol, ``<SYM>_1d.parquet`` and ``<SYM>_5min.parquet`` (any intraday
interval). Prices are split-adjusted "as of the fetch". When a symbol later splits (or the vendor
re-adjusts for a spin-off) the DAILY file is rewritten on the new basis by the split-aware top-up
(``MarketDataProviderInterface._verified_tail_topup`` -> ``force_full_refetch``) while the
INTRADAY file keeps its old basis. A reviewer measured 7 of 250 random symbols whose 5-minute prices
were a constant multiple of the daily ones for a whole year (DD 0.333, SIRI 0.10, SAFE 6.25, PROSY 2.18,
ESEA 1.23, IVR 1.05, CRESY ~1.02). An intraday-clock backtest reads its decision price from the
5-minute bars and its history (indicators, ATR, highs, TP/SL anchors) from the daily bars, so such a
symbol is silently wrong. Nothing compared the two intervals' price LEVELS until this module.

WHAT IS COMPARED, per regular session (both stamped in New York wall-clock, naive; the intraday stamp is
the bar START, 09:30..15:55 for 5-minute bars, the daily stamp is the session date at midnight):

* ``close_ratio`` = last intraday close of the regular session / daily close
* ``high_ratio``  = highest intraday high / daily high
* ``low_ratio``   = lowest intraday low / daily low

A session whose last intraday bar does not reach the calendar close (``ba2_common.core.market_calendar``:
16:00, or 13:00 on a half day) is INCOMPLETE and takes no part in the judgement (its last close is
mid-session, not the close). Sessions with a daily bar but no intraday bars are counted and reported.

CLASSES (``BasisResult.klass``)
-------------------------------
``ok``               the LEVEL of the close ratio (rolling median over 9 sessions) sits at 1; scatter
                     around it (closing auction, thin names) is not a basis and does not matter
``constant_factor``  one clean level away from 1 for the whole window: ``factor`` is that level
                     (intraday / daily)
``factor_changes``   piecewise-constant ratio: ``segments`` lists each (first day, last day, sessions,
                     factor); the day a segment starts is a split / re-adjustment boundary (the known
                     pattern: the 5-minute file is on the old basis up to the day of a later refetch,
                     or the daily file was rewritten on a new basis at a spin-off)
``noisy``            the level is displaced far (>= ~10%) without ever settling on one clean level (a
                     ticker re-used by another instrument, a merger mixed into one file), or the scatter
                     around a level at 1 is wild (MAD > 0.05): not one instrument's prices
``insufficient``     fewer than ``MIN_COMMON_SESSIONS`` comparable sessions: it CANNOT be judged, and
                     is reported as such, never as ok
``no_intraday`` / ``no_daily``  a file is absent / holds no bar in the window

``constant_factor``, ``factor_changes`` and ``noisy`` are the MISMATCH classes (``MISMATCH_KLASSES``): the
launch preflight, the job-start check and ``tools/cache_health_check.py`` refuse / fail on them. The
unjudged classes are listed wherever the verdicts are, never folded into ok.

Everything is vectorised: the expensive step (reading the intraday file and reducing it to one row per
session) is done once per (daily file identity, intraday file identity) and memoised (in process and in a
small ``.npz`` per symbol), so a GA does not rescan per trial. See ``BasisStore``.

TOLERANCES are measured, not guessed: see the comment on each constant (the measurement is
``tools/cache_health_check.py`` over the real cache; the numbers are in the commit message).
"""
from __future__ import annotations

import os
import random
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

__all__ = [
    "KLASS_OK", "KLASS_CONSTANT_FACTOR", "KLASS_FACTOR_CHANGES", "KLASS_NOISY",
    "KLASS_INSUFFICIENT", "KLASS_NO_INTRADAY", "KLASS_NO_DAILY",
    "MISMATCH_KLASSES", "UNJUDGED_KLASSES",
    "SESSION_TOL", "BASIS_TOL", "MIN_OFF_RUN", "LEVEL_TIERS", "NOISY_MAD", "TAIL_SESSIONS", "FAR_TOL",
    "SEGMENT_MAD_TOL", "MIN_COMMON_SESSIONS",
    "MIN_SEGMENT_SESSIONS", "SEGMENT_JUMP", "ALGORITHM_VERSION",
    "SessionTable", "BasisResult", "Segment", "interval_minutes", "reduce_sessions", "WHOLE_HISTORY",
    "classify", "BasisStore", "default_store", "store_for", "ohlcv_cache_dir", "symbol_files",
    "check_symbol", "check_symbols", "check_many", "MemoDir",
]

#: Bumped when the reduction or the classification changes: it is part of every memo key, so a changed
#: algorithm can never be answered from a memo written by the old one.
ALGORITHM_VERSION = 2

KLASS_OK = "ok"
KLASS_CONSTANT_FACTOR = "constant_factor"
KLASS_FACTOR_CHANGES = "factor_changes"
KLASS_NOISY = "noisy"
KLASS_INSUFFICIENT = "insufficient"
KLASS_NO_INTRADAY = "no_intraday"
KLASS_NO_DAILY = "no_daily"
#: classes that mean "the two intervals disagree": a launch / the health check REFUSES on these.
MISMATCH_KLASSES = (KLASS_CONSTANT_FACTOR, KLASS_FACTOR_CHANGES, KLASS_NOISY)
#: classes that mean "cannot be judged": reported loudly, never counted as ok.
UNJUDGED_KLASSES = (KLASS_INSUFFICIENT, KLASS_NO_INTRADAY, KLASS_NO_DAILY)

# --------------------------------------------------------------------------------------------------
# TOLERANCES. Each is justified from a MEASUREMENT over the real cache (FMPOHLCVProvider, 2020-01-01
# ..2026-10-07, every one of the 7 048 symbols holding a 5-minute file and a daily file; scan of
# 2026-10-08: 6 577 ok, 211 mismatched, 255 insufficient, 5 no_intraday). x below is ln(close_ratio) (a log
# ratio, so 0.01 is a 1% level difference either way). The measured numbers are repeated in the module
# docstring of ``tests/test_cross_interval_basis.py`` and in the commit message.
# --------------------------------------------------------------------------------------------------
#: A symbol's LEVEL is the rolling median of x over ``_LEVEL_WINDOW`` sessions; the level is DISPLACED when
#: |level| > BASIS_TOL. Healthy symbols: the whole-file median |x| of every ok symbol is <= 0.0091 (99th
#: percentile 0.0010: the closing auction moves single sessions, never the median); the smallest known
#: defect, CRESY (factor 1.0165, x = 0.0164), is above the tolerance.
BASIS_TOL = 0.012
#: ... for at least this many CONSECUTIVE sessions. A basis defect is a level shift that stays (the known
#: ones last 1.5 - 6 years); a handful of bad prints in a thin name does not make a run of 10 displaced
#: rolling medians.
MIN_OFF_RUN = 10
#: HOW LONG a displaced level must last to count, by its size (``(|ln f| at least, sessions)``, first match
#: wins). A big level (>= 5%: every split, ADR ratio, wrong-instrument mix) counts after MIN_OFF_RUN
#: sessions. The small levels are spin-off re-adjustments (measured: BDX 2.5% until the 2022-04 Embecta
#: spin, ZBH 3.1% until 2022-03, IBM 4.6% until 2021-11 Kyndryl, MRK 4.9% until 2021-06 Organon, ILMN 2.9%
#: until 2024-06, FNF 4.0%, O 3.3%, WPC 2.1%: all real, all 1.5 - 4 years long); 2-3 week stretches at
#: 0.97 - 1.03 also occur in healthy liquid names (CMCSA, OUT, KG, AMLP: an event, a bad print run), so
#: they must last 60 sessions (2% - 5%) or a whole year (1.2% - 2%, CRESY: 1.6% for the entire 6.7 years).
LEVEL_TIERS = ((0.05, MIN_OFF_RUN), (0.02, 60), (BASIS_TOL, 250))
#: A SESSION is "outside tolerance" when |x| > SESSION_TOL. Informational (out_share): thin names (SPACs,
#: ETFs, small caps) sit outside 2% on 15-25% of their sessions with a median of exactly 1, so this share
#: is NOT a verdict, which is why it is not one.
SESSION_TOL = 0.02
#: dispersion (median absolute deviation of x around its own level) above which a series is ``noisy``:
#: ok symbols have a MAD of 0.0005 (median), 0.0072 (99th percentile), 0.028 (the worst of 6 577); a
#: scatter of 0.05 means the two series are not the same instrument's prices on any single level.
NOISY_MAD = 0.05
#: a displaced segment is a clean level when its own MAD is at most this (the 99th percentile of a healthy
#: symbol's MAD is 0.0072: a constant factor times ordinary closing-auction noise has that MAD).
SEGMENT_MAD_TOL = 0.02
#: a displaced stretch that never settles on one clean level is ``noisy`` (refused) only when it is this far
#: from 1 (|ln| >= FAR_TOL, i.e. more than ~10% in either direction); below that it is a thin name's
#: closing-auction scatter (ANGH, BANL, GYRO, ... : median exactly 1, stretches of +-2-6%), not a basis.
FAR_TOL = 0.10
#: The TAIL of a frame about to be WRITTEN (a top-up appends a handful of sessions: far fewer than the
#: ``MIN_COMMON_SESSIONS`` the whole-history verdict needs) must sit at the daily level: the median of its
#: last TAIL_SESSIONS comparable sessions may be at most FAR_TOL (10%) from 1. That catches the append that
#: matters (new bars on the vendor's basis after a split, onto a file or beside a daily cache on the old one);
#: smaller re-adjustments are the stale marker's job. Measured: over a healthy symbol's whole history its
#: worst |rolling 5-session median of x| is <= 0.08 for 99% of the ok symbols.
TAIL_SESSIONS = 5
#: fewer comparable sessions than this CANNOT be judged (reported ``insufficient``).
MIN_COMMON_SESSIONS = 10
#: a level must hold for at least this many consecutive sessions to be a segment (shorter runs are
#: absorbed into their neighbour: a few bad prints are not a re-adjustment boundary).
MIN_SEGMENT_SESSIONS = 5
#: the rolling-median level steps by more than this (|delta ln r|) at a segment boundary.
SEGMENT_JUMP = 0.012
#: rolling-median window (sessions) used to find the level.
_LEVEL_WINDOW = 9

#: ``(start, end)`` that covers every cached session (the default window of a whole-file judgement).
WHOLE_HISTORY = ("1900-01-01", "2200-01-01")

_MIN_REGULAR = 9 * 60 + 30     # 09:30
_MAX_REGULAR = 16 * 60         # 16:00 (exclusive for a bar START)
_INTERVAL_MIN = {"1m": 1, "1min": 1, "5m": 5, "5min": 5, "15m": 15, "15min": 15, "30m": 30,
                 "30min": 30, "1h": 60, "1hour": 60, "60min": 60, "4h": 240, "4hour": 240}


def interval_minutes(interval: str) -> int:
    """Bar length in minutes for an intraday interval spelling. Refuses an unknown spelling."""
    try:
        return _INTERVAL_MIN[interval.lower()]
    except KeyError:
        raise ValueError(f"unknown intraday interval {interval!r}; known: {sorted(_INTERVAL_MIN)}") from None


# --------------------------------------------------------------------------------------------------
# The calendar: close minute (New York wall clock) of every regular session, half days included.
# --------------------------------------------------------------------------------------------------
_CAL_LOCK = threading.Lock()
_CAL: Optional[Tuple[np.ndarray, np.ndarray]] = None


def _calendar() -> Tuple[np.ndarray, np.ndarray]:
    """``(days datetime64[D], close_minute int16)`` for every NYSE regular session 2005..2030, from the
    repo's own calendar (``ba2_common.core.market_calendar``; raises ``MarketCalendarUnavailable``)."""
    global _CAL
    with _CAL_LOCK:
        if _CAL is None:
            from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
            sessions = nyse_regular_sessions(date(2005, 1, 3), date(2030, 12, 31))
            days, closes = [], []
            for _open, close_utc in sessions:
                local = close_utc.astimezone(NY_TZ)
                days.append(np.datetime64(local.date(), "D"))
                closes.append(local.hour * 60 + local.minute)
            _CAL = (np.asarray(days, dtype="datetime64[D]"), np.asarray(closes, dtype=np.int16))
        return _CAL


# --------------------------------------------------------------------------------------------------
# Reduction: two frames -> one row per DAILY session
# --------------------------------------------------------------------------------------------------
@dataclass
class SessionTable:
    """One row per daily session: the comparison of that session across the two intervals.

    ``close_ratio`` & co are NaN for a session the intraday file has no usable bars for; ``complete``
    says the intraday session reached the calendar close (the close ratio is only meaningful then)."""
    days: np.ndarray            # datetime64[D], ascending
    close_ratio: np.ndarray     # float64
    high_ratio: np.ndarray
    low_ratio: np.ndarray
    has_intraday: np.ndarray    # bool: any regular-session intraday bar that day
    complete: np.ndarray        # bool: the last intraday bar reaches the calendar close
    daily_first: Optional[date] = None
    daily_last: Optional[date] = None
    intraday_first: Optional[date] = None
    intraday_last: Optional[date] = None

    def __len__(self) -> int:
        return int(len(self.days))


def _naive_ny(dates: pd.Series) -> pd.Series:
    """A date column as naive New York wall-clock datetimes (the cache's own convention). A tz-aware
    column is converted TO New York first, so the instant is preserved, never reinterpreted."""
    s = pd.to_datetime(dates)
    if getattr(s.dt, "tz", None) is not None:
        from ba2_common.core.market_calendar import NY_TZ
        s = s.dt.tz_convert(NY_TZ).dt.tz_localize(None)
    return s


def _daily_wall(dates: pd.Series) -> pd.Series:
    """A DAILY date column as naive session dates. A daily bar is labelled with its session DATE (the
    FMP cache: naive midnight; Alpaca: 04:00/05:00 UTC), so a tz-aware label keeps its own wall-clock
    date: converting it to New York would move a midnight-UTC label to the previous day."""
    s = pd.to_datetime(dates)
    if getattr(s.dt, "tz", None) is not None:
        s = s.dt.tz_localize(None)
    return s


def reduce_sessions(daily: Optional[pd.DataFrame], intraday: Optional[pd.DataFrame],
                    interval: str) -> SessionTable:
    """Reduce an intraday frame to per-session (last close, high, low) and compare with ``daily``.

    ``daily`` needs Date, High, Low, Close; ``intraday`` the same (any interval, ``interval`` names it).
    Pure and vectorised: no per-row Python."""
    length = interval_minutes(interval)
    if daily is None or len(daily) == 0:
        return SessionTable(*(np.empty(0, dt) for dt in ("datetime64[D]", float, float, float, bool, bool)))
    dd = _daily_wall(daily["Date"]).to_numpy(dtype="datetime64[ns]")
    order = np.argsort(dd, kind="stable")
    dd = dd[order]
    dday = dd.astype("datetime64[D]")
    dh = daily["High"].to_numpy(dtype=np.float64)[order]
    dl = daily["Low"].to_numpy(dtype=np.float64)[order]
    dc = daily["Close"].to_numpy(dtype=np.float64)[order]
    # a duplicate daily date keeps the LAST row (the writers' own rule: drop_duplicates keep='last')
    keep = np.append(dday[1:] != dday[:-1], True)
    dday, dh, dl, dc = dday[keep], dh[keep], dl[keep], dc[keep]
    n = len(dday)
    close_ratio = np.full(n, np.nan)
    high_ratio = np.full(n, np.nan)
    low_ratio = np.full(n, np.nan)
    has_intraday = np.zeros(n, dtype=bool)
    complete = np.zeros(n, dtype=bool)
    i_first = i_last = None

    if intraday is not None and len(intraday):
        t = _naive_ny(intraday["Date"]).to_numpy(dtype="datetime64[ns]")
        h = intraday["High"].to_numpy(dtype=np.float64)
        l = intraday["Low"].to_numpy(dtype=np.float64)
        c = intraday["Close"].to_numpy(dtype=np.float64)
        order = np.argsort(t, kind="stable")
        t, h, l, c = t[order], h[order], l[order], c[order]
        iday = t.astype("datetime64[D]")
        mod = ((t - iday.astype("datetime64[ns]")) // np.timedelta64(1, "m")).astype(np.int64)
        reg = (mod >= _MIN_REGULAR) & (mod < _MAX_REGULAR)
        if reg.any():
            t, h, l, c, iday, mod = t[reg], h[reg], l[reg], c[reg], iday[reg], mod[reg]
            # a duplicate bar stamp keeps the last row, as the writers do
            keep_t = np.append(t[1:] != t[:-1], True)
            h, l, c, iday, mod = h[keep_t], l[keep_t], c[keep_t], iday[keep_t], mod[keep_t]
            starts = np.flatnonzero(np.append(True, iday[1:] != iday[:-1]))
            ends = np.append(starts[1:], len(iday)) - 1
            s_day = iday[starts]
            # bars with a non-finite price are not prices: take the extremum over the finite ones
            hh = np.where(np.isfinite(h), h, -np.inf)
            ll = np.where(np.isfinite(l), l, np.inf)
            s_high = np.maximum.reduceat(hh, starts)
            s_low = np.minimum.reduceat(ll, starts)
            s_high[~np.isfinite(s_high)] = np.nan
            s_low[~np.isfinite(s_low)] = np.nan
            s_close = c[ends]
            s_last_mod = mod[ends]
            i_first, i_last = s_day[0].astype(object), s_day[-1].astype(object)
            # align to the daily sessions
            pos = np.searchsorted(s_day, dday)
            pos_c = np.minimum(pos, len(s_day) - 1)
            hit = (pos < len(s_day)) & (s_day[pos_c] == dday)
            has_intraday = hit
            idx = np.where(hit, pos_c, 0)
            with np.errstate(divide="ignore", invalid="ignore"):
                ok_c = hit & (dc > 0)
                ok_h = hit & (dh > 0)
                ok_l = hit & (dl > 0)
                close_ratio = np.where(ok_c, s_close[idx] / dc, np.nan)
                high_ratio = np.where(ok_h, s_high[idx] / dh, np.nan)
                low_ratio = np.where(ok_l, s_low[idx] / dl, np.nan)
            for a in (close_ratio, high_ratio, low_ratio):
                a[~np.isfinite(a) | (a <= 0)] = np.nan
            cal_days, cal_close = _calendar()
            cpos = np.searchsorted(cal_days, dday)
            cpos_c = np.minimum(cpos, len(cal_days) - 1)
            in_cal = (cpos < len(cal_days)) & (cal_days[cpos_c] == dday)
            close_min = np.where(in_cal, cal_close[cpos_c], 0).astype(np.int64)
            # complete: the session's last bar ENDS at/after the calendar close. A day that is not a
            # regular session is never complete (a daily bar on a holiday is not comparable).
            complete = hit & in_cal & ((s_last_mod[idx] + length) >= close_min)
    return SessionTable(
        days=dday, close_ratio=close_ratio, high_ratio=high_ratio, low_ratio=low_ratio,
        has_intraday=has_intraday, complete=complete,
        daily_first=dday[0].astype(object), daily_last=dday[-1].astype(object),
        intraday_first=i_first, intraday_last=i_last)


# --------------------------------------------------------------------------------------------------
# Classification (pure, per window)
# --------------------------------------------------------------------------------------------------
@dataclass
class Segment:
    first_day: str
    last_day: str
    sessions: int
    factor: float           # intraday / daily over the segment (median close ratio)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BasisResult:
    symbol: str
    klass: str
    window_start: str = ""
    window_end: str = ""
    daily_sessions: int = 0          # daily bars in the window
    no_intraday_sessions: int = 0    # daily bar, no intraday bars that day
    incomplete_sessions: int = 0     # intraday present but the session does not reach the close
    common_sessions: int = 0         # comparable sessions the judgement is made on
    median_close_ratio: float = float("nan")
    median_high_ratio: float = float("nan")
    median_low_ratio: float = float("nan")
    out_share: float = float("nan")  # share of comparable sessions with |ln close_ratio| > SESSION_TOL
    far_sessions: int = 0            # comparable sessions with |ln close_ratio| >= FAR_TOL (short bursts too)
    first_day: str = ""              # first / last COMPARED session and its close ratio
    first_ratio: float = float("nan")
    last_day: str = ""
    last_ratio: float = float("nan")
    factor: float = float("nan")     # constant_factor: the level; factor_changes: the latest level
    segments: List[Segment] = field(default_factory=list)
    by_year: Dict[int, dict] = field(default_factory=dict)
    reason: str = ""

    @property
    def mismatched(self) -> bool:
        return self.klass in MISMATCH_KLASSES

    @property
    def unjudged(self) -> bool:
        return self.klass in UNJUDGED_KLASSES

    @property
    def boundaries(self) -> List[str]:
        """The first day of every segment after the first: where the basis changes."""
        return [s.first_day for s in self.segments[1:]]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["segments"] = [s.to_dict() for s in self.segments]
        return d

    def describe(self) -> str:
        """One line for a refusal / report: symbol, class, factor(s), boundary dates."""
        if self.klass == KLASS_CONSTANT_FACTOR:
            return f"{self.symbol}: constant_factor x{self.factor:.4g} (intraday/daily over {self.common_sessions} sessions)"
        if self.klass == KLASS_FACTOR_CHANGES:
            segs = "; ".join(f"{s.first_day}..{s.last_day} x{s.factor:.4g}" for s in self.segments)
            return f"{self.symbol}: factor_changes [{segs}]"
        if self.klass == KLASS_NOISY:
            return f"{self.symbol}: noisy ({self.reason}; {self.common_sessions} sessions)"
        return f"{self.symbol}: {self.klass}" + (f" ({self.reason})" if self.reason else "")


def _rolling_median(x: np.ndarray, window: int) -> np.ndarray:
    return pd.Series(x).rolling(window, center=True, min_periods=1).median().to_numpy()


def _longest_run(mask: np.ndarray) -> int:
    """Length of the longest run of consecutive True in a boolean array."""
    if not mask.any():
        return 0
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return int((edges[1::2] - edges[::2]).max())


def _segments(x: np.ndarray, level: np.ndarray) -> List[Tuple[int, int, float]]:
    """Piecewise-constant levels of the log ratio ``x``: ``[(lo, hi_exclusive, median)]``.

    A boundary is where the rolling-median ``level`` steps by more than SEGMENT_JUMP; a run shorter than
    MIN_SEGMENT_SESSIONS is absorbed into its neighbour; neighbours whose medians differ by no more than
    SEGMENT_JUMP are merged; each boundary is then moved to the session that minimises the absolute error
    of the two medians around it (the rolling median crosses a step a few sessions off its true date)."""
    n = len(x)
    if n == 0:
        return []
    steps = np.abs(np.diff(level))
    cuts = np.flatnonzero(steps > SEGMENT_JUMP) + 1
    if len(cuts):
        merged: List[int] = []
        group = [int(cuts[0])]
        for c in cuts[1:]:
            if c - group[-1] <= _LEVEL_WINDOW:
                group.append(int(c))
            else:
                merged.append(max(group, key=lambda g: steps[g - 1]))
                group = [int(c)]
        merged.append(max(group, key=lambda g: steps[g - 1]))
        cuts = np.asarray(merged, dtype=np.int64)
    bounds = [0, *[int(c) for c in cuts], n]
    runs = [[bounds[i], bounds[i + 1]] for i in range(len(bounds) - 1)]
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for i, (lo, hi) in enumerate(runs):
            if hi - lo < MIN_SEGMENT_SESSIONS:
                if i == 0:
                    runs[1][0] = lo
                else:
                    runs[i - 1][1] = hi
                del runs[i]
                changed = True
                break
    out = [[lo, hi, float(np.median(x[lo:hi]))] for lo, hi in runs]
    merged_runs = [out[0]]
    for lo, hi, med in out[1:]:
        if abs(med - merged_runs[-1][2]) <= SEGMENT_JUMP:
            merged_runs[-1][1] = hi
            merged_runs[-1][2] = float(np.median(x[merged_runs[-1][0]:hi]))
        else:
            merged_runs.append([lo, hi, med])
    for k in range(1, len(merged_runs)):
        prev_med, next_med = merged_runs[k - 1][2], merged_runs[k][2]
        b0 = merged_runs[k][0]
        lo_b = max(merged_runs[k - 1][0] + 1, b0 - _LEVEL_WINDOW)
        hi_b = min(merged_runs[k][1] - 1, b0 + _LEVEL_WINDOW)
        best, best_cost = b0, None
        for cand in range(lo_b, hi_b + 1):
            cost = (np.abs(x[merged_runs[k - 1][0]:cand] - prev_med).sum()
                    + np.abs(x[cand:merged_runs[k][1]] - next_med).sum())
            if best_cost is None or cost < best_cost:
                best, best_cost = cand, cost
        merged_runs[k - 1][1] = best
        merged_runs[k][0] = best
    return [(lo, hi, float(np.median(x[lo:hi]))) for lo, hi, _ in merged_runs]


def _iso(d: Any) -> str:
    return str(np.datetime64(d, "D"))


def _min_run(level: float) -> int:
    """Sessions a displaced level of size ``level`` (|ln f|) must last to count (``LEVEL_TIERS``)."""
    for at_least, sessions in LEVEL_TIERS:
        if level >= at_least:
            return sessions
    return LEVEL_TIERS[-1][1]


def _mad(v: np.ndarray) -> float:
    return float(np.median(np.abs(v - np.median(v)))) if len(v) else 0.0


def classify(table: SessionTable, symbol: str, start: Any, end: Any) -> BasisResult:
    """Judge one symbol over ``[start, end]`` (inclusive days) from its session table. Pure.

    The verdict is about the LEVEL of the close ratio, not its scatter: the rolling median of
    ln(intraday last close / daily close) is displaced from 0 by more than BASIS_TOL for at least
    MIN_OFF_RUN consecutive sessions -> a basis defect (then the levels are segmented and reported);
    otherwise the symbol is ok, unless its scatter around its own level is wild (``noisy``)."""
    lo = np.datetime64(pd.Timestamp(start).date(), "D")
    hi = np.datetime64(pd.Timestamp(end).date(), "D")
    res = BasisResult(symbol=symbol, klass=KLASS_INSUFFICIENT, window_start=str(lo), window_end=str(hi))
    if len(table) == 0:
        res.klass = KLASS_NO_DAILY
        res.reason = "no daily bars"
        return res
    w = (table.days >= lo) & (table.days <= hi)
    res.daily_sessions = int(w.sum())
    if res.daily_sessions == 0:
        res.klass = KLASS_NO_DAILY
        res.reason = f"no daily bar in {lo}..{hi}"
        return res
    has = table.has_intraday[w]
    if not has.any():
        res.klass = KLASS_NO_INTRADAY
        res.no_intraday_sessions = res.daily_sessions
        res.reason = f"no intraday bar in {lo}..{hi}"
        return res
    res.no_intraday_sessions = int((~has).sum())
    comp = w & table.complete & np.isfinite(table.close_ratio)
    res.incomplete_sessions = int((w & table.has_intraday & ~comp).sum())
    days = table.days[comp]
    r = table.close_ratio[comp]
    res.common_sessions = int(len(r))
    if res.common_sessions < MIN_COMMON_SESSIONS:
        res.klass = KLASS_INSUFFICIENT
        res.reason = (f"{res.common_sessions} comparable sessions < {MIN_COMMON_SESSIONS} "
                      f"({res.no_intraday_sessions} daily sessions without intraday bars, "
                      f"{res.incomplete_sessions} incomplete)")
        return res
    x = np.log(r)
    res.median_close_ratio = float(np.exp(np.median(x)))
    hr, lr = table.high_ratio[comp], table.low_ratio[comp]
    res.median_high_ratio = float(np.nanmedian(hr)) if np.isfinite(hr).any() else float("nan")
    res.median_low_ratio = float(np.nanmedian(lr)) if np.isfinite(lr).any() else float("nan")
    out = np.abs(x) > SESSION_TOL
    res.out_share = float(out.mean())
    res.far_sessions = int((np.abs(x) >= FAR_TOL).sum())
    res.first_day, res.first_ratio = _iso(days[0]), float(r[0])
    res.last_day, res.last_ratio = _iso(days[-1]), float(r[-1])
    years = days.astype("datetime64[Y]").astype(int) + 1970
    for y in np.unique(years):
        m = years == y
        res.by_year[int(y)] = {
            "sessions": int(m.sum()),
            "median_close_ratio": float(np.exp(np.median(x[m]))),
            "out_share": float(out[m].mean()),
            "first_ratio": float(r[m][0]), "last_ratio": float(r[m][-1]),
        }
    level = _rolling_median(x, _LEVEL_WINDOW)
    displaced = np.abs(level) > BASIS_TOL
    if _longest_run(displaced) < MIN_OFF_RUN:
        spread = _mad(x)
        if spread > NOISY_MAD:
            res.klass = KLASS_NOISY
            res.reason = f"median level at 1 but scatter MAD {spread:.3f} > {NOISY_MAD}"
        else:
            res.klass = KLASS_OK
        return res
    segs = _segments(x, level)
    res.segments = [Segment(_iso(days[lo_i]), _iso(days[hi_i - 1]), int(hi_i - lo_i), float(np.exp(med)))
                    for lo_i, hi_i, med in segs]
    clean_off = [sg for sg, (lo_i, hi_i, _med) in zip(res.segments, segs)
                 if abs(np.log(sg.factor)) > BASIS_TOL
                 and sg.sessions >= _min_run(abs(np.log(sg.factor)))
                 and _mad(x[lo_i:hi_i]) <= SEGMENT_MAD_TOL]
    if clean_off:
        if len(res.segments) == 1:
            res.klass = KLASS_CONSTANT_FACTOR
            res.factor = res.segments[0].factor
        else:
            res.klass = KLASS_FACTOR_CHANGES
            res.factor = res.segments[-1].factor
        return res
    res.segments = []
    far = float(np.abs(level[displaced]).max())
    if far >= FAR_TOL:
        res.klass = KLASS_NOISY
        res.reason = (f"the level is displaced by up to x{np.exp(far):.3g} for a stretch but never settles on "
                      f"one level (scatter MAD > {SEGMENT_MAD_TOL} within every run): not one instrument's prices")
        return res
    # Stretches of scatter below FAR_TOL around a median of 1: a thin name's closing auction, not a basis.
    res.klass = KLASS_OK
    res.reason = f"scatter stretches up to {np.exp(far) - 1:.1%} off, no consistent level"
    return res


# --------------------------------------------------------------------------------------------------
# Files, identity, memo
# --------------------------------------------------------------------------------------------------
def _cache_root() -> str:
    """The cache folder the OHLCV readers/writers use (``native_cache.CACHE_FOLDER``, read at call time so a
    test that rebinds it gets its own)."""
    from ba2_common.core import native_cache
    return native_cache.CACHE_FOLDER


def ohlcv_cache_dir(provider: str = "FMPOHLCVProvider") -> str:
    return os.path.join(_cache_root(), provider)


def _find(folder: str, symbol: str, spellings: Sequence[str]) -> Optional[str]:
    for sp in spellings:
        p = os.path.join(folder, f"{symbol.upper()}_{sp}.parquet")
        if os.path.exists(p):
            return p
    return None


_DAILY_SPELLINGS = ("1d", "1day", "daily")


def symbol_files(symbol: str, interval: str, folder: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """``(daily path, intraday path)`` for a symbol; each None when absent. Alias spellings resolve
    exactly as ``native_cache.find_timeseries_path`` does (canonical short form first)."""
    folder = folder or ohlcv_cache_dir()
    from ba2_common.core.native_cache import _INTERVAL_ALIASES, normalize_interval
    canon = normalize_interval(interval)
    return (_find(folder, symbol, _DAILY_SPELLINGS),
            _find(folder, symbol, _INTERVAL_ALIASES.get(canon, [canon])))


def _identity(path: Optional[str]) -> Optional[Tuple[int, int]]:
    """``(size, mtime_ns)``: the identity of a cache file. The parquet files carry no content hash and
    every writer replaces them atomically (temp + rename), so a changed file changes one of the two."""
    if path is None:
        return None
    st = os.stat(path)
    return (int(st.st_size), int(st.st_mtime_ns))


class MemoDir:
    """On-disk memo of a symbol's SessionTable: ``<dir>/<SYM>_<interval>.npz`` holding the identity of
    the two source files, the algorithm version and the arrays. Atomic write; a corrupt or stale file
    is simply a miss. Lives NEXT TO the cache folder (``<BA2_HOME>/common/cross_interval_basis``), never
    inside it: the cache folder is the vendor data and this is derived. ``MemoDir()`` resolves that
    location when it is first used, so a test that rebinds ``CACHE_FOLDER`` gets its own."""

    def __init__(self, directory: Optional[str] = None):
        self._directory = directory

    @property
    def directory(self) -> str:
        if self._directory is None:
            return os.path.join(os.path.dirname(os.path.normpath(_cache_root())), "cross_interval_basis")
        return self._directory

    def _path(self, symbol: str, interval: str) -> str:
        return os.path.join(self.directory, f"{symbol.upper()}_{interval.lower()}.npz")

    # File layout (3 uncompressed arrays: opening an .npz member costs ~1 ms, decompressing and 15 members
    # cost ~80 ms per symbol, which dominated a warm scan of 7 000 symbols):
    #   sig   int64 [ALGORITHM_VERSION, daily size, daily mtime_ns, intraday size, intraday mtime_ns]
    #   dates int64 [daily_first, daily_last, intraday_first, intraday_last] as date ordinals (-1 = none)
    #   data  float64 (n, 5): session day (days since 1970-01-01), close_ratio, high_ratio, low_ratio,
    #         flags (bit 0 = has_intraday, bit 1 = complete)
    def load(self, symbol: str, interval: str, sig: tuple) -> Optional[SessionTable]:
        p = self._path(symbol, interval)
        try:
            with np.load(p, allow_pickle=False) as z:
                if tuple(int(v) for v in z["sig"]) != _flat(sig):
                    return None
                data, dates = z["data"], z["dates"]
        except (OSError, ValueError, KeyError, EOFError):
            return None
        flags = data[:, 4].astype(np.int64)
        f = lambda v: None if int(v) < 0 else date.fromordinal(int(v))   # noqa: E731
        return SessionTable(
            days=data[:, 0].astype(np.int64).astype("datetime64[D]"), close_ratio=data[:, 1],
            high_ratio=data[:, 2], low_ratio=data[:, 3], has_intraday=(flags & 1) != 0,
            complete=(flags & 2) != 0, daily_first=f(dates[0]), daily_last=f(dates[1]),
            intraday_first=f(dates[2]), intraday_last=f(dates[3]))

    def save(self, symbol: str, interval: str, sig: tuple, t: SessionTable) -> None:
        os.makedirs(self.directory, exist_ok=True)
        p = self._path(symbol, interval)
        tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp.npz"
        o = lambda d: -1 if d is None else d.toordinal()   # noqa: E731
        data = np.column_stack([
            t.days.astype("datetime64[D]").astype(np.int64).astype(np.float64), t.close_ratio, t.high_ratio,
            t.low_ratio, t.has_intraday.astype(np.int64) + 2 * t.complete.astype(np.int64)]) \
            if len(t) else np.empty((0, 5), dtype=np.float64)
        try:
            np.savez(tmp, sig=np.asarray(_flat(sig), dtype=np.int64),
                     dates=np.asarray([o(t.daily_first), o(t.daily_last), o(t.intraday_first),
                                       o(t.intraday_last)], dtype=np.int64), data=data)
            os.replace(tmp, p)
        except OSError:
            # a memo that cannot be written only costs a rescan next time; it never changes a verdict
            try:
                os.remove(tmp)
            except OSError:
                pass


def _flat(sig: tuple) -> Tuple[int, ...]:
    out: List[int] = []
    for part in sig:
        if isinstance(part, tuple):
            out.extend(int(v) for v in part)
        else:
            out.append(-1 if part is None else int(part))
    return tuple(out)


def _tail_problem(table: SessionTable, symbol: str) -> Optional[BasisResult]:
    """The write-time TAIL check (``TAIL_SESSIONS``): a ``factor_changes`` result when the last comparable
    sessions of a frame sit more than FAR_TOL from the daily level, else None."""
    comp = table.complete & np.isfinite(table.close_ratio)
    if not comp.any():
        return None
    days, r = table.days[comp][-TAIL_SESSIONS:], table.close_ratio[comp][-TAIL_SESSIONS:]
    med = float(np.median(np.log(r)))
    if abs(med) < FAR_TOL:
        return None
    f = float(np.exp(med))
    return BasisResult(
        symbol=symbol, klass=KLASS_FACTOR_CHANGES, common_sessions=int(comp.sum()), factor=f,
        median_close_ratio=f, first_day=_iso(days[0]), last_day=_iso(days[-1]),
        segments=[Segment(_iso(days[0]), _iso(days[-1]), int(len(r)), f)],
        reason=f"the last {len(r)} comparable sessions sit at x{f:.4g} of the daily cache")


#: Session tables a store keeps in process memory (LRU): ~50 KB each, so a 1 368-symbol universe would
#: otherwise add ~70 MB to EVERY pool process of a GA worker. The on-disk memo (an ``.npz`` load of ~1 ms)
#: serves everything the LRU has dropped; the per-job verdict memo (``intraday_basis_preflight``) means a
#: trial does not come back here at all.
MEM_TABLES_MAX = 256


class BasisStore:
    """Per-process access to the per-symbol session tables of ONE provider's cache folder, with the memo
    layers:

    1. in-process dict keyed by ``(symbol, interval, identity of both files, ALGORITHM_VERSION)``;
    2. the ``.npz`` memo (``MemoDir``) with the same key, which survives processes and jobs;
    3. only then the parquet files are read.

    The identity is ``(size, mtime_ns)`` of the daily and the intraday file: every writer replaces a file
    atomically, so a rewritten file changes it, and an untouched file is never rescanned.
    ``reads`` counts the parquet reductions actually done (the test of "no rescan per trial").
    ``memo=None`` disables the on-disk layer (tests); ``folder=None`` resolves the provider's cache
    folder at each call."""

    def __init__(self, provider: str = "FMPOHLCVProvider", memo: Optional[MemoDir] = None,
                 folder: Optional[str] = None):
        self.provider = provider
        self.memo = memo
        self._folder = folder
        self._mem: "OrderedDict[tuple, SessionTable]" = OrderedDict()
        self._lock = threading.Lock()
        self.reads = 0
        self.memo_hits = 0
        self.mem_hits = 0

    @property
    def folder(self) -> str:
        return self._folder or ohlcv_cache_dir(self.provider)

    def table(self, symbol: str, interval: str) -> Tuple[Optional[SessionTable], str]:
        """``(SessionTable or None, why)``: None when a file is absent (``why`` names which)."""
        daily_p, intra_p = symbol_files(symbol, interval, self.folder)
        if daily_p is None:
            return None, KLASS_NO_DAILY
        if intra_p is None:
            return None, KLASS_NO_INTRADAY
        sig = (ALGORITHM_VERSION, _identity(daily_p), _identity(intra_p))
        key = (self.folder, symbol.upper(), interval.lower(), _flat(sig))
        with self._lock:
            hit = self._mem.get(key)
            if hit is not None:
                self._mem.move_to_end(key)
        if hit is not None:
            self.mem_hits += 1
            return hit, ""
        t = self.memo.load(symbol, interval, sig) if self.memo else None
        if t is not None:
            self.memo_hits += 1
        else:
            daily = pd.read_parquet(daily_p, columns=["Date", "High", "Low", "Close"])
            intra = pd.read_parquet(intra_p, columns=["Date", "High", "Low", "Close"])
            t = reduce_sessions(daily, intra, interval)
            self.reads += 1
            if self.memo:
                self.memo.save(symbol, interval, sig, t)
        with self._lock:
            self._mem[key] = t
            while len(self._mem) > MEM_TABLES_MAX:
                self._mem.popitem(last=False)
        return t, ""

    def check(self, symbol: str, interval: str, start: Any, end: Any) -> BasisResult:
        """Judge the files ON DISK over ``[start, end]`` (inclusive days)."""
        t, why = self.table(symbol, interval)
        if t is None:
            return BasisResult(symbol=symbol, klass=why, window_start=str(pd.Timestamp(start).date()),
                               window_end=str(pd.Timestamp(end).date()),
                               reason=("no daily file" if why == KLASS_NO_DAILY else "no intraday file"))
        return classify(t, symbol, start, end)

    def check_frame(self, symbol: str, interval: str, frame: pd.DataFrame,
                    start: Any = None, end: Any = None) -> BasisResult:
        """Judge an intraday FRAME (about to be written) against the daily file on disk. Not memoised
        (the frame is new data); ``start``/``end`` default to the frame's own range."""
        daily_p, _ = symbol_files(symbol, interval, self.folder)
        if daily_p is None:
            return BasisResult(symbol=symbol, klass=KLASS_NO_DAILY, reason="no daily file")
        if frame is None or len(frame) == 0:
            return BasisResult(symbol=symbol, klass=KLASS_NO_INTRADAY, reason="empty frame")
        daily = pd.read_parquet(daily_p, columns=["Date", "High", "Low", "Close"])
        t = reduce_sessions(daily, frame[["Date", "High", "Low", "Close"]], interval)
        lo = start if start is not None else t.intraday_first
        hi = end if end is not None else t.intraday_last
        if lo is None or hi is None:
            return BasisResult(symbol=symbol, klass=KLASS_NO_INTRADAY, reason="no regular-session bar in the frame")
        res = classify(t, symbol, lo, hi)
        if res.mismatched:
            return res
        tail = _tail_problem(t, symbol)
        return tail if tail is not None else res


_STORES: Dict[tuple, BasisStore] = {}
_STORES_LOCK = threading.Lock()


def store_for(provider: str = "FMPOHLCVProvider", memo_dir: Optional[str] = None) -> BasisStore:
    """The process-wide store of one provider's cache folder. ``memo_dir=None``: the default on-disk memo
    (``<BA2_HOME>/common/cross_interval_basis``); a path: that directory; ``""``: no on-disk memo."""
    with _STORES_LOCK:
        st = _STORES.get((provider, memo_dir))
        if st is None:
            memo = None if memo_dir == "" else MemoDir(memo_dir)
            st = _STORES[(provider, memo_dir)] = BasisStore(provider, memo=memo)
        return st


def default_store() -> BasisStore:
    return store_for("FMPOHLCVProvider")


def check_symbol(symbol: str, interval: str, start: Any, end: Any,
                 store: Optional[BasisStore] = None) -> BasisResult:
    return (store or default_store()).check(symbol, interval, start, end)


def _check_chunk(args: tuple) -> List[BasisResult]:
    provider, memo_dir, interval, start, end, symbols = args
    st = store_for(provider, memo_dir)
    return [st.check(s, interval, start, end) for s in symbols]


def check_many(symbols: Iterable[str], interval: str, start: Any, end: Any, *,
               provider: str = "FMPOHLCVProvider", workers: int = 1, memo_dir: Optional[str] = None,
               store: Optional[BasisStore] = None) -> List[BasisResult]:
    """Judge many symbols; results in the order of ``symbols``.

    ``workers > 1`` spreads the symbols over a PROCESS pool (the cold cost is the parquet read, which a
    thread pool does not overlap well) -- for tools and the launcher; a daemonic pool child cannot start
    children, so engine code calls it with ``workers=1`` (it still benefits from the on-disk memo that the
    launcher's scan warmed). Each process visits the symbols in its own pseudo-random order, so several
    processes scanning the same universe cold split the work through the shared memo instead of all doing
    all of it."""
    syms = list(symbols)
    if store is not None or workers <= 1 or len(syms) < 2 * workers:
        st = store or store_for(provider, memo_dir)
        order = list(range(len(syms)))
        random.Random(os.getpid()).shuffle(order)
        out: Dict[int, BasisResult] = {i: st.check(syms[i], interval, start, end) for i in order}
        return [out[i] for i in range(len(syms))]
    from concurrent.futures import ProcessPoolExecutor
    chunks = [syms[i::workers * 4] for i in range(workers * 4)]
    chunks = [c for c in chunks if c]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        parts = list(ex.map(_check_chunk, [(provider, memo_dir, interval, start, end, c) for c in chunks]))
    by_symbol = {r.symbol: r for part in parts for r in part}
    return [by_symbol[s] for s in syms]


def check_symbols(symbols: Iterable[str], interval: str, start: Any, end: Any,
                  store: Optional[BasisStore] = None) -> List[BasisResult]:
    return check_many(symbols, interval, start, end, store=store)
