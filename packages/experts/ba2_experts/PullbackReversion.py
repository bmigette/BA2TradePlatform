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

from datetime import datetime, time, timedelta, timezone
import json
import math
from numbers import Real
from typing import Mapping, Optional

import numpy as np
import pandas as pd

from ba2_common.core.db import add_instance, update_instance
from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.market_calendar import (
    MarketCalendarUnavailable, backtest_decision_label, decision_data_session, live_decision_label)
from ba2_common.core.market_conditions import (
    STATUS_INVALID_PRICES, STATUS_VALID, STRUCTURE_STATE_CODES, STRUCTURE_STATE_NONE_CODE, WINDOW,
    compute_chart_structure, compute_market_conditions)
from ba2_common.core.models import ExpertRecommendation
from ba2_common.core.types import (
    AnalysisUseCase, MarketAnalysisStatus, OrderRecommendation, Recommendation, RiskLevel,
    TimeHorizon)
from ba2_common.logger import get_expert_logger, logger

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
#: Calendar days of daily bars each decision reads, ending on its data session: about 289
#: sessions, enough for SMA200 and the 128-bar calculator window with holiday slack. Both paths
#: trim the frame to this window, so a provider serving more history decides identically.
LOOKBACK_DAYS = 420
#: A symbol whose last completed session is more than this many days before the data session
#: is stale (ETFTrend's bound).
MAX_STALE_DAYS = 7
#: The market benchmark read by ``trend_gate == "sma200_and_spy"``, and the reference that
#: tells a recent listing or a stopped symbol from missing data.
SPY_SYMBOL = "SPY"
#: A history whose first row lies within this many days of the lookback window's start covers
#: the window (weekends, holidays): it was not listed inside it.
LISTING_SLACK_DAYS = MAX_STALE_DAYS
#: Days fetched before the data session: the window plus the listing slack, so an old
#: symbol's first row lands before ``window start + slack`` and never looks newly listed.
FETCH_DAYS = LOOKBACK_DAYS + LISTING_SLACK_DAYS
#: How far back the rare "no rows in the fetch window" case looks for an older history, to
#: tell a symbol that stopped trading long ago (held by a backtest) from one never cached.
STOPPED_PROBE_DAYS = 3650
#: A symbol with fewer than this share of SPY's sessions in the window is logged as gappy.
GAP_WARN_RATIO = 0.9
#: Trading bars of backtest warmup covering ``FETCH_DAYS`` calendar days: the daily backtest
#: handler converts bars to calendar days at 1.45 per bar (then adds 10), so a derived warmup
#: always serves the whole fetch window from the first bar (the listing check depends on it).
BACKTEST_WARMUP_BARS = math.ceil(FETCH_DAYS / 1.45)

SKIP_INSUFFICIENT_HISTORY = "insufficient_history"
SKIP_STOPPED_TRADING = "symbol_stopped_trading"


def _utc_now() -> datetime:
    """The live decision instant (a seam for tests)."""
    return datetime.now(timezone.utc)


def decision_session(as_of: datetime, backtest: bool):
    """The last completed session a decision at ``as_of`` may read: the platform's BT/live
    parity rule (``market_calendar``: ``decision_data_session`` of the decision label).

    * Live, and a backtest on an intraday clock: ``live_decision_label(as_of)`` (the New York
      date), whose data session is the prior regular session. The decision day's own candle is
      still forming and never enters.
    * A backtest on the DAILY clock stamps bar D at D 00:00 UTC (``DailyBacktestEngine.run``
      turns a date key into midnight UTC). Bar D decides with D's close and fills on the next
      bar, i.e. it is the live decision of ``backtest_decision_label(D)``, whose data session
      is D itself.

    The engine's intraday stamps are New York wall-clock times LABELLED UTC, 09:30-15:55 (e.g.
    ``datetime(D, 9, 30, tzinfo=utc)`` for the 09:30 bar), so none is ever at 00:00 and the two
    clocks never collide. Read as true UTC such a stamp is 05:30-11:55 New York on the same
    date D, so ``live_decision_label`` still gives D and the data session is D-1: the right
    answer for a bar at 09:30-15:55 New York on D.

    A naive ``as_of`` is refused, as ``live_decision_label`` refuses it: its timezone would be
    a guess that moves the session boundary."""
    if not isinstance(as_of, datetime):
        raise ValueError(f"as_of must be a timezone-aware datetime, got {type(as_of).__name__}")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError(f"as_of must be timezone-aware, got naive {as_of!r}")
    utc = as_of.astimezone(timezone.utc)
    if backtest and utc.time() == time(0):
        return decision_data_session(backtest_decision_label(utc.date()))
    return decision_data_session(live_decision_label(as_of))


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


def _validate_frame(frame, name: str) -> None:
    if frame is None or len(frame) == 0:
        raise ValueError(f"Missing OHLCV history: {name}")
    missing = [k for k in ("Date",) + _OHLCV if k not in frame.columns]
    if missing:
        raise ValueError(f"OHLCV history for {name} is missing columns {missing}")


def completed_bars(frame, session, name: str) -> pd.DataFrame:
    """Provider frame (``Date`` column on a RangeIndex) -> its sessions from
    ``session - LOOKBACK_DAYS`` through ``session`` (the decision's data session, see
    ``decision_session``), on an ascending UTC DatetimeIndex. May be empty; staleness and
    length are the caller's verdicts. A candle is placed on its UTC calendar date."""
    _validate_frame(frame, name)
    stamps = _session_stamps(frame, name)
    # For UTC instants, ``ts.date() <= session`` is exactly ``ts < (session + 1) 00:00 UTC``.
    end = np.datetime64(session, "D") + np.timedelta64(1, "D")
    keep = (stamps < end) & (stamps >= end - np.timedelta64(LOOKBACK_DAYS + 1, "D"))
    index = pd.DatetimeIndex(stamps[keep], name="Date").tz_localize("UTC")
    bars = pd.DataFrame({k: frame[k].to_numpy()[keep] for k in _OHLCV}, index=index)
    if not index.is_monotonic_increasing:
        bars = bars.sort_index(kind="stable")
    return bars


def _span(bars) -> str:
    if len(bars) == 0:
        return "no sessions"
    return f"{bars.index[0].date()}..{bars.index[-1].date()} ({len(bars)} sessions)"


def _check_recent_listing(frame, bars, session, name: str, spy_bars, older_history) -> None:
    """Return quietly only when a short completed history (under ``MIN_BARS``, possibly empty on
    the listing day itself) is a genuine recent listing, which the expert skips; raise
    ``ValueError`` (the run aborts) otherwise.

    A recent listing is a valid history whose EARLIEST ROW EVER lies inside the lookback
    window. A history reaching back to the window start but still short has a gap; so does one
    with ANY row before the window (``older_history()``: the long probe fetch), however late in
    the window it resumes. "Inside" is only meaningful if the provider serves the whole window,
    which a backtest does not when its warmup is shorter than the fetch window: every symbol
    would then look newly listed and the run would skip them all. ``spy_bars()`` returns SPY's
    completed bars; SPY always has history, so a short SPY, or one that also starts inside the
    window, aborts."""
    window_start = session - timedelta(days=LOOKBACK_DAYS)
    covered_by = window_start + timedelta(days=LISTING_SLACK_DAYS)
    earliest = pd.Timestamp(_session_stamps(frame, name).min()).date()
    if earliest <= covered_by:
        raise ValueError(
            f"Insufficient history in {name}: {len(bars)} completed bars in the {LOOKBACK_DAYS}-day "
            f"window, need {MIN_BARS}, although its history starts on {earliest}: a gap in the "
            "data, not a new listing")
    older = older_history()
    if older is not None and len(older):
        earliest_ever = pd.Timestamp(_session_stamps(older, name).min()).date()
        if earliest_ever < window_start:
            raise ValueError(
                f"Insufficient history in {name}: {len(bars)} completed bars in the "
                f"{LOOKBACK_DAYS}-day window, need {MIN_BARS}; it resumes on {earliest} but has "
                f"rows from {earliest_ever}, before the window start {window_start}: a gap in the "
                "data, not a new listing")
    try:
        close = bars["Close"].to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Non-numeric close in {name}: {exc}") from exc
    if not (np.isfinite(close) & (close > 0)).all() or not bars.index.is_unique:
        raise ValueError(f"Corrupt OHLCV history for {name}: invalid close or duplicate session")
    reference = spy_bars()
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


def _check_stopped_trading(name: str, last, session, spy_bars) -> None:
    """Return quietly only when ``name``'s stale history means it stopped trading (delisted,
    or halted for over ``MAX_STALE_DAYS``): SPY, read on the same decision, is current. A
    backtest keeps a delisted position and its manage pass analyses it every bar; aborting
    the run there would make any long backtest fail on the first delisting. When SPY is stale
    too, the provider is missing data and the run aborts."""
    reference = spy_bars()
    spy_last = reference.index[-1].date() if len(reference) else None
    if spy_last != session:
        raise ValueError(
            f"Stale OHLCV history for {name}: last completed session {last}, data session "
            f"{session}, and {SPY_SYMBOL}'s last completed session is {spy_last}: missing data, "
            "not a symbol that stopped trading")


class PullbackReversion(MarketExpertInterface):
    """Backtest-registered (research) expert over ``pullback_signal``.

    Recommendation mapping (``expected_profit_percent`` is always 0.0: no price target):
    entry -> BUY (long) / SELL (short), confidence 50..100 by how far RSI is past the entry
    threshold; exit -> SELL (long) / BUY (short), confidence 100; none -> HOLD.
    A long expert's SELL is an EXIT signal: it only acts through a ruleset that closes an open
    position, never by itself opening a short.

    Skips (DeterministicScorer's fields, logged at WARNING since the backtest drops them):
    ``insufficient_history`` for a recent listing, ``symbol_stopped_trading`` for a stale symbol
    while SPY is current. Every other data problem aborts."""

    BACKTEST_WARMUP_BARS = BACKTEST_WARMUP_BARS

    #: ``(data session, provider, SPY completed bars)``: SPY is the same for every symbol of a
    #: bar, so it is fetched and converted once per session and provider object (compared with
    #: ``is``; class default for instances built without ``__init__``).
    _spy_memo = None
    #: ``{(symbol, skip_reason)}`` already logged at WARNING by this instance (created on the
    #: first skip; the class default serves instances built without ``__init__``).
    _skips_logged = None

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

    @staticmethod
    def _fetch(provider, name, session, as_of, days=FETCH_DAYS):
        start = datetime.combine(session - timedelta(days=days), time(0), tzinfo=timezone.utc)
        return provider.get_ohlcv_data(name, start_date=start, end_date=as_of,
                                       lookback_days=days, interval="1d")

    def _spy_bars(self, provider, session, as_of):
        memo = self._spy_memo
        if memo is not None and memo[0] == session and memo[1] is provider:
            return memo[2]
        bars = completed_bars(self._fetch(provider, SPY_SYMBOL, session, as_of), session,
                              SPY_SYMBOL)
        self._spy_memo = (session, provider, bars)  # replaces the previous session's entry
        return bars

    def _log_once(self, symbol, kind, message):
        """WARNING the first time this instance reports ``kind`` for ``symbol``, DEBUG after: a
        recent listing or a held delisted symbol repeats on every bar for months, in every GA
        trial."""
        logged = self._skips_logged
        if logged is None:
            logged = self._skips_logged = set()
        first_time = (symbol, kind) not in logged
        logged.add((symbol, kind))
        (logger.warning if first_time else logger.debug)(message)

    def _skip(self, symbol, reason, message, current_price, first, last, spy):
        self._log_once(symbol, reason,
                       f"PullbackReversion skip {symbol}: {reason}; {symbol} sessions "
                       f"{first}..{last}; {SPY_SYMBOL} sessions {_span(spy)}. {message}")
        return Recommendation(
            signal=OrderRecommendation.HOLD, confidence=0.0, current_price=current_price,
            details=message, expected_profit_percent=0.0, skip=True, skip_reason=reason)

    def _analyze(self, symbol, providers, settings, as_of, backtest, use_case):
        """``use_case`` is the pass: ``AnalysisUseCase.OPEN_POSITIONS`` (a held symbol) or
        anything else (an entry decision)."""
        session = decision_session(as_of, backtest)
        provider = providers.ohlcv()
        probe = []  # the long probe fetch, made at most once

        def spy():
            return self._spy_bars(provider, session, as_of)

        def older_history():
            if not probe:
                probe.append(self._fetch(provider, symbol, session, as_of,
                                         days=STOPPED_PROBE_DAYS))
            return probe[0]

        frame = self._fetch(provider, symbol, session, as_of)
        if frame is not None and len(frame) == 0:
            # Nothing in the fetch window: a symbol that stopped trading long ago (a backtest
            # still holds it) has an older history; one never cached has none and aborts.
            older = older_history()
            if older is not None and len(older):
                frame = older
        bars = completed_bars(frame, session, symbol)
        stamps = _session_stamps(frame, symbol)
        raw_first = pd.Timestamp(stamps.min()).date()
        raw_last = pd.Timestamp(stamps.max()).date()
        last = bars.index[-1].date() if len(bars) else None
        window_start = session - timedelta(days=LOOKBACK_DAYS)
        close = float(bars["Close"].iloc[-1]) if len(bars) else None

        stopped = ((last is not None and (session - last).days > MAX_STALE_DAYS)
                   or (last is None and raw_last < window_start))
        if stopped:
            last_seen = last or raw_last
            if use_case != AnalysisUseCase.OPEN_POSITIONS:
                # The entry universe only holds symbols with a bar at the execution interval
                # today (daily_engine.resolve_universe), so a stale DAILY history there is a
                # stale cache, not a delisting. Only a held position can outlive its data.
                raise ValueError(
                    f"Stale OHLCV history for {symbol}: last completed session {last_seen}, data "
                    f"session {session}, in a {use_case or 'entry'} decision (only the "
                    "open-positions pass may skip a symbol that stopped trading)")
            _check_stopped_trading(symbol, last_seen, session, spy)
            if close is None:
                close = float(frame["Close"].to_numpy(dtype=float)[np.argmax(stamps)])
            return self._skip(
                symbol, SKIP_STOPPED_TRADING,
                f"{symbol} stopped trading: last completed session {last_seen}, data session "
                f"{session}, while {SPY_SYMBOL} is current",
                close, raw_first, last_seen, spy())
        if last is None and raw_first <= session:
            raise ValueError(f"No completed sessions for {symbol} in the {LOOKBACK_DAYS}-day window "
                             f"ending {session}, although it has rows from {raw_first}")
        if len(bars) < MIN_BARS:  # possibly empty: the listing day's own candle only
            _check_recent_listing(frame, bars, session, symbol, spy, older_history)
            if close is None:
                # Listing day: no completed session yet. The only rows are after the data
                # session, i.e. today's candle, whose OPEN is known at the decision (its close
                # is not). Recommendation.current_price is a float, so no None.
                opens = frame["Open"].to_numpy(dtype=float)
                close = float(opens[np.argmin(stamps)])
                if not (math.isfinite(close) and close > 0):
                    raise ValueError(f"Invalid listing-day open for {symbol}: {close!r}")
            return self._skip(
                symbol, SKIP_INSUFFICIENT_HISTORY,
                f"Insufficient OHLCV history ({len(bars)} < {MIN_BARS}): {symbol} listed on "
                f"{raw_first}, inside the {LOOKBACK_DAYS}-day lookback ending {session}",
                close, raw_first, last, spy())

        # Gap check against SPY whatever the gate (SPY is read once per session): a history
        # much thinner than the benchmark's is evaluated, but reported.
        reference = spy()
        if len(bars) < GAP_WARN_RATIO * len(reference):
            self._log_once(
                symbol, "gap",
                f"PullbackReversion: {symbol} has {len(bars)} sessions in the {LOOKBACK_DAYS}-day "
                f"window ending {session}, under {GAP_WARN_RATIO:.0%} of {SPY_SYMBOL}'s "
                f"{len(reference)} (gaps or halts); evaluated anyway")
        spy_bars = reference if settings["trend_gate"] == "sma200_and_spy" else None
        result = pullback_signal(bars, settings, spy_bars)  # validates every setting it reads
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
        return Recommendation(
            signal=signal, confidence=confidence, current_price=close,
            expected_profit_percent=0.0,  # no price target or return forecast
            details=f"{symbol} {settings['direction']} pullback on session {last}: "
                    f"{result['reason']}. Confidence denotes a satisfied rule, not a "
                    "probability of profit.",
            raw_outputs={**result, "session": last.isoformat()})

    def analyze_as_of(self, as_of, context):
        # The daily engine's entry pass sets _gather_symbol; its management pass
        # and the newer context callers also carry extra['symbol'].
        symbol = context.extra["symbol"] if "symbol" in context.extra else self._gather_symbol
        try:
            return self._analyze(symbol, context.providers, context.settings, as_of,
                                 backtest=True, use_case=context.subtype)
        except (ValueError, KeyError, MarketCalendarUnavailable) as exc:
            # The engine logs and skips an ordinary ValueError per symbol; missing, short or
            # corrupt history must instead abort the run (as ETFTrend's basket does), never
            # turn into a plausible-looking backtest that silently never evaluated the symbol.
            # An unavailable session calendar is the same: no decision session, no decision.
            from ba2_providers.fmp_common import FMPHistoryCacheMiss
            raise FMPHistoryCacheMiss(f"PullbackReversion cannot evaluate {symbol}: {exc}") from exc

    def run_analysis(self, symbol, market_analysis):
        try:
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            rec = self._analyze(symbol, self._live_providers(),
                                self._resolve_settings(self._SETTING_KEYS),
                                _utc_now(), backtest=False, use_case=market_analysis.subtype)
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
