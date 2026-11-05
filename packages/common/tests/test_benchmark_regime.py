"""Benchmark (SPY) regime for the entry gates: mode tables, the pure LIVE classifier, the
precomputed BACKTEST calendar (asserted equal to the pure one for EVERY day), the knowable-close cut
and the publish/read seam.

The calendar re-implements the classifier arithmetic for speed, exactly like
``regime_overlay.StressedCalendar``; ``test_calendar_equals_pure_classifier_every_day`` is what keeps
the two honest, so a change to one without the other fails there.
"""
import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_common.core import benchmark_regime as br
from ba2_common.core import market_regime as mr


def _series(n, seed=7):
    """Like the regime-overlay synthetic closes, but with drifting trend phases so ALL FOUR trend
    states and all THREE vol states occur."""
    rng = random.Random(seed)
    px, d = 100.0, date(2015, 1, 1)
    dates, closes = [], []
    for i in range(n):
        drift = 0.0012 if i < 400 else (-0.0015 if i < 800 else 0.0010)
        sigma = 0.035 if 600 <= i < 700 else 0.007
        px *= math.exp(rng.gauss(drift, sigma))
        d += timedelta(days=1)
        dates.append(d)
        closes.append(px)
    return dates, closes


def test_calendar_equals_pure_classifier_every_day():
    dates, closes = _series(1100)
    cal = br.BenchmarkRegimeCalendar(dates, closes)
    for i, d in enumerate(dates):
        pure = br.classify_benchmark_regime(closes[:i + 1])
        got = cal.at(d)
        assert (got.trend, got.vol) == (pure.trend, pure.vol), f"diverged on day {i} ({d})"


def test_calendar_vol_stressed_equals_the_overlay_calendar():
    from ba2_common.core.regime_overlay import StressedCalendar
    dates, closes = _series(1100)
    new, old = br.BenchmarkRegimeCalendar(dates, closes), StressedCalendar(dates, closes)
    for d in dates:
        assert (new.at(d).vol == mr.STRESSED) == bool(old.at(d))


def test_sample_is_not_vacuous():
    dates, closes = _series(1100)
    cal = br.BenchmarkRegimeCalendar(dates, closes)
    assert {cal.at(d).trend for d in dates} >= {"up", "pullback", "bear_rally", "down", None}
    assert {cal.at(d).vol for d in dates} >= {"calm", "normal", "stressed", None}


def test_calendar_is_causal():
    dates, closes = _series(1100)
    full = br.BenchmarkRegimeCalendar(dates, closes)
    cut = br.BenchmarkRegimeCalendar(dates[:900], closes[:900])
    for d in dates[:900]:
        assert (full.at(d).trend, full.at(d).vol) == (cut.at(d).trend, cut.at(d).vol)


def test_calendar_holds_between_days_and_none_before_first():
    dates, closes = _series(700)
    cal = br.BenchmarkRegimeCalendar(dates, closes)
    before = cal.at(dates[0] - timedelta(days=5))
    assert (before.trend, before.vol) == (None, None) and before.trend_reason
    intraday = cal.at(datetime.combine(dates[650], datetime.min.time()) + timedelta(hours=7))
    same = cal.at(dates[650])
    assert (intraday.trend, intraday.vol) == (same.trend, same.vol)
    assert same.trend is not None


def test_calendar_ignores_none_closes_like_the_pure_functions():
    dates, closes = _series(300)
    closes = list(closes)
    closes[100] = None
    cal = br.BenchmarkRegimeCalendar(dates, closes)
    kept = [c for c in closes if c is not None]
    assert cal.at(dates[-1]).trend == br.classify_benchmark_regime(kept).trend


def test_insufficient_history_names_the_reason():
    out = br.classify_benchmark_regime([100.0 + i for i in range(50)])
    assert out.trend is None and "insufficient" in out.trend_reason
    assert out.vol is None and "insufficient" in out.vol_reason


def test_modes_cover_exactly_the_spec_and_codes_are_stable():
    assert list(br.TREND_MODE_CODES) == ["up", "pullback", "bear_rally", "down", "above_200", "below_200"]
    assert list(br.TREND_MODE_CODES.values()) == [1, 2, 3, 4, 5, 6]
    assert list(br.VOL_MODE_CODES) == ["calm", "normal", "stressed", "not_stressed", "not_calm"]
    assert list(br.VOL_MODE_CODES.values()) == [1, 2, 3, 4, 5]
    assert (br.TREND_FIELD, br.VOL_FIELD, br.BENCHMARK_SYMBOL) == ("spy_trend_regime", "spy_vol_regime", "SPY")


@pytest.mark.parametrize("mode,members", [("above_200", {"up", "pullback"}), ("below_200", {"bear_rally", "down"})])
def test_trend_set_modes_partition_the_states(mode, members):
    code = br.TREND_MODE_CODES[mode]
    for st in ("up", "pullback", "bear_rally", "down"):
        assert br.state_matches(br.TREND_FIELD, code, st) == (st in members)


def test_identity_modes_match_only_their_own_state():
    for mode, code in br.TREND_MODE_CODES.items():
        if mode in ("above_200", "below_200"):
            continue
        for st in ("up", "pullback", "bear_rally", "down"):
            assert br.state_matches(br.TREND_FIELD, code, st) == (st == mode)
    for mode in ("calm", "normal", "stressed"):
        for st in ("calm", "normal", "stressed"):
            assert br.state_matches(br.VOL_FIELD, br.VOL_MODE_CODES[mode], st) == (st == mode)


def test_vol_set_modes():
    c = br.VOL_MODE_CODES
    for st, ns, nc in (("calm", True, False), ("normal", True, True), ("stressed", False, True)):
        assert br.state_matches(br.VOL_FIELD, c["not_stressed"], st) is ns
        assert br.state_matches(br.VOL_FIELD, c["not_calm"], st) is nc


def test_unknown_state_never_matches_any_mode():
    for code in br.TREND_MODE_CODES.values():
        assert br.state_matches(br.TREND_FIELD, code, None) is False
    for code in br.VOL_MODE_CODES.values():
        assert br.state_matches(br.VOL_FIELD, code, None) is False


def test_mode_for_code_and_unknown_code_raises():
    assert br.mode_for_code(br.TREND_FIELD, 5) == "above_200"
    assert br.mode_for_code(br.VOL_FIELD, 4) == "not_stressed"
    with pytest.raises(ValueError):
        br.mode_for_code(br.TREND_FIELD, 99)
    with pytest.raises(ValueError):
        br.state_matches(br.TREND_FIELD, 99, "up")
    with pytest.raises(ValueError):
        br.mode_for_code("nope", 1)


def test_knowable_closes_drops_the_partial_today_bar():
    dates = [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]   # Wed..Fri sessions
    closes = [1.0, 2.0, 3.0]
    during = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)            # 11:00 NY: Friday unfinished
    after = datetime(2026, 10, 23, 21, 30, tzinfo=timezone.utc)            # 17:30 NY: finished
    assert br.knowable_closes(dates, closes, during)[1] == [1.0, 2.0]
    assert br.knowable_closes(dates, closes, after)[1] == [1.0, 2.0, 3.0]
    sat = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)
    assert br.knowable_closes(dates, closes, sat)[1] == [1.0, 2.0, 3.0]
    assert br.knowable_closes(dates, closes, during)[0] == dates[:2]


def test_knowable_closes_refuses_non_increasing_dates():
    now = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        br.knowable_closes([date(2026, 10, 22), date(2026, 10, 22)], [1.0, 2.0], now)


def test_seam_default_set_reset_and_failure():
    br.reset_benchmark_regime()
    assert br.get_benchmark_regime() is None and br.get_benchmark_regime_failure() is None
    r = br.BenchmarkRegime("up", "calm")
    br.set_benchmark_regime(r)
    br.set_benchmark_regime_failure("SPY fetch failed")
    assert br.get_benchmark_regime() is r
    assert br.get_benchmark_regime_failure() == "SPY fetch failed"
    br.reset_benchmark_regime()
    assert br.get_benchmark_regime() is None and br.get_benchmark_regime_failure() is None
