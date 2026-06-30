"""Shared condition-tree / exit-rule -> EventAction(triggers, actions) conversion.

SINGLE source of truth for both the live trade platform and the backtest test platform.
Reconciles the API/UI field names (action/comparison/action_value) with the canonical
EventAction shape the TradeActionEvaluator parses (event_type/operator/value, action_type).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

from ba2_common.core.types import (
    ExpertActionType,
    ExpertEventType,
    ReferenceValue,
    is_option_action,
)

# Strategy condition-tree field -> ExpertEventType for value (N_*) gates. These are the
# numeric fields an entry/exit condition tree tunes on; an unknown field is skipped (it
# never silently breaks the ruleset). COPIED VERBATIM from the test platform's
# default_rulesets._FIELD_EVENT.
FIELD_EVENT: Dict[str, ExpertEventType] = {
    "confidence": ExpertEventType.N_CONFIDENCE,
    "expected_profit": ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT,
    "expected_profit_percent": ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT,
    "expected_profit_target_percent": ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT,
    # Cooldown gates (avoid re-buying the same symbol right after exiting it). Pair with ">"
    # so the entry only fires once N days have passed since the last (qualifying) close.
    "days_since_last_close": ExpertEventType.N_DAYS_SINCE_LAST_CLOSE,
    "days_since_last_profitable_close": ExpertEventType.N_DAYS_SINCE_LAST_PROFITABLE_CLOSE,
    "days_since_last_losing_close": ExpertEventType.N_DAYS_SINCE_LAST_LOSING_CLOSE,
    # Exit (open_positions) numeric conditions.
    "profit_loss_percent": ExpertEventType.N_PROFIT_LOSS_PERCENT,
    "profit_loss_amount": ExpertEventType.N_PROFIT_LOSS_AMOUNT,
    "days_opened": ExpertEventType.N_DAYS_OPENED,
    "percent_to_current_target": ExpertEventType.N_PERCENT_TO_CURRENT_TARGET,
    "new_target_percent": ExpertEventType.N_NEW_TARGET_PERCENT,
}

# Flag (boolean) condition fields -> ExpertEventType (no operator/value). Used by exit
# (open_positions) rules whose triggers include sentiment / term / risk / rating-change /
# position flags — exactly the live open_positions trigger vocabulary. COPIED VERBATIM from
# the test platform's default_rulesets._FLAG_FIELD_EVENT.
FLAG_FIELD_EVENT: Dict[str, ExpertEventType] = {
    "bullish": ExpertEventType.F_BULLISH,
    "bearish": ExpertEventType.F_BEARISH,
    "has_position": ExpertEventType.F_HAS_POSITION,
    "has_no_position": ExpertEventType.F_HAS_NO_POSITION,
    "has_buy_position": ExpertEventType.F_HAS_BUY_POSITION,
    "has_sell_position": ExpertEventType.F_HAS_SELL_POSITION,
    "short_term": ExpertEventType.F_SHORT_TERM,
    "medium_term": ExpertEventType.F_MEDIUM_TERM,
    "long_term": ExpertEventType.F_LONG_TERM,
    "highrisk": ExpertEventType.F_HIGHRISK,
    "mediumrisk": ExpertEventType.F_MEDIUMRISK,
    "lowrisk": ExpertEventType.F_LOWRISK,
    "new_target_higher": ExpertEventType.F_NEW_TARGET_HIGHER,
    "new_target_lower": ExpertEventType.F_NEW_TARGET_LOWER,
    "current_rating_positive": ExpertEventType.F_CURRENT_RATING_POSITIVE,
    "current_rating_negative": ExpertEventType.F_CURRENT_RATING_NEGATIVE,
}

# action_type string -> (ExpertActionType, needs_reference_value). The adjust actions read
# reference_value (order_open_price/current_price/expert_target_price) + value (the % offset);
# close/sell take no params. Mirrors TradeActionEvaluator's action_config parsing. The base
# four entries are COPIED VERBATIM from the test platform's default_rulesets._EXIT_ACTION; the
# adjust_tp/adjust_sl aliases are ADDED so the API/UI shorthand resolves to the same actions.
EXIT_ACTION: Dict[str, Tuple[ExpertActionType, bool]] = {
    "close": (ExpertActionType.CLOSE, False),
    "sell": (ExpertActionType.SELL, False),
    "adjust_take_profit": (ExpertActionType.ADJUST_TAKE_PROFIT, True),
    "adjust_stop_loss": (ExpertActionType.ADJUST_STOP_LOSS, True),
    "adjust_tp": (ExpertActionType.ADJUST_TAKE_PROFIT, True),   # API/UI alias
    "adjust_sl": (ExpertActionType.ADJUST_STOP_LOSS, True),     # API/UI alias
}


# Builder word-form comparison -> engine symbol. The shared engine (TradeConditions.CompareCondition)
# accepts ONLY symbols and ValueErrors on 'gte'/'lte' — so normalise here, the single point where a
# leaf's operator becomes the trigger operator. This protects pre-existing strategies/rulesets that
# stored the builder word-form, and is once-per-leaf at seed time (zero per-bar cost).
_WORD_TO_SYMBOL = {
    "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "==", "neq": "!=", "ne": "!=",
}


def _operator_of(leaf: dict) -> str:
    # Reconcile API 'comparison' with seeding 'op'/'operator', then map any word-form to the
    # engine symbol so '>=' and 'gte' both resolve to '>='.
    raw = leaf.get("operator") or leaf.get("op") or leaf.get("comparison") or ">"
    return _WORD_TO_SYMBOL.get(str(raw).strip().lower(), raw)


def tree_leaves(node: Any) -> Iterable[dict]:
    """Yield leaf condition dicts (those with a 'field') from an AND/OR condition tree."""
    if not isinstance(node, dict):
        return
    kids = node.get("conditions")
    if kids:
        for child in kids:
            yield from tree_leaves(child)
    elif node.get("field"):
        yield node


def triggers_from_condition_tree(tree: Any) -> Dict[str, dict]:
    """Build an EventAction 'triggers' dict (ANDed) from a condition tree. Flag leaves ->
    value-less {event_type}; numeric leaves -> {event_type, operator, value}. Unknown fields
    skipped so a partial/edited tree never silently breaks the rule."""
    triggers: Dict[str, dict] = {}
    for i, leaf in enumerate(tree_leaves(tree)):
        field = str(leaf.get("field"))
        flag_et = FLAG_FIELD_EVENT.get(field)
        if flag_et is not None:
            triggers[f"cond_{i}"] = {"event_type": flag_et.value}
            continue
        num_et = FIELD_EVENT.get(field)
        if num_et is not None and leaf.get("value") is not None:
            triggers[f"cond_{i}"] = {
                "event_type": num_et.value,
                "operator": _operator_of(leaf),
                "value": leaf.get("value"),
            }
    return triggers


# Strategy rule option_* field -> the action-config key the ``TradeActionEvaluator`` reads
# when constructing an option ``TradeAction`` (see TradeActionEvaluator._create_trade_action:
# it forwards strike_method/strike_param/dte_min/dte_max/sizing/min_open_interest/max_spread_pct
# from action_config to the _OptionEntryAction ctor). These keys are the EXACT shape the live
# UI persists into ``EventAction.actions[key]`` (settings.py option-action save path), so a
# strategy option exit rule seeds an action config identical to live. Liquidity fields accept
# both the strategy option_* name and the bare evaluator name as a source. Each value is only
# emitted when present so the option action's own defaults apply otherwise.
_OPTION_ACTION_PARAM_KEYS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("strike_method", ("option_strike_method",)),
    ("strike_param", ("option_strike_param",)),
    ("dte_min", ("option_dte_min",)),
    ("dte_max", ("option_dte_max",)),
    ("sizing", ("option_sizing",)),
    ("min_open_interest", ("option_min_oi", "option_min_open_interest")),
    ("max_spread_pct", ("option_max_spread_pct", "option_max_spread")),
    ("wing_width_pct", ("option_wing_width_pct", "option_wing_width")),
)


def _option_action_config(raw: str, rule: dict) -> dict:
    """Build the option action config for ``raw`` (an option ExpertActionType value), pulling
    the selection params from the rule's ``option_*`` fields into the evaluator's keys.

    ``close_option`` resolves its contract from the held position, so it carries no selection
    params (mirrors live, where CLOSE_OPTION takes none)."""
    cfg: dict = {"action_type": raw}
    if raw == ExpertActionType.CLOSE_OPTION.value:
        return cfg
    for cfg_key, source_keys in _OPTION_ACTION_PARAM_KEYS:
        for src in source_keys:
            val = rule.get(src)
            if val is not None:
                cfg[cfg_key] = val
                break
    return cfg


def action_from_rule(rule: dict, key: str = "act") -> Optional[Dict[str, dict]]:
    """Build an EventAction 'actions' dict for one exit/entry rule, or None if the action is
    unknown. Accepts API 'action' or seeding 'action_type'; value from 'value' or
    'action_value'.

    OPTION actions (buy_call/buy_put/sell_covered_call/open_*_spread/open_straddle/
    open_strangle/close_option, ...) emit an action config carrying the option selection
    params (strike_method/strike_param/dte_min/dte_max/sizing/liquidity) in the EXACT shape
    the ``TradeActionEvaluator`` reads — so the BACKTEST builds the option ``TradeAction``
    identically to live."""
    raw = rule.get("action_type") or rule.get("action")
    if is_option_action(str(raw)):
        return {key: _option_action_config(str(raw), rule)}
    spec = EXIT_ACTION.get(str(raw))
    if spec is None:
        return None
    action_type, needs_ref = spec
    cfg: dict = {"action_type": action_type.value}
    if needs_ref:
        cfg["reference_value"] = rule.get("reference_value") or ReferenceValue.ORDER_OPEN_PRICE.value
        val = rule.get("value")
        cfg["value"] = val if val is not None else rule.get("action_value")
    return {key: cfg}


# --- REVERSE direction (live ruleset / export file -> backtester condition tree) -------------
# Re-exported here so callers have a SINGLE import point for all rules/ruleset conversion. This
# import sits at the BOTTOM on purpose: ``rules_convert`` imports the canonical maps + forward
# converters from THIS module, so the re-export must run after those are defined (no circular
# import — by the time this line executes, the names above already exist).
from ba2_common.core.rules_convert import (  # noqa: E402
    ACTION_VALUES,
    ADJUST_ACTIONS,
    BUY_ACTION,
    FLAG_EVENT_VALUES,
    NUMERIC_EVENT_VALUES,
    SELL_ACTION,
    entry_action_side,
    eventaction_to_entry_group,
    eventaction_to_exit_rule,
    groups_to_tree,
    live_export_to_strategy,
    opt_range,
    strategy_to_live_export,
    trigger_to_leaf,
)
