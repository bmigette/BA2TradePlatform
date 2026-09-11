"""Recorded expert replay: what a match means, and what a difference must show.

The bundle under test is a REAL captured session (see this package's
``__init__``), so these assertions are about the recording contract end to end:
capture -> export -> load -> re-run -> compare.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ba2_common.core.replay import (
    ReplayStatus,
    load_bundle,
    replay_now,
    use_capture_context,
)

from app.services.replay import expert_replay
from app.services.replay.expert_replay import replay_context
from tests.replay import (
    ALL_IDS,
    BOTH_PHASE_IDS,
    CALENDAR_ID,
    DRIFT_ID,
    ERROR_ID,
    RATING_ID,
    RECENCY_ID,
    SCORER_ID,
    SKIP_ID,
    capture_session,
    edit_analysis,
    rebuild_bundle_object,
    rebuild_object,
)


@pytest.fixture(scope="module")
def session(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("expert-replay")
    return capture_session(root / "store", root / "export")


@pytest.fixture
def bundle_copy(session, tmp_path) -> Path:
    """A private copy, so a test that tampers with the manifest cannot leak."""
    import shutil

    target = tmp_path / "bundle"
    shutil.copytree(session, target)
    return target


def _by_id(report):
    return {result.analysis_id: result for result in report.results}


def _reads(record, phase):
    return [value for value, read_phase
            in zip(record.clock_reads, record.clock_read_phases) if read_phase == phase]


# --------------------------------------------------------------------------- #
# 1. The recorded decisions reproduce -- all of them, including skip and error
# --------------------------------------------------------------------------- #
def test_every_recorded_analysis_replays_to_a_match(session):
    report = expert_replay.run(session)

    assert report.total == len(ALL_IDS)
    counts = report.counts()
    assert counts[ReplayStatus.COVERAGE_MATCH] == len(ALL_IDS), (
        f"not every analysis reproduced: "
        f"{[(r.analysis_id, r.status, r.detail) for r in report.results]}")
    assert sum(counts.values()) == report.total, "a status was dropped from the totals"


def test_skip_and_error_are_compared_on_outcome_not_ignored(session):
    results = _by_id(expert_replay.run(session))

    skip = results[SKIP_ID]
    assert skip.recorded_outcome == ReplayStatus.OUTCOME_SKIP
    assert skip.status == ReplayStatus.COVERAGE_MATCH
    assert "no consensus data" in skip.detail

    error = results[ERROR_ID]
    assert error.recorded_outcome == ReplayStatus.OUTCOME_ERROR
    assert error.status == ReplayStatus.COVERAGE_MATCH, (
        "an analysis that FAILED live must replay to the same failure, not be skipped")


def test_the_dataframe_bundle_round_trips(session):
    """DeterministicScorer's bundle carries an OHLCV frame; it must survive intact."""
    bundle = load_bundle(session)
    record = next(a for a in bundle.analyses if a.analysis_id == SCORER_ID)
    decoded = bundle.decode(record.bundle_object)

    assert decoded["ohlcv"].shape[0] == 400
    assert list(decoded["ohlcv"].columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert _by_id(expert_replay.run(session))[SCORER_ID].status == ReplayStatus.COVERAGE_MATCH


# --------------------------------------------------------------------------- #
# 2. The clock read handed to _process is the PROCESS one
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("analysis_id", BOTH_PHASE_IDS)
def test_the_process_phase_replays_its_own_read_not_the_gather_one(session, analysis_id):
    """An expert that reads a clock in BOTH halves is the case a flat list breaks.

    FMPRating times its price-target window in ``_gather`` and its rating-recency
    window in ``_process``; EarningsDrift builds its calendar window in ``_gather``
    and ages the report in ``_process``. Replaying either from the front of one
    undifferentiated list hands ``_process`` the gather instant -- quietly, and
    only sometimes with a visible effect.
    """
    bundle = load_bundle(session)
    record = next(a for a in bundle.analyses if a.analysis_id == analysis_id)

    gather_reads = _reads(record, ReplayStatus.PHASE_GATHER)
    process_reads = _reads(record, ReplayStatus.PHASE_PROCESS)
    assert gather_reads and process_reads, "this fixture must read a clock in both halves"
    assert gather_reads[0] != process_reads[0], (
        "the two phases recorded the same instant, so this assertion has no teeth")

    with use_capture_context(replay_context(record, ReplayStatus.PHASE_PROCESS)):
        assert replay_now() == datetime.fromisoformat(process_reads[0])

    with use_capture_context(replay_context(record, ReplayStatus.PHASE_GATHER)):
        assert replay_now() == datetime.fromisoformat(gather_reads[0])


def test_a_bundle_without_phase_tags_refuses_rather_than_guessing(bundle_copy):
    """An older bundle cannot be split back into phases, and does not pretend to."""
    edit_analysis(bundle_copy, RECENCY_ID, clock_read_phases=[])

    result = _by_id(expert_replay.run(bundle_copy))[RECENCY_ID]
    assert result.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "clock_read_phase" in result.detail


# --------------------------------------------------------------------------- #
# 3. A changed input is REPORTED, never masked
# --------------------------------------------------------------------------- #
def test_a_changed_bundle_input_is_reported_as_a_field_difference(bundle_copy):
    """Alter the recorded earnings surprise: the decision moves, and the diff says so."""
    def weaken_surprise(bundle):
        # The provider row carried a +20% beat, comfortably over the 5% entry
        # threshold. A +1% beat is not a signal, so the decision has to move.
        bundle["latest_earnings"]["reported_eps"] = 1.01
        bundle["latest_earnings"]["surprise_percent"] = 1.0

    rebuild_bundle_object(bundle_copy, DRIFT_ID, weaken_surprise)
    results = _by_id(expert_replay.run(bundle_copy))
    drift = results[DRIFT_ID]

    assert drift.status == ReplayStatus.COVERAGE_DIFFERENCE
    changed = {diff.field for diff in drift.field_diffs}
    assert "signal" in changed, f"the decision change was not reported: {drift.field_diffs}"
    assert {"confidence", "details"} <= changed
    signal_row = next(d for d in drift.field_diffs if d.field == "signal")
    assert "BUY" in signal_row.recorded and "HOLD" in signal_row.produced

    others = {i: r.status for i, r in results.items() if i != DRIFT_ID}
    assert set(others.values()) == {ReplayStatus.COVERAGE_MATCH}, (
        f"one changed input disturbed other analyses: {others}")


def test_the_recorded_recommendation_is_an_EXPECTATION_not_an_input(bundle_copy):
    """Tamper with the recorded OUTPUT: replay must recompute and disagree.

    If the recorded recommendation were fed back as an input (or copied to the
    result), a tampered expectation would still read as a match -- the single
    failure mode that would make the whole report worthless.
    """
    def bump(recommendation):
        recommendation.confidence = recommendation.confidence + 11.0

    rebuild_object(bundle_copy, RATING_ID, "recommendation_object", bump)

    rating = _by_id(expert_replay.run(bundle_copy))[RATING_ID]
    assert rating.status == ReplayStatus.COVERAGE_DIFFERENCE
    assert [diff.field for diff in rating.field_diffs] == ["confidence"]


def test_a_skip_compares_every_field_not_only_its_reason(bundle_copy):
    """A skip carries a price and details; changing one must be a difference.

    Recording only the reason left those fields unreplayable: the row would have
    matched on the reason alone while the skip's own text had moved.
    """
    def retext(recommendation):
        recommendation.details = "No analyst coverage (rewritten)"

    rebuild_object(bundle_copy, SKIP_ID, "recommendation_object", retext)

    skip = _by_id(expert_replay.run(bundle_copy))[SKIP_ID]
    assert skip.status == ReplayStatus.COVERAGE_DIFFERENCE
    assert [diff.field for diff in skip.field_diffs] == ["details"]
    assert "skip reproduced but" in skip.detail


def test_a_skip_recorded_without_its_recommendation_is_not_a_match(bundle_copy):
    """The reason alone is not the whole outcome, and must not be reported as it."""
    edit_analysis(bundle_copy, SKIP_ID, recommendation_object=None)

    skip = _by_id(expert_replay.run(bundle_copy))[SKIP_ID]
    assert skip.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "could not be compared" in skip.detail


# --------------------------------------------------------------------------- #
# 4. What cannot be replayed says so -- one row at a time
# --------------------------------------------------------------------------- #
def test_an_expert_outside_the_recorded_four_is_unsupported(bundle_copy):
    edit_analysis(bundle_copy, RATING_ID, expert_class="ETFTrend")

    result = _by_id(expert_replay.run(bundle_copy))[RATING_ID]
    assert result.status == ReplayStatus.COVERAGE_UNSUPPORTED
    assert "ETFTrend" in result.detail


def test_an_analysis_without_a_captured_bundle_is_missing_capture(bundle_copy):
    edit_analysis(bundle_copy, SCORER_ID,
                  bundle_capture_status=ReplayStatus.CAPTURE_NOT_ATTEMPTED,
                  bundle_object=None)

    report = expert_replay.run(bundle_copy)
    result = _by_id(report)[SCORER_ID]
    assert result.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert report.total == len(ALL_IDS), "an unreplayable analysis was dropped from the total"


def test_one_uncomparable_row_does_not_abort_the_whole_report(bundle_copy):
    """A poisoned row is ONE coverage gap, not the end of the run.

    Here the manifest points an analysis's ``recommendation_object`` at a bundle
    object, so the comparison is handed a dict where a ``Recommendation`` should
    be. Before the comparison was contained, that exception escaped ``run()`` and
    every other analysis in the session went unreported.
    """
    manifest = json.loads((bundle_copy / "manifest.json").read_text(encoding="utf-8"))
    scorer = next(a for a in manifest["analyses"] if a["analysis_id"] == SCORER_ID)
    edit_analysis(bundle_copy, CALENDAR_ID,
                  recommendation_object=scorer["bundle_object"])

    report = expert_replay.run(bundle_copy)
    results = _by_id(report)

    assert report.total == len(ALL_IDS)
    assert results[CALENDAR_ID].status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "the comparison could not run" in results[CALENDAR_ID].detail
    healthy = {i: r.status for i, r in results.items() if i != CALENDAR_ID}
    assert set(healthy.values()) == {ReplayStatus.COVERAGE_MATCH}, healthy


# --------------------------------------------------------------------------- #
# 5. The written report
# --------------------------------------------------------------------------- #
def test_the_report_is_written_and_states_what_did_not_run(session, tmp_path):
    out = tmp_path / "report"
    report = expert_replay.run(session, out)
    capability = ReplayStatus.CAPABILITY_RECORDED_EXPERT

    markdown = (out / f"{capability}.md").read_text(encoding="utf-8")
    payload = json.loads((out / f"{capability}.json").read_text(encoding="utf-8"))

    assert "Capability run:" in markdown and capability in markdown
    assert "NOT a live/backtest match" in markdown.replace("**", "")
    for stage in ("Selection", "Rules and sizing", "Execution"):
        row = next(line for line in markdown.splitlines() if line.startswith(f"| {stage} |"))
        assert row.endswith(f"| {ReplayStatus.COVERAGE_NOT_RUN} |"), row
    assert payload["counts"][ReplayStatus.COVERAGE_MATCH] == report.total
    assert set(payload["capabilities_not_run"]) == {
        ReplayStatus.CAPABILITY_GATHER_TAPE,
        ReplayStatus.CAPABILITY_HISTORICAL,
        ReplayStatus.CAPABILITY_DECISION,
    }


def test_the_markdown_and_the_json_agree_about_every_stage(session, tmp_path):
    """One rendering of the stage status, so the two files cannot diverge."""
    out = tmp_path / "report"
    report = expert_replay.run(session, out)
    capability = ReplayStatus.CAPABILITY_RECORDED_EXPERT
    markdown = (out / f"{capability}.md").read_text(encoding="utf-8")
    payload = json.loads((out / f"{capability}.json").read_text(encoding="utf-8"))

    for row in payload["stages"]:
        line = next(l for l in markdown.splitlines() if l.startswith(f"| {row['stage']} |"))
        assert line.rstrip().endswith(f"| {row['status']} |"), (row, line)
    assert [row["status"] for row in payload["stages"]].count(
        ReplayStatus.COVERAGE_NOT_RUN) == 4


def test_a_long_diff_cell_says_that_it_was_truncated(bundle_copy):
    """A silently clipped cell reads like a complete value that ends oddly."""
    from app.services.replay.report import _CELL_LIMIT, _cell

    long_value = "x" * (_CELL_LIMIT + 50)
    rendered = _cell(long_value)
    assert "truncated" in rendered
    assert str(len(long_value)) in rendered
