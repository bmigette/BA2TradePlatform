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
from datetime import datetime, timezone
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
                "default": "NagaAI/gpt-4o-search-preview",
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
                "default": "NagaAI/gpt-4o-search-preview",
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
                "default": ["yfinance"],
                "description": "Data vendor(s) for OHLCV price data",
                "valid_values": ["yfinance", "alpaca", "alphavantage", "fmp"],
                "multiple": True,
                "tooltip": "OHLCV data providers used for condition monitoring.",
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
            "PennyMomentumTrader uses live pipeline, not scheduled analysis"
        )

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

    def _run_daily_pipeline(self):
        self.logger.info("=== Daily pipeline starting ===")
        self._trade_mgr = PennyTradeManager(self.instance.id)
        market_analysis = self._create_market_analysis()
        self.logger.debug(f"Created MarketAnalysis id={market_analysis.id}")

        # Phase 0: Review existing positions
        self._current_phase = "review"
        self._update_state(market_analysis, {"phase": "review"})
        self._phase_0_review(market_analysis)
        if self._stop_event.is_set():
            self.logger.info("Pipeline aborted after phase 0 (stop requested)")
            return

        # Check balance for new entries
        if self.has_sufficient_balance_for_entry():
            # Phase 1: Screen — no LLM, returns top-N by volume
            self._current_phase = "screen"
            self._update_state(market_analysis, {"phase": "screen"})
            screener_candidates = self._phase_1_screen(market_analysis)
            self.logger.debug(f"Phase 1 complete: {len(screener_candidates)} tradeable candidates")
            if self._stop_event.is_set():
                self.logger.info("Pipeline aborted after phase 1 (stop requested)")
                return

            # Phase 2: Quick filter via LLM — narrows screener results only
            self._current_phase = "quick_filter"
            self._update_state(market_analysis, {"phase": "quick_filter"})
            survivors = self._phase_2_quick_filter(screener_candidates, market_analysis)
            self.logger.debug(f"Phase 2 complete: {len(survivors)} survivors")
            if self._stop_event.is_set():
                self.logger.info("Pipeline aborted after phase 2 (stop requested)")
                return

            # Phase 1b: LLM discovery — finds additional symbols NOT in screener results
            self._current_phase = "discovery"
            self._update_state(market_analysis, {"phase": "discovery"})
            discovered = self._phase_1b_llm_discovery(screener_candidates, market_analysis)
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

            # Combine survivors + LLM-discovered for deep triage
            deep_triage_input = survivors + new_ones

            # Phase 3: Deep triage via LLM
            self._current_phase = "deep_triage"
            self._update_state(market_analysis, {"phase": "deep_triage"})
            finalists = self._phase_3_deep_triage(deep_triage_input, market_analysis)
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
        self._phase_4_entry_conditions(finalists, market_analysis)
        if self._stop_event.is_set():
            self.logger.info("Pipeline aborted after phase 4 (stop requested)")
            return

        # Phase 5: Monitor
        self._current_phase = "monitoring"
        self._update_state(market_analysis, {"phase": "monitoring"})
        self._phase_5_monitor(market_analysis)

        # Phase 6: EOD
        self._current_phase = "eod"
        self._update_state(market_analysis, {"phase": "eod"})
        self._phase_6_eod(market_analysis)
        self._current_phase = "complete"
        self._update_state(market_analysis, {"phase": "complete"})
        self.logger.info("=== Daily pipeline complete ===")

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
        """Screen stocks via screener provider and filter for tradability."""
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
        filters = {
            "price_min": self.get_setting_with_interface_default(
                "scan_price_min", log_warning=False
            ),
            "price_max": self.get_setting_with_interface_default(
                "scan_price_max", log_warning=False
            ),
            "volume_min": self.get_setting_with_interface_default(
                "scan_volume_min", log_warning=False
            ),
            "market_cap_min": self.get_setting_with_interface_default(
                "scan_market_cap_min", log_warning=False
            ),
            "market_cap_max": self.get_setting_with_interface_default(
                "scan_market_cap_max", log_warning=False
            ),
            "sector_exclude": sector_exclude,
        }

        candidates = screener.screen_stocks(filters)
        self.logger.info(f"Screener returned {len(candidates)} candidates")

        # Exclude symbols where we already hold open positions
        open_position_symbols = {
            pos["symbol"] for pos in self._trade_mgr.get_open_positions()
        }
        if open_position_symbols:
            before = len(candidates)
            candidates = [c for c in candidates if c.get("symbol") not in open_position_symbols]
            self.logger.debug(
                f"Excluded {before - len(candidates)} already-held symbols from screener results"
            )

        # Sort by volume descending (highest momentum first) and cap at max_scan_candidates
        candidates.sort(key=lambda c: c.get("volume") or 0, reverse=True)
        if len(candidates) > max_candidates:
            self.logger.info(
                f"Capping candidates from {len(candidates)} to top {max_candidates} by volume"
            )
            candidates = candidates[:max_candidates]

        # Filter through account to remove untradeable symbols
        if candidates:
            from ....core.utils import get_account_instance_from_id
            account = get_account_instance_from_id(self.instance.account_id)
            candidate_symbols = [c["symbol"] for c in candidates if c.get("symbol")]
            if candidate_symbols:
                tradeable = account.symbols_exist(candidate_symbols)
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

        # Save scan results
        self._update_state(market_analysis, {"scan_results": candidates})
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
            f"Find {count} US penny stocks with strong momentum catalysts RIGHT NOW.\n\n"
            f"Criteria:\n"
            f"- Price between ${price_min:.2f} and ${price_max:.2f}\n"
            f"- Volume above {volume_min:,}\n"
            f"- Clear catalyst today (earnings, FDA news, SEC filing, short squeeze, "
            f"unusual options activity, social media buzz, technical breakout)\n"
            f"- US-listed stocks only (NYSE, NASDAQ, OTC)\n\n"
            f"We are ALREADY tracking or holding these symbols — DO NOT include them:\n"
            f"{known_list}\n\n"
            f"Return ONLY a JSON array (no explanation, no markdown) of exactly {count} objects:\n"
            f'[{{"symbol": "ABCD", "price": 1.23, "volume": 1500000, '
            f'"catalyst": "short description", "reason": "why it has momentum"}}]'
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
                # Try live price; fall back to LLM-reported price
                live_price = live_prices.get(symbol)
                price = live_price if live_price and live_price > 0 else item.get("price")
                if not price or price <= 0:
                    self.logger.debug(f"Phase 1b: skipping {symbol} (no price)")
                    continue
                results.append({
                    "symbol": symbol,
                    "price": price,
                    "volume": item.get("volume"),
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
            )
            return results

        except Exception as e:
            self.logger.error(f"Phase 1b: discovery LLM failed: {e}", exc_info=True)
            return []

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

        # Parse JSON response
        survivors = self._parse_json_response(raw_text, expected_type=list) or []
        survivor_symbols = [s["symbol"] for s in survivors if isinstance(s, dict) and "symbol" in s]

        self.logger.info(f"Quick filter kept {len(survivor_symbols)} candidates")

        # Save outputs
        self._update_state(market_analysis, {"quick_filter_survivors": survivor_symbols})
        self._save_analysis_output(
            market_analysis,
            provider_category="llm",
            provider_name=scanning_model,
            name="quick_filter_response",
            output_type="json",
            text=raw_text,
            symbol="PENNY_SCAN",
        )

        # Return full candidate dicts for survivors
        survivor_set = set(survivor_symbols)
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

        # Process ALL candidates (input is already bounded by phase 1+2 limits).
        # max_final_candidates caps the OUTPUT — we take the top N by confidence.
        for candidate in survivors:
            if self._stop_event.is_set():
                break

            symbol = candidate.get("symbol")
            if not symbol:
                continue

            self.logger.info(f"Deep triage: analyzing {symbol}")

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

            # Build prompt and call LLM
            prompt = build_deep_triage_prompt(
                symbol=symbol,
                news=news_text,
                insider=insider_text,
                fundamentals=fundamentals_text,
                social=social_text,
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

                # Save per-symbol analysis output
                self._save_analysis_output(
                    market_analysis,
                    provider_category="llm",
                    provider_name=deep_model,
                    name=f"deep_triage_{symbol}",
                    output_type="json",
                    text=raw_text,
                    symbol=symbol,
                )

            except Exception as e:
                self.logger.error(
                    f"Deep triage failed for {symbol}: {e}", exc_info=True
                )

        # Cap finalists to max_final by confidence (highest confidence first)
        if len(finalists) > max_final:
            finalists.sort(key=lambda f: f.get("confidence", 0), reverse=True)
            dropped = [f["symbol"] for f in finalists[max_final:]]
            finalists = finalists[:max_final]
            self.logger.info(
                f"Capped finalists to top {max_final} by confidence "
                f"(dropped: {dropped})"
            )
            # Remove dropped symbols from deep_triage_results to keep state consistent
            for sym in dropped:
                deep_triage_results.pop(sym, None)

        # Calculate position sizes for finalists
        if finalists:
            trade_mgr = self._trade_mgr
            available = self.get_available_balance() or 0
            sizing = trade_mgr.calculate_position_sizes(finalists, available)
            for symbol, size_info in sizing.items():
                if symbol in deep_triage_results:
                    deep_triage_results[symbol]["qty"] = size_info["qty"]
                    deep_triage_results[symbol]["allocation"] = size_info["allocation"]

        self._update_state(market_analysis, {"deep_triage_results": deep_triage_results})
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

        # Load existing monitored symbols from state
        monitored: Dict[str, Dict[str, Any]] = dict(
            market_analysis.state.get("monitored_symbols", {})
        )
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
        for finalist in finalists:
            if self._stop_event.is_set():
                break

            symbol = finalist["symbol"]
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
                            info["entry_conditions_status"] = (
                                evaluator.get_condition_status(
                                    entry_conds, symbol, entry_price
                                )
                            )

                    elif status == "watching":
                        # Check entry conditions for watched symbols
                        entry_conds = info.get("entry_conditions", {})
                        if entry_conds and evaluator.evaluate(entry_conds, symbol):
                            self.logger.info(
                                f"Entry conditions met for {symbol}"
                            )
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

                        # Update condition status for UI
                        if entry_conds:
                            info["entry_conditions_status"] = (
                                evaluator.get_condition_status(entry_conds, symbol)
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
                ma.status = MarketAnalysisStatus.COMPLETED
                state = ma.state or {}
                state["phase"] = "complete"
                state["completed_at"] = datetime.now(timezone.utc).isoformat()
                ma.state = state
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
        During extended hours (pre-market / after-hours): uses
        FMP /stable/aftermarket-quote endpoint for accurate prices.
        """
        import fmpsdk
        import requests
        from ....config import get_app_setting

        api_key = get_app_setting("FMP_API_KEY")
        if not api_key:
            self.logger.warning("FMP_API_KEY not configured, falling back to account prices")
            from ....core.utils import get_account_instance_from_id
            account = get_account_instance_from_id(self.instance.account_id)
            return account.get_instrument_current_price(symbols, price_type="mid")

        result: Dict[str, Optional[float]] = {s: None for s in symbols}

        try:
            if self._is_regular_session():
                data = fmpsdk.quote(apikey=api_key, symbol=symbols)
                if isinstance(data, list):
                    for item in data:
                        sym = item.get("symbol", "").upper()
                        price = item.get("price")
                        if sym in result and price is not None and price > 0:
                            result[sym] = float(price)
            else:
                # Extended hours: use batch aftermarket-quote endpoint
                resp = requests.get(
                    "https://financialmodelingprep.com/stable/batch-aftermarket-quote",
                    params={"symbol": ",".join(symbols), "apikey": api_key},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    for item in data:
                        sym = item.get("symbol", "").upper()
                        price = item.get("price") or item.get("lastPrice")
                        if sym in result and price is not None and price > 0:
                            result[sym] = float(price)
        except Exception as e:
            self.logger.warning(f"FMP quote fetch failed: {e}")

        return result

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
        with get_db() as session:
            ma = session.get(MarketAnalysis, market_analysis.id)
            if ma:
                state = ma.state or {}
                state.update(updates)
                ma.state = state
                session.add(ma)
                session.commit()
                market_analysis.state = state

    def _save_analysis_output(
        self,
        market_analysis: MarketAnalysis,
        provider_category: str,
        provider_name: str,
        name: str,
        output_type: str,
        text: str,
        symbol: str,
    ):
        """Persist an AnalysisOutput record."""
        try:
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
                provider = get_provider("news", vendor_name)
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
                provider = get_provider("fundamentals_overview", vendor_name)
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
        for i, candidate in enumerate(candidates):
            symbol = candidate.get("symbol")
            if not symbol:
                enriched.append(candidate)
                continue
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
