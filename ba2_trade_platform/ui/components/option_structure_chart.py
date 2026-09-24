"""The live option popup's price chart: candles, strikes, breakeven, zones, markers.

Spec 2026-09-20, steps 9 and 10, decision 2c. Redesigned 2026-09-24 to match the test
platform's trade popup.

**No rotated payoff curve.** It used to be drawn on a second x-axis overlaying the time axis
(P&L across, price up), so the horizontal axis meant dates for the candles and dollars for the
curve at once, its $0 line read as a date, and the operator could not tell what the diagonal
line was. What the chart keeps is what a TIME chart can say about the payoff: the strike
lines, a labelled "Breakeven at expiry" line, and faint profit/loss zones either side of it.
The figures (breakeven, max profit, max loss) stay in the popup's payoff section.

The figure is built by a PURE function so it can be asserted trace-by-trace in a test --
the alternative (only checking it by eye in a browser) is how a chart ends up drawing the
wrong axis for a month.

What it deliberately does NOT do: no option-premium history, no Greeks, no IV. Those are
contract detail (see ``live_trades._render_option_structure_section``), and the payoff curve
is derived from recorded terms alone.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ba2_common.core.option_payoff_chart import (
    PayoffChart, PayoffUnavailable, sample_curve, sign_segments, zone_bands,
)

logger = logging.getLogger(__name__)

CHART_HEIGHT = 420
PROFIT_COLOR = '#16a34a'
LOSS_COLOR = '#dc2626'
BREAKEVEN_COLOR = '#eab308'
GRID_COLOR = '#1f2937'
AXIS_TEXT_COLOR = '#cbd5e1'
#: Plotly's own toolbar (zoom, pan, lasso, download) off: none of it is useful on this popup.
PLOTLY_CONFIG = {'displayModeBar': False, 'scrollZoom': False, 'doubleClick': False}
#: What the zones and the breakeven line mean, shown under the chart (same words as the test
#: platform's popup).
CHART_LEGEND = ('Yellow line: breakeven at expiration. Green/red: where holding to '
                'expiration would end in profit/loss. An early exit is priced off the '
                "option's premium (time value included), so it can profit in the red zone. "
                'Markers are labels on the bar, not price levels.')
STRUCTURE_MARKER_COLOR = '#7c3aed'
LEG_MARKER_COLOR = '#0891b2'


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN check


def normalise_bars(bars: Any) -> List[Dict[str, Any]]:
    """Accept a DataFrame (provider shape) or a list of dicts; return plain dicts.

    A bar missing any of OHLC is DROPPED rather than zero-filled: a candle drawn at 0 is a
    lie about the price, and the chart is perfectly readable with a gap.
    """
    if bars is None:
        return []
    records: Iterable[Any]
    if hasattr(bars, 'to_dict'):
        records = bars.to_dict('records')
    else:
        records = bars

    out: List[Dict[str, Any]] = []
    for record in records:
        stamp = None
        for key in ('date', 'Date', 'effective_date', 'time'):
            if key in record and record[key] is not None:
                stamp = record[key]
                break
        if stamp is None:
            continue
        if isinstance(stamp, (datetime, date)):
            label = stamp.strftime('%Y-%m-%d')
        else:
            label = str(stamp)[:10]
        values = {
            'open': _as_float(record.get('open', record.get('Open'))),
            'high': _as_float(record.get('high', record.get('High'))),
            'low': _as_float(record.get('low', record.get('Low'))),
            'close': _as_float(record.get('close', record.get('Close'))),
        }
        if any(value is None for value in values.values()):
            continue
        out.append({'date': label, **values})
    out.sort(key=lambda bar: bar['date'])
    return out


def chart_inputs_from(txn: Any, orders: Sequence[Any], bars: Any
                      ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(bars, strike_lines, markers)`` from the live transaction and its orders.

    Markers are the two sets decision 2c asked for:

    * **structure** -- the transaction's own open and close, which is what "when was this
      position on" means;
    * **leg** -- each order's ``created_at``, labelled as when the ORDER was placed. A live
      order carries no per-leg fill or exit timestamp (the model has no such column), so
      this says what it is rather than implying a fill price at that instant.
    """
    normalised = normalise_bars(bars)

    by_strike: Dict[float, List[Any]] = {}
    for order in orders or []:
        strike = _as_float(getattr(order, 'strike', None))
        if strike is None or strike <= 0:
            continue
        by_strike.setdefault(strike, []).append(order)

    strike_lines = []
    for strike in sorted(by_strike):
        group = by_strike[strike]
        parts = []
        for order in group:
            side = getattr(order, 'side', None)
            side = getattr(side, 'value', side)
            right = getattr(order, 'option_type', None)
            right = getattr(right, 'value', right)
            parts.append(f"{'Long' if str(side).upper().endswith('BUY') else 'Short'} "
                         f"{order.quantity:g} {str(right).capitalize()} · K ${strike:.2f}")
        strike_lines.append({'price': strike, 'label': ' / '.join(parts)})

    # Markers are anchored to the EVENT's OWN candle (review R3). Using the latest close for
    # every historical event put a Sep 8 entry on the Sep 11 close -- outside its own bar, and
    # with enough chart padding possibly a price from after the exit. The anchor is a VISUAL
    # position on that bar, not a quote: no entry price is implied by it.
    by_date = {bar['date']: bar for bar in normalised}

    def _anchor(day: str, entry: bool) -> Tuple[Optional[float], str]:
        """(price, session) for an event on ``day``: its own bar, else the prior session."""
        bar = by_date.get(day)
        session = 'on_date'
        if bar is None:
            earlier = [candidate for candidate in normalised if candidate['date'] < day]
            if not earlier:
                return None, 'missing'
            bar, session = earlier[-1], 'prior_session'
        # Below the candle for an entry, above it for an exit, so the two never sit on top of
        # each other on the same bar.
        return (bar['low'] if entry else bar['high']), session

    slots: Dict[Any, int] = {}
    chart_span = (max(bar['high'] for bar in normalised) - min(bar['low'] for bar in normalised)
                  if normalised else 0.0)

    def _marker(day: str, kind: str, text: str, entry: bool,
                short: Optional[str] = None) -> Optional[Dict[str, Any]]:
        price, session = _anchor(day, entry)
        if price is None:
            return None  # no session at or before the event: no marker rather than a wrong one

        # Same-bar collisions: each further marker on the same day/side steps away from the
        # candle, so a structure entry and its leg orders are readable instead of stacked on
        # one bar. The label stays SHORT on the canvas; the full sentence goes to hover.
        slot = slots.get((day, entry), 0)
        slots[(day, entry)] = slot + 1
        if slot:
            bar = by_date.get(day) or next(
                (candidate for candidate in reversed(normalised) if candidate['date'] < day), None)
            span = (bar['high'] - bar['low']) if bar else 0.0
            # At least 6% of the chart's own price range: a step sized off the candle alone
            # is invisible on a narrow bar, and "Entry" printed over the leg label.
            step = max(span * 0.5, chart_span * 0.06, abs(price) * 0.005, 0.05)
            price = price - slot * step if entry else price + slot * step

        return {
            'date': day, 'kind': kind, 'text': text if session == 'on_date'
                    else f'{text} (prior session)', 'price': price,
            'short': short or text,
            'anchor': 'event_bar_low' if entry else 'event_bar_high', 'session': session,
            'text_position': 'bottom center' if entry else 'top center',
        }

    markers: List[Dict[str, Any]] = []
    open_date = getattr(txn, 'open_date', None)
    if open_date is not None:
        marker = _marker(open_date.strftime('%Y-%m-%d'), 'structure', 'Structure entry', True,
                         short='Entry')
        if marker:
            markers.append(marker)
    close_date = getattr(txn, 'close_date', None)
    if close_date is not None:
        marker = _marker(close_date.strftime('%Y-%m-%d'), 'structure', 'Structure exit', False,
                         short='Exit')
        if marker:
            markers.append(marker)
    for order in orders or []:
        created = getattr(order, 'created_at', None)
        if created is None:
            continue
        side = getattr(order, 'side', None)
        side = getattr(side, 'value', side)
        right = getattr(order, 'option_type', None)
        right = getattr(right, 'value', right)
        premium = _as_float(getattr(order, 'open_price', None))
        is_long = str(side).upper().endswith('BUY')
        is_call = str(right).lower().startswith('call')
        marker = _marker(
            created.strftime('%Y-%m-%d'), 'leg',
            f"{'Long' if is_long else 'Short'} "
            f"{str(right).capitalize()} ${_as_float(getattr(order, 'strike', None)) or 0:.2f} "
            f"order placed" + ('' if premium is None else f' @ ${premium:.2f}/share'),
            True,
            short=f"{'L' if is_long else 'S'} {_as_float(getattr(order, 'strike', None)) or 0:.0f}"
                  f"{'C' if is_call else 'P'}",
        )
        if marker:
            markers.append(marker)

    # Chronological, so a same-day entry/exit pair is processed in a defined order.
    markers.sort(key=lambda marker: (marker['date'], marker['kind'] != 'structure'))
    return normalised, strike_lines, markers


def build_option_structure_figure(
    *,
    bars: Sequence[Dict[str, Any]],
    strike_lines: Sequence[Dict[str, Any]] = (),
    markers: Sequence[Dict[str, Any]] = (),
    payoff: Optional[Any] = None,
    show_overlay: bool = True,
    show_structure_markers: bool = True,
    show_leg_markers: bool = True,
    underlying: str = '',
    height: int = CHART_HEIGHT,
):
    """Build the figure. Pure: same inputs, same figure, no I/O.

    Green and red mean POSITION P&L at expiration and nothing else. Moneyness is not
    coloured, because ITM is not profitable.
    """
    import plotly.graph_objects as go

    figure = go.Figure()

    if bars:
        figure.add_trace(go.Candlestick(
            x=[bar['date'] for bar in bars],
            open=[bar['open'] for bar in bars],
            high=[bar['high'] for bar in bars],
            low=[bar['low'] for bar in bars],
            close=[bar['close'] for bar in bars],
            name=underlying or 'Underlying',
            increasing_line_color=PROFIT_COLOR, decreasing_line_color=LOSS_COLOR,
            showlegend=False,
        ))

    for line in strike_lines:
        figure.add_hline(
            y=line['price'], line_dash='dash', line_width=1, line_color='#64748b',
            # Inside the plot, above the line's right end: outside it, the label was cut off
            # by the figure edge ("Long 2" of "Long 2 Call · K $165.00").
            annotation_text=line.get('label', ''), annotation_position='top right',
            annotation_font_size=10,
        )

    chart = payoff if isinstance(payoff, PayoffChart) else None

    # Sign-only zones either side of the breakeven, at low opacity, and the breakeven itself
    # as a labelled line. Both follow the "Expiry payoff" toggle.
    price_bounds = _price_bounds(bars, strike_lines, chart)
    if show_overlay and chart is not None:
        bands = list(zone_bands(chart))
        for index, (sign, low, high) in enumerate(bands):
            # The outermost zones run to the edge of what is on screen (as the test platform's
            # do): clipped at the payoff's own sample range, "loss below breakeven" shaded a
            # thin strip by the strike and left the candles beneath it unshaded.
            if price_bounds is not None:
                if index == 0:
                    low = min(low, price_bounds[0])
                if index == len(bands) - 1:
                    high = max(high, price_bounds[1])
            figure.add_hrect(
                y0=low, y1=high, line_width=0,
                fillcolor=PROFIT_COLOR if sign == 'profit' else LOSS_COLOR,
                opacity=0.06,
            )
        for breakeven in chart.breakevens:
            if breakeven is None:
                continue
            figure.add_hline(
                y=breakeven, line_dash='dot', line_width=1.5, line_color=BREAKEVEN_COLOR,
                annotation_text=f'Breakeven at expiry ${breakeven:.2f}',
                annotation_position='top left', annotation_font_size=10,
                annotation_font_color=BREAKEVEN_COLOR,
            )

    for kind, enabled, colour in (
        ('structure', show_structure_markers, STRUCTURE_MARKER_COLOR),
        ('leg', show_leg_markers, LEG_MARKER_COLOR),
    ):
        selected = [m for m in markers if m.get('kind') == kind and m.get('date')]
        if not (enabled and selected):
            continue
        figure.add_trace(go.Scatter(
            x=[m['date'] for m in selected], y=[m.get('price') for m in selected],
            mode='markers+text', name='Structure' if kind == 'structure' else 'Leg orders',
            # Arrows like the test platform: up under the bar for an entry, down over it for
            # an exit.
            marker=dict(symbol=['triangle-up' if m.get('anchor') == 'event_bar_low'
                                else 'triangle-down' for m in selected],
                        size=12 if kind == 'structure' else 9, color=colour),
            # Short label on the canvas (long labels overlap into an unreadable smear),
            # full sentence on hover.
            text=[m.get('short') or m.get('text', '') for m in selected],
            textposition=[m.get('text_position', 'top center') for m in selected],
            textfont=dict(size=9),
            hovertext=[m.get('text', '') for m in selected], hoverinfo='text',
        ))

    figure.update_layout(
        height=height,
        template='plotly_dark',
        # Transparent: the popup's card shows through, instead of plotly_dark's own near-black
        # panel sitting inside it (the test platform's chart does the same).
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color=AXIS_TEXT_COLOR),
        margin=dict(l=40, r=40, t=20, b=30),
        # A fixed view: drag-to-zoom on a popup chart mostly happens by accident, and the
        # window already spans the position's life. (The toolbar is hidden in PLOTLY_CONFIG.)
        dragmode=False,
        # Weekends skipped: daily bars have none, and the gaps read as missing data.
        xaxis=dict(title={'text': 'Date', 'font': {'size': 11}}, rangeslider=dict(visible=False),
                   rangebreaks=[dict(bounds=['sat', 'mon'])], fixedrange=True,
                   gridcolor=GRID_COLOR, linecolor=GRID_COLOR),
        yaxis=dict(title={'text': 'Underlying price', 'font': {'size': 11}},
                   range=list(price_bounds) if price_bounds else None, fixedrange=True,
                   gridcolor=GRID_COLOR, linecolor=GRID_COLOR, zeroline=False),
        legend=dict(orientation='h', y=-0.12, bgcolor='rgba(0,0,0,0)'),
        hovermode='x unified',
    )
    return figure


def _price_bounds(bars, strike_lines, chart) -> Optional[Tuple[float, float]]:
    anchors: List[float] = []
    for bar in bars:
        anchors.extend((bar['low'], bar['high']))
    anchors.extend(line['price'] for line in strike_lines)
    if chart is not None:
        anchors.extend(chart.breakevens)
        anchors.extend(leg.payoff.strike for leg in chart.legs if leg.payoff.strike is not None)
    anchors = [value for value in anchors if value is not None]
    if not anchors:
        return None
    lowest, highest = min(anchors), max(anchors)
    padding = max(1.0, 0.05 * (highest - lowest))
    # Never suggest a negative underlying price.
    return max(0.0, lowest - padding), highest + padding


def fetch_underlying_bars(symbol: str, start: datetime, end: datetime,
                          interval: str = '1d', provider: Any = None):
    """BLOCKING daily bars for one underlying. Only ever called inside ``to_thread``.

    The same provider call the platform's other live charts use; failures are the caller's
    to render as "no bars", never as a fabricated series.
    """
    if provider is None:
        from ...modules.dataproviders import get_provider
        provider = get_provider('ohlcv', 'fmp')
    return provider.get_ohlcv_data(symbol=symbol, start_date=start, end_date=end, interval=interval)


def render_option_structure_chart(container, *, txn, orders, payoff,
                                  bars=None, underlying: str = '') -> None:
    """Paint the figure into a NiceGUI container (no-op without bars)."""
    from nicegui import ui

    normalised, strike_lines, markers = chart_inputs_from(txn, orders, bars)
    figure = build_option_structure_figure(
        bars=normalised, strike_lines=strike_lines, markers=markers, payoff=payoff,
        underlying=underlying,
    )
    with container:
        ui.plotly({**figure.to_plotly_json(), 'config': PLOTLY_CONFIG})             .classes('w-full').style(f'height: {CHART_HEIGHT}px;')
        if isinstance(payoff, PayoffChart):
            ui.label(CHART_LEGEND).classes('text-xs text-secondary-custom')
        if not normalised:
            ui.label(
                'No daily bars available for this underlying — the strikes and the payoff '
                'below are unaffected.'
            ).classes('text-xs text-secondary-custom')
