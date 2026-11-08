"""Code provenance on the replay side: the revision line, `code_drift`, `checkout`, `export`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ba2_common.core.replay import ReplayStatus
from ba2_common.core.replay import provenance as prov
from ba2_common.core.replay.schemas import SessionRecord
from ba2_common.core.replay.service import ReplayStore, load_bundle

from app.services.replay import expert_replay, provenance
from app.services.replay.report import AnalysisResult, ReplayReport, STATUS_ORDER
from tests.replay import capture_session

LAUNCHER = Path(__file__).resolve().parents[3] / "ba2test_launcher.py"
REPO_ROOT = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "ba2_trade_platform").mkdir(parents=True)
    (repo / "ba2_trade_platform" / "mod.py").write_text("X = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _recorded_bundle(tmp_path, repo):
    """A bundle recorded on `repo` while dirty (mod.py edited, new.py untracked)."""
    (repo / "ba2_trade_platform" / "mod.py").write_text("X = 2\n")
    (repo / "ba2_trade_platform" / "new.py").write_text("Y = 1\n")
    store = ReplayStore(tmp_path / "store")
    p, err = prov.collect_provenance(repo, store.publish_session_object)
    assert err is None
    store.begin_session(SessionRecord(
        session_id="S1", instance_id="i", started_at=NOW, exchange_tz="America/New_York",
        app_version="v", source_revision=_git(repo, "rev-parse", "HEAD"), dirty=True,
        provenance=p))
    out = store.export_session("S1", tmp_path / "bundle")
    store.close()
    return out, p


def _report(status=ReplayStatus.COVERAGE_DIFFERENCE):
    report = ReplayReport(session_id="S1", bundle_dir="b",
                          capability=ReplayStatus.CAPABILITY_RECORDED_EXPERT)
    report.results = [
        AnalysisResult("a1", "E", "AAPL", "enter", "recommendation", status),
        AnalysisResult("a2", "E", "MSFT", "enter", "recommendation", ReplayStatus.COVERAGE_MATCH),
    ]
    return report


# --------------------------------------------------------------------------- drift labelling
def test_differences_become_code_drift_when_code_differs(tmp_path):
    repo = _make_repo(tmp_path)
    clean = prov.running_code(repo)
    (repo / "ba2_trade_platform" / "mod.py").write_text("X = 3\n")
    dirty = prov.running_code(repo)
    report = _report()
    report.stage_results = {"Recommendation": {"a1": "difference", "a2": "match"}}
    report.apply_code_comparison(prov.compare_code(clean, dirty))

    counts = report.counts()
    assert counts[ReplayStatus.COVERAGE_CODE_DRIFT] == 1
    assert counts[ReplayStatus.COVERAGE_DIFFERENCE] == 0
    assert counts[ReplayStatus.COVERAGE_MATCH] == 1          # matches stay matches
    assert report.stage_results["Recommendation"]["a1"] == "code_drift"
    md = report.to_markdown()
    assert "recorded on" in md and "+patch" in md and "CODE DRIFT" in md
    assert report.to_mapping()["code"]["drift"] is True
    assert "code_drift" in STATUS_ORDER


def test_differences_stay_differences_on_identical_code(tmp_path):
    repo = _make_repo(tmp_path)
    same = prov.running_code(repo)
    report = _report()
    report.apply_code_comparison(prov.compare_code(same, same))
    assert report.counts()[ReplayStatus.COVERAGE_DIFFERENCE] == 1
    assert report.counts()[ReplayStatus.COVERAGE_CODE_DRIFT] == 0


def test_an_unrecorded_patch_is_reported_not_guessed_as_drift(tmp_path):
    repo = _make_repo(tmp_path)
    old = SessionRecord(session_id="S", instance_id="i", started_at=NOW, exchange_tz="x",
                        app_version="v", source_revision=_git(repo, "rev-parse", "HEAD"),
                        dirty=True, schema_version=1)
    report = _report()
    report.apply_code_comparison(provenance.compare_with_recording(old, repo))
    assert report.counts()[ReplayStatus.COVERAGE_DIFFERENCE] == 1
    assert "dirty, patch not recorded (pre-schema 2)" in report.code_provenance["line"]


def test_a_real_replay_carries_the_revision_line(tmp_path):
    exported = capture_session(tmp_path / "store", tmp_path / "export")
    bundle = tmp_path / "bundle"
    shutil.copytree(exported, bundle)
    report = expert_replay.run(bundle, tmp_path / "out")
    line = report.code_provenance["line"]
    assert line.startswith("recorded on ") and ", running " in line
    assert not report.code_provenance["drift"]            # recorded on the code that replays it
    written = json.loads((tmp_path / "out" / "recorded_expert.json").read_text())
    assert written["code"]["line"] == line


# --------------------------------------------------------------------------- checkout
def test_checkout_rebuilds_the_recorded_code(tmp_path):
    repo = _make_repo(tmp_path)
    bundle, p = _recorded_bundle(tmp_path, repo)
    _git(repo, "checkout", "--", ".")            # the live tree moves on ...
    (repo / "ba2_trade_platform" / "new.py").unlink()
    (repo / "ba2_trade_platform" / "mod.py").write_text("X = 99\n")
    _git(repo, "commit", "-qam", "later")

    into = tmp_path / "wt"
    provenance.checkout_recorded_code(bundle, into, repo)

    assert (into / "ba2_trade_platform" / "mod.py").read_text() == "X = 2\n"
    assert (into / "ba2_trade_platform" / "new.py").read_text() == "Y = 1\n"
    state = prov.running_code(into)
    assert state.patch_sha256 == p["patch_sha256"]       # same code id as recorded
    comparison = prov.compare_code(prov.recorded_code(load_bundle(bundle).session), state)
    assert not comparison.drift, comparison.reasons


def test_checkout_refuses_a_patch_that_does_not_apply(tmp_path):
    repo = _make_repo(tmp_path)
    bundle, _ = _recorded_bundle(tmp_path, repo)
    # forge an unappliable situation: rewrite the manifest revision to a commit whose mod.py differs
    _git(repo, "checkout", "--", ".")
    (repo / "ba2_trade_platform" / "mod.py").write_text("X = 7\nZ = 1\n")
    _git(repo, "commit", "-qam", "other")
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["session"]["source_revision"] = _git(repo, "rev-parse", "HEAD")
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    into = tmp_path / "wt"
    with pytest.raises(prov.ProvenanceError, match="does not apply"):
        provenance.checkout_recorded_code(bundle, into, repo)
    assert not into.exists()                              # no half-made worktree left behind


def test_checkout_refuses_an_unknown_revision_and_unrecorded_patch(tmp_path):
    repo = _make_repo(tmp_path)
    bundle, _ = _recorded_bundle(tmp_path, repo)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["session"]["source_revision"] = "f" * 40
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(prov.ProvenanceError, match="unknown"):
        provenance.checkout_recorded_code(bundle, tmp_path / "wt", repo)

    manifest["session"]["provenance"] = {}
    manifest["session"]["source_revision"] = _git(repo, "rev-parse", "HEAD")
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(prov.ProvenanceError, match="patch not recorded"):
        provenance.checkout_recorded_code(bundle, tmp_path / "wt", repo)


# --------------------------------------------------------------------------- export / CLI
def _run_cli(*args, cwd):
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=300,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            str(REPO_ROOT / "packages" / n) for n in ("common", "providers", "experts"))})


def test_export_from_a_store_copy_is_read_only_and_complete(tmp_path):
    exported = capture_session(tmp_path / "store", tmp_path / "export")
    store_copy = tmp_path / "copy"
    shutil.copytree(tmp_path / "store", store_copy)
    index = store_copy / "index.sqlite"
    index.chmod(0o444)                                    # a writer lock would not matter either
    result = _run_cli("replay", "export", "--store", str(store_copy), "--session",
                      "S-REPLAY-TEST", "--out", str(tmp_path / "out"), cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert load_bundle(tmp_path / "out").session.session_id == "S-REPLAY-TEST"
    assert len(load_bundle(tmp_path / "out").analyses) == len(load_bundle(exported).analyses)

    missing = _run_cli("replay", "export", "--store", str(store_copy), "--session", "nope",
                       "--out", str(tmp_path / "out2"), cwd=tmp_path)
    assert missing.returncode != 0 and "nope" in missing.stdout + missing.stderr


def test_cli_prints_the_revision_line_and_checkout_runs(tmp_path):
    exported = capture_session(tmp_path / "store", tmp_path / "export")
    bundle = tmp_path / "bundle"
    shutil.copytree(exported, bundle)
    result = _run_cli("replay", "experts", "--bundle", str(bundle), cwd=tmp_path)
    assert "recorded on " in result.stdout and ", running " in result.stdout,         result.stdout + result.stderr

    repo = _make_repo(tmp_path)
    recorded, _ = _recorded_bundle(tmp_path, repo)
    out = _run_cli("replay", "checkout", "--bundle", str(recorded), "--into",
                   str(tmp_path / "wt"), "--repo", str(repo), cwd=tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (tmp_path / "wt" / "ba2_trade_platform" / "new.py").is_file()
