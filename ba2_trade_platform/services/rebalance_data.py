"""The inputs of the allocator's REBALANCE TESTER, downloaded symbol by symbol with progress and Cancel.

Same data as the label growth simulator (``services/label_growth.py``), from the same two per-symbol reads -- not a
second way to fetch prices:

* closes: ``price_history.daily_closes`` on the account's own history provider (``price_history.history_provider``:
  FMP ``historical-price-full`` ``close`` or the account's Alpaca store; split-adjusted, not dividend-adjusted);
* distributions: ``label_growth.distribution_history`` (FMP ``stock_dividend`` ``adjDividend``).

What differs is the SHAPE of the download, because a whole account can be ~90 symbols:

* NO DEADLINE. The growth simulator's 45 s cap (``price_history.PRICE_DEADLINE_S``) is not applied here; the
  download runs to completion, reporting ``on_progress(done, total, symbol)`` once per symbol (``done`` = symbols
  finished before this one), and stops cleanly at the next symbol boundary when ``cancel`` is set (a request already
  in flight finishes; its result is kept).
* ONE SYMBOL AT A TIME, holding ONE of the process-wide download slots the price path and the growth simulator share
  (``price_history._SLOTS``, ``MAX_CONCURRENT``) for both of its requests. Serial, so this feature adds at most one
  concurrent FMP request to what live trading already sends -- a ~90-symbol account is ~180 sequential requests.
* RETRIES ARE THE SHARED ONES, never a layer on top: every FMP request here is ``ba2_providers.fmp_common
  .fmp_http_get`` (429/5xx and transient-error backoff 5/15/30 s, ``Retry-After`` honoured, and the GLOBAL FMP
  cooldown gate every FMP caller in the process waits on). The FMP OHLCV provider adds its own re-asks of an empty
  200 answer (``FMPOHLCVProvider._fetch_daily_data``); nothing in this module retries.
* A FAILED SYMBOL is NAMED with its redacted reason in ``failed`` and is not cached; the caller refuses the scope and
  a retry downloads only the failed ones. A symbol the source answered EMPTY for is complete (``completed``) and
  absent from ``closes`` ("no price history", which the tester names).

Read-only: nothing here writes to the database or touches an order. Blocking; the dialog runs it in
``asyncio.to_thread``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, Iterable, List, Optional

from ..logger import logger
from ..ui.utils.label_growth_sim import DistributionHistory, history_start
from . import label_growth, price_history

#: How long one wait for a free download slot lasts before Cancel is checked again.
SLOT_POLL_S = 0.5

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class RebalanceFetchResult:
    #: ``fmp`` | ``alpaca``: the account's history source.
    price_source: str
    total: int
    #: Symbols fully downloaded (closes AND, when there are closes, distributions): safe to cache.
    completed: List[str] = field(default_factory=list)
    #: ``{SYMBOL: {YYYY-MM-DD: close}}`` for completed symbols the source had closes for.
    closes: Dict[str, Dict[str, float]] = field(default_factory=dict)
    distributions: Dict[str, DistributionHistory] = field(default_factory=dict)
    #: ``{SYMBOL: redacted reason}``: failed after the shared retries. Never cached.
    failed: Dict[str, str] = field(default_factory=dict)
    cancelled: bool = False


def _acquire_slot(cancel: Optional[threading.Event]) -> bool:
    """One shared download slot, waiting as long as it takes; False when Cancel was set while waiting."""
    while True:
        if price_history._SLOTS.acquire(timeout=SLOT_POLL_S):
            return True
        if cancel is not None and cancel.is_set():
            return False


def fetch_rebalance_inputs(account_id: int, symbols: Iterable[str], *, as_of: date, api_key: str,
                           on_progress: Optional[ProgressCallback] = None,
                           cancel: Optional[threading.Event] = None) -> RebalanceFetchResult:
    """Closes from ``history_start(as_of)`` through today and the distribution history of each of ``symbols``, one
    symbol at a time (see the module docstring)."""
    if not api_key:
        raise ValueError("FMP API key not configured (Settings > App Settings > FMP_API_KEY): the distribution "
                         "history comes from FMP")
    wanted = sorted({str(s).strip().upper() for s in symbols if s and str(s).strip()})
    result = RebalanceFetchResult(price_source=label_growth._history_source(account_id), total=len(wanted))
    if not wanted:
        return result
    provider = price_history.history_provider(account_id)
    start = history_start(as_of)
    for index, symbol in enumerate(wanted):
        if cancel is not None and cancel.is_set():
            result.cancelled = True
            break
        if on_progress is not None:
            on_progress(index, len(wanted), symbol)
        if cancel is not None and cancel.is_set():          # Cancel pressed while the progress was being shown
            result.cancelled = True
            break
        if not _acquire_slot(cancel):
            result.cancelled = True
            break
        try:
            closes = price_history.daily_closes(provider, symbol, start)
            payouts = label_growth.distribution_history(api_key, symbol) if closes else None
        except Exception as e:  # noqa: BLE001 -- a provider raises its own types; the symbol is NAMED, never dropped
            reason = label_growth.redacted_reason(e)
            logger.warning(f"rebalance tester: download failed for {symbol} (account {account_id}): {reason}")
            result.failed[symbol] = reason
            continue
        finally:
            price_history._SLOTS.release()
        result.completed.append(symbol)
        if closes:
            result.closes[symbol] = closes
            result.distributions[symbol] = payouts
    if result.cancelled:
        logger.info(f"rebalance tester: download for account {account_id} cancelled after "
                    f"{len(result.completed) + len(result.failed)} of {len(wanted)} symbols")
    return result
