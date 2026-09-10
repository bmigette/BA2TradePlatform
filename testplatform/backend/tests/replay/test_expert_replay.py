"""Recorded expert replay: what a match means, and what a difference must show.

The bundle under test is a REAL captured session (see this package's
``__init__``), so these assertions are about the recording contract end to end:
capture -> export -> load -> re-run -> compare.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ba2_common.core.replay import ReplayStatus, load_bundle

from app.services.replay import expert_replay
from tests.replay import (
    ALL_IDS,
    DRIFT_ID,
    ERROR_ID,
    RATING_ID,
    SCORER_ID,
    SKIP_ID,
    capture_session,
    rebuild_bundle_object,
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
# 2. A changed input is REPORTED, never masked
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
    changed = {name for name, _recorded, _produced in drift.field_diffs}
    assert "signal" in changed, f"the decision change was not reported: {drift.field_diffs}"
    assert {"confidence", "details"} <= changed
    signal_row = next(row for row in drift.field_diffs if row[0] == "signal")
    assert "BUY" in signal_row[1] and "HOLD" in signal_row[2]

    others = {i: r.status for i, r in results.items() if i != DRIFT_ID}
    assert set(others.values()) == {ReplayStatus.COVERAGE_MATCH}, (
        f"one changed input disturbed other analyses: {others}")


def test_the_recorded_recommendation_is_an_EXPECTATION_not_an_input(bundle_copy):
    """Tamper with the recorded OUTPUT: replay must recompute and disagree.

    If the recorded recommendation were fed back as an input (or copied to the
    result), a tampered expectation would still read as a match -- which is the
    single failure mode that would make the whole report worthless.
    """
    manifest_path = bundle_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(a for a in manifest["analyses"] if a["analysis_id"] == RATING_ID)

    from ba2_common.core.replay import encode
    from ba2_common.core.replay.store import ObjectStore
    from ba2_common.core.replay.codec import decode

    store = ObjectStore(bundle_copy)
    kind, data, meta = store.get(entry["recommendation_object"])
    recommendation = decode(kind, data, meta, frames=store.get)
    recommendation.confidence = recommendation.confidence + 11.0
    encoded = encode(recommendation)
    new_hash = store.put(encoded.kind, encoded.data)
    manifest["objects"].append({
        "hash": new_hash, "kind": encoded.kind, "size": len(encoded.data),
        "path": store.relative_path_for(new_hash, encoded.kind).as_posix()})
    entry["recommendation_object"] = new_hash
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    rating = _by_id(expert_replay.run(bundle_copy))[RATING_ID]
    assert rating.status == ReplayStatus.COVERAGE_DIFFERENCE
    assert [name for name, _r, _p in rating.field_diffs] == ["confidence"]


# --------------------------------------------------------------------------- #
# 3. What cannot be replayed says so
# --------------------------------------------------------------------------- #
def test_an_expert_outside_the_recorded_four_is_unsupported(bundle_copy):
    manifest_path = bundle_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(a for a in manifest["analyses"] if a["analysis_id"] == RATING_ID)
    entry["expert_class"] = "ETFTrend"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = _by_id(expert_replay.run(bundle_copy))[RATING_ID]
    assert result.status == ReplayStatus.COVERAGE_UNSUPPORTED
    assert "ETFTrend" in result.detail
    assert result.status != ReplayStatus.COVERAGE_MATCH


def test_an_analysis_without_a_captured_bundle_is_missing_capture(bundle_copy):
    manifest_path = bundle_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(a for a in manifest["analyses"] if a["analysis_id"] == SCORER_ID)
    entry["bundle_capture_status"] = ReplayStatus.CAPTURE_NOT_ATTEMPTED
    entry["bundle_object"] = None
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    report = expert_replay.run(bundle_copy)
    result = _by_id(report)[SCORER_ID]
    assert result.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert report.total == len(ALL_IDS), "an unreplayable analysis was dropped from the total"


# --------------------------------------------------------------------------- #
# 4. The written report
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
