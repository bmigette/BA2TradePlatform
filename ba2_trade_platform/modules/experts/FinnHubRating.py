from typing import Any, Dict, Optional
from datetime import datetime, timezone
import json
import requests

from ...core.MarketExpertInterface import MarketExpertInterface
from ...core.models import ExpertInstance, MarketAnalysis, AnalysisOutput, ExpertRecommendation
from ...core.db import get_db, get_instance, update_instance, add_instance, get_setting
from ...core.types import MarketAnalysisStatus, OrderRecommendation, RiskLevel, TimeHorizon
from ...logger import logger


class FinnHubRating(MarketExpertInterface):
    """
    FinnHubRating Expert Implementation
    
    Simple expert that uses Finnhub's recommendation trends API to generate
    trading recommendations based on analyst ratings. Confidence is calculated
    from the buy/sell ratio weighted by strong factor.
    """
    
    @classmethod
    def description(cls) -> str:
        return "Finnhub analyst recommendation trends with weighted confidence scoring"
    
    def __init__(self, id: int):
        """Initialize FinnHubRating expert with database instance."""
        super().__init__(id)
        logger.debug(f'Initializing FinnHubRating expert with instance ID: {id}')
        
        self._load_expert_instance(id)
        self._api_key = self._get_finnhub_api_key()
    
    def _load_expert_instance(self, id: int) -> None:
        """Load and validate expert instance from database."""
        self.instance = get_instance(ExpertInstance, id)
        if not self.instance:
            raise ValueError(f"ExpertInstance with ID {id} not found")
        logger.debug(f'FinnHubRating expert loaded: {self.instance.expert}')
    
    def _get_finnhub_api_key(self) -> Optional[str]:
        """Get Finnhub API key from app settings."""
        api_key = get_setting('finnhub_api_key')
        if not api_key:
            logger.warning("Finnhub API key not found in app settings")
        return api_key
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """Define configurable settings for FinnHubRating expert."""
        return {
            "strong_factor": {
                "type": "float", 
                "required": True, 
                "default": 2.0,
                "description": "Weight multiplier for strong buy/sell ratings",
                "tooltip": "Strong buy and strong sell ratings are multiplied by this factor when calculating confidence. Higher values (2-3) give more weight to strong ratings."
            }
        }
    
    def _fetch_recommendation_trends(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Fetch recommendation trends from Finnhub API.
        
        Args:
            symbol: Stock symbol to query
            
        Returns:
            API response data or None if error
        """
        if not self._api_key:
            logger.error("Cannot fetch recommendations: Finnhub API key not configured")
            return None
        
        try:
            url = "https://finnhub.io/api/v1/stock/recommendation"
            params = {
                "symbol": symbol,
                "token": self._api_key
            }
            
            logger.debug(f"Fetching Finnhub recommendations for {symbol}")
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            logger.debug(f"Received {len(data) if isinstance(data, list) else 0} recommendation periods")
            
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch Finnhub recommendations for {symbol}: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching recommendations for {symbol}: {e}", exc_info=True)
            return None
    
    def _calculate_recommendation(self, trends_data: list, strong_factor: float) -> Dict[str, Any]:
        """
        Calculate trading recommendation from trends data.
        
        Uses the most recent recommendation period and calculates:
        - Buy score: (strong_buy * strong_factor) + buy
        - Sell score: (strong_sell * strong_factor) + sell
        - Confidence: (max(buy_score, sell_score) / total_weighted_recommendations)
        - Signal: BUY if buy_score > sell_score, SELL if sell_score > buy_score, HOLD otherwise
        
        Args:
            trends_data: List of recommendation trend periods from Finnhub
            strong_factor: Weight multiplier for strong ratings
            
        Returns:
            Dictionary with recommendation details
        """
        if not trends_data or len(trends_data) == 0:
            return {
                'signal': OrderRecommendation.HOLD,
                'confidence': 0.0,
                'details': 'No recommendation data available',
                'buy_score': 0,
                'sell_score': 0,
                'hold_count': 0,
                'period': None
            }
        
        # Use the most recent period (first item in list)
        latest = trends_data[0]
        
        # Extract counts
        strong_buy = latest.get('strongBuy', 0)
        buy = latest.get('buy', 0)
        hold = latest.get('hold', 0)
        sell = latest.get('sell', 0)
        strong_sell = latest.get('strongSell', 0)
        period = latest.get('period', 'Unknown')
        
        # Calculate weighted scores (hold is also treated as a score now)
        buy_score = (strong_buy * strong_factor) + buy
        sell_score = (strong_sell * strong_factor) + sell
        hold_score = hold  # Hold ratings are counted as-is
        
        # Total weighted recommendations
        total_weighted = buy_score + sell_score + hold_score
        
        # Determine signal
        if buy_score > sell_score and buy_score > hold_score:
            signal = OrderRecommendation.BUY
            dominant_score = buy_score
        elif sell_score > buy_score and sell_score > hold_score:
            signal = OrderRecommendation.SELL
            dominant_score = sell_score
        else:
            signal = OrderRecommendation.HOLD
            dominant_score = hold_score
        
        # Calculate confidence (ratio of dominant signal to total) - stored as 1-100 scale
        confidence = (dominant_score / total_weighted * 100) if total_weighted > 0 else 0.0
        
        # Build details string
        details = f"""Finnhub Recommendation Trends Analysis (Period: {period})

Analyst Ratings:
- Strong Buy: {strong_buy}
- Buy: {buy}
- Hold: {hold}
- Sell: {sell}
- Strong Sell: {strong_sell}

Weighted Scores (Strong Factor: {strong_factor}x):
- Buy Score: {buy_score:.1f}
- Hold Score: {hold_score:.1f}
- Sell Score: {sell_score:.1f}
- Total Weighted: {total_weighted:.1f}

Recommendation: {signal.value}
Confidence: {confidence:.1f}%

Calculation Method:
Buy Score = (Strong Buy × {strong_factor}) + Buy = ({strong_buy} × {strong_factor}) + {buy} = {buy_score:.1f}
Hold Score = Hold = {hold}
Sell Score = (Strong Sell × {strong_factor}) + Sell = ({strong_sell} × {strong_factor}) + {sell} = {sell_score:.1f}
Confidence = Dominant Score / Total × 100 = {dominant_score:.1f} / {total_weighted:.1f} × 100 = {confidence:.1f}%
"""
        
        return {
            'signal': signal,
            'confidence': confidence,
            'details': details,
            'buy_score': buy_score,
            'sell_score': sell_score,
            'hold_score': hold_score,
            'period': period,
            'raw_data': latest
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
                expected_profit_percent=0.0,  # FinnHub doesn't provide profit targets
                price_at_date=current_price or 0.0,
                details=recommendation_data['details'][:100000] if recommendation_data['details'] else None,
                confidence=round(recommendation_data['confidence'], 1),  # Store as 1-100 scale
                risk_level=RiskLevel.MEDIUM,  # Always medium risk
                time_horizon=TimeHorizon.MEDIUM_TERM,  # Always medium term
                market_analysis_id=market_analysis_id,
                created_at=datetime.now(timezone.utc)
            )
            
            recommendation_id = add_instance(expert_recommendation)
            logger.info(f"Created ExpertRecommendation (ID: {recommendation_id}) for {symbol}: "
                       f"{recommendation_data['signal'].value} with {recommendation_data['confidence']:.1f}% confidence")
            return recommendation_id
            
        except Exception as e:
            logger.error(f"Failed to create ExpertRecommendation for {symbol}: {e}", exc_info=True)
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
            logger.error(f"Failed to store analysis outputs: {e}", exc_info=True)
            raise
        finally:
            session.close()
    
    def _get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price for the symbol from the account."""
        try:
            from ...core.utils import get_account_instance_from_id
            
            expert_instance = get_instance(ExpertInstance, self.id)
            if not expert_instance:
                return None
            
            account = get_account_instance_from_id(expert_instance.account_id)
            if not account:
                return None
            
            return account.get_instrument_current_price(symbol)
            
        except Exception as e:
            logger.error(f"Error getting current price for {symbol}: {e}", exc_info=True)
            return None
    
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
        """
        Run FinnHubRating analysis for a symbol and create ExpertRecommendation.
        
        Args:
            symbol: Financial instrument symbol to analyze
            market_analysis: MarketAnalysis instance to update with results
        """
        logger.info(f"Starting FinnHubRating analysis for {symbol} (Analysis ID: {market_analysis.id})")
        
        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            
            # Get strong factor setting
            strong_factor = self.settings.get('strong_factor', 2.0)
            
            # Fetch recommendation trends from Finnhub
            trends_data = self._fetch_recommendation_trends(symbol)
            
            if trends_data is None:
                raise ValueError("Failed to fetch recommendation trends from Finnhub API")
            
            # Calculate recommendation
            recommendation_data = self._calculate_recommendation(trends_data, strong_factor)
            
            # Get current price
            current_price = self._get_current_price(symbol)
            
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
                'buy_score': recommendation_data['buy_score'],
                'sell_score': recommendation_data['sell_score'],
                'hold_score': recommendation_data['hold_score'],
                'period': str(recommendation_data['period']) if recommendation_data['period'] else None
            }
            
            # Store analysis state
            market_analysis.state = {
                'finnhub_rating': {
                    'recommendation': sanitized_recommendation,
                    'api_response': sanitized_trends,
                    'settings': {
                        'strong_factor': strong_factor
                    },
                    'expert_recommendation_id': recommendation_id,
                    'analysis_timestamp': datetime.now(timezone.utc).isoformat(),
                    'current_price': current_price
                }
            }
            
            # Update status to completed
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            
            logger.info(f"Completed FinnHubRating analysis for {symbol}: {recommendation_data['signal'].value} "
                       f"(confidence: {recommendation_data['confidence']:.1f}%)")
            
        except Exception as e:
            logger.error(f"FinnHubRating analysis failed for {symbol}: {e}", exc_info=True)
            
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
                logger.error(f"Failed to store error output: {db_error}", exc_info=True)
            
            raise
    
    def render_market_analysis(self, market_analysis: MarketAnalysis) -> None:
        """
        Render FinnHubRating market analysis results in the UI.
        
        Args:
            market_analysis: The market analysis instance to render.
        """
        from nicegui import ui
        
        try:
            # Handle different analysis states
            if market_analysis.status == MarketAnalysisStatus.PENDING:
                self._render_pending(market_analysis)
            elif market_analysis.status == MarketAnalysisStatus.RUNNING:
                self._render_running(market_analysis)
            elif market_analysis.status == MarketAnalysisStatus.FAILED:
                self._render_failed(market_analysis)
            elif market_analysis.status == MarketAnalysisStatus.COMPLETED:
                self._render_completed(market_analysis)
            else:
                with ui.card().classes('w-full p-4'):
                    ui.label(f"Unknown analysis status: {market_analysis.status}")
                    
        except Exception as e:
            logger.error(f"Error rendering market analysis {market_analysis.id}: {e}", exc_info=True)
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('error', size='3rem', color='negative').classes('mb-4')
                ui.label('Rendering Error').classes('text-h5 text-negative')
                ui.label(f'Failed to render analysis: {str(e)}').classes('text-grey-7')
    
    def _render_pending(self, market_analysis: MarketAnalysis) -> None:
        """Render pending analysis state."""
        from nicegui import ui
        
        with ui.card().classes('w-full p-8 text-center'):
            ui.icon('schedule', size='3rem', color='grey').classes('mb-4')
            ui.label('Analysis Pending').classes('text-h5')
            ui.label(f'FinnHubRating analysis for {market_analysis.symbol} is queued').classes('text-grey-7')
    
    def _render_running(self, market_analysis: MarketAnalysis) -> None:
        """Render running analysis state."""
        from nicegui import ui
        
        with ui.card().classes('w-full p-8 text-center'):
            ui.spinner(size='3rem', color='primary').classes('mb-4')
            ui.label('Analysis Running').classes('text-h5')
            ui.label(f'Fetching analyst recommendations for {market_analysis.symbol}...').classes('text-grey-7')
    
    def _render_failed(self, market_analysis: MarketAnalysis) -> None:
        """Render failed analysis state."""
        from nicegui import ui
        
        with ui.card().classes('w-full p-4'):
            with ui.row().classes('items-center mb-4'):
                ui.icon('error', color='negative', size='2rem')
                ui.label('Analysis Failed').classes('text-h5 text-negative ml-2')
            
            if market_analysis.state and isinstance(market_analysis.state, dict):
                error_msg = market_analysis.state.get('error', 'Unknown error')
                ui.label(f'Error: {error_msg}').classes('text-grey-8')
    
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
        with ui.card().classes('w-full'):
            # Header with recommendation
            with ui.card_section().classes('bg-blue-1'):
                ui.label('FinnHub Analyst Recommendations').classes('text-h5 text-weight-bold')
                ui.label(f'{market_analysis.symbol} - Analyst Consensus').classes('text-grey-7')
            
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
                        ui.label('Recommendation').classes('text-grey-6 text-caption')
                        with ui.row().classes('items-center gap-2'):
                            ui.icon(signal_icon, color=signal_color, size='2rem')
                            ui.label(signal_text).classes(f'text-h4 text-{signal_color}')
                    
                    with ui.column().classes('text-right'):
                        ui.label('Confidence').classes('text-grey-6 text-caption')
                        ui.label(f'{confidence:.1f}%').classes('text-h4')
                
                if current_price:
                    ui.separator().classes('my-2')
                    ui.label(f'Current Price: ${current_price:.2f}').classes('text-grey-7')
            
            # Ratings breakdown
            if api_data and len(api_data) > 0:
                latest = api_data[0]
                period = latest.get('period', 'Unknown')
                
                with ui.card_section().classes('bg-grey-1'):
                    ui.label(f'Analyst Ratings - {period}').classes('text-subtitle1 text-weight-medium mb-2')
                    
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
                                ui.label('Strong Buy').classes('w-24 text-right text-sm')
                                ui.label(str(strong_buy)).classes('w-8 text-sm font-bold')
                                with ui.element('div').classes('flex-grow bg-grey-3 rounded overflow-hidden h-6'):
                                    ui.element('div').classes('bg-green-600 h-full').style(f'width: {pct}%')
                        
                        # Buy
                        if buy > 0:
                            pct = (buy / total * 100) if total > 0 else 0
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label('Buy').classes('w-24 text-right text-sm')
                                ui.label(str(buy)).classes('w-8 text-sm font-bold')
                                with ui.element('div').classes('flex-grow bg-grey-3 rounded overflow-hidden h-6'):
                                    ui.element('div').classes('bg-green-400 h-full').style(f'width: {pct}%')
                        
                        # Hold
                        if hold > 0:
                            pct = (hold / total * 100) if total > 0 else 0
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label('Hold').classes('w-24 text-right text-sm')
                                ui.label(str(hold)).classes('w-8 text-sm font-bold')
                                with ui.element('div').classes('flex-grow bg-grey-3 rounded overflow-hidden h-6'):
                                    ui.element('div').classes('bg-grey-500 h-full').style(f'width: {pct}%')
                        
                        # Sell
                        if sell > 0:
                            pct = (sell / total * 100) if total > 0 else 0
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label('Sell').classes('w-24 text-right text-sm')
                                ui.label(str(sell)).classes('w-8 text-sm font-bold')
                                with ui.element('div').classes('flex-grow bg-grey-3 rounded overflow-hidden h-6'):
                                    ui.element('div').classes('bg-red-400 h-full').style(f'width: {pct}%')
                        
                        # Strong Sell
                        if strong_sell > 0:
                            pct = (strong_sell / total * 100) if total > 0 else 0
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label('Strong Sell').classes('w-24 text-right text-sm')
                                ui.label(str(strong_sell)).classes('w-8 text-sm font-bold')
                                with ui.element('div').classes('flex-grow bg-grey-3 rounded overflow-hidden h-6'):
                                    ui.element('div').classes('bg-red-600 h-full').style(f'width: {pct}%')
                    
                    ui.separator().classes('my-2')
                    ui.label(f'Total Analysts: {total}').classes('text-sm text-grey-7')
            
            # Weighted scores
            buy_score = rec.get('buy_score', 0)
            sell_score = rec.get('sell_score', 0)
            hold_score = rec.get('hold_score', 0)
            strong_factor = settings.get('strong_factor', 2.0)
            
            with ui.card_section():
                ui.label('Weighted Scoring').classes('text-subtitle1 text-weight-medium mb-2')
                ui.label(f'Strong Factor: {strong_factor}x').classes('text-sm text-grey-7 mb-2')
                
                with ui.grid(columns=3).classes('w-full gap-4'):
                    with ui.card().classes('bg-green-50'):
                        ui.label('Buy Score').classes('text-caption text-grey-7')
                        ui.label(f'{buy_score:.1f}').classes('text-h5 text-green-700')
                    
                    with ui.card().classes('bg-grey-50'):
                        ui.label('Hold Score').classes('text-caption text-grey-7')
                        ui.label(f'{hold_score:.1f}').classes('text-h5 text-grey-700')
                    
                    with ui.card().classes('bg-red-50'):
                        ui.label('Sell Score').classes('text-caption text-grey-7')
                        ui.label(f'{sell_score:.1f}').classes('text-h5 text-red-700')
            
            # Methodology
            with ui.expansion('Calculation Methodology', icon='info').classes('w-full'):
                with ui.card_section().classes('bg-grey-1'):
                    ui.markdown('''
**Confidence Calculation:**

The confidence score is calculated by weighting strong buy/sell ratings and comparing all scores:

1. **Buy Score** = (Strong Buy × Strong Factor) + Buy
2. **Hold Score** = Hold
3. **Sell Score** = (Strong Sell × Strong Factor) + Sell  
4. **Total Weighted** = Buy Score + Hold Score + Sell Score
5. **Confidence** = Dominant Score / Total Weighted

**Recommendation Logic:**
- **BUY**: Buy Score > Sell Score AND Buy Score > Hold Score
- **SELL**: Sell Score > Buy Score AND Sell Score > Hold Score
- **HOLD**: Otherwise (Hold Score is highest)

**Strong Factor**: Multiplier for strong buy/sell ratings (default: 2x)
                    ''').classes('text-sm')
