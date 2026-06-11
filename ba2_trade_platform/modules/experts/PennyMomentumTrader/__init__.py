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
    MarketAnalysis,
)
from ....core.db import add_instance, get_db, get_instance
from ....core.types import MarketAnalysisStatus
from ....logger import get_expert_logger

from .conditions import validate_condition_set
from .prompts import build_conditions_fix_prompt
from .trade_manager import PennyTradeManager
from .settings import SETTINGS_DEFINITIONS
from .screening import ScreeningPhasesMixin
from .monitoring import MonitoringPhasesMixin
from .data_gathering import DataGatheringMixin


class PennyMomentumTrader(
    ScreeningPhasesMixin,
    MonitoringPhasesMixin,
    DataGatheringMixin,
    LiveExpertInterface,
):
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
            "required_instrument_selection_method": "expert",
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
        return dict(SETTINGS_DEFINITIONS)

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

        # In resume mode, reuse today's existing MarketAnalysis to preserve
        # monitored symbols and other state across restarts
        market_analysis = None
        if is_resume:
            market_analysis = self._get_todays_market_analysis()
            if market_analysis:
                self.logger.info(
                    f"Resuming existing MarketAnalysis id={market_analysis.id} "
                    f"(state keys: {list((market_analysis.state or {}).keys())})"
                )
                # Mark as running again
                with get_db() as session:
                    from sqlalchemy.orm import attributes
                    ma = session.get(MarketAnalysis, market_analysis.id)
                    if ma:
                        ma.status = MarketAnalysisStatus.RUNNING
                        session.add(ma)
                        session.commit()
                        market_analysis.status = MarketAnalysisStatus.RUNNING

        if market_analysis is None:
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
        # If we reused today's analysis, its own state already has monitored data
        own_monitored = (market_analysis.state or {}).get("monitored_symbols", {})
        if own_monitored:
            prev_monitored = own_monitored
            self.logger.info(
                f"Using {len(own_monitored)} monitored symbols from current "
                f"MarketAnalysis id={market_analysis.id}"
            )
        else:
            prev_monitored = self._get_previous_monitored_data(market_analysis.id)
        prev_watching = sum(
            1 for info in prev_monitored.values() if info.get("status") == "watching"
        )
        # Check if a full scan already ran today — either in the current (reused)
        # analysis or in a separate one
        own_state = market_analysis.state or {}
        scan_done_today = is_resume and (
            own_state.get("deep_triage_results") is not None
            or self._full_scan_completed_today(market_analysis.id)
        )
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

            # Phase 2b: Quick filter discovered candidates (1b + 1c) before deep triage
            if new_ones:
                self.logger.info(f"Phase 2b: Quick filtering {len(new_ones)} discovered candidates (1b+1c)")
                self._current_phase = "quick_filter_discovered"
                self._update_state(market_analysis, {"phase": "quick_filter_discovered"})
                filtered_new = self._time_phase(
                    "quick_filter_discovered", market_analysis,
                    self._phase_2_quick_filter, new_ones, market_analysis
                )
                self.logger.info(
                    f"Phase 2b: {len(filtered_new)}/{len(new_ones)} discovered candidates survived quick filter"
                )
                if self._stop_event.is_set():
                    self.logger.info("Pipeline aborted after phase 2b (stop requested)")
                    return
                new_ones = filtered_new

            # Strip discovered symbols not supported by the OHLCV provider
            # (LLM/StockTwits may surface crypto, OTC, or delisted tickers)
            if new_ones:
                before_ohlcv = len(new_ones)
                new_ones = self._filter_by_ohlcv_support(new_ones)
                if len(new_ones) < before_ohlcv:
                    self.logger.info(
                        f"OHLCV filter: {len(new_ones)}/{before_ohlcv} discovered candidates "
                        f"have OHLCV data and will proceed to deep triage"
                    )

            # Combine survivors + filtered discovered for deep triage
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

    def _get_todays_market_analysis(self) -> "MarketAnalysis | None":
        """Find an existing MarketAnalysis from today that has monitored symbols.

        Returns the most recent one with state data, or None if no suitable
        analysis exists for today.
        """
        try:
            today = datetime.utcnow().date()
            with get_db() as session:
                from sqlmodel import select as sql_select
                statement = (
                    sql_select(MarketAnalysis)
                    .where(MarketAnalysis.expert_instance_id == self.instance.id)
                    .order_by(MarketAnalysis.id.desc())
                    .limit(10)
                )
                for ma in session.exec(statement).all():
                    if not ma.created_at or ma.created_at.date() != today:
                        break
                    if ma.state and ma.state.get("monitored_symbols"):
                        # Detach from session so it can be used outside
                        session.expunge(ma)
                        return ma
        except Exception as e:
            self.logger.warning(f"Failed to find today's MarketAnalysis: {e}")
        return None

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

    def _invoke_conditions_with_retry(
        self,
        llm,
        initial_prompt: str,
        symbol: str = "",
        max_retries: int = 2,
    ) -> Optional[dict]:
        """
        Invoke an LLM to generate a condition set, retrying with targeted error
        feedback when the response fails JSON parsing or schema validation.

        On each failure the validation errors are fed back to the LLM via
        build_conditions_fix_prompt() so it can correct only the broken parts
        rather than regenerating from scratch.

        Args:
            llm: LangChain-compatible LLM instance.
            initial_prompt: The full prompt to use on the first attempt.
            symbol: Ticker symbol (used in log messages only).
            max_retries: How many correction attempts to make after the first
                         failure (default 2, so up to 3 total LLM calls).

        Returns:
            Validated conditions dict, or None if all attempts fail.
        """
        prompt = initial_prompt
        for attempt in range(max_retries + 1):
            try:
                response = llm.invoke(prompt)
                raw_text = response.content if hasattr(response, "content") else str(response)
            except Exception as e:
                self.logger.warning(f"[{symbol}] Attempt {attempt + 1}: LLM call failed: {e}")
                return None

            conditions = self._parse_json_response(raw_text, expected_type=dict)
            if not conditions:
                errors = ["Response was not valid JSON or had unexpected structure"]
                if attempt < max_retries:
                    self.logger.warning(
                        f"[{symbol}] Attempt {attempt + 1}: JSON parse failed, retrying with fix prompt"
                    )
                    prompt = build_conditions_fix_prompt(raw_text, errors)
                    continue
                self.logger.warning(f"[{symbol}] All {max_retries + 1} attempts failed to produce valid JSON")
                return None

            is_valid, errors = validate_condition_set(conditions)
            if is_valid:
                if attempt > 0:
                    self.logger.info(f"[{symbol}] Conditions fixed after {attempt + 1} attempts")
                return conditions

            if attempt < max_retries:
                self.logger.warning(
                    f"[{symbol}] Attempt {attempt + 1}: invalid conditions {errors}, retrying with fix prompt"
                )
                prompt = build_conditions_fix_prompt(raw_text, errors)
            else:
                self.logger.warning(
                    f"[{symbol}] All {max_retries + 1} attempts produced invalid conditions. "
                    f"Last errors: {errors}"
                )
                return None

        return None

    # ------------------------------------------------------------------
    # Data gathering helpers
    # ------------------------------------------------------------------

