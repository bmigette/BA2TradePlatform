"""Pure axis / series decisions for the Account Growth charts (no NiceGUI, no DB).

* ``month_axis``: ONE helper for the monthly bar charts' x axis -- contiguous months from the
  range start to the last month with an actual or forecast value (never an empty trailing month).
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
    End: the LATEST of the current month, the last month holding a forecast payment (the
    forecast already covers only today .. today + 2 months, so a payment on 2 Dec shows a
    forecast-only December column) and the last actual month. Never a trailing month with
    nothing in it; months in between with no data stay on the axis (a gap is information).
    """
    actual = sorted({m for m in actual_months})
    fc = sorted({m for m in forecast_months})
    if isinstance(today, datetime):
        today = today.date()
    end = max([month_key(today)] + fc + actual)
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


TOTAL_NAME = 'Total (shown labels, each symbol once)'


def shown_label_totals(months: Sequence[str], shown: Iterable[str],
                       by_symbol: Dict[str, Dict[str, float]],
                       labels_of: Dict[str, Sequence[str]]) -> Dict[str, Any]:
    """Per month: ``totals`` = realized P&L + dividends of the DISTINCT symbols that carry at
    least one shown label (each symbol counted ONCE however many shown labels it has),
    ``not_shown`` = what the other symbols made, and ``overlap`` = does any symbol with a value
    carry two or more shown labels (then a per-label stack cannot add up). Pure.

    ``by_symbol``: ``{month: {symbol: value}}``; ``labels_of``: ``{symbol: [labels]}`` (a symbol
    without labels is 'Unlabeled'). Months with no row give ``None``.
    """
    shown_set = set(shown)
    totals: List[Optional[float]] = []
    not_shown: List[Optional[float]] = []
    overlap = False
    for m in months:
        row = by_symbol.get(m)
        if not row:
            totals.append(None)
            not_shown.append(None)
            continue
        t = n = 0.0
        for sym, v in row.items():
            labs = labels_of.get(sym) or ['Unlabeled']
            k = sum(1 for lb in labs if lb in shown_set)
            if k >= 2 and v:
                overlap = True
            if k >= 1:
                t += v
            else:
                n += v
        totals.append(round(t, 2))
        not_shown.append(round(n, 2))
    return {'totals': totals, 'not_shown': not_shown, 'overlap': overlap}


def stacked_month_series(months: Sequence[str], labels: Sequence[str],
                         cell: Callable[[str, str], Optional[float]],
                         forecast_cell: Callable[[str, str], Optional[float]],
                         color_of: Callable[[str], str],
                         faded_of: Callable[[str], str],
                         include_forecast: bool = True,
                         stacked: bool = True,
                         totals: Optional[Sequence[Optional[float]]] = None) -> List[dict]:
    """One bar series per label (+ a transparent dashed forecast segment per label) and a
    Total marker per month.

    ``stacked=True``: all labels share one ``samesign`` stack (positives up, negatives down) --
    only honest when the shown labels are DISJOINT. ``stacked=False``: labels sit side by side,
    each label's forecast stacked on its own bar. ``totals`` is the Total marker (see
    :func:`shown_label_totals`: distinct symbols); without it the marker is the sum of the
    ACTUAL segments. The forecast is never part of the Total."""
    series: List[dict] = []
    sums = [0.0 for _ in months]
    any_sum = [False for _ in months]
    for lb in labels:
        data = [cell(m, lb) for m in months]
        for i, v in enumerate(data):
            if v is not None:
                sums[i] += v
                any_sum[i] = True
        s = {'name': lb, 'type': 'bar', 'stack': 'labels' if stacked else lb, 'data': data,
             'itemStyle': {'color': color_of(lb)}, 'barMaxWidth': 46}
        if stacked:
            s['stackStrategy'] = 'samesign'
        series.append(s)
    if include_forecast:
        for lb in labels:
            data = [forecast_cell(m, lb) for m in months]
            if any(v is not None for v in data):
                s = {'name': f'{lb} (forecast, estimated)', 'type': 'bar',
                     'stack': 'labels' if stacked else lb, 'data': data, 'barMaxWidth': 46,
                     'itemStyle': {'color': faded_of(lb), 'borderColor': color_of(lb),
                                   'borderType': 'dashed', 'borderWidth': 1}}
                if stacked:
                    s['stackStrategy'] = 'samesign'
                series.append(s)
    marker = (list(totals) if totals is not None
              else [round(t, 2) if any_sum[i] else None for i, t in enumerate(sums)])
    series.append({'name': TOTAL_NAME, 'type': 'line', 'symbol': 'diamond', 'symbolSize': 9,
                   'lineStyle': {'width': 0}, 'itemStyle': {'color': '#f59e0b', 'borderColor': '#fff',
                                                             'borderWidth': 1},
                   'z': 10, 'data': marker})
    return series


def stacked_tooltip_js(pct: bool = False, not_shown: Optional[Sequence[Optional[float]]] = None) -> str:
    """Tooltip: every non-zero label entry sorted by magnitude, then the distinct-symbol Total and,
    when the shown labels do not cover everything the account made that month, a
    'Not shown (other labels)' line."""
    fmt = ("v => Number(v).toFixed(2) + '%'" if pct
           else "v => (v < 0 ? '-$' : '$') + Math.abs(v).toFixed(2)")
    ns = json.dumps(list(not_shown) if not_shown else [])
    return (
        "(params) => { const rows = params.filter(p => p.value !== null && p.value !== undefined"
        " && p.seriesName !== " + json.dumps(TOTAL_NAME) + " && Number(p.value) !== 0)"
        ".sort((a, b) => Math.abs(b.value) - Math.abs(a.value));"
        f" const fmt = {fmt};"
        " let out = '<b>' + params[0].axisValueLabel + '</b>';"
        " rows.forEach(p => { out += '<br/>' + p.marker + p.seriesName + ': ' + fmt(Number(p.value)); });"
        " const t = params.find(p => p.seriesName === " + json.dumps(TOTAL_NAME) + ");"
        " if (t && t.value !== null && t.value !== undefined) out += '<br/><b>' + t.seriesName + ': ' + fmt(Number(t.value)) + '</b>';"
        f" const ns = {ns}[params[0].dataIndex];"
        " if (ns !== null && ns !== undefined && Math.abs(ns) >= 0.005) out += '<br/>Not shown (other labels): ' + fmt(Number(ns));"
        " return out; }"
    )
