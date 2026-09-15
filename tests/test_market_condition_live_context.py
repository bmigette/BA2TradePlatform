"""Live market-condition context (design 2026-09-15 section 4.1).

* ``BA2_MARKET_CONDITION_PROFILE`` gates installation in ``wire_all_seams``;
* one ``replay_now()`` read per decision pass, on the coordinating thread, and none at all when
  the profile is not wired;
* every leaf of the pass resolves the same frozen context -- also from a pool thread, provided the
  fan-out carries the context (``submit_in_decision_context`` / ``run_in_decision_context``);
* the live reader reads the FMP parquet cache only and re-reads a file that changed.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import ba2_common.core.TradeConditions as TC
from ba2_common.core import market_condition_live as live
from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_readers import FMPCacheMarketConditionReader
from ba2_common.core.market_conditions import STATUS_NO_CONTEXT, STATUS_VALID, WINDOW
from ba2_common.core.types import ExpertEventType

DECISION = datetime(2025, 7, 1, 14, 0, tzinfo=timezone.utc)   # 10:00 New York
PRIOR = date(2025, 6, 30)


def _write_fmp_parquet(root, symbol, days, scale=1.0):
    folder = os.path.join(root, "FMPOHLCVProvider")
    os.makedirs(folder, exist_ok=True)
    rng = np.random.default_rng(5)
    c = 100.0 * scale * np.exp(np.cumsum(rng.normal(0, 0.01, len(days))))
    df = pd.DataFrame({
        "Date": pd.to_datetime([d.isoformat() for d in days]),
        "Open": c * 1.001, "High": c * 1.01, "Low": c * 0.99, "Close": c,
        "Volume": np.full(len(days), 1_000_000, dtype=np.int64),
    })
    df["effective_date"] = df["Date"]
    path = os.path.join(folder, f"{symbol}_1d.parquet")
    df.to_parquet(path, index=False)
    return path


def _write_certifiable(root, unadjusted=False):
    """AAPL/NVDA caches spanning their certification splits, smooth prices (split-adjusted), or
    with the pre-split bars multiplied by the split factor (unadjusted)."""
    folder = os.path.join(root, "FMPOHLCVProvider")
    os.makedirs(folder, exist_ok=True)
    for symbol, split, factor in (("AAPL", date(2020, 8, 31), 4.0), ("NVDA", date(2024, 6, 10), 10.0)):
        days = regular_sessions_ending_at(split + timedelta(days=30), 60)
        c = np.linspace(100.0, 110.0, len(days))
        if unadjusted:
            c = np.where(np.array([d < split for d in days]), c * factor, c)
        pd.DataFrame({"Date": pd.to_datetime([d.isoformat() for d in days]),
                      "Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                      "Volume": np.full(len(days), 1, dtype=np.int64)}).to_parquet(
            os.path.join(folder, f"{symbol}_1d.parquet"), index=False)
    return root


@pytest.fixture
def cache_root(tmp_path):
    _write_fmp_parquet(str(tmp_path), "AAA", regular_sessions_ending_at(date(2025, 7, 1), 200))
    return str(tmp_path)


@pytest.fixture
def clock(monkeypatch):
    reads = []

    def fake_now():
        reads.append(1)
        return DECISION

    monkeypatch.setattr(live, "_replay_now", fake_now)
    return reads


@pytest.fixture
def installed(cache_root):
    saved = TC.get_market_condition_context_resolver()
    resolver = live.LiveMarketConditionResolver(
        "ohlcv-v1", reader=FMPCacheMarketConditionReader("ohlcv-v1", cache_root))
    TC.set_market_condition_context_resolver(resolver)
    yield resolver
    TC.set_market_condition_context_resolver(saved)


def _leaf(op=">", value=-1e9):
    return TC.create_condition(ExpertEventType.N_UNDERLYING_ADX, object(), "AAA", None,
                               operator_str=op, value=value)


# --------------------------------------------------------------------------- env gating
def test_resolver_from_env(tmp_path):
    root = _write_certifiable(str(tmp_path / "ok"))
    assert live.resolver_from_env({}) is None
    assert live.resolver_from_env({live.PROFILE_ENV: ""}) is None
    assert live.resolver_from_env({live.PROFILE_ENV: "none"}) is None
    r = live.resolver_from_env({live.PROFILE_ENV: "ohlcv-v1"}, cache_root=root)
    assert isinstance(r, live.LiveMarketConditionResolver) and r.profile == "ohlcv-v1"
    assert r.source_profile == "fmp-daily-split-adjusted-v1"
    with pytest.raises(ValueError, match="not a registered"):
        live.resolver_from_env({live.PROFILE_ENV: "ohlcv-v9"}, cache_root=root)


def test_resolver_from_env_refuses_an_uncertified_cache(tmp_path):
    bad = _write_certifiable(str(tmp_path / "bad"), unadjusted=True)
    with pytest.raises(live.SourceCertificationError) as err:
        live.resolver_from_env({live.PROFILE_ENV: "ohlcv-v1"}, cache_root=bad)
    assert not err.value.report.consistent
    assert {c.basis for c in err.value.report.symbols} == {"unadjusted"}
    assert "AAPL" in str(err.value) and "NVDA" in str(err.value)
    with pytest.raises(live.SourceCertificationError):
        live.resolver_from_env({live.PROFILE_ENV: "ohlcv-v1"}, cache_root=str(tmp_path / "empty"))


def test_resolver_from_env_certifies_the_native_cache_root_by_default(tmp_path, monkeypatch):
    from ba2_common.core import native_cache

    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path / "missing"))
    with pytest.raises(live.SourceCertificationError):
        live.resolver_from_env({live.PROFILE_ENV: "ohlcv-v1"})
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", _write_certifiable(str(tmp_path / "ok")))
    assert live.resolver_from_env({live.PROFILE_ENV: "ohlcv-v1"}) is not None


@pytest.mark.parametrize("env_value,expect_installed", [(None, False), ("ohlcv-v1", True)])
def test_wire_all_seams_installs_only_with_the_setting(monkeypatch, tmp_path, env_value, expect_installed):
    from ba2_common.core import native_cache
    from tests.test_seam_wiring import _isolated_seam_state

    monkeypatch.setattr(native_cache, "CACHE_FOLDER", _write_certifiable(str(tmp_path)))

    saved = TC.get_market_condition_context_resolver()
    if env_value is None:
        monkeypatch.delenv(live.PROFILE_ENV, raising=False)
    else:
        monkeypatch.setenv(live.PROFILE_ENV, env_value)
    try:
        TC.set_market_condition_context_resolver(None)
        with _isolated_seam_state() as seam_wiring:
            seam_wiring._wired = False
            seam_wiring.wire_all_seams()
            got = TC.get_market_condition_context_resolver()
            if expect_installed:
                assert isinstance(got, live.LiveMarketConditionResolver) and got.profile == env_value
            else:
                assert got is None
    finally:
        TC.set_market_condition_context_resolver(saved)


# --------------------------------------------------------------------------- one clock read
def test_scope_without_the_resolver_reads_no_clock(clock):
    saved = TC.get_market_condition_context_resolver()
    TC.set_market_condition_context_resolver(None)
    try:
        with live.market_condition_decision_scope() as state:
            assert state is None
            leaf = _leaf()
            assert leaf.evaluate() is False and leaf.last_status == STATUS_NO_CONTEXT
    finally:
        TC.set_market_condition_context_resolver(saved)
    assert clock == []


def test_decision_time_is_read_once_per_analysis(installed, clock):
    with live.market_condition_decision_scope() as state:
        ctxs = [TC.resolve_market_condition_context(object(), "AAA", None) for _ in range(3)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            ctxs += [f.result() for f in [live.submit_in_decision_context(
                pool, TC.resolve_market_condition_context, object(), "AAA", None) for _ in range(6)]]
        leaves = [_leaf() for _ in range(3)]
        assert all(leaf.evaluate() for leaf in leaves)
    assert len(clock) == 1 and installed.decisions == 1
    assert all(c is ctxs[0] for c in ctxs)
    ctx = ctxs[0]
    assert ctx is state.context()
    assert ctx.decision_time == DECISION
    assert ctx.session_label == date(2025, 7, 1)
    assert ctx.prior_session == PRIOR
    assert ctx.recorder is None

    with live.market_condition_decision_scope():
        second = TC.resolve_market_condition_context(object(), "AAA", None)
    assert len(clock) == 2 and second is not ctx
    # Outside any scope: no context, no read.
    assert TC.resolve_market_condition_context(object(), "AAA", None) is None
    assert len(clock) == 2


def test_nested_scope_reuses_the_outer_state(installed, clock):
    with live.market_condition_decision_scope() as outer:
        with live.market_condition_decision_scope() as inner:
            assert inner is outer
            ctx = TC.resolve_market_condition_context(object(), "AAA", None)
        assert TC.resolve_market_condition_context(object(), "AAA", None) is ctx
        assert live.current_decision() is outer
    assert len(clock) == 1 and installed.decisions == 1
    assert live.current_decision() is None


def test_leaf_outside_a_decision_scope_warns_once_per_field(installed, clock, monkeypatch):
    warnings = []

    class _Spy:
        def warning(self, msg, *args):
            warnings.append(msg % args)

        def debug(self, *a, **k):
            pass

    monkeypatch.setattr(TC, "logger", _Spy())
    monkeypatch.setattr(TC, "_warned_no_market_condition_context_fields", set())
    for _ in range(3):
        leaf = _leaf()
        assert leaf.evaluate() is False
        assert leaf.last_status == STATUS_NO_CONTEXT
        assert "OUTSIDE a market_condition_decision_scope" in leaf.last_reason
    slope = TC.create_condition(ExpertEventType.N_UNDERLYING_TREND_SLOPE, object(), "AAA", None,
                                operator_str=">", value=-1e9)
    assert slope.evaluate() is False
    assert len(warnings) == 2
    assert "underlying_adx_14" in warnings[0] and "OUTSIDE a market_condition_decision_scope" in warnings[0]
    assert "underlying_trend_slope_50_atr14" in warnings[1]
    assert clock == []


def test_a_market_leaf_in_a_pool_thread_sees_the_coordinators_context(installed, clock):
    def evaluate():
        leaf = _leaf()
        return leaf.evaluate(), leaf.last_status, leaf.calculated_value

    with live.market_condition_decision_scope():
        with ThreadPoolExecutor(max_workers=2) as pool:
            carried = live.submit_in_decision_context(pool, evaluate).result()
            wrapped = list(pool.map(live.run_in_decision_context(lambda _: evaluate()), range(4)))
            bare = pool.submit(evaluate).result()
    assert carried[0] is True and carried[1] == STATUS_VALID and carried[2] is not None
    assert all(w == carried for w in wrapped)
    # Without carrying the context a pool thread has none: that is why the fan-out must wrap.
    assert bare == (False, STATUS_NO_CONTEXT, None)
    assert len(clock) == 1


def test_concurrent_analyses_do_not_share_a_clock(installed, monkeypatch):
    import threading

    times = iter([datetime(2025, 7, 1, 14, 0, tzinfo=timezone.utc),
                  datetime(2025, 7, 2, 14, 0, tzinfo=timezone.utc)])
    lock = threading.Lock()

    def fake_now():
        with lock:
            return next(times)

    monkeypatch.setattr(live, "_replay_now", fake_now)
    barrier = threading.Barrier(2)
    seen = {}

    def analysis(name):
        with live.market_condition_decision_scope():
            barrier.wait()
            seen[name] = TC.resolve_market_condition_context(object(), "AAA", None).prior_session

    threads = [threading.Thread(target=analysis, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seen.values()) == [date(2025, 6, 30), date(2025, 7, 1)]


def test_trade_manager_opens_the_decision_scope(installed, clock, monkeypatch):
    from ba2_trade_platform.core.TradeManager import TradeManager

    seen = []
    monkeypatch.setattr(TradeManager, "_process_expert_recommendations_after_analysis",
                        lambda self, expert_id, lookback_days=1: seen.append(live.current_decision()) or [])
    tm = TradeManager.__new__(TradeManager)
    assert tm.process_expert_recommendations_after_analysis(7) == []
    assert len(seen) == 1 and seen[0] is not None and seen[0].decision_time == DECISION
    assert live.current_decision() is None and len(clock) == 1


# --------------------------------------------------------------------------- live reader
def test_fmp_cache_reader_reads_the_parquet_and_rereads_a_changed_file(cache_root):
    reader = FMPCacheMarketConditionReader("ohlcv-v1", cache_root)
    row = reader.observe("AAA", PRIOR)
    assert {o.status for o in row.by_field().values()} == {STATUS_VALID}
    assert reader.observe("AAA", PRIOR) is row and reader.computed == 1
    assert reader.observe("MISSING", PRIOR) is None

    path = _write_fmp_parquet(cache_root, "AAA", regular_sessions_ending_at(date(2025, 7, 1), 200), scale=2.0)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    again = reader.observe("AAA", PRIOR)
    assert reader.computed == 2
    # A pure price rescale leaves slope/ADX/RV unchanged: the re-read is proven by the counter.
    assert again == row


def test_fmp_cache_reader_short_history(tmp_path):
    _write_fmp_parquet(str(tmp_path), "YNG", regular_sessions_ending_at(PRIOR, WINDOW - 10))
    row = FMPCacheMarketConditionReader("ohlcv-v1", str(tmp_path)).observe("YNG", PRIOR)
    assert {o.status for o in row.by_field().values()} == {"insufficient_history"}
