"""
Vendored from FinanceHarness (https://github.com/Yijia-Xiao/FinanceHarness),
Apache-2.0 license. Modified for BA2TradePlatform: ToolSpec/ToolResponse layer
removed, ToolError replaced with ValueError, imports localized.

Valuation family — DCF, DCF sensitivity grid, and WACC, flattened from
FinanceHarness's compute.valuation.{dcf, dcf_sensitivity, wacc} into one module.

dcf: discounted cash flow with two terminal methods. Pure-math intrinsic value
from an explicit FCF schedule + discount rate + terminal method. Returns EV,
equity value (net of debt), intrinsic value/share, and the full PV breakdown.
Dollars in, dollars out — the caller chooses WACC (for FCFF, net_debt matters)
or cost of equity (FCFE, net_debt=0); the tool faithfully discounts whatever
schedule it's given.

dcf_sensitivity: a DCF value across a 2-D driver grid. Reuses the DCF core
(`compute_dcf`) to value every cell of a discount-rate × terminal-driver grid,
so an analyst sees how intrinsic value moves with the two assumptions that
dominate it — and reads a bear/base/bull range straight off the table.
Pure-math; each cell is a faithful DCF, invalid cells (e.g. g ≥ r) are left
blank rather than fudged.

wacc: CAPM cost of equity + optional WACC blend — the discount-rate input to
the valuation pipeline (wacc → dcf → dcf_sensitivity). Pure-math. CAPM only
(risk_free_rate, equity_risk_premium, beta) → cost of equity (WACC = Re). Add
the debt trio (cost_of_debt, tax_rate, debt_to_equity, all-or-none) → the
weighted blend. Re = Rf + β·ERP; WACC = E/V·Re + D/V·Rd·(1−Tc) with weights
from D/E. Exposes cost_of_equity + wacc as top-level fields for
chaining/sensitivity, plus implied_equity_risk_premium and wacc_premium_over_rf.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, ValidationError, model_validator

from ba2_common.core.finance_calc.format import money, num, pct

# ---------------------------------------------------------------------------
# DCF (vendored from compute.valuation.dcf)
# ---------------------------------------------------------------------------

_DESCRIPTION_DCF = (
    "Discounted-cash-flow valuation. Takes an explicit-period FCF schedule "
    "(year 1→N; accepts a prev: chain reference), a discount_rate (decimal), a "
    "terminal_method (gordon_growth needs terminal_growth_rate; exit_multiple "
    "needs terminal_ebitda + terminal_ebitda_multiple), and optional net_debt + "
    "shares_outstanding. Returns enterprise value, equity value, intrinsic value "
    "per share, and the PV breakdown. Pure-math; FCF in dollars consistent with shares."
)


class DCFRequest(BaseModel):
    """Input for a discounted-cash-flow valuation."""

    fcf_schedule: list[float] = Field(
        ...,
        min_length=1,
        max_length=100,  # generous rail for long-life/project-finance assets, not a limit
        description="Explicit-period free cash flows as a flat list, year 1 → year N "
        "(USD), e.g. [1200, 1320, 1450].",
    )
    discount_rate: float = Field(
        ...,
        gt=0,
        lt=1,
        description="Discount rate, decimal (0.10=10%). WACC for FCFF; cost of equity for FCFE.",
    )
    terminal_method: Literal["gordon_growth", "exit_multiple"] = Field(
        "gordon_growth",
        description="gordon_growth = FCF·(1+g)/(r−g); exit_multiple = year-N EBITDA × multiple.",
    )
    terminal_growth_rate: float | None = Field(
        None,
        ge=-0.1,
        lt=1,
        description="Perpetuity growth g, decimal. Required for gordon_growth; must be < discount_rate.",  # noqa: E501
    )
    terminal_ebitda: float | None = Field(
        None, ge=0, description="Year-N EBITDA for the exit-multiple terminal value."
    )
    terminal_ebitda_multiple: float | None = Field(
        None, gt=0, description="EV/EBITDA multiple at the exit horizon."
    )
    net_debt: float = Field(
        0.0, description="Net debt (debt − cash) subtracted from EV for equity value. 0 for FCFE."
    )
    shares_outstanding: float | None = Field(
        None, gt=0, description="Diluted shares for per-share value. Omit for aggregate only."
    )

    @model_validator(mode="after")
    def _check_terminal(self) -> Self:
        if self.terminal_method == "gordon_growth":
            if self.terminal_growth_rate is None:
                raise ValueError("terminal_growth_rate is required for gordon_growth.")
            if self.terminal_growth_rate >= self.discount_rate:
                raise ValueError(
                    f"terminal_growth_rate ({self.terminal_growth_rate}) must be < "
                    f"discount_rate ({self.discount_rate}); the perpetuity is undefined otherwise."
                )
        elif self.terminal_ebitda is None or self.terminal_ebitda_multiple is None:
            raise ValueError(
                "terminal_ebitda and terminal_ebitda_multiple are both required for exit_multiple."
            )
        return self


def compute_dcf(req: DCFRequest) -> dict[str, Any]:
    """Pure-math core (validation already happened at the Pydantic boundary)."""
    r = req.discount_rate
    years = list(range(1, len(req.fcf_schedule) + 1))
    discount_factors = [(1 + r) ** t for t in years]
    pv_fcfs = [fcf / df for fcf, df in zip(req.fcf_schedule, discount_factors, strict=True)]
    sum_pv_explicit = sum(pv_fcfs)

    if req.terminal_method == "gordon_growth":
        g = req.terminal_growth_rate
        fcf_n_plus_1 = req.fcf_schedule[-1] * (1 + g)
        terminal_value = fcf_n_plus_1 / (r - g)
        terminal_inputs = {
            "method": "gordon_growth",
            "terminal_growth_rate": g,
            "fcf_n_plus_1": fcf_n_plus_1,
        }
    else:
        terminal_value = req.terminal_ebitda * req.terminal_ebitda_multiple
        terminal_inputs = {
            "method": "exit_multiple",
            "terminal_ebitda": req.terminal_ebitda,
            "terminal_ebitda_multiple": req.terminal_ebitda_multiple,
        }

    pv_terminal = terminal_value / discount_factors[-1]
    enterprise_value = sum_pv_explicit + pv_terminal
    equity_value = enterprise_value - req.net_debt
    intrinsic_per_share = equity_value / req.shares_outstanding if req.shares_outstanding else None
    tv_share_of_ev = pv_terminal / enterprise_value if enterprise_value else None

    return {
        "years": years,
        "fcf_schedule": list(req.fcf_schedule),
        "discount_factors": discount_factors,
        "pv_fcfs": pv_fcfs,
        "sum_pv_explicit": sum_pv_explicit,
        "terminal_inputs": terminal_inputs,
        "terminal_value": terminal_value,
        "pv_terminal": pv_terminal,
        "enterprise_value": enterprise_value,
        "equity_value": equity_value,
        "intrinsic_per_share": intrinsic_per_share,
        "tv_share_of_ev": tv_share_of_ev,
        "discount_rate": r,
        "net_debt": req.net_debt,
        "shares_outstanding": req.shares_outstanding,
    }


def _markdown_dcf(res: dict[str, Any]) -> str:
    ti = res["terminal_inputs"]
    if ti["method"] == "gordon_growth":
        g = pct(ti["terminal_growth_rate"])
        tline = f"Gordon growth (g={g}, FCF_N+1={money(ti['fcf_n_plus_1'])})"
    else:
        mx = ti["terminal_ebitda_multiple"]
        tline = f"exit multiple ({money(ti['terminal_ebitda'])} EBITDA x {num(mx, 1)})"
    lines = [
        "**DCF valuation**",
        f"Discount rate {pct(res['discount_rate'])} · {len(res['years'])}y explicit · {tline}",
        "",
        f"Sum PV explicit FCF: {money(res['sum_pv_explicit'])}",
        f"Terminal value (yr N): {money(res['terminal_value'])} · PV: {money(res['pv_terminal'])}",
        f"**Enterprise value: {money(res['enterprise_value'])}** · net debt {money(res['net_debt'])}",  # noqa: E501
        f"**Equity value: {money(res['equity_value'])}**",
    ]
    if res["shares_outstanding"]:
        lines.append(f"**Intrinsic value/share: {money(res['intrinsic_per_share'])}**")
    if res["tv_share_of_ev"] is not None:
        lines.append(f"Terminal value is {pct(res['tv_share_of_ev'])} of EV.")
    return "\n".join(lines)


def render_dcf(req: DCFRequest) -> str:
    """DCF markdown renderer — the string an agent sees."""
    return _markdown_dcf(compute_dcf(req))


# ---------------------------------------------------------------------------
# DCF sensitivity (vendored from compute.valuation.dcf_sensitivity)
# ---------------------------------------------------------------------------

_DESCRIPTION_SENSITIVITY = (
    "DCF sensitivity grid: values intrinsic value across a range of discount rates "
    "(rows) and a terminal driver (columns) — terminal_growth_rates for "
    "gordon_growth, or terminal_ebitda_multiples for exit_multiple. Takes the same "
    "FCF schedule / net_debt / shares as the DCF tool plus the two axes, and returns "
    "the value matrix with the bear/base/bull range. Use it to show how the "
    "valuation depends on WACC and terminal assumptions."
)


class DCFSensitivityRequest(BaseModel):
    """Input for a DCF sensitivity matrix over discount and terminal assumptions."""

    fcf_schedule: list[float] = Field(
        ...,
        min_length=1,
        max_length=100,  # generous rail for long-life assets, not a limit
        description="Explicit-period FCFs as a flat list, year 1 → N (USD), "
        "e.g. [1200, 1320, 1450].",
    )
    discount_rates: list[float] = Field(
        ...,
        min_length=1,
        max_length=25,
        description="Discount-rate axis (rows), decimals e.g. [0.09, 0.10, 0.11].",
    )
    terminal_method: Literal["gordon_growth", "exit_multiple"] = "gordon_growth"
    terminal_growth_rates: list[float] | None = Field(
        None,
        max_length=25,
        description="Terminal growth axis (cols) for gordon_growth, decimals "
        "e.g. [0.02, 0.03, 0.04].",
    )
    terminal_ebitda: float | None = Field(
        None, ge=0, description="Year-N EBITDA for exit_multiple cells."
    )
    terminal_ebitda_multiples: list[float] | None = Field(
        None,
        max_length=25,
        description="EV/EBITDA multiple axis (cols) for exit_multiple, e.g. [10, 12, 14].",
    )
    net_debt: float = Field(0.0, description="Net debt subtracted from EV for equity value.")
    shares_outstanding: float | None = Field(
        None, gt=0, description="Diluted shares → per-share grid; omit for equity-value grid."
    )

    @model_validator(mode="after")
    def _check_axes(self) -> Self:
        if self.terminal_method == "gordon_growth":
            if not self.terminal_growth_rates:
                raise ValueError("terminal_growth_rates is required for gordon_growth.")
        elif self.terminal_ebitda is None or not self.terminal_ebitda_multiples:
            raise ValueError(
                "terminal_ebitda and terminal_ebitda_multiples are required for exit_multiple."
            )
        return self


def _col_axis(req: DCFSensitivityRequest) -> list[float]:
    return (
        req.terminal_growth_rates
        if req.terminal_method == "gordon_growth"
        else req.terminal_ebitda_multiples
    ) or []


def _cell_value(req: DCFSensitivityRequest, dr: float, col: float) -> float | None:
    """One DCF at (discount_rate=dr, terminal driver=col). None if the cell is
    not a valid DCF (e.g. g ≥ r) — surfaced as a blank, never fudged."""
    kw: dict[str, Any] = {
        "fcf_schedule": req.fcf_schedule,
        "discount_rate": dr,
        "terminal_method": req.terminal_method,
        "net_debt": req.net_debt,
        "shares_outstanding": req.shares_outstanding,
    }
    if req.terminal_method == "gordon_growth":
        kw["terminal_growth_rate"] = col
    else:
        kw["terminal_ebitda"] = req.terminal_ebitda
        kw["terminal_ebitda_multiple"] = col
    try:
        res = compute_dcf(DCFRequest(**kw))
    except (ValidationError, ValueError, ZeroDivisionError):
        return None
    return res["intrinsic_per_share"] if req.shares_outstanding else res["equity_value"]


def compute_sensitivity(req: DCFSensitivityRequest) -> dict[str, Any]:
    """Compute a DCF grid for the request's discount-rate and terminal axes."""

    cols = _col_axis(req)
    grid = [[_cell_value(req, dr, c) for c in cols] for dr in req.discount_rates]
    valid = [v for row in grid for v in row if v is not None]
    metric = "intrinsic_per_share" if req.shares_outstanding else "equity_value"
    return {
        "metric": metric,
        "terminal_method": req.terminal_method,
        "discount_rates": req.discount_rates,
        "terminal_axis": cols,
        "terminal_axis_label": (
            "terminal_growth_rate"
            if req.terminal_method == "gordon_growth"
            else "terminal_ebitda_multiple"
        ),
        "grid": grid,
        "low": min(valid) if valid else None,
        "high": max(valid) if valid else None,
        "n_valid": len(valid),
    }


def _fmt(v: float | None) -> str:
    return "  —  " if v is None else money(v)


def _markdown_sensitivity(res: dict[str, Any]) -> str:
    per_share = res["metric"] == "intrinsic_per_share"
    is_gordon = res["terminal_method"] == "gordon_growth"
    col_fmt = (lambda c: pct(c)) if is_gordon else (lambda c: f"{num(c, 1)}x")
    header = "WACC \\ " + ("g" if is_gordon else "exit×") + " | " + " | ".join(
        col_fmt(c) for c in res["terminal_axis"]
    )
    rows = [header, "—" * len(header)]
    for dr, row in zip(res["discount_rates"], res["grid"], strict=True):
        rows.append(f"{pct(dr):>6} | " + " | ".join(_fmt(v) for v in row))
    label = "intrinsic value/share" if per_share else "equity value"
    summary = (
        f"\n**Range ({label}): {money(res['low'])} – {money(res['high'])}**"
        if res["n_valid"]
        else "\nNo valid cells (every combination had g ≥ r)."
    )
    title = "**DCF sensitivity** (" + (
        "Gordon growth" if is_gordon else "exit multiple"
    ) + f") — {label}\n"
    return title + "\n".join(rows) + summary


def render_sensitivity(req: DCFSensitivityRequest) -> str:
    """DCF-sensitivity markdown renderer — the string an agent sees."""
    return _markdown_sensitivity(compute_sensitivity(req))


# ---------------------------------------------------------------------------
# WACC (vendored from compute.valuation.wacc)
# ---------------------------------------------------------------------------

_DESCRIPTION_WACC = (
    "Cost of capital via CAPM + optional WACC blend — the discount rate for DCF. "
    "Always: risk_free_rate, equity_risk_premium, beta (CAPM cost of equity). "
    "Optionally cost_of_debt, tax_rate, debt_to_equity (all-or-none) for a WACC blend "
    "with after-tax cost of debt. β can come from data.equity.reference; the "
    "risk-free rate from data.market.rates. Returns cost_of_equity and wacc as "
    "top-level fields plus implied_equity_risk_premium and wacc_premium_over_rf. "
    "Pass rates as decimals (0.045 not 4.5)."
)


class CostOfCapitalRequest(BaseModel):
    """Input for CAPM cost of equity and optional WACC blend."""

    risk_free_rate: float = Field(
        ..., ge=0, lt=1, description="Annual risk-free rate, decimal (0.045)."
    )
    equity_risk_premium: float = Field(
        ..., ge=0, lt=1, description="Equity risk premium (Rm−Rf), decimal."
    )
    beta: float = Field(
        ..., ge=-2, le=5, description="Equity beta. From data.equity.reference or supplied."
    )
    cost_of_debt: float | None = Field(
        None, ge=0, lt=1, description="Pre-tax cost of debt, decimal (all-or-none)."
    )
    tax_rate: float | None = Field(
        None, ge=0, le=1, description="Effective tax rate, decimal (all-or-none)."
    )
    debt_to_equity: float | None = Field(None, ge=0, description="D/E ratio (all-or-none).")

    @model_validator(mode="after")
    def _check_debt_all_or_none(self) -> Self:
        set_count = sum(
            1 for f in (self.cost_of_debt, self.tax_rate, self.debt_to_equity) if f is not None
        )
        if 0 < set_count < 3:
            raise ValueError(
                "cost_of_debt, tax_rate, and debt_to_equity must all be set (WACC blend) "
                f"or all unset (CAPM only). Got {set_count}/3."
            )
        return self


def compute_cost_of_capital(req: CostOfCapitalRequest) -> dict[str, Any]:
    """Pure-math core (Pydantic enforced ranges + all-or-none)."""
    rf, erp, beta = req.risk_free_rate, req.equity_risk_premium, req.beta
    cost_of_equity = rf + beta * erp
    components: dict[str, Any] = {"risk_free_rate": rf, "equity_risk_premium": erp, "beta": beta}

    if req.cost_of_debt is not None:
        rd, tc, de = req.cost_of_debt, req.tax_rate, req.debt_to_equity
        equity_weight = 1.0 / (1.0 + de)
        debt_weight = de / (1.0 + de)
        after_tax_cost_of_debt = rd * (1.0 - tc)
        wacc = equity_weight * cost_of_equity + debt_weight * after_tax_cost_of_debt
        components.update(
            {
                "cost_of_debt": rd,
                "tax_rate": tc,
                "debt_to_equity": de,
                "equity_weight": equity_weight,
                "debt_weight": debt_weight,
                "after_tax_cost_of_debt": after_tax_cost_of_debt,
            }
        )
        method = "CAPM + WACC"
    else:
        wacc = cost_of_equity
        method = "CAPM only"

    return {
        "cost_of_equity": cost_of_equity,
        "wacc": wacc,
        "implied_equity_risk_premium": cost_of_equity - rf,
        "wacc_premium_over_rf": wacc - rf,
        "method": method,
        "components": components,
    }


def _markdown_wacc(res: dict[str, Any]) -> str:
    c = res["components"]
    re_ = pct(res["cost_of_equity"])
    lines = [
        f"**Cost of capital** ({res['method']})",
        f"Re = Rf + b*ERP = {pct(c['risk_free_rate'])} + {num(c['beta'])}*{pct(c['equity_risk_premium'])} = **{re_}**",  # noqa: E501
        f"Implied ERP (Re-Rf = b*ERP): {pct(res['implied_equity_risk_premium'])}",
    ]
    if res["method"] == "CAPM + WACC":
        atrd = pct(c["after_tax_cost_of_debt"])
        lines += [
            f"After-tax Rd = {pct(c['cost_of_debt'])}*(1-{pct(c['tax_rate'])}) = {atrd}",
            f"Weights (D/E={num(c['debt_to_equity'], 3)}): E/V {pct(c['equity_weight'])}, "
            f"D/V {pct(c['debt_weight'])}",
            f"**WACC = {pct(res['wacc'])}** · premium over Rf {pct(res['wacc_premium_over_rf'])}",
        ]
    return "\n".join(lines)


def render_wacc(req: CostOfCapitalRequest) -> str:
    """WACC markdown renderer — the string an agent sees."""
    return _markdown_wacc(compute_cost_of_capital(req))
