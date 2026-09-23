"""What BOTH backtest option readers share about a decision's data session.

The two readers -- ``options_provider.HistoricalOptionsProvider`` (sqlite) and
``parquet_options_provider.ParquetOptionsProvider`` -- sit behind one seam and must apply the
same rules (BT/live option parity, ``docs/plans/2026-09-22-bt-live-option-parity.md`` B2):

  * ``require_data_session`` -- the caller's data session is a plain date no later than the
    as-of clamp;
  * ``ChainStaleness`` -- the per-run count of chain rows priced from a row older than the data
    session (volume is exact-session; prices are not, by decision, so this is measured).

Neutral module on purpose: neither reader owns these, and the parquet reader must not import
the sqlite one to reach them. Pure (no I/O, no numpy).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict

from ba2_common.core.option_session import require_plain_date


def require_data_session(as_of: date, data_session: Any) -> None:
    """Refuse a ``data_session`` that is not a plain date or lies after the as-of clamp.

    The data session is the ONE session whose bar a decision may read
    (``market_calendar.decision_data_session``); in a backtest it equals the bar date, so a
    later one would read a bar the clock has not reached (lookahead) and is a caller bug. The
    plain-date check is ``option_session``'s own, so the two cannot disagree on what a session
    date is.

    Raises:
        TypeError: ``data_session`` is a datetime or not a date.
        ValueError: ``data_session`` is after ``as_of``.
    """
    require_plain_date(data_session, "data_session")
    if data_session > as_of:
        raise ValueError(f"data_session {data_session} is after the as-of clamp {as_of}: the "
                         f"option reader would read a bar the backtest clock has not reached")


#: Calendar-day age buckets of a STALE chain row (see ``ChainStaleness``). Calendar days, not
#: sessions, so the per-row cost is one subtraction: no calendar lookup on the chain hot path.
_STALE_AGE_BUCKETS = ((3, "1-3"), (7, "4-7"), (30, "8-30"))
_STALE_AGE_OVERFLOW = "31+"


class ChainStaleness:
    """Per-run count of chain rows priced from a row OLDER than the data session.

    THE MEASUREMENT BEHIND A DECISION (2026-09-22, BT/live option parity B2). Volume became
    exact-session, but bid/ask/last and the greeks still come from each contract's latest row on
    or before the clock -- the user chose to keep that and to measure it. A row is STALE when
    the row its price comes from is dated before the data session: the chain shows a price the
    contract had some sessions ago, which live (reading today's snapshot) never shows.

    Held by the provider, which a run builds once (``options_store.build_options_provider``),
    so the counts are the run's. ``results.build_results`` copies ``snapshot()`` into the
    persisted results as ``option_chain_staleness``.
    """
    __slots__ = ("chain_rows", "stale_rows", "stale_age_days")

    def __init__(self) -> None:
        self.chain_rows = 0
        self.stale_rows = 0
        self.stale_age_days: Dict[str, int] = {key: 0 for _cap, key in _STALE_AGE_BUCKETS}
        self.stale_age_days[_STALE_AGE_OVERFLOW] = 0

    def note_stale(self, age_days: int) -> None:
        """One stale row, ``age_days`` calendar days older than the data session (>= 1)."""
        self.stale_rows += 1
        for cap, key in _STALE_AGE_BUCKETS:
            if age_days <= cap:
                self.stale_age_days[key] += 1
                return
        self.stale_age_days[_STALE_AGE_OVERFLOW] += 1

    def snapshot(self) -> Dict[str, Any]:
        """JSON-serialisable copy: ``chain_rows``, ``stale_rows``, ``stale_age_days``."""
        return {"chain_rows": self.chain_rows, "stale_rows": self.stale_rows,
                "stale_age_days": dict(self.stale_age_days)}
