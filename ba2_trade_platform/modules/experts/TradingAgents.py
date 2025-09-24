from typing import Any, Dict, List
from datetime import datetime

from ...core.MarketExpertInterface import MarketExpertInterface
from ...core.models import ExpertInstance, MarketAnalysis, AnalysisOutput
from ...core.db import get_db, get_instance, update_instance
from ...core.types import MarketAnalysisStatus, OrderRecommendation
from ...logger import logger
from ...thirdparties.TradingAgents.tradingagents.graph.trading_graph import TradingAgentsGraph
from ...thirdparties.TradingAgents.tradingagents.default_config import DEFAULT_CONFIG

class TradingAgents(MarketExpertInterface):
    """
    Implementation of MarketExpertInterface for AI-powered trading agents.
    
    This class provides market predictions and instrument management
    for AI-based trading strategies.
    """
    
    @classmethod
    def description(cls) -> str:
        return """Expert that provides AI-driven market predictions """
    
    def __init__(self, id: int):
        """
        Initialize the TradingAgent with an expert instance ID.
        
        Args:
            id (int): The ExpertInstance ID from the database
        """
        super().__init__(id)
        logger.debug(f'Initializing TradingAgent with instance id: {id}')
        
        # Set environment variables from database for API keys
        try:
            from ...thirdparties.TradingAgents.tradingagents.dataflows.config import set_environment_variables_from_database
            set_environment_variables_from_database()
        except Exception as e:
            logger.warning(f"Could not set API keys from database: {e}")
        
        # Load the expert instance from database
        self.instance = get_instance(ExpertInstance, id)
        if not self.instance:
            raise ValueError(f"ExpertInstance with id {id} not found")
        
        #logger.debug(f'TradingAgent initialized for expert: {self.instance.expert}')
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
            "debates_new_positions": {
                "type": "float",
                "required": True,
                "description": "Number of debates for new positions",
                "default": 3.0
            },
            "debates_existing_positions": {
                "type": "float", 
                "required": True,
                "description": "Number of debates for existing positions",
                "default": 3.0
            },
            "timeframe": {
                "type": "str",
                "required": True, 
                "description": "Timeframe (1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo)",
                "valid_values": ["1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"],
                "default": "1h"
            },
            "deep_think_llm": {
                "type": "str",
                "required": True,
                "description": "LLM model for deep analysis and complex reasoning",
                "valid_values": ["gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o4-mini"],
                "default": "o4-mini"
            },
            "quick_think_llm": {
                "type": "str",
                "required": True,
                "description": "LLM model for quick analysis and real-time decisions",
                "valid_values": ["gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o4-mini"],
                "default": "gpt-5-mini"
            },
            "news_lookback_days": {
                "type": "int",
                "required": True,
                "description": "Number of days to look back for news analysis",
                "default": 7
            },
            "market_history_days": {
                "type": "int",
                "required": True,
                "description": "Number of days of market history to analyze",
                "default": 90
            },
            "economic_data_days": {
                "type": "int",
                "required": True,
                "description": "Number of days of economic data to consider",
                "default": 90
            },
            "social_sentiment_days": {
                "type": "int",
                "required": True,
                "description": "Number of days of social sentiment data to analyze",
                "default": 3
            }
        }

        
    
    
    def _get_current_timestamp(self) -> str:
        """Get current timestamp for predictions."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    
   

    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """
        Run analysis for a specific symbol and market analysis instance.
        This method updates the market_analysis object with results.
        
        Args:
            symbol (str): The instrument symbol to analyze.
            market_analysis (MarketAnalysis): The market analysis instance to update with results.
        """
        logger.info(f"Running TradingAgents analysis for symbol: {symbol}, analysis ID: {market_analysis.id}")
        
        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            
            # Try to use actual TradingAgents implementation


                
            # Create custom config with our database settings
            config_copy = DEFAULT_CONFIG.copy()
            
            # Update config with user settings from this expert instance
            timeframe = self.settings.get('timeframe', '1h')
            debates_new = self.settings.get('debates_new_positions', 3)
            debates_existing = self.settings.get('debates_existing_positions', 3)
            deep_think_llm = self.settings.get('deep_think_llm', 'o4-mini')
            quick_think_llm = self.settings.get('quick_think_llm', 'gpt-5-mini')
            news_lookback_days = self.settings.get('news_lookback_days', 7)
            market_history_days = self.settings.get('market_history_days', 90)
            economic_data_days = self.settings.get('economic_data_days', 90)
            social_sentiment_days = self.settings.get('social_sentiment_days', 3)
            
            config_copy.update({
                'max_debate_rounds': int(debates_new),
                'max_risk_discuss_rounds': int(debates_existing),
                'deep_think_llm': deep_think_llm,
                'quick_think_llm': quick_think_llm,
                'news_lookback_days': int(news_lookback_days),
                'market_history_days': int(market_history_days),
                'economic_data_days': int(economic_data_days),
                'social_sentiment_days': int(social_sentiment_days),
                'log_dir': 'logs'  # Use same log directory as main platform
            })
            
            # Initialize TradingAgents with the existing market analysis ID
            ta_graph = TradingAgentsGraph(
                debug=True,
                config=config_copy,
                market_analysis_id=market_analysis.id  # Pass the existing MarketAnalysis ID
            )
            
            # Get current date for analysis
            trade_date = datetime.now().strftime("%Y-%m-%d")
            
            # Run the analysis
            logger.info(f"Running TradingAgents analysis for {symbol} on {trade_date}")
            logger.debug(f"TradingAgents config: expert_instance_id={self.id}, market_analysis_id={market_analysis.id}")
            
            try:
                final_state, processed_signal = ta_graph.propagate(symbol, trade_date)
                logger.info(f"TradingAgents analysis completed for {symbol}")
            except Exception as propagate_error:
                logger.error(f"Error during TradingAgents propagation for {symbol}: {propagate_error}", exc_info=True)
                raise
            
            # Extract recommendation from final state
            expert_recommendation = final_state.get('expert_recommendation', {})
            
            if expert_recommendation:
                # Use the generated recommendation
                signal = expert_recommendation.get('recommended_action', OrderRecommendation.HOLD)
                confidence = expert_recommendation.get('confidence', 0.0)
                expected_profit = expert_recommendation.get('expected_profit_percent', 0.0)
                details = expert_recommendation.get('details', 'TradingAgents analysis completed')
                price_at_date = expert_recommendation.get('price_at_date', 0.0)
            else:
                # Fallback to processed signal
                signal = processed_signal if processed_signal in ['BUY', 'SELL', 'HOLD'] else OrderRecommendation.HOLD
                confidence = 0.0
                expected_profit = 0.0
                details = f"TradingAgents analysis: {processed_signal}"
                price_at_date = 0.0
            
            prediction_result = {
                'instrument': symbol,
                'signal': signal,
                'confidence': round(confidence, 3),
                'expected_profit_percent': round(expected_profit, 2),
                'price_target': price_at_date if price_at_date > 0 else None,
                'reasoning': details[:500] if details else 'TradingAgents analysis completed',
                'timestamp': self._get_current_timestamp(),
                'expert_id': self.id,
                'expert_type': 'TradingAgents',
                'market_analysis_id': ta_graph.market_analysis_id,
                'analysis_method': 'tradingagents_full'
            }
            
            # Store the prediction result in the market analysis state
            market_analysis.state = {
                'prediction_result': prediction_result,
                'analysis_timestamp': self._get_current_timestamp(),
                'expert_settings': {
                    'timeframe': timeframe,
                    'debates_new_positions': debates_new,
                    'debates_existing_positions': debates_existing,
                    'deep_think_llm': deep_think_llm,
                    'quick_think_llm': quick_think_llm,
                    'news_lookback_days': news_lookback_days,
                    'market_history_days': market_history_days,
                    'economic_data_days': economic_data_days,
                    'social_sentiment_days': social_sentiment_days
                },
                'final_state': final_state,
                'processed_signal': processed_signal,
                'tradingagents_mode': True
            }
            
            # Create analysis outputs for detailed results
            if prediction_result.get('reasoning'):
                reasoning_output = AnalysisOutput(
                    market_analysis_id=market_analysis.id,
                    name="Trading Recommendation Reasoning",
                    type="text",
                    text=prediction_result['reasoning']
                )
                # Add to database
                session = get_db()
                session.add(reasoning_output)
                session.commit()
                session.close()
                
            # Create analysis output for full TradingAgents state
            if final_state:
                import json
                state_output = AnalysisOutput(
                    market_analysis_id=market_analysis.id,
                    name="TradingAgents Full State",
                    type="json",
                    text=json.dumps(final_state, indent=2, default=str)
                )
                # Add to database
                session = get_db()
                session.add(state_output)
                session.commit()
                session.close()
            
            logger.info(f"Successfully completed TradingAgents analysis for {symbol}, signal: {prediction_result.get('signal', 'UNKNOWN')}")
            
           
            # Create analysis output for prediction summary
            prediction_summary = f"""
TradingAgents Analysis Summary for {symbol}:

Signal: {prediction_result.get('signal', 'UNKNOWN')}
Confidence: {prediction_result.get('confidence', 0.0):.1%}
Expected Profit: {prediction_result.get('expected_profit_percent', 0.0):.2f}%
Weight: {prediction_result.get('weight', 0.0)}
Expert ID: {self.id}
Analysis Method: {prediction_result.get('analysis_method', 'unknown')}

Settings Used:
- Deep Think LLM: {deep_think_llm}
- Quick Think LLM: {quick_think_llm}
- News Lookback: {news_lookback_days} days
- Market History: {market_history_days} days
- Economic Data: {economic_data_days} days
- Social Sentiment: {social_sentiment_days} days
- Timeframe: {timeframe}
- Debates (New/Existing): {debates_new}/{debates_existing}

Analysis completed at: {prediction_result.get('timestamp', 'Unknown')}
            """.strip()
            
            summary_output = AnalysisOutput(
                market_analysis_id=market_analysis.id,
                name="Prediction Summary",
                type="summary",
                text=prediction_summary
            )
            
            # Add summary to database
            session = get_db()
            session.add(summary_output)
            session.commit()
            session.close()
            
            # Update status to completed
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            
        except Exception as e:
            logger.error(f"Error running TradingAgents analysis for {symbol}: {str(e)}")
            
            # Store error information in the market analysis
            market_analysis.state = {
                'error': str(e),
                'error_timestamp': self._get_current_timestamp(),
                'analysis_failed': True,
                'analysis_method': 'tradingagents_full'
            }
            
            # Create error output
            error_output = AnalysisOutput(
                market_analysis_id=market_analysis.id,
                name="Analysis Error",
                type="error",
                text=f"TradingAgents analysis failed for {symbol}: {str(e)}"
            )
            
            # Add error to database
            session = get_db()
            session.add(error_output)
            session.commit()
            session.close()
            
            # Update status to failed
            market_analysis.status = MarketAnalysisStatus.FAILED
            update_instance(market_analysis)
            
            # Re-raise the exception so the worker queue can handle it
            raise