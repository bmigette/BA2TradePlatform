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
  * A mapped file is NEVER overwritten in place (Windows cannot even delete it while mapped:
    WinError 32). A changed source gets a NEW signature directory; stale directories are
    removed best-effort and otherwise left for ``sweep()``.

LAYOUT.  ``<derived_root>/<key>/<sig>/<name>.npy`` + ``<derived_root>/<key>/<sig>/_done.json``
(written LAST -- a directory without it is not trusted and is rebuilt). ``sig`` is a sha1 over
the sorted ``(basename, size, mtime_ns)`` of the SOURCE files plus ``SCHEMA_VERSION`` -- the
same "immutable history" identity the cache-sync layer keys on. Builds happen in
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
SCHEMA_VERSION = 1          #: bump when layout/meaning changes; every signature moves
LOCK_STALE_S = float(os.getenv("BA2_SHARED_ARRAYS_LOCK_STALE_S", "900"))
_WAIT_POLL_S = 0.25

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
    """
    s = str(source_root)
    stripped = s.rstrip("/\\")
    if stripped:                       # keep a bare root like "C:\\" or "/" usable
        s = stripped
    return os.path.join(os.path.dirname(s), DERIVED_DIRNAME, os.path.basename(s))


def _tmp_dir_name(sig: str) -> str:
    """Name of a private staging directory for ``sig``; unique per process AND per thread."""
    return f"{sig}.{os.getpid()}.{threading.get_ident()}.tmp"


def _safe_key(key: str) -> str:
    """Make ``key`` (a symbol, usually) safe as a single path segment on Windows and POSIX."""
    return "".join(c if c not in '/\\:*?"<>|' else "_" for c in key)


class DerivedArrayStore:
    """A derived ``.npy`` cache rooted at ``root``, keyed by ``<key>/<signature>``."""

    def __init__(self, root: PathLike) -> None:
        self.root = Path(root)

    # ---------------------------------------------------------------- identity

    def signature(self, sources: Iterable[PathLike]) -> str:
        """sha1 over ``SCHEMA_VERSION`` + the sorted ``(name, size, mtime_ns)`` of the sources.

        Content hashing is deliberately NOT used: the option/OHLCV parquet tree is append-only
        immutable history and can run to hundreds of GB, so stat() identity is both sufficient
        and the only affordable check.
        """
        h = hashlib.sha1(f"schema={SCHEMA_VERSION}\n".encode())
        rows: List[Tuple[str, int, int]] = []
        for s in sources:
            p = Path(s)
            st = p.stat()
            rows.append((p.name, st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))))
        for name, size, mtime in sorted(rows):
            h.update(f"{name}|{size}|{mtime}\n".encode())
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

        ``build_fn`` takes no arguments and returns ``{name: ndarray}`` of numeric/bool arrays.
        It runs at most once per (key, signature) per host; concurrent callers wait on the lock
        and then open what the winner published. The returned arrays are plain read-only
        ``np.ndarray`` VIEWS over the mapping -- never ``np.memmap``, which is 33-43% slower on
        scalar reads -- except under the ``BA2_SHARED_ARRAYS=0`` escape hatch, which returns
        ``build_fn()``'s private writable arrays and writes nothing to disk.
        """
        if not enabled():
            return build_fn()
        sources = [Path(s) for s in sources]
        final = self.current_dir(key, sources)
        opened = self._try_open(final)
        if opened is not None:
            return opened
        lock = self.lock_path(key, sources)
        lock.parent.mkdir(parents=True, exist_ok=True)
        if self._acquire(lock):
            try:
                opened = self._try_open(final)
                if opened is None:
                    self._build(final, build_fn)
                    opened = self._try_open(final)
                    if opened is None:
                        raise RuntimeError(f"shared_arrays: build of {final} left no readable set")
                self._remove_stale_siblings(final)
                return opened
            finally:
                self._release(lock)
        t0 = time.monotonic()
        while True:
            opened = self._try_open(final)
            if opened is not None:
                return opened
            if not lock.exists() or self._lock_is_stale(lock):
                return self.build_or_open(key, sources, build_fn)
            if time.monotonic() - t0 > LOCK_STALE_S:
                raise TimeoutError(f"shared_arrays: waited {LOCK_STALE_S:.0f}s on {lock}")
            time.sleep(_WAIT_POLL_S)

    # ------------------------------------------------------------------- internals

    def _try_open(self, final: Path) -> Optional[ArrayDict]:
        """Map every array named by the done-marker of ``final``, or None if it is not trusted."""
        marker = final / DONE_MARKER
        if not marker.is_file():
            return None
        try:
            names = json.loads(marker.read_text(encoding="utf-8"))["arrays"]
        except (OSError, ValueError, KeyError):
            return None
        out: ArrayDict = {}
        for name in names:
            p = final / f"{name}.npy"
            try:
                arr = np.load(p, mmap_mode="r")
            except ValueError:          # some numpy versions refuse to mmap a zero-length array
                arr = np.load(p, allow_pickle=False)
                arr.setflags(write=False)
            except OSError:
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
                a = np.ascontiguousarray(arr)
                if a.dtype == object:
                    raise TypeError(
                        f"shared_arrays: {name!r} is an object array; only numeric/bool arrays "
                        "can be shared"
                    )
                np.save(tmp / f"{name}.npy", a, allow_pickle=False)
            (tmp / DONE_MARKER).write_text(
                json.dumps({"arrays": sorted(arrays), "schema": SCHEMA_VERSION,
                            "written_at": time.time(), "pid": os.getpid()}),
                encoding="utf-8",
            )
            self._publish(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def _publish(self, tmp: Path, final: Path) -> None:
        """Move ``tmp`` onto ``final`` with a single rename, clearing an untrusted leftover.

        ``os.replace`` cannot rename a directory onto an existing one (ENOTEMPTY on POSIX, and
        a plain refusal on Windows), so an untrusted ``final`` -- an interrupted build, or a
        marker lost to a crash -- is first renamed ASIDE under a ``.tmp`` name. A rename never
        touches bytes another process may still have mapped (an rmtree here would fail with
        WinError 32), and ``sweep()`` finishes the delete if it cannot land now.
        """
        if final.exists() and not (final / DONE_MARKER).is_file():
            stale = final.parent / _tmp_dir_name(final.name + ".stale")
            try:
                os.replace(final, stale)
            except OSError:
                pass                    # still mapped somewhere; the replace below will say so
            else:
                shutil.rmtree(stale, ignore_errors=True)
        try:
            os.replace(tmp, final)
        except OSError:
            if (final / DONE_MARKER).is_file():
                shutil.rmtree(tmp, ignore_errors=True)   # another builder won the race
            else:
                raise

    def _remove_stale_siblings(self, final: Path) -> None:
        """Best-effort delete of older signature directories for the same key.

        Only DIRECTORIES are considered, so sibling ``<sig>.lock`` files (possibly held by
        another process building a different signature) are never touched, and ``.tmp``
        staging directories -- which may belong to a concurrent builder -- are left to
        ``sweep()``.
        """
        for d in final.parent.iterdir():
            if d == final or not d.is_dir() or d.name.endswith(".tmp"):
                continue
            shutil.rmtree(d, ignore_errors=True)

    def _acquire(self, lock: Path) -> bool:
        """Try to take the O_EXCL build lock; break it first if it is older than LOCK_STALE_S."""
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._lock_is_stale(lock):
                try:
                    lock.unlink()
                except OSError:
                    return False
                return self._acquire(lock)
            return False
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True

    def _release(self, lock: Path) -> None:
        """Drop the build lock; a failure here is harmless (the next waiter breaks it)."""
        try:
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
        """Delete every superseded signature directory and ``.tmp`` leftover; return the count.

        Idempotent, and safe to call while other processes read: a directory still mapped
        simply fails to delete on Windows and is retried by the next sweep.
        """
        removed = 0
        if not self.root.is_dir():
            return 0
        for key_dir in self.root.iterdir():
            if not key_dir.is_dir():
                continue
            children = [d for d in key_dir.iterdir() if d.is_dir()]
            done = [d for d in children if (d / DONE_MARKER).is_file()]
            done.sort(key=lambda d: (d / DONE_MARKER).stat().st_mtime)
            tmps = [d for d in children if d.name.endswith(".tmp")]
            for d in list(done[:-1]) + tmps:
                try:
                    shutil.rmtree(d)
                    removed += 1
                except OSError:
                    pass
        return removed
