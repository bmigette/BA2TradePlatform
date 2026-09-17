"""Holiday- and half-day-aware NYSE regular-session calendar (pure, offline).

This is the ONLY NYSE session-time source in the codebase; no adapter hardcodes
09:30/16:00/13:00 and no adapter walks its own holiday list.

It is the FALLBACK behind ``ReadOnlyAccountInterface.get_market_hours()``: the
answer used when the broker publishes no market-hours endpoint, when its adapter
has not implemented one yet, or when the broker's own lookup failed.

REGULAR SESSION ONLY -- 09:30-16:00 ET, 09:30-13:00 ET on a half day. This
returns False during Alpaca's extended-hours window. That is exactly the
semantics the Portfolio Allocation wizard's submit gate wants, but it must not be
read as "can I trade at all".

Modelled on ``tastytrade/utils.py:44-57`` ``is_market_open_now()``, but
reimplemented rather than imported: that helper is a vendor SDK internal tied to
the ``tastytrade==12.0.2`` pin, it answers a bare bool (no session bounds, no
next open/close), and it reads the wall clock itself, so nothing can freeze time
around it.

Offline: ``pandas_market_calendars`` ships the NYSE holiday and half-day rules as
DATA, so no network call is ever made here.
"""
import threading
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np

from ba2_common.core.account_types import (
    MARKET_HOURS_SOURCE_FALLBACK,
    MarketHours,
)
from ba2_common.logger import logger

#: The exchange timezone, and THE canonical NY tz object for the packages and for
#: the UI's ``format_market_time``. ``America/New_York`` (not the ``US/Eastern``
#: alias tastytrade uses) because that is the canonical IANA name; both resolve to
#: the same rules. ZoneInfo needs the ``tzdata`` wheel on Windows -- guaranteed
#: here by the ``pandas<3`` pin in requirements.txt (pandas requires tzdata>=2022.7).
NY_TZ = ZoneInfo("America/New_York")

#: How far ahead to look for the next session. The longest NYSE closure is four
#: calendar days (a Friday or Monday holiday plus the weekend), so ten days always
#: contains at least one future session.
LOOKAHEAD_DAYS = 10

#: Memoised NYSE calendar. Building it parses the whole holiday ruleset, so it is
#: built once per process, LAZILY -- importing this module must not cost that
#: (``tastytrade/utils.py:15`` does it at module import; we deliberately do not).
_CALENDAR: Any = None


class MarketCalendarUnavailable(RuntimeError):
    """``pandas-market-calendars`` could not be imported or built.

    Raised, never swallowed here: the CALLER decides the safe direction. Every
    caller in this codebase fails CLOSED -- ``_get_market_hours_impl`` converts it
    to ``MARKET_HOURS_SOURCE_UNAVAILABLE``, which is shut for the submit gate and
    "unknown" for the banner.
    """


def _nyse_calendar() -> Any:
    """The memoised NYSE calendar.

    Raises:
        MarketCalendarUnavailable: when ``pandas-market-calendars`` is missing or
            unusable. It is pinned in requirements.txt and in
            packages/common/pyproject.toml, but it originally arrived only as a
            TRANSITIVE dep of ``tastytrade``, so this failure is worth naming.
    """
    global _CALENDAR
    if _CALENDAR is None:
        try:
            from pandas_market_calendars import get_calendar
            _CALENDAR = get_calendar("NYSE")
        except Exception as e:
            logger.error(f"NYSE market calendar unavailable: {e}", exc_info=True)
            raise MarketCalendarUnavailable(
                f"pandas-market-calendars could not provide the NYSE calendar: {e}") from e
    return _CALENDAR


def clear_nyse_calendar_cache() -> None:
    """Drop the memoised calendar so the next call rebuilds it.

    The holiday ruleset only changes when the package is upgraded, so this exists
    for tests and for a long-lived process that has had the package replaced
    underneath it. It is NOT the market-status cache -- that one lives on the
    account (``ReadOnlyAccountInterface.clear_market_hours_cache``).

    The computed SCHEDULES go too: they are derived from this calendar, so keeping them
    would serve the old holiday ruleset forever from a cache the caller just cleared.
    """
    global _CALENDAR
    _CALENDAR = None
    _nyse_sessions_memo.cache_clear()
    _sessions_ending_at_memo.cache_clear()
    _clear_session_table()
    for hook in list(_CACHE_CLEAR_HOOKS):
        hook()


#: Callables run by ``clear_nyse_calendar_cache``: memos in modules that import this one (and so
#: cannot be imported from here) register their ``cache_clear`` so no derived answer outlives the
#: calendar it was computed from.
_CACHE_CLEAR_HOOKS: List[Any] = []


def register_calendar_cache_clear_hook(fn: Any) -> None:
    """Run ``fn()`` whenever ``clear_nyse_calendar_cache`` runs (idempotent per callable)."""
    if fn not in _CACHE_CLEAR_HOOKS:
        _CACHE_CLEAR_HOOKS.append(fn)


def _require_aware(moment: datetime) -> datetime:
    """Normalise a caller-supplied instant to UTC, refusing a naive one.

    A naive datetime here is not a small ambiguity: read as UTC it moves the
    session boundary by four or five hours, which is the difference between
    "submit" and "the broker rejects the whole batch". No guess is made.

    Raises:
        ValueError: when ``moment`` has no usable tzinfo.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            "market-hours instants must be timezone-aware; a naive datetime "
            "would silently shift the 09:30/16:00 ET boundaries")
    return moment.astimezone(timezone.utc)


#: How many distinct day-ranges keep their computed schedule. `_nyse_calendar` memoises the
#: calendar OBJECT; `.schedule()` is where pandas_market_calendars actually works (~10ms a
#: call), and it was recomputed every time. On the option backtest path that was 40% of a
#: trial's wall-clock: `BacktestAccount._iv_rank_sample_dates` asks for one trailing window
#: per iv_rank evaluation, so a run walks a sliding range and re-derives neighbouring windows
#: that overlap by all but a day or two.
#:
#: BOUNDED, because the keys are unbounded: a backtest generates one range per analysis bar
#: (hundreds over a multi-year window) and a live process one per day, forever. 512 ranges of
#: ~250 sessions is a few MB and covers a whole trial's sliding window without eviction.
_SESSIONS_MEMO_SIZE = 512


@lru_cache(maxsize=_SESSIONS_MEMO_SIZE)
def _nyse_sessions_memo(first_day: date, last_day: date) -> Tuple[Tuple[datetime, datetime], ...]:
    """``nyse_regular_sessions`` without the defensive copy. Returns a TUPLE.

    Immutable on purpose: a cached list handed to two callers lets the first one's ``pop()``
    silently delete a trading session for every later caller, turning a read into a write.
    The public function copies into a fresh list, which is microseconds against the ~10ms
    this avoids.
    """
    schedule = _nyse_calendar().schedule(start_date=first_day, end_date=last_day)
    sessions = [
        (row.market_open.to_pydatetime().astimezone(timezone.utc),
         row.market_close.to_pydatetime().astimezone(timezone.utc))
        for row in schedule.itertuples()
    ]
    sessions.sort()
    return tuple(sessions)


def nyse_regular_sessions(first_day: date, last_day: date) -> List[Tuple[datetime, datetime]]:
    """Regular-session ``(open, close)`` pairs, in UTC, over an inclusive day range.

    Holiday and half-day handling both fall out of the DATA rather than needing
    rules here: ``pandas_market_calendars`` emits no row at all for a weekend or a
    holiday, and a half day carries its real early close (13:00 ET).

    Args:
        first_day: inclusive first calendar day (exchange-local).
        last_day: inclusive last calendar day (exchange-local).

    Returns:
        List[Tuple[datetime, datetime]]: ascending, tz-aware UTC. Empty when the
        range contains no trading day.

    Raises:
        MarketCalendarUnavailable: see ``_nyse_calendar``.
    """
    return list(_nyse_sessions_memo(first_day, last_day))


def nyse_market_hours(now: Optional[datetime] = None) -> MarketHours:
    """Regular-session market status at ``now``, from the NYSE calendar.

    The session is open-INCLUSIVE and close-EXCLUSIVE (``open <= now < close``),
    matching ``tastytrade/utils.py:56``: 09:30:00 ET is open, 16:00:00 ET is not.

    Args:
        now: the instant to describe; must be timezone-aware. Defaults to
            ``datetime.now(timezone.utc)``. Callers that need a reproducible
            answer (every test, and the account seam's cache) pass it explicitly.

    Returns:
        MarketHours: ``source == MARKET_HOURS_SOURCE_FALLBACK``, ``status`` left
        ``None`` (that field is the BROKER's word and there is no broker here).
        When the market is shut, ``open_at``/``close_at`` describe the UPCOMING
        session and so equal ``next_open``/``next_close``.

    Raises:
        ValueError: when ``now`` is naive.
        MarketCalendarUnavailable: see ``_nyse_calendar``.
    """
    now_utc = _require_aware(now if now is not None else datetime.now(timezone.utc))

    # Start a day early so a session still running past midnight UTC (any EDT
    # afternoon) is still in the window.
    first_day = now_utc.astimezone(NY_TZ).date() - timedelta(days=1)
    sessions = nyse_regular_sessions(first_day, first_day + timedelta(days=LOOKAHEAD_DAYS))

    current = next(((o, c) for o, c in sessions if o <= now_utc < c), None)
    next_open = next((o for o, _ in sessions if o > now_utc), None)
    next_close = next((c for _, c in sessions if c > now_utc), None)

    return MarketHours(
        is_open=current is not None,
        open_at=current[0] if current is not None else next_open,
        close_at=current[1] if current is not None else next_close,
        next_open=next_open,
        next_close=next_close,
        source=MARKET_HOURS_SOURCE_FALLBACK,
        as_of=now_utc,
    )


# ---------------------------------------------------------------------------
# Session-label arithmetic for the market-condition gates (design
# ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` section 4,
# ``prior_session_v1``).
#
# ONE wide session table, built with a single ``schedule()`` call and answered by binary search.
# The first version derived every answer from a fresh ``nyse_regular_sessions`` range: each
# (session, n) and each prior-session lookup was a distinct range key, so a backtest walking
# 1508 sessions missed the 512-entry range memo 4525 times -- measured 101.8 s cold, ~20 s per
# trial ongoing. The table costs one schedule call per process, then O(log n) lookups. The same
# pandas_market_calendars data answers both, so holidays and half days are unchanged.
# ---------------------------------------------------------------------------

#: Default span of the session table; grown automatically when a query falls outside it.
_TABLE_FIRST_DAY = date(1990, 1, 1)
_TABLE_FUTURE_DAYS = 2 * 366
#: Floor for backward growth.
_TABLE_MIN_DAY = date(1885, 1, 1)

_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)
_TABLE_LOCK = threading.Lock()


class _SessionTable:
    """Sorted session dates (``datetime64[D]``) with their UTC close instants (int64 ns) over
    the inclusive calendar-day span ``[first_day, last_day]``. Immutable once built."""

    __slots__ = ("first_day", "last_day", "days", "closes_ns")

    def __init__(self, first_day: date, last_day: date):
        schedule = _nyse_calendar().schedule(start_date=first_day, end_date=last_day)
        self.first_day = first_day
        self.last_day = last_day
        self.days = np.asarray(schedule.index.values).astype("datetime64[D]")
        closes = schedule["market_close"]
        if getattr(closes.dt, "tz", None) is not None:
            closes = closes.dt.tz_convert("UTC").dt.tz_localize(None)
        self.closes_ns = closes.to_numpy(dtype="datetime64[ns]").astype(np.int64)


_TABLE: Optional[_SessionTable] = None


def _session_table(covering: date, history_from: Optional[date] = None) -> _SessionTable:
    """The session table, (re)built so its span contains ``covering`` (and, when given, starts on
    or before ``history_from``). Rebuilding widens the span; it never narrows it."""
    global _TABLE
    table = _TABLE
    want_first = covering if history_from is None else min(covering, history_from)
    if table is not None and table.first_day <= max(want_first, _TABLE_MIN_DAY) \
            and covering <= table.last_day:
        return table
    with _TABLE_LOCK:
        table = _TABLE
        if table is not None and table.first_day <= max(want_first, _TABLE_MIN_DAY) \
                and covering <= table.last_day:
            return table
        if table is None:
            first, last = _TABLE_FIRST_DAY, date.today() + timedelta(days=_TABLE_FUTURE_DAYS)
        else:
            first, last = table.first_day, table.last_day
        first = max(_TABLE_MIN_DAY, min(first, want_first - timedelta(days=366)))
        last = max(last, covering + timedelta(days=_TABLE_FUTURE_DAYS))
        _TABLE = _SessionTable(first, last)
        _sessions_ending_at_memo.cache_clear()
        return _TABLE


def _clear_session_table() -> None:
    global _TABLE
    with _TABLE_LOCK:
        _TABLE = None


def _decision_local_date(decision: Any) -> date:
    """The exchange-local calendar date a decision belongs to.

    A tz-aware datetime is converted to America/New_York; a NAIVE datetime is refused (no
    timezone is guessed, design section 4); a plain ``date`` is the session label itself.
    """
    if isinstance(decision, datetime):
        if decision.tzinfo is None or decision.tzinfo.utcoffset(decision) is None:
            raise ValueError(
                f"decision time must be timezone-aware, got naive {decision!r}; "
                "a guessed timezone would move the session boundary")
        return decision.astimezone(NY_TZ).date()
    if isinstance(decision, date):
        return decision
    raise TypeError(f"decision must be a datetime or a date, got {type(decision).__name__}")


def _require_session_date(session: Any) -> date:
    if isinstance(session, datetime) or not isinstance(session, date):
        raise TypeError(f"session must be a date, got {type(session).__name__}")
    return session


def _session_index(table: _SessionTable, session: date) -> int:
    d64 = np.datetime64(session, "D")
    i = int(np.searchsorted(table.days, d64, side="left"))
    if i >= len(table.days) or table.days[i] != d64:
        raise ValueError(f"{session} is not a regular NYSE session")
    return i


def prior_regular_session(decision: Any) -> date:
    """The last regular NYSE session STRICTLY BEFORE the decision's exchange-local date.

    ``prior_session_v1``: a decision on local date D reads the completed bar of the session
    before D -- also after D's close (the policy is stable for the whole local date), and also
    when D itself is not a session (a Saturday decision reads Friday).

    Args:
        decision: a tz-aware ``datetime`` (converted to America/New_York first) or a ``date``
            (a daily backtest's session label, used as-is).

    Raises:
        ValueError: a naive datetime, or no session before the calendar's first day.
        TypeError: anything that is not a date/datetime.
        MarketCalendarUnavailable: see ``_nyse_calendar``.
    """
    local_day = _decision_local_date(decision)
    table = _session_table(local_day)
    d64 = np.datetime64(local_day, "D")
    i = int(np.searchsorted(table.days, d64, side="left")) - 1
    if i < 0:
        table = _session_table(local_day, local_day - timedelta(days=4 * LOOKAHEAD_DAYS))
        i = int(np.searchsorted(table.days, d64, side="left")) - 1
        if i < 0:
            raise ValueError(f"no regular NYSE session before {local_day}")
    return table.days[i].astype(object)


@lru_cache(maxsize=4096)
def _sessions_ending_at_memo(session: date, n: int) -> Tuple[date, ...]:
    table = _session_table(session)
    i = _session_index(table, session)
    if i + 1 < n:
        # ~1.45 calendar days per session: 3x plus a margin reaches far enough in one regrowth.
        table = _session_table(session, session - timedelta(days=3 * n + 30))
        i = _session_index(table, session)
        if i + 1 < n:
            raise ValueError(f"fewer than {n} regular NYSE sessions end at {session}")
    return tuple(table.days[i - n + 1:i + 1].astype(object).tolist())


def regular_sessions_ending_at_tuple(session: date, n: int) -> Tuple[date, ...]:
    """``regular_sessions_ending_at`` as the memoised immutable tuple (no copy)."""
    _require_session_date(session)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"n must be a positive int, got {n!r}")
    return _sessions_ending_at_memo(session, n)


def regular_sessions_ending_at(session: date, n: int) -> List[date]:
    """The ``n`` regular-session dates ending at ``session`` INCLUSIVE, ascending.

    Returns a fresh list (the memo holds a tuple, so no caller can edit another's answer).

    Raises:
        ValueError: ``session`` is not a regular session, ``n < 1``, or the calendar holds fewer
            than ``n`` sessions ending there.
        TypeError: ``session`` is a datetime or not a date (a session label is a calendar day).
    """
    return list(regular_sessions_ending_at_tuple(session, n))


def regular_session_close_utc(session: date) -> datetime:
    """The regular close (tz-aware UTC) of ``session``: 16:00 ET, 13:00 ET on a half day.

    Raises:
        ValueError: ``session`` is not a regular session.
    """
    _require_session_date(session)
    table = _session_table(session)
    ns = int(table.closes_ns[_session_index(table, session)])
    return _EPOCH_UTC + timedelta(microseconds=ns // 1000)


def regular_session_dates(first_day: date, last_day: date) -> List[date]:
    """Every regular NYSE session DATE in the inclusive range ``[first_day, last_day]``, ascending.

    The date-shaped sibling of :func:`nyse_regular_sessions` (same memoised schedule, so asking
    for the dates costs nothing extra once the pairs are built). A snapshot-coverage check needs
    "which sessions does this window contain", not their open/close instants.

    Returns an empty list when the range contains no session, INCLUDING when ``last_day`` is
    before ``first_day`` -- an empty range is a legitimate question with an empty answer, and the
    calendar would otherwise raise on the inverted span.
    """
    if last_day < first_day:
        return []
    return [open_utc.astimezone(NY_TZ).date()
            for open_utc, _close in _nyse_sessions_memo(first_day, last_day)]
