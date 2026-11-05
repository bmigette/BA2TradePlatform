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
import numbers
from typing import Any, Optional

#: The order-row ``data`` key (and the evaluator's action-config key) carrying the tag.
ORDER_ENTRY_TAG_KEY = "entry_tag"

#: The only comparisons a tag supports. It is a member LABEL, not a quantity: ``entry_tag > 3``
#: compares member codes as if they sat on a scale. ``EntryTagCondition`` refuses anything else
#: at construction, and the rule editor offers exactly these (``trigger_catalog``), so the two
#: cannot disagree.
ENTRY_TAG_OPERATORS = ("==", "!=")


def coerce_entry_tag(raw: Any) -> Optional[int]:
    """None stays None; anything else must be a positive integer, returned as a plain ``int``.

    Any ``numbers.Integral`` (a numpy int from a GA genome included) and an integral real
    (``3.0``, which is how a JSON/GA round trip may deliver it) are accepted. Refused, by name:
    ``bool`` (``True == 1``; numpy's ``bool_`` is not a number ABC at all), strings (a stored
    ``"3"`` is a producer bug, not a spelling to guess at), non-integral / non-finite reals,
    zero, negatives, containers.
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, numbers.Real):
        raise ValueError(f"entry_tag must be a positive integer, got {raw!r} "
                         f"({type(raw).__name__})")
    if not isinstance(raw, numbers.Integral):
        as_float = float(raw)
        if not math.isfinite(as_float) or not as_float.is_integer():
            raise ValueError(f"entry_tag must be a positive integer, got {raw!r}")
    if raw < 1:
        raise ValueError(f"entry_tag must be a positive integer, got {raw!r}")
    return int(raw)


def validate_entry_tag_comparison(operator: Any, value: Any, where: str) -> int:
    """The ONE check of an ``entry_tag`` comparison, shared by the rule converter (every
    TradeRule -> EventAction path, via ``rule_builders.triggers_from_condition_tree``) and the
    engine (``EntryTagCondition``), so a leaf the engine would refuse can never be converted.

    ``operator`` must already be the engine symbol (``==`` / ``!=``); ``value`` must be a tag.
    Returns the tag. Raises ``ValueError`` naming ``where`` and ``entry_tag`` otherwise -- a
    missing value included: the tag is what scopes a member's exit to that member's positions,
    so a leaf that cannot be honoured is refused, never dropped (a dropped tag leaf is an exit
    for EVERY member's positions on the symbol).
    """
    if operator not in ENTRY_TAG_OPERATORS:
        raise ValueError(
            f"{where}: entry_tag is a member LABEL: only {list(ENTRY_TAG_OPERATORS)} compare "
            f"it, got {operator!r}")
    if value is None:
        raise ValueError(f"{where}: entry_tag comparison has no value (the member tag to "
                         f"match); without it the rule would apply to every member's positions")
    try:
        return coerce_entry_tag(value)
    except ValueError as e:
        raise ValueError(f"{where}: entry_tag comparison value: {e}") from e


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
