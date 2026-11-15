"""LIVE iv-v1 reader over Alpha Vantage ``HISTORICAL_OPTIONS`` (the prior session's EOD chain).

Design: ``docs/plans/2026-11-15-option-iv-conditions.md`` (live reader). The option reruns read the
Alpha Vantage option store; live reads the SAME vendor's chain for the PREVIOUS session through the
same endpoint the downloader uses (``alphavantage.fetch_chain_payload``), accepts and maps its rows
with the store's own ``map_chain`` (ONE acceptance rule for both: a row ``map_chain`` drops is dropped
in both, a chain it empties is MISSING in both), filters it with the store reader's own
``chain_quotes_from_frame``, takes the OHLCV window the ohlcv-v1 live reader reads (FMP / Alpaca daily
cache, ``assemble_window``) and computes the row with ``market_conditions.compute_iv_v1`` -- the
function the store build calls. Same function, same inputs: identical values
(``tests/test_av_live_iv_certification.py``).

TWO PATHS, ONE CACHE.

* PREFETCH (off the decision path, :func:`prefetch_chains`): the live scheduler runs it early in the
  morning ET for the candidate symbols of experts with an ACTIVE iv gate (``ba2_trade_platform``
  ``iv_chain_prefetch``); ``tools/prefetch_iv_chains.py`` runs it by hand. It waits for budget slots
  (``PREFETCH_PER_MINUTE``), resumes from the disk cache, prunes the cache, records a failing symbol
  and continues, and stops promptly on :func:`request_stop` (app shutdown, interpreter exit).
* ON DEMAND (the decision pass, :class:`AlphaVantageLiveChainReader`): a symbol the prefetch did not
  cover is fetched when a gate on an iv field is evaluated for it. It must not stall the pass: each
  request has a TOTAL deadline (``REQUEST_DEADLINE_S``, the HTTP timeout ``HTTP_TIMEOUT_S`` inside it);
  the first budget refusal waits at most ``BUDGET_WAIT_S`` and then LATCHES until the budget window
  frees a slot; ``ChainLiveReader.CIRCUIT_AFTER`` consecutive HTTP failures open a per-pass circuit;
  a refusal is memoised for the pass and retried on the next pass.

Every fetched chain is cached once per (symbol, session):
``<cache_root>/market_conditions_live/alphavantage/<SYM>/<session>.parquet`` -- only the columns the
computation reads, written atomically (``os.replace`` retried briefly on a Windows sharing violation:
several app instances share one cache folder); a file that fails to read is deleted (after a re-stat,
so a file another instance just replaced survives) and refetched. A chain is re-checked in the cache
after waiting for a budget slot, so instances prefetching in lockstep fetch it once.

EVERY FAILURE IS AN UNKNOWN (``no_context`` + reason; the gate never passes; one summary WARNING per
reason per pass): no API key (only an ACTIVE gate ever asks for it), HTTP error / timeout / deadline,
an open circuit, rate limit (AV's notice, or no budget slot -- reported as "chain not prefetched"), a
chain AV does not serve yet (an empty response). A failure is never cached. No fallback source, no
approximation.

THE NUMBERS (settings-free constants, documented in the design note): the key allows 75 calls/minute,
shared through ``_av_rate_budget.json`` by every process that opts in (the download takes up to 60);
the live share is ``ON_DEMAND_PER_MINUTE`` = 10 on the decision path and ``PREFETCH_PER_MINUTE`` = 12 for
the prefetch, PER app instance. The file enforces the key's 75 across all of them: while the download
runs, the live owners simply get fewer slots (see the design note's operator guidance).
"""
from __future__ import annotations

import atexit
import glob
import os
import random
import threading
import time
from datetime import date
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from ba2_common.core.market_condition_iv_live import ChainLiveReader, IvEvidence
from ba2_common.core.market_condition_source import (
    SOURCE_PROFILE_FMP_DAILY, assemble_window, fmp_daily_cache_path, read_fmp_daily_cache,
)
from ba2_common.core.market_conditions import CHAIN_COMPUTE_BY_PROFILE, STATUS_MISSING_SESSION, STATUS_VALID

__all__ = ["AlphaVantageLiveChainReader", "make_reader", "prefetch_chains", "maintain_cache", "request_stop",
           "clear_stop", "LIVE_CHAIN_DIR", "BUDGET_OWNER", "ON_DEMAND_PER_MINUTE", "PREFETCH_PER_MINUTE",
           "BUDGET_WAIT_S", "HTTP_TIMEOUT_S", "REQUEST_DEADLINE_S", "KEEP_SESSIONS"]

LIVE_CHAIN_DIR = os.path.join("market_conditions_live", "alphavantage")
BUDGET_OWNER = "iv_live"
ON_DEMAND_PER_MINUTE = 10
PREFETCH_PER_MINUTE = 12
#: The decision path's FIRST budget wait; after it the reader latches (see the module docstring).
BUDGET_WAIT_S = 5.0
HTTP_TIMEOUT_S = 12.0
#: The TOTAL time one request may take (a trickling response cannot outlive it).
REQUEST_DEADLINE_S = 15.0
#: Sessions of chains kept per symbol (replay/debug); ``maintain_cache`` prunes older files and removes
#: a symbol whose newest chain is older than this many sessions.
KEEP_SESSIONS = 10
_COLUMNS = ("option_type", "strike", "expiry", "bar_date", "bid", "ask")
_REPLACE_TRIES = 6
_REPLACE_PAUSE_S = 0.05

#: Set by :func:`request_stop` (JobManager stop/shutdown, interpreter exit): a running prefetch stops at
#: its next check -- per symbol, inside every budget wait, while waiting on a request.
_STOP = threading.Event()


def request_stop() -> None:
    _STOP.set()


def clear_stop() -> None:
    _STOP.clear()


atexit.register(request_stop)


class Stopped(RuntimeError):
    """The prefetch was asked to stop."""


def _default_api_key() -> Optional[str]:
    from ba2_common.config import get_app_setting

    return get_app_setting("alpha_vantage_api_key")


def _budget_path(cache_root: str) -> str:
    return os.path.join(cache_root, "_av_rate_budget.json")


class _LatchedBudget:
    """``FileRateBudget`` for the decision path: wait at most ``wait_s`` for a slot; on a refusal,
    LATCH until the budget window frees one, refusing immediately meanwhile."""

    def __init__(self, path: str, per_minute: int, wait_s: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        from ba2_providers.av_rate_budget import FileRateBudget

        self._b = FileRateBudget(path, owner=f"{BUDGET_OWNER}:{os.getpid()}", per_minute=per_minute, key_limit=75)
        self._wait_s, self._clock, self._sleep = wait_s, clock, sleep
        self._until = 0.0

    def acquire(self) -> bool:
        now = self._clock()
        if now < self._until:
            return False
        deadline = now + self._wait_s
        while True:
            granted, wait = self._b._try()
            if granted:
                return True
            step = min(max(wait, 0.05), 1.0)
            if self._clock() + step > deadline:
                self._until = self._clock() + max(wait, 1.0)
                return False
            self._sleep(step)


class _StoppableBudget:
    """``FileRateBudget`` for the prefetch: waits for a slot, in short steps, until :func:`request_stop`."""

    def __init__(self, path: str, per_minute: int, stop: threading.Event, sleep: Callable[[float], None] = time.sleep):
        from ba2_providers.av_rate_budget import FileRateBudget

        self._b = FileRateBudget(path, owner=f"{BUDGET_OWNER}:{os.getpid()}", per_minute=per_minute, key_limit=75)
        self._stop, self._sleep = stop, sleep

    def acquire(self) -> bool:
        while not self._stop.is_set():
            granted, wait = self._b._try()
            if granted:
                return True
            self._stop.wait(min(max(wait, 0.05), 1.0))
        raise Stopped("prefetch stopped while waiting for a budget slot")


def _call_with_deadline(fn: Callable[[], Any], deadline_s: float, stop: Optional[threading.Event] = None) -> Any:
    """``fn()`` on a daemon thread, waited for at most ``deadline_s`` (and until ``stop``). A request that
    outlives it is abandoned (its own HTTP timeout ends it) and reported as a timeout."""
    box: Dict[str, Any] = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 -- handed back to the caller verbatim
            box["error"] = e

    t = threading.Thread(target=run, name="iv-live-av-request", daemon=True)
    t.start()
    end = time.monotonic() + deadline_s
    while t.is_alive():
        left = end - time.monotonic()
        if left <= 0:
            from ba2_providers.options.alphavantage import AlphaVantageOptionsError
            raise AlphaVantageOptionsError(f"TimeoutError: no complete response within {deadline_s:.0f}s")
        if stop is not None and stop.is_set():
            raise Stopped("stopped while waiting for a response")
        t.join(min(left, 0.25))
    if "error" in box:
        raise box["error"]
    return box.get("value")


class DailyWindowMixin:
    """The OHLCV window and the memo freshness key every live chain reader shares (the ohlcv-v1 live
    reader's daily cache and freshness rule). Needs ``cache_root`` and ``source_profile``."""

    cache_root: str
    source_profile: str

    def _memo_extra(self, symbol: str) -> Tuple[Any, ...]:
        """The daily-bar file's (path, mtime, size): a row computed from a stale or missing file is
        recomputed as soon as the file changes (the ohlcv-v1 live reader's freshness rule)."""
        path = fmp_daily_cache_path(symbol, self.cache_root, source_profile=self.source_profile)
        if path is None:
            return (None,)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return (path, None)
        return (path, st.st_mtime_ns, st.st_size)

    def _window(self, symbol: str, session: date):
        path = fmp_daily_cache_path(symbol, self.cache_root, source_profile=self.source_profile)
        if path is None:
            return None, STATUS_MISSING_SESSION, f"no daily bar cache for {symbol}"
        win = assemble_window(*read_fmp_daily_cache(path), session)
        if not win.ok:
            return None, win.status, win.reason
        return win.arrays(), STATUS_VALID, ""


class AlphaVantageLiveChainReader(DailyWindowMixin, ChainLiveReader):
    option_source = "alphavantage"
    serving_kind = "alphavantage"

    def __init__(self, profile: str = "iv-v1", *, cache_root: Optional[str] = None,
                 source_profile: str = SOURCE_PROFILE_FMP_DAILY,
                 api_key: Callable[[], Optional[str]] = _default_api_key,
                 fetch: Optional[Callable[..., Any]] = None, budget: Optional[Any] = None):
        super().__init__(profile)
        if cache_root is None:
            from ba2_common.core import native_cache
            cache_root = native_cache.CACHE_FOLDER
        self.cache_root = os.fspath(cache_root)
        self.source_profile = source_profile
        self._api_key = api_key
        self._fetch = fetch
        self._budget = budget
        self._fetch_lock = threading.Lock()
        #: provider calls made (visibility for tests).
        self.calls = 0

    def _chain_path(self, symbol: str, session: date) -> str:
        return chain_path(self.cache_root, symbol, session)

    def _budget_obj(self):
        if self._budget is None:
            self._budget = _LatchedBudget(_budget_path(self.cache_root), ON_DEMAND_PER_MINUTE, BUDGET_WAIT_S)
        return self._budget

    def _chain_frame(self, symbol: str, session: date) -> Tuple[Any, str, str]:
        """``(frame, failure, kind)``: the MAPPED chain of ``symbol`` on ``session`` from the disk
        cache, or one call."""
        frame = read_cached(self.cache_root, symbol, session)
        if frame is not None:
            return frame, "", ""
        if self.circuit_open():
            return None, (f"Alpha Vantage failed {self.CIRCUIT_AFTER} times in a row this pass; no further "
                          f"request this pass (chain not prefetched)"), "circuit_open"
        with self._fetch_lock:                         # one call at a time per reader
            frame = read_cached(self.cache_root, symbol, session)
            if frame is not None:
                return frame, "", ""
            frame, failure, kind = fetch_and_cache(self.cache_root, symbol, session, api_key=self._api_key,
                                                   fetch=self._fetch, budget=self._budget_obj(), counter=self)
        if kind == "http":
            self.note_http_failure()
        elif frame is not None:
            self.note_http_success()
        return frame, failure, kind

    def _evidence(self, symbol: str, session: date) -> IvEvidence:
        frame, failure, kind = self._chain_frame(symbol, session)
        if failure:
            return self._refusal(symbol, session, failure, kind)
        chain = chain_quotes(frame, symbol, session)
        window, wstatus, wreason = self._window(symbol, session)
        row = CHAIN_COMPUTE_BY_PROFILE[self.profile](session, chain, window, wstatus, wreason)
        return IvEvidence(row=row, chain=chain, window=window, window_status=wstatus, window_reason=wreason,
                          session=session)


# ----------------------------------------------------------------------------------- shared helpers
def chain_path(cache_root: str, symbol: str, session: date, chain_dir: str = LIVE_CHAIN_DIR) -> str:
    return os.path.join(cache_root, chain_dir, symbol.upper(), f"{session.isoformat()}.parquet")


def read_cached(cache_root: str, symbol: str, session: date, chain_dir: str = LIVE_CHAIN_DIR):
    """The cached mapped frame, or None. A file that fails to read is DELETED (it is refetched) -- after
    a re-stat, so a file another instance replaced meanwhile is never removed."""
    import pandas as pd
    import pyarrow as pa

    path = chain_path(cache_root, symbol, session, chain_dir)
    try:
        before = os.stat(path)
    except FileNotFoundError:
        return None
    try:
        return pd.read_parquet(path, columns=list(_COLUMNS))
    except (OSError, ValueError, pa.ArrowException) as e:
        try:
            after = os.stat(path)
            if (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size):
                os.remove(path)
        except OSError:
            pass
        from ba2_common.logger import logger
        logger.warning(f"iv-v1 live chain cache {path} could not be read ({type(e).__name__}: {e}); "
                       f"deleted, it is refetched")
        return None


def chain_quotes(frame, symbol: str, session: date):
    """The mapped frame through the store reader's own filter, as ``ChainQuotes``."""
    import pyarrow as pa

    from ba2_common.core.implied_vol import ChainQuotes
    from ba2_providers.market_conditions.option_chain_source import chain_quotes_from_frame

    if not len(frame):
        return ChainQuotes.empty()
    t = pa.Table.from_pandas(frame[list(_COLUMNS)].astype({"option_type": "string", "expiry": "string",
                                                           "bar_date": "string"}), preserve_index=False)
    s = session.isoformat()
    part = chain_quotes_from_frame(t, s, s, f"{symbol}@{session}")
    if not part:
        return ChainQuotes.empty()                 # no 7..130-day contract: the calculator says so
    return ChainQuotes(part["exp"], part["call"], part["strike"], part["bid"], part["ask"])


def _replace(tmp: str, path: str) -> None:
    """``os.replace`` retried briefly on a sharing violation (another instance reading the target)."""
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_TRIES - 1:
                raise
            time.sleep(_REPLACE_PAUSE_S * (attempt + 1))


def fetch_and_cache(cache_root: str, symbol: str, session: date, *, api_key: Callable[[], Optional[str]],
                    fetch: Optional[Callable[..., Any]], budget: Any, counter: Any = None,
                    stop: Optional[threading.Event] = None) -> Tuple[Any, str, str]:
    """One call for (symbol, session), accepted and mapped by the store's ``map_chain`` and cached
    atomically. ``(frame, failure, kind)``; a chain ``map_chain`` empties is cached as an EMPTY frame
    (MISSING, exactly as the store records it), never refused."""
    from ba2_providers.options.alphavantage import (
        AlphaVantageOptionsError, AlphaVantageThrottled, fetch_chain_payload, map_chain, redact,
    )

    key = api_key()
    if not key:
        return None, ("alpha_vantage_api_key is not set in the app settings; an active iv-v1 gate needs an "
                      "Alpha Vantage key whose plan includes HISTORICAL_OPTIONS"), "no_key"
    if not budget.acquire():
        return None, ("chain not prefetched; rate budget exhausted (the key is shared with the option "
                      "download); retried next pass"), "not_prefetched_budget"
    cached = read_cached(cache_root, symbol, session)      # another instance may have fetched it meanwhile
    if cached is not None:
        return cached, "", ""
    if counter is not None:
        counter.calls += 1

    def call():
        if fetch is None:
            return fetch_chain_payload(symbol, session, api_key=key, timeout=HTTP_TIMEOUT_S)
        return fetch(symbol, session, api_key=key, timeout=HTTP_TIMEOUT_S)

    try:
        rows = _call_with_deadline(call, REQUEST_DEADLINE_S, stop)
    except AlphaVantageThrottled as e:
        return None, f"rate limit (Alpha Vantage): {redact(str(e), key)[:160]}", "rate_limit"
    except AlphaVantageOptionsError as e:          # HTTP errors, timeouts and bad bodies, key redacted
        return None, f"Alpha Vantage request failed: {redact(str(e), key)[:200]}", "http"
    if not rows:
        return None, (f"Alpha Vantage serves no chain for {symbol} on {session} yet (not published, or no "
                      f"listed options)"), "not_published"
    frame, _stats = map_chain(symbol, session, rows)
    out = frame[list(_COLUMNS)]
    path = chain_path(cache_root, symbol, session)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
    try:
        out.to_parquet(tmp, index=False, compression="zstd")
        _replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return out, "", ""


def maintain_cache(cache_root: str, keep_sessions: int = KEEP_SESSIONS, *, part_age_s: float = 3600.0,
                   reference: Optional[date] = None, chain_dir: str = LIVE_CHAIN_DIR) -> Dict[str, int]:
    """Remove stale ``*.part`` files, keep only the newest ``keep_sessions`` sessions per symbol, and
    remove a symbol whose newest session is older than the ``keep_sessions`` sessions ending at
    ``reference`` (default: the session a live decision now reads). Runs from the prefetch, never
    from a gate."""
    from ba2_common.core.market_calendar import regular_sessions_ending_at

    root = os.path.join(cache_root, chain_dir)
    out = {"parts_removed": 0, "sessions_pruned": 0, "symbols_removed": 0}
    if not os.path.isdir(root):
        return out
    if reference is None:
        reference = _live_prior_session()
    cutoff = regular_sessions_ending_at(reference, max(keep_sessions, 1))[0].isoformat()
    now = time.time()
    for part in glob.glob(os.path.join(root, "*", "*.part")):
        try:
            if now - os.path.getmtime(part) > part_age_s:
                os.remove(part)
                out["parts_removed"] += 1
        except OSError:
            pass
    for sdir in glob.glob(os.path.join(root, "*")):
        if os.path.basename(sdir).startswith("_"):
            continue                                 # bookkeeping (capture manifests), not a symbol
        files = sorted(glob.glob(os.path.join(sdir, "????-??-??.parquet")))
        stale = not files or os.path.basename(files[-1])[:10] < cutoff
        for old in (files if stale else files[:-keep_sessions] if keep_sessions > 0 else files):
            try:
                os.remove(old)
                out["sessions_pruned"] += 1
            except OSError:
                pass
        if stale:
            try:
                if not os.listdir(sdir):
                    os.rmdir(sdir)
                    out["symbols_removed"] += 1
            except OSError:
                pass
    return out


def recent_symbols(cache_root: str, sessions: int, reference: Optional[date] = None,
                   chain_dir: str = LIVE_CHAIN_DIR) -> list:
    """Symbols with a cached chain for one of the ``sessions`` regular sessions ending at ``reference``
    (by the FILE'S session date, never its age on disk)."""
    from ba2_common.core.market_calendar import regular_sessions_ending_at

    root = os.path.join(cache_root, chain_dir)
    if not os.path.isdir(root):
        return []
    wanted = {d.isoformat() for d in regular_sessions_ending_at(reference or _live_prior_session(), sessions)}
    out = []
    for sym in sorted(os.listdir(root)):
        if sym.startswith("_"):
            continue
        try:
            names = os.listdir(os.path.join(root, sym))
        except OSError:
            continue
        if any(n[:10] in wanted and n.endswith(".parquet") for n in names):
            out.append(sym.upper())
    return out


def _live_prior_session() -> date:
    from datetime import datetime, timezone

    from ba2_common.core.market_calendar import decision_data_session, live_decision_label

    return decision_data_session(live_decision_label(datetime.now(timezone.utc)))


def prefetch_chains(symbols: Iterable[str], session: date, *, cache_root: Optional[str] = None,
                    api_key: Callable[[], Optional[str]] = _default_api_key, fetch: Optional[Callable[..., Any]] = None,
                    budget: Optional[Any] = None, deadline: Optional[float] = None,
                    stop: Optional[threading.Event] = None, jitter_s: float = 0.5,
                    log: Callable[[str], None] = lambda m: None) -> Dict[str, Any]:
    """Fetch and cache ``session``'s chain for every symbol not cached yet (resumable: the cache is the
    record). Waits for budget slots (``PREFETCH_PER_MINUTE``); stops at ``deadline``
    (``time.monotonic()``) or on ``stop`` (default: the module stop flag set by :func:`request_stop`)
    and reports what remains for the decision path. A symbol that fails (bad data, I/O) is recorded as
    failed and the run continues."""
    from ba2_providers.options.alphavantage import AlphaVantageOptionsError

    stop = _STOP if stop is None else stop
    if cache_root is None:
        from ba2_common.core import native_cache
        cache_root = native_cache.CACHE_FOLDER
    cache_root = os.fspath(cache_root)
    if budget is None:
        budget = _StoppableBudget(_budget_path(cache_root), PREFETCH_PER_MINUTE, stop)
    t0 = time.monotonic()
    report: Dict[str, Any] = {"session": session.isoformat(), "symbols": 0, "cached": 0, "fetched": 0,
                              "failed": {}, "remaining": [], "stopped": False}
    try:
        report["maintenance"] = maintain_cache(cache_root, reference=session)
    except (OSError, ValueError) as e:
        report["maintenance"] = {"error": f"{type(e).__name__}: {e}"}
    syms = sorted({str(s).upper() for s in symbols})
    report["symbols"] = len(syms)
    for i, sym in enumerate(syms):
        if stop.is_set():
            report["remaining"], report["stopped"] = syms[i:], True
            break
        if deadline is not None and time.monotonic() >= deadline:
            report["remaining"] = syms[i:]
            break
        try:
            if read_cached(cache_root, sym, session) is not None:
                report["cached"] += 1
                continue
            if jitter_s:
                stop.wait(random.uniform(0.0, jitter_s))   # instances prefetching in lockstep spread out
            frame, failure, kind = fetch_and_cache(cache_root, sym, session, api_key=api_key, fetch=fetch,
                                                   budget=budget, stop=stop)
        except Stopped:
            report["remaining"], report["stopped"] = syms[i:], True
            break
        except (ValueError, OSError, KeyError, AlphaVantageOptionsError) as e:
            # ONE symbol's bad data (an impossible OCC expiry in map_chain) or I/O (a sharing violation
            # that outlived the retries) fails THAT symbol, never the run.
            report["failed"][sym] = f"error: {type(e).__name__}: {e}"
            continue
        if frame is None:
            report["failed"][sym] = f"{kind}: {failure}"
            if kind == "no_key":
                report["remaining"] = syms[i + 1:]
                break
        else:
            report["fetched"] += 1
    report["elapsed_s"] = round(time.monotonic() - t0, 1)
    log(f"iv-v1 chain prefetch {session}: {report['fetched']} fetched, {report['cached']} already cached, "
        f"{len(report['failed'])} failed, {len(report['remaining'])} left for the decision path"
        f"{' (stopped)' if report['stopped'] else ''} ({report['elapsed_s']}s)")
    return report


def make_reader(profile: str, *, cache_root: Optional[str] = None,
                source_profile: str = SOURCE_PROFILE_FMP_DAILY) -> AlphaVantageLiveChainReader:
    """The factory the live host registers (``register_live_chain_reader("alphavantage", ...)``)."""
    return AlphaVantageLiveChainReader(profile, cache_root=cache_root, source_profile=source_profile)
