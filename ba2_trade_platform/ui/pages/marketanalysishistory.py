"""
Market Analysis History Page
Displays historical price action with overlaid expert recommendations for a specific symbol.
"""
from nicegui import ui
from typing import Dict, List, Any, Optional
import pandas as pd
from datetime import datetime, timedelta, timezone
from sqlmodel import Session, select, and_, or_
from ...logger import logger
from ...core.db import get_db
from ...core.models import MarketAnalysis, ExpertRecommendation, ExpertInstance, AccountDefinition
from ...core.types import OrderRecommendation
from ...ui.components import InstrumentGraph


class MarketAnalysisHistoryPage:
    """
    Page for displaying market analysis history with price action and recommendations.
    """
    
    def __init__(self, symbol: str):
        """
        Initialize the history page for a specific symbol.
        
        Args:
            symbol: The instrument symbol to display history for (e.g., "AAPL")
        """
        self.symbol = symbol.upper()
        
        # Data containers
        self.price_data = None
        self.recommendations = []
        self.experts = {}  # Map expert_id -> expert info
        
        # UI references
        self.graph_container = None
        self.expert_filters = {}  # Map expert_id -> checkbox UI element
        self.visible_experts = {}  # Map expert_id -> bool
        
    def render(self) -> None:
        """Render the complete market analysis history page."""
        try:
            # Page header
            with ui.row().classes('w-full items-center justify-between mb-4'):
                ui.label(f'📊 Market Analysis History - {self.symbol}').classes('text-h4')
                ui.button('← Back', on_click=lambda: ui.navigate.back()).props('flat')
            
            # Load data
            self._load_data()
            
            # Expert filter controls (in a container that doesn't constrain width)
            with ui.column().classes('w-full'):
                self._render_expert_filters()
            
            # Price chart with recommendations - ensure full width container
            with ui.column().classes('w-full mt-4'):
                self.graph_container = ui.column().classes('w-full')
                self._render_chart()
            
            # Recommendations table
            with ui.column().classes('w-full mt-4'):
                self._render_recommendations_table()
            
        except Exception as e:
            logger.error(f"Error rendering market analysis history page: {e}", exc_info=True)
            ui.label(f'Error loading market analysis history: {e}').classes('text-red-500')
    
    def _load_data(self) -> None:
        """Load price data and recommendations from database."""
        try:
            # Load recommendations for this symbol
            session = get_db()
            
            # Get all expert recommendations for this symbol
            statement = (
                select(ExpertRecommendation, ExpertInstance, AccountDefinition)
                .join(ExpertInstance, ExpertRecommendation.instance_id == ExpertInstance.id)
                .join(AccountDefinition, ExpertInstance.account_id == AccountDefinition.id)
                .where(ExpertRecommendation.symbol == self.symbol)
                .order_by(ExpertRecommendation.created_at.desc())
            )
            results = session.exec(statement).all()
            
            # Process recommendations
            self.recommendations = []
            for rec, expert_instance, account in results:
                # Get expert name
                expert_name = f"{expert_instance.alias or expert_instance.expert}-{expert_instance.id}"
                
                # Add to experts dict if not present
                if expert_instance.id not in self.experts:
                    self.experts[expert_instance.id] = {
                        'id': expert_instance.id,
                        'name': expert_name,
                        'expert_type': expert_instance.expert,
                        'alias': expert_instance.alias,
                        'account_name': account.name
                    }
                    # Initialize as visible by default
                    self.visible_experts[expert_instance.id] = True
                
                # Add recommendation
                self.recommendations.append({
                    'id': rec.id,
                    'expert_id': expert_instance.id,
                    'expert_name': expert_name,
                    'date': rec.created_at,
                    'action': rec.recommended_action,
                    'confidence': rec.confidence,
                    'time_horizon': rec.time_horizon,
                    'expected_profit': rec.expected_profit_percent,
                    'price_at_date': rec.price_at_date
                })
            
            logger.info(f"Loaded {len(self.recommendations)} recommendations for {self.symbol} "
                       f"from {len(self.experts)} experts")
            
            # Load price data
            self._load_price_data()
            
            session.close()
            
        except Exception as e:
            logger.error(f"Error loading data for {self.symbol}: {e}", exc_info=True)
            raise
    
    def _load_price_data(self) -> None:
        """Load price data for the symbol covering the analysis period."""
        try:
            # Determine date range
            if self.recommendations:
                # Get earliest and latest recommendation dates
                dates = [rec['date'] for rec in self.recommendations if rec['date']]
                if dates:
                    earliest_date = min(dates)
                    latest_date = max(dates)
                    
                    # Expand range by 3 months before earliest and 1 month after latest
                    start_date = earliest_date - timedelta(days=90)
                    end_date = latest_date + timedelta(days=30)
                else:
                    # Default to last 3 months
                    end_date = datetime.now(timezone.utc)
                    start_date = end_date - timedelta(days=90)
            else:
                # Default to last 3 months
                end_date = datetime.now(timezone.utc)
                start_date = end_date - timedelta(days=90)
            
            logger.info(f"Loading price data for {self.symbol} from {start_date.date()} to {end_date.date()}")
            
            # Get price data from account (using any expert's account)
            # Try to get from first expert's account
            if self.experts:
                first_expert_id = list(self.experts.keys())[0]
                expert_instance = get_db().exec(
                    select(ExpertInstance).where(ExpertInstance.id == first_expert_id)
                ).first()
                
                if expert_instance:
                    from ...core.utils import get_account_instance_from_id
                    account = get_account_instance_from_id(expert_instance.account_id)
                    
                    if account:
                        # Get historical data from account
                        # Note: Account interface should provide get_historical_data method
                        # For now, we'll use yfinance as fallback
                        self._load_price_data_yfinance(start_date, end_date)
                    else:
                        logger.warning(f"Could not get account instance for expert {first_expert_id}")
                        self._load_price_data_yfinance(start_date, end_date)
                else:
                    logger.warning(f"Could not find expert instance {first_expert_id}")
                    self._load_price_data_yfinance(start_date, end_date)
            else:
                # No experts, use yfinance
                self._load_price_data_yfinance(start_date, end_date)
            
        except Exception as e:
            logger.error(f"Error loading price data: {e}", exc_info=True)
            # Create empty DataFrame
            self.price_data = pd.DataFrame()
    
    def _load_price_data_yfinance(self, start_date: datetime, end_date: datetime) -> None:
        """Load price data using yfinance as fallback."""
        try:
            import yfinance as yf
            
            # Download data
            ticker = yf.Ticker(self.symbol)
            df = ticker.history(
                start=start_date.strftime('%Y-%m-%d'),
                end=end_date.strftime('%Y-%m-%d'),
                interval='1d'
            )
            
            if df.empty:
                logger.warning(f"No price data found for {self.symbol}")
                self.price_data = pd.DataFrame()
                return
            
            # Ensure required columns exist
            required_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
            for col in required_cols:
                if col not in df.columns:
                    logger.error(f"Missing required column: {col}")
                    self.price_data = pd.DataFrame()
                    return
            
            self.price_data = df[required_cols]
            logger.info(f"Loaded {len(self.price_data)} days of price data for {self.symbol}")
            
        except ImportError:
            logger.error("yfinance not installed, cannot load price data")
            self.price_data = pd.DataFrame()
        except Exception as e:
            logger.error(f"Error loading price data with yfinance: {e}", exc_info=True)
            self.price_data = pd.DataFrame()
    
    def _render_expert_filters(self) -> None:
        """Render expert filter checkboxes."""
        if not self.experts:
            return
        
        with ui.card().classes('w-full p-4 mb-4'):
            ui.label('Filter Recommendations by Expert:').classes('text-h6 mb-2')
            
            with ui.row().classes('gap-4 flex-wrap'):
                for expert_id, expert_info in self.experts.items():
                    checkbox = ui.checkbox(
                        expert_info['name'],
                        value=self.visible_experts.get(expert_id, True)
                    ).on_value_change(lambda e, eid=expert_id: self._toggle_expert(eid, e.value))
                    
                    self.expert_filters[expert_id] = checkbox
    
    def _toggle_expert(self, expert_id: int, is_visible: bool) -> None:
        """Toggle visibility of expert's recommendations."""
        self.visible_experts[expert_id] = is_visible
        logger.debug(f"Toggled expert {expert_id} to {is_visible}")
        
        # Re-render chart with updated filters
        self._render_chart()
    
    def _render_chart(self) -> None:
        """Render the price chart with recommendation markers."""
        try:
            if self.graph_container:
                self.graph_container.clear()
            
            with self.graph_container:
                if self.price_data is None or self.price_data.empty:
                    ui.label('No price data available for this symbol').classes('text-gray-500 text-center py-8')
                    return
                
                # Create chart with multiple recommendation markers
                # For now, we'll create a simple chart without overlaying multiple recommendations
                # We'll enhance this to show all visible recommendations
                self._render_chart_with_recommendations()
                
        except Exception as e:
            logger.error(f"Error rendering chart: {e}", exc_info=True)
            with self.graph_container:
                ui.label(f'Error creating chart: {e}').classes('text-red-500')
    
    def _render_chart_with_recommendations(self) -> None:
        """Render chart with recommendation markers overlaid."""
        try:
            import plotly.graph_objects as go
            from plotly.subplots import make_subplots
            
            # Create subplot with secondary y-axis for volume
            fig = make_subplots(
                rows=1, cols=1,
                specs=[[{"secondary_y": True}]],
                subplot_titles=[f'{self.symbol} Price History with Expert Recommendations']
            )
            
            # Prepare x-axis data
            if isinstance(self.price_data.index, pd.DatetimeIndex):
                x_data = self.price_data.index.strftime('%Y-%m-%d').tolist()
            else:
                x_data = list(range(len(self.price_data)))
            
            # Add candlestick chart
            fig.add_trace(
                go.Candlestick(
                    x=x_data,
                    open=self.price_data['Open'],
                    high=self.price_data['High'],
                    low=self.price_data['Low'],
                    close=self.price_data['Close'],
                    name='Price',
                    increasing_line_color='#26a69a',
                    increasing_fillcolor='#26a69a',
                    decreasing_line_color='#ef5350',
                    decreasing_fillcolor='#ef5350',
                    showlegend=True
                ),
                row=1, col=1,
                secondary_y=False
            )
            
            # Add volume bars
            if 'Volume' in self.price_data.columns:
                colors_vol = ['#ef5350' if close < open_ else '#26a69a' 
                             for close, open_ in zip(self.price_data['Close'], self.price_data['Open'])]
                
                fig.add_trace(
                    go.Bar(
                        x=x_data,
                        y=self.price_data['Volume'],
                        name='Volume',
                        marker_color=colors_vol,
                        opacity=0.15,
                        showlegend=True,
                        yaxis='y2'
                    ),
                    row=1, col=1,
                    secondary_y=True
                )
            
            # Add recommendation markers
            self._add_recommendation_markers(fig)
            
            # Update layout
            fig.update_layout(
                title={
                    'text': f'{self.symbol} - Price History with Recommendations',
                    'x': 0.5,
                    'xanchor': 'center',
                    'font': {'size': 20, 'color': '#1f2937'}
                },
                height=700,
                width=None,  # Let it auto-size to container
                autosize=True,
                hovermode='x unified',
                showlegend=True,
                legend=dict(
                    orientation="v",
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=1.01,
                    bgcolor="rgba(255, 255, 255, 0.9)",
                    bordercolor="#e5e7eb",
                    borderwidth=1
                ),
                template='plotly_white',
                margin=dict(l=60, r=200, t=80, b=60),  # Reduced right margin from 220 to 200
                paper_bgcolor='white',
                plot_bgcolor='#fafafa',
                dragmode='pan',
                xaxis_rangeslider_visible=False
            )
            
            # Update axes
            fig.update_xaxes(
                title_text="Date",
                showgrid=True,
                gridwidth=1,
                gridcolor='#e5e7eb',
                tickangle=-45,
                rangebreaks=[dict(bounds=["sat", "mon"])]  # Remove weekends
            )
            
            fig.update_yaxes(
                title_text="Price ($)",
                showgrid=True,
                gridwidth=1,
                gridcolor='#e5e7eb',
                secondary_y=False
            )
            
            fig.update_yaxes(
                title_text="Volume",
                showgrid=False,
                secondary_y=True
            )
            
            # Render chart - use full width with responsive config
            # The responsive=True config option makes Plotly automatically resize to container
            ui.plotly(fig).classes('w-full').style('height: 700px; width: 100%; max-width: 100%;')
            #config={'responsive': True}
        except ImportError:
            logger.error("Plotly not installed")
            ui.label('Plotly is required to display charts. Please install: pip install plotly').classes('text-red-500')
        except Exception as e:
            logger.error(f"Error rendering chart with recommendations: {e}", exc_info=True)
            ui.label(f'Error creating chart: {e}').classes('text-red-500')
    
    def _add_recommendation_markers(self, fig) -> None:
        """Add recommendation markers to the chart."""
        try:
            # Filter recommendations by visible experts
            visible_recs = [
                rec for rec in self.recommendations
                if self.visible_experts.get(rec['expert_id'], False) and rec['date']
            ]
            
            if not visible_recs:
                return
            
            logger.info(f"Adding {len(visible_recs)} recommendation markers to chart")
            
            # Define colors and icons for different actions
            action_colors = {
                'BUY': '#10b981',
                'SELL': '#ef4444',
                'HOLD': '#f59e0b',
                'ERROR': '#6b7280'
            }
            
            action_icons = {
                'BUY': '📈',
                'SELL': '📉',
                'HOLD': '➖',
                'ERROR': '❌'
            }
            
            # Group recommendations by date to stack annotations
            from collections import defaultdict
            recs_by_date = defaultdict(list)
            
            for rec in visible_recs:
                date_str = rec['date'].strftime('%Y-%m-%d')
                recs_by_date[date_str].append(rec)
            
            # Add markers for each recommendation
            for date_str, date_recs in recs_by_date.items():
                # Add vertical line for each unique date
                # Extract action string from enum
                first_rec = date_recs[0]
                action_str = str(first_rec['action'].value if hasattr(first_rec['action'], 'value') else first_rec['action'])
                action_str = action_str.replace('OrderRecommendation.', '').upper()
                color = action_colors.get(action_str, '#6b7280')
                
                fig.add_shape(
                    type="line",
                    x0=date_str,
                    x1=date_str,
                    y0=0,
                    y1=1,
                    yref="y domain",
                    xref="x",
                    line=dict(
                        color=color,
                        width=2,
                        dash="dash"
                    ),
                    opacity=0.5
                )
                
                # Add annotation for each recommendation at this date
                y_position = 0.95  # Start near top
                y_step = 0.15  # Move down for each annotation
                
                for i, rec in enumerate(date_recs):
                    action_str = str(rec['action'].value if hasattr(rec['action'], 'value') else rec['action'])
                    action_str = action_str.replace('OrderRecommendation.', '').upper()
                    
                    icon = action_icons.get(action_str, '📊')
                    color = action_colors.get(action_str, '#6b7280')
                    confidence = rec.get('confidence', 0)
                    time_horizon = str(rec.get('time_horizon', 'UNKNOWN')).replace('TimeHorizon.', '')
                    expert_name = rec['expert_name']
                    
                    # Create label with action, confidence, and time horizon
                    label = f"{icon} {action_str}<br>{confidence:.1f}% | {time_horizon}<br><small>{expert_name}</small>"
                    
                    fig.add_annotation(
                        x=date_str,
                        y=y_position - (i * y_step),
                        yref="y domain",
                        xref="x",
                        text=label,
                        showarrow=True,
                        arrowhead=2,
                        arrowsize=1,
                        arrowwidth=2,
                        arrowcolor=color,
                        ax=40 if i % 2 == 0 else -40,  # Alternate left/right
                        ay=-30,
                        font=dict(size=10, color=color, family='Arial Black'),
                        bgcolor="rgba(255, 255, 255, 0.95)",
                        bordercolor=color,
                        borderwidth=2,
                        borderpad=4,
                        opacity=0.9
                    )
            
            logger.info(f"Successfully added recommendation markers")
            
        except Exception as e:
            logger.error(f"Error adding recommendation markers: {e}", exc_info=True)
    
    def _render_recommendations_table(self) -> None:
        """Render table showing all recommendations."""
        if not self.recommendations:
            return
        
        with ui.card().classes('w-full p-4 mt-4'):
            ui.label('Expert Recommendations').classes('text-h6 mb-4')
            
            # Filter by visible experts
            visible_recs = [
                rec for rec in self.recommendations
                if self.visible_experts.get(rec['expert_id'], False)
            ]
            
            if not visible_recs:
                ui.label('No recommendations from selected experts').classes('text-gray-500')
                return
            
            # Prepare table data
            columns = [
                {'name': 'date', 'label': 'Date', 'field': 'date', 'sortable': True, 'align': 'left'},
                {'name': 'expert', 'label': 'Expert', 'field': 'expert', 'sortable': True, 'align': 'left'},
                {'name': 'action', 'label': 'Action', 'field': 'action', 'sortable': True, 'align': 'center'},
                {'name': 'confidence', 'label': 'Confidence', 'field': 'confidence', 'sortable': True, 'align': 'center'},
                {'name': 'time_horizon', 'label': 'Time Horizon', 'field': 'time_horizon', 'sortable': True, 'align': 'center'},
                {'name': 'expected_profit', 'label': 'Expected Profit', 'field': 'expected_profit', 'sortable': True, 'align': 'right'},
                {'name': 'price', 'label': 'Price at Date', 'field': 'price', 'sortable': True, 'align': 'right'},
            ]
            
            rows = []
            for rec in visible_recs:
                action_str = str(rec['action'].value if hasattr(rec['action'], 'value') else rec['action'])
                action_str = action_str.replace('OrderRecommendation.', '')
                
                time_horizon_str = str(rec.get('time_horizon', 'UNKNOWN')).replace('TimeHorizon.', '')
                
                rows.append({
                    'date': rec['date'].strftime('%Y-%m-%d %H:%M') if rec['date'] else 'N/A',
                    'expert': rec['expert_name'],
                    'action': action_str,
                    'confidence': f"{rec.get('confidence', 0):.1f}%",
                    'time_horizon': time_horizon_str,
                    'expected_profit': f"{rec.get('expected_profit', 0):.2f}%",
                    'price': f"${rec.get('price_at_date', 0):.2f}"
                })
            
            ui.table(
                columns=columns,
                rows=rows,
                row_key='date',
                pagination={'rowsPerPage': 20, 'sortBy': 'date', 'descending': True}
            ).classes('w-full')


def render_market_analysis_history(symbol: str):
    """
    Render function for the market analysis history page.
    
    Args:
        symbol: The instrument symbol to display history for
    """
    page = MarketAnalysisHistoryPage(symbol)
    page.render()
