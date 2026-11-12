"""Which code a replay session ran, exactly -- and whether the code replaying it is the same.

A live clone always carries uncommitted edits, so ``source_revision`` alone cannot
reproduce a recording: replaying it on another tree produces differences that look
like real parity breaks but are code drift (audit gap #6). So for a dirty tree the
session records the EXACT state:

* the tracked patch (``git diff HEAD --binary``) as a content-addressed object, plus its sha256;
* the untracked ``.py`` files under the code directories (path, sha256, size, and the content
  when small -- ``content_capped`` says so when it is not stored);
* the installed versions (app, test app, shared packages).

Collection runs once per process start, never in the decision path. A failure is returned as a
string for ``SessionRecord.provenance_error``; the caller records it -- it is never dropped.

The same :func:`snapshot_code` runs on the REPLAY side, so "running" and "recorded" are
computed by one function, and :func:`compare_code` decides drift.
"""
from __future__ import annotations

import base64
import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "CODE_DIRS",
    "CodeSnapshot",
    "CodeComparison",
    "ProvenanceError",
    "snapshot_code",
    "collect_provenance",
    "read_versions",
    "recorded_code",
    "running_code",
    "compare_code",
    "revision_line",
]

#: Directories whose untracked ``.py`` files count as code the process may have imported.
CODE_DIRS: Tuple[str, ...] = ("ba2_trade_platform", "packages", "testplatform", "tools")

#: A patch above this is refused (its sha256 is still recorded, with the error).
MAX_PATCH_BYTES = 32 * 1024 * 1024
#: Untracked file content is stored per file up to this size, and in total up to the next one.
MAX_UNTRACKED_FILE_BYTES = 256 * 1024
MAX_UNTRACKED_TOTAL_BYTES = 4 * 1024 * 1024

_GIT_TIMEOUT = 60
_SHORT = 8


class ProvenanceError(RuntimeError):
    """The code state could not be determined."""


def _git(repo, *args: str) -> bytes:
    try:
        done = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, timeout=_GIT_TIMEOUT
        )
    except Exception as exc:
        raise ProvenanceError(f"git {' '.join(args)} failed to run: {exc}") from exc
    if done.returncode != 0:
        raise ProvenanceError(
            f"git {' '.join(args)} exited {done.returncode}: "
            f"{done.stderr.decode('utf-8', 'replace').strip()}"
        )
    return done.stdout


def _is_code_file(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        name.endswith(".py")
        and "tests" not in parts[:-1]
        and not name.startswith("test_")
        and "__pycache__" not in parts
    )


@dataclass(frozen=True)
class CodeSnapshot:
    """The state of a git tree: HEAD, the tracked patch, the untracked code files."""

    revision: str
    dirty: bool
    patch: bytes
    patch_sha256: Optional[str]
    #: ``{"path", "sha256", "size"}`` per untracked code file, sorted by path.
    untracked: Tuple[Mapping[str, Any], ...]
    #: path -> bytes, for the files small enough to keep.
    contents: Mapping[str, bytes] = field(default_factory=dict)


def snapshot_code(repo) -> CodeSnapshot:
    """The exact code state of ``repo`` (raises :class:`ProvenanceError` when git cannot say)."""
    repo = Path(repo)
    revision = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    if not revision:
        raise ProvenanceError("git rev-parse HEAD returned nothing")
    status = _git(repo, "status", "--porcelain")
    patch = _git(repo, "diff", "HEAD", "--binary")
    listed = _git(repo, "ls-files", "--others", "--exclude-standard", "-z", "--", *CODE_DIRS)
    untracked: List[Dict[str, Any]] = []
    contents: Dict[str, bytes] = {}
    total = 0
    for raw in sorted(p for p in listed.decode("utf-8", "replace").split("\0") if p):
        if not _is_code_file(raw):
            continue
        data = (repo / raw).read_bytes()
        entry: Dict[str, Any] = {
            "path": raw, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
        }
        if len(data) <= MAX_UNTRACKED_FILE_BYTES and total + len(data) <= MAX_UNTRACKED_TOTAL_BYTES:
            contents[raw] = data
            total += len(data)
        untracked.append(entry)
    return CodeSnapshot(
        revision=revision,
        dirty=bool(status.strip()),
        patch=patch,
        patch_sha256=hashlib.sha256(patch).hexdigest() if patch else None,
        untracked=tuple(untracked),
        contents=contents,
    )


_VERSION_RE = r'^\s*{name}\s*(?::\s*\w+\s*)?=\s*["\']([^"\']+)["\']'


def _file_version(path: Path, name: str) -> Optional[str]:
    if not path.is_file():
        return None
    match = re.search(_VERSION_RE.format(name=name), path.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else None


def read_versions(repo) -> Dict[str, Optional[str]]:
    """APP_VERSION / TEST_APP_VERSION / the three package versions, from the tree's files.

    Read from the files (not imports) so the answer is the tree's own and an import failure
    cannot hide a version. ``None`` means the tree has no such file.
    """
    repo = Path(repo)
    return {
        "app_version": _file_version(repo / "ba2_trade_platform" / "version.py", "APP_VERSION"),
        "test_app_version": _file_version(repo / "testplatform" / "version.py", "TEST_APP_VERSION"),
        "ba2_common": _file_version(
            repo / "packages" / "common" / "ba2_common" / "version.py", "PACKAGE_VERSION"),
        "ba2_providers": _file_version(
            repo / "packages" / "providers" / "ba2_providers" / "version.py", "PACKAGE_VERSION"),
        "ba2_experts": _file_version(
            repo / "packages" / "experts" / "ba2_experts" / "version.py", "PACKAGE_VERSION"),
    }


def collect_provenance(
    repo, publish: Callable[[Any], str]
) -> Tuple[Dict[str, Any], Optional[str]]:
    """``(provenance, error)`` for a session. ``publish(value) -> hash`` stores an object.

    Never raises: a failure comes back as ``error`` text (and whatever was collected so far),
    to be stored as ``provenance_error``.
    """
    provenance: Dict[str, Any] = {}
    try:
        provenance["versions"] = read_versions(repo)
    except Exception as exc:
        return provenance, f"versions: {exc}"
    try:
        snap = snapshot_code(repo)
    except Exception as exc:
        return provenance, f"code snapshot: {exc}"
    try:
        provenance["patch_sha256"] = snap.patch_sha256
        provenance["patch_bytes"] = len(snap.patch)
        provenance["patch_object"] = None
        if snap.patch:
            if len(snap.patch) > MAX_PATCH_BYTES:
                return provenance, (
                    f"patch is {len(snap.patch)} bytes, over the {MAX_PATCH_BYTES} cap; "
                    f"its sha256 is recorded but the patch is not stored"
                )
            provenance["patch_object"] = publish({
                "kind": "git_patch",
                "sha256": snap.patch_sha256,
                "encoding": "base64",
                "data": base64.b64encode(snap.patch).decode("ascii"),
            })
        entries: List[Dict[str, Any]] = []
        for entry in snap.untracked:
            entry = dict(entry)
            data = snap.contents.get(entry["path"])
            if data is None:
                entry["content_object"] = None
                entry["content_capped"] = True
            else:
                entry["content_object"] = publish({
                    "kind": "untracked_file", "path": entry["path"],
                    "sha256": entry["sha256"], "encoding": "base64",
                    "data": base64.b64encode(data).decode("ascii"),
                })
                entry["content_capped"] = False
            entries.append(entry)
        provenance["untracked"] = entries
        provenance["untracked_scope"] = list(CODE_DIRS)
        provenance["untracked_content_caps"] = {
            "per_file_bytes": MAX_UNTRACKED_FILE_BYTES,
            "total_bytes": MAX_UNTRACKED_TOTAL_BYTES,
        }
    except Exception as exc:
        return provenance, f"storing provenance objects: {exc}"
    return provenance, None


# --------------------------------------------------------------------------- comparison


@dataclass(frozen=True)
class CodeState:
    """One side of a comparison (the recording, or the running tree)."""

    revision: Optional[str]
    dirty: bool
    patch_sha256: Optional[str]
    untracked: Mapping[str, str]          # path -> sha256
    versions: Mapping[str, Optional[str]]
    #: Why this side cannot be fully known ("" when it can).
    unknown: str = ""


def recorded_code(session) -> CodeState:
    """The recording's state from a ``SessionRecord`` (schema 1 sessions included)."""
    prov = session.provenance
    versions: Dict[str, Optional[str]] = {"app_version": session.app_version}
    versions.update(session.package_versions)
    versions.update(prov.get("versions") or {})
    unknown = ""
    if session.provenance_error:
        unknown = f"provenance_error: {session.provenance_error}"
    elif session.dirty and "patch_sha256" not in prov:
        unknown = "provenance: dirty, patch not recorded (pre-schema 2)"
    elif session.source_revision is None:
        unknown = "provenance: source revision not recorded"
    return CodeState(
        revision=session.source_revision,
        dirty=session.dirty,
        patch_sha256=prov.get("patch_sha256"),
        untracked={e["path"]: e["sha256"] for e in prov.get("untracked", ())},
        versions=versions,
        unknown=unknown,
    )


def running_code(repo) -> CodeState:
    """The state of the tree that is replaying (same computation as at capture)."""
    try:
        snap = snapshot_code(repo)
    except ProvenanceError as exc:
        return CodeState(None, False, None, {}, read_versions(repo), unknown=f"running: {exc}")
    return CodeState(
        revision=snap.revision,
        dirty=snap.dirty,
        patch_sha256=snap.patch_sha256,
        untracked={e["path"]: e["sha256"] for e in snap.untracked},
        versions=read_versions(repo),
    )


def _label(state: CodeState) -> str:
    text = (state.revision or "unknown")[:_SHORT]
    if state.patch_sha256:
        text += f"+patch {state.patch_sha256[:_SHORT]}"
    if state.untracked:
        text += f"+{len(state.untracked)} untracked"
    return text


def revision_line(recorded: CodeState, running: CodeState) -> str:
    return f"recorded on {_label(recorded)}, running {_label(running)}"


@dataclass(frozen=True)
class CodeComparison:
    recorded: CodeState
    running: CodeState
    #: True only when the two are PROVABLY different code.
    drift: bool
    reasons: Tuple[str, ...]
    #: Set when one side cannot be known: the line says so; ``drift`` stays False.
    unknown: str
    line: str

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "line": self.line, "drift": self.drift, "reasons": list(self.reasons),
            "unknown": self.unknown,
            "recorded": _state_mapping(self.recorded), "running": _state_mapping(self.running),
        }


def _state_mapping(state: CodeState) -> Dict[str, Any]:
    return {
        "revision": state.revision, "dirty": state.dirty, "patch_sha256": state.patch_sha256,
        "untracked": dict(state.untracked), "versions": dict(state.versions),
    }


def compare_code(recorded: CodeState, running: CodeState) -> CodeComparison:
    """Drift is declared only on evidence; an unknowable side is reported, not guessed."""
    reasons: List[str] = []
    unknown = "; ".join(u for u in (recorded.unknown, running.unknown) if u)
    if recorded.revision and running.revision and recorded.revision != running.revision:
        reasons.append("revision differs")
    # A patch comparison needs both patches to be known.
    if not recorded.unknown and not running.unknown:
        if recorded.patch_sha256 != running.patch_sha256:
            reasons.append("uncommitted patch differs")
        if dict(recorded.untracked) != dict(running.untracked):
            reasons.append("untracked code files differ")
    for key, value in recorded.versions.items():
        other = running.versions.get(key)
        if value and other and value != other:
            reasons.append(f"{key} {value} != {other}")
    line = revision_line(recorded, running)
    if unknown:
        line += f" [{unknown}]"
    return CodeComparison(recorded, running, bool(reasons), tuple(reasons), unknown, line)
