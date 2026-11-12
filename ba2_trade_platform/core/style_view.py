"""Read-only "style summary" of a composed option (O_STYLE) expert instance.

A style expert is an ordinary ExpertInstance whose ENTER ruleset holds one or more entry rules
per member (order_index = precedence, first match wins) and whose OPEN_POSITIONS ruleset holds
exit rules, most scoped to one member by an ``entry_tag == N`` leaf. This module only READS and
RENDERS that structure; it never changes a rule and never invents a value: a field it cannot
make readable is shown verbatim (``repr``) rather than dropped.

``build_style_summary`` is pure (plain dicts in, plain dicts out) so it is testable without a
DB; ``load_style_summary`` is the thin DB loader the UI calls.
"""
import re
from typing import Any, Dict, List, Optional

ENTRY_TAG_FIELD = "entry_tag"
_RULE_PREFIX = re.compile(r"^(O_[A-Za-z0-9]+)[-_ ]")

# Gate grouping by event_type. First match wins; everything else is "other".
_MARKET_PREFIXES = ("underlying_", "structure_", "channel_", "market_", "regime")
_SIGNAL_PREFIXES = ("current_rating", "rec_", "confidence", "expected_profit", "rating", "has_")

# Action keys shown after structure / DTE / strike, in this order; the rest follow alphabetically.
_PARAM_ORDER = [("sizing", "sizing %"), ("entry_cross", "entry cross"), ("min_volume", "min volume")]
_PARAM_SKIP = {"action_type", "entry_tag", "dte_min", "dte_max", "strike_method", "strike_param"}


def _group_of(field: str) -> str:
    if field.startswith(_MARKET_PREFIXES):
        return "market"
    if field.startswith("iv_") or field == "iv" or "_iv_" in field:
        return "iv"
    if field.startswith(_SIGNAL_PREFIXES):
        return "signal"
    return "other"


def _fmt_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _leaf_text(field: str, operator: Optional[str], value: Any) -> str:
    if operator is None:
        return field
    if operator in ("is_true", "is_false"):
        return f"{field} {operator}"
    return f"{field} {operator} {_fmt_value(value)}"


def _ordered_keys(d: Dict[str, Any]) -> List[str]:
    return sorted(d or {}, key=lambda k: (len(k), k))


def _parse_triggers(triggers: Dict[str, Any]):
    """(gates, tag, unreadable). Gates exclude the entry_tag scoping leaf. Leaves are
    ``{cond_N: {event_type, operator?, value?}}``; anything of another shape is returned
    verbatim in ``unreadable`` instead of being dropped."""
    gates: List[Dict[str, str]] = []
    tags: List[Any] = []
    unreadable: List[str] = []
    for key in _ordered_keys(triggers):
        leaf = triggers[key]
        if not isinstance(leaf, dict) or "event_type" not in leaf:
            unreadable.append(f"{key}: {leaf!r}")
            continue
        field = leaf["event_type"]
        operator = leaf["operator"] if "operator" in leaf else None
        value = leaf["value"] if "value" in leaf else None
        if field == ENTRY_TAG_FIELD:
            tags.append(value)
            continue
        gates.append({"group": _group_of(field), "field": field,
                      "text": _leaf_text(field, operator, value)})
    tag = None
    if tags:
        try:
            tag = int(float(tags[0]))
        except (TypeError, ValueError):
            unreadable.append(f"entry_tag: {tags[0]!r}")
    return gates, tag, unreadable


def _entry_action(actions: Dict[str, Any]):
    """The first action carrying a member tag: (action dict, tag) or (None, None)."""
    for key in _ordered_keys(actions):
        act = actions[key]
        if not isinstance(act, dict):
            continue
        raw = act["option_entry_tag"] if "option_entry_tag" in act else (
            act["entry_tag"] if "entry_tag" in act else None)
        if raw is not None and not isinstance(raw, bool):
            try:
                return act, int(raw)
            except (TypeError, ValueError):
                continue
    return None, None


def _params(act: Dict[str, Any]) -> List[Dict[str, str]]:
    a = {(k[len("option_"):] if k.startswith("option_") else k): v for k, v in act.items()}
    out: List[Dict[str, str]] = []
    if "action_type" in a:
        out.append({"label": "structure", "value": str(a["action_type"])})
    if "dte_min" in a or "dte_max" in a:
        lo = _fmt_value(a["dte_min"]) if "dte_min" in a else "?"
        hi = _fmt_value(a["dte_max"]) if "dte_max" in a else "?"
        out.append({"label": "DTE", "value": f"{lo}-{hi}"})
    if "strike_method" in a:
        param = f" {_fmt_value(a['strike_param'])}" if "strike_param" in a else ""
        out.append({"label": "strike", "value": f"{a['strike_method']}{param}"})
    elif "strike_param" in a:
        out.append({"label": "strike", "value": _fmt_value(a["strike_param"])})
    shown = set()
    for key, label in _PARAM_ORDER:
        if key in a:
            out.append({"label": label, "value": _fmt_value(a[key])})
            shown.add(key)
    for key in sorted(a):
        if key in _PARAM_SKIP or key in shown:
            continue
        out.append({"label": key, "value": _fmt_value(a[key])})
    return out


def _structure_name(rule_name: str, act: Dict[str, Any]) -> str:
    m = _RULE_PREFIX.match(rule_name or "")
    if m:
        return m.group(1).upper()
    return str(act["action_type"]) if "action_type" in act else "?"


def _action_types(actions: Dict[str, Any]) -> List[str]:
    return [str(a["action_type"]) for a in (actions or {}).values()
            if isinstance(a, dict) and "action_type" in a]


def build_style_summary(enter_rules: List[Dict[str, Any]], open_rules: List[Dict[str, Any]],
                        header: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Style summary from rules given IN PRECEDENCE ORDER, each a dict with ``name``,
    ``triggers``, ``actions``, ``continue_processing``. None when no entry action carries a
    member tag (not a style expert)."""
    members: Dict[int, Dict[str, Any]] = {}
    precedence = 0
    for rule in enter_rules or []:
        act, tag = _entry_action(rule["actions"])
        if act is None:
            continue
        precedence += 1
        gates, _scope, unreadable = _parse_triggers(rule["triggers"])
        member = members.get(tag)
        if member is None:
            member = {"tag": tag, "structure": _structure_name(rule["name"], act),
                      "rank": len(members) + 1, "entries": [], "exits": []}
            members[tag] = member
        member["entries"].append({
            "name": rule["name"], "precedence": precedence,
            "continue_processing": bool(rule["continue_processing"]),
            "params": _params(act),
            "gates": {g: [x["text"] for x in gates if x["group"] == g]
                      for g in ("signal", "iv", "market", "other")},
            "unreadable": unreadable,
        })
    if not members:
        return None

    all_exits: List[Dict[str, Any]] = []
    orphan_exits: List[Dict[str, Any]] = []
    for rule in open_rules or []:
        gates, tag, unreadable = _parse_triggers(rule["triggers"])
        exit_rule = {"name": rule["name"], "tag": tag,
                     "gates": [g["text"] for g in gates],
                     "actions": _action_types(rule["actions"]), "unreadable": unreadable,
                     "continue_processing": bool(rule["continue_processing"])}
        if tag is None:
            all_exits.append(exit_rule)
        elif tag in members:
            members[tag]["exits"].append(exit_rule)
        else:
            orphan_exits.append(exit_rule)
    return {"header": dict(header or {}), "members": list(members.values()),
            "all_member_exits": all_exits, "orphan_exits": orphan_exits}


def _rule_dicts(ruleset_id: Optional[int]) -> List[Dict[str, Any]]:
    if ruleset_id is None:
        return []
    from .db import ruleset_event_actions
    return [{"name": r.name, "triggers": r.triggers, "actions": r.actions,
             "continue_processing": r.continue_processing}
            for r in ruleset_event_actions(ruleset_id)]


def load_style_summary(instance_id: int) -> Optional[Dict[str, Any]]:
    """Summary for a stored ExpertInstance; None when missing or not a style expert."""
    from .db import get_instance
    from .models import AccountDefinition, ExpertInstance
    inst = get_instance(ExpertInstance, instance_id)
    if inst is None:
        return None
    account = get_instance(AccountDefinition, inst.account_id)
    header = {"id": inst.id, "alias": inst.alias, "expert": inst.expert, "enabled": inst.enabled,
              "account": account.name if account is not None else f"#{inst.account_id}",
              "virtual_equity_pct": inst.virtual_equity_pct}
    return build_style_summary(_rule_dicts(inst.enter_market_ruleset_id),
                               _rule_dicts(inst.open_positions_ruleset_id), header)
