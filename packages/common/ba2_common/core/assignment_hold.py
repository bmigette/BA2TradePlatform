"""Who keeps the stock a short-option ASSIGNMENT delivers: the member-scoped hold (stage-2 E2).

WHY. A physically assigned short put (or naked short call) leaves STOCK behind. Only a strategy
whose own rules manage that stock may keep it -- the wheel, whose covered-call overlay writes
calls against it. Every other option strategy's exits are all ``close_option``, so the backtest
liquidates the delivered stock at the next bar's open (the "no orphaned stock" policy; the OS1
blow-up was assigned stock riding unmanaged). Until stage 2 that was ONE run-level switch,
``hold_assigned_stock``. A style expert composes several members in one run (plan 2026-09-25
option-stage2-build, revision Task E2): the wheel member must hold, while a CSP / jade-lizard /
ratio-spread member in the same run must still liquidate, or each of them silently becomes a
second wheel.

THE DECISION (``holds_assigned_stock``), per assignment, from the ENTRY TAG of the option
position that was assigned (``ba2_common.core.entry_tag``):

* the run names no member tag set -> the run-level flag decides, EXACTLY as before (every run
  on record: byte-identical);
* the run names ``hold_assigned_stock_tags`` -> hold iff the position's tag is in the set;
* a tag set AND an untagged position -> REFUSED (``UntaggedAssignmentRefused``). A style run's
  positions are all opened by tagged member entries, so an untagged one is a wiring defect, and
  guessing either way picks a strategy (a wheel vs a naked-call simulation) for it.

A run may not name BOTH the run-level flag and a tag set: the two answer the same question, and
a run that states two answers is refused at configuration time, never resolved by precedence.

THE DELIVERED STOCK INHERITS THE TAG (``assigned_stock_entry_data``). The assignment's stock
entry order carries the option position's tag, so a tag-scoped rule keeps seeing the member
that owns the shares: the wheel's covered-call overlay (``entry_tag == <wheel>``) writes calls
only on stock the WHEEL member was put, never on a CSP member's shares waiting for their
next-bar liquidation. An untagged position delivers untagged stock (``None``: no ``data`` is
written), so every existing run and live row stays byte-identical.

LIVE. The broker reconciler (``AlpacaAccount._apply_option_activity``) has NO hold/liquidate
decision: live never sells assigned stock (``deploy_parity`` ``hold_assigned_stock``:
``why_no_live_analogue``), so ``holds_assigned_stock`` has one caller, the backtest account.
The tag carry IS shared: both runtimes stamp the assignment's stock entry order through
``assigned_stock_entry_data``.
"""
from dataclasses import dataclass
from typing import Any, FrozenSet, Mapping, Optional

from ba2_common.core.entry_tag import ORDER_ENTRY_TAG_KEY, coerce_entry_tag

#: The run-level switch (account setting). Truthy -> every assignment's stock is held.
HOLD_ASSIGNED_STOCK_KEY = "hold_assigned_stock"
#: The member-scoped form (account setting): the entry tags of the members that HOLD.
HOLD_ASSIGNED_STOCK_TAGS_KEY = "hold_assigned_stock_tags"


class UntaggedAssignmentRefused(ValueError):
    """A member-scoped run assigned a position that carries no entry tag: refused, not guessed."""


@dataclass(frozen=True)
class AssignedStockHoldPolicy:
    """The run's answer to "who keeps assigned stock", as configured.

    ``member_tags`` None: the run-level ``run_level`` flag decides for every assignment (the
    pre-stage-2 behaviour). A frozenset: only those members' assignments are held.
    """
    run_level: bool
    member_tags: Optional[FrozenSet[int]] = None


def resolve_assigned_stock_hold_policy(cfg: Mapping[str, Any]) -> AssignedStockHoldPolicy:
    """Read the policy off an account config, refusing a malformed or ambiguous one by name.

    ``hold_assigned_stock`` keeps its historical read -- optional, truthiness, default off -- so
    every existing config resolves exactly as ``self._cfg.get("hold_assigned_stock", False)``
    did. ``hold_assigned_stock_tags``, when present and not None, must be a non-empty list /
    tuple / set of positive integer tags with no duplicates (a duplicate is a producer bug, not
    a spelling).
    """
    run_level = bool(cfg.get(HOLD_ASSIGNED_STOCK_KEY, False))
    raw = cfg.get(HOLD_ASSIGNED_STOCK_TAGS_KEY)
    if raw is None:
        return AssignedStockHoldPolicy(run_level=run_level)
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, (list, tuple, set,
                                                                      frozenset)):
        raise ValueError(
            f"{HOLD_ASSIGNED_STOCK_TAGS_KEY} must be a list of member entry tags, got "
            f"{raw!r} ({type(raw).__name__})")
    items = list(raw)
    if not items:
        raise ValueError(
            f"{HOLD_ASSIGNED_STOCK_TAGS_KEY} is empty: omit the key for a run where no member "
            f"holds assigned stock (an empty set would still force every assignment to carry "
            f"a tag, for no member)")
    tags = []
    for item in items:
        tag = coerce_entry_tag(item)
        if tag is None:
            raise ValueError(f"{HOLD_ASSIGNED_STOCK_TAGS_KEY} holds None: {raw!r}")
        tags.append(tag)
    if len(set(tags)) != len(tags):
        raise ValueError(f"{HOLD_ASSIGNED_STOCK_TAGS_KEY} repeats a tag: {raw!r}")
    if run_level:
        raise ValueError(
            f"the run sets both {HOLD_ASSIGNED_STOCK_KEY}=True (hold EVERY assignment) and "
            f"{HOLD_ASSIGNED_STOCK_TAGS_KEY}={sorted(tags)} (hold only those members'): they "
            f"answer the same question, so state one")
    return AssignedStockHoldPolicy(run_level=False, member_tags=frozenset(tags))


def holds_assigned_stock(policy: AssignedStockHoldPolicy, entry_tag: Optional[int], *,
                         where: str) -> bool:
    """True when the stock this assignment delivers is KEPT; False -> liquidate it.

    ``entry_tag``: the tag of the ASSIGNED option position (``order_entry_tag`` of its entry
    order), or None when it carries none. ``where`` names the position in a refusal.
    """
    if policy.member_tags is None:
        return policy.run_level
    if entry_tag is None:
        raise UntaggedAssignmentRefused(
            f"{where}: the run holds assigned stock per member "
            f"({HOLD_ASSIGNED_STOCK_TAGS_KEY}={sorted(policy.member_tags)}) but the assigned "
            f"position carries no entry tag -- which member's policy applies cannot be known, "
            f"and guessing decides whether it is a wheel or a liquidated put")
    return entry_tag in policy.member_tags


def assigned_stock_entry_data(entry_tag: Optional[int]) -> Optional[dict]:
    """The ``data`` for the stock entry order an assignment delivers: the assigned position's
    member tag, or None (write no ``data`` at all) for an untagged position."""
    tag = coerce_entry_tag(entry_tag)
    if tag is None:
        return None
    return {ORDER_ENTRY_TAG_KEY: tag}
