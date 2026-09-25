"""The equity round lot (``lot_size``) must reach the risk manager -- live AND backtest.

O_CC / O_PP (the launcher's ``_with_round_lot_entry``) put ``lot_size: 100`` on their equity BUY
entry so the shares come in whole lots and the overlay (one contract per 100 shares) can be
written. From 37d207c4 (2026-07-25) until this fix it never took effect, at two links:

1. ``rules_convert._action_cfg_to_live`` rebuilt the buy action from ``action_type`` alone, so
   the seeded / exported live rule had no ``lot_size`` (authored [100] -> []);
2. ``trade_cycle.build_entry_candidate`` builds the RM candidate's ``data`` from the
   RECOMMENDATION, not from the fired ``BuyAction``, so even a live rule that kept ``lot_size``
   was sized as if it had none.

A probe through the real engine bought 500 shares with lot_size 100, with none, and with 7.
"""
from ba2_common.core.models import ExpertRecommendation
from ba2_common.core.rules_convert import (
    live_actions_from_trade_rule,
    live_export_to_trade_rules,
    trade_rules_to_live_export,
)
from ba2_common.core import trade_cycle
from ba2_common.core.TradeActions import BuyAction, SellAction
from ba2_common.core.types import OrderRecommendation, RiskLevel, TimeHorizon


def _rule(actions):
    return {"id": "buy", "name": "buy",
            "conditions": {"type": "AND", "conditions": [{"id": "b", "field": "bullish"}]},
            "actions": actions}


# --------------------------------------------------------------------------------------- #
# link 1: rules_convert
# --------------------------------------------------------------------------------------- #
def test_the_buy_action_keeps_its_lot_size_when_converted_to_a_live_rule():
    live = live_actions_from_trade_rule(_rule([{"action": "buy", "action_type": "buy",
                                                "lot_size": 100}]))
    assert live == {"a0": {"action_type": "buy", "lot_size": 100}}


def test_a_buy_action_WITHOUT_lot_size_converts_exactly_as_before():
    """Every rule that does not ask for a round lot -- all but O_CC / O_PP -- is byte-identical."""
    live = live_actions_from_trade_rule(_rule([{"action": "buy", "action_type": "buy"}]))
    assert live == {"a0": {"action_type": "buy"}}
    sell = live_actions_from_trade_rule(_rule([{"action_type": "sell"}]))
    assert sell == {"a0": {"action_type": "sell"}}


def test_lot_size_survives_the_export_import_round_trip():
    export = trade_rules_to_live_export(
        entry_rules=[_rule([{"action_type": "buy", "lot_size": 100}])], exit_rules=[])
    enter = [rs for rs in export["rulesets"] if rs["subtype"] == "enter_market"][0]
    assert enter["rules"][0]["actions"]["a0"] == {"action_type": "buy", "lot_size": 100}

    back = live_export_to_trade_rules(export)
    assert back["entry_rules"][0]["actions"][0]["lot_size"] == 100
    again = trade_rules_to_live_export(entry_rules=back["entry_rules"], exit_rules=[])
    enter2 = [rs for rs in again["rulesets"] if rs["subtype"] == "enter_market"][0]
    assert enter2["rules"][0]["actions"]["a0"] == {"action_type": "buy", "lot_size": 100}


# --------------------------------------------------------------------------------------- #
# link 2: trade_cycle
# --------------------------------------------------------------------------------------- #
def _rec(action=OrderRecommendation.BUY, data=None):
    return ExpertRecommendation(
        id=42, instance_id=1, symbol="AAPL", recommended_action=action,
        expected_profit_percent=10.0, price_at_date=100.0, confidence=80.0,
        risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.MEDIUM_TERM, data=data)


class _Evaluator:
    def __init__(self, actions):
        self.trade_actions = actions


class _PlainAccount:
    """Not an OptionsAccountInterface: the lot is the configured int unchanged."""
    id = 7


def _buy(lot):
    return BuyAction("AAPL", _PlainAccount(), OrderRecommendation.BUY, lot_size=lot)


def test_the_fired_buy_actions_lot_size_is_read_off_the_evaluator():
    assert trade_cycle.fired_entry_lot_size(_Evaluator([_buy(100)])) == 100


def test_no_lot_size_when_the_fired_buy_carries_none():
    assert trade_cycle.fired_entry_lot_size(_Evaluator([_buy(None)])) is None
    assert trade_cycle.fired_entry_lot_size(_Evaluator([])) is None
    assert trade_cycle.fired_entry_lot_size(_Evaluator(
        [SellAction("AAPL", _PlainAccount(), OrderRecommendation.SELL)])) is None


def test_the_lot_is_the_ACTIONS_equity_lot_not_the_raw_int():
    """On a split-adjusted backtest book one contract delivers 100 x k ADJUSTED shares; the
    action's own ``_equity_lot_size`` owns that conversion, so the helper must go through it."""
    class _Scaled(BuyAction):
        def _equity_lot_size(self):
            return 1000
    assert trade_cycle.fired_entry_lot_size(_Evaluator(
        [_Scaled("NFLX", _PlainAccount(), OrderRecommendation.BUY, lot_size=100)])) == 1000


def test_the_candidate_carries_the_lot_size_when_one_is_given():
    rec = _rec(data={"note": "x"})
    c = trade_cycle.build_entry_candidate(rec, account_id=7, lot_size=100)
    assert c.data == {"note": "x", "lot_size": 100}
    assert rec.data == {"note": "x"}, "the recommendation's own data must not be mutated"

    c2 = trade_cycle.build_entry_candidate(_rec(), account_id=7, lot_size=100)
    assert c2.data == {"lot_size": 100}


def test_no_lot_size_leaves_the_candidate_byte_identical():
    for data in (None, {}, {"note": "x"}):
        rec = _rec(data=data)
        before = trade_cycle.build_entry_candidate(rec, account_id=7)
        after = trade_cycle.build_entry_candidate(rec, account_id=7, lot_size=None)
        # created_at is a wall-clock default_factory stamp: the only field two builds of the
        # same candidate may legitimately differ in.
        assert (before.model_dump(exclude={"created_at"})
                == after.model_dump(exclude={"created_at"}))
        assert after.data is (data or None)


# --------------------------------------------------------------------------------------- #
# the risk-based sizer: whole lots, and a sub-lot refusal that says so
# --------------------------------------------------------------------------------------- #
def test_the_risk_sizer_floors_to_whole_lots_and_names_a_sub_lot_refusal():
    from ba2_common.core.position_sizing import compute_risk_based_quantity

    kw = dict(equity=100_000.0, current_price=10.0, risk_per_trade_pct=1.0, stop_price=9.0,
              available_balance=100_000.0)
    # $1,000 risk / $1 per share = 1,000 shares; the $5,370 ceiling cuts it to 537.
    assert compute_risk_based_quantity(max_position_value=5_370.0, **kw)["quantity"] == 537
    assert compute_risk_based_quantity(max_position_value=5_370.0, lot_size=100,
                                       **kw)["quantity"] == 500
    refused = compute_risk_based_quantity(max_position_value=660.0, lot_size=100, **kw)
    assert refused["quantity"] == 0
    assert "less than one lot of 100" in refused["reason"]
