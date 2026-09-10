"""Gather-tape replay: the LIVE ``_gather`` re-run from the recorded returns.

This is the comparison that separates a mapping/shortcut defect from a
calculation defect, so the two things it must get right are (1) a bundle
rebuilt from the tape is byte-for-byte the bundle live normalized, and (2) a
response the tape does not hold stops that ONE comparison -- it never triggers a
real request and never falls through to a historical endpoint.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ba2_common.core.replay import ReplayStatus

from app.services.replay import gather_tape
from app.services.replay.gather_tape import ReplayTape
from tests.replay import (
    ALL_IDS,
    DRIFT_ID,
    INSIDER_ID,
    RATING_ID,
    SCORER_ID,
    capture_session,
    drop_observations,
)

#: The analyses whose whole live gather routes through TAPPED boundaries.
#: DeterministicScorer is deliberately absent: its statements/macro/index reads
#: bypass the tapped provider methods, so its gather cannot be served from a tape
#: in this delivery -- an explicit coverage gap, asserted below rather than left
#: to be discovered as a mystery `difference`.
TAPE_SERVEABLE = tuple(i for i in ALL_IDS if i != SCORER_ID)


@pytest.fixture(scope="module")
def session(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("gather-tape")
    return capture_session(root / "store", root / "export")


@pytest.fixture
def bundle_copy(session, tmp_path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(session, target)
    return target


def _by_id(report):
    return {result.analysis_id: result for result in report.results}


# --------------------------------------------------------------------------- #
# 1. The tape rebuilds the recorded bundle
# --------------------------------------------------------------------------- #
def test_the_tape_reproduces_every_serveable_gather(session):
    results = _by_id(gather_tape.run(session))

    for analysis_id in TAPE_SERVEABLE:
        result = results[analysis_id]
        assert result.status == ReplayStatus.COVERAGE_MATCH, (
            f"{analysis_id} ({result.expert_class}): {result.detail}")


def test_a_boundary_that_was_never_tapped_is_missing_capture_not_a_difference(session):
    """DeterministicScorer's gather is not tape-serveable, and the report says why."""
    result = _by_id(gather_tape.run(session))[SCORER_ID]

    assert result.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "replay miss" in result.detail
    assert "get_ohlcv_data" in result.detail


def test_totals_cover_every_analysis(session):
    report = gather_tape.run(session)
    assert report.total == len(ALL_IDS)
    assert report.capability == ReplayStatus.CAPABILITY_GATHER_TAPE


# --------------------------------------------------------------------------- #
# 2. A missing response stops THAT comparison and nothing else
# --------------------------------------------------------------------------- #
def test_a_deleted_observation_misses_for_that_analysis_only(bundle_copy):
    removed = drop_observations(bundle_copy, INSIDER_ID, "insider_get")
    assert removed == 1

    results = _by_id(gather_tape.run(bundle_copy))
    insider = results[INSIDER_ID]
    assert insider.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "insider_get" in insider.detail
    assert "no recorded observation" in insider.detail

    for analysis_id in TAPE_SERVEABLE:
        if analysis_id == INSIDER_ID:
            continue
        assert results[analysis_id].status == ReplayStatus.COVERAGE_MATCH, (
            f"deleting one observation disturbed {analysis_id}")


def test_a_deleted_quote_observation_is_a_miss_not_a_live_price(bundle_copy):
    """The quote is served from the tape too; without it there is no fallback."""
    assert drop_observations(bundle_copy, RATING_ID, "get_instrument_current_price") == 1

    result = _by_id(gather_tape.run(bundle_copy))[RATING_ID]
    assert result.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "get_instrument_current_price" in result.detail


# --------------------------------------------------------------------------- #
# 3. The tape itself
# --------------------------------------------------------------------------- #
def test_the_tape_serves_each_recorded_return_once_then_misses(session):
    from ba2_common.core.replay import ReplayMiss, load_bundle

    bundle = load_bundle(session)
    analysis = next(a for a in bundle.analyses if a.analysis_id == DRIFT_ID)
    tape = ReplayTape(bundle, analysis)
    identity = tape.identities("provider_cache", "past_earnings_get")[0]

    first = tape.take("provider_cache", "past_earnings_get", identity)
    assert first["earnings"], "the recorded payload came back empty"
    with pytest.raises(ReplayMiss) as excinfo:
        tape.take("provider_cache", "past_earnings_get", identity)
    assert "tape_exhausted" in str(excinfo.value), (
        "a second read must miss, not hand back the first response again")


def test_an_unknown_identity_misses_and_names_the_request(session):
    from ba2_common.core.replay import ReplayMiss, load_bundle

    bundle = load_bundle(session)
    analysis = next(a for a in bundle.analyses if a.analysis_id == DRIFT_ID)
    tape = ReplayTape(bundle, analysis)

    with pytest.raises(ReplayMiss) as excinfo:
        tape.take("provider_cache", "past_earnings_get", {"symbol": "NOT-RECORDED"})
    message = str(excinfo.value)
    assert "NOT-RECORDED" in message and DRIFT_ID in message
