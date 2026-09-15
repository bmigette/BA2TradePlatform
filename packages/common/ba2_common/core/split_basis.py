"""Split-basis drift detection for an appended daily OHLCV cache.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` section 4 (source
contract) and the Task 6 "split-basis drift" amendment of the implementation plan.

THE EXPOSURE. ``certify_source_columns`` proves that a daily file FETCHED AFTER a split is on one
split-adjusted basis. The provider cache is then topped up by APPENDING bars after the last cached
one (``MarketDataProviderInterface._refresh_parquet_if_stale``). A symbol that splits after its
file was first fetched therefore ends up on a MIXED basis: pre-split bars as originally fetched
(unadjusted), post-split bars adjusted. The calculators would read a fake 2x/4x/10x move with
status ``valid``.

THE AUTHORITY IS THE SPLIT CALENDAR, not a price-only detector (a reviewer scan found 59 lasting
1/k steps since 2020 across the cache, many of them real crashes). For every calendar split dated
after the file's first bar:

* a full-history fetch recorded AFTER the split date (the "full fetch marker" written next to the
  parquet) proves the pre-split bars were delivered adjusted -> ``refetched``;
* otherwise the ratio rule of ``certify_split`` is applied AT THAT SPLIT DATE (the first cached
  bar on or after it): split-adjusted -> ``consistent``; unadjusted / mixed -> ``drift``;
* a split whose factor is too small for the ratio rule to tell from an ordinary day
  (``MIN_DETECTABLE_FACTOR``) cannot be verified from prices -> ``undetectable``;
* a split dated after the file's last bar is not in the file yet -> ``future`` (the top-up that
  crosses it is checked then).

``drift`` and ``undetectable`` both require a FULL re-fetch; the marker the re-fetch writes turns
them into ``refetched``, so the repair happens once, never on every refresh.

Pure apart from the marker read/write helpers.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import numpy as np

__all__ = [
    "CalendarSplit",
    "SplitBasisCheck",
    "MIN_DETECTABLE_FACTOR",
    "MARKER_DIRNAME",
    "VERDICT_CONSISTENT",
    "VERDICT_REFETCHED",
    "VERDICT_DRIFT",
    "VERDICT_UNDETECTABLE",
    "VERDICT_FUTURE",
    "REFETCH_VERDICTS",
    "check_split_basis",
    "needs_full_refetch",
    "full_fetch_marker_path",
    "read_full_fetch_marker",
    "write_full_fetch_marker",
    "splits_from_pairs",
]

VERDICT_CONSISTENT = "consistent"
VERDICT_REFETCHED = "refetched"
VERDICT_DRIFT = "drift"
VERDICT_UNDETECTABLE = "undetectable"
VERDICT_FUTURE = "future"
REFETCH_VERDICTS = (VERDICT_DRIFT, VERDICT_UNDETECTABLE)

#: Below this factor the ratio rule's "ordinary day" and "unadjusted" bands (ln 1.5 wide each,
#: see ``market_condition_source._MATCH_TOLERANCE``) overlap, so prices cannot decide.
MIN_DETECTABLE_FACTOR = 1.5

#: Sub-directory of the provider cache folder holding full-fetch markers. A directory, so a
#: ``*_1d.parquet`` glob over the provider folder never sees a marker.
MARKER_DIRNAME = "_split_basis"


@dataclass(frozen=True)
class CalendarSplit:
    """One split from the provider's split calendar. ``ratio`` = numerator / denominator
    (4-for-1 -> 4.0, 1-for-10 reverse -> 0.1)."""

    date: date
    ratio: float


@dataclass(frozen=True)
class SplitBasisCheck:
    split_date: date
    ratio: float
    verdict: str
    #: The first cached bar on or after the split date the ratio rule was applied at.
    checked_bar: Optional[date] = None
    basis: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["split_date"] = self.split_date.isoformat()
        d["checked_bar"] = None if self.checked_bar is None else self.checked_bar.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SplitBasisCheck":
        return cls(split_date=date.fromisoformat(d["split_date"]), ratio=float(d["ratio"]),
                   verdict=d["verdict"],
                   checked_bar=None if d["checked_bar"] is None else date.fromisoformat(d["checked_bar"]),
                   basis=d["basis"], reason=d["reason"])


def needs_full_refetch(checks: Iterable[SplitBasisCheck]) -> bool:
    return any(c.verdict in REFETCH_VERDICTS for c in checks)


def check_split_basis(dates: Any, o: Any, h: Any, l: Any, c: Any, splits: Sequence[CalendarSplit],
                      *, symbol: str = "", marker: Optional[dict] = None) -> List[SplitBasisCheck]:
    """Classify every calendar split dated after the series' first bar (see module docstring).

    ``dates`` are session labels (anything ``market_condition_source._to_day64`` accepts).
    ``marker`` is ``read_full_fetch_marker(...)`` for the file the series came from (or None).
    """
    from ba2_common.core.market_condition_source import SplitFixture, _to_day64, certify_split

    d = _to_day64(dates)
    if not len(d):
        return []
    order = np.argsort(d, kind="stable")
    d = d[order]
    cols = [np.asarray(x, dtype=np.float64)[order] for x in (o, h, l, c)]
    first, last = d[0], d[-1]
    fetched_on = _marker_date(marker)
    out: List[SplitBasisCheck] = []
    for sp in sorted(splits, key=lambda s: s.date):
        ratio = float(sp.ratio)
        if not (math.isfinite(ratio) and ratio > 0) or ratio == 1.0:
            continue
        sd = np.datetime64(sp.date, "D")
        if sd <= first:
            continue  # the whole file postdates the split: delivered adjusted by construction
        if sd > last:
            out.append(SplitBasisCheck(sp.date, ratio, VERDICT_FUTURE,
                                       reason="split is after the last cached bar"))
            continue
        if fetched_on is not None and fetched_on > sp.date:
            out.append(SplitBasisCheck(sp.date, ratio, VERDICT_REFETCHED,
                                       reason=f"full history fetched on {fetched_on.isoformat()} (UTC)"))
            continue
        factor = max(ratio, 1.0 / ratio)
        if factor < MIN_DETECTABLE_FACTOR:
            out.append(SplitBasisCheck(sp.date, ratio, VERDICT_UNDETECTABLE,
                                       reason=f"factor {factor:.3f} < {MIN_DETECTABLE_FACTOR}: prices "
                                              "cannot tell the basis; only a full re-fetch can"))
            continue
        i = int(np.searchsorted(d, sd, side="left"))
        bar = d[i].astype(object)
        cert = certify_split(d, *cols, SplitFixture(symbol, bar, factor))
        verdict = VERDICT_CONSISTENT if cert.consistent else VERDICT_DRIFT
        out.append(SplitBasisCheck(sp.date, ratio, verdict, checked_bar=bar, basis=cert.basis,
                                   reason=cert.reason))
    return out


def _marker_date(marker: Optional[dict]) -> Optional[date]:
    if not marker:
        return None
    try:
        return date.fromisoformat(marker["fetched_on_utc"])
    except (KeyError, TypeError, ValueError):
        return None


def full_fetch_marker_path(parquet_path: str) -> str:
    folder, name = os.path.split(parquet_path)
    stem = name[:-len(".parquet")] if name.endswith(".parquet") else name
    return os.path.join(folder, MARKER_DIRNAME, stem + ".json")


def read_full_fetch_marker(parquet_path: Optional[str]) -> Optional[dict]:
    """The marker for ``parquet_path`` or None (an absent/unreadable marker proves nothing)."""
    if not parquet_path:
        return None
    try:
        with open(full_fetch_marker_path(parquet_path), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_full_fetch_marker(parquet_path: str, *, first_bar: Optional[date], last_bar: Optional[date],
                            rows: int, now: Optional[datetime] = None) -> str:
    """Record that ``parquet_path`` was REPLACED by a full-history fetch now. Atomic."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    path = full_fetch_marker_path(parquet_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "fetched_on_utc": now.date().isoformat(),
        "fetched_at_utc": now.isoformat(),
        "first_bar": None if first_bar is None else first_bar.isoformat(),
        "last_bar": None if last_bar is None else last_bar.isoformat(),
        "rows": int(rows),
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True)
    os.replace(tmp, path)
    return path


def splits_from_pairs(pairs: Iterable[Tuple[Any, Any]]) -> List[CalendarSplit]:
    """``[(date | iso string, ratio), ...]`` -> ``CalendarSplit`` list; a None ratio is dropped."""
    out = []
    for d, r in pairs:
        if r is None:
            continue
        out.append(CalendarSplit(d if isinstance(d, date) else date.fromisoformat(str(d)[:10]), float(r)))
    return out
