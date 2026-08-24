"""``percent_below_recent_high`` / ``percent_above_recent_low`` must measure the window that
ENDS AT THE EVALUATION BAR, not the end of the backtest run.

Both conditions called ``get_ohlcv_data(symbol, interval="1d", lookback_days=N)`` with **no
``end_date``**. In live that is harmless (``end_date=None`` -> now). In a BACKTEST the
resolver hands them the run's ``MemoizedOHLCVProvider``, which DISCARDS ``lookback_days``
entirely and, with both dates ``None``, returns the whole ``[start - warmup, run end]``
window. ``df.tail(20)["High"].max()`` was then the high of the last 20 days OF THE ENTIRE
RUN — a number from the simulated future.

This does not merely miss signal, it FABRICATES it: every dip-entry gate in the option grid
sits on ``percent_below_recent_high``, so the GA has been selecting genes against a quantity
no live run can reproduce.

The fix is at the CONDITION, not the provider: the backtest deliberately injects the
unclamped memoized provider (correct for experts, which self-clamp by passing
``end_date=as_of``), so the conditions must self-clamp the same way.

Frozen clock by construction: the simulated bar is in 2021 and the run window runs to 2021-12,
while the wall clock is 2026 — nothing here can pass by accidentally agreeing with today.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from ba2_common.core import TradeConditions
from ba2_common.core.TradeConditions import create_condition
from ba2_common.core.types import ExpertEventType

# The simulated evaluation bar and the run's full window. The run END is deliberately far
# from the bar so a fetch that ignores the as-of returns visibly different numbers.
SIM_BAR = date(2021, 3, 15)
RUN_START = "2021-01-01"
RUN_END = "2021-12-31"

# Three regimes, so a wrong answer is a DIFFERENT number for each distinct way of being
# wrong (an all-one-value fixture would let "no tail() window at all" pass):
#
#   OLD    ..< SIM_BAR-19  high 300 low  50  — inside the 50-day FETCH, outside the 20-day
#                                              WINDOW. Reading it = the tail() window is gone.
#   WINDOW SIM_BAR-19..BAR high 200 low 100  — the correct trailing RECENT_WINDOW.
#   FUTURE  > SIM_BAR      high 500 low  20  — the simulated future. Reading it = no clamp.
OLD_HIGH, OLD_LOW = 300.0, 50.0
WINDOW_HIGH, WINDOW_LOW = 200.0, 100.0
FUTURE_HIGH, FUTURE_LOW = 500.0, 20.0
CURRENT_PRICE = 150.0

#: (150 vs) the three highs -> 25% / 50% / 70% below; the three lows -> 50% / 200% / 650%.
CORRECT_BELOW, NO_TAIL_BELOW, LOOKAHEAD_BELOW = 25.0, 50.0, 70.0
CORRECT_ABOVE, NO_TAIL_ABOVE, LOOKAHEAD_ABOVE = 50.0, 200.0, 650.0

RECENT_WINDOW = 20  # what both conditions declare


# Inside the WINDOW the highs/lows VARY, so max() vs min() (and Open/Close vs High/Low) are
# different numbers. A flat window would let "recent high = the window MINIMUM" pass.
WINDOW_INNER_HIGH, WINDOW_INNER_LOW = 160.0, 130.0


def _run_window_df(stamp_hour: int = 0):
    """The run's full OHLCV window.

    ``stamp_hour`` puts the bars at that hour of their day. Daily FMP bars are stamped at
    midnight, but an as-of ceiling of *midnight* would silently drop the current bar for any
    feed that stamps intraday — hence the end-OF-DAY ceiling, and hence this knob.
    """
    idx = pd.date_range(RUN_START, RUN_END, freq="D") + pd.Timedelta(hours=stamp_hour)
    window_start = SIM_BAR - timedelta(days=RECENT_WINDOW - 1)
    highs, lows = [], []
    for i, d in enumerate(idx.date):
        if d > SIM_BAR:
            highs.append(FUTURE_HIGH); lows.append(FUTURE_LOW)
        elif d >= window_start:
            # Alternate so the window is not flat: max(High) is WINDOW_HIGH and min(Low) is
            # WINDOW_LOW, but min(High)/max(Low) are different numbers.
            inner = (i % 2 == 1)
            highs.append(WINDOW_INNER_HIGH if inner else WINDOW_HIGH)
            lows.append(WINDOW_INNER_LOW if inner else WINDOW_LOW)
        else:
            highs.append(OLD_HIGH); lows.append(OLD_LOW)
    return pd.DataFrame({
        "Date": idx, "Open": 150.0, "High": highs, "Low": lows,
        "Close": 150.0, "Volume": 1_000_000.0,
    })


class _MemoizedLikeProvider:
    """Stands in for the backtest's ``MemoizedOHLCVProvider``: it DISCARDS ``lookback_days``
    (the real one has no such parameter — it serves in-memory slices of one preloaded window)
    and honours only ``start_date`` / ``end_date``. Records every call."""

    def __init__(self, df):
        self._df = df
        self.calls = []

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kwargs):
        self.calls.append({"symbol": symbol, "start_date": start_date,
                           "end_date": end_date, **kwargs})
        df = self._df
        if start_date is not None:
            df = df[df["Date"] >= pd.Timestamp(start_date).tz_localize(None)]
        if end_date is not None:
            df = df[df["Date"] <= pd.Timestamp(end_date).tz_localize(None)]
        return df.reset_index(drop=True)


class _BacktestAccount:
    """Duck-types the backtest account's simulated clock (``BacktestAccount._as_of_date``)."""
    id = 1

    def _as_of_date(self):
        return SIM_BAR

    def get_instrument_current_price(self, symbol, *a, **k):
        return CURRENT_PRICE


class _LiveAccount:
    """No ``_as_of_date`` — the live shape. Behaviour here must stay byte-identical."""
    id = 1

    def get_instrument_current_price(self, symbol, *a, **k):
        return CURRENT_PRICE


def _rec():
    return SimpleNamespace(created_at=datetime(*SIM_BAR.timetuple()[:3], tzinfo=timezone.utc),
                           instance_id=1, symbol="AAPL", data={})


@pytest.fixture
def provider(monkeypatch):
    p = _MemoizedLikeProvider(_run_window_df())
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: p, raising=False)
    return p


def _cond(event_type, account, op, value):
    return create_condition(event_type, account, "AAPL", _rec(),
                            operator_str=op, value=value)


# --------------------------------------------------------------------------------------
# The lookahead itself
# --------------------------------------------------------------------------------------
def test_percent_below_recent_high_uses_the_window_ending_at_the_simulated_bar(provider):
    cond = _cond(ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH, _BacktestAccount(), ">=", 0.0)
    cond.evaluate()
    assert cond.calculated_value == pytest.approx(CORRECT_BELOW), (
        f"got {cond.calculated_value}% below the recent high. "
        f"{CORRECT_BELOW}% = the correct trailing {RECENT_WINDOW}-day window; "
        f"{LOOKAHEAD_BELOW}% = the last {RECENT_WINDOW} bars of the whole RUN (lookahead); "
        f"{NO_TAIL_BELOW}% = the whole 50-day FETCH (the tail() window was dropped)"
    )


def test_percent_above_recent_low_uses_the_window_ending_at_the_simulated_bar(provider):
    cond = _cond(ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW, _BacktestAccount(), ">=", 0.0)
    cond.evaluate()
    assert cond.calculated_value == pytest.approx(CORRECT_ABOVE), (
        f"got {cond.calculated_value}% above the recent low. "
        f"{CORRECT_ABOVE}% = correct; {LOOKAHEAD_ABOVE}% = whole-run lookahead; "
        f"{NO_TAIL_ABOVE}% = the whole 50-day fetch (tail() window dropped)"
    )


@pytest.mark.parametrize("event_type,window_val,fetch_val", [
    (ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH, CORRECT_BELOW, NO_TAIL_BELOW),
    (ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW, CORRECT_ABOVE, NO_TAIL_ABOVE),
])
def test_the_measured_window_is_RECENT_WINDOW_not_the_whole_fetch(event_type, window_val,
                                                                  fetch_val, provider):
    """The FETCH is deliberately wider than the WINDOW (``lookback_days = N*2 + 10``) to
    survive weekends/holidays. The measurement must still be the last ``RECENT_WINDOW`` BARS,
    so extra fetched history cannot widen the "recent" high/low."""
    cond = _cond(event_type, _BacktestAccount(), ">=", 0.0)
    cond.evaluate()
    assert cond.calculated_value == pytest.approx(window_val)
    assert cond.calculated_value != pytest.approx(fetch_val)
    assert cond.RECENT_WINDOW == RECENT_WINDOW


@pytest.mark.parametrize("event_type", [ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH,
                                        ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW])
def test_the_fetch_is_clamped_to_the_simulated_bar(event_type, provider):
    """Pin the mechanism, not just the number: an ``end_date`` must actually be passed, and
    it must be the BAR — not the run end, not the wall clock."""
    _cond(event_type, _BacktestAccount(), ">=", 0.0).evaluate()
    (call,) = provider.calls
    end = call["end_date"]
    assert end is not None, (
        "get_ohlcv_data was called with end_date=None; the backtest's memoized provider "
        "then returns the ENTIRE run window and tail(20) reads the simulated future"
    )
    end_date = end.date() if isinstance(end, datetime) else end
    assert end_date == SIM_BAR, (
        f"end_date is {end_date}, not the simulated bar {SIM_BAR}"
    )


def test_the_gate_decision_flips_not_just_the_number(provider):
    """The economic statement: a dip-entry gate reading the future ENTERS on a dip that has
    not happened. ``below >= 50%`` is False on the real trailing window (25%) and True on the
    whole-run window (70%)."""
    cond = _cond(ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH, _BacktestAccount(), ">=", 50.0)
    assert cond.evaluate() is False


# --------------------------------------------------------------------------------------
# Live must be byte-identical
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("event_type", [ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH,
                                        ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW])
def test_live_account_still_fetches_with_no_end_date(event_type, provider):
    """A live account has no ``_as_of_date``. It must keep passing ``end_date=None``: that is
    the spelling ``MarketDataProviderInterface._is_latest_request`` uses to allow the parquet
    cache top-up, and an explicit end-of-day stamp would suppress it whenever the local date
    is behind UTC."""
    _cond(event_type, _LiveAccount(), ">=", 0.0).evaluate()
    (call,) = provider.calls
    assert call["end_date"] is None
    assert call["lookback_days"] == 50   # RECENT_WINDOW * 2 + 10, unchanged


@pytest.mark.parametrize("event_type,expected", [
    (ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH, CORRECT_BELOW),
    (ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW, CORRECT_ABOVE),
])
def test_intraday_stamped_bars_still_include_the_as_of_bar(event_type, expected, monkeypatch):
    """The as-of ceiling is the END of the bar's day, not midnight.

    With a midnight ceiling, any feed that stamps its bars intraday (16:00 close) would have
    the CURRENT bar sliced off — the condition would silently measure through yesterday, and
    on a 1-bar-old signal that is the difference between firing and not."""
    p = _MemoizedLikeProvider(_run_window_df(stamp_hour=16))
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: p, raising=False)
    cond = _cond(event_type, _BacktestAccount(), ">=", 0.0)
    cond.evaluate()
    last = p.calls[0]
    assert cond.calculated_value == pytest.approx(expected), (
        f"the 16:00-stamped bar of {SIM_BAR} was excluded by the as-of ceiling "
        f"{last['end_date']}"
    )
    # ...and the future is still excluded.
    assert cond.calculated_value != pytest.approx(LOOKAHEAD_BELOW)


def test_a_broken_simulated_clock_does_not_silently_become_the_wall_clock(provider):
    """If an account advertises ``_as_of_date`` but it raises, falling back to ``date.today()``
    would reinstate the very lookahead this fixes. The condition must refuse to evaluate."""
    class _BrokenClock(_BacktestAccount):
        def _as_of_date(self):
            raise RuntimeError("simulated clock unavailable")

    cond = _cond(ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH, _BrokenClock(), ">=", 0.0)
    assert cond.evaluate() is False
    assert cond.calculated_value is None
    assert provider.calls == [], "no OHLCV fetch may happen without a usable evaluation date"


def test_a_simulated_clock_returning_none_does_not_become_the_wall_clock(provider):
    class _NoneClock(_BacktestAccount):
        def _as_of_date(self):
            return None

    cond = _cond(ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW, _NoneClock(), ">=", 0.0)
    assert cond.evaluate() is False
    assert cond.calculated_value is None
    assert provider.calls == []
