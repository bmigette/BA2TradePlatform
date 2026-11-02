"""SPY (benchmark) regime for the option entry gates ``spy-trend`` and ``spy-vol``.

Two CATEGORICAL fields, each gated by ONE set-membership leaf (``field == mode code``) whose mode
the GA searches, ``off`` included:

    spy_trend_regime   up | pullback | bear_rally | down   (+ sets above_200, below_200)
    spy_vol_regime     calm | normal | stressed            (+ sets not_stressed, not_calm)

THE MODE/CODE TABLES ARE A PERSISTED CONTRACT. The GA choice-gene index follows code order, so a
stored genome only decodes to the same mode while the order is unchanged: never reorder, only append.

ONE RULE, TWO CALLERS (backtest == live):

* LIVE  -> :func:`classify_benchmark_regime` on the closes of FINISHED sessions
           (:func:`knowable_closes` drops a partial TODAY bar).
* BACKTEST -> :class:`BenchmarkRegimeCalendar`, the same arithmetic precomputed once for a whole
           run; ``tests/test_benchmark_regime.py`` asserts it equals the pure function for EVERY day.

The host publishes the regime once per bar through the process-global seam below (same rationale as
``regime_overlay``: the value is market-wide, and each GA trial has its own process). The overlay's
``set_stressed`` / ``StressedCalendar`` are deliberately left untouched so overlay results cannot move.
An unknown regime (too little history, no SPY data, nothing published) NEVER passes a gate, and says
why -- the condition reads :func:`get_benchmark_regime_failure` and the ``*_reason`` fields.
"""
import bisect
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, List, Optional, Sequence, Tuple

from ba2_common.core import market_regime as mr
from ba2_common.core.regime_overlay import _as_date

BENCHMARK_SYMBOL = "SPY"
TREND_FIELD = "spy_trend_regime"
VOL_FIELD = "spy_vol_regime"

TREND_MODE_CODES = OrderedDict([("up", 1), ("pullback", 2), ("bear_rally", 3), ("down", 4),
                                ("above_200", 5), ("below_200", 6)])
VOL_MODE_CODES = OrderedDict([("calm", 1), ("normal", 2), ("stressed", 3),
                              ("not_stressed", 4), ("not_calm", 5)])
_MODE_CODES = {TREND_FIELD: TREND_MODE_CODES, VOL_FIELD: VOL_MODE_CODES}
#: field -> mode table. The IDENTITY modes are named like the state they accept, so this also maps a state to its code.
CODES_BY_FIELD = _MODE_CODES

#: mode -> the states it accepts. Identity modes accept exactly their own state.
MODE_STATES = {
    TREND_FIELD: {
        "up": {mr.TREND_UP}, "pullback": {mr.TREND_PULLBACK},
        "bear_rally": {mr.TREND_BEAR_RALLY}, "down": {mr.TREND_DOWN},
        "above_200": {mr.TREND_UP, mr.TREND_PULLBACK},
        "below_200": {mr.TREND_BEAR_RALLY, mr.TREND_DOWN},
    },
    VOL_FIELD: {
        "calm": {mr.CALM}, "normal": {mr.NORMAL}, "stressed": {mr.STRESSED},
        "not_stressed": {mr.CALM, mr.NORMAL},
        "not_calm": {mr.NORMAL, mr.STRESSED},
    },
}


def mode_for_code(field: str, code: float) -> str:
    """The mode name a numeric leaf value stands for. ValueError on an unknown field or code."""
    codes = _MODE_CODES.get(field)
    if codes is None:
        raise ValueError(f"unknown benchmark regime field {field!r}; have {sorted(_MODE_CODES)}")
    for mode, c in codes.items():
        if c == code:
            return mode
    raise ValueError(f"{field}: unknown mode code {code!r}; have {dict(codes)}")


def state_matches(field: str, code: float, state: Optional[str]) -> bool:
    """True when ``state`` is accepted by the mode ``code`` names. An unknown state (None) matches
    NOTHING; the caller decides which reason to report."""
    mode = mode_for_code(field, code)
    if state is None:
        return False
    return state in MODE_STATES[field][mode]


@dataclass(frozen=True)
class BenchmarkRegime:
    """The benchmark's regime at one decision. ``None`` axis = unknown, with its reason."""
    trend: Optional[str]
    vol: Optional[str]
    trend_reason: str = ""
    vol_reason: str = ""
    session: Optional[date] = None


def classify_benchmark_regime(closes: Sequence[Optional[float]], session: Optional[date] = None
                              ) -> BenchmarkRegime:
    """PURE: both axes from ``closes`` (oldest first, already cut at the knowable session).

    THIS IS THE LIVE PATH, and the oracle the backtest calendar is asserted against.
    """
    vals = list(closes)
    trend = mr.classify_trend_state(vals)
    vol = mr.classify_volatility_regime(vals)
    return BenchmarkRegime(trend["state"], vol["regime"], trend["reason"], vol["reason"], session)


def knowable_closes(dates: Sequence[date], closes: Sequence[Optional[float]], now_utc: datetime
                    ) -> Tuple[List[date], List[Optional[float]]]:
    """Drop every row whose session has not FINISHED at ``now_utc``.

    A provider asked up to "now" can return a partial TODAY bar after the open; its "close" is a
    live print, not a close. A session counts only once its regular close (16:00 ET, 13:00 on a
    half day) is at or before ``now_utc``. Raises ValueError on non-increasing dates.
    """
    from ba2_common.core.market_calendar import regular_session_close_utc

    dates = [_as_date(d) for d in dates]
    if len(dates) != len(closes):
        raise ValueError(f"dates ({len(dates)}) and closes ({len(closes)}) differ in length")
    for a, b in zip(dates, dates[1:]):
        if b <= a:
            raise ValueError(f"benchmark dates must be strictly increasing ({a} then {b})")
    keep_d: List[date] = []
    keep_c: List[Optional[float]] = []
    for d, c in zip(dates, closes):
        if regular_session_close_utc(d) <= now_utc:
            keep_d.append(d)
            keep_c.append(c)
    return keep_d, keep_c


class BenchmarkRegimeCalendar:
    """``bar date -> BenchmarkRegime``, precomputed once for a whole backtest run.

    Same reasons and same guarantees as ``regime_overlay.StressedCalendar`` (see there): day *i* is
    classified from ``closes[:i+1]`` only, and ``at`` uses the same bisect-on-date rule. The
    arithmetic mirrors ``market_regime`` slice for slice (sum over the same elements in the same
    order), so the result is bit-identical to :func:`classify_benchmark_regime`.
    """

    __slots__ = ("_dates", "_trend", "_vol")

    def __init__(self, dates: Sequence[Any], closes: Sequence[Optional[float]]):
        vals: List[float] = []
        keys: List[date] = []
        for d, c in zip(dates, closes):
            if c is None:
                continue                      # mirrors the classifiers' own None filtering
            vals.append(float(c))
            keys.append(_as_date(d))
        self._dates = keys
        self._trend = self._build_trend(vals)
        self._vol = self._build_vol(vals)

    @staticmethod
    def _build_trend(vals: List[float]) -> List[Optional[str]]:
        sma_n, slope_n, lag = mr._TREND_SMA_PERIOD, mr._SLOPE_SMA, mr._SLOPE_LAG
        need = max(sma_n, slope_n + lag)
        out: List[Optional[str]] = []
        for i in range(len(vals)):
            if i + 1 < need:
                out.append(None)
                continue
            sma = sum(vals[i + 1 - sma_n:i + 1]) / sma_n
            if not sma or sma <= 0:
                out.append(None)
                continue
            now50 = sum(vals[i + 1 - slope_n:i + 1]) / slope_n
            lag50 = sum(vals[i + 1 - lag - slope_n:i + 1 - lag]) / slope_n
            rising = (now50 - lag50) > 0
            if vals[i] > sma:
                out.append(mr.TREND_UP if rising else mr.TREND_PULLBACK)
            else:
                out.append(mr.TREND_BEAR_RALLY if rising else mr.TREND_DOWN)
        return out

    @staticmethod
    def _build_vol(vals: List[float]) -> List[Optional[str]]:
        window, lookback = mr._VOL_WINDOW, mr._VOL_RANK_LOOKBACK
        # rv[i] = realized vol of the window ENDING at day i, i.e. mr._realized_vol(vals[:i+1], window).
        rv: List[Optional[float]] = [mr._realized_vol(vals[:i + 1], window) for i in range(len(vals))]
        out: List[Optional[str]] = []
        for i in range(len(vals)):
            if i + 1 < window + lookback:
                out.append(None)
                continue
            series = [v for v in rv[i - lookback:i + 1] if v is not None]
            if len(series) < 2:
                out.append(None)
                continue
            current = series[-1]
            if max(series) - min(series) <= 0:
                out.append(mr.NORMAL)         # no dispersion: rank undefined -> no evidence of stress
                continue
            rank = sum(1 for v in series if v <= current) / len(series) * 100.0
            if rank < mr._CALM_PCTL:
                out.append(mr.CALM)
            elif rank > mr._STRESSED_PCTL:
                out.append(mr.STRESSED)
            else:
                out.append(mr.NORMAL)
        return out

    def at(self, as_of: Any) -> BenchmarkRegime:
        """The regime of the most recent benchmark day at or before ``as_of``; both axes None (with
        a reason) when ``as_of`` precedes every benchmark day."""
        key = _as_date(as_of)
        idx = bisect.bisect_right(self._dates, key) - 1
        if idx < 0:
            why = (f"no benchmark bar at or before {key}" if self._dates
                   else "no benchmark bars")
            return BenchmarkRegime(None, None, why, why, None)
        trend, vol = self._trend[idx], self._vol[idx]
        n = idx + 1
        t_why = "" if trend is not None else (
            f"insufficient history ({n} bars, need {max(mr._TREND_SMA_PERIOD, mr._SLOPE_SMA + mr._SLOPE_LAG)})")
        v_why = "" if vol is not None else (
            f"insufficient history ({n} bars, need {mr._VOL_WINDOW + mr._VOL_RANK_LOOKBACK})")
        return BenchmarkRegime(trend, vol, t_why, v_why, self._dates[idx])


# ---------------------------------------------------------------------------------------------
# Per-bar publish/read seam. Default None = "nothing published" -> every gate fails closed.
# ---------------------------------------------------------------------------------------------
_REGIME_NOW: Optional[BenchmarkRegime] = None
_FAILURE_NOW: Optional[str] = None


def set_benchmark_regime(regime: Optional[BenchmarkRegime]) -> None:
    """Publish this bar's regime (``None`` = nothing known)."""
    global _REGIME_NOW
    _REGIME_NOW = regime


def get_benchmark_regime() -> Optional[BenchmarkRegime]:
    """This bar's published regime, or None when nothing has been published."""
    return _REGIME_NOW


def set_benchmark_regime_failure(reason: Optional[str]) -> None:
    """Live: record why the regime could not be computed (fetch failure), so a gate can say why."""
    global _FAILURE_NOW
    _FAILURE_NOW = reason


def get_benchmark_regime_failure() -> Optional[str]:
    return _FAILURE_NOW


def reset_benchmark_regime() -> None:
    """Clear the regime AND the failure. Call between runs so one trial cannot inherit another's bar."""
    global _REGIME_NOW, _FAILURE_NOW
    _REGIME_NOW = None
    _FAILURE_NOW = None


# ---------------------------------------------------------------------------------------------
# Deploy: what a gated run tells the importer about its SPY series
# ---------------------------------------------------------------------------------------------
#: The history source LIVE reads SPY from (owner decision 2026-10-24: the same FMP source as the
#: stock prices). A backtest on another source can classify a few days differently; see
#: ``test_files/probe_spy_regime_source_disagreement.py``.
LIVE_BENCHMARK_HISTORY_SOURCE = "fmp"


def _has_benchmark_leaf(node: Any) -> bool:
    if isinstance(node, dict):
        if node.get("field") in (TREND_FIELD, VOL_FIELD):
            return True
        return any(_has_benchmark_leaf(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_has_benchmark_leaf(v) for v in node)
    return False


def benchmark_deploy_block(backtest_block: Any) -> Optional[dict]:
    """The ``benchmark_regime`` block a deploy payload carries: ``{"symbol", "history_source"}`` when the
    run's backtest block says it was gated, else None (the payload then has no such key at all)."""
    from ba2_common.core.ohlcv_history import resolve_job_history_source

    if not isinstance(backtest_block, dict) or not backtest_block.get("benchmark_regime_gates"):
        return None
    return {"symbol": BENCHMARK_SYMBOL, "history_source": resolve_job_history_source(backtest_block)}


def benchmark_import_warnings(block: Any, entry_rules: Any, exit_rules: Any) -> List[str]:
    """Warnings (NOT refusals) for a payload whose rules carry a SPY regime leaf: the source its
    backtest read SPY from differs from the one live reads, or is unknown. Empty when the rules carry
    no such leaf. A different source can flip the SMA200 / vol-rank state on a few days."""
    if not (_has_benchmark_leaf(entry_rules) or _has_benchmark_leaf(exit_rules)):
        return []
    if not isinstance(block, dict) or not block.get("history_source"):
        return [f"SPY regime gate deployed but the payload does not say which history source its "
                f"backtest read {BENCHMARK_SYMBOL} from (unknown); live reads "
                f"{LIVE_BENCHMARK_HISTORY_SOURCE.upper()}"]
    source = str(block["history_source"])
    if source == LIVE_BENCHMARK_HISTORY_SOURCE:
        return []
    return [f"SPY regime gate: the backtest read {BENCHMARK_SYMBOL} from {source!r} but live reads "
            f"{LIVE_BENCHMARK_HISTORY_SOURCE!r}; the SMA200 / vol-rank state can differ on a few days"]
