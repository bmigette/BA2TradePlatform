"""goal2027 design 2026-11-10 part 3: the pullback entry conditions (Wilder RSI of finished
daily closes, price vs SMA200) and their registration."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import TradeConditions
from ba2_common.core.TradeConditions import CONDITION_MAP, create_condition
from ba2_common.core.rsi import wilder_rsi
from ba2_common.core.rule_builders import FIELD_EVENT, triggers_from_condition_tree
from ba2_common.core.types import ExpertEventType, get_numeric_event_values

SIM_BAR = date(2021, 9, 15)
RUN_START, RUN_END = "2020-01-01", "2021-12-31"

RSI_EVENTS = {2: ExpertEventType.N_PULLBACK_RSI_2, 3: ExpertEventType.N_PULLBACK_RSI_3,
              4: ExpertEventType.N_PULLBACK_RSI_4, 5: ExpertEventType.N_PULLBACK_RSI_5}
SMA_EVENT = ExpertEventType.N_PRICE_VS_SMA200_PERCENT


def test_wilder_rsi_hand_computed():
    # period 2: alpha 0.5. changes +2, -1: seed up=2 down=0; then up=1, down=0.5 -> RS 2 -> 66.667
    assert wilder_rsi(np.array([10.0, 12.0, 11.0]), 2) == pytest.approx(200.0 / 3.0)
    # no down move at all -> 100; no move at all -> undefined
    assert wilder_rsi(np.array([1.0, 2.0, 3.0]), 3) == 100.0
    with pytest.raises(ValueError):
        wilder_rsi(np.array([5.0, 5.0, 5.0]), 3)


def _closes(n=300):
    rng = np.random.default_rng(7)
    return 100.0 + np.cumsum(rng.normal(0, 1.0, n)).clip(-50, None) + 60.0


def _frame(closes, future_close=None):
    """Daily bars ending ON the simulated bar, plus (optionally) 'future' bars after it."""
    idx = pd.bdate_range(end=pd.Timestamp(SIM_BAR), periods=len(closes))
    df = pd.DataFrame({"Date": idx, "Open": closes, "High": closes, "Low": closes,
                       "Close": closes, "Volume": 1e6})
    if future_close is not None:
        fidx = pd.bdate_range(start=pd.Timestamp(SIM_BAR) + pd.Timedelta(days=1), periods=40)
        fut = pd.DataFrame({"Date": fidx, "Open": future_close, "High": future_close,
                            "Low": future_close, "Close": future_close, "Volume": 1e6})
        df = pd.concat([df, fut], ignore_index=True)
    return df


class _Provider:
    """Honours end_date like the backtest's memoized provider, ignores lookback_days."""

    def __init__(self, df):
        self.df, self.calls = df, []

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        self.calls.append({"end_date": end_date, **kw})
        df = self.df
        if end_date is not None:
            df = df[df["Date"] <= pd.Timestamp(end_date).tz_localize(None)]
        return df.reset_index(drop=True)


class _Account:
    id = 1

    def __init__(self, price, sim=True):
        self._price = price
        if sim:
            self._as_of_date = lambda: SIM_BAR

    def get_instrument_current_price(self, symbol, *a, **k):
        return self._price


def _rec():
    return SimpleNamespace(created_at=datetime(2021, 9, 15, tzinfo=timezone.utc),
                           instance_id=1, symbol="AAPL", data={})


def _cond(event, account, op, value):
    return create_condition(event, account, "AAPL", _rec(), operator_str=op, value=value)


def _install(monkeypatch, df):
    p = _Provider(df)
    monkeypatch.setattr(TradeConditions, "_provider_resolver", lambda c, n, **kw: p, raising=False)
    return p


def _pandas_rsi(close, n):
    d = pd.Series(close).diff().dropna()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return float((100 - 100 / (1 + up / dn)).iloc[-1])


@pytest.mark.parametrize("period", [2, 3, 4, 5])
def test_rsi_condition_value_matches_an_independent_formulation(period, monkeypatch):
    closes = _closes()
    _install(monkeypatch, _frame(closes))
    c = _cond(RSI_EVENTS[period], _Account(100.0), ">=", 0.0)
    assert c.evaluate() is True
    assert c.calculated_value == pytest.approx(_pandas_rsi(closes, period), abs=1e-6)


def test_rsi_threshold_operator(monkeypatch):
    closes = _closes()
    _install(monkeypatch, _frame(closes))
    v = _pandas_rsi(closes, 3)
    assert _cond(RSI_EVENTS[3], _Account(1.0), "<", v + 1).evaluate() is True
    assert _cond(RSI_EVENTS[3], _Account(1.0), "<", v - 1).evaluate() is False


def test_sma200_distance(monkeypatch):
    closes = _closes()
    _install(monkeypatch, _frame(closes))
    sma = float(np.mean(closes[-200:]))
    price = sma * 1.05
    c = _cond(SMA_EVENT, _Account(price), ">", 4.9)
    assert c.evaluate() is True
    assert c.calculated_value == pytest.approx(5.0)
    assert _cond(SMA_EVENT, _Account(sma * 0.9), "<", -9.9).evaluate() is True


@pytest.mark.parametrize("event", [RSI_EVENTS[2], RSI_EVENTS[5], SMA_EVENT])
def test_no_lookahead_future_bars_are_never_read(event, monkeypatch):
    closes = _closes()
    base = _install(monkeypatch, _frame(closes))
    a = _cond(event, _Account(120.0), ">=", -1e9)
    a.evaluate()
    (call,) = base.calls
    assert call["end_date"] is not None
    assert (call["end_date"].date() if isinstance(call["end_date"], datetime) else call["end_date"]) == SIM_BAR
    _install(monkeypatch, _frame(closes, future_close=1000.0))
    b = _cond(event, _Account(120.0), ">=", -1e9)
    b.evaluate()
    assert b.calculated_value == pytest.approx(a.calculated_value)


@pytest.mark.parametrize("event", [RSI_EVENTS[2], SMA_EVENT])
def test_live_fetch_is_unclamped(event, monkeypatch):
    p = _install(monkeypatch, _frame(_closes()))
    _cond(event, _Account(100.0, sim=False), ">=", 0.0).evaluate()
    assert p.calls[0]["end_date"] is None


@pytest.mark.parametrize("event", [RSI_EVENTS[2], SMA_EVENT])
def test_insufficient_history_is_unknown_never_a_default(event, monkeypatch):
    _install(monkeypatch, _frame(_closes(50)))
    c = _cond(event, _Account(100.0), ">=", -1e9)
    assert c.evaluate() is False
    assert c.calculated_value is None
    # a gate that is UNKNOWN fails every operator
    assert _cond(event, _Account(100.0), "<", 1e9).evaluate() is False


def test_missing_price_is_unknown(monkeypatch):
    _install(monkeypatch, _frame(_closes()))
    c = _cond(SMA_EVENT, _Account(None), ">=", -1e9)
    assert c.evaluate() is False and c.calculated_value is None


def test_every_pullback_event_is_registered_everywhere():
    fields = {"pullback_rsi_2", "pullback_rsi_3", "pullback_rsi_4", "pullback_rsi_5",
              "price_vs_sma200_percent"}
    for f in fields:
        event = ExpertEventType(f)
        assert event in CONDITION_MAP
        assert FIELD_EVENT[f] is event
        assert f in get_numeric_event_values()
        tree = {"operator": "AND", "conditions": [
            {"id": "x", "field": f, "op": "<", "comparison": "<", "value": 10.0, "field_type": "numeric"}]}
        (trig,) = triggers_from_condition_tree(tree).values()
        assert trig["event_type"] == f
