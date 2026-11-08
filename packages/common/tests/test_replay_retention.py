"""Replay store retention: one shared prune, safe beside a live writer, nothing automatic."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from ba2_common.core.replay import retention
from ba2_common.core.replay.context import capture_scope
from ba2_common.core.replay.schemas import ReplayStatus, SessionRecord
from ba2_common.core.replay.service import ReplayStore, load_bundle

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def _session(session_id, started):
    return SessionRecord(
        session_id=session_id, instance_id="inst", started_at=started, exchange_tz="America/New_York",
        app_version="x", package_versions={}, source_revision="abc", dirty=False)


def _frame():
    idx = pd.DatetimeIndex(["2026-09-08", "2026-09-09"], tz="America/New_York", name="ts")
    return pd.DataFrame({"close": [1.5, 2.5]}, index=idx)


def _record(store, session_id, analysis_id, started, marker):
    """One analysis with a UNIQUE observation payload and a SHARED frame (same bytes in every session)."""
    meta = {"analysis_id": analysis_id, "attempt_id": "att", "session_id": session_id,
            "expert_class": "FMPRating", "expert_instance_id": 1, "symbol": "AAPL",
            "use_case": ReplayStatus.USE_CASE_ENTER_MARKET, "scheduled_at": started, "started_at": started}
    with capture_scope(store, meta) as ctx:
        ctx.set_bundle({"symbol": "AAPL", "ohlcv": _frame(), "current_price": 1.0})
        ctx.record_observation(provider="fmp", method="m", request_identity={"s": marker},
                               payload=[{"unique": marker}], provenance=ReplayStatus.PROVENANCE_NETWORK)


def _age_all_objects(store, when):
    stamp = when.timestamp()
    for path in (store.root / "objects").rglob("*"):
        if path.is_file():
            os.utime(path, (stamp, stamp))


def _finish(store, session_id, ended):
    store.index.update_session_status(session_id, ReplayStatus.SESSION_FINALIZED, ended_at=ended)


@pytest.fixture
def world(tmp_path):
    """old (finalized 40d ago), recent (finalized 2d ago), live (open). The old session's objects are aged."""
    store = ReplayStore(tmp_path / "store", writer="sync")
    for sid, started in (("old", NOW - 40 * DAY), ("recent", NOW - 2 * DAY), ("live", NOW - timedelta(hours=1))):
        store.begin_session(_session(sid, started))
    _record(store, "old", "a-old", NOW - 40 * DAY, "old")
    _finish(store, "old", NOW - 40 * DAY)
    _age_all_objects(store, NOW - 40 * DAY)
    _record(store, "recent", "a-recent", NOW - 2 * DAY, "recent")      # re-uses the shared frame: mtime refreshed
    _finish(store, "recent", NOW - 2 * DAY)
    _record(store, "live", "a-live", NOW - timedelta(hours=1), "live")
    yield store
    store.close(timeout=2.0)


def _hashes(store):
    return {p.stem for p in (store.root / "objects").rglob("*.*") if p.suffix in (".json", ".arrow")}


def _later():
    return datetime.now(timezone.utc)


def test_preview_deletes_nothing_and_reports_what_execute_will_delete(world):
    before = _hashes(world)
    plan = retention.preview_prune(world.root, 30, now=_later())

    assert [s["session_id"] for s in plan["sessions_delete"]] == ["old"]
    assert plan["sessions_kept"] == 1 and plan["sessions_open_protected"] == ["live"]
    assert plan["objects_delete"] > 0 and plan["bytes_delete"] > 0 and plan["problems"] == []
    assert _hashes(world) == before
    assert world.index.get_session("old")                      # still there


def test_execute_deletes_old_session_and_its_unique_objects_and_keeps_everything_else(world):
    now = _later()
    plan = retention.preview_prune(world.root, 30, now=now)
    old_unique = world.index.root_hashes("old") - world.index.root_hashes("recent") - world.index.root_hashes("live")
    assert old_unique

    result = retention.execute_prune(world.root, 30, now=now)

    assert result["executed"] is True
    assert result["objects_delete"] == plan["objects_delete"] and result["bytes_delete"] == plan["bytes_delete"]
    assert not (old_unique & _hashes(world))                   # its unique payload is gone
    assert sorted(s.session_id for s in world.index.list_sessions()) == ["live", "recent"]
    # the kept sessions still export and load, hash-verified, with their shared frame intact
    for sid in ("recent", "live"):
        bundle = load_bundle(world.export_session(sid, world.root.parent / f"out-{sid}"))
        assert len(bundle.analyses) == 1
        assert bundle.decode(bundle.analyses[0].bundle_object)["ohlcv"] is not None
    assert not any(world.index.has_object(h) for h in old_unique)
    assert all(world.index.has_object(h) for h in world.index.root_hashes("recent"))


def test_the_shared_object_survives_because_a_kept_session_reaches_it(world):
    shared = world.index.root_hashes("old") & world.index.root_hashes("recent")
    # the bundle object links to the shared frame: reachability follows the link, so look one level down
    frames = {p.stem for p in (world.root / "objects").rglob("*.arrow")}
    assert frames, "the fixture must publish a frame shared by an old and a kept session"
    stamp = (NOW - 90 * DAY).timestamp()
    for path in (world.root / "objects").rglob("*.arrow"):
        os.utime(path, (stamp, stamp))             # old by AGE: only reachability (the $frame link) can save it
    retention.execute_prune(world.root, 30, now=_later())
    assert frames <= _hashes(world)
    assert shared <= _hashes(world)


def test_an_open_session_is_never_deleted_however_old(world):
    result = retention.execute_prune(world.root, 1, now=_later() + 400 * DAY)
    assert "live" in result["sessions_open_protected"]
    assert any(s.session_id == "live" for s in world.index.list_sessions())
    assert world.index.root_hashes("live") <= _hashes(world)


def test_an_unreferenced_object_is_deleted_only_when_older_than_the_age_bound(world):
    old_orphan = world.root / "objects" / "ab" / ("a" * 64 + ".json")
    young_orphan = world.root / "objects" / "cd" / ("c" * 64 + ".json")
    for path in (old_orphan, young_orphan):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}")
    aged = (NOW - 90 * DAY).timestamp()
    os.utime(old_orphan, (aged, aged))

    result = retention.execute_prune(world.root, 30, now=_later())

    assert not old_orphan.exists()                             # crash residue, long unreferenced
    assert young_orphan.exists()                               # could be a live writer's, not yet committed
    assert result["objects_kept_young"] >= 1


def test_republishing_an_existing_object_refreshes_its_age(world):
    path = next(p for p in (world.root / "objects").rglob("*.json"))
    aged = (NOW - 90 * DAY).timestamp()
    os.utime(path, (aged, aged))
    world.objects.put("json", path.read_bytes())
    assert path.stat().st_mtime > aged + DAY.total_seconds()


def test_a_kept_session_with_a_missing_object_refuses_before_deleting_anything(world):
    victim = next(iter(world.index.root_hashes("recent") - world.index.root_hashes("old")))
    next(p for p in (world.root / "objects").rglob("*") if p.stem == victim).unlink()
    before_sessions = [s.session_id for s in world.index.list_sessions()]

    with pytest.raises(retention.RetentionRefused, match=victim):
        retention.execute_prune(world.root, 30, now=_later())

    assert [s.session_id for s in world.index.list_sessions()] == before_sessions
    assert retention.preview_prune(world.root, 30)["problems"]            # the preview says so too


@pytest.mark.parametrize("bad", [0, -3, 1.5, "30", None, True])
def test_keep_days_must_be_a_whole_positive_number(world, bad):
    with pytest.raises(retention.RetentionRefused, match="keep_days"):
        retention.preview_prune(world.root, bad)
    with pytest.raises(retention.RetentionRefused, match="keep_days"):
        retention.execute_prune(world.root, bad)


def test_a_directory_that_is_not_a_store_is_refused(tmp_path):
    with pytest.raises(retention.RetentionRefused, match="not a replay store"):
        retention.statistics(tmp_path)
    with pytest.raises(retention.RetentionRefused, match="not a replay store"):
        retention.execute_prune(tmp_path, 30)


def test_a_second_prune_is_refused_while_the_lock_is_held(world):
    (world.root / retention.LOCK_NAME).write_text("pid=1")
    with pytest.raises(retention.RetentionRefused, match="another prune"):
        retention.execute_prune(world.root, 30)
    assert any(s.session_id == "old" for s in world.index.list_sessions())


def test_a_file_that_cannot_be_deleted_is_raised_not_skipped(world, monkeypatch):
    real_unlink = Path.unlink

    def failing(self, *a, **k):
        if self.suffix in (".json", ".arrow"):
            raise PermissionError("locked")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", failing)
    with pytest.raises(retention.RetentionError, match="could not be deleted"):
        retention.execute_prune(world.root, 30, now=_later())
    monkeypatch.undo()
    assert not (world.root / retention.LOCK_NAME).exists()                # the lock is released regardless


def test_statistics_report_sessions_objects_bytes_and_dates(world):
    stats = retention.statistics(world.root)
    assert stats["sessions"] == 3 and stats["sessions_by_status"] == {"finalized": 2, "open": 1}
    assert stats["objects"] == len(_hashes(world)) and stats["object_bytes"] > 0
    assert stats["total_bytes"] == stats["object_bytes"] + stats["index_bytes"]
    assert stats["oldest_session"].startswith("2026-09-29") and stats["newest_session"].startswith("2026-11-08")
    assert sum(stats["bytes_by_day"].values()) <= stats["object_bytes"]


# --------------------------------------------------------------------------- prune vs live writer races
def _prunable(world):
    """(hash, kind, data, path) of an object only the old session reaches: the prune WILL delete it."""
    unique = world.index.root_hashes("old") - world.index.root_hashes("recent") - world.index.root_hashes("live")
    object_hash = sorted(unique)[0]
    kind, data, _ = world.objects.get(object_hash)
    return object_hash, kind, data, world.objects.path_for(object_hash, kind)


def test_a_republish_between_the_second_scan_and_the_unlink_keeps_the_object(world, monkeypatch):
    """Race A: the second scan saw the file old, then a live pass re-published identical content."""
    object_hash, kind, data, path = _prunable(world)
    real_scan = retention._scan
    calls = []

    def scan_then_republish(*args, **kwargs):
        scan = real_scan(*args, **kwargs)
        calls.append(1)
        if len(calls) == 2:
            assert any(h == object_hash for h, _, _ in scan.delete_files)      # the stale scan lists it as old
            world.objects.put(kind, data)                                     # live writer: touch
        return scan

    monkeypatch.setattr(retention, "_scan", scan_then_republish)
    result = retention.execute_prune(world.root, 30, now=_later())
    assert path.is_file() and world.objects.get(object_hash)[1] == data
    assert result["objects_skipped_republished"] >= 1


def test_a_republish_after_the_prune_moved_the_file_aside_keeps_the_object(world, monkeypatch):
    """Race A, tighter: the writer touches between the prune's stat and its delete (file already moved aside)."""
    object_hash, kind, data, path = _prunable(world)
    real_replace = os.replace

    def replace_then_republish(src, dst, *a, **k):
        real_replace(src, dst, *a, **k)
        if Path(src) == path:
            world.objects.put(kind, data)           # path is gone: put rewrites it
    monkeypatch.setattr(retention.os, "replace", replace_then_republish)
    retention.execute_prune(world.root, 30, now=_later())
    assert path.is_file() and world.objects.get(object_hash)[1] == data


def test_put_whose_file_is_deleted_before_the_touch_rewrites_it(world, monkeypatch):
    """Race B: put saw the file, the prune unlinked it, the touch hit FileNotFoundError."""
    object_hash, kind, data, path = _prunable(world)
    real_utime = os.utime

    def unlink_then_utime(target, *a, **k):
        if Path(target) == path and path.exists():
            path.unlink()
        return real_utime(target, *a, **k)

    monkeypatch.setattr("ba2_common.core.replay.store.os.utime", unlink_then_utime)
    assert world.objects.put(kind, data) == object_hash
    assert path.is_file() and world.objects.get(object_hash)[1] == data


def test_concurrent_writer_never_loses_an_object_to_a_prune(world):
    import threading
    object_hash, kind, data, path = _prunable(world)
    stop = threading.Event()
    errors = []

    def writer():
        while not stop.is_set():
            try:
                world.objects.put(kind, data)
                if not path.is_file():
                    errors.append("put returned for a missing file")
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(15):
            _age_all_objects(world, NOW - 90 * DAY)
            try:
                retention.execute_prune(world.root, 30, now=_later())
            except retention.RetentionError:
                pass
            world.objects.put(kind, data)
    finally:
        stop.set()
        thread.join()
    assert not errors, errors[:3]
    assert path.is_file()


# --------------------------------------------------------------------------- prune cost / lock time
def test_sessions_are_deleted_in_short_batches(world, monkeypatch):
    from ba2_common.core.replay import store as store_module

    for n in range(6):
        sid = f"old-{n}"
        world.begin_session(_session(sid, NOW - 50 * DAY))
        _record(world, sid, f"a-{sid}", NOW - 50 * DAY, sid)
        _finish(world, sid, NOW - 50 * DAY)
    entered = []
    real_enter = store_module._transaction.__enter__

    def counting(self):
        entered.append(1)
        return real_enter(self)

    monkeypatch.setattr(store_module._transaction, "__enter__", counting)
    assert world.index.delete_sessions([f"old-{n}" for n in range(6)], batch_size=2) == 6
    assert len(entered) == 3                                   # one short transaction per batch
    assert not any(s.session_id.startswith("old-") for s in world.index.list_sessions())


def test_a_batched_delete_still_refuses_an_open_or_unknown_session_before_deleting_any(world):
    from ba2_common.core.replay.store import ReplayStoreError

    with pytest.raises(ReplayStoreError):
        world.index.delete_sessions(["old", "live"], batch_size=1)          # live is open
    assert any(s.session_id == "old" for s in world.index.list_sessions())


def test_a_closed_kept_session_is_read_once_per_prune(world, monkeypatch):
    calls = []
    real = type(world.index).root_hashes

    def counting(self, session_id):
        calls.append(session_id)
        return real(self, session_id)

    monkeypatch.setattr(type(world.index), "root_hashes", counting)
    retention.execute_prune(world.root, 30, now=_later())
    assert calls.count("recent") == 1                  # finalized: cannot change, scanned once for both scans
    assert calls.count("live") == 2                    # open: a writer may add to it between the scans
