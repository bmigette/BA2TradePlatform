# PennyMomentumTrader Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a PennyMomentumTrader LiveExpert that autonomously scans for penny stock momentum opportunities, triages candidates via multi-provider news aggregation and LLM reasoning, monitors technical entry conditions in real-time, and executes trades with pre-calculated position sizing.

**Architecture:** New `LiveExpertInterface` base class extends `MarketExpertInterface` with start/stop lifecycle and dedicated thread. New `ScreenerProviderInterface` abstracts stock screening (FMP first). Expert runs 7 phases in a single thread: review previous day, screen, quick filter, deep triage, entry condition setup, monitor, EOD cleanup. Structured JSON condition schema enables LLM-defined entry/exit rules evaluated without LLM calls.

**Tech Stack:** Python 3.11+, SQLModel, NiceGUI, LangChain (via ModelFactory), APScheduler (via existing JobManager pattern), pytz for timezone handling.

**Design doc:** `docs/plans/2026-03-16-penny-momentum-trader-design.md`

---

## Task 1: ScreenerProviderInterface + FMP Implementation

**Files:**
- Create: `ba2_trade_platform/core/interfaces/ScreenerProviderInterface.py`
- Create: `ba2_trade_platform/modules/dataproviders/screener/__init__.py`
- Create: `ba2_trade_platform/modules/dataproviders/screener/FMPScreenerProvider.py`
- Modify: `ba2_trade_platform/core/interfaces/__init__.py` (add ScreenerProviderInterface export)
- Modify: `ba2_trade_platform/modules/dataproviders/__init__.py` (add screener registry)
- Test: `tests/test_screener_provider.py`

**Context:**
- Follow `DataProviderInterface` pattern at `ba2_trade_platform/core/interfaces/DataProviderInterface.py`
- Follow FMP API pattern from `ba2_trade_platform/modules/dataproviders/ohlcv/FMPOHLCVProvider.py`
- FMP API key retrieved via `get_app_setting('FMP_API_KEY')`
- FMP screener endpoint: `GET https://financialmodelingprep.com/api/v3/stock-screener?apikey=KEY&priceMoreThan=0.1&priceLowerThan=5&volumeMoreThan=500000&marketCapMoreThan=10000000&marketCapLowerThan=500000000&exchange=NASDAQ,NYSE,AMEX`
- FMP screener returns JSON array of objects with: symbol, companyName, marketCap, sector, industry, beta, price, lastAnnualDividend, volume, exchange, exchangeShortName, country, isEtf, isFund, isActivelyTrading

**Step 1: Create ScreenerProviderInterface**

```python
# ba2_trade_platform/core/interfaces/ScreenerProviderInterface.py
from abc import ABC, abstractmethod
from typing import Dict, Any, List


class ScreenerProviderInterface(ABC):
    """Base interface for stock screener providers."""

    @abstractmethod
    def get_provider_name(self) -> str:
        pass

    @abstractmethod
    def screen_stocks(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Screen stocks matching filters.
        Filters: price_min, price_max, volume_min, market_cap_min, market_cap_max,
                 float_max, exchanges (List[str]), sector_exclude (List[str]), limit (int)
        Returns list of dicts with: symbol, company_name, price, volume, market_cap, sector, exchange
        """
        pass

    @abstractmethod
    def validate_config(self) -> bool:
        pass
```

**Step 2: Create FMPScreenerProvider**

Implementation that calls FMP's `/stock-screener` endpoint. Maps our filter names to FMP params (priceMoreThan, priceLowerThan, volumeMoreThan, marketCapMoreThan, marketCapLowerThan, exchange). Applies sector exclusion client-side since FMP doesn't support it. Sets `isEtf=False`, `isFund=False`, `isActivelyTrading=True`. Normalizes response to standard format.

**Step 3: Create screener `__init__.py`, register in interfaces and dataproviders**

Add `ScreenerProviderInterface` to `core/interfaces/__init__.py`. Add `SCREENER_PROVIDERS` dict and `FMPScreenerProvider` to `modules/dataproviders/__init__.py`. Update `get_provider()` and `list_providers()` registries.

**Step 4: Write tests** (mock FMP API responses, test filter mapping, sector exclusion)

**Step 5: Run tests and commit**

---

## Task 2: Structured Condition Schema and Evaluator

**Files:**
- Create: `ba2_trade_platform/modules/experts/PennyMomentumTrader/conditions.py`
- Test: `tests/test_penny_conditions.py`

**Context:**
- Conditions are JSON-serializable for storage in MarketAnalysis.state
- Uses OHLCV providers for price/volume data (via `get_ohlcv_data(symbol, interval, lookback_days)`)
- Calculates EMA, SMA, RSI, MACD locally from OHLCV DataFrames (pandas)
- Must be fast and deterministic (called every 60 seconds per symbol)
- Cache indicator data within a single evaluation cycle to avoid redundant API calls

**Step 1: Create condition types registry**

Define `CONDITION_TYPES` dict mapping condition names to required params:
- Price-based: `price_above`, `price_below`, `price_above_ema`, `price_below_ema`, `price_above_sma`, `price_below_sma`, `price_above_vwap`, `price_below_vwap`, `opening_range_breakout`
- Volume-based: `volume_above_avg`, `volume_spike`
- Indicator-based: `rsi_above`, `rsi_below`, `rsi_between`, `macd_bullish_cross`, `macd_bearish_cross`, `ema_cross_above`, `ema_cross_below`
- Percentage-based: `percent_above_entry`, `percent_below_entry`
- Time-based: `time_after`, `time_before`

Add `get_condition_types_for_llm()` that returns formatted string for LLM prompts.
Add `validate_condition()` and `validate_condition_set()` for validating LLM output.

**Step 2: Create ConditionEvaluator class**

```python
class ConditionEvaluator:
    def __init__(self, ohlcv_provider, market_timezone="US/Eastern"):
        # Takes an OHLCV provider, caches data per evaluation cycle

    def evaluate(self, conditions: dict, symbol: str, entry_price=None) -> bool:
        # Handles composite conditions: {"all": [...]}, {"any": [...]}, single

    def evaluate_single(self, condition: dict, symbol: str, entry_price=None) -> bool:
        # Dispatches to specific handler per condition type

    def get_condition_status(self, conditions: dict, symbol: str, entry_price=None) -> dict:
        # Returns per-condition met/unmet status for UI display

    def clear_cache(self):
        # Clear between evaluation cycles
```

Each condition type handler:
- `_get_current_price()`: Latest close from 1m OHLCV
- `_get_ema()`: EMA calculation from OHLCV DataFrame
- `_get_sma()`: SMA calculation
- `_get_rsi()`: RSI calculation (standard 14-period Wilder's RSI)
- `_check_macd_cross()`: MACD/Signal crossover detection (last 2 bars)
- `_check_ema_cross()`: EMA crossover detection (last 2 bars)
- `_get_vwap()`: VWAP from intraday typical price * volume
- `_check_opening_range_breakout()`: Price vs first N minutes high
- `_check_volume_spike()`: Recent volume vs average

**Step 3: Write tests** (validation, composite logic, time conditions, percent conditions, caching)

**Step 4: Run tests and commit**

---

## Task 3: LiveExpertInterface Base Class

**Files:**
- Create: `ba2_trade_platform/core/interfaces/LiveExpertInterface.py`
- Modify: `ba2_trade_platform/core/interfaces/__init__.py` (add export)
- Test: `tests/test_live_expert_interface.py`

**Context:**
- Extends `MarketExpertInterface` at `ba2_trade_platform/core/interfaces/MarketExpertInterface.py`
- Single daemon thread, stoppable via `threading.Event`
- Timezone-aware via pytz
- Auto-start only if current time < kick-off time
- Must merge its own builtin settings into MarketExpertInterface's

**Step 1: Create LiveExpertInterface**

```python
class LiveExpertInterface(MarketExpertInterface):
    def __init__(self, id: int):
        super().__init__(id)
        self._thread = None
        self._stop_event = threading.Event()
        self._manual_start_event = threading.Event()
        self._is_running = False
        self._current_phase = None
```

Key methods:
- `start()` / `stop()`: Thread lifecycle, daemon=True
- `request_manual_start()` / `request_stop()`: Callable from UI actions, returns status string
- `is_running` property, `current_phase` property
- `_run_loop()`: Main thread - check trading day, wait for kickoff or manual trigger, run pipeline, sleep until next day
- `_run_daily_pipeline()`: Abstract - subclasses implement phase orchestration
- Timezone helpers: `_get_market_tz()`, `_get_market_now()`, `_get_kickoff_time_today()`, `_get_market_close_today()`, `_is_trading_day()`, `_should_auto_start()`, `_seconds_until_kickoff()`, `_seconds_until_next_kickoff()`, `_is_market_open()`

Builtin settings (via `_get_live_expert_settings()`):
- `start_time` (str, default "07:00")
- `market_timezone` (str, default "US/Eastern", valid_values for common US/EU timezones)
- `trading_days` (json, default mon-fri all true)
- `monitoring_interval_seconds` (int, default 60)
- `market_close_time` (str, default "16:00")

Settings merge: Override `_ensure_builtin_settings()` to call `super()` then update with live expert settings.

**Step 2: Register in interfaces/__init__.py**

**Step 3: Write tests** (timezone parsing, trading day check, kickoff time calculations)

**Step 4: Commit**

---

## Task 4: PennyMomentumTrader Expert Core Class

**Files:**
- Create: `ba2_trade_platform/modules/experts/PennyMomentumTrader/__init__.py`
- Modify: `ba2_trade_platform/modules/experts/__init__.py` (register expert)

**Context:**
- Inherits from `LiveExpertInterface`
- Follow `TradingAgents` settings pattern with `"ui_editor_type": "ModelSelector"` for LLM selection
- Follow `FMPSenateTraderCopy` for `get_expert_properties()` with `can_recommend_instruments: True`, `should_expand_instrument_jobs: False`
- Use `get_expert_logger()` from `ba2_trade_platform/logger.py`
- Model selection uses `ModelFactory.create_llm()` from `ba2_trade_platform/core/ModelFactory.py`

**Step 1: Define all settings**

LLM Models (all with `"ui_editor_type": "ModelSelector"`):
- `scanning_llm` (default "OpenAI/gpt-4o-mini") - Quick filter
- `deep_analysis_llm` (default "OpenAI/gpt-4o") - Deep triage
- `websearch_llm` (default "NagaAI/gpt-4o-search-preview", `required_labels: ["websearch"]`) - Web search
- `entry_definition_llm` (default "OpenAI/gpt-4o") - Entry/exit conditions

Screening filters:
- `scan_price_min` (float, 0.10), `scan_price_max` (float, 5.00)
- `scan_volume_min` (int, 500000), `scan_relative_volume_min` (float, 2.0)
- `scan_market_cap_min` (float, 10000000), `scan_market_cap_max` (float, 500000000)
- `scan_float_max` (int, 20000000), `scan_premarket_gap_pct_min` (float, 5.0)
- `scan_sector_exclude` (str, ""), `screener_provider` (str, "FMP")

Triage/monitoring limits:
- `max_scan_candidates` (int, 50), `max_quick_filter_candidates` (int, 20), `max_final_candidates` (int, 10)
- `max_monitored_symbols` (int, 20), `max_entry_age_days` (int, 3), `max_holding_days` (int, 30)

Data vendors (type "list", multiple=True):
- `vendor_news` (default ["ai", "alpaca", "fmp", "finnhub"])
- `vendor_fundamentals` (default ["fmp", "alpha_vantage"])
- `vendor_insider` (default ["fmp"])
- `vendor_social_media` (default ["ai"])
- `vendor_ohlcv` (default ["yfinance"])

**Step 2: Implement phase orchestration**

`_run_daily_pipeline()`:
```python
def _run_daily_pipeline(self):
    # Create MarketAnalysis record
    market_analysis = self._create_market_analysis()

    # Phase 0: Review previous day's positions & monitors
    self._current_phase = "review"
    self._phase_0_review(market_analysis)
    if self._stop_event.is_set(): return

    # Check balance for new entries
    if self.has_sufficient_balance_for_entry():
        # Phase 1: Screen
        self._current_phase = "screen"
        candidates = self._phase_1_screen(market_analysis)
        if self._stop_event.is_set(): return

        # Phase 2: Quick filter
        self._current_phase = "quick_filter"
        survivors = self._phase_2_quick_filter(candidates, market_analysis)
        if self._stop_event.is_set(): return

        # Phase 3: Deep triage
        self._current_phase = "deep_triage"
        finalists = self._phase_3_deep_triage(survivors, market_analysis)
        if self._stop_event.is_set(): return
    else:
        self.logger.info("Insufficient balance - skipping scan phases, monitoring existing only")

    # Phase 4: Entry condition setup (for new + existing monitors)
    self._current_phase = "entry_setup"
    self._phase_4_entry_conditions(market_analysis)
    if self._stop_event.is_set(): return

    # Phase 5: Monitor
    self._current_phase = "monitoring"
    self._phase_5_monitor(market_analysis)

    # Phase 6: EOD
    self._current_phase = "eod"
    self._phase_6_eod(market_analysis)
    self._current_phase = "complete"
```

Each phase method should:
1. Log start
2. Do its work (call providers, LLMs, evaluator, trade manager)
3. Update `market_analysis.state` and save to DB
4. Store heavy data as `AnalysisOutput` records
5. Log completion

**Step 3: Implement `run_analysis()` and `render_market_analysis()`**

```python
def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
    self.logger.info("PennyMomentumTrader uses live pipeline, not scheduled analysis")

def render_market_analysis(self, market_analysis: MarketAnalysis) -> str:
    from .ui import PennyMomentumTraderUI
    renderer = PennyMomentumTraderUI(market_analysis)
    renderer.render()
    return ""
```

**Step 4: Implement `get_expert_actions()`**

```python
@classmethod
def get_expert_actions(cls) -> List[Dict[str, Any]]:
    return [
        {"name": "start_scan", "label": "Start Scan", "description": "Force-start the scanning pipeline", "icon": "play_arrow", "callback": "request_manual_start"},
        {"name": "stop_scan", "label": "Stop Scan", "description": "Stop scanning/monitoring", "icon": "stop", "callback": "request_stop"},
    ]
```

**Step 5: Implement `get_recommended_instruments()`**

Return symbols currently being monitored from latest MarketAnalysis state.

**Step 6: Register in experts/__init__.py**

Add `from .PennyMomentumTrader import PennyMomentumTrader` and add to `experts` list.

**Step 7: Commit**

---

## Task 5: Trade Manager

**Files:**
- Create: `ba2_trade_platform/modules/experts/PennyMomentumTrader/trade_manager.py`
- Test: `tests/test_penny_trade_manager.py`

**Context:**
- Reuses patterns from `SmartRiskManagerToolkit` at `ba2_trade_platform/core/SmartRiskManagerToolkit.py`
- Uses `get_account_instance_from_id()`, `get_expert_instance_from_id()` from `core/utils.py`
- Creates `ExpertRecommendation` records (confidence 1-100, risk_level HIGH for penny stocks)
- Creates `TradingOrder` records and places via `account.place_order()`
- Must clamp qty to available balance and `max_virtual_equity_per_instrument_percent` at execution time
- Account broker validation via `account.symbols_exist()` (see `AlpacaAccount.py:1318`)

**Step 1: Create PennyTradeManager class**

```python
class PennyTradeManager:
    def __init__(self, expert_instance_id: int):
        # Load expert, instance, account

    def calculate_position_sizes(self, candidates, available_balance) -> dict:
        # Weight by confidence, clamp to max_per_instrument
        # Returns {symbol: {qty, allocation, confidence, price}}

    def execute_entry(self, symbol, qty, confidence, catalyst, strategy, exit_conditions, market_analysis_id=None) -> Optional[int]:
        # Validate balance at execution time
        # Clamp qty
        # Create ExpertRecommendation
        # Create TradingOrder (MARKET, BUY)
        # Place via account
        # Return order_id

    def execute_exit(self, symbol, exit_pct=100.0, reason="exit condition met") -> bool:
        # Find open transactions for symbol+expert
        # Create sell orders for exit_pct of position
        # Place via account
```

**Step 2: Write tests** (position sizing weighted by confidence, clamping to max, zero-price handling)

**Step 3: Commit**

---

## Task 6: LLM Prompts

**Files:**
- Create: `ba2_trade_platform/modules/experts/PennyMomentumTrader/prompts.py`

**Context:**
- LLM calls use `ModelFactory.create_llm()` with the configured model string
- Each prompt returns a string, the caller invokes the LLM
- Must include `get_condition_types_for_llm()` output in the entry condition prompt
- Must specify JSON output format explicitly

**Step 1: Create prompt templates**

Four prompt functions:
1. `build_quick_filter_prompt(candidates: List[dict], max_survivors: int) -> str`
   - Input: ~50 candidate profiles (symbol, price, volume, market_cap, sector, gap%)
   - Output instruction: JSON array of selected symbols with brief reasoning

2. `build_deep_triage_prompt(symbol: str, news: str, insider: str, fundamentals: str, social: str) -> str`
   - Input: Full dossier for one stock
   - Output instruction: JSON with confidence (1-100), catalyst, strategy (intraday/swing), expected_profit_pct

3. `build_entry_conditions_prompt(symbol: str, analysis_summary: str, condition_types: str) -> str`
   - Input: Triage result + available condition types from registry
   - Output instruction: JSON matching condition schema (entry, stop_loss, take_profit tiers)

4. `build_exit_update_prompt(symbol: str, current_conditions: dict, new_data: str) -> str`
   - Input: Existing exit conditions + new market data/news
   - Output instruction: Updated condition JSON or "NO_CHANGE"

**Step 2: Commit**

---

## Task 7: Custom UI

**Files:**
- Create: `ba2_trade_platform/modules/experts/PennyMomentumTrader/ui.py`

**Context:**
- Follow `TradingAgentsUI` at `ba2_trade_platform/modules/experts/TradingAgentsUI.py`
- Uses NiceGUI: `ui.tabs`, `ui.tab_panels`, `ui.table`, `ui.card`, `ui.label`, `ui.expansion`, `ui.badge`
- Reads from `market_analysis.state` and queries `AnalysisOutput` records
- 5 tabs as defined in design doc

**Step 1: Create PennyMomentumTraderUI**

```python
class PennyMomentumTraderUI:
    def __init__(self, market_analysis: MarketAnalysis):
        self.market_analysis = market_analysis
        self.state = market_analysis.state or {}

    def render(self):
        # Create 5 tabs and render each

    def _render_scan_results(self):
        # Table of scan_results with color-coded rows (green=passed filter, red=rejected)

    def _render_triage(self):
        # Quick filter survivors + deep triage finalist cards with confidence bars

    def _render_monitors(self):
        # Per-symbol cards with condition status checklist, badges for status

    def _render_trades(self):
        # Table of executed trades with P&L

    def _render_raw_data(self):
        # Expandable sections with AnalysisOutput content per symbol
```

**Step 2: Commit**

---

## Task 8: Platform Integration

**Files:**
- Modify: `ba2_trade_platform/main.py` (add LiveExpert startup after InstrumentAutoAdder, ~line 118)

**Context:**
- Follow the pattern used for JobManager, WorkerQueue, SmartRiskManagerQueue, InstrumentAutoAdder
- Query all enabled ExpertInstance records, check if their expert class is a LiveExpertInterface subclass
- Call `start()` on each, store references for shutdown
- Use `get_expert_class()` from `modules/experts/__init__.py`

**Step 1: Add LiveExpert startup code**

After the InstrumentAutoAdder initialization block (~line 118), add:
```python
# Initialize and start Live Expert instances
logger.info("Initializing Live Expert instances...")
from ba2_trade_platform.core.interfaces import LiveExpertInterface
from ba2_trade_platform.modules.experts import get_expert_class

all_experts = get_all_instances(ExpertInstance)
for ei in all_experts:
    if not ei.enabled:
        continue
    ec = get_expert_class(ei.expert)
    if ec and issubclass(ec, LiveExpertInterface):
        try:
            expert = ec(ei.id)
            expert.start()
            logger.info(f"Started LiveExpert: {ei.expert} (ID: {ei.id})")
        except Exception as e:
            logger.error(f"Failed to start LiveExpert {ei.id}: {e}", exc_info=True)
```

**Step 2: Commit**

---

## Task 9: Manual Trigger from UI

**Files:**
- Verify: `ba2_trade_platform/ui/pages/settings.py` (expert actions rendering)

**Context:**
- `get_expert_actions()` is already rendered generically in the settings page
- Verify that "Start Scan" and "Stop Scan" buttons appear and call the correct callbacks
- If the existing UI doesn't handle LiveExpert actions, add the handling

**Step 1: Verify expert actions are rendered correctly**

The existing settings page should already discover and render actions from `get_expert_actions()`. Check that:
1. Buttons appear in the expert settings UI
2. Clicking them calls `request_manual_start()` / `request_stop()` on the expert instance
3. The return string from the callback is displayed to the user

**Step 2: Fix if needed, commit**

---

## Task 10: Integration Testing

**Files:**
- Create: `tests/test_penny_momentum_integration.py`

**Step 1: Write integration tests**

Test that:
1. `PennyMomentumTrader` can be instantiated with a mock expert instance
2. All settings are properly defined and accessible via `get_merged_settings_definitions()`
3. Expert properties return `can_recommend_instruments: True`
4. Expert actions return Start/Stop entries
5. `LiveExpertInterface.start()` creates a daemon thread
6. `LiveExpertInterface.stop()` cleanly terminates the thread
7. Phase pipeline handles stop_event between phases
8. Full pipeline with mocked providers/LLMs produces correct state structure

**Step 2: Run all tests, commit**

---

## Dependency Order

```
Task 1 (Screener)          ──┐
Task 2 (Conditions)        ──┤
Task 3 (LiveExpertInterface) ┼── Task 4 (Expert Core) ──┬── Task 8 (Platform Integration)
Task 5 (Trade Manager)    ──┤                           ├── Task 9 (UI Trigger)
Task 6 (Prompts)          ──┤                           └── Task 10 (Testing)
Task 7 (Custom UI)        ──┘
```

Tasks 1, 2, 3, 5, 6, 7 can be worked on in parallel.
Task 4 depends on all of them (it orchestrates everything).
Tasks 8, 9, 10 depend on Task 4.

---

## Key Files Reference

| Existing File | Why You Need It |
|--------------|-----------------|
| `core/interfaces/MarketExpertInterface.py` | Base class, builtin settings, balance methods |
| `core/interfaces/ExtendableSettingsInterface.py` | Settings save/load pattern |
| `core/interfaces/DataProviderInterface.py` | Provider interface pattern |
| `core/interfaces/__init__.py` | Register new interfaces |
| `modules/experts/__init__.py` | Register new expert |
| `modules/experts/FMPSenateTraderCopy.py` | Expert-specified instruments pattern |
| `modules/experts/TradingAgents.py` | ModelSelector settings, data vendor config |
| `modules/experts/TradingAgentsUI.py` | Custom UI rendering pattern |
| `modules/dataproviders/__init__.py` | Provider registry, get_provider() |
| `modules/dataproviders/ohlcv/FMPOHLCVProvider.py` | FMP API pattern |
| `core/SmartRiskManagerToolkit.py` | Trade execution, order creation patterns |
| `core/ModelFactory.py` | LLM creation from model strings |
| `core/JobManager.py` | Background thread, APScheduler pattern |
| `core/models.py` | MarketAnalysis, AnalysisOutput, ExpertRecommendation, TradingOrder |
| `main.py` | App startup/initialization, line ~115-118 for insertion point |
| `modules/accounts/AlpacaAccount.py:1318` | `symbols_exist()` for broker validation |
