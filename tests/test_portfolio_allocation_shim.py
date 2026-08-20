"""The in-tree portfolio_allocation path must BE the ba2_common module object."""
import importlib
import sys

import ba2_common.core.portfolio_allocation as pkg


def test_in_tree_path_resolves_to_the_package_module():
    shim = importlib.import_module("ba2_trade_platform.core.portfolio_allocation")
    assert shim is pkg
    assert sys.modules["ba2_trade_platform.core.portfolio_allocation"] is pkg


def test_shim_exposes_the_engine_entry_points():
    from ba2_trade_platform.core.portfolio_allocation import (
        AllocationPlan, PositionFetchFailed, VALUATION_MODE_COST, compute_allocation,
    )
    assert callable(compute_allocation)
    assert AllocationPlan().scale_factor == 1.0
    assert VALUATION_MODE_COST == "cost"
    assert issubclass(PositionFetchFailed, RuntimeError)
