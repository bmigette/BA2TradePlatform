"""Which OHLCV bars may be PERSISTED: only bars whose session / interval is FINAL.

THE BUG THIS PREVENTS (2026-10-07 data-refresh analysis). The daily top-up runs near 09:31 New York
and the vendor answers with TODAY's still-forming bar (one tick: ``Open == High``, ``Low ==
Close``). Nothing asked "is this session over?", so the snapshot was written to the shared parquet
cache and frozen there: every backtest then read a partial bar as history (312 flagged bars in 273
symbols since 2026-09-14, a lower bound) and the tail guard refused every later top-up for the
symbol, because a one-tick snapshot's open/high lie OUTSIDE the final day's range. The 5-minute
cache has the same defect: the top-up resumes at ``last_bar + 5 min``, so a persisted forming
5-minute bar is never fetched again.

THE RULE (one function, applied at the single OHLCV parquet writer, ``native_cache.write_timeseries``,
and again by the in-memory merge logic of the providers so they never treat a forming bar as history):

* a DAILY bar dated ``D`` is final at ``regular session close(D) + SETTLE_AFTER_CLOSE`` (13:00 ET on a
  half day), America/New_York, taken from ``ba2_common.core.market_calendar``;
* an INTRADAY bar is final when its interval has ended: ``label + interval <= now`` (the cached
  labels are New York wall-clock bar STARTS, verified on SPY_5min);
* a weekly / monthly bar is final when its whole period is over.

NON-US SYMBOLS. The shared cache holds ~5,000 symbols with an exchange suffix (``.HK`` ``.KS``
``.L`` ``.SZ`` ``.T`` ``.TW`` ...), and crypto / forex pairs. The NYSE calendar is NOT theirs, and this
module has no calendar for any other exchange. It never guesses one: for a symbol that does not use
the NYSE calendar, and for a date that is not an NYSE session, a bar is final only once the
calendar-free upper bound has passed: ``D + 1 day + 12 h + SETTLE_AFTER_CLOSE`` UTC. No exchange on
Earth is still in session for local date ``D`` after UTC-12 reaches midnight of ``D + 1``, so the
bound is safe for every calendar and costs a late (never an early) persist. If the NYSE calendar
itself cannot be built (``MarketCalendarUnavailable``) the same bound is used and an ERROR is logged.

SETTLEMENT DELAY. ``SETTLE_AFTER_CLOSE = 4 h``: the 20:00 ET convention the live repair already used
(``ohlcv_provisional.SETTLED_AFTER_HOUR_ET``); see the measurement in the commit message.

Pure functions apart from the clock (``_now_utc``, replaced by tests) and the market calendar.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Tuple

import pandas as pd

from ba2_common.core.market_calendar import (
    MarketCalendarUnavailable, NY_TZ, is_regular_session, nyse_regular_sessions,
    regular_session_close_utc,
)
from ba2_common.logger import logger

__all__ = [
    "SETTLE_AFTER_CLOSE",
    "UnknownIntervalError",
    "session_final_at",
    "session_is_final",
    "written_before_final",
    "split_unfinished",
    "drop_unfinished_bars",
    "uses_nyse_calendar",
    "now_utc",
    "interval_length",
    "is_daily_interval",
    "forming_session_day",
    "last_final_session_day",
    "live_overlay_enabled",
    "set_live_overlay_enabled",
    "forming_bar_status",
    "last_bar_is_today",
    "FORMING_STATUS_ATTR",
]

#: THE settlement constant: a session's bar stays unfinished this long after the regular close.
#: Measured on FMP (2026-10-07, 10 files written 16:45 ET on 2026-09-25, i.e. 45 min after the close,
#: against today's vendor bar): 9/10 equal in OHLC, volume still up to 5% short, one high/low 0.01 off,
#: so 45 minutes is NOT settled. Nothing between 17:00 and 20:00 ET was observed; 4 h is the 20:00 ET
#: convention of ``ohlcv_provisional.SETTLED_AFTER_HOUR_ET``. Every other 4-hour literal in this
#: module is an interval length, not this constant. Changing it changes which bars may be written.
SETTLE_AFTER_CLOSE = timedelta(hours=4)

#: The most-behind UTC offset on Earth (UTC-12): local midnight of ``D + 1`` is 12:00 UTC.
_LATEST_LOCAL_MIDNIGHT_UTC = timedelta(hours=12)

#: Intraday interval (canonical spelling) -> its length.
_INTRADAY_LENGTH = {
    "1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30), "1h": timedelta(hours=1), "4h": timedelta(hours=4),
}

#: A row whose label is this much older than now is final under every rule (bound: 12 h offset +
#: 4 h settle + 1 day): the per-row work is limited to the frame's tail.
_TAIL_WINDOW = timedelta(days=4)


#: A weekly / monthly bar can be unfinished for up to a month: its tail window is wider.
_PERIOD_TAIL_WINDOW = timedelta(days=45)

#: ``DataFrame.attrs`` key set by ``get_ohlcv_data`` on a LATEST daily frame: ``"present"`` (the last
#: row is today's forming bar), ``"missing"`` (a session is open / settling and no forming bar could
#: be obtained: the last row is an OLDER session, see :func:`last_bar_is_today`) or ``"not_expected"``.
FORMING_STATUS_ATTR = "ohlcv_forming_bar"

#: OPT-IN, default OFF (2026-10-07 live review). ON means: a LATEST daily read during a session forces a
#: vendor fetch (throttled by ``FORMING_BAR_TTL_S``) and overlays the one-tick forming bar on the returned
#: frame. Nothing in live consumes the forming bar (every expert prices from the account quote), the forced
#: fetch costs vendor calls at 09:30 on a rate-limited key, and an overlaid one-tick bar would become the
#: last row of an indicator frame that the backtest's history (finished sessions only) never has. OFF: a
#: LATEST daily read returns history up to the last FINAL session and uses the file-mtime freshness gate.
#: The write-side guard (never persist an unfinished bar) does NOT depend on this switch.
_LIVE_OVERLAY_ENABLED = False


def live_overlay_enabled() -> bool:
    """Whether this process serves forming bars to LATEST reads. Off unless a tool opts in."""
    return _LIVE_OVERLAY_ENABLED


def set_live_overlay_enabled(enabled: bool) -> None:
    """Process-wide switch (default OFF). ``MemoizedOHLCVProvider`` (every backtest's provider) sets it
    False: a backtest process never overlays or fetches a forming bar, whatever ``end_date`` its reads carry."""
    global _LIVE_OVERLAY_ENABLED
    _LIVE_OVERLAY_ENABLED = bool(enabled)


class UnknownIntervalError(ValueError):
    """An interval this module has no finality rule for. Refused, never guessed."""


def now_utc() -> datetime:
    """The clock of every finality decision (tz-aware UTC). Tests replace this ONE function."""
    return datetime.now(timezone.utc)


def uses_nyse_calendar(symbol: str) -> bool:
    """True for a symbol whose daily bars follow the NYSE session calendar: a plain US ticker
    (``AAPL``, ``BRK-B``). An exchange-suffixed symbol (``0700.HK``, ``VOD.L``) trades elsewhere.
    Crypto / forex pairs without a suffix are treated as US: they never print on an NYSE holiday
    bar-less, and their dates that are not NYSE sessions take the calendar-free bound anyway."""
    return "." not in str(symbol)


def _bound_final_at(day: date) -> datetime:
    """Calendar-free upper bound for the end of local date ``day`` on any exchange, plus settlement."""
    return (datetime.combine(day + timedelta(days=1), time(0), tzinfo=timezone.utc)
            + _LATEST_LOCAL_MIDNIGHT_UTC + SETTLE_AFTER_CLOSE)


def session_final_at(symbol: str, day: date) -> datetime:
    """The instant (tz-aware UTC) from which the DAILY bar of ``symbol`` dated ``day`` is final."""
    if uses_nyse_calendar(symbol):
        try:
            if is_regular_session(day):
                return regular_session_close_utc(day) + SETTLE_AFTER_CLOSE
        except MarketCalendarUnavailable as e:
            logger.error(f"NYSE calendar unavailable ({e}); the daily bar of {symbol} dated {day} "
                         f"is judged by the calendar-free bound instead (later, never earlier).")
    return _bound_final_at(day)


def session_is_final(symbol: str, day: date, now: Optional[datetime] = None) -> bool:
    """Whether the daily bar of ``symbol`` dated ``day`` may be persisted at ``now`` (default: the clock)."""
    now = now or now_utc()
    if day <= (now.astimezone(timezone.utc).date() - timedelta(days=2)):
        return True                              # past every rule's bound: no calendar lookup needed
    return now >= session_final_at(symbol, day)


def written_before_final(symbol: str, day: date, written: datetime) -> bool:
    """PROOF that a file last written at ``written`` captured the bar dated ``day`` while that
    session was still being traded or settling: the write falls inside ``[session open(day),
    final_at(day))``. A write before the session even began (a backdated / inconsistent mtime) proves
    nothing, and neither does one after the bar was final."""
    final_at = session_final_at(symbol, day)
    if written >= final_at:
        return False
    if uses_nyse_calendar(symbol):
        try:
            sessions = nyse_regular_sessions(day, day)
        except MarketCalendarUnavailable:
            sessions = []
        if sessions:
            return written >= sessions[0][0]
    # no NYSE session that day: the earliest local start of ``day`` anywhere is UTC+14
    return written >= datetime.combine(day, time(0), tzinfo=timezone.utc) - timedelta(hours=14)


def _day_labels(dates: pd.Series) -> pd.Series:
    """Session labels as tz-naive midnights (an aware stamp is read in UTC first, as the top-up
    guard's ``day_index`` does: FMP daily bars arrive as UTC midnights, the cache holds naive ones)."""
    d = pd.to_datetime(dates)
    if getattr(d.dt, "tz", None) is not None:
        d = d.dt.tz_convert("UTC").dt.tz_localize(None)
    return d.dt.normalize()


def _daily_mask(df: pd.DataFrame, symbol: str, now: datetime) -> pd.Series:
    """Boolean Series, True where the bar is FINAL, for a daily frame."""
    labels = _day_labels(df["Date"])
    cutoff = pd.Timestamp(now.astimezone(timezone.utc).date() - timedelta(days=2))
    final = labels <= cutoff
    for day in sorted(labels[~final].unique()):
        final |= (labels == day) & session_is_final(symbol, pd.Timestamp(day).date(), now)
    return final


def _period_mask(df: pd.DataFrame, interval: str, now: datetime) -> pd.Series:
    """Weekly / monthly bars (labelled with the period START): final once the period's last day's
    bound has passed. A label that is really the period END only delays the bar, never advances it."""
    labels = _day_labels(df["Date"])
    if interval == "1wk":
        end_excl = labels + pd.Timedelta(days=7)
    else:
        end_excl = labels.dt.to_period("M").dt.end_time.dt.normalize() + pd.Timedelta(days=1)
    bound = end_excl.dt.tz_localize("UTC") + pd.Timedelta(_LATEST_LOCAL_MIDNIGHT_UTC + SETTLE_AFTER_CLOSE)
    return bound <= pd.Timestamp(now)


def _intraday_mask(df: pd.DataFrame, symbol: str, interval: str, now: datetime) -> pd.Series:
    """Boolean Series, True where the bar's interval has ENDED, for an intraday frame.

    Naive labels are New York wall-clock bar starts for NYSE symbols (ambiguous fall-back hour read
    as the LATER instant). For any other symbol the label's zone is unknown: it is read at the
    latest offset on Earth (label + 12 h), so the bar is final late, never early."""
    length = _INTRADAY_LENGTH[interval]
    labels = pd.to_datetime(df["Date"])
    final = pd.Series(True, index=df.index)
    if getattr(labels.dt, "tz", None) is not None:
        # FMP intraday stamps are New York wall-clock text parsed with utc=True (tagged UTC, not
        # converted), and part of the cache holds them so. The wall-clock reading is the later of
        # the two possible instants, so it is the safe one whichever the tag really meant.
        labels = labels.dt.tz_localize(None)
    tail = labels >= pd.Timestamp(now.astimezone(timezone.utc).replace(tzinfo=None) - _TAIL_WINDOW)
    if not tail.any():
        return final
    part = labels[tail]
    if uses_nyse_calendar(symbol):
        instant = part.dt.tz_localize(NY_TZ, ambiguous=False, nonexistent="shift_forward").dt.tz_convert("UTC")
    else:
        instant = part.dt.tz_localize("UTC") + pd.Timedelta(_LATEST_LOCAL_MIDNIGHT_UTC)
    final[tail] = ((instant + length) <= pd.Timestamp(now)).to_numpy()
    return final


def final_mask(df: pd.DataFrame, symbol: str, interval: str, now: Optional[datetime] = None) -> pd.Series:
    """True per row where the bar is final at ``now``. Raises :class:`UnknownIntervalError` for an
    interval without a rule. Only the frame's TAIL is judged: every older row is final under every
    rule, so a 150,000-row 5-minute frame costs one datetime conversion and one comparison."""
    from ba2_common.core.native_cache import normalize_interval
    if "Date" not in df.columns:
        raise ValueError("an OHLCV frame needs a Date column to be judged for finality")
    now = now or now_utc()
    canon = normalize_interval(interval)
    if df.empty:
        return pd.Series([], dtype=bool, index=df.index)
    if canon not in _INTRADAY_LENGTH and canon not in ("1d", "1wk", "1mo"):
        raise UnknownIntervalError(
            f"no finality rule for interval {interval!r} (known: 1d, {sorted(_INTRADAY_LENGTH)}, 1wk, 1mo); "
            f"refusing to persist bars whose completeness cannot be judged")
    dates = pd.to_datetime(df["Date"])
    naive = dates.dt.tz_convert("UTC").dt.tz_localize(None) if getattr(dates.dt, "tz", None) is not None else dates
    span = _PERIOD_TAIL_WINDOW if canon in ("1wk", "1mo") else _TAIL_WINDOW
    tail = (naive >= pd.Timestamp(now.astimezone(timezone.utc).replace(tzinfo=None) - span)).to_numpy()
    out = pd.Series(True, index=df.index)
    if not tail.any():
        return out
    sub = df[tail]
    if canon == "1d":
        m = _daily_mask(sub, symbol, now)
    elif canon in _INTRADAY_LENGTH:
        m = _intraday_mask(sub, symbol, canon, now)
    else:
        m = _period_mask(sub, canon, now)
    out[tail] = m.to_numpy()
    return out


def is_daily_interval(interval: str) -> bool:
    from ba2_common.core.native_cache import normalize_interval
    return normalize_interval(interval) == "1d"


def interval_length(interval: str) -> Optional[timedelta]:
    """Length of an INTRADAY interval (any spelling), ``None`` for daily-or-longer / unknown."""
    from ba2_common.core.native_cache import normalize_interval
    return _INTRADAY_LENGTH.get(normalize_interval(interval))


def forming_session_day(symbol: str, now: Optional[datetime] = None) -> Optional[date]:
    """The NYSE session date whose daily bar is FORMING at ``now`` -- the session is open or still
    settling (``open <= now < final``) -- else ``None`` (before the open, after the settlement, a
    weekend / holiday, or a symbol that does not use the NYSE calendar)."""
    now = now or now_utc()
    if not uses_nyse_calendar(symbol):
        return None
    day = now.astimezone(NY_TZ).date()
    try:
        sessions = nyse_regular_sessions(day, day)
    except MarketCalendarUnavailable:
        return None
    if not sessions:
        return None
    opens = sessions[0][0]
    return day if opens <= now < session_final_at(symbol, day) else None


def last_final_session_day(symbol: str, now: Optional[datetime] = None) -> Optional[date]:
    """The newest NYSE session whose bar is FINAL at ``now`` (``None`` for a non-NYSE symbol)."""
    now = now or now_utc()
    if not uses_nyse_calendar(symbol):
        return None
    day = now.astimezone(NY_TZ).date() + timedelta(days=1)
    for _ in range(15):
        day -= timedelta(days=1)
        try:
            ok = is_regular_session(day)
        except MarketCalendarUnavailable:
            return None
        if ok and now >= session_final_at(symbol, day):
            return day
    return None


def forming_bar_status(frame) -> Optional[str]:
    """The status ``get_ohlcv_data`` stamped on a LATEST daily frame (see ``FORMING_STATUS_ATTR``),
    ``None`` when the frame did not come from there."""
    return getattr(frame, "attrs", {}).get(FORMING_STATUS_ATTR)


def last_bar_is_today(frame, symbol: str = "", now: Optional[datetime] = None) -> bool:
    """For a live caller that prices off the frame's last close: True when the last row is the bar of
    the CURRENT New York date (the forming bar was obtained). False when it is an older session, which
    during an open session means the forming bar could not be fetched (``get_ohlcv_data`` has already
    logged an ERROR). Judges the frame itself, not an attribute."""
    if frame is None or len(frame) == 0:
        return False
    last = pd.Timestamp(pd.to_datetime(frame["Date"]).max())
    if last.tzinfo is not None:
        last = last.tz_convert("UTC").tz_localize(None)
    now = now or now_utc()
    return last.date() == now.astimezone(NY_TZ).date()


def split_unfinished(df: pd.DataFrame, symbol: str, interval: str,
                     now: Optional[datetime] = None) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """``(final_rows, unfinished_rows)``. ``final_rows`` is ``df`` itself when nothing is unfinished."""
    if df is None or df.empty:
        return df, (df.iloc[0:0] if df is not None else df)
    mask = final_mask(df, symbol, interval, now)
    if bool(mask.all()):
        return df, df.iloc[0:0]
    return df[mask.to_numpy()], df[~mask.to_numpy()]


def drop_unfinished_bars(df: pd.DataFrame, symbol: str, interval: str,
                         now: Optional[datetime] = None) -> Tuple[pd.DataFrame, List[str]]:
    """``(frame without unfinished bars, ISO labels of the dropped bars)``."""
    keep, gone = split_unfinished(df, symbol, interval, now)
    return keep, [pd.Timestamp(d).isoformat() for d in gone["Date"]] if len(gone) else []
