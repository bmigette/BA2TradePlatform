"""PullbackReversion: the expert contract around ``pullback_signal``.

Recommendation mapping, the platform's decision-session rule (the decision day's own candle
never enters a live or intraday decision), the SPY path, skips (recent listing, stopped
trading) versus fail-loud data handling, warmup, and registration: backtest handler +
ba2_experts only, never the live registry.
"""
import ast
from datetime import date, datetime, timedelta, timezone
import importlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.backtest_context import BacktestContext
from ba2_common.core.types import AnalysisUseCase, OrderRecommendation
from ba2_experts.PullbackReversion import (
    DIRECTIONS, EXIT_MODES, FETCH_DAYS, LOOKBACK_DAYS, TREND_GATES, PullbackReversion,
    completed_bars, decision_session, pullback_signal)
from ba2_providers.fmp_common import FMPHistoryCacheMiss

# Monday 2024-06-17 09:30 New York. The last completed session is Friday 2024-06-14.
AS_OF = datetime(2024, 6, 17, 13, 30, tzinfo=timezone.utc)
SESSION = date(2024, 6, 14)
LAST_SESSION = "2024-06-14"
N = 300
MODULE = importlib.import_module("ba2_experts.PullbackReversion")


def provider_frame(closes, end=LAST_SESSION):
    """The provider's real shape: a ``Date`` column on a RangeIndex."""
    closes = np.asarray(closes, dtype=float)
    dates = pd.bdate_range(end=end, periods=len(closes), tz="UTC")
    return pd.DataFrame({"Date": dates, "Open": closes, "High": closes * 1.01,
                         "Low": closes * 0.99, "Close": closes, "Volume": 1_000_000.0})


def with_today(frame, close, day="2024-06-17"):
    """Append the decision day's own (still forming) candle, as a provider returns it at as_of."""
    row = frame.iloc[[-1]].copy()
    row["Date"] = pd.Timestamp(day, tz="UTC")
    for col in ("Open", "High", "Low", "Close"):
        row[col] = close
    return pd.concat([frame, row], ignore_index=True)


def _wiggle(start, stop, n=N):
    return np.linspace(start, stop, n) + 0.2 * (-1.0) ** np.arange(n)


def uptrend():
    return _wiggle(100.0, 200.0)


def downtrend():
    return _wiggle(200.0, 100.0)


def long_dip(n=N):
    """An uptrend closing on three sharp down days: oversold, still far above SMA200."""
    closes = _wiggle(100.0, 200.0, n)
    closes[-3:] = closes[-4] - np.array([3.0, 6.0, 9.0])
    return closes


def long_mild_dip():
    """Below SMA5 but not oversold: neither entry nor exit."""
    closes = uptrend()
    closes[-2:] = closes[-3] - np.array([0.6, 1.2])
    return closes


def short_rally():
    closes = downtrend()
    closes[-3:] = closes[-4] + np.array([3.0, 6.0, 9.0])
    return closes


def settings_for(**overrides):
    settings = {"direction": "long", "trend_gate": "sma200", "rsi_period": 2,
                "entry_threshold": 5.0, "exit_mode": "sma5", "rsi_exit": 70.0}
    settings.update(overrides)
    return settings


class FakeOHLCV:
    """Returns the whole stored frame (a provider serving MORE than asked, like the backtest
    memo before its slice), or with ``honour=True`` only ``[start_date, end_date]``."""

    def __init__(self, frames, honour=False):
        self.frames = frames
        self.honour = honour
        self.calls = []

    def get_ohlcv_data(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        frame = self.frames.get(symbol)
        if frame is None or not self.honour:
            return frame
        dates = pd.to_datetime(frame["Date"], utc=True)
        keep = (dates >= kwargs["start_date"]) & (dates <= kwargs["end_date"])
        return frame[keep.to_numpy()].reset_index(drop=True)


class Recorder:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message % args if args else message)


@pytest.fixture
def warnings(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(MODULE, "logger", recorder)
    return recorder.warnings


def analyze(frames, settings, symbol="AAA", as_of=AS_OF, honour=False, expert=None,
            subtype=None):
    provider = FakeOHLCV(frames, honour=honour)
    bundle = SimpleNamespace(ohlcv=lambda: provider)
    expert = expert or object.__new__(PullbackReversion)
    rec = expert.analyze_as_of(as_of, BacktestContext(
        providers=bundle, settings=settings, subtype=subtype, extra={"symbol": symbol}))
    return rec, provider


def fetch_kwargs(session, as_of):
    start = datetime(session.year, session.month, session.day, tzinfo=timezone.utc) - timedelta(
        days=FETCH_DAYS)
    return {"start_date": start, "end_date": as_of, "lookback_days": FETCH_DAYS, "interval": "1d"}


def pure(closes, settings, spy=None):
    """The pure decision on the completed history, as the expert must hand it over."""
    bars = completed_bars(provider_frame(closes), SESSION, "AAA")
    spy_bars = None if spy is None else completed_bars(provider_frame(spy), SESSION, "SPY")
    return pullback_signal(bars, settings, spy_bars)


# --------------------------------------------------------------------------- mapping
def test_long_entry_is_buy_with_depth_confidence():
    settings = settings_for()
    assert pure(long_dip(), settings)["action"] == "entry"
    rec, _ = analyze({"AAA": provider_frame(long_dip())}, settings)
    assert rec.signal == OrderRecommendation.BUY
    rsi = rec.raw_outputs["rsi"]
    assert rec.confidence == pytest.approx(50 + 50 * min(max((5.0 - rsi) / 5.0, 0), 1))
    assert 50 < rec.confidence <= 100
    assert rec.expected_profit_percent == 0.0
    assert rec.current_price == pytest.approx(long_dip()[-1])
    assert rec.raw_outputs["session"] == LAST_SESSION
    assert rec.raw_outputs["action"] == "entry"


def test_long_exit_is_sell_100():
    settings = settings_for()
    assert pure(uptrend(), settings)["action"] == "exit"
    rec, _ = analyze({"AAA": provider_frame(uptrend())}, settings)
    assert rec.signal == OrderRecommendation.SELL
    assert rec.confidence == 100.0
    assert rec.expected_profit_percent == 0.0


def test_short_entry_is_sell_with_depth_confidence():
    settings = settings_for(direction="short")
    assert pure(short_rally(), settings)["action"] == "entry"
    rec, _ = analyze({"AAA": provider_frame(short_rally())}, settings)
    assert rec.signal == OrderRecommendation.SELL
    rsi = rec.raw_outputs["rsi"]
    assert rec.confidence == pytest.approx(50 + 50 * min(max((rsi - 95.0) / 5.0, 0), 1))
    assert 50 < rec.confidence <= 100
    assert rec.expected_profit_percent == 0.0


def test_short_exit_is_buy_100():
    settings = settings_for(direction="short")
    assert pure(downtrend(), settings)["action"] == "exit"
    rec, _ = analyze({"AAA": provider_frame(downtrend())}, settings)
    assert rec.signal == OrderRecommendation.BUY
    assert rec.confidence == 100.0


@pytest.mark.parametrize("closes, settings", [
    (long_mild_dip(), settings_for()),                        # below SMA5, not oversold
    (uptrend(), settings_for(exit_mode="time")),              # time never signals
    (long_dip(), settings_for(direction="short", exit_mode="time")),  # short gate fails
])
def test_no_signal_is_hold(closes, settings):
    assert pure(closes, settings)["action"] == "none"
    rec, _ = analyze({"AAA": provider_frame(closes)}, settings)
    assert rec.signal == OrderRecommendation.HOLD
    assert rec.skip is False
    assert rec.expected_profit_percent == 0.0


def test_recommendation_carries_the_pure_result():
    settings = settings_for()
    rec, _ = analyze({"AAA": provider_frame(long_dip())}, settings)
    expected = pure(long_dip(), settings)
    assert {k: rec.raw_outputs[k] for k in expected} == expected
    assert expected["reason"] in rec.details


# --------------------------------------------------------------------------- decision session
@pytest.mark.parametrize("as_of, backtest, expected", [
    # Daily clock: bar D is stamped D 00:00 UTC and reads D (backtest_decision_label parity).
    (datetime(2024, 6, 14, tzinfo=timezone.utc), True, date(2024, 6, 14)),
    (datetime(2024, 6, 18, tzinfo=timezone.utc), True, date(2024, 6, 18)),
    # A 5-minute stamp, 09:35 New York: the prior regular session, in a backtest and live.
    (datetime(2024, 6, 18, 13, 35, tzinfo=timezone.utc), True, date(2024, 6, 17)),
    (datetime(2024, 6, 18, 13, 35, tzinfo=timezone.utc), False, date(2024, 6, 17)),
    # Live after the close still reads the prior session; Monday reads Friday.
    (datetime(2024, 6, 18, 20, 30, tzinfo=timezone.utc), False, date(2024, 6, 17)),
    (AS_OF, False, SESSION),
    # Live at midnight UTC is 20:00 New York the day before: the helper's New York date.
    (datetime(2024, 6, 18, tzinfo=timezone.utc), False, date(2024, 6, 14)),
])
def test_decision_session_is_the_platform_parity_rule(as_of, backtest, expected):
    from ba2_common.core.market_calendar import (
        backtest_decision_label, decision_data_session, live_decision_label)
    assert decision_session(as_of, backtest) == expected
    helper = (decision_data_session(backtest_decision_label(as_of.date()))
              if backtest and as_of.hour == 0 and as_of.minute == 0
              else decision_data_session(live_decision_label(as_of)))
    assert helper == expected


def test_daily_clock_reads_bar_d_itself_not_d_minus_2():
    """Tuesday bar on the daily clock (midnight UTC): the decision reads Tuesday's close."""
    frame = provider_frame(long_dip(), end="2024-06-18")
    frame = with_today(frame, 999.0, day="2024-06-19")  # a later candle must never leak
    rec, provider = analyze({"AAA": frame}, settings_for(),
                            as_of=datetime(2024, 6, 18, tzinfo=timezone.utc))
    assert rec.raw_outputs["session"] == "2024-06-18"
    assert rec.signal == OrderRecommendation.BUY
    assert provider.calls[0][1] == fetch_kwargs(date(2024, 6, 18),
                                                datetime(2024, 6, 18, tzinfo=timezone.utc))


def test_five_minute_stamp_reads_the_prior_session():
    frame = with_today(provider_frame(long_dip(), end="2024-06-17"), 999.0, day="2024-06-18")
    rec, _ = analyze({"AAA": frame}, settings_for(),
                     as_of=datetime(2024, 6, 18, 13, 35, tzinfo=timezone.utc))
    assert rec.raw_outputs["session"] == "2024-06-17"
    assert rec.signal == OrderRecommendation.BUY


@pytest.mark.parametrize("as_of", [
    AS_OF,                                                    # 09:30 New York
    datetime(2024, 6, 17, 20, 30, tzinfo=timezone.utc),       # after the close
])
def test_decision_day_candle_cannot_change_the_decision(as_of):
    settings = settings_for()
    completed = provider_frame(long_dip())
    today = with_today(completed, long_dip()[-1] + 30.0)
    # Non-vacuous: were today's candle a completed session, the decision would flip to exit.
    leaked = today.set_index(pd.DatetimeIndex(today["Date"]))
    assert pullback_signal(leaked, settings)["action"] == "exit"

    rec, provider = analyze({"AAA": today}, settings, as_of=as_of)
    base, _ = analyze({"AAA": completed}, settings, as_of=as_of)
    assert rec.signal == OrderRecommendation.BUY
    assert rec.raw_outputs == base.raw_outputs
    assert rec.current_price == base.current_price == pytest.approx(long_dip()[-1])
    assert provider.calls == [("AAA", fetch_kwargs(SESSION, as_of))]


def test_naive_as_of_is_refused():
    with pytest.raises(FMPHistoryCacheMiss, match="timezone-aware"):
        analyze({"AAA": provider_frame(long_dip())}, settings_for(),
                as_of=datetime(2024, 6, 17, 9, 30))


def test_completed_bars_is_the_window_through_the_session_on_a_utc_index():
    frame = with_today(provider_frame(uptrend()), 999.0)
    bars = completed_bars(frame, SESSION, "AAA")
    assert isinstance(bars.index, pd.DatetimeIndex) and str(bars.index.tz) == "UTC"
    assert bars.index[-1] == pd.Timestamp(LAST_SESSION, tz="UTC")
    assert list(bars.columns) == ["Open", "High", "Low", "Close", "Volume"]
    window = frame["Close"].to_numpy()[:-1][-len(bars):]
    assert (bars["Close"].to_numpy() == window).all()
    # Unordered provider rows are put in session order; naive dates are read as UTC.
    shuffled = frame.sample(frac=1.0, random_state=1).reset_index(drop=True)
    shuffled["Date"] = shuffled["Date"].dt.tz_localize(None)
    assert completed_bars(shuffled, SESSION, "AAA").equals(bars)


def test_history_is_trimmed_to_the_lookback_window():
    long_history = np.concatenate([np.linspace(50.0, 100.0, 500), uptrend()])
    bars = completed_bars(provider_frame(long_history), SESSION, "AAA")
    assert bars.index[0] == pd.Timestamp(SESSION - timedelta(days=LOOKBACK_DAYS), tz="UTC")
    assert len(bars) < len(long_history)


def test_more_history_than_the_window_decides_identically():
    """M8: a provider serving years more (the backtest memo) or exactly the fetch window (a
    provider honouring start_date) gives identical outputs."""
    frames = {"AAA": provider_frame(long_dip(900)), "SPY": provider_frame(_wiggle(80, 200, 900))}
    for gate in ("sma200", "sma200_and_spy", "slope_ohlcv_v1"):
        wide, _ = analyze(frames, settings_for(trend_gate=gate, exit_mode="sma5_or_choch"))
        exact, provider = analyze(frames, settings_for(trend_gate=gate,
                                                       exit_mode="sma5_or_choch"), honour=True)
        assert wide.raw_outputs == exact.raw_outputs
        assert wide.signal == exact.signal and wide.confidence == exact.confidence
        served = provider.get_ohlcv_data("AAA", **provider.calls[0][1])
        assert len(served) < 900  # the honouring provider really returned less
        assert provider.calls[0][1]["start_date"] == fetch_kwargs(SESSION, AS_OF)["start_date"]


# --------------------------------------------------------------------------- SPY
def test_spy_is_fetched_with_the_same_as_of_and_cut_only_for_the_spy_gate():
    settings = settings_for(trend_gate="sma200_and_spy")
    frames = {"AAA": with_today(provider_frame(long_dip()), 500.0),
              "SPY": with_today(provider_frame(uptrend()), 1.0)}
    rec, provider = analyze(frames, settings)
    assert rec.signal == OrderRecommendation.BUY
    assert [c[0] for c in provider.calls] == ["AAA", "SPY"]
    assert all(c[1] == fetch_kwargs(SESSION, AS_OF) for c in provider.calls)
    # A bear SPY blocks the same long entry.
    frames["SPY"] = provider_frame(downtrend())
    assert analyze(frames, settings)[0].signal != OrderRecommendation.BUY
    # Other gates never read SPY.
    _, provider = analyze(frames, settings_for())
    assert [c[0] for c in provider.calls] == ["AAA"]


def test_spy_is_read_once_per_session_across_symbols():
    """M3: SPY's completed bars are memoised on the instance for the session."""
    settings = settings_for(trend_gate="sma200_and_spy")
    frames = {"AAA": provider_frame(long_dip()), "BBB": provider_frame(uptrend()),
              "SPY": provider_frame(uptrend())}
    provider = FakeOHLCV(frames)
    bundle = SimpleNamespace(ohlcv=lambda: provider)
    expert = object.__new__(PullbackReversion)

    def run(symbol, as_of=AS_OF):
        return expert.analyze_as_of(as_of, BacktestContext(
            providers=bundle, settings=settings, extra={"symbol": symbol}))

    first, second = run("AAA"), run("BBB")
    assert [c[0] for c in provider.calls] == ["AAA", "SPY", "BBB"]
    assert first.signal == OrderRecommendation.BUY and second.signal == OrderRecommendation.SELL
    # A new session reads SPY again (and no stale SPY survives the session change).
    frames["AAA"] = with_today(provider_frame(long_dip()), 150.0)
    frames["SPY"] = with_today(provider_frame(uptrend()), 150.0)
    run("AAA", as_of=datetime(2024, 6, 18, 13, 35, tzinfo=timezone.utc))
    assert [c[0] for c in provider.calls] == ["AAA", "SPY", "BBB", "AAA", "SPY"]


@pytest.mark.parametrize("spy", [
    provider_frame(uptrend(), end="2024-06-13"),   # misses the decision session
    None,                                           # not in the cache
])
def test_misaligned_or_missing_spy_aborts(spy):
    frames = {"AAA": provider_frame(long_dip())}
    if spy is not None:
        frames["SPY"] = spy
    with pytest.raises(FMPHistoryCacheMiss, match="PullbackReversion cannot evaluate AAA"):
        analyze(frames, settings_for(trend_gate="sma200_and_spy"))


def test_short_spy_under_the_spy_gate_aborts():
    frames = {"AAA": provider_frame(long_dip()), "SPY": provider_frame(uptrend()[-150:])}
    with pytest.raises(FMPHistoryCacheMiss, match="Insufficient history in spy_bars"):
        analyze(frames, settings_for(trend_gate="sma200_and_spy"))


def test_gappy_history_is_logged_when_spy_is_read_anyway(warnings):
    """M1: under 90% of SPY's sessions -> WARNING, still evaluated; not checked without SPY."""
    frame = provider_frame(long_dip())
    drop = [i for i in range(40, 240) if i % 4 == 0]  # 50 missing sessions mid-window
    frame = frame.drop(index=drop).reset_index(drop=True)
    frames = {"AAA": frame, "SPY": provider_frame(uptrend())}
    rec, _ = analyze(frames, settings_for(trend_gate="sma200_and_spy"))
    assert rec.skip is False and rec.raw_outputs["action"] in ("entry", "exit", "none")
    assert len(warnings) == 1 and "AAA has 250 sessions" in warnings[0] and "SPY" in warnings[0]
    warnings.clear()
    _, provider = analyze(frames, settings_for())
    assert warnings == [] and [c[0] for c in provider.calls] == ["AAA"]


# --------------------------------------------------------------------------- fail loud
def _defect(kind):
    frame = provider_frame(long_dip())
    if kind == "missing":
        return None
    if kind == "empty":
        return frame.iloc[0:0]
    if kind == "gap":  # history reaches back to the window start, the middle is missing
        return pd.concat([frame.iloc[:50], frame.iloc[-100:]], ignore_index=True)
    if kind == "nan":
        frame.loc[N - 10, "Close"] = float("nan")
        return frame
    if kind == "duplicate":
        return pd.concat([frame, frame.iloc[[-1]]], ignore_index=True)
    if kind == "no_close":
        return frame.drop(columns=["Close"])
    if kind == "no_date":
        frame.loc[5, "Date"] = pd.NaT
        return frame
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["missing", "empty", "gap", "nan", "duplicate", "no_close",
                                  "no_date"])
def test_bad_history_aborts_the_backtest(kind):
    # SPY is present and current: a skip would be possible, so an abort is the defect's own.
    frames = {"AAA": _defect(kind), "SPY": provider_frame(uptrend())}
    with pytest.raises(FMPHistoryCacheMiss):
        analyze(frames, settings_for())


def test_a_gap_is_not_mistaken_for_a_new_listing():
    frames = {"AAA": _defect("gap"), "SPY": provider_frame(uptrend())}
    with pytest.raises(FMPHistoryCacheMiss, match="a gap in the data, not a new listing"):
        analyze(frames, settings_for())


@pytest.mark.parametrize("bad", [{"direction": "sideways"}, {"rsi_period": 9},
                                 {"entry_threshold": 0.0}, {"exit_mode": "never"}])
def test_bad_settings_abort_the_backtest(bad):
    with pytest.raises(FMPHistoryCacheMiss):
        analyze({"AAA": provider_frame(long_dip())}, settings_for(**bad))


def test_missing_setting_aborts_the_backtest():
    settings = settings_for()
    del settings["trend_gate"]
    with pytest.raises(FMPHistoryCacheMiss):
        analyze({"AAA": provider_frame(long_dip())}, settings)


# --------------------------------------------------------------------------- stopped trading
def test_held_delisted_symbol_is_skipped_in_the_manage_pass(warnings):
    """C1: the manage pass analyses every held symbol each bar; one whose data ended must not
    abort the run while SPY is current."""
    from app.services.backtest.daily_engine import _recommendation_to_expert_recommendation
    frames = {"AAA": provider_frame(long_dip(), end="2024-05-31"), "SPY": provider_frame(uptrend())}
    rec, provider = analyze(frames, settings_for(), subtype=AnalysisUseCase.OPEN_POSITIONS)
    assert rec.skip is True and rec.skip_reason == "symbol_stopped_trading"
    assert rec.signal == OrderRecommendation.HOLD and rec.confidence == 0.0
    assert rec.expected_profit_percent == 0.0
    assert rec.current_price == pytest.approx(long_dip()[-1])
    assert [c[0] for c in provider.calls] == ["AAA", "SPY"]
    assert len(warnings) == 1
    assert "AAA" in warnings[0] and "symbol_stopped_trading" in warnings[0]
    assert "2024-05-31" in warnings[0] and "..2024-06-14" in warnings[0]
    # The engine's manage pass drops it (no row, no action) and carries on.
    assert _recommendation_to_expert_recommendation(
        rec, expert_instance_id=1, symbol="AAA", as_of=AS_OF, allow_hold=True,
        subtype=AnalysisUseCase.OPEN_POSITIONS) is None


def test_symbol_delisted_before_the_whole_window_is_still_skipped(warnings):
    """A position held for over a year after its last candle: nothing in the fetch window, but
    an older history exists (probe), so it stopped trading rather than never being cached."""
    frames = {"AAA": provider_frame(long_dip(), end="2023-01-31"), "SPY": provider_frame(uptrend())}
    rec, provider = analyze(frames, settings_for(), honour=True)
    assert rec.skip_reason == "symbol_stopped_trading"
    assert [c[0] for c in provider.calls] == ["AAA", "AAA", "SPY"]
    assert "2023-01-31" in warnings[0]


@pytest.mark.parametrize("spy_end", ["2024-05-31", "2024-06-13"])
def test_stale_symbol_with_stale_spy_aborts(spy_end):
    frames = {"AAA": provider_frame(long_dip(), end="2024-05-31"),
              "SPY": provider_frame(uptrend(), end=spy_end)}
    with pytest.raises(FMPHistoryCacheMiss, match="missing data, not a symbol that stopped"):
        analyze(frames, settings_for(), subtype=AnalysisUseCase.OPEN_POSITIONS)


# --------------------------------------------------------------------------- recent listings
def recent_listing():
    """120 sessions ending on the decision session: listed around Dec 2023, well inside the
    420-day window that starts 2023-04-21."""
    return provider_frame(long_dip()[-120:])


def spy_full():
    return provider_frame(uptrend())


@pytest.mark.parametrize("gate", ["sma200", "sma200_and_spy"])
def test_recent_listing_is_skipped_like_deterministic_scorer(gate, warnings):
    rec, provider = analyze({"AAA": recent_listing(), "SPY": spy_full()},
                            settings_for(trend_gate=gate))
    assert rec.skip is True
    assert rec.skip_reason == "insufficient_history"
    assert rec.signal == OrderRecommendation.HOLD
    assert rec.confidence == 0.0
    assert rec.expected_profit_percent == 0.0
    assert rec.current_price == pytest.approx(long_dip()[-1])
    assert "Insufficient OHLCV history (120 < 200)" in rec.details
    # SPY is read once, as the reference: the listing is decided before any gate.
    assert [c[0] for c in provider.calls] == ["AAA", "SPY"]
    assert all(c[1]["end_date"] == AS_OF for c in provider.calls)
    # I3: logged, since the backtest drops skips.
    assert len(warnings) == 1
    first = recent_listing()["Date"].iloc[0].date()
    for part in ("AAA", "insufficient_history", f"{first}..2024-06-14", "SPY sessions"):
        assert part in warnings[0], part


def test_listing_day_itself_is_a_recent_listing(warnings):
    """M2: only today's (forming) candle exists: no completed session, still a listing skip."""
    frame = provider_frame([50.0], end="2024-06-17")
    rec, _ = analyze({"AAA": frame, "SPY": spy_full()}, settings_for())
    assert rec.skip is True and rec.skip_reason == "insufficient_history"
    assert rec.current_price == 0.0
    assert "(0 < 200)" in rec.details and "listed on 2024-06-17" in rec.details
    assert len(warnings) == 1
    # Without a sound SPY reference it still aborts.
    with pytest.raises(FMPHistoryCacheMiss, match="Missing OHLCV history: SPY"):
        analyze({"AAA": frame}, settings_for())


def test_a_skip_leaves_the_backtest_ledger_untouched():
    from app.services.backtest.daily_engine import _recommendation_to_expert_recommendation
    rec, _ = analyze({"AAA": recent_listing(), "SPY": spy_full()}, settings_for())
    assert _recommendation_to_expert_recommendation(
        rec, expert_instance_id=1, symbol="AAA", as_of=AS_OF, allow_hold=True) is None


@pytest.mark.parametrize("spy, match", [
    (provider_frame(uptrend()[-150:]), "benchmark always has history"),        # SPY short
    (provider_frame(uptrend()[-250:]), "provider does not cover the window"),  # warmup short
    (None, "Missing OHLCV history: SPY"),
])
def test_recent_listing_needs_a_sound_spy_reference(spy, match):
    frames = {"AAA": recent_listing()}
    if spy is not None:
        frames["SPY"] = spy
    with pytest.raises(FMPHistoryCacheMiss, match=match):
        analyze(frames, settings_for())


def test_recent_listing_with_corrupt_closes_aborts():
    frame = recent_listing()
    frame.loc[10, "Close"] = float("nan")
    with pytest.raises(FMPHistoryCacheMiss, match="Corrupt OHLCV history"):
        analyze({"AAA": frame, "SPY": spy_full()}, settings_for())


# --------------------------------------------------------------------------- settings
def test_settings_definitions_defaults_and_choices():
    defs = PullbackReversion.get_settings_definitions()
    assert set(defs) == set(PullbackReversion._SETTING_KEYS)
    defaults = {k: v["default"] for k, v in defs.items()}
    assert defaults == {"direction": "long", "trend_gate": "sma200", "rsi_period": 2,
                        "entry_threshold": 5.0, "exit_mode": "sma5", "rsi_exit": 70.0}
    assert defs["direction"]["valid_values"] == list(DIRECTIONS)
    assert defs["trend_gate"]["valid_values"] == list(TREND_GATES)
    assert defs["exit_mode"]["valid_values"] == list(EXIT_MODES)
    for spec in defs.values():
        assert spec["required"] is True and spec["type"] in ("str", "int", "float")
        assert spec["description"]
    # The defaults are a valid configuration of the pure function.
    assert pure(long_dip(), defaults)["action"] == "entry"


# --------------------------------------------------------------------------- live == backtest
def _live_expert(monkeypatch, frames):
    monkeypatch.setattr(MODULE, "_utc_now", lambda: AS_OF)
    captured, statuses = [], []
    monkeypatch.setattr(MODULE, "add_instance", lambda row: captured.append(row))
    monkeypatch.setattr(MODULE, "update_instance", lambda row: statuses.append(row.status))
    bundle = SimpleNamespace(ohlcv=lambda: FakeOHLCV(frames))
    expert = object.__new__(PullbackReversion)
    expert.id = 1
    expert.logger = SimpleNamespace(error=lambda *a, **kw: pytest.fail(str(a)))
    expert._live_providers = lambda: bundle
    expert._resolve_settings = lambda keys: settings_for()
    analysis = SimpleNamespace(id=10, subtype=None, status=None, state={})
    expert.run_analysis("AAA", analysis)
    return analysis, captured, statuses


def test_live_run_analysis_persists_the_backtest_decision(monkeypatch):
    frames = {"AAA": with_today(provider_frame(long_dip()), 500.0)}
    analysis, captured, _ = _live_expert(monkeypatch, frames)
    rec, _ = analyze(frames, settings_for())
    row = captured[0]
    assert row.recommended_action == rec.signal == OrderRecommendation.BUY
    assert row.confidence == rec.confidence
    assert row.price_at_date == rec.current_price
    assert row.expected_profit_percent == 0.0
    assert row.data["PullbackReversion"] == rec.raw_outputs
    assert analysis.state == {"PullbackReversion": rec.raw_outputs}


def test_live_bar_d_reads_the_same_session_as_the_backtest_bar_d():
    """Parity: backtest daily bar D (midnight UTC) and the live decision during N(D) read D."""
    frames = {"AAA": with_today(provider_frame(long_dip()), 500.0)}
    bt, _ = analyze(frames, settings_for(), as_of=datetime(2024, 6, 14, tzinfo=timezone.utc))
    live, _ = analyze(frames, settings_for(), as_of=AS_OF)  # Monday 09:30, N(Friday)
    assert bt.raw_outputs == live.raw_outputs


@pytest.mark.parametrize("frames, reason", [
    ({"AAA": provider_frame(long_dip()[-120:]), "SPY": provider_frame(_wiggle(100, 200))},
     "insufficient_history"),
    ({"AAA": provider_frame(long_dip(), end="2024-05-31"), "SPY": provider_frame(_wiggle(100, 200))},
     "symbol_stopped_trading"),
])
def test_live_run_analysis_skips_like_deterministic_scorer(monkeypatch, frames, reason):
    from ba2_common.core.types import MarketAnalysisStatus
    analysis, captured, statuses = _live_expert(monkeypatch, frames)
    rec, _ = analyze(frames, settings_for())
    assert captured == []  # no recommendation row, as DeterministicScorer
    assert analysis.status == MarketAnalysisStatus.SKIPPED
    assert statuses[-1] == MarketAnalysisStatus.SKIPPED
    assert analysis.state == {"skipped": True, "skip_reason": reason,
                              "skip_message": rec.details}


# --------------------------------------------------------------------------- registration
def test_registered_in_ba2_experts_and_the_backtest_handler():
    import ba2_experts
    from app.services.backtest import daily_backtest_handler as handler
    assert ba2_experts.get_expert_class("PullbackReversion") is PullbackReversion
    assert PullbackReversion in ba2_experts.experts
    assert handler._SUPPORTED_EXPERTS["PullbackReversion"] == "ba2_experts.PullbackReversion"
    module = importlib.import_module(handler._SUPPORTED_EXPERTS["PullbackReversion"])
    assert getattr(module, "PullbackReversion") is PullbackReversion


def test_derived_warmup_covers_the_fetch_window():
    """I2: the handler's own bars->days conversion of the class's warmup serves the whole
    fetch window (lookback + listing slack) from the first bar; the table agrees with the
    class, since derive_warmup_days falls back to it on an import failure."""
    from app.services.backtest import daily_backtest_handler as handler
    bars = PullbackReversion.BACKTEST_WARMUP_BARS
    assert handler._EXPERT_WARMUP_BARS["PullbackReversion"] == bars
    days = handler.derive_warmup_days([{"class": "PullbackReversion"}])
    assert days == int(bars * handler._BARS_TO_CALDAYS) + 10
    assert days >= FETCH_DAYS == LOOKBACK_DAYS + MODULE.LISTING_SLACK_DAYS


def _live_registry_file():
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "ba2_trade_platform" / "modules" / "experts" / "__init__.py"
        if candidate.is_file():
            return candidate
    pytest.fail("ba2_trade_platform/modules/experts/__init__.py not found above this test")


def test_live_registry_does_not_list_it():
    """Research-only, like ETFTrend. Read the source (importing it pulls TradingAgents/langchain):
    the names in the ``_build_experts_list`` return list ARE the live ``experts`` list."""
    path = _live_registry_file()
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    build = next(n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "_build_experts_list")
    returned = next(n for n in ast.walk(build) if isinstance(n, ast.Return))
    names = {elt.id for elt in returned.value.elts}
    assert {"TradingAgents", "DeterministicScorer"} <= names  # non-vacuous
    assert "PullbackReversion" not in names
    assert "PullbackReversion" not in source
    assert not (path.parent / "PullbackReversion.py").exists()  # no alias shim either
