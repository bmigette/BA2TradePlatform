from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import json
import requests

from ba2_common.core.interfaces import MarketExpertInterface
from ba2_experts.expert_mixins import AnalysisStatusRenderMixin
from ba2_common.core.models import MarketAnalysis, AnalysisOutput, ExpertRecommendation
from ba2_common.core.db import get_db, update_instance, add_instance, get_setting
from ba2_common.core.types import (
    MarketAnalysisStatus, OrderRecommendation, Recommendation, RiskLevel, TimeHorizon,
)
from ba2_common.core.backtest_context import BacktestContext, ProviderBundle
from ba2_common.core.provider_utils import parse_provider_date
from ba2_common.logger import get_expert_logger


# Standard analyst-consensus values for the 5 rating buckets (bearish -> bullish).
RATING_VALUES = {"strongBuy": 5, "buy": 4, "hold": 3, "sell": 2, "strongSell": 1}

# Default consensus-mean thresholds: the inclusive lower bound of each grade on
# the 1-5 scale. mean >= buy -> BUY, >= overweight -> OVERWEIGHT, >= hold -> HOLD,
# >= underweight -> UNDERWEIGHT, else SELL.
DEFAULT_RATING_THRESHOLDS = {"buy": 4.5, "overweight": 3.5, "hold": 2.5, "underweight": 1.5}


def consensus_from_counts(counts: Dict[str, Any], thresholds: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Map Finnhub analyst rating counts to a 5-grade recommendation.

    Uses the consensus mean on a 1-5 scale (strongSell=1 ... strongBuy=5) and
    buckets it into SELL/UNDERWEIGHT/HOLD/OVERWEIGHT/BUY. Confidence is the share
    (0-100) of analysts on the dominant side of the chosen grade.

    Returns a dict with: signal (OrderRecommendation), confidence (float),
    mean (float|None), total (int).
    """
    t = thresholds or DEFAULT_RATING_THRESHOLDS
    sb = counts.get("strongBuy", 0) or 0
    b = counts.get("buy", 0) or 0
    h = counts.get("hold", 0) or 0
    s = counts.get("sell", 0) or 0
    ss = counts.get("strongSell", 0) or 0
    total = sb + b + h + s + ss
    if total <= 0:
        return {"signal": OrderRecommendation.HOLD, "confidence": 0.0, "mean": None, "total": 0}

    mean = (5 * sb + 4 * b + 3 * h + 2 * s + 1 * ss) / total
    if mean >= t["buy"]:
        signal, agree = OrderRecommendation.BUY, sb + b
    elif mean >= t["overweight"]:
        signal, agree = OrderRecommendation.OVERWEIGHT, sb + b
    elif mean >= t["hold"]:
        signal, agree = OrderRecommendation.HOLD, h
    elif mean >= t["underweight"]:
        signal, agree = OrderRecommendation.UNDERWEIGHT, s + ss
    else:
        signal, agree = OrderRecommendation.SELL, s + ss
    confidence = (agree / total) * 100.0
    return {"signal": signal, "confidence": confidence, "mean": mean, "total": total}


class FinnHubRating(AnalysisStatusRenderMixin, MarketExpertInterface):
    """
    FinnHubRating Expert Implementation
    
    Simple expert that uses Finnhub's recommendation trends API to generate
    trading recommendations based on analyst ratings. Confidence is calculated
    from the buy/sell ratio weighted by strong factor.
    """
    
    RENDER_PENDING_MESSAGE = 'FinnHubRating analysis for {symbol} is queued'
    RENDER_RUNNING_MESSAGE = 'Fetching analyst recommendations for {symbol}...'

    @classmethod
    def description(cls) -> str:
        return "Finnhub analyst recommendation trends with weighted confidence scoring"
    
    def __init__(self, id: int):
        """Initialize FinnHubRating expert with database instance."""
        super().__init__(id)
        
        self._load_expert_instance(id)
        # Initialize the expert logger BEFORE _get_finnhub_api_key(): that helper logs a warning
        # via self.logger when the key is absent, so the logger must exist first or the (common in
        # backtests) no-key path raises AttributeError.
        self.logger = get_expert_logger("FinnHubRating", id)
        self._api_key = self._get_finnhub_api_key()
    
    def _get_finnhub_api_key(self) -> Optional[str]:
        """Get Finnhub API key from app settings."""
        api_key = get_setting('finnhub_api_key')
        if not api_key:
            self.logger.warning("Finnhub API key not found in app settings")
        return api_key
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """Define configurable settings for FinnHubRating expert.

        The analyst consensus mean (1=strongSell .. 5=strongBuy) is bucketed into
        the 5-grade scale using these inclusive lower-bound thresholds.
        """
        return {
            "buy_threshold": {
                "type": "float", "required": True, "default": DEFAULT_RATING_THRESHOLDS["buy"],
                "description": "Consensus mean (1-5) at or above which the rating is BUY (strong bullish)",
                "tooltip": "Default 4.5 — needs a strong-buy-leaning analyst consensus."
            },
            "overweight_threshold": {
                "type": "float", "required": True, "default": DEFAULT_RATING_THRESHOLDS["overweight"],
                "description": "Consensus mean at or above which the rating is OVERWEIGHT (mild bullish)",
                "tooltip": "Default 3.5 — buy-leaning consensus below the BUY threshold."
            },
            "hold_threshold": {
                "type": "float", "required": True, "default": DEFAULT_RATING_THRESHOLDS["hold"],
                "description": "Consensus mean at or above which the rating is HOLD (neutral)",
                "tooltip": "Default 2.5 — below this becomes UNDERWEIGHT/SELL."
            },
            "underweight_threshold": {
                "type": "float", "required": True, "default": DEFAULT_RATING_THRESHOLDS["underweight"],
                "description": "Consensus mean at or above which the rating is UNDERWEIGHT (mild bearish); below it is SELL",
                "tooltip": "Default 1.5 — below this becomes SELL (strong bearish)."
            },
        }

    def _get_rating_thresholds(self) -> Dict[str, float]:
        """Read the consensus-mean bucket thresholds from settings (with defaults)."""
        return {
            "buy": float(self.get_setting_with_interface_default("buy_threshold")),
            "overweight": float(self.get_setting_with_interface_default("overweight_threshold")),
            "hold": float(self.get_setting_with_interface_default("hold_threshold")),
            "underweight": float(self.get_setting_with_interface_default("underweight_threshold")),
        }
    
    # ------------------------------------------------------------------
    # Backtest contract (Phase 1): _gather (provider/API I/O) + _process (pure).
    # The SAME pair runs live (run_analysis, as_of=None) and in backtest
    # (analyze_as_of, as_of=<date>). Finnhub is NOT in the get_provider registry
    # (it is a separate analyst-trends API), so _gather keeps the direct Finnhub
    # client for the trends fetch and routes ONLY the current price through the
    # providers bundle (Decision 1: one price source for all experts).
    #
    # _gather stores the FULL trends_data list (not just trends_data[0]) so
    # _process can do no-lookahead period selection by date (see _select_period):
    # with as_of=None it is byte-identical to the live trends_data[0] behaviour;
    # with as_of set it picks the latest period whose date <= as_of.
    # ------------------------------------------------------------------
    _SETTING_KEYS = ("buy_threshold", "overweight_threshold", "hold_threshold", "underweight_threshold")

    def _gather(self, providers: ProviderBundle, as_of: Optional[datetime]) -> Dict[str, Any]:
        symbol = self._gather_symbol
        trends_data = self._fetch_recommendation_trends(symbol)
        if not isinstance(trends_data, list):
            trends_data = []
        # Live (as_of=None) reads the account/broker quote (the original live
        # source); backtest (as_of set) reads the OHLCV close-at-as_of.
        current_price = (self._get_current_price(symbol) if as_of is None
                         else providers.price_at_date(symbol, as_of))
        return {"trends_data": trends_data, "current_price": current_price, "symbol": symbol}

    def _process(self, data_bundle: Dict[str, Any], settings: Dict[str, Any],
                 as_of: Optional[datetime] = None) -> Recommendation:
        thresholds = {
            "buy": float(settings["buy_threshold"]),
            "overweight": float(settings["overweight_threshold"]),
            "hold": float(settings["hold_threshold"]),
            "underweight": float(settings["underweight_threshold"]),
        }
        rec = self._calculate_recommendation(
            data_bundle.get("trends_data") or [], thresholds, as_of=as_of)
        return Recommendation(
            signal=rec["signal"],
            confidence=round(rec["confidence"], 1),
            current_price=data_bundle["current_price"],
            details=rec["details"],
            # FinnHub provides no price targets, so it cannot estimate profit; 0.0
            # is intentional (matches the live ExpertRecommendation.expected_profit_percent).
            expected_profit_percent=0.0,
            raw_outputs={"name": "FinnHub Recommendation Analysis",
                         "type": "finnhub_rating_analysis", "text": rec["details"],
                         "mean": rec["mean"], "total": rec["total"],
                         "period": rec["period"]})

    def analyze_as_of(self, as_of: datetime, context: BacktestContext) -> Recommendation:
        """BacktestInterface entry: runs the SAME _gather+_process as the live path."""
        self._gather_symbol = context.extra.get("symbol", getattr(self, "_gather_symbol", None))
        bundle = self._gather(context.providers, as_of)
        return self._process(bundle, context.settings, as_of)

    def _resolve_thresholds_settings(self) -> Dict[str, Any]:
        """Resolve the consensus-mean bucket thresholds as a plain dict for _process."""
        return self._resolve_settings(self._SETTING_KEYS)

    def _fetch_recommendation_trends(self, symbol: str) -> list:
        """
        Fetch recommendation trends from Finnhub API.

        The full per-symbol trends history is fetched once and disk-cached (backtest
        path only) via ``fmp_history_disk_cached`` so spawned grid workers read it from
        disk instead of re-hammering Finnhub; the live path (freeze flag unset) is a
        straight passthrough. The post-fetch ``_select_period`` <= as_of filtering is
        applied by the caller and is unaffected by the cache (the whole history is
        cached, not a date-filtered slice).

        Args:
            symbol: Stock symbol to query

        Returns:
            API response data (list of recommendation periods)

        Raises:
            ValueError: If API key not configured or API returns error
        """
        if not self._api_key:
            raise ValueError("Finnhub API key not configured")

        def _fetch() -> list:
            try:
                url = "https://finnhub.io/api/v1/stock/recommendation"
                params = {
                    "symbol": symbol,
                    "token": self._api_key
                }

                self.logger.debug(f"Fetching Finnhub recommendations for {symbol}")
                response = requests.get(url, params=params, timeout=30)

                # Check for HTTP errors and include response body in error message
                if response.status_code != 200:
                    try:
                        error_body = response.json()
                        error_msg = error_body.get('error', response.text)
                    except:
                        error_msg = response.text
                    raise ValueError(f"Finnhub API error (HTTP {response.status_code}): {error_msg}")

                data = response.json()

                # Check for API-level errors in response
                if isinstance(data, dict) and 'error' in data:
                    raise ValueError(f"Finnhub API error: {data['error']}")

                self.logger.debug(f"Received {len(data) if isinstance(data, list) else 0} recommendation periods")

                return data

            except requests.exceptions.Timeout:
                raise ValueError(f"Finnhub API timeout for {symbol} (30s)")
            except requests.exceptions.ConnectionError as e:
                raise ValueError(f"Finnhub API connection error for {symbol}: {e}")
            except requests.exceptions.RequestException as e:
                raise ValueError(f"Finnhub API request failed for {symbol}: {e}")

        # Backtest-only per-symbol disk cache (passthrough live); gated on the global
        # backtest freeze flag. Errors propagate (never cached) so they retry next run.
        from ba2_providers.fmp_common import fmp_history_disk_cached
        return fmp_history_disk_cached("finnhub_reco_trends", symbol, _fetch)
    
    @staticmethod
    def _select_period(trends_data: list, as_of: Optional[datetime]) -> Optional[Dict[str, Any]]:
        """Pick the recommendation period the decision logic should use.

        LOOKAHEAD BUG FIX: the live code used ``trends_data[0]`` (Finnhub returns
        newest-first), which in a backtest would leak periods published AFTER as_of.

        - ``as_of=None`` (live): byte-identical to the old behaviour — return
          ``trends_data[0]`` (the latest period) with no date filtering.
        - ``as_of`` set (backtest): return the most-recent period whose ``period``
          date is on/before ``as_of`` (no-lookahead); ``None`` if none qualify.
        """
        if not trends_data:
            return None
        if as_of is None:
            return trends_data[0]
        eligible = [t for t in trends_data
                    if (pd_ := parse_provider_date(t.get("period"))) is not None and pd_ <= as_of]
        if not eligible:
            return None
        return max(eligible, key=lambda t: parse_provider_date(t.get("period")))

    def _calculate_recommendation(self, trends_data: list,
                                  thresholds: Optional[Dict[str, float]] = None,
                                  as_of: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Calculate a 5-grade trading recommendation from trends data.

        Uses the selected recommendation period (see ``_select_period``: latest
        live, or latest-on-or-before-``as_of`` in backtest) and the analyst
        consensus mean (1=strongSell .. 5=strongBuy), bucketed into BUY/OVERWEIGHT/
        HOLD/UNDERWEIGHT/SELL via the configured thresholds. Confidence is the share
        of analysts on the dominant side of the chosen grade.

        Args:
            trends_data: List of recommendation trend periods from Finnhub
            thresholds: Optional consensus-mean bucket thresholds (defaults applied)
            as_of: point-in-time ceiling for period selection (None => latest live)

        Returns:
            Dictionary with recommendation details
        """
        latest = self._select_period(trends_data, as_of)
        if latest is None:
            return {
                'signal': OrderRecommendation.HOLD,
                'confidence': 0.0,
                'details': 'No recommendation data available',
                'mean': None,
                'total': 0,
                'counts': {},
                'period': None,
            }

        period = latest.get('period', 'Unknown')
        counts = {k: latest.get(k, 0) for k in ('strongBuy', 'buy', 'hold', 'sell', 'strongSell')}

        result = consensus_from_counts(counts, thresholds)
        signal = result['signal']
        confidence = result['confidence']
        mean = result['mean']
        total = result['total']
        mean_str = f"{mean:.2f}" if mean is not None else "N/A"

        details = f"""Finnhub Recommendation Trends Analysis (Period: {period})

Analyst Ratings:
- Strong Buy: {counts['strongBuy']}
- Buy: {counts['buy']}
- Hold: {counts['hold']}
- Sell: {counts['sell']}
- Strong Sell: {counts['strongSell']}

Consensus Mean (1=Strong Sell .. 5=Strong Buy): {mean_str}  ({total} analysts)

Recommendation: {signal.value}
Confidence (agreement on dominant side): {confidence:.1f}%
"""

        return {
            'signal': signal,
            'confidence': confidence,
            'details': details,
            'mean': mean,
            'total': total,
            'counts': counts,
            'period': period,
            'raw_data': latest,
        }
    
    def _create_expert_recommendation(self, recommendation_data: Dict[str, Any], 
                                     symbol: str, market_analysis_id: int,
                                     current_price: Optional[float]) -> int:
        """Create ExpertRecommendation record in database."""
        try:
            expert_recommendation = ExpertRecommendation(
                instance_id=self.id,
                symbol=symbol,
                recommended_action=recommendation_data['signal'],
                # FinnHub doesn't provide price targets, so it cannot estimate profit.
                # 0.0 is intentional: the classic risk manager prioritizes 0.0-profit
                # recommendations by CONFIDENCE (compute_order_priority_score, RM-3/EX-5),
                # so these orders are still funded by conviction rather than starved.
                expected_profit_percent=0.0,
                price_at_date=current_price,
                details=recommendation_data['details'][:100000] if recommendation_data['details'] else None,
                confidence=round(recommendation_data['confidence'], 1),  # Store as 1-100 scale
                risk_level=RiskLevel.MEDIUM,  # Always medium risk
                time_horizon=TimeHorizon.MEDIUM_TERM,  # Always medium term
                market_analysis_id=market_analysis_id,
                created_at=datetime.now(timezone.utc)
            )
            
            recommendation_id = add_instance(expert_recommendation)
            self.logger.info(f"Created ExpertRecommendation (ID: {recommendation_id}) for {symbol}: "
                       f"{recommendation_data['signal'].value} with {recommendation_data['confidence']:.1f}% confidence")
            return recommendation_id
            
        except Exception as e:
            self.logger.error(f"Failed to create ExpertRecommendation for {symbol}: {e}", exc_info=True)
            raise
    
    def _store_analysis_outputs(self, market_analysis_id: int, symbol: str,
                               recommendation_data: Dict[str, Any],
                               full_api_response: list) -> None:
        """Store detailed analysis outputs in database."""
        session = get_db()
        
        try:
            # Store analysis details
            details_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="FinnHub Recommendation Analysis",
                type="finnhub_rating_analysis",
                text=recommendation_data['details']
            )
            session.add(details_output)
            
            # Store full API response
            api_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="FinnHub API Response",
                type="finnhub_api_response",
                text=json.dumps(full_api_response, indent=2)
            )
            session.add(api_output)
            
            session.commit()
            
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to store analysis outputs: {e}", exc_info=True)
            raise
        finally:
            session.close()
    
    def _sanitize_api_response(self, trends_data: list) -> list:
        """
        Sanitize API response data to make it JSON serializable.
        Converts pandas Timestamp objects to strings.
        
        Args:
            trends_data: List of recommendation trends from Finnhub API
            
        Returns:
            Sanitized list safe for JSON serialization
        """
        import pandas as pd
        
        sanitized = []
        for item in trends_data:
            sanitized_item = {}
            for key, value in item.items():
                # Convert pandas Timestamp to string
                if isinstance(value, pd.Timestamp):
                    sanitized_item[key] = value.strftime('%Y-%m-%d') if pd.notna(value) else None
                # Handle other non-serializable types if needed
                else:
                    sanitized_item[key] = value
            sanitized.append(sanitized_item)
        
        return sanitized
    
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """Thin live orchestrator: resolve settings -> _gather(as_of=None) ->
        _process -> persist ExpertRecommendation + AnalysisOutput + state. Runs the
        EXACT same _gather/_process the backtest engine drives via analyze_as_of;
        with as_of=None the period selection is byte-identical to the old
        trends_data[0] behaviour."""
        self.logger.info(f"Starting FinnHubRating analysis for {symbol} (Analysis ID: {market_analysis.id})")

        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)

            # Get consensus-mean bucket thresholds (resolved dict, _process never reads self)
            settings = self._resolve_thresholds_settings()
            thresholds = {
                "buy": float(settings["buy_threshold"]),
                "overweight": float(settings["overweight_threshold"]),
                "hold": float(settings["hold_threshold"]),
                "underweight": float(settings["underweight_threshold"]),
            }

            # Gather (Finnhub trends + as_of close) then process (pure decision logic)
            self._gather_symbol = symbol
            providers = self._live_providers()
            bundle = self._gather(providers, as_of=None)
            trends_data = bundle["trends_data"]
            current_price = bundle["current_price"]
            if not current_price:
                raise ValueError(f"Unable to get current price for {symbol}")
            rec = self._process(bundle, settings, as_of=None)

            # Re-assemble the legacy recommendation_data dict for the persisted artifacts
            # (the ExpertRecommendation row, the two AnalysisOutputs, and the state block
            # are kept byte-identical to the pre-refactor live behaviour).
            recommendation_data = {
                'signal': rec.signal,
                'confidence': rec.confidence,
                'details': rec.details,
                'mean': rec.raw_outputs['mean'],
                'total': rec.raw_outputs['total'],
                'period': rec.raw_outputs['period'],
            }

            # Create ExpertRecommendation record
            recommendation_id = self._create_expert_recommendation(
                recommendation_data, symbol, market_analysis.id, current_price
            )

            # Store analysis outputs
            self._store_analysis_outputs(market_analysis.id, symbol, recommendation_data, trends_data)

            # Sanitize API response for JSON serialization
            sanitized_trends = self._sanitize_api_response(trends_data)
            
            # Sanitize recommendation data (convert enums to values)
            sanitized_recommendation = {
                'signal': recommendation_data['signal'].value if hasattr(recommendation_data['signal'], 'value') else recommendation_data['signal'],
                'confidence': recommendation_data['confidence'],
                'details': recommendation_data['details'],
                'mean': recommendation_data['mean'],
                'total': recommendation_data['total'],
                'period': str(recommendation_data['period']) if recommendation_data['period'] else None
            }

            # Store analysis state
            market_analysis.state = {
                'finnhub_rating': {
                    'recommendation': sanitized_recommendation,
                    'api_response': sanitized_trends,
                    'settings': {
                        'thresholds': thresholds
                    },
                    'expert_recommendation_id': recommendation_id,
                    'analysis_timestamp': datetime.now(timezone.utc).isoformat(),
                    'current_price': current_price
                }
            }
            
            # Update status to completed
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            
            self.logger.info(f"Completed FinnHubRating analysis for {symbol}: {recommendation_data['signal'].value} "
                       f"(confidence: {recommendation_data['confidence']:.1f}%)")
            
        except Exception as e:
            self.logger.error(f"FinnHubRating analysis failed for {symbol}: {e}", exc_info=True)
            
            # Update status to failed
            market_analysis.state = {
                'error': str(e),
                'error_timestamp': datetime.now(timezone.utc).isoformat(),
                'analysis_failed': True
            }
            market_analysis.status = MarketAnalysisStatus.FAILED
            update_instance(market_analysis)
            
            # Create error output
            try:
                session = get_db()
                error_output = AnalysisOutput(
                    market_analysis_id=market_analysis.id,
                    name="Analysis Error",
                    type="error",
                    text=f"FinnHubRating analysis failed for {symbol}: {str(e)}"
                )
                session.add(error_output)
                session.commit()
                session.close()
            except Exception as db_error:
                self.logger.error(f"Failed to store error output: {db_error}", exc_info=True)
            
            raise
    
    def _render_completed(self, market_analysis: MarketAnalysis) -> None:
        """Render completed analysis with beautiful UI."""
        from nicegui import ui
        
        if not market_analysis.state or 'finnhub_rating' not in market_analysis.state:
            with ui.card().classes('w-full p-4'):
                ui.label('No analysis data available').classes('text-grey-7')
            return
        
        state = market_analysis.state['finnhub_rating']
        rec = state.get('recommendation', {})
        api_data = state.get('api_response', [])
        settings = state.get('settings', {})
        current_price = state.get('current_price')
        
        # Main card
        with ui.card().classes('w-full').style('background-color: #1e2a3a'):
            # Header with recommendation
            with ui.card_section().style('background: linear-gradient(135deg, #00d4aa 0%, #00b894 100%)'):
                ui.label('FinnHub Analyst Recommendations').classes('text-h5 text-weight-bold').style('color: white')
                ui.label(f'{market_analysis.symbol} - Analyst Consensus').style('color: rgba(255,255,255,0.8)')
            
            # Recommendation summary
            signal = rec.get('signal', OrderRecommendation.HOLD)
            confidence = rec.get('confidence', 0.0)
            
            # Handle signal as either enum or string (from JSON deserialization)
            if isinstance(signal, str):
                # Convert string to enum if needed
                try:
                    signal = OrderRecommendation(signal)
                except (ValueError, KeyError):
                    signal = OrderRecommendation.HOLD
                signal_text = signal.value
            elif hasattr(signal, 'value'):
                signal_text = signal.value
            else:
                signal_text = str(signal)
            
            # Color based on signal
            if signal == OrderRecommendation.BUY or signal == 'BUY':
                signal_color = 'positive'
                signal_icon = 'trending_up'
            elif signal == OrderRecommendation.SELL or signal == 'SELL':
                signal_color = 'negative'
                signal_icon = 'trending_down'
            else:
                signal_color = 'grey'
                signal_icon = 'trending_flat'
            
            with ui.card_section():
                with ui.row().classes('w-full items-center justify-between'):
                    with ui.column():
                        ui.label('Recommendation').classes('text-caption').style('color: #a0aec0')
                        with ui.row().classes('items-center gap-2'):
                            ui.icon(signal_icon, color=signal_color, size='2rem')
                            ui.label(signal_text).classes(f'text-h4 text-{signal_color}')
                    
                    with ui.column().classes('text-right'):
                        ui.label('Confidence').classes('text-caption').style('color: #a0aec0')
                        ui.label(f'{confidence:.1f}%').classes('text-h4').style('color: #e2e8f0')
                
                if current_price:
                    ui.separator().classes('my-2')
                    ui.label(f'Current Price: ${current_price:.2f}').style('color: #a0aec0')
            
            # Ratings breakdown
            if api_data and len(api_data) > 0:
                latest = api_data[0]
                period = latest.get('period', 'Unknown')
                
                with ui.card_section().style('background-color: #141c28'):
                    ui.label(f'Analyst Ratings - {period}').classes('text-subtitle1 text-weight-medium mb-2').style('color: #e2e8f0')
                    
                    strong_buy = latest.get('strongBuy', 0)
                    buy = latest.get('buy', 0)
                    hold = latest.get('hold', 0)
                    sell = latest.get('sell', 0)
                    strong_sell = latest.get('strongSell', 0)
                    total = strong_buy + buy + hold + sell + strong_sell
                    
                    # Create a visual bar chart
                    with ui.column().classes('w-full gap-2'):
                        # Strong Buy
                        if strong_buy > 0:
                            pct = (strong_buy / total * 100) if total > 0 else 0
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label('Strong Buy').classes('w-24 text-right text-sm').style('color: #a0aec0')
                                ui.label(str(strong_buy)).classes('w-8 text-sm font-bold').style('color: #00d4aa')
                                with ui.element('div').classes('flex-grow rounded overflow-hidden h-6').style('background-color: #2d3748'):
                                    ui.element('div').classes('h-full').style(f'width: {pct}%; background-color: #00d4aa')
                        
                        # Buy
                        if buy > 0:
                            pct = (buy / total * 100) if total > 0 else 0
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label('Buy').classes('w-24 text-right text-sm').style('color: #a0aec0')
                                ui.label(str(buy)).classes('w-8 text-sm font-bold').style('color: #48bb78')
                                with ui.element('div').classes('flex-grow rounded overflow-hidden h-6').style('background-color: #2d3748'):
                                    ui.element('div').classes('h-full').style(f'width: {pct}%; background-color: #48bb78')
                        
                        # Hold
                        if hold > 0:
                            pct = (hold / total * 100) if total > 0 else 0
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label('Hold').classes('w-24 text-right text-sm').style('color: #a0aec0')
                                ui.label(str(hold)).classes('w-8 text-sm font-bold').style('color: #ffa94d')
                                with ui.element('div').classes('flex-grow rounded overflow-hidden h-6').style('background-color: #2d3748'):
                                    ui.element('div').classes('h-full').style(f'width: {pct}%; background-color: #ffa94d')
                        
                        # Sell
                        if sell > 0:
                            pct = (sell / total * 100) if total > 0 else 0
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label('Sell').classes('w-24 text-right text-sm').style('color: #a0aec0')
                                ui.label(str(sell)).classes('w-8 text-sm font-bold').style('color: #ff6b6b')
                                with ui.element('div').classes('flex-grow rounded overflow-hidden h-6').style('background-color: #2d3748'):
                                    ui.element('div').classes('h-full').style(f'width: {pct}%; background-color: #ff6b6b')
                        
                        # Strong Sell
                        if strong_sell > 0:
                            pct = (strong_sell / total * 100) if total > 0 else 0
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label('Strong Sell').classes('w-24 text-right text-sm').style('color: #a0aec0')
                                ui.label(str(strong_sell)).classes('w-8 text-sm font-bold').style('color: #e53e3e')
                                with ui.element('div').classes('flex-grow rounded overflow-hidden h-6').style('background-color: #2d3748'):
                                    ui.element('div').classes('h-full').style(f'width: {pct}%; background-color: #e53e3e')
                    
                    ui.separator().classes('my-2')
                    ui.label(f'Total Analysts: {total}').classes('text-sm').style('color: #00d4aa')
            
            # Consensus mean
            mean = rec.get('mean')
            total = rec.get('total', 0)
            mean_str = f'{mean:.2f}' if isinstance(mean, (int, float)) else 'N/A'

            with ui.card_section():
                ui.label('Consensus').classes('text-subtitle1 text-weight-medium mb-2').style('color: #e2e8f0')
                with ui.grid(columns=2).classes('w-full gap-4'):
                    with ui.card().style('background-color: rgba(0, 212, 170, 0.15)'):
                        ui.label('Consensus Mean (1-5)').classes('text-caption').style('color: #a0aec0')
                        ui.label(mean_str).classes('text-h5').style('color: #00d4aa')
                    with ui.card().style('background-color: rgba(160, 174, 192, 0.15)'):
                        ui.label('Analysts').classes('text-caption').style('color: #a0aec0')
                        ui.label(str(total)).classes('text-h5').style('color: #e2e8f0')

            # Methodology
            with ui.expansion('Calculation Methodology', icon='info').classes('w-full').style('color: #e2e8f0'):
                with ui.card_section().style('background-color: #141c28'):
                    ui.markdown('''
**5-Grade Consensus Mapping:**

Each analyst rating is scored on a 1-5 scale (Strong Sell = 1 ... Strong Buy = 5),
and the **consensus mean** is bucketed into a grade:

| Consensus mean | Grade |
|---|---|
| ≥ 4.5 | **BUY** (strong bullish) |
| 3.5 – 4.5 | **OVERWEIGHT** (mild bullish) |
| 2.5 – 3.5 | **HOLD** (neutral) |
| 1.5 – 2.5 | **UNDERWEIGHT** (mild bearish) |
| < 1.5 | **SELL** (strong bearish) |

**Confidence** = share of analysts on the dominant side of the chosen grade.

Thresholds are configurable per instance (buy / overweight / hold / underweight).
                    ''').classes('text-sm')
