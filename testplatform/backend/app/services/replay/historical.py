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
``Recommendation`` (the "Recommendation" stage). Every numeric difference carries
its absolute and relative distance, because spec section 8 asks for "absolute/
relative input differences" and a pair of reprs cannot say whether a price moved
by a cent or by a factor of thirty. The deltas are REPORTING only: equality stays
exact, with no tolerance anywhere.

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
    ``inputs[...]``, the recorded gather branch under ``branch.*`` and the
    decision under ``recommendation.*`` -- because "historical comparison reports
    absolute/relative input differences and resulting decision differences; it
    does not assume zero difference is always attainable" (spec section 8). A
    difference is EVIDENCE, not automatically a defect.
``missing_history``
    The pinned root cannot answer for a required artifact: it is absent, or it
    does not COVER the window (a short tail, a missing prefix, material holes),
    or the reconstruction hit a hermetic cache miss while running. The row NAMES
    the requirement, so the answer is a warm plan and not a guess.
``revision_unknown``
    The reconstruction matched, but the artifacts behind it cannot be shown to be
    the revision live consumed: the pin recorded them as
    ``legacy_history_unknown_revision`` (or recorded nothing about them at all),
    or their bytes have drifted from the pin's own hashes, or an analyst-estimates
    payload postdates the live consumption. "Legacy history files may be reused as
    reconstruction inputs [...] That designation cannot satisfy exact live
    observation coverage" (spec section 3). A real DIFFERENCE still reports as
    ``difference`` -- the diff is evidence whatever the provenance is -- with the
    caveat carried in its detail.
``missing_capture``
    The SESSION cannot answer: no recorded bundle, no recorded settings, or the
    historical run recorded nothing to compare against (including a child that ran
    out of time before reaching this analysis).
``unsupported``
    An expert with no replay-dependency adapter. Its reads are undeclared, so a
    green row would be a lie (spec section 5).

**What the run leaves behind.** The reconstruction is recorded through the SAME
capture machinery live uses, into the SAME store, under a session id derived from
the live one (``<session>#historical:<stamp>``) -- so the rebuilt bundle, the
provider returns behind it and the produced recommendation are evidence in their
own right, not transient values inside a diff. Objects are content-addressed, so a
``match`` literally shares its object files with the live session. NOTE that this
evidence lives in the store's ``index.sqlite`` and ``objects/`` only: the
bundle's ``manifest.json`` describes the exported LIVE session and is never
rewritten, so ``replay inventory`` and ``load_bundle`` keep reporting the session
as recorded. Read the historical session with
``ReplayIndex(<bundle>/index.sqlite).analyses(<historical session id>)``, which
the report's ``historical.historical_session_id`` names. The child's job and
result JSON are written beside the report (or into the bundle when no ``out`` is
given) and are part of that evidence.

Nothing here opens a trading database or a broker. BOTH processes run inside
:func:`app.services.replay.isolation.replay_isolation` (hermetic FMP, a closed
socket layer, refusing instance/TradeConditions resolvers) and the report carries
the child's isolation probe, so "offline" is an observation and not a claim.
"""
from __future__ import annotations

import json
import logging
import os
import re
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
    KIND_INDICATOR,
    KIND_SERIES,
    KIND_TIMESERIES,
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
from app.services.replay.report import (
    AnalysisResult,
    FieldDiff,
    ReplayReport,
    merge_coverage,
)

__all__ = [
    "HistoricalRunError",
    "OFFLINE_API_KEY",
    "PinnedRoot",
    "SUPPORTED_EXPERTS",
    "evaluation_time",
    "offline_credentials",
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

#: Settings-key SEGMENTS that mean "a credential". Matched per segment, not as a
#: substring: a bare ``"key" in name`` also matches ``monkey_mode`` and
#: ``keyword_weights``, and answering a real configuration value with a fake API
#: key would steer the calculation being compared.
_CREDENTIAL_SEGMENTS = frozenset({
    "key", "keys", "apikey", "secret", "secrets", "token", "tokens",
    "password", "passwd", "credential", "credentials",
})

#: The DeterministicScorer branch flag that says whether the LIVE gather held an
#: FMP key. The expert only fetches analyst rows when it did, so forcing a truthy
#: key here would run a branch live never took (``gather_tape`` reads the same
#: flag for the same reason).
DS_ANALYST_KEY_FLAG = "ds_analyst_key_present"

#: Branch flags that describe the HOST, not the calculation. Nothing to compare.
_HOST_BRANCH_FLAGS = frozenset({
    "use_case", "market_analysis_id", "batch_id",
    "historical", "as_of", "live_attempt_id",
})

#: Branch flags whose value is DECIDED BY ``as_of is None``, i.e. by which of the
#: two paths ran. They differ between a live capture and its reconstruction by
#: construction, and spec section 2 is explicit that "different endpoints alone
#: are not a failure" -- so a mismatch here is reported as an expected
#: path difference in the row's detail and never as a ``difference`` status.
#: Verified against every ``record_branch_flag`` call in the packages:
#:   * ``as_of_is_none`` -- set True by the live recorded pair;
#:   * ``earnings_calendar_branch`` / ``earnings_detail_fetch`` -- the live-only
#:     bulk-calendar shortcut and its complement (``FMPEarningsDrift._gather``
#:     gates the first on ``as_of is None``);
#:   * ``fmp_rating_branch`` -- literally "live_snapshot" vs "as_of_reconstruction".
#: Everything else (``ds_analyst_key_present``, ``fmp_rating_analyst_grades``) is
#: decided by the DATA or the SETTINGS and must reproduce, so a mismatch there is a
#: real finding.
_PATH_BRANCH_FLAGS = frozenset({
    "as_of_is_none", "earnings_calendar_branch", "earnings_detail_fetch",
    "fmp_rating_branch",
})

#: Diff-name prefixes, and the spec section 8 stage each belongs to. A branch flag
#: is a fact about what ``_gather`` did, so it lands on "Expert inputs".
STAGE_EXPERT_INPUTS = "Expert inputs"
STAGE_RECOMMENDATION = "Recommendation"
_INPUT_PREFIXES = ("inputs", "branch.")
_RECOMMENDATION_PREFIX = "recommendation."

#: Filename stems exchanged with the child. The run stamp is appended so two runs
#: over one bundle (a second operator, a retry) cannot clobber each other's job
#: or result file mid-flight.
JOB_STEM = "historical_job"
CHILD_RESULT_STEM = "historical_child_result"

#: The child's session id is derived from the live one, and carries the run
#: instant so a second run is a second session rather than a second attempt
#: silently merged into the first.
SESSION_SUFFIX = "#historical"

#: How long the child may take: a fixed budget for importing ba2_providers and
#: opening the store, plus a per-analysis allowance. A constant total would give
#: 500 analyses the same budget as one.
CHILD_TIMEOUT_BASE_S = 300.0
CHILD_TIMEOUT_PER_ANALYSIS_S = 120.0


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

    A manifest that IS present is verified: its format version must be the one
    this build reads (a newer pin's fields mean something else), and every file it
    lists is re-hashed once per run. A file whose bytes have drifted from the pin
    is no longer the artifact the pin vouched for, so no analysis that reads it
    can be called a ``match``.
    """

    def __init__(self, root) -> None:
        from ba2_providers.warm import roots as warm_roots

        self.root = os.path.abspath(str(root))
        self.legacy_provenance = warm_roots.PROVENANCE_LEGACY
        self._files: Dict[str, Dict[str, Any]] = {}
        self.has_manifest = False
        self.drifted: Tuple[str, ...] = ()
        self.unpinned: Tuple[str, ...] = ()
        manifest_path = os.path.join(self.root, warm_roots.MANIFEST_NAME)
        if not os.path.exists(manifest_path):
            return
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        version = int(manifest["version"])
        if version != warm_roots.MANIFEST_VERSION:
            raise HistoricalRunError(
                f"the pinned root {self.root} carries manifest version {version}; this "
                f"build reads {warm_roots.MANIFEST_VERSION}. Re-pin it rather than "
                f"comparing against fields that may mean something else.")
        self._files = dict(manifest["files"])
        self.has_manifest = True
        # ONCE per run, not per analysis: a pin is immutable for the duration of a
        # comparison, and re-hashing a multi-gigabyte root per analysis would cost
        # more than the reconstruction.
        self.drifted = tuple(warm_roots.verify_pinned_root(self.root))
        self.unpinned = tuple(manifest["unpinned"])

    def relpath(self, path: str) -> str:
        return os.path.relpath(os.path.abspath(path), self.root).replace(os.sep, "/")

    def entry(self, path: str) -> Optional[Dict[str, Any]]:
        return self._files.get(self.relpath(path))

    def provenance(self, path: str) -> str:
        entry = self.entry(path)
        return entry["provenance"] if entry else self.legacy_provenance

    def has_drifted(self, path: str) -> bool:
        return self.relpath(path) in self.drifted

    def recorded_mtime(self, path: str) -> Optional[datetime]:
        """When the pin says the file was last written, or ``None`` if unpinned.

        A file's CURRENT mtime on disk is never consulted: it is not evidence
        about the payload it holds (spec section 3, "File mtime is not
        publication time"). The pin's RECORDED mtime is used only to detect an
        artifact fetched AFTER the live consumption it is being compared to, and
        a manifest entry always carries one.
        """
        entry = self.entry(path)
        if entry is None:
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
    coverage_notes: Tuple[str, ...]


def _preflight(bundle: SessionBundle, analysis: AnalysisRecord, pinned: PinnedRoot,
               plans):
    """Either the row this analysis already earns, or the job that will produce it."""
    from ba2_common.core.replay.dependencies import adapter_for

    if analysis.expert_class not in SUPPORTED_EXPERTS or \
            adapter_for(analysis.expert_class) is None:
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_UNSUPPORTED,
            f"{analysis.expert_class} has no replay-dependency adapter and is not one of "
            f"the recorded experts {list(SUPPORTED_EXPERTS)}")
    if analysis.bundle_capture_status != ReplayStatus.CAPTURE_CAPTURED:
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
            f"bundle_capture_status={analysis.bundle_capture_status}; there is no recorded "
            f"bundle to compare a reconstruction against")
    if analysis.bundle_object is None:
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
            "the record claims a captured bundle but references no object")
    if analysis.settings_object is None:
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
            "no recorded settings; replay will not invent them")

    try:
        settings = bundle.decode(analysis.settings_object)
    except Exception as exc:  # noqa: BLE001 -- one analysis's gap, never the report's
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
            f"the recorded settings could not be decoded: {type(exc).__name__}: {exc}")

    as_of = evaluation_time(analysis)
    try:
        plan = plans.plan_for(analysis, settings, as_of)
    except MissingDependencySetting as exc:
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
            f"the recorded settings cannot declare this expert's dependencies: {exc}")

    blocking, notes = _coverage_verdict(plan)
    if blocking:
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_MISSING_HISTORY,
            "the pinned root cannot answer for: " + "; ".join(blocking))

    return _Job(analysis=analysis, as_of=as_of,
                revision_notes=tuple(_revision_notes(plan, pinned, as_of)),
                coverage_notes=tuple(notes))


def _coverage_verdict(plan) -> Tuple[List[str], List[str]]:
    """``(blocking shortfalls, non-blocking notes)`` for one analysis's plan.

    ``STATUS_STALE`` means two DIFFERENT things depending on the requirement kind,
    and the difference decides whether the row can be compared at all:

    * for a parquet SERIES (``timeseries``/``indicator``) ``_judge_timeseries``
      returns it for a missing prefix, a short tail or material holes -- the file
      exists but does not COVER the window. A reconstruction off that series is
      built from a gap, so it is a ``missing_history`` shortfall, named with the
      planner's own detail.
    * for an ``fmp_history`` payload or a FRED series it is returned purely on
      FILE AGE, and the hermetic reader ignores age by design ("historical data
      doesn't go stale", ``_fmp_history_disk_read_or_fetch``). Calling that
      ``missing_history`` would report a perfectly readable artifact as absent, so
      it is carried as a NOTE on whatever row the comparison produces instead of
      being dropped.

    ``STATUS_UNSUPPORTED`` is blocking too. It cannot normally be reached (an
    ``unsupported`` requirement is caught before the plan is built) and is handled
    here so a future requirement kind cannot arrive as silent coverage.
    """
    from ba2_providers.warm import planner

    blocking: List[str] = []
    notes: List[str] = []
    for entry in plan.entries:
        if entry.requirement.optional:
            continue
        key = entry.requirement.key
        if entry.status in (planner.STATUS_MISSING, planner.STATUS_UNSUPPORTED):
            blocking.append(f"{key} ({entry.status}: {entry.detail})")
        elif entry.status == planner.STATUS_STALE:
            if entry.requirement.kind in (KIND_TIMESERIES, KIND_INDICATOR):
                blocking.append(f"{key} (incomplete coverage: {entry.detail})")
            else:
                notes.append(f"{key} is stale on the pin ({entry.detail}); the hermetic "
                             f"reader ignores fmp_history/FRED age, so it was read anyway")
    return blocking, notes


def _revision_notes(plan, pinned: PinnedRoot, as_of: datetime) -> List[str]:
    """Every reason this reconstruction's inputs are not a proven vintage.

    Three, the first two from spec section 3:

    * an artifact the pin records as ``legacy_history_unknown_revision`` (or does
      not record at all) -- nothing says which revision it holds. This applies to
      PRICE SERIES as much as to per-symbol histories: a split revises every past
      bar of a series, so "a daily bar is immutable" is not true and a parquet of
      unknown vintage cannot back a proven match;
    * an ANALYST-ESTIMATES payload written after the live analysis consumed its
      estimates. The endpoint filters fiscal periods, not revisions, so a payload
      fetched later is today's revision of a number live read months ago, and "a
      response first observed later" is never proof it was available earlier. This
      one is history-only: it is a statement about that endpoint's semantics, not
      about file ages in general;
    * an artifact whose bytes no longer match the pin's own hash. The pin promised
      those bytes; something else is on disk now, so the comparison ran against an
      artifact nobody vouched for.
    """
    from ba2_experts.replay_dependencies import ESTIMATOR_ESTIMATES_NAMESPACE

    notes: List[str] = []
    for entry in plan.entries:
        requirement: Requirement = entry.requirement
        if requirement.optional or not entry.path:
            continue
        if requirement.kind not in (KIND_HISTORY, KIND_SERIES, KIND_TIMESERIES,
                                    KIND_INDICATOR):
            continue
        relative = pinned.relpath(entry.path)
        if pinned.has_drifted(entry.path):
            notes.append(
                f"{requirement.key}: {relative} no longer matches the hash the pin "
                f"recorded for it; the bytes read are not the ones pinned")
            continue
        if pinned.provenance(entry.path) == pinned.legacy_provenance:
            notes.append(
                f"{requirement.key}: {relative} carries provenance "
                f"{pinned.legacy_provenance}"
                + ("" if pinned.has_manifest
                   else " (the root has no pin manifest, so nothing recorded its revision)"))
            continue
        if requirement.kind == KIND_HISTORY and \
                requirement.namespace == ESTIMATOR_ESTIMATES_NAMESPACE:
            written = pinned.recorded_mtime(entry.path)
            if written is not None and written > as_of:
                notes.append(
                    f"{requirement.key}: {relative} was written {written.isoformat()}, after "
                    f"the live consumption at {as_of.isoformat()}; the estimates endpoint "
                    f"filters fiscal periods, not revisions, so this is today's revision")
    return notes


class _PlanCache:
    """One warm plan per distinct (expert, settings, symbol, day), built once.

    ``planner.plan`` builds a fresh ``_SizeIndex`` (which scans every cache
    sub-directory) and re-reads each parquet's footer. A session is typically one
    settings dict over many symbols on one day, so without this the same root is
    re-scanned once per analysis -- minutes of stat calls on a real cache for an
    answer that cannot change within a run.
    """

    def __init__(self, pinned: PinnedRoot) -> None:
        self._pinned = pinned
        self._plans: Dict[Tuple[str, str, str, str], Any] = {}

    def plan_for(self, analysis: AnalysisRecord, settings, as_of: datetime):
        from ba2_providers.warm import planner

        key = (analysis.expert_class, str(analysis.settings_object),
               analysis.symbol.upper(), as_of.date().isoformat())
        if key in self._plans:
            return self._plans[key]
        # start=None: this command asks whether the ARTIFACT is on the pinned root,
        # and for the per-symbol FMP histories there is no range parameter to ask
        # for a narrower one.
        #
        # What that means for a SERIES, precisely: only the adapters that derive
        # their own start survive it -- DeterministicScorer
        # (``window.trailing(OHLCV_LOOKBACK_DAYS)``) and FMPInsiderClusterBuy
        # (``window.trailing(lookback_days)``) hand the planner a real span and get
        # the prefix and hole checks. FMPRating and FMPEarningsDrift pass this
        # window straight through, so their price requirement carries
        # ``start=None`` and ``_judge_timeseries`` can only judge the TAIL (is the
        # newest bar at least the last completed session before the as_of). A
        # series with a hole in the middle of their lookback is therefore not
        # detected here; it is a warm-plan question, and this command reports the
        # tail shortfall it CAN see (see _coverage_verdict) rather than implying
        # more.
        requirements = expert_replay_inputs(
            analysis.expert_class, settings, [analysis.symbol],
            Window(start=None, end=as_of))
        plan = planner.plan(requirements, [self._pinned.root], as_of_now=as_of)
        self._plans[key] = plan
        return plan


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
def run(bundle_dir, cache_root, out_dir=None, *, timeout: Optional[float] = None) -> ReplayReport:
    """Compare every recorded analysis against a reconstruction from ``cache_root``.

    ``cache_root`` is a PINNED root (``ba2-test replay warm`` + the materializer
    write one, manifest included). An ordinary shared cache works too and answers
    honestly -- every row comes back ``revision_unknown`` at best, because nothing
    recorded which revision those files hold.

    ``timeout`` bounds the child in seconds; by default it scales with the number
    of analyses. A child that runs out of time does NOT discard the report: every
    analysis it committed is still compared, and the rest are reported as rows
    naming the timeout.

    The PARENT half runs inside :func:`replay_isolation` too. It reads a bundle
    and stats a cache root, so it needs nothing from the network -- and entering
    the isolation is what makes the ``BA2_HERMETIC_ALLOW_NETWORK`` refusal reach
    this command at all, instead of being quietly stripped from the child's
    environment while the parent ran with the FMP lock open.
    """
    import ba2_experts.replay_dependencies  # noqa: F401 - registers the adapters

    from app.services.replay.isolation import replay_isolation

    with replay_isolation():
        return _run_isolated(bundle_dir, cache_root, out_dir, timeout)


def _run_isolated(bundle_dir, cache_root, out_dir, timeout: Optional[float]) -> ReplayReport:
    bundle = load_bundle(bundle_dir)
    pinned = PinnedRoot(cache_root)
    if not os.path.isdir(pinned.root):
        raise HistoricalRunError(f"{pinned.root} is not a directory")

    # Keyed on the ATTEMPT, never on the analysis id: a re-run of the same live
    # analysis is a second recorded attempt, and collapsing the two would report one
    # row twice and drop the other from the totals.
    decided: Dict[str, AnalysisResult] = {}
    jobs: List[_Job] = []
    plans = _PlanCache(pinned)
    for analysis in bundle.analyses:
        outcome = _preflight(bundle, analysis, pinned, plans)
        if isinstance(outcome, _Job):
            jobs.append(outcome)
        else:
            decided[analysis.attempt_id] = outcome

    stamp = _run_stamp()
    session_id = f"{bundle.session.session_id}{SESSION_SUFFIX}:{stamp}"
    # ``isolation: None`` until a child actually runs. An empty probe would read as
    # "nothing tried to leave the machine" for a run where nothing ran at all.
    child: Dict[str, Any] = {"analyses": {}, "isolation": None, "timed_out": False}
    if jobs:
        child = _run_child(bundle, jobs, pinned, session_id, out_dir, stamp, timeout)
        produced = _produced_records(bundle_dir, session_id)
        decoder = _object_decoder(bundle_dir)
        for job in jobs:
            attempt_id = job.analysis.attempt_id
            decided[attempt_id] = _compare(
                bundle, job, produced.get(attempt_id),
                child["analyses"].get(attempt_id), decoder, child["timed_out"])

    results = [decided[a.attempt_id] for a in bundle.analyses]
    report = ReplayReport(
        session_id=bundle.session.session_id,
        bundle_dir=str(Path(bundle_dir)),
        capability=ReplayStatus.CAPABILITY_HISTORICAL,
        results=results,
        stage_results=_stage_results(results),
        extra={ReplayStatus.CAPABILITY_HISTORICAL: {
            "cache_root": pinned.root,
            "pin_manifest": pinned.has_manifest,
            "pin_drifted_files": list(pinned.drifted),
            "pin_unpinned_requirements": list(pinned.unpinned),
            "historical_session_id": session_id,
            "analyses_reconstructed": len(jobs),
            "child_timed_out": bool(child["timed_out"]),
            "isolation": child["isolation"],
        }},
    )
    merge_coverage(bundle_dir, report)
    if out_dir is not None:
        report.write(out_dir)
    return report


def _stage_results(results: Sequence[AnalysisResult]) -> Dict[str, Dict[str, str]]:
    """Per-stage status for every row, from the diff names it carries.

    An analysis whose inputs reproduced and whose RECOMMENDATION moved is a
    ``match`` at "Expert inputs" and a ``difference`` at "Recommendation". Rolling
    one analysis-level status into both rows points the reader at the wrong stage
    -- and the historical report is the one that fills two stages at once.

    Keyed on ``AnalysisResult.row_id`` -- the ATTEMPT -- for the same reason the
    rest of this module is: a re-run of one live analysis is a second recorded
    attempt and a second row. Keyed on the analysis id, two attempts collapse into
    one entry, the stage then answers for fewer analyses than the report holds, and
    the totals guard turns a retried analysis into an unrenderable report.
    """
    inputs: Dict[str, str] = {}
    recommendation: Dict[str, str] = {}
    for result in results:
        if result.status != ReplayStatus.COVERAGE_DIFFERENCE:
            # Every other status is a statement about the analysis as a whole (it
            # was not run, not covered, or reproduced), so both stages carry it.
            inputs[result.row_id] = result.status
            recommendation[result.row_id] = result.status
            continue
        names = [diff.field for diff in result.field_diffs]
        inputs[result.row_id] = (
            ReplayStatus.COVERAGE_DIFFERENCE
            if any(name.startswith(_INPUT_PREFIXES) for name in names)
            else ReplayStatus.COVERAGE_MATCH)
        recommendation[result.row_id] = (
            ReplayStatus.COVERAGE_DIFFERENCE
            if any(name.startswith(_RECOMMENDATION_PREFIX) for name in names)
            else ReplayStatus.COVERAGE_MATCH)
    return {STAGE_EXPERT_INPUTS: inputs, STAGE_RECOMMENDATION: recommendation}


def _run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]


def _child_work_dir(bundle_dir, out_dir) -> Path:
    """Where the job and result files live: beside the report when there is one."""
    base = Path(out_dir) if out_dir is not None else Path(bundle_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base


def child_timeout(job_count: int) -> float:
    """The default child budget for ``job_count`` analyses."""
    return CHILD_TIMEOUT_BASE_S + CHILD_TIMEOUT_PER_ANALYSIS_S * max(job_count, 0)


def _run_child(bundle: SessionBundle, jobs: Sequence[_Job], pinned: PinnedRoot,
               session_id: str, out_dir, stamp: str,
               timeout: Optional[float]) -> Dict[str, Any]:
    """Run every job in ONE child with ``CACHE_FOLDER`` pinned before import.

    One child, not one per analysis: ``CACHE_FOLDER`` is fixed for the whole run,
    so a process per analysis would buy no isolation and pay the (multi-second)
    ba2_providers import for each. Within the child the analyses run in sequence
    and share the provider caches exactly as the bars of a backtest do.
    """
    work_dir = _child_work_dir(bundle.root, out_dir)
    job_path = work_dir / f"{JOB_STEM}_{stamp}.json"
    result_path = work_dir / f"{CHILD_RESULT_STEM}_{stamp}.json"

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
                # The RECORD says which branch the live gather took; the child must
                # not infer it from what the pinned root happens to hold.
                "branch_flags": dict(job.analysis.branch_flags),
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
    # Defence in depth only: the PARENT already refuses to run with this set (it
    # enters replay_isolation, which will not start while the FMP network lock has
    # its escape hatch open), so the child can never inherit it in practice. It is
    # removed anyway so a future caller that bypasses run() cannot hand the child a
    # half-open lock.
    env.pop("BA2_HERMETIC_ALLOW_NETWORK", None)

    budget = child_timeout(len(jobs)) if timeout is None else float(timeout)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "app.services.replay.historical", str(job_path)],
            env=env, cwd=str(backend_dir), capture_output=True, text=True,
            timeout=budget)
    except subprocess.TimeoutExpired as exc:
        # NOT a lost report. The child commits each analysis as it finishes, so
        # everything it got through is already in the store and is still compared;
        # the rest become rows that name the timeout.
        logging.getLogger(__name__).error(
            "the historical child exceeded its %.0fs budget over %d analyses; reporting "
            "what it committed.\nstderr tail:\n%s",
            budget, len(jobs), _tail(_as_text(exc.stderr)))
        return {"analyses": {}, "isolation": None, "timed_out": True,
                "timeout_seconds": budget}

    if not result_path.exists():
        raise HistoricalRunError(
            f"the historical child wrote no result (exit {completed.returncode}).\n"
            f"stderr tail:\n{_tail(completed.stderr)}")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HistoricalRunError(
            f"the historical child's result at {result_path} could not be read "
            f"({type(exc).__name__}: {exc}); exit {completed.returncode}.\n"
            f"stderr tail:\n{_tail(completed.stderr)}") from exc
    if not result.get("ok"):
        raise HistoricalRunError(
            f"the historical child failed: {result.get('error')}\n"
            f"stderr tail:\n{_tail(completed.stderr)}")
    result["timed_out"] = False
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
             child_note: Optional[Dict[str, Any]], decode_object,
             timed_out: bool) -> AnalysisResult:
    """Diff the reconstruction against what live recorded, and classify it."""
    analysis = job.analysis
    if produced is None:
        if timed_out:
            return AnalysisResult.for_analysis(
                analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
                "the historical child ran out of time before reconstructing this analysis; "
                "re-run it with a longer --timeout (nothing about it is inferred here)")
        detail = (child_note or {}).get("detail") or "the child reported nothing"
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
            f"the historical run recorded no analysis for this id ({detail})")

    # A hermetic cache miss means a declared-or-undeclared history was NOT on the
    # pinned root and the reader was handed ``[]`` instead (it logs and continues
    # for the first few symbols). That empty is not data: comparing against it
    # would report a plausible "difference" produced by a gap.
    misses = list((child_note or {}).get("hermetic_misses") or ())
    if misses:
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_MISSING_HISTORY,
            "the reconstruction read an EMPTY history for: " + ", ".join(misses)
            + " -- the pinned root does not hold it, and the hermetic reader answered "
              "with an empty payload rather than failing")

    try:
        raw, path_notes = _branch_diffs(analysis, produced)
        raw += list(_input_diffs(bundle, analysis, produced, decode_object))
        raw += list(_decision_diffs(bundle, analysis, produced, decode_object))
        # COERCE INSIDE THE TRY. Every comparison here is asked for its numeric
        # distance below, so a diff that arrived in some other shape must become a
        # FieldDiff before it can reach that -- and if some future comparison hands
        # back something that cannot, it has to be this analysis's row rather than
        # an AttributeError escaping into the caller and taking the whole report
        # with it, after the child has already done all of the work.
        diffs: List[FieldDiff] = [FieldDiff.coerce(diff) for diff in raw]
    except ReplayMiss as miss:
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
            f"the comparison could not run: {miss}")
    except Exception as exc:  # noqa: BLE001 -- one row's gap, never the report's
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_MISSING_CAPTURE,
            f"the comparison could not run: {type(exc).__name__}: {exc}")

    notes = list(job.coverage_notes) + path_notes
    caveat = f"; coverage notes: {'; '.join(notes)}" if notes else ""
    revision = ("; reconstruction inputs of unknown revision: "
                + "; ".join(job.revision_notes)) if job.revision_notes else ""
    if diffs:
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_DIFFERENCE,
            f"{_detail_for(diffs)}{revision}{caveat}", diffs)
    if job.revision_notes:
        return AnalysisResult.for_analysis(
            analysis, ReplayStatus.COVERAGE_REVISION_UNKNOWN,
            "the reconstruction reproduced the recorded inputs and recommendation, but its "
            "inputs cannot be shown to be the revision live consumed: "
            + "; ".join(job.revision_notes) + caveat)
    return AnalysisResult.for_analysis(
        analysis, ReplayStatus.COVERAGE_MATCH,
        "the historical reconstruction reproduced the recorded inputs and "
        "recommendation" + caveat)


def _detail_for(diffs: Sequence[FieldDiff], limit: int = 5) -> str:
    """The one-line summary of a set of differences, distances included.

    Bounded at ``limit`` fields: the full set is in the row's ``field_diffs`` (and
    in the report's own tables), and a detail line that grows with the diff count
    is unreadable in the coverage row it also becomes.
    """
    changed = ", ".join(_diff_summary(FieldDiff.coerce(diff)) for diff in diffs[:limit])
    return f"{len(diffs)} field(s) differ: {changed}"


def _diff_summary(diff: FieldDiff) -> str:
    """``field (abs 0.2 (16.7%))`` -- the distance belongs in the one-line detail too."""
    delta = diff.delta_text()
    return f"{diff.field} ({delta})" if delta else diff.field


def _branch_diffs(analysis: AnalysisRecord,
                  produced: AnalysisRecord) -> Tuple[List[FieldDiff], List[str]]:
    """Did the reconstruction take the same GATHER BRANCH the live analysis took?

    ``(findings, path notes)``. A branch is a recorded FACT, not something to
    infer from which responses exist, and a bundle rebuilt down a different branch
    can differ in ways no field diff explains -- so the branch is compared in its
    own right rather than left to be guessed at from the inputs.

    The split matters. A flag decided by ``as_of is None``
    (:data:`_PATH_BRANCH_FLAGS`) differs between a live capture and its
    reconstruction BY CONSTRUCTION, and "different endpoints alone are not a
    failure" (spec section 2): it is reported as a path note on whatever row the
    comparison produces. A flag decided by the DATA or the SETTINGS
    (``ds_analyst_key_present``, ``fmp_rating_analyst_grades``) has to reproduce,
    and a mismatch there is a finding of its own.
    """
    findings: List[FieldDiff] = []
    notes: List[str] = []
    recorded = {k: v for k, v in analysis.branch_flags.items()
                if k not in _HOST_BRANCH_FLAGS}
    rebuilt = {k: v for k, v in produced.branch_flags.items()
               if k not in _HOST_BRANCH_FLAGS}
    for name in sorted(set(recorded) | set(rebuilt)):
        was = recorded.get(name, "<not recorded>")
        now = rebuilt.get(name, "<not recorded>")
        if was == now:
            continue
        if name in _PATH_BRANCH_FLAGS:
            notes.append(f"branch.{name}: live {was!r} -> historical {now!r} (the two "
                         f"paths take this branch differently by construction)")
        else:
            findings.append(FieldDiff(field=f"branch.{name}", recorded=repr(was),
                                      produced=repr(now)))
    return findings, notes


def _input_diffs(bundle: SessionBundle, analysis: AnalysisRecord,
                 produced: AnalysisRecord, decode_object) -> List[Any]:
    """The "Expert inputs" stage: the two normalized ``_gather`` bundles, field by field."""
    if produced.bundle_capture_status != ReplayStatus.CAPTURE_CAPTURED or \
            produced.bundle_object is None:
        return [FieldDiff(
            field="inputs", recorded="<the recorded bundle>",
            produced=f"the historical gather produced no bundle "
                     f"({produced.bundle_capture_status}: {produced.error or 'no detail'})")]
    recorded = bundle.decode(analysis.bundle_object)
    rebuilt = decode_object(produced.bundle_object)
    return compare_values(recorded, rebuilt, "inputs", with_deltas=True)


def _decision_diffs(bundle: SessionBundle, analysis: AnalysisRecord,
                    produced: AnalysisRecord, decode_object) -> List[Any]:
    """The "Recommendation" stage: outcome first, then every recommendation field."""
    if analysis.outcome != produced.outcome:
        return [FieldDiff(
            field="recommendation.outcome",
            recorded=_outcome_text(analysis.outcome, analysis.skip_reason, analysis.error),
            produced=_outcome_text(produced.outcome, produced.skip_reason, produced.error))]
    if analysis.outcome == ReplayStatus.OUTCOME_ERROR:
        if analysis.error == produced.error:
            return []
        return [FieldDiff(field="recommendation.error", recorded=str(analysis.error),
                          produced=str(produced.error))]

    diffs: List[Any] = []
    if analysis.outcome == ReplayStatus.OUTCOME_SKIP and \
            analysis.skip_reason != produced.skip_reason:
        diffs.append(FieldDiff(field="recommendation.skip_reason",
                               recorded=str(analysis.skip_reason),
                               produced=str(produced.skip_reason)))
    if analysis.recommendation_object is None or produced.recommendation_object is None:
        # One side has no Recommendation object to compare. Saying "match" would claim
        # the price, details and confidence were checked when nothing compared them.
        if (analysis.recommendation_object is None) != (produced.recommendation_object is None):
            diffs.append(FieldDiff(
                field="recommendation",
                recorded="recorded" if analysis.recommendation_object else "<absent>",
                produced="produced" if produced.recommendation_object else "<absent>"))
        return diffs
    recorded = bundle.decode(analysis.recommendation_object)
    rebuilt = decode_object(produced.recommendation_object)
    for diff in compare_recommendations(recorded, rebuilt, with_deltas=True):
        coerced = FieldDiff.coerce(diff)
        diffs.append(FieldDiff(field=f"recommendation.{coerced.field}",
                               recorded=coerced.recorded, produced=coerced.produced,
                               abs_delta=coerced.abs_delta, rel_delta=coerced.rel_delta,
                               rel_delta_undefined=coerced.rel_delta_undefined))
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
def is_credential_setting(key: str) -> bool:
    """Whether a settings key names a credential, by SEGMENT rather than substring."""
    return any(segment in _CREDENTIAL_SEGMENTS
               for segment in re.split(r"[^a-z0-9]+", str(key).lower()) if segment)


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

    **Every module that imported the function is patched, found by IDENTITY.**
    ``from ba2_common.config import get_app_setting`` binds by value at import, and
    the bindings are not only in ``ba2_providers``: ``ba2_experts.expert_mixins``
    and ``ba2_experts.FMPRating`` hold their own. Walking ``sys.modules`` for the
    ORIGINAL object finds every one of them and cannot mistake an unrelated
    same-named function for a binding to patch.

    The sweep is repeated on EXIT, for the replacement this time. A module imported
    while the context is open -- and a reconstruction imports provider modules
    lazily -- binds the offline closure, and restoring only the modules seen at
    entry would leave that binding refusing every settings read for the rest of the
    process.
    """
    import ba2_common.config as config

    original = config.get_app_setting

    def _offline_get_app_setting(key: str, default: Optional[str] = None) -> Optional[str]:
        if is_credential_setting(key):
            return OFFLINE_API_KEY
        raise ReplayMiss(
            "app_setting", request_identity={"key": key},
            detail="the historical replay does not open the trading database; a setting "
                   "that steers a calculation must come from the recorded settings")

    def _bound_to(function):
        found = [module for module in list(sys.modules.values())
                 if module is not None
                 and getattr(module, "get_app_setting", None) is function]
        if config not in found and config.get_app_setting is function:
            found.append(config)
        return found

    targets = _bound_to(original)
    if config not in targets:
        targets.append(config)
    for module in targets:
        module.get_app_setting = _offline_get_app_setting
    try:
        yield tuple(getattr(m, "__name__", "?") for m in targets)
    finally:
        for module in set(targets) | set(_bound_to(_offline_get_app_setting)):
            module.get_app_setting = original


def _api_key_from_record(entry: Dict[str, Any]):
    """The ``_get_fmp_api_key`` this analysis's RECORD says the live gather had.

    ``DeterministicScorer`` records :data:`DS_ANALYST_KEY_FLAG` at the point it
    reads the key and only fetches analyst rows when the key was truthy. Forcing a
    key here would run the analyst branch for a live analysis that never did, and
    the extra grade/target rows would then read as a reconstruction difference in
    a bundle that was actually reproduced correctly.

    With the flag ABSENT the accessor refuses. That is not the same as "no key":
    the flag is only recorded when ``w_analyst > 0``, i.e. exactly when the key is
    read, so a refusal fires only where the record genuinely cannot answer.
    (``gather_tape._wire_expert`` reads the same flag the same way.)
    """
    flags = entry["branch_flags"]
    analysis_id = entry["analysis_id"]

    if DS_ANALYST_KEY_FLAG not in flags:
        def _refuse():
            raise ReplayMiss(
                "fmp_api_key", analysis_id=analysis_id,
                request_identity={"expert_class": entry["expert_class"]},
                detail=f"the record does not carry {DS_ANALYST_KEY_FLAG}, so replay cannot "
                       f"say whether the live gather held an FMP key; it will not decide "
                       f"that branch on its own")
        return _refuse
    if flags[DS_ANALYST_KEY_FLAG]:
        return lambda: OFFLINE_API_KEY
    return lambda: None


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
    # FMPRating caches its key on the instance and BOTH of its branches need one to
    # address a fetch -- the key's presence is not a branch it records, so an inert
    # placeholder is the whole answer there. DeterministicScorer's key presence IS a
    # recorded branch and is read from the record instead (see _api_key_from_record).
    expert._api_key = OFFLINE_API_KEY
    expert._get_fmp_api_key = _api_key_from_record(entry)

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
    from ba2_providers.fmp_common import hermetic_miss_symbols, reset_hermetic_misses

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
    # A hermetic fmp_history miss returns ``[]`` and only LOGS for the first few
    # symbols, so a reconstruction can be built from a gap and look like data. The
    # registry is process-wide, so it is cleared per analysis and read back after,
    # and any miss makes this analysis missing_history rather than a difference.
    reset_hermetic_misses()
    outcome: Dict[str, Any]
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
        outcome = {"ran": True}
    except (KeyboardInterrupt, SystemExit):
        # An operator stopping the run, or the interpreter tearing down. Neither is
        # a finding about this analysis and neither may be turned into one.
        raise
    except BaseException as exc:  # noqa: BLE001
        # capture_scope has already recorded this as the analysis's outcome and
        # re-raised it; the comparison reads that record. One failing analysis
        # must not abandon the rest of the session.
        outcome = {"ran": True, "raised": f"{type(exc).__name__}: {exc}"}
    outcome["hermetic_misses"] = sorted(hermetic_miss_symbols())
    return outcome


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
        _write_child_result(result_path, {"ok": False, "error": message})
        return 2

    import ba2_common.config as config

    if os.path.abspath(config.CACHE_FOLDER) != os.path.abspath(job["cache_root"]):
        return _fail(
            f"this process imported CACHE_FOLDER={config.CACHE_FOLDER}, but the job pins "
            f"{job['cache_root']}; refusing to reconstruct from a root half the cache "
            f"readers do not point at")

    try:
        return _child_run(job, result_path)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001
        # The child owns its process; an exception here would reach the parent only
        # as "no result file", with the cause buried in a stderr tail. Writing the
        # reason down is what lets the parent raise something an operator can act on.
        import traceback

        return _fail(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


def _write_child_result(result_path: Path, payload: Dict[str, Any]) -> None:
    """Publish the result atomically: the parent must never read a half-written file.

    A partially flushed JSON is indistinguishable, to the parent, from a child that
    failed in a novel way -- and it would be reported as one.
    """
    tmp = result_path.with_name(result_path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, result_path)


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
        # FINALIZE, always. An open session is what
        # ``ReplayStore.mark_interrupted_sessions`` relabels ``interrupted`` at the
        # next startup, so a completed comparison would later describe itself as a
        # crash. Finalizing also drains the writer before the result is published.
        store.finalize_session(session_id, timeout=60.0)
        store.close(timeout=60.0)

    _write_child_result(result_path, {
        "ok": True,
        "session_id": session_id,
        "cache_root": os.path.abspath(config.CACHE_FOLDER),
        "analyses": analyses,
        "isolation": {
            "network_attempts": list(probe.network_attempts),
            "instance_resolutions": list(probe.instance_resolutions),
            "provider_resolutions": list(probe.provider_resolutions),
        },
    })
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through run()
    sys.exit(child_main(sys.argv[1:]))
