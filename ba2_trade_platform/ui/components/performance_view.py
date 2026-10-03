"""Pure helpers behind the Performance tab: formatting, ordering, colours, chart figures.

No NiceGUI import here. Everything the page *decides* (how money is written, which expert
is drawn first and in what colour, how tall a chart is, whether a legend fits below it,
what an empty page says) is a function of plain data, so it is unit-tested without a
browser; ``pages/performance.py`` only places the results on the page.
"""
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import plotly.graph_objects as go

from ..utils.responsive import is_phone_width

# ---------------------------------------------------------------------------
# Period selector
# ---------------------------------------------------------------------------

#: days -> button label. ``3650`` is "All Time" (the page has always meant ten years).
PERIOD_OPTIONS: Dict[int, str] = {7: '7 Days', 30: '30 Days', 90: '90 Days', 365: '1 Year', 3650: 'All Time'}
DEFAULT_PERIOD_DAYS = 30
#: Same mechanism as the Overview page's ``overview_time_range`` (a JSON value in the
#: ``AppSetting`` table, so a phone and a PC agree and it survives a restart); a separate
#: key because the option set is different.
PERIOD_SETTING_KEY = 'performance_time_range'


def normalize_period_days(value: Any) -> int:
    """A stored value -> a valid number of days; anything else (absent, corrupt, retired)
    -> the default. ``bool`` is rejected (``True == 1`` must not pick a period)."""
    if isinstance(value, bool):
        return DEFAULT_PERIOD_DAYS
    return value if value in PERIOD_OPTIONS else DEFAULT_PERIOD_DAYS


def period_caption(days: int) -> str:
    """Subtitle for the KPI tiles: 'Last 30 days', 'Last 1 year', 'All time'."""
    days = normalize_period_days(days)
    if days == 3650:
        return 'All time'
    if days == 365:
        return 'Last 1 year'
    return f'Last {days} days'


def read_period_days() -> int:
    from ..utils.overview_label_scope import read_overview_setting
    return normalize_period_days(read_overview_setting(PERIOD_SETTING_KEY))


def write_period_days(days: int) -> bool:
    from ..utils.overview_label_scope import write_overview_setting
    return write_overview_setting(PERIOD_SETTING_KEY, normalize_period_days(days))


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_money(value: Optional[float], decimals: int = 2) -> str:
    """``-$1.98`` (sign before the symbol), ``$1,234.50``; ``None`` -> an em dash.
    A value that rounds to zero is written as ``$0.00``, never ``-$0.00``."""
    if value is None:
        return '—'
    rounded = round(float(value), decimals)
    if rounded == 0:
        rounded = 0.0
    sign = '-' if rounded < 0 else ''
    return f'{sign}${abs(rounded):,.{decimals}f}'


def format_profit_factor(profit_factor: Optional[float]) -> Tuple[str, str]:
    """``(text, tooltip)``. ``inf`` is "no losing trades", not a number."""
    if profit_factor is None:
        return '—', 'No winning or losing trades in this period, so there is no ratio.'
    if math.isinf(profit_factor):
        return '∞', 'No losing trades in this period (gross profit divided by a gross loss of zero).'
    return f'{profit_factor:.2f}', 'Gross profit divided by gross loss.'


#: Fewest per-trade returns the Sharpe ratio is computed from
#: (``ba2_common.analytics.performance.calculate_sharpe_ratio``).
SHARPE_MIN_RETURNS = 30


def format_sharpe(sharpe: Optional[float], n_returns: int) -> Tuple[str, str]:
    """``(text, tooltip)``. N/A always comes with its reason."""
    if sharpe is None:
        return 'N/A', (f'Needs at least {SHARPE_MIN_RETURNS} closed trades with a measurable '
                       f'return for this expert; it has {n_returns} in this period.')
    return f'{sharpe:.2f}', ('Annualised (252 periods) from this expert\'s per-trade returns, '
                             'risk-free rate 2%.')


def format_days(value: float) -> str:
    return f'{value:.1f}'


# ---------------------------------------------------------------------------
# Expert order and colours (one order, one colour per expert on EVERY chart)
# ---------------------------------------------------------------------------

#: Distinct hues on the dark theme; beyond this many experts the list repeats.
EXPERT_COLORS = [
    '#00d4aa', '#74c0fc', '#ffa94d', '#b197fc', '#ff8787',
    '#ffd43b', '#69db7c', '#f783ac', '#4dabf7', '#a9e34b',
    '#da77f2', '#63e6be', '#fab005', '#ff6b6b', '#38d9a9',
    '#91a7ff', '#e599f7', '#ffc078', '#8ce99a', '#99e9f2',
]
WIN_COLOR = '#00d4aa'
LOSS_COLOR = '#ff6b6b'


def order_experts(expert_metrics: Dict[str, Dict[str, Any]],
                  extra_names: Iterable[str] = ()) -> List[str]:
    """Experts best total P&L first (name breaks ties); names that only appear in
    ``extra_names`` (e.g. in the 12-month charts but not in the selected period) follow,
    alphabetically. The ONE order every chart and the table use."""
    ranked = sorted(expert_metrics, key=lambda n: (-float(expert_metrics[n]['total_pnl']), n))
    rest = sorted(set(extra_names) - set(expert_metrics))
    return ranked + rest


def assign_colors(order: Sequence[str]) -> Dict[str, str]:
    return {name: EXPERT_COLORS[i % len(EXPERT_COLORS)] for i, name in enumerate(order)}


def shorten_label(name: str, max_chars: Optional[int]) -> str:
    """Tick label: the name, or its head and its TAIL joined by an ellipsis (the suffix
    is what tells ``goal2020-small_ED_S1top1`` from ``goal2020-small_ED_S2top1``), at most
    ``max_chars`` long. The full name stays in the hover."""
    if max_chars is None or len(name) <= max_chars:
        return name
    keep = max_chars - 1
    tail = (keep * 9 + 10) // 20       # ~45% tail
    head = keep - tail
    return name[:head] + '…' + name[len(name) - tail:]


def shorten_labels(names: Sequence[str], max_chars: Optional[int]) -> List[str]:
    """Shorten every name to ``max_chars``, then widen the limit until distinct full names
    stay distinct (falls back to the full names). Pure."""
    names = list(names)
    if max_chars is None:
        return names
    distinct = len(set(names))
    limit = max_chars
    longest = max((len(n) for n in names), default=0)
    while limit < longest:
        out = [shorten_label(n, limit) for n in names]
        if len(set(out)) == distinct:
            return out
        limit += 1
    return names


# ---------------------------------------------------------------------------
# Layout per viewport width
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChartLayout:
    phone: bool
    row_px: int            # height of one bar row in a horizontal bar chart
    chrome_px: int         # axis + margins around the rows
    min_height: int
    label_max_chars: Optional[int]
    tick_font: int
    value_pad_frac: float  # extra x-range, as a fraction of the span, for the value labels
    legend_width_px: int   # width a legend row can use
    line_height: int       # plot height of a line chart (legend and axis come on top)
    month_dtick: str


DESKTOP_LAYOUT = ChartLayout(phone=False, row_px=30, chrome_px=50, min_height=240,
                             label_max_chars=None, tick_font=11, value_pad_frac=0.2,
                             legend_width_px=700, line_height=300, month_dtick='M1')
PHONE_LAYOUT = ChartLayout(phone=True, row_px=28, chrome_px=46, min_height=220,
                           label_max_chars=20, tick_font=10, value_pad_frac=0.45,
                           legend_width_px=330, line_height=240, month_dtick='M2')


def choose_layout(width_px: Optional[float]) -> ChartLayout:
    """Phone layout at the app's 639px breakpoint, desktop otherwise (and when the width
    could not be read: a desktop layout on a phone is readable, the reverse is not)."""
    if width_px is None:
        return DESKTOP_LAYOUT
    return PHONE_LAYOUT if is_phone_width(width_px) else DESKTOP_LAYOUT


def bar_chart_height(n_rows: int, layout: ChartLayout) -> int:
    """Height scaled to the number of experts, never below the layout's minimum."""
    return max(layout.min_height, n_rows * layout.row_px + layout.chrome_px)


def padded_range(values: Sequence[float], frac: float) -> List[float]:
    """Axis range that always includes zero and leaves ``frac`` of the span free on the
    side(s) the bars grow towards, so ``outside`` value labels are never clipped."""
    lo = min([0.0] + [float(v) for v in values])
    hi = max([0.0] + [float(v) for v in values])
    if lo == 0.0 and hi == 0.0:
        return [-1.0, 1.0]
    span = hi - lo
    return [lo - frac * span if lo < 0 else 0.0, hi + frac * span if hi > 0 else 0.0]


def legend_rows(names: Sequence[str], avail_px: int, font_px: int = 10) -> int:
    """How many rows a horizontal legend of ``names`` needs in ``avail_px`` (estimated
    from text length; a swatch and padding add ~40px per entry)."""
    if not names:
        return 0
    rows, used = 1, 0
    for name in names:
        item = int(len(name) * font_px * 0.62) + 40
        if used and used + item > avail_px:
            rows += 1
            used = 0
        used += item
    return rows


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------

def empty_state(account_selected: bool, account_has_experts: bool,
                expert_filter_active: bool, days: int) -> Tuple[str, str]:
    """``(headline, explanation)`` for a page with nothing to show. The explanation says
    what the page counts, because "no closed transactions" is false for an account whose
    trades were placed by hand."""
    counts = ('This page counts closed transactions that were opened by an expert instance. '
              'Trades placed manually (including from Portfolio Allocation) are not recorded '
              'against an expert, so they never appear here.')
    if account_selected and not account_has_experts:
        return ('This account has no expert instances', counts)
    period = 'the selected period' if days == 3650 else f'the last {days} days'
    headline = f'No closed expert transactions in {period}'
    tail = ' Try a longer period' + (' or clear the expert filter.' if expert_filter_active else '.')
    return (headline, counts + tail)


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

_TEXT = '#e2e8f0'
_MUTED = '#a0aec0'
_HOVER = dict(bgcolor='#1a1f2e', font_size=12, font_color=_TEXT, bordercolor='#3d4a5c')


def _base_layout(height: int, layout: ChartLayout) -> Dict[str, Any]:
    return dict(
        height=height,
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font=dict(color=_MUTED, size=layout.tick_font),
        hoverlabel=_HOVER,
    )


def hbar_figure(names: Sequence[str], values: Sequence[float], colors: Dict[str, str],
                layout: ChartLayout, kind: str) -> go.Figure:
    """One horizontal bar per expert, in the order given (first on top), coloured by
    expert. ``kind`` is ``'money'`` or ``'days'``. The title lives in the page (HTML),
    so it wraps instead of being cut."""
    text = [format_money(v) if kind == 'money' else f'{format_days(v)} d' for v in values]
    height = bar_chart_height(len(names), layout)
    fig = go.Figure(go.Bar(
        x=list(values), y=list(names), orientation='h',
        marker=dict(color=[colors[n] for n in names], line=dict(width=0)),
        text=text, textposition='outside', cliponaxis=False,
        textfont=dict(color=_TEXT, size=layout.tick_font),
        hovertemplate='<b>%{y}</b><br>%{text}<extra></extra>',
    ))
    fig.update_layout(
        **_base_layout(height, layout), showlegend=False, bargap=0.25,
        margin=dict(l=10, r=10, t=8, b=34),
        xaxis=dict(range=padded_range(values, layout.value_pad_frac),
                   gridcolor='rgba(160,174,192,0.15)', zerolinecolor='rgba(160,174,192,0.4)',
                   tickfont=dict(size=layout.tick_font - 1), fixedrange=True,
                   tickformat='$,.0f' if kind == 'money' else None,
                   ticksuffix=' d' if kind == 'days' else ''),
        yaxis=dict(autorange='reversed', automargin=True, fixedrange=True,
                   tickmode='array', tickvals=list(names),
                   ticktext=shorten_labels(names, layout.label_max_chars),
                   tickfont=dict(size=layout.tick_font, color=_TEXT)),
    )
    return fig


def win_loss_figure(names: Sequence[str], wins: Sequence[int], losses: Sequence[int],
                    layout: ChartLayout) -> go.Figure:
    """Stacked horizontal bar per expert: wins green, losses red, counts inside the
    segments and the win rate after the bar."""
    height = bar_chart_height(len(names), layout) + 26   # + the legend row
    totals = [w + l for w, l in zip(wins, losses)]
    rates = [(w / t * 100.0) if t else None for w, t in zip(wins, totals)]
    fig = go.Figure()
    for label, series, color in (('Wins', wins, WIN_COLOR), ('Losses', losses, LOSS_COLOR)):
        fig.add_trace(go.Bar(
            x=list(series), y=list(names), orientation='h', name=label,
            marker=dict(color=color, line=dict(width=0)),
            text=[str(v) if v else '' for v in series], textposition='inside',
            insidetextanchor='middle', textfont=dict(color='#10141f', size=layout.tick_font),
            hovertemplate=f'<b>%{{y}}</b><br>{label}: %{{x}}<extra></extra>',
        ))
    fig.update_layout(
        **_base_layout(height, layout), barmode='stack', bargap=0.25,
        margin=dict(l=10, r=10, t=8, b=62),
        xaxis=dict(range=[0, max(totals + [1]) * (1 + layout.value_pad_frac + 0.05)],
                   gridcolor='rgba(160,174,192,0.15)', fixedrange=True, rangemode='tozero',
                   tickfont=dict(size=layout.tick_font - 1)),
        yaxis=dict(autorange='reversed', automargin=True, fixedrange=True,
                   tickmode='array', tickvals=list(names),
                   ticktext=shorten_labels(names, layout.label_max_chars),
                   tickfont=dict(size=layout.tick_font, color=_TEXT)),
        legend=dict(orientation='h', x=0, xanchor='left', yanchor='top',
                    y=-(34 / max(1, height - 8 - 62)),
                    font=dict(color=_MUTED, size=10), bgcolor='rgba(0,0,0,0)'),
        annotations=[dict(x=t, y=n, xref='x', yref='y', xanchor='left', showarrow=False,
                          xshift=6, text=(f'{r:.0f}%' if r is not None else '—'),
                          font=dict(color=_TEXT, size=layout.tick_font))
                     for n, t, r in zip(names, totals, rates)],
    )
    return fig


def monthly_line_figure(series: Dict[str, List[Tuple[datetime, Optional[float]]]],
                        order: Sequence[str], colors: Dict[str, str], layout: ChartLayout,
                        value_kind: str, show_legend: bool = True,
                        reverse_y: bool = False, y_title: str = '') -> go.Figure:
    """One line per expert over the months. The legend sits BELOW the plot, in as many
    rows as the names need; the figure's bottom margin is sized to that, so nothing
    overlaps the plot or the title (which is HTML, outside the figure).
    ``value_kind``: ``'money'``, ``'count'`` or ``'pct'``."""
    names = [n for n in order if n in series and series[n]]
    hover_val = {'money': '%{y:$,.2f}', 'count': '%{y:.0f}', 'pct': '%{y:.1f}%'}[value_kind]
    fig = go.Figure()
    for name in names:
        pts = series[name]
        color = colors[name]
        fig.add_trace(go.Scatter(
            x=[p[0] for p in pts], y=[p[1] for p in pts], mode='lines+markers', name=name,
            line=dict(color=color, width=2), marker=dict(size=6, color=color),
            connectgaps=False,
            hovertemplate=f'<b>{name}</b><br>%{{x|%b %Y}}: {hover_val}<extra></extra>'))
    rows = legend_rows(names, layout.legend_width_px) if show_legend else 0
    legend_px = rows * 18
    margin_t, margin_b = 10, 40 + legend_px
    height = layout.line_height + margin_t + margin_b
    yaxis = dict(gridcolor='rgba(160,174,192,0.2)', zerolinecolor='rgba(160,174,192,0.4)',
                 tickfont=dict(size=layout.tick_font - 1), fixedrange=True,
                 tickformat='$,.0f' if value_kind == 'money' else None,
                 ticksuffix='%' if value_kind == 'pct' else '')
    if value_kind == 'count':
        yaxis['rangemode'] = 'tozero'
    if y_title:
        yaxis['title'] = dict(text=y_title, font=dict(size=layout.tick_font - 1, color=_MUTED),
                              standoff=6)
    if reverse_y:
        yaxis['autorange'] = 'reversed'
        yaxis['rangemode'] = 'tozero'
    fig.update_layout(
        **_base_layout(height, layout), showlegend=show_legend, hovermode='closest',
        margin=dict(l=10, r=14, t=margin_t, b=margin_b),
        xaxis=dict(tickformat='%b %y', dtick=layout.month_dtick, fixedrange=True,
                   gridcolor='rgba(160,174,192,0.12)', tickfont=dict(size=layout.tick_font - 1)),
        yaxis={**yaxis, 'automargin': True},
        legend=dict(orientation='h', x=0, xanchor='left', yanchor='top',
                    y=-(34 / max(1, layout.line_height)),
                    font=dict(color=_MUTED, size=10), bgcolor='rgba(0,0,0,0)',
                    tracegroupgap=0),
    )
    return fig


# ---------------------------------------------------------------------------
# Monthly series
# ---------------------------------------------------------------------------

def monthly_series(monthly_data: Dict[str, Dict[str, Dict[str, float]]]):
    """``{month: {expert: {'pnl', 'count'}}}`` -> ``(profit, count, drawdown)`` series dicts
    ``{expert: [(month_start, value), ...]}`` over every month in the data.

    Drawdown is a property of the CUMULATIVE curve, so it is walked month by month rather
    than derived from each month's P&L in isolation: a month that made money can still leave
    the expert further below its peak. The value is in DOLLARS below the running peak of
    cumulative P&L (peak starts at 0, as in ``max_drawdown_from_pnl``): a percentage of that
    peak explodes when the peak is tiny, so none is computed. An expert that was never in
    drawdown has no drawdown series at all.
    """
    months = sorted(monthly_data)
    names = set()
    for per_expert in monthly_data.values():
        names.update(per_expert)
    profit: Dict[str, list] = {}
    count: Dict[str, list] = {}
    drawdown: Dict[str, list] = {}
    for name in names:
        p_pts, c_pts, d_pts = [], [], []
        cumulative = peak = 0.0
        for month in months:
            month_date = datetime.strptime(month, '%Y-%m')
            cell = monthly_data[month].get(name, {'pnl': 0, 'count': 0})
            p_pts.append((month_date, cell['pnl']))
            c_pts.append((month_date, cell['count']))
            cumulative += cell['pnl']
            peak = max(peak, cumulative)
            d_pts.append((month_date, round(peak - cumulative, 2)))
        profit[name], count[name] = p_pts, c_pts
        if any(v for _d, v in d_pts):
            drawdown[name] = d_pts
    return profit, count, drawdown
