"""E1b, LIVE: the funded-entry RM never spends the cash an open cash-secured put is secured by.

The live half of ``testplatform/backend/tests/backtest/test_csp_collateral_rm_budget.py``. The
expert holds an open CSP (reserve = strike x 100 x contracts on its entry row, exactly what the
option entry action persists) and the funded-entry loop sizes a stock BUY through the classic
RM. Both runtimes size from the SHARED ``option_collateral_budget.rm_equity_budget``.

THE DOUBLE-SUBTRACTION PIN. Live Alpaca's snapshot ``buying_power`` is the Reg-T REMAINING
buying power, which the broker has ALREADY reduced by the collateral of every open CSP. The
second test publishes such a net figure and asserts the budget is charged the collateral once,
not twice.

Reuses the real funded-entry harness of ``tests/test_funded_entry_loop.py`` (file-backed DB,
the real ``AccountInterface.submit_order``, only the broker call doubled).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

from ba2_common.core.types import (AnalysisUseCase, AssetClass, ExpertActionType, OptionRight,
                                   OrderDirection, OrderRecommendation, OrderStatus, OrderType,
                                   TransactionStatus)

from tests import factories
from tests.test_funded_entry_loop import (  # noqa: F401  (file_db is a fixture)
    FROZEN_NOW, _EnterExpert, _FundedEntryAccount, _run_enter, file_db)

PRICE = 74.50
#: 40% of $100k = $40,000 per instrument -> 536 shares when the collateral is ignored.
CAP_PCT = 40.0
PUT = "MSFT260116P00350000"
CONTRACTS = 2
PREMIUM = 1.00
#: strike 350 x 100 x 2 contracts: leaves ~$30k of the $100k for stock.
RESERVE = 350.0 * 100.0 * CONTRACTS


def _scenario():
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction, TradingOrder

    acct_def = factories.create_account_definition(provider="MockAccount")
    rs = factories.create_ruleset(name="enter-csp-budget", subtype=AnalysisUseCase.ENTER_MARKET)
    ea = factories.create_event_action(
        name="buy", subtype=AnalysisUseCase.ENTER_MARKET,
        triggers={"trigger_0": {"event_type": "bullish"}},
        actions={"action_0": {"action_type": ExpertActionType.BUY.value}})
    factories.link_rule_to_ruleset(rs.id, ea.id, 0)
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_EnterExpert", virtual_equity_pct=100.0,
        enter_market_ruleset_id=rs.id)
    expert = _EnterExpert(inst.id)
    expert.save_settings({
        "allow_automated_trade_opening": (True, "bool"),
        "enable_buy": (True, "bool"),
        "enable_sell": (False, "bool"),
        "max_virtual_equity_per_instrument_percent": (CAP_PCT, "float"),
    })
    factories.create_recommendation(
        instance_id=inst.id, symbol="AAPL", recommended_action=OrderRecommendation.BUY,
        expected_profit_percent=30.0, price_at_date=100.0, confidence=90.0,
        created_at=FROZEN_NOW)
    # The expert's OPEN cash-secured put, shaped as the option entry action persists it.
    txn_id = add_instance(Transaction(
        symbol="MSFT", quantity=CONTRACTS, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, open_price=PREMIUM, multiplier=100.0,
        open_date=datetime(2026, 1, 2, tzinfo=timezone.utc), expert_id=inst.id))
    add_instance(TradingOrder(
        account_id=acct_def.id, symbol="MSFT", quantity=CONTRACTS, filled_qty=CONTRACTS,
        side=OrderDirection.SELL, order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        transaction_id=txn_id, asset_class=AssetClass.OPTION, contract_symbol=PUT,
        option_type=OptionRight.PUT, strike=350.0, expiry=date(2026, 1, 16),
        underlying_symbol="MSFT", option_strategy="cash_secured_put", multiplier=100,
        data={"option_reserve": RESERVE}))
    return acct_def, inst, expert


def _run(file_db, broker_buying_power=None):
    """Drive the live loop; return (the budgets the RM sized from, the broker submits)."""
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement

    acct, inst, expert = _scenario()
    account = _FundedEntryAccount(acct.id, probe=file_db)
    account._prices["AAPL"] = PRICE
    account._prices["MSFT"] = 400.0
    if broker_buying_power is not None:
        info = account.get_account_info()
        account.get_account_info = lambda: {**info, "buying_power": broker_buying_power}

    budgets = []
    real = TradeRiskManagement._calculate_order_quantities

    def spy(self, prioritized, total_virtual_balance, *a, **k):
        budgets.append(total_virtual_balance)
        return real(self, prioritized, total_virtual_balance, *a, **k)

    with patch.object(TradeRiskManagement, "_calculate_order_quantities", spy):
        _run_enter(expert, account, inst.id)
    return budgets, account.submits


def test_the_live_rm_budget_excludes_the_open_csps_collateral(file_db):
    budgets, submits = _run(file_db)
    assert budgets, "the stock entry never reached the RM"
    assert budgets[0] <= 100_000.0 - RESERVE + 1e-6, (
        f"the live RM budget ${budgets[0]:,.2f} includes the ${RESERVE:,.0f} the put is "
        f"secured by")
    assert [s["quantity"] for s in submits] == [int(budgets[0] // PRICE)], (
        f"the budget, not the $40k per-instrument cap, must size the entry: {submits}")
    assert submits[0]["quantity"] < 536


def test_a_reg_t_buying_power_that_already_nets_the_collateral_is_not_charged_twice(file_db):
    """Alpaca's Reg-T buying power: cash + premium - the collateral it holds."""
    reg_t = 100_000.0 + PREMIUM * 100.0 * CONTRACTS - RESERVE
    net_budgets, _ = _run(file_db)
    budgets, submits = _run(file_db, broker_buying_power=reg_t)
    assert budgets[0] == min(net_budgets[0], reg_t), (
        f"budget ${budgets[0]:,.2f}: the broker figure ${reg_t:,.2f} already nets the "
        f"collateral and must not be charged it again")
    assert budgets[0] > reg_t - RESERVE + 1.0, "the collateral was subtracted twice"
    assert submits and submits[0]["quantity"] == int(budgets[0] // PRICE)
