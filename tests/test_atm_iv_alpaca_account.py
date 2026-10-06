"""AlpacaAccount.get_iv_rank / _iv_series / iv_sample_count served from the derived ATM-IV series."""
from datetime import date

import pytest

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H
from tests.atm_iv_fakes import FakeWorld, make_provider, reset_module_state

L = date(2026, 3, 6)


@pytest.fixture(autouse=True)
def _clean():
    reset_module_state()
    yield
    reset_module_state()


def _account(prov, snapshots=None, monkeypatch=None):
    acct = AlpacaAccount.__new__(AlpacaAccount)
    acct.id = 7
    acct._atm_iv_history_provider = prov
    snaps = snapshots or {}
    acct._snapshot_iv_by_session = lambda u, a, b: {d: v for d, v in snaps.items() if a <= d <= b}

    def boom(underlying):
        raise AssertionError("the live snapshot IV must not be used as the CURRENT value")
    acct.get_atm_implied_volatility = boom
    return acct


def _fill(prov, jobs):
    prov.get_atm_iv_series("TEST", L, 365)
    while jobs:
        jobs.pop()()


def test_rank_is_none_while_filling_and_warns_once_per_day(tmp_path, monkeypatch):
    prov, world, bars, lister, jobs = make_provider(tmp_path)
    acct = _account(prov)
    msgs = []
    monkeypatch.setattr(H.logger, "warning", lambda m, *a, **k: msgs.append(m))
    assert acct.get_iv_rank("TEST", min_samples=5) is None       # cold: filling, gate stays shut
    assert acct.get_iv_rank("TEST", min_samples=5) is None
    filling = [m for m in msgs if "filling" in m]
    assert len(filling) == 1 and "TEST" in filling[0] and "0/" in filling[0]
    assert len(jobs) == 1                                         # one fill queued, not two
    assert acct._iv_series("TEST") == []                          # not a short "real" series


def test_rank_uses_the_last_completed_session_derived_value_and_never_the_snapshot(tmp_path):
    prov, world, bars, lister, jobs = make_provider(tmp_path)
    _fill(prov, jobs)
    acct = _account(prov)
    rank = acct.get_iv_rank("TEST", min_samples=20)
    res = prov.peek("TEST", L, 365)
    series = [v for d, v in res.values.items() if d < L]
    expected = acct._iv_rank_from_series(series, res.values[L], 20)
    assert rank == expected == 100.0           # the fake curve rises with time: L is the highest
    # series excludes L itself (BT: sessions before the as-of bar)
    assert len(acct._iv_series("TEST")) == len(series) == res.covered - 1
    # no API call was needed for any of this
    n = prov.api_calls
    acct.get_iv_rank("TEST", min_samples=20)
    assert prov.api_calls == n


def test_rank_reflects_the_percentile_not_just_the_top(tmp_path):
    world = FakeWorld()
    # make L's IV mid-pack: lower the curve for the last session via a volume-free bar close shift
    orig = world.true_iv
    world.true_iv = lambda d, K, e: (0.2242 if d == L else orig(d, K, e))
    prov, world, bars, lister, jobs = make_provider(tmp_path, world)
    _fill(prov, jobs)
    acct = _account(prov)
    res = prov.peek("TEST", L, 365)
    series = [v for d, v in res.values.items() if d < L]
    below = sum(1 for v in series if v < res.values[L])
    assert 0 < below < len(series)
    assert acct.get_iv_rank("TEST", min_samples=20) == round(below / len(series) * 100, 2)


def test_low_coverage_gives_none_not_a_partial_number(tmp_path):
    world = FakeWorld()
    world.no_bar_days = {d for i, d in enumerate(world.sessions) if i % 3 == 0}   # ~33% tombstones
    prov, world, bars, lister, jobs = make_provider(tmp_path, world)
    _fill(prov, jobs)
    res = prov.peek("TEST", L, 365)
    assert res.status == H.STATUS_COMPLETE and res.covered / res.expected < 0.8
    assert _account(prov).get_iv_rank("TEST", min_samples=5) is None


def test_snapshot_fills_only_dates_with_no_derived_row_and_counts_provenance(tmp_path):
    world = FakeWorld()
    hole = date(2026, 2, 20)
    world.no_spot_days = {hole}                       # derived cannot compute this session
    tomb = date(2026, 2, 19)
    world.no_bar_days = {tomb}
    prov, world, bars, lister, jobs = make_provider(tmp_path, world)
    _fill(prov, jobs)
    acct = _account(prov, snapshots={hole: 0.5, tomb: 0.6, date(2026, 2, 18): 0.9})
    st = acct._iv_rank_state("TEST", 365)
    assert st["ok"] and st["provenance"][H.PROVENANCE_SNAPSHOT] == 1      # only `hole`
    assert 0.5 in st["series"] and 0.6 not in st["series"] and 0.9 not in st["series"]
    assert st["provenance"][H.PROVENANCE_DERIVED] > 100


def test_iv_sample_count_reads_the_store_only(tmp_path):
    prov, world, bars, lister, jobs = make_provider(tmp_path)
    acct = _account(prov)
    assert acct.iv_sample_count("TEST") == 0
    assert prov.api_calls == 0 and not jobs           # no fill started by the readiness report
    _fill(prov, jobs)
    assert acct.iv_sample_count("TEST") > 100


def test_unavailable_prerequisite_fails_closed(tmp_path):
    def no_rate(a, b):
        raise H.AtmIvUnavailable("no FRED cache")
    prov, *_ = make_provider(tmp_path, rate_source=no_rate)
    assert _account(prov).get_iv_rank("TEST", min_samples=5) is None
