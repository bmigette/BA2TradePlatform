"""Point-in-time FRED series with a syncable disk cache.

WHY THIS EXISTS. ``FREDMacroProvider`` is a markdown-report generator: it returns the
LATEST value per indicator, keyed by friendly name ("Unemployment Rate"), with the yield
curve as a list of ``{maturity, yield, date}``. Experts need the opposite -- dated SERIES
keyed by FRED series id, sliced to an ``as_of`` with no lookahead. That mismatch is why
``DeterministicScorer``'s macro section silently produced nothing (see
docs/plans/2026-08-11-shared-news-cache-design.md section 4).

NO-LOOKAHEAD. Two regimes, chosen per series and verified against the live API:

  * REVISED series (UNRATE, CPIAUCSL, PAYEMS, GDP) are published with a lag AND revised
    afterwards. Filtering on the observation date leaks: January's unemployment rate is
    dated 2024-01-01 but was not public until 2024-02-02. These are fetched with
    ``output_type=4`` (initial release only) over the full realtime range, which stamps
    every observation with ``realtime_start`` -- its true first-publication date. We then
    filter on THAT. No lag heuristics, no guessing.

  * UNREVISED daily series (VIXCLS, T10Y3M, BAMLH0A0HYM2, DGS*) are published same-day and
    never restated, so the observation date IS the publication date. They also REJECT
    vintage queries outright ("There are 3907 vintage dates in the specified real-time
    period"), so plain date filtering is both correct and the only option.

HERMETIC BACKTESTS. The full history of a series is fetched ONCE and cached as JSON under
``CACHE_FOLDER/fred/<SERIES_ID>.json``. Because it lives under CACHE_FOLDER it is picked up
by ``cache_sync.build_manifest`` automatically, so remote GA workers receive it with the
rest of the cache and never touch the network. ~10 files of a few hundred KB -- negligible
against a 104k-file manifest.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from ba2_common.config import CACHE_FOLDER
from ba2_common.logger import logger

API_URL = "https://api.stlouisfed.org/fred/series/observations"

# FRED's own sentinel bounds for "every vintage that has ever existed".
_REALTIME_MIN = "1776-07-04"
_REALTIME_MAX = "9999-12-31"

# Series the platform consumes, and how each must be read point-in-time.
#   vintage=True  -> published with a lag and revised; filter on first-publication date
#   vintage=False -> daily, unrevised, same-day; filter on observation date
SERIES_SPEC: Dict[str, Dict[str, Any]] = {
    "VIXCLS":     {"vintage": False, "freq": "daily",   "desc": "CBOE VIX close"},
    "T10Y3M":     {"vintage": False, "freq": "daily",   "desc": "10y-3m Treasury spread (percent)"},
    # Credit spread. NOT the ICE BofA HY OAS (BAMLH0A0HYM2) the expert originally named:
    # FRED serves ICE indices under a rolling ~3-year licence (count=793, starting
    # 2023-08-11), which is useless for a 2020-start backtest. BAA10Y (Moody's Baa less
    # 10y Treasury) is daily, unrestricted and runs from 1986. ``credit_score`` z-scores
    # its input, so it is unit-agnostic and this is a clean drop-in.
    "BAA10Y":     {"vintage": False, "freq": "daily",   "desc": "Moody's Baa - 10y Treasury spread"},
    "DGS10":      {"vintage": False, "freq": "daily",   "desc": "10y Treasury constant maturity"},
    "DGS3MO":     {"vintage": False, "freq": "daily",   "desc": "3m Treasury constant maturity"},
    "UNRATE":     {"vintage": True,  "freq": "monthly", "desc": "Unemployment rate (Sahm rule input)"},
    "CPIAUCSL":   {"vintage": True,  "freq": "monthly", "desc": "CPI, all urban consumers"},
    "PAYEMS":     {"vintage": True,  "freq": "monthly", "desc": "Nonfarm payrolls"},
    "FEDFUNDS":   {"vintage": True,  "freq": "monthly", "desc": "Federal funds effective rate"},
}

# NO PMI INPUT -- deliberately, not an oversight.
#
# The expert's ``pmi_score`` is ``(pmi - 50) / 5``, hard-wired to ISM's 50 expansion
# boundary. ISM revoked FRED's licence around 2016, so ``NAPM`` now returns "The series
# does not exist", and there is no free ISM PMI on FRED. Every candidate stand-in is
# scaled differently: OECD ``BSCICP03USM665S`` was discontinued in Jan-2024, and
# ``UMCSENT`` (~50-110, mean ~85) would evaluate to (85-50)/5 = 7 -> clipped to a
# PERMANENT +1.0. Substituting it would install exactly the class of always-on,
# looks-active-but-isn't input this module exists to remove. The regime composite
# renormalizes over present inputs, so dropping PMI is the honest outcome.

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
# Process-wide memo so a per-bar expert parses each series file once, not once per bar.
_MEM: Dict[str, List[dict]] = {}


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def cache_path(series_id: str) -> str:
    d = os.path.join(CACHE_FOLDER, "fred")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{series_id.upper()}.json")


def _spec(series_id: str) -> Dict[str, Any]:
    try:
        return SERIES_SPEC[series_id.upper()]
    except KeyError:
        raise ValueError(
            f"Unknown FRED series {series_id!r}. Add it to SERIES_SPEC with an explicit "
            f"vintage mode -- guessing whether a series is revised is exactly how lookahead "
            f"gets in."
        ) from None


def fetch_full_history(series_id: str, api_key: str) -> List[dict]:
    """Fetch a series' COMPLETE history from FRED. No date window, no ``limit``.

    The legacy provider passed ``limit=100, sort_order=desc``, which silently truncated every
    series to its last 100 observations -- the credit z-score alone wants ~756. We take the
    whole series once and slice locally instead.

    Vintage series return one row per observation stamped with ``realtime_start`` (its first
    publication date); unrevised series return plain observations.
    """
    sid = series_id.upper()
    spec = _spec(sid)
    params = {
        "series_id": sid,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "asc",
    }
    if spec["vintage"]:
        params["output_type"] = 4                    # initial release only
        params["realtime_start"] = _REALTIME_MIN
        params["realtime_end"] = _REALTIME_MAX

    resp = requests.get(API_URL, params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    if "error_message" in payload:
        raise RuntimeError(f"FRED rejected {sid}: {payload['error_message']}")

    rows = [o for o in payload.get("observations", []) if o.get("value") not in (".", None, "")]
    if not rows:
        raise RuntimeError(f"FRED returned no usable observations for {sid}")
    logger.info("FRED %s: fetched %d observations", sid, len(rows))
    return rows


def refresh_series(series_id: str, api_key: str) -> int:
    """Fetch *series_id* and write it to the disk cache atomically. Returns row count.

    This is the ONLY network path. Experts never call it -- the prewarm/refresh tool does,
    so a backtest reads a file that is already on disk (and already synced to the worker).
    """
    sid = series_id.upper()
    rows = fetch_full_history(sid, api_key)
    path = cache_path(sid)
    tmp = f"{path}.tmp"
    with _lock_for(sid):
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"series_id": sid, "fetched_at": datetime.now(timezone.utc).isoformat(),
                       "vintage": _spec(sid)["vintage"], "observations": rows}, fh)
        os.replace(tmp, path)        # atomic: a concurrent reader never sees a partial file
        _MEM.pop(sid, None)
    return len(rows)


def _load(series_id: str) -> List[dict]:
    sid = series_id.upper()
    cached = _MEM.get(sid)
    if cached is not None:
        return cached
    path = cache_path(sid)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"FRED series {sid} is not in the cache ({path}). Run the FRED refresh/prewarm "
            f"before a backtest -- experts must never fetch macro data on the hot path."
        )
    with open(path, "r", encoding="utf-8") as fh:
        rows = json.load(fh).get("observations", [])
    _MEM[sid] = rows
    return rows


def reset_cache() -> None:
    """Drop the in-process memo (tests, and the live /api/reload path)."""
    _MEM.clear()


def get_series_as_of(series_id: str, as_of: Optional[datetime]) -> pd.Series:
    """Return the series as it was KNOWN at *as_of*, indexed by observation date.

    ``as_of=None`` means "latest" (the live path). For vintage series the cut is on
    first-publication date, so a backtest standing on 2024-01-31 cannot see January's
    unemployment rate -- it was not published until 2024-02-02.
    """
    sid = series_id.upper()
    # Spec BEFORE load: an unknown series is a coding error and must say so, not surface
    # as a confusing "not in the cache" that sends you looking for a prewarm problem.
    vintage = _spec(sid)["vintage"]
    rows = _load(sid)

    cut = None
    if as_of is not None:
        cut = pd.Timestamp(as_of)
        if cut.tz is not None:
            cut = cut.tz_convert("UTC").tz_localize(None)

    dates: List[pd.Timestamp] = []
    values: List[float] = []
    for row in rows:
        try:
            obs_date = pd.Timestamp(row["date"])
            known_on = pd.Timestamp(row["realtime_start"]) if vintage else obs_date
        except (KeyError, ValueError):
            continue
        if cut is not None and known_on > cut:
            continue
        try:
            values.append(float(row["value"]))
        except (TypeError, ValueError):
            continue
        dates.append(obs_date)

    if not dates:
        return pd.Series(dtype="float64")
    return pd.Series(values, index=pd.DatetimeIndex(dates)).sort_index()
