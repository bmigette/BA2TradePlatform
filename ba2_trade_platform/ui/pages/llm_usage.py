"""
LLM Usage Tracking UI Page - Visualize token consumption and costs.

Provides comprehensive charts and statistics for monitoring LLM API usage across
all experts, accounts, and use cases.
"""

from nicegui import ui
from datetime import datetime
import asyncio
from typing import Dict, Any

from ba2_trade_platform.core.LLMUsageQueries import (
    get_usage_summary,
    get_usage_by_day,
    get_usage_by_model,
    get_usage_by_expert,
    get_usage_by_use_case,
    get_usage_by_provider,
    get_recent_requests
)


def format_number(value: float) -> str:
    """Format large numbers with K/M suffixes."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    elif value >= 1_000:
        return f"{value / 1_000:.1f}K"
    else:
        return f"{value:.0f}"


def format_cost(value: float) -> str:
    """Format cost in USD."""
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


class LLMUsagePage:
    """LLM Usage tracking page with charts and statistics."""
    
    def __init__(self):
        self.days_filter = 30
        self.summary_cards = {}
        self.charts = {}
        self.recent_table = None
        
    def create_summary_card(self, title: str, value: str, icon: str, color: str = "primary"):
        """Create a summary statistic card."""
        with ui.card().classes('w-full'):
            with ui.row().classes('w-full items-center'):
                ui.icon(icon, size='lg').classes(f'text-{color}')
                with ui.column().classes('flex-grow'):
                    ui.label(title).classes('text-caption text-grey-7')
                    label = ui.label(value).classes('text-h6 font-bold')
                    self.summary_cards[title] = label
    
    def create_tokens_over_time_chart(self, data: list):
        """Create line chart for tokens over time."""
        if not data:
            return ui.label('No data available').classes('text-grey-6')
        
        chart_options = {
            'backgroundColor': 'transparent',
            'title': {
                'text': 'Daily Token Usage',
                'left': 'center',
                'textStyle': {
                    'color': '#a0aec0',
                    'fontSize': 16,
                    'fontWeight': 'normal'
                }
            },
            'tooltip': {
                'trigger': 'axis',
                'backgroundColor': 'rgba(37, 43, 59, 0.95)',
                'borderColor': 'rgba(255, 255, 255, 0.1)',
                'textStyle': {
                    'color': '#ffffff'
                }
            },
            'grid': {
                'left': '3%',
                'right': '4%',
                'bottom': '10%',
                'top': '20%',
                'containLabel': True
            },
            'xAxis': {
                'type': 'category',
                'data': [d['date'] for d in data],
                'axisLabel': {
                    'color': '#a0aec0',
                    'fontSize': 11
                },
                'axisLine': {
                    'lineStyle': {
                        'color': 'rgba(255, 255, 255, 0.1)'
                    }
                }
            },
            'yAxis': {
                'type': 'value',
                'name': 'Tokens',
                'nameTextStyle': {
                    'color': '#a0aec0'
                },
                'axisLabel': {
                    'color': '#a0aec0',
                    'formatter': '{value}'
                },
                'axisLine': {
                    'lineStyle': {
                        'color': 'rgba(255, 255, 255, 0.1)'
                    }
                },
                'splitLine': {
                    'lineStyle': {
                        'color': 'rgba(255, 255, 255, 0.05)'
                    }
                }
            },
            'series': [{
                'name': 'Total Tokens',
                'type': 'line',
                'smooth': True,
                'symbol': 'circle',
                'symbolSize': 6,
                'data': [d['total_tokens'] for d in data],
                'lineStyle': {
                    'width': 3,
                    'color': '#1976D2'
                },
                'areaStyle': {
                    'color': {
                        'type': 'linear',
                        'x': 0,
                        'y': 0,
                        'x2': 0,
                        'y2': 1,
                        'colorStops': [
                            {'offset': 0, 'color': 'rgba(25, 118, 210, 0.3)'},
                            {'offset': 1, 'color': 'rgba(25, 118, 210, 0.05)'}
                        ]
                    }
                },
                'itemStyle': {
                    'color': '#1976D2',
                    'borderWidth': 2,
                    'borderColor': '#ffffff'
                }
            }]
        }
        
        chart = ui.echart(chart_options).classes('w-full h-96')
        self.charts['tokens_over_time'] = chart
        return chart
    
    def create_usage_by_model_chart(self, data: list):
        """Create bar chart for usage by model."""
        if not data:
            return ui.label('No data available').classes('text-grey-6')
        
        chart_options = {
            'backgroundColor': 'transparent',
            'title': {
                'text': 'Token Usage by Model',
                'left': 'center',
                'textStyle': {
                    'color': '#a0aec0',
                    'fontSize': 16,
                    'fontWeight': 'normal'
                }
            },
            'tooltip': {
                'trigger': 'axis',
                'backgroundColor': 'rgba(37, 43, 59, 0.95)',
                'borderColor': 'rgba(255, 255, 255, 0.1)',
                'textStyle': {
                    'color': '#ffffff'
                },
                'axisPointer': {
                    'type': 'shadow'
                }
            },
            'grid': {
                'left': '3%',
                'right': '4%',
                'bottom': '20%',
                'top': '20%',
                'containLabel': True
            },
            'xAxis': {
                'type': 'category',
                'data': [d['model'] for d in data],
                'axisLabel': {
                    'rotate': 45,
                    'interval': 0,
                    'color': '#a0aec0',
                    'fontSize': 10
                },
                'axisLine': {
                    'lineStyle': {
                        'color': 'rgba(255, 255, 255, 0.1)'
                    }
                }
            },
            'yAxis': {
                'type': 'value',
                'name': 'Tokens',
                'nameTextStyle': {
                    'color': '#a0aec0'
                },
                'axisLabel': {
                    'color': '#a0aec0'
                },
                'axisLine': {
                    'lineStyle': {
                        'color': 'rgba(255, 255, 255, 0.1)'
                    }
                },
                'splitLine': {
                    'lineStyle': {
                        'color': 'rgba(255, 255, 255, 0.05)'
                    }
                }
            },
            'series': [{
                'name': 'Total Tokens',
                'type': 'bar',
                'data': [
                    {
                        'value': d['total_tokens'],
                        'itemStyle': {
                            'borderRadius': [4, 4, 0, 0],
                            'color': {
                                'type': 'linear',
                                'x': 0,
                                'y': 0,
                                'x2': 0,
                                'y2': 1,
                                'colorStops': [
                                    {'offset': 0, 'color': '#66BB6A'},
                                    {'offset': 1, 'color': '#43A047'}
                                ]
                            }
                        }
                    } for d in data
                ],
                'barMaxWidth': 40
            }]
        }
        
        chart = ui.echart(chart_options).classes('w-full h-96')
        self.charts['usage_by_model'] = chart
        return chart
    
    def create_usage_by_provider_chart(self, data: list):
        """Create pie chart for usage by provider."""
        if not data:
            return ui.label('No data available').classes('text-grey-6')
        
        # Color palette for providers
        colors = ['#5470C6', '#91CC75', '#FAC858', '#EE6666', '#73C0DE', '#3BA272', '#FC8452', '#9A60B4']
        
        chart_options = {
            'backgroundColor': 'transparent',
            'title': {
                'text': 'Token Usage by Provider',
                'left': 'center',
                'textStyle': {
                    'color': '#a0aec0',
                    'fontSize': 16,
                    'fontWeight': 'normal'
                }
            },
            'tooltip': {
                'trigger': 'item',
                'formatter': '{b}: {c} tokens ({d}%)',
                'backgroundColor': 'rgba(37, 43, 59, 0.95)',
                'borderColor': 'rgba(255, 255, 255, 0.1)',
                'textStyle': {
                    'color': '#ffffff'
                }
            },
            'legend': {
                'bottom': '5%',
                'textStyle': {
                    'color': '#a0aec0'
                }
            },
            'color': colors,
            'series': [{
                'name': 'Tokens',
                'type': 'pie',
                'radius': ['40%', '65%'],
                'center': ['50%', '45%'],
                'data': [
                    {'name': d['provider'], 'value': d['total_tokens']}
                    for d in data
                ],
                'label': {
                    'color': '#a0aec0',
                    'fontSize': 12
                },
                'emphasis': {
                    'itemStyle': {
                        'shadowBlur': 15,
                        'shadowOffsetX': 0,
                        'shadowColor': 'rgba(0, 0, 0, 0.7)'
                    },
                    'label': {
                        'fontSize': 14,
                        'fontWeight': 'bold'
                    }
                },
                'itemStyle': {
                    'borderRadius': 8,
                    'borderColor': 'rgba(0, 0, 0, 0.3)',
                    'borderWidth': 2
                }
            }]
        }
        
        chart = ui.echart(chart_options).classes('w-full h-96')
        self.charts['usage_by_provider'] = chart
        return chart
    
    def create_usage_by_use_case_chart(self, data: list):
        """Create bar chart for usage by use case."""
        if not data:
            return ui.label('No data available').classes('text-grey-6')
        
        chart_options = {
            'backgroundColor': 'transparent',
            'title': {
                'text': 'Token Usage by Use Case',
                'left': 'center',
                'textStyle': {
                    'color': '#a0aec0',
                    'fontSize': 16,
                    'fontWeight': 'normal'
                }
            },
            'tooltip': {
                'trigger': 'axis',
                'backgroundColor': 'rgba(37, 43, 59, 0.95)',
                'borderColor': 'rgba(255, 255, 255, 0.1)',
                'textStyle': {
                    'color': '#ffffff'
                },
                'axisPointer': {
                    'type': 'shadow'
                }
            },
            'grid': {
                'left': '3%',
                'right': '4%',
                'bottom': '20%',
                'top': '20%',
                'containLabel': True
            },
            'xAxis': {
                'type': 'category',
                'data': [d['use_case'] for d in data],
                'axisLabel': {
                    'rotate': 45,
                    'interval': 0,
                    'color': '#a0aec0',
                    'fontSize': 10
                },
                'axisLine': {
                    'lineStyle': {
                        'color': 'rgba(255, 255, 255, 0.1)'
                    }
                }
            },
            'yAxis': {
                'type': 'value',
                'name': 'Tokens',
                'nameTextStyle': {
                    'color': '#a0aec0'
                },
                'axisLabel': {
                    'color': '#a0aec0'
                },
                'axisLine': {
                    'lineStyle': {
                        'color': 'rgba(255, 255, 255, 0.1)'
                    }
                },
                'splitLine': {
                    'lineStyle': {
                        'color': 'rgba(255, 255, 255, 0.05)'
                    }
                }
            },
            'series': [{
                'name': 'Total Tokens',
                'type': 'bar',
                'data': [
                    {
                        'value': d['total_tokens'],
                        'itemStyle': {
                            'borderRadius': [4, 4, 0, 0],
                            'color': {
                                'type': 'linear',
                                'x': 0,
                                'y': 0,
                                'x2': 0,
                                'y2': 1,
                                'colorStops': [
                                    {'offset': 0, 'color': '#FFB74D'},
                                    {'offset': 1, 'color': '#FB8C00'}
                                ]
                            }
                        }
                    } for d in data
                ],
                'barMaxWidth': 40
            }]
        }
        
        chart = ui.echart(chart_options).classes('w-full h-96')
        self.charts['usage_by_use_case'] = chart
        return chart
    
    def create_usage_by_expert_chart(self, data: list):
        """Create bar chart for usage by expert."""
        if not data:
            return ui.label('No data available').classes('text-grey-6')
        
        chart_options = {
            'backgroundColor': 'transparent',
            'title': {
                'text': 'Token Usage by Expert',
                'left': 'center',
                'textStyle': {
                    'color': '#a0aec0',
                    'fontSize': 16,
                    'fontWeight': 'normal'
                }
            },
            'tooltip': {
                'trigger': 'axis',
                'backgroundColor': 'rgba(37, 43, 59, 0.95)',
                'borderColor': 'rgba(255, 255, 255, 0.1)',
                'textStyle': {
                    'color': '#ffffff'
                },
                'axisPointer': {
                    'type': 'shadow'
                }
            },
            'grid': {
                'left': '3%',
                'right': '4%',
                'bottom': '20%',
                'top': '20%',
                'containLabel': True
            },
            'xAxis': {
                'type': 'category',
                'data': [d['expert_name'] for d in data],
                'axisLabel': {
                    'rotate': 45,
                    'interval': 0,
                    'color': '#a0aec0',
                    'fontSize': 10
                },
                'axisLine': {
                    'lineStyle': {
                        'color': 'rgba(255, 255, 255, 0.1)'
                    }
                }
            },
            'yAxis': {
                'type': 'value',
                'name': 'Tokens',
                'nameTextStyle': {
                    'color': '#a0aec0'
                },
                'axisLabel': {
                    'color': '#a0aec0'
                },
                'axisLine': {
                    'lineStyle': {
                        'color': 'rgba(255, 255, 255, 0.1)'
                    }
                },
                'splitLine': {
                    'lineStyle': {
                        'color': 'rgba(255, 255, 255, 0.05)'
                    }
                }
            },
            'series': [{
                'name': 'Total Tokens',
                'type': 'bar',
                'data': [
                    {
                        'value': d['total_tokens'],
                        'itemStyle': {
                            'borderRadius': [4, 4, 0, 0],
                            'color': {
                                'type': 'linear',
                                'x': 0,
                                'y': 0,
                                'x2': 0,
                                'y2': 1,
                                'colorStops': [
                                    {'offset': 0, 'color': '#EF5350'},
                                    {'offset': 1, 'color': '#E53935'}
                                ]
                            }
                        }
                    } for d in data
                ],
                'barMaxWidth': 40
            }]
        }
        
        chart = ui.echart(chart_options).classes('w-full h-96')
        self.charts['usage_by_expert'] = chart
        return chart
    
    def create_recent_requests_table(self, data: list):
        """Create table for recent requests."""
        if not data:
            return ui.label('No recent requests').classes('text-grey-6')
        
        columns = [
            {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'align': 'left', 'sortable': True},
            {'name': 'use_case', 'label': 'Use Case', 'field': 'use_case', 'align': 'left', 'sortable': True},
            {'name': 'model', 'label': 'Model', 'field': 'model', 'align': 'left', 'sortable': True},
            {'name': 'provider', 'label': 'Provider', 'field': 'provider', 'align': 'left', 'sortable': True},
            {'name': 'tokens', 'label': 'Tokens', 'field': 'total_tokens', 'align': 'right', 'sortable': True},
            {'name': 'duration', 'label': 'Duration (ms)', 'field': 'duration_ms', 'align': 'right', 'sortable': True},
            {'name': 'expert', 'label': 'Expert ID', 'field': 'expert_instance_id', 'align': 'right', 'sortable': True},
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left', 'sortable': True},
        ]
        
        # Format timestamps
        for row in data:
            dt = datetime.fromisoformat(row['timestamp'])
            row['timestamp'] = dt.strftime('%Y-%m-%d %H:%M:%S')
        
        table = ui.table(
            columns=columns, 
            rows=data, 
            row_key='id',
            pagination={'rowsPerPage': 50, 'sortBy': 'timestamp', 'descending': True}
        ).classes('w-full')
        table.add_slot('body-cell-tokens', '''
            <q-td :props="props">
                <q-badge color="primary">{{ props.value.toLocaleString() }}</q-badge>
            </q-td>
        ''')
        
        self.recent_table = table
        return table
    
    async def load_data(self):
        """Load all usage data and update UI."""
        # Get summary
        summary = get_usage_summary(self.days_filter)
        
        # Update summary cards
        if 'Total Requests' in self.summary_cards:
            self.summary_cards['Total Requests'].set_text(format_number(summary['total_requests']))
        if 'Total Tokens' in self.summary_cards:
            self.summary_cards['Total Tokens'].set_text(format_number(summary['total_tokens']))
        if 'Unique Models' in self.summary_cards:
            self.summary_cards['Unique Models'].set_text(str(summary['unique_models']))
        
        # Update charts
        if 'tokens_over_time' in self.charts:
            data = get_usage_by_day(self.days_filter)
            self.charts['tokens_over_time'].options['xAxis']['data'] = [d['date'] for d in data]
            self.charts['tokens_over_time'].options['series'][0]['data'] = [d['total_tokens'] for d in data]
            self.charts['tokens_over_time'].update()
        
        if 'usage_by_model' in self.charts:
            data = get_usage_by_model(self.days_filter, 10)
            self.charts['usage_by_model'].options['xAxis']['data'] = [d['model'] for d in data]
            self.charts['usage_by_model'].options['series'][0]['data'] = [
                {
                    'value': d['total_tokens'],
                    'itemStyle': {
                        'borderRadius': [4, 4, 0, 0],
                        'color': {
                            'type': 'linear',
                            'x': 0,
                            'y': 0,
                            'x2': 0,
                            'y2': 1,
                            'colorStops': [
                                {'offset': 0, 'color': '#66BB6A'},
                                {'offset': 1, 'color': '#43A047'}
                            ]
                        }
                    }
                } for d in data
            ]
            self.charts['usage_by_model'].update()
        
        if 'usage_by_provider' in self.charts:
            data = get_usage_by_provider(self.days_filter)
            self.charts['usage_by_provider'].options['series'][0]['data'] = [
                {'name': d['provider'], 'value': d['total_tokens']} for d in data
            ]
            self.charts['usage_by_provider'].update()
        
        if 'usage_by_use_case' in self.charts:
            data = get_usage_by_use_case(self.days_filter)
            self.charts['usage_by_use_case'].options['xAxis']['data'] = [d['use_case'] for d in data]
            self.charts['usage_by_use_case'].options['series'][0]['data'] = [
                {
                    'value': d['total_tokens'],
                    'itemStyle': {
                        'borderRadius': [4, 4, 0, 0],
                        'color': {
                            'type': 'linear',
                            'x': 0,
                            'y': 0,
                            'x2': 0,
                            'y2': 1,
                            'colorStops': [
                                {'offset': 0, 'color': '#FFB74D'},
                                {'offset': 1, 'color': '#FB8C00'}
                            ]
                        }
                    }
                } for d in data
            ]
            self.charts['usage_by_use_case'].update()
        
        if 'usage_by_expert' in self.charts:
            data = get_usage_by_expert(self.days_filter, 10)
            self.charts['usage_by_expert'].options['xAxis']['data'] = [d['expert_name'] for d in data]
            self.charts['usage_by_expert'].options['series'][0]['data'] = [
                {
                    'value': d['total_tokens'],
                    'itemStyle': {
                        'borderRadius': [4, 4, 0, 0],
                        'color': {
                            'type': 'linear',
                            'x': 0,
                            'y': 0,
                            'x2': 0,
                            'y2': 1,
                            'colorStops': [
                                {'offset': 0, 'color': '#EF5350'},
                                {'offset': 1, 'color': '#E53935'}
                            ]
                        }
                    }
                } for d in data
            ]
            self.charts['usage_by_expert'].update()
        
        if self.recent_table:
            data = get_recent_requests(1000)
            for row in data:
                dt = datetime.fromisoformat(row['timestamp'])
                row['timestamp'] = dt.strftime('%Y-%m-%d %H:%M:%S')
            self.recent_table.rows = data
            self.recent_table.update()
    
    async def on_days_change(self, value: int):
        """Handle days filter change."""
        self.days_filter = value
        await self.load_data()
    
    def render(self):
        """Render the LLM usage page."""
        ui.label('LLM Usage Tracking').classes('text-h4 mb-4')
        
        # Time filter
        with ui.row().classes('w-full mb-4 items-center'):
            ui.label('Time Period:').classes('mr-2')
            with ui.select(
                options={7: '7 days', 30: '30 days', 90: '90 days'},
                value=self.days_filter,
                on_change=lambda e: asyncio.create_task(self.on_days_change(e.value))
            ).classes('w-32'):
                pass
            
            ui.space()
            
            ui.button(
                'Refresh',
                icon='refresh',
                on_click=lambda: asyncio.create_task(self.load_data())
            ).props('flat color=primary')
        
        # Summary cards
        with ui.row().classes('w-full mb-4 gap-4'):
            with ui.column().classes('flex-grow'):
                self.create_summary_card('Total Requests', '0', 'api', 'primary')
            with ui.column().classes('flex-grow'):
                self.create_summary_card('Total Tokens', '0', 'data_usage', 'green')
            with ui.column().classes('flex-grow'):
                self.create_summary_card('Unique Models', '0', 'model_training', 'orange')
        
        # Charts
        ui.label('Usage Analytics').classes('text-h6 mb-2 mt-4')
        
        with ui.row().classes('w-full gap-4 mb-4'):
            with ui.card().classes('flex-grow'):
                self.create_tokens_over_time_chart(get_usage_by_day(self.days_filter))
            with ui.card().classes('flex-grow'):
                self.create_usage_by_provider_chart(get_usage_by_provider(self.days_filter))
        
        with ui.row().classes('w-full gap-4 mb-4'):
            with ui.card().classes('flex-grow'):
                self.create_usage_by_model_chart(get_usage_by_model(self.days_filter, 10))
            with ui.card().classes('flex-grow'):
                self.create_usage_by_use_case_chart(get_usage_by_use_case(self.days_filter))
        
        with ui.row().classes('w-full gap-4 mb-4'):
            with ui.card().classes('flex-grow'):
                self.create_usage_by_expert_chart(get_usage_by_expert(self.days_filter, 10))
        
        # Recent requests table
        ui.label('Recent Requests (Last 1000)').classes('text-h6 mb-2 mt-4')
        with ui.card().classes('w-full'):
            self.create_recent_requests_table(get_recent_requests(1000))
        
        # Initial data load
        asyncio.create_task(self.load_data())


def create_llm_usage_page():
    """Factory function to create and render the LLM usage page."""
    page = LLMUsagePage()
    page.render()
    return page
