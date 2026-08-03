"""
Vendored from FinanceHarness (https://github.com/Yijia-Xiao/FinanceHarness),
Apache-2.0 license. Modified for BA2TradePlatform: ToolSpec/ToolResponse layer
removed, ToolError replaced with ValueError, imports localized.

compute.fixed_income.bond — fixed-rate bond analytics (price, yield, risk).

Pure-math. From a bond's terms and either its YTM or its price, returns the full analytic
set: clean price, YTM (solved from price if that's what's given), current yield, Macaulay &
modified duration, convexity, DV01, and the ±100bp price change from the second-order
approximation. The output carries the domain caveats — the premium/discount read, the
positive-convexity asymmetry, DV01 additivity, and the flag that analytic duration is
invalid for callables/MBS (use effective duration).

Analytic measures assume option-free, fixed-rate cash flows.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ba2_common.core.finance_calc.format import num, pct


def _cashflows(coupon_per_period: float, n: int, face: float) -> list[float]:
    cfs = [coupon_per_period] * n
    cfs[-1] += face
    return cfs


def _price_at(cfs: list[float], periodic_yield: float) -> float:
    return sum(cf / (1 + periodic_yield) ** (t + 1) for t, cf in enumerate(cfs))


def _solve_ytm(cfs: list[float], target_price: float, m: int) -> float:
    """Bisect the periodic yield that reprices to target_price (price is monotone in yield)."""
    lo, hi = -0.5 / m, 2.0 / m  # periodic-yield bracket (annual −50%..200%)
    for _ in range(200):
        mid = (lo + hi) / 2
        if _price_at(cfs, mid) > target_price:
            lo = mid  # price too high → need a higher yield
        else:
            hi = mid
    return (lo + hi) / 2 * m  # back to annual nominal


def bond_analytics(
    coupon_rate: float,
    years_to_maturity: float,
    *,
    frequency: int = 2,
    face: float = 100.0,
    ytm: float | None = None,
    price: float | None = None,
) -> dict[str, Any]:
    """Price/yield + duration/convexity/DV01 for an option-free fixed-rate bond."""
    m = frequency
    n = round(years_to_maturity * m)
    coupon_per_period = coupon_rate * face / m
    cfs = _cashflows(coupon_per_period, n, face)

    if ytm is None:
        ytm = _solve_ytm(cfs, float(price), m)  # type: ignore[arg-type]
    per_y = ytm / m
    # Always the model price at the solved yield (even when the user passed a price) so the
    # duration/convexity below are internally consistent with the yield.
    p = _price_at(cfs, per_y)

    # Macaulay duration (years): PV-weighted average time to cash flow.
    pv = [cf / (1 + per_y) ** (t + 1) for t, cf in enumerate(cfs)]
    p_model = sum(pv)
    d_mac = sum((t + 1) / m * pvt for t, pvt in enumerate(pv)) / p_model
    d_mod = d_mac / (1 + per_y)
    # Convexity (annual): periods² measure scaled by m².
    conv_periods = sum((t + 1) * (t + 2) * pvt / (1 + per_y) ** 2 for t, pvt in enumerate(pv))
    conv_periods /= p_model
    convexity = conv_periods / (m * m)
    dv01 = d_mod * p * 0.0001  # currency change per 1bp, per `face`

    current_yield = coupon_rate * face / p
    up = -d_mod * 0.01 + 0.5 * convexity * 0.01**2  # +100bp
    down = d_mod * 0.01 + 0.5 * convexity * 0.01**2  # -100bp
    # Par first, with a tolerance — else float noise (~1e-13) reads as a premium.
    par_tol = max(1e-6, face * 1e-9)
    label = "par" if abs(p - face) < par_tol else "premium" if p > face else "discount"

    return {
        "price": round(p, 6),
        "ytm": round(ytm, 8),
        "current_yield": round(current_yield, 8),
        "premium_discount": label,
        "macaulay_duration": round(d_mac, 6),
        "modified_duration": round(d_mod, 6),
        "convexity": round(convexity, 4),
        "dv01": round(dv01, 8),
        "price_change_up_100bp_pct": round(up, 8),
        "price_change_down_100bp_pct": round(down, 8),
        "n_periods": n,
        "coupon_per_period": round(coupon_per_period, 6),
        "periodic_yield": round(per_y, 8),
        "inputs": {
            "coupon_rate": coupon_rate,
            "years_to_maturity": years_to_maturity,
            "frequency": m,
            "face": face,
        },
    }


class BondRequest(BaseModel):
    """Input for option-free fixed-rate bond analytics. Give exactly one of ytm or price."""

    model_config = ConfigDict(allow_inf_nan=False)  # reject NaN/inf inputs

    coupon_rate: float = Field(..., ge=0, le=1, description="Annual coupon rate, decimal (0.04).")
    years_to_maturity: float = Field(..., gt=0, le=100, description="Years to maturity.")
    frequency: int = Field(2, ge=1, le=12, description="Coupon payments per year (default 2).")
    face: float = Field(100.0, gt=0, description="Face/par value (default 100).")
    ytm: float | None = Field(None, gt=-0.1, le=1, description="Yield to maturity, decimal p.a.")
    price: float | None = Field(None, gt=0, description="Clean price per `face` (solve for YTM).")

    @model_validator(mode="after")
    def _one_of_ytm_price(self) -> BondRequest:
        if (self.ytm is None) == (self.price is None):
            raise ValueError("give exactly one of `ytm` or `price`")
        return self


def _markdown(r: dict[str, Any]) -> str:
    face = r["inputs"]["face"]
    return (
        f"**Bond** — price **{num(r['price'], 4)}** ({r['premium_discount']}, per {num(face, 0)} "
        f"face) · YTM {pct(r['ytm'])} · current yield {pct(r['current_yield'])}\n"
        f"  Macaulay {num(r['macaulay_duration'])}y · modified {num(r['modified_duration'])} · "
        f"convexity {num(r['convexity'], 1)} · DV01 {num(r['dv01'], 5)}/face\n"
        f"  ±100bp (duration+convexity): **{pct(r['price_change_down_100bp_pct'])}** on a fall "
        f"vs **{pct(r['price_change_up_100bp_pct'])}** on a rise — the gap is positive convexity.\n"
        f"  (Option-free fixed-rate only; DV01 is additive across positions; for "
        f"callables/MBS/floaters use effective duration, not this analytic measure.)"
    )


def render_bond(req: BondRequest) -> str:
    """Bond markdown renderer — the string an agent sees."""
    return _markdown(
        bond_analytics(
            req.coupon_rate,
            req.years_to_maturity,
            frequency=req.frequency,
            face=req.face,
            ytm=req.ytm,
            price=req.price,
        )
    )
