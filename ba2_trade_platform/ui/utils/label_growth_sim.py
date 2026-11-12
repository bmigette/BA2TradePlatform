"""The allocator's label growth simulator: the arithmetic. Pure -- no NiceGUI, no IO, no clock.

"If I had put ``amount`` into this label at the start of the period, split by the label's CURRENT effective shares,
what would it be worth today?" Buy and hold from the start: no rebalancing. Four horizons (YTD from the last close of
the previous year, 1, 3 and 5 years back to the same calendar day), each to the latest close. Two answers per horizon:

* PRICE ONLY -- the shares bought at the start, valued at their last close;
* PRICE + DISTRIBUTIONS -- the same, plus every distribution those shares received in the window, as CASH (not
  reinvested). Several of the owner's funds pay large distributions out of their own price, so the price-only figure
  alone understates them.

ONE BASIS. The closes must be split-adjusted and NOT dividend-adjusted (a dividend-adjusted close already contains the
distributions, which would then be counted twice), and the per-share distribution amounts must be split-adjusted to
the SAME basis -- the service (``services/label_growth.py``) hands in FMP ``close`` and ``adjDividend`` for that
reason. Nothing here adjusts anything: amounts are used exactly as given.

RULES THAT ARE NOT ARITHMETIC
* The base close of a horizon is the last bar ON OR BEFORE its start date. History is fetched from a full year before
  the longest horizon (``FETCH_BUFFER_DAYS``), so a symbol with no bar between that fetch start and a horizon's start
  had no price for at least a year before it: it did not exist (or did not trade) for the whole period. Its slice is
  held as CASH until its first bar, invested at that close, and the row (with the dates actually observed) and the
  horizon both say so ("N of M symbols did not exist for the whole period").
* A base close more than ``GAP_DAYS`` before the start is a DATA GAP (a halt, a hole in the store), not a late start:
  the slice is invested at that last real close and the row and the horizon flag the gap with its dates.
* A symbol with no price history at all is UNMEASURABLE: its slice stays cash, clearly marked, never priced.
* A distribution counts when its ex-date is AFTER the buy bar (a buy at the ex-date's close is already ex) and on or
  before the symbol's last bar. A distribution whose amount is unknown, or a history that could not be fetched, makes
  the PRICE + DISTRIBUTIONS answer unknown (``None`` plus a reason) -- never smaller. The price-only answer stands.
* Shares under 100% leave the rest of the amount in cash (the allocator itself leaves an unassigned share unbought);
  shares over 100% are refused.
* A bar with no usable close (non-finite or not positive) is dropped and counted on the row, never read as a price.

Dates are ISO ``YYYY-MM-DD`` strings throughout (the shape ``services.price_history.fetch_closes`` returns), which
compare correctly as strings.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: The amount the dialog opens with.
DEFAULT_AMOUNT = 10_000.0

#: The horizons, in display order. ``ytd`` is measured from the LAST CLOSE OF THE PRIOR YEAR (the standard base), the
#: others from the same calendar day N years earlier -- the same windows the symbol-info panel uses.
HORIZONS: Tuple[str, ...] = ('ytd', '1y', '3y', '5y')
HORIZON_NAMES = {'ytd': 'YTD', '1y': '1 year', '3y': '3 years', '5y': '5 years'}
_YEARS_BACK = {'1y': 1, '3y': 3, '5y': 5}

#: Calendar days fetched before the longest horizon's start. A YEAR, not a holiday weekend: "no bar between the fetch
#: start and the horizon start" must mean the symbol had no price for a long time (it did not exist or did not trade),
#: not that it was halted for two weeks -- with 14 days a short halt read as "did not exist".
FETCH_BUFFER_DAYS = 366

#: A base close older than this (calendar days before the start) is reported as a data gap. Seven covers the longest
#: ordinary closure before a start date (a weekend plus Christmas / New Year).
GAP_DAYS = 7

#: Shares are stored on a cent grid; a total within this of 100 is 100.
SHARE_TOLERANCE_PCT = 0.005

STATUS_INVESTED = 'invested'
STATUS_LATE = 'late'
STATUS_GAP = 'gap'
STATUS_NO_HISTORY = 'no_history'
STATUS_ZERO_SHARE = 'zero_share'
#: The statuses that hold shares (the rest hold cash or nothing).
ACTIVE_STATUSES = (STATUS_INVESTED, STATUS_LATE, STATUS_GAP)

SERIES_PRICE = 'Price only'
SERIES_TOTAL = 'Price + distributions (cash)'
#: Validated (dataviz validate_palette, dark mode, on the panel's #1a1f2e surface): lightness band, chroma, CVD and
#: normal-vision separation and contrast all pass.
COLOR_PRICE = '#3c8fd6'
COLOR_TOTAL = '#d4761f'

UNKNOWN_TEXT = 'n/a'


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------

def _years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:          # 29 February -> 28
        return day.replace(year=day.year - years, day=28)


def horizon_start(horizon: str, as_of: date) -> date:
    """The date a horizon is measured FROM: 31 December of the prior year for ``ytd``, else the same calendar day N
    years before ``as_of`` (29 February folded to 28). The base close is the last bar on or before it."""
    if horizon == 'ytd':
        return date(as_of.year - 1, 12, 31)
    if horizon not in _YEARS_BACK:
        raise ValueError(f"unknown horizon {horizon!r}; expected one of {HORIZONS}")
    return _years_before(as_of, _YEARS_BACK[horizon])


def history_start(as_of: date, horizons: Sequence[str] = HORIZONS) -> date:
    """The first date to fetch closes from: the earliest horizon start minus ``FETCH_BUFFER_DAYS``."""
    return min(horizon_start(h, as_of) for h in horizons) - timedelta(days=FETCH_BUFFER_DAYS)


# ---------------------------------------------------------------------------
# inputs and results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DistributionHistory:
    """One symbol's distributions: ``events`` = ``((ex-date, amount per share on the price basis or None), ...)``.

    ``events=None`` means the history could NOT be fetched (``error`` says why) -- not the same as ``()``, a symbol
    that paid nothing."""
    events: Optional[Tuple[Tuple[str, Optional[float]], ...]]
    error: Optional[str] = None


@dataclass
class SymbolOutcome:
    symbol: str
    share_pct: float
    invested: float
    status: str
    buy_date: Optional[str] = None
    buy_price: Optional[float] = None
    last_date: Optional[str] = None
    last_price: Optional[float] = None
    quantity: Optional[float] = None
    #: The slice valued at the last close; the slice itself (cash) for a symbol with no history.
    price_value: float = 0.0
    #: Per share and in money; None = unknown (see the horizon's ``distributions_unknown``).
    distributions_per_share: Optional[float] = 0.0
    distributions: Optional[float] = 0.0
    total_value: Optional[float] = 0.0
    price_gain_pct: Optional[float] = None
    total_gain_pct: Optional[float] = None
    stale: bool = False
    note: str = ''


@dataclass
class HorizonResult:
    horizon: str
    name: str
    start: str
    end: Optional[str]
    amount: float
    unassigned_pct: float
    unassigned_cash: float
    price_value: float
    price_gain: float
    price_gain_pct: float
    distributions: Optional[float]
    total_value: Optional[float]
    total_gain: Optional[float]
    total_gain_pct: Optional[float]
    rows: List[SymbolOutcome]
    invested_count: int
    late_symbols: List[str]
    no_history_symbols: List[str]
    gap_symbols: List[str] = field(default_factory=list)
    distributions_unknown: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    series_dates: List[str] = field(default_factory=list)
    series_price: List[float] = field(default_factory=list)
    series_total: Optional[List[float]] = None


@dataclass
class SimulationResult:
    amount: float
    as_of: date
    horizons: Dict[str, HorizonResult]


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------

def _finite(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _validate(amount, shares: Mapping[str, float]) -> Tuple[float, Dict[str, float], float]:
    if not _finite(amount) or float(amount) <= 0:
        raise ValueError(f"The amount must be a positive number of dollars, not {amount!r}")
    clean: Dict[str, float] = {}
    for symbol, share in shares.items():
        if not _finite(share) or float(share) < 0:
            raise ValueError(f"{symbol}: share {share!r} is not a usable percentage (it must be 0 or more)")
        clean[str(symbol).strip().upper()] = float(share)
    total = sum(clean.values())
    if total > 100.0 + SHARE_TOLERANCE_PCT:
        raise ValueError(f"The label's shares add to {total:.2f}%, over 100%: fix the split first")
    return float(amount), clean, min(total, 100.0)


def _usable_bars(closes: Optional[Mapping[str, Any]], as_of_iso: str) -> Tuple[List[Tuple[str, float]], int]:
    """``([(date, close)] ascending, bars dropped for an unusable close)``, bars after ``as_of`` left out."""
    bars, dropped = [], 0
    for day, close in (closes or {}).items():
        day = str(day)[:10]
        if day > as_of_iso:
            continue
        if not _finite(close) or float(close) <= 0:
            dropped += 1
            continue
        bars.append((day, float(close)))
    bars.sort()
    return bars, dropped


def _gain_pct(value: Optional[float], invested: float) -> Optional[float]:
    if value is None or invested <= 0:
        return None
    return (value - invested) / invested * 100.0


def _simulate_symbol(symbol: str, share: float, slice_: float, bars: List[Tuple[str, float]], dropped: int,
                     payouts: Optional[DistributionHistory], start_iso: str, history_from: str,
                     unknown: List[str]) -> SymbolOutcome:
    notes: List[str] = []
    if dropped:
        notes.append(f"{dropped} bar(s) with no usable close ignored")
    if share <= 0:
        return SymbolOutcome(symbol, share, 0.0, STATUS_ZERO_SHARE, price_value=0.0,
                             note='; '.join(['0% share: nothing invested'] + notes))
    if not bars:
        return SymbolOutcome(symbol, share, slice_, STATUS_NO_HISTORY, price_value=slice_, total_value=slice_,
                             price_gain_pct=0.0, total_gain_pct=0.0,
                             note='; '.join(['No price history from the price source: unmeasurable, this slice is '
                                             'held as cash'] + notes))
    base = None
    for day, close in bars:
        if day <= start_iso:
            base = (day, close)
        else:
            break
    late = base is None
    gap = False
    if late:
        base = bars[0]
        notes.insert(0, f"No price on or before the start ({start_iso}) in the history fetched from "
                        f"{history_from}: did not exist or did not trade then; held as cash until its first price "
                        f"on {base[0]}, invested then")
    elif (date.fromisoformat(start_iso) - date.fromisoformat(base[0])).days > GAP_DAYS:
        gap = True
        notes.insert(0, f"Data gap: no price between {base[0]} and the start ({start_iso}); bought at the "
                        f"{base[0]} close, the last real price before the start")
    buy_date, buy_price = base
    last_date, last_price = bars[-1]
    qty = slice_ / buy_price
    price_value = qty * last_price

    per_share: Optional[float]
    if payouts is None:
        per_share = None
        unknown.append(f"{symbol}: no distribution history was fetched")
    elif payouts.events is None:
        per_share = None
        unknown.append(f"{symbol}: the distribution history could not be fetched"
                       + (f" ({payouts.error})" if payouts.error else ''))
    else:
        window = [amt for ex, amt in payouts.events if buy_date < str(ex)[:10] <= last_date]
        missing = sum(1 for a in window if not _finite(a))
        if missing:
            per_share = None
            unknown.append(f"{symbol}: {missing} of {len(window)} distribution(s) in the window carry no "
                           f"split-adjusted amount")
        else:
            per_share = float(sum(float(a) for a in window))
    dist = None if per_share is None else qty * per_share
    total = None if dist is None else price_value + dist
    return SymbolOutcome(
        symbol, share, slice_, STATUS_LATE if late else (STATUS_GAP if gap else STATUS_INVESTED),
        buy_date=buy_date, buy_price=buy_price, last_date=last_date, last_price=last_price, quantity=qty,
        price_value=price_value, distributions_per_share=per_share, distributions=dist, total_value=total,
        price_gain_pct=_gain_pct(price_value, slice_), total_gain_pct=_gain_pct(total, slice_),
        note='; '.join(notes))


def _series(rows: List[SymbolOutcome], bars_by: Dict[str, List[Tuple[str, float]]],
            payouts_by: Mapping[str, DistributionHistory], start_iso: str, end_iso: Optional[str],
            constant_cash: float, totals_known: bool) -> Tuple[List[str], List[float], Optional[List[float]]]:
    """The label's simulated value on every bar date in (start, end], plus a first point at the start date."""
    active = [r for r in rows if r.status in ACTIVE_STATUSES]
    cash = constant_cash + sum(r.invested for r in rows if r.status == STATUS_NO_HISTORY)
    dates = sorted({d for r in active for d, _c in bars_by[r.symbol] if start_iso < d <= (end_iso or start_iso)})
    dates = [start_iso] + dates
    prices: List[float] = []
    totals: List[float] = []
    cursor = {r.symbol: -1 for r in active}
    events = {}
    for r in active:
        payouts = payouts_by.get(r.symbol)
        evs = payouts.events if (payouts is not None and payouts.events is not None) else ()
        events[r.symbol] = sorted((str(ex)[:10], float(a)) for ex, a in evs
                                  if _finite(a) and r.buy_date < str(ex)[:10] <= r.last_date)
    ev_cursor = {r.symbol: 0 for r in active}
    paid = {r.symbol: 0.0 for r in active}
    for day in dates:
        value = cash
        dist = 0.0
        for r in active:
            bars = bars_by[r.symbol]
            i = cursor[r.symbol]
            while i + 1 < len(bars) and bars[i + 1][0] <= day:
                i += 1
            cursor[r.symbol] = i
            if day < r.buy_date or i < 0:
                value += r.invested                                   # still cash (late starter)
                continue
            value += r.quantity * bars[i][1]
            evs, j = events[r.symbol], ev_cursor[r.symbol]
            while j < len(evs) and evs[j][0] <= day:
                paid[r.symbol] += r.quantity * evs[j][1]
                j += 1
            ev_cursor[r.symbol] = j
            dist += paid[r.symbol]
        prices.append(value)
        totals.append(value + dist)
    return dates, prices, (totals if totals_known else None)


def simulate_horizon(horizon: str, amount: float, shares: Mapping[str, float],
                     closes: Mapping[str, Mapping[str, float]], distributions: Mapping[str, DistributionHistory],
                     *, as_of: date) -> HorizonResult:
    amount, shares, share_total = _validate(amount, shares)
    as_of_iso = as_of.isoformat()
    start_iso = horizon_start(horizon, as_of).isoformat()
    history_from = history_start(as_of).isoformat()
    unassigned_pct = max(0.0, 100.0 - share_total)
    if unassigned_pct < SHARE_TOLERANCE_PCT:
        unassigned_pct = 0.0
    unassigned_cash = amount * unassigned_pct / 100.0
    upper = {str(k).strip().upper(): v for k, v in (closes or {}).items()}
    payouts_by = {str(k).strip().upper(): v for k, v in (distributions or {}).items()}

    rows: List[SymbolOutcome] = []
    bars_by: Dict[str, List[Tuple[str, float]]] = {}
    unknown: List[str] = []
    for symbol, share in shares.items():
        bars, dropped = _usable_bars(upper.get(symbol), as_of_iso)
        bars_by[symbol] = bars
        rows.append(_simulate_symbol(symbol, share, amount * share / 100.0, bars, dropped,
                                     payouts_by.get(symbol), start_iso, history_from, unknown))

    active = [r for r in rows if r.status in ACTIVE_STATUSES]
    end_iso = max((r.last_date for r in active), default=None)
    for r in active:
        if end_iso and r.last_date < end_iso:
            r.stale = True
            r.note = '; '.join(filter(None, [f"Last price is from {r.last_date}, older than the label's latest "
                                             f"close ({end_iso})", r.note]))

    cash_rows = sum(r.invested for r in rows if r.status == STATUS_NO_HISTORY)
    price_value = unassigned_cash + cash_rows + sum(r.price_value for r in active)
    known = not unknown
    distributions_total = sum(r.distributions for r in active) if known else None
    total_value = price_value + distributions_total if known else None

    invested = [r for r in rows if r.status != STATUS_ZERO_SHARE]
    late = [r.symbol for r in rows if r.status == STATUS_LATE]
    blind = [r.symbol for r in rows if r.status == STATUS_NO_HISTORY]
    gaps = [r.symbol for r in rows if r.status == STATUS_GAP]
    notes: List[str] = []
    if late:
        first = ', '.join(f"{r.symbol} from {r.buy_date}" for r in rows if r.status == STATUS_LATE)
        notes.append(f"{len(late)} of {len(invested)} symbols did not exist for the whole period: their slice was "
                     f"held as cash until their first price ({first}).")
    if gaps:
        first = ', '.join(f"{r.symbol} last priced {r.buy_date}" for r in rows if r.status == STATUS_GAP)
        notes.append(f"{len(gaps)} of {len(invested)} symbols have a data gap at the start (no price in the "
                     f"{GAP_DAYS} days before it): bought at their last earlier close ({first}).")
    if blind:
        notes.append(f"{len(blind)} of {len(invested)} symbols have no price history at all ({', '.join(blind)}): "
                     f"unmeasurable, their slice is held as cash.")
    for r in active:
        if r.stale:
            notes.append(f"{r.symbol}: last price is from {r.last_date}, older than the label's latest close "
                         f"({end_iso}).")
    if unassigned_cash > 0:
        notes.append(f"The label's shares add to {share_total:.2f}%: the other {unassigned_pct:.2f}% "
                     f"({fmt_money(unassigned_cash)}) is held as cash.")
    if unknown:
        notes.append("Distributions could not be measured, so the price + distributions figure is unknown: "
                     + '; '.join(unknown) + '.')

    dates, series_price, series_total = _series(rows, bars_by, payouts_by, start_iso, end_iso, unassigned_cash,
                                                known)
    return HorizonResult(
        horizon=horizon, name=HORIZON_NAMES[horizon], start=start_iso, end=end_iso, amount=amount,
        unassigned_pct=unassigned_pct, unassigned_cash=unassigned_cash,
        price_value=price_value, price_gain=price_value - amount, price_gain_pct=(price_value - amount) / amount * 100,
        distributions=distributions_total, total_value=total_value,
        total_gain=None if total_value is None else total_value - amount,
        total_gain_pct=_gain_pct(total_value, amount),
        rows=rows, invested_count=len(invested), late_symbols=late, no_history_symbols=blind, gap_symbols=gaps,
        distributions_unknown=unknown, notes=notes,
        series_dates=dates, series_price=series_price, series_total=series_total)


def simulate_label_growth(amount: float, shares: Mapping[str, float], closes: Mapping[str, Mapping[str, float]],
                          distributions: Mapping[str, DistributionHistory], *, as_of: date,
                          horizons: Sequence[str] = HORIZONS) -> SimulationResult:
    """Every horizon of the simulation. ``shares`` = ``{SYMBOL: effective share of the label %}`` (0 allowed, total at
    most 100); ``closes`` = ``{SYMBOL: {YYYY-MM-DD: split-adjusted close}}`` (a symbol absent = no price history);
    ``distributions`` = ``{SYMBOL: DistributionHistory}`` on the same split basis. Raises ``ValueError`` for an amount
    that is not positive or shares that are negative or over 100%."""
    _validate(amount, shares)
    return SimulationResult(
        amount=float(amount), as_of=as_of,
        horizons={h: simulate_horizon(h, amount, shares, closes, distributions, as_of=as_of) for h in horizons})


# ---------------------------------------------------------------------------
# display helpers (pure)
# ---------------------------------------------------------------------------

def fmt_money(value: Optional[float]) -> str:
    if value is None:
        return UNKNOWN_TEXT
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def fmt_signed_money(value: Optional[float]) -> str:
    if value is None:
        return UNKNOWN_TEXT
    return f"-${abs(value):,.2f}" if value < 0 else f"+${value:,.2f}"


def fmt_signed_pct(value: Optional[float]) -> str:
    if value is None:
        return UNKNOWN_TEXT
    return f"{value:+.2f}%"


def fmt_price(value: Optional[float]) -> str:
    if value is None:
        return UNKNOWN_TEXT
    return f"${value:,.4f}" if value < 1 else f"${value:,.2f}"


def gain_color(value: Optional[float]) -> str:
    """Text colour of a gain: green up, red down, muted for zero / unknown."""
    if value is None or abs(value) < 1e-9:
        return '#a0aec0'
    return '#00d4aa' if value > 0 else '#ff6b6b'


STATUS_TEXT = {STATUS_INVESTED: 'Held', STATUS_LATE: 'Late start', STATUS_GAP: 'Data gap at start',
               STATUS_NO_HISTORY: 'Unmeasurable (cash)', STATUS_ZERO_SHARE: '0% share'}


def table_rows(h: HorizonResult) -> List[Dict[str, Any]]:
    """The per-symbol breakdown of one horizon as table rows (strings, ready to draw)."""
    out = []
    for r in h.rows:
        out.append({
            'symbol': r.symbol,
            'share': f"{r.share_pct:.2f}%",
            'status': STATUS_TEXT[r.status],
            'invested': fmt_money(r.invested),
            'start': ('—' if r.buy_date is None else f"{fmt_price(r.buy_price)} on {r.buy_date}"),
            'last': ('—' if r.last_date is None else f"{fmt_price(r.last_price)} on {r.last_date}"),
            'distributions': ('—' if r.status in (STATUS_ZERO_SHARE, STATUS_NO_HISTORY)
                              else fmt_money(r.distributions)),
            'price_value': fmt_money(r.price_value),
            'total_value': fmt_money(r.total_value),
            'price_gain_pct': fmt_signed_pct(r.price_gain_pct) if r.price_gain_pct is not None else '—',
            'total_gain_pct': fmt_signed_pct(r.total_gain_pct) if r.total_gain_pct is not None else '—',
            'note': r.note,
        })
    return out


def growth_chart_options(h: HorizonResult) -> Dict[str, Any]:
    """ECharts options: the label's simulated value over the horizon, price only and price + distributions, on ONE
    money axis. The total line is left out when distributions are unknown (a line of invented zeros would lie)."""
    def line(name, data, color):
        return {'name': name, 'type': 'line', 'data': [round(v, 2) for v in data], 'showSymbol': False,
                'lineStyle': {'width': 2, 'color': color}, 'itemStyle': {'color': color}}

    series = [line(SERIES_PRICE, h.series_price, COLOR_PRICE)]
    if h.series_total is not None:
        series.append(line(SERIES_TOTAL, h.series_total, COLOR_TOTAL))
    muted = '#a0aec0'
    return {
        'backgroundColor': 'transparent',
        'animation': False,
        'grid': {'left': 70, 'right': 20, 'top': 40, 'bottom': 40},
        'legend': {'data': [s['name'] for s in series], 'textStyle': {'color': '#e2e8f0'}, 'top': 5},
        'tooltip': {'trigger': 'axis', 'axisPointer': {'type': 'cross'}},
        'xAxis': {'type': 'category', 'data': list(h.series_dates), 'boundaryGap': False,
                  'axisLabel': {'color': muted}, 'axisLine': {'lineStyle': {'color': '#4a5568'}}},
        'yAxis': {'type': 'value', 'scale': True, 'name': 'Value ($)', 'nameTextStyle': {'color': muted},
                  'axisLabel': {'color': muted},
                  'splitLine': {'lineStyle': {'color': 'rgba(255,255,255,0.08)'}}},
        'series': series,
    }
