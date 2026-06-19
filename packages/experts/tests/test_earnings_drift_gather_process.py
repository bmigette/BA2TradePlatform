"""Phase 1 Task 5: FMPEarningsDrift _gather/_process split + analyze_as_of parity.

Proves: _process is pure (no provider/DB reads), _gather threads as_of into the
provider (no datetime.now() leak), and analyze_as_of runs the SAME _gather+_process
as the live path (logic-equality is the golden-test contract).
"""
from datetime import datetime, timezone

import pandas as pd

from ba2_experts.FMPEarningsDrift import FMPEarningsDrift
from ba2_common.core.types import OrderRecommendation
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)
SETTINGS = {"surprise_min_pct": 5.0, "max_days_since_report": 30, "expected_profit_percent": 8.0}


class FakeDetails:
    def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
        return {"earnings": [{"report_date": "2026-06-10", "reported_eps": 1.2,
                              "estimated_eps": 1.0, "surprise_percent": 20.0}]}


class FakeOHLCV:
    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        return pd.DataFrame({"Close": [100.0]})


def _get_provider(cat, name, **kw):
    return {"fundamentals_details": FakeDetails(), "ohlcv": FakeOHLCV()}[cat]


def _expert():
    e = FMPEarningsDrift.__new__(FMPEarningsDrift)
    e.id = 1
    e._gather_symbol = "AAPL"
    return e


def test_process_buy_on_fresh_beat():
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_get_provider), as_of=NOW)
    rec = e._process(bundle, SETTINGS, as_of=NOW)
    assert rec.signal == OrderRecommendation.BUY
    assert 55.0 <= rec.confidence <= 100.0
    assert rec.current_price == 100.0
    assert rec.expected_profit_percent == 8.0


def test_process_hold_below_threshold():
    """Surprise below the threshold => HOLD, confidence 10, expected_profit 0."""
    class WeakDetails:
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
            return {"earnings": [{"report_date": "2026-06-10", "reported_eps": 1.01,
                                  "estimated_eps": 1.0, "surprise_percent": 1.0}]}

    e = _expert()
    bundle = e._gather(
        LiveProviderBundle(lambda c, n, **k: WeakDetails() if c == "fundamentals_details" else FakeOHLCV()),
        as_of=NOW)
    rec = e._process(bundle, SETTINGS, as_of=NOW)
    assert rec.signal == OrderRecommendation.HOLD
    assert rec.confidence == 10.0
    assert rec.expected_profit_percent == 0.0


def test_gather_threads_as_of_into_provider():
    captured = {}

    class Spy(FakeDetails):
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
            captured["end_date"] = end_date
            return super().get_past_earnings(symbol, frequency, end_date, lookback_periods, format_type)

    e = _expert()
    e._gather(
        LiveProviderBundle(lambda c, n, **k: Spy() if c == "fundamentals_details" else FakeOHLCV()),
        as_of=NOW)
    assert captured["end_date"] == NOW  # as_of threaded, not datetime.now()


def test_analyze_as_of_equals_live_process():
    """analyze_as_of(now) drives the same _gather+_process as _process(_gather(live, None))."""
    e = _expert()
    ctx = BacktestContext(providers=LiveProviderBundle(_get_provider),
                          settings=SETTINGS, as_of=NOW)
    rec_asof = e.analyze_as_of(NOW, ctx)

    # "live" path with as_of=None against the same fake providers, then pin the same now
    bundle_live = e._gather(LiveProviderBundle(_get_provider), as_of=None)
    rec_live = e._process(bundle_live, SETTINGS, as_of=NOW)

    assert rec_asof.almost_equals(rec_live)
    assert rec_asof.signal == OrderRecommendation.BUY
    assert rec_asof.details == rec_live.details
