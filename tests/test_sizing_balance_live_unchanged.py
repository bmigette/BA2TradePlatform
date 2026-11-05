"""Finding 6 (2026-10-07) moved the BACKTEST's sizing balance onto equity. LIVE must be untouched.

The fix is one override, ``BacktestAccount._plain_balance``. Nothing a live broker adapter inherits
changed except documentation, so a live account's tradable balance is still ``get_balance()`` (margin
off) and, because ``get_balance()`` IS equity at a live broker, it equals ``get_tradable_equity()``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface

_SEAM_METHODS = ("_plain_balance", "get_tradable_balance", "get_tradable_equity",
                 "get_option_tradable_balance", "effective_margin_factor")


@pytest.mark.parametrize("module,cls", [
    ("ba2_trade_platform.modules.accounts.AlpacaAccount", "AlpacaAccount"),
    ("ba2_trade_platform.modules.accounts.TastyTradeAccount", "TastyTradeAccount"),
    ("ba2_trade_platform.modules.accounts.IBKRAccount", "IBKRAccount"),
])
def test_no_live_adapter_overrides_the_balance_seam(module, cls):
    import importlib
    account_cls = getattr(importlib.import_module(module), cls)
    for name in _SEAM_METHODS:
        assert getattr(account_cls, name) is getattr(ReadOnlyAccountInterface, name), (
            f"{cls}.{name} is overridden: the finding-6 fix assumed live accounts use the shared "
            f"implementation (get_balance() == equity)")


def test_the_seam_body_is_still_get_balance_for_a_live_shaped_account():
    """A live-shaped account (``get_balance()`` is equity): tradable balance == balance == the
    snapshot's equity, margin off."""
    class _Live(ReadOnlyAccountInterface):
        id = 7

        def __init__(self):            # bare: no DB, no broker
            pass

        def get_balance(self):
            return 4_000.0

        def get_account_snapshot(self):
            return AccountSnapshot(equity=4_000.0)

        def _margin_enabled(self):
            return False

    _Live.__abstractmethods__ = frozenset()      # only the seam under test is exercised
    live = object.__new__(_Live)
    assert live._plain_balance() == 4_000.0
    assert live.get_tradable_balance() == 4_000.0
    assert live.get_tradable_equity() == 4_000.0 == live.get_tradable_balance()
    assert live.get_option_tradable_balance() == 4_000.0

