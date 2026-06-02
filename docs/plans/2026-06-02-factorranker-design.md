# FactorRanker — configurable multi-factor equity expert — design

Date: 2026-06-02
Status: validated (brainstorm), ready for implementation planning

## Context

A strategy-gap review (vs the systematic-trading canon and 2025-26 SOTA) found the platform
has AI-discretionary and alternative-data alpha (TradingAgents, congressional copy, analyst
ratings) but **none of the bread-and-butter equity factors** — cross-sectional momentum,
value, quality — and **no portfolio-level factor combiner**. These are the most robust,
well-researched, low-correlation additions and reuse data we already pull (FMP/Finnhub).

This design adds **FactorRanker**: one configurable, cross-sectional, multi-factor expert
(momentum, post-earnings-drift, value, quality as selectable/weightable factors) that ranks
a universe and holds the top slice. It also fills the "multi-factor combiner" gap itself.

The RL alpha-weighting and evolutionary strategy-discovery ideas (arXiv 2509.01393, 2510.18569)
are parked in BA2MLTestPlatform/TODO.md — they need a backtester first and are research-grade.

## Architecture

FactorRanker is a plain **MarketExpert** — not a LiveExpert (no intraday machinery needed):

- **Selection method `EXPERT`** with **`should_expand_instrument_jobs = False`** → JobManager
  submits a single `run_analysis("EXPERT", ma)` batch job per rebalance (JobManager.py:1026-1042),
  instead of N per-symbol jobs. This is how it does cross-sectional ranking in one run.
- **Cadence = the schedule cron** (see Scheduler extension below). One batch run = one rebalance.
- **One batch run does everything** (entries *and* exits) — so it renders **only** the
  enter-market schedule (`schedules_open_positions = False`); no separate OPEN_POSITIONS job.

### `run_analysis("EXPERT", ma)` pipeline

1. **Universe** — `self.get_enabled_instruments()` (existing static/label, `StockScreener`, or
   `AIInstrumentSelector` selection — no new universe machinery).
2. **Factor inputs** — one **bulk** fetch per enabled factor (FMP/Finnhub batch endpoints),
   not per-symbol.
3. **Factor calculators** — each `compute(symbols) -> {symbol: raw_value}`:
   - `momentum`: 12-1 total return (skip most recent month).
   - `pead`: standardized earnings surprise (SUE), gated to the post-earnings drift window.
   - `value`: composite of earnings yield (E/P) + FCF/EV yield.
   - `quality`: ROE + gross-profitability + low accruals (or Piotroski F-score).
4. **Combine** — winsorize → cross-sectional z-score per factor → optional sector-neutralize →
   weighted sum (per-factor weights are settings) → composite score → rank.
5. **Portfolio construction** (pluggable; v1 `long_only_top_n`) → target weights:
   `top_n`/top-quantile, weighting `equal | score-proportional`, `max_weight_per_name` cap,
   `target_gross_exposure`. Typed seam for a future `long_short` mode (YAGNI for now).
6. **Rebalance** — `FactorPortfolioManager` diffs target weights vs current holdings and
   submits buy/sell deltas directly (names dropped from the top slice are sold). Optional
   per-name hard stop between rebalances (setting).
7. **Audit/UI** — write a `MarketAnalysis` + rich `AnalysisOutput`s (the ranked book); a
   dedicated `render_market_analysis` shows the ranked universe table (symbol · per-factor
   sub-scores · composite z · rank · target weight · action), a current-vs-target panel, and
   the resulting trades. Same richness as FMPRating's renderer.

### No ExpertRecommendation, no SmartRiskManager

The generic "emit recs → SmartRiskManager picks best bets" flow would fight the deliberate
top-N construction, so it is **not used**. Order/expert attribution flows through
`transaction.expert_id` (recs are optional and `expert_recommendation_id` is nullable), so
FactorRanker creates **no** `ExpertRecommendation` records — `MarketAnalysis` is the audit
trail and `FactorPortfolioManager` is the execution/risk path. (Mirrors PennyMomentum's
self-contained execution, minus the recommendation records.)

## Scheduler extension (platform feature — benefits all experts)

Today `_parse_schedule` (JobManager.py:783) only builds weekly crons (`hour/minute/day_of_week`).
Add **monthly Nth-weekday** scheduling:

- Schedule config + UI gains a **weekly/monthly switch**, for *both* `enter_market` and
  `open_positions`.
- **Monthly** = weekday (Mon–Sun) + ordinal dropdown (1st/2nd/3rd) → `CronTrigger(day="1st mon", ...)`
  (APScheduler supports `"Nth weekday"` natively; trading-day robust, no fixed-calendar-day
  weekend problem).
- New expert property **`schedules_open_positions`** (default True) → when False (FactorRanker),
  the UI renders only the enter-market schedule. Lets other experts opt into monthly too.

## Settings (FactorRanker)

- `factor_weights`: dict, e.g. `{"momentum": 1.0, "value": 1.0, "quality": 1.0, "pead": 0.0}`
  (0 disables a factor).
- `top_n` (or `top_quantile`), `weighting` (`equal|score`), `max_weight_per_name`,
  `target_gross_exposure`.
- `sector_neutralize` (bool), `winsorize_pct`.
- `min_price`, `min_dollar_volume` (liquidity guards applied to the resolved universe).
- `hard_stop_pct` (optional per-name stop between rebalances).
- Cadence comes from the schedule cron (no separate frequency setting).

## Data sources

FMP bulk endpoints: historical prices (momentum), earnings surprises + calendar (pead),
ratios/key-metrics + cash-flow (value/quality). Reuse existing dataprovider plumbing where
possible; add bulk wrappers as needed. No live price fallbacks (per platform rules).

## Files

- `modules/experts/FactorRanker/__init__.py` — the expert (pipeline, settings, batch flag,
  `get_settings_definitions`, `render_market_analysis`).
- `modules/experts/FactorRanker/factors.py` — pure factor calculators + combine/rank helpers.
- `modules/experts/FactorRanker/portfolio.py` — `FactorPortfolioManager` (target weights →
  order deltas → submit).
- `core/JobManager.py` + schedule UI — monthly Nth-weekday support + `schedules_open_positions`.
- Expert registration in `modules/experts/__init__.py`.

## Testing (TDD)

- **Pure, high-value, no-broker**: each factor calculator (known inputs → expected raw values);
  combine (winsorize/z-score/weighted composite/rank) on a small fixture universe; construction
  (`long_only_top_n` weighting + caps + exposure); rebalance diff (target vs holdings → expected
  buy/sell deltas).
- **Scheduler**: monthly Nth-weekday config → correct `CronTrigger`; `schedules_open_positions=False`
  suppresses the open-positions job.
- **Integration** (in-memory DB + mock account): one batch `run_analysis("EXPERT")` over a small
  universe produces the expected target book and order deltas; second run with shifted scores
  rebalances correctly (sells dropped names).

## Verification

Run the app with a FactorRanker instance on a tiny enabled universe + paper account; trigger a
manual run; confirm the ranked-book UI renders and the rebalance trades match the target weights.
Full unit suite green. Bump `version.py` before push.

## Out of scope (deferred)

`long_short` construction, pairs/stat-arb, options income, the RL alpha-weighting and
evolutionary discovery (→ BA2MLTestPlatform). A backtester would let us validate factor choices
pre-capital — worth doing before scaling allocation, but not required to ship v1.
