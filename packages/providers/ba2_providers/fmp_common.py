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

import threading
import time
from contextlib import contextmanager
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


@contextmanager
def persist_empty_sentinel():
    """Within this context a genuinely-empty FMP history is cached as ``[]`` (prewarm sentinel).
    Used by ``ba2-test prewarm`` so no-data symbols don't perpetually look 'not pre-warmed'."""
    global _PERSIST_EMPTY_SENTINEL
    prev = _PERSIST_EMPTY_SENTINEL
    _PERSIST_EMPTY_SENTINEL = True
    try:
        yield
    finally:
        _PERSIST_EMPTY_SENTINEL = prev


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
                            max_age_days: float = _FMP_HISTORY_DISK_MAX_AGE_DAYS) -> Any:
    """Disk-persist a per-symbol FMP *history* payload so spawned backtest workers read it from
    disk instead of re-fetching from FMP.

    BACKTEST-ONLY: when the TTL freeze flag is NOT set (the live path) this is a straight
    passthrough to ``fetch_fn`` — live analysis always hits the live API. Keyed by
    ``(namespace, symbol)`` as JSON under ``CACHE_FOLDER/fmp_history``; reused if younger than
    ``max_age_days`` (past-data histories rarely change; a week balances reuse vs picking up
    newly-published rows). Best-effort: any disk error falls back to a live ``fetch_fn`` so a
    cache problem can never break a run. The atomic tmp+replace write means concurrent workers
    never read a half-written file.
    """
    if not _is_ttl_frozen():
        return fetch_fn()  # live path: never cache to disk; always pull fresh from the API
    # BACKTEST in-process layer: hold the loaded payload in memory for the (frozen) run so each
    # (namespace, symbol) is read+parsed from disk ONCE per worker, not once per analysis bar.
    # Without this, per-bar experts (insider/earnings/senate/finnhub) re-`json.load`ed the whole
    # per-symbol history every bar — the dominant backtest bottleneck (FMPRating sidestepped it
    # with its own TTLCache; this generalises that to every disk-cached history). Returns the SAME
    # object across calls, so callers' per-row date memoization (e.g. ``_pd``/``_td_memo``) sticks.
    return _HISTORY_MEM_CACHE.get_or_call(
        f"{namespace}__{symbol.upper()}",
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
    except Exception:  # corrupt / partial / unreadable -> re-fetch (or raise, hermetic)
        pass

    # HERMETIC backtest: NEVER network-fetch — a miss means the data wasn't pre-warmed. Raise a
    # clear, actionable error (0-fetch guarantee) instead of a silent multi-minute network grind.
    if _is_hermetic_fmp_history():
        raise FMPHistoryCacheMiss(
            f"fmp_history '{namespace}/{symbol}' not pre-warmed (hermetic backtest, 0 fetch). "
            f"Run `ba2-test prewarm` for this universe before backtesting."
        )

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
    to_persist = data if data else ([] if _PERSIST_EMPTY_SENTINEL else None)
    if to_persist is not None:
        tmp = None
        try:
            _os.makedirs(d, exist_ok=True)
            tmp = f"{path}.{_os.getpid()}.tmp"
            with open(tmp, "w") as fh:
                _json.dump(to_persist, fh)
            _os.replace(tmp, path)  # atomic
        except Exception:
            if tmp:
                try:
                    _os.remove(tmp)  # never leave a half-written tmp behind
                except OSError:
                    pass
    return data


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
    total_attempts = len(delays) + 1
    last_reason: Any = None
    retry_after: Optional[float] = None

    for attempt in range(total_attempts):
        # Respect any GLOBAL cooldown armed by a concurrent 429 before firing (prevents the storm).
        _gate_wait(sleep)

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
    total_attempts = len(delays) + 1
    last_payload: Any = None

    for attempt in range(total_attempts):
        if attempt > 0:
            sleep(delays[attempt - 1])

        result = fn()
        last_payload = result

        # Legitimate results.
        if isinstance(result, list):
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
