"""Host-neutral live-capture / replay contract (spec step 1).

`docs/plans/2026-09-10-live-capture-prewarm-backtest-replay-spec.md` sections 3
and 4. This package defines WHAT is recorded and HOW it is stored; it decides
nothing about where. It imports no provider, broker, DB model or live-platform
module and reads no configured path -- the host injects the store root and
installs the store through :func:`set_replay_store`.
"""
from ba2_common.core.replay.clock import ReplayMiss, replay_now
from ba2_common.core.replay.codec import (
    CODEC_VERSION,
    Encoded,
    UnsupportedCaptureType,
    content_hash,
    decode,
    encode,
    freeze,
)
from ba2_common.core.replay.context import (
    CaptureContext,
    CaptureHealth,
    PendingObservation,
    capture_scope,
    current_capture,
    use_capture_context,
)
from ba2_common.core.replay.schemas import (
    SCHEMA_VERSION,
    AnalysisRecord,
    CoverageEntry,
    ProviderObservation,
    ReplayStatus,
    SessionRecord,
)
from ba2_common.core.replay.service import (
    ReplayStore,
    SessionBundle,
    get_replay_store,
    load_bundle,
    set_replay_store,
)
from ba2_common.core.replay.store import (
    ObjectHashMismatch,
    ObjectNotFound,
    ObjectRef,
    ObjectStore,
    ReplayIndex,
    ReplayStoreError,
)

__all__ = [
    "SCHEMA_VERSION",
    "CODEC_VERSION",
    "ReplayStatus",
    "SessionRecord",
    "AnalysisRecord",
    "ProviderObservation",
    "CoverageEntry",
    "Encoded",
    "UnsupportedCaptureType",
    "encode",
    "decode",
    "freeze",
    "content_hash",
    "ObjectStore",
    "ObjectRef",
    "ReplayIndex",
    "ReplayStoreError",
    "ObjectNotFound",
    "ObjectHashMismatch",
    "CaptureContext",
    "CaptureHealth",
    "PendingObservation",
    "capture_scope",
    "current_capture",
    "use_capture_context",
    "replay_now",
    "ReplayMiss",
    "ReplayStore",
    "SessionBundle",
    "load_bundle",
    "set_replay_store",
    "get_replay_store",
]
