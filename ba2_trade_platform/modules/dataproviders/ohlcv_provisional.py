"""Replace a STUCK PROVISIONAL daily bar with the vendor's final bar (live app only).

THE BUG (8082 error log, Monday 2026-10-05). A daily refresh near 09:31 New York caches the
current session's bar as it stood: one tick, ``Open == High`` and ``Low == Close`` (AMD 2026-09-28
cached 635.068/635.068/623.78/623.78; the vendor's final bar is 624.9/629.75/596/~600). The
snapshot's open/high lie ABOVE the final day range, so ``ohlcv_topup_guard._classify`` calls the
overlap a mismatch ("the vendor disagrees with the cache where they meet"), every later top-up
REFUSES, and the symbol stays stuck (live analyses then fail for it).

THE FIX. Before the guarded top-up (``MarketDataProviderInterface._verified_tail_topup``), a daily
cache's newest bars are checked for the provisional SIGNATURE and such a bar is replaced by the
vendor's final bar. The guard itself is untouched: a true disagreement (a settled older bar, a
split, a rescaled history) still refuses / re-bases exactly as before.

THE SIGNATURE (all must hold; see :func:`find_provisional_days`):

1. the cached bar is one of the newest ``PROVISIONAL_MAX_BARS`` cached bars AND is at most
   ``PROVISIONAL_MAX_AGE_DAYS`` calendar days old: a settled historical bar is never a candidate;
2. it is a one-tick snapshot: ``Open == High`` (to 1e-6) and the Close within 0.5% above the Low. A settled
   bar that ALSO has that shape is replaced by the vendor's value too, which is what a correct
   cache would hold anyway (the vendor is the source of truth for the session);
3. the vendor has a usable bar for that day and it DIFFERS from the cached one;
4. the vendor's bar is on the same price basis: all four cached prices lie within
   ``PROVISIONAL_RANGE_TOL`` (8%) of the vendor's day range. A split moves a bar by >= ~10%
   (smaller than the smallest ratio ``SPLIT_RATIO_TOL`` recognises), so a rescaled bar fails this;
5. the basis is PROVEN by anchors: every OTHER cached bar in the window the vendor also holds equals
   the vendor's, and at least one such bar exists. A rebased history (split) fails here, and the
   guard's own logic then decides, as before.

Only the candidate bars are replaced; no other bar is touched. Installed by
``core.seam_wiring.wire_all_seams`` (live app); the test platform / backtests never run this.
"""
from __future__ import annotations

import functools
import math
import re
from datetime import date, datetime
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from ba2_common.logger import logger

__all__ = [
    "PROVISIONAL_MAX_BARS",
    "PROVISIONAL_MAX_AGE_DAYS",
    "PROVISIONAL_RANGE_TOL",
    "is_provisional_shape",
    "find_provisional_days",
    "replace_provisional_bars",
    "install",
]

#: Only this many of the newest cached bars can be a provisional snapshot (the guard's overlap).
PROVISIONAL_MAX_BARS = 5
#: ... and only if the bar is at most this many calendar days old.
PROVISIONAL_MAX_AGE_DAYS = 14
#: The cached prices must lie inside the vendor's day range widened by this fraction.
PROVISIONAL_RANGE_TOL = 0.08
_SHAPE_TOL = 1e-6
_CLOSE_LOW_TOL = 0.005

_OHLC = ["Open", "High", "Low", "Close"]


def _by_day(df: pd.DataFrame) -> pd.DataFrame:
    from ba2_common.core.ohlcv_topup_guard import day_index
    out = df[_OHLC].apply(pd.to_numeric, errors="coerce").astype(float)
    out.index = day_index(df["Date"])
    return out[~out.index.duplicated(keep="last")].sort_index()


def _usable(r) -> bool:
    return all(math.isfinite(x) and x > 0 for x in (r.Open, r.High, r.Low, r.Close))


def _equal(c, v) -> bool:
    from ba2_common.core.ohlcv_topup_guard import PRICE_ABS_TOL, PRICE_REL_TOL
    return all(abs(getattr(c, k) - getattr(v, k)) <= max(PRICE_REL_TOL * abs(getattr(v, k)), PRICE_ABS_TOL)
               for k in _OHLC)


def is_provisional_shape(o: float, h: float, l: float, c: float) -> bool:
    """A one-tick snapshot: Open == High (exactly) and Close at the Low (within ``_CLOSE_LOW_TOL``;
    AMD/INTC are exactly L == C, MU 2026-09-28 was 1074.73 / 1076.77 = 0.19% apart)."""
    return (math.isclose(o, h, rel_tol=_SHAPE_TOL, abs_tol=0.0)
            and 0 <= c - l <= _CLOSE_LOW_TOL * h)


def _in_vendor_range(c, v) -> bool:
    lo, hi = v.Low * (1 - PROVISIONAL_RANGE_TOL), v.High * (1 + PROVISIONAL_RANGE_TOL)
    return all(lo <= getattr(c, k) <= hi for k in _OHLC)


def find_provisional_days(cached: pd.DataFrame, vendor: pd.DataFrame, *,
                          lookback_bars: int = PROVISIONAL_MAX_BARS,
                          today: Optional[date] = None) -> List[pd.Timestamp]:
    """Cached session days that are stuck provisional snapshots of the vendor's bars (see the module
    docstring for the signature). Empty when none, or when the basis is not proven by anchors."""
    if cached is None or cached.empty or vendor is None or vendor.empty:
        return []
    today = today or date.today()
    c = _by_day(cached).tail(lookback_bars)
    v = _by_day(vendor)
    candidates, anchors = [], 0
    for d in c.index:
        if d not in v.index:
            continue
        cr, vr = c.loc[d], v.loc[d]
        if not (_usable(cr) and _usable(vr)):
            continue
        if _equal(cr, vr):
            anchors += 1
            continue
        young = (today - d.date()).days <= PROVISIONAL_MAX_AGE_DAYS
        if (young and is_provisional_shape(cr.Open, cr.High, cr.Low, cr.Close)
                and _in_vendor_range(cr, vr)):
            candidates.append(d)
        else:
            return []          # a bar that is neither equal nor a provisional snapshot: not ours
    return candidates if anchors >= 1 else []


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
                            fetch_end: datetime, *, lookback_bars: int = PROVISIONAL_MAX_BARS,
                            today: Optional[date] = None) -> Tuple[pd.DataFrame, List[date]]:
    """Fetch the vendor's bars over the newest ``lookback_bars`` cached sessions and replace the
    stuck provisional ones. Returns ``(frame, replaced days)``; the frame is ``df`` itself when
    nothing is replaced. Writes nothing. A fetch error propagates to the caller."""
    from ba2_common.core.ohlcv_topup_guard import day_index
    days = day_index(df["Date"])
    # cheap local pre-check: no one-tick bar among the newest bars -> no vendor call at all
    tail = df.assign(_d=days).sort_values("_d").tail(lookback_bars)
    shaped = [r for r in tail.itertuples()
              if all(math.isfinite(float(x)) for x in (r.Open, r.High, r.Low, r.Close))
              and is_provisional_shape(float(r.Open), float(r.High), float(r.Low), float(r.Close))]
    if not shaped:
        return df, []
    start = days.sort_values()[-lookback_bars:][0].to_pydatetime()
    probe = provider._get_ohlcv_data_impl(symbol, start, fetch_end, interval)
    if probe is None or probe.empty:
        return df, []
    probe = provider._clean_dataframe(probe.copy())
    if probe.empty:
        return df, []
    probe["Date"] = provider._match_tz(pd.to_datetime(probe["Date"]), pd.to_datetime(df["Date"]))
    found = find_provisional_days(df, probe, lookback_bars=lookback_bars, today=today)
    if not found:
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
        rows.append({"date": m.group(1), "cached_ohlc": m.group(2), "vendor_ohlc": m.group(3)})
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
        try:
            df2, replaced = repair_provisional_bars(self, df.copy(), symbol, interval, fetch_end)
        except Exception as e:  # noqa: BLE001 -- the guarded top-up below decides and reports
            logger.warning(f"{provider_name} {symbol} ({interval}): provisional-bar check skipped: {e}")
            return orig(self, df, symbol, interval, provider_name, fetch_end,
                        raise_fetch_errors=raise_fetch_errors)
        if not replaced:
            return orig(self, df, symbol, interval, provider_name, fetch_end,
                        raise_fetch_errors=raise_fetch_errors)
        # the symbol may have been refused before the repair: ask the guard afresh
        provider_base._TOPUP_REFUSED.pop((provider_name, str(symbol).upper(), interval), None)
        logger.info(f"{provider_name} {symbol} ({interval}): replacing stuck provisional bar(s) "
                    f"{[d.isoformat() for d in replaced]} with the vendor's final bar(s)")
        out, action = orig(self, df2, symbol, interval, provider_name, fetch_end,
                           raise_fetch_errors=raise_fetch_errors)
        # "unchanged" would drop the repair: the caller only writes on append/replaced
        return out, ("append" if action == "unchanged" else action)

    _verified_tail_topup._ba2_provisional_wrapped = True
    provider_base._verified_tail_topup = _verified_tail_topup
