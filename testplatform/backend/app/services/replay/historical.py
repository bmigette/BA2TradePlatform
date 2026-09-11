"""Historical expert comparison against a pinned cache root (spec step 5).

Spec section 2, second row: "Normal ``analyze_as_of`` path against a pinned,
warmed historical cache; capture its normalized bundle and compare to live.
Measures input and recommendation differences caused by historical
reconstruction, coverage, timing or revisions. **Different endpoints alone are
not a failure.**"

So this command re-runs each recorded analysis the way a BACKTEST would -- the
real ``analyze_as_of`` over a real ``LiveProviderBundle``, reading a pinned cache
root and nothing else -- and diffs BOTH halves against what live recorded: the
normalized ``_gather`` bundle (the "Expert inputs" stage) and the produced
``Recommendation`` (the "Recommendation" stage).

**Why a subprocess, always.** ``ba2_common.config.CACHE_FOLDER`` is read at IMPORT
time by ``native_cache._CACHE_ROOT`` and ``fred_series.cache_path``, and at CALL
time by ``fmp_common._fmp_history_cache_dir``. Assigning to a module attribute
afterwards therefore moves SOME readers to the pinned root and leaves others on
the ambient cache -- a comparison that silently mixes two roots. The only way to
point every reader at one pinned root is to set ``CACHE_FOLDER`` in the
environment of a process that has not imported ba2 yet, which is what
:func:`run` does (the pattern ``ba2_common.core.replay._spawn_child`` and
``ba2-test replay warm`` already document). The parent's own ``CACHE_FOLDER`` is
never touched.

**What each status means here.**

``match``
    The historical reconstruction produced the same inputs AND the same
    recommendation, from artifacts whose revision the pinned root vouches for.
``difference``
    Something differs. The row carries every differing field -- inputs under
    ``inputs[...]`` and the decision under ``recommendation.*`` -- because
    "historical comparison reports absolute/relative input differences and
    resulting decision differences; it does not assume zero difference is always
    attainable" (spec section 8). A difference is EVIDENCE, not automatically a
    defect.
``missing_history``
    The pinned root does not hold a required artifact. The row NAMES the
    requirement, so the answer is a warm plan and not a guess. Nothing is run for
    that analysis -- running it would compare against a reconstruction built from
    a gap.
``revision_unknown``
    The reconstruction matched, but the artifacts behind it cannot be shown to be
    the revision live consumed: either the pin recorded them as
    ``legacy_history_unknown_revision`` (or recorded nothing about them at all),
    or an analyst-estimates payload postdates the live consumption. "Legacy
    history files may be reused as reconstruction inputs [...] That designation
    cannot satisfy exact live observation coverage" (spec section 3). A real
    DIFFERENCE still reports as ``difference`` -- the diff is evidence whatever
    the provenance is -- with the caveat carried in its detail.
``missing_capture``
    The SESSION cannot answer: no recorded bundle, no recorded settings, or the
    historical run recorded nothing to compare against.
``unsupported``
    An expert with no replay-dependency adapter. Its reads are undeclared, so a
    green row would be a lie (spec section 5).

**What the run leaves behind.** The reconstruction is recorded through the SAME
capture machinery live uses, into the SAME store, under a session id derived from
the live one (``<session>#historical:<stamp>``) -- so the rebuilt bundle, the
provider returns behind it and the produced recommendation are evidence in their
own right, not transient values inside a diff. Objects are content-addressed, so a
``match`` literally shares its object files with the live session. The child's job
and result JSON are written beside the report (or into the bundle when no ``out``
is given) and are part of that evidence.

Nothing here opens a trading database or a broker. The child runs inside
:func:`app.services.replay.isolation.replay_isolation` (hermetic FMP, a closed
socket layer, refusing instance/TradeConditions resolvers) and the report carries
the isolation probe, so "offline" is an observation and not a claim.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ba2_common.core.replay import (
    AnalysisRecord,
    ReplayMiss,
    ReplayStatus,
    SessionBundle,
    SessionRecord,
    load_bundle,
)
from ba2_common.core.replay.codec import decode
from ba2_common.core.replay.dependencies import (
    KIND_HISTORY,
    KIND_SERIES,
    KIND_UNSUPPORTED,
    MissingDependencySetting,
    Requirement,
    Window,
    expert_replay_inputs,
)
from ba2_common.core.replay.store import ObjectStore, ReplayIndex

from app.services.replay.expert_replay import (
    RECORDED_EXPERTS,
    compare_recommendations,
    compare_values,
)
from app.services.replay.report import AnalysisResult, ReplayReport, merge_coverage

__all__ = [
    "HistoricalRunError",
    "OFFLINE_API_KEY",
    "PinnedRoot",
    "SUPPORTED_EXPERTS",
    "evaluation_time",
    "run",
]

#: Only the experts step 2 records have a dependency adapter AND a recorded
#: bundle, so only they can be compared. Anything else is ``unsupported``.
SUPPORTED_EXPERTS: Tuple[str, ...] = RECORDED_EXPERTS

#: The credential every provider constructor asks the trading database for. A
#: replay has none by design -- "Do not copy the production trading DB, API keys
#: or broker credentials" (spec section 8) -- and needs none: the transport is
#: closed and every read comes off the pinned root. The placeholder exists so a
#: provider can be CONSTRUCTED; it can never be USED, and it is deliberately not
#: a plausible key.
OFFLINE_API_KEY = "replay-offline-no-network"

#: Settings keys a provider may ask for offline. Anything else is a loud miss
#: rather than a ``None`` that a constructor would read as "not configured".
_CREDENTIAL_HINTS = ("key", "secret", "token", "password")

#: Filenames exchanged with the child.
JOB_NAME = "historical_job.json"
CHILD_RESULT_NAME = "historical_child_result.json"

#: The child's session id is derived from the live one, and carries the run
#: instant so a second run is a second session rather than a second attempt
#: silently merged into the first.
SESSION_SUFFIX = "#historical"

#: How long the child may take before the parent gives up and says so.
CHILD_TIMEOUT_S = 1800.0


class HistoricalRunError(RuntimeError):
    """The historical child could not run at all (not a per-analysis coverage gap)."""


# --------------------------------------------------------------------------- #
# The recorded evaluation time
# --------------------------------------------------------------------------- #
def evaluation_time(analysis: AnalysisRecord) -> datetime:
    """The instant to hand ``analyze_as_of`` for this recorded analysis.

    The FIRST ``process``-phase clock read: that is the evaluation time the live
    decision was actually made at (``_process`` is where the date math that moves
    a signal lives -- the drift window, the rating-recency cut). A ``gather``
    read is a different instant and would silently shift those windows.

    With no process read recorded -- an expert whose ``_process`` does no date
    math, or an older bundle -- the analysis's ``started_at`` stands in. It is a
    recorded fact about the same analysis, never a wall-clock read taken now.
    """
    for value, phase in zip(analysis.clock_reads, analysis.clock_read_phases):
        if phase == ReplayStatus.PHASE_PROCESS:
            return datetime.fromisoformat(value)
    return analysis.started_at


# --------------------------------------------------------------------------- #
# The pinned root's own account of itself
# --------------------------------------------------------------------------- #
class PinnedRoot:
    """A cache root plus whatever ``ba2_providers.warm.roots`` recorded about it.

    The manifest is the ONLY thing that can say which revision a file holds. A
    root without one (an ordinary shared cache pointed at this command) records
    nothing, which is precisely what ``legacy_history_unknown_revision`` means --
    so an absent manifest entry is treated as legacy rather than as "fine".
    """

    def __init__(self, root) -> None:
        from ba2_providers.warm import roots as warm_roots

        self.root = os.path.abspath(str(root))
        self.legacy_provenance = warm_roots.PROVENANCE_LEGACY
        self._files: Dict[str, Dict[str, Any]] = {}
        self.has_manifest = False
        manifest_path = os.path.join(self.root, warm_roots.MANIFEST_NAME)
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
            self._files = dict(manifest["files"])
            self.has_manifest = True

    def relpath(self, path: str) -> str:
        return os.path.relpath(os.path.abspath(path), self.root).replace(os.sep, "/")

    def entry(self, path: str) -> Optional[Dict[str, Any]]:
        return self._files.get(self.relpath(path))

    def provenance(self, path: str) -> str:
        entry = self.entry(path)
        return entry["provenance"] if entry else self.legacy_provenance

    def recorded_mtime(self, path: str) -> Optional[datetime]:
        """When the pin says the file was last written, or ``None`` if unrecorded.

        ``None`` stays ``None``: a file's CURRENT mtime on disk is not evidence
        about the payload it holds (spec section 3, "File mtime is not
        publication time"), and the pin's recorded mtime is used only to detect
        an artifact fetched AFTER the live consumption it is being compared to.
        """
        entry = self.entry(path)
        if not entry or not entry.get("mtime"):
            return None
        return datetime.fromisoformat(entry["mtime"])


# --------------------------------------------------------------------------- #
# One analysis, before anything is run
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Job:
    """A recorded analysis the pinned root can actually answer for."""

    analysis: AnalysisRecord
    as_of: datetime
    revision_notes: Tuple[str, ...]


def _result_for(analysis: AnalysisRecord, status: str, detail: str = "",
                field_diffs: Sequence[Tuple[str, str, str]] = ()) -> AnalysisResult:
    return AnalysisResult(
        analysis_id=analysis.analysis_id,
        expert_class=analysis.expert_class,
        symbol=analysis.symbol,
        use_case=analysis.use_case,
        recorded_outcome=analysis.outcome,
        status=status,
        detail=detail,
        field_diffs=tuple(field_diffs),
    )


def _preflight(bundle: SessionBundle, analysis: AnalysisRecord, pinned: PinnedRoot):
    """Either the row this analysis already earns, or the job that will produce it."""
    from ba2_providers.warm import planner

    if analysis.expert_class not in SUPPORTED_EXPERTS:
        return _result_for(
            analysis, ReplayStatus.COVERAGE_UNSUPPORTED,
            f"{analysis.expert_class} is not one of the recorded experts "
            f"{list(SUPPORTED_EXPERTS)}")
    if analysis.bundle_capture_status != ReplayStatus.CAPTURE_CAPTURED:
        return _result_for(analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
                           f"bundle_capture_status={analysis.bundle_capture_status}; there is "
                           f"no recorded bundle to compare a reconstruction against")
    if analysis.bundle_object is None:
        return _result_for(analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
                           "the record claims a captured bundle but references no object")
    if analysis.settings_object is None:
        return _result_for(analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
                           "no recorded settings; replay will not invent them")

    try:
        settings = bundle.decode(analysis.settings_object)
    except Exception as exc:  # noqa: BLE001 -- one analysis's gap, never the report's
        return _result_for(analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
                           f"the recorded settings could not be decoded: "
                           f"{type(exc).__name__}: {exc}")

    as_of = evaluation_time(analysis)
    # start=None: this command asks whether the ARTIFACT is on the pinned root, and
    # for the per-symbol FMP histories there is no range parameter to ask for a
    # narrower one. How deep a series has to reach is the warm planner's judgement
    # and it still reports a short prefix as `stale` in the entry detail.
    window = Window(start=None, end=as_of)
    try:
        requirements = expert_replay_inputs(
            analysis.expert_class, settings, [analysis.symbol], window)
    except MissingDependencySetting as exc:
        return _result_for(
            analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
            f"the recorded settings cannot declare this expert's dependencies: {exc}")
    if any(req.kind == KIND_UNSUPPORTED for req in requirements):
        return _result_for(analysis, ReplayStatus.COVERAGE_UNSUPPORTED,
                           "; ".join(req.reason for req in requirements
                                     if req.kind == KIND_UNSUPPORTED))

    plan = planner.plan(requirements, [pinned.root], as_of_now=as_of)
    missing = [entry for entry in plan.entries
               if not entry.requirement.optional and entry.status == planner.STATUS_MISSING]
    if missing:
        return _result_for(
            analysis, ReplayStatus.COVERAGE_MISSING_HISTORY,
            "the pinned root does not hold: " + "; ".join(
                f"{entry.requirement.key} ({entry.detail})" for entry in missing))

    return _Job(analysis=analysis, as_of=as_of,
                revision_notes=tuple(_revision_notes(plan, pinned, as_of)))


def _revision_notes(plan, pinned: PinnedRoot, as_of: datetime) -> List[str]:
    """Every reason this reconstruction's inputs are not a proven vintage.

    Two, both from spec section 3:

    * an artifact the pin records as ``legacy_history_unknown_revision`` (or does
      not record at all) -- nothing says which revision it holds;
    * an ANALYST-ESTIMATES payload written after the live analysis consumed its
      estimates. The endpoint filters fiscal periods, not revisions, so a payload
      fetched later is today's revision of a number live read months ago, and "a
      response first observed later" is never proof it was available earlier.

    Price series are deliberately NOT flagged: a daily bar is stamped with its own
    session and the reader slices to ``effective_date <= as_of``, so a file
    written later still answers with the same bars.
    """
    from ba2_experts.replay_dependencies import ESTIMATOR_ESTIMATES_NAMESPACE

    notes: List[str] = []
    for entry in plan.entries:
        requirement: Requirement = entry.requirement
        if requirement.optional or not entry.path:
            continue
        if requirement.kind not in (KIND_HISTORY, KIND_SERIES):
            continue
        relative = pinned.relpath(entry.path)
        if pinned.provenance(entry.path) == pinned.legacy_provenance:
            notes.append(
                f"{requirement.key}: {relative} carries provenance "
                f"{pinned.legacy_provenance}"
                + ("" if pinned.has_manifest
                   else " (the root has no pin manifest, so nothing recorded its revision)"))
            continue
        if requirement.namespace == ESTIMATOR_ESTIMATES_NAMESPACE:
            written = pinned.recorded_mtime(entry.path)
            if written is not None and written > as_of:
                notes.append(
                    f"{requirement.key}: {relative} was written {written.isoformat()}, after "
                    f"the live consumption at {as_of.isoformat()}; the estimates endpoint "
                    f"filters fiscal periods, not revisions, so this is today's revision")
    return notes


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
def run(bundle_dir, cache_root, out_dir=None) -> ReplayReport:
    """Compare every recorded analysis against a reconstruction from ``cache_root``.

    ``cache_root`` is a PINNED root (``ba2-test replay warm`` + the materializer
    write one, manifest included). An ordinary shared cache works too and answers
    honestly -- every row comes back ``revision_unknown`` at best, because nothing
    recorded which revision those files hold.
    """
    import ba2_experts.replay_dependencies  # noqa: F401 - registers the adapters

    bundle = load_bundle(bundle_dir)
    pinned = PinnedRoot(cache_root)
    if not os.path.isdir(pinned.root):
        raise HistoricalRunError(f"{pinned.root} is not a directory")

    # Keyed on the ATTEMPT, never on the analysis id: a re-run of the same live
    # analysis is a second recorded attempt, and collapsing the two would report one
    # row twice and drop the other from the totals.
    decided: Dict[str, AnalysisResult] = {}
    jobs: List[_Job] = []
    for analysis in bundle.analyses:
        outcome = _preflight(bundle, analysis, pinned)
        if isinstance(outcome, _Job):
            jobs.append(outcome)
        else:
            decided[analysis.attempt_id] = outcome

    session_id = f"{bundle.session.session_id}{SESSION_SUFFIX}:{_run_stamp()}"
    # ``isolation: None`` until a child actually runs. An empty probe would read as
    # "nothing tried to leave the machine" for a run where nothing ran at all.
    child: Dict[str, Any] = {"analyses": {}, "isolation": None}
    if jobs:
        child = _run_child(bundle, jobs, pinned, session_id, out_dir)
        produced = _produced_records(bundle_dir, session_id)
        decoder = _object_decoder(bundle_dir)
        for job in jobs:
            attempt_id = job.analysis.attempt_id
            decided[attempt_id] = _compare(
                bundle, job, produced.get(attempt_id),
                child["analyses"].get(attempt_id), decoder)

    report = ReplayReport(
        session_id=bundle.session.session_id,
        bundle_dir=str(Path(bundle_dir)),
        capability=ReplayStatus.CAPABILITY_HISTORICAL,
        results=[decided[a.attempt_id] for a in bundle.analyses],
        extra={ReplayStatus.CAPABILITY_HISTORICAL: {
            "cache_root": pinned.root,
            "pin_manifest": pinned.has_manifest,
            "historical_session_id": session_id,
            "analyses_reconstructed": len(jobs),
            "isolation": child["isolation"],
        }},
    )
    merge_coverage(bundle_dir, report)
    if out_dir is not None:
        report.write(out_dir)
    return report


def _run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]


def _child_work_dir(bundle_dir, out_dir) -> Path:
    """Where the job and result files live: beside the report when there is one."""
    base = Path(out_dir) if out_dir is not None else Path(bundle_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _run_child(bundle: SessionBundle, jobs: Sequence[_Job], pinned: PinnedRoot,
               session_id: str, out_dir) -> Dict[str, Any]:
    """Run every job in ONE child with ``CACHE_FOLDER`` pinned before import.

    One child, not one per analysis: ``CACHE_FOLDER`` is fixed for the whole run,
    so a process per analysis would buy no isolation and pay the (multi-second)
    ba2_providers import for each. Within the child the analyses run in sequence
    and share the provider caches exactly as the bars of a backtest do.
    """
    work_dir = _child_work_dir(bundle.root, out_dir)
    job_path = work_dir / JOB_NAME
    result_path = work_dir / CHILD_RESULT_NAME
    if result_path.exists():
        result_path.unlink()

    payload = {
        "bundle_dir": str(Path(bundle.root).resolve()),
        "store_root": str(Path(bundle.root).resolve()),
        "cache_root": pinned.root,
        "historical_session_id": session_id,
        "live_session_id": bundle.session.session_id,
        "exchange_tz": bundle.session.exchange_tz,
        "instance_id": bundle.session.instance_id,
        "result_path": str(result_path),
        "analyses": [
            {
                "analysis_id": job.analysis.analysis_id,
                # The LIVE attempt this reconstruction answers for. It is carried
                # through as a branch flag rather than reused as the historical
                # record's own attempt_id: the index keys analyses on
                # ``(analysis_id, attempt_id)`` GLOBALLY, so reusing it would make a
                # second historical run collide with the first.
                "live_attempt_id": job.analysis.attempt_id,
                "expert_class": job.analysis.expert_class,
                "expert_instance_id": job.analysis.expert_instance_id,
                "symbol": job.analysis.symbol,
                "use_case": job.analysis.use_case,
                "settings_object": job.analysis.settings_object,
                "as_of": job.as_of.isoformat(),
            }
            for job in jobs
        ],
    }
    job_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    backend_dir = Path(__file__).resolve().parents[3]
    env = dict(os.environ)
    env["CACHE_FOLDER"] = pinned.root
    env["PYTHONPATH"] = os.pathsep.join(
        [str(backend_dir)] + _package_roots() + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    # File logging is a LIVE sink and two processes racing on one rotating handler is a
    # Windows error, not a diagnostic.
    env["BA2_FILE_LOGGING"] = "0"
    # The FMP hermetic lock has an escape hatch; a child that inherited it would run
    # with one lock open while calling itself offline. replay_isolation refuses to
    # start with it set, and it is removed here so the refusal cannot even arise.
    env.pop("BA2_HERMETIC_ALLOW_NETWORK", None)

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "app.services.replay.historical", str(job_path)],
            env=env, cwd=str(backend_dir), capture_output=True, text=True,
            timeout=CHILD_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise HistoricalRunError(
            f"the historical child did not finish within {CHILD_TIMEOUT_S:g}s over "
            f"{len(jobs)} analyses; nothing is reported for them rather than guessed.\n"
            f"stderr tail:\n{_tail(_as_text(exc.stderr))}") from exc
    if not result_path.exists():
        raise HistoricalRunError(
            f"the historical child wrote no result (exit {completed.returncode}).\n"
            f"stderr tail:\n{_tail(completed.stderr)}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not result.get("ok"):
        raise HistoricalRunError(
            f"the historical child failed: {result.get('error')}\n"
            f"stderr tail:\n{_tail(completed.stderr)}")
    return result


def _tail(text: Optional[str], lines: int = 40) -> str:
    return "\n".join((text or "").splitlines()[-lines:])


def _as_text(value: Any) -> str:
    """``TimeoutExpired.stderr`` is bytes even with ``text=True``."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def _package_roots() -> List[str]:
    """The package roots THIS process imported, so the child imports the same ones.

    A worktree's shared venv has the three packages installed editable against the
    MAIN checkout, so a child that resolved them by name would compare the
    worktree's bundle using the main checkout's expert code. Deriving the roots
    from the already-imported modules makes the child agree with its parent by
    construction.
    """
    import ba2_common
    import ba2_experts
    import ba2_providers

    roots: List[str] = []
    for module in (ba2_common, ba2_providers, ba2_experts):
        root = str(Path(module.__file__).resolve().parents[1])
        if root not in roots:
            roots.append(root)
    return roots


# --------------------------------------------------------------------------- #
# Reading what the child produced
# --------------------------------------------------------------------------- #
def _produced_records(bundle_dir, session_id: str) -> Dict[str, AnalysisRecord]:
    """The historical session's records, keyed by the LIVE attempt each answers for."""
    index = ReplayIndex(Path(bundle_dir) / "index.sqlite")
    try:
        records = index.analyses(session_id)
    finally:
        index.close()
    produced: Dict[str, AnalysisRecord] = {}
    for record in records:
        live_attempt = record.branch_flags.get("live_attempt_id")
        if live_attempt is None:
            # Nothing to attribute it to. Dropping it silently would let a
            # reconstruction that DID run report as "recorded nothing".
            raise HistoricalRunError(
                f"the historical record for {record.analysis_id} carries no "
                f"live_attempt_id; it cannot be matched to the analysis it reconstructs")
        produced[live_attempt] = record
    return produced


def _object_decoder(bundle_dir):
    objects = ObjectStore(Path(bundle_dir))

    def decode_object(object_hash: str) -> Any:
        kind, data, meta = objects.get(object_hash)
        return decode(kind, data, meta, frames=objects.get)

    return decode_object


# --------------------------------------------------------------------------- #
# The comparison
# --------------------------------------------------------------------------- #
def _compare(bundle: SessionBundle, job: _Job, produced: Optional[AnalysisRecord],
             child_note: Optional[Dict[str, Any]], decode_object) -> AnalysisResult:
    """Diff the reconstruction against what live recorded, and classify it."""
    analysis = job.analysis
    if produced is None:
        detail = (child_note or {}).get("detail") or "the child reported nothing"
        return _result_for(
            analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
            f"the historical run recorded no analysis for this id ({detail})")

    try:
        diffs = list(_input_diffs(bundle, analysis, produced, decode_object))
        diffs += list(_decision_diffs(bundle, analysis, produced, decode_object))
    except ReplayMiss as miss:
        return _result_for(analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
                           f"the comparison could not run: {miss}")
    except Exception as exc:  # noqa: BLE001 -- one row's gap, never the report's
        return _result_for(analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
                           f"the comparison could not run: {type(exc).__name__}: {exc}")

    caveat = ("; reconstruction inputs of unknown revision: "
              + "; ".join(job.revision_notes)) if job.revision_notes else ""
    if diffs:
        changed = ", ".join(name for name, _recorded, _produced in diffs[:5])
        return _result_for(
            analysis, ReplayStatus.COVERAGE_DIFFERENCE,
            f"{len(diffs)} field(s) differ: {changed}{caveat}", diffs)
    if job.revision_notes:
        return _result_for(
            analysis, ReplayStatus.COVERAGE_REVISION_UNKNOWN,
            "the reconstruction reproduced the recorded inputs and recommendation, but its "
            "inputs cannot be shown to be the revision live consumed: "
            + "; ".join(job.revision_notes))
    return _result_for(
        analysis, ReplayStatus.COVERAGE_MATCH,
        "the historical reconstruction reproduced the recorded inputs and recommendation")


def _input_diffs(bundle: SessionBundle, analysis: AnalysisRecord,
                 produced: AnalysisRecord, decode_object) -> List[Tuple[str, str, str]]:
    """The "Expert inputs" stage: the two normalized ``_gather`` bundles, field by field."""
    if produced.bundle_capture_status != ReplayStatus.CAPTURE_CAPTURED or \
            produced.bundle_object is None:
        return [("inputs", "<the recorded bundle>",
                 f"the historical gather produced no bundle "
                 f"({produced.bundle_capture_status}: {produced.error or 'no detail'})")]
    recorded = bundle.decode(analysis.bundle_object)
    rebuilt = decode_object(produced.bundle_object)
    return compare_values(recorded, rebuilt, "inputs")


def _decision_diffs(bundle: SessionBundle, analysis: AnalysisRecord,
                    produced: AnalysisRecord, decode_object) -> List[Tuple[str, str, str]]:
    """The "Recommendation" stage: outcome first, then every recommendation field."""
    if analysis.outcome != produced.outcome:
        return [("recommendation.outcome",
                 _outcome_text(analysis.outcome, analysis.skip_reason, analysis.error),
                 _outcome_text(produced.outcome, produced.skip_reason, produced.error))]
    if analysis.outcome == ReplayStatus.OUTCOME_ERROR:
        if analysis.error == produced.error:
            return []
        return [("recommendation.error", str(analysis.error), str(produced.error))]

    diffs: List[Tuple[str, str, str]] = []
    if analysis.outcome == ReplayStatus.OUTCOME_SKIP and \
            analysis.skip_reason != produced.skip_reason:
        diffs.append(("recommendation.skip_reason",
                      str(analysis.skip_reason), str(produced.skip_reason)))
    if analysis.recommendation_object is None or produced.recommendation_object is None:
        # One side has no Recommendation object to compare. Saying "match" would claim
        # the price, details and confidence were checked when nothing compared them.
        if analysis.recommendation_object is not produced.recommendation_object:
            diffs.append(("recommendation",
                          "recorded" if analysis.recommendation_object else "<absent>",
                          "produced" if produced.recommendation_object else "<absent>"))
        return diffs
    recorded = bundle.decode(analysis.recommendation_object)
    rebuilt = decode_object(produced.recommendation_object)
    diffs += [(f"recommendation.{name}", was, now)
              for name, was, now in compare_recommendations(recorded, rebuilt)]
    return diffs


def _outcome_text(outcome: str, skip_reason: Optional[str], error: Optional[str]) -> str:
    if outcome == ReplayStatus.OUTCOME_SKIP:
        return f"skip: {skip_reason}"
    if outcome == ReplayStatus.OUTCOME_ERROR:
        return f"error: {error}"
    return outcome


# --------------------------------------------------------------------------- #
# The child
# --------------------------------------------------------------------------- #
@contextmanager
def offline_credentials():
    """Answer every provider's credential lookup offline, and refuse anything else.

    Provider constructors read their API key from the trading database
    (``ba2_common.config.get_app_setting``), which a replay may not open -- and
    ``get_app_setting`` swallows the failure and returns ``None``, which every FMP
    constructor turns into "FMP API key not configured". So the lookup is answered
    with :data:`OFFLINE_API_KEY` instead: enough to construct a provider, useless
    for a request, and unable to reach a database.

    Any OTHER setting is a loud :class:`ReplayMiss`. A configuration value that
    actually steers a calculation must never be silently ``None`` here.

    Every ``ba2_providers`` module binds ``get_app_setting`` by value at import,
    so each binding is replaced, not just the one in ``ba2_common.config``.
    """
    import ba2_common.config as config

    def _offline_get_app_setting(key: str, default: Optional[str] = None) -> Optional[str]:
        if any(hint in str(key).lower() for hint in _CREDENTIAL_HINTS):
            return OFFLINE_API_KEY
        raise ReplayMiss(
            "app_setting", request_identity={"key": key},
            detail="the historical replay does not open the trading database; a setting "
                   "that steers a calculation must come from the recorded settings")

    targets = [config]
    for name, module in list(sys.modules.items()):
        if not name.startswith("ba2_providers") or module is None:
            continue
        if getattr(module, "get_app_setting", None) is not None:
            targets.append(module)

    previous = [(module, module.get_app_setting) for module in targets]
    for module in targets:
        module.get_app_setting = _offline_get_app_setting
    try:
        yield
    finally:
        for module, original in previous:
            module.get_app_setting = original


def _build_historical_expert(entry: Dict[str, Any]):
    """An expert object that can run ``analyze_as_of`` and nothing else.

    ``__new__`` without ``__init__``, exactly as
    ``expert_replay.build_replay_expert`` does and for the same reason: the live
    constructor loads the ``ExpertInstance`` row and its settings out of the
    trading database. Every seam that would reach a live host is a refusal, so a
    reconstruction that unexpectedly reaches for a broker quote, a live settings
    resolve or the live provider registry is a typed miss naming the analysis
    instead of a silent live read.
    """
    from ba2_experts import get_expert_class

    expert_class = entry["expert_class"]
    cls = get_expert_class(expert_class)
    if cls is None:
        raise ReplayMiss("expert_class", analysis_id=entry["analysis_id"],
                         request_identity={"expert_class": expert_class},
                         detail="not a known ba2_experts class")
    expert = cls.__new__(cls)
    expert.id = entry["expert_instance_id"]
    expert.logger = logging.getLogger(f"replay.historical.{expert_class}")
    expert._gather_symbol = entry["symbol"]
    # The two ways an expert holds an FMP key (FMPRating caches it on the instance,
    # DeterministicScorer resolves it through a method). Both are inert: the
    # transport is closed and every read comes off the pinned root.
    expert._api_key = OFFLINE_API_KEY
    expert._get_fmp_api_key = lambda: OFFLINE_API_KEY

    def _refuse_quote(symbol):
        raise ReplayMiss("quote", analysis_id=entry["analysis_id"],
                         request_identity={"symbol": symbol},
                         detail="the historical path prices from the pinned OHLCV series "
                                "(providers.price_at_date), never a broker quote")

    def _refuse_settings(keys):
        raise ReplayMiss("settings", analysis_id=entry["analysis_id"],
                         request_identity={"keys": list(keys)},
                         detail="replay uses the RECORDED settings, never a live resolve")

    def _refuse_live_providers():
        raise ReplayMiss("providers", analysis_id=entry["analysis_id"],
                         request_identity={"expert_class": expert_class},
                         detail="the historical run is driven through BacktestContext.providers")

    expert._get_current_price = _refuse_quote
    expert._resolve_settings = _refuse_settings
    expert._live_providers = _refuse_live_providers
    return expert


def _install_bundle_recorder(expert, context) -> None:
    """Snapshot the normalized bundle between ``_gather`` and ``_process``.

    ``analyze_as_of`` is called UNCHANGED -- it is the production entry point and
    the thing under comparison -- so the snapshot is taken by wrapping this ONE
    instance's ``_gather``, the same point ``_gather_and_process`` snapshots at
    live. The wrapper observes and returns; it never alters what ``_process``
    receives.
    """
    original = expert._gather

    def _recording_gather(*args, **kwargs):
        context.set_phase(ReplayStatus.PHASE_GATHER)
        produced = original(*args, **kwargs)
        context.set_bundle(produced)
        context.set_phase(ReplayStatus.PHASE_PROCESS)
        return produced

    expert._gather = _recording_gather


def _run_one_analysis(bundle: SessionBundle, entry: Dict[str, Any], store,
                      session_id: str) -> Dict[str, Any]:
    """Reconstruct ONE analysis against the pinned root, recording what it consumed."""
    from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle
    from ba2_common.core.replay import capture_scope
    from ba2_providers import get_provider

    analysis_id = entry["analysis_id"]
    try:
        settings = bundle.decode(entry["settings_object"])
        expert = _build_historical_expert(entry)
    except Exception as exc:  # noqa: BLE001 -- reported as this analysis's gap
        return {"ran": False, "detail": f"{type(exc).__name__}: {exc}"}

    as_of = datetime.fromisoformat(entry["as_of"])
    meta = {
        "analysis_id": analysis_id,
        "attempt_id": uuid.uuid4().hex,
        "session_id": session_id,
        "expert_class": entry["expert_class"],
        "expert_instance_id": entry["expert_instance_id"],
        "symbol": entry["symbol"],
        "use_case": entry["use_case"],
        "scheduled_at": None,
        "started_at": datetime.now(timezone.utc),
        "settings": settings,
        "branch_flags": {"historical": True, "as_of": as_of.isoformat(),
                         "live_attempt_id": entry["live_attempt_id"]},
    }
    try:
        with capture_scope(store, meta) as context:
            if context is None:
                return {"ran": False, "detail": "the capture scope did not open"}
            _install_bundle_recorder(expert, context)
            recommendation = expert.analyze_as_of(as_of, BacktestContext(
                providers=LiveProviderBundle(get_provider),
                settings=settings,
                as_of=as_of,
                extra={"symbol": entry["symbol"]}))
            type(expert)._record_outcome(context, recommendation)
    except BaseException as exc:  # noqa: BLE001
        # capture_scope has already recorded this as the analysis's outcome and
        # re-raised it; the comparison reads that record. One failing analysis
        # must not abandon the rest of the session.
        return {"ran": True, "raised": f"{type(exc).__name__}: {exc}"}
    return {"ran": True}


def child_main(argv: Sequence[str]) -> int:
    """The child entry point: ``python -m app.services.replay.historical <job.json>``.

    It verifies FIRST that the ``CACHE_FOLDER`` it imported with is the pinned root
    the job names. Half the cache readers resolve that value at import, so a
    mismatch means the comparison would be reading two roots -- which is a refusal,
    not a warning.
    """
    if len(argv) != 1:
        raise SystemExit("usage: python -m app.services.replay.historical <job.json>")
    job = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    result_path = Path(job["result_path"])

    def _fail(message: str) -> int:
        result_path.write_text(json.dumps({"ok": False, "error": message}, indent=2),
                               encoding="utf-8")
        return 2

    import ba2_common.config as config

    if os.path.abspath(config.CACHE_FOLDER) != os.path.abspath(job["cache_root"]):
        return _fail(
            f"this process imported CACHE_FOLDER={config.CACHE_FOLDER}, but the job pins "
            f"{job['cache_root']}; refusing to reconstruct from a root half the cache "
            f"readers do not point at")

    try:
        return _child_run(job, result_path)
    except BaseException as exc:  # noqa: BLE001
        # The child owns its process; an exception here would reach the parent only
        # as "no result file", with the cause buried in a stderr tail. Writing the
        # reason down is what lets the parent raise something an operator can act on.
        import traceback

        return _fail(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


def _child_run(job: Dict[str, Any], result_path: Path) -> int:
    """The child's body, once the pinned root is verified (see :func:`child_main`)."""
    import ba2_common.config as config
    from ba2_common.core.replay import ReplayStore
    from ba2_providers.fmp_common import frozen_ttl_cache

    from app.services.replay.isolation import replay_isolation

    bundle = load_bundle(job["bundle_dir"])
    store = ReplayStore(job["store_root"], writer="sync")
    session_id = job["historical_session_id"]
    store.begin_session(SessionRecord(
        session_id=session_id,
        instance_id=job["instance_id"],
        started_at=datetime.now(timezone.utc),
        exchange_tz=job["exchange_tz"],
        # The reconstruction is THIS build's, not the recorded one's -- naming the
        # live session's app version here would claim the old code produced it.
        app_version="replay-historical",
        package_versions={},
        source_revision=None,
        dirty=False,
        config_hashes={"cache_root": os.path.abspath(job["cache_root"])},
        capabilities={ReplayStatus.CAPABILITY_HISTORICAL: True,
                      "live_session_id": job["live_session_id"]},
    ))

    analyses: Dict[str, Any] = {}
    try:
        with ExitStack() as stack:
            probe = stack.enter_context(replay_isolation())
            # The fmp_history disk cache is BACKTEST-ONLY: without the freeze the
            # live passthrough would call the fetcher, which the hermetic lock then
            # refuses. Frozen, every per-symbol history is read from the pinned root.
            stack.enter_context(frozen_ttl_cache())
            stack.enter_context(offline_credentials())
            for entry in job["analyses"]:
                # Keyed on the LIVE attempt, like the records themselves: two
                # attempts of one analysis are two rows, not one overwritten twice.
                analyses[entry["live_attempt_id"]] = _run_one_analysis(
                    bundle, entry, store, session_id)
    finally:
        store.close(timeout=60.0)

    result_path.write_text(json.dumps({
        "ok": True,
        "session_id": session_id,
        "cache_root": os.path.abspath(config.CACHE_FOLDER),
        "analyses": analyses,
        "isolation": {
            "network_attempts": list(probe.network_attempts),
            "instance_resolutions": list(probe.instance_resolutions),
            "provider_resolutions": list(probe.provider_resolutions),
        },
    }, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through run()
    sys.exit(child_main(sys.argv[1:]))
