"""``entry_tag``: which member of a composed (style-group) option expert opened a position.

WHY (plan 2026-09-25 option-stage2-build, Task 1). One OPEN_POSITIONS ruleset serves every
member of a style expert and is evaluated per SYMBOL against the entry order, so per-member
exits need to know which member opened the position. ``Transaction.option_strategy`` cannot
tell (O_LC, O_LEAPC and O_CONVEXC are all ``long_call``), so the ENTRY writes an integer tag
onto its parent order row -- the same seam, and the same "read back, never reconstruct" rule,
as ``earnings_event_date`` and ``max_loss_per_contract``.

WHAT IS PINNED HERE.

* ABSENT STAYS ABSENT. A rule with no ``option_entry_tag`` converts to the byte-identical
  action config, the action carries ``entry_tag = None``, and the order row gets NO key. This
  is what keeps every existing single-structure run, every live rule and both goldens
  byte-identical.
* THE ROW, NOT THE RESULT. The stamp is read back off the STORED order row (the whitelist
  trap ``test_earnings_event_date_persisted_at_submit`` describes).
* LOUD ON NONSENSE. A tag that is not a positive integer is refused at construction (and a
  corrupted stored tag is refused at read-back) -- never guessed at, never read as absent.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import create_action
from ba2_common.core.entry_tag import ORDER_ENTRY_TAG_KEY, coerce_entry_tag, order_entry_tag
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.rule_builders import action_from_rule
from ba2_common.core.types import ExpertActionType, OptionRight, OrderDirection

TODAY = date(2024, 6, 1)
EXPIRY = date(2024, 6, 21)


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """Sibling DB-seam tests repoint the global DB seam without restoring it, so re-point to a
    fresh sqlite per test here (order-independence)."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "entry_tag_fact.sqlite"))
    db.init_db()
    yield


class FakeAccount(OptionsAccountInterface):
    """Captures ``submit_option_order`` and returns the id of a REAL order row, so the entry
    facts are persisted and can be read back exactly as the exit side reads them."""

    def __init__(self, order_id):
        self.id = 1
        self.submitted = []
        self.order_id = order_id

    def decision_label(self):
        return self._as_of_date()

    def _as_of_date(self):
        return TODAY

    def get_balance(self):
        return 100_000.0

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=100_000.0, equity=100_000.0, net_liquidation=100_000.0)

    def get_instrument_current_price(self, symbol, price_type=None):
        return 100.0

    def get_current_price(self, symbol=None):
        return 100.0

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        return []

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(dict(legs=legs, quantity=quantity, strategy=option_strategy))
        return SimpleNamespace(id=self.order_id, data={})

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order

    def get_option_quote(self, contract_symbol):
        return None

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None):
        return None


def _rec():
    return SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                           expected_profit_percent=None, recommended_action=None)


def _parent_row():
    """A parent order row as ``submit_option_order`` persists one: no contract symbol (the
    CONTRACTS ride the leg children, the entry facts ride the parent)."""
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderStatus, OrderType
    return add_instance(TradingOrder(account_id=1, symbol="XYZ", quantity=1,
                                     side=OrderDirection.BUY, order_type=OrderType.MARKET,
                                     status=OrderStatus.PENDING))


def _call_leg():
    return OptionLeg(contract_symbol="XYZ240621C00100000", side=OrderDirection.BUY,
                     position_intent="buy_to_open", option_type=OptionRight.CALL,
                     strike=100.0, expiry=EXPIRY, underlying="XYZ")


def _put_leg():
    return OptionLeg(contract_symbol="XYZ240621P00100000", side=OrderDirection.BUY,
                     position_intent="buy_to_open", option_type=OptionRight.PUT,
                     strike=100.0, expiry=EXPIRY, underlying="XYZ")


class _Submitted:
    def __init__(self, action, legs, strategy):
        self.action, self.legs, self.strategy = action, legs, strategy

    def submitted_order(self):
        from ba2_common.core.db import get_instance
        from ba2_common.core.models import TradingOrder
        res = self.action._submit_option_order(self.legs, 1, 6.0, self.strategy)
        assert res["success"], res["message"]
        return get_instance(TradingOrder, self.action.account.order_id)


def _builder(action_type, legs, strategy):
    def build(**kw):
        acct = FakeAccount(_parent_row())
        action = create_action(action_type, "XYZ", acct, SimpleNamespace(), None, _rec(),
                               strike_method="percent_otm", strike_param=0.0,
                               dte_min=10, dte_max=40, sizing=5.0, **kw)
        action.submit_to_broker = True
        return _Submitted(action, legs, strategy)
    return build


@pytest.fixture
def buy_call_with():
    return _builder(ExpertActionType.BUY_CALL, [_call_leg()], "long_call")


@pytest.fixture
def straddle_with():
    return _builder(ExpertActionType.OPEN_STRADDLE, [_call_leg(), _put_leg()], "long_straddle")


# --------------------------------------------------------------------------- #
# rule -> action config -> action ctor
# --------------------------------------------------------------------------- #
def test_rule_builder_forwards_option_entry_tag():
    cfg = action_from_rule({"action_type": "buy_call", "option_entry_tag": 7})["act"]
    assert cfg["entry_tag"] == 7


def test_rule_builder_omits_an_absent_tag():
    """Byte-identical conversion for every rule written before the tag existed."""
    cfg = action_from_rule({"action_type": "buy_call", "option_sizing": 5.0})["act"]
    assert "entry_tag" not in cfg
    assert cfg == {"action_type": "buy_call", "sizing": 5.0}


def test_close_option_never_carries_a_tag():
    """A close resolves its position; the tag belongs to the ENTRY that opened it. A tag on a
    close is a label nothing stamps, so it is REFUSED (E1 follow-up; it used to be dropped
    silently)."""
    with pytest.raises(ValueError, match="entry_tag"):
        action_from_rule({"action_type": "close_option", "option_entry_tag": 7})
    assert action_from_rule({"action_type": "close_option"})["act"] == {
        "action_type": "close_option"}


def test_the_evaluator_forwards_it_to_the_entry_action():
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
    from ba2_common.core.types import OrderRecommendation

    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    ev.account = FakeAccount(order_id=1)
    action = ev._create_trade_action(
        ExpertActionType.BUY_CALL, {"action_type": "buy_call", "entry_tag": 5}, "XYZ",
        OrderRecommendation.BUY, None, _rec())
    assert action is not None and action.entry_tag == 5

    untagged = ev._create_trade_action(
        ExpertActionType.BUY_CALL, {"action_type": "buy_call"}, "XYZ",
        OrderRecommendation.BUY, None, _rec())
    assert untagged is not None and untagged.entry_tag is None


def test_the_deploy_converter_carries_it_to_the_live_action():
    """trade_rules_to_live_export is what tools/import_deploy_payload.py writes live rules
    from: a style entry's tag must reach the live EventAction, or every live position of
    that member is untagged and its (tag-scoped) exits can never fire."""
    from ba2_common.core.rules_convert import live_actions_from_trade_rule
    rule = {"id": "enter", "actions": [{"action_type": "buy_call", "option_sizing": 5.0,
                                         "option_entry_tag": 3}]}
    out = live_actions_from_trade_rule(rule)
    (action,) = out.values()
    assert action["entry_tag"] == 3


def test_a_live_ruleset_round_trip_keeps_the_tag():
    """live export -> TradeRules -> live export: ``_live_action_to_action_cfg`` keeps the live
    ``entry_tag`` key verbatim, so the forward builder must read the bare key too, or a
    re-export silently strips every member's tag."""
    from ba2_common.core.rules_convert import (live_actions_from_trade_rule,
                                               live_export_to_trade_rules)
    payload = {"rulesets": [{"name": "enter", "subtype": "enter_market", "rules": [{
        "name": "o_lc-entry", "order_index": 0, "triggers": {},
        "actions": {"a0": {"action_type": "buy_call", "sizing": 5.0, "entry_tag": 1}}}]}]}
    (rule,) = live_export_to_trade_rules(payload)["entry_rules"]
    (action,) = live_actions_from_trade_rule(rule).values()
    assert action["entry_tag"] == 1


def test_a_bad_tag_is_refused_where_the_rule_is_converted():
    with pytest.raises(ValueError, match="entry_tag"):
        action_from_rule({"action_type": "buy_call", "option_entry_tag": "x"})


@pytest.mark.parametrize("raw,want", [(None, None), (1, 1), (14, 14), (3.0, 3)])
def test_coerce_accepts_positive_integers_and_none(raw, want):
    got = coerce_entry_tag(raw)
    assert got == want and (got is None or type(got) is int)


@pytest.mark.parametrize("bad", [0, -3, 1.5, "x", "3", True, False, float("nan"),
                                 float("inf"), [3], {"tag": 3}])
def test_coerce_refuses_everything_else(bad):
    with pytest.raises(ValueError, match="entry_tag"):
        coerce_entry_tag(bad)


@pytest.mark.parametrize("bad", [0, -3, 1.5, "x", True])
def test_a_bad_tag_is_refused_at_construction(buy_call_with, bad):
    with pytest.raises(ValueError, match="entry_tag"):
        buy_call_with(entry_tag=bad)


# --------------------------------------------------------------------------- #
# the order ROW (the only thing the exit side can read)
# --------------------------------------------------------------------------- #
def test_the_tag_rides_the_parent_order_row(buy_call_with):
    order = buy_call_with(entry_tag=14).submitted_order()
    assert order.data[ORDER_ENTRY_TAG_KEY] == 14
    assert order_entry_tag(order) == 14


def test_no_tag_writes_nothing(buy_call_with):
    order = buy_call_with().submitted_order()
    assert ORDER_ENTRY_TAG_KEY not in (order.data or {})
    assert order_entry_tag(order) is None


def test_no_tag_leaves_the_result_data_byte_identical(buy_call_with):
    """The TradeActionResult data an untagged entry returns is exactly what it was before
    the tag existed (the tag is an ORDER-ROW fact, stamped by name)."""
    sub = buy_call_with()
    res = sub.action._submit_option_order(sub.legs, 1, 6.0, sub.strategy)
    assert res["success"] and ORDER_ENTRY_TAG_KEY not in res["data"]


def test_multi_leg_structures_tag_the_PARENT_not_a_leg(straddle_with):
    parent = straddle_with(entry_tag=3).submitted_order()
    assert parent.contract_symbol is None and parent.data[ORDER_ENTRY_TAG_KEY] == 3
    # its neighbour on the same seam, so the seam itself is identified
    assert "max_loss_per_contract" in parent.data


# --------------------------------------------------------------------------- #
# read-back
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("order", [
    None,
    SimpleNamespace(id=1),                     # no data attribute at all
    SimpleNamespace(id=1, data=None),
    SimpleNamespace(id=1, data={}),
    SimpleNamespace(id=1, data={"max_loss_per_contract": 500.0}),
    SimpleNamespace(id=1, data={ORDER_ENTRY_TAG_KEY: None}),
])
def test_read_back_is_None_for_every_untagged_shape(order):
    assert order_entry_tag(order) is None


@pytest.mark.parametrize("stored", [0, -1, 2.5, "3", True, [1]])
def test_a_corrupted_stored_tag_is_refused_not_read_as_absent(stored):
    """A decision depends on this value (which member's exits may close the position): a
    stored value no entry could have written is a defect, refused by name."""
    with pytest.raises(ValueError, match="entry_tag"):
        order_entry_tag(SimpleNamespace(id=9, data={ORDER_ENTRY_TAG_KEY: stored}))


# --------------------------------------------------------------------------- #
# review 2026-09-25 follow-up
# --------------------------------------------------------------------------- #
def test_numpy_integers_are_tags_but_numpy_bools_are_not():
    np = pytest.importorskip("numpy")
    assert coerce_entry_tag(np.int64(7)) == 7 and type(coerce_entry_tag(np.int64(7))) is int
    assert coerce_entry_tag(np.float64(7.0)) == 7
    with pytest.raises(ValueError, match="entry_tag"):
        coerce_entry_tag(np.bool_(True))
    with pytest.raises(ValueError, match="entry_tag"):
        coerce_entry_tag(np.int64(0))


def _submit_against(action, order_id):
    action.account.order_id = order_id
    return action._submit_option_order([_call_leg()], 1, 6.0, "long_call")


def test_a_tag_that_cannot_reach_its_row_fails_loudly(buy_call_with):
    """The row is the ONLY thing the member's exits can read: a tagged entry whose row is
    missing must not report success with the tag silently lost."""
    sub = buy_call_with(entry_tag=5)
    with pytest.raises(RuntimeError, match="entry_tag"):
        _submit_against(sub.action, 987654)          # no such order row


def test_an_untagged_entry_with_no_row_keeps_todays_behaviour(buy_call_with):
    """Byte-identical for every existing entry: the missing-row case is still only logged."""
    sub = buy_call_with()
    res = _submit_against(sub.action, 987654)
    assert res["success"] is True


# ------------------------------------------------------------------------------------------- #
# stage-2 E2 review C1: an OVERLAY entry inherits the tag of the position it is written on
# ------------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("action_type, cfg", [
    (ExpertActionType.SELL_COVERED_CALL, {"action_type": "sell_covered_call"}),
    (ExpertActionType.BUY_PROTECTIVE_PUT, {"action_type": "buy_protective_put"}),
])
def test_an_overlay_inherits_the_member_tag_of_the_evaluated_position(action_type, cfg):
    """``sell_covered_call`` / ``buy_protective_put`` come from an OPEN_POSITIONS rule and
    author no tag; the covered stock's entry order (the evaluated ``existing_order`` both
    runtimes hand the rule) carries its member's. The overlay inherits it, so that member's
    own tag-scoped exits can see its call / put. Shared: the live open-positions pass builds
    its actions through this same evaluator."""
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
    from ba2_common.core.types import OrderRecommendation

    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    ev.account = FakeAccount(order_id=1)
    stock = SimpleNamespace(id=9, data={ORDER_ENTRY_TAG_KEY: 7}, contract_symbol=None)
    action = ev._create_trade_action(action_type, dict(cfg), "XYZ", OrderRecommendation.BUY,
                                     stock, _rec())
    assert action is not None and action.entry_tag == 7

    untagged = ev._create_trade_action(action_type, dict(cfg), "XYZ",
                                       OrderRecommendation.BUY,
                                       SimpleNamespace(id=9, data=None, contract_symbol=None),
                                       _rec())
    assert untagged is not None and untagged.entry_tag is None, "untagged stays untagged"


def test_an_overlays_own_authored_tag_wins_over_the_inherited_one():
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
    from ba2_common.core.types import OrderRecommendation

    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    ev.account = FakeAccount(order_id=1)
    stock = SimpleNamespace(id=9, data={ORDER_ENTRY_TAG_KEY: 7}, contract_symbol=None)
    action = ev._create_trade_action(
        ExpertActionType.SELL_COVERED_CALL, {"action_type": "sell_covered_call", "entry_tag": 2},
        "XYZ", OrderRecommendation.BUY, stock, _rec())
    assert action.entry_tag == 2


def test_a_non_overlay_entry_never_inherits_a_tag():
    """A plain option entry is its own member's position: it must not pick up the tag of
    whatever order happens to be evaluated."""
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
    from ba2_common.core.types import OrderRecommendation

    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    ev.account = FakeAccount(order_id=1)
    stock = SimpleNamespace(id=9, data={ORDER_ENTRY_TAG_KEY: 7}, contract_symbol=None)
    action = ev._create_trade_action(ExpertActionType.BUY_CALL, {"action_type": "buy_call"},
                                     "XYZ", OrderRecommendation.BUY, stock, _rec())
    assert action.entry_tag is None
