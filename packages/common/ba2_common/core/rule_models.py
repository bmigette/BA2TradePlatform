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
    """A single condition (numeric gate or flag). Accepts every legacy alias; emits canonical."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: Optional[str] = None
    field: str = Field(validation_alias=AliasChoices("field", "event_type"))
    field_type: Optional[str] = Field(default=None, validation_alias=AliasChoices("fieldType", "field_type"))
    comparison: Optional[str] = Field(default=None, validation_alias=AliasChoices("comparison", "op", "operator"))
    value: Optional[float] = None
    optimize_enabled: bool = Field(default=False, validation_alias=AliasChoices("optimizeEnabled", "optimize_enabled", "optimize"))
    value_min: Optional[float] = Field(default=None, validation_alias=AliasChoices("valueMin", "value_min"))
    value_max: Optional[float] = Field(default=None, validation_alias=AliasChoices("valueMax", "value_max"))
    value_step: Optional[float] = Field(default=None, validation_alias=AliasChoices("valueStep", "value_step"))
    toggle_optimize: Optional[bool] = Field(default=None, validation_alias=AliasChoices("toggleOptimize", "toggle_optimize"))
    confirmation_bars: Optional[int] = Field(default=None, validation_alias=AliasChoices("confirmationBars", "confirmation_bars"))
    confirmation_bars_min: Optional[int] = Field(default=None, validation_alias=AliasChoices("confirmationBarsMin", "confirmation_bars_min"))
    confirmation_bars_max: Optional[int] = Field(default=None, validation_alias=AliasChoices("confirmationBarsMax", "confirmation_bars_max"))
    confirmation_bars_step: Optional[int] = Field(default=None, validation_alias=AliasChoices("confirmationBarsStep", "confirmation_bars_step"))

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
    reference_value: Optional[str] = Field(default=None, validation_alias=AliasChoices("referenceValue", "reference_value"))
    action_value: Optional[float] = Field(default=None, validation_alias=AliasChoices("actionValue", "action_value", "value"))
    action_value_optimize: Optional[bool] = Field(default=None, validation_alias=AliasChoices("actionValueOptimize", "action_value_optimize"))
    action_value_min: Optional[float] = Field(default=None, validation_alias=AliasChoices("actionValueMin", "action_value_min"))
    action_value_max: Optional[float] = Field(default=None, validation_alias=AliasChoices("actionValueMax", "action_value_max"))
    action_value_step: Optional[float] = Field(default=None, validation_alias=AliasChoices("actionValueStep", "action_value_step"))
    toggle_optimize: Optional[bool] = Field(default=None, validation_alias=AliasChoices("toggleOptimize", "toggle_optimize"))

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
