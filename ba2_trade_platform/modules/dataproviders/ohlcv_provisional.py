"""Replace a STUCK PROVISIONAL daily bar with the vendor's final bar (live app only).

THE BUG (8082 error log, Monday 2026-10-05). A daily refresh near 09:31 New York caches the
current session's bar as it stood: one tick, ``Open == High`` and ``Low == Close`` (AMD 2026-09-28
cached 635.068/635.068/623.78/623.78; the vendor's final bar is 624.9/629.75/596/~600). The
snapshot's open/high lie ABOVE the final day range, so ``ohlcv_topup_guard._classify`` calls the
overlap a mismatch ("the vendor disagrees with the cache where they meet"), every later top-up
REFUSES, and the symbol stays stuck (live analyses then fail for it).

THE FIX. Before the guarded top-up (``MarketDataProviderInterface._verified_tail_topup``, daily
``1d`` only), the cache's NEWEST bar is checked for the provisional signature and, when it holds,
replaced by the vendor's final bar. The guard itself is untouched: a true disagreement (a settled
bar, an older bar, a split, a rescaled history) still refuses / re-bases exactly as before.

WHY THE NEWEST BAR IS THE ANCHOR OF THE REPAIR. A refused top-up never appends past a stuck bar, so
a stuck provisional bar is always the newest cached bar; only a file whose mtime proves that is ever
repaired. An earlier top-up that appended a new bar did not refresh the bar BEFORE it, though, so the
cache can hold more than one stale snapshot (AMD/INTC/MU 2026-09-25 AND 2026-09-28, field failure on
APP 1231). Earlier bars in the newest-5 window are therefore replaced too, but only when each proves
by CONTENT to be a partial-session snapshot (condition 3 below). Anything else that differs is a real
disagreement and the guard refuses.

THE SIGNATURE (all must hold; see :func:`find_provisional_days`):

1. PROOF OF PROVENANCE: the cache file's last-modified time falls on the NEWEST bar's own New York
   session date and before 20:00 ET (:func:`written_mid_session`). Nothing else wrote the file since,
   so that bar was captured while its session was still open. A settled bar, or a file touched on a
   later day / after the close, never matches. (Copying a cache without preserving mtimes defeats the
   proof; the bar is then simply refused as before.)
2. the newest cached bar DIFFERS from the vendor's usable bar for that day and all four cached
   prices lie within ``PROVISIONAL_RANGE_TOL`` (8%) of the vendor's day range (its open/high may
   exceed the final high); a split moves a bar by >= ~10%;
3. every EARLIER bar of the window the vendor also holds either equals the vendor's (an ANCHOR) or is
   a partial-session snapshot: cached volume <= 50% of the vendor's AND O/H/L/C all CONTAINED in
   the vendor's [Low, High] (0.5% tolerance). A partial session is necessarily inside the final
   day's range; a rebased bar is scaled so it is not contained with its volume intact; a settled bar
   equals the vendor's. Any other differing bar means a real disagreement: nothing is replaced.
4. at least one anchor exists. A rebase (split, spin-off: MOD 2026-10-05, every window bar ~9% off)
   fails here and the guard decides, as before.

Only the newest bar and the partial-session snapshots are replaced; no other bar is touched. Installed by
``core.seam_wiring.wire_all_seams`` (live app); the test platform / backtests never run this.
"""
from __future__ import annotations

import functools
import math
import os
import re
import time
from datetime import date, datetime
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from ba2_common.logger import logger

__all__ = [
    "PROVISIONAL_MAX_BARS",
    "written_mid_session",
    "PROVISIONAL_RANGE_TOL",
    "find_provisional_days",
    "replace_provisional_bars",
    "install",
]

#: Only this many of the newest cached bars can be a provisional snapshot (the guard's overlap).
PROVISIONAL_MAX_BARS = 5
#: The cached prices must lie inside the vendor's day range widened by this fraction.
PROVISIONAL_RANGE_TOL = 0.08

#: FMP's end-of-day bar can keep changing until the evening: a write before this NY hour is not settled.
SETTLED_AFTER_HOUR_ET = 20

_OHLC = ["Open", "High", "Low", "Close"]


def _by_day(df: pd.DataFrame) -> pd.DataFrame:
    from ba2_common.core.ohlcv_topup_guard import day_index
    cols = _OHLC + (["Volume"] if "Volume" in df.columns else [])
    out = df[cols].apply(pd.to_numeric, errors="coerce").astype(float)
    if "Volume" not in out.columns:
        out["Volume"] = float("nan")
    out.index = day_index(df["Date"])
    return out[~out.index.duplicated(keep="last")].sort_index()


def _usable(r) -> bool:
    return all(math.isfinite(x) and x > 0 for x in (r.Open, r.High, r.Low, r.Close))


def _equal(c, v) -> bool:
    from ba2_common.core.ohlcv_topup_guard import PRICE_ABS_TOL, PRICE_REL_TOL
    return all(abs(getattr(c, k) - getattr(v, k)) <= max(PRICE_REL_TOL * abs(getattr(v, k)), PRICE_ABS_TOL)
               for k in _OHLC)


def written_mid_session(mtime: Optional[float], bar_day) -> bool:
    """True when a file last modified at ``mtime`` (epoch seconds) was written during the NEW YORK
    session of ``bar_day`` (same date, before 20:00 ET): proof the bar it ends with was captured
    while that session was still open."""
    if mtime is None:
        return False
    from ba2_common.core.market_calendar import NY_TZ
    t = datetime.fromtimestamp(mtime, NY_TZ)
    return t.date() == pd.Timestamp(bar_day).date() and t.hour < SETTLED_AFTER_HOUR_ET


def _in_vendor_range(c, v) -> bool:
    lo, hi = v.Low * (1 - PROVISIONAL_RANGE_TOL), v.High * (1 + PROVISIONAL_RANGE_TOL)
    return all(lo <= getattr(c, k) <= hi for k in _OHLC)


#: An earlier bar is a partial-session snapshot only if its volume is at most this share of the vendor's ...
PARTIAL_VOLUME_SHARE = 0.5
#: ... and its prices lie inside the vendor's [Low, High] widened by this fraction.
PARTIAL_CONTAIN_TOL = 0.005


def _is_partial_snapshot(c, v) -> bool:
    if not (math.isfinite(c.Volume) and math.isfinite(v.Volume) and v.Volume > 0):
        return False
    if c.Volume > PARTIAL_VOLUME_SHARE * v.Volume:
        return False
    lo, hi = v.Low * (1 - PARTIAL_CONTAIN_TOL), v.High * (1 + PARTIAL_CONTAIN_TOL)
    return all(lo <= getattr(c, k) <= hi for k in _OHLC)


def describe_differences(cached: pd.DataFrame, vendor: pd.DataFrame, *,
                         lookback_bars: int = PROVISIONAL_MAX_BARS) -> List[str]:
    """One text per bar of the newest ``lookback_bars`` that differs from the vendor's, in the
    guard's own ``DATE cached O/H/L/C ... vs vendor ...`` format (+ volumes)."""
    c, v = _by_day(cached).tail(lookback_bars), _by_day(vendor)
    out = []
    for d in c.index:
        if d in v.index and _usable(c.loc[d]) and _usable(v.loc[d]) and not _equal(c.loc[d], v.loc[d]):
            a, b = c.loc[d], v.loc[d]
            out.append(f"{d.date()} cached O/H/L/C {a.Open:g}/{a.High:g}/{a.Low:g}/{a.Close:g} vs vendor "
                       f"{b.Open:g}/{b.High:g}/{b.Low:g}/{b.Close:g} (volume {a.Volume:g} vs {b.Volume:g})")
    return out


def find_provisional_days(cached: pd.DataFrame, vendor: pd.DataFrame, *, mtime: Optional[float],
                          lookback_bars: int = PROVISIONAL_MAX_BARS) -> List[pd.Timestamp]:
    """The earlier partial-session snapshots of the window plus the newest cached day, when the
    newest bar is a stuck provisional snapshot of the vendor's bar; else ``[]`` (see the module
    docstring for the signature)."""
    if cached is None or cached.empty or vendor is None or vendor.empty:
        return []
    c = _by_day(cached).tail(lookback_bars)
    v = _by_day(vendor)
    newest = c.index.max()
    if not written_mid_session(mtime, newest) or newest not in v.index:
        return []
    candidate, partial, anchors = None, [], 0
    for d in c.index:
        if d not in v.index:
            continue
        cr, vr = c.loc[d], v.loc[d]
        if not (_usable(cr) and _usable(vr)):
            continue
        if _equal(cr, vr):
            anchors += 1
        elif d == newest and _in_vendor_range(cr, vr):
            candidate = d
        elif d != newest and _is_partial_snapshot(cr, vr):
            partial.append(d)
        else:
            return []          # any other bar that differs is a real disagreement: not ours
    if candidate is None or anchors < 1:
        return []
    return partial + [candidate]


def replace_provisional_bars(df: pd.DataFrame, vendor: pd.DataFrame,
                             days: List[pd.Timestamp]) -> pd.DataFrame:
    """``df`` with the bars of ``days`` replaced by the vendor's rows (everything else untouched,
    ``Date`` convention of ``df`` kept)."""
    from ba2_common.core.ohlcv_topup_guard import day_index
    dd, vd = day_index(df["Date"]), day_index(vendor["Date"])
    want = set(days)
    keep = df[~np.asarray(dd.isin(want))]
    new = vendor[np.asarray(vd.isin(want))].copy()
    new = new.drop_duplicates(subset=["Date"], keep="last")
    new["effective_date"] = new["Date"]
    for col in df.columns:
        if col not in new.columns:
            new[col] = np.nan
    out = pd.concat([keep, new[list(df.columns)]], ignore_index=True)
    return out.sort_values("Date").reset_index(drop=True)


def repair_provisional_bars(provider, df: pd.DataFrame, symbol: str, interval: str,
                            fetch_end: datetime, *, mtime: Optional[float],
                            lookback_bars: int = PROVISIONAL_MAX_BARS,
                            diag: Optional[list] = None) -> Tuple[pd.DataFrame, List[date]]:
    """Fetch the vendor's bars over the newest ``lookback_bars`` cached sessions and replace the
    newest cached bar when it is a stuck provisional snapshot. Returns ``(frame, replaced days)``;
    the frame is ``df`` itself when nothing is replaced. Writes nothing. A fetch error propagates."""
    from ba2_common.core.ohlcv_topup_guard import day_index
    days = day_index(df["Date"])
    if df.empty or not written_mid_session(mtime, days.max()):
        return df, []              # no vendor call at all unless the file proves a mid-session write
    start = days.sort_values()[-lookback_bars:][0].to_pydatetime()
    probe = provider._get_ohlcv_data_impl(symbol, start, fetch_end, interval)
    if probe is None or probe.empty:
        return df, []
    probe = provider._clean_dataframe(probe.copy())
    if probe.empty:
        return df, []
    probe["Date"] = provider._match_tz(pd.to_datetime(probe["Date"]), pd.to_datetime(df["Date"]))
    found = find_provisional_days(df, probe, mtime=mtime, lookback_bars=lookback_bars)
    if not found:
        if diag is not None:
            diag.extend(describe_differences(df, probe, lookback_bars=lookback_bars))
        return df, []
    out = replace_provisional_bars(df, probe, found)
    return out, [d.date() for d in found]


#: ``(provider, SYMBOL, interval, ISO day)`` already written to the Activity Log in this process:
#: a refused symbol is retried by every pass, the operator needs ONE entry per symbol per day.
_REFUSALS_LOGGED: set = set()
_ROW_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) cached O/H/L/C (\S+) vs vendor ([^\s;]+)")


def _parse_rows(message: str) -> list:
    rows = []
    for m in _ROW_RE.finditer(message):
        row = {"date": m.group(1), "cached_ohlc": m.group(2), "vendor_ohlc": m.group(3)}
        if row not in rows:
            rows.append(row)
    return rows


def _write_activity(**kw) -> None:
    from ba2_common.core.db import log_activity
    log_activity(**kw)


def log_refusal_once(provider_name: str, symbol: str, interval: str, message: str,
                     today: Optional[date] = None) -> bool:
    """Write ONE FAILURE Activity Log entry per symbol per day for a refused top-up (shown in the
    Activity Monitor). Returns True when an entry was written. Never raises."""
    day = (today or date.today()).isoformat()
    key = (provider_name, str(symbol).upper(), interval, day)
    if key in _REFUSALS_LOGGED:
        return False
    _REFUSALS_LOGGED.add(key)
    try:
        from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
        rows = _parse_rows(message)
        _write_activity(
            severity=ActivityLogSeverity.FAILURE,
            # no dedicated type exists (types.py is a shared package); closest existing one
            activity_type=ActivityLogType.ANALYSIS_FAILED,
            description=(f"OHLCV top-up REFUSED for {symbol} ({interval}, {provider_name}): the vendor "
                         f"disagrees with the cached bars; cache left untouched. Review needed. "
                         f"{message}"),
            data={"kind": "ohlcv_topup_refused", "symbol": str(symbol).upper(), "interval": interval,
                  "provider": provider_name, "day": day, "bars": rows, "message": message})
        return True
    except Exception as e:  # noqa: BLE001 -- the refusal itself is still raised to the caller
        logger.warning(f"Could not write the Activity Log entry for the refused top-up of {symbol}: {e}")
        return False


def install(provider_base=None) -> None:
    """Wrap ``MarketDataProviderInterface._verified_tail_topup`` (idempotent, live process only)."""
    if provider_base is None:
        from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
        provider_base = MarketDataProviderInterface
    orig = provider_base._verified_tail_topup
    if getattr(orig, "_ba2_provisional_wrapped", False):
        return

    from ba2_common.core.ohlcv_topup_guard import OHLCVTopUpRefused

    @functools.wraps(orig)
    def _verified_tail_topup(self, df, symbol, interval, provider_name, fetch_end, *,
                             raise_fetch_errors: bool = False):
        try:
            return _inner(self, df, symbol, interval, provider_name, fetch_end, raise_fetch_errors)
        except OHLCVTopUpRefused as e:
            log_refusal_once(provider_name, symbol, interval, str(e))
            raise

    def _inner(self, df, symbol, interval, provider_name, fetch_end, raise_fetch_errors):
        if interval != "1d":
            return orig(self, df, symbol, interval, provider_name, fetch_end,
                        raise_fetch_errors=raise_fetch_errors)
        key = (provider_name, str(symbol).upper(), interval)
        memo = provider_base._TOPUP_REFUSED.get(key)
        memo_live = memo is not None and time.monotonic() - memo[0] < self.TOPUP_REFUSAL_MEMO_S
        try:
            # ORIGINAL first: a normal top-up costs exactly the one vendor call it always did
            return orig(self, df, symbol, interval, provider_name, fetch_end,
                        raise_fetch_errors=raise_fetch_errors)
        except OHLCVTopUpRefused as refusal:
            if memo_live:
                raise                      # refused moments ago: no extra vendor call here
            try:
                from ba2_common.core import native_cache
                path = native_cache.find_timeseries_path(provider_name, symbol, interval)
                mtime = os.path.getmtime(path) if path else None
                diag: list = []
                df2, replaced = repair_provisional_bars(self, df.copy(), symbol, interval, fetch_end,
                                                        mtime=mtime, diag=diag)
            except Exception as e:  # noqa: BLE001 -- the refusal stands; say why the repair did not run
                logger.warning(f"{provider_name} {symbol} ({interval}): provisional-bar check skipped: {e}")
                raise_original = True
                diag = []
            else:
                raise_original = not replaced
            if raise_original:
                if diag:       # the whole picture: every bar of the window that differs, not just the guard's
                    raise OHLCVTopUpRefused(
                        f"{refusal} | every bar of the newest {PROVISIONAL_MAX_BARS} that differs from the "
                        f"vendor: " + "; ".join(diag)) from refusal
                raise
        # repaired: ask the guard afresh, ONCE (a second refusal propagates and is reported)
        provider_base._TOPUP_REFUSED.pop(key, None)
        logger.info(f"{provider_name} {symbol} ({interval}): the top-up REFUSED just above was a stuck "
                    f"provisional bar: replacing {[d.isoformat() for d in replaced]} with the "
                    f"vendor's final bar(s) and retrying once")
        out, action = orig(self, df2, symbol, interval, provider_name, fetch_end,
                           raise_fetch_errors=raise_fetch_errors)
        # "unchanged" would drop the repair: the caller only writes on append/replaced
        return out, ("append" if action == "unchanged" else action)

    _verified_tail_topup._ba2_provisional_wrapped = True
    provider_base._verified_tail_topup = _verified_tail_topup
