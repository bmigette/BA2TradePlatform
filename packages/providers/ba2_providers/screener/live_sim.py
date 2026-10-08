"""Simulation of the LIVE FMP stock screener for the backtest gate (``criteria_version`` ``live-daily-v1``).

LIVE IS THE REFERENCE AND IS NOT CHANGED.  This module reproduces, for a decision day D and a finite
universe, what ``ba2_providers.StockScreener.StockScreener(...).screen()`` (``as_of=None``) would have returned
at the open of D, from stored data only.  Order of operations (identical to live, see ``select_from_columns``):

  1. vendor stage      band on the market cap (inclusive), price floor / ceiling on the vendor ``price`` (the
                       quote), ``volume_min`` on the vendor ``volume`` (today's session volume so far); the
                       vendor returns the rows sorted by market cap, descending
  2. RVOL stage        (only when ``relative_volume_min > 0``): rvol >= min, ``volume_max`` on the last finished
                       session's volume
  3. Weinstein         (optional) stage 2 only
  4. rank              by market cap, descending (stable)
  5. price drop        walk the ranked list, keep ``drop >= price_drop_pct``, stop after ``max_stocks`` passes

WHAT IS IMPORTED FROM LIVE, WHAT IS RE-WRITTEN.  ``classify_weinstein_stage`` is imported from
``ba2_common.core.weinstein`` (live calls the same function; the panel's vectorised stage is tested against it).
The RVOL, price-drop and Weinstein-window formulas of live are entangled with HTTP (``_fetch_history_bulk``) so
their pure equivalents live here (``rvol_scalar``, ``drop_scalar``, ``weinstein_scalar``) and
``packages/providers/tests/test_live_sim_parity.py`` feeds the SAME bars to live's code path (HTTP faked at
``fmp_http_get``) and to these functions, asserting equality on many random inputs; the vectorised panel is in
turn asserted equal to the scalar functions.  ``StockScreener.screen()`` itself is run end to end against the same
faked vendor and compared with ``select_from_columns``.

THE THREE LIVE BEHAVIOURS UNDER REVISION (branch ``fix/live-screener-quirks``) are isolated in ``LiveBehaviour``
and the three marked functions ``float_filter_applies``, ``volume_min_test`` and ``rvol_stage_runs``; nothing else
in this module knows about them.  ``behaviour_from_live()`` reads the capability flags the fix branch sets on
``StockScreener`` (``SUPPORTS_FLOAT_FILTER``, ``VOLUME_MIN_IS_AVERAGE``, ``RVOL_ZERO_SKIPS_STAGE``; defaults =
the current quirky behaviour).  ``LEGACY_CURRENT`` is the behaviour that produced the September-October 2026
recorded live picks; it is a VALIDATION-ONLY parameter (``tools/screener_parity_report.py --behaviour legacy``),
no job path passes it.

WHAT THE BACKTEST CANNOT REPRODUCE (measured sizes in ``docs/plans/2026-10-08-screener-live-sim.md``):
  * the live quote's exact second (30-90 s after the open): "now" for the drop / price floor is the opening
    print of the session (owner-approved exception, screener only) when the decision is on the first bar, else
    the latest ENDED intraday bar's close at T.  Drop margins within ~1.8 % of price flip with it.
  * the vendor's share count per past date: ``shares`` = FMP's implied share series (cached historical market cap
    / close) delayed by ``SHARES_LAG_DAYS`` (filing lag) and scaled per symbol to the vendor's own count of the
    most recent vendor snapshot.  The market cap used for the band is ``close(D-1) x shares``.
  * volume so far at T: summed over ENDED intraday bars; 0 on a first-bar decision (the first minutes' volume
    is not knowable from ended bars).  A ``volume_min`` > 0 therefore selects NOTHING on a first-bar decision.
  * tie order of equal market caps (the vendor's order is unknown): symbol ascending.
  * the rank key: live re-reads the cap from ``/quote`` before ranking when the RVOL stage ran; the simulation
    ranks by the band cap (previous close x shares).  Differs only for neighbours at the ``max_stocks`` cut.
  * a bar of D that FMP returns during the session (forming bar) is treated as absent (measured identical).
  * missing bars of a symbol inside a window (cache gaps) are skipped; the n-session trim of the drop window is
    applied on the common NYSE session grid.
  * delisted / renamed names: the universe is the finite store universe.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np

#: Part of every job identity that uses the simulation (name token, checkpoint fingerprint, panel manifest).
CRITERIA_VERSION = "live-daily-v1"
#: ... and its job-name token (``tools/matrix_flags.SCREENER_CRITERIA_NAME_TOKEN`` is pinned equal by a test).
CRITERIA_NAME_TOKEN = "-lds1"
#: Filing lag applied to the implied share series (days).  See ``build_shares_matrix``.
SHARES_LAG_DAYS = 45
PANEL_FORMAT = 3          # 3: day-major arrays (sessions x symbols): one decision day is one contiguous row

#: FMP's ``historical-price-full`` returns the session's FORMING bar when queried at ~09:31 (dated today, close =
#: the current price).  Live's price-drop window and its Weinstein filter read bars WITHOUT dropping it (only the
#: RVOL stage drops "today").  MEASURED 2026-10-08 on prod instance 7 (Weinstein on): the two residual live-only
#: picks HOOD 2026-09-14 and WDC 2026-09-21 are stage 2 only with the opening print appended as the last close
#: (HOOD slope 0.47 % -> 0.85 %; WDC below its SMA -> above).  ONE-LINE SWITCH.  The forming bar is modelled as
#: one bar with open = high = low = close = "now".
FORMING_BAR_PRESENT = True

#: Live's windows (calendar days added to the lookback; ``StockScreener._fetch_history_bulk``: ``anchor - (lookback + 5)``).
RVOL_LOOKBACK_DAYS = 30          # _quotes_from_bars -> window(20) + 10
RVOL_WINDOW_BARS = 20
WEINSTEIN_LOOKBACK_DAYS = 250    # _filter_by_weinstein_stage2
WEINSTEIN_MIN_CLOSES = 170       # sma_period 150 + slope_lookback 20
CALENDAR_PAD = 5


#: Live's data-outage guard (``StockScreener.SCREENER_DATA_FAILURE_MAX_FRACTION``).
SCREENER_DATA_FAILURE_MAX_FRACTION = 0.10


class ScreenerDataOutage(RuntimeError):
    """More than 10 % of a day's screened candidates have no finished daily bars: the cache is incomplete.
    JOB-FATAL (``job_fatal.JOB_FATAL_ERROR_TYPES``), like live's ``ScreenerDataError``."""


class SimulationRefusal(RuntimeError):
    """A setting or input the simulation cannot reproduce faithfully.  Raised instead of approximating."""


# ======================================================================================================
# The three behaviours that changed in live (``fix/live-screener-quirks`` @ 75789e0d), isolated
# ======================================================================================================
@dataclass(frozen=True)
class LiveBehaviour:
    name: str
    float_filter: bool              # live has a float stage (stage 1b, bulk shares_float table)
    volume_is_average: bool         # volume_min / volume_max are AVERAGE-volume filters in stage 2
    rvol_zero_skips_stage: bool     # rvol_min == 0 skips the whole stage 2 (pre-fix)


#: Live as of ``75789e0d``: what jobs simulate.
POST_FIX = LiveBehaviour("post-fix", float_filter=True, volume_is_average=True, rvol_zero_skips_stage=False)
#: Live BEFORE the fix: float settings no-ops, ``volume_min`` sent to the vendor (session volume so far),
#: ``volume_max`` on the last session's volume, stage 2 skipped at ``rvol_min == 0``.  Produced the recorded
#: September-October 2026 picks.  VALIDATION ONLY (``tools/screener_parity_report.py --behaviour legacy``);
#: no job path passes it.
LEGACY_CURRENT = LiveBehaviour("legacy-pre-fix", float_filter=False, volume_is_average=False,
                               rvol_zero_skips_stage=True)


def behaviour_from_live() -> LiveBehaviour:
    """The behaviour of the live screener in THIS checkout: the post-fix contract when the checkout carries
    ``ba2_providers.screener.float_filter`` (the fix's module), else the pre-fix one."""
    import importlib.util
    return POST_FIX if importlib.util.find_spec("ba2_providers.screener.float_filter") else LEGACY_CURRENT


def weinstein_forming_pass(wa: np.ndarray, wp: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Weinstein stage 2 when the forming bar's close ``x`` is the LAST close: ``wa`` = sum of the last 149 finished
    closes, ``wp`` = the prior SMA (150 closes ending 20 bars before the end), both NaN where the window holds
    fewer than 170 closes.  sma_now = (wa + x)/150; above = x > sma_now; rising = slope > 0.5 % (the classifier's
    arithmetic, vectorised).  Monotone increasing in ``x``."""
    with np.errstate(invalid="ignore", divide="ignore"):
        sma = (wa + x) / 150.0
        slope = (sma - wp) / wp * 100.0
        return (wp > 0) & (x > sma) & (slope > 0.5)


def float_mask(beh: LiveBehaviour, fl: np.ndarray, fmin: float, fmax: float) -> np.ndarray:
    """Float stage (vectorised twin of ``float_filter.apply_float_filter``, asserted equal in the tests): a
    symbol whose float is unknown (NaN or <= 0) passes; known floats must satisfy ``fmin <= f <= fmax`` (a bound
    of 0 is off).  Pre-fix: the float settings selected nothing (no-op)."""
    if not beh.float_filter or (fmin <= 0 and fmax <= 0):
        return np.ones(fl.shape, dtype=bool)
    with np.errstate(invalid="ignore"):
        unknown = ~(fl > 0)
        ok = np.ones(fl.shape, dtype=bool)
        if fmin > 0:
            ok &= fl >= fmin
        if fmax > 0:
            ok &= fl <= fmax
    return unknown | ok


def rvol_stage_runs(beh: LiveBehaviour, rvol_min: float) -> bool:
    """Whether stage 2 runs.  Post-fix: always.  Pre-fix: only when ``rvol_min > 0``."""
    return True if not beh.rvol_zero_skips_stage else rvol_min > 0


def rvol_test_applies(rvol_min: float) -> bool:
    return rvol_min > 0


# ======================================================================================================
# Rounding exactly as Python's round(x, 2), vectorised
# ======================================================================================================
def py_round2(x: np.ndarray) -> np.ndarray:
    """``round(x, 2)`` as Python computes it (correctly rounded decimal), vectorised: numpy's ``round`` differs
    from Python's by one ulp for values within 1e-9 of a rounding tie, so those few are redone in Python."""
    x = np.asarray(x, dtype=np.float64)
    out = np.round(x, 2)
    with np.errstate(invalid="ignore"):
        frac = np.abs((x * 100.0) - np.floor(x * 100.0) - 0.5)
        tie = np.isfinite(x) & (frac < 1e-6)
    if tie.any():
        flat = out.reshape(-1)
        xs = x.reshape(-1)
        for i in np.flatnonzero(tie.reshape(-1)):
            flat[i] = round(float(xs[i]), 2)
    return out


# ======================================================================================================
# Scalar reference definitions (pure equivalents of live's entangled post-filters)
# ======================================================================================================
def _iso(d: Any) -> str:
    return d if isinstance(d, str) else d.isoformat()


def _cutoff(day: str, lookback: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=int(lookback) + CALENDAR_PAD)).isoformat()


def rvol_scalar(dates: Sequence[str], volumes: Sequence[Optional[float]], day: str) -> Optional[Tuple[float, float, float]]:
    """``(last_volume, avg20, rvol)`` as live computes them on the morning of ``day`` (``_quotes_from_bars`` +
    ``_enrich_with_rvol``): bars dated within ``[day - 35 calendar days, day)``, last bar = last finished
    session, mean of the last 20 bars INCLUDING it rounded to 2 decimals, ``rvol = round(volume/avg, 2)``.
    ``None`` when there is no bar (live: no quote -> rvol 0 -> dropped)."""
    cut = _cutoff(day, RVOL_LOOKBACK_DAYS)
    bars = [(d, v) for d, v in zip(dates, volumes) if cut <= d < day]
    if not bars:
        return None
    last_vol = bars[-1][1] or 0
    window = bars[-RVOL_WINDOW_BARS:]
    vols = [v for _, v in window if v is not None]
    avg = round(sum(vols) / len(vols), 2) if vols else 0.0
    rvol = round(last_vol / avg, 2) if avg > 0 else 0.0
    return float(last_vol), float(avg), float(rvol)


def drop_scalar(dates: Sequence[str], highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
                day: str, n: int, now: Optional[float], forming: Optional[float] = None) -> Optional[float]:
    """The price-drop percentage live computes (``_filter_by_price_drop``): bars dated in
    ``[day - (n+5) calendar days, day)``, the last ``n`` of them, ``peak = max(max(high, low))``,
    ``current = now`` (the quote) else the last bar's close, ``round((peak-current)/peak*100, 2)``.
    ``None`` = live skips the symbol (no bars, peak <= 0, no price)."""
    cut = _cutoff(day, n)
    idx = [i for i, d in enumerate(dates) if cut <= d < day]
    if not idx and forming is None:
        return None
    bars = [(highs[i] or 0, lows[i] or 0) for i in idx]
    if forming is not None:                       # the forming bar of ``day``: high = low = the current price
        bars.append((forming, forming))
    lb = bars[-n:] if n < len(bars) else bars
    peak = max(max(h, l) for h, l in lb)
    current = now or closes[idx[-1]]
    if peak <= 0 or current is None:
        return None
    return round(((peak - current) / peak) * 100, 2)


def weinstein_scalar(dates: Sequence[str], closes: Sequence[float], day: str, forming: Optional[float] = None) -> bool:
    """Live's Weinstein filter on the morning of ``day``: ``classify_weinstein_stage`` (IMPORTED) on the closes
    of the bars dated in ``[day - 255 calendar days, day)``."""
    from ba2_common.core.weinstein import classify_weinstein_stage
    cut = _cutoff(day, WEINSTEIN_LOOKBACK_DAYS)
    cl = [c for d, c in zip(dates, closes) if cut <= d < day and c is not None]
    if forming is not None:
        cl.append(forming)
    if not cl:
        return False
    return classify_weinstein_stage(cl).get("stage") == 2


# ======================================================================================================
# The selection function (live's order of operations) over ONE day's columns
# ======================================================================================================
_NOW = Union[np.ndarray, Callable[[np.ndarray], Tuple[np.ndarray, np.ndarray]]]

#: Which price the vendor's / the refreshed market cap is struck on at the open.  The live verdict of the
#: open-snapshot experiment decides; ONE-LINE SWITCH ("prev_close" | "now").  Assumed until measured:
#: the previous close (fit of the 2026-09/10 recorded days; XENE 2026-09-18).
BAND_CAP_BASIS = "prev_close"       # stage-1 vendor band
RANK_CAP_BASIS = "prev_close"       # rank key after the stage-2 refresh (/quote marketCap)


def _fnum(settings: Dict[str, Any], key: str) -> float:
    v = settings.get(key)
    return 0.0 if v is None else float(v)


def check_settings(settings: Dict[str, Any], beh: LiveBehaviour) -> None:
    """Refuse (loudly) what the simulation does not reproduce."""
    sm = settings.get("sort_metric") or "market_cap"
    if sm not in ("market_cap", "relative_volume"):
        raise SimulationRefusal(f"sort_metric {sm!r} is not simulated (live ranks by it with data the panel "
                                f"does not carry); supported: market_cap, relative_volume")
    if sm == "relative_volume" and not rvol_stage_runs(beh, _fnum(settings, "relative_volume_min")):
        raise SimulationRefusal("sort_metric relative_volume with relative_volume_min 0 before the fix: "
                                "live ranks by a relative_volume it never computed there")
    if _fnum(settings, "dollar_volume_min") > 0:
        raise SimulationRefusal("dollar_volume_min is a backtest-only filter (live has no such stage)")


def select_from_columns(*, symbols: np.ndarray, shares: np.ndarray, last_close: np.ndarray, rvol: np.ndarray,
                        last_vol: np.ndarray, avg20: np.ndarray, w2: np.ndarray, fl: np.ndarray,
                        peak: Optional[np.ndarray], now: _NOW, wa: Optional[np.ndarray] = None,
                        wp: Optional[np.ndarray] = None,
                        settings: Dict[str, Any], beh: LiveBehaviour,
                        vol_today: Optional[Callable[[np.ndarray], np.ndarray]] = None,
                        valid: Optional[np.ndarray] = None, cut: bool = True,
                        band_basis: Optional[str] = None, rank_basis: Optional[str] = None,
                        diag: Optional[Dict[str, int]] = None) -> np.ndarray:
    """Indices (into the column arrays) of the symbols live would return, IN LIVE'S ORDER.

    ``now(idx)`` -> ``(now_lo, now_hi)`` per candidate: the price knowable at T (equal arrays), or the day's
    low / high bounds for the SUPERSET (the same function, a looser input).  ``vol_today(idx)`` -> session
    volume so far (needed only by the pre-fix ``LEGACY_CURRENT`` ``volume_min``).  ``peak`` = highest high over
    live's drop window (``None`` when no drop filter is set).  ``cut=False`` returns every symbol that passes
    the filters (no ``max_stocks`` cut) for the superset / prune.  Ties on the rank key: symbol ascending.
    ``diag``, when given, receives live's stage accounting (``stage1``, ``dropped_float``, ``dropped_no_history``,
    ``dropped_rvol``, ``dropped_volume_min``, ``dropped_volume_max``, ``weinstein``, ``price_drop``, ``final``)."""
    check_settings(settings, beh)
    band_basis = band_basis or BAND_CAP_BASIS
    rank_basis = rank_basis or RANK_CAP_BASIS
    symbols = np.asarray(symbols)
    if symbols.dtype.kind != "U":
        symbols = symbols.astype(str)
    mcap_prev = last_close * shares
    ok = np.isfinite(mcap_prev) & (mcap_prev > 0)
    if valid is not None:
        ok &= valid
    cmin, cmax = _fnum(settings, "market_cap_min"), _fnum(settings, "market_cap_max")
    if band_basis == "prev_close":
        if cmin > 0:
            ok &= mcap_prev >= cmin
        if cmax > 0:
            ok &= mcap_prev <= cmax
        idx = np.flatnonzero(ok)
    else:                                  # "now": the band is struck on the quote; pre-select loosely first
        if cmin > 0:
            ok &= mcap_prev >= 0.5 * cmin
        if cmax > 0:
            ok &= mcap_prev <= 2.0 * cmax
        idx = np.flatnonzero(ok)
        n_lo, _n_hi = now(idx) if callable(now) else (np.asarray(now)[idx], np.asarray(now)[idx])
        px = np.where(np.isfinite(n_lo), n_lo, last_close[idx])
        c_now = px * shares[idx]
        k = np.ones(idx.size, dtype=bool)
        if cmin > 0:
            k &= c_now >= cmin
        if cmax > 0:
            k &= c_now <= cmax
        idx = idx[k]
    if idx.size == 0:
        return idx
    idx = idx[np.lexsort((symbols[idx], -mcap_prev[idx]))]     # vendor order: cap descending, ties by symbol
    if callable(now):
        now_lo, now_hi = now(idx)
    else:
        now_lo = now_hi = np.asarray(now)[idx]
    pmin, pmax = _fnum(settings, "price_min"), _fnum(settings, "price_max")
    keep = np.ones(idx.size, dtype=bool)
    with np.errstate(invalid="ignore"):
        if pmin > 0:
            keep &= now_hi >= pmin
        if pmax > 0:
            keep &= now_lo <= pmax
    vmin, vmax = _fnum(settings, "volume_min"), _fnum(settings, "volume_max")
    if not beh.volume_is_average and vmin > 0:             # pre-fix: the VENDOR tests today's session volume
        if vol_today is None:
            raise SimulationRefusal("pre-fix volume_min needs the session volume so far (vol_today)")
        with np.errstate(invalid="ignore"):
            keep &= vol_today(idx) >= vmin
    idx, now_lo, now_hi = idx[keep], now_lo[keep], now_hi[keep]
    # stage 1b: float (post-fix)
    dg: Dict[str, int] = diag if diag is not None else {}
    dg["stage1"] = int(idx.size)
    dg["dropped_float"] = dg["dropped_no_history"] = dg["dropped_rvol"] = 0
    dg["dropped_volume_min"] = dg["dropped_volume_max"] = 0
    if idx.size:
        fk = float_mask(beh, fl[idx], _fnum(settings, "float_min"), _fnum(settings, "float_max"))
        dg["dropped_float"] = int((~fk).sum())
        idx, now_lo, now_hi = idx[fk], now_lo[fk], now_hi[fk]
    # stage 2: rvol + average-volume filters (+ the price / cap refresh, which is the rank key below)
    rvmin = _fnum(settings, "relative_volume_min")
    stage2 = rvol_stage_runs(beh, rvmin)
    if idx.size and stage2:
        n_in = idx.size
        needs_bars = rvol_test_applies(rvmin) or vmin > 0 or vmax > 0
        if beh.volume_is_average and not np.isfinite(avg20[idx]).any():
            # TOTAL history failure (no candidate has any bars): raises whatever the bounds (live's stage 2)
            raise ScreenerDataOutage(f"no candidate of {n_in} has finished daily bars: the OHLCV cache is empty/broken")
        with np.errstate(invalid="ignore"):
            if beh.volume_is_average and needs_bars:
                # post-fix: a symbol with no finished-session bars cannot be checked against ANY bound: dropped
                # under its own label; more than 10 % of the candidates -> a data outage, never a smaller list
                nh = ~np.isfinite(avg20[idx])
                if nh.any():
                    dg["dropped_no_history"] = int(nh.sum())
                    if nh.sum() / n_in > SCREENER_DATA_FAILURE_MAX_FRACTION:
                        raise ScreenerDataOutage(
                            f"no finished daily bars for {int(nh.sum())}/{n_in} screened candidates (limit "
                            f"{SCREENER_DATA_FAILURE_MAX_FRACTION:.0%}); first: {[str(symbols[i]) for i in idx[nh][:8]]}: "
                            f"the OHLCV cache is incomplete")
                    idx, now_lo, now_hi = idx[~nh], now_lo[~nh], now_hi[~nh]
            if rvol_test_applies(rvmin) and idx.size:
                k = rvol[idx] >= rvmin
                dg["dropped_rvol"] = int((~k).sum())
                idx, now_lo, now_hi = idx[k], now_lo[k], now_hi[k]
            if beh.volume_is_average:
                if vmin > 0 and idx.size:
                    k = np.nan_to_num(avg20[idx], nan=0.0) >= vmin
                    dg["dropped_volume_min"] = int((~k).sum())
                    idx, now_lo, now_hi = idx[k], now_lo[k], now_hi[k]
                if vmax > 0 and idx.size:
                    k = ~(avg20[idx] > vmax)
                    dg["dropped_volume_max"] = int((~k).sum())
                    idx, now_lo, now_hi = idx[k], now_lo[k], now_hi[k]
            elif vmax > 0 and idx.size:                 # pre-fix: the last session's volume
                k = ~(last_vol[idx] > vmax)
                dg["dropped_volume_max"] = int((~k).sum())
                idx, now_lo, now_hi = idx[k], now_lo[k], now_hi[k]
    wflag = settings.get("weinstein_stage2_only")
    if idx.size and wflag is not None and float(wflag) > 0:
        if FORMING_BAR_PRESENT and wa is not None:
            # the forming bar's close is the price at T; its best case (superset bounds) is the day's high
            x = np.where(np.isfinite(now_hi) & (now_hi > 0), now_hi, last_close[idx])
            k = weinstein_forming_pass(wa[idx], wp[idx], x)
        else:
            k = w2[idx]
        idx, now_lo, now_hi = idx[k], now_lo[k], now_hi[k]
        dg["weinstein"] = int(idx.size)
    if idx.size == 0:
        dg["final"] = 0
        return idx
    # rank: the stage-2 refresh replaced the cap by the live /quote cap (post-fix: always)
    sm = settings.get("sort_metric") or "market_cap"
    if sm == "relative_volume":
        order = np.lexsort((symbols[idx], -rvol[idx]))
    elif rank_basis == "now" and stage2:
        px = np.where(np.isfinite(now_lo) & (now_lo > 0), now_lo, last_close[idx])
        order = np.lexsort((symbols[idx], -(px * shares[idx])))
    else:
        order = np.lexsort((symbols[idx], -mcap_prev[idx]))
    idx, now_lo, now_hi = idx[order], now_lo[order], now_hi[order]
    max_stocks = int(_fnum(settings, "max_stocks"))
    dpct, ddays = _fnum(settings, "price_drop_pct"), int(_fnum(settings, "price_drop_days"))
    if dpct > 0 and ddays > 0:
        if peak is None:
            raise SimulationRefusal("price_drop_pct > 0 but no peak column was supplied")
        pk = peak[idx]
        cur = np.where(np.isfinite(now_lo) & (now_lo > 0), now_lo, last_close[idx])
        if FORMING_BAR_PRESENT:                       # the forming bar's high is part of the peak (>= the price now)
            pk = np.fmax(pk, np.where(np.isfinite(now_lo) & (now_lo > 0), now_lo, np.nan))
        with np.errstate(invalid="ignore", divide="ignore"):
            d = (pk - cur) / pk * 100.0
        good = np.isfinite(pk) & (pk > 0) & np.isfinite(cur)
        # exact round(d, 2) >= pct: only the borderline values are rounded the Python way
        passes = np.zeros(idx.size, dtype=bool)
        far = good & (np.abs(d - dpct) > 0.0051)
        passes[far] = d[far] >= dpct
        for j in np.flatnonzero(good & ~far):
            passes[j] = round(((float(pk[j]) - float(cur[j])) / float(pk[j])) * 100, 2) >= dpct
        idx = idx[passes]
        dg["price_drop"] = int(idx.size)
    if cut and max_stocks > 0:
        idx = idx[:max_stocks]
    dg["final"] = int(idx.size)
    return idx

# ======================================================================================================
# The panel
# ======================================================================================================
_RAW = ("o", "h", "l", "c", "v", "shares", "fl")
_DERIVED = ("rvol", "avg20", "lv", "lc", "w2", "wa", "wp")
_PANEL_FILES = {k: f"{k}.npy" for k in _RAW + _DERIVED}


class DailyPanel:
    """symbols x sessions daily arrays + the genome-independent criterion values (memory-mapped, shared).

    Position ``p`` means "the morning of ``sessions[p]``": bars with index < p are finished.  Derived column
    ``X[p]`` (a contiguous row of the day-major (sessions, symbols) array) is the criterion value live computes
    on that morning."""

    def __init__(self, symbols: List[str], sessions: List[str], arrays: Dict[str, np.ndarray],
                 manifest: Dict[str, Any]):
        self.symbols = np.array(symbols, dtype=str)
        self.sessions = list(sessions)
        self.arrays = arrays
        self.manifest = manifest
        self.sym_index = {s: i for i, s in enumerate(symbols)}
        self._ord = np.array([date.fromisoformat(s).toordinal() for s in sessions], dtype=np.int64)
        self._sess_pos = {s: i for i, s in enumerate(sessions)}
        self._peak: Dict[int, np.ndarray] = {}
        self._hl: Optional[np.ndarray] = None

    # -- geometry -----------------------------------------------------------------------------------
    def pos(self, day: str) -> int:
        """Position of the session ``day`` (the morning being screened)."""
        try:
            return self._sess_pos[day]
        except KeyError:
            raise SimulationRefusal(f"{day} is not a session of the daily panel "
                                    f"({self.sessions[0]}..{self.sessions[-1]})") from None

    def _lo_index(self, lookback: int) -> np.ndarray:
        """For every position p, the first session index dated >= sessions[p] - (lookback + 5) days."""
        return np.searchsorted(self._ord, self._ord - (int(lookback) + CALENDAR_PAD), side="left")

    # -- peak(n) --------------------------------------------------------------------------------------
    def peak(self, n: int) -> np.ndarray:
        """(S, T) view of :meth:`peak_by_day` (symbols x sessions), for callers that index ``[symbol, day]``."""
        return self.peak_by_day(n).T

    def peak_by_day(self, n: int) -> np.ndarray:
        """(T, S) highest high over live's drop window of lookback ``n`` for every morning (memoised per ``n``).
        Window = sessions dated in [D-(n+5) days, D) plus the FORMING bar, the last ``n`` bars of that list (so the
        last ``n-1`` finished sessions when the forming bar is present); NaN where there is no bar."""
        n = int(n)
        got = self._peak.get(n)
        if got is not None:
            return got
        if self._hl is None:
            h = np.asarray(self.arrays["h"]); l = np.asarray(self.arrays["l"])          # (T, S)
            hl = np.fmax(np.nan_to_num(h, nan=-np.inf), np.nan_to_num(l, nan=-np.inf))
            hl[~np.isfinite(hl)] = np.nan
            self._hl = hl
        hl = self._hl
        T, S = hl.shape
        lo = self._lo_index(n)
        p = np.arange(T)
        out = np.full((T, S), np.nan)
        lim = n - 1 if FORMING_BAR_PRESENT else n           # the forming bar takes one of live's ``bars[-n:]``
        for j in range(1, lim + 1):
            rows = np.flatnonzero((p - j) >= np.maximum(lo, 0))   # session p-j lies inside the calendar window
            rows = rows[rows >= j]
            if rows.size == 0:
                break
            out[rows] = np.fmax(out[rows], hl[rows - j])
        if len(self._peak) >= 6:
            self._peak.pop(next(iter(self._peak)))
        self._peak[n] = out
        return out

    # -- one day's columns ------------------------------------------------------------------------------
    def columns(self, p: int) -> Dict[str, np.ndarray]:
        a = self.arrays
        lc = np.asarray(a["lc"][p])
        return {"symbols": self.symbols, "shares": np.asarray(a["shares"][p]),
                "rvol": np.asarray(a["rvol"][p]), "last_vol": np.asarray(a["lv"][p]),
                "avg20": np.asarray(a["avg20"][p]), "w2": np.asarray(a["w2"][p]).astype(bool),
                "wa": np.asarray(a["wa"][p]), "wp": np.asarray(a["wp"][p]),
                "fl": np.asarray(a["fl"][p]), "last_close": lc}

    def day_bounds(self, p: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(low, high, volume) of the session AT position p: the bounds any intraday 'now' / volume so far lies in
        (used ONLY to build the superset, never as a decision input)."""
        a = self.arrays
        return np.asarray(a["l"][p]), np.asarray(a["h"][p]), np.asarray(a["v"][p])

    def select(self, day: str, settings: Dict[str, Any], beh: LiveBehaviour, *, now: _NOW,
               vol_today: Optional[Callable[[np.ndarray], np.ndarray]] = None, cut: bool = True,
               valid: Optional[np.ndarray] = None, diag: Optional[Dict[str, int]] = None) -> List[str]:
        p = self.pos(day)
        cols = self.columns(p)
        dpct, ddays = _fnum(settings, "price_drop_pct"), int(_fnum(settings, "price_drop_days"))
        peak = self.peak_by_day(ddays)[p] if dpct > 0 and ddays > 0 else None
        idx = select_from_columns(**cols, peak=peak, now=now, vol_today=vol_today, settings=settings, beh=beh,
                                  valid=valid, cut=cut, diag=diag)
        return [str(self.symbols[i]) for i in idx]

    def select_bounds(self, day: str, settings: Dict[str, Any], beh: LiveBehaviour, *, cut: bool = False,
                      valid: Optional[np.ndarray] = None) -> List[str]:
        """The SAME selection with ``now`` replaced by the session's [low, high] and the volume so far by the
        full-day volume: a superset of what the gate returns at ANY decision time T of that session."""
        p = self.pos(day)
        lo, hi, vol = self.day_bounds(p)

        def _now(idx):
            return lo[idx], hi[idx]
        return self.select(day, settings, beh, now=_now, vol_today=lambda idx: vol[idx], cut=cut, valid=valid)


# ======================================================================================================
# Building the panel
# ======================================================================================================
def _read_daily(cache_dir: str, sym: str):
    """The symbol's cached daily bars as numpy: ``(ordinals, o, h, l, c, v)`` ascending, de-duplicated (last
    row of a date wins), rows with a NaN close or volume dropped; ``None`` when there is no cache file."""
    import pyarrow.parquet as pq
    for cand in (sym, sym.replace("-", "_"), sym.replace("-", ".")):
        path = os.path.join(cache_dir, f"{cand}_1d.parquet")
        if os.path.exists(path):
            t = pq.read_table(path, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
            d = t.column("Date").to_numpy(zero_copy_only=False).astype("datetime64[D]").astype(np.int64)
            d = d + 719163                                   # days since 1970-01-01 -> date.toordinal()
            cols = [t.column(n).to_numpy(zero_copy_only=False).astype(np.float64)
                    for n in ("Open", "High", "Low", "Close", "Volume")]
            ok = np.isfinite(cols[3]) & np.isfinite(cols[4])
            d, cols = d[ok], [c[ok] for c in cols]
            if d.size > 1 and not (np.diff(d) > 0).all():
                order = np.argsort(d, kind="stable")
                d, cols = d[order], [c[order] for c in cols]
                last = np.concatenate((d[1:] != d[:-1], [True]))      # keep the last row of a date
                d, cols = d[last], [c[last] for c in cols]
            return (d, *cols)
    return None


def _derive_symbol(sess_ord: np.ndarray, vi: np.ndarray, vol: np.ndarray, cl: np.ndarray, lo_rv: np.ndarray,
                   lo_w: np.ndarray, T: int, out: Dict[str, np.ndarray], s: int) -> None:
    """Fill the derived columns of one symbol from its valid bars (``vi`` session indices, ascending)."""
    if vi.size == 0:
        return
    p = np.arange(T)
    e = np.searchsorted(vi, p, side="left")            # number of finished bars at each morning
    sw = np.searchsorted(vi, lo_rv, side="left")
    n_in = e - sw
    has = n_in > 0
    csv = np.concatenate(([0.0], np.cumsum(vol)))
    k = np.minimum(RVOL_WINDOW_BARS, n_in)
    with np.errstate(invalid="ignore", divide="ignore"):
        avg = np.where(has, py_round2((csv[e] - csv[np.maximum(e - k, 0)]) / np.maximum(k, 1)), np.nan)
        lvol = np.where(has, vol[np.maximum(e - 1, 0)], np.nan)
        rv = np.where(has, np.where(avg > 0, py_round2(np.nan_to_num(lvol) / np.where(avg > 0, avg, 1.0)), 0.0), np.nan)
    out["avg20"][s] = avg
    out["lv"][s] = lvol
    out["rvol"][s] = rv
    # the previous close exists only for a symbol with a finished bar inside live's 35-day window (a vendor
    # lists only ACTIVELY TRADING names; a delisted symbol's last close must not stay a candidate forever)
    out["lc"][s] = np.where(has, cl[np.maximum(e - 1, 0)], np.nan)
    # Weinstein on the closes of the 255-day window
    sww = np.searchsorted(vi, lo_w, side="left")
    cnt = e - sww
    if cl.size >= 170:
        from numpy.lib.stride_tricks import sliding_window_view
        M = np.concatenate((np.full(149, np.nan), sliding_window_view(cl, 150).mean(axis=-1)))
        ok = (cnt >= WEINSTEIN_MIN_CLOSES) & (e >= 171)
        ee = np.maximum(e, 21)
        now_ = M[ee - 1]
        prior = M[ee - 21]
        with np.errstate(invalid="ignore", divide="ignore"):
            slope = (now_ - prior) / prior * 100.0
            st2 = ok & (prior > 0) & (cl[ee - 1] > now_) & (slope > 0.5)
        out["w2"][s] = st2
        # the forming-bar variant: sum of the last 149 finished closes and the prior SMA (needs 169 finished closes
        # in the 255-day window, 170 with the forming one)
        csc = np.concatenate(([0.0], np.cumsum(cl)))
        ok2 = (cnt >= WEINSTEIN_MIN_CLOSES - 1) & (e >= 170)
        e2 = np.maximum(e, 170)
        A = csc[e2] - csc[e2 - 149]
        P = M[e2 - 20]
        out["wa"][s] = np.where(ok2, A, np.nan)
        out["wp"][s] = np.where(ok2, P, np.nan)
    # (symbols with < 170 closes never reach stage 2: the array was initialised False)


def build_panel_arrays(bars: Dict[str, Tuple[np.ndarray, ...]], sessions: List[str],
                       shares: Optional[np.ndarray], symbols: List[str], progress: Optional[Callable[[str], None]] = None,
                       fl: Optional[np.ndarray] = None) -> Dict[str, np.ndarray]:
    """``bars[sym] = (session_idx, o, h, l, c, v)`` -> the raw and derived arrays.  Pure numpy."""
    S, T = len(symbols), len(sessions)
    sess_ord = np.array([date.fromisoformat(s).toordinal() for s in sessions], dtype=np.int64)
    arr = {k: np.full((S, T), np.nan) for k in ("o", "h", "l", "c", "v", "rvol", "avg20", "lv", "lc")}
    arr["w2"] = np.zeros((S, T), dtype=np.uint8)
    arr["wa"] = np.full((S, T), np.nan)
    arr["wp"] = np.full((S, T), np.nan)
    arr["shares"] = np.full((S, T), np.nan) if shares is None else shares
    arr["fl"] = np.full((S, T), np.nan) if fl is None else fl
    lo_rv = np.searchsorted(sess_ord, sess_ord - (RVOL_LOOKBACK_DAYS + CALENDAR_PAD), side="left")
    lo_w = np.searchsorted(sess_ord, sess_ord - (WEINSTEIN_LOOKBACK_DAYS + CALENDAR_PAD), side="left")
    for s, sym in enumerate(symbols):
        b = bars.get(sym)
        if b is None:
            continue
        vi, o, h, l, c, v = b
        for k, x in (("o", o), ("h", h), ("l", l), ("c", c), ("v", v)):
            arr[k][s, vi] = x
        _derive_symbol(sess_ord, vi, np.asarray(v, dtype=np.float64), np.asarray(c, dtype=np.float64),
                       lo_rv, lo_w, T, arr, s)
        if progress and s % 500 == 0:
            progress(f"derived {s}/{S}")
    # day-major (sessions, symbols): one decision day is one contiguous row (the gate reads rows)
    return {k: np.ascontiguousarray(np.asarray(v).T) for k, v in arr.items()}


def panel_dir_for(cache_folder: str, name: str = "daily_panel") -> str:
    return os.path.join(cache_folder, "screener", name)


def save_panel(path: str, symbols: List[str], sessions: List[str], arrays: Dict[str, np.ndarray],
               manifest: Dict[str, Any]) -> None:
    os.makedirs(path, exist_ok=True)
    tmp = path + ".building"
    os.makedirs(tmp, exist_ok=True)
    for k, a in arrays.items():
        np.save(os.path.join(tmp, _PANEL_FILES[k]), a)
    with open(os.path.join(tmp, "symbols.json"), "w") as f:
        json.dump(symbols, f)
    with open(os.path.join(tmp, "sessions.json"), "w") as f:
        json.dump(sessions, f)
    manifest = dict(manifest, criteria_version=CRITERIA_VERSION, panel_format=PANEL_FORMAT,
                    n_symbols=len(symbols), first_session=sessions[0], last_session=sessions[-1],
                    built_at=datetime.utcnow().isoformat(timespec="seconds"))
    with open(os.path.join(tmp, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    # swap in (the manifest is the last file written, so a half-built directory is never taken for a panel)
    import shutil
    try:
        for fn in os.listdir(tmp):
            os.replace(os.path.join(tmp, fn), os.path.join(path, fn))
    except PermissionError as e:
        raise SimulationRefusal(
            f"cannot replace the panel at {path}: its files are memory-mapped by a running process (Windows keeps "
            f"them locked). Stop the optimizations/workers that use it, or build into another directory "
            f"(--screener-panel). {e}") from None
    shutil.rmtree(tmp, ignore_errors=True)


def read_manifest(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(os.path.join(path, "manifest.json")) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - missing / unreadable = no panel
        return None


def load_panel(path: str) -> DailyPanel:
    man = read_manifest(path)
    if man is None:
        raise SimulationRefusal(f"no daily criteria panel at {path} (run `ba2-test prewarm` / "
                                f"`ba2-test build-screener-metrics --daily-panel`)")
    with open(os.path.join(path, "symbols.json")) as f:
        symbols = json.load(f)
    with open(os.path.join(path, "sessions.json")) as f:
        sessions = json.load(f)
    arrays = {k: np.load(os.path.join(path, fn), mmap_mode="r") for k, fn in _PANEL_FILES.items()}
    return DailyPanel(symbols, sessions, arrays, man)


class PanelCoverageError(SimulationRefusal):
    """The panel cannot serve this run (absent, wrong criteria version, window not covered)."""


def _sessions_of(path: str) -> List[str]:
    try:
        with open(os.path.join(path, "sessions.json")) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def panel_problems(path: str, start_day: str, end_day: str, *, warmup_days: int = 260,
                   need_symbols: Optional[Iterable[str]] = None) -> List[str]:
    """Every reason the panel at ``path`` cannot serve a run over [start_day, end_day] (empty = fine)."""
    man = read_manifest(path)
    if man is None:
        return [f"daily criteria panel missing at {path}"]
    out: List[str] = []
    if man.get("criteria_version") != CRITERIA_VERSION:
        out.append(f"panel criteria_version {man.get('criteria_version')!r} != {CRITERIA_VERSION!r}")
    if man.get("panel_format") != PANEL_FORMAT:
        out.append(f"panel format {man.get('panel_format')!r} != {PANEL_FORMAT}")
    first = (date.fromisoformat(start_day[:10]) - timedelta(days=warmup_days)).isoformat()
    if man["first_session"] > first:
        out.append(f"panel starts {man['first_session']}, the run needs {first} (start - {warmup_days}d warm-up)")
    # data coverage: bars must reach the last session finished before the run's last morning
    sess = [x for x in _sessions_of(path) if x < end_day[:10]]
    need_bar = sess[-1] if sess else end_day[:10]
    if man.get("last_bar_date", "") < need_bar:
        out.append(f"panel bars end {man.get('last_bar_date')}, the run needs bars through {need_bar} "
                   f"(end {end_day[:10]})")
    if man["last_session"] < end_day[:10]:
        out.append(f"panel sessions end {man['last_session']}, the run ends {end_day[:10]}")
    for need in ("shares_vendor_snapshot", "shares_lag_days", "fresh_fraction"):
        if need not in man:
            out.append(f"panel manifest lacks {need!r}")
    if man.get("fresh_fraction", 1.0) < 1.0 - SCREENER_DATA_FAILURE_MAX_FRACTION:
        out.append(f"only {man['fresh_fraction']:.0%} of the panel's symbols have a bar within 6 days of its last bar "
                   f"({man.get('last_bar_date')}): the OHLCV cache is incomplete (live refuses a screen with more "
                   f"than {SCREENER_DATA_FAILURE_MAX_FRACTION:.0%} of candidates lacking history); run "
                   f"`ba2-test fetch-cache --timeframes 1d` for the universe, then rebuild the panel")
    if need_symbols is not None:
        try:
            with open(os.path.join(path, "symbols.json")) as f:
                have = set(json.load(f))
            miss = sorted(set(need_symbols) - have)
            if miss:
                out.append(f"{len(miss)} symbols not in the panel: {miss[:15]}")
        except Exception as e:  # noqa: BLE001
            out.append(f"panel symbols unreadable: {e}")
    return out


def require_panel(path: str, start_day: str, end_day: str, **kw: Any) -> None:
    problems = panel_problems(path, start_day, end_day, **kw)
    if problems:
        raise PanelCoverageError("screener daily panel cannot serve this run:\n  - " + "\n  - ".join(problems) +
                                 "\nBuild/refresh it with `ba2-test prewarm` (screener jobs) or "
                                 "`ba2-test build-screener-metrics --daily-panel`; remote workers receive it with "
                                 "`cache push` (directory screener/daily_panel).")
