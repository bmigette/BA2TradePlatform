"""Shared condition-tree / exit-rule -> EventAction(triggers, actions) conversion.

SINGLE source of truth for both the live trade platform and the backtest test platform.
Reconciles the API/UI field names (action/comparison/action_value) with the canonical
EventAction shape the TradeActionEvaluator parses (event_type/operator/value, action_type).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

from ba2_common.logger import logger
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
    # Remaining option life (expiry - the evaluation date), the complement of days_opened.
    # MUST be listed here or the leaf is silently dropped by triggers_from_condition_tree
    # ("an unknown field is skipped") and the engine never sees the gate at all.
    "days_to_expiry": ExpertEventType.N_DAYS_TO_EXPIRY,
    "percent_to_current_target": ExpertEventType.N_PERCENT_TO_CURRENT_TARGET,
    "new_target_percent": ExpertEventType.N_NEW_TARGET_PERCENT,
    # --- registered-but-unmapped numeric conditions -----------------------------------
    # Every one of these has a working condition class in TradeConditions.CONDITION_MAP and
    # was reachable from the UI / strategy builder, but was absent HERE — so the leaf was
    # dropped by triggers_from_condition_tree and the engine never evaluated the gate while
    # the GA kept tuning its :value/:enabled genes. On the built OS1 option group that was
    # 40 of 77 genes (the four price_vs_target_* gates on all five members).
    # The invariant is now enforced by
    # packages/common/tests/test_condition_registry_coverage.py.
    "percent_to_new_target": ExpertEventType.N_PERCENT_TO_NEW_TARGET,
    "percent_open_to_new_target": ExpertEventType.N_PERCENT_OPEN_TO_NEW_TARGET,
    # Price vs. the FMPRating analyst target range — the option grid's dip/extension gates.
    "price_vs_target_low_percent": ExpertEventType.N_PRICE_VS_TARGET_LOW_PERCENT,
    "price_vs_target_high_percent": ExpertEventType.N_PRICE_VS_TARGET_HIGH_PERCENT,
    "price_vs_target_consensus_percent": ExpertEventType.N_PRICE_VS_TARGET_CONSENSUS_PERCENT,
    "instrument_account_share": ExpertEventType.N_INSTRUMENT_ACCOUNT_SHARE,
    "percent_below_recent_high": ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH,
    "percent_above_recent_low": ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW,
    "iv_rank": ExpertEventType.N_IV_RANK,
    "days_to_earnings": ExpertEventType.N_DAYS_TO_EARNINGS,
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
    # Option-overlay position flags (added for the O_CC/O_PP overlay guards, bug B2): the
    # conditions themselves are registered in TradeConditions.create_condition and documented
    # in rules_documentation.py ("require NOT has_covered_call before sell_covered_call" —
    # expressed as a stop_processing guard rule, the codebase's negation idiom). Without these
    # mappings a condition-tree leaf naming one of these fields was silently SKIPPED here.
    "has_option_position": ExpertEventType.F_HAS_OPTION_POSITION,
    "has_covered_call": ExpertEventType.F_HAS_COVERED_CALL,
    "has_protective_put": ExpertEventType.F_HAS_PROTECTIVE_PUT,
    # The wheel's entry guard: stock the expert was ASSIGNED, as opposed to any stock it
    # holds (has_buy_position), which would let the overlay cover ordinary long equity.
    "has_assigned_shares": ExpertEventType.F_HAS_ASSIGNED_SHARES,
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
    # --- registered-but-unmapped flag conditions --------------------------------------
    # Same silent-drop hole as the numeric block above. The 5-grade rating buckets and the
    # ordinal upgrade/downgrade events have condition classes; the six 3-bucket rating
    # TRANSITION events are served by RatingChangeCondition (TradeConditions.
    # RATING_CHANGE_CONDITIONS). All were droppable leaves until now.
    "current_rating_overweight": ExpertEventType.F_CURRENT_RATING_OVERWEIGHT,
    "current_rating_neutral": ExpertEventType.F_CURRENT_RATING_NEUTRAL,
    "current_rating_underweight": ExpertEventType.F_CURRENT_RATING_UNDERWEIGHT,
    "rating_upgraded": ExpertEventType.F_RATING_UPGRADED,
    "rating_downgraded": ExpertEventType.F_RATING_DOWNGRADED,
    "rating_negative_to_neutral": ExpertEventType.F_RATING_NEGATIVE_TO_NEUTRAL,
    "rating_negative_to_positive": ExpertEventType.F_RATING_NEGATIVE_TO_POSITIVE,
    "rating_neutral_to_negative": ExpertEventType.F_RATING_NEUTRAL_TO_NEGATIVE,
    "rating_neutral_to_positive": ExpertEventType.F_RATING_NEUTRAL_TO_POSITIVE,
    "rating_positive_to_negative": ExpertEventType.F_RATING_POSITIVE_TO_NEGATIVE,
    "rating_positive_to_neutral": ExpertEventType.F_RATING_POSITIVE_TO_NEUTRAL,
    # Account-wide position flags (any expert holds it), as opposed to the expert-scoped
    # has_position / has_no_position above.
    "has_position_account": ExpertEventType.F_HAS_POSITION_ACCOUNT,
    "has_no_position_account": ExpertEventType.F_HAS_NO_POSITION_ACCOUNT,
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
    are skipped (a partial/edited tree must never break the rule) but NOT silently: the skip
    is logged at WARNING with the field name.

    Muteness here is what let three separate batches of registered conditions go unmapped —
    the leaf vanishes, the rule seeds looking healthy, the strategy runs ungated and the GA
    keeps scoring genes the engine cannot see. Dropping a gate is never routine, so it gets a
    line in the log; the structural guarantee is
    packages/common/tests/test_condition_registry_coverage.py, which fails if any condition
    in ``TradeConditions.CONDITION_MAP`` lacks a mapping here.
    """
    triggers: Dict[str, dict] = {}
    for i, leaf in enumerate(tree_leaves(tree)):
        field = str(leaf.get("field"))
        flag_et = FLAG_FIELD_EVENT.get(field)
        if flag_et is not None:
            triggers[f"cond_{i}"] = {"event_type": flag_et.value}
            continue
        num_et = FIELD_EVENT.get(field)
        if num_et is None:
            logger.warning(
                f"Condition leaf {leaf.get('id') or i!r} names field {field!r}, which has no "
                f"rule_builders FIELD_EVENT/FLAG_FIELD_EVENT mapping — the gate is DROPPED "
                f"and the rule will run without it")
            continue
        if leaf.get("value") is None:
            logger.warning(
                f"Numeric condition leaf {leaf.get('id') or i!r} ({field!r}) has no value — "
                f"the gate is DROPPED and the rule will run without it")
            continue
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
    ("min_volume", ("option_min_volume", "option_min_vol")),
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


# NOTE: the REVERSE direction (live ruleset / export file -> backtester condition tree) lives in
# ``rules_convert.py``, which imports the canonical maps + forward converters FROM this module.
# This module deliberately does NOT import anything back from ``rules_convert`` (that used to be
# a bottom-of-file re-export "for a single import point", but it made the pair import-order
# dependent: importing ``rules_convert`` cold, before anything had imported ``rule_builders``,
# raised ``ImportError: cannot import name 'ACTION_VALUES' from partially initialized module``.
# ``rules_convert`` is a strict superset of this module's names (it re-exports everything it
# imports from here plus its own reverse-direction names), so callers who want the reverse
# direction should import from ``rules_convert`` directly — see its module docstring.
