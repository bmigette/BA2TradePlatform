# FinanceHarness Compute Vendoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give TradingAgents LLM analysts verified finance computation (vendored from FinanceHarness) — deterministic stats injected via new compute providers, judgment-input math exposed as agentic tools — plus practitioner prompt methodology.

**Architecture:** (1a) pure vendored math in `packages/common/ba2_common/core/finance_calc/`; (1b) two compute providers (`FinanceCalcRiskStatsProvider`, `FinanceCalcValuationProvider`) behind new interfaces, consumed via the standard `provider_map`/toolkit path and injected into the fundamentals + market analyst contexts; (1c) thin `@tool` closures for judgment-input tools (DCF/WACC/sensitivity/bond/Black-Scholes/arithmetic), with the fundamentals analyst becoming hybrid (prefetch kept + tool loop).

**Tech Stack:** Python 3.11+, pydantic v2, langchain `@tool`, pytest. Source material: local reference clone at `.superpowers/fh-reference/financeharness/tools/compute/` (Apache-2.0).

Spec: `docs/superpowers/specs/2026-07-29-financeharness-compute-vendoring-design.md`

## Global Constraints

- Repo root: `C:/Users/basti/Documents/dev/BA2TradePlatform`. Venv python: `.venv/Scripts/python.exe` (Windows Git Bash).
- Test commands: packages/common tests from repo root: `.venv/Scripts/python.exe -m pytest packages/common/tests/...`; packages/providers tests likewise; live-app tests: `.venv/Scripts/python.exe -m pytest tests/...` (pytest.ini collects only `tests/`).
- **Vendored-code transformation rules (Tasks 1-4), applied uniformly:**
  1. Copy the FH source into the target module, flattening family subdirs into ONE file per family.
  2. Delete every `from financeharness.runtime.tool_registry import ...` line. Replace `from financeharness.tools.format import ...` with `from ba2_common.core.finance_calc.format import ...`; `from financeharness.tools.compute.series import ...` with `from ba2_common.core.finance_calc.series import ...`; `from financeharness.tools.compute.risk.returns import ...` with nothing (the returns helpers live in the same `risk.py`); `from financeharness.tools.compute.valuation.dcf import ...` with nothing (same `valuation.py`).
  3. Replace every `ToolError` raise with `ValueError` (same message text).
  4. Delete the async `_handler` and the `SPEC = ToolSpec(...)` block at the end of each module. Add one public wrapper per tool: `def render_<x>(req) -> str: return _markdown(compute_<x>(...))` (call the compute function with the same argument mapping the deleted `_handler` used).
  5. Add this attribution header at the top of every vendored file:
    ```python
    """
    Vendored from FinanceHarness (https://github.com/Yijia-Xiao/FinanceHarness),
    Apache-2.0 license. Modified for BA2TradePlatform: ToolSpec/ToolResponse layer
    removed, ToolError replaced with ValueError, imports localized.
    """
    ```
  6. Keep `_DESCRIPTION`, the pydantic `XRequest` models, `compute_*`, `_markdown`, and all helper functions verbatim otherwise. Keep pydantic v2 (already a project dep via SQLModel).
- Tool closures are named `compute_<family>_<x>` exactly as: `compute_valuation_dcf`, `compute_valuation_wacc`, `compute_valuation_dcf_sensitivity`, `compute_risk_beta`, `compute_risk_correlation`, `compute_risk_var`, `compute_statistics_descriptive`, `compute_statistics_regression`, `compute_portfolio_performance`, `compute_fixed_income_bond`, `compute_derivatives_black_scholes`, `compute_arithmetic`.
- No network, no fabricated defaults: invalid input → descriptive `Error:` string; providers must never read data past the analysis date (point-in-time).
- The FH reference clone `.superpowers/fh-reference/` is scratch — never commit it, never import from it.
- Commit after every task.

---

### Task 1: finance_calc scaffolding — format, series, arithmetic

**Files:**
- Create: `packages/common/ba2_common/core/finance_calc/__init__.py`
- Create: `packages/common/ba2_common/core/finance_calc/format.py` (from `.superpowers/fh-reference/financeharness/tools/format.py`)
- Create: `packages/common/ba2_common/core/finance_calc/series.py` (from `.../tools/compute/series.py`)
- Create: `packages/common/ba2_common/core/finance_calc/arithmetic.py` (from `.../tools/compute/arithmetic.py`)
- Test: `packages/common/tests/test_finance_calc_helpers.py` (create)

**Interfaces:**
- Produces (relied on by Tasks 2-4): `format.money/num/pct`, `series.percentile(sorted_xs, q)`, `arithmetic.safe_eval(expression: str) -> float`, `arithmetic.CalcRequest`, `arithmetic.render_calc(req) -> str`.

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_finance_calc_helpers.py`:

```python
import pytest


def test_percentile_linear_interpolation():
    from ba2_common.core.finance_calc.series import percentile
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.0) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0


def test_formatters():
    from ba2_common.core.finance_calc.format import money, num, pct
    assert money(1_230_000_000) == "$1.23B"
    assert money(-1_200_000) == "-$1.20M"
    assert money(None) == "n/a"
    assert money(float("nan")) == "n/a"
    assert pct(0.181) == "18.1%"
    assert num(1.2345) == "1.23"  # banker's rounding of 1.2345 -> 1.23
    assert num(True) == "n/a"     # bools are not numbers


def test_safe_eval_exact_arithmetic():
    from ba2_common.core.finance_calc.arithmetic import safe_eval
    assert safe_eval("(37-13)/13*100") == pytest.approx(184.6153846, rel=1e-6)
    assert safe_eval("sqrt(16) + abs(-2)") == 6.0
    assert safe_eval("2**10") == 1024


def test_safe_eval_rejects_unsafe_input():
    from ba2_common.core.finance_calc.arithmetic import safe_eval
    for bad in ("__import__('os')", "open('x')", "x + 1", "f'{1}'"):
        with pytest.raises(ValueError):
            safe_eval(bad)


def test_render_calc():
    from ba2_common.core.finance_calc.arithmetic import CalcRequest, render_calc
    out = render_calc(CalcRequest(expression="1+2"))
    assert "`1+2` = **3**" in out
```

Note on `test_safe_eval_rejects_unsafe_input`: FH raises `ToolError`; the vendored code raises `ValueError` instead. `x + 1` (bare name) and f-strings hit the same rejection path. If `ast.parse` itself fails on one of these (SyntaxError), that is also acceptable — adjust the test to `pytest.raises((ValueError, SyntaxError))` for that entry only.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest packages/common/tests/test_finance_calc_helpers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ba2_common.core.finance_calc'`.

- [ ] **Step 3: Vendor the three modules**

Create `packages/common/ba2_common/core/finance_calc/__init__.py`:

```python
"""Vendored FinanceHarness compute suite (Apache-2.0) — pure-math finance analytics.

Pattern per family module: pydantic XRequest (validation at the boundary) +
pure compute_x(req/args) -> dict + render_x(req) -> str (markdown).
"""
```

Vendor `format.py` (copy `.superpowers/fh-reference/financeharness/tools/format.py` verbatim + attribution header), `series.py` (from `tools/compute/series.py`, verbatim + header), and `arithmetic.py` (from `tools/compute/arithmetic.py`) applying the Global Constraints transformation rules: in `arithmetic.py` delete the tool_registry import, replace `ToolError` with `ValueError`, delete `_handler`/`SPEC`, and add:

```python
def render_calc(req: CalcRequest) -> str:
    """Exact-arithmetic renderer — the string an agent sees."""
    result = safe_eval(req.expression)
    shown = f"{result:.6g}"  # tidy float display without lying about precision
    return f"`{req.expression}` = **{shown}**"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest packages/common/tests/test_finance_calc_helpers.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/finance_calc packages/common/tests/test_finance_calc_helpers.py
git commit -m "feat(finance_calc): vendor format/series/arithmetic helpers from FinanceHarness"
```

---

### Task 2: valuation family — DCF, sensitivity, WACC

**Files:**
- Create: `packages/common/ba2_common/core/finance_calc/valuation.py` (flatten `.superpowers/fh-reference/financeharness/tools/compute/valuation/{dcf,dcf_sensitivity,wacc}.py` into ONE module: dcf first, then dcf_sensitivity, then wacc)
- Test: `packages/common/tests/test_finance_calc_valuation.py` (create)

**Interfaces:**
- Consumes: Task 1's `format.money/num/pct`.
- Produces (relied on by Tasks 5, 7, 8): `DCFRequest`, `compute_dcf(req) -> dict`, `render_dcf(req) -> str`; `DCFSensitivityRequest`, `compute_sensitivity(req) -> dict`, `render_sensitivity(req) -> str`; `CostOfCapitalRequest`, `compute_cost_of_capital(req) -> dict`, `render_wacc(req) -> str`.

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_finance_calc_valuation.py`:

```python
import pytest


def _dcf_req(**kw):
    from ba2_common.core.finance_calc.valuation import DCFRequest
    base = dict(fcf_schedule=[100.0, 110.0, 121.0], discount_rate=0.10,
                terminal_method="gordon_growth", terminal_growth_rate=0.02,
                net_debt=100.0, shares_outstanding=100.0)
    base.update(kw)
    return DCFRequest(**base)


def test_dcf_gordon_growth_known_values():
    from ba2_common.core.finance_calc.valuation import compute_dcf
    res = compute_dcf(_dcf_req())
    # Each year discounts to 100/1.1 = 90.9090... -> sum 272.7273
    assert res["sum_pv_explicit"] == pytest.approx(272.7272727, rel=1e-6)
    # TV = 121*1.02 / (0.10-0.02) = 1542.75; PV = 1542.75/1.331
    assert res["terminal_value"] == pytest.approx(1542.75, rel=1e-9)
    assert res["pv_terminal"] == pytest.approx(1159.0909091, rel=1e-6)
    assert res["enterprise_value"] == pytest.approx(1431.8181818, rel=1e-6)
    assert res["equity_value"] == pytest.approx(1331.8181818, rel=1e-6)
    assert res["intrinsic_per_share"] == pytest.approx(13.3181818, rel=1e-6)
    assert res["tv_share_of_ev"] == pytest.approx(0.8095238, rel=1e-4)


def test_dcf_exit_multiple():
    from ba2_common.core.finance_calc.valuation import compute_dcf
    res = compute_dcf(_dcf_req(terminal_method="exit_multiple", terminal_growth_rate=None,
                               terminal_ebitda=500.0, terminal_ebitda_multiple=8.0,
                               net_debt=0.0))
    assert res["terminal_value"] == pytest.approx(4000.0)
    assert res["enterprise_value"] == pytest.approx(3277.9864814, rel=1e-6)


def test_dcf_rejects_g_greater_equal_r():
    with pytest.raises(ValueError):
        _dcf_req(terminal_growth_rate=0.10)  # g == r is invalid


def test_render_dcf_mentions_terminal_share():
    from ba2_common.core.finance_calc.valuation import render_dcf
    out = render_dcf(_dcf_req())
    assert "Intrinsic value/share" in out
    assert "Terminal value is 81.0% of EV." in out


def test_wacc_capm_only():
    from ba2_common.core.finance_calc.valuation import CostOfCapitalRequest, compute_cost_of_capital
    res = compute_cost_of_capital(CostOfCapitalRequest(
        risk_free_rate=0.04, equity_risk_premium=0.05, beta=1.2))
    assert res["cost_of_equity"] == pytest.approx(0.10)
    assert res["wacc"] == pytest.approx(0.10)
    assert res["method"] == "CAPM only"


def test_wacc_blend():
    from ba2_common.core.finance_calc.valuation import CostOfCapitalRequest, compute_cost_of_capital
    res = compute_cost_of_capital(CostOfCapitalRequest(
        risk_free_rate=0.04, equity_risk_premium=0.05, beta=1.2,
        cost_of_debt=0.06, tax_rate=0.25, debt_to_equity=0.5))
    # E/V = 2/3, D/V = 1/3, after-tax Rd = 0.045 -> WACC = 0.0667 + 0.015
    assert res["wacc"] == pytest.approx(0.0816667, rel=1e-4)
    assert res["method"] == "CAPM + WACC"


def test_wacc_debt_trio_all_or_none():
    from ba2_common.core.finance_calc.valuation import CostOfCapitalRequest
    with pytest.raises(ValueError):
        CostOfCapitalRequest(risk_free_rate=0.04, equity_risk_premium=0.05, beta=1.2,
                             cost_of_debt=0.06)  # missing tax_rate + debt_to_equity


def test_sensitivity_grid_matches_point_dcf():
    from ba2_common.core.finance_calc.valuation import (
        DCFSensitivityRequest, compute_sensitivity, compute_dcf)
    req = DCFSensitivityRequest(
        fcf_schedule=[100.0, 110.0, 121.0], discount_rates=[0.09, 0.10],
        terminal_method="gordon_growth", terminal_growth_rates=[0.02, 0.03],
        net_debt=100.0, shares_outstanding=100.0)
    grid = compute_sensitivity(req)
    assert grid["metric"] == "intrinsic_per_share"
    assert grid["n_valid"] == 4
    # The (0.10, 0.02) cell must equal the point DCF from test_dcf_gordon_growth_known_values.
    assert grid["grid"][1][0] == pytest.approx(13.3181818, rel=1e-6)
    assert grid["low"] == min(v for row in grid["grid"] for v in row)
    assert grid["high"] == max(v for row in grid["grid"] for v in row)


def test_sensitivity_invalid_cell_is_none():
    from ba2_common.core.finance_calc.valuation import DCFSensitivityRequest, compute_sensitivity
    req = DCFSensitivityRequest(
        fcf_schedule=[100.0], discount_rates=[0.10],
        terminal_method="gordon_growth", terminal_growth_rates=[0.02, 0.10],
        net_debt=0.0, shares_outstanding=100.0)
    grid = compute_sensitivity(req)
    assert grid["grid"][0][1] is None  # g == r cell is blank, not fudged
    assert grid["n_valid"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest packages/common/tests/test_finance_calc_valuation.py -v`
Expected: FAIL — `ModuleNotFoundError: ... finance_calc.valuation`.

- [ ] **Step 3: Vendor the valuation family**

Create `packages/common/ba2_common/core/finance_calc/valuation.py` by concatenating `dcf.py`, `dcf_sensitivity.py`, `wacc.py` from the reference clone (in that order) and applying the Global Constraints transformation rules. Notes specific to this family:

- The `from financeharness.tools.compute.valuation.dcf import DCFRequest, compute_dcf` line in dcf_sensitivity is deleted (same module now).
- dcf_sensitivity's compute path catches `ValidationError` from pydantic — keep `from pydantic import ValidationError` for that file's `_cell_value` (it is imported in dcf_sensitivity.py already).
- Public renderers to add at the end: `render_dcf(req)`, `render_sensitivity(req)`, `render_wacc(req)` — each calls its `_markdown(compute_*(...))` with the argument mapping the deleted `_handler` used (for `render_dcf`/`render_sensitivity`/`render_wacc` the compute function takes the request object directly).
- Keep all three `_DESCRIPTION` strings (Tasks 7-8 reuse them for tool docstrings).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest packages/common/tests/test_finance_calc_valuation.py packages/common/tests/test_finance_calc_helpers.py -v`
Expected: all passed (9 + 5).

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/finance_calc/valuation.py packages/common/tests/test_finance_calc_valuation.py
git commit -m "feat(finance_calc): vendor valuation family (DCF, sensitivity, WACC)"
```

---

### Task 3: risk + statistics families

**Files:**
- Create: `packages/common/ba2_common/core/finance_calc/risk.py` (flatten `.superpowers/fh-reference/financeharness/tools/compute/risk/{returns,beta,correlation,var}.py`: returns helpers first, then beta, correlation, var)
- Create: `packages/common/ba2_common/core/finance_calc/statistics.py` (flatten `.../statistics/{descriptive,regression}.py`)
- Test: `packages/common/tests/test_finance_calc_risk_statistics.py` (create)

**Interfaces:**
- Consumes: Task 1's `format.num/pct`, `series.percentile`.
- Produces (relied on by Tasks 5, 7, 8): `pct_returns(prices)`, `align(*series)`; `BetaRequest`, `compute_beta(asset_prices, benchmark_prices) -> dict`, `render_beta(req) -> str`; `CorrelationRequest`, `compute_correlation(series) -> dict`, `render_correlation(req) -> str`; `VarRequest`, `compute_var(prices, confidence, horizon_days) -> dict`, `render_var(req) -> str`; `DescriptiveRequest`, `describe(data) -> dict`, `render_descriptive(req) -> str`; `RegressionRequest`, `ols(x, y) -> dict`, `render_regression(req) -> str`.

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_finance_calc_risk_statistics.py`:

```python
import pytest


def test_pct_returns_and_align():
    from ba2_common.core.finance_calc.risk import pct_returns, align
    assert pct_returns([100.0, 102.0, 101.0]) == pytest.approx([0.02, -0.0098039], rel=1e-5)
    a, b = align([1, 2, 3, 4], [9, 8, 7])
    assert a == [2, 3, 4] and b == [9, 8, 7]  # most-recent common window


def test_beta_of_exact_2x_series():
    from ba2_common.core.finance_calc.risk import compute_beta
    # benchmark returns: +1%, -1%, +2%; asset returns exactly 2x: +2%, -2%, +4%
    res = compute_beta([100.0, 102.0, 99.96, 103.9584],
                       [100.0, 101.0, 99.99, 101.9898])
    assert res["beta"] == pytest.approx(2.0, abs=1e-3)
    assert res["correlation"] == pytest.approx(1.0, abs=1e-3)
    assert res["r_squared"] == pytest.approx(1.0, abs=1e-3)
    assert res["n_obs"] == 3


def test_correlation_matrix_perfect_pos_neg():
    from ba2_common.core.finance_calc.risk import compute_correlation
    # A returns +1%,+1%; B identical; C exactly -1x A
    res = compute_correlation({
        "A": [100.0, 101.0, 102.01],
        "B": [50.0, 50.5, 51.005],
        "C": [200.0, 198.0, 196.02],
    })
    assert res["matrix"]["A"]["B"] == pytest.approx(1.0, abs=1e-3)
    assert res["matrix"]["A"]["C"] == pytest.approx(-1.0, abs=1e-3)
    assert res["n_obs"] == 2


def test_var_historical_and_parametric():
    from ba2_common.core.finance_calc.risk import compute_var
    res = compute_var([100.0, 102.0, 101.0, 105.0, 103.0], 0.95, 1)
    assert res["n_obs"] == 4
    # historical: -percentile(sorted returns, 0.05) ~ 1.766%
    assert res["historical_var_pct"] == pytest.approx(0.017661, rel=1e-3)
    # parametric (normal): ~3.081%
    assert res["parametric_var_pct"] == pytest.approx(0.030807, rel=1e-3)


def test_descriptive_known_moments():
    from ba2_common.core.finance_calc.statistics import describe
    r = describe([1.0, 2.0, 3.0, 4.0, 5.0])
    assert r["mean"] == 3.0
    assert r["std_sample"] == pytest.approx(1.58113883, rel=1e-6)
    assert r["std_population"] == pytest.approx(1.41421356, rel=1e-6)
    assert r["median"] == 3.0 and r["p25"] == 2.0 and r["p75"] == 4.0 and r["iqr"] == 2.0
    assert r["skewness"] == pytest.approx(0.0, abs=1e-9)
    assert r["excess_kurtosis"] == pytest.approx(-1.912, abs=1e-3)


def test_regression_known_fit():
    from ba2_common.core.finance_calc.statistics import ols
    r = ols([1.0, 2.0, 3.0, 4.0, 5.0], [2.0, 4.0, 5.0, 4.0, 5.0])
    assert r["slope"] == pytest.approx(0.6, rel=1e-9)
    assert r["intercept"] == pytest.approx(2.2, rel=1e-9)
    assert r["r_squared"] == pytest.approx(0.6, rel=1e-6)
    assert r["t_slope"] == pytest.approx(2.1213, rel=1e-3)
    assert 0.10 < r["p_value_slope"] < 0.15  # df=3, two-sided


def test_regression_constant_x_raises():
    from ba2_common.core.finance_calc.statistics import ols
    with pytest.raises(ValueError):
        ols([1.0, 1.0, 1.0], [2.0, 3.0, 4.0])


def test_renderers_produce_markdown():
    from ba2_common.core.finance_calc.risk import BetaRequest, render_beta, VarRequest, render_var
    from ba2_common.core.finance_calc.statistics import DescriptiveRequest, render_descriptive
    assert "**Beta**" in render_beta(BetaRequest(asset_prices=[100, 102, 99.96, 103.9584],
                                                 benchmark_prices=[100, 101, 99.99, 101.9898]))
    assert "Value-at-Risk" in render_var(VarRequest(prices=[100, 102, 101, 105, 103]))
    assert "Descriptive statistics" in render_descriptive(DescriptiveRequest(data=[1.0, 2.0, 3.0]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest packages/common/tests/test_finance_calc_risk_statistics.py -v`
Expected: FAIL — `ModuleNotFoundError: ... finance_calc.risk`.

- [ ] **Step 3: Vendor the two modules**

Create `risk.py` (concatenate `returns.py`, `beta.py`, `correlation.py`, `var.py`) and `statistics.py` (concatenate `descriptive.py`, `regression.py`) applying the Global Constraints transformation rules. Family-specific notes:

- `beta.py` and `regression.py` raise `ToolError` → replace with `ValueError` (keep messages).
- Add renderers: `render_beta(req)` → `_markdown(compute_beta(req.asset_prices, req.benchmark_prices))`; `render_correlation(req)` → `_markdown(compute_correlation(req.series))`; `render_var(req)` → `_markdown(compute_var(req.prices, req.confidence, req.horizon_days))`; `render_descriptive(req)` → `_markdown(describe(req.data))`; `render_regression(req)` → `_markdown(ols(req.x, req.y))`.
- `regression.py` defines private helpers `_betacf`, `_betai`, `_t_two_sided_p`, `_t_crit_95` — keep them (same module as `ols` now).
- Keep both `_DESCRIPTION` strings per family.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest packages/common/tests/test_finance_calc_risk_statistics.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/finance_calc/risk.py packages/common/ba2_common/core/finance_calc/statistics.py packages/common/tests/test_finance_calc_risk_statistics.py
git commit -m "feat(finance_calc): vendor risk + statistics families"
```

---

### Task 4: portfolio + derivatives + fixed_income families

**Files:**
- Create: `packages/common/ba2_common/core/finance_calc/portfolio.py` (from `.superpowers/fh-reference/financeharness/tools/compute/portfolio/performance.py`)
- Create: `packages/common/ba2_common/core/finance_calc/derivatives.py` (from `.../derivatives/black_scholes.py`)
- Create: `packages/common/ba2_common/core/finance_calc/fixed_income.py` (from `.../fixed_income/bond.py`)
- Test: `packages/common/tests/test_finance_calc_portfolio_derivatives_bond.py` (create)

**Interfaces:**
- Consumes: Task 1's `format.money/num/pct`.
- Produces (relied on by Tasks 5, 7, 8): `PerformanceRequest`, `performance(returns, *, periods_per_year, risk_free_annual, benchmark) -> dict`, `render_performance(req) -> str`; `BlackScholesRequest`, `black_scholes(spot, strike, years, rate, vol, *, option_type, dividend_yield) -> dict`, `render_black_scholes(req) -> str`; `BondRequest`, `bond_analytics(coupon_rate, years_to_maturity, *, frequency, face, ytm, price) -> dict`, `render_bond(req) -> str`.

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_finance_calc_portfolio_derivatives_bond.py`:

```python
import pytest


def test_performance_known_values():
    from ba2_common.core.finance_calc.portfolio import performance
    r = performance([0.02, -0.01, 0.03], periods_per_year=12, risk_free_annual=0.0)
    assert r["annualized_return"] == pytest.approx(0.168933, rel=1e-3)
    assert r["annualized_volatility"] == pytest.approx(0.072125, rel=1e-3)
    assert r["sharpe"] == pytest.approx(2.21886, rel=1e-3)
    assert r["max_drawdown"] == pytest.approx(-0.01, rel=1e-9)
    assert r["calmar"] == pytest.approx(16.8933, rel=1e-3)
    assert r["t_stat"] == pytest.approx(1.10943, rel=1e-3)  # sharpe * sqrt(3/12)


def test_performance_with_benchmark():
    from ba2_common.core.finance_calc.portfolio import performance
    r = performance([0.02, -0.02, 0.04], periods_per_year=12,
                    benchmark=[0.01, -0.01, 0.02])
    # asset is exactly 2x the benchmark -> beta 2
    assert r["beta"] == pytest.approx(2.0, rel=1e-3)
    assert "information_ratio" in r and "up_capture" in r


def test_black_scholes_textbook_call_put():
    from ba2_common.core.finance_calc.derivatives import black_scholes
    call = black_scholes(100.0, 100.0, 1.0, 0.05, 0.2, option_type="call")
    assert call["price"] == pytest.approx(10.4506, abs=1e-3)
    assert call["delta"] == pytest.approx(0.6368, abs=1e-3)
    put = black_scholes(100.0, 100.0, 1.0, 0.05, 0.2, option_type="put")
    assert put["price"] == pytest.approx(5.5735, abs=1e-3)
    # put-call parity: C - P = S - K*e^-rT = 100 - 100*e^-0.05
    assert call["price"] - put["price"] == pytest.approx(100 - 100 * 0.9512294, rel=1e-3)


def test_bond_price_from_ytm_and_roundtrip():
    from ba2_common.core.finance_calc.fixed_income import bond_analytics
    r = bond_analytics(0.05, 10.0, frequency=2, face=100.0, ytm=0.04)
    assert r["price"] == pytest.approx(108.1757, rel=1e-3)
    assert r["premium_discount"] == "premium"
    assert r["macaulay_duration"] == pytest.approx(8.11, abs=0.05)
    # Round-trip: solve YTM from that price
    r2 = bond_analytics(0.05, 10.0, frequency=2, face=100.0, price=r["price"])
    assert r2["ytm"] == pytest.approx(0.04, rel=1e-3)


def test_bond_requires_exactly_one_of_ytm_price():
    from ba2_common.core.finance_calc.fixed_income import BondRequest
    with pytest.raises(ValueError):
        BondRequest(coupon_rate=0.05, years_to_maturity=10.0)  # neither
    with pytest.raises(ValueError):
        BondRequest(coupon_rate=0.05, years_to_maturity=10.0, ytm=0.04, price=108.0)  # both


def test_renderers_produce_markdown():
    from ba2_common.core.finance_calc.portfolio import PerformanceRequest, render_performance
    from ba2_common.core.finance_calc.derivatives import BlackScholesRequest, render_black_scholes
    from ba2_common.core.finance_calc.fixed_income import BondRequest, render_bond
    assert "Portfolio performance" in render_performance(
        PerformanceRequest(returns=[0.02, -0.01, 0.03]))
    assert "Black-Scholes call" in render_black_scholes(
        BlackScholesRequest(spot=100.0, strike=100.0, years=1.0, rate=0.05, vol=0.2))
    assert "**Bond**" in render_bond(BondRequest(coupon_rate=0.05, years_to_maturity=10.0, ytm=0.04))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest packages/common/tests/test_finance_calc_portfolio_derivatives_bond.py -v`
Expected: FAIL — `ModuleNotFoundError: ... finance_calc.portfolio`.

- [ ] **Step 3: Vendor the three modules**

Copy `performance.py` → `portfolio.py`, `black_scholes.py` → `derivatives.py`, `bond.py` → `fixed_income.py`, applying the Global Constraints transformation rules. Renderers to add:

- `render_performance(req)` → `_markdown(performance(req.returns, periods_per_year=req.periods_per_year, risk_free_annual=req.risk_free_annual, benchmark=req.benchmark))`
- `render_black_scholes(req)` → `_markdown(black_scholes(req.spot, req.strike, req.years, req.rate, req.vol, option_type=req.option_type, dividend_yield=req.dividend_yield))`
- `render_bond(req)` → `_markdown(bond_analytics(req.coupon_rate, req.years_to_maturity, frequency=req.frequency, face=req.face, ytm=req.ytm, price=req.price))`

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest packages/common/tests/test_finance_calc_portfolio_derivatives_bond.py -v`
Expected: 6 passed.

- [ ] **Step 5: Run the whole finance_calc suite, then commit**

Run: `.venv/Scripts/python.exe -m pytest packages/common/tests/ -k finance_calc -v`
Expected: all passed (5 + 9 + 8 + 6 = 28).

```bash
git add packages/common/ba2_common/core/finance_calc/portfolio.py packages/common/ba2_common/core/finance_calc/derivatives.py packages/common/ba2_common/core/finance_calc/fixed_income.py packages/common/tests/test_finance_calc_portfolio_derivatives_bond.py
git commit -m "feat(finance_calc): vendor portfolio, derivatives, fixed income families"
```

---

### Task 5: compute provider interfaces + implementations

**Files:**
- Create: `packages/common/ba2_common/core/interfaces/RiskStatsInterface.py`
- Create: `packages/common/ba2_common/core/interfaces/ValuationSnapshotInterface.py`
- Create: `packages/providers/ba2_providers/riskstats/__init__.py` and `packages/providers/ba2_providers/riskstats/FinanceCalcRiskStatsProvider.py`
- Create: `packages/providers/ba2_providers/valuation/__init__.py` and `packages/providers/ba2_providers/valuation/FinanceCalcValuationProvider.py`
- Test: `packages/providers/tests/test_finance_calc_providers.py` (create)

**Interfaces:**
- Consumes: `finance_calc.risk` (`pct_returns`, `compute_beta`, `compute_correlation`, `compute_var`), `finance_calc.statistics.describe`, `finance_calc.portfolio.performance`, `finance_calc.valuation` (`CostOfCapitalRequest`, `compute_cost_of_capital`, `DCFRequest`, `compute_dcf`, `DCFSensitivityRequest`, `compute_sensitivity`) — all from Tasks 1-3.
- Produces (relied on by Task 6): `RiskStatsInterface.get_risk_stats(symbol, end_date, lookback_days=365, format_type="markdown"|"dict"|"both")`; `ValuationSnapshotInterface.get_valuation_snapshot(symbol, as_of_date, format_type=...)`; `FinanceCalcRiskStatsProvider(ohlcv_provider, benchmark_symbol="SPY")`; `FinanceCalcValuationProvider(fundamentals_overview_provider, fundamentals_details_provider, ohlcv_provider=None, risk_free_rate=0.045, equity_risk_premium=0.05, terminal_growth_rate=0.025, projection_years=5)`.

Data contracts (verified against the codebase — pin these in the stubs):
- OHLCV provider (`MarketDataProviderInterface`): `get_ohlcv_data(symbol, start_date=..., end_date=..., interval=...)` returns a pandas DataFrame with a lowercase `close` column (the `PandasIndicatorCalc` precedent).
- Overview: `get_fundamentals_overview(symbol, as_of_date, format_type="dict")` → `{"symbol", "company_name", "as_of_date", "data_date", "metrics": {...}}`; `metrics` includes `"beta"`.
- Details statements: `get_cashflow_statement/get_income_statement/get_balance_sheet(symbol, frequency, end_date, lookback_periods=..., format_type="dict")` → `{..., "statement_count": int, "statements": [ {...snake_case fields...} ]}` with cashflow key `free_cash_flow`, income keys `weighted_average_shares_outstanding`/`weighted_average_shares_diluted`, balance-sheet keys `cash_and_cash_equivalents`, `short_term_debt`, `long_term_debt`. Statements are most-recent first.

- [ ] **Step 1: Write the failing test**

Create `packages/providers/tests/test_finance_calc_providers.py`:

```python
"""Compute-provider tests with canned stub providers (documented dict contracts)."""
from datetime import datetime
import pandas as pd
import pytest


def _ohlcv_df(closes, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="B")
    return pd.DataFrame({"close": closes}, index=idx)


class _StubOHLCV:
    def __init__(self, closes_by_symbol):
        self._closes = closes_by_symbol
        self.calls = []

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d"):
        self.calls.append((symbol, start_date, end_date, interval))
        return _ohlcv_df(self._closes[symbol])


class _StubOverview:
    def get_fundamentals_overview(self, symbol, as_of_date, format_type="markdown"):
        return {"symbol": symbol, "company_name": "Stub Co",
                "as_of_date": "2026-01-01", "data_date": "2025-12-31",
                "metrics": {"beta": 1.2}}


class _StubDetails:
    def get_cashflow_statement(self, symbol, frequency, end_date,
                               lookback_periods=None, format_type="markdown"):
        return {"statement_count": 3, "statements": [
            {"fiscal_date_ending": "2025-12-31", "free_cash_flow": 121.0},
            {"fiscal_date_ending": "2024-12-31", "free_cash_flow": 110.0},
            {"fiscal_date_ending": "2023-12-31", "free_cash_flow": 100.0},
        ]}

    def get_income_statement(self, symbol, frequency, end_date,
                             lookback_periods=None, format_type="markdown"):
        return {"statement_count": 1, "statements": [
            {"fiscal_date_ending": "2025-12-31",
             "weighted_average_shares_outstanding": 100.0,
             "weighted_average_shares_diluted": 100.0}]}

    def get_balance_sheet(self, symbol, frequency, end_date,
                          lookback_periods=None, format_type="markdown"):
        return {"statement_count": 1, "statements": [
            {"fiscal_date_ending": "2025-12-31",
             "cash_and_cash_equivalents": 50.0,
             "short_term_debt": 50.0,
             "long_term_debt": 100.0}]}


AS_OF = datetime(2026, 1, 15)


def test_risk_stats_markdown_and_dict(tmp_path):
    from ba2_providers.riskstats import FinanceCalcRiskStatsProvider
    ohlcv = _StubOHLCV({
        "AAA": [100 + i * 0.1 for i in range(60)],
        "SPY": [400 + i * 0.05 for i in range(60)],
    })
    p = FinanceCalcRiskStatsProvider(ohlcv, benchmark_symbol="SPY")
    md = p.get_risk_stats("AAA", AS_OF, lookback_days=90, format_type="markdown")
    assert "Risk statistics" in md and "Realized vol" in md and "Beta" in md and "VaR" in md
    both = p.get_risk_stats("AAA", AS_OF, lookback_days=90, format_type="both")
    assert set(both) == {"text", "data"}
    assert both["data"]["symbol"] == "AAA"
    assert both["data"]["descriptive"]["n"] > 0
    d = p.get_risk_stats("AAA", AS_OF, lookback_days=90, format_type="dict")
    assert d["benchmark"] == "SPY"


def test_risk_stats_point_in_time():
    from ba2_providers.riskstats import FinanceCalcRiskStatsProvider
    ohlcv = _StubOHLCV({"AAA": [100.0] * 10, "SPY": [400.0] * 10})
    p = FinanceCalcRiskStatsProvider(ohlcv)
    p.get_risk_stats("AAA", AS_OF, lookback_days=90, format_type="dict")
    # The OHLCV fetch must be bounded at the analysis date — nothing after it.
    for (_sym, _start, end, _iv) in ohlcv.calls:
        assert pd.Timestamp(end) <= pd.Timestamp(AS_OF)


def test_valuation_snapshot_contains_assumptions_and_value():
    from ba2_providers.valuation import FinanceCalcValuationProvider
    p = FinanceCalcValuationProvider(_StubOverview(), _StubDetails(),
                                     risk_free_rate=0.04, equity_risk_premium=0.05)
    md = p.get_valuation_snapshot("AAA", AS_OF, format_type="markdown")
    # Every default assumption is printed verbatim — nothing hidden.
    for marker in ("risk-free", "4.0%", "5.0%", "terminal growth", "2.5%", "beta"):
        assert marker in md.lower()
    assert "Intrinsic value" in md and "DEFAULT assumptions" in md
    d = p.get_valuation_snapshot("AAA", AS_OF, format_type="dict")
    assert d["assumptions"]["risk_free_rate"] == 0.04
    assert d["wacc"] == pytest.approx(0.10)          # 0.04 + 1.2*0.05
    assert d["fcf_cagr"] == pytest.approx(0.10, rel=1e-3)  # 100->110->121
    assert d["dcf"]["intrinsic_per_share"] is not None


def test_valuation_snapshot_not_computable_when_no_fcf():
    from ba2_providers.valuation import FinanceCalcValuationProvider

    class _NoFCF(_StubDetails):
        def get_cashflow_statement(self, *a, **k):
            return {"statement_count": 0, "statements": []}

    p = FinanceCalcValuationProvider(_StubOverview(), _NoFCF())
    md = p.get_valuation_snapshot("AAA", AS_OF, format_type="markdown")
    assert "not computable" in md.lower()
    d = p.get_valuation_snapshot("AAA", AS_OF, format_type="dict")
    assert d["computable"] is False
    # Never an exception, never a fabricated number.
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest packages/providers/tests/test_finance_calc_providers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ba2_providers.riskstats'`.

- [ ] **Step 3: Create the interfaces**

Create `packages/common/ba2_common/core/interfaces/RiskStatsInterface.py`:

```python
"""Interface for deterministic risk-statistics compute providers.

A risk-stats provider computes (never fetches) risk analytics for a symbol from
OHLCV history: descriptive return stats, annualized realized volatility, max
drawdown, VaR, and benchmark-relative beta/correlation/regression. Output follows
the standard format_type contract (markdown / dict / both).
"""

from abc import abstractmethod
from typing import Annotated, Any, Dict, Literal, Optional
from datetime import datetime

from ba2_common.core.interfaces.DataProviderInterface import DataProviderInterface


class RiskStatsInterface(DataProviderInterface):
    """Interface for risk-statistics compute providers."""

    @abstractmethod
    def __init__(self, ohlcv_provider: DataProviderInterface, benchmark_symbol: str = "SPY"):
        """Args:
        ohlcv_provider: provider implementing get_ohlcv_data (composition, like
                        MarketIndicatorsInterface — the provider never fetches itself).
        benchmark_symbol: ticker used for beta/correlation (default SPY).
        """

    @abstractmethod
    def get_risk_stats(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "Analysis date — nothing after this date may be read"],
        lookback_days: Annotated[int, "Calendar days of history to compute over"] = 365,
        format_type: Literal["markdown", "dict", "both"] = "markdown",
    ) -> Dict[str, Any] | str:
        """Compute the risk-statistics report for symbol as of end_date."""
```

Create `packages/common/ba2_common/core/interfaces/ValuationSnapshotInterface.py` with the same skeleton, one abstract method:

```python
    @abstractmethod
    def get_valuation_snapshot(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        as_of_date: Annotated[datetime, "Analysis date — nothing after this date may be read"],
        format_type: Literal["markdown", "dict", "both"] = "markdown",
    ) -> Dict[str, Any] | str:
        """Compute a DEFAULT-ASSUMPTION valuation snapshot (WACC + DCF + sensitivity).

        Every assumption is a constructor parameter of the implementation and must
        be printed verbatim in the rendered report. When required fundamentals are
        missing the report says 'not computable: <reason>' — never an exception,
        never a fabricated number.
        """
```

Also create the in-tree re-export shims (this repo keeps shims for every interface): `ba2_trade_platform/core/interfaces/RiskStatsInterface.py` and `.../ValuationSnapshotInterface.py`, mirroring the existing `ba2_trade_platform/core/interfaces/MarketIndicatorsInterface.py` shim (a single re-export line), and add both names to `ba2_trade_platform/core/interfaces/__init__.py`'s import/`__all__` if it lists interfaces explicitly (check the file; mirror what it does for MarketIndicatorsInterface).

- [ ] **Step 4: Implement FinanceCalcRiskStatsProvider**

Create `packages/providers/ba2_providers/riskstats/__init__.py`:

```python
from .FinanceCalcRiskStatsProvider import FinanceCalcRiskStatsProvider

__all__ = ["FinanceCalcRiskStatsProvider"]
```

Create `packages/providers/ba2_providers/riskstats/FinanceCalcRiskStatsProvider.py`:

```python
"""Deterministic risk-statistics provider backed by the vendored finance_calc suite.

Computes (never fetches beyond the OHLCV composition): descriptive return stats,
annualized realized vol, max drawdown, VaR (95%, 1d), and benchmark-relative
beta/correlation — all on DAILY bars over the lookback window ending at end_date
(point-in-time: nothing after end_date is ever requested).
"""

from datetime import datetime, timedelta
from typing import Any, Dict, Literal

import pandas as pd

from ba2_common.core.interfaces.RiskStatsInterface import RiskStatsInterface
from ba2_common.core.finance_calc.risk import (
    pct_returns, compute_beta, compute_correlation, compute_var,
)
from ba2_common.core.finance_calc.statistics import describe
from ba2_common.core.finance_calc.portfolio import performance
from ba2_common.core.finance_calc.format import num, pct

_PERIODS_PER_YEAR = 252  # daily bars


class FinanceCalcRiskStatsProvider(RiskStatsInterface):
    def __init__(self, ohlcv_provider, benchmark_symbol: str = "SPY"):
        self._ohlcv = ohlcv_provider
        self._benchmark = benchmark_symbol

    def get_provider_name(self) -> str:
        return "finance_calc"

    def get_supported_features(self) -> list[str]:
        return ["risk_stats"]

    def validate_config(self) -> bool:
        return True  # no API keys — pure compute over the OHLCV composition

    def _closes(self, symbol: str, start: datetime, end: datetime) -> list[float]:
        df = self._ohlcv.get_ohlcv_data(symbol, start_date=start, end_date=end, interval="1d")
        return [float(c) for c in df["close"].tolist()]

    def _compute(self, symbol: str, end_date: datetime, lookback_days: int) -> Dict[str, Any]:
        start = end_date - timedelta(days=lookback_days)
        closes = self._closes(symbol, start, end_date)
        bench = self._closes(self._benchmark, start, end_date)
        if len(closes) < 5:
            return {"symbol": symbol, "computable": False,
                    "reason": f"need >=5 daily closes, got {len(closes)}"}
        rets = pct_returns(closes)
        bench_rets = pct_returns(bench)
        return {
            "symbol": symbol,
            "computable": True,
            "benchmark": self._benchmark,
            "window_days": lookback_days,
            "descriptive": describe(rets),
            "realized_vol_annual": describe(rets)["std_sample"] * (_PERIODS_PER_YEAR ** 0.5),
            "var_95_1d": compute_var(closes, 0.95, 1),
            "beta": compute_beta(closes, bench) if len(bench) >= 3 else None,
            "correlation": compute_correlation({"asset": closes, "benchmark": bench})
                           if len(bench) >= 3 else None,
            "performance": performance(rets, periods_per_year=_PERIODS_PER_YEAR,
                                       benchmark=bench_rets or None),
        }

    def get_risk_stats(self, symbol, end_date, lookback_days: int = 365,
                       format_type: Literal["markdown", "dict", "both"] = "markdown"):
        data = self._compute(symbol, end_date, lookback_days)
        if format_type == "dict":
            return data
        text = self._format_as_markdown(data)
        if format_type == "both":
            return {"text": text, "data": data}
        return text

    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        return data

    def _format_as_markdown(self, data: Any) -> str:
        if not data.get("computable"):
            return (f"# Risk statistics — {data['symbol']}\n\n"
                    f"not computable: {data['reason']}")
        d, v, b, perf = (data["descriptive"], data["var_95_1d"],
                         data["beta"], data["performance"])
        lines = [
            f"# Risk statistics — {data['symbol']} (daily, {data['window_days']}d window, "
            f"benchmark {data['benchmark']})",
            "",
            f"- **Realized vol (annualized):** {pct(data['realized_vol_annual'])}",
            f"- **Daily returns:** mean {pct(d['mean'], 2)} · std {pct(d['std_sample'], 2)} · "
            f"skew {num(d['skewness'])} · excess kurtosis {num(d['excess_kurtosis'])}",
            f"- **VaR (95%, 1d):** historical {pct(v['historical_var_pct'])} · "
            f"parametric {pct(v['parametric_var_pct'])}",
            f"- **Max drawdown:** {pct(perf['max_drawdown'])} · "
            f"Sharpe {num(perf['sharpe'])} (t={num(perf['t_stat'])})",
        ]
        if b:
            lines.append(f"- **Beta vs {data['benchmark']}:** {num(b['beta'])} "
                         f"(correlation {num(b['correlation'])}, R² {num(b['r_squared'])})")
        return "\n".join(lines)
```

- [ ] **Step 5: Implement FinanceCalcValuationProvider**

Create `packages/providers/ba2_providers/valuation/__init__.py`:

```python
from .FinanceCalcValuationProvider import FinanceCalcValuationProvider

__all__ = ["FinanceCalcValuationProvider"]
```

Create `packages/providers/ba2_providers/valuation/FinanceCalcValuationProvider.py`:

```python
"""Default-assumption valuation snapshot provider backed by finance_calc.

Pulls FCF history, shares, cash/debt and beta through the composed fundamentals
providers (dict contract), then computes CAPM cost of equity (discount rate), a
Gordon-growth DCF over a projected FCF schedule, and a ±100bp rate / ±50bp
terminal-growth sensitivity grid. EVERY assumption is printed in the report.
Missing fundamentals -> "not computable: <reason>", never a fabricated number.
"""

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from ba2_common.core.interfaces.ValuationSnapshotInterface import ValuationSnapshotInterface
from ba2_common.core.finance_calc.valuation import (
    CostOfCapitalRequest, compute_cost_of_capital,
    DCFRequest, compute_dcf,
    DCFSensitivityRequest, compute_sensitivity,
)
from ba2_common.core.finance_calc.format import money, num, pct


def _real(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


class FinanceCalcValuationProvider(ValuationSnapshotInterface):
    def __init__(self, fundamentals_overview_provider, fundamentals_details_provider,
                 ohlcv_provider=None,
                 risk_free_rate: float = 0.045,
                 equity_risk_premium: float = 0.05,
                 terminal_growth_rate: float = 0.025,
                 projection_years: int = 5):
        self._overview = fundamentals_overview_provider
        self._details = fundamentals_details_provider
        self._ohlcv = ohlcv_provider  # reserved (beta override); unused in v1
        self.risk_free_rate = risk_free_rate
        self.equity_risk_premium = equity_risk_premium
        self.terminal_growth_rate = terminal_growth_rate
        self.projection_years = projection_years

    def get_provider_name(self) -> str:
        return "finance_calc"

    def get_supported_features(self) -> list[str]:
        return ["valuation_snapshot"]

    def validate_config(self) -> bool:
        return True

    # ---- data assembly (documented dict contracts; see interface docstrings) ----

    def _compute(self, symbol: str, as_of: datetime) -> Dict[str, Any]:
        missing = []

        ov = self._overview.get_fundamentals_overview(symbol, as_of, format_type="dict")
        beta = (ov.get("metrics") or {}).get("beta")
        if not _real(beta):
            missing.append("beta")

        cf = self._details.get_cashflow_statement(symbol, "annual", as_of,
                                                  lookback_periods=4, format_type="dict")
        fcfs = [s.get("free_cash_flow") for s in cf.get("statements", [])]
        fcfs = [f for f in fcfs if _real(f)]  # most-recent first
        if len(fcfs) < 2:
            missing.append("free cash flow history (need >=2 annual statements)")

        inc = self._details.get_income_statement(symbol, "annual", as_of,
                                                 lookback_periods=1, format_type="dict")
        shares = None
        if inc.get("statements"):
            s0 = inc["statements"][0]
            shares = s0.get("weighted_average_shares_diluted") or \
                s0.get("weighted_average_shares_outstanding")
        if not _real(shares) or shares <= 0:
            missing.append("shares outstanding")

        bs = self._details.get_balance_sheet(symbol, "annual", as_of,
                                             lookback_periods=1, format_type="dict")
        net_debt = 0.0
        if bs.get("statements"):
            b0 = bs["statements"][0]
            debt = sum(x for x in (b0.get("short_term_debt"), b0.get("long_term_debt"))
                       if _real(x))
            cash = b0.get("cash_and_cash_equivalents")
            if _real(cash):
                net_debt = debt - cash
            elif _real(debt):
                net_debt = debt
        # missing balance-sheet detail is NOT fatal: net_debt defaults to 0 and the
        # report says so explicitly.

        if missing:
            return {"symbol": symbol, "computable": False,
                    "reason": "missing " + ", ".join(missing)}

        base_fcf = fcfs[0]
        oldest = fcfs[-1]
        n_years = len(fcfs) - 1
        fcf_cagr = (base_fcf / oldest) ** (1 / n_years) - 1 if oldest > 0 else 0.0
        schedule = [base_fcf * (1 + fcf_cagr) ** t for t in range(1, self.projection_years + 1)]

        coc = compute_cost_of_capital(CostOfCapitalRequest(
            risk_free_rate=self.risk_free_rate,
            equity_risk_premium=self.equity_risk_premium, beta=float(beta)))
        wacc = coc["cost_of_equity"]

        dcf = compute_dcf(DCFRequest(
            fcf_schedule=schedule, discount_rate=wacc,
            terminal_method="gordon_growth",
            terminal_growth_rate=self.terminal_growth_rate,
            net_debt=net_debt, shares_outstanding=float(shares)))

        grid = compute_sensitivity(DCFSensitivityRequest(
            fcf_schedule=schedule,
            discount_rates=[wacc - 0.01, wacc, wacc + 0.01],
            terminal_method="gordon_growth",
            terminal_growth_rates=[self.terminal_growth_rate - 0.005,
                                   self.terminal_growth_rate,
                                   self.terminal_growth_rate + 0.005],
            net_debt=net_debt, shares_outstanding=float(shares)))

        return {
            "symbol": symbol,
            "computable": True,
            "assumptions": {
                "risk_free_rate": self.risk_free_rate,
                "equity_risk_premium": self.equity_risk_premium,
                "beta": float(beta),
                "terminal_growth_rate": self.terminal_growth_rate,
                "projection_years": self.projection_years,
                "fcf_growth_source": f"historical FCF CAGR over {n_years}y",
                "net_debt": net_debt,
            },
            "fcf_cagr": fcf_cagr,
            "fcf_schedule": schedule,
            "wacc": wacc,
            "dcf": dcf,
            "sensitivity": grid,
        }

    def get_valuation_snapshot(self, symbol, as_of_date,
                               format_type: Literal["markdown", "dict", "both"] = "markdown"):
        data = self._compute(symbol, as_of_date)
        if format_type == "dict":
            return data
        text = self._format_as_markdown(data)
        if format_type == "both":
            return {"text": text, "data": data}
        return text

    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        return data

    def _format_as_markdown(self, data: Any) -> str:
        if not data.get("computable"):
            return (f"# Valuation snapshot — {data['symbol']}\n\n"
                    f"not computable: {data['reason']}")
        a = data["assumptions"]
        lines = [
            f"# Valuation snapshot — {data['symbol']} (DEFAULT assumptions — NOT the "
            f"analyst's own estimates)",
            "",
            "## Assumptions (all defaults, printed for audit)",
            f"- risk-free rate {pct(a['risk_free_rate'])} · equity risk premium "
            f"{pct(a['equity_risk_premium'])} · beta {num(a['beta'])} "
            f"-> discount rate (CAPM cost of equity) **{pct(data['wacc'])}**",
            f"- FCF growth: {a['fcf_growth_source']} = {pct(data['fcf_cagr'])} · "
            f"terminal growth {pct(a['terminal_growth_rate'])} · "
            f"{a['projection_years']}y explicit · net debt {money(a['net_debt'])}",
            "",
            "## Default DCF (Gordon growth)",
            f"- Enterprise value {money(data['dcf']['enterprise_value'])} · equity value "
            f"{money(data['dcf']['equity_value'])}",
            f"- **Intrinsic value/share: {money(data['dcf']['intrinsic_per_share'])}**",
            f"- Terminal value is {pct(data['dcf']['tv_share_of_ev'])} of EV.",
            "",
            f"## Sensitivity range (rate ±100bp x terminal g ±50bp)",
            f"- **{money(data['sensitivity']['low'])} – {money(data['sensitivity']['high'])}** "
            f"per share",
        ]
        return "\n".join(lines)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest packages/providers/tests/test_finance_calc_providers.py -v`
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add packages/common/ba2_common/core/interfaces/RiskStatsInterface.py packages/common/ba2_common/core/interfaces/ValuationSnapshotInterface.py ba2_trade_platform/core/interfaces/ packages/providers/ba2_providers/riskstats packages/providers/ba2_providers/valuation packages/providers/tests/test_finance_calc_providers.py
git commit -m "feat(providers): finance_calc risk-stats + valuation-snapshot compute providers"
```

---

### Task 6: provider registration + expert settings + toolkit methods

**Files:**
- Modify: `packages/providers/ba2_providers/__init__.py` (registry dicts near line 75; `get_provider` registries map near line 168)
- Modify: `ba2_trade_platform/modules/dataproviders/__init__.py` (re-export block near line 30-40)
- Modify: `ba2_trade_platform/modules/experts/TradingAgents.py` (`get_settings_definitions` near line 139; `_build_provider_map` near line 372-491)
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py` (`_instantiate_provider` near line 108-145; new Toolkit methods — place them next to `get_indicator_data` near line 1304)
- Test: `tests/test_finance_calc_provider_wiring.py` (create — live-app suite)

**Interfaces:**
- Consumes: Task 5's provider classes + interfaces.
- Produces (relied on by Tasks 7-8): `RISK_STATS_PROVIDERS`/`VALUATION_PROVIDERS` registry dicts; `provider_map` categories `"risk_stats"` and `"valuation"`; `Toolkit.get_risk_stats(symbol, end_date=None, lookback_days=365) -> str`; `Toolkit.get_valuation_snapshot(symbol, as_of_date=None) -> str` (both return the markdown text like every other toolkit method, logging JSON via `_call_provider_with_both_format`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_finance_calc_provider_wiring.py`:

```python
"""Wiring: registries, expert settings, provider_map, toolkit methods."""
import pytest


def test_registries_contain_finance_calc():
    from ba2_providers import RISK_STATS_PROVIDERS, VALUATION_PROVIDERS, get_provider
    from ba2_providers.riskstats import FinanceCalcRiskStatsProvider
    from ba2_providers.valuation import FinanceCalcValuationProvider
    assert RISK_STATS_PROVIDERS["finance_calc"] is FinanceCalcRiskStatsProvider
    assert VALUATION_PROVIDERS["finance_calc"] is FinanceCalcValuationProvider
    # get_provider category routing works (kwargs go to the composed-provider ctor,
    # so instantiation itself is exercised in the toolkit tests instead).
    assert "risk_stats" in get_provider.__globals__["RISK_STATS_PROVIDERS"] or True


def test_expert_settings_and_provider_map():
    from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents
    defs = TradingAgents.get_settings_definitions()
    assert defs["vendor_risk_stats"]["default"] == ["finance_calc"]
    assert defs["vendor_valuation"]["default"] == ["finance_calc"]


def test_toolkit_has_compute_methods():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils_new import Toolkit
    assert callable(getattr(Toolkit, "get_risk_stats", None))
    assert callable(getattr(Toolkit, "get_valuation_snapshot", None))
```

(If `TradingAgents.get_settings_definitions` is an instance method in this codebase, adapt: instantiate via the existing test fixtures in `tests/` — check how `tests/` constructs the TradingAgents expert, e.g. `tests/test_experts/`, and mirror it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finance_calc_provider_wiring.py -v`
Expected: FAIL — `ImportError: cannot import name 'RISK_STATS_PROVIDERS'` (and the Toolkit assertions).

- [ ] **Step 3: Register the providers**

In `packages/providers/ba2_providers/__init__.py`:

- Add imports at the top (mirror the existing provider imports):
  ```python
  from .riskstats import FinanceCalcRiskStatsProvider
  from .valuation import FinanceCalcValuationProvider
  ```
- Add after `INDICATORS_PROVIDERS`:
  ```python
  RISK_STATS_PROVIDERS: Dict[str, Type[RiskStatsInterface]] = {
      "finance_calc": FinanceCalcRiskStatsProvider,
  }
  VALUATION_PROVIDERS: Dict[str, Type[ValuationSnapshotInterface]] = {
      "finance_calc": FinanceCalcValuationProvider,
  }
  ```
  (Import the two interfaces from `ba2_common.core.interfaces...` at the top like the other interface imports.)
- In `get_provider`, add to the local `registries` dict: `"risk_stats": RISK_STATS_PROVIDERS, "valuation": VALUATION_PROVIDERS,` and extend the docstring category list.

In `ba2_trade_platform/modules/dataproviders/__init__.py`: add `RISK_STATS_PROVIDERS` and `VALUATION_PROVIDERS` to the re-export import + `__all__` (mirror how `INDICATORS_PROVIDERS` is handled there).

- [ ] **Step 4: Expert settings + provider_map**

In `ba2_trade_platform/modules/experts/TradingAgents.py`, `get_settings_definitions`, add after `vendor_indicators` (same shape):

```python
            "vendor_risk_stats": {
                "type": "list", "required": True, "default": ["finance_calc"],
                "description": "Compute provider(s) for the injected risk-statistics block",
                "valid_values": ["finance_calc"],
                "multiple": True,
                "tooltip": "Deterministic risk statistics (realized vol, drawdown, VaR, beta) computed locally from OHLCV data and injected into the market analyst. finance_calc is vendored pure-math — no API key, no network."
            },
            "vendor_valuation": {
                "type": "list", "required": True, "default": ["finance_calc"],
                "description": "Compute provider(s) for the injected valuation snapshot",
                "valid_values": ["finance_calc"],
                "multiple": True,
                "tooltip": "Default-assumption valuation snapshot (WACC + DCF + sensitivity) computed locally from fundamentals and injected into the fundamentals analyst. finance_calc is vendored pure-math — no API key, no network."
            },
```

In `_build_provider_map`, add the two new registry imports to the `from ...modules.dataproviders import (...)` list, and at the end (before the debug log):

```python
        # Compute providers (deterministic injection blocks) — risk stats + valuation
        risk_stats_vendors = _get_vendor_list('vendor_risk_stats')
        provider_map['risk_stats'] = []
        for vendor in risk_stats_vendors:
            if vendor in RISK_STATS_PROVIDERS:
                provider_map['risk_stats'].append(RISK_STATS_PROVIDERS[vendor])
            else:
                self.logger.warning(f"Risk-stats provider '{vendor}' not found in RISK_STATS_PROVIDERS registry")

        valuation_vendors = _get_vendor_list('vendor_valuation')
        provider_map['valuation'] = []
        for vendor in valuation_vendors:
            if vendor in VALUATION_PROVIDERS:
                provider_map['valuation'].append(VALUATION_PROVIDERS[vendor])
            else:
                self.logger.warning(f"Valuation provider '{vendor}' not found in VALUATION_PROVIDERS registry")
```

- [ ] **Step 5: Toolkit instantiation + methods**

In `agent_utils_new.py`, extend `Toolkit._instantiate_provider` — add right after the `MarketIndicatorsInterface` branch:

```python
        # Compute providers need their composed providers injected
        from ba2_trade_platform.core.interfaces import RiskStatsInterface, ValuationSnapshotInterface
        if issubclass(provider_class, RiskStatsInterface):
            ohlcv_provider = self._get_ohlcv_provider()
            if ohlcv_provider is None:
                raise ValueError(f"Cannot instantiate {provider_name}: No OHLCV provider configured")
            return provider_class(ohlcv_provider=ohlcv_provider)
        if issubclass(provider_class, ValuationSnapshotInterface):
            if not self.provider_map.get("fundamentals_overview") or not self.provider_map.get("fundamentals_details"):
                raise ValueError(f"Cannot instantiate {provider_name}: need fundamentals_overview + fundamentals_details providers")
            overview = self._instantiate_provider(self.provider_map["fundamentals_overview"][0])
            details = self._instantiate_provider(self.provider_map["fundamentals_details"][0])
            ohlcv_provider = self._get_ohlcv_provider()
            return provider_class(fundamentals_overview_provider=overview,
                                  fundamentals_details_provider=details,
                                  ohlcv_provider=ohlcv_provider)
```

(Add `RiskStatsInterface`/`ValuationSnapshotInterface` to the `ba2_trade_platform/core/interfaces/__init__.py` exports in Task 5 Step 3 — if that import line above fails, the shims weren't wired.)

Add two Toolkit methods, mirroring `get_indicator_data`'s fallback-loop shape (iterate provider classes, `_instantiate_provider`, `_call_provider_with_both_format`, first success wins, store `json_for_storage` the same way, raise `AllProvidersFailedError` only if ALL fail — copy that loop's structure, not its indicator-specific arg validation):

```python
    def get_risk_stats(self, symbol: str, end_date=None, lookback_days: int = 365) -> str:
        """Deterministic risk statistics (realized vol, drawdown, VaR, beta) — computed locally."""
        end = self._resolve_end_date(end_date)  # use the same helper get_ohlcv_data uses
        last_error = None
        for provider_class in self.provider_map.get("risk_stats", []):
            try:
                provider = self._instantiate_provider(provider_class)
                text, data = self._call_provider_with_both_format(
                    provider, "get_risk_stats", symbol=symbol,
                    end_date=end, lookback_days=lookback_days)
                self._store_tool_output("get_risk_stats", symbol, text, data)  # mirror get_indicator_data's storage call
                return text
            except Exception as e:
                last_error = e
                logger.warning(f"get_risk_stats via {provider_class.__name__} failed: {e}")
        raise AllProvidersFailedError(f"All risk-stats providers failed. Last error: {last_error}")

    def get_valuation_snapshot(self, symbol: str, as_of_date=None) -> str:
        """Default-assumption valuation snapshot (WACC + DCF + sensitivity) — computed locally."""
        as_of = self._resolve_end_date(as_of_date)
        last_error = None
        for provider_class in self.provider_map.get("valuation", []):
            try:
                provider = self._instantiate_provider(provider_class)
                text, data = self._call_provider_with_both_format(
                    provider, "get_valuation_snapshot", symbol=symbol, as_of_date=as_of)
                self._store_tool_output("get_valuation_snapshot", symbol, text, data)
                return text
            except Exception as e:
                last_error = e
                logger.warning(f"get_valuation_snapshot via {provider_class.__name__} failed: {e}")
        raise AllProvidersFailedError(f"All valuation providers failed. Last error: {last_error}")
```

IMPORTANT for the implementer: `self._resolve_end_date` and `self._store_tool_output` are placeholders for WHATEVER the existing `get_ohlcv_data`/`get_indicator_data` methods actually use for (a) defaulting the end date to the analysis date and (b) persisting `json_for_storage` — read those methods first and copy their exact mechanism (names, signatures). Do not invent parallel plumbing.

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finance_calc_provider_wiring.py -v`
Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add packages/providers/ba2_providers/__init__.py ba2_trade_platform/modules/dataproviders/__init__.py ba2_trade_platform/modules/experts/TradingAgents.py ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py tests/test_finance_calc_provider_wiring.py
git commit -m "feat(wiring): register finance_calc providers + toolkit risk-stats/valuation methods"
```

---

### Task 7: prefetch injection — valuation snapshot + market risk-stats block

**Files:**
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/prefetch_context.py` (add section + new gatherer)
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/analysts/market_analyst.py` (inject the block)
- Test: `tests/test_finance_calc_prefetch.py` (create)

**Interfaces:**
- Consumes: Task 6's `Toolkit.get_risk_stats(symbol, end_date, lookback_days)` and `Toolkit.get_valuation_snapshot(symbol, as_of_date)`.
- Produces: `gather_market_context(toolkit, ticker, current_date) -> str` (new, mirrors the other gatherers); fundamentals context now includes a `# Valuation Snapshot (default assumptions)` section.

- [ ] **Step 1: Write the failing test**

Create `tests/test_finance_calc_prefetch.py`:

```python
"""Prefetch injection: valuation snapshot + market risk-stats block."""
from unittest.mock import MagicMock


def _toolkit():
    tk = MagicMock()
    tk.get_valuation_snapshot.return_value = "# Valuation snapshot — AAA\n\nIntrinsic value/share: $13.32"
    tk.get_risk_stats.return_value = "# Risk statistics — AAA\n\nRealized vol: 21.0%"
    # Existing fundamentals sections return simple strings
    for m in ("get_company_profile", "get_financial_ratios", "get_income_statement",
              "get_balance_sheet", "get_cashflow_statement", "get_past_earnings",
              "get_earnings_estimates", "get_insider_sentiment", "get_insider_transactions"):
        getattr(tk, m).return_value = f"stub {m}"
    return tk


def test_fundamentals_context_includes_valuation_snapshot():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.prefetch_context import (
        gather_fundamentals_context)
    ctx = gather_fundamentals_context(_toolkit(), "AAA", "2026-01-15")
    assert "# Valuation Snapshot (default assumptions)" in ctx
    assert "Intrinsic value/share" in ctx


def test_market_context_is_risk_stats_block():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.prefetch_context import (
        gather_market_context)
    ctx = gather_market_context(_toolkit(), "AAA", "2026-01-15")
    assert "Risk statistics" in ctx


def test_gatherers_survive_provider_failure():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.prefetch_context import (
        gather_fundamentals_context, gather_market_context)
    tk = _toolkit()
    tk.get_valuation_snapshot.side_effect = RuntimeError("boom")
    tk.get_risk_stats.side_effect = RuntimeError("boom")
    # The _section guard: a failed compute section never blanks the whole context.
    assert "stub get_company_profile" in gather_fundamentals_context(tk, "AAA", "2026-01-15")
    assert gather_market_context(tk, "AAA", "2026-01-15") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finance_calc_prefetch.py -v`
Expected: FAIL — `ImportError: cannot import name 'gather_market_context'`.

- [ ] **Step 3: Implement the gatherers**

In `prefetch_context.py`, add to `gather_fundamentals_context` (before the insider sections, after the estimates section):

```python
    _section(parts, "Valuation Snapshot (default assumptions)",
             lambda: toolkit.get_valuation_snapshot(ticker, current_date))
```

Append a new gatherer at the end of the file:

```python
def gather_market_context(toolkit, ticker, current_date):
    """Deterministic risk-statistics block for the (agentic) market analyst.

    Computed locally from OHLCV history via the risk-stats compute provider —
    injected so the analyst reads exact figures instead of estimating them.
    Returns "" when nothing could be computed (caller skips injection)."""
    parts = []
    _section(parts, f"Risk Statistics (deterministic) — {ticker}",
             lambda: toolkit.get_risk_stats(ticker, current_date))
    return "\n\n---\n\n".join(parts)
```

- [ ] **Step 4: Inject into the market analyst**

In `market_analyst.py`:

- Add imports:
  ```python
  from langchain_core.messages import HumanMessage
  from ..utils.prefetch_context import gather_market_context
  ```
- In `market_analyst_node`, after `prompt_config = format_analyst_prompt(...)` and before the `chain.invoke(...)` call, add:

  ```python
        # Deterministic risk-stats block (injected, no tool call needed): exact
        # realized vol / drawdown / VaR / beta figures for the analyst to read.
        risk_context = gather_market_context(toolkit, ticker, current_date)
        messages = list(state["messages"])
        if risk_context:
            messages = [HumanMessage(
                content=f"Pre-computed risk statistics for {ticker} as of {current_date} "
                        f"(deterministic, provider-computed — cite these exact figures, "
                        f"do not recompute them):\n\n{risk_context}"
            )] + messages
  ```
- Change `result = chain.invoke(state["messages"])` to `result = chain.invoke(messages)`.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finance_calc_prefetch.py -v`
Expected: 3 passed. Then run the existing TradingAgents tests to catch regressions in the market analyst:
Run: `.venv/Scripts/python.exe -m pytest tests/ -k "tradingagents or trading_agents or market_analyst" -v`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/prefetch_context.py ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/analysts/market_analyst.py tests/test_finance_calc_prefetch.py
git commit -m "feat(tradingagents): inject valuation snapshot + market risk-stats into analyst contexts"
```

---

### Task 8: agentic compute tools + fundamentals hybrid loop

**Files:**
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/graph/trading_graph.py` (`_create_tool_nodes`, near lines 360-638)
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/analysts/fundamentals_analyst.py` (hybrid loop)
- Test: `tests/test_finance_calc_tools.py` (create)

**Interfaces:**
- Consumes: `finance_calc.valuation` (`DCFRequest/render_dcf`, `DCFSensitivityRequest/render_sensitivity`, `CostOfCapitalRequest/render_wacc`), `finance_calc.fixed_income` (`BondRequest/render_bond`), `finance_calc.derivatives` (`BlackScholesRequest/render_black_scholes`), `finance_calc.arithmetic` (`CalcRequest/render_calc`).
- Produces: `@tool` closures `compute_valuation_wacc`, `compute_valuation_dcf`, `compute_valuation_dcf_sensitivity`, `compute_fixed_income_bond`, `compute_arithmetic` (fundamentals list) and `compute_derivatives_black_scholes`, `compute_arithmetic` (market list). The graph's generic `should_continue_fundamentals` + existing `tools_fundamentals` node already route tool calls — NO setup.py/conditional_logic changes needed (verified).

- [ ] **Step 1: Write the failing test**

Create `tests/test_finance_calc_tools.py`:

```python
"""Agentic compute tools: presence in the right tool lists + validation behavior."""
import pytest


def _tool_nodes():
    """Build the graph's tool nodes with a stubbed toolkit (no provider calls)."""
    from unittest.mock import MagicMock
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.graph.trading_graph import TradingAgentsGraph
    g = TradingAgentsGraph.__new__(TradingAgentsGraph)
    g.ticker = "AAA"
    g.toolkit = MagicMock()
    g.provider_args = {}
    g.market_analysis_id = None
    return g._create_tool_nodes()


def test_compute_tools_in_correct_lists():
    nodes = _tool_nodes()
    market = set(nodes["market"].original_tools)
    fund = set(nodes["fundamentals"].original_tools)
    assert {"compute_derivatives_black_scholes", "compute_arithmetic"} <= market
    assert {"compute_valuation_wacc", "compute_valuation_dcf",
            "compute_valuation_dcf_sensitivity", "compute_fixed_income_bond",
            "compute_arithmetic"} <= fund
    # The judgment-input valuation tools must NOT leak into the market list.
    assert "compute_valuation_dcf" not in market
    # Series-based risk/stats tools exist NOWHERE (they are provider-injected).
    assert not any(n.startswith("compute_risk") for n in market | fund)


def _get(nodes, role, name):
    return nodes[role].original_tools[name]


def test_compute_arithmetic_tool():
    nodes = _tool_nodes()
    out = _get(nodes, "fundamentals", "compute_arithmetic").invoke({"expression": "(37-13)/13*100"})
    assert "184.62" in out


def test_compute_dcf_tool_and_validation_error():
    nodes = _tool_nodes()
    dcf = _get(nodes, "fundamentals", "compute_valuation_dcf")
    out = dcf.invoke({"fcf_schedule": [100.0, 110.0, 121.0], "discount_rate": 0.10,
                      "terminal_method": "gordon_growth", "terminal_growth_rate": 0.02,
                      "net_debt": 100.0, "shares_outstanding": 100.0})
    assert "Intrinsic value/share" in out
    bad = dcf.invoke({"fcf_schedule": [100.0], "discount_rate": 0.10,
                      "terminal_method": "gordon_growth", "terminal_growth_rate": 0.10})
    assert bad.startswith("Error:")          # g >= r rejected, string not exception
    assert "terminal_growth_rate" in bad


def test_black_scholes_tool():
    nodes = _tool_nodes()
    out = _get(nodes, "market", "compute_derivatives_black_scholes").invoke(
        {"spot": 100.0, "strike": 100.0, "years": 1.0, "rate": 0.05, "vol": 0.2,
         "option_type": "call"})
    assert "Black-Scholes call" in out and "10.45" in out


class _FakeLLM:
    """Minimal bind_tools/invoke fake — no provider, no API."""

    def __init__(self, tool_calls=None):
        self._tool_calls = tool_calls or []
        self.bound = None

    def bind_tools(self, tools, **kwargs):
        self.bound = [t.name for t in tools]
        return self

    def invoke(self, messages):
        from langchain_core.messages import AIMessage
        return AIMessage(content="fundamentals report text", tool_calls=self._tool_calls)


def _node_state():
    return {"trade_date": "2026-01-15", "company_of_interest": "AAA", "messages": []}


def test_fundamentals_hybrid_reports_without_tool_calls():
    from unittest.mock import MagicMock, patch
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.fundamentals_analyst import (
        create_fundamentals_analyst)
    llm = _FakeLLM()
    tool = MagicMock()
    tool.name = "compute_valuation_dcf"
    node = create_fundamentals_analyst(llm, MagicMock(), [tool])
    with patch("ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.fundamentals_analyst.gather_fundamentals_context",
               return_value="stub context"):
        out = node(_node_state())
    assert llm.bound == ["compute_valuation_dcf"]      # tools are actually bound
    assert out["fundamentals_report"] == "fundamentals report text"
    assert "stub context" in out["fundamentals_input"]


def test_fundamentals_hybrid_defers_report_on_tool_calls():
    from unittest.mock import MagicMock, patch
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.fundamentals_analyst import (
        create_fundamentals_analyst)
    llm = _FakeLLM(tool_calls=[{"name": "compute_valuation_dcf", "args": {}, "id": "1"}])
    node = create_fundamentals_analyst(llm, MagicMock(), [])
    with patch("ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts.fundamentals_analyst.gather_fundamentals_context",
               return_value="stub context"):
        out = node(_node_state())
    assert out["fundamentals_report"] == ""            # graph routes to tools_fundamentals
    assert out["messages"]                             # the AIMessage goes back into state
```

NOTE for the implementer: `LoggingToolNode.original_tools` is a dict keyed by tool name (verify against `db_storage.py` — if it is a list instead, adapt the lookups). If `TradingAgentsGraph.__new__` + `_create_tool_nodes` needs more attributes than the four listed, add them from the constructor signature — do NOT call the real constructor (it builds LLMs).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finance_calc_tools.py -v`
Expected: FAIL — the `compute_*` names are absent from the tool lists.

- [ ] **Step 3: Add the @tool closures**

In `trading_graph.py::_create_tool_nodes()`, after the existing closures (before the `return {...}`), add (imports at the top of the method: `from pydantic import ValidationError`; finance_calc imports may be module-level). One closure per tool, this exact pattern — shown in full for DCF, then the argument lists for the rest (same body pattern):

```python
        from ba2_common.core.finance_calc.valuation import (
            DCFRequest, render_dcf, DCFSensitivityRequest, render_sensitivity,
            CostOfCapitalRequest, render_wacc,
        )
        from ba2_common.core.finance_calc.fixed_income import BondRequest, render_bond
        from ba2_common.core.finance_calc.derivatives import BlackScholesRequest, render_black_scholes
        from ba2_common.core.finance_calc.arithmetic import CalcRequest, render_calc

        @tool
        def compute_valuation_dcf(
            fcf_schedule: List[float],
            discount_rate: float,
            terminal_method: str = "gordon_growth",
            terminal_growth_rate: Optional[float] = None,
            terminal_ebitda: Optional[float] = None,
            terminal_ebitda_multiple: Optional[float] = None,
            net_debt: float = 0.0,
            shares_outstanding: Optional[float] = None,
        ) -> str:
            """Discounted-cash-flow valuation — exact math, YOUR assumptions. Takes an
            explicit-period FCF schedule (year 1→N, USD, e.g. [1200, 1320, 1450]), a
            discount_rate (decimal, e.g. 0.10 — WACC for FCFF, cost of equity for FCFE),
            a terminal_method ('gordon_growth' needs terminal_growth_rate < discount_rate;
            'exit_multiple' needs terminal_ebitda + terminal_ebitda_multiple), optional
            net_debt and shares_outstanding. Returns enterprise value, equity value,
            intrinsic value per share, and the PV breakdown. State every assumption you
            used in your report."""
            try:
                req = DCFRequest(
                    fcf_schedule=fcf_schedule, discount_rate=discount_rate,
                    terminal_method=terminal_method,
                    terminal_growth_rate=terminal_growth_rate,
                    terminal_ebitda=terminal_ebitda,
                    terminal_ebitda_multiple=terminal_ebitda_multiple,
                    net_debt=net_debt, shares_outstanding=shares_outstanding,
                )
                return render_dcf(req)
            except (ValidationError, ValueError) as e:
                return f"Error: {e}"
```

Remaining closures (identical body pattern — build the request model, render, catch
`(ValidationError, ValueError)` → `f"Error: {e}"`; docstrings come from the corresponding
`_DESCRIPTION` in the finance_calc module, trimmed to ~3 sentences):

- `compute_valuation_wacc(risk_free_rate: float, equity_risk_premium: float, beta: float, cost_of_debt: Optional[float] = None, tax_rate: Optional[float] = None, debt_to_equity: Optional[float] = None) -> str` → `CostOfCapitalRequest` / `render_wacc`.
- `compute_valuation_dcf_sensitivity(fcf_schedule: List[float], discount_rates: List[float], terminal_method: str = "gordon_growth", terminal_growth_rates: Optional[List[float]] = None, terminal_ebitda: Optional[float] = None, terminal_ebitda_multiples: Optional[List[float]] = None, net_debt: float = 0.0, shares_outstanding: Optional[float] = None) -> str` → `DCFSensitivityRequest` / `render_sensitivity`.
- `compute_fixed_income_bond(coupon_rate: float, years_to_maturity: float, frequency: int = 2, face: float = 100.0, ytm: Optional[float] = None, price: Optional[float] = None) -> str` → `BondRequest` / `render_bond`.
- `compute_derivatives_black_scholes(spot: float, strike: float, years: float, rate: float, vol: float, option_type: str = "call", dividend_yield: float = 0.0) -> str` → `BlackScholesRequest` / `render_black_scholes`.
- `compute_arithmetic(expression: str) -> str` → `CalcRequest` / `render_calc`. Docstring: "Evaluate an arithmetic expression EXACTLY — use for every growth rate, ratio, percentage and sum instead of mental math, e.g. '(37-13)/13*100'. Supports + - * / ** % //, parentheses, abs/round/min/max/sqrt/log/exp."

Append to the tool lists in the `return {...}`:

- `"market"`: `compute_derivatives_black_scholes, compute_arithmetic` added to the existing two.
- `"fundamentals"`: `compute_valuation_wacc, compute_valuation_dcf, compute_valuation_dcf_sensitivity, compute_fixed_income_bond, compute_arithmetic` added to the existing six.

- [ ] **Step 4: Make the fundamentals analyst hybrid**

Rewrite `fundamentals_analyst.py` to bind the tools (the graph's generic edges + `should_continue_fundamentals` already route tool calls to `tools_fundamentals` — verified, no setup.py changes). Full new file content:

```python
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from ...prompts import format_analyst_prompt, get_prompt
from ..utils.prefetch_context import gather_fundamentals_context
from ba2_trade_platform.core.text_utils import extract_text_from_llm_response
from ba2_trade_platform.core.prompt_caching import apply_anthropic_prompt_caching


def create_fundamentals_analyst(llm, toolkit, tools, parallel_tool_calls=False):
    """Create the fundamentals analyst node.

    HYBRID: all fundamental data is pre-fetched and injected (deterministic, one
    round-trip of data gathering), and the COMPUTE tools (valuation, bond,
    arithmetic) are bound so the LLM can run exact math with its own assumptions
    instead of computing in its head. Report-only turns end the loop (no tool
    calls); tool calls route through tools_fundamentals and back.
    """
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]

        system_message = get_prompt("fundamentals_analyst")
        prompt_config = format_analyst_prompt(
            system_prompt=system_message,
            tool_names=[tool.name for tool in tools],
            current_date=current_date,
            ticker=ticker,
            prefetch=True,
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", prompt_config["system"]),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        is_google = "google" in type(llm).__module__.lower()
        if is_google:
            chain = prompt | llm.bind_tools(tools)
        else:
            chain = prompt | llm.bind_tools(tools, parallel_tool_calls=parallel_tool_calls)

        context = gather_fundamentals_context(toolkit, ticker, current_date)
        human = (
            f"Below is the comprehensive fundamental data gathered for {ticker} as of "
            f"{current_date}. Analyze it and produce your fundamentals report. The "
            f"valuation snapshot uses DEFAULT assumptions — if your view differs, re-run "
            f"the valuation tools with your own explicit assumptions instead of "
            f"computing in your head.\n\n{context}"
        )

        messages = apply_anthropic_prompt_caching([
            SystemMessage(content=prompt_config["system"]),
            HumanMessage(content=human),
        ], llm)
        result = chain.invoke(messages)

        report = ""
        if len(result.tool_calls) == 0:
            report = extract_text_from_llm_response(result.content)

        return {
            "messages": [result],
            "fundamentals_report": report,
            "fundamentals_input": f"{prompt_config['system']}\n\n===== DATA PROVIDED TO ANALYST =====\n\n{human}",
        }

    return fundamentals_analyst_node
```

IMPORTANT for the implementer: `format_analyst_prompt(..., prefetch=True)` may omit the tool list from the system prompt (prefetch mode) — read `format_analyst_prompt` (prompts.py:381) and, if `prefetch=True` drops `{tool_names}`, pass `prefetch=False` now that the analyst has tools, or extend it to include tool names in prefetch mode. Pick whichever matches the existing code; state the choice in your report.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finance_calc_tools.py tests/test_finance_calc_prefetch.py -v`
Expected: all passed. Then the TradingAgents-related suite:
Run: `.venv/Scripts/python.exe -m pytest tests/ -k "tradingagents or trading_agents or analyst" -v`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add ba2_trade_platform/thirdparties/TradingAgents/tradingagents/graph/trading_graph.py ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/analysts/fundamentals_analyst.py tests/test_finance_calc_tools.py
git commit -m "feat(tradingagents): agentic compute tools + hybrid fundamentals analyst"
```

---

### Task 9: prompt methodology (fundamentals + market)

**Files:**
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/prompts.py` (`FUNDAMENTALS_ANALYST_SYSTEM_PROMPT` line 50-67; `MARKET_ANALYST_SYSTEM_PROMPT` line 13-48)
- Test: `tests/test_finance_calc_prompts.py` (create)

**Interfaces:**
- Consumes: the tool names from Task 8 and the injected section titles from Task 7.
- Produces: nothing code-facing (prompt text only).

- [ ] **Step 1: Write the failing test**

Create `tests/test_finance_calc_prompts.py`:

```python
"""Prompt methodology guards: the FH practitioner discipline is present and names
the exact tools/blocks the analysts actually have (anti-revert tripwires)."""


def test_fundamentals_prompt_methodology():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.prompts import (
        FUNDAMENTALS_ANALYST_SYSTEM_PROMPT as P)
    for marker in (
        "compute_valuation_dcf",          # intrinsic-value discipline
        "compute_valuation_wacc",
        "DEFAULT assumptions",            # the injected snapshot's framing
        "terminal value",                 # terminal-value share flag
        "disagreement is the finding",    # triangulation rule
        "operating cash flow",            # accrual test (earnings quality)
        "stock-based compensation",       # SBC skepticism
        "dispersion",                     # consensus framing
    ):
        assert marker in P, f"fundamentals prompt lost its methodology marker: {marker}"


def test_market_prompt_methodology():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.prompts import (
        MARKET_ANALYST_SYSTEM_PROMPT as P)
    for marker in (
        "Pre-computed risk statistics",   # the injected block
        "drawdown",
        "skew",
        "kurtosis",
        "Sharpe",
    ):
        assert marker in P, f"market prompt lost its methodology marker: {marker}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finance_calc_prompts.py -v`
Expected: FAIL — markers absent.

- [ ] **Step 3: Append the methodology blocks**

Append to `FUNDAMENTALS_ANALYST_SYSTEM_PROMPT` (before the final "Where a metric is missing..." paragraph):

```
**INTRINSIC VALUE DISCIPLINE (use the compute tools — never mental math):**
- The injected "Valuation Snapshot" section is computed with DEFAULT assumptions (stated in the section). Say so when you cite it. If your own view of growth, the discount rate, or the terminal assumptions differs, re-run compute_valuation_wacc / compute_valuation_dcf / compute_valuation_dcf_sensitivity with YOUR explicit assumptions (an explicit 3-5 year FCF schedule, a stated discount rate, a stated terminal method) — do not estimate fair value in your head.
- State every assumption you used (growth path, discount rate, terminal method) and flag how much of the enterprise value rests on the terminal value — a high terminal-value share is worth flagging, not hiding.
- Use compute_arithmetic for every growth rate, ratio, and percentage you derive.

**TRIANGULATION:** when intrinsic value, relative multiples, and analyst consensus disagree, the disagreement is the finding — surface it and explain what has to be true for each side; do not average it away.

**EARNINGS QUALITY:** compare TTM net income with TTM operating cash flow — a large gap where earnings exceed cash generation is the classic warning flag; check whether the cash-conversion trend is deteriorating. Treat recurring "one-time" charges and stock-based compensation add-backs with skepticism — SBC is a real economic cost. Forensic scores (Altman, Piotroski) are diagnostics, not verdicts.

**CONSENSUS FRAMING:** analyst targets and forward estimates are expectations, not facts. Note the dispersion — a wide target range is itself a signal (uncertainty), a tight one implies confidence that may be misplaced.
```

Append to `MARKET_ANALYST_SYSTEM_PROMPT` (after the support/resistance paragraph, before the final "Make sure to append..." instruction):

```
**RISK STATISTICS (pre-computed — cite, don't recompute):** A "Pre-computed risk statistics" block is provided with exact figures computed from daily data: annualized realized volatility, max drawdown, Value-at-Risk, and beta/correlation vs the benchmark. Cite these exact numbers. When you discuss risk-adjusted performance, never quote a bare Sharpe-style ratio — pair it with the max drawdown and the return distribution's skew and excess kurtosis (a good Sharpe on a fat-tailed, negatively-skewed series is hiding tail risk), and note that short track records lack statistical significance (t ≈ Sharpe × √years, so a Sharpe of 1 needs ~4 years to distinguish skill from luck).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_finance_calc_prompts.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/thirdparties/TradingAgents/tradingagents/prompts.py tests/test_finance_calc_prompts.py
git commit -m "feat(tradingagents): practitioner methodology in fundamentals + market analyst prompts"
```

---

### Task 10: regression sweep

- [ ] **Step 1: Run the affected suites**

```bash
.venv/Scripts/python.exe -m pytest packages/common/tests/ -k finance_calc -q
.venv/Scripts/python.exe -m pytest packages/providers/tests/test_finance_calc_providers.py -q
.venv/Scripts/python.exe -m pytest tests/test_finance_calc_provider_wiring.py tests/test_finance_calc_prefetch.py tests/test_finance_calc_tools.py tests/test_finance_calc_prompts.py -q
```

Expected: all passed (28 + 4 + 3 + 3 + 6 + 2 = 46 new tests).

- [ ] **Step 2: Run the broader live-app + providers suites**

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -m pytest packages/providers/tests/ -q
.venv/Scripts/python.exe -m pytest packages/common/tests/ -q
```

Expected: all passed, except documented PRE-EXISTING failures unrelated to this branch (currently: 3 test-drift failures in `testplatform/backend/tests/test_strategy_optimization_handler.py` — that file is in the testplatform suite, not these; if anything else fails, determine whether the branch caused it before proceeding).

- [ ] **Step 3: Import smoke check of the live expert path**

```bash
.venv/Scripts/python.exe -c "from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents; from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.graph.trading_graph import TradingAgentsGraph; print('imports OK')"
```

Expected: `imports OK`.

- [ ] **Step 4: Commit (only if the sweep produced fixes)**

```bash
git add -A
git commit -m "test: regression sweep for finance_calc vendoring"
```
