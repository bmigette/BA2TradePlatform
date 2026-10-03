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

    last_date = dates[-1]                      # the schedule runs from the last REAL payment date
    recent = dates[-OUTLIER_WINDOW:]
    clean = [d for d in dates if not (d in recent and is_outlier(
        by_date[d], [by_date[o] for o in recent if o != d]))]
    if not clean:
        return []
    last3 = [by_date[d] for d in clean[-3:]]
    mean3 = sum(last3) / len(last3)
    variable = len(last3) > 1 and mean3 > 0 and (max(last3) - min(last3)) / mean3 > VARIABLE_TOLERANCE
    per_share = median(last3) if variable else by_date[clean[-1]]

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
