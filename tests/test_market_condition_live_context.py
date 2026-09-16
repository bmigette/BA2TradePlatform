"""Live market-condition context (design 2026-09-15 section 4.1).

* the EXPERT SETTING ``market_condition_profile`` decides which experts are gated, on one
  process, with the retired ``BA2_MARKET_CONDITION_PROFILE`` failing startup;
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
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import ba2_common.core.TradeConditions as TC
from ba2_common.core import market_condition_live as live
from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_readers import FMPCacheMarketConditionReader
from ba2_common.core.market_conditions import (
    PROFILES,
    STATUS_NO_CONTEXT,
    STATUS_VALID,
    WINDOW,
)
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


def _leaf(op=">", value=-1e9, rec=None):
    """A market-condition leaf. ``rec`` is what carries the expert instance id to the dispatching
    resolver; ``None`` is the "no recommendation" case (and what a directly installed
    single-expert resolver ignores)."""
    return TC.create_condition(ExpertEventType.N_UNDERLYING_ADX, object(), "AAA", rec,
                               operator_str=op, value=value)


class _LogSpy:
    def __init__(self):
        self.errors, self.warnings = [], []

    def error(self, msg, *args, **kwargs):
        self.errors.append(msg % args if args else msg)

    def warning(self, msg, *args, **kwargs):
        self.warnings.append(msg % args if args else msg)

    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


# --------------------------------------------------------------------------- the expert setting
@pytest.fixture
def instances(monkeypatch):
    """A live-ish instance registry: ``{id: market_condition_profile setting value}``.

    Patches the ba2_common instance-resolver seam, which is what the per-instance resolver reads
    the setting through (the live host backs it with ``get_expert_instance_from_id`` + the expert
    instance cache).
    """
    import ba2_common.core.instance_resolver as ir

    settings = {}

    class _Resolver:
        def get_expert_instance(self, expert_id):
            if expert_id not in settings:
                raise KeyError(f"no expert instance {expert_id}")
            return SimpleNamespace(id=expert_id,
                                   settings={"market_condition_profile": settings[expert_id]})

    monkeypatch.setattr(ir, "get_instance_resolver", lambda: _Resolver())
    return settings


@pytest.fixture
def dispatcher(cache_root, instances):
    """The resolver ``wire_all_seams`` installs, pointed at a cache that carries BOTH the test
    symbol AAA and the two symbols split certification reads."""
    _write_certifiable(cache_root)
    saved = TC.get_market_condition_context_resolver()
    live.clear_certification_cache()
    resolver = live.PerInstanceMarketConditionResolver(cache_root=cache_root, environ={})
    TC.set_market_condition_context_resolver(resolver)
    yield resolver
    TC.set_market_condition_context_resolver(saved)
    live.clear_certification_cache()


def _rec(instance_id):
    return SimpleNamespace(instance_id=instance_id, symbol="AAA", data={}, confidence=80.0)


def test_the_retired_env_var_fails_startup(monkeypatch, tmp_path):
    """A deploy script still exporting BA2_MARKET_CONDITION_PROFILE must not pass in silence:
    nothing reads it, so the platform would run on whatever the settings rows hold."""
    from tests.test_seam_wiring import _isolated_seam_state

    monkeypatch.setenv(live.PROFILE_ENV_RETIRED, "ohlcv-v1")
    saved = TC.get_market_condition_context_resolver()
    try:
        TC.set_market_condition_context_resolver(None)
        with _isolated_seam_state() as seam_wiring:
            seam_wiring._wired = False
            with pytest.raises(RuntimeError, match="expert setting"):
                seam_wiring.wire_all_seams()
    finally:
        TC.set_market_condition_context_resolver(saved)


def test_wire_all_seams_installs_the_dispatcher_and_serves_nothing_by_default(monkeypatch, tmp_path):
    """Installed UNCONDITIONALLY (it is free) -- and inert: an expert with an empty setting gets
    no resolver, so no clock is read and every market leaf reads no_context, exactly as on a
    platform that never heard of the feature."""
    from ba2_common.core import native_cache
    from tests.test_seam_wiring import _isolated_seam_state

    monkeypatch.delenv(live.PROFILE_ENV_RETIRED, raising=False)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", _write_certifiable(str(tmp_path)))
    saved = TC.get_market_condition_context_resolver()
    try:
        TC.set_market_condition_context_resolver(None)
        with _isolated_seam_state() as seam_wiring:
            seam_wiring._wired = False
            seam_wiring.wire_all_seams()
            got = TC.get_market_condition_context_resolver()
            assert isinstance(got, live.PerInstanceMarketConditionResolver)
    finally:
        TC.set_market_condition_context_resolver(saved)


def test_two_experts_on_one_process_observe_their_own_field_sets(dispatcher, instances, clock):
    """The whole point of the setting: ONE process, two experts, two different data supplies."""
    instances[1] = "ohlcv-v1"
    instances[2] = "ta-structure-v1"
    instances[3] = ""

    def fields_for(instance_id):
        with live.market_condition_decision_scope(expert_instance_id=instance_id):
            ctx = TC.resolve_market_condition_context(object(), "AAA", _rec(instance_id))
            if ctx is None:
                return None
            return set(ctx.reader.observe("AAA", PRIOR).by_field())

    assert fields_for(1) == {f.name for f in PROFILES["ohlcv-v1"].fields}
    assert fields_for(2) == {f.name for f in PROFILES["ta-structure-v1"].fields}
    # The un-gated expert gets NO resolver: no context and -- pinned by the clock -- no scope,
    # so an expert that does not use the feature pays nothing for its existence.
    assert fields_for(3) is None
    assert len(clock) == 2


def test_an_expert_with_the_empty_setting_and_a_market_leaf_raises_nothing_and_passes_nothing(
        dispatcher, instances, clock, monkeypatch):
    """"no_context" is not a pass. The leaf is FALSE and the reason names the expert and the
    setting, so an operator reading the log is told which instance is missing its profile."""
    monkeypatch.setattr(TC, "_warned_no_market_condition_context_fields", set())
    monkeypatch.setattr(TC, "logger", _LogSpy())
    instances[9] = ""
    with live.market_condition_decision_scope(expert_instance_id=9) as state:
        assert state is None
        leaf = _leaf(rec=_rec(9))
        assert leaf.evaluate() is False
        assert leaf.last_status == STATUS_NO_CONTEXT
        assert "expert instance 9" in leaf.last_reason
        assert "market_condition_profile" in leaf.last_reason
    assert clock == []


def test_a_leaf_with_no_recommendation_says_so_rather_than_guessing(dispatcher, instances):
    """Without a recommendation there is no instance, so no setting, so no profile. Guessing one
    would serve a gate from some other expert's snapshot."""
    leaf = _leaf(rec=None)
    assert leaf.evaluate() is False
    assert leaf.last_status == STATUS_NO_CONTEXT
    assert "no expert recommendation" in leaf.last_reason


def test_the_resolver_is_cached_per_instance(dispatcher, instances):
    """One reader (and one memo) per expert, not one per decision."""
    instances[1] = "ohlcv-v1"
    instances[2] = "ohlcv-v1"
    first = dispatcher.resolver_for(1)
    assert first is dispatcher.resolver_for(1)
    assert first.profiles == ("ohlcv-v1",)
    # Same profile, DIFFERENT expert: its own resolver, because coverage and the manifest are
    # answered per instance.
    assert dispatcher.resolver_for(2) is not first


def test_a_changed_profile_setting_is_never_served_from_the_old_resolver(dispatcher, instances):
    """The cache key carries the PROFILES as well as the id, so a setting that now names a
    different profile cannot be answered from the reader built for the old one -- even if every
    invalidation were missed."""
    instances[1] = "ohlcv-v1"
    first = dispatcher.resolver_for(1)
    instances[1] = "ta-structure-v1"
    assert dispatcher.resolver_for(1).profiles == ("ta-structure-v1",)
    instances[1] = ""
    assert dispatcher.resolver_for(1) is None
    instances[1] = "ohlcv-v1"
    assert dispatcher.resolver_for(1) is first        # back to the cached one, not a third build


def test_clear_cache_forces_a_rebuild_for_an_unchanged_setting(dispatcher, instances):
    """What the invalidations are actually FOR. The profile name is not the only thing behind a
    resolver -- the pinned manifest and the certified cache root are too -- so a rebuild has to
    be forceable for a setting whose text did not change at all. Live, it is also how a profile
    edited out of band gets past the expert-instance settings cache."""
    instances[1] = "ohlcv-v1"
    first = dispatcher.resolver_for(1)

    from ba2_trade_platform.core.instance_registry import drop_market_condition_resolver

    drop_market_condition_resolver(1)
    second = dispatcher.resolver_for(1)
    assert second is not first and second.profiles == ("ohlcv-v1",)

    drop_market_condition_resolver()                  # the whole-process form
    assert dispatcher.resolver_for(1) is not second


def test_api_reload_drops_the_resolver_cache_through_the_route(dispatcher, instances, monkeypatch):
    """The route, not just the helper: /api/reload is the documented way to make a settings edit
    take effect without a restart, and the market-condition resolvers built from those settings
    have to be part of what it re-reads."""
    from ba2_trade_platform.ui import api_routes

    instances[4] = "ohlcv-v1"
    first = dispatcher.resolver_for(4)

    class _Jm:
        def refresh_expert_schedules(self, _id):
            pass

    monkeypatch.setattr("ba2_trade_platform.core.JobManager.get_job_manager", lambda: _Jm())
    assert api_routes.reload_from_db(
        api_routes.ReloadRequest(expert_instance_id=4))["status"] == "ok"
    assert dispatcher.resolver_for(4) is not first

    scoped = dispatcher.resolver_for(4)
    assert api_routes.reload_from_db(api_routes.ReloadRequest())["status"] == "ok"
    assert dispatcher.resolver_for(4) is not scoped


def test_an_unparseable_setting_refuses_the_gate_and_does_not_stop_the_pass(
        dispatcher, instances, monkeypatch):
    """A profile name this build does not know is a loud ERROR and a refused gate -- never a
    silently ungated entry, and never a crash that would take exits down with it."""
    import ba2_common.logger as bl

    spy = _LogSpy()
    monkeypatch.setattr(bl, "logger", spy)
    instances[6] = "ohlcv-v99"
    assert dispatcher.resolver_for(6) is None
    assert len(spy.errors) == 1 and "ohlcv-v99" in spy.errors[0]
    assert dispatcher(object(), "AAA", _rec(6)) is None


def test_a_comma_list_builds_one_reader_per_profile(dispatcher, instances):
    instances[7] = "ohlcv-v1,ta-structure-v1"
    resolver = dispatcher.resolver_for(7)
    assert resolver.profiles == ("ohlcv-v1", "ta-structure-v1")
    with live.market_condition_decision_scope(expert_instance_id=7):
        ctx = TC.resolve_market_condition_context(object(), "AAA", _rec(7))
    served = set(ctx.reader.observe("AAA", PRIOR).by_field())
    assert served == {f.name for p in ("ohlcv-v1", "ta-structure-v1")
                      for f in PROFILES[p].fields}


def test_an_uncertified_cache_refuses_that_experts_gates_and_leaves_exits_alone(
        tmp_path, instances, monkeypatch, clock):
    import ba2_common.logger as bl

    monkeypatch.setattr(bl, "logger", _LogSpy())
    spy = _LogSpy()
    monkeypatch.setattr(TC, "logger", spy)
    monkeypatch.setattr(TC, "_warned_no_market_condition_context_fields", set())
    live.clear_certification_cache()
    bad = _write_certifiable(str(tmp_path / "bad"), unadjusted=True)
    resolver = live.PerInstanceMarketConditionResolver(cache_root=bad, environ={})
    saved = TC.get_market_condition_context_resolver()
    TC.set_market_condition_context_resolver(resolver)
    instances[8] = "ohlcv-v1"
    try:
        degraded = resolver.resolver_for(8)
        assert isinstance(degraded, live.UncertifiedSourceResolver)
        assert degraded.failing_symbols == ("AAPL", "NVDA")
        with live.market_condition_decision_scope(expert_instance_id=8) as state:
            assert state is None          # no usable resolver: no clock read
            for _ in range(2):
                leaf = _leaf(rec=_rec(8))
                assert leaf.evaluate() is False
                assert leaf.last_status == STATUS_NO_CONTEXT
                assert leaf.last_reason == degraded.no_context_reason
        assert len(spy.warnings) == 1 and "certif" in spy.warnings[0].lower()
        # A non-market condition (what exit / protective rulesets use) evaluates normally.
        conf = TC.create_condition(ExpertEventType.N_CONFIDENCE, object(), "AAA", _rec(8),
                                   operator_str=">", value=50.0)
        assert conf.evaluate() is True
    finally:
        TC.set_market_condition_context_resolver(saved)
        live.clear_certification_cache()
    assert clock == []


def test_certification_defaults_to_the_native_cache_root_and_is_paid_once(tmp_path, monkeypatch,
                                                                         instances):
    import ba2_common.logger as bl
    from ba2_common.core import native_cache

    monkeypatch.setattr(bl, "logger", _LogSpy())
    live.clear_certification_cache()
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path / "missing"))
    resolver = live.PerInstanceMarketConditionResolver(environ={})
    instances[1] = "ohlcv-v1"
    assert isinstance(resolver.resolver_for(1), live.UncertifiedSourceResolver)

    live.clear_certification_cache()
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", _write_certifiable(str(tmp_path / "ok")))
    resolver = live.PerInstanceMarketConditionResolver(environ={})
    instances[2] = "ohlcv-v1"
    assert isinstance(resolver.resolver_for(2), live.LiveMarketConditionResolver)

    # Two experts, ONE certification: it is a property of the cache, not of who asks.
    calls = []
    import ba2_common.core.market_condition_source as mcs

    real = mcs.certify_source_columns
    monkeypatch.setattr(mcs, "certify_source_columns",
                        lambda root: calls.append(root) or real(root))
    instances[3] = "ohlcv-v1"
    resolver.resolver_for(3)
    assert calls == []          # already memoised by instance 2's build
    live.clear_certification_cache()


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
