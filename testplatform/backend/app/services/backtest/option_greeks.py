"""Black-Scholes implied volatility + Greeks from an option's own traded price.

WHY: IV is not an independently-observed quantity — it is the volatility that makes the
Black-Scholes formula reproduce the option's ACTUAL price. Alpaca (our only integrated
options source) only exposes IV/greeks as a live snapshot, never historically. But we
already fetch the option's own daily close (`fetch_options.py`'s per-contract bars) plus
the underlying's daily close and a risk-free rate — everything Black-Scholes needs. So we
compute IV/greeks ourselves instead of buying them from a vendor.

Pure (no I/O, no DB). European-style Black-Scholes; equity options are American, so this is
an approximation (mainly affects deep-ITM puts near ex-dividend) — acceptable for backtest
strike selection / IV-rank gating, not for pricing early-exercise value.
"""
from __future__ import annotations

import math
from typing import Optional

from ba2_common.core.types import OptionRight

_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float, q: float) -> tuple:
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / v
    return d1, d1 - v


def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             option_type: OptionRight, q: float = 0.0) -> Optional[float]:
    """Black-Scholes theoretical price. None if inputs are degenerate (T<=0, sigma<=0)."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    if option_type == OptionRight.CALL:
        return S * math.exp(-q * T) * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * math.exp(-q * T) * _norm_cdf(-d1)


# Bisection bracket for sigma: 0.01% to 500% annualized — wide enough for any liquid equity
# option, narrow enough that bisection converges in ~40 iterations to float precision.
_SIGMA_LO = 1e-4
_SIGMA_HI = 5.0
_MAX_ITER = 60
_TOL = 1e-6


def implied_volatility(price: float, S: float, K: float, T: float, r: float,
                        option_type: OptionRight, q: float = 0.0) -> Optional[float]:
    """Invert Black-Scholes for sigma given the option's actual traded price.

    None when the price is outside the no-arbitrage bounds bisection can bracket (e.g. below
    intrinsic value — common with our zero-spread close-price proxy on illiquid days) or when
    T<=0 (expired) — never raises, so a bad/missing input just yields no computed IV for that
    row rather than aborting the whole cache build.
    """
    if price is None or price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    lo, hi = _SIGMA_LO, _SIGMA_HI
    f_lo = bs_price(S, K, T, r, lo, option_type, q) - price
    f_hi = bs_price(S, K, T, r, hi, option_type, q) - price
    if f_lo is None or f_hi is None or f_lo * f_hi > 0:
        return None  # price outside what the bracket can reproduce (e.g. below intrinsic)
    for _ in range(_MAX_ITER):
        mid = (lo + hi) / 2.0
        f_mid = bs_price(S, K, T, r, mid, option_type, q) - price
        if abs(f_mid) < _TOL or (hi - lo) < _TOL:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def greeks(S: float, K: float, T: float, r: float, sigma: float,
           option_type: OptionRight, q: float = 0.0) -> Optional[dict]:
    """Analytic Black-Scholes Greeks given a known sigma (call AFTER implied_volatility).

    Returns {"delta","gamma","theta","vega"} or None if inputs are degenerate. Theta is
    PER-DAY (annual theta / 365, the conventionally quoted figure); vega is PER 1-VOL-POINT
    (i.e. per 1% absolute change in IV, the conventionally quoted figure) — both divided down
    from the raw per-unit Black-Scholes derivatives so callers get broker-familiar numbers."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    pdf_d1 = _norm_pdf(d1)
    disc_q = math.exp(-q * T)
    disc_r = math.exp(-r * T)
    sqrt_T = math.sqrt(T)

    gamma = disc_q * pdf_d1 / (S * sigma * sqrt_T)
    vega_annual = S * disc_q * pdf_d1 * sqrt_T
    vega = vega_annual / 100.0  # per 1 vol POINT (1%), not per 1.0 (100%)

    if option_type == OptionRight.CALL:
        delta = disc_q * _norm_cdf(d1)
        theta_annual = (-(S * disc_q * pdf_d1 * sigma) / (2 * sqrt_T)
                        - r * K * disc_r * _norm_cdf(d2)
                        + q * S * disc_q * _norm_cdf(d1))
    else:
        delta = disc_q * (_norm_cdf(d1) - 1.0)
        theta_annual = (-(S * disc_q * pdf_d1 * sigma) / (2 * sqrt_T)
                        + r * K * disc_r * _norm_cdf(-d2)
                        - q * S * disc_q * _norm_cdf(-d1))

    return {"delta": delta, "gamma": gamma, "theta": theta_annual / 365.0, "vega": vega}


def compute_iv_and_greeks(price: Optional[float], S: Optional[float], K: float, T: float,
                          r: float, option_type: OptionRight, q: float = 0.0) -> dict:
    """Convenience one-shot: solve IV from `price` then derive Greeks from it.

    Returns a dict with keys iv/delta/gamma/theta/vega, each None where computation was not
    possible (missing price/underlying, expired, or price outside no-arbitrage bounds) — safe
    to splat directly into a chain/bar row without the caller re-checking every input."""
    out = {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}
    if price is None or S is None:
        return out
    iv = implied_volatility(price, S, K, T, r, option_type, q)
    if iv is None:
        return out
    out["iv"] = iv
    g = greeks(S, K, T, r, iv, option_type, q)
    if g is not None:
        out.update(g)
    return out
