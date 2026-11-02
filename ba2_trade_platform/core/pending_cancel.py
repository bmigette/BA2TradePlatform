"""PENDING_CANCEL bookkeeping shared by every broker adapter.

A cancel is a WAIT for the broker's confirmation, and the 24 h give-up limit on that wait
must be measured from the moment the cancel was REQUESTED, not from ``created_at`` (a
protective leg of a position held for days is "older than 24 h" the instant it is
cancelled). The moment is stored in ``TradingOrder.data["pending_cancel_since"]`` (UTC ISO
string) so no schema migration is needed.

Every place that moves an order into PENDING_CANCEL must call :func:`mark_pending_cancel`.
"""
from datetime import datetime, timezone
from typing import Optional

from .types import OrderStatus

PENDING_CANCEL_SINCE_KEY = "pending_cancel_since"


def mark_pending_cancel(order, now: Optional[datetime] = None) -> None:
    """Set ``order.status`` to PENDING_CANCEL and stamp when. Caller persists the row.

    ``data`` is replaced by a new dict (a JSON column does not track in-place mutation).
    """
    moment = now or datetime.now(timezone.utc)
    data = dict(order.data) if order.data else {}
    data[PENDING_CANCEL_SINCE_KEY] = moment.astimezone(timezone.utc).isoformat()
    order.data = data
    order.status = OrderStatus.PENDING_CANCEL


def get_pending_cancel_since(order_data) -> Optional[datetime]:
    """The recorded cancel moment (aware UTC), or None when absent (legacy row).

    A present-but-unparseable stamp is a corrupt record and raises: it must not read as
    'no stamp' (which would silently restart the wait).
    """
    if not order_data:
        return None
    raw = order_data.get(PENDING_CANCEL_SINCE_KEY)
    if raw is None:
        return None
    moment = datetime.fromisoformat(raw)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
