"""Point-in-time DAILY feature store for the ML exploration v2 (trains on (symbol, session) rows, 2016-2026).

Source: the Alpaca-built daily cache ``<cache>/AlpacaHistoryOHLCVProvider/<SYM>_1d.parquet`` only (no network, never
FMP prices). The decision clock is the session CLOSE: a row for session S is what a model knows when it decides at S's
close (the order then fills at S+1's open). So a row may use

  * the symbol's own bars up to and INCLUDING S (a daily bar is public at its close);
  * other symbols' bars up to and including S (same clock);
  * a weekly statistic only once its week has ended, i.e. from the next week's first session on.

Nothing here may read a bar dated after S. The proof is the CUT TEST (tests/test_ml_features.py): build from a cache
truncated after day D and every common row, every column, is identical. The one column that deliberately looks one
session ahead is ``label_bar_filled`` (is the fill bar of a decision at S a fabricated one?): it is a label ingredient
for the trainer, not a feature, and has no group prefix.

Every symbol is loaded from its FIRST cached bar whatever ``first_day`` is, so a row's value never depends on where the
request starts (recursive indicators - Wilder, EMA, SAR, OBV - are seeded at the symbol's first bar, always). Rows
before the first bar are unlisted and NaN; the early rows after it are NaN until each window is full (a 200-session
average is NaN for the first 199 sessions of a symbol).

Feature groups are column prefixes registered in ``GROUPS`` (``t_`` technical, ``mtf_`` multi-timeframe, ``m_`` market)
so they can be switched on and off as grid factors. A ``GroupSpec`` holds the builder ``(daily, ctx) -> DataFrame``
indexed ``[symbol, session]`` (exactly ``ctx.symbols`` x ``ctx.sessions``, float columns, every column carrying the
prefix), whether it needs a sector map, and the context symbols it needs loaded (``build_features`` adds them, the
caller does not assemble ETF lists). Later groups (cross-sectional, macro, fundamentals, analyst, insider, structure)
register themselves the same way. Every column is scale-free (log returns, ratios, ATR multiples, z-scores), never a
price level.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

import numpy as np
import pandas as pd

from ba2_providers.macro import fred_series as fs
from ba2_providers.screener import metric_store as ms

from app.services import ml_holdout, ml_universe
from app.services.ml_panel import PanelDataError, ema, regular_sessions, true_range, wilder

#: The longest run of consecutive sessions missing BETWEEN two cached bars that is filled flat (previous close, volume
#: 0, ``filled`` True, listed in the load meta). A longer run is refused. The rule is local, so the result never
#: depends on the length of the window loaded.
MAX_FILLED_RUN = 2
#: A series whose last cached bar is more than this many sessions before the cache's newest bar has ENDED (delisted);
#: 1..this many sessions short is a stale download and refuses.
ENDED_AFTER_SESSIONS = 10
BAR_COLUMNS: Tuple[str, ...] = ("open", "high", "low", "close", "volume")
_RAW = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
_INDEX_NAMES = ["symbol", "session"]


class StaleSeriesError(PanelDataError):
    """A series that stops short of the cache end (1..``ENDED_AFTER_SESSIONS`` sessions) or that the metric store still lists
    after its last bar: a stale download that can clear after a cache refresh, NOT an ended (delisted) series."""


# ------------------------------------------------------------------------------------------ daily bars
def _cache_file(cache_root: Path, symbol: str) -> Path:
    p = Path(cache_root) / "AlpacaHistoryOHLCVProvider" / f"{symbol}_1d.parquet"
    if not p.exists():
        raise PanelDataError(f"no cached daily bars for {symbol}: {p}")
    return p


def _read_daily(symbol: str, cache_root: Path) -> pd.DataFrame:
    """One symbol's raw daily bars indexed by their (naive, midnight) session date. Refuses what it cannot trust."""
    p = _cache_file(cache_root, symbol)
    df = pd.read_parquet(p, columns=["Date", *_RAW])
    if df.empty:
        raise PanelDataError(f"{symbol}: the cached daily file is empty: {p}")
    if getattr(df["Date"].dt, "tz", None) is not None:
        raise PanelDataError(f"{symbol}: Date is timezone-aware; expected naive session dates ({p})")
    if (df["Date"] != df["Date"].dt.normalize()).any():
        raise PanelDataError(f"{symbol}: Date carries a time of day; expected one bar per session date ({p})")
    if df["Date"].duplicated().any():
        raise PanelDataError(f"{symbol}: duplicate daily bars, first {df.loc[df['Date'].duplicated(), 'Date'].iloc[0].date()}")
    if df[list(_RAW)].isna().any().any():
        raise PanelDataError(f"{symbol}: NaN in a cached daily bar ({p}); refusing to guess")
    df = df.sort_values("Date")
    bad = (df["Close"] <= 0) | (df["High"] < df["Low"])
    if bad.any():
        first = df.loc[bad, "Date"].iloc[0].date()
        raise PanelDataError(f"{symbol}: {int(bad.sum())} invalid daily bars (close <= 0 or high < low), first {first} ({p})")
    df = df.rename(columns=_RAW).set_index("Date")
    df.index = pd.DatetimeIndex(df.index.astype("datetime64[ns]"), name="session")
    df["volume"] = df["volume"].astype("float64")
    return df


def _date_column(symbol: str, cache_root: Path) -> pd.Series:
    dates = pd.read_parquet(_cache_file(cache_root, symbol), columns=["Date"])["Date"]
    if dates.empty:
        raise PanelDataError(f"{symbol}: the cached daily file is empty")
    return dates


def first_cached_bar(symbol: str, cache_root: Path) -> pd.Timestamp:
    """Date of the symbol's first cached daily bar (reads the Date column only)."""
    return pd.Timestamp(_date_column(symbol, cache_root).min()).normalize()


def last_cached_bar(symbol: str, cache_root: Path) -> date:
    """Date of the symbol's LAST cached daily bar; SPY's is the cache end that ``ended`` is measured against."""
    return pd.Timestamp(_date_column(symbol, cache_root).max()).normalize().date()


@lru_cache(maxsize=64)
def _sessions_index(first: date, last: date) -> pd.DatetimeIndex:
    """The NYSE sessions of ``[first, last]`` (memoised: callers never mutate the index)."""
    return pd.DatetimeIndex(pd.to_datetime(regular_sessions(first, last)["session"]), name="session")


def _longest_run(missing: np.ndarray) -> Tuple[int, int]:
    """(start position, length) of the longest run of True in ``missing``; (0, 0) when there is none."""
    best, best_start, run, start = 0, 0, 0, 0
    for i, m in enumerate(missing):
        if m:
            if run == 0:
                start = i
            run += 1
            if run > best:
                best, best_start = run, start
        else:
            run = 0
    return best_start, best


def load_daily(symbols: Iterable[str], first: date, last: date, cache_root: Path,
               cache_end: Optional[date] = None) -> Tuple[Dict[str, pd.DataFrame], Dict[str, object]]:
    """Daily OHLCV per symbol on the NYSE sessions of ``[first, last]`` (``ml_panel.regular_sessions``).

    Each frame is indexed by ``session`` (naive midnight timestamps) with columns open, high, low, close, volume,
    ``listed``, ``filled`` and ``ended``:

    * before the symbol's first cached bar: OHLCV are NaN and ``listed`` is False (it did not exist yet, so no
      feature may be built there). A cache that starts before ``first`` is listed from the first session;
    * a session missing BETWEEN two cached bars is filled flat (OHLC = previous close, volume 0, ``filled`` True) and
      listed in ``meta["filled"]``, but a run of more than ``MAX_FILLED_RUN`` consecutive missing sessions refuses.
      A run that straddles ``first`` is measured whole (from the last bar before the range);
    * a session AFTER the symbol's last cached bar is never filled. With ``cache_end`` (the newest bar date of the
      whole cache, SPY's) a series whose last bar is MORE than ``ENDED_AFTER_SESSIONS`` sessions before it has ENDED
      (delisted, acquired): its rows after the last bar are NaN with ``listed`` False and ``ended`` True. A series
      that is 1..``ENDED_AFTER_SESSIONS`` sessions short is STALE and refuses; so does any tail when ``cache_end``
      is None;
    * a bar on a date that is not an NYSE session, a duplicate, a NaN, ``close <= 0``, ``high < low`` or a
      timezone-aware Date refuses.

    Returns ``(daily, meta)`` with ``meta = {"filled": {sym: [iso date, ...]}, "first_bar": {sym: iso date},
    "ended": {sym: iso date of the last bar}}`` (``first_bar`` is the symbol's first bar in the cache file, whatever
    the range; ``ended`` lists the series that ended before the range's ``last``)."""
    symbols = list(symbols)
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"duplicate symbols: {sorted({s for s in symbols if symbols.count(s) > 1})}")
    sessions = _sessions_index(first, last)
    calendars: Dict[date, pd.DatetimeIndex] = {}
    daily: Dict[str, pd.DataFrame] = {}
    meta: Dict[str, object] = {"filled": {}, "first_bar": {}, "ended": {}}
    for sym in symbols:
        raw = _read_daily(sym, cache_root)
        meta["first_bar"][sym] = str(raw.index[0].date())
        inside = raw[(raw.index >= sessions[0]) & (raw.index <= sessions[-1])]
        off = inside.index.difference(sessions)
        if len(off):
            raise PanelDataError(f"{sym}: {len(off)} daily bars not on the NYSE calendar, first {off[0].date()}")
        listed = np.asarray(sessions >= raw.index[0])
        if not listed.any():                                         # first bar after the range: not listed yet
            d = inside.reindex(sessions)
            d["listed"], d["filled"] = listed, np.zeros(len(sessions), dtype=bool)
            d["ended"] = np.zeros(len(sessions), dtype=bool)
            daily[sym] = d
            continue
        have_ts = raw[raw.index <= sessions[-1]].index[-1]                     # the last bar the range can see
        ended = False
        if inside.empty or inside.index[-1] < sessions[-1]:
            short = 0
            if cache_end is not None and cache_end > have_ts.date():
                short = len(_sessions_index(have_ts.date(), cache_end)) - 1       # sessions between the last bar and the end
            ended = short > ENDED_AFTER_SESSIONS
            if not ended:
                gap = sessions[sessions > have_ts]
                raise StaleSeriesError(
                    f"{sym}: stale series, last cached bar {have_ts.date()} but {len(gap)} sessions up to "
                    f"{sessions[-1].date()} have none" + (
                        f"; it is {short} sessions short of the cache end {cache_end}: too short to count as ended "
                        f"(ended needs more than {ENDED_AFTER_SESSIONS})" if cache_end is not None else
                        "; no cache end was given, so no tail can count as ended"))
            meta["ended"][sym] = str(have_ts.date())
        before = raw[raw.index < sessions[0]]
        start = before.index[-1] if len(before) else sessions[0]
        if start not in calendars:
            calendars[start] = _sessions_index(start.date(), last)
        cal = calendars[start]
        d = raw.reindex(cal)
        live = np.asarray((cal >= raw.index[0]) & (cal <= have_ts))
        missing = d["close"].isna().to_numpy() & live
        if missing.any():
            at, n = _longest_run(missing)
            if n > MAX_FILLED_RUN:
                raise PanelDataError(f"{sym}: {n} consecutive sessions missing from {cal[at].date()} "
                                     f"(more than {MAX_FILLED_RUN}); refusing to fill")
            prev = d["close"].ffill()
            for c in ("open", "high", "low", "close"):
                d.loc[missing, c] = prev[missing]
            d.loc[missing, "volume"] = 0.0
        d["listed"] = live
        d["filled"] = missing
        d["ended"] = np.asarray(cal > have_ts) if ended else np.zeros(len(cal), dtype=bool)
        d = d[d.index >= sessions[0]]
        if missing[np.asarray(cal >= sessions[0])].any():
            meta["filled"][sym] = [str(t.date()) for t in d.index[d["filled"].to_numpy()]]
        daily[sym] = d
    return daily, meta


# ------------------------------------------------------------------------------------------ build context / registry
@dataclass(frozen=True)
class BuildContext:
    """What every group builder needs besides the loaded bars.

    ``symbols`` are the rows being built (in output order); ``universe`` is the symbol set cross-sectional statistics
    (breadth, later ranks) are taken over - a superset of ``symbols``, so a build in chunks gives the same values as
    one build; ``context_symbols`` is everything else loaded (indices, sector ETFs); ``sessions`` is the whole loaded
    window; ``sector_of`` maps a symbol to its sector (groups with ``needs_sector``); ``cache_root`` lets a later group
    read its own stores; ``store_dir`` is the screener metric store (groups X and F). ``membership`` (sessions x
    universe, bool) says who is in the universe on each session (None: every universe symbol, always);
    ``sector_overrides`` are the caller's explicit sectors, which win over the store's."""
    symbols: Tuple[str, ...]
    universe: Tuple[str, ...]
    context_symbols: Tuple[str, ...]
    sessions: pd.DatetimeIndex
    first_day: date
    last_day: date
    cache_root: Path
    store_dir: Path
    sector_of: Dict[str, str] = field(default_factory=dict)
    membership: Optional[pd.DataFrame] = None
    sector_overrides: Dict[str, str] = field(default_factory=dict)
    provenance: Dict[str, object] = field(default_factory=dict)      # filled by groups: what files they read
    cache: Optional["BuildCache"] = field(default=None, compare=False, repr=False)

    def member_mask(self) -> pd.DataFrame:
        """(sessions x universe) bool: who is in the cross-section on each session."""
        if self.membership is None:
            return pd.DataFrame(True, index=self.sessions, columns=list(self.universe))
        return self.membership


Builder = Callable[[Dict[str, pd.DataFrame], BuildContext], pd.DataFrame]
ContextFn = Callable[[Sequence[str], Optional[Dict[str, str]]], Set[str]]


def _no_context(symbols: Sequence[str], sector_of: Optional[Dict[str, str]]) -> Set[str]:
    return set()



class BuildCache:
    """Loaded daily frames and per-symbol technical features shared by several ``build_features`` calls over ONE window
    (``preloaded=``), e.g. a 300-name universe built in chunks: each symbol is read and its indicators computed once.

    The window is fixed by the first build (its ``last_day``, cache root and cache end, and its load start); a later
    build for another ``last_day`` / cache, or one that needs a symbol whose first bar is before the window start,
    raises (build the first chunk with the earliest ``first_day``, or preload everything at once). Values are the same
    as without the cache."""

    def __init__(self):
        self.window: Optional[Tuple[str, str, str]] = None
        self.first: Optional[date] = None
        self.daily: Dict[str, pd.DataFrame] = {}
        self.meta: Dict[str, Dict[str, object]] = {"filled": {}, "first_bar": {}, "ended": {}}
        self._tech: Dict[str, pd.DataFrame] = {}
        self._memo: Dict[object, object] = {}

    def memo(self, key: object, compute: Callable[[], object]) -> object:
        """``compute()`` once per ``key`` for the life of the cache: tables that are the same for every chunk of one store build
        (the macro table, the S manifests, a universe member's X columns). The key must contain whatever the value depends on."""
        if key not in self._memo:
            self._memo[key] = compute()
        return self._memo[key]

    def load(self, symbols: Sequence[str], first: date, last: date, cache_root: Path,
             cache_end: date) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Dict[str, object]]]:
        window = (str(last), str(Path(cache_root).resolve()), str(cache_end))
        if self.window is None:
            self.window, self.first = window, first
        else:
            for name, mine, theirs in zip(("last_day", "cache_root", "cache_end"), self.window, window):
                if mine != theirs:
                    raise ValueError(f"the shared BuildCache was built for {name} {mine}, this build asks for {theirs}")
            if first < self.first:
                raise ValueError(f"the shared BuildCache starts at {self.first}; this build needs {first}: build the "
                                 f"first chunk with the earliest first_day")
        missing = [s for s in symbols if s not in self.daily]
        for sym in missing:
            if first_cached_bar(sym, cache_root).date() < self.first:
                raise ValueError(f"{sym} has bars before the shared BuildCache window starts ({self.first}); preload it "
                                 f"in the first build")
        if missing:
            frames, meta = load_daily(missing, self.first, last, cache_root, cache_end=cache_end)
            self.daily.update(frames)
            for k in self.meta:
                self.meta[k].update(meta[k])
        sub = {k: {s: v for s, v in self.meta[k].items() if s in symbols} for k in self.meta}
        return {s: self.daily[s] for s in symbols}, sub

    def technical(self, symbol: str, live: pd.DataFrame) -> pd.DataFrame:
        if symbol not in self._tech:
            self._tech[symbol] = _technical_one(live)
        return self._tech[symbol]


def _memoised(ctx: "BuildContext", key: object, compute: Callable[[], object]) -> object:
    return ctx.cache.memo(key, compute) if ctx.cache is not None else compute()


def _tech(ctx: "BuildContext", symbol: str, live: pd.DataFrame) -> pd.DataFrame:
    """``_technical_one`` of a symbol's listed bars, shared across chunked builds when a BuildCache is in use."""
    return ctx.cache.technical(symbol, live) if ctx.cache is not None else _technical_one(live)


@dataclass(frozen=True)
class GroupSpec:
    """A feature group: its ``builder``, its column ``prefix`` (``"t_"``), whether it needs ``sector_of``, and
    ``required_context(symbols, sector_of)``: the extra symbols whose bars the builder reads."""
    builder: Builder
    prefix: str
    needs_sector: bool = False
    required_context: ContextFn = _no_context
    #: the columns whose presence defines the group's coverage (``coverage_report``); None = all of them. A group lists only
    #: the columns that are non-NaN whenever its data exist: a column that is NaN BY DESIGN (no resistance level, no target
    #: in the window) must not make the whole group look uncovered.
    coverage_columns: Optional[Tuple[str, ...]] = None
    #: columns that are required for coverage EXCEPT for a symbol declared sectorless (``meta["sectorless"]``): they are NaN
    #: by design there, but NaN for a stock is a real hole that must count against it.
    sectorless_exempt: Tuple[str, ...] = ()


def _per_symbol(daily: Dict[str, pd.DataFrame], ctx: BuildContext,
                fn: Callable[[pd.DataFrame, str], pd.DataFrame]) -> pd.DataFrame:
    """Run ``fn`` on each built symbol's LISTED bars and put the result on the full session grid: rows before the
    listing are NaN (an indicator is never seeded from a symbol that did not exist yet)."""
    frames: Dict[str, pd.DataFrame] = {}
    for sym in ctx.symbols:
        d = daily[sym]
        frames[sym] = fn(d[d["listed"]], sym).reindex(ctx.sessions).astype("float64")
    return pd.concat(frames, names=_INDEX_NAMES)


# ------------------------------------------------------------------------------------------ indicator helpers (causal)
def _rsi_scaled(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI scaled to [-0.5, 0.5]. Unlike ``ml_panel._rsi`` it is defined when there is no down move (100) or
    no move at all (50) instead of NaN."""
    d = close.diff()
    up, dn = wilder(d.clip(lower=0), n), wilder((-d).clip(lower=0), n)
    rsi = 100.0 - 100.0 / (1.0 + up / dn)
    rsi = rsi.where(dn != 0, pd.Series(np.where(up > 0, 100.0, 50.0), index=up.index))
    return (rsi / 100.0 - 0.5).where(up.notna() & dn.notna())


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's ADX in 0..100 (Wilder-smoothed +DM / -DM / TR, then Wilder-smoothed DX). ``app.indicators`` smooths
    with a plain rolling mean, which is causal but not Wilder's ADX; this is the textbook one."""
    up, dn = high.diff(), -low.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0).where(up.notna())
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0).where(dn.notna())
    tr = true_range(high, low, close).where(close.shift(1).notna())
    atr = wilder(tr, n)
    pdi, mdi = 100.0 * wilder(plus_dm, n) / atr, 100.0 * wilder(minus_dm, n) / atr
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi)
    return wilder(dx, n)


def _parabolic_sar(high: np.ndarray, low: np.ndarray, af_start: float = 0.02, af_max: float = 0.2) -> np.ndarray:
    """Standard parabolic SAR, a numpy port of ``TechnicalIndicators.calculate_sar`` (same recursion, checked equal in
    tests). Causal: SAR[i] is a function of bars 0..i, seeded at the symbol's first bar (always the same seed, since
    every symbol is loaded from its first cached bar)."""
    n = len(high)
    sar = np.full(n, np.nan)
    if n == 0:
        return sar
    sar[0] = low[0]
    af, up, ep = af_start, True, high[0]
    for i in range(1, n):
        prev2_lo = low[i - 2] if i >= 2 else low[i - 1]
        prev2_hi = high[i - 2] if i >= 2 else high[i - 1]
        s = sar[i - 1] + af * (ep - sar[i - 1])
        if up:
            s = min(s, low[i - 1], prev2_lo)
            if low[i] < s:
                up, s, ep, af = False, ep, low[i], af_start
            elif high[i] > ep:
                ep, af = high[i], min(af + af_start, af_max)
        else:
            s = max(s, high[i - 1], prev2_hi)
            if high[i] > s:
                up, s, ep, af = True, ep, high[i], af_start
            elif low[i] < ep:
                ep, af = low[i], min(af + af_start, af_max)
        sar[i] = s
    return sar


def _rolling_slope(y: pd.Series, n: int) -> pd.Series:
    """Least-squares slope of the last ``n`` values against 0..n-1 (per step), NaN until ``n`` values exist."""
    arr = y.to_numpy(dtype="float64")
    out = np.full(len(arr), np.nan)
    if len(arr) >= n:
        x = np.arange(n, dtype="float64") - (n - 1) / 2.0
        out[n - 1:] = np.lib.stride_tricks.sliding_window_view(arr, n) @ x / (x @ x)
    return pd.Series(out, index=y.index)


def _roll(x: pd.Series, n: int):
    return x.rolling(n, min_periods=n)


def _roll_missing_ok(x: pd.Series, n: int):
    """Rolling window of ``n`` sessions that tolerates the volumes of filled bars being NaN: defined once the window
    is full (``n`` sessions of history) and at most ``MAX_FILLED_RUN`` of them are NaN."""
    return x.rolling(n, min_periods=n - MAX_FILLED_RUN)


def _full_windows_only(stat: pd.Series, n: int) -> pd.Series:
    out = stat.copy()
    out.iloc[:n - 1] = np.nan                       # fewer than n sessions of history: not a window yet
    return out


# ------------------------------------------------------------------------------------------ group T (daily technical)
T_COLUMNS: Tuple[str, ...] = (
    "t_ret_1", "t_ret_5", "t_ret_20", "t_ret_60", "t_rvol_20", "t_rvol_60", "t_atr_pct", "t_rsi", "t_macd_hist",
    "t_bb_pctb", "t_bb_width", "t_dist_sma20", "t_dist_sma50", "t_dist_sma200", "t_adx", "t_sar_dist",
    "t_donch_20", "t_donch_55", "t_obv_slope", "t_pivot_dist", "t_vol_z")


def _technical_one(d: pd.DataFrame) -> pd.DataFrame:
    """Daily technical features of one symbol, row S from bars up to and including S only (every operation below is a
    trailing window, an exponential recursion, or a ONE-session lag; nothing looks forward).

    The volume of a FILLED bar is fabricated (0): it is NaN in the two volume statistics (``t_vol_z``,
    ``t_obv_slope``), which skip it (up to ``MAX_FILLED_RUN`` per window) instead of reading log1p(0)."""
    h, l, c = d["high"], d["low"], d["close"]
    v = d["volume"].where(~d["filled"])
    lr = np.log(c)
    r1 = lr.diff()
    atr = wilder(true_range(h, l, c), 14)
    out = pd.DataFrame(index=d.index)
    for n in (1, 5, 20, 60):
        out[f"t_ret_{n}"] = lr.diff(n)
    out["t_rvol_20"], out["t_rvol_60"] = _roll(r1, 20).std(), _roll(r1, 60).std()
    out["t_atr_pct"] = atr / c
    out["t_rsi"] = _rsi_scaled(c)
    macd = ema(c, 12) - ema(c, 26)
    out["t_macd_hist"] = (macd - ema(macd, 9)) / atr
    mid, sd = _roll(c, 20).mean(), _roll(c, 20).std()
    out["t_bb_pctb"] = (c - (mid - 2 * sd)) / (4 * sd) - 0.5
    out["t_bb_width"] = 4 * sd / mid
    for n in (20, 50, 200):
        out[f"t_dist_sma{n}"] = (c - _roll(c, n).mean()) / atr
    out["t_adx"] = _adx(h, l, c) / 100.0
    out["t_sar_dist"] = (c - _parabolic_sar(h.to_numpy(), l.to_numpy())) / atr
    for n in (20, 55):
        hi, lo = _roll(h, n).max(), _roll(l, n).min()
        out[f"t_donch_{n}"] = (c - lo) / (hi - lo) - 0.5
    obv = (np.sign(c.diff()).fillna(0.0) * v.fillna(0.0)).cumsum()       # a filled bar has sign 0 anyway
    out["t_obv_slope"] = _rolling_slope(obv, 20) / _full_windows_only(_roll_missing_ok(v, 20).mean(), 20)
    pivot_prev = ((h + l + c) / 3.0).shift(1)               # the PREVIOUS session's classic pivot
    out["t_pivot_dist"] = (c - pivot_prev) / atr
    lv = np.log1p(v)
    mean60 = _full_windows_only(_roll_missing_ok(lv, 60).mean(), 60)
    std60 = _full_windows_only(_roll_missing_ok(lv, 60).std(), 60)
    out["t_vol_z"] = (lv - mean60) / std60
    return out[list(T_COLUMNS)]


def technical(daily: Dict[str, pd.DataFrame], ctx: BuildContext) -> pd.DataFrame:
    """Group ``t_``: see ``T_COLUMNS``. Price-derived columns are log returns, ATR multiples or ratios; ``t_rsi`` and
    ``t_donch_*`` are centred on 0 ([-0.5, 0.5]); ``t_adx`` is ADX/100; ``t_bb_pctb`` is %B - 0.5."""
    return _per_symbol(daily, ctx, lambda live, sym: _tech(ctx, sym, live))


# ------------------------------------------------------------------------------------------ group MTF (weekly)
MTF_COLUMNS: Tuple[str, ...] = ("mtf_w_rsi14", "mtf_w_dist_sma10", "mtf_w_dist_sma40", "mtf_w_ret_4", "mtf_w_ret_13")


def _week_key(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """The Monday of each session's calendar week. A week ends at its LAST SESSION (Thursday before Good Friday)."""
    return pd.DatetimeIndex(index - pd.to_timedelta(index.weekday, unit="D"))


def _weekly_one(d: pd.DataFrame) -> pd.DataFrame:
    """Weekly features attached to the daily sessions of the FOLLOWING weeks only.

    Weekly bars are the week's max high, min low and last close. The features are computed on weekly bars, shifted
    one week, and looked up by each session's week: a session in week W sees week W-1's finished bar and never week
    W's partial one, including on W's last session (the week's final close)."""
    key = _week_key(d.index)
    g = d.groupby(key)
    w = pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last()})
    c = w["close"]
    watr = wilder(true_range(w["high"], w["low"], c), 14)
    f = pd.DataFrame(index=w.index)
    f["mtf_w_rsi14"] = _rsi_scaled(c)
    f["mtf_w_dist_sma10"] = (c - _roll(c, 10).mean()) / watr
    f["mtf_w_dist_sma40"] = (c - _roll(c, 40).mean()) / watr
    lc = np.log(c)
    f["mtf_w_ret_4"], f["mtf_w_ret_13"] = lc.diff(4), lc.diff(13)
    prior = f.shift(1)                                       # week k's bar becomes visible in week k+1
    out = prior.reindex(key)
    out.index = d.index
    return out[list(MTF_COLUMNS)]


def multi_timeframe(daily: Dict[str, pd.DataFrame], ctx: BuildContext) -> pd.DataFrame:
    """Group ``mtf_``: weekly RSI14 (scaled), distance to the 10- and 40-week SMA in weekly-ATR units, 4- and 13-week
    log returns, all from COMPLETED weeks (see ``_weekly_one``)."""
    return _per_symbol(daily, ctx, lambda live, sym: _weekly_one(live))


# ------------------------------------------------------------------------------------------ group M (market)
#: sector (FMP / screener-store spelling) -> its SPDR sector ETF. A sector outside this map is refused, never defaulted.
SECTOR_ETF: Dict[str, str] = {
    "Technology": "XLK", "Financial Services": "XLF", "Energy": "XLE", "Healthcare": "XLV",
    "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP", "Industrials": "XLI", "Basic Materials": "XLB",
    "Utilities": "XLU", "Real Estate": "XLRE", "Communication Services": "XLC"}
#: broad / cross-asset context series every M build needs (equity, tech, small caps, rates, gold)
MARKET_ETFS: Tuple[str, ...] = ("SPY", "QQQ", "IWM", "TLT", "GLD")
_M_RET_DAYS: Tuple[int, ...] = (1, 5, 20)
_M_RS_DAYS: Tuple[int, ...] = (5, 20)
M_COLUMNS: Tuple[str, ...] = (
    tuple(f"m_{e.lower()}_ret_{n}" for e in MARKET_ETFS for n in _M_RET_DAYS)
    + tuple(f"m_rs_spy_{n}" for n in _M_RS_DAYS) + tuple(f"m_rs_sector_{n}" for n in _M_RS_DAYS)
    + ("m_breadth_sma50",))


#: A sector value declaring that the symbol HAS NO sector (an index or a sector ETF): its ``m_rs_sector_*`` are NaN and no
#: sector ETF is loaded for it. Anything else that is not in ``SECTOR_ETF`` still refuses.
NO_SECTOR = "NO_SECTOR"


def _sector_etfs(symbols: Sequence[str], sector_of: Optional[Dict[str, str]]) -> Dict[str, Optional[str]]:
    """symbol -> its sector ETF (None for ``NO_SECTOR``). Refuses a missing map, a symbol without a sector and a sector
    outside SECTOR_ETF."""
    if not sector_of:
        raise PanelDataError("group m needs sector_of (symbol -> sector); got none")
    etf_of: Dict[str, Optional[str]] = {}
    for sym in symbols:
        sector = sector_of[sym] if sym in sector_of else None
        if sector is None:
            raise PanelDataError(f"group m: no sector for {sym}")
        if sector == NO_SECTOR:
            etf_of[sym] = None
            continue
        if sector not in SECTOR_ETF:
            raise PanelDataError(f"group m: unknown sector {sector!r} for {sym}; known {sorted(SECTOR_ETF)}")
        etf_of[sym] = SECTOR_ETF[sector]
    return etf_of


def _market_context(symbols: Sequence[str], sector_of: Optional[Dict[str, str]]) -> Set[str]:
    return set(MARKET_ETFS) | {e for e in _sector_etfs(symbols, sector_of).values() if e is not None}


def _log_ret(d: pd.DataFrame, n: int) -> pd.Series:
    """``n``-session log return of a loaded frame on the full session grid (NaN before its listing)."""
    return np.log(d["close"]).diff(n)


def _require(daily: Dict[str, pd.DataFrame], sym: str, why: str) -> pd.DataFrame:
    if sym not in daily:
        raise PanelDataError(f"group m needs {sym} ({why}) among the loaded symbols")
    return daily[sym]


def market(daily: Dict[str, pd.DataFrame], ctx: BuildContext) -> pd.DataFrame:
    """Group ``m_``: the market the symbol trades in, as of the same session close.

    * ``m_<etf>_ret_{1,5,20}``: SPY / QQQ / IWM / TLT / GLD log returns (broad equity, tech, small caps, rates, gold);
    * ``m_rs_spy_{5,20}``: the symbol's log return minus SPY's; ``m_rs_sector_{5,20}``: minus its sector ETF's;
    * ``m_breadth_sma50``: the share of the universe's MEMBERS that session (``ctx.member_mask``; not the context
      ETFs) whose close is above its own 50-session SMA. Symbols not yet listed, or without 50 bars, are not counted.
      The value is the same for every symbol on a given session; it is a property of the universe, so a build in
      chunks over the same universe and membership gives the same breadth as one build.

    Every input is a bar of session S or earlier, so a row is fixed once S has closed. Each built symbol needs a
    sector in ``SECTOR_ETF``; anything else raises."""
    etf_of = _sector_etfs(ctx.symbols, ctx.sector_of)
    for e in MARKET_ETFS:
        _require(daily, e, "market series")
    for e in sorted({e for e in etf_of.values() if e is not None}):
        _require(daily, e, "sector ETF")
    mk = pd.DataFrame(index=ctx.sessions)
    for e in MARKET_ETFS:
        for n in _M_RET_DAYS:
            mk[f"m_{e.lower()}_ret_{n}"] = _log_ret(daily[e], n)
    closes = pd.DataFrame({s: daily[s]["close"] for s in ctx.universe})
    sma50 = closes.rolling(50, min_periods=50).mean()                     # the average uses the symbol's own history ...
    valid = sma50.notna() & ctx.member_mask()                              # ... and only the session's members vote
    mk["m_breadth_sma50"] = ((closes > sma50) & valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan)
    frames: Dict[str, pd.DataFrame] = {}
    for sym in ctx.symbols:
        f = mk.copy()
        for n in _M_RS_DAYS:
            own = _log_ret(daily[sym], n)
            f[f"m_rs_spy_{n}"] = own - _log_ret(daily["SPY"], n)
            f[f"m_rs_sector_{n}"] = (np.nan if etf_of[sym] is None else own - _log_ret(daily[etf_of[sym]], n))
        f = f[list(M_COLUMNS)]
        f.loc[~daily[sym]["listed"].to_numpy(), :] = np.nan                 # no row before the symbol exists
        frames[sym] = f
    return pd.concat(frames, names=_INDEX_NAMES).astype("float64")


# ------------------------------------------------------------------------------------------ group X (cross-sectional)
#: sector -> integer code of group X (committed: the code of a sector never changes)
SECTOR_CODE: Dict[str, int] = {
    "Basic Materials": 1, "Communication Services": 2, "Consumer Cyclical": 3, "Consumer Defensive": 4, "Energy": 5,
    "Financial Services": 6, "Healthcare": 7, "Industrials": 8, "Real Estate": 9, "Technology": 10, "Utilities": 11}
#: T features ranked / z-scored across the universe members on each session
X_RANKED: Tuple[str, ...] = ("t_ret_5", "t_ret_20", "t_ret_60", "t_rvol_20", "t_dist_sma50", "t_rsi", "t_vol_z")
X_OWN_COLUMNS: Tuple[str, ...] = ("x_mom_12_1", "x_mom_12_1_rk", "x_weinstein", "x_vol_ratio_20_60", "x_sector")
X_COLUMNS: Tuple[str, ...] = (tuple(f"x_rk_{c[2:]}" for c in X_RANKED) + tuple(f"x_z_{c[2:]}" for c in X_RANKED)
                              + X_OWN_COLUMNS)
#: A cross-section is only ranked / z-scored when at least ``X_MIN_MEMBERS`` members have a value AND they are at
#: least ``X_MIN_SHARE`` of that session's members: max(5, 50% of the members). A rank among a handful of survivors
#: of a thin start-up period would be a different statistic from the one later years get.
X_MIN_MEMBERS = 5
X_MIN_SHARE = 0.5


def _cross_ok(wide: pd.DataFrame, members_n: pd.Series) -> pd.Series:
    n = wide.notna().sum(axis=1)
    return (n >= X_MIN_MEMBERS) & (n >= X_MIN_SHARE * members_n)


def _cross_rank(wide: pd.DataFrame, members_n: pd.Series) -> pd.DataFrame:
    """Per row (session) the midpoint rank (rank - 0.5) / n in (0, 1) among the non-NaN cells (ties averaged); NaN
    when the cross-section is too small (``_cross_ok``). ``wide`` has NaN for non-members."""
    n = wide.notna().sum(axis=1)
    return (wide.rank(axis=1, method="average") - 0.5).div(n, axis=0).where(_cross_ok(wide, members_n), np.nan)


def _cross_z(wide: pd.DataFrame, members_n: pd.Series) -> pd.DataFrame:
    """Per row the z-score (sample std) among the non-NaN cells; NaN when the cross-section is too small."""
    return wide.sub(wide.mean(axis=1), axis=0).div(wide.std(axis=1, ddof=1), axis=0).where(_cross_ok(wide, members_n), np.nan)


def _own_columns(d: pd.DataFrame) -> pd.DataFrame:
    """x_mom_12_1, x_weinstein and x_vol_ratio_20_60 of one symbol FROM ITS BARS (all trailing windows, every year).

    * ``x_mom_12_1``: the 12-1 month return, ``close[S-21] / close[S-251] - 1`` (``metric_store.momentum_12_1_series``,
      the store's own definition);
    * ``x_weinstein``: the Weinstein stage 1-4 from the 150-session (30-week) SMA of the close and its 20-session slope
      (``metric_store.weinstein_stage_series``: 2 = above a rising SMA, 4 = below a falling one, 3 = above a flat or
      falling one, 1 = otherwise; flat = slope within +-0.5%);
    * ``x_vol_ratio_20_60``: 20-session over 60-session mean volume (NOT the screener's ``relative_volume``, which is
      today's volume over the prior 20-day average) (a filled bar's volume is left out, see ``MAX_FILLED_RUN``)."""
    from ba2_providers.screener import metric_store as ms
    close = d["close"]
    v = d["volume"].where(~d["filled"])
    out = pd.DataFrame(index=d.index)
    out["x_mom_12_1"] = ms.momentum_12_1_series(close)
    out["x_weinstein"] = ms.weinstein_stage_series(close)
    m20 = _full_windows_only(_roll_missing_ok(v, 20).mean(), 20)
    m60 = _full_windows_only(_roll_missing_ok(v, 60).mean(), 60)
    out["x_vol_ratio_20_60"] = m20 / m60.where(m60 > 0)
    return out


def _sector_codes(ctx: BuildContext) -> Dict[str, float]:
    """symbol -> sector code: the store's static profile sector, then the caller's explicit sectors on top. A symbol
    with no sector is NaN; a sector that is not in SECTOR_CODE refuses."""
    sectors = dict(ml_universe.sector_of_from_store(ctx.universe, ctx.store_dir))
    sectors.update(ctx.sector_of)
    sectors.update(ctx.sector_overrides)
    out: Dict[str, float] = {}
    for sym in ctx.universe:
        if sym not in sectors or sectors[sym] == NO_SECTOR:
            out[sym] = np.nan
        elif sectors[sym] not in SECTOR_CODE:
            raise PanelDataError(f"group x: sector {sectors[sym]!r} of {sym} is not in SECTOR_CODE; add it to SECTOR_CODE "
                                 f"(a new code never changes an existing one)")
        else:
            out[sym] = float(SECTOR_CODE[sectors[sym]])
    return out


def cross_section(daily: Dict[str, pd.DataFrame], ctx: BuildContext) -> pd.DataFrame:
    """Group ``x_``: where the symbol stands among the universe's MEMBERS on each session.

    * ``x_rk_*`` / ``x_z_*``: rank in (0, 1) and z-score, across the members of that session (listed, with a value;
      an unlisted, ended or non-member symbol is not in the cross-section and gets NaN itself) of ``X_RANKED`` (the T
      features, computed here for every universe member from bars up to S). Too small a cross-section is NaN
      (``X_MIN_MEMBERS``, ``X_MIN_SHARE``);
    * ``x_mom_12_1`` and its member rank ``x_mom_12_1_rk``, ``x_weinstein``, ``x_vol_ratio_20_60``: computed from the bars
      (``_own_columns``), so they exist in every year, not only from the store's first scan (2020-01);
    * ``x_sector``: ``SECTOR_CODE`` of the symbol's sector, the store's profile sector (static, see
      ``ml_universe``) unless the caller overrides it; NaN when unknown.

    Membership is ``ctx.membership`` (per session, e.g. ``ml_universe.membership_from_universe``) or, when None, every
    universe symbol always (the NDQ30 stage). A build in chunks over the same universe and membership gives the same
    values as one build."""
    members = sorted(ctx.universe)            # a fixed column order: float sums (mean, std) must not depend on the caller

    def compute_parts() -> Dict[str, pd.DataFrame]:
        mask = ctx.member_mask()
        members_n = mask.sum(axis=1)
        wide = {c: {} for c in X_RANKED + ("x_mom_12_1",)}
        own: Dict[str, pd.DataFrame] = {}
        for sym in members:
            d = daily[sym]
            live = d[d["listed"]]
            if live.empty:
                t = pd.DataFrame(np.nan, index=ctx.sessions, columns=list(X_RANKED))
                o = pd.DataFrame(np.nan, index=ctx.sessions, columns=["x_mom_12_1", "x_weinstein", "x_vol_ratio_20_60"])
            else:
                t = _tech(ctx, sym, live)[list(X_RANKED)].reindex(ctx.sessions)
                o = _memoised(ctx, ("own", sym, ctx.sessions[0], ctx.sessions[-1]),
                              lambda live=live: _own_columns(live).reindex(ctx.sessions))
            own[sym] = o
            for c in X_RANKED:
                wide[c][sym] = t[c]
            wide["x_mom_12_1"][sym] = o["x_mom_12_1"]
        parts: Dict[str, pd.DataFrame] = {}
        for c in X_RANKED:
            w = pd.DataFrame(wide[c], columns=members).astype("float64").where(mask)
            parts[f"x_rk_{c[2:]}"] = _cross_rank(w, members_n)
            parts[f"x_z_{c[2:]}"] = _cross_z(w, members_n)
        mom = pd.DataFrame(wide["x_mom_12_1"], columns=members).astype("float64")
        parts["x_mom_12_1"] = mom
        parts["x_mom_12_1_rk"] = _cross_rank(mom.where(mask), members_n)
        parts["x_weinstein"] = pd.DataFrame({s: own[s]["x_weinstein"] for s in members}, columns=members)
        parts["x_vol_ratio_20_60"] = pd.DataFrame({s: own[s]["x_vol_ratio_20_60"] for s in members}, columns=members)
        codes = _sector_codes(ctx)
        parts["x_sector"] = pd.DataFrame({s: pd.Series(codes[s], index=ctx.sessions) for s in members}, columns=members)
        return parts

    key = ("x_core", tuple(members), ctx.sessions[0], ctx.sessions[-1],
           None if ctx.membership is None else _membership_digest(ctx.membership), X_MIN_MEMBERS, X_MIN_SHARE,
           tuple(sorted(ctx.sector_of.items())), tuple(sorted(ctx.sector_overrides.items())), str(ctx.store_dir))
    parts = _memoised(ctx, key, compute_parts)         # the same for every chunk of one store build
    frames = {sym: pd.DataFrame({name: parts[name][sym] for name in X_COLUMNS}, index=ctx.sessions)[list(X_COLUMNS)]
              for sym in ctx.symbols}
    return pd.concat(frames, names=_INDEX_NAMES).astype("float64")


# ------------------------------------------------------------------------------------------ group MAC (FRED)
#: FRED series of group MAC and how the "level" column is defined: ``lvl`` the series itself (percent / index points),
#: ``yoy`` the year-over-year change of the first-release level (CPI), ``3m`` its 3-month change (payrolls)
MAC_SERIES: Dict[str, str] = {"VIXCLS": "lvl", "DGS10": "lvl", "DGS3MO": "lvl", "T10Y3M": "lvl", "BAA10Y": "lvl",
                              "CPIAUCSL": "yoy", "PAYEMS": "3m", "UNRATE": "lvl"}
MAC_COLUMNS: Tuple[str, ...] = tuple(
    f"mac_{sid.lower()}_{suffix}" for sid, kind in MAC_SERIES.items() for suffix in (kind, "chg20", "z252"))
#: Sessions of history the 20-session change and the 252-session z-score need before the first built session
#: (calendar days; 460 is ~316 sessions), so those columns are defined from the first session on.
_MAC_HISTORY_DAYS = 460
#: ``fred_series`` raises ``MacroAvailabilityUnknown`` (no subclass, no code) for a decision labelled on or before the
#: series' first recorded vintage; this is a fragment of that message, pinned by a test, the only way to tell it from
#: the refusals that must propagate (missing / old cache).
_BEFORE_FIRST_VINTAGE = "on or before the series' first recorded vintage"
#: How long a value may be held: weekdays since it became public for a daily series, calendar days for a monthly one
#: (100: the 2025 shutdown left a 76-day gap between two releases, which must pass with room).
#: Age is counted from the RELEASE (ALFRED realtime_start), not the observation date: a delayed release (the 2025
#: shutdown) is not older than the day it became public. A longer hold refuses instead of carrying a dead series.
MAX_HOLD_WEEKDAYS = 10
MAX_HOLD_DAYS_MONTHLY = 100
#: Known stalls of the real cache, as reviewed facts (series -> [(last release before the stall, first release after
#: it)]): FRED published no BAA10Y between 2016-10-07 and 2017-03-22 (the observations of those months were first
#: released together on 2017-03-22). Inside such a window the series is NaN (a 166-day-old spread is not the
#: spread), outside any window an over-age value refuses.
KNOWN_STALLS: Dict[str, List[Tuple[str, str]]] = {"BAA10Y": [("2016-10-07", "2017-03-22")]}
#: Pinned first-release content of the real FRED cache (rows first released on or before ``FRED_PIN_CUTOFF``): number of
#: rows and the first 16 hex digits of sha256(json.dumps([(date, value or None, realtime_start), ...])). DGS3MO, the
#: legacy observation-date file, is pinned over (date, float(value)) for date <= the cutoff; the same rows with the
#: values as strings digest to 6f412254a9723315 (the BT session's figure). FEDFUNDS (866, 051c1f1e135efad8) matches too
#: but is not a MAC series. A build on a cache whose pinned part changed refuses: the features would not be reproducible.
FRED_PIN_CUTOFF = "2026-09-30"
REAL_FRED_PINS: Dict[str, Tuple[int, str]] = {
    "BAA10Y": (10186, "7c375266b7540404"), "CPIAUCSL": (955, "3202c9fa1f77020e"), "DGS10": (16171, "b26e35debc223db1"),
    "DGS3MO": (11270, "d8133ea13e3d18a2"), "PAYEMS": (1052, "6b869176a1b63918"), "T10Y3M": (11189, "aa1f40ccafe7a168"),
    "UNRATE": (943, "faa617ca44da8c14"), "VIXCLS": (9285, "a9c65696dbec8602")}
#: the active pins (a test can swap them for a synthetic cache)
FRED_PINS: Dict[str, Tuple[int, str]] = dict(REAL_FRED_PINS)


class FredPinMismatch(PanelDataError):
    """The FRED cache's pinned content is not what the features were specified on."""


_FRED_FILE_MEMO: Dict[Tuple[str, int, int], Dict[str, object]] = {}


def _read_fred_file(sid: str, folder: Path) -> Dict[str, object]:
    """Read the series file DIRECTLY (not through the reader): its count / digest / header / release dates. Memoised
    per file version."""
    path = Path(folder) / "fred" / f"{sid}.json"
    if not path.exists():
        raise fs.MacroAvailabilityUnknown(f"FRED {sid}: not in the cache ({path}); warm it with tools/refresh_fred_cache.py")
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _FRED_FILE_MEMO.get(key)
    if hit is not None:
        return hit
    raw = path.read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    obs = doc["observations"]
    first_release = fs.SERIES_SPEC[sid]["availability"] == fs.AVAIL_FIRST_RELEASE
    if first_release:
        rows = [(o["date"], None if o["value"] == "." else float(o["value"]), o["realtime_start"])
                for o in obs if o["realtime_start"] <= FRED_PIN_CUTOFF]
        released = {pd.Timestamp(o["date"]): pd.Timestamp(o["realtime_start"]) for o in obs}
        last_vintage = max((o["realtime_start"] for o in obs), default=None)
    else:
        rows = [(o["date"], None if o["value"] == "." else float(o["value"])) for o in obs if o["date"] <= FRED_PIN_CUTOFF]
        released = {}
        last_vintage = max((o["date"] for o in obs), default=None)
    digest = hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]
    file_fp = {**_fingerprint(path), "sha256": hashlib.sha256(raw).hexdigest()}     # hosts compare the whole file
    rec = {"count": len(rows), "digest": digest, "fetched_at": doc["fetched_at"] if "fetched_at" in doc else None,
           "last_vintage": last_vintage, "released": released, "file": file_fp}
    _FRED_FILE_MEMO[key] = rec
    return rec


def fred_digest(series_id: str, folder: Optional[Path] = None) -> Tuple[int, str]:
    """(row count, digest) of a series' pinned part (see ``REAL_FRED_PINS``), read straight from its cache file."""
    rec = _read_fred_file(series_id, Path(fs.CACHE_FOLDER if folder is None else folder))
    return rec["count"], rec["digest"]


def check_fred_pins(folder: Path) -> Dict[str, Dict[str, object]]:
    """Refuse (``FredPinMismatch``, naming the series) when a series' pinned part is not the pinned one. Returns the
    provenance of every MAC series (header, last vintage, count, digest, file fingerprint)."""
    prov: Dict[str, Dict[str, object]] = {}
    for sid in MAC_SERIES:
        rec = _read_fred_file(sid, folder)
        if sid in FRED_PINS:
            n, digest = FRED_PINS[sid]
            if rec["count"] != n:
                raise FredPinMismatch(f"FRED {sid}: {rec['count']} rows up to {FRED_PIN_CUTOFF}, pinned {n} (count mismatch); "
                                      f"the cache is not the one the MAC features are specified on")
            if rec["digest"] != digest:
                raise FredPinMismatch(f"FRED {sid}: digest {rec['digest']} of the rows up to {FRED_PIN_CUTOFF}, pinned "
                                      f"{digest} (digest mismatch); a pinned value or release date changed")
        prov[sid] = {k: rec[k] for k in ("count", "digest", "fetched_at", "last_vintage", "file")}
    return prov


def _macro_value(series: pd.Series, kind: str) -> Tuple[float, Optional[pd.Timestamp]]:
    """(value, observation date) of the newest observation with a valid transform; (NaN, None) if there is none.

    The transform is computed AT THE OBSERVATION: ``lvl`` the value, ``yoy`` the value over the one 12 months earlier
    minus 1, ``3m`` over the one 3 months earlier minus 1. An observation whose earlier month is missing (a skipped
    release) has no transform, and the last valid transformed value is held instead of producing NaN."""
    if series.empty:
        return np.nan, None
    if kind == "lvl":
        return float(series.iloc[-1]), series.index[-1]
    months = 12 if kind == "yoy" else 3
    index = series.index
    for k in range(1, min(len(series), _MAX_BACK_OBS) + 1):
        t = index[-k]
        prior = t - pd.DateOffset(months=months)
        pos = index.searchsorted(prior)
        if pos < len(index) and index[pos] == prior and float(series.iloc[pos]) > 0:
            return float(series.iloc[-k]) / float(series.iloc[pos]) - 1.0, t
    return np.nan, None


#: how many of the newest observations are tried for a computable transform before giving up
_MAX_BACK_OBS = 6


def _z252(level: pd.Series) -> pd.Series:
    """z-score over the last 252 sessions; NaN (not inf) where the window has no variation."""
    mean = level.rolling(252, min_periods=252).mean()
    std = level.rolling(252, min_periods=252).std()
    return (level - mean) / std.where(std > 1e-9 * (1.0 + mean.abs()))


def macro_levels(series_id: str, sessions: pd.DatetimeIndex) -> pd.Series:
    """The ``MAC_SERIES`` level of ``series_id`` as a decision at each session's close sees it.

    One ``fred_series.get_series_as_of`` call per session, always offline (``hermetic_fmp_history``: a missing or old
    cache raises ``MacroAvailabilityUnknown`` instead of fetching; NEVER call the readers outside that context).
    ``as_of`` is the session DATE at midnight UTC, which is the platform's DAILY-bar convention
    (``fred_series.decision_label``): bar D decides on D's close and fills at the next session's open, so it sees every
    observation whose first release (ALFRED ``realtime_start``) is before the next session, i.e. up to and including
    D. NEVER the observation date plus a lag. (An as_of of D's close INSTANT would map to the label D itself and hide
    everything released on D.)

    The transform (YoY, 3-month change) is taken at the observation level and the last valid transformed value is held
    (``_macro_value``); a value held longer than ``MAX_HOLD_WEEKDAYS`` (daily series) / ``MAX_HOLD_DAYS_MONTHLY``
    (monthly), counted from its release, refuses with ``PanelDataError``.

    DGS3MO is the one series the cache stores without release dates (observation-date format, "same-day" by design for
    the option rate). Its observation of day D is published after D's close, so it is read one session later: the
    value for session S is the observation of the previous session (its age is counted from that observation date).

    A session whose decision label is on or before the series' first recorded vintage is NaN (nothing provable is
    public yet); every other refusal propagates."""
    from ba2_providers.fmp_common import hermetic_fmp_history

    if series_id not in MAC_SERIES:
        raise ValueError(f"{series_id} is not a MAC series; known {sorted(MAC_SERIES)}")
    sessions = pd.DatetimeIndex(sessions)
    kind = MAC_SERIES[series_id]
    monthly = fs.SERIES_SPEC[series_id]["freq"] == "monthly"
    same_day = fs.SERIES_SPEC[series_id]["availability"] != fs.AVAIL_FIRST_RELEASE
    released = _read_fred_file(series_id, Path(fs.CACHE_FOLDER))["released"]
    if same_day:
        first = sessions[0].date()
        prev0 = regular_sessions(first - pd.Timedelta(days=10), first - pd.Timedelta(days=1))["session"].iloc[-1]
        as_of_days = [pd.Timestamp(prev0)] + list(sessions[:-1])
    else:
        as_of_days = list(sessions)
    out = np.full(len(sessions), np.nan)
    with hermetic_fmp_history():
        for i, day in enumerate(as_of_days):
            try:
                visible = fs.get_series_as_of(series_id, pd.Timestamp(day).tz_localize("UTC"))
            except fs.MacroAvailabilityUnknown as e:
                if _BEFORE_FIRST_VINTAGE in str(e):
                    continue
                raise
            value, obs = _macro_value(visible, kind)
            if obs is None:
                continue
            since = obs if same_day else released[obs]
            age = ((sessions[i] - since).days if monthly
                   else int(np.busday_count(since.date(), sessions[i].date())))
            limit = MAX_HOLD_DAYS_MONTHLY if monthly else MAX_HOLD_WEEKDAYS
            if age > limit and any(since == pd.Timestamp(a) and sessions[i] < pd.Timestamp(b)
                                   for a, b in KNOWN_STALLS.get(series_id, [])):
                continue                                          # a known, reviewed stall: NaN, not a stale value
            if age > limit:
                raise PanelDataError(
                    f"FRED {series_id}: the value at {sessions[i].date()} was public since {since.date()}, older than "
                    f"{limit} {'days' if monthly else 'weekdays'} ({age}); the series has stalled, refusing to hold it")
            out[i] = value
    return pd.Series(out, index=sessions, name=series_id)


def macro(daily: Dict[str, pd.DataFrame], ctx: BuildContext) -> pd.DataFrame:
    """Group ``mac_``: the macro backdrop, FRED first releases as of each session's close (see ``macro_levels``).

    Per series (``MAC_SERIES``): ``mac_<id>_<lvl|yoy|3m>`` the level, ``mac_<id>_chg20`` its 20-session change,
    ``mac_<id>_z252`` its z-score over the last 252 sessions of as-of levels. The value is the same for every symbol
    on a session. The levels of the sessions before the first built one are computed too (``_MAC_HISTORY_DAYS``), so
    the change and the z-score never depend on where the build's session grid starts.

    FRED's cache folder must be the build's ``cache_root`` (the reader has one global folder): a different one
    refuses rather than reading another cache."""
    if Path(fs.CACHE_FOLDER).resolve() != Path(ctx.cache_root).resolve():
        raise PanelDataError(f"group mac reads FRED from {fs.CACHE_FOLDER}, not from cache_root {ctx.cache_root}")
    ctx.provenance["fred"] = check_fred_pins(Path(fs.CACHE_FOLDER))

    def compute() -> pd.DataFrame:
        first = (ctx.sessions[0] - pd.Timedelta(days=_MAC_HISTORY_DAYS)).date()
        grid = _sessions_index(first, ctx.sessions[-1].date())
        cols: Dict[str, pd.Series] = {}
        for sid, kind in MAC_SERIES.items():
            lvl = macro_levels(sid, grid)
            name = f"mac_{sid.lower()}"
            cols[f"{name}_{kind}"] = lvl
            cols[f"{name}_chg20"] = lvl - lvl.shift(20)
            cols[f"{name}_z252"] = _z252(lvl)
        return pd.DataFrame(cols)[list(MAC_COLUMNS)].reindex(ctx.sessions).astype("float64")
    # the same for every symbol and every chunk of one store build; keyed by the grid and the FRED folder it reads
    table = _memoised(ctx, ("mac", ctx.sessions[0], ctx.sessions[-1], str(fs.CACHE_FOLDER)), compute)
    return pd.concat({sym: table for sym in ctx.symbols}, names=_INDEX_NAMES)


# ------------------------------------------------------------------------------------------ group F (fundamentals)
#: the nine columns of ``ml_panel.fundamental_features`` (earnings surprise / growth, margins, earnings yield) ...
_F_PANEL_COLUMNS: Tuple[str, ...] = (
    "f_eps_surprise", "f_rev_surprise", "f_eps_yoy", "f_rev_yoy", "f_gross_margin", "f_op_margin",
    "f_annual_rev_growth", "f_days_since_earn", "f_earn_yield")
#: ... plus the annual balance-sheet / cash-flow ratios
_F_STATEMENT_COLUMNS: Tuple[str, ...] = (
    "f_debt_assets", "f_current_ratio", "f_fcf_margin", "f_fcf_yield", "f_buyback_yield", "f_sbc_rev", "f_capex_rev")
F_COLUMNS: Tuple[str, ...] = _F_PANEL_COLUMNS + _F_STATEMENT_COLUMNS
#: the columns that divide by the SCREENER market cap: NaN before the metric store's first scan (2020-01) and for a symbol the
#: scan lacks, so they are not coverage columns of the group (the other columns are not masked for that reason)
F_MARKET_CAP_COLUMNS: Tuple[str, ...] = ("f_fcf_yield", "f_buyback_yield")
#: Sessions before the first built one that ``fundamentals_table`` is run over, so an earnings date before the grid
#: keeps its real session (``f_days_since_earn`` would otherwise count from the grid's first session).
_F_HISTORY_DAYS = 450


def _safe_ratio(num: np.ndarray, den: np.ndarray, lo: float = -5.0, hi: float = 5.0) -> np.ndarray:
    """``num / den`` where the denominator is a positive finite number and the numerator is finite, else NaN (never
    inf); clipped to ``[lo, hi]`` like the ratios of ``ml_panel.fundamentals_table``."""
    num, den = np.asarray(num, dtype="float64"), np.asarray(den, dtype="float64")
    ok = np.isfinite(num) & np.isfinite(den) & (den > 0)
    out = np.full(num.shape, np.nan)
    out[ok] = np.clip(num[ok] / den[ok], lo, hi)
    return out


def _num(rec: dict, key: str) -> float:
    v = rec[key] if key in rec else None
    return float(v) if v is not None else np.nan


def _select_visible(sessions: Sequence[date], events: List[Tuple[date, str, int]]) -> np.ndarray:
    """For each session the payload index of the record with the NEWEST fiscal date among those already visible
    (``events``: (first visible session, fiscal date, payload index)); -1 before the first one. A late-filed older
    statement never replaces a newer one."""
    events = sorted(events, key=lambda e: (e[0], e[1]))
    out = np.full(len(sessions), -1, dtype=np.int64)
    ei, best, best_idx = 0, "", -1
    for k, day in enumerate(sessions):
        while ei < len(events) and events[ei][0] <= day:
            _, fiscal, idx = events[ei]
            if fiscal >= best:
                best, best_idx = fiscal, idx
            ei += 1
        out[k] = best_idx
    return out


def _pick(values: np.ndarray, chosen: np.ndarray) -> np.ndarray:
    out = np.full(len(chosen), np.nan)
    have = chosen >= 0
    out[have] = values[chosen[have]]
    return out


#: the keys each annual statement file must carry (in at least one record) for group F
_STATEMENT_KEYS: Dict[str, Tuple[str, ...]] = {
    "balance_sheet_annual": ("totalLiabilities", "totalAssets", "totalCurrentAssets", "totalCurrentLiabilities"),
    "cashflow_statement_annual": ("freeCashFlow", "commonStockRepurchased", "stockBasedCompensation", "capitalExpenditure"),
    "income_statement_annual": ("revenue",)}
_F_FILES: Tuple[str, ...] = ("past_earnings_quarterly",) + tuple(_STATEMENT_KEYS)


def fmp_history_path(kind: str, symbol: str, cache_root: Path) -> Path:
    return Path(cache_root) / "fmp_history" / f"{kind}__{symbol}.json"


def load_statements(symbol: str, cache_root: Path) -> Dict[str, List[dict]]:
    """The annual statements group F reads, or ``PanelDataError``: a missing file (an ETF has none), an empty file, or
    a needed key absent from EVERY record of a file (a schema change; a key missing from some records is just NaN)."""
    from app.services import ml_panel
    out: Dict[str, List[dict]] = {}
    for kind, keys in _STATEMENT_KEYS.items():
        recs = ml_panel._load_json(cache_root, kind, symbol)
        if not recs:
            raise PanelDataError(f"{symbol}: {kind} has no records (an empty file); group F is stock-only")
        for key in keys:
            if not any(key in r for r in recs):
                raise PanelDataError(f"{symbol}: {kind} has no '{key}' in any of its {len(recs)} records")
        out[kind] = recs
    return out


def check_fundamentals_files(symbol: str, cache_root: Path) -> None:
    """Everything group F needs for ``symbol`` is present (``PanelDataError`` otherwise)."""
    from app.services import ml_panel
    ml_panel._load_json(cache_root, "past_earnings_quarterly", symbol)
    load_statements(symbol, cache_root)


def _statement_features(symbol: str, sessions: Sequence[date], market_cap: np.ndarray,
                        statements: Dict[str, List[dict]]) -> pd.DataFrame:
    """The ``_F_STATEMENT_COLUMNS`` of one symbol on ``sessions`` (dates).

    Each annual statement is visible from the session after its ``acceptedDate`` (``ml_panel.fundamentals_visible_from``
    "annual" rule) and the newest fiscal year visible is used. The ratios over revenue need the income statement of
    the SAME fiscal date, so they appear when both filings are public; the yields need only the cash flow. The records
    come from ``load_statements`` (a missing file or key refuses)."""
    from app.services import ml_panel

    sessions = list(sessions)
    bs, cf, inc = (statements["balance_sheet_annual"], statements["cashflow_statement_annual"],
                   statements["income_statement_annual"])

    def visible(rec: dict) -> Optional[date]:
        return ml_panel.fundamentals_visible_from("annual", rec, sessions) if "date" in rec else None

    out: Dict[str, np.ndarray] = {}
    # balance sheet
    ev = [(v, r["date"], i) for i, r in enumerate(bs) if (v := visible(r)) is not None]
    pick = _select_visible(sessions, ev)
    out["f_debt_assets"] = _pick(_safe_ratio([_num(r, "totalLiabilities") for r in bs], [_num(r, "totalAssets") for r in bs]), pick)
    out["f_current_ratio"] = _pick(_safe_ratio([_num(r, "totalCurrentAssets") for r in bs],
                                               [_num(r, "totalCurrentLiabilities") for r in bs]), pick)
    # cash flow alone: the yields over the market cap visible that session
    ev = [(v, r["date"], i) for i, r in enumerate(cf) if (v := visible(r)) is not None]
    pick = _select_visible(sessions, ev)
    fcf = _pick(np.array([_num(r, "freeCashFlow") for r in cf]), pick)
    bought = _pick(np.array([_num(r, "commonStockRepurchased") for r in cf]), pick)
    out["f_fcf_yield"] = _safe_ratio(fcf, market_cap)
    out["f_buyback_yield"] = _safe_ratio(-bought, market_cap)
    # cash flow + income statement of the same fiscal date: the revenue ratios
    inc_by_date = {r["date"]: r for r in inc if "date" in r}
    pairs, rev, sbc, capex, fcf_p = [], [], [], [], []
    for r in cf:
        match = inc_by_date[r["date"]] if "date" in r and r["date"] in inc_by_date else None
        v_cf, v_inc = visible(r), (visible(match) if match is not None else None)
        if v_cf is not None and v_inc is not None:
            pairs.append((max(v_cf, v_inc), r["date"], len(rev)))
            rev.append(_num(match, "revenue"))
            sbc.append(_num(r, "stockBasedCompensation"))
            capex.append(_num(r, "capitalExpenditure"))
            fcf_p.append(_num(r, "freeCashFlow"))
    pick = _select_visible(sessions, pairs)
    rev_a = np.array(rev)
    out["f_fcf_margin"] = _pick(_safe_ratio(fcf_p, rev_a), pick)
    out["f_sbc_rev"] = _pick(_safe_ratio(sbc, rev_a), pick)
    out["f_capex_rev"] = _pick(_safe_ratio(-np.array(capex), rev_a), pick)
    return pd.DataFrame(out, index=pd.Index(sessions))[list(_F_STATEMENT_COLUMNS)]


def fundamentals(daily: Dict[str, pd.DataFrame], ctx: BuildContext) -> pd.DataFrame:
    """Group ``f_``: point-in-time fundamentals of a STOCK (building it for an ETF raises: no statements are cached).

    * the existing ``ml_panel`` table (``fundamentals_table`` + ``fundamental_features``): quarterly earnings
      surprise and year-over-year growth, annual margins and revenue growth, days since the last earnings, trailing
      earnings yield, with its visibility rules (earnings: ``bmo`` the same session, otherwise the next; annual
      statements: the session after ``acceptedDate``);
    * from the annual balance sheet and cash flow: ``f_debt_assets`` = totalLiabilities / totalAssets,
      ``f_current_ratio`` = totalCurrentAssets / totalCurrentLiabilities, ``f_fcf_margin`` = freeCashFlow / revenue,
      ``f_sbc_rev`` = stockBasedCompensation / revenue, ``f_capex_rev`` = -capitalExpenditure / revenue (revenue of the
      income statement of the same fiscal date), ``f_fcf_yield`` = freeCashFlow / market cap and ``f_buyback_yield`` =
      -commonStockRepurchased / market cap, the market cap being the metric-store scan visible at the session (NaN
      before the first scan, 2020-01, and for a symbol the scan lacks). These two (``F_MARKET_CAP_COLUMNS``) are NaN
      BEFORE 2020 by design and are left out of the group's coverage columns, so the coverage mask does not blank the
      other fourteen columns for the years before the metric store starts; ``column_coverage`` still shows their holes.

    Ratios are clipped to [-5, 5]; a non-positive denominator gives NaN. The statements are FMP's latest restated
    figures with their filing dates (``acceptedDate``): the figures are not first-print, the visibility is."""
    from app.services import ml_panel

    ext_first = (ctx.sessions[0] - pd.Timedelta(days=_F_HISTORY_DAYS)).date()
    ext = [t.date() for t in _sessions_index(ext_first, ctx.sessions[-1].date())]
    dates = [t.date() for t in ctx.sessions]
    caps = ml_universe.scan_values(list(ctx.symbols), ctx.sessions, ["market_cap"], ctx.store_dir)["market_cap"]
    frames: Dict[str, pd.DataFrame] = {}
    files = ctx.provenance.setdefault("fmp_history", {})
    for sym in ctx.symbols:
        statements = load_statements(sym, ctx.cache_root)                       # refuses a missing file / key first
        table = ml_panel.fundamentals_table(sym, ext, ctx.cache_root)
        for kind in _F_FILES:
            path = fmp_history_path(kind, sym, ctx.cache_root)
            files[path.name] = _fingerprint(path)
        b = pd.DataFrame({"session": dates, "close": daily[sym]["close"].to_numpy()}, index=ctx.sessions)
        base = ml_panel.fundamental_features(b, table)[list(_F_PANEL_COLUMNS)]
        stm = _statement_features(sym, dates, caps[sym].to_numpy(dtype="float64"), statements)
        stm.index = ctx.sessions
        frames[sym] = pd.concat([base, stm], axis=1)[list(F_COLUMNS)]
    return pd.concat(frames, names=_INDEX_NAMES).astype("float64")


# ------------------------------------------------------------------------------------------ dated event files (A, I)
def _read_fmp_rows(kind: str, symbol: str, ctx: BuildContext) -> List[dict]:
    """The rows of ``fmp_history/<kind>__<symbol>.json`` (``PanelDataError`` if the file is missing) and its fingerprint
    into the provenance. Every event-file group reads ITS kinds through here only."""
    from app.services import ml_panel
    rows = ml_panel._load_json(ctx.cache_root, kind, symbol)
    path = fmp_history_path(kind, symbol, ctx.cache_root)
    ctx.provenance.setdefault("fmp_history", {})[path.name] = _fingerprint(path)
    return rows


def _event_days(rows: List[dict], key: str, symbol: str, kind: str) -> np.ndarray:
    """The calendar day (datetime64[D], the date part of a date or timestamp string) of each row's ``key``."""
    out = np.empty(len(rows), dtype="datetime64[D]")
    for i, r in enumerate(rows):
        try:
            out[i] = np.datetime64(str(r[key])[:10], "D")
        except (KeyError, ValueError):
            raise PanelDataError(f"{symbol}: {kind} row {i} has no readable {key!r}")
    return out


def _window_bounds(event_days: np.ndarray, sessions: pd.DatetimeIndex, days: int,
                   shift_days: int = 0) -> Tuple[np.ndarray, np.ndarray]:
    """For each session S the slice ``[lo, hi)`` of the (sorted) events dated in ``[T - days, T)`` with ``T = S - shift_days``:
    an event is public from the session AFTER its date, so at T exactly the events dated before T are visible; older ones fall
    out. ``shift_days`` > 0 is the window as it stood that many calendar days earlier."""
    s_days = sessions.values.astype("datetime64[D]") - np.timedelta64(int(shift_days), "D")
    return (np.searchsorted(event_days, s_days - np.timedelta64(days, "D"), side="left"),
            np.searchsorted(event_days, s_days, side="left"))


def _csum(values: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(values, dtype="float64")])


def _window_sum(csum: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    return csum[hi] - csum[lo]


def _distinct(codes: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Distinct codes (>= 0; -1 is "no name") in each window."""
    out = np.zeros(len(lo))
    for i in range(len(lo)):
        w = codes[lo[i]:hi[i]]
        out[i] = np.unique(w[w >= 0]).size
    return out


def _codes(names: List[object]) -> np.ndarray:
    """Integer code per distinct non-empty name; -1 for a missing / empty name."""
    book: Dict[str, int] = {}
    out = np.full(len(names), -1, dtype=np.int64)
    for i, n in enumerate(names):
        if n is None or str(n).strip() == "":
            continue
        out[i] = book.setdefault(str(n).strip(), len(book))
    return out


#: A CAPPED history (the vendor keeps a limited number of rows or started its feed late) has a first retained row; a 90-day
#: window reaching back before it is partly truncated. Coverage of a capped file therefore starts this many calendar days
#: after the first retained row (or the vendor start). An UNCAPPED file is covered from the panel start: its empty windows
#: and zero counts are real values.
COVERAGE_WARMUP_DAYS = 90
#: FMP's ``insider_v2`` keeps at most this many rows per symbol (1087 of 5058 files hold exactly 2000): a file with this many
#: rows is capped and its first retained filing is a truncation, not the symbol's first filing.
INSIDER_ROW_CAP = 2000
#: Earliest ``publishedDate`` of ANY ``price_target`` file in the cache (3729 non-empty files, checked 2026-10-28): the feed holds
#: nothing before it for any symbol. It is a FLOOR, not the day the feed was complete: the feed onboards symbols over time (the
#: first rows of the files spike in 2022-04/05; Agilent has no target before 2022-04-29; AAPL has only 3 in 2021), so each
#: file's coverage starts at max(its first row, this floor) + 90 days - an empty window before that means "the feed did not cover
#: the symbol yet", not "no targets". (The feed also thins from 2023-03: a density measure for later, not modelled here.)
PRICE_TARGET_VENDOR_START = date(2021, 4, 12)
_FAR_PAST = np.datetime64("1900-01-01", "D")


def _coverage_start(event_days: np.ndarray, *, capped: bool, floor_day: Optional[date] = None,
                    warmup_days: int = COVERAGE_WARMUP_DAYS) -> Optional[np.datetime64]:
    """The first day with coverage, or None for an empty file (never covered: whether the vendor has the symbol is unknown).

    * ``capped``: ``warmup_days`` after the earliest retained event;
    * uncapped: the panel start (``_FAR_PAST``);
    * ``floor_day`` (a vendor start): coverage never starts before ``floor_day + warmup_days``."""
    if len(event_days) == 0:
        return None
    start = event_days.min() + np.timedelta64(int(warmup_days), "D") if capped else _FAR_PAST
    if floor_day is not None:
        start = max(start, np.datetime64(floor_day, "D") + np.timedelta64(int(warmup_days), "D"))
    return start


def _covered_from(event_days: np.ndarray, sessions: pd.DatetimeIndex, *, capped: bool, floor_day: Optional[date] = None,
                  shift_days: int = 0) -> np.ndarray:
    """Sessions with coverage (evaluated at ``S - shift_days``); all False for an empty file. See ``_coverage_start``."""
    start = _coverage_start(event_days, capped=capped, floor_day=floor_day)
    if start is None:
        return np.zeros(len(sessions), dtype=bool)
    return sessions.values.astype("datetime64[D]") - np.timedelta64(int(shift_days), "D") >= start


# ------------------------------------------------------------------------------------------ groups AG / AT (analysts)
#: a mean analyst target further than this (as a ratio to the close, minus 1) from the close is not a view, it is an adjustment
#: mismatch between the target feed and the price series: the build refuses instead of clipping it
MAX_TARGET_GAP = 5.0
AG_COLUMNS: Tuple[str, ...] = ("ag_net_upgrades_30", "ag_net_upgrades_90", "ag_n_graders_90", "ag_covered")
AT_COLUMNS: Tuple[str, ...] = ("at_target_gap", "at_target_rev_30", "at_n_analysts_90", "at_covered")
#: `action` values of analyst_grades. Upgrades / downgrades score +1 / -1; everything in NEUTRAL_ACTIONS scores 0 (a
#: first-time rating, a repeated one, a resumed coverage ... is not a change of view). Anything else refuses, so a new
#: vendor spelling is looked at instead of silently counted as neutral. (The real cache only has upgrade, downgrade,
#: maintain and one empty string.)
_ACTION_SCORE: Dict[str, int] = {"upgrade": 1, "downgrade": -1}
NEUTRAL_ACTIONS: Tuple[str, ...] = ("maintain", "", "initialize", "initialise", "initiate", "initiated", "init", "reiterate",
                                    "reiterated", "reaffirm", "assume", "assumed", "resume", "resumed", "none")


def _grades_one(ctx: BuildContext, symbol: str) -> pd.DataFrame:
    sessions = ctx.sessions
    grades = _read_fmp_rows("analyst_grades", symbol, ctx)
    gd = _event_days(grades, "date", symbol, "analyst_grades")
    score = np.zeros(len(grades))
    for i, r in enumerate(grades):
        action = (str(r["action"]) if "action" in r and r["action"] is not None else "").strip().lower()
        if action in _ACTION_SCORE:
            score[i] = _ACTION_SCORE[action]
        elif action not in NEUTRAL_ACTIONS:
            raise PanelDataError(f"{symbol}: analyst_grades has unknown action {action!r} (scoring {sorted(_ACTION_SCORE)}, "
                                 f"neutral {sorted(NEUTRAL_ACTIONS)}); add it to one of them")
    order = np.argsort(gd, kind="stable")
    gd, score = gd[order], score[order]
    codes = _codes([grades[i].get("gradingCompany") for i in order])
    cov = _covered_from(gd, sessions, capped=False)
    cs = _csum(score)
    out: Dict[str, np.ndarray] = {}
    for n in (30, 90):
        lo, hi = _window_bounds(gd, sessions, n)
        out[f"ag_net_upgrades_{n}"] = np.where(cov, _window_sum(cs, lo, hi), np.nan)
    lo, hi = _window_bounds(gd, sessions, 90)
    out["ag_n_graders_90"] = np.where(cov, _distinct(codes, lo, hi), np.nan)
    out["ag_covered"] = cov.astype("float64")
    return pd.DataFrame(out, index=sessions)[list(AG_COLUMNS)]


def analyst_grades(daily: Dict[str, pd.DataFrame], ctx: BuildContext) -> pd.DataFrame:
    """Group ``ag_``: analyst GRADE changes from ``analyst_grades__<SYM>.json`` only (``date``, ``gradingCompany``,
    ``action``). A row is public from the session AFTER its date; windows are ``[S - N days, S)``.

    * ``ag_net_upgrades_30`` / ``_90``: upgrades minus downgrades in the window (``NEUTRAL_ACTIONS`` score 0);
    * ``ag_n_graders_90``: distinct ``gradingCompany`` in the 90-day window;
    * ``ag_covered``: 1 from the panel start for a non-empty file (the grade history, from 2012, is not capped inside the panel:
      an empty window is a real 0), 0 for an empty file, whose columns are NaN.

    FMP's consensus / summary files are never opened (they are windowed to about the last quarter: look-ahead before it).
    A missing file or an unknown action refuses; an empty file is never covered."""
    frames = {sym: _grades_one(ctx, sym) for sym in ctx.symbols}
    return pd.concat(frames, names=_INDEX_NAMES).astype("float64")


def _target_arrays(rows: List[dict], symbol: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(dates, adjusted targets, analyst codes) of the rows that carry a target, sorted by date."""
    keep = [i for i, r in enumerate(rows) if _num(r, "adjPriceTarget") > 0]       # the other rows carry no target
    td = _event_days([rows[i] for i in keep], "publishedDate", symbol, "price_target")
    adj = np.array([_num(rows[i], "adjPriceTarget") for i in keep])
    order = np.argsort(td, kind="stable")
    return td[order], adj[order], _codes([rows[keep[i]].get("analystCompany") for i in order])


def _target_means(sessions: pd.DatetimeIndex, td: np.ndarray, adj: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(covered flag, mean of the 90-day window now, the same mean as it stood 30 calendar days earlier); the means are NaN
    outside coverage or when the window has no target."""
    cov = _covered_from(td, sessions, capped=True, floor_day=PRICE_TARGET_VENDOR_START)
    cov_before = _covered_from(td, sessions, capped=True, floor_day=PRICE_TARGET_VENDOR_START, shift_days=30)
    csum = _csum(adj)

    def mean90(shift_days: int) -> np.ndarray:
        lo, hi = _window_bounds(td, sessions, 90, shift_days)
        count = (hi - lo).astype("float64")
        out = np.full(len(sessions), np.nan)
        has = count > 0
        out[has] = _window_sum(csum, lo, hi)[has] / count[has]
        return out
    return cov, np.where(cov, mean90(0), np.nan), np.where(cov_before, mean90(30), np.nan)


def _target_ratios(symbol: str, sessions: pd.DatetimeIndex, close: np.ndarray, now: np.ndarray,
                   before: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """(at_target_gap, at_target_rev_30). A value beyond +-MAX_TARGET_GAP is not a view but a price-adjustment mismatch between
    the target feed and the price series (or a broken target): it refuses, naming the symbol, column and first date, and is never
    clipped."""
    gap = np.full(len(sessions), np.nan)
    ok = np.isfinite(now) & (close > 0)
    gap[ok] = now[ok] / close[ok] - 1.0
    rev = np.full(len(sessions), np.nan)
    ok_r = np.isfinite(now) & np.isfinite(before) & (before > 0)
    rev[ok_r] = now[ok_r] / before[ok_r] - 1.0
    for name, series in (("at_target_gap", gap), ("at_target_rev_30", rev)):
        wild = np.isfinite(series) & (np.abs(series) > MAX_TARGET_GAP)
        if wild.any():
            i = int(np.argmax(wild))
            raise PanelDataError(f"{symbol}: {name} {series[i]:+.1f} on {sessions[i].date()} (mean adjPriceTarget {now[i]:.2f}, "
                                 f"close {close[i]:.2f}, 30 days earlier {before[i]:.2f}) is beyond +-{MAX_TARGET_GAP:g}: a "
                                 f"price-adjustment mismatch or a broken target, refusing to clip it")
    return gap, rev


def check_target_ratios(symbol: str, daily: pd.DataFrame, cache_root: Path) -> None:
    """The pre-screen of group AT: the ratio refusals of ``_targets_one`` for one symbol, on its loaded daily frame."""
    from app.services import ml_panel
    td, adj, _ = _target_arrays(ml_panel._load_json(cache_root, "price_target", symbol), symbol)
    sessions = daily.index
    _, now, before = _target_means(sessions, td, adj)
    _target_ratios(symbol, sessions, daily["close"].to_numpy(dtype="float64"), now, before)


def _targets_one(ctx: BuildContext, symbol: str, close: np.ndarray) -> pd.DataFrame:
    sessions = ctx.sessions
    td, adj, codes = _target_arrays(_read_fmp_rows("price_target", symbol, ctx), symbol)
    cov, now, before = _target_means(sessions, td, adj)
    gap, rev = _target_ratios(symbol, sessions, close, now, before)
    lo, hi = _window_bounds(td, sessions, 90)
    out = {"at_target_gap": gap, "at_target_rev_30": rev,
           "at_n_analysts_90": np.where(cov, _distinct(codes, lo, hi), np.nan), "at_covered": cov.astype("float64")}
    return pd.DataFrame(out, index=sessions)[list(AT_COLUMNS)]


def analyst_targets(daily: Dict[str, pd.DataFrame], ctx: BuildContext) -> pd.DataFrame:
    """Group ``at_``: analyst PRICE TARGETS from ``price_target__<SYM>.json`` only (``publishedDate``, ``analystCompany``,
    ``adjPriceTarget``); a row is public from the session AFTER its date part, windows ``[S - N days, S)``.

    * ``at_target_gap``: mean ``adjPriceTarget`` of the 90-day window over the S close, minus 1 (clipped to [-5, 5]); NaN
      when the window has no target. This is the group's coverage column;
    * ``at_target_rev_30``: that 90-day mean over the same mean as seen 30 CALENDAR days earlier, minus 1 (NaN unless both
      instants are covered and have a target);
    * ``at_n_analysts_90``: distinct ``analystCompany`` in the window (two targets of one company count once);
    * ``at_covered``: 1 from max(the file's first row, ``PRICE_TARGET_VENDOR_START``) + ``COVERAGE_WARMUP_DAYS``: the feed
      onboards symbols over time, so every file is treated as capped at its own first row; always 0 for an empty file.

    Before coverage every column but the flag is NaN; an empty window after it is 0 names and no gap (``at_n_analysts_90`` is
    the availability measure; the gap is NaN where no target was published, which is information, not a hole). A gap beyond
    ``MAX_TARGET_GAP`` refuses (naming symbol and date), and so does a revision beyond it; the pre-screen (``screen_symbols``)
    finds both so a bad symbol is listed as refused instead of aborting the build. A missing file refuses."""
    frames = {sym: _targets_one(ctx, sym, daily[sym]["close"].to_numpy(dtype="float64")) for sym in ctx.symbols}
    return pd.concat(frames, names=_INDEX_NAMES).astype("float64")


# ------------------------------------------------------------------------------------------ group I (insider)
I_COLUMNS: Tuple[str, ...] = ("i_net_buy_90", "i_n_buyers_90", "i_covered")
#: the column that divides by the SCREENER market cap (NaN before the metric store's first scan, 2020-01): not a coverage column
I_MARKET_CAP_COLUMNS: Tuple[str, ...] = ("i_net_buy_90",)


def _insider_one(ctx: BuildContext, symbol: str, market_cap: np.ndarray) -> pd.DataFrame:
    sessions = ctx.sessions
    rows = _read_fmp_rows("insider_v2", symbol, ctx)
    days = _event_days(rows, "filingDate", symbol, "insider_v2")
    order = np.argsort(days, kind="stable")
    days = days[order]
    rows = [rows[i] for i in order]
    dollars = np.zeros(len(rows))
    buyers: List[object] = [None] * len(rows)
    for i, r in enumerate(rows):
        if "formType" not in r or str(r["formType"]).strip() != "4":            # Form 4 only (not 3, 5, or amendments)
            continue
        kind = str(r["transactionType"]).strip() if "transactionType" in r and r["transactionType"] is not None else ""
        value = _num(r, "securitiesTransacted") * _num(r, "price")
        if not np.isfinite(value):
            continue
        if kind.startswith("P-") and value > 0:                  # a zero-price "purchase" (a grant coded P) is not a buyer
            dollars[i] = value
            who = r["reportingCik"] if "reportingCik" in r and r["reportingCik"] else (r["reportingName"] if "reportingName" in r else None)
            buyers[i] = who
        elif kind.startswith("S-"):
            dollars[i] = -value
    covered = _covered_from(days, sessions, capped=len(rows) >= INSIDER_ROW_CAP)
    lo, hi = _window_bounds(days, sessions, 90)
    net = _window_sum(_csum(dollars), lo, hi)
    out = {
        "i_net_buy_90": np.where(covered, _safe_ratio(net, market_cap), np.nan),
        "i_n_buyers_90": np.where(covered, _distinct(_codes(buyers), lo, hi), np.nan),
        "i_covered": covered.astype("float64")}
    return pd.DataFrame(out, index=sessions)[list(I_COLUMNS)]


def insiders(daily: Dict[str, pd.DataFrame], ctx: BuildContext) -> pd.DataFrame:
    """Group ``i_``: insider trading from ``insider_v2__<SYM>.json`` (Form 4 filings), visible the session AFTER the
    ``filingDate`` (the filing, not the transaction, is what the market can see); windows are ``[S - 90 days, S)`` of filing
    dates.

    * ``i_net_buy_90``: (shares x price of ``P-`` purchases minus ``S-`` sales, Form 4 only) over the window, over the
      store's market cap visible at S (NaN before the first scan, 2020-01, and for a symbol the scan lacks; this column
      is not a coverage column of the group - ``I_MARKET_CAP_COLUMNS`` - so the mask does not blank ``i_n_buyers_90``
      before 2020 for it);
    * ``i_n_buyers_90``: distinct reporting persons (``reportingCik``) with a purchase in the window;
    * ``i_covered``: for a CAPPED file (``INSIDER_ROW_CAP`` = 2000 rows, FMP's limit) 1 from ``COVERAGE_WARMUP_DAYS`` (90)
      calendar days after the first retained filing, else 0: the oldest filings were dropped, and a window reaching back
      before the first retained row is truncated (this DELIBERATELY deviates from the plan's "from the first filing"). For an
      UNCAPPED file (fewer rows: the whole history is there) it is 1 from the panel start, an empty window being a real 0.
      An empty file is never covered. Before coverage the other two columns are NaN.

    A missing file refuses; an empty file is never covered (flag 0, the others NaN)."""
    caps = ml_universe.scan_values(list(ctx.symbols), ctx.sessions, ["market_cap"], ctx.store_dir)["market_cap"]
    frames = {sym: _insider_one(ctx, sym, caps[sym].to_numpy(dtype="float64")) for sym in ctx.symbols}
    return pd.concat(frames, names=_INDEX_NAMES).astype("float64")


# ------------------------------------------------------------------------------------------ group S (market structure)
#: Pinned manifests of the platform's market-condition store (``<cache>/market_conditions/<profile>/manifests/<sha>.json``),
#: oldest first; for a symbol the first manifest that covers it is used. They are the newest ALPACA-sourced
#: (``alpaca-daily-split-verified-v1``, calc-2, ``prior_session_v1``) manifests: the option-universe one (97 symbols) and
#: the 753-symbol one. Rows exist 2020-01-02 .. 2025-12-31 only, for those universes only; anything else is NaN.
S_MANIFESTS: Dict[str, Tuple[str, ...]] = {
    "ohlcv-v1": ("2b958769b8a44539d90b4f9b3e156a3e9a5461c425cb717c118400eb999c275f",
                 "59f8826b7381baa6039c8b5c2467c785aa0770e2435cf717537880183fa78d65"),
    "ta-structure-v1": ("7e1904763ec74ca61aa6f580f1328630c160e4de141bd93a4252fb71bdc42070",
                        "8fac4e9de1d2b68669d34b87ed121765979ae3f51a22e7eeefd8ea40869859d7")}
#: column -> (profile, store field, scale). ADX is divided by 100 (as ``t_adx``) and the bars-since counts by the 128-session
#: window, so every column is a ratio / ATR multiple / small count / 0-1 value.
S_FIELDS: Dict[str, Tuple[str, str, float]] = {
    "s_trend_slope_atr": ("ohlcv-v1", "underlying_trend_slope_50_atr14", 1.0),
    "s_adx": ("ohlcv-v1", "underlying_adx_14", 0.01),
    "s_rv_ratio": ("ohlcv-v1", "underlying_realized_vol_ratio_5_20", 1.0),
    "s_dist_support_atr": ("ta-structure-v1", "structure_dist_support_atr", 1.0),
    "s_dist_resistance_atr": ("ta-structure-v1", "structure_dist_resistance_atr", 1.0),
    "s_support_touches": ("ta-structure-v1", "structure_support_touches", 1.0),
    "s_resistance_touches": ("ta-structure-v1", "structure_resistance_touches", 1.0),
    "s_channel_slope_20_atr": ("ta-structure-v1", "channel_slope_20_atr", 1.0),
    "s_channel_width_20_atr": ("ta-structure-v1", "channel_width_20_atr", 1.0),
    "s_channel_pos_20": ("ta-structure-v1", "channel_pos_20", 1.0),
    "s_close_vs_prior_high_20_atr": ("ta-structure-v1", "close_vs_prior_high_20_atr", 1.0),
    "s_close_vs_prior_low_20_atr": ("ta-structure-v1", "close_vs_prior_low_20_atr", 1.0),
    "s_structure_state": ("ta-structure-v1", "structure_state", 1.0),
    "s_bars_since_bos": ("ta-structure-v1", "structure_bars_since_bos", 1.0 / 128.0),
    "s_bars_since_choch": ("ta-structure-v1", "structure_bars_since_choch", 1.0 / 128.0)}
#: 1.0 where the symbol HAS a row in the structure store for that session (even if some of its values are invalid), else 0.0:
#: the per-row coverage flag of a group that has per-symbol holes (the group-year share cannot show which symbols lack rows).
S_COVERED = "s_covered"
S_COLUMNS: Tuple[str, ...] = tuple(S_FIELDS) + (S_COVERED,)


def _s_manifests(store, profile: str) -> List[Dict[str, object]]:
    """The pinned manifests of ``profile``, read with the platform's own reader and checked consistent: every one Alpaca-sourced
    (never FMP prices), on the registered calculator version, ``prior_session_v1``, with the profile's declared fields."""
    from ba2_common.core import market_conditions as mc
    out = []
    for digest in S_MANIFESTS[profile]:
        try:
            man = store.read_manifest(digest, profile)
        except (FileNotFoundError, ValueError) as e:
            raise PanelDataError(f"group s: {profile} manifest {digest[:12]} cannot be read ({e})") from e
        if not str(man["source_profile"]).startswith("alpaca"):
            raise PanelDataError(f"group s: {profile} manifest {digest[:12]} is sourced from {man['source_profile']!r}; only "
                                 f"Alpaca-sourced bars are allowed (never FMP prices)")
        if not mc.calc_version_accepted(profile, str(man["calc_version"])) or man["timing_policy"] != "prior_session_v1":
            raise PanelDataError(f"group s: {profile} manifest {digest[:12]} has calc {man['calc_version']!r} / timing "
                                 f"{man['timing_policy']!r}")
        if tuple(man["fields"]) != tuple(f.name for f in mc.PROFILES[profile].fields):
            raise PanelDataError(f"group s: {profile} manifest {digest[:12]} carries other fields than the registry's")
        out.append(man)
    if len({(m["source_profile"], m["calc_version"]) for m in out}) != 1:
        raise PanelDataError(f"group s: the pinned {profile} manifests mix sources or calculator versions")
    return out


def structure(daily: Dict[str, pd.DataFrame], ctx: BuildContext) -> pd.DataFrame:
    """Group ``s_``: chart-structure measurements from the platform's market-condition store (``ohlcv-v1``: trend slope, ADX,
    realised-volatility ratio; ``ta-structure-v1``: support / resistance distance and touches, 20-session channel, closes
    vs the prior 20-session high / low, structure state, bars since a break of structure / change of character), read through
    ``MarketConditionStore`` at the pinned manifests (``S_MANIFESTS``).

    TIMING. The store row of session T is computed from the 128 bars ENDING AT T (the retained window's last bar is T; pivots are
    only used once confirmed), so it is public at T's close: the row of S is used at S, unshifted. (The platform's own
    ``prior_session_v1`` policy has a decision on D read the row of the session BEFORE D; our decision is at the close of
    S, with S's bar complete. A test on the real store checks the window's last bar equals the row's session.)

    A value whose status is not ``valid`` is NaN: ``insufficient_history`` is meaningful (no confirmed pivot above the close
    means no resistance), so several structure columns are NaN by design on many rows. Symbols the manifests do not cover,
    and sessions outside their window (2020-01-02 .. 2025-12-31), are NaN; the uncovered symbols are listed in
    ``meta["source"]["market_conditions"]``. ``s_covered`` is 1.0 where the symbol has a row of the structure profile for the
    session, else 0.0 (a per-row flag for the holes a group-year share cannot show)."""
    from ba2_common.core.market_condition_store import MarketConditionStore, status_code
    from ba2_common.core.market_conditions import STATUS_VALID

    root = Path(ctx.cache_root)
    if not (root / "market_conditions").is_dir():
        raise PanelDataError(f"group s: no market_conditions store under {root}")
    store = MarketConditionStore(root)
    valid = status_code(STATUS_VALID)
    by_profile = {p: _memoised(ctx, ("s_manifests", p, tuple(S_MANIFESTS[p]), str(root)), lambda p=p: _s_manifests(store, p))
                  for p in S_MANIFESTS}
    prov: Dict[str, Dict[str, object]] = {}
    per_symbol: Dict[str, Dict[str, np.ndarray]] = {}
    for profile, mans in by_profile.items():
        uncovered: List[str] = []
        for sym in ctx.symbols:
            man = next((m for m in mans if sym in m["coverage"]), None)
            cols = {c: np.full(len(ctx.sessions), np.nan) for c, (p, _, _) in S_FIELDS.items() if p == profile}
            if profile == "ta-structure-v1":
                cols[S_COVERED] = np.zeros(len(ctx.sessions))
            if man is None:
                uncovered.append(sym)
            else:
                rf = store.rows_frame(man, sym)
                pos = ctx.sessions.values.astype("datetime64[D]")
                where = np.searchsorted(rf.sessions, pos)
                where_c = np.minimum(where, max(len(rf) - 1, 0))
                have = (where < len(rf)) & (rf.sessions[where_c] == pos) if len(rf) else np.zeros(len(pos), dtype=bool)
                if S_COVERED in cols:
                    cols[S_COVERED] = have.astype("float64")
                for c in [c for c in cols if c != S_COVERED]:
                    _, field, scale = S_FIELDS[c]
                    j = rf.fields.index(field)
                    good = have & (rf.status[where_c, j] == valid) if len(rf) else have
                    cols[c][good] = rf.values[where_c[good], j] * scale
            per_symbol.setdefault(sym, {}).update(cols)
        prov[profile] = {"manifests": list(S_MANIFESTS[profile]), "source_profile": mans[0]["source_profile"],
                         "calc_version": mans[0]["calc_version"],
                         "window": [min(m["window_start"] for m in mans), max(m["window_end"] for m in mans)],
                         "uncovered": uncovered}
    ctx.provenance["market_conditions"] = prov
    frames = {sym: pd.DataFrame(per_symbol[sym], index=ctx.sessions)[list(S_COLUMNS)] for sym in ctx.symbols}
    return pd.concat(frames, names=_INDEX_NAMES).astype("float64")


#: group name -> spec. The name is the grid factor; the spec's prefix names its columns. Later tasks register here.
GROUPS: Dict[str, GroupSpec] = {
    "t": GroupSpec(technical, "t_"),
    "mtf": GroupSpec(multi_timeframe, "mtf_"),
    "m": GroupSpec(market, "m_", needs_sector=True, required_context=_market_context,
                   sectorless_exempt=("m_rs_sector_5", "m_rs_sector_20")),
    "x": GroupSpec(cross_section, "x_"),
    "mac": GroupSpec(macro, "mac_", coverage_columns=tuple(
        c for c in MAC_COLUMNS if not any(c.startswith(f"mac_{sid.lower()}_") for sid in KNOWN_STALLS))),
    # the screener market cap starts with the metric store (2020-01): the yield columns divide by it and are NaN before it BY
    # DESIGN (documented in ``fundamentals``), so they must not make the whole group look uncovered and masked before 2020
    "f": GroupSpec(fundamentals, "f_", coverage_columns=tuple(c for c in F_COLUMNS if c not in F_MARKET_CAP_COLUMNS)),
    "ag": GroupSpec(analyst_grades, "ag_", coverage_columns=("ag_net_upgrades_30", "ag_net_upgrades_90", "ag_n_graders_90")),
    "at": GroupSpec(analyst_targets, "at_", coverage_columns=("at_n_analysts_90",)),
    "i": GroupSpec(insiders, "i_", coverage_columns=tuple(c for c in I_COLUMNS if c not in I_MARKET_CAP_COLUMNS
                                                         and c != "i_covered")),
    "s": GroupSpec(structure, "s_", coverage_columns=tuple(
        c for c in S_COLUMNS if c not in ("s_dist_support_atr", "s_dist_resistance_atr", "s_support_touches",
                                          "s_resistance_touches", "s_bars_since_bos", "s_bars_since_choch", S_COVERED))),
}


def _spec(group: str) -> GroupSpec:
    if group not in GROUPS:
        raise ValueError(f"unknown feature group {group!r}; known {sorted(GROUPS)}")
    return GROUPS[group]


def feature_columns(df: pd.DataFrame, groups: Optional[Iterable[str]] = None) -> List[str]:
    """The feature columns of a built frame (prefix of a registered group), in frame order; ``listed``, ``filled``,
    ``label_bar_filled`` and any other unprefixed column is not a feature. Raises on an unknown group."""
    prefixes = tuple(_spec(g).prefix for g in (GROUPS if groups is None else groups))
    return [c for c in df.columns if c.startswith(prefixes)]


# ------------------------------------------------------------------------------------------ the store
def _check_contract(group: str, spec: GroupSpec, cols: pd.DataFrame, ctx: BuildContext) -> None:
    """A builder returns exactly ``ctx.symbols`` x ``ctx.sessions`` (in that order), float columns, all prefixed."""
    expected = pd.MultiIndex.from_product([list(ctx.symbols), ctx.sessions], names=_INDEX_NAMES)
    if not cols.index.equals(expected):
        raise ValueError(f"group {group!r}: index must be [symbols x sessions] ({len(expected)} rows, symbols in "
                         f"order), got {len(cols)} rows")
    bad = [c for c in cols.columns if not c.startswith(spec.prefix)]
    if bad:
        raise ValueError(f"group {group!r} produced columns without its prefix {spec.prefix!r}: {bad}")
    notfloat = [c for c in cols.columns if not pd.api.types.is_float_dtype(cols[c])]
    if notfloat:
        raise ValueError(f"group {group!r}: columns must be float, got non-float {notfloat}")
    inf = np.isinf(cols.to_numpy(dtype="float64"))
    if inf.any():
        r, c = np.argwhere(inf)[0]
        sym, day = cols.index[r]
        raise PanelDataError(f"group {group!r}: infinite {cols.columns[c]} for {sym} at {pd.Timestamp(day).date()} "
                             f"({int(inf.sum())} infinite values); refusing to turn it into NaN")


def _nan_rates(out: pd.DataFrame, groups: Sequence[str]) -> Dict[str, float]:
    """Share of LISTED rows that are NaN, per feature column of ``groups``."""
    listed = out["listed"].to_numpy()
    return {c: (float(out.loc[listed, c].isna().mean()) if listed.any() else 1.0) for c in feature_columns(out, groups)}


def _membership_digest(membership: pd.DataFrame) -> str:
    h = hashlib.sha256()
    h.update(np.asarray(membership.index.values, dtype="datetime64[ns]").tobytes())
    h.update(",".join(map(str, membership.columns)).encode())
    h.update(np.ascontiguousarray(membership.to_numpy(dtype=bool)).tobytes())
    return h.hexdigest()[:16]


def _membership_record(membership: pd.DataFrame) -> Dict[str, object]:
    """Provenance of a membership: size, first member session, a digest of the whole mask, and the parameters it was
    made with (``ml_universe.membership_from_universe`` stores them in ``attrs``; None for a hand-made frame)."""
    params = membership.attrs["membership"] if "membership" in membership.attrs else {}
    any_member = membership.any(axis=1).to_numpy()
    return {"symbols": int(membership.shape[1]), "sessions": int(membership.shape[0]), "digest": _membership_digest(membership),
            "first_member_session": str(membership.index[any_member][0].date()) if any_member.any() else None,
            "top_n": params["top_n"] if "top_n" in params else None,
            "purpose": params["purpose"] if "purpose" in params else None,
            "min_dollar_volume": params["min_dollar_volume"] if "min_dollar_volume" in params else None}


def _refuse_if_store_lists_ended(ended: Dict[str, str], store_path: Path) -> None:
    """A series that looks ended (its last bar is far before the cache end) while the metric store still has a scan row
    for it MORE THAN ``STALENESS_MAX_DAYS`` calendar days after that bar is a download hole, not a delisting: STALE,
    refused. (The store keeps a name until its latest bar is older than that limit, so a genuinely delisted name has
    scans 1..7 days after its last bar.)"""
    if not ended or not Path(store_path).exists():
        return
    limit = pd.Timedelta(days=ms.STALENESS_MAX_DAYS)
    for sym, last in ended.items():
        newest = ml_universe.last_scan_with(sym, store_path)
        if newest is not None and pd.Timestamp(newest) > pd.Timestamp(last) + limit:
            raise StaleSeriesError(f"{sym}: the metric store still lists it on {newest}, more than {ms.STALENESS_MAX_DAYS} "
                                 f"days after its last cached bar {last}; the cached series is stale (a delisted name "
                                 f"leaves the store within {ms.STALENESS_MAX_DAYS} days), not ended")


def screen_symbols(symbols: Sequence[str], first: date, last: date, cache_root: Path, groups: Sequence[str] = (),
                   store_dir: Optional[Path] = None) -> Tuple[List[str], Dict[str, str], Dict[str, str]]:
    """Pre-screen ``symbols``: ``(usable, permanent, transient)``. ``permanent`` maps each symbol that ``load_daily`` (bars
    missing, a run of missing sessions, an invalid bar ...) or group F (``"f" in groups``: missing statement files or keys)
    would refuse to the reason; ``transient`` those refused with a ``StaleSeriesError`` (a stale download, or one the metric
    store still lists after its last bar), which can clear after a cache refresh. Classified by exception TYPE. Nothing is built."""
    from app.services import ml_panel
    cache_end = last_cached_bar("SPY", cache_root)
    store_path = Path(store_dir) if store_dir is not None else ml_universe.store_dir_for(cache_root)
    # the event files the groups read (group F has its own, fuller check); filtered once, not per symbol
    event_kinds = list(dict.fromkeys(k for g in groups if g != "f" for k in _FMP_KINDS_BY_GROUP.get(g, ())))
    usable: List[str] = []
    permanent: Dict[str, str] = {}
    transient: Dict[str, str] = {}
    for sym in symbols:
        try:
            frames, meta = load_daily([sym], min(first, first_cached_bar(sym, cache_root).date()), last, cache_root,
                                      cache_end=cache_end)
            _refuse_if_store_lists_ended(meta["ended"], store_path)
            if "f" in groups:
                check_fundamentals_files(sym, cache_root)
            for kind in event_kinds:                                      # the event files a group reads must exist
                ml_panel._load_json(cache_root, kind, sym)
            if "at" in groups:
                check_target_ratios(sym, frames[sym], cache_root)
        except StaleSeriesError as e:
            transient[sym] = str(e)
        except PanelDataError as e:
            permanent[sym] = str(e)
        else:
            usable.append(sym)
    return usable, permanent, transient


def usable_symbols(symbols: Sequence[str], first: date, last: date, cache_root: Path, groups: Sequence[str] = (),
                   store_dir: Optional[Path] = None) -> Tuple[List[str], Dict[str, str]]:
    """``(usable, refused)`` of ``screen_symbols`` with the permanent and the transient refusals together."""
    usable, permanent, transient = screen_symbols(symbols, first, last, cache_root, groups, store_dir)
    return usable, {**permanent, **transient}


def _fingerprint(p: Path) -> Dict[str, object]:
    st = p.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def build_features(symbols: Sequence[str], first_day: date, last_day: date, groups: Sequence[str],
                   cache_root: Path, context_symbols: Sequence[str] = (),
                   sector_of: Union[None, str, Dict[str, str]] = None,
                   universe: Optional[Sequence[str]] = None, store_dir: Optional[Path] = None,
                   sector_overrides: Optional[Dict[str, str]] = None,
                   membership: Optional[pd.DataFrame] = None,
                   preloaded: Optional[BuildCache] = None) -> pd.DataFrame:
    """Point-in-time daily features indexed ``[symbol, session]`` for ``symbols`` over ``[first_day, last_day]``.

    Every loaded symbol is read from its first cached bar (``first_day`` only trims the output), the requested
    ``groups`` are built on the whole history and the result is trimmed, so a row is the same whatever ``first_day``
    is. ``context_symbols`` are extra series to load; each group's own required context (market ETFs, the sector ETFs
    of ``symbols``) is added automatically. ``universe`` is the symbol set cross-sectional statistics are taken
    over: None means ``symbols``, otherwise a superset of ``symbols`` (build a large universe in chunks by passing
    the whole set every time). ``sector_of`` is a symbol -> sector dict, or ``"store"`` to read the sectors from the
    metric store at ``store_dir`` (default ``<cache_root>/screener/metric_store_alpaca_20261022``); a symbol the store
    has no sector for is simply absent, so a group that needs it refuses unless ``sector_overrides`` supplies it (the
    overrides win over both sources). ``membership`` (optional, sessions x symbols bool, e.g.
    ``ml_universe.membership_from_universe``) says who is in the universe on each session: group X ranks only the
    members of that session and group M's breadth counts only them; None means every universe symbol always (the
    NDQ30 stage). With a membership the universe defaults to ``symbols`` plus the membership's columns; its index must cover every build
    session from its first member session on (a gap would silently mean "no member"). DECISION: the final-holdout
    features are built on the UNION universe (dev + holdout, about 300 names), larger than the dev universe (about 200),
    so a holdout cross-section is not the dev one; pass the union membership for it. ``preloaded``
    (a ``BuildCache``) shares the loaded bars and the technical features across chunked builds of one window. The cache end (what ``ended`` is measured against) is SPY's last bar, so SPY
    must be cached.

    Columns: ``listed`` (a real bar exists: False before a symbol's first bar and after its last), ``ended`` (the
    series ended before the cache's end: rows after its last bar), ``filled`` (the session's bar is a fabricated flat
    fill), ``label_bar_filled`` (the fill bar of a decision at S is fabricated or does not exist: the NEXT session is
    filled or ended, so the trainer can drop the row; <NA> on the last session, whose next session is unknown; the
    only column that looks forward, and a label ingredient not a feature), then the feature columns of each group
    in the order given.
    Features are NaN for unlisted rows and while a symbol's own windows fill up. A computed +-inf raises.
    ``df.attrs["meta"]`` carries the load meta, the groups' columns, the universe and the source fingerprints.

    Raises PanelDataError on missing / inconsistent cached data (see ``load_daily``), ValueError on bad arguments."""
    symbols, groups, context_symbols = list(symbols), list(groups), list(context_symbols)
    if not symbols:
        raise ValueError("no symbols")
    if len(set(symbols)) != len(symbols):
        raise ValueError("duplicate symbols")
    if not groups or len(set(groups)) != len(groups):
        raise ValueError(f"groups must be a non-empty list without duplicates, got {groups}")
    specs = {g: _spec(g) for g in groups}
    if first_day > last_day:
        raise ValueError(f"first_day {first_day} is after last_day {last_day}")
    if membership is not None:
        if not all(pd.api.types.is_bool_dtype(t) for t in membership.dtypes):
            raise ValueError("membership must be a bool DataFrame (sessions x symbols)")
        needed = list(dict.fromkeys(symbols + list(membership.columns)))
        universe = needed if universe is None else list(dict.fromkeys(universe))
        if not set(needed) <= set(universe):
            raise ValueError(f"universe must contain the built symbols and every membership symbol; missing "
                             f"{sorted(set(needed) - set(universe))}")
    else:
        universe = list(symbols) if universe is None else list(dict.fromkeys(universe))
        if not set(symbols) <= set(universe):
            raise ValueError(f"universe must contain every built symbol; missing {sorted(set(symbols) - set(universe))}")
    store_path = Path(store_dir) if store_dir is not None else ml_universe.store_dir_for(cache_root)
    sector_source = None
    sectors: Optional[Dict[str, str]] = None
    if any(spec.needs_sector for spec in specs.values()):
        if isinstance(sector_of, str):
            if sector_of != "store":
                raise ValueError(f"sector_of must be a dict or 'store', got {sector_of!r}")
            sectors, sector_source = dict(ml_universe.sector_of_from_store(symbols, store_path)), "store"
        elif sector_of is not None:
            sectors, sector_source = dict(sector_of), "given"
        if sector_overrides:
            sectors = {**(sectors or {}), **sector_overrides}
        for g, spec in specs.items():
            if spec.needs_sector and sectors is None:
                raise PanelDataError(f"group {g!r} needs sector_of (symbol -> sector, or 'store'); got none")
    required: Set[str] = set()
    for spec in specs.values():
        required |= set(spec.required_context(symbols, sectors))
    load = list(dict.fromkeys(symbols + universe + context_symbols + sorted(required)))
    earliest = min(first_cached_bar(s, cache_root) for s in load)
    cache_end = last_cached_bar("SPY", cache_root)
    load_first = min(first_day, earliest.date())
    if preloaded is None:
        daily, load_meta = load_daily(load, load_first, last_day, cache_root, cache_end=cache_end)
    else:
        daily, load_meta = preloaded.load(load, load_first, last_day, cache_root, cache_end)
    _refuse_if_store_lists_ended(load_meta["ended"], store_path)
    sessions = next(iter(daily.values())).index
    if membership is not None:
        any_member = membership.any(axis=1).to_numpy()
        if any_member.any():
            first_member = membership.index[any_member][0]
            missing = sessions[sessions >= first_member].difference(membership.index)
            if len(missing):
                raise ValueError(
                    f"membership does not cover {len(missing)} sessions of the build from its first member session "
                    f"{first_member.date()} on (first missing: {missing[0].date()}, last missing: {missing[-1].date()}); "
                    f"build it over the build's sessions, a gap would silently mean 'no member'")
    mask = (None if membership is None
            else membership.reindex(index=sessions, columns=universe, fill_value=False).fillna(False).astype(bool))
    ctx = BuildContext(tuple(symbols), tuple(universe), tuple(s for s in load if s not in symbols), sessions,
                       first_day, last_day, Path(cache_root), store_path, dict(sectors) if sectors is not None else {},
                       mask, dict(sector_overrides) if sector_overrides else {}, cache=preloaded)
    parts: List[pd.DataFrame] = []
    per_group: Dict[str, List[str]] = {}
    for g, spec in specs.items():
        cols = spec.builder(daily, ctx)
        _check_contract(g, spec, cols, ctx)
        parts.append(cols)
        per_group[g] = list(cols.columns)
    flags = pd.concat({s: pd.DataFrame({
        "listed": daily[s]["listed"].astype(bool),
        "ended": daily[s]["ended"].astype(bool),
        "filled": daily[s]["filled"].astype(bool),
        "label_bar_filled": (daily[s]["filled"] | daily[s]["ended"]).astype("boolean").shift(-1)}) for s in symbols},
        names=_INDEX_NAMES)
    out = pd.concat([flags] + parts, axis=1)
    dup = out.columns[out.columns.duplicated()].tolist()
    if dup:
        raise ValueError(f"columns produced by more than one group: {dup}")
    feats = feature_columns(out, groups)
    out.loc[~out["listed"].to_numpy(), feats] = np.nan         # safety net: no feature exists before the listing
    day = out.index.get_level_values("session")
    out = out[(day >= pd.Timestamp(first_day)) & (day <= pd.Timestamp(last_day))]
    listed = out["listed"].to_numpy()
    nan_rate = _nan_rates(out, groups)
    if any(g in groups for g in ("x", "f", "i")):
        st = ml_universe.scan_store(store_path)
        ctx.provenance["store"] = {"dir": str(store_path), "newest_scan": st.dates[-1], "fingerprint": st.fingerprint}
    out.attrs["meta"] = {
        "first_day": str(first_day), "last_day": str(last_day), "groups": per_group, "symbols": symbols,
        "universe": universe, "context_symbols": [s for s in load if s not in symbols],
        "required_context": sorted(required), "load": load_meta, "rows": int(len(out)),
        "cache_end": str(cache_end), "sector_source": sector_source,
        "sectorless": sorted(sym for sym in symbols if sectors is not None and sectors.get(sym) == NO_SECTOR),
        "membership": (None if membership is None else _membership_record(membership)),
        "sector_overrides": dict(sector_overrides) if sector_overrides else {},
        "ended": dict(load_meta["ended"]), "nan_rate": nan_rate,
        "columns": list(out.columns),
        "source": {"daily": {s: _fingerprint(_cache_file(cache_root, s)) for s in load}, **ctx.provenance}}
    return out


# ------------------------------------------------------------------------------------------ coverage report and masking
def _group_columns(df: pd.DataFrame, group: str) -> List[str]:
    """Every feature column of ``group`` in ``df`` (the ``<prefix>missing`` flag is not a feature)."""
    prefix = _spec(group).prefix
    return [c for c in df.columns if c.startswith(prefix) and not c.endswith("missing")]


def _coverage_columns(df: pd.DataFrame, group: str) -> List[str]:
    """The columns that define the group's coverage in ``df`` (``GroupSpec.coverage_columns``, else all of its columns)."""
    spec = _spec(group)
    cols = _group_columns(df, group)
    if spec.coverage_columns is not None:
        cols = [c for c in cols if c in spec.coverage_columns]
    if not cols:
        raise ValueError(f"group {group!r}: no columns in the frame")
    return cols


def _years(df: pd.DataFrame) -> np.ndarray:
    return df.index.get_level_values("session").year.to_numpy()


def _sectorless_symbols(df: pd.DataFrame, sectorless: Optional[Iterable[str]]) -> List[str]:
    if sectorless is not None:
        return list(sectorless)
    meta = df.attrs["meta"] if "meta" in df.attrs else {}
    return list(meta["sectorless"]) if "sectorless" in meta else []


def coverage_report(df: pd.DataFrame, groups: Sequence[str], sectorless: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """Per (group, year) the share of LISTED rows in which every coverage column of the group is non-NaN.

    Returns a frame indexed ``[group, year]`` with ``rows`` (listed rows of the year), ``complete`` and ``share``. Unlisted rows
    are not counted; a year with no listed row is absent. The coverage columns of a group are those of its
    ``GroupSpec.coverage_columns`` (all of its columns by default). A group's ``sectorless_exempt`` columns are not required
    for the symbols in ``sectorless`` (default: ``df.attrs["meta"]["sectorless"]``, recorded by the build)."""
    listed = df["listed"].to_numpy(dtype=bool)
    year = _years(df)
    is_sectorless = np.asarray(df.index.get_level_values("symbol").isin(_sectorless_symbols(df, sectorless)))
    rows = []
    for g in groups:
        cols = _coverage_columns(df, g)
        complete_row = df[cols].notna().all(axis=1).to_numpy()
        exempt = [c for c in _spec(g).sectorless_exempt if c in cols]
        if exempt:
            relaxed = df[[c for c in cols if c not in exempt]].notna().all(axis=1).to_numpy()
            complete_row = np.where(is_sectorless, relaxed, complete_row)
        for y in np.unique(year[listed]):
            sel = listed & (year == y)
            n, ok = int(sel.sum()), int((sel & complete_row).sum())
            rows.append((g, int(y), n, ok, ok / n))
    out = pd.DataFrame(rows, columns=["group", "year", "rows", "complete", "share"]).set_index(["group", "year"])
    return out


def column_coverage(df: pd.DataFrame, groups: Sequence[str]) -> pd.DataFrame:
    """Per (column, year) the share of listed rows in which that single column is non-NaN (all columns of the groups). For
    a ``*_covered`` flag column (never NaN) the share is the share of listed rows where the flag is 1."""
    listed = df["listed"].to_numpy(dtype=bool)
    year = _years(df)
    rows = []
    for g in groups:
        for c in _group_columns(df, g):
            ok_col = (df[c] == 1.0).to_numpy() if c.endswith("_covered") else df[c].notna().to_numpy()
            for y in np.unique(year[listed]):
                sel = listed & (year == y)
                rows.append((c, int(y), int(sel.sum()), float((sel & ok_col).sum() / sel.sum())))
    return pd.DataFrame(rows, columns=["column", "year", "rows", "share"]).set_index(["column", "year"])


def apply_coverage_mask(df: pd.DataFrame, report: pd.DataFrame, min_share: float = 0.7) -> pd.DataFrame:
    """A copy of ``df`` where, for every (group, year) of ``report`` under ``min_share``, ALL the group's feature columns
    are NaN for that year, and a column ``<prefix>missing`` is 1.0 on those rows (0.0 elsewhere; one per group in the
    report). The ``*_covered`` flag columns are never masked. The masked (group, year) pairs are listed in
    ``out.attrs["coverage_masked"]``. A model sees a thin year as
    absent, with a flag, instead of a few real rows among NaNs."""
    out = df.copy()
    out.attrs = dict(df.attrs)
    year = _years(out)
    masked: List[Tuple[str, int]] = []
    for g in report.index.get_level_values("group").unique():
        sub = report.xs(g, level="group")
        bad = sorted(int(y) for y, share in sub["share"].items() if share < min_share)
        flag = np.isin(year, bad)
        cols = [c for c in _group_columns(out, g) if not c.endswith("_covered")]       # flags about the data stay intact
        out.loc[flag, cols] = np.nan
        out[f"{_spec(g).prefix}missing"] = flag.astype("float64")
        masked += [(g, y) for y in bad]
    out.attrs["coverage_masked"] = masked
    return out


# ------------------------------------------------------------------------------------------ the store build
ENSURE_VERSION = 1
_FMP_KINDS_BY_GROUP: Dict[str, Tuple[str, ...]] = {"f": _F_FILES, "ag": ("analyst_grades",), "at": ("price_target",),
                                                   "i": ("insider_v2",)}


def _package_versions() -> Dict[str, str]:
    """PACKAGE_VERSION of the shared packages the features are computed with (part of the rebuild key)."""
    import importlib
    return {m: importlib.import_module(f"{m}.version").PACKAGE_VERSION for m in ("ba2_common", "ba2_providers", "ba2_experts")}


def code_version() -> str:
    """16 hex digits of the sha256 of the feature code (``ml_features.py``, ``ml_universe.py``, ``ml_panel.py``): part of the
    store's rebuild key, so a change of logic rebuilds without ``--force`` (and so does a comment: the key is the bytes)."""
    from app.services import ml_panel
    h = hashlib.sha256()
    for path in (Path(__file__), Path(ml_universe.__file__), Path(ml_panel.__file__)):
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _fp_or_none(path: Path) -> Optional[Dict[str, object]]:
    return _fingerprint(path) if path.exists() else None


#: The files of a complete store; a store missing any of them is never "up to date".
STORE_FILES: Tuple[str, ...] = ("features.parquet", "features.parquet.meta.json", "coverage_report.parquet", "coverage_report.csv",
                                "column_coverage.parquet", "column_coverage.csv")


def _store_complete(out_dir: Path) -> bool:
    return all((Path(out_dir) / name).exists() for name in STORE_FILES)


def _pid_alive(pid: int) -> bool:
    import psutil
    return psutil.pid_exists(int(pid))


def _dir_pid(path: Path) -> Optional[int]:
    tail = path.name.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _meta_request(built: Path) -> Optional[Dict[str, object]]:
    """The ``ensure.request`` of a store directory's meta, or None (no meta / no key / unreadable)."""
    path = Path(built) / "features.parquet.meta.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta["ensure"]["request"] if "ensure" in meta and "request" in meta["ensure"] else None


def _sweep_leftovers(out_dir: Path, request: Optional[Dict[str, object]] = None) -> Optional[Path]:
    """At the start of a build, clean up what earlier runs left next to ``out_dir`` and return a build to recover, if any.

    * a ``<out>.building-*`` / ``<out>.old-*`` directory whose pid is ALIVE and not ours belongs to another process that may be
      building or swapping right now: untouched. Only leftovers of dead processes (and of our own earlier attempts) are handled;
    * a complete ``<out>.old-*`` while ``out_dir`` is missing is a crash between the two renames: restored;
    * a complete ``<out>.building-*`` whose meta ``ensure.request`` equals ``request`` is a finished build whose swap failed:
      returned for the caller to swap in instead of recomputing (the newest one);
    * every other leftover is removed."""
    import os
    import shutil
    out_dir = Path(out_dir)
    if not out_dir.parent.exists():
        return None

    def mine_to_handle(d: Path) -> bool:
        pid = _dir_pid(d)
        return pid is None or pid == os.getpid() or not _pid_alive(pid)
    olds = sorted((d for d in out_dir.parent.glob(f"{out_dir.name}.old-*") if mine_to_handle(d)),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    builds = sorted((d for d in out_dir.parent.glob(f"{out_dir.name}.building-*") if mine_to_handle(d)),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not out_dir.exists():
        for old in olds:
            if _store_complete(old):
                os.replace(old, out_dir)
                olds.remove(old)
                break
    recover: Optional[Path] = None
    for d in builds:
        if recover is None and request is not None and _store_complete(d) and _meta_request(d) == request:
            recover = d
        else:
            shutil.rmtree(d, ignore_errors=True)
    for d in olds:
        shutil.rmtree(d, ignore_errors=True)
    return recover


def _swap_in(built: Path, out_dir: Path) -> None:
    """Replace ``out_dir`` by the fully written ``built`` directory: a reader sees the old store or the new one, never half of
    each (a crash between the two renames leaves no ``features.parquet.meta.json`` at ``out_dir``, i.e. "not built")."""
    import os
    import shutil
    out_dir = Path(out_dir)
    if out_dir.exists():
        old = out_dir.with_name(f"{out_dir.name}.old-{os.getpid()}")
        shutil.rmtree(old, ignore_errors=True)
        os.replace(out_dir, old)
        try:
            os.replace(built, out_dir)
        except OSError:
            os.replace(old, out_dir)
            raise
        shutil.rmtree(old, ignore_errors=True)
    else:
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(built, out_dir)


def _input_fingerprints(load: Sequence[str], rows: Sequence[str], groups: Sequence[str], cache_root: Path,
                        store_path: Path, uses_store: bool = False) -> Dict[str, object]:
    """Cheap (size, mtime) fingerprints of every source file the build reads: what ``ensure_feature_store`` compares to
    decide whether the stored frame is still the one these inputs give. A missing file is None (not an error here)."""
    fp: Dict[str, object] = {"daily": {s: _fp_or_none(Path(cache_root) / "AlpacaHistoryOHLCVProvider" / f"{s}_1d.parquet")
                                       for s in load}}
    if "mac" in groups:
        fp["fred"] = {sid: _fp_or_none(Path(fs.CACHE_FOLDER) / "fred" / f"{sid}.json") for sid in MAC_SERIES}
    kinds = [k for g in groups for k in _FMP_KINDS_BY_GROUP.get(g, ())]
    if kinds:
        fp["fmp_history"] = {f"{k}__{s}": _fp_or_none(fmp_history_path(k, s, cache_root)) for s in rows for k in dict.fromkeys(kinds)}
    if uses_store or any(g in groups for g in ("x", "f", "i")):
        fp["store"] = ml_universe.store_fingerprint(store_path)
    if "s" in groups:
        fp["market_conditions"] = {p: list(d) for p, d in S_MANIFESTS.items()}
    return fp


def ensure_feature_store(out_dir: Path, symbols: Sequence[str], first: date, last: date, groups: Sequence[str],
                         membership: Optional[pd.DataFrame] = None, *, cache_root: Optional[Path] = None,
                         chunk_size: int = 25, sector_of: Union[None, str, Dict[str, str]] = "store",
                         sector_overrides: Optional[Dict[str, str]] = None, universe: Optional[Sequence[str]] = None,
                         store_dir: Optional[Path] = None, min_share: float = 0.7, force: bool = False,
                         holdout: bool = False) -> Dict[str, object]:
    """Build (or find up to date) the feature store of ``symbols`` over ``[first, last]`` in ``out_dir``.

    * the HOLDOUT guard: the stored symbols, the ``universe`` and the membership columns must all be unlocked
      (``ml_holdout.assert_unlocked``, ``LockedSymbolError`` otherwise) unless ``holdout=True`` is given, which is
      recorded as ``meta["purpose"] = "holdout"`` (a dev store says ``"dev"``) and is part of the rebuild key. The grid
      refuses a store whose purpose is holdout or that lists a locked symbol (``ml_grid.check_dev_store``);
    * pre-screens every symbol ONCE with ``usable_symbols`` (rows with ``groups``, membership-only names on their bars) and
      filters the membership columns by the usable list once. ``meta["ensure"]["refused"]`` lists the permanent refusals with
      reasons; ``meta["ensure"]["transient"]`` is a SEPARATE list of the refusals that can clear after a cache refresh (a
      stale download). Both are INFORMATIONAL: they are written for the reader and NEVER read back - every call screens
      again, so a transient name simply becomes usable (and triggers a rebuild) once its data is complete;
    * with group M, a symbol without a sector (not in the metric store, no override) is refused as unusable;
    * loads every bar once into a ``BuildCache`` and builds in chunks of ``chunk_size`` symbols over the whole universe, so
      cross-sectional values do not depend on the chunking;
    * writes ``features.parquet`` + ``features.parquet.meta.json`` (``save_features``), ``coverage_report.parquet`` / ``.csv``
      and ``meta["ensure"]`` (refused, inputs, timings, ``min_share``). Apply the mask with ``load_feature_store``.

    Rebuilds only when the inputs signature changes: the feature code (``code_version``), the groups and their coverage
    columns, the built symbols, the window, the membership digest, the resolved sectors, or the (size, mtime) of any source file. Returns ``{"status": "built" | "up_to_date", ...}``."""
    import shutil
    import time
    from ba2_common.config import CACHE_FOLDER

    started = time.time()
    out_dir = Path(out_dir)
    groups = list(groups)
    for g in groups:
        _spec(g)
    if not isinstance(holdout, bool):
        raise ValueError(f"holdout must be True or False, got {holdout!r}")
    purpose = "holdout" if holdout else "dev"
    if not holdout:
        ml_holdout.assert_unlocked(
            list(dict.fromkeys(list(symbols) + list(universe or []) + (list(membership.columns) if membership is not None else []))),
            "a dev feature store (pass holdout=True / --holdout to build the holdout store on purpose)")
    cache_root = Path(CACHE_FOLDER if cache_root is None else cache_root)
    store_path = Path(store_dir) if store_dir is not None else ml_universe.store_dir_for(cache_root)
    symbols = list(dict.fromkeys(symbols))
    extra = [c for c in dict.fromkeys((list(membership.columns) if membership is not None else []) + list(universe or []))
             if c not in symbols]
    uses_sector = any(_spec(g).needs_sector for g in groups)
    request_load = list(dict.fromkeys(symbols + extra + ["SPY"] + (sorted(set(MARKET_ETFS) | set(SECTOR_ETF.values()))
                                                                    if uses_sector else [])))
    meta_path = Path(str(out_dir / "features.parquet") + ".meta.json")
    request = json.loads(json.dumps({
        "version": ENSURE_VERSION, "code": code_version(), "packages": _package_versions(), "min_share": min_share,
        "purpose": purpose, "groups": groups, "coverage_columns": {g: _spec(g).coverage_columns for g in groups}, "symbols": symbols,
        "extra": extra, "universe": list(universe) if universe is not None else None, "first": str(first), "last": str(last),
        "membership": None if membership is None else _membership_digest(membership),
        "sector_of": sector_of if (sector_of is None or isinstance(sector_of, str)) else dict(sector_of),
        "sector_overrides": dict(sector_overrides or {}), "store_dir": str(store_path),
        "fingerprints": _input_fingerprints(request_load, symbols, groups, cache_root, store_path,
                                            uses_store=uses_sector and isinstance(sector_of, str))}, sort_keys=True))
    kept = _sweep_leftovers(out_dir, request)                  # may restore a lost store; returns a finished build to recover
    if not force and meta_path.exists() and _store_complete(out_dir):
        old = json.loads(meta_path.read_text(encoding="utf-8"))
        if "ensure" in old and "request" in old["ensure"] and old["ensure"]["request"] == request:   # unchanged: nothing is loaded
            if kept is not None:
                shutil.rmtree(kept, ignore_errors=True)       # identical to the store in place: redundant
            return {"status": "up_to_date", "out_dir": str(out_dir), "symbols": old["symbols"],
                    "refused": old["ensure"]["refused"], "transient": old["ensure"]["transient"]}
    if kept is not None and not force:                         # the same request was built and only its swap failed
        _swap_in(kept, out_dir)
        done = json.loads(meta_path.read_text(encoding="utf-8"))
        return {"status": "built", "recovered": str(kept), "out_dir": str(out_dir), "symbols": done["symbols"],
                "chunks": done["ensure"]["chunks"], "rows": done["rows"], "seconds": done["ensure"]["build_seconds"],
                "refused": done["ensure"]["refused"], "transient": done["ensure"]["transient"],
                "coverage": pd.read_parquet(out_dir / "coverage_report.parquet")}
    usable_rows, refused, transient = screen_symbols(symbols, first, last, cache_root, groups, store_path)
    usable_extra, refused_extra, transient_extra = (screen_symbols(extra, first, last, cache_root, (), store_path)
                                                    if extra else ([], {}, {}))
    refused.update(refused_extra)
    transient.update(transient_extra)
    resolved: Optional[Dict[str, str]] = None
    if any(_spec(g).needs_sector for g in groups):
        if isinstance(sector_of, str):
            resolved = dict(ml_universe.sector_of_from_store(usable_rows, store_path))
        elif sector_of is not None:
            resolved = dict(sector_of)
        resolved = {**(resolved or {}), **(sector_overrides or {})}
        for sym in list(usable_rows):
            if sym not in resolved:
                refused[sym] = f"{sym}: no sector (not in the metric store); add it to sector_overrides (NO_SECTOR for an ETF)"
                usable_rows.remove(sym)
    if not usable_rows:
        raise ValueError(f"no usable symbol among {symbols}: {refused}")
    members = None
    if membership is not None:
        keep = [c for c in membership.columns if c in set(usable_rows) | set(usable_extra)]
        members = membership[keep]
        members.attrs = dict(membership.attrs)
    uni = list(dict.fromkeys(usable_rows + [c for c in (universe or []) if c in set(usable_extra) | set(usable_rows)]
                             + (list(members.columns) if members is not None else [])))
    required: Set[str] = {"SPY"}
    for g in groups:
        required |= set(_spec(g).required_context(usable_rows, resolved))
    load = list(dict.fromkeys(usable_rows + uni + sorted(required)))
    inputs = json.loads(json.dumps({
        "version": ENSURE_VERSION, "code": code_version(), "packages": _package_versions(), "min_share": min_share,
        "purpose": purpose, "groups": groups,
        "coverage_columns": {g: _spec(g).coverage_columns for g in groups}, "symbols": usable_rows, "universe": uni, "first": str(first),
        "last": str(last), "membership": None if members is None else _membership_digest(members), "sectors": resolved,
        "fingerprints": _input_fingerprints(load, usable_rows, groups, cache_root, store_path)}, sort_keys=True))
    if not force and meta_path.exists() and _store_complete(out_dir):
        old = json.loads(meta_path.read_text(encoding="utf-8"))
        if "ensure" in old and "request" in old["ensure"] and old["ensure"]["inputs"] == inputs:
            return {"status": "up_to_date", "out_dir": str(out_dir), "symbols": usable_rows, "refused": refused,
                    "transient": transient}
    cache = BuildCache()
    earliest = min(first_cached_bar(s, cache_root) for s in load)
    cache_end = last_cached_bar("SPY", cache_root)
    cache.load(load, min(first, earliest.date()), last, cache_root, cache_end)
    frames: List[pd.DataFrame] = []
    metas: List[Dict[str, object]] = []
    for i in range(0, len(usable_rows), chunk_size):
        chunk = usable_rows[i:i + chunk_size]
        part = build_features(chunk, first, last, groups, cache_root, (), sector_of=resolved if resolved is not None else None,
                              universe=uni, store_dir=store_path, membership=members, preloaded=cache)
        frames.append(part)
        metas.append(part.attrs["meta"])
    df = pd.concat(frames)
    meta = dict(metas[0])
    meta["symbols"], meta["universe"] = usable_rows, uni
    meta["purpose"] = purpose
    meta["rows"], meta["columns"], meta["nan_rate"] = int(len(df)), list(df.columns), _nan_rates(df, groups)
    meta["ended"] = {k: v for m in metas for k, v in m["ended"].items()}
    meta["sectorless"] = sorted({sym for m in metas for sym in m["sectorless"]})
    meta["sector_overrides"] = dict(sector_overrides or {})
    meta["required_context"] = sorted({c for m in metas for c in m["required_context"]})
    meta["context_symbols"] = sorted({c for m in metas for c in m["context_symbols"]} - set(usable_rows))
    meta["load"] = {k: {s: v for m in metas for s, v in m["load"][k].items()} for k in metas[0]["load"]}
    source: Dict[str, object] = {"daily": {}, "fmp_history": {}}
    for m in metas:
        source["daily"].update(m["source"]["daily"])
        source["fmp_history"].update(m["source"].get("fmp_history", {}))
        for k, v in m["source"].items():
            if k in ("daily", "fmp_history"):
                continue
            if k == "market_conditions":
                cur = source.setdefault(k, {p: {**r, "uncovered": []} for p, r in v.items()})
                for p, r in v.items():
                    cur[p]["uncovered"] = sorted(set(cur[p]["uncovered"]) | set(r["uncovered"]))
            else:
                source.setdefault(k, v)
    if not source["fmp_history"]:
        del source["fmp_history"]
    meta["source"] = source
    covered_groups = [g for g in groups if _coverage_has_columns(df, g)]
    report = coverage_report(df, covered_groups, sectorless=meta["sectorless"])
    columns = column_coverage(df, covered_groups)
    meta["ensure"] = {"version": ENSURE_VERSION, "inputs": inputs, "request": request, "chunk_size": chunk_size, "chunks": len(frames),
                      "refused": refused, "transient": transient, "min_share": min_share,
                      "build_seconds": round(time.time() - started, 1),
                      "note": "features.parquet is UNMASKED; apply_coverage_mask(df, coverage_report, min_share) or "
                              "load_feature_store(mask=True) masks thin (group, year) pairs"}
    df.attrs["meta"] = meta
    import os
    import shutil
    building = out_dir.with_name(f"{out_dir.name}.building-{os.getpid()}")      # written whole, then swapped in
    shutil.rmtree(building, ignore_errors=True)
    building.mkdir(parents=True)
    try:                                                             # an incomplete write is useless: removed
        save_features(df, building / "features.parquet")
        report.to_parquet(building / "coverage_report.parquet")
        report.to_csv(building / "coverage_report.csv")
        columns.to_parquet(building / "column_coverage.parquet")    # every column x year, so excluded columns' holes stay visible
        columns.to_csv(building / "column_coverage.csv")
        missing = [n for n in STORE_FILES if not (building / n).exists()]
        if missing:
            raise RuntimeError(f"the store build did not write {missing}")
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    try:
        _swap_in(building, out_dir)
    except OSError as e:                                            # the finished build is NOT thrown away
        raise OSError(f"{e}; the finished build is kept at {building} (the previous store is untouched)") from e
    return {"status": "built", "out_dir": str(out_dir), "symbols": usable_rows, "chunks": len(frames),
            "rows": int(len(df)), "seconds": meta["ensure"]["build_seconds"], "refused": meta["ensure"]["refused"],
            "transient": meta["ensure"]["transient"], "coverage": report}


def _coverage_has_columns(df: pd.DataFrame, group: str) -> bool:
    return bool(_group_columns(df, group))


def load_feature_store(out_dir: Path, mask: bool = True, min_share: Optional[float] = None) -> pd.DataFrame:
    """Read a store written by ``ensure_feature_store``; with ``mask`` apply its coverage report at ``min_share`` (default: the
    one the store was built with, 0.7). ``df.attrs["meta"]`` is the stored meta."""
    out_dir = Path(out_dir)
    df = pd.read_parquet(out_dir / "features.parquet")
    meta = json.loads(Path(str(out_dir / "features.parquet") + ".meta.json").read_text(encoding="utf-8"))
    df.attrs["meta"] = meta
    if mask:
        share = meta["ensure"]["min_share"] if min_share is None else min_share
        df = apply_coverage_mask(df, pd.read_parquet(out_dir / "coverage_report.parquet"), share)
        df.attrs["meta"] = meta
    return df


def save_features(df: pd.DataFrame, path: Path) -> None:
    """Write ``df`` to ``path`` (parquet) and its meta to the sidecar ``<path>.meta.json``. The meta is what the
    frame was built from (groups and columns, universe, load fills, source fingerprints); a frame without it, i.e. not
    straight from ``build_features``, is refused."""
    if "meta" not in df.attrs:
        raise ValueError("frame has no meta (df.attrs['meta']); build it with build_features before saving")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    Path(str(path) + ".meta.json").write_text(json.dumps(df.attrs["meta"], indent=2, sort_keys=True), encoding="utf-8")
