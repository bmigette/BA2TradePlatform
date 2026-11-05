"""AlpacaAccount.get_iv_rank / _iv_series / iv_sample_count served from the derived ATM-IV series."""
from datetime import date
from types import SimpleNamespace

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


def _fill(prov, jobs=None):
    prov.ensure_filled("TEST", L, 365)


def test_rank_is_none_while_filling_and_warns_once_per_day(tmp_path, monkeypatch):
    prov, world, bars, lister = make_provider(tmp_path)
    acct = _account(prov)
    msgs = []
    monkeypatch.setattr(H.logger, "warning", lambda m, *a, **k: msgs.append(m))
    assert acct.get_iv_rank("TEST", min_samples=5) is None       # cold: filling, gate stays shut
    assert acct.get_iv_rank("TEST", min_samples=5) is None
    filling = [m for m in msgs if "filling" in m]
    assert len(filling) == 1 and "TEST" in filling[0] and "0/" in filling[0]
    assert prov.api_calls == 0                                    # get_iv_rank NEVER calls the API
    assert acct._iv_series("TEST") == []                          # not a short "real" series


def test_rank_uses_the_last_completed_session_derived_value_and_never_the_snapshot(tmp_path):
    prov, world, bars, lister = make_provider(tmp_path)
    _fill(prov)
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
    prov, world, bars, lister = make_provider(tmp_path, world)
    _fill(prov)
    acct = _account(prov)
    res = prov.peek("TEST", L, 365)
    series = [v for d, v in res.values.items() if d < L]
    below = sum(1 for v in series if v < res.values[L])
    assert 0 < below < len(series)
    assert acct.get_iv_rank("TEST", min_samples=20) == round(below / len(series) * 100, 2)


def test_thin_name_with_tombstones_still_ranks_on_resolved_coverage(tmp_path):
    """F4: ~33% tombstoned sessions are RESOLVED (nothing more to fetch); the rank is then the
    backtest's: min_samples usable values, no extra 80%-of-values floor."""
    world = FakeWorld()
    world.no_bar_days = {d for i, d in enumerate(world.sessions) if i % 3 == 1 and d != L}
    prov, world, bars, lister = make_provider(tmp_path, world)
    _fill(prov)
    res = prov.peek("TEST", L, 365)
    assert res.status == H.STATUS_COMPLETE and res.covered / res.expected < 0.8
    acct = _account(prov)
    assert acct.get_iv_rank("TEST", min_samples=5) is not None
    assert acct.iv_sample_count("TEST") > 100


def test_unresolved_sessions_below_80_percent_give_none_and_readiness_zero(tmp_path):
    world = FakeWorld()
    world.no_spot_days = {d for i, d in enumerate(world.sessions) if i % 3 == 0 and d != L}   # cannot compute
    prov, world, bars, lister = make_provider(tmp_path, world)
    _fill(prov)
    acct = _account(prov)
    assert acct.get_iv_rank("TEST", min_samples=5) is None
    assert acct.iv_sample_count("TEST") == 0          # readiness never says ARMED for a closed gate


def test_readiness_is_zero_while_filling_and_equals_the_series_when_armed(tmp_path):
    prov, world, bars, lister = make_provider(tmp_path)
    acct = _account(prov)
    assert acct.get_iv_rank("TEST", min_samples=5) is None   # cold store: None, zero API calls
    assert prov.api_calls == 0 and acct.iv_sample_count("TEST") == 0
    _fill(prov)
    n = acct.iv_sample_count("TEST")
    assert n == len(acct._iv_series("TEST")) > 100
    assert acct.get_iv_rank("TEST", min_samples=5) is not None


def test_live_rank_equals_backtestaccount_get_iv_rank_on_the_same_series(tmp_path):
    """BT/live parity at as_of == the last completed session."""
    import os
    import sys
    tp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testplatform", "backend")
    if not os.path.exists(os.path.join(tp, "app", "services", "backtest", "backtest_account.py")):
        pytest.skip("testplatform backtest_account.py genuinely absent")
    if tp not in sys.path:
        sys.path.insert(0, tp)
    from app.services.backtest.backtest_account import BacktestAccount

    world = FakeWorld()
    world.true_iv = (lambda orig: (lambda d, K, e: 0.2242 if d == L else orig(d, K, e)))(world.true_iv)
    prov, world, bars, lister = make_provider(tmp_path, world)
    _fill(prov)
    live = _account(prov)
    res = prov.peek("TEST", L, 365)
    options = SimpleNamespace(get_atm_iv=lambda u, d: res.values.get(d))
    bt = BacktestAccount.__new__(BacktestAccount)
    bt._options = options
    bt._as_of_date = lambda: L
    for ms in (5, 20):
        assert live.get_iv_rank("TEST", min_samples=ms) == bt.get_iv_rank("TEST", min_samples=ms) is not None


def test_snapshot_fills_only_dates_with_no_derived_row_and_counts_provenance(tmp_path):
    world = FakeWorld()
    hole = date(2026, 2, 20)
    world.no_spot_days = {hole}                       # derived cannot compute this session
    tomb = date(2026, 2, 19)
    world.no_bar_days = {tomb}
    prov, world, bars, lister = make_provider(tmp_path, world)
    _fill(prov)
    acct = _account(prov, snapshots={hole: 0.5, tomb: 0.6, date(2026, 2, 18): 0.9})
    st = acct._iv_rank_state("TEST", 365)
    assert st["ok"] and st["provenance"][H.PROVENANCE_SNAPSHOT] == 1      # only `hole`
    assert 0.5 in st["series"] and 0.6 not in st["series"] and 0.9 not in st["series"]
    assert st["provenance"][H.PROVENANCE_DERIVED] > 100


def test_iv_sample_count_reads_the_store_only(tmp_path):
    prov, world, bars, lister = make_provider(tmp_path)
    acct = _account(prov)
    assert acct.iv_sample_count("TEST") == 0
    assert prov.api_calls == 0           # the readiness report spends nothing
    _fill(prov)
    assert acct.iv_sample_count("TEST") > 100


def test_unavailable_prerequisite_fails_closed(tmp_path):
    def no_rate(a, b):
        raise H.AtmIvUnavailable("no FRED cache")
    prov, *_ = make_provider(tmp_path, rate_source=no_rate)
    assert prov.ensure_filled("TEST", L, 365).status == H.STATUS_UNAVAILABLE
    assert _account(prov).get_iv_rank("TEST", min_samples=5) is None
