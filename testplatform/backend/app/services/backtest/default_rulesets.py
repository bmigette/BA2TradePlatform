"""Seed the minimal enter_market ruleset(s) the daily engine drives.

The live platform configures rulesets through the UI; the backtest host seeds a small,
faithful default directly into the per-run backtest DB so ``TradeActionEvaluator`` has a
ruleset to evaluate (``ExpertInstance.enter_market_ruleset_id`` points at it).

The default enter ruleset is the standard "enter on a bullish recommendation when flat"
rule: a single ``EventAction`` whose triggers are the packaged ``BullishCondition``
(``recommended_action == BUY``) AND ``HasNoPositionCondition`` (no existing expert position
for the symbol — prevents duplicate entries), and whose single action is a ``buy``. This is
exactly the ruleset shape the live ``TradeActionEvaluator`` evaluates; the engine does NOT
invent a new evaluation path.

``enter_long_short_ruleset`` additionally adds the symmetric SELL-on-bearish rule for experts
that short (gated by the expert's ``enable_sell`` setting in the RM, so it is safe to include).

Trigger/action JSON shapes verified against ba2_common:
  * trigger: ``{"<key>": {"event_type": "<ExpertEventType value>"}}`` — empty/unknown
    operators are fine for flag conditions; ``BullishCondition``/``HasNoPositionCondition``
    take no value. ``ExpertEventType.F_BULLISH = "bullish"``, ``F_HAS_NO_POSITION =
    "has_no_position"``, ``F_BEARISH = "bearish"``.
  * action: ``{"<key>": {"action_type": "<ExpertActionType value>"}}`` — ``ExpertActionType.BUY
    = "buy"``, ``SELL = "sell"``. The action is parsed by ``_create_and_store_trade_actions``.
"""
from __future__ import annotations

from typing import List

from sqlmodel import Session

from ba2_common.core.db import add_instance, get_db
from ba2_common.core.models import EventAction, Ruleset, RulesetEventActionLink
from ba2_common.core.rule_builders import (
    FIELD_EVENT,
    FLAG_FIELD_EVENT,
    EXIT_ACTION,
    tree_leaves,
    triggers_from_condition_tree,
    action_from_rule,
)
from ba2_common.core.types import (
    AnalysisUseCase,
    ExpertActionType,
    ExpertEventRuleType,
    ExpertEventType,
)

# Backward-compat aliases for modules that import the (previously local) maps/helpers by name.
# These are now the SHARED ba2_common.core.rule_builders definitions — the single source of
# truth reconciled with the API/UI shape. ``rules_tree_json`` imports ``_FIELD_EVENT`` from here.
_FIELD_EVENT = FIELD_EVENT
_FLAG_FIELD_EVENT = FLAG_FIELD_EVENT
_EXIT_ACTION = EXIT_ACTION
_tree_leaves = tree_leaves
_triggers_from_conditions = triggers_from_condition_tree


def _make_event_action(name: str, triggers: dict, actions: dict,
                       subtype: "AnalysisUseCase" = AnalysisUseCase.ENTER_MARKET) -> int:
    """Create one ``EventAction`` (default enter_market subtype) and return its id."""
    ea = EventAction(
        name=name,
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=subtype,
        triggers=triggers,
        actions=actions,
        extra_parameters={},
        continue_processing=False,
    )
    return add_instance(ea)


def _link(ruleset_id: int, event_action_ids: List[int]) -> None:
    """Attach the event actions to the ruleset (ordered) via the M2M link table.

    ``RulesetEventActionLink`` has a composite PK (ruleset_id + eventaction_id) and NO ``id``
    column, so ``add_instance`` (which reads ``.id`` after flush) cannot be used — insert via
    a session directly, mirroring the rules_export_import session-based link inserts.
    """
    with Session(get_db().bind) as session:
        for order_index, ea_id in enumerate(event_action_ids):
            session.add(
                RulesetEventActionLink(
                    ruleset_id=ruleset_id,
                    eventaction_id=ea_id,
                    order_index=order_index,
                )
            )
        session.commit()


def seed_open_positions_ruleset(exit_rules, name: str = "backtest-open-positions") -> int:
    """Seed an OPEN_POSITIONS ruleset from a Strategy exit-rule LIST; return its id.

    Each entry in ``exit_rules`` (the shape ``decode_params`` emits: ``{id, conditions,
    action_type, reference_value, action_value, enabled}``; enabled-off rules are already
    pruned) becomes ONE ordered ``EventAction`` whose triggers are the ANDed condition leaves
    and whose single action is Close/Sell/Adjust-TP/Adjust-SL. This is evaluated by the SAME
    packaged ``TradeActionEvaluator`` the live ``process_open_positions_recommendations`` uses
    (open_positions use case), so the backtest manages open positions identically to live.

    A rule with no usable action is skipped. Returns the ruleset id (with NO event actions if
    every rule was skipped — the caller can treat that as "no exit management").
    """
    ruleset = Ruleset(
        name=name,
        description="Backtest open_positions ruleset built from a Strategy exit-rule list.",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase.OPEN_POSITIONS,
    )
    ruleset_id = add_instance(ruleset)

    ea_ids = []
    for idx, rule in enumerate(exit_rules or []):
        # Build via the SHARED rule-builder core: ``action_from_rule`` accepts both the seeding
        # shape (``action_type``/``action_value``) AND the API/UI shape (``action``/``value``);
        # ``triggers_from_condition_tree`` reconciles a leaf's ``comparison`` with ``op``/
        # ``operator``. This is the fix for API exit rules being silently skipped.
        action = action_from_rule(rule)
        if action is None:
            continue
        triggers = triggers_from_condition_tree(rule.get("conditions"))
        ea_ids.append(
            _make_event_action(
                name=f"{name}-rule-{idx}",
                triggers=triggers,
                actions=action,
                subtype=AnalysisUseCase.OPEN_POSITIONS,
            )
        )
    if ea_ids:
        _link(ruleset_id, ea_ids)
    return ruleset_id


def _gate_triggers(tree) -> dict:
    """The optimizer's numeric/flag entry gates from a buy/sell condition tree (ANDed).

    Thin wrapper over the SHARED ``triggers_from_condition_tree``: identical leaf -> trigger
    logic, but the entry path uses a ``gate_`` key prefix (vs the exit path's ``cond_``). The
    prefix is cosmetic to ``TradeActionEvaluator`` (it iterates ``triggers.items()`` and uses the
    key only for logging — all triggers are ANDed regardless of key, provided keys are unique),
    so we reuse the shared builder and just rename the keys to preserve the entry-rule contract.
    """
    shared = triggers_from_condition_tree(tree)
    return {key.replace("cond_", "gate_", 1): cfg for key, cfg in shared.items()}


def _gate_trigger_groups(tree) -> list:
    """One ANDed gate-trigger set per top-level OR group, preserving OR semantics (ANY group
    enters). The flattening ``triggers_from_condition_tree`` ANDs all leaves, which is correct
    WITHIN a group but wrong across an OR of groups (e.g. an imported live ruleset with several
    alternative entry conditions — long_term-group OR short_term-group). So when the tree is a
    top-level OR of groups we emit a separate gate set per group (the caller makes one BUY rule
    each); otherwise a single gate set. A leaf-only or AND tree yields one set unchanged."""
    if isinstance(tree, dict):
        op = str(tree.get("type") or tree.get("operator") or "AND").upper()
        children = tree.get("conditions") or []
        if op == "OR" and children and all(
            isinstance(c, dict) and c.get("conditions") is not None for c in children
        ):
            return [_gate_triggers(c) for c in children]
    return [_gate_triggers(tree)]


def _entry_actions(side: str, entry_action: "dict | None" = None) -> dict:
    """The open action for an entry rule.

    Equity BUY (long) / SELL (short) by default. When ``entry_action`` (an OPTION action
    config dict in the rule_builders shape — ``action_type`` + ``option_strike_*`` /
    ``option_dte_*`` / ``option_sizing`` / ``option_wing_width*`` keys) is given, emit THAT as
    the entry action instead, so the enter_market ruleset fires an OPTION action DIRECTLY (a
    pure-option entry — no equity leg). The option action sizes + submits itself (the engine
    runs the option entry with ``submit_to_broker=True``); the equity BUY/SELL stays a PENDING
    qty=0 order the RM sizes later.

    NOTE (equity path): the entry TP/SL bracket is NOT emitted here as Adjust actions. At
    enter_market time the BUY/SELL only stages a PENDING order (the RM sizes + submits it
    later), so there is no transaction yet for an Adjust action to attach an OCO leg to —
    emitting Adjust here sets the transaction's tp/sl field with no working leg AND suppresses
    the fallback, so nothing closes. The engine applies the (reference-aware, optimizable)
    initial bracket at transaction-OPEN instead (``_apply_initial_brackets``).
    """
    if entry_action:
        built = action_from_rule(entry_action, key=side)
        if built:
            return built
    open_act = ExpertActionType.BUY.value if side == "buy" else ExpertActionType.SELL.value
    return {side: {"action_type": open_act}}


def seed_ruleset_from_tree(buy_tree, name: str = "backtest-enter-tree",
                           enable_short: bool = False,
                           entry_action: "dict | None" = None) -> int:
    """Seed an enter_market ruleset from a Strategy buy-entry condition TREE; return its id.

    The base "BUY when bullish and flat" triggers are kept, AND each leaf condition in the tree
    is added as an extra trigger (ANDed) — exactly what the optimizer's cond:<id>:value / on-off
    genes tune. When ``enable_short`` a symmetric SELL rule (bearish + flat + the SAME gates) is
    added so the strategy can short (gated by the RM's enable_sell). The initial TP/SL bracket is
    applied at transaction-open by the engine (``_apply_initial_brackets``), NOT as an entry Adjust
    action (see ``_entry_actions``) — so the entry seeder carries no bracket plumbing. Unknown
    fields are skipped; falls back to bullish+flat when the tree adds nothing.

    When ``entry_action`` (an OPTION action config) is given, the open action is the OPTION
    action (a pure-option entry — no equity leg; see ``_entry_actions``). If ``buy_tree`` is
    None in that case, a single permissive gate group is used so the option fires on the base
    bullish+flat triggers alone (the entry condition is just "expert is bullish & flat").
    """
    ruleset = Ruleset(
        name=name,
        description="Backtest enter ruleset built from a Strategy condition tree.",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase.ENTER_MARKET,
    )
    ruleset_id = add_instance(ruleset)

    # One BUY rule per top-level OR group (faithful OR: ANY group enters); a single rule for an
    # AND / leaf tree. has_no_position keeps it to one entry even if several groups match.
    # When there's no tree but an option entry_action is set, use a single permissive gate
    # (the option fires on bullish+flat alone — the base triggers ARE the entry condition).
    if buy_tree is None and entry_action:
        groups = [{}]
    else:
        groups = _gate_trigger_groups(buy_tree)
    multi = len(groups) > 1
    eas = []
    for gi, gate in enumerate(groups):
        suffix = f"-{gi}" if multi else ""
        buy_triggers = {
            "bullish": {"event_type": ExpertEventType.F_BULLISH.value},
            "no_position": {"event_type": ExpertEventType.F_HAS_NO_POSITION.value},
            **gate,
        }
        eas.append(_make_event_action(
            name=f"{name}-enter-long{suffix}",
            triggers=buy_triggers,
            actions=_entry_actions("buy", entry_action),
        ))
        if enable_short:
            sell_triggers = {
                "bearish": {"event_type": ExpertEventType.F_BEARISH.value},
                "no_position": {"event_type": ExpertEventType.F_HAS_NO_POSITION.value},
                **gate,
            }
            eas.append(_make_event_action(
                name=f"{name}-enter-short{suffix}",
                triggers=sell_triggers,
                actions=_entry_actions("sell", entry_action),
            ))
    _link(ruleset_id, eas)
    return ruleset_id


def seed_enter_long_ruleset(name: str = "backtest-enter-long") -> int:
    """Seed a "BUY when bullish and flat" enter_market ruleset; return its id.

    One rule: triggers = bullish AND has_no_position; action = buy.
    """
    ruleset = Ruleset(
        name=name,
        description="Backtest default: enter long when the expert recommends BUY and the "
        "expert has no open position for the symbol.",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase.ENTER_MARKET,
    )
    ruleset_id = add_instance(ruleset)

    enter_long = _make_event_action(
        name=f"{name}-enter-long",
        triggers={
            "bullish": {"event_type": ExpertEventType.F_BULLISH.value},
            "no_position": {"event_type": ExpertEventType.F_HAS_NO_POSITION.value},
        },
        actions={"buy": {"action_type": ExpertActionType.BUY.value}},
    )
    _link(ruleset_id, [enter_long])
    return ruleset_id


def seed_enter_long_short_ruleset(name: str = "backtest-enter-long-short") -> int:
    """Seed a long+short enter_market ruleset; return its id.

    Two rules (evaluated in order, ``continue_processing=False`` so the first matching rule
    stops evaluation): bullish+flat -> buy; bearish+flat -> sell. The SELL leg is still
    gated by the expert's ``enable_sell`` permission inside the risk manager, so seeding it
    is safe for buy-only experts (their SELL orders are dropped by the RM permission filter).
    """
    ruleset = Ruleset(
        name=name,
        description="Backtest default: enter long on BUY / short on SELL when flat.",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        subtype=AnalysisUseCase.ENTER_MARKET,
    )
    ruleset_id = add_instance(ruleset)

    enter_long = _make_event_action(
        name=f"{name}-enter-long",
        triggers={
            "bullish": {"event_type": ExpertEventType.F_BULLISH.value},
            "no_position": {"event_type": ExpertEventType.F_HAS_NO_POSITION.value},
        },
        actions={"buy": {"action_type": ExpertActionType.BUY.value}},
    )
    enter_short = _make_event_action(
        name=f"{name}-enter-short",
        triggers={
            "bearish": {"event_type": ExpertEventType.F_BEARISH.value},
            "no_position": {"event_type": ExpertEventType.F_HAS_NO_POSITION.value},
        },
        actions={"sell": {"action_type": ExpertActionType.SELL.value}},
    )
    _link(ruleset_id, [enter_long, enter_short])
    return ruleset_id
