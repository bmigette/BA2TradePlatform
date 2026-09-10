"""Replay contract records (spec step 1, section 3 "Stored records").

Host-neutral by construction: this module imports nothing from providers,
brokers, DB models or the live platform, and never reads a configured path.
Every record is a frozen dataclass carrying an explicit ``schema_version`` so a
bundle written today can be rejected loudly by a future reader instead of being
mis-parsed.

Time rule (spec section 3 "Observation time and revision rules"): every datetime
field is tz-aware and normalized to UTC, or ``None``. Unknown stays unknown --
a missing publication time is never back-filled from a file mtime or a fiscal
period end, and a naive datetime is refused rather than assumed to be UTC.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

__all__ = [
    "SCHEMA_VERSION",
    "ReplayStatus",
    "SessionRecord",
    "AnalysisRecord",
    "ProviderObservation",
    "CoverageEntry",
    "to_iso",
    "from_iso",
]


class ReplayStatus:
    """The string constants used across the replay records.

    Kept as plain strings (not an Enum) because they are persisted verbatim in
    JSON manifests and a SQLite index that outlive any one Python process.
    """

    # session.status
    SESSION_OPEN = "open"
    SESSION_FINALIZED = "finalized"
    SESSION_INTERRUPTED = "interrupted"

    # analysis.bundle_capture_status
    CAPTURE_CAPTURED = "captured"
    CAPTURE_UNSUPPORTED = "unsupported"
    CAPTURE_FAILED = "failed"
    CAPTURE_NOT_ATTEMPTED = "not_attempted"

    # analysis.outcome
    OUTCOME_RECOMMENDATION = "recommendation"
    OUTCOME_SKIP = "skip"
    OUTCOME_ERROR = "error"

    # analysis.use_case
    USE_CASE_ENTER_MARKET = "enter_market"
    USE_CASE_OPEN_POSITIONS = "open_positions"

    # observation.payload_kind
    PAYLOAD_JSON = "json"
    PAYLOAD_FRAME = "frame"
    PAYLOAD_SCALAR = "scalar"
    PAYLOAD_NONE = "none"

    # observation.response_class
    RESPONSE_DATA = "data"
    RESPONSE_EMPTY = "empty"
    RESPONSE_ERROR = "error"

    # observation.provenance
    PROVENANCE_NETWORK = "network"
    PROVENANCE_MEMO_CACHE = "memo_cache"
    PROVENANCE_DISK_CACHE = "disk_cache"
    PROVENANCE_UNKNOWN = "unknown"

    # coverage.capability
    CAPABILITY_RECORDED_EXPERT = "recorded_expert"
    CAPABILITY_GATHER_TAPE = "gather_tape"
    CAPABILITY_HISTORICAL = "historical"
    CAPABILITY_DECISION = "decision"

    # coverage.status
    COVERAGE_MATCH = "match"
    COVERAGE_DIFFERENCE = "difference"
    COVERAGE_MISSING_CAPTURE = "missing_capture"
    COVERAGE_MISSING_HISTORY = "missing_history"
    COVERAGE_REVISION_UNKNOWN = "revision_unknown"
    COVERAGE_UNSUPPORTED = "unsupported"
    COVERAGE_NOT_RUN = "not_run"

    # capture context mode
    MODE_CAPTURE = "capture"
    MODE_REPLAY = "replay"


# --------------------------------------------------------------------------- helpers


def to_iso(value: Optional[datetime]) -> Optional[str]:
    """ISO-8601 for a tz-aware UTC datetime; ``None`` stays ``None``."""
    if value is None:
        return None
    return value.isoformat()


def from_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse what :func:`to_iso` wrote. ``None`` stays ``None``."""
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _utc_or_none(name: str, value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime or None, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be timezone-aware (naive datetimes are refused)")
    return value.astimezone(timezone.utc)


def _frozen_sequence(value: Sequence[Any]) -> Tuple[Any, ...]:
    if value is None:
        raise TypeError("sequence fields must not be None; use an empty sequence")
    if isinstance(value, (str, bytes)):
        raise TypeError("sequence fields must be a list/tuple, not a string")
    return tuple(value)


def _copied_mapping(value: Mapping[str, Any]) -> Dict[str, Any]:
    if value is None:
        raise TypeError("mapping fields must not be None; use an empty mapping")
    return dict(value)


class _Record:
    """Shared (de)serialization for the frozen record dataclasses."""

    _DATETIME_FIELDS: Tuple[str, ...] = ()
    _SEQUENCE_FIELDS: Tuple[str, ...] = ()
    _MAPPING_FIELDS: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in self._DATETIME_FIELDS:
            object.__setattr__(self, name, _utc_or_none(name, getattr(self, name)))
        for name in self._SEQUENCE_FIELDS:
            object.__setattr__(self, name, _frozen_sequence(getattr(self, name)))
        for name in self._MAPPING_FIELDS:
            object.__setattr__(self, name, _copied_mapping(getattr(self, name)))

    def to_mapping(self) -> Dict[str, Any]:
        """A JSON-serializable mapping (datetimes as ISO strings, tuples as lists)."""
        out: Dict[str, Any] = {}
        for spec in fields(self):  # type: ignore[arg-type]
            value = getattr(self, spec.name)
            if spec.name in self._DATETIME_FIELDS:
                out[spec.name] = to_iso(value)
            elif spec.name in self._SEQUENCE_FIELDS:
                out[spec.name] = list(value)
            elif spec.name in self._MAPPING_FIELDS:
                out[spec.name] = dict(value)
            else:
                out[spec.name] = value
        return out

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]):
        known = {spec.name for spec in fields(cls)}  # type: ignore[arg-type]
        unknown = set(data) - known
        if unknown:
            raise ValueError(
                f"{cls.__name__}: unknown field(s) {sorted(unknown)} -- refusing to drop them"
            )
        kwargs: Dict[str, Any] = {}
        for spec in fields(cls):  # type: ignore[arg-type]
            if spec.name not in data:
                continue
            value = data[spec.name]
            if spec.name in cls._DATETIME_FIELDS:
                value = from_iso(value)
            kwargs[spec.name] = value
        return cls(**kwargs)


# --------------------------------------------------------------------------- records


@dataclass(frozen=True, kw_only=True)
class SessionRecord(_Record):
    """One live capture session (spec section 3, "Session" row)."""

    session_id: str
    instance_id: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    exchange_tz: str
    app_version: str
    package_versions: Mapping[str, str] = field(default_factory=dict)
    source_revision: Optional[str] = None
    dirty: bool
    config_hashes: Mapping[str, str] = field(default_factory=dict)
    status: str = ReplayStatus.SESSION_OPEN
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    _DATETIME_FIELDS = ("started_at", "ended_at")
    _MAPPING_FIELDS = ("package_versions", "config_hashes", "capabilities")


@dataclass(frozen=True, kw_only=True)
class AnalysisRecord(_Record):
    """One expert analysis attempt (spec section 3, "Analysis" row).

    ``settings_object`` / ``bundle_object`` / ``recommendation_object`` are
    content hashes into the object store, or ``None`` when that piece was not
    captured -- ``bundle_capture_status`` says why for the bundle, and
    ``capture_gaps`` names every role (``settings``/``bundle``/``recommendation``/
    ``observation:<id>``) whose object could not be encoded.

    ``capture_failures`` counts this analysis's own recording degradation by
    :class:`~ba2_common.core.replay.context.CaptureHealth` kind, so one bad
    analysis is visible in the record itself and not only in a process-wide
    counter.
    """

    analysis_id: str
    attempt_id: str
    session_id: str
    expert_class: str
    expert_instance_id: Optional[int] = None
    symbol: str
    use_case: str
    scheduled_at: Optional[datetime] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    settings_hash: Optional[str] = None
    settings_object: Optional[str] = None
    bundle_object: Optional[str] = None
    bundle_capture_status: str = ReplayStatus.CAPTURE_NOT_ATTEMPTED
    clock_reads: Sequence[str] = ()
    outcome: str
    recommendation_object: Optional[str] = None
    skip_reason: Optional[str] = None
    error: Optional[str] = None
    observation_ids: Sequence[str] = ()
    branch_flags: Mapping[str, Any] = field(default_factory=dict)
    capture_gaps: Sequence[str] = ()
    capture_failures: Mapping[str, int] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    _DATETIME_FIELDS = ("scheduled_at", "started_at", "finished_at")
    _SEQUENCE_FIELDS = ("clock_reads", "observation_ids", "capture_gaps")
    _MAPPING_FIELDS = ("branch_flags", "capture_failures")

    def object_hashes(self) -> Tuple[str, ...]:
        """Every object this record references, in a stable order."""
        return tuple(
            h
            for h in (self.settings_object, self.bundle_object, self.recommendation_object)
            if h is not None
        )


@dataclass(frozen=True, kw_only=True)
class ProviderObservation(_Record):
    """One provider return, at the return boundary (spec section 3, "Provider observation").

    ``request_identity`` is the sanitized, result-changing part of the request:
    credentials are never persisted here.
    """

    observation_id: str
    session_id: str
    analysis_ids: Sequence[str] = ()
    provider: str
    method: str
    request_identity: Mapping[str, Any] = field(default_factory=dict)
    invocation_seq: int
    payload_object: Optional[str] = None
    payload_kind: str
    response_class: str
    content_hash: Optional[str] = None
    fetched_at: Optional[datetime] = None
    observed_at: Optional[datetime] = None
    consumed_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    first_observed_at: Optional[datetime] = None
    provenance: str
    schema_version: int = SCHEMA_VERSION

    _DATETIME_FIELDS = (
        "fetched_at",
        "observed_at",
        "consumed_at",
        "published_at",
        "first_observed_at",
    )
    _SEQUENCE_FIELDS = ("analysis_ids",)
    _MAPPING_FIELDS = ("request_identity",)


@dataclass(frozen=True, kw_only=True)
class CoverageEntry(_Record):
    """Per-analysis, per-capability coverage (spec section 3, "Coverage" row)."""

    session_id: str
    analysis_id: str
    capability: str
    status: str
    detail: Optional[str] = None
    schema_version: int = SCHEMA_VERSION
