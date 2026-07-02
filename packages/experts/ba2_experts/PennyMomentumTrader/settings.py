"""PennyMomentumTrader settings definitions.

Extracted from __init__.py (EX-4) to shrink the module. Pure data — no logic.
"""
from typing import Any, Dict


SETTINGS_DEFINITIONS: Dict[str, Any] = {
            # LLM Models
            "scanning_llm": {
                "type": "str",
                "required": True,
                "default": "OpenAI/gpt-4o-mini",
                "description": "LLM model for quick-filter scanning",
                "ui_editor_type": "ModelSelector",
                "tooltip": "Fast model used to narrow screener candidates. Runs once per scan on the full candidate list.",
            },
            "deep_analysis_llm": {
                "type": "str",
                "required": True,
                "default": "OpenAI/gpt-4o",
                "description": "LLM model for deep triage analysis",
                "ui_editor_type": "ModelSelector",
                "tooltip": "Analytical model used for in-depth evaluation of each candidate with news, fundamentals, and insider data.",
            },
            "websearch_llm": {
                "type": "str",
                "required": True,
                "default": "OpenAI/gpt5_mini",
                "description": "LLM model for web search (social sentiment, news)",
                "ui_editor_type": "ModelSelector",
                "required_labels": ["websearch"],
                "tooltip": "Model with web search capability for gathering social sentiment and live news.",
            },
            "entry_definition_llm": {
                "type": "str",
                "required": True,
                "default": "OpenAI/gpt-4o",
                "description": "LLM model for defining entry/exit conditions",
                "ui_editor_type": "ModelSelector",
                "tooltip": "Model used to generate structured entry, stop-loss, and take-profit conditions.",
            },
            "exit_update_llm": {
                "type": "str",
                "required": True,
                "default": "OpenAI/gpt-4o-mini",
                "description": "LLM model for periodic exit condition re-evaluation",
                "ui_editor_type": "ModelSelector",
                "tooltip": "Lighter model used to periodically adjust exit conditions based on fresh news. Runs every exit_update_interval_ticks monitor cycles.",
            },
            # Screening filters
            "scan_price_min": {
                "type": "float",
                "required": True,
                "default": 0.10,
                "description": "Minimum stock price for screener",
                "tooltip": "Stocks below this price are excluded from screening.",
            },
            "scan_price_max": {
                "type": "float",
                "required": True,
                "default": 5.00,
                "description": "Maximum stock price for screener",
                "tooltip": "Stocks above this price are excluded from screening.",
            },
            "scan_volume_min": {
                "type": "int",
                "required": True,
                "default": 500000,
                "description": "Minimum average volume for screener",
                "tooltip": "Stocks with lower average volume are excluded.",
            },
            "scan_market_cap_min": {
                "type": "float",
                "required": True,
                "default": 8000000,
                "description": "Minimum market cap for screener",
                "tooltip": "Stocks with market cap below this value are excluded.",
            },
            "scan_market_cap_max": {
                "type": "float",
                "required": True,
                "default": 500000000,
                "description": "Maximum market cap for screener",
                "tooltip": "Stocks with market cap above this value are excluded.",
            },
            "scan_float_max": {
                "type": "float",
                "required": False,
                "default": 500000000,
                "description": "Maximum share float for screener",
                "tooltip": "Stocks with float above this value are excluded. Lower float stocks move faster on volume. Set to 0 to disable.",
            },
            "min_relative_volume": {
                "type": "float",
                "required": True,
                "default": 1.0,
                "description": "Minimum relative volume (RVOL) to keep a candidate",
                "tooltip": "RVOL = today's volume / average volume. 1.5 means 50% above average. Candidates below this are dropped. Set to 1.0 to disable filtering.",
            },
            "include_gainers": {
                "type": "bool",
                "required": False,
                "default": True,
                "description": "Merge FMP top gainers into screener results",
                "tooltip": "Fetch today's biggest gainers from FMP and merge any matching price/mcap criteria into the candidate pool.",
            },
            "scan_sector_exclude": {
                "type": "str",
                "required": False,
                "default": "",
                "description": "Comma-separated sectors to exclude from screening",
                "tooltip": "Sectors to exclude, e.g. 'Healthcare,Energy'. Leave empty to include all.",
            },
            "screener_provider": {
                "type": "str",
                "required": True,
                "default": "fmp",
                "description": "Screener provider to use",
                "valid_values": ["fmp"],
                "tooltip": "Stock screener data source.",
            },
            "split_guard_enabled": {
                "type": "bool",
                "required": False,
                "default": True,
                "description": "Drop scan candidates with a stock split effective within ±1 day",
                "tooltip": (
                    "Reverse-split transition days produce bogus screener prints (e.g. a ~$11 "
                    "stock shown at $0.56 on its 1:20 split day), admitting mid-caps into the "
                    "penny scan and faking huge gains. When enabled, the FMP split calendar is "
                    "fetched once per scan and any symbol with a split effective within ±1 "
                    "calendar day of today is filtered out. A second price-continuity layer "
                    "drops rows whose price is >50% away from previousClose while the quoted "
                    "day-change is <20% (a data-discontinuity signature)."
                ),
            },
            # Triage/monitoring limits
            "max_scan_candidates": {
                "type": "int",
                "required": True,
                "default": 100,
                "description": "Maximum candidates from screener",
                "tooltip": "Limit the number of stocks returned by the screener.",
            },
            "max_quick_filter_candidates": {
                "type": "int",
                "required": True,
                "default": 15,
                "description": "Maximum survivors from quick filter",
                "tooltip": "How many candidates the quick-filter LLM should keep from the screener results.",
            },
            "max_final_candidates": {
                "type": "int",
                "required": True,
                "default": 15,
                "description": "Maximum finalists from deep triage",
                "tooltip": "Maximum finalists to carry forward from deep triage, selected by highest confidence score.",
            },
            "deep_triage_workers": {
                "type": "int",
                "required": True,
                "default": 3,
                "description": "Parallel workers for deep triage",
                "tooltip": "Number of symbols to deep-triage simultaneously. Each worker makes independent LLM and data API calls. Higher values reduce phase 3 duration but increase API concurrency.",
            },
            "max_monitored_symbols": {
                "type": "int",
                "required": True,
                "default": 40,
                "description": "Maximum symbols to monitor simultaneously",
                "tooltip": "Upper bound on the number of symbols being actively monitored.",
            },
            "discovery_llm": {
                "type": "str",
                "required": True,
                "default": "OpenAI/gpt5_mini",
                "description": "LLM model for discovering additional penny stocks via web search",
                "ui_editor_type": "ModelSelector",
                "required_labels": ["websearch"],
                "tooltip": "Websearch-capable model used to discover extra momentum candidates beyond the screener.",
            },
            "max_discovery_candidates": {
                "type": "int",
                "required": True,
                "default": 10,
                "description": "Number of additional stocks to discover via LLM web search",
                "tooltip": "How many extra penny stocks the discovery LLM should find each scan.",
            },
            "max_entry_age_days": {
                "type": "int",
                "required": True,
                "default": 3,
                "description": "Maximum age (days) for entry conditions before expiry",
                "tooltip": "Entry conditions older than this are removed from monitoring.",
            },
            "max_holding_days": {
                "type": "int",
                "required": True,
                "default": 14,
                "description": "Maximum days to hold a position before forced exit",
                "tooltip": "Safety net: positions held longer than this are closed automatically. Set high enough to ride multi-day trends; exit conditions handle normal exits.",
            },
            "trail_after_max_holding": {
                "type": "bool",
                "required": False,
                "default": True,
                "description": "Trail profitable positions at max_holding_days instead of flat-closing",
                "tooltip": (
                    "When a position reaches max_holding_days AT A PROFIT, do not market-close "
                    "it. Instead the stop is tightened to max(current stop, high-watermark × "
                    "(1 - trailing_stop_pct/100)) and the position keeps running, re-tightening "
                    "on every monitoring tick (ratchet-only — the stop never loosens). "
                    "Positions at a LOSS at max_holding_days are still flat-closed (time stop "
                    "for dead money). Prevents the time stop from flattening winners."
                ),
            },
            "trailing_stop_pct": {
                "type": "float",
                "required": False,
                "default": 8.0,
                "description": "Trailing stop distance (%) below the high-watermark",
                "tooltip": (
                    "Distance of the ratcheting trailing stop below the position's highest "
                    "observed price, used once trail_after_max_holding activates. "
                    "8.0 = stop trails 8% below the high-watermark."
                ),
            },
            "min_confidence_threshold": {
                "type": "int",
                "required": True,
                "default": 65,
                "description": "Minimum confidence score (1-100) for deep triage finalists",
                "tooltip": "Candidates below this confidence threshold are dropped after deep triage. Higher = more selective.",
            },
            "exit_update_interval_ticks": {
                "type": "int",
                "required": True,
                "default": 30,
                "description": "Monitor ticks between LLM exit-condition re-evaluations",
                "tooltip": "Every N monitor cycles, open positions are re-evaluated: fresh news is fetched and the LLM can tighten stops, adjust take-profit, or add new conditions. Set to 0 to disable.",
            },
            # Data vendors
            "vendor_news": {
                "type": "list",
                "required": True,
                "default": ["alpaca", "fmp", "finnhub"],
                "description": "Data vendor(s) for company news",
                "valid_values": ["alpaca", "alphavantage", "ai", "fmp", "finnhub", "google"],
                "multiple": True,
                "tooltip": "News providers used during deep triage. Multiple vendors are aggregated.",
            },
            "vendor_fundamentals": {
                "type": "list",
                "required": True,
                "default": ["fmp"],
                "description": "Data vendor(s) for company fundamentals overview",
                "valid_values": ["alpha_vantage", "ai", "fmp"],
                "multiple": True,
                "tooltip": "Fundamentals providers for deep triage analysis.",
            },
            "vendor_insider": {
                "type": "list",
                "required": True,
                "default": ["fmp"],
                "description": "Data vendor(s) for insider trading data",
                "valid_values": ["fmp"],
                "multiple": True,
                "tooltip": "Insider trading data providers.",
            },
            "vendor_social": {
                "type": "list",
                "required": True,
                "default": ["stocktwits"],
                "description": "Data vendor(s) for social sentiment",
                "valid_values": ["stocktwits", "websearch"],
                "multiple": True,
                "tooltip": (
                    "'stocktwits' fetches real-time Bullish/Bearish tags directly. "
                    "'websearch' uses the websearch_llm to search social media. "
                    "StockTwits data is also injected into the quick-filter LLM context."
                ),
            },
            "vendor_ohlcv": {
                "type": "list",
                "required": True,
                "default": ["fmp"],
                "description": "Data vendor(s) for OHLCV price data",
                "valid_values": ["fmp"],
                "multiple": True,
                "tooltip": "OHLCV data provider for condition monitoring. Restricted to FMP for extended-hours data consistency.",
            },
            "vendor_live_price": {
                "type": "str",
                "required": True,
                "default": "fmp",
                "description": "Live price quote source for monitoring",
                "valid_values": ["fmp", "account"],
                "tooltip": (
                    "Source for real-time price quotes during monitoring. "
                    "'fmp' uses FMP /quote-short endpoint (requires FMP premium for real-time). "
                    "'account' uses the broker account's price API (may be 15-min delayed on Alpaca free tier)."
                ),
            },
            # StockTwits trending discovery
            "use_stocktwits_discovery": {
                "type": "bool",
                "required": False,
                "default": True,
                "description": "Enable StockTwits trending symbol discovery (phase 1c)",
                "tooltip": (
                    "When enabled, fetches top_watched, most_active, and symbols_enhanced from "
                    "StockTwits and adds symbols priced below stocktwits_discovery_price_max to the "
                    "candidate pool. Requires stocktwits_oauth_token."
                ),
            },
            "stocktwits_discovery_price_max": {
                "type": "float",
                "required": False,
                "default": 6.0,
                "description": "Maximum price for StockTwits trending discovery",
                "tooltip": "Only StockTwits trending symbols at or below this price are added to the pipeline.",
            },
            "stocktwits_oauth_token": {
                "type": "str",
                "required": False,
                "default": "",
                "description": "StockTwits OAuth token (optional — public access works without it)",
                "tooltip": (
                    "Optional OAuth token for StockTwits API. "
                    "The trending endpoints are publicly accessible without authentication. "
                    "Providing a token gives higher rate limits."
                ),
                "ui_editor_type": "password",
            },
            "premarket_minutes": {
                "type": "int",
                "required": False,
                "default": 150,
                "description": "Minutes before market open (09:30 ET) to start the daily scan pipeline.",
                "tooltip": (
                    "How early before market open to begin screening and analysis. "
                    "Default 150 = start at 07:00 ET (2.5 hours pre-market). "
                    "Set to 0 to use the start_time setting instead."
                ),
            },
            # Entry execution
            "entry_rvol_decay_threshold": {
                "type": "float",
                "required": False,
                "default": 0.30,
                "description": "Minimum RVOL ratio (vs peak RVOL) required at entry trigger",
                "tooltip": (
                    "Guards against entering after momentum has already faded. "
                    "At entry trigger time, current RVOL must be at least this fraction of the "
                    "highest RVOL seen for that symbol during monitoring. "
                    "0.30 = current RVOL must be ≥ 30% of peak. "
                    "Set to 0.0 to disable the guard."
                ),
            },
            "entry_limit_slippage_pct": {
                "type": "float",
                "required": False,
                "default": 3.0,
                "description": "Max slippage % above current price for limit buy orders",
                "tooltip": (
                    "Entry orders are placed as limit orders at current_price × (1 + slippage_pct / 100). "
                    "Prevents filling at a severely inflated price during a fast-moving spike. "
                    "3.0 = limit 3% above the current quote. "
                    "Set to 0.0 to use market orders instead."
                ),
            },
            "max_already_moved_pct": {
                "type": "float",
                "required": False,
                "default": 15.0,
                "description": "Max % move from prev close allowed at entry trigger",
                "tooltip": (
                    "Guards against chasing stocks that have already made their move. "
                    "At entry trigger time, if the stock has already risen more than this % "
                    "from yesterday's close, the entry is skipped. "
                    "25.0 = skip if stock is up more than 25% on the day. "
                    "Set to 0.0 to disable the guard."
                ),
            },
            # EOD / retrospective
            "filter_postmortem_enabled": {
                "type": "bool",
                "required": False,
                "default": True,
                "description": "Run the automated filter post-mortem during EOD wrap-up",
                "tooltip": (
                    "Each EOD, the analysis run from ~5 trading days ago is re-examined: "
                    "5-day forward returns are computed per funnel stage (scanned, "
                    "quick-filter rejected, triage rejected, triaged, entered, expired) "
                    "against current FMP quotes, excluding split-affected symbols. Results "
                    "are persisted as a 'filter_postmortem' output on the current analysis "
                    "and missed winners (rejected/expired symbols that ran >= +25%) are "
                    "highlighted. Fail-soft: errors never break the scan pipeline."
                ),
            },
            "eod_flat": {
                "type": "bool",
                "required": False,
                "default": False,
                "description": "Close ALL open positions at EOD wrap-up (day-trade mode)",
                "tooltip": (
                    "Opt-in overnight gap control: when enabled, phase 6 EOD wrap-up "
                    "market-closes every open position of this expert, eliminating "
                    "overnight gap-through-stop risk (e.g. a -15% gap through a 7% stop). "
                    "Default off — swing positions are held overnight."
                ),
            },
}
