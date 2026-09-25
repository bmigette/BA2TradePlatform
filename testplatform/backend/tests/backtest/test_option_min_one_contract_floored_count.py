"""The run report counts entries that exist only because of the 1-contract sizing floor
(``min_one_contract``, plan 2026-09-24 Task 8): ``option_min_one_contract_floored_entries``.

A genome whose result rests on floored tickets is a different bet from one sized by its own
budget, and without this count the two are indistinguishable in the results. The count is
read off the ORDER ROWS, where the SHARED entry path stamps ``min_one_contract_floor`` -- so
these tests drive a real ``BuyCallAction`` against a real ``BacktestAccount``, proving the
stamp reaches the row and the row reaches the summary.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from tests.backtest.test_backtest_account_options import (  # noqa: F401  (fixtures)
    _results_config, backtest_account_no_options, options_account,
)


def _buy_call(acct, *, floor):
    from ba2_common.core.TradeActions import create_action
    from ba2_common.core.types import ExpertActionType

    rec = SimpleNamespace(id=None, instance_id=None, data=None, price_at_date=181.0,
                          expected_profit_percent=None, recommended_action=None,
                          confidence=80.0, created_at=datetime(2024, 3, 5))
    # $100k x 0.1% = $100 < one AAPL 180 call at 3.20 ($320) -> 0 contracts without the floor.
    a = create_action(ExpertActionType.BUY_CALL, "AAPL", acct, SimpleNamespace(), None, rec,
                      strike_method="percent_otm", strike_param=0.0, dte_min=5, dte_max=15,
                      sizing=0.1, min_one_contract=floor)
    a.submit_to_broker = True
    # The per-instrument cap and the commitment on the name are unit-tested in
    # packages/common; here they are pinned so the test is about the ROW and the SUMMARY.
    a._max_equity_per_instrument_cap = lambda equity: equity * 0.10
    a._committed_to_underlying = lambda: (0.0, None)
    # No ExpertRecommendation row behind this action, so the TradeActionResult (whose FK is
    # NOT NULL) is returned rather than persisted; the ORDER row is what this file is about.
    a.create_and_save_action_result = lambda **kw: {
        "success": kw["success"], "message": kw["message"], "data": kw["data"]}
    return a


def test_a_floored_entry_is_stamped_on_its_order_row_and_counted(options_account):
    from app.services.backtest.results import build_results

    assert options_account.option_min_one_contract_floored_entries() == 0
    res = _buy_call(options_account, floor=True).execute()
    assert res["success"], res["message"]
    assert res["data"]["quantity"] == 1 and res["data"]["min_one_contract_floor"] is True

    assert options_account.option_min_one_contract_floored_entries() == 1
    options_account.snapshot_equity(datetime(2024, 3, 5))
    out = build_results(options_account, {**_results_config(), "option_trade_records": True})
    assert out["option_min_one_contract_floored_entries"] == 1


def test_an_entry_sized_by_its_own_budget_is_not_counted(options_account):
    """Flag off, the same entry is refused (0 contracts) and nothing is counted."""
    from app.services.backtest.results import build_results

    res = _buy_call(options_account, floor=False).execute()
    assert not res["success"] and "Insufficient budget" in res["message"]
    options_account.snapshot_equity(datetime(2024, 3, 5))
    out = build_results(options_account, {**_results_config(), "option_trade_records": True})
    assert out["option_min_one_contract_floored_entries"] == 0


def test_an_equity_run_gains_no_key(backtest_account_no_options):
    from app.services.backtest.results import build_results

    backtest_account_no_options.snapshot_equity(datetime(2024, 3, 5))
    out = build_results(backtest_account_no_options, _results_config())
    assert "option_min_one_contract_floored_entries" not in out
