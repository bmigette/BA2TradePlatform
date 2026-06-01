"""
Identity-based take-profit tier tracking for PennyMomentumTrader.

A take-profit tier must fire exactly once, even though the exit-condition LLM
rewrites the tier list on every refresh. Tracking fired tiers by list index is
fragile: re-applying the list re-arms tiers that already sold. Instead each tier
carries a stable ``id`` and the fired set is stored as ``triggered_tp_tier_ids``.

These are pure helpers with no platform dependencies so they can be unit-tested
in isolation.
"""
from typing import Any, Dict, List, Tuple


def ensure_tier_ids(tiers: List[Any], next_id: int) -> Tuple[List[Any], int]:
    """Assign a stable string id to every tier dict that lacks one.

    Ids are minted as ``"t{n}"`` from the monotonic ``next_id`` counter.
    Non-dict entries and tiers that already have an id are left untouched.

    Returns the (same) list and the advanced counter.
    """
    for tier in tiers:
        if isinstance(tier, dict) and not tier.get("id"):
            tier["id"] = f"t{next_id}"
            next_id += 1
    return tiers, next_id


def merge_tier_update(
    old_tiers: List[Any], new_tiers: List[Any], next_id: int
) -> Tuple[List[Any], int]:
    """Merge an LLM-provided tier list with the existing one, preserving ids by position.

    Position ``i`` in ``new_tiers`` inherits ``old_tiers[i]``'s id when present, so a
    tier that already fired keeps its id (and therefore its fired status) even if its
    condition was rewritten (e.g. trailing). Positions beyond the old length get fresh
    ids; positions removed from the new list simply drop out. New tier *content* (the
    condition / exit_pct from ``new_tiers``) is always applied.

    ``old_tiers`` is not mutated. Returns (merged_tiers, advanced_counter).
    """
    merged: List[Any] = []
    for i, tier in enumerate(new_tiers):
        if not isinstance(tier, dict):
            merged.append(tier)
            continue
        tier = dict(tier)  # copy so callers' new list / old list stay intact
        old = old_tiers[i] if i < len(old_tiers) else None
        if isinstance(old, dict) and old.get("id"):
            tier["id"] = old["id"]
        elif not tier.get("id"):
            tier["id"] = f"t{next_id}"
            next_id += 1
        merged.append(tier)
    return merged, next_id


def migrate_triggered_state(info: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade a monitored-symbol ``info`` dict to id-based tier tracking, in place.

    - Ensures every take-profit tier has an ``id`` (advancing ``info["_next_tier_id"]``).
    - If ``triggered_tp_tier_ids`` is absent, derives it from the legacy index-based
      ``triggered_tp_tiers`` by mapping each index to the tier id at that position.

    Idempotent and safe to call on a fresh info dict with no exit conditions.
    """
    tiers = (info.get("exit_conditions") or {}).get("take_profit") or []
    next_id = info.get("_next_tier_id", 0)
    tiers, next_id = ensure_tier_ids(tiers, next_id)
    info["_next_tier_id"] = next_id

    if "triggered_tp_tier_ids" not in info:
        legacy = info.get("triggered_tp_tiers") or []
        ids: List[str] = []
        for idx in legacy:
            if isinstance(idx, int) and 0 <= idx < len(tiers) and isinstance(tiers[idx], dict):
                tid = tiers[idx].get("id")
                if tid:
                    ids.append(tid)
        info["triggered_tp_tier_ids"] = ids
    return info
