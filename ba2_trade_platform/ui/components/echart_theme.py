"""Shared ECharts dark theme configuration for all chart components."""

# Common color tokens
MUTED_TEXT = '#a0aec0'
TOOLTIP_BG = 'rgba(37, 43, 59, 0.95)'
BORDER_COLOR = 'rgba(255, 255, 255, 0.1)'
SPLIT_LINE_COLOR = 'rgba(255, 255, 255, 0.05)'
ERROR_COLOR = '#ff6b6b'
TEXT_WHITE = '#ffffff'


def get_dark_theme_base():
    """Return base ECharts options for dark theme.

    Includes: backgroundColor, tooltip, legend, grid, xAxis, yAxis styling.
    Callers should deep-merge chart-specific options on top.
    """
    return {
        'backgroundColor': 'transparent',
        'tooltip': {
            'backgroundColor': TOOLTIP_BG,
            'borderColor': BORDER_COLOR,
            'textStyle': {'color': TEXT_WHITE},
        },
        'legend': {
            'textStyle': {'color': MUTED_TEXT},
        },
        'grid': {
            'left': '3%',
            'right': '4%',
            'containLabel': True,
        },
        'xAxis': {
            'axisLabel': {'color': MUTED_TEXT},
            'axisLine': {'lineStyle': {'color': BORDER_COLOR}},
        },
        'yAxis': {
            'axisLabel': {'color': MUTED_TEXT},
            'axisLine': {'lineStyle': {'color': BORDER_COLOR}},
            'splitLine': {'lineStyle': {'color': SPLIT_LINE_COLOR}},
        },
    }


def deep_merge(base, override):
    """Deep merge override dict into base dict. Returns new dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def make_chart_options(**overrides):
    """Create chart options by merging overrides into the dark theme base.

    Example:
        options = make_chart_options(
            tooltip={'trigger': 'axis'},
            grid={'bottom': '20%'},
            xAxis={'axisLabel': {'rotate': 45}},
            series=[...],
        )
    """
    base = get_dark_theme_base()
    return deep_merge(base, overrides)
