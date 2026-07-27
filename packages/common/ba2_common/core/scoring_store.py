"""Columnar, memory-mappable store for fixed-schema scoring caches.

WHY (2026-07-27, Senate S4 / opt 224). ``FMPSenateTraderWeight._load_scoring_cache`` reads each
congress scoring cache with ``json.load`` into ``_WORKER_SCORING_CACHE`` -- a MODULE-level dict,
therefore one full copy PER POOL CHILD. Measured on remote150:

    skill       3,406,775 entries   452MB on disk -> 2.22GB in RAM
    confidence  1,778,046 entries   657MB on disk -> 2.95GB in RAM
                                     1.1GB        -> 5.17GB per process

Four children => ~20.7GB of a 64GB box holding four identical copies of the same read-only
table. That is what drove remote150 to 99.2% memory; trials then swapped past the master's
fixed 1800s budget and were abandoned mid-flight (six timeouts, all in S4).

The blow-up is representational, not informational: JSON -> Python expands 4.7x because every
entry becomes a ~50-char ``str`` key (~104B) plus a 4- or 12-key ``dict`` of floats (~547B /
~1557B), to carry what is really 4 or 12 numbers. The values have a FIXED schema, so a columnar
layout stores exactly those numbers.

DESIGN
    base     immutable, mmapped, sorted by key hash. Shared across every process that opens the
             same path via the OS page cache -- the resident cost is paid ONCE FOR THE BOX.
    overlay  a small private ``dict`` for entries written after open. Lookups check it first.

Writes never touch the base, so the shared mapping stays valid for other processes; this
preserves the existing append-only delta-file semantics in ``_save_scoring_cache_throttled``
unchanged.

Keys are stored in full and compared byte-for-byte on lookup. A 64-bit hash alone would collide
across 3.4M keys with probability ~3e-7 -- small, but a wrong skill score is a SILENT
correctness bug (the GA would score a different strategy than it reports), and the key blob
costs ~163MB against a ~4.9GB saving.

ON-DISK LAYOUT (one directory per cache)
    meta.json              {"fields", "dtypes", "nullable", "n"}
    keys.blob              all keys, UTF-8, concatenated
    offsets.npy            uint64[n+1], byte offsets into keys.blob
    hashes.npy             uint64[n], ASCENDING -- the lookup index
    col_<field>.npy        int64[n] or float64[n]
    col_<field>_isnull.npy bool[n], only for nullable fields
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import MutableMapping
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Sequence, Tuple

import numpy as np

_META = "meta.json"
_KEYS = "keys.blob"
_OFFSETS = "offsets.npy"
_HASHES = "hashes.npy"


def _hash_key(key: str) -> np.uint64:
    """Stable 64-bit key hash.

    Deliberately NOT the builtin ``hash()``: PYTHONHASHSEED randomises str hashing per process,
    so a base built by one process would be unreadable (silently: wrong bucket -> lookup miss ->
    recompute) by the next. blake2b with a fixed digest size is stable across processes and
    releases. Module-level so tests can monkeypatch it to force collisions.
    """
    d = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return np.uint64(int.from_bytes(d, "little"))


def _infer_dtype(values: Iterable[Any]) -> str:
    """int64 only if every non-None value is a real int; else float64.

    ``bool`` is excluded explicitly -- it is an ``int`` subclass, and silently widening a flag
    to 0/1 int64 would round-trip as ``int`` instead of ``bool`` and change caller branching.
    """
    seen = False
    for v in values:
        if v is None:
            continue
        seen = True
        if isinstance(v, bool) or not isinstance(v, int):
            return "f8"
    return "i8" if seen else "f8"


class ScoringStore(MutableMapping):
    """Drop-in for the plain ``dict[str, dict]`` scoring cache.

    Implements exactly what the call sites in ``FMPSenateTraderWeight`` use -- ``get``,
    ``__setitem__``, and iteration/``items`` for the compacting ``json.dump`` flush -- plus the
    rest of ``MutableMapping`` for free.
    """

    # ------------------------------------------------------------------ build
    @staticmethod
    def build(path, mapping: Mapping[str, Mapping[str, Any]],
              fields: Sequence[str], nullable: Sequence[str] = ()) -> None:
        """Write *mapping* to *path* as a columnar store. Overwrites any existing store."""
        path = str(path)
        os.makedirs(path, exist_ok=True)
        items = list(mapping.items())

        # Sort by (hash, key) so the hash column is ascending for searchsorted, and equal-hash
        # runs have a deterministic order (reproducible builds; easier diffing).
        items.sort(key=lambda kv: (int(_hash_key(kv[0])), kv[0]))
        n = len(items)

        # A field is nullable if declared OR observed to contain None. Declaring matters even
        # when this batch has no None: the value may legitimately be None later.
        null_fields = set(nullable)
        for _k, v in items:
            for f in fields:
                if v.get(f) is None:
                    null_fields.add(f)

        dtypes = {f: _infer_dtype(v.get(f) for _k, v in items) for f in fields}

        blob = bytearray()
        offsets = np.zeros(n + 1, dtype=np.uint64)
        hashes = np.zeros(n, dtype=np.uint64)
        cols = {f: np.zeros(n, dtype=np.dtype(dtypes[f])) for f in fields}
        nulls = {f: np.zeros(n, dtype=bool) for f in null_fields if f in fields}

        for i, (k, v) in enumerate(items):
            blob += k.encode("utf-8")
            offsets[i + 1] = len(blob)
            hashes[i] = _hash_key(k)
            for f in fields:
                val = v.get(f)
                if val is None:
                    if f in nulls:
                        nulls[f][i] = True
                    continue  # leave the column at 0; the mask is authoritative
                cols[f][i] = val

        with open(os.path.join(path, _KEYS), "wb") as fh:
            fh.write(bytes(blob))
        np.save(os.path.join(path, _OFFSETS), offsets)
        np.save(os.path.join(path, _HASHES), hashes)
        for f, arr in cols.items():
            np.save(os.path.join(path, f"col_{f}.npy"), arr)
        for f, arr in nulls.items():
            np.save(os.path.join(path, f"col_{f}_isnull.npy"), arr)

        with open(os.path.join(path, _META), "w", encoding="utf-8") as fh:
            json.dump({"fields": list(fields), "dtypes": dtypes,
                       "nullable": sorted(null_fields & set(fields)), "n": n}, fh)

    # ------------------------------------------------------------------- open
    @classmethod
    def open(cls, path) -> "ScoringStore":
        return cls(str(path))

    def __init__(self, path: str):
        self._path = path
        with open(os.path.join(path, _META), "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        self._fields: list = meta["fields"]
        self._dtypes: dict = meta["dtypes"]
        self._nullable = set(meta["nullable"])
        self._n: int = meta["n"]
        self._overlay: Dict[str, Dict[str, Any]] = {}

        if self._n == 0:
            # np.memmap cannot map a zero-length file; an empty store has no pages to share
            # anyway, so plain empty arrays are equivalent and avoid a special case downstream.
            self._blob = np.empty(0, dtype=np.uint8)
            self._offsets = np.zeros(1, dtype=np.uint64)
            self._hashes = np.empty(0, dtype=np.uint64)
            self._cols = {f: np.empty(0, dtype=np.dtype(self._dtypes[f])) for f in self._fields}
            self._nulls = {}
            return

        self._blob = np.memmap(os.path.join(path, _KEYS), dtype=np.uint8, mode="r")
        self._offsets = np.load(os.path.join(path, _OFFSETS), mmap_mode="r")
        self._hashes = np.load(os.path.join(path, _HASHES), mmap_mode="r")
        self._cols = {f: np.load(os.path.join(path, f"col_{f}.npy"), mmap_mode="r")
                      for f in self._fields}
        self._nulls = {f: np.load(os.path.join(path, f"col_{f}_isnull.npy"), mmap_mode="r")
                       for f in self._nullable}

    # ------------------------------------------------------------------ lookup
    def _row(self, key: str) -> Optional[int]:
        """Row index for *key*, or None. Resolves hash collisions by exact key comparison."""
        if self._n == 0:
            return None
        h = _hash_key(key)
        lo = int(np.searchsorted(self._hashes, h, side="left"))
        hi = int(np.searchsorted(self._hashes, h, side="right"))
        if lo == hi:
            return None
        raw = key.encode("utf-8")
        for i in range(lo, hi):
            a, b = int(self._offsets[i]), int(self._offsets[i + 1])
            if bytes(self._blob[a:b]) == raw:
                return i
        return None

    def _materialise(self, i: int) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for f in self._fields:
            mask = self._nulls.get(f)
            if mask is not None and bool(mask[i]):
                out[f] = None
                continue
            # Cast to a Python scalar: numpy types are not JSON-serialisable, and the
            # compacting flush does json.dump() straight off this mapping.
            out[f] = int(self._cols[f][i]) if self._dtypes[f] == "i8" else float(self._cols[f][i])
        return out

    # --------------------------------------------------------- Mapping protocol
    def get(self, key: str, default: Any = None) -> Any:
        hit = self._overlay.get(key)
        if hit is not None:
            return hit
        i = self._row(key)
        return default if i is None else self._materialise(i)

    def __getitem__(self, key: str) -> Dict[str, Any]:
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v

    def __setitem__(self, key: str, value: Mapping[str, Any]) -> None:
        # Always into the overlay: the base is mmapped and may be shared with other processes.
        self._overlay[key] = dict(value)

    def __delitem__(self, key: str) -> None:
        if key in self._overlay:
            del self._overlay[key]
            return
        if self._row(key) is not None:
            raise NotImplementedError(
                "ScoringStore base entries are immutable; rebuild the store to drop keys")
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return key in self._overlay or self._row(key) is not None

    def __iter__(self) -> Iterator[str]:
        seen = set()
        for k in self._overlay:
            seen.add(k)
            yield k
        for i in range(self._n):
            a, b = int(self._offsets[i]), int(self._offsets[i + 1])
            k = bytes(self._blob[a:b]).decode("utf-8")
            if k not in seen:
                yield k

    def __len__(self) -> int:
        # Overlay keys that shadow a base key must not be double-counted.
        return sum(1 for _ in iter(self))

    def items(self) -> Iterable[Tuple[str, Dict[str, Any]]]:  # type: ignore[override]
        for k in self:
            yield k, self.get(k)
