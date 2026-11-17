"""DeterministicScorer's LIVE OHLCV frame must not be frozen for the process lifetime.

``fetch_ohlcv`` caches the whole frame per (symbol, lookback) and refetched only when the requested
window started EARLIER than the covered one. Live, the window start (now - lookback) only moves forward,
so the first frame fetched by a long-running process was served for its whole life (every later
session's bar missing). A live read now refetches once per New York session date."""
from datetime import datetime, timezone

import pandas as pd
import pytest

from ba2_experts.DeterministicScorer import data


class _Ohlcv:
    def __init__(self, last_bar_for):
        self.calls = 0
        self.last_bar_for = last_bar_for     # () -> last bar date (a pd.Timestamp)

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        self.calls += 1
        last = self.last_bar_for()
        dates = pd.date_range(end=last, periods=5, freq="B")
        return pd.DataFrame({"Date": dates, "Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1})


class _Providers:
    def __init__(self, ohlcv):
        self._o = ohlcv

    def ohlcv(self):
        return self._o


@pytest.fixture(autouse=True)
def _fresh():
    data.reset_caches()
    yield
    data.reset_caches()


def _clock(monkeypatch, holder):
    monkeypatch.setattr(data, "replay_now", lambda x: holder["now"] if x is None else x)


def test_a_live_frame_advances_with_the_session_date_and_is_not_refetched_within_a_day(monkeypatch):
    clock = {"now": datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)}          # Mon 10:00 NY
    _clock(monkeypatch, clock)
    last = {"d": pd.Timestamp("2026-10-02")}                                    # Friday: last FINAL session
    oh = _Ohlcv(lambda: last["d"])
    p = _Providers(oh)
    f1 = data.fetch_ohlcv(p, "AAPL", None)
    assert pd.Timestamp(f1["Date"].max()) == pd.Timestamp("2026-10-02") and oh.calls == 1
    clock["now"] = datetime(2026, 10, 5, 19, 30, tzinfo=timezone.utc)           # same NY day, later
    data.fetch_ohlcv(p, "AAPL", None)
    assert oh.calls == 1                                                        # cached within the session day
    clock["now"] = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)            # next NY day
    last["d"] = pd.Timestamp("2026-10-05")                                      # Monday became final
    f2 = data.fetch_ohlcv(p, "AAPL", None)
    assert oh.calls == 2
    assert pd.Timestamp(f2["Date"].max()) == pd.Timestamp("2026-10-05")         # the frame ADVANCED


def test_the_new_york_date_not_the_utc_date_is_the_key(monkeypatch):
    clock = {"now": datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)}           # Mon 22:00 NY (UTC says Tuesday)
    _clock(monkeypatch, clock)
    oh = _Ohlcv(lambda: pd.Timestamp("2026-10-05"))
    p = _Providers(oh)
    data.fetch_ohlcv(p, "AAPL", None)
    clock["now"] = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)            # Tue 08:00 NY: a new NY day
    data.fetch_ohlcv(p, "AAPL", None)
    assert oh.calls == 2
    clock["now"] = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)            # Tue 16:00 NY: same NY day
    data.fetch_ohlcv(p, "AAPL", None)
    assert oh.calls == 2


def test_a_backtest_read_is_keyed_by_coverage_only_as_before(monkeypatch):
    clock = {"now": datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)}
    _clock(monkeypatch, clock)
    oh = _Ohlcv(lambda: pd.Timestamp("2026-10-02"))
    p = _Providers(oh)
    asof = datetime(2026, 10, 5, 14, 0)
    data.fetch_ohlcv(p, "AAPL", asof)
    clock["now"] = datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc)            # the wall clock moves on
    data.fetch_ohlcv(p, "AAPL", datetime(2026, 10, 6, 14, 0))                   # a later bar of the same run
    assert oh.calls == 1                                                        # ONE fetch per symbol per run


class _DatedCloses:
    """A daily SPY frame whose closes encode the bar date (close = YYYYMMDD), ending on ``last()``."""

    def __init__(self, last_bar_for):
        self.calls = 0
        self.last_bar_for = last_bar_for

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        self.calls += 1
        dates = pd.date_range(end=self.last_bar_for(), periods=300, freq="B")
        closes = [float(d.strftime("%Y%m%d")) for d in dates]
        return pd.DataFrame({"Date": dates, "Open": closes, "High": closes, "Low": closes,
                             "Close": closes, "Volume": 1})


def test_the_live_index_closes_follow_the_session_day(monkeypatch):
    """The SPY index closes (DeterministicScorer's macro trend input) are memoised on (index, "live").
    A hit used to be served while the SPY frame's fingerprint was unchanged -- and the frame is only
    refreshed by ``fetch_ohlcv``, reached only on a miss -- so a long-running live app scored the trend
    leg on SPY closes frozen at its first analysis. The memo now obeys the per-symbol frames' rule: a
    new New York session day refetches SPY once and refills the memo."""
    clock = {"now": datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)}          # Mon 10:00 NY
    _clock(monkeypatch, clock)
    last = {"d": pd.Timestamp("2026-10-02")}                                    # Friday: last final session
    oh = _DatedCloses(lambda: last["d"])
    p = _Providers(oh)

    d1 = data.fetch_index_closes(p, None, "SPY")
    assert oh.calls == 1 and d1.iloc[-1] == 20261002.0
    clock["now"] = datetime(2026, 10, 5, 19, 30, tzinfo=timezone.utc)           # same NY day, later
    again = data.fetch_index_closes(p, None, "SPY")
    assert oh.calls == 1, "a same-day call must be served from the memo"
    assert again is d1

    clock["now"] = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)            # next NY session day
    last["d"] = pd.Timestamp("2026-10-05")                                      # Monday became final
    d2 = data.fetch_index_closes(p, None, "SPY")
    assert oh.calls == 2, "a new session day must refetch the index frame"
    assert d2.iloc[-1] == 20261005.0, "the index closes stayed frozen at the previous session day"
    clock["now"] = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)
    assert data.fetch_index_closes(p, None, "SPY") is d2 and oh.calls == 2, "the memo was not refilled"


def test_a_backtest_index_read_never_reads_the_clock(monkeypatch):
    """The backtest path (as_of given) is untouched: no wall-clock read decides a hit."""
    clock = {"now": datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)}
    _clock(monkeypatch, clock)
    oh = _DatedCloses(lambda: pd.Timestamp("2026-10-02"))
    p = _Providers(oh)
    asof = datetime(2026, 10, 2, 16, 0)
    first = data.fetch_index_closes(p, asof, "SPY")
    clock["now"] = datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc)            # wall clock moves days on
    assert data.fetch_index_closes(p, asof, "SPY") is first and oh.calls == 1
