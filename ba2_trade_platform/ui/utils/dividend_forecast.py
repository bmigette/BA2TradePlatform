"""Dividend forecast for the next N months, from a symbol's own payment history.

One pure function, :func:`forecast_dividends`. No invented numbers: a symbol with
too little history, an irregular cadence or a stale last payment gets NO forecast.

The rule
--------
* ``history``: ``(date, per_share_amount)`` pairs. Several entries on one date are
  summed (a regular plus a special payment on the same day is one payment).
* Cadence: each of the last gaps is classified weekly (<=10d), monthly (25-35), quarterly
  (80-100), semi-annual (170-195) or annual (350-380); the cadence is the class that more than
  plurality (at least two gaps, strictly more than any other class) agree on -- one missed
  month does not hide a monthly payer, monthly + quarterly supplements still read monthly, an
  irregular series gets none. At least two payments are needed.
* Specials (``specials=True``, used for declared provider history): a payment more than
  ``SPECIAL_RATIO`` (3x) the median of the payments before it is a special, not projected --
  neither its amount nor its date takes part (MAIN: monthly regular + quarterly supplemental
  projects only the monthly cadence).
* Outliers: a per-share value more than ``OUTLIER_RATIO`` (5x) above, or below 1/5 of, the
  MEDIAN of the symbol's other recent values (last ``OUTLIER_WINDOW``) is excluded from the
  AMOUNT (not from the cadence: it is still a real payment date). A payment right after a
  forced liquidation divides a normal payout by a tiny share count and explodes.
* Amount per share: the last non-outlier payment; if the last three non-outlier values differ
  by more than 10% of their mean (a variable payer) the MEDIAN of those three.
* Dates: last payment + one period, repeated (7 days for weekly, whole calendar
  months otherwise, day clamped to the month's end). Every date inside
  ``[today, today + months]`` is a forecast payment: a weekly payer yields several.
* Overdue: if the next date is before ``today`` it is taken as paid late and
  placed ON ``today`` (later ones keep the original schedule) -- but only when it
  is less than one full period overdue; a payer silent for longer is treated as
  stopped and gets no forecast.
* ``amount = per_share * held_qty``; ``held_qty <= 0`` gives nothing.
"""
import calendar
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: (min_gap_days, max_gap_days, step) -- step is ('days', n) or ('months', n).
_CADENCES = (
    (1, 10, ('days', 7)),
    (25, 35, ('months', 1)),
    (80, 100, ('months', 3)),
    (170, 195, ('months', 6)),
    (350, 380, ('months', 12)),
)
VARIABLE_TOLERANCE = 0.10
OUTLIER_RATIO = 5.0
SPECIAL_RATIO = 3.0
OUTLIER_WINDOW = 8
#: A per-date quantity below this share of the CURRENT holding is not a trustworthy divisor.
TINY_QTY_FRACTION = 0.10


def is_outlier(value: float, others: Sequence[float]) -> bool:
    """More than ``OUTLIER_RATIO`` x above, or below 1/``OUTLIER_RATIO`` of, the median of
    ``others`` (needs at least two others to have an opinion). Pure."""
    vals = [v for v in others if v is not None and v > 0]
    if len(vals) < 2:
        return False
    med = median(vals)
    return value > OUTLIER_RATIO * med or value < med / OUTLIER_RATIO


def drop_tiny_quantity_outliers(per_date: Dict[Any, float], qty_on_date: Dict[Any, float],
                                current_qty: float) -> Dict[Any, float]:
    """Per-share values to trust: a date is dropped when the shares held on it were below
    ``TINY_QTY_FRACTION`` of today's quantity AND its per-share value is an outlier against
    the symbol's other values. Pure."""
    out = {}
    for d, v in per_date.items():
        others = [x for k, x in per_date.items() if k != d]
        tiny = current_qty > 0 and qty_on_date.get(d, 0) < TINY_QTY_FRACTION * current_qty
        if tiny and is_outlier(v, others):
            continue
        out[d] = v
    return out


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def add_months(d: date, months: int) -> date:
    """``d`` plus whole calendar months, day clamped to the target month's end."""
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _advance(d: date, step: Tuple[str, int], k: int, anchor: date) -> date:
    """The k-th period after ``anchor`` (computed from the anchor, so month-end
    clamping never drifts: 31 Jan -> 28 Feb -> 31 Mar, not 28 Mar)."""
    kind, n = step
    if kind == 'days':
        return anchor + timedelta(days=n * k)
    return add_months(anchor, n * k)


def _step_days(step: Tuple[str, int]) -> int:
    return step[1] if step[0] == 'days' else step[1] * 30


def _classify_gap(gap_days: int) -> Optional[Tuple[str, int]]:
    for lo, hi, step in _CADENCES:
        if lo <= gap_days <= hi:
            return step
    return None


def detect_cadence(dates: List[date]) -> Optional[Tuple[str, int]]:
    """The step ('days'|'months', n) more than half of the last gaps agree on, else None."""
    if len(dates) < 2:
        return None
    recent = dates[-7:]
    gaps = [(b - a).days for a, b in zip(recent, recent[1:])]
    counts: Dict[Tuple[str, int], int] = {}
    for g in gaps:
        step = _classify_gap(g)
        if step is not None:
            counts[step] = counts.get(step, 0) + 1
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], _step_days(kv[0])))
    best, best_n = ranked[0]
    if len(gaps) == 1:
        return best
    # a clear plurality of at least two gaps: a monthly payer with quarterly supplements (gaps of
    # ~31 and ~10 days) still reads monthly; two different classes tied or one stray gap does not
    second_n = ranked[1][1] if len(ranked) > 1 else 0
    return best if best_n >= 2 and best_n > second_n else None


def drop_specials(by_date: Dict[date, float]) -> Dict[date, float]:
    """Payments that are not a special: a value more than ``SPECIAL_RATIO`` x the median of the
    (up to six) payments before it is dropped. The first two payments cannot be judged."""
    out: Dict[date, float] = {}
    prior: List[float] = []
    for d in sorted(by_date):
        v = by_date[d]
        if len(prior) >= 2 and v > SPECIAL_RATIO * median(prior[-6:]):
            continue                                  # special: not part of the regular series
        out[d] = v
        prior.append(v)
    return out


def _normalise(history: Iterable[Tuple[Any, float]]) -> Dict[date, float]:
    by_date: Dict[date, float] = {}
    for d, amt in history or []:
        if d is None or amt is None:
            continue
        amt = float(amt)
        if amt <= 0:
            continue
        key = _to_date(d)
        by_date[key] = by_date.get(key, 0.0) + amt
    return by_date


def _project(anchor: date, step: Tuple[str, int], per_share: float, held_qty: float,
             today: date, months: int, first_is_anchor: bool = False) -> List[Dict[str, Any]]:
    """Events in ``[today, today + months]`` on the schedule ``anchor + k * step`` (k >= 1, or
    k >= 0 when ``first_is_anchor``). A date before today (one period overdue at most -- the
    callers guard staleness) is taken as paid late and placed ON today; later ones keep the
    original schedule."""
    end = add_months(today, months)
    out: List[Dict[str, Any]] = []
    k = 0 if first_is_anchor else 1
    while True:
        due = _advance(None, step, k, anchor)
        if due > end:
            break
        k += 1
        if due < today:
            due = today if not out else due
            if due < today:
                continue
        out.append({'date': due, 'per_share': round(per_share, 6),
                    'amount': round(per_share * float(held_qty), 2)})
    return out


def forecast_dividends_ex(history: Iterable[Tuple[Any, float]], held_qty: float, today: Any,
                          months: int = 2, specials: bool = False, include_declared: bool = False):
    """``(events, info)``. ``info['status']``: ``'ok'`` (a cadence was found; ``events`` may be
    empty because the next payment is beyond the horizon -- correct), ``'no_qty'``,
    ``'no_history'``, ``'no_cadence'`` or ``'stale'``; plus ``cadence`` and ``per_share``."""
    today = _to_date(today)
    info: Dict[str, Any] = {'status': 'no_history', 'cadence': None, 'per_share': None}
    if not held_qty or float(held_qty) <= 0:
        info['status'] = 'no_qty'
        return [], info
    by_date = _normalise(history)
    if specials:
        by_date = drop_specials(by_date)
    dates = sorted(by_date)
    if not dates:
        return [], info
    step = detect_cadence(dates)
    if step is None:
        info['status'] = 'no_cadence'
        return [], info
    info['cadence'] = step

    last_date = dates[-1]                      # the schedule runs from the last REAL payment date
    recent = dates[-OUTLIER_WINDOW:]
    clean = [d for d in dates if not (d in recent and is_outlier(
        by_date[d], [by_date[o] for o in recent if o != d]))]
    if not clean:
        return [], info
    last3 = [by_date[d] for d in clean[-3:]]
    mean3 = sum(last3) / len(last3)
    variable = len(last3) > 1 and mean3 > 0 and (max(last3) - min(last3)) / mean3 > VARIABLE_TOLERANCE
    per_share = median(last3) if variable else by_date[clean[-1]]
    info['per_share'] = per_share

    if (today - last_date).days >= 2 * _step_days(step):      # silent for over a full extra period
        info['status'] = 'stale'
        return [], info
    info['status'] = 'ok'
    events = _project(last_date, step, per_share, held_qty, today, months)
    if include_declared:
        # Payments the provider already DECLARED (dated after today) are certain amounts: they come
        # first, with their own per-share value; the projection continues after the last of them.
        end = add_months(today, months)
        declared = [{'date': d, 'per_share': round(by_date[d], 6),
                     'amount': round(by_date[d] * float(held_qty), 2)}
                    for d in dates if today < d <= end]
        events = declared + events
    return events, info


def forecast_dividends(history: Iterable[Tuple[Any, float]], held_qty: float,
                       today: Any, months: int = 2, specials: bool = False,
                       include_declared: bool = False) -> List[Dict[str, Any]]:
    """Expected dividend payments in ``[today, today + months]`` (see the module docstring).

    Returns ``[{'date': date, 'per_share': float, 'amount': float}, ...]`` in date
    order; ``[]`` when nothing can honestly be forecast."""
    return forecast_dividends_ex(history, held_qty, today, months, specials, include_declared)[0]


# ---------------------------------------------------------------------------------------------
# declared (provider) history
# ---------------------------------------------------------------------------------------------

def parse_provider_history(payload: Any) -> Tuple[List[Tuple[date, float]], str]:
    """``([(payment date, declared per-share amount)], date_kind)`` from an FMP
    ``stock_dividend`` payload (``{"historical": [{"date": ex-date, "dividend", "paymentDate"}]}``).

    The PAYMENT date is used when every row has one; otherwise ex-dates (``date_kind`` is
    ``'ex'`` and the forecast dates are then ex-dates, a few days early). Rows without an
    amount are skipped."""
    rows = payload.get('historical') if isinstance(payload, dict) else payload
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    out_pay: List[Tuple[date, float]] = []
    out_ex: List[Tuple[date, float]] = []
    all_pay = True
    for r in rows:
        try:
            amt = float(r.get('dividend'))
            ex = _to_date(r.get('date'))
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        out_ex.append((ex, amt))
        try:
            out_pay.append((_to_date(r.get('paymentDate')), amt))
        except (TypeError, ValueError):
            all_pay = False
    if out_pay and all_pay:
        return sorted(out_pay), 'pay'
    return sorted(out_ex), 'ex'


def net_ratio(rows: Iterable[Dict[str, Any]]) -> Optional[float]:
    """The account's observed net / gross dividend ratio from its own rows (``amount`` is net,
    ``gross_amount`` gross); None when there is no row with both. Clamped to [0.3, 1.0]."""
    net = gross = 0.0
    for r in rows or []:
        try:
            a, g = float(r.get('amount')), float(r.get('gross_amount'))
        except (TypeError, ValueError):
            continue
        if g > 0 and a >= 0:
            net += a
            gross += g
    if gross <= 0:
        return None
    return min(1.0, max(0.3, net / gross))


# ---------------------------------------------------------------------------------------------
# broker metadata (TastyTrade market metrics)
# ---------------------------------------------------------------------------------------------

_PERIODS_PER_YEAR = {('days', 7): 52, ('months', 1): 12, ('months', 3): 4, ('months', 6): 2,
                     ('months', 12): 1}


def broker_forecast(meta: Optional[Dict[str, Any]], held_qty: float, today: Any, months: int,
                    step_hint: Optional[Tuple[str, int]], per_share_hint: Optional[float]):
    """``(events, info)`` from the broker's dividend metadata, or ``([], None)`` when it cannot
    be used honestly.

    ``meta``: ``{'rate': float, 'pay_date': date|None, 'next_date': date|None, ...}`` (see
    ``TastyTradeAccount.get_dividend_metadata``). The first forecast date is ``pay_date`` when it
    is today or later, else ``next_date``. The rate's MEANING is not assumed: it is checked
    against ``per_share_hint`` (the symbol's own recent per-share payout from another source) --
    a ratio near 1 = a per-payment amount, a ratio near the cadence's payments-per-year = an
    annual rate (divided by it). With no hint to check against the broker rate is NOT used.
    Without a cadence the one dated payment is projected alone.
    """
    today = _to_date(today)
    if not meta or not held_qty or float(held_qty) <= 0:
        return [], None
    try:
        rate = float(meta.get('rate'))
    except (TypeError, ValueError):
        return [], None
    if rate <= 0:
        return [], None
    first = None
    for key in ('pay_date', 'next_date'):
        d = meta.get(key)
        if d is not None:
            d = _to_date(d)
            if d >= today:
                first = d
                break
    if first is None or per_share_hint is None or per_share_hint <= 0:
        return [], None
    ratio = rate / per_share_hint
    step = step_hint
    per_payment = None
    if 0.7 <= ratio <= 1.4:
        per_payment = rate
    else:
        # an annual rate: it must match the KNOWN cadence's payments per year when there is one
        candidates = ([(step_hint, _PERIODS_PER_YEAR.get(step_hint))] if step_hint
                      else list(_PERIODS_PER_YEAR.items()))
        for st, n in candidates:
            if n and n > 1 and 0.7 * n <= ratio <= 1.4 * n:
                per_payment, step = rate / n, st
                break
    if per_payment is None:
        return [], None
    events: List[Dict[str, Any]] = []
    if first <= add_months(today, months):
        if step is not None:
            events = _project(first, step, per_payment, held_qty, today, months, first_is_anchor=True)
        else:
            events = [{'date': first, 'per_share': round(per_payment, 6),
                       'amount': round(per_payment * float(held_qty), 2)}]
    return events, {'status': 'ok', 'cadence': step, 'per_share': per_payment}
