# PennyMomentumTrader

AI-driven live trading expert that scans US penny stocks daily, applies a multi-stage LLM analysis pipeline, and monitors structured entry/exit conditions in real time.

---

## Overview

PennyMomentumTrader runs as a background daemon thread (via `LiveExpertInterface`). Each trading day it executes a sequential pipeline that progressively narrows a universe of penny stocks down to a watched list, then monitors those symbols for entry triggers and manages exits.

**Key characteristics:**
- Price range: $0.10 – $5.00 (configurable)
- Focus: momentum catalysts — volume surges, breakouts, social buzz, news events
- Risk level: HIGH — all positions are classified as high-risk
- Position sizing: confidence-weighted across available balance
- Social data: real-time StockTwits sentiment integrated at scan and deep-triage stages

---

## Pipeline

The pipeline runs once per trading day at a configured kickoff time. Manual start is also available from the UI.

```
Phase 0   Review existing positions
   │
   ├── Insufficient balance? → Skip phases 1–3, go to Phase 4 (monitor only)
   │
Phase 1   Screen (no LLM)
   │         FMP screener → all matches → sort by volume → top N (default 50)
   │
Phase 2   Quick Filter (LLM + StockTwits)
   │         Enrich candidates with StockTwits sentiment → fast LLM picks top 15
   │
Phase 1b  LLM Discovery (websearch LLM)
   │         Finds 10 fresh symbols NOT already in screener results
   │
Phase 3   Deep Triage (LLM per symbol, all 25 combined)
   │         News + Fundamentals + Insider + Social → confidence score
   │         Top 15 by confidence become finalists
   │
Phase 4   Entry Conditions (LLM per new finalist)
   │         Generates structured entry, stop-loss, take-profit conditions
   │         Adds to monitored_symbols (up to max 40 simultaneously)
   │
Phase 5   Monitor (real-time loop until market close)
   │         Evaluates conditions against live OHLCV every N seconds
   │         Executes entries when conditions met, exits on stop-loss/take-profit
   │
Phase 6   EOD
            Marks MarketAnalysis as completed
```

### Phase 0 — Review Existing Positions

Loads all open positions via `PennyTradeManager`. Any position held longer than `max_holding_days` is force-exited. Open position symbols are excluded from all subsequent screening.

### Phase 1 — Screen

Calls the configured screener provider (default: FMP `/stock-screener`). Filters by price, volume, market cap, and optionally excludes sectors. Returns **all** matches, sorted by volume descending, capped at `max_scan_candidates` (default 50). Symbols already held are excluded. Surviving symbols are queued for `InstrumentAutoAdder`.

### Phase 2 — Quick Filter

Before calling the LLM, if `vendor_social` includes `stocktwits`, **all** screener candidates are enriched with live StockTwits data:

| Field | Meaning |
|---|---|
| `st_watchlist` | Number of StockTwits users watching this stock |
| `st_bullish_pct` | % of tagged messages that are Bullish |
| `st_bearish_pct` | % of tagged messages that are Bearish |
| `st_trending` | Whether the stock is currently trending |
| `st_trending_score` | Positive = gaining attention, negative = losing |

The fast LLM (default: `gpt-4o-mini`) then selects the top `max_quick_filter_candidates` (default **15**) based on sector quality, volume relative to average, market cap, exchange, price range, and social signals.

### Phase 1b — LLM Discovery

A websearch-capable LLM (default: `gpt-4o-search-preview`) is prompted to find `max_discovery_candidates` (default **10**) additional penny stocks with strong momentum catalysts **not already in the screener results** (open positions, previously monitored symbols, and all screener candidates are all excluded from the discovery prompt). Each returned symbol gets a live price check; those with no valid price are dropped.

### Phase 3 — Deep Triage

Runs on the combined **25 candidates** (15 survivors + 10 discovered). For each symbol, four data sources are gathered in parallel and fed to the deep analysis LLM:

| Data source | Setting | Default |
|---|---|---|
| News (3-day lookback) | `vendor_news` | alpaca, fmp, finnhub |
| Fundamentals | `vendor_fundamentals` | fmp |
| Insider transactions (90-day) | `vendor_insider` | fmp |
| Social sentiment | `vendor_social` | stocktwits |

The LLM outputs a JSON object with `confidence` (0–100), `catalyst`, `strategy` (`intraday`/`swing`), `expected_profit_pct`, `risk_assessment`, and `reasoning`.

Symbols with `confidence < 40` are dropped. All remaining are sorted by confidence descending and capped at `max_final_candidates` (default **15**). An `ExpertRecommendation` record is created for each finalist.

### Phase 4 — Entry Conditions

For each finalist not already being monitored, the entry-conditions LLM (default: `gpt-4o`) generates a structured JSON condition set:

```json
{
  "entry": { "type": "and", "conditions": [...] },
  "stop_loss": { "type": "price_below", "params": { "value": 1.45 } },
  "take_profit": [
    { "condition": { "type": "percent_above_entry", "params": { "percent": 15 } }, "exit_pct": 50 },
    { "condition": { "type": "percent_above_entry", "params": { "percent": 30 } }, "exit_pct": 100 }
  ]
}
```

Conditions use the deterministic `ConditionEvaluator` schema (no LLM at evaluation time). Symbols are added to `monitored_symbols` state up to `max_monitored_symbols` (default 40). Monitors older than `max_entry_age_days` (default 3) are expired.

### Phase 5 — Monitor

Real-time loop that checks every `monitoring_interval_seconds` (default 60s) until market close:

- **Entry**: evaluates `entry_conditions` for watched symbols; executes market buy when met
- **Intraday EOD hard-exit**: force-exits intraday positions 15 minutes before market close
- **Stop-loss**: evaluates `stop_loss` condition for open positions; full exit when triggered
- **Take-profit**: evaluates each tier sequentially; partial or full exit at each tier (already-fired tiers are tracked and skipped)
- **Exit condition update**: every `exit_update_interval_ticks` monitor cycles (default 30), the LLM re-evaluates exit conditions for open positions using fresh news and social data — can tighten stops, adjust take-profit targets, or add conditions based on intraday events

State (last price, condition status, check timestamp) is persisted to `MarketAnalysis.state` on every tick for UI display.

### Phase 6 — EOD

Marks `MarketAnalysis.status = COMPLETED` and records `completed_at`.

---

## Condition Types

The structured condition schema supports the following types, combinable with `and`, `or`, `not` logic:

**Price:** `price_above`, `price_below`, `price_above_ema`, `price_below_ema`, `price_above_sma`, `price_below_sma`, `price_above_vwap`, `price_below_vwap`, `opening_range_breakout`

**Volume:** `volume_above_avg`, `volume_spike`

**Indicators:** `rsi_above`, `rsi_below`, `rsi_between`, `macd_bullish_cross`, `macd_bearish_cross`, `ema_cross_above`, `ema_cross_below`

**Relative to entry:** `percent_above_entry`, `percent_below_entry`

**Time:** `time_after`, `time_before`

---

## Settings Reference

### LLM Models

| Setting | Default | Description |
|---|---|---|
| `scanning_llm` | `OpenAI/gpt-4o-mini` | Phase 2 quick filter |
| `deep_analysis_llm` | `OpenAI/gpt-4o` | Phase 3 deep triage (per symbol) |
| `websearch_llm` | `NagaAI/gpt-4o-search-preview` | Phase 5 social websearch (if configured) |
| `entry_definition_llm` | `OpenAI/gpt-4o` | Phase 4 entry/exit condition generation |
| `exit_update_llm` | `OpenAI/gpt-4o-mini` | Periodic exit condition re-evaluation |
| `discovery_llm` | `NagaAI/gpt-4o-search-preview` | Phase 1b LLM discovery |

### Screener Filters

| Setting | Default | Description |
|---|---|---|
| `scan_price_min` | 0.10 | Minimum stock price |
| `scan_price_max` | 5.00 | Maximum stock price |
| `scan_volume_min` | 500,000 | Minimum average volume |
| `scan_market_cap_min` | $10M | Minimum market cap |
| `scan_market_cap_max` | $500M | Maximum market cap |
| `scan_sector_exclude` | _(empty)_ | Comma-separated sectors to skip |
| `screener_provider` | `fmp` | Screener data source |

### Pipeline Limits

| Setting | Default | Description |
|---|---|---|
| `max_scan_candidates` | 50 | Top-N from screener (by volume) |
| `max_quick_filter_candidates` | 15 | Survivors from quick filter (Phase 2 output) |
| `max_discovery_candidates` | 10 | Extra symbols from LLM discovery (Phase 1b) |
| `max_final_candidates` | 15 | Max finalists from deep triage (top N by confidence) |
| `max_monitored_symbols` | 40 | Max simultaneously watched symbols |
| `max_entry_age_days` | 3 | Days before a watch entry expires |
| `max_holding_days` | 14 | Max days to hold a position before forced exit |
| `min_confidence_threshold` | 55 | Minimum confidence (1-100) for deep triage finalists |
| `exit_update_interval_ticks` | 30 | Monitor ticks between LLM exit-condition re-evaluations (0 = disabled) |

### Data Vendors

| Setting | Default | Options |
|---|---|---|
| `vendor_news` | `["alpaca", "fmp", "finnhub"]` | alpaca, alphavantage, ai, fmp, finnhub, google |
| `vendor_fundamentals` | `["fmp"]` | alpha_vantage, ai, fmp |
| `vendor_insider` | `["fmp"]` | fmp |
| `vendor_social` | `["stocktwits"]` | stocktwits, websearch |
| `vendor_ohlcv` | `["fmp"]` | fmp |
| `vendor_live_price` | `fmp` | fmp, account |

### Scheduling (inherited from LiveExpertInterface)

| Setting | Default | Description |
|---|---|---|
| `start_time` | `07:00` | Daily kickoff time (market timezone) |
| `market_timezone` | `US/Eastern` | Timezone for all scheduling |
| `market_close_time` | `16:00` | Stop monitoring at this time |
| `trading_days` | Mon–Fri | Which days to run |
| `monitoring_interval_seconds` | 60 | Seconds between condition checks in Phase 5 |

---

## State Schema

`MarketAnalysis.state` stores the full pipeline state and is readable by the UI:

```json
{
  "phase": "monitoring",
  "open_positions": [{ "symbol": "ABCD", "qty": 100, "entry_price": 1.23 }],
  "scan_results": [...],
  "quick_filter_survivors": ["ABCD", "EFGH", ...],
  "deep_triage_results": {
    "ABCD": {
      "confidence": 78, "catalyst": "...", "strategy": "swing",
      "expected_profit_pct": 25, "risk_assessment": "...", "reasoning": "...",
      "qty": 500, "allocation": 615.00
    }
  },
  "monitored_symbols": {
    "ABCD": {
      "status": "watching",
      "entry_conditions": { "type": "and", "conditions": [...] },
      "exit_conditions": { "stop_loss": {...}, "take_profit": [...] },
      "confidence": 78,
      "catalyst": "...",
      "strategy": "swing",
      "qty": 500,
      "allocation": 615.00,
      "created_at": "2026-03-16T07:05:00Z",
      "last_price": 1.27,
      "last_checked": "2026-03-16T14:30:00Z",
      "entry_conditions_status": { "met": false, "details": [...] }
    }
  },
  "executed_trades": [
    { "symbol": "ABCD", "action": "entry", "reason": "entry conditions met", "timestamp": "..." }
  ],
  "completed_at": "2026-03-16T16:05:00Z"
}
```

---

## Position Sizing

Positions are sized by `PennyTradeManager` using confidence-weighted allocation:

```
allocation_i = available_balance × (confidence_i / sum_of_all_confidences)
allocation_i = min(allocation_i, max_per_instrument_pct × virtual_equity)
qty_i = floor(allocation_i / price_i)
```

Position sizes are pre-calculated in Phase 3 and re-verified at execution time in Phase 5.

---

## Dependencies

| Library | Purpose |
|---|---|
| `curl_cffi` | StockTwits API access (Cloudflare bypass) |
| `pandas`, `numpy` | Condition evaluation (EMA, SMA, VWAP, RSI, MACD) |
| `pytz` | Market timezone handling |
| FMP API key | Screener + news + fundamentals + insider data |
| LLM API keys | OpenAI / NagaAI (per model configuration) |

---

## File Structure

```
PennyMomentumTrader/
├── __init__.py          # Main expert class + pipeline phases
├── prompts.py           # LLM prompt builders for each phase
├── conditions.py        # Condition type registry + ConditionEvaluator
├── trade_manager.py     # Position sizing + trade execution
├── ui.py                # NiceGUI renderer for MarketAnalysis view
└── PENNYMOMENTUMTRADER.md  # This file
```
