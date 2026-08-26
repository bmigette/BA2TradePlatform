"""Canonical condition-tree / exit-rule models — the SINGLE source of truth for the rule
format shared by the live platform and the backtest test platform.

WHY THIS EXISTS
---------------
One logical condition was historically represented several ways:
  * builder/UI:        ``operator`` groups, ``comparison: "gte"``, camelCase optimize keys
  * storage/optimizer: ``type`` groups, ``op: ">="``, snake optimize keys, NO ``fieldType``
  * live import:       ``operator`` groups, ``comparison: ">="``
The shared engine (``TradeConditions``) is SYMBOL-only (``'>=': operator.ge``) and rejects
``gte``. Loading a storage-format tree into the builder produced "Empty field" because the UI
detects groups by ``operator`` while storage uses ``type``. These models END that by being the
one contract: they ACCEPT every legacy spelling on input and EMIT a canonical SUPERSET dict.

PERFORMANCE / SCOPE
-------------------
These models run ONLY at boundaries (API request/response, export, import, load, save) — never
in the per-bar engine hot path and never per-trial in the optimizer (those keep reading plain
dicts). Validation cost is paid once when data crosses a boundary.

BACKWARD-COMPATIBILITY (no DB migration)
----------------------------------------
``to_canonical_dict`` emits a SUPERSET: the canonical camelCase builder keys PLUS the snake
aliases (``op``, ``type``, ``value_min/max/step``, ``optimize``) that the untouched
engine/optimizer readers expect. So normalising a tree changes no existing reader, and existing
DB rows are read-and-normalised, never rewritten.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from ba2_common.core.rule_builders import FIELD_EVENT, FLAG_FIELD_EVENT

# ---------------------------------------------------------------------------
# Comparison vocabulary: builder word-forms <-> engine symbols. The engine
# (TradeConditions.CompareCondition) accepts ONLY the symbols, so canonical = symbol.
# ---------------------------------------------------------------------------
_COMPARISON_TO_SYMBOL: Dict[str, str] = {
    "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "==", "neq": "!=", "ne": "!=",
    ">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "==", "!=": "!=",
    # value-less / range comparisons pass through unchanged
    "between": "between", "is_true": "is_true", "is_false": "is_false",
}

# UI fieldType values that are meaningful on their own (ML strategy fields). When a leaf carries
# one of these we keep it; otherwise we infer flag/numeric from the shared event maps.
_EXPLICIT_FIELD_TYPES = {"model_probability", "model_class", "position", "time", "price", "trade"}


def normalize_comparison(op: Optional[str]) -> Optional[str]:
    """Map any comparison spelling (``gte``/``>=``) to the engine symbol (``>=``). None-safe."""
    if op is None:
        return None
    return _COMPARISON_TO_SYMBOL.get(str(op).strip().lower(), str(op))


def infer_field_type(field: Optional[str], given: Optional[str]) -> str:
    """Resolve a leaf's fieldType. Known flag fields -> 'flag'; known numeric fields ->
    'numeric'; an explicit ML/position/etc. type is respected; prefixed fields keep their
    prefix; otherwise default to 'numeric'. ('model_probability' is treated as the legacy
    blank default and overridden by flag/numeric inference.)"""
    f = (field or "").strip()
    g = (given or "").strip()
    if f in FLAG_FIELD_EVENT:
        return "flag"
    if f in FIELD_EVENT:
        return "numeric"
    if g and g != "model_probability":
        return g
    if ":" in f:  # ML / position / time / price prefixed field
        return g or f.split(":", 1)[0]
    return g or "numeric"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class ConditionLeaf(BaseModel):
    """A single condition (numeric gate or flag). Accepts every legacy alias; emits canonical.

    Alias order is snake_case-first everywhere a camelCase mirror exists -- see ActionCfg's
    docstring above for why: a persisted condition can carry both spellings with DIFFERENT
    values when something (e.g. GA gene decode) mutates only the snake_case copy in place,
    and AliasChoices silently picks whichever alias is listed first when both are present."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: Optional[str] = None
    field: str = Field(validation_alias=AliasChoices("field", "event_type"))
    field_type: Optional[str] = Field(default=None, validation_alias=AliasChoices("field_type", "fieldType"))
    comparison: Optional[str] = Field(default=None, validation_alias=AliasChoices("comparison", "op", "operator"))
    value: Optional[float] = None
    optimize_enabled: bool = Field(default=False, validation_alias=AliasChoices("optimize_enabled", "optimize", "optimizeEnabled"))
    value_min: Optional[float] = Field(default=None, validation_alias=AliasChoices("value_min", "valueMin"))
    value_max: Optional[float] = Field(default=None, validation_alias=AliasChoices("value_max", "valueMax"))
    value_step: Optional[float] = Field(default=None, validation_alias=AliasChoices("value_step", "valueStep"))
    #: RELATIVE threshold: the id of another leaf in the SAME condition tree whose threshold
    #: this leaf's optimizer gene is measured FROM (decoded value = that leaf's value + gene;
    #: see strategy_param_space._apply_to_tree). Lets an interval on ONE field be expressed as
    #: (floor, width > 0) instead of two independent absolute bounds, roughly half of whose
    #: joint grid is an empty conjunction that can never fire. ``value`` itself stays ABSOLUTE
    #: so an un-decoded template still seeds a correct ruleset; only value_min/max/step
    #: describe the width.
    #: MUST be a DECLARED field: extra='allow' keeps unknown keys on the model, but
    #: to_canonical_dict rebuilds the dict from declared fields only, so an undeclared key is
    #: silently dropped by normalize_trade_rules and the gene reverts to an absolute threshold.
    value_offset_from: Optional[str] = Field(default=None, validation_alias=AliasChoices("value_offset_from", "valueOffsetFrom"))
    toggle_optimize: Optional[bool] = Field(default=None, validation_alias=AliasChoices("toggle_optimize", "toggleOptimize"))
    confirmation_bars: Optional[int] = Field(default=None, validation_alias=AliasChoices("confirmation_bars", "confirmationBars"))
    confirmation_bars_min: Optional[int] = Field(default=None, validation_alias=AliasChoices("confirmation_bars_min", "confirmationBarsMin"))
    confirmation_bars_max: Optional[int] = Field(default=None, validation_alias=AliasChoices("confirmation_bars_max", "confirmationBarsMax"))
    confirmation_bars_step: Optional[int] = Field(default=None, validation_alias=AliasChoices("confirmation_bars_step", "confirmationBarsStep"))

    def to_canonical_dict(self) -> Dict[str, Any]:
        ftype = infer_field_type(self.field, self.field_type)
        is_flag = ftype == "flag"
        # Flags carry the 'is_true' sentinel (operator/value hidden in the UI); numeric leaves
        # carry the engine symbol. Default numeric op to '>=' when none was supplied.
        if is_flag:
            comp = "is_true"
        else:
            comp = normalize_comparison(self.comparison) or ">="
        out: Dict[str, Any] = {
            "id": self.id,
            "field": self.field,
            # canonical (builder) + snake alias (engine/optimizer read both)
            "fieldType": ftype,
            "field_type": ftype,
            "comparison": comp,
            "op": comp,            # engine _operator_of fallback
            "optimizeEnabled": bool(self.optimize_enabled),
            "optimize": bool(self.optimize_enabled),  # optimizer reads 'optimize'
        }
        if not is_flag and self.value is not None:
            out["value"] = self.value
        if self.value_min is not None:
            out["valueMin"] = self.value_min
            out["value_min"] = self.value_min
        if self.value_max is not None:
            out["valueMax"] = self.value_max
            out["value_max"] = self.value_max
        if self.value_step is not None:
            out["valueStep"] = self.value_step
            out["value_step"] = self.value_step
        if self.value_offset_from is not None:
            out["valueOffsetFrom"] = self.value_offset_from
            out["value_offset_from"] = self.value_offset_from
        if self.toggle_optimize is not None:
            out["toggleOptimize"] = self.toggle_optimize
            out["toggle_optimize"] = self.toggle_optimize
        for camel, snake, val in (
            ("confirmationBars", "confirmation_bars", self.confirmation_bars),
            ("confirmationBarsMin", "confirmation_bars_min", self.confirmation_bars_min),
            ("confirmationBarsMax", "confirmation_bars_max", self.confirmation_bars_max),
            ("confirmationBarsStep", "confirmation_bars_step", self.confirmation_bars_step),
        ):
            if val is not None:
                out[camel] = val
                out[snake] = val
        if self.id is None:
            out.pop("id")
        return out


class ConditionGroup(BaseModel):
    """An AND/OR group of leaves and sub-groups. Accepts ``operator`` OR ``type`` for the
    boolean; emits both so the builder (operator) and engine _gate_trigger_groups (type) agree."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: Optional[str] = None
    operator: str = Field(default="AND", validation_alias=AliasChoices("operator", "type"))
    conditions: List[Any] = Field(default_factory=list)

    def to_canonical_dict(self) -> Dict[str, Any]:
        op = str(self.operator or "AND").upper()
        if op not in ("AND", "OR"):
            op = "AND"
        out: Dict[str, Any] = {
            "operator": op,
            "type": op,  # engine _gate_trigger_groups reads 'type' (or 'operator')
            "conditions": [_node_to_canonical(c) for c in (self.conditions or [])],
        }
        if self.id is not None:
            out = {"id": self.id, **out}
        return out


def _is_group(node: Any) -> bool:
    return isinstance(node, dict) and node.get("conditions") is not None


def _node_to_canonical(node: Any) -> Dict[str, Any]:
    """Normalise ONE tree node (group or leaf) to canonical. Non-dict input -> empty group."""
    if not isinstance(node, dict):
        return ConditionGroup().to_canonical_dict()
    if _is_group(node):
        return ConditionGroup.model_validate(node).to_canonical_dict()
    return ConditionLeaf.model_validate(node).to_canonical_dict()


def normalize_tree(tree: Any) -> Optional[Dict[str, Any]]:
    """Normalise a buy/sell entry condition TREE to canonical. None passes through as None
    (an absent tree), so a buy-only strategy keeps sell_tree=None."""
    if tree is None:
        return None
    if not isinstance(tree, dict):
        return ConditionGroup().to_canonical_dict()
    return _node_to_canonical(tree)


class ExitRule(BaseModel):
    """One exit (open_positions) rule: a condition group + an action. Unknown keys (option_*,
    etc.) are preserved via extra='allow' so option exit rules round-trip losslessly."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: Optional[str] = None
    name: Optional[str] = None
    conditions: Optional[Any] = None
    action: Optional[str] = Field(default=None, validation_alias=AliasChoices("action", "action_type"))
    reference_value: Optional[str] = Field(default=None, validation_alias=AliasChoices("reference_value", "referenceValue"))
    action_value: Optional[float] = Field(default=None, validation_alias=AliasChoices("action_value", "value", "actionValue"))
    action_value_optimize: Optional[bool] = Field(default=None, validation_alias=AliasChoices("action_value_optimize", "actionValueOptimize"))
    action_value_min: Optional[float] = Field(default=None, validation_alias=AliasChoices("action_value_min", "actionValueMin"))
    action_value_max: Optional[float] = Field(default=None, validation_alias=AliasChoices("action_value_max", "actionValueMax"))
    action_value_step: Optional[float] = Field(default=None, validation_alias=AliasChoices("action_value_step", "actionValueStep"))
    toggle_optimize: Optional[bool] = Field(default=None, validation_alias=AliasChoices("toggle_optimize", "toggleOptimize"))

    def to_canonical_dict(self) -> Dict[str, Any]:
        # Preserve any extra keys (option_strike_param, option_dte_min, enabled, etc.) verbatim.
        extra = {k: v for k, v in (self.__pydantic_extra__ or {}).items()}
        out: Dict[str, Any] = dict(extra)
        if self.id is not None:
            out["id"] = self.id
        if self.name is not None:
            out["name"] = self.name
        if self.conditions is not None:
            out["conditions"] = _node_to_canonical(self.conditions)
        if self.action is not None:
            # both spellings: builder reads 'action', seeding reads 'action_type'
            out["action"] = self.action
            out["action_type"] = self.action
        if self.reference_value is not None:
            out["referenceValue"] = self.reference_value
            out["reference_value"] = self.reference_value
        if self.action_value is not None:
            out["actionValue"] = self.action_value
            out["action_value"] = self.action_value
        if self.action_value_optimize is not None:
            out["actionValueOptimize"] = self.action_value_optimize
            out["action_value_optimize"] = self.action_value_optimize
        for camel, snake, val in (
            ("actionValueMin", "action_value_min", self.action_value_min),
            ("actionValueMax", "action_value_max", self.action_value_max),
            ("actionValueStep", "action_value_step", self.action_value_step),
        ):
            if val is not None:
                out[camel] = val
                out[snake] = val
        if self.toggle_optimize is not None:
            out["toggleOptimize"] = self.toggle_optimize
            out["toggle_optimize"] = self.toggle_optimize
        return out


def normalize_exit_rules(rules: Any) -> List[Dict[str, Any]]:
    """Normalise a list of exit rules to canonical. Non-list / non-dict entries are skipped."""
    if not isinstance(rules, list):
        return []
    out: List[Dict[str, Any]] = []
    for r in rules:
        if isinstance(r, dict):
            out.append(ExitRule.model_validate(r).to_canonical_dict())
    return out


def normalize_ruleset(buy: Any = None, sell: Any = None, exits: Any = None) -> Dict[str, Any]:
    """Convenience: normalise a full ruleset (buy tree, sell tree, exit rules) at once."""
    return {
        "buy_entry_conditions": normalize_tree(buy),
        "sell_entry_conditions": normalize_tree(sell),
        "exit_conditions": normalize_exit_rules(exits),
    }


# ---------------------------------------------------------------------------
# Unified rule model (EventAction-shaped): TradeRule = conditions + actions[] +
# continue_processing. The canonical Strategy shape going forward — mirrors the live
# EventAction 1:1 (ordered rules, first-match unless continue_processing, N actions per
# rule), ending the split between condition trees and a flat shared action list.
# ---------------------------------------------------------------------------
class ActionCfg(BaseModel):
    """ONE action of a TradeRule (open/close/adjust/option). Accepts every legacy spelling
    (``action``/``action_type``/``actionType``; ``value``/``action_value``); ``option_*``
    selection params and other extras are preserved verbatim.

    Alias order is snake_case-first (matching ``action_type``'s original order) on EVERY
    field here, not just for style: a persisted rule can carry BOTH spellings of a field
    with DIFFERENT values when something mutates only the snake_case copy in place (e.g. a
    GA gene decode writing ``action_value``/``value`` without also updating the camelCase
    mirror) — ``AliasChoices`` silently picks whichever alias is listed FIRST when several
    are present, so a camelCase-first order picks the STALE value. snake_case/``value`` are
    what the actual execution engine (``TradeActionEvaluator``/``TradeActions.py``) and the
    frontend's own read-back (``fmtExitAction``) already treat as authoritative, so they must
    win here too. Confirmed live: this exact ordering corrupted an exported/deployed backtest
    rule's TP (50 instead of the real 48) and SL (2 instead of the real 1) — see
    packages/common/ba2_common/core/TradeActions.py history for the incident."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: Optional[str] = None
    action_type: str = Field(validation_alias=AliasChoices("action_type", "action", "actionType"))
    reference_value: Optional[str] = Field(default=None, validation_alias=AliasChoices("reference_value", "referenceValue"))
    action_value: Optional[float] = Field(default=None, validation_alias=AliasChoices("action_value", "value", "actionValue"))
    action_value_optimize: Optional[bool] = Field(default=None, validation_alias=AliasChoices("action_value_optimize", "actionValueOptimize"))
    action_value_min: Optional[float] = Field(default=None, validation_alias=AliasChoices("action_value_min", "actionValueMin"))
    action_value_max: Optional[float] = Field(default=None, validation_alias=AliasChoices("action_value_max", "actionValueMax"))
    action_value_step: Optional[float] = Field(default=None, validation_alias=AliasChoices("action_value_step", "actionValueStep"))
    toggle_optimize: Optional[bool] = Field(default=None, validation_alias=AliasChoices("toggle_optimize", "toggleOptimize"))

    def to_canonical_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {k: v for k, v in (self.__pydantic_extra__ or {}).items()}
        if self.id is not None:
            out["id"] = self.id
        # both spellings, same convention as ExitRule (builder reads 'action', seeding
        # 'action_type'); 'value' additionally mirrors the live EventAction actions-cfg key.
        out["action"] = self.action_type
        out["action_type"] = self.action_type
        if self.reference_value is not None:
            out["referenceValue"] = self.reference_value
            out["reference_value"] = self.reference_value
        if self.action_value is not None:
            out["actionValue"] = self.action_value
            out["action_value"] = self.action_value
            out["value"] = self.action_value
        if self.action_value_optimize is not None:
            out["actionValueOptimize"] = self.action_value_optimize
            out["action_value_optimize"] = self.action_value_optimize
        for camel, snake, val in (
            ("actionValueMin", "action_value_min", self.action_value_min),
            ("actionValueMax", "action_value_max", self.action_value_max),
            ("actionValueStep", "action_value_step", self.action_value_step),
        ):
            if val is not None:
                out[camel] = val
                out[snake] = val
        if self.toggle_optimize is not None:
            out["toggleOptimize"] = self.toggle_optimize
            out["toggle_optimize"] = self.toggle_optimize
        return out


# Keys that belong to the RULE when lifting a legacy single-action row (everything else is
# treated as part of that row's single action, so option_* params travel with the action).
_RULE_LEVEL_KEYS = {
    "id", "name", "conditions", "enabled",
    "continue_processing", "continueProcessing",
    "toggle_optimize", "toggleOptimize",
    "actions",
}


def _lift_legacy_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    """Lift a LEGACY single-action rule row (the exit_conditions / entry_actions shape:
    action fields at the top level) into the TradeRule shape. New-shape rules (with an
    ``actions`` list) pass through unchanged."""
    if "actions" in rule and isinstance(rule["actions"], list):
        return rule
    action = {k: v for k, v in rule.items() if k not in _RULE_LEVEL_KEYS}
    lifted = {k: v for k, v in rule.items() if k in _RULE_LEVEL_KEYS}
    lifted["actions"] = [action] if action else []
    return lifted


class TradeRule(BaseModel):
    """One EventAction-shaped strategy rule: conditions + ordered actions + continue flag.

    Mirrors the live ``EventAction`` contract exactly: within a ruleset rules evaluate in
    order, the first rule whose conditions match fires ALL its actions, and evaluation stops
    unless ``continue_processing`` is True. ``conditions: None`` means "always matches"
    (engine base triggers only). Accepts the new shape AND legacy single-action rows (lift
    via :func:`_lift_legacy_rule` / :func:`normalize_trade_rules`)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: Optional[str] = None
    name: Optional[str] = None
    conditions: Optional[Any] = None
    actions: List[Any] = Field(default_factory=list)
    continue_processing: bool = Field(default=False, validation_alias=AliasChoices("continue_processing", "continueProcessing"))
    toggle_optimize: Optional[bool] = Field(default=None, validation_alias=AliasChoices("toggle_optimize", "toggleOptimize"))

    def to_canonical_dict(self) -> Dict[str, Any]:
        extra = {k: v for k, v in (self.__pydantic_extra__ or {}).items()}
        out: Dict[str, Any] = dict(extra)
        if self.id is not None:
            out["id"] = self.id
        if self.name is not None:
            out["name"] = self.name
        if self.conditions is not None:
            out["conditions"] = _node_to_canonical(self.conditions)
        out["actions"] = [
            ActionCfg.model_validate(a).to_canonical_dict()
            for a in (self.actions or [])
            if isinstance(a, dict)
        ]
        out["continueProcessing"] = bool(self.continue_processing)
        out["continue_processing"] = bool(self.continue_processing)
        if self.toggle_optimize is not None:
            out["toggleOptimize"] = self.toggle_optimize
            out["toggle_optimize"] = self.toggle_optimize
        return out


def normalize_trade_rules(rules: Any) -> List[Dict[str, Any]]:
    """Normalise a list of rules to the canonical TradeRule shape. Accepts new-shape rules
    (``actions`` list) AND legacy single-action rows (exit_conditions / entry_actions shape)
    in the same list. Non-list input / non-dict entries are skipped."""
    if not isinstance(rules, list):
        return []
    out: List[Dict[str, Any]] = []
    for r in rules:
        if isinstance(r, dict):
            out.append(TradeRule.model_validate(_lift_legacy_rule(r)).to_canonical_dict())
    return out


def trade_rules_from_legacy(
    buy_tree: Any = None,
    sell_tree: Any = None,
    entry_actions: Any = None,
    exit_conditions: Any = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Convert the LEGACY Strategy trio (buy/sell condition trees + flat entry_actions +
    single-action exit rows) into ``{"entry_rules": [...], "exit_rules": [...]}``.

    Entry: one TradeRule per top-level OR branch of each side's tree (an AND/leaf tree is a
    single rule), actions = the side's open action + the flat entry_actions bracket replicated
    per rule — the exact semantics the old seeder implemented, now explicit in the data. The
    old seeder also implicitly ANDed base gates (bullish/bearish + has_no_position) onto every
    entry rule; the unified seeder is 1:1 with no magic, so those base leaves are PREPENDED
    here explicitly whenever the branch doesn't already carry them. A ``None`` tree
    contributes no rules for that side.
    Exit: each legacy row lifts to a one-action TradeRule (order preserved).
    """
    bracket = [a for a in (entry_actions or []) if isinstance(a, dict)]

    def _branches(tree: Any) -> List[Any]:
        if not isinstance(tree, dict):
            return []
        op = str(tree.get("operator") or tree.get("type") or "AND").upper()
        if op == "OR":
            return [b for b in (tree.get("conditions") or []) if isinstance(b, dict)]
        return [tree]

    def _leaf_fields(node: Any) -> set:
        if not isinstance(node, dict):
            return set()
        fields = set()
        for child in (node.get("conditions") or []):
            fields |= _leaf_fields(child)
        if node.get("field"):
            fields.add(node["field"])
        return fields

    def _with_base_gates(branch: dict, side: str, rid: str) -> dict:
        present = _leaf_fields(branch)
        signal = "bullish" if side == "buy" else "bearish"
        base = []
        if signal not in present:
            base.append({"id": f"{rid}-{signal}", "field": signal, "field_type": "flag"})
        if "has_no_position" not in present:
            base.append({"id": f"{rid}-flat", "field": "has_no_position", "field_type": "flag"})
        if not base:
            return branch
        kids = branch.get("conditions") if isinstance(branch, dict) else None
        if isinstance(kids, list):
            merged = dict(branch)
            merged["conditions"] = base + kids
            return merged
        return {"id": f"{rid}-grp", "operator": "AND", "conditions": base + [branch]}

    entry_rules: List[Dict[str, Any]] = []
    for tree, open_action, side in ((buy_tree, "buy", "buy"), (sell_tree, "sell", "sell")):
        branches = _branches(tree)
        for j, branch in enumerate(branches):
            suffix = f"-{j + 1}" if len(branches) > 1 else ""
            rid = f"{side}{suffix}"
            entry_rules.append({
                "id": rid,
                "name": f"enter-{side}{suffix}",
                "conditions": _with_base_gates(branch, side, rid),
                "actions": [{"action_type": open_action}] + [dict(a) for a in bracket],
                "continue_processing": False,
            })

    return {
        "entry_rules": normalize_trade_rules(entry_rules),
        "exit_rules": normalize_trade_rules(exit_conditions or []),
    }
