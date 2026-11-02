"""The in-tree Black-Scholes inversion is a COPY of the backtest's; pin them to each other."""
import os
import random
import sys

import pytest

_TP_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "testplatform", "backend")
_GREEKS_FILE = os.path.join(_TP_BACKEND, "app", "services", "backtest", "option_greeks.py")


def _load_original():
    if not os.path.exists(_GREEKS_FILE):
        pytest.skip(f"testplatform option_greeks.py genuinely absent in this checkout ({_GREEKS_FILE})")
    if _TP_BACKEND not in sys.path:
        sys.path.insert(0, _TP_BACKEND)
    from app.services.backtest import option_greeks as og      # an ImportError here is a real failure
    return og


def test_inversion_matches_the_backtest_function_on_10000_random_inputs():
    from ba2_common.core.types import OptionRight
    from ba2_trade_platform.modules.dataproviders.options import bs_inversion as mine
    og = _load_original()
    rng = random.Random(20261006)
    n_iv = 0
    for _ in range(10_000):
        S = rng.uniform(5, 800)
        K = S * rng.uniform(0.7, 1.3)
        T = rng.uniform(1, 400) / 365.0
        r = rng.uniform(-0.005, 0.07)
        is_call = rng.random() < 0.6
        sigma = rng.uniform(0.05, 1.8)
        exact = mine.bs_price(S, K, T, r, sigma, is_call)
        # half the inputs are real prices (an IV exists), half arbitrary (often no IV / below intrinsic)
        price = exact if rng.random() < 0.5 else rng.uniform(0.0, S * 0.3)
        right = OptionRight.CALL if is_call else OptionRight.PUT
        ref = og.compute_iv_and_greeks(price, S, K, T, r, right)
        iv, delta = mine.iv_and_delta(price, S, K, T, r, is_call)
        assert (iv is None) == (ref["iv"] is None)
        if iv is not None:
            n_iv += 1
            assert abs(iv - ref["iv"]) <= 1e-12
            assert (delta is None) == (ref["delta"] is None)
            if delta is not None:
                assert abs(delta - ref["delta"]) <= 1e-12
        assert mine.bs_price(S, K, T, r, sigma, is_call) == og.bs_price(S, K, T, r, sigma, right)
    assert n_iv > 3000      # the comparison is not vacuous
