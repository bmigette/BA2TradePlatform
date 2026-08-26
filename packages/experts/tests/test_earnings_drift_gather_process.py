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


def test_process_static_mode_ignores_dynamic_scale():
    """expected_profit_mode='static' must reproduce today's flat-percent behaviour exactly,
    even when dynamic_scale is set to something nonzero (it should simply be ignored)."""
    e = _expert()
    settings = {**SETTINGS, "expected_profit_mode": "static", "dynamic_scale": 5.0}
    bundle = e._gather(LiveProviderBundle(_get_provider), as_of=NOW)
    rec = e._process(bundle, settings, as_of=NOW)
    assert rec.signal == OrderRecommendation.BUY
    assert rec.expected_profit_percent == 8.0


def test_process_dynamic_mode_zero_scale_matches_static():
    """dynamic_scale=0.0 must be numerically identical to 'static' -- the documented invariant."""
    e = _expert()
    settings = {**SETTINGS, "expected_profit_mode": "dynamic", "dynamic_scale": 0.0}
    bundle = e._gather(LiveProviderBundle(_get_provider), as_of=NOW)
    rec = e._process(bundle, settings, as_of=NOW)
    assert rec.expected_profit_percent == 8.0


def test_process_dynamic_mode_scales_with_excess_surprise():
    """dynamic mode: expected_profit = base + scale * max(0, surprise_pct - surprise_min_pct).
    Fixture surprise is 20.0%, surprise_min_pct is 5.0% -> excess 15.0%."""
    e = _expert()
    settings = {**SETTINGS, "expected_profit_mode": "dynamic", "dynamic_scale": 0.5}
    bundle = e._gather(LiveProviderBundle(_get_provider), as_of=NOW)
    rec = e._process(bundle, settings, as_of=NOW)
    assert rec.expected_profit_percent == 8.0 + 0.5 * (20.0 - 5.0)  # == 15.5


def test_process_dynamic_mode_hold_still_zero():
    """A HOLD (surprise below threshold) must stay expected_profit=0 regardless of mode --
    dynamic scaling only ever applies to an actual BUY signal."""
    class WeakDetails:
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
            return {"earnings": [{"report_date": "2026-06-10", "reported_eps": 1.01,
                                  "estimated_eps": 1.0, "surprise_percent": 1.0}]}

    e = _expert()
    settings = {**SETTINGS, "expected_profit_mode": "dynamic", "dynamic_scale": 2.0}
    bundle = e._gather(
        LiveProviderBundle(lambda c, n, **k: WeakDetails() if c == "fundamentals_details" else FakeOHLCV()),
        as_of=NOW)
    rec = e._process(bundle, settings, as_of=NOW)
    assert rec.signal == OrderRecommendation.HOLD
    assert rec.expected_profit_percent == 0.0


def test_process_dynamic_mode_caps_expected_profit_on_near_zero_estimate_blowup():
    """Regression for the 2026-08-26 live incident (instance 6, SA): reported 0.76 vs estimated
    -0.04 is a genuine earnings beat, but the near-zero/negative estimate denominator makes
    surprise_pct a mathematically-correct but meaningless 2000% -- 'dynamic' mode must not be
    free to scale a price target off that number. Fixture mirrors the exact real numbers:
    base=3.0, dynamic_scale=2.0, surprise_min_pct=13.0 -> uncapped would be 3.0 + 2.0*(2000-13)
    = 3977.0, exactly what went live. The default cap (100.0, unset in settings here) must
    clamp it."""
    class BlowupDetails:
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
            return {"earnings": [{"report_date": "2026-06-10", "reported_eps": 0.76,
                                  "estimated_eps": -0.04, "surprise_percent": 2000.0}]}

    e = _expert()
    settings = {"surprise_min_pct": 13.0, "max_days_since_report": 30,
               "expected_profit_percent": 3.0, "expected_profit_mode": "dynamic",
               "dynamic_scale": 2.0}
    bundle = e._gather(
        LiveProviderBundle(lambda c, n, **k: BlowupDetails() if c == "fundamentals_details" else FakeOHLCV()),
        as_of=NOW)
    rec = e._process(bundle, settings, as_of=NOW)
    assert rec.signal == OrderRecommendation.BUY
    assert rec.expected_profit_percent == 100.0  # capped, not 3977.0


def test_process_dynamic_mode_explicit_cap_below_default():
    """max_expected_profit_percent is itself GA-tunable (20-500) -- a value below the 100.0
    default must be honoured, not just the default."""
    class BlowupDetails:
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
            return {"earnings": [{"report_date": "2026-06-10", "reported_eps": 0.76,
                                  "estimated_eps": -0.04, "surprise_percent": 2000.0}]}

    e = _expert()
    settings = {"surprise_min_pct": 13.0, "max_days_since_report": 30,
               "expected_profit_percent": 3.0, "expected_profit_mode": "dynamic",
               "dynamic_scale": 2.0, "max_expected_profit_percent": 50.0}
    bundle = e._gather(
        LiveProviderBundle(lambda c, n, **k: BlowupDetails() if c == "fundamentals_details" else FakeOHLCV()),
        as_of=NOW)
    rec = e._process(bundle, settings, as_of=NOW)
    assert rec.expected_profit_percent == 50.0


def test_process_cap_is_a_noop_below_the_ceiling():
    """The ordinary case (this file's existing fixture, well under any sane cap) must be
    completely unaffected -- proven by the pre-existing dynamic-mode test's own expected value,
    re-asserted here with the cap setting explicitly present."""
    e = _expert()
    settings = {**SETTINGS, "expected_profit_mode": "dynamic", "dynamic_scale": 0.5,
               "max_expected_profit_percent": 100.0}
    bundle = e._gather(LiveProviderBundle(_get_provider), as_of=NOW)
    rec = e._process(bundle, settings, as_of=NOW)
    assert rec.expected_profit_percent == 8.0 + 0.5 * (20.0 - 5.0)  # == 15.5, unchanged


def test_process_static_mode_also_capped():
    """A misconfigured flat 'static' expected_profit_percent above the ceiling is capped too --
    the ceiling is a safety backstop, not a dynamic-mode-only feature."""
    e = _expert()
    settings = {**SETTINGS, "expected_profit_percent": 250.0, "expected_profit_mode": "static",
               "max_expected_profit_percent": 100.0}
    bundle = e._gather(LiveProviderBundle(_get_provider), as_of=NOW)
    rec = e._process(bundle, settings, as_of=NOW)
    assert rec.expected_profit_percent == 100.0


def test_process_missing_cap_setting_defaults_to_100():
    """Existing deployed instances (e.g. instance 6, pre-fix) have no
    max_expected_profit_percent key in their settings row at all -- must fall back to the
    class default (100.0), not raise KeyError. This IS the fix for the live incident: no
    settings migration required, the very next analysis run is capped."""
    class BlowupDetails:
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods, format_type, **kw):
            return {"earnings": [{"report_date": "2026-06-10", "reported_eps": 0.76,
                                  "estimated_eps": -0.04, "surprise_percent": 2000.0}]}

    e = _expert()
    settings = {"surprise_min_pct": 13.0, "max_days_since_report": 30,
               "expected_profit_percent": 3.0, "expected_profit_mode": "dynamic",
               "dynamic_scale": 2.0}  # no max_expected_profit_percent key at all
    bundle = e._gather(
        LiveProviderBundle(lambda c, n, **k: BlowupDetails() if c == "fundamentals_details" else FakeOHLCV()),
        as_of=NOW)
    rec = e._process(bundle, settings, as_of=NOW)
    assert rec.expected_profit_percent == 100.0


def test_process_missing_mode_and_scale_default_to_static():
    """Settings dicts built without the new (optional) keys -- e.g. older/hand-built configs,
    existing tests/fixtures -- must fall back to 'static' behaviour, not raise KeyError."""
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_get_provider), as_of=NOW)
    rec = e._process(bundle, SETTINGS, as_of=NOW)  # SETTINGS has neither key
    assert rec.expected_profit_percent == 8.0


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
