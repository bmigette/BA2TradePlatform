# PennyMomentumTrader - Design Document

## Overview

A new `LiveExpert` type that autonomously scans for penny stock momentum opportunities, triages candidates using multi-source news aggregation and LLM reasoning, monitors technical entry conditions in real-time, and executes trades with pre-calculated position sizing. Combines API-driven screening with LLM-powered analysis to catch explosive small-cap moves (uplistings, FDA catalysts, contract wins, insider buying) before they go parabolic.

---

## Architecture

### New Base Class: `LiveExpertInterface`

**File:** `ba2_trade_platform/core/interfaces/LiveExpertInterface.py`

Extends `MarketExpertInterface` with continuous execution lifecycle:

```python
class LiveExpertInterface(MarketExpertInterface):
    def start(self) -> None
    def stop(self) -> None
    def request_manual_start(self) -> None
    def is_running -> bool  # property
    def on_phase_complete(self, phase_name: str) -> None
```

Key behaviors:
- Single dedicated thread with event-driven phase chaining
- Timezone-aware scheduling: configurable start time (default 7:00 AM EST), converts to host timezone
- Configurable trading days (Mon-Fri, each day togglable, all on by default)
- **Auto-start rule:** Only starts if current time is before kick-off time. If app starts after kick-off, the daily process does NOT auto-start. User can force-start via manual analysis tab.
- App startup: platform discovers all `LiveExpertInterface` subclasses and calls `start()` on each
- App shutdown: platform calls `stop()` on each, thread joins gracefully

### New Screener Interface: `ScreenerProviderInterface`

**File:** `ba2_trade_platform/core/interfaces/ScreenerProviderInterface.py`

```python
class ScreenerProviderInterface:
    def screen_stocks(self, filters: dict) -> List[dict]
```

Filters supported:
- `price_min`, `price_max` (float)
- `volume_min` (int) - average daily volume
- `market_cap_min`, `market_cap_max` (float)
- `float_max` (int) - max shares float
- `exchanges` (List[str]) - e.g., ["NASDAQ", "NYSE", "AMEX"]
- `sector_exclude` (List[str]) - sectors to skip
- `relative_volume_min` (float) - today vs average multiplier
- `premarket_gap_pct_min` (float) - minimum pre-market gap %

**First implementation:** `FMPScreenerProvider` in `ba2_trade_platform/modules/dataproviders/FMPScreenerProvider.py`

Uses FMP's `/stock-screener` endpoint. Returns list of dicts with: symbol, price, volume, market_cap, sector, beta, float_shares, exchange, company_name.

### Expert Package: `modules/experts/PennyMomentumTrader/`

```
PennyMomentumTrader/
    __init__.py          # PennyMomentumTrader class (inherits LiveExpertInterface)
    ui.py                # PennyMomentumTraderUI class (custom MarketAnalysis rendering)
    conditions.py        # Structured condition schema, evaluator, and registry
    trade_manager.py     # Position sizing, entry/exit execution, partial exits
```

---

## Expert Settings

### Schedule & Lifecycle
| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `start_time` | str | "07:00" | Daily kick-off time (EST) |
| `market_timezone` | str | "US/Eastern" | Market timezone for schedule |
| `trading_days` | json | `{"mon":true,"tue":true,"wed":true,"thu":true,"fri":true}` | Active trading days |
| `monitoring_interval_seconds` | int | 60 | Entry/exit condition check interval |

### Screening
| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `scan_price_min` | float | 0.10 | Minimum stock price |
| `scan_price_max` | float | 5.00 | Maximum stock price |
| `scan_volume_min` | int | 500000 | Minimum average daily volume |
| `scan_relative_volume_min` | float | 2.0 | Minimum relative volume (today vs avg) |
| `scan_market_cap_min` | float | 10000000 | Minimum market cap ($) |
| `scan_market_cap_max` | float | 500000000 | Maximum market cap ($) |
| `scan_float_max` | int | 20000000 | Maximum float (shares) |
| `scan_premarket_gap_pct_min` | float | 5.0 | Minimum pre-market gap % |
| `scan_sector_exclude` | str | "" | Comma-separated sectors to exclude |
| `screener_provider` | str | "FMP" | Screener provider to use |

### Triage & Analysis
| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `max_scan_candidates` | int | 50 | Max candidates from screener |
| `max_quick_filter_candidates` | int | 20 | Survivors from quick LLM filter |
| `max_final_candidates` | int | 10 | Final candidates per daily run |
| `max_monitored_symbols` | int | 20 | Absolute cap on monitored symbols |
| `max_entry_age_days` | int | 3 | Days before dropping unmatched monitor |
| `max_holding_days` | int | 30 | Max days to hold a position (swing limit) |

### LLM Models
| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `scanning_llm` | str (ModelSelector) | "OpenAI/gpt-4o-mini" | LLM for quick filter (Stage A) |
| `deep_analysis_llm` | str (ModelSelector) | "OpenAI/gpt-4o" | High-reasoning LLM for deep triage (Stage B) |
| `websearch_llm` | str (ModelSelector, required_labels=["websearch"]) | "NagaAI/gpt-4o-search-preview" | LLM for web search data gathering |
| `entry_definition_llm` | str (ModelSelector) | "OpenAI/gpt-4o" | LLM for defining entry/exit conditions |

### Data Vendors (all providers aggregated, not fallback)
| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `vendor_news` | list | ["ai", "alpaca", "fmp", "finnhub"] | News providers (all queried) |
| `vendor_fundamentals` | list | ["fmp", "alpha_vantage"] | Fundamentals providers |
| `vendor_insider` | list | ["fmp"] | Insider trading providers |
| `vendor_social_media` | list | ["ai"] | Social sentiment providers |
| `vendor_ohlcv` | list | ["yfinance"] | OHLCV for technical monitoring |

Note: Existing expert builtin settings (`max_virtual_equity_per_instrument_percent`, `min_available_balance_pct`, etc.) are inherited from `MarketExpertInterface` and enforced at trade execution time.

---

## Daily Lifecycle (Phase Chaining)

All phases chain automatically from the configured start time. Each phase triggers the next on completion.

```
[Kick-off at start_time]
        |
        v
+------------------+
| Phase 0: Review  |  Check previous day's swing positions
| Previous Day     |  Fetch fresh news/data for existing monitors
|                  |  Update exit conditions via LLM if needed
+------------------+
        |
        v
  has_sufficient_balance_for_entry()?
        |
    NO /  \ YES
      /    \
     v      v
  (skip   +------------------+
  to      | Phase 1: Screen  |  FMPScreenerProvider -> ~50 candidates
  phase   | (API only)       |  Pre-filter against account's available instruments
  4)      +------------------+
                  |
                  v
          +------------------+
          | Phase 2: Quick   |  Brief profiles for 50 candidates
          | Filter (LLM)     |  One LLM call (scanning_llm) -> ~20 survivors
          +------------------+
                  |
                  v
          +------------------+
          | Phase 3: Deep    |  Aggregate news from ALL providers for 20 survivors
          | Triage (LLM)     |  Full dossier per stock (news, insider, fundamentals)
          |                  |  High-reasoning LLM (deep_analysis_llm) -> ~10 finalists
          |                  |  Calculate qty per symbol (balance weighted by confidence)
          +------------------+
                  |
                  v
+------------------+
| Phase 4: Entry   |  LLM (entry_definition_llm) defines structured conditions
| Condition Setup  |  Per-symbol: entry conditions, stop-loss, take-profit tiers
|                  |  Creates MarketAnalysis record with state + AnalysisOutputs
+------------------+
        |
        v
+------------------+
| Phase 5: Monitor |  Loop every monitoring_interval_seconds (default 60s)
| (no LLM)        |  Check technical conditions via OHLCV/indicator providers
|                  |  On entry trigger: execute trade with pre-calculated qty
|                  |  On exit trigger: close/partial-close position
|                  |  Drop symbols older than max_entry_age_days
|                  |  Stop at market close (configurable end time)
+------------------+
        |
        v
+------------------+
| Phase 6: EOD     |  Close intraday positions
| Cleanup          |  Evaluate swing positions (keep or flag for tomorrow)
|                  |  Update MarketAnalysis state
|                  |  Thread sleeps until next trading day
+------------------+
```

### Low Balance Behavior
When `has_sufficient_balance_for_entry()` is false:
- **Skip:** Phases 1-3 (screening, quick filter, deep triage) — no new symbols added
- **Still run:** Phase 0 (review existing positions, update exit conditions via LLM with fresh data), Phase 4 (update entry conditions for already-queued symbols), Phase 5 (monitor entries for pre-existing monitors with pre-calculated qty, monitor exits), Phase 6 (EOD cleanup)

### Manual Trigger
- Available from the manual analysis tab as an expert action
- Can force-start the full pipeline at any time (even after kick-off window)
- Can restart mid-day (creates new MarketAnalysis, existing monitors preserved unless replaced)

---

## Structured Condition Schema

**File:** `PennyMomentumTrader/conditions.py`

The LLM outputs structured JSON conditions. The monitor evaluates them without LLM calls.

### Condition Types Registry

```python
CONDITION_TYPES = {
    # Price-based
    "price_above": {"params": ["value"]},
    "price_below": {"params": ["value"]},
    "price_above_ema": {"params": ["period", "timeframe"]},
    "price_below_ema": {"params": ["period", "timeframe"]},
    "price_above_sma": {"params": ["period", "timeframe"]},
    "price_above_vwap": {"params": ["timeframe"]},
    "price_below_vwap": {"params": ["timeframe"]},
    "opening_range_breakout": {"params": ["minutes"]},

    # Volume-based
    "volume_above_avg": {"params": ["multiplier", "window"]},
    "volume_spike": {"params": ["multiplier", "minutes"]},

    # Indicator-based
    "rsi_above": {"params": ["threshold", "period", "timeframe"]},
    "rsi_below": {"params": ["threshold", "period", "timeframe"]},
    "rsi_between": {"params": ["min", "max", "period", "timeframe"]},
    "macd_bullish_cross": {"params": ["timeframe"]},
    "macd_bearish_cross": {"params": ["timeframe"]},
    "golden_cross": {"params": ["fast_period", "slow_period", "timeframe"]},
    "death_cross": {"params": ["fast_period", "slow_period", "timeframe"]},

    # Percentage-based (relative to entry price)
    "percent_above_entry": {"params": ["percent"]},
    "percent_below_entry": {"params": ["percent"]},

    # Time-based
    "time_after": {"params": ["time"]},  # e.g., "09:45" EST
    "time_before": {"params": ["time"]},
}
```

### Condition Composition

Conditions combine with `all` (AND) and `any` (OR) operators:

```json
{
  "entry": {
    "all": [
      {"type": "price_above_ema", "period": 9, "timeframe": "15m"},
      {"type": "volume_above_avg", "multiplier": 3, "window": 20},
      {"type": "rsi_between", "min": 40, "max": 70, "period": 14, "timeframe": "15m"},
      {"type": "time_after", "time": "09:45"}
    ]
  },
  "stop_loss": {
    "any": [
      {"type": "percent_below_entry", "percent": 5},
      {"type": "price_below_ema", "period": 200, "timeframe": "1d"}
    ]
  },
  "take_profit": [
    {"condition": {"type": "percent_above_entry", "percent": 10}, "exit_pct": 50},
    {"condition": {"type": "percent_above_entry", "percent": 20}, "exit_pct": 100}
  ]
}
```

### Condition Evaluator

```python
class ConditionEvaluator:
    def evaluate(self, conditions: dict, market_data: dict) -> bool
    def evaluate_single(self, condition: dict, market_data: dict) -> bool
    def get_condition_status(self, conditions: dict, market_data: dict) -> dict
        # Returns per-condition met/unmet status for UI display
```

The evaluator uses existing data providers:
- `PandasIndicatorCalc` for EMA, SMA, RSI, MACD, Bollinger Bands
- OHLCV providers for price and volume data
- Calculations done locally, no API cost per check

---

## Trade Manager

**File:** `PennyMomentumTrader/trade_manager.py`

Handles position sizing, entry execution, exit execution, and partial exits. Reuses patterns from `SmartRiskManagerToolkit` but with its own flow.

### Position Sizing (Pre-calculated at Triage)

```python
def calculate_position_sizes(self, candidates: List[dict], available_balance: float) -> dict:
    """
    Distribute available_balance across candidates weighted by confidence score.

    For each candidate:
      weight = confidence / sum(all_confidences)
      raw_allocation = available_balance * weight
      clamped_allocation = min(raw_allocation, max_per_instrument)
      qty = floor(clamped_allocation / current_price)

    Returns: {symbol: {"qty": int, "allocation": float, "confidence": float}}
    """
```

### Entry Execution (On Condition Trigger)

When entry conditions are met:
1. Validate balance: clamp qty to current available balance if needed
2. Validate instrument still available on broker
3. Create `ExpertRecommendation` for the symbol
4. Create `TradingOrder` (MARKET order) via account interface
5. Create dependent stop-loss and take-profit orders (OCO pattern from SmartRiskManager)
6. Update MarketAnalysis state: monitor status -> "triggered", order_id reference
7. Log activity

### Exit Execution

- **Stop-loss / Take-profit:** Handled automatically via OCO dependent orders (existing TradeManager infrastructure)
- **Partial exits:** Multiple take-profit tiers, each closing a percentage of the position
- **Time-based exit:** Close positions exceeding `max_holding_days`
- **LLM-updated exits:** Next day's Phase 0 can override exit conditions with fresh analysis
- **EOD intraday close:** Positions marked as intraday are closed at market end

### Intraday vs Swing

The LLM determines during triage whether a position is intraday or swing based on the catalyst type:
- **Intraday:** Opening range breakouts, momentum plays, gap-and-go setups
- **Swing:** Uplisting plays, FDA calendar bets, insider buying patterns

Swing positions carry over across days but are capped at `max_holding_days` (default 30).

---

## State & Storage

### MarketAnalysis Record

One `MarketAnalysis` record created per daily run (or manual trigger). The `state` JSON field stores operational data:

```python
state = {
    "phase": "monitoring",           # Current phase for resume on restart
    "phase_started_at": "2026-03-16T07:00:00-04:00",
    "run_date": "2026-03-16",
    "settings_snapshot": {...},      # Frozen config for this run

    "scan_results": [                # Phase 1 output: ~50 candidates
        {"symbol": "SHAZ", "price": 1.23, "volume": 2500000, "market_cap": 45000000, ...},
        ...
    ],

    "quick_filter": {                # Phase 2 output
        "survivors": ["SHAZ", "BATL", ...],  # ~20 symbols
        "rejected": [{"symbol": "XYZ", "reason": "No catalyst identified"}, ...],
        "llm_reasoning": "..."
    },

    "triage_results": [              # Phase 3 output: ~10 finalists
        {
            "symbol": "SHAZ",
            "confidence": 82.5,
            "catalyst": "Nasdaq uplisting + NVIDIA partnership",
            "strategy": "swing",
            "pre_calculated_qty": 150,
            "allocation": 450.00,
            "deep_analysis_summary": "..."
        },
        ...
    ],

    "monitors": {                    # Phase 4-5: active monitoring state
        "SHAZ": {
            "status": "watching",    # watching | triggered | expired | closed
            "added_date": "2026-03-16",
            "entry_conditions": {...},
            "exit_conditions": {...},
            "pre_calculated_qty": 150,
            "condition_status": {    # Last evaluation results
                "price_above_ema_9_15m": true,
                "volume_above_avg_3x": false,
                "rsi_between_40_70": true
            },
            "last_checked": "2026-03-16T10:15:00-04:00",
            "last_price": 1.45
        },
        "BATL": {
            "status": "triggered",
            "triggered_at": "2026-03-16T09:47:00-04:00",
            "order_id": 456,
            "entry_price": 2.34,
            "exit_conditions": {...}
        }
    }
}
```

### AnalysisOutput Records

Heavy content stored as `AnalysisOutput` linked to the MarketAnalysis:
- `scan_raw_screener_response` — Full screener API response
- `triage_news_{symbol}` — Aggregated news per symbol from all providers
- `triage_insider_{symbol}` — Insider trading data per symbol
- `triage_fundamentals_{symbol}` — Fundamentals data per symbol
- `triage_llm_prompt_stage_a` — Quick filter LLM prompt
- `triage_llm_response_stage_a` — Quick filter LLM response
- `triage_llm_prompt_stage_b_{symbol}` — Deep triage prompt per symbol
- `triage_llm_response_stage_b_{symbol}` — Deep triage response per symbol
- `conditions_llm_prompt_{symbol}` — Entry/exit condition definition prompt
- `conditions_llm_response_{symbol}` — Entry/exit condition definition response

---

## Custom UI

**File:** `PennyMomentumTrader/ui.py`

Class `PennyMomentumTraderUI` renders the MarketAnalysis with 5 tabs:

### Tab 1: Scan Results
- Table of all ~50 candidates from screener
- Columns: Symbol, Price, Volume, RelVol, Market Cap, Float, Sector, Gap%
- Color-coded rows: green = passed quick filter, red = rejected
- Sortable columns

### Tab 2: Triage
- **Quick Filter section:** List of ~20 survivors with LLM reasoning summary
- **Deep Triage section:** Cards for ~10 finalists showing:
  - Symbol, confidence score (progress bar), catalyst summary
  - Strategy type (intraday/swing badge)
  - Pre-calculated qty and allocation
  - Expandable: full deep analysis text

### Tab 3: Active Monitors
- Per-symbol cards showing:
  - Symbol name, status badge (watching/triggered/expired/closed)
  - Entry conditions checklist (green check / red X per condition)
  - Last checked time, last price
  - Days remaining before expiry
  - If triggered: entry price, current P&L, exit condition status

### Tab 4: Trades
- Table of executed trades
- Columns: Symbol, Entry Price, Current Price, P&L%, Qty, Strategy, Status
- Partial exit history (tier 1 hit, tier 2 pending, etc.)
- Links to TradingOrder records

### Tab 5: Raw Data
- Expandable sections per symbol with:
  - Full news articles from all providers
  - Insider trading data
  - Fundamentals snapshot
  - LLM prompts and responses (quick filter, deep triage, condition definition)

---

## Account Integration

### Pre-filtering Against Broker

During Phase 1 (after screener returns candidates), filter out symbols not available on the linked account. Uses the same validation as the dynamic instrument selector:

```python
account = get_account_instance_from_id(self.instance.account_id)
available = account.validate_instruments(candidate_symbols)
candidates = [c for c in candidates if c["symbol"] in available]
```

### Trade Execution

Uses existing `AccountInterface` methods:
- `open_buy_position(symbol, quantity, ...)` for entries
- `close_position(symbol, quantity, ...)` for exits and partial exits
- OCO order creation for stop-loss / take-profit (existing TradeManager pattern)

---

## Expert Properties

```python
@classmethod
def get_expert_properties(cls) -> Dict[str, Any]:
    return {
        "can_recommend_instruments": True,
        "should_expand_instrument_jobs": False,
    }

@classmethod
def get_expert_actions(cls) -> List[Dict]:
    return [
        {
            "name": "Start Scan",
            "description": "Force-start the scanning pipeline",
            "callback": "request_manual_start"
        },
        {
            "name": "Stop Scan",
            "description": "Stop the current scanning/monitoring process",
            "callback": "request_stop"
        }
    ]
```

---

## File Summary

| File | Purpose |
|------|---------|
| `core/interfaces/LiveExpertInterface.py` | New base class with start/stop lifecycle, thread management, timezone-aware scheduling |
| `core/interfaces/ScreenerProviderInterface.py` | Abstract screener interface |
| `modules/dataproviders/FMPScreenerProvider.py` | FMP stock screener implementation |
| `modules/experts/PennyMomentumTrader/__init__.py` | Expert class: phases, LLM orchestration, state management |
| `modules/experts/PennyMomentumTrader/ui.py` | Custom 5-tab MarketAnalysis UI |
| `modules/experts/PennyMomentumTrader/conditions.py` | Condition schema, registry, evaluator |
| `modules/experts/PennyMomentumTrader/trade_manager.py` | Position sizing, entry/exit execution |

---

## Configuration Defaults Summary

```
Start time:              07:00 EST
Trading days:            Mon-Fri (all enabled)
Monitoring interval:     60 seconds
Scan candidates:         50
Quick filter survivors:  20
Final candidates:        10
Max monitored symbols:   20
Max entry age:           3 days
Max holding period:      30 days
Price range:             $0.10 - $5.00
Min volume:              500,000
Relative volume:         2.0x
Market cap:              $10M - $500M
Max float:               20M shares
Pre-market gap:          5%+
```
