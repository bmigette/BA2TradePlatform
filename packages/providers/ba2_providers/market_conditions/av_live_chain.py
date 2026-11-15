"""LIVE iv-v1 reader over Alpha Vantage ``HISTORICAL_OPTIONS`` (the prior session's EOD chain).

Design: ``docs/plans/2026-11-15-option-iv-conditions.md`` (live reader). The option reruns read the
Alpha Vantage option store; live reads the SAME vendor's chain for the PREVIOUS session through the
same endpoint the downloader uses (``alphavantage.fetch_chain_payload``), maps it with the store's
own ``map_chain``, filters it with the store reader's own ``chain_quotes_from_frame``, takes the
OHLCV window the ohlcv-v1 live reader reads (FMP / Alpaca daily cache, ``assemble_window``) and
computes the row with ``market_conditions.compute_iv_v1`` -- the function the store build calls.
Same function, same inputs: identical values (``tests/test_av_live_iv_certification.py``).

TWO PATHS, ONE CACHE.

* PREFETCH (off the decision path, :func:`prefetch_chains`): the live scheduler runs it early in the
  morning ET for the candidate symbols of experts with an ACTIVE iv gate (``ba2_trade_platform``
  ``iv_chain_prefetch``); ``tools/prefetch_iv_chains.py`` runs it by hand. It waits for budget
  slots (``PREFETCH_PER_MINUTE``), resumes from the disk cache, and prunes the cache.
* ON DEMAND (the decision pass, :class:`AlphaVantageLiveChainReader`): a symbol the prefetch did not
  cover is fetched when a gate on an iv field is evaluated for it. It must not stall the pass: the
  HTTP timeout is ``HTTP_TIMEOUT_S``; the FIRST budget refusal waits at most ``BUDGET_WAIT_S`` and
  then LATCHES "budget exhausted" until the budget window frees a slot, so every further uncached
  symbol of the pass is refused immediately; a refusal is memoised for the pass (the second leaf of
  a symbol costs nothing) and retried on the next pass.

Every fetched chain is cached once per (symbol, session):
``<cache_root>/market_conditions_live/alphavantage/<SYM>/<session>.parquet`` -- only the columns the
computation reads, written atomically; a file that fails to read is deleted and refetched.

EVERY FAILURE IS AN UNKNOWN (``no_context`` + reason; the gate never passes; one summary WARNING per
reason per pass): no API key (only an ACTIVE gate ever asks for it, so a blank key breaks nothing
else), HTTP error or timeout, rate limit (AV's notice or no budget slot), a chain AV does not serve
yet (empty), a chain whose rows carry another date or (for a material share) none. A failure is
never cached. There is no fallback source and no approximation.

THE NUMBERS (settings-free constants, documented in the design note): the download holds 60 of the
key's 75 calls/minute, so the live share is ``ON_DEMAND_PER_MINUTE`` = 10 on the decision path and
``PREFETCH_PER_MINUTE`` = 12 for the prefetch (they share one owner tag; the shared file enforces the
key's 75 across every process).
"""
from __future__ import annotations

import glob
import os
import threading
import time
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ba2_common.core.market_condition_iv_live import ChainLiveReader, IvEvidence
from ba2_common.core.market_condition_source import (
    SOURCE_PROFILE_FMP_DAILY, assemble_window, fmp_daily_cache_path, read_fmp_daily_cache,
)
from ba2_common.core.market_conditions import CHAIN_COMPUTE_BY_PROFILE, STATUS_MISSING_SESSION, STATUS_VALID

__all__ = ["AlphaVantageLiveChainReader", "make_reader", "prefetch_chains", "maintain_cache", "LIVE_CHAIN_DIR",
           "BUDGET_OWNER", "ON_DEMAND_PER_MINUTE", "PREFETCH_PER_MINUTE", "BUDGET_WAIT_S", "HTTP_TIMEOUT_S",
           "KEEP_SESSIONS", "MAX_UNDATED_SHARE"]

LIVE_CHAIN_DIR = os.path.join("market_conditions_live", "alphavantage")
BUDGET_OWNER = "iv_live"
ON_DEMAND_PER_MINUTE = 10
PREFETCH_PER_MINUTE = 12
#: The decision path's FIRST budget wait; after it the reader latches (see the module docstring).
BUDGET_WAIT_S = 5.0
HTTP_TIMEOUT_S = 12.0
#: Sessions of chains kept per symbol (replay/debug); older files are pruned by ``maintain_cache``.
KEEP_SESSIONS = 10
#: A live chain whose rows lack a ``date`` on more than this share is refused (rows WITH a date
#: must all carry the requested session).
MAX_UNDATED_SHARE = 0.01
_COLUMNS = ("option_type", "strike", "expiry", "bar_date", "bid", "ask")


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


class AlphaVantageLiveChainReader(ChainLiveReader):
    option_source = "alphavantage"

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

    # -- the chain cache
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
        with self._fetch_lock:                         # one call at a time per reader
            frame = read_cached(self.cache_root, symbol, session)
            if frame is not None:
                return frame, "", ""
            return fetch_and_cache(self.cache_root, symbol, session, api_key=self._api_key,
                                   fetch=self._fetch, budget=self._budget_obj(), counter=self)

    def _chain(self, symbol: str, session: date):
        """``(ChainQuotes | None, failure, kind)``: the frame through the store reader's own filter."""
        frame, failure, kind = self._chain_frame(symbol, session)
        if frame is None:
            return None, failure, kind
        return chain_quotes(frame, symbol, session), "", ""

    # -- the window (the ohlcv-v1 live reader's source and assembly)
    def _window(self, symbol: str, session: date):
        path = fmp_daily_cache_path(symbol, self.cache_root, source_profile=self.source_profile)
        if path is None:
            return None, STATUS_MISSING_SESSION, f"no daily bar cache for {symbol}"
        win = assemble_window(*read_fmp_daily_cache(path), session)
        if not win.ok:
            return None, win.status, win.reason
        return win.arrays(), STATUS_VALID, ""

    def _evidence(self, symbol: str, session: date) -> IvEvidence:
        chain, failure, kind = self._chain(symbol, session)
        if failure:
            return self._refusal(symbol, session, failure, kind)
        window, wstatus, wreason = self._window(symbol, session)
        row = CHAIN_COMPUTE_BY_PROFILE[self.profile](session, chain, window, wstatus, wreason)
        return IvEvidence(row=row, chain=chain, window=window, window_status=wstatus, window_reason=wreason,
                          session=session)


# ----------------------------------------------------------------------------------- shared helpers
def chain_path(cache_root: str, symbol: str, session: date) -> str:
    return os.path.join(cache_root, LIVE_CHAIN_DIR, symbol.upper(), f"{session.isoformat()}.parquet")


def read_cached(cache_root: str, symbol: str, session: date):
    """The cached mapped frame, or None. A file that fails to read is DELETED (it is refetched)."""
    import pandas as pd

    path = chain_path(cache_root, symbol, session)
    if not os.path.exists(path):
        return None
    try:
        return pd.read_parquet(path, columns=list(_COLUMNS))
    except (OSError, ValueError) as e:
        try:
            os.remove(path)
        except OSError:
            pass
        from ba2_common.logger import logger
        logger.warning(f"iv-v1 live chain cache {path} could not be read ({type(e).__name__}: {e}); "
                       f"deleted, it is refetched")
        return None
    except Exception as e:  # pyarrow raises its own ArrowInvalid for a truncated file
        from ba2_common.core.failure_modes import absorb_if_benign
        import pyarrow as pa

        absorb_if_benign(e, pa.ArrowException)
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def chain_quotes(frame, symbol: str, session: date):
    """The mapped frame through the store reader's own filter, as ``ChainQuotes``."""
    import pyarrow as pa

    from ba2_common.core.implied_vol import ChainQuotes
    from ba2_providers.market_conditions.option_chain_source import chain_quotes_from_frame

    t = pa.Table.from_pandas(frame[list(_COLUMNS)].astype({"option_type": "string", "expiry": "string",
                                                           "bar_date": "string"}), preserve_index=False)
    s = session.isoformat()
    part = chain_quotes_from_frame(t, s, s, f"{symbol}@{session}")
    if not part:
        return ChainQuotes.empty()                 # no 7..130-day contract: the calculator says so
    return ChainQuotes(part["exp"], part["call"], part["strike"], part["bid"], part["ask"])


def check_live_dates(rows: List[Dict[str, Any]], session: date) -> Optional[str]:
    """The LIVE date contract (stricter than the store's ``map_chain``, which treats an undated row as
    the requested day): every dated row must carry ``session``, and at most ``MAX_UNDATED_SHARE`` of
    the rows may lack a date. Returns the refusal, or None."""
    want = session.isoformat()
    undated = 0
    others = set()
    for r in rows:
        d = str(r.get("date") or "")[:10]
        if not d:
            undated += 1
        elif d != want:
            others.add(d)
    if others:
        return f"Alpha Vantage returned rows dated {sorted(others)[:3]} for a {want} chain"
    if rows and undated / len(rows) > MAX_UNDATED_SHARE:
        return f"{undated} of {len(rows)} Alpha Vantage rows carry no date (more than {MAX_UNDATED_SHARE:.0%})"
    return None


def fetch_and_cache(cache_root: str, symbol: str, session: date, *, api_key: Callable[[], Optional[str]],
                    fetch: Optional[Callable[..., Any]], budget: Any, counter: Any = None) -> Tuple[Any, str, str]:
    """One call for (symbol, session), mapped by the store's ``map_chain`` and cached atomically.
    ``(frame, failure, kind)``."""
    from ba2_providers.options.alphavantage import (
        AlphaVantageOptionsError, AlphaVantageThrottled, fetch_chain_payload, map_chain, redact,
    )

    key = api_key()
    if not key:
        return None, ("alpha_vantage_api_key is not set in the app settings; an active iv-v1 gate needs an "
                      "Alpha Vantage key whose plan includes HISTORICAL_OPTIONS"), "no_key"
    if not budget.acquire():
        return None, ("rate limit: no slot in the shared Alpha Vantage budget (the key is shared with the "
                      "option download); retried next pass"), "rate_limit"
    if counter is not None:
        counter.calls += 1
    try:
        if fetch is None:
            rows = fetch_chain_payload(symbol, session, api_key=key, timeout=HTTP_TIMEOUT_S)
        else:
            rows = fetch(symbol, session, api_key=key, timeout=HTTP_TIMEOUT_S)
    except AlphaVantageThrottled as e:
        return None, f"rate limit (Alpha Vantage): {redact(str(e), key)[:160]}", "rate_limit"
    except AlphaVantageOptionsError as e:          # HTTP errors, timeouts and bad bodies, key redacted
        return None, f"Alpha Vantage request failed: {redact(str(e), key)[:200]}", "http"
    if not rows:
        return None, (f"Alpha Vantage serves no chain for {symbol} on {session} yet (not published, or no "
                      f"listed options)"), "not_published"
    bad = check_live_dates(rows, session)
    if bad:
        return None, bad, "bad_chain"
    frame, _stats = map_chain(symbol, session, rows)
    if not len(frame):
        return None, f"Alpha Vantage chain for {symbol} on {session} has no usable contract", "bad_chain"
    out = frame[list(_COLUMNS)]
    path = chain_path(cache_root, symbol, session)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
    try:
        out.to_parquet(tmp, index=False, compression="zstd")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return out, "", ""


def maintain_cache(cache_root: str, keep_sessions: int = KEEP_SESSIONS, *, part_age_s: float = 3600.0) -> Dict[str, int]:
    """Remove stale ``*.part`` files and keep only the newest ``keep_sessions`` sessions per symbol.
    Runs from the prefetch (off the decision path), never from a gate."""
    root = os.path.join(cache_root, LIVE_CHAIN_DIR)
    out = {"parts_removed": 0, "sessions_pruned": 0}
    if not os.path.isdir(root):
        return out
    now = time.time()
    for part in glob.glob(os.path.join(root, "*", "*.part")):
        try:
            if now - os.path.getmtime(part) > part_age_s:
                os.remove(part)
                out["parts_removed"] += 1
        except OSError:
            pass
    for sdir in glob.glob(os.path.join(root, "*")):
        files = sorted(glob.glob(os.path.join(sdir, "????-??-??.parquet")))
        for old in files[:-keep_sessions] if keep_sessions > 0 else files:
            try:
                os.remove(old)
                out["sessions_pruned"] += 1
            except OSError:
                pass
    return out


def prefetch_chains(symbols: Iterable[str], session: date, *, cache_root: Optional[str] = None,
                    api_key: Callable[[], Optional[str]] = _default_api_key, fetch: Optional[Callable[..., Any]] = None,
                    budget: Optional[Any] = None, deadline: Optional[float] = None,
                    log: Callable[[str], None] = lambda m: None) -> Dict[str, Any]:
    """Fetch and cache ``session``'s chain for every symbol not cached yet (resumable: the cache is the
    record). Waits for budget slots (``PREFETCH_PER_MINUTE``) instead of refusing; stops at
    ``deadline`` (``time.monotonic()`` value) and reports what remains for the decision path."""
    if cache_root is None:
        from ba2_common.core import native_cache
        cache_root = native_cache.CACHE_FOLDER
    cache_root = os.fspath(cache_root)
    if budget is None:
        from ba2_providers.av_rate_budget import FileRateBudget

        b = FileRateBudget(_budget_path(cache_root), owner=f"{BUDGET_OWNER}:{os.getpid()}",
                           per_minute=PREFETCH_PER_MINUTE, key_limit=75)

        class _Blocking:
            def acquire(self):
                b.acquire()
                return True
        budget = _Blocking()
    t0 = time.monotonic()
    report: Dict[str, Any] = {"session": session.isoformat(), "symbols": 0, "cached": 0, "fetched": 0,
                              "failed": {}, "remaining": [], "maintenance": maintain_cache(cache_root)}
    syms = sorted({str(s).upper() for s in symbols})
    report["symbols"] = len(syms)
    for i, sym in enumerate(syms):
        if read_cached(cache_root, sym, session) is not None:
            report["cached"] += 1
            continue
        if deadline is not None and time.monotonic() >= deadline:
            report["remaining"] = syms[i:]
            break
        frame, failure, kind = fetch_and_cache(cache_root, sym, session, api_key=api_key, fetch=fetch, budget=budget)
        if frame is None:
            report["failed"][sym] = f"{kind}: {failure}"
            if kind == "no_key":
                report["remaining"] = syms[i + 1:]
                break
        else:
            report["fetched"] += 1
    report["elapsed_s"] = round(time.monotonic() - t0, 1)
    log(f"iv-v1 chain prefetch {session}: {report['fetched']} fetched, {report['cached']} already cached, "
        f"{len(report['failed'])} failed, {len(report['remaining'])} left for the decision path "
        f"({report['elapsed_s']}s)")
    return report


def make_reader(profile: str, *, cache_root: Optional[str] = None,
                source_profile: str = SOURCE_PROFILE_FMP_DAILY) -> AlphaVantageLiveChainReader:
    """The factory the live host registers (``register_live_chain_reader("alphavantage", ...)``)."""
    return AlphaVantageLiveChainReader(profile, cache_root=cache_root, source_profile=source_profile)
