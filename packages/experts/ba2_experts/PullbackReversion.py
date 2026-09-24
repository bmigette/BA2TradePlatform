"""Short-horizon pullback reversion: buy an oversold dip in an uptrend (long), or sell an
overbought rally in a downtrend (short). Research-only.

``pullback_signal`` is the pure, causal decision for the LAST bar of a completed daily history.
Its semantics mirror the feasibility probe ``test_files/pullback_feasibility_20260924.py``
(``rsi``, ``entry_ok``, ``exit_signal``); the market-condition gates read the platform's own
calculators over the 128-bar window ending at that bar, exactly as the live gate would.
Missing, short or corrupt inputs raise ``ValueError``: the function never answers "none" for a
bar it could not evaluate.

The core runs on numpy arrays and Python floats: it is called per symbol per bar in the GA.

``PullbackReversion`` wraps it as an expert registered in ba2_experts and the daily backtest
handler only. Like ETFTrend it is NOT in the live registry. It never submits orders: entry and
exit become BUY/SELL recommendations that the ordinary rules and RM act on.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from numbers import Real
from typing import Mapping, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ba2_common.core.db import add_instance, update_instance
from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.market_conditions import (
    STATUS_INVALID_PRICES, STATUS_VALID, STRUCTURE_STATE_CODES, STRUCTURE_STATE_NONE_CODE, WINDOW,
    compute_chart_structure, compute_market_conditions)
from ba2_common.core.models import ExpertRecommendation
from ba2_common.core.types import (
    MarketAnalysisStatus, OrderRecommendation, Recommendation, RiskLevel, TimeHorizon)
from ba2_common.logger import get_expert_logger

DIRECTIONS = ("long", "short")
TREND_GATES = ("sma200", "slope_ohlcv_v1", "sma200_and_spy")
EXIT_MODES = ("sma5", "rsi", "sma5_or_choch", "time")
SMA_TREND = 200
SMA_EXIT = 5
#: Bars the function needs: SMA200 always; the 128-bar calculator window fits inside it.
MIN_BARS = max(SMA_TREND, WINDOW)
_OHLCV = ("Open", "High", "Low", "Close", "Volume")
_STATE_NAMES = {float(code): name for name, code in STRUCTURE_STATE_CODES.items()}
_STATE_NAMES[STRUCTURE_STATE_NONE_CODE] = "none"


def _choice(settings: Mapping, key: str, options) -> str:
    if key not in settings:
        raise ValueError(f"Missing setting {key!r}")
    value = settings[key]
    if value not in options:
        raise ValueError(f"Setting {key!r} must be one of {options}, got {value!r}")
    return value


def _number(settings: Mapping, key: str, lo: float, hi: float, integer: bool = False):
    if key not in settings:
        raise ValueError(f"Missing setting {key!r}")
    value = settings[key]
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ValueError(f"Setting {key!r} must be a finite number, got {value!r}")
    if integer:
        if float(value) != int(value):
            raise ValueError(f"Setting {key!r} must be an integer, got {value!r}")
        value = int(value)
    else:
        value = float(value)
    if not lo <= value <= hi:
        raise ValueError(f"Setting {key!r} must be in [{lo}, {hi}], got {value!r}")
    return value


def _closes(bars, name: str) -> np.ndarray:
    """The validated close array of a completed daily history (DatetimeIndex, strictly
    ascending sessions, every close finite and positive)."""
    if not isinstance(bars, pd.DataFrame):
        raise ValueError(f"{name} must be a DataFrame, got {type(bars).__name__}")
    missing = [k for k in _OHLCV if k not in bars.columns]
    if missing:
        raise ValueError(f"{name} is missing columns {missing}")
    if not isinstance(bars.index, pd.DatetimeIndex):
        # A provider frame carries a Date column on a RangeIndex: ordering and the SPY session
        # check would then compare row positions and pass silently.
        raise ValueError(f"{name} must be indexed by session date (DatetimeIndex), "
                         f"got {type(bars.index).__name__}")
    if len(bars) < MIN_BARS:
        raise ValueError(f"Insufficient history in {name}: {len(bars)} bars, need {MIN_BARS}")
    if not (bars.index.is_monotonic_increasing and bars.index.is_unique):
        raise ValueError(f"{name} index must be strictly ascending by session")
    try:
        close = np.asarray(bars["Close"], dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Non-numeric close in {name}: {exc}") from exc
    bad = ~(np.isfinite(close) & (close > 0))
    if bad.any():
        i = int(np.flatnonzero(bad)[-1])
        raise ValueError(f"Invalid close in {name} at {bars.index[i]}: {close[i]!r}")
    return close


def _wilder_rsi(close: np.ndarray, period: int) -> float:
    """Wilder RSI of the last bar: the recursion of the probe's ``rsi()``, i.e. pandas
    ``ewm(alpha=1/n, adjust=False)`` seeded on the first close-to-close change.

    With no down move in the smoothed history the RSI is 100. The probe yields NaN there and
    never acts on that bar; here a long in ``rsi`` exit mode exits. The short-entry RSI
    condition also holds, but such a history closes above its SMA200 with a positive slope, so
    no short trend gate passes. With no move at all the RSI is undefined and raises."""
    alpha = 1.0 / period
    keep = 1.0 - alpha
    values = close.tolist()
    change = values[1] - values[0]
    up = change if change > 0 else 0.0
    down = -change if change < 0 else 0.0
    prev = values[1]
    for price in values[2:]:
        change = price - prev
        prev = price
        up = keep * up + alpha * (change if change > 0 else 0.0)
        down = keep * down + alpha * (-change if change < 0 else 0.0)
    if down == 0:
        if up == 0:
            raise ValueError("RSI undefined: no price change in the history")
        return 100.0
    return 100.0 - 100.0 / (1.0 + up / down)


def _window(bars: pd.DataFrame, close: np.ndarray):
    """The last ``WINDOW`` bars as the five float arrays the calculators take."""
    tail = bars.iloc[-WINDOW:]
    return (tail["Open"].to_numpy(dtype=float), tail["High"].to_numpy(dtype=float),
            tail["Low"].to_numpy(dtype=float), close[-WINDOW:],
            tail["Volume"].to_numpy(dtype=float))


def _measured(obs, what: str):
    """``obs.value`` when valid; ``None`` when the calculator could not measure it; a
    ``ValueError`` when it found corrupt bars (that is bad data, not an absent signal)."""
    if obs.status == STATUS_VALID:
        return obs.value
    if obs.status == STATUS_INVALID_PRICES:
        raise ValueError(f"Corrupt bars in the {WINDOW}-bar window for {what}: {obs.reason}")
    return None


def _same_session(bars: pd.DataFrame, spy_bars: pd.DataFrame) -> None:
    tz, spy_tz = bars.index.tz, spy_bars.index.tz
    if str(tz) != str(spy_tz):
        raise ValueError(f"Timezone mismatch: bars index is {tz or 'tz-naive'}, "
                         f"spy_bars index is {spy_tz or 'tz-naive'}")
    if spy_bars.index[-1] != bars.index[-1]:
        raise ValueError(f"spy_bars end on {spy_bars.index[-1]}, bars on {bars.index[-1]}: "
                         "the market regime must be read on the decision session")


def pullback_signal(bars: pd.DataFrame, settings: Mapping,
                    spy_bars: Optional[pd.DataFrame] = None) -> dict:
    """Entry / exit / none for the last bar of ``bars`` (completed daily OHLCV on a
    DatetimeIndex, ascending).

    ``spy_bars`` is required by ``trend_gate == "sma200_and_spy"`` only; it must end on the same
    session as ``bars``. Entry beats exit on the same bar; ``exit_mode == "time"`` never signals
    (the max-hold rule owns that exit). In ``sma5_or_choch`` mode ``structure_state`` is always
    reported ('bull'/'bear'/'none', or None when unmeasurable); otherwise it is None."""
    direction = _choice(settings, "direction", DIRECTIONS)
    gate = _choice(settings, "trend_gate", TREND_GATES)
    period = _number(settings, "rsi_period", 2, 5, integer=True)
    threshold = _number(settings, "entry_threshold", 1.0, 30.0)
    exit_mode = _choice(settings, "exit_mode", EXIT_MODES)
    rsi_exit = _number(settings, "rsi_exit", 50.0, 90.0)
    long = direction == "long"

    close = _closes(bars, "bars")
    last = float(close[-1])
    sma200 = float(close[-SMA_TREND:].mean())
    sma5 = float(close[-SMA_EXIT:].mean())
    rsi = _wilder_rsi(close, period)
    window = None
    notes = []

    if gate == "slope_ohlcv_v1":
        window = _window(bars, close)
        slope = _measured(compute_market_conditions(*window).trend_slope, "the trend slope")
        if slope is None:
            trend_ok = False
            notes.append("trend slope unmeasurable")
        else:
            trend_ok = slope > 0 if long else slope < 0
            notes.append(f"trend slope {slope:+.3f}")
    else:
        trend_ok = last > sma200 if long else last < sma200
        notes.append(f"close {last:.4f} vs SMA200 {sma200:.4f}")
        if gate == "sma200_and_spy":
            if spy_bars is None:
                raise ValueError("trend_gate sma200_and_spy needs spy_bars")
            spy_close = _closes(spy_bars, "spy_bars")
            _same_session(bars, spy_bars)
            spy_last = float(spy_close[-1])
            spy_sma200 = float(spy_close[-SMA_TREND:].mean())
            spy_ok = spy_last > spy_sma200 if long else spy_last < spy_sma200
            trend_ok = trend_ok and spy_ok
            notes.append(f"SPY {spy_last:.4f} vs SMA200 {spy_sma200:.4f}")
    notes.append(f"trend {'ok' if trend_ok else 'not ok'} for {direction}")
    notes.append(f"RSI{period} {rsi:.2f}")

    structure_state = None
    if exit_mode == "sma5_or_choch":
        if window is None:
            window = _window(bars, close)
        code = _measured(compute_chart_structure(*window).structure_state, "the swing structure")
        if code is not None:
            if code not in _STATE_NAMES:
                raise ValueError(f"Unknown structure_state code {code!r}")
            structure_state = _STATE_NAMES[code]
        notes.append(f"structure {structure_state or 'unmeasurable'}")

    extreme = rsi < threshold if long else rsi > 100.0 - threshold
    if trend_ok and extreme:
        action = "entry"
    else:
        beyond_sma5 = last > sma5 if long else last < sma5
        if exit_mode == "sma5":
            exiting = beyond_sma5
            notes.append(f"close vs SMA5 {sma5:.4f}")
        elif exit_mode == "rsi":
            exiting = rsi > rsi_exit if long else rsi < 100.0 - rsi_exit
            notes.append(f"RSI exit level {rsi_exit:g}")
        elif exit_mode == "sma5_or_choch":
            exiting = beyond_sma5 or structure_state == ("bear" if long else "bull")
            notes.append(f"close vs SMA5 {sma5:.4f}")
        else:  # time: the max-hold rule owns the exit
            exiting = False
        action = "exit" if exiting else "none"

    return {"action": action, "rsi": rsi, "sma200": sma200, "sma5": sma5,
            "trend_ok": bool(trend_ok), "structure_state": structure_state,
            "reason": f"{action}: " + "; ".join(notes)}


# ---------------------------------------------------------------------------
# Expert
# ---------------------------------------------------------------------------
#: Calendar days of daily bars each decision reads: about 289 sessions, enough for SMA200 and
#: the 128-bar calculator window with holiday slack. Both paths trim the frame to this window,
#: so live (which fetches exactly this) and the backtest (whose memoised provider can return
#: more) decide on the same bars.
LOOKBACK_DAYS = 420
#: A completed history whose last session is older than this is stale (ETFTrend's bound).
MAX_STALE_DAYS = 7
#: The market benchmark read by ``trend_gate == "sma200_and_spy"``, and the reference that
#: tells a recent listing from missing data.
SPY_SYMBOL = "SPY"
#: A history whose first row lies within this many days of the lookback window's start covers
#: the window (weekends, holidays): it was not listed inside it.
LISTING_SLACK_DAYS = MAX_STALE_DAYS


def _decision_date(as_of: datetime):
    """The New York calendar date of the decision (ETFTrend's reading of ``as_of``)."""
    local = as_of.astimezone(ZoneInfo("America/New_York")) if as_of.tzinfo else as_of
    return local.date()


def _session_stamps(frame, name: str) -> np.ndarray:
    """The provider frame's ``Date`` column as naive UTC datetime64 instants.

    ETFTrend reads the dates as ``pd.to_datetime(Date, utc=True)``: tz-aware stamps become UTC
    instants, naive ones are taken as UTC. A datetime64 column's ``.values`` is exactly those
    UTC instants, so the conversion is only paid for object/string columns (this runs per
    symbol per bar in the GA; the pandas path cost ~1 ms a call). A missing date is corrupt
    data: it would otherwise drop out of every comparison silently."""
    column = frame["Date"]
    stamps = (column.values if column.dtype.kind == "M"
              else pd.to_datetime(column, utc=True).values)
    if np.isnat(stamps).any():
        raise ValueError(f"Missing session date in the OHLCV history of {name}")
    return stamps


def completed_bars(frame, as_of: datetime, name: str) -> pd.DataFrame:
    """Provider frame (``Date`` column on a RangeIndex) -> the completed daily sessions of the
    last ``LOOKBACK_DAYS`` before ``as_of``, on an ascending UTC DatetimeIndex.

    The session exclusion is ETFTrend's: daily candles are date-labelled, so even at 09:30 the
    decision day's own candle (still forming live, already final in a backtest cache) must never
    enter the decision; only earlier dates (New York calendar date of ``as_of``) are kept.
    Missing, empty or stale histories raise ``ValueError``."""
    if frame is None or len(frame) == 0:
        raise ValueError(f"Missing OHLCV history: {name}")
    missing = [k for k in ("Date",) + _OHLCV if k not in frame.columns]
    if missing:
        raise ValueError(f"OHLCV history for {name} is missing columns {missing}")
    today = _decision_date(as_of)
    stamps = _session_stamps(frame, name)
    # For UTC instants, ``ts.date() < today`` is exactly ``ts < today 00:00 UTC``.
    end = np.datetime64(today, "D")
    keep = (stamps < end) & (stamps >= end - np.timedelta64(LOOKBACK_DAYS, "D"))
    if not keep.any():
        raise ValueError(f"No completed sessions for {name} before {today}")
    index = pd.DatetimeIndex(stamps[keep], name="Date").tz_localize("UTC")
    bars = pd.DataFrame({k: frame[k].to_numpy()[keep] for k in _OHLCV}, index=index)
    if not index.is_monotonic_increasing:
        bars = bars.sort_index(kind="stable")
    last = bars.index[-1].date()
    if (today - last).days > MAX_STALE_DAYS:
        raise ValueError(f"Stale OHLCV history for {name}: last completed session {last}, "
                         f"analysis date {today}")
    return bars


def _check_recent_listing(frame, bars, as_of: datetime, name: str, reference_bars) -> None:
    """Return quietly only when a short completed history (under ``MIN_BARS``) is a genuine
    recent listing, which the expert skips; raise ``ValueError`` (the run aborts) otherwise.

    A recent listing is a valid history whose EARLIEST row, before any cut, lies inside the
    lookback window: a history reaching back to the window start but still short has a gap.
    "Inside" is only meaningful if the provider serves the whole window, which a backtest does
    not when its warmup is shorter than ``LOOKBACK_DAYS``: every symbol would then look newly
    listed and the run would skip silently. ``reference_bars()`` returns SPY's completed bars
    (fetched only here, off the hot path); SPY always has history, so a short SPY, or one that
    also starts inside the window, is a data problem and aborts."""
    today = _decision_date(as_of)
    window_start = today - timedelta(days=LOOKBACK_DAYS)
    covered_by = window_start + timedelta(days=LISTING_SLACK_DAYS)
    earliest = pd.Timestamp(_session_stamps(frame, name).min()).date()
    if earliest <= covered_by:
        raise ValueError(
            f"Insufficient history in {name}: {len(bars)} completed bars in the {LOOKBACK_DAYS}-day "
            f"window, need {MIN_BARS}, although its history starts on {earliest}: a gap in the "
            "data, not a new listing")
    try:
        close = bars["Close"].to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Non-numeric close in {name}: {exc}") from exc
    if not (np.isfinite(close) & (close > 0)).all() or not bars.index.is_unique:
        raise ValueError(f"Corrupt OHLCV history for {name}: invalid close or duplicate session")
    reference = reference_bars()
    if len(reference) < MIN_BARS:
        raise ValueError(f"Insufficient history in {SPY_SYMBOL}: {len(reference)} completed bars, "
                         f"need {MIN_BARS}; the benchmark always has history, so this is a data "
                         "problem")
    served_from = reference.index[0].date()
    if served_from > covered_by:
        raise ValueError(
            f"The OHLCV history served for {SPY_SYMBOL} starts on {served_from}, inside the "
            f"{LOOKBACK_DAYS}-day window from {window_start}: the provider does not cover the "
            f"window (backtest warmup too short?), so {name} starting on {earliest} cannot be "
            "told apart from missing data")


class PullbackReversion(MarketExpertInterface):
    """Backtest-registered (research) expert over ``pullback_signal``.

    Recommendation mapping (``expected_profit_percent`` is always 0.0: no price target):
    entry -> BUY (long) / SELL (short), confidence 50..100 by how far RSI is past the entry
    threshold; exit -> SELL (long) / BUY (short), confidence 100; none -> HOLD.
    A long expert's SELL is an EXIT signal: it only acts through a ruleset that closes an open
    position, never by itself opening a short."""

    @classmethod
    def description(cls):
        return ("Short-horizon RSI pullback reversion inside the prevailing trend "
                "(long dips in uptrends or short rallies in downtrends)")

    @classmethod
    def get_settings_definitions(cls):
        return {
            "direction": {"type": "str", "required": True, "default": "long",
                          "valid_values": list(DIRECTIONS),
                          "description": "long buys oversold dips in an uptrend; short sells "
                                         "overbought rallies in a downtrend"},
            "trend_gate": {"type": "str", "required": True, "default": "sma200",
                           "valid_values": list(TREND_GATES),
                           "description": "Trend filter: close vs SMA200, the ohlcv-v1 trend "
                                          "slope sign, or SMA200 plus SPY vs its own SMA200"},
            "rsi_period": {"type": "int", "required": True, "default": 2,
                           "description": "Wilder RSI period in bars (2-5)"},
            "entry_threshold": {"type": "float", "required": True, "default": 5.0,
                                "description": "Entry when RSI < threshold (long) or "
                                               "> 100 - threshold (short); 1-30"},
            "exit_mode": {"type": "str", "required": True, "default": "sma5",
                          "valid_values": list(EXIT_MODES),
                          "description": "Exit signal: close beyond SMA5, RSI beyond rsi_exit, "
                                         "SMA5 or a swing-structure CHoCH, or time (the "
                                         "max-hold rule owns the exit)"},
            "rsi_exit": {"type": "float", "required": True, "default": 70.0,
                         "description": "RSI exit level for exit_mode=rsi: long exits above it, "
                                        "short below 100 - rsi_exit; 50-90"},
        }

    _SETTING_KEYS = ("direction", "trend_gate", "rsi_period", "entry_threshold",
                     "exit_mode", "rsi_exit")

    def __init__(self, id):
        super().__init__(id)
        self._load_expert_instance(id)
        self.logger = get_expert_logger("PullbackReversion", id)

    def _analyze(self, symbol, providers, settings, as_of):
        provider = providers.ohlcv()

        def fetch(name):
            return provider.get_ohlcv_data(name, end_date=as_of, lookback_days=LOOKBACK_DAYS,
                                           interval="1d")

        def history(name):
            return completed_bars(fetch(name), as_of, name)

        frame = fetch(symbol)
        bars = completed_bars(frame, as_of, symbol)
        if len(bars) < MIN_BARS:
            _check_recent_listing(frame, bars, as_of, symbol, lambda: history(SPY_SYMBOL))
            # The recent listing is skipped exactly as DeterministicScorer skips a thin history.
            return Recommendation(
                signal=OrderRecommendation.HOLD, confidence=0.0,
                current_price=float(bars["Close"].iloc[-1]),
                details=f"Insufficient OHLCV history ({len(bars)} < {MIN_BARS}): {symbol} listed "
                        f"on {bars.index[0].date()}, inside the {LOOKBACK_DAYS}-day lookback",
                expected_profit_percent=0.0, skip=True,
                skip_reason="insufficient_history")
        spy = history(SPY_SYMBOL) if settings["trend_gate"] == "sma200_and_spy" else None
        result = pullback_signal(bars, settings, spy)  # validates every setting it reads
        long = settings["direction"] == "long"
        action = result["action"]
        if action == "entry":
            threshold = float(settings["entry_threshold"])
            depth = ((threshold - result["rsi"]) if long
                     else (result["rsi"] - (100.0 - threshold))) / threshold
            signal = OrderRecommendation.BUY if long else OrderRecommendation.SELL
            confidence = 50.0 + 50.0 * min(max(depth, 0.0), 1.0)
        elif action == "exit":
            signal = OrderRecommendation.SELL if long else OrderRecommendation.BUY
            confidence = 100.0
        elif action == "none":
            signal, confidence = OrderRecommendation.HOLD, 50.0
        else:
            raise ValueError(f"Unknown pullback_signal action {action!r}")
        session = bars.index[-1].date().isoformat()
        return Recommendation(
            signal=signal, confidence=confidence, current_price=float(bars["Close"].iloc[-1]),
            expected_profit_percent=0.0,  # no price target or return forecast
            details=f"{symbol} {settings['direction']} pullback on session {session}: "
                    f"{result['reason']}. Confidence denotes a satisfied rule, not a "
                    "probability of profit.",
            raw_outputs={**result, "session": session})

    def analyze_as_of(self, as_of, context):
        # The daily engine's entry pass sets _gather_symbol; its management pass
        # and the newer context callers also carry extra['symbol'].
        symbol = context.extra["symbol"] if "symbol" in context.extra else self._gather_symbol
        try:
            return self._analyze(symbol, context.providers, context.settings, as_of)
        except (ValueError, KeyError) as exc:
            # The engine logs and skips an ordinary ValueError per symbol; missing, short or
            # corrupt history must instead abort the run (as ETFTrend's basket does), never
            # turn into a plausible-looking backtest that silently never evaluated the symbol.
            from ba2_providers.fmp_common import FMPHistoryCacheMiss
            raise FMPHistoryCacheMiss(f"PullbackReversion cannot evaluate {symbol}: {exc}") from exc

    def run_analysis(self, symbol, market_analysis):
        try:
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            rec = self._analyze(symbol, self._live_providers(),
                                self._resolve_settings(self._SETTING_KEYS), datetime.now(timezone.utc))
            if rec.skip:  # DeterministicScorer's live skip: no recommendation row
                market_analysis.state = {"skipped": True, "skip_reason": rec.skip_reason,
                                         "skip_message": rec.details}
                market_analysis.status = MarketAnalysisStatus.SKIPPED
                update_instance(market_analysis)
                return
            add_instance(ExpertRecommendation(
                instance_id=self.id, symbol=symbol, market_analysis_id=market_analysis.id,
                recommended_action=rec.signal, expected_profit_percent=0.0,
                price_at_date=rec.current_price, confidence=rec.confidence, details=rec.details,
                risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.SHORT_TERM,
                subtype=market_analysis.subtype, data={"PullbackReversion": rec.raw_outputs}))
            market_analysis.state = {"PullbackReversion": rec.raw_outputs}
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
        except Exception as exc:
            self.logger.error("PullbackReversion analysis failed for %s: %s", symbol, exc,
                              exc_info=True)
            market_analysis.status = MarketAnalysisStatus.FAILED
            market_analysis.state = {"error": str(exc)}
            update_instance(market_analysis)

    def render_market_analysis(self, market_analysis):
        return json.dumps(market_analysis.state, indent=2)
