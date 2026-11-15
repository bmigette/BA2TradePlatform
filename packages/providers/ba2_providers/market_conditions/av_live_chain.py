"""LIVE iv-v1 reader over Alpha Vantage ``HISTORICAL_OPTIONS`` (the prior session's EOD chain).

Design: ``docs/plans/2026-11-15-option-iv-conditions.md`` (live reader). The option reruns read the
Alpha Vantage option store; live reads the SAME vendor's chain for the PREVIOUS session through the
same endpoint the downloader uses (``alphavantage.fetch_chain_payload``), maps it with the store's
own ``map_chain``, filters it with the store reader's own ``chain_quotes_from_frame``, takes the
OHLCV window the ohlcv-v1 live reader reads (FMP / Alpaca daily cache, ``assemble_window``) and
computes the row with ``market_conditions.compute_iv_v1`` -- the function the store build calls.
Same function, same inputs: identical values (``tests/test_av_live_iv_certification.py``).

ON DEMAND ONLY. A chain is fetched when a gate on an iv field is evaluated for a symbol (the reader
is built only for an expert whose setting names iv-v1, and the deploy importer drops iv-v1 from a
setting no leaf reads). Each fetched chain is cached on disk per (symbol, session) -- the mapped
frame, ``<cache_root>/market_conditions_live/alphavantage/<SYM>/<session>.parquet`` -- so a repeat
pass, a restart or another expert costs no call; a row is memoised per reader as well.

EVERY FAILURE IS AN UNKNOWN, refused loudly and countably (``no_context`` + reason, one WARNING
per (symbol, session, reason); the gate never passes): no API key (only an ACTIVE gate ever asks
for it, so a blank key breaks nothing else), HTTP error, rate limit (AV's notice, or no slot in the
shared ``_av_rate_budget.json`` within ``budget_wait_s``), a chain AV does not serve yet (empty),
or a chain for another date. A failure is never cached, so the next pass tries again. There is no
fallback source and no approximation.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date
from typing import Any, Callable, Optional

from ba2_common.core.market_condition_iv_live import ChainLiveReader, IvEvidence
from ba2_common.core.market_condition_source import (
    SOURCE_PROFILE_FMP_DAILY, assemble_window, fmp_daily_cache_path, read_fmp_daily_cache,
)
from ba2_common.core.market_conditions import CHAIN_COMPUTE_BY_PROFILE, STATUS_MISSING_SESSION, STATUS_VALID

__all__ = ["AlphaVantageLiveChainReader", "make_reader", "LIVE_CHAIN_DIR", "BUDGET_OWNER", "BUDGET_PER_MINUTE"]

LIVE_CHAIN_DIR = os.path.join("market_conditions_live", "alphavantage")
BUDGET_OWNER = "iv_live"
#: This reader's own share of the key's 75 calls/minute (the download's default is 60).
BUDGET_PER_MINUTE = 10
#: How long one fetch may wait for a slot in the shared budget before it refuses.
BUDGET_WAIT_S = 20.0


def _default_api_key() -> Optional[str]:
    from ba2_common.config import get_app_setting

    return get_app_setting("alpha_vantage_api_key")


class _TimedBudget:
    """``FileRateBudget`` with a bounded wait: a live decision pass must not block a minute on a
    budget the download saturates; no slot within ``wait_s`` refuses (rate limit) instead."""

    def __init__(self, path: str, per_minute: int, wait_s: float, sleep: Callable[[float], None] = time.sleep):
        from ba2_providers.av_rate_budget import FileRateBudget

        self._b = FileRateBudget(path, owner=f"{BUDGET_OWNER}:{os.getpid()}", per_minute=per_minute, key_limit=75)
        self._wait_s, self._sleep = wait_s, sleep

    def acquire(self) -> bool:
        deadline = time.monotonic() + self._wait_s
        while True:
            granted, wait = self._b._try()
            if granted:
                return True
            if time.monotonic() + min(max(wait, 0.05), 2.0) > deadline:
                return False
            self._sleep(min(max(wait, 0.05), 2.0))


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

    # -- the chain
    def _chain_path(self, symbol: str, session: date) -> str:
        return os.path.join(self.cache_root, LIVE_CHAIN_DIR, symbol, f"{session.isoformat()}.parquet")

    def _budget_obj(self):
        if self._budget is None:
            self._budget = _TimedBudget(os.path.join(self.cache_root, "_av_rate_budget.json"),
                                        BUDGET_PER_MINUTE, BUDGET_WAIT_S)
        return self._budget

    def _chain_frame(self, symbol: str, session: date):
        """``(frame, failure)``: the MAPPED chain of ``symbol`` on ``session`` (store mapping), from
        the per-(symbol, session) disk cache or one call."""
        import pandas as pd

        path = self._chain_path(symbol, session)
        if os.path.exists(path):
            return pd.read_parquet(path), ""
        key = self._api_key()
        if not key:
            return None, ("alpha_vantage_api_key is not set in the app settings; an active iv-v1 gate needs "
                          "an Alpha Vantage key whose plan includes HISTORICAL_OPTIONS")
        from ba2_providers.options.alphavantage import (
            AlphaVantageOptionsError, AlphaVantageThrottled, fetch_chain_payload, map_chain, redact,
        )

        with self._fetch_lock:                         # one call at a time per reader
            if os.path.exists(path):
                return pd.read_parquet(path), ""
            if not self._budget_obj().acquire():
                return None, (f"rate limit: no slot in the shared Alpha Vantage budget within "
                              f"{BUDGET_WAIT_S:.0f}s (the key is shared with the option download)")
            fetch = self._fetch or fetch_chain_payload
            self.calls += 1
            try:
                rows = fetch(symbol, session, api_key=key)
            except AlphaVantageThrottled as e:
                return None, f"rate limit (Alpha Vantage): {redact(str(e), key)[:160]}"
            except AlphaVantageOptionsError as e:
                return None, f"Alpha Vantage request failed: {redact(str(e), key)[:200]}"
            except Exception as e:  # noqa: BLE001 -- any transport fault is an unknown, key redacted
                return None, f"Alpha Vantage request failed: {type(e).__name__}: {redact(str(e), key)[:200]}"
            if not rows:
                return None, (f"Alpha Vantage serves no chain for {symbol} on {session} yet (not published, "
                              f"or no listed options)")
            frame, stats = map_chain(symbol, session, rows)
            if stats.dropped_wrong_date and not len(frame):
                return None, f"Alpha Vantage returned a chain for another date than {session}"
            if not len(frame):
                return None, f"Alpha Vantage chain for {symbol} on {session} has no usable contract"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
            frame.to_parquet(tmp, index=False, compression="zstd")
            os.replace(tmp, path)
            return frame, ""

    def _chain(self, symbol: str, session: date):
        """``(ChainQuotes | None, failure)``: the frame through the store reader's own filter."""
        import pyarrow as pa

        from ba2_common.core.implied_vol import ChainQuotes
        from ba2_providers.market_conditions.option_chain_source import _COLUMNS, chain_quotes_from_frame

        frame, failure = self._chain_frame(symbol, session)
        if frame is None:
            return None, failure
        t = pa.Table.from_pandas(frame[_COLUMNS].astype({"option_type": "string", "expiry": "string",
                                                         "bar_date": "string"}), preserve_index=False)
        s = session.isoformat()
        part = chain_quotes_from_frame(t, s, s, f"{symbol}@{session}")
        if not part:
            return ChainQuotes.empty(), ""            # a chain with no 7..130-day contract: the calculator says so
        return ChainQuotes(part["exp"], part["call"], part["strike"], part["bid"], part["ask"]), ""

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
        chain, failure = self._chain(symbol, session)
        if failure:
            return self._refusal(symbol, session, failure)
        window, wstatus, wreason = self._window(symbol, session)
        row = CHAIN_COMPUTE_BY_PROFILE[self.profile](session, chain, window, wstatus, wreason)
        return IvEvidence(row=row, chain=chain, window=window, window_status=wstatus, window_reason=wreason)


def make_reader(profile: str, *, cache_root: Optional[str] = None,
                source_profile: str = SOURCE_PROFILE_FMP_DAILY) -> AlphaVantageLiveChainReader:
    """The factory the live host registers (``register_live_chain_reader("alphavantage", ...)``)."""
    return AlphaVantageLiveChainReader(profile, cache_root=cache_root, source_profile=source_profile)
