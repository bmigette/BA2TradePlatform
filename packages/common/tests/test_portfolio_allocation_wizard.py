"""Pure unit tests for the Portfolio Allocation wizard arithmetic.

Everything here runs with no DB, no broker and no NiceGUI. Invoke by explicit
path -- pytest.ini has `testpaths = tests`, so this directory is not collected
by a bare `pytest`.
"""
import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.portfolio_allocation import (
    VALUATION_MODE_COST,
    VALUATION_MODE_MARKET,
    BaseSnapshot,
    PositionState,
    WARNING_NO_MULTIPLIER,
    build_base_snapshot,
    compute_base_notional,
)


def test_base_notional_adds_cost_basis_of_managed_positions_only():
    current = {
        "AAPL": PositionState(symbol="AAPL", quantity=10, cost_basis=1500.0, price=160.0),
        "MSFT": PositionState(symbol="MSFT", quantity=5, cost_basis=2000.0, price=410.0),
        "TSLA": PositionState(symbol="TSLA", quantity=3, cost_basis=900.0, price=300.0),
    }
    # TSLA is held but NOT managed -> it must not inflate the base.
    # valuation_mode is REQUIRED (no default, see the Task 25 amendment) and COST is
    # what 1500 + 2000 is: at market those two positions are 1600 + 2050.
    base = compute_base_notional(10_000.0, current, ["AAPL", "MSFT"],
                                 valuation_mode=VALUATION_MODE_COST)
    assert base == pytest.approx(13_500.0)


def test_build_base_snapshot_splits_buying_power_from_managed_value():
    snap = AccountSnapshot(cash=2_000.0, buying_power=10_000.0, margin_multiplier=2.0,
                           is_margin_account=True, supports_fractional=True)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10, cost_basis=1500.0)}

    base = build_base_snapshot(snap, current, ["AAPL"])

    assert isinstance(base, BaseSnapshot)
    assert base.available_buying_power == pytest.approx(10_000.0)
    assert base.managed_value == pytest.approx(1_500.0)
    assert base.base_notional == pytest.approx(11_500.0)
    assert base.default_bp_factor == pytest.approx(2.0)
    assert base.valuation_mode == VALUATION_MODE_COST
    assert base.warnings == []


def test_build_base_snapshot_in_market_mode_values_positions_at_the_live_price():
    snap = AccountSnapshot(buying_power=10_000.0, margin_multiplier=1.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10, cost_basis=1500.0,
                                     price=250.0)}

    base = build_base_snapshot(snap, current, ["AAPL"],
                               valuation_mode=VALUATION_MODE_MARKET)

    assert base.managed_value == pytest.approx(2_500.0)
    assert base.base_notional == pytest.approx(12_500.0)
    assert base.valuation_mode == VALUATION_MODE_MARKET


def test_build_base_snapshot_without_multiplier_assumes_cash_account():
    snap = AccountSnapshot(buying_power=5_000.0, margin_multiplier=None)
    base = build_base_snapshot(snap, {}, [])
    assert base.default_bp_factor == pytest.approx(1.0)
    assert WARNING_NO_MULTIPLIER in base.warnings


def test_build_base_snapshot_missing_buying_power_raises():
    snap = AccountSnapshot(cash=1_000.0, buying_power=None)
    with pytest.raises(ValueError):
        build_base_snapshot(snap, {}, [])


def test_build_base_snapshot_with_no_snapshot_at_all_raises():
    with pytest.raises(ValueError):
        build_base_snapshot(None, {}, [])
