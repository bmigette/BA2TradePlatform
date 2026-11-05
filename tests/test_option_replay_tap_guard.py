"""Guard: every SHIPPED options account has its replay taps, overrides included.

``OptionsAccountInterface.__init_subclass__`` taps each override at class creation.
This pins the outcome on the real accounts (a root test: it imports the live ones), so a
new broker, a changed base order or a hand-rolled wrapper that loses the tap fails here
rather than silently recording nothing on 8081/8082.

The rule per class: each name of ``OPTION_TAPPED_METHODS`` resolves (MRO) to a callable
carrying the tap marker, and no class in the MRO defines one of them without the marker
unless it opted out with ``_replay_taps_exempt``.
"""
import os
import sys

import pytest

from ba2_common.core.replay.observe import OBSERVED_ATTRIBUTE, tapped_boundary
from ba2_common.core.replay.option_taps import OPTION_TAPPED_METHODS

_TP_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "testplatform", "backend")


def _alpaca():
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
    return AlpacaAccount


def _ibkr():
    from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
    return IBKRAccount


def _backtest():
    path = os.path.join(_TP_BACKEND, "app", "services", "backtest", "backtest_account.py")
    if not os.path.exists(path):
        pytest.skip("testplatform backend not in this checkout")
    if _TP_BACKEND not in sys.path:
        sys.path.insert(0, _TP_BACKEND)
    # An ImportError here is a real failure, as in the other testplatform-importing tests.
    from app.services.backtest.backtest_account import BacktestAccount
    return BacktestAccount


@pytest.fixture(params=[_alpaca, _ibkr, _backtest], ids=["alpaca", "ibkr", "backtest"])
def account_class(request):
    return request.param()


def test_every_tapped_name_resolves_to_a_tapped_callable(account_class):
    for name in OPTION_TAPPED_METHODS:
        resolved = getattr(account_class, name)
        assert callable(resolved), f"{account_class.__name__}.{name}"
        assert getattr(resolved, OBSERVED_ATTRIBUTE, None) is not None, (
            f"{account_class.__name__}.{name} is not tapped (resolved from "
            f"{[k.__name__ for k in account_class.__mro__ if name in vars(k)][:1]})")


def test_no_class_in_the_mro_defines_an_untapped_override(account_class):
    for klass in account_class.__mro__:
        if getattr(klass, "_replay_taps_exempt", False):
            continue
        for name in OPTION_TAPPED_METHODS:
            if name not in vars(klass):
                continue
            defined = vars(klass)[name]
            if getattr(defined, "__isabstractmethod__", False):
                continue
            # Only classes that derive from OptionsAccountInterface get the hook; the plain
            # AccountInterface bases (get_positions, get_account_snapshot) legitimately stay
            # untapped -- what matters is what the concrete class resolves to (test above).
            from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
            if not issubclass(klass, OptionsAccountInterface):
                continue
            assert tapped_boundary(defined) is not None, (
                f"{klass.__name__} defines {name} without the replay tap")


def test_quote_caching_wrapper_is_exempt():
    from ba2_trade_platform.core.option_pnl_display import QuoteCachingAccount
    assert QuoteCachingAccount._replay_taps_exempt is True
    assert tapped_boundary(QuoteCachingAccount.get_option_quote) is None
