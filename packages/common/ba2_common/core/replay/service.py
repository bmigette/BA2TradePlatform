"""The replay store service and its host seam (spec step 1, section 3).

"Normal recording is asynchronous after an immutable copy is made. It never
waits on remote I/O. On queue saturation, disk failure or unsupported data,
leave the existing live trading behavior intact and raise a visible capture
health error. Coverage becomes incomplete. [...] Session finalization waits for
persistence separately from trading."

The store is host-injected: ``set_replay_store`` is called by the live platform
at startup, and every shared tap reads ``get_replay_store()``. The default is
``None`` -- capture off.
"""
from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ba2_common.core.replay.codec import (
    CODEC_VERSION,
    UnsupportedCaptureType,
    decode,
    encode,
    frame_refs,
)
from ba2_common.core.replay.context import (
    CaptureHealth,
    PendingObservation,
    classify_failure,
)
from ba2_common.core.replay.schemas import (
    SCHEMA_VERSION,
    AnalysisRecord,
    CoverageEntry,
    ProviderObservation,
    ReplayStatus,
    SessionRecord,
)
from ba2_common.core.replay.store import (
    ObjectRef,
    ObjectStore,
    ReplayIndex,
    ReplayStoreError,
)
from ba2_common.logger import logger

__all__ = [
    "ReplayStore",
    "SessionBundle",
    "load_bundle",
    "set_replay_store",
    "get_replay_store",
]

#: object role -> the AnalysisRecord field that holds its content hash
_ROLE_FIELDS = {
    "settings": "settings_object",
    "bundle": "bundle_object",
    "recommendation": "recommendation_object",
}

MANIFEST_NAME = "manifest.json"
COVERAGE_NAME = "coverage.json"

_STOP = object()


@dataclass(frozen=True)
class _Pending:
    record: AnalysisRecord
    objects: Mapping[str, Any]
    observations: Tuple[PendingObservation, ...]


class ReplayStore:
    """Owns the object store, the index and the (optional) writer thread."""

    def __init__(self, root, *, writer: str = "sync", queue_maxsize: int = 512):
        if writer not in ("sync", "thread"):
            raise ValueError(f"writer must be 'sync' or 'thread', got {writer!r}")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = ReplayIndex(self.root / "index.sqlite")
        self.objects = ObjectStore(self.root, index=self.index)
        self.health = CaptureHealth()
        self.writer = writer
        self._queue: Optional["queue.Queue"] = None
        self._thread: Optional[threading.Thread] = None
        if writer == "thread":
            self._queue = queue.Queue(maxsize=queue_maxsize)
            self._thread = threading.Thread(
                target=self._writer_loop, name="ba2-replay-writer", daemon=True
            )
            self._thread.start()

    # -- sessions

    def begin_session(self, session: SessionRecord) -> None:
        self.index.begin_session(session)
        logger.info(
            f"replay capture: session {session.session_id} open "
            f"(instance {session.instance_id}, app {session.app_version})"
        )

    def finalize_session(self, session_id: str) -> None:
        """Drain the writer, then mark the session finalized."""
        self.drain()
        self.index.update_session_status(
            session_id, ReplayStatus.SESSION_FINALIZED, ended_at=datetime.now(timezone.utc)
        )

    def mark_interrupted_sessions(self) -> List[str]:
        marked = self.index.mark_interrupted()
        if marked:
            logger.info(f"replay capture: marked interrupted session(s) {marked}")
        return marked

    def drain(self) -> None:
        if self._queue is not None:
            self._queue.join()

    def close(self) -> None:
        if self._queue is not None and self._thread is not None:
            self._queue.put(_STOP)
            self._thread.join(timeout=60)
            self._thread = None
        self.index.close()

    # -- recording

    def submit(
        self,
        record: AnalysisRecord,
        objects: Optional[Mapping[str, Any]] = None,
        observations: Sequence[PendingObservation] = (),
    ) -> None:
        """Accept a finished analysis for persistence.

        Never blocks on I/O in the caller's thread when ``writer='thread'``: a
        full queue drops the record with a visible health error and an explicit
        ``missing_capture`` coverage row, rather than delaying the trading path.
        """
        pending = _Pending(
            record=record,
            objects=dict(objects or {}),
            observations=tuple(observations),
        )
        if self._queue is None:
            self._write_pending(pending)
            return
        try:
            self._queue.put_nowait(pending)
        except queue.Full:
            self.health.record(CaptureHealth.QUEUE_SATURATION)
            logger.error(
                f"replay capture: writer queue saturated, dropped analysis "
                f"{record.analysis_id} ({record.expert_class}/{record.symbol}); "
                f"coverage is incomplete"
            )
            self._record_drop(record, "writer queue saturated")

    def _record_drop(self, record: AnalysisRecord, detail: str) -> None:
        try:
            self.index.record_coverage(
                [
                    CoverageEntry(
                        session_id=record.session_id,
                        analysis_id=record.analysis_id,
                        capability=ReplayStatus.CAPABILITY_RECORDED_EXPERT,
                        status=ReplayStatus.COVERAGE_MISSING_CAPTURE,
                        detail=detail,
                    )
                ]
            )
        except Exception as exc:
            self.health.record(classify_failure(exc))
            logger.error(
                f"replay capture: could not even record the dropped analysis "
                f"{record.analysis_id}: {exc}",
                exc_info=True,
            )

    def _writer_loop(self) -> None:
        assert self._queue is not None
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                self._write_pending(item)
            except Exception as exc:
                self.health.record(classify_failure(exc))
                logger.error(f"replay capture: writer failed to persist a record: {exc}",
                             exc_info=True)
            finally:
                self._queue.task_done()

    def _write_pending(self, pending: _Pending) -> None:
        """Encode, publish every object, then commit the references in one transaction."""
        refs: List[ObjectRef] = []
        updates: Dict[str, Any] = {}
        record = pending.record

        for role, value in pending.objects.items():
            if role not in _ROLE_FIELDS:
                raise ReplayStoreError(f"unknown capture object role {role!r}")
            try:
                updates[_ROLE_FIELDS[role]] = self._publish(value, refs)
            except UnsupportedCaptureType as exc:
                self.health.record(CaptureHealth.UNSUPPORTED_TYPE)
                logger.error(
                    f"replay capture: {role} of analysis {record.analysis_id} is not "
                    f"representable ({exc}); recorded as a capture gap"
                )
                if role == "bundle":
                    updates["bundle_capture_status"] = ReplayStatus.CAPTURE_UNSUPPORTED
        if "settings_object" in updates:
            updates["settings_hash"] = updates["settings_object"]

        observations: List[ProviderObservation] = []
        for item in pending.observations:
            try:
                payload_hash = self._publish(item.payload, refs)
            except UnsupportedCaptureType as exc:
                self.health.record(CaptureHealth.UNSUPPORTED_TYPE)
                logger.error(
                    f"replay capture: observation {item.observation.observation_id} "
                    f"payload is not representable ({exc}); recorded without its payload"
                )
                payload_hash = None
            observations.append(
                replace(item.observation, payload_object=payload_hash, content_hash=payload_hash)
            )

        self.index.commit_analysis(
            replace(record, **updates),
            observations=observations,
            objects=refs,
            verify_objects=self.objects,
        )

    def _publish(self, value: Any, refs: List[ObjectRef]) -> str:
        encoded = encode(value)
        for side in encoded.sides:
            side_hash = self.objects.put(side.kind, side.data, side.meta)
            refs.append(ObjectRef(hash=side_hash, kind=side.kind, size=len(side.data)))
        object_hash = self.objects.put(encoded.kind, encoded.data, encoded.meta)
        refs.append(ObjectRef(hash=object_hash, kind=encoded.kind, size=len(encoded.data)))
        return object_hash

    # -- reading

    def decode_object(self, object_hash: str) -> Any:
        kind, data, meta = self.objects.get(object_hash)
        return decode(kind, data, meta, frames=self.objects.get)

    # -- export

    def export_session(self, session_id: str, out_dir) -> Path:
        """Write a self-contained, relocatable bundle for ``session_id``.

        Relative object references and content hashes only: no absolute path of
        the recording host ends up in the export.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        session = self.index.get_session(session_id)
        analyses = self.index.analyses(session_id)
        observations = self.index.session_observations(session_id)
        coverage = self.index.coverage(session_id)

        roots: List[str] = []
        for analysis in analyses:
            roots.extend(analysis.object_hashes())
        for observation in observations:
            if observation.payload_object is not None:
                roots.append(observation.payload_object)

        exported: Dict[str, Dict[str, Any]] = {}
        pending = list(roots)
        while pending:
            object_hash = pending.pop()
            if object_hash in exported:
                continue
            kind, data, _meta = self.objects.get(object_hash)
            relative = Path("objects") / object_hash[:2] / self.objects.path_for(
                object_hash, kind
            ).name
            target = out / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            exported[object_hash] = {
                "hash": object_hash,
                "kind": kind,
                "size": len(data),
                "path": relative.as_posix(),
            }
            pending.extend(frame_refs(kind, data))

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "codec_version": CODEC_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "session": session.to_mapping(),
            "analyses": [a.to_mapping() for a in analyses],
            "observations": [o.to_mapping() for o in observations],
            "objects": [exported[h] for h in sorted(exported)],
        }
        (out / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, allow_nan=False, ensure_ascii=False), encoding="utf-8"
        )
        (out / COVERAGE_NAME).write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "session_id": session_id,
                    "entries": [entry.to_mapping() for entry in coverage],
                },
                indent=2,
                allow_nan=False,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        logger.info(
            f"replay capture: exported session {session_id} "
            f"({len(analyses)} analyses, {len(exported)} objects) to {out}"
        )
        return out


@dataclass(frozen=True)
class SessionBundle:
    """A loaded, hash-verified export."""

    root: Path
    session: SessionRecord
    analyses: Tuple[AnalysisRecord, ...]
    observations: Tuple[ProviderObservation, ...]
    coverage: Tuple[CoverageEntry, ...]
    objects: ObjectStore

    def decode(self, object_hash: str) -> Any:
        kind, data, meta = self.objects.get(object_hash)
        return decode(kind, data, meta, frames=self.objects.get)

    def observations_for(self, analysis_id: str) -> Tuple[ProviderObservation, ...]:
        return tuple(o for o in self.observations if analysis_id in o.analysis_ids)


def load_bundle(directory) -> SessionBundle:
    """Load an exported bundle, validating the schema version and every hash."""
    root = Path(directory)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.exists():
        raise ReplayStoreError(f"{root} is not a replay bundle: no {MANIFEST_NAME}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest["schema_version"]
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"bundle schema_version {version} is not supported (expected {SCHEMA_VERSION})"
        )
    codec_version = manifest["codec_version"]
    if codec_version != CODEC_VERSION:
        raise ValueError(
            f"bundle codec_version {codec_version} is not supported (expected {CODEC_VERSION})"
        )

    objects = ObjectStore(root)
    for entry in manifest["objects"]:
        # ObjectStore.get re-hashes: a tampered or truncated object is refused here.
        kind, data, _meta = objects.get(entry["hash"])
        if kind != entry["kind"] or len(data) != entry["size"]:
            raise ReplayStoreError(
                f"object {entry['hash']} does not match the manifest "
                f"(kind {kind!r}/{entry['kind']!r}, size {len(data)}/{entry['size']})"
            )

    coverage_path = root / COVERAGE_NAME
    coverage: List[CoverageEntry] = []
    if coverage_path.exists():
        payload = json.loads(coverage_path.read_text(encoding="utf-8"))
        coverage = [CoverageEntry.from_mapping(item) for item in payload["entries"]]

    return SessionBundle(
        root=root,
        session=SessionRecord.from_mapping(manifest["session"]),
        analyses=tuple(AnalysisRecord.from_mapping(item) for item in manifest["analyses"]),
        observations=tuple(
            ProviderObservation.from_mapping(item) for item in manifest["observations"]
        ),
        coverage=tuple(coverage),
        objects=objects,
    )


#: Convenience: ``ReplayStore.load_bundle(dir)`` reads an export without a store.
ReplayStore.load_bundle = staticmethod(load_bundle)


# --------------------------------------------------------------------------- host seam

_STORE: Optional[ReplayStore] = None
_STORE_LOCK = threading.Lock()


def set_replay_store(store) -> None:
    """Install (or clear with ``None``) the process-wide capture store."""
    global _STORE
    with _STORE_LOCK:
        _STORE = store


def get_replay_store():
    """The installed capture store, or ``None`` when capture is off."""
    return _STORE
