"""E1b, backtest: the equity RM never spends the cash a cash-secured put is secured by.

Plan 2026-09-25 option-stage2-build, "Added after the E1 review", E1b. The CSP member of a style
run sells a put (option entry, self-sized, reserving strike x 100 per contract) and, on the same
bar, the stock member's entry goes through the classic RM. The RM's budget used to charge the
put only its premium, so it bought stock with the put's collateral. It now sizes from the SHARED
``option_collateral_budget.rm_equity_budget`` -- the function the live TradeManager's RM pass
calls too (``tests/test_csp_collateral_rm_budget_live.py``).

Runs the REAL ``DailyBacktestEngine.run()`` through the two-symbol fixture of
``test_per_entry_routing`` (OPX: the option member; CCX at $18.60: the stock member).

Run from the backend dir:
    python -m pytest tests/backtest/test_csp_collateral_rm_budget.py -q
"""
from __future__ import annotations

import pytest

from ba2_common.core.types import OrderDirection

from tests.backtest import test_per_entry_routing as R

#: The CSP member sizes big enough that its collateral leaves LESS than the RM's 10%
#: per-instrument cap ($10,000) for the stock member: the budget, not the cap, binds.
BIG_CSP = {**R.SELL_CSP, "option_sizing": 95.0}
#: No round lot: the share count is the budget itself, to the share.
BUY = {"action_type": "buy", "entry_tag": R.TAG_EQUITY}


@pytest.fixture(scope="module")
def run():
    from _pytest.monkeypatch import MonkeyPatch
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement

    budgets = []
    real = TradeRiskManagement._calculate_order_quantities

    def spy(self, prioritized, total_virtual_balance, *a, **k):
        budgets.append(total_virtual_balance)
        return real(self, prioritized, total_virtual_balance, *a, **k)

    mp = MonkeyPatch()
    mp.setattr(TradeRiskManagement, "_calculate_order_quantities", spy)
    try:
        orders, account = R._run(8711, [R._option_rule(BIG_CSP), R._equity_rule([BUY])])
    finally:
        mp.undo()
    return orders, account, budgets


def _collateral(orders):
    """What the put's entry row reserved (strike x 100 x contracts)."""
    rows = [o for o in orders if getattr(o, "option_strategy", None) == "cash_secured_put"
            and isinstance(o.data, dict) and o.data.get("option_reserve")]
    assert rows, "the CSP member reserved nothing -- the fixture proves nothing"
    return sum(float(o.data["option_reserve"]) for o in rows)


def test_the_csp_member_sold_its_put_and_reserved_its_collateral(run):
    orders, _account, _ = run
    puts = R._executed(R._option_orders(orders, R.OPX))
    assert puts and all(o.side is OrderDirection.SELL for o in puts)
    assert _collateral(orders) > 90_000.0 - 1.0, "the put must tie up most of the account"


def test_the_rm_budget_excludes_the_csp_collateral(run):
    orders, _account, budgets = run
    collateral = _collateral(orders)
    assert budgets, "the stock member never reached the RM"
    assert budgets[0] <= R.CFG["starting_cash"] - collateral + 1e-6, (
        f"the RM budget ${budgets[0]:,.2f} includes the ${collateral:,.2f} the put is "
        f"secured by")


def test_the_stock_member_buys_only_what_the_collateral_leaves(run):
    orders, _account, budgets = run
    collateral = _collateral(orders)
    shares = R._executed(R._stock_orders(orders, R.CCX))
    assert shares, "the stock member bought nothing"
    bought = shares[0].filled_qty or shares[0].quantity
    assert bought * R.CCX_SPOT <= R.CFG["starting_cash"] - collateral, (
        f"{bought} shares x ${R.CCX_SPOT} spends the put's collateral")
    assert bought == int((budgets[0]) // R.CCX_SPOT), (
        f"the budget, not the per-instrument cap, sized the entry: {bought} shares from a "
        f"${budgets[0]:,.2f} budget")
