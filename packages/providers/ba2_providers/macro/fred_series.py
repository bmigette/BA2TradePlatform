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
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import requests

from ba2_common.config import CACHE_FOLDER
from ba2_common.core.replay.observe import observe_provider
from ba2_common.core.replay.schemas import ReplayStatus
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

#: How old a cached series may be on the LIVE path before it is refetched, by the frequency
#: SERIES_SPEC already declares. Keyed off the spec rather than one flat number because a
#: monthly series does not become stale in twelve hours, and refetching PAYEMS every run would
#: be a call that can never return anything new.
#:
#: Both values are under a day, so nothing live is ever more than one session behind. The cost
#: is trivial -- nine small series, at most one fetch each per run -- and it is only paid live:
#: a backtest never ages anything (see ``_load``).
LIVE_MAX_AGE_HOURS: Dict[str, float] = {"daily": 12.0, "monthly": 24.0}
#: The fallback for a spec that declares no frequency. The short one: being early costs a
#: request, being late costs a trading decision made on yesterday's regime.
LIVE_MAX_AGE_HOURS_DEFAULT = 12.0

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
# Process-wide memo so a per-bar expert parses each series file once, not once per bar.
_MEM: Dict[str, List[dict]] = {}
#: When each ``_MEM`` entry was loaded, so the LIVE path can let it expire. Without this the
#: memo is the staleness bug on its own: it short-circuits before any file check, so a
#: long-running live process that loaded VIXCLS at startup would serve that same payload for
#: the life of the process no matter how often the file underneath it was refreshed.
#: A BACKTEST never expires its memo -- that is what it is for, and a run must read one payload
#: from first bar to last.
_MEM_AT: Dict[str, float] = {}
# The PARSED form of each memoized series: {series id: (rows object, _ParsedSeries)}.
#
# STALENESS. The rows object is stored WITH the parse and re-checked with ``is`` on every
# read, so the memo can only ever be served for the exact payload it was built from. That
# matters on the LIVE path, where ``_fill_cache_on_the_live_path`` can rewrite a series
# mid-process: ``_load`` then returns a NEW list and this memo misses by construction. A
# memo keyed on the series id alone would happily serve yesterday's macro data into today's
# trading. ``reset_cache()`` and ``refresh_series()`` drop it alongside ``_MEM`` as well --
# belt and braces, and it keeps the parse from outliving the rows it describes.
_PARSED: Dict[str, Any] = {}


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

    # Counted in the SAME purpose counters as FMP (spec section 6: requests and bytes by
    # endpoint and purpose). A warm that refreshes nine macro series is real background
    # traffic, and an allowance that could not see it was governing the wrong half.
    from ba2_providers.fmp_common import record_fmp_bytes, record_fmp_request

    record_fmp_request("fred-observations")
    resp = requests.get(API_URL, params=params, timeout=60)
    resp.raise_for_status()
    record_fmp_bytes("fred-observations", len(getattr(resp, "content", b"") or b""))
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
        _MEM_AT.pop(sid, None)
        _PARSED.pop(sid, None)       # the parse describes the rows we just replaced
    return len(rows)


def _fill_cache_on_the_live_path(sid: str, path: str) -> bool:
    """Fetch a missing OR STALE series and write it to the cache. True when the file exists.

    LIVE ONLY, and that asymmetry is the point. A backtest must read a file that was already
    on disk before it started: fetching mid-run makes the run non-reproducible, un-syncable to
    a GA worker, and dependent on FRED being up -- which is why ``_load`` still raises there.

    Live has the opposite problem. The guard was refusing on a cache that nothing ever filled:
    the live platform has no prewarm step (``tools/refresh_fred_cache.py`` says it should run
    "on a schedule for the live platform" and nothing ever did), so prod's ``cache/fred`` was
    EMPTY and every analysis logged a macro failure. Live is already allowed to reach the
    network for every other provider; macro was the one that refused and then had no other way
    to get the data.

    Best effort by design: a failure here returns False and the caller raises the same
    FileNotFoundError it always did. Macro is an overlay -- it must never be the reason a live
    analysis dies.
    """
    try:
        from ba2_common.core.fred_api_key import resolve_fred_api_key
        api_key = resolve_fred_api_key()
        if not api_key:
            logger.error(
                "FRED series %s is missing and 'fred_api_key' is not configured, so it cannot "
                "be fetched; the macro overlay is unavailable until one is set", sid)
            return False
        logger.info("FRED %s not cached; fetching it once and writing the cache", sid)
        refresh_series(sid, api_key)
        return os.path.exists(path)
    except Exception as e:  # noqa: BLE001 -- see the docstring: never kill a live analysis
        logger.error("FRED %s could not be fetched on the live path: %s", sid, e, exc_info=True)
        return False


def _max_age_hours(sid: str) -> float:
    """How old this series may be on the live path, from the frequency the spec declares."""
    try:
        freq = str(_spec(sid).get("freq") or "")
    except ValueError:
        return LIVE_MAX_AGE_HOURS_DEFAULT
    return LIVE_MAX_AGE_HOURS.get(freq, LIVE_MAX_AGE_HOURS_DEFAULT)


def _age_hours(path: str) -> Optional[float]:
    """Age of the cache file in hours, or None when it cannot be measured."""
    try:
        return max(0.0, (time.time() - os.path.getmtime(path)) / 3600.0)
    except OSError:
        return None


def _is_stale(sid: str, path: str) -> bool:
    """LIVE freshness test. A file whose age cannot be read is treated as FRESH: an unreadable
    mtime is a filesystem oddity, and refetching nine series on every analysis because of one
    is worse than serving data that is probably current."""
    age = _age_hours(path)
    return age is not None and age > _max_age_hours(sid)


def _load(series_id: str) -> List[dict]:
    sid = series_id.upper()
    from ba2_providers.fmp_common import _is_hermetic_fmp_history, _is_ttl_frozen

    # A BACKTEST NEVER AGES ANYTHING. It reads what was prewarmed, memoises it for the whole
    # run, and never reaches the network -- determinism, worker-syncability, and the reason
    # the memo exists at all (a per-bar expert must not re-parse per bar).
    offline = _is_ttl_frozen() or _is_hermetic_fmp_history()

    cached = _MEM.get(sid)
    if cached is not None:
        if offline:
            return cached
        loaded_at = _MEM_AT.get(sid)
        # NO RECORDED TIME MEANS FRESH, never "expired". ``_load`` always stamps what it
        # stores, so in a live process this is unreachable; what it does reach is a memo
        # SEEDED deliberately -- the replay-tap tests patch ``_MEM`` precisely so the read
        # touches neither disk nor network. Expiring an entry whose age is unknown turned
        # that seed into a real disk read of the operator's own cache, which is the opposite
        # of what seeding is for. Expiry requires positive evidence of age.
        if loaded_at is None or (time.time() - loaded_at) / 3600.0 <= _max_age_hours(sid):
            return cached
        # Fall through: the memo is past its window, so re-check the file underneath it.
        # Without this the memo IS the staleness bug -- it short-circuits every check below,
        # so a live process would serve its startup payload for its whole lifetime.

    path = cache_path(sid)
    if not offline and (not os.path.exists(path) or _is_stale(sid, path)):
        # MISSING or STALE, both on the live path only. Missing is the empty-cache case this
        # was written for; stale is the one that makes it stay true -- VIXCLS is a daily
        # series, so a cache filled once and never refreshed is right for a day and wrong
        # after that, which is worse than obviously empty.
        _fill_cache_on_the_live_path(sid, path)

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"FRED series {sid} is not in the cache ({path}). Run the FRED refresh/prewarm "
            f"before a backtest -- experts must never fetch macro data on the hot path."
        )
    with open(path, "r", encoding="utf-8") as fh:
        rows = json.load(fh).get("observations", [])
    # A REFRESH THAT FAILED leaves the old file in place and we read it: stale macro degrades
    # a regime overlay, a hard failure would stop the analysis, and macro is never worth that.
    # Said out loud so it is a decision in the log rather than a silence.
    if not offline and _is_stale(sid, path):
        logger.warning(
            "FRED %s is %.1fh old and could not be refreshed; using the stale copy",
            sid, _age_hours(path) or -1.0)
    _MEM[sid] = rows
    _MEM_AT[sid] = time.time()
    return rows


def reset_cache() -> None:
    """Drop the in-process memos -- raw rows AND their parse (tests, live /api/reload)."""
    _MEM.clear()
    _MEM_AT.clear()
    _PARSED.clear()


class _ParsedSeries:
    """``get_series_as_of``'s per-row parse, done ONCE per cached payload.

    WHY. The function used to walk every raw row on EVERY call, doing two
    ``pd.Timestamp(<string>)`` parses per row, and it is called once per (series,
    decision date). Measured on a real 10-symbol / 501-bar DeterministicScorer
    backtest: 2,004 calls, 633,264 ``strptime`` calls, 49.9 s -- 59% of the whole
    run. The answer for a given cut is a pure FILTER over a parse that never
    changes, so the parse moves here and the call becomes a mask + Series build.

    WHAT IS STORED, and why it is stored this way:

      * ``index``  -- observation dates of the surviving rows, IN ORIGINAL ROW
        ORDER. The order is load-bearing: ``get_series_as_of`` ends in
        ``.sort_index()``, pandas' default sort is not stable, so a different
        pre-sort order can reorder ties and change the returned series.
      * ``known``  -- the date each surviving row became public: ``realtime_start``
        for a vintage series, the observation date otherwise (``_spec(sid)``
        decides, exactly as before).
      * ``values`` -- the parsed floats, aligned with ``index``.
      * ``deferred_known`` / ``deferred_exc`` -- see SKIP SEMANTICS.

    SKIP SEMANTICS, reproduced exactly. The original loop dropped a row when the
    date parse raised KeyError/ValueError, and -- separately -- when
    ``float(row["value"])`` raised TypeError/ValueError; note it appended the
    VALUE first, so a bad value skipped the row entirely rather than leaving the
    two lists misaligned. Both drops are unconditional (a dropped row is dropped
    for every cut), so they happen here.

    A row whose ``"value"`` KEY is missing is the one case that is NOT
    unconditional: ``row["value"]`` raises KeyError, which the original did not
    catch -- but it was only reached for rows INSIDE the cut, because the cut was
    tested first. Such rows are therefore parked in ``deferred_known`` and the
    KeyError is re-raised only by a call whose cut reaches them.

    One accepted narrowing: the DatetimeIndex is built over the full surviving
    set rather than per cut. For a payload whose dates are homogeneous -- every
    FRED file, whose dates are plain ``YYYY-MM-DD`` strings -- that is identical.
    A payload mixing tz-aware and naive dates would raise here for every cut
    instead of only for the cuts that span both, which is the loud direction.
    """

    __slots__ = ("index", "known", "values", "deferred_known", "deferred_exc")

    def __init__(self, rows: List[dict], vintage: bool) -> None:
        dates: List[pd.Timestamp] = []
        known: List[pd.Timestamp] = []
        values: List[float] = []
        deferred: List[pd.Timestamp] = []
        deferred_exc: Optional[KeyError] = None
        for row in rows:
            try:
                obs_date = pd.Timestamp(row["date"])
                known_on = pd.Timestamp(row["realtime_start"]) if vintage else obs_date
            except (KeyError, ValueError):
                continue
            try:
                value = float(row["value"])
            except (TypeError, ValueError):
                continue
            except KeyError as e:
                # No "value" key at all: the original raised this, but only once a
                # cut reached the row. Defer it rather than dropping the row.
                deferred.append(known_on)
                if deferred_exc is None:
                    deferred_exc = e
                continue
            values.append(value)
            dates.append(obs_date)
            known.append(known_on)
        self.index = pd.DatetimeIndex(dates)
        # Non-vintage rows are known on their observation date -- the same objects,
        # so the index is aliased rather than rebuilt.
        self.known = pd.DatetimeIndex(known) if vintage else self.index
        self.values = np.asarray(values, dtype="float64")
        self.deferred_known = pd.DatetimeIndex(deferred) if deferred else None
        self.deferred_exc = deferred_exc


def _parsed(sid: str, rows: List[dict], vintage: bool) -> _ParsedSeries:
    """The parse of *rows*, built once and served only back to that same object."""
    entry = _PARSED.get(sid)
    if entry is not None and entry[0] is rows:
        return entry[1]
    parsed = _ParsedSeries(rows, vintage)
    _PARSED[sid] = (rows, parsed)
    return parsed


def series_identity(args):
    """What makes a point-in-time FRED read what it is: the series and the cut.

    Named (not an inline lambda) because the offline replay tape imports it to
    look a recorded series up by exactly the identity the tap wrote. ``as_of`` is
    taken AS THE CALLER PASSED IT (``None`` on the live path, which means "every
    vintage published so far"): normalizing it here would build a key the caller
    cannot reproduce.
    """
    return {"series_id": args["series_id"], "as_of": args["as_of"]}


@observe_provider("macro", "get_series_as_of", identity=series_identity,
                  provenance=ReplayStatus.PROVENANCE_DISK_CACHE)
def get_series_as_of(series_id: str, as_of: Optional[datetime]) -> pd.Series:
    """Return the series as it was KNOWN at *as_of*, indexed by observation date.

    Recorded at this boundary (provenance ``disk_cache``): this function NEVER
    reaches the network -- it reads the synced cache file (or the in-process memo
    of it) and raises when the series was not warmed -- so every return here came
    off disk by construction.

    ``as_of=None`` means "latest" (the live path). For vintage series the cut is on
    first-publication date, so a backtest standing on 2024-01-31 cannot see January's
    unemployment rate -- it was not published until 2024-02-02.
    """
    sid = series_id.upper()
    # Spec BEFORE load: an unknown series is a coding error and must say so, not surface
    # as a confusing "not in the cache" that sends you looking for a prewarm problem.
    vintage = _spec(sid)["vintage"]
    rows = _load(sid)
    # Parse once per payload (see _ParsedSeries); this call is then a filter + build.
    parsed = _parsed(sid, rows, vintage)

    cut = None
    if as_of is not None:
        cut = pd.Timestamp(as_of)
        if cut.tz is not None:
            cut = cut.tz_convert("UTC").tz_localize(None)

    if cut is None:
        if parsed.deferred_exc is not None:
            raise parsed.deferred_exc
        keep = np.ones(parsed.values.shape, dtype=bool)
    else:
        if parsed.deferred_known is not None and bool((~(parsed.deferred_known > cut)).any()):
            raise parsed.deferred_exc
        # ``~(known > cut)``, NOT ``known <= cut``. ``pd.Timestamp(None)`` is NaT, which
        # compares False both ways -- and the original only skipped on ``known_on > cut``,
        # so a NaT row was KEPT. Spelling this as <= would silently start dropping it.
        keep = ~np.asarray(parsed.known > cut)

    values = parsed.values[keep]
    if not values.size:
        return pd.Series(dtype="float64")
    return pd.Series(values, index=parsed.index[keep]).sort_index()
