"""Daily closes for the growth figures (Account Growth tab AND the management API), from the platform's own price source.

The source is the one the live account uses for history (``core.account_history.history_provider_for_account``):
the FMP OHLCV daily provider by default, the account's Alpaca history when its ``ohlcv_history_source`` says so. There
is no yfinance here and no silent fallback to another source: a symbol the source has no closes for is ABSENT from the
result (the caller reports it as unpriced), never a 0; if the source answers nothing at all it is
``PriceSourceUnreachable`` (503). The whole batch is bounded by ``PRICE_DEADLINE_S``."""
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

from ..logger import logger
from .errors import PriceSourceUnreachable

PRICE_DEADLINE_S = 45.0
#: Symbols fetched at once (the providers block on HTTP).
WORKERS = 4

#: yfinance-style ``period`` names the page's cache ranks by, as the number of calendar days each covers.
_PERIOD_DAYS = {'6mo': 190, '1y': 370, '3y': 3 * 370}
_MAX_FALLBACK_START = date(2000, 1, 1)


def start_for_period(period: str, earliest: Optional[date] = None) -> date:
    """First date to ask for to cover ``period`` ('max' = from the account's first activity, else 2000-01-01)."""
    if period in _PERIOD_DAYS:
        return date.today() - timedelta(days=_PERIOD_DAYS[period])
    return earliest or _MAX_FALLBACK_START


def symbols_by_account(positions_by_account: Dict[int, Iterable[str]], symbols: Iterable[str]) -> Dict[int, List[str]]:
    """Assign each symbol to the lowest-numbered account that holds it (its history source prices it). A symbol no
    account holds goes to the first account."""
    accounts = sorted(positions_by_account)
    if not accounts:
        raise ValueError("no account to resolve a price source from")
    out: Dict[int, List[str]] = {}
    for sym in sorted(set(symbols)):
        owner = next((a for a in accounts if sym in positions_by_account[a]), accounts[0])
        out.setdefault(owner, []).append(sym)
    return out


def closes_from_frame(df) -> Dict[str, float]:
    """``{YYYY-MM-DD: close}`` from an OHLCV frame (a ``Date`` column or a date index, a ``Close`` column). Rows with
    no close are dropped."""
    if df is None or len(df) == 0 or 'Close' not in df.columns:
        return {}
    dates = df['Date'] if 'Date' in df.columns else df.index
    out = {}
    for stamp, close in zip(dates, df['Close']):
        if close is None or close != close:      # None or NaN
            continue
        out[stamp.strftime('%Y-%m-%d')] = float(close)
    return out


def _provider(account_id: int):
    from ..core.account_history import history_provider_for_account
    return history_provider_for_account(account_id)


def fetch_closes(by_account: Dict[int, List[str]], start: date, *,
                 deadline_s: Optional[float] = None) -> Dict[str, Dict[str, float]]:
    """``{symbol: {date: close}}`` from ``start`` through today, each account's symbols from that account's history
    source. A symbol whose fetch fails or returns nothing is absent; if EVERY symbol failed to fetch the source is
    unreachable (``PriceSourceUnreachable``); past the deadline too."""
    deadline = PRICE_DEADLINE_S if deadline_s is None else deadline_s
    jobs = [(aid, sym) for aid, syms in by_account.items() for sym in syms]
    if not jobs:
        return {}
    providers = {aid: _provider(aid) for aid in by_account}     # a bad source setting is a ValueError (Unmeasurable)
    now = datetime.now()
    begin = datetime.combine(start, datetime.min.time())

    def one(job):
        aid, sym = job
        return sym, closes_from_frame(providers[aid].get_ohlcv_data(sym, start_date=begin, end_date=now, interval='1d'))
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    futures = [pool.submit(one, job) for job in jobs]
    done, pending = wait(futures, timeout=deadline)
    pool.shutdown(wait=False, cancel_futures=True)
    if pending:
        raise PriceSourceUnreachable(f"The price source did not answer within {deadline:.0f}s",
                                     {"symbols": len(jobs), "answered": len(done)})
    out: Dict[str, Dict[str, float]] = {}
    failed = 0
    for fut in done:
        try:
            sym, closes = fut.result()
        except Exception as e:  # noqa: BLE001 -- a provider raises its own types; one symbol's failure = unpriced
            failed += 1
            logger.warning(f"price history fetch failed: {type(e).__name__}: {e}")
            continue
        if closes:
            out[sym] = closes
    if failed == len(jobs):
        raise PriceSourceUnreachable("The price source failed for every symbol", {"symbols": len(jobs)})
    return out
