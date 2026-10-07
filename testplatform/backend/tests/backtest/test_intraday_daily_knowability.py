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
MINUTES = [(9, 30), (9, 35), (9, 40), (9, 45)]


def _intraday_rows():
    rows, px = [], 100.0
    # the session BEFORE the run supplies the price knowable at the first decision of the first day
    for d in (date(2023, 12, 29), *SESSIONS):
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
        # any other end shape is clamped BY DEFAULT: the far future, the end of the decision day, None
        rec["far_end"] = last(ohlcv.get_ohlcv_data(SYMBOL, start_date=as_of - timedelta(days=30),
                                                   end_date=far, interval="1d"))
        rec["eod_end"] = last(ohlcv.get_ohlcv_data(
            SYMBOL, end_date=datetime.combine(as_of.date(), datetime.max.time(), tzinfo=timezone.utc),
            interval="1d"))
        rec["none_end"] = last(ohlcv.get_ohlcv_data(SYMBOL, interval="1d"))
        # the EXPLICIT opt-out: the unsliced series, which the caller slices with the hook
        bulk = ohlcv.get_ohlcv_data_unsliced(SYMBOL, start_date=as_of - timedelta(days=30),
                                             end_date=far, interval="1d")
        cut = pd.Timestamp(knowable_daily_end(providers, as_of))
        cut = cut.tz_localize(None) if cut.tzinfo else cut
        rec["bulk_sliced"] = last(bulk[pd.to_datetime(bulk["Date"]) <= cut])
        rec["bulk_unsliced"] = last(bulk)
        # 5. the price every expert's current_price comes from
        rec["price_at_date"] = providers.price_at_date(SYMBOL, as_of)
        self.records.append(rec)
        return Recommendation(signal=OrderRecommendation.HOLD, confidence=50.0,
                              current_price=float(self._ps.close_at(SYMBOL, as_of)),
                              details="probe", expected_profit_percent=0.0)


def _run(interval: str, run_id: int, times=None):
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
                        "enabled_instruments": [SYMBOL], "seed": 42,
                        **({"run_schedule_override": {"days": {d: True for d in (
                            "monday", "tuesday", "wednesday", "thursday", "friday")}, "times": list(times)}}
                           if times else {})},
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
KNOWABLE_READERS = ("explicit_end", "clamped_none_end", "ds_fetch_ohlcv", "bulk_sliced",
                    "far_end", "eod_end", "none_end")


#: the schedule time of a decision: the session's FIRST bar (a stored row's 09:30), one bar later,
#: and two bars later. The daily-bar rule must be the same at each.
DECISION_TIMES = ["09:30", "09:35", "09:40", "09:45"]


@pytest.fixture(scope="module", params=DECISION_TIMES)
def intraday_run(request):
    records, ps = _run("5min", 700 + DECISION_TIMES.index(request.param), times=[request.param])
    return records, ps, request.param


def test_every_decision_was_recorded(intraday_run):
    records, _, hhmm = intraday_run
    assert len(records) == len(SESSIONS)   # one scheduled decision per session
    assert all(r["as_of"].strftime("%H:%M") == hhmm for r in records)


@pytest.mark.parametrize("reader", KNOWABLE_READERS)
def test_no_daily_reader_sees_the_decision_sessions_own_bar(intraday_run, reader):
    """THE guard: at every intraday decision the newest daily bar is the PRIOR session's."""
    records, _, _ = intraday_run
    for rec in records:
        decision_day = rec["as_of"].date()
        assert rec[reader] == PRIOR[decision_day], (
            f"{reader} returned the {rec[reader]} bar at the {rec['as_of']} decision: "
            f"only {PRIOR[decision_day]} is a finished session at that instant")


def test_only_the_explicit_unsliced_read_returns_the_whole_series(intraday_run):
    """The clamp is the DEFAULT for every shape of ``end_date``; the whole series comes back only from
    the explicit opt-out ``get_ohlcv_data_unsliced`` (DeterministicScorer's cache), which the caller
    slices with ``knowable_daily_end``."""
    records, _, _ = intraday_run
    assert all(rec["bulk_unsliced"] == date(2024, 1, 5) for rec in records)
    assert all(rec["far_end"] != date(2024, 1, 5) for rec in records)


def test_price_at_date_is_the_close_of_the_latest_bar_that_has_ended(intraday_run):
    """The decision price is the close of the latest bar that ENDED at or before the decision --
    never the decision bar's own close (not printed yet), never a daily close -- with NO special
    case at the session open: on the first bar of a session it is the PREVIOUS session's last bar
    (and nothing at all on the first bar of the data)."""
    records, ps, hhmm = intraday_run
    by_stamp = {r["Date"]: r for r in _intraday_rows()}
    for rec in records:
        t = rec["as_of"].replace(tzinfo=None)
        ended = [r for stamp, r in by_stamp.items() if stamp + timedelta(minutes=5) <= t]
        expected = ended[-1]["Close"] if ended else None
        assert rec["price_at_date"] == expected, (rec["as_of"], rec["price_at_date"], expected)
        if rec["price_at_date"] is not None:
            assert rec["price_at_date"] not in {r[4] for r in DAILY}
            assert rec["price_at_date"] != by_stamp[t]["Close"]


def test_a_decision_on_the_first_bar_warns_and_a_later_one_does_not(monkeypatch):
    """One WARNING per run when a schedule time equals the first bar of a session; none otherwise."""
    from app.services.backtest import daily_engine

    seen = []
    monkeypatch.setattr(daily_engine.logger, "warning", lambda msg, *a, **k: seen.append(str(msg)))
    _run("5min", 710, times=["09:30"])
    assert sum("first bar of a session" in m for m in seen) == 1
    seen.clear()
    _run("5min", 711, times=["09:40"])
    assert not any("first bar of a session" in m for m in seen)


def test_an_intraday_run_with_an_unbound_reader_refuses_to_start(monkeypatch):
    """Control + guard: a memo not bound to the run's price source is exactly the pre-fix defect (daily
    reads return the decision session's own bar). The engine refuses to run instead of serving it."""
    monkeypatch.setattr(MemoizedOHLCVProvider, "bind_price_source", lambda self, ps: None)
    with pytest.raises(RuntimeError, match="not bound"):
        _run("5min", 702)


@pytest.mark.parametrize("hhmm", ["09:30", "10:00", "15:55"])
@pytest.mark.parametrize("shape", ["none", "decision", "eod", "now", "far"])
def test_fuzz_no_end_date_shape_returns_anything_newer_than_the_cutoff(hhmm, shape):
    """Reviewer's fuzz: end_date in {None, as_of, end of the as_of day, wall-clock now, far future} x
    decision times -> nothing newer than the knowable cutoff, unless the unsliced read is explicit."""
    ps = _ps("5min")
    memo = MemoizedOHLCVProvider(_FakeDaily(), datetime(2023, 12, 1), datetime(2024, 1, 31), interval="5min")
    memo.bind_price_source(ps)
    h, m = int(hhmm[:2]), int(hhmm[3:])
    t = _wall(2024, 1, 3, h, m)
    ps.set_clock(t)
    end = {"none": None, "decision": t, "eod": datetime.combine(t.date(), datetime.max.time(), tzinfo=timezone.utc),
           "now": datetime.now(timezone.utc), "far": datetime(2035, 1, 1, tzinfo=timezone.utc)}[shape]
    df = memo.get_ohlcv_data(SYMBOL, end_date=end, interval="1d")
    assert pd.Timestamp(df["Date"].iloc[-1]).date() == date(2024, 1, 2)       # 2024-01-03 not finished
    full = memo.get_ohlcv_data_unsliced(SYMBOL, end_date=end, interval="1d")
    assert pd.Timestamp(full["Date"].iloc[-1]).date() > date(2024, 1, 2)       # explicit opt-out only


def test_while_the_intraday_clock_is_on_a_missing_hook_raises(monkeypatch):
    """No silent fallbacks: with the intraday decision clock on, an unbound reader, a provider without
    the hook and a read before the first tick all RAISE."""
    from ba2_common.core.knowability import intraday_decisions

    memo = MemoizedOHLCVProvider(_FakeDaily(), datetime(2023, 12, 1), datetime(2024, 1, 31), interval="5min")

    class _P:
        def __init__(self, o): self._o = o
        def ohlcv(self): return self._o

    with intraday_decisions(True):
        with pytest.raises(RuntimeError):
            memo.knowable_daily_end(_wall(2024, 1, 3, 10, 0))
        with pytest.raises(RuntimeError):
            memo.get_ohlcv_data(SYMBOL, interval="1d")
        with pytest.raises(RuntimeError):
            knowable_daily_end(_P(_FakeDaily()), _wall(2024, 1, 3, 10, 0))
        memo.bind_price_source(_ps("5min"))              # bound but the clock was never set
        with pytest.raises(RuntimeError):
            memo.get_ohlcv_data(SYMBOL, interval="1d")
    # flag off (live / daily clock): the identity, as before
    assert knowable_daily_end(_P(_FakeDaily()), _wall(2024, 1, 3, 10, 0)) == _wall(2024, 1, 3, 10, 0)


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


def _wall(y, m, d, hh, mm):
    """A decision instant as the engine labels it: exchange-local wall time, tagged UTC."""
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


@pytest.mark.parametrize("interval", ["1min", "5min", "15min", "1h"])
@pytest.mark.parametrize("decision, session", [
    # the session's first instant, mid-session, the last bar, the close itself, after it
    (_wall(2024, 1, 3, 9, 30), date(2024, 1, 2)),
    (_wall(2024, 1, 3, 11, 0), date(2024, 1, 2)),
    (_wall(2024, 1, 3, 15, 55), date(2024, 1, 2)),      # 5 minutes before the close: today's bar is NOT done
    (_wall(2024, 1, 3, 16, 0), date(2024, 1, 3)),       # at the close it is
    (_wall(2024, 1, 3, 16, 5), date(2024, 1, 3)),
    (_wall(2024, 1, 3, 23, 0), date(2024, 1, 3)),
    (_wall(2024, 1, 3, 8, 0), date(2024, 1, 2)),        # pre-market
    # calendar edges
    (_wall(2024, 1, 2, 9, 30), date(2023, 12, 29)),     # a holiday Monday precedes
    (_wall(2024, 1, 8, 9, 30), date(2024, 1, 5)),       # Monday sees Friday
    (_wall(2024, 1, 6, 12, 0), date(2024, 1, 5)),       # Saturday
    (_wall(2024, 7, 9, 9, 30), date(2024, 7, 8)),       # summer time
    # HALF DAY (Fri 2023-11-24 closes 13:00; Thu 11-23 is Thanksgiving): the calendar's close
    # decides, not a fixed 16:00
    (_wall(2023, 11, 24, 12, 55), date(2023, 11, 22)),
    (_wall(2023, 11, 24, 13, 0), date(2023, 11, 24)),
    (_wall(2023, 11, 24, 14, 0), date(2023, 11, 24)),
])
def test_intraday_session_date(decision, session, interval):
    """Daily history at T = the sessions FINISHED at or before T, for any T and any bar length."""
    ps = _ps(interval)
    assert ps.daily_session_date(decision) == session
    end = ps.knowable_daily_end(decision)
    assert end.date() == session and end.replace(hour=0, minute=0, second=0, microsecond=0) <= decision


def _day_bars(d, first=(9, 30), minutes=390, step=5, base=100.0):
    """One session of bars stamped at their START; open and close differ so a read of the wrong
    one is visible: open = base + n, close = base + n + 0.5."""
    rows, t = [], datetime(d.year, d.month, d.day, *first)
    for n in range(minutes // step):
        rows.append({"Date": t, "Open": base + n, "High": base + n + 1, "Low": base + n - 1,
                     "Close": base + n + 0.5, "Volume": 10})
        t += timedelta(minutes=step)
    return rows


@pytest.mark.parametrize("interval, step", [("1min", 1), ("5min", 5), ("15min", 15), ("1h", 60)])
def test_decision_price_rule_at_any_time_and_interval(interval, step):
    """PRICE at T = the close of the latest bar that has ENDED at or before T. ONE rule at the open,
    mid-session, on the last bar and after the close: no opening-print special case anywhere."""
    ps = _ps(interval)
    rows = _day_bars(date(2024, 1, 2), step=step, base=50.0) + _day_bars(date(2024, 1, 3), step=step, base=100.0)
    ps.load_bars(SYMBOL, rows)
    n_day = 390 // step
    day3 = rows[n_day:]

    def at(hh, mm):
        return ps.decision_price(SYMBOL, _wall(2024, 1, 3, hh, mm))

    # the session's first bar: the PREVIOUS session's last bar -- NOT the opening print of this one
    assert at(9, 30) == rows[n_day - 1]["Close"] != day3[0]["Open"]
    # one bar later: the first bar has ended, and its CLOSE (not its open) is the price
    nxt = day3[1]["Date"]
    assert at(nxt.hour, nxt.minute) == day3[0]["Close"] != day3[0]["Open"]
    # mid-session: the close of the bar that ENDED at T (stamped T - step), NOT anything of the
    # bar stamped T (which has not ended)
    idx = 3
    mid = day3[idx]["Date"]
    assert at(mid.hour, mid.minute) == day3[idx - 1]["Close"]
    assert at(mid.hour, mid.minute) not in (day3[idx]["Open"], day3[idx]["Close"])
    # the last bar: the previous bar's close, not its own
    last = day3[-1]
    assert at(last["Date"].hour, last["Date"].minute) == day3[-2]["Close"]
    # after the close: the last bar has ended
    assert at(16, 5) == last["Close"]
    assert at(23, 0) == last["Close"]
    # pre-market: nothing of today's session has printed; the previous session's last close
    assert at(8, 0) == rows[n_day - 1]["Close"]


def test_decision_price_does_not_depend_on_where_the_session_starts():
    """A symbol whose first bar is stamped LATE (thin name) follows the same rule: until a bar of
    today has ended the price is the previous session's last close, then the latest ended bar's
    close. Nothing in the rule is tied to 09:30; if anyone hard-codes the open or a time the
    assertions below move."""
    ps = _ps("5min")
    prev = _day_bars(date(2024, 1, 2), base=50.0)
    late = _day_bars(date(2024, 1, 3), first=(10, 0), minutes=300, base=100.0)
    ps.load_bars(SYMBOL, prev + late)
    assert ps.decision_price(SYMBOL, _wall(2024, 1, 3, 9, 30)) == prev[-1]["Close"]
    assert ps.decision_price(SYMBOL, _wall(2024, 1, 3, 10, 0)) == prev[-1]["Close"]
    assert ps.decision_price(SYMBOL, _wall(2024, 1, 3, 10, 5)) == late[0]["Close"]


def test_decision_price_is_none_when_nothing_is_knowable():
    ps = _ps("5min")
    ps.load_bars(SYMBOL, _day_bars(date(2024, 1, 3)))
    assert ps.decision_price(SYMBOL, _wall(2024, 1, 3, 8, 0)) is None
    assert ps.decision_price("NOPE", _wall(2024, 1, 3, 9, 30)) is None


def test_the_fill_is_still_the_next_bars_open():
    """Unchanged: the order of a decision on the bar stamped T fills at the open of the NEXT bar,
    so the decision price (T's opening print / the previous bar's close) and the fill price never
    come from the same instant."""
    ps = _ps("5min")
    rows = _day_bars(date(2024, 1, 3))
    ps.load_bars(SYMBOL, rows)
    nb = ps.next_bar(SYMBOL, _wall(2024, 1, 3, 9, 30))
    assert nb["open"] == rows[1]["Open"]
    assert ps.decision_price(SYMBOL, _wall(2024, 1, 3, 9, 30)) is None   # nothing has ended yet


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


# --------------------------------------------------------------------------- #
# a thin symbol with a GAP in its bars at T is still decided, and filled at its next bar
# --------------------------------------------------------------------------- #

def _thin_ps():
    """AAPL prints every 5 minutes; THIN prints at 09:30, 09:35 and then not until 09:55; STALE's last
    bar is three sessions before the run."""
    ps = _ps("5min")
    ps.load_bars("AAPL", _day_bars(date(2024, 1, 3), minutes=120, base=100.0))
    thin = [r for r in _day_bars(date(2024, 1, 3), minutes=120, base=20.0)
            if r["Date"].strftime("%H:%M") in ("09:30", "09:35", "09:55", "10:00")]
    ps.load_bars("THIN", thin)
    ps.load_bars("STALE", _day_bars(date(2023, 12, 27), minutes=60, base=5.0))
    return ps, thin


def test_a_symbol_without_a_bar_at_T_is_still_decidable():
    from app.services.backtest.daily_engine import resolve_universe

    ps, thin = _thin_ps()
    cfg = {"enabled_instruments": ["AAPL", "THIN", "STALE", "NODATA"]}
    t = _wall(2024, 1, 3, 9, 45)                    # no THIN bar is stamped 09:45
    assert ps.bar_at("THIN", t) is None
    ps.set_clock(t)
    assert resolve_universe(t, cfg, ps) == ["AAPL", "THIN"]         # decidable; stale/no data are not
    # its price is the close of its latest ENDED bar (09:35, ended 09:40), not a stale or future print
    assert ps.decision_price("THIN", t) == thin[1]["Close"]


def test_a_stale_symbol_is_not_decidable_but_the_prior_session_is_fine():
    ps, _ = _thin_ps()
    ps.load_bars("PRIOR", _day_bars(date(2024, 1, 2), minutes=390, base=7.0))
    assert ps.decision_price("STALE", _wall(2024, 1, 3, 10, 0)) is None
    assert ps.decision_price("PRIOR", _wall(2024, 1, 3, 9, 30)) is not None


def test_the_account_prices_a_decision_with_the_knowable_price():
    from app.services.backtest.backtest_account import BacktestAccount
    from tests.backtest.test_max_loss_stop_engine import CFG

    ps, thin = _thin_ps()
    t = _wall(2024, 1, 3, 9, 45)
    ps.set_clock(t)
    acct = BacktestAccount(998, ps, CFG)
    assert acct.get_instrument_current_price("THIN") == thin[1]["Close"]
    aapl = _day_bars(date(2024, 1, 3), minutes=120, base=100.0)
    # AAPL: the bar stamped 09:40 (the last that ended by 09:45), NOT the 09:45 bar's own close
    assert acct.get_instrument_current_price("AAPL") == aapl[2]["Close"]
    with pytest.raises(ValueError):
        acct.get_instrument_current_price("STALE")


def test_a_market_order_on_a_thin_symbol_fills_at_its_next_bar_whenever_that_opens():
    from app.services.backtest.backtest_account import BacktestAccount
    from tests.backtest.test_max_loss_stop_engine import CFG

    ps, thin = _thin_ps()
    t = _wall(2024, 1, 3, 9, 45)
    ps.set_clock(t)
    acct = BacktestAccount(997, ps, CFG)
    nb = ps.next_bar("THIN", t)
    assert nb["open"] == thin[2]["Open"]          # the 09:55 print: ten minutes later, still THE next open

    class _O:  # the minimal order shape _bar_for_fill reads
        symbol = "THIN"

    assert acct._bar_for_fill(_O(), t)["open"] == thin[2]["Open"]


# --------------------------------------------------------------------------- #
# I1: decision-time marks use the decision price; the recorded curve keeps the bar close
# --------------------------------------------------------------------------- #

def _marked_account(acct_id):
    from app.services.backtest.backtest_account import BacktestAccount
    from tests.backtest.test_max_loss_stop_engine import CFG

    ps = _ps("5min")
    rows = _day_bars(date(2024, 1, 3), minutes=120, base=100.0)
    ps.load_bars("AAPL", rows)
    t = _wall(2024, 1, 3, 10, 0)
    ps.set_clock(t)
    acct = BacktestAccount(acct_id, ps, CFG)
    acct._update_position("AAPL", 10, 100.0)
    return acct, ps, rows, t


def test_equity_for_sizing_marks_at_the_decision_price_not_the_bar_close():
    acct, ps, rows, t = _marked_account(996)
    wall = t.replace(tzinfo=None)
    ended = max(r["Date"] for r in rows if r["Date"] + timedelta(minutes=5) <= wall)
    dec = next(r["Close"] for r in rows if r["Date"] == ended)
    clock_close = next(r["Close"] for r in rows if r["Date"] == wall)
    assert dec != clock_close
    assert acct.equity() - acct._cash == pytest.approx(10 * dec)                          # decision read
    assert acct.equity(close_mark=True) - acct._cash == pytest.approx(10 * clock_close)   # recorded value
    assert acct.snapshot_equity(t)["equity_value"] == pytest.approx(10 * clock_close)     # the curve keeps the close
    assert acct.get_positions()[0]["current_price"] == pytest.approx(dec)                 # rules read the decision price


def test_daily_clock_marks_are_unchanged():
    from app.services.backtest.backtest_account import BacktestAccount
    from tests.backtest.test_max_loss_stop_engine import CFG

    ps = _ps("1d")
    ps.load_bars(SYMBOL, [{"Date": d, "Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1}
                          for d, o, h, lo, c in DAILY if d <= date(2024, 1, 4)])
    ps.set_clock(datetime(2024, 1, 3, tzinfo=timezone.utc))
    acct = BacktestAccount(995, ps, CFG)
    acct._update_position(SYMBOL, 10, 100.0)
    assert acct.equity() - acct._cash == pytest.approx(10 * 97.75)
    assert acct.equity(close_mark=True) == acct.equity()


# --------------------------------------------------------------------------- #
# I3: an equity MARKET entry whose next bar is in another session expires (DAY order), counted
# --------------------------------------------------------------------------- #

def test_a_market_entry_whose_next_bar_is_days_away_expires_instead_of_filling(monkeypatch):
    from types import SimpleNamespace
    from app.services.backtest import backtest_account as BA
    from app.services.backtest.backtest_account import BacktestAccount
    from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus, OrderType
    from tests.backtest.test_max_loss_stop_engine import CFG

    ps = _ps("5min")
    ps.load_bars("AAPL", _day_bars(date(2024, 1, 2), minutes=390) + _day_bars(date(2024, 1, 3), minutes=390))
    # THIN last printed on 2024-01-02, its next print is Friday 2024-01-12; decision on 2024-01-03
    ps.load_bars("THIN", _day_bars(date(2024, 1, 2), minutes=390, base=20.0)
                 + _day_bars(date(2024, 1, 12), minutes=390, base=21.0))
    t = _wall(2024, 1, 3, 10, 0)
    ps.set_clock(t)
    assert ps.decision_price("THIN", t) is not None      # decidable: last print is the prior session
    acct = BacktestAccount(994, ps, CFG)
    monkeypatch.setattr(BA, "update_instance", lambda o: None)

    def order(sym, side=OrderDirection.BUY):
        return SimpleNamespace(symbol=sym, side=side, order_type=OrderType.MARKET,
                               asset_class=AssetClass.EQUITY, comment="", status=OrderStatus.ACCEPTED)

    o = order("THIN")
    assert acct._refuse_cross_session_market_entry(o, t) is True
    assert o.status == OrderStatus.EXPIRED
    assert acct.intraday_counters["entries_refused_next_bar_other_session"] == 1
    assert acct._refuse_cross_session_market_entry(order("AAPL"), t) is False   # next bar same session
    acct._update_position("THIN", 5, 20.0)
    assert acct._refuse_cross_session_market_entry(order("THIN", OrderDirection.SELL), t) is False  # closing
