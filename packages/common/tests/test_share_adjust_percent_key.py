"""increase/decrease-instrument-share read the percent the rule editor saves.

The settings rule editor saves the target percent under ``target_percent``; the unified/tree
format and the converters use ``value``. Before 2026-09-25 the evaluator read only ``value``,
so every rule built in the editor reached execution with ``target_percent=None`` and failed.
Either key works now; both set and disagreeing is refused rather than guessed.
"""
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator, share_adjust_target_percent
from ba2_common.core.TradeActions import DecreaseInstrumentShareAction, IncreaseInstrumentShareAction
from ba2_common.core.types import ExpertActionType, OrderRecommendation

SHARE_ACTIONS = [
    (ExpertActionType.INCREASE_INSTRUMENT_SHARE, IncreaseInstrumentShareAction),
    (ExpertActionType.DECREASE_INSTRUMENT_SHARE, DecreaseInstrumentShareAction),
]


def _build(action_type, config):
    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    ev.account = SimpleNamespace(id=1)
    rec = SimpleNamespace(id=1, instance_id=None, recommended_action=OrderRecommendation.BUY)
    return ev._create_trade_action(action_type, {"action_type": action_type.value, **config},
                                   "AAPL", OrderRecommendation.BUY, None, rec)


@pytest.mark.parametrize("action_type,cls", SHARE_ACTIONS)
def test_the_editor_key_target_percent_reaches_the_action(action_type, cls):
    action = _build(action_type, {"target_percent": 5.0})
    assert isinstance(action, cls) and action.target_percent == 5.0


@pytest.mark.parametrize("action_type,cls", SHARE_ACTIONS)
def test_value_still_works(action_type, cls):
    action = _build(action_type, {"value": 7.5})
    assert isinstance(action, cls) and action.target_percent == 7.5


@pytest.mark.parametrize("action_type,cls", SHARE_ACTIONS)
def test_both_keys_equal_is_accepted(action_type, cls):
    assert _build(action_type, {"value": 10, "target_percent": 10.0}).target_percent == 10


@pytest.mark.parametrize("action_type,cls", SHARE_ACTIONS)
def test_both_keys_disagreeing_is_refused(action_type, cls):
    assert _build(action_type, {"value": 10.0, "target_percent": 5.0}).target_percent is None


@pytest.mark.parametrize("action_type,cls", SHARE_ACTIONS)
def test_neither_key_warns_and_leaves_it_none(action_type, cls, monkeypatch):
    import ba2_common.core.TradeActionEvaluator as mod
    warnings = []
    monkeypatch.setattr(mod.logger, "warning", lambda msg, *a, **k: warnings.append(msg))
    assert _build(action_type, {}).target_percent is None
    assert warnings and "target_percent" in warnings[0] and "value" in warnings[0]


def test_a_none_value_falls_back_to_target_percent():
    """A converter can write ``value: None`` next to the editor's key; None is not a value."""
    assert share_adjust_target_percent(ExpertActionType.DECREASE_INSTRUMENT_SHARE,
                                       {"value": None, "target_percent": 4.0}) == 4.0


def test_a_non_numeric_pair_is_refused_not_raised():
    assert share_adjust_target_percent(ExpertActionType.INCREASE_INSTRUMENT_SHARE,
                                       {"value": "abc", "target_percent": 5.0}) is None


def test_the_tree_converter_still_does_not_carry_share_actions():
    """Known and deliberate (see the market-exit work): the tree-form converter has no share
    action, so it drops them and the tree-form market rules refuse "reduce". Editor-built rules
    never pass through it -- they live as EventAction JSON the evaluator reads directly, which
    is the path this fix repairs. Pinned so a future converter change revisits the key."""
    from ba2_common.core.rules_convert import _action_cfg_to_live
    assert _action_cfg_to_live(
        {"action_type": "decrease_instrument_share", "target_percent": 5.0}, "a0") is None
