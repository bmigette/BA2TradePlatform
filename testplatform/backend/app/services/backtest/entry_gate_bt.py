"""Backtest-only, read-only counter of WHICH entry gate blocks a recommendation.

``TradeActionEvaluator._evaluate_conditions`` ANDs a rule's triggers in order and stops at the first
False one. This module only READS the evaluator's ``rule_evaluations`` afterwards (never
re-evaluates, never changes ``evaluate_all_conditions``) and counts, per entry rule:

* ``n``   -- times the rule was evaluated (reached),
* ``met`` -- times every condition was true,
* ``b``   -- ``{leaf id: times that leaf was the FIRST false one}``,
* ``order`` -- the leaf ids in evaluation order, so ``reached_k = n - sum(b[j] for j < k)``.

Trigger keys are positional (``cond_{i}``), so the leaf id comes from a registry filled when the
ruleset is seeded (``default_rulesets.seed_ruleset_from_rules``) from the SAME walk that built the
triggers (``rule_builders.trigger_leaf_ids``). A trigger with no registered leaf is counted under
``?<trigger key>`` -- never a crash, and visibly not a real leaf id.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

# event action id -> {"name": EA name, "rule": stable rule key, "leaves": {trigger key: leaf id}}
_REGISTRY: Dict[int, Dict[str, Any]] = {}


def register_entry_rule(ea_id: int, ea_name: str, rule_key: str, leaf_ids: Mapping[str, str]) -> None:
    """Remember the leaf ids of the triggers seeded into event action ``ea_id``."""
    _REGISTRY[int(ea_id)] = {"name": ea_name, "rule": str(rule_key), "leaves": dict(leaf_ids)}


class EntryGateRecord:
    """Per-run counter fed with ``evaluator.rule_evaluations`` after each ENTRY evaluation."""

    def __init__(self) -> None:
        self._rules: Dict[str, Dict[str, Any]] = {}

    def note_rules(self, rule_evaluations: Iterable[Mapping[str, Any]]) -> None:
        for ev in rule_evaluations or ():
            reg = _REGISTRY.get(ev.get("rule_id"))
            if reg is not None and reg["name"] != ev.get("rule_name"):
                reg = None  # stale id from an earlier seeding: do not mislabel
            key = reg["rule"] if reg else f"ea:{ev.get('rule_name')}"
            leaves = reg["leaves"] if reg else {}
            rec = self._rules.setdefault(key, {"n": 0, "met": 0, "b": {}, "order": []})
            rec["n"] += 1
            conds = ev.get("conditions") or []
            for c in conds:
                label = leaves.get(c.get("trigger_key")) or f"?{c.get('trigger_key')}"
                if label not in rec["order"]:
                    rec["order"].append(label)
            if ev.get("all_conditions_met") and not ev.get("error"):
                rec["met"] += 1
                continue
            first: Optional[str] = None
            for c in conds:
                if not c.get("condition_result"):
                    first = leaves.get(c.get("trigger_key")) or f"?{c.get('trigger_key')}"
                    break
            if first is None:
                first = "?error" if ev.get("error") else "?unknown"
            rec["b"][first] = rec["b"].get(first, 0) + 1

    def record(self) -> Optional[Dict[str, Any]]:
        """JSON-able ``{rule key: {"n", "met", "b", "order"}}``, or None when nothing was evaluated."""
        if not self._rules:
            return None
        return {k: {"n": v["n"], "met": v["met"], "b": dict(v["b"]), "order": list(v["order"])}
                for k, v in self._rules.items()}
