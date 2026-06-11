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

from ...core.interfaces import MarketExpertInterface
from ...core.models import AnalysisOutput, ExpertRecommendation, MarketAnalysis
from ...core.db import add_instance, get_db, update_instance
from ...core.types import MarketAnalysisStatus, OrderRecommendation, RiskLevel, TimeHorizon
from ...logger import get_expert_logger
from .expert_mixins import AnalysisStatusRenderMixin


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
    def _fetch_transactions(self, symbol: str, lookback_days: int) -> Dict[str, Any]:
        from ..dataproviders.insider.FMPInsiderProvider import FMPInsiderProvider
        provider = FMPInsiderProvider()
        return provider.get_insider_transactions(
            symbol=symbol,
            end_date=datetime.now(timezone.utc),
            lookback_days=lookback_days,
            format_type="dict",
        )

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
        self.logger.info(f"Starting insider-cluster analysis for {symbol} (Analysis ID: {market_analysis.id})")
        try:
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)

            lookback_days = int(self.get_setting_with_interface_default("lookback_days"))
            min_insiders = int(self.get_setting_with_interface_default("min_insiders"))
            min_total_value = float(self.get_setting_with_interface_default("min_total_value"))
            expected_profit = float(self.get_setting_with_interface_default("expected_profit_percent"))

            insider_data = self._fetch_transactions(symbol, lookback_days)
            if not isinstance(insider_data, dict):
                raise ValueError(f"Insider provider returned no structured data for {symbol}")

            rec = self._calculate_recommendation(
                insider_data, min_insiders, min_total_value, expected_profit)

            current_price = self._get_current_price(symbol)
            if not current_price:
                raise ValueError(f"Unable to get current price for {symbol}")

            recommendation_id = add_instance(ExpertRecommendation(
                instance_id=self.id,
                symbol=symbol,
                recommended_action=rec["signal"],
                expected_profit_percent=rec["expected_profit_percent"],
                price_at_date=current_price,
                details=rec["details"],
                confidence=round(rec["confidence"], 1),
                risk_level=RiskLevel.MEDIUM,
                time_horizon=TimeHorizon.MEDIUM_TERM,
                market_analysis_id=market_analysis.id,
                created_at=datetime.now(timezone.utc),
            ))

            session = get_db()
            try:
                session.add(AnalysisOutput(
                    market_analysis_id=market_analysis.id,
                    name="Insider Cluster Analysis",
                    type="insider_cluster_analysis",
                    text=rec["details"],
                ))
                session.commit()
            finally:
                session.close()

            cluster = rec["cluster"]
            market_analysis.state = {
                "insider_cluster": {
                    "recommendation": {
                        "signal": rec["signal"].value,
                        "confidence": rec["confidence"],
                        "expected_profit_percent": rec["expected_profit_percent"],
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
                    "current_price": current_price,
                    "analysis_timestamp": datetime.now(timezone.utc).isoformat(),
                }
            }
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            self.logger.info(
                f"Completed insider-cluster analysis for {symbol}: {rec['signal'].value} "
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
