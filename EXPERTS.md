# BA2 Trade Platform - Expert Documentation

This document provides comprehensive information about all available trading experts in the BA2 Trade Platform. Each expert implements different trading strategies and analysis methodologies to provide trading recommendations based on various data sources and algorithms.

## Overview

The BA2 Trade Platform uses a plugin-based expert system where each expert can:
- Analyze financial instruments using different methodologies
- Either **generate trading recommendations** (BUY/SELL/HOLD, scored by the SmartRiskManager) **or self-execute** their own orders (e.g. PennyMomentumTrader, FactorRanker)
- Provide confidence scores and expected profit estimates
- Configure instrument selection methods (static, dynamic, expert-driven, or screener)
- Run on customizable weekly or monthly schedules

## Available Experts

### 1. TradingAgents
**Multi-agent AI trading system with debate-based analysis and risk assessment**

- **Type**: AI-powered multi-agent system
- **Methodology**: Uses multiple AI agents that debate and analyze market conditions
- **Data Sources**: Market data, financial statements, news sentiment
- **Instrument Selection**: Static/Dynamic (cannot recommend its own instruments)
- **Key Features**:
  - Debate-based analysis with configurable rounds
  - Risk assessment and position sizing
  - Support for both new positions and existing position management
  - Customizable timeframes and analysis depth

**Key Settings** (25 total):
- `debates_new_positions`: Number of debate rounds for new position analysis
- `debates_existing_positions`: Number of debate rounds for existing position analysis  
- `timeframe`: Analysis timeframe for market data
- `use_advanced_analysis`: Enable advanced technical and fundamental analysis
- `risk_tolerance`: Risk tolerance level (conservative, moderate, aggressive)

### 2. FinnHubRating
**Finnhub analyst recommendation trends with weighted confidence scoring**

- **Type**: Analyst consensus aggregator
- **Methodology**: Aggregates analyst recommendations from Finnhub API
- **Data Sources**: Finnhub analyst ratings and price targets
- **Instrument Selection**: Static/Dynamic (cannot recommend its own instruments)
- **Key Features**:
  - Weighted scoring based on recommendation strength
  - Trend analysis of rating changes
  - Confidence scoring based on analyst consensus

**Key Settings** (1 total):
- `strong_factor`: Weight multiplier for strong buy/sell ratings (default: 2.0)

### 3. FMPRating
**FMP analyst price consensus with profit potential calculation**

- **Type**: Price target analyzer
- **Methodology**: Analyzes analyst price targets and consensus ratings
- **Data Sources**: Financial Modeling Prep (FMP) analyst data
- **Instrument Selection**: Static/Dynamic (cannot recommend its own instruments)
- **Key Features**:
  - Price target consensus analysis
  - Profit potential calculation based on current vs target prices
  - Minimum analyst threshold for reliability

**Key Settings** (2 total):
- `profit_ratio`: Profit ratio multiplier for expected profit calculation
- `min_analysts`: Minimum number of analysts required for valid recommendation

### 4. FMPSenateTraderWeight
**Government official trading activity analysis using weighted algorithm based on portfolio allocation**

- **Type**: Government trading tracker (sophisticated algorithm)
- **Methodology**: Weighted algorithm considering portfolio allocation percentages
- **Data Sources**: FMP Senate/House trading disclosure data
- **Instrument Selection**: Static/Dynamic, **or** expert-driven **basket dispatch**
  (`can_recommend_instruments=True`, `should_expand_instrument_jobs=False`,
  `instrument_selection_method=expert`) — same dispatch style as FMPSenateTraderCopy below.
  `instrument_selection_method` is a per-`ExpertInstance` setting, not a class default, so
  any existing instance configured `static`/`dynamic` is unaffected until an operator
  explicitly switches it to `expert`.
- **Key Features**:
  - Portfolio allocation analysis (symbol focus percentage)
  - Historical trader performance evaluation
  - Investment size and timing considerations
  - Age-based filtering for trade relevance
  - Complex confidence calculation based on trader behavior patterns
  - **Basket dispatch** (opt-in via `instrument_selection_method=expert`): one analysis
    cycle per bar/job scans ALL congressional disclosures (not a fixed universe) via
    `_gather_all`/`_process_all` and emits one recommendation per qualifying symbol that
    passes the disclosure-window filters plus a tradable-stock-only filter. Verified ~4.6x
    faster (543s→117s) than the legacy per-symbol path on a 42-month/498-symbol backtest,
    identical trade output. See `_gather_all`'s docstring in
    `packages/experts/ba2_experts/FMPSenateTraderWeight.py` for the full design and its
    documented "unbounded symbol discovery" caveat: a discovered symbol whose OHLCV was
    never prewarmed is silently skipped for that bar (not a crash), not a fixed list like
    `tools/senate_universe.txt` (see
    [`docs/plans/2026-07-18-senate-basket-dispatch.md`](docs/plans/2026-07-18-senate-basket-dispatch.md)).

**Key Settings** (4 total):
- `max_disclose_date_days`: Maximum days since trade disclosure (default: 30)
- `max_trade_exec_days`: Maximum days since trade execution (default: 60)
- `max_trade_price_delta_pct`: Maximum price change since trade (default: 10%)
- `growth_confidence_multiplier`: Multiplier for confidence calculation (default: 5.0)

### 5. FMPSenateTraderCopy
**Copy trades from specific senators/representatives with 100% confidence**

- **Type**: Government trading tracker (simple copy trading)
- **Methodology**: Direct copy trading from specified government officials
- **Data Sources**: FMP Senate/House trading disclosure data
- **Instrument Selection**: Expert-driven (can recommend its own instruments)
- **Key Features**:
  - **Can recommend instruments**: Yes (can select its own trading targets)
  - **Should expand instrument jobs**: False (prevents job duplication)
  - Simple copy trading with fixed confidence and profit targets
  - Follows specific senators/representatives by name
  - Issues only one recommendation per instrument (most recent trade wins)
  - Age-based filtering for trade relevance

**Key Settings** (4 total):
- `copy_trade_names`: Senators/representatives to copy trade (comma-separated, **required**)
- `max_disclose_date_days`: Maximum days since trade disclosure (default: 30)
- `max_trade_exec_days`: Maximum days since trade execution (default: 60)
- `should_expand_instrument_jobs`: Expand instrument jobs (default: False)

### 6. PennyMomentumTrader
**Live intraday penny-stock momentum trader with catalyst triggers and staged exits**

- **Type**: Live, self-executing momentum trader (`LiveExpertInterface`)
- **Methodology**: Screens for penny-stock momentum candidates, deep-triages them, opens positions on catalysts, and manages staged (tiered) exits intraday
- **Data Sources**: Market data + `StockScreener`, social/news catalysts
- **Instrument Selection**: Expert-driven (`can_recommend_instruments=True`, `should_expand_instrument_jobs=False`)
- **Key Features**:
  - **Self-executing**: places and manages its own orders — does **not** use the SmartRiskManager (`uses_risk_manager=False`) and creates **no** `ExpertRecommendation` records (order/expert attribution flows through `Transaction.expert_id`)
  - Screener-based candidate universe with confidence-weighted position sizing
  - Tiered take-profit / stop exits, wash-trade-safe exit staging
- **Settings**: numerous (screener filters, triage thresholds, tier/exit configuration) — configure in the Expert Settings UI.

### 7. FactorRanker
**Configurable cross-sectional multi-factor equity ranker (momentum / value / quality / PEAD)**

- **Type**: Systematic factor / portfolio expert (self-executing)
- **Methodology**: Ranks a candidate universe each rebalance by a weighted blend of factors and holds the long-only top slice
- **Data Sources**: FMP daily prices, income/balance/cash-flow statements, company profile, earnings; `StockScreener` for screener universes
- **Instrument Selection**: Expert-driven — one **batch run per rebalance** (`should_expand_instrument_jobs=False`)
- **Key Features**:
  - Factors: **momentum** (12-1), **value** (E/P + FCF/EV), **quality** (ROE + gross profitability − accruals), **PEAD** (post-earnings-announcement drift / SUE)
  - Universe from static `enabled_instruments` *or* the `StockScreener` (`universe_source`)
  - Long-only top-N construction (equal or score weighting, per-name cap, gross exposure)
  - **Self-rebalancing** via `FactorPortfolioManager` (diffs targets vs holdings → buy/sell deltas) — **no `ExpertRecommendation`, no SmartRiskManager** (`uses_risk_manager=False`)
  - Renders **only** the Enter-Market schedule (`schedules_open_positions=False`); supports weekly *or* monthly (Nth-weekday) schedules

**Key Settings**: `universe_source`, `factor_weight_momentum` / `factor_weight_value` / `factor_weight_quality` / `factor_weight_pead`, `top_n`, `weighting`, `max_weight_per_name`, `gross_exposure`, `winsorize_pct`, `pead_drift_window_days`, `min_price` (+ `screener_*` when `universe_source=screener`).

📖 **Full guide:** [docs/FACTORRANKER_EXPERT.md](docs/FACTORRANKER_EXPERT.md)

### 8. PremiumSeller — REMOVED 2026-08-31
**Deleted** (operator decision; option-model plan Task 12, `docs/superpowers/plans/2026-08-24-option-model-and-lifecycle.md`). The systematic short-premium expert's capabilities were promoted into shared code — book rails and circuit breaker in `ba2_common.core.option_book`, exit lifecycle (profit capture, tested-delta, roll-DTE, stops) in `ba2_common.core.option_lifecycle` — so they are no longer one expert's private machinery. The launcher refuses `ba2-test optimize --expert PremiumSeller` loudly; historical backtest/optimization rows naming it remain readable.

**What is actually wired, as of 2026-09-01.** Set `risk_manager_mode: classic_options` on any expert and its option **entries** are gated by the sleeve rails and the breaker latch, in live and in the backtest alike — one implementation at the `TradeActions` submit choke point (design §4).

- **Entry gating: both runtimes.** `max_deployment_pct`, `undefined_risk_max_pct`, `max_notional_leverage`, `max_concurrent_structures`, one-per-underlying, assignment capacity, and a refusal while the breaker latch is `halted`. The four rails have **no defaults** — an expert that declares none refuses its option entries and names the missing setting.
- **Breaker TRANSITIONS: both runtimes, one function** (since 2026-09-01). `OptionRiskManagement.update_sleeve_breaker` ratchets the sleeve's peak equity, trips the stand-down and re-arms it on recovery. Live calls it from `option_lifecycle_service` (the exit pass, on the `JobManager` schedule); the backtest calls it once per bar from `daily_engine`, behind the same `classic_options` check the entry gate dispatches on — so an equity trial reaches it zero times. Before that the transition was live-only and `RAIL_BREAKER_HALTED` was unreachable in a backtest, which made a `classic_options` backtest systematically more permissive than live.
- **One definition of the sleeve's SIZING equity: `account.get_account_snapshot().equity`** — cash plus positions marked to market, in both runtimes. It is the denominator of `max_deployment_pct` / `max_notional_leverage`. It replaced `account.get_balance()`, which meant account EQUITY on Alpaca and spendable CASH on the backtest account, so those rails had been measuring different quantities in the two runtimes. **Option grid results produced before 2026-09-01 measured the backtest rails against CASH and are not comparable** to results after it.
- **The BREAKER measures the account's TRUE equity, which is not the same number under a backtest equity cap.** The capped figure the sizer reads is `min(cap, cash + marks)`, and that clamp is one-sided — it compresses peaks and never troughs, so a 50k-capped account falling 100k -> 64k reports a 0.0% drawdown and never stands down, while the identical path live halts at -20%. The breaker therefore reads `account.true_equity()`: the same snapshot field for every real broker (live behaviour unchanged, there being no cap), the uncapped `cash + marks` on the backtest account. It is still one shared breaker function over one latch store — the difference is the account's own answer, not forked logic. Sizing rails are deliberately NOT moved onto it: a sizer must respect the cap.
- **The exit/servicing pass is still LIVE ONLY**, by design: profit capture, tested-delta, roll-DTE and the stops run in `option_lifecycle_service`, while a backtest expresses the same exits as the strategy's own `close_option` rules, which the GA searches. Do not read a backtest as evidence about profit-capture/roll-DTE/tested-delta behaviour.
- **The lifecycle thresholds are declared on `MarketExpertInterface`, with NO defaults.** `circuit_breaker_pct`, `profit_capture_pct`, `roll_dte`, `tested_delta_enabled`, `dr_stop_enabled`, `ur_stop_enabled` and the four conditional ones (`strangle_capture_pct`, `tested_delta`, `dr_stop_credit_mult`, `ur_stop_credit_mult`) left the tree with PremiumSeller's settings block and were re-declared on 2026-09-01, so any expert can be switched over and configured. A risk threshold nobody stated is not a threshold: `circuit_breaker_pct` is a REQUIRED **rail** (an undeclared one refuses every option entry by name, like the other four), and a sleeve missing any of the five required lifecycle thresholds has its live exit pass abort loudly rather than run on a substituted number.

📖 **Historical spec:** [docs/superpowers/specs/2026-07-24-premium-seller-expert-design.md](docs/superpowers/specs/2026-07-24-premium-seller-expert-design.md)

### 9. FMPEarningsEvent
**Ranks upcoming earnings events for the options grid's `O_ERN` (earnings long-vol) key**

- **Type**: Event-driven ranker (not a screener/rating expert — it emits one
  recommendation per symbol carrying an event inside its look window, not a
  standing directional view)
- **Methodology**: A per-symbol composite score from three features computed
  off the FMP disk cache (`past_earnings_quarterly`, and — for
  `w_vol_cheapness` — an ATM straddle read from an options-capable account),
  mapped to confidence 1–100. Below `min_hist_events` a symbol gets NO
  recommendation (never a padded rank).
- **Data Sources**: FMP `past_earnings_quarterly` (dates + eps/epsEstimated,
  used for both the event date and the historical-move/surprise features);
  `earnings_estimates_quarterly` (analyst count, for the `min_analysts` gate
  only — dispersion/revision features are deliberately withheld, see below);
  an options-capable account (backtest parquet reader / live broker chain)
  for the implied-move leg of `w_vol_cheapness`, duck-typed and fail-to-absent.
- **Instrument Selection**: Static/Dynamic (screener-style, like FMPRating —
  cannot recommend its own instruments)
- **Timing split (design rule)**: the EXPERT owns the ranking; the STRATEGY
  (`O_ERN`) owns the timing. The expert surfaces every event inside a fixed
  look-ahead (`earnings_days_look`, a plain setting, not a gene) and stamps
  `days_to_earnings` + feature values onto the recommendation; `O_ERN`'s
  searched entry gene (`rec_days_to_earnings <= X`, 1–5) reads that stamp.
- **Warmup**: `BACKTEST_WARMUP_BARS = 620` (pinned equal to the launcher's
  `_EXPERT_WARMUP_BARS`/`_SUPPORTED_EXPERTS` table entry by a dedicated test —
  a mismatch there would silently starve the expert of history it needs).
- **Stamp contract** (`ba2_common.core.earnings_stamp`): recommendations
  carry `raw_outputs[EARNINGS_STAMP_NAMESPACE]` (namespace
  `"FMPEarningsEvent"`) with `DAYS_TO_EARNINGS_KEY` (`"days_to_earnings"`)
  and `EVENT_DATE_KEY` (`"event_date"`, ISO string). The `O_ERN` entry order
  carries the event date FORWARD onto its own row under
  `ORDER_EVENT_DATE_KEY` (`"earnings_event_date"`) at submit time, so the
  exit's `days_after_event >= Y` condition reads the date off the ORDER
  (which does not change after entry), never off whatever recommendation
  happens to be in hand at exit time (a later, unrelated event by then).
  `stamped_event_date()` is the one reader both the entry-stamp and the
  order-carry-forward code paths share.
- **Key Features**:
  - Composite rank from `w_hist_move` (avg absolute earnings-day move over
    past events), `w_surprise_vol` (std of past EPS surprises), and
    `w_vol_cheapness` (historical move ÷ option-implied move — the only
    feature comparing what you PAY to what you GET; absent when no
    options-capable account is available, never demotes)
  - `min_analysts` (0–5, 0 = gate off; expert default 1 as a data-quality
    floor, not a selection filter — see the setting's own comment for the
    2026-09-01 measurement that moved the default down from 3)
  - `allow_unconfirmed_dates` (off by default — an unconfirmed print date
    slips, and buying volatility ahead of a date that moves buys nothing)
  - `w_dispersion`/`w_revision` DO NOT EXIST: design §9 withheld them on
    measured coverage (~3 in-window `earnings_estimates_quarterly` rows per
    symbol, a forward-biased endpoint, and 1-analyst degeneracy in mid/small
    caps) — unlocked only by a point-in-time replay proving the estimate
    rows predate the events they would score.

**Key Settings** (7 total): `earnings_days_look` (default 10, plain setting
not a gene), `min_hist_events` (default 4), `min_analysts` (default 1, gene
0–5), `allow_unconfirmed_dates` (default False, gene), `w_hist_move` /
`w_surprise_vol` / `w_vol_cheapness` (default 1.0 each, genes).

📖 **Design:** [docs/superpowers/specs/2026-08-31-leaps-grid-design.md](docs/superpowers/specs/2026-08-31-leaps-grid-design.md) §9

## Expert Properties Comparison

| Expert | Can Recommend Instruments | Self-executing¹ | Typical Use Case |
|--------|---------------------------|-----------------|------------------|
| TradingAgents | No | No | Complex AI-driven analysis with debate system |
| FinnHubRating | No | No | Analyst consensus tracking |
| FMPRating | No | No | Price target analysis |
| FMPSenateTraderWeight | **Yes** (opt-in basket mode)² | No | Sophisticated government trading analysis |
| FMPSenateTraderCopy | **Yes** | No | Simple government trade copying |
| PennyMomentumTrader | **Yes** | **Yes** | Live intraday penny-stock momentum |
| FactorRanker | **Yes** | **Yes** | Systematic multi-factor equity ranking |

¹ *Self-executing* experts place and manage their own orders via a dedicated manager (no `ExpertRecommendation`, no SmartRiskManager); order/expert attribution flows through `Transaction.expert_id`.

² Unlike FMPSenateTraderCopy, this is **opt-in**: `can_recommend_instruments`/`should_expand_instrument_jobs` are always set on the class, but `instrument_selection_method` is a per-`ExpertInstance` setting — an instance stays on its configured `static`/`dynamic` selection until an operator explicitly switches it to `expert`.

## Instrument Selection Methods

### Static Selection
- **Manual Configuration**: User manually selects which instruments to analyze
- **Used by**: All experts except FMPSenateTraderCopy in expert mode
- **Best for**: Focused analysis on specific securities

### Dynamic Selection  
- **AI-Driven Prompts**: User provides natural language descriptions of desired instruments
- **Used by**: All experts when configured
- **Best for**: Flexible, criteria-based instrument selection

### Expert-Driven Selection
- **Expert Decides**: The expert algorithm determines which instruments to analyze
- **Used by**: FMPSenateTraderCopy, PennyMomentumTrader, FactorRanker (can recommend their own instruments); FMPSenateTraderWeight when its `instrument_selection_method` setting is switched to `expert` (opt-in basket dispatch, off by default)
- **Best for**: Autonomous trading systems that discover opportunities

### Screener Selection
- **Filter-Based**: Instruments resolved at run time from `StockScreener` filters (market cap, price, volume, …)
- **Used by**: PennyMomentumTrader; FactorRanker when `universe_source="screener"`
- **Best for**: Strategies that rank/trade a broad, dynamically-filtered universe

## Job Scheduling and Management

### Standard Experts
- Create scheduled jobs for each enabled instrument
- Follow traditional scheduling patterns
- Suitable for portfolio-based strategies

### Self-Managing Experts (FMPSenateTraderCopy, PennyMomentumTrader, FactorRanker, FMPSenateTraderWeight in basket mode)
- Can recommend their own instruments
- Use `should_expand_instrument_jobs: False` to run a single batch job (no per-symbol duplication)
- Run analysis and discover/rank trading opportunities autonomously
- Ideal for discovery-based and portfolio/factor strategies
- FMPSenateTraderWeight is opt-in here: it only joins this group once an instance's
  `instrument_selection_method` setting is switched to `expert`; by default it still runs
  as a Standard Expert against `static`/`dynamic`-selected instruments

### Weekly vs Monthly Schedules
- Each analysis schedule (Enter-Market, Open-Positions) can fire **weekly** (chosen days + time) or **monthly** on the **Nth weekday** (e.g. *1st Monday*, *3rd Tuesday*).
- Experts that handle entries and exits in a single batch run (e.g. FactorRanker) set `schedules_open_positions=False` and render only the Enter-Market schedule.
- **Tip:** when running many self-executing experts that hit the same data API, stagger their schedule times to avoid rate-limiting.

## Configuration Best Practices

### For Portfolio Management
1. Use **TradingAgents** for comprehensive AI analysis
2. Combine with **FMPRating** or **FinnHubRating** for consensus validation
3. Configure static instrument selection for your portfolio

### For Government Trading Following
1. Use **FMPSenateTraderWeight** for sophisticated analysis of government trading patterns
   — either against a `static`/`dynamic`-selected instrument list, or (opt-in) against its
   own live-disclosure-derived basket by setting `instrument_selection_method: expert`
2. Use **FMPSenateTraderCopy** for simple copy trading of specific officials
3. FMPSenateTraderCopy works best with `should_expand_instrument_jobs: False`; the same is
   true of FMPSenateTraderWeight once switched into basket (`expert`) mode

### For Market Discovery
1. **FMPSenateTraderCopy** with expert-driven instrument selection
2. Set up minimal scheduling to let the expert discover opportunities
3. Monitor expert recommendations for new instrument additions

## API Requirements

| Expert | Required API Keys | Data Sources |
|--------|------------------|--------------|
| TradingAgents | Various (OpenAI, etc.) | Multiple AI services |
| FinnHubRating | FINNHUB_API_KEY | Finnhub.io |
| FMPRating | FMP_API_KEY | Financial Modeling Prep |
| FMPSenateTraderWeight | FMP_API_KEY | Financial Modeling Prep |
| FMPSenateTraderCopy | FMP_API_KEY | Financial Modeling Prep |
| PennyMomentumTrader | FMP_API_KEY (+ catalyst sources) | Financial Modeling Prep, social/news |
| FactorRanker | FMP_API_KEY | Financial Modeling Prep |

## Risk and Compliance Notes

### Government Trading Data
- **Data Lag**: Government officials must disclose trades within 30-45 days
- **Price Movement**: Opportunities may be reduced by the time data is available
- **Legal Compliance**: Ensure compliance with local regulations regarding government trading data usage

### AI-Based Analysis
- **Model Limitations**: AI recommendations should be validated with traditional analysis
- **Data Quality**: Ensure high-quality data feeds for optimal AI performance
- **Risk Management**: Always use appropriate position sizing and risk controls

## Contributing New Experts

To add a new expert to the platform:

1. **Create Expert Class**: Extend `MarketExpertInterface`
2. **Implement Required Methods**: 
   - `description()`: Human-readable description
   - `get_settings_definitions()`: Configuration options
   - `run_analysis()`: Main analysis logic
   - `render_market_analysis()`: UI rendering
3. **Define Properties** (`get_expert_properties`): e.g. `can_recommend_instruments`, `should_expand_instrument_jobs`, `schedules_open_positions`, `uses_risk_manager` (set `False` for self-executing experts that manage their own orders)
4. **Register Expert**: Add to `ba2_trade_platform/modules/experts/__init__.py`
5. **Test Integration**: Verify settings, scheduling, and analysis functionality

For detailed implementation guidelines, see the existing expert implementations in `ba2_trade_platform/modules/experts/`.