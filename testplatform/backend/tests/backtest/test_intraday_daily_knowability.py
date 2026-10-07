"""No daily-bar reader may return a bar whose session is not finished at the decision instant.

THE DEFECT (confirmed 2026-10-06 on real runs). Classic stock GA backtests run on a 5-minute
clock: the decision bar is 09:30 America/New_York, the order fills at the next bar's open. Daily
bars are stamped at midnight and daily reads were sliced ``<= as_of``, so at the 09:30 decision
EVERY daily reader got the decision day's OWN finished bar (close, high, low, volume). Live at
09:30 only has the PRIOR session's bar. Effect on stored rows: EarningsDrift row 1088
+375% -> +37%, InsiderClusterBuy +158% -> +106%.

THE RULE (``AsOfPriceSource.knowable_daily_end`` / ``daily_session_date`` / ``decision_price``,
one place, ``prior_session_v1``): at an intraday decision on exchange-local date D, daily data is
knowable through the last regular session BEFORE D, and the price is the decision bar's OPEN.
On the DAILY clock the bar stamped D decides with D's close and fills at D+1's open; that is
not a look-ahead, and these tests pin that it did not move.

This is the engine-level guard: a full ``DailyBacktestEngine.run()`` on a 5-minute clock whose
stub expert reads daily bars through every access path the real experts use, and records the
newest bar each one returned. Each assertion below FAILS on the code before the fix (the
decision day's own bar came back).

Run from the backend dir:
    python -m pytest tests/backtest/test_intraday_daily_knowability.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from ba2_common.core.backtest_context import knowable_daily_end
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation

from app.services.backtest import price_source as ps_mod
from app.services.backtest.price_source import (
    AsOfClampedOHLCVProvider, AsOfPriceSource, MemoizedOHLCVProvider)

SYMBOL = "AAPL"
#: Regular sessions around the run. 2024-01-01 is a holiday: the session before Tue 2024-01-02 is
#: Fri 2023-12-29.
DAILY = [  # (date, open, high, low, close) -- every close distinct and never equal to an intraday price
    (date(2023, 12, 27), 90.0, 91.0, 89.0, 90.5),
    (date(2023, 12, 28), 91.0, 92.0, 90.0, 91.5),
    (date(2023, 12, 29), 92.0, 93.0, 91.0, 92.5),
    (date(2024, 1, 2), 93.0, 99.0, 92.0, 98.25),     # a FINISHED bar the 09:30 decision must not see
    (date(2024, 1, 3), 94.0, 100.0, 93.0, 97.75),
    (date(2024, 1, 4), 95.0, 101.0, 94.0, 96.125),
    (date(2024, 1, 5), 96.0, 102.0, 95.0, 99.5),
]
#: The 5-minute bars carry EXCHANGE-LOCAL wall-clock stamps (09:30 = the New York open) in the
#: engine's naive "UTC" keys: that is why the default schedule time "09:30" matches the open bar.
#: Only the 09:30 bar of each session is a scheduled decision; 09:35/09:40 give the price source
#: later bars so the open/close of the decision bar and the next bar are distinguishable.
SESSIONS = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
MINUTES = [(9, 30), (9, 35), (9, 40)]


def _intraday_rows():
    rows, px = [], 100.0
    for d in SESSIONS:
        for h, m in MINUTES:
            # open != close on purpose, and no price equals any daily close
            rows.append({"Date": datetime(d.year, d.month, d.day, h, m),
                         "Open": px, "High": px + 0.5, "Low": px - 0.5, "Close": px + 0.3,
                         "Volume": 1000})
            px += 0.7
    return rows


class _FakeDaily:
    """In-memory stand-in for the inner OHLCV provider (no network, no cache dir)."""

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        assert interval == "1d"
        return pd.DataFrame({
            "Date": [pd.Timestamp(d) for d, *_ in DAILY],
            "Open": [r[1] for r in DAILY], "High": [r[2] for r in DAILY],
            "Low": [r[3] for r in DAILY], "Close": [r[4] for r in DAILY],
            "Volume": [1000] * len(DAILY)})


class _ProbeExpert(MarketExpertInterface):
    """Reads daily bars every way the shipped experts do and records the newest bar of each."""

    def __init__(self, id, price_source):
        super().__init__(id)
        self._ps = price_source
        self.records = []

    @classmethod
    def description(cls) -> str:
        return "knowability probe"

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        from ba2_experts.DeterministicScorer import data as ds_data

        providers = context.providers
        ohlcv = providers.ohlcv()
        far = datetime(2030, 1, 1, tzinfo=timezone.utc)

        def last(df):
            return None if df is None or not len(df) else pd.Timestamp(df["Date"].iloc[-1]).date()

        rec = {"as_of": as_of}
        # 1. an expert / condition that passes the decision instant as the end (EarningsDrift,
        #    InsiderClusterBuy, the data conditions of the ruleset)
        rec["explicit_end"] = last(ohlcv.get_ohlcv_data(SYMBOL, end_date=as_of, interval="1d"))
        # 2. the indicator / ATR path: wall-clock end, capped at the clock by the wrapper
        rec["clamped_none_end"] = last(
            AsOfClampedOHLCVProvider(ohlcv, self._ps).get_ohlcv_data(SYMBOL, interval="1d"))
        # 3. DeterministicScorer's real reader (whole series cached for the run, sliced per bar)
        rec["ds_fetch_ohlcv"] = last(ds_data.fetch_ohlcv(providers, SYMBOL, as_of))
        # 4. the whole-series (bulk) read + the cutoff hook a caching reader must slice with
        bulk = ohlcv.get_ohlcv_data(SYMBOL, start_date=as_of - timedelta(days=30), end_date=far,
                                    interval="1d")
        cut = pd.Timestamp(knowable_daily_end(providers, as_of)).tz_localize(None) \
            if pd.Timestamp(knowable_daily_end(providers, as_of)).tzinfo else \
            pd.Timestamp(knowable_daily_end(providers, as_of))
        rec["bulk_sliced"] = last(bulk[pd.to_datetime(bulk["Date"]) <= cut])
        rec["bulk_unsliced"] = last(bulk)   # documented: the CALLER slices a bulk read
        # 5. the price every expert's current_price comes from
        rec["price_at_date"] = providers.price_at_date(SYMBOL, as_of)
        self.records.append(rec)
        return Recommendation(signal=OrderRecommendation.HOLD, confidence=50.0,
                              current_price=float(self._ps.close_at(SYMBOL, as_of)),
                              details="probe", expected_profit_percent=0.0)


def _run(interval: str, run_id: int):
    """Full engine.run() with the probe expert; returns (records, price_source)."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.seam_wiring import set_backtest_ohlcv_override, wire_backtest_seams
    from tests.backtest.test_max_loss_stop_engine import CFG

    ps_mod.clear_ohlcv_memo()
    from ba2_experts.DeterministicScorer import data as ds_data
    ds_data._OHLCV_COVERAGE.clear()
    ds_data._OHLCV_VIEWS.clear()

    account_id = expert_id = run_id
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"knowability-{run_id}")
    ctx.__enter__()
    try:
        seed_account_definition(account_id, CFG)
        ruleset_id = seed_enter_long_ruleset()
        seed_expert_instance(account_id=account_id, expert_class_name="_ProbeExpert",
                             enter_market_ruleset_id=ruleset_id, instance_id=expert_id)
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
        account = BacktestAccount(account_id, ps, CFG)
        resolver.register_account(account_id, account)
        expert = _ProbeExpert(expert_id, ps)
        expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                              "enable_buy": (True, "bool")})
        resolver.register_expert(expert_id, expert)
        set_backtest_ohlcv_override(memo)
        try:
            engine = DailyBacktestEngine(
                account=account, experts=[(expert, expert_id, {}, ruleset_id)], price_source=ps,
                config={"start_date": datetime(2024, 1, 2), "end_date": datetime(2024, 1, 4, 23, 59),
                        "enabled_instruments": [SYMBOL], "seed": 42},
                indicator_provider=None)
            engine._indicator_provider = None
            engine.run()
        finally:
            set_backtest_ohlcv_override(None)
        return expert.records, ps
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# intraday clock: every reader sees the PRIOR session
# --------------------------------------------------------------------------- #

PRIOR = {date(2024, 1, 2): date(2023, 12, 29), date(2024, 1, 3): date(2024, 1, 2),
         date(2024, 1, 4): date(2024, 1, 3)}
#: the readers whose result must be prior-session on an intraday clock
KNOWABLE_READERS = ("explicit_end", "clamped_none_end", "ds_fetch_ohlcv", "bulk_sliced")


@pytest.fixture(scope="module")
def intraday_run():
    return _run("5min", 701)


def test_every_decision_was_recorded(intraday_run):
    records, _ = intraday_run
    assert len(records) == len(SESSIONS)   # one scheduled 09:30 decision per session


@pytest.mark.parametrize("reader", KNOWABLE_READERS)
def test_no_daily_reader_sees_the_decision_sessions_own_bar(intraday_run, reader):
    """THE guard: at every intraday decision the newest daily bar is the PRIOR session's."""
    records, _ = intraday_run
    for rec in records:
        decision_day = rec["as_of"].date()
        assert rec[reader] == PRIOR[decision_day], (
            f"{reader} returned the {rec[reader]} bar at the {rec['as_of']} decision: "
            f"only {PRIOR[decision_day]} is a finished session at that instant")


def test_a_bulk_read_is_the_callers_to_slice(intraday_run):
    """A whole-series read (end past the clock) is returned whole -- caching readers slice it with
    ``knowable_daily_end``. Pinned so nobody 'fixes' it by clamping and freezing DeterministicScorer's
    run-long cache at its first bar."""
    records, _ = intraday_run
    assert all(rec["bulk_unsliced"] == date(2024, 1, 5) for rec in records)


def test_price_at_date_is_the_decision_bars_open(intraday_run):
    """The decision price is the OPEN of the bar stamped at the decision (what a live quote at
    09:30 returns), not that bar's close (printed a bar later, when the order fills) and not any
    daily close."""
    records, ps = intraday_run
    for rec in records:
        bar = ps.bar_at(SYMBOL, rec["as_of"])
        assert rec["price_at_date"] == pytest.approx(bar["open"])
        assert rec["price_at_date"] != pytest.approx(bar["close"])
        assert rec["price_at_date"] not in {r[4] for r in DAILY}


def test_unclamped_run_is_the_defect(monkeypatch):
    """Control: with the memo's price source unbound (the pre-fix behaviour) the same run DOES
    return the decision session's own bar -- so the assertions above are not vacuous."""
    monkeypatch.setattr(MemoizedOHLCVProvider, "bind_price_source", lambda self, ps: None)
    records, _ = _run("5min", 702)
    leaked = [r for r in records if r["explicit_end"] == r["as_of"].date()]
    assert leaked, "an unbound memo should still serve the same-session bar (the defect)"


# --------------------------------------------------------------------------- #
# daily clock: the convention is DIFFERENT and must not move
# --------------------------------------------------------------------------- #

def test_daily_clock_decides_on_the_days_own_close():
    """``execution_interval=1d``: the bar stamped D decides with D's close and fills at D+1's open.
    That equals live deciding before D+1's open; it is NOT a look-ahead and is unchanged."""
    records, ps = _run("1d", 703)
    assert len(records) == len(SESSIONS)
    close = {d: c for d, _o, _h, _l, c in DAILY}
    for rec in records:
        d = rec["as_of"].date()
        assert rec["explicit_end"] == d
        assert rec["clamped_none_end"] == d
        assert rec["ds_fetch_ohlcv"] == d
        assert rec["bulk_sliced"] == d
        assert rec["price_at_date"] == pytest.approx(close[d])


# --------------------------------------------------------------------------- #
# the rule itself, and the per-day stores that share it
# --------------------------------------------------------------------------- #

def _ps(interval):
    return AsOfPriceSource(ohlcv_provider=None, interval=interval)


@pytest.mark.parametrize("decision, session", [
    (datetime(2024, 1, 3, 9, 30, tzinfo=timezone.utc), date(2024, 1, 2)),    # Wed 09:30 -> Tue
    (datetime(2024, 1, 2, 9, 30, tzinfo=timezone.utc), date(2023, 12, 29)),  # Tue after a holiday Monday -> Fri
    (datetime(2024, 1, 8, 9, 30, tzinfo=timezone.utc), date(2024, 1, 5)),    # Monday -> Friday
    (datetime(2024, 1, 3, 14, 30, tzinfo=timezone.utc), date(2024, 1, 2)),   # true UTC stamp of the same open
    (datetime(2024, 7, 9, 13, 30, tzinfo=timezone.utc), date(2024, 7, 8)),   # EDT 09:30 as UTC
])
def test_intraday_session_date(decision, session):
    ps = _ps("5min")
    assert ps.daily_session_date(decision) == session
    end = ps.knowable_daily_end(decision)
    assert end.date() == session and end < decision


def test_daily_clock_session_date_is_the_bars_own_date():
    ps = _ps("1d")
    as_of = datetime(2024, 1, 3, tzinfo=timezone.utc)
    assert ps.daily_session_date(as_of) == date(2024, 1, 3)
    assert ps.knowable_daily_end(as_of) == as_of


def test_screener_scan_day_is_the_prior_session_on_an_intraday_clock(tmp_path):
    """The metric store's row for day S is built from S's finished bar: a 09:30 decision on S
    must read the row of the session before it."""
    import pandas as pd_
    from ba2_providers.screener import metric_store as ms
    from app.services.backtest.daily_engine import _screened_symbols_for_bar

    as_of = datetime(2024, 1, 3, 14, 30, tzinfo=timezone.utc)
    seen = {}

    class _Store:
        pass

    orig_load, orig_dates, orig_screen = ms.load_store, ms.scan_dates, ms.screen_universe_for_day
    ms.load_store = lambda store: _Store()
    ms.scan_dates = lambda df, store_key=None: ["2024-01-02", "2024-01-03"]

    def _screen(df, day, settings, excluded=None):
        seen["day"] = day
        return [SYMBOL]

    ms.screen_universe_for_day = _screen
    try:
        rt = {"store": "x", "settings": {}}
        ps = _ps("5min")
        _screened_symbols_for_bar(rt, as_of, None, session_date=ps.daily_session_date(as_of))
        assert seen["day"] == "2024-01-02", "an intraday 09:30 decision read its own day's scan row"
        _screened_symbols_for_bar(rt, as_of, None)   # legacy callers: the bar's own date
        assert seen["day"] == "2024-01-03"
    finally:
        ms.load_store, ms.scan_dates, ms.screen_universe_for_day = orig_load, orig_dates, orig_screen


def test_metric_store_atr_reads_the_knowable_session(monkeypatch):
    from ba2_providers.screener import metric_store as ms
    from app.services.backtest.seam_wiring import MetricStoreATRProvider

    asked = {}
    monkeypatch.setattr(ms, "load_store", lambda d: object())
    monkeypatch.setattr(ms, "metrics_as_of", lambda df, day, cols: asked.setdefault("day", day) and {})
    as_of = datetime(2024, 1, 3, 14, 30, tzinfo=timezone.utc)
    MetricStoreATRProvider("x", session_date_fn=_ps("5min").daily_session_date).get_indicator(
        SYMBOL, "atr", end_date=as_of, period=ms.ATR_PERIODS[0])
    assert asked["day"] == "2024-01-02"


def test_regime_calendar_lookup_day_is_the_prior_session_on_an_intraday_clock():
    from ba2_common.core.regime_overlay import StressedCalendar

    cal = StressedCalendar([date(2024, 1, 2) + timedelta(days=i) for i in range(4)], [100, 101, 102, 103])
    cal.at(date(2024, 1, 3))   # smoke: accepts the session date the engine now passes
    as_of = datetime(2024, 1, 3, 14, 30, tzinfo=timezone.utc)
    assert _ps("5min").daily_session_date(as_of) == date(2024, 1, 2)
    assert _ps("1d").daily_session_date(as_of) == date(2024, 1, 3)
