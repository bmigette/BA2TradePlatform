"""Dirty-tree provenance: the exact uncommitted code a session ran is recorded, exported and
comparable; schema-1 sessions are still read; a read-only store opens beside a writer."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ba2_common.core.replay import provenance as prov
from ba2_common.core.replay.schemas import SCHEMA_VERSION, ReplayStatus, SessionRecord
from ba2_common.core.replay.service import ReplayStore, load_bundle
from ba2_common.core.replay.store import ReplayStoreError

NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "ba2_trade_platform").mkdir(parents=True)
    (repo / "ba2_trade_platform" / "version.py").write_text('APP_VERSION = "2026.11.00001"\n')
    (repo / "ba2_trade_platform" / "mod.py").write_text("X = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _session(store_root=None, **kw):
    base = dict(
        session_id="s1", instance_id="i", started_at=NOW, exchange_tz="America/New_York",
        app_version="2026.11.00001", package_versions={}, source_revision="abc",
        dirty=True, status=ReplayStatus.SESSION_OPEN)
    base.update(kw)
    return SessionRecord(**base)


def test_schema_version_bumped_and_old_session_reads():
    assert SCHEMA_VERSION == 2
    old = {k: v for k, v in _session().to_mapping().items()
           if k not in ("provenance", "provenance_error")}
    old["schema_version"] = 1
    rec = SessionRecord.from_mapping(old)
    assert rec.provenance == {} and rec.provenance_error is None
    state = prov.recorded_code(rec)
    assert state.unknown == "provenance: dirty, patch not recorded (pre-schema 2)"


def test_clean_tree_records_no_patch(tmp_path):
    repo = _make_repo(tmp_path)
    published = []
    p, err = prov.collect_provenance(repo, lambda v: published.append(v) or "h")
    assert err is None and published == []
    assert p["patch_sha256"] is None and p["untracked"] == []
    assert p["versions"]["app_version"] == "2026.11.00001"


def test_dirty_tree_records_patch_untracked_and_versions(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "ba2_trade_platform" / "mod.py").write_text("X = 2\n")
    (repo / "ba2_trade_platform" / "new.py").write_text("Y = 1\n")
    (repo / "ba2_trade_platform" / "big.py").write_text("Z = 1\n" + "#" * (300 * 1024))
    (repo / "notes.txt").write_text("not code")
    published = {}

    def publish(value):
        key = f"h{len(published)}"
        published[key] = value
        return key

    p, err = prov.collect_provenance(repo, publish)
    assert err is None
    snap = prov.snapshot_code(repo)
    assert p["patch_sha256"] == snap.patch_sha256 and p["patch_object"]
    assert published[p["patch_object"]]["sha256"] == snap.patch_sha256
    by_path = {e["path"]: e for e in p["untracked"]}
    assert set(by_path) == {"ba2_trade_platform/new.py", "ba2_trade_platform/big.py"}
    assert by_path["ba2_trade_platform/new.py"]["content_capped"] is False
    assert by_path["ba2_trade_platform/new.py"]["content_object"]
    big = by_path["ba2_trade_platform/big.py"]
    assert big["content_capped"] is True and big["content_object"] is None and big["sha256"]


def test_collection_failure_is_returned_not_raised(tmp_path):
    p, err = prov.collect_provenance(tmp_path / "not-a-repo", lambda v: "h")
    assert err and "code snapshot" in err


def test_compare_code_drift_and_unknown(tmp_path):
    repo = _make_repo(tmp_path)
    rev = prov.snapshot_code(repo).revision
    clean = prov.running_code(repo)
    (repo / "ba2_trade_platform" / "mod.py").write_text("X = 2\n")
    dirty = prov.running_code(repo)
    same = prov.compare_code(clean, clean)
    assert not same.drift and same.line == f"recorded on {rev[:8]}, running {rev[:8]}"
    cmp = prov.compare_code(clean, dirty)
    assert cmp.drift and "uncommitted patch differs" in cmp.reasons
    assert f"running {rev[:8]}+patch {dirty.patch_sha256[:8]}" in cmp.line
    # an old dirty recording: patch unknown -> reported, never declared drift or match
    old = prov.recorded_code(_session(source_revision=rev))
    cmp = prov.compare_code(old, dirty)
    assert not cmp.drift and "patch not recorded" in cmp.line
    # different revision is drift
    other = prov.compare_code(prov.recorded_code(_session(source_revision="0" * 40, dirty=False)),
                              clean)
    assert other.drift and "revision differs" in other.reasons


def test_session_roundtrip_export_ships_provenance_objects(tmp_path):
    store = ReplayStore(tmp_path / "store")
    repo = _make_repo(tmp_path)
    (repo / "ba2_trade_platform" / "mod.py").write_text("X = 2\n")
    p, err = prov.collect_provenance(repo, store.publish_session_object)
    assert err is None
    store.begin_session(_session(provenance=p))
    out = store.export_session("s1", tmp_path / "bundle")
    bundle = load_bundle(out)
    assert bundle.session.provenance["patch_sha256"] == p["patch_sha256"]
    assert bundle.decode(p["patch_object"])["sha256"] == p["patch_sha256"]
    store.close()


def test_read_only_store_exports_beside_a_live_writer(tmp_path):
    live = ReplayStore(tmp_path / "store")
    live.begin_session(_session(provenance={}, provenance_error="boom"))
    before = (tmp_path / "store" / "index.sqlite").stat().st_mtime_ns
    ro = ReplayStore(tmp_path / "store", read_only=True)
    out = ro.export_session("s1", tmp_path / "out")
    assert load_bundle(out).session.provenance_error == "boom"
    with pytest.raises(Exception):
        ro.index._connection().execute("DELETE FROM sessions")
    ro.index.close()
    live.begin_session(_session(session_id="s2"))   # the live writer still works
    assert before <= (tmp_path / "store" / "index.sqlite").stat().st_mtime_ns
    with pytest.raises(ReplayStoreError):
        ReplayStore(tmp_path / "nope", read_only=True)
    live.close()
