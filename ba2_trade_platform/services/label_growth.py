"""The inputs of the allocator's label growth simulator (``ui/utils/label_growth_sim.py``): daily closes and per-share
distributions for a label's symbols, on ONE split basis. Read-only: nothing here writes to the database or touches an
order. Blocking (HTTP); the page calls it through ``asyncio.to_thread``.

WHERE THE NUMBERS COME FROM, AND WHY THEY ARE ON ONE BASIS
* CLOSES: ``services.price_history.fetch_closes`` -- the same source, concurrency cap and deadline the Account Growth
  tab and the management API use: the ACCOUNT'S OWN history provider (``core.account_history``): FMP
  ``/api/v3/historical-price-full/{symbol}`` field ``close`` by default, or the account's Alpaca history store when its
  ``ohlcv_history_source`` is ``alpaca``. Both are SPLIT-ADJUSTED and NOT dividend-adjusted: FMP's ``close`` is
  back-adjusted for splits (its dividend-adjusted series is the separate ``adjClose``, which is never read here), and the
  Alpaca store is built from ``adjustment=raw`` bars back-adjusted by the split table only ("Cash dividends are not
  adjusted (like FMP's close)", ``ba2_providers/ohlcv/alpaca_history/build.py``). Not yfinance, no fallback.
* DISTRIBUTIONS: FMP ``/api/v3/historical-price-full/stock_dividend/{symbol}`` (``ba2_providers.symbol_info
  .fetch_dividends``, 24 h cache), field ``adjDividend`` -- the per-share amount back-adjusted for later splits, i.e. on
  the same basis as the split-adjusted close (AAPL 2020-08-07: ``dividend`` 0.82, ``adjDividend`` 0.205 after the 4:1
  split; NVDA 2020-06-04: 0.16 / 0.004 after 4:1 and 10:1). The as-declared ``dividend`` is NEVER used: across a split
  it is on another basis. A row without ``adjDividend`` stays unknown (``None``) and the simulator reports it.

FAILURE IS NEVER ABSENCE
* A price download that FAILED for any one symbol (the provider raised: a 5xx after retries, a timeout, ...) refuses the
  whole result with ``PriceSourceUnreachable`` naming the symbol and the reason. Reporting it as "no price history"
  would park its slice in cash at 0% and dilute every horizon's headline under a false label. (Only a symbol the source
  answered EMPTY for is "no price history".) Same for a source that is busy, past its deadline, or empty for every
  symbol.
* A distribution fetch that failed, or did not answer within ``DIVIDEND_DEADLINE_S``, is recorded per symbol
  (``DistributionHistory(events=None, error=...)``) and makes the price + distributions figure unknown, with the reason.
* The distribution downloads run on the SAME process-wide download slots as the price path
  (``price_history._SLOTS``, ``MAX_CONCURRENT``): this feature never adds load on the FMP key live trading uses beyond
  that cap. No free slot = every distribution unknown ("busy").
* Every reason shown or logged goes through ``redacted_reason`` (FMP ``apikey=`` plus the platform log redaction:
  URL secrets, Authorization / Bearer, ``sk-`` keys, JWTs).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from ..logger import logger
from ..ui.utils.label_growth_sim import DistributionHistory, history_start
from . import price_history
from .errors import PriceSourceUnreachable

#: The whole distribution download is bounded like the price download.
DIVIDEND_DEADLINE_S = price_history.PRICE_DEADLINE_S


def redacted_reason(exc: BaseException) -> str:
    """``"<Type>: <message>"`` with credentials masked -- for anything shown in the dialog or logged."""
    return price_history._redacted(exc)


@dataclass
class LabelGrowthInputs:
    as_of: date
    start: date
    #: ``{SYMBOL: {YYYY-MM-DD: close}}``; a symbol the source had nothing for is ABSENT (unmeasurable).
    closes: Dict[str, Dict[str, float]]
    distributions: Dict[str, DistributionHistory]
    #: ``fmp`` | ``alpaca``: which history source the account's closes came from (shown in the dialog).
    price_source: str


def distribution_events_from_fmp(payload: Any) -> Tuple[Tuple[str, Optional[float]], ...]:
    """``((ex-date, adjDividend), ...)`` ascending from an FMP ``stock_dividend`` payload. ``adjDividend`` ONLY: a row
    without it keeps ``None`` (never the as-declared ``dividend``, which is on the pre-split basis). An FMP error body
    (HTTP 200 carrying ``Error Message``) raises ``ValueError`` rather than reading as "paid nothing"."""
    from ba2_providers.symbol_info import parse_dividends
    if isinstance(payload, dict):
        for key in ('Error Message', 'error', 'message'):
            if key in payload and 'historical' not in payload:
                raise ValueError(f"FMP stock_dividend answered an error: {payload[key]}")
    elif payload is not None and not isinstance(payload, list):
        raise ValueError(f"FMP stock_dividend answered an unexpected {type(payload).__name__}")
    return tuple((e.ex_date.isoformat(), e.adj_dividend) for e in parse_dividends(payload))


def distribution_history(api_key: str, symbol: str) -> DistributionHistory:
    """ONE symbol's distribution history (``adjDividend`` per ex-date). The single per-symbol read: this module's
    workers and the rebalance tester both call it. The request is ``ba2_providers.symbol_info.fetch_dividends`` ->
    ``_get_json`` -> ``fmp_common.fmp_http_get`` (429/5xx backoff, Retry-After, the process-wide FMP gate) behind the
    24 h ``fmp_live_cached`` cache; nothing here retries. Raises ``FMPError`` / ``OSError`` / ``ValueError`` on a
    failed fetch or an FMP error body."""
    from ba2_providers.symbol_info import fetch_dividends
    return DistributionHistory(events=distribution_events_from_fmp(fetch_dividends(api_key, symbol)))


def _history_source(account_id: int) -> str:
    from ..core.account_history import history_source_for_account
    return history_source_for_account(account_id)


def fetch_label_growth_inputs(account_id: int, symbols: Iterable[str], *, as_of: date,
                              api_key: str) -> LabelGrowthInputs:
    """Closes from ``history_start(as_of)`` through today and the distribution histories of ``symbols``.

    Raises ``PriceSourceUnreachable`` when the price source is down, busy, or did not answer for every symbol within its
    deadline (a symbol it never got to is NOT "no history": it would be reported as unmeasurable, so the whole fetch is
    refused instead)."""
    if not api_key:
        raise ValueError("FMP API key not configured (Settings > App Settings > FMP_API_KEY): the distribution "
                         "history comes from FMP")
    wanted = sorted({str(s).strip().upper() for s in symbols if s and str(s).strip()})
    start = history_start(as_of)
    source = _history_source(account_id)
    if not wanted:
        return LabelGrowthInputs(as_of, start, {}, {}, source)
    failures: Dict[str, str] = {}
    closes = price_history.fetch_closes({account_id: wanted}, start, errors=failures)
    if failures:
        detail = price_history.redact_message('; '.join(f"{sym}: {failures[sym]}" for sym in sorted(failures)))
        raise PriceSourceUnreachable(
            f"The price fetch failed for {len(failures)} of {len(wanted)} symbol(s), so no total would be complete "
            f"-- price fetch failed for {detail}. Try again.",
            {"failed": {k: price_history.redact_message(v) for k, v in failures.items()}})
    if closes.timed_out:
        raise PriceSourceUnreachable(
            f"The price source did not answer for {', '.join(closes.unfetched)} within "
            f"{price_history.PRICE_DEADLINE_S:.0f}s; try again",
            {"unfetched": list(closes.unfetched)})
    missing = [s for s in wanted if s not in closes]
    if len(missing) == len(wanted):
        # Every symbol empty is a source problem far more often than a label of unlisted symbols: refuse, loudly,
        # rather than draw a page of "unmeasurable, held as cash" that reads like a 0% return.
        raise PriceSourceUnreachable(
            f"The price source ({source}) returned no history for any of {', '.join(wanted)}; see the log",
            {"symbols": wanted})
    if missing:
        logger.warning(f"label growth: account {account_id} ({source}) has no price history for {missing}; "
                       f"reported as unmeasurable")
    distributions = fetch_distributions(api_key, [s for s in wanted if s in closes])
    return LabelGrowthInputs(as_of=as_of, start=start, closes={s: dict(closes[s]) for s in wanted if s in closes},
                             distributions=distributions, price_source=source)


def fetch_distributions(api_key: str, symbols: List[str], *,
                        deadline_s: Optional[float] = None) -> Dict[str, DistributionHistory]:
    """``{SYMBOL: DistributionHistory}`` for ``symbols``, on the price path's download slots, within a deadline.

    A symbol whose fetch failed, that was still running at the deadline, or that never got a slot (all busy) gets
    ``events=None`` and the reason. Results that arrive after the deadline are ignored."""
    from ba2_providers.symbol_info import FMPError
    deadline = DIVIDEND_DEADLINE_S if deadline_s is None else deadline_s
    out: Dict[str, DistributionHistory] = {}
    if not symbols:
        return out
    slots_ = price_history._SLOTS
    slots = sum(1 for _ in range(min(price_history.WORKERS, len(symbols))) if slots_.acquire(blocking=False))
    if slots == 0:
        why = (f"the download slots shared with the price history are all in use (busy, "
               f"{price_history.MAX_CONCURRENT} max); try again")
        logger.error(f"label growth: distributions not fetched for {symbols}: {why}")
        return {s: DistributionHistory(events=None, error=why) for s in symbols}
    lock = threading.Lock()
    queue = list(symbols)
    state = {"closed": False}

    def worker():
        try:
            while True:
                with lock:
                    if state["closed"] or not queue:
                        return
                    sym = queue.pop(0)
                try:
                    found = distribution_history(api_key, sym)
                except (FMPError, OSError, ValueError) as e:
                    reason = redacted_reason(e)
                    logger.error(f"label growth: distribution history for {sym} could not be fetched: {reason}")
                    found = DistributionHistory(events=None, error=reason)
                with lock:
                    if not state["closed"]:
                        out[sym] = found
        finally:
            slots_.release()

    threads = [threading.Thread(target=worker, daemon=True, name="label-growth-dividends") for _ in range(slots)]
    for t in threads:
        t.start()
    end_at = time.monotonic() + deadline
    for t in threads:
        t.join(max(0.0, end_at - time.monotonic()))
    with lock:
        state["closed"] = True                  # late answers are dropped; running calls still free their slot
        result = dict(out)
    late = [s for s in symbols if s not in result]
    if late:
        why = f"no answer within {deadline:g}s"
        logger.error(f"label growth: distribution history for {late}: {why}")
        for s in late:
            result[s] = DistributionHistory(events=None, error=why)
    return result


def symbols_to_fetch(shares: Mapping[str, float]) -> list:
    """Only symbols with a positive share need history: a 0% share invests nothing."""
    return sorted(s for s, v in shares.items() if v and v > 0)
