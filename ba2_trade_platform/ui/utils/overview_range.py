"""The Overview page's single time-range setting (3m / 6m / YTD / 1y / 3y / Max).

ONE pure function, :func:`resolve_range_start`, turns the choice into a start date;
every chart's date filtering goes through it (via :func:`filter_dates`,
:func:`filter_months`, :func:`date_in_range`), so no chart can disagree about where
"YTD" starts.

Persisted in the DATABASE under one app-setting key (a page-level value, not per
account) so a phone and a PC see the same choice and it survives a restart.
"""
import calendar
from datetime import date, datetime
from typing import Any, Iterable, List, Optional

from .overview_label_scope import read_overview_setting, write_overview_setting

RANGE_OPTIONS = ['3m', '6m', 'YTD', '1y', '3y', 'Max']
DEFAULT_RANGE = 'YTD'
RANGE_SETTING_KEY = 'overview_time_range'


def _minus_months(today: date, months: int) -> date:
    """``today`` minus whole calendar months, clamped to the target month's last day
    (31 May - 3m = 28/29 Feb, 29 Feb - 12m = 28 Feb)."""
    total = today.year * 12 + (today.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def resolve_range_start(range_key: str, today: date) -> Optional[date]:
    """First day included by ``range_key``; ``None`` for ``'Max'`` (no lower bound).

    ``3m``/``6m``/``1y``/``3y`` are calendar offsets back from ``today`` (month-end
    clamped); ``YTD`` is 1 January of today's year (1 December of the previous year on
    1-7 January, when the year has fewer than two completed trading days). An unknown key raises -- a typo
    must not silently become "everything".
    """
    if isinstance(today, datetime):
        today = today.date()
    if range_key == 'Max':
        return None
    if range_key == 'YTD':
        # In the first week of January the year has fewer than two COMPLETED trading days
        # (1 Jan is a holiday; 2026-01-04/05 have one, 2027-01-04 none), so a Jan-1 start
        # would leave every chart blank or a single point. Through 7 January the start
        # therefore reaches back to 1 December of the previous year; from 8 January on it
        # is plain 1 January.
        if today.month == 1 and today.day <= 7:
            return date(today.year - 1, 12, 1)
        return date(today.year, 1, 1)
    offsets = {'3m': 3, '6m': 6, '1y': 12, '3y': 36}
    if range_key not in offsets:
        raise ValueError(f"Unknown time range {range_key!r}; expected one of {RANGE_OPTIONS}")
    return _minus_months(today, offsets[range_key])


def effective_range_start(range_start: Optional[date], earliest: Optional[date]) -> Optional[date]:
    """The start charts actually use: the range start, but NEVER earlier than the account's
    first balance / trade / dividend date (``Max`` -- ``range_start is None`` -- starts there
    instead of at the oldest listing date of a symbol, decades of zeros)."""
    if range_start is None:
        return earliest
    if earliest is None:
        return range_start
    return max(range_start, earliest)


def normalize_range(value: Any) -> str:
    """A stored value -> a valid key; anything else (absent, corrupt, retired) -> the default."""
    return value if value in RANGE_OPTIONS else DEFAULT_RANGE


def yf_period_for(range_key: str) -> str:
    """The yfinance ``period`` that covers the range (3m/6m share the old 6mo fetch)."""
    return {'3m': '6mo', '6m': '6mo', 'YTD': '1y', '1y': '1y', '3y': '3y', 'Max': 'max'}[range_key]


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


def read_range() -> str:
    return normalize_range(read_overview_setting(RANGE_SETTING_KEY))


def write_range(range_key: str) -> bool:
    return write_overview_setting(RANGE_SETTING_KEY, normalize_range(range_key))


def history_capped(range_start: Optional[date], balance_first: Optional[date],
                   activity_first: Optional[date], slack_days: int = 30) -> bool:
    """Did the broker return LESS balance history than the range asks for?

    True when the first balance snapshot is more than ``slack_days`` later than the point the
    range wants (the range start, but never before the account's first trade / dividend --
    an account younger than the range is not "capped"). Alpaca returns 1A and TastyTrade 365
    days by default, so 3y / Max can otherwise look like they cover more than they do.
    """
    if balance_first is None or activity_first is None:
        return False
    wanted = max(range_start, activity_first) if range_start else activity_first
    return (balance_first - wanted).days > slack_days
