"""
Instrument Distribution Pie Chart Component

A pie chart showing distribution of open positions by instrument labels, categories, or type.
"""

from nicegui import ui
from sqlmodel import select
from typing import Dict, List, Literal
from collections import defaultdict
from ...core.db import get_db
from ...core.models import Position, Instrument
from ...logger import logger


class InstrumentDistributionChart:
    """Component that displays a pie chart of open positions by instrument categorization."""
    
    def __init__(self, positions: List[Dict] = None, grouping_field: Literal['labels', 'categories'] = 'labels'):
        """
        Initialize the chart with positions data.
        
        Args:
            positions: List of position dictionaries from account providers.
                      If None, chart will display "No data" message.
            grouping_field: Field to group positions by - either 'labels' or 'categories'.
                           Defaults to 'labels'.
        """
        self.chart = None
        self.positions = positions or []
        self.grouping_field = grouping_field
        self.render()
    
    def calculate_position_distribution(self) -> Dict[str, float]:
        """
        Calculate market value distribution by instrument labels/categories.
        
        Returns:
            Dict mapping labels/categories to their total market value
        """
        distribution = defaultdict(float)
        
        with get_db() as session:
            logger.debug(f"Processing {len(self.positions)} positions for distribution by {self.grouping_field}")
            
            if not self.positions:
                return {}
            
            # Group by instrument labels/categories based on grouping_field
            for position in self.positions:
                try:
                    # Get symbol from position dict
                    symbol = position.get('symbol', '')
                    if not symbol:
                        logger.debug(f"Position without symbol: {position}")
                        continue
                    
                    # Get market value (convert from string if needed)
                    market_value_str = position.get('market_value', '0')
                    try:
                        market_value = float(market_value_str) if isinstance(market_value_str, str) else market_value_str
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid market_value for {symbol}: {market_value_str}")
                        continue
                    
                    # Get instrument info from database
                    instrument = session.exec(
                        select(Instrument).where(Instrument.name == symbol)
                    ).first()
                    
                    if instrument:
                        # Select categorization based on grouping_field
                        if self.grouping_field == 'labels':
                            # Use labels if available, otherwise categories, otherwise type
                            if instrument.labels and len(instrument.labels) > 0:
                                category = instrument.labels[0]
                            elif instrument.categories and len(instrument.categories) > 0:
                                category = instrument.categories[0]
                            else:
                                category = instrument.instrument_type.value if instrument.instrument_type else 'Unknown'
                        else:  # grouping_field == 'categories'
                            # Use categories if available, otherwise labels, otherwise type
                            if instrument.categories and len(instrument.categories) > 0:
                                category = instrument.categories[0]
                            elif instrument.labels and len(instrument.labels) > 0:
                                category = instrument.labels[0]
                            else:
                                category = instrument.instrument_type.value if instrument.instrument_type else 'Unknown'
                    else:
                        # Instrument not in database, use symbol as category
                        category = 'Uncategorized'
                        logger.debug(f"Instrument {symbol} not found in database")
                    
                    # Add market value to category
                    distribution[category] += abs(market_value)
                    
                    logger.debug(f"Position {symbol}: {category}, Market Value: ${market_value:.2f}")
                    
                except Exception as e:
                    symbol = position.get('symbol', 'unknown') if isinstance(position, dict) else 'unknown'
                    logger.error(f"Error processing position {symbol}: {e}", exc_info=True)
                    continue
            
            # Sort by value (highest to lowest)
            distribution = dict(sorted(distribution.items(), key=lambda x: x[1], reverse=True))
            
            logger.info(f"Calculated distribution across {len(distribution)} {self.grouping_field}")
        
        return distribution
    
    def render(self):
        """Render the instrument distribution pie chart."""
        # Determine chart title based on grouping field
        chart_title = '🥧 Position Distribution by Label' if self.grouping_field == 'labels' else '🥧 Position Distribution by Category'
        category_label = 'Label' if self.grouping_field == 'labels' else 'Category'
        
        with ui.card().classes('p-4'):
            ui.label(chart_title).classes('text-h6 mb-4')
            
            # Get distribution data
            distribution = self.calculate_position_distribution()
            
            if not distribution:
                ui.label('No open positions found.').classes('text-sm text-gray-500')
                return
            
            # Prepare data for pie chart
            pie_data = [
                {'value': round(value, 2), 'name': name}
                for name, value in distribution.items()
            ]
            
            # Calculate total
            total_value = sum(distribution.values())
            
            # Modern color palette for dark theme
            colors = [
                '#00d4aa', '#4dabf7', '#ffa94d', '#ff6b6b', '#9775fa',
                '#69db7c', '#ffd43b', '#ff8787', '#74c0fc', '#a9e34b',
                '#f783ac', '#63e6be', '#da77f2', '#fab005', '#40c057'
            ]
            
            # Create echart options
            options = {
                'backgroundColor': 'transparent',
                'color': colors,
                'tooltip': {
                    'trigger': 'item',
                    'formatter': '{b}<br/>${c:,.2f}<br/>{d}%',
                    'backgroundColor': 'rgba(37, 43, 59, 0.95)',
                    'borderColor': 'rgba(255, 255, 255, 0.1)',
                    'textStyle': {
                        'color': '#ffffff'
                    }
                },
                'legend': {
                    'show': False  # Hide legend, use labels instead
                },
                'series': [{
                    'name': 'Position Distribution',
                    'type': 'pie',
                    'radius': ['35%', '55%'],
                    'center': ['50%', '50%'],
                    'avoidLabelOverlap': True,
                    'itemStyle': {
                        'borderRadius': 4,
                        'borderColor': 'rgba(26, 31, 46, 0.8)',
                        'borderWidth': 2
                    },
                    'label': {
                        'show': True,
                        'position': 'outside',
                        'fontSize': 9,
                        'color': '#a0aec0',
                        'formatter': '{b}\n{d}%',
                        'lineHeight': 12,
                        'overflow': 'truncate',
                        'width': 80
                    },
                    'emphasis': {
                        'label': {
                            'show': True,
                            'fontSize': 11,
                            'fontWeight': 'bold',
                            'color': '#ffffff'
                        },
                        'itemStyle': {
                            'shadowBlur': 10,
                            'shadowOffsetX': 0,
                            'shadowColor': 'rgba(0, 0, 0, 0.5)'
                        }
                    },
                    'labelLine': {
                        'show': True,
                        'length': 10,
                        'length2': 15,
                        'lineStyle': {
                            'color': 'rgba(255, 255, 255, 0.3)'
                        }
                    },
                    'data': pie_data
                }]
            }
            
            # Create the chart - use w-full for responsive width
            self.chart = ui.echart(options).classes('w-full h-64')
            
            # Add summary statistics
            with ui.row().classes('w-full justify-between mt-4 text-sm'):
                ui.label(f'Total {category_label}s: {len(distribution)}').classes('text-gray-600')
                ui.label(f'Total Market Value: ${total_value:,.2f}').classes('font-bold text-blue-600')
            
            # Add detailed breakdown table
            with ui.expansion('View Detailed Breakdown', icon='table_chart').classes('w-full mt-2'):
                columns = [
                    {'name': 'category', 'label': category_label, 'field': 'category', 'align': 'left', 'sortable': True},
                    {'name': 'value', 'label': 'Market Value', 'field': 'value', 'align': 'right', 'sortable': True},
                    {'name': 'percentage', 'label': 'Percentage', 'field': 'percentage', 'align': 'right', 'sortable': True}
                ]
                
                rows = [
                    {
                        'category': name,
                        'value': f'${value:,.2f}',
                        'percentage': f'{(value/total_value*100):.1f}%'
                    }
                    for name, value in distribution.items()
                ]
                
                ui.table(columns=columns, rows=rows, row_key='category').classes('w-full')
    
    def refresh(self, positions: List[Dict] = None):
        """
        Refresh the chart with updated data.
        
        Args:
            positions: New list of position dictionaries. If None, uses existing positions.
        """
        if positions is not None:
            self.positions = positions
        
        if self.chart:
            distribution = self.calculate_position_distribution()
            
            if distribution:
                pie_data = [
                    {'value': value, 'name': f"{name} (${value:,.2f})"}
                    for name, value in distribution.items()
                ]
                
                self.chart.options['series'][0]['data'] = pie_data
                self.chart.update()
