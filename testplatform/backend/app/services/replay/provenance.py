"""Replay-side provenance: compare running code with the recording's, check it out, export it.

* :func:`finish_report` is the ONE tail every replay command shares (experts, gather,
  option-selection, historical): it compares the running code with the recording's, relabels
  differing results ``code_drift`` when the code is provably different, merges coverage and
  writes the report.
* :func:`checkout_recorded_code` builds a git worktree at the recorded revision with the
  recorded patch applied, so a recording can be replayed on its own code.
* :func:`export_session` is the read-only ``replay export`` (safe while the live app writes).
"""
from __future__ import annotations

import base64
import hashlib
import subprocess
from pathlib import Path
from typing import Any, Optional

from ba2_common.core.replay import load_bundle
from ba2_common.core.replay.provenance import (
    CodeComparison,
    ProvenanceError,
    compare_code,
    recorded_code,
    running_code,
)
from ba2_common.core.replay.service import ReplayStore
from ba2_common.core.replay.store import ReplayStoreError
from ba2_common.logger import logger

from app.services.replay.report import ReplayReport, merge_coverage

__all__ = [
    "running_repo",
    "compare_with_recording",
    "finish_report",
    "checkout_recorded_code",
    "export_session",
]


def running_repo() -> Path:
    """The git work tree this very code runs from."""
    here = Path(__file__).resolve().parent
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=str(here), capture_output=True, text=True,
        timeout=30)
    if out.returncode != 0:
        raise ProvenanceError(f"{here} is not inside a git work tree: {out.stderr.strip()}")
    return Path(out.stdout.strip())


def compare_with_recording(session, repo=None) -> CodeComparison:
    return compare_code(recorded_code(session), running_code(repo or running_repo()))


def finish_report(report: ReplayReport, bundle, bundle_dir, out_dir=None, repo=None) -> ReplayReport:
    """Compare code, relabel drift, write coverage back, write the report."""
    comparison = compare_with_recording(bundle.session, repo)
    report.apply_code_comparison(comparison)
    # Printed to the log here (loud even when the caller discards the report); the CLI
    # prints it too, next to the markdown.
    logger.warning(comparison.line) if comparison.drift else logger.info(comparison.line)
    merge_coverage(bundle_dir, report)
    if out_dir is not None:
        report.write(out_dir)
    return report


def _git(repo, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, timeout=120)
    if done.returncode != 0:
        raise ProvenanceError(
            f"git {' '.join(args)} exited {done.returncode}: "
            f"{done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout.decode("utf-8", "replace")


def _decoded_blob(bundle, object_hash: str, expected_sha: str, what: str) -> bytes:
    value = bundle.decode(object_hash)
    data = base64.b64decode(value["data"])
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha:
        raise ProvenanceError(f"{what}: stored content hashes to {actual}, recorded {expected_sha}")
    return data


def checkout_recorded_code(bundle_dir, into, repo=None, *, allow_capped_untracked: bool = False
                           ) -> Path:
    """Create a worktree at ``into`` holding the recorded revision plus the recorded patch.

    Refuses (raising :class:`ProvenanceError`, and removing any half-made worktree) when the
    revision is unknown to ``repo``, the recording carries no patch for a dirty tree, the patch
    does not apply, or an untracked file's content was not stored (unless allowed).
    """
    bundle = load_bundle(bundle_dir)
    session = bundle.session
    repo = Path(repo) if repo else running_repo()
    into = Path(into)
    prov = session.provenance
    if session.provenance_error:
        raise ProvenanceError(
            f"session {session.session_id} has provenance_error: {session.provenance_error}")
    revision = session.source_revision
    if not revision:
        raise ProvenanceError(f"session {session.session_id} recorded no source revision")
    if session.dirty and "patch_sha256" not in prov:
        raise ProvenanceError(
            f"session {session.session_id}: provenance: dirty, patch not recorded "
            f"(pre-schema 2); the recorded code cannot be reconstructed")
    try:
        _git(repo, "cat-file", "-e", f"{revision}^{{commit}}")
    except ProvenanceError as exc:
        raise ProvenanceError(
            f"recorded revision {revision} is unknown to {repo} (fetch it first): {exc}") from exc
    if into.exists() and any(into.iterdir()):
        raise ProvenanceError(f"{into} exists and is not empty")

    patch = None
    if prov.get("patch_object"):
        patch = _decoded_blob(bundle, prov["patch_object"], prov["patch_sha256"], "patch")
    elif prov.get("patch_sha256"):
        raise ProvenanceError("the patch was recorded by hash only (over the size cap)")
    untracked = []
    for entry in prov.get("untracked", ()):
        if not entry.get("content_object"):
            if allow_capped_untracked:
                logger.warning(f"untracked {entry['path']} not stored (capped); NOT restored")
                continue
            raise ProvenanceError(
                f"untracked file {entry['path']} ({entry['size']} bytes) was not stored "
                f"(content capped); pass --allow-capped-untracked to check out without it")
        untracked.append((entry["path"], _decoded_blob(
            bundle, entry["content_object"], entry["sha256"], entry["path"])))

    _git(repo, "worktree", "add", "--detach", str(into), revision)
    try:
        if patch:
            patch_file = into.parent / f".{into.name}.recorded.patch"
            patch_file.write_bytes(patch)
            try:
                _git(into, "apply", "--binary", "--whitespace=nowarn", str(patch_file))
            finally:
                patch_file.unlink(missing_ok=True)
        for rel, data in untracked:
            target = into / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    except Exception as exc:
        try:
            _git(repo, "worktree", "remove", "--force", str(into))
        except Exception as cleanup:
            logger.error(f"could not remove the half-made worktree {into}: {cleanup}")
        raise ProvenanceError(
            f"the recorded patch does not apply on {revision[:8]}: {exc}") from exc
    return into


def export_session(store_dir, session_id: str, out_dir) -> Path:
    """Export one session from a store directory, READ-ONLY (safe beside the live writer)."""
    store = ReplayStore(store_dir, read_only=True)
    try:
        known = [s.session_id for s in store.index.list_sessions()]
        if session_id not in known:
            raise ReplayStoreError(
                f"session {session_id!r} is not in {store_dir}; sessions: {known}")
        return store.export_session(session_id, out_dir)
    finally:
        store.index.close()
