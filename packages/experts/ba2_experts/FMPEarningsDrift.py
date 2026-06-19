"""
FMPEarningsDrift Expert (post-earnings-announcement drift, PEAD)

Signal: a company that just reported a meaningful positive EPS surprise tends
to keep drifting upward for the following weeks (one of the most persistent
documented anomalies). BUY when the latest quarterly report is fresh (within
``max_days_since_report``) and beat estimates by at least ``surprise_min_pct``.

The exit is time-boxed by the paired ruleset (close after the drift window),
not by this expert: it keeps emitting HOLD once the report is stale.

Data: FMPCompanyDetailsProvider.get_past_earnings (quarterly, dict format).
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.models import AnalysisOutput, ExpertRecommendation, MarketAnalysis
from ba2_common.core.db import add_instance, get_db, update_instance
from ba2_common.core.types import (
    MarketAnalysisStatus, OrderRecommendation, Recommendation, RiskLevel, TimeHorizon,
)
from ba2_common.core.backtest_context import ProviderBundle
from ba2_common.logger import get_expert_logger
from ba2_experts.expert_mixins import AnalysisStatusRenderMixin
from ba2_providers.cache.cached_get import past_earnings_get


def evaluate_earnings_drift(latest_earnings: Optional[Dict[str, Any]],
                            now: datetime,
                            surprise_min_pct: float,
                            max_days_since_report: int) -> Dict[str, Any]:
    """Pure PEAD evaluation of the latest quarterly earnings row.

    Args:
        latest_earnings: one row from get_past_earnings(...)["earnings"]
            (keys: report_date, reported_eps, estimated_eps, surprise_percent)
            or None when no earnings data exists.
        now: evaluation timestamp (tz-aware).
        surprise_min_pct: minimum EPS beat (%) to trigger.
        max_days_since_report: how fresh the report must be (calendar days).

    Returns dict: is_signal, surprise_pct, days_since_report, report_date,
    reported_eps, estimated_eps, confidence, reason.
    """
    out = {
        "is_signal": False, "surprise_pct": None, "days_since_report": None,
        "report_date": None, "reported_eps": None, "estimated_eps": None,
        "confidence": 0.0, "reason": "",
    }
    if not latest_earnings:
        out["reason"] = "no earnings data"
        return out

    report_date_str = latest_earnings.get("report_date") or latest_earnings.get("fiscal_date_ending")
    if not report_date_str:
        out["reason"] = "no report date"
        return out
    try:
        report_date = datetime.fromisoformat(str(report_date_str).split("T")[0]).replace(tzinfo=timezone.utc)
    except ValueError:
        out["reason"] = f"unparseable report date {report_date_str!r}"
        return out

    surprise_pct = latest_earnings.get("surprise_percent")
    reported = latest_earnings.get("reported_eps")
    estimated = latest_earnings.get("estimated_eps")
    if surprise_pct is None and reported is not None and estimated not in (None, 0):
        surprise_pct = (reported - estimated) / abs(estimated) * 100.0
    if surprise_pct is None:
        out["reason"] = "no surprise data"
        return out

    days_since = (now - report_date).days
    out.update({
        "surprise_pct": round(float(surprise_pct), 2),
        "days_since_report": days_since,
        "report_date": report_date.date().isoformat(),
        "reported_eps": reported,
        "estimated_eps": estimated,
    })

    if days_since < 0 or days_since > max_days_since_report:
        out["reason"] = f"report not fresh ({days_since}d > {max_days_since_report}d window)"
        return out
    if float(surprise_pct) < surprise_min_pct:
        out["reason"] = f"surprise {surprise_pct:.1f}% below {surprise_min_pct:.1f}% threshold"
        return out

    # Confidence: base 55, + up to +25 for surprise size (2 pts per surprise %),
    # + up to +10 freshness bonus (full within 3 days of the report).
    confidence = 55.0
    confidence += min(25.0, float(surprise_pct) * 2.0)
    confidence += max(0.0, 10.0 - max(0, days_since - 3) * 2.5)
    out["is_signal"] = True
    out["confidence"] = max(0.0, min(100.0, confidence))
    out["reason"] = "fresh positive earnings surprise"
    return out


class FMPEarningsDrift(AnalysisStatusRenderMixin, MarketExpertInterface):
    """Post-earnings-drift expert: BUY fresh EPS beats, time-boxed hold."""

    RENDER_PENDING_MESSAGE = 'Earnings-drift analysis for {symbol} is queued'
    RENDER_RUNNING_MESSAGE = 'Checking latest earnings surprise for {symbol}...'

    @classmethod
    def description(cls) -> str:
        return "BUY fresh positive earnings surprises (post-earnings-announcement drift)"

    def __init__(self, id: int):
        super().__init__(id)
        self._load_expert_instance(id)
        self.logger = get_expert_logger("FMPEarningsDrift", id)

    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
            "surprise_min_pct": {
                "type": "float", "required": True, "default": 5.0,
                "description": "Minimum EPS surprise (%) vs estimate to trigger a BUY",
            },
            "max_days_since_report": {
                "type": "int", "required": True, "default": 10,
                "description": "Maximum age (days) of the earnings report to still enter",
            },
            "expected_profit_percent": {
                "type": "float", "required": True, "default": 8.0,
                "description": "Expected profit %% attached to BUY recommendations",
            },
        }

    # ------------------------------------------------------------------
    # Backtest contract (Phase 1): _gather (provider I/O) + _process (pure).
    # The SAME pair runs live (run_analysis, as_of=None) and in backtest
    # (analyze_as_of, as_of=<date>). _gather threads as_of into the provider so
    # the point-in-time (no-lookahead) fetch and the live latest fetch share one
    # code path; with as_of=None the fetch is byte-identical to the live path.
    # ------------------------------------------------------------------
    _SETTING_KEYS = ("surprise_min_pct", "max_days_since_report", "expected_profit_percent")

    def _gather(self, providers: ProviderBundle, as_of: Optional[datetime]) -> Dict[str, Any]:
        symbol = self._gather_symbol
        details_provider = providers.fundamentals_details()
        data = past_earnings_get(
            details_provider, symbol, as_of=as_of,
            frequency="quarterly", lookback_periods=1, format_type="dict")
        latest = None
        if isinstance(data, dict):
            earnings = data.get("earnings") or []
            latest = earnings[0] if earnings else None
        # Live (as_of=None) reads the account/broker quote (the original live
        # source); backtest (as_of set) reads the OHLCV close-at-as_of.
        current_price = (self._get_current_price(symbol) if as_of is None
                         else providers.price_at_date(symbol, as_of))
        return {"latest_earnings": latest, "current_price": current_price, "symbol": symbol}

    def _process(self, data_bundle: Dict[str, Any], settings: Dict[str, Any],
                 as_of: Optional[datetime] = None) -> Recommendation:
        now = as_of or datetime.now(timezone.utc)
        surprise_min = float(settings["surprise_min_pct"])
        max_days = int(settings["max_days_since_report"])
        expected_profit = float(settings["expected_profit_percent"])
        result = evaluate_earnings_drift(data_bundle["latest_earnings"], now, surprise_min, max_days)

        if result["is_signal"]:
            signal, confidence, expected = OrderRecommendation.BUY, result["confidence"], expected_profit
        else:
            signal, confidence, expected = OrderRecommendation.HOLD, 10.0, 0.0

        current_price = data_bundle["current_price"]
        symbol = data_bundle["symbol"]
        # Byte-identical to the pre-refactor live `details` block (golden parity).
        details = f"""Post-Earnings-Drift Analysis for {symbol}

Latest report: {result['report_date'] or 'N/A'} ({result['days_since_report']} days ago)
Reported EPS: {result['reported_eps']}  vs estimate {result['estimated_eps']}
EPS surprise: {result['surprise_pct']}% (threshold {surprise_min}%, freshness window {max_days}d)
Verdict: {result['reason']}

Recommendation: {signal.value}
Confidence: {confidence:.1f}%
"""
        return Recommendation(
            signal=signal, confidence=round(confidence, 1), current_price=current_price,
            details=details, expected_profit_percent=expected,
            raw_outputs={
                "name": "Earnings Drift Analysis", "type": "earnings_drift_analysis",
                "text": details,
                "evaluation": {k: result[k] for k in (
                    "is_signal", "surprise_pct", "days_since_report", "report_date",
                    "reported_eps", "estimated_eps", "reason")},
            })

    # ------------------------------------------------------------------
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """Thin live orchestrator: resolve settings -> _gather(as_of=None) ->
        _process -> persist ExpertRecommendation + AnalysisOutput + state. Runs the
        EXACT same _gather/_process the backtest engine drives via analyze_as_of."""
        self.logger.info(f"Starting earnings-drift analysis for {symbol} (Analysis ID: {market_analysis.id})")
        try:
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)

            settings = self._resolve_settings(self._SETTING_KEYS)
            self._gather_symbol = symbol
            providers = self._live_providers()
            bundle = self._gather(providers, as_of=None)
            if not bundle.get("current_price"):
                raise ValueError(f"Unable to get current price for {symbol}")
            rec = self._process(bundle, settings, as_of=None)

            recommendation_id = add_instance(ExpertRecommendation(
                instance_id=self.id,
                symbol=symbol,
                recommended_action=rec.signal,
                expected_profit_percent=rec.expected_profit_percent,
                price_at_date=rec.current_price,
                details=rec.details,
                confidence=round(rec.confidence, 1),
                risk_level=RiskLevel.MEDIUM,
                time_horizon=TimeHorizon.SHORT_TERM,
                market_analysis_id=market_analysis.id,
                created_at=datetime.now(timezone.utc),
            ))

            session = get_db()
            try:
                session.add(AnalysisOutput(
                    market_analysis_id=market_analysis.id,
                    name=rec.raw_outputs["name"],
                    type=rec.raw_outputs["type"],
                    text=rec.raw_outputs["text"],
                ))
                session.commit()
            finally:
                session.close()

            market_analysis.state = {
                "earnings_drift": {
                    "recommendation": {
                        "signal": rec.signal.value,
                        "confidence": rec.confidence,
                        "expected_profit_percent": rec.expected_profit_percent,
                    },
                    "evaluation": rec.raw_outputs["evaluation"],
                    "settings": {
                        "surprise_min_pct": settings["surprise_min_pct"],
                        "max_days_since_report": settings["max_days_since_report"],
                    },
                    "expert_recommendation_id": recommendation_id,
                    "current_price": rec.current_price,
                    "analysis_timestamp": datetime.now(timezone.utc).isoformat(),
                }
            }
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            self.logger.info(
                f"Completed earnings-drift analysis for {symbol}: {rec.signal.value} "
                f"({rec.raw_outputs['evaluation']['reason']})")

        except Exception as e:
            self.logger.error(f"Earnings-drift analysis failed for {symbol}: {e}", exc_info=True)
            market_analysis.state = {
                "error": str(e),
                "error_timestamp": datetime.now(timezone.utc).isoformat(),
                "analysis_failed": True,
            }
            market_analysis.status = MarketAnalysisStatus.FAILED
            update_instance(market_analysis)
            raise

    # ------------------------------------------------------------------
    def _render_completed(self, market_analysis: MarketAnalysis) -> None:
        from nicegui import ui
        state = (market_analysis.state or {}).get("earnings_drift")
        if not state:
            with ui.card().classes('w-full p-4'):
                ui.label('No analysis data available').classes('text-grey-7')
            return
        rec = state.get("recommendation", {})
        ev = state.get("evaluation", {})
        with ui.card().classes('w-full p-4'):
            ui.label(f"Post-Earnings-Drift Analysis - {market_analysis.symbol}").classes('text-h6')
            with ui.row().classes('gap-8 mt-2'):
                ui.label(f"Signal: {rec.get('signal', 'N/A')}").classes('text-h6')
                ui.label(f"Confidence: {rec.get('confidence', 0):.1f}%")
                ui.label(f"Surprise: {ev.get('surprise_pct')}%")
                ui.label(f"Reported: {ev.get('report_date')} ({ev.get('days_since_report')}d ago)")
            ui.label(f"Verdict: {ev.get('reason', '')}").classes('text-grey-8 mt-2')
