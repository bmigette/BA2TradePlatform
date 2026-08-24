"""Unit tests for OptionPortfolioManager with a stub account/expert (no DB, no
resolver): the manager is built via __new__ and wired by hand. The resolver-backed
__init__ path is FactorPortfolioManager's proven pattern and is covered by the
engine seam tests (testplatform/backend/tests/backtest/test_premium_seller_seams.py).
"""
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus
from ba2_experts.PremiumSeller import portfolio as ps_portfolio_mod
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


# ---------------------------------------------------------------------------
# Circuit-breaker stopgap (2026-08): three ways the breaker could FAIL OPEN.
#
# These pin the *live* manager only. The same semantics are owned, pure, by
# ba2_common.core.option_book (update_breaker / rearm) for the Task 8 rewire; the
# assertions below deliberately agree with it — the peak ratchets on EVERY
# evaluation (including a flat sleeve), a non-positive peak is an unusable
# baseline rather than a licence to trip, and re-arming keeps the peak.
# ---------------------------------------------------------------------------
def _one_holding():
    return {7: (SimpleNamespace(id=7, symbol="XYZ"), SimpleNamespace(id=70))}


def _no_natural_exits(monkeypatch, pm):
    """Isolate the breaker: the per-structure exit rules never fire on their own."""
    monkeypatch.setattr(pm, "_should_close", lambda txn, parent, as_of: False)


def test_rails_equity_guard_declines_on_its_own(monkeypatch):
    """The equity guard must decline by itself, not by luck of a downstream rail.

    A zero-cost candidate makes the deployment/leverage/undefined-risk rails inert, so
    only `equity is None or equity <= 0` can decline it. Without this, weakening the
    guard to `equity is None` passes every existing rails test."""
    for balance in (None, 0.0, -5_000.0):
        pm = make_manager(balance=balance)
        monkeypatch.setattr(pm, "get_option_holdings", lambda: {})
        pm.rebalance({"structures": [_spec(max_loss=0.0, notional=0.0)]})
        assert pm.account.submitted == [], f"opened a structure on equity {balance!r}"


def test_peak_ratchets_while_the_sleeve_is_flat(monkeypatch):
    """Defect 1: the flat-book early return must not skip the peak ratchet."""
    pm = make_manager(balance=10_000.0)
    monkeypatch.setattr(pm, "get_option_holdings", lambda: {})
    assert pm.manage_open(datetime(2024, 1, 3)) == []
    assert pm._peak_equity == 10_000.0


def test_breaker_fires_against_a_peak_set_while_the_sleeve_was_flat(monkeypatch):
    """Defect 1, consequence: a sleeve flattened at 10k and re-entered must still
    measure its drawdown from 10k, not from wherever equity stood on re-entry."""
    pm = make_manager(balance=10_000.0)
    holdings: Dict[int, Any] = {}
    monkeypatch.setattr(pm, "get_option_holdings", lambda: holdings)
    _no_natural_exits(monkeypatch, pm)
    pm.manage_open(datetime(2024, 1, 3))            # flat bar -> peak must ratchet to 10k
    holdings.update(_one_holding())                 # sleeve re-enters
    pm.account._balance = 7_000.0                   # -30% from the flat-bar peak
    closed = []
    monkeypatch.setattr(pm, "_close_structure", lambda txn, parent: closed.append(txn.id) or None)
    pm.manage_open(datetime(2024, 1, 4))
    assert closed == [7]
    assert pm._halted is True


def test_peak_never_ratchets_down(monkeypatch):
    pm = make_manager(balance=10_000.0)
    monkeypatch.setattr(pm, "get_option_holdings", lambda: {})
    pm.manage_open(datetime(2024, 1, 3))
    pm.account._balance = 9_000.0                   # -10%: below the breaker, above the trip
    pm.manage_open(datetime(2024, 1, 4))
    assert pm._peak_equity == 10_000.0


def test_peak_ratchets_up_with_rising_equity(monkeypatch):
    """The other direction: a peak that only ever took its FIRST reading would
    understate every later drawdown and the breaker would fire far too late."""
    pm = make_manager(balance=10_000.0)
    monkeypatch.setattr(pm, "get_option_holdings", lambda: {})
    pm.manage_open(datetime(2024, 1, 3))
    assert pm._peak_equity == 10_000.0
    pm.account._balance = 12_500.0
    pm.manage_open(datetime(2024, 1, 4))
    assert pm._peak_equity == 12_500.0


def test_zero_equity_is_recorded_as_a_peak_rather_than_skipped(monkeypatch):
    """The same is-not-None discipline on the RATCHET's equity operand: `if balance:`
    would discard a reading of exactly 0.0 and leave the peak unset."""
    pm = make_manager(balance=0.0)
    monkeypatch.setattr(pm, "get_option_holdings", lambda: {})
    pm.manage_open(datetime(2024, 1, 3))
    assert pm._peak_equity == 0.0


def test_breaker_does_not_read_a_non_positive_peak_as_a_baseline(monkeypatch):
    """Defect 2: the usable-peak test must be explicit, not truthiness.

    A negative peak is truthy, so `and self._peak_equity` accepted it and compared
    against `peak x 0.8` — which for a negative peak is ABOVE the peak, so the
    breaker fired on a drawdown that does not exist. A peak-to-trough drawdown is
    undefined unless the peak is positive (option_book.update_breaker: `peak is
    None or peak <= 0` -> blind, never tripped)."""
    pm = make_manager(balance=-5_000.0)
    pm._peak_equity = -5_000.0
    closed = []
    monkeypatch.setattr(pm, "get_option_holdings", lambda: _one_holding())
    _no_natural_exits(monkeypatch, pm)
    monkeypatch.setattr(pm, "_close_structure", lambda txn, parent: closed.append(txn.id) or None)
    assert pm.manage_open(datetime(2024, 1, 3)) == []
    assert closed == []
    assert pm._halted is False


def test_breaker_treats_a_zero_peak_as_unusable_not_as_disabled(monkeypatch):
    """Defect 2, the 0.0 case: an unusable peak stands the BREAKER down, not the
    whole exit pass — the per-structure rules must still be evaluated."""
    pm = make_manager(balance=0.0)
    pm._peak_equity = 0.0
    seen = []
    monkeypatch.setattr(pm, "get_option_holdings", lambda: _one_holding())
    monkeypatch.setattr(pm, "_should_close",
                        lambda txn, parent, as_of: bool(seen.append(txn.id)))
    monkeypatch.setattr(pm, "_close_structure", lambda txn, parent: None)
    pm.manage_open(datetime(2024, 1, 3))
    assert seen == [7]              # fell through to the normal per-structure path
    assert pm._halted is False      # ... and the breaker did not fire on 0/0


def test_zero_equity_against_a_positive_peak_trips_the_breaker(monkeypatch):
    """Defect 2, one operand to the LEFT: the equity operand needs `is not None` too.

    A balance of exactly 0.0 is not a missing reading, it is a 100% drawdown — the
    deepest one there is. Testing `balance` for truthiness reads that total loss as
    "no equity reported" and silently stands the breaker down at the one moment it
    most needs to fire. Pairs with test_unknown_equity_is_not_a_trip: 0.0 is a trip,
    None is not, and the two must never collapse into the same answer."""
    pm = make_manager(balance=0.0)
    pm._peak_equity = 10_000.0
    closed = []
    monkeypatch.setattr(pm, "get_option_holdings", lambda: _one_holding())
    _no_natural_exits(monkeypatch, pm)
    monkeypatch.setattr(pm, "_close_structure", lambda txn, parent: closed.append(txn.id) or None)
    pm.manage_open(datetime(2024, 1, 3))
    assert closed == [7]
    assert pm._halted is True


def test_unknown_equity_is_not_a_trip(monkeypatch):
    pm = make_manager(balance=None)
    pm._peak_equity = 10_000.0
    closed = []
    monkeypatch.setattr(pm, "get_option_holdings", lambda: _one_holding())
    _no_natural_exits(monkeypatch, pm)
    monkeypatch.setattr(pm, "_close_structure", lambda txn, parent: closed.append(txn.id) or None)
    pm.manage_open(datetime(2024, 1, 3))
    assert closed == []
    assert pm._halted is False
    assert pm._peak_equity == 10_000.0      # unknown equity does not disturb the peak


def test_breaker_flatten_does_not_leak_nones_into_the_returned_orders(monkeypatch):
    """_close_structure returns None for a structure with nothing left to offset. The
    breaker's flatten must filter those exactly as the normal exit path does, or a None
    lands in the order list the engine consumes."""
    pm = make_manager(balance=7_000.0)
    pm._peak_equity = 10_000.0
    holdings = {7: (SimpleNamespace(id=7, symbol="XYZ"), SimpleNamespace(id=70)),
                8: (SimpleNamespace(id=8, symbol="ABC"), SimpleNamespace(id=80))}
    real = SimpleNamespace(id=99)
    monkeypatch.setattr(pm, "get_option_holdings", lambda: holdings)
    _no_natural_exits(monkeypatch, pm)
    monkeypatch.setattr(pm, "_close_structure",
                        lambda txn, parent: real if txn.id == 7 else None)
    assert pm.manage_open(datetime(2024, 1, 3)) == [real]
    assert pm._halted is True


def test_breaker_trips_at_exactly_the_configured_drawdown(monkeypatch):
    """The boundary is inclusive: `equity <= peak x (1 - pct/100)`, so exactly -20%
    trips. option_book.update_breaker pins the identical comparison, deliberately, so
    the stopgap and its Task 8 replacement cannot disagree at the edge."""
    pm = make_manager(balance=8_000.0)          # peak 10k, circuit_breaker_pct 20.0
    pm._peak_equity = 10_000.0
    closed = []
    monkeypatch.setattr(pm, "get_option_holdings", lambda: _one_holding())
    _no_natural_exits(monkeypatch, pm)
    monkeypatch.setattr(pm, "_close_structure", lambda txn, parent: closed.append(txn.id) or None)
    pm.manage_open(datetime(2024, 1, 3))
    assert closed == [7]
    assert pm._halted is True


def test_breaker_does_not_trip_above_the_configured_drawdown(monkeypatch):
    """... and one basis point shallower does NOT trip. Without this the breaker could
    be firing on any drawdown at all and every other test here would still be green."""
    pm = make_manager(balance=8_100.0)          # -19%, inside the 20% rail
    pm._peak_equity = 10_000.0
    closed = []
    monkeypatch.setattr(pm, "get_option_holdings", lambda: _one_holding())
    _no_natural_exits(monkeypatch, pm)
    monkeypatch.setattr(pm, "_close_structure", lambda txn, parent: closed.append(txn.id) or None)
    pm.manage_open(datetime(2024, 1, 3))
    assert closed == []
    assert pm._halted is False


def test_entry_cycle_that_opens_nothing_does_not_rearm(monkeypatch):
    """Defect 3: merely RUNNING an entry cycle must not clear a stand-down."""
    pm = make_manager()
    pm._halted = True
    monkeypatch.setattr(pm, "get_option_holdings", lambda: {})
    pm.rebalance({"structures": []})
    assert pm.account.submitted == []
    assert pm._halted is True


def test_entry_cycle_whose_candidates_are_all_declined_does_not_rearm(monkeypatch):
    """Defect 3: candidates that the rails decline are not a re-entry either."""
    pm = make_manager(balance=10_000.0)
    pm._halted = True
    monkeypatch.setattr(pm, "get_option_holdings", lambda: {})
    pm.rebalance({"structures": [_spec(notional=40_000.0, max_loss=1_000.0)]})
    assert pm.account.submitted == []
    assert pm._halted is True


def test_entry_cycle_that_opens_a_structure_rearms(monkeypatch):
    """Defect 3, the other half: a sleeve that actually re-enters MUST re-arm, or
    the new structures would never be managed (manage_open no-ops while halted)."""
    pm = make_manager()
    pm._halted = True
    monkeypatch.setattr(pm, "get_option_holdings", lambda: {})
    pm.rebalance({"structures": [_spec()]})
    assert len(pm.account.submitted) == 1
    assert pm._halted is False


def test_breaker_can_fire_again_after_a_reentry(monkeypatch):
    """The latch must not be a one-shot: halt -> re-enter -> the breaker still works.

    The peak is deliberately KEPT across the re-arm (option_book.rearm), so a sleeve
    that re-enters while still under water trips again immediately."""
    pm = make_manager(balance=7_000.0)
    pm._peak_equity = 10_000.0
    holdings = _one_holding()
    closed = []
    monkeypatch.setattr(pm, "get_option_holdings", lambda: holdings)
    _no_natural_exits(monkeypatch, pm)
    monkeypatch.setattr(pm, "_close_structure", lambda txn, parent: closed.append(txn.id) or None)
    pm.manage_open(datetime(2024, 1, 3))
    assert closed == [7] and pm._halted is True
    pm.rebalance({"structures": [_spec("ABC")]})    # a real re-entry re-arms
    assert pm._halted is False
    assert pm._peak_equity == 10_000.0              # ... without forgetting the peak
    closed.clear()
    pm.manage_open(datetime(2024, 1, 5))
    assert closed == [7]                            # and the breaker fires again
    assert pm._halted is True


def _combo_orders():
    """Multi-leg combo: parent order + two child SELL legs WITH parent_order_id set."""
    return [
        SimpleNamespace(id=70, parent_order_id=None, asset_class=AssetClass.OPTION,
                        contract_symbol=None, status=OrderStatus.FILLED,
                        side=OrderDirection.SELL, option_strategy="put_credit_spread"),
        SimpleNamespace(id=71, parent_order_id=70, asset_class=AssetClass.OPTION,
                        contract_symbol="XYZP95", status=OrderStatus.FILLED,
                        side=OrderDirection.SELL, filled_qty=1.0, quantity=1.0,
                        expiry=date(2024, 2, 9), underlying_symbol="XYZ"),
        SimpleNamespace(id=72, parent_order_id=70, asset_class=AssetClass.OPTION,
                        contract_symbol="XYZP90", status=OrderStatus.FILLED,
                        side=OrderDirection.SELL, filled_qty=1.0, quantity=1.0,
                        expiry=date(2024, 2, 9), underlying_symbol="XYZ"),
    ]


def test_tested_combo_short_leg_over_threshold(monkeypatch):
    pm = make_manager()                              # tested_delta = 0.30
    # Patch the module OBJECT (not the "ba2_experts.PremiumSeller.portfolio" string path):
    # ba2_experts/__init__.py binds the name PremiumSeller to the CLASS, which shadows the
    # submodule attribute and breaks monkeypatch's dotted-path resolution.
    monkeypatch.setattr(ps_portfolio_mod, "orders_where", lambda **_: _combo_orders())
    pm.account.get_option_chain = lambda underlying, start, end: [
        SimpleNamespace(symbol="XYZP95", delta=-0.35)]   # |delta| 0.35 >= 0.30
    parent = SimpleNamespace(id=70, transaction_id=7)
    assert pm._tested(parent) is True


def test_tested_combo_short_leg_under_threshold(monkeypatch):
    pm = make_manager()                              # tested_delta = 0.30
    # Patch the module OBJECT (not the "ba2_experts.PremiumSeller.portfolio" string path):
    # ba2_experts/__init__.py binds the name PremiumSeller to the CLASS, which shadows the
    # submodule attribute and breaks monkeypatch's dotted-path resolution.
    monkeypatch.setattr(ps_portfolio_mod, "orders_where", lambda **_: _combo_orders())
    pm.account.get_option_chain = lambda underlying, start, end: [
        SimpleNamespace(symbol="XYZP95", delta=-0.20),   # |delta| 0.20 < 0.30
        SimpleNamespace(symbol="XYZP90", delta=None)]    # None delta -> no action
    parent = SimpleNamespace(id=70, transaction_id=7)
    assert pm._tested(parent) is False
