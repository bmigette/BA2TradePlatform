"""Entry-bracket seeding: seed_ruleset_from_tree emits adjust_take_profit /
adjust_stop_loss actions on the enter rule when entry TP/SL percents are given
(mirroring the live BUY_Longterm_70pctConfidence_10pctProfit pattern: one
EventAction with action_0=buy + adjust actions), and emits NOTHING extra when
they are None (backward compat: every historical config stays byte-identical)."""
import json

from app.services.backtest.default_rulesets import seed_ruleset_from_tree
from ba2_common.core.models import EventAction, Ruleset
from ba2_common.core.types import ExpertActionType, ReferenceValue


def _actions_of_first_rule(ruleset_id: int) -> dict:
    from sqlmodel import select
    from ba2_common.core.db import get_db
    from ba2_common.core.models import RulesetEventActionLink
    with get_db() as session:
        link = session.exec(select(RulesetEventActionLink).where(
            RulesetEventActionLink.ruleset_id == ruleset_id)).first()
        ea = session.get(EventAction, link.eventaction_id)
        actions = ea.actions
        return json.loads(actions) if isinstance(actions, str) else actions


def test_no_bracket_by_default():
    rid = seed_ruleset_from_tree(None, name="t-nobracket", entry_action={"action_type": "buy"})
    actions = _actions_of_first_rule(rid)
    types = {cfg["action_type"] for cfg in actions.values()}
    assert ExpertActionType.ADJUST_TAKE_PROFIT.value not in types
    assert ExpertActionType.ADJUST_STOP_LOSS.value not in types


def test_bracket_emits_tp_and_sl_actions():
    rid = seed_ruleset_from_tree(
        None, name="t-bracket", entry_action={"action_type": "buy"},
        entry_tp_percent=8.0, entry_sl_percent=3.0,
    )
    actions = _actions_of_first_rule(rid)
    by_type = {cfg["action_type"]: cfg for cfg in actions.values()}
    tp = by_type[ExpertActionType.ADJUST_TAKE_PROFIT.value]
    sl = by_type[ExpertActionType.ADJUST_STOP_LOSS.value]
    # Sign convention per TradeActions.compute_price: long price = ref*(1+value/100),
    # so TP is +8.0 (above entry) and SL is -3.0 (below entry); shorts invert automatically.
    assert tp["value"] == 8.0
    assert sl["value"] == -3.0
    assert tp["reference_value"] == ReferenceValue.ORDER_OPEN_PRICE.value
    assert sl["reference_value"] == ReferenceValue.ORDER_OPEN_PRICE.value
    # The buy action must still be present (Phase 1 creates the order Phase 2 adjusts).
    assert "buy" in {cfg["action_type"] for cfg in actions.values()}


def test_bracket_tp_reference_expert_target_passes_value_signed():
    # S4 semantics: TP anchored on the analyst target; the value is a SIGNED
    # offset-from-target (live example: -5.0 = 5% below target), passed through as-is.
    rid = seed_ruleset_from_tree(
        None, name="t-bracket-ref", entry_action={"action_type": "buy"},
        entry_tp_percent=-5.0, entry_sl_percent=3.0,
        entry_tp_reference=ReferenceValue.EXPERT_TARGET_PRICE.value,
    )
    actions = _actions_of_first_rule(rid)
    by_type = {cfg["action_type"]: cfg for cfg in actions.values()}
    tp = by_type[ExpertActionType.ADJUST_TAKE_PROFIT.value]
    assert tp["value"] == -5.0
    assert tp["reference_value"] == ReferenceValue.EXPERT_TARGET_PRICE.value


def test_sl_only_bracket():
    rid = seed_ruleset_from_tree(
        None, name="t-slonly", entry_action={"action_type": "buy"},
        entry_sl_percent=4.0,
    )
    actions = _actions_of_first_rule(rid)
    types = {cfg["action_type"] for cfg in actions.values()}
    assert ExpertActionType.ADJUST_STOP_LOSS.value in types
    assert ExpertActionType.ADJUST_TAKE_PROFIT.value not in types
