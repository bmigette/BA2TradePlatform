"""Counter-only DRY RUN of the stock decision-pass recorder (design Task 1: measure what recording costs).

``BA2_REPLAY_DECISION_DRYRUN=1`` makes every decision pass of a process that has NO replay store run its
recorders for real, into a throwaway in-memory :class:`CaptureContext` that is never submitted: nothing is
written to any store, and the decision is unchanged (the recorders are the same read-only code). When the
pass ends, :func:`measure_context` encodes what WOULD have been written (``pass_state`` + every step + the
decision summary, with the codec the store uses) and counts the bytes, and the pass-thread seconds the
recorders spent are read off the pass ledger. One INFO line per pass, and a process-wide
:class:`DryRunCounters` for the totals.

The dry run runs the FULL recorder on the live pass thread, so it is a test / dev measuring tool: outside
pytest it is honoured only together with ``BA2_REPLAY_DECISION_DRYRUN_CONFIRM=dev`` (set by hand on a dev
instance). On any other process (prod) the flag is IGNORED and one ERROR line says so.

With a real store installed the flag is ignored: real writes are already the measurement
(``tests/test_decision_pass_sizing_counter.py`` pins that the two agree).
"""
from __future__ import annotations

import os
import threading
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Set

from ba2_common.core.replay.codec import content_hash, encode
from ba2_common.core.replay.context import CaptureContext, CaptureHealth, use_capture_context
from ba2_common.logger import logger

__all__ = [
    "DRYRUN_ENV",
    "DRYRUN_CONFIRM_ENV",
    "DryRunCounters",
    "COUNTERS",
    "dry_run_enabled",
    "DryRunStore",
    "dry_run_scope",
    "encoded_size",
    "measure_context",
]

DRYRUN_ENV = "BA2_REPLAY_DECISION_DRYRUN"
#: The explicit "this is a dev instance" confirmation the flag needs outside pytest.
DRYRUN_CONFIRM_ENV = "BA2_REPLAY_DECISION_DRYRUN_CONFIRM"
_DRYRUN_SESSION = "dryrun"
_REFUSAL_LOGGED = False


def dry_run_enabled() -> bool:
    """Whether the counter-only dry run is on: the flag AND a test / dev context (see the module doc)."""
    global _REFUSAL_LOGGED
    if os.environ.get(DRYRUN_ENV) != "1":
        return False
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get(DRYRUN_CONFIRM_ENV) == "dev":
        return True
    if not _REFUSAL_LOGGED:
        _REFUSAL_LOGGED = True
        logger.error(
            f"{DRYRUN_ENV}=1 is IGNORED: the decision-pass dry run runs the full recorder on the pass "
            f"thread and is a test/dev tool. To use it on a dev instance also set "
            f"{DRYRUN_CONFIRM_ENV}=dev. This process records nothing.")
    return False


class DryRunStore:
    """Just enough store for ``TradeManager._decision_pass_meta`` (a session id): nothing is written."""

    @property
    def session_id(self) -> str:
        return _DRYRUN_SESSION

    def current_session_id(self) -> str:
        return _DRYRUN_SESSION


def encoded_size(value: Any, seen: Optional[Set[str]] = None) -> int:
    """Bytes the store would write for ``value`` (the object itself plus its side objects: frames and
    shared rows). With ``seen`` (a set of object hashes already stored) a side object already in it costs
    nothing -- the content-addressed store keeps one copy -- and the new ones are added to it (the object
    itself included: two byte-identical payloads are one object)."""
    encoded = encode(value)
    total = 0
    for obj in (*encoded.sides, encoded):
        if seen is not None:
            key = content_hash(obj.kind, obj.data)
            if key in seen:
                continue
            seen.add(key)
        total += len(obj.data)
    return total


def measure_context(context: CaptureContext, stored_hashes: Optional[Set[str]] = None) -> Dict[str, Any]:
    """What one finished (or dry) pass would have written, by step kind.

    ``bytes`` / ``total_bytes`` are what the pass writes on its own (every side object counted once per pass
    that holds it). ``stored_bytes`` is what is NEW on disk given the objects ``stored_hashes`` already holds
    (the previous passes of the process): the dedupe of unchanged rows across passes. ``stored_hashes`` is
    updated.
    """
    by_kind: Dict[str, int] = defaultdict(int)
    steps: Dict[str, int] = defaultdict(int)
    pass_seen: Set[str] = set()
    disk_seen = stored_hashes if stored_hashes is not None else set()
    stored = 0
    stored_by_kind: Dict[str, int] = defaultdict(int)
    for pending in context.observations:
        if pending.observation.provider != "decision_step":
            kind = "other_observations"
        else:
            kind = pending.observation.method
            steps[kind] += 1
        by_kind[kind] += encoded_size(pending.payload, pass_seen)
        new = encoded_size(pending.payload, disk_seen)
        stored += new
        stored_by_kind[kind] += new
    objects = context.objects
    by_kind["decision"] = encoded_size(objects["decision"], pass_seen) if "decision" in objects else 0
    stored += encoded_size(objects["decision"], disk_seen) if "decision" in objects else 0
    ledger = context.decision_ledger
    return {
        "pass": context.meta["branch_flags"]["pass"],
        "expert_instance_id": context.meta["expert_instance_id"],
        "outcome": context.build_record().outcome,
        "steps": dict(steps),
        "bytes": dict(by_kind),
        "total_bytes": sum(by_kind.values()),
        "stored_bytes": stored,
        "stored_bytes_by_kind": dict(stored_by_kind),
        "record_ms": 1000.0 * (ledger["record_seconds"] if "record_seconds" in ledger else 0.0),
    }


class DryRunCounters:
    """Process-wide totals of the dry run (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.passes: List[Dict[str, Any]] = []
        #: Hashes of every side object a measured pass has "stored": what a later pass dedupes against.
        self.stored_hashes: Set[str] = set()

    def add(self, measurement: Dict[str, Any]) -> None:
        with self._lock:
            self.passes.append(measurement)

    def reset(self) -> None:
        with self._lock:
            self.passes = []
            self.stored_hashes = set()

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.passes)

    def summary(self) -> Dict[str, Any]:
        rows = self.snapshot()
        total = sum(r["total_bytes"] for r in rows)
        return {
            "passes": len(rows),
            "total_bytes": total,
            "mean_bytes_per_pass": total / len(rows) if rows else 0,
            "mean_record_ms_per_pass": sum(r["record_ms"] for r in rows) / len(rows) if rows else 0,
        }


COUNTERS = DryRunCounters()


@contextmanager
def dry_run_scope(meta):
    """A throwaway capture context around one pass; measured and discarded at the end. Never raises
    into the pass (a failure to measure is logged once and counted nowhere else: it IS the dry run)."""
    try:
        context = CaptureContext(analysis_meta=meta, health=CaptureHealth())
    except Exception as exc:  # noqa: BLE001 -- the dry run must not stop a decision
        logger.error(f"decision dry run: could not open a scope: {exc}", exc_info=True)
        yield None
        return
    with use_capture_context(context):
        try:
            yield context
        finally:
            try:
                measurement = measure_context(context, COUNTERS.stored_hashes)
                COUNTERS.add(measurement)
                logger.info(
                    "decision dry run: expert %s %s pass: %s, %d bytes %s, %d step(s) %s, "
                    "recording cost %.1f ms on the pass thread",
                    measurement["expert_instance_id"], measurement["pass"], measurement["outcome"],
                    measurement["total_bytes"], measurement["bytes"], sum(measurement["steps"].values()),
                    measurement["steps"], measurement["record_ms"])
            except Exception as exc:  # noqa: BLE001
                logger.error(f"decision dry run: pass not measured: {exc}", exc_info=True)
