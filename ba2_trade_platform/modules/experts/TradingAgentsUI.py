from nicegui import ui
from typing import Dict, Any, Optional
import json
from datetime import datetime

from ...core.models import MarketAnalysis
from ...core.types import MarketAnalysisStatus
from ...logger import logger


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
            return bool(content and content.strip())
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
                            ui.html(content)  # Use html() to preserve formatting
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
                        # Interleave messages based on chronological order
                        max_len = max(len(bull_messages), len(bear_messages))
                        for i in range(max_len):
                            if i < len(bull_messages):
                                all_messages.append(('Bull Analyst', bull_messages[i], 'blue'))
                            if i < len(bear_messages):
                                all_messages.append(('Bear Analyst', bear_messages[i], 'red'))
                
                elif state_key == 'risk_debate_state':
                    risky_messages = debate_state.get('risky_messages', [])
                    safe_messages = debate_state.get('safe_messages', [])
                    neutral_messages = debate_state.get('neutral_messages', [])
                    if risky_messages or safe_messages or neutral_messages:
                        has_individual_messages = True
                        # Interleave messages based on chronological order
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
                        ui.html(history)  # Use html() to preserve formatting
            
            # Current responses
            current_response = debate_state.get('current_response', '')
            if current_response:
                ui.label('💬 Current Response').classes('text-h6 mb-2')
                if self._looks_like_markdown(current_response):
                    ui.markdown(current_response).classes('w-full mb-4')
                else:
                    with ui.element('pre').classes('whitespace-pre-wrap text-sm p-4 bg-grey-1 rounded mb-4 font-mono overflow-x-auto'):
                        ui.html(current_response)  # Use html() to preserve formatting
            
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
                            ui.html(risk_response)  # Use html() to preserve formatting
                
                if safe_response:
                    ui.label('🛡️ Conservative Assessment').classes('text-h6 mb-2')
                    if self._looks_like_markdown(safe_response):
                        ui.markdown(safe_response).classes('w-full mb-4')
                    else:
                        with ui.element('pre').classes('whitespace-pre-wrap text-sm p-4 bg-green-1 rounded mb-4 font-mono overflow-x-auto'):
                            ui.html(safe_response)  # Use html() to preserve formatting
                
                if neutral_response:
                    ui.label('⚖️ Balanced Assessment').classes('text-h6 mb-2')
                    if self._looks_like_markdown(neutral_response):
                        ui.markdown(neutral_response).classes('w-full mb-4')
                    else:
                        with ui.element('pre').classes('whitespace-pre-wrap text-sm p-4 bg-blue-1 rounded mb-4 font-mono overflow-x-auto'):
                            ui.html(neutral_response)  # Use html() to preserve formatting
            
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
                        ui.html(str(error_info))  # Use html() to preserve formatting
    
    def _render_error_ui(self, error_message: str) -> None:
        """Render UI for rendering errors."""
        with ui.card().classes('w-full p-8 text-center'):
            ui.icon('warning', size='4rem', color='warning').classes('mb-4')
            ui.label('Rendering Error').classes('text-h4 text-warning mb-2')
            ui.label('An error occurred while rendering the analysis results.').classes('text-grey-7 mb-4')
            
            with ui.element('pre').classes('bg-orange-50 p-3 rounded text-sm overflow-auto max-h-32 whitespace-pre-wrap font-mono text-orange-900 border'):
                ui.html(error_message)  # Use html() to preserve formatting
    
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
            # Load expert recommendations using a proper database session
            from ...core.db import get_db
            from ...core.models import ExpertRecommendation
            from sqlmodel import select
            
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
                    ui.label(f'{action_icon} {latest_recommendation.recommended_action}').classes(f'text-xl font-bold {action_color}')
                
                # Confidence card
                with ui.card().classes('p-4 flex-1'):
                    ui.label('Confidence').classes('text-subtitle2 mb-2')
                    confidence = (latest_recommendation.confidence or 0) * 100
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
                            ui.html(latest_recommendation.details)  # Use html() to preserve formatting
            
            # Risk and Time Horizon if available
            with ui.row().classes('w-full gap-4 mt-3'):
                if latest_recommendation.risk_level:
                    risk_icon = {'LOW': '🟢', 'MEDIUM': '🟡', 'HIGH': '🔴'}.get(latest_recommendation.risk_level, '⚪')
                    ui.label(f'{risk_icon} Risk: {latest_recommendation.risk_level}').classes('text-sm')
                
                if latest_recommendation.time_horizon:
                    horizon_icon = {'SHORT_TERM': '⏱️', 'MEDIUM_TERM': '📅', 'LONG_TERM': '🗓️'}.get(latest_recommendation.time_horizon, '⏰')
                    horizon_text = latest_recommendation.time_horizon.replace('_', ' ').title()
                    ui.label(f'{horizon_icon} Horizon: {horizon_text}').classes('text-sm')
                
                if latest_recommendation.created_at:
                    ui.label(f'🕒 Generated: {latest_recommendation.created_at.strftime("%Y-%m-%d %H:%M:%S")}').classes('text-sm text-grey-7')
        
        except Exception as e:
            logger.error(f"Error rendering expert recommendation: {e}")
            # Fail silently to not break the UI