"""Live TradeManager neutral entry uses real evaluator/actions and a mock broker only."""
from datetime import timedelta
import pytest
from ba2_common.core.types import AnalysisUseCase, OrderRecommendation as Signal, AssetClass
from tests import factories
from tests.conftest import MockAccount
from tests.test_funded_entry_loop import _EnterExpert, _run_enter, _orders, FROZEN_NOW


def scenario(mode, signal, confidence=20, *, action="open_straddle"):
    acct = factories.create_account_definition(provider="MockAccount")
    rs = factories.create_ruleset(name="neutral", subtype=AnalysisUseCase.ENTER_MARKET)
    trigger = ({"event_type": "current_rating_neutral"} if mode != "low_confidence" else
               {"event_type": "confidence", "operator": "<=", "value": 30.})
    ea = factories.create_event_action(name="neutral", subtype=AnalysisUseCase.ENTER_MARKET,
        triggers={"trigger_0": trigger}, actions={"action_0": {
            "action_type": action, "strike_method": "percent_otm", "strike_param": 0,
            "dte_min": 20, "dte_max": 45, "sizing": 10.0,
            "min_open_interest": 100, "max_spread_pct": 30.}})
    factories.link_rule_to_ruleset(rs.id, ea.id, 0)
    inst = factories.create_expert_instance(account_id=acct.id, expert="_EnterExpert",
        virtual_equity_pct=100., enter_market_ruleset_id=rs.id)
    expert = _EnterExpert(inst.id)
    expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                         "neutral_option_entry_mode": (mode, "str")})
    factories.create_recommendation(instance_id=inst.id, symbol="AAPL",
        recommended_action=signal, confidence=confidence, price_at_date=150.,
        expected_profit_percent=0., created_at=FROZEN_NOW)
    account = MockAccount(acct.id)
    return inst, expert, account


@pytest.mark.parametrize("mode,signal,confidence,opens", [
    ("hold", Signal.HOLD, 10, True), ("hold", Signal.BUY, 20, False),
    ("hold", Signal.SELL, 20, False), ("low_confidence", Signal.BUY, 20, True),
    ("low_confidence", Signal.SELL, 20, True), ("low_confidence", Signal.BUY, 60, False),
    ("low_confidence", Signal.HOLD, 10, False), ("legacy", Signal.HOLD, 10, False),
])
def test_live_neutral_entry(mode, signal, confidence, opens):
    inst, expert, account = scenario(mode, signal, confidence)
    result = _run_enter(expert, account, inst.id)
    assert bool(result) == opens
    assert len([o for o in _orders() if o.asset_class == AssetClass.OPTION]) == (3 if opens else 0)
    if opens:
        _run_enter(expert, account, inst.id)
        assert len(_orders()) == 3, "pending/open structure must block another entry"


def test_newer_buy_does_not_resurrect_an_older_hold():
    inst, expert, account = scenario("hold", Signal.HOLD)
    factories.create_recommendation(instance_id=inst.id, symbol="AAPL",
        recommended_action=Signal.BUY, confidence=90., price_at_date=150.,
        created_at=FROZEN_NOW + timedelta(seconds=1))
    assert not _run_enter(expert, account, inst.id)
    assert not _orders()


def test_hold_cannot_execute_an_equity_action():
    inst, expert, account = scenario("hold", Signal.HOLD, action="buy")
    assert not _run_enter(expert, account, inst.id)
    assert not _orders()


def test_automated_opening_gate_still_blocks_hold():
    inst, expert, account = scenario("hold", Signal.HOLD)
    expert.save_settings({"allow_automated_trade_opening": (False, "bool")})
    assert not _run_enter(expert, account, inst.id)
    assert not _orders()


@pytest.mark.parametrize("settings", [{}, {"neutral_option_entry_mode": None}])
def test_unsaved_optional_mode_retains_legacy(settings):
    from ba2_common.core.neutral_option_entry import entry_mode
    assert entry_mode(settings) == "legacy"


def test_unknown_mode_fails_closed():
    from ba2_common.core.neutral_option_entry import entry_mode
    with pytest.raises(ValueError):
        entry_mode({"neutral_option_entry_mode": "typo"})
