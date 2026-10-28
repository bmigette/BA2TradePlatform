"""Point-in-time universe and store-derived columns from the screener metric store (ML exploration v2).

The store (``<cache>/screener/metric_store_alpaca_20261022``) holds weekly scans from 2020-01-01: one row per symbol
and scan date with the close, market cap, volume, relative volume, momentum_12_1, Weinstein stage and sector as of that
scan date's close. It is read through ``ba2_providers.screener.metric_store`` (``load_store`` memoises it per process,
``scan_dates``, ``visible_scan_date``, ``check_scan_freshness``).

THE VISIBILITY RULE (daily clock): a scan dated S is visible on day D iff S <= D (``visible_scan_date(...,
intraday=False)``), i.e. for the decision at D's close; the newest visible scan is used and a later one never is. Between
two scans the older scan is held (the universe and the store columns change weekly). Before the first scan there is
nothing: ``universe_as_of`` raises ``UniverseUnavailable``, the column helpers return NaN. A day more than the store's
staleness limit after its newest scan raises ``MetricStoreStaleError`` (never silently reuses the last scan).
"""
from __future__ import annotations

import os
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from ba2_common.config import CACHE_FOLDER
from ba2_providers.screener import metric_store as ms

from app.services import ml_holdout
from app.services.ml_panel import PanelDataError

DEFAULT_STORE_NAME = "metric_store_alpaca_20261022"
#: a stock must trade above this price at the scan to qualify
MIN_PRICE = 5.0
#: default floor on the MEDIAN dollar volume (price x volume of the scan day) over the last ``DV_SCANS`` visible scans
DEFAULT_MIN_DOLLAR_VOLUME = 20_000_000.0
DV_SCANS = 4
PURPOSES = ("dev", "holdout")


class UniverseUnavailable(PanelDataError):
    """The store cannot answer: no scan on or before the day, or an ambiguous attribute."""


def default_store_dir() -> Path:
    return Path(CACHE_FOLDER) / "screener" / DEFAULT_STORE_NAME


def _key(store_dir: Any) -> str:
    return str(Path(store_dir) if store_dir is not None else default_store_dir())


class ScanStore:
    """The loaded store plus its sorted scan dates. One per store dir per process (see ``scan_store``)."""

    def __init__(self, store_dir: str):
        self.store_dir = store_dir
        self.df = ms.load_store(store_dir)
        self.dates: List[str] = ms.scan_dates(self.df, store_dir)
        if not self.dates:
            raise UniverseUnavailable(f"metric store {store_dir} holds no scans")


@lru_cache(maxsize=None)
def _scan_store(key: str) -> ScanStore:
    return ScanStore(key)


def scan_store(store_dir: Any = None) -> ScanStore:
    """The store at ``store_dir`` (default: the committed Alpaca store), loaded once per process."""
    return _scan_store(_key(store_dir))


def _as_date(day: Any) -> date:
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, pd.Timestamp):
        return day.date()
    if isinstance(day, date):
        return day
    return date.fromisoformat(str(day)[:10])


def visible_scan(store_dir: Any, day: Any) -> Optional[str]:
    """The newest scan date visible at ``day``'s close ('YYYY-MM-DD'), or None before the first scan."""
    st = scan_store(store_dir)
    return ms.visible_scan_date(st.dates, _as_date(day), intraday=False)


def _checked_scan(st: ScanStore, day: date) -> Optional[str]:
    scan = ms.visible_scan_date(st.dates, day, intraday=False)
    if scan is not None:
        ms.check_scan_freshness(day.isoformat(), st.dates[-1], st.dates)
    return scan


def universe_as_of(day: Any, top_n: int, store_dir: Any = None, purpose: str = "dev",
                   min_dollar_volume: float = DEFAULT_MIN_DOLLAR_VOLUME) -> List[str]:
    """The point-in-time large-cap universe at ``day``'s close, largest market cap first.

    * the scan is the newest one visible at ``day`` (never a later one), held until the next scan;
    * the ``top_n`` symbols by ``market_cap`` among those with price > ``MIN_PRICE``, a market cap, and a MEDIAN
      dollar volume (price x volume at the scan, over the last ``DV_SCANS`` visible scans) of at least
      ``min_dollar_volume``; ``ms.EXCLUDED_SYMBOLS`` never qualify;
    * then the split: ``purpose="dev"`` drops the locked holdout symbols (``ml_holdout.is_locked_largecap``),
      ``purpose="holdout"`` keeps only them (used once, at the end). The top ``top_n`` is taken BEFORE the split, so
      dev and holdout partition the same large-cap set.

    Raises ``UniverseUnavailable`` before the first scan, ``MetricStoreStaleError`` after the store ends."""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}, got {purpose!r}")
    st = scan_store(store_dir)
    d = _as_date(day)
    scan = _checked_scan(st, d)
    if scan is None:
        raise UniverseUnavailable(f"no scan on or before {d} (first scan {st.dates[0]})")
    window = st.dates[max(0, st.dates.index(scan) - DV_SCANS + 1): st.dates.index(scan) + 1]
    sub = st.df[st.df["date"].isin(window)]
    sub = sub[["date", "symbol", "price", "volume", "market_cap"]].copy()
    sub["date"] = sub["date"].astype(str)
    sub["symbol"] = sub["symbol"].astype(str)
    sub["dv"] = sub["price"] * sub["volume"]
    median_dv = sub.groupby("symbol")["dv"].median()
    cur = sub[sub["date"] == scan]
    cur = cur[(cur["price"] > MIN_PRICE) & cur["market_cap"].notna() & ~cur["symbol"].isin(ms.EXCLUDED_SYMBOLS)]
    cur = cur[cur["symbol"].map(median_dv) >= float(min_dollar_volume)]
    cur = cur.sort_values(["market_cap", "symbol"], ascending=[False, True], kind="mergesort").head(int(top_n))
    symbols = list(cur["symbol"])
    locked = [ml_holdout.is_locked_largecap(s) for s in symbols]
    want_locked = purpose == "holdout"
    return [s for s, lk in zip(symbols, locked) if lk == want_locked]


def scan_values(symbols: Sequence[str], sessions: pd.DatetimeIndex, columns: Sequence[str],
                store_dir: Any = None) -> Dict[str, pd.DataFrame]:
    """For each column a frame (``sessions`` x ``symbols``) of the values in the scan VISIBLE at each session.

    NaN before the first scan and for a symbol the scan does not hold. A duplicated (scan, symbol) row raises. The
    last session beyond the store's staleness limit raises ``MetricStoreStaleError``."""
    st = scan_store(store_dir)
    symbols = list(symbols)
    sessions = pd.DatetimeIndex(sessions)
    if len(sessions) and ms.visible_scan_date(st.dates, sessions[-1].date(), intraday=False) is not None:
        ms.check_scan_freshness(sessions[-1].date().isoformat(), st.dates[-1], st.dates)
    scans = [ms.visible_scan_date(st.dates, t.date(), intraday=False) for t in sessions]
    sub = st.df[st.df["symbol"].isin(symbols)]
    out: Dict[str, pd.DataFrame] = {}
    for col in columns:
        part = sub[["date", "symbol", col]].copy()
        part["date"] = part["date"].astype(str)
        part["symbol"] = part["symbol"].astype(str)
        if col == "sector":
            part[col] = part[col].astype(object).where(part[col].notna(), None)
        else:
            part[col] = part[col].astype("float64")
        if part.duplicated(["date", "symbol"]).any():
            raise PanelDataError(f"metric store {st.store_dir}: duplicate (date, symbol) rows for {col}")
        wide = part.pivot(index="date", columns="symbol", values=col) if len(part) else \
            pd.DataFrame(index=pd.Index([], name="date"), columns=symbols)
        wide = wide.reindex(columns=symbols)
        picked = wide.reindex([s if s is not None else "" for s in scans])       # "" is not a scan: all NaN
        picked.index = sessions
        out[col] = picked
    return out


def sector_of_from_store(symbols: Iterable[str], store_dir: Any = None) -> Dict[str, str]:
    """symbol -> its sector as the store holds it (FMP profile sector, a classification not a market datum).

    A symbol with no sector in any scan is left out (the caller decides: group M refuses it). Two different sectors
    for one symbol raise ``UniverseUnavailable``. Every symbol of the real store has at most one sector."""
    st = scan_store(store_dir)
    symbols = list(symbols)
    sub = st.df[st.df["symbol"].isin(symbols) & st.df["sector"].notna()][["symbol", "sector"]]
    sub = sub.assign(symbol=sub["symbol"].astype(str), sector=sub["sector"].astype(str)).drop_duplicates()
    found: Dict[str, str] = {}
    for sym, grp in sub.groupby("symbol"):
        uniq = sorted(grp["sector"].unique())
        if len(uniq) != 1:
            raise UniverseUnavailable(f"{sym}: more than one sector in the store {uniq}; refusing to pick")
        found[sym] = uniq[0]
    return found
