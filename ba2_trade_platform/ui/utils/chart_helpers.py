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

    ``None`` -- a GAP in the line, never 0.0 -- wherever the invested capital is absent,
    zero OR NEGATIVE. Before a label's first purchase there is no denominator, and a 0%
    there would draw a flat line along the axis claiming the position existed and
    returned nothing. ECharts renders ``None`` as a break, which is the truth.

    NEGATIVE IS THE ONE THAT BIT. A cost accumulator that reduces on a sale by the sale
    PROCEEDS rather than by the cost of the shares sold goes below zero the moment a
    profitable liquidation happens -- a margin call, in the reported case -- and
    ``value / -8 * 100`` is a perfectly finite -1,200% that rescales the whole chart and
    flattens every other series into the axis. A percentage OF a negative capital base
    is not a small number or a large one; it is not a quantity at all.

    A shorter ``invested`` than ``values`` is padded with ``None`` rather than raising:
    these come from parallel per-date accumulations and a mismatch means one series
    started later, which is a gap, not an error.
    """
    out: List[Optional[float]] = []
    for i, value in enumerate(values):
        denom = invested[i] if i < len(invested) else None
        if value is None or denom is None or float(denom) <= 0:
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


def label_series_colors(labels: Sequence[str], palette: Sequence[str],
                        stored: Optional[dict] = None) -> List[str]:
    """One colour per label: the one CHOSEN on Portfolio Allocation, else the palette.

    A label the user has deliberately coloured should look the same everywhere it is
    drawn. Account Growth cycled a fixed ten-colour list by POSITION instead, so the
    same label was blue on one page and orange on the other -- and, worse, changed
    colour on Account Growth itself as soon as a label was added above it in the sort.

    Falling back POSITIONALLY for the rest is deliberate: an uncoloured label has no
    opinion, and the palette's job is only to keep neighbouring lines distinguishable.

    ``stored`` maps label -> whatever the database holds; anything unparseable falls
    through to the palette rather than reaching a CSS value, exactly as
    ``resolve_label_icon_color`` decides for the icon.
    """
    from .portfolio_allocation_view import (DEFAULT_LABEL_ICON_COLOR,
                                            resolve_label_icon_color)
    stored = stored or {}
    out: List[str] = []
    for i, label in enumerate(labels):
        raw = stored.get(label)
        chosen = resolve_label_icon_color(raw) if raw else None
        # The resolver answers the NEUTRAL GREY for anything it cannot parse. That is
        # the right answer for an icon and the wrong one for a chart, where several
        # unparseable labels would become several identical grey lines.
        if chosen and chosen != DEFAULT_LABEL_ICON_COLOR:
            out.append(chosen)
        else:
            out.append(palette[i % len(palette)])
    return out


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
        options = build_options()
        if not options:
            # An async panel whose data has not arrived yet. Saying so beats opening a
            # full-screen empty box, which reads as a broken chart rather than as one
            # that is still loading.
            ui.notify('That chart has nothing to show yet.', type='info')
            return
        with ui.dialog().props('maximized') as dialog, \
                ui.card().classes('w-full h-full flex flex-col'):
            with ui.row().classes('w-full items-center justify-between shrink-0'):
                ui.label(title).classes('text-lg font-bold')
                ui.button(icon='close', on_click=dialog.close).props('flat round dense')
            # The chart is built INSIDE the open dialog so ECharts initialises into the
            # box it will live in, rather than into a small one it then has to grow out
            # of -- the same reason SymbolInfoPanel builds its chart at render time.
            ui.echart(options).classes('w-full grow').style('min-height: 0')
        dialog.open()

    return ui.button(icon='fullscreen', on_click=_open).props('flat round dense') \
        .tooltip('Open this chart full screen')


def fullscreen_content_button(render: Callable[[], None], *, title: str) -> "ui.button":
    """The same control for a panel that is NOT an ECharts option dict.

    ``render`` draws the panel's body into whatever container is current, so it works
    for a table, a grid of cards, or anything else. It is a callable for the same
    reason ``fullscreen_button``'s is: it is invoked on click, so the full-screen copy
    is built from the data as it stands rather than from a snapshot.

    Separate from ``fullscreen_button`` rather than a mode of it, because the two take
    genuinely different things -- one returns options, the other draws -- and a single
    function switching on the return type would be the kind of cleverness that makes a
    caller check the source to find out which contract it is under.
    """
    def _open() -> None:
        with ui.dialog().props('maximized') as dialog, \
                ui.card().classes('w-full h-full flex flex-col'):
            with ui.row().classes('w-full items-center justify-between shrink-0'):
                ui.label(title).classes('text-lg font-bold')
                ui.button(icon='close', on_click=dialog.close).props('flat round dense')
            with ui.column().classes('w-full grow overflow-auto'):
                render()
        dialog.open()

    return ui.button(icon='fullscreen', on_click=_open).props('flat round dense') \
        .tooltip(f'Open {title} full screen')
