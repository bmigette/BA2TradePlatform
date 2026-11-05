"""Engine review fixes on the intraday clock (E4): the decision-time mark of a held position, the clamp
of every non-daily interval, the narrowed ATR calendar failure, the I3 expiry state."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.services.backtest.price_source import (
    AsOfPriceSource, MemoizedOHLCVProvider, _ended_bar_cap)
from tests.backtest.test_intraday_daily_knowability import _FakeDaily, _wall, _ps, SYMBOL


def _bars(d, minutes=390, step=5, base=100.0):
    rows, t, px = [], datetime(d.year, d.month, d.day, 9, 30), base
    for _ in range(minutes // step):
        rows.append({"Date": t, "Open": px, "High": px + .5, "Low": px - .5, "Close": px + .3, "Volume": 10})
        t += timedelta(minutes=step)
        px += .1
    return rows


# --------------------------------------------------------------------------- the mark of a held position
def _account_with_held(ps, sym="THIN"):
    from app.services.backtest.backtest_account import BacktestAccount
    from tests.backtest.test_max_loss_stop_engine import CFG
    acct = BacktestAccount(995, ps, CFG)
    acct._update_position(sym, 10, 20.0)
    return acct


def test_a_stale_held_symbol_is_marked_at_its_last_ended_close_not_the_entry_price_nor_the_clock_bar():
    ps = _ps("5min")
    # THIN printed only on 2023-12-20 (older than the last finished session): not DECIDABLE, still held
    ps.load_bars("THIN", _bars(date(2023, 12, 20), base=31.0))
    t = _wall(2024, 1, 3, 10, 0)
    ps.set_clock(t)
    assert ps.decision_price("THIN", t) is None
    acct = _account_with_held(ps)
    last_close = float(ps.last_ended_close("THIN", t))
    assert last_close == pytest.approx(31.0 + 0.1 * 77 + 0.3)          # the last bar of 2023-12-20
    p = next(p for p in acct._positions.values() if p.symbol == "THIN")
    assert acct._equity_mark_price(p, decision=True) == pytest.approx(last_close)   # not 20.0 (entry)
    cur = {x["symbol"]: x["current_price"] for x in acct.get_positions()}
    assert cur["THIN"] == pytest.approx(last_close)


def test_the_mark_is_never_the_clock_bars_own_close():
    ps = _ps("5min")
    ps.load_bars("THIN", _bars(date(2024, 1, 3), base=50.0))
    t = _wall(2024, 1, 3, 10, 0)
    ps.set_clock(t)
    own_close = ps.close_at("THIN")                                     # the 10:00 bar's close: not printed yet
    ended = ps.last_ended_close("THIN", t)
    assert own_close is not None and ended != own_close
    assert ps.decision_price("THIN", t) == pytest.approx(ended)


def test_a_held_symbol_with_no_ended_bar_at_all_raises():
    ps = _ps("5min")
    ps.load_bars("THIN", _bars(date(2024, 1, 3), base=50.0))
    t = _wall(2024, 1, 3, 9, 30)                                        # its first bar has not ended yet
    ps.set_clock(t)
    acct = _account_with_held(ps)
    p = next(p for p in acct._positions.values() if p.symbol == "THIN")
    with pytest.raises(ValueError, match="no bar has ever ended"):
        acct._equity_mark_price(p, decision=True)
    with pytest.raises(ValueError, match="no bar has"):
        acct.get_positions()


# --------------------------------------------------------------------------- clamp of every interval
@pytest.mark.parametrize("interval", ["5min", "15min", "1h"])
@pytest.mark.parametrize("hhmm", ["09:30", "10:00", "10:07", "15:55"])
def test_intraday_interval_reads_return_only_bars_that_have_ended(interval, hhmm):
    ps = _ps(interval if interval != "1h" else "5min")
    memo = MemoizedOHLCVProvider(_FakeDaily(), datetime(2023, 12, 1), datetime(2024, 1, 31), interval="5min")
    memo.bind_price_source(ps)
    span = {"5min": 5, "15min": 15, "1h": 60}[interval]
    d = date(2024, 1, 3)
    mins = pd.date_range(datetime(2024, 1, 2, 9, 30), datetime(2024, 1, 3, 15, 55), freq=f"{span}min")
    mins = [m for m in mins if 9 * 60 + 30 <= m.hour * 60 + m.minute < 16 * 60]
    df = pd.DataFrame({"Date": mins, "Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1})
    dates = df["Date"].values.astype("datetime64[ns]")
    memo._full = lambda symbol, iv: (df, dates)
    h, m = int(hhmm[:2]), int(hhmm[3:])
    t = _wall(2024, 1, 3, h, m)
    ps.set_clock(t)
    for end in (None, t, datetime(2035, 1, 1, tzinfo=timezone.utc)):
        out = memo.get_ohlcv_data(SYMBOL, end_date=end, interval=interval)
        last = pd.Timestamp(out["Date"].iloc[-1])
        assert last + timedelta(minutes=span) <= pd.Timestamp(t.replace(tzinfo=None)), (interval, hhmm, end, last)
    full = memo.get_ohlcv_data_unsliced(SYMBOL, end_date=None, interval=interval)     # explicit opt-out only
    assert pd.Timestamp(full["Date"].iloc[-1]) == mins[-1]


def test_weekly_and_monthly_caps_exclude_the_unfinished_bar():
    ps = _ps("5min")
    wed = _wall(2024, 1, 3, 10, 0)                       # last finished session: Tue 2024-01-02
    ps.set_clock(wed)
    assert _ended_bar_cap(ps, wed, "1wk") < datetime(2024, 1, 1, tzinfo=timezone.utc)   # Mon 01-01's week not over
    assert _ended_bar_cap(ps, wed, "1mo") < datetime(2024, 1, 1, tzinfo=timezone.utc)   # January not over
    mon = _wall(2024, 1, 8, 10, 0)                       # last finished: Fri 2024-01-05 -> week of 01-01 is over
    assert _ended_bar_cap(ps, mon, "1wk") >= datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_an_unreadable_interval_is_refused_not_guessed():
    ps = _ps("5min")
    t = _wall(2024, 1, 3, 10, 0)
    ps.set_clock(t)
    with pytest.raises(ValueError, match="cannot read the bar length"):
        _ended_bar_cap(ps, t, "fortnight")


# --------------------------------------------------------------------------- the ATR calendar failure
def test_a_calendar_failure_in_the_atr_provider_propagates_instead_of_meaning_no_atr():
    from app.services.backtest import seam_wiring as sw
    prov = sw.MetricStoreATRProvider.__new__(sw.MetricStoreATRProvider)

    def boom(as_of):
        raise RuntimeError("calendar unavailable")

    prov._session_date_fn = boom
    from ba2_providers.screener import metric_store as ms
    with pytest.raises(RuntimeError, match="calendar unavailable"):
        prov.get_indicator("AAPL", "atr", end_date=datetime(2024, 1, 3), interval="1d",
                           period=sorted(ms.ATR_PERIODS)[0])
