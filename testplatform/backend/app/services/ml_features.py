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
