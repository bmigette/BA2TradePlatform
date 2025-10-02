from typing import Any, Dict, List
from datetime import datetime, timezone
import json

from ...core.MarketExpertInterface import MarketExpertInterface
from ...core.models import ExpertInstance, MarketAnalysis, AnalysisOutput, ExpertRecommendation
from ...core.db import get_db, get_instance, update_instance, add_instance
from ...core.types import MarketAnalysisStatus, OrderRecommendation, RiskLevel, TimeHorizon, AnalysisUseCase
from ...logger import logger
from ...thirdparties.TradingAgents.tradingagents.graph.trading_graph import TradingAgentsGraph
from ...thirdparties.TradingAgents.tradingagents.default_config import DEFAULT_CONFIG
from ...thirdparties.TradingAgents.tradingagents.db_storage import update_market_analysis_status


class TradingAgents(MarketExpertInterface):
    """
    TradingAgents Expert Implementation
    
    Multi-agent AI system for market analysis and trading recommendations.
    Integrates news, technical, fundamental, and macro-economic analysis
    through specialized AI agents with debate-based decision making.
    """
    
    @classmethod
    def description(cls) -> str:
        return "Multi-agent AI trading system with debate-based analysis and risk assessment"
    
    @classmethod
    def _get_timeframe_valid_values(cls) -> List[str]:
        """Get valid timeframe values from TimeInterval enum."""
        from ...core.types import TimeInterval
        return TimeInterval.get_all_intervals()
    
    def __init__(self, id: int):
        """Initialize TradingAgents expert with database instance."""
        super().__init__(id)
        #logger.debug(f'Initializing TradingAgents expert with instance ID: {id}')
        
        self._setup_api_keys()
        self._load_expert_instance(id)
    
    def _setup_api_keys(self) -> None:
        """Setup API keys from database configuration."""
        try:
            from ...thirdparties.TradingAgents.tradingagents.dataflows.config import set_environment_variables_from_database
            set_environment_variables_from_database()
            #logger.debug("API keys loaded from database")
        except Exception as e:
            logger.warning(f"Could not load API keys from database: {e}")
    
    def _load_expert_instance(self, id: int) -> None:
        """Load and validate expert instance from database."""
        self.instance = get_instance(ExpertInstance, id)
        if not self.instance:
            raise ValueError(f"ExpertInstance with ID {id} not found")
        #logger.debug(f'TradingAgents expert loaded: {self.instance.expert}')
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """Define configurable settings for TradingAgents expert."""
        return {
            # Analysis Configuration
            "debates_new_positions": {
                "type": "float", "required": True, "default": 3.0,
                "description": "Number of debate rounds for new position analysis",
                "tooltip": "Controls how many debate rounds the AI agents will conduct when analyzing potential new positions. More rounds = more thorough analysis but takes longer. Recommended: 2-4 rounds."
            },
            "debates_existing_positions": {
                "type": "float", "required": True, "default": 3.0,
                "description": "Number of debate rounds for existing position analysis",
                "tooltip": "Controls how many debate rounds the AI agents will conduct when reviewing existing open positions. More rounds = more thorough analysis. Recommended: 2-3 rounds for faster real-time decisions."
            },
            "timeframe": {
                "type": "str", "required": True, "default": "1h",
                "description": "Analysis timeframe for market data",
                "valid_values": cls._get_timeframe_valid_values(),
                "tooltip": "The time interval used for technical analysis charts and indicators. Shorter timeframes (1m, 5m) are for day trading, medium (1h, 4h, 1d) for swing trading, longer (1wk, 1mo) for position trading."
            },
            
            # LLM Models
            "deep_think_llm": {
                "type": "str", "required": True, "default": "gpt-5-mini",
                "description": "LLM model for complex reasoning and deep analysis",
                "valid_values": ["gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o4-mini"],
                "tooltip": "The AI model used for in-depth analysis requiring complex reasoning, such as fundamental analysis and debate arbitration. Higher-tier models (gpt-5) provide better insights but cost more. Mini/nano variants balance cost and performance."
            },
            "quick_think_llm": {
                "type": "str", "required": True, "default": "gpt-5-mini",
                "description": "LLM model for quick analysis and real-time decisions",
                "valid_values": ["gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o4-mini", "o4-mini-deep-research"],
                "help": "For more information on available models, see [OpenAI Models Documentation](https://platform.openai.com/docs/models)",
                "tooltip": "The AI model used for faster analysis tasks like technical indicators and quick data summarization. Nano/mini models are cost-effective for these simpler tasks. The o4-mini-deep-research variant provides enhanced analytical capabilities."
            },
            
            # Data Lookback Periods
            "news_lookback_days": {
                "type": "int", "required": True, "default": 7,
                "description": "Days of news data to analyze",
                "tooltip": "How many days back to search for news articles about the symbol. More days = broader context but may include outdated information. Recommended: 3-7 days for active stocks, 14-30 for slower-moving positions."
            },
            "market_history_days": {
                "type": "int", "required": True, "default": 90,
                "description": "Days of market history for technical analysis",
                "tooltip": "Historical price data window for calculating technical indicators (moving averages, RSI, MACD, etc.). 90 days provides good context for most indicators. Increase to 180-365 for longer-term trend analysis."
            },
            "economic_data_days": {
                "type": "int", "required": True, "default": 90,
                "description": "Days of economic data to consider",
                "tooltip": "Lookback period for macroeconomic indicators (inflation, GDP, interest rates, etc.). 90 days captures recent economic trends. Increase to 180-365 for broader economic cycle analysis."
            },
            "social_sentiment_days": {
                "type": "int", "required": True, "default": 3,
                "description": "Days of social sentiment data to analyze",
                "tooltip": "How many days of social media and Reddit sentiment to analyze. Social sentiment changes rapidly, so 1-7 days is typical. Shorter periods (1-3 days) capture current buzz, longer periods (7-14 days) smooth out noise."
            },
            "debug_mode": {
                "type": "bool", "required": True, "default": True,
                "description": "Enable debug mode with detailed console output",
                "tooltip": "When enabled, outputs detailed logs of the AI agent's thinking process, data gathering, and decision-making steps. Useful for understanding why recommendations were made. Disable for cleaner logs in production."
            }
        }

    def _get_current_timestamp(self) -> str:
        """Get current UTC timestamp in ISO format."""
        return datetime.now(timezone.utc).isoformat()
    
    def _create_tradingagents_config(self, subtype: str) -> Dict[str, Any]:
        """Create TradingAgents configuration from expert settings."""
        config = DEFAULT_CONFIG.copy()
        
        # Get settings definitions for default values
        settings_def = self.get_settings_definitions()
        
        # Choose debate settings based on analysis subtype
        if subtype == AnalysisUseCase.ENTER_MARKET:
            # For new position analysis, use debates_new_positions setting
            max_debate_rounds = int(self.settings.get('debates_new_positions', settings_def['debates_new_positions']['default']))
            max_risk_discuss_rounds = int(self.settings.get('debates_new_positions', settings_def['debates_new_positions']['default']))
        elif subtype == AnalysisUseCase.OPEN_POSITIONS:
            # For existing position analysis, use debates_existing_positions setting
            max_debate_rounds = int(self.settings.get('debates_existing_positions', settings_def['debates_existing_positions']['default']))
            max_risk_discuss_rounds = int(self.settings.get('debates_existing_positions', settings_def['debates_existing_positions']['default']))
        else:
            # Default fallback
            max_debate_rounds = int(self.settings.get('debates_new_positions', settings_def['debates_new_positions']['default']))
            max_risk_discuss_rounds = int(self.settings.get('debates_existing_positions', settings_def['debates_existing_positions']['default']))
        
        # Apply user settings with defaults from settings definitions
        config.update({
            'max_debate_rounds': max_debate_rounds,
            'max_risk_discuss_rounds': max_risk_discuss_rounds,
            'deep_think_llm': self.settings.get('deep_think_llm', settings_def['deep_think_llm']['default']),
            'quick_think_llm': self.settings.get('quick_think_llm', settings_def['quick_think_llm']['default']),
            'news_lookback_days': int(self.settings.get('news_lookback_days', settings_def['news_lookback_days']['default'])),
            'market_history_days': int(self.settings.get('market_history_days', settings_def['market_history_days']['default'])),
            'economic_data_days': int(self.settings.get('economic_data_days', settings_def['economic_data_days']['default'])),
            'social_sentiment_days': int(self.settings.get('social_sentiment_days', settings_def['social_sentiment_days']['default'])),
            'timeframe': self.settings.get('timeframe', settings_def['timeframe']['default']),
        })
        
        return config
    
    def _extract_recommendation_data(self, final_state: Dict, processed_signal: str, symbol: str) -> Dict[str, Any]:
        """Extract recommendation data from TradingAgents analysis results."""
        expert_recommendation = final_state.get('expert_recommendation', {})
        
        if expert_recommendation:
            return {
                'signal': expert_recommendation.get('recommended_action', OrderRecommendation.ERROR),
                'confidence': expert_recommendation.get('confidence', 0.0),
                'expected_profit': expert_recommendation.get('expected_profit_percent', 0.0),
                'details': expert_recommendation.get('details', 'TradingAgents analysis completed'),
                'price_at_date': expert_recommendation.get('price_at_date', 0.0),
                'risk_level': expert_recommendation.get('risk_level', RiskLevel.MEDIUM),
                'time_horizon': expert_recommendation.get('time_horizon', TimeHorizon.SHORT_TERM)
            }
        else:
            # Fallback to processed signal
            return {
                'signal': processed_signal if processed_signal in ['BUY', 'SELL', 'HOLD'] else OrderRecommendation.ERROR,
                'confidence': 0.0,
                'expected_profit': 0.0,
                'details': f"TradingAgents analysis: {processed_signal}",
                'price_at_date': 0.0,
                'risk_level': RiskLevel.MEDIUM,
                'time_horizon': TimeHorizon.SHORT_TERM
            }
    
    def _create_expert_recommendation(self, recommendation_data: Dict[str, Any], symbol: str, market_analysis_id: int) -> int:
        """Create ExpertRecommendation record in database."""
        try:
            expert_recommendation = ExpertRecommendation(
                instance_id=self.id,
                symbol=symbol,
                recommended_action=recommendation_data['signal'],
                expected_profit_percent=round(recommendation_data['expected_profit'], 2),
                price_at_date=recommendation_data['price_at_date'],
                details=recommendation_data['details'][:100000] if recommendation_data['details'] else None,
                confidence=round(recommendation_data['confidence'], 3),
                risk_level=recommendation_data['risk_level'],
                time_horizon=recommendation_data['time_horizon'],
                market_analysis_id=market_analysis_id,
                created_at=datetime.now(timezone.utc)
            )
            
            recommendation_id = add_instance(expert_recommendation)
            logger.info(f"[SUCCESS] Created ExpertRecommendation (ID: {recommendation_id}) for {symbol}: "
                       f"{recommendation_data['signal']} with {recommendation_data['confidence']:.1%} confidence")
            return recommendation_id
            
        except Exception as e:
            logger.error(f"[ERROR] Failed to create ExpertRecommendation for {symbol}: {e}", exc_info=True)
            raise
    
    def _store_analysis_outputs(self, market_analysis_id: int, symbol: str, 
                               recommendation_data: Dict[str, Any], final_state: Dict, 
                               expert_settings: Dict) -> None:
        """Store detailed analysis outputs in database."""
        session = get_db()
        
        try:
            # Store reasoning
            if recommendation_data.get('details'):
                reasoning_output = AnalysisOutput(
                    market_analysis_id=market_analysis_id,
                    name="Trading Recommendation Reasoning",
                    type="trading_recommendation_reasoning",
                    text=recommendation_data['details']
                )
                session.add(reasoning_output)
            
            # Store full TradingAgents state
            if final_state:
                state_output = AnalysisOutput(
                    market_analysis_id=market_analysis_id,
                    name="TradingAgents Full State",
                    type="tradingagents_full_state",
                    text=json.dumps(final_state, indent=2, default=str)
                )
                session.add(state_output)
            
            # Store analysis summary
            summary_text = self._create_analysis_summary(symbol, recommendation_data, expert_settings)
            summary_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Analysis Summary",
                type="tradingagents_analysis_summary",
                text=summary_text
            )
            session.add(summary_output)
            
            session.commit()
            
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to store analysis outputs: {e}", exc_info=True)
            raise
        finally:
            session.close()
    
    def _create_analysis_summary(self, symbol: str, recommendation_data: Dict[str, Any], 
                                expert_settings: Dict) -> str:
        """Create formatted analysis summary text."""
        return f"""TradingAgents Analysis Summary for {symbol}:

Signal: {recommendation_data.get('signal', 'UNKNOWN')}
Confidence: {recommendation_data.get('confidence', 0.0):.1%}
Expected Profit: {recommendation_data.get('expected_profit', 0.0):.2f}%
Risk Level: {recommendation_data.get('risk_level', 'UNKNOWN')}
Time Horizon: {recommendation_data.get('time_horizon', 'UNKNOWN')}
Expert ID: {self.id}

Configuration:
- Deep Think LLM: {expert_settings.get('deep_think_llm', 'Unknown')}
- Quick Think LLM: {expert_settings.get('quick_think_llm', 'Unknown')}
- News Lookback: {expert_settings.get('news_lookback_days', 0)} days
- Market History: {expert_settings.get('market_history_days', 0)} days
- Economic Data: {expert_settings.get('economic_data_days', 0)} days
- Social Sentiment: {expert_settings.get('social_sentiment_days', 0)} days
- Timeframe: {expert_settings.get('timeframe', 'Unknown')}
- Debates (New/Existing): {expert_settings.get('debates_new_positions', 0)}/{expert_settings.get('debates_existing_positions', 0)}

Analysis completed at: {self._get_current_timestamp()}"""

    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """
        Run TradingAgents analysis for a symbol and create ExpertRecommendation.
        
        Args:
            symbol: Financial instrument symbol to analyze
            market_analysis: MarketAnalysis instance to update with results
        """
        logger.info(f"[START] Starting TradingAgents analysis for {symbol} (Analysis ID: {market_analysis.id})")
        
        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            
            # Execute TradingAgents analysis
            final_state, processed_signal = self._execute_tradingagents_analysis(symbol, market_analysis.id, market_analysis.subtype)
            
            # Extract recommendation data
            recommendation_data = self._extract_recommendation_data(final_state, processed_signal, symbol)
            
            # Create ExpertRecommendation record
            recommendation_id = self._create_expert_recommendation(recommendation_data, symbol, market_analysis.id)
            
            # Store analysis state and outputs
            self._store_analysis_state(market_analysis, recommendation_data, final_state, processed_signal, recommendation_id)
            self._store_analysis_outputs(market_analysis.id, symbol, recommendation_data, final_state, self.settings)
            
            # Mark analysis as completed
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            market_analysis.state['trading_agent_graph'] = self._clean_state_for_json_storage(final_state)
            # Explicitly mark the state field as modified for SQLAlchemy
            from sqlalchemy.orm import attributes
            attributes.flag_modified(market_analysis, "state")
            update_instance(market_analysis)

            logger.info(f"[COMPLETE] TradingAgents analysis completed for {symbol}: "
                       f"{recommendation_data['signal']} ({recommendation_data['confidence']:.1%} confidence)")
            
            # Trigger Trade Manager to process the recommendation (if automatic trading is enabled)
            self._notify_trade_manager(recommendation_id, symbol)

        except Exception as e:
            logger.error(f"[FAILED] TradingAgents analysis failed for {symbol}: {e}", exc_info=True)
            self._handle_analysis_error(market_analysis, symbol, str(e))
            raise
    
    def _execute_tradingagents_analysis(self, symbol: str, market_analysis_id: int, subtype: str) -> tuple:
        """Execute the core TradingAgents analysis."""
        # Create configuration
        config = self._create_tradingagents_config(subtype)
        
        # Initialize TradingAgents graph
        # Get debug mode from settings (defaults to True for detailed logging)
        debug_mode = self.settings.get('debug_mode', True)
        
        ta_graph = TradingAgentsGraph(
            debug=debug_mode,
            config=config,
            market_analysis_id=market_analysis_id
        )
        
        # Run analysis
        trade_date = datetime.now().strftime("%Y-%m-%d")
        logger.debug(f"Running TradingAgents propagation for {symbol} on {trade_date}")
        
        final_state, processed_signal = ta_graph.propagate(symbol, trade_date)
        logger.debug(f"TradingAgents propagation completed for {symbol}")
        
        return final_state, processed_signal
    
    def _store_analysis_state(self, market_analysis: MarketAnalysis, recommendation_data: Dict[str, Any], 
                             final_state: Dict, processed_signal: str, recommendation_id: int) -> None:
        """Store analysis results in MarketAnalysis state using proper state merging."""
        # Import database update function
        
        prediction_result = {
            'instrument': market_analysis.symbol,
            'signal': recommendation_data['signal'],
            'confidence': round(recommendation_data['confidence'], 3),
            'expected_profit_percent': round(recommendation_data['expected_profit'], 2),
            'price_target': recommendation_data['price_at_date'] if recommendation_data['price_at_date'] > 0 else None,
            'reasoning': recommendation_data['details'][:500] if recommendation_data['details'] else 'Analysis completed',
            'timestamp': self._get_current_timestamp(),
            'expert_id': self.id,
            'expert_type': 'TradingAgents',
            'market_analysis_id': market_analysis.id,
            'expert_recommendation_id': recommendation_id,
            'analysis_method': 'tradingagents_full'
        }
        
        # Use proper state merging under 'trading_agent_graph' key
        trading_agent_state = {
            'prediction_result': prediction_result,
            'analysis_timestamp': self._get_current_timestamp(),
            'expert_settings': self.settings,
            'final_state': self._clean_state_for_json_storage(final_state),
            'processed_signal': processed_signal,
            'tradingagents_mode': True
        }
        
        # Update using the db_storage function which properly merges state
        update_market_analysis_status(
            analysis_id=market_analysis.id,
            status=market_analysis.status,
            state=trading_agent_state
        )
    
    def _handle_analysis_error(self, market_analysis: MarketAnalysis, symbol: str, error_message: str) -> None:
        """Handle analysis errors by storing error state and creating error output."""
        # Store error in market analysis
        market_analysis.state = {
            'error': error_message,
            'error_timestamp': self._get_current_timestamp(),
            'analysis_failed': True,
            'analysis_method': 'tradingagents_full'
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
                text=f"TradingAgents analysis failed for {symbol}: {error_message}"
            )
            session.add(error_output)
            session.commit()
            session.close()
        except Exception as db_error:
            logger.error(f"Failed to store error output: {db_error}", exc_info=True)
    
    def render_market_analysis(self, market_analysis: MarketAnalysis) -> None:
        """
        Render comprehensive TradingAgents market analysis results using the TradingAgentsUI class.
        
        Args:
            market_analysis (MarketAnalysis): The market analysis instance to render.
        """
        try:
            # Import and use the dedicated UI class
            from .TradingAgentsUI import TradingAgentsUI
            
            # Create UI instance and render directly
            trading_ui = TradingAgentsUI(market_analysis)
            trading_ui.render()
            
        except Exception as e:
            logger.error(f"Error rendering market analysis {market_analysis.id}: {e}", exc_info=True)
            # Fallback to error display
            from nicegui import ui
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('error', size='3rem', color='negative').classes('mb-4')
                ui.label('Rendering Error').classes('text-h5 text-negative')
                ui.label(f'Failed to render analysis: {str(e)}').classes('text-grey-7')
    
    def _render_in_progress_analysis(self, market_analysis: MarketAnalysis) -> str:
        """Render analysis in progress state with partial results and running tabs."""
        try:
            # Load analysis outputs for this analysis (even if still running)
            from ...core.db import get_db
            from sqlmodel import select
            
            session = get_db()
            statement = select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == market_analysis.id).order_by(AnalysisOutput.created_at)
            analysis_outputs = session.exec(statement).all()
            session.close()
            
            # Get state data (might have partial data)
            state = market_analysis.state if market_analysis.state else {}
            trading_state = state.get('trading_agent_graph', {}) if isinstance(state, dict) else {}
            
            # Build in-progress HTML content with tabs showing partial results
            return self._build_in_progress_html_tabs(market_analysis, trading_state, analysis_outputs)
            
        except Exception as e:
            logger.error(f"Error rendering in-progress analysis: {e}", exc_info=True)
            return self._render_basic_in_progress()
    
    def _render_basic_in_progress(self) -> str:
        """Render basic in-progress message as fallback."""
        return """⏳ **Analysis in Progress**

The TradingAgents multi-agent analysis is currently running. This includes:
- News sentiment analysis
- Technical indicator analysis  
- Fundamental analysis
- Risk assessment
- Multi-agent debate and consensus

Please check back in a few minutes for results."""
    
    def _render_cancelled_analysis(self, market_analysis: MarketAnalysis) -> str:
        """Render cancelled analysis state."""
        return "❌ **Analysis Cancelled**\n\nThe TradingAgents analysis was cancelled before completion."
    
    def _render_failed_analysis(self, market_analysis: MarketAnalysis) -> str:
        """Render failed analysis state."""
        summary = "⚠️ **TradingAgents Analysis Failed**\n\n"
        if market_analysis.state and isinstance(market_analysis.state, dict):
            error_info = market_analysis.state.get('error', 'Unknown error occurred during analysis.')
            summary += f"**Error Details:** {error_info}\n\n"
        
        summary += "The multi-agent analysis system encountered an error during execution. Please try running the analysis again."
        return summary
    
    def _render_basic_completion(self, market_analysis: MarketAnalysis) -> str:
        """Render basic completion without detailed state."""
        return "✅ **Analysis Completed**\n\nTradingAgents analysis completed successfully but no detailed results are available."
    
    def _render_completed_analysis_comprehensive(self, market_analysis: MarketAnalysis) -> str:
        """Render comprehensive completed analysis with all details, tabs, and interactive content."""
        try:
            # Load analysis outputs for this analysis
            from ...core.db import get_db
            from sqlmodel import select
            
            session = get_db()
            statement = select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == market_analysis.id).order_by(AnalysisOutput.created_at)
            analysis_outputs = session.exec(statement).all()
            session.close()
            
            # Get state data
            state = market_analysis.state if market_analysis.state else {}
            trading_state = state.get('trading_agent_graph', {}) if isinstance(state, dict) else {}
            
            # Build comprehensive HTML content with tabs
            html_content = self._build_analysis_html_tabs(market_analysis, trading_state, analysis_outputs)
            
            return html_content
            
        except Exception as e:
            logger.error(f"Error rendering comprehensive analysis: {e}", exc_info=True)
            return self._render_fallback_analysis(market_analysis)
    
    def _build_analysis_html_tabs(self, market_analysis: MarketAnalysis, trading_state: dict, analysis_outputs: list) -> str:
        """Build HTML content with tabs for comprehensive analysis display."""
        # Extract key data
        recommendation_data = self._extract_recommendation_from_state(trading_state)
        agent_communications = self._extract_llm_outputs_from_state(trading_state)
        grouped_outputs = self._group_analysis_outputs(analysis_outputs)
        
        # Build comprehensive markdown with collapsible sections
        content = self._build_summary_section(market_analysis, recommendation_data, trading_state)
        
        # Agent Communications Section  
        if agent_communications:
            content += self._build_agent_communications_section(agent_communications)
        
        # Tool Outputs Section
        if grouped_outputs:
            content += self._build_tool_outputs_section(grouped_outputs)
        
        # Individual Agent Sections
        agent_names = set()
        if agent_communications:
            agent_names.update(agent_communications.keys())
        if grouped_outputs:
            agent_names.update(grouped_outputs.keys())
        
        for agent_name in sorted(agent_names):
            content += self._build_individual_agent_section(
                agent_name, 
                grouped_outputs.get(agent_name, []), 
                agent_communications.get(agent_name, [])
            )
        
        return content
    
    def _build_summary_section(self, market_analysis: MarketAnalysis, recommendation_data: dict, trading_state: dict) -> str:
        """Build the analysis summary section."""
        content = '# ✅ TradingAgents Analysis Completed\n\n'
        
        # Recommendation section
        if recommendation_data:
            content += '## 🎯 Final Recommendation\n\n'
            content += f"**Action:** {recommendation_data.get('action', 'N/A')}  \n"
            content += f"**Confidence:** {recommendation_data.get('confidence', 'N/A')}  \n"
            if recommendation_data.get('reasoning'):
                content += f"**Reasoning:** {recommendation_data['reasoning']}  \n\n"
        
        # Agent summary
        agent_summaries = self._extract_agent_summaries(trading_state)
        if agent_summaries:
            content += '## 🤖 Agent Analysis Summary\n\n'
            for agent_name, summary in agent_summaries.items():
                content += f"- **{agent_name}:** {summary}\n"
            content += '\n'
        
        # Metadata
        content += '## 📊 Analysis Metadata\n\n'
        content += f"**Symbol:** {market_analysis.symbol}  \n"
        content += f"**Analysis Method:** TradingAgents Multi-Agent System  \n"
        content += f"**Completed:** {market_analysis.created_at.strftime('%Y-%m-%d %H:%M:%S') if market_analysis.created_at else 'Unknown'}  \n"
        content += f"**Expert ID:** {self.id}  \n\n"
        
        content += '*Detailed agent communications and tool outputs are shown below.*\n\n'
        
        return content
    
    def _build_agent_communications_section(self, agent_communications: dict) -> str:
        """Build agent communications section with collapsible details."""
        content = '## 💬 Agent Communications\n\n'
        
        for agent_name, messages in agent_communications.items():
            content += f'<details>\n<summary><strong>{agent_name} ({len(messages)} messages)</strong></summary>\n\n'
            
            for i, message in enumerate(messages):
                message_type = message.get('type', 'unknown')
                msg_content = message.get('content', '')
                
                # Format message based on type
                icon = '🤖' if message_type == 'ai' else '👤' if message_type == 'human' else '⚙️'
                content += f'### {icon} {message_type.title()} Message {i+1}\n\n'
                
                if isinstance(msg_content, str):
                    content += f'```\n{msg_content}\n```\n\n'
                else:
                    content += f'```json\n{json.dumps(msg_content, indent=2)}\n```\n\n'
            
            content += '</details>\n\n'
        
        return content
    
    def _build_tool_outputs_section(self, grouped_outputs: dict) -> str:
        """Build tool outputs section with collapsible details."""
        content = '## 🔧 Tool Execution Outputs\n\n'
        
        for agent_name, outputs in grouped_outputs.items():
            content += f'<details>\n<summary><strong>{agent_name} Tools ({len(outputs)} outputs)</strong></summary>\n\n'
            
            for output in outputs:
                tool_name = output.name or "Unknown Tool"
                timestamp = output.created_at.strftime("%H:%M:%S") if output.created_at else "Unknown time"
                
                content += f'### 🔠 {tool_name} - {timestamp}\n\n'
                
                # Tool parameters
                if hasattr(output, 'tool_parameters') and output.tool_parameters:
                    content += '**Parameters:**\n\n'
                    try:
                        if isinstance(output.tool_parameters, str):
                            params = json.loads(output.tool_parameters)
                        else:
                            params = output.tool_parameters
                        content += f'```json\n{json.dumps(params, indent=2)}\n```\n\n'
                    except:
                        content += f'```\n{str(output.tool_parameters)}\n```\n\n'
                
                # Tool output
                content += '**Output:**\n\n'
                if output.text:
                    try:
                        # Try to format as JSON
                        parsed_json = json.loads(output.text)
                        formatted_output = json.dumps(parsed_json, indent=2, ensure_ascii=False)
                        content += f'```json\n{formatted_output}\n```\n\n'
                    except:
                        # Use as plain text
                        content += f'```\n{output.text}\n```\n\n'
                elif output.blob:
                    content += f'*Binary data ({len(output.blob)} bytes)*\n\n'
                else:
                    content += '*No output content*\n\n'
            
            content += '</details>\n\n'
        
        return content
    
    def _build_individual_agent_section(self, agent_name: str, tool_outputs: list, llm_outputs: list) -> str:
        """Build individual agent section."""
        if not tool_outputs and not llm_outputs:
            return ''
        
        content = f'## 🤖 {agent_name} - Detailed View\n\n'
        
        # LLM Communications
        if llm_outputs:
            content += f'### 💬 {agent_name} Communications\n\n'
            for i, message in enumerate(llm_outputs):
                message_type = message.get('type', 'unknown')
                msg_content = message.get('content', '')
                icon = '🤖' if message_type == 'ai' else '👤' if message_type == 'human' else '⚙️'
                
                content += f'<details>\n<summary><strong>{icon} {message_type.title()} Message {i+1}</strong></summary>\n\n'
                
                if isinstance(msg_content, str):
                    content += f'```\n{msg_content}\n```\n\n'
                else:
                    content += f'```json\n{json.dumps(msg_content, indent=2)}\n```\n\n'
                
                content += '</details>\n\n'
        
        # Tool Outputs
        if tool_outputs:
            content += f'### 🔧 {agent_name} Tool Outputs\n\n'
            for output in tool_outputs:
                tool_name = output.name or "Unknown Tool"
                timestamp = output.created_at.strftime("%H:%M:%S") if output.created_at else "Unknown time"
                
                content += f'<details>\n<summary><strong>🔠 {tool_name} - {timestamp}</strong></summary>\n\n'
                
                if output.text:
                    try:
                        parsed_json = json.loads(output.text)
                        formatted_output = json.dumps(parsed_json, indent=2, ensure_ascii=False)
                        content += f'```json\n{formatted_output}\n```\n\n'
                    except:
                        content += f'```\n{output.text}\n```\n\n'
                elif output.blob:
                    content += f'*Binary data ({len(output.blob)} bytes)*\n\n'
                else:
                    content += '*No output content*\n\n'
                
                content += '</details>\n\n'
        
        return content
    
    def _build_in_progress_html_tabs(self, market_analysis: MarketAnalysis, trading_state: dict, analysis_outputs: list) -> str:
        """Build HTML content with tabs showing in-progress analysis and partial results."""
        # Extract available data
        agent_communications = self._extract_llm_outputs_from_state(trading_state)
        grouped_outputs = self._group_analysis_outputs(analysis_outputs)
        
        # Build in-progress content with status indicators
        content = f'# ⏳ TradingAgents Analysis in Progress - {market_analysis.symbol}\n\n'
        
        # Progress summary
        content += '## 📊 Analysis Progress\n\n'
        content += 'The multi-agent analysis is currently running. Below you can see partial results as they become available:\n\n'
        
        # Show completed and running sections
        sections_status = self._determine_sections_status(trading_state, grouped_outputs, agent_communications)
        for section_name, status in sections_status.items():
            icon = '✅' if status == 'completed' else '⏳' if status == 'running' else '⌛'
            content += f"- {icon} **{section_name}:** {status.title()}\n"
        content += '\n'
        
        # Show any available recommendation (even if partial)
        recommendation_data = self._extract_recommendation_from_state(trading_state)
        if recommendation_data:
            content += '## 🎯 Preliminary Recommendation\n\n'
            content += f"**Action:** {recommendation_data.get('action', 'Analyzing...')}  \n"
            content += f"**Confidence:** {recommendation_data.get('confidence', 'Calculating...')}  \n"
            if recommendation_data.get('reasoning'):
                content += f"**Reasoning:** {recommendation_data['reasoning']}  \n\n"
        else:
            content += '## ⏳ Final Recommendation\n\n'
            content += '*Final recommendation will appear here once analysis is complete...*\n\n'
        
        # Agent Communications Section (with running indicator if empty)
        if agent_communications:
            content += '## 💬 Agent Communications\n\n'
            for agent_name, messages in agent_communications.items():
                content += f'<details>\n<summary><strong>✅ {agent_name} ({len(messages)} messages completed)</strong></summary>\n\n'
                
                for i, message in enumerate(messages):
                    message_type = message.get('type', 'unknown')
                    msg_content = message.get('content', '')
                    
                    icon = '🤖' if message_type == 'ai' else '👤' if message_type == 'human' else '⚙️'
                    content += f'### {icon} {message_type.title()} Message {i+1}\n\n'
                    
                    if isinstance(msg_content, str):
                        content += f'```\n{msg_content}\n```\n\n'
                    else:
                        content += f'```json\n{json.dumps(msg_content, indent=2)}\n```\n\n'
                
                content += '</details>\n\n'
        else:
            content += '## ⏳ Agent Communications\n\n'
            content += '*Agent communications will appear here as the analysis progresses...*\n\n'
        
        # Tool Outputs Section (with running indicators)
        if grouped_outputs:
            content += '## 🔧 Tool Execution Outputs\n\n'
            for agent_name, outputs in grouped_outputs.items():
                content += f'<details>\n<summary><strong>✅ {agent_name} Tools ({len(outputs)} outputs completed)</strong></summary>\n\n'
                
                for output in outputs:
                    tool_name = output.name or "Unknown Tool"
                    timestamp = output.created_at.strftime("%H:%M:%S") if output.created_at else "Unknown time"
                    
                    content += f'### 🔄 {tool_name} - {timestamp}\n\n'
                    
                    # Tool parameters
                    if hasattr(output, 'tool_parameters') and output.tool_parameters:
                        content += '**Parameters:**\n\n'
                        try:
                            if isinstance(output.tool_parameters, str):
                                params = json.loads(output.tool_parameters)
                            else:
                                params = output.tool_parameters
                            content += f'```json\n{json.dumps(params, indent=2)}\n```\n\n'
                        except:
                            content += f'```\n{str(output.tool_parameters)}\n```\n\n'
                    
                    # Tool output
                    content += '**Output:**\n\n'
                    if output.text:
                        try:
                            parsed_json = json.loads(output.text)
                            formatted_output = json.dumps(parsed_json, indent=2, ensure_ascii=False)
                            content += f'```json\n{formatted_output}\n```\n\n'
                        except:
                            content += f'```\n{output.text}\n```\n\n'
                    elif output.blob:
                        content += f'*Binary data ({len(output.blob)} bytes)*\n\n'
                    else:
                        content += '*No output content*\n\n'
                
                content += '</details>\n\n'
        else:
            content += '## ⏳ Tool Execution Outputs\n\n'
            content += '*Tool execution results will appear here as agents complete their analysis...*\n\n'
        
        # Show expected agents that are still running
        expected_agents = ['News Agent', 'Technical Agent', 'Fundamental Agent', 'Risk Agent', 'Portfolio Agent']
        running_agents = set(expected_agents) - set(agent_communications.keys()) - set(grouped_outputs.keys())
        
        if running_agents:
            content += '## ⏳ Running Agents\n\n'
            content += 'The following agents are currently analyzing:\n\n'
            for agent in sorted(running_agents):
                content += f'- ⏳ **{agent}:** Analysis in progress...\n'
            content += '\n'
        
        # Refresh notice
        content += '---\n\n'
        content += '**💡 Tip:** This page will automatically update as the analysis progresses. '
        content += 'Refresh the page to see the latest results.\n\n'
        
        return content
    
    def _determine_sections_status(self, trading_state: dict, grouped_outputs: dict, agent_communications: dict) -> dict:
        """Determine the status of different analysis sections."""
        status = {}
        
        # Check recommendation status
        if self._extract_recommendation_from_state(trading_state):
            status['Final Recommendation'] = 'completed'
        else:
            status['Final Recommendation'] = 'running'
        
        # Check agent communications
        if agent_communications:
            status['Agent Communications'] = 'completed'
        else:
            status['Agent Communications'] = 'running'
        
        # Check tool outputs
        if grouped_outputs:
            status['Tool Execution'] = 'completed'
        else:
            status['Tool Execution'] = 'running'
        
        # Check individual agents
        expected_agents = ['News Analysis', 'Technical Analysis', 'Fundamental Analysis', 'Risk Assessment', 'Portfolio Analysis']
        for agent in expected_agents:
            if any(agent.lower().replace(' ', '_') in key.lower() for key in trading_state.keys()):
                status[agent] = 'completed'
            else:
                status[agent] = 'running'
        
        return status
    
    def _render_fallback_analysis(self, market_analysis: MarketAnalysis) -> str:
        """Render a fallback analysis when comprehensive rendering fails."""
        summary = "✅ **TradingAgents Analysis Completed**\n\n"
        
        if market_analysis.state and isinstance(market_analysis.state, dict):
            trading_state = market_analysis.state.get('trading_agent_graph', {})
            if trading_state:
                # Try to extract basic recommendation
                recommendation_data = self._extract_recommendation_from_state(trading_state)
                if recommendation_data:
                    summary += "## 🎯 Final Recommendation\n\n"
                    summary += f"**Action:** {recommendation_data.get('action', 'N/A')}  \n"
                    summary += f"**Confidence:** {recommendation_data.get('confidence', 'N/A')}  \n"
                    if recommendation_data.get('reasoning'):
                        summary += f"**Reasoning:** {recommendation_data['reasoning'][:200]}...  \n\n"
        
        summary += "*Detailed analysis results are available in the database.*\n"
        return summary
    
    def _is_status(self, market_analysis: MarketAnalysis, *statuses: MarketAnalysisStatus) -> bool:
        """Check if market analysis status matches any of the provided statuses (case-insensitive)."""
        if not market_analysis.status:
            return False
        
        current_status = market_analysis.status
        return current_status in statuses
    
    def _format_agent_name_from_key(self, key: str) -> str:
        """Format agent name from state key for display."""
        # Convert keys like "news_agent" or "newsAgent" to "News Agent"
        if '_' in key:
            parts = key.split('_')
            return ' '.join(word.title() for word in parts)
        
        # Handle camelCase
        import re
        camel_case_parts = re.findall(r'[A-Z][a-z]*', key)
        if camel_case_parts:
            return ' '.join(camel_case_parts)
        
        return key.title()
    
    def _extract_recommendation_from_state(self, trading_state: dict) -> dict:
        """Extract recommendation data from trading state."""
        recommendation_data = {}
        
        # Look for prediction_result first
        if 'prediction_result' in trading_state:
            pred_result = trading_state['prediction_result']
            if isinstance(pred_result, dict):
                recommendation_data['action'] = pred_result.get('signal', 'N/A')
                recommendation_data['confidence'] = f"{pred_result.get('confidence', 0):.1f}%" if pred_result.get('confidence') else 'N/A'
                recommendation_data['reasoning'] = pred_result.get('reasoning', '')
                return recommendation_data
        
        # Look for other recommendation formats
        for key in ['final_recommendation', 'recommendation', 'expert_recommendation']:
            if key in trading_state:
                rec = trading_state[key]
                if isinstance(rec, dict):
                    recommendation_data['action'] = rec.get('action', rec.get('signal', 'N/A'))
                    confidence = rec.get('confidence', 0)
                    if isinstance(confidence, (int, float)):
                        recommendation_data['confidence'] = f"{confidence:.1f}%"
                    else:
                        recommendation_data['confidence'] = str(confidence)
                    recommendation_data['reasoning'] = rec.get('reasoning', rec.get('details', ''))
                    return recommendation_data
        
        return recommendation_data
    
    def _extract_llm_outputs_from_state(self, trading_state: dict) -> dict:
        """Extract LLM outputs from trading state."""
        llm_outputs = {}
        
        for key, value in trading_state.items():
            if isinstance(value, dict) and 'messages' in value:
                messages = value['messages']
                if isinstance(messages, list) and messages:
                    agent_name = self._format_agent_name_from_key(key)
                    llm_outputs[agent_name] = messages
        
        return llm_outputs
    
    def _group_analysis_outputs(self, analysis_outputs: list) -> dict:
        """Group analysis outputs by agent name."""
        grouped = {}
        for output in analysis_outputs:
            agent_name = self._extract_agent_name_from_output(output)
            if agent_name not in grouped:
                grouped[agent_name] = []
            grouped[agent_name].append(output)
        return grouped
    
    def _extract_agent_name_from_output(self, output) -> str:
        """Extract agent name from analysis output name."""
        name = output.name or "Unknown"
        
        # Handle patterns like "agent_name_tool_name" or "AgentName: tool_name"
        if '_' in name:
            parts = name.split('_')
            if len(parts) >= 2:
                return parts[0].title()
        
        if ':' in name:
            return name.split(':')[0].strip()
        
        # Handle patterns like "NewsAgentTool" -> "News Agent"
        import re
        camel_case_pattern = re.findall(r'[A-Z][a-z]*', name)
        if camel_case_pattern and len(camel_case_pattern) >= 2:
            return ' '.join(camel_case_pattern[:-1])  # Exclude "Tool" suffix
        
        return name
    
    def _extract_agent_summaries(self, trading_state: dict) -> dict:
        """Extract agent summaries from trading state."""
        agent_summaries = {}
        
        for key, value in trading_state.items():
            if isinstance(value, dict) and 'messages' in value:
                messages = value['messages']
                if isinstance(messages, list) and messages:
                    agent_name = self._format_agent_name_from_key(key)
                    
                    # Get the last AI message as summary
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get('type') == 'ai':
                            content = message.get('content', '')
                            if content:
                                # Truncate long content
                                summary = str(content)[:150]
                                if len(str(content)) > 150:
                                    summary += "..."
                                agent_summaries[agent_name] = summary
                                break
        
        return agent_summaries

    def _clean_state_for_json_storage(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Clean state data to make it JSON serializable by removing non-serializable objects."""
        cleaned_state = {}
        
        for key, value in state.items():
            if key == 'messages':
                # Store message summary instead of full HumanMessage objects
                if isinstance(value, list):
                    cleaned_state['messages_summary'] = {
                        'count': len(value),
                        'types': [msg.__class__.__name__ for msg in value if hasattr(msg, '__class__')]
                    }
                else:
                    cleaned_state['messages_summary'] = {'count': 0, 'types': []}
            elif key in ['investment_debate_state', 'risk_debate_state']:
                # Keep debate states as they are crucial for UI display
                if isinstance(value, dict):
                    cleaned_value = {}
                    for debate_key, debate_value in value.items():
                        # Ensure all values are JSON serializable
                        if isinstance(debate_value, (str, int, float, bool, type(None))):
                            cleaned_value[debate_key] = debate_value
                        elif isinstance(debate_value, list):
                            # Clean list items
                            cleaned_list = []
                            for item in debate_value:
                                if isinstance(item, (str, int, float, bool, type(None))):
                                    cleaned_list.append(item)
                                else:
                                    cleaned_list.append(str(item))
                            cleaned_value[debate_key] = cleaned_list
                        else:
                            cleaned_value[debate_key] = str(debate_value)
                    cleaned_state[key] = cleaned_value
                else:
                    cleaned_state[key] = str(value) if value is not None else ""
            elif isinstance(value, (str, int, float, bool, type(None))):
                # Keep simple types as-is
                cleaned_state[key] = value
            elif isinstance(value, (dict, list)):
                # Try to keep dictionaries and lists, but convert complex objects to strings
                try:
                    json.dumps(value)  # Test if it's JSON serializable
                    cleaned_state[key] = value
                except (TypeError, ValueError):
                    # If not serializable, convert to string representation
                    cleaned_state[key] = str(value)
            else:
                # Convert everything else to string
                cleaned_state[key] = str(value)
        
        return cleaned_state
    
    def _notify_trade_manager(self, recommendation_id: int, symbol: str) -> None:
        """
        Notify the Trade Manager about a new recommendation and trigger order creation if enabled.
        
        Args:
            recommendation_id: The ID of the created ExpertRecommendation
            symbol: The trading symbol
        """
        try:
            # Check if automatic trade opening is enabled for this expert
            allow_automated_trade_opening = self.settings.get('allow_automated_trade_opening', False)
            # Also check legacy setting for backward compatibility
            legacy_automatic_trading = self.settings.get('automatic_trading', False)
            
            if not allow_automated_trade_opening and not legacy_automatic_trading:
                logger.debug(f"[TRADE MANAGER] Automatic trade opening disabled for expert {self.id}, skipping order creation for {symbol}")
                return
            
            # Get the recommendation from database
            from ...core.models import ExpertRecommendation
            from ...core.db import get_instance
            from ...core.TradeManager import get_trade_manager
            
            recommendation = get_instance(ExpertRecommendation, recommendation_id)
            if not recommendation:
                logger.error(f"[TRADE MANAGER] ExpertRecommendation {recommendation_id} not found")
                return
            
            # Skip HOLD recommendations as they don't require orders
            if recommendation.recommended_action == OrderRecommendation.HOLD:
                logger.debug(f"[TRADE MANAGER] HOLD recommendation for {symbol}, no order needed")
                return
            
            # Get the trade manager and process the recommendation
            trade_manager = get_trade_manager()
            placed_order = trade_manager.process_recommendation(recommendation)
            
            if placed_order:
                logger.info(f"[TRADE MANAGER] Successfully created order {placed_order.id} for {symbol} "
                           f"({recommendation.recommended_action.value}) based on recommendation {recommendation_id}")
            else:
                logger.info(f"[TRADE MANAGER] No order created for {symbol} recommendation {recommendation_id} "
                           f"(may be filtered by rules or permissions)")
                
        except Exception as e:
            logger.error(f"[TRADE MANAGER] Error notifying trade manager for recommendation {recommendation_id}, symbol {symbol}: {e}", exc_info=True)