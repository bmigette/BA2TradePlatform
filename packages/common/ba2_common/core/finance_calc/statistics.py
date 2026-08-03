"""
Vendored from FinanceHarness (https://github.com/Yijia-Xiao/FinanceHarness),
Apache-2.0 license. Modified for BA2TradePlatform: ToolSpec/ToolResponse layer
removed, ToolError replaced with ValueError, imports localized.

Statistics family — descriptive statistics and single-regressor OLS,
flattened from FinanceHarness's compute.statistics.{descriptive, regression}
into one module.

descriptive: summary statistics & distributional moments. Pure-math. The
quant-research workhorse: from a numeric series, central tendency and
dispersion (sample AND population, since the choice changes the number),
quartiles/IQR, and the higher moments (skewness, excess kurtosis) with the
standard error that says whether they mean anything. The output carries the
domain caveats — which variance convention was used, that higher moments are
noisy at small n (skew SE ~ sqrt(6/n)), and that skewed cross-sections should
be read with the median and IQR, not the mean and sigma. Skewness and excess
kurtosis use the (1/n) standardized-moment formulas with the sample s (a
common convention); other conventions (population s, Excel/CFA
sample-adjusted) differ.

regression: ordinary least squares (single regressor). Pure-math,
dependency-free. Fits y = b0 + b1*x by OLS and returns the full inferential
set: slope and intercept, R^2 and adjusted R^2, standard errors, t-statistics,
two-sided p-values (Student-t via the regularized incomplete beta) and the 95%
confidence interval on the slope. The output carries the domain caveats — the
slope IS beta and the intercept IS alpha when y is asset returns and x is
market returns (don't call alpha outperformance unless its t is significant),
correlation is not causation, and on financial time series serial correlation
inflates these i.i.d.-OLS t-stats, so they are an optimistic bound. Single
regressor only; for multifactor models the same machinery generalizes but
needs matrix algebra.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ba2_common.core.finance_calc.format import num
from ba2_common.core.finance_calc.series import percentile

# ---------------------------------------------------------------------------
# Descriptive statistics (vendored from compute.statistics.descriptive)
# ---------------------------------------------------------------------------

_DESCRIPTION_DESCRIPTIVE = (
    "Summary statistics for a numeric series: mean, median, sample & population standard "
    "deviation and variance, min/max/range, quartiles (p25/median/p75) and IQR, skewness and "
    "excess kurtosis (with the standard error of skewness), coefficient of variation, and a "
    "robust scale (normalized MAD). The quant-research descriptive toolkit — pass any series "
    "(returns, ratios, a peer-set metric)."
)
_MIN_POINTS = 3


def describe(data: list[float]) -> dict[str, Any]:
    """Summary statistics + distributional moments for a numeric series."""
    n = len(data)
    s = sorted(data)
    mean = statistics.fmean(data)
    var_pop = statistics.pvariance(data, mean)
    var_samp = statistics.variance(data, mean)
    std_pop = var_pop**0.5
    std_samp = var_samp**0.5
    median = percentile(s, 0.5)
    p25, p75 = percentile(s, 0.25), percentile(s, 0.75)

    # Higher moments: (1/n) standardized moments using the SAMPLE s (KB's stated convention).
    if std_samp > 0:
        z3 = sum(((x - mean) / std_samp) ** 3 for x in data) / n
        z4 = sum(((x - mean) / std_samp) ** 4 for x in data) / n
        skewness: float | None = round(z3, 6)
        excess_kurtosis: float | None = round(z4 - 3, 6)
    else:
        skewness = excess_kurtosis = None

    mad = statistics.median(abs(x - median) for x in data) * 1.4826  # robust scale estimate
    return {
        "n": n,
        "mean": round(mean, 8),
        "median": round(median, 8),
        "std_sample": round(std_samp, 8),
        "std_population": round(std_pop, 8),
        "variance_sample": round(var_samp, 10),
        "variance_population": round(var_pop, 10),
        "min": round(s[0], 8),
        "max": round(s[-1], 8),
        "range": round(s[-1] - s[0], 8),
        "p25": round(p25, 8),
        "p75": round(p75, 8),
        "iqr": round(p75 - p25, 8),
        "skewness": skewness,
        "excess_kurtosis": excess_kurtosis,
        "skewness_stderr": round((6.0 / n) ** 0.5, 6),  # noise floor for skew
        "coefficient_of_variation": round(std_samp / mean, 6) if mean != 0 else None,
        "mad_normalized": round(mad, 8),
    }


class DescriptiveRequest(BaseModel):
    """Input for descriptive statistics over a numeric series."""

    model_config = ConfigDict(allow_inf_nan=False)  # reject NaN/inf inputs

    data: list[float] = Field(
        ..., min_length=_MIN_POINTS, description="Numeric series (>=3), e.g. [3.1,-2.4,1.8]."
    )


def _shape(r: dict[str, Any]) -> str:
    if r["skewness"] is None:
        return "constant series"
    sk = r["skewness"]
    side = "left-skewed" if sk < -0.1 else "right-skewed" if sk > 0.1 else "~symmetric"
    tails = "fat-tailed" if (r["excess_kurtosis"] or 0) > 1 else "near-normal tails"
    return f"{side}, {tails}"


def _markdown_descriptive(r: dict[str, Any]) -> str:
    cvv = r["coefficient_of_variation"]
    cv = "" if cvv is None else f" · CV {num(cvv)}"
    moments = ""
    if r["skewness"] is not None:
        moments = (
            f"\n  skew {num(r['skewness'])} (±{num(r['skewness_stderr'])} SE) · "
            f"excess kurtosis {num(r['excess_kurtosis'])} — {_shape(r)}"
        )
    return (
        f"**Descriptive statistics** (n={r['n']}): mean {num(r['mean'], 4)} · "
        f"median {num(r['median'], 4)} · std {num(r['std_sample'], 4)} (sample) / "
        f"{num(r['std_population'], 4)} (pop){cv}\n"
        f"  range [{num(r['min'], 4)}, {num(r['max'], 4)}] · "
        f"IQR [{num(r['p25'], 4)}, {num(r['p75'], 4)}] = {num(r['iqr'], 4)}{moments}\n"
        f"  (skew/kurtosis use the 1/n formula with the sample s — other conventions differ; "
        f"at n<~100 they are indicative. Read a skewed cross-section by median & IQR, not mean±σ.)"
    )


def render_descriptive(req: DescriptiveRequest) -> str:
    """Descriptive-statistics markdown renderer — the string an agent sees."""
    return _markdown_descriptive(describe(req.data))


# ---------------------------------------------------------------------------
# OLS regression (vendored from compute.statistics.regression)
# ---------------------------------------------------------------------------

_DESCRIPTION_REGRESSION = (
    "Ordinary least squares of y on a single regressor x (same-length numeric series). "
    "Returns slope and intercept, R^2 and adjusted R^2, Pearson correlation, standard errors, "
    "t-stats, two-sided p-values, and the 95% CI on the slope. For a CAPM beta, pass asset "
    "excess returns as y and market excess returns as x — the slope is beta, the intercept alpha."
)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Numerical Recipes)."""
    fpmin, eps, maxit = 1e-300, 3e-14, 300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, maxit + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _t_two_sided_p(t: float, df: int) -> float:
    """Two-sided p-value P(|T| > |t|) for a Student-t with df degrees of freedom."""
    if df <= 0:
        return float("nan")
    return _betai(df / 2.0, 0.5, df / (df + t * t))


def _t_crit_95(df: int) -> float:
    """The two-sided 95% critical value t_{0.975, df} (bisection on the p-value)."""
    lo, hi = 0.0, 1000.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if _t_two_sided_p(mid, df) > 0.05:
            lo = mid  # not far enough into the tail yet
        else:
            hi = mid
    return (lo + hi) / 2


def ols(x: list[float], y: list[float]) -> dict[str, Any]:
    """Single-regressor OLS with full inference."""
    n = len(x)
    df = n - 2
    xbar, ybar = statistics.fmean(x), statistics.fmean(y)
    sxx = sum((xi - xbar) ** 2 for xi in x)
    sxy = sum((xi - xbar) * (yi - ybar) for xi, yi in zip(x, y, strict=True))
    syy = sum((yi - ybar) ** 2 for yi in y)

    if sxx == 0:  # constant regressor → the slope is unidentified
        raise ValueError("regressor x has zero variance — a regression on a constant x is undefined")

    slope = sxy / sxx
    intercept = ybar - slope * xbar
    sse = sum((yi - (intercept + slope * xi)) ** 2 for xi, yi in zip(x, y, strict=True))
    r2 = 1 - sse / syy if syy > 0 else None
    adj_r2 = 1 - (1 - r2) * (n - 1) / df if (r2 is not None and df > 0) else None
    pearson = sxy / (sxx * syy) ** 0.5 if sxx > 0 and syy > 0 else None

    se_est = (sse / df) ** 0.5 if df > 0 else None
    se_slope = se_est / sxx**0.5 if se_est is not None else None
    se_intercept = (
        se_est * (1.0 / n + xbar * xbar / sxx) ** 0.5 if se_est is not None else None
    )
    t_slope = slope / se_slope if se_slope else None
    t_intercept = intercept / se_intercept if se_intercept else None
    tcrit = _t_crit_95(df) if df > 0 else None
    # A perfect fit (se_slope == 0) has no finite inference — keep t/p/CI all None together.
    ci = (
        [round(slope - tcrit * se_slope, 8), round(slope + tcrit * se_slope, 8)]
        if (tcrit is not None and se_slope)
        else None
    )
    f_stat = (r2 / (1 - r2)) * df if (r2 is not None and r2 < 1 and df > 0) else None

    def _rp(t: float | None) -> float | None:
        return None if t is None else round(_t_two_sided_p(t, df), 6)

    return {
        "n": n,
        "df": df,
        "slope": round(slope, 8),
        "intercept": round(intercept, 8),
        "r_squared": None if r2 is None else round(r2, 6),
        "adjusted_r_squared": None if adj_r2 is None else round(adj_r2, 6),
        "pearson_r": None if pearson is None else round(pearson, 6),
        "std_err_estimate": None if se_est is None else round(se_est, 8),
        "std_err_slope": None if se_slope is None else round(se_slope, 8),
        "std_err_intercept": None if se_intercept is None else round(se_intercept, 8),
        "t_slope": None if t_slope is None else round(t_slope, 6),
        "t_intercept": None if t_intercept is None else round(t_intercept, 6),
        "p_value_slope": _rp(t_slope),
        "p_value_intercept": _rp(t_intercept),
        "slope_ci_95": ci,
        "f_stat": None if f_stat is None else round(f_stat, 6),
    }


class RegressionRequest(BaseModel):
    """Input for a single-regressor OLS (y on x)."""

    model_config = ConfigDict(allow_inf_nan=False)  # reject NaN/inf inputs

    y: list[float] = Field(
        ..., min_length=_MIN_POINTS, description="Dependent series (e.g. asset returns)."
    )
    x: list[float] = Field(
        ..., min_length=_MIN_POINTS, description="Regressor series (e.g. market returns)."
    )

    @model_validator(mode="after")
    def _same_length(self) -> RegressionRequest:
        if len(self.x) != len(self.y):
            raise ValueError("x and y must be the same length")
        return self


def _markdown_regression(r: dict[str, Any]) -> str:
    c = r["slope_ci_95"]
    ci = "" if c is None else f" 95% CI [{num(c[0], 4)}, {num(c[1], 4)}]"
    return (
        f"**OLS** (y ~ x, n={r['n']}): slope **{num(r['slope'], 4)}**{ci} · "
        f"intercept {num(r['intercept'], 4)} · R² {num(r['r_squared'])} "
        f"(adj {num(r['adjusted_r_squared'])})\n"
        f"  t(slope) {num(r['t_slope'])} (p {num(r['p_value_slope'], 3)}) · "
        f"t(intercept) {num(r['t_intercept'])} (p {num(r['p_value_intercept'], 3)}) · "
        f"std err of estimate {num(r['std_err_estimate'], 4)}\n"
        f"  (If y=asset & x=market returns, slope=beta and intercept=alpha — don't call alpha "
        f"outperformance unless its t is significant. Correlation ≠ causation. On financial "
        f"time series serial correlation inflates these i.i.d.-OLS t-stats: treat them as an "
        f"optimistic bound and use HAC/Newey-West SEs for inference.)"
    )


def render_regression(req: RegressionRequest) -> str:
    """OLS-regression markdown renderer — the string an agent sees."""
    return _markdown_regression(ols(req.x, req.y))
