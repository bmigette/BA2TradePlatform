"""Unit tests for OptionPortfolioManager with a stub account/expert (no DB, no
resolver): the manager is built via __new__ and wired by hand. The resolver-backed
__init__ path is FactorPortfolioManager's proven pattern and is covered by the
engine seam tests (testplatform/backend/tests/backtest/test_premium_seller_seams.py).
"""
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OrderDirection
from ba2_experts.PremiumSeller.portfolio import OptionPortfolioManager
from ba2_experts.PremiumSeller.structures import StructureSpec

SETTINGS = {
    "max_concurrent_structures": 2,
    "max_notional_leverage": 3.0,
    "undefined_risk_max_pct": 20.0,
    "max_deployment_pct": 40.0,
    "profit_capture_pct": 50.0,
    "strangle_capture_pct": 25.0,
    "roll_dte": 21,
    "tested_delta_enabled": False,
    "tested_delta": 0.30,
    "dr_stop_enabled": False,
    "dr_stop_credit_mult": 2.0,
    "ur_stop_enabled": True,
    "ur_stop_credit_mult": 2.0,
    "circuit_breaker_pct": 20.0,
}


class StubExpert:
    def get_setting_with_interface_default(self, name, log_warning=False):
        return SETTINGS[name]


class StubAccount:
    def __init__(self, balance=10_000.0):
        self._balance = balance
        self.submitted: List[Dict[str, Any]] = []

    def get_balance(self):
        return self._balance

    def submit_option_order(self, *, legs, quantity, order_type="limit", limit_price=None,
                            option_strategy=None, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append({"legs": legs, "quantity": quantity, "order_type": order_type,
                               "limit_price": limit_price, "option_strategy": option_strategy,
                               "transaction_id": transaction_id})
        return SimpleNamespace(id=len(self.submitted), transaction_id=transaction_id)


def make_manager(balance=10_000.0):
    pm = OptionPortfolioManager.__new__(OptionPortfolioManager)
    pm.expert_instance_id = 1
    pm.expert = StubExpert()
    pm.account = StubAccount(balance)
    pm._peak_equity = None
    pm._halted = False
    return pm


def _spec(underlying="XYZ", strategy="put_credit_spread", credit=0.5, qty=1,
          max_loss=450.0, notional=9_500.0):
    legs = [OptionLeg(contract_symbol=f"{underlying}P95", side=OrderDirection.SELL,
                      ratio_qty=1, option_type=None, strike=95.0,
                      expiry=date(2024, 2, 9), underlying=underlying)]
    return StructureSpec(underlying, strategy, legs, credit, qty, max_loss, notional,
                         date(2024, 2, 9))


def test_rails_notional_leverage_blocks(monkeypatch):
    pm = make_manager(balance=10_000.0)   # cap = 3.0 x 10k = 30k notional
    monkeypatch.setattr(pm, "get_option_holdings", lambda: {})
    big = _spec(notional=40_000.0, max_loss=1_000.0)
    pm.rebalance({"structures": [big]})
    assert pm.account.submitted == []


def test_rails_open_within_caps(monkeypatch):
    pm = make_manager()
    monkeypatch.setattr(pm, "get_option_holdings", lambda: {})
    pm.rebalance({"structures": [_spec()]})
    assert len(pm.account.submitted) == 1
    call = pm.account.submitted[0]
    assert call["option_strategy"] == "put_credit_spread"
    assert call["limit_price"] == -0.5          # credit -> negative net limit
    assert call["transaction_id"] is not None   # expert-attributed pre-created txn


def test_rails_one_structure_per_underlying(monkeypatch):
    pm = make_manager()
    held_txn = SimpleNamespace(id=7, symbol="XYZ")
    monkeypatch.setattr(pm, "get_option_holdings", lambda: {7: (held_txn, SimpleNamespace())})
    pm.rebalance({"structures": [_spec("XYZ"), _spec("ABC")]})
    assert len(pm.account.submitted) == 1
    assert pm.account.submitted[0]["legs"][0].underlying == "ABC"


def test_rails_concurrent_cap(monkeypatch):
    pm = make_manager()
    monkeypatch.setattr(pm, "get_option_holdings", lambda: {})
    pm.rebalance({"structures": [_spec("A"), _spec("B"), _spec("C")]})
    assert len(pm.account.submitted) == 2       # max_concurrent_structures = 2


def test_circuit_breaker_flattens_and_halts(monkeypatch):
    pm = make_manager(balance=7_000.0)          # peak 10k -> dd 30% > 20%
    pm._peak_equity = 10_000.0
    closed = []
    monkeypatch.setattr(pm, "get_option_holdings",
                        lambda: {7: (SimpleNamespace(id=7, symbol="XYZ"), SimpleNamespace(id=70))})
    monkeypatch.setattr(pm, "_close_structure", lambda txn, parent: closed.append(txn.id) or None)
    pm.manage_open(datetime(2024, 1, 3))
    assert closed == [7]
    assert pm._halted is True
    # While halted, manage_open is a no-op:
    closed.clear()
    pm.manage_open(datetime(2024, 1, 4))
    assert closed == []
