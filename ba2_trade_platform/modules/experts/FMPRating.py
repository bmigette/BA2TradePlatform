from typing import Any, Dict, Optional
from datetime import datetime, timezone
import json
import requests

from ...core.interfaces import MarketExpertInterface
from ...core.models import ExpertInstance, MarketAnalysis, AnalysisOutput, ExpertRecommendation
from ...core.db import get_db, get_instance, update_instance, add_instance, get_setting
from ...core.types import MarketAnalysisStatus, OrderRecommendation, RiskLevel, TimeHorizon
from ...logger import get_expert_logger
from ...config import get_app_setting


class FMPRating(MarketExpertInterface):
    """
    FMPRating Expert Implementation
    
    Expert that uses FMP's analyst price target consensus and upgrade/downgrade data
    to generate trading recommendations. Calculates expected profit based on price
    targets weighted by analyst confidence and configurable profit ratio.
    """
    
    @classmethod
    def description(cls) -> str:
        return "FMP analyst price consensus with profit potential calculation"
    
    def __init__(self, id: int):
        """Initialize FMPRating expert with database instance."""
        super().__init__(id)
        
        self._load_expert_instance(id)
        self._api_key = self._get_fmp_api_key()
        
        # Initialize expert-specific logger
        self.logger = get_expert_logger("FMPRating", id)
    
    def _load_expert_instance(self, id: int) -> None:
        """Load and validate expert instance from database."""
        self.instance = get_instance(ExpertInstance, id)
        if not self.instance:
            raise ValueError(f"ExpertInstance with ID {id} not found")
    
    def _get_fmp_api_key(self) -> Optional[str]:
        """Get FMP API key from app settings."""
        api_key = get_app_setting('FMP_API_KEY')
        if not api_key:
            self.logger.warning("FMP API key not found in app settings")
        return api_key
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """Define configurable settings for FMPRating expert."""
        return {
            "profit_ratio": {
                "type": "float", 
                "required": True, 
                "default": 1.0,
                "description": "Profit ratio multiplier for expected profit calculation",
                "tooltip": "Multiplier applied to the weighted price target delta. Default 1.0 means use full analyst consensus range. Lower values (0.5-0.8) are more conservative."
            },
            "min_analysts": {
                "type": "int",
                "required": True,
                "default": 3,
                "description": "Minimum number of analysts required for valid recommendation",
                "tooltip": "Recommendations with fewer analysts than this threshold will result in HOLD with low confidence."
            }
        }
    
    def _fetch_price_target_consensus(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Fetch price target consensus from FMP API.
        
        Args:
            symbol: Stock symbol to query
            
        Returns:
            API response data or None if error
        """
        if not self._api_key:
            self.logger.error("Cannot fetch price target consensus: FMP API key not configured")
            return None
        
        try:
            url = f"https://financialmodelingprep.com/api/v4/price-target-consensus"
            params = {
                "symbol": symbol,
                "apikey": self._api_key
            }
            
            self.logger.debug(f"Fetching FMP price target consensus for {symbol}")
            
            # Retry logic with 60s timeout
            max_retries = 3
            timeout = 60
            response = None
            
            for attempt in range(1, max_retries + 1):
                try:
                    response = requests.get(url, params=params, timeout=timeout)
                    response.raise_for_status()
                    break  # Success
                except requests.exceptions.ReadTimeout:
                    if attempt < max_retries:
                        self.logger.warning(f"FMP price target consensus timeout for {symbol} (attempt {attempt}/{max_retries}), retrying...")
                        continue
                    else:
                        error_msg = f"Failed to fetch price target consensus for {symbol} after {max_retries} attempts (timeout)"
                        self.logger.error(error_msg)
                        raise ValueError(error_msg)
                except requests.exceptions.RequestException as e:
                    error_msg = f"Failed to fetch FMP price target consensus for {symbol}: {e}"
                    self.logger.error(error_msg)
                    raise ValueError(error_msg) from e
            
            data = response.json()
            self.logger.debug(f"Received price target consensus data for {symbol}")
            
            # FMP returns a list with one item, extract the first element
            if isinstance(data, list) and len(data) > 0:
                return data[0]
            elif isinstance(data, dict):
                return data
            else:
                self.logger.warning(f"Unexpected price target consensus format for {symbol}: {type(data)}")
                return None
            
        except ValueError:
            # Re-raise ValueError (from our error handling above)
            raise
        except Exception as e:
            error_msg = f"Unexpected error fetching price target consensus for {symbol}: {e}"
            self.logger.error(error_msg)
            raise ValueError(error_msg) from e
    
    def _fetch_upgrade_downgrade(self, symbol: str) -> Optional[list]:
        """
        Fetch analyst upgrade/downgrade summary from FMP API.
        
        Args:
            symbol: Stock symbol to query
            
        Returns:
            API response data or None if error
        """
        if not self._api_key:
            self.logger.error("Cannot fetch upgrade/downgrade data: FMP API key not configured")
            return None
        
        try:
            url = f"https://financialmodelingprep.com/api/v4/upgrades-downgrades-consensus"
            params = {
                "symbol": symbol,
                "apikey": self._api_key
            }
            
            self.logger.debug(f"Fetching FMP upgrade/downgrade consensus for {symbol}")
            
            # Retry logic with 60s timeout
            max_retries = 3
            timeout = 60
            response = None
            
            for attempt in range(1, max_retries + 1):
                try:
                    response = requests.get(url, params=params, timeout=timeout)
                    response.raise_for_status()
                    break  # Success
                except requests.exceptions.ReadTimeout:
                    if attempt < max_retries:
                        self.logger.warning(f"FMP upgrade/downgrade timeout for {symbol} (attempt {attempt}/{max_retries}), retrying...")
                        continue
                    else:
                        error_msg = f"Failed to fetch upgrade/downgrade for {symbol} after {max_retries} attempts (timeout)"
                        self.logger.error(error_msg)
                        raise ValueError(error_msg)
                except requests.exceptions.RequestException as e:
                    error_msg = f"Failed to fetch FMP upgrade/downgrade for {symbol}: {e}"
                    self.logger.error(error_msg)
                    raise ValueError(error_msg) from e
            
            data = response.json()
            self.logger.debug(f"Received {len(data) if isinstance(data, list) else 0} upgrade/downgrade records")
            
            # Return the list as-is (we'll use it to count analysts)
            return data if isinstance(data, list) else None
            
        except ValueError:
            # Re-raise ValueError (from our error handling above)
            raise
        except Exception as e:
            error_msg = f"Unexpected error fetching upgrade/downgrade for {symbol}: {e}"
            self.logger.error(error_msg)
            raise ValueError(error_msg) from e
    
    def _calculate_recommendation(self, consensus_data: Dict[str, Any], 
                                 upgrade_data: list,
                                 current_price: float,
                                 profit_ratio: float,
                                 min_analysts: int) -> Dict[str, Any]:
        """
        Calculate trading recommendation from price target consensus.
        
        New Formula (matching FinnHub methodology with price target boost):
        1. Calculate base score from analyst buy/sell ratings (FinnHub style)
        2. Determine signal based on dominant rating
        3. Calculate price target boost from current price to lower/consensus targets
        4. Average the boosts and add to base confidence
        5. Clamp final confidence to 0-100%
        
        Args:
            consensus_data: Price target consensus from FMP
            upgrade_data: Upgrade/downgrade data from FMP
            current_price: Current stock price
            profit_ratio: Profit ratio multiplier setting
            min_analysts: Minimum analysts required
            
        Returns:
            Dictionary with recommendation details
        """
        if not consensus_data:
            return {
                'signal': OrderRecommendation.HOLD,
                'confidence': 0.0,
                'expected_profit_percent': 0.0,
                'details': 'No price target consensus data available',
                'target_consensus': None,
                'target_high': None,
                'target_low': None,
                'target_median': None,
                'analyst_count': 0
            }
        
        # Extract consensus data
        target_consensus = consensus_data.get('targetConsensus')
        target_high = consensus_data.get('targetHigh')
        target_low = consensus_data.get('targetLow')
        target_median = consensus_data.get('targetMedian')
        
        # Get analyst ratings from upgrade/downgrade data
        analyst_count = 0
        strong_buy = 0
        buy = 0
        hold = 0
        sell = 0
        strong_sell = 0
        
        if upgrade_data and len(upgrade_data) > 0:
            latest_grade = upgrade_data[0]
            # Sum all rating categories to get total analyst count
            strong_buy = latest_grade.get('strongBuy', 0)
            buy = latest_grade.get('buy', 0)
            hold = latest_grade.get('hold', 0)
            sell = latest_grade.get('sell', 0)
            strong_sell = latest_grade.get('strongSell', 0)
            analyst_count = strong_buy + buy + hold + sell + strong_sell
        
        # Check minimum analysts threshold
        if analyst_count < min_analysts:
            return {
                'signal': OrderRecommendation.HOLD,
                'confidence': 20.0,  # Low confidence due to insufficient data
                'expected_profit_percent': 0.0,
                'details': f'Insufficient analyst coverage ({analyst_count} analysts, minimum {min_analysts} required)',
                'target_consensus': target_consensus,
                'target_high': target_high,
                'target_low': target_low,
                'target_median': target_median,
                'analyst_count': analyst_count
            }
        
        # === NEW CONFIDENCE CALCULATION (FinnHub style with price target boost) ===
        
        # Step 1: Calculate base score from analyst ratings (same as FinnHub)
        strong_factor = 2.0  # Weight for strong ratings
        buy_score = (strong_buy * strong_factor) + buy
        sell_score = (strong_sell * strong_factor) + sell
        hold_score = hold
        
        total_weighted = buy_score + sell_score + hold_score
        
        # Step 2: Determine signal and base confidence from ratings
        if buy_score > sell_score and buy_score > hold_score:
            signal = OrderRecommendation.BUY
            dominant_score = buy_score
            target_price = target_high if target_high else target_consensus
        elif sell_score > buy_score and sell_score > hold_score:
            signal = OrderRecommendation.SELL
            dominant_score = sell_score
            target_price = target_low if target_low else target_consensus
        else:
            signal = OrderRecommendation.HOLD
            dominant_score = hold_score
            target_price = target_consensus
        
        # Base confidence from analyst consensus (0-100 scale)
        base_confidence = (dominant_score / total_weighted * 100) if total_weighted > 0 else 0.0
        
        # Step 3: Calculate price target boost
        price_target_boost = 0.0
        boost_to_lower = 0.0
        boost_to_consensus = 0.0
        
        if current_price and target_low and target_consensus:
            # Calculate expected profit % if we hit lower target
            if current_price > target_low:
                # Current price is above lower target - negative boost (bearish)
                boost_to_lower = ((target_low - current_price) / current_price) * 100
            else:
                # Current price is below lower target - positive boost (bullish)
                boost_to_lower = ((target_low - current_price) / current_price) * 100
            
            # Calculate expected profit % if we hit consensus target
            if current_price > target_consensus:
                # Current price is above consensus - negative boost (bearish)
                boost_to_consensus = ((target_consensus - current_price) / current_price) * 100
            else:
                # Current price is below consensus - positive boost (bullish)
                boost_to_consensus = ((target_consensus - current_price) / current_price) * 100
            
            # Average the two boosts
            price_target_boost = (boost_to_lower + boost_to_consensus) / 2.0
        
        # Step 4: Apply price target boost to base confidence
        # Positive boost increases confidence, negative boost decreases it
        confidence = base_confidence + price_target_boost
        
        # Step 5: Clamp final confidence to 0-100%
        confidence = max(0.0, min(100.0, confidence))
        
        # Calculate expected profit
        if signal == OrderRecommendation.BUY and target_price and current_price:
            # Profit potential: (target - current) * confidence * profit_ratio
            price_delta = target_price - current_price
            weighted_delta = price_delta * (confidence / 100.0) * profit_ratio
            expected_profit_percent = (weighted_delta / current_price) * 100
        elif signal == OrderRecommendation.SELL and target_price and current_price:
            # For SELL, profit is from current to low target
            price_delta = current_price - target_price
            weighted_delta = price_delta * (confidence / 100.0) * profit_ratio
            expected_profit_percent = (weighted_delta / current_price) * 100
        else:
            expected_profit_percent = 0.0
        
        # Build details string
        details = f"""FMP Analyst Price Target Consensus Analysis

Current Price: ${current_price:.2f}

Analyst Ratings:
- Strong Buy: {strong_buy}
- Buy: {buy}
- Hold: {hold}
- Sell: {sell}
- Strong Sell: {strong_sell}
Total Analysts: {analyst_count}

Price Targets:
- Consensus Target: ${target_consensus:.2f} ({((target_consensus - current_price) / current_price * 100):.1f}% from current)
- High Target: ${target_high:.2f} ({((target_high - current_price) / current_price * 100):.1f}% from current)
- Low Target: ${target_low:.2f} ({((target_low - current_price) / current_price * 100):.1f}% from current)
- Median Target: ${target_median:.2f} ({((target_median - current_price) / current_price * 100):.1f}% from current)

Recommendation: {signal.value}
Confidence: {confidence:.1f}%
Expected Profit: {expected_profit_percent:.1f}%

Confidence Calculation (FinnHub Methodology + Price Target Boost):

Step 1 - Weighted Scores (Strong Factor: {strong_factor}x):
Buy Score = (Strong Buy × {strong_factor}) + Buy = ({strong_buy} × {strong_factor}) + {buy} = {buy_score:.1f}
Hold Score = Hold = {hold}
Sell Score = (Strong Sell × {strong_factor}) + Sell = ({strong_sell} × {strong_factor}) + {sell} = {sell_score:.1f}
Total Weighted = {total_weighted:.1f}

Step 2 - Base Confidence from Analyst Ratings:
Base Confidence = Dominant Score / Total × 100 = {dominant_score:.1f} / {total_weighted:.1f} × 100 = {base_confidence:.1f}%

Step 3 - Price Target Boost:
Boost to Lower Target = ((${target_low:.2f} - ${current_price:.2f}) / ${current_price:.2f}) × 100 = {boost_to_lower:.1f}%
Boost to Consensus = ((${target_consensus:.2f} - ${current_price:.2f}) / ${current_price:.2f}) × 100 = {boost_to_consensus:.1f}%
Avg Price Target Boost = ({boost_to_lower:.1f}% + {boost_to_consensus:.1f}%) / 2 = {price_target_boost:.1f}%

Step 4 - Final Confidence (clamped to 0-100%):
Final Confidence = Base Confidence + Avg Boost = {base_confidence:.1f}% + {price_target_boost:.1f}% = {confidence:.1f}%

Expected Profit Calculation:
Price Delta = {'High' if signal == OrderRecommendation.BUY else 'Low'} Target - Current = ${target_price:.2f} - ${current_price:.2f} = ${target_price - current_price:.2f}
Weighted Delta = Price Delta × Confidence × Profit Ratio = ${target_price - current_price:.2f} × {confidence/100:.2f} × {profit_ratio} = ${(target_price - current_price) * (confidence/100) * profit_ratio:.2f}
Expected Profit % = (Weighted Delta / Current) × 100 = {expected_profit_percent:.1f}%
"""
        
        return {
            'signal': signal,
            'confidence': confidence,
            'expected_profit_percent': expected_profit_percent,
            'details': details,
            'target_consensus': target_consensus,
            'target_high': target_high,
            'target_low': target_low,
            'target_median': target_median,
            'analyst_count': analyst_count,
            # New calculation components
            'strong_buy': strong_buy,
            'buy': buy,
            'hold': hold,
            'sell': sell,
            'strong_sell': strong_sell,
            'buy_score': buy_score,
            'sell_score': sell_score,
            'hold_score': hold_score,
            'base_confidence': base_confidence,
            'boost_to_lower': boost_to_lower,
            'boost_to_consensus': boost_to_consensus,
            'price_target_boost': price_target_boost,
            'target_price': target_price
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
                expected_profit_percent=recommendation_data['expected_profit_percent'],
                price_at_date=current_price or 0.0,
                details=recommendation_data['details'][:100000] if recommendation_data['details'] else None,
                confidence=round(recommendation_data['confidence'], 1),  # Store as 1-100 scale
                risk_level=RiskLevel.MEDIUM,  # Always medium risk
                time_horizon=TimeHorizon.MEDIUM_TERM,  # Always medium term
                market_analysis_id=market_analysis_id,
                created_at=datetime.now(timezone.utc)
            )
            
            recommendation_id = add_instance(expert_recommendation)
            self.logger.info(f"Created ExpertRecommendation (ID: {recommendation_id}) for {symbol}: "
                       f"{recommendation_data['signal'].value} with {recommendation_data['confidence']:.1f}% confidence, "
                       f"expected profit: {recommendation_data['expected_profit_percent']:.1f}%")
            return recommendation_id
            
        except Exception as e:
            self.logger.error(f"Failed to create ExpertRecommendation for {symbol}: {e}", exc_info=True)
            raise
    
    def _store_analysis_outputs(self, market_analysis_id: int, symbol: str,
                               recommendation_data: Dict[str, Any],
                               consensus_data: Dict[str, Any],
                               upgrade_data: list) -> None:
        """Store detailed analysis outputs in database."""
        session = get_db()
        
        try:
            # Store analysis details
            details_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="FMP Price Target Analysis",
                type="fmp_rating_analysis",
                text=recommendation_data['details']
            )
            session.add(details_output)
            
            # Store price targets as structured output
            targets_text = f"""Analyst Price Targets:
- Consensus: ${recommendation_data['target_consensus']:.2f}
- High: ${recommendation_data['target_high']:.2f}
- Low: ${recommendation_data['target_low']:.2f}
- Median: ${recommendation_data['target_median']:.2f}
- Analysts: {recommendation_data['analyst_count']}"""
            
            targets_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Price Targets",
                type="price_targets",
                text=targets_text
            )
            session.add(targets_output)
            
            # Store full consensus API response
            consensus_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="FMP Consensus API Response",
                type="fmp_consensus_response",
                text=json.dumps(consensus_data, indent=2)
            )
            session.add(consensus_output)
            
            # Store upgrade/downgrade data if available
            if upgrade_data:
                upgrade_output = AnalysisOutput(
                    market_analysis_id=market_analysis_id,
                    name="FMP Upgrade/Downgrade Data",
                    type="fmp_upgrade_downgrade",
                    text=json.dumps(upgrade_data, indent=2)
                )
                session.add(upgrade_output)
            
            session.commit()
            
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to store analysis outputs: {e}", exc_info=True)
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
            self.logger.error(f"Error getting current price for {symbol}: {e}", exc_info=True)
            return None
    
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """
        Run FMPRating analysis for a symbol and create ExpertRecommendation.
        
        Args:
            symbol: Financial instrument symbol to analyze
            market_analysis: MarketAnalysis instance to update with results
        """
        self.logger.info(f"Starting FMPRating analysis for {symbol} (Analysis ID: {market_analysis.id})")
        
        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            
            # Get settings
            profit_ratio = float(self.settings.get('profit_ratio', 1.0))
            min_analysts = int(self.settings.get('min_analysts', 3))
            
            # Get current price first (needed for calculation)
            current_price = self._get_current_price(symbol)
            if not current_price:
                raise ValueError(f"Unable to get current price for {symbol}")
            
            # Fetch price target consensus from FMP
            consensus_data = self._fetch_price_target_consensus(symbol)
            
            if consensus_data is None:
                raise ValueError("Failed to fetch price target consensus from FMP API")
            
            # Fetch upgrade/downgrade data
            upgrade_data = self._fetch_upgrade_downgrade(symbol)
            
            # Calculate recommendation
            recommendation_data = self._calculate_recommendation(
                consensus_data, upgrade_data, current_price, profit_ratio, min_analysts
            )
            
            # Create ExpertRecommendation record
            recommendation_id = self._create_expert_recommendation(
                recommendation_data, symbol, market_analysis.id, current_price
            )
            
            # Store analysis outputs
            self._store_analysis_outputs(
                market_analysis.id, symbol, recommendation_data, 
                consensus_data, upgrade_data or []
            )
            
            # Store analysis state
            market_analysis.state = {
                'fmp_rating': {
                    'recommendation': {
                        'signal': recommendation_data['signal'].value,
                        'confidence': recommendation_data['confidence'],
                        'expected_profit_percent': recommendation_data['expected_profit_percent'],
                        'details': recommendation_data['details']
                    },
                    'price_targets': {
                        'consensus': recommendation_data['target_consensus'],
                        'high': recommendation_data['target_high'],
                        'low': recommendation_data['target_low'],
                        'median': recommendation_data['target_median'],
                        'analyst_count': recommendation_data['analyst_count']
                    },
                    'analyst_breakdown': {
                        'strong_buy': recommendation_data.get('strong_buy', 0),
                        'buy': recommendation_data.get('buy', 0),
                        'hold': recommendation_data.get('hold', 0),
                        'sell': recommendation_data.get('sell', 0),
                        'strong_sell': recommendation_data.get('strong_sell', 0)
                    },
                    'confidence_breakdown': {
                        # New calculation components (FinnHub methodology + price target boost)
                        'base_confidence': recommendation_data.get('base_confidence', 0),
                        'price_target_boost': recommendation_data.get('price_target_boost', 0),
                        'boost_to_lower': recommendation_data.get('boost_to_lower', 0),
                        'boost_to_consensus': recommendation_data.get('boost_to_consensus', 0),
                        'buy_score': recommendation_data.get('buy_score', 0),
                        'sell_score': recommendation_data.get('sell_score', 0),
                        'hold_score': recommendation_data.get('hold_score', 0),
                    },
                    'consensus_data': consensus_data,
                    'upgrade_data': upgrade_data,
                    'settings': {
                        'profit_ratio': profit_ratio,
                        'min_analysts': min_analysts
                    },
                    'expert_recommendation_id': recommendation_id,
                    'analysis_timestamp': datetime.now(timezone.utc).isoformat(),
                    'current_price': current_price
                }
            }
            
            # Update status to completed
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            
            self.logger.info(f"Completed FMPRating analysis for {symbol}: {recommendation_data['signal'].value} "
                       f"(confidence: {recommendation_data['confidence']:.1f}%, "
                       f"expected profit: {recommendation_data['expected_profit_percent']:.1f}%)")
            
        except Exception as e:
            self.logger.error(f"FMPRating analysis failed for {symbol}: {e}", exc_info=True)
            
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
                    text=f"FMPRating analysis failed for {symbol}: {str(e)}"
                )
                session.add(error_output)
                session.commit()
                session.close()
            except Exception as db_error:
                self.logger.error(f"Failed to store error output: {db_error}", exc_info=True)
            
            raise
    
    def render_market_analysis(self, market_analysis: MarketAnalysis) -> None:
        """
        Render FMPRating market analysis results in the UI.
        
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
            elif market_analysis.status == MarketAnalysisStatus.SKIPPED:
                self._render_skipped(market_analysis)
            else:
                with ui.card().classes('w-full p-4'):
                    ui.label(f"Unknown analysis status: {market_analysis.status}")
                    
        except Exception as e:
            self.logger.error(f"Error rendering market analysis {market_analysis.id}: {e}", exc_info=True)
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
            ui.label(f'FMPRating analysis for {market_analysis.symbol} is queued').classes('text-grey-7')
    
    def _render_running(self, market_analysis: MarketAnalysis) -> None:
        """Render running analysis state."""
        from nicegui import ui
        
        with ui.card().classes('w-full p-8 text-center'):
            ui.spinner(size='3rem', color='primary').classes('mb-4')
            ui.label('Analysis Running').classes('text-h5')
            ui.label(f'Fetching analyst price targets for {market_analysis.symbol}...').classes('text-grey-7')
    
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
    
    def _render_skipped(self, market_analysis: MarketAnalysis) -> None:
        """Render skipped analysis state."""
        from nicegui import ui
        
        with ui.card().classes('w-full p-4'):
            with ui.row().classes('items-center mb-4'):
                ui.icon('skip_next', color='orange', size='2rem')
                ui.label('Analysis Skipped').classes('text-h5 text-orange ml-2')
            
            if market_analysis.state and isinstance(market_analysis.state, dict):
                skip_reason = market_analysis.state.get('skip_reason', 'Analysis was skipped')
                ui.label(f'Reason: {skip_reason}').classes('text-grey-8')
    
    def _render_completed(self, market_analysis: MarketAnalysis) -> None:
        """Render completed analysis with beautiful UI."""
        from nicegui import ui
        
        if not market_analysis.state or 'fmp_rating' not in market_analysis.state:
            with ui.card().classes('w-full p-4'):
                ui.label('No analysis data available').classes('text-grey-7')
            return
        
        state = market_analysis.state['fmp_rating']
        rec = state.get('recommendation', {})
        targets = state.get('price_targets', {})
        settings = state.get('settings', {})
        current_price = state.get('current_price')
        
        # Main card
        with ui.card().classes('w-full'):
            # Header with recommendation
            with ui.card_section().classes('bg-blue-1'):
                ui.label('FMP Analyst Price Target Consensus').classes('text-h5 text-weight-bold')
                ui.label(f'{market_analysis.symbol} - Price Target Analysis').classes('text-grey-7')
            
            # Recommendation summary
            signal = rec.get('signal', 'HOLD')
            confidence = rec.get('confidence', 0.0)
            expected_profit = rec.get('expected_profit_percent', 0.0)
            
            # Color based on signal
            if signal == 'BUY':
                signal_color = 'positive'
                signal_icon = 'trending_up'
            elif signal == 'SELL':
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
                            ui.label(signal).classes(f'text-h4 text-{signal_color}')
                    
                    with ui.column().classes('text-right'):
                        ui.label('Confidence').classes('text-grey-6 text-caption')
                        ui.label(f'{confidence:.1f}%').classes('text-h4')
                    
                    with ui.column().classes('text-right'):
                        ui.label('Expected Profit').classes('text-grey-6 text-caption')
                        profit_color = 'positive' if expected_profit > 0 else 'negative' if expected_profit < 0 else 'grey'
                        ui.label(f'{expected_profit:+.1f}%').classes(f'text-h4 text-{profit_color}')
                
                if current_price:
                    ui.separator().classes('my-2')
                    ui.label(f'Current Price: ${current_price:.2f}').classes('text-grey-7')
            
            # Price Targets
            consensus = targets.get('consensus')
            high = targets.get('high')
            low = targets.get('low')
            median = targets.get('median')
            analyst_count = targets.get('analyst_count', 0)
            
            if consensus and high and low:
                with ui.card_section().classes('bg-grey-1'):
                    ui.label(f'Analyst Price Targets ({analyst_count} analysts)').classes('text-subtitle1 text-weight-medium mb-3')
                    
                    with ui.grid(columns=2).classes('w-full gap-4'):
                        # Consensus Target
                        with ui.card().classes('bg-blue-50'):
                            ui.label('Consensus Target').classes('text-caption text-grey-7')
                            ui.label(f'${consensus:.2f}').classes('text-h5 text-blue-700')
                            if current_price:
                                delta_pct = ((consensus - current_price) / current_price) * 100
                                delta_color = 'positive' if delta_pct > 0 else 'negative'
                                ui.label(f'{delta_pct:+.1f}% from current').classes(f'text-xs text-{delta_color}')
                        
                        # Median Target
                        with ui.card().classes('bg-grey-50'):
                            ui.label('Median Target').classes('text-caption text-grey-7')
                            ui.label(f'${median:.2f}').classes('text-h5 text-grey-700')
                            if current_price:
                                delta_pct = ((median - current_price) / current_price) * 100
                                delta_color = 'positive' if delta_pct > 0 else 'negative'
                                ui.label(f'{delta_pct:+.1f}% from current').classes(f'text-xs text-{delta_color}')
                        
                        # High Target
                        with ui.card().classes('bg-green-50'):
                            ui.label('High Target').classes('text-caption text-grey-7')
                            ui.label(f'${high:.2f}').classes('text-h5 text-green-700')
                            if current_price:
                                delta_pct = ((high - current_price) / current_price) * 100
                                ui.label(f'{delta_pct:+.1f}% upside').classes('text-xs text-positive')
                        
                        # Low Target
                        with ui.card().classes('bg-red-50'):
                            ui.label('Low Target').classes('text-caption text-grey-7')
                            ui.label(f'${low:.2f}').classes('text-h5 text-red-700')
                            if current_price:
                                delta_pct = ((low - current_price) / current_price) * 100
                                ui.label(f'{delta_pct:+.1f}% downside').classes('text-xs text-negative')
                    
                    # Target range visualization
                    if current_price:
                        ui.separator().classes('my-3')
                        ui.label('Price Range').classes('text-caption text-grey-7 mb-2')
                        
                        # Calculate positions for visualization
                        price_range = high - low
                        current_pos = ((current_price - low) / price_range * 100) if price_range > 0 else 50
                        consensus_pos = ((consensus - low) / price_range * 100) if price_range > 0 else 50
                        
                        with ui.element('div').classes('relative w-full h-12 bg-grey-3 rounded'):
                            # Low to High gradient background
                            ui.element('div').classes('absolute inset-0 bg-gradient-to-r from-red-200 via-grey-200 to-green-200 rounded')
                            
                            # Current price marker
                            with ui.element('div').classes('absolute top-0 bottom-0 w-1 bg-blue-600').style(f'left: {current_pos}%'):
                                with ui.element('div').classes('absolute -top-6 left-1/2 transform -translate-x-1/2'):
                                    ui.label('Current').classes('text-xs font-bold text-blue-600')
                            
                            # Consensus marker
                            with ui.element('div').classes('absolute top-0 bottom-0 w-1 bg-orange-600').style(f'left: {consensus_pos}%'):
                                with ui.element('div').classes('absolute -bottom-6 left-1/2 transform -translate-x-1/2'):
                                    ui.label('Target').classes('text-xs font-bold text-orange-600')
                        
                        with ui.row().classes('w-full justify-between mt-8'):
                            ui.label(f'${low:.2f}').classes('text-xs text-grey-6')
                            ui.label(f'${high:.2f}').classes('text-xs text-grey-6')
            
            # Analyst Recommendations Breakdown
            analyst_breakdown = state.get('analyst_breakdown', {})
            if analyst_breakdown and analyst_count > 0:
                strong_buy = analyst_breakdown.get('strong_buy', 0)
                buy = analyst_breakdown.get('buy', 0)
                hold = analyst_breakdown.get('hold', 0)
                sell = analyst_breakdown.get('sell', 0)
                strong_sell = analyst_breakdown.get('strong_sell', 0)
                
                with ui.card_section().classes('bg-grey-1'):
                    ui.label('Analyst Recommendations Breakdown').classes('text-subtitle1 text-weight-medium mb-3')
                    
                    # Create a visual bar chart - show all categories
                    with ui.column().classes('w-full gap-2'):
                        # Strong Buy
                        pct = (strong_buy / analyst_count * 100) if analyst_count > 0 else 0
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label('Strong Buy').classes('w-24 text-right text-sm')
                            ui.label(str(strong_buy)).classes('w-8 text-sm font-bold text-green-700')
                            with ui.element('div').classes('flex-grow bg-grey-3 rounded overflow-hidden h-6'):
                                if pct > 0:
                                    ui.element('div').classes('bg-green-600 h-full').style(f'width: {pct}%')
                            ui.label(f'{pct:.0f}%').classes('w-12 text-xs text-grey-6')
                        
                        # Buy
                        pct = (buy / analyst_count * 100) if analyst_count > 0 else 0
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label('Buy').classes('w-24 text-right text-sm')
                            ui.label(str(buy)).classes('w-8 text-sm font-bold text-green-600')
                            with ui.element('div').classes('flex-grow bg-grey-3 rounded overflow-hidden h-6'):
                                if pct > 0:
                                    ui.element('div').classes('bg-green-400 h-full').style(f'width: {pct}%')
                            ui.label(f'{pct:.0f}%').classes('w-12 text-xs text-grey-6')
                        
                        # Hold
                        pct = (hold / analyst_count * 100) if analyst_count > 0 else 0
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label('Hold').classes('w-24 text-right text-sm')
                            ui.label(str(hold)).classes('w-8 text-sm font-bold text-amber-700')
                            with ui.element('div').classes('flex-grow bg-grey-3 rounded overflow-hidden h-6'):
                                if pct > 0:
                                    ui.element('div').classes('bg-amber-400 h-full').style(f'width: {pct}%')
                            ui.label(f'{pct:.0f}%').classes('w-12 text-xs text-grey-6')
                        
                        # Sell
                        pct = (sell / analyst_count * 100) if analyst_count > 0 else 0
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label('Sell').classes('w-24 text-right text-sm')
                            ui.label(str(sell)).classes('w-8 text-sm font-bold text-red-600')
                            with ui.element('div').classes('flex-grow bg-grey-3 rounded overflow-hidden h-6'):
                                if pct > 0:
                                    ui.element('div').classes('bg-red-400 h-full').style(f'width: {pct}%')
                            ui.label(f'{pct:.0f}%').classes('w-12 text-xs text-grey-6')
                        
                        # Strong Sell
                        pct = (strong_sell / analyst_count * 100) if analyst_count > 0 else 0
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label('Strong Sell').classes('w-24 text-right text-sm')
                            ui.label(str(strong_sell)).classes('w-8 text-sm font-bold text-red-700')
                            with ui.element('div').classes('flex-grow bg-grey-3 rounded overflow-hidden h-6'):
                                if pct > 0:
                                    ui.element('div').classes('bg-red-600 h-full').style(f'width: {pct}%')
                            ui.label(f'{pct:.0f}%').classes('w-12 text-xs text-grey-6')
                    
                    ui.separator().classes('my-2')
                    ui.label(f'Total Analysts: {analyst_count}').classes('text-sm text-grey-7')
            
            # Confidence Breakdown
            confidence_breakdown = state.get('confidence_breakdown', {})
            if confidence_breakdown:
                base_confidence = confidence_breakdown.get('base_confidence', 0)
                price_target_boost = confidence_breakdown.get('price_target_boost', 0)
                boost_to_lower = confidence_breakdown.get('boost_to_lower', 0)
                boost_to_consensus = confidence_breakdown.get('boost_to_consensus', 0)
                
                with ui.card_section():
                    ui.label('Confidence Score Breakdown (New Methodology)').classes('text-subtitle1 text-weight-medium mb-2')
                    ui.label('FinnHub Analyst Rating Base + Price Target Boost').classes('text-xs text-grey-6 mb-3')
                    
                    with ui.grid(columns=2).classes('w-full gap-4'):
                        # Base Confidence from Analyst Ratings
                        with ui.card().classes('bg-blue-50'):
                            ui.label('Base (from Analyst Ratings)').classes('text-caption text-grey-7')
                            ui.label(f'{base_confidence:.1f}%').classes('text-h5 text-blue-700')
                            ui.label(f'Weighted buy/sell/hold scores').classes('text-xs text-grey-6')
                        
                        # Price Target Boost
                        boost_color = 'positive' if price_target_boost > 0 else 'negative' if price_target_boost < 0 else 'grey'
                        with ui.card().classes(f'bg-{"green" if price_target_boost > 0 else "red" if price_target_boost < 0 else "grey"}-50'):
                            ui.label('Price Target Boost').classes('text-caption text-grey-7')
                            ui.label(f'{price_target_boost:+.1f}%').classes(f'text-h5 text-{boost_color}')
                            ui.label(f'Avg of lower & consensus targets').classes('text-xs text-grey-6')
                        
                        # Boost to Lower Target
                        with ui.card().classes('bg-grey-50'):
                            ui.label('Boost to Lower Target').classes('text-caption text-grey-7')
                            ui.label(f'{boost_to_lower:+.1f}%').classes('text-h6 text-grey-700')
                        
                        # Boost to Consensus Target
                        with ui.card().classes('bg-grey-50'):
                            ui.label('Boost to Consensus Target').classes('text-caption text-grey-7')
                            ui.label(f'{boost_to_consensus:+.1f}%').classes('text-h6 text-grey-700')
                    
                    ui.separator().classes('my-2')
                    
                    # Calculate what confidence should be
                    calculated_confidence = base_confidence + price_target_boost
                    clamped_confidence = max(0.0, min(100.0, calculated_confidence))
                    
                    # Show the formula
                    ui.label(f'Final Confidence = Base + Boost = {base_confidence:.1f}% + {price_target_boost:+.1f}% = {calculated_confidence:.1f}%').classes('text-sm text-grey-7')
                    
                    # If clamping occurred, show it
                    if calculated_confidence != clamped_confidence:
                        ui.label(f'Clamped to valid range [0-100%]: {clamped_confidence:.1f}%').classes('text-sm text-orange-600 font-medium')
                    
                    # If stored confidence doesn't match what we calculated, show warning
                    if abs(confidence - clamped_confidence) > 0.1:
                        ui.label(f'⚠️ Stored confidence ({confidence:.1f}%) differs from calculated ({clamped_confidence:.1f}%)').classes('text-sm text-red-600 font-bold')
            
            # Settings
            profit_ratio = float(settings.get('profit_ratio', 1.0))
            min_analysts = int(settings.get('min_analysts', 3))
            
            with ui.card_section():
                ui.label('Analysis Settings').classes('text-subtitle1 text-weight-medium mb-2')
                
                with ui.row().classes('gap-4'):
                    ui.label(f'Profit Ratio: {profit_ratio}x').classes('text-sm text-grey-7')
                    ui.label(f'Min Analysts: {min_analysts}').classes('text-sm text-grey-7')
            
            # Methodology
            with ui.expansion('Calculation Methodology', icon='info').classes('w-full'):
                with ui.card_section().classes('bg-grey-1'):
                    confidence_breakdown = state.get('confidence_breakdown', {})
                    base_conf = confidence_breakdown.get('base_confidence', 0)
                    price_boost = confidence_breakdown.get('price_target_boost', 0)
                    boost_lower = confidence_breakdown.get('boost_to_lower', 0)
                    boost_consensus = confidence_breakdown.get('boost_to_consensus', 0)
                    buy_score = confidence_breakdown.get('buy_score', 0)
                    sell_score = confidence_breakdown.get('sell_score', 0)
                    hold_score = confidence_breakdown.get('hold_score', 0)
                    
                    analyst_breakdown = state.get('analyst_breakdown', {})
                    strong_buy = analyst_breakdown.get('strong_buy', 0)
                    buy = analyst_breakdown.get('buy', 0)
                    hold = analyst_breakdown.get('hold', 0)
                    sell = analyst_breakdown.get('sell', 0)
                    strong_sell = analyst_breakdown.get('strong_sell', 0)
                    
                    ui.markdown(f'''
**Signal Determination:**

The recommendation signal is based on the price delta from current price to consensus target:
- **BUY**: Consensus target is >5% above current price (uses High target for profit calc)
- **SELL**: Consensus target is >5% below current price (uses Low target for profit calc)
- **HOLD**: Consensus target is within ±5% of current price

---

**NEW Confidence Score Calculation (FinnHub Methodology + Price Target Boost):**

1. **Weighted Analyst Scores**
   - Buy Score = (Strong Buy × 2) + Buy = ({strong_buy} × 2) + {buy} = {buy_score:.1f}
   - Hold Score = Hold = {hold}
   - Sell Score = (Strong Sell × 2) + Sell = ({strong_sell} × 2) + {sell} = {sell_score:.1f}
   - Total Weighted = {buy_score + hold_score + sell_score:.1f}

2. **Base Confidence from Analyst Ratings**
   - Base Confidence = Dominant Score / Total × 100
   - Current: {base_conf:.1f}%
   - **Logic**: Analyst consensus strength drives base confidence

3. **Price Target Boost**
   - Boost to Lower Target = ((Low Target - Current) / Current) × 100 = {boost_lower:+.1f}%
   - Boost to Consensus = ((Consensus - Current) / Current) × 100 = {boost_consensus:+.1f}%
   - Avg Price Target Boost = ({boost_lower:+.1f}% + {boost_consensus:+.1f}%) / 2 = {price_boost:+.1f}%
   - **Logic**: Positive when targets are above current price (more upside potential)

4. **Final Confidence (Clamped to 0-100%)**
   - Final = Base + Boost = {base_conf:.1f}% + {price_boost:+.1f}% = **{confidence:.1f}%**

---

**Expected Profit Calculation:**

For **BUY** signals:
1. **Price Delta** = High Target - Current Price
2. **Weighted Delta** = Price Delta × (Confidence / 100) × Profit Ratio
3. **Expected Profit %** = (Weighted Delta / Current Price) × 100

For **SELL** signals:
1. **Price Delta** = Current Price - Low Target
2. **Weighted Delta** = Price Delta × (Confidence / 100) × Profit Ratio
3. **Expected Profit %** = (Weighted Delta / Current Price) × 100

**Profit Ratio Setting**: Adjusts expected profit based on risk tolerance (current: {profit_ratio}x)

---

**Analyst Recommendations:**

The breakdown shows how many analysts rate the stock as:
- **Strong Buy / Buy**: Bullish ratings (expecting price increase) - weighted 2x for strong ratings
- **Hold**: Neutral rating (price expected to stay stable)
- **Sell / Strong Sell**: Bearish ratings (expecting price decrease) - weighted 2x for strong ratings

This distribution helps validate the consensus target and signal strength.

**Signal Logic:**
- **BUY**: Consensus > Current Price + 5%
- **SELL**: Consensus < Current Price - 5%
- **HOLD**: Otherwise

**Profit Ratio**: Multiplier setting (default 1.0) to adjust conservative/aggressive positioning
                    ''').classes('text-sm')
