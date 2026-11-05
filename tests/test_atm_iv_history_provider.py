"""AtmIvHistoryProvider.ensure_filled: blocking incremental fill, backoff, tombstones, atomicity,
concurrency (all fakes; no network)."""
import math
import os
import threading
import time
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from ba2_common.core import market_calendar
from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H
from tests.atm_iv_fakes import (NOW, FakeBars, FakeLister, FakeRate, FakeSpots, FakeWorld, MultiBars,
                                MultiSpots, RateLimitError, ServerError, make_provider, reset_module_state)

END = date(2026, 3, 6)
LOOK = 60          # calendar days: ~41 sessions, quick


@pytest.fixture(autouse=True)
def _clean():
    reset_module_state()
    yield
    reset_module_state()


def _advance_one_day(prov, world, lister):
    world.sessions = market_calendar.regular_session_dates(date(2025, 1, 2), date(2026, 3, 9))
    prov._now = lambda: datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)
    lister.today = date(2026, 3, 10)


def _truth(prov, world, sym="TEST"):
    df = prov.read_store(sym)
    ok = df[df["reason"] == "ok"]
    assert len(ok) > 0
    for r in ok.itertuples():
        d = r.session_date.date()
        assert abs(r.iv - world.true_iv(d, r.strike, r.expiry.date())) < 1e-4, (d, r.occ)
        assert 20 <= r.dte <= 45
        assert r.provenance == H.PROVENANCE_DERIVED and r.method_version == H.METHOD_VERSION


def test_cold_fill_completes_blocking_then_later_calls_make_zero_api_calls(tmp_path):
    prov, world, bars, lister = make_provider(tmp_path)
    res = prov.ensure_filled("TEST", END, LOOK)
    assert res.status == H.STATUS_COMPLETE and res.covered == res.expected > 30
    n = prov.api_calls
    again = prov.ensure_filled("TEST", END, LOOK)
    assert again.status == H.STATUS_COMPLETE and prov.api_calls == n
    assert again.provenance == {H.PROVENANCE_DERIVED: again.covered}
    _truth(prov, world)


def test_peek_never_calls_the_api(tmp_path):
    prov, *_ = make_provider(tmp_path)
    r = prov.peek("TEST", END, LOOK)
    assert r.status == H.STATUS_FILLING and r.covered == 0 and prov.api_calls == 0


def test_one_new_day_appends_exactly_one_row_with_few_calls(tmp_path):
    prov, world, bars, lister = make_provider(tmp_path)
    prov.ensure_filled("TEST", END, LOOK)
    before = prov.read_store("TEST")
    _advance_one_day(prov, world, lister)
    calls = prov.api_calls
    res = prov.ensure_filled("TEST", date(2026, 3, 9), LOOK)
    after = prov.read_store("TEST")
    assert res.status == H.STATUS_COMPLETE and len(after) == len(before) + 1
    assert after["session_date"].max().date() == date(2026, 3, 9)
    assert prov.api_calls - calls <= 3        # active-listing refresh + the bars


def test_monday_catch_up_over_mwf_expiries_is_one_blocking_multi_batch_call(tmp_path):
    """Mon/Wed/Fri chains: ~440 contracts per session -> several 200-symbol batches for a 5-session
    catch-up. Blocking, so no budget problem."""
    world = FakeWorld(mwf=True)
    prov, world, bars, lister = make_provider(tmp_path, world, now=datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc))
    prov.ensure_filled("TEST", date(2026, 2, 27), LOOK)
    prov._now = lambda: NOW
    lister.today = NOW.date()
    n_before = len(bars.calls)
    res = prov.ensure_filled("TEST", END, LOOK)
    assert res.status == H.STATUS_COMPLETE and res.covered == res.expected
    assert len(bars.calls) - n_before >= 3


def test_deadline_returns_filling_keeps_progress_and_the_next_pass_resumes(tmp_path):
    prov, world, bars, lister = make_provider(tmp_path)
    orig = prov._compute_chunk
    n = []

    def deadline_in_second_chunk(*a, **k):
        n.append(1)
        if len(n) == 2:
            raise H.BudgetExhausted("per-symbol deadline reached")
        return orig(*a, **k)
    prov._compute_chunk = deadline_in_second_chunk
    first = prov.ensure_filled("TEST", END, 365, deadline_seconds=900)
    assert first.status == H.STATUS_FILLING and "deadline" in first.reason
    kept = prov.peek("TEST", END, 365)
    assert 0 < kept.covered < kept.expected                  # chunk-by-chunk progress persisted
    listings = len(lister.calls)
    prov._compute_chunk = orig
    second = prov.ensure_filled("TEST", END, 365, deadline_seconds=300)
    assert second.status == H.STATUS_COMPLETE and second.covered == second.expected
    assert len(lister.calls) == listings                      # the listing was not redone
    assert prov.read_store("TEST").session_date.is_unique


def test_real_deadline_stops_a_slow_fill(tmp_path):
    prov, world, bars, lister = make_provider(tmp_path)
    bars.delay = 0.3
    t0 = time.monotonic()
    res = prov.ensure_filled("TEST", END, 365, deadline_seconds=0.5)
    assert time.monotonic() - t0 < 6 and res.status == H.STATUS_FILLING


def test_thin_day_is_a_tombstone_after_one_look_again_and_nothing_is_carried_forward(tmp_path):
    world = FakeWorld()
    thin = date(2026, 2, 24)
    world.no_bar_days = {thin}
    prov, world, bars, lister = make_provider(tmp_path, world)
    res = prov.ensure_filled("TEST", END, LOOK)
    df = prov.read_store("TEST")
    row = df[df["session_date"] == pd.Timestamp(thin)].iloc[0]
    assert row["reason"] == "no_bar" and math.isnan(row["iv"])
    assert thin not in res.values and res.tombstones == 1 and res.status == H.STATUS_COMPLETE


def test_transient_empty_response_is_not_a_permanent_tombstone_even_on_a_cold_fill(tmp_path):
    prov, world, bars, lister = make_provider(tmp_path)
    bars.empty_first = 1                                       # the first bars call answers {}
    res = prov.ensure_filled("TEST", END, LOOK)
    assert res.status == H.STATUS_COMPLETE and res.tombstones == 0 and res.covered == res.expected
    assert (prov.read_store("TEST")["reason"] == "ok").all()


def test_young_no_bar_tombstone_is_retried_next_day_old_one_is_not(tmp_path):
    world = FakeWorld()
    world.no_bar_days = {END}
    prov, world, bars, lister = make_provider(tmp_path, world)
    prov.ensure_filled("TEST", END, LOOK)
    assert prov.read_store("TEST").set_index("session_date").loc[pd.Timestamp(END), "reason"] == "no_bar"
    n = prov.api_calls
    prov.ensure_filled("TEST", END, LOOK)                      # same calendar day: nothing re-fetched
    assert prov.api_calls == n
    world.no_bar_days = set()                                  # the data landed later
    _advance_one_day(prov, world, lister)
    prov.ensure_filled("TEST", date(2026, 3, 9), LOOK)
    assert prov.read_store("TEST").set_index("session_date").loc[pd.Timestamp(END), "reason"] == "ok"


def test_low_volume_close_is_kept_exactly_like_the_backtest(tmp_path):
    world = FakeWorld()
    low = date(2026, 2, 25)
    world.volume_by_day = {low: 1}
    prov, world, bars, lister = make_provider(tmp_path, world)
    prov.ensure_filled("TEST", END, LOOK)
    row = prov.read_store("TEST").set_index("session_date").loc[pd.Timestamp(low)]
    assert row["reason"] == "ok" and row["bar_volume"] == 1
    assert abs(row["iv"] - world.true_iv(low, row["strike"], row["expiry"].date())) < 1e-4
    assert not hasattr(H, "MIN_BAR_VOLUME")


def test_missing_fred_or_spot_is_unavailable_never_a_default(tmp_path):
    def no_rate(a, b):
        raise H.AtmIvUnavailable("risk-free rate unavailable: no cache")
    prov, *_ = make_provider(tmp_path, rate_source=no_rate)
    res = prov.ensure_filled("TEST", END, LOOK)
    assert res.status == H.STATUS_UNAVAILABLE and "risk-free" in res.reason
    assert prov.api_calls == 0 and not os.path.exists(prov.store_path("TEST"))

    class NoSpot:
        def spots(self, s, a, b):
            raise H.SpotUnavailable("no OHLCV")
    prov2, *_ = make_provider(tmp_path / "s", spot_source=NoSpot())
    res2 = prov2.ensure_filled("TEST", END, LOOK)
    assert res2.status == H.STATUS_UNAVAILABLE and prov2.api_calls == 0


def test_sessions_without_spot_are_not_computed_and_the_call_is_still_complete(tmp_path):
    world = FakeWorld()
    hole = date(2026, 2, 26)
    world.no_spot_days = {hole}
    prov, world, bars, lister = make_provider(tmp_path, world)
    res = prov.ensure_filled("TEST", END, LOOK)
    assert hole not in res.values and res.unresolved == 1 and res.status == H.STATUS_COMPLETE


# ---- backoff / retries -------------------------------------------------------------------------
def test_429_is_retried_with_exponential_backoff_and_jitter_then_completes(tmp_path):
    sleeps, clock = [], [0.0]

    def sleep(sec):
        sleeps.append(sec)
        clock[0] += sec
    bucket = H.TokenBucket(per_minute=6000, burst=10, clock=lambda: clock[0], sleep=sleep)
    prov, world, bars, lister = make_provider(tmp_path, sleep=sleep, bucket=bucket)
    prov._jitter = lambda: 1.0                                 # +25%
    bars.fail_first = 3
    res = prov.ensure_filled("TEST", END, LOOK)
    assert res.status == H.STATUS_COMPLETE
    backoffs = [x for x in sleeps if x > 1.0]                  # (the bucket's own debt waits are <= 1 s)
    assert backoffs[:3] == pytest.approx([2.0 * 1.25, 4.0 * 1.25, 8.0 * 1.25])


def test_retry_after_header_is_respected_and_5xx_is_retried(tmp_path):
    sleeps = []
    prov, world, bars, lister = make_provider(tmp_path, sleep=sleeps.append)
    bars.fail_first = 1
    bars.fail_exc = lambda: RateLimitError(retry_after=7)
    assert prov.ensure_filled("TEST", END, LOOK).status == H.STATUS_COMPLETE
    assert sleeps[0] == pytest.approx(7.0)
    prov2, w2, b2, l2 = make_provider(tmp_path / "b", sleep=sleeps.append)
    b2.fail_first = 2
    b2.fail_exc = ServerError
    assert prov2.ensure_filled("TEST", END, LOOK).status == H.STATUS_COMPLETE


def test_persistent_429_returns_filling_with_one_warning_and_a_later_pass_resumes(tmp_path, monkeypatch):
    msgs = []
    monkeypatch.setattr(H.logger, "warning", lambda m, *a, **k: msgs.append(m))
    prov, world, bars, lister = make_provider(tmp_path)
    bars.fail_always = True
    r1 = prov.ensure_filled("TEST", END, LOOK)
    r2 = prov.ensure_filled("TEST", END, LOOK)
    assert r1.status == r2.status == H.STATUS_FILLING
    assert len([m for m in msgs if "stopped after" in m]) == 1       # one warning per symbol per day
    bars.fail_always = False
    assert prov.ensure_filled("TEST", END, LOOK).status == H.STATUS_COMPLETE


def test_a_hung_api_call_cannot_pin_the_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "REQUEST_TIMEOUT_SECONDS", 0.2)
    prov, world, bars, lister = make_provider(tmp_path)
    bars.gate = threading.Event()                              # every bars call hangs
    t0 = time.monotonic()
    res = prov.ensure_filled("TEST", END, LOOK, deadline_seconds=3)
    assert time.monotonic() - t0 < 10 and res.status == H.STATUS_FILLING
    bars.gate.set()


# ---- store safety ---------------------------------------------------------------------------------
def test_crash_mid_write_leaves_the_old_file_valid(tmp_path, monkeypatch):
    prov, world, bars, lister = make_provider(tmp_path)
    prov.ensure_filled("TEST", END, LOOK)
    path = prov.store_path("TEST")
    before = pd.read_parquet(path)
    row = prov._tomb(dict(session_date=date(2026, 3, 9), spot=100.0, rate=0.04, provenance=H.PROVENANCE_DERIVED,
                          method_version=H.METHOD_VERSION, computed_at=pd.Timestamp("2026-03-10")), "no_bar")

    def boom(src, dst):
        raise OSError("disk died mid-replace")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        prov._merge_rows("TEST", [row])
    monkeypatch.undo()
    pd.testing.assert_frame_equal(before, pd.read_parquet(path))
    assert not [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")]
    monkeypatch.setattr(pd.DataFrame, "to_parquet",
                        lambda self, p, **k: (open(p, "wb").write(b"junk"), (_ for _ in ()).throw(OSError("x"))))
    with pytest.raises(OSError):
        prov._merge_rows("TEST", [row])
    monkeypatch.undo()
    pd.testing.assert_frame_equal(before, pd.read_parquet(path))


def test_crash_mid_chunk_keeps_every_earlier_chunk(tmp_path):
    """The process dies (exception) in the SECOND chunk: the first chunk is already on disk and
    valid, and a re-run only does the rest."""
    prov, world, bars, lister = make_provider(tmp_path)
    orig = prov._compute_chunk
    n = []

    def flaky(*a, **k):
        n.append(1)
        if len(n) == 2:
            raise KeyboardInterrupt("simulated crash")
        return orig(*a, **k)
    prov._compute_chunk = flaky
    with pytest.raises(KeyboardInterrupt):
        prov.ensure_filled("TEST", END, 365)
    prov._compute_chunk = orig
    kept = prov.peek("TEST", END, 365)
    assert 0 < kept.covered < kept.expected
    assert prov.ensure_filled("TEST", END, 365).status == H.STATUS_COMPLETE


def test_method_version_bump_recomputes_and_ignores_stale_rows(tmp_path, monkeypatch):
    prov, world, bars, lister = make_provider(tmp_path)
    n = prov.ensure_filled("TEST", END, LOOK)
    monkeypatch.setattr(H, "METHOD_VERSION", "ruleC-barclose-v9")
    stale = prov.peek("TEST", END, LOOK)
    assert stale.covered == 0 and stale.status == H.STATUS_FILLING
    calls = prov.api_calls
    redone = prov.ensure_filled("TEST", END, LOOK)
    assert redone.status == H.STATUS_COMPLETE and redone.covered == n.covered and prov.api_calls > calls
    assert set(prov.read_store("TEST")["method_version"]) == {"ruleC-barclose-v9"}


def test_never_fetches_todays_session(tmp_path):
    prov, world, bars, lister = make_provider(tmp_path)
    res = prov.ensure_filled("TEST", date(2026, 3, 9), LOOK)        # asks for "today"
    assert res.end_session == END
    assert prov.read_store("TEST")["session_date"].max().date() == END


def test_contract_listing_inactive_once_active_daily(tmp_path):
    prov, world, bars, lister = make_provider(tmp_path)
    prov.ensure_filled("TEST", END, LOOK)
    st = [c[0] for c in lister.calls]
    assert st.count("inactive") == 1 and st.count("active") == 1
    _advance_one_day(prov, world, lister)
    prov.ensure_filled("TEST", date(2026, 3, 9), LOOK)
    st = [c[0] for c in lister.calls]
    assert st.count("inactive") == 1 and st.count("active") == 2


def test_orphan_tmp_files_older_than_an_hour_are_swept_on_start(tmp_path):
    d = tmp_path / "AtmIvHistory"
    d.mkdir()
    old, new = d / "A.parquet.1.2.tmp", d / "B.parquet.1.2.tmp"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    t = time.time()
    os.utime(old, (t - 7200, t - 7200))
    make_provider(tmp_path)
    assert not old.exists() and new.exists()


def test_snapshot_precedence_and_provenance_counts():
    series = H.AtmIvSeries("TEST", END, H.STATUS_COMPLETE, values={date(2026, 3, 4): 0.3, date(2026, 3, 5): 0.31},
                           covered=2, expected=5, window_start=date(2026, 3, 2))
    snaps = {date(2026, 3, 4): 0.99, date(2026, 3, 3): 0.28, date(2026, 3, 6): 0.27, date(2026, 2, 1): 0.5}
    merged, prov_counts = H.merge_snapshots(series, snaps, stored_sessions={date(2026, 3, 6)})
    assert merged[date(2026, 3, 4)] == 0.3 and merged[date(2026, 3, 3)] == 0.28
    assert date(2026, 3, 6) not in merged and date(2026, 2, 1) not in merged
    assert prov_counts == {H.PROVENANCE_DERIVED: 2, H.PROVENANCE_SNAPSHOT: 1}


# ---- concurrency -------------------------------------------------------------------------------------
def test_two_threads_same_symbol_one_fills_the_other_waits_and_makes_no_calls(tmp_path):
    prov, world, bars, lister = make_provider(tmp_path)
    bars.gate = threading.Event()
    out = {}

    def run(name):
        out[name] = prov.ensure_filled("TEST", END, LOOK, deadline_seconds=60)
    a = threading.Thread(target=run, args=("a",))
    a.start()
    assert bars.entered.wait(5)
    b = threading.Thread(target=run, args=("b",))
    b.start()
    time.sleep(0.3)
    calls_during = len(bars.calls)
    assert b.is_alive()                                        # waiting on the symbol lock
    bars.gate.set()
    a.join(30)
    b.join(30)
    assert out["a"].status == out["b"].status == H.STATUS_COMPLETE
    total = prov.api_calls
    solo, *_ = make_provider(tmp_path / "solo")
    solo.ensure_filled("TEST", END, LOOK)
    assert total == solo.api_calls                              # the waiter added not a single call
    assert calls_during == 1


def test_waiter_reads_the_partial_store_when_its_own_deadline_passes_first(tmp_path):
    prov, world, bars, lister = make_provider(tmp_path)
    bars.gate = threading.Event()
    a = threading.Thread(target=lambda: prov.ensure_filled("TEST", END, LOOK, deadline_seconds=60))
    a.start()
    assert bars.entered.wait(5)
    res = prov.ensure_filled("TEST", END, LOOK, deadline_seconds=0.3)
    assert res.status == H.STATUS_FILLING and "another fill" in res.reason
    bars.gate.set()
    a.join(30)


def test_many_threads_different_symbols_share_one_bucket_without_starvation(tmp_path):
    syms = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    worlds = {s: FakeWorld(root=s) for s in syms}
    bars = {s: FakeBars(worlds[s]) for s in syms}
    lists = {s: FakeLister(worlds[s]) for s in syms}
    per_min = 600.0                                            # 10 calls/s
    burst = 2.0
    bucket = H.TokenBucket(per_minute=per_min, burst=burst)
    client = MultiBars(bars, lists)
    prov = H.AtmIvHistoryProvider(
        cache_dir=str(tmp_path / "AtmIvHistory"), bars_client=client, contract_lister=client,
        spot_source=MultiSpots({s: FakeSpots(worlds[s]) for s in syms}),
        rate_source=lambda a, b: FakeRate(), now=lambda: NOW, bucket=bucket, jitter=lambda: 0.0)
    out = {}
    t0 = time.monotonic()
    threads = [threading.Thread(target=lambda s=s: out.__setitem__(s, prov.ensure_filled(s, END, 30, deadline_seconds=120)))
               for s in syms]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    elapsed = time.monotonic() - t0
    assert all(out[s].status == H.STATUS_COMPLETE for s in syms)           # nobody starved
    stamps = sorted(sum((b.times for b in bars.values()), []) + sum((getattr(l, "times", []) for l in lists.values()), []))
    n = len(stamps)
    assert n == prov.api_calls and n >= 3 * len(syms)
    # the process-wide rate is respected: n calls need at least (n - burst) / rate seconds
    assert elapsed >= (n - burst) / (per_min / 60.0) * 0.85
    # and in any 1-second window no more than rate + burst calls went out
    worst = max(sum(1 for t in stamps if s0 <= t < s0 + 1.0) for s0 in stamps)
    assert worst <= per_min / 60.0 + burst + 1


def test_token_bucket_and_budget_deadline():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s
    b = H.TokenBucket(per_minute=60, burst=2, clock=lambda: now[0], sleep=sleep)
    assert b.try_acquire() == 0.0 and b.try_acquire() == 0.0 and b.try_acquire() > 0
    assert b.acquire(True) and slept
    budget = H._Budget(0.05, H.TokenBucket(per_minute=1, burst=1), sleep=lambda s: time.sleep(0.01))
    budget.spend()
    with pytest.raises(H.BudgetExhausted):
        budget.spend()                  # the next token is a minute away, past the deadline


# ---- split basis ---------------------------------------------------------------------------------------
def test_fmp_spot_source_converts_adjusted_closes_to_as_traded():
    from ba2_common.core.split_basis import CalendarSplit
    days = market_calendar.regular_session_dates(date(2026, 1, 2), date(2026, 3, 6))
    split = date(2026, 2, 10)
    traded = {d: (1000.0 if d < split else 100.0) + (d.toordinal() % 7) for d in days}
    rows = []
    for d in days:
        adj = traded[d] / 10.0 if d < split else traded[d]
        rows.append({"Date": pd.Timestamp(d), "Open": adj, "High": adj * 1.01, "Low": adj * 0.99, "Close": adj})
    df = pd.DataFrame(rows)
    src = H.FmpSpotSource(ohlcv_loader=lambda s, a, b: (df, None), split_loader=lambda s: [CalendarSplit(split, 10.0)])
    for d, v in src.spots("NVDA", date(2026, 1, 20), date(2026, 3, 6)).items():
        assert v == pytest.approx(traded[d]), d
    bad = H.FmpSpotSource(ohlcv_loader=lambda s, a, b: (df, None), split_loader=lambda s: None)
    with pytest.raises(H.SpotUnavailable):
        bad.spots("NVDA", date(2026, 1, 20), date(2026, 3, 6))


def test_split_symbol_derives_the_true_iv_on_the_as_traded_basis(tmp_path):
    world = FakeWorld()
    world.spot_scale = 10.0
    world.strikes = [float(k) for k in range(500, 1501, 10)]

    class Traded(FakeSpots):
        def spots(self, symbol, start, end):
            return {d: v * 10.0 for d, v in super().spots(symbol, start, end).items()}
    prov, world, bars, lister = make_provider(tmp_path, world, spot_source=Traded(world))
    prov.ensure_filled("TEST", END, LOOK)
    ok = prov.read_store("TEST").query("reason == 'ok'")
    assert len(ok) > 30
    for r in ok.itertuples():
        d = r.session_date.date()
        assert abs(r.iv - world.true_iv(d, r.strike, r.expiry.date())) < 1e-4
        assert abs(r.strike - r.spot) < 100
