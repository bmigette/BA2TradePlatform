"""Market-condition gate on an INTRADAY clock: through the manifest-pinned (mapped) reader.

Bar D then reads the row of the last FINISHED session P(D), never D's own. A 5-minute run cannot
compute features itself (an intraday store has no daily window), so it works only through the
mapped reader; its FIRST session reads a row one session BEFORE the run's start.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.backtest.price_source import AsOfPriceSource


def _wall(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_an_intraday_run_serves_its_first_session_from_the_pinned_manifest(tmp_path, monkeypatch):
    """The preflight accepts a snapshot warmed for exactly the run's window (the warmup writes
    ``[prior(first), last]``) and the first session's gate reads a real row, not missing_session."""
    import ba2_common.config as bc
    from ba2_common.core.market_calendar import prior_regular_session, regular_session_dates
    from ba2_common.core.market_condition_reader import clear_window_coverage_cache
    from app.services.backtest import seam_wiring
    from tests.backtest.test_market_condition_bt_context import _publish_manifest

    clear_window_coverage_cache()
    start, end = date(2025, 6, 3), date(2025, 6, 6)
    bars = regular_session_dates(start, end)
    prior = prior_regular_session(bars[0])
    store, digest = _publish_manifest(tmp_path / "cache",
                                      sessions=tuple(regular_session_dates(prior, bars[-1])))
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store.cache_root))
    intr = AsOfPriceSource(ohlcv_provider=None, interval="5min")
    cfg = {"market_condition_profile": "ohlcv-v1", "market_condition_manifest": digest,
           "execution_interval": "5min", "start_date": start, "end_date": end,
           "enabled_instruments": ["AAA"], "_ga_trial": True}
    resolver = seam_wiring.install_backtest_market_conditions(cfg, intr)
    try:
        assert seam_wiring.check_market_condition_window(cfg, resolver.reader) == []
        intr.set_clock(_wall(bars[0].year, bars[0].month, bars[0].day, 10, 0))
        ctx = resolver(SimpleNamespace(_as_of_date=lambda: bars[0], _price=intr), "AAA", None)
        assert ctx.prior_session == prior
        assert ctx.reader.observe("AAA", ctx.prior_session) is not None, \
            "the first session's row P(first) is missing: it would read missing_session"
        assert resolver.reader.computed == 0 and resolver.reader.mapped_rows >= 1
    finally:
        seam_wiring.clear_backtest_market_conditions()


def test_an_intraday_snapshot_without_the_prior_session_row_is_refused_in_preflight(tmp_path):
    """A snapshot built under the older row rule holds ``first..last`` only: fine for the daily clock
    (bar D reads D) but the intraday clock's first bar reads P(first): refused, not silently empty."""
    from ba2_common.core.market_calendar import regular_session_dates
    from ba2_common.core.market_condition_reader import (
        MappedMarketConditionReader, clear_window_coverage_cache, window_coverage_problems)
    from tests.backtest.test_market_condition_bt_context import _publish_manifest

    from ba2_common.core.market_condition_store import MarketConditionStore
    from ba2_common.core.market_conditions import PROFILES, STATUS_VALID

    clear_window_coverage_cache()
    start, end = date(2025, 6, 3), date(2025, 6, 6)
    sessions = tuple(regular_session_dates(start, end))
    profile = PROFILES["ohlcv-v1"]
    n = len(profile.fields)
    store = MarketConditionStore(tmp_path / "cache")
    rows = [{"session": s, "values": [1.0] * n, "status": [STATUS_VALID] * n,
             "reasons": ["published row"] * n, "window_digest": "sha256:" + "0" * 64,
             "raw_shard_ref": "", "raw_row_lo": 0, "raw_row_hi": 0} for s in sessions]
    entry, _ = store.write_feature_object(profile, "AAA", rows)
    # the coverage record a real warmup writes (first/last session), which is what exposes the hole
    manifest = store.make_manifest(
        profile, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=[entry], raw_objects=[], universe=["AAA"], sessions=list(sessions),
        coverage={"AAA": {"rows": len(rows), "exceptions": [],
                          "first_session": sessions[0].isoformat(),
                          "last_session": sessions[-1].isoformat()}},
        window_start=sessions[0], window_end=sessions[-1])
    digest = store.write_manifest(manifest)
    mapped = MappedMarketConditionReader(store.cache_root, digest, "ohlcv-v1", store=store)
    assert window_coverage_problems(mapped, ["AAA"], start, end) == []            # daily clock: ok
    clear_window_coverage_cache()
    problems = window_coverage_problems(mapped, ["AAA"], start, end, intraday=True)
    assert problems and "AAA" in problems[0], problems


def test_an_intraday_run_without_a_manifest_is_refused_loudly():
    from app.services.backtest.market_condition_bt import BacktestMarketConditionReader

    reader = BacktestMarketConditionReader(AsOfPriceSource(ohlcv_provider=None, interval="5min"),
                                           "ohlcv-v1")
    with pytest.raises(ValueError, match="pin a market-condition manifest"):
        reader._bars("AAA", date(2025, 6, 2))


def test_entry_state_counters_mean_bound_to_the_last_finished_session_on_the_intraday_clock():
    from app.services.backtest.market_condition_bt import attach_entry_states

    rec = {"symbol": "AAA", "session": "2025-06-03", "prior_session": "2025-06-02", "values": {}}
    older = {"symbol": "BBB", "session": "2025-06-03", "prior_session": "2025-05-30", "values": {}}

    def trades(sym):
        return [{"symbol": sym, "entry_time": "2025-06-03 10:05:00", "exit_time": "2025-06-04"}]

    # intraday: the decision on 06-03 read 06-02 (the last finished session): the NORMAL binding
    out = attach_entry_states(trades("AAA"), [rec], intraday=True)
    assert (out["same_session"], out["with_gap"], out["attached"]) == (1, 0, 1)
    # a decision made on an OLDER session (a resting entry that filled later) is the inference
    out = attach_entry_states(trades("BBB"), [older], intraday=True)
    assert (out["same_session"], out["with_gap"]) == (0, 1)
    # daily clock: unchanged (gap 0 = same session), so the same records count as a gap there
    out = attach_entry_states(trades("AAA"), [rec])
    assert (out["same_session"], out["with_gap"]) == (0, 1)
