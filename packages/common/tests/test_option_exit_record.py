"""BT/live option parity, plan Part C3: the EXIT half of the option trade record.

Every option closing ticket that runs through shared code writes ``data["exit_record"]``:

    {"version", "trigger": OptionCloseReason, "rule_id", "rule_name",
     "legs": [leg_snapshot...], "legs_without_quote": [...]}

* the TRIGGER of a rule-fired close is classified from the rule's trigger SEMANTICS by
  ``TradeActionEvaluator.option_close_trigger`` (never its name) -- pinned here against the
  exact triggers the grid launcher emits;
* the leg snapshots come from the SAME ``snapshot_legs`` the entry record uses, fed with the
  quote the close priced itself from (no second fetch);
* the record NEVER blocks the close: a failure is an ``error`` record carrying the trigger.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import (
    CloseOptionAction, build_exit_record, create_action,
)
from ba2_common.core.TradeActionEvaluator import option_close_trigger
from ba2_common.core.option_trade_record import (
    LEG_SNAPSHOT_FIELDS, OPTION_TRADE_RECORD_VERSION, contract_from_quote, exit_record,
    exit_record_error,
)
from ba2_common.core.option_types import OptionContract, OptionLeg, OptionQuote
from ba2_common.core.types import (
    ExpertActionType, OptionCloseReason, OptionRight, OrderDirection, OrderRecommendation,
)


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "exit_record.sqlite"))
    db.init_db()
    yield


def _rule(*leaves):
    return SimpleNamespace(id=7, name="rule", actions={}, triggers={
        f"c{i}": {"event_type": et, "operator": op, "value": v}
        for i, (et, op, v) in enumerate(leaves)})


# ---------------------------------------------------------------------------
# the classifier
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rule_id,leaf,expected", [
    # exactly what ba2test_launcher._option_exit_rules / _overlay_rules emit
    ("opt_tp", ("profit_loss_percent", ">", 50), OptionCloseReason.TAKE_PROFIT),
    ("opt_tp_mult", ("profit_multiple_of_premium", ">=", 3.0), OptionCloseReason.TAKE_PROFIT),
    ("opt_sl", ("profit_loss_percent", "<", -100), OptionCloseReason.STOP_LOSS),
    ("opt_sl_ml", ("loss_pct_of_max_loss", ">", 50), OptionCloseReason.STOP_LOSS),
    ("opt_time", ("days_opened", ">", 28), OptionCloseReason.TIME_EXIT),
    ("opt_event", ("days_after_event", ">=", 1), OptionCloseReason.TIME_EXIT),
    ("opt_dte", ("days_to_expiry", "<=", 21), OptionCloseReason.DTE_EXIT),
    ("cc_dte", ("covered_call_days_to_expiry", "<=", 5), OptionCloseReason.DTE_EXIT),
    ("short_dte", ("short_leg_days_to_expiry", "<=", 5), OptionCloseReason.DTE_EXIT),
    ("credit_tp", ("credit_decayed_pct", ">=", 50), OptionCloseReason.TAKE_PROFIT),
    ("amount_sl", ("profit_loss_amount", "<=", -500), OptionCloseReason.STOP_LOSS),
    ("multiple_sl", ("profit_multiple_of_premium", "<", 1.0), OptionCloseReason.STOP_LOSS),
    # the inverted field's profit side is NOT a take-profit (no TP reading, see forced_option_exit)
    ("ml_other_side", ("loss_pct_of_max_loss", "<", -25), OptionCloseReason.RULE_EXIT),
    ("delta", ("long_leg_delta", "<", 0.3), OptionCloseReason.RULE_EXIT),
])
def test_each_grid_exit_rule_records_its_trigger(rule_id, leaf, expected):
    assert option_close_trigger(_rule(leaf)) is expected, rule_id


def test_a_flag_rule_or_an_empty_rule_is_a_rule_exit():
    flag = SimpleNamespace(triggers={"c0": {"event_type": "bearish"}})
    assert option_close_trigger(flag) is OptionCloseReason.RULE_EXIT
    assert option_close_trigger(SimpleNamespace(triggers={})) is OptionCloseReason.RULE_EXIT
    assert option_close_trigger(None) is OptionCloseReason.RULE_EXIT


def test_anded_triggers_record_the_highest_precedence_class():
    """Triggers in one rule are ANDed -- the rule fired on all of them. Risk outranks a
    schedule, a schedule outranks a profit target."""
    dte_and_stop = _rule(("days_to_expiry", "<=", 21), ("profit_loss_percent", "<", -50))
    time_and_tp = _rule(("days_opened", ">", 10), ("profit_loss_percent", ">", 20))
    dte_and_time = _rule(("days_opened", ">", 10), ("days_to_expiry", "<=", 7))
    assert option_close_trigger(dte_and_stop) is OptionCloseReason.STOP_LOSS
    assert option_close_trigger(time_and_tp) is OptionCloseReason.TIME_EXIT
    assert option_close_trigger(dte_and_time) is OptionCloseReason.DTE_EXIT


def test_the_evaluator_threads_trigger_and_rule_identity_without_touching_forced_exit():
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    ev.account = SimpleNamespace()
    rec = SimpleNamespace(id=1, instance_id=None, recommended_action=OrderRecommendation.SELL)
    tp = _rule(("profit_loss_percent", ">", 50))
    tp.id, tp.name = 12, "opt_tp-rule"
    action = ev._create_trade_action(
        ExpertActionType.CLOSE_OPTION, {"action_type": "close_option"}, "XYZ",
        OrderRecommendation.SELL, None, rec, event_action=tp)
    assert isinstance(action, CloseOptionAction)
    assert action.close_trigger is OptionCloseReason.TAKE_PROFIT
    assert (action.rule_id, action.rule_name) == (12, "opt_tp-rule")
    assert action.forced_exit is False                 # the decision input is unchanged


def test_a_close_built_outside_the_evaluator_is_manual():
    a = create_action(ExpertActionType.CLOSE_OPTION, "XYZ", SimpleNamespace(),
                      SimpleNamespace(), None, SimpleNamespace(id=1))
    assert a.close_trigger is OptionCloseReason.MANUAL and a.rule_id is None
    # a __new__ double (the unit tests' idiom) reads the same class defaults
    b = CloseOptionAction.__new__(CloseOptionAction)
    assert b.close_trigger is OptionCloseReason.MANUAL and b._close_quotes is None


# ---------------------------------------------------------------------------
# the record shape
# ---------------------------------------------------------------------------
def test_exit_record_shape_and_an_unknown_trigger_is_refused():
    rec = exit_record("stop_loss", rule_id=3, rule_name="r", legs_without_quote=["X"])
    assert rec == {"version": OPTION_TRADE_RECORD_VERSION, "trigger": "stop_loss",
                   "rule_id": 3, "rule_name": "r", "legs": [], "legs_without_quote": ["X"]}
    assert exit_record(OptionCloseReason.ROLL)["trigger"] == "roll"
    with pytest.raises(ValueError):
        exit_record("option_expiry")                    # the old free-text reason
    with pytest.raises(ValueError):
        exit_record("take_profit", legs=[{"contract_symbol": "X"}])   # a short leg snapshot
    err = exit_record_error("assigned", "boom", rule_id=None)
    assert err["trigger"] == "assigned" and err["error"] == "boom"


_OCC = "XYZ240621C00100000"
_STAMP = datetime(2024, 6, 1, 15, 30, tzinfo=timezone.utc)


def _closing_leg():
    return OptionLeg(contract_symbol=_OCC, side=OrderDirection.SELL,
                     position_intent="sell_to_close", option_type=OptionRight.CALL,
                     strike=100.0, expiry=date(2024, 6, 21), underlying="XYZ")


def test_a_close_quote_becomes_the_same_contract_view_an_entry_reads():
    quote = OptionQuote(symbol=_OCC, bid=2.0, ask=2.2, last=2.1, implied_volatility=0.3,
                        delta=0.4, gamma=0.05, theta=-0.02, vega=0.1, timestamp=_STAMP,
                        rho=0.01, volume=77)
    c = contract_from_quote(quote, _closing_leg())
    assert isinstance(c, OptionContract)
    assert (c.symbol, c.option_type, c.strike, c.expiry, c.underlying) == (
        _OCC, OptionRight.CALL, 100.0, date(2024, 6, 21), "XYZ")
    assert (c.bid, c.ask, c.volume, c.rho, c.quote_time) == (2.0, 2.2, 77, 0.01, _STAMP)
    assert c.open_interest is None and c.greeks_source is None   # unknown, never 0
    assert contract_from_quote(None, _closing_leg()) is None
    assert contract_from_quote(c, _closing_leg()) is c


class _LiveLikeAccount:
    OPTION_GREEKS_SOURCE = "broker"

    def __init__(self, spot=105.0, records=True):
        self.spot = spot
        self.spot_reads = 0
        self.records = records

    def records_option_trades(self):
        return self.records

    def get_option_underlying_price(self, symbol, price_type=None):
        self.spot_reads += 1
        return self.spot

    def decision_label(self):
        return date(2024, 6, 1)


def test_build_exit_record_snapshots_the_priced_leg():
    acct = _LiveLikeAccount()
    quote = OptionQuote(symbol=_OCC, bid=2.0, ask=2.2, last=2.1, delta=0.4, timestamp=_STAMP)
    rec = build_exit_record(acct, OptionCloseReason.TAKE_PROFIT, legs=[_closing_leg()],
                            quotes={_OCC: quote}, underlying="XYZ", rule_id=5,
                            rule_name="opt_tp")
    assert rec["trigger"] == "take_profit" and rec["legs_without_quote"] == []
    (leg,) = rec["legs"]
    assert set(leg) == set(LEG_SNAPSHOT_FIELDS)
    assert leg["side"] == "sell" and leg["position_intent"] == "sell_to_close"
    assert leg["spot"] == 105.0 and leg["dte"] == 20 and leg["data_session"] == "2024-05-31"
    assert leg["greeks_source"] == "broker" and leg["quote_time"] == _STAMP.isoformat()
    assert leg["mid"] == pytest.approx(2.1)
    assert acct.spot_reads == 1


def test_a_close_with_no_quote_names_the_leg_and_reads_no_spot():
    acct = _LiveLikeAccount()
    rec = build_exit_record(acct, "circuit_breaker", legs=[_closing_leg()], quotes={},
                            underlying="XYZ")
    assert rec["legs"] == [] and rec["legs_without_quote"] == [_OCC]
    assert acct.spot_reads == 0


@pytest.mark.parametrize("spot", [None, 0.0, float("nan")])
def test_a_missing_spot_is_an_error_record_that_keeps_the_trigger(spot):
    rec = build_exit_record(_LiveLikeAccount(spot=spot), OptionCloseReason.STOP_LOSS,
                            legs=[_closing_leg()],
                            quotes={_OCC: OptionQuote(symbol=_OCC, bid=1.0, ask=1.1)},
                            underlying="XYZ", rule_id=9)
    assert rec["trigger"] == "stop_loss" and rec["rule_id"] == 9
    assert "EntryRecordSpotUnavailable" in rec["error"] and "legs" not in rec


def test_an_account_declaring_no_greeks_source_is_an_error_record_not_a_crash():
    acct = _LiveLikeAccount()
    acct.OPTION_GREEKS_SOURCE = None
    rec = build_exit_record(acct, "take_profit", legs=[_closing_leg()],
                            quotes={_OCC: OptionQuote(symbol=_OCC, bid=1.0, ask=1.1)},
                            underlying="XYZ")
    assert rec["trigger"] == "take_profit" and "OptionGreeksSourceUndeclared" in rec["error"]


def test_a_bad_trigger_is_still_recorded_loudly_not_dropped():
    rec = build_exit_record(_LiveLikeAccount(), "not-a-reason", legs=[_closing_leg()],
                            quotes={}, underlying="XYZ")
    assert rec["trigger"] is None and "error" in rec


# ---------------------------------------------------------------------------
# the close path writes it onto the ORDER ROW (live-shaped account)
# ---------------------------------------------------------------------------
def test_a_single_leg_close_stamps_its_exit_record_on_the_order_row():
    from tests.test_new_option_actions import FakeAccount
    from ba2_common.core.db import add_instance, get_instance
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import AssetClass, OrderStatus, OrderType

    class Broker(FakeAccount):
        OPTION_GREEKS_SOURCE = "broker"

        def get_option_quote(self, contract_symbol):
            return OptionQuote(symbol=contract_symbol, bid=2.0, ask=2.2, last=2.1,
                               timestamp=_STAMP)

        def close_option_position(self, position, order_type="limit", limit_price=None,
                                  transaction_id=None):
            return get_instance(TradingOrder, add_instance(TradingOrder(
                account_id=1, symbol=position.contract_symbol, quantity=1,
                side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
                status=OrderStatus.PENDING, limit_price=limit_price,
                asset_class=AssetClass.OPTION, contract_symbol=position.contract_symbol,
                data={})))

    entry = get_instance(TradingOrder, add_instance(TradingOrder(
        account_id=1, symbol="XYZ", underlying_symbol="XYZ", quantity=1, filled_qty=1,
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED,
        open_price=3.0, asset_class=AssetClass.OPTION, contract_symbol=_OCC,
        option_type=OptionRight.CALL, strike=100.0, expiry=date(2024, 6, 21), data={})))
    acct = Broker(spot=95.0)
    action = create_action(ExpertActionType.CLOSE_OPTION, "XYZ", acct, SimpleNamespace(),
                           entry, SimpleNamespace(id=1), close_trigger="stop_loss",
                           rule_id=31, rule_name="opt_sl")
    action.submit_to_broker = True
    result = action.execute()
    assert result["success"], result["message"]

    close = [o for o in _all_orders() if o.side == OrderDirection.SELL]
    (close,) = close
    record = close.data["exit_record"]
    assert (record["trigger"], record["rule_id"], record["rule_name"]) == (
        "stop_loss", 31, "opt_sl")
    (leg,) = record["legs"]
    assert leg["bid"] == 2.0 and leg["spot"] == 95.0 and leg["greeks_source"] == "broker"
    assert leg["quote_time"] == _STAMP.isoformat()


def _all_orders():
    from sqlmodel import Session, select
    from ba2_common.core.db import get_db
    from ba2_common.core.models import TradingOrder
    with Session(get_db().bind) as s:
        rows = s.exec(select(TradingOrder)).all()
        s.expunge_all()
    return rows


# ---------------------------------------------------------------------------
# the live lifecycle pass's closes
# ---------------------------------------------------------------------------
def test_every_lifecycle_closing_reason_has_a_trigger():
    from ba2_common.core.option_lifecycle import (
        LIFECYCLE_CLOSE_TRIGGERS, LIFECYCLE_CLOSING_REASONS,
    )
    assert set(LIFECYCLE_CLOSE_TRIGGERS) == set(LIFECYCLE_CLOSING_REASONS)
    assert all(isinstance(v, OptionCloseReason) for v in LIFECYCLE_CLOSE_TRIGGERS.values())


# ---------------------------------------------------------------------------
# a run that does NOT record option trades (a GA fitness trial)
# ---------------------------------------------------------------------------
def test_a_non_recording_account_gets_only_the_trigger_and_no_spot_read():
    acct = _LiveLikeAccount(records=False)
    rec = build_exit_record(acct, OptionCloseReason.STOP_LOSS, legs=[_closing_leg()],
                            quotes={_OCC: OptionQuote(symbol=_OCC, bid=1.0, ask=1.1)},
                            underlying="XYZ", rule_id=4, rule_name="opt_sl")
    assert rec == {"version": OPTION_TRADE_RECORD_VERSION, "trigger": "stop_loss",
                   "rule_id": 4, "rule_name": "opt_sl", "lean": True}
    assert acct.spot_reads == 0


def test_every_live_account_records_fully():
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
    assert OptionsAccountInterface.records_option_trades(object()) is True


def test_a_non_recording_entry_builds_no_record_but_keeps_the_refusal_gate():
    """No record (no snapshot, no payoff chart) -- and the SAME refusal on a missing spot, so
    whether the entry happens does not depend on whether it is recorded."""
    from tests.test_new_option_actions import FakeAccount

    class Account(FakeAccount):
        OPTION_GREEKS_SOURCE = "broker"
        records = True

        def records_option_trades(self):
            return self.records

    chosen = OptionContract(symbol=_OCC, underlying="XYZ", option_type=OptionRight.CALL,
                            strike=100.0, expiry=date(2024, 6, 21), bid=2.0, ask=2.2)
    leg = OptionLeg(contract_symbol=_OCC, side=OrderDirection.BUY, position_intent="buy_to_open",
                    option_type=OptionRight.CALL, strike=100.0, expiry=date(2024, 6, 21),
                    underlying="XYZ", quote=chosen)
    kw = dict(quantity=1, limit_price=2.1, option_strategy="long_call",
              max_loss_per_contract=210.0)
    for records in (True, False):
        for spot, refused in ((100.0, False), (None, True), (0.0, True)):
            acct = Account(spot=spot)
            acct.records = records
            act = create_action(ExpertActionType.BUY_CALL, "XYZ", acct, SimpleNamespace(),
                                None, SimpleNamespace(id=1))
            act._last_spot = None
            record, refusal = act._entry_record_or_refusal([leg], **kw)
            assert (refusal is not None) is refused, (records, spot)
            if records and not refused:
                assert record["legs"][0]["contract_symbol"] == _OCC
            else:
                assert record is None
