"""OPT-B1 — the O_CC / O_PP option overlay must be REACHABLE under first-match evaluation.

The engine evaluates an OPEN_POSITIONS ruleset with FIRST-MATCH semantics:
``TradeActionEvaluator.evaluate`` (``packages/common/ba2_common/core/TradeActionEvaluator.py``)
walks the EventActions in order and ``break``s out of the loop as soon as one rule's
conditions are met, unless that rule sets ``continue_processing`` (default False —
``rule_models.ExitRule``). A ``stop_processing`` action breaks unconditionally.

The launcher used to APPEND ``cc_guard``/``cc_sell`` (resp. ``pp_guard``/``pp_buy``) after
S2's exit list, whose last rule ``exit_stoploss`` is conditioned only on ``has_position``.
``has_position`` is true for every position the manage pass is invoked for, so that rule
ALWAYS matches, and with ``continue_processing`` unset the evaluator broke there — the
overlay could never run. The GA could not route around it either: ``exit_stoploss``
declares no ``toggle_optimize``, so ``collect_param_space`` emits no
``exit:exit_stoploss:enabled`` gene and the shadow is unconditional in EVERY genome.

Consequence: every O_CC and O_PP result ever produced was a mislabelled plain-equity run,
and the two jobs were byte-identical to each other.

These tests model the evaluator's loop over the launcher's exit-rule list and assert the
overlay is reached. They deliberately do NOT assert a specific index or rule id — a fix
that merely renames or reorders without restoring reachability must still fail.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

# Load the launcher module by path (it lives at testplatform/ba2test_launcher.py).
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


_OVERLAY_ACTIONS = ("sell_covered_call", "buy_protective_put")

# The flag leaves that are true for EVERY position the OPEN_POSITIONS pass is invoked
# for. A rule whose whole condition tree reduces to these always matches.
_ALWAYS_TRUE_FLAGS = frozenset({"has_position"})


def _leaves(node):
    """Flatten a condition tree to its leaf dicts."""
    if not isinstance(node, dict):
        return []
    kids = node.get("conditions")
    if isinstance(kids, list):
        out = []
        for k in kids:
            out.extend(_leaves(k))
        return out
    return [node]


def _always_matches(rule) -> bool:
    """True when the rule's conditions are satisfied on every managed bar.

    That is: a non-empty AND tree whose every leaf is an unconditional flag from
    ``_ALWAYS_TRUE_FLAGS`` (no numeric comparison, no market-dependent flag).
    """
    tree = rule.get("conditions") or {}
    if str(tree.get("type") or tree.get("operator") or "AND").upper() != "AND":
        return False
    leaves = _leaves(tree)
    if not leaves:
        return False
    return all(
        leaf.get("field") in _ALWAYS_TRUE_FLAGS and leaf.get("value") is None
        for leaf in leaves
    )


def _action_types(rule):
    return [str(a.get("action_type") or a.get("action") or "")
            for a in (rule.get("actions") or [])]


def _walk_until_break(exit_rules, *, matched):
    """Replay the evaluator's first-match walk; return the ids of the rules it REACHES.

    ``matched(rule) -> bool`` decides whether a rule's conditions are met on this bar.
    A matched rule ends the walk unless it sets ``continue_processing``; a matched
    ``stop_processing`` rule always ends it.
    """
    reached = []
    for rule in exit_rules or []:
        reached.append(rule.get("id"))
        if not matched(rule):
            continue
        if "stop_processing" in _action_types(rule):
            break
        if not rule.get("continue_processing"):
            break
    return reached


def _overlay_rule(exit_rules):
    hits = [r for r in exit_rules or []
            if any(a in _OVERLAY_ACTIONS for a in _action_types(r))]
    assert len(hits) == 1, f"expected exactly one overlay rule, got {[h.get('id') for h in hits]}"
    return hits[0]


@pytest.mark.parametrize("kind", ["O_CC", "O_PP"])
def test_the_overlay_is_reached_when_only_the_unconditional_rules_match(kind):
    """The bar every overlay run needs: a held position, no signal/profit exit triggered.

    Only the always-matching rules fire. The overlay must still be reached — before the
    fix, S2's ``exit_stoploss`` matched first, broke the loop, and the overlay was dead in
    every genome.
    """
    strat = mod._build_strategy(kind, kind, "FMPRating")
    rules = list(strat.exit_rules or [])
    overlay = _overlay_rule(rules)

    reached = _walk_until_break(rules, matched=_always_matches)

    assert overlay.get("id") in reached, (
        f"{kind}: the overlay rule {overlay.get('id')!r} is never reached. The walk stopped "
        f"after {reached!r} — an earlier always-matching rule with continue_processing unset "
        f"shadows it, so the overlay can never fire (OPT-B1)."
    )


@pytest.mark.parametrize("kind", ["O_CC", "O_PP"])
def test_writing_the_overlay_does_not_disarm_the_floor_stop(kind):
    """The overlay is an OPEN, not an exit: it must fall through to the exit rules.

    S2's floor stop (``exit_stoploss``, the always-matching last rule) is the position's
    only condition-driven downside protection and carries no on/off gene precisely so the
    GA cannot disable it. An overlay placed ahead of it with ``continue_processing`` unset
    would shadow it exactly the way it was itself shadowed — trading one silent breakage
    for another.
    """
    strat = mod._build_strategy(kind, kind, "FMPRating")
    rules = list(strat.exit_rules or [])
    overlay = _overlay_rule(rules)

    floor_stops = [r for r in rules
                   if _always_matches(r)
                   and "adjust_stop_loss" in _action_types(r)]
    assert floor_stops, f"{kind}: S2's always-on floor stop disappeared from the exit list"

    reached = _walk_until_break(rules, matched=_always_matches)
    for stop in floor_stops:
        assert stop.get("id") in reached, (
            f"{kind}: the floor stop {stop.get('id')!r} is no longer reached (walk stopped "
            f"after {reached!r}) — the overlay {overlay.get('id')!r} must set "
            f"continue_processing so the exit chain still runs."
        )


@pytest.mark.parametrize("kind", ["O_CC", "O_PP"])
def test_a_closing_exit_still_wins_over_the_overlay(kind):
    """No new short call on a bar the equity is being sold.

    Writing a covered call on the same bar a close fires would leave a NAKED short call
    once the equity sells. The overlay must therefore sit AFTER the closing rules, so a
    matched close breaks the walk before the overlay is reached.
    """
    strat = mod._build_strategy(kind, kind, "FMPRating")
    rules = list(strat.exit_rules or [])
    overlay = _overlay_rule(rules)

    closers = [r for r in rules if _action_types(r) == ["close"]]
    assert closers, f"{kind}: S2's close rules disappeared from the exit list"

    for closer in closers:
        # This bar: the closer's trigger fires, plus everything unconditional.
        reached = _walk_until_break(
            rules, matched=lambda r, c=closer: r is c or _always_matches(r))
        assert overlay.get("id") not in reached, (
            f"{kind}: the overlay {overlay.get('id')!r} is still reached on a bar where "
            f"{closer.get('id')!r} closes the equity — that writes an option against "
            f"shares that are being sold."
        )


@pytest.mark.parametrize("kind", ["O_CC", "O_PP"])
def test_the_anti_stacking_guard_still_precedes_the_overlay(kind):
    """The guard is the codebase's NOT idiom; it only works if it evaluates first."""
    strat = mod._build_strategy(kind, kind, "FMPRating")
    rules = list(strat.exit_rules or [])
    ids = [r.get("id") for r in rules]
    overlay = _overlay_rule(rules)

    guards = [r for r in rules if "stop_processing" in _action_types(r)]
    assert len(guards) == 1, f"{kind}: expected exactly one guard rule, got {ids}"
    guard = guards[0]
    guard_fields = {leaf.get("field") for leaf in _leaves(guard.get("conditions") or {})}
    assert guard_fields == {"has_covered_call" if kind == "O_CC" else "has_protective_put"}, (
        f"{kind}: guard gates on {guard_fields}, not the existing-overlay flag"
    )
    assert ids.index(guard.get("id")) < ids.index(overlay.get("id")), (
        f"{kind}: the guard {guard.get('id')!r} must precede the overlay "
        f"{overlay.get('id')!r}; got order {ids}"
    )
