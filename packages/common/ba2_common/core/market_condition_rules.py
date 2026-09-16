"""Rule-level validation of market-condition leaves (design 2026-09-15 sections 5 and 6).

Three refusals live here, each closing a failure that is SILENT without it:

* :func:`assert_no_market_conditions` -- a market-condition leaf inside an OPEN-POSITIONS / exit
  ruleset. The design forbids a gate that could delay an exit, a reduction or a protective-order
  adjustment, and live such a leaf would never pass anyway (outside a decision scope the resolver
  has no context, so the gate reads ``no_context`` and the rule simply never fires). A deployed
  sleeve whose stop-loss rule silently cannot fire is the worst outcome in this whole design.
* :func:`assert_market_conditions_resolved` -- an UNRESOLVED mode gene leaving for live. Live
  receives concrete conditions only: ``mode_optimize`` is a TEMPLATE marker (the optimizer picks
  the mode), and a leaf still carrying it -- or a ``mode`` token with no concrete operator/value
  behind it -- describes a search space, not a rule.
* :data:`STRICT_FIELD_NAMES` / :func:`market_condition_fields` -- the names that must never be
  DROPPED by a converter that does not recognise them (see
  ``rule_builders.assert_market_fields_mappable``). Importing a gated ruleset onto a server whose
  event vocabulary predates these fields would otherwise deploy the strategy UNGATED, which is
  not a degraded version of it: it is a different strategy with the same name.

The literal names exist so the refusals survive on a server whose REGISTRY does not know the
field (an older ba2_common, or a future one that retired a profile): a name is permanent once a
payload can carry it, whereas a registry entry is code that travels.
"""
from __future__ import annotations

from typing import Any, Iterator, List, Mapping, Tuple

from ba2_common.core.rule_models import MODE_OFF

#: Every market-condition field name that has ever been deployable, independent of what THIS
#: process's registry happens to hold. ohlcv-v1 (design 3) and the ta-structure-v1 searched
#: subset (design 3.2). A new profile adds its names HERE as well as to the registry.
STRICT_FIELD_NAMES = frozenset({
    "underlying_trend_slope_50_atr14",
    "underlying_adx_14",
    "underlying_realized_vol_ratio_5_20",
    "structure_dist_support_atr",
    "structure_dist_resistance_atr",
    "channel_pos_20",
    "close_vs_prior_high_20_atr",
    "structure_state",
})


def market_condition_fields() -> frozenset:
    """The strict names UNION whatever this process's registry declares."""
    from ba2_common.core.market_conditions import PROFILES

    return STRICT_FIELD_NAMES | {f.name for prof in PROFILES.values() for f in prof.fields}


def iter_market_condition_leaves(node: Any, path: str = "rules") -> Iterator[Tuple[str, Mapping]]:
    """Yield ``(label, leaf)`` for every condition leaf under ``node`` naming a market field.

    ``label`` is the leaf's id when it has one, else its path inside ``node`` -- so a message can
    always point at something the reader can find. Walks dicts and lists; does not decode
    JSON-encoded trees (that is ``seam_wiring.market_condition_leaves_in``'s job, for configs).
    """
    fields = market_condition_fields()

    def walk(n: Any, p: str) -> Iterator[Tuple[str, Mapping]]:
        if isinstance(n, dict):
            field = n.get("field")
            if isinstance(field, str) and field in fields:
                yield (str(n["id"]) if n.get("id") else p), n
            for key, value in n.items():
                yield from walk(value, f"{p}.{key}")
        elif isinstance(n, (list, tuple)):
            for i, value in enumerate(n):
                yield from walk(value, f"{p}[{i}]")

    yield from walk(node, path)


def assert_no_market_conditions(rules: Any, where: str) -> None:
    """Refuse a market-condition leaf anywhere in an exit / open-positions ruleset."""
    hits = [label for label, _ in iter_market_condition_leaves(rules, where)]
    if hits:
        raise ValueError(
            f"{where}: market-condition leaves {hits!r} are not allowed in an open-positions / "
            f"exit ruleset. These gates decide whether to ENTER; on an exit rule the live "
            f"resolver has no decision context, so the condition reads 'no_context', the rule "
            f"never fires, and the position's exit or protective-order adjustment silently stops "
            f"happening. Put the gate on the entry ruleset instead.")


def _unresolved_reason(leaf: Mapping) -> str:
    """Why this leaf is not a concrete live condition ("" when it is one)."""
    if leaf.get("mode_optimize") or leaf.get("modeOptimize"):
        return "it still carries mode_optimize (an optimizer template, not a resolved rule)"
    mode = leaf.get("mode")
    if mode is None:
        return ""
    if mode == MODE_OFF:
        return (f"its mode is {MODE_OFF!r}: an off leaf is REMOVED by the decode, so a payload "
                f"that still contains it was not decoded")
    op = leaf.get("comparison") or leaf.get("op") or leaf.get("operator")
    if not op:
        return f"its mode {mode!r} resolved to no operator"
    if op != "==" and leaf.get("value") is None:
        return f"its mode {mode!r} resolved to operator {op!r} but no threshold value"
    return ""


def assert_market_conditions_resolved(rules: Any, where: str) -> None:
    """Refuse an UNRESOLVED market-condition leaf (design section 5: live gets concrete rules)."""
    bad: List[str] = []
    for label, leaf in iter_market_condition_leaves(rules, where):
        reason = _unresolved_reason(leaf)
        if reason:
            bad.append(f"{label} ({leaf.get('field')}): {reason}")
    if bad:
        raise ValueError(
            f"{where}: unresolved market-condition gene(s) -- " + "; ".join(bad) +
            ". Live deployment receives resolved conditions only: export a DECODED genome (the "
            "optimizer's chosen mode written onto the leaf as an ordinary operator/threshold), "
            "never the search template.")
