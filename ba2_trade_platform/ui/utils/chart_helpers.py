"""Shared chart affordances: the $/% conversion, a legend that cannot overlap, fullscreen.

Three things every chart panel on the Overview page wants, written once so a fourth
panel gets them by asking rather than by copying eighty lines of ECharts options.

THE % CONVERSION IS A RATIO OF INVESTED CAPITAL, not a rebase to the first point. A
portfolio takes contributions: rebasing to day one makes a label that doubled its
capital look identical to one that doubled its return. Dividing by the invested capital
AT EACH DATE answers the question actually being asked -- "what is this worth against
what I put in" -- and it makes the Invested series itself a flat 100% line, which is
exactly the reference the eye needs to read the others against.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from nicegui import ui

#: y-axis label formats for the two modes.
AXIS_FORMAT_MONEY = '${value}'
AXIS_FORMAT_PCT = '{value}%'

#: How many legend entries fit on one row before ECharts must paginate it. Below this a
#: plain legend is nicer (no arrows); above it, an un-paginated legend wraps onto extra
#: rows and grows DOWNWARD into the plot -- which is the overlap that was reported, with
#: 30 entries covering the top gridline and the "$1,800" label beside it.
LEGEND_SCROLL_THRESHOLD = 8


def pct_of_invested(values: Sequence[Optional[float]],
                    invested: Sequence[Optional[float]]) -> List[Optional[float]]:
    """``values`` as a percentage of ``invested``, point by point. Pure.

    ``None`` -- a GAP in the line, never 0.0 -- wherever the invested capital is zero,
    absent or unmeasurable. Before a label's first purchase there is no denominator, and
    a 0% there would draw a flat line along the axis claiming the position existed and
    returned nothing. ECharts renders ``None`` as a break, which is the truth.

    A shorter ``invested`` than ``values`` is padded with ``None`` rather than raising:
    these come from parallel per-date accumulations and a mismatch means one series
    started later, which is a gap, not an error.
    """
    out: List[Optional[float]] = []
    for i, value in enumerate(values):
        denom = invested[i] if i < len(invested) else None
        if value is None or denom is None or not denom:
            out.append(None)
        else:
            out.append(round(float(value) / float(denom) * 100.0, 2))
    return out


def axis_format(is_pct: bool) -> str:
    """The y-axis ``formatter`` for the current mode."""
    return AXIS_FORMAT_PCT if is_pct else AXIS_FORMAT_MONEY


def legend_options(legend_data: Sequence[str], *, top: int = 5) -> dict:
    """A legend that never grows into the plot.

    Past ``LEGEND_SCROLL_THRESHOLD`` entries it becomes ECharts' SCROLL legend: one row
    with page arrows, a fixed height whatever the entry count. The alternative -- letting
    it wrap -- is what put four rows of series names over the top of the chart.

    ``type: 'scroll'`` is deliberately NOT applied unconditionally: on a three-series
    chart it adds arrows to a legend that already fits, and a control that does nothing
    is worse than no control.
    """
    options = {
        'data': list(legend_data),
        'textStyle': {'color': '#a0aec0'},
        'top': top,
    }
    if len(legend_data) > LEGEND_SCROLL_THRESHOLD:
        options['type'] = 'scroll'
        # Paginator in the same muted grey as the labels; the ECharts default is a
        # near-black that vanishes on this background.
        options['pageTextStyle'] = {'color': '#a0aec0'}
        options['pageIconColor'] = '#a0aec0'
        options['pageIconInactiveColor'] = '#4a5568'
    return options


def grid_options(legend_data: Sequence[str]) -> dict:
    """Plot area, pushed down far enough to clear the legend above it.

    Paired with ``legend_options`` and reading the same list, because the two numbers
    have to agree: a scroll legend is one row and needs ~40px, a plain one may wrap and
    needs room per row. Computing this from the same input is what stops them drifting.
    """
    rows = 1 if len(legend_data) > LEGEND_SCROLL_THRESHOLD else max(
        1, (len(legend_data) + 3) // 4)
    return {'left': '3%', 'right': '3%', 'bottom': '3%',
            'top': 30 + rows * 18, 'containLabel': True}


def mode_toggle() -> "ui.toggle":
    """The $ / % switch, in the shape the monthly charts already use."""
    return ui.toggle(['$', '%'], value='$').props('dense')


def fullscreen_button(build_options: Callable[[], dict], *, title: str) -> "ui.button":
    """An expand control that reopens THIS chart, as it currently is, maximised.

    ``build_options`` is a zero-argument callable rather than a options dict, and that is
    the whole design: it is re-invoked when the button is pressed, so the full-screen
    copy reflects whatever the panel's toggles, label pickers and $/% switch say AT THAT
    MOMENT. Passing the dict would freeze a snapshot taken at page build and quietly
    show a stale chart.

    A ``maximized`` dialog rather than a browser fullscreen request: the latter needs a
    user-gesture-scoped API call and drops the app's own styling, and it cannot host the
    close button.
    """
    def _open() -> None:
        with ui.dialog().props('maximized') as dialog, \
                ui.card().classes('w-full h-full flex flex-col'):
            with ui.row().classes('w-full items-center justify-between shrink-0'):
                ui.label(title).classes('text-lg font-bold')
                ui.button(icon='close', on_click=dialog.close).props('flat round dense')
            # The chart is built INSIDE the open dialog so ECharts initialises into the
            # box it will live in, rather than into a small one it then has to grow out
            # of -- the same reason SymbolInfoPanel builds its chart at render time.
            ui.echart(build_options()).classes('w-full grow').style('min-height: 0')
        dialog.open()

    return ui.button(icon='fullscreen', on_click=_open).props('flat round dense') \
        .tooltip('Open this chart full screen')
