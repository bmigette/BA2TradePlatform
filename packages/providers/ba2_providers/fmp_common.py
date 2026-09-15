"""Shared helpers for Financial Modeling Prep (FMP) data providers.

FMP returns rate-limit / API errors as **HTTP 200 with a JSON dict body** such
as ``{"Error Message": "Limit Reach."}`` instead of a proper error status code.
Providers that assume a list then crash when they slice/index the dict
(``unhashable type: 'slice'`` or ``KeyError: 0``).

``fmp_list_call`` wraps an FMP call and guarantees a list result:

* list           -> returned as-is
* ``None`` / ``[]`` -> ``[]`` (legitimate "no data")
* error dict     -> retried with backoff, then ``FMPError`` (raw payload logged)
* unexpected dict -> ``FMPError`` immediately (raw payload logged, no retry)
"""

import contextvars
import os as _os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

import requests

from ba2_common.logger import logger


# --- backtest cache freeze -------------------------------------------------
# When frozen, every TTLCache treats already-stored entries as NON-expiring for the
# duration. A long backtest (wall-clock longer than the 15-min FMP TTL) would otherwise
# let an entry expire mid-run and re-fetch the SAME per-symbol full-history payload many
# times. Each FMP cache is keyed by symbol and stores the full history; the no-lookahead
# as_of filtering runs after the fetch, so reusing one fetch across all as_of dates is
# correct. The LIVE path never enters frozen mode, so it keeps normal TTL expiry.
# THREAD-LOCAL freeze/hermetic state. These were module GLOBALS, which is unsafe when several
# backtests run as THREADS in one process (e.g. the re-run worker pool / the main task queue):
# one run's ``frozen_ttl_cache()``/``hermetic_fmp_history()`` exit would reset the flag to False
# WHILE a concurrent run was still going, dropping it out of frozen/hermetic mode mid-run — so its
# ``fmp_history_disk_cached`` calls silently became LIVE passthroughs (thousands of network fetches,
# minutes of grind, non-hermetic). Per-thread state isolates concurrent runs. The GA's PROCESS pool
# was unaffected (separate globals per process); this generalises the guarantee to threaded runs.
_tls = threading.local()


def _is_ttl_frozen() -> bool:
    return getattr(_tls, "ttl_frozen", False)


def _is_hermetic_fmp_history() -> bool:
    return getattr(_tls, "hermetic_fmp_history", False)


def set_ttl_frozen(frozen: bool) -> None:
    """Set the (thread-local) TTLCache freeze flag. Prefer the ``frozen_ttl_cache()`` context
    manager; this setter exists for explicit teardown in tests."""
    _tls.ttl_frozen = bool(frozen)


@contextmanager
def frozen_ttl_cache():
    """Within this context every ``TTLCache`` entry is non-expiring (one fetch per key for
    the whole backtest). Per-thread + restores the prior flag on exit (re-entrant + concurrency
    safe — a sibling thread's run never clobbers this thread's freeze state)."""
    prev = _is_ttl_frozen()
    _tls.ttl_frozen = True
    try:
        yield
    finally:
        _tls.ttl_frozen = prev


# --- hermetic FMP-history (backtest) ---------------------------------------
# A backtest must run from PRE-WARMED caches only — ZERO network fetches. OHLCV already enforces
# this (BacktestCacheMiss). When hermetic is on, ``fmp_history_disk_cached`` serves an existing
# per-symbol file (ignoring age — historical data doesn't go stale) and RAISES on a miss instead
# of silently network-fetching mid-run. Prewarm / fetch-cache run WITHOUT hermetic so they can
# populate the cache. Separate from the freeze flag because prewarm also freezes (to write disk).
class FMPHistoryCacheMiss(RuntimeError):
    """A per-symbol FMP history was absent from the cache during a hermetic backtest.

    Means a required dataset wasn't pre-warmed. The run aborts loudly (no live fetch) so the
    gap is fixed via ``ba2-test prewarm`` rather than silently network-fetching / skipping.
    """


@contextmanager
def hermetic_fmp_history():
    """Within this context ``fmp_history_disk_cached`` never network-fetches: a cache miss raises
    ``FMPHistoryCacheMiss``. The backtest run enters this; prewarm/fetch-cache do NOT. Per-thread
    (concurrency safe — a sibling backtest thread never drops this thread out of hermetic mode)."""
    prev = _is_hermetic_fmp_history()
    _tls.hermetic_fmp_history = True
    try:
        yield
    finally:
        _tls.hermetic_fmp_history = prev


# --- persist-empty sentinel (prewarm) --------------------------------------
# Normally a falsy FMP payload (None/[]/{}) is NOT cached (it could be a transient hiccup, and we
# want a retry next run). But that makes a symbol FMP genuinely has NO data for (a baby bond /
# preferred / note with no earnings, e.g. BNH) indistinguishable from one that was never warmed:
# both leave NO file, so the next hermetic read reports "not pre-warmed". When this flag is on
# (PREWARM only — fmp_list_call RAISES on real FMP errors, so a falsy result here is a genuine
# "no data"), a genuine empty IS persisted as an EMPTY-LIST SENTINEL ``[]``. Then a sentinel file
# means "checked, FMP has nothing" (no signal, NO error) while an ABSENT file still means "never
# warmed" -> fatal in a hermetic backtest. This keeps fail-loud for real prewarm gaps.
_PERSIST_EMPTY_SENTINEL = False


# --- hermetic miss policy --------------------------------------------------
# A missing per-symbol history used to raise, which FAILED THE WHOLE TRIAL. Measured on the
# goal2020 grid: ONE symbol (OP, never prewarmed) killed 77 trials across three jobs, and because
# a failed trial is scored at the always-worst sentinel, the GA was taught to avoid every screener
# region containing that symbol -- a selection bias produced by a missing file.
#
# So the policy is two-tier, and the tiers answer different questions:
#
#   1 symbol missing  -> a data gap for ONE instrument. Skip it (empty history = no signal, the
#                        same thing an empty-sentinel file means) and let the trial stand. Logged
#                        ONCE per symbol so it is visible without flooding.
#   N symbols missing -> the PREWARM ITSELF is broken. Every trial is now scoring a crippled
#                        universe, so finishing 8 generations produces a confidently wrong answer.
#                        Raise: _trial_worker marks FMPHistoryCacheMiss fatal, and the handler
#                        aborts the job immediately.
#
# Counted in DISTINCT SYMBOLS, not miss events: one symbol missed 77 times is one gap, whereas ten
# different symbols is a systematically incomplete cache. Per-process (each trial worker is its own
# process), which is the right scope -- the question is whether THIS run's cache is sound.
_HERMETIC_MISS_LIMIT = int(_os.environ.get("BA2_PREWARM_MISS_LIMIT") or 10)
_hermetic_misses: set = set()


def hermetic_miss_symbols() -> set:
    """Distinct ``namespace/symbol`` histories skipped this process (diagnostics/tests)."""
    return set(_hermetic_misses)


def reset_hermetic_misses() -> None:
    """Clear the miss registry AND the memoized empties it produced, between runs.

    Both halves matter. ``_record_hermetic_miss`` returns ``[]``, which the in-process history memo
    then caches — so on a second run the memo would serve that empty WITHOUT going through the
    recorder, the symbol would never be re-registered, and the abort counter would under-count a
    cache that is still just as broken. Dropping the memo entries keeps the registry an honest
    picture of THIS run.
    """
    for key in _hermetic_misses:
        ns, _, sym = key.partition("/")
        _HISTORY_MEM_CACHE.invalidate(f"{ns}__{sym.upper()}")
    _hermetic_misses.clear()


def _record_hermetic_miss(namespace: str, symbol: str):
    """Register a hermetic cache miss, then skip the symbol -- or abort once too many are missing."""
    import logging as _logging

    key = f"{namespace}/{symbol}"
    first_time = key not in _hermetic_misses
    _hermetic_misses.add(key)

    if len(_hermetic_misses) >= _HERMETIC_MISS_LIMIT:
        missing = ", ".join(sorted(_hermetic_misses)[:20])
        raise FMPHistoryCacheMiss(
            f"{len(_hermetic_misses)} distinct fmp_history datasets are not pre-warmed "
            f"(limit {_HERMETIC_MISS_LIMIT}) — the cache is incomplete, so every trial is scoring "
            f"a crippled universe. Aborting instead of finishing the search. Missing: {missing}. "
            f"Run `ba2-test prewarm` for this universe, then re-run."
        )

    if first_time:
        # WARNING so it survives the optimizer's logging.disable(INFO); once per symbol per
        # process, so a persistent gap is visible without one line per bar.
        _logging.getLogger(__name__).warning(
            "fmp_history '%s' not pre-warmed — DISABLING this symbol for the run (%d/%d before "
            "abort). Run `ba2-test prewarm` to close the gap.",
            key, len(_hermetic_misses), _HERMETIC_MISS_LIMIT)
    return []


@contextmanager
def persist_empty_sentinel():
    """Within this context a genuinely-empty FMP history is cached as ``[]`` (prewarm sentinel).
    Used by ``ba2-test prewarm`` so no-data symbols don't perpetually look 'not pre-warmed'.

    PROCESS-WIDE on purpose, and it has to stay that way: ``run_prewarm`` enters this on the
    SUBMITTING thread and relies on the flag reaching its ``ThreadPoolExecutor`` workers (the
    freeze flag next to it is thread-local, which is exactly why that one needs an
    ``initializer``). Anything that must NOT reach sibling threads uses
    :func:`thread_persist_empty_sentinel` instead.
    """
    global _PERSIST_EMPTY_SENTINEL
    prev = _PERSIST_EMPTY_SENTINEL
    _PERSIST_EMPTY_SENTINEL = True
    try:
        yield
    finally:
        _PERSIST_EMPTY_SENTINEL = prev


@contextmanager
def thread_persist_empty_sentinel(enabled: bool = True):
    """Sentinel semantics for THIS THREAD ONLY, overriding the process-wide flag.

    The warm worker runs inside the LIVE trading process, where the process-global
    :func:`persist_empty_sentinel` would be a shared mutation: a concurrent backtest thread
    (frozen, so it does reach the persist branch) would start writing ``[]`` sentinels it never
    asked for, and "checked, FMP has nothing" is a claim only a deliberate warm may make. Spec
    section 9: "process-global empty-sentinel flags must not leak into concurrent live work."

    The override is consulted BEFORE the global (see ``_persist_empty_sentinel_enabled``), so a
    warm thread can also turn the sentinel OFF inside a process that turned it on.
    """
    prev = getattr(_tls, "persist_empty_sentinel", None)
    _tls.persist_empty_sentinel = bool(enabled)
    try:
        yield
    finally:
        _tls.persist_empty_sentinel = prev


def _persist_empty_sentinel_enabled() -> bool:
    """Whether a genuine empty is persisted as the ``[]`` sentinel right now.

    This thread's explicit override wins; otherwise the process-wide prewarm flag.
    """
    override = getattr(_tls, "persist_empty_sentinel", None)
    if override is not None:
        return override
    return _PERSIST_EMPTY_SENTINEL


# --- live-only short-TTL cache for SETTINGS-INDEPENDENT bulk fetches --------
# The mirror image of fmp_history_disk_cached below: that one is BACKTEST-only (gated ON the
# freeze flag) and long-lived on disk; this one is LIVE-only (gated OFF it) and short-lived in
# memory. Together they cover both paths without either interfering with the other.
#
# Why (2026-08-06): a `screener` instrument-selection expert runs StockScreener on EVERY analysis
# cycle, and each run pulls the whole market before evaluating a single threshold —
# available-traded/list, the PAGINATED delisted-companies list, then quote + historical-price-full
# for the entire universe in chunks. With 16 such live instances (8 FMPRating, 4
# FMPEarningsDrift, 4 FMPInsiderClusterBuy) that is the same multi-thousand-call sweep repeated
# per instance per cycle. It exhausted the FMP daily quota on 2026-08-05 22:57 and still returns
# 429s on available-traded / delisted-companies.
#
# It is SAFE to share those payloads across instances because NONE of the screener settings reach
# FMP: the queries carry only {apikey}, {apikey,page} or {apikey,from,to}. market_cap_min,
# relative_volume_min, weinstein_stage2_only, price_drop_* and max_stocks are all applied AFTER
# the fetch, in local filtering. So every instance is asking for identical data and filtering it
# differently — exactly the shape a shared cache is for.
_FMP_LIVE_BULK_TTL_S = 6 * 3600.0     # market-structure lists: change at most daily
_FMP_LIVE_QUOTE_TTL_S = 900.0         # quotes: 15 min, matching the existing FMP TTL convention
# ONE CACHE PER DISTINCT TTL, keyed by ttl_seconds. TTLCache fixes its expiry window at
# CONSTRUCTION (``self._clock() + self._ttl``), so a single shared instance would hand every
# caller whichever TTL happened to construct it first. In a screen the 6h lifecycle map is
# fetched BEFORE the quotes, so one shared cache would have pinned live quotes for six hours.
_LIVE_BULK_CACHES: dict = {}          # built lazily so the TTLCache class below is defined first
_LIVE_BULK_CACHES_LOCK = threading.Lock()


def fmp_live_cached(key: str, fetch_fn: Callable[[], Any],
                    ttl_seconds: float = _FMP_LIVE_BULK_TTL_S) -> Any:
    """Memoize a settings-INDEPENDENT live fetch for *ttl_seconds*, shared across callers.

    Passthrough in a frozen (backtest) run: backtests must keep taking the hermetic disk path,
    and a frozen TTLCache never expires, which would pin a live-shaped payload for the whole run.

    ``key`` must capture everything that varies the RESPONSE (endpoint + symbols + date window)
    and nothing that varies only the caller's later filtering.
    """
    if _is_ttl_frozen():
        return fetch_fn()
    return _live_bulk_cache(ttl_seconds).get_or_call(key, fetch_fn)


#: Returned by ``fmp_live_cache_get`` when a key is absent. A sentinel rather than
#: ``None`` because ``None`` is a legitimate cached value for a symbol the provider
#: has nothing for, and treating that as a miss re-fetches it on every screen.
FMP_LIVE_CACHE_MISS = object()


def _live_bulk_cache(ttl_seconds: float):
    """The shared TTLCache for one TTL, created on first use. Internal."""
    with _LIVE_BULK_CACHES_LOCK:
        cache = _LIVE_BULK_CACHES.get(ttl_seconds)
        if cache is None:
            cache = _LIVE_BULK_CACHES[ttl_seconds] = TTLCache(ttl_seconds)
    return cache


def fmp_live_cache_enabled() -> bool:
    """Whether the live memo is in play at all -- i.e. this is NOT a frozen backtest.

    Exposed so a caller can decide whether a caching-shaped strategy is worth adopting.
    The screener widens its fetch window to share one payload across passes, which pays
    for itself only if the payload can be reused; in a frozen run the memo is inert, so
    the wider window would be extra bytes bought for nothing.
    """
    return not _is_ttl_frozen()


def fmp_live_cache_get(key: str, ttl_seconds: float = _FMP_LIVE_BULK_TTL_S):
    """Read a live-cache entry WITHOUT fetching. ``FMP_LIVE_CACHE_MISS`` when absent.

    ``fmp_live_cached`` fetches on a miss, which is right for one payload and wrong
    for a BATCHED endpoint: to keep batching you must first know which members are
    missing, and asking through get_or_call would fetch them one at a time. This is
    the read half of that; ``fmp_live_cache_put`` is the write half.

    A frozen (backtest) run always reports a miss, so the hermetic disk path is
    reached exactly as before and nothing a backtest does is served from a live memo.
    """
    if _is_ttl_frozen():
        return FMP_LIVE_CACHE_MISS
    cache = _live_bulk_cache(ttl_seconds)
    with cache._lock:                                    # noqa: SLF001 - same module
        item = cache._store.get(key)                     # noqa: SLF001
        if item is not None and cache._clock() < item[1]:  # noqa: SLF001
            return item[0]
    return FMP_LIVE_CACHE_MISS


def fmp_live_cache_put(key: str, value, ttl_seconds: float = _FMP_LIVE_BULK_TTL_S) -> None:
    """Store one entry a batched fetch produced. No-op in a frozen (backtest) run."""
    if _is_ttl_frozen():
        return
    cache = _live_bulk_cache(ttl_seconds)
    with cache._lock:                                    # noqa: SLF001 - same module
        cache._store[key] = (value, cache._clock() + cache._ttl)   # noqa: SLF001


# --- backtest-only disk cache for per-symbol FMP history payloads -----------
# A spawned GA optimization worker pool starts each worker with EMPTY module-level TTLCaches,
# so every fresh worker re-fetches the same per-symbol full-history payloads from FMP — the
# dominant cost of an optimization grid (~26s/symbol; minutes per fresh worker). These
# histories are time-invariant PAST data, so we persist them to disk (keyed by symbol): every
# worker/trial then reads from disk instead of the network. Gated on the freeze flag so it is
# BACKTEST-ONLY — the live analysis path (never frozen) always pulls fresh from the FMP API.
_FMP_HISTORY_DISK_MAX_AGE_DAYS = 7.0


def _fmp_history_cache_dir() -> str:
    import os as _os
    import ba2_common.config as _cfg  # read at call time so tests that rebind CACHE_FOLDER win
    return _os.path.join(_cfg.CACHE_FOLDER, "fmp_history")


def fmp_history_disk_cached(namespace: str, symbol: str, fetch_fn: Callable[[], Any],
                            max_age_days: float = _FMP_HISTORY_DISK_MAX_AGE_DAYS,
                            *, retain: bool = True) -> Any:
    """Disk-persist a per-symbol FMP *history* payload so spawned backtest workers read it from
    disk instead of re-fetching from FMP.

    BACKTEST-ONLY: when the TTL freeze flag is NOT set (the live path) this is a straight
    passthrough to ``fetch_fn`` — live analysis always hits the live API. Keyed by
    ``(namespace, symbol)`` as JSON under ``CACHE_FOLDER/fmp_history``; reused if younger than
    ``max_age_days`` (past-data histories rarely change; a week balances reuse vs picking up
    newly-published rows). Best-effort: any disk error falls back to a live ``fetch_fn`` so a
    cache problem can never break a run. The atomic tmp+replace write means concurrent workers
    never read a half-written file.

    ``retain=False`` takes the payload WITHOUT pinning it in the in-process memo below. For a
    caller that reads a history once and immediately projects it down to something small, the
    memo is pure cost: measured 2026-09-05 with tracemalloc on one FMPSenateTraderWeight trial,
    ``json/decoder.py`` held 2,465 MB in 44.6M live objects and was still climbing linearly past
    the (flat, 2,145 MB) bar cache -- decoded ``historical_price_full`` payloads for the ~1,800
    tickers the Senate feed discloses, every one of them already reduced to a
    ``{date: open}`` map by its caller. An entry ALREADY memoized is still served from the memo:
    the flag declines to ADD, it never bypasses a hit, so mixing retaining and non-retaining
    callers of the same key cannot produce two different objects.
    """
    if not _is_ttl_frozen():
        return fetch_fn()  # live path: never cache to disk; always pull fresh from the API
    # BACKTEST in-process layer: hold the loaded payload in memory for the (frozen) run so each
    # (namespace, symbol) is read+parsed from disk ONCE per worker, not once per analysis bar.
    # Without this, per-bar experts (insider/earnings/senate/finnhub) re-`json.load`ed the whole
    # per-symbol history every bar — the dominant backtest bottleneck (FMPRating sidestepped it
    # with its own TTLCache; this generalises that to every disk-cached history). Returns the SAME
    # object across calls, so callers' per-row date memoization (e.g. ``_pd``/``_td_memo``) sticks.
    key = f"{namespace}__{symbol.upper()}"
    if not retain:
        hit = _HISTORY_MEM_CACHE.peek(key)
        if hit is not _MISSING:
            return hit
        return _fmp_history_disk_read_or_fetch(namespace, symbol, fetch_fn, max_age_days)
    return _HISTORY_MEM_CACHE.get_or_call(
        key,
        lambda: _fmp_history_disk_read_or_fetch(namespace, symbol, fetch_fn, max_age_days),
    )


def _fmp_history_disk_read_or_fetch(namespace: str, symbol: str, fetch_fn: Callable[[], Any],
                                    max_age_days: float) -> Any:
    """Disk read (if fresh) else fetch + persist — the original ``fmp_history_disk_cached`` body,
    now invoked once per (namespace, symbol) per process via the in-process cache above."""
    import json as _json
    import os as _os
    import time as _time

    d = _fmp_history_cache_dir()
    path = _os.path.join(d, f"{namespace}__{symbol.upper()}.json")

    # 1. Disk read (best-effort). A corrupt/unreadable/stale file falls through to a fresh fetch
    #    rather than being served — EXCEPT in hermetic mode, where age is ignored (historical data
    #    doesn't go stale) and a miss raises instead of fetching.
    try:
        if _os.path.exists(path) and (_is_hermetic_fmp_history()
                                      or (_time.time() - _os.path.getmtime(path)) / 86400.0 <= max_age_days):
            with open(path, "r") as fh:
                return _json.load(fh)
    except Exception as e:  # corrupt / partial / unreadable -> re-fetch (or raise, hermetic)
        # NAMED, at WARNING. A silently unreadable cache file re-downloads its payload on
        # every single read, forever, and the only symptom is a provider bill.
        logger.warning(
            f"fmp_history cache file {path} could not be read ({type(e).__name__}: {e}); "
            f"re-fetching it")

    # HERMETIC backtest: NEVER network-fetch — a miss means the data wasn't pre-warmed.
    # ONE missing symbol DISABLES THAT SYMBOL; MANY abort the run. See _record_hermetic_miss.
    if _is_hermetic_fmp_history():
        return _record_hermetic_miss(namespace, symbol)

    # 2. Fetch. Any error PROPAGATES to the caller and is NEVER cached, so the failure is
    #    retried on the next run instead of poisoning the cache with a bad value.
    data = fetch_fn()

    # 3. Persist (best-effort). Normally ONLY a non-empty result is cached — a falsy payload
    #    (None/[]/{}) is left uncached so it's retried next run (and, in hermetic mode, surfaces as
    #    a clear "not pre-warmed" miss). EXCEPTION: under ``persist_empty_sentinel()`` (PREWARM), a
    #    genuine empty is persisted as an EMPTY-LIST SENTINEL ``[]`` — fmp_list_call RAISES on real
    #    FMP errors, so a falsy result here is a true "FMP has no data for this symbol". A sentinel
    #    file then means "checked, no data" (no signal) while an ABSENT file still means "never
    #    warmed" (fatal in a hermetic backtest), so no-data instruments stop looking like prewarm
    #    gaps. The atomic tmp+replace means a concurrent reader never sees a half-written file.
    to_persist = data if data else ([] if _persist_empty_sentinel_enabled() else None)
    if to_persist is not None:
        tmp = None
        try:
            _os.makedirs(d, exist_ok=True)
            tmp = f"{path}.{_os.getpid()}.tmp"
            with open(tmp, "w") as fh:
                _json.dump(to_persist, fh)
            _os.replace(tmp, path)  # atomic
        except Exception as e:
            logger.warning(
                f"fmp_history cache file {path} could not be written "
                f"({type(e).__name__}: {e}); this payload will be re-fetched next run")
            if tmp:
                try:
                    _os.remove(tmp)  # never leave a half-written tmp behind
                except OSError as cleanup_error:
                    logger.warning(f"could not remove the temp file {tmp}: {cleanup_error}")
    return data


#: Sentinel for "no entry", distinct from a cached ``None``. See ``TTLCache.peek``.
_MISSING = object()


class TTLCache:
    """Tiny thread-safe time-to-live cache to dedupe identical fetches across callers.

    ``get_or_call(key, fn)`` returns the cached value if present and unexpired,
    otherwise calls ``fn()`` (outside the lock), caches the result — including
    ``None`` — and returns it. Intended to collapse the many redundant FMP calls
    that multiple experts make for the same symbol within a short window.

    When the module-level freeze flag is set (``frozen_ttl_cache()``, the backtest path),
    a present entry is returned regardless of its expiry — so a multi-hour backtest fetches
    each key once instead of re-fetching every 15 minutes.
    """

    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.time):
        self._ttl = ttl_seconds
        self._clock = clock
        self._store: dict = {}
        self._lock = threading.Lock()

    def peek(self, key) -> Any:
        """The cached value, or ``_MISSING`` -- NEVER calls ``fn``, never stores.

        For ``retain=False``: a value someone else already memoized is free to reuse, but this
        caller must not be the one that puts it there. ``_MISSING`` rather than ``None`` because
        ``None`` is a legitimate cached value here (``get_or_call`` caches it deliberately).
        """
        with self._lock:
            item = self._store.get(key)
            if item is not None and (_is_ttl_frozen() or self._clock() < item[1]):
                return item[0]
        return _MISSING

    def invalidate(self, key) -> None:
        """Drop one entry. Used to un-memoize a hermetic cache-miss empty (see
        reset_hermetic_misses) so a later run re-evaluates it instead of inheriting the empty."""
        with self._lock:
            self._store.pop(key, None)

    def get_or_call(self, key, fn: Callable[[], Any]) -> Any:
        with self._lock:
            item = self._store.get(key)
            if item is not None and (_is_ttl_frozen() or self._clock() < item[1]):
                return item[0]
        value = fn()  # network call outside the lock
        with self._lock:
            self._store[key] = (value, self._clock() + self._ttl)
        return value


# In-process layer for fmp_history_disk_cached (defined above; resolved at call time). TTL is
# irrelevant — it's only consulted on the frozen (backtest) path, where entries never expire — so
# any value works; reuse the disk freshness window. Thread-safe (used by the parallel prewarm).
_HISTORY_MEM_CACHE = TTLCache(_FMP_HISTORY_DISK_MAX_AGE_DAYS * 86400.0)


# FMP error responses use one of these keys in a 200-status JSON dict.
_FMP_ERROR_KEYS = ("Error Message", "error", "message")


class FMPError(RuntimeError):
    """Raised when an FMP call returns an error/unexpected payload (after retries)."""


class FMPHermeticViolation(RuntimeError):
    """A backtest tried to reach the FMP network. The hermetic contract forbids it.

    Deliberately NOT an ``FMPError`` subclass: several providers wrap FMP calls in
    ``except FMPError`` and degrade to an empty result, which would turn a contract breach back
    into the silent behaviour this exists to stop.

    WHY THIS GUARD IS AT ``fmp_http_get`` AND NOT PER-CALLER (2026-08-06)
    --------------------------------------------------------------------
    ``daily_backtest_handler`` documents the contract as "a backtest must run from PRE-WARMED
    caches only (0 network fetches)". But ``hermetic_fmp_history()`` was only ever consulted
    inside ``_fmp_history_disk_read_or_fetch`` -- i.e. it protected the ONE path that already had
    a disk cache. NINE provider modules call ``fmp_http_get`` directly and none was covered, so
    the "guarantee" held only for callers that had already opted in.

    What that cost: FactorRanker with ``universe_source=screener`` and no ``screener_store`` falls
    back to the live ``StockScreener``, which fetches per-symbol history for the whole universe.
    Every trial worker of opt 255 sat blocked in ``_gate_wait`` for EIGHT HOURS with zero trials
    completed, and the same job on 2026-08-05 exhausted the FMP daily quota at 22:57. Neither was
    attributable from the cache (these calls never write ``fmp_history``) nor from the logs.

    Enforcing at the single choke point makes the contract structural: a backtest cannot reach the
    network by any route, and a breach fails LOUDLY and IMMEDIATELY, naming the endpoint, instead
    of grinding for hours behind a rate limiter.
    """


def _assert_not_hermetic(fn_name: str, endpoint: str, symbol: str = "") -> None:
    """Refuse an outbound FMP call while a hermetic (backtest) run is active.

    The single choke point both ``fmp_http_get`` and ``fmp_list_call`` pass through, so the
    "0 network fetches" contract holds for EVERY provider rather than only the ones that route
    through ``fmp_history_disk_cached``. See ``FMPHermeticViolation`` for what the gap cost.

    ``BA2_HERMETIC_ALLOW_NETWORK=1`` re-opens the network for a deliberate diagnostic run (e.g.
    comparing the live screener against the store). It is an escape hatch, not a setting: a grid
    must never be run with it on, or the stall it prevents comes straight back.
    """
    if not _is_hermetic_fmp_history():
        return
    import os as _os
    if _os.environ.get("BA2_HERMETIC_ALLOW_NETWORK") == "1":
        return
    raise FMPHermeticViolation(
        f"HERMETIC CONTRACT BREACH: {fn_name} tried to reach FMP endpoint "
        f"'{endpoint or '?'}'{f' for {symbol}' if symbol else ''} during a backtest. A backtest "
        f"must read from pre-warmed caches only. Either pre-warm this data, or point the caller "
        f"at its cached equivalent (e.g. set FactorRanker's `screener_store` so the universe "
        f"resolves from the metric_store instead of the live StockScreener). "
        f"Set BA2_HERMETIC_ALLOW_NETWORK=1 ONLY for a deliberate diagnostic run."
    )


def _fmp_error_message(payload: dict) -> Any:
    """Return the FMP error string from a dict payload, or None if not an error."""
    for key in _FMP_ERROR_KEYS:
        if key in payload and payload[key]:
            return payload[key]
    return None


def _parse_retry_after(value) -> Optional[float]:
    """Parse a Retry-After header value expressed in seconds; ignore HTTP-date form."""
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---- Global FMP rate-limit gate ---------------------------------------------------------------
# Per-request backoff alone STORMS under concurrency: every in-flight thread gets the same 429 and
# retries in lockstep, re-triggering the limit. The gate makes the backoff GLOBAL — a single 429
# arms a shared cooldown that ALL FMP requests (across threads) wait out before firing again, with
# small per-thread jitter so they don't resume as a thundering herd.
import threading as _threading

_GATE_LOCK = _threading.Lock()
_GATE_UNTIL = 0.0  # monotonic timestamp; no FMP request fires before this

# Injectable clock (tests monkeypatch this together with their fake ``sleep`` so the gate's
# remaining-time reads advance with the virtual sleeps; a wall-clock gate would busy-loop
# against a fake sleep that doesn't consume real time). Production: time.monotonic.
_now = time.monotonic


def _gate_wait(sleep: Callable[[float], None]) -> None:
    import random as _random
    while True:
        with _GATE_LOCK:
            remaining = _GATE_UNTIL - _now()
        if remaining <= 0:
            return
        # cap each wait slice (so a later, shorter gate is re-read) + jitter to stagger resume
        sleep(min(remaining, 2.0) + _random.uniform(0.0, 0.4))


def _gate_arm(delay: float) -> None:
    global _GATE_UNTIL
    with _GATE_LOCK:
        _GATE_UNTIL = max(_GATE_UNTIL, _now() + max(0.0, delay))


def gate_remaining_seconds() -> float:
    """How long the SHARED FMP cooldown still has to run (0.0 when it is not armed).

    Read-only; arming stays internal. Exposed for the warm budget, which must pause
    background work while a 429/5xx backoff is in force so the remaining allowance goes to
    live requests first (spec section 6, "Live requests retain priority").
    """
    with _GATE_LOCK:
        return max(0.0, _GATE_UNTIL - _now())


# ---- Request / byte accounting by purpose ------------------------------------------------------
# Spec section 6: "Track requests/bytes by endpoint and purpose: normal live, capture overhead
# (must be zero network), and warmup." Nothing measured this before, so "capture adds zero
# requests" and "warm stayed inside its allowance" were both unfalsifiable claims.
#
# The purpose is a ContextVar, not a thread-local, because the callers that need to tag their
# work fan out through ``ThreadPoolExecutor`` (prewarm) and ``capture_aware_submit`` (gather),
# both of which copy a ``contextvars.Context`` into the worker. A thread-local would have
# reported every pooled warm fetch as "live".
#: The purposes a request can be made for. ``capture`` must never appear with a non-zero count:
#: recording reads what live already fetched and issues no request of its own.
PURPOSE_LIVE = "live"
PURPOSE_CAPTURE = "capture"
PURPOSE_WARM = "warm"
PURPOSES = (PURPOSE_LIVE, PURPOSE_CAPTURE, PURPOSE_WARM)

_fmp_purpose: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "fmp_purpose", default=PURPOSE_LIVE)

#: ``{utc_day: {(purpose, endpoint): {"requests": int, "bytes": int}}}``. Kept per UTC day so a
#: daily allowance is measured against a day and an old day cannot silently consume today's.
_PURPOSE_STATS: dict = {}
_PURPOSE_LOCK = _threading.Lock()


def current_fmp_purpose() -> str:
    """What the requests made from this context are being made FOR."""
    return _fmp_purpose.get()


@contextmanager
def fmp_purpose(purpose: str):
    """Tag every FMP request made inside this context (and in contexts copied from it)."""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown FMP request purpose {purpose!r}; expected one of {PURPOSES}")
    token = _fmp_purpose.set(purpose)
    try:
        yield
    finally:
        _fmp_purpose.reset(token)


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


#: Where an attempt whose endpoint name failed :func:`validate_endpoint_key` is counted.
#: One fixed key, never the offending text: the whole reason the name is refused is that
#: a URL carries the symbol and, on some FMP paths, the API key.
MALFORMED_ENDPOINT_KEY = "malformed"


def validate_endpoint_key(endpoint: str) -> str:
    """The stripped endpoint NAME, or ``ValueError`` saying why it is not one.

    ``endpoint`` must be short. The counters are keyed by it, and a full URL -- which
    carries the symbol and, on some FMP paths, the API KEY -- would both explode the key
    space and put a credential in a log line. A caller that cannot name its endpoint has
    a bug, not a counting problem.

    THE RULE LIVES HERE, ON ITS OWN, so it can be stated strictly without a meter being
    able to fail a market-data fetch: :func:`record_fmp_request` runs on the live path,
    before the request and outside any try, and coerces a rejected name instead of
    raising it (see there). Anything that wants the rule enforced -- a test, a caller
    that builds a key ahead of time -- calls this.
    """
    name = (endpoint or "").strip()
    if not name:
        raise ValueError(
            "record_fmp_request needs a short endpoint name to key the counters by; pass the "
            "endpoint, never the URL (it carries the symbol and, on some paths, the api key)")
    if "?" in name or "://" in name or len(name) > _MAX_ENDPOINT_KEY:
        raise ValueError(
            f"endpoint {name[:40]!r}... does not look like an endpoint NAME; pass the short "
            f"path segment (e.g. 'price-target'), never a URL")
    return name


#: Fingerprints of the malformed endpoint values already warned about, so a mis-named
#: endpoint used on every fetch warns ONCE per process, not once per request.
_MALFORMED_WARNED: set = set()


def _warn_malformed_endpoint_once(endpoint: object) -> None:
    """One WARNING per distinct offending value, describing its SHAPE, never its text.

    The value is refused precisely because it may be a URL carrying the API key, so
    the log line must not quote it -- not even a prefix (the key sits at a different
    offset on every base URL). Length and the two tell-tale markers are enough to find
    the call site; the fingerprint lets two log lines be matched without the text.
    """
    import hashlib

    text = endpoint if isinstance(endpoint, str) else repr(endpoint)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    with _PURPOSE_LOCK:
        if digest in _MALFORMED_WARNED:
            return
        _MALFORMED_WARNED.add(digest)
    logger.warning(
        f"FMP request counter: an endpoint value is not an endpoint NAME (length {len(text)}, "
        f"query={'?' in text}, scheme={'://' in text}, fingerprint {digest}); pass the short "
        f"path segment, e.g. 'price-target', never a URL. The attempt is counted under "
        f"{MALFORMED_ENDPOINT_KEY!r}; further requests with this value are not logged.")


def _counter_key(endpoint: str) -> str:
    """``endpoint`` as a counter key, falling back to ``malformed``. Never raises."""
    try:
        return validate_endpoint_key(endpoint)
    except ValueError:
        return MALFORMED_ENDPOINT_KEY


def record_fmp_request(endpoint: str, nbytes: Optional[int] = None) -> None:
    """Count one FMP/FRED request ATTEMPT against (today, purpose, endpoint).

    WHAT IS COUNTED. Every attempt, including the ones that came back 429 or 5xx and were
    retried: a rate-limited attempt consumed a request slot at the provider, and a counter
    that hid it would under-report exactly the traffic that caused the throttling. BYTES are
    added only when a body actually arrived -- an attempt that failed transferred nothing to
    charge the allowance for.

    ``nbytes`` is ``None`` when the response cannot report a size (a stubbed getter in a test,
    a streamed body, an fmpsdk call whose payload is already decoded): the attempt is still
    counted and the byte total is left alone rather than padded with a guess. A budget that
    silently invents bytes is not a measurement.

    ``endpoint`` is REQUIRED and must satisfy :func:`validate_endpoint_key`. A name that does
    not is a WARNING and is counted under :data:`MALFORMED_ENDPOINT_KEY`, never raised: this
    runs on the live fetch path, before the request and outside any try (``fmp_http_get``,
    ``fmp_list_call``), so raising here turned a mis-named endpoint -- a logging defect -- into
    a failed market-data fetch. The attempt is real and stays counted; only the key it is
    charged to is lost, and the offending text never becomes a key (that is the point of the
    rule). Callers that want the rule enforced call the validator directly.
    """
    name = _counter_key(endpoint)
    if name == MALFORMED_ENDPOINT_KEY:
        _warn_malformed_endpoint_once(endpoint)
    day = _utc_day()
    key = (current_fmp_purpose(), name)
    with _PURPOSE_LOCK:
        # One day at a time: yesterday's counters are dropped as soon as a request lands on a
        # new day, which is the "reset per UTC day" the allowance is defined against.
        if day not in _PURPOSE_STATS:
            _PURPOSE_STATS.clear()
            _PURPOSE_STATS[day] = {}
        entry = _PURPOSE_STATS[day].setdefault(key, {"requests": 0, "bytes": 0})
        entry["requests"] += 1
        if nbytes:
            entry["bytes"] += int(nbytes)


#: Longest endpoint name the counters accept as a key (see ``record_fmp_request``).
_MAX_ENDPOINT_KEY = 64


def get_purpose_stats() -> dict:
    """Today's request/byte counters as ``{purpose: {"requests", "bytes", "endpoints": {...}}}``.

    A purpose with no requests today is absent rather than reported as zero-of-nothing; a caller
    that wants the full shape reads ``PURPOSES``.
    """
    day = _utc_day()
    out: dict = {}
    with _PURPOSE_LOCK:
        for (purpose, endpoint), entry in _PURPOSE_STATS.get(day, {}).items():
            bucket = out.setdefault(purpose, {"requests": 0, "bytes": 0, "endpoints": {}})
            bucket["requests"] += entry["requests"]
            bucket["bytes"] += entry["bytes"]
            bucket["endpoints"][endpoint] = dict(entry)
    return out


def reset_purpose_stats() -> None:
    """Drop every counter (tests, and an explicit operator reset)."""
    with _PURPOSE_LOCK:
        _PURPOSE_STATS.clear()


def record_fmp_bytes(endpoint: str, nbytes: Optional[int]) -> None:
    """Add transferred bytes to an attempt already counted by :func:`record_fmp_request`.

    Keyed through the SAME coercion as the request it belongs to, silently: the request
    already warned, and a URL that cannot be a request key must not become a byte key
    either (it carries the api key on some paths).
    """
    if not nbytes:
        return
    name = _counter_key(endpoint)
    day = _utc_day()
    key = (current_fmp_purpose(), name)
    with _PURPOSE_LOCK:
        entry = _PURPOSE_STATS.setdefault(day, {}).setdefault(key, {"requests": 0, "bytes": 0})
        entry["bytes"] += int(nbytes)


def _decoded_payload_bytes(payload) -> Optional[int]:
    """The size of an ALREADY-DECODED payload, as a stand-in for its wire bytes.

    ``fmpsdk`` hands back parsed JSON, so the response object -- and with it the true
    compressed transfer size -- is gone by the time this sees it. The serialized length is
    the honest available measure: same order of magnitude, systematically LARGER than the
    gzipped wire bytes, which is the safe direction for a budget (it can only end up
    pausing early, never overspending silently). Labelled here, and in the setting's
    documentation, as an approximation rather than reported as measured wire bytes.
    """
    try:
        import json as _json

        return len(_json.dumps(payload, default=str))
    except Exception as e:  # noqa: BLE001 - a size estimate must never break a data fetch
        logger.warning(f"FMP byte accounting: payload size unavailable ({type(e).__name__}: {e})")
        return None


def _response_bytes(resp) -> Optional[int]:
    """The size of a response body, or ``None`` when it cannot be read without consuming it."""
    try:
        content = getattr(resp, "content", None)
        if content is not None:
            return len(content)
    except Exception as e:  # noqa: BLE001 - a size read must never break a data fetch
        logger.warning(
            f"FMP byte accounting: could not size a {type(resp).__name__} response "
            f"({type(e).__name__}: {e}); the request is counted, its bytes are not")
        return None
    return None


def fmp_http_get(
    url: str,
    params: Optional[dict] = None,
    *,
    symbol: str = "",
    endpoint: str = "",
    timeout: int = 60,
    delays: tuple = (5, 15, 30),
    sleep: Callable[[float], None] = time.sleep,
    getter: Optional[Callable] = None,
    retry_statuses: tuple = (429, 500, 502, 503, 504),
):
    """GET an FMP endpoint with backoff retry on real HTTP rate-limit/server errors.

    Unlike ``fmp_list_call`` (which handles FMP's 200-status error-dict form), the
    direct v3/v4 endpoints return a genuine HTTP **429 Too Many Requests** (or 5xx).
    This retries those — and transient connection/timeout errors — with backoff,
    honouring a ``Retry-After`` header when present. Non-retryable HTTP errors
    (e.g. 401/404) raise immediately via ``raise_for_status``.

    Returns the successful ``requests.Response``. Raises ``FMPError`` after the
    retries are exhausted on a retryable condition.
    """
    getter = getter or requests.get
    _assert_not_hermetic("fmp_http_get", endpoint or url, symbol)
    total_attempts = len(delays) + 1
    last_reason: Any = None
    retry_after: Optional[float] = None

    for attempt in range(total_attempts):
        # Respect any GLOBAL cooldown armed by a concurrent 429 before firing (prevents the storm).
        _gate_wait(sleep)

        # Counted BEFORE the outcome is known: this attempt reached the provider (or tried
        # to), which is what a rate-limit budget has to see. Bytes are added below, only on
        # a response that actually carried a body.
        record_fmp_request(endpoint or "unknown")
        try:
            resp = getter(url, params=params, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last_reason = e
            _gate_arm(delays[min(attempt, len(delays) - 1)])  # brief global pause on transient err
            logger.warning(
                f"FMP {endpoint or 'call'} request error for {symbol or '?'} "
                f"(attempt {attempt + 1}/{total_attempts}): {e}"
            )
            continue

        status = getattr(resp, "status_code", None)
        if status in retry_statuses:
            retry_after = _parse_retry_after(getattr(resp, "headers", {}).get("Retry-After"))
            delay = delays[min(attempt, len(delays) - 1)]
            if retry_after is not None:
                delay = max(delay, retry_after)
            # Arm the SHARED gate so EVERY concurrent FMP request backs off, not just this one.
            _gate_arm(delay)
            last_reason = f"HTTP {status}"
            logger.warning(
                f"FMP {endpoint or 'call'} {status} for {symbol or '?'} "
                f"(attempt {attempt + 1}/{total_attempts}); global backoff {delay:.0f}s"
            )
            continue

        # Any other 4xx (401/404/...) is a non-retryable client error -> raise.
        resp.raise_for_status()
        # The attempt was already counted above; add what the body actually cost.
        record_fmp_bytes(endpoint or "unknown", _response_bytes(resp))
        return resp

    logger.error(
        f"FMP {endpoint or 'call'} failed for {symbol or '?'} after "
        f"{total_attempts} attempts (last: {last_reason})"
    )
    raise FMPError(
        f"FMP {endpoint or 'call'} failed for {symbol or '?'} after "
        f"{total_attempts} attempts (last: {last_reason})"
    )


def fmp_list_call(
    fn: Callable[[], Any],
    *,
    symbol: str = "",
    endpoint: str = "",
    delays: tuple = (15, 30, 60),
    sleep: Callable[[float], None] = time.sleep,
) -> List[Any]:
    """Call ``fn`` (a 0-arg wrapper around an FMP request) and normalize to a list.

    Args:
        fn: Zero-arg callable that performs the FMP call (e.g. an fmpsdk call).
        symbol: Ticker symbol, for logging context.
        endpoint: FMP endpoint name, for logging context.
        delays: Backoff delays (seconds) between retries on FMP error dicts.
            The call is retried ``len(delays)`` times.
        sleep: Sleep function (injectable for tests).

    Returns:
        A list (possibly empty) of FMP records.

    Raises:
        FMPError: On a persistent FMP error dict (after retries) or on an
            unexpected (non-list, non-error-dict) payload.
    """
    _assert_not_hermetic("fmp_list_call", endpoint, symbol)
    total_attempts = len(delays) + 1
    last_payload: Any = None

    for attempt in range(total_attempts):
        if attempt > 0:
            sleep(delays[attempt - 1])

        # COUNTED HERE TOO, not only in fmp_http_get. This wrapper carries the DOMINANT
        # warm traffic -- the three statement histories and the earnings calendar all come
        # through fmpsdk, which does its own HTTP -- so a budget that only saw fmp_http_get
        # governed the minority of the bytes it claimed to govern.
        record_fmp_request(endpoint or "fmp_list_call")

        result = fn()
        last_payload = result

        # Legitimate results.
        if isinstance(result, list):
            # SIZING IS WARM-BUDGET MACHINERY. ``_decoded_payload_bytes`` json.dumps the
            # whole payload, and on the live path nothing reads the result -- so every
            # live statement/earnings fetch paid a full serialization of its own response
            # for a counter no budget consults. The ATTEMPT stays counted in every
            # purpose; only the sizing is skipped where it has no consumer.
            if current_fmp_purpose() != PURPOSE_LIVE:
                record_fmp_bytes(endpoint or "fmp_list_call", _decoded_payload_bytes(result))
            return result
        if result is None:
            return []

        if isinstance(result, dict):
            err = _fmp_error_message(result)
            if err is not None:
                # FMP error dict (e.g. rate limit) -> warn and retry with backoff.
                logger.warning(
                    f"FMP {endpoint or 'call'} error for {symbol or '?'} "
                    f"(attempt {attempt + 1}/{total_attempts}): {err}"
                )
                continue
            # Dict without a known error key -> unexpected shape, no retry.
            logger.error(
                f"FMP {endpoint or 'call'} unexpected payload for {symbol or '?'}. "
                f"Payload: {result!r}"
            )
            raise FMPError(
                f"FMP {endpoint or 'call'} returned unexpected payload for {symbol or '?'}"
            )

        # Any other type (str, int, ...) is unexpected -> no retry.
        logger.error(
            f"FMP {endpoint or 'call'} unexpected payload type "
            f"({type(result).__name__}) for {symbol or '?'}. Payload: {result!r}"
        )
        raise FMPError(
            f"FMP {endpoint or 'call'} returned unexpected payload type "
            f"{type(result).__name__} for {symbol or '?'}"
        )

    # All attempts exhausted on FMP error dicts -> log raw payload and raise.
    logger.error(
        f"FMP {endpoint or 'call'} error for {symbol or '?'} after "
        f"{total_attempts} attempts. Payload: {last_payload!r}"
    )
    raise FMPError(
        f"FMP {endpoint or 'call'} error for {symbol or '?'} after {total_attempts} attempts"
    )
