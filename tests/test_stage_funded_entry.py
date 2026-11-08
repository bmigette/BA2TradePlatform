"""``stage_funded_entry`` (``ba2_common.core.trade_cycle``): the funded-entry composition up to the broker.

Task 7 of the stock decision-pass replay design: the body of the funded-entry loop of
``TradeManager.process_expert_recommendations_after_analysis`` -- execute the evaluator, find the main
order, write the RM-sized quantity and safeguard stop onto it, stamp the protective legs -- is ONE shared
function that TradeManager and the offline decision replay both call.

* The CHARACTERISATION tests drive the real loop and pin what lands on disk (they were written against the
  inline code first and must pass unchanged on the extracted one).
* The DIRECT tests call the function itself.

The broker submit (with its DB-lock retry and the unsent-entry compensation) stays in TradeManager: it is
the live-only tail.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlmodel import select

from ba2_common.core.db import get_db
from ba2_common.core.models import TradingOrder
from ba2_common.core.types import OrderDirection, OrderStatus, OrderType

from tests import factories
from tests.test_funded_entry_loop import (  # noqa: F401  (file_db is a fixture)
    _FundedEntryAccount, _make_scenario, _orders, _run_enter, _transactions, file_db,
)


# --------------------------------------------------------------------------------------------------
# characterisation: the live loop, observed from outside
# --------------------------------------------------------------------------------------------------
def test_the_loop_stages_the_rm_decision_on_disk_for_every_funded_entry_then_submits_once_each(file_db):
    _acct, inst, expert = _make_scenario(["AAPL", "MSFT"])
    account = _FundedEntryAccount(_acct.id, probe=file_db)

    created = _run_enter(expert, account, inst.id)

    assert [o.symbol for o in created] == ["AAPL", "MSFT"]
    assert [s["symbol"] for s in account.submits] == ["AAPL", "MSFT"]          # once each, in funded order
    assert account.disk_qty_at_submit == {"AAPL": 400.0, "MSFT": 400.0}        # persisted BEFORE the broker
    assert account.disk_stop_at_submit == {
        symbol: account.submits[i]["sl_price"] for i, symbol in enumerate(["AAPL", "MSFT"])}
    for symbol in ("AAPL", "MSFT"):
        entry = [o for o in _orders(symbol) if o.order_type == OrderType.MARKET][0]
        legs = [o for o in _orders(symbol) if o.depends_on_order == entry.id]
        assert entry.quantity == 400.0 and entry.stop_price
        assert legs and all(leg.quantity == 400.0 for leg in legs)
        assert len(_transactions(symbol)) == 1


def _patched_execute(failing_symbol, outcome):
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

    real_execute = TradeActionEvaluator.execute

    def execute(self, *args, **kwargs):
        if self.instrument_name == failing_symbol:
            return outcome()
        return real_execute(self, *args, **kwargs)

    return patch.object(TradeActionEvaluator, "execute", execute)


def test_an_execute_that_creates_no_main_order_submits_nothing_and_does_not_stop_the_pass(file_db):
    """execute() answering without an order (nothing to submit) is logged and skipped; the next candidate
    is still staged and submitted."""
    _acct, inst, expert = _make_scenario(["AAPL", "MSFT"])
    account = _FundedEntryAccount(_acct.id, probe=file_db)

    with _patched_execute("AAPL", lambda: []):
        created = _run_enter(expert, account, inst.id)

    assert [s["symbol"] for s in account.submits] == ["MSFT"]
    assert [o.symbol for o in created] == ["MSFT"]
    assert _orders("AAPL") == []


def test_an_execute_that_raises_is_contained_to_its_candidate(file_db):
    _acct, inst, expert = _make_scenario(["AAPL", "MSFT"])
    account = _FundedEntryAccount(_acct.id, probe=file_db)

    def boom():
        raise RuntimeError("execute exploded")

    with _patched_execute("AAPL", boom):
        created = _run_enter(expert, account, inst.id)

    assert [s["symbol"] for s in account.submits] == ["MSFT"]
    assert [o.symbol for o in created] == ["MSFT"]


# --------------------------------------------------------------------------------------------------
# the extracted function
# --------------------------------------------------------------------------------------------------
class _Evaluator:
    """execute() creates a PENDING entry (quantity 0, like the candidate) with two protective legs."""

    def __init__(self, account_id, symbol="AAPL", create=True, raises=None):
        self.account_id, self.symbol, self.create, self.raises = account_id, symbol, create, raises

    def execute(self):
        if self.raises:
            raise self.raises
        if not self.create:
            return [{"success": True, "data": {}}, {"success": False, "data": {"order_id": 99999}}]
        entry = factories.create_trading_order(
            account_id=self.account_id, symbol=self.symbol, quantity=0.0, status=OrderStatus.PENDING,
            side=OrderDirection.BUY)
        for leg_type in (OrderType.SELL_STOP, OrderType.SELL_LIMIT):
            factories.create_trading_order(
                account_id=self.account_id, symbol=self.symbol, quantity=0.0,
                status=OrderStatus.WAITING_TRIGGER, side=OrderDirection.SELL, order_type=leg_type,
                depends_on_order=entry.id, depends_order_status_trigger=OrderStatus.FILLED)
        return [{"success": True, "data": {"order_id": entry.id}}]


def _row(order_id):
    with get_db() as session:
        return session.get(TradingOrder, order_id)


def test_stage_funded_entry_persists_the_rm_quantity_stop_and_leg_quantities(file_db):
    from ba2_common.core.trade_cycle import stage_funded_entry

    acct = factories.create_account_definition(provider="MockAccount")
    staged = stage_funded_entry(_Evaluator(acct.id), "AAPL", 7.0, 93.0)

    assert staged.order is not None and staged.results[0]["success"]
    row = _row(staged.order.id)
    assert (row.quantity, row.stop_price) == (7.0, 93.0)
    with get_db() as session:
        legs = list(session.exec(select(TradingOrder).where(TradingOrder.depends_on_order == row.id)).all())
    assert len(legs) == 2 and all(leg.quantity == 7.0 for leg in legs)


def test_stage_funded_entry_reports_the_results_before_it_persists(file_db):
    """The recorder's step needs the execute results (and the order) even when a later part fails."""
    from ba2_common.core.trade_cycle import stage_funded_entry

    acct = factories.create_account_definition(provider="MockAccount")
    seen = []

    def on_executed(results, order):
        seen.append((results, order, _row(order.id).quantity))

    stage_funded_entry(_Evaluator(acct.id), "AAPL", 7.0, 93.0, on_executed=on_executed)

    ((results, order, quantity_on_disk),) = seen
    assert results[0]["data"]["order_id"] == order.id and quantity_on_disk == 0.0


def test_stage_funded_entry_without_a_main_order_returns_none_and_still_reports(file_db):
    from ba2_common.core.trade_cycle import stage_funded_entry

    acct = factories.create_account_definition(provider="MockAccount")
    seen = []
    staged = stage_funded_entry(_Evaluator(acct.id, create=False), "AAPL", 7.0, 93.0,
                                on_executed=lambda results, order: seen.append(order))

    assert staged.order is None and seen == [None]


def test_stage_funded_entry_lets_an_execute_failure_through(file_db):
    from ba2_common.core.trade_cycle import stage_funded_entry

    acct = factories.create_account_definition(provider="MockAccount")
    with pytest.raises(RuntimeError, match="boom"):
        stage_funded_entry(_Evaluator(acct.id, raises=RuntimeError("boom")), "AAPL", 7.0, 93.0)


def test_a_stop_the_ruleset_put_on_the_order_is_not_overwritten(file_db):
    from ba2_common.core.trade_cycle import persist_funded_entry

    acct = factories.create_account_definition(provider="MockAccount")
    order = factories.create_trading_order(account_id=acct.id, symbol="WSC", quantity=0.0,
                                           status=OrderStatus.PENDING, stop_price=88.0)
    persist_funded_entry(order, 7.0, 93.0)
    assert (_row(order.id).quantity, _row(order.id).stop_price) == (7.0, 88.0)
    persist_funded_entry(order, 8.0, None)
    assert (_row(order.id).quantity, _row(order.id).stop_price) == (8.0, 88.0)
