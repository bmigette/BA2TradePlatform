"""
PennyMomentumTrader Expert

Live trading expert that scans for penny stock momentum candidates,
performs multi-stage LLM-powered triage, defines structured entry/exit
conditions, and monitors positions in real time.

Pipeline phases:
  0. Review existing positions
  1. Screen stocks via screener provider
  2. Quick-filter candidates via fast LLM
  3. Deep triage via analytical LLM with news/fundamentals/insider data
  4. Generate structured entry/exit conditions via LLM
  5. Monitor conditions and execute trades
  6. EOD wrap-up
"""

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ....core.interfaces import LiveExpertInterface
from ....core.models import (
    AnalysisOutput,
    ExpertInstance,
    ExpertRecommendation,
    MarketAnalysis,
)
from ....core.db import add_instance, get_db, get_instance
from ....core.types import MarketAnalysisStatus, OrderRecommendation, RiskLevel, TimeHorizon
from ....core.ModelFactory import ModelFactory
from ....logger import get_expert_logger

from .conditions import ConditionEvaluator, get_condition_types_for_llm, validate_condition_set
from .prompts import (
    build_deep_triage_prompt,
    build_entry_conditions_prompt,
    build_exit_update_prompt,
    build_quick_filter_prompt,
)
from .trade_manager import PennyTradeManager


class PennyMomentumTrader(LiveExpertInterface):
    """
    Live penny-stock momentum trading expert.

    Runs a daily pipeline that screens, triages, and monitors penny stocks
    for momentum trades using LLM-powered analysis and structured conditions.
    """

    @classmethod
    def description(cls) -> str:
        return "AI-powered penny stock momentum scanner with structured entry/exit conditions"

    @classmethod
    def get_expert_properties(cls) -> Dict[str, Any]:
        return {
            "can_recommend_instruments": True,
            "should_expand_instrument_jobs": False,
        }

    @classmethod
    def get_expert_actions(cls) -> List[Dict[str, Any]]:
        return [
            {
                "name": "start_scan",
                "label": "Start Scan",
                "description": "Force-start the scanning pipeline",
                "icon": "play_arrow",
                "callback": "request_manual_start",
            },
            {
                "name": "stop_scan",
                "label": "Stop Scan",
                "description": "Stop scanning/monitoring",
                "icon": "stop",
                "callback": "request_stop",
            },
            {
                "name": "evaluate_conditions",
                "label": "Evaluate Conditions",
                "description": "Fetch live prices and evaluate entry conditions for all watched symbols now",
                "icon": "rule",
                "callback": "evaluate_conditions_now",
            },
        ]

    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
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
                "default": 10000000,
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
            # Triage/monitoring limits
            "max_scan_candidates": {
                "type": "int",
                "required": True,
                "default": 50,
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
            "min_confidence_threshold": {
                "type": "int",
                "required": True,
                "default": 55,
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
        }

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, id: int):
        super().__init__(id)
        self.instance = get_instance(ExpertInstance, id)
        if not self.instance:
            raise ValueError(f"ExpertInstance with ID {id} not found")
        self.logger = get_expert_logger("PennyMomentumTrader", id)

    # ------------------------------------------------------------------
    # MarketExpertInterface overrides
    # ------------------------------------------------------------------

    def run_analysis(self, symbol, market_analysis):
        self.logger.info(
            "PennyMomentumTrader uses live pipeline, not scheduled analysis - skipping"
        )
        # Mark the analysis as completed so it doesn't stay stuck in "Running"
        with get_db() as session:
            ma = session.get(MarketAnalysis, market_analysis.id)
            if ma:
                ma.status = MarketAnalysisStatus.COMPLETED
                ma.state = {"phase": "skipped", "reason": "PennyMomentumTrader uses live pipeline"}
                session.add(ma)
                session.commit()

    def render_market_analysis(self, market_analysis):
        from .ui import PennyMomentumTraderUI

        renderer = PennyMomentumTraderUI(market_analysis)
        renderer.render()
        return ""

    def get_recommended_instruments(self) -> Optional[List[str]]:
        """Return symbols currently being monitored from the latest MarketAnalysis state."""
        try:
            with get_db() as session:
                from sqlmodel import select

                statement = (
                    select(MarketAnalysis)
                    .where(MarketAnalysis.expert_instance_id == self.instance.id)
                    .order_by(MarketAnalysis.created_at.desc())  # type: ignore[union-attr]
                    .limit(1)
                )
                ma = session.exec(statement).first()
                if ma and ma.state:
                    monitored = ma.state.get("monitored_symbols", {})
                    return list(monitored.keys()) if monitored else None
        except Exception as e:
            self.logger.warning(f"Failed to get recommended instruments: {e}")
        return None

    # ------------------------------------------------------------------
    # Daily pipeline (LiveExpertInterface abstract method)
    # ------------------------------------------------------------------

    def _time_phase(self, phase_name: str, market_analysis: MarketAnalysis, func, *args, **kwargs):
        """Run a phase function and record its elapsed time in state."""
        start = time.time()
        result = func(*args, **kwargs)
        elapsed = round(time.time() - start, 1)
        self.logger.info(f"{phase_name} completed in {elapsed}s")
        # Merge timing into state
        with get_db() as session:
            ma = session.get(MarketAnalysis, market_analysis.id)
            if ma:
                from sqlalchemy.orm import attributes
                state = ma.state or {}
                timings = state.get("phase_timings", {})
                timings[phase_name] = elapsed
                state["phase_timings"] = timings
                ma.state = state
                attributes.flag_modified(ma, "state")
                session.add(ma)
                session.commit()
                market_analysis.state = state
        return result

    def _run_daily_pipeline(self):
        is_resume = getattr(self, "_resume_mode", False)
        mode_label = "RESUME (mid-day restart)" if is_resume else "FULL"
        self.logger.info(f"=== Daily pipeline starting [{mode_label}] ===")
        self._trade_mgr = PennyTradeManager(self.instance.id)
        market_analysis = self._create_market_analysis()
        self.logger.debug(f"Created MarketAnalysis id={market_analysis.id}")
        pipeline_start = time.time()

        # Phase 0: Review existing positions
        self._current_phase = "review"
        self._update_state(market_analysis, {"phase": "review"})
        self._time_phase("review", market_analysis, self._phase_0_review, market_analysis)
        if self._stop_event.is_set():
            self.logger.info("Pipeline aborted after phase 0 (stop requested)")
            return

        # In resume mode, skip scan only if we're already at the monitored-symbols cap
        max_monitored = int(self.get_setting_with_interface_default(
            "max_monitored_symbols", log_warning=False
        ))
        prev_monitored = self._get_previous_monitored_data(market_analysis.id)
        prev_watching = sum(
            1 for info in prev_monitored.values() if info.get("status") == "watching"
        )
        scan_done_today = is_resume and self._full_scan_completed_today(market_analysis.id)
        at_capacity = is_resume and (prev_watching >= max_monitored or scan_done_today)

        if at_capacity:
            reason = (
                f"scan already ran today"
                if scan_done_today
                else f"already at monitor capacity ({prev_watching}/{max_monitored})"
            )
            self.logger.info(
                f"Resume mode: {reason} — "
                "skipping scan phases, phase 4 will carry over monitored symbols"
            )
            finalists = []
        # Check balance for new entries
        elif self.has_sufficient_balance_for_entry():
            # Phase 1: Screen — no LLM, returns top-N by volume
            self._current_phase = "screen"
            self._update_state(market_analysis, {"phase": "screen"})
            screener_candidates = self._time_phase(
                "screen", market_analysis, self._phase_1_screen, market_analysis
            )
            self.logger.debug(f"Phase 1 complete: {len(screener_candidates)} tradeable candidates")
            if self._stop_event.is_set():
                self.logger.info("Pipeline aborted after phase 1 (stop requested)")
                return

            # Phase 2: Quick filter via LLM — narrows screener results only
            self._current_phase = "quick_filter"
            self._update_state(market_analysis, {"phase": "quick_filter"})
            survivors = self._time_phase(
                "quick_filter", market_analysis, self._phase_2_quick_filter, screener_candidates, market_analysis
            )
            self.logger.debug(f"Phase 2 complete: {len(survivors)} survivors")
            if self._stop_event.is_set():
                self.logger.info("Pipeline aborted after phase 2 (stop requested)")
                return

            # Phase 1b: LLM discovery — finds additional symbols NOT in screener results
            self._current_phase = "discovery"
            self._update_state(market_analysis, {"phase": "discovery"})
            discovered = self._time_phase(
                "discovery", market_analysis, self._phase_1b_llm_discovery, screener_candidates, market_analysis
            )
            if discovered:
                survivor_symbols = {c.get("symbol") for c in survivors}
                new_ones = [d for d in discovered if d.get("symbol") not in survivor_symbols]
                self.logger.info(
                    f"Phase 1b added {len(new_ones)} LLM-discovered candidates "
                    f"(survivors: {len(survivors)}, total for deep triage: {len(survivors) + len(new_ones)})"
                )
            else:
                new_ones = []
            if self._stop_event.is_set():
                self.logger.info("Pipeline aborted after phase 1b (stop requested)")
                return

            # Phase 1c: StockTwits trending discovery
            self._current_phase = "stocktwits_discovery"
            self._update_state(market_analysis, {"phase": "stocktwits_discovery"})
            social_discovered = self._time_phase(
                "stocktwits_discovery", market_analysis,
                self._phase_1c_social_discovery, screener_candidates, survivors, new_ones, market_analysis
            )
            if social_discovered:
                all_known_symbols = (
                    {c.get("symbol") for c in survivors}
                    | {c.get("symbol") for c in new_ones}
                )
                social_new = [d for d in social_discovered if d.get("symbol") not in all_known_symbols]
                if social_new:
                    self.logger.info(
                        f"Phase 1c added {len(social_new)} StockTwits trending candidates"
                    )
                    new_ones = new_ones + social_new
            if self._stop_event.is_set():
                self.logger.info("Pipeline aborted after phase 1c (stop requested)")
                return

            # Combine survivors + LLM-discovered + StockTwits-discovered for deep triage
            deep_triage_input = survivors + new_ones

            # Phase 3: Deep triage via LLM
            self._current_phase = "deep_triage"
            self._update_state(market_analysis, {"phase": "deep_triage"})
            finalists = self._time_phase(
                "deep_triage", market_analysis, self._phase_3_deep_triage, deep_triage_input, market_analysis
            )
            self.logger.debug(f"Phase 3 complete: {len(finalists)} finalists")
            if self._stop_event.is_set():
                self.logger.info("Pipeline aborted after phase 3 (stop requested)")
                return
        else:
            self.logger.info(
                "Insufficient balance - skipping scan, monitoring existing only"
            )
            finalists = []

        # Phase 4: Entry condition setup
        self._current_phase = "entry_setup"
        self._update_state(market_analysis, {"phase": "entry_setup"})
        self._time_phase(
            "entry_setup", market_analysis, self._phase_4_entry_conditions, finalists, market_analysis
        )
        if self._stop_event.is_set():
            self.logger.info("Pipeline aborted after phase 4 (stop requested)")
            return

        # Phase 5: Monitor
        self._current_phase = "monitoring"
        self._update_state(market_analysis, {"phase": "monitoring"})
        self._time_phase("monitoring", market_analysis, self._phase_5_monitor, market_analysis)

        # Phase 6: EOD
        self._current_phase = "eod"
        self._update_state(market_analysis, {"phase": "eod"})
        self._time_phase("eod", market_analysis, self._phase_6_eod, market_analysis)
        self._current_phase = "complete"
        total_elapsed = round(time.time() - pipeline_start, 1)
        self._update_state(market_analysis, {
            "phase": "complete",
            "pipeline_total_seconds": total_elapsed,
        })
        self.logger.info(f"=== Daily pipeline complete in {total_elapsed}s ===")

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    def _phase_0_review(self, market_analysis: MarketAnalysis):
        """Review existing open positions and record current state."""
        self.logger.info("Phase 0: Reviewing existing positions")
        trade_mgr = self._trade_mgr
        open_positions = trade_mgr.get_open_positions()

        self.logger.info(f"Found {len(open_positions)} open positions")

        # Check for positions exceeding max holding days
        max_holding = int(self.get_setting_with_interface_default(
            "max_holding_days", log_warning=False
        ))
        for pos in open_positions:
            try:
                with get_db() as session:
                    from ....core.models import Transaction

                    trans = session.get(Transaction, pos["transaction_id"])
                    if trans and trans.created_at:
                        age_days = (datetime.now(timezone.utc) - trans.created_at).days
                        if age_days >= max_holding:
                            self.logger.info(
                                f"Position {pos['symbol']} held for {age_days} days "
                                f"(max {max_holding}), forcing exit"
                            )
                            trade_mgr.execute_exit(
                                pos["symbol"],
                                exit_pct=100.0,
                                reason=f"max holding period ({max_holding} days) exceeded",
                            )
            except Exception as e:
                self.logger.error(
                    f"Error checking position age for {pos['symbol']}: {e}",
                    exc_info=True,
                )

        self._update_state(
            market_analysis,
            {
                "open_positions": [
                    {
                        "symbol": p["symbol"],
                        "qty": p["qty"],
                        "entry_price": p["entry_price"],
                    }
                    for p in open_positions
                ],
            },
        )

    def _phase_1_screen(self, market_analysis: MarketAnalysis) -> List[Dict[str, Any]]:
        """Screen stocks via screener provider, merge gainers, enrich with RVOL, and filter for tradability."""
        self.logger.info("Phase 1: Screening stocks")

        screener_name = self.get_setting_with_interface_default(
            "screener_provider", log_warning=False
        )

        from ....modules.dataproviders import get_provider

        screener = get_provider("screener", screener_name)

        # Build filters from settings
        sector_exclude_str = self.get_setting_with_interface_default(
            "scan_sector_exclude", log_warning=False
        )
        sector_exclude = (
            [s.strip() for s in sector_exclude_str.split(",") if s.strip()]
            if sector_exclude_str
            else []
        )

        max_candidates = int(self.get_setting_with_interface_default(
            "max_scan_candidates", log_warning=False
        ))
        min_rvol = float(self.get_setting_with_interface_default(
            "min_relative_volume", log_warning=False
        ))
        include_gainers = self.get_setting_with_interface_default(
            "include_gainers", log_warning=False
        )
        price_min = self.get_setting_with_interface_default("scan_price_min", log_warning=False)
        price_max = self.get_setting_with_interface_default("scan_price_max", log_warning=False)
        mcap_min = self.get_setting_with_interface_default("scan_market_cap_min", log_warning=False)
        mcap_max = self.get_setting_with_interface_default("scan_market_cap_max", log_warning=False)
        float_max = self.get_setting_with_interface_default("scan_float_max", log_warning=False)

        filters = {
            "price_min": price_min,
            "price_max": price_max,
            "volume_min": self.get_setting_with_interface_default(
                "scan_volume_min", log_warning=False
            ),
            "market_cap_min": mcap_min,
            "market_cap_max": mcap_max,
            "sector_exclude": sector_exclude,
        }
        if float_max and float(float_max) > 0:
            filters["float_max"] = float_max

        candidates = screener.screen_stocks(filters)
        self.logger.info(f"Screener returned {len(candidates)} candidates")

        # Track all filtered stocks with reasons
        filtered_stocks: Dict[str, Dict[str, Any]] = {}

        # Merge FMP top gainers into candidate pool
        seen_symbols = {c.get("symbol", "").upper() for c in candidates if c.get("symbol")}
        gainers_count = 0
        if include_gainers:
            try:
                raw_gainers = self._fetch_gainers()
                for g in raw_gainers:
                    sym = (g.get("symbol") or "").upper()
                    if not sym or sym in seen_symbols:
                        continue
                    g_price = g.get("price", 0) or 0
                    g_mcap = g.get("marketCap", 0) or 0
                    if price_min is not None and g_price < float(price_min):
                        continue
                    if price_max is not None and g_price > float(price_max):
                        continue
                    if mcap_min is not None and g_mcap < float(mcap_min):
                        continue
                    if mcap_max is not None and g_mcap > float(mcap_max):
                        continue
                    g_sector = (g.get("sector") or "").lower()
                    if sector_exclude and g_sector in [s.lower() for s in sector_exclude]:
                        continue
                    candidates.append({
                        "symbol": sym,
                        "company_name": g.get("name", ""),
                        "price": g_price,
                        "volume": g.get("volume", 0),
                        "market_cap": g_mcap,
                        "sector": g.get("sector", ""),
                        "industry": g.get("industry", ""),
                        "exchange": g.get("exchangeShortName") or g.get("exchange", ""),
                        "_source": "gainers",
                    })
                    seen_symbols.add(sym)
                    gainers_count += 1
                if gainers_count:
                    self.logger.info(f"Merged {gainers_count} FMP top gainers into candidate pool")
            except Exception as e:
                self.logger.warning(f"Failed to fetch FMP gainers: {e}")

        # Exclude symbols where we already hold open positions
        open_position_symbols = {
            pos["symbol"] for pos in self._trade_mgr.get_open_positions()
        }
        if open_position_symbols:
            before = len(candidates)
            for c in candidates:
                sym = c.get("symbol")
                if sym and sym in open_position_symbols:
                    filtered_stocks[sym] = {
                        "phase": "screen",
                        "reason": "already_held",
                        "details": "Symbol already in open positions",
                    }
            candidates = [c for c in candidates if c.get("symbol") not in open_position_symbols]
            self.logger.debug(
                f"Excluded {before - len(candidates)} already-held symbols from screener results"
            )

        # Enrich with RVOL via FMP batch quotes
        all_symbols = [c["symbol"] for c in candidates if c.get("symbol")]
        quotes_map = self._fetch_quotes_chunked(all_symbols) if all_symbols else {}

        for c in candidates:
            sym = c.get("symbol", "").upper()
            quote = quotes_map.get(sym, {})
            volume = quote.get("volume") or c.get("volume") or 0
            avg_vol = quote.get("avgVolume", 0) or 0
            rvol = round(volume / avg_vol, 2) if avg_vol > 0 else 0.0
            c["volume"] = volume
            c["avg_volume"] = avg_vol
            c["rvol"] = rvol
            c["change_percent"] = quote.get("changesPercentage", 0) or 0
            # Update price from quote if available
            q_price = quote.get("price")
            if q_price and q_price > 0:
                c["price"] = q_price

        # Filter by minimum relative volume
        if min_rvol > 0:
            before = len(candidates)
            for c in candidates:
                if c.get("rvol", 0) < min_rvol:
                    sym = c.get("symbol")
                    if sym:
                        filtered_stocks[sym] = {
                            "phase": "screen",
                            "reason": "low_rvol",
                            "details": f"RVOL {c.get('rvol', 0):.1f}x below minimum {min_rvol:.1f}x",
                        }
            candidates = [c for c in candidates if c.get("rvol", 0) >= min_rvol]
            dropped = before - len(candidates)
            if dropped:
                self.logger.info(f"Dropped {dropped} candidates below RVOL {min_rvol:.1f}x")

        # Sort by RVOL descending (unusual volume first) and cap at max_scan_candidates
        candidates.sort(key=lambda c: c.get("rvol", 0), reverse=True)
        if len(candidates) > max_candidates:
            self.logger.info(
                f"Capping candidates from {len(candidates)} to top {max_candidates} by RVOL"
            )
            for c in candidates[max_candidates:]:
                sym = c.get("symbol")
                if sym:
                    filtered_stocks[sym] = {
                        "phase": "screen",
                        "reason": "rvol_cap",
                        "details": f"Ranked beyond top {max_candidates} by RVOL (rvol={c.get('rvol', 0):.1f}x)",
                    }
            candidates = candidates[:max_candidates]

        self.logger.info(
            f"Phase 1 after RVOL enrichment: {len(candidates)} candidates "
            f"(top RVOL: {candidates[0].get('rvol', 0):.1f}x)" if candidates else
            f"Phase 1 after RVOL enrichment: 0 candidates"
        )

        # Filter through account to remove untradeable symbols
        if candidates:
            from ....core.utils import get_account_instance_from_id
            account = get_account_instance_from_id(self.instance.account_id)
            candidate_symbols = [c["symbol"] for c in candidates if c.get("symbol")]
            if candidate_symbols:
                tradeable = account.symbols_exist(candidate_symbols)
                for c in candidates:
                    sym = c.get("symbol")
                    if sym and not tradeable.get(sym, False):
                        filtered_stocks[sym] = {
                            "phase": "screen",
                            "reason": "not_tradeable",
                            "details": "Symbol not tradeable on broker account",
                        }
                candidates = [
                    c
                    for c in candidates
                    if c.get("symbol") and tradeable.get(c["symbol"], False)
                ]
                self.logger.info(
                    f"{len(candidates)} candidates remain after tradability filter"
                )

                # Queue symbols for InstrumentAutoAdder
                tradeable_symbols = [c["symbol"] for c in candidates]
                if tradeable_symbols:
                    try:
                        from ....core.InstrumentAutoAdder import get_instrument_auto_adder

                        auto_adder = get_instrument_auto_adder()
                        auto_adder.queue_instruments_for_addition(
                            symbols=tradeable_symbols,
                            expert_shortname=f"penny-{self.instance.id}",
                            source="expert",
                            extra_labels=["Penny"],
                        )
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to queue instruments for auto-adder: {e}"
                        )

        # Save scan results and filtered stocks
        self._update_state(market_analysis, {
            "scan_results": candidates,
            "filtered_stocks": filtered_stocks,
        })
        self._save_analysis_output(
            market_analysis,
            provider_category="screener",
            provider_name=screener_name,
            name="scan_raw_screener_response",
            output_type="json",
            text=json.dumps(candidates, default=str),
            symbol="PENNY_SCAN",
        )

        if candidates:
            from ....core.db import log_activity
            from ....core.types import ActivityLogSeverity, ActivityLogType
            symbols = [c["symbol"] for c in candidates if c.get("symbol")]
            log_activity(
                severity=ActivityLogSeverity.INFO,
                activity_type=ActivityLogType.ANALYSIS_COMPLETED,
                description=f"PennyMomentumTrader screened {len(symbols)} tradeable candidates: {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}",
                data={"symbols": symbols, "total": len(symbols)},
                source_expert_id=self.instance.id,
            )

        return candidates

    def _get_previously_monitored_symbols(self, current_market_analysis_id: int) -> set:
        """Return symbols from the most recent prior MarketAnalysis (if any)."""
        try:
            with get_db() as session:
                from sqlmodel import select as sql_select
                statement = (
                    sql_select(MarketAnalysis)
                    .where(MarketAnalysis.expert_instance_id == self.instance.id)
                    .where(MarketAnalysis.id != current_market_analysis_id)
                    .order_by(MarketAnalysis.created_at.desc())  # type: ignore[union-attr]
                    .limit(1)
                )
                ma = session.exec(statement).first()
                if ma and ma.state:
                    return set(ma.state.get("monitored_symbols", {}).keys())
        except Exception as e:
            self.logger.warning(f"Failed to load previously monitored symbols: {e}")
        return set()

    def _full_scan_completed_today(self, current_market_analysis_id: int) -> bool:
        """Return True if a full (non-resume) scan was already completed today."""
        try:
            today = datetime.utcnow().date()
            with get_db() as session:
                from sqlmodel import select as sql_select
                statement = (
                    sql_select(MarketAnalysis)
                    .where(MarketAnalysis.expert_instance_id == self.instance.id)
                    .where(MarketAnalysis.id != current_market_analysis_id)
                    .order_by(MarketAnalysis.created_at.desc())  # type: ignore[union-attr]
                    .limit(10)
                )
                for ma in session.exec(statement).all():
                    if not ma.state or not ma.created_at:
                        continue
                    if ma.created_at.date() != today:
                        break  # records are newest-first; stop at first record from a prior day
                    # deep_triage_results is only written during a full scan
                    if ma.state.get("deep_triage_results") is not None:
                        return True
        except Exception as e:
            self.logger.warning(f"Failed to check today's scan history: {e}")
        return False

    def _get_previous_monitored_data(self, current_market_analysis_id: int) -> Dict[str, Dict[str, Any]]:
        """Return the full monitored_symbols dict from the most recent prior MarketAnalysis."""
        try:
            with get_db() as session:
                from sqlmodel import select as sql_select
                statement = (
                    sql_select(MarketAnalysis)
                    .where(MarketAnalysis.expert_instance_id == self.instance.id)
                    .where(MarketAnalysis.id != current_market_analysis_id)
                    .order_by(MarketAnalysis.created_at.desc())  # type: ignore[union-attr]
                    .limit(1)
                )
                ma = session.exec(statement).first()
                if ma and ma.state:
                    return dict(ma.state.get("monitored_symbols", {}))
        except Exception as e:
            self.logger.warning(f"Failed to load previous monitored data: {e}")
        return {}

    def _phase_1b_llm_discovery(
        self, screener_candidates: List[Dict[str, Any]], market_analysis: MarketAnalysis
    ) -> List[Dict[str, Any]]:
        """Use a websearch LLM to discover additional penny stock candidates."""
        discovery_model = self.get_setting_with_interface_default(
            "discovery_llm", log_warning=False
        )
        count = int(self.get_setting_with_interface_default(
            "max_discovery_candidates", log_warning=False
        ))

        if count <= 0:
            return []

        # Build the known-symbols set: open positions + previously monitored + screener results
        open_position_symbols = {
            pos["symbol"] for pos in self._trade_mgr.get_open_positions()
        }
        prev_monitored = self._get_previously_monitored_symbols(market_analysis.id)
        screener_symbols = {c.get("symbol") for c in screener_candidates if c.get("symbol")}
        all_known = open_position_symbols | prev_monitored | screener_symbols

        price_min = self.get_setting_with_interface_default("scan_price_min", log_warning=False)
        price_max = self.get_setting_with_interface_default("scan_price_max", log_warning=False)
        volume_min = int(self.get_setting_with_interface_default("scan_volume_min", log_warning=False))

        known_list = ", ".join(sorted(all_known)) if all_known else "none"
        prompt = (
            f"You are a penny stock momentum trader scanning for today's top movers.\n\n"
            f"Search the web and find {count} US penny stocks with strong momentum catalysts RIGHT NOW.\n\n"
            f"Criteria:\n"
            f"- Price roughly between ${price_min:.2f} and ${price_max:.2f}\n"
            f"- High trading volume today\n"
            f"- Clear catalyst today (earnings, FDA news, SEC filing, short squeeze, "
            f"unusual options activity, social media buzz, technical breakout)\n"
            f"- US-listed stocks only (NYSE, NASDAQ, OTC)\n\n"
            f"We are ALREADY tracking or holding these symbols — DO NOT include them:\n"
            f"{known_list}\n\n"
            f"IMPORTANT: Do NOT invent or guess prices or volumes. We will fetch real data ourselves.\n\n"
            f"Return ONLY a JSON array (no explanation, no markdown) of exactly {count} objects:\n"
            f'[{{"symbol": "ABCD", "catalyst": "short description", "reason": "why it has momentum"}}]'
        )

        self.logger.debug(
            f"Phase 1b: calling discovery LLM {discovery_model} for {count} extra candidates "
            f"(excluding {len(all_known)} known symbols)"
        )

        try:
            llm = ModelFactory.create_llm(
                discovery_model,
                temperature=0.3,
                expert_instance_id=self.instance.id,
                use_case="PennyMomentum LLM Discovery",
            )
            response = llm.invoke(prompt)
            raw_text = response.content if hasattr(response, "content") else str(response)

            items = self._parse_json_response(raw_text, expected_type=list) or []
            self.logger.info(f"Phase 1b: discovery LLM returned {len(items)} candidates")

            # Normalise and validate each item
            # Collect valid symbols first, then batch-fetch prices
            valid_items: List[Dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                symbol = item.get("symbol", "").strip().upper()
                if not symbol or symbol in all_known:
                    continue
                valid_items.append({"symbol": symbol, **item})

            # Batch-fetch live prices for all discovered symbols
            discovered_symbols = [vi["symbol"] for vi in valid_items]
            live_prices = self._get_live_prices(discovered_symbols) if discovered_symbols else {}

            results: List[Dict[str, Any]] = []
            for item in valid_items:
                symbol = item["symbol"]
                live_price = live_prices.get(symbol)
                if not live_price or live_price <= 0:
                    self.logger.debug(f"Phase 1b: skipping {symbol} (no live price)")
                    continue
                results.append({
                    "symbol": symbol,
                    "price": live_price,
                    "volume": None,
                    "market_cap": None,
                    "sector": None,
                    "industry": None,
                    "exchange": None,
                    "beta": None,
                    "is_actively_trading": True,
                    "company_name": None,
                    "country": "US",
                    "_discovery_catalyst": item.get("catalyst", ""),
                    "_discovery_reason": item.get("reason", ""),
                })

            self._save_analysis_output(
                market_analysis,
                provider_category="llm",
                provider_name=discovery_model,
                name="llm_discovery_response",
                output_type="json",
                text=raw_text,
                symbol="PENNY_SCAN",
                prompt=prompt,
            )
            return results

        except Exception as e:
            self.logger.error(f"Phase 1b: discovery LLM failed: {e}", exc_info=True)
            return []

    def _phase_1c_social_discovery(
        self,
        screener_candidates: List[Dict[str, Any]],
        survivors: List[Dict[str, Any]],
        llm_discovered: List[Dict[str, Any]],
        market_analysis: MarketAnalysis,
    ) -> List[Dict[str, Any]]:
        """Fetch StockTwits trending symbols and return new candidates not already known."""
        use_discovery = self.get_setting_with_interface_default(
            "use_stocktwits_discovery", log_warning=False
        )
        if not use_discovery:
            return []

        oauth_token = self.get_setting_with_interface_default(
            "stocktwits_oauth_token", log_warning=False
        ) or ""  # empty string = unauthenticated public access (still works)

        price_max = float(self.get_setting_with_interface_default(
            "stocktwits_discovery_price_max", log_warning=False
        ))

        # Build set of all already-known symbols to avoid duplicates
        all_known_symbols = (
            {c.get("symbol") for c in screener_candidates if c.get("symbol")}
            | {c.get("symbol") for c in survivors if c.get("symbol")}
            | {c.get("symbol") for c in llm_discovered if c.get("symbol")}
            | {pos["symbol"] for pos in self._trade_mgr.get_open_positions()}
        )

        self.logger.info(
            f"Phase 1c: fetching StockTwits trending symbols (price_max=${price_max:.2f})"
        )

        try:
            from ....modules.dataproviders.socialmedia.StockTwitsTrending import StockTwitsTrending

            provider = StockTwitsTrending(
                oauth_token=oauth_token,
                price_max=price_max,
            )
            trending = provider.get_trending_symbols()
            self.logger.info(
                f"Phase 1c: StockTwits returned {len(trending)} symbols under ${price_max:.2f}"
            )
        except Exception as e:
            self.logger.warning(f"Phase 1c: StockTwits fetch failed: {e}")
            return []

        results: List[Dict[str, Any]] = []
        for item in trending:
            symbol = item.get("symbol", "").upper()
            if not symbol or symbol in all_known_symbols:
                continue
            price = item.get("price")
            if not price or price <= 0:
                continue
            results.append({
                "symbol": symbol,
                "price": price,
                "volume": item.get("volume"),
                "market_cap": item.get("market_cap"),
                "sector": item.get("sector"),
                "industry": item.get("industry"),
                "exchange": item.get("exchange"),
                "beta": None,
                "is_actively_trading": True,
                "company_name": item.get("company_name"),
                "country": "US",
                "rvol": item.get("rvol"),
                "change_percent": item.get("change_pct"),
                "_source": "stocktwits_trending",
                "_stocktwits_sources": item.get("sources", []),
                "_stocktwits_watchlist_count": item.get("watchlist_count"),
            })
            all_known_symbols.add(symbol)

        self.logger.info(
            f"Phase 1c: {len(results)} new StockTwits candidates "
            f"(not in screener/survivors/llm-discovered)"
        )
        return results

    def _phase_2_quick_filter(
        self, candidates: List[Dict[str, Any]], market_analysis: MarketAnalysis
    ) -> List[Dict[str, Any]]:
        """Quick-filter candidates via fast LLM, optionally enriched with StockTwits data."""
        self.logger.info(f"Phase 2: Quick filtering {len(candidates)} candidates")

        if not candidates:
            self.logger.debug("Phase 2: no candidates, skipping LLM call")
            self._update_state(market_analysis, {"quick_filter_survivors": []})
            return []

        # Enrich with StockTwits data if configured
        vendor_social = self.get_setting_with_interface_default(
            "vendor_social", log_warning=False
        )
        if "stocktwits" in (vendor_social or []):
            candidates = self._enrich_with_stocktwits(candidates)

        max_survivors = int(self.get_setting_with_interface_default(
            "max_quick_filter_candidates", log_warning=False
        ))
        prompt = build_quick_filter_prompt(candidates, max_survivors)

        scanning_model = self.get_setting_with_interface_default(
            "scanning_llm", log_warning=False
        )
        self.logger.debug(f"Phase 2: calling LLM {scanning_model} (max_survivors={max_survivors})")
        llm = ModelFactory.create_llm(
            scanning_model,
            temperature=0.3,
            expert_instance_id=self.instance.id,
            use_case="PennyMomentum Quick Filter",
        )
        response = llm.invoke(prompt)
        raw_text = response.content if hasattr(response, "content") else str(response)

        # Parse JSON response — new format returns {selected: [...], dropped: [...]}
        parsed = self._parse_json_response(raw_text, expected_type=dict)
        if parsed and "selected" in parsed:
            survivors = parsed.get("selected", [])
            dropped_list = parsed.get("dropped", [])
        else:
            # Fallback: old format (plain list) or parse failure
            survivors = self._parse_json_response(raw_text, expected_type=list) or []
            dropped_list = []

        survivor_symbols = [s["symbol"] for s in survivors if isinstance(s, dict) and "symbol" in s]

        self.logger.info(f"Quick filter kept {len(survivor_symbols)} candidates")

        # Track LLM-dropped stocks with justification
        filtered_stocks = dict(market_analysis.state.get("filtered_stocks", {}))
        survivor_set = set(survivor_symbols)
        for item in dropped_list:
            if isinstance(item, dict) and "symbol" in item:
                sym = item["symbol"]
                filtered_stocks[sym] = {
                    "phase": "quick_filter",
                    "reason": "llm_rejected",
                    "details": item.get("reason", "Dropped by LLM quick filter"),
                }
        # Also catch any candidates not in survivors or dropped (edge case)
        for c in candidates:
            sym = c.get("symbol")
            if sym and sym not in survivor_set and sym not in filtered_stocks:
                filtered_stocks[sym] = {
                    "phase": "quick_filter",
                    "reason": "llm_rejected",
                    "details": "Not selected by LLM quick filter (no specific reason provided)",
                }

        # Save outputs
        self._update_state(market_analysis, {
            "quick_filter_survivors": survivor_symbols,
            "filtered_stocks": filtered_stocks,
        })
        self._save_analysis_output(
            market_analysis,
            provider_category="llm",
            provider_name=scanning_model,
            name="quick_filter_response",
            output_type="json",
            text=raw_text,
            symbol="PENNY_SCAN",
            prompt=prompt,
        )

        # Return full candidate dicts for survivors
        return [c for c in candidates if c.get("symbol") in survivor_set]

    def _phase_3_deep_triage(
        self, survivors: List[Dict[str, Any]], market_analysis: MarketAnalysis
    ) -> List[Dict[str, Any]]:
        """Deep triage analysis of survivors via analytical LLM."""
        self.logger.info(f"Phase 3: Deep triage of {len(survivors)} survivors")

        if not survivors:
            self._update_state(market_analysis, {"deep_triage_results": {}})
            return []

        max_final = int(self.get_setting_with_interface_default(
            "max_final_candidates", log_warning=False
        ))
        deep_model = self.get_setting_with_interface_default(
            "deep_analysis_llm", log_warning=False
        )

        deep_triage_results: Dict[str, Dict[str, Any]] = {}
        finalists: List[Dict[str, Any]] = []
        filtered_stocks = dict(market_analysis.state.get("filtered_stocks", {}))

        # Process ALL candidates (input is already bounded by phase 1+2 limits).
        # max_final_candidates caps the OUTPUT — we take the top N by confidence.
        total_survivors = len(survivors)
        for idx, candidate in enumerate(survivors, 1):
            if self._stop_event.is_set():
                break

            symbol = candidate.get("symbol")
            if not symbol:
                continue

            self.logger.info(f"Phase 3: {idx}/{total_survivors} - deep triage {symbol}")

            # Update progress state so UI can show live status during long phase
            self._update_state(market_analysis, {
                "deep_triage_progress": {
                    "current_symbol": symbol,
                    "processed": idx - 1,
                    "total": total_survivors,
                    "passed": len(finalists),
                },
            })

            # Gather data from all configured vendors in parallel
            self.logger.debug(f"Phase 3 [{symbol}]: gathering data (news, fundamentals, insider, social)")
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    executor.submit(self._gather_news, symbol): "news",
                    executor.submit(self._gather_fundamentals, symbol): "fundamentals",
                    executor.submit(self._gather_insider, symbol): "insider",
                    executor.submit(self._gather_social, symbol): "social",
                }
                gathered = {}
                for future in as_completed(futures):
                    key = futures[future]
                    try:
                        gathered[key] = future.result()
                    except Exception as e:
                        self.logger.warning(f"Phase 3 [{symbol}]: {key} gathering failed: {e}")
                        gathered[key] = f"No {key} data available."

            news_text = gathered.get("news", "No news data available.")
            fundamentals_text = gathered.get("fundamentals", "No fundamentals data available.")
            insider_text = gathered.get("insider", "No insider data available.")
            social_text = gathered.get("social", "No social sentiment data available.")

            # Build market context string for strategy timing guidance
            now_utc = datetime.now(timezone.utc)
            # EST = UTC-5, EDT = UTC-4 (DST approx Mar–Nov)
            from datetime import date as _date
            _month = now_utc.month
            _et_offset = -4 if 3 <= _month <= 11 else -5
            now_et = now_utc + timedelta(hours=_et_offset)
            tz_label = "EDT" if _et_offset == -4 else "EST"
            is_open = self._is_market_open()
            market_state = "Regular session open (09:30–16:00 ET)" if is_open else (
                "Pre-market" if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30)
                else "After-hours / post-market"
            )
            market_context = (
                f"{market_state}. "
                f"Current time: {now_et.strftime('%Y-%m-%d %H:%M')} {tz_label}."
            )

            # Build prompt and call LLM
            prompt = build_deep_triage_prompt(
                symbol=symbol,
                news=news_text,
                insider=insider_text,
                fundamentals=fundamentals_text,
                social=social_text,
                market_context=market_context,
            )

            try:
                self.logger.debug(f"Phase 3 [{symbol}]: calling LLM {deep_model}")
                llm = ModelFactory.create_llm(
                    deep_model,
                    temperature=0.3,
                    expert_instance_id=self.instance.id,
                    use_case="PennyMomentum Deep Triage",
                )
                response = llm.invoke(prompt)
                raw_text = (
                    response.content if hasattr(response, "content") else str(response)
                )

                result = self._parse_json_response(raw_text, expected_type=dict)
                if not result:
                    self.logger.warning(f"Phase 3 [{symbol}]: failed to parse LLM response")
                    filtered_stocks[symbol] = {
                        "phase": "deep_triage",
                        "reason": "llm_parse_failed",
                        "details": "Failed to parse LLM deep triage response",
                    }
                    continue

                confidence = result.get("confidence", 0)
                self.logger.debug(f"Phase 3 [{symbol}]: confidence={confidence}, catalyst={result.get('catalyst', '')!r}")
                min_confidence = int(self.get_setting_with_interface_default(
                    "min_confidence_threshold", log_warning=False
                ))
                if confidence < min_confidence:
                    self.logger.info(
                        f"{symbol} confidence {confidence} below threshold {min_confidence}, skipping"
                    )
                    filtered_stocks[symbol] = {
                        "phase": "deep_triage",
                        "reason": "low_confidence",
                        "details": (
                            f"Confidence {confidence} below threshold {min_confidence}. "
                            f"Catalyst: {result.get('catalyst', 'N/A')}. "
                            f"Risk: {result.get('risk_assessment', 'N/A')}. "
                            f"Reasoning: {result.get('reasoning', 'N/A')}"
                        ),
                    }
                    self._update_state(market_analysis, {
                        "filtered_stocks": dict(filtered_stocks),
                        "deep_triage_progress": {
                            "current_symbol": symbol,
                            "processed": idx,
                            "total": total_survivors,
                            "passed": len(finalists),
                        },
                    })
                    continue

                # Create ExpertRecommendation
                time_horizon = (
                    TimeHorizon.SHORT_TERM
                    if result.get("strategy") == "intraday"
                    else TimeHorizon.MEDIUM_TERM
                )
                rec = ExpertRecommendation(
                    instance_id=self.instance.id,
                    symbol=symbol,
                    recommended_action=OrderRecommendation.BUY,
                    expected_profit_percent=result.get("expected_profit_pct", 0),
                    price_at_date=candidate.get("price"),
                    confidence=confidence,
                    risk_level=RiskLevel.HIGH,
                    time_horizon=time_horizon,
                    details=result.get("reasoning", ""),
                    market_analysis_id=market_analysis.id,
                    data={
                        "catalyst": result.get("catalyst", ""),
                        "strategy": result.get("strategy", ""),
                        "risk_assessment": result.get("risk_assessment", ""),
                    },
                )
                add_instance(rec)

                from ....core.db import log_activity
                from ....core.types import ActivityLogSeverity, ActivityLogType
                log_activity(
                    severity=ActivityLogSeverity.SUCCESS,
                    activity_type=ActivityLogType.EXPERT_RECOMMENDATION,
                    description=(
                        f"PennyMomentumTrader recommendation: BUY {symbol} "
                        f"| Confidence: {confidence}% "
                        f"| Catalyst: {result.get('catalyst', 'N/A')} "
                        f"| Expected profit: {result.get('expected_profit_pct', 0):.1f}%"
                    ),
                    data={
                        "symbol": symbol,
                        "confidence": confidence,
                        "catalyst": result.get("catalyst", ""),
                        "strategy": result.get("strategy", ""),
                        "expected_profit_pct": result.get("expected_profit_pct", 0),
                    },
                    source_expert_id=self.instance.id,
                )

                result["price"] = candidate.get("price")
                deep_triage_results[symbol] = result
                finalists.append(
                    {
                        "symbol": symbol,
                        "confidence": confidence,
                        "price": candidate.get("price"),
                        "catalyst": result.get("catalyst", ""),
                        "strategy": result.get("strategy", ""),
                    }
                )

                # Persist incremental results so UI shows progress during long phase
                self._update_state(market_analysis, {
                    "deep_triage_results": dict(deep_triage_results),
                    "filtered_stocks": dict(filtered_stocks),
                    "deep_triage_progress": {
                        "current_symbol": symbol,
                        "processed": idx,
                        "total": total_survivors,
                        "passed": len(finalists),
                    },
                })

                # Save per-symbol analysis output
                self._save_analysis_output(
                    market_analysis,
                    provider_category="llm",
                    provider_name=deep_model,
                    name=f"deep_triage_{symbol}",
                    output_type="json",
                    text=raw_text,
                    symbol=symbol,
                    prompt=prompt,
                )

            except Exception as e:
                self.logger.error(
                    f"Deep triage failed for {symbol}: {e}", exc_info=True
                )
                filtered_stocks[symbol] = {
                    "phase": "deep_triage",
                    "reason": "deep_triage_error",
                    "details": f"Deep triage failed: {e}",
                }

        # Cap finalists to max_final by confidence (highest confidence first)
        if len(finalists) > max_final:
            finalists.sort(key=lambda f: f.get("confidence", 0), reverse=True)
            dropped = finalists[max_final:]
            finalists = finalists[:max_final]
            cutoff_confidence = finalists[-1].get("confidence", 0) if finalists else 0
            self.logger.info(
                f"Capped finalists to top {max_final} by confidence "
                f"(dropped: {[f['symbol'] for f in dropped]})"
            )
            # Remove dropped symbols from deep_triage_results to keep state consistent
            for f in dropped:
                sym = f["symbol"]
                deep_triage_results.pop(sym, None)
                filtered_stocks[sym] = {
                    "phase": "deep_triage",
                    "reason": "capped_by_limit",
                    "details": (
                        f"Confidence {f.get('confidence', 0)} passed threshold but "
                        f"ranked beyond top {max_final} finalists (cutoff: {cutoff_confidence})"
                    ),
                }

        # Calculate position sizes for finalists
        if finalists:
            trade_mgr = self._trade_mgr
            available = self.get_available_balance() or 0
            sizing = trade_mgr.calculate_position_sizes(finalists, available)
            for symbol, size_info in sizing.items():
                if symbol in deep_triage_results:
                    deep_triage_results[symbol]["qty"] = size_info["qty"]
                    deep_triage_results[symbol]["allocation"] = size_info["allocation"]

        self._update_state(market_analysis, {
            "deep_triage_results": deep_triage_results,
            "filtered_stocks": filtered_stocks,
        })
        self.logger.info(f"Deep triage produced {len(finalists)} finalists")
        return finalists

    def _phase_4_entry_conditions(
        self, finalists: List[Dict[str, Any]], market_analysis: MarketAnalysis
    ):
        """Generate structured entry/exit conditions for new finalists."""
        self.logger.info(f"Phase 4: Setting entry conditions for {len(finalists)} finalists")

        entry_model = self.get_setting_with_interface_default(
            "entry_definition_llm", log_warning=False
        )
        condition_types_str = get_condition_types_for_llm()

        # Load existing monitored symbols from state; seed from previous run if empty
        # (handles app restart and daily cycle where a fresh MarketAnalysis is created)
        monitored: Dict[str, Dict[str, Any]] = dict(
            market_analysis.state.get("monitored_symbols", {})
        )
        if not monitored:
            prev_data = self._get_previous_monitored_data(market_analysis.id)
            carried = {
                sym: info
                for sym, info in prev_data.items()
                if info.get("status") == "watching"
            }
            if carried:
                self.logger.info(
                    f"Phase 4: carrying over {len(carried)} monitored symbols from previous run: "
                    f"{list(carried.keys())}"
                )
                monitored = carried
        max_monitored = int(self.get_setting_with_interface_default(
            "max_monitored_symbols", log_warning=False
        ))

        # Expire old monitors
        max_age = int(self.get_setting_with_interface_default(
            "max_entry_age_days", log_warning=False
        ))
        now = datetime.now(timezone.utc)
        expired_symbols = []
        for sym, info in monitored.items():
            created_str = info.get("created_at")
            if created_str:
                try:
                    created = datetime.fromisoformat(created_str)
                    if (now - created).days >= max_age and info.get("status") == "watching":
                        expired_symbols.append(sym)
                except (ValueError, TypeError):
                    pass
        for sym in expired_symbols:
            self.logger.info(f"Expiring monitor for {sym} (age exceeded {max_age} days)")
            monitored[sym]["status"] = "expired"

        # Add new finalists
        total_finalists = len(finalists)
        for idx, finalist in enumerate(finalists, 1):
            if self._stop_event.is_set():
                break

            symbol = finalist["symbol"]
            self.logger.info(f"Phase 4: {idx}/{total_finalists} - setting conditions for {symbol}")
            if symbol in monitored and monitored[symbol].get("status") == "watching":
                self.logger.debug(f"{symbol} already being monitored, skipping condition generation")
                continue

            # Check monitor limit
            active_count = sum(
                1 for m in monitored.values() if m.get("status") == "watching"
            )
            if active_count >= max_monitored:
                self.logger.info(
                    f"Monitor limit reached ({max_monitored}), skipping {symbol}"
                )
                continue

            # Build analysis summary for prompt
            deep_triage = market_analysis.state.get("deep_triage_results", {})
            triage_data = deep_triage.get(symbol, {})
            analysis_summary = (
                f"Symbol: {symbol}\n"
                f"Catalyst: {triage_data.get('catalyst', 'N/A')}\n"
                f"Strategy: {triage_data.get('strategy', 'N/A')}\n"
                f"Confidence: {triage_data.get('confidence', 'N/A')}\n"
                f"Expected Profit: {triage_data.get('expected_profit_pct', 'N/A')}%\n"
                f"Risk: {triage_data.get('risk_assessment', 'N/A')}\n"
                f"Reasoning: {triage_data.get('reasoning', 'N/A')}"
            )

            prompt = build_entry_conditions_prompt(
                symbol=symbol,
                analysis_summary=analysis_summary,
                condition_types_str=condition_types_str,
            )

            try:
                llm = ModelFactory.create_llm(
                    entry_model,
                    temperature=0.3,
                    expert_instance_id=self.instance.id,
                    use_case="PennyMomentum Entry Conditions",
                )
                response = llm.invoke(prompt)
                raw_text = (
                    response.content
                    if hasattr(response, "content")
                    else str(response)
                )

                conditions = self._parse_json_response(raw_text, expected_type=dict)
                if not conditions:
                    self.logger.warning(
                        f"Failed to parse entry conditions for {symbol}"
                    )
                    continue

                # Validate conditions
                is_valid, errors = validate_condition_set(conditions)
                if not is_valid:
                    self.logger.warning(
                        f"Invalid conditions for {symbol}: {errors}"
                    )
                    continue

                monitored[symbol] = {
                    "status": "watching",
                    "entry_conditions": conditions.get("entry", {}),
                    "exit_conditions": {
                        "stop_loss": conditions.get("stop_loss", {}),
                        "take_profit": conditions.get("take_profit", []),
                    },
                    "confidence": finalist.get("confidence", 0),
                    "catalyst": finalist.get("catalyst", ""),
                    "strategy": finalist.get("strategy", ""),
                    "qty": triage_data.get("qty"),
                    "allocation": triage_data.get("allocation"),
                    "created_at": now.isoformat(),
                }

                self._save_analysis_output(
                    market_analysis,
                    provider_category="llm",
                    provider_name=entry_model,
                    name=f"entry_conditions_{symbol}",
                    output_type="json",
                    text=raw_text,
                    symbol=symbol,
                    prompt=prompt,
                )

                self.logger.info(f"Set entry conditions for {symbol}")

                from ....core.db import log_activity
                from ....core.types import ActivityLogSeverity, ActivityLogType
                log_activity(
                    severity=ActivityLogSeverity.INFO,
                    activity_type=ActivityLogType.ANALYSIS_COMPLETED,
                    description=(
                        f"PennyMomentumTrader watching {symbol}: entry conditions set "
                        f"| Confidence: {finalist.get('confidence', 0)}% "
                        f"| Catalyst: {finalist.get('catalyst', 'N/A')}"
                    ),
                    data={
                        "symbol": symbol,
                        "confidence": finalist.get("confidence", 0),
                        "catalyst": finalist.get("catalyst", ""),
                        "strategy": finalist.get("strategy", ""),
                    },
                    source_expert_id=self.instance.id,
                )

            except Exception as e:
                self.logger.error(
                    f"Failed to set conditions for {symbol}: {e}", exc_info=True
                )

        self._update_state(market_analysis, {"monitored_symbols": monitored})

    def _phase_5_monitor(self, market_analysis: MarketAnalysis):
        """Monitor conditions and execute trades until market close."""
        self.logger.info("Phase 5: Monitoring conditions")

        interval = self.get_setting_with_interface_default(
            "monitoring_interval_seconds", log_warning=False
        )
        market_tz_str = self.get_setting_with_interface_default(
            "market_timezone", log_warning=False
        )

        # Set up OHLCV provider for condition evaluation
        ohlcv_vendor_list = self.get_setting_with_interface_default(
            "vendor_ohlcv", log_warning=False
        )
        ohlcv_vendor = ohlcv_vendor_list[0] if ohlcv_vendor_list else "yfinance"

        from ....modules.dataproviders import get_provider

        ohlcv_provider = get_provider("ohlcv", ohlcv_vendor)

        trade_mgr = self._trade_mgr
        monitor_tick = 0

        while not self._stop_event.is_set():
            # Check if market is still open
            if not self._is_market_open():
                self.logger.info("Market closed, exiting monitor loop")
                break

            # Reload monitored symbols from state
            with get_db() as session:
                ma = session.get(MarketAnalysis, market_analysis.id)
                if ma and ma.state:
                    monitored = dict(ma.state.get("monitored_symbols", {}))
                else:
                    monitored = {}

            evaluator = ConditionEvaluator(
                ohlcv_provider, market_timezone=market_tz_str
            )

            open_positions = trade_mgr.get_open_positions()
            open_position_symbols = {p["symbol"] for p in open_positions}

            active_symbols = [s for s, i in monitored.items() if i.get("status") in ("watching", "triggered")]
            # Log a summary every 10 ticks to avoid spam
            monitor_tick += 1
            if monitor_tick % 10 == 1:
                self.logger.debug(
                    f"Monitor tick {monitor_tick}: {len(active_symbols)} active symbol(s): "
                    f"{active_symbols} | open positions: {list(open_position_symbols)}"
                )

            # Batch-fetch live prices for all active symbols in one call
            live_prices = self._get_live_prices(active_symbols) if active_symbols else {}

            for symbol, info in list(monitored.items()):
                if self._stop_event.is_set():
                    break

                status = info.get("status", "")
                if status not in ("watching", "triggered"):
                    continue

                evaluator.clear_cache()

                try:
                    # Use batch-fetched price
                    current_price = live_prices.get(symbol)
                    if current_price is not None:
                        info["last_price"] = current_price
                    info["last_checked"] = datetime.now(timezone.utc).isoformat()

                    if symbol in open_position_symbols:
                        # Check exit conditions for open positions
                        pos = next(
                            p for p in open_positions if p["symbol"] == symbol
                        )
                        entry_price = pos["entry_price"]
                        info["entry_price"] = entry_price

                        exit_conds = info.get("exit_conditions", {})

                        # Hard EOD exit for intraday strategies
                        if info.get("strategy") == "intraday":
                            market_close = self._get_market_close_today()
                            market_now = self._get_market_now()
                            minutes_to_close = (market_close - market_now).total_seconds() / 60
                            if minutes_to_close <= 15:
                                self.logger.info(
                                    f"Intraday EOD hard-exit for {symbol} "
                                    f"({minutes_to_close:.0f}m to close)"
                                )
                                trade_mgr.execute_exit(
                                    symbol, exit_pct=100.0, reason="intraday EOD hard-exit"
                                )
                                info["status"] = "closed"
                                self._record_trade(
                                    market_analysis, symbol, "exit", "intraday EOD hard-exit"
                                )
                                continue

                        # Check stop loss
                        stop_loss = exit_conds.get("stop_loss")
                        if stop_loss and evaluator.evaluate(
                            stop_loss, symbol, entry_price=entry_price
                        ):
                            self.logger.info(
                                f"Stop loss triggered for {symbol}"
                            )
                            trade_mgr.execute_exit(
                                symbol, exit_pct=100.0, reason="stop loss triggered"
                            )
                            info["status"] = "closed"
                            self._record_trade(
                                market_analysis, symbol, "exit", "stop loss"
                            )
                            continue

                        # Check take profit tiers (skip already-triggered tiers)
                        take_profit = exit_conds.get("take_profit", [])
                        triggered_tiers = info.get("triggered_tp_tiers", [])
                        for tier_idx, tp_tier in enumerate(take_profit):
                            if tier_idx in triggered_tiers:
                                continue
                            if not isinstance(tp_tier, dict):
                                continue
                            tp_condition = tp_tier.get("condition")
                            tp_exit_pct = tp_tier.get("exit_pct", 100.0)
                            if tp_condition and evaluator.evaluate(
                                tp_condition, symbol, entry_price=entry_price
                            ):
                                self.logger.info(
                                    f"Take profit tier {tier_idx + 1} triggered for {symbol} "
                                    f"(exit {tp_exit_pct}%)"
                                )
                                trade_mgr.execute_exit(
                                    symbol,
                                    exit_pct=tp_exit_pct,
                                    reason=f"take profit tier {tier_idx + 1} ({tp_exit_pct}%)",
                                )
                                triggered_tiers.append(tier_idx)
                                info["triggered_tp_tiers"] = triggered_tiers
                                self._record_trade(
                                    market_analysis,
                                    symbol,
                                    "partial_exit" if tp_exit_pct < 100 else "exit",
                                    f"take profit tier {tier_idx + 1} ({tp_exit_pct}%)",
                                )
                                if tp_exit_pct >= 100:
                                    info["status"] = "closed"
                                break

                        # Update condition status for UI
                        entry_conds = info.get("entry_conditions", {})
                        if entry_conds:
                            info["conditions_last_eval"] = (
                                evaluator.get_condition_status(
                                    entry_conds, symbol, entry_price
                                )
                            )

                    elif status == "watching":
                        # Check entry conditions for watched symbols
                        entry_conds = info.get("entry_conditions", {})
                        if not entry_conds:
                            self.logger.debug(f"No entry_conditions stored for {symbol}, skipping")
                        else:
                            # Collect per-condition status for UI and debug logging
                            cond_status = evaluator.get_condition_status(entry_conds, symbol)
                            info["conditions_last_eval"] = cond_status
                            if monitor_tick % 10 == 1:
                                details = evaluator.get_condition_details(entry_conds, symbol)
                                met_parts = [v for k, v in details.items() if cond_status.get(k)]
                                unmet_parts = [v for k, v in details.items() if not cond_status.get(k)]
                                self.logger.debug(
                                    f"{symbol} conditions\n"
                                    f"  MET:   {met_parts}\n"
                                    f"  UNMET: {unmet_parts}"
                                )
                            # Use evaluate() to respect all/any composite logic
                            if evaluator.evaluate(entry_conds, symbol):
                                self.logger.info(f"Entry conditions met for {symbol}")
                                qty = info.get("qty", 0)
                                if qty and qty > 0:
                                    order_id = trade_mgr.execute_entry(
                                        symbol=symbol,
                                        qty=qty,
                                        confidence=info.get("confidence", 50),
                                        catalyst=info.get("catalyst", ""),
                                        strategy=info.get("strategy", "swing"),
                                        exit_conditions=info.get("exit_conditions"),
                                        market_analysis_id=market_analysis.id,
                                    )
                                    if order_id:
                                        info["status"] = "triggered"
                                        self._record_trade(
                                            market_analysis,
                                            symbol,
                                            "entry",
                                            info.get("catalyst", ""),
                                        )

                except Exception as e:
                    self.logger.error(
                        f"Error monitoring {symbol}: {e}", exc_info=True
                    )

            # Periodically re-evaluate exit conditions for open positions via LLM
            exit_update_interval = int(self.get_setting_with_interface_default(
                "exit_update_interval_ticks", log_warning=False
            ))
            if (
                exit_update_interval > 0
                and monitor_tick % exit_update_interval == 0
                and open_positions
            ):
                self._update_exit_conditions_via_llm(
                    monitored, open_position_symbols, market_analysis
                )

            # Persist updated monitored state
            self._update_state(market_analysis, {"monitored_symbols": monitored})

            # Wait for next interval
            if self._stop_event.wait(timeout=interval):
                break

    def _phase_6_eod(self, market_analysis: MarketAnalysis):
        """End-of-day wrap-up: mark analysis complete, update state."""
        self.logger.info("Phase 6: EOD wrap-up")

        with get_db() as session:
            ma = session.get(MarketAnalysis, market_analysis.id)
            if ma:
                from sqlalchemy.orm import attributes
                ma.status = MarketAnalysisStatus.COMPLETED
                state = ma.state or {}
                state["phase"] = "complete"
                state["completed_at"] = datetime.now(timezone.utc).isoformat()
                ma.state = state
                attributes.flag_modified(ma, "state")
                session.add(ma)
                session.commit()
                market_analysis.state = state
                market_analysis.status = MarketAnalysisStatus.COMPLETED

        self.logger.info("Pipeline completed")

    # ------------------------------------------------------------------
    # Live exit condition updates
    # ------------------------------------------------------------------

    def _update_exit_conditions_via_llm(
        self,
        monitored: Dict[str, Dict[str, Any]],
        open_position_symbols: set,
        market_analysis: MarketAnalysis,
    ):
        """
        For each open position, fetch fresh news and ask the LLM whether
        exit conditions should be adjusted (tighten stops, widen TP, etc.).
        """
        exit_model = self.get_setting_with_interface_default(
            "exit_update_llm", log_warning=False
        )

        for symbol in list(open_position_symbols):
            if self._stop_event.is_set():
                break

            info = monitored.get(symbol)
            if not info or info.get("status") not in ("triggered", "watching"):
                continue

            exit_conds = info.get("exit_conditions")
            if not exit_conds:
                continue

            try:
                self.logger.info(f"Re-evaluating exit conditions for {symbol}")

                # Gather fresh news + social (lightweight check)
                news_text = self._gather_news(symbol)
                social_text = self._gather_social(symbol)
                new_data = f"LATEST NEWS:\n{news_text}\n\nSOCIAL SENTIMENT:\n{social_text}"

                prompt = build_exit_update_prompt(
                    symbol=symbol,
                    current_conditions=exit_conds,
                    new_data=new_data,
                )

                llm = ModelFactory.create_llm(
                    exit_model,
                    temperature=0.3,
                    expert_instance_id=self.instance.id,
                    use_case="PennyMomentum Exit Update",
                )
                response = llm.invoke(prompt)
                raw_text = response.content if hasattr(response, "content") else str(response)

                # Check for NO_CHANGE
                if raw_text.strip().strip('"') == "NO_CHANGE":
                    self.logger.debug(f"Exit conditions unchanged for {symbol}")
                    continue

                updated = self._parse_json_response(raw_text, expected_type=dict)
                if not updated:
                    self.logger.warning(f"Failed to parse exit update for {symbol}")
                    continue

                # Validate the updated conditions
                validation_set = {"stop_loss": updated.get("stop_loss"), "take_profit": updated.get("take_profit")}
                is_valid, errors = validate_condition_set(
                    {k: v for k, v in validation_set.items() if v is not None}
                )
                if not is_valid:
                    self.logger.warning(f"Invalid updated exit conditions for {symbol}: {errors}")
                    continue

                # Apply updates
                if "stop_loss" in updated:
                    info["exit_conditions"]["stop_loss"] = updated["stop_loss"]
                if "take_profit" in updated:
                    info["exit_conditions"]["take_profit"] = updated["take_profit"]
                    # Reset triggered tiers since conditions changed
                    info["triggered_tp_tiers"] = []

                self.logger.info(f"Exit conditions updated for {symbol}")

                self._save_analysis_output(
                    market_analysis,
                    provider_category="llm",
                    provider_name=exit_model,
                    name=f"exit_update_{symbol}_{datetime.now(timezone.utc).strftime('%H%M')}",
                    output_type="json",
                    text=raw_text,
                    symbol=symbol,
                    prompt=prompt,
                )

                from ....core.db import log_activity
                from ....core.types import ActivityLogSeverity, ActivityLogType
                log_activity(
                    severity=ActivityLogSeverity.INFO,
                    activity_type=ActivityLogType.ANALYSIS_COMPLETED,
                    description=f"PennyMomentumTrader updated exit conditions for {symbol} based on new market data",
                    data={"symbol": symbol, "updated_keys": list(updated.keys())},
                    source_expert_id=self.instance.id,
                )

            except Exception as e:
                self.logger.error(f"Exit condition update failed for {symbol}: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Screener enrichment helpers
    # ------------------------------------------------------------------

    def _fetch_gainers(self) -> List[Dict[str, Any]]:
        """Fetch today's top gainers from FMP /api/v3/stock_market/gainers."""
        import requests
        from ....config import get_app_setting

        api_key = get_app_setting("FMP_API_KEY")
        if not api_key:
            return []
        resp = requests.get(
            "https://financialmodelingprep.com/api/v3/stock_market/gainers",
            params={"apikey": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def _fetch_quotes_chunked(
        self, symbols: List[str], chunk_size: int = 50
    ) -> Dict[str, Dict[str, Any]]:
        """Batch-fetch FMP full quotes in chunks. Returns {symbol: quote_dict}."""
        import fmpsdk
        from ....config import get_app_setting

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
                self.logger.warning(f"FMP quote chunk {i}-{i+len(chunk)} failed: {e}")
        self.logger.debug(f"Fetched FMP quotes for {len(result)}/{len(symbols)} symbols")
        return result

    # ------------------------------------------------------------------
    # Live price helpers
    # ------------------------------------------------------------------

    def _get_live_prices(self, symbols: List[str]) -> Dict[str, Optional[float]]:
        """
        Batch-fetch live prices for the given symbols using the configured
        vendor_live_price source.

        Returns:
            Dict mapping symbol -> price (None if unavailable).
        """
        if not symbols:
            return {}

        source = self.get_setting_with_interface_default(
            "vendor_live_price", log_warning=False
        )

        if source == "fmp":
            return self._get_fmp_quotes(symbols)

        # Fallback: use account (broker) API
        from ....core.utils import get_account_instance_from_id
        account = get_account_instance_from_id(self.instance.account_id)
        return account.get_instrument_current_price(symbols, price_type="mid")

    def _is_regular_session(self) -> bool:
        """Return True if current time is within regular market hours (9:30-16:00 ET)."""
        now = self._get_market_now()
        regular_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        regular_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return regular_open <= now <= regular_close

    def _get_fmp_quotes(self, symbols: List[str]) -> Dict[str, Optional[float]]:
        """
        Fetch real-time quotes from FMP.

        During regular market hours (9:30-16:00 ET): uses fmpsdk.quote()
        which returns the current price in a single batch call.

        During extended hours (pre-market / after-hours), tries in order:
          1. /stable/batch-aftermarket-quote  (one call, Premium+ plans)
          2. /stable/aftermarket-quote         (per-symbol, Starter+ plans)
          3. fmpsdk.quote()                    (final fallback)
        """
        import fmpsdk
        from ....config import get_app_setting

        api_key = get_app_setting("FMP_API_KEY")
        if not api_key:
            self.logger.warning("FMP_API_KEY not configured, falling back to account prices")
            from ....core.utils import get_account_instance_from_id
            account = get_account_instance_from_id(self.instance.account_id)
            return account.get_instrument_current_price(symbols, price_type="mid")

        result: Dict[str, Optional[float]] = {s: None for s in symbols}

        try:
            if not self._is_regular_session():
                result = self._get_fmp_aftermarket_quotes(symbols, api_key)
                if any(v is not None for v in result.values()):
                    return result
                self.logger.warning(
                    "FMP aftermarket quotes unavailable during extended hours, "
                    "falling back to regular quote (prices may be stale closing prices)"
                )

            # Regular session or aftermarket fallback: use fmpsdk.quote()
            data = fmpsdk.quote(apikey=api_key, symbol=symbols)
            if isinstance(data, list):
                for item in data:
                    sym = item.get("symbol", "").upper()
                    price = item.get("price")
                    if sym in result and price is not None and price > 0:
                        result[sym] = float(price)
        except Exception as e:
            self.logger.warning(f"FMP quote fetch failed: {e}")

        return result

    def _get_fmp_aftermarket_quotes(
        self, symbols: List[str], api_key: str
    ) -> Dict[str, Optional[float]]:
        """
        Fetch extended-hours quotes from FMP aftermarket endpoints.

        Tries batch endpoint first (/stable/batch-aftermarket-quote),
        falls back to single-symbol endpoint (/stable/aftermarket-quote)
        if the batch returns 402 (plan limitation).

        Returns mid-price computed from bid/ask.
        Returns all-None dict if both endpoints are unavailable.
        """
        import requests

        result: Dict[str, Optional[float]] = {s: None for s in symbols}

        # 1. Try batch endpoint (Premium+ plans)
        try:
            resp = requests.get(
                "https://financialmodelingprep.com/stable/batch-aftermarket-quote",
                params={"symbol": ",".join(symbols), "apikey": api_key},
                timeout=10,
            )
            if resp.status_code != 402:
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    for item in data:
                        sym = item.get("symbol", "").upper()
                        price = self._extract_aftermarket_price(item)
                        if sym in result and price is not None:
                            result[sym] = price
                    return result
            # 402 = plan doesn't include batch, fall through to single
            self.logger.debug("FMP batch-aftermarket-quote not available (402), trying single endpoint")
        except Exception as e:
            self.logger.debug(f"FMP batch aftermarket failed: {e}")

        # 2. Fall back to single-symbol endpoint (Starter+ plans)
        for symbol in symbols:
            try:
                resp = requests.get(
                    "https://financialmodelingprep.com/stable/aftermarket-quote",
                    params={"symbol": symbol, "apikey": api_key},
                    timeout=10,
                )
                if resp.status_code == 402:
                    self.logger.debug("FMP aftermarket-quote not available (402), falling back to regular quote")
                    return {s: None for s in symbols}
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list) and data:
                    price = self._extract_aftermarket_price(data[0])
                    if price is not None:
                        result[symbol] = price
            except Exception as e:
                self.logger.debug(f"FMP aftermarket quote failed for {symbol}: {e}")

        return result

    @staticmethod
    def _extract_aftermarket_price(item: Dict[str, Any]) -> Optional[float]:
        """Compute mid-price from aftermarket bid/ask, or use price field."""
        bid = item.get("bidPrice")
        ask = item.get("askPrice")
        if bid and ask and float(bid) > 0 and float(ask) > 0:
            return round((float(bid) + float(ask)) / 2, 4)
        if bid and float(bid) > 0:
            return float(bid)
        if ask and float(ask) > 0:
            return float(ask)
        # Some responses may include a direct price field
        price = item.get("price")
        if price and float(price) > 0:
            return float(price)
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_market_analysis(self) -> MarketAnalysis:
        """Create a new MarketAnalysis record for this pipeline run."""
        ma = MarketAnalysis(
            symbol="PENNY_SCAN",
            expert_instance_id=self.instance.id,
            status=MarketAnalysisStatus.RUNNING,
            state={},
        )
        add_instance(ma, expunge_after_flush=True)
        return ma

    def _update_state(self, market_analysis: MarketAnalysis, updates: Dict[str, Any]):
        """Merge updates into the market_analysis.state and persist."""
        from sqlalchemy.orm import attributes
        with get_db() as session:
            ma = session.get(MarketAnalysis, market_analysis.id)
            if ma:
                state = ma.state or {}
                state.update(updates)
                ma.state = state
                attributes.flag_modified(ma, "state")
                session.add(ma)
                session.commit()
                market_analysis.state = state

    def _get_idle_status(self) -> Optional[str]:
        """Evaluate conditions every 15-min tick and surface results in the countdown log."""
        try:
            # Refresh condition evaluation so state stays current during idle periods
            self.evaluate_conditions_now()

            from sqlmodel import select as sql_select
            with get_db() as session:
                statement = (
                    sql_select(MarketAnalysis)
                    .where(MarketAnalysis.expert_instance_id == self.instance.id)
                    .order_by(MarketAnalysis.created_at.desc())  # type: ignore[union-attr]
                    .limit(1)
                )
                ma = session.exec(statement).first()
                if not ma or not ma.state:
                    return None
                monitored = ma.state.get("monitored_symbols", {})

            watching = {s: i for s, i in monitored.items() if i.get("status") == "watching"}
            if not watching:
                return None

            max_age = int(self.get_setting_with_interface_default(
                "max_entry_age_days", log_warning=False
            ))
            now = datetime.now(timezone.utc)

            parts = []
            for sym, info in watching.items():
                # Conditions summary
                eval_result = info.get("conditions_last_eval")
                if eval_result:
                    met = sum(1 for v in eval_result.values() if v is True)
                    total = len(eval_result)
                    cond_str = f"{met}/{total}"
                else:
                    cond_str = "?"

                # Age / time remaining
                age_str = ""
                created_str = info.get("created_at")
                if created_str:
                    try:
                        created = datetime.fromisoformat(created_str)
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        age_days = (now - created).days
                        days_left = max_age - age_days
                        expire_date = (created + timedelta(days=max_age)).strftime("%m/%d")
                        age_str = f", day {age_days + 1}/{max_age}, exp {expire_date}"
                    except (ValueError, TypeError):
                        pass

                parts.append(f"{sym} ({cond_str}{age_str})")
            return f"watching: {', '.join(parts)}"
        except Exception:
            return None

    def evaluate_conditions_now(self) -> str:
        """
        Manually evaluate entry conditions for all watched/triggered symbols and
        persist results back to the MarketAnalysis state. Called via expert actions.
        """
        try:
            from sqlmodel import select as sql_select
            from sqlalchemy.orm import attributes

            # Find the most recent MarketAnalysis for this expert
            with get_db() as session:
                statement = (
                    sql_select(MarketAnalysis)
                    .where(MarketAnalysis.expert_instance_id == self.instance.id)
                    .order_by(MarketAnalysis.created_at.desc())  # type: ignore[union-attr]
                    .limit(1)
                )
                ma = session.exec(statement).first()
                if not ma or not ma.state:
                    return "No MarketAnalysis found"
                ma_id = ma.id
                monitored: Dict[str, Dict[str, Any]] = dict(
                    ma.state.get("monitored_symbols", {})
                )

            if not monitored:
                return "No monitored symbols"

            active_symbols = [
                s for s, i in monitored.items()
                if i.get("status") in ("watching", "triggered")
            ]
            if not active_symbols:
                return "No active symbols to evaluate"

            ohlcv_vendor_list = self.get_setting_with_interface_default(
                "vendor_ohlcv", log_warning=False
            )
            ohlcv_vendor = ohlcv_vendor_list[0] if ohlcv_vendor_list else "yfinance"
            market_tz_str = self.get_setting_with_interface_default(
                "market_timezone", log_warning=False
            )
            from ....modules.dataproviders import get_provider
            ohlcv_provider = get_provider("ohlcv", ohlcv_vendor)
            evaluator = ConditionEvaluator(ohlcv_provider, market_timezone=market_tz_str)

            live_prices = self._get_live_prices(active_symbols)
            now_iso = datetime.now(timezone.utc).isoformat()
            evaluated = 0

            for symbol in active_symbols:
                info = monitored[symbol]
                evaluator.clear_cache()
                try:
                    price = live_prices.get(symbol)
                    if price is not None:
                        info["last_price"] = price
                    info["last_checked"] = now_iso

                    entry_conds = info.get("entry_conditions", {})
                    if entry_conds:
                        status_map = evaluator.get_condition_status(
                            entry_conds, symbol, entry_price=info.get("entry_price")
                        )
                        info["conditions_last_eval"] = status_map

                    evaluated += 1
                except Exception as e:
                    self.logger.error(
                        f"evaluate_conditions_now: error for {symbol}: {e}", exc_info=True
                    )

            # Persist back
            with get_db() as session:
                ma_db = session.get(MarketAnalysis, ma_id)
                if ma_db:
                    state = ma_db.state or {}
                    state["monitored_symbols"] = monitored
                    ma_db.state = state
                    attributes.flag_modified(ma_db, "state")
                    session.add(ma_db)
                    session.commit()

            self.logger.info(f"evaluate_conditions_now: evaluated {evaluated} symbols")
            return f"Evaluated {evaluated} symbols"

        except Exception as e:
            self.logger.error(f"evaluate_conditions_now failed: {e}", exc_info=True)
            return f"Error: {e}"

    def _save_analysis_output(
        self,
        market_analysis: MarketAnalysis,
        provider_category: str,
        provider_name: str,
        name: str,
        output_type: str,
        text: str,
        symbol: str,
        prompt: Optional[str] = None,
    ):
        """Persist an AnalysisOutput record. If prompt is provided, stores both as a conversation."""
        try:
            if prompt is not None:
                # Store as conversation (prompt + response) for chat-style UI display
                text = json.dumps({"prompt": prompt, "response": text}, default=str)
                output_type = "llm_conversation"
            output = AnalysisOutput(
                market_analysis_id=market_analysis.id,
                provider_category=provider_category,
                provider_name=provider_name,
                name=name,
                type=output_type,
                text=text,
                symbol=symbol,
            )
            add_instance(output)
        except Exception as e:
            self.logger.error(f"Failed to save analysis output '{name}': {e}", exc_info=True)

    def _record_trade(
        self,
        market_analysis: MarketAnalysis,
        symbol: str,
        action: str,
        reason: str,
    ):
        """Append a trade event to executed_trades in market_analysis state."""
        with get_db() as session:
            ma = session.get(MarketAnalysis, market_analysis.id)
            if ma:
                from sqlalchemy.orm import attributes
                state = ma.state or {}
                trades = state.get("executed_trades", [])
                trades.append(
                    {
                        "symbol": symbol,
                        "action": action,
                        "reason": reason,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "status": "submitted",
                    }
                )
                state["executed_trades"] = trades
                ma.state = state
                attributes.flag_modified(ma, "state")
                session.add(ma)
                session.commit()
                market_analysis.state = state

    def _parse_json_response(
        self, raw_text: str, expected_type: type = dict
    ) -> Optional[Any]:
        """Parse a JSON response from LLM output, stripping markdown fences."""
        text = raw_text.strip()
        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```json or ```)
            lines = lines[1:]
            # Remove last line if it's ```
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            parsed = json.loads(text)
            if isinstance(parsed, expected_type):
                return parsed
            self.logger.warning(
                f"JSON response type mismatch: expected {expected_type.__name__}, "
                f"got {type(parsed).__name__}"
            )
            return None
        except json.JSONDecodeError as e:
            self.logger.warning(f"Failed to parse JSON response: {e}")
            self.logger.debug(f"Raw response: {raw_text[:500]}")
            return None

    # ------------------------------------------------------------------
    # Data gathering helpers
    # ------------------------------------------------------------------

    def _gather_news(self, symbol: str) -> str:
        """Aggregate news from all configured news vendors."""
        vendor_list = self.get_setting_with_interface_default(
            "vendor_news", log_warning=False
        )
        from ....modules.dataproviders import get_provider

        all_news: List[str] = []
        for vendor_name in vendor_list:
            try:
                kwargs = {}
                if vendor_name == "ai":
                    kwargs["model"] = self.get_setting_with_interface_default(
                        "websearch_llm", log_warning=False
                    )
                provider = get_provider("news", vendor_name, **kwargs)
                news = provider.get_company_news(
                    symbol,
                    end_date=datetime.now(timezone.utc),
                    lookback_days=3,
                    format_type="markdown",
                )
                all_news.append(f"--- {vendor_name} ---\n{news}")
            except Exception as e:
                self.logger.warning(
                    f"News provider {vendor_name} failed for {symbol}: {e}"
                )
        return "\n\n".join(all_news) if all_news else "No news data available."

    def _gather_fundamentals(self, symbol: str) -> str:
        """Get fundamentals from configured vendors."""
        vendor_list = self.get_setting_with_interface_default(
            "vendor_fundamentals", log_warning=False
        )
        from ....modules.dataproviders import get_provider

        for vendor_name in vendor_list:
            try:
                kwargs = {}
                if vendor_name == "ai":
                    kwargs["model"] = self.get_setting_with_interface_default(
                        "websearch_llm", log_warning=False
                    )
                provider = get_provider("fundamentals_overview", vendor_name, **kwargs)
                return provider.get_fundamentals_overview(
                    symbol,
                    as_of_date=datetime.now(timezone.utc),
                    format_type="markdown",
                )
            except Exception as e:
                self.logger.warning(
                    f"Fundamentals provider {vendor_name} failed for {symbol}: {e}"
                )
        return "No fundamentals data available."

    def _gather_insider(self, symbol: str) -> str:
        """Get insider trading data from configured vendors."""
        vendor_list = self.get_setting_with_interface_default(
            "vendor_insider", log_warning=False
        )
        from ....modules.dataproviders import get_provider

        for vendor_name in vendor_list:
            try:
                provider = get_provider("insider", vendor_name)
                return provider.get_insider_transactions(
                    symbol,
                    end_date=datetime.now(timezone.utc),
                    lookback_days=90,
                    format_type="markdown",
                )
            except Exception as e:
                self.logger.warning(
                    f"Insider provider {vendor_name} failed for {symbol}: {e}"
                )
        return "No insider data available."

    def _gather_social(self, symbol: str) -> str:
        """Get social sentiment from configured vendors (StockTwits and/or websearch LLM)."""
        vendor_social = self.get_setting_with_interface_default(
            "vendor_social", log_warning=False
        ) or []

        parts: List[str] = []

        if "stocktwits" in vendor_social:
            try:
                provider = self._get_stocktwits_provider()
                result = provider.get_social_media_sentiment(
                    symbol,
                    end_date=datetime.now(timezone.utc),
                    lookback_days=3,
                    format_type="markdown",
                )
                parts.append(f"--- StockTwits ---\n{result}")
            except Exception as e:
                self.logger.warning(f"StockTwits failed for {symbol}: {e}")

        if "websearch" in vendor_social:
            websearch_model = self.get_setting_with_interface_default(
                "websearch_llm", log_warning=False
            )
            try:
                llm = ModelFactory.create_llm(
                    websearch_model,
                    temperature=0.3,
                    expert_instance_id=self.instance.id,
                    use_case="PennyMomentum Social Sentiment",
                )
                prompt = (
                    f"Search for the latest social media sentiment and discussion "
                    f"about {symbol} stock on Reddit, StockTwits, Twitter/X, and "
                    f"financial forums. Summarize the overall sentiment (bullish, "
                    f"bearish, neutral), key themes being discussed, and any "
                    f"notable trends in retail interest. Be concise."
                )
                response = llm.invoke(prompt)
                text = response.content if hasattr(response, "content") else str(response)
                parts.append(f"--- AI Websearch ---\n{text}")
            except Exception as e:
                self.logger.warning(f"Websearch social sentiment failed for {symbol}: {e}")

        return "\n\n".join(parts) if parts else "No social sentiment data available."

    def _get_stocktwits_provider(self):
        """Return a cached StockTwits provider instance (lazy init)."""
        if not hasattr(self, "_stocktwits_provider") or self._stocktwits_provider is None:
            from ....modules.dataproviders.socialmedia import StockTwitsSentiment
            self._stocktwits_provider = StockTwitsSentiment(message_limit=30)
        return self._stocktwits_provider

    def _enrich_with_stocktwits(
        self, candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Batch-fetch StockTwits metadata for all candidates and inject fields:
        st_watchlist, st_bullish_pct, st_bearish_pct, st_trending, st_trending_score.

        On failure the candidate is included unenriched so the pipeline continues.
        """
        provider = self._get_stocktwits_provider()
        self.logger.info(
            f"Fetching StockTwits data for {len(candidates)} candidates..."
        )

        enriched = []
        total_candidates = len(candidates)
        for i, candidate in enumerate(candidates):
            symbol = candidate.get("symbol")
            if not symbol:
                enriched.append(candidate)
                continue
            self.logger.debug(f"Phase 2: StockTwits {i + 1}/{total_candidates} - {symbol}")
            try:
                result = provider.get_social_media_sentiment(
                    symbol,
                    end_date=datetime.now(timezone.utc),
                    lookback_days=1,
                    format_type="dict",
                )
                meta = result.get("metadata", {})
                sent = result.get("sentiment", {})
                enriched_candidate = dict(candidate)
                enriched_candidate["st_watchlist"] = meta.get("watchlist_count")
                enriched_candidate["st_bullish_pct"] = sent.get("bullish_pct")
                enriched_candidate["st_bearish_pct"] = sent.get("bearish_pct")
                enriched_candidate["st_trending"] = meta.get("trending")
                enriched_candidate["st_trending_score"] = meta.get("trending_score")
                enriched.append(enriched_candidate)
            except Exception as e:
                self.logger.warning(
                    f"StockTwits enrichment failed for {symbol}: {e}"
                )
                enriched.append(candidate)

            # Small delay between requests to avoid rate limiting
            if i < len(candidates) - 1:
                time.sleep(0.2)

        fetched = sum(1 for c in enriched if c.get("st_watchlist") is not None)
        failed = len(candidates) - fetched
        if failed:
            self.logger.warning(
                f"StockTwits enrichment: {fetched}/{len(candidates)} fetched, "
                f"{failed} failed (will proceed without their social data)"
            )
        else:
            self.logger.info(
                f"StockTwits enrichment complete: {fetched}/{len(candidates)} fetched"
            )
        return enriched
