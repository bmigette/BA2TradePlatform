"""Point-in-time FRED series with a syncable disk cache.

WHY THIS EXISTS. ``FREDMacroProvider`` is a markdown-report generator: it returns the
LATEST value per indicator, keyed by friendly name ("Unemployment Rate"), with the yield
curve as a list of ``{maturity, yield, date}``. Experts need the opposite -- dated SERIES
keyed by FRED series id, sliced to an ``as_of`` with no lookahead. That mismatch is why
``DeterministicScorer``'s macro section silently produced nothing (see
docs/plans/2026-08-11-shared-news-cache-design.md section 4).

NO-LOOKAHEAD: FIRST-RELEASE AVAILABILITY (2026-09-26). A macro SIGNAL input is known to a
decision only once FRED had actually published it. Every signal series is therefore cached with
the date each observation FIRST appeared on FRED -- its ALFRED ``realtime_start`` of initial
release -- and read through ONE rule, shared by the backtest and live:

    a row is visible to a decision labelled L  <=>  first_release_date < L

``L`` is the platform's decision label (``market_calendar``): live, the New York date of the
decision instant; a DAILY backtest bar D, ``backtest_decision_label(D)`` = the next regular
session N(D), because bar D decides on D's close and fills at N(D)'s open -- it IS the live
decision made at ~09:30 ET on N(D). See :func:`decision_label`.

STRICTLY BEFORE L, because FRED's vintages are day-granular and a vintage dated L is released
DURING L (measured: at 09:32 ET on 2026-09-25 BAA10Y's vintage of that day did not exist yet,
nor DGS10's at 06:55 ET on 2026-08-11; T10Y3M/VIXCLS vintage d carries d's own close, so it
cannot exist before that close). A
release on the morning of L (UNRATE/CPI at 08:30 ET) is therefore NOT visible on L, in the
backtest AND live -- one rule, so they cannot drift.

WHY NOT THE OBSERVATION DATE. Until 2026-09-26 the daily series (VIXCLS, BAA10Y, T10Y3M) were
cut on their observation date against the bar's midnight-UTC ``as_of``, so a backtest decision
on bar d saw rows dated d. FRED publishes them later: BAA10Y/DGS10 obs d appear in the vintage
of d+1 (or later), VIXCLS has stalled for days (on 2026-09-25 the live cache ended at 09-22).
The measured live fetch at 13:32Z on 2026-09-25 held VIXCLS->09-22, BAA10Y->09-23,
T10Y3M->09-24; the backtest bar 09-24 (same decision) saw 09-24 on all three.

HOW THE FIRST-RELEASE DATE IS OBTAINED (``_fetch_first_release``). ALFRED refuses a vintage
query spanning more than ~2000 vintage dates, so the history is assembled from: the series'
vintage-date list; the FIRST vintage's full snapshot (every row it held is stamped with that
vintage, the earliest date anything can be proven public); ``output_type=4`` (initial release
only) over consecutive windows of the vintage list; and, for the handful of rows the current
vintage holds but no initial release carries as a number (first published as "." and filled
later), a per-row walk of their real-time periods. VALUES are the first-release values, the
number that was on FRED the day the row became visible -- revisions are not applied (they were
not for UNRATE before this either).

REFUSED, never guessed (:class:`MacroAvailabilityUnknown`): a missing cache file (not the
OSError it used to be -- the expert's broad handler absorbs those); a cache written in the old
observation-date format; a decision labelled on or before the series' first vintage (nothing
is provably public yet); a decision labelled after the day the cache was fetched (vintages
after the fetch are unknown -- a backtest cannot see them, a live run must refetch first).

LIVE FETCHING. JobManager refreshes the series at 09:00 ET (``refresh_for_live_decision``) so
the 09:30 analyses read a file fetched that morning. A read that still finds its file not
covering today refetches it itself -- one fetch per series at a time (per-series lock with a
double-check), with a ``LIVE_REFETCH_BACKOFF_SECONDS`` backoff after a failure -- and refuses
if that does not succeed.

THE ONE EXCEPTION: DGS3MO is the option Black-Scholes risk-free rate (``risk_free_rate``), not a
signal. It inverts bar d's OWN close, known only after that close, so it stays SAME-DAY on the
observation date (reviewed and accepted 2026-09-26). It keeps its plain-observation cache format
and its ``as_of`` cut here is on the observation date.

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
from datetime import date, datetime, time as dtime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

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

API_VINTAGEDATES_URL = "https://api.stlouisfed.org/fred/series/vintagedates"

#: How a series' rows become known to a decision (see the module docstring).
AVAIL_FIRST_RELEASE = "first_release"      # signal input: visible once first published, < L
AVAIL_SAME_DAY_CLOSE = "same_day_close"    # DGS3MO only: BS rate, observation-date cut
#: The cache-file marker of a first-release payload. A file without it (the pre-2026-09-26
#: observation-date format, whose ``realtime_start`` is the FETCH vintage for every row) is
#: refused: reading it as first-release would hide every row before the fetch day.
CACHE_FORMAT_FIRST_RELEASE = "first_release_v1"
#: The calendar a cache's fetch DAY is read on: New York, the calendar of the decision label it
#: is compared with (review 2026-09-26: comparing a Chicago fetch date with a New York label
#: misjudged every fetch made 00:00-01:00 ET). A file fetched on New York date F holds every
#: vintage FRED released before F began in New York; FRED's releases are daytime Central-time
#: events, so none falls in the 23:00-00:00 CT hour this treats as "the next day".
FETCH_DAY_TZ = ZoneInfo("America/New_York")

#: After a failed LIVE refetch, how long every read of that series refuses without re-running
#: the ~21-request ALFRED fetch (review 2026-09-26, I3). Each symbol of an analysis batch reads
#: the macro series; without this, FRED being down meant one full refetch attempt (up to 120 s
#: per request) PER SYMBOL. The reads still REFUSE loudly -- the backoff only stops the retry
#: storm -- and the scheduled pre-open refresh (``refresh_for_live_decision``) ignores it.
LIVE_REFETCH_BACKOFF_SECONDS = 300.0
#: Vintage dates per ALFRED window. FRED refuses a request whose real-time period spans more
#: than ~2000 ("There are 3907 vintage dates in the specified real-time period").
_VINTAGE_WINDOW = 1500

# Series the platform consumes, and how each must be read point-in-time.
SERIES_SPEC: Dict[str, Dict[str, Any]] = {
    "VIXCLS":     {"availability": AVAIL_FIRST_RELEASE, "freq": "daily",   "desc": "CBOE VIX close"},
    "T10Y3M":     {"availability": AVAIL_FIRST_RELEASE, "freq": "daily",   "desc": "10y-3m Treasury spread (percent)"},
    # Credit spread. NOT the ICE BofA HY OAS (BAMLH0A0HYM2) the expert originally named:
    # FRED serves ICE indices under a rolling ~3-year licence (count=793, starting
    # 2023-08-11), which is useless for a 2020-start backtest. BAA10Y (Moody's Baa less
    # 10y Treasury) is daily, unrestricted and runs from 1986. ``credit_score`` z-scores
    # its input, so it is unit-agnostic and this is a clean drop-in.
    "BAA10Y":     {"availability": AVAIL_FIRST_RELEASE, "freq": "daily",   "desc": "Moody's Baa - 10y Treasury spread"},
    "DGS10":      {"availability": AVAIL_FIRST_RELEASE, "freq": "daily",   "desc": "10y Treasury constant maturity"},
    # The option BS rate, NOT a signal input: same-day by design (see the module docstring).
    "DGS3MO":     {"availability": AVAIL_SAME_DAY_CLOSE, "freq": "daily",  "desc": "3m Treasury constant maturity"},
    "UNRATE":     {"availability": AVAIL_FIRST_RELEASE, "freq": "monthly", "desc": "Unemployment rate (Sahm rule input)"},
    "CPIAUCSL":   {"availability": AVAIL_FIRST_RELEASE, "freq": "monthly", "desc": "CPI, all urban consumers"},
    "PAYEMS":     {"availability": AVAIL_FIRST_RELEASE, "freq": "monthly", "desc": "Nonfarm payrolls"},
    "FEDFUNDS":   {"availability": AVAIL_FIRST_RELEASE, "freq": "monthly", "desc": "Federal funds effective rate"},
}


class MacroAvailabilityUnknown(RuntimeError):
    """When a macro row became public cannot be established for this decision -- refused.

    Not an ``OSError`` on purpose: ``absorb_if_benign`` treats the OSError family as benign
    (a missing file), and this is not the world being uncooperative, it is a decision that would
    otherwise be made on data it could not have had (or without data it would have had).
    """


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
#: The cache-file HEADER of each memoized series (everything but the observations): the format
#: marker, ``first_vintage`` and ``fetched_at`` the first-release reader refuses on. Loaded with
#: ``_MEM`` and dropped with it. A series memoized WITHOUT a header (a test seeding ``_MEM``
#: directly) is refused by the first-release reader exactly like an old-format file.
_META: Dict[str, Dict[str, Any]] = {}
#: One live FETCH per series at a time (separate from ``_locks``, which ``refresh_series`` holds
#: around the atomic write: re-taking it here would deadlock).
_FETCH_LOCKS: Dict[str, threading.Lock] = {}
#: ``_monotonic()`` of each series' last failed live fetch -- the backoff clock.
_FETCH_FAILED_AT: Dict[str, float] = {}


def _monotonic() -> float:
    """The backoff clock (a seam for tests)."""
    return time.monotonic()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _fetch_lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _FETCH_LOCKS.setdefault(key, threading.Lock())


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
            f"availability mode -- guessing when a series becomes public is exactly how "
            f"lookahead gets in."
        ) from None


def _fred_get(url: str, params: Dict[str, Any], sid: str) -> Dict[str, Any]:
    """One FRED GET, counted in the SAME purpose counters as FMP (spec section 6: requests and
    bytes by endpoint and purpose). A warm that refreshes the macro series is real background
    traffic, and an allowance that could not see it was governing the wrong half."""
    from ba2_providers.fmp_common import record_fmp_bytes, record_fmp_request

    record_fmp_request("fred-observations")
    resp = requests.get(url, params=params, timeout=120)
    resp.raise_for_status()
    record_fmp_bytes("fred-observations", len(getattr(resp, "content", b"") or b""))
    payload = resp.json()
    if "error_message" in payload:
        raise RuntimeError(f"FRED rejected {sid}: {payload['error_message']}")
    return payload


def _is_value(v: Any) -> bool:
    """FRED writes "." for a date with no print; that is no observation, never a zero."""
    return v not in (".", None, "")


def _fetch_plain(sid: str, api_key: str) -> List[dict]:
    """The current vintage's observations, oldest first (no real-time window)."""
    payload = _fred_get(API_URL, {"series_id": sid, "api_key": api_key, "file_type": "json",
                                  "sort_order": "asc"}, sid)
    return list(payload.get("observations", []))


def _fetch_vintage_dates(sid: str, api_key: str) -> List[str]:
    """Every vintage date FRED/ALFRED holds for *sid*, ascending (paged)."""
    out: List[str] = []
    while True:
        payload = _fred_get(API_VINTAGEDATES_URL, {
            "series_id": sid, "api_key": api_key, "file_type": "json",
            "limit": 10000, "offset": len(out)}, sid)
        page = list(payload.get("vintage_dates", []))
        out.extend(page)
        if not page or len(out) >= int(payload["count"]):
            break
    if not out:
        raise RuntimeError(f"FRED returned no vintage dates for {sid}")
    if out != sorted(out):
        raise RuntimeError(f"FRED vintage dates for {sid} are not ascending")
    return out


def _vintage_windows(vintages: List[str]) -> List[Tuple[str, str]]:
    """Consecutive, non-overlapping real-time windows covering every vintage, each under
    FRED's vintage-count limit. The last one is open-ended."""
    chunks = [vintages[i:i + _VINTAGE_WINDOW] for i in range(0, len(vintages), _VINTAGE_WINDOW)]
    return [(c[0], c[-1] if i < len(chunks) - 1 else _REALTIME_MAX) for i, c in enumerate(chunks)]


def _first_numeric_release(sid: str, api_key: str, obs_date: str,
                           windows: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    """``(realtime_start, value)`` of the first vintage that carried *obs_date* as a NUMBER.

    For the rows ``output_type=4`` cannot date: first published as "." and filled in a later
    vintage, or absent from the initial releases altogether. Walks the row's real-time periods
    window by window, oldest first. Within a window the periods are clamped to its start, but the
    FIRST window holding a numeric period is the one that period began in, so its start is
    exact. ``None`` when no vintage ever carried it as a number.
    """
    for w_start, w_end in windows:
        payload = _fred_get(API_URL, {
            "series_id": sid, "api_key": api_key, "file_type": "json", "sort_order": "asc",
            "observation_start": obs_date, "observation_end": obs_date,
            "realtime_start": w_start, "realtime_end": w_end}, sid)
        numeric = sorted((o["realtime_start"], o["value"])
                         for o in payload.get("observations", [])
                         if o.get("date") == obs_date and _is_value(o.get("value")))
        if numeric:
            return numeric[0]
    return None


def _fetch_first_release(sid: str, api_key: str,
                         prior_late: Optional[Dict[str, Tuple[str, str]]] = None,
                         ) -> Tuple[List[dict], Dict[str, Any]]:
    """Every observation with the date it FIRST appeared on FRED (see the module docstring).

    Returns ``(rows, header)``: rows ``{"date", "value", "realtime_start"}`` ascending by date,
    ``realtime_start`` = first-release vintage date and ``value`` = the value first published;
    header ``{"first_vintage", "last_vintage", "n_vintages", "late_filled"}``.

    Raises ``RuntimeError`` when a row of the current vintage cannot be dated: a row whose
    availability is unknown is refused at fetch time, never cached as if it had always been
    public.

    ``prior_late`` -- ``{date: (realtime_start, value)}`` of rows the previous cache already
    dated by the per-row walk (step 3). ALFRED's history is append-only, so a first release
    found once never moves; reusing it keeps a live refresh from re-walking (each walk is up to
    one request per vintage window, ~20 s apiece on FRED's side).
    """
    vintages = _fetch_vintage_dates(sid, api_key)
    first = vintages[0]
    known: Dict[str, Tuple[str, str]] = {}

    # 1. The first vintage's full snapshot. output_type=4 only reports rows NEW in a vintage, so
    #    every row the first vintage already held is read here, stamped with that vintage -- the
    #    earliest date it can be proven public.
    snap = _fred_get(API_URL, {"series_id": sid, "api_key": api_key, "file_type": "json",
                               "sort_order": "asc", "realtime_start": first,
                               "realtime_end": first}, sid)
    for o in snap.get("observations", []):
        known[o["date"]] = (first, o["value"])

    # 2. Initial releases, window by window. A row is reported only by the window holding its
    #    first release (verified 2026-09-26: a window starting the day after a release omits that
    #    row rather than clamping it), so the windows neither overlap nor miss one. Min wins,
    #    defensively.
    windows = _vintage_windows(vintages)
    for w_start, w_end in windows:
        payload = _fred_get(API_URL, {
            "series_id": sid, "api_key": api_key, "file_type": "json", "sort_order": "asc",
            "output_type": 4, "realtime_start": w_start, "realtime_end": w_end}, sid)
        for o in payload.get("observations", []):
            cur = known.get(o["date"])
            if cur is None or o["realtime_start"] < cur[0]:
                known[o["date"]] = (o["realtime_start"], o["value"])

    # 3. Rows the CURRENT vintage carries as a number whose first release was "." (or that no
    #    initial release reported at all): dated by their own real-time periods.
    late_filled: List[str] = []
    for o in _fetch_plain(sid, api_key):
        if not _is_value(o.get("value")):
            continue
        d = o["date"]
        cur = known.get(d)
        if cur is not None and _is_value(cur[1]):
            continue
        found = (prior_late or {}).get(d) or _first_numeric_release(sid, api_key, d, windows)
        if found is None:
            raise RuntimeError(
                f"FRED {sid}: observation {d} is in the current vintage but no vintage records "
                f"when it was first published -- its availability is unknown, so the series is "
                f"not cached")
        known[d] = found
        late_filled.append(d)

    rows = [{"date": d, "value": v, "realtime_start": rt}
            for d, (rt, v) in sorted(known.items()) if _is_value(v)]
    if not rows:
        raise RuntimeError(f"FRED returned no usable observations for {sid}")
    _warn_releases_before_their_session(sid, rows, first)
    header = {"first_vintage": first, "last_vintage": vintages[-1],
              "n_vintages": len(vintages), "late_filled": late_filled}
    return rows, header


def _prior_late_filled(sid: str) -> Dict[str, Tuple[str, str]]:
    """The per-row-walked first releases of the CURRENT first-release cache file, if any."""
    path = cache_path(sid)
    if not os.path.exists(path):
        return {}
    try:
        doc = _read_doc(path)
    except (OSError, ValueError):
        return {}
    if doc.get("format") != CACHE_FORMAT_FIRST_RELEASE:
        return {}
    late = set(doc.get("late_filled") or [])
    return {o["date"]: (o["realtime_start"], o["value"])
            for o in doc.get("observations", []) if o.get("date") in late}


def _warn_releases_before_their_session(sid: str, rows: List[dict], first_vintage: str) -> None:
    """WARN when a row was first released BEFORE its own observation date on a trading day.

    Only plausible for FRED's pre-inserted rows dated on a non-session day (VIXCLS carries US
    holidays, published the business day before -- 25 of them since 2020). On a regular session
    it means the value could not have been the close it claims to be: worth a look, not a
    refusal (the reader shows it no earlier than it was published either way). Rows stamped
    with the first vintage are skipped -- that date is an upper bound, not a release.
    """
    from ba2_common.core.market_calendar import is_regular_session

    odd: List[str] = []
    for r in rows:
        if r["realtime_start"] >= r["date"] or r["realtime_start"] == first_vintage:
            continue
        try:
            if is_regular_session(date.fromisoformat(r["date"])):
                odd.append(f"{r['date']} (first released {r['realtime_start']})")
        except ValueError:
            continue                  # outside the calendar table: nothing to compare with
    if odd:
        logger.warning("FRED %s: %d row(s) first released before its observation date on a "
                       "trading day -- check the source: %s", sid, len(odd), ", ".join(odd[:10]))


def _fetch_payload(series_id: str, api_key: str) -> Tuple[List[dict], Dict[str, Any]]:
    sid = series_id.upper()
    spec = _spec(sid)
    if spec["availability"] == AVAIL_FIRST_RELEASE:
        rows, header = _fetch_first_release(sid, api_key, _prior_late_filled(sid))
    else:
        rows = [o for o in _fetch_plain(sid, api_key) if _is_value(o.get("value"))]
        if not rows:
            raise RuntimeError(f"FRED returned no usable observations for {sid}")
        header = {}
    logger.info("FRED %s: fetched %d observations", sid, len(rows))
    return rows, header


def fetch_full_history(series_id: str, api_key: str) -> List[dict]:
    """Fetch a series' COMPLETE history from FRED. No date window, no ``limit``.

    The legacy provider passed ``limit=100, sort_order=desc``, which silently truncated every
    series to its last 100 observations -- the credit z-score alone wants ~756. We take the
    whole series once and slice locally instead.

    A first-release series returns rows stamped with their first-publication date (see
    ``_fetch_first_release``); the same-day series (DGS3MO) returns plain observations.
    """
    return _fetch_payload(series_id, api_key)[0]


def refresh_series(series_id: str, api_key: str) -> int:
    """Fetch *series_id* and write it to the disk cache atomically. Returns row count.

    This is the ONLY network path. Experts never call it -- the prewarm/refresh tool does,
    so a backtest reads a file that is already on disk (and already synced to the worker).

    A first-release series is written with ``"format": CACHE_FORMAT_FIRST_RELEASE`` and its
    vintage bounds; the same-day series (DGS3MO) keeps exactly the header it always had, so the
    option risk-free-rate reader is untouched.
    """
    sid = series_id.upper()
    rows, header = _fetch_payload(sid, api_key)
    fetched_at = datetime.now(timezone.utc).isoformat()
    if _spec(sid)["availability"] == AVAIL_FIRST_RELEASE:
        doc = {"series_id": sid, "fetched_at": fetched_at,
               "availability": AVAIL_FIRST_RELEASE, "format": CACHE_FORMAT_FIRST_RELEASE,
               **header, "observations": rows}
    else:
        doc = {"series_id": sid, "fetched_at": fetched_at, "vintage": False,
               "observations": rows}
    path = cache_path(sid)
    tmp = f"{path}.tmp"
    with _lock_for(sid):
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)        # atomic: a concurrent reader never sees a partial file
        _MEM.pop(sid, None)
        _MEM_AT.pop(sid, None)
        _META.pop(sid, None)
        _PARSED.pop(sid, None)       # the parse describes the rows we just replaced
    return len(rows)


def _file_is_current_for_live(sid: str, path: str) -> bool:
    """The file exists, is inside its live age window, and covers today's decision."""
    if not os.path.exists(path) or _is_stale(sid, path):
        return False
    try:
        return _covers_live_today(sid, _header(_read_doc(path)))
    except (OSError, ValueError):
        return False


def _fill_cache_on_the_live_path(sid: str, path: str, *, ignore_backoff: bool = False) -> bool:
    """Fetch a missing, STALE or not-today series and write it to the cache. True on success
    (or when another thread just did it).

    LIVE ONLY, and that asymmetry is the point. A backtest must read a file that was already
    on disk before it started: fetching mid-run makes the run non-reproducible, un-syncable to
    a GA worker, and dependent on FRED being up -- which is why ``_load`` still raises there.

    Live has the opposite problem. The guard was refusing on a cache that nothing ever filled:
    the live platform had no prewarm step, so prod's ``cache/fred`` was EMPTY and every analysis
    logged a macro failure. The scheduled pre-open refresh (``refresh_for_live_decision``, run
    by JobManager at 09:00 ET) is now the normal filler; this is the fallback for a read that
    finds the file still not covering today.

    GUARDED (review 2026-09-26, I3), because a first-release fetch is ~21 ALFRED requests:
      * ONE fetch per series at a time: the per-series lock, with a DOUBLE-CHECK after it is
        acquired -- the worker threads that queued behind a fetch read its result instead of
        repeating it;
      * a BACKOFF of ``LIVE_REFETCH_BACKOFF_SECONDS`` after a failure: the reads in that window
        do not refetch (each symbol would otherwise re-run the whole fetch against a FRED that
        is down), but they still refuse -- ``get_series_as_of`` raises
        ``MacroAvailabilityUnknown`` for a file that does not cover today, and a missing one.
        ``ignore_backoff`` is for the scheduled refresh, which IS the retry.

    A failure returns False and is logged at ERROR; it never raises out of here. The REFUSAL
    is ``get_series_as_of``'s: a live decision must not run on fewer vintages than the backtest
    of the same decision sees.
    """
    with _fetch_lock_for(sid):
        if _file_is_current_for_live(sid, path):
            return True                      # another thread fetched it while we waited
        failed_at = _FETCH_FAILED_AT.get(sid)
        if (not ignore_backoff and failed_at is not None
                and _monotonic() - failed_at < LIVE_REFETCH_BACKOFF_SECONDS):
            logger.error(
                "FRED %s: the last live fetch failed %.0fs ago; backing off for %.0fs before "
                "trying again -- reads of it refuse until then", sid,
                _monotonic() - failed_at, LIVE_REFETCH_BACKOFF_SECONDS)
            return False
        try:
            from ba2_common.core.fred_api_key import resolve_fred_api_key
            api_key = resolve_fred_api_key()
            if not api_key:
                logger.error(
                    "FRED series %s needs fetching and 'fred_api_key' is not configured, so it "
                    "cannot be; the macro overlay refuses until one is set", sid)
                return False
            logger.info("FRED %s missing, stale or older than today's decision; fetching it "
                        "and writing the cache", sid)
            refresh_series(sid, api_key)
        except Exception as e:  # noqa: BLE001 -- reported here; the READ is what refuses
            _FETCH_FAILED_AT[sid] = _monotonic()
            logger.error("FRED %s could not be fetched on the live path: %s", sid, e,
                         exc_info=True)
            return False
        _FETCH_FAILED_AT.pop(sid, None)
        return os.path.exists(path)


def refresh_for_live_decision(series_ids: List[str]) -> Dict[str, List[str]]:
    """Make every first-release series in *series_ids* cover today's live decision.

    What the JobManager's 09:00 ET pre-open job runs, so the 09:30 analyses read files fetched
    that morning instead of depending on ~21 ALFRED requests succeeding at the instant they run
    (entries are weekly: one failed 09:30 costs a week). A series already current is left
    alone; the rest are fetched through the same guarded path a read would use, ignoring a
    backoff a failed read left behind (this IS the retry). Failures are logged at ERROR and
    returned -- never raised, so one series cannot stop the others.

    Returns ``{"refreshed": [...], "current": [...], "failed": [...]}``.

    Raises:
        ValueError: a series that is not read by first-release date (DGS3MO, the option BS
            rate, has its own warm path and no live decision reads it).
    """
    sids = [sid.upper() for sid in series_ids]
    wrong = [sid for sid in sids if _spec(sid)["availability"] != AVAIL_FIRST_RELEASE]
    if wrong:
        raise ValueError(f"not first-release macro series: {wrong}")
    out: Dict[str, List[str]] = {"refreshed": [], "current": [], "failed": []}
    for sid in sids:
        path = cache_path(sid)
        if _file_is_current_for_live(sid, path):
            out["current"].append(sid)
        elif _fill_cache_on_the_live_path(sid, path, ignore_backoff=True):
            out["refreshed"].append(sid)
        else:
            out["failed"].append(sid)
    if out["failed"]:
        logger.error("FRED pre-open refresh FAILED for %s: today's decisions reading them will "
                     "refuse unless a later read's refetch succeeds", out["failed"])
    return out


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


def _read_doc(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _header(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Everything in a cache document except its observations."""
    return {k: v for k, v in doc.items() if k != "observations"}


def _fetched_day(meta: Optional[Dict[str, Any]]) -> Optional[date]:
    """The NEW YORK calendar date the cache was fetched on (the decision label's calendar), or
    None when unrecorded."""
    raw = (meta or {}).get("fetched_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return None                  # an instant with no timezone proves nothing
    return ts.astimezone(FETCH_DAY_TZ).date()


def _is_first_release_payload(meta: Optional[Dict[str, Any]]) -> bool:
    return bool(meta) and meta.get("format") == CACHE_FORMAT_FIRST_RELEASE


def is_current_format(series_id: str, path: Optional[str] = None) -> bool:
    """False when *series_id* is read by first-release date but its cache file is not in that
    format (the reader refuses it). A series read some other way, or a missing/unreadable file,
    is True here: this answers "is the FORMAT wrong", and absence is reported by whatever reads
    it next. The whole document is parsed (not a header sniff): the tools and warm planner that
    ask are not on a hot path, and key order is not part of the format."""
    sid = series_id.upper()
    spec = SERIES_SPEC.get(sid) or {}
    if spec.get("availability") != AVAIL_FIRST_RELEASE:
        return True
    try:
        doc = _read_doc(path or cache_path(sid))
    except (OSError, ValueError):
        return True
    return isinstance(doc, dict) and _is_first_release_payload(_header(doc))


def cache_is_fresh(series_id: str, max_age_hours: float) -> bool:
    """For the warm/refresh tools: the file exists, is younger than *max_age_hours*, AND is in
    the format its reader accepts. A young file in the old observation-date format is NOT fresh
    -- skipping it as fresh left a file the reader refuses (review 2026-09-26, item 4)."""
    path = cache_path(series_id)
    if not os.path.exists(path):
        return False
    age = _age_hours(path)
    if age is None or age >= max_age_hours:
        return False
    return is_current_format(series_id, path)


def _covers_live_today(sid: str, meta: Optional[Dict[str, Any]]) -> bool:
    """LIVE: does this payload hold every vintage a decision made NOW may see?

    A first-release series must be in the current format and fetched on or after today's
    decision label (both New York dates), else a vintage published since the fetch would be missing
    live while a backtest of the same decision sees it. A same-day series (DGS3MO) is governed by
    its age alone, as before.
    """
    if _spec(sid)["availability"] != AVAIL_FIRST_RELEASE:
        return True
    if not _is_first_release_payload(meta):
        return False
    fetched = _fetched_day(meta)
    return fetched is not None and fetched >= decision_label(None)


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
        if loaded_at is None:
            return cached
        if ((time.time() - loaded_at) / 3600.0 <= _max_age_hours(sid)
                and _covers_live_today(sid, _META.get(sid))):
            return cached
        # Fall through: the memo is past its window (or was loaded before today's decision
        # label), so re-check the file underneath it. Without this the memo IS the staleness
        # bug -- it short-circuits every check below, so a live process would serve its
        # startup payload for its whole lifetime.

    path = cache_path(sid)
    doc: Optional[Dict[str, Any]] = None
    if not offline:
        if not os.path.exists(path) or _is_stale(sid, path):
            # MISSING or STALE, both on the live path only. Missing is the empty-cache case this
            # was written for; stale is the one that makes it stay true -- VIXCLS is a daily
            # series, so a cache filled once and never refreshed is right for a day and wrong
            # after that, which is worse than obviously empty.
            _fill_cache_on_the_live_path(sid, path)
        else:
            doc = _read_doc(path)
            if not _covers_live_today(sid, _header(doc)):
                # Old format, or fetched before today's decision label: refetch, so live holds
                # every vintage the same decision sees in a backtest.
                if _fill_cache_on_the_live_path(sid, path):
                    doc = None

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"FRED series {sid} is not in the cache ({path}). Run the FRED refresh/prewarm "
            f"before a backtest -- experts must never fetch macro data on the hot path."
        )
    if doc is None:
        doc = _read_doc(path)
    rows = doc.get("observations", [])
    meta = _header(doc)
    # A REFRESH THAT FAILED leaves the old file in place and we read it. For the same-day
    # series (DGS3MO) that is the documented degrade, said out loud. A FIRST-RELEASE series is
    # then refused by ``get_series_as_of`` (fetched before the decision label, or old format):
    # the rows it lacks are rows a backtest of this decision would see.
    if not offline and (_is_stale(sid, path) or not _covers_live_today(sid, meta)):
        logger.warning(
            "FRED %s is %.1fh old (fetched %s) and could not be refreshed; a first-release "
            "series is refused for decisions after its fetch day",
            sid, _age_hours(path) or -1.0, meta.get("fetched_at"))
    _MEM[sid] = rows
    _META[sid] = meta
    _MEM_AT[sid] = time.time()
    return rows


def reset_cache() -> None:
    """Drop the in-process memos -- raw rows, headers AND their parse (tests, /api/reload)."""
    _MEM.clear()
    _MEM_AT.clear()
    _META.clear()
    _PARSED.clear()


# --------------------------------------------------------------------------- #
# THE decision label -- one rule for the backtest and live.
# --------------------------------------------------------------------------- #

def _live_decision_instant() -> datetime:
    """The instant a live decision is made at.

    Inside an enter-market decision pass it is the pass's frozen ``decision_time``; otherwise
    the wall clock. Deliberately NOT ``replay_now``: that RECORDS a clock read under capture,
    and this function runs inside the tapped ``get_series_as_of`` whose replay returns the
    recorded payload without re-reading the clock -- a read recorded here would shift every
    later clock read of the analysis by one.
    """
    from ba2_common.core.market_condition_live import current_decision

    decision = current_decision()
    if decision is not None:
        return decision.decision_time
    return datetime.now(timezone.utc)


def decision_label(as_of: Any) -> date:
    """The live session label of a decision at *as_of*: which day's 09:30 ET decision it IS.

    THE function both paths use to decide which macro rows a decision may see (a row is
    visible iff its first release is strictly before this label):

      * ``None`` -> LIVE: the New York date of the live decision instant
        (``market_calendar.live_decision_label``).
      * a DAILY backtest bar -- a stamp at exactly 00:00 UTC, or a plain date / date string --
        -> the next regular session N(D) (``market_calendar.backtest_decision_label``'s rule).
        Bar D decides on D's close and fills at N(D)'s open, so it is the live decision of
        N(D). A stamp on a non-session day (a tool's date, a vendor bar on a closure) maps by
        the same rule to the first session after it -- the decision that data would feed --
        rather than failing a whole run over a macro overlay.
      * any other instant (an intraday backtest bar, a recorded live decision time) -> its New
        York date, as live. The engine's intraday stamps are New York wall-clock times labelled
        UTC (09:30-15:55), which convert to the same New York date.

    A naive datetime / string is read as UTC, the platform's backtest clock convention.
    """
    from ba2_common.core.market_calendar import live_decision_label, next_regular_session

    if as_of is None:
        return live_decision_label(_live_decision_instant())
    ts = pd.Timestamp(as_of)
    if ts is pd.NaT:
        raise ValueError(f"as_of {as_of!r} is not a date")
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    if ts == ts.normalize():
        return next_regular_session(ts.date())
    return live_decision_label(ts.to_pydatetime())


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
      * ``known``  -- the date each surviving row became public: its first-release
        ``realtime_start`` for a first-release series, the observation date for the
        same-day series (``_spec(sid)`` decides).
      * ``values`` -- the parsed floats, aligned with ``index``.
      * ``deferred_known`` / ``deferred_exc`` -- see SKIP SEMANTICS.

    SKIP SEMANTICS, reproduced exactly. The original loop dropped a row when the
    date parse raised KeyError/ValueError, and -- separately -- when
    ``float(row["value"])`` raised TypeError/ValueError; note it appended the
    VALUE first, so a bad value skipped the row entirely rather than leaving the
    two lists misaligned. Both drops are unconditional (a dropped row is dropped
    for every cut), so they happen here.

    EXCEPT a first-release row whose ``realtime_start`` is missing or unreadable:
    its availability is unknown, so it is not dropped (the series would quietly
    lose a row a live run has) -- the parse refuses (``MacroAvailabilityUnknown``).

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

    def __init__(self, rows: List[dict], first_release: bool, sid: str = "") -> None:
        dates: List[pd.Timestamp] = []
        known: List[pd.Timestamp] = []
        values: List[float] = []
        deferred: List[pd.Timestamp] = []
        deferred_exc: Optional[KeyError] = None
        for row in rows:
            try:
                obs_date = pd.Timestamp(row["date"])
            except (KeyError, ValueError):
                continue
            if first_release:
                try:
                    known_on = pd.Timestamp(row["realtime_start"])
                except (KeyError, ValueError, TypeError):
                    known_on = pd.NaT
                if known_on is pd.NaT:
                    raise MacroAvailabilityUnknown(
                        f"FRED {sid} row {row.get('date')!r} carries no first-release date "
                        f"(realtime_start={row.get('realtime_start')!r}); when it became public "
                        f"is unknown. Re-warm the series (tools/refresh_fred_cache.py).")
            else:
                known_on = obs_date
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
        # Same-day rows are known on their observation date -- the same objects, so the index
        # is aliased rather than rebuilt.
        self.known = pd.DatetimeIndex(known) if first_release else self.index
        self.values = np.asarray(values, dtype="float64")
        self.deferred_known = pd.DatetimeIndex(deferred) if deferred else None
        self.deferred_exc = deferred_exc


def _parsed(sid: str, rows: List[dict], first_release: bool) -> _ParsedSeries:
    """The parse of *rows*, built once and served only back to that same object."""
    entry = _PARSED.get(sid)
    if entry is not None and entry[0] is rows:
        return entry[1]
    parsed = _ParsedSeries(rows, first_release, sid)
    _PARSED[sid] = (rows, parsed)
    return parsed


def series_identity(args):
    """What makes a point-in-time FRED read what it is: the series and the cut.

    Named (not an inline lambda) because the offline replay tape imports it to
    look a recorded series up by exactly the identity the tap wrote. ``as_of`` is
    taken AS THE CALLER PASSED IT (``None`` on the live path, which means "what a
    decision made now may see"): normalizing it here would build a key the caller
    cannot reproduce.
    """
    return {"series_id": args["series_id"], "as_of": args["as_of"]}


def _require_first_release_coverage(sid: str, meta: Optional[Dict[str, Any]],
                                    label: date) -> None:
    """Refuse a decision whose macro availability this payload cannot establish."""
    if not _is_first_release_payload(meta):
        raise MacroAvailabilityUnknown(
            f"FRED {sid}: the cache is not in the first-release format "
            f"({CACHE_FORMAT_FIRST_RELEASE}); its rows carry no publication dates, so what a "
            f"decision could see is unknown. Re-warm it with tools/refresh_fred_cache.py "
            f"--series {sid} and sync <CACHE_FOLDER>/fred/{sid}.json to every host.")
    first = meta.get("first_vintage")
    try:
        first_day = date.fromisoformat(str(first))
    except ValueError:
        raise MacroAvailabilityUnknown(
            f"FRED {sid}: the cache records no first vintage ({first!r}); re-warm it.") from None
    if label <= first_day:
        raise MacroAvailabilityUnknown(
            f"FRED {sid}: a decision labelled {label} is on or before the series' first "
            f"recorded vintage ({first_day}); nothing in it is provably public yet.")
    fetched = _fetched_day(meta)
    if fetched is None or fetched < label:
        raise MacroAvailabilityUnknown(
            f"FRED {sid}: the cache was fetched on {fetched} (New York date), before the "
            f"decision label {label}; vintages published since are unknown. Refresh it "
            f"(tools/refresh_fred_cache.py) -- live refetches automatically and failed.")


@observe_provider("macro", "get_series_as_of", identity=series_identity,
                  provenance=ReplayStatus.PROVENANCE_DISK_CACHE)
def get_series_as_of(series_id: str, as_of: Optional[datetime]) -> pd.Series:
    """Return the series as a decision at *as_of* could see it, indexed by observation date.

    Recorded at this boundary (provenance ``disk_cache``): this function NEVER
    reaches the network on the backtest path -- it reads the synced cache file (or the
    in-process memo of it) and raises when the series was not warmed -- so every return
    here came off disk by construction.

    FIRST-RELEASE series (every macro signal input): rows whose first publication on FRED is
    strictly before ``decision_label(as_of)``. ``as_of=None`` is the live decision made now;
    a daily backtest bar D is the live decision of N(D). The SAME rule, the SAME function,
    for both -- see the module docstring. Refuses (:class:`MacroAvailabilityUnknown`) when the
    payload cannot establish availability for that label.

    SAME-DAY series (DGS3MO, the option BS rate only): cut on the observation date
    (``as_of=None`` -> every row), as it always was.
    """
    sid = series_id.upper()
    # Spec BEFORE load: an unknown series is a coding error and must say so, not surface
    # as a confusing "not in the cache" that sends you looking for a prewarm problem.
    first_release = _spec(sid)["availability"] == AVAIL_FIRST_RELEASE
    label = decision_label(as_of) if first_release else None
    try:
        rows = _load(sid)
    except FileNotFoundError as e:
        if not first_release:
            raise
        # NOT an OSError out of here (review 2026-09-26, I2): the expert's broad handler
        # absorbs the OSError family as benign, which turned a missing macro file into a
        # trend-only regime with a WARNING -- in a backtest as in live.
        raise MacroAvailabilityUnknown(f"FRED {sid}: {e}") from e
    if first_release:
        _require_first_release_coverage(sid, _META.get(sid), label)
    # Parse once per payload (see _ParsedSeries); this call is then a filter + build.
    parsed = _parsed(sid, rows, first_release)

    if first_release:
        cut = pd.Timestamp(label)
        if parsed.deferred_known is not None and bool((parsed.deferred_known < cut).any()):
            raise parsed.deferred_exc
        keep = np.asarray(parsed.known < cut)
    else:
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
