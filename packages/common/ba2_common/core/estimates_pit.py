"""Point-in-time analyst-estimate rules (pure) + the typed refusal for non-point-in-time reads.

WHY. FMP's ``/analyst-estimates`` returns only TODAY's consensus: a backtest decision in 2021 would read
estimates as revised later (lookahead). The provider itself therefore refuses a historical ``as_of`` inside
a hermetic backtest (``EstimatesNotPointInTime``). Alpha Vantage ``EARNINGS_ESTIMATES`` keeps, per fiscal
quarter, a frozen snapshot and its 7/30/60/90-day-ago values, which ``quarterly_consensus`` turns into
point-in-time rows.

MEASURED on the fixtures in ``packages/providers/tests/fixtures/alphavantage`` (MSFT, AAPL, NVDA; 2026-11-07):

* A past quarter's row is FROZEN at its report: ``eps_estimate_average`` ~ the EARNINGS endpoint's
  ``estimatedEPS`` (MSFT: median relative difference 0.4% over 38 quarters).
* Frozen rows are NOT split-adjusted, and are on the basis of the freeze date: NVDA quarter 2024-04-30
  (reported 2024-05-22, 10:1 split 2024-06-10) AV 5.59 vs the split-adjusted ``estimatedEPS`` 0.559 (x10);
  quarter 2021-04-30 AV 3.28 vs 0.0822 (x40 = 4:1 in 2021 times 10:1 in 2024). The EARNINGS endpoint
  itself IS split-adjusted. So a value is on today's basis after dividing by every split whose ex-date is
  after the freeze date.
* The label (``date``) is the fiscal quarter end; the EARNINGS endpoint maps it to the report date
  (``fiscalDateEnding`` -> ``reportedDate``). AAPL shows the label-shift glitch the owner described: the
  row 2026-09-30 (reported 2026-11-02) holds 2.9035 while ``estimatedEPS`` is 1.98 -- it carries the NEXT
  quarter. Such a row is detected by comparing it with ``estimatedEPS`` and DROPPED (never guessed).
* The 7/30/60/90-day-ago values are on the basis of THEIR OWN date, not adjusted: NVDA quarter 2024-07-31
  (reported 2024-08-28, split 2024-06-10) has 90d 5.86 (pre-split), 60d 0.63, 30d 0.63, 0d 0.64 (post-split).
  That also pins the ANCHOR of the "N days ago" values to the report date: 60d post-split and 90d pre-split
  puts the anchor between 2024-08-09 and 2024-09-08, which fits the report date (08-28) and rules out the
  period end (07-31). MEASURED on this one quarter only; every other quarter is consistent with it but not
  discriminating. Anchoring to the report date is also the lookahead-safe reading.
* UNVERIFIED: whether the ago-values of a quarter NOT yet reported (live rows) are as-then raw too. The
  cross-check in ``quarterly_consensus`` omits a snapshot whose split correction disagrees with the
  freeze-day value, so a wrong guess costs a missing snapshot, not a wrong number.
"""
from __future__ import annotations

import math
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

#: Days before the freeze (report) date of the snapshots a row carries; 0 is the frozen value itself.
SNAPSHOT_AGES: Tuple[int, ...] = (0, 7, 30, 60, 90)

#: A frozen row whose average differs from the EARNINGS endpoint's ``estimatedEPS`` (on today's basis) by more
#: than max(REL_TOL * |estimatedEPS|, ABS_TOL) is a label-shifted / corrupt row and is dropped.
CONSISTENCY_REL_TOL = 0.25
CONSISTENCY_ABS_TOL = 0.05


class EstimatesNotPointInTime(RuntimeError):
    """A backtest asked a source for estimates it can only answer as of TODAY (FMP's snapshot): the result
    would read the future. Use a point-in-time source (``alphavantage``) or a method that needs none."""


_snapshot_permission = threading.local()


@contextmanager
def estimate_snapshot_allowed() -> Iterator[None]:
    """HISTORICAL REPLAY ONLY: let a non-point-in-time estimate read through with a historical ``as_of``.

    The replay rebuilds a recorded LIVE analysis to compare it field by field and labels any estimates input
    "revision unknown" itself; it is not a trading decision. A backtest must never enter this. Per-thread."""
    previous = getattr(_snapshot_permission, "allowed", False)
    _snapshot_permission.allowed = True
    try:
        yield
    finally:
        _snapshot_permission.allowed = previous


def snapshot_read_allowed() -> bool:
    return getattr(_snapshot_permission, "allowed", False)


def is_historical_as_of(as_of: Optional[datetime], today: Optional[date] = None) -> bool:
    """True when ``as_of`` is a day before today. ``None`` (live) and today are not historical."""
    if as_of is None:
        return False
    day = as_of.date() if isinstance(as_of, datetime) else as_of
    return day < (today or date.today())


def refuse_if_not_point_in_time(as_of: Optional[datetime], *, hermetic: bool, what: str) -> None:
    """Raise ``EstimatesNotPointInTime`` for a historical ``as_of`` inside a hermetic backtest."""
    if hermetic and is_historical_as_of(as_of) and not snapshot_read_allowed():
        raise EstimatesNotPointInTime(
            f"{what}: the source returns only today's consensus, so an as-of read at {as_of:%Y-%m-%d} inside a "
            f"backtest would see later revisions. Run the backtest with --estimates-source alphavantage "
            f"(point-in-time) or use a method that needs no estimates.")


# --------------------------------------------------------------------------------------- split basis
def split_factor_after(splits: Sequence[Tuple[date, float]], freeze: date) -> float:
    """Product of the ratios of splits with ex-date AFTER ``freeze`` (4-for-1 -> 4.0): the divisor that takes
    a value on the basis of ``freeze`` to today's basis. A ratio that is not a positive finite number
    refuses (an unknown ratio must never read as 1)."""
    factor = 1.0
    for ex, ratio in splits:
        if ex <= freeze:
            continue
        if ratio is None or not (math.isfinite(float(ratio)) and float(ratio) > 0):
            raise ValueError(f"split on {ex} has an unusable ratio {ratio!r}; the estimate basis is unknown")
        factor *= float(ratio)
    return factor


def _num(value: Any) -> Optional[float]:
    if value is None or value in ("None", ""):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _day(value: Any) -> Optional[date]:
    if not value or value == "None":
        return None
    return date.fromisoformat(str(value)[:10])


def quarterly_consensus(estimates_payload: Dict[str, Any], earnings_payload: Dict[str, Any],
                        splits: Sequence[Tuple[date, float]], as_of: Optional[datetime],
                        fetched_on: date) -> List[Dict[str, Any]]:
    """Unreported fiscal quarters as of ``as_of``, each on today's split basis. PURE.

    ``estimates_payload`` / ``earnings_payload`` are the raw Alpha Vantage ``EARNINGS_ESTIMATES`` / ``EARNINGS``
    responses; ``splits`` the ``(ex_date, ratio)`` calendar; ``fetched_on`` the day the payloads were fetched
    (the freeze date of a quarter with no report date yet). ``as_of`` None = now = ``fetched_on`` (live).

    For each ``fiscal quarter`` row: its report date comes from EARNINGS (``fiscalDateEnding`` match); the row
    is unreported at ``as_of`` when that date is after ``as_of``. A quarter EARNINGS does not list has no
    report date: for a live read it is unreported (frozen "now" = ``fetched_on``); for a historical ``as_of``
    it cannot be placed in time and is omitted. The value returned is the latest of the 0/7/30/60/90-day
    snapshots dated (freeze date - age) <= ``as_of``; none -> the quarter is omitted. Every snapshot is on the
    split basis of its own date and is divided by the splits AFTER that date; when a split lies between a
    snapshot and the freeze date, the result must agree (0.5x-2x) with the freeze-day value on today's basis,
    else AV adjusted it after all and the snapshot is omitted. High/low/analyst count exist only for the
    0-day snapshot (AV keeps no history of them) and are None for the others.
    """
    rows = estimates_payload.get("estimates")
    if not isinstance(rows, list):
        raise ValueError("Alpha Vantage EARNINGS_ESTIMATES payload has no 'estimates' list")
    quarters = earnings_payload.get("quarterlyEarnings")
    if not isinstance(quarters, list):
        raise ValueError("Alpha Vantage EARNINGS payload has no 'quarterlyEarnings' list")
    reported = {str(q.get("fiscalDateEnding")): q for q in quarters}
    as_of_day = fetched_on if as_of is None else (as_of.date() if isinstance(as_of, datetime) else as_of)
    # A quarter with no report date can only be placed in time from the fetch day on (live, or an as_of at/after it).
    live = as_of is None or as_of_day >= fetched_on

    out: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("horizon") != "fiscal quarter":
            continue
        period_end = _day(row.get("date"))
        if period_end is None:
            continue
        match = reported.get(period_end.isoformat())
        report_date = _day(match.get("reportedDate")) if match else None
        if report_date is None:
            if not live:
                continue                    # cannot place the snapshots in time
            freeze, verified = fetched_on, False
        else:
            if report_date <= as_of_day:
                continue                    # reported by as_of: not an open consensus any more
            freeze, verified = report_date, True
        factor0 = split_factor_after(splits, freeze)

        if match is not None:
            est = _num(match.get("estimatedEPS"))
            avg0 = _num(row.get("eps_estimate_average"))
            if est is not None and avg0 is not None:
                if abs(avg0 / factor0 - est) > max(CONSISTENCY_REL_TOL * abs(est), CONSISTENCY_ABS_TOL):
                    continue                # label-shifted / corrupt row: drop, never guess
        ref = _num(row.get("eps_estimate_average"))
        ref = None if ref is None else ref / factor0       # the freeze-day value on today's basis
        pick = None
        for age in SNAPSHOT_AGES:           # ascending age: the first one dated <= as_of is the latest
            snap_day = freeze - timedelta(days=age)
            if snap_day > as_of_day:
                continue
            key = "eps_estimate_average" if age == 0 else f"eps_estimate_average_{age}_days_ago"
            value = _num(row.get(key))
            if value is None:
                continue
            # Each snapshot is on the basis of ITS OWN date (measured: NVDA 2024-07-31, 90d value 5.86 is
            # pre-split, 60d value 0.63 post-split): divide by the splits after that date.
            factor = split_factor_after(splits, snap_day)
            if factor != factor0:
                # A split lies between this snapshot and the freeze date. Verify the as-then reading against the
                # freeze-day value; if AV had adjusted it instead, the division would be wrong -> omit it.
                if not ref or not (0.5 <= (value / factor) / ref <= 2.0):
                    continue
            pick = (age, snap_day, value, factor)
            break
        if pick is None:
            continue
        age, snap_day, value, factor = pick
        cur = age == 0
        high = _num(row.get("eps_estimate_high")) if cur else None
        low = _num(row.get("eps_estimate_low")) if cur else None
        count = _num(row.get("eps_estimate_analyst_count")) if cur else None
        out.append({
            "fiscal_period_end": period_end.isoformat(),
            "report_date": report_date.isoformat() if report_date else None,
            "eps_avg": value / factor,
            "eps_high": None if high is None else high / factor,
            "eps_low": None if low is None else low / factor,
            "analyst_count": None if count is None else int(count),
            "snapshot_date": snap_day.isoformat(),
            "snapshot_age_days": age,
            "label_verified": verified,
            "split_factor": factor,
        })
    out.sort(key=lambda r: (r["report_date"] or "9999", r["fiscal_period_end"]))
    return out
