# Stock Screener Module Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract penny trader screening logic into a reusable `StockScreener` module, add a new `"screener"` instrument selection mode for all experts, and refactor PennyMomentumTrader to use it.

**Architecture:** New `core/StockScreener.py` provides a stateless screen-and-rank utility. MarketExpertInterface gains 14 new builtin settings for screener configuration. JobManager/WorkerQueue handle a new `"SCREENER"` expansion type. The UI Instruments tab is restructured to hold the selection method dropdown and conditionally render mode-specific content. PennyMomentumTrader's Phase 1 delegates base screening to StockScreener.

**Tech Stack:** Python, SQLModel, NiceGUI, FMP API (fmpsdk), requests

---

### Task 1: Create `core/StockScreener.py` — Base Module

**Files:**
- Create: `ba2_trade_platform/core/StockScreener.py`
- Reference: `ba2_trade_platform/modules/dataproviders/screener/FMPScreenerProvider.py` (full file)
- Reference: `ba2_trade_platform/modules/experts/PennyMomentumTrader/__init__.py:2464-2488` (`_fetch_quotes_chunked`)

**Step 1: Write StockScreener class**

Create `ba2_trade_platform/core/StockScreener.py` with:

```python
"""
Reusable Stock Screener Module

Provides a stateless screen-and-rank utility that:
1. Calls a screener provider (e.g., FMP) with basic filters
2. Optionally enriches results with RVOL via batch quotes
3. Optionally filters by price drop using OHLCV data
4. Ranks and returns top N stocks by a chosen metric
"""

import fmpsdk
from typing import Any, Dict, List, Optional

from ..logger import logger
from ..config import get_app_setting


class StockScreener:
    """Stateless stock screener that filters, enriches, and ranks stocks."""

    # Valid sort metrics for ranking
    VALID_SORT_METRICS = ["market_cap", "volume", "float_shares", "relative_volume", "composite"]

    def __init__(self, settings: Dict[str, Any]):
        """
        Initialize with a settings dict containing screener parameters.

        Expected keys (all optional, with sensible defaults):
            screener_provider: str (default "fmp")
            screener_market_cap_min: int (default 1_000_000_000)
            screener_market_cap_max: int (default 0, disabled)
            screener_volume_min: int (default 500_000)
            screener_volume_max: int (default 0, disabled)
            screener_float_min: int (default 10_000_000)
            screener_float_max: int (default 0, disabled)
            screener_price_min: float (default 20.0)
            screener_price_max: float (default 0, disabled)
            screener_relative_volume_min: float (default 1.5, 0=disabled)
            screener_price_drop_pct: float (default 15.0, 0=disabled)
            screener_price_drop_days: int (default 1)
            screener_max_stocks: int (default 10)
            screener_sort_metric: str (default "market_cap")
        """
        self.settings = settings

    def screen(self) -> List[Dict[str, Any]]:
        """
        Main entry point: screen, enrich, filter, rank, and return top N stocks.

        Returns:
            List of normalized stock dicts sorted by the chosen metric.
        """
        provider_name = self._get("screener_provider", "fmp")
        max_stocks = int(self._get("screener_max_stocks", 10))
        sort_metric = self._get("screener_sort_metric", "market_cap")

        # Step 1: Basic screening via provider
        candidates = self._run_provider_screen(provider_name)
        logger.info(f"StockScreener: provider returned {len(candidates)} candidates")

        if not candidates:
            return []

        # Step 2: RVOL enrichment (if enabled)
        rvol_min = float(self._get("screener_relative_volume_min", 0))
        if rvol_min > 0:
            candidates = self._enrich_and_filter_rvol(candidates, rvol_min)
            logger.info(f"StockScreener: {len(candidates)} candidates after RVOL filter (min {rvol_min}x)")

        # Step 3: Price drop filter (if enabled)
        drop_pct = float(self._get("screener_price_drop_pct", 0))
        drop_days = int(self._get("screener_price_drop_days", 1))
        if drop_pct > 0 and drop_days > 0:
            candidates = self._enrich_and_filter_price_drop(candidates, drop_pct, drop_days)
            logger.info(f"StockScreener: {len(candidates)} candidates after price drop filter (>{drop_pct}% in {drop_days}d)")

        if not candidates:
            return []

        # Step 4: Rank by metric and return top N
        candidates = self._rank(candidates, sort_metric)
        result = candidates[:max_stocks]
        logger.info(f"StockScreener: returning top {len(result)} stocks (sorted by {sort_metric})")
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, key: str, default: Any = None) -> Any:
        """Get a setting value, falling back to default."""
        value = self.settings.get(key)
        if value is None:
            return default
        return value

    def _run_provider_screen(self, provider_name: str) -> List[Dict[str, Any]]:
        """Run the basic screener provider with configured filters."""
        from ..modules.dataproviders import get_provider

        screener = get_provider("screener", provider_name)

        filters: Dict[str, Any] = {}

        # Map settings to provider filter keys
        mapping = {
            "screener_price_min": "price_min",
            "screener_price_max": "price_max",
            "screener_volume_min": "volume_min",
            "screener_market_cap_min": "market_cap_min",
            "screener_market_cap_max": "market_cap_max",
            "screener_float_max": "float_max",
        }

        for setting_key, filter_key in mapping.items():
            value = self._get(setting_key)
            if value is not None:
                numeric = float(value)
                if numeric > 0:
                    filters[filter_key] = numeric

        # float_min is not natively supported by FMP screener — applied client-side below
        return screener.screen_stocks(filters)

    def _enrich_and_filter_rvol(
        self, candidates: List[Dict[str, Any]], min_rvol: float
    ) -> List[Dict[str, Any]]:
        """Batch-fetch FMP quotes, calculate RVOL, and filter."""
        symbols = [c["symbol"] for c in candidates if c.get("symbol")]
        quotes = self._fetch_quotes_chunked(symbols) if symbols else {}

        for c in candidates:
            sym = (c.get("symbol") or "").upper()
            quote = quotes.get(sym, {})
            volume = quote.get("volume") or c.get("volume") or 0
            avg_vol = quote.get("avgVolume", 0) or 0
            rvol = round(volume / avg_vol, 2) if avg_vol > 0 else 0.0
            c["volume"] = volume
            c["avg_volume"] = avg_vol
            c["relative_volume"] = rvol
            # Update price and market_cap from live quote
            q_price = quote.get("price")
            if q_price and q_price > 0:
                c["price"] = q_price
            q_mcap = quote.get("marketCap")
            if q_mcap and q_mcap > 0:
                c["market_cap"] = q_mcap

        # Apply float_min filter client-side (not supported by FMP screener API)
        float_min = float(self._get("screener_float_min", 0))
        if float_min > 0:
            candidates = [
                c for c in candidates
                if (c.get("float_shares") or 0) >= float_min
            ]

        # Apply volume_max filter client-side
        volume_max = float(self._get("screener_volume_max", 0))
        if volume_max > 0:
            candidates = [
                c for c in candidates
                if (c.get("volume") or 0) <= volume_max
            ]

        return [c for c in candidates if c.get("relative_volume", 0) >= min_rvol]

    def _enrich_and_filter_price_drop(
        self, candidates: List[Dict[str, Any]], min_drop_pct: float, lookback_days: int
    ) -> List[Dict[str, Any]]:
        """Fetch OHLCV data, calculate price drop, and filter."""
        from ..modules.dataproviders import get_provider
        from datetime import datetime, timedelta

        ohlcv_provider = get_provider("ohlcv", "fmp")
        end_date = datetime.now()

        result = []
        for c in candidates:
            sym = c.get("symbol")
            if not sym:
                continue
            try:
                ohlcv = ohlcv_provider.get_ohlcv(
                    sym,
                    end_date=end_date,
                    lookback_days=lookback_days + 5,  # extra buffer for weekends/holidays
                    interval="1d",
                    format_type="dict",
                )
                data = ohlcv.get("data", []) if isinstance(ohlcv, dict) else []
                if len(data) < 2:
                    continue

                # Calculate drop from lookback_days ago to latest
                latest_close = data[-1].get("close", 0)
                ref_close = data[-min(lookback_days + 1, len(data))].get("close", 0)
                if ref_close > 0 and latest_close > 0:
                    drop_pct = ((ref_close - latest_close) / ref_close) * 100
                    c["price_drop_pct"] = round(drop_pct, 2)
                    if drop_pct >= min_drop_pct:
                        result.append(c)
                    else:
                        logger.debug(f"StockScreener: {sym} drop {drop_pct:.1f}% < min {min_drop_pct}%")
            except Exception as e:
                logger.warning(f"StockScreener: OHLCV fetch failed for {sym}: {e}")
                continue

        return result

    def _rank(
        self, candidates: List[Dict[str, Any]], metric: str
    ) -> List[Dict[str, Any]]:
        """Sort candidates by the chosen metric (descending)."""
        if metric == "composite":
            def sort_key(c):
                mcap = c.get("market_cap") or 0
                vol = c.get("volume") or 0
                flt = c.get("float_shares") or 1
                return mcap * vol * flt
        elif metric in ("market_cap", "volume", "float_shares", "relative_volume"):
            def sort_key(c):
                return c.get(metric) or 0
        else:
            logger.warning(f"StockScreener: unknown sort metric '{metric}', defaulting to market_cap")
            def sort_key(c):
                return c.get("market_cap") or 0

        candidates.sort(key=sort_key, reverse=True)
        return candidates

    @staticmethod
    def _fetch_quotes_chunked(
        symbols: List[str], chunk_size: int = 50
    ) -> Dict[str, Dict[str, Any]]:
        """Batch-fetch FMP full quotes in chunks. Returns {SYMBOL: quote_dict}."""
        api_key = get_app_setting("FMP_API_KEY")
        if not api_key:
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i : i + chunk_size]
            try:
                data = fmpsdk.quote(apikey=api_key, symbol=chunk)
                if isinstance(data, list):
                    for item in data:
                        sym = item.get("symbol", "").upper()
                        if sym:
                            result[sym] = item
            except Exception as e:
                logger.warning(f"StockScreener: FMP quote chunk {i}-{i+len(chunk)} failed: {e}")
        logger.debug(f"StockScreener: fetched quotes for {len(result)}/{len(symbols)} symbols")
        return result
```

**Step 2: Commit**

```bash
git add ba2_trade_platform/core/StockScreener.py
git commit -m "feat: add reusable StockScreener module for screen/enrich/rank pipeline"
```

---

### Task 2: Add Screener Settings to MarketExpertInterface

**Files:**
- Modify: `ba2_trade_platform/core/interfaces/MarketExpertInterface.py:96-100` (instrument_selection_method choices)
- Modify: `ba2_trade_platform/core/interfaces/MarketExpertInterface.py:236-238` (add new settings before closing brace)

**Step 1: Add "screener" to instrument_selection_method choices**

At line 99, change:
```python
"choices": ["static", "dynamic", "expert"]
```
to:
```python
"choices": ["static", "dynamic", "expert", "screener"]
```

**Step 2: Add screener settings before the closing `}` of `_builtin_settings`**

Before line 238 (the closing `}` of `_builtin_settings`), add the following settings block. These are only rendered in the UI when `instrument_selection_method == "screener"`:

```python
                # --- Screener instrument selection settings ---
                # These settings are only active when instrument_selection_method is "screener".
                # Setting a value to 0 (or None) disables that filter (whether min or max).
                "screener_provider": {
                    "type": "str", "required": False, "default": "fmp",
                    "description": "Screener data provider",
                    "valid_values": ["fmp"],
                    "tooltip": "The screener provider to use for stock discovery."
                },
                "screener_market_cap_min": {
                    "type": "int", "required": False, "default": 1000000000,
                    "description": "Min market cap (0 = disabled)",
                    "tooltip": "Minimum market capitalization. Set to 0 to disable this filter."
                },
                "screener_market_cap_max": {
                    "type": "int", "required": False, "default": 0,
                    "description": "Max market cap (0 = disabled)",
                    "tooltip": "Maximum market capitalization. Set to 0 to disable this filter."
                },
                "screener_volume_min": {
                    "type": "int", "required": False, "default": 500000,
                    "description": "Min avg volume (0 = disabled)",
                    "tooltip": "Minimum average daily volume. Set to 0 to disable this filter."
                },
                "screener_volume_max": {
                    "type": "int", "required": False, "default": 0,
                    "description": "Max avg volume (0 = disabled)",
                    "tooltip": "Maximum average daily volume. Set to 0 to disable this filter."
                },
                "screener_float_min": {
                    "type": "int", "required": False, "default": 10000000,
                    "description": "Min share float (0 = disabled)",
                    "tooltip": "Minimum share float (shares available for trading). Set to 0 to disable this filter."
                },
                "screener_float_max": {
                    "type": "int", "required": False, "default": 0,
                    "description": "Max share float (0 = disabled)",
                    "tooltip": "Maximum share float. Set to 0 to disable this filter."
                },
                "screener_price_min": {
                    "type": "float", "required": False, "default": 20.0,
                    "description": "Min price (0 = disabled)",
                    "tooltip": "Minimum stock price. Set to 0 to disable this filter."
                },
                "screener_price_max": {
                    "type": "float", "required": False, "default": 0,
                    "description": "Max price (0 = disabled)",
                    "tooltip": "Maximum stock price. Set to 0 to disable this filter."
                },
                "screener_relative_volume_min": {
                    "type": "float", "required": False, "default": 1.5,
                    "description": "Min relative volume (0 = disabled)",
                    "tooltip": "Minimum relative volume (today's volume / avg volume). 1.5 means 50% above average. Set to 0 to disable. Enabling triggers extra API calls to fetch live quotes."
                },
                "screener_price_drop_pct": {
                    "type": "float", "required": False, "default": 15.0,
                    "description": "Min price drop % (0 = disabled)",
                    "tooltip": "Minimum price drop percentage over the lookback period. Set to 0 to disable. Enabling triggers extra API calls to fetch OHLCV data."
                },
                "screener_price_drop_days": {
                    "type": "int", "required": False, "default": 1,
                    "description": "Price drop lookback days",
                    "tooltip": "Number of trading days to look back for the price drop calculation."
                },
                "screener_max_stocks": {
                    "type": "int", "required": False, "default": 10,
                    "description": "Max stocks to select",
                    "tooltip": "Maximum number of stocks to return from the screener after ranking."
                },
                "screener_sort_metric": {
                    "type": "str", "required": False, "default": "market_cap",
                    "description": "Ranking metric for stock selection",
                    "valid_values": ["market_cap", "volume", "float_shares", "relative_volume", "composite"],
                    "tooltip": "How to rank and select stocks when more match than the limit. 'composite' uses market_cap * volume * float_shares."
                },
```

**Step 3: Update `get_enabled_instruments()` to handle "screener"**

At line 317-318, the existing `elif instrument_selection_method == 'dynamic':` block. Add a new elif before it:

```python
            elif instrument_selection_method == 'screener':
                # Screener-based selection - return SCREENER symbol
                return ["SCREENER"]
```

**Step 4: Commit**

```bash
git add ba2_trade_platform/core/interfaces/MarketExpertInterface.py
git commit -m "feat: add screener instrument selection mode with 14 configurable settings"
```

---

### Task 3: Update JobManager for SCREENER Expansion

**Files:**
- Modify: `ba2_trade_platform/core/JobManager.py:219-258` (`submit_market_analysis` — add SCREENER to special symbols)
- Modify: `ba2_trade_platform/core/JobManager.py:669-719` (`_get_enabled_instruments` — handle screener mode)
- Modify: `ba2_trade_platform/core/JobManager.py:826` (`_execute_scheduled_analysis` — add SCREENER to special symbols)
- Add: new `_execute_screener_analysis()` method after `_execute_open_positions_analysis` (~line 1135)

**Step 1: Add "SCREENER" to special symbol handling in `submit_market_analysis()`**

At line 241, change:
```python
if symbol in ["DYNAMIC", "EXPERT", "OPEN_POSITIONS"]:
```
to:
```python
if symbol in ["DYNAMIC", "EXPERT", "OPEN_POSITIONS", "SCREENER"]:
```

**Step 2: Add screener handling in `_get_enabled_instruments()`**

After line 701 (the `elif instrument_selection_method == 'dynamic':` return), add:
```python
            elif instrument_selection_method == 'screener':
                # Screener-based selection - create job with SCREENER symbol
                # At execution time, JobManager will run the StockScreener and expand into individual jobs
                logger.info(f"Expert {instance_id} uses screener instrument selection - creating SCREENER job")
                return ["SCREENER"]
```

**Step 3: Add "SCREENER" to special symbol handling in `_execute_scheduled_analysis()`**

At line 826, change:
```python
if symbol in ["DYNAMIC", "EXPERT", "OPEN_POSITIONS"]:
```
to:
```python
if symbol in ["DYNAMIC", "EXPERT", "OPEN_POSITIONS", "SCREENER"]:
```

**Step 4: Add `_execute_screener_analysis()` method**

Add after `_execute_open_positions_analysis()` (after line ~1135):

```python
    def _execute_screener_analysis(self, expert_instance_id: int, subtype: str, batch_id: Optional[str] = None):
        """Execute screener-based instrument selection and analysis."""
        try:
            logger.info(f"Executing screener analysis for expert {expert_instance_id}, batch_id={batch_id}")

            from .utils import get_expert_instance_from_id
            expert = get_expert_instance_from_id(expert_instance_id)
            if not expert:
                logger.error(f"Expert instance {expert_instance_id} not found for screener analysis")
                return

            # Run the stock screener with expert's settings
            from .StockScreener import StockScreener
            screener = StockScreener(expert.settings)
            selected_stocks = screener.screen()

            if not selected_stocks:
                logger.warning(f"Screener returned no stocks for expert {expert_instance_id}")
                return

            selected_instruments = [s["symbol"] for s in selected_stocks if s.get("symbol")]

            # Filter out symbols not supported by the broker
            from .db import get_instance
            from .models import ExpertInstance
            from .utils import get_account_instance_from_id
            expert_instance = get_instance(ExpertInstance, expert_instance_id)
            if not expert_instance:
                logger.error(f"Expert instance {expert_instance_id} not found in database")
                return

            account = get_account_instance_from_id(expert_instance.account_id)
            if account:
                selected_instruments = account.filter_supported_symbols(
                    selected_instruments,
                    log_prefix=f"StockScreener-{expert_instance_id}"
                )
                if not selected_instruments:
                    logger.warning(f"No supported symbols remain after broker filter for expert {expert_instance_id}")
                    return

            logger.info(f"Screener selected {len(selected_instruments)} instruments for expert {expert_instance_id}: {selected_instruments[:10]}{'...' if len(selected_instruments) > 10 else ''}")

            # Auto-add instruments to database
            try:
                from .InstrumentAutoAdder import get_instrument_auto_adder
                auto_adder = get_instrument_auto_adder()
                auto_adder.queue_instruments_for_addition(
                    symbols=selected_instruments,
                    expert_shortname=expert.shortname,
                    source='screener'
                )
            except Exception as e:
                logger.warning(f"Could not queue instruments for auto-addition: {e}")

            # Submit analysis jobs for selected instruments
            for instrument in selected_instruments:
                try:
                    if subtype == AnalysisUseCase.OPEN_POSITIONS:
                        if not self._has_open_transactions_for_symbol(expert_instance_id, instrument):
                            logger.debug(f"Skipping OPEN_POSITIONS analysis for expert {expert_instance_id}, symbol {instrument}: no open transactions")
                            continue

                    task_id = self.submit_market_analysis(
                        expert_instance_id=expert_instance_id,
                        symbol=instrument,
                        subtype=subtype,
                        priority=0,
                        batch_id=batch_id
                    )
                    logger.debug(f"Submitted screener analysis for {instrument}: {task_id}")

                except Exception as e:
                    logger.error(f"Error submitting screener analysis for {instrument}: {e}")

        except Exception as e:
            logger.error(f"Error in screener analysis for expert {expert_instance_id}: {e}", exc_info=True)
```

**Step 5: Commit**

```bash
git add ba2_trade_platform/core/JobManager.py
git commit -m "feat: add SCREENER expansion type to JobManager"
```

---

### Task 4: Update WorkerQueue for SCREENER Expansion

**Files:**
- Modify: `ba2_trade_platform/core/WorkerQueue.py:80` (docstring)
- Modify: `ba2_trade_platform/core/WorkerQueue.py:1344-1355` (add SCREENER case)

**Step 1: Add SCREENER expansion type**

At line ~1351, after the `elif task.expansion_type == "OPEN_POSITIONS":` block, add:
```python
            elif task.expansion_type == "SCREENER":
                job_manager._execute_screener_analysis(task.expert_instance_id, task.subtype, batch_id=task.batch_id)
                logger.info(f"Screener analysis expansion completed for expert {task.expert_instance_id}")
```

**Step 2: Update docstrings**

Update the `InstrumentExpansionTask` docstring at line 80 to include SCREENER:
```python
"""Represents an instrument expansion task (DYNAMIC/EXPERT/OPEN_POSITIONS/SCREENER) to be executed by a worker."""
```

And the `expansion_type` field at line 83:
```python
expansion_type: str  # "DYNAMIC", "EXPERT", "OPEN_POSITIONS", or "SCREENER"
```

**Step 3: Commit**

```bash
git add ba2_trade_platform/core/WorkerQueue.py
git commit -m "feat: add SCREENER to WorkerQueue expansion task handling"
```

---

### Task 5: Update UI — Restructure Instruments Tab

**Files:**
- Modify: `ba2_trade_platform/ui/pages/settings.py`
  - Lines 1573-1580: Remove instrument_selection_method from General tab
  - Lines 1832-1839: Restructure Instruments tab
  - Lines 2647-2730: Update `_render_instrument_content()`
  - Lines 3467-3484: Update `_save_expert()` for screener mode
  - Lines 3651-3693: Update `_save_instrument_configuration()` for screener mode

**Step 1: Remove instrument_selection_method from General tab**

At lines 1573-1580, remove the `self.instrument_selection_method_select` select widget from the General tab. The entire block:
```python
                                self.instrument_selection_method_select = ui.select(
                                    options=["static", "dynamic", "expert"],
                                    label='Instrument Selection Method',
                                    value="static",
                                    on_change=self._on_instrument_selection_method_change
                                ).classes('flex-1').props('dense').tooltip(
                                    "Static (manual), Dynamic (AI prompt), Expert (expert-driven)"
                                )
```
Remove this entirely from the General tab row.

**Step 2: Add instrument_selection_method to Instruments tab**

At lines 1832-1839, restructure the Instruments tab. Replace:
```python
                    with ui.tab_panel('Instruments'):
                        ui.label('Select and configure instruments for this expert:').classes('text-subtitle1 mb-4')

                        # Container for dynamic instrument UI content
                        self.instruments_content_container = ui.column().classes('w-full')

                        # Initialize with static content
                        self._render_instrument_content(expert_instance, is_edit)
```

With:
```python
                    with ui.tab_panel('Instruments'):
                        # Instrument selection method dropdown at top of tab
                        self.instrument_selection_method_select = ui.select(
                            options=["static", "dynamic", "expert", "screener"],
                            label='Instrument Selection Method',
                            value="static",
                            on_change=self._on_instrument_selection_method_change
                        ).classes('w-full mb-4').props('dense').tooltip(
                            "Static (manual), Dynamic (AI prompt), Expert (expert-driven), Screener (automated stock screener)"
                        )

                        # Container for dynamic instrument UI content
                        self.instruments_content_container = ui.column().classes('w-full')

                        # Initialize with static content
                        self._render_instrument_content(expert_instance, is_edit)
```

**Step 3: Add screener rendering to `_render_instrument_content()`**

At lines 2647-2730, add a new `elif` for screener mode. After the `elif selection_method == 'dynamic':` block (before `else: # static`), add:

```python
            elif selection_method == 'screener':
                # Screener-based selection - show screener settings
                with ui.card().classes('w-full p-4 alert-banner info'):
                    with ui.row():
                        ui.icon('filter_list').classes('text-[#4dabf7] text-xl mr-3')
                        with ui.column():
                            ui.label('Stock Screener Instrument Selection').classes('text-lg font-semibold text-[#4dabf7]')
                            ui.label('Stocks are automatically selected using configurable screening criteria. Set a value to 0 (or None) to disable that filter.').classes('text-secondary-custom')

                self._render_screener_settings(expert_instance)
                self.instrument_selector = None
```

**Step 4: Add `_render_screener_settings()` method**

Add a new method after `_render_instrument_content()`:

```python
    def _render_screener_settings(self, expert_instance):
        """Render screener-specific settings in a compact 2-column grid."""
        from ...core.interfaces.MarketExpertInterface import MarketExpertInterface
        MarketExpertInterface._ensure_builtin_settings()
        builtin = MarketExpertInterface._builtin_settings

        # Load current values if editing
        current_settings = {}
        if expert_instance:
            expert = get_expert_instance_from_id(expert_instance.id)
            if expert:
                current_settings = expert.settings

        self.screener_settings_inputs = {}

        # Screener setting keys in display order
        screener_keys = [
            "screener_provider",
            "screener_market_cap_min", "screener_market_cap_max",
            "screener_volume_min", "screener_volume_max",
            "screener_float_min", "screener_float_max",
            "screener_price_min", "screener_price_max",
            "screener_relative_volume_min",
            "screener_price_drop_pct", "screener_price_drop_days",
            "screener_max_stocks", "screener_sort_metric",
        ]

        with self.instruments_content_container:
            with ui.column().classes('w-full mt-4'):
                for key in screener_keys:
                    meta = builtin.get(key)
                    if not meta:
                        continue

                    label = meta.get("description", key)
                    current_value = current_settings.get(key)
                    default_value = meta.get("default")
                    valid_values = meta.get("valid_values")
                    tooltip_text = meta.get("tooltip")

                    # 2-column grid (same as expert settings)
                    setting_container = ui.element('div').classes('w-full mb-2').style(
                        'display: grid; grid-template-columns: 60% 40%; align-items: center; gap: 8px'
                    )
                    with setting_container:
                        # Label with tooltip
                        if tooltip_text:
                            with ui.row().classes('items-center gap-1'):
                                ui.label(label).classes('text-xs')
                                with ui.icon('help_outline', size='xs').classes('text-gray-500 cursor-help'):
                                    ui.tooltip(tooltip_text).style('font-size: 14px; max-width: 400px; line-height: 1.4')
                        else:
                            ui.label(label).classes('text-xs')

                        # Input field
                        if meta["type"] == "str" and valid_values:
                            value = current_value if current_value is not None else default_value or ""
                            inp = ui.select(
                                options=valid_values,
                                label='',
                                value=value if value in valid_values else valid_values[0]
                            ).classes('w-full').props('dense')
                        elif meta["type"] == "int":
                            value = current_value if current_value is not None else default_value or 0
                            try:
                                value = int(value)
                            except (ValueError, TypeError):
                                value = default_value or 0
                            inp = ui.input(label='', value=str(value)).classes('w-full').props('dense')
                        elif meta["type"] == "float":
                            value = current_value if current_value is not None else default_value or 0.0
                            inp = ui.input(label='', value=str(value)).classes('w-full').props('dense')
                        else:
                            value = current_value if current_value is not None else default_value or ""
                            inp = ui.input(label='', value=str(value)).classes('w-full').props('dense')

                    self.screener_settings_inputs[key] = inp

                # Test Screener button
                with ui.row().classes('w-full justify-end mt-4'):
                    self.test_screener_button = ui.button(
                        'Test Screener',
                        on_click=self._test_screener,
                        icon='filter_list'
                    ).props('color=positive')
```

**Step 5: Add `_test_screener()` method**

Add after `_render_screener_settings()`:

```python
    async def _test_screener(self):
        """Test stock screener with current (unsaved) form values."""
        try:
            if hasattr(self, 'test_screener_button'):
                self.test_screener_button.props('loading disable')

            # Build settings dict from current form values
            settings = {}
            if hasattr(self, 'screener_settings_inputs'):
                from ...core.interfaces.MarketExpertInterface import MarketExpertInterface
                MarketExpertInterface._ensure_builtin_settings()
                builtin = MarketExpertInterface._builtin_settings

                for key, inp in self.screener_settings_inputs.items():
                    meta = builtin.get(key, {})
                    if meta.get("type") == "int":
                        try:
                            settings[key] = int(inp.value) if inp.value else 0
                        except (ValueError, TypeError):
                            settings[key] = 0
                    elif meta.get("type") == "float":
                        try:
                            settings[key] = float(inp.value) if inp.value else 0.0
                        except (ValueError, TypeError):
                            settings[key] = 0.0
                    else:
                        settings[key] = inp.value

            from ...core.StockScreener import StockScreener
            screener = StockScreener(settings)

            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, screener.screen)

            if hasattr(self, 'test_screener_button'):
                self.test_screener_button.props(remove='loading disable')

            if result:
                with ui.dialog() as result_dialog, ui.card().classes('w-full').style('max-width: 900px'):
                    ui.label(f'Screener Results ({len(result)} stocks)').classes('text-lg font-semibold mb-4')

                    columns = [
                        {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left'},
                        {'name': 'company_name', 'label': 'Company', 'field': 'company_name', 'align': 'left'},
                        {'name': 'price', 'label': 'Price', 'field': 'price', 'align': 'right'},
                        {'name': 'market_cap', 'label': 'Market Cap', 'field': 'market_cap_fmt', 'align': 'right'},
                        {'name': 'volume', 'label': 'Volume', 'field': 'volume_fmt', 'align': 'right'},
                        {'name': 'float_shares', 'label': 'Float', 'field': 'float_fmt', 'align': 'right'},
                        {'name': 'relative_volume', 'label': 'RVOL', 'field': 'rvol_fmt', 'align': 'right'},
                        {'name': 'price_drop_pct', 'label': 'Drop %', 'field': 'drop_fmt', 'align': 'right'},
                    ]

                    def fmt_number(n, suffix=''):
                        if n is None:
                            return '-'
                        if abs(n) >= 1e9:
                            return f'{n/1e9:.1f}B{suffix}'
                        if abs(n) >= 1e6:
                            return f'{n/1e6:.1f}M{suffix}'
                        if abs(n) >= 1e3:
                            return f'{n/1e3:.0f}K{suffix}'
                        return f'{n:.0f}{suffix}'

                    rows = []
                    for s in result:
                        rows.append({
                            'symbol': s.get('symbol', ''),
                            'company_name': (s.get('company_name') or '')[:30],
                            'price': f"${s.get('price', 0):.2f}" if s.get('price') else '-',
                            'market_cap_fmt': fmt_number(s.get('market_cap')),
                            'volume_fmt': fmt_number(s.get('volume')),
                            'float_fmt': fmt_number(s.get('float_shares')),
                            'rvol_fmt': f"{s.get('relative_volume', 0):.1f}x" if s.get('relative_volume') else '-',
                            'drop_fmt': f"{s.get('price_drop_pct', 0):.1f}%" if s.get('price_drop_pct') else '-',
                        })

                    ui.table(columns=columns, rows=rows).classes('w-full').props('dense')

                    with ui.row().classes('w-full justify-end mt-4'):
                        ui.button('Close', on_click=result_dialog.close)
                result_dialog.open()
            else:
                ui.notify('Screener returned no results. Try adjusting the filters.', type='warning')

        except Exception as e:
            if hasattr(self, 'test_screener_button'):
                self.test_screener_button.props(remove='loading disable')
            logger.error(f'Error testing screener: {e}', exc_info=True)
            ui.notify(f'Error testing screener: {str(e)}', type='negative')
```

**Step 6: Update `_save_expert()` to handle screener mode**

At lines 3467-3484 in `_save_expert()`, add a screener case to the `has_instruments` check:

After line 3484 (after the `elif selection_method == 'expert':` block), add:
```python
            elif selection_method == 'screener':
                # Screener mode - always considered configured
                has_instruments = True
```

**Step 7: Update `_save_instrument_configuration()` to save screener settings**

At lines 3651-3693, add screener handling in `_save_instrument_configuration()`. After the `elif selection_method == 'expert':` return block (line 3675), add:

```python
        elif selection_method == 'screener':
            # Save screener settings
            if hasattr(self, 'screener_settings_inputs'):
                from ...core.interfaces.MarketExpertInterface import MarketExpertInterface
                MarketExpertInterface._ensure_builtin_settings()
                builtin = MarketExpertInterface._builtin_settings

                for key, inp in self.screener_settings_inputs.items():
                    meta = builtin.get(key, {})
                    if meta.get("type") == "int":
                        try:
                            value = int(inp.value) if inp.value and str(inp.value).strip() != "" else 0
                        except (ValueError, TypeError):
                            value = 0
                        expert.save_setting(key, value, setting_type="int")
                    elif meta.get("type") == "float":
                        try:
                            value = float(inp.value) if inp.value and str(inp.value).strip() != "" else 0.0
                        except (ValueError, TypeError):
                            value = 0.0
                        expert.save_setting(key, value, setting_type="float")
                    else:
                        expert.save_setting(key, inp.value or "", setting_type="str")
                logger.debug(f'Saved screener settings for expert {expert_id}')
            return
```

**Step 8: Commit**

```bash
git add ba2_trade_platform/ui/pages/settings.py
git commit -m "feat: restructure Instruments tab with screener mode, test button, and dynamic rendering"
```

---

### Task 6: Refactor PennyMomentumTrader Phase 1

**Files:**
- Modify: `ba2_trade_platform/modules/experts/PennyMomentumTrader/__init__.py`
  - Lines 777-1054: `_phase_1_screen()` — delegate base screening to StockScreener
  - Lines 2464-2488: `_fetch_quotes_chunked()` — can be removed (now in StockScreener)

**Step 1: Refactor `_phase_1_screen()` to use StockScreener**

Replace the initial screener call + RVOL enrichment logic (roughly lines 777-970) with a StockScreener call. Keep the penny-trader-specific layers (gainers merge, StockTwits, LLM discovery, tradability filter) on top.

The key changes inside `_phase_1_screen()`:

1. Build a settings dict from penny trader settings mapping to screener settings:
```python
        # Map penny trader settings to StockScreener settings
        screener_settings = {
            "screener_provider": screener_name,
            "screener_price_min": price_min,
            "screener_price_max": price_max,
            "screener_market_cap_min": mcap_min,
            "screener_market_cap_max": mcap_max,
            "screener_float_max": float_max if float_max and float(float_max) > 0 else 0,
            "screener_volume_min": self.get_setting_with_interface_default("scan_volume_min", log_warning=False),
            "screener_relative_volume_min": min_rvol,
            "screener_max_stocks": max_candidates,
            "screener_sort_metric": "relative_volume",  # Penny trader always sorts by RVOL
        }
```

2. Call `StockScreener(screener_settings).screen()` for the base screening.

3. Keep the gainers merge, open position exclusion, StockTwits discovery, LLM discovery, and tradability filter exactly as they are.

4. The `_fetch_quotes_chunked()` method on PennyMomentumTrader can be replaced with calls to `StockScreener._fetch_quotes_chunked()` (it's a static method). However, since other parts of the penny trader may still use it, keep the method but make it delegate:

```python
    def _fetch_quotes_chunked(self, symbols, chunk_size=50):
        """Delegate to StockScreener's static method."""
        from ....core.StockScreener import StockScreener
        return StockScreener._fetch_quotes_chunked(symbols, chunk_size)
```

**Important:** The penny trader currently does RVOL enrichment on ALL candidates (including gainers) and sorts by RVOL. With StockScreener, the base screening already handles RVOL. The gainers that are merged in after the screener call will still need RVOL enrichment separately — this should remain in the penny trader code since it's specific to the gainers merge logic.

**Step 2: Commit**

```bash
git add ba2_trade_platform/modules/experts/PennyMomentumTrader/__init__.py
git commit -m "refactor: delegate PennyMomentumTrader Phase 1 base screening to StockScreener"
```

---

### Task 7: Manual Testing & Verification

**Step 1: Verify the application starts without errors**

```bash
.venv/bin/python main.py
```

Open http://localhost:8080/settings and:
1. Open an expert dialog
2. Go to Instruments tab — verify the selection method dropdown is there (not in General tab)
3. Switch to "screener" — verify all 14 settings appear in compact grid
4. Click "Test Screener" — verify it returns results in a table dialog
5. Switch to "static" — verify instrument selector table appears
6. Switch to "dynamic" — verify AI prompt textarea appears
7. Switch to "expert" — verify info banner appears
8. Save expert with screener mode — verify settings persist on reopen

**Step 2: Verify scheduled job creation**

After saving an expert with screener mode, check logs for:
```
Expert X uses screener instrument selection - creating SCREENER job
```

**Step 3: Commit final state**

```bash
git add -A
git commit -m "feat: complete stock screener module with screener instrument selection mode"
```
