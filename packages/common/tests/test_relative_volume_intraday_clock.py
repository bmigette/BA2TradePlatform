"""RelativeVolume on an INTRADAY backtest clock: today's volume is what has traded so far (the sum of
the intraday bars ended at the decision), the baseline is the last W FINISHED sessions. Without the
account hook (live, daily clock) the as-of bar stays the last bar of the series, as before."""
from types import SimpleNamespace

import pandas as pd
import pytest

from ba2_common.core import TradeConditions as TC


class _Prov:
    """Daily series of FINISHED sessions only (what the memoized provider returns on an intraday clock)."""

    def __init__(self, vols):
        self._df = pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=len(vols)), "Volume": vols})

    def get_ohlcv_data(self, *a, **k):
        return self._df


class _Acct:
    def __init__(self, so_far):
        self._so_far = so_far

    def intraday_volume_so_far(self, symbol):
        return (True, self._so_far)

    def _as_of_date(self):
        from datetime import date
        return date(2024, 2, 1)


def _cond(acct, vols, monkeypatch, op=">", value=1.0):
    monkeypatch.setattr(TC, "_get_provider", lambda *a, **k: _Prov(vols))
    return TC.RelativeVolumeCondition(acct, "AAPL", None, op, value)


def test_intraday_clock_compares_volume_so_far_with_the_finished_baseline(monkeypatch):
    vols = [100.0] * 25                     # 25 finished sessions, baseline 100
    c = _cond(_Acct(250.0), vols, monkeypatch)
    assert c.evaluate() is True
    assert c.calculated_value == pytest.approx(2.5)        # 250 so far / 100, not 100/100


def test_intraday_clock_baseline_does_not_drop_a_finished_session(monkeypatch):
    # the last finished session is a spike: it IS part of the baseline (it is not "today")
    vols = [100.0] * 24 + [2100.0]          # mean of the last 20 = (19*100 + 2100)/20 = 200
    c = _cond(_Acct(100.0), vols, monkeypatch)
    c.evaluate()
    assert c.calculated_value == pytest.approx(0.5)


def test_no_intraday_volume_is_unknown_not_one(monkeypatch):
    c = _cond(SimpleNamespace(intraday_volume_so_far=lambda s: (True, None), _as_of_date=lambda: __import__("datetime").date(2024, 2, 1)),
              [100.0] * 25, monkeypatch)
    assert c.evaluate() is False and c.calculated_value is None


def test_daily_clock_and_live_keep_the_as_of_bar_semantics(monkeypatch):
    acct = SimpleNamespace(intraday_volume_so_far=lambda s: (False, None),
                           _as_of_date=lambda: __import__("datetime").date(2024, 2, 1))
    vols = [100.0] * 24 + [300.0]
    c = _cond(acct, vols, monkeypatch)
    c.evaluate()
    assert c.calculated_value == pytest.approx(3.0)        # last bar vs the 20 before it
