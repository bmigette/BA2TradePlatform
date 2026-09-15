"""Central immutable market-condition feature store: objects, manifests, identity, reading.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` sections 4.2-4.3 and
4.6; implementation plan Task 6.

LAYOUT under ``<cache_root>/market_conditions/`` (ordinary syncable cache data -- NOT ``_derived``;
``cache_sync`` mirrors the bucket, remote hosts never recompute):

* ``raw/<sha256>.parquet`` -- normalized bar shards, one per (symbol, calendar month): ``session``
  (date32) + ``open/high/low/close/volume`` (float64), sorted, deduplicated. SHARED by every
  profile (the bars do not depend on what is computed from them) and deduplicated by content.
* ``<profile>/objects/<sha256>.parquet`` -- feature shards, rows = one symbol x sessions of one
  calendar month. Columns, DECLARED from the profile's registry (never inferred, so an all-invalid
  column is still float64): ``symbol``, ``session`` (date32), one float64 column per field in
  registry order (a categorical field holds its integer code), one int8 ``<field>_status`` per field
  (index into ``STATUSES``), one ``<field>_reason`` string per field, ``window_digest``
  (``sha256:<hex>``), ``raw_shard_ref``, ``raw_row_lo``, ``raw_row_hi``.
  The ``<field>_reason`` columns are an ADDITION to the column list in the design (section 4.3):
  an ``Observation`` carries ``(value, status, reason)``, so without them a stored row could not
  be rebuilt into the row the calculators produce, and "why is this symbol unknown today" would
  survive only in the manifest's aggregate coverage exceptions. They are dictionary-encoded and
  empty for every valid row, so they cost almost nothing.
  A (symbol, month) may be covered by several objects (an extension publishes a delta object and
  reuses the old one by hash); a (symbol, session) appears in exactly one object of a manifest.
* ``<profile>/manifests/<sha256>.json`` -- the exact object list plus the pinned definition
  versions. The file name is the manifest's PORTABLE IDENTITY: sha256 of the canonical JSON
  without ``created_at`` (no machine paths, mtimes or job names inside).

OBJECTS ARE IMMUTABLE AND CONTENT-ADDRESSED: bytes are serialized in memory, named by their
sha256, written to a ``.part`` file and ``os.replace``-d into place. An object that already exists
with the right hash is reused, never rewritten.

RAW WINDOW REFERENCES. A feature row's 128-session window spans several monthly raw shards.
``raw_shard_ref`` is the ``;``-joined, chronologically ordered list of those shards' sha256 hex
names; ``raw_row_lo``/``raw_row_hi`` index (``[lo, hi)``) the CONCATENATION of their rows. For a
valid row the slice is exactly the window, and its normalized bytes hash to ``window_digest``
(``market_condition_source.window_digest``), which ``retained_window`` re-checks. A row whose window
could not be assembled carries the bars that DO exist in its span and an
``unavailable_window_digest`` over them (dates included), so a later bar changes its identity and
invalidates it.

This module does no provider access and computes no indicator.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from ba2_common.core.market_condition_source import (
    normalized_window_bytes,
    window_digest_of_bytes,
    window_from_bytes,
)
from ba2_common.core.market_conditions import (
    PROFILES,
    STATUS_VALID,
    STATUSES,
    FeatureRow,
    Observation,
    ProfileSpec,
)

__all__ = [
    "MC_DIRNAME",
    "RAW_DIRNAME",
    "SCHEMA_VERSION",
    "MANIFEST_KEYS",
    "ManifestError",
    "ManifestConflictError",
    "ObjectEntry",
    "RawEntry",
    "RowsFrame",
    "VerifyReport",
    "MarketConditionStore",
    "feature_schema",
    "raw_schema",
    "manifest_identity",
    "canonical_manifest_json",
    "universe_digest",
    "sessions_digest",
    "calendar_version",
    "unavailable_window_digest",
    "month_of",
    "status_code",
    "sha256_file",
]

MC_DIRNAME = "market_conditions"
RAW_DIRNAME = "raw"
OBJECTS_DIRNAME = "objects"
MANIFESTS_DIRNAME = "manifests"
#: Feature-object / manifest layout version. A change of columns or meaning gets a new number.
SCHEMA_VERSION = 1
_COMPRESSION = "zstd"

MANIFEST_KEYS = (
    "profile", "source_profile", "timing_policy", "calc_version", "calendar_version",
    "schema_version", "fields", "objects", "raw_objects", "coverage", "universe_digest",
    "sessions_digest", "window_start", "window_end", "created_at",
)

_STATUS_CODE = {s: i for i, s in enumerate(STATUSES)}
_RAW_COLUMNS = ("open", "high", "low", "close", "volume")


class ManifestError(ValueError):
    """A manifest that is malformed, inconsistent with its objects, or whose identity is wrong."""


class ManifestConflictError(ManifestError):
    """Two objects of one manifest both carry the same (symbol, session)."""


# ---------------------------------------------------------------------------
# Schemas and small helpers
# ---------------------------------------------------------------------------
def _pa():
    import pyarrow as pa
    return pa


def feature_schema(fields: Sequence[str]):
    """The declared pyarrow schema of a feature object for ``fields`` (registry order)."""
    pa = _pa()
    cols = [pa.field("symbol", pa.string(), nullable=False), pa.field("session", pa.date32(), nullable=False)]
    cols += [pa.field(f, pa.float64(), nullable=False) for f in fields]
    cols += [pa.field(f"{f}_status", pa.int8(), nullable=False) for f in fields]
    cols += [pa.field(f"{f}_reason", pa.string(), nullable=False) for f in fields]
    cols += [
        pa.field("window_digest", pa.string(), nullable=False),
        pa.field("raw_shard_ref", pa.string(), nullable=False),
        pa.field("raw_row_lo", pa.int64(), nullable=False),
        pa.field("raw_row_hi", pa.int64(), nullable=False),
    ]
    return pa.schema(cols)


def raw_schema():
    pa = _pa()
    return pa.schema([pa.field("session", pa.date32(), nullable=False)]
                     + [pa.field(c, pa.float64(), nullable=False) for c in _RAW_COLUMNS])


def status_code(status: str) -> int:
    return _STATUS_CODE[status]


def month_of(session: date) -> str:
    return f"{session.year:04d}-{session.month:02d}"


def calendar_version() -> str:
    import pandas_market_calendars
    return f"pandas_market_calendars/{pandas_market_calendars.__version__}"


def universe_digest(symbols: Iterable[str]) -> str:
    uniq = sorted({s.upper() for s in symbols})
    return "sha256:" + hashlib.sha256("\n".join(uniq).encode("utf-8")).hexdigest()


def sessions_digest(sessions: Iterable[date]) -> str:
    """Identity of the exact set of feature-row sessions a manifest was built for. Provenance:
    two manifests over the same universe and window but a different calendar answer (a holiday
    rule change) differ here, and it is part of the manifest identity."""
    iso = sorted({s.isoformat() for s in sessions})
    return "sha256:" + hashlib.sha256("\n".join(iso).encode("utf-8")).hexdigest()


def unavailable_window_digest(days: np.ndarray, o: Any, h: Any, l: Any, c: Any, v: Any) -> str:
    """Identity of the bars present in an UNASSEMBLABLE window's span: dates and values, so a
    bar appearing, disappearing or changing gives a different identity. A separate domain from
    ``window_digest`` (a prefix), so it can never collide with a valid window's key.

    NOTE: over an EMPTY span (no bars at all) this is the same constant for every symbol and
    session -- an empty span has no content to tell them apart. That is safe because a row is
    only ever reused for the (symbol, session) it is stored under, and any bar arriving changes
    the digest; but it means the digest alone is not a row identity, and ``retained_window`` is
    meaningful only for VALID rows (a window that was never assembled has nothing to retain)."""
    d = np.asarray(days).astype("datetime64[D]").astype("<i8")
    payload = b"mc-unavailable-window/v1\x00" + d.tobytes() + normalized_window_bytes(o, h, l, c, v)
    return window_digest_of_bytes(payload)


def sha256_file(path: os.PathLike, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _ordered_lists(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """The manifest with its object lists in canonical order (objects by symbol, month, hash;
    raw objects by path): list order carries no meaning, so it must not change the identity."""
    body = dict(manifest)
    if isinstance(body.get("objects"), list):
        body["objects"] = sorted(body["objects"], key=lambda o: (o["symbol"], o["month"], o["sha256"]))
    if isinstance(body.get("raw_objects"), list):
        body["raw_objects"] = sorted(body["raw_objects"], key=lambda r: r["path"])
    return body


def canonical_manifest_json(manifest: Mapping[str, Any], *, include_created_at: bool = False) -> str:
    body = {k: v for k, v in _ordered_lists(manifest).items() if include_created_at or k != "created_at"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def manifest_identity(manifest: Mapping[str, Any]) -> str:
    """sha256 hex of the canonical manifest JSON WITHOUT ``created_at`` (dict order irrelevant)."""
    return hashlib.sha256(canonical_manifest_json(manifest).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ObjectEntry:
    path: str          # relative to <cache_root>/market_conditions, forward slashes
    sha256: str
    symbol: str
    month: str
    rows: int

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "symbol": self.symbol,
                "month": self.month, "rows": int(self.rows)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ObjectEntry":
        return cls(path=str(d["path"]), sha256=str(d["sha256"]), symbol=str(d["symbol"]),
                   month=str(d["month"]), rows=int(d["rows"]))


@dataclass(frozen=True)
class RawEntry:
    path: str
    sha256: str
    rows: int

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "rows": int(self.rows)}


@dataclass
class RowsFrame:
    """One symbol's rows of a manifest as arrays, sorted by session."""

    symbol: str
    fields: Tuple[str, ...]
    calc_version: str
    sessions: np.ndarray            # datetime64[D], shape (n,)
    values: np.ndarray              # float64, shape (n, F)
    status: np.ndarray              # int8, shape (n, F) -- index into STATUSES
    reasons: List[Tuple[str, ...]]  # n tuples of F reasons
    window_digests: List[str]
    raw_shard_refs: List[str]
    raw_row_lo: np.ndarray
    raw_row_hi: np.ndarray

    def __len__(self) -> int:
        return int(len(self.sessions))

    def row(self, i: int) -> FeatureRow:
        values = {}
        for j, f in enumerate(self.fields):
            st = STATUSES[int(self.status[i, j])]
            val = float(self.values[i, j]) if st == STATUS_VALID else None
            values[f] = Observation(val, st, self.reasons[i][j])
        return FeatureRow(values=values, calc_versions={f: self.calc_version for f in self.fields})


@dataclass
class VerifyReport:
    manifest_digest: str
    identity_ok: bool
    objects_checked: int = 0
    raw_checked: int = 0
    missing: List[str] = dc_field(default_factory=list)
    corrupt: List[str] = dc_field(default_factory=list)
    errors: List[str] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.identity_ok and not self.missing and not self.corrupt and not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {"manifest": self.manifest_digest, "ok": self.ok, "identity_ok": self.identity_ok,
                "objects_checked": self.objects_checked, "raw_checked": self.raw_checked,
                "missing": list(self.missing), "corrupt": list(self.corrupt), "errors": list(self.errors)}


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class MarketConditionStore:
    """Read/write access to ``<cache_root>/market_conditions``. Thread-safe for concurrent
    publishers (every write is content-addressed + atomic)."""

    def __init__(self, cache_root: os.PathLike):
        self.cache_root = Path(cache_root)
        self.root = self.cache_root / MC_DIRNAME
        self._window_index: Dict[str, Tuple[str, int, int]] = {}
        self._indexed_manifests: set = set()
        self._index_lock = threading.Lock()

    # -- paths
    def abspath(self, rel: str) -> Path:
        return self.root / Path(*rel.split("/"))

    @staticmethod
    def object_rel(profile: str, sha: str) -> str:
        return f"{profile}/{OBJECTS_DIRNAME}/{sha}.parquet"

    @staticmethod
    def raw_rel(sha: str) -> str:
        return f"{RAW_DIRNAME}/{sha}.parquet"

    def manifest_path(self, profile: str, digest: str) -> Path:
        return self.root / profile / MANIFESTS_DIRNAME / f"{digest}.json"

    # -- low-level immutable publication
    def _publish_bytes(self, rel: str, data: bytes, sha: str) -> bool:
        """Place ``data`` at ``rel`` unless a file with the right hash is already there.
        Returns True when an existing verified object was REUSED."""
        final = self.abspath(rel)
        if final.exists():
            try:
                if sha256_file(final) == sha:
                    return True
            except OSError:
                pass
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f"{final.name}.{os.getpid()}.{threading.get_ident()}.part")
        reused = False
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(tmp, final)
            except OSError:
                # Windows refuses to replace a file another process has open, and a concurrent
                # publisher may have put the SAME bytes there between our check and this call.
                # The name is the content, so an existing file that hashes right IS our object.
                if not (final.exists() and sha256_file(final) == sha):
                    raise
                reused = True
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return reused

    @staticmethod
    def _serialize(table) -> bytes:
        import pyarrow.parquet as pq
        pa = _pa()
        sink = pa.BufferOutputStream()
        pq.write_table(table, sink, compression=_COMPRESSION)
        return sink.getvalue().to_pybytes()

    def write_raw_shard(self, sessions: np.ndarray, o, h, l, c, v) -> Tuple[RawEntry, bool]:
        """Publish one raw bar shard (already sorted and unique). Returns (entry, reused)."""
        pa = _pa()
        days = np.asarray(sessions).astype("datetime64[D]")
        if len(days) and not (np.diff(days.astype("<i8")) > 0).all():
            raise ValueError("raw shard sessions must be strictly increasing")
        arrays = [pa.array(days, type=pa.date32())]
        arrays += [pa.array(np.asarray(x, dtype=np.float64), type=pa.float64()) for x in (o, h, l, c, v)]
        table = pa.Table.from_arrays(arrays, schema=raw_schema())
        data = self._serialize(table)
        sha = hashlib.sha256(data).hexdigest()
        rel = self.raw_rel(sha)
        reused = self._publish_bytes(rel, data, sha)
        return RawEntry(path=rel, sha256=sha, rows=len(days)), reused

    def feature_table(self, fields: Sequence[str], symbol: str, rows: Sequence[Mapping[str, Any]]):
        """Build a feature table with the DECLARED schema. Each row mapping holds ``session``
        (date), ``values`` (per-field float or None), ``status`` (per-field status string),
        ``reasons`` (per-field str), ``window_digest``, ``raw_shard_ref``, ``raw_row_lo``,
        ``raw_row_hi``. Rows are written sorted by session."""
        pa = _pa()
        rows = sorted(rows, key=lambda r: r["session"])
        n, nf = len(rows), len(fields)
        cols: List[Any] = [
            pa.array([symbol] * n, type=pa.string()),
            pa.array(np.array([r["session"] for r in rows], dtype="datetime64[D]"), type=pa.date32()),
        ]
        for j in range(nf):
            vals = np.array([np.nan if r["values"][j] is None else float(r["values"][j]) for r in rows],
                            dtype=np.float64)
            cols.append(pa.array(vals, type=pa.float64()))
        for j in range(nf):
            cols.append(pa.array(np.array([_STATUS_CODE[r["status"][j]] for r in rows], dtype=np.int8),
                                 type=pa.int8()))
        for j in range(nf):
            cols.append(pa.array([str(r["reasons"][j]) for r in rows], type=pa.string()))
        cols.append(pa.array([str(r["window_digest"]) for r in rows], type=pa.string()))
        cols.append(pa.array([str(r["raw_shard_ref"]) for r in rows], type=pa.string()))
        cols.append(pa.array(np.array([int(r["raw_row_lo"]) for r in rows], dtype=np.int64), type=pa.int64()))
        cols.append(pa.array(np.array([int(r["raw_row_hi"]) for r in rows], dtype=np.int64), type=pa.int64()))
        return pa.Table.from_arrays(cols, schema=feature_schema(fields))

    def write_feature_object(self, profile: ProfileSpec, symbol: str,
                             rows: Sequence[Mapping[str, Any]]) -> Tuple[ObjectEntry, bool]:
        """Publish one feature object for ``symbol`` whose rows all fall in one calendar month."""
        if not rows:
            raise ValueError("a feature object needs at least one row")
        months = {month_of(r["session"]) for r in rows}
        if len(months) != 1:
            raise ValueError(f"a feature object covers one calendar month, got {sorted(months)}")
        fields = [f.name for f in profile.fields]
        data = self._serialize(self.feature_table(fields, symbol, rows))
        sha = hashlib.sha256(data).hexdigest()
        rel = self.object_rel(profile.name, sha)
        reused = self._publish_bytes(rel, data, sha)
        return ObjectEntry(path=rel, sha256=sha, symbol=symbol, month=months.pop(), rows=len(rows)), reused

    # -- reading objects
    def read_table(self, rel: str, columns: Optional[Sequence[str]] = None):
        import pyarrow.parquet as pq
        return pq.read_table(self.abspath(rel), columns=list(columns) if columns else None)

    def read_raw_concat(self, ref: str) -> Tuple[np.ndarray, np.ndarray]:
        """``(sessions datetime64[D], bars float64 (n, 5))`` of the ``;``-joined raw shard list."""
        if not ref:
            return np.empty(0, dtype="datetime64[D]"), np.empty((0, 5), dtype=np.float64)
        days, mats = [], []
        for sha in ref.split(";"):
            t = self.read_table(self.raw_rel(sha))
            days.append(t.column("session").to_numpy().astype("datetime64[D]"))
            mats.append(np.stack([t.column(c).to_numpy().astype(np.float64) for c in _RAW_COLUMNS], axis=1))
        return np.concatenate(days), np.concatenate(mats, axis=0)

    # -- manifests
    def make_manifest(self, profile: ProfileSpec, *, source_profile: str, timing_policy: str,
                      objects: Iterable[ObjectEntry], raw_objects: Iterable[RawEntry],
                      coverage: Mapping[str, Any], universe: Iterable[str], sessions: Iterable[date],
                      window_start: date, window_end: date,
                      created_at: Optional[str] = None) -> Dict[str, Any]:
        return {
            "profile": profile.name,
            "source_profile": source_profile,
            "timing_policy": timing_policy,
            "calc_version": profile.calc_version,
            "calendar_version": calendar_version(),
            "schema_version": SCHEMA_VERSION,
            "fields": [f.name for f in profile.fields],
            "objects": [o.to_dict() for o in objects],
            "raw_objects": [r.to_dict() for r in raw_objects],
            "coverage": {k: coverage[k] for k in sorted(coverage)},
            "universe_digest": universe_digest(universe),
            "sessions_digest": sessions_digest(sessions),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _normalize(manifest: Mapping[str, Any]) -> Dict[str, Any]:
        missing = [k for k in MANIFEST_KEYS if k not in manifest]
        if missing:
            raise ManifestError(f"manifest lacks keys {missing}")
        extra = [k for k in manifest if k not in MANIFEST_KEYS]
        if extra:
            raise ManifestError(f"manifest carries unknown keys {extra}")
        # Deep copy through JSON (rejects non-JSON values), lists in canonical order.
        return _ordered_lists(json.loads(json.dumps(dict(manifest), allow_nan=False)))

    def write_manifest(self, manifest: Mapping[str, Any]) -> str:
        """Validate ``manifest`` against its objects and publish it LAST; returns its identity.

        Every referenced object and raw shard must exist and hash to its name; each object's rows
        must match its entry (symbol, month, row count) and reference only listed raw shards; and
        no (symbol, session) may appear twice across objects. An existing manifest file of the
        same identity is kept (manifests are immutable)."""
        m = self._normalize(manifest)
        profile = m["profile"]
        fields = list(m["fields"])
        raw_shas = set()
        for r in m["raw_objects"]:
            p = self.abspath(r["path"])
            if not p.exists():
                raise ManifestError(f"raw object {r['path']} is missing")
            if sha256_file(p) != r["sha256"] or r["path"] != self.raw_rel(r["sha256"]):
                raise ManifestError(f"raw object {r['path']} does not hash to its name")
            raw_shas.add(r["sha256"])
        # (symbol, session) -> (object path, 16-byte row fingerprint). A fingerprint, not the
        # row's values: the tuple-of-strings this used to keep measured ~470 MB at 100 symbols x
        # 1500 sessions (~1.9 GB at 400), for a check that only needs equality.
        seen: Dict[Tuple[str, Any], Tuple[str, bytes]] = {}
        for o in m["objects"]:
            if o["path"] != self.object_rel(profile, o["sha256"]):
                raise ManifestError(f"object path {o['path']} is not {profile}/objects/<sha256>.parquet")
            p = self.abspath(o["path"])
            if not p.exists():
                raise ManifestError(f"object {o['path']} is missing")
            if sha256_file(p) != o["sha256"]:
                raise ManifestError(f"object {o['path']} does not hash to its name")
            t = self.read_table(o["path"])
            if [c for c in t.schema.names] != feature_schema(fields).names:
                raise ManifestError(f"object {o['path']} columns do not match the manifest fields")
            if t.num_rows != int(o["rows"]):
                raise ManifestError(f"object {o['path']} holds {t.num_rows} rows, entry says {o['rows']}")
            syms = set(t.column("symbol").to_pylist())
            if syms != {o["symbol"]}:
                raise ManifestError(f"object {o['path']} holds symbols {sorted(syms)}, entry says {o['symbol']}")
            sessions = t.column("session").to_pylist()
            if {month_of(s) for s in sessions} != {o["month"]}:
                raise ManifestError(f"object {o['path']} rows are not all in month {o['month']}")
            for ref in set(t.column("raw_shard_ref").to_pylist()):
                unknown = [s for s in ref.split(";") if s and s not in raw_shas]
                if unknown:
                    raise ManifestError(f"object {o['path']} references raw shards not in raw_objects: {unknown}")
            payload_cols = [c for c in t.schema.names if c not in ("symbol", "session")]
            records = t.select(payload_cols).to_pylist()
            for s, rec in zip(sessions, records):
                key = (o["symbol"], s)
                sig = hashlib.blake2b(repr(sorted(rec.items())).encode("utf-8"), digest_size=16).digest()
                if key in seen:
                    other_path, other_sig = seen[key]
                    kind = "identical" if other_sig == sig else "conflicting"
                    raise ManifestConflictError(
                        f"{kind} duplicate row {o['symbol']} {s} in {other_path} and {o['path']}")
                seen[key] = (o["path"], sig)
        digest = manifest_identity(m)
        final = self.manifest_path(profile, digest)
        if final.exists():
            return digest
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f"{final.name}.{os.getpid()}.{threading.get_ident()}.part")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(m, sort_keys=True, indent=1, ensure_ascii=True, allow_nan=False))
            f.flush()
            os.fsync(f.fileno())
        try:
            try:
                os.replace(tmp, final)
            except OSError:
                # Same race as an object: another publisher wrote this identity first, or a
                # reader holds it open on Windows. The name is the content.
                if not (final.exists() and manifest_identity(json.loads(final.read_text(encoding="utf-8")))
                        == digest):
                    raise
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        return digest

    def _find_manifest(self, digest: str, profile: Optional[str]) -> Path:
        digest = digest.split(":", 1)[1] if digest.startswith("sha256:") else digest
        if profile is not None:
            candidates = [self.manifest_path(profile, digest)]
        else:
            profiles = set(PROFILES)
            if self.root.exists():
                profiles |= {p.name for p in self.root.iterdir() if p.is_dir() and p.name != RAW_DIRNAME}
            candidates = [self.manifest_path(p, digest) for p in sorted(profiles)]
        for c in candidates:
            if c.exists():
                return c
        raise FileNotFoundError(f"no market-condition manifest {digest} under {self.root}")

    def read_manifest(self, digest: str, profile: Optional[str] = None) -> Dict[str, Any]:
        """Load a manifest and check its identity equals ``digest`` (ManifestError otherwise)."""
        path = self._find_manifest(digest, profile)
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
        want = path.stem
        got = manifest_identity(m)
        if got != want:
            raise ManifestError(f"manifest {path} content hashes to {got}, not its name {want}")
        return m

    def list_manifests(self, profile: str) -> List[str]:
        """Digests of the profile's manifests, oldest ``created_at`` first."""
        d = self.root / profile / MANIFESTS_DIRNAME
        if not d.exists():
            return []
        out = []
        for p in d.glob("*.json"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    created = json.load(f).get("created_at", "")
            except (OSError, ValueError):
                continue
            out.append((created, p.stem))
        return [digest for _c, digest in sorted(out)]

    # -- verification
    def verify(self, manifest: Mapping[str, Any], digest: Optional[str] = None) -> VerifyReport:
        """Re-hash EVERY referenced object and raw shard (size equality is not integrity)."""
        ident = manifest_identity(manifest)
        report = VerifyReport(manifest_digest=digest or ident, identity_ok=(digest is None or digest == ident))
        if not report.identity_ok:
            report.errors.append(f"manifest content hashes to {ident}, not {digest}")
        try:
            entries = [(o["path"], o["sha256"]) for o in manifest["objects"]]
            raws = [(r["path"], r["sha256"]) for r in manifest["raw_objects"]]
        except (KeyError, TypeError) as e:
            # A manifest without its object lists is not "nothing to check": it is malformed.
            report.errors.append(f"manifest is missing its object lists: {e}")
            return report
        for kind, items in (("objects", entries), ("raw", raws)):
            for rel, sha in items:
                p = self.abspath(rel)
                if kind == "objects":
                    report.objects_checked += 1
                else:
                    report.raw_checked += 1
                if not p.exists():
                    report.missing.append(rel)
                    continue
                try:
                    if sha256_file(p) != sha:
                        report.corrupt.append(rel)
                except OSError as e:
                    report.corrupt.append(rel)
                    report.errors.append(f"{rel}: {e}")
        return report

    # -- reading rows
    def rows_frame(self, manifest: Mapping[str, Any], symbol: str) -> RowsFrame:
        fields = tuple(manifest["fields"])
        tables = [self.read_table(o["path"]) for o in manifest["objects"] if o["symbol"] == symbol]
        n_f = len(fields)
        if not tables:
            return RowsFrame(symbol, fields, manifest["calc_version"], np.empty(0, "datetime64[D]"),
                             np.empty((0, n_f)), np.empty((0, n_f), np.int8), [], [], [],
                             np.empty(0, np.int64), np.empty(0, np.int64))
        import pyarrow as pa
        t = pa.concat_tables(tables)
        sessions = t.column("session").to_numpy().astype("datetime64[D]")
        order = np.argsort(sessions, kind="stable")
        values = np.stack([t.column(f).to_numpy().astype(np.float64) for f in fields], axis=1)[order]
        status = np.stack([t.column(f"{f}_status").to_numpy().astype(np.int8) for f in fields], axis=1)[order]
        reason_cols = [t.column(f"{f}_reason").to_pylist() for f in fields]
        reasons = [tuple(col[i] for col in reason_cols) for i in order]
        digests = t.column("window_digest").to_pylist()
        refs = t.column("raw_shard_ref").to_pylist()
        return RowsFrame(
            symbol=symbol, fields=fields, calc_version=manifest["calc_version"], sessions=sessions[order],
            values=values, status=status, reasons=reasons,
            window_digests=[digests[i] for i in order], raw_shard_refs=[refs[i] for i in order],
            raw_row_lo=t.column("raw_row_lo").to_numpy()[order], raw_row_hi=t.column("raw_row_hi").to_numpy()[order])

    def iter_rows(self, manifest: Mapping[str, Any], symbol: str) -> Iterator[Tuple[date, FeatureRow]]:
        """``(session, FeatureRow)`` for every row of ``symbol`` in ``manifest``, by session."""
        frame = self.rows_frame(manifest, symbol)
        for i in range(len(frame)):
            yield frame.sessions[i].astype(object), frame.row(i)

    # -- retained evidence
    def _index_manifest(self, manifest: Mapping[str, Any]) -> None:
        for o in manifest["objects"]:
            t = self.read_table(o["path"], columns=["window_digest", "raw_shard_ref", "raw_row_lo", "raw_row_hi"])
            rows = list(zip(t.column("window_digest").to_pylist(), t.column("raw_shard_ref").to_pylist(),
                            t.column("raw_row_lo").to_pylist(), t.column("raw_row_hi").to_pylist()))
            with self._index_lock:
                for dg, ref, lo, hi in rows:
                    self._window_index.setdefault(dg, (ref, int(lo), int(hi)))

    def retained_window(self, window_digest: str, manifest: Optional[Mapping[str, Any]] = None
                        ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """The exact ``(o, h, l, c, v)`` window whose digest is ``window_digest``, rebuilt from the
        retained raw shards (no provider access). Searches ``manifest`` when given, else every
        manifest of every profile. KeyError when no row carries the digest; ManifestError when
        the retained bytes do not hash back to it.

        Only VALID rows have a retained window: a row whose window could not be assembled carries
        an ``unavailable_window_digest`` over a partial (possibly empty) span, which is evidence
        of absence, not a 128-bar window, and is not served here."""
        with self._index_lock:
            hit = self._window_index.get(window_digest)
        if hit is None:
            # Object I/O happens OUTSIDE the lock: indexing a large manifest would otherwise block
            # every other reader of this store for the whole scan.
            manifests = []
            if manifest is not None:
                key = ("explicit", manifest.get("universe_digest"), manifest.get("created_at"),
                       len(manifest.get("objects", ())))
                with self._index_lock:
                    already = key in self._indexed_manifests
                    self._indexed_manifests.add(key)
                if not already:
                    manifests = [manifest]
            else:
                profiles = [p.name for p in self.root.iterdir() if p.is_dir() and p.name != RAW_DIRNAME] \
                    if self.root.exists() else []
                for prof in sorted(profiles):
                    for dg in self.list_manifests(prof):
                        with self._index_lock:
                            already = (prof, dg) in self._indexed_manifests
                            self._indexed_manifests.add((prof, dg))
                        if not already:
                            manifests.append(self.read_manifest(dg, prof))
            for m in manifests:
                self._index_manifest(m)
                with self._index_lock:
                    if window_digest in self._window_index:
                        break
            with self._index_lock:
                hit = self._window_index.get(window_digest)
        if hit is None:
            raise KeyError(f"no retained window with digest {window_digest}")
        ref, lo, hi = hit
        _days, bars = self.read_raw_concat(ref)
        data = np.ascontiguousarray(bars[lo:hi], dtype="<f8").tobytes()
        if window_digest_of_bytes(data) != window_digest:
            raise ManifestError(f"retained raw bars for {window_digest} hash to {window_digest_of_bytes(data)}")
        return window_from_bytes(data)
