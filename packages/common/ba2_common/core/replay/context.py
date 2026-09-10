"""The per-analysis capture context (spec step 1, sections 3 and 4).

"Recording is observational; it must not turn an otherwise valid trade into a
skipped trade or silently mark a gap as complete." So every public method here
is a boundary: it does its work, and if that work fails it counts the failure in
:class:`CaptureHealth`, logs it ONCE per analysis at ERROR, and returns. Nothing
raised inside recording reaches the expert.

Absence of a context means capture is off -- there is no null-object stand-in to
mistake for a live recorder.

**BA2_ERROR_MODE.** The house rule is that a broad handler propagates unless the
site names the type. Recording is the deliberate exception, and this module is
where the exception lives: an analysis that fails to RECORD must still trade. The
loudness that replaces propagation is explicit -- a :class:`CaptureHealth` counter
by kind, a per-analysis ``capture_failures`` map on the record itself, one ERROR
log per analysis, and a ``missing_capture`` coverage row -- so a swallowed
recording failure is still visible in four places, just not in the trading path.
"""
from __future__ import annotations

import contextvars
import functools
import queue
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd

from ba2_common.core.replay.codec import UnsupportedCaptureType, freeze
from ba2_common.core.replay.schemas import AnalysisRecord, ProviderObservation, ReplayStatus
from ba2_common.logger import logger

__all__ = [
    "CaptureHealth",
    "MissingSkipReason",
    "CaptureContext",
    "PendingObservation",
    "ReplayMiss",
    "capture_scope",
    "current_capture",
    "record_branch_flag",
    "use_capture_context",
    "run_in_capture_context",
    "capture_aware_submit",
]

_CURRENT: contextvars.ContextVar[Optional["CaptureContext"]] = contextvars.ContextVar(
    "ba2_replay_capture_context", default=None
)


class ReplayMiss(Exception):
    """Replay needed a recorded value that the bundle does not contain.

    Raised instead of falling back to a wall clock, a live request or a
    historical estimate -- a miss is a coverage fact, not something to paper over.
    """

    def __init__(self, kind: str, *, analysis_id=None, request_identity=None, detail=None):
        parts = [f"replay miss ({kind})"]
        if analysis_id is not None:
            parts.append(f"analysis={analysis_id}")
        if request_identity is not None:
            parts.append(f"request={request_identity}")
        if detail is not None:
            parts.append(str(detail))
        super().__init__("; ".join(parts))
        self.kind = kind
        self.analysis_id = analysis_id
        self.request_identity = request_identity
        self.detail = detail


class MissingSkipReason(ValueError):
    """A skip was recorded without saying what it skipped on.

    ``Recommendation(skip=True, skip_reason=None)`` is a contract violation by the
    expert, not a recording failure: the live orchestrators branch on the reason,
    and a coverage report that says "skip" with no reason is a row nobody can act
    on. :meth:`CaptureContext.set_skip` therefore requires a reason, which makes
    the empty case impossible to record by construction.
    """

    def __init__(self, analysis_id):
        super().__init__(
            f"analysis {analysis_id}: a skip must carry a reason "
            f"(Recommendation.skip_reason was empty)"
        )
        self.analysis_id = analysis_id


class CaptureHealth:
    """Visible capture failure counters (spec section 3: "raise a visible capture
    health error"). Thread-safe; shared by every analysis of one store."""

    QUEUE_SATURATION = "queue_saturation"
    DISK_ERROR = "disk_error"
    UNSUPPORTED_TYPE = "unsupported_type"
    OTHER = "other"
    KINDS = (QUEUE_SATURATION, DISK_ERROR, UNSUPPORTED_TYPE, OTHER)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {kind: 0 for kind in self.KINDS}

    def record(self, kind: str) -> None:
        if kind not in self._counts:
            raise ValueError(f"unknown capture health kind {kind!r}")
        with self._lock:
            self._counts[kind] += 1

    def count(self, kind: str) -> int:
        with self._lock:
            return self._counts[kind]

    @property
    def queue_saturation(self) -> int:
        return self.count(self.QUEUE_SATURATION)

    @property
    def disk_error(self) -> int:
        return self.count(self.DISK_ERROR)

    @property
    def unsupported_type(self) -> int:
        return self.count(self.UNSUPPORTED_TYPE)

    @property
    def other(self) -> int:
        return self.count(self.OTHER)

    @property
    def total(self) -> int:
        with self._lock:
            return sum(self._counts.values())

    def as_dict(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counts)


def classify_failure(exc: BaseException) -> str:
    """Which health counter a recording failure belongs to."""
    if isinstance(exc, UnsupportedCaptureType):
        return CaptureHealth.UNSUPPORTED_TYPE
    if isinstance(exc, queue.Full):
        return CaptureHealth.QUEUE_SATURATION
    if isinstance(exc, OSError):
        return CaptureHealth.DISK_ERROR
    return CaptureHealth.OTHER


@dataclass(frozen=True)
class PendingObservation:
    """An observation record plus the frozen payload the writer still has to encode."""

    observation: ProviderObservation
    payload: Any


_REQUIRED_META = (
    "analysis_id",
    "attempt_id",
    "session_id",
    "expert_class",
    "expert_instance_id",
    "symbol",
    "use_case",
    "scheduled_at",
    "started_at",
)


class CaptureContext:
    """Everything one analysis records, held in a ContextVar for the duration.

    In ``capture`` mode it accumulates clock reads, provider observations, the
    normalized bundle and the outcome. In ``replay`` mode it hands back the
    recorded clock reads in order (see :func:`ba2_common.core.replay.clock.replay_now`).
    """

    def __init__(
        self,
        *,
        analysis_meta: Mapping[str, Any],
        health: CaptureHealth,
        mode: str = ReplayStatus.MODE_CAPTURE,
        clock_reads: Sequence[str] = (),
        clock_read_phases: Sequence[str] = (),
        phase: str = ReplayStatus.PHASE_UNKNOWN,
    ):
        missing = [key for key in _REQUIRED_META if key not in analysis_meta]
        if missing:
            raise KeyError(f"analysis_meta is missing required key(s): {missing}")
        if mode == ReplayStatus.MODE_CAPTURE and analysis_meta["started_at"] is None:
            raise ValueError("analysis_meta['started_at'] is required in capture mode")
        self.meta: Dict[str, Any] = dict(analysis_meta)
        self.health = health
        self.mode = mode
        self.analysis_id: str = self.meta["analysis_id"]

        self._objects: Dict[str, Any] = {}
        self._observations: Dict[int, PendingObservation] = {}
        self._next_seq = 0
        self._failures: Dict[str, int] = {}
        self._clock_reads: List[str] = []
        self._clock_read_phases: List[str] = []
        self._phase = phase
        # Replay: the recorded reads split by the phase that took them, so a
        # replay of _process cannot be handed _gather's read (and vice versa).
        self._replay_by_phase: Dict[str, List[str]] = {}
        self._replay_untagged = bool(clock_reads) and len(clock_reads) != len(clock_read_phases)
        if not self._replay_untagged:
            for value, read_phase in zip(clock_reads, clock_read_phases):
                self._replay_by_phase.setdefault(read_phase, []).append(value)
        self._bundle_status = ReplayStatus.CAPTURE_NOT_ATTEMPTED
        self._outcome: Optional[str] = None
        self._skip_reason: Optional[str] = None
        self._error: Optional[str] = None
        self._error_logged = False
        self._lock = threading.Lock()

        if "settings" in self.meta:
            self.set_settings(self.meta["settings"])

    # -- mode

    @classmethod
    def current(cls) -> Optional["CaptureContext"]:
        """The active context, or None when capture is off (see :func:`current_capture`)."""
        return _CURRENT.get()

    @property
    def has_outcome(self) -> bool:
        return self._outcome is not None

    @property
    def is_replay(self) -> bool:
        return self.mode == ReplayStatus.MODE_REPLAY

    @classmethod
    def for_replay(cls, *, analysis_id: str, clock_reads: Sequence[str],
                   clock_read_phases: Sequence[str] = (),
                   phase: str = ReplayStatus.PHASE_UNKNOWN, **meta) -> "CaptureContext":
        """A replay-mode context: no recording, recorded clock reads only.

        ``phase`` says WHICH half of the recorded pair is being replayed, and only
        that half's reads are handed back. Replaying ``_process`` from the front of
        a flat list would feed it ``_gather``'s evaluation time -- a different
        instant, silently.
        """
        base: Dict[str, Any] = {
            "analysis_id": analysis_id,
            "attempt_id": analysis_id,
            "session_id": "replay",
            "expert_class": "",
            "expert_instance_id": None,
            "symbol": "",
            "use_case": "",
            "scheduled_at": None,
            "started_at": None,
        }
        base.update(meta)
        return cls(
            analysis_meta=base,
            health=CaptureHealth(),
            mode=ReplayStatus.MODE_REPLAY,
            clock_reads=clock_reads,
            clock_read_phases=clock_read_phases,
            phase=phase,
        )

    # -- recording boundary

    def set_settings(self, settings: Any) -> None:
        try:
            self._objects["settings"] = freeze(settings)
        except Exception as exc:  # never propagate into the expert
            self._note_failure("settings snapshot failed", exc)

    def set_bundle(self, bundle: Any) -> None:
        """Snapshot the normalized ``_gather`` bundle before ``_process`` runs."""
        try:
            self._objects["bundle"] = freeze(bundle)
            self._bundle_status = ReplayStatus.CAPTURE_CAPTURED
        except Exception as exc:
            self._bundle_status = ReplayStatus.CAPTURE_FAILED
            self._note_failure("bundle snapshot failed", exc)

    def set_outcome(self, *, recommendation=None, skip_reason=None, error=None) -> None:
        """Record what the analysis actually produced: a recommendation, a skip or an error."""
        given = [name for name, value in
                 (("recommendation", recommendation), ("skip_reason", skip_reason), ("error", error))
                 if value is not None]
        if len(given) != 1:
            self._note_failure(
                f"set_outcome needs exactly one of recommendation/skip_reason/error, got {given}",
                ValueError("ambiguous outcome"),
            )
            return
        try:
            if recommendation is not None:
                self._objects["recommendation"] = freeze(recommendation)
                self._outcome = ReplayStatus.OUTCOME_RECOMMENDATION
            elif skip_reason is not None:
                self._outcome = ReplayStatus.OUTCOME_SKIP
                self._skip_reason = str(skip_reason)
            else:
                self._outcome = ReplayStatus.OUTCOME_ERROR
                self._error = _format_error(error)
        except Exception as exc:
            self._note_failure("outcome snapshot failed", exc)

    def set_skip(self, skip_reason: str, recommendation: Any = None) -> None:
        """Record that the analysis ended in a SKIP, with the reason it skipped on.

        The reason is required (:class:`MissingSkipReason` otherwise) -- there is
        no way to record a reasonless skip through this API.

        The skipping ``Recommendation`` is kept alongside it. ``outcome`` is what
        says what the live platform acted on (``skip``), so there is no ambiguity
        in storing the object too -- and there IS a cost in dropping it: a skip
        carries a current_price, details and often a confidence, and a replay that
        only had the reason could not tell a skip whose details changed from one
        that did not. Recording the reason alone was the earlier behaviour and it
        made those fields unreplayable.
        """
        if skip_reason is None or not str(skip_reason).strip():
            raise MissingSkipReason(self.analysis_id)
        if recommendation is not None:
            try:
                self._objects["recommendation"] = freeze(recommendation)
            except Exception as exc:
                self._note_failure("skip recommendation snapshot failed", exc)
        self._outcome = ReplayStatus.OUTCOME_SKIP
        self._skip_reason = str(skip_reason)

    def record_observation(
        self,
        *,
        provider: str,
        method: str,
        request_identity: Mapping[str, Any],
        payload: Any,
        provenance: str,
        response_class: Optional[str] = None,
        fetched_at: Optional[datetime] = None,
        published_at: Optional[datetime] = None,
        first_observed_at: Optional[datetime] = None,
    ) -> Optional[str]:
        """Record one provider return at its return boundary (cache hits included).

        Concurrency: the invocation number is drawn from a monotonic counter under
        the lock and never reused, so two provider calls racing inside one
        analysis (thread pools inside a gather) cannot be handed the same
        ``observation_id`` -- which the index would silently collapse into one row.
        The expensive part (freezing the payload) happens outside the lock.

        The id is keyed on the ATTEMPT, not the analysis: a re-run of the same
        host analysis id is a second attempt (the index keys analyses on
        ``(analysis_id, attempt_id)``), and keying on the analysis alone would
        make the retry's observations collide with the first attempt's.
        """
        try:
            with self._lock:
                seq = self._next_seq
                self._next_seq += 1
            observation_id = f"{self.meta['session_id']}:{self.meta['attempt_id']}#{seq:06d}"
            now = _utc_now()
            observation = ProviderObservation(
                observation_id=observation_id,
                session_id=self.meta["session_id"],
                analysis_ids=[self.analysis_id],
                provider=provider,
                method=method,
                request_identity=dict(request_identity),
                invocation_seq=seq,
                payload_object=None,
                payload_kind=payload_kind_of(payload),
                response_class=response_class or response_class_of(payload),
                content_hash=None,
                fetched_at=fetched_at,
                observed_at=now,
                consumed_at=now,
                published_at=published_at,
                first_observed_at=first_observed_at,
                provenance=provenance,
            )
            pending = PendingObservation(observation=observation, payload=freeze(payload))
            with self._lock:
                self._observations[seq] = pending
            return observation_id
        except Exception as exc:
            self._note_failure(f"observation {provider}.{method} not recorded", exc)
            return None

    @property
    def phase(self) -> str:
        """Which half of the recorded pair is running right now."""
        return self._phase

    def set_phase(self, phase: str) -> None:
        """Enter ``gather`` or ``process``.

        In capture mode this tags the reads that follow; in replay mode it selects
        which recorded reads :meth:`next_clock_read` hands back. Set by
        ``_gather_and_process`` around each half, so the two can never be mixed up
        by an expert that reads a clock in both.
        """
        if phase not in ReplayStatus.PHASES:
            self._note_failure(f"unknown analysis phase {phase!r}",
                               ValueError(f"unknown analysis phase {phase!r}"))
            return
        self._phase = phase

    def record_clock_read(self, value: datetime) -> None:
        try:
            with self._lock:
                self._clock_reads.append(value.isoformat())
                self._clock_read_phases.append(self._phase)
        except Exception as exc:
            self._note_failure("clock read not recorded", exc)

    def next_clock_read(self) -> datetime:
        """Replay mode: the next recorded read OF THIS PHASE. Exhausted is a loud miss."""
        if self._replay_untagged:
            raise ReplayMiss(
                "clock_read_phase", analysis_id=self.analysis_id,
                detail="the recorded reads carry no phase, so none can be replayed "
                       "into a single half of the pair without guessing")
        with self._lock:
            queue = self._replay_by_phase.get(self._phase)
            if not queue:
                raise ReplayMiss(
                    "clock_read", analysis_id=self.analysis_id,
                    detail=f"no unread {self._phase} clock read remains")
            recorded = queue.pop(0)
        return datetime.fromisoformat(recorded)

    def set_branch_flag(self, name: str, value: Any) -> None:
        try:
            flags = dict(self.meta["branch_flags"]) if "branch_flags" in self.meta else {}
            flags[name] = value
            self.meta["branch_flags"] = flags
        except Exception as exc:
            self._note_failure(f"branch flag {name} not recorded", exc)

    # -- finishing

    def build_record(self) -> AnalysisRecord:
        outcome = self._outcome
        error = self._error
        if outcome is None:
            # No silent failure: an analysis that recorded nothing is an error,
            # not an absent row that quietly shrinks the coverage total.
            outcome = ReplayStatus.OUTCOME_ERROR
            error = "analysis ended without a recorded outcome"
        return AnalysisRecord(
            analysis_id=self.analysis_id,
            attempt_id=self.meta["attempt_id"],
            session_id=self.meta["session_id"],
            expert_class=self.meta["expert_class"],
            expert_instance_id=self.meta["expert_instance_id"],
            symbol=self.meta["symbol"],
            use_case=self.meta["use_case"],
            scheduled_at=self.meta["scheduled_at"],
            started_at=self.meta["started_at"],
            finished_at=_utc_now(),
            settings_hash=None,
            settings_object=None,
            bundle_object=None,
            bundle_capture_status=self._bundle_status,
            clock_reads=list(self._clock_reads),
            clock_read_phases=list(self._clock_read_phases),
            outcome=outcome,
            recommendation_object=None,
            skip_reason=self._skip_reason,
            error=error,
            observation_ids=[p.observation.observation_id for p in self.observations],
            branch_flags=dict(self.meta["branch_flags"]) if "branch_flags" in self.meta else {},
            capture_failures=self.capture_failures,
        )

    @property
    def objects(self) -> Dict[str, Any]:
        return dict(self._objects)

    @property
    def observations(self) -> List[PendingObservation]:
        with self._lock:
            return [self._observations[seq] for seq in sorted(self._observations)]

    def submit_to(self, store) -> None:
        """Hand the finished record to the store. Failures stay inside recording."""
        try:
            store.submit(self.build_record(), objects=self.objects, observations=self.observations)
        except Exception as exc:
            self._note_failure("analysis record not submitted", exc)

    # -- failure boundary

    @property
    def capture_failures(self) -> Dict[str, int]:
        """Per-analysis degradation counts by kind (empty when nothing failed)."""
        with self._lock:
            return dict(self._failures)

    def note_failure(self, message: str, exc: BaseException) -> None:
        """Count a recording failure raised OUTSIDE this class (a provider tap).

        The taps in :mod:`ba2_common.core.replay.observe` do work of their own --
        binding arguments, building an identity, probing provenance -- and a
        failure there is exactly the same kind of event as a failure in here: it
        degrades coverage, it is counted and logged once, and it never reaches
        the expert.
        """
        self._note_failure(message, exc)

    def _note_failure(self, message: str, exc: BaseException) -> None:
        kind = classify_failure(exc)
        self.health.record(kind)
        with self._lock:
            if kind not in self._failures:
                self._failures[kind] = 0
            self._failures[kind] += 1
        if self._error_logged:
            return
        self._error_logged = True
        logger.error(
            f"replay capture degraded for analysis {self.analysis_id} "
            f"({self.meta['expert_class']}/{self.meta['symbol']}): {message}: {exc}"
        )


# --------------------------------------------------------------------------- helpers


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_error(error: Any) -> str:
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"
    return str(error)


def payload_kind_of(payload: Any) -> str:
    if payload is None:
        return ReplayStatus.PAYLOAD_NONE
    if isinstance(payload, (pd.DataFrame, pd.Series)):
        return ReplayStatus.PAYLOAD_FRAME
    if isinstance(payload, (dict, list)):
        return ReplayStatus.PAYLOAD_JSON
    return ReplayStatus.PAYLOAD_SCALAR


def response_class_of(payload: Any) -> str:
    """Classify a return WITHOUT coercing it: empty is a fact, not a missing value."""
    if payload is None:
        return ReplayStatus.RESPONSE_EMPTY
    if isinstance(payload, (pd.DataFrame, pd.Series)):
        return ReplayStatus.RESPONSE_EMPTY if len(payload) == 0 else ReplayStatus.RESPONSE_DATA
    if isinstance(payload, (dict, list, tuple, str)):
        return ReplayStatus.RESPONSE_EMPTY if len(payload) == 0 else ReplayStatus.RESPONSE_DATA
    return ReplayStatus.RESPONSE_DATA


def current_capture() -> Optional[CaptureContext]:
    """The active capture/replay context, or None when capture is off."""
    return _CURRENT.get()


def record_branch_flag(name: str, value: Any) -> None:
    """Record WHICH branch of a gather/process the live analysis actually took.

    A branch is not derivable from the observations it produced: "there is a
    calendar response on the tape" and "the calendar branch ran" are different
    statements, and a replay that infers the second from the first will happily
    run the OTHER branch when the response is missing and call the result a
    match. So the branch is recorded as a fact at the point the decision is made.

    A no-op with no capture context, and in replay mode (where the recorded flag
    is what is being read, not rewritten).
    """
    context = current_capture()
    if context is None or context.is_replay:
        return
    context.set_branch_flag(name, value)


@contextmanager
def use_capture_context(context: Optional[CaptureContext]):
    """Install ``context`` for the duration of the block (used by replay)."""
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


def run_in_capture_context(fn):
    """Wrap ``fn`` so it keeps the CURRENT capture context when another thread runs it.

    A ``ContextVar`` is per-thread: a worker inside a ``ThreadPoolExecutor`` sees
    NO capture context, so provider taps and the clock seam inside a pooled
    gather would silently record nothing. Wrap the callable at submit time --
    where the context is still active -- and the worker re-enters it.

    Unlike handing a single ``contextvars.Context`` to several workers (which
    raises once two of them enter it at the same time), this wrapper only
    re-installs the capture ContextVar and is safe to reuse concurrently, e.g.
    with ``executor.map``.
    """
    context = current_capture()

    @functools.wraps(fn)
    def _runner(*args, **kwargs):
        if context is None:
            return fn(*args, **kwargs)
        with use_capture_context(context):
            return fn(*args, **kwargs)

    return _runner


def capture_aware_submit(executor, fn, *args, **kwargs):
    """``executor.submit(fn, ...)`` with the caller's full context copied into the task.

    Uses ``contextvars.copy_context().run`` -- one fresh copy per submission, so
    concurrent tasks never share a Context object.
    """
    context = contextvars.copy_context()
    return executor.submit(context.run, functools.partial(fn, *args, **kwargs))


@contextmanager
def capture_scope(store, analysis_meta: Mapping[str, Any]):
    """Open a recording scope around one live analysis.

    ``store is None`` means capture is off: the block runs with no context at
    all, which is what every provider tap and the clock seam check for.
    """
    if store is None:
        yield None
        return
    try:
        context = CaptureContext(analysis_meta=analysis_meta, health=store.health)
    except Exception as exc:
        store.health.record(classify_failure(exc))
        logger.error(f"replay capture could not open a scope: {exc}", exc_info=True)
        yield None
        return
    token = _CURRENT.set(context)
    try:
        yield context
    except BaseException as exc:
        if not context.has_outcome:
            context.set_outcome(error=exc)
        raise
    finally:
        _CURRENT.reset(token)
        context.submit_to(store)
