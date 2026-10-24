"""Top-up core for the Alpaca history cache (plan 2026-10-24-history-source-task11, Task 3). Pure apart from the
injected ``fetch`` callable and the market calendar.

Owner decisions 2026-10-24 (plan Task 0): live bars append IN PLACE and UNGATED, so the safety lives here:

* FINISHED sessions only (``ohlcv_final_bars.last_final_session_day``: close + settlement).
* CONTINUITY (hard): the append starts at the first regular NYSE session after the file's last bar and covers EVERY
  finished session up to the newest; a session Alpaca did not return (daily) / returned no regular 5-minute bar for
  (5min: the build's own completeness rule, ``days_without_intraday_bars``) -> ``TopUpGap``, ERROR logged, NOTHING
  appended. Rows already present are never re-appended; the result is strictly increasing.
* The split table is applied with the build's ``adjust`` (one function, two callers). An unexplained overnight jump at
  the seam (``split_table.JUMP_HI`` / ``JUMP_LO``) means a corporate action the table does not know yet:
  ``PendingCorporateAction``; a mixed basis is never written.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ba2_common.core import ohlcv_final_bars
from ba2_common.core.market_calendar import regular_session_dates
from ba2_common.logger import logger
from ba2_providers.ohlcv.alpaca_history import build
from ba2_providers.ohlcv.alpaca_history.split_table import JUMP_HI, JUMP_LO

#: A file further behind than this many sessions is refused rather than back-filled (owner: "e.g. 30").
MAX_BACKFILL_SESSIONS = 30

Fetch = Callable[[date, date], pd.DataFrame]          # (first_session, end_exclusive) -> RAW native frame


class TopUpRefused(RuntimeError):
    """Nothing was appended; the file is untouched."""


class TopUpGap(TopUpRefused):
    def __init__(self, symbol: str, interval: str, missing: Sequence[date]):
        self.symbol, self.interval, self.missing = symbol, interval, list(missing)
        super().__init__(f"{symbol} ({interval}): Alpaca returned no bars for session(s) "
                         f"{[d.isoformat() for d in self.missing]}; nothing appended")


class TopUpTooFarBehind(TopUpRefused):
    pass


class PendingCorporateAction(TopUpRefused):
    """The first appended bar jumps beyond the split bounds against the cache: a split the table does not know."""


class SymbolNotInHistory(TopUpRefused):
    """The symbol has no cached history (it is not in the build)."""


def _days(frame: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(frame["Date"]).dt.normalize()


def unexplained_jump(frame: pd.DataFrame, prev_close: Optional[float] = None) -> Optional[str]:
    """A message when some bar of the (adjusted, ascending) ``frame`` opens beyond the split bounds of the previous close
    (``prev_close`` for the first bar; none = the first bar is not checked): a corporate action nobody accepted."""
    closes = frame["Close"].astype(float).reset_index(drop=True)
    prev = pd.concat([pd.Series([prev_close] if prev_close is not None else [float("nan")]), closes.iloc[:-1]], ignore_index=True)
    jumps = frame["Open"].astype(float).reset_index(drop=True) / prev
    bad = jumps[(jumps < JUMP_LO) | (jumps > JUMP_HI)]
    if not len(bad):
        return None
    i = int(bad.index[0])
    return (f"the bar at {frame['Date'].iloc[i]} opens x{float(bad.iloc[0]):.3f} of the previous close with no accepted "
            f"corporate action explaining it")


def topup_frame(cached: pd.DataFrame, fetch: Fetch, splits: Sequence[Tuple[str, float]],
                spinoffs: Sequence[Tuple[str, float]], *, interval: str, now_utc: datetime, symbol: str
                ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """``(cached + the new finished sessions, report)``; ``cached`` itself when there is nothing new (``fetch`` not called)."""
    if interval not in ("1d", "5min"):
        raise ValueError(f"interval {interval!r}: only '1d' and '5min' are topped up")
    if cached is None or cached.empty:
        raise SymbolNotInHistory(f"{symbol} ({interval}): no cached history to extend (not in the build)")
    last_day = _days(cached).max().date()
    newest = ohlcv_final_bars.last_final_session_day(symbol, now_utc)
    if newest is None:
        raise TopUpRefused(f"{symbol} ({interval}): not an NYSE symbol, no finished-session rule to append by")
    if newest <= last_day:
        return cached, {"appended": 0, "sessions": []}
    expected = regular_session_dates(last_day + timedelta(days=1), newest)
    if not expected:
        return cached, {"appended": 0, "sessions": []}
    if len(expected) > MAX_BACKFILL_SESSIONS:
        raise TopUpTooFarBehind(f"{symbol} ({interval}): {len(expected)} sessions behind (max {MAX_BACKFILL_SESSIONS})")
    raw = fetch(expected[0], expected[-1] + timedelta(days=1))
    if raw is None:
        raw = cached.iloc[0:0]
    exp = set(expected)
    new = raw[~pd.to_datetime(raw["Date"]).isin(set(pd.to_datetime(cached["Date"])))] if len(raw) else raw
    have = set(_days(new).dt.date) if len(new) else set()
    stray = sorted(have - exp)
    if stray:
        raise TopUpRefused(f"{symbol} ({interval}): Alpaca returned bars outside the finished sessions {stray}")
    missing = [d for d in expected if d not in have]
    if missing:
        logger.error(f"{symbol} ({interval}): live top-up refused, Alpaca has no bars for session(s) "
                     f"{[d.isoformat() for d in missing]}; the file is untouched, the next refresh retries")
        raise TopUpGap(symbol, interval, missing)
    new = build.adjust(new.sort_values("Date", kind="stable").reset_index(drop=True), splits, spinoffs)
    err = unexplained_jump(new, float(cached.sort_values("Date")["Close"].iloc[-1]))
    if err is not None:
        raise PendingCorporateAction(f"{symbol} ({interval}): {err}; nothing appended")
    new = build.finish(new, interval)
    merged = pd.concat([cached, new[list(cached.columns)]], ignore_index=True)
    if not pd.to_datetime(merged["Date"]).is_monotonic_increasing or merged["Date"].duplicated().any():
        raise TopUpRefused(f"{symbol} ({interval}): the append would not be strictly increasing")
    return merged, {"appended": len(new), "sessions": expected}
