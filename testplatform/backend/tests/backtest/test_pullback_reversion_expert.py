"""PullbackReversion: the expert contract around ``pullback_signal``.

Recommendation mapping, the causal session cut (the decision day's own candle never enters),
the SPY path, fail-loud data handling, and registration: backtest handler + ba2_experts only,
never the live registry.
"""
import ast
from datetime import datetime, timezone
import importlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.backtest_context import BacktestContext
from ba2_common.core.types import OrderRecommendation
from ba2_experts.PullbackReversion import (
    DIRECTIONS, EXIT_MODES, TREND_GATES, PullbackReversion, completed_bars, pullback_signal)
from ba2_providers.fmp_common import FMPHistoryCacheMiss

# Monday 2024-06-17 09:30 New York. The last completed session is Friday 2024-06-14.
AS_OF = datetime(2024, 6, 17, 13, 30, tzinfo=timezone.utc)
LAST_SESSION = "2024-06-14"
N = 300


def provider_frame(closes, end=LAST_SESSION):
    """The provider's real shape: a ``Date`` column on a RangeIndex."""
    closes = np.asarray(closes, dtype=float)
    dates = pd.bdate_range(end=end, periods=len(closes), tz="UTC")
    return pd.DataFrame({"Date": dates, "Open": closes, "High": closes * 1.01,
                         "Low": closes * 0.99, "Close": closes, "Volume": 1_000_000.0})


def with_today(frame, close):
    """Append the decision day's own (still forming) candle, as a provider returns it at as_of."""
    row = frame.iloc[[-1]].copy()
    row["Date"] = pd.Timestamp("2024-06-17", tz="UTC")
    for col in ("Open", "High", "Low", "Close"):
        row[col] = close
    return pd.concat([frame, row], ignore_index=True)


def _wiggle(start, stop):
    return np.linspace(start, stop, N) + 0.2 * (-1.0) ** np.arange(N)


def uptrend():
    return _wiggle(100.0, 200.0)


def downtrend():
    return _wiggle(200.0, 100.0)


def long_dip():
    """An uptrend closing on three sharp down days: oversold, still far above SMA200."""
    closes = uptrend()
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
    def __init__(self, frames):
        self.frames = frames
        self.calls = []

    def get_ohlcv_data(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        return self.frames.get(symbol)


def analyze(frames, settings, symbol="AAA", as_of=AS_OF):
    provider = FakeOHLCV(frames)
    bundle = SimpleNamespace(ohlcv=lambda: provider)
    expert = object.__new__(PullbackReversion)
    rec = expert.analyze_as_of(as_of, BacktestContext(
        providers=bundle, settings=settings, extra={"symbol": symbol}))
    return rec, provider


def pure(closes, settings, spy=None):
    """The pure decision on the completed history, as the expert must hand it over."""
    bars = completed_bars(provider_frame(closes), AS_OF, "AAA")
    spy_bars = None if spy is None else completed_bars(provider_frame(spy), AS_OF, "SPY")
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
    assert rec.expected_profit_percent == 0.0


def test_recommendation_carries_the_pure_result():
    settings = settings_for()
    rec, _ = analyze({"AAA": provider_frame(long_dip())}, settings)
    expected = pure(long_dip(), settings)
    assert {k: rec.raw_outputs[k] for k in expected} == expected
    assert expected["reason"] in rec.details


# --------------------------------------------------------------------------- causality
@pytest.mark.parametrize("as_of", [
    AS_OF,                                                    # 09:30 New York
    datetime(2024, 6, 17, 20, 30, tzinfo=timezone.utc),       # after the close
    datetime(2024, 6, 17, 9, 30),                             # tz-naive, as a backtest may pass
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
    assert provider.calls == [("AAA", {"end_date": as_of, "lookback_days": 420, "interval": "1d"})]


def test_completed_bars_is_the_provider_frame_minus_today_on_a_utc_index():
    frame = with_today(provider_frame(uptrend()), 999.0)
    bars = completed_bars(frame, AS_OF, "AAA")
    assert isinstance(bars.index, pd.DatetimeIndex) and str(bars.index.tz) == "UTC"
    assert bars.index[-1] == pd.Timestamp(LAST_SESSION, tz="UTC")
    assert list(bars.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert (bars["Close"].to_numpy() == frame["Close"].to_numpy()[:-1]).all()
    # Unordered provider rows are put in session order; naive dates are read as UTC.
    shuffled = frame.sample(frac=1.0, random_state=1).reset_index(drop=True)
    shuffled["Date"] = shuffled["Date"].dt.tz_localize(None)
    assert completed_bars(shuffled, AS_OF, "AAA").equals(bars)


def test_history_is_trimmed_to_the_lookback_window():
    long_history = np.concatenate([np.linspace(50.0, 100.0, 500), uptrend()])
    bars = completed_bars(provider_frame(long_history), AS_OF, "AAA")
    assert bars.index[0] == pd.Timestamp("2023-04-24", tz="UTC")  # 420 days before 2024-06-17
    assert len(bars) < len(long_history)


# --------------------------------------------------------------------------- SPY
def test_spy_is_fetched_with_the_same_as_of_and_cut_only_for_the_spy_gate():
    settings = settings_for(trend_gate="sma200_and_spy")
    frames = {"AAA": with_today(provider_frame(long_dip()), 500.0),
              "SPY": with_today(provider_frame(uptrend()), 1.0)}
    rec, provider = analyze(frames, settings)
    assert rec.signal == OrderRecommendation.BUY
    assert [c[0] for c in provider.calls] == ["AAA", "SPY"]
    assert all(c[1] == {"end_date": AS_OF, "lookback_days": 420, "interval": "1d"}
               for c in provider.calls)
    # A bear SPY blocks the same long entry.
    frames["SPY"] = provider_frame(downtrend())
    assert analyze(frames, settings)[0].signal != OrderRecommendation.BUY
    # Other gates never read SPY.
    _, provider = analyze(frames, settings_for())
    assert [c[0] for c in provider.calls] == ["AAA"]


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


# --------------------------------------------------------------------------- fail loud
def _defect(kind):
    frame = provider_frame(long_dip())
    if kind == "missing":
        return None
    if kind == "empty":
        return frame.iloc[0:0]
    if kind == "short":
        return frame.iloc[-150:].reset_index(drop=True)
    if kind == "stale":
        return provider_frame(long_dip(), end="2024-05-31")
    if kind == "nan":
        frame.loc[N - 10, "Close"] = float("nan")
        return frame
    if kind == "duplicate":
        return pd.concat([frame, frame.iloc[[-1]]], ignore_index=True)
    if kind == "no_close":
        return frame.drop(columns=["Close"])
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["missing", "empty", "short", "stale", "nan", "duplicate",
                                  "no_close"])
def test_bad_history_aborts_the_backtest(kind):
    with pytest.raises(FMPHistoryCacheMiss):
        analyze({"AAA": _defect(kind)}, settings_for())


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
def test_live_run_analysis_persists_the_backtest_decision(monkeypatch):
    module = importlib.import_module("ba2_experts.PullbackReversion")

    class Clock:
        @staticmethod
        def now(tz):
            return AS_OF

    monkeypatch.setattr(module, "datetime", Clock)
    captured = []
    monkeypatch.setattr(module, "add_instance", lambda row: captured.append(row))
    monkeypatch.setattr(module, "update_instance", lambda row: None)
    frames = {"AAA": with_today(provider_frame(long_dip()), 500.0)}
    provider = FakeOHLCV(frames)
    bundle = SimpleNamespace(ohlcv=lambda: provider)
    expert = object.__new__(PullbackReversion)
    expert.id = 1
    expert.logger = SimpleNamespace(error=lambda *a, **kw: pytest.fail(str(a)))
    expert._live_providers = lambda: bundle
    expert._resolve_settings = lambda keys: settings_for()
    analysis = SimpleNamespace(id=10, subtype=None, status=None, state={})
    expert.run_analysis("AAA", analysis)
    rec, _ = analyze(frames, settings_for())
    row = captured[0]
    assert row.recommended_action == rec.signal == OrderRecommendation.BUY
    assert row.confidence == rec.confidence
    assert row.price_at_date == rec.current_price
    assert row.expected_profit_percent == 0.0
    assert row.data["PullbackReversion"] == rec.raw_outputs
    assert analysis.state == {"PullbackReversion": rec.raw_outputs}


# --------------------------------------------------------------------------- registration
def test_registered_in_ba2_experts_and_the_backtest_handler():
    import ba2_experts
    from app.services.backtest import daily_backtest_handler as handler
    assert ba2_experts.get_expert_class("PullbackReversion") is PullbackReversion
    assert PullbackReversion in ba2_experts.experts
    assert handler._SUPPORTED_EXPERTS["PullbackReversion"] == "ba2_experts.PullbackReversion"
    assert handler._EXPERT_WARMUP_BARS["PullbackReversion"] == 274
    module = importlib.import_module(handler._SUPPORTED_EXPERTS["PullbackReversion"])
    assert getattr(module, "PullbackReversion") is PullbackReversion


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
