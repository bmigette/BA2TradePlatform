"""Per-entry routing (plan 2026-09-25 option-stage2-build, revision Task E1) and the EQUITY
half of the style-group ``entry_tag``.

ONE decision, shared by both runtimes: ``trade_cycle.entry_route`` looks at the actions the
FIRED entry rule produced and answers

* ``option``  -- every action is an option action: the actions size + submit themselves;
* ``equity``  -- no action is an option action: the entry is staged for the RM sizing pass;
* a MIX is refused (``MixedEntryActionsRefused``): neither path can size both halves -- the
  equity RM would stamp a share count + a stock stop onto the option order, and the option
  path would submit the equity action unsized.

Live ``TradeManager`` (the 2026-09-24 f73a33f3 fix) and the backtest ``daily_engine`` both call
it, so the routing cannot drift between them. Before E1 the backtest decided ONCE PER RUN from
``config["entry_action"]``, which made a style expert mixing an equity-entry member (O_CC,
O_PP) with option-entry members impossible.

The equity half of the tag: O_CC / O_PP enter with an EQUITY buy, and their exits are scoped
by the same ``entry_tag`` leaf, so ``BuyAction`` writes the tag onto the entry order row it
creates (absent tag = the byte-identical order it always created).
"""
from types import SimpleNamespace

import pytest

from ba2_common.core.entry_tag import ORDER_ENTRY_TAG_KEY
from ba2_common.core.trade_cycle import (
    ENTRY_ROUTE_EQUITY,
    ENTRY_ROUTE_OPTION,
    MixedEntryActionsRefused,
    entry_route,
)
from ba2_common.core.types import ExpertActionType as A


# --------------------------------------------------------------------------- #
# the routing decision
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("types", [
    [A.BUY_CALL],
    [A.SELL_CASH_SECURED_PUT],
    [A.OPEN_IRON_CONDOR, A.BUY_PUT],
    ["buy_call"],                                  # a raw value routes like its enum
])
def test_all_option_actions_self_size(types):
    assert entry_route(types) == ENTRY_ROUTE_OPTION


@pytest.mark.parametrize("types", [
    [A.BUY],
    [A.BUY, A.ADJUST_TAKE_PROFIT, A.ADJUST_STOP_LOSS],   # the S-family entry bracket
    ["buy"],
])
def test_no_option_action_goes_to_the_rm(types):
    assert entry_route(types) == ENTRY_ROUTE_EQUITY


@pytest.mark.parametrize("types", [
    [A.BUY_CALL, A.BUY],
    [A.BUY, A.SELL_COVERED_CALL],
    [A.BUY_CALL, A.ADJUST_TAKE_PROFIT],
])
def test_a_mix_is_refused_by_name(types):
    with pytest.raises(MixedEntryActionsRefused, match="mixes option and equity") as err:
        entry_route(types)
    assert isinstance(err.value, ValueError)


def test_no_actions_is_refused_not_routed():
    """An empty action list reaching the router is a caller bug (both callers stop earlier on
    'no actions'): answering it would pick a path for nothing."""
    with pytest.raises(ValueError, match="no actions"):
        entry_route([])


def test_the_decision_is_O_of_the_actions_not_of_the_run():
    """Nothing is read but the action types handed in (no DB, no config): the engine calls
    this once per fired recommendation, so it must stay a pure function of its argument."""
    import inspect
    from ba2_common.core import trade_cycle
    src = inspect.getsource(trade_cycle.entry_route)
    for forbidden in ("get_db", "get_instance", "config", "account"):
        assert forbidden not in src.split('"""')[-1], forbidden


# --------------------------------------------------------------------------- #
# the equity entry tag
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "entry_route.sqlite"))
    db.init_db()
    yield


class _Acct:
    id = 1

    def get_instrument_current_price(self, symbol, price_type=None):
        return 20.0


def _buy(**kw):
    """A real ``BuyAction``; only its RESULT row is stubbed (that row needs a persisted
    recommendation, and what is under test is the ORDER row the action creates)."""
    from ba2_common.core.TradeActions import BuyAction
    from ba2_common.core.types import OrderRecommendation
    a = BuyAction("CCX", _Acct(), OrderRecommendation.BUY, expert_recommendation=None, **kw)
    a.create_and_save_action_result = (
        lambda action_type, success, message, data=None:
        {"success": success, "message": message, "data": data})
    return a


def _stored(result):
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import TradingOrder
    assert result["success"], result
    return get_instance(TradingOrder, result["data"]["order_id"])


def test_a_tagged_buy_writes_the_tag_on_its_entry_order():
    order = _stored(_buy(entry_tag=19).execute())
    assert order.data[ORDER_ENTRY_TAG_KEY] == 19


def test_the_tag_rides_beside_the_round_lot():
    order = _stored(_buy(entry_tag=19, lot_size=100).execute())
    assert order.data[ORDER_ENTRY_TAG_KEY] == 19 and order.data["lot_size"] == 100


def test_an_untagged_buy_writes_the_order_it_always_wrote():
    order = _stored(_buy().execute())
    assert ORDER_ENTRY_TAG_KEY not in (order.data or {})
    lot = _stored(_buy(lot_size=100).execute())
    assert lot.data == {"lot_size": 100}


@pytest.mark.parametrize("bad", [0, -1, 2.5, "3", True])
def test_a_bad_equity_tag_is_refused_at_construction(bad):
    with pytest.raises(ValueError, match="entry_tag"):
        _buy(entry_tag=bad)


def test_the_equity_order_tag_is_what_the_exit_condition_reads():
    from ba2_common.core.TradeConditions import EntryTagCondition
    order = _stored(_buy(entry_tag=19).execute())
    cond = EntryTagCondition(account=None, instrument_name="CCX", expert_recommendation=None,
                             operator_str="==", value=19, existing_order=order)
    assert cond.evaluate() is True


def test_the_evaluator_forwards_the_tag_to_the_buy_action():
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
    from ba2_common.core.types import OrderRecommendation
    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    ev.account = _Acct()
    rec = SimpleNamespace(id=1, instance_id=None)
    tagged = ev._create_trade_action(A.BUY, {"action_type": "buy", "entry_tag": 19}, "CCX",
                                     OrderRecommendation.BUY, None, rec)
    assert tagged.entry_tag == 19
    plain = ev._create_trade_action(A.BUY, {"action_type": "buy"}, "CCX",
                                    OrderRecommendation.BUY, None, rec)
    assert plain.entry_tag is None


@pytest.mark.parametrize("key", ["entry_tag", "option_entry_tag"])
def test_a_trade_rule_buy_carries_its_tag_to_the_event_action(key):
    """The TradeRule -> EventAction conversion (backtest seeder AND deploy export) used to
    emit a bare ``{"action_type": "buy"}`` for an equity open, dropping every extra key."""
    from ba2_common.core.rules_convert import live_actions_from_trade_rule
    out = live_actions_from_trade_rule({"id": "o_cc-entry", "actions": [
        {"action_type": "buy", key: 19}]})
    assert out == {"a0": {"action_type": "buy", "entry_tag": 19}}


def test_an_untagged_trade_rule_buy_converts_exactly_as_before():
    from ba2_common.core.rules_convert import live_actions_from_trade_rule
    assert live_actions_from_trade_rule({"id": "e", "actions": [{"action_type": "buy"}]}) == {
        "a0": {"action_type": "buy"}}


@pytest.mark.parametrize("action", ["sell", "close", "adjust_take_profit"])
def test_a_tag_on_an_action_that_never_writes_it_is_refused(action):
    """Only ENTRY actions stamp the tag (option entries and ``buy``); on any other action it
    would be a label nothing writes, so the member's exits would silently never find it."""
    from ba2_common.core.rules_convert import live_actions_from_trade_rule
    with pytest.raises(ValueError, match="entry_tag"):
        live_actions_from_trade_rule({"id": "x", "actions": [
            {"action_type": action, "entry_tag": 19, "reference_value": "order_open_price",
             "action_value": 5.0}]})


def test_the_router_agrees_with_is_option_action_on_every_action_type():
    """The router tests membership against a frozen copy of the option-action set (speed);
    this pins that it can never disagree with the canonical ``is_option_action``, for an enum
    member or its value."""
    from ba2_common.core.types import is_option_action
    for member in A:
        want = ENTRY_ROUTE_OPTION if is_option_action(member.value) else ENTRY_ROUTE_EQUITY
        assert entry_route([member]) == want, member
        assert entry_route([member.value]) == want, member
