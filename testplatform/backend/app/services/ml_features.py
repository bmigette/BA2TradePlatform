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

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

import numpy as np
import pandas as pd

from app.services import ml_universe
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


def _sessions_index(first: date, last: date) -> pd.DatetimeIndex:
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
                raise PanelDataError(f"{sym}: stale series, last cached bar {have_ts.date()} but {len(gap)} sessions "
                                     f"up to {sessions[-1].date()} have none")
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
    read its own stores; ``store_dir`` is the screener metric store (groups X and F)."""
    symbols: Tuple[str, ...]
    universe: Tuple[str, ...]
    context_symbols: Tuple[str, ...]
    sessions: pd.DatetimeIndex
    first_day: date
    last_day: date
    cache_root: Path
    store_dir: Path
    sector_of: Dict[str, str] = field(default_factory=dict)


Builder = Callable[[Dict[str, pd.DataFrame], BuildContext], pd.DataFrame]
ContextFn = Callable[[Sequence[str], Optional[Dict[str, str]]], Set[str]]


def _no_context(symbols: Sequence[str], sector_of: Optional[Dict[str, str]]) -> Set[str]:
    return set()


@dataclass(frozen=True)
class GroupSpec:
    """A feature group: its ``builder``, its column ``prefix`` (``"t_"``), whether it needs ``sector_of``, and
    ``required_context(symbols, sector_of)``: the extra symbols whose bars the builder reads."""
    builder: Builder
    prefix: str
    needs_sector: bool = False
    required_context: ContextFn = _no_context


def _per_symbol(daily: Dict[str, pd.DataFrame], ctx: BuildContext,
                fn: Callable[[pd.DataFrame], pd.DataFrame]) -> pd.DataFrame:
    """Run ``fn`` on each built symbol's LISTED bars and put the result on the full session grid: rows before the
    listing are NaN (an indicator is never seeded from a symbol that did not exist yet)."""
    frames: Dict[str, pd.DataFrame] = {}
    for sym in ctx.symbols:
        d = daily[sym]
        frames[sym] = fn(d[d["listed"]]).reindex(ctx.sessions).astype("float64")
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
    return _per_symbol(daily, ctx, _technical_one)


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
    return _per_symbol(daily, ctx, _weekly_one)


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


def _sector_etfs(symbols: Sequence[str], sector_of: Optional[Dict[str, str]]) -> Dict[str, str]:
    """symbol -> its sector ETF. Refuses a missing map, a symbol without a sector and a sector outside SECTOR_ETF."""
    if not sector_of:
        raise PanelDataError("group m needs sector_of (symbol -> sector); got none")
    etf_of: Dict[str, str] = {}
    for sym in symbols:
        sector = sector_of[sym] if sym in sector_of else None
        if sector is None:
            raise PanelDataError(f"group m: no sector for {sym}")
        if sector not in SECTOR_ETF:
            raise PanelDataError(f"group m: unknown sector {sector!r} for {sym}; known {sorted(SECTOR_ETF)}")
        etf_of[sym] = SECTOR_ETF[sector]
    return etf_of


def _market_context(symbols: Sequence[str], sector_of: Optional[Dict[str, str]]) -> Set[str]:
    return set(MARKET_ETFS) | set(_sector_etfs(symbols, sector_of).values())


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
    * ``m_breadth_sma50``: the share of ``ctx.universe`` (not the context ETFs) whose close is above its own
      50-session SMA on that session. Symbols not yet listed, or without 50 bars, are not counted. The value is the
      same for every symbol on a given session; it is a property of the universe, so a build in chunks over the same
      universe gives the same breadth as one build.

    Every input is a bar of session S or earlier, so a row is fixed once S has closed. Each built symbol needs a
    sector in ``SECTOR_ETF``; anything else raises."""
    etf_of = _sector_etfs(ctx.symbols, ctx.sector_of)
    for e in MARKET_ETFS:
        _require(daily, e, "market series")
    for e in sorted(set(etf_of.values())):
        _require(daily, e, "sector ETF")
    mk = pd.DataFrame(index=ctx.sessions)
    for e in MARKET_ETFS:
        for n in _M_RET_DAYS:
            mk[f"m_{e.lower()}_ret_{n}"] = _log_ret(daily[e], n)
    closes = pd.DataFrame({s: daily[s]["close"] for s in ctx.universe})
    sma50 = closes.rolling(50, min_periods=50).mean()
    valid = sma50.notna()
    mk["m_breadth_sma50"] = ((closes > sma50) & valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan)
    frames: Dict[str, pd.DataFrame] = {}
    for sym in ctx.symbols:
        f = mk.copy()
        for n in _M_RS_DAYS:
            own = _log_ret(daily[sym], n)
            f[f"m_rs_spy_{n}"] = own - _log_ret(daily["SPY"], n)
            f[f"m_rs_sector_{n}"] = own - _log_ret(daily[etf_of[sym]], n)
        f = f[list(M_COLUMNS)]
        f.loc[~daily[sym]["listed"].to_numpy(), :] = np.nan                 # no row before the symbol exists
        frames[sym] = f
    return pd.concat(frames, names=_INDEX_NAMES).astype("float64")


# ------------------------------------------------------------------------------------------ group X (cross-sectional)
#: sector -> integer code of group X (committed: the code of a sector never changes)
SECTOR_CODE: Dict[str, int] = {
    "Basic Materials": 1, "Communication Services": 2, "Consumer Cyclical": 3, "Consumer Defensive": 4, "Energy": 5,
    "Financial Services": 6, "Healthcare": 7, "Industrials": 8, "Real Estate": 9, "Technology": 10, "Utilities": 11}
#: T features ranked / z-scored across the universe on each session
X_RANKED: Tuple[str, ...] = ("t_ret_5", "t_ret_20", "t_ret_60", "t_rvol_20", "t_dist_sma50", "t_rsi", "t_vol_z")
X_STORE_COLUMNS: Tuple[str, ...] = ("x_mom_12_1", "x_mom_12_1_rk", "x_weinstein", "x_relvol", "x_sector")
X_COLUMNS: Tuple[str, ...] = (tuple(f"x_rk_{c[2:]}" for c in X_RANKED) + tuple(f"x_z_{c[2:]}" for c in X_RANKED)
                              + X_STORE_COLUMNS)
#: a cross-section needs at least this many members with a value, else its ranks and z-scores are NaN
MIN_CROSS_SECTION = 2


def _cross_rank(wide: pd.DataFrame) -> pd.DataFrame:
    """Per row (session) the midpoint rank (rank - 0.5) / n in (0, 1) among the members with a value (ties averaged)."""
    n = wide.notna().sum(axis=1)
    return (wide.rank(axis=1, method="average") - 0.5).div(n, axis=0).where(n >= MIN_CROSS_SECTION)


def _cross_z(wide: pd.DataFrame) -> pd.DataFrame:
    """Per row the z-score (sample std) among the members with a value."""
    n = wide.notna().sum(axis=1)
    return wide.sub(wide.mean(axis=1), axis=0).div(wide.std(axis=1, ddof=1), axis=0).where(n >= MIN_CROSS_SECTION)


def cross_section(daily: Dict[str, pd.DataFrame], ctx: BuildContext) -> pd.DataFrame:
    """Group ``x_``: where the symbol stands among ``ctx.universe`` on each session.

    * ``x_rk_*`` / ``x_z_*``: rank in (0, 1) and z-score, across the universe's rows OF THAT SESSION (listed members
      with a value; an unlisted or ended member is not in the cross-section), of ``X_RANKED`` (the T features, computed
      here for every universe member from bars up to S);
    * from the metric store, in the newest scan visible at S (``ml_universe.scan_values``; NaN before the first scan,
      2020-01): ``x_mom_12_1`` and its universe rank ``x_mom_12_1_rk``, ``x_weinstein`` (stage 1-4), ``x_relvol``
      (relative volume), ``x_sector`` (``SECTOR_CODE``; NaN when the store has no sector for the symbol).

    The universe is explicit, so a build in chunks over the same universe gives the same values as one build. A
    universe that changes over time (``ml_universe.universe_as_of``) is the caller's job: pass the union of names and
    note that membership is then not a function of the session."""
    members = list(ctx.universe)
    wide = {c: {} for c in X_RANKED}
    for sym in members:
        d = daily[sym]
        live = d[d["listed"]]
        if live.empty:
            t = pd.DataFrame(np.nan, index=ctx.sessions, columns=list(X_RANKED))
        else:
            t = _technical_one(live)[list(X_RANKED)].reindex(ctx.sessions)
        for c in X_RANKED:
            wide[c][sym] = t[c]
    parts: Dict[str, pd.DataFrame] = {}
    for c in X_RANKED:
        w = pd.DataFrame(wide[c], columns=members).astype("float64")
        parts[f"x_rk_{c[2:]}"] = _cross_rank(w)
        parts[f"x_z_{c[2:]}"] = _cross_z(w)
    store = ml_universe.scan_values(members, ctx.sessions, ["momentum_12_1", "weinstein_stage", "relative_volume",
                                                            "sector"], ctx.store_dir)
    parts["x_mom_12_1"] = store["momentum_12_1"].astype("float64")
    parts["x_mom_12_1_rk"] = _cross_rank(parts["x_mom_12_1"])
    parts["x_weinstein"] = store["weinstein_stage"].astype("float64")
    parts["x_relvol"] = store["relative_volume"].astype("float64")
    sector = store["sector"]
    unknown = sorted({str(v) for v in sector.to_numpy().ravel() if isinstance(v, str)} - set(SECTOR_CODE))
    if unknown:
        raise PanelDataError(f"group x: sector(s) {unknown} are not in SECTOR_CODE")
    parts["x_sector"] = sector.apply(lambda col: col.map(SECTOR_CODE)).astype("float64")
    frames = {sym: pd.DataFrame({name: parts[name][sym] for name in X_COLUMNS}, index=ctx.sessions)[list(X_COLUMNS)]
              for sym in ctx.symbols}
    return pd.concat(frames, names=_INDEX_NAMES).astype("float64")


#: group name -> spec. The name is the grid factor; the spec's prefix names its columns. Later tasks register here.
GROUPS: Dict[str, GroupSpec] = {
    "t": GroupSpec(technical, "t_"),
    "mtf": GroupSpec(multi_timeframe, "mtf_"),
    "m": GroupSpec(market, "m_", needs_sector=True, required_context=_market_context),
    "x": GroupSpec(cross_section, "x_"),
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


def _fingerprint(p: Path) -> Dict[str, object]:
    st = p.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def build_features(symbols: Sequence[str], first_day: date, last_day: date, groups: Sequence[str],
                   cache_root: Path, context_symbols: Sequence[str] = (),
                   sector_of: Union[None, str, Dict[str, str]] = None,
                   universe: Optional[Sequence[str]] = None, store_dir: Optional[Path] = None,
                   sector_overrides: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Point-in-time daily features indexed ``[symbol, session]`` for ``symbols`` over ``[first_day, last_day]``.

    Every loaded symbol is read from its first cached bar (``first_day`` only trims the output), the requested
    ``groups`` are built on the whole history and the result is trimmed, so a row is the same whatever ``first_day``
    is. ``context_symbols`` are extra series to load; each group's own required context (market ETFs, the sector ETFs
    of ``symbols``) is added automatically. ``universe`` is the symbol set cross-sectional statistics are taken
    over: None means ``symbols``, otherwise a superset of ``symbols`` (build a large universe in chunks by passing
    the whole set every time). ``sector_of`` is a symbol -> sector dict, or ``"store"`` to read the sectors from the
    metric store at ``store_dir`` (default ``<cache_root>/screener/metric_store_alpaca_20261022``); a symbol the store
    has no sector for is simply absent, so a group that needs it refuses unless ``sector_overrides`` supplies it (the
    overrides win over both sources). The cache end (what ``ended`` is measured against) is SPY's last bar, so SPY
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
    universe = list(symbols) if universe is None else list(dict.fromkeys(universe))
    if not set(symbols) <= set(universe):
        raise ValueError(f"universe must contain every built symbol; missing {sorted(set(symbols) - set(universe))}")
    store_path = (Path(store_dir) if store_dir is not None
                  else Path(cache_root) / "screener" / ml_universe.DEFAULT_STORE_NAME)
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
    daily, load_meta = load_daily(load, min(first_day, earliest.date()), last_day, cache_root, cache_end=cache_end)
    sessions = next(iter(daily.values())).index
    ctx = BuildContext(tuple(symbols), tuple(universe), tuple(s for s in load if s not in symbols), sessions,
                       first_day, last_day, Path(cache_root), store_path, dict(sectors) if sectors is not None else {})
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
    out.attrs["meta"] = {
        "first_day": str(first_day), "last_day": str(last_day), "groups": per_group, "symbols": symbols,
        "universe": universe, "context_symbols": [s for s in load if s not in symbols],
        "required_context": sorted(required), "load": load_meta, "rows": int(len(out)),
        "cache_end": str(cache_end), "sector_source": sector_source,
        "sector_overrides": dict(sector_overrides) if sector_overrides else {},
        "store": ({"dir": str(store_path), "newest_scan": ml_universe.scan_store(store_path).dates[-1]}
                  if any(g in groups for g in ("x", "f")) else None),
        "columns": list(out.columns),
        "source": {s: _fingerprint(_cache_file(cache_root, s)) for s in load}}
    return out


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
