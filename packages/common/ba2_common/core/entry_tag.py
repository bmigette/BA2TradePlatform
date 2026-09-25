"""The style-group ENTRY TAG: which member of a composed option expert opened a position.

WHY (plan 2026-09-25 option-stage2-build, Task 1). One OPEN_POSITIONS ruleset serves every
member of a style expert and is evaluated per SYMBOL against the position's entry order, so
per-member exits need to know which member opened the position. ``Transaction.option_strategy``
cannot tell (O_LC, O_LEAPC and O_CONVEXC are all ``long_call``), so the entry action carries an
integer ``entry_tag`` and ``_OptionEntryAction._submit_option_order`` writes it onto the PARENT
order row's ``data`` -- the same seam as ``earnings_event_date`` and ``max_loss_per_contract``,
and the same "read back, never reconstruct" rule (design 2026-08-29 S8.2). ``EntryTagCondition``
reads it back.

ABSENCE IS NEVER A VALUE. Every position not opened by a style-group entry -- every equity
order, every single-structure option job, every live rule written before the tag existed -- has
no key, and ``order_entry_tag`` returns None for it; the condition then never fires. Nothing is
defaulted, so an untagged rule and an untagged order row stay byte-identical to before.

NONSENSE IS NEVER GUESSED AT. The tag decides which member's exits may close a position, so a
value that is not a positive integer is refused by name: at construction (a bad rule) and at
read-back (a corrupted row). ``bool`` is refused explicitly because ``True == 1`` would
otherwise read as member 1.

SHARED by live and backtest: both runtimes build the same ``_OptionEntryAction`` and read the
same order row, so there is no backtest-only branch here.
"""
import math
from typing import Any, Optional

#: The order-row ``data`` key (and the evaluator's action-config key) carrying the tag.
ORDER_ENTRY_TAG_KEY = "entry_tag"

#: The only comparisons a tag supports. It is a member LABEL, not a quantity: ``entry_tag > 3``
#: compares member codes as if they sat on a scale. ``EntryTagCondition`` refuses anything else
#: at construction, and the rule editor offers exactly these (``trigger_catalog``), so the two
#: cannot disagree.
ENTRY_TAG_OPERATORS = ("==", "!=")


def coerce_entry_tag(raw: Any) -> Optional[int]:
    """None stays None; anything else must be a positive integer, returned as ``int``.

    An integral float (``3.0``, which is how a JSON/GA round trip may deliver it) is accepted.
    Refused, by name: ``bool`` (``True == 1``), strings (a stored ``"3"`` is a producer bug, not
    a spelling to guess at), non-integral / non-finite floats, zero, negatives, containers.
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"entry_tag must be a positive integer, got {raw!r} "
                         f"({type(raw).__name__})")
    if isinstance(raw, float) and (not math.isfinite(raw) or not raw.is_integer()):
        raise ValueError(f"entry_tag must be a positive integer, got {raw!r}")
    if raw < 1:
        raise ValueError(f"entry_tag must be a positive integer, got {raw!r}")
    return int(raw)


def order_entry_tag(order: Any) -> Optional[int]:
    """The tag stamped on ``order``'s entry row, or None when it carries none.

    Resolved EXACTLY as ``earnings_stamp.order_event_date`` resolves its stamp: straight off the
    order's ``data`` dict, which is the parent order row the entry facts were written onto (the
    OPEN_POSITIONS evaluator hands the condition that row). No order, no ``data`` dict, or no
    key -> None. A key holding something no entry could have written raises ``ValueError``
    (see ``coerce_entry_tag``) rather than reading as absent.
    """
    data = getattr(order, "data", None)
    if not isinstance(data, dict):
        return None
    raw = data.get(ORDER_ENTRY_TAG_KEY)
    if raw is None:
        return None
    try:
        return coerce_entry_tag(raw)
    except ValueError as e:
        raise ValueError(f"order {getattr(order, 'id', None)}: stored {e}") from e
