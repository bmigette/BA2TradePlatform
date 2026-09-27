"""The Risk Manager run dialog shows a classic-mode option entry as an option."""
from types import SimpleNamespace

from ba2_common.core import risk_manager_run as rmr
from ba2_trade_platform.ui.pages.marketanalysis import (
    classic_run_detail_rows, classic_run_option_legend,
)


def _option_row():
    record = {"legs": [{"contract_symbol": "GILD261120C00150000", "side": "buy", "ratio_qty": 1,
                        "right": "call", "strike": 150.0, "expiry": "2026-11-20", "dte": 54,
                        "delta": 0.35, "iv": 0.31, "mid": 3.1, "bid": 3.0, "ask": 3.2,
                        "open_interest": 1200, "volume": 85}],
              "structure": {"strategy": "long_call", "quantity": 2, "multiplier": 100,
                            "net_price": 3.1, "max_loss": 310.0, "max_profit": None,
                            "breakevens": [153.1]}}
    return rmr.option_entry_decision("GILD", {"success": True,
                                              "data": {"order_id": 42, "entry_record": record}})


def test_an_option_row_reads_as_contracts_with_the_contract_and_its_quote():
    equity = rmr.decision("KO", rmr.OUTCOME_FUNDED, "funded at 16", quantity=16, rank=1,
                          cost=1421.6)
    rows = classic_run_detail_rows([equity, _option_row()])

    assert [r["symbol"] for r in rows] == ["KO", "GILD · long_call"]   # after the ranked rows
    opt = rows[1]
    assert opt["quantity"] == "2 ct"
    assert opt["size"] == "620.00"
    assert "C 150 2026-11-20" in opt["reason"]
    assert "bid 3.00 / ask 3.20" in opt["qty_detail"] and "IV 31%" in opt["qty_detail"]
    assert "OI 1,200" in opt["qty_detail"] and "max loss $310.00/ct" in opt["qty_detail"]
    assert "breakeven 153.10" in opt["qty_detail"]
    assert rows[0]["quantity"] == "16"


def test_the_option_legend_appears_only_with_option_rows():
    assert classic_run_option_legend([_option_row()])
    assert classic_run_option_legend([rmr.decision("KO", rmr.OUTCOME_FUNDED, "x", quantity=1)]) == ""


def test_an_expert_on_the_option_risk_manager_is_not_recorded_twice(monkeypatch):
    from ba2_trade_platform.core.TradeManager import TradeManager
    import ba2_common.core.OptionRiskManagement as orm
    mgr = object.__new__(TradeManager)
    mgr.logger = SimpleNamespace(warning=lambda *a, **k: None)
    result = {"action_type": "buy_call", "success": False, "message": "no contract", "data": {}}

    monkeypatch.setattr(orm, "option_risk_manager_enabled", lambda s, expert_instance_id=None: True)
    assert mgr._option_entry_decisions(SimpleNamespace(settings={}), 9, "GILD", [result]) == []

    monkeypatch.setattr(orm, "option_risk_manager_enabled", lambda s, expert_instance_id=None: False)
    rows = mgr._option_entry_decisions(SimpleNamespace(settings={}), 9, "GILD", [result])
    assert [r["outcome"] for r in rows] == [rmr.OUTCOME_OPTION_NOT_PLACED]


def test_a_non_entry_result_in_the_same_pass_is_not_a_row(monkeypatch):
    from ba2_trade_platform.core.TradeManager import TradeManager
    import ba2_common.core.OptionRiskManagement as orm
    monkeypatch.setattr(orm, "option_risk_manager_enabled", lambda s, expert_instance_id=None: False)
    mgr = object.__new__(TradeManager)
    mgr.logger = SimpleNamespace(warning=lambda *a, **k: None)

    rows = mgr._option_entry_decisions(SimpleNamespace(settings={}), 9, "GILD", [
        {"action_type": "adjust_take_profit", "success": True, "data": {}}])

    assert rows == []
