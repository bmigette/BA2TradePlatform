"""The EVENT DATE is carried forward onto the entry order at submit -- design 2026-08-31 S9.

``days_after_event`` (the ``O_ERN`` exit gene) measures from the date of the event the
position was OPENED for. By exit time the recommendation in hand is a different, later one,
so the date has to travel with the position. It rides the same seam ``max_loss_per_contract``
uses: ``_submit_option_order``, the one choke point every option builder reaches, writing
onto the parent order's ``data``.

TWO THINGS ARE PINNED HERE AND NEITHER IS COSMETIC.

**The gate.** Stamped ONLY when the recommendation actually carried an earnings payload.
Absence of the key is what makes ``days_after_event`` unevaluable for every position that is
not an earnings trade -- every equity order and every option order from any other expert. If
the submit path defaulted the date (to today, to the expiry, to anything), the exit would
fire on positions that have no event.

**The whitelist.** ``_submit_option_order`` writes its result ``data`` freely but persists
only a NAMED TUPLE of keys onto the order ROW. A key in ``data`` and not in that tuple shows
up in every log and every TradeActionResult while the ORDER -- the only thing the exit
condition can read -- never gets it. That is a live gene tuned against a simulation it cannot
touch (the whitelist trap this codebase has now hit on both the condition side and the
settings side). So the test that matters is the one that reads the STORED ROW back.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import create_action
from ba2_common.core.earnings_stamp import (
    EARNINGS_STAMP_NAMESPACE,
    ORDER_EVENT_DATE_KEY,
)
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import ExpertActionType, OptionRight, OrderDirection

TODAY = date(2024, 6, 1)
EXPIRY = date(2024, 6, 21)
EVENT = date(2024, 6, 3)          # the Monday print this entry is timed on


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """Sibling DB-seam tests repoint the global DB seam without restoring it, so re-point
    to a fresh sqlite per test here (order-independence)."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "earnings_event_at_submit.sqlite"))
    db.init_db()
    yield


class FakeAccount(OptionsAccountInterface):
    """Captures ``submit_option_order`` and returns an order id the test can point at a row."""

    def __init__(self):
        self.id = 1
        self.submitted = []
        self.next_order_id = 1

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
        self.submitted.append(dict(legs=legs, quantity=quantity, limit_price=limit_price,
                                   strategy=option_strategy))
        return SimpleNamespace(id=self.next_order_id, data={})

    # --- unused abstract bits
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


def _rec(data):
    return SimpleNamespace(id=1, instance_id=None, data=data, price_at_date=None,
                           expected_profit_percent=None, recommended_action=None)


def _straddle_legs():
    """A long straddle -- the O_ERN structure. Both legs long, so the max-loss stamp is
    measured too and the two keys travel together, which is the point."""
    return [
        OptionLeg(contract_symbol="XYZ240621C00100000", side=OrderDirection.BUY,
                  position_intent="buy_to_open", option_type=OptionRight.CALL,
                  strike=100.0, expiry=EXPIRY, underlying="XYZ"),
        OptionLeg(contract_symbol="XYZ240621P00100000", side=OrderDirection.BUY,
                  position_intent="buy_to_open", option_type=OptionRight.PUT,
                  strike=100.0, expiry=EXPIRY, underlying="XYZ"),
    ]


def _submit(acct, rec_data, *, strategy="long_straddle"):
    action = create_action(ExpertActionType.OPEN_STRADDLE, "XYZ", acct, SimpleNamespace(),
                           None, _rec(rec_data),
                           strike_method="percent_otm", strike_param=0.0,
                           dte_min=10, dte_max=40, sizing=5.0)
    action.submit_to_broker = True
    return action._submit_option_order(_straddle_legs(), 1, 6.0, strategy)


EARNINGS_REC = {EARNINGS_STAMP_NAMESPACE: {"days_to_earnings": 2,
                                           "event_date": EVENT.isoformat(),
                                           "event_time": "amc"}}


def test_an_earnings_entry_stamps_the_event_date():
    acct = FakeAccount()
    res = _submit(acct, EARNINGS_REC)
    assert res["success"], res["message"]
    assert res["data"][ORDER_EVENT_DATE_KEY] == EVENT.isoformat()


def test_the_stamp_reaches_the_stored_order_row_THE_WHITELIST():
    """THE test. ``_submit_option_order`` persists a NAMED TUPLE of keys onto the order row;
    a key present in the result ``data`` but missing from that tuple never reaches the ORDER,
    which is the only thing ``DaysAfterEventCondition`` can read. Mutation target: drop
    ``ORDER_EVENT_DATE_KEY`` from ``entry_facts`` and every log still shows the stamp while
    the exit gene goes permanently inert."""
    from ba2_common.core.db import add_instance, get_instance
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderStatus, OrderType

    row = TradingOrder(account_id=1, symbol="XYZ", quantity=1, side=OrderDirection.BUY,
                       order_type=OrderType.MARKET, status=OrderStatus.PENDING)
    row_id = add_instance(row)

    acct = FakeAccount()
    acct.next_order_id = row_id
    res = _submit(acct, EARNINGS_REC)
    assert res["success"], res["message"]

    stored = get_instance(TradingOrder, row_id)
    assert stored.data[ORDER_EVENT_DATE_KEY] == EVENT.isoformat()
    # the neighbour it rides beside, so the seam itself is identified
    assert "max_loss_per_contract" in stored.data


def test_the_stored_stamp_is_exactly_what_the_exit_condition_reads_back():
    """End to end across the seam: submit, then evaluate ``days_after_event`` against the
    STORED row on the day after the event. 2024-06-03 print, 2024-06-04 bar -> 1."""
    from ba2_common.core.db import add_instance, get_instance
    from ba2_common.core.TradeConditions import DaysAfterEventCondition
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderStatus, OrderType

    row_id = add_instance(TradingOrder(
        account_id=1, symbol="XYZ", quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.PENDING))
    acct = FakeAccount()
    acct.next_order_id = row_id
    assert _submit(acct, EARNINGS_REC)["success"]

    class _NextDayBar:
        id = 1

        def _as_of_date(self):
            return date(2024, 6, 4)

    cond = DaysAfterEventCondition(
        account=_NextDayBar(), instrument_name="XYZ",
        expert_recommendation=_rec(None), operator_str=">=", value=1.0,
        existing_order=get_instance(TradingOrder, row_id))
    assert cond.evaluate() is True
    assert cond.calculated_value == 1


@pytest.mark.parametrize("rec_data", [
    None,
    {},
    {"FMPRating": {"target_low": 90.0}},
    {EARNINGS_STAMP_NAMESPACE: {}},
    {EARNINGS_STAMP_NAMESPACE: {"days_to_earnings": 2}},          # days, no date
    {EARNINGS_STAMP_NAMESPACE: {"event_date": "not-a-date"}},
    {EARNINGS_STAMP_NAMESPACE: {"event_date": None}},
], ids=["none", "empty", "other-expert", "empty-payload", "days-only", "garbage-date",
        "null-date"])
def test_a_non_earnings_entry_stamps_NOTHING(rec_data):
    """ABSENCE IS THE GATE. Every equity trade and every option trade from another expert
    reaches this same choke point; a defaulted date here would arm ``days_after_event`` on
    all of them. The garbage/None cases matter as much as the missing ones: a date that
    cannot be parsed must leave NO key rather than a key that parses to nothing later."""
    acct = FakeAccount()
    res = _submit(acct, rec_data)
    assert res["success"], res["message"]
    assert ORDER_EVENT_DATE_KEY not in res["data"]


def test_a_non_earnings_entry_leaves_the_order_row_without_the_key():
    from ba2_common.core.db import add_instance, get_instance
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderStatus, OrderType

    row_id = add_instance(TradingOrder(
        account_id=1, symbol="XYZ", quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.PENDING))
    acct = FakeAccount()
    acct.next_order_id = row_id
    assert _submit(acct, {"FMPRating": {"target_low": 90.0}})["success"]

    stored = get_instance(TradingOrder, row_id)
    assert ORDER_EVENT_DATE_KEY not in (stored.data or {})


def test_the_stamp_costs_a_pure_equity_trial_nothing_STRUCTURAL():
    """PERFORMANCE ACCEPTANCE CRITERION 1: option work must add ZERO per-bar/per-action work
    to a pure-equity trial. Argued structurally rather than from wall clock, because the
    statement is about REACHABILITY: every reference to the event stamp in ``TradeActions``
    sits inside ``_OptionEntryAction._submit_option_order``, which no equity order path
    reaches. An equity trial therefore executes none of it -- not once per bar, not once per
    order -- and the RESULT-level consequence is pinned by the equity golden run's
    fingerprint, which this task leaves untouched.

    Counted from source rather than asserted in prose: if a later change adds a reference
    from an equity path (or from a per-bar loop), the count moves and this fails.

    WHAT THIS PIN CANNOT SEE, stated so nobody reads it as stronger than it is:

    * ALIASING. It counts TOKENS. ``_sed = stamped_event_date`` at module scope, or
      ``from ba2_common.core import earnings_stamp`` + ``earnings_stamp.stamped_event_date``,
      reaches the same function under a name this test does not count -- the counts hold
      and an equity path could call it.
    * COMMENT BRITTLENESS, both directions. Writing either name in a COMMENT or docstring
      raises the count and fails this test for no behavioural reason; deleting a real call
      and adding a comment mentioning it keeps the count and passes. It is a proxy for
      reachability, not a call graph.

    It is kept because the cheap thing it does catch -- a new call site pasted somewhere
    else in this 5,000-line module -- is the realistic regression, and because the
    RESULT-level guarantee does not depend on it: the equity golden run's fingerprint
    (testplatform/backend/tests/backtest/test_equity_golden_run.py) is what actually
    proves an equity trial is untouched, byte for byte.
    """
    import inspect

    from ba2_common.core import TradeActions as TA

    module_src = inspect.getsource(TA)
    submit_src = inspect.getsource(TA._OptionEntryAction._submit_option_order)

    for token, module_expected, submit_expected in (("stamped_event_date", 2, 1),
                                                    ("ORDER_EVENT_DATE_KEY", 3, 2)):
        # module count includes the single import line
        assert module_src.count(token) == module_expected, (
            f"{token} appears {module_src.count(token)} times in TradeActions "
            f"(expected {module_expected}: the import plus its use(s) inside "
            f"_submit_option_order) -- a new reference may sit on an equity path")
        assert submit_src.count(token) == submit_expected, (
            f"{token} appears {submit_src.count(token)} times inside "
            f"_submit_option_order (expected {submit_expected})")
