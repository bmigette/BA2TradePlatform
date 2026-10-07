"""When is a dated item KNOWABLE at a backtest decision?  (the event-data half of the rule)

THE RULE. At a decision time T a reader may only return information that was published before T.
The daily-BAR half of the rule lives with the clock owner (``AsOfPriceSource.knowable_daily_end``).
This module is the half for EVENT data: earnings reports, SEC filings (statements, Form 4),
analyst grades and price targets, congressional disclosures.

WHY IT IS NEEDED. FMP stamps these with a DATE (sometimes a time), the readers parse them date-only
at 00:00 and keep everything ``<= as_of``. On an INTRADAY backtest clock (the classic GA runs:
decision 09:30 New York, fill at the next bar) an item dated D was therefore visible at D 09:30
whether it was published before the open or after the close. Measured live (prod replay store, 74
sessions): at 09:30 live received an ``amc`` earnings row dated D with its EPS still NULL, a
``bmo`` row dated D with EPS filled, a Form-4 filed 09:00:48 on D and none filed later, price
targets published before 11:49 UTC and none later, and (cache scan, 5,902 annual statements)
51% of statements are accepted after 16:00 on their filing day, 66% after 09:30.

THE CLOCK. Backtest 5-minute bars carry exchange-local wall-clock stamps labelled as UTC (09:30
is the New York open), so a decision ``as_of`` is a New York wall time and a New York wall time is
what every timestamp below is compared in; real UTC stamps (price targets) are converted into it.

SCOPE. The rules apply only while ``intraday_decision_clock()`` is on, which the backtest handler
switches on (thread-local, for the duration of one run) when the run's execution interval is
intraday. LIVE (``as_of`` None) and the DAILY clock keep the legacy ``<= as_of`` reading: on a
daily clock the bar stamped D decides after D's close and fills at D+1's open, so an item dated D
IS known (``reference-bt-5min-clock-same-day-close-lookahead``).

CONVENTIONS (per kind; where the source has only a DATE the after-close item must not be visible
on its own date, so the conservative reading is "from the NEXT session"):

  earnings report   ``time`` bmo           visible from its date            (reported pre-open)
                    ``time`` amc/--/None   visible from the NEXT session    (after close / unknown)
  statement         ``acceptedDate`` time  visible from that instant
                    date only              visible from the NEXT session
  Form 4            ``filingDate`` time    visible from that instant
                    date only              visible from the NEXT session
  analyst grade     date only              visible from the NEXT session
  price target      ``publishedDate`` (UTC timestamp) -> visible from that instant
  congress          ``disclosureDate``     visible from the NEXT session
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

#: The entry/decision time (exchange-local HH:MM) NEW stock backtests, grids and optimizations run
#: at, and therefore the time their deployed instances must fire at. The price at a decision is
#: the close of the latest 5-minute bar that has ENDED, so the decision must sit at least one bar
#: after the session's first bar: at 09:40 the 09:35 bar has ended (live reads a quote of that
#: moment). ONE definition: the launcher, the API, the robustness suite, the UI default
#: (frontend/src/lib/decisionTime.ts, pinned equal by a test) and the deploy tools use it.
DEFAULT_DECISION_TIME = "09:40"

#: The time every STORED row before 2026-10-07 ran at (the session's first bar). Used ONLY to
#: reconstruct such rows that carry no explicit time; never a default for a new run.
LEGACY_DECISION_TIME = "09:30"

_NY = ZoneInfo("America/New_York")
_tl = threading.local()


def intraday_decision_clock() -> bool:
    """True while the CURRENT THREAD runs a backtest on an intraday clock."""
    return bool(getattr(_tl, "active", False))


@contextmanager
def intraday_decisions(active: bool) -> Iterator[None]:
    """Mark this thread's backtest as running on an intraday clock (restores the prior value)."""
    prior = getattr(_tl, "active", False)
    _tl.active = bool(active)
    try:
        yield
    finally:
        _tl.active = prior


def _as_naive_wall(d: Any) -> datetime:
    if isinstance(d, datetime):
        return d.replace(tzinfo=None) if d.tzinfo is not None else d
    if isinstance(d, date):
        return datetime(d.year, d.month, d.day)
    raise TypeError(f"cannot read {d!r} as a decision time")


def next_session_start(day: Any) -> datetime:
    """Midnight of the day after ``day``: the earliest 09:30 decision that can see a date-only
    item published on ``day`` at an unknown time. (Calendar day, not session: a Friday item is
    visible from Saturday midnight, i.e. to Monday's decision, and nothing decides on Saturday.)"""
    w = _as_naive_wall(day)
    return datetime(w.year, w.month, w.day) + timedelta(days=1)


def earnings_visible_from(report_day: Any, slot: Optional[str]) -> datetime:
    """The first decision instant that may see an earnings row dated ``report_day``.

    ``bmo`` is reported before the open: visible at the day's own 09:30 decision. Anything else
    (``amc``, FMP's ``--`` placeholder, a missing slot) is not knowable at 09:30 of its date.
    Only meaningful while ``intraday_decision_clock()``; callers keep ``report_day`` otherwise."""
    w = _as_naive_wall(report_day)
    if str(slot or "").strip().lower() == "bmo":
        return datetime(w.year, w.month, w.day)
    return next_session_start(w)


def parse_wall_timestamp(value: Any) -> Optional[datetime]:
    """A provider timestamp -> naive New York wall time, or None when it carries only a date.

    ``"YYYY-MM-DD HH:MM:SS"`` / ``"YYYY-MM-DDTHH:MM:SS"`` are read as New York wall time (SEC
    acceptance times; verified: a Form 4 'filed 09:00:48' is before EDGAR opens in UTC but not in
    New York). A trailing ``Z`` / explicit offset is converted from that instant."""
    if value is None or value == "":
        return None
    s = str(value).strip()
    if len(s) <= 10:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return None   # a midnight stamp is a date padded to a timestamp, not a publication time
    if dt.tzinfo is not None:
        dt = dt.astimezone(_NY).replace(tzinfo=None)
    return dt


def filing_visible_from(value: Any) -> Optional[datetime]:
    """The first decision instant that may see a filing stamped ``value`` (a date or a timestamp),
    as a naive wall time; None when unparseable (the caller refuses or falls back explicitly)."""
    if value is None or value == "":
        return None
    ts = parse_wall_timestamp(value)
    if ts is not None:
        return ts
    try:
        day = datetime.fromisoformat(str(value).split("T")[0].split(" ")[0])
    except ValueError:
        return None
    return next_session_start(day)


def date_visible_from(value: Any) -> Optional[datetime]:
    """Date-only item (analyst grade, congressional disclosure): visible from the next session."""
    if value is None or value == "":
        return None
    try:
        return next_session_start(datetime.fromisoformat(str(value).split("T")[0].split(" ")[0]))
    except ValueError:
        return None


def published_known(raw: Any, decision: Any) -> bool:
    """Whether an item stamped ``raw`` (a date or a timestamp) was public at ``decision``: a
    timestamp is public from that instant, a bare date from the next session, an unparseable
    stamp never. Callers use it ONLY while ``intraday_decision_clock()``."""
    return known_at(filing_visible_from(raw), decision)


def known_at(visible_from: Optional[datetime], decision: Any) -> bool:
    """``visible_from <= decision`` on naive wall clocks; an unknown ``visible_from`` is NOT known
    (an item whose publication cannot be established is never silently admitted)."""
    if visible_from is None:
        return False
    return visible_from <= _as_naive_wall(decision)
