"""
Smart Risk Manager Content Component

Shared rendering logic for Smart Risk Manager job details.
Used by both the detail dialog and the detail page to avoid code duplication.
"""

from nicegui import ui
from typing import Optional
import json
from datetime import datetime, timezone

from ...core.db import get_instance
from ...core.models import SmartRiskManagerJob, ExpertInstance, AccountDefinition, Transaction
from ...logger import logger


def format_duration(seconds: Optional[int]) -> str:
    """Format duration in seconds to human-readable string."""
    if seconds is None:
        return "N/A"
    
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"


def get_status_color(status: str) -> str:
    """Get color class for status badge."""
    if status == "COMPLETED":
        return "positive"
    elif status == "RUNNING":
        return "warning"
    elif status == "FAILED":
        return "negative"
    else:
        return "grey"


class SmartRiskManagerContentRenderer:
    """
    Renders Smart Risk Manager job details content.
    
    This class provides the shared rendering logic used by both
    SmartRiskManagerDetailDialog and the smart_risk_manager_detail page.
    
    Args:
        use_explicit_dark_colors: If True, uses explicit color styles for dark theme.
                                  If False, uses Quasar semantic classes.
    """
    
    def __init__(self, use_explicit_dark_colors: bool = False):
        self.use_dark = use_explicit_dark_colors
    
    def _text_style(self, color_type: str = "primary") -> str:
        """Get text style or class based on theme mode."""
        if self.use_dark:
            colors = {
                "primary": "color: #e2e8f0;",
                "secondary": "color: #a0aec0;",
                "positive": "color: #00d4aa;",
                "negative": "color: #ff6b6b;",
            }
            return colors.get(color_type, "color: #e2e8f0;")
        return ""
    
    def _text_class(self, color_type: str = "primary") -> str:
        """Get text class based on theme mode."""
        if not self.use_dark:
            classes = {
                "secondary": "text-grey-7",
                "positive": "text-positive",
                "negative": "text-negative",
            }
            return classes.get(color_type, "")
        return ""
    
    def _card_style(self) -> str:
        """Get card background style for dark mode."""
        if self.use_dark:
            return "background-color: #1e2a3a;"
        return ""
    
    def _error_card_style(self) -> str:
        """Get error card style."""
        if self.use_dark:
            return "background-color: rgba(255, 107, 107, 0.1);"
        return ""
    
    def render(self, job: SmartRiskManagerJob) -> None:
        """
        Render the full Smart Risk Manager job details.
        
        Args:
            job: The SmartRiskManagerJob instance to render
        """
        # Load the expert instance
        expert_instance = get_instance(ExpertInstance, job.expert_instance_id)
        expert_name = expert_instance.expert if expert_instance else "Unknown"
        if expert_instance and expert_instance.alias:
            expert_name = expert_instance.alias
        
        # Load the account
        account = get_instance(AccountDefinition, job.account_id)
        account_name = account.name if account else "Unknown"
        
        # Job metadata card
        self._render_job_info_card(job, expert_name, account_name)
        
        # Portfolio snapshot card
        self._render_portfolio_snapshot(job)
        
        # User instructions
        self._render_user_instructions(job)
        
        # Actions summary
        self._render_actions_summary(job)
        
        # Detailed actions log
        self._render_actions_log(job)
        
        # Research findings and final summary
        self._render_research_and_summary(job)
        
        # Graph state viewer
        self._render_graph_state(job)
        
        # Error message
        self._render_error_message(job)
    
    def _render_job_info_card(self, job: SmartRiskManagerJob, expert_name: str, account_name: str) -> None:
        """Render the job information card."""
        with ui.card().classes('w-full mb-4').style(self._card_style()):
            ui.label('Job Information').classes(f'text-h6 mb-2 {self._text_class("primary")}').style(self._text_style("primary"))
            ui.separator()
            
            with ui.grid(columns=2).classes('w-full gap-4 mt-2'):
                # Left column
                with ui.column().classes('gap-2'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('event').classes(self._text_class("secondary")).style(self._text_style("secondary"))
                        # Convert UTC to local time for display
                        if job.run_date:
                            if job.run_date.tzinfo is None:
                                utc_time = job.run_date.replace(tzinfo=timezone.utc)
                            else:
                                utc_time = job.run_date
                            local_time = utc_time.astimezone()
                            run_date_str = local_time.strftime('%Y-%m-%d %H:%M:%S')
                        else:
                            run_date_str = 'N/A'
                        ui.label(f"Run Date: {run_date_str}").classes(f'text-sm {self._text_class("primary")}').style(self._text_style("primary"))
                    
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('schedule').classes(self._text_class("secondary")).style(self._text_style("secondary"))
                        ui.label(f"Duration: {format_duration(job.duration_seconds)}").classes(f'text-sm {self._text_class("primary")}').style(self._text_style("primary"))
                    
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('psychology').classes(self._text_class("secondary")).style(self._text_style("secondary"))
                        ui.label(f"Model: {job.model_used or 'N/A'}").classes(f'text-sm {self._text_class("primary")}').style(self._text_style("primary"))
                
                # Right column
                with ui.column().classes('gap-2'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('loop').classes(self._text_class("secondary")).style(self._text_style("secondary"))
                        ui.label(f"Iterations: {job.iteration_count or 0}").classes(f'text-sm {self._text_class("primary")}').style(self._text_style("primary"))
                    
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('trending_up').classes(self._text_class("secondary")).style(self._text_style("secondary"))
                        actions_text = self._format_actions_taken(job)
                        ui.label(actions_text).classes(f'text-sm {self._text_class("primary")}').style(self._text_style("primary"))
    
    def _format_actions_taken(self, job: SmartRiskManagerJob) -> str:
        """Format actions taken count with failed count if any."""
        total = job.actions_taken_count or 0
        
        # Count failed actions from graph_state if available
        failed = 0
        if job.graph_state and "actions_log" in job.graph_state:
            actions_log = job.graph_state["actions_log"]
            failed = sum(1 for action in actions_log if not action.get("success", False))
        
        if failed > 0:
            return f"Actions Taken: {total} ({failed} Failed)"
        else:
            return f"Actions Taken: {total}"
    
    def _render_portfolio_snapshot(self, job: SmartRiskManagerJob) -> None:
        """Render the portfolio snapshot card."""
        if job.initial_portfolio_equity is None and job.final_portfolio_equity is None and job.initial_available_balance is None:
            return
        
        with ui.card().classes('w-full mb-4').style(self._card_style()):
            ui.label('Portfolio Snapshot').classes(f'text-h6 mb-2 {self._text_class("primary")}').style(self._text_style("primary"))
            ui.separator()
            
            # Initial Equity at Run
            with ui.row().classes('w-full gap-4 mt-2 mb-3'):
                with ui.column().classes('text-center flex-1'):
                    ui.label('Initial Portfolio Equity at Run').classes(f'text-sm {self._text_class("secondary")}').style(self._text_style("secondary"))
                    equity_text = f"${job.initial_portfolio_equity:,.2f}" if job.initial_portfolio_equity else "N/A"
                    ui.label(equity_text).classes(f'text-h6 {self._text_class("primary")}').style(self._text_style("primary"))
            
            ui.separator()
            
            # Available Balance Before/After
            with ui.grid(columns=3).classes('w-full gap-4 mt-3'):
                # Initial balance
                with ui.column().classes('text-center'):
                    ui.label('Available Balance Before').classes(f'text-sm {self._text_class("secondary")}').style(self._text_style("secondary"))
                    balance_before = f"${job.initial_available_balance:,.2f}" if job.initial_available_balance else "N/A"
                    ui.label(balance_before).classes(f'text-h6 {self._text_class("primary")}').style(self._text_style("primary"))
                
                # Final balance
                with ui.column().classes('text-center'):
                    ui.label('Available Balance After').classes(f'text-sm {self._text_class("secondary")}').style(self._text_style("secondary"))
                    balance_after = f"${job.final_available_balance:,.2f}" if job.final_available_balance else "N/A"
                    ui.label(balance_after).classes(f'text-h6 {self._text_class("primary")}').style(self._text_style("primary"))
                
                # Change
                with ui.column().classes('text-center'):
                    ui.label('Balance Change').classes(f'text-sm {self._text_class("secondary")}').style(self._text_style("secondary"))
                    if job.initial_available_balance is not None and job.final_available_balance is not None:
                        change = job.final_available_balance - job.initial_available_balance
                        change_pct = (change / job.initial_available_balance * 100) if job.initial_available_balance > 0 else 0
                        sign = '+' if change >= 0 else ''
                        if self.use_dark:
                            color = '#00d4aa' if change >= 0 else '#ff6b6b'
                            ui.label(f"{sign}${change:,.2f} ({sign}{change_pct:.2f}%)").classes('text-h6').style(f'color: {color};')
                        else:
                            color_class = 'positive' if change >= 0 else 'negative'
                            ui.label(f"{sign}${change:,.2f} ({sign}{change_pct:.2f}%)").classes(f'text-h6 text-{color_class}')
                    else:
                        ui.label("N/A").classes(f'text-h6 {self._text_class("primary")}').style(self._text_style("primary"))
    
    def _render_user_instructions(self, job: SmartRiskManagerJob) -> None:
        """Render user instructions card."""
        if not job.user_instructions:
            return
        
        with ui.card().classes('w-full mb-4').style(self._card_style()):
            ui.label('User Instructions').classes(f'text-h6 mb-2 {self._text_class("primary")}').style(self._text_style("primary"))
            ui.separator()
            ui.label(job.user_instructions).classes(f'text-sm whitespace-pre-wrap mt-2 {self._text_class("primary")}').style(self._text_style("primary"))
    
    def _render_actions_summary(self, job: SmartRiskManagerJob) -> None:
        """Render actions summary card."""
        if not job.actions_summary:
            return
        
        with ui.card().classes('w-full mb-4').style(self._card_style()):
            ui.label('Actions Summary').classes(f'text-h6 mb-2 {self._text_class("primary")}').style(self._text_style("primary"))
            ui.separator()
            ui.label(job.actions_summary).classes(f'text-sm whitespace-pre-wrap mt-2 {self._text_class("primary")}').style(self._text_style("primary"))
    
    def _render_actions_log(self, job: SmartRiskManagerJob) -> None:
        """Render detailed actions log."""
        if not job.graph_state:
            return
        
        try:
            state = json.loads(job.graph_state) if isinstance(job.graph_state, str) else job.graph_state
            actions_log = state.get("actions_log", [])
            
            if not actions_log:
                return
            
            with ui.card().classes('w-full mb-4').style(self._card_style()):
                ui.label(f'Detailed Actions Log ({len(actions_log)} actions)').classes(f'text-h6 mb-2 {self._text_class("primary")}').style(self._text_style("primary"))
                ui.separator()
                
                # Display each action with full details
                for i, action in enumerate(actions_log):
                    self._render_single_action(i, action)
                
        except (json.JSONDecodeError, TypeError, AttributeError) as e:
            logger.debug(f"Could not extract actions_log from graph_state: {e}")
    
    def _render_single_action(self, index: int, action: dict) -> None:
        """Render a single action from the actions log."""
        # Extract symbol from arguments or result
        symbol = None
        if action.get('arguments'):
            symbol = action['arguments'].get('symbol')
        if not symbol and action.get('result'):
            symbol = action['result'].get('symbol')
        
        # If still no symbol, try to look it up from transaction_id
        if not symbol:
            transaction_id = None
            if action.get('arguments'):
                transaction_id = action['arguments'].get('transaction_id')
            if not transaction_id and action.get('result'):
                transaction_id = action['result'].get('transaction_id')
            
            if transaction_id:
                try:
                    transaction = get_instance(Transaction, transaction_id)
                    if transaction:
                        symbol = transaction.symbol
                except Exception:
                    pass
        
        action_name = action.get('action_type') or action.get('action', 'Unknown')
        # Handle both 'success' boolean and 'status' string formats
        if 'success' in action:
            success = action['success']
            status = 'success' if success else 'error'
        else:
            status = action.get('status', 'unknown')
            success = status == 'success'
        
        # Build expansion title
        title_parts = [f"Action {index+1}: {action_name}"]
        if symbol:
            title_parts[0] += f' ({symbol})'
        
        expansion_icon = 'check_circle' if success else 'error' if status == 'error' else 'play_arrow'
        
        with ui.expansion(title_parts[0], icon=expansion_icon).classes('w-full mb-2').style(self._text_style("primary")):
            with ui.column().classes('gap-2 mt-2'):
                # Status
                with ui.row().classes('items-center gap-2'):
                    ui.label('Status:').classes(f'text-sm font-bold {self._text_class("secondary")}').style(self._text_style("secondary"))
                    ui.badge(status, color='positive' if status == 'success' else 'negative' if status == 'error' else 'warning')
                
                # Timestamp and iteration
                with ui.row().classes('gap-4'):
                    if action.get('iteration') is not None:
                        ui.label(f"Iteration: {action.get('iteration')}").classes(f'text-sm {self._text_class("secondary")}').style(self._text_style("secondary"))
                    if action.get('timestamp'):
                        try:
                            ts = datetime.fromisoformat(action['timestamp'].replace('Z', '+00:00'))
                            local_ts = ts.astimezone()
                            ui.label(f"Time: {local_ts.strftime('%Y-%m-%d %H:%M:%S')}").classes(f'text-sm {self._text_class("secondary")}').style(self._text_style("secondary"))
                        except Exception:
                            ui.label(f"Time: {action.get('timestamp')}").classes(f'text-sm {self._text_class("secondary")}').style(self._text_style("secondary"))
                
                # Confidence and source
                with ui.row().classes('gap-4'):
                    if action.get('confidence') is not None:
                        conf = action['confidence']
                        ui.label(f"Confidence: {conf:.1f}%").classes(f'text-sm {self._text_class("secondary")}').style(self._text_style("secondary"))
                    if action.get('source'):
                        ui.label(f"Source: {action['source']}").classes(f'text-sm {self._text_class("secondary")}').style(self._text_style("secondary"))
                
                # Summary (if available)
                if action.get('summary'):
                    ui.label('Summary:').classes(f'text-sm font-bold mt-2 {self._text_class("secondary")}').style(self._text_style("secondary"))
                    ui.label(action['summary']).classes(f'text-sm whitespace-pre-wrap {self._text_class("primary")}').style(self._text_style("primary"))
                
                # Reasoning (support both 'reason' and 'reasoning' keys)
                reasoning = action.get('reason') or action.get('reasoning')
                if reasoning:
                    ui.label('Reasoning:').classes(f'text-sm font-bold mt-2 {self._text_class("secondary")}').style(self._text_style("secondary"))
                    ui.label(reasoning).classes(f'text-sm whitespace-pre-wrap {self._text_class("primary")}').style(self._text_style("primary"))
                
                # Arguments
                if action.get('arguments'):
                    ui.label('Arguments:').classes(f'text-sm font-bold mt-2 {self._text_class("secondary")}').style(self._text_style("secondary"))
                    with ui.column().classes('gap-1 mt-1'):
                        for key, value in action['arguments'].items():
                            ui.label(f"  • {key}: {value}").classes(f'text-sm {self._text_class("primary")}').style(self._text_style("primary"))
                
                # Result
                if action.get('result'):
                    self._render_action_result(action['result'])
                
                # Error (if present)
                if action.get('error'):
                    ui.label('Error:').classes(f'text-sm font-bold mt-2 {self._text_class("negative")}').style(self._text_style("negative"))
                    ui.label(action['error']).classes(f'text-sm whitespace-pre-wrap {self._text_class("negative")}').style(self._text_style("negative"))
    
    def _render_action_result(self, result: dict) -> None:
        """Render action result details."""
        ui.label('Result:').classes(f'text-sm font-bold mt-2 {self._text_class("secondary")}').style(self._text_style("secondary"))
        
        # Success status with color
        if 'success' in result:
            success = result.get('success', False)
            if self.use_dark:
                color = '#00d4aa' if success else '#ff6b6b'
                ui.label(f"Status: {'Success' if success else 'Failed'}").classes('text-sm').style(f'color: {color};')
            else:
                status_color = 'positive' if success else 'negative'
                ui.label(f"Status: {'Success' if success else 'Failed'}").classes(f'text-sm text-{status_color}')
        
        # Message
        if result.get('message'):
            ui.label(f"Message: {result['message']}").classes(f'text-sm whitespace-pre-wrap {self._text_class("primary")}').style(self._text_style("primary"))
        
        # Show relevant result fields
        result_fields = [
            ('symbol', 'Symbol', None),
            ('quantity', 'Quantity', None),
            ('direction', 'Direction', None),
            ('entry_price', 'Entry Price', '${:.2f}'),
            ('tp_price', 'Take Profit', '${:.2f}'),
            ('sl_price', 'Stop Loss', '${:.2f}'),
            ('transaction_id', 'Transaction ID', None),
            ('order_id', 'Order ID', None),
            ('old_quantity', 'Old Quantity', None),
            ('new_quantity', 'New Quantity', None),
            ('old_sl_price', 'Old Stop Loss', '${:.2f}'),
            ('new_sl_price', 'New Stop Loss', '${:.2f}'),
            ('old_tp_price', 'Old Take Profit', '${:.2f}'),
            ('new_tp_price', 'New Take Profit', '${:.2f}'),
        ]
        
        with ui.column().classes('gap-1 mt-1'):
            for key, label, fmt in result_fields:
                if result.get(key) is not None:
                    value = result.get(key)
                    if fmt:
                        display_value = fmt.format(value)
                    else:
                        display_value = str(value)
                    ui.label(f"  • {label}: {display_value}").classes(f'text-sm {self._text_class("primary")}').style(self._text_style("primary"))
    
    def _render_research_and_summary(self, job: SmartRiskManagerJob) -> None:
        """Render research findings and final summary cards."""
        if not job.graph_state:
            return
        
        research_findings = None
        final_summary = None
        
        try:
            state = json.loads(job.graph_state) if isinstance(job.graph_state, str) else job.graph_state
            research_findings = state.get("research_findings")
            final_summary = state.get("final_summary")
        except (json.JSONDecodeError, TypeError, AttributeError):
            return
        
        # Research Node Analysis card
        if research_findings:
            with ui.card().classes('w-full mb-4').style(self._card_style()):
                with ui.row().classes('items-center gap-2 mb-2'):
                    ui.icon('psychology', color='primary').props('size=md')
                    ui.label('Research Node Analysis').classes(f'text-h6 {self._text_class("primary")}').style(self._text_style("primary"))
                ui.separator()
                ui.label(research_findings).classes(f'text-sm whitespace-pre-wrap mt-2 {self._text_class("primary")}').style(self._text_style("primary"))
        
        # Final Summary card
        if final_summary:
            with ui.card().classes('w-full mb-4').style(self._card_style()):
                with ui.row().classes('items-center gap-2 mb-2'):
                    ui.icon('summarize', color='primary').props('size=md')
                    ui.label('Final Summary').classes(f'text-h6 {self._text_class("primary")}').style(self._text_style("primary"))
                ui.separator()
                ui.label(final_summary).classes(f'text-sm whitespace-pre-wrap mt-2 {self._text_class("primary")}').style(self._text_style("primary"))
    
    def _render_graph_state(self, job: SmartRiskManagerJob) -> None:
        """Render graph state JSON viewer."""
        if not job.graph_state:
            return
        
        with ui.expansion('Graph State (Technical Details)', icon='account_tree').classes('w-full mb-4').style(self._text_style("primary")):
            try:
                # Try to parse as JSON
                state = json.loads(job.graph_state) if isinstance(job.graph_state, str) else job.graph_state
                ui.json_editor({'content': {'json': state}}).classes('w-full')
            except (json.JSONDecodeError, TypeError):
                # Display as plain text if not valid JSON
                ui.label(str(job.graph_state)).classes(f'text-sm whitespace-pre-wrap {self._text_class("primary")}').style(self._text_style("primary"))
    
    def _render_error_message(self, job: SmartRiskManagerJob) -> None:
        """Render error message card for failed jobs."""
        if job.status != "FAILED" or not job.error_message:
            return
        
        with ui.card().classes('w-full mb-4').style(self._error_card_style()):
            with ui.row().classes('items-center gap-2'):
                ui.icon('error', color='negative').props('size=md')
                ui.label('Error Details').classes(f'text-h6 {self._text_class("negative")}').style(self._text_style("negative"))
            ui.separator()
            ui.label(job.error_message).classes(f'text-sm whitespace-pre-wrap mt-2 {self._text_class("negative")}').style(self._text_style("negative"))


def render_smart_risk_manager_content(job: SmartRiskManagerJob, use_explicit_dark_colors: bool = False) -> None:
    """
    Render Smart Risk Manager job details.
    
    This is the main entry point for rendering SRM job content.
    
    Args:
        job: The SmartRiskManagerJob instance to render
        use_explicit_dark_colors: If True, uses explicit color styles for dark theme dialogs.
                                  If False, uses Quasar semantic classes.
    """
    renderer = SmartRiskManagerContentRenderer(use_explicit_dark_colors=use_explicit_dark_colors)
    renderer.render(job)
