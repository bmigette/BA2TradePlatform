"""Pass-level health of the live price reads: ONE ERROR for a broker outage, not hundreds of lines.

Every expert prices from ``account.get_instrument_current_price`` (one quote call per symbol). With the
broker down, each symbol produced an adapter ERROR/WARNING plus an expert "no current price" ERROR. The
per-symbol lines are WARNING/DEBUG now; this module counts the symbols of an analysis pass that ended
with a ``no_price`` skip, and when the pass ends (``WorkerQueue``'s batch-end hook) logs ONE ERROR if
more than ``NO_PRICE_SHARE_THRESHOLD`` of the pass's symbols (and at least ``NO_PRICE_MIN_SYMBOLS``)
could not be priced: the expert instance, the account, the count and the first error.

Nothing here changes what is traded: it observes and reports.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from ba2_common.logger import logger

#: A pass is an incident when MORE than this share of its symbols ended with a no_price skip...
NO_PRICE_SHARE_THRESHOLD = 0.20
#: ...and at least this many symbols did (two unlucky symbols of a three-symbol pass are not an outage).
NO_PRICE_MIN_SYMBOLS = 5

_lock = threading.Lock()
_no_price: Dict[int, List[str]] = {}          # expert instance id -> symbols skipped as no_price this pass
_first_error: Dict[int, str] = {}             # expert instance id -> the first price-read error text


def note_error(expert_id: Optional[int], symbol: str, error: BaseException) -> None:
    """Remember the FIRST price-read error of the current pass for ``expert_id`` (for the summary)."""
    if expert_id is None:
        return
    with _lock:
        _first_error.setdefault(expert_id, f"{symbol}: {type(error).__name__}: {error}")


def record_no_price(expert_id: Optional[int], symbol: str) -> None:
    """A symbol of this pass ended with a ``no_price`` skip."""
    if expert_id is None:
        return
    with _lock:
        _no_price.setdefault(expert_id, []).append(symbol)


def summarise_pass(expert_id: Optional[int], total_symbols: int, *, account_id: Optional[int] = None,
                   batch_id: Optional[str] = None) -> Optional[str]:
    """Close the pass of ``expert_id``: reset its counters and, when the no_price share is over the
    threshold, log ONE ERROR and return its text (else None)."""
    if expert_id is None:
        return None
    with _lock:
        symbols = _no_price.pop(expert_id, [])
        first = _first_error.pop(expert_id, None)
    count = len(set(symbols))
    if count < NO_PRICE_MIN_SYMBOLS or total_symbols <= 0 or count / total_symbols <= NO_PRICE_SHARE_THRESHOLD:
        return None
    message = (f"PRICE OUTAGE: expert instance {expert_id} (account {account_id}"
               f"{', batch ' + batch_id if batch_id else ''}): {count} of {total_symbols} symbols of this "
               f"pass could not be priced and were skipped as no_price "
               f"(> {NO_PRICE_SHARE_THRESHOLD:.0%}); first error: {first or 'none recorded (empty quote)'}. "
               f"Check the broker / market-data connection; the per-symbol lines are WARNINGs.")
    logger.error(message)
    return message


def reset() -> None:
    """Tests."""
    with _lock:
        _no_price.clear()
        _first_error.clear()
