"""``ba2-test replay prune --store <dir> --keep-days N [--dry-run]``: the CLI over the shared retention function."""
import os
import types
from datetime import datetime, timedelta, timezone

import pytest

import ba2test_launcher
from ba2_common.core.replay import ReplayStatus, ReplayStore, SessionRecord

OLD = datetime.now(timezone.utc) - timedelta(days=60)


@pytest.fixture
def store(tmp_path):
    s = ReplayStore(tmp_path / "store", writer="sync")
    for sid, started in (("old", OLD), ("live", datetime.now(timezone.utc))):
        s.begin_session(SessionRecord(
            session_id=sid, instance_id="i", started_at=started, exchange_tz="America/New_York",
            app_version="x", package_versions={}, source_revision="abc", dirty=False))
    s.publish_session_object({"patch": "old-only"})
    s.index.update_session_status("old", ReplayStatus.SESSION_FINALIZED, ended_at=OLD)
    for path in (s.root / "objects").rglob("*.json"):
        os.utime(path, (OLD.timestamp(), OLD.timestamp()))
    yield s
    s.close(timeout=2.0)


def _prune(store, *, keep_days, dry_run):
    args = types.SimpleNamespace(replay_cmd="prune", store=str(store.root), keep_days=keep_days, dry_run=dry_run)
    return ba2test_launcher._cmd_replay(args)


def test_dry_run_prints_what_would_go_and_deletes_nothing(store, capsys):
    assert _prune(store, keep_days=30, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "would delete 1 session(s)" in out and "old" in out and "dry run" in out
    assert {s.session_id for s in store.index.list_sessions()} == {"old", "live"}


def test_a_real_run_deletes_the_old_session_and_keeps_the_open_one(store, capsys):
    assert _prune(store, keep_days=30, dry_run=False) == 0
    assert "deleted 1 session(s)" in capsys.readouterr().out
    assert [s.session_id for s in store.index.list_sessions()] == ["live"]


def test_a_refusal_is_a_loud_exit_not_a_silent_success(store):
    with pytest.raises(SystemExit) as exc:
        _prune(store, keep_days=0, dry_run=False)
    assert "keep_days" in str(exc.value)
    assert {s.session_id for s in store.index.list_sessions()} == {"old", "live"}
