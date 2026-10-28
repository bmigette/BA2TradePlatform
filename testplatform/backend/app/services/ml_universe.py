"""Universe and store-derived columns from the screener metric store (ML exploration v2).

POINT-IN-TIME IN TIME, SURVIVOR-ONLY IN MEMBERSHIP, FROM 2020-01. The store
(``<cache>/screener/metric_store_alpaca_20261022``) holds weekly scans from 2020-01-01: one row per symbol and scan date
with the close, market cap, volume, relative volume, momentum_12_1, Weinstein stage and sector as of that scan date's
close. Which scan a day may use is point-in-time (below), but WHO is in the store is not: the store was built from
today's listings, so it holds survivors only. Names that were large and later stopped trading (TWTR, ATVI, SIVB, FRC,
CELG, XLNX, VMW, ...) are absent, and a universe taken from it is biased towards winners. This is an accepted,
documented bias of the large-cap scale-up (owner decision); it is not a leak of future prices into a row, but it flatters
any strategy that is evaluated on the survivors. Nothing before the first scan (2020-01-01) can be built from it:
``universe_as_of`` raises ``UniverseUnavailable`` there, the column helpers return NaN. The NDQ30 stage uses an explicit
static list instead and does not touch this module's membership.

It is read through ``ba2_providers.screener.metric_store`` (``load_store`` memoises it per process, ``scan_dates``,
``visible_scan_date``, ``check_scan_freshness``).

THE VISIBILITY RULE (daily clock): a scan dated S is visible on day D iff S <= D (``visible_scan_date(...,
intraday=False)``), i.e. for the decision at D's close; the newest visible scan is used and a later one never is. Between
two scans the older scan is held (the universe and the store columns change weekly). A day more than the store's
staleness limit after its newest scan raises ``MetricStoreStaleError`` (never silently reuses the last scan); two scans
more than ``MAX_SCAN_GAP_DAYS`` apart refuse when the store loads.

DEV vs HOLDOUT. ``purpose="dev"`` universes never contain a locked holdout symbol (``ml_holdout.is_locked_largecap``), so
development cross-sections are computed without them. ``purpose="holdout"`` keeps only the locked ones. The final
holdout verdict is computed on the FULL universe (dev and holdout together, about 300 names against about 200 for dev);
build it from the union of both memberships. The holdout features are therefore NOT the dev features of the same
symbols: their cross-sections include the dev names, and that difference is part of the protocol.
"""
from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

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
#: the largest calendar gap tolerated between two consecutive scans (the store is weekly)
MAX_SCAN_GAP_DAYS = 14


class UniverseUnavailable(PanelDataError):
    """The store cannot answer: no scan on or before the day, a gap in the scans, or an ambiguous attribute."""


def store_dir_for(cache_root: Optional[Path] = None) -> Path:
    """THE path of the committed Alpaca metric store under ``cache_root`` (default: the platform cache folder)."""
    return Path(CACHE_FOLDER if cache_root is None else cache_root) / "screener" / DEFAULT_STORE_NAME


def _key(store_dir: Any) -> str:
    return str(Path(store_dir) if store_dir is not None else store_dir_for())


def store_fingerprint(store_dir: Any) -> Dict[str, Any]:
    """What the store is, for provenance: number and total size of its partition files and the newest mtime."""
    files = sorted(Path(_key(store_dir)).glob("ym=*/*.parquet"))
    stats = [f.stat() for f in files]
    return {"files": len(files), "bytes": int(sum(s.st_size for s in stats)),
            "newest_mtime_ns": int(max((s.st_mtime_ns for s in stats), default=0))}


class ScanStore:
    """The loaded store plus its sorted scan dates. One per store dir per process (see ``scan_store``)."""

    def __init__(self, store_dir: str):
        self.store_dir = store_dir
        self.df = ms.load_store(store_dir)
        self.dates: List[str] = ms.scan_dates(self.df, store_dir)
        if not self.dates:
            raise UniverseUnavailable(f"metric store {store_dir} holds no scans")
        ts = pd.to_datetime(pd.Series(self.dates))
        gaps = ts.diff().dt.days
        if len(gaps) > 1 and gaps.max() > MAX_SCAN_GAP_DAYS:
            i = int(gaps.idxmax())
            raise UniverseUnavailable(
                f"metric store {store_dir}: gap of {int(gaps.max())} days between the scans {self.dates[i - 1]} and "
                f"{self.dates[i]} (limit {MAX_SCAN_GAP_DAYS}); holding the older scan across it would be stale")
        self.fingerprint = store_fingerprint(store_dir)


@lru_cache(maxsize=None)
def _scan_store(key: str) -> ScanStore:
    return ScanStore(key)


def scan_store(store_dir: Any = None) -> ScanStore:
    """The store at ``store_dir`` (default: the committed Alpaca store), loaded once per process."""
    return _scan_store(_key(store_dir))


def last_scan_with(symbol: str, store_dir: Any = None) -> Optional[str]:
    """The newest scan date ('YYYY-MM-DD') holding a row for ``symbol``, or None if the store never lists it."""
    st = scan_store(store_dir)
    dates = st.df.loc[st.df["symbol"] == symbol, "date"]
    return None if dates.empty else str(dates.astype(str).max())


def _as_date(day: Any) -> date:
    if isinstance(day, datetime):
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
    """The large-cap universe at ``day``'s close, largest market cap first (survivors only, see the module docstring).

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
    return _universe_at_scan(st, scan, top_n, purpose, min_dollar_volume)


def _universe_at_scan(st: ScanStore, scan: str, top_n: int, purpose: str, min_dollar_volume: float) -> List[str]:
    at = st.dates.index(scan)
    window = st.dates[max(0, at - DV_SCANS + 1): at + 1]
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
    want_locked = purpose == "holdout"
    return [s for s in symbols if ml_holdout.is_locked_largecap(s) == want_locked]


def membership_from_universe(sessions: pd.DatetimeIndex, top_n: int, purpose: str = "dev", store_dir: Any = None,
                             min_dollar_volume: float = DEFAULT_MIN_DOLLAR_VOLUME) -> pd.DataFrame:
    """Per-session membership (``sessions`` x symbols, bool) of the ``universe_as_of`` universe: each session holds the
    universe of the newest scan visible at its close, so membership changes once a week and never before a scan is
    public. Columns are every symbol that is a member on at least one session; sessions before the first scan have
    no member. The input of group X's per-session cross-sections (``build_features(membership=...)``)."""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}, got {purpose!r}")
    st = scan_store(store_dir)
    sessions = pd.DatetimeIndex(sessions)
    if len(sessions) and ms.visible_scan_date(st.dates, sessions[-1].date(), intraday=False) is not None:
        ms.check_scan_freshness(sessions[-1].date().isoformat(), st.dates[-1], st.dates)
    scans = [ms.visible_scan_date(st.dates, t.date(), intraday=False) for t in sessions]
    members = {s: _universe_at_scan(st, s, top_n, purpose, min_dollar_volume) for s in sorted({s for s in scans if s})}
    columns = sorted({sym for syms in members.values() for sym in syms})
    out = pd.DataFrame(False, index=sessions, columns=columns)
    for i, s in enumerate(scans):
        if s is not None and members[s]:
            out.iloc[i, out.columns.get_indexer(members[s])] = True
    out.attrs["membership"] = {"top_n": int(top_n), "purpose": purpose, "min_dollar_volume": float(min_dollar_volume),
                               "store_dir": _key(store_dir)}
    return out


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
        dup = part.duplicated(["date", "symbol"], keep=False)
        if dup.any():
            ex = part[dup].iloc[0]
            raise UniverseUnavailable(f"metric store {st.store_dir}: {int(dup.sum())} duplicate (date, symbol) rows for "
                                      f"{col}, e.g. {ex['symbol']} on {ex['date']}")
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
