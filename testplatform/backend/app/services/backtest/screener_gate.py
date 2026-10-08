"""The backtest's screener gate on the DAILY criteria panel (``criteria_version`` ``live-daily-v1``).

ONE selection function (``ba2_providers.screener.live_sim.select_from_columns``) serves three callers, so they cannot
disagree:
  * ``PanelGate.symbols``      the per-decision gate (engine): the decision's own morning, "now" = the price
                               knowable at T (``price_source.screener_now_price``);
  * ``prune_symbols``          the per-trial preload prune: the same function with "now" replaced by the session's
                               [low, high] bounds and NO ``max_stocks`` cut (a cut is not monotone in "now");
  * ``static_universe``        the job's static superset: ``prune_symbols`` at the LOOSEST filter values of every
                               searched gene (``universe_superset.loosest_filter_variants``).

Decision-day mapping (``screen_days``).  INTRADAY clock: the decision at T on session D screens the morning of D
(bars of sessions < D are finished; "now" is the price at T, the opening print when T is inside the first bar).
DAILY clock (``execution_interval=1d``): the decision stamped D uses D's close and fills at D+1's open, so it
screens the morning of the NEXT session with data through D and "now" = D's close (the last price knowable).
"""
from __future__ import annotations

import bisect
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from ba2_providers.screener import live_sim as ls
from ba2_providers.screener import metric_store as ms

_PANEL_MEMO: Dict[str, ls.DailyPanel] = {}


class ScreenerGateRefusal(RuntimeError):
    """The panel gate cannot be built or used for this run (panel missing / wrong version / day not covered)."""


def get_panel(path: str) -> ls.DailyPanel:
    """The memory-mapped panel at ``path``, loaded once per process (the arrays are shared pages)."""
    pan = _PANEL_MEMO.get(path)
    man = ls.read_manifest(path)
    if pan is not None and man is not None and pan.manifest.get("source_fingerprint") == man.get("source_fingerprint"):
        return pan
    try:
        pan = ls.load_panel(path)
    except ls.SimulationRefusal as e:
        raise ScreenerGateRefusal(str(e)) from None
    _PANEL_MEMO[path] = pan
    return pan


def clear_panel_memo() -> None:
    _PANEL_MEMO.clear()


def valid_mask(panel: ls.DailyPanel, excluded_symbols: Optional[Iterable[str]]) -> np.ndarray:
    """Symbols allowed through the gate: everything but the module-level and the per-run exclusions."""
    both = set(ms.EXCLUDED_SYMBOLS) | {str(s).upper() for s in (excluded_symbols or ())}
    if not both:
        return np.ones(len(panel.symbols), dtype=bool)
    return ~np.isin(np.char.upper(panel.symbols.astype(str)), np.array(sorted(both)))


def _iso(d: Any) -> str:
    return d if isinstance(d, str) else d.isoformat()


def screen_days(panel: ls.DailyPanel, start_day: str, end_day: str, *, intraday: bool) -> List[str]:
    """The mornings the gate can screen for decisions on sessions in ``[start_day, end_day]``."""
    s, e = str(start_day)[:10], str(end_day)[:10]
    sess = panel.sessions
    lo = bisect.bisect_left(sess, s)
    hi = bisect.bisect_right(sess, e)
    days = sess[lo:hi]
    if intraday:
        return list(days)
    return [sess[i + 1] for i in range(lo, hi) if i + 1 < len(sess)]      # next session of every decision day


def next_session(panel: ls.DailyPanel, day: str) -> str:
    i = bisect.bisect_right(panel.sessions, day)
    if i >= len(panel.sessions):
        raise ScreenerGateRefusal(f"the daily panel has no session after {day} (panel ends {panel.sessions[-1]}): "
                                  f"rebuild it with a later end")
    return panel.sessions[i]


def prune_symbols(panel: ls.DailyPanel, start_day: str, end_day: str, settings: Dict[str, Any],
                  excluded_symbols: Optional[Iterable[str]], *, intraday: bool,
                  beh: Optional[ls.LiveBehaviour] = None) -> List[str]:
    """Sorted union, over every morning of the window, of what the gate can return for ``settings`` at ANY decision
    time: the SAME selection as the gate, with "now" = the session's [low, high] and no ``max_stocks`` cut."""
    beh = beh or ls.behaviour_from_live()
    valid = valid_mask(panel, excluded_symbols)
    out: set = set()
    for day in screen_days(panel, start_day, end_day, intraday=intraday):
        out.update(panel.select_bounds(day, settings, beh, cut=False, valid=valid))
    return sorted(out)


def static_universe(panel: ls.DailyPanel, start_day: str, end_day: str, base: Dict[str, Any],
                    ranges: Dict[str, Dict[str, Any]], *, intraday: bool,
                    excluded_symbols: Optional[Iterable[str]] = None,
                    beh: Optional[ls.LiveBehaviour] = None) -> List[str]:
    """The job's static superset: ``prune_symbols`` at the loosest value of every searched FILTER gene (and, when a
    drop threshold is enforced at its loosest value, over every searched window length)."""
    from ba2_providers.screener import universe_superset as us
    out: set = set()
    for settings in us.loosest_filter_variants(base, ranges):
        out.update(prune_symbols(panel, start_day, end_day, settings, excluded_symbols,
                                 intraday=intraday, beh=beh))
    return sorted(out)


class PanelGate:
    """Per-run gate.  ``runtime`` = the engine's ``screener_runtime`` ({"panel", "settings", "excluded_symbols",
    "criteria_version"}); ``price_source`` supplies "now"."""

    def __init__(self, runtime: Dict[str, Any], price_source: Any, *, intraday: bool,
                 beh: Optional[ls.LiveBehaviour] = None):
        if runtime["criteria_version"] != ls.CRITERIA_VERSION:
            raise ScreenerGateRefusal(
                f"this run was built for screener criteria {runtime['criteria_version']!r}, the code simulates "
                f"{ls.CRITERIA_VERSION!r}: re-launch the job (a job run under another definition is a different job)")
        self.panel = get_panel(runtime["panel"])
        if self.panel.manifest.get("criteria_version") != ls.CRITERIA_VERSION:
            raise ScreenerGateRefusal(f"panel {runtime['panel']} was built with criteria "
                                      f"{self.panel.manifest.get('criteria_version')!r}, not {ls.CRITERIA_VERSION!r}")
        self.settings = dict(runtime["settings"])
        self.valid = valid_mask(self.panel, runtime.get("excluded_symbols"))
        self.beh = beh or ls.behaviour_from_live()
        self.ps = price_source
        self.intraday = intraday
        self._cache: Dict[Any, List[str]] = {}
        ls.check_settings(self.settings, self.beh)
        if float(self.settings.get("volume_min") or 0) > 0 and not self.beh.volume_is_average and not intraday:
            raise ScreenerGateRefusal("pre-fix volume_min (session volume so far) cannot be simulated on a daily clock")

    def screen_day(self, as_of_dt: datetime) -> str:
        d = as_of_dt.date().isoformat()
        return d if self.intraday else next_session(self.panel, d)

    def symbols(self, as_of_dt: datetime) -> List[str]:
        day = self.screen_day(as_of_dt)
        key = (day, as_of_dt.time()) if self.intraday else (day,)
        got = self._cache.get(key)
        if got is not None:
            return got
        panel, ps, syms = self.panel, self.ps, self.panel.symbols
        memo: Dict[int, float] = {}
        hmemo: Dict[int, float] = {}

        def _now(idx: np.ndarray):
            vals = np.empty(idx.size)
            for j, i in enumerate(idx):
                v = memo.get(i)
                if v is None:
                    px = ps.screener_now_price(str(syms[i]), as_of_dt)
                    v = float("nan") if px is None else float(px)
                    memo[i] = v
                vals[j] = v
            return vals, vals

        def _forming_hi(idx: np.ndarray) -> np.ndarray:
            """High of the forming daily bar through T (session open + bars ended <= T), NaN -> the price now."""
            out = np.empty(idx.size)
            for j, i in enumerate(idx):
                v = hmemo.get(i)
                if v is None:
                    h = ps.screener_session_high(str(syms[i]), as_of_dt)
                    v = float("nan") if h is None else float(h)
                    hmemo[i] = v
                out[j] = v if np.isfinite(v) else memo.get(i, float("nan"))
            return out

        vol = None
        if not self.beh.volume_is_average:
            def vol(idx):                                   # pre-fix validation mode only
                return np.array([ps.volume_so_far(str(syms[i]), as_of_dt) or 0.0 for i in idx])
        res = panel.select(day, self.settings, self.beh, now=_now, vol_today=vol, valid=self.valid,
                           forming_hi=_forming_hi if self.intraday else None)
        self._cache[key] = res
        return res
