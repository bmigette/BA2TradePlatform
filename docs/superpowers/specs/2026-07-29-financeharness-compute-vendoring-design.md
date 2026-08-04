# Vendored FinanceHarness compute suite + practitioner prompt methodology for TradingAgents

Date: 2026-07-29
Status: approved design (pre-plan)

Source: [FinanceHarness](https://github.com/Yijia-Xiao/FinanceHarness) (Apache-2.0),
[paper](https://arxiv.org/abs/2607.27853). Reference checkout for implementation:
`.superpowers/fh-reference/` (shallow clone, scratch — not committed).

## Goal

Two-part adoption of FinanceHarness material into BA2TradePlatform:

1. Give TradingAgents LLM analysts real, verified finance computation (DCF, WACC, beta,
   correlation, VaR, portfolio performance, regression, bond math, Black-Scholes) instead of
   LLM mental math.
2. Upgrade the fundamentals and market analyst system prompts with the practitioner
   methodology distilled from the FinanceHarness SKILL.md workflows.

Approved decisions (three rounds):

- Vendor the FULL compute suite (including bond math and the arithmetic calculator).
- **Hybrid exposure**: deterministic-input computations are pre-computed and INJECTED into
  the analysts' context (no tool calls); judgment-input computations (DCF with LLM-chosen
  assumptions, Black-Scholes scenarios, bond math, arithmetic) stay agentic tools.
- **Use the provider architecture** for the deterministic computations: proper compute
  providers behind interfaces (the `PandasIndicatorCalc` pattern), not ad-hoc prefetch code.

## Architecture overview

Three layers:

1. `packages/common/ba2_common/core/finance_calc/` — pure vendored math (no I/O).
2. `packages/providers/ba2_providers/` — two compute PROVIDERS (deterministic,
   symbol-in → report-out) behind new interfaces in `ba2_common`, consumed through the
   standard `provider_map`/toolkit path and injected into analyst contexts.
3. TradingAgents in-tree wiring — thin agentic `@tool` closures over layer 1 for the
   judgment-input tools, prefetch injection of layer 2 output, prompt methodology updates.

## Part 1a — vendored pure math: `packages/common/ba2_common/core/finance_calc/`

One module per family, vendored from `financeharness/tools/compute/` and adapted:

- `valuation.py` — DCF (Gordon growth + exit multiple), DCF sensitivity, WACC (CAPM + blend)
- `risk.py` — beta, correlation, VaR (historical + parametric), returns helpers
- `statistics.py` — descriptive stats (incl. skew/kurtosis), OLS regression
- `portfolio.py` — performance (annualized return/vol, Sharpe, Sortino, Calmar, max
  drawdown, t-stats; benchmark-relative metrics when a benchmark is passed)
- `fixed_income.py` — bond price/YTM/duration/convexity
- `derivatives.py` — Black-Scholes price + Greeks (European)
- `series.py`, `arithmetic.py`, `format.py` — shared helpers (`percentile`, safe expression
  calculator, `money`/`num`/`pct` markdown renderers)

Adaptation rule (uniform): keep the pydantic v2 `XRequest` model + pure
`compute_x(req) -> dict` + markdown `render_x(req) -> str`; DROP the FinanceHarness
`ToolSpec`/`ToolResponse`/`tool_registry` layer. Replace `financeharness.tools.format`
imports with the local `format.py`. Everything else is stdlib; pydantic v2 is already a
project dependency (SQLModel). Each vendored file carries an Apache-2.0 attribution header
(origin URL + "modified for BA2TradePlatform"). Vendored copy, not a dependency (FH requires
Python ≥3.12; this repo supports 3.11).

## Part 1b — compute providers (deterministic, injected)

Two new providers following the `MarketIndicatorsInterface`/`PandasIndicatorCalc` pattern
(interface in `packages/common/ba2_common/core/interfaces/`, implementation in
`packages/providers/ba2_providers/`, constructor takes its input providers, output per
`format_type` = markdown / dict / both):

- **`RiskStatsInterface.get_risk_stats(symbol, format_type=...)`** →
  `ba2_providers/riskstats/FinanceCalcRiskStatsProvider`. Constructor takes the OHLCV
  provider (composition, like `PandasIndicatorCalc`) plus a benchmark symbol (default SPY).
  Computes from the configured window/interval via `finance_calc`: descriptive stats,
  annualized realized vol, max drawdown, historical + parametric VaR, beta/correlation/OLS
  regression vs the benchmark, and the portfolio-performance block (Sharpe/Sortino/Calmar
  with t-stats).
- **`ValuationSnapshotInterface.get_valuation_snapshot(symbol, format_type=...)`** →
  `ba2_providers/valuation/FinanceCalcValuationProvider`. Constructor takes a fundamentals
  provider and the OHLCV provider. Pulls latest/historical FCF, shares outstanding,
  cash/debt, and beta through those providers' public `format_type="dict"` interfaces, then
  computes a DEFAULT-ASSUMPTION WACC + DCF + bear/base/bull sensitivity grid via
  `finance_calc`. Every assumption (risk-free proxy, ERP, terminal growth, FCF growth
  source) is a constructor parameter with a documented default AND is printed verbatim in
  the rendered report — assumptions are declared, never hidden (FH's own "state every
  assumption" principle; this repo's no-fabrication rule). Missing fundamentals (no FCF
  history, negative-only FCF) produce an explicit "not computable: <reason>" section, never
  an exception or a fabricated number.

Registration & consumption (the standard path):

- New registry dicts in `packages/providers/ba2_providers/__init__.py`
  (`RISK_STATS_PROVIDERS`, `VALUATION_PROVIDERS`, each `{"finance_calc": <class>}`),
  re-exported by the live shim.
- New expert settings on the TradingAgents expert (`vendor_risk_stats`,
  `vendor_valuation`; list-type like `vendor_indicators`, default `["finance_calc"]`), new
  `provider_map` categories in `_build_provider_map`.
- `Toolkit._instantiate_provider` special-cases the two new interfaces (inject the first
  OHLCV / fundamentals provider from `provider_map`), exactly like the indicators case.
- New Toolkit methods `get_risk_stats(symbol)` / `get_valuation_snapshot(symbol)` using
  `_call_provider_with_both_format` (so the JSON half lands in `AnalysisOutput` storage).
- Prefetch injection: one `_section(...)` line each — valuation snapshot into
  `gather_fundamentals_context`; risk-stats block into the MARKET analyst via a small new
  prefetch step (it has none today; the market analyst keeps its agentic loop, the block is
  just injected context).

## Part 1c — agentic judgment-input tools

Thin `@tool` closures in `trading_graph.py::_create_tool_nodes()` over `finance_calc`
directly (no provider — inputs are LLM-chosen assumptions, not a symbol). Named
`compute_<family>_<x>` per the FH taxonomy:

- **market** list: `compute_derivatives_black_scholes`, `compute_arithmetic`
- **fundamentals** list: `compute_valuation_wacc`, `compute_valuation_dcf`,
  `compute_valuation_dcf_sensitivity`, `compute_fixed_income_bond`, `compute_arithmetic`

Validation failures return a descriptive `"Error: ..."` string (existing convention); the
LLM always receives the rendered markdown string. `fundamentals_analyst.py` becomes hybrid:
prefetch context (now including the valuation snapshot) kept + tool-calling loop mirroring
`market_analyst.py` for the valuation tools. `graph/setup.py` already passes the role's
tools into the analyst factory; only the analyst node changes. No new on/off expert setting
for the tools (inert unless called; existing `enable_*_analyst` flags gate the analysts).

## Part 2 — prompt methodology (from FH SKILL.md workflows)

Targeted, distilled edits to
`ba2_trade_platform/thirdparties/TradingAgents/tradingagents/prompts.py` — short bullet
blocks merged into the existing prompts, NOT wholesale pastes (token cost matters):

- `FUNDAMENTALS_ANALYST_SYSTEM_PROMPT` gains:
  - **Intrinsic-value discipline** (from `dcf-valuation`): the injected valuation snapshot
    uses documented DEFAULT assumptions — say so when citing it; when the analyst's own view
    differs, re-run `compute_valuation_dcf` / `compute_valuation_dcf_sensitivity` with its
    own explicit FCF schedule and assumptions rather than doing mental math; always state
    assumptions and flag the terminal-value share of EV.
  - **Triangulation rule** (from `equity-deep-dive`): when intrinsic value, relative
    multiples, and consensus disagree, the disagreement is the finding — surface it.
  - **Earnings quality** (from `earnings-quality`): accrual test (TTM net income vs
    operating cash flow gap), cash-conversion trend, skepticism of recurring "one-time"
    charges and SBC add-backs; forensic scores are diagnostics, not verdicts.
  - **Consensus framing** (from `consensus-check`): estimates are expectations, not facts;
    wide target dispersion is itself a signal.
- `MARKET_ANALYST_SYSTEM_PROMPT` gains (from `portfolio-review` + `options-strategy`):
  read the injected risk-stats block — annualized realized vol, drawdown, VaR, beta vs the
  benchmark; never report a bare Sharpe-style ratio — pair it with max drawdown and
  skew/kurtosis; note track-record significance (t ≈ Sharpe × √years).
- Skipped skills: `bond-analysis`/`credit-analysis` (no fixed-income mandate),
  `industry-analysis` (macro analyst is a different prefetch-only role), `options-strategy`
  (TradingAgents has no options analyst; the platform's options flow is rules-based — noted
  as a future candidate for the options experts).

## Testing

- `packages/common/tests/test_finance_calc_<family>.py`: known-value tests per family
  (hand-computed/published references — DCF both terminal methods, WACC, beta/correlation on
  fixed series, VaR, skew/kurtosis, regression, bond price↔YTM round-trip, Black-Scholes vs
  published values, portfolio performance, arithmetic safety). FH ships no public tests.
- Provider tests (`packages/providers/tests/`): `FinanceCalcRiskStatsProvider` and
  `FinanceCalcValuationProvider` against canned OHLCV/fundamentals stub providers —
  point-in-time safety (no data after end_date), all three `format_type` outputs, the
  "not computable" path, assumptions printed in the report.
- Wiring tests: new providers in registries + `provider_map`; Toolkit methods return the
  `"both"` shape; the `@tool` closures appear in the correct `_create_tool_nodes()` lists;
  validation errors surface as `Error:` strings.
- Fundamentals hybrid loop: fake-LLM test mirroring the market-analyst tool tests.
- Prompt-content tests: methodology markers + tool names present in the rewritten prompt
  constants.
- No backtest/parity impact: the LLM seam is deliberately unwired in tests. Providers must
  obey point-in-time discipline (never read past the analysis date) so they stay
  backtest-safe if reused later.

## Out of scope

- FinanceGym-style evaluation harness for LLM output quality.
- Progressive-disclosure / reference-chaining runtime patterns (separate future candidates).
- `options-strategy` skill adoption for the options experts.
- Bond/credit prompt methodology; consumption of the new providers by other experts
  (enabled by the architecture, deliberately not wired yet).
