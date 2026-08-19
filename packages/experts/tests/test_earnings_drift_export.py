"""FMPEarningsDrift.export_symbol_data: post-earnings-drift (PEAD) metric rows.

FMPEarningsDrift's live (as_of=None) _gather only takes the market-wide
earnings-calendar shortcut when providers.fundamentals_details() is a REAL
FMPCompanyDetailsProvider (see the isinstance check in FMPEarningsDrift._gather
and the module docstring's LIVE-ONLY optimization section); a plain fake
details provider (as used in most tests below) always falls through to the
ordinary past_earnings_get(...) per-symbol fetch, so those tests exercise
exactly one deterministic code path regardless of as_of.
test_export_symbol_data_takes_calendar_shortcut_with_real_details_provider is
the exception: it uses a real FMPCompanyDetailsProvider subclass (mirroring
test_earnings_drift_calendar_shortcut.py's FakeFMPDetails fixture) so the
isinstance-gated shortcut branch fires through export_symbol_data itself, not
just via a direct _gather(...) call -- proving analyze_as_of's
self._gather_max_days_since_report = context.settings["max_days_since_report"]
wiring (the one attribute this expert's export wiring introduced solely for
that branch) actually reaches and is used by it.

Like FinnHubRating (see test_finnhub_rating_export.py), FMPEarningsDrift's
live current_price flows through self._get_current_price(symbol), which the
bypass-constructed instance (ExpertDataExportInterface._bypass_instance)
automatically resolves via providers.price_at_date -- UNLESS something has
already customized _get_current_price on the class. FMPEarningsDrift does not
override _get_current_price (verified by reading the file in full), so these
tests inject a fake OHLCV provider and assert its call count (>= 1) to prove
the automatic price-resolution wiring actually fired, not merely that the
fake existed unused (per the Task 6 brief's nice-to-have).

FMPEarningsDrift has exactly ONE Recommendation(...) construction site
(verified by reading _process in full: both the fresh-beat BUY branch and the
"everything else" HOLD branch fall through to the same `return
Recommendation(...)`) -- skip is never set, so rec.skip is always False and
the base 'Recommendation' row from ExpertDataExportInterface is always kept,
never the 'Skipped' fallback.

Report dates are computed relative to datetime.now(timezone.utc) at test run
time (not a fixed calendar date): _gather's fallback fetch path
(past_earnings_get) resolves `end_date = as_of or datetime.now(timezone.utc)`
and _process resolves `now = as_of or datetime.now(timezone.utc)`; since
export_symbol_data's live path always calls with as_of=None, both reads are
genuinely wall-clock, so a fixed report_date fixture would silently rot.

SYMBOL360 always calls export_symbol_data with as_of=None (the live path);
the as_of/lookahead reconstruction path is exercised separately in
test_earnings_drift_gather_process.py and is not re-tested here.
"""
from datetime import datetime, timedelta, timezone
import importlib

import pandas as pd
import pytest

from ba2_experts.FMPEarningsDrift import FMPEarningsDrift
# ba2_experts/__init__.py does `from .FMPEarningsDrift import FMPEarningsDrift`, which shadows
# the `ba2_experts.FMPEarningsDrift` ATTRIBUTE with the class (see
# test_earnings_drift_calendar_shortcut.py) -- importlib.import_module bypasses that and
# returns the actual module object, needed to monkeypatch its fmpsdk/_CALENDAR_CACHE.
_mod = importlib.import_module("ba2_experts.FMPEarningsDrift")
from ba2_providers.fmp_common import TTLCache
from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider

SETTINGS = {"surprise_min_pct": 5.0, "max_days_since_report": 10, "expected_profit_percent": 8.0}


class FakeOHLCV:
    """Tracks call count so a test can ASSERT the fake was actually invoked
    (see FinnHubRating's FakeOHLCV docstring for why this matters: without the
    assertion, a broken _bypass_instance price-override binding would leave
    every one of these tests passing unchanged since current_price never
    surfaces in the surprise/days-since/within-window rows)."""
    def __init__(self, close: float = 150.0):
        self._close = close
        self.calls = 0

    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        self.calls += 1
        return pd.DataFrame({"Close": [self._close]})


class FakeDetails:
    """NOT an FMPCompanyDetailsProvider instance, so FMPEarningsDrift._gather's
    live calendar shortcut (isinstance-gated) never fires here -- every test
    exercises the ordinary past_earnings_get(...) fallback fetch."""
    def __init__(self, earnings):
        self._earnings = earnings

    def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
        return {"earnings": self._earnings}


class FakeFMPDetails(FMPCompanyDetailsProvider):
    """A REAL FMPCompanyDetailsProvider subclass instance (the isinstance check
    in FMPEarningsDrift._gather must pass) so the live calendar-shortcut branch
    is reachable at all. Mirrors test_earnings_drift_calendar_shortcut.py's
    fixture of the same name; deliberately skips super().__init__() (which
    reads FMP_API_KEY from the DB -- no DB in this test). get_past_earnings is
    stubbed as the per-symbol FALLBACK fetch, with a call flag so a test can
    prove the shortcut branch was taken directly (this method never called)
    rather than silently falling through to it."""
    def __init__(self):
        self.api_key = "fake-key"
        self.detail_fetch_called = False

    def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
        self.detail_fetch_called = True
        return {"earnings": []}


def _resolver(details, ohlcv):
    return lambda cat, name, **kw: {"fundamentals_details": details, "ohlcv": ohlcv}[cat]


def _earnings_row(days_ago: int, reported_eps: float, estimated_eps: float) -> dict:
    report_date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()
    return {"report_date": report_date, "reported_eps": reported_eps, "estimated_eps": estimated_eps}


def test_export_symbol_data_fresh_beat_buys_with_populated_rows():
    """A recent (3d ago) 20% EPS beat, well inside the 10-day window and above
    the 5% threshold -> BUY, with surprise/days-since/within-window all
    genuinely computed (not None/n/a)."""
    earnings = [_earnings_row(days_ago=3, reported_eps=1.2, estimated_eps=1.0)]
    ohlcv = FakeOHLCV()
    result = FMPEarningsDrift.export_symbol_data(
        "AAPL", overrides=SETTINGS, providers_resolver=_resolver(FakeDetails(earnings), ohlcv))

    assert result.error is None, result.error
    assert result.overall_signal == "buy"
    assert result.skipped is False
    assert 55.0 <= result.confidence <= 100.0
    # Proves the fake OHLCV provider was actually invoked (the price-override
    # binding fired), not merely constructed and ignored.
    assert ohlcv.calls >= 1

    labels = {m.label for m in result.metrics}
    assert "Recommendation" in labels
    assert {"EPS surprise", "Days since report", "Within drift window"} <= labels

    surprise_row = next(m for m in result.metrics if m.label == "EPS surprise")
    assert surprise_row.value == pytest.approx(20.0)
    assert surprise_row.display == "+20.0%"
    assert surprise_row.signal == "buy"

    days_row = next(m for m in result.metrics if m.label == "Days since report")
    assert days_row.value == 3
    assert days_row.display == "3d ago"

    window_row = next(m for m in result.metrics if m.label == "Within drift window")
    assert window_row.value is True
    assert window_row.display == "Yes"


def test_export_symbol_data_stale_report_holds_but_still_shows_surprise():
    """A 60-day-old 20% beat, outside the 10-day drift window, must fall back to
    HOLD via the freshness gate (not the surprise threshold) -- yet the surprise
    figure itself is still a real, computed number (evaluate_earnings_drift
    populates surprise_pct/days_since/report_date BEFORE the freshness check),
    only the 'within window' verdict flips to No."""
    earnings = [_earnings_row(days_ago=60, reported_eps=1.2, estimated_eps=1.0)]
    ohlcv = FakeOHLCV()
    result = FMPEarningsDrift.export_symbol_data(
        "AAPL", overrides=SETTINGS, providers_resolver=_resolver(FakeDetails(earnings), ohlcv))

    assert result.error is None, result.error
    assert result.overall_signal == "hold"
    assert result.skipped is False
    assert result.confidence == pytest.approx(10.0)
    assert ohlcv.calls >= 1

    surprise_row = next(m for m in result.metrics if m.label == "EPS surprise")
    assert surprise_row.value == pytest.approx(20.0)
    assert surprise_row.display == "+20.0%"

    days_row = next(m for m in result.metrics if m.label == "Days since report")
    assert days_row.value == 60
    assert days_row.display == "60d ago"

    window_row = next(m for m in result.metrics if m.label == "Within drift window")
    assert window_row.value is False
    assert window_row.display == "No"


def test_export_symbol_data_no_earnings_data_holds_with_na_rows():
    """No earnings rows at all -> evaluate_earnings_drift(None, ...) -> HOLD with
    every derived field genuinely None (not a computed 0.0/0d) -- must render as
    n/a everywhere, and must NOT surface as the base class's 'Skipped' row
    (FMPEarningsDrift has no skip path)."""
    ohlcv = FakeOHLCV()
    result = FMPEarningsDrift.export_symbol_data(
        "AAPL", overrides=SETTINGS, providers_resolver=_resolver(FakeDetails([]), ohlcv))

    assert result.error is None, result.error
    assert result.overall_signal == "hold"
    assert result.skipped is False
    assert ohlcv.calls >= 1

    labels = {m.label for m in result.metrics}
    assert "Skipped" not in labels
    assert "Recommendation" in labels

    surprise_row = next(m for m in result.metrics if m.label == "EPS surprise")
    assert surprise_row.value is None
    assert surprise_row.display == "n/a"
    assert surprise_row.signal is None

    days_row = next(m for m in result.metrics if m.label == "Days since report")
    assert days_row.value is None
    assert days_row.display == "n/a"

    window_row = next(m for m in result.metrics if m.label == "Within drift window")
    assert window_row.value is None
    assert window_row.display == "n/a"


def test_export_symbol_data_takes_calendar_shortcut_with_real_details_provider(monkeypatch):
    """export_symbol_data's default as_of=None, combined with a GENUINE
    FMPCompanyDetailsProvider instance, must reach and correctly use the live
    calendar-shortcut branch in _gather -- not merely fall back to the
    per-symbol detail fetch (which _gather's broad except-Exception around the
    shortcut would silently do if analyze_as_of's
    self._gather_max_days_since_report = context.settings["max_days_since_report"]
    wiring were missing or misnamed, since that attribute is read ONLY inside
    this branch and only set by analyze_as_of for the export path).

    Isolated from test_earnings_drift_calendar_shortcut.py's cache state (and
    from the other tests in this file, none of which touch it) by swapping in
    a fresh TTLCache for the duration of this test only."""
    monkeypatch.setattr(_mod, "_CALENDAR_CACHE", TTLCache(_mod._CALENDAR_CACHE_TTL_SECONDS))
    report_date = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()

    def fake_calendar(apikey, from_date, to_date):
        return [{"symbol": "AAPL", "date": report_date, "eps": 1.2, "epsEstimated": 1.0}]

    monkeypatch.setattr(_mod.fmpsdk, "earning_calendar", fake_calendar)

    details = FakeFMPDetails()
    ohlcv = FakeOHLCV()
    result = FMPEarningsDrift.export_symbol_data(
        "AAPL", overrides=SETTINGS, providers_resolver=_resolver(details, ohlcv))

    assert result.error is None, result.error
    # The real regression guard: proves the calendar row was consumed directly
    # (shortcut branch) rather than the per-symbol fallback fetch silently
    # picking up the slack with a correct-looking answer.
    assert details.detail_fetch_called is False
    assert result.overall_signal == "buy"
    assert result.skipped is False
    assert ohlcv.calls >= 1

    surprise_row = next(m for m in result.metrics if m.label == "EPS surprise")
    assert surprise_row.value == pytest.approx(20.0)
    assert surprise_row.display == "+20.0%"
    assert surprise_row.signal == "buy"

    days_row = next(m for m in result.metrics if m.label == "Days since report")
    assert days_row.value == 3
    assert days_row.display == "3d ago"

    window_row = next(m for m in result.metrics if m.label == "Within drift window")
    assert window_row.value is True
    assert window_row.display == "Yes"
