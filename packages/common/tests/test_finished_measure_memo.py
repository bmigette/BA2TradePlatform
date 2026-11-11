"""Per-process memo of measurements that depend only on finished daily history (RSI, SMA200, recent
high): bit-identical to the uncached path, keyed by the clamped as-of session (no lookahead), bounded,
resettable, and the CURRENT price is never cached."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import TradeConditions
from ba2_common.core.TradeConditions import (
    FINISHED_MEASURE_STATS, create_condition, reset_finished_measure_memo)
from ba2_common.core.types import ExpertEventType

DAY1, DAY2 = date(2021, 9, 15), date(2021, 9, 16)
EVENTS = {"rsi3": ExpertEventType.N_PULLBACK_RSI_3, "rsi5": ExpertEventType.N_PULLBACK_RSI_5,
          "sma": ExpertEventType.N_PRICE_VS_SMA200_PERCENT,
          "high": ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH}


def _frame(seed, end=DAY2, n=320):
    rng = np.random.default_rng(seed)
    c = 150.0 + np.cumsum(rng.normal(0, 1.0, n)).clip(-80, None)
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    return pd.DataFrame({"Date": idx, "Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1e6})


class _Provider:
    def __init__(self, frames):
        self.frames, self.calls = frames, 0

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        self.calls += 1
        df = self.frames[symbol]
        if end_date is not None:
            df = df[df["Date"] <= pd.Timestamp(end_date).tz_localize(None)]
        return df.reset_index(drop=True)


class _Account:
    id = 1

    def __init__(self, day, price):
        self.day, self.price = day, price

    def _as_of_date(self):
        return self.day

    def get_instrument_current_price(self, symbol, *a, **k):
        return self.price


def _eval(key, symbol, day, price=120.0):
    rec = SimpleNamespace(created_at=datetime(2021, 9, 15, tzinfo=timezone.utc), instance_id=1,
                          symbol=symbol, data={})
    c = create_condition(EVENTS[key], _Account(day, price), symbol, rec, operator_str=">=", value=-1e9)
    c.evaluate()
    return c.calculated_value


@pytest.fixture
def prov(monkeypatch):
    p = _Provider({"AAA": _frame(1), "BBB": _frame(2)})
    monkeypatch.setattr(TradeConditions, "_provider_resolver", lambda c, n, **kw: p, raising=False)
    return p


def _grid():
    return [(k, s, d) for k in EVENTS for s in ("AAA", "BBB") for d in (DAY1, DAY2, DAY1)]


def test_values_are_bit_identical_with_the_memo_on_and_bypassed(prov, monkeypatch):
    monkeypatch.setattr(TradeConditions, "FINISHED_MEASURE_MEMO_ENABLED", False)
    plain = [_eval(*a) for a in _grid()]
    monkeypatch.setattr(TradeConditions, "FINISHED_MEASURE_MEMO_ENABLED", True)
    reset_finished_measure_memo()
    cached = [_eval(*a) for a in _grid()]
    assert cached == plain and all(v is not None for v in plain)
    assert [_eval(*a) for a in _grid()] == plain          # a second, fully warm pass too


def test_one_computation_per_symbol_measure_session(prov):
    reset_finished_measure_memo()
    for _ in range(5):
        _eval("rsi3", "AAA", DAY1)
    assert FINISHED_MEASURE_STATS == {"evaluations": 5, "computations": 1}
    _eval("rsi3", "AAA", DAY2)          # a session change recomputes
    _eval("rsi3", "BBB", DAY2)          # another symbol recomputes
    _eval("rsi5", "AAA", DAY2)          # another measurement recomputes
    assert FINISHED_MEASURE_STATS["computations"] == 4


def test_a_later_session_never_serves_an_earlier_decision(prov):
    # DAY1's value (history clamped at DAY1) differs from DAY2's; alternating must not mix them
    a1, a2 = _eval("rsi3", "AAA", DAY1), _eval("rsi3", "AAA", DAY2)
    assert a1 != a2
    assert _eval("rsi3", "AAA", DAY1) == a1 and _eval("rsi3", "AAA", DAY2) == a2


def test_the_current_price_is_never_cached(prov):
    v1 = _eval("sma", "AAA", DAY1, price=100.0)
    v2 = _eval("sma", "AAA", DAY1, price=200.0)
    assert v1 != v2 and FINISHED_MEASURE_STATS["computations"] == 1


def test_unknown_outcome_is_memoised_and_reset_clears(prov):
    prov.frames["SHORT"] = _frame(3, n=30)       # < MIN_CLOSES: UNKNOWN
    assert _eval("rsi3", "SHORT", DAY2) is None and _eval("rsi3", "SHORT", DAY2) is None
    assert FINISHED_MEASURE_STATS["computations"] == 1
    reset_finished_measure_memo()
    assert FINISHED_MEASURE_STATS == {"evaluations": 0, "computations": 0}
    _eval("rsi3", "AAA", DAY2)
    assert FINISHED_MEASURE_STATS["computations"] == 1


def test_the_memo_is_bounded_lru(prov, monkeypatch):
    monkeypatch.setattr(TradeConditions, "_FINISHED_MEASURE_MEMO_MAX", 3)
    for i in range(10):
        prov.frames[f"S{i}"] = _frame(i)
        _eval("rsi3", f"S{i}", DAY2)
    assert len(TradeConditions._FINISHED_MEASURE_MEMO) == 3
