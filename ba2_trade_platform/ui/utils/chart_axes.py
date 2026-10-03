"""Pure axis / series decisions for the Account Growth charts (no NiceGUI, no DB).

* ``month_axis``: ONE helper for the monthly bar charts' x axis -- contiguous months from the
  range start to the current month plus the next one (never an empty December).
* ``tick_plan`` / ``date_axis``: readable date axes for the daily line charts -- week ticks
  for a short span, month ticks for a year, quarter / year ticks beyond -- horizontal labels,
  tooltips stay daily.
* ``null_before_start``: no line before a position existed (None, not 0).
* ``stacked_month_series``: per-label bars STACKED per month (positives up, negatives down)
  plus a Total marker; the same numbers as the old side-by-side bars, only laid out differently.
"""
import json
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

#: Month-chart forecast horizon shown: the current month and the next one.
FORECAST_MONTHS_AHEAD = 1

#: Dividends / Invested lines are drawn for at most this many selected labels (readability).
MAX_DETAIL_LABELS = 6


# --------------------------------------------------------------------------------------------
# month axis
# --------------------------------------------------------------------------------------------

def month_key(d) -> str:
    if isinstance(d, datetime):
        d = d.date()
    return f'{d.year:04d}-{d.month:02d}'


def shift_month(key: str, n: int) -> str:
    y, m = int(key[:4]), int(key[5:7])
    total = y * 12 + (m - 1) + n
    y, m = divmod(total, 12)
    return f'{y:04d}-{m + 1:02d}'


def month_axis(actual_months: Iterable[str], forecast_months: Iterable[str],
               range_start: Optional[date], today: date) -> List[str]:
    """Contiguous ``YYYY-MM`` keys for the monthly bar charts.

    Start: the range start's month, or (no range start = Max) the first month with data.
    End: the current month plus ``FORECAST_MONTHS_AHEAD`` -- today's Oct + Nov, never an empty
    Dec -- or the last actual month if that is later. Months in between with no data stay on
    the axis (a gap is information).
    """
    actual = sorted({m for m in actual_months})
    fc = sorted({m for m in forecast_months})
    if isinstance(today, datetime):
        today = today.date()
    end = shift_month(month_key(today), FORECAST_MONTHS_AHEAD)
    if actual:
        end = max(end, actual[-1])
    if range_start is not None:
        start = month_key(range_start)
    else:
        pool = actual + fc
        start = pool[0] if pool else month_key(today)
    if start > end:
        start = end
    out, cur = [], start
    while cur <= end:
        out.append(cur)
        cur = shift_month(cur, 1)
    return out


def clip_forecast(forecast: Dict[str, Any], axis: Sequence[str]) -> Dict[str, Any]:
    """Forecast months that are on the axis."""
    on = set(axis)
    return {m: v for m, v in (forecast or {}).items() if m in on}


# --------------------------------------------------------------------------------------------
# date axis for the daily line charts
# --------------------------------------------------------------------------------------------

def _d(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def tick_plan(dates: Sequence[str]) -> Tuple[str, List[int]]:
    """``(mode, indices)``: which category indices carry a label.

    Span <= 100 days: the first date of each ISO week ('week'); <= 800 days: first date of each
    month ('month'); <= 2200: each quarter ('quarter'); else each year ('year').
    """
    if not dates:
        return 'month', []
    span = (_d(dates[-1]) - _d(dates[0])).days
    if span <= 100:
        mode = 'week'
    elif span <= 800:
        mode = 'month'
    elif span <= 2200:
        mode = 'quarter'
    else:
        mode = 'year'
    idx, seen = [], None
    for i, s in enumerate(dates):
        d = _d(s)
        if mode == 'week':
            key = d.isocalendar()[:2]
        elif mode == 'month':
            key = (d.year, d.month)
        elif mode == 'quarter':
            key = (d.year, (d.month - 1) // 3)
        else:
            key = d.year
        if key != seen:
            idx.append(i)
            seen = key
    return mode, idx


_FORMATTERS = {
    'week': "(v) => v.slice(5)",
    'month': "(v) => ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+v.slice(5,7)-1] + ' ' + v.slice(2,4)",
    'quarter': "(v) => ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+v.slice(5,7)-1] + ' ' + v.slice(2,4)",
    'year': "(v) => v.slice(0,4)",
}


def date_axis(dates: Sequence[str], font_size: int = 11) -> dict:
    """An ECharts x axis over daily categories with readable, horizontal labels.

    Labels sit only at the tick indices (see :func:`tick_plan`); ``':interval'`` and
    ``':formatter'`` are NiceGUI dynamic (JS) properties. The category data stays daily so
    tooltips and series keep one point per day.
    """
    mode, idx = tick_plan(dates)
    return {
        'type': 'category',
        'data': list(dates),
        'boundaryGap': False,
        'axisLabel': {'color': '#a0aec0', 'rotate': 0, 'fontSize': font_size, 'hideOverlap': True,
                      ':interval': f"(i) => {json.dumps(idx)}.includes(i)",
                      ':formatter': _FORMATTERS[mode]},
        'axisTick': {'show': False},
        'axisLine': {'lineStyle': {'color': 'rgba(255, 255, 255, 0.1)'}},
    }


# --------------------------------------------------------------------------------------------
# series helpers
# --------------------------------------------------------------------------------------------

def first_holding_index(values: Sequence[Optional[float]]) -> int:
    """Index of the first non-zero, non-None value (``len`` if there is none)."""
    for i, v in enumerate(values):
        if v is not None and v != 0:
            return i
    return len(values)


def null_before_start(values: Sequence[Optional[float]], start: int) -> List[Optional[float]]:
    """``values`` with everything before ``start`` replaced by None (no line before the
    position existed instead of a flat 0 that then jumps)."""
    return [None if i < start else v for i, v in enumerate(values)]


def detail_labels(visible: Sequence[str], cap: int = MAX_DETAIL_LABELS) -> Tuple[List[str], int]:
    """``(labels that get Dividends/Invested lines, how many were left out)``."""
    vis = list(visible)
    return vis[:cap], max(0, len(vis) - cap)


def stacked_month_series(months: Sequence[str], labels: Sequence[str],
                         cell: Callable[[str, str], Optional[float]],
                         forecast_cell: Callable[[str, str], Optional[float]],
                         color_of: Callable[[str], str],
                         faded_of: Callable[[str], str],
                         include_forecast: bool = True) -> List[dict]:
    """Bars stacked per month: positive values up, negative down (``samesign`` stacking), one
    series per label, a transparent dashed forecast segment per label on top of its actual,
    and a ``Total`` marker per month = the sum of the ACTUAL values only (forecast is never
    mixed into it)."""
    series: List[dict] = []
    totals = [0.0 for _ in months]
    any_total = [False for _ in months]
    for lb in labels:
        data = [cell(m, lb) for m in months]
        for i, v in enumerate(data):
            if v is not None:
                totals[i] += v
                any_total[i] = True
        series.append({'name': lb, 'type': 'bar', 'stack': 'labels', 'stackStrategy': 'samesign',
                       'data': data, 'itemStyle': {'color': color_of(lb)}, 'barMaxWidth': 46})
    if include_forecast:
        for lb in labels:
            data = [forecast_cell(m, lb) for m in months]
            if any(v is not None for v in data):
                series.append({'name': f'{lb} (forecast, estimated)', 'type': 'bar', 'stack': 'labels',
                               'stackStrategy': 'samesign', 'data': data, 'barMaxWidth': 46,
                               'itemStyle': {'color': faded_of(lb), 'borderColor': color_of(lb),
                                             'borderType': 'dashed', 'borderWidth': 1}})
    series.append({'name': 'Total', 'type': 'line', 'symbol': 'diamond', 'symbolSize': 9,
                   'lineStyle': {'width': 0}, 'itemStyle': {'color': '#f59e0b', 'borderColor': '#fff',
                                                             'borderWidth': 1},
                   'z': 10,
                   'data': [round(t, 2) if any_total[i] else None for i, t in enumerate(totals)]})
    return series


def stacked_tooltip_js(pct: bool = False) -> str:
    """Tooltip for the stacked chart: every non-zero entry sorted by magnitude, ``Total`` last."""
    fmt = ("v => Number(v).toFixed(2) + '%'" if pct
           else "v => (v < 0 ? '-$' : '$') + Math.abs(v).toFixed(2)")
    return (
        "(params) => { const rows = params.filter(p => p.value !== null && p.value !== undefined"
        " && p.seriesName !== 'Total' && Number(p.value) !== 0)"
        ".sort((a, b) => Math.abs(b.value) - Math.abs(a.value));"
        f" const fmt = {fmt};"
        " let out = '<b>' + params[0].axisValueLabel + '</b>';"
        " rows.forEach(p => { out += '<br/>' + p.marker + p.seriesName + ': ' + fmt(Number(p.value)); });"
        " const t = params.find(p => p.seriesName === 'Total');"
        " if (t && t.value !== null && t.value !== undefined) out += '<br/><b>Total: ' + fmt(Number(t.value)) + '</b>';"
        " return out; }"
    )
