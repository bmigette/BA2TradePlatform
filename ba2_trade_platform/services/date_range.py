"""Date-range helpers shared by the Account Growth tab and the management API (no UI imports)."""
from datetime import date, datetime
from typing import Any, Iterable, List, Optional


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def date_in_range(value: Any, start: Optional[date]) -> bool:
    """Is ``value`` (date, datetime or ISO string) on/after ``start``? Unparseable -> False."""
    if start is None:
        return True
    d = _as_date(value)
    return d is not None and d >= start


def filter_dates(dates: Iterable[str], start: Optional[date]) -> List[str]:
    """ISO date strings on/after ``start`` (order kept)."""
    return [d for d in dates if date_in_range(d, start)]


def filter_months(months: Iterable[str], start: Optional[date]) -> List[str]:
    """``YYYY-MM`` keys of months that overlap the range (the start's own month stays)."""
    if start is None:
        return list(months)
    floor = start.strftime('%Y-%m')
    return [m for m in months if str(m) >= floor]
