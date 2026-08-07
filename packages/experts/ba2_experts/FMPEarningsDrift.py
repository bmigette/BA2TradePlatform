"""
FMPEarningsDrift Expert (post-earnings-announcement drift, PEAD)

Signal: a company that just reported a meaningful positive EPS surprise tends
to keep drifting upward for the following weeks (one of the most persistent
documented anomalies). BUY when the latest quarterly report is fresh (within
``max_days_since_report``) and beat estimates by at least ``surprise_min_pct``.

The exit is time-boxed by the paired ruleset (close after the drift window),
not by this expert: it keeps emitting HOLD once the report is stale.

Data: FMPCompanyDetailsProvider.get_past_earnings (quarterly, dict format).

LIVE-ONLY optimization: a screener universe scan schedules one analysis per symbol, and
today each one makes its own per-symbol ``historical_earning_calendar`` call regardless of
whether that symbol reported recently at all. ``fmpsdk.earning_calendar(from_date, to_date)``
is a market-wide, DATE-RANGE call (no symbol parameter, no batching/chunking needed) that
returns every company reporting in the window in ONE request; a module-level TTLCache shares
that single fetch across every symbol analyzed within the window. Most symbols either aren't
in the result at all (definitively no signal, zero per-symbol calls) or are, and about ~30%
of the time the calendar row already carries both actual + estimated EPS (no per-symbol call
needed either) — only the remainder falls back to the existing per-symbol detail fetch.
Backtest (``as_of`` set) is completely unchanged: same per-symbol fetch as before.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import fmpsdk

from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.models import AnalysisOutput, ExpertRecommendation, MarketAnalysis
from ba2_common.core.db import add_instance, get_db, update_instance
from ba2_common.core.types import (
    MarketAnalysisStatus, OrderRecommendation, Recommendation, RiskLevel, TimeHorizon,
)
from ba2_common.core.backtest_context import ProviderBundle
from ba2_common.logger import get_expert_logger
from ba2_experts.expert_mixins import AnalysisStatusRenderMixin
from ba2_experts.earnings_surprise import surprise_percent as _surprise_percent
from ba2_providers.cache.cached_get import past_earnings_get
from ba2_providers.fmp_common import TTLCache
from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider

# 4h TTL: long enough that every symbol in one scheduled scan cycle (all analyzed within
# minutes of each other) shares the SAME calendar fetch, short enough to pick up same-day
# report updates on the next cycle. Keyed on (from_date, to_date), so a new calendar day
# naturally busts the cache without needing explicit invalidation.
_CALENDAR_CACHE_TTL_SECONDS = 4 * 3600
_CALENDAR_CACHE = TTLCache(_CALENDAR_CACHE_TTL_SECONDS)


def _fetch_earnings_calendar_by_symbol(api_key: str, from_date: str, to_date: str) -> Dict[str, dict]:
    """Market-wide earnings calendar for [from_date, to_date], deduped to the LATEST row per
    symbol, keyed upper-case. ONE API call regardless of universe size — no symbol list, so
    no chunking is needed (unlike e.g. a batch quote/profile endpoint)."""
    def _do_fetch() -> Dict[str, dict]:
        rows = fmpsdk.earning_calendar(apikey=api_key, from_date=from_date, to_date=to_date) or []
        by_symbol: Dict[str, dict] = {}
        for row in rows:
            sym = (row.get("symbol") or "").upper()
            if not sym:
                continue
            prior = by_symbol.get(sym)
            if prior is None or (row.get("date") or "") > (prior.get("date") or ""):
                by_symbol[sym] = row
        return by_symbol
    return _CALENDAR_CACHE.get_or_call((from_date, to_date), _do_fetch)


def _calendar_row_to_latest_earnings(row: dict) -> Optional[Dict[str, Any]]:
    """Map a bulk-calendar row to the ``get_past_earnings()`` 'latest_earnings' row shape —
    ONLY when both actual and estimated EPS are present (about ~30% of calendar rows in
    practice; the rest need the per-symbol detail fetch since the bulk calendar doesn't
    always carry the analyst estimate). Returns None when the estimate is missing."""
    if row.get("eps") is None or row.get("epsEstimated") is None:
        return None
    return {
        "report_date": row.get("date"),
        "reported_eps": row.get("eps"),
        "estimated_eps": row.get("epsEstimated"),
    }


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
    if surprise_pct is None:
        # Shared with DeterministicScorer's PEAD section -- one formula, one
        # place. (Same value as the old inline expression, including the |est|
        # denominator that keeps the sign right on a negative consensus.)
        surprise_pct = _surprise_percent(reported, estimated)
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
                "description": "Base expected profit %% attached to BUY recommendations. In "
                               "'dynamic' mode this is the floor (surprise at the trigger "
                               "threshold); in 'static' mode it's the flat value for every signal.",
            },
            "expected_profit_mode": {
                "type": "str", "required": False, "default": "static",
                "choices": ["static", "dynamic"],
                "description": "'static': every BUY gets the flat expected_profit_percent. "
                               "'dynamic': expected_profit_percent scales up with how far the "
                               "EPS surprise exceeds surprise_min_pct (see dynamic_scale). "
                               "dynamic_scale=0 makes 'dynamic' numerically identical to 'static'.",
            },
            "dynamic_scale": {
                "type": "float", "required": False, "default": 0.0,
                "description": "Only used when expected_profit_mode='dynamic'. Added profit %% "
                               "per 1 point the EPS surprise exceeds surprise_min_pct: "
                               "expected_profit = expected_profit_percent + dynamic_scale * "
                               "max(0, surprise_pct - surprise_min_pct). Ignored in 'static' mode.",
            },
        }

    # ------------------------------------------------------------------
    # Backtest contract (Phase 1): _gather (provider I/O) + _process (pure).
    # The SAME pair runs live (run_analysis, as_of=None) and in backtest
    # (analyze_as_of, as_of=<date>). _gather threads as_of into the provider so
    # the point-in-time (no-lookahead) fetch and the live latest fetch share one
    # code path; with as_of=None the fetch is byte-identical to the live path.
    # ------------------------------------------------------------------
    _SETTING_KEYS = ("surprise_min_pct", "max_days_since_report", "expected_profit_percent",
                      "expected_profit_mode", "dynamic_scale")

    def _gather(self, providers: ProviderBundle, as_of: Optional[datetime]) -> Dict[str, Any]:
        symbol = self._gather_symbol
        details_provider = providers.fundamentals_details()
        latest = None
        skip_detail_fetch = False
        # LIVE-ONLY calendar shortcut (see module docstring). Backtest (as_of set) never enters
        # this branch, so grid/backtest results are byte-identical to before this change.
        if as_of is None and isinstance(details_provider, FMPCompanyDetailsProvider):
            try:
                api_key = details_provider.api_key
                max_days = int(self._gather_max_days_since_report)
                now = datetime.now(timezone.utc)
                from_date = (now - timedelta(days=max_days)).date().isoformat()
                to_date = now.date().isoformat()
                calendar = _fetch_earnings_calendar_by_symbol(api_key, from_date, to_date)
                row = calendar.get(symbol.upper())
                if row is None:
                    # No report in the lookback window at all -> definitively no signal;
                    # evaluate_earnings_drift(None, ...) -> "no earnings data" -> HOLD.
                    skip_detail_fetch = True
                else:
                    mapped = _calendar_row_to_latest_earnings(row)
                    if mapped is not None:
                        latest = mapped
                        skip_detail_fetch = True
                    # else: report exists but the calendar is missing the analyst estimate ->
                    # fall through to the per-symbol detail fetch below.
            except Exception as e:  # noqa: BLE001 -- best-effort optimization; never break the run
                self.logger.debug(
                    f"Earnings calendar shortcut failed for {symbol}, falling back to "
                    f"per-symbol fetch: {e}")

        if not skip_detail_fetch:
            data = past_earnings_get(
                details_provider, symbol, as_of=as_of,
                frequency="quarterly", lookback_periods=1, format_type="dict")
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
        expected_profit_base = float(settings["expected_profit_percent"])
        # Both declared optional (required=False) with defaults in get_settings_definitions --
        # .get() here mirrors that contract for callers (tests, older configs) that don't pass
        # them, unlike the required settings above which fail loud on a missing key.
        expected_profit_mode = str(settings.get("expected_profit_mode", "static"))
        dynamic_scale = float(settings.get("dynamic_scale", 0.0))
        result = evaluate_earnings_drift(data_bundle["latest_earnings"], now, surprise_min, max_days)

        if result["is_signal"]:
            if expected_profit_mode == "dynamic" and result["surprise_pct"] is not None:
                # dynamic_scale=0 is numerically identical to 'static' -- the excess-surprise
                # bonus vanishes and expected_profit collapses to expected_profit_base.
                excess_surprise = max(0.0, float(result["surprise_pct"]) - surprise_min)
                expected_profit = expected_profit_base + dynamic_scale * excess_surprise
            else:
                expected_profit = expected_profit_base
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
            self._gather_max_days_since_report = settings["max_days_since_report"]
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
