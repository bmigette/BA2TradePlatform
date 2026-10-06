"""The option capital limit in the BACKTEST (owner rule, 2026-10-06; shared half in
``packages/common/tests/test_option_capital_limit.py``): the debits paid plus the collateral
reserved by every open option structure never exceed the account's cash.

Two halves, both driven through the real engine / account:
  * the SHARED sizing seam cuts a debit entry decided in the same session as others to what is
    still uncommitted (so the fill-time cash cap is a backstop, not the mechanism);
  * the fill-time cap itself reads the cash LESS the reserved collateral -- it used to read the
    raw ledger cash and could spend a cash-secured put's collateral.
"""
from __future__ import annotations

import logging

import pytest

from ba2_common.core.db import update_instance
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus
from datetime import date

from tests.backtest._structure_engine import build_account, model_price, occ, run_structure

START_CASH = 100_000.0


def _leg(sym, strike, side, intent, right=OptionRight.CALL):
    return OptionLeg(contract_symbol=sym, side=side, ratio_qty=1, position_intent=intent,
                     option_type=right, strike=strike, expiry=date(2024, 3, 15),
                     underlying="AAPL")


def test_a_debit_fill_cannot_spend_the_collateral_a_cash_secured_put_reserved():
    """A cash-secured put reserving 90k is open. A 50-contract call buy would cost 32.5k, but
    only the 10.6k above the reserve is spendable: the fill is cut, and the account never holds
    less cash than it has reserved."""
    put, call = occ("P", 180.0), occ("C", 180.0)
    acct, ps, ctx, _ = build_account()
    try:
        csp = acct.submit_option_order(
            legs=[_leg(put, 180.0, OrderDirection.SELL, "sell_to_open", OptionRight.PUT)],
            quantity=1, order_type="market", option_strategy="cash_secured_put")
        csp.data = {"option_reserve": 90_000.0}
        update_instance(csp)
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct._option_positions[put].qty == -1
        assert acct.reserved_option_buying_power() == pytest.approx(90_000.0)
        cash_before = acct._cash
        assert cash_before > START_CASH            # the put's premium came in

        order = acct.submit_option_order(
            legs=[_leg(call, 180.0, OrderDirection.BUY, "buy_to_open")], quantity=50,
            order_type="market", option_strategy="long_call")
        acct.refresh_orders()
        acct.refresh_transactions()

        cost = model_price("C", 180.0, date(2024, 2, 2)) * 100.0
        spendable = cash_before - 90_000.0
        held = acct._option_positions[call].qty
        assert 0 < held < 50
        assert held == int(spendable // cost)
        assert acct._cash >= acct.reserved_option_buying_power() - 1e-6
        assert acct.get_order(order.id).status == OrderStatus.FILLED
    finally:
        ctx.__exit__(None, None, None)


def test_a_debit_structure_that_cannot_fit_the_reserve_does_not_open():
    """Not even one structure fits above the reserve: the entry is cancelled, no lot, cash
    untouched (the fill-time cap's existing refusal, now measured against the reserve)."""
    put, call = occ("P", 180.0), occ("C", 180.0)
    acct, ps, ctx, _ = build_account()
    try:
        csp = acct.submit_option_order(
            legs=[_leg(put, 180.0, OrderDirection.SELL, "sell_to_open", OptionRight.PUT)],
            quantity=1, order_type="market", option_strategy="cash_secured_put")
        csp.data = {"option_reserve": START_CASH}          # every dollar is collateral
        update_instance(csp)
        acct.refresh_orders()
        acct.refresh_transactions()
        cash_before = acct._cash
        acct.submit_option_order(
            legs=[_leg(call, 180.0, OrderDirection.BUY, "buy_to_open")], quantity=3,
            order_type="market", option_strategy="long_call")
        acct.refresh_orders()
        assert acct._option_positions.get(call) is None or acct._option_positions[call].qty == 0
        assert acct._cash == pytest.approx(cash_before)
    finally:
        ctx.__exit__(None, None, None)


def test_same_session_debit_entries_never_commit_more_than_the_account_holds(caplog):
    """Five names each sized at 60 % of the account, decided on the same bar. The shared seam
    sizes each against what the earlier ones will still pay, so the sum paid stays inside the
    account -- and the fill-time cash cap never has to scale an order silently."""
    with caplog.at_level(logging.ERROR):
        _, acct, ctx, _ = run_structure("buy_call", sizing=60.0,
                                        symbols=tuple(f"S{i}" for i in range(5)))
    try:
        paid = sum(p.qty * p.avg_price * p.multiplier
                   for p in acct._option_positions.values() if p.qty > 0)
        assert 0.0 < paid <= START_CASH
        assert acct._cash >= -1e-6
        assert not [r for r in caplog.records if "capping to" in r.getMessage()
                    or "NOT opened" in r.getMessage()], \
            "the fill-time cash cap had to cut an order the sizing seam should have fitted"
    finally:
        ctx.__exit__(None, None, None)


def test_the_backtests_headroom_is_equity_less_the_cost_of_what_it_holds():
    """The backtest publishes its equity in the snapshot, and its open option transaction carries
    its cost: headroom = equity - cost. With marks at cost that is the cash left -- the figure a
    live account (balance = equity) reaches by the SAME subtraction."""
    call = occ("C", 180.0)
    acct, ps, ctx, _ = build_account()
    try:
        acct.submit_option_order(
            legs=[_leg(call, 180.0, OrderDirection.BUY, "buy_to_open")], quantity=10,
            order_type="market", option_strategy="long_call")
        acct.refresh_orders()
        acct.refresh_transactions()
        # the headroom reads what the account's EXPERTS hold (the stock path's own scope): the
        # engine always has one; this direct submission has none, so attach it to one.
        from ba2_common.core.db import add_instance
        from ba2_common.core.models import ExpertInstance
        from ba2_common.core.trade_store import transactions_where
        expert_id = add_instance(ExpertInstance(account_id=acct.id, expert="Stub"))
        for txn in transactions_where():
            txn.expert_id = expert_id
            update_instance(txn)
        cost = 10 * model_price("C", 180.0, date(2024, 2, 2)) * 100.0
        assert acct.get_balance() == pytest.approx(START_CASH - cost)         # CASH fell
        assert acct.option_capital_equity() == pytest.approx(START_CASH, abs=200.0)   # equity did not (marks drift a few dollars)
        assert acct.open_position_cost_basis().total == pytest.approx(cost)
        assert acct.option_capital_headroom() == pytest.approx(START_CASH - cost, abs=200.0)
    finally:
        ctx.__exit__(None, None, None)
