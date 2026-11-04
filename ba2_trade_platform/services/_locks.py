"""The TradeManager per-expert processing lock, for management-API writes (design section 3)."""
from contextlib import contextmanager
from typing import Optional

from ..core.TradeManager import get_trade_manager
from .errors import Conflict

#: How long a write waits for a running pass before answering 409. An entry pass holds the lock for as long as
#: it sizes and submits orders, so this is deliberately short: the caller can simply retry.
LOCK_WAIT_S = 3.0


@contextmanager
def expert_write_lock(expert_id: Optional[int], timeout: float = LOCK_WAIT_S):
    """Hold the expert's enter_market and open_positions locks for the duration of the block, or raise
    ``Conflict`` if a pass keeps them past ``timeout``. ``None`` (a transaction with no expert) takes no lock."""
    if expert_id is None:
        yield
        return
    with get_trade_manager().hold_expert_locks(expert_id, timeout) as held:
        if not held:
            raise Conflict(f"Expert {expert_id} is being processed (an analysis pass holds its lock); "
                           f"retry in a moment", {"expert_id": expert_id})
        yield
