"""Dividend forecast for the next N months, from a symbol's own payment history.

One pure function, :func:`forecast_dividends`. No invented numbers: a symbol with
too little history, an irregular cadence or a stale last payment gets NO forecast.

The rule
--------
* ``history``: ``(date, per_share_amount)`` pairs. Several entries on one date are
  summed (a regular plus a special payment on the same day is one payment).
* Cadence: the MEDIAN gap (days) between the last four payments, classified as
  weekly (<=10d), monthly (25-35), quarterly (80-100), semi-annual (170-195) or
  annual (350-380). Anything else is irregular -> no forecast. At least two
  payments are needed to see a cadence.
* Amount per share: the last payment; if the last three differ by more than 10%
  of their mean (a variable payer) the mean of the last three.
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
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: (min_gap_days, max_gap_days, step) -- step is ('days', n) or ('months', n).
_CADENCES = (
    (1, 10, ('days', 7)),
    (25, 35, ('months', 1)),
    (80, 100, ('months', 3)),
    (170, 195, ('months', 6)),
    (350, 380, ('months', 12)),
)
VARIABLE_TOLERANCE = 0.10


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


def detect_cadence(dates: List[date]) -> Optional[Tuple[str, int]]:
    if len(dates) < 2:
        return None
    recent = dates[-5:]
    gaps = [(b - a).days for a, b in zip(recent, recent[1:])]
    gap = median(gaps)
    for lo, hi, step in _CADENCES:
        if lo <= gap <= hi:
            return step
    return None


def forecast_dividends(history: Iterable[Tuple[Any, float]], held_qty: float,
                       today: Any, months: int = 2) -> List[Dict[str, Any]]:
    """Expected dividend payments in ``[today, today + months]``.

    Returns ``[{'date': date, 'per_share': float, 'amount': float}, ...]`` in date
    order; ``[]`` when nothing can honestly be forecast. See the module docstring.
    """
    today = _to_date(today)
    if not held_qty or float(held_qty) <= 0:
        return []
    by_date: Dict[date, float] = {}
    for d, amt in history or []:
        if d is None or amt is None:
            continue
        amt = float(amt)
        if amt <= 0:
            continue
        key = _to_date(d)
        by_date[key] = by_date.get(key, 0.0) + amt
    dates = sorted(by_date)
    step = detect_cadence(dates)
    if step is None:
        return []

    last_date = dates[-1]
    last3 = [by_date[d] for d in dates[-3:]]
    mean3 = sum(last3) / len(last3)
    variable = len(last3) > 1 and mean3 > 0 and (max(last3) - min(last3)) / mean3 > VARIABLE_TOLERANCE
    per_share = mean3 if variable else by_date[last_date]

    period = _step_days(step)
    if (today - last_date).days >= 2 * period:      # silent for over a full extra period
        return []

    end = add_months(today, months)
    out: List[Dict[str, Any]] = []
    k = 1
    while True:
        due = _advance(last_date, step, k, last_date)
        if due > end:
            break
        k += 1
        if due < today:
            # one period overdue at most (guarded above): paid late, so "today";
            # only the first missed one is moved, the schedule itself is unchanged
            due = today if not out else due
            if due < today:
                continue
        out.append({'date': due, 'per_share': round(per_share, 6),
                    'amount': round(per_share * float(held_qty), 2)})
    return out
