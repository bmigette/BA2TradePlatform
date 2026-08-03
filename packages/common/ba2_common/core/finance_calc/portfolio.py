"""
Vendored from FinanceHarness (https://github.com/Yijia-Xiao/FinanceHarness),
Apache-2.0 license. Modified for BA2TradePlatform: ToolSpec/ToolResponse layer
removed, ToolError replaced with ValueError, imports localized.

compute.portfolio.performance — risk-adjusted performance from a return series.

Pure-math. From a series of periodic returns (and an optional benchmark), returns the
standard performance report: annualized return/vol, Sharpe, Sortino, Calmar, max drawdown,
and — when a benchmark is given — beta, Jensen's alpha, tracking error, information ratio,
Treynor, and up/down capture. It also reports the statistic practitioners most often skip:
the t-stat of the track record (t = Sharpe x sqrt(years)) and how many years it would take
to reach significance, so a short record is not mistaken for skill.

Annualization uses the sqrt(m) rule and assumes i.i.d. returns.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ba2_common.core.finance_calc.format import num, pct

_MIN_POINTS = 3


def _max_drawdown(returns: list[float]) -> float:
    """Largest peak-to-trough decline of the cumulative wealth path (a negative number)."""
    wealth = 1.0
    peak = 1.0
    mdd = 0.0
    for r in returns:
        wealth *= 1 + r
        peak = max(peak, wealth)
        mdd = min(mdd, wealth / peak - 1)
    return mdd


def performance(
    returns: list[float],
    *,
    periods_per_year: int = 12,
    risk_free_annual: float = 0.0,
    benchmark: list[float] | None = None,
) -> dict[str, Any]:
    """The standard risk-adjusted performance report for a return series."""
    m = periods_per_year
    n = len(returns)
    years = n / m
    rf = risk_free_annual / m  # per-period risk-free (linear approximation)
    root_m = math.sqrt(m)

    mean = statistics.fmean(returns)
    vol = statistics.stdev(returns)  # sample stdev
    ann_return = math.prod(1 + r for r in returns) ** (m / n) - 1  # geometric
    ann_vol = vol * root_m

    sharpe = (mean - rf) / vol * root_m if vol > 0 else None
    downside = math.sqrt(sum(min(r - rf, 0.0) ** 2 for r in returns) / n)
    sortino = (mean - rf) / downside * root_m if downside > 0 else None
    mdd = _max_drawdown(returns)
    calmar = ann_return / abs(mdd) if mdd < 0 else None

    # Track-record significance (the point most often missed): t = Sharpe_ann x sqrt(years).
    # Valid for a negative Sharpe too — a sufficiently bad record is significantly bad.
    t_stat = sharpe * math.sqrt(years) if sharpe is not None else None
    years_to_sig = (2.0 / abs(sharpe)) ** 2 if sharpe else None

    out: dict[str, Any] = {
        "n_obs": n,
        "periods_per_year": m,
        "years": round(years, 4),
        "mean_return": round(mean, 8),
        "volatility": round(vol, 8),
        "annualized_return": round(ann_return, 8),
        "annualized_volatility": round(ann_vol, 8),
        "sharpe": None if sharpe is None else round(sharpe, 6),
        "sortino": None if sortino is None else round(sortino, 6),
        "calmar": None if calmar is None else round(calmar, 6),
        "max_drawdown": round(mdd, 8),
        "downside_deviation": round(downside, 8),
        "t_stat": None if t_stat is None else round(t_stat, 4),
        "years_to_significance": None if years_to_sig is None else round(years_to_sig, 2),
    }

    if benchmark is not None:
        b_mean = statistics.fmean(benchmark)
        b_var = statistics.pvariance(benchmark, b_mean)
        cov = sum((r - mean) * (bb - b_mean) for r, bb in zip(returns, benchmark, strict=True)) / n
        beta = cov / b_var if b_var > 0 else None
        active = [r - bb for r, bb in zip(returns, benchmark, strict=True)]
        te = statistics.stdev(active) if n >= 2 else 0.0
        ir = (mean - b_mean) / te * root_m if te > 0 else None
        # The same significance check applies to the IR: t_IR = IR_annual * sqrt(years).
        ir_t = ir * math.sqrt(years) if ir is not None else None
        alpha_period = (mean - rf) - (beta * (b_mean - rf) if beta is not None else 0.0)
        treynor = (mean - rf) * m / beta if beta not in (None, 0) else None
        up = [(r, bb) for r, bb in zip(returns, benchmark, strict=True) if bb > 0]
        dn = [(r, bb) for r, bb in zip(returns, benchmark, strict=True) if bb < 0]
        up_cap = (
            statistics.fmean([r for r, _ in up]) / statistics.fmean([bb for _, bb in up])
            if up
            else None
        )
        dn_cap = (
            statistics.fmean([r for r, _ in dn]) / statistics.fmean([bb for _, bb in dn])
            if dn
            else None
        )
        out.update(
            {
                "beta": None if beta is None else round(beta, 6),
                "jensen_alpha_annual": round(alpha_period * m, 8),
                "tracking_error": round(te * root_m, 8),
                "information_ratio": None if ir is None else round(ir, 6),
                "information_ratio_t": None if ir_t is None else round(ir_t, 4),
                "treynor": None if treynor is None else round(treynor, 6),
                "up_capture": None if up_cap is None else round(up_cap, 6),
                "down_capture": None if dn_cap is None else round(dn_cap, 6),
            }
        )
    return out


class PerformanceRequest(BaseModel):
    """Input for a risk-adjusted performance report over a return series."""

    model_config = ConfigDict(allow_inf_nan=False)  # reject NaN/inf inputs

    returns: list[float] = Field(
        ..., min_length=_MIN_POINTS, description="Periodic returns, decimals (e.g. [0.02,-0.01])."
    )
    periods_per_year: int = Field(
        12, ge=1, le=366, description="12 monthly, 252 daily, 4 quarterly."
    )
    risk_free_annual: float = Field(0.0, ge=-0.1, le=1, description="Annual risk-free, decimal.")
    benchmark: list[float] | None = Field(
        None, description="Benchmark returns, same length as returns (enables IR, beta, capture)."
    )

    @model_validator(mode="after")
    def _benchmark_length(self) -> PerformanceRequest:
        if self.benchmark is not None and len(self.benchmark) != len(self.returns):
            raise ValueError("benchmark must be the same length as returns")
        return self


def _sig_note(r: dict[str, Any]) -> str:
    if r["t_stat"] is None or r["years_to_significance"] is None:
        return ""
    verdict = (
        "significant" if abs(r["t_stat"]) >= 2 else "too short to distinguish skill from luck"
    )
    return (
        f"  Track record: t = Sharpe×√years = {num(r['t_stat'])} "
        f"(needs ~{num(r['years_to_significance'], 1)}y to reach t=2 — {verdict}).\n"
    )


def _markdown(r: dict[str, Any]) -> str:
    line1 = (
        f"**Portfolio performance** (n={r['n_obs']}, {num(r['years'], 1)}y): "
        f"ann. return {pct(r['annualized_return'])} · ann. vol {pct(r['annualized_volatility'])} · "
        f"**Sharpe {num(r['sharpe'])}**"
    )
    line2 = (
        f"  Sortino {num(r['sortino'])} · Calmar {num(r['calmar'])} · "
        f"max drawdown {pct(r['max_drawdown'])}"
    )
    bench = ""
    if "beta" in r:
        bench = (
            f"\n  vs benchmark: β {num(r['beta'])} · **IR {num(r['information_ratio'])}** · "
            f"Treynor {num(r['treynor'])} · α {pct(r['jensen_alpha_annual'])}/yr · "
            f"capture up {num(r['up_capture'])}/down {num(r['down_capture'])}"
        )
    tail = (
        "  (For a benchmarked mandate the **information ratio** is the right measure, not the "
        "Sharpe; annualization uses √m and assumes i.i.d. returns.)"
    )
    return f"{line1}\n{line2}{bench}\n{_sig_note(r)}{tail}"


def render_performance(req: PerformanceRequest) -> str:
    """Performance markdown renderer — the string an agent sees."""
    return _markdown(
        performance(
            req.returns,
            periods_per_year=req.periods_per_year,
            risk_free_annual=req.risk_free_annual,
            benchmark=req.benchmark,
        )
    )
