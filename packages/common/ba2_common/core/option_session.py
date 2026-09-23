"""Session-scoped option-contract facts shared by the backtest and live (BT/live option parity).

A decision reads ONE completed session (``market_calendar.decision_data_session``). Anything a
strategy asks about a contract "as of" that decision -- here, how much it traded -- must be taken
from exactly that session in both paths, or the backtest and live see different liquidity for the
same decision. This module holds those rules so the two callers cannot drift apart.
"""
from datetime import date, datetime
from typing import Any, Iterable, Optional, Tuple


def _require_plain_date(value: Any, what: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{what} must be a date, got {type(value).__name__}")
    return value


#: Public name for the plain-date check, for callers that validate a session date before handing
#: it here (the backtest option readers' ``option_read_common.require_data_session``): one rule
#: for "a session is a plain date", not two.
require_plain_date = _require_plain_date


def _normalise_volume(volume: Any, data_session: date) -> int:
    """A present bar's volume as a plain ``int``, refusing anything that is not a real count.

    ``None``, NaN and ``pandas.NA`` are MISSING, not zero: the backtest's parquet provider turns
    its documented "present bar, NaN volume" known-zero into 0 before calling, so a missing value
    reaching here is a parse bug. ``pandas.NA`` is detected without importing pandas: its
    comparisons return NA, whose truth value raises TypeError.
    """
    if volume is None or isinstance(volume, bool):
        raise ValueError(f"option bar for {data_session} carries no volume ({volume!r})")
    try:
        is_nan = bool(volume != volume)
    except TypeError:  # pandas.NA: ``NA != NA`` is NA, and bool(NA) raises
        raise ValueError(f"option bar for {data_session} carries no volume ({volume!r})") from None
    if is_nan:
        raise ValueError(f"option bar for {data_session} carries no volume ({volume!r})")
    try:
        as_int = int(volume)
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"option bar for {data_session} has a non-numeric volume {volume!r}") from e
    if as_int != volume:
        raise ValueError(f"option bar for {data_session} has a non-integral volume {volume!r}")
    if as_int < 0:
        raise ValueError(f"option bar for {data_session} has a negative volume {volume!r}")
    return as_int


def session_volume(bars: Iterable[Tuple[date, Optional[int]]], data_session: date) -> int:
    """Volume the contract traded IN ``data_session``: the bar dated exactly that session, else 0.

    A bar that exists but carries no volume is refused (ValueError): every source we read
    (ThetaData EOD, Alpaca option bars) publishes volume on every bar, so a missing one is a
    parse bug, not a zero. A bar dated after data_session is ignored (never lookahead).

    No bar for the session means the contract did not trade in it (both sources omit no-trade
    days), so 0 is the answer, not a fallback. An OLDER bar is not carried forward: yesterday's
    volume is not today's liquidity.

    Returns a plain Python ``int`` (a numpy integer is converted).

    Raises:
        TypeError: ``data_session`` or a bar date is a datetime rather than a plain date.
        ValueError: the matching bar's volume is missing (None/NaN/NA), negative or non-integral,
            or two bars for ``data_session`` disagree after normalisation.
    """
    _require_plain_date(data_session, "data_session")
    matched: Optional[int] = None
    for bar_date, volume in bars:
        _require_plain_date(bar_date, "bar date")
        if bar_date != data_session:
            continue
        volume = _normalise_volume(volume, data_session)
        if matched is not None and matched != volume:
            raise ValueError(
                f"conflicting option bars for {data_session}: volume {matched} vs {volume}")
        matched = volume
    if matched is None:
        return 0
    return matched


def live_decision_time() -> datetime:
    """The instant a LIVE option decision is made at: the one clock every live option read
    (chain volume session, action DTE label) derives its session from.

    Inside an enter-market decision pass it is the pass's frozen ``decision_time`` -- the same
    instant the market-condition gates read, so no second clock read is taken (or, under
    replay capture, recorded). Otherwise the replay-aware evaluation clock (``replay_now``):
    the wall clock live, the recorded read in replay. Imports are lazy: this module is on the
    backtest hot path and must not pull the live market-condition stack in at import.
    """
    from ba2_common.core.market_condition_live import current_decision

    decision = current_decision()
    if decision is not None:
        return decision.decision_time
    from ba2_common.core.replay.clock import replay_now
    return replay_now()


def live_decision_label_now() -> date:
    """``live_decision_label(live_decision_time())``: the New York calendar date of the live
    decision instant -- the session label its orders execute in (a weekend instant labels
    that weekend day; see ``OptionsAccountInterface.decision_label``)."""
    from ba2_common.core.market_calendar import live_decision_label
    return live_decision_label(live_decision_time())
