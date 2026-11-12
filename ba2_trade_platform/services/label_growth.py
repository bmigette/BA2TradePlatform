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

A failed distribution fetch is recorded per symbol (``DistributionHistory(events=None, error=...)``) and makes the
price + distributions figure unknown, loudly; a price source that is unreachable, busy or past its deadline raises
``PriceSourceUnreachable`` and the dialog shows it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from ..logger import logger
from ..ui.utils.label_growth_sim import DistributionHistory, history_start
from . import price_history
from .errors import PriceSourceUnreachable


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
    closes = price_history.fetch_closes({account_id: wanted}, start)
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
    distributions: Dict[str, DistributionHistory] = {}
    from ba2_providers.symbol_info import FMPError, fetch_dividends
    for sym in wanted:
        try:
            distributions[sym] = DistributionHistory(events=distribution_events_from_fmp(fetch_dividends(api_key, sym)))
        except (FMPError, OSError, ValueError) as e:
            logger.error(f"label growth: distribution history for {sym} could not be fetched: {e}", exc_info=True)
            distributions[sym] = DistributionHistory(events=None, error=f"{type(e).__name__}: {e}")
    return LabelGrowthInputs(as_of=as_of, start=start, closes={s: dict(closes[s]) for s in wanted if s in closes},
                             distributions=distributions, price_source=source)


def symbols_to_fetch(shares: Mapping[str, float]) -> list:
    """Only symbols with a positive share need history: a 0% share invests nothing."""
    return sorted(s for s, v in shares.items() if v and v > 0)
