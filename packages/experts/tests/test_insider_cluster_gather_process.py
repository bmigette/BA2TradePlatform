"""Phase 1 Task 6: FMPInsiderClusterBuy _gather/_process split + analyze_as_of parity.

Proves: _process is pure (no provider/DB reads), _gather threads as_of into the
insider provider (no datetime.now() leak; the corrected provider enforces the
filingDate no-lookahead anchor when as_of is set), and analyze_as_of runs the SAME
_gather+_process as the live path (logic-equality is the golden-test contract).

The insider transaction dicts use the dict-format keys the real provider emits
(insider_name / transaction_type / value), reused from the Phase-0
detect_insider_cluster fixtures for value parity.
"""
from datetime import datetime, timezone

import pandas as pd

from ba2_experts.FMPInsiderClusterBuy import FMPInsiderClusterBuy
from ba2_common.core.types import OrderRecommendation
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)
SETTINGS = {"lookback_days": 30, "min_insiders": 3, "min_total_value": 200_000.0,
            "expected_profit_percent": 10.0}

THREE_BUYERS = {
    "start_date": "2026-05-14T00:00:00", "end_date": "2026-06-13T00:00:00",
    "transactions": [
        {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "C", "transaction_type": "P-Purchase", "value": 100_000},
    ],
}
TWO_BUYERS = {
    "start_date": "2026-05-14T00:00:00", "end_date": "2026-06-13T00:00:00",
    "transactions": [
        {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
    ],
}


class FakeInsider:
    def __init__(self, payload):
        self._payload = payload

    def get_insider_transactions(self, symbol, end_date, lookback_days=None,
                                 as_of=None, format_type="dict", **kw):
        return self._payload


class FakeOHLCV:
    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        return pd.DataFrame({"Close": [100.0]})


def _provider_resolver(insider_payload):
    insider = FakeInsider(insider_payload)
    ohlcv = FakeOHLCV()
    return lambda cat, name, **kw: {"insider": insider, "ohlcv": ohlcv}[cat]


def _expert():
    e = FMPInsiderClusterBuy.__new__(FMPInsiderClusterBuy)
    e.id = 1
    e._gather_symbol = "AAPL"
    e._gather_lookback_days = 30
    return e


def test_process_buy_on_three_buyer_cluster():
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_provider_resolver(THREE_BUYERS)), as_of=NOW)
    rec = e._process(bundle, SETTINGS, as_of=NOW)
    assert rec.signal == OrderRecommendation.BUY
    assert rec.confidence > 55.0
    assert rec.current_price == 100.0
    assert rec.expected_profit_percent == 10.0
    # cluster carried in raw_outputs for live state persistence
    assert rec.raw_outputs["cluster"]["buyer_count"] == 3
    assert rec.raw_outputs["cluster"]["buy_value"] == 300_000


def test_process_hold_below_min_insiders():
    """Only 2 distinct buyers => no cluster => HOLD, confidence 10, expected_profit 0."""
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_provider_resolver(TWO_BUYERS)), as_of=NOW)
    rec = e._process(bundle, SETTINGS, as_of=NOW)
    assert rec.signal == OrderRecommendation.HOLD
    assert rec.confidence == 10.0
    assert rec.expected_profit_percent == 0.0
    assert rec.raw_outputs["cluster"]["buyer_count"] == 2


def test_gather_threads_as_of_into_provider():
    """_gather must pass as_of (and lookback) to the insider provider, not datetime.now()."""
    captured = {}

    class Spy(FakeInsider):
        def get_insider_transactions(self, symbol, end_date, lookback_days=None,
                                     as_of=None, format_type="dict", **kw):
            captured["as_of"] = as_of
            captured["end_date"] = end_date
            captured["lookback_days"] = lookback_days
            return super().get_insider_transactions(
                symbol, end_date, lookback_days=lookback_days,
                as_of=as_of, format_type=format_type)

    spy = Spy(THREE_BUYERS)
    e = _expert()
    e._gather(LiveProviderBundle(
        lambda c, n, **k: spy if c == "insider" else FakeOHLCV()), as_of=NOW)
    assert captured["as_of"] == NOW          # as_of threaded for the filingDate anchor
    assert captured["end_date"] == NOW       # as_of -> end_date (cached_get alias)
    assert captured["lookback_days"] == 30   # gather-time fetch window


def test_gather_coerces_nondict_to_empty():
    """A non-dict insider response must not crash _gather; it becomes an empty cluster."""
    class BadInsider(FakeInsider):
        def get_insider_transactions(self, *a, **k):
            return "no data"

    e = _expert()
    bundle = e._gather(LiveProviderBundle(
        lambda c, n, **k: BadInsider(None) if c == "insider" else FakeOHLCV()), as_of=NOW)
    assert bundle["insider_data"] == {"transactions": [], "start_date": "", "end_date": ""}
    rec = e._process(bundle, SETTINGS, as_of=NOW)
    assert rec.signal == OrderRecommendation.HOLD


def test_analyze_as_of_equals_live_process():
    """analyze_as_of(now) drives the same _gather+_process as _process(_gather(live, None))."""
    e = _expert()
    ctx = BacktestContext(
        providers=LiveProviderBundle(_provider_resolver(THREE_BUYERS)),
        settings=SETTINGS, as_of=NOW, extra={"symbol": "AAPL"})
    rec_asof = e.analyze_as_of(NOW, ctx)

    # "live" path with as_of=None against the same fake providers, then pin the same now
    bundle_live = e._gather(LiveProviderBundle(_provider_resolver(THREE_BUYERS)), as_of=None)
    rec_live = e._process(bundle_live, SETTINGS, as_of=NOW)

    assert rec_asof.almost_equals(rec_live)
    assert rec_asof.signal == OrderRecommendation.BUY
    assert rec_asof.details == rec_live.details


def test_analyze_as_of_sets_gather_lookback_from_settings():
    """analyze_as_of must resolve the gather-time lookback from context.settings BEFORE _gather."""
    captured = {}

    class Spy(FakeInsider):
        def get_insider_transactions(self, symbol, end_date, lookback_days=None,
                                     as_of=None, format_type="dict", **kw):
            captured["lookback_days"] = lookback_days
            return super().get_insider_transactions(
                symbol, end_date, lookback_days=lookback_days,
                as_of=as_of, format_type=format_type)

    spy = Spy(THREE_BUYERS)
    e = FMPInsiderClusterBuy.__new__(FMPInsiderClusterBuy)
    e.id = 1
    # deliberately do NOT pre-set _gather_lookback_days; analyze_as_of must set it
    ctx = BacktestContext(
        providers=LiveProviderBundle(lambda c, n, **k: spy if c == "insider" else FakeOHLCV()),
        settings={**SETTINGS, "lookback_days": 45}, as_of=NOW, extra={"symbol": "AAPL"})
    e.analyze_as_of(NOW, ctx)
    assert captured["lookback_days"] == 45
