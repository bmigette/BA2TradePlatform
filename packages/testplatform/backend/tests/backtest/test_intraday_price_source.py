"""Intraday execution-interval support for the backtest price source / clock.

The default ``1d`` path keys bars by calendar date (covered by the existing daily
tests). With an intraday ``interval`` (e.g. ``1h``) the source must key bars by full
timestamp so multiple bars per day are distinct, and the engine clock must step once
per intraday bar — enabling finer open/close fill detection. Experts' own data fetches
are unaffected (they go through the provider seam, not this price source).
"""
from datetime import date, datetime

from app.services.backtest.price_source import AsOfPriceSource, _norm, _is_intraday
from app.services.backtest.daily_engine import trading_days


def _bars_hourly():
    # Two trading days, 3 hourly bars each — same calendar date, distinct timestamps.
    return [
        {"date": datetime(2024, 1, 2, 9, 0), "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1},
        {"date": datetime(2024, 1, 2, 10, 0), "open": 10.5, "high": 12, "low": 10, "close": 11.5, "volume": 1},
        {"date": datetime(2024, 1, 2, 11, 0), "open": 11.5, "high": 12.5, "low": 11, "close": 12.0, "volume": 1},
        {"date": datetime(2024, 1, 3, 9, 0), "open": 12, "high": 13, "low": 11.5, "close": 12.8, "volume": 1},
        {"date": datetime(2024, 1, 3, 10, 0), "open": 12.8, "high": 13.5, "low": 12.5, "close": 13.2, "volume": 1},
    ]


class TestIntervalClassification:
    def test_is_intraday(self):
        assert _is_intraday("1h") and _is_intraday("15m") and _is_intraday("5min")
        assert not _is_intraday("1d") and not _is_intraday("1wk") and not _is_intraday("1mo")

    def test_norm_daily_returns_date(self):
        assert _norm(datetime(2024, 1, 2, 10, 30), "1d") == date(2024, 1, 2)

    def test_norm_intraday_returns_datetime(self):
        k = _norm(datetime(2024, 1, 2, 10, 0), "1h")
        assert isinstance(k, datetime) and k == datetime(2024, 1, 2, 10, 0)


class TestIntradaySource:
    def test_distinct_bars_same_day(self):
        ps = AsOfPriceSource(ohlcv_provider=None, interval="1h")
        ps.load_bars("AAPL", _bars_hourly())
        # 5 distinct intraday keys, not 2 calendar days.
        assert len(ps.all_dates()) == 5
        assert all(isinstance(k, datetime) for k in ps.all_dates())

    def test_clock_resolves_exact_bar(self):
        ps = AsOfPriceSource(ohlcv_provider=None, interval="1h")
        ps.load_bars("AAPL", _bars_hourly())
        ps.set_clock(datetime(2024, 1, 2, 10, 0))
        assert ps.close_at("AAPL") == 11.5            # the 10:00 bar, not the day's last
        ps.set_clock(datetime(2024, 1, 2, 11, 0))
        assert ps.close_at("AAPL") == 12.0

    def test_next_bar_is_next_hour(self):
        ps = AsOfPriceSource(ohlcv_provider=None, interval="1h")
        ps.load_bars("AAPL", _bars_hourly())
        nb = ps.next_bar("AAPL", datetime(2024, 1, 2, 9, 0))
        assert nb["open"] == 10.5                      # the 10:00 bar (next-bar fill)

    def test_engine_clock_steps_per_intraday_bar(self):
        ps = AsOfPriceSource(ohlcv_provider=None, interval="1h")
        ps.load_bars("AAPL", _bars_hourly())
        clock = trading_days(datetime(2024, 1, 2), datetime(2024, 1, 3, 23, 59), ps)
        assert len(clock) == 5                         # one step per intraday bar
        assert clock[0] == datetime(2024, 1, 2, 9, 0)


class TestDailyUnchanged:
    def test_daily_still_keys_by_date(self):
        ps = AsOfPriceSource(ohlcv_provider=None, interval="1d")
        ps.load_bars("AAPL", [
            {"date": "2024-01-02", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1},
            {"date": "2024-01-03", "open": 10.5, "high": 12, "low": 10, "close": 11.5, "volume": 1},
        ])
        assert ps.all_dates() == [date(2024, 1, 2), date(2024, 1, 3)]
        ps.set_clock(datetime(2024, 1, 3, 14, 0))      # any time-of-day resolves the day's bar
        assert ps.close_at("AAPL") == 11.5
