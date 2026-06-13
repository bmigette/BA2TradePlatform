# Expert-Aware Backtest Platform — Design

**Date:** 2026-06-13 · **Status:** validated design (brainstorming complete), pre-implementation.

Goal: backtest BA2 trading **experts** (not just ML models) by running the *real*
live decision/sizing/order code path against historical data, with one optimization
run that jointly tunes the expert, the classic risk manager, and the enter/exit
trade conditions. Kill the provider/expert divergence between the live and test
platforms by extracting shared, pip-installable packages.

TradingAgents is out of scope (LLM, not replayable). The existing ML strategy
backtester is retained as a future "ML expert".

---

## 1. Package topology

Three pip-installable packages (each its own GitHub repo + `pyproject.toml`), strict
one-way dependencies, consumed by **both** platforms:

```
ba2_common  ←  ba2_providers  ←  ba2_experts
     ▲               ▲                ▲
     └──── BA2TradePlatform (live)  ──┘
     └──── BA2TestPlatform (backtest/ML) ┘
```

| Layer | GitHub repo | dist name | import | contents |
|---|---|---|---|---|
| Foundation | `BA2TradeCommon` *(exists)* | `ba2trade-common` | `ba2_common` | interface base classes (`MarketExpertInterface`, provider interfaces, `AccountInterface`/`OptionsAccountInterface`, `BacktestInterface`), enums/types, `TradeConditions`/ruleset engine, shared SQLModel/Pydantic models, **classic** `TradeRiskManagement` + `position_sizing` |
| Data | `BA2TradeProviders` *(new)* | `ba2trade-providers` | `ba2_providers` | all data providers (OHLCV, fundamentals, indicators, news, macro, insider, screener; FMP/Finnhub/AlphaVantage clients) |
| Experts | `BA2TradeExperts` *(new)* | `ba2trade-experts` | `ba2_experts` | expert implementations |

Renames: GitHub `BA2MLTestPlatform → BA2TestPlatform` (done); local folder + remote to follow. `BA2TradePlatform` keeps its name.

`BA2TradeCommon/install.{sh,ps1}` installs the chain from git (common → providers →
experts); a `--editable` flag installs from local clones for dev.

The **smart** risk manager stays in BA2TradePlatform (out of backtest scope).

---

## 2. The backtest contract — two interfaces

**`BacktestInterface` (one method).** No `get_` methods; the expert declares neither
data requirements nor parameter ranges (those belong to the optimizer/config).

```
analyze_as_of(as_of: datetime, context) → Recommendation
```

Guaranteed to run the **same processing code** as live `run_analysis`. Achieved by
decoupling fetch from process inside every expert:
- `_gather(providers, as_of) → data_bundle` — pulls everything via providers (never
  raw HTTP/DB). With `as_of=None` it returns the latest data, exactly as live does today.
- `_process(data_bundle, settings) → Recommendation` — pure decision logic.

Live `run_analysis` = `_gather(live, now)` + `_process`. Backtest `analyze_as_of` =
`_gather(providers, as_of)` + the **same** `_process`. Settings are set from outside
(the optimizer overrides them per trial); the expert just reads `self.settings`.

**`BacktestAccount(AccountInterface)` (the simulated broker).** Implements the same
`AccountInterface` (+ `OptionsAccountInterface`) as `AlpacaAccount`, so the entire
live path — expert → `Recommendation` → ba2trade `TradeConditions`/ruleset → classic
risk manager sizing → `account.submit_order()` — runs unchanged. It fills against the
dataset's point-in-time prices (next bar + fees + slippage), tracks multi-symbol
cash/positions/equity, and evaluates TP/SL/stop exits each bar.

---

## 3. Providers: as-of contract + native cache

Every provider gains a uniform contract (defined in `ba2_common`):

```
get(symbol, as_of=None, lookback=...) → time-indexed data
```

- `as_of=None` ⇒ **latest available** (live path, unchanged behavior).
- `as_of=<date>` ⇒ **point-in-time**: only rows whose *publication/effective* timestamp
  ≤ `as_of` (filing date for insider/senate, `publishedDate` for price targets, report
  `date` for earnings, bar close for OHLCV). This is the single no-lookahead enforcement point.

**Native caching.** First request for a (symbol, field) fetches a bounded history once
(backtest range + warmup), stores it (existing `datasets/cache` + parquet/SQLite),
then serves slices from cache. ~50 fetches for a 50-symbol/500-bar run, not 25 000.
Cache key = provider + symbol + field + range; invalidation is explicit (history is
immutable). Each row carries its **effective date** distinct from its value date —
fundamentals keyed on report/filing date prevents restatement lookahead.

The cache *is* the per-symbol dataset; no expert-declared requirements needed.

**ML datasets remain first-class.** The ML dataset builder still materializes a fixed
feature/target matrix for training, now sourced *through* the provider layer. Two
consumers of one cache: experts read point-in-time slices; ML training reads a
materialized dataset.

---

## 4. Screener history (single provider, real-time + history)

The screener is **one provider class** following the `as_of` contract:
- `as_of=None` → live FMP screener endpoint (today's behavior).
- `as_of=<date>` → reconstructed historical screen, computed **once per (range,
  screen-config) and cached**.

**Reconstruction reuses the real screen logic.** `StockScreener` is refactored along
the fetch/process seam: today it calls FMP's point-in-time endpoint then applies
client-side filters. For history we skip the endpoint and apply the *same* filter
functions (market-cap = shares × as-of price, volume, price, fundamentals, Weinstein
SMA, price-drop) over a historical universe using as-of provider data.

**Universe (bounded to the backtest range + warmup):**
- Broad experts: union of `available-traded/list` (current) + `delisted-companies`
  (`ipoDate`/`delistedDate`); per date keep `ipoDate ≤ date ≤ delistedDate-or-active`.
  The delisted set removes survivorship bias.
- Index-scoped experts (FactorRanker N50): `sp500`/`nasdaq` historical constituents
  (dated add/remove) → exact membership as-of date.

Output: per scan date, a **grouped, labeled** symbol set
`(symbol, scan_date, group/expert, screen_config_hash)`. On each scan/rebalance bar the
expert's tradable universe = that date's set. Static-universe experts are the trivial
case (no reconstruction). All FMP universe endpoints verified available on our key.

---

## 5. Engine & optimization

**Engine loop** (daily clock over the range):
1. Universe for the bar = static list or reconstructed screener set (§4).
2. `expert.analyze_as_of(date)` → `Recommendation`(s) via shared `_process`.
3. Real ba2trade path: enter/exit `TradeConditions`/ruleset → classic risk manager →
   `position_sizing` → `account.submit_order()`.
4. `BacktestAccount` fills next bar (+fees/slippage), evaluates TP/SL/stop exits.
5. Record trades, equity curve, drawdown.

Because steps 3–4 are the live modules, the backtest measures the real system.

**Custom simulator, not a third-party engine.** Evaluated NautilusTrader, vectorbt,
backtrader, zipline-reloaded, QSTrader (2026). All *invert control* — they call your
strategy in their idiom, which would mean rewriting experts/risk/orders into their
API and recreating the divergence we are removing. The fill engine they provide is the
small part; reusing our live decision/sizing/order code is the whole point. So we build
a focused `BacktestAccount` portfolio simulator (bookkeeping: cash, multi-symbol
positions, fills, fees/slippage, TP/SL/stop) and **reuse the existing module's metrics,
results (`Backtest` SQLModel), condition-eval helpers, job-queue and UI**. The existing
`backtesting.py` `MLStrategy` path is retained as the **ML expert's** (single-asset)
engine — one results model, one UI, two engines chosen by expert type. vectorbt is
noted as a possible *future* optimizer accelerator.

**Options:** the simulator implements `OptionsAccountInterface`, so it is options-ready
(legs/spreads/TP-SL). Gated on **historical options data** (FMP lacks cheap chains/
greeks/IV) — ship equities first, enable options when a data source exists.

**Optimization (single joint run).** The existing genetic optimizer owns all parameter
ranges (the expert does not). One trial sets — from outside — **expert params +
classic-RM params (risk %, per-instrument cap, min-stop, ATR mult, diversification) +
enter/exit ruleset/`TradeConditions` params (TP/SL %, confidence thresholds, indicator
levels)** — runs the deterministic backtest, scores one fitness metric (Sharpe/return/
profit-factor/etc., already in the `Backtest` model). One run tunes signal + sizing +
conditions jointly. Same cache + same params ⇒ identical result.

**Cadence:** daily v1 (covers rating/factor/event experts). Intraday experts
(PennyMomentum) need intraday data — out of scope for v1.

---

## 6. Migration & rollout (phased)

- **Phase 0 — Scaffold packages.** Create `BA2TradeProviders`, `BA2TradeExperts`;
  `pyproject.toml` each + `BA2TradeCommon/install.{sh,ps1}`. Move code: interfaces/
  types/`TradeConditions`/models/classic risk manager/`position_sizing` → common;
  providers → providers; experts → experts.
- **Phase 1 — Provider `as_of` + native cache.** Add the contract (live = `as_of=None`,
  unchanged). Refactor experts to split `_gather`/`_process`. **Golden test:** live
  `run_analysis` and `analyze_as_of(now)` yield identical recommendations.
- **Phase 2 — Engine + `BacktestAccount(AccountInterface)`** in BA2TestPlatform. First
  daily backtests on the clean experts (EarningsDrift, InsiderClusterBuy).
- **Phase 3 — Screener provider history** + universe + grouped/labeled cache → enables
  FactorRanker / screener experts (incl. Weinstein filter).
- **Phase 4 — Joint optimizer:** expand the genetic search space to expert + classic-RM
  + ruleset-condition params in one run.
- **Phase 5 — Cache-management UI** (disk usage per type, clean-all / by-type / by-date)
  + re-source ML datasets through the provider layer (training keeps working).
- **Phase 6 — Migrate BA2TradePlatform** onto the packages (kills the divergence). Can
  run parallel/last.

**Scope guardrails (YAGNI):** daily cadence only; no smart-RM; no intraday experts;
equities first (options-ready interface); survivorship handled via the delisted
universe; reuse the genetic optimizer and `Backtest`/metrics models rather than new ones.

## Backtestable experts (from FMP feasibility survey, 2026-06-13)
Clean: **FMPEarningsDrift**, **FMPInsiderClusterBuy**. With care: Senate traders,
FactorRanker, Weinstein filter. Reconstruction needed: **FMPRating** (consensus is
point-in-time; rebuild from `grades-historical` + dated `price-target`). See
`docs/FMP_BACKTEST_FEASIBILITY.md`.
