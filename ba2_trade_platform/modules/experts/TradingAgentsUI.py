from nicegui import ui
from typing import Dict, Any, Optional
import json
import html
from datetime import datetime
import pandas as pd
import io

from ...core.models import MarketAnalysis, AnalysisOutput
from ...core.types import MarketAnalysisStatus
from ...logger import logger
# Load expert recommendations using a proper database session
from ...core.db import get_db
from ...core.models import ExpertRecommendation
from sqlmodel import select
from ...ui.components import InstrumentGraph
            

class TradingAgentsUI:
    """
    UI rendering class for TradingAgents market analysis results.
    Provides clean, tabbed interface for displaying comprehensive analysis data.
    """
    
    def __init__(self, market_analysis: MarketAnalysis):
        """
        Initialize the UI renderer with a MarketAnalysis instance.
        
        Args:
            market_analysis: The MarketAnalysis instance to render
        """
        self.market_analysis = market_analysis
        self.state = market_analysis.state if market_analysis.state else {}
        self.trading_state = self.state.get('trading_agent_graph', {}) if isinstance(self.state, dict) else {}
        self._use_stored_indicators = True  # Toggle state for indicators
        self._stored_provider_info = None  # Cache provider info
    
    def _get_ohlcv_data_provider(self):
        """
        Detect which OHLCV provider was used during analysis.
        
        Detection order:
        1. Check database for tool_output_get_ohlcv_data_json with provider metadata
        2. Fallback to expert settings for configured provider
        3. Default to YFinanceDataProvider
        
        Returns:
            Tuple of (provider_instance, provider_info_dict or None)
        """
        try:
            session = get_db()
            try:
                # Look for stored OHLCV data with provider info
                statement = (
                    select(AnalysisOutput)
                    .where(AnalysisOutput.market_analysis_id == self.market_analysis.id)
                    .where(AnalysisOutput.name == 'tool_output_get_ohlcv_data_json')
                )
                output = session.exec(statement).first()
                
                if output and output.text:
                    try:
                        data = json.loads(output.text)
                        provider_module_str = data.get('provider_module')
                        provider_class_name = data.get('provider_class')
                        
                        if provider_module_str and provider_class_name:
                            logger.info(f"Found stored provider info: {provider_module_str}.{provider_class_name}")
                            
                            # Dynamically import the provider class
                            try:
                                parts = provider_module_str.rsplit('.', 1)
                                if len(parts) == 2:
                                    module_path, class_name = parts
                                    module = __import__(module_path, fromlist=[class_name])
                                    provider_class = getattr(module, class_name)
                                    provider_instance = provider_class()
                                    self._stored_provider_info = data
                                    logger.info(f"Successfully instantiated provider from database: {provider_class_name}")
                                    return provider_instance, data
                            except Exception as e:
                                logger.warning(f"Could not reconstruct provider from database metadata: {e}")
                    except json.JSONDecodeError:
                        logger.warning("Could not parse provider JSON from database")
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"Error checking for stored provider info: {e}")
        
        # Fallback: Get from expert settings
        try:
            from ...modules.experts.TradingAgents import TradingAgents
            trading_agents = TradingAgents(self.market_analysis.expert_instance_id)
            provider_setting = trading_agents.settings.get('ohlcv_provider', 'YFinance')
            
            logger.info(f"Using provider from expert settings: {provider_setting}")
            
            if provider_setting == 'FMP':
                from ba2_trade_platform.modules.dataproviders import FMPOHLCVProvider
                return FMPOHLCVProvider(), None
            elif provider_setting == 'AlphaVantage':
                from ba2_trade_platform.modules.dataproviders import AlphaVantageOHLCVProvider
                return AlphaVantageOHLCVProvider(), None
            elif provider_setting == 'Alpaca':
                from ba2_trade_platform.modules.dataproviders import AlpacaOHLCVProvider
                return AlpacaOHLCVProvider(), None
            else:
                from ba2_trade_platform.modules.dataproviders import YFinanceDataProvider
                return YFinanceDataProvider(), None
        except Exception as e:
            logger.warning(f"Error getting provider from settings, defaulting to YFinance: {e}")
            from ba2_trade_platform.modules.dataproviders import YFinanceDataProvider
            return YFinanceDataProvider(), None
    
    def render(self) -> None:
        """Render the complete TradingAgents analysis UI with tabs."""
        try:
            # Handle different analysis states
            if self.market_analysis.status in [MarketAnalysisStatus.PENDING, MarketAnalysisStatus.RUNNING]:
                self._render_in_progress_ui()
            elif self.market_analysis.status == MarketAnalysisStatus.CANCELLED:
                self._render_cancelled_ui()
            elif self.market_analysis.status == MarketAnalysisStatus.FAILED:
                self._render_failed_ui()
            else:
                self._render_completed_ui()
                
        except Exception as e:
            logger.error(f"Error rendering TradingAgents UI: {e}", exc_info=True)
            self._render_error_ui(str(e))
    
    def _render_completed_ui(self) -> None:
        """Render the completed analysis with full tab interface."""
        with ui.tabs().classes('w-full') as tabs:
            summary_tab = ui.tab('📊 Summary')
            dataviz_tab = ui.tab('📉 Data Visualization')
            tools_tab = ui.tab('🔧 Tool Outputs')
            market_tab = ui.tab('📈 Market Analysis')
            sentiment_tab = ui.tab('💬 Social Sentiment') 
            news_tab = ui.tab('📰 News Analysis')
            fundamentals_tab = ui.tab('🏛️ Fundamentals')
            macro_tab = ui.tab('🌍 Macro Analysis')
            debate_tab = ui.tab('🎯 Researcher Debate')
            research_tab = ui.tab('📋 Research Manager')
            trader_tab = ui.tab('💼 Trader Plan')
            risk_tab = ui.tab('⚠️ Risk Debate')
            decision_tab = ui.tab('✅ Final Decision')
        
        with ui.tab_panels(tabs, value=summary_tab).classes('w-full'):
            with ui.tab_panel(summary_tab):
                self._render_summary_panel()
            
            with ui.tab_panel(dataviz_tab):
                self._render_data_visualization_panel()
            
            with ui.tab_panel(tools_tab):
                self._render_tool_outputs_panel()
            
            with ui.tab_panel(market_tab):
                self._render_content_panel('market_report', '📈 Market Analysis', 
                                         'Technical analysis and market indicators')
            
            with ui.tab_panel(sentiment_tab):
                self._render_content_panel('sentiment_report', '💬 Social Sentiment Analysis',
                                         'Social media and public sentiment analysis')
            
            with ui.tab_panel(news_tab):
                self._render_content_panel('news_report', '📰 News Analysis',
                                         'Latest news analysis and market impact')
            
            with ui.tab_panel(fundamentals_tab):
                self._render_content_panel('fundamentals_report', '🏛️ Fundamental Analysis',
                                         'Company financials and fundamental metrics')
            
            with ui.tab_panel(macro_tab):
                self._render_content_panel('macro_report', '🌍 Macroeconomic Analysis',
                                         'Macroeconomic environment and policy analysis')
            
            with ui.tab_panel(debate_tab):
                self._render_debate_panel('investment_debate_state', '🎯 Investment Research Debate',
                                        'Multi-agent debate on investment thesis')
            
            with ui.tab_panel(research_tab):
                self._render_content_panel('investment_plan', '📋 Research Manager Summary',
                                         'Comprehensive investment research summary')
            
            with ui.tab_panel(trader_tab):
                self._render_content_panel('trader_investment_plan', '💼 Trader Investment Plan',
                                         'Actionable trading plan and recommendations')
            
            with ui.tab_panel(risk_tab):
                self._render_debate_panel('risk_debate_state', '⚠️ Risk Management Debate',
                                        'Risk assessment and management strategies')
            
            with ui.tab_panel(decision_tab):
                self._render_content_panel('final_trade_decision', '✅ Final Trading Decision',
                                         'Final recommendation and action plan')
    
    def _render_in_progress_ui(self) -> None:
        """Render in-progress analysis with partial data in tabs."""
        with ui.tabs().classes('w-full') as tabs:
            summary_tab = ui.tab('📊 Summary')
            dataviz_tab = ui.tab('📉 Data Visualization')
            tools_tab = ui.tab('🔧 Tool Outputs')
            market_tab = ui.tab(self._get_tab_label('market_report', '📈 Market Analysis'))
            sentiment_tab = ui.tab(self._get_tab_label('sentiment_report', '💬 Social Sentiment'))
            news_tab = ui.tab(self._get_tab_label('news_report', '📰 News Analysis'))
            fundamentals_tab = ui.tab(self._get_tab_label('fundamentals_report', '🏛️ Fundamentals'))
            macro_tab = ui.tab(self._get_tab_label('macro_report', '🌍 Macro Analysis'))
            debate_tab = ui.tab(self._get_tab_label('investment_debate_state', '🎯 Researcher Debate'))
            research_tab = ui.tab(self._get_tab_label('investment_plan', '📋 Research Manager'))
            trader_tab = ui.tab(self._get_tab_label('trader_investment_plan', '💼 Trader Plan'))
            risk_tab = ui.tab(self._get_tab_label('risk_debate_state', '⚠️ Risk Debate'))
            decision_tab = ui.tab(self._get_tab_label('final_trade_decision', '✅ Final Decision'))
        
        with ui.tab_panels(tabs, value=summary_tab).classes('w-full'):
            with ui.tab_panel(summary_tab):
                self._render_in_progress_summary()
            
            with ui.tab_panel(dataviz_tab):
                self._render_data_visualization_panel()
            
            with ui.tab_panel(tools_tab):
                self._render_tool_outputs_panel()
            
            with ui.tab_panel(market_tab):
                self._render_content_panel('market_report', '📈 Market Analysis', 
                                         'Technical analysis and market indicators')
            
            with ui.tab_panel(sentiment_tab):
                self._render_content_panel('sentiment_report', '💬 Social Sentiment Analysis',
                                         'Social media and public sentiment analysis')
            
            with ui.tab_panel(news_tab):
                self._render_content_panel('news_report', '📰 News Analysis',
                                         'Latest news analysis and market impact')
            
            with ui.tab_panel(fundamentals_tab):
                self._render_content_panel('fundamentals_report', '🏛️ Fundamental Analysis',
                                         'Company financials and fundamental metrics')
            
            with ui.tab_panel(macro_tab):
                self._render_content_panel('macro_report', '🌍 Macroeconomic Analysis',
                                         'Macroeconomic environment and policy analysis')
            
            with ui.tab_panel(debate_tab):
                self._render_debate_panel('investment_debate_state', '🎯 Investment Research Debate',
                                        'Multi-agent debate on investment thesis')
            
            with ui.tab_panel(research_tab):
                self._render_content_panel('investment_plan', '📋 Research Manager Summary',
                                         'Comprehensive investment research summary')
            
            with ui.tab_panel(trader_tab):
                self._render_content_panel('trader_investment_plan', '💼 Trader Investment Plan',
                                         'Actionable trading plan and recommendations')
            
            with ui.tab_panel(risk_tab):
                self._render_debate_panel('risk_debate_state', '⚠️ Risk Management Debate',
                                        'Risk assessment and management strategies')
            
            with ui.tab_panel(decision_tab):
                self._render_content_panel('final_trade_decision', '✅ Final Trading Decision',
                                         'Final recommendation and action plan')
    
    def _get_tab_label(self, state_key: str, default_label: str) -> str:
        """Get tab label with progress indicator."""
        if self._has_content(state_key):
            return default_label.replace('⏳', '✅')
        else:
            return default_label.replace('📈', '⏳').replace('💬', '⏳').replace('📰', '⏳').replace('🏛️', '⏳').replace('🌍', '⏳').replace('🎯', '⏳').replace('📋', '⏳').replace('💼', '⏳').replace('⚠️', '⏳').replace('✅', '⏳')
    
    def _has_content(self, state_key: str) -> bool:
        """Check if state key has meaningful content."""
        if state_key in ['investment_debate_state', 'risk_debate_state']:
            debate_state = self.trading_state.get(state_key, {})
            if isinstance(debate_state, dict):
                return bool(debate_state.get('history') or debate_state.get('current_response') or 
                          debate_state.get('current_risky_response') or debate_state.get('current_safe_response') or
                          debate_state.get('current_neutral_response'))
        else:
            content = self.trading_state.get(state_key, '')
            # Handle both string and list types
            if isinstance(content, list):
                return bool(content)  # List is truthy if non-empty
            elif isinstance(content, str):
                return bool(content and content.strip())
            else:
                return bool(content)  # For other types, just check truthiness
        return False
    
    def _render_summary_panel(self) -> None:
        """Render the summary panel with key insights."""
        with ui.card().classes('w-full'):
            ui.label('Analysis Overview').classes('text-h5 mb-4')
            
            with ui.row().classes('w-full gap-4 mb-6'):
                # Status card
                with ui.card().classes('p-4'):
                    ui.label('Status').classes('text-h6 mb-2')
                    status_icon = '✅' if self.market_analysis.status == MarketAnalysisStatus.COMPLETED else '⏳'
                    ui.label(f'{status_icon} {self.market_analysis.status.value.title()}').classes('text-lg')
                
                # Symbol card
                with ui.card().classes('p-4'):
                    ui.label('Symbol').classes('text-h6 mb-2')
                    ui.label(f'📊 {self.market_analysis.symbol}').classes('text-lg font-bold')
                
                # Timestamp card
                with ui.card().classes('p-4'):
                    ui.label('Analysis Time').classes('text-h6 mb-2')
                    timestamp = self.market_analysis.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.market_analysis.created_at else 'Unknown'
                    ui.label(f'🕒 {timestamp}').classes('text-lg')
            
            # Progress summary
            ui.label('Analysis Components').classes('text-h6 mb-3')
            with ui.grid(columns=2).classes('w-full gap-2'):
                components = [
                    ('Market Analysis', 'market_report', '📈'),
                    ('Social Sentiment', 'sentiment_report', '💬'),
                    ('News Analysis', 'news_report', '📰'),
                    ('Fundamentals', 'fundamentals_report', '🏛️'),
                    ('Macro Analysis', 'macro_report', '🌍'),
                    ('Research Debate', 'investment_debate_state', '🎯'),
                    ('Research Manager', 'investment_plan', '📋'),
                    ('Trader Plan', 'trader_investment_plan', '💼'),
                    ('Risk Debate', 'risk_debate_state', '⚠️'),
                    ('Final Decision', 'final_trade_decision', '✅')
                ]
                
                for name, key, icon in components:
                    status_icon = '✅' if self._has_content(key) else '⏳'
                    status_text = 'Completed' if self._has_content(key) else 'In Progress'
                    ui.label(f'{icon} {status_icon} {name}: {status_text}').classes('text-sm')
            
            # Display Expert Recommendation if available
            self._render_expert_recommendation()
            
            # Final recommendation if available
            final_decision = self.trading_state.get('final_trade_decision', '')
            if final_decision:
                ui.separator().classes('my-4')
                ui.label('🎯 Key Recommendation').classes('text-h6 mb-2')
                
                # Extract recommendation from final decision
                lines = final_decision.split('\n')
                recommendation_line = next((line for line in lines if 'Recommendation:' in line), '')
                if recommendation_line:
                    recommendation = recommendation_line.replace('Recommendation:', '').strip()
                    ui.label(f'{recommendation}').classes('text-lg font-bold text-primary')
    
    def _render_in_progress_summary(self) -> None:
        """Render summary for in-progress analysis."""
        with ui.card().classes('w-full'):
            ui.label('⏳ Analysis in Progress').classes('text-h5 mb-4')
            
            ui.label(f'Analyzing {self.market_analysis.symbol}...').classes('text-lg mb-4')
            
            # Show current step if available
            current_step = self.trading_state.get('current_step', 'initialization')
            ui.label(f'Current Step: {current_step.replace("_", " ").title()}').classes('text-subtitle1 mb-4')
            
            # Show progress
            ui.label('Progress Status:').classes('text-h6 mb-2')
            components = [
                ('Market Analysis', 'market_report'),
                ('Social Sentiment', 'sentiment_report'),
                ('News Analysis', 'news_report'),
                ('Fundamentals', 'fundamentals_report'),
                ('Macro Analysis', 'macro_report'),
                ('Research Debate', 'investment_debate_state'),
                ('Research Manager', 'investment_plan'),
                ('Trader Plan', 'trader_investment_plan'),
                ('Risk Debate', 'risk_debate_state'),
                ('Final Decision', 'final_trade_decision')
            ]
            
            for name, key in components:
                status_icon = '✅' if self._has_content(key) else '⏳'
                status_text = 'Completed' if self._has_content(key) else 'In Progress'
                ui.label(f'{status_icon} {name}: {status_text}')
            
            ui.separator().classes('my-4')
            ui.label('💡 Tip: This page will automatically update as the analysis progresses. Refresh to see the latest results.').classes('text-caption')
    
    def _render_content_panel(self, state_key: str, title: str, description: str) -> None:
        """Render a content panel for text-based analysis results."""
        content = self.trading_state.get(state_key, '')
        
        with ui.card().classes('w-full'):
            ui.label(title).classes('text-h5 mb-2')
            ui.label(description).classes('text-caption text-grey-7 mb-4')
            
            if content and content.strip():
                # Render the content in a scrollable container
                with ui.scroll_area().classes('w-full h-96'):
                    # Try to render as markdown if it looks like markdown, otherwise as pre-formatted text
                    if self._looks_like_markdown(content):
                        ui.markdown(content).classes('w-full')
                    else:
                        with ui.element('pre').classes('whitespace-pre-wrap text-sm p-4 bg-grey-1 rounded font-mono overflow-x-auto'):
                            ui.html(content, sanitize=False)  # Use html() to preserve formatting
            else:
                # Show in-progress indicator
                with ui.card().classes('w-full p-8 text-center bg-grey-1'):
                    ui.icon('hourglass_empty', size='3rem', color='grey-5').classes('mb-4')
                    ui.label('Analysis in progress...').classes('text-h6 text-grey-7')
                    ui.label(f'{description} will appear here once completed.').classes('text-caption text-grey-6')
    
    def _render_debate_panel(self, state_key: str, title: str, description: str) -> None:
        """Render a debate panel with chat-like conversation display."""
        debate_state = self.trading_state.get(state_key, {})
        
        with ui.card().classes('w-full'):
            ui.label(title).classes('text-h5 mb-2')
            ui.label(description).classes('text-caption text-grey-7 mb-4')
            
            if isinstance(debate_state, dict) and debate_state:
                # Check if we have individual messages (new format)
                has_individual_messages = False
                all_messages = []
                
                if state_key == 'investment_debate_state':
                    bull_messages = debate_state.get('bull_messages', [])
                    bear_messages = debate_state.get('bear_messages', [])
                    if bull_messages or bear_messages:
                        has_individual_messages = True
                        # Interleave messages: Bull speaks first, then alternates Bull → Bear → Bull → Bear
                        max_len = max(len(bull_messages), len(bear_messages))
                        for i in range(max_len):
                            if i < len(bull_messages):
                                all_messages.append(('Bull Researcher', bull_messages[i], 'blue'))
                            if i < len(bear_messages):
                                all_messages.append(('Bear Researcher', bear_messages[i], 'red'))
                
                elif state_key == 'risk_debate_state':
                    risky_messages = debate_state.get('risky_messages', [])
                    safe_messages = debate_state.get('safe_messages', [])
                    neutral_messages = debate_state.get('neutral_messages', [])
                    if risky_messages or safe_messages or neutral_messages:
                        has_individual_messages = True
                        # Interleave messages: Risky → Safe → Neutral → Risky → Safe → Neutral cycle
                        max_len = max(len(risky_messages), len(safe_messages), len(neutral_messages))
                        for i in range(max_len):
                            if i < len(risky_messages):
                                all_messages.append(('Risky Analyst', risky_messages[i], 'orange'))
                            if i < len(safe_messages):
                                all_messages.append(('Safe Analyst', safe_messages[i], 'green'))
                            if i < len(neutral_messages):
                                all_messages.append(('Neutral Analyst', neutral_messages[i], 'purple'))
                
                if has_individual_messages:
                    # Render chat-like conversation
                    with ui.scroll_area().classes('w-full h-96'):
                        ui.label('💬 Debate Conversation').classes('text-h6 mb-3')
                        
                        for speaker, message, color in all_messages:
                            self._render_chat_message(speaker, message, color)
                        
                        # Show judge decision if available
                        judge_decision = debate_state.get('judge_decision', '')
                        if judge_decision:
                            ui.separator().classes('my-4')
                            self._render_chat_message('Judge', judge_decision, 'primary')
                        
                        # Debate count
                        count = debate_state.get('count', 0)
                        if count > 0:
                            ui.separator().classes('my-2')
                            ui.label(f'🔄 Total Messages: {count}').classes('text-caption text-grey-6 text-center')
                else:
                    # Fallback to old format display
                    self._render_debate_panel_legacy(debate_state, state_key)
            else:
                # Show in-progress indicator
                with ui.card().classes('w-full p-8 text-center bg-grey-1'):
                    ui.icon('psychology', size='3rem', color='grey-5').classes('mb-4')
                    ui.label('Debate in progress...').classes('text-h6 text-grey-7')
                    ui.label(f'{description} will appear here once completed.').classes('text-caption text-grey-6')
    
    def _render_chat_message(self, speaker: str, message: str, color: str) -> None:
        """Render a single chat message in the debate."""
        # Extract the message content (remove speaker prefix if present)
        content = message
        if content.startswith(f'{speaker}:'):
            content = content[len(f'{speaker}:'):].strip()
        elif ':' in content and content.split(':', 1)[0].strip().endswith('Analyst'):
            content = content.split(':', 1)[1].strip()
        
        # Determine avatar and styling based on speaker
        if 'Bull' in speaker:
            avatar = '🐂'
            bg_color = 'bg-blue-50'
            border_color = 'border-l-4 border-blue-500'
        elif 'Bear' in speaker:
            avatar = '🐻'
            bg_color = 'bg-red-50'
            border_color = 'border-l-4 border-red-500'
        elif 'Risky' in speaker:
            avatar = '⚡'
            bg_color = 'bg-orange-50'
            border_color = 'border-l-4 border-orange-500'
        elif 'Safe' in speaker or 'Conservative' in speaker:
            avatar = '🛡️'
            bg_color = 'bg-green-50'
            border_color = 'border-l-4 border-green-500'
        elif 'Neutral' in speaker:
            avatar = '⚖️'
            bg_color = 'bg-purple-50'
            border_color = 'border-l-4 border-purple-500'
        elif 'Judge' in speaker:
            avatar = '⚖️'
            bg_color = 'bg-indigo-50'
            border_color = 'border-l-4 border-indigo-500'
        else:
            avatar = '🤖'
            bg_color = 'bg-grey-50'
            border_color = 'border-l-4 border-grey-500'
        
        # Render the chat message
        with ui.row().classes('w-full items-start gap-3 mb-4'):
            # Avatar
            with ui.card().classes('flex-shrink-0 w-12 h-12 flex items-center justify-center p-0'):
                ui.label(avatar).classes('text-2xl')
            
            # Message content
            with ui.card().classes(f'flex-1 p-4 {bg_color} {border_color}'):
                ui.label(speaker).classes('font-bold text-sm mb-2')
                
                # Render content based on format
                if self._looks_like_markdown(content):
                    ui.markdown(content).classes('text-sm')
                else:
                    with ui.element('pre').classes('whitespace-pre-wrap text-sm font-mono bg-white p-2 rounded border overflow-x-auto'):
                        ui.html(content)  # Use html() to preserve formatting
    
    def _render_debate_panel_legacy(self, debate_state: dict, state_key: str) -> None:
        """Legacy debate panel rendering for backward compatibility."""
        with ui.scroll_area().classes('w-full h-96'):
            # History
            history = debate_state.get('history', '')
            if history:
                ui.label('📜 Debate History').classes('text-h6 mb-2')
                if self._looks_like_markdown(history):
                    ui.markdown(history).classes('w-full mb-4')
                else:
                    with ui.element('pre').classes('whitespace-pre-wrap text-sm p-4 bg-grey-1 rounded mb-4 font-mono overflow-x-auto'):
                        ui.html(history, sanitize=False)  # Use html() to preserve formatting
            
            # Current responses
            current_response = debate_state.get('current_response', '')
            if current_response:
                ui.label('💬 Current Response').classes('text-h6 mb-2')
                if self._looks_like_markdown(current_response):
                    ui.markdown(current_response).classes('w-full mb-4')
                else:
                    with ui.element('pre').classes('whitespace-pre-wrap text-sm p-4 bg-grey-1 rounded mb-4 font-mono overflow-x-auto'):
                        ui.html(current_response, sanitize=False)  # Use html() to preserve formatting
            
            # Risk-specific responses
            if state_key == 'risk_debate_state':
                risk_response = debate_state.get('current_risky_response', '')
                safe_response = debate_state.get('current_safe_response', '')
                neutral_response = debate_state.get('current_neutral_response', '')
                
                if risk_response:
                    ui.label('⚠️ Risk Assessment').classes('text-h6 mb-2')
                    if self._looks_like_markdown(risk_response):
                        ui.markdown(risk_response).classes('w-full mb-4')
                    else:
                        with ui.element('pre').classes('whitespace-pre-wrap text-sm p-4 bg-red-1 rounded mb-4 font-mono overflow-x-auto'):
                            ui.html(risk_response, sanitize=False)  # Use html() to preserve formatting
                
                if safe_response:
                    ui.label('🛡️ Conservative Assessment').classes('text-h6 mb-2')
                    if self._looks_like_markdown(safe_response):
                        ui.markdown(safe_response).classes('w-full mb-4')
                    else:
                        with ui.element('pre').classes('whitespace-pre-wrap text-sm p-4 bg-green-1 rounded mb-4 font-mono overflow-x-auto'):
                            ui.html(safe_response, sanitize=False)  # Use html() to preserve formatting
                
                if neutral_response:
                    ui.label('⚖️ Balanced Assessment').classes('text-h6 mb-2')
                    if self._looks_like_markdown(neutral_response):
                        ui.markdown(neutral_response).classes('w-full mb-4')
                    else:
                        with ui.element('pre').classes('whitespace-pre-wrap text-sm p-4 bg-blue-1 rounded mb-4 font-mono overflow-x-auto'):
                            ui.html(neutral_response, sanitize=False)  # Use html() to preserve formatting
            
            # Debate count
            count = debate_state.get('count', 0)
            if count > 0:
                ui.label(f'🔄 Debate Rounds: {count}').classes('text-caption text-grey-6')
    
    def _render_cancelled_ui(self) -> None:
        """Render UI for cancelled analysis."""
        with ui.card().classes('w-full p-8 text-center'):
            ui.icon('cancel', size='4rem', color='orange').classes('mb-4')
            ui.label('Analysis Cancelled').classes('text-h4 mb-2')
            ui.label(f'The TradingAgents analysis for {self.market_analysis.symbol} was cancelled before completion.').classes('text-grey-7')
    
    def _render_failed_ui(self) -> None:
        """Render UI for failed analysis."""
        with ui.card().classes('w-full p-8 text-center'):
            ui.icon('error', size='4rem', color='negative').classes('mb-4')
            ui.label('Analysis Failed').classes('text-h4 text-negative mb-2')
            ui.label(f'The TradingAgents analysis for {self.market_analysis.symbol} encountered an error.').classes('text-grey-7 mb-4')
            
            # Show error details if available
            if self.state and isinstance(self.state, dict):
                error_info = self.state.get('error', '')
                if error_info:
                    ui.label('Error Details:').classes('text-h6 mb-2')
                    with ui.element('pre').classes('bg-red-50 p-3 rounded text-sm overflow-auto max-h-32 whitespace-pre-wrap font-mono text-red-900 border'):
                        ui.html(str(error_info), sanitize=False)  # Use html() to preserve formatting
    
    def _render_error_ui(self, error_message: str) -> None:
        """Render UI for rendering errors."""
        with ui.card().classes('w-full p-8 text-center'):
            ui.icon('warning', size='4rem', color='warning').classes('mb-4')
            ui.label('Rendering Error').classes('text-h4 text-warning mb-2')
            ui.label('An error occurred while rendering the analysis results.').classes('text-grey-7 mb-4')
            
            with ui.element('pre').classes('bg-orange-50 p-3 rounded text-sm overflow-auto max-h-32 whitespace-pre-wrap font-mono text-orange-900 border'):
                ui.html(error_message, sanitize=False)  # Use html() to preserve formatting
    
    def _looks_like_markdown(self, content: str) -> bool:
        """Check if content appears to be markdown formatted."""
        if not content:
            return False
        
        markdown_indicators = [
            '##', '**', '*', '- ', '| ', '[', '](', '```', '---', 
            '\n#', '\n*', '\n-', '\n1.', '\n2.', '\n3.', '\n4.', '\n5.',
            '> ', '\n> ', '_', '__', '`', '~~~', '===', '***',
            '![', '\n\n', '\r\n\r\n'  # Additional markdown patterns
        ]
        
        # Check for markdown patterns
        has_markdown = any(indicator in content for indicator in markdown_indicators)
        
        # Also check for structured text patterns that benefit from markdown
        has_structure = (
            content.count('\n') > 3 and  # Multi-line content
            ('Analysis:' in content or 'Summary:' in content or 'Recommendation:' in content or
             'Conclusion:' in content or 'Key Points:' in content or 'Risks:' in content)
        )
        
        return has_markdown or has_structure
    
    def _render_expert_recommendation(self) -> None:
        """Render the expert recommendation if available."""
        try:

            
            session = get_db()
            try:
                statement = select(ExpertRecommendation).where(
                    ExpertRecommendation.market_analysis_id == self.market_analysis.id
                ).order_by(ExpertRecommendation.created_at.desc())
                
                expert_recommendations = session.exec(statement).all()
                
                if not expert_recommendations:
                    return
                
                # Get the most recent expert recommendation
                latest_recommendation = expert_recommendations[0]
            finally:
                session.close()
            
            ui.separator().classes('my-4')
            ui.label('🎯 Expert Recommendation').classes('text-h6 mb-3')
            
            with ui.row().classes('w-full gap-4 mb-4'):
                # Action card
                with ui.card().classes('p-4 flex-1'):
                    ui.label('Recommended Action').classes('text-subtitle2 mb-2')
                    action_icon = {'BUY': '📈', 'SELL': '📉', 'HOLD': '➖', 'ERROR': '❌'}.get(latest_recommendation.recommended_action, '❓')
                    action_color = {
                        'BUY': 'text-green-600', 
                        'SELL': 'text-red-600', 
                        'HOLD': 'text-orange-600', 
                        'ERROR': 'text-grey-600'
                    }.get(latest_recommendation.recommended_action, 'text-grey-600')
                    ui.label(f'{action_icon} {latest_recommendation.recommended_action.value}').classes(f'text-xl font-bold {action_color}')
                
                # Confidence card
                with ui.card().classes('p-4 flex-1'):
                    ui.label('Confidence').classes('text-subtitle2 mb-2')
                    confidence = latest_recommendation.confidence or 0
                    confidence_color = 'text-green-600' if confidence >= 75 else 'text-orange-600' if confidence >= 50 else 'text-red-600'
                    ui.label(f'🎯 {confidence:.1f}%').classes(f'text-xl font-bold {confidence_color}')
                
                # Expected Profit card  
                if latest_recommendation.expected_profit_percent:
                    with ui.card().classes('p-4 flex-1'):
                        ui.label('Expected Profit').classes('text-subtitle2 mb-2')
                        profit = latest_recommendation.expected_profit_percent
                        profit_color = 'text-green-600' if profit > 0 else 'text-red-600' if profit < 0 else 'text-grey-600'
                        profit_icon = '📈' if profit > 0 else '📉' if profit < 0 else '➖'
                        ui.label(f'{profit_icon} {profit:+.1f}%').classes(f'text-xl font-bold {profit_color}')
            
            # Additional details
            if latest_recommendation.details:
                with ui.card().classes('w-full p-4'):
                    ui.label('Analysis Summary').classes('text-subtitle2 mb-2')
                    # Check if details look like markdown or need pre-formatting
                    if self._looks_like_markdown(latest_recommendation.details):
                        ui.markdown(latest_recommendation.details).classes('text-sm text-grey-8')
                    else:
                        with ui.element('pre').classes('whitespace-pre-wrap text-sm text-grey-8 font-mono bg-grey-50 p-2 rounded overflow-x-auto'):
                            ui.html(latest_recommendation.details, sanitize=False)  # Use html() to preserve formatting
            
            # Risk and Time Horizon if available
            with ui.row().classes('w-full gap-4 mt-3'):
                if latest_recommendation.risk_level:
                    # Get the risk level value and format it properly
                    risk_value = latest_recommendation.risk_level.value if hasattr(latest_recommendation.risk_level, 'value') else str(latest_recommendation.risk_level)
                    risk_icon = {'LOW': '🟢', 'MEDIUM': '🟡', 'HIGH': '🔴'}.get(risk_value, '⚪')
                    ui.label(f'{risk_icon} Risk: {risk_value.title()}').classes('text-sm')
                
                if latest_recommendation.time_horizon:
                    # Get the time horizon value and format it properly
                    horizon_value = latest_recommendation.time_horizon.value if hasattr(latest_recommendation.time_horizon, 'value') else str(latest_recommendation.time_horizon)
                    horizon_icon = {'SHORT_TERM': '⏱️', 'MEDIUM_TERM': '📅', 'LONG_TERM': '🗓️'}.get(horizon_value, '⏰')
                    horizon_text = horizon_value.replace('_', ' ').title()
                    ui.label(f'{horizon_icon} Horizon: {horizon_text}').classes('text-sm')
                
                if latest_recommendation.created_at:
                    ui.label(f'🕒 Generated: {latest_recommendation.created_at.strftime("%Y-%m-%d %H:%M:%S")}').classes('text-sm text-grey-7')
        
        except Exception as e:
            logger.error(f"Error rendering expert recommendation: {e}", exc_info=True)
            # Fail silently to not break the UI
    
    def _render_data_visualization_panel(self) -> None:
        """Render data visualization panel with price and indicator charts."""
        try:
            with ui.card().classes('w-full p-4'):
                ui.label('📉 Market Data & Technical Indicators').classes('text-h6 mb-4')
                ui.label('Interactive charts showing price action and technical indicators from analysis').classes('text-sm text-gray-600 mb-4')
                
                # Get expert instance to retrieve settings
                from ...core.db import get_instance
                from ...core.models import ExpertInstance
                from datetime import timedelta
                
                expert_instance = get_instance(ExpertInstance, self.market_analysis.expert_instance_id)
                if not expert_instance:
                    ui.label('Error: Expert instance not found').classes('text-red-500')
                    return
                
                # Get expert settings - use self.settings directly since we're already a TradingAgents instance
                # Get settings definitions for default values
                from ...modules.experts.TradingAgents import TradingAgents
                settings_def = TradingAgents.get_settings_definitions()
                
                # Create TradingAgents instance to get settings
                trading_agents = TradingAgents(expert_instance.id)
                
                # Extract key parameters directly from settings
                market_history_days = int(trading_agents.settings.get('market_history_days') or settings_def['market_history_days']['default'])
                timeframe = trading_agents.settings.get('timeframe') or settings_def['timeframe']['default']
                
                # Calculate date range - start from original analysis date, extend to current date
                from datetime import datetime
                recommendation_date = self.market_analysis.created_at  # Date when recommendation was made
                start_date = recommendation_date - timedelta(days=market_history_days)
                end_date = datetime.now()  # Extend to current date to see how prediction performed
                
                logger.info(f"Fetching data for visualization: {self.market_analysis.symbol}, "
                           f"{start_date.date()} to {end_date.date()} (recommendation: {recommendation_date.date()}), interval={timeframe}, "
                           f"lookback={market_history_days} days")
                
                # Get the correct OHLCV provider (was used during analysis, not hardcoded YFinance)
                provider, provider_info = self._get_ohlcv_data_provider()
                provider_name = provider.__class__.__name__
                provider_source = "Stored (from database)" if provider_info else "Settings (fallback)"
                
                logger.info(f"Using OHLCV provider: {provider_name} ({provider_source})")
                
                # Fetch price data using the correct provider
                try:
                    price_data = provider.get_ohlcv_data(
                        symbol=self.market_analysis.symbol,
                        start_date=start_date,
                        end_date=end_date,
                        interval=timeframe
                    )
                    
                    # Set Date as index for charting
                    if 'Date' in price_data.columns and not isinstance(price_data.index, pd.DatetimeIndex):
                        price_data['Date'] = pd.to_datetime(price_data['Date'])
                        price_data.set_index('Date', inplace=True)
                    
                    # Make price data timezone-aware to match indicator data (UTC)
                    if isinstance(price_data.index, pd.DatetimeIndex) and price_data.index.tz is None:
                        price_data.index = price_data.index.tz_localize('UTC')
                    
                    logger.info(f"Fetched price data for visualization: {len(price_data)} rows from {provider_name}, "
                               f"index tz={price_data.index.tz if isinstance(price_data.index, pd.DatetimeIndex) else 'N/A'}")
                    
                except Exception as e:
                    logger.error(f"Error fetching price data for visualization from {provider_name}: {e}", exc_info=True)
                    ui.label(f'Error fetching price data: {e}').classes('text-red-500')
                    return
                
                # Add toggle control for indicators
                ui.separator().classes('my-2')
                with ui.row().classes('w-full gap-4 items-center mb-4'):
                    use_stored_checkbox = ui.checkbox(
                        'Use Stored Indicators from Database',
                        value=True
                    ).classes('flex')
                    ui.label('(Uncheck to recalculate indicators live)').classes('text-xs text-gray-500')
                
                # Add event handler to refresh the panel when checkbox value changes
                def on_checkbox_change():
                    """Refresh the entire data visualization panel when checkbox changes."""
                    ui.notify('Refreshing indicators...', type='info')
                    # Trigger a page refresh to reload with new checkbox state
                    ui.run_javascript('window.location.reload()')
                
                use_stored_checkbox.on_value_change(lambda _: on_checkbox_change())
                
                # Get analysis outputs from database for indicators
                session = get_db()
                try:
                    statement = (
                        select(AnalysisOutput)
                        .where(AnalysisOutput.market_analysis_id == self.market_analysis.id)
                    )
                    outputs = session.exec(statement).all()
                    
                    # Look for indicator outputs
                    indicators_data = {}
                    indicators_source = "Database" if use_stored_checkbox.value else "Live Recalculation"
                    
                    # If using stored indicators, fetch from database
                    if use_stored_checkbox.value:
                        for output in outputs:
                            output_obj = output[0] if isinstance(output, tuple) else output
                            
                            # Look for technical indicators from both old and new sources
                            # Old format: tool_output_get_stockstats_indicators_*
                            # New format: tool_output_get_indicator_data (with _json variant)
                            is_indicator_output = (
                                'tool_output_get_indicator_data' in output_obj.name.lower() or
                                'tool_output_get_stockstats_indicators' in output_obj.name.lower()
                            )
                            
                            if is_indicator_output:
                                try:
                                    # Prefer JSON format if available
                                    if output_obj.name.endswith('_json') and output_obj.text:
                                        import json
                                        try:
                                            params = json.loads(output_obj.text)
                                        except json.JSONDecodeError:
                                            logger.warning(f"Could not parse JSON from {output_obj.name}, skipping")
                                            continue
                                        
                                        logger.debug(f"Processing indicator JSON output: {output_obj.name}")
                                        logger.debug(f"JSON keys: {params.keys() if isinstance(params, dict) else 'Not a dict'}")
                                        
                                        # Handle new format from get_indicator_data (get_indicator_data tool)
                                        if params.get('tool') == 'get_indicator_data':
                                            indicator_name = params.get('indicator', 'Unknown')
                                            
                                            # Clean up indicator name
                                            indicator_name = indicator_name.replace('_', ' ').title()
                                            
                                            # Get the data - could be dict or raw string
                                            indicator_data = params.get('data', {})
                                            
                                            if isinstance(indicator_data, dict):
                                                # If data is a dict, try to convert to DataFrame
                                                # Format: {"dates": [...], "values": [...]} or similar
                                                try:
                                                    if 'dates' in indicator_data and 'values' in indicator_data:
                                                        dates = pd.to_datetime(indicator_data['dates'])
                                                        indicator_df = pd.DataFrame({
                                                            indicator_name: indicator_data['values']
                                                        }, index=dates)
                                                        indicator_df.index.name = 'Date'
                                                        
                                                        # Make timezone-aware to match price data (UTC)
                                                        if indicator_df.index.tz is None:
                                                            indicator_df.index = indicator_df.index.tz_localize('UTC')
                                                        
                                                        indicators_data[indicator_name] = indicator_df
                                                        logger.info(f"Loaded indicator '{indicator_name}' from JSON: {len(indicator_df)} rows")
                                                    else:
                                                        logger.debug(f"Indicator data format not recognized for {indicator_name}, skipping")
                                                except Exception as e:
                                                    logger.warning(f"Could not convert indicator data to DataFrame: {e}")
                                            else:
                                                logger.debug(f"Indicator data is not a dict for {indicator_name}, might be raw data")
                                        
                                        # Handle old format from get_stock_stats_indicators
                                        elif params.get('tool') == 'get_stock_stats_indicators_window':
                                            indicator_name = params.get('indicator', 'Unknown')
                                            indicator_name = indicator_name.replace('_', ' ').title()
                                            logger.info(f"Reconstructing indicator '{indicator_name}' from old JSON format")
                                            
                                            # Use StockstatsUtils to recalculate indicator from cached price data
                                            from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.dataflows.stockstats_utils import StockstatsUtils
                                            
                                            try:
                                                indicator_df = StockstatsUtils.get_stock_stats_range(
                                                    symbol=params['symbol'],
                                                    indicator=params.get('indicator', ''),
                                                    start_date=params['start_date'],
                                                    end_date=params['end_date'],
                                                    data_dir='',  # Not used when online=True
                                                    online=True,  # Use data provider
                                                    interval=params.get('interval', '1d')
                                                )
                                                
                                                # Convert Date column to datetime index and match price data timezone
                                                if 'Date' in indicator_df.columns:
                                                    indicator_df['Date'] = pd.to_datetime(indicator_df['Date'])
                                                    # Make timezone-aware to match price data (UTC)
                                                    if indicator_df['Date'].dt.tz is None:
                                                        indicator_df['Date'] = indicator_df['Date'].dt.tz_localize('UTC')
                                                    indicator_df.set_index('Date', inplace=True)
                                                
                                                indicators_data[indicator_name] = indicator_df
                                                logger.info(f"Reconstructed indicator '{indicator_name}' from cache: {len(indicator_df)} rows")
                                            except Exception as e:
                                                logger.warning(f"Could not reconstruct indicator {indicator_name}: {e}")

                                except Exception as e:
                                    logger.error(f"Error parsing indicator data from {output_obj.name}: {e}", exc_info=True)
                    else:
                        # Recalculate indicators live if checkbox is unchecked
                        logger.info("Recalculating indicators live (checkbox unchecked)")
                        indicators_source = "Live Recalculation"
                        ui.label("📊 Recalculating indicators live...").classes('text-sm text-green-600 mb-2')
                        
                        # Extract indicator names from stored analysis outputs to know what to recalculate
                        indicators_to_calculate = set()
                        for output in outputs:
                            output_obj = output[0] if isinstance(output, tuple) else output
                            is_indicator_output = (
                                'tool_output_get_indicator_data' in output_obj.name.lower() or
                                'tool_output_get_stockstats_indicators' in output_obj.name.lower()
                            )
                            
                            if is_indicator_output and output_obj.name.endswith('_json') and output_obj.text:
                                try:
                                    import json
                                    params = json.loads(output_obj.text)
                                    if params.get('tool') == 'get_indicator_data':
                                        indicator_name = params.get('indicator', '')
                                        if indicator_name:
                                            indicators_to_calculate.add(indicator_name)
                                except json.JSONDecodeError:
                                    continue
                        
                        logger.info(f"Found {len(indicators_to_calculate)} indicators to recalculate: {list(indicators_to_calculate)}")
                        
                        # Recalculate each indicator using PandasIndicatorCalc
                        from ...modules.dataproviders.indicators.PandasIndicatorCalc import PandasIndicatorCalc
                        
                        calculator = PandasIndicatorCalc()
                        
                        for indicator_name in indicators_to_calculate:
                            try:
                                logger.info(f"Recalculating indicator '{indicator_name}' live")
                                indicator_result = calculator.get_indicator_data(
                                    symbol=self.market_analysis.symbol,
                                    indicator=indicator_name,
                                    start_date=start_date.strftime('%Y-%m-%d'),
                                    end_date=end_date.strftime('%Y-%m-%d'),
                                    interval=timeframe,
                                    format_type="dict"  # Get structured data only
                                )
                                
                                if isinstance(indicator_result, dict) and 'dates' in indicator_result and 'values' in indicator_result:
                                    # Convert to DataFrame format expected by chart
                                    dates = pd.to_datetime(indicator_result['dates'])
                                    display_name = indicator_name.replace('_', ' ').title()
                                    
                                    indicator_df = pd.DataFrame({
                                        display_name: indicator_result['values']
                                    }, index=dates)
                                    indicator_df.index.name = 'Date'
                                    
                                    # Make timezone-aware to match price data (UTC)
                                    if indicator_df.index.tz is None:
                                        indicator_df.index = indicator_df.index.tz_localize('UTC')
                                    
                                    indicators_data[display_name] = indicator_df
                                    logger.info(f"Successfully recalculated indicator '{display_name}': {len(indicator_df)} rows")
                                else:
                                    logger.warning(f"Invalid format returned for indicator '{indicator_name}': {type(indicator_result)}")
                                    
                            except Exception as e:
                                logger.error(f"Failed to recalculate indicator '{indicator_name}': {e}", exc_info=True)
                                ui.label(f"⚠️ Failed to recalculate {indicator_name}: {e}").classes('text-sm text-orange-600')

                    
                    # Render the graph if we have data
                    if price_data is not None and not price_data.empty:
                        ui.separator().classes('my-4')
                        
                        # Get recommendation action for chart marker
                        recommendation_action = None
                        session2 = get_db()
                        try:
                            statement = select(ExpertRecommendation).where(
                                ExpertRecommendation.market_analysis_id == self.market_analysis.id
                            ).order_by(ExpertRecommendation.created_at.desc())
                            recommendations = session2.exec(statement).all()
                            if recommendations:
                                recommendation_action = recommendations[0].recommended_action
                        finally:
                            session2.close()
                        
                        graph = InstrumentGraph(
                            symbol=self.market_analysis.symbol,
                            price_data=price_data,
                            indicators_data=indicators_data,
                            recommendation_date=recommendation_date,  # Show marker on chart
                            recommendation_action=recommendation_action  # BUY/SELL/HOLD for marker label
                        )
                        graph.render()
                    else:
                        ui.label('No price data available for visualization').classes('text-gray-500 text-center py-8')
                        ui.label(f'Unable to fetch price data for {self.market_analysis.symbol}').classes('text-sm text-gray-400 text-center')
                    
                    # Show data summary
                    if price_data is not None or indicators_data:
                        ui.separator().classes('my-4')
                        with ui.expansion('📊 Data Summary & Sources', icon='info').classes('w-full'):
                            ui.label('Data Retrieval Parameters:').classes('text-sm font-bold mb-2')
                            ui.label(f'  • Symbol: {self.market_analysis.symbol}').classes('text-xs text-gray-600')
                            ui.label(f'  • Date Range: {start_date.date()} to {end_date.date()}').classes('text-xs text-gray-600')
                            ui.label(f'  • 📊 Recommendation Date: {recommendation_date.date()} (marked on chart)').classes('text-xs text-amber-600 font-semibold')
                            ui.label(f'  • Lookback Period: {market_history_days} days before recommendation').classes('text-xs text-gray-600')
                            ui.label(f'  • Timeframe/Interval: {timeframe}').classes('text-xs text-gray-600')
                            
                            # Show data sources
                            ui.label('Data Sources:').classes('text-sm font-bold mt-3 mb-2')
                            ui.label(f'  📈 Price Data Provider: {provider_name} ({provider_source})').classes('text-xs text-blue-600 font-semibold')
                            ui.label(f'  📊 Indicator Source: {indicators_source}').classes('text-xs text-green-600 font-semibold')
                            
                            if price_data is not None:
                                ui.label(f'Price Data: {len(price_data)} data points').classes('text-sm font-bold mt-3')
                                ui.label(f'Columns: {", ".join(price_data.columns)}').classes('text-xs text-gray-600 mb-2')
                            
                            if indicators_data:
                                ui.label(f'Technical Indicators: {len(indicators_data)} indicators loaded').classes('text-sm font-bold mt-2')
                                for name, df in indicators_data.items():
                                    ui.label(f'  • {name}: {len(df)} data points, columns: {", ".join(df.columns)}').classes('text-xs text-gray-600')
                            
                            if provider_info:
                                ui.label('Stored Provider Metadata:').classes('text-sm font-bold mt-3 mb-2')
                                ui.label(f'  • Provider Module: {provider_info.get("provider_module", "N/A")}').classes('text-xs text-gray-600')
                                ui.label(f'  • Start Date: {provider_info.get("start_date", "N/A")}').classes('text-xs text-gray-600')
                                ui.label(f'  • End Date: {provider_info.get("end_date", "N/A")}').classes('text-xs text-gray-600')
                
                finally:
                    session.close()
                    
        except Exception as e:
            logger.error(f"Error rendering data visualization panel: {e}", exc_info=True)
            ui.label(f'Error loading visualization data: {e}').classes('text-red-500')
    
    def _render_tool_outputs_panel(self) -> None:
        """Render tool outputs panel with expandable sections and JSON viewer for JSON outputs."""
        try:
            with ui.card().classes('w-full p-4'):
                ui.label('🔧 Tool Outputs').classes('text-h6 mb-4')
                ui.label('All tool calls made during analysis with their outputs').classes('text-sm text-gray-600 mb-4')
                
                # Get analysis outputs from database
                session = get_db()
                try:
                    statement = (
                        select(AnalysisOutput)
                        .where(AnalysisOutput.market_analysis_id == self.market_analysis.id)
                        .order_by(AnalysisOutput.id)
                    )
                    outputs = session.exec(statement).all()
                    
                    if not outputs:
                        with ui.card().classes('w-full p-8 text-center bg-grey-1'):
                            ui.icon('build', size='3rem', color='grey-5').classes('mb-4')
                            ui.label('No tool outputs available').classes('text-h6 text-grey-7')
                            ui.label('Tool outputs will appear here once the analysis runs').classes('text-caption text-grey-6')
                        return
                    
                    # Group outputs by type/category
                    tool_outputs = []
                    for output in outputs:
                        output_obj = output[0] if isinstance(output, tuple) else output
                        
                        # Skip non-tool outputs (like analysis summaries)
                        if not output_obj.name or 'tool_output' not in output_obj.name.lower():
                            continue
                        
                        tool_outputs.append(output_obj)
                    
                    if not tool_outputs:
                        ui.label('No tool outputs found (only analysis summaries available)').classes('text-grey-7')
                        return
                    
                    # Display count
                    ui.label(f'Total Tool Calls: {len(tool_outputs)}').classes('text-sm font-bold mb-4')
                    
                    # Render each tool output in an expandable section
                    for idx, output_obj in enumerate(tool_outputs, 1):
                        # Determine icon based on tool type
                        tool_name = output_obj.name.lower()
                        if 'price' in tool_name or 'market' in tool_name:
                            icon = '📈'
                        elif 'news' in tool_name:
                            icon = '📰'
                        elif 'social' in tool_name or 'sentiment' in tool_name:
                            icon = '💬'
                        elif 'financial' in tool_name or 'fundamental' in tool_name:
                            icon = '🏛️'
                        elif 'indicator' in tool_name or 'technical' in tool_name:
                            icon = '📊'
                        elif 'macro' in tool_name or 'economic' in tool_name:
                            icon = '🌍'
                        else:
                            icon = '🔧'
                        
                        # Clean up tool name for display
                        display_name = output_obj.name.replace('tool_output_', '').replace('_', ' ').title()
                        
                        # Check if this is a JSON output
                        is_json_output = output_obj.name.endswith('_json')
                        
                        with ui.expansion(f'{icon} {display_name}', icon='code').classes('w-full mb-2'):
                            with ui.card().classes('w-full p-4 bg-grey-1'):
                                # Show metadata
                                with ui.row().classes('w-full gap-4 mb-3'):
                                    ui.label(f'Output #{idx}').classes('text-xs text-grey-7')
                                    ui.label(f'Type: {output_obj.type}').classes('text-xs text-grey-7')
                                    if output_obj.created_at:
                                        ui.label(f'Time: {output_obj.created_at.strftime("%H:%M:%S")}').classes('text-xs text-grey-7')
                                
                                ui.separator().classes('my-2')
                                
                                # Render content based on type
                                if is_json_output and output_obj.text:
                                    # JSON output - use json_editor for nice display
                                    try:
                                        json_data = json.loads(output_obj.text)
                                        ui.label('📋 Tool Parameters (JSON):').classes('text-sm font-bold mb-2')
                                        ui.json_editor({'content': {'json': json_data}}).classes('w-full')
                                    except json.JSONDecodeError as e:
                                        logger.warning(f"Failed to parse JSON for {output_obj.name}: {e}")
                                        # Fallback to text display with pre tag
                                        with ui.scroll_area().classes('w-full max-h-96'):
                                            escaped_text = html.escape(output_obj.text or '(empty)')
                                            ui.html(f'<pre class="whitespace-pre-wrap text-xs font-mono bg-white p-3 rounded border overflow-x-auto">{escaped_text}</pre>', sanitize=False)
                                
                                elif output_obj.text:
                                    # Text/Markdown output - show in scrollable pre
                                    with ui.scroll_area().classes('w-full max-h-96'):
                                        if self._looks_like_markdown(output_obj.text):
                                            ui.markdown(output_obj.text).classes('text-sm')
                                        else:
                                            # Use pre tag for preserving formatting and whitespace
                                            escaped_text = html.escape(output_obj.text)
                                            ui.html(f'<pre class="whitespace-pre-wrap text-xs font-mono bg-white p-3 rounded border overflow-x-auto">{escaped_text}</pre>', sanitize=False)
                                else:
                                    ui.label('(No output content)').classes('text-grey-5 italic')
                
                finally:
                    session.close()
                    
        except Exception as e:
            logger.error(f"Error rendering tool outputs panel: {e}", exc_info=True)
            with ui.card().classes('w-full p-4'):
                ui.label(f'Error loading tool outputs: {e}').classes('text-red-500')