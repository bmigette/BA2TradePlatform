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

ONE BLACK-SCHOLES, NOT THREE (2026-09-02, plan Task 14b item 8). This module used to carry
its OWN closed form — a private ``_d1_d2`` plus hand-written normal CDF/PDF — alongside
``ba2_common.core.finance_calc.derivatives.black_scholes`` (the pinned shared pricer) and
``ba2_common.core.option_bs.bs_price`` (the mark-fallback wrapper over it). Three copies of
one formula, kept in step only by a 1e-6 parity test that would have tolerated a real
divergence in the sixth decimal of every cached greek. ``bs_price`` and ``greeks`` below now
DELEGATE to the shared function; what stays here is what is genuinely this module's own: the
degenerate-input gate, the bisection inverter, and the one-shot convenience wrapper.

WHAT THAT CHANGED, stated because it is a real (tiny) numeric move and not a pure refactor:
``black_scholes`` ROUNDS its outputs to 6 decimals, and the old local form did not. Prices and
greeks computed here therefore land on the same 6-decimal grid the platform's other greeks
already use — which is the point (the cache viewer and the mark fallback were showing numbers
from a different arithmetic), but it means a value can move by up to 5e-7. The Greek
CONVENTIONS were already identical on both sides (theta per calendar day = annual/365, vega
per 1 vol point = raw/100), so nothing needed translating.
"""
from __future__ import annotations

from typing import Optional

from ba2_common.core.finance_calc.derivatives import black_scholes
from ba2_common.core.types import OptionRight


def _shared(S: float, K: float, T: float, r: float, sigma: float,
            option_type: OptionRight, q: float) -> Optional[dict]:
    """The ONE shared BSM evaluation, behind this module's degenerate-input gate.

    The gate is this module's own responsibility and cannot move into ``black_scholes``: the
    shared function is a pure formula and RAISES on degenerate inputs (``log(S/K)`` for
    S <= 0, a zero denominator for ``sigma <= 0`` or ``T <= 0``), while every caller here is
    a per-bar cache builder that must return "no computed greek for this row" and carry on.
    """
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None
    try:
        return black_scholes(S, K, T, r, sigma,
                             option_type="call" if option_type == OptionRight.CALL else "put",
                             dividend_yield=q)
    except (ValueError, ZeroDivisionError, OverflowError, ArithmeticError):
        return None


def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             option_type: OptionRight, q: float = 0.0) -> Optional[float]:
    """Black-Scholes theoretical price. None if inputs are degenerate (T<=0, sigma<=0)."""
    out = _shared(S, K, T, r, sigma, option_type, q)
    return None if out is None else float(out["price"])


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
    out = _shared(S, K, T, r, sigma, option_type, q)
    if out is None:
        return None
    # The shared pricer already publishes both under this module's conventions:
    # ``theta_per_day`` is annual/365 and ``vega_per_point`` is raw/100. Renaming rather than
    # recomputing is the whole point -- a second division here would be a second convention.
    return {"delta": out["delta"], "gamma": out["gamma"],
            "theta": out["theta_per_day"], "vega": out["vega_per_point"]}


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
