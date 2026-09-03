"""``covered_call_days_to_expiry`` + ``close_option(close_target='covered_call')``.

THE PROBLEM THEY EXIST FOR, measured rather than argued. Every other option exit condition
resolves its option through ``existing_order.transaction_id``. On an equity-entry overlay key
(``O_CC``; ``O_WHEEL`` once its put has been assigned) both runtimes evaluate the
OPEN_POSITIONS ruleset once per SYMBOL against the OLDEST entry order -- the STOCK --
(``daily_engine._manage_open_positions``, live ``TradeManager.process_open_positions_
recommendations``), while ``SellCoveredCallAction`` writes the call on its own transaction.
So ``days_to_expiry`` there is not merely wrong, it is INERT: it never fires in either
direction, in either runtime, while carrying a searched gene. That was proved end to end in
``testplatform/backend/tests/backtest/test_covered_call_engine.py`` (the written call expired
worthless with the rule live and with it removed, identically).

THE STANDING RULE these pin: an option exit condition anchored on the evaluated transaction is
inert for a stock-anchored overlay key; overlay keys resolve through the trade REPOSITORY, the
way ``has_covered_call`` already does. Condition and close action use the SAME lookup, so they
cannot disagree about which call is being measured and closed.

The clock is frozen by construction: the evaluation instant is the recommendation's
``created_at`` (2024 sim dates) and the wall clock is 2026, so an implementation reading
``date.today()`` produces a wildly different number.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


SIM_AS_OF = datetime(2024, 6, 15, 15, 30, tzinfo=timezone.utc)
SIM_TODAY = date(2024, 6, 15)
CALL = "AAPL240719C00200000"
CALL2 = "AAPL240816C00210000"


def _setup_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "cc_dte.sqlite"))
    db.init_db()
    return db


class _FakeAccount:
    id = 1


def _rec(as_of=SIM_AS_OF, symbol="AAPL", instance_id=1):
    return SimpleNamespace(created_at=as_of, instance_id=instance_id, symbol=symbol)


def _equity_txn(db, *, symbol="AAPL", expert_id=1):
    """The STOCK the covered call is written against — and the transaction the manage pass
    anchors on, which is the whole point."""
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import AssetClass, OrderDirection, TransactionStatus

    txn = Transaction(
        symbol=symbol, quantity=100, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_date=SIM_AS_OF - timedelta(days=40),
        asset_class=AssetClass.EQUITY, expert_id=expert_id, multiplier=1)
    return db.add_instance(txn)


def _equity_order(db, txn_id, *, symbol="AAPL"):
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus, OrderType

    order = TradingOrder(
        account_id=1, symbol=symbol, quantity=100, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=txn_id,
        asset_class=AssetClass.EQUITY, open_price=190.0, filled_qty=100,
        created_at=datetime.now(timezone.utc))
    db.add_instance(order, expunge_after_flush=True)
    return order


def _call_txn(db, *, symbol="AAPL", expert_id=1):
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import AssetClass, OrderDirection, TransactionStatus

    txn = Transaction(
        symbol=symbol, quantity=1, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, open_date=SIM_AS_OF - timedelta(days=10),
        asset_class=AssetClass.OPTION, option_strategy="covered_call",
        expert_id=expert_id, multiplier=100)
    return db.add_instance(txn)


def _call_order(db, txn_id, *, contract=CALL, expiry, side=None, strategy="covered_call",
                symbol="AAPL", qty=1, status=None, strike=200.0):
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import (
        AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType)

    order = TradingOrder(
        account_id=1, symbol=contract, quantity=qty,
        side=side or OrderDirection.SELL, order_type=OrderType.MARKET,
        status=status or OrderStatus.FILLED, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, contract_symbol=contract, underlying_symbol=symbol,
        option_type=OptionRight.CALL, strike=strike, multiplier=100, expiry=expiry,
        option_strategy=strategy, open_price=2.0, filled_qty=qty,
        created_at=datetime.now(timezone.utc))
    db.add_instance(order, expunge_after_flush=True)
    return order


def _written_call(db, *, expiry, expert_id=1):
    """The whole shape: an OPENED equity lot plus a covered call on its own transaction."""
    eq_txn = _equity_txn(db, expert_id=expert_id)
    stock = _equity_order(db, eq_txn)
    call_txn = _call_txn(db, expert_id=expert_id)
    _call_order(db, call_txn, expiry=expiry)
    return stock, call_txn


def _cond(order, *, op="<=", value=7, rec=None):
    from ba2_common.core.TradeConditions import CoveredCallDaysToExpiryCondition
    return CoveredCallDaysToExpiryCondition(
        account=_FakeAccount(), instrument_name="AAPL",
        expert_recommendation=rec or _rec(), operator_str=op, value=value,
        existing_order=order)


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------
def test_the_written_call_is_measured_from_the_STOCK_position(tmp_path):
    """THE POINT. The evaluated order is the equity lot — the call is on another
    transaction entirely — and the condition still measures it."""
    db = _setup_db(tmp_path)
    stock, _ = _written_call(db, expiry=SIM_TODAY + timedelta(days=5))
    cond = _cond(stock, op="<=", value=7)

    assert cond.evaluate() is True
    assert cond.get_calculated_value() == 5


def test_the_transaction_anchored_reader_sees_NOTHING_on_the_same_book(tmp_path):
    """THE CONTROL, and the reason this condition exists at all.

    Identical rows, identical evaluated order: ``days_to_expiry`` is unevaluable, so it
    answers False for BOTH ``<=`` and ``>`` — inert, not merely wrong. Without this the new
    condition could look like a duplicate of an existing one.
    """
    from ba2_common.core.TradeConditions import DaysToExpiryCondition

    db = _setup_db(tmp_path)
    stock, _ = _written_call(db, expiry=SIM_TODAY + timedelta(days=5))

    def _plain(op, value):
        return DaysToExpiryCondition(
            account=_FakeAccount(), instrument_name="AAPL", expert_recommendation=_rec(),
            operator_str=op, value=value, existing_order=stock)

    assert _plain("<=", 7).evaluate() is False
    assert _plain(">", 7).evaluate() is False       # inert in BOTH directions
    assert _plain("<=", 7).get_calculated_value() is None


def test_the_boundary_is_inclusive_and_the_sign_survives_expiry(tmp_path):
    db = _setup_db(tmp_path)
    _written_call(db, expiry=SIM_TODAY + timedelta(days=7))
    stock = _equity_order(db, _equity_txn(db, expert_id=2), symbol="AAPL")

    exact = _cond(_equity_order(db, _equity_txn(db)), op="<=", value=7)
    assert exact.evaluate() is True and exact.get_calculated_value() == 7


def test_a_call_already_past_its_expiry_reports_a_NEGATIVE_number(tmp_path):
    """Past expiry on a still-open short call is a real and alarming state; clamping to 0
    would make ``> 0`` answer 'still alive'."""
    db = _setup_db(tmp_path)
    stock, _ = _written_call(db, expiry=SIM_TODAY - timedelta(days=3))
    cond = _cond(stock, op="<=", value=0)

    assert cond.evaluate() is True
    assert cond.get_calculated_value() == -3
    assert "expired 3d ago" in cond.get_actual_value_display()


# ---------------------------------------------------------------------------
# unknown is never a value
# ---------------------------------------------------------------------------
def test_no_written_call_is_UNEVALUABLE_not_zero(tmp_path):
    """'Nothing to close' is not '0 days left'. Zero would buy back a call that does not
    exist on every bar the overlay has not yet written one."""
    db = _setup_db(tmp_path)
    stock = _equity_order(db, _equity_txn(db))

    for op, value in (("<=", 7), (">", 7)):
        cond = _cond(stock, op=op, value=value)
        assert cond.evaluate() is False, f"{op} {value} fired with no call held"
        assert cond.get_calculated_value() is None
        assert "no covered call is held" in cond.get_actual_value_display()


def test_a_call_that_was_BOUGHT_BACK_stops_being_measured(tmp_path):
    """The netting the existence check does not do. The ``sell_to_open`` row is still on an
    open transaction after the buy-back; a resolver that only filtered ``side=SELL`` would
    keep reporting the position and the rule would re-submit a close every bar."""
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    stock, call_txn = _written_call(db, expiry=SIM_TODAY + timedelta(days=5))
    _call_order(db, call_txn, expiry=SIM_TODAY + timedelta(days=5),
                side=OrderDirection.BUY, strategy="close")

    cond = _cond(stock, op="<=", value=7)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_a_held_call_with_no_recorded_expiry_is_UNEVALUABLE(tmp_path):
    db = _setup_db(tmp_path)
    stock, _ = _written_call(db, expiry=None)
    cond = _cond(stock, op="<=", value=7)

    assert cond.evaluate() is False
    assert "no recorded expiry" in cond.get_actual_value_display()


def test_two_expiries_held_at_once_are_a_CONTRADICTION_not_a_min(tmp_path):
    """min() closes the wrong contract early, max() never closes. Neither is an answer."""
    db = _setup_db(tmp_path)
    stock, _ = _written_call(db, expiry=SIM_TODAY + timedelta(days=5))
    _call_order(db, _call_txn(db), contract=CALL2, strike=210.0,
                expiry=SIM_TODAY + timedelta(days=40))

    cond = _cond(stock, op="<=", value=7)
    assert cond.evaluate() is False
    assert "different expiries" in cond.get_actual_value_display()


def test_another_experts_covered_call_is_not_this_experts_business(tmp_path):
    db = _setup_db(tmp_path)
    _written_call(db, expiry=SIM_TODAY + timedelta(days=5), expert_id=99)
    stock = _equity_order(db, _equity_txn(db, expert_id=1))

    cond = _cond(stock, op="<=", value=7, rec=_rec(instance_id=1))
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_a_short_call_that_is_a_SPREAD_leg_is_not_a_covered_call(tmp_path):
    """Only the ``covered_call`` tag the entry seam stamped counts. Closing a credit
    spread's short leg as if it were an overlay would leave a naked long."""
    db = _setup_db(tmp_path)
    stock = _equity_order(db, _equity_txn(db))
    _call_order(db, _call_txn(db), expiry=SIM_TODAY + timedelta(days=5),
                strategy="call_credit_spread")

    cond = _cond(stock, op="<=", value=7)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


# ---------------------------------------------------------------------------
# the close that targets it — ONE close implementation, one lookup
# ---------------------------------------------------------------------------
def _close_action(order, *, target="covered_call", rec=None):
    from ba2_common.core.TradeActions import CloseOptionAction
    from ba2_common.core.types import OrderRecommendation

    return CloseOptionAction(
        instrument_name="AAPL", account=_FakeAccount(),
        order_recommendation=OrderRecommendation.SELL, existing_order=order,
        expert_recommendation=rec or _rec(), close_target=target)


def test_the_close_resolves_the_SAME_call_the_condition_measured(tmp_path):
    db = _setup_db(tmp_path)
    stock, _ = _written_call(db, expiry=SIM_TODAY + timedelta(days=5))

    resolved = _close_action(stock)._resolve_option_order()
    assert resolved is not None and resolved.contract_symbol == CALL


def test_without_the_target_the_close_resolves_exactly_as_it_always_did(tmp_path):
    """The parameter is opt-in: absent, the equity anchor finds no option, as before.

    This is what makes every pre-existing ``close_option`` rule byte-identical."""
    db = _setup_db(tmp_path)
    stock, _ = _written_call(db, expiry=SIM_TODAY + timedelta(days=5))

    assert _close_action(stock, target=None)._resolve_option_order() is None


def test_the_close_refuses_rather_than_picking_between_two_written_calls(tmp_path):
    db = _setup_db(tmp_path)
    stock, _ = _written_call(db, expiry=SIM_TODAY + timedelta(days=5))
    _call_order(db, _call_txn(db), contract=CALL2, strike=210.0,
                expiry=SIM_TODAY + timedelta(days=40))

    assert _close_action(stock)._resolve_option_order() is None


def test_the_close_finds_nothing_once_the_call_is_bought_back(tmp_path):
    from ba2_common.core.types import OrderDirection

    db = _setup_db(tmp_path)
    stock, call_txn = _written_call(db, expiry=SIM_TODAY + timedelta(days=5))
    _call_order(db, call_txn, expiry=SIM_TODAY + timedelta(days=5),
                side=OrderDirection.BUY, strategy="close")

    assert _close_action(stock)._resolve_option_order() is None


def test_the_rule_builder_carries_the_target_into_the_action_config():
    """The knob has to survive the shared converter, or the rule names a target nothing
    reads — the exact shape of a silently inert gene."""
    from ba2_common.core.rule_builders import action_from_rule

    built = action_from_rule({"action": "close_option", "close_target": "covered_call"})
    assert built["act"] == {"action_type": "close_option", "close_target": "covered_call"}
    # ...and an ordinary close is untouched.
    assert action_from_rule({"action": "close_option"})["act"] == {
        "action_type": "close_option"}


def test_the_live_export_carries_the_target_too():
    """Both directions go through one converter, so a deployed O_CC ruleset closes the same
    call the backtest closed."""
    from ba2_common.core.rules_convert import trade_rules_to_live_export

    rule = {"id": "cc_dte", "actions": [{"action_type": "close_option",
                                         "close_target": "covered_call"}]}
    export = trade_rules_to_live_export(entry_rules=[], exit_rules=[rule])
    actions = export["rulesets"][0]["rules"][0]["actions"]
    assert actions["a0"]["close_target"] == "covered_call"
