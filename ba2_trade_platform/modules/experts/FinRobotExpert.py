from typing import Any, Dict, List, Optional
from datetime import datetime, timezone, timedelta
import json
import autogen

from ...core.MarketExpertInterface import MarketExpertInterface
from ...core.models import ExpertInstance, MarketAnalysis, AnalysisOutput, ExpertRecommendation
from ...core.db import get_db, get_instance, update_instance, add_instance, get_setting
from ...core.types import MarketAnalysisStatus, OrderRecommendation, RiskLevel, TimeHorizon, AnalysisUseCase
from ...logger import logger
#from ...thirdparties.FinRobot.finrobot.agents.workflow import SingleAssistant
#from ...thirdparties.FinRobot.finrobot.utils import get_current_date


class FinRobotExpert(MarketExpertInterface):
    """
    FinRobot Expert Implementation
    
    AI Agent Platform for financial analysis using Large Language Models.
    Integrates market data, news, and financial statements through
    specialized AI agents for comprehensive market analysis.
    """
    
    @classmethod
    def description(cls) -> str:
        return "AI Agent Platform for financial analysis with LLM-powered market insights"
    
    @classmethod
    def is_available(cls) -> bool:
        """FinRobot expert is currently disabled."""
        return False
    
    def __init__(self, id: int):
        """Initialize FinRobot expert with database instance."""
        super().__init__(id)
        logger.debug(f'Initializing FinRobot expert with instance ID: {id}')
        return
        self._setup_api_keys()
        self._load_expert_instance(id)
        self._setup_llm_config()
    
    def _setup_api_keys(self) -> None:
        """Setup API keys from application configuration."""
        try:
            # FinRobot uses FINNHUB_API_KEY and OpenAI API keys
            # These should be in our global config
            from ...thirdparties.FinRobot.finrobot.utils import register_keys_from_json
            
            # Check if we need to register keys
            # Our system already has them in environment variables
            logger.debug("API keys loaded from application configuration")
        except Exception as e:
            logger.warning(f"Could not setup API keys: {e}")
    
    def _load_expert_instance(self, id: int) -> None:
        """Load and validate expert instance from database."""
        self.instance = get_instance(ExpertInstance, id)
        if not self.instance:
            raise ValueError(f"ExpertInstance with ID {id} not found")
        logger.debug(f'FinRobot expert loaded: {self.instance.expert}')
    
    def _setup_llm_config(self) -> None:
        """Setup LLM configuration for AutoGen."""
        # Get OpenAI API key from database settings
        openai_api_key = get_setting("openai_api_key")
        if not openai_api_key:
            raise ValueError("openai_api_key not configured in database settings")
        
        # Get model from settings
        settings_def = self.get_settings_definitions()
        model = self.settings.get('llm_model', settings_def['llm_model']['default'])
        temperature = self.settings.get('temperature', settings_def['temperature']['default'])
        timeout = self.settings.get('timeout', settings_def['timeout']['default'])
        
        self.llm_config = {
            "config_list": [{
                "model": model,
                "api_key": openai_api_key,
            }],
            "timeout": timeout,
            "temperature": temperature,
        }
        logger.debug(f"LLM config setup with model: {model}")
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """Define configurable settings for FinRobot expert."""
        return {
            # LLM Configuration
            "llm_model": {
                "type": "str", "required": True, "default": "gpt-5-mini",
                "description": "LLM model for analysis",
                "valid_values": [
                    "gpt-5", 
                    "gpt-5-mini", 
                    "gpt-5-nano", 
                    "gpt-4.1", 
                    "gpt-4.1-mini", 
                    "gpt-4.1-nano", 
                    "o4-mini",
                    "o4-mini-deep-research"
                ],
                "tooltip": "The OpenAI model used for financial analysis. GPT-5 variants provide better analysis. Mini/nano variants balance cost and performance. The o4-mini variants provide enhanced analytical capabilities."
            },
            "temperature": {
                "type": "float", "required": True, "default": 0.0,
                "description": "LLM temperature for response randomness",
                "tooltip": "Controls randomness in AI responses. 0 = deterministic/focused, 1 = creative/random. Recommended: 0-0.3 for financial analysis."
            },
            "timeout": {
                "type": "int", "required": True, "default": 120,
                "description": "API timeout in seconds",
                "tooltip": "Maximum time to wait for LLM responses. Increase if getting timeout errors. Recommended: 120-300 seconds."
            },
            
            # Analysis Configuration
            "human_input_mode": {
                "type": "str", "required": True, "default": "NEVER",
                "description": "Human interaction mode",
                "valid_values": ["NEVER", "ALWAYS", "TERMINATE"],
                "tooltip": "NEVER = fully automated, ALWAYS = require human approval for each step, TERMINATE = only at completion. Use NEVER for automated trading."
            },
            "max_auto_reply": {
                "type": "int", "required": True, "default": 10,
                "description": "Maximum consecutive auto-replies",
                "tooltip": "Limits how many times the agent can respond automatically to prevent infinite loops. Recommended: 5-15."
            },
            
            # Data Lookback Periods  
            "news_lookback_days": {
                "type": "int", "required": True, "default": 7,
                "description": "Days of news data to analyze",
                "tooltip": "How many days back to search for company news. More days = broader context. Recommended: 3-14 days."
            },
            "market_data_days": {
                "type": "int", "required": True, "default": 90,
                "description": "Days of market history for analysis",
                "tooltip": "Historical price data window for technical analysis. Recommended: 60-180 days."
            },
            
            # Code Execution
            "use_docker": {
                "type": "bool", "required": True, "default": False,
                "description": "Use Docker for code execution",
                "tooltip": "Run generated code in isolated Docker containers for security. Requires Docker installed. Disable for simpler setup."
            },
            "work_dir": {
                "type": "str", "required": True, "default": "finrobot_workspace",
                "description": "Working directory for code execution",
                "tooltip": "Directory where FinRobot saves generated code and analysis files."
            },
            
            # Analysis Prompts
            "include_news": {
                "type": "bool", "required": True, "default": True,
                "description": "Include news analysis",
                "tooltip": "Retrieve and analyze recent company news as part of the analysis."
            },
            "include_financials": {
                "type": "bool", "required": True, "default": True,
                "description": "Include financial statements",
                "tooltip": "Retrieve and analyze company financial statements (balance sheet, income statement, cash flow)."
            },
            "include_technical": {
                "type": "bool", "required": True, "default": True,
                "description": "Include technical analysis",
                "tooltip": "Retrieve and analyze stock price data and technical indicators."
            },
        }
    
    def _create_market_analyst(self) -> SingleAssistant:
        """Create a FinRobot Market_Analyst agent instance."""
        # Get settings
        settings_def = self.get_settings_definitions()
        human_input_mode = self.settings.get('human_input_mode', settings_def['human_input_mode']['default'])
        max_auto_reply = self.settings.get('max_auto_reply', settings_def['max_auto_reply']['default'])
        use_docker = self.settings.get('use_docker', settings_def['use_docker']['default'])
        work_dir = self.settings.get('work_dir', settings_def['work_dir']['default'])
        
        # Create assistant
        assistant = SingleAssistant(
            "Market_Analyst",
            self.llm_config,
            human_input_mode=human_input_mode,
            max_consecutive_auto_reply=max_auto_reply,
            code_execution_config={
                "work_dir": work_dir,
                "use_docker": use_docker,
            }
        )
        
        logger.debug(f"Created Market_Analyst agent for expert instance {self.id}")
        return assistant
    
    def _build_analysis_prompt(self, symbol: str, company_name: str = None) -> str:
        """Build the analysis prompt for the Market_Analyst agent."""
        settings_def = self.get_settings_definitions()
        
        # Get configuration
        include_news = self.settings.get('include_news', settings_def['include_news']['default'])
        include_financials = self.settings.get('include_financials', settings_def['include_financials']['default'])
        include_technical = self.settings.get('include_technical', settings_def['include_technical']['default'])
        
        company = company_name or symbol
        current_date = get_current_date()
        
        # Build prompt components
        data_retrieval = []
        if include_technical:
            data_retrieval.append("recent stock price data and technical indicators")
        if include_financials:
            data_retrieval.append("basic financial statements")
        if include_news:
            data_retrieval.append("recent company news")
        
        data_str = ", ".join(data_retrieval) if data_retrieval else "available market data"
        
        prompt = (
            f"Use all the tools provided to retrieve {data_str} for {company} (symbol: {symbol}) as of {current_date}. "
            f"Analyze the positive developments and potential concerns for {company} with 2-4 most important factors respectively and keep them concise. "
            f"Most factors should be inferred from company related news and financial data. "
            f"Then make a prediction (e.g. up/down by 2-5%) of the {company} stock price movement for next week. "
            f"Provide a confidence level (0-100%) for your prediction and a summary analysis to support it. "
            f"Format your final recommendation as: PREDICTION: [BUY/SELL/HOLD] | CONFIDENCE: [0-100]% | EXPECTED_CHANGE: [+/-X.X]%"
        )
        
        return prompt
    
    def _parse_recommendation_from_response(self, response: str, symbol: str) -> Dict[str, Any]:
        """Parse the agent's response to extract recommendation data."""
        try:
            # Look for formatted recommendation in response
            response_upper = response.upper()
            
            # Extract recommendation (BUY/SELL/HOLD)
            signal = OrderRecommendation.HOLD
            if "PREDICTION: BUY" in response_upper or "RECOMMENDATION: BUY" in response_upper:
                signal = OrderRecommendation.BUY
            elif "PREDICTION: SELL" in response_upper or "RECOMMENDATION: SELL" in response_upper:
                signal = OrderRecommendation.SELL
            elif "PREDICTION: HOLD" in response_upper or "RECOMMENDATION: HOLD" in response_upper:
                signal = OrderRecommendation.HOLD
            
            # Extract confidence (look for percentage)
            confidence = 50.0  # default
            import re
            confidence_match = re.search(r'CONFIDENCE:\s*(\d+(?:\.\d+)?)\s*%', response_upper)
            if confidence_match:
                confidence = float(confidence_match.group(1))
            
            # Extract expected change
            expected_profit = 0.0
            change_match = re.search(r'EXPECTED_CHANGE:\s*([+-]?\d+(?:\.\d+)?)\s*%', response_upper)
            if change_match:
                expected_profit = float(change_match.group(1))
            
            # Determine risk level based on volatility mentioned in response
            risk_level = RiskLevel.MEDIUM
            if any(word in response_upper for word in ["HIGH RISK", "VOLATILE", "RISKY"]):
                risk_level = RiskLevel.HIGH
            elif any(word in response_upper for word in ["LOW RISK", "STABLE", "CONSERVATIVE"]):
                risk_level = RiskLevel.LOW
            
            # Time horizon is typically short-term for FinRobot's weekly predictions
            time_horizon = TimeHorizon.SHORT_TERM
            
            logger.info(f"Parsed recommendation for {symbol}: {signal} (confidence: {confidence}%, expected: {expected_profit:+.1f}%)")
            
            return {
                'signal': signal,
                'confidence': confidence,
                'expected_profit': expected_profit,
                'details': response[:10000],  # Truncate to fit in DB
                'price_at_date': 0.0,  # Will be populated later
                'risk_level': risk_level,
                'time_horizon': time_horizon
            }
            
        except Exception as e:
            logger.error(f"Error parsing recommendation from response: {e}", exc_info=True)
            return {
                'signal': OrderRecommendation.ERROR,
                'confidence': 0.0,
                'expected_profit': 0.0,
                'details': f"Failed to parse recommendation: {str(e)}",
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
                confidence=round(recommendation_data['confidence'], 1),
                risk_level=recommendation_data['risk_level'],
                time_horizon=recommendation_data['time_horizon'],
                market_analysis_id=market_analysis_id,
                created_at=datetime.now(timezone.utc)
            )
            
            recommendation_id = add_instance(expert_recommendation)
            logger.info(f"Created expert recommendation {recommendation_id} for {symbol}: {recommendation_data['signal']}")
            
            return recommendation_id
            
        except Exception as e:
            logger.error(f"Error creating expert recommendation: {e}", exc_info=True)
            raise
    
    def get_prediction_for_instrument(self, symbol: str, **kwargs) -> OrderRecommendation:
        """
        Get a quick prediction for an instrument without full analysis.
        
        Args:
            symbol: Stock ticker symbol
            **kwargs: Additional parameters (company_name, etc.)
        
        Returns:
            OrderRecommendation (BUY, SELL, HOLD, or ERROR)
        """
        try:
            logger.info(f"Getting prediction for {symbol} from FinRobot expert {self.id}")
            
            # Create market analyst
            assistant = self._create_market_analyst()
            
            # Build prompt
            company_name = kwargs.get('company_name', symbol)
            prompt = self._build_analysis_prompt(symbol, company_name)
            
            # Get prediction
            logger.debug(f"Sending prompt to FinRobot Market_Analyst: {prompt[:200]}...")
            result = assistant.chat(prompt)
            
            # Extract response
            if result and hasattr(result, 'chat_history'):
                # Get last message from assistant
                for msg in reversed(result.chat_history):
                    if msg.get('role') == 'assistant' and msg.get('content'):
                        response = msg['content']
                        break
                else:
                    response = str(result)
            else:
                response = str(result)
            
            # Parse recommendation
            recommendation_data = self._parse_recommendation_from_response(response, symbol)
            
            logger.info(f"FinRobot prediction for {symbol}: {recommendation_data['signal']}")
            return recommendation_data['signal']
            
        except Exception as e:
            logger.error(f"Error getting prediction for {symbol}: {e}", exc_info=True)
            return OrderRecommendation.ERROR
    
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """
        Run FinRobot analysis for a symbol and update the MarketAnalysis instance.
        
        This is the abstract method implementation required by MarketExpertInterface.
        It delegates to run_market_analysis for the actual analysis work.
        
        Args:
            symbol: Financial instrument symbol to analyze
            market_analysis: MarketAnalysis instance to update with results
        """
        logger.info(f"[START] Starting FinRobot analysis for {symbol} (Analysis ID: {market_analysis.id})")
        
        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            
            # Create market analyst
            assistant = self._create_market_analyst()
            
            # Build and send prompt
            prompt = self._build_analysis_prompt(symbol, symbol)
            
            logger.info(f"Running FinRobot analysis for {symbol}...")
            result = assistant.chat(prompt)
            
            # Extract response and save outputs
            response = ""
            chat_history = []
            
            if result and hasattr(result, 'chat_history'):
                chat_history = result.chat_history
                messages = []
                for msg in chat_history:
                    if msg.get('content'):
                        messages.append(f"{msg.get('role', 'unknown')}: {msg['content']}")
                response = "\n\n".join(messages)
            else:
                response = str(result)
            
            # Save chat history as structured outputs
            self._save_chat_outputs(chat_history, market_analysis.id, symbol)
            
            # Parse recommendation
            recommendation_data = self._parse_recommendation_from_response(response, symbol)
            
            # Get current price for price_at_date
            try:
                from ...modules.dataproviders import YFinanceDataProvider
                data_provider = YFinanceDataProvider()
                hist = data_provider.get_historical_data(symbol, period="1d", interval="1d")
                if not hist.empty:
                    recommendation_data['price_at_date'] = float(hist['Close'].iloc[-1])
            except Exception as e:
                logger.warning(f"Could not get current price for {symbol}: {e}")
            
            # Create recommendation
            recommendation_id = self._create_expert_recommendation(
                recommendation_data, symbol, market_analysis.id
            )
            
            # Create analysis output for full conversation
            analysis_output = AnalysisOutput(
                market_analysis_id=market_analysis.id,
                name="FinRobot Conversation",
                type="conversation",
                content=response,
                created_at=datetime.now(timezone.utc)
            )
            add_instance(analysis_output)
            
            # Save final recommendation as separate output
            rec_summary = self._format_recommendation_summary(recommendation_data)
            rec_output = AnalysisOutput(
                market_analysis_id=market_analysis.id,
                name="Final Recommendation",
                type="recommendation",
                content=rec_summary,
                created_at=datetime.now(timezone.utc)
            )
            add_instance(rec_output)
            
            # Update market analysis status
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            market_analysis.state = {
                'recommendation_id': recommendation_id,
                'signal': recommendation_data['signal'].value,
                'confidence': recommendation_data['confidence'],
                'completed_at': datetime.now(timezone.utc).isoformat()
            }
            # Explicitly mark the state field as modified for SQLAlchemy
            from sqlalchemy.orm import attributes
            attributes.flag_modified(market_analysis, "state")
            update_instance(market_analysis)
            
            logger.info(f"[COMPLETE] FinRobot analysis completed for {symbol}: "
                       f"{recommendation_data['signal']} ({recommendation_data['confidence']:.1f}% confidence)")
        
        except Exception as e:
            logger.error(f"[FAILED] FinRobot analysis failed for {symbol}: {e}", exc_info=True)
            try:
                market_analysis.status = MarketAnalysisStatus.FAILED
                market_analysis.state = {
                    'error': str(e),
                    'failed_at': datetime.now(timezone.utc).isoformat()
                }
                from sqlalchemy.orm import attributes
                attributes.flag_modified(market_analysis, "state")
                update_instance(market_analysis)
            except Exception as update_error:
                logger.error(f"Error updating failed status: {update_error}")
            raise
    
    def run_market_analysis(self, symbol: str, subtype: str = AnalysisUseCase.ENTER_MARKET, **kwargs) -> int:
        """
        Run comprehensive market analysis and store results.
        
        Args:
            symbol: Stock ticker symbol
            subtype: Type of analysis (ENTER_MARKET, OPEN_POSITIONS, etc.)
            **kwargs: Additional parameters
        
        Returns:
            MarketAnalysis ID
        """
        market_analysis = None
        
        try:
            logger.info(f"Starting FinRobot market analysis for {symbol} (subtype: {subtype})")
            
            # Create market analysis record
            market_analysis = MarketAnalysis(
                symbol=symbol,
                expert_instance_id=self.id,
                status=MarketAnalysisStatus.RUNNING,
                subtype=subtype,
                state={},
                created_at=datetime.now(timezone.utc)
            )
            analysis_id = add_instance(market_analysis)
            market_analysis.id = analysis_id
            
            logger.debug(f"Created market analysis record {analysis_id}")
            
            # Create market analyst
            assistant = self._create_market_analyst()
            
            # Build and send prompt
            company_name = kwargs.get('company_name', symbol)
            prompt = self._build_analysis_prompt(symbol, company_name)
            
            logger.info(f"Running FinRobot analysis for {symbol}...")
            result = assistant.chat(prompt)
            
            # Extract response and save outputs
            response = ""
            chat_history = []
            
            if result and hasattr(result, 'chat_history'):
                chat_history = result.chat_history
                # Collect all messages for full analysis details
                messages = []
                for msg in chat_history:
                    if msg.get('content'):
                        messages.append(f"{msg.get('role', 'unknown')}: {msg['content']}")
                response = "\n\n".join(messages)
            else:
                response = str(result)
            
            # Save chat history as structured outputs
            self._save_chat_outputs(chat_history, analysis_id, symbol)
            
            # Parse recommendation
            recommendation_data = self._parse_recommendation_from_response(response, symbol)
            
            # Get current price for price_at_date
            try:
                from ...modules.dataproviders import YFinanceDataProvider
                data_provider = YFinanceDataProvider()
                hist = data_provider.get_historical_data(symbol, period="1d", interval="1d")
                if not hist.empty:
                    recommendation_data['price_at_date'] = float(hist['Close'].iloc[-1])
            except Exception as e:
                logger.warning(f"Could not get current price for {symbol}: {e}")
            
            # Create recommendation
            recommendation_id = self._create_expert_recommendation(
                recommendation_data, symbol, analysis_id
            )
            
            # Create analysis output for full conversation
            analysis_output = AnalysisOutput(
                market_analysis_id=analysis_id,
                name="FinRobot Conversation",
                type="conversation",
                content=response,
                created_at=datetime.now(timezone.utc)
            )
            add_instance(analysis_output)
            
            # Save final recommendation as separate output
            rec_summary = self._format_recommendation_summary(recommendation_data)
            rec_output = AnalysisOutput(
                market_analysis_id=analysis_id,
                name="Final Recommendation",
                type="recommendation",
                content=rec_summary,
                created_at=datetime.now(timezone.utc)
            )
            add_instance(rec_output)
            
            # Update market analysis status
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            market_analysis.state = {
                'recommendation_id': recommendation_id,
                'signal': recommendation_data['signal'].value,
                'confidence': recommendation_data['confidence'],
                'completed_at': datetime.now(timezone.utc).isoformat()
            }
            update_instance(market_analysis)
            
            logger.info(f"Completed FinRobot analysis {analysis_id} for {symbol}: {recommendation_data['signal']}")
            return analysis_id
            
        except Exception as e:
            logger.error(f"Error running market analysis for {symbol}: {e}", exc_info=True)
            
            # Update status to failed if we have an analysis record
            if market_analysis and market_analysis.id:
                try:
                    market_analysis.status = MarketAnalysisStatus.FAILED
                    market_analysis.state = {
                        'error': str(e),
                        'failed_at': datetime.now(timezone.utc).isoformat()
                    }
                    update_instance(market_analysis)
                except Exception as update_error:
                    logger.error(f"Error updating failed status: {update_error}")
            
            raise
    
    def _save_chat_outputs(self, chat_history: list, analysis_id: int, symbol: str) -> None:
        """Save chat history as structured AnalysisOutput records."""
        try:
            for idx, msg in enumerate(chat_history):
                if not msg.get('content'):
                    continue
                
                role = msg.get('role', 'unknown')
                content = msg.get('content', '')
                
                # Determine output type and name
                if role == 'assistant':
                    output_name = f"Agent Response {idx + 1}"
                    output_type = "agent_response"
                elif role == 'user':
                    output_name = f"User Query {idx + 1}"
                    output_type = "user_query"
                elif role == 'function':
                    output_name = f"Tool Output {idx + 1}"
                    output_type = "tool_output"
                else:
                    output_name = f"{role.title()} Message {idx + 1}"
                    output_type = role
                
                # Create analysis output
                analysis_output = AnalysisOutput(
                    market_analysis_id=analysis_id,
                    name=output_name,
                    type=output_type,
                    content=content if isinstance(content, str) else json.dumps(content),
                    created_at=datetime.now(timezone.utc)
                )
                add_instance(analysis_output)
                
            logger.debug(f"Saved {len(chat_history)} chat outputs for analysis {analysis_id}")
            
        except Exception as e:
            logger.error(f"Error saving chat outputs: {e}", exc_info=True)
    
    def _format_recommendation_summary(self, recommendation_data: Dict[str, Any]) -> str:
        """Format recommendation data as a readable summary."""
        signal = recommendation_data.get('signal', 'UNKNOWN')
        signal_str = signal.value if hasattr(signal, 'value') else str(signal)
        confidence = recommendation_data.get('confidence', 0.0)
        expected_profit = recommendation_data.get('expected_profit', 0.0)
        risk_level = recommendation_data.get('risk_level', 'MEDIUM')
        risk_str = risk_level.value if hasattr(risk_level, 'value') else str(risk_level)
        time_horizon = recommendation_data.get('time_horizon', 'SHORT_TERM')
        time_str = time_horizon.value if hasattr(time_horizon, 'value') else str(time_horizon)
        
        summary = f"""# Final Recommendation

**Action:** {signal_str}
**Confidence:** {confidence:.1f}%
**Expected Change:** {expected_profit:+.2f}%
**Risk Level:** {risk_str}
**Time Horizon:** {time_str}

## Analysis Summary

{recommendation_data.get('details', 'No additional details available.')}
"""
        return summary
    
    def render_market_analysis(self, market_analysis: MarketAnalysis) -> None:
        """Render FinRobot market analysis results in a nice UI."""
        from nicegui import ui
        from ...core.db import get_db
        from sqlmodel import select
        
        try:
            # Load analysis outputs
            session = get_db()
            statement = select(AnalysisOutput).where(
                AnalysisOutput.market_analysis_id == market_analysis.id
            ).order_by(AnalysisOutput.created_at)
            analysis_outputs = session.exec(statement).all()
            
            # Load expert recommendation
            rec_statement = select(ExpertRecommendation).where(
                ExpertRecommendation.market_analysis_id == market_analysis.id
            )
            recommendation = session.exec(rec_statement).first()
            session.close()
            
            # Group outputs by type
            outputs_by_type = {}
            for output in analysis_outputs:
                output_type = output.type or 'other'
                if output_type not in outputs_by_type:
                    outputs_by_type[output_type] = []
                outputs_by_type[output_type].append(output)
            
            # Render summary section
            self._render_summary_section(market_analysis, recommendation)
            
            # Create tabs for different output types
            with ui.tabs().classes('w-full') as tabs:
                tab_recommendation = ui.tab('📋 Recommendation', icon='assessment')
                tab_conversation = ui.tab('💬 Conversation', icon='chat')
                tab_analysis = ui.tab('🔍 Analysis', icon='analytics')
                tab_raw = ui.tab('📄 Raw Data', icon='code')
            
            with ui.tab_panels(tabs, value=tab_recommendation).classes('w-full'):
                # Recommendation Tab
                with ui.tab_panel(tab_recommendation):
                    self._render_recommendation_panel(recommendation, outputs_by_type.get('recommendation', []))
                
                # Conversation Tab
                with ui.tab_panel(tab_conversation):
                    self._render_conversation_panel(
                        outputs_by_type.get('agent_response', []),
                        outputs_by_type.get('user_query', []),
                        outputs_by_type.get('tool_output', [])
                    )
                
                # Analysis Tab
                with ui.tab_panel(tab_analysis):
                    self._render_analysis_panel(outputs_by_type.get('conversation', []))
                
                # Raw Data Tab
                with ui.tab_panel(tab_raw):
                    self._render_raw_data_panel(analysis_outputs)
        
        except Exception as e:
            logger.error(f"Error rendering FinRobot analysis: {e}", exc_info=True)
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('error', size='3rem', color='negative').classes('mb-4')
                ui.label('Rendering Error').classes('text-h5 text-negative')
                ui.label(f'Failed to render analysis: {str(e)}').classes('text-grey-7')
    
    def _render_summary_section(self, market_analysis: MarketAnalysis, recommendation: Optional[ExpertRecommendation]) -> None:
        """Render the analysis summary section."""
        from nicegui import ui
        
        with ui.card().classes('w-full mb-4 bg-blue-50'):
            ui.label(f'🤖 FinRobot Analysis - {market_analysis.symbol}').classes('text-h5 mb-4')
            
            with ui.row().classes('w-full gap-8'):
                # Recommendation summary
                with ui.column().classes('flex-1'):
                    ui.label('Final Recommendation').classes('text-subtitle1 font-bold mb-2')
                    
                    if recommendation:
                        action = recommendation.recommended_action.value if hasattr(recommendation.recommended_action, 'value') else str(recommendation.recommended_action)
                        confidence = recommendation.confidence or 0.0
                        
                        # Action with color coding
                        action_colors = {'BUY': 'green', 'SELL': 'red', 'HOLD': 'orange', 'ERROR': 'grey'}
                        action_icons = {'BUY': '📈', 'SELL': '📉', 'HOLD': '⏸️', 'ERROR': '❌'}
                        
                        with ui.row().classes('items-center mb-2'):
                            ui.label(action_icons.get(action, '📊')).classes('text-2xl mr-2')
                            ui.label(f'{action}').classes(f'text-xl font-bold text-{action_colors.get(action, "grey")}-600')
                        
                        ui.label(f'Confidence: {confidence:.1f}%').classes('text-sm')
                        
                        if recommendation.expected_profit_percent:
                            ui.label(f'Expected Change: {recommendation.expected_profit_percent:+.2f}%').classes('text-sm')
                    else:
                        ui.label('No recommendation available').classes('text-grey-600')
                
                # Risk assessment
                with ui.column().classes('flex-1'):
                    ui.label('Risk Assessment').classes('text-subtitle1 font-bold mb-2')
                    
                    if recommendation:
                        risk_level = recommendation.risk_level.value if hasattr(recommendation.risk_level, 'value') else str(recommendation.risk_level)
                        time_horizon = recommendation.time_horizon.value if hasattr(recommendation.time_horizon, 'value') else str(recommendation.time_horizon)
                        
                        risk_colors = {'LOW': 'green', 'MEDIUM': 'orange', 'HIGH': 'red'}
                        ui.label(f'Risk Level: {risk_level}').classes(f'text-sm text-{risk_colors.get(risk_level, "grey")}-600')
                        ui.label(f'Time Horizon: {time_horizon}').classes('text-sm')
                        
                        if recommendation.price_at_date:
                            ui.label(f'Price at Analysis: ${recommendation.price_at_date:.2f}').classes('text-sm')
                    else:
                        ui.label('No risk data available').classes('text-grey-600')
                
                # Analysis metadata
                with ui.column().classes('flex-1'):
                    ui.label('Analysis Details').classes('text-subtitle1 font-bold mb-2')
                    
                    if market_analysis.created_at:
                        created = market_analysis.created_at.replace(tzinfo=timezone.utc).astimezone()
                        ui.label(f'Completed: {created.strftime("%Y-%m-%d %H:%M:%S")}').classes('text-sm')
                    
                    ui.label(f'Status: {market_analysis.status.value}').classes('text-sm')
                    ui.label(f'Expert Instance: {self.id}').classes('text-sm')
    
    def _render_recommendation_panel(self, recommendation: Optional[ExpertRecommendation], rec_outputs: list) -> None:
        """Render the recommendation panel."""
        from nicegui import ui
        
        if recommendation and recommendation.details:
            with ui.card().classes('w-full'):
                ui.markdown(recommendation.details)
        elif rec_outputs:
            for output in rec_outputs:
                with ui.card().classes('w-full mb-4'):
                    ui.label(output.name).classes('text-h6 mb-2')
                    ui.markdown(output.content)
        else:
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('info', size='2rem', color='grey').classes('mb-4')
                ui.label('No detailed recommendation available').classes('text-grey-600')
    
    def _render_conversation_panel(self, agent_responses: list, user_queries: list, tool_outputs: list) -> None:
        """Render the conversation panel."""
        from nicegui import ui
        
        # Combine and sort all messages by creation time
        all_messages = []
        
        for output in agent_responses:
            all_messages.append(('agent', output))
        
        for output in user_queries:
            all_messages.append(('user', output))
        
        for output in tool_outputs:
            all_messages.append(('tool', output))
        
        # Sort by created_at
        all_messages.sort(key=lambda x: x[1].created_at if x[1].created_at else datetime.min.replace(tzinfo=timezone.utc))
        
        if not all_messages:
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('info', size='2rem', color='grey').classes('mb-4')
                ui.label('No conversation data available').classes('text-grey-600')
            return
        
        # Render messages
        for msg_type, output in all_messages:
            if msg_type == 'agent':
                # Agent response (right-aligned, blue)
                with ui.card().classes('w-full mb-4 bg-blue-50 border-l-4 border-blue-500'):
                    with ui.row().classes('items-start'):
                        ui.icon('smart_toy', color='blue', size='sm').classes('mt-1 mr-2')
                        with ui.column().classes('flex-1'):
                            ui.label(output.name).classes('text-sm font-bold text-blue-800')
                            ui.markdown(output.content).classes('text-sm')
            
            elif msg_type == 'user':
                # User query (left-aligned, green)
                with ui.card().classes('w-full mb-4 bg-green-50 border-l-4 border-green-500'):
                    with ui.row().classes('items-start'):
                        ui.icon('person', color='green', size='sm').classes('mt-1 mr-2')
                        with ui.column().classes('flex-1'):
                            ui.label(output.name).classes('text-sm font-bold text-green-800')
                            ui.markdown(output.content).classes('text-sm')
            
            elif msg_type == 'tool':
                # Tool output (left-aligned, orange)
                with ui.card().classes('w-full mb-4 bg-orange-50 border-l-4 border-orange-500'):
                    with ui.row().classes('items-start'):
                        ui.icon('build', color='orange', size='sm').classes('mt-1 mr-2')
                        with ui.column().classes('flex-1'):
                            ui.label(output.name).classes('text-sm font-bold text-orange-800')
                            with ui.element('pre').classes('bg-white p-3 rounded text-xs overflow-auto max-h-64 whitespace-pre-wrap font-mono border'):
                                ui.label(output.content)
    
    def _render_analysis_panel(self, conversation_outputs: list) -> None:
        """Render the full analysis panel."""
        from nicegui import ui
        
        if not conversation_outputs:
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('info', size='2rem', color='grey').classes('mb-4')
                ui.label('No analysis data available').classes('text-grey-600')
            return
        
        for output in conversation_outputs:
            with ui.card().classes('w-full mb-4'):
                ui.label(output.name).classes('text-h6 mb-2')
                ui.markdown(output.content)
    
    def _render_raw_data_panel(self, analysis_outputs: list) -> None:
        """Render the raw data panel."""
        from nicegui import ui
        
        if not analysis_outputs:
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('info', size='2rem', color='grey').classes('mb-4')
                ui.label('No raw data available').classes('text-grey-600')
            return
        
        with ui.card().classes('w-full'):
            ui.label('All Analysis Outputs').classes('text-h6 mb-4')
            
            # Create table of outputs
            columns = [
                {'name': 'name', 'label': 'Name', 'field': 'name', 'align': 'left'},
                {'name': 'type', 'label': 'Type', 'field': 'type', 'align': 'left'},
                {'name': 'created', 'label': 'Created', 'field': 'created', 'align': 'left'},
                {'name': 'size', 'label': 'Size', 'field': 'size', 'align': 'right'},
            ]
            
            rows = []
            for output in analysis_outputs:
                created = output.created_at.strftime('%Y-%m-%d %H:%M:%S') if output.created_at else 'Unknown'
                content_size = len(output.content) if output.content else (len(output.blob) if output.blob else 0)
                
                rows.append({
                    'id': output.id,
                    'name': output.name or 'Unnamed',
                    'type': output.type or 'unknown',
                    'created': created,
                    'size': f'{content_size} bytes',
                    'content': output.content
                })
            
            table = ui.table(columns=columns, rows=rows, row_key='id').classes('w-full')
            table.add_slot('body', '''
                <q-tr :props="props" @click="props.expand = !props.expand" class="cursor-pointer hover:bg-gray-50">
                    <q-td v-for="col in props.cols" :key="col.name" :props="props">
                        {{ col.value }}
                    </q-td>
                </q-tr>
                <q-tr v-show="props.expand" :props="props">
                    <q-td colspan="100%">
                        <div class="bg-gray-50 p-4 rounded">
                            <pre class="whitespace-pre-wrap text-xs font-mono">{{ props.row.content }}</pre>
                        </div>
                    </q-td>
                </q-tr>
            ''')
