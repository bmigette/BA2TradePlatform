"""Real-time FMP last price for broker accounts (the `fmp` live price source).

The FMP call is the SAME one PennyMomentumTrader's ``vendor_live_price = fmp`` uses
(``fmpsdk.quote`` through the global ``fmp_list_call`` rate-limit gate), minus its
"fall back to account prices when the key is missing" behaviour.

Live-only: used by broker accounts to price instruments NOW. FMP ``/quote`` carries a last
``price`` and a ``timestamp`` but NO bid/ask, so this helper yields a last price only.

Rules (CLAUDE.md "Live Data - No Fallbacks"): a symbol FMP does not return, returns with a
non-positive / non-numeric price, returns STALE, or whose chunk fails, is ABSENT from the
result and named in a log line. Nothing here ever substitutes another source, and the result
is never cached (callers own the price-cache TTL).

Staleness (row ``timestamp``, epoch seconds), judged against the NYSE calendar:
  * regular session open: the quote must be <= 15 min old; during the first 15 min of the
    session the previous finished session close (minus tolerance) also passes, because a
    09:30 decision legitimately sees the prior close (what the backtest sees: the last bar
    that ENDED);
  * otherwise: the quote must be at/after the most recent FINISHED session close minus 5 min.
Holidays, weekends and half days come from the calendar data.

No retry sleeps: a 429/error dict makes the chunk unpriced at once (a price call must never
block for 15-60 s).
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional

from ba2_common.config import get_app_setting
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_providers.fmp_common import FMPError, FMPHermeticViolation, fmp_list_call, redact

from ..logger import logger

FMP_QUOTE_BATCH = 100
MAX_AGE_OPEN_SESSION = timedelta(minutes=15)
CLOSE_TOLERANCE = timedelta(minutes=5)


class FMPPriceError(RuntimeError):
    """FMP cannot be used at all (e.g. no API key configured)."""


class _RedactFilter(logging.Filter):
    """Scrub ``apikey=...`` from a record message and traceback text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = redact(record.exc_text)
        except Exception:  # noqa: BLE001 -- a logging filter must never raise
            record.msg = "<log record withheld: redaction failed>"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def install_fmpsdk_log_redaction() -> None:
    """fmpsdk logs request exceptions (URL incl. ``?apikey=``) via the ROOT logger. Put an
    idempotent redacting filter on the root logger, the fmpsdk loggers and every root handler."""
    targets = [logging.getLogger(), logging.getLogger("fmpsdk")]
    targets += list(logging.getLogger().handlers)
    for t in targets:
        if not any(isinstance(f, _RedactFilter) for f in t.filters):
            t.addFilter(_RedactFilter())


def _to_fmp(symbol: str) -> str:
    # Alpaca writes share classes BRK.B, FMP writes BRK-B.
    return symbol.upper().replace(".", "-")


def _fmpsdk_quote(api_key: str, symbols: List[str]):
    import fmpsdk
    return fmpsdk.quote(apikey=api_key, symbol=symbols)


def _oldest_acceptable(now: datetime) -> datetime:
    """The earliest quote time that is still current at ``now`` (see module docstring)."""
    local_day = now.astimezone(NY_TZ).date()
    sessions = nyse_regular_sessions(local_day - timedelta(days=12), local_day + timedelta(days=1))
    finished = [c for _o, c in sessions if c <= now]
    if not finished:
        raise FMPPriceError(f"no finished NYSE session found before {now.isoformat()}")
    last_close = finished[-1]
    current = next(((o, c) for o, c in sessions if o <= now < c), None)
    if current is None:
        return last_close - CLOSE_TOLERANCE
    lower = now - MAX_AGE_OPEN_SESSION
    if now - current[0] < MAX_AGE_OPEN_SESSION:
        lower = min(lower, last_close - CLOSE_TOLERANCE)
    return lower


def _quote_time(row: dict) -> Optional[datetime]:
    ts = row.get("timestamp")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def fetch_fmp_last_prices(symbols: Iterable[str], *, api_key: Optional[str] = None,
                          quote_fn: Optional[Callable] = None,
                          now: Optional[datetime] = None) -> Dict[str, float]:
    """Return ``{symbol: last price}`` for the symbols FMP priced above zero and currently.

    One HTTP call per ``FMP_QUOTE_BATCH`` symbols. Keys are the caller spellings (one FMP
    symbol can serve several, e.g. BRK.B and BRK-B). Raises ``FMPPriceError`` when no API key
    is configured; ``FMPHermeticViolation`` is never swallowed.
    """
    wanted: List[str] = list(dict.fromkeys(symbols))
    if not wanted:
        return {}
    key = api_key or get_app_setting("FMP_API_KEY")
    if not key:
        raise FMPPriceError("FMP_API_KEY is not configured; the fmp price feed cannot be used")
    install_fmpsdk_log_redaction()
    now = now or datetime.now(timezone.utc)
    oldest = _oldest_acceptable(now)

    back: Dict[str, List[str]] = {}
    for s in wanted:
        back.setdefault(_to_fmp(s), []).append(s)
    fmp_syms = list(back)
    result: Dict[str, float] = {}
    call = quote_fn or _fmpsdk_quote
    for i in range(0, len(fmp_syms), FMP_QUOTE_BATCH):
        chunk = fmp_syms[i:i + FMP_QUOTE_BATCH]
        try:
            rows = fmp_list_call(lambda: call(key, chunk), endpoint="quote", delays=())
        except FMPHermeticViolation:
            raise
        except Exception as e:  # noqa: BLE001 -- the chunk symbols are reported unpriced
            kind = "FMPError" if isinstance(e, FMPError) else type(e).__name__
            # no exc_info: the traceback would carry the request URL
            logger.error(f"FMP price feed: quote call failed ({kind}: {redact(e)}) for "
                         f"{len(chunk)} symbols: {', '.join(chunk)}")
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper()
            price = row.get("price")
            if sym not in back or isinstance(price, bool) \
                    or not isinstance(price, (int, float)) or not price > 0:
                continue
            qt = _quote_time(row)
            if qt is None:
                logger.warning(f"FMP price feed: {sym} quote has no usable timestamp "
                               f"({row.get('timestamp')!r}); treated as unpriced")
                continue
            if qt < oldest or qt > now + CLOSE_TOLERANCE:
                logger.warning(f"FMP price feed: {sym} quote is stale/out of range: quote time "
                               f"{qt.isoformat()}, acceptable from {oldest.isoformat()} "
                               f"(now {now.isoformat()}); treated as unpriced")
                continue
            for caller_spelling in back[sym]:
                result[caller_spelling] = float(price)

    missing = [s for s in wanted if s not in result]
    if missing:
        logger.warning(f"FMP price feed: no usable price for {len(missing)} of {len(wanted)} "
                       f"symbols: {', '.join(missing)}")
    return result
