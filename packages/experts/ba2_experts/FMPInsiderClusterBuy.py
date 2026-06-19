"""
FMPInsiderClusterBuy Expert

Signal: an insider *cluster buy* — several distinct insiders purchasing the
company's stock on the open market within a short window. A lone purchase can
be noise; multiple officers/directors independently committing capital in the
same period is one of the better-documented bullish signals.

Data: FMPInsiderProvider (Form-4 insider transactions). Only open-market
purchases (``P-Purchase``) count toward the cluster; awards/grants and option
exercises are ignored, and concurrent open-market selling reduces confidence.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.models import AnalysisOutput, ExpertRecommendation, MarketAnalysis
from ba2_common.core.db import add_instance, get_db, update_instance
from ba2_common.core.types import (
    MarketAnalysisStatus, OrderRecommendation, Recommendation, RiskLevel, TimeHorizon,
)
from ba2_common.core.backtest_context import BacktestContext, ProviderBundle
from ba2_common.logger import get_expert_logger
from ba2_experts.expert_mixins import AnalysisStatusRenderMixin
from ba2_providers.cache.cached_get import insider_get


def detect_insider_cluster(transactions: List[Dict[str, Any]],
                           min_insiders: int,
                           min_total_value: float) -> Dict[str, Any]:
    """Pure cluster-detection over dict-format insider transactions.

    Args:
        transactions: rows from FMPInsiderProvider.get_insider_transactions
            (dict format): each has insider_name, transaction_type, value.
        min_insiders: distinct buyers required for a cluster.
        min_total_value: combined open-market purchase value ($) required.

    Returns dict with: is_cluster, buyer_count, buy_value, sell_value,
    buyers (name -> value), confidence (0-100).
    """
    buyers: Dict[str, float] = {}
    buy_value = 0.0
    sell_value = 0.0
    for t in transactions:
        ttype = (t.get("transaction_type") or "").upper()
        value = abs(float(t.get("value") or 0.0))
        name = (t.get("insider_name") or "").strip()
        if ttype.startswith("P-"):          # open-market purchase
            if value > 0 and name:
                buyers[name] = buyers.get(name, 0.0) + value
                buy_value += value
        elif ttype.startswith("S-"):        # open-market sale
            sell_value += value

    buyer_count = len(buyers)
    is_cluster = buyer_count >= min_insiders and buy_value >= min_total_value

    confidence = 0.0
    if is_cluster:
        # Base 55; +8 per buyer beyond the minimum (cap +24); + up to +15 for
        # purchase size ($1M buys = +5); selling against the cluster subtracts
        # proportionally (full offset when sells match buys).
        confidence = 55.0
        confidence += min(24.0, (buyer_count - min_insiders) * 8.0)
        confidence += min(15.0, (buy_value / 1_000_000.0) * 5.0)
        if buy_value > 0 and sell_value > 0:
            confidence -= min(30.0, (sell_value / buy_value) * 30.0)
        confidence = max(0.0, min(100.0, confidence))

    return {
        "is_cluster": is_cluster,
        "buyer_count": buyer_count,
        "buy_value": buy_value,
        "sell_value": sell_value,
        "buyers": buyers,
        "confidence": confidence,
    }


class FMPInsiderClusterBuy(AnalysisStatusRenderMixin, MarketExpertInterface):
    """Insider cluster-buy expert: BUY when several insiders bought recently."""

    RENDER_PENDING_MESSAGE = 'Insider cluster analysis for {symbol} is queued'
    RENDER_RUNNING_MESSAGE = 'Scanning insider transactions for {symbol}...'

    @classmethod
    def description(cls) -> str:
        return "BUY when multiple insiders purchased on the open market within a short window"

    def __init__(self, id: int):
        super().__init__(id)
        self._load_expert_instance(id)
        self.logger = get_expert_logger("FMPInsiderClusterBuy", id)

    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
            "lookback_days": {
                "type": "int", "required": True, "default": 30,
                "description": "Window (days) in which the insider purchases must cluster",
            },
            "min_insiders": {
                "type": "int", "required": True, "default": 3,
                "description": "Distinct insiders that must have bought within the window",
            },
            "min_total_value": {
                "type": "float", "required": True, "default": 200000.0,
                "description": "Minimum combined open-market purchase value ($)",
            },
            "expected_profit_percent": {
                "type": "float", "required": True, "default": 10.0,
                "description": "Expected profit %% attached to BUY recommendations",
            },
        }

    # ------------------------------------------------------------------
    # Backtest contract (Phase 1): _gather (provider I/O) + _process (pure).
    # The SAME pair runs live (run_analysis, as_of=None) and in backtest
    # (analyze_as_of, as_of=<date>). _gather threads as_of into the insider
    # provider so the point-in-time (no-lookahead filingDate anchor) fetch and the
    # live latest fetch share one code path; with as_of=None the fetch is
    # byte-identical to the live transactionDate-range behaviour.
    #
    # lookback_days is a SETTING needed at gather time (it bounds the fetch
    # window), so it must be resolved BEFORE _gather and stashed on
    # self._gather_lookback_days: live run_analysis sets it from resolved
    # settings; analyze_as_of sets it from context.settings["lookback_days"].
    # ------------------------------------------------------------------
    _SETTING_KEYS = ("lookback_days", "min_insiders", "min_total_value", "expected_profit_percent")

    def _gather(self, providers: ProviderBundle, as_of: Optional[datetime]) -> Dict[str, Any]:
        symbol = self._gather_symbol
        lookback_days = int(self._gather_lookback_days)   # resolved by caller from settings
        insider_provider = providers.insider()
        insider_data = insider_get(
            insider_provider, symbol, as_of=as_of,
            lookback=lookback_days, format_type="dict")
        if not isinstance(insider_data, dict):
            insider_data = {"transactions": [], "start_date": "", "end_date": ""}
        # Live (as_of=None) reads the account/broker quote (the original live
        # source); backtest (as_of set) reads the OHLCV close-at-as_of.
        current_price = (self._get_current_price(symbol) if as_of is None
                         else providers.price_at_date(symbol, as_of))
        return {"insider_data": insider_data, "current_price": current_price, "symbol": symbol}

    def _process(self, data_bundle: Dict[str, Any], settings: Dict[str, Any],
                 as_of: Optional[datetime] = None) -> Recommendation:
        rec = self._calculate_recommendation(
            data_bundle["insider_data"], int(settings["min_insiders"]),
            float(settings["min_total_value"]), float(settings["expected_profit_percent"]))
        return Recommendation(
            signal=rec["signal"], confidence=round(rec["confidence"], 1),
            current_price=data_bundle["current_price"], details=rec["details"],
            expected_profit_percent=rec["expected_profit_percent"],
            raw_outputs={"name": "Insider Cluster Analysis", "type": "insider_cluster_analysis",
                         "text": rec["details"], "cluster": rec["cluster"]})

    def analyze_as_of(self, as_of: datetime, context: BacktestContext) -> Recommendation:
        """BacktestInterface entry: resolve the gather-time lookback (needed BEFORE
        _gather to bound the fetch window) then run the SAME _gather+_process as live."""
        self._gather_symbol = context.extra.get("symbol", getattr(self, "_gather_symbol", None))
        self._gather_lookback_days = int(context.settings["lookback_days"])
        bundle = self._gather(context.providers, as_of)
        return self._process(bundle, context.settings, as_of)

    def _calculate_recommendation(self, insider_data: Dict[str, Any],
                                  min_insiders: int, min_total_value: float,
                                  expected_profit: float) -> Dict[str, Any]:
        cluster = detect_insider_cluster(
            insider_data.get("transactions", []), min_insiders, min_total_value)

        if cluster["is_cluster"]:
            signal = OrderRecommendation.BUY
            confidence = cluster["confidence"]
            expected = expected_profit
        else:
            signal = OrderRecommendation.HOLD
            confidence = 10.0
            expected = 0.0

        buyer_lines = "\n".join(
            f"- {name}: ${value:,.0f}" for name, value in
            sorted(cluster["buyers"].items(), key=lambda kv: -kv[1])
        ) or "- none"

        details = f"""Insider Cluster-Buy Analysis ({insider_data.get('start_date', '')[:10]} to {insider_data.get('end_date', '')[:10]})

Open-market buyers: {cluster['buyer_count']} (minimum {min_insiders})
Combined purchase value: ${cluster['buy_value']:,.0f} (minimum ${min_total_value:,.0f})
Open-market sales in window: ${cluster['sell_value']:,.0f}

Buyers:
{buyer_lines}

Cluster detected: {cluster['is_cluster']}
Recommendation: {signal.value}
Confidence: {confidence:.1f}%
"""
        return {
            "signal": signal,
            "confidence": confidence,
            "expected_profit_percent": expected,
            "details": details,
            "cluster": cluster,
        }

    # ------------------------------------------------------------------
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """Thin live orchestrator: resolve settings -> _gather(as_of=None) ->
        _process -> persist ExpertRecommendation + AnalysisOutput + state. Runs the
        EXACT same _gather/_process the backtest engine drives via analyze_as_of."""
        self.logger.info(f"Starting insider-cluster analysis for {symbol} (Analysis ID: {market_analysis.id})")
        try:
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)

            settings = self._resolve_settings(self._SETTING_KEYS)
            lookback_days = int(settings["lookback_days"])
            min_insiders = int(settings["min_insiders"])
            min_total_value = float(settings["min_total_value"])

            self._gather_symbol = symbol
            self._gather_lookback_days = lookback_days   # gather-time fetch window
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
                time_horizon=TimeHorizon.MEDIUM_TERM,
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

            cluster = rec.raw_outputs["cluster"]
            market_analysis.state = {
                "insider_cluster": {
                    "recommendation": {
                        "signal": rec.signal.value,
                        "confidence": rec.confidence,
                        "expected_profit_percent": rec.expected_profit_percent,
                    },
                    "cluster": {
                        "is_cluster": cluster["is_cluster"],
                        "buyer_count": cluster["buyer_count"],
                        "buy_value": cluster["buy_value"],
                        "sell_value": cluster["sell_value"],
                        "buyers": cluster["buyers"],
                    },
                    "settings": {
                        "lookback_days": lookback_days,
                        "min_insiders": min_insiders,
                        "min_total_value": min_total_value,
                    },
                    "expert_recommendation_id": recommendation_id,
                    "current_price": rec.current_price,
                    "analysis_timestamp": datetime.now(timezone.utc).isoformat(),
                }
            }
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            self.logger.info(
                f"Completed insider-cluster analysis for {symbol}: {rec.signal.value} "
                f"({cluster['buyer_count']} buyers, ${cluster['buy_value']:,.0f})")

        except Exception as e:
            self.logger.error(f"Insider-cluster analysis failed for {symbol}: {e}", exc_info=True)
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
        state = (market_analysis.state or {}).get("insider_cluster")
        if not state:
            with ui.card().classes('w-full p-4'):
                ui.label('No analysis data available').classes('text-grey-7')
            return
        rec = state.get("recommendation", {})
        cluster = state.get("cluster", {})
        with ui.card().classes('w-full p-4'):
            ui.label(f"Insider Cluster Analysis - {market_analysis.symbol}").classes('text-h6')
            with ui.row().classes('gap-8 mt-2'):
                ui.label(f"Signal: {rec.get('signal', 'N/A')}").classes('text-h6')
                ui.label(f"Confidence: {rec.get('confidence', 0):.1f}%")
                ui.label(f"Buyers: {cluster.get('buyer_count', 0)}")
                ui.label(f"Bought: ${cluster.get('buy_value', 0):,.0f}")
                ui.label(f"Sold: ${cluster.get('sell_value', 0):,.0f}")
            buyers = cluster.get("buyers", {})
            if buyers:
                with ui.column().classes('mt-2'):
                    ui.label('Open-market buyers:').classes('text-bold')
                    for name, value in sorted(buyers.items(), key=lambda kv: -kv[1]):
                        ui.label(f"  {name}: ${value:,.0f}").classes('text-grey-8')
