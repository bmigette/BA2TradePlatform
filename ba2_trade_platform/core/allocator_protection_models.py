"""Tables of the allocator's per-symbol TP/SL protection layer and manual exclusions.

LIVE-ONLY and deliberately IN-TREE (not in ``ba2_common``): the tables track orders placed at a
broker and operator decisions about live trading, nothing the backtest engine or the GA can reach
may import them, and keeping them out of ``packages/`` is what makes the whole feature GA-neutral
(no ``PACKAGE_VERSION`` bump, no ``ga_neutral_package_paths`` entry). See
``docs/plans/2026-10-04-allocator-tp-sl-design.md``.

Because ``ba2_common.core.db.init_db`` cannot import an in-tree module, registration with the
shared ``SQLModel.metadata`` is the job of whoever imports this module first:
``main.initialize_system`` does it BEFORE ``init_db()`` (so ``create_all`` builds the tables on
any database), ``alembic/env.py`` does it for autogenerate, and the stores import it.

FOUR tables: ``allocator_protection`` (+ ``allocator_protection_order`` per broker order),
``allocator_exclusion`` (the ONE way a symbol is kept out of allocation: the operator's manual
"disable") and ``allocator_weight_change`` (the audit trail of the automatic weight reductions a
protective fill makes).
"""
from datetime import datetime as DateTime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, UniqueConstraint
from sqlmodel import Column, Field, SQLModel


def _utcnow() -> DateTime:
    return DateTime.now(timezone.utc)


# --- order kinds ------------------------------------------------------------------------
ORDER_KIND_OCO = "OCO"      # take-profit limit OR stop, as one broker complex order
ORDER_KIND_STOP = "STOP"    # a plain GTC stop-only sell (the runner no take-profit covers)

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

# --- exclusion ---------------------------------------------------------------------------
#: The ONLY reason a symbol is kept out of allocation: the operator disabled it by hand.
#: (A TP/SL fill does NOT exclude a symbol: protection never takes a symbol out of the
#: rebalance, it is re-placed on the new quantity after each one.)
EXCLUDED_DISABLED = "disabled"

# --- automatic weight changes ------------------------------------------------------------
WEIGHT_REASON_TP_FILL = "tp_fill"
WEIGHT_REASON_SL_FILL = "sl_fill"


class AllocatorProtection(SQLModel, table=True):
    """The operator's TP/SL configuration for ONE symbol of ONE account, plus its state.

    One row per ``(account_id, symbol)``. Absence of a row means OFF -- the default for every
    symbol. ``enabled`` False means the operator switched it off (the numbers are kept so they
    can be switched back on, but no order is live). A protection NEVER excludes the symbol from
    allocation: a rebalance cancels its orders, trades, and re-places them on the new quantity
    from THESE numbers (same stop, same target prices and fractions).

    ``tp_targets`` is ``[{"price": float, "fraction": float}, ...]``: ``fraction`` is the share
    of the protected position that target covers. They sum to AT MOST 1.0: the remainder (the
    "runner") is protected by a stop-only order, and an empty list means a stop for everything.
    Prices are stored AS ENTERED (the tick rounding is applied at order time and recorded on the
    slice).

    ``pending_replace`` is the durable marker for "we cancelled the orders ourselves (a
    rebalance, a resize) and still owe the re-placement": set BEFORE the first cancel, cleared
    only after the new slices are live, so a crash in between is visible to the next reconcile
    instead of leaving a position silently naked.

    ``last_fill_at`` / ``last_fill_note`` record the latest protective fill ("TP1 filled
    2026-10-03: share 6% -> 3%") for the row's small note; nothing reads them for a decision.
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
        description='[{"price": float, "fraction": float}], fractions sum to AT MOST 1; [] = stop only')
    pending_replace: bool = Field(default=False, description="We cancelled the orders and owe the re-placement")
    pending_replace_since: DateTime | None = Field(default=None)
    last_fill_at: DateTime | None = Field(default=None, description="When a protective order last filled")
    auto_failures: int = Field(default=0, description="Consecutive failed AUTOMATIC placements (growth / resize / renewal); the automatic paths stop at AUTO_FAILURE_LIMIT")
    last_auto_action_at: DateTime | None = Field(default=None, description="When the background reconcile last cancelled/re-placed on its own (rate limit)")
    disarmed_at: DateTime | None = Field(default=None, description="The position was exited: the settings were cleared (history below)")
    disarmed_note: str | None = Field(default=None, description="History record of the cleared settings: 'SL 45, TP 60@50%'")
    last_fill_note: str | None = Field(default=None, description="'TP1 filled 2026-10-03: share 6% -> 3%'")
    alert_code: str | None = Field(default=None, description="Last loud alert code; None = no open alert")
    alert_message: str | None = Field(default=None)
    alerted_at: DateTime | None = Field(default=None)
    last_error: str | None = Field(default=None, description="Last refusal text, shown in the dialog")
    protected_quantity: float = Field(default=0.0, description="Whole shares covered by LIVE slices at the last reconcile (display cache)")
    created_at: DateTime = Field(default_factory=_utcnow)
    updated_at: DateTime = Field(default_factory=_utcnow)


class AllocatorProtectionOrder(SQLModel, table=True):
    """One order of a protection: an OCO complex order (one take-profit target's slice, kind
    ``OCO``) or a plain stop-only order for the part no take-profit covers (kind ``STOP``).

    ``quantity`` is WHOLE shares (TastyTrade refuses a fractional quantity on every priced
    order). The prices are the ones SENT (after tick rounding). For an OCO the broker id is
    ``complex_order_id``; a STOP has no complex order, its id is ``sl_order_id`` and ``tp_price``
    is None. The broker id is ``None`` only while ``state == PLACING`` (row written before the
    broker call so a crash in the call leaves evidence) or when the broker refused.

    ``weight_applied_qty`` is the part of ``filled_qty`` the allocator weight has already been
    reduced for (so a slice that fills in several reconciles reduces the weight once per share).
    """
    __tablename__ = "allocator_protection_order"

    id: int | None = Field(default=None, primary_key=True)
    protection_id: int = Field(foreign_key="allocator_protection.id", ondelete="CASCADE", index=True)
    slice_index: int = Field(default=0)
    target_index: int = Field(default=0, description="Index into AllocatorProtection.tp_targets at placement time; -1 for a STOP slice")
    quantity: int = Field(default=0)
    kind: str = Field(default=ORDER_KIND_OCO, description="OCO | STOP (stop-only runner)")
    tp_price: float | None = Field(default=None, description="None for a STOP slice")
    sl_price: float = Field(default=0.0)
    complex_order_id: int | None = Field(default=None, index=True)
    tp_order_id: int | None = Field(default=None)
    sl_order_id: int | None = Field(default=None)
    external_tag: str | None = Field(default=None, description="external_identifier sent on both legs")
    state: str = Field(default=SLICE_PLACING, index=True)
    filled_qty: float = Field(default=0.0)
    weight_applied_qty: float = Field(default=0.0, description="Filled shares the allocator weight was already reduced for")
    fill_price: float | None = Field(default=None)
    gtc_date: DateTime | None = Field(default=None)
    gtc_date_assumed: bool = Field(default=False)
    gtc_warned: bool = Field(default=False, description="The expiry warning for this slice was already raised")
    cancel_requested: bool = Field(default=False, description="WE asked for the cancel (so a CANCELLED result is not an alarm)")
    detail: str | None = Field(default=None, description="Last broker/classification note")
    placed_at: DateTime = Field(default_factory=_utcnow)
    closed_at: DateTime | None = Field(default=None)


class AllocatorExclusion(SQLModel, table=True):
    """A symbol the OPERATOR switched OFF for allocation, for one account.

    THE one exclusion mechanism. The row's EXISTENCE is the flag (delete it to include the
    symbol again); ``excluded_reason`` is a plain str, today only ``EXCLUDED_DISABLED``. An
    excluded symbol is outside the managed money, like an unmanaged holding: the plan never
    emits an order for it, its value is not part of the label's value nor of the investable base,
    and its weight is ignored (the label's other symbols are normalised over the enabled ones).
    Excluding does NOT touch protective TP/SL orders (they stay exactly as they are).

    Per ACCOUNT and symbol, not per label: a symbol in two labels is excluded from both. It is
    separate from ``portfolio_allocation_symbol`` on purpose: that row is per label, lazily
    created and deleted with the membership, while this is a standing decision about a holding.
    """
    __tablename__ = "allocator_exclusion"
    __table_args__ = (
        UniqueConstraint('account_id', 'symbol', name='uix_alloc_exclusion_account_symbol'),
    )

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    symbol: str = Field(index=True, description="Upper-cased symbol")
    excluded_reason: str = Field(default=EXCLUDED_DISABLED, description="Plain str; today only 'disabled' (manual)")
    since: DateTime = Field(default_factory=_utcnow)
    note: str | None = Field(default=None, description="Optional operator note, e.g. 'bought manually'")


class AllocatorWeightChange(SQLModel, table=True):
    """Audit row: one AUTOMATIC change of a stored symbol weight, with its reason.

    Written when a protective TP/SL fill reduces a symbol's share of a label
    (``new = old x remaining protected qty / protected qty before the fill``; 0 when the
    protection exited the position). ``before_pct`` / ``after_pct`` are the stored weights, so
    the freed share (``before - after``) is auditable. It never touches
    ``previous_weight_pct`` (that column is "what the last RUN went out with" and only
    ``save_allocation_targets`` writes it).
    """
    __tablename__ = "allocator_weight_change"

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    label: str = Field(index=True)
    symbol: str = Field(index=True)
    reason: str = Field(description="tp_fill | sl_fill (plain str)")
    before_pct: float
    after_pct: float
    detail: str | None = Field(default=None, description="'TP1 filled: 4 of 10 protected sh sold @ 61.5'")
    created_at: DateTime = Field(default_factory=_utcnow, index=True)
