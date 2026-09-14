"""Read-only numpy array sets shared across worker processes through memory-mapped ``.npy``
files in a DERIVED cache beside the source tree.

WHY. A GA runs N worker processes that each parse the same parquet into the same numpy arrays
and hold them privately: N copies of identical bytes. On the 2020 ThetaData option tree that
was ~15.6 GB PER WORKER (177.8M rows x ~88 B), which OOM-killed a 251 GB host at 16 workers.
Mapping the arrays from files lets the OS page cache hold ONE copy per host; measured
2026-09-14 (reports/strategy_research/option_array_sharing_bench_2026-09-14.md): -1..-6%
throughput at 32 workers, 5x less committed memory, on Windows and Linux.

RULES THAT CAME OUT OF THE MEASUREMENT (do not "simplify" them away):
  * Every opened array is ``np.asarray(np.load(path, mmap_mode='r'))``. The ``np.memmap``
    SUBCLASS costs 33-43% on scalar indexing; the plain ndarray view over the same mapping
    does not. The view keeps the mapping alive through ``.base``.
  * A mapped array is NEVER pickled or sent to a child (it materialises); every process opens
    its own through ``build_or_open``.
  * A published directory is NEVER edited in place. A changed source gets a NEW signature
    directory, and an obsolete one is removed only by ``_evict_dir``.

WINDOWS, AND WHY EVICTION IS ALL-OR-NOTHING. While any process maps ``<sig>/x.npy``, NTFS
refuses to delete it (WinError 32), refuses to rename it (WinError 32), AND refuses to rename
its PARENT directory (WinError 5) -- all three measured on this host. So a partial delete is a
trap, not an inconvenience: ``shutil.rmtree(..., ignore_errors=True)`` happily removes
``_done.json`` and leaves the mapped ``.npy``, producing a directory that is untrusted (so every
worker tries to rebuild) and immovable (so no worker can). ``_evict_dir`` therefore PROBES
first: it renames every child aside, rolls every rename back on the first refusal and reports
False, leaving the directory byte-for-byte intact and still trusted. Nothing in this module
deletes a published directory any other way.

LAYOUT.  ``<derived_root>/<key>/<sig>/<name>.npy`` + ``<derived_root>/<key>/<sig>/_done.json``
(written LAST -- a directory without it is not trusted and is rebuilt). ``sig`` is a sha1 over
the sorted ``(resolved path, size, mtime_ns)`` of the SOURCE files plus ``SCHEMA_VERSION`` --
the same "immutable history" identity the cache-sync layer keys on. Builds happen in
``<key>/<sig>.<pid>.<tid>.tmp/`` and are published with one ``os.replace`` (atomic on POSIX
and on Windows for a same-volume rename). A ``<key>/<sig>.lock`` created with O_EXCL serialises
concurrent cold builders across processes; the losers wait for ``_done.json``.

ESCAPE HATCH. ``BA2_SHARED_ARRAYS=0`` -> ``build_or_open`` returns ``build_fn()`` directly
(private arrays, nothing written): the pre-2026-09-14 behaviour, and what every parity test
compares against.

This module is deliberately free of any ``ba2_*`` import (pure numpy + stdlib) so that
``ba2_providers`` and the backtest engine can depend on it without a cycle.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

import numpy as np

DONE_MARKER = "_done.json"
DERIVED_DIRNAME = "_derived"
TMP_SUFFIX = ".tmp"
EVICT_SUFFIX = ".evict"
SCHEMA_VERSION = 1          #: bump when layout/meaning changes; every signature moves
LOCK_STALE_S = float(os.getenv("BA2_SHARED_ARRAYS_LOCK_STALE_S", "900"))
_WAIT_POLL_S = 0.25

#: Names NTFS refuses as a path segment whatever the extension; PRN and AUX are plausible
#: tickers, so a symbol key must be refused rather than silently mangled.
_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_UNSAFE_CHARS = '/\\:*?"<>|'

ArrayDict = Dict[str, np.ndarray]
PathLike = Union[str, os.PathLike]
BuildFn = Callable[[], ArrayDict]


def enabled() -> bool:
    """True unless ``BA2_SHARED_ARRAYS`` is set to a falsey value (the escape hatch)."""
    return os.getenv("BA2_SHARED_ARRAYS", "1").strip().lower() not in ("0", "false", "no", "off")


def derived_root_for(source_root: PathLike) -> str:
    """``.../cache/<Provider>`` -> ``.../cache/_derived/<Provider>``.

    The derived tree is a SIBLING of the source tree, never inside it: the cache-sync layer
    mirrors the source tree wholesale and must not carry machine-local mappings with it.

    Raises ValueError for a bare filesystem or drive root, which has no provider name to
    mirror and would otherwise yield a relative path.
    """
    s = os.fspath(source_root)
    drive, rest = os.path.splitdrive(str(s))
    rest = rest.rstrip("/\\")
    if not rest:
        raise ValueError(
            f"shared_arrays: {source_root!r} is a bare root, not a provider cache directory"
        )
    s = drive + rest
    return os.path.join(os.path.dirname(s), DERIVED_DIRNAME, os.path.basename(s))


def _tmp_dir_name(sig: str) -> str:
    """Name of a private staging directory for ``sig``; unique per process AND per thread."""
    return f"{sig}.{os.getpid()}.{threading.get_ident()}{TMP_SUFFIX}"


def _safe_key(key: str) -> str:
    """Return ``key`` as a single safe path segment, or raise ValueError if it cannot be one.

    Refusing is the point. Sanitising ``""`` or ``"."`` down to something empty would collapse
    ``key_dir`` onto the store ROOT, and the next ``_remove_stale_siblings`` would then treat
    every OTHER key as an obsolete sibling and evict the whole cache (reproduced).
    """
    cleaned = "".join(
        "_" if (c in _UNSAFE_CHARS or ord(c) < 32) else c for c in str(key)
    ).strip().rstrip(".")
    if cleaned in ("", ".", ".."):
        raise ValueError(f"shared_arrays: {key!r} is not usable as a cache key")
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        raise ValueError(
            f"shared_arrays: {key!r} is a reserved device name on Windows and cannot be a "
            "cache key; prefix it (e.g. 'sym_PRN') at the call site"
        )
    return cleaned


def _marker_mtime(d: Path) -> Optional[float]:
    """mtime of the done-marker in ``d``, or None if ``d`` is not a trusted directory.

    Read through in ONE stat: an ``is_file()`` followed by a ``stat()`` is a TOCTOU that
    raised FileNotFoundError out of ``sweep()`` when a sibling vanished mid-scan.
    """
    try:
        return (d / DONE_MARKER).stat().st_mtime
    except OSError:
        return None


def _tmp_is_live(d: Path) -> bool:
    """True if ``d`` looks like a staging directory a builder is still writing into.

    A build in flight touches its tmp directory continuously, so an mtime younger than
    ``LOCK_STALE_S`` means "someone owns this". Deleting it out from under them raised
    FileNotFoundError from inside ``np.save`` (reproduced). Unreadable -> assume live.
    """
    try:
        return time.time() - d.stat().st_mtime < LOCK_STALE_S
    except OSError:
        return True


class DerivedArrayStore:
    """A derived ``.npy`` cache rooted at ``root``, keyed by ``<key>/<signature>``."""

    def __init__(self, root: PathLike) -> None:
        self.root = Path(root)

    # ---------------------------------------------------------------- identity

    def signature(self, sources: Iterable[PathLike]) -> str:
        """sha1 over ``SCHEMA_VERSION`` + the sorted ``(path, size, mtime_ns)`` of the sources.

        The full RESOLVED path is hashed, not the basename: a per-symbol tree of
        ``<symbol>/<year>.parquet`` files gives many same-named sources, and two of them with
        equal size and mtime would otherwise share a signature and serve one symbol's arrays
        for another.

        Content hashing is deliberately NOT used: the option/OHLCV parquet tree is append-only
        immutable history and can run to hundreds of GB, so stat() identity is both sufficient
        and the only affordable check.
        """
        sources = list(sources)
        if not sources:
            raise ValueError("shared_arrays: signature() needs at least one source file")
        h = hashlib.sha1(f"schema={SCHEMA_VERSION}\n".encode())
        rows: List[Tuple[str, int, int]] = []
        for s in sources:
            p = Path(s)
            st = p.stat()
            ident = p.resolve().as_posix()
            if os.name == "nt":
                ident = ident.lower()       # NTFS is case-insensitive; the identity must be too
            rows.append((ident, st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))))
        for ident, size, mtime in sorted(rows):
            h.update(f"{ident}|{size}|{mtime}\n".encode())
        return h.hexdigest()[:20]

    def key_dir(self, key: str) -> Path:
        """Directory holding every signature built for ``key``."""
        return self.root / _safe_key(key)

    def current_dir(self, key: str, sources: Iterable[PathLike]) -> Path:
        """Directory the arrays for ``key`` live in given the CURRENT state of ``sources``."""
        return self.key_dir(key) / self.signature(sources)

    def lock_path(self, key: str, sources: Iterable[PathLike]) -> Path:
        """The O_EXCL build lock guarding ``current_dir(key, sources)``."""
        return self.key_dir(key) / (self.signature(sources) + ".lock")

    # ------------------------------------------------------------ the entry point

    def build_or_open(self, key: str, sources: Iterable[PathLike], build_fn: BuildFn) -> ArrayDict:
        """Open the mapped arrays for ``key``, building them from ``build_fn`` if absent.

        ``build_fn`` takes no arguments and returns ``{name: ndarray}`` of C-contiguous
        numeric/bool arrays. It runs at most once per (key, signature) per host; concurrent
        callers wait on the lock and then open what the winner published. The returned arrays
        are plain read-only ``np.ndarray`` VIEWS over the mapping -- never ``np.memmap``, which
        is 33-43% slower on scalar reads -- except under the ``BA2_SHARED_ARRAYS=0`` escape
        hatch, which returns the private writable arrays of ``build_fn()`` and writes nothing.
        """
        if not enabled():
            return build_fn()
        sources = [Path(s) for s in sources]
        # Resolve the signature ONCE: recomputing it for the directory and then for the lock
        # would let a source that changes mid-call guard one path with the other's lock.
        sig = self.signature(sources)
        key_dir = self.key_dir(key)
        final = key_dir / sig
        lock = key_dir / (sig + ".lock")
        t_start = time.monotonic()
        while True:
            opened = self._try_open(final)
            if opened is not None:
                return opened
            key_dir.mkdir(parents=True, exist_ok=True)
            if self._acquire(lock):
                try:
                    opened = self._try_open(final)
                    if opened is None:
                        self._build(final, build_fn)
                        opened = self._try_open(final)
                        if opened is None:
                            raise RuntimeError(
                                f"shared_arrays: build of {final} left no readable set"
                            )
                    self._remove_stale_siblings(final)
                    return opened
                finally:
                    self._release(lock)
            # Somebody else holds the lock: wait for what they publish, and take over if they
            # die. A loop, not recursion -- a long-lived worker must not grow a stack per retry.
            t_wait = time.monotonic()
            while True:
                opened = self._try_open(final)
                if opened is not None:
                    return opened
                if not lock.exists() or self._lock_is_stale(lock):
                    break                       # the builder vanished; re-enter and take over
                if time.monotonic() - t_wait > LOCK_STALE_S:
                    raise TimeoutError(f"shared_arrays: waited {LOCK_STALE_S:.0f}s on {lock}")
                time.sleep(_WAIT_POLL_S)
            if time.monotonic() - t_start > 2 * LOCK_STALE_S:
                raise TimeoutError(
                    f"shared_arrays: {final} never settled after {2 * LOCK_STALE_S:.0f}s"
                )

    # ------------------------------------------------------------------- internals

    def _try_open(self, final: Path) -> Optional[ArrayDict]:
        """Map every array named by the done-marker of ``final``, or None if it is not usable.

        A marker that cannot be parsed, or an array that cannot be loaded (missing, truncated,
        not a ``.npy`` at all), reports "not usable" rather than raising: the caller's answer to
        both is the same rebuild, and the corrupt directory is then evicted by ``_publish``.
        """
        marker = final / DONE_MARKER
        try:
            names = json.loads(marker.read_text(encoding="utf-8"))["arrays"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        out: ArrayDict = {}
        for name in names:
            try:
                arr = np.load(final / f"{name}.npy", mmap_mode="r")
            except (OSError, ValueError):
                return None
            out[name] = np.asarray(arr)
        return out

    def _build(self, final: Path, build_fn: BuildFn) -> None:
        """Run ``build_fn`` into a private ``.tmp`` directory and publish it onto ``final``."""
        arrays = build_fn()
        tmp = final.parent / _tmp_dir_name(final.name)
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            for name, arr in arrays.items():
                a = np.asarray(arr)
                if a.dtype == object:
                    raise TypeError(
                        f"shared_arrays: {name!r} is an object array; only numeric/bool arrays "
                        "can be shared"
                    )
                if a.ndim and not a.flags.c_contiguous:
                    # np.ascontiguousarray here would silently double peak RSS -- 15 GB of it
                    # for the option set this module exists for. Make the copy the caller's,
                    # where it is visible in the builder that produced the odd layout.
                    raise ValueError(
                        f"shared_arrays: {name!r} is not C-contiguous; copy it with "
                        "np.ascontiguousarray inside the builder, where the cost is visible"
                    )
                self._write_fsynced(tmp / f"{name}.npy", lambda f, a=a: np.save(f, a, allow_pickle=False))
            payload = json.dumps({"arrays": sorted(arrays), "schema": SCHEMA_VERSION,
                                  "written_at": time.time(), "pid": os.getpid()})
            # The marker is written and fsynced LAST: it is the only thing _try_open trusts, so
            # a crash anywhere above leaves a directory nobody will read.
            self._write_fsynced(tmp / DONE_MARKER, lambda f: f.write(payload.encode("utf-8")))
            self._publish(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    @staticmethod
    def _write_fsynced(path: Path, write: Callable[[object], None]) -> None:
        """Write ``path`` through ``write(fileobj)`` and fsync it before returning.

        Without the fsync a crash can leave a fully published, marker-carrying directory whose
        ``.npy`` bodies are still in the OS write cache -- NTFS would then serve a correctly
        sized extent of zeros, which is wrong data rather than a detectable failure. The
        directory ENTRY is not fsynced: losing the publish itself only costs a rebuild.
        """
        with open(path, "wb") as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())

    def _publish(self, tmp: Path, final: Path) -> None:
        """Move ``tmp`` onto ``final`` with a single rename, clearing whatever is in the way.

        ``os.replace`` cannot rename a directory onto an existing one (ENOTEMPTY on POSIX, a
        plain refusal on Windows), so anything already at ``final`` has to go first, and going
        means ``_evict_dir`` -- the only removal that cannot leave a half-deleted directory
        behind (see the module docstring).
        """
        if final.exists() and _marker_mtime(final) is None:
            if not self._evict_dir(final):
                raise RuntimeError(
                    f"shared_arrays: {final} has no {DONE_MARKER} but cannot be removed or "
                    "moved aside; a process on this host still maps a file inside it. Stop the "
                    "workers (or let them exit) and run sweep()."
                )
        try:
            os.replace(tmp, final)
        except OSError:
            # Trust READABILITY, not the mere presence of a marker: a marked-but-truncated
            # final would otherwise make us discard a good build and then fail forever.
            if self._try_open(final) is not None:
                shutil.rmtree(tmp, ignore_errors=True)      # another builder really won
            elif self._evict_dir(final):
                os.replace(tmp, final)
            else:
                raise

    def _evict_dir(self, d: Path) -> bool:
        """Remove ``d`` entirely, or leave it exactly as it was and return False.

        Every child is renamed aside first. A file another process maps refuses that rename on
        NTFS, and the refusal is the probe: every rename already made is undone, so the
        directory keeps its marker and stays openable by anyone mid-flight. Only once the whole
        directory has proven movable is it deleted. On POSIX every rename succeeds and this is
        an rmtree with extra steps -- which is the point: one behaviour on both platforms.
        """
        try:
            children = list(d.iterdir())
        except OSError:
            return not d.exists()
        # The marker goes LAST. While a data file is renamed but the marker is not, a concurrent
        # reader must still see a TRUSTED directory, or it starts the rebuild this probe exists
        # to prevent.
        children.sort(key=lambda c: c.name == DONE_MARKER)
        renamed: List[Tuple[Path, Path]] = []

        def _rollback() -> None:
            for original, moved in reversed(renamed):
                try:
                    os.rename(moved, original)
                except OSError:
                    pass                    # best effort; sweep() collects whatever is left
        for child in children:
            target = child.with_name(child.name + EVICT_SUFFIX)
            try:
                os.rename(child, target)
            except OSError:
                _rollback()
                return False
            renamed.append((child, target))
        try:
            shutil.rmtree(d)
        except OSError:
            _rollback()
            return False
        return True

    def _remove_stale_siblings(self, final: Path) -> None:
        """Best-effort eviction of OBSOLETE signature directories for the same key.

        Two guards, both for concurrency rather than tidiness. Only DIRECTORIES are considered,
        so a sibling ``<sig>.lock`` held by a process building a different signature is never
        touched. And a sibling whose marker is younger than ``LOCK_STALE_S`` is left alone: it
        may have been published seconds ago by another worker that has not opened it yet, and
        this call has no way to tell that from genuine garbage.
        """
        now = time.time()
        try:
            children = list(final.parent.iterdir())
        except OSError:
            return
        for d in children:
            if d == final or d.name.endswith(TMP_SUFFIX):
                continue
            try:
                if not d.is_dir():
                    continue
            except OSError:
                continue
            mtime = _marker_mtime(d)
            if mtime is None or now - mtime < LOCK_STALE_S:
                continue                    # marker-less or freshly published: leave to sweep()
            self._evict_dir(d)

    def _acquire(self, lock: Path) -> bool:
        """Take the O_EXCL build lock, breaking it first if it is older than LOCK_STALE_S."""
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._lock_is_stale(lock):
                    return False
                try:
                    lock.unlink()
                except FileNotFoundError:
                    continue                # somebody else broke it; race for it again
                except OSError:
                    return False
                continue
            except OSError:
                return False
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return True

    def _release(self, lock: Path) -> None:
        """Drop the build lock, but only if it is still OURS.

        A build that overran ``LOCK_STALE_S`` has had its lock broken and re-taken by another
        process; unlinking that one would hand a third process a lock the second still thinks
        it holds.
        """
        try:
            if lock.read_text(encoding="utf-8").strip() != str(os.getpid()):
                return
            lock.unlink()
        except OSError:
            pass

    @staticmethod
    def _lock_is_stale(lock: Path) -> bool:
        """True when the mtime of ``lock`` is older than ``LOCK_STALE_S`` (or it vanished)."""
        try:
            return time.time() - lock.stat().st_mtime > LOCK_STALE_S
        except OSError:
            return True

    def sweep(self) -> int:
        """Collect superseded signatures, marker-less orphans and dead ``.tmp`` dirs.

        Returns how many directories were removed. Idempotent and total: every scan step
        tolerates a sibling vanishing underneath it, and a directory another process still maps
        is simply left for the next sweep by ``_evict_dir``.

        Orphans -- a directory with no marker and no ``.tmp`` suffix -- are collected here and
        only here. Nothing can ever open one by name (``_try_open`` needs the marker), so it is
        pure garbage, and it is exactly what an interrupted publish or a pre-``_evict_dir``
        partial delete leaves behind.
        """
        removed = 0
        try:
            key_dirs = list(self.root.iterdir())
        except OSError:
            return 0
        for key_dir in key_dirs:
            try:
                if not key_dir.is_dir():
                    continue
                children = [d for d in key_dir.iterdir() if d.is_dir()]
            except OSError:
                continue
            done: List[Tuple[float, Path]] = []
            orphans: List[Path] = []
            dead_tmps: List[Path] = []
            for d in children:
                if d.name.endswith(TMP_SUFFIX):
                    if not _tmp_is_live(d):
                        dead_tmps.append(d)
                    continue
                mtime = _marker_mtime(d)
                if mtime is None:
                    orphans.append(d)
                else:
                    done.append((mtime, d))
            done.sort(key=lambda row: row[0])
            for d in [row[1] for row in done[:-1]] + orphans + dead_tmps:
                if self._evict_dir(d):
                    removed += 1
        return removed
