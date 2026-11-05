"""DeterministicScorer: record with TODAY'S code, replay both ways, expect a match.

The prod store holds recordings made by older code; the replay layer has a compatibility rule for them
(``test_gather_tape.py``). These tests pin the other half: a recording made by the CURRENT code (decision
price from the account quote, per-session OHLCV refetch, ``knowable_daily_end`` slice, ``no_price`` skip)
must be replayable by the CURRENT replay -- by BOTH comparisons:

* ``expert_replay`` (recorded bundle -> ``_process``): the comparison the owner relies on, ~99% on prod;
* ``gather_tape`` (recorded provider returns -> the live ``_gather``).

The capture goes through the same ``_analysis_capture`` + ``_gather_and_process`` pair the live
``run_analysis`` uses (see ``tests/replay/__init__.py``), with a real store installed.
"""
from __future__ import annotations

import functools
from pathlib import Path

import pytest

from ba2_common.core.replay import ReplayStatus

from app.services.replay import expert_replay, gather_tape
from tests.replay import capture_session, scorer_case

FIRST, SECOND, NO_PRICE, THIN = "2001", "2002", "2003", "2004"


def _by_id(report):
    return {result.analysis_id: result for result in report.results}


@pytest.fixture(scope="module")
def session(tmp_path_factory) -> Path:
    """Four scorer analyses, recorded by one process in this order.

    FIRST and SECOND are the SAME symbol with the per-process memos left in place between them, which is
    how a second live analysis of a pass is served (its frame, the index closes and the macro series come
    from memory). NO_PRICE is a broker outage; THIN has too little history.
    """
    root = tmp_path_factory.mktemp("scorer-roundtrip")
    builders = (
        (FIRST, functools.partial(scorer_case, FIRST)),
        (SECOND, functools.partial(scorer_case, SECOND, reset_caches=False)),
        (NO_PRICE, functools.partial(scorer_case, NO_PRICE, quote=None)),
        (THIN, functools.partial(scorer_case, THIN, bars=10)),
    )
    return capture_session(root / "store", root / "export", builders)


# --------------------------------------------------------------------------- #
# expert_replay: the recorded bundle through _process
# --------------------------------------------------------------------------- #
def test_every_new_format_scorer_recording_replays_through_process(session):
    results = _by_id(expert_replay.run(session))
    for analysis_id in (FIRST, SECOND, NO_PRICE, THIN):
        result = results[analysis_id]
        assert result.status == ReplayStatus.COVERAGE_MATCH, f"{analysis_id}: {result.detail}"


def test_the_no_price_skip_is_recorded_and_reproduced(session):
    result = _by_id(expert_replay.run(session))[NO_PRICE]
    assert result.status == ReplayStatus.COVERAGE_MATCH
    assert "no_price" in result.detail


def test_the_insufficient_history_skip_is_recorded_and_reproduced(session):
    result = _by_id(expert_replay.run(session))[THIN]
    assert result.status == ReplayStatus.COVERAGE_MATCH
    assert "insufficient_history" in result.detail


# --------------------------------------------------------------------------- #
# gather_tape: the live _gather from the recorded provider returns
# --------------------------------------------------------------------------- #
def test_every_new_format_scorer_recording_replays_through_the_gather_tape(session):
    results = _by_id(gather_tape.run(session))
    for analysis_id in (FIRST, SECOND, NO_PRICE, THIN):
        result = results[analysis_id]
        assert result.status == ReplayStatus.COVERAGE_MATCH, f"{analysis_id}: {result.detail}"
        assert "legacy_frame_close" not in result.detail, (
            f"{analysis_id}: a new-format recording must not take the legacy rule")


def test_an_analysis_served_from_the_process_memos_still_records_the_reads_it_made(session):
    """The second analysis never reached the provider for its own frame or for the index: the module
    memoises both. Those reads are part of what its gather DID (it read the clock for them and used
    their values), so the recording must say so -- otherwise the replay issues requests the tape never
    held and the row is a ``missing_capture`` for a recording that is complete as far as live is
    concerned."""
    from ba2_common.core.replay import load_bundle

    bundle = load_bundle(session)
    reads = {}
    for observation in bundle.observations:
        if observation.method == "get_ohlcv_data":
            for analysis_id in observation.analysis_ids:
                reads.setdefault(analysis_id, []).append(observation.request_identity["symbol"])
    assert sorted(reads[FIRST]) == ["GOOG", "SPY"]
    assert sorted(reads[SECOND]) == ["GOOG", "SPY"]
