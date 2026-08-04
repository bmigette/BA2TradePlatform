"""
Vendored from FinanceHarness (https://github.com/Yijia-Xiao/FinanceHarness),
Apache-2.0 license. Modified for BA2TradePlatform: ToolSpec/ToolResponse layer
removed, imports localized.

compute.derivatives.black_scholes — European option price + Greeks (BSM).

Pure-math. Prices a European call/put on an asset with a continuous dividend yield via
Black-Scholes-Merton, and returns the first-order Greeks in the conventions the desk uses
(vega per volatility point, theta per calendar day, rho per 1% rate move). The output
carries the grounding that separates a correct read from a plausible one — N(d2) is the
*risk-neutral* ITM probability (a pricing weight, not a real-world probability), the model
is European-only, and sigma is forward-looking implied vol.
"""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ba2_common.core.finance_calc.format import num, pct

_N = NormalDist()


def black_scholes(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    vol: float,
    *,
    option_type: str = "call",
    dividend_yield: float = 0.0,
) -> dict[str, Any]:
    """European BSM price + first-order Greeks. Rates/vol continuously compounded, decimals."""
    is_call = option_type == "call"
    sqrt_t = years**0.5
    d1 = (math.log(spot / strike) + (rate - dividend_yield + 0.5 * vol * vol) * years) / (
        vol * sqrt_t
    )
    d2 = d1 - vol * sqrt_t
    disc_q = math.exp(-dividend_yield * years)
    disc_r = math.exp(-rate * years)
    nd1, nd2 = _N.cdf(d1), _N.cdf(d2)
    pdf_d1 = _N.pdf(d1)

    if is_call:
        price = spot * disc_q * nd1 - strike * disc_r * nd2
        delta = disc_q * nd1
        # annual theta, then per calendar day
        theta_annual = (
            -(spot * disc_q * pdf_d1 * vol) / (2 * sqrt_t)
            - rate * strike * disc_r * nd2
            + dividend_yield * spot * disc_q * nd1
        )
        rho = strike * years * disc_r * nd2
        prob_itm_rn = nd2  # risk-neutral P(S_T > K)
    else:
        price = strike * disc_r * _N.cdf(-d2) - spot * disc_q * _N.cdf(-d1)
        delta = -disc_q * _N.cdf(-d1)
        theta_annual = (
            -(spot * disc_q * pdf_d1 * vol) / (2 * sqrt_t)
            + rate * strike * disc_r * _N.cdf(-d2)
            - dividend_yield * spot * disc_q * _N.cdf(-d1)
        )
        rho = -strike * years * disc_r * _N.cdf(-d2)
        prob_itm_rn = _N.cdf(-d2)  # risk-neutral P(S_T < K)

    gamma = disc_q * pdf_d1 / (spot * vol * sqrt_t)  # same for call/put
    vega = spot * disc_q * pdf_d1 * sqrt_t  # same for call/put
    intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)

    return {
        "option_type": option_type,
        "price": round(price, 6),
        "intrinsic_value": round(intrinsic, 6),
        "time_value": round(price - intrinsic, 6),
        "delta": round(delta, 6),
        "gamma": round(gamma, 6),
        "vega_per_point": round(vega / 100.0, 6),  # per 1 vol point (KB convention)
        "theta_per_day": round(theta_annual / 365.0, 6),  # per calendar day
        "rho_per_1pct": round(rho / 100.0, 6),  # per 100bp rate move
        "prob_itm_risk_neutral": round(prob_itm_rn, 6),
        "d1": round(d1, 6),
        "d2": round(d2, 6),
        "inputs": {
            "spot": spot,
            "strike": strike,
            "years": years,
            "rate": rate,
            "vol": vol,
            "dividend_yield": dividend_yield,
        },
    }


class BlackScholesRequest(BaseModel):
    """Input for a European Black-Scholes-Merton price + Greeks."""

    model_config = ConfigDict(allow_inf_nan=False)  # reject NaN/inf inputs

    spot: float = Field(..., gt=0, description="Current underlying price S.")
    strike: float = Field(..., gt=0, description="Strike price K.")
    years: float = Field(..., gt=0, le=100, description="Time to expiry in years (e.g. 0.5).")
    rate: float = Field(
        ..., ge=-0.1, le=1, description="Continuously-compounded risk-free rate, decimal p.a."
    )
    vol: float = Field(..., gt=0, le=5, description="Volatility of log returns, decimal p.a.")
    option_type: Literal["call", "put"] = Field("call", description="call or put.")
    dividend_yield: float = Field(
        0.0, ge=0, le=1, description="Continuous dividend yield q (foreign rate for FX)."
    )


def _markdown(r: dict[str, Any]) -> str:
    inp = r["inputs"]
    return (
        f"**Black-Scholes {r['option_type']}** (European) — price **{num(r['price'], 4)}** "
        f"(intrinsic {num(r['intrinsic_value'], 4)} · time value {num(r['time_value'], 4)})\n"
        f"  Δ {num(r['delta'], 4)} · Γ {num(r['gamma'], 5)} · "
        f"vega {num(r['vega_per_point'], 4)}/pt · θ {num(r['theta_per_day'], 4)}/day · "
        f"ρ {num(r['rho_per_1pct'], 4)}/1%\n"
        f"  risk-neutral P(ITM) = N(d₂) = {pct(r['prob_itm_risk_neutral'])} — a pricing "
        f"weight, **not** a real-world probability.\n"
        f"  (European exercise only; σ={pct(inp['vol'])} is forward-looking implied vol, not "
        f"trailing realized; r is continuously compounded.)"
    )


def render_black_scholes(req: BlackScholesRequest) -> str:
    """Black-Scholes markdown renderer — the string an agent sees."""
    return _markdown(
        black_scholes(
            req.spot,
            req.strike,
            req.years,
            req.rate,
            req.vol,
            option_type=req.option_type,
            dividend_yield=req.dividend_yield,
        )
    )
