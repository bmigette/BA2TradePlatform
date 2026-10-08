"""The reviewed list of symbols excluded from intraday-reading jobs because their intraday price basis is broken.

SAME FILE FORMAT AND SAME RULE as ``ba2_providers/screener/panel_exclusions.json`` (branch
``feat/bt-live-screener-sim``): ``{"entries": [{symbol, reason, added, reviewed_by}]}``; an entry whose
``reviewed_by`` starts with "pending" is NOT in force (the owner has not reviewed it, so the launch still
refuses the symbol). The two lists are kept as TWO FILES because they answer different questions (a symbol whose
screener data is unusable can still be traded on daily bars; a symbol whose 5-minute basis is broken can still be
screened), but the loader contract is identical (``load_exclusions`` / ``in_force`` / ``pending``), so unifying
them at merge is: move this loader next to the screener's, give each file a ``scope`` key, and keep one
``{symbol: entry}`` reader. Nothing else changes.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

EXCLUSIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intraday_basis_exclusions.json")

__all__ = ["EXCLUSIONS_PATH", "ExclusionListError", "load_exclusions", "in_force", "pending"]


class ExclusionListError(ValueError):
    """An exclusion entry lacks symbol, reason, added or reviewed_by: an exclusion needs all four."""


def load_exclusions(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """``{SYMBOL: entry}`` of the whole list (pending entries included; use ``in_force`` / ``pending``)."""
    with open(path or EXCLUSIONS_PATH, encoding="utf-8") as f:
        entries = json.load(f)["entries"]
    out: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        miss = [k for k in ("symbol", "reason", "added", "reviewed_by") if not e.get(k)]
        if miss:
            raise ExclusionListError(f"intraday-basis exclusion entry {e!r} lacks {miss}: an exclusion needs a "
                                     f"symbol, a reason, a date and a reviewer")
        out[str(e["symbol"]).upper()] = e
    return out


def _is_pending(e: Dict[str, Any]) -> bool:
    return str(e.get("reviewed_by", "")).strip().lower().startswith("pending")


def in_force(excl: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The entries somebody has reviewed (``reviewed_by`` does not start with 'pending')."""
    return {s: e for s, e in excl.items() if not _is_pending(e)}


def pending(excl: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The entries nobody has reviewed: listed, but NOT in force."""
    return {s: e for s, e in excl.items() if _is_pending(e)}
