"""Engine + handler wiring of the SPY benchmark regime (plan 2026-10-24 Task 9).

* the engine publishes ``calendar.at(price.scan_cutoff_date(as_of))`` per bar, the same lookup day as
  ``set_stressed`` (daily clock: the bar's own date; intraday: the last FINISHED session);
* a gated run without a calendar is REFUSED; a plain run pays nothing;
* the seam is reset at the start and the end of a run;
* the handler builds the calendar only when needed, memoised on (symbol, history source, window).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from ba2_common.core import benchmark_regime as br
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.regime_overlay import StressedCalendar, get_stressed
from ba2_common.core.types import OrderRecommendation, Recommendation

from app.services.backtest import price_source as ps_mod
from app.services.backtest.price_source import AsOfPriceSource, MemoizedOHLCVProvider
from tests.backtest.test_intraday_daily_knowability import (
    DAILY, SYMBOL, _FakeDaily, _intraday_rows)

SESSIONS = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]


def _bench_dates(n: int = 300):
    out, d = [], date(2023, 1, 2)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _calendar(n: int = 300) -> br.BenchmarkRegimeCalendar:
    dates = _bench_dates(n)
    return br.BenchmarkRegimeCalendar(dates, [100.0 + i * 0.1 for i in range(len(dates))])


class _RegimeProbe(MarketExpertInterface):
    """Records the published benchmark regime (and the overlay flag) at every decision."""

    def __init__(self, id):
        super().__init__(id)
        self.records = []

    @classmethod
    def description(cls) -> str:
        return "benchmark regime probe"

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        self.records.append({"as_of": as_of, "regime": br.get_benchmark_regime(),
                             "stressed": get_stressed()})
        return Recommendation(signal=OrderRecommendation.HOLD, confidence=50.0, current_price=100.0,
                              details="probe", expected_profit_percent=0.0)


def _run(interval, run_id, *, calendar=None, required=False, regime_calendar=None, times=None):
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.seam_wiring import set_backtest_ohlcv_override, wire_backtest_seams
    from tests.backtest.test_max_loss_stop_engine import CFG

    ps_mod.clear_ohlcv_memo()
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"benchmark-regime-{run_id}")
    ctx.__enter__()
    try:
        seed_account_definition(run_id, CFG)
        ruleset_id = seed_enter_long_ruleset()
        seed_expert_instance(account_id=run_id, expert_class_name="_RegimeProbe",
                             enter_market_ruleset_id=ruleset_id, instance_id=run_id)
        memo = MemoizedOHLCVProvider(_FakeDaily(), datetime(2023, 12, 1), datetime(2024, 1, 31),
                                     interval=interval)
        ps = AsOfPriceSource(ohlcv_provider=None, interval=interval)
        memo.bind_price_source(ps)
        if interval == "1d":
            ps.load_bars(SYMBOL, [{"Date": d, "Open": o, "High": h, "Low": lo, "Close": c,
                                   "Volume": 1000} for d, o, h, lo, c in DAILY
                                  if date(2024, 1, 2) <= d <= date(2024, 1, 4)])
        else:
            ps.load_bars(SYMBOL, _intraday_rows())
        account = BacktestAccount(run_id, ps, CFG)
        resolver.register_account(run_id, account)
        expert = _RegimeProbe(run_id)
        expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                              "enable_buy": (True, "bool")})
        resolver.register_expert(run_id, expert)
        set_backtest_ohlcv_override(memo)
        try:
            engine = DailyBacktestEngine(
                account=account, experts=[(expert, run_id, {}, ruleset_id)], price_source=ps,
                config={"start_date": datetime(2024, 1, 2), "end_date": datetime(2024, 1, 4, 23, 59),
                        "enabled_instruments": [SYMBOL], "seed": 42,
                        **({"run_schedule_override": {"days": {d: True for d in (
                            "monday", "tuesday", "wednesday", "thursday", "friday")},
                            "times": list(times)}} if times else {})},
                indicator_provider=None, regime_calendar=regime_calendar,
                benchmark_calendar=calendar, benchmark_regime_required=required)
            engine._indicator_provider = None
            engine.run()
        finally:
            set_backtest_ohlcv_override(None)
        return expert.records
    finally:
        ctx.__exit__(None, None, None)


@pytest.fixture(autouse=True)
def _clean_seam():
    br.reset_benchmark_regime()
    yield
    br.reset_benchmark_regime()


def test_engine_refuses_gated_run_without_calendar():
    with pytest.raises(RuntimeError, match="benchmark regime"):
        _run("1d", 901, calendar=None, required=True)


def test_engine_without_gates_does_not_need_a_calendar():
    records = _run("1d", 902, calendar=None, required=False)
    assert records and all(r["regime"] is None for r in records)


def test_published_regime_is_the_knowable_session_daily_clock():
    cal = _calendar()
    records = _run("1d", 903, calendar=cal, required=True)
    assert [r["as_of"].date() for r in records] == SESSIONS
    for r in records:
        assert r["regime"] == cal.at(r["as_of"].date())
        assert r["regime"].session is not None


def test_published_regime_intraday_uses_last_finished_session():
    # the calendar holds the decision day itself (and later days): the point is that it is NOT read.
    # Session dates only (holidays out), as a real SPY series is.
    days = [date(2023, 12, 1) + timedelta(days=i) for i in range(50)]
    days = [d for d in days if d.weekday() < 5 and d not in (date(2023, 12, 25), date(2024, 1, 1))]
    cal = br.BenchmarkRegimeCalendar(days, [100.0 + i for i in range(len(days))])
    records = _run("5min", 904, calendar=cal, required=True, times=["09:30"])
    assert len(records) == len(SESSIONS)
    prior = {date(2024, 1, 2): date(2023, 12, 29), date(2024, 1, 3): date(2024, 1, 2),
             date(2024, 1, 4): date(2024, 1, 3)}
    for r in records:
        day = r["as_of"].date()
        assert r["regime"] == cal.at(prior[day])
        assert r["regime"].session != day


def test_seam_reset_between_runs():
    _run("1d", 905, calendar=_calendar(), required=True)
    assert br.get_benchmark_regime() is None
    br.set_benchmark_regime(_calendar().at(date(2024, 1, 3)))      # a dead trial's leftover
    records = _run("1d", 906, calendar=None, required=False)
    assert all(r["regime"] is None for r in records)


def test_overlay_calendar_still_published_identically():
    dates = _bench_dates(300)
    closes = [100.0 + (i % 7) for i in range(len(dates))]
    plain = _run("1d", 907, regime_calendar=StressedCalendar(dates, closes))
    gated = _run("1d", 908, regime_calendar=StressedCalendar(dates, closes),
                 calendar=_calendar(), required=True)
    assert [r["stressed"] for r in plain] == [r["stressed"] for r in gated]


def test_warmup_without_a_regime_logs_the_first_date_a_regime_exists(monkeypatch):
    from app.services.backtest import daily_engine

    seen = []
    monkeypatch.setattr(daily_engine.logger, "warning", lambda msg, *a, **k: seen.append(str(msg)))
    short = br.BenchmarkRegimeCalendar(_bench_dates(100), [100.0] * 100)    # too short: always unknown
    _run("1d", 909, calendar=short, required=True)
    assert any("benchmark regime" in m and "first" in m for m in seen), seen


# ----------------------------------------------------------------------------------------- handler
class _FakeMemo:
    calls = []

    def __init__(self, inner, start, end, interval="1d", cached_only=False):
        self.inner = inner
        _FakeMemo.calls.append((inner.tag, start, end))

    def get_ohlcv_data(self, symbol, interval="1d", **kw):
        n = 300
        d0 = date(2022, 1, 3)
        dates = [pd.Timestamp(d0 + timedelta(days=i)) for i in range(n)]
        return pd.DataFrame({"Date": dates, "Close": [100.0 + i * 0.1 for i in range(n)]})


class _Raw:
    def __init__(self, tag):
        self.tag = tag


@pytest.fixture
def fake_memo(monkeypatch):
    from app.services.backtest import daily_backtest_handler as h

    _FakeMemo.calls = []
    monkeypatch.setattr(ps_mod, "MemoizedOHLCVProvider", _FakeMemo)
    h._REGIME_CALENDAR_MEMO.clear()
    h._BENCHMARK_CALENDAR_MEMO.clear()
    yield h
    h._REGIME_CALENDAR_MEMO.clear()
    h._BENCHMARK_CALENDAR_MEMO.clear()


S, E = datetime(2024, 1, 2), datetime(2024, 3, 1)


def test_memo_key_includes_history_source(fake_memo):
    h = fake_memo
    a = h._build_benchmark_regime_calendar(_Raw("alpaca"), S, E, "alpaca")
    f = h._build_benchmark_regime_calendar(_Raw("fmp"), S, E, "fmp")
    assert a is not None and f is not None and a is not f
    assert h._build_benchmark_regime_calendar(_Raw("alpaca"), S, E, "alpaca") is a
    assert len(_FakeMemo.calls) == 2                       # the repeat was memoised


def test_overlay_memo_key_includes_history_source(fake_memo):
    h = fake_memo
    a = h._build_regime_calendar(_Raw("alpaca"), S, E, "alpaca")
    f = h._build_regime_calendar(_Raw("fmp"), S, E, "fmp")
    assert a is not f
    assert h._build_regime_calendar(_Raw("fmp"), S, E, "fmp") is f
    assert [c[0] for c in _FakeMemo.calls] == ["alpaca", "fmp"]


def test_benchmark_calendar_none_when_spy_history_unreadable(monkeypatch, fake_memo):
    class _Empty(_FakeMemo):
        def get_ohlcv_data(self, symbol, interval="1d", **kw):
            return pd.DataFrame({"Date": [], "Close": []})
    monkeypatch.setattr(ps_mod, "MemoizedOHLCVProvider", _Empty)
    assert fake_memo._build_benchmark_regime_calendar(_Raw("x"), S, E, "x") is None


def _tree(field):
    return {"op": "and", "children": [{"id": "m-spy-trend", "field": field, "op": "==", "value": 1}]}


def test_config_uses_benchmark_regime_detection():
    import json
    from app.services.backtest.daily_backtest_handler import _config_uses_benchmark_regime as uses

    assert not uses({"experts": [], "entry_rules": [{"field": "rsi"}]})
    assert uses({"benchmark_regime_gates": True})
    assert uses({"entry_rules": [_tree("spy_trend_regime")]})
    assert uses({"entry_rules": [_tree("spy_vol_regime")]})
    assert uses({"experts": [{"settings": {"rules": json.dumps(_tree("spy_vol_regime"))}}]})
    assert not uses({"benchmark_regime_gates": False})


def test_handler_builds_benchmark_calendar_only_when_needed(monkeypatch):
    from app.services.backtest import daily_backtest_handler as h

    calls = []
    monkeypatch.setattr(h, "_build_benchmark_regime_calendar",
                        lambda *a, **k: calls.append(a) or "CAL")
    cfg = {"start_date": S, "end_date": E, "entry_rules": [{"field": "rsi"}]}
    assert h._benchmark_calendar_for_run(_Raw("x"), cfg) is None and not calls
    cfg["entry_rules"] = [_tree("spy_trend_regime")]
    assert h._benchmark_calendar_for_run(_Raw("x"), cfg) == "CAL" and len(calls) == 1


def test_standalone_rerun_of_a_gated_row_still_gets_its_calendar():
    """A stored-row re-run carries the gate IN ITS RULES (entryActions), not as a launcher flag: the
    rebuilt config must still be recognised as gated so the handler builds the calendar and the
    re-run reproduces the stored numbers (a gated row without it would be refused, never silently
    ungated)."""
    from types import SimpleNamespace

    from app.services.backtest.daily_backtest_handler import _config_uses_benchmark_regime
    from app.services.backtest.rerun_handler import _build_standalone_rerun_config

    sp = {"universe": {"mode": "static", "symbols": ["AAPL"]}, "seed": 42,
          "fillModel": "next_bar_open", "entryActions": [_tree("spy_trend_regime")]}
    bt = SimpleNamespace(
        id=81, name="gated", expert_name="FMPRating", optimization_id=None,
        start_date=datetime(2024, 3, 1), end_date=datetime(2024, 3, 8),
        initial_capital=100_000.0, commission=1.0, slippage=0.0, strategy_params=sp)
    assert _config_uses_benchmark_regime(_build_standalone_rerun_config(bt))
    sp["entryActions"] = [{"op": "and", "children": []}]
    assert not _config_uses_benchmark_regime(_build_standalone_rerun_config(bt))
