"""Chart-side payoff view: breakevens, limits, moneyness, plot sampling (spec 2026-09-20).

**BUILT ON ``option_payoff``, NOT BESIDE IT.** That module already owns the leg type
(``PayoffLeg``), the payoff formula (``payoff_at``), validation (``validate_legs``), the kinks
(``critical_points``) and both limit answers with their three named states (``max_loss`` /
``max_profit`` -> ``MEASURED`` / ``UNBOUNDED`` / ``UNMEASURABLE``). This module adds ONLY what
a chart needs and the engine deliberately does not: breakeven roots, flat-zero intervals, a
display domain, moneyness, and the sampling/segmentation a plot draws.

WHY THAT MATTERS: a second implementation of the payoff formula is a second answer to "what is
this position worth", and the popup would then disagree with the risk manager about the same
trade. There is one formula, in one place.

The engine's ``multiplier`` DEFAULTS TO 100.0 -- correct for the rules engine, which only ever
sees real contracts, and exactly the assumption a POPUP must not inherit. Provenance is
therefore carried explicitly on ``ChartLeg.multiplier_recorded``, and a leg whose multiplier
was not recorded refuses to price here even though the engine would happily accept it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ba2_common.core.option_payoff import (
    MEASURED, UNBOUNDED, PayoffLeg, critical_points, max_loss, max_profit, payoff_at,
    validate_legs,
)

MONEYNESS_TOLERANCE = 1e-9
_SLOPE_EPSILON = 1e-9


def moneyness_tolerance(spot: float, strike: float) -> float:
    return MONEYNESS_TOLERANCE * max(1.0, abs(spot), abs(strike))


def moneyness(option_type: Optional[str], strike: Optional[float],
              spot: Optional[float]) -> str:
    """``'ITM'`` / ``'ATM'`` / ``'OTM'`` / ``'unknown'``.

    LONG/SHORT DOES NOT INVERT THIS: a short call is still ITM above its strike. ATM is
    equality at the recorded precision -- floating-point dust scaled to the numbers, never a
    discretionary "near the money" band.
    """
    if option_type not in ('call', 'put') or strike is None or spot is None:
        return 'unknown'
    try:
        strike, spot = float(strike), float(spot)
    except (TypeError, ValueError):
        return 'unknown'
    if not (math.isfinite(strike) and math.isfinite(spot)) or strike <= 0 or spot <= 0:
        return 'unknown'

    difference = spot - strike
    if abs(difference) <= moneyness_tolerance(spot, strike):
        return 'ATM'
    if option_type == 'call':
        return 'ITM' if difference > 0 else 'OTM'
    return 'ITM' if difference < 0 else 'OTM'


def spot_vs_strike_percent(spot: Optional[float], strike: Optional[float]) -> Optional[float]:
    """``100 * (S - K) / K``, labelled "spot vs strike": not a return."""
    if spot is None or strike is None:
        return None
    try:
        spot, strike = float(spot), float(strike)
    except (TypeError, ValueError):
        return None
    return (100.0 * (spot - strike)) / strike if strike > 0 else None


@dataclass(frozen=True)
class Limit:
    """One of the two limits, in the engine's own three states, named so they cannot be confused.

    ``UNBOUNDED`` and ``UNAVAILABLE`` are the distinction the popup must never blur: "Unlimited"
    is a mathematical fact about a tail, "Unavailable" means we could not work it out.
    """

    state: str
    amount: Optional[float] = None
    reason: Optional[str] = None

    @property
    def unlimited(self) -> bool:
        return self.state == UNBOUNDED

    @property
    def measured(self) -> bool:
        return self.state == MEASURED

    def describe(self, loss: bool = False) -> str:
        if self.state == UNBOUNDED:
            return 'Unlimited'
        if self.state == MEASURED and self.amount is not None:
            signed = -self.amount if loss else self.amount
            return f'${signed:,.2f}'
        return 'Unavailable'


@dataclass(frozen=True)
class ChartLeg:
    """A leg plus the provenance a chart must not lose.

    ``multiplier_recorded`` is False when the multiplier was a serialization default rather
    than a recorded contract term -- the number looks identical to a real one and is 100x
    wrong when it is not.
    """

    payoff: PayoffLeg
    expiry: Optional[str] = None
    underlying: Optional[str] = None
    label: str = ''
    multiplier_recorded: bool = True


@dataclass(frozen=True)
class PayoffUnavailable:
    reason: str

    @property
    def available(self) -> bool:
        return False


@dataclass(frozen=True)
class PayoffChart:
    legs: Tuple[ChartLeg, ...]
    net_entry_debit: float
    breakevens: Tuple[float, ...]
    max_profit: Limit
    max_loss: Limit
    flat_zero_intervals: Tuple[Tuple[float, float], ...]
    domain: Tuple[float, float]

    @property
    def available(self) -> bool:
        return True

    def payoff_at(self, underlying_price: float) -> float:
        """The engine's formula, called -- not reimplemented."""
        return payoff_at([leg.payoff for leg in self.legs], underlying_price)


def _to_limit(result, *, loss: bool) -> Limit:
    """The engine's three-state answer, kept in three states."""
    if result.state == MEASURED and result.amount is not None:
        return Limit(MEASURED, amount=float(result.amount))
    if result.state == UNBOUNDED:
        return Limit(UNBOUNDED)
    return Limit(result.state, reason=getattr(result, 'reason', None))


def build_payoff_chart(legs: Sequence[ChartLeg], scope: Optional[int] = None
                       ) -> Union[PayoffChart, PayoffUnavailable]:
    """Breakevens, limits, domain and the payoff itself for the recorded legs.

    ``scope`` prices one leg alone. Without it every leg must validate, because "we left out
    the leg we could not read" silently changes the position -- while a single leg with good
    terms stays inspectable when a sibling is unreadable.

    A combined chart additionally requires ONE expiry and ONE underlying: pricing a diagonal at
    the earlier expiry's intrinsic would be an invented number.
    """
    all_legs = list(legs or [])
    if not all_legs:
        return PayoffUnavailable('no legs recorded')

    if scope is None:
        selected = all_legs
    else:
        if scope < 0 or scope >= len(all_legs):
            return PayoffUnavailable('selected leg not found')
        selected = [all_legs[scope]]

    for offset, leg in enumerate(selected):
        index = offset if scope is None else scope
        if not leg.multiplier_recorded:
            return PayoffUnavailable(f'leg {index + 1}: contract multiplier is unverified')

    problem = validate_legs([leg.payoff for leg in selected])
    if problem is not None:
        return PayoffUnavailable(problem)

    if scope is None:
        expiries = {leg.expiry for leg in all_legs if leg.expiry}
        if len(expiries) > 1:
            return PayoffUnavailable(
                'combined expiration payoff unavailable for different expiries')
        underlyings = {leg.underlying for leg in all_legs if leg.underlying}
        if len(underlyings) > 1:
            return PayoffUnavailable(
                'combined expiration payoff unavailable for different underlyings')

    payoffs = [leg.payoff for leg in selected]
    legs_tuple = tuple(selected)

    net_entry_debit = sum(
        (1.0 if leg.payoff.side.value.upper().startswith('BUY') else -1.0)
        * float(leg.payoff.ratio) * float(leg.payoff.multiplier) * float(leg.payoff.premium)
        for leg in legs_tuple)

    strikes = sorted({float(leg.payoff.strike) for leg in legs_tuple
                      if leg.payoff.strike is not None})
    # The engine's kinks, plus the far tail. Bounded intervals start at 0: an underlying price
    # cannot go negative, which is what keeps a long put's best case finite.
    bounds = sorted({0.0, *critical_points(payoffs)})
    breakevens: List[float] = []
    flat_zero: List[Tuple[float, float]] = []

    for index, low in enumerate(bounds):
        is_last = index == len(bounds) - 1
        high = low + 1.0 if is_last else bounds[index + 1]
        if high <= low:
            continue
        low_value = payoff_at(payoffs, low)
        high_value = payoff_at(payoffs, high)
        slope = (high_value - low_value) / (high - low)
        if abs(slope) <= _SLOPE_EPSILON:
            if abs(low_value) <= _SLOPE_EPSILON:
                flat_zero.append((low, math.inf if is_last else high))
            continue
        root = low - low_value / slope
        in_segment = root >= low if is_last else low <= root <= high
        if in_segment:
            tolerance = moneyness_tolerance(root, max(1.0, abs(root)))
            if not any(abs(existing - root) <= tolerance for existing in breakevens):
                breakevens.append(root)

    breakevens.sort()
    anchors = strikes + breakevens
    lowest, highest = min(anchors), max(anchors)
    padding = max(1.0, 0.1 * (highest - lowest))

    return PayoffChart(
        legs=legs_tuple,
        net_entry_debit=net_entry_debit,
        breakevens=tuple(breakevens),
        max_profit=_to_limit(max_profit(payoffs), loss=False),
        max_loss=_to_limit(max_loss(payoffs), loss=True),
        flat_zero_intervals=tuple(flat_zero),
        domain=(max(0.0, lowest - padding), highest + padding),
    )


def chart_legs_from_rows(rows: Iterable[Dict[str, Any]]) -> List[ChartLeg]:
    """Adapters for raw saved rows or live orders -> ``ChartLeg``.

    ``multiplier_recorded`` defaults to "was the key present", which is the honest question
    for a raw row: the published trade rows default the multiplier to 1 for DISPLAY, and that
    default must never be certified as a contract term.
    """
    out: List[ChartLeg] = []
    for row in rows or []:
        out.append(chart_leg_from_row(row))
    return out


def chart_leg_from_row(row: Dict[str, Any]) -> ChartLeg:
    from ba2_common.core.types import OrderDirection

    recorded = row.get('multiplier_recorded')
    if recorded is None:
        recorded = 'multiplier' in row and row.get('multiplier') is not None
    multiplier = _numeric(row.get('multiplier')) if recorded else None

    side = row.get('side', row.get('direction'))
    if isinstance(side, OrderDirection):
        direction = side
    else:
        direction = OrderDirection.BUY if str(side).strip().lower() in ('buy', 'long', 'b') \
            else OrderDirection.SELL

    kind = row.get('option_type', row.get('kind'))
    kind = getattr(kind, 'value', kind)
    kind = str(kind).strip().lower() if kind is not None else None

    return ChartLeg(
        payoff=PayoffLeg(
            kind=kind,
            side=direction,
            premium=_numeric(row.get('entry_premium', row.get('entry_price', row.get('open_price')))) or 0.0,
            strike=_numeric(row.get('strike')),
            ratio=int(_numeric(row.get('ratio')) or _numeric(
                row.get('contracts', row.get('size', row.get('quantity')))) or 0),
            # NOT substituted with the engine's 100.0 default: an absent multiplier must
            # reach ``validate_legs`` as absent and be refused, not silently priced at 100x.
            multiplier=multiplier,
        ),
        expiry=row.get('expiry'),
        underlying=row.get('underlying_symbol', row.get('underlying')),
        label=row.get('label', ''),
        multiplier_recorded=bool(recorded),
    )


def _numeric(value: Any) -> Optional[float]:
    """Same rule as the engine's own reader: never raises, never accepts a bool or a string."""
    if value is None or isinstance(value, (bool, str, bytes)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def sample_curve(chart: PayoffChart, samples: int = 160) -> List[Tuple[float, float]]:
    """``(price, pnl)`` ascending, including every strike and breakeven.

    Including them means no kink or crossing can fall between two samples and be drawn as a
    straight line through it.
    """
    anchors = {chart.domain[0], chart.domain[1], *chart.breakevens}
    anchors.update(float(leg.payoff.strike) for leg in chart.legs
                   if leg.payoff.strike is not None)
    low, high = chart.domain
    step = (high - low) / max(1, samples)
    value = low
    while value <= high:
        anchors.add(round(value, 6))
        value += step
    return [(price, chart.payoff_at(price)) for price in sorted(a for a in anchors if a >= 0)]


def sign_segments(points: Sequence[Tuple[float, float]]
                  ) -> List[Tuple[str, List[Tuple[float, float]]]]:
    """Contiguous runs of one sign, each including its zero crossing so fills meet."""
    segments: List[Tuple[str, List[Tuple[float, float]]]] = []
    current: Optional[List[Tuple[float, float]]] = None
    current_sign: Optional[str] = None
    previous: Optional[Tuple[float, float]] = None

    for point in points:
        sign = 'profit' if point[1] > 0 else ('loss' if point[1] < 0 else None)
        if sign is None:
            if current is not None:
                current.append(point)
            previous, current, current_sign = point, None, None
            continue
        if current is not None and sign == current_sign:
            current.append(point)
        else:
            start = [previous, point] if previous is not None and previous != point else [point]
            current, current_sign = list(start), sign
            segments.append((sign, current))
        previous = point
    return [(sign, points) for sign, points in segments if len(points) > 1]


def zone_bands(chart: PayoffChart) -> List[Tuple[str, float, float]]:
    """Where the position would be profitable at expiration, as price bands.

    The sign-only reading of the same curve: what remains when the curve cannot be drawn, and
    the low-opacity band underneath it when it can. Boundaries are the breakevens.
    """
    bounds = sorted({chart.domain[0], *chart.breakevens, chart.domain[1]})
    bands = []
    for low, high in zip(bounds, bounds[1:]):
        if high <= low:
            continue
        sign = 'profit' if chart.payoff_at((low + high) / 2) >= 0 else 'loss'
        bands.append((sign, low, high))
    return bands
