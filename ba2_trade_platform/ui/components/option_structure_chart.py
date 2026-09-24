"""The live option popup's price chart: candles, strikes, markers, rotated payoff overlay.

Spec 2026-09-20, steps 9 and 10, decisions 1a and 2c.

**Why Plotly makes decision 1a easy here.** The React popup needs a custom overlay layer to
draw the payoff rotated onto the price axis. Plotly has a native form of it: the payoff is a
trace with ``x = position P&L`` and ``y = underlying price`` on a SECOND x-axis that overlays
the time axis (``xaxis2``, side ``top``), and each sign run is filled to the zero line with
``fill='tozerox'``. No fake dates, no custom JavaScript.

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
CURVE_COLOR = '#0e7490'
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
            step = span * 0.16 if span > 0 else max(abs(price) * 0.005, 0.05)
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
            annotation_text=line.get('label', ''), annotation_position='right',
            annotation_font_size=10,
        )

    chart = payoff if isinstance(payoff, PayoffChart) else None

    # Sign-only bands: the same reading as the overlay, at low opacity. They are what
    # remains when the curve cannot be drawn (or the toggle is off).
    if chart is not None:
        for sign, low, high in zone_bands(chart):
            figure.add_hrect(
                y0=low, y1=high, line_width=0,
                fillcolor=PROFIT_COLOR if sign == 'profit' else LOSS_COLOR,
                opacity=0.06 if show_overlay else 0.10,
            )

    if show_overlay and chart is not None:
        samples = sample_curve(chart)
        # Each sign run, filled back to the zero line of the P&L axis.
        for sign, points in sign_segments(samples):
            figure.add_trace(go.Scatter(
                x=[point[1] for point in points], y=[point[0] for point in points],
                xaxis='x2', yaxis='y', mode='lines',
                line=dict(color=PROFIT_COLOR if sign == 'profit' else LOSS_COLOR, width=1),
                fill='tozerox',
                fillcolor=(PROFIT_COLOR if sign == 'profit' else LOSS_COLOR),
                opacity=0.16, hoverinfo='skip', showlegend=False,
            ))
        figure.add_trace(go.Scatter(
            x=[point[1] for point in samples], y=[point[0] for point in samples],
            xaxis='x2', yaxis='y', mode='lines', name='Expiration payoff',
            line=dict(color=CURVE_COLOR, width=2),
            hovertemplate='underlying %{y:.2f} → P&L %{x:+.2f}<extra></extra>',
        ))
        figure.add_vline(x=0, xref='x2', line_dash='dot', line_width=1, line_color='#94a3b8')

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
            marker=dict(symbol='diamond' if kind == 'structure' else 'circle',
                        size=10 if kind == 'structure' else 7, color=colour),
            # Short label on the canvas (long labels overlap into an unreadable smear),
            # full sentence on hover.
            text=[m.get('short') or m.get('text', '') for m in selected],
            textposition=[m.get('text_position', 'top center') for m in selected],
            textfont=dict(size=9),
            hovertext=[m.get('text', '') for m in selected], hoverinfo='text',
        ))

    axis2: Dict[str, Any] = {
        'overlaying': 'x', 'side': 'top', 'showgrid': False,
        'title': {'text': 'Position P&L at expiration ($)', 'font': {'size': 11}},
        'zeroline': False,
    }
    if chart is not None and show_overlay:
        reach = max((abs(point[1]) for point in sample_curve(chart)), default=1.0) or 1.0
        axis2['range'] = [-reach * 1.05, reach * 1.05]

    price_bounds = _price_bounds(bars, strike_lines, chart)
    figure.update_layout(
        height=height,
        template='plotly_dark',
        margin=dict(l=40, r=40, t=40, b=30),
        xaxis=dict(title={'text': 'Date', 'font': {'size': 11}}, rangeslider=dict(visible=False)),
        xaxis2=axis2,
        yaxis=dict(title={'text': 'Underlying price', 'font': {'size': 11}},
                   range=list(price_bounds) if price_bounds else None),
        legend=dict(orientation='h', y=-0.12),
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
        ui.plotly(figure).classes('w-full').style(f'height: {CHART_HEIGHT}px;')
        if not normalised:
            ui.label(
                'No daily bars available for this underlying — the strikes and the payoff '
                'below are unaffected.'
            ).classes('text-xs text-secondary-custom')
