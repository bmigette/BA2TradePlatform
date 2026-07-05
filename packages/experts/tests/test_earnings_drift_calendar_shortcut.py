"""LIVE-ONLY earnings-calendar shortcut for FMPEarningsDrift._gather.

Proves: a symbol absent from the bulk calendar skips the per-symbol detail fetch entirely
(no signal); a symbol present WITH both actual+estimated EPS uses the calendar row directly
(no per-symbol fetch); a symbol present but missing the estimate falls through to the
per-symbol fetch; the calendar fetch is shared (cached) across symbols; backtest (as_of set)
never touches the calendar at all; and any calendar-path failure falls back safely.
"""
from datetime import datetime, timezone

import importlib

import pandas as pd
import pytest

from ba2_experts.FMPEarningsDrift import FMPEarningsDrift

# ba2_experts/__init__.py does `from .FMPEarningsDrift import FMPEarningsDrift`, which shadows
# the `ba2_experts.FMPEarningsDrift` ATTRIBUTE with the class -- `import ... as mod` would
# silently bind the class, not the submodule. importlib.import_module bypasses that shadowing
# and returns the actual module object from sys.modules.
mod = importlib.import_module("ba2_experts.FMPEarningsDrift")
from ba2_common.core.backtest_context import LiveProviderBundle
from ba2_providers.fmp_common import TTLCache
from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)


class FakeOHLCV:
    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        return pd.DataFrame({"Close": [100.0]})


class FakeFMPDetails(FMPCompanyDetailsProvider):
    """A real FMPCompanyDetailsProvider subclass instance (isinstance check must pass)
    with get_past_earnings stubbed so the per-symbol fallback is observable/spy-able.
    Deliberately skips super().__init__() (which reads FMP_API_KEY from the DB)."""
    def __init__(self, per_symbol_row=None):
        self.api_key = "fake-key"
        self._per_symbol_row = per_symbol_row
        self.detail_fetch_called = False

    def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
        self.detail_fetch_called = True
        earnings = [self._per_symbol_row] if self._per_symbol_row else []
        return {"earnings": earnings}


@pytest.fixture(autouse=True)
def fresh_calendar_cache(monkeypatch):
    """Each test gets an empty TTLCache so calendar fetches never leak across tests."""
    monkeypatch.setattr(mod, "_CALENDAR_CACHE", TTLCache(mod._CALENDAR_CACHE_TTL_SECONDS))


def _expert(max_days=10):
    e = FMPEarningsDrift.__new__(FMPEarningsDrift)
    e.id = 1
    e._gather_symbol = "AAPL"
    e._gather_max_days_since_report = max_days
    import logging
    e.logger = logging.getLogger("test")
    return e


def _provider_bundle(details):
    return LiveProviderBundle(
        lambda cat, name, **kw: details if cat == "fundamentals_details" else FakeOHLCV())


def test_symbol_absent_from_calendar_skips_detail_fetch(monkeypatch):
    calls = {"n": 0}

    def fake_calendar(apikey, from_date, to_date):
        calls["n"] += 1
        return [{"symbol": "MSFT", "date": "2026-06-10", "eps": 1.0, "epsEstimated": 0.9}]

    monkeypatch.setattr(mod.fmpsdk, "earning_calendar", fake_calendar)
    details = FakeFMPDetails()
    e = _expert()
    bundle = e._gather(_provider_bundle(details), as_of=None)

    assert bundle["latest_earnings"] is None
    assert details.detail_fetch_called is False
    assert calls["n"] == 1


def test_symbol_present_with_full_eps_uses_calendar_row_directly(monkeypatch):
    def fake_calendar(apikey, from_date, to_date):
        return [{"symbol": "AAPL", "date": "2026-06-10", "eps": 1.2, "epsEstimated": 1.0}]

    monkeypatch.setattr(mod.fmpsdk, "earning_calendar", fake_calendar)
    details = FakeFMPDetails()
    e = _expert()
    bundle = e._gather(_provider_bundle(details), as_of=None)

    assert bundle["latest_earnings"] == {
        "report_date": "2026-06-10", "reported_eps": 1.2, "estimated_eps": 1.0,
    }
    assert details.detail_fetch_called is False


def test_symbol_present_but_missing_estimate_falls_back_to_detail_fetch(monkeypatch):
    def fake_calendar(apikey, from_date, to_date):
        return [{"symbol": "AAPL", "date": "2026-06-10", "eps": 1.2, "epsEstimated": None}]

    monkeypatch.setattr(mod.fmpsdk, "earning_calendar", fake_calendar)
    per_symbol_row = {"report_date": "2026-06-10", "reported_eps": 1.2, "estimated_eps": 1.05}
    details = FakeFMPDetails(per_symbol_row=per_symbol_row)
    e = _expert()
    bundle = e._gather(_provider_bundle(details), as_of=None)

    assert details.detail_fetch_called is True
    assert bundle["latest_earnings"] == per_symbol_row


def test_calendar_fetch_shared_across_symbols_via_ttl_cache(monkeypatch):
    calls = {"n": 0}

    def fake_calendar(apikey, from_date, to_date):
        calls["n"] += 1
        return [
            {"symbol": "AAPL", "date": "2026-06-10", "eps": 1.2, "epsEstimated": 1.0},
            {"symbol": "MSFT", "date": "2026-06-11", "eps": 2.0, "epsEstimated": 1.8},
        ]

    monkeypatch.setattr(mod.fmpsdk, "earning_calendar", fake_calendar)
    details = FakeFMPDetails()

    e1 = _expert()
    e1._gather_symbol = "AAPL"
    e1._gather(_provider_bundle(details), as_of=None)

    e2 = _expert()
    e2._gather_symbol = "MSFT"
    e2._gather(_provider_bundle(details), as_of=None)

    assert calls["n"] == 1  # one bulk fetch served both symbols


def test_calendar_dedupes_to_latest_row_per_symbol(monkeypatch):
    def fake_calendar(apikey, from_date, to_date):
        return [
            {"symbol": "AAPL", "date": "2026-05-01", "eps": 0.5, "epsEstimated": 0.4},
            {"symbol": "AAPL", "date": "2026-06-10", "eps": 1.2, "epsEstimated": 1.0},
        ]

    monkeypatch.setattr(mod.fmpsdk, "earning_calendar", fake_calendar)
    details = FakeFMPDetails()
    e = _expert()
    bundle = e._gather(_provider_bundle(details), as_of=None)

    assert bundle["latest_earnings"]["report_date"] == "2026-06-10"


def test_calendar_failure_falls_back_to_detail_fetch(monkeypatch):
    def broken_calendar(apikey, from_date, to_date):
        raise RuntimeError("FMP rate limit")

    monkeypatch.setattr(mod.fmpsdk, "earning_calendar", broken_calendar)
    per_symbol_row = {"report_date": "2026-06-10", "reported_eps": 1.2, "estimated_eps": 1.0}
    details = FakeFMPDetails(per_symbol_row=per_symbol_row)
    e = _expert()
    bundle = e._gather(_provider_bundle(details), as_of=None)

    assert details.detail_fetch_called is True
    assert bundle["latest_earnings"] == per_symbol_row


def test_backtest_path_never_touches_calendar(monkeypatch):
    calls = {"n": 0}

    def fake_calendar(apikey, from_date, to_date):
        calls["n"] += 1
        return []

    monkeypatch.setattr(mod.fmpsdk, "earning_calendar", fake_calendar)
    per_symbol_row = {"report_date": "2026-06-10", "reported_eps": 1.2, "estimated_eps": 1.0}
    details = FakeFMPDetails(per_symbol_row=per_symbol_row)
    e = _expert()
    bundle = e._gather(_provider_bundle(details), as_of=NOW)  # backtest: as_of is set

    assert calls["n"] == 0
    assert details.detail_fetch_called is True
    assert bundle["latest_earnings"] == per_symbol_row


def test_non_fmp_provider_falls_back_to_detail_fetch(monkeypatch):
    """A non-FMP details provider (e.g. AlphaVantage/YFinance) must never hit the FMP-only
    calendar shortcut — isinstance guard skips it and goes straight to the detail fetch."""
    calls = {"n": 0}

    def fake_calendar(apikey, from_date, to_date):
        calls["n"] += 1
        return []

    monkeypatch.setattr(mod.fmpsdk, "earning_calendar", fake_calendar)

    class NonFMPDetails:
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
            return {"earnings": [{"report_date": "2026-06-10", "reported_eps": 1.2,
                                  "estimated_eps": 1.0, "surprise_percent": 20.0}]}

    e = _expert()
    bundle = e._gather(_provider_bundle(NonFMPDetails()), as_of=None)

    assert calls["n"] == 0
    assert bundle["latest_earnings"]["reported_eps"] == 1.2
