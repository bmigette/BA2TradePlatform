# Statistical discovery before option GA

Date: 2026-09-15. Code inspected at `cb584a60`.
Status: feasibility review and proposed workflow. No profitability study, GA run,
cache download, engine change or production action was performed.

User decision after review: retain the GA discovery jobs. The statistical workflow
below is a reference proposal, not an active implementation plan.
The requested follow-up is the [new market-condition genes design](../../docs/plans/2026-09-15-option-market-condition-genes-design.md).

**Recommendation:** add a statistical discovery pass before expensive genetic
search. Compute market features and a modest catalogue of fixed option-trade
outcomes once, then reuse those observations to identify useful conditions.
Use GA to refine and combine the resulting rules. Correlation alone cannot
establish profitability, especially for nonlinear option payoffs.

## What is already available

The current discovery driver runs 16 permitted structures under FMPRating and
DeterministicScorer: 32 jobs, population 200, up to 60 generations. The September
12 genome review measured 47–63 genes per job. That is up to roughly 384,000
population slots before early stopping, duplicate avoidance and other execution
details; it is not a measured count of unique backtests.

There is enough infrastructure to build the proposed pass:

| Resource | Verified capability | Qualification |
|---|---|---|
| OHLCV | Local FMP parquet files; existing vectorized daily metric calculations | Confirm adjustment basis, session timestamps and coverage before joining to options. |
| Screener metrics | Market cap, relative volume, price drops, Weinstein stage, momentum and ATR | The September 14 inventory measured weekly scan dates. Do not interpret weekly observations as independently observed daily features. Verify historical membership and market-cap provenance. |
| ThetaData option parquet | Contract identity, expiry, strike, option type, OHLC, bid, ask, IV, volume and open interest | A directory or non-null field is not proof of valid, complete or executable data. |
| Current pure-option entry conditions | IV rank, IV / trailing realized volatility, relative volume, expert direction, confidence and expected-profit gates | No direct price-channel or broad-market regime gate in this entry template. Covered-call/protective-put overlays use separate equity-entry rules. Trailing realized volatility is a feature; future realized volatility is an outcome. |
| Option backtest reader | Contract lookup, quotes and derived IV/Greeks | The reader derives IV/Greeks from its price/spot inputs. Raw vendor IV is a different series and must not silently replace it. |
| Existing study pattern | `tools/news_event_study.py` already separates signal timestamps from forward outcomes and reports correlations/buckets | Reuse the approach, not its symbol-level t-statistic as a complete dependence correction. |

A read-only check found **856 symbol directories** in the local ThetaData tree.
INTC, KO and AAPL each had 350 expiry directories spanning 2020-01-03 to
2026-09-11. One partition per symbol was sampled, expiry 2024-06-07:

| Symbol | Rows | Non-null close | Non-null bid / ask | Non-null IV | Rows with bid > 0 and ask >= bid |
|---|---:|---:|---:|---:|---:|
| INTC | 1,930 | 1,062 | 1,930 / 1,930 | 1,929 | 1,538 |
| KO | 1,790 | 510 | 1,790 / 1,790 | 1,759 | 1,272 |
| AAPL | 2,416 | 1,209 | 2,416 / 2,416 | 2,411 | 1,809 |

These samples cover 2024-04-25 through 2024-06-07. They demonstrate that real
quote columns are populated, including many rows without a last-trade close.
They do **not** certify the complete universe, quote freshness, all years or
historical execution at the quoted size. No extra API requests are needed to
start a pilot; missing coverage should first be reported explicitly.

### Follow-up: what the current GA actually discovers

Built `O_LC`, `O_IC`, `O_CC`, `O_PP` and `O_WHEEL` through the current launcher,
called `collect_param_space`, and verified every generated entry leaf maps to a
backtest trigger. This was a read-only configuration check, not a backtest or
inspection of a remote running job's frozen payload.

- Pure-option entries have actual `cond:*:value` and `cond:*:enabled` genes for
  IV rank, IV / trailing realized volatility and relative volume, plus expert
  confidence/expected profit and a direction-filter toggle. For example,
  `cond:o_ic-iv_rank:value` searches 20–70, with a separately searchable on/off
  gene. The wheel inherits the cash-secured put's entry conditions.
- The GA searches thresholds and toggles within a fixed AND tree. Operators are
  authored: long-premium members require IV rank below a threshold in 10–60;
  credit members require it above a threshold in 20–70. The IV/RV threshold is
  0.8–1.6, with the same fixed direction split. Relative volume is greater than
  a threshold in 0.5–3.0. Disabling a gate removes that restriction.
- There is no direct SPY-trend, VIX, ADX/ranging, skew or term-structure gene in
  this stage-1 entry template. DeterministicScorer does incorporate trend, ADX
  and macro inputs including index trend/VIX into its signal. Its actual launcher
  genes include `macro_mode` (`multiply`, `gate`, `off`) and section weights;
  regime handling is therefore partly searched through the expert. The grid
  keeps its indicator periods and detailed regime thresholds fixed. This does
  not create independently searchable per-structure conditions for those raw
  fields. An iron condor's direction gate is currently bullish-or-off, not an
  explicit sideways-market selector.
- `O_CC` and `O_PP` use S2 equity-entry conditions and position-state overlays;
  they do not search the pure-option IV-rank / IV-RV / relative-volume gates.
- Stage 1 scores the resulting conditional strategy over its full test window;
  this is not a separate performance measurement for every named market regime.
  Broad claims that the grid discovers arbitrary market regimes would overstate
  this search space. The current jobs remain useful within the conditions they
  can express; additional regime genes would be a separate, versioned search.

## Proposed discovery pass

### 1. Build one reusable observation table

One row per symbol and decision timestamp. Store feature availability timestamps,
source/vendor, cache version and quality flags. Use data known before entry:

- Direction: trailing returns, trend/channel position and recent pullback.
- Movement: trailing realized volatility, ATR, gaps and relative volume.
- Options: consistent-tenor ATM IV, historical IV rank, IV / trailing realized
  volatility, spread and liquidity. Skew and term structure can be added only
  where comparable contracts and live rule support are available.
- Context: historical cap band, market regime, and cached FMPRating / DS signals
  under explicitly fixed expert configurations. Full expert parameter search
  remains a later step; one frozen signal is not evidence about every variant.

Use the same opportunity table for both experts plus a signal-free control;
do not recompute market history separately for every expert/structure pair.
Compute forward 5-, 10- and 20-session return, excursion and realized-volatility
labels separately. Add horizons only for a stated hypothesis.

### 2. Identify conditional patterns

Start with rank correlations, coarse buckets and a small set of two-feature
interactions. For example, compare future movement across trend direction and
IV / trailing realized-volatility buckets. Fit bucket boundaries on training
periods only. Look for broad stable regions rather than an isolated best threshold.

Report effect size, observation count, distinct dates/issuers, coverage, tails
and results by year. This can expose a useful relationship even when its global
linear correlation is near zero. It can also reveal a direction signal too small
to cover option costs.

### 3. Measure actual option outcomes for fixed recipes

For the same decision opportunities, select contracts using information available
at entry and evaluate a small, declared set of DTE/delta/width/holding-period
recipes. Preserve the selected contracts through exit. Start with representative
long-premium and defined-risk credit/debit structures, then extend coverage to
all 16 permitted structures. The initial subset is an engineering pilot, not a
performance exclusion of the other structures.

Cache dollar P&L, capital required, holding duration and marked P&L paths per
recipe/opportunity. Condition tests then become filters and aggregations over
those cached outcomes instead of complete account simulations.

Use observed two-sided quotes for the executable-price study, charging spread,
fees and an explicit slippage assumption. Buying at ask and selling at bid is a
useful conservative scenario, not a fill guarantee. Record rejected and missing
entry/exit quotes; do not quietly count missing exits as zero or remove losses.
Keep model-priced / close-only observations separate and labelled.

End-of-day option snapshots do not establish a 09:30 fill or the order of
intraday TP/SL touches. Features from today's close must not select a trade
filled earlier today. A daily snapshot pilot must declare its signal-to-entry
lag. Exact live cadence and path-dependent exits require the engine replay.
Corporate actions must match underlying prices, strikes and contract deliverables;
exclude unsupported adjustments explicitly.

Compare net dollar expectancy, loss tails, capital-days, turnover and top-five
issuer concentration, alongside return on the engine's applicable capital
requirement. Do not compare premium received as profit or use premium as the
capital denominator for short options. Independent overlapping observations
cannot be summed into an achievable account equity curve.

Covered calls, protective puts and the wheel need their stock legs and lifecycle
accounted for. A short-put event study is not a complete wheel backtest.

### 4. Export understandable rules and narrow the expensive search

Convert robust conditional patterns into existing ruleset fields and bounded
parameter ranges. Example hypotheses, **not findings**:

- A positive direction signal with relatively cheap IV may favour a long call
  or bull call spread.
- A positive signal with relatively expensive IV may favour a bull put spread.
- A quiet underlying with expensive IV may favour an iron condor, subject to
  spread costs and jump losses.

Do not hard-code these assumptions as discoveries. Compare the alternatives,
including stock-only and no-trade controls. Unsupported new features need a
separate shared live/backtest condition implementation before deployment.

A reasonable initial compute budget is **4–8 targeted GA jobs**, selected after
the study, instead of automatically funding 32 equally large searches. That is
a proposed budget, not a measured speed-up or assurance that 4–8 will suffice.
Keep an exploration allowance for weakly sampled structures and alternative
expert settings. Benchmark the pilot before estimating full-universe runtime;
building option paths can still be substantial work.

The existing design explicitly preserves structures that help only in a
portfolio. Keep that rule: standalone statistics prioritize compute and supply
seeds, but do not permanently remove diversification candidates. The existing
exclusion of naked short straddles/strangles remains in force.

## Validation and compatibility

- Freeze the recipe list, feature definitions, source manifest and evaluation
  protocol before inspecting the final evaluation period. Track every tried
  condition/recipe, including failures. Statistical screening also overfits when
  many alternatives are tried; changing the search method does not eliminate
  the multiple-testing problem. See [Bailey et al., Statistical Overfitting and
  Backtest Performance](https://www.davidhbailey.com/dhbpapers/overfitting.pdf).
- Use chronological walk-forward comparisons. Purge overlapping outcome windows
  at split boundaries. Estimate uncertainty by resampling calendar blocks jointly
  across symbols, preserving market-wide shocks and overlapping-trade dependence.
  Millions of option rows are not millions of independent market observations.
- Inspect historical-universe provenance and survivor/delisting exclusions. The
  current selected option universe is not automatically a historical investable
  universe. Report this limitation even if all selected symbols have long histories.
- The 2020–2025 history already informed prior searches. A new split of that
  history is a robustness diagnostic, not newly untouched evidence. Reserve a
  genuinely unused period where available and confirm through forward testing.
- Validate any vectorized feature/contract selection against the existing shared
  implementation on representative fixtures. Replay shortlisted rules in the
  current engine for profit, CAR, drawdown, capital competition, fills, exits,
  assignment and sizing. Only that replay produces comparable account metrics.
- Make capital explicit. The current option driver defaults to **$20,000**;
  the earlier ten-strategy equity experiment used **$10,000**. Test the intended
  deployable allocation separately, because indivisible option contracts and
  stock assignments prevent proportional scaling between those budgets.
- Add this as a research tool and separate result family. Preserve original
  backtests, engine semantics and current GA results. No existing result needs
  invalidating to introduce this workflow.

## Suggested implementation deliverables

1. A cache-only inventory and feature/outcome builder with explicit universe,
   date window, source root, entry timing, capital and recipe configuration.
2. A statistical report: conditional option outcomes, coverage, stability,
   concentration, uncertainty and coarse rule candidates.
3. A ruleset/GA-seed export using the current launcher schema, with a dry-run
   manifest of the smaller proposed search and subsequent full-engine checks.

Pilot on a fixed, transparent subset of the current option universe across
multiple years before expanding. Do not select pilot names using their later
profits. The immediate output should answer **which conditions and structures
deserve expensive optimization**, not claim a deployable strategy from a heatmap.

## Repository references

- [Grid design](../../docs/superpowers/specs/2026-08-27-option-ga-grid-design.md)
- [Discovery driver](../../tools/run_options_matrix.py)
- [September 12 review](option_stage1_driver_review_2026-09-12.md)
- [Shared daily metrics](../../packages/providers/ba2_providers/screener/metric_store.py)
- [Option parquet schema](../../packages/providers/ba2_providers/options/parquet_store.py)
- [Backtest option reader](../../testplatform/backend/app/services/backtest/parquet_options_provider.py)
- [Existing event study](../../tools/news_event_study.py)
