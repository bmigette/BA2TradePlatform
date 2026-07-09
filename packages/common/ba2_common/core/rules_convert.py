"""Shared LIVE-ruleset <-> backtester condition-tree conversion (the REVERSE direction).

SINGLE source of truth for turning a live ``EventAction`` (triggers/actions) — or a whole
live ruleset EXPORT FILE (``export_type`` rulesets/ruleset/rule) — into the backtester's
strategy shapes (condition-tree entry trees + ExitCondition-shaped exit rules), and back.

Companion to ``rule_builders.py`` (the FORWARD direction: condition-tree -> triggers/actions).
The canonical field/flag/action maps live in ``rule_builders``; this module derives its
vocabulary from them so there is exactly ONE place defining what a flag / numeric / action is.

Leaf ``field`` is ALWAYS the raw ``ExpertEventType`` value (e.g. ``"confidence"``,
``"long_term"``, ``"percent_to_new_target"``) — the same vocabulary the ``/api/ruleset/
vocabulary`` endpoint exposes and the frontend ConditionBuilder keys on. This keeps entry and
exit conversion identical and never drops a numeric event that happens to be absent from
``FIELD_EVENT`` (the forward map only needs the tunable subset).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from ba2_common.core.rule_builders import (
    FIELD_EVENT,
    FLAG_FIELD_EVENT,
    action_from_rule,
    triggers_from_condition_tree,
)
from ba2_common.core.types import (
    AnalysisUseCase,
    ExpertActionType,
    ExpertEventRuleType,
    ExpertEventType,
    ReferenceValue,
)

# Vocabulary derived ONCE from the enums / canonical maps (no second copy of the lists).
NUMERIC_EVENT_VALUES = {m.value for m in ExpertEventType if m.name.startswith("N_")}
FLAG_EVENT_VALUES = {m.value for m in ExpertEventType if m.name.startswith("F_")}
ACTION_VALUES = {m.value for m in ExpertActionType}
ADJUST_ACTIONS = {
    ExpertActionType.ADJUST_TAKE_PROFIT.value,
    ExpertActionType.ADJUST_STOP_LOSS.value,
}
BUY_ACTION = ExpertActionType.BUY.value
SELL_ACTION = ExpertActionType.SELL.value

# Suppress unused-import warnings — these are re-exported as part of the public surface.
__all__ = [
    "NUMERIC_EVENT_VALUES",
    "FLAG_EVENT_VALUES",
    "ACTION_VALUES",
    "ADJUST_ACTIONS",
    "BUY_ACTION",
    "SELL_ACTION",
    "opt_range",
    "trigger_to_leaf",
    "eventaction_to_exit_rule",
    "eventaction_to_entry_group",
    "entry_action_side",
    "groups_to_tree",
    "live_export_to_strategy",
    "strategy_to_live_export",
    "live_export_to_trade_rules",
    "trade_rules_to_live_export",
    "live_actions_from_trade_rule",
]


def opt_range(value: Any) -> Tuple[float, float, float]:
    """Sensible default optimize range for a numeric leaf/action value: ±50%, step ≈ |v|/5.

    Returns ``(min, max, step)``. A zero/None value yields a small symmetric default so the
    optimizer still has a range to explore. Negative values (e.g. a -3% SL offset) keep the
    correct ordering (min <= max).
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    if v == 0.0:
        return (-1.0, 1.0, 0.2)
    lo, hi = sorted((v * 0.5, v * 1.5))
    step = abs(v) / 5.0
    return (lo, hi, step)


def _operator_of(trig: dict) -> str:
    """Operator from a trigger, tolerant of v1.0 ``operator`` and v1.1 ``op``."""
    return trig.get("operator") or trig.get("op") or ">"


def trigger_to_leaf(idx: int, trig: dict) -> Optional[dict]:
    """Convert ONE live trigger to a backtester ConditionBase-shaped leaf dict, or None.

    Flag triggers (event_type in FLAG_EVENT_VALUES) become value-less ``{id, field, field_type:
    "flag"}`` leaves; numeric triggers (in NUMERIC_EVENT_VALUES) carry field/comparison/value and
    are marked optimizable with a default ±50% range. Unknown event types are skipped (return
    None) so a partial live rule never breaks the import. ``field`` is the RAW event_type value.

    This is the SINGLE leaf converter used by BOTH exit and entry conversion (resolving the old
    entry/exit inconsistency where entry used a lossy inverse map). Reads both v1.0 ``operator``
    and v1.1 ``op``.
    """
    et = trig.get("event_type")
    leaf_id = f"c{idx}"
    if et in FLAG_EVENT_VALUES:
        return {"id": leaf_id, "field": et, "field_type": "flag"}
    if et in NUMERIC_EVENT_VALUES:
        value = trig.get("value")
        vmin, vmax, vstep = opt_range(value)
        return {
            "id": leaf_id,
            "field": et,
            "field_type": "numeric",
            "comparison": _operator_of(trig),
            "value": value,
            "optimize": True,
            "optimize_enabled": True,
            "value_min": vmin,
            "value_max": vmax,
            "value_step": vstep,
        }
    return None


def _leaves_from_triggers(triggers: dict) -> List[dict]:
    """All recognizable leaves from a triggers dict (skipping unknown event types)."""
    leaves: List[dict] = []
    for i, trig in enumerate((triggers or {}).values()):
        if not isinstance(trig, dict):
            continue
        leaf = trigger_to_leaf(i, trig)
        if leaf is not None:
            leaves.append(leaf)
    return leaves


def eventaction_to_exit_rule(
    ea_id: Any, name: str, triggers: dict, actions: dict
) -> Optional[dict]:
    """Convert one live ``EventAction`` (open_positions) into an ExitCondition-shaped dict.

    Returns None when the action_type is unknown/unsupported (skip the rule rather than emit
    something that fails ExitCondition validation). Numeric conditions and adjust action_values
    are marked optimizable with sensible default ranges; the whole rule carries
    ``toggle_optimize`` so the optimizer can drop it.
    """
    # The live ``actions`` JSON is ``{"<key>": {"action_type": ..., reference_value?, value?}}``.
    # An open_positions rule has exactly one action; take the first usable one.
    action_cfg = None
    for cfg in (actions or {}).values():
        if isinstance(cfg, dict) and cfg.get("action_type") in ACTION_VALUES:
            action_cfg = cfg
            break
    if action_cfg is None:
        return None
    action = action_cfg["action_type"]

    leaves = _leaves_from_triggers(triggers)

    rule: dict = {
        "id": f"live-{ea_id}",
        "name": name,
        "conditions": {"id": f"grp-{ea_id}", "operator": "AND", "conditions": leaves},
        "action": action,
        "toggle_optimize": True,
    }

    if action in ADJUST_ACTIONS:
        rule["reference_value"] = (
            action_cfg.get("reference_value") or ReferenceValue.ORDER_OPEN_PRICE.value
        )
        av = action_cfg.get("value")
        rule["action_value"] = av
        amin, amax, astep = opt_range(av)
        rule["action_value_optimize"] = True
        rule["action_value_min"] = amin
        rule["action_value_max"] = amax
        rule["action_value_step"] = astep

    return rule


def eventaction_to_entry_group(ea_id: Any, triggers: dict) -> Optional[Tuple[dict, List[dict]]]:
    """Convert one live enter_market EventAction's triggers into an AND-group of tree leaves.

    Returns ``(group, leaves)`` where ``group`` is ``{id, operator:"AND", conditions:[leaves]}``,
    or None if the EventAction yields no recognizable leaves (so an all-unknown rule is dropped
    rather than emitting an empty group). Uses the shared ``trigger_to_leaf`` (raw enum value
    field) so numeric enums missing from ``FIELD_EVENT`` (e.g. ``percent_to_new_target``) are
    PRESERVED.
    """
    leaves = _leaves_from_triggers(triggers)
    if not leaves:
        return None
    group = {"id": f"grp-{ea_id}", "operator": "AND", "conditions": leaves}
    return group, leaves


def entry_action_side(actions: dict) -> Optional[str]:
    """Return "buy"/"sell" for an enter_market EventAction's open action, or None if neither."""
    for cfg in (actions or {}).values():
        if not isinstance(cfg, dict):
            continue
        at = cfg.get("action_type")
        if at == BUY_ACTION:
            return "buy"
        if at == SELL_ACTION:
            return "sell"
    return None


def groups_to_tree(groups: List[dict]) -> Optional[dict]:
    """Combine AND-groups into one entry condition tree: single group as-is, multiple OR-ed."""
    if not groups:
        return None
    if len(groups) == 1:
        return groups[0]
    return {"id": "grp-or", "operator": "OR", "conditions": groups}


def _iter_export_rulesets(payload: dict) -> Iterable[dict]:
    """Yield ruleset dicts from a live export payload (rulesets/ruleset/rule flavors + bare)."""
    if not isinstance(payload, dict):
        return
    if isinstance(payload.get("rulesets"), list):
        for rs in payload["rulesets"]:
            if isinstance(rs, dict):
                yield rs
        return
    if isinstance(payload.get("ruleset"), dict):
        yield payload["ruleset"]
        return
    if isinstance(payload.get("rule"), dict):
        rule = payload["rule"]
        yield {"subtype": rule.get("subtype"), "rules": [rule]}
        return
    # Bare fallback: a ruleset-shaped object with a top-level ``rules`` list.
    if isinstance(payload.get("rules"), list):
        yield payload


def live_export_to_strategy(payload: dict) -> dict:
    """Convert a live ruleset EXPORT FILE into backtester strategy shapes.

    Accepts ``export_type`` ``rulesets`` (``payload['rulesets']``: list), ``ruleset``
    (``payload['ruleset']``: obj), or ``rule`` (``payload['rule']``: obj, wrapped as a 1-rule
    ruleset whose subtype comes from the rule). Routes each rule by its EFFECTIVE subtype
    (``rule.subtype`` or the ruleset's ``subtype``; falls back to the action when absent):

      * ``enter_market`` -> entry trees. ``entry_action_side`` decides buy/sell; the triggers
        become an AND-group via ``eventaction_to_entry_group``; multiple buy/sell rules are
        OR-ed. Rules whose action side is neither buy nor sell (e.g. ``stop_processing``, or a
        rule that only carries ``adjust_*`` brackets) are SKIPPED for the tree. Extra
        ``adjust_take_profit``/``adjust_stop_loss`` actions on a buy/sell rule (the position's
        initial TP/SL) are IGNORED for the tree (mirrors the live-DB enter import) but COUNTED.
      * ``open_positions`` -> exit rules via ``eventaction_to_exit_rule`` (None -> skipped).

    Returns ``{"buy_entry_conditions": tree|None, "sell_entry_conditions": tree|None,
    "exit_conditions": [rule, ...], "summary": {...}}``. Pure/total: never raises on an unknown
    event_type/action (it is skipped) so a partial/edited file still imports.
    """
    buy_groups: List[dict] = []
    sell_groups: List[dict] = []
    exit_rules: List[dict] = []
    n_rulesets = n_rules = buy_n = sell_n = skipped = ignored_brackets = 0

    for rs in _iter_export_rulesets(payload or {}):
        n_rulesets += 1
        rs_sub = rs.get("subtype")
        for ridx, rule in enumerate(rs.get("rules") or []):
            if not isinstance(rule, dict):
                continue
            n_rules += 1
            subtype = rule.get("subtype") or rs_sub
            triggers = rule.get("triggers") or {}
            actions = rule.get("actions") or {}
            name = rule.get("name") or f"rule-{ridx}"
            ea_id = rule.get("order_index", ridx)

            if subtype:
                is_exit = subtype == AnalysisUseCase.OPEN_POSITIONS.value
            else:
                # No subtype: infer — a buy/sell open action => entry, otherwise exit.
                is_exit = entry_action_side(actions) is None

            if is_exit:
                er = eventaction_to_exit_rule(ea_id, name, triggers, actions)
                if er is None:
                    skipped += 1
                else:
                    exit_rules.append(er)
                continue

            side = entry_action_side(actions)
            if side is None:
                skipped += 1  # e.g. stop_processing, or only adjust_* brackets
                continue
            ignored_brackets += sum(
                1
                for cfg in actions.values()
                if isinstance(cfg, dict) and cfg.get("action_type") in ADJUST_ACTIONS
            )
            converted = eventaction_to_entry_group(f"{side}-{ea_id}", triggers)
            if converted is None:
                skipped += 1
                continue
            group, _leaves = converted
            if side == "buy":
                buy_groups.append(group)
                buy_n += 1
            else:
                sell_groups.append(group)
                sell_n += 1

    return {
        "buy_entry_conditions": groups_to_tree(buy_groups),
        "sell_entry_conditions": groups_to_tree(sell_groups),
        "exit_conditions": exit_rules,
        "summary": {
            "rulesets": n_rulesets,
            "rules": n_rules,
            "buy_rules": buy_n,
            "sell_rules": sell_n,
            "exit_rules": len(exit_rules),
            "skipped_rules": skipped,
            "ignored_initial_brackets": ignored_brackets,
        },
    }


# ---------------------------------------------------------------------------
# Unified TradeRule converters (EventAction-shaped, LOSSLESS in both directions).
# Unlike live_export_to_strategy/strategy_to_live_export (the legacy split-shape pair kept
# for not-yet-migrated callers), these preserve EVERY action on a rule, the rule ORDER, and
# ``continue_processing`` — a live ruleset round-trips byte-equivalently.
# ---------------------------------------------------------------------------
def _live_action_to_action_cfg(cfg: dict) -> Optional[dict]:
    """One live EventAction actions-cfg entry -> canonical ActionCfg dict, or None if the
    action_type is unknown (skipped, so a partial/edited file still converts)."""
    at = cfg.get("action_type") or cfg.get("type")
    if at not in ACTION_VALUES:
        return None
    out: dict = {"action_type": at}
    # Preserve everything else the live cfg carries (option selection params, etc.).
    for k, v in cfg.items():
        if k in ("action_type", "type", "value", "reference_value"):
            continue
        out[k] = v
    if cfg.get("reference_value") is not None:
        out["reference_value"] = cfg["reference_value"]
    if cfg.get("value") is not None:
        out["action_value"] = cfg["value"]
    if at in ADJUST_ACTIONS:
        av = cfg.get("value")
        amin, amax, astep = opt_range(av)
        out.setdefault("action_value_optimize", True)
        out.setdefault("action_value_min", amin)
        out.setdefault("action_value_max", amax)
        out.setdefault("action_value_step", astep)
    return out


def live_export_to_trade_rules(payload: dict) -> dict:
    """Convert a live ruleset EXPORT FILE into unified TradeRule lists — LOSSLESS.

    Returns ``{"entry_rules": [...], "exit_rules": [...], "summary": {...}}`` where each rule
    is the canonical TradeRule shape (conditions tree + ORDERED actions list +
    continue_processing). Every action on a rule survives (open + adjust_* brackets,
    stop_processing guards, multi-action exits), rule order follows ``order_index`` when
    present, and numeric leaves / adjust values get default optimize ranges. Routing by
    effective subtype matches ``live_export_to_strategy``.
    """
    from ba2_common.core.rule_models import normalize_trade_rules

    entry_rules: List[dict] = []
    exit_rules: List[dict] = []
    n_rulesets = n_rules = skipped = 0

    for rs in _iter_export_rulesets(payload or {}):
        n_rulesets += 1
        rs_sub = rs.get("subtype")
        rules = [r for r in (rs.get("rules") or []) if isinstance(r, dict)]
        rules.sort(key=lambda r: (r.get("order_index") is None, r.get("order_index", 0)))
        for ridx, rule in enumerate(rules):
            n_rules += 1
            subtype = rule.get("subtype") or rs_sub
            triggers = rule.get("triggers") or {}
            actions = rule.get("actions") or {}
            name = rule.get("name") or f"rule-{ridx}"
            ea_id = rule.get("order_index", ridx)

            action_list = []
            for cfg in actions.values():
                if isinstance(cfg, dict):
                    converted = _live_action_to_action_cfg(cfg)
                    if converted is not None:
                        action_list.append(converted)
            if not action_list:
                skipped += 1
                continue

            leaves = _leaves_from_triggers(triggers)
            conditions = (
                {"id": f"grp-{ea_id}", "operator": "AND", "conditions": leaves}
                if leaves else None
            )
            tr = {
                "id": f"live-{ea_id}",
                "name": name,
                "conditions": conditions,
                "actions": action_list,
                "continue_processing": bool(rule.get("continue_processing", False)),
                "toggle_optimize": True,
            }
            if subtype == AnalysisUseCase.OPEN_POSITIONS.value or (
                subtype is None and entry_action_side(actions) is None
            ):
                exit_rules.append(tr)
            else:
                entry_rules.append(tr)

    return {
        "entry_rules": normalize_trade_rules(entry_rules),
        "exit_rules": normalize_trade_rules(exit_rules),
        "summary": {
            "rulesets": n_rulesets,
            "rules": n_rules,
            "entry_rules": len(entry_rules),
            "exit_rules": len(exit_rules),
            "skipped_rules": skipped,
        },
    }


def _action_cfg_to_live(action: dict, key: str) -> Optional[Dict[str, dict]]:
    """One canonical ActionCfg -> a live actions-dict entry. Open (buy/sell) and
    stop_processing actions convert directly; everything else goes through the shared
    ``action_from_rule`` so option params and adjust references stay consistent with the
    forward builders."""
    at = str(action.get("action_type") or action.get("action") or "")
    if at in (BUY_ACTION, SELL_ACTION) and at not in ADJUST_ACTIONS:
        # 'sell' is also a close-flavored EXIT_ACTION; as a TradeRule action we emit the
        # bare open/close action with no reference baggage — identical either way.
        return {key: {"action_type": at}}
    if at == ExpertActionType.STOP_PROCESSING.value:
        return {key: {"action_type": at}}
    return action_from_rule(action, key=key)


def live_actions_from_trade_rule(rule: dict) -> Optional[Dict[str, dict]]:
    """A TradeRule's ordered ``actions`` list -> ONE live EventAction ``actions`` dict
    (keys ``a0``..``aN``), or None when nothing converts. Shared by the export path AND the
    backtest seeder so both build byte-identical action configs."""
    actions: Dict[str, dict] = {}
    for ai, action in enumerate(a for a in (rule.get("actions") or []) if isinstance(a, dict)):
        converted = _action_cfg_to_live(action, key=f"a{ai}")
        if converted is not None:
            actions.update(converted)
    return actions or None


def trade_rules_to_live_export(
    entry_rules: Optional[List[dict]] = None,
    exit_rules: Optional[List[dict]] = None,
    name: str = "backtest-strategy",
) -> dict:
    """Reverse of ``live_export_to_trade_rules``: TradeRule lists -> a live export-file dict.

    One live rule per TradeRule, ALL actions preserved in order (keys ``a0``..``aN``),
    ``continue_processing`` carried through, rule order becomes ``order_index``. Rules whose
    actions all fail to convert are dropped (counted nowhere — same convention as the legacy
    exporter's silent skip).
    """
    def _rules_for(rules: Optional[List[dict]], subtype: str) -> List[dict]:
        out: List[dict] = []
        for i, rule in enumerate(rules or []):
            if not isinstance(rule, dict):
                continue
            actions = live_actions_from_trade_rule(rule)
            if not actions:
                continue
            conds = rule.get("conditions")
            triggers = triggers_from_condition_tree(conds) if conds else {}
            out.append({
                "name": rule.get("name") or f"{name}-{subtype}-{i}",
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "subtype": subtype,
                "triggers": triggers,
                "actions": actions,
                "extra_parameters": {},
                "continue_processing": bool(rule.get("continue_processing")
                                            or rule.get("continueProcessing") or False),
                "order_index": len(out),
            })
        return out

    enter = _rules_for(entry_rules, AnalysisUseCase.ENTER_MARKET.value)
    op = _rules_for(exit_rules, AnalysisUseCase.OPEN_POSITIONS.value)

    rulesets: List[dict] = []
    if enter:
        rulesets.append({
            "name": f"{name} enter_market",
            "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
            "subtype": AnalysisUseCase.ENTER_MARKET.value,
            "rules": enter,
        })
    if op:
        rulesets.append({
            "name": f"{name} open_positions",
            "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
            "subtype": AnalysisUseCase.OPEN_POSITIONS.value,
            "rules": op,
        })
    return {"export_version": "1.0", "export_type": "rulesets", "rulesets": rulesets}


def _entry_rule(
    name: str,
    side_action: str,
    tree: dict,
    order_index: int,
    extra_actions: Optional[Dict[str, dict]] = None,
) -> Optional[dict]:
    """Build one enter_market export rule from a condition tree, or None if it has no triggers.

    ``extra_actions`` (e.g. entry-time ``adjust_take_profit``/``adjust_stop_loss`` brackets)
    are attached alongside the open action — live's per-rule bracket shape: each enter_market
    EventAction carries its own buy/sell + adjust_* actions."""
    triggers = triggers_from_condition_tree(tree)
    if not triggers:
        return None
    return {
        "name": name,
        "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
        "subtype": AnalysisUseCase.ENTER_MARKET.value,
        "triggers": triggers,
        "actions": {"act": {"action_type": side_action}, **(extra_actions or {})},
        "extra_parameters": {},
        "continue_processing": False,
        "order_index": order_index,
    }


def _or_branches(tree: Optional[dict]) -> List[dict]:
    """Top-level OR branches of an entry tree, each an AND-group -> ONE live rule.

    The live model expresses OR via MULTIPLE rules in a ruleset (any rule's ANDed triggers match
    -> enter); within a rule triggers are ANDed. ``triggers_from_condition_tree`` flattens ALL
    leaves of a tree into one ANDed dict, so a top-level OR of N AND-groups MUST be split into N
    rules here — otherwise the whole strategy collapses into one rule with every trigger ANDed,
    which never matches. Mirrors the forward ``groups_to_tree`` (which OR-combines N groups).
    AND/leaf trees yield a single branch (the tree itself)."""
    if not tree:
        return []
    op = str(tree.get("operator") or tree.get("type") or "AND").upper()
    if op == "OR":
        return [b for b in (tree.get("conditions") or []) if b]
    return [tree]


def strategy_to_live_export(
    buy_tree: Optional[dict] = None,
    sell_tree: Optional[dict] = None,
    exit_rules: Optional[List[dict]] = None,
    name: str = "backtest-strategy",
    entry_actions: Optional[List[dict]] = None,
) -> dict:
    """Reverse of ``live_export_to_strategy``: strategy trees/rules -> a live export-file dict.

    Produces ``export_version "1.0"``/``export_type "rulesets"`` with an ``enter_market`` ruleset
    (buy/sell rules) and an ``open_positions`` ruleset (exit rules). Delegates trigger/action
    building to ``rule_builders`` (the forward direction) so export stays consistent with import.
    Rulesets/rules with no usable content are omitted.

    ``entry_actions`` (the strategy's entry-time TP/SL bracket, same rule shape as exit rules
    minus a conditions sub-tree) are attached to EVERY enter_market rule — entry actions are
    per-rule (per condition branch) in the live model, and the backtester applies its single
    flat list on every entry regardless of which branch fired, so replicating the bracket onto
    each rule is the faithful live representation. Actions with ``enabled`` explicitly False
    (GA-disabled) are skipped.
    """
    entry_action_cfgs: Dict[str, dict] = {}
    for i, ea in enumerate(entry_actions or []):
        if not isinstance(ea, dict) or ea.get("enabled") is False:
            continue
        key = str(ea.get("id") or f"entry-{i}")
        if key == "act":  # reserved for the open action
            key = f"entry-{i}"
        converted = action_from_rule(ea, key=key)
        if converted is not None:
            entry_action_cfgs.update(converted)

    enter_rules: List[dict] = []
    for side_tree, side_action, side in ((buy_tree, BUY_ACTION, "buy"), (sell_tree, SELL_ACTION, "sell")):
        branches = _or_branches(side_tree)
        for j, branch in enumerate(branches):
            # One live rule PER OR-branch (suffix only when there's more than one, to keep names
            # stable for single-group strategies).
            rname = f"{name}-{side}" + (f"-{j + 1}" if len(branches) > 1 else "")
            r = _entry_rule(rname, side_action, branch, len(enter_rules), entry_action_cfgs)
            if r is not None:
                enter_rules.append(r)

    op_rules: List[dict] = []
    for i, rule in enumerate(exit_rules or []):
        if not isinstance(rule, dict):
            continue
        actions = action_from_rule(rule)
        if actions is None:
            continue
        conds = rule.get("conditions")
        triggers = triggers_from_condition_tree(conds) if conds else {}
        op_rules.append(
            {
                "name": rule.get("name") or f"{name}-exit-{i}",
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "subtype": AnalysisUseCase.OPEN_POSITIONS.value,
                "triggers": triggers,
                "actions": actions,
                "extra_parameters": {},
                "continue_processing": False,
                "order_index": i,
            }
        )

    rulesets: List[dict] = []
    if enter_rules:
        rulesets.append(
            {
                "name": f"{name} enter_market",
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "subtype": AnalysisUseCase.ENTER_MARKET.value,
                "rules": enter_rules,
            }
        )
    if op_rules:
        rulesets.append(
            {
                "name": f"{name} open_positions",
                "type": ExpertEventRuleType.TRADING_RECOMMENDATION_RULE.value,
                "subtype": AnalysisUseCase.OPEN_POSITIONS.value,
                "rules": op_rules,
            }
        )

    return {
        "export_version": "1.0",
        "export_type": "rulesets",
        "rulesets": rulesets,
    }
