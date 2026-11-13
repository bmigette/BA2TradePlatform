"""Daily closes for the growth figures (Account Growth tab AND the management API), from the platform's own price source.

The source is the one the live account uses for history (``core.account_history.history_provider_for_account``):
the FMP OHLCV daily provider by default, the account's Alpaca history when its ``ohlcv_history_source`` says so. There
is no yfinance here and no silent fallback to another source: a symbol the source has no closes for is ABSENT from the
result (the caller reports it as unpriced), never a 0; if the source answers nothing at all it is
``PriceSourceUnreachable`` (503). The whole batch is bounded by ``PRICE_DEADLINE_S``."""
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

from ..logger import logger
from .errors import PriceSourceUnreachable

PRICE_DEADLINE_S = 45.0
#: Concurrent provider calls from this path, process-wide (the providers block on HTTP, and FMP's key is shared with
#: live trading). One request uses at most WORKERS of them.
MAX_CONCURRENT = 4
WORKERS = 4
_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)

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


def history_provider(account_id: int):
    """The account's own history provider (FMP or its Alpaca store), the one every fetch on this path reads."""
    return _provider(account_id)


def daily_closes(provider, symbol: str, start: date, *, now: Optional[datetime] = None) -> Dict[str, float]:
    """ONE symbol's ``{YYYY-MM-DD: close}`` from ``start`` through ``now`` from ``provider`` (see
    :func:`history_provider`). The single per-symbol read of this module: ``fetch_closes`` runs it in its workers and the
    allocator's rebalance tester calls it symbol by symbol. The provider's own request path applies (for FMP:
    ``ba2_providers.fmp_common.fmp_http_get`` -- 429/5xx backoff and the process-wide FMP gate); nothing here retries.
    Raises whatever the provider raises; ``{}`` = the source has nothing for the window."""
    begin = datetime.combine(start, datetime.min.time())
    return closes_from_frame(provider.get_ohlcv_data(symbol, start_date=begin, end_date=now or datetime.now(),
                                                     interval='1d'))


def redact_message(text: str) -> str:
    """``text`` with credentials masked: FMP's ``apikey=`` (``fmp_common.redact``) and every other URL / header
    secret the platform log redaction knows (``log_redaction.redact_text``). Idempotent."""
    from ba2_providers.fmp_common import redact
    from ..log_redaction import redact_text
    return redact_text(redact(str(text)))


def _redacted(exc: BaseException) -> str:
    """``"<Type>: <message>"``, redacted."""
    return redact_message(f"{type(exc).__name__}: {exc}")


class Closes(dict):
    """``{symbol: {date: close}}`` plus how complete it is: ``timed_out`` (the deadline passed with symbols still
    unfetched) and ``unfetched`` (those symbols). A symbol that is simply absent is unpriced."""
    timed_out: bool = False
    unfetched: List[str] = []


def fetch_closes(by_account: Dict[int, List[str]], start: date, *,
                 deadline_s: Optional[float] = None,
                 errors: Optional[Dict[str, str]] = None) -> Closes:
    """``{symbol: {date: close}}`` from ``start`` through today, each account's symbols from that account's history
    source.

    CONCURRENCY IS CAPPED for the whole process (``MAX_CONCURRENT`` slots, shared by the page and the API, and a
    slot is held until its provider call RETURNS, even after a request has given up): this path's calls against the
    FMP key live trading also uses can never exceed it. A request that finds no slot free is refused at once with
    ``PriceSourceUnreachable`` (``detail.busy``); the page treats that like any failed download (unpriced).

    A symbol whose fetch raises something other than a network error, or returns nothing, is absent (unpriced). If
    EVERY symbol failed with a network error the source is unreachable (``PriceSourceUnreachable``). At the deadline
    the symbols already fetched are returned with ``timed_out`` set and the rest in ``unfetched``; only if nothing at
    all finished is it ``PriceSourceUnreachable``.

    ``errors``: OPT-IN. A caller that must tell "the fetch FAILED" from "the source has nothing" passes a dict; every
    symbol whose provider call raised (before the deadline) is recorded in it as ``{symbol: "<Type>: <message>"}``,
    credentials redacted. The returned ``Closes`` is identical either way, so a caller passing nothing sees no change."""
    deadline = PRICE_DEADLINE_S if deadline_s is None else deadline_s
    jobs = [(aid, sym) for aid, syms in by_account.items() for sym in syms]
    out = Closes()
    out.unfetched = []
    if not jobs:
        return out
    providers = {aid: _provider(aid) for aid in by_account}     # a bad source setting is a ValueError (Unmeasurable)
    now = datetime.now()
    slots =sum(1 for _ in range(min(WORKERS, len(jobs))) if _SLOTS.acquire(blocking=False))
    if slots == 0:
        raise PriceSourceUnreachable("The price source is busy: every download slot is in use",
                                     {"busy": True, "slots": MAX_CONCURRENT})
    lock = threading.Lock()
    queue = list(jobs)
    state = {"stop": False, "net_failed": 0, "failed": 0}
    failures: Dict[str, str] = {}

    def worker():
        try:
            while True:
                with lock:
                    if state["stop"] or not queue:
                        return
                    aid, sym = queue.pop(0)
                try:
                    closes = daily_closes(providers[aid], sym, start, now=now)
                except Exception as e:  # noqa: BLE001 -- a provider raises its own types; one symbol's failure = unpriced
                    reason = _redacted(e)
                    logger.warning(f"price history fetch failed for {sym}: {reason}")
                    with lock:
                        failures[sym] = reason
                        state["failed"] += 1
                        if isinstance(e, OSError):
                            state["net_failed"] += 1
                    continue
                with lock:
                    if closes:
                        out[sym] = closes
        finally:
            _SLOTS.release()

    threads = [threading.Thread(target=worker, daemon=True, name="price-history") for _ in range(slots)]
    for t in threads:
        t.start()
    end_at = time.monotonic() + deadline
    for t in threads:
        t.join(max(0.0, end_at - time.monotonic()))
    with lock:
        timed_out = any(t.is_alive() for t in threads)
        if timed_out:
            state["stop"] = True                   # running calls finish and free their slot; nothing new starts
        result = Closes(out)
        result.timed_out = timed_out
        result.unfetched = sorted({sym for _aid, sym in queue})
        failed, net_failed = state["failed"], state["net_failed"]
        if errors is not None:
            errors.update(failures)
    if timed_out and not result:
        raise PriceSourceUnreachable(f"The price source did not answer within {deadline:.0f}s",
                                     {"symbols": len(jobs)})
    if not result and failed == len(jobs) and net_failed == failed:
        raise PriceSourceUnreachable("The price source failed for every symbol", {"symbols": len(jobs)})
    return result
