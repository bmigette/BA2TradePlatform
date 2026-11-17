"""Two backtests running as THREADS of one process must each read their OWN market regime.

The serve master runs backtests in-process on task-queue threads (the re-run queue,
``BACKTEST_RERUN_WORKERS`` default 2; the main queue's ``daily_backtest`` tasks and a serial
in-process GA). The per-bar regime seams (``regime_overlay.get_stressed``,
``benchmark_regime.get_benchmark_regime`` / ``_failure``, and ``is_downtrend_now`` on top of it)
were process-global: run X read the flag run Y had just published for a different bar, and its
sizing, validation and entry gates then used the wrong regime.

The interleaving is FORCED, not timed: both runs' experts meet at a barrier once both engines
have published the bar's regime (the engine publishes before any expert runs), read the seam,
and meet again before either publishes the next bar. Run A's timeline is the opposite of run
B's on every bar, so with a shared seam exactly one of the two reads the other's value on every
bar, deterministically.
"""
from __future__ import annotations

import threading
from datetime import date, datetime

import pytest

from ba2_common.core import benchmark_regime as br
from ba2_common.core import market_regime as mr
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.regime_overlay import _as_date, get_stressed, is_downtrend_now
from ba2_common.core.types import OrderRecommendation, Recommendation

from app.services.backtest import price_source as ps_mod
from app.services.backtest.price_source import AsOfPriceSource, MemoizedOHLCVProvider
from tests.backtest.test_intraday_daily_knowability import DAILY, SYMBOL, _FakeDaily

SESSIONS = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]

#: Run A: stressed / calm / stressed and up / down / up. Run B: the exact opposite on every bar.
STRESS_A = {d: i % 2 == 0 for i, d in enumerate(SESSIONS)}
STRESS_B = {d: not v for d, v in STRESS_A.items()}
TREND_A = {d: (mr.TREND_UP if i % 2 == 0 else mr.TREND_DOWN) for i, d in enumerate(SESSIONS)}
TREND_B = {d: (mr.TREND_DOWN if t == mr.TREND_UP else mr.TREND_UP) for d, t in TREND_A.items()}


class _DayCalendar:
    """Stand-in for ``StressedCalendar`` / ``BenchmarkRegimeCalendar``: the engine only calls ``at``."""

    def __init__(self, by_day):
        self._by_day = by_day

    def at(self, as_of):
        return self._by_day[_as_date(as_of)]


def _benchmark(trend_by_day):
    return _DayCalendar({d: br.BenchmarkRegime(t, mr.NORMAL, "", "", d) for d, t in trend_by_day.items()})


class _LockstepProbe(MarketExpertInterface):
    """Reads the regime seams at every decision, in lockstep with the sibling run's probe."""

    barrier: threading.Barrier = None

    def __init__(self, id):
        super().__init__(id)
        self.records = []

    @classmethod
    def description(cls) -> str:
        return "lockstep regime probe"

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        self.barrier.wait()      # both engines have PUBLISHED this bar's regime
        regime = br.get_benchmark_regime()
        self.records.append((as_of.date(), get_stressed(), regime.trend if regime else None,
                             is_downtrend_now()))
        self.barrier.wait()      # both have READ before either engine publishes the next bar
        return Recommendation(signal=OrderRecommendation.HOLD, confidence=50.0, current_price=100.0,
                              details="probe", expected_profit_percent=0.0)


def _run(run_id, stress_by_day, trend_by_day, barrier):
    """One small synthetic daily backtest (3 sessions, 1 symbol) on THIS thread."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.seam_wiring import set_backtest_ohlcv_override, wire_backtest_seams
    from tests.backtest.test_max_loss_stop_engine import CFG

    resolver = wire_backtest_seams()
    with backtest_trading_db(f"regime-concurrent-{run_id}"):
        seed_account_definition(run_id, CFG)
        ruleset_id = seed_enter_long_ruleset()
        seed_expert_instance(account_id=run_id, expert_class_name="_LockstepProbe",
                             enter_market_ruleset_id=ruleset_id, instance_id=run_id)
        memo = MemoizedOHLCVProvider(_FakeDaily(), datetime(2023, 12, 1), datetime(2024, 1, 31),
                                     interval="1d")
        ps = AsOfPriceSource(ohlcv_provider=None, interval="1d")
        memo.bind_price_source(ps)
        ps.load_bars(SYMBOL, [{"Date": d, "Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1000}
                              for d, o, h, lo, c in DAILY if SESSIONS[0] <= d <= SESSIONS[-1]])
        account = BacktestAccount(run_id, ps, CFG)
        resolver.register_account(run_id, account)
        expert = _LockstepProbe(run_id)
        expert.barrier = barrier
        expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                              "enable_buy": (True, "bool")})
        resolver.register_expert(run_id, expert)
        set_backtest_ohlcv_override(memo)
        try:
            engine = DailyBacktestEngine(
                account=account, experts=[(expert, run_id, {}, ruleset_id)], price_source=ps,
                config={"start_date": datetime(2024, 1, 2), "end_date": datetime(2024, 1, 4, 23, 59),
                        "enabled_instruments": [SYMBOL], "seed": 42},
                indicator_provider=None,
                regime_calendar=_DayCalendar(stress_by_day),
                benchmark_calendar=_benchmark(trend_by_day), benchmark_regime_required=True)
            engine._indicator_provider = None
            engine.run()
        finally:
            set_backtest_ohlcv_override(None)
        return expert.records


def _expected(stress_by_day, trend_by_day):
    return [(d, stress_by_day[d], trend_by_day[d], trend_by_day[d] == mr.TREND_DOWN) for d in SESSIONS]


def _run_concurrently(specs):
    """Run each ``(run_id, stress, trend)`` on its own thread, in lockstep; return records per run id."""
    ps_mod.clear_ohlcv_memo()
    barrier = threading.Barrier(len(specs), timeout=60)
    out, errors = {}, []

    def _target(run_id, stress, trend):
        try:
            out[run_id] = _run(run_id, stress, trend, barrier)
        except BaseException as e:  # noqa: BLE001 -- handed to the main thread below
            errors.append(e)
            barrier.abort()

    threads = [threading.Thread(target=_target, args=spec, name=f"bt-run-{spec[0]}") for spec in specs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
        assert not t.is_alive(), f"{t.name} did not finish"
    if errors:
        raise errors[0]
    return out


@pytest.fixture(autouse=True)
def _clean_seams():
    from ba2_common.core.regime_overlay import reset_stressed
    reset_stressed()
    br.reset_benchmark_regime()
    yield
    reset_stressed()
    br.reset_benchmark_regime()


def test_two_concurrent_runs_each_read_their_own_regime_on_every_bar():
    got = _run_concurrently([(1301, STRESS_A, TREND_A), (1302, STRESS_B, TREND_B)])
    assert got[1301] == _expected(STRESS_A, TREND_A)
    assert got[1302] == _expected(STRESS_B, TREND_B)


def test_a_run_never_reads_or_writes_the_process_global_live_value():
    """A value published OUTSIDE any run (what live's TradeManager does) is neither read by a running
    backtest nor overwritten by it."""
    from ba2_common.core.regime_overlay import set_stressed

    live_regime = br.BenchmarkRegime(mr.TREND_PULLBACK, mr.CALM, "", "", date(2024, 1, 1))
    set_stressed(True)
    br.set_benchmark_regime(live_regime)
    br.set_benchmark_regime_failure(None)
    got = _run_concurrently([(1303, STRESS_A, TREND_A)])
    assert got[1303] == _expected(STRESS_A, TREND_A)
    assert get_stressed() is True
    assert br.get_benchmark_regime() is live_regime
