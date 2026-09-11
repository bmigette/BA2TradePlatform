"""Pin a comparison's artifacts into an isolated root (spec section 6).

"Historical artifacts are versioned/pinned for a comparison run. A mutable latest
cache may be materialized for existing readers in an isolated folder; it is derived
from explicitly selected versions."

:func:`materialize_pinned_root` is that materialization. Given a
:class:`~app.services.warm.planner.WarmPlan` (the explicit selection) it copies each
resolved artifact into ``dest`` under the SAME relative path a normal cache root
uses, so an ordinary reader -- a subprocess with ``CACHE_FOLDER`` pointed at the pin
-- works unchanged, and writes ``manifest.json`` beside it.

**The manifest is the point.** Two facts about every file, neither recoverable
afterwards:

* its sha256, so a later run can prove the pin has not drifted
  (:func:`verify_pinned_root`);
* its PROVENANCE. ``warmed`` means this warm run downloaded it, and it is therefore
  today's revision of the data, NOT the vintage live consumed.
  ``legacy_history_unknown_revision`` means it was already on the root before
  anyone recorded revisions. A historical comparison must report both as what they
  are; presenting either as a reconstructed vintage is the exact failure spec
  section 5 warns about.

Source roots are opened read-only. The destination is refused if it lives inside
one of them: pinning must never be able to mutate the evidence it pins.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from typing import Any, Collection, Dict, List, Sequence

from ba2_common.logger import logger

#: The manifest file written into every pinned root.
MANIFEST_NAME = "manifest.json"
#: Manifest format version.
MANIFEST_VERSION = 1

#: This warm run fetched the file; it is today's revision of the payload.
PROVENANCE_WARMED = "warmed"
#: The file predates this run and nothing recorded which revision it holds.
PROVENANCE_LEGACY = "legacy_history_unknown_revision"

#: Bytes per read while hashing. Statement and price histories run to megabytes;
#: reading them whole to hash them is avoidable memory for nothing.
_HASH_CHUNK = 1024 * 1024


class PinnedRootError(RuntimeError):
    """The pin cannot be built as asked (a destination inside a source root, ...)."""


def sha256_file(path: str) -> str:
    """The sha256 of a file's bytes, streamed."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize_pinned_root(plan, source_roots: Sequence[str], dest: str, *,
                            warmed: Collection[str] = ()) -> Dict[str, Any]:
    """Copy ``plan``'s resolved artifacts into ``dest`` and write its manifest.

    ``warmed`` holds the :attr:`~ba2_common.core.replay.dependencies.Requirement.key`
    of every requirement THIS run fetched; those files are stamped
    ``warmed`` and everything else ``legacy_history_unknown_revision``.

    Returns the manifest. Running it again over an unchanged plan reproduces the same
    FILES -- same bytes, same hashes, same provenance -- which is what makes a
    comparison repeatable. The manifest itself is not byte-identical between runs: it
    carries its own ``created_at``, deliberately, because when a pin was taken is part
    of the evidence.
    """
    dest_path = os.path.abspath(dest)
    for root in source_roots:
        root_path = os.path.abspath(root)
        if dest_path == root_path:
            raise PinnedRootError(
                f"the pinned root and the source root are the same directory ({root_path}); "
                f"pinning must never write into the cache it is preserving")

    # A SUB-directory of a cache root is allowed and is in fact where the live host pins
    # (``<cache>/replay/pinned/<date>``): the pin's own subtree holds no artifact any
    # reader resolves -- the readers look under ``fmp_history/``, ``fred/`` and the
    # per-provider parquet directories, never under ``replay/``. What must never happen
    # is a file being copied onto ITSELF, which is checked per file below.
    warmed_keys = set(warmed)
    os.makedirs(dest_path, exist_ok=True)
    files: Dict[str, Dict[str, Any]] = {}
    unpinned: List[str] = []
    hardlinked = 0

    for entry in plan.entries:
        key = entry.requirement.key
        if not entry.path or not os.path.exists(entry.path):
            # Not an error: a requirement can legitimately have no artifact (a
            # checked-empty namespace has one, platform state never does, a gap the
            # budget could not close does not). The comparison is told rather than
            # discovering it as a cache miss mid-run.
            if entry.status not in ("local_state", "unsupported"):
                unpinned.append(key)
            continue
        source_root = entry.source_root or _containing_root(entry.path, source_roots)
        relative = os.path.relpath(entry.path, source_root).replace(os.sep, "/")
        target = os.path.join(dest_path, *relative.split("/"))
        if os.path.abspath(target) == os.path.abspath(entry.path):
            raise PinnedRootError(
                f"pinning {relative} would copy it onto itself ({target}); the destination "
                f"overlaps the source layout and would destroy the artifact it preserves")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if _place(entry.path, target):
            hardlinked += 1
        files[relative] = {
            "sha256": sha256_file(target),
            "source": source_root,
            "mtime": datetime.fromtimestamp(
                os.path.getmtime(entry.path), timezone.utc).isoformat(),
            "size_bytes": os.path.getsize(target),
            "provenance": PROVENANCE_WARMED if key in warmed_keys else PROVENANCE_LEGACY,
            "requirement": key,
        }

    manifest = {
        "version": MANIFEST_VERSION,
        "root": dest_path,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_roots": [os.path.abspath(r) for r in source_roots],
        "plan_created_at": plan.created_at,
        "files": files,
        "unpinned": unpinned,
        "hardlinked": hardlinked,
    }
    tmp = os.path.join(dest_path, f"{MANIFEST_NAME}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    os.replace(tmp, os.path.join(dest_path, MANIFEST_NAME))
    logger.info(
        f"pinned {len(files)} artifact(s) into {dest_path} "
        f"({hardlinked} hardlinked, {len(unpinned)} requirement(s) with no artifact)")
    return manifest


def _containing_root(path: str, source_roots: Sequence[str]) -> str:
    """Which source root a path lies under. Raises rather than guessing a prefix."""
    absolute = os.path.abspath(path)
    for root in source_roots:
        root_path = os.path.abspath(root)
        if absolute.startswith(root_path + os.sep):
            return root_path
    raise PinnedRootError(f"{path} is not inside any of the source roots {list(source_roots)}")


def _place(source: str, target: str) -> bool:
    """Hardlink ``source`` to ``target`` where the filesystem allows it, else copy.

    Returns True when a hardlink was made. A hardlink is the cheap path for a
    multi-gigabyte pin and is safe here because every writer of these artifacts
    replaces the file atomically -- verified, not assumed:
    ``fmp_common._fmp_history_disk_read_or_fetch`` (tmp + ``os.replace``),
    ``native_cache.write_timeseries`` (temp + rename) and
    ``fred_series.refresh_series`` (tmp + ``os.replace``). A replace breaks the link
    instead of editing the pinned inode, so a later warm of the SOURCE root cannot
    reach through into the pin. Any failure --
    cross-device, a filesystem without links, Windows without the privilege -- falls
    back to a real copy, which is always correct and only slower.
    """
    if os.path.exists(target):
        os.remove(target)
    try:
        os.link(source, target)
        return True
    except OSError:
        shutil.copy2(source, target)
        return False


def verify_pinned_root(root: str) -> List[str]:
    """Relative paths whose bytes no longer match the manifest (empty = intact).

    A missing file counts as a mismatch: the pin promised it.
    """
    manifest_path = os.path.join(root, MANIFEST_NAME)
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    if int(manifest["version"]) != MANIFEST_VERSION:
        raise PinnedRootError(
            f"pinned root {root} carries manifest version {manifest['version']}, this build "
            f"reads {MANIFEST_VERSION}")
    drifted: List[str] = []
    for relative, entry in sorted(manifest["files"].items()):
        path = os.path.join(root, *relative.split("/"))
        if not os.path.exists(path) or sha256_file(path) != entry["sha256"]:
            drifted.append(relative)
    return drifted


__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "PROVENANCE_LEGACY",
    "PROVENANCE_WARMED",
    "PinnedRootError",
    "materialize_pinned_root",
    "sha256_file",
    "verify_pinned_root",
]
