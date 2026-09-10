"""The live host's capture switch, store root, session and batch linkage (spec step 2).

This is the layer that decides WHETHER to record, WHERE, and FOR WHICH INSTANCE.
Four things must hold, because each of them silently breaks something else when
it does not:

* the switch is OFF until someone turns it on, and the setting row exists so it
  CAN be turned on from the UI (there is no migration for AppSetting rows);
* the store lands under this instance's cache folder, not a shared default;
* a session is bounded by the UTC date, so an export names a day rather than
  "everything since the process started";
* the batch id reaches the RECORD and nothing else -- never a trading row.
"""
import os

import pytest

from ba2_common.core.db import get_setting
from ba2_common.core.replay import get_replay_store
from ba2_trade_platform.core import replay_capture
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import AppSetting


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Point this instance's cache folder at a temp dir and always tear capture down."""
    import ba2_trade_platform.config as config

    monkeypatch.setattr(config, "CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "DB_FILE", str(tmp_path / "db.sqlite"))
    try:
        yield tmp_path
    finally:
        replay_capture.shutdown_replay_capture(timeout=5.0)
        replay_capture.clear_current_batch()


def _enable():
    add_instance(AppSetting(key=replay_capture.CAPTURE_SETTING_KEY, value_str="true"))


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #
def test_missing_setting_is_created_as_false_and_capture_stays_off(cache_root):
    assert get_setting(replay_capture.CAPTURE_SETTING_KEY) is None

    store = replay_capture.initialize_replay_capture()

    assert store is None, "capture must be OFF until someone turns it on"
    assert get_replay_store() is None, "no store means every tap is a passthrough"
    assert get_setting(replay_capture.CAPTURE_SETTING_KEY) == "false", (
        "the row must exist so the switch is reachable from the UI (no migration "
        "creates AppSetting rows)")
    assert not os.path.exists(replay_capture.store_root()), (
        "capture off must not create a store directory")


@pytest.mark.parametrize("spelling", ["true", "True", "1", "yes"])
def test_any_true_spelling_turns_capture_on(cache_root, spelling):
    """coerce_bool, not ``== 'true'``: a setting written as 1 means ON."""
    add_instance(AppSetting(key=replay_capture.CAPTURE_SETTING_KEY, value_str=spelling))
    try:
        assert replay_capture.initialize_replay_capture() is not None
    finally:
        replay_capture.shutdown_replay_capture(timeout=5.0)


# --------------------------------------------------------------------------- #
# Where, and describing what was running
# --------------------------------------------------------------------------- #
def test_enabled_opens_the_store_under_this_instances_cache_folder(cache_root):
    _enable()
    store = replay_capture.initialize_replay_capture()

    assert store is not None
    assert get_replay_store() is store
    expected = os.path.join(str(cache_root / "cache"), "replay", "v1")
    assert str(store.root) == expected
    assert os.path.isdir(expected)
    assert store.writer == "thread", "recording must not wait on disk in a worker"


def test_the_session_describes_the_running_build(cache_root):
    from ba2_trade_platform.version import APP_VERSION

    _enable()
    store = replay_capture.initialize_replay_capture()
    session = store.index.get_session(store.session_id)

    assert session.app_version == APP_VERSION
    assert set(session.package_versions) == {"ba2_common", "ba2_providers", "ba2_experts"}
    assert session.exchange_tz == "America/New_York"
    assert session.instance_id and len(session.instance_id) == 16
    assert str(cache_root) not in session.instance_id, (
        "the instance id must be opaque -- an export must not carry a home directory")
    assert list(session.capabilities["recorded_experts"]) == list(
        replay_capture.RECORDED_EXPERTS)
    # source_revision is either a real revision or an honest None -- never invented.
    assert session.source_revision is None or len(session.source_revision) == 40


def test_two_instances_get_different_ids(cache_root, monkeypatch):
    import ba2_trade_platform.config as config

    first = replay_capture._instance_id()
    monkeypatch.setattr(config, "DB_FILE", str(cache_root / "other.sqlite"))
    assert replay_capture._instance_id() != first


# --------------------------------------------------------------------------- #
# Sessions roll at the UTC date change
# --------------------------------------------------------------------------- #
def test_session_rolls_over_at_the_utc_date_change(cache_root):
    _enable()
    store = replay_capture.initialize_replay_capture()
    first = store.session_id

    # The rollover check compares the OPEN session's date to today's: pretend the
    # open one belongs to yesterday, exactly as it does after midnight.
    replay_capture._SESSION_DATE = "2020-01-01"
    store.current_session_id()
    second = store.session_id

    assert second != first
    assert store.index.get_session(first).status == "finalized", (
        "the session we rolled away from must be closed, not left open forever")
    assert store.index.get_session(second).status == "open"


def test_the_rollover_check_is_free_on_the_same_day(cache_root):
    _enable()
    store = replay_capture.initialize_replay_capture()
    first = store.session_id

    for _ in range(5):
        assert store.current_session_id() == first


# --------------------------------------------------------------------------- #
# Batch linkage
# --------------------------------------------------------------------------- #
def test_batch_id_is_thread_local_and_reaches_the_record(cache_root):
    """The record names the batch; nothing is written to a trading row to get it."""
    import threading

    from ba2_common.core.interfaces.MarketExpertInterface import _current_capture_batch

    _enable()
    store = replay_capture.initialize_replay_capture()

    replay_capture.set_current_batch("7_0930_20260910")
    assert _current_capture_batch() == "7_0930_20260910"

    seen = {}

    def worker():
        seen["other_thread"] = _current_capture_batch()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen["other_thread"] is None, (
        "a process-wide batch id would attribute one expert's analyses to another's batch")

    replay_capture.clear_current_batch()
    assert _current_capture_batch() is None


def test_the_batch_id_lands_on_the_analysis_record(cache_root):
    from ba2_common.core.replay import capture_scope

    _enable()
    store = replay_capture.initialize_replay_capture()
    replay_capture.set_current_batch("batch-42")

    from ba2_trade_platform.core.interfaces.MarketExpertInterface import (
        MarketExpertInterface,
    )

    class _Expert(MarketExpertInterface):
        def __init__(self):
            self.id = 3

        @classmethod
        def description(cls):
            return "test"

        def render_market_analysis(self, market_analysis):
            return ""

        def run_analysis(self, symbol, market_analysis):
            return None

    class _MA:
        id = 99
        symbol = "AAPL"
        subtype = None
        created_at = None

    expert = _Expert()
    with expert._analysis_capture(_MA(), {"a": 1}, "enter_market") as context:
        assert context is not None
        context.set_outcome(skip_reason="nothing to do")

    store.drain(timeout=10.0)
    records = store.index.analyses(store.session_id)
    assert len(records) == 1
    assert records[0].branch_flags["batch_id"] == "batch-42"
    assert records[0].expert_instance_id == 3
    assert records[0].symbol == "AAPL"
    assert records[0].outcome == "skip"
    assert records[0].skip_reason == "nothing to do"


# --------------------------------------------------------------------------- #
# Failure behaviour
# --------------------------------------------------------------------------- #
def test_initialize_is_idempotent(cache_root):
    _enable()
    first = replay_capture.initialize_replay_capture()
    assert replay_capture.initialize_replay_capture() is first


def test_capture_health_is_empty_when_capture_is_off(cache_root):
    assert replay_capture.get_capture_health() == {}


def test_shutdown_finalizes_the_session_and_uninstalls_the_store(cache_root):
    _enable()
    store = replay_capture.initialize_replay_capture()
    session_id = store.session_id

    replay_capture.shutdown_replay_capture(timeout=5.0)

    assert get_replay_store() is None
    from ba2_common.core.replay import ReplayStore

    reopened = ReplayStore(store.root, writer="sync")
    try:
        assert reopened.index.get_session(session_id).status == "finalized"
    finally:
        reopened.close()
