"""Real-time FMP last price for broker accounts (the `fmp` live price source).

The FMP call is the SAME one PennyMomentumTrader's ``vendor_live_price = fmp`` uses
(``fmpsdk.quote`` through the global ``fmp_list_call`` rate-limit gate), minus its
"fall back to account prices when the key is missing" behaviour.

Live-only: used by broker accounts to price instruments NOW. FMP ``/quote`` carries a last
``price`` but NO bid/ask, so this helper yields a last price only.

Rules (CLAUDE.md "Live Data - No Fallbacks"): a symbol FMP does not return, returns with a
non-positive / null price, or whose chunk fails, is ABSENT from the result and named in a log
line. Nothing here ever substitutes another source, and the result is never cached (callers
own the price-cache TTL; ``fmp_live_cached``'s 15 minutes would defeat the purpose).
"""
from typing import Callable, Dict, Iterable, List, Optional

from ba2_common.config import get_app_setting
from ba2_providers.fmp_common import FMPError, fmp_list_call, redact

from ..logger import logger

FMP_QUOTE_BATCH = 100


class FMPPriceError(RuntimeError):
    """FMP cannot be used at all (e.g. no API key configured)."""


def _to_fmp(symbol: str) -> str:
    # Alpaca writes share classes BRK.B, FMP writes BRK-B.
    return symbol.upper().replace(".", "-")


def _fmpsdk_quote(api_key: str, symbols: List[str]):
    import fmpsdk
    return fmpsdk.quote(apikey=api_key, symbol=symbols)


def fetch_fmp_last_prices(symbols: Iterable[str], *, api_key: Optional[str] = None,
                          quote_fn: Optional[Callable] = None) -> Dict[str, float]:
    """Return ``{symbol: last price}`` for the symbols FMP priced above zero.

    One HTTP call per ``FMP_QUOTE_BATCH`` symbols. Keys are the caller's spelling.
    Raises ``FMPPriceError`` when no API key is configured.
    """
    wanted: List[str] = list(dict.fromkeys(symbols))
    if not wanted:
        return {}
    key = api_key or get_app_setting("FMP_API_KEY")
    if not key:
        raise FMPPriceError("FMP_API_KEY is not configured; the fmp price feed cannot be used")

    back = {_to_fmp(s): s for s in wanted}
    fmp_syms = list(back)
    result: Dict[str, float] = {}
    for i in range(0, len(fmp_syms), FMP_QUOTE_BATCH):
        chunk = fmp_syms[i:i + FMP_QUOTE_BATCH]
        try:
            call = quote_fn or _fmpsdk_quote
            rows = fmp_list_call(lambda: call(key, chunk), endpoint="quote")
        except Exception as e:  # noqa: BLE001 -- the chunk's symbols are reported unpriced below
            kind = "FMPError" if isinstance(e, FMPError) else type(e).__name__
            logger.error(f"FMP price feed: quote call failed ({kind}: {redact(e)}) for "
                         f"{len(chunk)} symbols: {', '.join(chunk)}")  # no exc_info: the traceback would carry the unredacted URL/key
            continue
        if not isinstance(rows, list):
            logger.error(f"FMP price feed: unexpected quote payload {type(rows).__name__} for "
                         f"{', '.join(chunk)}")
            continue
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            price = row.get("price")
            if sym in back and isinstance(price, (int, float)) and not isinstance(price, bool) \
                    and price > 0:
                result[back[sym]] = float(price)

    missing = [s for s in wanted if s not in result]
    if missing:
        logger.warning(f"FMP price feed: no usable price for {len(missing)} of {len(wanted)} "
                       f"symbols: {', '.join(missing)}")
    return result
