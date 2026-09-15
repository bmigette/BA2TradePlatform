"""Host-neutral live-capture / replay contract (spec step 1).

`docs/plans/2026-09-10-live-capture-prewarm-backtest-replay-spec.md` sections 3
and 4. This package defines WHAT is recorded and HOW it is stored; it decides
nothing about where. It imports no provider, broker, DB model or live-platform
module and reads no configured path -- the host injects the store root and
installs the store through :func:`set_replay_store`.

The ONE deliberate exception to "reads no configured path" is
``from ba2_common.logger import logger``: the house logger transitively imports
``ba2_common.config`` to place its log files. That is logging only -- no replay
root, cache root or database path is ever read from config here.

**Thread pools.** The capture context lives in a ``ContextVar``, which does NOT
propagate into ``ThreadPoolExecutor`` workers: a provider tap running in a pooled
worker would silently record nothing. Anything that fans a gather out across a
pool must submit through :func:`capture_aware_submit` (one copied
``contextvars.Context`` per task) or wrap the callable with
:func:`run_in_capture_context` (safe to reuse concurrently, e.g. with
``executor.map``); :func:`use_capture_context` re-enters a context explicitly.

**Two invariants that are cheap to state and expensive to rediscover.**

* *Clock reads are phase-tagged.* An analysis reads the evaluation clock in both
  halves of the recorded pair, so every read carries the phase (``gather`` /
  ``process``) that took it and a replay of one half consumes only that half's
  reads -- see :class:`~ba2_common.core.replay.schemas.ReplayStatus`.
* *No request-identity key may carry an un-replayed wall clock.* An identity is
  what a replay looks a response up by; a ``datetime.now()`` inside one makes the
  recorded request unmatchable forever. See
  :mod:`ba2_common.core.replay.observe`.
"""
from ba2_common.core.replay.clock import ReplayMiss, replay_now
from ba2_common.core.replay.codec import (
    CODEC_VERSION,
    ENUM_MODULE_PREFIXES,
    CodecDrift,
    Encoded,
    UnsafeEnumReference,
    UnsupportedCaptureType,
    content_hash,
    decode,
    encode,
    freeze,
)
from ba2_common.core.replay.context import (
    CaptureContext,
    CaptureHealth,
    MissingSkipReason,
    PendingObservation,
    capture_aware_submit,
    capture_scope,
    current_capture,
    record_branch_flag,
    run_in_capture_context,
    use_capture_context,
)
from ba2_common.core.replay.dependencies import (
    KINDS,
    KIND_HISTORY,
    KIND_INDICATOR,
    KIND_SERIES,
    KIND_STATE,
    KIND_TIMESERIES,
    KIND_UNSUPPORTED,
    MissingDependencySetting,
    Requirement,
    Window,
    register_adapter,
    registered_experts,
    require_setting,
    required_replay_inputs,
    rule_requirements,
    unregister_adapter,
)
from ba2_common.core.replay.observe import (
    SECRET_KEY_TOKENS,
    observe_provider,
    record_observation,
    sanitize_identity,
    tapped_boundary,
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
    "CodecDrift",
    "UnsupportedCaptureType",
    "UnsafeEnumReference",
    "ENUM_MODULE_PREFIXES",
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
    "MissingSkipReason",
    "PendingObservation",
    "capture_scope",
    "current_capture",
    "record_branch_flag",
    "use_capture_context",
    "run_in_capture_context",
    "capture_aware_submit",
    "observe_provider",
    "record_observation",
    "sanitize_identity",
    "tapped_boundary",
    "SECRET_KEY_TOKENS",
    "replay_now",
    "ReplayMiss",
    "ReplayStore",
    "SessionBundle",
    "load_bundle",
    "set_replay_store",
    "get_replay_store",
    "KINDS",
    "KIND_HISTORY",
    "KIND_INDICATOR",
    "KIND_SERIES",
    "KIND_STATE",
    "KIND_TIMESERIES",
    "KIND_UNSUPPORTED",
    "MissingDependencySetting",
    "Requirement",
    "Window",
    "register_adapter",
    "registered_experts",
    "require_setting",
    "required_replay_inputs",
    "rule_requirements",
    "unregister_adapter",
]
