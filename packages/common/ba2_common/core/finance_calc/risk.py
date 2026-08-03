"""
Vendored from FinanceHarness (https://github.com/Yijia-Xiao/FinanceHarness),
Apache-2.0 license. Modified for BA2TradePlatform: ToolSpec/ToolResponse layer
removed, ToolError replaced with ValueError, imports localized, and the
correlation matrix reports ±1.0 for zero-variance series that are exact
scalar multiples of each other (upstream reported None there).

Risk family — return helpers, beta, correlation matrix, and Value-at-Risk,
flattened from FinanceHarness's compute.risk.{returns, beta, correlation, var}
into one module.

returns: shared return helpers — pct_returns (simple period-over-period
returns) and align (align series to their most-recent common window so paired
stats compare the same observations). Pure, no network.

beta: beta of an asset vs a benchmark, from price series. Pure-math. From an
asset price series and a benchmark price series, converts to returns, aligns,
and computes β = cov(asset, bench) / var(bench), plus the correlation and R².
A transparent, window-explicit beta to complement a static reference beta.

correlation: return-correlation matrix across named price series. Pure-math.
Takes a set of named price series, converts to returns, aligns to the common
window, and returns the pairwise Pearson correlation matrix. Use to see how
names co-move (diversification, crowding, pair risk).

var: Value-at-Risk of a return series (historical + parametric). Pure-math.
From a price series, the worst expected loss at a confidence level over a
horizon: historical (empirical left-tail quantile) and parametric (normal:
−(μ + zσ)), each scaled by √horizon. Reported as a positive % loss. A
downside-risk lens on a single name.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from ba2_common.core.finance_calc.format import num, pct
from ba2_common.core.finance_calc.series import percentile

# ---------------------------------------------------------------------------
# Return helpers (vendored from compute.risk.returns)
# ---------------------------------------------------------------------------


def pct_returns(prices: list[float]) -> list[float]:
    """Simple period-over-period returns from a price series (skips zero priors)."""
    out: list[float] = []
    for i in range(1, len(prices)):
        prev = prices[i - 1]
        if prev:
            out.append(prices[i] / prev - 1.0)
    return out


def align(*series: list[float]) -> list[list[float]]:
    """Align return series to their common length by keeping the most recent N of each
    — so paired stats (correlation, beta) compare the same observations."""
    n = min((len(s) for s in series), default=0)
    return [s[len(s) - n :] for s in series]


# ---------------------------------------------------------------------------
# Beta (vendored from compute.risk.beta)
# ---------------------------------------------------------------------------

_DESCRIPTION_BETA = (
    "Beta of an asset vs a benchmark from price series (asset_prices + "
    "benchmark_prices — chain `prev:<id>.closes` from data.equity.prices on the stock and "
    "an index ticker like ^GSPC). Converts "
    "to returns, aligns the common window, and returns beta = cov/var(benchmark), "
    "with correlation and R^2. A window-explicit beta vs the static one in reference."
)
_MIN_POINTS = 3


class BetaRequest(BaseModel):
    """Input for estimating asset beta versus a benchmark series."""

    asset_prices: list[float] = Field(
        ..., min_length=_MIN_POINTS, description="Asset price series, e.g. [180, 182, 179, 185]."
    )
    benchmark_prices: list[float] = Field(
        ...,
        min_length=_MIN_POINTS,
        description="Benchmark (e.g. index) price series, e.g. [4500, 4520, 4490, 4550].",
    )
    # length is enforced by Field(min_length=_MIN_POINTS) on both fields — no extra validator.


def compute_beta(asset_prices: list[float], benchmark_prices: list[float]) -> dict[str, Any]:
    """Compute beta from aligned asset and benchmark percentage returns."""

    ra, rb = align(pct_returns(asset_prices), pct_returns(benchmark_prices))
    if len(ra) < 2:
        raise ValueError("not enough overlapping observations to estimate beta")
    var_b = statistics.variance(rb)
    beta = statistics.covariance(ra, rb) / var_b if var_b else None
    corr = statistics.correlation(ra, rb) if var_b and statistics.variance(ra) else None
    return {
        "beta": round(beta, 4) if beta is not None else None,
        "correlation": round(corr, 4) if corr is not None else None,
        "r_squared": round(corr**2, 4) if corr is not None else None,
        "n_obs": len(ra),
    }


def _markdown_beta(res: dict[str, Any]) -> str:
    return (
        f"**Beta** (n={res['n_obs']}): {num(res['beta'])} · "
        f"correlation {num(res['correlation'])} · R² {num(res['r_squared'])}"
    )


def render_beta(req: BetaRequest) -> str:
    """Beta markdown renderer — the string an agent sees."""
    return _markdown_beta(compute_beta(req.asset_prices, req.benchmark_prices))


# ---------------------------------------------------------------------------
# Correlation (vendored from compute.risk.correlation)
# ---------------------------------------------------------------------------

_DESCRIPTION_CORRELATION = (
    "Return-correlation matrix across two or more named price series (pass each as a "
    "price list — chain `prev:<id>.closes` from data.equity.prices). Returns the pairwise "
    "Pearson correlations of their returns over the common window. Use for co-movement, "
    "diversification, and pair risk."
)


class CorrelationRequest(BaseModel):
    """Input for a correlation matrix over named price series."""

    series: dict[str, list[float]] = Field(
        ...,
        min_length=2,
        description='Map of ticker → price list (>=2 tickers, >=3 prices each), '
        'e.g. {"AAPL": [180, 182, 179], "MSFT": [400, 405, 402]}.',
    )

    @model_validator(mode="after")
    def _check_lengths(self) -> Self:
        short = [k for k, v in self.series.items() if len(v) < _MIN_POINTS]
        if short:
            raise ValueError(f"series need >= {_MIN_POINTS} prices; too short: {short}")
        return self


def _proportional_corr(ra: list[float], rb: list[float]) -> float | None:
    """±1.0 when two return series are exact scalar multiples of each other.

    Pearson correlation is undefined (and statistics.correlation raises) when
    either series is constant — but two constant (or near-constant) series that
    ARE proportional still co-move perfectly, so report +1.0 (same direction)
    or -1.0 (opposite). Non-proportional series stay None.
    """
    k: float | None = None
    for a, b in zip(ra, rb, strict=True):
        if a != 0:
            k = b / a
            break
        if b != 0:
            return None  # zero in ra maps to nonzero in rb — not proportional
    if k is None:
        return None  # both all-zero: no co-movement to measure
    if all(
        math.isclose(b, k * a, rel_tol=1e-9, abs_tol=1e-15)
        for a, b in zip(ra, rb, strict=True)
    ):
        return 1.0 if k > 0 else -1.0
    return None


def compute_correlation(series: dict[str, list[float]]) -> dict[str, Any]:
    """Compute pairwise correlations from one common aligned return window."""

    names = list(series)
    rets = {k: pct_returns(v) for k, v in series.items()}
    # Align ALL series to one common (most-recent) window, so every pairwise
    # correlation is measured over the same observations — the standard for a
    # correlation matrix — and n_obs is the actual count used.
    aligned = dict(zip(names, align(*(rets[k] for k in names)), strict=True))
    n_obs = len(next(iter(aligned.values()))) if aligned else 0
    matrix: dict[str, dict[str, float | None]] = {a: {} for a in names}
    for i, a in enumerate(names):
        for b in names[i:]:
            ra, rb = aligned[a], aligned[b]
            # correlation is undefined (and statistics.correlation raises) when either
            # series is constant — report None rather than erroring on valid input,
            # unless the series are exact scalar multiples (perfect ±1 co-movement).
            if n_obs >= 2 and statistics.variance(ra) > 0 and statistics.variance(rb) > 0:
                corr = statistics.correlation(ra, rb)
            elif n_obs >= 2:
                corr = _proportional_corr(ra, rb)
            else:
                corr = None
            c = round(corr, 4) if corr is not None else None
            matrix[a][b] = c
            matrix[b][a] = c
    return {"names": names, "matrix": matrix, "n_obs": n_obs}


def _markdown_correlation(res: dict[str, Any]) -> str:
    names = res["names"]
    head = "corr | " + " | ".join(names)
    rows = [head, "—" * len(head)]
    for a in names:
        rows.append(f"{a} | " + " | ".join(num(res["matrix"][a].get(b)) for b in names))
    return f"**Return correlation** (n={res['n_obs']}):\n" + "\n".join(rows)


def render_correlation(req: CorrelationRequest) -> str:
    """Correlation markdown renderer — the string an agent sees."""
    return _markdown_correlation(compute_correlation(req.series))


# ---------------------------------------------------------------------------
# Value-at-Risk (vendored from compute.risk.var)
# ---------------------------------------------------------------------------

_DESCRIPTION_VAR = (
    "Value-at-Risk for a price series (pass prices, e.g. from data.equity.prices): the "
    "worst expected loss at a confidence level over a horizon, both historical "
    "(empirical tail) and parametric (normal). Returns positive % losses. "
    "confidence default 0.95, horizon_days default 1."
)
_MIN_POINTS_VAR = 5


class VarRequest(BaseModel):
    """Input for value-at-risk over a price series."""

    prices: list[float] = Field(
        ...,
        min_length=_MIN_POINTS_VAR,
        description="Price series (>=5), e.g. [180, 182, 179, 185, 183].",
    )
    confidence: float = Field(0.95, gt=0.5, lt=1, description="Confidence level, e.g. 0.95.")
    horizon_days: int = Field(
        1, ge=1, le=2520, description="Horizon in periods (sqrt-scaled)."
    )  # ceiling is a unit-error rail (~10y), not a limit — √-scaling is O(1), any horizon is cheap


def compute_var(prices: list[float], confidence: float, horizon_days: int) -> dict[str, Any]:
    """Compute historical VaR from percentage returns."""

    rets = pct_returns(prices)
    scale = horizon_days**0.5
    tail = 1 - confidence
    hist = -percentile(sorted(rets), tail) * scale
    mu = statistics.fmean(rets)
    sigma = statistics.pstdev(rets)
    z = statistics.NormalDist().inv_cdf(tail)  # negative
    param = -(mu + z * sigma) * scale
    return {
        "confidence": confidence,
        "horizon_days": horizon_days,
        "historical_var_pct": round(max(hist, 0.0), 6),
        "parametric_var_pct": round(max(param, 0.0), 6),
        "mean_return": round(mu, 6),
        "volatility": round(sigma, 6),
        "n_obs": len(rets),
    }


def _markdown_var(res: dict[str, Any]) -> str:
    return (
        f"**Value-at-Risk** ({pct(res['confidence'])} conf, {res['horizon_days']}d, "
        f"n={res['n_obs']}):\n"
        f"  historical {pct(res['historical_var_pct'])} · "
        f"parametric {pct(res['parametric_var_pct'])} loss\n"
        f"  (per-period vol {pct(res['volatility'])})"
    )


def render_var(req: VarRequest) -> str:
    """VaR markdown renderer — the string an agent sees."""
    return _markdown_var(compute_var(req.prices, req.confidence, req.horizon_days))
