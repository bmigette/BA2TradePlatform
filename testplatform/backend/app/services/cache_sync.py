"""Cache mirroring primitives — the "avoid redownload" core for remote workers.

Almost everything under ``CACHE_FOLDER`` is immutable provider history (OHLCV parquet,
fmp_history, the options/screener sqlite caches), so it's safe to sync ONE-WAY master -> worker:
a worker mirrors the master's cache and then runs the hermetic backtest with zero provider/network
calls (the backtest contract raises ``BacktestCacheMiss`` rather than fetching). The screener
metric_store is the one exception: it DOES get rebuilt/compacted in place (e.g. replacing many
``part-NNNNN.parquet`` fragments with one ``part.parquet`` per month), so a worker's copy can go
stale even though nothing looks "missing" — see ``diff_stale``/``prune_paths``. The per-host
DERIVED array caches (``_derived/``, ``ba2_common.core.shared_arrays``) are the second exception,
in the opposite direction: they are never synced EITHER way (see ``_SKIP_DIRNAMES``).

PUSH model: the MASTER builds the list of files a worker is missing (``diff_missing`` against the
worker's manifest) and streams them as ONE tar (``iter_tar``); the WORKER extracts that stream
(``extract_tar``). ``build_manifest`` / ``safe_resolve`` are used on both ends. Dedup is by
``(rel_path, size)`` — immutable history means a size match is an identity match, so re-pushes
only send genuinely new files. ``diff_stale``/``prune_paths`` are the reverse direction: a rel_path
the worker has that the master's CURRENT manifest no longer lists is a leftover from before a
rebuild — a partition-globbing reader (e.g. the screener metric_store's ``load_store``) would
otherwise keep ingesting it alongside the fresh file, silently corrupting that worker's results.
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import threading
import time
from pathlib import Path
from stat import S_ISREG
from typing import Callable, Iterable, Iterator, List, Optional

from ba2_common.config import CACHE_FOLDER
from ba2_common.core.db_maintenance import format_bytes
from ba2_common.core.shared_arrays import DERIVED_DIRNAME

logger = logging.getLogger(__name__)

# How often extract_tar logs progress, in seconds. Time-based (not per-file or per-N-bytes) so
# it scales the same way whether the tar is a thousand small parquet files or one huge sqlite --
# either way an operator sees a heartbeat at a predictable cadence instead of either silence or
# log spam.
_PROGRESS_LOG_INTERVAL_S = 15.0

# Transient / in-use sidecar files that must never be synced (they are machine-local and would
# corrupt a fresh sqlite open on the worker). The main ``.sqlite`` IS synced; the worker opens
# it cleanly and re-derives any -wal/-shm.
_SKIP_SUFFIXES = (".tmp", ".part", ".lock", "-wal", "-shm", ".journal")

# Per-host DERIVED caches (memory-mapped .npy sets built from parquet already on the worker --
# see ba2_common.core.shared_arrays). Deterministic from the source and ~3x its size, so each
# host builds its own on first touch instead of pulling it over the wire. Excluded from the
# manifest on BOTH ends, which also keeps diff_stale/prune_paths from mistaking a worker's own
# derived cache for a stale leftover of a master rebuild.
_SKIP_DIRNAMES = (DERIVED_DIRNAME,)


def cache_root(root: Optional[str] = None) -> Path:
    return Path(root or CACHE_FOLDER)


def _is_syncable(p: Path) -> bool:
    """Whether *p* (a path RELATIVE to the cache root) may be synced.

    Relative on purpose: the ``_SKIP_DIRNAMES`` component match would otherwise fire on a
    directory in the cache root's own absolute prefix and silently empty the whole manifest.
    Only an exact path COMPONENT matches, so a file literally named ``_derived`` or a sibling
    directory like ``_derived_something`` stays ordinary cache content.
    """
    if any(part in _SKIP_DIRNAMES for part in p.parts[:-1]):
        return False
    name = p.name
    if name.startswith("."):
        return False
    return not any(name.endswith(s) for s in _SKIP_SUFFIXES)


def _crc32_file(p: Path, chunk: int = 1 << 20) -> int:
    import zlib
    crc = 0
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            crc = zlib.crc32(block, crc)
    return crc


def build_manifest(root: Optional[str] = None, with_hash: bool = False) -> dict:
    """Enumerate every syncable cache file under *root* (default ``CACHE_FOLDER``).

    Returns ``{root, count, total_bytes, files:[{rel_path, size, mtime[, crc32]}]}`` with
    POSIX-style relative paths (stable across OSes). Recurses the whole cache tree so newly-added
    buckets are covered automatically (no allowlist to drift) — with one DENYlist: any directory
    named ``_derived`` (``_SKIP_DIRNAMES``) is not even descended into, since a per-host derived
    array cache is rebuilt locally rather than synced.

    ``with_hash=True`` adds a ``crc32`` per file (read LOCALLY, so no extra network transfer —
    only the checksum crosses the wire). CRC32 (not sha256): this is corruption/staleness
    detection over a possibly-tens-of-GB cache, not a security boundary, so the much faster
    non-cryptographic checksum is the right tradeoff. Off by default: the hot ``push_cache`` path
    only needs ``(rel_path, size)`` and reading every file is disk-IO-heavy; use it for the
    periodic cache-integrity check, which needs to catch a same-size-different-content drift that
    (rel_path, size) alone can't see (e.g. a rebuild that rewrites a file at its old byte size).
    """
    base = cache_root(root)
    files: List[dict] = []
    if base.is_dir():
        # os.walk (not rglob) so a skipped directory is PRUNED from the descent: a derived cache
        # holds several .npy per signature dir over the whole provider tree, and this walk is the
        # one with history (a cold manifest over 312k files took ~140s and got a worker excluded).
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRNAMES]
            rel_dir = Path(dirpath).relative_to(base)
            for fn in filenames:
                rel = rel_dir / fn
                if not _is_syncable(rel):
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not S_ISREG(st.st_mode):  # dir symlink/junction listed as a name: not a file
                    continue
                entry = {
                    "rel_path": rel.as_posix(),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
                if with_hash:
                    try:
                        entry["crc32"] = _crc32_file(p)
                    except OSError:
                        continue
                files.append(entry)
    return {
        "root": str(base),
        "count": len(files),
        "total_bytes": sum(f["size"] for f in files),
        "files": files,
    }


def safe_resolve(rel_path: str, root: Optional[str] = None) -> Path:
    """Resolve *rel_path* under *root*, rejecting any path that escapes it (traversal guard).

    Absolute paths and ``../`` escapes both resolve outside *root* and are rejected.
    """
    base = cache_root(root).resolve()
    target = (base / rel_path).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"path escapes cache root: {rel_path!r}")
    return target


# --------------------------------------------------------------------------------------------
# Push primitives (tar stream)
# --------------------------------------------------------------------------------------------
def diff_missing(local_files: List[dict], remote_manifest: dict) -> List[str]:
    """Return the rel_paths present in *local_files* that the remote is missing or has at a
    different size (immutable history ⇒ size match = identity match). *local_files* and
    ``remote_manifest['files']`` are manifest entries (``{rel_path, size, ...}``)."""
    remote = {f["rel_path"]: f["size"] for f in remote_manifest.get("files", [])}
    return [f["rel_path"] for f in local_files if remote.get(f["rel_path"]) != f["size"]]


def diff_stale(local_files: List[dict], remote_manifest: dict) -> List[str]:
    """Return the rel_paths the remote has that *local_files* no longer has at all.

    The mirror of ``diff_missing``: a worker file with no matching rel_path on the master is a
    LEFTOVER from before a local rebuild/compaction (e.g. the screener metric_store replacing many
    ``part-NNNNN.parquet`` fragments with one ``part.parquet`` per month) — ``push_cache`` alone
    never removes it, so a partition-globbing reader on the worker keeps ingesting the stale
    fragment alongside the fresh file, corrupting that worker's results silently."""
    local = {f["rel_path"] for f in local_files}
    return [f["rel_path"] for f in remote_manifest.get("files", []) if f["rel_path"] not in local]


def diff_content_mismatch(local_files: List[dict], remote_manifest: dict) -> List[str]:
    """Return rel_paths that match on (rel_path, size) but have a DIFFERENT ``crc32`` — the one
    drift ``diff_missing``/``diff_stale`` structurally cannot see (a rebuild that rewrites a file
    at its old byte size). Both manifests must have been built with ``with_hash=True``; entries
    missing a ``crc32`` are skipped (can't compare)."""
    remote = {f["rel_path"]: f.get("crc32") for f in remote_manifest.get("files", [])}
    out = []
    for f in local_files:
        rh = remote.get(f["rel_path"])
        lh = f.get("crc32")
        if rh is not None and lh is not None and rh != lh:
            out.append(f["rel_path"])
    return out


def iter_tar(rel_paths: Iterable[str], root: Optional[str] = None,
             chunk: int = 1 << 20) -> Iterator[bytes]:
    """Yield a single uncompressed tar STREAM of *rel_paths* (resolved under *root*).

    Truly streaming: a background thread writes the tar to an OS pipe while this generator reads
    + yields from the other end, so an arbitrarily large file (e.g. the options sqlite) never
    buffers fully in memory. Each member is stored under its rel_path (traversal-guarded via
    ``safe_resolve``). Cache parquet is already compressed, so the tar is uncompressed for speed.
    """
    base = cache_root(root)
    paths = [p for p in rel_paths]
    r_fd, w_fd = os.pipe()

    def _build() -> None:
        try:
            with os.fdopen(w_fd, "wb") as wf:
                with tarfile.open(fileobj=wf, mode="w|") as tar:
                    for rel in paths:
                        try:
                            p = safe_resolve(rel, str(base))
                        except ValueError:
                            continue
                        if p.is_file():
                            tar.add(str(p), arcname=rel, recursive=False)
        except (OSError, BrokenPipeError):
            pass  # reader went away; nothing more to do

    t = threading.Thread(target=_build, daemon=True, name="cache-tar-build")
    t.start()
    try:
        with os.fdopen(r_fd, "rb") as rf:
            while True:
                data = rf.read(chunk)
                if not data:
                    break
                yield data
    finally:
        # Always join, even if the consumer abandons the generator early or raises (the build
        # thread sees BrokenPipe when the read end closes, so this won't hang).
        t.join()


def extract_tar(fileobj, dest: Optional[str] = None,
                log: Callable[[str], None] = logger.info) -> dict:
    """Extract a tar STREAM (*fileobj*, a binary readable) into *dest* (default ``CACHE_FOLDER``).

    Streaming read (``mode='r|'``). Every member path is traversal-guarded via ``safe_resolve``
    (a malicious ``../`` member is skipped, not written outside the cache). Atomic temp+rename per
    file. Returns ``{extracted, bytes, skipped}``.

    Progress: a push can run into the tens of GB (module docstring) and take a while on a slow
    disk, so *log* is called at most every ``_PROGRESS_LOG_INTERVAL_S`` seconds while extracting
    -- an operator tailing this worker's log then sees a heartbeat instead of a long silent gap
    that looks indistinguishable from a hang.
    """
    dest_root = cache_root(dest)
    dest_root.mkdir(parents=True, exist_ok=True)
    extracted = 0
    total = 0
    skipped = 0
    last_log = time.monotonic()
    with tarfile.open(fileobj=fileobj, mode="r|") as tar:
        for member in tar:
            if not member.isfile():
                continue
            try:
                target = safe_resolve(member.name, str(dest_root))
            except ValueError:
                skipped += 1
                continue
            src = tar.extractfile(member)
            if src is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".part")
            try:
                with open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                os.replace(tmp, target)
            except BaseException:
                tmp.unlink(missing_ok=True)  # never orphan a .part on disk-full / I/O error
                raise
            extracted += 1
            total += member.size
            now = time.monotonic()
            if now - last_log >= _PROGRESS_LOG_INTERVAL_S:
                log(f"cache extract: {extracted} file(s), {format_bytes(total)} so far "
                    f"(last: {member.name})")
                last_log = now
    return {"extracted": extracted, "bytes": total, "skipped": skipped}


def prune_paths(rel_paths: Iterable[str], root: Optional[str] = None) -> dict:
    """Delete *rel_paths* under *root* (default ``CACHE_FOLDER``). Traversal-guarded via
    ``safe_resolve``; a path outside the root is skipped, not deleted. Missing files are a
    no-op (already gone). Returns ``{pruned, skipped, failed}``.

    One undeletable path never aborts the sweep: on Windows a file another process still has
    open/memory-mapped refuses to unlink (WinError 32 -> PermissionError), and letting that
    escape mid-loop would leave every genuinely-stale file AFTER it un-pruned — exactly the
    silent-corruption case ``diff_stale`` exists to prevent. Each failure is counted and logged.
    """
    base = str(cache_root(root))
    pruned = 0
    skipped = 0
    failed = 0
    for rel in rel_paths:
        try:
            target = safe_resolve(rel, base)
        except ValueError:
            skipped += 1
            continue
        try:
            target.unlink()
            pruned += 1
        except FileNotFoundError:
            pass
        except OSError as e:  # PermissionError (locked/mapped file) and friends
            failed += 1
            logger.warning(f"cache prune: could not delete {rel}: {e}")
    return {"pruned": pruned, "skipped": skipped, "failed": failed}
