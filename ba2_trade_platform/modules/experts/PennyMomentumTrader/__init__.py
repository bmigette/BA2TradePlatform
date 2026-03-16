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
                "default": 20,
                "description": "Maximum survivors from quick filter",
                "tooltip": "How many candidates the quick-filter LLM should keep.",
            },
            "max_final_candidates": {
                "type": "int",
                "required": True,
                "default": 10,
                "description": "Maximum finalists from deep triage",
                "tooltip": "How many survivors to perform deep triage on.",
            },
            "max_monitored_symbols": {
                "type": "int",
                "required": True,
                "default": 20,
                "description": "Maximum symbols to monitor simultaneously",
                "tooltip": "Upper bound on the number of symbols being actively monitored.",
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
                "default": 30,
                "description": "Maximum days to hold a position before forced exit",
                "tooltip": "Positions held longer than this are closed automatically.",
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
            "vendor_ohlcv": {
                "type": "list",
                "required": True,
                "default": ["yfinance"],
                "description": "Data vendor(s) for OHLCV price data",
                "valid_values": ["yfinance", "alpaca", "alphavantage", "fmp"],
                "multiple": True,
                "tooltip": "OHLCV data providers used for condition monitoring.",
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
            # Phase 1: Screen
            self._current_phase = "screen"
            self._update_state(market_analysis, {"phase": "screen"})
            candidates = self._phase_1_screen(market_analysis)
            self.logger.debug(f"Phase 1 complete: {len(candidates)} tradeable candidates")
            if self._stop_event.is_set():
                self.logger.info("Pipeline aborted after phase 1 (stop requested)")
                return

            # Phase 2: Quick filter via LLM
            self._current_phase = "quick_filter"
            self._update_state(market_analysis, {"phase": "quick_filter"})
            survivors = self._phase_2_quick_filter(candidates, market_analysis)
            self.logger.debug(f"Phase 2 complete: {len(survivors)} survivors")
            if self._stop_event.is_set():
                self.logger.info("Pipeline aborted after phase 2 (stop requested)")
                return

            # Phase 3: Deep triage via LLM
            self._current_phase = "deep_triage"
            self._update_state(market_analysis, {"phase": "deep_triage"})
            finalists = self._phase_3_deep_triage(survivors, market_analysis)
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
        trade_mgr = PennyTradeManager(self.instance.id)
        open_positions = trade_mgr.get_open_positions()

        self.logger.info(f"Found {len(open_positions)} open positions")

        # Check for positions exceeding max holding days
        max_holding = self.get_setting_with_interface_default(
            "max_holding_days", log_warning=False
        )
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
            "limit": self.get_setting_with_interface_default(
                "max_scan_candidates", log_warning=False
            ),
        }

        candidates = screener.screen_stocks(filters)
        self.logger.info(f"Screener returned {len(candidates)} candidates")

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

    def _phase_2_quick_filter(
        self, candidates: List[Dict[str, Any]], market_analysis: MarketAnalysis
    ) -> List[Dict[str, Any]]:
        """Quick-filter candidates via fast LLM."""
        self.logger.info(f"Phase 2: Quick filtering {len(candidates)} candidates")

        if not candidates:
            self.logger.debug("Phase 2: no candidates, skipping LLM call")
            self._update_state(market_analysis, {"quick_filter_survivors": []})
            return []

        max_survivors = self.get_setting_with_interface_default(
            "max_quick_filter_candidates", log_warning=False
        )
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

        max_final = self.get_setting_with_interface_default(
            "max_final_candidates", log_warning=False
        )
        deep_model = self.get_setting_with_interface_default(
            "deep_analysis_llm", log_warning=False
        )

        deep_triage_results: Dict[str, Dict[str, Any]] = {}
        finalists: List[Dict[str, Any]] = []

        for candidate in survivors[:max_final]:
            if self._stop_event.is_set():
                break

            symbol = candidate.get("symbol")
            if not symbol:
                continue

            self.logger.info(f"Deep triage: analyzing {symbol}")

            # Gather data from all configured vendors
            self.logger.debug(f"Phase 3 [{symbol}]: gathering news")
            news_text = self._gather_news(symbol)
            self.logger.debug(f"Phase 3 [{symbol}]: gathering fundamentals")
            fundamentals_text = self._gather_fundamentals(symbol)
            self.logger.debug(f"Phase 3 [{symbol}]: gathering insider data")
            insider_text = self._gather_insider(symbol)
            self.logger.debug(f"Phase 3 [{symbol}]: gathering social sentiment")
            social_text = self._gather_social(symbol)

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
                if confidence < 40:
                    self.logger.info(
                        f"{symbol} confidence {confidence} too low, skipping"
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

        # Calculate position sizes for finalists
        if finalists:
            trade_mgr = PennyTradeManager(self.instance.id)
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
        max_monitored = self.get_setting_with_interface_default(
            "max_monitored_symbols", log_warning=False
        )

        # Expire old monitors
        max_age = self.get_setting_with_interface_default(
            "max_entry_age_days", log_warning=False
        )
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

        trade_mgr = PennyTradeManager(self.instance.id)
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

            for symbol, info in list(monitored.items()):
                if self._stop_event.is_set():
                    break

                status = info.get("status", "")
                if status not in ("watching", "triggered"):
                    continue

                evaluator.clear_cache()

                try:
                    # Get current price for display
                    from ....core.utils import get_account_instance_from_id
                    account = get_account_instance_from_id(self.instance.account_id)
                    current_price = account.get_instrument_current_price(symbol)
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

                        # Check take profit tiers
                        take_profit = exit_conds.get("take_profit", [])
                        for tp_tier in take_profit:
                            if not isinstance(tp_tier, dict):
                                continue
                            tp_condition = tp_tier.get("condition")
                            tp_exit_pct = tp_tier.get("exit_pct", 100.0)
                            if tp_condition and evaluator.evaluate(
                                tp_condition, symbol, entry_price=entry_price
                            ):
                                self.logger.info(
                                    f"Take profit triggered for {symbol} "
                                    f"(exit {tp_exit_pct}%)"
                                )
                                trade_mgr.execute_exit(
                                    symbol,
                                    exit_pct=tp_exit_pct,
                                    reason=f"take profit tier ({tp_exit_pct}%)",
                                )
                                self._record_trade(
                                    market_analysis,
                                    symbol,
                                    "partial_exit" if tp_exit_pct < 100 else "exit",
                                    f"take profit {tp_exit_pct}%",
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
        ma_id = add_instance(ma)
        ma.id = ma_id
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
                        "status": "filled",
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
        """Get social sentiment via websearch LLM."""
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
            return response.content if hasattr(response, "content") else str(response)
        except Exception as e:
            self.logger.warning(f"Social sentiment gathering failed for {symbol}: {e}")
            return "No social sentiment data available."
