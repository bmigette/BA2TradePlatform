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

MARKET-CONDITION FEATURE OBJECTS are the one bucket with stronger rules than the rest of the
cache (design 2026-09-15 section 4.5), and both of them are implemented here:

  * ARRIVAL IS VERIFIED BY SHA256, not by size. Every other bucket is append-only vendor history
    where ``(rel_path, size)`` is identity; a feature object's name IS its sha256, its content
    decides what a gated genome trades, and a truncated-then-repadded or bit-flipped transfer
    would match on size and be mapped into every worker on the box. ``verify_market_conditions``
    re-hashes every object and raw shard any locally present manifest references, and the worker
    runs it after each push that touched the bucket.
  * PRUNING IS SNAPSHOT-SCOPED. ``diff_stale`` lists what the master's CURRENT manifest does not
    carry, which for this bucket includes objects pinned by a manifest ANOTHER job (or a stored
    backtest, or a captured replay) still reads on that worker. ``prune_paths`` therefore walks
    the worker's own manifests first and refuses to delete anything they reference.

Neither adds work to the ordinary path: the hash pass reads only the market-condition bucket, and
a threshold-only rerun pushes nothing but a manifest (its objects are already there, matched by
the same cheap ``(rel_path, size)`` diff as everything else).
"""

from __future__ import annotations

import json
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
from ba2_common.core.market_condition_store import (
    MANIFESTS_DIRNAME,
    MC_DIRNAME,
    RAW_DIRNAME,
)
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

# The central market-condition feature store bucket. The layout constants come FROM the store
# module (``market_conditions/raw/<sha>.parquet``,
# ``market_conditions/<profile>/objects/<sha>.parquet``,
# ``market_conditions/<profile>/manifests/<digest>.json``) so a layout change moves both ends
# together instead of leaving a silently-wrong string here.
MC_BUCKET = MC_DIRNAME


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
        # os.walk defaults to followlinks=False, which is exactly Path.rglob's behaviour on 3.12
        # (Path.walk(follow_symlinks=False)); junctions are not symlinks to Python and are
        # descended by both. Deliberately NOT following symlinks: a cycle would loop this walk,
        # and a symlinked-in subtree is symmetric on master and worker so it never reaches
        # diff_stale.
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
                # not a regular file (fifo/device, or a file-became-dir race); stat follows
                # symlinks, so a symlinked file still passes
                if not S_ISREG(st.st_mode):
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

    ``market_conditions`` counts the members that landed in the feature-store bucket and
    ``market_condition_paths`` names them, so the receiving side can verify exactly the snapshots
    this push touched instead of re-hashing every manifest on the host (every other push must
    stay exactly as cheap as it is today).
    """
    dest_root = cache_root(dest)
    dest_root.mkdir(parents=True, exist_ok=True)
    extracted = 0
    total = 0
    skipped = 0
    mc: List[str] = []
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
            name = member.name.replace("\\", "/")
            if name.startswith(MC_BUCKET + "/"):
                mc.append(name)
            total += member.size
            now = time.monotonic()
            if now - last_log >= _PROGRESS_LOG_INTERVAL_S:
                log(f"cache extract: {extracted} file(s), {format_bytes(total)} so far "
                    f"(last: {member.name})")
                last_log = now
    return {"extracted": extracted, "bytes": total, "skipped": skipped,
            "market_conditions": len(mc), "market_condition_paths": mc}


def mc_manifest_files(root: Optional[str] = None) -> List[Path]:
    """Every market-condition manifest FILE present under *root* (all profiles, sorted)."""
    mc = cache_root(root) / MC_BUCKET
    if not mc.is_dir():
        return []
    out: List[Path] = []
    for profile_dir in sorted(mc.iterdir()):
        if not profile_dir.is_dir() or profile_dir.name == RAW_DIRNAME:
            continue
        manifests = profile_dir / MANIFESTS_DIRNAME
        if manifests.is_dir():
            out.extend(sorted(manifests.glob("*.json")))
    return out


def mc_referenced_paths(root: Optional[str] = None) -> set:
    """Cache-relative paths of every object and raw shard referenced by ANY manifest under *root*.

    This is the PROTECTED set. Whole-object reuse means an extension's manifest keeps the previous
    manifest's object hashes verbatim, and a raw shard is pinned by every manifest that
    transitively references it — so "the master's newest manifest does not list it" says nothing
    about whether something on this host still needs it. A manifest that cannot be parsed protects
    nothing of its own but is never treated as absent: it is reported by
    ``verify_market_conditions`` and left in place.
    """
    protected: set = set()
    for path in mc_manifest_files(root):
        protected.add(_manifest_rel(path))
        try:
            with open(path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            continue
        for key in ("objects", "raw_objects"):
            for entry in manifest.get(key) or ():
                rel = entry.get("path") if isinstance(entry, dict) else None
                if rel:
                    protected.add(f"{MC_BUCKET}/{rel}")
    return protected


def _manifest_rel(path: Path) -> str:
    """Cache-relative path of a manifest FILE (``market_conditions/<profile>/manifests/x.json``)."""
    return f"{MC_BUCKET}/{path.parent.parent.name}/{MANIFESTS_DIRNAME}/{path.name}"


def verify_market_conditions(root: Optional[str] = None,
                             rel_paths: Optional[Iterable[str]] = None) -> dict:
    """Re-hash (sha256) the objects and raw shards the locally present manifests reference.

    Runs on the RECEIVING side after a push that carried the bucket. Size equality is not
    integrity: an object's file name is the sha256 of its bytes, its content decides what a gated
    genome trades, and a same-size corruption is exactly what the ordinary ``(rel_path, size)``
    diff structurally cannot see — it would be mapped by every worker process on the host.

    SCOPE. ``rel_paths`` (what a push actually delivered) narrows the pass to the manifests that
    reference at least one of those files, plus any manifest that arrived in the push itself.
    Without it every manifest on the host is checked, which on a box carrying a season of
    snapshots means re-hashing the whole bucket on every push that touched one object — and, far
    worse, lets ONE stale manifest's missing object condemn every other digest. A full scan stays
    available (``rel_paths=None``) for an explicit integrity check.

    Returns ``{ok, manifests, checked, missing, corrupt, errors, failed_digests, checked_digests}``
    — the two digest lists are what a caller revokes and keeps. Never raises: the caller (a
    worker's ``/cache/push``, a preparation step) reports and refuses work rather than dying.
    """
    from ba2_common.core.market_condition_store import manifest_identity, sha256_file

    base = cache_root(root)
    wanted = None
    if rel_paths is not None:
        wanted = {str(r).replace("\\", "/").lstrip("/") for r in rel_paths}
    checked = 0
    missing: List[str] = []
    corrupt: List[str] = []
    errors: List[str] = []
    failed: List[str] = []
    seen: List[str] = []
    for path in mc_manifest_files(root):
        try:
            with open(path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError) as e:
            errors.append(f"{path.name}: unreadable manifest ({e})")
            continue
        refs = [f"{MC_BUCKET}/{e['path']}"
                for key in ("objects", "raw_objects")
                for e in (manifest.get(key) or ()) if isinstance(e, dict) and e.get("path")]
        if wanted is not None and _manifest_rel(path) not in wanted and not (wanted & set(refs)):
            continue                      # nothing this push delivered belongs to this snapshot
        seen.append(path.stem)
        try:
            if manifest_identity(manifest) != path.stem:
                corrupt.append(_manifest_rel(path))
                failed.append(path.stem)
                continue
        except (KeyError, TypeError, ValueError) as e:
            errors.append(f"{path.name}: malformed manifest ({e})")
            failed.append(path.stem)
            continue
        bad = False
        for key in ("objects", "raw_objects"):
            for entry in manifest.get(key) or ():
                rel = f"{MC_BUCKET}/{entry['path']}"
                target = base / Path(*rel.split("/"))
                checked += 1
                if not target.is_file():
                    missing.append(rel)
                    bad = True
                    continue
                try:
                    if sha256_file(target) != entry["sha256"]:
                        corrupt.append(rel)
                        bad = True
                except OSError as e:
                    errors.append(f"{rel}: {e}")
                    bad = True
        if bad:
            failed.append(path.stem)
    return {"ok": not (missing or corrupt or errors), "manifests": len(seen),
            "checked": checked, "missing": sorted(set(missing)), "corrupt": sorted(set(corrupt)),
            "errors": errors, "failed_digests": sorted(set(failed)),
            "checked_digests": sorted(set(seen))}


def prune_paths(rel_paths: Iterable[str], root: Optional[str] = None) -> dict:
    """Delete *rel_paths* under *root* (default ``CACHE_FOLDER``). Traversal-guarded via
    ``safe_resolve``; a path outside the root is skipped, not deleted. Missing files are a
    no-op (already gone). Returns ``{pruned, skipped, failed, protected}``.

    One undeletable path never aborts the sweep: on Windows a file another process still has
    open/memory-mapped refuses to unlink (WinError 32 -> PermissionError), and letting that
    escape mid-loop would leave every genuinely-stale file AFTER it un-pruned — exactly the
    silent-corruption case ``diff_stale`` exists to prevent. Each failure is counted and logged.

    SNAPSHOT SCOPE. Anything under ``market_conditions/`` that a manifest ON THIS HOST references
    is PROTECTED and counted rather than deleted: the caller's stale list is computed against the
    master's current manifest, which knows nothing about the other jobs, stored backtests and
    captured replays pinned here (design section 4.5: "generic stale-file pruning must not remove
    another job's retained history"). The protected set is walked only when the list actually
    names the bucket, so every other prune is byte-identical to before.
    """
    base = str(cache_root(root))
    rel_paths = list(rel_paths)
    protected = (mc_referenced_paths(root)
                 if any(str(r).replace("\\", "/").startswith(MC_BUCKET + "/") for r in rel_paths)
                 else set())
    pruned = 0
    skipped = 0
    failed = 0
    kept = 0
    for rel in rel_paths:
        if protected and str(rel).replace("\\", "/") in protected:
            kept += 1
            logger.info(f"cache prune: keeping {rel} — referenced by a manifest on this host")
            continue
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
    return {"pruned": pruned, "skipped": skipped, "failed": failed, "protected": kept}
