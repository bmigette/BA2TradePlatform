"""The locks of a management-API write (design section 3).

Order, everywhere: the expert's TradeManager processing locks FIRST, the per-transaction lock SECOND. Scheduled
passes only try-lock the expert locks (0.5 s) and skip when busy, so they cannot take part in a cycle.

* Expert locks: close, TP/SL and cancel hold only ``open_positions`` (an exit-side action must not make the day's
  ENTRY pass skip). Settings / enable / run PATCHes are short and hold both.
* Transaction lock: one plain ``threading.Lock`` per transaction id, taken by every API write on that transaction
  AND by the Live Trades page's close / TP/SL paths through the shared service, so an API close racing a UI close
  serializes. It also covers a transaction that has no expert."""
import threading
from contextlib import contextmanager
from typing import Dict, Optional

from ..core.TradeManager import get_trade_manager
from .errors import Conflict

#: How long a write waits for a running pass / another writer before answering 409. An entry pass holds its lock
#: for as long as it sizes and submits orders, so this is deliberately short: the caller can simply retry.
LOCK_WAIT_S = 3.0

EXIT_USE_CASES = ("open_positions",)

_transaction_locks: Dict[int, threading.Lock] = {}
_registry_lock = threading.Lock()


@contextmanager
def expert_write_lock(expert_id: Optional[int], timeout: Optional[float] = None, use_cases=None):
    """Hold the expert's processing locks (``use_cases``; default both) for the block, or raise ``Conflict`` if a
    pass keeps them past ``timeout``. ``None`` (a transaction with no expert) takes none."""
    if expert_id is None:
        yield
        return
    wait = LOCK_WAIT_S if timeout is None else timeout
    manager = get_trade_manager()
    cm = manager.hold_expert_locks(expert_id, wait) if use_cases is None \
        else manager.hold_expert_locks(expert_id, wait, use_cases)
    with cm as held:
        if not held:
            raise Conflict(f"Expert {expert_id} is being processed (an analysis pass holds its lock); "
                           f"retry in a moment", {"expert_id": expert_id})
        yield


def transaction_lock(transaction_id: int) -> threading.Lock:
    with _registry_lock:
        if transaction_id not in _transaction_locks:
            _transaction_locks[transaction_id] = threading.Lock()
        return _transaction_locks[transaction_id]


def acquire_transaction_lock(transaction_id: int, timeout: Optional[float] = None) -> threading.Lock:
    """Take the transaction's lock (the caller releases it) or raise ``Conflict``."""
    lock = transaction_lock(transaction_id)
    if not lock.acquire(blocking=True, timeout=LOCK_WAIT_S if timeout is None else timeout):
        raise Conflict(f"Transaction {transaction_id} is being changed by another request; retry in a moment",
                       {"id": transaction_id})
    return lock


@contextmanager
def transaction_write_lock(transaction_id: int, expert_id: Optional[int] = None, *, with_expert: bool = True,
                           timeout: Optional[float] = None):
    """Expert exit lock (when ``with_expert`` and the transaction has an expert), then the transaction lock."""
    if with_expert:
        with expert_write_lock(expert_id, timeout, EXIT_USE_CASES):
            lock = acquire_transaction_lock(transaction_id, timeout)
            try:
                yield
            finally:
                lock.release()
    else:
        lock = acquire_transaction_lock(transaction_id, timeout)
        try:
            yield
        finally:
            lock.release()
