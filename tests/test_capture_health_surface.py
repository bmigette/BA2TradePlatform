"""Capture health is VISIBLE: the ops endpoint, the session record at finalize.

* ``GET /api/v1/capture-health`` (same access rule as every read) returns the live counters of the
  installed ``ReplayStore``: on/off, the open session, every kind, the discards by reason and by expert.
  With capture off it says ``enabled: false`` (never an empty "all clear").
* ``finalize_session`` freezes the counters into ``capabilities.capture_health`` of the session, so an
  exported bundle states what its recorder failed to record.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ba2_common.core.replay import ReplayStore, SessionRecord, set_replay_store
from ba2_common.core.replay.context import CaptureHealth
from tests.api_helpers import v1_client

pytestmark = pytest.mark.api_db

NOW = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = ReplayStore(tmp_path / "store", writer="sync")
    s.begin_session(SessionRecord(
        session_id="s1", instance_id="i", started_at=NOW, exchange_tz="America/New_York",
        app_version="x", package_versions={}, source_revision="abc", dirty=False))
    set_replay_store(s)
    yield s
    set_replay_store(None)
    s.close(timeout=2.0)


def test_the_endpoint_says_capture_is_off_rather_than_reporting_a_clean_bill():
    set_replay_store(None)
    body = v1_client().get("/capture-health").json()
    assert body["enabled"] is False and body["session_id"] is None and body["counters"] == {}


def test_the_endpoint_shows_every_counter_and_the_discards(store):
    store.health.record(CaptureHealth.PASS_NOT_RECORDED)
    store.health.record(CaptureHealth.STEP_NOT_RECORDED)
    store.health.record_discard("lock_miss", "FMPRating")
    store.health.record_discard("pending_jobs", "FMPRating")

    body = v1_client().get("/capture-health").json()

    assert body["enabled"] is True and body["session_id"] == "s1"
    assert body["counters"]["pass_not_recorded"] == 1 and body["counters"]["step_not_recorded"] == 1
    assert body["counters"]["pass_discarded"] == 2
    assert body["discards"] == {"lock_miss": 1, "pending_jobs": 1}
    assert body["discards_by_expert"] == {"FMPRating": 2}
    assert body["total_failures"] == 2                      # a discard is accounted for, not a failure


def test_the_endpoint_follows_the_access_rule():
    from tests.api_helpers import set_api_token, TOKEN

    set_api_token()
    assert v1_client(host="10.1.1.1", token=TOKEN).get("/capture-health").status_code == 403   # remote off
    assert v1_client(host="10.1.1.1").get("/capture-health").status_code == 403


def test_finalize_writes_the_health_counters_into_the_session_capabilities(store):
    store.health.record(CaptureHealth.DISK_ERROR)
    store.health.record_discard("lock_miss", "FMPRating")

    store.finalize_session("s1")

    health = store.index.get_session("s1").capabilities["capture_health"]
    assert health["counters"]["disk_error"] == 1 and health["counters"]["pass_discarded"] == 1
    assert health["discards"] == {"lock_miss": 1} and health["discards_by_expert"] == {"FMPRating": 1}
    assert health["total_failures"] == 1 and health["scope"] == "process_cumulative"
    # other capabilities survive the update
    assert store.index.get_session("s1").status == "finalized"


def test_the_exported_bundle_carries_the_health(store, tmp_path):
    from ba2_common.core.replay import load_bundle

    store.health.record(CaptureHealth.QUEUE_SATURATION)
    store.finalize_session("s1")
    bundle = load_bundle(store.export_session("s1", tmp_path / "bundle"))
    assert bundle.session.capabilities["capture_health"]["counters"]["queue_saturation"] == 1
