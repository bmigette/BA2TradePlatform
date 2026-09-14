# Shared Memory-Mapped Arrays Across GA Workers — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (implementers and quality reviewers on **opus**, spec reviewers on **sonnet**).

**Goal:** GA worker processes stop holding private copies of read-only market data. Per-symbol numeric arrays are built ONCE per host into a derived cache of `.npy` files and every worker `np.load(..., mmap_mode='r')`s them, so the OS page cache holds one copy per host instead of one per process.

**Architecture:** A small shared module (`ba2_common.core.shared_arrays`) owns a derived-cache directory beside each source tree: `build_or_open(key, sources, build_fn)` computes a signature over the source files' `(rel_path, size, mtime_ns)`, and either opens an existing `<key>/<sig>/*.npy` set as `np.asarray(np.load(mmap_mode='r'))` or calls `build_fn()` once (cross-process lock file, atomic tmp-dir + `os.replace`, `_done.json` written last) and then opens it. Consumers change only at their load seam: the parquet option reader (`_load_raw_underlying`), the OHLCV bar cache (`AsOfPriceSource.preload`), and — behind a spike gate — the screener metric store. Hot paths are untouched: the Python list projections the bisects need are rebuilt per process from the mapped arrays (measured: `searchsorted` on the mapping is 4.4x slower, the lists are the right call), and every mapped array is wrapped in `np.asarray` (the `np.memmap` subclass costs 33-43% on scalar reads). Values stay bit-identical to the private path and a `BA2_SHARED_ARRAYS=0` escape hatch restores it.

**Tech Stack:** numpy `.npy` + `mmap_mode='r'`, `os.replace` atomic renames (same-volume, Windows-safe), `O_EXCL` lock files, pytest. No new dependencies.

**Measured basis (2026-09-14, `reports/strategy_research/option_array_sharing_bench_2026-09-14.md`):** at 32 workers memmap is -6.3% (Linux) / -1.1% (Windows) throughput vs private for a 5x cut in committed memory; `multiprocessing.shared_memory` is equivalent and not worth its lifecycle; Windows cannot delete a mapped file (evict by path swap, never overwrite); a memmap must never be pickled to a child.

**Why now:** option stage 1 at the 2020 window OOM-killed remote227 at 20 AND 16 consumers — the reader holds 177.8M rows x ~88 B as private float64 per worker (~15.6 GB each, sixteen copies of 3.2 GB of parquet). Equity screener grids hold up to 5.3 GB of bars per worker and re-parse them before every individual (`BT_BAR_CACHE_TRIALS=0`; matrix3 paid ~8 h).

---

## Conventions every task follows

* **Where code goes.** Shared pure code -> `packages/common/ba2_common/core/shared_arrays.py` (CLAUDE.md: packages are the source of truth; in-tree files are shims). Test-platform consumers stay in `testplatform/backend/app/services/backtest/`.
* **Test commands** (Windows, run from the directories shown; never concurrently — see memory `worktree-test-running-quirks`):
  * packages/common: `cd packages/common && C:/Users/basti/ba2-venvs/test/Scripts/python.exe -m pytest tests/<file> -q -p no:cacheprovider`
  * backend: `cd testplatform/backend && C:/Users/basti/ba2-venvs/test/Scripts/python.exe -m pytest tests/<file> -q -p no:cacheprovider`
* **Derived cache location.** For a source tree at `<root>` the derived root is `<root>/../_derived/<basename(root)>` — i.e. `CACHE_FOLDER/_derived/ThetaDataOptionsProvider/`, `CACHE_FOLDER/_derived/FMPOHLCVProvider/`. Relative to the source root (NOT `CACHE_FOLDER` bound at import) so test fixtures under `tmp_path` get their own derived dir and the providers conftest `CACHE_FOLDER` rebind keeps working.
* **The derived cache is per-host and NEVER synced** to remote workers (Task 2): it is deterministic from the parquet the workers already have, and `.npy` is ~3x the parquet size. Each host builds its own on first touch.
* **Atomic writes.** Build into `<key>/<sig>.<pid>.<tid>.tmp/` (directory), write `_done.json` last, then `os.replace(tmpdir, finaldir)`. If the replace fails because `finaldir` now exists (a concurrent builder won), delete the tmp dir and open the winner. Mirrors `parquet_store._tmp_name` + `os.replace`.
* **Never overwrite a mapped file.** A signature change creates a NEW `<sig>` directory; old ones are removed best-effort (`PermissionError` on Windows means a worker still maps it — leave it, the sweep tool in Task 8 cleans later).
* **Escape hatch.** `BA2_SHARED_ARRAYS=0` makes `build_or_open` call `build_fn()` and return its arrays directly (private path, today's behaviour). Every parity test runs the consumer both ways and compares.
* **Commit after every task** with the message given; do NOT bump versions or push until Task 10.

---

### Task 1: `shared_arrays` module — build, open, signature, lock

**Files:**
- Create: `packages/common/ba2_common/core/shared_arrays.py`
- Test: `packages/common/tests/test_shared_arrays.py`

**Step 1: Write the failing tests**

```python
"""ba2_common.core.shared_arrays -- one derived-cache mechanism for every read-only array set
a GA worker used to hold privately. See docs/plans/2026-09-14-shared-arrays-across-workers.md."""
import json
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from ba2_common.core import shared_arrays as SA


def _src(tmp_path, name="a.parquet", payload=b"x" * 100):
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    return p


def _arrays():
    return {
        "close": np.array([1.0, 2.5, np.nan], dtype="float64"),
        "bar_ord": np.array([737000, 737001, 737002], dtype="int32"),
        "is_call": np.array([True, False, True], dtype=bool),
        "empty": np.empty(0, dtype="float64"),
    }


def test_build_then_open_returns_equal_arrays_not_memmap_subclass(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    calls = []
    def build():
        calls.append(1)
        return _arrays()
    got = store.build_or_open("AAPL", [_src(tmp_path)], build)
    assert calls == [1]
    for k, v in _arrays().items():
        np.testing.assert_array_equal(got[k], v)
        assert got[k].dtype == v.dtype
        assert type(got[k]) is np.ndarray, "must be np.asarray-wrapped, not np.memmap"
    # Second open: no rebuild, data comes from the files.
    got2 = store.build_or_open("AAPL", [_src(tmp_path)], build)
    assert calls == [1]
    np.testing.assert_array_equal(got2["close"], _arrays()["close"])


def test_opened_arrays_are_file_backed_and_read_only(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    got = store.build_or_open("AAPL", [_src(tmp_path)], _arrays)
    assert isinstance(got["close"].base, np.memmap) or got["close"].base is not None
    with pytest.raises((ValueError, TypeError)):
        got["close"][0] = 99.0


def test_signature_tracks_source_size_and_mtime(tmp_path):
    src = _src(tmp_path)
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    s1 = store.signature([src])
    assert s1 == store.signature([src])                      # stable
    src.write_bytes(b"y" * 101)                              # size change
    s2 = store.signature([src])
    assert s2 != s1
    os.utime(src, (time.time() + 100, time.time() + 100))    # mtime change, same size
    assert store.signature([src]) != s2


def test_source_change_triggers_rebuild_in_a_new_directory(tmp_path):
    src = _src(tmp_path)
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    calls = []
    def build():
        calls.append(1)
        return _arrays()
    store.build_or_open("AAPL", [src], build)
    d1 = store.current_dir("AAPL", [src])
    src.write_bytes(b"z" * 200)
    store.build_or_open("AAPL", [src], build)
    d2 = store.current_dir("AAPL", [src])
    assert calls == [1, 1]
    assert d1 != d2 and d2.exists()


def test_done_marker_is_required_and_written_last(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    store.build_or_open("AAPL", [src], _arrays)
    d = store.current_dir("AAPL", [src])
    (d / SA.DONE_MARKER).unlink()
    calls = []
    def build():
        calls.append(1)
        return _arrays()
    store.build_or_open("AAPL", [src], build)   # no marker => not trusted => rebuilt
    assert calls == [1]
    assert json.loads((store.current_dir("AAPL", [src]) / SA.DONE_MARKER).read_text())["arrays"] == sorted(_arrays())


def test_build_fn_exception_leaves_nothing_behind(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    def boom():
        raise RuntimeError("cache miss")
    with pytest.raises(RuntimeError):
        store.build_or_open("AAPL", [src], boom)
    key_dir = tmp_path / "_derived" / "X" / "AAPL"
    assert not key_dir.exists() or not any(key_dir.iterdir())


def test_concurrent_builders_build_once(tmp_path):
    """Two threads (stand-ins for two worker processes) race on a cold key: one builds, the
    other waits on the lock and opens the result. build_fn runs exactly once."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    calls = []
    def slow_build():
        calls.append(1)
        time.sleep(0.5)
        return _arrays()
    out = {}
    def run(i):
        out[i] = store.build_or_open("AAPL", [src], slow_build)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert calls == [1]
    np.testing.assert_array_equal(out[0]["close"], out[1]["close"])


def test_stale_lock_is_broken(tmp_path, monkeypatch):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    lock = store.lock_path("AAPL", [src])
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("dead pid")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    monkeypatch.setattr(SA, "LOCK_STALE_S", 60.0)
    got = store.build_or_open("AAPL", [src], _arrays)
    np.testing.assert_array_equal(got["close"], _arrays()["close"])


def test_escape_hatch_returns_private_arrays_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "0")
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    got = store.build_or_open("AAPL", [_src(tmp_path)], _arrays)
    np.testing.assert_array_equal(got["close"], _arrays()["close"])
    assert not (tmp_path / "_derived").exists()
    got["close"][0] = 5.0   # private, writable


def test_derived_root_for_source_tree():
    assert SA.derived_root_for(r"C:\cache\ThetaDataOptionsProvider") == \
        os.path.join(r"C:\cache", "_derived", "ThetaDataOptionsProvider")
    assert SA.derived_root_for("/home/x/cache/FMPOHLCVProvider/") == \
        os.path.join("/home/x/cache", "_derived", "FMPOHLCVProvider")


def test_tmp_dirs_use_the_skip_suffix_and_lock_files_the_lock_suffix(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    assert store.lock_path("AAPL", [src]).name.endswith(".lock")
    assert SA._tmp_dir_name("abc").endswith(".tmp")
```

**Step 2: Run to verify they fail**

Run: `cd packages/common && C:/Users/basti/ba2-venvs/test/Scripts/python.exe -m pytest tests/test_shared_arrays.py -q -p no:cacheprovider`
Expected: `ModuleNotFoundError: No module named 'ba2_common.core.shared_arrays'`

**Step 3: Implement**

```python
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
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

import numpy as np

DONE_MARKER = "_done.json"
DERIVED_DIRNAME = "_derived"
#: Bump when the on-disk layout or the meaning of an array changes; every signature moves.
SCHEMA_VERSION = 1
#: A lock older than this is a crashed builder; break it. Cold builds of the largest
#: underlying measured ~10 s, so 15 min is generous without stranding a pool for an hour.
LOCK_STALE_S = float(os.getenv("BA2_SHARED_ARRAYS_LOCK_STALE_S", "900"))
_WAIT_POLL_S = 0.25

ArrayDict = Dict[str, np.ndarray]


def enabled() -> bool:
    return os.getenv("BA2_SHARED_ARRAYS", "1").strip().lower() not in ("0", "false", "no", "off")


def derived_root_for(source_root: str) -> str:
    """``<parent of source_root>/_derived/<basename(source_root)>``.

    Relative to the SOURCE tree, not to CACHE_FOLDER bound at import: a test fixture under
    tmp_path gets its own derived dir, and the providers conftest CACHE_FOLDER rebind holds.
    """
    p = Path(str(source_root).rstrip("/\\"))
    return str(p.parent / DERIVED_DIRNAME / p.name)


def _tmp_dir_name(sig: str) -> str:
    return f"{sig}.{os.getpid()}.{threading.get_ident()}.tmp"


def _safe_key(key: str) -> str:
    # One path segment; keys are symbols / "SYM_1d" style names, so only guard the separators.
    return "".join(c if c not in '/\\:*?"<>|' else "_" for c in key)


class DerivedArrayStore:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    # -- identity -----------------------------------------------------------------------
    def signature(self, sources: Iterable[str | os.PathLike]) -> str:
        h = hashlib.sha1(f"schema={SCHEMA_VERSION}\n".encode())
        rows = []
        for s in sources:
            p = Path(s)
            st = p.stat()
            rows.append((p.name, st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))))
        for name, size, mtime in sorted(rows):
            h.update(f"{name}|{size}|{mtime}\n".encode())
        return h.hexdigest()[:20]

    def key_dir(self, key: str) -> Path:
        return self.root / _safe_key(key)

    def current_dir(self, key: str, sources: Iterable[str | os.PathLike]) -> Path:
        return self.key_dir(key) / self.signature(sources)

    def lock_path(self, key: str, sources: Iterable[str | os.PathLike]) -> Path:
        return self.key_dir(key) / (self.signature(sources) + ".lock")

    # -- the one entry point -------------------------------------------------------------
    def build_or_open(self, key: str, sources: Iterable[str | os.PathLike],
                      build_fn: Callable[[], ArrayDict]) -> ArrayDict:
        """Open ``key``'s arrays for the current source signature, building them first if
        absent. ``build_fn`` must return ``{name: 1-D ndarray}`` (any dtype np.save handles;
        object arrays are refused -- keep string projections out of here and rebuild them per
        process from the mapped arrays)."""
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
                opened = self._try_open(final)      # a winner may have finished meanwhile
                if opened is None:
                    self._build(final, build_fn)
                    opened = self._try_open(final)
                    if opened is None:
                        raise RuntimeError(f"shared_arrays: build of {final} left no readable set")
                self._remove_stale_siblings(final)
                return opened
            finally:
                self._release(lock)
        # Lost the race: wait for the winner's marker (or its lock to go stale).
        t0 = time.monotonic()
        while True:
            opened = self._try_open(final)
            if opened is not None:
                return opened
            if not lock.exists() or self._lock_is_stale(lock):
                return self.build_or_open(key, sources, build_fn)   # take over
            if time.monotonic() - t0 > LOCK_STALE_S:
                raise TimeoutError(f"shared_arrays: waited {LOCK_STALE_S:.0f}s on {lock}")
            time.sleep(_WAIT_POLL_S)

    # -- internals ----------------------------------------------------------------------
    def _try_open(self, final: Path) -> Optional[ArrayDict]:
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
            except ValueError:
                # np.load refuses to mmap a zero-length array; it is a few bytes, load it.
                arr = np.load(p)
            except OSError:
                return None
            out[name] = np.asarray(arr)     # NOT the np.memmap subclass -- see module docstring
        return out

    def _build(self, final: Path, build_fn: Callable[[], ArrayDict]) -> None:
        arrays = build_fn()
        tmp = final.parent / _tmp_dir_name(final.name)
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            for name, arr in arrays.items():
                a = np.ascontiguousarray(arr)
                if a.dtype == object:
                    raise TypeError(f"shared_arrays: {name!r} is an object array; only numeric/"
                                    f"bool arrays can be shared")
                np.save(tmp / f"{name}.npy", a, allow_pickle=False)
            (tmp / DONE_MARKER).write_text(json.dumps({
                "arrays": sorted(arrays), "schema": SCHEMA_VERSION,
                "written_at": time.time(), "pid": os.getpid()}), encoding="utf-8")
            try:
                os.replace(tmp, final)
            except OSError:
                if final.is_dir():          # a concurrent builder published first
                    shutil.rmtree(tmp, ignore_errors=True)
                else:
                    raise
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def _remove_stale_siblings(self, final: Path) -> None:
        for d in final.parent.iterdir():
            if d == final or not d.is_dir() or d.name.endswith(".tmp"):
                continue
            shutil.rmtree(d, ignore_errors=True)   # PermissionError on Windows = still mapped

    def _acquire(self, lock: Path) -> bool:
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
        try:
            lock.unlink()
        except OSError:
            pass

    @staticmethod
    def _lock_is_stale(lock: Path) -> bool:
        try:
            return time.time() - lock.stat().st_mtime > LOCK_STALE_S
        except OSError:
            return True

    # -- housekeeping -------------------------------------------------------------------
    def sweep(self) -> int:
        """Remove every signature directory that is not the newest per key plus leftover
        ``.tmp`` dirs. Returns directories removed. Best-effort; mapped files stay."""
        removed = 0
        if not self.root.is_dir():
            return 0
        for key_dir in self.root.iterdir():
            if not key_dir.is_dir():
                continue
            done = [d for d in key_dir.iterdir() if d.is_dir() and (d / DONE_MARKER).is_file()]
            done.sort(key=lambda d: (d / DONE_MARKER).stat().st_mtime)
            for d in list(done[:-1]) + [d for d in key_dir.iterdir()
                                        if d.is_dir() and d.name.endswith(".tmp")]:
                try:
                    shutil.rmtree(d)
                    removed += 1
                except OSError:
                    pass
        return removed
```

**Step 4: Run the tests**

Run: `cd packages/common && C:/Users/basti/ba2-venvs/test/Scripts/python.exe -m pytest tests/test_shared_arrays.py -q -p no:cacheprovider`
Expected: `11 passed`

**Step 5: Commit**

```bash
git add packages/common/ba2_common/core/shared_arrays.py packages/common/tests/test_shared_arrays.py
git commit -m "feat(shared-arrays): derived .npy cache with mmap open, signature invalidation, cross-process lock"
```

---

### Task 2: The derived cache is never synced to remote workers

**Files:**
- Modify: `testplatform/backend/app/services/cache_sync.py:46-57` (`_SKIP_SUFFIXES`, `_is_syncable`) and `build_manifest` (`:69-110`)
- Test: `testplatform/backend/tests/test_cache_sync.py` (append)

**Step 1: Failing test**

```python
def test_derived_array_cache_is_never_part_of_the_manifest(tmp_path):
    """The per-host derived .npy cache is deterministic from parquet the worker already has and
    ~3x its size; it must never ride the pre-flight push."""
    (tmp_path / "FMPOHLCVProvider").mkdir()
    (tmp_path / "FMPOHLCVProvider" / "AAPL_1d.parquet").write_bytes(b"p")
    d = tmp_path / "_derived" / "FMPOHLCVProvider" / "AAPL_1d" / "abc123"
    d.mkdir(parents=True)
    (d / "close.npy").write_bytes(b"n")
    (d / "_done.json").write_text("{}")
    from app.services import cache_sync
    rel = {f["rel_path"] for f in cache_sync.build_manifest(str(tmp_path))["files"]}
    assert rel == {"FMPOHLCVProvider/AAPL_1d.parquet"}
```

**Step 2: Run** — `cd testplatform/backend && ...pytest tests/test_cache_sync.py -q -k derived` — Expected: FAIL (the `.npy` and marker are listed).

**Step 3: Implement** — in `cache_sync.py`, next to `_SKIP_SUFFIXES`:

```python
# Per-host DERIVED caches (memory-mapped .npy sets built from parquet already on the worker --
# see ba2_common.core.shared_arrays). Deterministic from the source and ~3x its size, so each
# host builds its own on first touch instead of pulling it over the wire.
_SKIP_DIRNAMES = ("_derived",)
```

and in `_is_syncable(p)` add, before the suffix check: `if any(part in _SKIP_DIRNAMES for part in p.parts): return False`. (`p` is the absolute path in `build_manifest`; `_derived` only ever appears as a directory name, so a component match is exact.)

**Step 4: Run** the whole file: `...pytest tests/test_cache_sync.py -q` — Expected: all pass.

**Step 5: Commit** — `git commit -am "fix(cache-sync): never push the per-host _derived array cache"`

---

### Task 3: Option reader — `_RawUnderlying` built from an array dict

Pure refactor, no behaviour change: split the parquet-frame parsing (numeric arrays) from the per-process projections (lists/dicts), so Task 4 can source the numeric part from the derived store.

**Files:**
- Modify: `testplatform/backend/app/services/backtest/parquet_options_provider.py:228-341` (`_RawUnderlying`)
- Test: `testplatform/backend/tests/backtest/test_parquet_options_provider.py` (append)

**Step 1: Failing test**

```python
def test_raw_underlying_round_trips_through_its_array_dict(store_root):
    """The numeric arrays a _RawUnderlying is built from are a plain dict of 1-D arrays, and
    building from that dict gives the identical object -- the seam the shared store plugs into."""
    from app.services.backtest import parquet_options_provider as pq
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore
    df = OptionHistoryParquetStore(root=store_root).read_underlying("GOOG")
    arrays = pq._RawUnderlying.arrays_from_frame(df)
    assert set(arrays) == set(pq._RawUnderlying.ARRAY_NAMES)
    for name, arr in arrays.items():
        assert arr.ndim == 1 and arr.dtype != object, name
    a = pq._RawUnderlying("GOOG", df)
    b = pq._RawUnderlying.from_arrays("GOOG", arrays)
    for name in pq._RawUnderlying.ARRAY_NAMES:
        np.testing.assert_array_equal(getattr(a, name), getattr(b, name))
    assert a.c_occ == b.c_occ and a.c_index == b.c_index and a.bar_ord_l == b.bar_ord_l
    assert a.starts_l == b.starts_l and a.c_expiry_iso == b.c_expiry_iso
    assert a.c_right == b.c_right and a.has_quotes == b.has_quotes and a.n_rows == b.n_rows
```

**Step 2: Run** — Expected: `AttributeError: type object '_RawUnderlying' has no attribute 'arrays_from_frame'`.

**Step 3: Implement.** In `_RawUnderlying`:

* Add class constants:
  ```python
  #: The numeric per-row / per-contract arrays -- everything that can live in a shared store.
  #: ``c_occ`` (strings) is carried as ``c_occ_codes``+``c_occ_chars``? NO: the OCC symbols are
  #: needed as Python strings on the hot path (c_index dict), so they are stored ONCE per
  #: process as a list; they are ~1,400-60,000 short strings per underlying, not per row.
  ARRAY_NAMES = ("bar_ord", "open", "high", "low", "close", "volume", "open_interest",
                 "vendor_iv", "bid", "ask", "starts", "stops", "c_strike", "c_expiry_ord",
                 "c_is_call")
  ```
  plus `"c_occ_utf8"`: a 1-D `uint8` array of the OCC symbols joined by `\n` (so the strings ARE
  shareable and reconstructed per process with one `bytes(arr).decode().split("\n")`), and
  `"has_quotes"` as a 1-element bool array. Add both to `ARRAY_NAMES`.
* `@staticmethod arrays_from_frame(df) -> Dict[str, np.ndarray]`: the existing numeric part of `__init__` (sort, `starts/stops`, `bar_ord`, the eight float64 columns, `bid/ask` or NaN, `c_strike`, `c_expiry_ord`, `c_is_call`) returning the dict; `c_occ_utf8 = np.frombuffer("\n".join(c_occ).encode(), dtype=np.uint8)`; `has_quotes = np.array([bool], dtype=bool)`. The empty-frame branch returns zero-length arrays of the right dtype (`c_occ_utf8` empty, `has_quotes=[False]`).
* `@classmethod from_arrays(cls, underlying, arrays)`: `self = cls.__new__(cls)`; bind every `ARRAY_NAMES` entry via `setattr`; `has_quotes = bool(arrays["has_quotes"][0])`; `c_occ = bytes(arrays["c_occ_utf8"]).decode("utf-8").split("\n") if arrays["c_occ_utf8"].size else []`; then the existing projection block (`c_index`, `bar_ord_l` interned, `starts_l`, `stops_l`, `date_of_ord`, `iso_of_ord`, `c_expiry_ord_l/_date/_iso`, `c_strike_f`, `c_right`, `c_type_str`, `n_rows = len(bar_ord)`). The `priceless` invariant log stays in `arrays_from_frame` (it is a fact about the store, checked at build time).
* `__init__(self, underlying, df)` becomes: `arrays = self.arrays_from_frame(df); self._bind(underlying, arrays)` where `_bind` is the body of `from_arrays` — so existing callers and tests are unchanged.

**Step 4: Run** the whole reader test file: `...pytest tests/backtest/test_parquet_options_provider.py -q` — Expected: all pass (previous count + 1).

**Step 5: Commit** — `git commit -am "refactor(options): _RawUnderlying splits numeric arrays (shareable) from per-process projections"`

---

### Task 4: Option reader loads its arrays from the derived store

**Files:**
- Modify: `parquet_options_provider.py:545-566` (`_load_raw_underlying`), module docstring CACHING section
- Modify: `packages/providers/ba2_providers/options/parquet_store.py:414-425` — add `partition_paths(underlying) -> List[str]` (the same sorted glob `read_underlying` uses) so the reader can sign the sources without duplicating the glob.
- Test: `test_parquet_options_provider.py` (append), `packages/providers/tests/test_option_history_parquet_store.py` (append one test for `partition_paths`)

**Step 1: Failing tests**

```python
def test_shared_store_is_used_and_second_process_style_open_reads_files(store_root, monkeypatch):
    from app.services.backtest import parquet_options_provider as pq
    from ba2_common.core import shared_arrays as SA
    pq.clear_worker_parquet_options_cache()
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    raw = pq._load_raw_underlying(store_root, "GOOG")
    d = Path(SA.derived_root_for(store_root)) / "GOOG"
    sigs = [p for p in d.iterdir() if (p / SA.DONE_MARKER).is_file()]
    assert len(sigs) == 1 and (sigs[0] / "close.npy").is_file()
    assert type(raw.close) is np.ndarray and raw.close.base is not None   # mapped, wrapped
    # Simulate another process: clear this one's caches and count parquet reads on re-open.
    pq.clear_worker_parquet_options_cache()
    with _count_reads(monkeypatch) as n:     # existing helper; make it count read_underlying
        raw2 = pq._load_raw_underlying(store_root, "GOOG")
    assert n() == 0, "a built derived set must be opened, not re-parsed from parquet"
    np.testing.assert_array_equal(raw.close, raw2.close)


def test_shared_and_private_paths_are_bit_identical(store_root, quoted_store_root, monkeypatch):
    """The whole point: same chain, same marks, same greeks, whichever path served the arrays."""
    from app.services.backtest import parquet_options_provider as pq
    from datetime import date
    for root in (store_root, quoted_store_root):
        results = {}
        for flag in ("0", "1"):
            monkeypatch.setenv("BA2_SHARED_ARRAYS", flag)
            pq.clear_worker_parquet_options_cache()
            prov = pq.ParquetOptionsProvider(root, spot_source=lambda s, d: 100.0,
                                             risk_free_rate=0.04, spot_scope="t")
            u = prov._raw("GOOG") if hasattr(prov, "_raw") else pq._raw_underlying(root, "GOOG")
            chain = prov.get_chain("GOOG", date(2024, 3, 15))      # use a date the fixture covers
            results[flag] = ([c.__dict__ if hasattr(c, "__dict__") else c for c in chain],
                             {n: getattr(u, n).tolist() for n in pq._RawUnderlying.ARRAY_NAMES})
        assert results["0"] == results["1"]


def test_a_rewritten_partition_invalidates_the_derived_set(store_root, monkeypatch):
    from app.services.backtest import parquet_options_provider as pq
    from ba2_common.core import shared_arrays as SA
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    pq.clear_worker_parquet_options_cache()
    pq._load_raw_underlying(store_root, "GOOG")
    d = Path(SA.derived_root_for(store_root)) / "GOOG"
    before = {p.name for p in d.iterdir() if p.is_dir()}
    # Re-write one partition with the store's own writer (new mtime, maybe new size).
    store = OptionHistoryParquetStore(root=store_root)
    exp = store.completed_expiries("GOOG")[0]
    ... # rewrite: read_partition -> write_partition (see fixture for the argument shape)
    pq.clear_worker_parquet_options_cache()
    pq._load_raw_underlying(store_root, "GOOG")
    after = {p.name for p in d.iterdir() if p.is_dir() and (p / SA.DONE_MARKER).is_file()}
    assert after != before
```

Adjust `_count_reads` (`test_parquet_options_provider.py:304-318`) so it counts `OptionHistoryParquetStore.read_underlying` calls instead of `_load_raw_underlying` (the latter now runs on every open; the parquet READ is what a warm derived set skips). Update the two tests that rely on it (`test_a_new_spot_scope_reuses_the_PARQUET_BYTES...`, `test_a_new_risk_free_rate_also_only_redoes_the_greeks`) — their assertions (1 parquet read) still hold with the shared path ON.

**Step 2: Run** — Expected: the three new tests fail (`derived_root_for` dir absent / reads counted).

**Step 3: Implement**

`parquet_store.py`:
```python
    def partition_paths(self, underlying: str) -> List[str]:
        """Every readable ``*_1d.parquet`` partition for ``underlying``, sorted -- the exact
        set ``read_underlying`` concatenates, exposed so a derived cache can sign it."""
        base = os.path.join(self.root, underlying.upper())
        return sorted(glob.glob(os.path.join(base, "exp=*", f"*_{BARS_INTERVAL}.parquet")))
```
and `read_underlying` uses it.

`parquet_options_provider.py`:
```python
def _load_raw_underlying(root: str, underlying: str) -> "_RawUnderlying":
    from ba2_common.core import shared_arrays as _sa
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    store = OptionHistoryParquetStore(root=root)
    parts = store.partition_paths(underlying)
    if not parts:
        u = _RawUnderlying(underlying, None)
        logger.warning("[backtest] parquet option store: NO partitions for %s under %s — "
                       "every chain read for it will be empty.", underlying, root)
        return u

    def _build():
        return _RawUnderlying.arrays_from_frame(store.read_underlying(underlying))

    derived = _sa.DerivedArrayStore(_sa.derived_root_for(root))
    arrays = derived.build_or_open(underlying.upper(), parts, _build)
    u = _RawUnderlying.from_arrays(underlying, arrays)
    if u.n_rows:
        logger.info("[backtest] parquet option store: %s %d bars / %d contracts, %s..%s",
                    underlying, u.n_rows, len(u.c_occ),
                    date.fromordinal(int(u.bar_ord.min())).isoformat(),
                    date.fromordinal(int(u.bar_ord.max())).isoformat())
    return u
```
Update the CACHING docstring: the raw cache now holds *views over the host-shared derived set*; `clear_worker_parquet_options_cache()` drops the views (the mapping closes when the last view dies) and never touches the files. Update the `_UNDERLYING_CACHE_MAX` comment: the per-process cost of an entry is now the projections (~8 B/row for `bar_ord_l` + the contract lists), not the arrays.

**Step 4: Run** `tests/backtest/test_parquet_options_provider.py`, `tests/backtest/test_thetadata_store.py`, `tests/backtest/test_options_store_selection.py`, and `packages/providers/tests/test_option_history_parquet_store.py` (separate invocations). Expected: all pass.

**Step 5: Commit** — `git commit -am "feat(options): parquet reader maps its arrays from the host-shared derived cache"`

---

### Task 5: OHLCV bar cache — o/h/l/c/v mapped, keys private

Keys stay a private `array('q')` per process (the measured 26x scalar-read regression on an ndarray key path, `price_source.py:323-347`, is exactly the class of regression the benchmark warned about); the five float64 columns (40 of 48 B/bar) are shared.

**Files:**
- Modify: `testplatform/backend/app/services/backtest/price_source.py` — `_store` (`:667-687`), `load_bars_df` (`:709-735`), `preload` miss branch (`:578-599`), `memory_stats` (`:228-276`)
- Test: `testplatform/backend/tests/backtest/test_bar_cache_shared.py` (new)

**Step 1: Failing tests**

```python
"""OHLCV bars shared across workers through the derived store (Task 5 of
docs/plans/2026-09-14-shared-arrays-across-workers.md)."""
from array import array
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


def _native_tree(tmp_path, monkeypatch, syms=("AAA", "BBB"), n=300):
    """A CACHE_FOLDER/FMPOHLCVProvider/<SYM>_1d.parquet tree, like ba2-test fetch-cache writes."""
    import ba2_common.config as cfg
    from ba2_common.core import native_cache
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    dates = pd.bdate_range("2024-01-01", periods=n)
    for i, s in enumerate(syms):
        df = pd.DataFrame({"Date": dates, "Open": np.arange(n) + i, "High": np.arange(n) + i + 1,
                           "Low": np.arange(n) + i - 1, "Close": np.arange(n) + i + 0.5,
                           "Volume": np.arange(n) * 10.0})
        d = tmp_path / "FMPOHLCVProvider"; d.mkdir(exist_ok=True)
        df.to_parquet(d / f"{s}_1d.parquet", index=False)
    return tmp_path


def _source(tmp_path):
    from app.services.backtest import price_source as ps
    # Reuse the hermetic fixture provider the existing tests use (see test_hermetic_ohlcv_cache.py
    # for the FMPOHLCVProvider-named stand-in) wrapped in MemoizedOHLCVProvider(cached_only=True).
    ...
    return ps.AsOfPriceSource(ohlcv=provider, interval="1d")


def test_preload_maps_ohlcv_columns_and_keeps_keys_private(tmp_path, monkeypatch):
    from app.services.backtest import price_source as ps
    from ba2_common.core import shared_arrays as SA
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    ps.clear_worker_bar_cache()
    src = _source(_native_tree(tmp_path, monkeypatch))
    src.preload(["AAA", "BBB"], datetime(2024, 3, 1), datetime(2024, 12, 31), warmup_days=30)
    assert isinstance(src._keys["AAA"], array) and src._keys["AAA"].typecode == "q"
    assert src._c["AAA"].base is not None and type(src._c["AAA"]) is np.ndarray
    d = Path(SA.derived_root_for(str(tmp_path / "FMPOHLCVProvider")))
    assert any(d.rglob("close.npy"))


def test_shared_and_private_preload_are_bit_identical(tmp_path, monkeypatch):
    from app.services.backtest import price_source as ps
    got = {}
    for flag in ("0", "1"):
        monkeypatch.setenv("BA2_SHARED_ARRAYS", flag)
        ps.clear_worker_bar_cache()
        src = _source(_native_tree(tmp_path, monkeypatch))
        src.preload(["AAA", "BBB"], datetime(2024, 3, 1), datetime(2024, 12, 31), warmup_days=30)
        got[flag] = {s: (list(src._keys[s]), src._o[s].tolist(), src._h[s].tolist(),
                         src._l[s].tolist(), src._c[s].tolist(), src._v[s].tolist())
                     for s in ("AAA", "BBB")}
    assert got["0"] == got["1"]


def test_hermetic_miss_still_raises_and_writes_no_derived_entry(tmp_path, monkeypatch):
    ...  # preload a symbol with no parquet -> BacktestCacheMiss path unchanged (see
         # test_missing_symbol_tolerance.py); assert no dir under _derived for it.


def test_memory_stats_reports_shared_bytes_separately(tmp_path, monkeypatch):
    from app.services.backtest import price_source as ps
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    ps.clear_worker_bar_cache()
    src = _source(_native_tree(tmp_path, monkeypatch))
    src.preload(["AAA"], datetime(2024, 3, 1), datetime(2024, 12, 31), warmup_days=30)
    st = ps.memory_stats()["bar_cache"]
    assert st["shared_mb"] > 0 and st["mb"] < st["shared_mb"] + st["mb"]  # private part is keys only
```

**Step 2: Run** — Expected: failures (`shared_mb` missing; `.base is None`).

**Step 3: Implement**

* Extract from `load_bars_df` a module-level pure function:
  ```python
  def _ohlcv_arrays_from_df(df, intraday: bool) -> Dict[str, np.ndarray]:
      """The sorted/deduped columnar form of one symbol's frame (the numeric half of _store).
      Returns {"keys_ns": int64, "o","h","l","c","v": float64}. Pure -- safe to run in a
      build_fn and identical to what _store produced."""
  ```
  containing the datetime normalisation from `load_bars_df` and the argsort/dedup from `_store`, ending with `keys_ns = np.ascontiguousarray(keys64.astype("datetime64[D]" if not intraday else "datetime64[ns]").astype("datetime64[ns]").astype(np.int64))` (exactly what `_keys64_from_datetime64` does before `frombytes`).
* `_store` / `load_bars_df` become thin: build the dict, then `_bind_arrays(symbol, arrays)`:
  ```python
  def _bind_arrays(self, symbol, a):
      k = array("q"); k.frombytes(np.ascontiguousarray(a["keys_ns"]).tobytes())   # private copy
      self._keys[symbol] = k
      self._o[symbol], self._h[symbol], self._l[symbol] = a["o"], a["h"], a["l"]
      self._c[symbol], self._v[symbol] = a["c"], a["v"]
  ```
* In `preload`'s miss branch, replace `self.load_bars_df(sym, df)` with:
  ```python
  arrays = self._shared_or_private_arrays(sym, win, fetch_start, end)
  self._bind_arrays(sym, arrays)
  ```
  where
  ```python
  def _shared_or_private_arrays(self, sym, win, fetch_start, end):
      from ba2_common.core import shared_arrays as _sa
      src_path = self._native_parquet_path(sym)          # via _ohlcv._read_cached_path if present
      def _build():
          df = self._read_window(sym, fetch_start, end)   # the existing _reader/get_ohlcv_data logic
          return _ohlcv_arrays_from_df(df, self._intraday)
      if src_path is None:                                # fixture provider / no native file
          return _build()
      derived = _sa.DerivedArrayStore(_sa.derived_root_for(os.path.dirname(src_path)))
      key = f"{sym}_{self._interval}_{win[1][:10]}_{win[2][:10]}"
      return derived.build_or_open(key, [src_path], _build)
  ```
  `MemoizedOHLCVProvider` gains `cached_path(symbol, interval) -> Optional[str]` (the `find_timeseries_path` lookup already inside `_read_cached_df`), and `AsOfPriceSource._native_parquet_path` calls it when present (`getattr`, like `read_window`). `BacktestCacheMiss` raised inside `_build` propagates through `build_or_open` (Task 1 guarantees nothing is written).
* `memory_stats`: split `bar_bytes` into `private` (keys + arrays whose `.base is None`) and `shared` (arrays with a `.base`); return `"mb"` = private (what the governor's per-process reasoning cares about) and add `"shared_mb"`. Update the `mem gen` log line in `strategy_optimization_handler.py:788-796` to print `bars ... {mb}MB (+{shared_mb}MB shared)`.
* **No per-individual flush when shared (operator decision 2026-09-14: "as now data is shared, it should persist across the whole GA job and not flush since it is pretty much static").** In `preload`: the `_flush_bar_cache_for_new_individual()` call and the end-of-preload `_evict_bar_cache_by_recency()` call are both skipped when `shared_arrays.enabled()`; the `BT_BAR_CACHE_MAX` count backstop and the governor's explicit `_worker_release_memory` stay. `BT_BAR_CACHE_TRIALS` keeps its meaning ONLY for the private path (`BA2_SHARED_ARRAYS=0`). Add a helper `_bar_cache_persists() -> bool` (= `shared_arrays.enabled()`) used at both sites and by `memory_stats` (report `"mode": "persistent" | "flush" | "recency"`). Update the `_WORKER_BAR_CACHE_TRIALS` / `_flush_bar_cache_for_new_individual` docstrings: the flush existed to bound the PEAK of PRIVATE arrays; with the columns mapped the private part is the keys (8 B/bar, ~1 GB for the largest screener band) and the mapped pages are reclaimable by the OS, so the peak argument no longer applies.
* Tests: `test_bar_cache_recency_eviction.py` pins the PRIVATE flush/recency semantics — add an autouse fixture there that sets `BA2_SHARED_ARRAYS=0` (those tests keep guarding the escape-hatch path). In the new `test_bar_cache_shared.py` add `test_shared_mode_never_flushes_between_individuals` (two preloads with disjoint symbol sets -> both sets remain in `_WORKER_BAR_CACHE`, `read_window` called once per symbol) and `test_shared_mode_still_honours_the_count_backstop` (`BT_BAR_CACHE_MAX=1` via monkeypatch -> oldest evicted).

**Step 4: Run** (separately): `tests/backtest/test_bar_cache_shared.py`, `test_bar_cache_recency_eviction.py`, `test_ohlcv_memo_eviction.py`, `test_preload_timing.py`, `test_price_source_memory_stats.py`, `test_intraday_price_source.py`, `test_missing_symbol_tolerance.py`, `test_hermetic_ohlcv_cache.py`, `test_hermetic_cache_miss_loud.py`, `test_daily_engine_unit.py`. Expected: all pass.

**Step 5: Commit** — `git commit -am "feat(price-source): OHLCV columns mapped from the host-shared derived cache, keys stay private"`

---

### Task 6: Worker-side hooks and telemetry

**Files:**
- Modify: `strategy_optimization_handler.py:312-352` (`_worker_release_memory`), `:788-796` (log line), `:67` (`_WORKER_ENV_KEYS`)
- Test: `testplatform/backend/tests/test_worker_release_keeps_derived_files.py` (new)

**Step 1: Failing test**

```python
def test_release_drops_views_but_keeps_the_derived_files(tmp_path, monkeypatch):
    """The governor's release hook must free the PROCESS's memory, not the HOST's cache."""
    ...  # build a derived set via price_source preload (Task 5 fixture), call
         # _worker_release_memory(), assert _WORKER_BAR_CACHE is empty AND the .npy files exist
         # AND a following preload does not call read_window (count it).


def test_shared_arrays_env_flag_reaches_spawned_workers():
    from app.services import strategy_optimization_handler as H
    assert "BA2_SHARED_ARRAYS" in H._WORKER_ENV_KEYS
```

**Step 2/3:** add `"BA2_SHARED_ARRAYS"` and `"BA2_SHARED_ARRAYS_LOCK_STALE_S"` to `_WORKER_ENV_KEYS`; extend `_worker_release_memory`'s docstring ("drops the views; the mapped files are the host's and stay"); print `shared_mb` in the `mem gen` line (Task 5 already added the field).

**Step 4: Run** the new test + `tests/test_memory_governor.py`, `tests/test_governor_single_actuator.py`. **Step 5: Commit** — `git commit -am "feat(workers): shared-array env reaches workers; release hook keeps the derived files; telemetry shows shared MB"`

---

### Task 7: Prewarm tool — build the derived cache before a grid launches

**MANDATORY before any cold grid, not optional (Task 4 review, 2026-09-14).** `build_or_open` serialises builders per KEY only; there is no host-wide cap. The build transient is ~2.3x the pandas frame (measured ~835 B/row: TastyTrade TSLA 1.48M rows peaks 1.24 GB; ThetaData TSLA 7.6M rows ~7-8 GB). Twenty-four workers cold-starting on DIFFERENT keys is 150-190 GB of transient on remote227 -- the OOM this plan exists to remove, relocated to cold start -- and 12-42 GB on a 32 GB / 6-worker box. In lockstep they instead serialise into a 10-20 min cold start with 23 workers idling. So: run this tool to completion at `--jobs 3-4` before launching, on every host that will run trials. Also: an `ARRAYS_VERSION` bump changes the key, so old `*.v<n>` dirs are never swept by the per-key logic -- `--sweep` must also remove version dirs older than the current one. Never re-warm a source tree mid-grid on Windows: the old derived set stays mapped and both coexist until the grid exits.

**Files:**
- Create: `tools/build_shared_arrays.py`
- Test: `testplatform/backend/tests/test_build_shared_arrays_tool.py`

**Behaviour:** `python tools/build_shared_arrays.py --options-store thetadata --universe-file tools/options_universe_top100.txt [--ohlcv-provider FMPOHLCVProvider --interval 1d --start 2020-01-01 --end 2025-12-31 --warmup-days 60] [--jobs 4] [--sweep]`. For options: resolve the root via `options_store.default_options_parquet_root(store)`, and for each symbol call `parquet_options_provider._load_raw_underlying(root, sym)` in a small process pool (each call builds or opens; ignore the returned object). For OHLCV: construct the same `MemoizedOHLCVProvider` + `AsOfPriceSource` the backtest uses and call `preload` once for the universe/window (that builds every entry). `--sweep` runs `DerivedArrayStore.sweep()` on both roots AND a key-level pass (Task 5 review): `sweep()` only collects superseded signatures WITHIN a key, but the OHLCV key carries the window (`u_<SYM>_<interval>_<from>_<to>_v<n>_<winsig>`) and both consumers carry an `ARRAYS_VERSION`, so a long-lived remote worker accumulates one full set (~48 B/bar; 5.6 GB per 116M-bar band) per (window, version) that nothing ever removes. Add `--sweep-max-age-days N` (default 14): remove any KEY directory whose newest marker is older than N days, and any key whose `.v<n>` is below the consumer's current `ARRAYS_VERSION` (`_RawUnderlying.ARRAYS_VERSION`, `price_source.ARRAYS_VERSION`). Eviction goes through `_evict_dir` semantics (never delete a mapped file; skip and report). Print bytes reclaimed. Note in the tool's docstring that nothing calls `sweep()` at runtime -- this tool is the only collector, so schedule it between grids on every worker host. Prints per-symbol build/open and a total. Test with the Task 4/5 fixtures: run `main([...])` on a tmp tree, assert the `.npy` sets exist, run again, assert `read_underlying` was not called.

**Commit** — `git commit -am "feat(tools): build_shared_arrays prewarms the derived cache for a grid"`

---

### Task 8: Screener metric store — spike with a decision gate (do NOT skip the gate)

`_STORE_MEMO` (`packages/providers/ba2_providers/screener/metric_store.py:699,766-780`) is one concatenated DataFrame with string columns (`symbol`, `date`). `pd.DataFrame(dict_of_arrays)` consolidates same-dtype columns into 2-D blocks (a COPY), so mapping columns does not automatically share a DataFrame. Before writing code, the implementer must:

1. Measure the store's dtype composition and the hot consumers (`FactorRanker/__init__.py:412,617`, `scan_dates`, and the per-bar screen in `ba2test_launcher._screener_gate_opt_block` / `daily_engine`): which columns are read per bar, and whether they are read via DataFrame ops or could be read from arrays.
2. Verify with `np.shares_memory` whether `pd.DataFrame({...}, copy=False)` on this pandas version keeps a mapped float64 column un-copied (single-column-per-block only, in pandas 2.x with CoW).
3. Write findings to `reports/strategy_research/metric_store_sharing_spike_2026-09-14.md` with ONE recommendation: (a) share numeric columns + categorical codes for strings and rebuild the frame per process with `copy=False`, or (b) keep the memo private and instead lower per-worker cost by loading only the run's window (`ym=` partitions in range) — which the store's monthly layout already supports.

Stop there and report; the metric store implementation is a follow-on plan once the recommendation is approved. (Rationale: the option grid is the blocker; the metric store is a screener-grid concern with a different access pattern.)

---

### Task 9a: ACCEPTANCE — byte-for-byte comparison of known backtests, private vs shared

**Operator acceptance criterion (2026-09-14): "ensure byte comparison of known backtest with new shared cache."** Nothing ships to remote227 until this passes.

**Files:**
- Create: `tools/backtest_parity.py`
- Test: `testplatform/backend/tests/test_backtest_parity_tool.py`

**What the tool does.** `python tools/backtest_parity.py --opt <id> [--rank best|<n>] [--bt <id>]` re-runs ONE known genome twice — once with `BA2_SHARED_ARRAYS=0` (today's private path) and once with `=1` — each in its OWN subprocess (the worker caches are process globals, so two modes cannot share a process), persists each as a NEW Backtest named `PARITY-<mode>-<original name>` with labels `["parity", "<mode>"]` (the source row is never touched; refuse to run if the names already exist), then compares the two rows and prints `PASS` or the first difference and exits non-zero on `FAIL`.

Base the re-run on the existing scratch pattern (it is the same machinery the OK1000/OK2000 rows were produced with):
```python
# child process: python tools/backtest_parity.py --_child <mode> --opt <id> --rank <r> --name <NAME>
import logging; logging.disable(logging.WARNING)          # memory: standalone runs must silence logging
import sys; sys.path.insert(0, "<repo>/testplatform"); import ba2test_launcher as L; L._enter_backend()
import app.models  # noqa
from app.models.database import SessionLocal
from app.models.backtest import Backtest
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization
from app.services.strategy_optimization_handler import _build_daily_trial_config, _persist_trial_worker
from app.services.strategy_param_space import decode_params
from app.services.backtest.daily_backtest_handler import _persist_results

db = SessionLocal()
opt = db.query(StrategyOptimization).get(opt_id); strat = db.query(Strategy).get(opt.strategy_id)
genome = opt.best_params if rank == "best" else <rank-th of opt.all_results by distinct fitness, as tools/recover_missing_topn.py does>
bt_block = dict((opt.optimization_config or {})["backtest"])
cfg = _build_daily_trial_config(bt_block, decode_params(strat, genome), None)
cfg["name"] = NAME; cfg["persist_trading_db"] = True; cfg["ga_fitness"] = opt.best_fitness
out = _persist_trial_worker(cfg)                       # runs the backtest in THIS process
bt = Backtest(name=NAME, model_id=None, engine_type="daily_expert", expert_name=opt.expert_name,
              optimization_id=opt.id, labels=["parity", mode], strategy_params=..., start_date=..., end_date=...,
              initial_capital=bt_block["initial_capital"], status="running", started_at=now)
db.add(bt); db.commit(); db.refresh(bt); _persist_results(db, bt, out["results"]); bt.status="completed"; bt.is_saved=True; db.commit()
print(bt.id)
```
The parent runs the child twice with `env={**os.environ, "BA2_SHARED_ARRAYS": mode}` and the test venv python, reads the two ids, then compares.

**Comparison (`compare_rows(a, b) -> list[str]` — importable, unit-tested):**
* Blob columns `results`, `trades`, `equity_curve`, `drawdown_curve`: parse JSON, canonicalise with `json.dumps(obj, sort_keys=True, separators=(",", ":"))`, compare the canonical STRINGS byte-for-byte. Strip only keys that are run identity, not results: `name`, `id`, `backtest_id`, `created_at`, `started_at`, `completed_at`, `run_seconds`/timing fields — enumerate them explicitly in a `_IDENTITY_KEYS` tuple with a comment per key; anything else that differs is a FAIL.
* Numeric columns: every column in `Backtest.__table__.columns` of Float/Integer type except `id`, `optimization_id`, `model_id`, `strategy_id` — compared with `==` (exact, NOT tolerance: the point is bit-identity; NaN==NaN counts as equal).
* Report the first differing key path (e.g. `trades[17].exit_price: 12.34 != 12.35`) and the total count of differing paths.

**Reference runs (record ids + verdicts in the bench report under "Acceptance"):**
1. Equity, OHLCV-heavy with the screener gate: the opt behind bt **1681** (`TOP1-scr-small-FMPInsiderClusterBuy-S7-goal2020-notional`, small-cap screener, 2020 window) — exercises Task 5 (bars) and the metric store path. `--rank 1`.
2. Equity, Senate: the opt behind bt **1680** / **1645** (`sen-S6-goal2020-notional`) — exercises bars + FMP history through a different expert. `--rank best`.
3. Options on ThetaData at 2020: the TOP1 row of the GA probe from Task 9 step 4 (pop 8 / gen 2 on the 98-symbol universe, `--options-store thetadata --start 2020-01-01`) — exercises Task 4. `--rank 1`.
4. Options on the TastyTrade store (the archive baseline): any persisted `optm-*` row from the options-grid2 era (2026-08-25..09-03) whose config carries `options_store: "parquet"` — proves the alias + the tastytrade tree still read identically. `--rank 1`.
All four must print PASS. A FAIL is a blocker, not a tolerance discussion: the shared path must produce the SAME bytes, or the difference must be traced to a genuine bug in the private path and fixed there first.

**Unit test for the tool** (`test_backtest_parity_tool.py`): build two fake row objects with identical blobs -> `[]`; change one trade's `exit_price` -> exactly one path reported naming `trades[..].exit_price`; reorder keys inside `results` -> `[]` (canonicalisation); NaN metric on both -> equal; different `created_at` -> `[]` (identity key).

**Commit** — `git commit -am "feat(tools): backtest_parity re-runs a known genome private vs shared and compares byte-for-byte"`

---

### Task 9: Full regression + Windows/Linux sanity on the real trees

1. Backend suite: `cd testplatform/backend && ...pytest tests/backtest -q -p no:cacheprovider` — Expected: baseline 1,169 passed + the new tests, 1 skipped, 2 xfailed, zero new failures. Then `tests/test_cache_sync.py`, `tests/test_memory_governor.py`, `tests/test_option_discovery_driver.py`.
2. packages/common suite: `cd packages/common && ...pytest tests -q` (separate invocation).
3. Real-tree sanity (Windows, from repo root, test venv): `python tools/build_shared_arrays.py --options-store thetadata --universe-file tools/options_universe_top100.txt --jobs 4` twice — the second run must report 98 opened / 0 built, and `du -sh ~/Documents/ba2/common/cache/_derived/ThetaDataOptionsProvider` ≈ 3x the parquet (~10 GB).
4. Measure: run the GA probe shape from memory `option-grid-thetadata-2020-and-cache-followups` (pop 8 / gen 2 / parallel 4, the 98-symbol universe, `--options-store thetadata --start 2020-01-01`) and record per-worker RSS and USS from the `mem gen` lines / `psutil` — expected: USS per worker drops from ~10-16 GB to ~1.5-2.5 GB (the `bar_ord_l` list + contract lists + trial working set); trial seconds within 10% of the 267-444 s measured on remote227.
5. Record the numbers in `reports/strategy_research/option_array_sharing_bench_2026-09-14.md` under a "Post-implementation" heading, then run Task 9a's four reference parity runs (the options one uses this probe's TOP1) and record their ids and PASS lines under "Acceptance".

---

### Task 10: Bump, push, relaunch stage 1 on remote227

0. The `TEST_APP_VERSION` bump is load-bearing for the cache-sync change: `POST /workers/{id}/sync-cache` has no `ensure_synced`, so never use it against a worker still on pre-12ed8c74 code (its manifest would still list `_derived`, and the master's would not — `diff_stale` then prunes that worker's derived cache on every push).
1. `packages/` and `testplatform/` changed -> bump `testplatform/version.py` `TEST_APP_VERSION` 0033 -> 0034. Commit: `chore: TEST_APP_VERSION 0033 -> 0034 (shared memory-mapped arrays across workers)`. Push `dev`.
2. On remote227 (`debian@141.94.199.227`): `cd /home/debian/ba2-grid/repo && git fetch https://github.com/bmigette/BA2TradePlatform.git dev && git checkout -q -B stage1-2020 FETCH_HEAD`; then prewarm (MANDATORY, `--jobs 4` at most -- see Task 7; do not launch the unit until it reports 98 opened / 0 built on a second run): `PYTHONPATH=<the four paths from tools/stage1_run.sh> BA2_HOME=/home/debian/ba2-grid/home /opt/ba2worker/ba2-venvs/test/bin/python tools/build_shared_arrays.py --options-store thetadata --universe-file tools/options_universe_top100.txt --ohlcv-provider FMPOHLCVProvider --interval 1d --start 2020-01-01 --end 2025-12-31 --warmup-days 60 --jobs 8`.
3. `tools/stage1_run.sh`: set `PARALLEL` default to **24** and note the shared-array basis in its header; relaunch exactly as before (`sudo systemd-run --unit=ba2-stage1 ... -p MemoryMax=232G`), then verify after 10 minutes that per-child USS (`ps -o pid,rss` is misleading for mapped pages — use `/proc/<pid>/smaps_rollup` `Private_Dirty`+`Private_Clean`) sits near 2 GB and the cgroup total is far below the cap. Record in memory `option-stage1-run-2026-09-14`.

---

## Out of scope (recorded, not built here)

* `price_source.py` ~:761 — the tolerated-cache-miss `logger.warning` ("Never let this be quiet") is child-blind for the same reason as Task 5's I13: `_worker_init` installs `logging.disable(ERROR)`, so in a GA worker it is quiet. Switch it to `_worker_log` in a follow-up (it also records `_dropped_symbols`, so it is not wholly silent today).

* Senate scoring shards (`_WORKER_SCORING_CACHE`): read-modify-write during a job; needs a write path. Separate design.
* `_FULL_SERIES_MEMO` (full-series DataFrames for expert indicator fetches): lazily filled per expert request; revisit after Task 8's finding on DataFrame sharing.
* `BT_BAR_CACHE_TRIALS` is now relevant only to the private path (see Task 5); no further change.
* float32 prices / dropping open-high-low residency: a further ~40% on the SHARED footprint; only matters once the shared copy itself is the constraint.
