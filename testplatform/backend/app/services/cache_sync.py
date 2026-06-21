"""Cache mirroring primitives — the "avoid redownload" core for remote workers.

Everything under ``CACHE_FOLDER`` is immutable provider history (OHLCV parquet, fmp_history,
the screener metric_store + fundamentals, the options/screener sqlite caches). It is therefore
safe to sync ONE-WAY master -> worker: a worker mirrors the master's cache and then runs the
hermetic backtest with zero provider/network calls (the backtest contract raises
``BacktestCacheMiss`` rather than fetching).

PUSH model: the MASTER builds the list of files a worker is missing (``diff_missing`` against the
worker's manifest) and streams them as ONE tar (``iter_tar``); the WORKER extracts that stream
(``extract_tar``). ``build_manifest`` / ``safe_resolve`` are used on both ends. Dedup is by
``(rel_path, size)`` — immutable history means a size match is an identity match, so re-pushes
only send genuinely new files.
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import threading
from pathlib import Path
from typing import Iterable, Iterator, List, Optional

from ba2_common.config import CACHE_FOLDER

logger = logging.getLogger(__name__)

# Transient / in-use sidecar files that must never be synced (they are machine-local and would
# corrupt a fresh sqlite open on the worker). The main ``.sqlite`` IS synced; the worker opens
# it cleanly and re-derives any -wal/-shm.
_SKIP_SUFFIXES = (".tmp", ".part", ".lock", "-wal", "-shm", ".journal")


def cache_root(root: Optional[str] = None) -> Path:
    return Path(root or CACHE_FOLDER)


def _is_syncable(p: Path) -> bool:
    name = p.name
    if name.startswith("."):
        return False
    return not any(name.endswith(s) for s in _SKIP_SUFFIXES)


def build_manifest(root: Optional[str] = None) -> dict:
    """Enumerate every syncable cache file under *root* (default ``CACHE_FOLDER``).

    Returns ``{root, count, total_bytes, files:[{rel_path, size, mtime}]}`` with POSIX-style
    relative paths (stable across OSes). Recurses the whole cache tree so newly-added buckets
    are covered automatically (no allowlist to drift).
    """
    base = cache_root(root)
    files: List[dict] = []
    if base.is_dir():
        for p in base.rglob("*"):
            if not p.is_file() or not _is_syncable(p):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({
                "rel_path": p.relative_to(base).as_posix(),
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
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


def extract_tar(fileobj, dest: Optional[str] = None) -> dict:
    """Extract a tar STREAM (*fileobj*, a binary readable) into *dest* (default ``CACHE_FOLDER``).

    Streaming read (``mode='r|'``). Every member path is traversal-guarded via ``safe_resolve``
    (a malicious ``../`` member is skipped, not written outside the cache). Atomic temp+rename per
    file. Returns ``{extracted, bytes, skipped}``.
    """
    dest_root = cache_root(dest)
    dest_root.mkdir(parents=True, exist_ok=True)
    extracted = 0
    total = 0
    skipped = 0
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
    return {"extracted": extracted, "bytes": total, "skipped": skipped}
