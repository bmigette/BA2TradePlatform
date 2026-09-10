"""Recorded expert replay: does the shared calculation still produce that decision?

Spec section 2, first row: "Exact normalized bundle captured immediately before
``_process``, settings, evaluation clock; execute shared expert calculation
again. [...] Same calculation and recorded inputs produce the same
recommendation. Does not validate ``_gather`` or historical reconstruction."

So for each recorded analysis this module decodes the bundle and the settings,
enters replay mode with the recorded clock reads, calls ``_process(bundle,
settings, as_of=None)`` -- exactly the call live made, ``as_of`` still ``None``
so the live branch selector is untouched -- and compares the result to the
recorded one.

Two rules that are easy to lose:

* **The recorded output is never an input.** It is decoded only to compare
  against, after ``_process`` has already returned.
* **Equality is exact.** "numeric equality uses exact serialized values [...]
  No broad 'close enough' tolerance may hide a changed signal, threshold
  crossing, share quantity or stop tick." (spec section 8) Comparison is
  therefore over ``codec.encode`` bytes -- except for DataFrames/Series, whose
  Arrow bytes are environment-dependent BY DESIGN and which are compared
  structurally with ``pandas.testing``.
"""
from __future__ import annotations

import logging
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import pandas as pd

from ba2_common.core.replay import (
    AnalysisRecord,
    CaptureContext,
    ReplayMiss,
    ReplayStatus,
    SessionBundle,
    encode,
    load_bundle,
    use_capture_context,
)
from ba2_common.core.types import Recommendation

from app.services.replay.isolation import refuse, replay_isolation
from app.services.replay.report import AnalysisResult, ReplayReport

__all__ = [
    "RECORDED_EXPERTS",
    "build_replay_expert",
    "compare_values",
    "run",
]

#: The four experts step 2 records. Anything else is ``unsupported`` -- never a
#: green row: "Unsupported expert/provider combinations return ``unsupported``,
#: never a green coverage result." (spec section 5)
RECORDED_EXPERTS: Tuple[str, ...] = (
    "FMPRating",
    "FMPEarningsDrift",
    "FMPInsiderClusterBuy",
    "DeterministicScorer",
)


# --------------------------------------------------------------------------- #
# Building the expert offline
# --------------------------------------------------------------------------- #
def build_replay_expert(expert_class_name: str, analysis: AnalysisRecord):
    """An expert object that can run ``_gather``/``_process`` and NOTHING else.

    ``__new__`` without ``__init__``: the live constructor loads the
    ``ExpertInstance`` row and its settings out of the trading database, which
    replay may not open. The pure pair does not need any of that -- it needs the
    resolved settings (recorded) and a logger. Every seam that WOULD reach a live
    host is replaced with a refusal, so a calculation that unexpectedly reaches
    for a quote, a setting or a provider is a typed miss naming the analysis
    rather than a silent live read.
    """
    from ba2_experts import get_expert_class

    cls = get_expert_class(expert_class_name)
    if cls is None:
        raise refuse("expert_class", analysis_id=analysis.analysis_id,
                     request_identity={"expert_class": expert_class_name},
                     detail="not a known ba2_experts class")
    expert = cls.__new__(cls)
    expert.id = analysis.expert_instance_id
    expert.logger = logging.getLogger(f"replay.{expert_class_name}")
    expert._gather_symbol = analysis.symbol

    def _refuse_quote(symbol):
        raise refuse("quote", analysis_id=analysis.analysis_id,
                     request_identity={"symbol": symbol},
                     detail="the recorded bundle already carries current_price")

    def _refuse_settings(keys):
        raise refuse("settings", analysis_id=analysis.analysis_id,
                     request_identity={"keys": list(keys)},
                     detail="replay uses the RECORDED settings, never a live resolve")

    def _refuse_providers():
        raise refuse("providers", analysis_id=analysis.analysis_id,
                     request_identity={"expert_class": expert_class_name},
                     detail="replay reads providers from the tape, never live")

    expert._get_current_price = _refuse_quote
    expert._resolve_settings = _refuse_settings
    expert._live_providers = _refuse_providers
    return expert


def replay_context(analysis: AnalysisRecord) -> CaptureContext:
    """A replay-mode context feeding back this analysis's recorded clock reads."""
    return CaptureContext.for_replay(
        analysis_id=analysis.analysis_id,
        clock_reads=analysis.clock_reads,
        expert_class=analysis.expert_class,
        symbol=analysis.symbol,
        use_case=analysis.use_case,
        expert_instance_id=analysis.expert_instance_id,
    )


# --------------------------------------------------------------------------- #
# Exact comparison
# --------------------------------------------------------------------------- #
def compare_values(recorded: Any, produced: Any, path: str = "") -> List[Tuple[str, str, str]]:
    """Every difference between two captured values, as ``(path, recorded, produced)``.

    Exact by default (codec bytes), structural for frames. Containers are walked
    so a difference is reported at the field that actually moved rather than as
    "the whole bundle differs".
    """
    label = path or "value"
    if isinstance(recorded, (pd.DataFrame, pd.Series)) or isinstance(produced, (pd.DataFrame, pd.Series)):
        return _compare_frames(recorded, produced, label)
    if isinstance(recorded, dict) and isinstance(produced, dict):
        diffs: List[Tuple[str, str, str]] = []
        for key in sorted(set(recorded) | set(produced), key=str):
            if key not in recorded:
                diffs.append((f"{label}[{key!r}]", "<absent>", _text(produced[key])))
            elif key not in produced:
                diffs.append((f"{label}[{key!r}]", _text(recorded[key]), "<absent>"))
            else:
                diffs += compare_values(recorded[key], produced[key], f"{label}[{key!r}]")
        return diffs
    if isinstance(recorded, (list, tuple)) and isinstance(produced, (list, tuple)):
        if len(recorded) != len(produced):
            return [(f"{label} (length)", str(len(recorded)), str(len(produced)))]
        diffs = []
        for index, (left, right) in enumerate(zip(recorded, produced)):
            diffs += compare_values(left, right, f"{label}[{index}]")
        return diffs
    if _encoded_bytes(recorded, label) == _encoded_bytes(produced, label):
        return []
    return [(label, _text(recorded), _text(produced))]


def _compare_frames(recorded: Any, produced: Any, label: str) -> List[Tuple[str, str, str]]:
    """Frames are compared STRUCTURALLY, not by bytes.

    Arrow IPC bytes carry environment-dependent buffer padding and dictionary
    layout, so two frames that are equal cell for cell can serialize
    differently. ``pandas.testing`` compares dtypes, index, column order and
    every value -- which is what "the same frame" actually means.
    """
    if type(recorded) is not type(produced):
        return [(label, _text(recorded), _text(produced))]
    try:
        if isinstance(recorded, pd.DataFrame):
            pd.testing.assert_frame_equal(recorded, produced, check_exact=True)
        else:
            pd.testing.assert_series_equal(recorded, produced, check_exact=True)
    except AssertionError as exc:
        return [(label, f"{type(recorded).__name__} shape={_shape(recorded)}",
                 f"{type(produced).__name__} shape={_shape(produced)}: {exc}")]
    return []


def _shape(frame: Any) -> str:
    return str(getattr(frame, "shape", "?"))


def _encoded_bytes(value: Any, label: str) -> bytes:
    """The value's exact serialized form; an unencodable value is a loud miss."""
    try:
        return encode(value).data
    except Exception as exc:
        raise refuse("codec", request_identity={"path": label},
                     detail=f"value is not comparable: {exc}") from exc


def _text(value: Any) -> str:
    if isinstance(value, (pd.DataFrame, pd.Series)):
        return f"{type(value).__name__} shape={_shape(value)}"
    return repr(value)


def compare_recommendations(recorded: Recommendation,
                            produced: Any) -> List[Tuple[str, str, str]]:
    """Field-by-field, in declaration order."""
    if not isinstance(produced, Recommendation):
        return [("type", type(recorded).__name__, type(produced).__name__)]
    diffs: List[Tuple[str, str, str]] = []
    for spec in dataclass_fields(Recommendation):
        diffs += compare_values(getattr(recorded, spec.name),
                                getattr(produced, spec.name), spec.name)
    return diffs


# --------------------------------------------------------------------------- #
# One analysis
# --------------------------------------------------------------------------- #
def replay_analysis(bundle: SessionBundle, analysis: AnalysisRecord) -> AnalysisResult:
    """Re-run ``_process`` for one recorded analysis and classify the outcome."""
    def result(status: str, detail: str = "",
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

    if analysis.expert_class not in RECORDED_EXPERTS:
        return result(ReplayStatus.COVERAGE_UNSUPPORTED,
                      f"{analysis.expert_class} is not one of the recorded experts "
                      f"{list(RECORDED_EXPERTS)}")
    if analysis.bundle_capture_status != ReplayStatus.CAPTURE_CAPTURED:
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      f"bundle_capture_status={analysis.bundle_capture_status}")
    if analysis.bundle_object is None:
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      "the record claims a captured bundle but references no object")
    if analysis.settings_object is None:
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      "no recorded settings; replay will not invent them")

    try:
        data_bundle = bundle.decode(analysis.bundle_object)
        settings = bundle.decode(analysis.settings_object)
        expert = build_replay_expert(analysis.expert_class, analysis)
    except ReplayMiss as miss:
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE, str(miss))
    except Exception as exc:  # noqa: BLE001
        # A record this build of the code cannot rebuild (codec drift, a class
        # that moved) is a COVERAGE fact about one analysis, not a reason to
        # abandon the other 500. It is reported as a row naming the failure --
        # never counted as a match, and never swallowed.
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      f"could not rebuild the recorded inputs: {type(exc).__name__}: {exc}")

    produced: Any = None
    raised: Optional[BaseException] = None
    with use_capture_context(replay_context(analysis)):
        try:
            produced = expert._process(data_bundle, settings, as_of=None)
        except ReplayMiss as miss:
            # The calculation reached for something the bundle does not hold (a
            # clock read past the recorded ones, a refused live seam).
            return result(ReplayStatus.COVERAGE_MISSING_CAPTURE, str(miss))
        except BaseException as exc:  # noqa: BLE001
            # An exception is one of the three outcomes a live analysis can have,
            # so it is CAPTURED here and compared below against what the record
            # says live did -- which is the only way "live failed, replay does
            # not" can be reported instead of masked.
            raised = exc

    return _classify(bundle, analysis, produced, raised, result)


def _classify(bundle: SessionBundle, analysis: AnalysisRecord, produced: Any,
              raised: Optional[BaseException], result) -> AnalysisResult:
    """Compare what replay produced against what the record says live produced."""
    if analysis.outcome == ReplayStatus.OUTCOME_ERROR:
        if raised is None:
            return result(ReplayStatus.COVERAGE_DIFFERENCE,
                          "live failed here, replay did not",
                          [("outcome", f"error: {analysis.error}", "recommendation")])
        replayed = f"{type(raised).__name__}: {raised}"
        if replayed == analysis.error:
            return result(ReplayStatus.COVERAGE_MATCH, "error reproduced")
        return result(ReplayStatus.COVERAGE_DIFFERENCE, "a different error",
                      [("error", str(analysis.error), replayed)])

    if raised is not None:
        return result(ReplayStatus.COVERAGE_DIFFERENCE,
                      "replay raised where live did not",
                      [("outcome", analysis.outcome,
                        f"error: {type(raised).__name__}: {raised}")])

    if analysis.outcome == ReplayStatus.OUTCOME_SKIP:
        if not getattr(produced, "skip", False):
            return result(ReplayStatus.COVERAGE_DIFFERENCE, "live skipped, replay did not",
                          [("skip", "True", str(getattr(produced, "skip", None))),
                           ("skip_reason", str(analysis.skip_reason),
                            str(getattr(produced, "skip_reason", None)))])
        replayed_reason = getattr(produced, "skip_reason", None)
        if replayed_reason != analysis.skip_reason:
            return result(ReplayStatus.COVERAGE_DIFFERENCE, "a different skip reason",
                          [("skip_reason", str(analysis.skip_reason), str(replayed_reason))])
        return result(ReplayStatus.COVERAGE_MATCH, f"skip reproduced: {replayed_reason}")

    # outcome == recommendation
    if analysis.recommendation_object is None:
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      "the record claims a recommendation but references no object")
    if getattr(produced, "skip", False):
        return result(ReplayStatus.COVERAGE_DIFFERENCE, "replay skipped, live did not",
                      [("skip", "False", "True"),
                       ("skip_reason", "None", str(getattr(produced, "skip_reason", None)))])
    try:
        recorded = bundle.decode(analysis.recommendation_object)
    except Exception as exc:
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      f"the recorded recommendation could not be decoded: {exc}")
    diffs = compare_recommendations(recorded, produced)
    if not diffs:
        return result(ReplayStatus.COVERAGE_MATCH, "recommendation reproduced exactly")
    changed = ", ".join(name for name, _r, _p in diffs[:5])
    return result(ReplayStatus.COVERAGE_DIFFERENCE,
                  f"{len(diffs)} field(s) differ: {changed}", diffs)


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
def run(bundle_dir, out_dir=None) -> ReplayReport:
    """Replay every recorded analysis in ``bundle_dir``; optionally write the report."""
    bundle = load_bundle(bundle_dir)
    report = ReplayReport(
        session_id=bundle.session.session_id,
        bundle_dir=str(Path(bundle_dir)),
        capability=ReplayStatus.CAPABILITY_RECORDED_EXPERT,
    )
    with replay_isolation():
        for analysis in bundle.analyses:
            report.results.append(replay_analysis(bundle, analysis))
    if out_dir is not None:
        report.write(out_dir)
    return report
