"""Black-Scholes implied-volatility inversion for the LIVE ATM-IV history provider.

SOURCE (copied, not imported): ``testplatform/backend/app/services/backtest/option_greeks.py``
(``_shared``, ``bs_price``, ``implied_volatility``, and the delta half of ``greeks`` /
``compute_iv_and_greeks``). The live app must not import testplatform, so the minimal pure
functions are duplicated here VERBATIM in arithmetic -- same bracket, same 60-step bisection,
same 1e-6 tolerance, same delegation to the shared pricer
``ba2_common.core.finance_calc.derivatives.black_scholes``.

WHY A COPY IS SAFE: ``tests/test_atm_iv_bs_parity.py`` imports the original and asserts the two
agree to <= 1e-12 on 10,000 random inputs. If the backtest function ever changes, that test is
the alarm; update this file and bump ``atm_iv_history.METHOD_VERSION`` together.

Pure (no I/O). European Black-Scholes on American equity options is the same approximation the
backtest makes; matching it is the point (BT/live parity).
"""
from __future__ import annotations

from typing import Optional, Tuple

from ba2_common.core.finance_calc.derivatives import black_scholes

_SIGMA_LO = 1e-4
_SIGMA_HI = 5.0
_MAX_ITER = 60
_TOL = 1e-6


def _shared(S: float, K: float, T: float, r: float, sigma: float, is_call: bool,
            q: float = 0.0) -> Optional[dict]:
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None
    try:
        return black_scholes(S, K, T, r, sigma, option_type="call" if is_call else "put",
                             dividend_yield=q)
    except (ValueError, ZeroDivisionError, OverflowError, ArithmeticError):
        return None


def bs_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool,
             q: float = 0.0) -> Optional[float]:
    out = _shared(S, K, T, r, sigma, is_call, q)
    return None if out is None else float(out["price"])


def implied_volatility(price: float, S: float, K: float, T: float, r: float, is_call: bool,
                       q: float = 0.0) -> Optional[float]:
    """Sigma that reproduces ``price`` (None if unbracketable / degenerate). Never raises."""
    if price is None or price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    lo, hi = _SIGMA_LO, _SIGMA_HI
    f_lo = bs_price(S, K, T, r, lo, is_call, q) - price
    f_hi = bs_price(S, K, T, r, hi, is_call, q) - price
    if f_lo is None or f_hi is None or f_lo * f_hi > 0:
        return None
    for _ in range(_MAX_ITER):
        mid = (lo + hi) / 2.0
        f_mid = bs_price(S, K, T, r, mid, is_call, q) - price
        if abs(f_mid) < _TOL or (hi - lo) < _TOL:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def iv_and_delta(price: Optional[float], S: Optional[float], K: float, T: float, r: float,
                 is_call: bool, q: float = 0.0) -> Tuple[Optional[float], Optional[float]]:
    """``(iv, delta)`` exactly as ``compute_iv_and_greeks`` yields them; ``(None, None)`` when
    no IV, ``(iv, None)`` if the greeks could not be evaluated."""
    if price is None or S is None:
        return None, None
    iv = implied_volatility(price, S, K, T, r, is_call, q)
    if iv is None:
        return None, None
    out = _shared(S, K, T, r, iv, is_call, q)
    return iv, (None if out is None else out["delta"])
