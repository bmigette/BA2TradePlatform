"""Is it safe to APPEND freshly fetched daily bars to a cached history? (live refresh + fetch-cache)

THE BUG (dev app, 2026-09-28 15:34). The APH daily cache was 1,391 hours old. The live refresh
(``MarketDataProviderInterface._refresh_parquet_if_stale``) fetched 2026-07-01..2026-09-29 -- 61
bars already adjusted for APH's 2:1 split of 2026-09-03 -- and APPENDED them to the cached bars up
to 2026-06-30, which were not. The file became mixed-basis: a fake x0.488 one-day move on
2026-07-01 that every reader (live analysis, backtests, the market-condition gates) takes as real.
The drift report that ran afterwards looked only 30 days around the ex-date and said nothing.

THE RULE. A top-up may be appended only when the vendor still agrees with the cache where they
meet. The top-up therefore asks the vendor for the last ``TOPUP_OVERLAP_BARS`` CACHED bars too and
compares them (:func:`verify_topup`):

* every overlap bar equal (or a provisional snapshot of the vendor's bar, below) and no calendar
  split crossed by the new bars -> ``agree``: append, byte-identical to the old append;
* the overlap rescaled by one split-like factor (the vendor re-based its history: a split, or a
  split applied early like CRWD's) -> ``basis_change``: REPLACE the history with a full re-fetch;
* a calendar split crossed by the new bars while the overlap still agrees:
    - the vendor's new bars show no as-traded step at the ex-date -> ``calendar_split``: the vendor
      is on one basis across the split, so REPLACE with a full re-fetch too (the old bars may still
      be on a basis older than the vendor's);
    - they do show the step (or the factor is too small to tell) -> ``vendor_unadjusted``: the
      vendor has not adjusted its pre-split history yet. Appending would write a mixed file and so
      would a full re-fetch (it would deliver the same unadjusted history). REFUSED, nothing
      written; the next refresh retries;
* anything else -> ``unexplained`` / ``no_overlap``: REFUSED, nothing written.

PROVISIONAL BARS. A daily refresh during the session caches today's bar as it stood then (the dev
app refreshes near 09:30 New York). That bar is never revisited by an append-only top-up, so the
cache holds such snapshots, e.g. AAOI 2026-09-21 O=H=L=C=108.01 on 360,654 shares. A cached bar
whose open equals the vendor's and whose high/low/close/volume lie INSIDE the vendor's final bar is
such a snapshot: it is not a disagreement, and the ``agree`` top-up replaces it with the vendor's
bar (``TopUpVerdict.provisional_days``).

Pure (pandas/numpy only). The provider side lives in ``MarketDataProviderInterface``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

__all__ = [
    "OHLCVTopUpRefused",
    "TopUpVerdict",
    "TOPUP_OVERLAP_BARS",
    "PRICE_REL_TOL",
    "PRICE_ABS_TOL",
    "CALENDAR_MATCH_TOL",
    "SPLIT_RATIO_TOL",
    "VERDICT_AGREE",
    "VERDICT_BASIS_CHANGE",
    "VERDICT_CALENDAR_SPLIT",
    "VERDICT_VENDOR_UNADJUSTED",
    "VERDICT_UNVERIFIABLE_STEP",
    "VERDICT_UNEXPLAINED",
    "VERDICT_NO_OVERLAP",
    "FULL_REFETCH_VERDICTS",
    "verify_topup",
    "verify_replacement",
    "shared_mismatches",
    "day_index",
]

#: How many of the newest cached bars the top-up re-asks the vendor for and compares.
TOPUP_OVERLAP_BARS = 5
#: Two prices are "the same" within this fraction ...
PRICE_REL_TOL = 0.005
#: ... or within this absolute amount (cent rounding of a low-priced stock).
PRICE_ABS_TOL = 0.0101
#: A rescale factor matches a (cumulative) calendar split within this, in log.
CALENDAR_MATCH_TOL = 0.02
#: With no calendar event to match, a rescale factor is split-like when it lies within this (log)
#: of p/q with 1 <= q <= 10 (2:1, 3:2, 5:4, 1:10, 1:20, ...).
SPLIT_RATIO_TOL = 0.01
#: With the calendar unavailable, a one-day move among the NEW bars beyond this, whose size is
#: within ``_STEP_RATIO_TOL`` (log) of such a p/q, may be a split the vendor has not adjusted yet.
_STEP_JUMP = 1.4
_STEP_RATIO_TOL = 0.04
#: Below this factor a one-day step cannot be told from an ordinary day (``split_basis``).
_MIN_DETECTABLE_FACTOR = 1.5

VERDICT_AGREE = "agree"
VERDICT_BASIS_CHANGE = "basis_change"
VERDICT_CALENDAR_SPLIT = "calendar_split"
VERDICT_VENDOR_UNADJUSTED = "vendor_unadjusted"
VERDICT_UNVERIFIABLE_STEP = "unverifiable_step"
VERDICT_UNEXPLAINED = "unexplained"
VERDICT_NO_OVERLAP = "no_overlap"
#: Verdicts that REPLACE the cached history with a full re-fetch instead of appending.
FULL_REFETCH_VERDICTS = (VERDICT_BASIS_CHANGE, VERDICT_CALENDAR_SPLIT)

_EQUAL, _PROVISIONAL, _MISMATCH = "equal", "provisional", "mismatch"


class OHLCVTopUpRefused(RuntimeError):
    """A cached OHLCV history could not be topped up without writing a mixed-basis file, so
    NOTHING was written. Never absorbed by ``failure_modes`` (the numbers would be wrong)."""


@dataclass(frozen=True)
class TopUpVerdict:
    verdict: str
    #: vendor / cached price factor of the rescaled overlap (1.0 when it agrees).
    factor: float = 1.0
    #: Overlap days whose cached bar is an intraday snapshot of the vendor's bar.
    provisional_days: Tuple[date, ...] = ()
    #: Calendar splits dated after the last cached bar, up to the vendor's last bar.
    spanned_splits: Tuple[Any, ...] = ()
    #: Number of overlap days compared.
    shared: int = 0
    reason: str = ""

    @property
    def appendable(self) -> bool:
        return self.verdict == VERDICT_AGREE

    @property
    def needs_full_refetch(self) -> bool:
        return self.verdict in FULL_REFETCH_VERDICTS


def day_index(dates: Any) -> pd.DatetimeIndex:
    """Session labels as tz-naive midnights (an aware stamp is read in UTC first: FMP daily bars
    arrive as UTC midnights and the cache holds naive midnights)."""
    d = pd.to_datetime(pd.Series(dates).reset_index(drop=True))
    if getattr(d.dt, "tz", None) is not None:
        d = d.dt.tz_convert("UTC").dt.tz_localize(None)
    return pd.DatetimeIndex(d.dt.normalize())


def _by_day(df: pd.DataFrame) -> pd.DataFrame:
    out = df[["Open", "High", "Low", "Close"] + (["Volume"] if "Volume" in df.columns else [])].copy()
    out = out.apply(pd.to_numeric, errors="coerce").astype(float)
    out.index = day_index(df["Date"])
    return out[~out.index.duplicated(keep="last")].sort_index()


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= max(PRICE_REL_TOL * abs(b), PRICE_ABS_TOL)


def _usable(row) -> bool:
    return all(math.isfinite(x) and x > 0 for x in (row.Open, row.High, row.Low, row.Close))


def _classify(c, v, k: float) -> str:
    """The cached bar ``c`` (prices x ``k``) against the vendor bar ``v``."""
    co, ch, cl, cc = c.Open * k, c.High * k, c.Low * k, c.Close * k
    if _same(co, v.Open) and _same(ch, v.High) and _same(cl, v.Low) and _same(cc, v.Close):
        return _EQUAL
    tol_hi = lambda x: x + max(PRICE_REL_TOL * abs(x), PRICE_ABS_TOL)   # noqa: E731
    tol_lo = lambda x: x - max(PRICE_REL_TOL * abs(x), PRICE_ABS_TOL)   # noqa: E731
    inside = (_same(co, v.Open) and ch <= tol_hi(v.High) and cl >= tol_lo(v.Low)
              and tol_lo(v.Low) <= cc <= tol_hi(v.High))
    if inside:
        cv, vv = getattr(c, "Volume", float("nan")), getattr(v, "Volume", float("nan"))
        if not (math.isfinite(cv) and math.isfinite(vv) and vv > 0) or cv / k <= vv * 1.02:
            return _PROVISIONAL
    return _MISMATCH


def _row_text(day, c, v) -> str:
    return (f"{day.date()} cached O/H/L/C {c.Open:g}/{c.High:g}/{c.Low:g}/{c.Close:g} vs vendor "
            f"{v.Open:g}/{v.High:g}/{v.Low:g}/{v.Close:g}")


def _split_like_ratio(f: float, tol: float) -> Optional[str]:
    """The split ``"p:q"`` (1 <= q <= 10 on either side) that rescales older prices by ``f``
    (a 2:1 split halves them: ``f`` 0.5 -> ``"2:1"``; a 1:10 reverse split: 10.0 -> ``"1:10"``),
    within ``tol`` in log; None when no such split is that close or ``f`` is ~1."""
    if not (math.isfinite(f) and f > 0):
        return None
    g = 1.0 / f                                   # the split's own ratio p/q
    for q in range(1, 11):
        for x, flip in ((g, False), (1.0 / g, True)):
            p = round(x * q)
            if p >= 1 and p != q and abs(math.log(x) - math.log(p / q)) <= tol:
                return f"{q}:{p}" if flip else f"{p}:{q}"
    return None


def _calendar_match(price_ratio: float, splits: Optional[Sequence], after: date) -> Optional[list]:
    """The calendar splits (dated after ``after``) whose cumulative ratio explains a vendor/cached
    price factor of ``price_ratio`` (a 2:1 split halves the older prices: 0.5), else None."""
    if not splits:
        return None
    later = sorted((s for s in splits if s.date > after), key=lambda s: s.date)
    cum = 1.0
    for j, s in enumerate(later):
        cum *= float(s.ratio)
        if abs(math.log(price_ratio) + math.log(cum)) <= CALENDAR_MATCH_TOL:
            return later[:j + 1]
    return None


def _describe_splits(splits) -> str:
    return ", ".join(f"{s.date.isoformat()} x{float(s.ratio):g}" for s in splits)


def verify_topup(cached: pd.DataFrame, vendor: pd.DataFrame, splits: Optional[Sequence], *,
                 calendar_failed: bool = False, symbol: str = "") -> TopUpVerdict:
    """Decide whether ``vendor``'s bars after ``cached``'s last bar may be appended to it.

    ``cached`` is the cached history (only its last ``TOPUP_OVERLAP_BARS`` rows are read);
    ``vendor`` the vendor's answer from the first of those days onwards. ``splits`` is the split
    calendar (``[CalendarSplit]``), ``None`` when the provider has none; ``calendar_failed`` says the
    provider HAS one but it could not be read on this refresh. See the module docstring.
    """
    tag = f"{symbol}: " if symbol else ""
    c = _by_day(cached).tail(TOPUP_OVERLAP_BARS)
    v = _by_day(vendor)
    last_cached = c.index.max().date()
    shared_days = [d for d in c.index if d in v.index]
    usable = [d for d in shared_days if _usable(c.loc[d]) and _usable(v.loc[d])]
    new = v[v.index > pd.Timestamp(last_cached)]
    last_vendor = v.index.max().date() if len(v) else last_cached
    spanned = tuple(sorted((s for s in (splits or ())
                            if last_cached < s.date <= last_vendor
                            and abs(math.log(float(s.ratio))) > PRICE_REL_TOL),
                           key=lambda s: s.date))
    if not usable:
        return TopUpVerdict(VERDICT_NO_OVERLAP, spanned_splits=spanned, reason=(
            f"{tag}the vendor returned none of the last {len(c)} cached sessions "
            f"({c.index.min().date()}..{last_cached}) with usable prices, so nothing proves its new "
            f"bars are on the cache's basis"))

    cls1 = {d: _classify(c.loc[d], v.loc[d], 1.0) for d in usable}
    if all(x != _MISMATCH for x in cls1.values()):
        prov = tuple(d.date() for d in usable if cls1[d] == _PROVISIONAL)
        base = dict(provisional_days=prov, spanned_splits=spanned, shared=len(usable))
        if spanned:
            return _spanned_verdict(c, new, spanned, base, tag, last_cached)
        if calendar_failed:
            step = _unverified_step(c, new)
            if step is not None:
                return TopUpVerdict(VERDICT_UNVERIFIABLE_STEP, **base, reason=(
                    f"{tag}the split calendar could not be read, and the new bars move "
                    f"x{step[1]:.4f} on {step[0]} -- the size of a split ({step[2]}). Without the "
                    f"calendar nothing tells a split the vendor has not adjusted yet from a real "
                    f"move"))
        return TopUpVerdict(VERDICT_AGREE, **base, reason=(
            f"{tag}{len(usable)} overlap session(s) agree"
            + (f"; {len(prov)} cached provisional bar(s) {[d.isoformat() for d in prov]}"
               if prov else "")))

    # --- the overlap disagrees: is it ONE split-like rescale (or an older stale run + agreement)?
    k = {}
    for d in usable:
        co, vo = c.loc[d].Open, v.loc[d].Open
        k[d] = vo / co
    runs: List[List[pd.Timestamp]] = []
    for d in usable:
        tol = PRICE_REL_TOL + PRICE_ABS_TOL / min(c.loc[d].Open, v.loc[d].Open)
        if runs and abs(math.log(k[d]) - math.log(k[runs[-1][0]])) <= 2 * tol:
            runs[-1].append(d)
        else:
            runs.append([d])
    bad = [d for d in usable if cls1[d] == _MISMATCH]
    listing = "; ".join(_row_text(d, c.loc[d], v.loc[d]) for d in bad)
    unexplained = TopUpVerdict(VERDICT_UNEXPLAINED, shared=len(usable), spanned_splits=spanned,
                               reason=(f"{tag}the vendor disagrees with the cache where they meet "
                                       f"and no single split-like rescale explains it: {listing}"))
    if len(runs) > 2:
        return unexplained
    scaled = runs[0]
    if len(runs) == 2:
        # Only "older bars rescaled, newer bars agree" is a basis change (a cache that already
        # straddles a split it was appended across). The reverse is not.
        if any(cls1[d] == _MISMATCH for d in runs[1]):
            return unexplained
    factor = float(np.median([k[d] for d in scaled]))
    if any(_classify(c.loc[d], v.loc[d], factor) == _MISMATCH for d in scaled):
        return unexplained
    matched = _calendar_match(factor, splits, scaled[-1].date())
    ratio_text = _split_like_ratio(factor, SPLIT_RATIO_TOL)
    if matched is None and ratio_text is None:
        return TopUpVerdict(VERDICT_UNEXPLAINED, factor=factor, shared=len(usable),
                            spanned_splits=spanned, reason=(
            f"{tag}the vendor's overlap bars are the cached ones x{factor:.4f}, which matches no "
            f"calendar split and no split ratio: {listing}"))
    why = (f"calendar split(s) {_describe_splits(matched)}" if matched is not None else
           f"split-like ratio {ratio_text}, no calendar event"
           + (" (calendar unavailable)" if calendar_failed else ""))
    prov = tuple(d.date() for d in scaled if _classify(c.loc[d], v.loc[d], factor) == _PROVISIONAL)
    return TopUpVerdict(VERDICT_BASIS_CHANGE, factor=factor, provisional_days=prov,
                        spanned_splits=spanned, shared=len(usable), reason=(
        f"{tag}the vendor's bars for {scaled[0].date()}..{scaled[-1].date()} are the cached ones "
        f"x{factor:.4f} ({why}): the vendor re-based its history, so appending its new bars would "
        f"put them on another basis than the cached history"))


def _spanned_verdict(c: pd.DataFrame, new: pd.DataFrame, spanned, base: dict, tag: str,
                     last_cached: date) -> TopUpVerdict:
    """Overlap agrees, but the new bars cross calendar split(s): look for the as-traded step."""
    series = pd.concat([c, new]).sort_index()
    unadjusted, undetectable = [], []
    for s in spanned:
        ex = series.index.searchsorted(pd.Timestamp(s.date), side="left")
        if ex <= 0 or ex >= len(series):
            continue
        r = float(series["Close"].iloc[ex] / series["Close"].iloc[ex - 1])
        ratio = float(s.ratio)
        f = max(ratio, 1.0 / ratio)
        if f < _MIN_DETECTABLE_FACTOR:
            undetectable.append((s, r))
        elif abs(math.log(r) + math.log(ratio)) < abs(math.log(r)):
            unadjusted.append((s, r))
    if unadjusted or undetectable:
        seen = ", ".join(f"{s.date.isoformat()} x{float(s.ratio):g} (step x{r:.4f}"
                         + (", too small to tell from prices)" if (s, r) in undetectable else ")")
                         for s, r in unadjusted + undetectable)
        return TopUpVerdict(VERDICT_VENDOR_UNADJUSTED, **base, reason=(
            f"{tag}the new bars cross calendar split(s) {seen}, yet the vendor's bars up to "
            f"{last_cached} still equal the cached, unadjusted ones: the vendor has not adjusted its "
            f"pre-split history. Appending would write a mixed-basis file and so would a full "
            f"re-fetch; nothing is written until the vendor adjusts (a later refresh retries)"))
    return TopUpVerdict(VERDICT_CALENDAR_SPLIT, **base, reason=(
        f"{tag}the new bars cross calendar split(s) {_describe_splits(spanned)} with no as-traded "
        f"step at the ex-date and the overlap agrees; the vendor is on one basis across the split, "
        f"and only a full re-fetch proves the older cached history is on it too"))


def _unverified_step(c: pd.DataFrame, new: pd.DataFrame) -> Optional[Tuple[date, float, str]]:
    if new.empty:
        return None
    series = pd.concat([c.tail(1), new]).sort_index()
    close = series["Close"].to_numpy(dtype=float)
    for i in range(1, len(close)):
        if not (close[i] > 0 and close[i - 1] > 0):
            continue
        r = close[i] / close[i - 1]
        if max(r, 1.0 / r) < _STEP_JUMP:
            continue
        ratio = _split_like_ratio(r, _STEP_RATIO_TOL)
        if ratio is not None:
            return series.index[i].date(), r, ratio
    return None


def verify_replacement(probe: pd.DataFrame, fresh: pd.DataFrame, cached_days: Sequence[date], *,
                       symbol: str = "") -> None:
    """A full re-fetch about to REPLACE the cache must reproduce the vendor answer that triggered
    it: every ``cached_days`` session present, and every session both hold equal (or the probe's
    bar a provisional snapshot of the fresh one). Raises ``OHLCVTopUpRefused`` otherwise."""
    tag = f"{symbol}: " if symbol else ""
    p, f = _by_day(probe), _by_day(fresh)
    missing = [d for d in cached_days if pd.Timestamp(d) not in f.index]
    if missing:
        raise OHLCVTopUpRefused(
            f"{tag}the full re-fetch lacks the session(s) {[d.isoformat() for d in missing]} the "
            f"top-up compared; refusing to replace the cache with it")
    bad = [d for d in p.index if d in f.index and _usable(p.loc[d]) and _usable(f.loc[d])
           and _classify(p.loc[d], f.loc[d], 1.0) == _MISMATCH]
    if bad:
        raise OHLCVTopUpRefused(
            f"{tag}the full re-fetch disagrees with the vendor's own answer moments earlier: "
            + "; ".join(_row_text(d, p.loc[d], f.loc[d]) for d in bad)
            + ". Refusing to replace the cache with it")


def shared_mismatches(cached: pd.DataFrame, vendor: pd.DataFrame) -> Tuple[int, List[str]]:
    """``(sessions both hold, [description of each that disagrees])`` -- a cached provisional
    snapshot of the vendor's bar is not a disagreement. For a fetched piece merged INTO a cached
    history (a gap fill, a head extension), where the pieces meet."""
    c, v = _by_day(cached), _by_day(vendor)
    shared = [d for d in c.index if d in v.index and _usable(c.loc[d]) and _usable(v.loc[d])]
    bad = [_row_text(d, c.loc[d], v.loc[d]) for d in shared
           if _classify(c.loc[d], v.loc[d], 1.0) == _MISMATCH]
    return len(shared), bad
