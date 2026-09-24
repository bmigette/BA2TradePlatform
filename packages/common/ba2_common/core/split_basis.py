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

MIXED-BASIS SCAN (plan Part G1a). The ratio rule looks at the ex-date bar only. A file can be
adjusted AROUND a split and still be wrong: CRWD's cache (4-for-1, ex-date 2026-07-02) is on
the as-traded basis through 2026-06-15 and divided by 4 from 2026-06-16 -- the vendor applied
the adjustment 12 sessions early to the bars appended after that day, so the 07-02 bar looks
"consistent" while every bar before 06-16 is 4x too high. So, for EVERY calendar split (any
factor, marker or not), a one-day close move beyond ``MIXED_BASIS_JUMP`` (either direction)
within ``MIXED_BASIS_WINDOW_DAYS`` calendar days of the ex-date, on a bar that is NOT the
ex-date's own bar, classifies the split ``mixed_basis`` -- a re-fetch verdict, whatever the
ratio rule or the marker said. The marker does not exempt a file from it: a marker proves a
full fetch happened, not that the vendor delivered one basis, and a jump like CRWD's in a
marked file is exactly the defect a marker would otherwise hide for ever. A real move of that
size that close to a split is rare (APP +46%, ARM +48%, NFLX -35% are all far from any
split); if one ever is, the symbol is refused loudly, never silently priced.

``drift``, ``undetectable`` and ``mixed_basis`` all require a FULL re-fetch. For ``drift`` and
``undetectable`` the marker the re-fetch writes turns them into ``refetched``, so that repair
happens once, never on every refresh. ``mixed_basis`` is NOT cleared by a marker: if the vendor
re-delivers the same mixed file, every ``--fetch-missing`` re-fetches it again (and the warmup
then excludes the symbol) until the file is on one basis or the jump is recorded as a real move
(``split_basis_overrides`` kind ``acknowledge_jump``).

Pure apart from the marker read/write helpers.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, List, Optional, Sequence

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
    "VERDICT_MIXED_BASIS",
    "MIXED_BASIS_JUMP",
    "MIXED_BASIS_WINDOW_DAYS",
    "REFETCH_VERDICTS",
    "check_split_basis",
    "needs_full_refetch",
    "full_fetch_marker_path",
    "read_full_fetch_marker",
    "write_full_fetch_marker",
    "SplitBasisRefused",
    "SymbolSplitBasis",
    "as_traded_factor",
    "resolve_symbol_split_basis",
]

VERDICT_CONSISTENT = "consistent"
VERDICT_REFETCHED = "refetched"
VERDICT_DRIFT = "drift"
VERDICT_UNDETECTABLE = "undetectable"
VERDICT_FUTURE = "future"
VERDICT_MIXED_BASIS = "mixed_basis"
REFETCH_VERDICTS = (VERDICT_DRIFT, VERDICT_UNDETECTABLE, VERDICT_MIXED_BASIS)

#: Mixed-basis scan (module docstring): a one-day close ratio beyond this factor, either
#: direction, near a calendar split but not on its ex-date bar. Every real split in the cache
#: is >= 1.5 when unadjusted; the 1.4 floor also catches a 3-for-2 applied on the wrong day.
MIXED_BASIS_JUMP = 1.4
#: How far (calendar days, either side of the ex-date) the scan looks.
MIXED_BASIS_WINDOW_DAYS = 30

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


_FROM_OVERRIDES = object()


def check_split_basis(dates: Any, o: Any, h: Any, l: Any, c: Any, splits: Sequence[CalendarSplit],
                      *, symbol: str = "", marker: Optional[dict] = None,
                      acknowledged_jumps: Any = _FROM_OVERRIDES) -> List[SplitBasisCheck]:
    """Classify every calendar split dated after the series' first bar (see module docstring).

    ``dates`` are session labels (anything ``market_condition_source._to_day64`` accepts).
    ``marker`` is ``read_full_fetch_marker(...)`` for the file the series came from (or None).
    ``acknowledged_jumps`` ``{date: close_ratio}`` are real moves the mixed-basis scan ignores
    when the file still shows them (date AND ratio within ``ANCHOR_TOLERANCE``); by default the
    ``acknowledge_jump`` entries of ``split_basis_overrides`` for ``symbol``.
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
    jump_idx = _close_jumps(cols[3])
    if acknowledged_jumps is _FROM_OVERRIDES:
        from ba2_common.core import split_basis_overrides as _ovr
        acknowledged_jumps = _ovr.acknowledged_jumps(_ovr.overrides_for(symbol)) if symbol else {}
    if acknowledged_jumps and len(jump_idx):
        jump_idx = _drop_acknowledged(d, cols[3], jump_idx, acknowledged_jumps)
    out: List[SplitBasisCheck] = []
    for sp in sorted(splits, key=lambda s: s.date):
        ratio = float(sp.ratio)
        if not (math.isfinite(ratio) and ratio > 0) or ratio == 1.0:
            continue
        sd = np.datetime64(sp.date, "D")
        if sd <= first:
            continue  # the whole file postdates the split: delivered adjusted by construction
        mixed = _mixed_basis_check(d, cols[3], jump_idx, sp, ratio)
        if mixed is not None:
            out.append(mixed)
            continue
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


def _close_jumps(close: np.ndarray) -> np.ndarray:
    """Indices ``i`` (>= 1) whose close / previous close is beyond ``MIXED_BASIS_JUMP``."""
    if len(close) < 2:
        return np.empty(0, dtype=np.int64)
    prev, cur = close[:-1], close[1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        r = cur / prev
    ok = np.isfinite(r) & (prev > 0) & (cur > 0)
    big = ok & ((r > MIXED_BASIS_JUMP) | (r < 1.0 / MIXED_BASIS_JUMP))
    return np.flatnonzero(big) + 1


def _jump_matches(d: np.ndarray, close: np.ndarray, day: date, ratio: float) -> Optional[float]:
    """The file's close / previous close at ``day`` when it matches ``ratio`` within
    ``ANCHOR_TOLERANCE``, else None (no bar, first bar, or a different move)."""
    from ba2_common.core.split_basis_overrides import ANCHOR_TOLERANCE

    hit = np.flatnonzero(d == np.datetime64(day, "D"))
    if not len(hit) or hit[0] == 0:
        return None
    i = int(hit[0])
    prev, cur = float(close[i - 1]), float(close[i])
    if not (prev > 0 and cur > 0):
        return None
    r = cur / prev
    return r if abs(r / float(ratio) - 1.0) <= ANCHOR_TOLERANCE else None


def _drop_acknowledged(d: np.ndarray, close: np.ndarray, jump_idx: np.ndarray,
                       acknowledged: dict) -> np.ndarray:
    keep = []
    for i in jump_idx:
        day = d[int(i)].astype(object)
        if day in acknowledged and _jump_matches(d, close, day, acknowledged[day]) is not None:
            continue
        keep.append(int(i))
    return np.asarray(keep, dtype=np.int64)


def _mixed_basis_check(d: np.ndarray, close: np.ndarray, jump_idx: np.ndarray,
                       sp: CalendarSplit, ratio: float) -> Optional[SplitBasisCheck]:
    """The ``mixed_basis`` verdict for ``sp`` when a split-sized one-day move sits within
    ``MIXED_BASIS_WINDOW_DAYS`` of its ex-date on any bar but the ex-date's own (the first bar
    on or after it), else None. See the module docstring."""
    if not len(jump_idx):
        return None
    sd = np.datetime64(sp.date, "D")
    ex_i = int(np.searchsorted(d, sd, side="left"))
    win = np.timedelta64(MIXED_BASIS_WINDOW_DAYS, "D")
    hits = [int(i) for i in jump_idx
            if i != ex_i and abs(d[i] - sd) <= win]
    if not hits:
        return None
    moves = ", ".join(f"{d[i].astype(object).isoformat()} x{close[i] / close[i - 1]:.4f}"
                      for i in hits)
    return SplitBasisCheck(
        sp.date, ratio, VERDICT_MIXED_BASIS, checked_bar=d[hits[0]].astype(object),
        basis="mixed",
        reason=(f"one-day close move(s) beyond x{MIXED_BASIS_JUMP} within "
                f"{MIXED_BASIS_WINDOW_DAYS} days of the {sp.date.isoformat()} split but not on "
                f"its ex-date bar: {moves}. The file is on a mixed basis (adjusted on the wrong "
                f"day); only a full re-fetch can repair it"))


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


# ---------------------------------------------------------------------------------------------
# AS-TRADED FACTOR (BT/live option parity, plan Part E1)
# ---------------------------------------------------------------------------------------------
# Option strikes and premiums are stored AS TRADED (ThetaData, TastyTrade, Alpaca: an option
# is never re-struck for a later split). The FMP daily cache every backtest prices equities
# from is BACK-ADJUSTED for every split up to the day the file's history was fetched. The
# option path therefore converts the adjusted close into the as-traded basis before comparing
# it with a strike:
#
#     as_traded_price(day) = adjusted_close(day) * as_traded_factor(day)
#     as_traded_factor(day) = PRODUCT of split ratios with  day < ex_date <= basis_date
#
# ``basis_date`` is the last date the cache's adjustment covers: a split AFTER it is not in
# the file (no bar crosses it), so dividing it out would double count. A split ON OR BEFORE
# ``day`` is already in the as-traded price of ``day`` (the ex-date is the first session on the
# new basis). Live needs none of this: Alpaca's spot and strikes are both as traded, so its
# factor is 1 by construction.


class SplitBasisRefused(RuntimeError):
    """The as-traded factor cannot be stated for a symbol, so the option path must not run.

    Never answered with a factor of 1: that is exactly the silent 10x/40x mis-strike this
    exists to prevent (NFLX 2024: FMP close $55.17 against a $553 as-traded chain)."""


def as_traded_factor(splits: Optional[Sequence[CalendarSplit]], day: date, *,
                     basis_date: Optional[date], symbol: str = "") -> float:
    """Adjusted -> as-traded price multiplier for ``day`` (see the section comment above).

    PURE. ``splits`` is the provider's split calendar for the symbol (any order); ``basis_date``
    the last date the adjusted cache's adjustment covers. A 4-for-1 split has ratio 4.0 and a
    1-for-8 reverse split 0.125, so a reverse split makes the factor < 1.

    Raises ``SplitBasisRefused`` for a missing calendar (``None`` -- an EMPTY list is a real
    answer: the symbol never split), a missing basis date, or a split inside the window whose
    ratio is unknown / not a positive finite number.
    """
    tag = f"{symbol}: " if symbol else ""
    if splits is None:
        raise SplitBasisRefused(f"{tag}no split calendar; the as-traded factor for {day} is unknown")
    if basis_date is None:
        raise SplitBasisRefused(f"{tag}no adjustment basis date; the as-traded factor for {day} is unknown")
    factor = 1.0
    for sp in splits:
        if not (day < sp.date <= basis_date):
            continue
        ratio = float(sp.ratio) if sp.ratio is not None else float("nan")
        if not (math.isfinite(ratio) and ratio > 0):
            raise SplitBasisRefused(
                f"{tag}split on {sp.date.isoformat()} has an unusable ratio {sp.ratio!r}; the "
                f"as-traded factor for {day} cannot be stated")
        factor *= ratio
    return factor


@dataclass(frozen=True)
class SymbolSplitBasis:
    """One symbol's verified adjustment basis: the calendar splits the adjusted cache holds,
    and the date through which its adjustment is known to run.

    Built by ``resolve_symbol_split_basis``; ``factor(day)`` is ``as_traded_factor`` over it."""

    symbol: str
    splits: tuple
    basis_date: date
    #: Human-readable provenance of the basis (file range, marker, per-split verdicts).
    evidence: str = ""
    #: Version + digest of the operator-maintained corrections applied on top of the vendor
    #: calendar (``split_basis_overrides``; None = the vendor calendar as-is). Part of
    #: ``identity``, so
    #: a changed override set is a cache miss for everything priced in the as-traded basis.
    overrides_version: Optional[str] = None

    def factor(self, day: date) -> float:
        return as_traded_factor(self.splits, day, basis_date=self.basis_date, symbol=self.symbol)

    def identity(self) -> tuple:
        """What the factor is a pure function of -- for cache keys over as-traded values."""
        return (self.symbol, self.basis_date.isoformat(),
                tuple((s.date.isoformat(), float(s.ratio)) for s in self.splits),
                self.overrides_version)


def resolve_symbol_split_basis(symbol: str, dates: Any, o: Any, h: Any, l: Any, c: Any,
                               splits: Optional[Sequence[CalendarSplit]], *,
                               marker: Optional[dict] = None,
                               overrides: Optional[tuple] = None) -> SymbolSplitBasis:
    """The verified adjustment basis of one cached daily series, or ``SplitBasisRefused``.

    THE BASIS IS PROVEN PER SPLIT, with the same authority the market-condition warmup
    preflight and the provider's drift report use (``check_split_basis``), not from the
    full-fetch marker alone: only a handful of files carry a marker (it is written by a
    repair re-fetch), and a file first fetched after its symbols' last split is on one basis
    with no marker at all. For every calendar split dated inside the file:

      * ``consistent`` / ``refetched`` -> the file is adjusted for it: IN the factor;
      * ``drift`` / ``undetectable``   -> the file's basis is mixed or unprovable: REFUSE
        (the repair is ``tools/warm_market_conditions.py plan --fetch-missing``);

    a split after the last cached bar (``future``) is not in the file and stays OUT, and one
    on/before the first bar can never be inside a (day, basis] window of a priced day.
    So ``basis_date`` is the file's last bar.

    OPERATOR OVERRIDES (plan Part G1b, ``split_basis_overrides``) are applied AFTER the proof:
    every anchor close of the symbol's entries must match the file (else REFUSE -- the entries
    were measured on other bytes); ``exclude_calendar_event`` drops a calendar row (and its
    verdict); ``add_price_adjustment`` adds a factor event WITHOUT a jump proof (a ~5% spin-off
    is not provable from prices, so it must not be classified ``undetectable``). The entries'
    version + digest become ``overrides_version`` (None for a symbol without entries).
    ``overrides`` defaults to ``split_basis_overrides.BASIS_OVERRIDES`` (tests pass their own).
    """
    from ba2_common.core.market_condition_source import _to_day64

    if splits is None:
        raise SplitBasisRefused(f"{symbol}: no split calendar cached; cannot state the as-traded basis")
    bad = [s for s in splits if s.ratio is None or not (math.isfinite(float(s.ratio)) and float(s.ratio) > 0)]
    d = _to_day64(dates)
    if not len(d):
        raise SplitBasisRefused(f"{symbol}: no cached daily bars; cannot state the as-traded basis")
    first = d.min().astype(object)
    last = d.max().astype(object)
    from ba2_common.core import split_basis_overrides as ovr

    entries = ovr.overrides_for(symbol, overrides)
    # Verified against the WHOLE calendar, ratio-less rows included: an operator exclusion of
    # a row FMP gives no ratio for is what lets such a row inside the file stop refusing.
    excluded = _verify_overrides(symbol, d, c, list(splits), entries)
    for s in bad:
        if first < s.date <= last and s.date not in excluded:
            raise SplitBasisRefused(
                f"{symbol}: calendar split on {s.date.isoformat()} has no usable ratio "
                f"({s.ratio!r}) and lies inside the cached history {first}..{last}")
    good = [s for s in splits if s not in bad]
    checks = check_split_basis(d, o, h, l, c, good, symbol=symbol, marker=marker,
                               acknowledged_jumps=ovr.acknowledged_jumps(entries))
    checks = [ch for ch in checks if ch.split_date not in excluded]
    wrong = [ch for ch in checks if ch.verdict in REFETCH_VERDICTS]
    if wrong:
        raise SplitBasisRefused(
            f"{symbol}: the cached daily history is not verifiably on one split basis "
            f"({[ch.to_dict() for ch in wrong]}); the option path cannot convert it to the "
            f"as-traded basis. Repair with tools/warm_market_conditions.py plan --fetch-missing.")
    added = [CalendarSplit(e.event_date, float(e.ratio)) for e in entries
             if e.kind == ovr.KIND_ADD_PRICE_ADJUSTMENT]
    included = tuple(sorted((s for s in list(good) + added
                             if first < s.date <= last and float(s.ratio) != 1.0
                             and s.date not in excluded),
                            key=lambda s: s.date))
    evidence = (f"bars {first}..{last}; marker={_marker_date(marker)}; "
                + ", ".join(f"{ch.split_date}:{ch.ratio:g}:{ch.verdict}" for ch in checks))
    version = ovr.overrides_version_for(entries)
    if entries:
        evidence += "; overrides " + ", ".join(
            f"{e.event_date}:{e.ratio:g}:{e.kind}" for e in entries) + f" ({version})"
    return SymbolSplitBasis(symbol=symbol, splits=included, basis_date=last, evidence=evidence,
                            overrides_version=version)


def _verify_overrides(symbol: str, d: np.ndarray, c: Any, calendar: Sequence[CalendarSplit],
                      entries: tuple) -> set:
    """Check ``symbol``'s override entries against the file and the calendar; return the
    calendar dates the entries exclude. REFUSES when any anchor close is missing or off by more
    than ``ANCHOR_TOLERANCE``, when an ``exclude_calendar_event`` names a row the calendar does
    not hold, or when an ``add_price_adjustment`` lands on a date the calendar already lists
    (the vendor now carries it: counting both would double the factor). Each of those means
    the entry was measured against other data: re-measure it, never guess."""
    from ba2_common.core import split_basis_overrides as ovr

    if not entries:
        return set()
    close = np.asarray(c, dtype=np.float64)
    cal_dates = {s.date for s in calendar}
    excluded: set = set()
    for e in entries:
        for a_day, a_close in e.anchors:
            hit = np.flatnonzero(d == np.datetime64(a_day, "D"))
            actual = float(close[hit[0]]) if len(hit) else None
            if actual is None or not (abs(actual / float(a_close) - 1.0) <= ovr.ANCHOR_TOLERANCE):
                shown = "no bar" if actual is None else f"{actual:g}"
                raise SplitBasisRefused(
                    f"{symbol}: split-basis override {e.kind} {e.event_date.isoformat()} "
                    f"x{e.ratio:g} (overrides {ovr.OVERRIDES_VERSION}) anchor "
                    f"{a_day.isoformat()}: expected FMP close {float(a_close):g}, actual {shown} "
                    f"(tolerance {ovr.ANCHOR_TOLERANCE:.1%}). The cached history is not the one "
                    f"the override was measured on; re-measure it before pricing options on "
                    f"{symbol}.")
        if e.kind == ovr.KIND_ACKNOWLEDGE_JUMP:
            if _jump_matches(d, close, e.event_date, e.ratio) is None:
                hit = np.flatnonzero(d == np.datetime64(e.event_date, "D"))
                i = int(hit[0]) if len(hit) else -1
                seen = (f"x{close[i] / close[i - 1]:.4f}" if i > 0 and close[i - 1] > 0
                        else "no such bar")
                raise SplitBasisRefused(
                    f"{symbol}: split-basis override acknowledge_jump {e.event_date.isoformat()} "
                    f"x{e.ratio:g} (overrides {ovr.OVERRIDES_VERSION}) no longer matches the "
                    f"file: its close move on that day is {seen}. The cached history changed "
                    f"since the jump was acknowledged; re-measure.")
            continue
        if e.kind == ovr.KIND_EXCLUDE_CALENDAR_EVENT:
            if e.event_date not in cal_dates:
                raise SplitBasisRefused(
                    f"{symbol}: split-basis override exclude_calendar_event "
                    f"{e.event_date.isoformat()} names a row the split calendar does not hold "
                    f"(overrides {ovr.OVERRIDES_VERSION}); the calendar changed since it was "
                    f"measured. Re-measure.")
            excluded.add(e.event_date)
        elif e.event_date in cal_dates:
            raise SplitBasisRefused(
                f"{symbol}: split-basis override add_price_adjustment {e.event_date.isoformat()} "
                f"x{e.ratio:g} now also appears in the vendor split calendar (overrides "
                f"{ovr.OVERRIDES_VERSION}); applying both would double count it. Re-measure.")
    return excluded
