"""Tables of the allocator's per-symbol TP/SL protection layer (TastyTrade).

LIVE-ONLY and deliberately IN-TREE (not in ``ba2_common``): the table exists to track
orders placed at a broker, nothing the backtest engine or the GA can reach may import it, and
keeping it out of ``packages/`` is what makes the whole feature GA-neutral (no
``PACKAGE_VERSION`` bump, no ``ga_neutral_package_paths`` entry). See
``docs/plans/2026-10-04-allocator-tp-sl-design.md``.

Because ``ba2_common.core.db.init_db`` cannot import an in-tree module, registration with the
shared ``SQLModel.metadata`` is the job of whoever imports this module first:
``main.initialize_system`` does it BEFORE ``init_db()`` (so ``create_all`` builds the tables on
any database), ``alembic/env.py`` does it for autogenerate, and the store imports it.
"""
from datetime import datetime as DateTime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, UniqueConstraint
from sqlmodel import Column, Field, SQLModel


def _utcnow() -> DateTime:
    return DateTime.now(timezone.utc)


# --- slice states (plain str column: never a bare literal outside this block) -------------
SLICE_PLACING = "PLACING"            # row written, broker call not yet answered
SLICE_LIVE = "LIVE"                  # complex order resting at the broker
SLICE_CANCELLING = "CANCELLING"      # we asked, the broker has not confirmed
SLICE_CANCELLED_BY_US = "CANCELLED_BY_US"
SLICE_FILLED_TP = "FILLED_TP"        # take-profit leg filled (fully or partly)
SLICE_FILLED_SL = "FILLED_SL"        # stop leg filled (fully or partly)
SLICE_LOST_EXPIRED = "LOST_EXPIRED"      # alarm: the broker expired it
SLICE_LOST_CANCELLED = "LOST_CANCELLED"  # alarm: cancelled and we did not ask
SLICE_LOST_REJECTED = "LOST_REJECTED"    # alarm: rejected after acceptance
SLICE_UNKNOWN = "UNKNOWN"                # alarm: state could not be read as any of the above

#: Slices that no longer rest at the broker and are history, not alarms.
SLICE_HISTORY_STATES = frozenset({SLICE_CANCELLED_BY_US, SLICE_FILLED_TP, SLICE_FILLED_SL})
#: Slices that are alarms: protection that was supposed to exist and does not, or cannot be
#: shown to exist.
SLICE_ALARM_STATES = frozenset({SLICE_LOST_EXPIRED, SLICE_LOST_CANCELLED,
                                SLICE_LOST_REJECTED, SLICE_UNKNOWN})
#: Slices that (may) still rest at the broker: they reserve shares.
SLICE_RESTING_STATES = frozenset({SLICE_PLACING, SLICE_LIVE, SLICE_CANCELLING})


class AllocatorProtection(SQLModel, table=True):
    """The operator's TP/SL configuration for ONE symbol of ONE account, plus its state.

    One row per ``(account_id, symbol)``. Absence of a row means OFF -- the default for every
    symbol. ``enabled`` False means the operator switched it off (the numbers are kept so they
    can be switched back on, but no order is live). ``held_at`` set means a protective order
    FILLED and the allocator must not trade the symbol until the operator re-enables it.

    ``tp_targets`` is ``[{"price": float, "fraction": float}, ...]``: ``fraction`` is the share
    of the protected position that target covers, summing to 1.0. Prices are stored AS ENTERED
    (the tick rounding is applied at order time and recorded on the slice).

    ``pending_replace`` is the durable marker for "we cancelled the orders ourselves (a
    rebalance, a resize) and still owe the re-placement": set BEFORE the first cancel, cleared
    only after the new slices are live, so a crash in between is visible to the next reconcile
    instead of leaving a position silently naked.
    """
    __tablename__ = "allocator_protection"
    __table_args__ = (
        UniqueConstraint('account_id', 'symbol', name='uix_alloc_protection_account_symbol'),
    )

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    symbol: str = Field(index=True, description="Upper-cased symbol")
    enabled: bool = Field(default=False, description="Operator intent; False = no orders live")
    sl_price: float = Field(default=0.0, description="The one stop-loss price")
    tp_targets: List[Dict[str, Any]] = Field(
        sa_column=Column(JSON), default_factory=list,
        description='[{"price": float, "fraction": float}], fractions sum to 1')
    held_at: DateTime | None = Field(default=None, description="A protective order filled; allocator must skip the symbol")
    held_reason: str | None = Field(default=None)
    pending_replace: bool = Field(default=False, description="We cancelled the orders and owe the re-placement")
    pending_replace_since: DateTime | None = Field(default=None)
    alert_code: str | None = Field(default=None, description="Last loud alert code; None = no open alert")
    alert_message: str | None = Field(default=None)
    alerted_at: DateTime | None = Field(default=None)
    last_error: str | None = Field(default=None, description="Last refusal text, shown in the dialog")
    protected_quantity: float = Field(default=0.0, description="Whole shares covered by LIVE slices at the last reconcile (display cache)")
    created_at: DateTime = Field(default_factory=_utcnow)
    updated_at: DateTime = Field(default_factory=_utcnow)


class AllocatorProtectionOrder(SQLModel, table=True):
    """One OCO complex order (one take-profit target's slice) of a protection.

    ``quantity`` is WHOLE shares (TastyTrade refuses a fractional quantity on every priced
    order). The prices are the ones SENT (after tick rounding). ``complex_order_id`` is
    ``None`` only while ``state == PLACING`` (row written before the broker call so a crash in
    the call leaves evidence) or when the broker refused.
    """
    __tablename__ = "allocator_protection_order"

    id: int | None = Field(default=None, primary_key=True)
    protection_id: int = Field(foreign_key="allocator_protection.id", ondelete="CASCADE", index=True)
    slice_index: int = Field(default=0)
    target_index: int = Field(default=0, description="Index into AllocatorProtection.tp_targets at placement time")
    quantity: int = Field(default=0)
    tp_price: float = Field(default=0.0)
    sl_price: float = Field(default=0.0)
    complex_order_id: int | None = Field(default=None, index=True)
    tp_order_id: int | None = Field(default=None)
    sl_order_id: int | None = Field(default=None)
    external_tag: str | None = Field(default=None, description="external_identifier sent on both legs")
    state: str = Field(default=SLICE_PLACING, index=True)
    filled_qty: float = Field(default=0.0)
    fill_price: float | None = Field(default=None)
    gtc_date: DateTime | None = Field(default=None)
    gtc_date_assumed: bool = Field(default=False)
    gtc_warned: bool = Field(default=False, description="The expiry warning for this slice was already raised")
    cancel_requested: bool = Field(default=False, description="WE asked for the cancel (so a CANCELLED result is not an alarm)")
    detail: str | None = Field(default=None, description="Last broker/classification note")
    placed_at: DateTime = Field(default_factory=_utcnow)
    closed_at: DateTime | None = Field(default=None)
