"""Point-in-time DAILY feature store for the ML exploration v2 (trains on (symbol, session) rows, 2016-2026).

Source: the Alpaca-built daily cache ``<cache>/AlpacaHistoryOHLCVProvider/<SYM>_1d.parquet`` only (no network, never
FMP prices). The decision clock is the session CLOSE: a row for session S is what a model knows when it decides at S's
close (the order then fills at S+1's open). So a row may use

  * the symbol's own bars up to and INCLUDING S (a daily bar is public at its close);
  * other symbols' bars up to and including S (same clock);
  * a weekly statistic only once its week has ended, i.e. from the next week's first session on.

Nothing here may read a bar dated after S. The proof is the CUT TEST (tests/test_ml_features.py): build from a cache
truncated after day D and every common row, every column, is identical.

Feature groups are column prefixes (``GROUPS``: ``t_`` technical, ``mtf_`` multi-timeframe, ``m_`` market) so they
can be switched on and off as grid factors. A builder is ``(daily, ctx) -> DataFrame`` indexed ``[symbol, session]``
holding its columns for ``ctx.symbols`` over the whole loaded window (warm-up included); ``build_features`` runs the
requested builders and trims to ``[first_day, last_day]``. Later groups (cross-sectional, macro, fundamentals, analyst,
insider, structure) register themselves in ``GROUPS`` the same way. Every column is scale-free (log returns, ratios,
ATR multiples, z-scores), never a price level.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from app.services.ml_panel import (PanelDataError, _ema, _true_range, _wilder,  # noqa: F401  (shared, causal helpers)
                                   regular_sessions)

#: A symbol may miss at most this share of the sessions since its listing; each missing bar is filled flat at the
#: previous close with zero volume and LISTED in the load meta. Above it the load refuses (same policy as the
#: intraday panel's ``align_to_reference``).
MAX_MISSING_SESSION_SHARE = 0.001
#: Calendar days loaded before ``first_day``: the 200-session average needs ~290 days, the 40-week average (of the
#: previous, completed week) ~290 days, so 420 leaves every indicator warm on day one.
WARMUP_CALENDAR_DAYS = 420
BAR_COLUMNS: Tuple[str, ...] = ("open", "high", "low", "close", "volume")
_RAW = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}


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
    df = df.sort_values("Date").rename(columns=_RAW).set_index("Date")
    df.index = pd.DatetimeIndex(df.index.astype("datetime64[ns]"), name="session")
    df["volume"] = df["volume"].astype("float64")
    return df


def load_daily(symbols: Iterable[str], first: date, last: date,
               cache_root: Path) -> Tuple[Dict[str, pd.DataFrame], Dict[str, object]]:
    """Daily OHLCV per symbol on the NYSE sessions of ``[first, last]`` (``ml_panel.regular_sessions``).

    Each frame is indexed by ``session`` (naive midnight timestamps) with columns open, high, low, close, volume,
    ``listed`` and ``filled``:

    * before the symbol's first cached bar: OHLCV are NaN and ``listed`` is False (it did not exist yet, so no
      feature may be built there). A cache that starts before ``first`` is listed from the first session;
    * a bar missing AFTER listing is filled flat (OHLC = previous close, volume 0, ``filled`` True) when at most
      ``MAX_MISSING_SESSION_SHARE`` of the sessions since listing are missing, and listed in ``meta["filled"]``;
      above that the load refuses;
    * a bar on a date that is not an NYSE session, a duplicate, a NaN or a timezone-aware Date refuses.

    Returns ``(daily, meta)`` with ``meta = {"filled": {sym: [iso date, ...]}, "first_bar": {sym: iso date}}``
    (``first_bar`` is the symbol's first bar in the cache file, whatever the range)."""
    symbols = list(symbols)
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"duplicate symbols: {sorted({s for s in symbols if symbols.count(s) > 1})}")
    sessions = pd.DatetimeIndex(pd.to_datetime(regular_sessions(first, last)["session"]), name="session")
    daily: Dict[str, pd.DataFrame] = {}
    meta: Dict[str, object] = {"filled": {}, "first_bar": {}}
    for sym in symbols:
        raw = _read_daily(sym, cache_root)
        meta["first_bar"][sym] = str(raw.index[0].date())
        inside = raw[(raw.index >= sessions[0]) & (raw.index <= sessions[-1])]
        off = inside.index.difference(sessions)
        if len(off):
            raise PanelDataError(f"{sym}: {len(off)} daily bars not on the NYSE calendar, first {off[0].date()}")
        d = inside.reindex(sessions)
        listed = sessions >= raw.index[0]
        missing = d["close"].isna().to_numpy() & listed
        n_listed = int(listed.sum())
        if missing.any():
            share = missing.sum() / n_listed
            if share > MAX_MISSING_SESSION_SHARE:
                raise PanelDataError(f"{sym}: {int(missing.sum())} of {n_listed} sessions missing since listing "
                                     f"({share:.3%} > {MAX_MISSING_SESSION_SHARE:.3%}), first {sessions[missing][0].date()}")
            # the last bar before the range seeds a fill that opens the range (a listed-from-the-start symbol)
            prev = d["close"].copy()
            if missing[0]:
                before = raw[raw.index < sessions[0]]
                if before.empty:
                    raise PanelDataError(f"{sym}: the first session of the range is missing and nothing precedes it")
                prev.iloc[0] = before["close"].iloc[-1]
            prev = prev.ffill()
            for c in ("open", "high", "low", "close"):
                d.loc[missing, c] = prev[missing]
            d.loc[missing, "volume"] = 0.0
            meta["filled"][sym] = [str(t.date()) for t in sessions[missing]]
        d["listed"] = listed
        d["filled"] = missing
        daily[sym] = d
    return daily, meta


# ------------------------------------------------------------------------------------------ build context / registry
@dataclass(frozen=True)
class BuildContext:
    """What every group builder needs besides the loaded bars.

    ``symbols`` are the rows being built (in output order); ``context_symbols`` are extra series loaded for them
    (indices, sector ETFs); ``sessions`` is the loaded window INCLUDING the warm-up; ``sector_of`` maps a symbol to its
    sector (group M only)."""
    symbols: Tuple[str, ...]
    context_symbols: Tuple[str, ...]
    sessions: pd.DatetimeIndex
    first_day: date
    last_day: date
    sector_of: Dict[str, str] = field(default_factory=dict)


Builder = Callable[[Dict[str, pd.DataFrame], BuildContext], pd.DataFrame]


def _per_symbol(daily: Dict[str, pd.DataFrame], ctx: BuildContext,
                fn: Callable[[pd.DataFrame], pd.DataFrame]) -> pd.DataFrame:
    """Run ``fn`` on each built symbol's LISTED bars and put the result on the full session grid: rows before the
    listing are NaN (an indicator is never seeded from a symbol that did not exist yet)."""
    frames: Dict[str, pd.DataFrame] = {}
    for sym in ctx.symbols:
        d = daily[sym]
        out = fn(d[d["listed"]]).replace([np.inf, -np.inf], np.nan)
        frames[sym] = out.reindex(ctx.sessions).astype("float64")
    return pd.concat(frames, names=["symbol", "session"])


# ------------------------------------------------------------------------------------------ indicator helpers (causal)
def _rsi_scaled(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI scaled to [-0.5, 0.5]. Unlike ``ml_panel._rsi`` it is defined when there is no down move (100) or
    no move at all (50) instead of NaN."""
    d = close.diff()
    up, dn = _wilder(d.clip(lower=0), n), _wilder((-d).clip(lower=0), n)
    rsi = 100.0 - 100.0 / (1.0 + up / dn)
    rsi = rsi.where(dn != 0, pd.Series(np.where(up > 0, 100.0, 50.0), index=up.index))
    return (rsi / 100.0 - 0.5).where(up.notna() & dn.notna())


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's ADX in 0..100 (Wilder-smoothed +DM / -DM / TR, then Wilder-smoothed DX). ``app.indicators`` smooths
    with a plain rolling mean, which is causal but not Wilder's ADX; this is the textbook one."""
    up, dn = high.diff(), -low.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0).where(up.notna())
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0).where(dn.notna())
    tr = _true_range(high, low, close).where(close.shift(1).notna())
    atr = _wilder(tr, n)
    pdi, mdi = 100.0 * _wilder(plus_dm, n) / atr, 100.0 * _wilder(minus_dm, n) / atr
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi)
    return _wilder(dx, n)


def _parabolic_sar(high: np.ndarray, low: np.ndarray, af_start: float = 0.02, af_max: float = 0.2) -> np.ndarray:
    """Standard parabolic SAR, a numpy port of ``TechnicalIndicators.calculate_sar`` (same recursion, checked equal in
    tests). Causal: SAR[i] is a function of bars 0..i. Its value depends on where the series starts until the first
    reversal, as for every recursive indicator, hence the 420-day warm-up."""
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


# ------------------------------------------------------------------------------------------ group T (daily technical)
T_COLUMNS: Tuple[str, ...] = (
    "t_ret_1", "t_ret_5", "t_ret_20", "t_ret_60", "t_rvol_20", "t_rvol_60", "t_atr_pct", "t_rsi", "t_macd_hist",
    "t_bb_pctb", "t_bb_width", "t_dist_sma20", "t_dist_sma50", "t_dist_sma200", "t_adx", "t_sar_dist",
    "t_donch_20", "t_donch_55", "t_obv_slope", "t_pivot_dist", "t_vol_z")


def _technical_one(d: pd.DataFrame) -> pd.DataFrame:
    """Daily technical features of one symbol, row S from bars up to and including S only (every operation below is a
    trailing window, an exponential recursion, or a ONE-session lag; nothing looks forward)."""
    h, l, c, v = d["high"], d["low"], d["close"], d["volume"]
    lr = np.log(c)
    r1 = lr.diff()
    atr = _wilder(_true_range(h, l, c), 14)
    out = pd.DataFrame(index=d.index)
    for n in (1, 5, 20, 60):
        out[f"t_ret_{n}"] = lr.diff(n)
    out["t_rvol_20"], out["t_rvol_60"] = _roll(r1, 20).std(), _roll(r1, 60).std()
    out["t_atr_pct"] = atr / c
    out["t_rsi"] = _rsi_scaled(c)
    macd = _ema(c, 12) - _ema(c, 26)
    out["t_macd_hist"] = (macd - _ema(macd, 9)) / atr
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
    obv = (np.sign(c.diff()).fillna(0.0) * v).cumsum()
    out["t_obv_slope"] = _rolling_slope(obv, 20) / _roll(v, 20).mean()
    pivot_prev = ((h + l + c) / 3.0).shift(1)               # the PREVIOUS session's classic pivot
    out["t_pivot_dist"] = (c - pivot_prev) / atr
    lv = np.log1p(v)                                         # log1p: a filled (volume 0) session stays finite
    out["t_vol_z"] = (lv - _roll(lv, 60).mean()) / _roll(lv, 60).std()
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
    watr = _wilder(_true_range(w["high"], w["low"], c), 14)
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
#: broad / cross-asset context series every M build needs in ``context_symbols`` (equity, small caps, rates, gold)
MARKET_ETFS: Tuple[str, ...] = ("SPY", "QQQ", "IWM", "TLT", "GLD")
_M_RET_DAYS: Tuple[int, ...] = (1, 5, 20)
_M_RS_DAYS: Tuple[int, ...] = (5, 20)
M_COLUMNS: Tuple[str, ...] = (
    tuple(f"m_{e.lower()}_ret_{n}" for e in MARKET_ETFS for n in _M_RET_DAYS)
    + tuple(f"m_rs_spy_{n}" for n in _M_RS_DAYS) + tuple(f"m_rs_sector_{n}" for n in _M_RS_DAYS)
    + ("m_breadth_sma50",))


def _log_ret(d: pd.DataFrame, n: int) -> pd.Series:
    """``n``-session log return of a loaded frame on the full session grid (NaN before its listing)."""
    return np.log(d["close"]).diff(n)


def _require(daily: Dict[str, pd.DataFrame], sym: str, why: str) -> pd.DataFrame:
    if sym not in daily:
        raise PanelDataError(f"group m needs {sym} ({why}) among the context symbols")
    return daily[sym]


def market(daily: Dict[str, pd.DataFrame], ctx: BuildContext) -> pd.DataFrame:
    """Group ``m_``: the market the symbol trades in, as of the same session close.

    * ``m_<etf>_ret_{1,5,20}``: SPY / QQQ / IWM / TLT / GLD log returns (broad equity, tech, small caps, rates, gold);
    * ``m_rs_spy_{5,20}``: the symbol's log return minus SPY's; ``m_rs_sector_{5,20}``: minus its sector ETF's;
    * ``m_breadth_sma50``: the share of the BUILD'S symbol set (``ctx.symbols``, not the context ETFs) whose close is
      above its own 50-session SMA on that session. Symbols not yet listed, or without 50 bars, are not counted. The
      value is the same for every symbol on a given session, and it depends on the symbol set built: a different
      universe is a different breadth.

    Every input is a bar of session S or earlier, so a row is fixed once S has closed. ``ctx.sector_of`` must give each
    built symbol a sector in ``SECTOR_ETF``; anything else raises."""
    if not ctx.sector_of:
        raise PanelDataError("group m needs sector_of (symbol -> sector); got none")
    etf_of: Dict[str, str] = {}
    for sym in ctx.symbols:
        sector = ctx.sector_of[sym] if sym in ctx.sector_of else None
        if sector is None:
            raise PanelDataError(f"group m: no sector for {sym}")
        if sector not in SECTOR_ETF:
            raise PanelDataError(f"group m: unknown sector {sector!r} for {sym}; known {sorted(SECTOR_ETF)}")
        etf_of[sym] = SECTOR_ETF[sector]
    for e in MARKET_ETFS:
        _require(daily, e, "market series")
    for e in sorted(set(etf_of.values())):
        _require(daily, e, "sector ETF")
    mk = pd.DataFrame(index=ctx.sessions)
    for e in MARKET_ETFS:
        for n in _M_RET_DAYS:
            mk[f"m_{e.lower()}_ret_{n}"] = _log_ret(daily[e], n)
    closes = pd.DataFrame({s: daily[s]["close"] for s in ctx.symbols})
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
    return pd.concat(frames, names=["symbol", "session"]).astype("float64")


#: group name -> builder. The name is also the column prefix (``<name>_``); later tasks register more groups here.
GROUPS: Dict[str, Builder] = {"t": technical, "mtf": multi_timeframe, "m": market}


def group_prefix(group: str) -> str:
    return f"{group}_"


def feature_columns(df: pd.DataFrame, groups: Optional[Iterable[str]] = None) -> List[str]:
    """The feature columns of a built frame (prefix of a registered group), in frame order; ``listed`` and any other
    unprefixed column is not a feature."""
    prefixes = tuple(group_prefix(g) for g in (GROUPS if groups is None else groups))
    return [c for c in df.columns if c.startswith(prefixes)]


# ------------------------------------------------------------------------------------------ the store
def build_features(symbols: Sequence[str], first_day: date, last_day: date, groups: Sequence[str],
                   cache_root: Path, context_symbols: Sequence[str],
                   sector_of: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Point-in-time daily features indexed ``[symbol, session]`` for ``symbols`` over ``[first_day, last_day]``.

    Bars are loaded from ``WARMUP_CALENDAR_DAYS`` before ``first_day`` (so a 200-day and a 40-week statistic are warm
    on day one), the requested ``groups`` are built on the whole window and the result is trimmed to the range. The
    frame has a ``listed`` column (False before a symbol's first bar; its features are NaN there) followed by the
    feature columns of each group in the order given. NaN appears only for pre-listing rows and for a symbol's own
    warm-up. ``df.attrs["meta"]`` carries the load meta (filled bars, first bars) and the columns per group.

    Raises PanelDataError on missing / inconsistent cached data (see ``load_daily``), ValueError on bad arguments."""
    symbols, groups, context_symbols = list(symbols), list(groups), list(context_symbols)
    if not symbols:
        raise ValueError("no symbols")
    if len(set(symbols)) != len(symbols):
        raise ValueError("duplicate symbols")
    if not groups or len(set(groups)) != len(groups):
        raise ValueError(f"groups must be a non-empty list without duplicates, got {groups}")
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        raise ValueError(f"unknown feature groups {unknown}; known {sorted(GROUPS)}")
    if first_day > last_day:
        raise ValueError(f"first_day {first_day} is after last_day {last_day}")
    load = symbols + [s for s in dict.fromkeys(context_symbols) if s not in symbols]
    daily, load_meta = load_daily(load, first_day - timedelta(days=WARMUP_CALENDAR_DAYS), last_day, cache_root)
    sessions = next(iter(daily.values())).index
    ctx = BuildContext(tuple(symbols), tuple(context_symbols), sessions, first_day, last_day,
                       dict(sector_of) if sector_of is not None else {})
    parts: List[pd.DataFrame] = []
    per_group: Dict[str, List[str]] = {}
    for g in groups:
        cols = GROUPS[g](daily, ctx)
        bad = [c for c in cols.columns if not c.startswith(group_prefix(g))]
        if bad:
            raise ValueError(f"group {g!r} produced columns without its prefix {group_prefix(g)!r}: {bad}")
        parts.append(cols)
        per_group[g] = list(cols.columns)
    listed = pd.concat({s: daily[s]["listed"] for s in symbols}, names=["symbol", "session"]).rename("listed")
    out = pd.concat([listed] + parts, axis=1)
    dup = out.columns[out.columns.duplicated()].tolist()
    if dup:
        raise ValueError(f"columns produced by more than one group: {dup}")
    feats = feature_columns(out, groups)
    out.loc[~out["listed"].to_numpy(), feats] = np.nan         # safety net: no feature exists before the listing
    day = out.index.get_level_values("session")
    out = out[(day >= pd.Timestamp(first_day)) & (day <= pd.Timestamp(last_day))]
    out.attrs["meta"] = {"first_day": str(first_day), "last_day": str(last_day), "groups": per_group,
                         "load": load_meta, "symbols": symbols, "context_symbols": context_symbols}
    return out
