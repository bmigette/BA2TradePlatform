"""The in-tree ba2_trade_platform.core.account_types must BE the package module.

Phase 6 alias shims swap themselves out of sys.modules so that both import paths
resolve to one module object; a shim that merely re-exported would give
unittest.mock.patch two different targets.
"""
from datetime import date


def test_in_tree_account_types_is_the_package_module():
    import ba2_common.core.account_types as pkg
    import ba2_trade_platform.core.account_types as shim
    assert shim is pkg


def test_in_tree_import_path_exposes_the_value_objects():
    from ba2_trade_platform.core.account_types import (
        CASH_TRANSFER_DEPOSIT,
        AccountSnapshot,
        CashTransfer,
        MarginInfo,
        OrderImpact,
    )
    assert CASH_TRANSFER_DEPOSIT == "DEPOSIT"
    assert AccountSnapshot().buying_power is None
    assert MarginInfo(symbol="A", bp_factor=1.0).symbol == "A"
    assert OrderImpact(symbol="A", change_in_buying_power=-1.0).bp_cost == 1.0
    assert CashTransfer(external_id="a", event_date=date(2026, 8, 1),
                        event_type=CASH_TRANSFER_DEPOSIT, amount=5.0).is_income is True
