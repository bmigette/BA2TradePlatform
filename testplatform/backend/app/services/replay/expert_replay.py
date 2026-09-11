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
  against, after ``_process`` has already returned. A recorded SKIP is compared
  the same way: the skipping ``Recommendation`` is recorded too, so its price,
  details and confidence are checked and not just its reason.
* **Equality is exact.** "numeric equality uses exact serialized values [...]
  No broad 'close enough' tolerance may hide a changed signal, threshold
  crossing, share quantity or stop tick." (spec section 8) Comparison is
  therefore over ``codec.encode`` bytes -- except for DataFrames/Series, whose
  Arrow bytes are environment-dependent BY DESIGN and which are compared
  structurally with ``pandas.testing``.
"""
from __future__ import annotations

import logging
import math
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
from ba2_common.logger import logger

from app.services.replay.isolation import refuse, replay_isolation
from app.services.replay.report import AnalysisResult, ReplayReport, merge_coverage

__all__ = [
    "RECORDED_EXPERTS",
    "build_replay_expert",
    "compare_recommendations",
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


def replay_context(analysis: AnalysisRecord, phase: str) -> CaptureContext:
    """A replay-mode context feeding back this analysis's recorded clock reads.

    ``phase`` selects WHICH half's reads are replayed. FMPRating reads a clock in
    both halves -- the price-target window in ``_gather``, the rating-recency
    window in ``_process`` -- so replaying ``_process`` from the front of a flat
    list would hand it the gather instant instead, silently and only sometimes
    visibly.
    """
    return CaptureContext.for_replay(
        analysis_id=analysis.analysis_id,
        clock_reads=analysis.clock_reads,
        clock_read_phases=analysis.clock_read_phases,
        phase=phase,
        expert_class=analysis.expert_class,
        symbol=analysis.symbol,
        use_case=analysis.use_case,
        expert_instance_id=analysis.expert_instance_id,
    )


# --------------------------------------------------------------------------- #
# Exact comparison
# --------------------------------------------------------------------------- #
def compare_values(recorded: Any, produced: Any, path: str = "", *,
                   with_deltas: bool = False) -> List[Any]:
    """Every difference between two captured values, as ``(path, recorded, produced)``.

    Exact by default (codec bytes), structural for frames. Containers are walked
    so a difference is reported at the field that actually moved rather than as
    "the whole bundle differs".

    ``with_deltas`` returns :class:`~app.services.replay.report.FieldDiff` objects
    for EVERY difference -- with the absolute and relative distance attached to the
    numeric leaves and absent from the rest --
    spec section 8 asks the historical comparison for "absolute/relative input
    differences", and a pair of reprs cannot say whether a price moved by a cent
    or by a factor of thirty. It is OFF by default so the recorded-expert and
    gather-tape reports keep their exact previous shape, and it never affects
    EQUALITY: the match criterion is the codec bytes either way, with no
    tolerance anywhere.
    """
    label = path or "value"
    if isinstance(recorded, (pd.DataFrame, pd.Series)) or isinstance(produced, (pd.DataFrame, pd.Series)):
        return _compare_frames(recorded, produced, label, with_deltas=with_deltas)
    if isinstance(recorded, dict) and isinstance(produced, dict):
        diffs: List[Any] = []
        for key in sorted(set(recorded) | set(produced), key=str):
            if key not in recorded:
                diffs.append(_structural_diff(f"{label}[{key!r}]", "<absent>",
                                              _text(produced[key]), with_deltas))
            elif key not in produced:
                diffs.append(_structural_diff(f"{label}[{key!r}]", _text(recorded[key]),
                                              "<absent>", with_deltas))
            else:
                diffs += compare_values(recorded[key], produced[key], f"{label}[{key!r}]",
                                        with_deltas=with_deltas)
        return diffs
    if isinstance(recorded, (list, tuple)) and isinstance(produced, (list, tuple)):
        if len(recorded) != len(produced):
            return [_structural_diff(f"{label} (length)", str(len(recorded)),
                                     str(len(produced)), with_deltas)]
        diffs = []
        for index, (left, right) in enumerate(zip(recorded, produced)):
            diffs += compare_values(left, right, f"{label}[{index}]",
                                    with_deltas=with_deltas)
        return diffs
    if _encoded_bytes(recorded, label) == _encoded_bytes(produced, label):
        return []
    if with_deltas:
        return [_leaf_diff(label, recorded, produced)]
    return [(label, _text(recorded), _text(produced))]


def _is_number(value: Any) -> bool:
    """A real number. ``bool`` is excluded: True/False is a branch, not a magnitude."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _structural_diff(label: str, recorded: str, produced: str, with_deltas: bool):
    """A difference with no distance to report, in whichever shape the caller asked for."""
    if not with_deltas:
        return (label, recorded, produced)
    from app.services.replay.report import FieldDiff

    return FieldDiff(field=label, recorded=recorded, produced=produced)


def _leaf_diff(label: str, recorded: Any, produced: Any):
    """One differing leaf, with the numeric distance attached where there is one.

    A NaN (or an infinity) on either side is NOT a distance. Carrying it would
    abort ``report.write`` (``allow_nan=False``) and, if it ever reached a reader,
    would be a non-number presented as a measurement -- so the row records that the
    distance is not finite and leaves the numbers out.
    """
    from app.services.replay.report import FieldDiff

    if not (_is_number(recorded) and _is_number(produced)):
        return FieldDiff(field=label, recorded=_text(recorded), produced=_text(produced))
    absolute = abs(float(produced) - float(recorded))
    if not math.isfinite(absolute):
        return FieldDiff(field=label, recorded=_text(recorded), produced=_text(produced),
                         delta_not_finite=True)
    if float(recorded) == 0.0:
        return FieldDiff(field=label, recorded=_text(recorded), produced=_text(produced),
                         abs_delta=absolute, rel_delta=None, rel_delta_undefined=True)
    relative = absolute / abs(float(recorded))
    if not math.isfinite(relative):
        return FieldDiff(field=label, recorded=_text(recorded), produced=_text(produced),
                         delta_not_finite=True)
    return FieldDiff(field=label, recorded=_text(recorded), produced=_text(produced),
                     abs_delta=absolute, rel_delta=relative)


def _compare_frames(recorded: Any, produced: Any, label: str, *,
                    with_deltas: bool = False) -> List[Any]:
    """Frames are compared STRUCTURALLY, not by bytes.

    Arrow IPC bytes carry environment-dependent buffer padding and dictionary
    layout, so two frames that are equal cell for cell can serialize
    differently. ``pandas.testing`` compares dtypes, index, column order and
    every value -- which is what "the same frame" actually means.

    With ``with_deltas`` and two ALIGNED frames (same shape, index and columns)
    the numeric cells are additionally measured, and the row carries the LARGEST
    absolute and relative cell distance. A frame-level maximum rather than one
    row per cell: a 600-bar OHLCV series that shifted by a split would otherwise
    emit thousands of rows, and the largest distance is the number that says
    whether the drift is a rounding artefact or a different series.
    """
    if type(recorded) is not type(produced):
        return [_structural_diff(label, _text(recorded), _text(produced), with_deltas)]
    try:
        if isinstance(recorded, pd.DataFrame):
            pd.testing.assert_frame_equal(recorded, produced, check_exact=True)
        else:
            pd.testing.assert_series_equal(recorded, produced, check_exact=True)
    except AssertionError as exc:
        left = f"{type(recorded).__name__} shape={_shape(recorded)}"
        right = f"{type(produced).__name__} shape={_shape(produced)}: {exc}"
        if with_deltas:
            return [_frame_diff(label, recorded, produced, left, right)]
        return [(label, left, right)]
    return []


def _frame_diff(label: str, recorded: Any, produced: Any, left: str, right: str):
    """The frame row, with the largest numeric cell distance when the two align."""
    from app.services.replay.report import FieldDiff

    summary = _frame_numeric_extremes(recorded, produced)
    if summary is None:
        return FieldDiff(field=label, recorded=left, produced=right)
    cells, absolute, relative = summary
    return FieldDiff(
        field=f"{label} (largest of {cells} differing numeric cell(s))",
        recorded=left, produced=right, abs_delta=absolute, rel_delta=relative,
        rel_delta_undefined=relative is None)


def _frame_numeric_extremes(recorded: Any, produced: Any):
    """``(differing cells, max abs delta, max rel delta)`` for two ALIGNED frames.

    ``None`` when they cannot be aligned (a different shape, index or column set
    is a structural difference, not a distance), and ``relative`` is ``None``
    when every differing cell had a zero baseline.
    """
    try:
        left = recorded.to_frame() if isinstance(recorded, pd.Series) else recorded
        right = produced.to_frame() if isinstance(produced, pd.Series) else produced
        if left.shape != right.shape or not left.index.equals(right.index) \
                or not left.columns.equals(right.columns):
            return None
        numeric = [c for c in left.columns
                   if pd.api.types.is_numeric_dtype(left[c])
                   and pd.api.types.is_numeric_dtype(right[c])
                   and left[c].dtype != bool and right[c].dtype != bool]
        if not numeric:
            return None
        delta = (right[numeric].astype(float) - left[numeric].astype(float)).abs()
        differing = delta.gt(0)
        cells = int(differing.to_numpy().sum())
        if not cells:
            return None
        # ``.max().max()`` (pandas, NaN-SKIPPING) rather than numpy's: a single NaN
        # cell -- a bar the reconstruction has no value for -- propagates through
        # ``ndarray.max`` and turns the whole frame's distance into NaN, which then
        # aborts ``report.write``. What is wanted is the largest distance among the
        # cells that HAVE one.
        absolute = float(delta.max().max())
        if not math.isfinite(absolute):
            return None
        base = left[numeric].astype(float).abs()
        ratio = delta.where(differing & base.gt(0)) / base.where(base.gt(0))
        relative = float(ratio.max().max()) if ratio.notna().to_numpy().any() else None
        if relative is not None and not math.isfinite(relative):
            relative = None
        return cells, absolute, relative
    except Exception as exc:  # noqa: BLE001 -- the diff itself already stands
        # NAMED, at WARNING: the diff is still reported without its distance, but a
        # measurement that silently stops being taken is how a report quietly gets
        # less useful with nobody noticing.
        logger.warning(
            f"replay: could not measure the numeric distance between two frames "
            f"({type(exc).__name__}: {exc}); the difference is reported without it",
            exc_info=True)
        return None


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


def compare_recommendations(recorded: Recommendation, produced: Any, *,
                            with_deltas: bool = False) -> List[Any]:
    """Field-by-field, in declaration order. See :func:`compare_values` for ``with_deltas``."""
    if not isinstance(produced, Recommendation):
        return [_structural_diff("type", type(recorded).__name__,
                                 type(produced).__name__, with_deltas)]
    diffs: List[Any] = []
    for spec in dataclass_fields(Recommendation):
        diffs += compare_values(getattr(recorded, spec.name),
                                getattr(produced, spec.name), spec.name,
                                with_deltas=with_deltas)
    return diffs


# --------------------------------------------------------------------------- #
# One analysis
# --------------------------------------------------------------------------- #
def replay_analysis(bundle: SessionBundle, analysis: AnalysisRecord) -> AnalysisResult:
    """Re-run ``_process`` for one recorded analysis and classify the outcome."""
    def result(status: str, detail: str = "",
               field_diffs: Sequence[Tuple[str, str, str]] = ()) -> AnalysisResult:
        return AnalysisResult.for_analysis(analysis, status, detail, field_diffs)

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
    with use_capture_context(replay_context(analysis, ReplayStatus.PHASE_PROCESS)):
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

    try:
        return _classify(bundle, analysis, produced, raised, result)
    except ReplayMiss as miss:
        # The COMPARISON needed something it could not get -- a field the codec
        # refuses, a recorded object that will not decode. That is one analysis's
        # coverage gap; it must not abort the report for the other 500.
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      f"the comparison could not run: {miss}")
    except Exception as exc:  # noqa: BLE001 -- same containment, one row at a time
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      f"the comparison could not run: {type(exc).__name__}: {exc}")


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
        if analysis.recommendation_object is None:
            # A skip recorded WITHOUT its Recommendation (an older bundle, or the
            # Sep-10 bootstrap): the reason matched, and that is all this session
            # can answer for. Saying "match" would claim the skip's price, details
            # and confidence were checked when nothing compared them.
            return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                          f"the skip reason reproduced ({replayed_reason}) but the "
                          f"skipping recommendation was not recorded, so its other "
                          f"fields could not be compared")
        recorded = bundle.decode(analysis.recommendation_object)
        diffs = compare_recommendations(recorded, produced)
        if not diffs:
            return result(ReplayStatus.COVERAGE_MATCH, f"skip reproduced: {replayed_reason}")
        changed = ", ".join(name for name, _r, _p in diffs[:5])
        return result(ReplayStatus.COVERAGE_DIFFERENCE,
                      f"the skip reproduced but {len(diffs)} field(s) differ: {changed}",
                      diffs)

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
    # Running a capability IS the coverage fact, so it is written back into the
    # bundle: otherwise `replay inventory` still reports `not_run` for a
    # capability that has just been run, and the two commands disagree about the
    # same session.
    merge_coverage(bundle_dir, report)
    if out_dir is not None:
        report.write(out_dir)
    return report
