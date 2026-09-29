"""Classic-mode option entries in the classic risk-manager run record.

Option entries size and submit themselves, so the classic manager never sized them and its
run used to say nothing about them. They are now rows of the same pass's run.
"""
import pytest

from ba2_common.core import risk_manager_run as rmr
from ba2_common.core import TradeRiskManagement as trm


def _record(net=3.1, qty=2):
    leg = {"contract_symbol": "GILD261120C00150000", "side": "buy", "ratio_qty": 1,
           "right": "call", "strike": 150.0, "expiry": "2026-11-20", "dte": 54,
           "delta": 0.35, "iv": 0.31, "mid": 3.1, "bid": 3.0, "ask": 3.2,
           "open_interest": 1200, "volume": 85}
    return {"version": "option_trade_record_v1", "legs": [leg], "legs_without_quote": [],
            "structure": {"strategy": "long_call", "quantity": qty, "multiplier": 100,
                          "net_price": net, "max_loss": 310.0, "max_profit": None,
                          "breakevens": [153.1]}}


def test_a_placed_entry_is_funded_in_contracts_with_the_contract_named():
    row = rmr.option_entry_decision("GILD", {
        "success": True, "message": "Submitted long_call for GILD",
        "data": {"order_id": 42, "entry_record": _record()}})

    assert row["outcome"] == rmr.OUTCOME_FUNDED
    assert row["quantity"] == 2.0
    assert row["asset_class"] == "option"
    assert row["cost"] == pytest.approx(620.0)
    assert row["premium_side"] == "debit"
    assert row["legs"][0]["strike"] == 150.0
    assert "long_call x2: BUY 1x C 150 2026-11-20 (54 DTE, delta 0.35, mid 3.10)" in row["reason"]
    assert "debit $620.00" in row["reason"]


def test_a_credit_structure_says_credit():
    row = rmr.option_entry_decision("GILD", {
        "success": True, "data": {"order_id": 1, "entry_record": _record(net=-1.25, qty=1)}})

    assert row["premium_side"] == "credit" and row["cost"] == pytest.approx(125.0)


def test_an_entry_that_placed_nothing_is_a_refusal_with_its_own_message():
    row = rmr.option_entry_decision("GILD", {
        "success": False, "message": "no contract in the 40-60 DTE / 0.35 delta box",
        "data": {}})

    assert row["outcome"] == rmr.OUTCOME_OPTION_NOT_PLACED
    assert row["outcome"] in rmr.REFUSED_OUTCOMES
    assert row["reason"] == "no contract in the 40-60 DTE / 0.35 delta box"
    assert "quantity" not in row


def test_success_without_an_order_is_not_counted_as_funded():
    row = rmr.option_entry_decision("GILD", {"success": True, "message": "preview", "data": {}})

    assert row["outcome"] == rmr.OUTCOME_OPTION_NOT_PLACED


# ---------------------------------------------------------------------------
# The manager writes them
# ---------------------------------------------------------------------------

@pytest.fixture
def recorded(monkeypatch):
    runs = []
    monkeypatch.setattr(rmr, "record_run", lambda **kw: runs.append(kw) or 1)
    monkeypatch.setattr(trm, "get_instance", lambda model, _id: type("EI", (), {"account_id": 5})())
    import ba2_common.core.trade_store as ts
    monkeypatch.setattr(ts, "inmem_trades_active", lambda: False)
    return runs


def _manager():
    import logging
    mgr = object.__new__(trm.TradeRiskManagement)
    mgr.logger = logging.getLogger("test")
    return mgr


def test_a_pass_with_only_option_entries_still_writes_a_run(recorded):
    rows = [rmr.option_entry_decision("GILD", {
        "success": True, "data": {"order_id": 42, "entry_record": _record()}})]

    assert _manager().size_candidate_orders(9, [], option_decisions=rows) == []

    assert len(recorded) == 1
    run = recorded[0]
    assert run["mode"] == rmr.MODE_CLASSIC and run["account_id"] == 5
    assert run["decisions"] == rows


def test_no_option_entries_and_no_candidates_writes_nothing(recorded):
    assert _manager().size_candidate_orders(9, []) == []
    assert recorded == []


def test_the_option_rows_follow_the_equity_rows_of_the_same_run(recorded, monkeypatch):
    mgr = _manager()
    equity = [rmr.decision("KO", rmr.OUTCOME_FUNDED, "funded at 16", quantity=16, rank=1)]
    monkeypatch.setattr(mgr, "_build_run_decisions", lambda **kw: equity)
    option = rmr.option_entry_decision("GILD", {"success": False, "message": "no contract"})

    mgr._record_candidate_run(
        expert_instance_id=9, account_id=5, started_at=None, candidates=[],
        dropped_by_permission=[], orders_to_update=[], orders_to_delete=[],
        symbol_prices={}, context={}, extra_decisions=[option])

    assert [d["symbol"] for d in recorded[0]["decisions"]] == ["KO", "GILD"]
