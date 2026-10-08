"""Simulation of the LIVE FMP stock screener for the backtest gate (``criteria_version`` ``live-daily-v1``).

LIVE IS THE REFERENCE AND IS NOT CHANGED.  This module reproduces, for a decision day D and a finite
universe, what ``ba2_providers.StockScreener.StockScreener(...).screen()`` (``as_of=None``) would have returned
at the decision instant T of D, from stored data only.  Order of operations (identical to live, see ``select_from_columns``):

  1. vendor stage      band on the market cap (previous close x the vendor's share count, inclusive), price floor /
                       ceiling on the quote ("now"); sorted by market cap, descending
  1b. float            unknown float passes; ``float_min <= float <= float_max``
  2. volume / RVOL     ALWAYS: no finished bars -> dropped; ``rvol >= rvol_min`` (if > 0), ``avg_volume >= volume_min``,
                       ``avg_volume <= volume_max``
  3. Weinstein         (optional) stage 2 only
  4. rank              by market cap, descending (ties: symbol ascending)
  5. price drop        walk the ranked list, keep ``drop >= price_drop_pct``, stop after ``max_stocks`` passes

"NOW" AND THE FORMING DAILY BAR.  Live reads FMP's daily history at the scheduled instant T: the history carries the
session's FORMING bar (dated today).  The simulation models it as: close = the price at T ("now"), high = the highest
high over the session's intraday bars ENDED <= T (+ the open), and uses it (i) as the last close of the Weinstein
input, (ii) as one of the last ``n`` bars of the drop window (its high joins the peak), (iii) as the drop test's
current price.  "now" = the close of the latest bar ended at or before T (the ``DecisionPrice`` rule), the OPENING
PRINT only when T lies inside the session's first bar (owner-approved, screener only).  A candidate with no price
is not a candidate: there is no fallback price.

WHAT IS IMPORTED FROM LIVE, WHAT IS RE-WRITTEN.  ``classify_weinstein_stage`` is imported from
``ba2_common.core.weinstein``.  The RVOL, price-drop and Weinstein-window formulas of live are entangled with HTTP
(``_fetch_history_bulk``) so their pure equivalents live here (``rvol_scalar``, ``drop_scalar``, ``weinstein_scalar``)
and ``packages/providers/tests/test_live_sim_parity.py`` feeds the SAME bars to live's code path (HTTP faked at
``fmp_http_get``) and to these functions on random worlds; ``StockScreener.screen()`` itself is run end to end against
the same faked vendor and compared with ``select_from_columns``.

JOBS SIMULATE ``POST_FIX`` (live as of ``fix/live-screener-quirks``), hard-coded; ``LEGACY_CURRENT`` (the code that
produced the 2026-09/10 recorded picks) exists for VALIDATION only (``tools/screener_parity_report.py``).

WHAT THE BACKTEST CANNOT REPRODUCE (measured sizes in ``docs/plans/2026-10-08-screener-live-sim.md``):
  * the live quote's exact second (30-90 s after the open at 09:30);
  * the vendor's share count per past date: the vendor's own dated series when fetched
    (``prewarm --screener-panel``), else FMP's implied series delayed by ``SHARES_LAG_DAYS``;
  * tie order of equal market caps (the vendor's order is unknown): symbol ascending;
  * the rank key: live ranks by the refreshed ``/quote`` cap; the simulation by previous close x shares;
  * missing bars of a symbol inside a window (cache gaps) are skipped; the n-session trim is applied on the common
    NYSE session grid; delisted / renamed names: the universe is the finite store universe.
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
PANEL_FORMAT = 4          # 4: day-major (sessions x symbols), + the as-traded split factor `fac`

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
    """What JOBS simulate: ``POST_FIX``, hard-coded (never decided by which modules this checkout happens to carry);
    its name is part of the job identity (``screener_opt.behaviour``)."""
    return POST_FIX


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

#: MEASURED, 2026-10-08 (recorded live screens at 13:41-13:54 UTC = 09:41-09:54 New York, 2,018 candidates; plus the open snapshots
#: at 09:30:07 / 09:37:49 / 10:00:01):
#:  * the RANK KEY (the /quote ``marketCap`` stage 2 writes onto the candidate) = ``quote price x vendor shares`` (median ratio 1.0000;
#:    vs previous close x shares 0.9993): the cap at "now", always;
#:  * the stage-1 BAND cap is the vendor screener's ``price`` (last trade) x shares: at 09:30:07 the previous close for the names that have not
#:    printed yet (mixed per symbol), from 09:35 on the price AT T.  So the band test is on the previous close at the opening-print decision
#:    (T inside the first bar) and on the price at T for every later T: ``band_at_now`` (the gate sets it from T).
RANK_CAP_BASIS = "now"


def _fnum(settings: Dict[str, Any], key: str) -> float:
    v = settings.get(key)
    return 0.0 if v is None else float(v)


def require_complete_settings(settings: Dict[str, Any], *, where: str = "screener settings") -> None:
    """REFUSE a settings dict that lacks any key that decides a selection.  Live would run a missing key on
    ``StockScreener._DEFAULTS`` (price_min 20, volume_min 500,000, float_min 10M ...) while the simulation read it as
    off: a genome would be backtested without the floors and deployed with them.  No fallback defaults."""
    from ba2_common.core.deploy_parity import missing_screener_keys
    miss = missing_screener_keys(settings)
    if miss:
        raise SimulationRefusal(
            f"{where}: missing selection key(s) {miss}. State every one explicitly (the launcher's built-in base "
            f"states price_min, price_max, volume_min, volume_max, float_min, float_max = 0 and sort_metric "
            f"market_cap; override with --screener-base-json). A missing key would run on StockScreener's "
            f"defaults live.")


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
                        wp: Optional[np.ndarray] = None, fac: Optional[np.ndarray] = None,
                        settings: Dict[str, Any], beh: LiveBehaviour,
                        vol_today: Optional[Callable[[np.ndarray], np.ndarray]] = None,
                        valid: Optional[np.ndarray] = None, cut: bool = True,
                        diag: Optional[Dict[str, int]] = None,
                        forming_hi: Optional[Callable[[np.ndarray], np.ndarray]] = None,
                        band_at_now: bool = False,
                        forming_close: Optional[Callable[[np.ndarray], np.ndarray]] = None) -> np.ndarray:
    """Indices (into the column arrays) of the symbols live would return, IN LIVE'S ORDER.

    ``now(idx)`` -> ``(now_lo, now_hi)`` per candidate: the price knowable at T (equal arrays), or the session's
    low / high bounds for the SUPERSET (the same function, a looser input).  It is evaluated LAZILY, only when a
    stage needs a price (price floor / ceiling, the forming-bar Weinstein, the drop test) and only for the
    candidates still alive.  A candidate with no finite price is NOT a candidate (``dropped_no_price``): there is
    no fallback price.  ``vol_today(idx)`` -> session volume so far (only the pre-fix ``LEGACY_CURRENT``
    ``volume_min``).  ``peak`` = highest high over live's finished-session drop window (``None`` when no drop filter).
    ``cut=False`` returns every symbol that passes the filters (no ``max_stocks`` cut) for the superset / prune.
    Ties on the rank key: symbol ascending.
    ``forming_hi(idx)`` -> the HIGH of the forming daily bar through the decision instant T (the session open and the
    highest high of the intraday bars ended <= T; inside the first bar the opening print); it joins the drop
    window's peak.  ``None``: the forming bar is the single price ``now`` (high = low = close = now).  The superset
    passes the session's full-day high (>= every high-so-far).
    ``forming_close(idx)`` -> the CLOSE of the forming daily bar as FMP's history returns it: the LAST CLOSE of the Weinstein input.  It is
    NOT the quote price: MEASURED 2026-10-08 (1,750 symbols): median |bar close / quote - 1| 0.26 %, p95 1.1 % (the history's forming bar is
    refreshed less often than the quote); QCOM at 10:05 sat 0.9 % below its quote, on the other side of its SMA150, and live (bar close)
    rejected it where the quote would have accepted it.  ``None``: the price at T stands in for it (the backtest has no second price).
    ``band_at_now``: the vendor's band cap is ``price at T x shares`` (every decision after the first bar) instead of
    ``previous close x shares`` (the opening-print decision).  The candidates are pre-selected on the previous-close cap within
    [0.5 x cap_min, 2 x cap_max] (a one-day move beyond that is outside the model), then tested on the price at T; with bounds as "now"
    the min-cap test uses the HIGH and the max-cap test the LOW (a superset of every price in between).
    ``diag``, when given, receives live's stage accounting (``stage1``, ``dropped_float``, ``dropped_no_history``,
    ``dropped_no_price``, ``dropped_rvol``, ``dropped_volume_min``, ``dropped_volume_max``, ``weinstein``,
    ``price_drop``, ``final``)."""
    check_settings(settings, beh)
    symbols = np.asarray(symbols)
    if symbols.dtype.kind != "U":
        symbols = symbols.astype(str)
    # AS-TRADED BASIS of the morning: the OHLCV cache is split-adjusted as of its fetch, the vendor's share count (and the
    # quote live compares with price_min / price_max) is the raw figure as of D; ``fac`` = product of the ratios of the
    # splits dated AFTER D (``as_traded_factor``), NaN = the split calendar is unknown (not a candidate).
    mcap_prev = last_close * shares * (fac if fac is not None else 1.0)
    ok = np.isfinite(mcap_prev) & (mcap_prev > 0)
    if valid is not None:
        ok &= valid
    cmin, cmax = _fnum(settings, "market_cap_min"), _fnum(settings, "market_cap_max")
    if band_at_now:
        if cmin > 0:
            ok &= mcap_prev >= 0.5 * cmin
        if cmax > 0:
            ok &= mcap_prev <= 2.0 * cmax
    else:
        if cmin > 0:
            ok &= mcap_prev >= cmin
        if cmax > 0:
            ok &= mcap_prev <= cmax
    idx = np.flatnonzero(ok)
    dg: Dict[str, int] = diag if diag is not None else {}
    dg["stage1"] = int(idx.size)
    for k_ in ("dropped_float", "dropped_no_history", "dropped_no_price", "dropped_rvol", "dropped_volume_min",
               "dropped_volume_max"):
        dg[k_] = 0
    if idx.size == 0:
        dg["final"] = 0
        return idx
    idx = idx[np.lexsort((symbols[idx], -mcap_prev[idx]))]     # vendor order: cap descending, ties by symbol
    lo = hi = None                                             # "now" bounds, aligned with idx once evaluated

    def keep(mask: np.ndarray) -> None:
        nonlocal idx, lo, hi
        idx = idx[mask]
        if lo is not None:
            lo, hi = lo[mask], hi[mask]

    def need_now() -> None:
        nonlocal lo, hi
        if lo is None and idx.size:
            if callable(now):
                a, b = now(idx)
                lo, hi = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
            else:
                lo = hi = np.asarray(now, dtype=float)[idx]
        if lo is not None:
            bad = ~(np.isfinite(lo) & np.isfinite(hi) & (lo > 0))
            if bad.any():
                dg["dropped_no_price"] += int(bad.sum())
                keep(~bad)

    if band_at_now and (cmin > 0 or cmax > 0) and idx.size:
        need_now()
        f_b = fac[idx] if fac is not None else 1.0
        with np.errstate(invalid="ignore"):
            mb = np.ones(idx.size, dtype=bool)
            if cmin > 0:
                mb &= hi * shares[idx] * f_b >= cmin
            if cmax > 0:
                mb &= lo * shares[idx] * f_b <= cmax
        keep(mb)
        dg["stage1"] = int(idx.size)
    pmin, pmax = _fnum(settings, "price_min"), _fnum(settings, "price_max")
    if pmin > 0 or pmax > 0:
        need_now()
        f_ = fac[idx] if fac is not None else 1.0
        with np.errstate(invalid="ignore"):
            m = np.ones(idx.size, dtype=bool)
            if pmin > 0:
                m &= hi * f_ >= pmin
            if pmax > 0:
                m &= lo * f_ <= pmax
        keep(m)
    vmin, vmax = _fnum(settings, "volume_min"), _fnum(settings, "volume_max")
    if not beh.volume_is_average and vmin > 0 and idx.size:     # pre-fix: the VENDOR tests today's session volume
        if vol_today is None:
            raise SimulationRefusal("pre-fix volume_min needs the session volume so far (vol_today)")
        with np.errstate(invalid="ignore"):
            keep(vol_today(idx) >= vmin)
    # stage 1b: float (post-fix)
    if idx.size:
        fk = float_mask(beh, fl[idx], _fnum(settings, "float_min"), _fnum(settings, "float_max"))
        dg["dropped_float"] = int((~fk).sum())
        keep(fk)
    # stage 2: rvol + average-volume filters (+ the price / cap refresh, which is the rank key below)
    rvmin = _fnum(settings, "relative_volume_min")
    if idx.size and rvol_stage_runs(beh, rvmin):
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
                    keep(~nh)
            if rvol_test_applies(rvmin) and idx.size:
                k = rvol[idx] >= rvmin
                dg["dropped_rvol"] = int((~k).sum())
                keep(k)
            if beh.volume_is_average:
                if vmin > 0 and idx.size:
                    k = np.nan_to_num(avg20[idx], nan=0.0) >= vmin
                    dg["dropped_volume_min"] = int((~k).sum())
                    keep(k)
                if vmax > 0 and idx.size:
                    k = ~(avg20[idx] > vmax)
                    dg["dropped_volume_max"] = int((~k).sum())
                    keep(k)
            elif vmax > 0 and idx.size:                 # pre-fix: the last session's volume
                k = ~(last_vol[idx] > vmax)
                dg["dropped_volume_max"] = int((~k).sum())
                keep(k)
    wflag = settings.get("weinstein_stage2_only")
    if idx.size and wflag is not None and float(wflag) > 0:
        if FORMING_BAR_PRESENT and wa is not None:
            # the forming bar's close is the price at T; its best case (superset bounds) is the session high
            need_now()
            if idx.size:
                x_w = hi
                if forming_close is not None:
                    fc = np.asarray(forming_close(idx), dtype=float)
                    x_w = np.where(np.isfinite(fc) & (fc > 0), fc, hi)
                k = weinstein_forming_pass(wa[idx], wp[idx], x_w)
            else:
                k = np.zeros(0, dtype=bool)
        else:
            k = w2[idx]
        keep(k)
        dg["weinstein"] = int(idx.size)
    if idx.size == 0:
        dg["final"] = 0
        return idx
    # rank: the stage-2 refresh replaced the cap by the live /quote cap (post-fix: always)
    sm = settings.get("sort_metric") or "market_cap"
    if sm == "relative_volume":
        key = rvol[idx]
    elif RANK_CAP_BASIS == "now":
        # the refreshed cap = price now x the vendor's shares.  Ranking never DROPS a candidate (only the filters do): a name with no
        # price at T ranks on its previous-close cap.
        if lo is None:
            if callable(now):
                r_lo = np.asarray(now(idx)[0], dtype=float)
            else:
                r_lo = np.asarray(now, dtype=float)[idx]
        else:
            r_lo = lo
        fac_i = fac[idx] if fac is not None else 1.0
        key = np.where(np.isfinite(r_lo) & (r_lo > 0), r_lo * shares[idx] * fac_i, mcap_prev[idx])
    else:
        key = mcap_prev[idx]
    order = np.lexsort((symbols[idx], -key))
    idx = idx[order]
    if lo is not None:
        lo, hi = lo[order], hi[order]
    max_stocks = int(_fnum(settings, "max_stocks"))
    dpct, ddays = _fnum(settings, "price_drop_pct"), int(_fnum(settings, "price_drop_days"))
    if dpct > 0 and ddays > 0:
        if peak is None:
            raise SimulationRefusal("price_drop_pct > 0 but no peak column was supplied")
        need_now()
        cur = lo
        pk = peak[idx]
        if FORMING_BAR_PRESENT and idx.size:           # the forming bar's high is part of the peak (>= the price now)
            fh = forming_hi(idx) if forming_hi is not None else lo
            pk = np.fmax(pk, np.where(np.isfinite(fh) & (fh > 0), fh, np.nan))
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
_RAW = ("o", "h", "l", "c", "v", "shares", "fl", "fac")
_DERIVED = ("rvol", "avg20", "lv", "lc", "w2", "wa", "wp")
_PANEL_FILES = {k: f"{k}.npy" for k in _RAW + _DERIVED}


class _PeakView:
    def __init__(self, panel: "DailyPanel", n: int, transpose: bool):
        self._p, self._n, self._t = panel, n, transpose

    def __getitem__(self, key):
        if isinstance(key, tuple):
            a, b = key
            p, i = (b, a) if self._t else (a, b)
            return self._p.peak_row(self._n, int(p))[i]
        return self._p.peak_row(self._n, int(key))


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
        import threading
        self._lock = threading.Lock()
        self._lo_memo: Dict[int, np.ndarray] = {}
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
    def _lo_for(self, n: int) -> np.ndarray:
        got = self._lo_memo.get(n)
        if got is None:
            got = self._lo_memo.setdefault(n, self._lo_index(n))
        return got

    def _hl_arr(self) -> np.ndarray:
        if self._hl is None:
            with self._lock:
                if self._hl is None:
                    h = np.asarray(self.arrays["h"]); l = np.asarray(self.arrays["l"])      # (T, S)
                    hl = np.fmax(np.nan_to_num(h, nan=-np.inf), np.nan_to_num(l, nan=-np.inf))
                    hl[~np.isfinite(hl)] = np.nan
                    self._hl = hl
        return self._hl

    def peak_row(self, n: int, p: int) -> np.ndarray:
        """(S,) highest ``max(high, low)`` over live's drop window of lookback ``n`` on the morning at position ``p``:
        the finished sessions dated in [D-(n+5) days, D), the last ``n-1`` of them when the forming bar takes the
        n-th slot (``FORMING_BAR_PRESENT``); NaN where there is no bar.  Computed per row (no per-n cache: a row costs
        ~n x S float ops, 0.1 ms), so there is nothing to evict and nothing shared between threads but the read-only
        ``max(high, low)`` array."""
        n = int(n)
        hl = self._hl_arr()
        lim = n - 1 if FORMING_BAR_PRESENT else n
        start = max(int(self._lo_for(n)[p]), p - lim, 0)
        if start >= p:
            return np.full(hl.shape[1], np.nan)
        return np.fmax.reduce(hl[start:p], axis=0)

    def peak_by_day(self, n: int) -> "_PeakView":
        """Row-indexed view ``[p]`` / ``[p, symbol_index]`` over :meth:`peak_row` (tests and the report tool)."""
        return _PeakView(self, int(n), transpose=False)

    def peak(self, n: int) -> "_PeakView":
        """The same view indexed ``[symbol_index, p]``."""
        return _PeakView(self, int(n), transpose=True)

    # -- one day's columns ------------------------------------------------------------------------------
    def columns(self, p: int) -> Dict[str, np.ndarray]:
        a = self.arrays
        lc = np.asarray(a["lc"][p])
        return {"symbols": self.symbols, "shares": np.asarray(a["shares"][p]),
                "rvol": np.asarray(a["rvol"][p]), "last_vol": np.asarray(a["lv"][p]),
                "avg20": np.asarray(a["avg20"][p]), "w2": np.asarray(a["w2"][p]).astype(bool),
                "wa": np.asarray(a["wa"][p]), "wp": np.asarray(a["wp"][p]),
                "fl": np.asarray(a["fl"][p]), "fac": np.asarray(a["fac"][p], dtype=np.float64), "last_close": lc}

    def day_bounds(self, p: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(low, high, volume) of the session AT position p: the bounds any intraday 'now' / volume so far lies in
        (used ONLY to build the superset, never as a decision input)."""
        a = self.arrays
        return np.asarray(a["l"][p]), np.asarray(a["h"][p]), np.asarray(a["v"][p])

    def select(self, day: str, settings: Dict[str, Any], beh: LiveBehaviour, *, now: _NOW,
               vol_today: Optional[Callable[[np.ndarray], np.ndarray]] = None, cut: bool = True,
               valid: Optional[np.ndarray] = None, diag: Optional[Dict[str, int]] = None,
               forming_hi: Optional[Callable[[np.ndarray], np.ndarray]] = None, band_at_now: bool = False,
               forming_close: Optional[Callable[[np.ndarray], np.ndarray]] = None) -> List[str]:
        p = self.pos(day)
        cols = self.columns(p)
        dpct, ddays = _fnum(settings, "price_drop_pct"), int(_fnum(settings, "price_drop_days"))
        peak = self.peak_row(ddays, p) if dpct > 0 and ddays > 0 else None
        idx = select_from_columns(**cols, peak=peak, now=now, vol_today=vol_today, settings=settings, beh=beh,
                                  valid=valid, cut=cut, diag=diag,
                                  forming_hi=forming_hi, band_at_now=band_at_now,
                                  forming_close=forming_close)
        return [str(self.symbols[i]) for i in idx]

    def select_bounds(self, day: str, settings: Dict[str, Any], beh: LiveBehaviour, *, cut: bool = False,
                      valid: Optional[np.ndarray] = None, daily_clock: bool = False) -> List[str]:
        """The SAME selection with ``now`` replaced by bounds of everything the gate can read for this morning:
        a superset of what the gate returns at ANY decision time T.

        INTRADAY clock: [min(session low, previous close), max(session high, previous close)] (the previous close
        covers a name that has not traded yet at T: its price is the last session's last bar).  The forming bar's
        high is bounded by the session high.  DAILY clock: the gate reads session D's CLOSE, which for the morning
        of the next session is the previous close ``lc`` itself: a degenerate interval."""
        p = self.pos(day)
        a = self.arrays
        lc = np.asarray(a["lc"][p])
        if daily_clock:
            lo = hi = fh = lc
            vol = np.zeros_like(lc)
        else:
            lo = np.fmin(np.asarray(a["l"][p]), lc)
            hi = np.fmax(np.asarray(a["h"][p]), lc)
            fh = hi
            vol = np.asarray(a["v"][p])

        def _now(idx):
            return lo[idx], hi[idx]
        return self.select(day, settings, beh, now=_now, vol_today=lambda idx: vol[idx], cut=cut, valid=valid,
                           forming_hi=lambda idx: fh[idx], band_at_now=not daily_clock)


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
                       fl: Optional[np.ndarray] = None, fac: Optional[np.ndarray] = None) -> Dict[str, np.ndarray]:
    """``bars[sym] = (session_idx, o, h, l, c, v)`` -> the raw and derived arrays.  Pure numpy."""
    S, T = len(symbols), len(sessions)
    sess_ord = np.array([date.fromisoformat(s).toordinal() for s in sessions], dtype=np.int64)
    arr = {k: np.full((S, T), np.nan) for k in ("o", "h", "l", "c", "v", "rvol", "avg20", "lv", "lc")}
    arr["w2"] = np.zeros((S, T), dtype=np.uint8)
    arr["wa"] = np.full((S, T), np.nan)
    arr["wp"] = np.full((S, T), np.nan)
    arr["shares"] = np.full((S, T), np.nan) if shares is None else shares
    arr["fl"] = np.full((S, T), np.nan) if fl is None else fl
    arr["fac"] = (np.ones((S, T), dtype=np.float32) if fac is None else np.asarray(fac, dtype=np.float32))
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


def panel_root(cache_folder: str) -> str:
    return os.path.join(cache_folder, "screener", "daily_panel")


def panel_dir_for(cache_folder: str, fingerprint: str) -> str:
    """Each panel lives in its OWN directory named by its fingerprint: ``cache push`` compares (path, size), and the
    panel's ``.npy`` files have fixed sizes, so a rebuilt panel written over the old one would never reach a worker."""
    return os.path.join(panel_root(cache_folder), fingerprint)


def panel_rel(cache_folder: str, path: str) -> str:
    """The panel path RELATIVE to the cache root (what a job stores: the master's absolute path means nothing on a
    worker)."""
    return os.path.relpath(path, cache_folder).replace("\\", "/")


def resolve_panel_path(stored: str, cache_folder: Optional[str] = None) -> str:
    """The local directory of a stored panel reference (relative to THIS machine's cache root; absolute paths are
    used as given: scratch builds and tests)."""
    if os.path.isabs(stored):
        return stored
    if cache_folder is None:
        import ba2_common.config as _cfg
        cache_folder = _cfg.CACHE_FOLDER
    return os.path.join(cache_folder, *stored.split("/"))


def list_panels(cache_folder: str) -> List[Tuple[str, Dict[str, Any], float]]:
    """``[(dir, manifest, size_mb)]`` of every complete panel under the cache, newest first."""
    return list_panels_in(panel_root(cache_folder))


def list_panels_in(root: str) -> List[Tuple[str, Dict[str, Any], float]]:
    out = []
    if os.path.isdir(root):
        for name in os.listdir(root):
            d = os.path.join(root, name)
            man = read_manifest(d) if os.path.isdir(d) else None
            if man:
                size = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d)) / 1e6
                out.append((d, man, size))
    return sorted(out, key=lambda t: t[1].get("built_at", ""), reverse=True)


def latest_panel(cache_folder: str) -> Optional[str]:
    """The newest complete panel directory on THIS machine, or None.  Resolved once at LAUNCH and stamped on the job
    (path relative to the cache + fingerprint); a job never switches panel afterwards."""
    lp = list_panels(cache_folder)
    return lp[0][0] if lp else None


def save_panel(path: str, symbols: List[str], sessions: List[str], arrays: Dict[str, np.ndarray],
               manifest: Dict[str, Any]) -> None:
    """Write the panel into ``path`` (a fingerprint-named directory).  Every file is written as ``<name>.tmp`` and
    renamed, the manifest LAST: a directory without a manifest is not a panel, and ``.tmp`` files are never shipped by
    ``cache push``."""
    os.makedirs(path, exist_ok=True)

    def _put(name: str, writer) -> None:
        tmp = os.path.join(path, name + ".tmp")
        with open(tmp, "wb") as f:
            writer(f)
        try:
            os.replace(tmp, os.path.join(path, name))
        except PermissionError as e:
            raise SimulationRefusal(
                f"cannot replace {name} of the panel at {path}: it is memory-mapped by a running process (Windows keeps "
                f"it locked). Stop the optimizations/workers that use it. {e}") from None

    for k, a in arrays.items():
        _put(_PANEL_FILES[k], lambda f, a=a: np.save(f, a))
    _put("symbols.json", lambda f: f.write(json.dumps(symbols).encode()))
    _put("sessions.json", lambda f: f.write(json.dumps(sessions).encode()))
    manifest = dict(manifest, criteria_version=CRITERIA_VERSION, panel_format=PANEL_FORMAT,
                    n_symbols=len(symbols), first_session=sessions[0], last_session=sessions[-1],
                    built_at=datetime.utcnow().isoformat(timespec="seconds"))
    manifest.setdefault("panel_fingerprint", str(manifest.get("source_fingerprint", ""))[:16])
    _put("manifest.json", lambda f: f.write(json.dumps(manifest, indent=1).encode()))


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
    bad = [f"{k} {tuple(a.shape)}" for k, a in arrays.items() if tuple(a.shape) != (len(sessions), len(symbols))]
    if bad:
        raise SimulationRefusal(f"panel at {path} is corrupt or half-synced: arrays {bad} do not match "
                                f"(sessions {len(sessions)}, symbols {len(symbols)})")
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
                   need_symbols: Optional[Iterable[str]] = None, expect_fp: Optional[str] = None) -> List[str]:
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
    for need in ("shares_vendor_snapshot", "shares_lag_days", "stale_listed_symbols", "panel_fingerprint"):
        if need not in man:
            out.append(f"panel manifest lacks {need!r}")
    if expect_fp is not None and man.get("panel_fingerprint") != expect_fp:
        out.append(f"panel fingerprint {man.get('panel_fingerprint')!r} != the job's {expect_fp!r} (another panel was "
                   f"built or synced under this path: a job never switches panel)")
    if man.get("stale_listed_symbols"):
        sl = man["stale_listed_symbols"]
        out.append(f"{len(sl)} symbols are in the vendor's CURRENT listing but their daily bars are stale "
                   f"(last bar > 6 days before {man.get('last_bar_date')}): {sl[:15]}; refresh them with "
                   f"`ba2-test fetch-cache --timeframes 1d --symbols ...` and rebuild the panel (or acknowledge with "
                   f"`--acknowledge-stale` at build time)")
    if man.get("splits_unknown"):
        su = man["splits_unknown"]
        out.append(f"{len(su)} symbols have no split calendar (the as-traded basis of their market cap and price is "
                   f"unknown): {su[:15]}; run `ba2-test prewarm --screener-panel` (it fetches the calendars)")
    if man.get("shares_missing_listed"):
        sm = man["shares_missing_listed"]
        out.append(f"{len(sm)} listed symbols have no share-count source at all: {sm[:15]}")
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
