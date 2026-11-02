"""Fractional-share eligibility ON DISK, for backtests. File I/O only -- no network.

A backtest has no broker to ask. The live answer to "can AAPL be traded in fractions?"
comes from ``AccountInterface.get_fractionable`` (a broker call, cached a day); a backtest
answers the same question from this file, which ``ba2-test prewarm`` refreshes with one
bulk broker call. The file lives under ``CACHE_FOLDER``, so remote GA workers receive it
with the rest of the cache sync -- the same channel the FRED macro series use.

HERMETIC BY CONSTRUCTION. Nothing here reaches for the network. A missing file, an
unreadable one, or a symbol it does not list is simply UNKNOWN, and the share grid sizes
unknown in whole shares. So a backtest run without a warm file behaves exactly as it did
before fractional support existed -- conservative, never a fraction nobody verified.

THE SAME ANSWER FOR EVERY TRIAL. The map is memoised on the file's modification time, so a
worker running thousands of trials reads and parses it once, and a prewarm that rewrites
it mid-campaign is picked up by the next trial rather than silently ignored.

TRI-STATE, as everywhere: ``True`` / ``False`` are what the broker said; a symbol the file
does not list is absent, and callers read absence as ``None``, never as ``False``.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from ba2_common.logger import logger

#: Sub-folder of CACHE_FOLDER. Its own folder rather than a loose file at the cache root,
#: so a future per-broker file (IBKR's eligibility differs from Alpaca's) has somewhere to go.
SUBDIR = "symbol_facts"
FILENAME = "fractionable.json"

_lock = threading.Lock()
#: ``(path, mtime_ns, map)`` of the last successful read.
_memo: Optional[Tuple[str, int, Dict[str, bool]]] = None


def store_path() -> str:
    """Absolute path of the file. Read from config at CALL time, never at import, so a
    process that points ``CACHE_FOLDER`` elsewhere after importing this module (every test,
    and the backtest worker's own cache override) gets the folder it configured."""
    from ba2_common import config
    return os.path.join(config.CACHE_FOLDER, SUBDIR, FILENAME)


def load_fractionable_map() -> Dict[str, bool]:
    """``{SYMBOL: True/False}`` as last written, or ``{}`` when there is no usable file.

    ``{}`` for a missing file is the NORMAL cold state and is not logged above debug. A
    file that exists but cannot be parsed IS logged, as a warning: that is a warm cache
    somebody believes they have, silently sizing every symbol in whole shares.

    Only real booleans survive the read. A hand-edited ``"AAPL": "yes"`` is dropped --
    the symbol reads as unknown -- rather than being coerced with ``bool()``, which would
    turn the string ``"false"`` into ``True``.
    """
    global _memo
    path = store_path()
    try:
        mtime = os.stat(path).st_mtime_ns
    except FileNotFoundError:
        logger.debug(f"No fractionable store at {path}; every symbol sizes in whole shares")
        return {}
    except OSError as e:
        logger.warning(f"Fractionable store {path} unreadable ({e}); whole shares for all")
        return {}

    with _lock:
        if _memo is not None and _memo[0] == path and _memo[1] == mtime:
            return _memo[2]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            raw = payload.get("symbols") if isinstance(payload, dict) else None
            if not isinstance(raw, dict):
                raise ValueError("no 'symbols' object")
        except (OSError, ValueError) as e:
            logger.warning(f"Fractionable store {path} could not be parsed ({e}); "
                           f"every symbol will size in whole shares until it is rewritten")
            return {}
        cleaned = {str(k).strip().upper(): v for k, v in raw.items()
                   if str(k).strip() and (v is True or v is False)}
        _memo = (path, mtime, cleaned)
        return cleaned


def save_fractionable_map(symbols: Dict[str, bool], *, source: str) -> int:
    """Replace the file with ``symbols``. Returns the number of entries written.

    ATOMIC: written to a sibling temp file and ``os.replace``d into place, so a worker that
    reads while the prewarm writes sees either the old map or the new one, never half a
    JSON document.

    An EMPTY map is refused rather than written. An empty broker answer is a failed fetch
    (every broker lists thousands of equities), and writing it would replace a good warm
    file with one that sizes everything in whole shares -- a regression dressed as a
    refresh.
    """
    cleaned = {str(k).strip().upper(): v for k, v in (symbols or {}).items()
               if str(k).strip() and (v is True or v is False)}
    if not cleaned:
        raise ValueError("refusing to write an empty fractionable map over the existing store")
    path = store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(cleaned),
        "symbols": dict(sorted(cleaned.items())),
    }
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, path)
    return len(cleaned)


def store_age_hours(now: Optional[datetime] = None) -> Optional[float]:
    """Hours since the file was last written, or ``None`` when there is no file.

    Measured from the payload's own ``fetched_at`` rather than the file's mtime: a cache
    sync that copies the file to a worker stamps a fresh mtime on a stale answer.
    """
    path = store_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            fetched = datetime.fromisoformat(json.load(fh)["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - fetched).total_seconds() / 3600.0)


def clear_memo() -> None:
    """Drop the in-process memo. For tests; production relies on the mtime check."""
    global _memo
    with _lock:
        _memo = None
