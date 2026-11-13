"""The allocator's "rebalance due" reminder: is the last completed rebalance older than the account's interval? Pure --
no NiceGUI, no database, no clock (``today`` is passed in).

The reminder only REMINDS. Its button is the page's existing "Review and Submit" control: the same dry run, the same
order preview, the same Submit the owner must press. Nothing here places, schedules or pre-accepts an order.

``last_run_at`` is the ``created_at`` (UTC) of the account's latest REBALANCE run that created at least one order
(``core.allocator_rebalance_reminder.last_rebalance_run_at``); ``None`` means no such run exists, and the reminder then
says so instead of inventing a date.

ONE CLOCK: every date here is a UTC date. ``created_at`` is stored in UTC (SQLite drops the tzinfo, so a NAIVE datetime
is read as UTC; an aware one is converted), and the caller passes ``today`` as the UTC date
(``datetime.now(timezone.utc).date()``), never the local ``date.today()`` -- mixing the two puts the day count off by
one for part of every day.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional, Union


@dataclass(frozen=True)
class RebalanceReminder:
    due: bool
    interval_months: int
    #: None = no completed rebalance run on record.
    last_run_date: Optional[date]
    days_elapsed: Optional[int]
    due_date: Optional[date]
    text: str


def utc_date(moment: Union[date, datetime]) -> date:
    """The UTC calendar date of ``moment``: an aware datetime is converted to UTC, a naive one is taken AS UTC (how the
    database hands back ``created_at``), a plain date is returned unchanged."""
    if isinstance(moment, datetime):
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc)
        return moment.date()
    return moment


def add_months(day: date, months: int) -> date:
    """``day`` plus ``months`` calendar months, the day clipped to the target month's length (31 Jan + 1 = 28/29 Feb)."""
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def rebalance_reminder(last_run_at: Optional[Union[date, datetime]], interval_months: int,
                       today: date) -> RebalanceReminder:
    """Due when ``today`` (a UTC date) is on or after the last run's UTC date plus ``interval_months`` calendar months,
    or when there is no run at all. Raises ``ValueError`` for an interval that is not a positive whole number of months."""
    if isinstance(interval_months, bool) or not isinstance(interval_months, int) or interval_months < 1:
        raise ValueError(f"The reminder interval must be a whole number of months, 1 or more, not {interval_months!r}")
    every = f"{interval_months} month{'s' if interval_months != 1 else ''}"
    if last_run_at is None:
        return RebalanceReminder(
            due=True, interval_months=interval_months, last_run_date=None, days_elapsed=None, due_date=None,
            text=f"No completed rebalance run is recorded for this account (reminder interval: every {every}).")
    last = utc_date(last_run_at)
    elapsed = (today - last).days
    due_date = add_months(last, interval_months)
    due = today >= due_date
    if due:
        text = (f"Rebalance due: the last rebalance run was on {last.isoformat()} (UTC), {elapsed} day(s) ago "
                f"(reminder interval: every {every}).")
    else:
        text = (f"Last rebalance run on {last.isoformat()} (UTC), {elapsed} day(s) ago; the next reminder is due on "
                f"{due_date.isoformat()} (every {every}).")
    return RebalanceReminder(due=due, interval_months=interval_months, last_run_date=last, days_elapsed=elapsed,
                             due_date=due_date, text=text)
