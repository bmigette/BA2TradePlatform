"""One ``get_positions`` per account per short window, for the management API only.

Outside ``share_positions()`` (the UI, the schedulers, the other API endpoints) ``read_positions`` is exactly
``account.get_positions()``. Inside it, a NON-None answer is kept ``POSITIONS_TTL_S`` seconds per account, so the
sections of one ``/stats/overview`` request and a burst of ``/transactions`` calls share ONE broker read. ``None`` (the
fetch failed) and exceptions are never cached."""
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Tuple

POSITIONS_TTL_S = 15.0
_ON: ContextVar[bool] = ContextVar("ba2_share_positions", default=False)
_CACHE: Dict[int, Tuple[float, Any]] = {}
_LOCK = threading.Lock()


@contextmanager
def share_positions():
    token = _ON.set(True)
    try:
        yield
    finally:
        _ON.reset(token)


def read_positions(account, account_id: int):
    if not _ON.get():
        return account.get_positions()
    with _LOCK:
        hit = _CACHE[account_id] if account_id in _CACHE else None
        if hit is not None and time.monotonic() - hit[0] < POSITIONS_TTL_S:
            return list(hit[1])
    positions = account.get_positions()
    if positions is not None:
        with _LOCK:
            _CACHE[account_id] = (time.monotonic(), list(positions))
    return positions


def clear() -> None:
    with _LOCK:
        _CACHE.clear()
