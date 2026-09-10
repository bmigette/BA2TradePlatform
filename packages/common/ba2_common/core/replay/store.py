"""The immutable object store and the SQLite replay index (spec step 1, section 3).

"Write immutable objects to temporary files, verify hashes, atomically publish,
then commit references in the dedicated SQLite index. Use process-safe writes
[...]; thread-only locks are insufficient for concurrent warmup processes. A
crash before index commit leaves an orphan object, not a valid complete
analysis. Clean orphans only after a grace period."

Layout under ``root`` (the host injects the root; this module never resolves a
configured path):

    index.sqlite
    objects/<hash[:2]>/<sha256>.json | .arrow
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Set

from ba2_common.core.replay.codec import KIND_ARROW, KIND_JSON, content_hash, extract_meta
from ba2_common.core.replay.schemas import (
    AnalysisRecord,
    CoverageEntry,
    ProviderObservation,
    ReplayStatus,
    SessionRecord,
    to_iso,
)
from ba2_common.logger import logger

_EXTENSIONS = {KIND_JSON: ".json", KIND_ARROW: ".arrow"}
_KIND_BY_EXTENSION = {ext: kind for kind, ext in _EXTENSIONS.items()}

__all__ = [
    "ObjectRef",
    "ObjectStore",
    "ReplayIndex",
    "ReplayStoreError",
    "ObjectNotFound",
    "ObjectHashMismatch",
]


class ReplayStoreError(RuntimeError):
    """A replay store invariant was violated (never swallowed by the store itself)."""


class ObjectNotFound(ReplayStoreError):
    """The requested content hash is not present in the object store."""


class ObjectHashMismatch(ReplayStoreError):
    """A stored object's bytes no longer hash to its name: corrupted or tampered."""


class ObjectRef(NamedTuple):
    """A published object, ready to be referenced by an index commit."""

    hash: str
    kind: str
    size: int


# --------------------------------------------------------------------------- objects


class ObjectStore:
    """Content-addressed, write-once object storage.

    ``index`` (optional) is only consulted by :meth:`iter_orphans` to decide what
    is referenced; the store never writes to it.
    """

    def __init__(self, root, *, index: Optional["ReplayIndex"] = None):
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self._index = index

    def path_for(self, object_hash: str, kind: str) -> Path:
        if kind not in _EXTENSIONS:
            raise ValueError(f"unknown object kind {kind!r}")
        return self.objects_dir / object_hash[:2] / f"{object_hash}{_EXTENSIONS[kind]}"

    def put(self, kind: str, data: bytes, meta: Optional[Dict[str, Any]] = None) -> str:
        """Publish ``data`` atomically and return its content hash.

        Idempotent: an object that is already published is left untouched (it is
        immutable, so identical bytes are already there).
        """
        object_hash = content_hash(kind, data)
        path = self.path_for(object_hash, kind)
        if path.exists():
            return object_hash
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f"{object_hash}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            written = tmp.read_bytes()
            if content_hash(kind, written) != object_hash:
                raise ObjectHashMismatch(
                    f"object {object_hash} did not survive the write to {tmp}"
                )
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
        return object_hash

    def exists(self, object_hash: str) -> bool:
        return any(self.path_for(object_hash, kind).exists() for kind in _EXTENSIONS)

    def get(self, object_hash: str):
        """``(kind, data, meta)`` for a published object, hash-verified on read."""
        for kind in _EXTENSIONS:
            path = self.path_for(object_hash, kind)
            if not path.exists():
                continue
            data = path.read_bytes()
            actual = content_hash(kind, data)
            if actual != object_hash:
                raise ObjectHashMismatch(
                    f"object {path} hashes to {actual}, expected {object_hash}"
                )
            return kind, data, extract_meta(kind, data)
        raise ObjectNotFound(f"object {object_hash} is not in {self.objects_dir}")

    def iter_orphans(self, grace_seconds: float) -> Iterator[str]:
        """Objects older than ``grace_seconds`` that no index row references.

        These are the crash residue: published, then the process died before
        the index commit. They are evidence of an incomplete analysis, never of a
        valid one, so they are only ever reported here -- deletion is the
        caller's explicit decision.
        """
        cutoff = time.time() - grace_seconds
        for path in sorted(self.objects_dir.rglob("*")):
            if not path.is_file() or path.suffix not in _KIND_BY_EXTENSION:
                continue
            if path.stat().st_mtime > cutoff:
                continue
            object_hash = path.stem
            if self._index is not None and self._index.has_object(object_hash):
                continue
            yield object_hash


# --------------------------------------------------------------------------- index


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analyses (
        analysis_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        expert_class TEXT NOT NULL,
        symbol TEXT NOT NULL,
        use_case TEXT NOT NULL,
        outcome TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (analysis_id, attempt_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observations (
        observation_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        method TEXT NOT NULL,
        invocation_seq INTEGER NOT NULL,
        content_hash TEXT,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observation_links (
        observation_id TEXT NOT NULL,
        analysis_id TEXT NOT NULL,
        PRIMARY KEY (observation_id, analysis_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS coverage (
        session_id TEXT NOT NULL,
        analysis_id TEXT NOT NULL,
        capability TEXT NOT NULL,
        status TEXT NOT NULL,
        detail TEXT,
        PRIMARY KEY (session_id, analysis_id, capability)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS objects (
        hash TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        size INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_analyses_session ON analyses (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_observations_session ON observations (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_links_analysis ON observation_links (analysis_id)",
    "CREATE INDEX IF NOT EXISTS ix_coverage_session ON coverage (session_id)",
)


class ReplayIndex:
    """The replay index: a dedicated SQLite database, never the trading DB.

    Process-safe by design (WAL + ``BEGIN IMMEDIATE`` + ``busy_timeout``), so two
    live/warmup processes writing different sessions into the same root both
    succeed. Connections are per-thread; a ``ReplayIndex`` may be shared by the
    capture thread and the writer thread.
    """

    def __init__(self, db_path, *, busy_timeout_ms: int = 30000):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._connections: List[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._create_schema()

    # -- connections

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(self.path), timeout=self._busy_timeout_ms / 1000.0)
        conn.isolation_level = None  # explicit transactions only
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        self._local.conn = conn
        with self._connections_lock:
            self._connections.append(conn)
        return conn

    def _create_schema(self) -> None:
        conn = self._connection()
        with _transaction(conn):
            for statement in _SCHEMA:
                conn.execute(statement)

    def close(self) -> None:
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error as exc:
                logger.error(f"replay index: failed to close a connection: {exc}", exc_info=True)
        self._local = threading.local()

    # -- writes

    def begin_session(self, session: SessionRecord) -> None:
        conn = self._connection()
        with _transaction(conn):
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(session_id, instance_id, status, started_at, ended_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    session.instance_id,
                    session.status,
                    to_iso(session.started_at),
                    to_iso(session.ended_at),
                    _dumps(session.to_mapping()),
                ),
            )

    def update_session_status(self, session_id: str, status: str, ended_at=None) -> None:
        conn = self._connection()
        with _transaction(conn):
            row = conn.execute(
                "SELECT payload FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise ReplayStoreError(f"session {session_id} is not in the index")
            payload = json.loads(row["payload"])
            payload["status"] = status
            if ended_at is not None:
                if ended_at.tzinfo is None:
                    raise ValueError("ended_at must be timezone-aware")
                payload["ended_at"] = ended_at.astimezone(timezone.utc).isoformat()
            conn.execute(
                "UPDATE sessions SET status = ?, ended_at = ?, payload = ? WHERE session_id = ?",
                (status, payload["ended_at"], _dumps(payload), session_id),
            )

    def mark_interrupted(self) -> List[str]:
        """Mark every still-open session interrupted; returns the ids marked.

        Called when a process opens the store: a session left ``open`` means the
        previous process died, and its analyses must stay visible in coverage
        totals rather than disappear.
        """
        conn = self._connection()
        with _transaction(conn):
            rows = conn.execute(
                "SELECT session_id, payload FROM sessions WHERE status = ?",
                (ReplayStatus.SESSION_OPEN,),
            ).fetchall()
            marked = []
            for row in rows:
                payload = json.loads(row["payload"])
                payload["status"] = ReplayStatus.SESSION_INTERRUPTED
                conn.execute(
                    "UPDATE sessions SET status = ?, payload = ? WHERE session_id = ?",
                    (ReplayStatus.SESSION_INTERRUPTED, _dumps(payload), row["session_id"]),
                )
                marked.append(row["session_id"])
        return marked

    def commit_analysis(
        self,
        analysis: AnalysisRecord,
        *,
        observations: Sequence[ProviderObservation] = (),
        coverage: Sequence[CoverageEntry] = (),
        objects: Sequence[ObjectRef] = (),
        verify_objects: Optional[ObjectStore] = None,
    ) -> None:
        """Commit one analysis and everything it references, in one transaction.

        ``verify_objects`` (the object store the objects were published to) is
        checked FIRST: no reference is committed before its object exists.
        """
        referenced = set(analysis.object_hashes())
        for observation in observations:
            if observation.payload_object is not None:
                referenced.add(observation.payload_object)
        published = {ref.hash for ref in objects}
        if verify_objects is not None:
            missing = sorted(h for h in referenced | published if not verify_objects.exists(h))
            if missing:
                raise ReplayStoreError(
                    f"refusing to commit analysis {analysis.analysis_id}: "
                    f"object(s) not published: {missing}"
                )
        dangling = sorted(referenced - published)
        if dangling and verify_objects is None:
            raise ReplayStoreError(
                f"refusing to commit analysis {analysis.analysis_id}: "
                f"object(s) not listed in the commit: {dangling}"
            )

        now = _now_iso()
        conn = self._connection()
        with _transaction(conn):
            for ref in objects:
                conn.execute(
                    "INSERT OR IGNORE INTO objects (hash, kind, size, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (ref.hash, ref.kind, ref.size, now),
                )
            conn.execute(
                "INSERT OR REPLACE INTO analyses "
                "(analysis_id, attempt_id, session_id, expert_class, symbol, use_case, "
                " outcome, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    analysis.analysis_id,
                    analysis.attempt_id,
                    analysis.session_id,
                    analysis.expert_class,
                    analysis.symbol,
                    analysis.use_case,
                    analysis.outcome,
                    _dumps(analysis.to_mapping()),
                ),
            )
            for observation in observations:
                conn.execute(
                    "INSERT OR REPLACE INTO observations "
                    "(observation_id, session_id, provider, method, invocation_seq, "
                    " content_hash, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        observation.observation_id,
                        observation.session_id,
                        observation.provider,
                        observation.method,
                        observation.invocation_seq,
                        observation.content_hash,
                        _dumps(observation.to_mapping()),
                    ),
                )
                for analysis_id in observation.analysis_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO observation_links "
                        "(observation_id, analysis_id) VALUES (?, ?)",
                        (observation.observation_id, analysis_id),
                    )
            _write_coverage(conn, coverage)

    def record_coverage(self, entries: Sequence[CoverageEntry]) -> None:
        conn = self._connection()
        with _transaction(conn):
            _write_coverage(conn, entries)

    def register_objects(self, objects: Sequence[ObjectRef]) -> None:
        """Reference objects without an analysis (used by partial bootstraps)."""
        now = _now_iso()
        conn = self._connection()
        with _transaction(conn):
            for ref in objects:
                conn.execute(
                    "INSERT OR IGNORE INTO objects (hash, kind, size, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (ref.hash, ref.kind, ref.size, now),
                )

    # -- reads

    def list_sessions(self) -> List[SessionRecord]:
        rows = self._connection().execute(
            "SELECT payload FROM sessions ORDER BY started_at, session_id"
        ).fetchall()
        return [SessionRecord.from_mapping(json.loads(row["payload"])) for row in rows]

    def get_session(self, session_id: str) -> SessionRecord:
        row = self._connection().execute(
            "SELECT payload FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise ReplayStoreError(f"session {session_id} is not in the index")
        return SessionRecord.from_mapping(json.loads(row["payload"]))

    def analyses(self, session_id: str) -> List[AnalysisRecord]:
        rows = self._connection().execute(
            "SELECT payload FROM analyses WHERE session_id = ? ORDER BY rowid", (session_id,)
        ).fetchall()
        return [AnalysisRecord.from_mapping(json.loads(row["payload"])) for row in rows]

    def observations(self, analysis_id: str) -> List[ProviderObservation]:
        rows = self._connection().execute(
            "SELECT o.payload AS payload FROM observations o "
            "JOIN observation_links l ON l.observation_id = o.observation_id "
            "WHERE l.analysis_id = ? ORDER BY o.invocation_seq, o.rowid",
            (analysis_id,),
        ).fetchall()
        return [ProviderObservation.from_mapping(json.loads(row["payload"])) for row in rows]

    def session_observations(self, session_id: str) -> List[ProviderObservation]:
        rows = self._connection().execute(
            "SELECT payload FROM observations WHERE session_id = ? ORDER BY invocation_seq, rowid",
            (session_id,),
        ).fetchall()
        return [ProviderObservation.from_mapping(json.loads(row["payload"])) for row in rows]

    def coverage(self, session_id: str) -> List[CoverageEntry]:
        rows = self._connection().execute(
            "SELECT session_id, analysis_id, capability, status, detail FROM coverage "
            "WHERE session_id = ? ORDER BY rowid",
            (session_id,),
        ).fetchall()
        return [
            CoverageEntry(
                session_id=row["session_id"],
                analysis_id=row["analysis_id"],
                capability=row["capability"],
                status=row["status"],
                detail=row["detail"],
            )
            for row in rows
        ]

    def has_object(self, object_hash: str) -> bool:
        row = self._connection().execute(
            "SELECT 1 FROM objects WHERE hash = ?", (object_hash,)
        ).fetchone()
        return row is not None

    def referenced_hashes(self) -> Set[str]:
        rows = self._connection().execute("SELECT hash FROM objects").fetchall()
        return {row["hash"] for row in rows}

    def object_refs(self) -> List[ObjectRef]:
        rows = self._connection().execute(
            "SELECT hash, kind, size FROM objects ORDER BY hash"
        ).fetchall()
        return [ObjectRef(hash=r["hash"], kind=r["kind"], size=r["size"]) for r in rows]


# --------------------------------------------------------------------------- helpers


class _transaction:
    """``BEGIN IMMEDIATE`` ... ``COMMIT`` / ``ROLLBACK``.

    IMMEDIATE (rather than sqlite3's implicit deferred transaction) takes the
    write lock up front, so two processes serialize on ``busy_timeout`` instead
    of failing an upgrade mid-transaction.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")
        return False


def _write_coverage(conn: sqlite3.Connection, entries: Iterable[CoverageEntry]) -> None:
    for entry in entries:
        conn.execute(
            "INSERT OR REPLACE INTO coverage "
            "(session_id, analysis_id, capability, status, detail) VALUES (?, ?, ?, ?, ?)",
            (
                entry.session_id,
                entry.analysis_id,
                entry.capability,
                entry.status,
                entry.detail,
            ),
        )


def _dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, allow_nan=False, ensure_ascii=False, separators=(",", ":"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
