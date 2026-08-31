"""The launcher REFUSES PremiumSeller loudly since its removal (2026-08-31, plan Task 12).

The grid entry is gone from ``_EXPERT_OPT`` and the expert is gone from the backtest
handler's ``_SUPPORTED_EXPERTS`` map — so ``ba2-test optimize --expert PremiumSeller``
must exit with the "not configured" message, never silently run or silently skip, and a
historical payload naming the expert is rejected fail-early by the handler like any other
unknown class. Importlib-from-file pattern copied from test_launcher_parse_symbols.py.
"""
import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_premium_seller_has_no_grid_entry():
    assert "PremiumSeller" not in mod._EXPERT_OPT


def test_optimize_refuses_the_key_loudly():
    """`ba2-test optimize --expert PremiumSeller` dies with the not-configured exit —
    BEFORE any Strategy/Optimization row is created."""
    args = SimpleNamespace(expert="PremiumSeller")
    with pytest.raises(SystemExit) as e:
        mod._cmd_optimize(args)
    assert "not configured for expert 'PremiumSeller'" in str(e.value)


def test_backtest_handler_no_longer_supports_it():
    from app.services.backtest.daily_backtest_handler import (
        _EXPERT_WARMUP_BARS,
        _SUPPORTED_EXPERTS,
    )

    assert "PremiumSeller" not in _SUPPORTED_EXPERTS
    assert "PremiumSeller" not in _EXPERT_WARMUP_BARS
