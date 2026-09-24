"""Wash-trade markers carried on a ``TradingOrder``.

WHY A MARKER AT ALL. ``AccountInterface.submit_order`` reacts to a wash-trade blocker in
one of two ways (see ``docs/WASHTRADE-LOCK.md``): it either holds the order back as
``WASHTRADE_LOCKED``, which is visible in the order's own status, or it submits the order
as a COMPLEX order on the broker's documented exemption — and that second branch leaves
NO trace. When the broker then cancels the order anyway (measured 2026-09-22, see the
dated section of that document), all the database keeps is a CANCELED order with zero
fill, indistinguishable from any other cancellation. The rejection is then undiagnosable
and, worse, unclassifiable by the give-up path that has to clean up after it.

So the submit branch stamps what it did, and the give-up path stamps what the broker
answered. Both live in ``TradingOrder.data`` (a JSON column) under their own keys, so
nothing else that reads ``data`` is disturbed.

Pure helpers: they mutate the passed object and never touch the database. The caller
persists (``update_instance``), which keeps this module usable from the backtest, from
tests, and from either side of the package boundary.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

# ``TradingOrder.data`` keys. Stable strings: they are written to the live database and
# an operator greps the logs for them.
WASHTRADE_COMPLEX_SUBMIT_KEY = "washtrade_complex_submit"
WASHTRADE_REJECTED_KEY = "washtrade_rejected"


def _with_data(order, key: str, value: Dict[str, Any]) -> None:
    """Set ``order.data[key] = value`` by REBINDING ``data`` to a new dict.

    A JSON column mutated in place is not seen as dirty by SQLAlchemy unless the column
    is declared mutable, so an in-place ``order.data[key] = ...`` would be silently
    dropped on flush. Rebinding is what makes the write actually persist.
    """
    existing = getattr(order, "data", None)
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged[key] = value
    order.data = merged


def stamp_complex_submit(order, blocker) -> None:
    """Record that ``order`` went to the broker as a COMPLEX order because ``blocker``
    was working against it — i.e. it took the wash-trade exemption instead of locking.

    Idempotent in effect: a re-submission simply overwrites the stamp with the newer
    attempt, which is the one whose outcome matters.
    """
    _with_data(order, WASHTRADE_COMPLEX_SUBMIT_KEY, {
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "blocker_order_id": getattr(blocker, "id", None),
        "blocker_order_type": _enum_value(getattr(blocker, "order_type", None)),
        "blocker_status": _enum_value(getattr(blocker, "status", None)),
    })


def went_out_as_contended_complex(order) -> bool:
    """Did ``order`` take the complex-order exemption against a wash-trade blocker?"""
    data = getattr(order, "data", None)
    return isinstance(data, dict) and WASHTRADE_COMPLEX_SUBMIT_KEY in data


def complex_submit_record(order) -> Optional[Dict[str, Any]]:
    """The stamp written by :func:`stamp_complex_submit`, or None."""
    data = getattr(order, "data", None)
    if not isinstance(data, dict):
        return None
    record = data.get(WASHTRADE_COMPLEX_SUBMIT_KEY)
    return record if isinstance(record, dict) else None


def stamp_rejection(order, final_status) -> Dict[str, Any]:
    """Record that the broker killed a contended COMPLEX order without filling it —
    the wash-trade rejection the exemption was supposed to prevent.

    Returns the record it wrote, so the caller can name the blocker in its own log line.
    """
    submitted = complex_submit_record(order) or {}
    record = {
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "final_status": _enum_value(final_status),
        "blocker_order_id": submitted.get("blocker_order_id"),
        "blocker_order_type": submitted.get("blocker_order_type"),
        "submitted_at": submitted.get("submitted_at"),
    }
    _with_data(order, WASHTRADE_REJECTED_KEY, record)
    return record


def was_rejected_as_washtrade(order) -> bool:
    """Has :func:`stamp_rejection` already run on this order?"""
    data = getattr(order, "data", None)
    return isinstance(data, dict) and WASHTRADE_REJECTED_KEY in data


def _enum_value(value):
    """``OrderStatus.CANCELED`` -> ``"canceled"``; anything else unchanged (JSON-safe)."""
    return getattr(value, "value", value)
