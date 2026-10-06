"""AtmIvHistoryProvider: lazy fill, budgets, tombstones, atomicity, concurrency (all fakes)."""
import math
import os
import threading
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import market_calendar
from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H
from tests.atm_iv_fakes import (NOW, FakeWorld, FakeRate, FakeSpots, make_provider,
                                reset_module_state)

END = date(2026, 3, 6)
LOOK = 60          # calendar days: ~41 sessions, quick


@pytest.fixture(autouse=True)
def _clean():
    reset_module_state()
    yield
    reset_module_state()


def _row_truth_check(prov, world, sym="TEST"):
    df = prov.read_store(sym)
    ok = df[df["reason"] == "ok"]
    assert len(ok) > 0
    for r in ok.itertuples():
        d = r.session_date.date()
        true = world.true_iv(d, r.strike, r.expiry.date())
        assert abs(r.iv - true) < 1e-4, (d, r.occ, r.iv, true)
        assert 20 <= r.dte <= 45
        assert r.provenance == H.PROVENANCE_DERIVED and r.method_version == H.METHOD_VERSION


def _complete(prov, jobs, **kw):
    res = prov.get_atm_iv_series("TEST", END, LOOK, **kw)
    for j in list(jobs):
        jobs.remove(j)
        j()
    return res


def test_cold_fill_is_partial_filling_then_completes_with_zero_api_calls(tmp_path):
    prov, world, bars, lister, jobs = make_provider(tmp_path)
    res = prov.get_atm_iv_series("TEST", END, LOOK, max_api_calls=2)
    assert res.status == H.STATUS_FILLING          # cold listing alone eats the foreground budget
    assert res.covered < res.expected and len(jobs) == 1
    # a second caller while the fill is owned: partial, no new job, no API call
    calls_before = prov.api_calls
    again = prov.get_atm_iv_series("TEST", END, LOOK)
    assert again.status == H.STATUS_FILLING and len(jobs) == 1 and prov.api_calls == calls_before
    jobs.pop()()                                    # the background job
    done = prov.get_atm_iv_series("TEST", END, LOOK)
    assert done.status == H.STATUS_COMPLETE and done.covered == done.expected > 30
    n_calls = prov.api_calls
    third = prov.get_atm_iv_series("TEST", END, LOOK)
    assert third.status == H.STATUS_COMPLETE and prov.api_calls == n_calls   # ZERO calls
    assert third.provenance == {H.PROVENANCE_DERIVED: third.covered}
    _row_truth_check(prov, world)


def test_one_new_day_appends_exactly_one_row(tmp_path):
    prov, world, bars, lister, jobs = make_provider(tmp_path)
    prov.get_atm_iv_series("TEST", END, LOOK)
    jobs.pop()()
    before = prov.read_store("TEST")
    # next Monday 10:00 ET: last completed session = Mon 2026-03-09 is today -> use Tue
    world.sessions = market_calendar.regular_session_dates(date(2025, 1, 2), date(2026, 3, 9))
    prov._now = lambda: datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)
    prov.get_atm_iv_series  # noqa
    calls_before = prov.api_calls
    res = prov.get_atm_iv_series("TEST", date(2026, 3, 9), LOOK, max_api_calls=2)
    after = prov.read_store("TEST")
    assert res.status == H.STATUS_COMPLETE
    assert len(after) == len(before) + 1
    assert after["session_date"].max().date() == date(2026, 3, 9)
    assert prov.api_calls - calls_before <= 2        # one listing refresh + one bars batch


def test_thin_day_is_a_tombstone_and_nothing_is_carried_forward(tmp_path):
    world = FakeWorld()
    thin = date(2026, 2, 24)
    world.no_bar_days = {thin}
    prov, world, bars, lister, jobs = make_provider(tmp_path, world)
    prov.get_atm_iv_series("TEST", END, LOOK)
    jobs.pop()()
    res = prov.get_atm_iv_series("TEST", END, LOOK)
    df = prov.read_store("TEST")
    row = df[df["session_date"] == pd.Timestamp(thin)].iloc[0]
    assert row["reason"] == "no_bar" and math.isnan(row["iv"])
    assert thin not in res.values and res.tombstones == 1
    assert res.covered == res.expected - 1 and res.status == H.STATUS_COMPLETE


def test_low_volume_close_is_a_tombstone(tmp_path):
    world = FakeWorld()
    low = date(2026, 2, 25)
    world.volume_by_day = {low: H.MIN_BAR_VOLUME - 1}
    prov, world, bars, lister, jobs = make_provider(tmp_path, world)
    prov.get_atm_iv_series("TEST", END, LOOK)
    jobs.pop()()
    df = prov.read_store("TEST")
    row = df[df["session_date"] == pd.Timestamp(low)].iloc[0]
    assert row["reason"] == "low_volume" and math.isnan(row["iv"]) and row["bar_volume"] == 4
    # exactly at the threshold is a value
    world2 = FakeWorld()
    world2.volume_by_day = {low: H.MIN_BAR_VOLUME}
    prov2, *_r = make_provider(tmp_path / "b", world2)
    prov2.get_atm_iv_series("TEST", END, LOOK)
    _r[-1].pop()()
    r2 = prov2.read_store("TEST")
    assert r2[r2["session_date"] == pd.Timestamp(low)].iloc[0]["reason"] == "ok"


def test_missing_fred_or_spot_is_unavailable_never_a_default(tmp_path):
    def no_rate(a, b):
        raise H.AtmIvUnavailable("risk-free rate unavailable: no cache")
    prov, world, bars, lister, jobs = make_provider(tmp_path, rate_source=no_rate)
    res = prov.get_atm_iv_series("TEST", END, LOOK)
    assert res.status == H.STATUS_UNAVAILABLE and "risk-free" in res.reason
    assert prov.api_calls == 0 and not jobs and not os.path.exists(prov.store_path("TEST"))

    class NoSpot:
        def spots(self, s, a, b):
            raise H.SpotUnavailable("no OHLCV")
    prov2, *_ = make_provider(tmp_path / "s", spot_source=NoSpot())
    res2 = prov2.get_atm_iv_series("TEST", END, LOOK)
    assert res2.status == H.STATUS_UNAVAILABLE and prov2.api_calls == 0


def test_sessions_without_spot_are_not_computed_and_not_filling(tmp_path):
    world = FakeWorld()
    hole = date(2026, 2, 26)
    world.no_spot_days = {hole}
    prov, world, bars, lister, jobs = make_provider(tmp_path, world)
    prov.get_atm_iv_series("TEST", END, LOOK)
    jobs.pop()()
    res = prov.get_atm_iv_series("TEST", END, LOOK)
    assert hole not in res.values and res.unresolved == 1
    assert res.status == H.STATUS_COMPLETE          # nothing to wait for


def test_429_in_foreground_hands_off_and_background_retries(tmp_path):
    sleeps = []
    prov, world, bars, lister, jobs = make_provider(tmp_path, sleep=sleeps.append)
    # warm the listing so the foreground can reach the bars call
    prov.get_atm_iv_series("TEST", END, LOOK)
    jobs.pop()()
    # new day, the first bars call gets a 429
    world.sessions = market_calendar.regular_session_dates(date(2025, 1, 2), date(2026, 3, 9))
    prov._now = lambda: datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)
    bars.fail_first = len(bars.calls) + 1
    res = prov.get_atm_iv_series("TEST", date(2026, 3, 9), LOOK, max_api_calls=3)
    assert res.status == H.STATUS_FILLING and len(jobs) == 1
    jobs.pop()()                                     # background: blocking budget, retries
    assert prov.get_atm_iv_series("TEST", date(2026, 3, 9), LOOK).status == H.STATUS_COMPLETE


def test_persistent_failure_warns_once_and_cools_down(tmp_path, caplog):
    prov, world, bars, lister, jobs = make_provider(tmp_path)
    bars.fail_always = True
    prov.get_atm_iv_series("TEST", END, LOOK)        # foreground: listing eats the budget
    messages = []
    import ba2_trade_platform.modules.dataproviders.options.atm_iv_history as mod
    orig = mod.logger.warning
    mod.logger.warning = lambda m, *a, **k: messages.append(m)
    try:
        jobs.pop()()
    finally:
        mod.logger.warning = orig
    giving_up = [m for m in messages if "giving up" in m]
    assert len(giving_up) == 1
    res = prov.get_atm_iv_series("TEST", END, LOOK)
    assert res.status == H.STATUS_UNAVAILABLE and not jobs


def test_crash_mid_write_leaves_the_old_file_valid(tmp_path, monkeypatch):
    prov, world, bars, lister, jobs = make_provider(tmp_path)
    prov.get_atm_iv_series("TEST", END, LOOK)
    jobs.pop()()
    path = prov.store_path("TEST")
    before = pd.read_parquet(path)
    row = prov._tomb(dict(session_date=date(2026, 3, 9), spot=100.0, rate=0.04,
                          provenance=H.PROVENANCE_DERIVED, method_version=H.METHOD_VERSION,
                          computed_at=pd.Timestamp("2026-03-10")), "no_bar")

    def boom(src, dst):
        raise OSError("disk died mid-replace")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        prov._merge_rows("TEST", [row])
    monkeypatch.undo()
    after = pd.read_parquet(path)
    pd.testing.assert_frame_equal(before, after)
    assert not [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")]
    # and a half-written temp (to_parquet failing) also leaves it intact
    monkeypatch.setattr(pd.DataFrame, "to_parquet", lambda self, p, **k: (open(p, "wb").write(b"junk"), (_ for _ in ()).throw(OSError("x"))))
    with pytest.raises(OSError):
        prov._merge_rows("TEST", [row])
    monkeypatch.undo()
    pd.testing.assert_frame_equal(before, pd.read_parquet(path))
    assert not [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")]


def test_method_version_bump_recomputes_and_ignores_stale_rows(tmp_path, monkeypatch):
    prov, world, bars, lister, jobs = make_provider(tmp_path)
    prov.get_atm_iv_series("TEST", END, LOOK)
    jobs.pop()()
    n = prov.get_atm_iv_series("TEST", END, LOOK)
    monkeypatch.setattr(H, "METHOD_VERSION", "ruleC-barclose-v2")
    stale = prov.peek("TEST", END, LOOK)
    assert stale.covered == 0 and stale.status == H.STATUS_FILLING    # old rows are not served
    calls = prov.api_calls
    prov.get_atm_iv_series("TEST", END, LOOK)      # warm listing: fits the foreground budget
    while jobs:
        jobs.pop()()
    redone = prov.get_atm_iv_series("TEST", END, LOOK)
    assert redone.status == H.STATUS_COMPLETE and redone.covered == n.covered
    assert prov.api_calls > calls
    df = prov.read_store("TEST")
    assert set(df["method_version"]) == {"ruleC-barclose-v2"}      # replaced, not duplicated


def test_concurrent_callers_share_one_fill(tmp_path):
    started = []
    prov, world, bars, lister, jobs = make_provider(tmp_path, runner=lambda j: started.append(j))
    bars.gate = threading.Event()
    # warm listing so the foreground reaches the (blocked) bars call
    prov.get_atm_iv_series("TEST", END, LOOK)
    started.pop()()
    bars.gate = threading.Event()
    world.sessions = market_calendar.regular_session_dates(date(2025, 1, 2), date(2026, 3, 9))
    prov._now = lambda: datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)
    results = {}

    def first():
        results["a"] = prov.get_atm_iv_series("TEST", date(2026, 3, 9), LOOK, max_api_calls=5, max_seconds=30)
    bars.entered.clear()
    t = threading.Thread(target=first)
    t.start()
    assert bars.entered.wait(5)
    n_before = len(bars.calls)
    second = prov.get_atm_iv_series("TEST", date(2026, 3, 9), LOOK)
    assert second.status == H.STATUS_FILLING            # partial, did not start a second fill
    assert len(bars.calls) == n_before
    bars.gate.set()
    t.join(10)
    assert results["a"].status == H.STATUS_COMPLETE
    assert len(bars.calls) == n_before                  # exactly one fill's worth of calls


def test_token_bucket_gates_background_calls():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s
    b = H.TokenBucket(per_minute=60, burst=2, clock=lambda: now[0], sleep=sleep)
    assert b.acquire(False) and b.acquire(False) and not b.acquire(False)
    assert b.acquire(True) and slept            # waited for a refill
    budget = H._Budget(2, None, H.TokenBucket(per_minute=60, burst=5), blocking=False)
    budget.spend(2)
    with pytest.raises(H.BudgetExhausted):
        budget.spend(1)


def test_never_fetches_todays_session(tmp_path):
    prov, world, bars, lister, jobs = make_provider(tmp_path)
    res = prov.get_atm_iv_series("TEST", date(2026, 3, 9), LOOK)   # asks for "today"
    assert res.end_session == END                       # clamped to the last completed session
    jobs.pop()()
    df = prov.read_store("TEST")
    assert df["session_date"].max().date() == END


def test_contract_listing_inactive_once_active_daily(tmp_path):
    prov, world, bars, lister, jobs = make_provider(tmp_path)
    prov.get_atm_iv_series("TEST", END, LOOK)
    jobs.pop()()
    statuses = [c[0] for c in lister.calls]
    assert statuses.count("inactive") == 1 and statuses.count("active") == 1
    # same day again: no listing calls at all
    n = len(lister.calls)
    prov._merge_rows("TEST", [])
    assert len(lister.calls) == n
    # next day: active refreshed, inactive NOT refetched (within the 7-day tail window)
    prov._now = lambda: datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)
    world.sessions = market_calendar.regular_session_dates(date(2025, 1, 2), date(2026, 3, 9))
    prov.get_atm_iv_series("TEST", date(2026, 3, 9), LOOK, max_api_calls=5)
    statuses = [c[0] for c in lister.calls]
    assert statuses.count("inactive") == 1 and statuses.count("active") == 2


def test_snapshot_precedence_and_provenance_counts(tmp_path):
    prov, world, *_ = make_provider(tmp_path)
    series = H.AtmIvSeries("TEST", END, H.STATUS_COMPLETE, values={date(2026, 3, 4): 0.3, date(2026, 3, 5): 0.31},
                           covered=2, expected=5, window_start=date(2026, 3, 2))
    snaps = {date(2026, 3, 4): 0.99, date(2026, 3, 3): 0.28, date(2026, 3, 6): 0.27, date(2026, 2, 1): 0.5}
    merged, prov_counts = H.merge_snapshots(series, snaps, stored_sessions={date(2026, 3, 6)})
    assert merged[date(2026, 3, 4)] == 0.3                 # derived wins
    assert merged[date(2026, 3, 3)] == 0.28                # snapshot fills a date with no derived row
    assert date(2026, 3, 6) not in merged                  # a tombstoned date is not back-filled
    assert date(2026, 2, 1) not in merged                  # outside the window
    assert prov_counts == {H.PROVENANCE_DERIVED: 2, H.PROVENANCE_SNAPSHOT: 1}


# ---- split basis ---------------------------------------------------------------------------
def test_fmp_spot_source_converts_adjusted_closes_to_as_traded(tmp_path):
    """A 10:1 split on 2026-02-10: FMP's cache is adjusted (pre-split closes / 10); option strikes
    are as traded, so the spot before the split must be close x 10."""
    from ba2_common.core.split_basis import CalendarSplit
    days = market_calendar.regular_session_dates(date(2026, 1, 2), date(2026, 3, 6))
    split = date(2026, 2, 10)
    traded = {d: (1000.0 if d < split else 100.0) + (d.toordinal() % 7) for d in days}
    rows = []
    for d in days:
        adj = traded[d] / 10.0 if d < split else traded[d]
        rows.append({"Date": pd.Timestamp(d), "Open": adj, "High": adj * 1.01, "Low": adj * 0.99, "Close": adj})
    df = pd.DataFrame(rows)
    src = H.FmpSpotSource(ohlcv_loader=lambda s, a, b: (df, None),
                          split_loader=lambda s: [CalendarSplit(split, 10.0)])
    sp = src.spots("NVDA", date(2026, 1, 20), date(2026, 3, 6))
    for d, v in sp.items():
        assert v == pytest.approx(traded[d]), d
    # a symbol whose basis cannot be proven REFUSES (never assumes 1)
    bad = H.FmpSpotSource(ohlcv_loader=lambda s, a, b: (df, None), split_loader=lambda s: None)
    with pytest.raises(H.SpotUnavailable):
        bad.spots("NVDA", date(2026, 1, 20), date(2026, 3, 6))


def test_split_symbol_derives_the_true_iv_on_the_as_traded_basis(tmp_path):
    """The chain is priced off the as-traded spot (x10 here); the provider's spot source is the
    as-traded one, so the round trip still lands on the true IV. (With the adjusted spot the
    6-nearest-strike set would be empty / mispriced -- that is what the factor prevents.)"""
    world = FakeWorld()
    world.spot_scale = 10.0
    world.strikes = [float(k) for k in range(500, 1501, 10)]

    class Traded(FakeSpots):
        def spots(self, symbol, start, end):
            return {d: v * 10.0 for d, v in super().spots(symbol, start, end).items()}
    prov, world, bars, lister, jobs = make_provider(tmp_path, world, spot_source=Traded(world))
    prov.get_atm_iv_series("TEST", END, LOOK)
    jobs.pop()()
    df = prov.read_store("TEST")
    ok = df[df["reason"] == "ok"]
    assert len(ok) > 30
    for r in ok.itertuples():
        d = r.session_date.date()
        assert abs(r.iv - world.true_iv(d, r.strike, r.expiry.date())) < 1e-4
        assert abs(r.strike - r.spot) < 100          # ATM on the as-traded basis
