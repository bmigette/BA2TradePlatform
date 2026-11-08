"""The decision-pass capture scope opens for EVERY expert on both passes (stock decision replay, Task 5).

Before this, the scope opened only for an expert with a market-condition profile or option rules, and the
pass was recorded as the expert ``TradeManager``. Pinned here: every expert (stock, option, gated) gets a
scope carrying its REAL expert class and ``record_kind=decision_pass``; a lock miss / pending jobs DISCARD
(counted by reason, nothing written); automation off ends as a recorded skip; and a failure to open the
scope is counted, persisted as an error row, and NEVER stops the pass (exits especially).
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

import ba2_common.core.market_condition_live as mcl
import ba2_common.core.replay as replay_pkg
from ba2_common.core.replay import ReplayStatus, ReplayStore, SessionRecord, set_replay_store
from ba2_common.core.replay.context import CaptureHealth
from ba2_trade_platform.core.types import AnalysisUseCase
from tests.factories import create_account_definition, create_expert_instance
from tests.test_open_positions_market_condition_scope import SYMBOL, _run, world  # noqa: F401 -- fixture

NOW = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = ReplayStore(tmp_path / "store", writer="sync")
    s.begin_session(SessionRecord(
        session_id="s1", instance_id="i", started_at=NOW, exchange_tz="America/New_York",
        app_version="x", package_versions={}, source_revision="abc", dirty=False))
    set_replay_store(s)
    try:
        yield s
    finally:
        set_replay_store(None)
        s.close(timeout=2.0)


def _passes(store):
    return store.index.analyses("s1")


def _scope(expert_id, use_case=None, **kw):
    from ba2_trade_platform.core.TradeManager import TradeManager

    tm = TradeManager()
    cm = (tm._decision_capture_scope(expert_id, **kw) if use_case is None
          else tm._decision_capture_scope(expert_id, use_case, **kw))
    with cm as context:
        if context is not None:
            context.set_skip("test body")
    return context


def _expert(expert="FMPRating", **kw):
    return create_expert_instance(account_id=create_account_definition().id, expert=expert, **kw).id


def test_a_plain_stock_expert_gets_a_scope_on_both_passes_with_its_real_class(store):
    expert_id = _expert("FMPRating")
    _scope(expert_id)
    _scope(expert_id, AnalysisUseCase.OPEN_POSITIONS, trigger="manual")
    enter, exit_ = _passes(store)
    assert enter.expert_class == exit_.expert_class == "FMPRating"       # not "TradeManager"
    assert enter.expert_instance_id == expert_id and enter.symbol == ""
    assert enter.use_case == AnalysisUseCase.ENTER_MARKET.value
    assert enter.analysis_id.startswith(f"decision-{expert_id}-")
    assert exit_.use_case == AnalysisUseCase.OPEN_POSITIONS.value
    assert exit_.analysis_id.startswith(f"exit-decision-{expert_id}-")
    assert enter.branch_flags["record_kind"] == "decision_pass"
    assert enter.branch_flags["pass"] == "enter-market" and enter.branch_flags["trigger"] == "scheduler"
    assert exit_.branch_flags["pass"] == "open-positions" and exit_.branch_flags["trigger"] == "manual"


def test_the_pass_meta_records_the_hosts_timezone(store):
    _scope(_expert())
    (record,) = _passes(store)
    zone = record.branch_flags["host_timezone"]
    assert set(zone) == {"name", "utc_offset_seconds"} and isinstance(zone["utc_offset_seconds"], int)


def test_a_gated_expert_still_gets_a_scope_without_a_resolver_lookup(store, monkeypatch):
    def _boom(expert_id):
        raise AssertionError("the scope must not look the resolver up")

    monkeypatch.setattr(mcl, "resolver_for_expert_instance", _boom)
    _scope(_expert())
    assert len(_passes(store)) == 1


def test_capture_off_is_a_plain_no_scope(monkeypatch):
    monkeypatch.setattr(replay_pkg, "get_replay_store", lambda: None)
    assert _scope(_expert()) is None


def test_no_open_session_is_counted_logged_once_and_the_pass_is_not_blocked(store, monkeypatch):
    from ba2_trade_platform.core import TradeManager as tm_mod

    store._session_id = None
    errors = []
    monkeypatch.setattr(tm_mod.logger, "error", lambda msg, *a, **k: errors.append(msg))
    assert _scope(_expert()) is None
    assert store.health.count(CaptureHealth.PASS_NOT_RECORDED) == 1
    assert len(errors) == 1 and "not recorded" in errors[0]


def test_a_failed_scope_is_persisted_as_an_error_row_naming_the_cause(store, monkeypatch):
    import ba2_trade_platform.core.TradeManager as tm_mod

    expert_id = _expert("FMPRating")
    real = tm_mod.get_instance
    calls = {"n": 0}

    def _flaky(model, id_, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")
        return real(model, id_, *a, **k)

    monkeypatch.setattr(tm_mod, "get_instance", _flaky)
    monkeypatch.setattr(tm_mod.logger, "error", lambda *a, **k: None)
    assert _scope(expert_id, AnalysisUseCase.OPEN_POSITIONS) is None
    (row,) = _passes(store)
    assert row.outcome == ReplayStatus.OUTCOME_ERROR
    assert "pass not recorded" in row.error and "db hiccup" in row.error
    assert row.branch_flags["record_kind"] == "decision_pass" and row.expert_class == "FMPRating"
    assert store.health.count(CaptureHealth.PASS_NOT_RECORDED) == 1


def test_the_exit_pass_still_runs_when_its_scope_cannot_open(world, store, monkeypatch):
    """Recording is never a dependency of an exit: the scope fails, the exits are evaluated anyway."""
    import ba2_trade_platform.core.TradeManager as tm_mod

    def _boom(self, *a, **k):
        raise RuntimeError("meta build failed")

    monkeypatch.setattr(tm_mod.TradeManager, "_decision_pass_meta", _boom)
    monkeypatch.setattr(tm_mod.logger, "error", lambda *a, **k: None)
    result = _run(world["expert_id"])
    assert result == [{"success": True, "symbol": SYMBOL, "submit_to_broker": True}]
    assert store.health.count(CaptureHealth.PASS_NOT_RECORDED) == 1


def test_lock_miss_discards_and_counts_and_writes_nothing(world, store):
    from ba2_trade_platform.core.TradeManager import TradeManager

    tm = TradeManager()
    lock = tm._processing_lock(world["expert_id"], "open_positions")
    assert lock.acquire(blocking=False)
    try:
        assert tm.process_open_positions_recommendations(world["expert_id"]) == []
    finally:
        lock.release()
    assert _passes(store) == []
    assert store.health.discards() == {"lock_busy": 1}
    assert store.health.count(CaptureHealth.PASS_DISCARDED) == 1
    assert store.health.total == 0                       # a discard is not a failure


def test_an_exit_pass_with_automation_off_is_a_recorded_skip(world, store):
    world["state"]["expert"]._allow = False
    _run(world["expert_id"])
    (row,) = _passes(store)
    assert row.outcome == ReplayStatus.OUTCOME_SKIP
    assert row.skip_reason == "automated trade modification disabled"


def test_the_capture_scope_opens_outside_the_market_condition_scope_on_the_exit_pass(
        world, store, monkeypatch):
    """Order is load-bearing: the capture scope is entered BEFORE the market-condition scope."""
    order = []
    real = replay_pkg.capture_scope

    @contextmanager
    def _cap(st, meta):
        order.append("capture")
        with real(st, meta) as context:
            yield context
        order.append("capture-closed")

    @contextmanager
    def _mc(*, expert_instance_id=None, replay_reader=None):
        order.append("market")
        yield None

    monkeypatch.setattr(replay_pkg, "capture_scope", _cap)
    monkeypatch.setattr(mcl, "market_condition_decision_scope", _mc)
    _run(world["expert_id"])
    assert order.index("capture") < order.index("market") < order.index("capture-closed")


# --- I4: the post-pass bookkeeping is part of the guarded finish -----------------------------------
class _BadOrder:
    @property
    def id(self):
        raise RuntimeError("order row detached")


class _BadData:
    def __bool__(self):
        return True

    def get(self, *a, **k):
        raise RuntimeError("result data unreadable")


def test_enter_pass_bookkeeping_failure_never_loses_the_return_value(store, monkeypatch):
    """The orders were already sent; an error extracting their ids must not replace the return value."""
    import ba2_trade_platform.core.TradeManager as tm_mod
    from ba2_trade_platform.core.TradeManager import TradeManager

    expert_id = _expert("FMPRating")
    sent = [_BadOrder()]
    monkeypatch.setattr(TradeManager, "_process_expert_recommendations_after_analysis",
                        lambda self, *a, **k: sent)
    monkeypatch.setattr(tm_mod.logger, "error", lambda *a, **k: None)
    assert TradeManager().process_expert_recommendations_after_analysis(expert_id) is sent
    assert store.health.count(CaptureHealth.PASS_NOT_RECORDED) == 1


def test_exit_pass_bookkeeping_failure_never_loses_the_return_value(store, monkeypatch):
    import ba2_trade_platform.core.TradeManager as tm_mod
    from ba2_trade_platform.core.TradeManager import TradeManager

    expert_id = _expert("FMPRating")
    sent = [{"success": True, "data": _BadData()}]
    monkeypatch.setattr(TradeManager, "_process_open_positions_recommendations",
                        lambda self, *a, **k: sent)
    monkeypatch.setattr(tm_mod.logger, "error", lambda *a, **k: None)
    assert TradeManager().process_open_positions_recommendations(expert_id) is sent
    assert store.health.count(CaptureHealth.PASS_NOT_RECORDED) == 1


# --- I3: the rollout allow-list --------------------------------------------------------------------
def _set_allowlist(value):
    from ba2_trade_platform.core.db import add_instance
    from ba2_trade_platform.core.models import AppSetting

    add_instance(AppSetting(key="replay_decision_pass_experts", value_str=value))


def test_default_records_every_expert(store):
    _scope(_expert("FMPRating"))
    _scope(_expert("DeterministicScorer"))
    assert len(_passes(store)) == 2


def test_the_allowlist_restricts_the_rollout_and_counts_the_rest_as_discards(store):
    _set_allowlist(" FMPRating , DeterministicScorer ")
    _scope(_expert("FMPRating"))
    _scope(_expert("FMPEarningsDrift"))
    assert [p.expert_class for p in _passes(store)] == ["FMPRating"]
    assert store.health.discards() == {"expert_not_allowed": 1}
    assert store.health.discards_by_expert() == {"FMPEarningsDrift": 1}
    assert store.health.total == 0


def test_a_star_or_blank_allowlist_means_all(store):
    _set_allowlist("*")
    _scope(_expert("FMPEarningsDrift"))
    assert len(_passes(store)) == 1


def test_an_unreadable_allowlist_records_nothing_loudly_and_never_blocks_the_pass(store, monkeypatch):
    import ba2_trade_platform.core.TradeManager as tm_mod
    from ba2_trade_platform.core import replay_capture

    def _boom():
        raise RuntimeError("settings table unreadable")

    monkeypatch.setattr(replay_capture, "decision_pass_expert_allowlist", _boom)
    monkeypatch.setattr(tm_mod.logger, "error", lambda *a, **k: None)
    assert _scope(_expert("FMPRating")) is None
    assert store.health.count(CaptureHealth.PASS_NOT_RECORDED) == 1


def test_an_exception_in_a_pass_of_a_non_allowed_expert_propagates_untouched(store):
    from ba2_trade_platform.core.TradeManager import TradeManager

    _set_allowlist("FMPRating")
    expert_id = _expert("FMPEarningsDrift")
    with pytest.raises(ValueError, match="pass body"):
        with TradeManager()._decision_capture_scope(expert_id) as context:
            assert context is None
            raise ValueError("pass body")
