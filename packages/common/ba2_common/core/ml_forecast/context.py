"""The forecast context: ONE pure function for the store builder and (later) the live expert.

Both slice the model's input the same way from the same history source, so a backtest row and a live forecast see
the identical context. Pure: numpy and pandas only.

A context for session S is the last ``n`` daily closes whose last bar IS S (Date <= S, never a later bar).
"""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

#: Bump when the slicing or the plausibility rule changes (it is recorded in the store's meta).
CONTEXT_FUNCTION_VERSION = "1"
#: history source name -> directory under the cache root
SOURCE_DIRS = {"alpaca_build": "AlpacaHistoryOHLCVProvider"}
#: Missing sessions between two consecutive bars at or above this make the context implausible (a hole in the
#: history, not a quiet market). Holidays leave at most 1-2 missing weekdays, so the weekday count is enough.
MAX_GAP_SESSIONS = 20

SeriesT = Tuple[np.ndarray, np.ndarray]     # (dates datetime64[D] ascending, closes float64)


def load_daily_series(cache_root, symbol: str, source: str = "alpaca_build") -> SeriesT:
    if source not in SOURCE_DIRS:
        raise ValueError(f"unknown history source {source!r}; known: {sorted(SOURCE_DIRS)}")
    p = Path(cache_root) / SOURCE_DIRS[source] / f"{symbol}_1d.parquet"
    df = pd.read_parquet(p, columns=["Date", "Close"])
    dt = pd.to_datetime(df["Date"])
    if getattr(dt.dt, "tz", None) is not None:
        raise ValueError(f"{p}: Date column must be tz-naive, got {dt.dt.tz}")
    d = dt.to_numpy().astype("datetime64[D]")
    c = df["Close"].to_numpy(dtype=np.float64)
    order = np.argsort(d, kind="stable")
    d, c = d[order], c[order]
    if len(np.unique(d)) != len(d):
        raise ValueError(f"{symbol}: duplicate dates in {p}")
    return d, c


def context_for(dates: np.ndarray, closes: np.ndarray, session: date, n: int,
                max_gap_sessions: int = MAX_GAP_SESSIONS) -> Tuple[Optional[np.ndarray], Optional[str]]:
    """``(context, None)`` or ``(None, reason)`` with reason ``no_bar`` (no bar dated S), ``short`` (fewer than
    ``n`` bars up to S) or ``gap`` (>= ``max_gap_sessions`` missing sessions inside the window)."""
    s64 = np.datetime64(session, "D")
    idx = int(np.searchsorted(dates, s64, side="right"))      # bars [0, idx) have Date <= session
    if idx == 0 or dates[idx - 1] != s64:
        return None, "no_bar"
    if idx < n:
        return None, "short"
    win = dates[idx - n:idx]
    assert win.max() <= s64 and win[-1] == s64, "point-in-time violation"
    if n > 1:
        missing = np.busday_count(win[:-1], win[1:]) - 1
        if int(missing.max()) >= max_gap_sessions:
            return None, "gap"
    return closes[idx - n:idx], None


def history_provenance(cache_root, source: str = "alpaca_build") -> Dict[str, Any]:
    """What the store was built from: the source name, the sha256 of the cache's build manifest (its build id) and
    the context-function version. The live expert must read the SAME source and refuse on a mismatch."""
    if source not in SOURCE_DIRS:
        raise ValueError(f"unknown history source {source!r}; known: {sorted(SOURCE_DIRS)}")
    mf = Path(cache_root) / SOURCE_DIRS[source] / "_meta" / "build_manifest.json"
    return {"history_source": source,
            "build_manifest_sha256": hashlib.sha256(mf.read_bytes()).hexdigest(),
            "context_function_version": CONTEXT_FUNCTION_VERSION}
