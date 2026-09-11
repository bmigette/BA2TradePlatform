"""Gather-tape replay: the LIVE ``_gather`` re-run from the recorded returns.

This is the comparison that separates a mapping/shortcut defect from a
calculation defect, so the things it must get right are: (1) a bundle rebuilt
from the tape is byte-for-byte the bundle live normalized; (2) a response the
tape does not hold stops that ONE comparison -- it never triggers a real request
and never falls through to a historical endpoint; and (3) the BRANCH the live
gather took is read off the record, never inferred from what the tape happens to
contain.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ba2_common.core.replay import (
    CaptureContext,
    CaptureHealth,
    ReplayMiss,
    ReplayStatus,
    load_bundle,
    use_capture_context,
)

from app.services.replay import gather_tape
from app.services.replay.gather_tape import ReplayTape
from tests.replay import (
    ALL_IDS,
    CALENDAR_ID,
    DRIFT_ID,
    INSIDER_ID,
    NOW,
    RATING_ID,
    SCORER_ID,
    TapedOHLCV,
    capture_session,
    drop_observations,
    edit_analysis,
    _scorer_frame,
)

#: The analyses whose whole live gather routes through TAPPED boundaries -- all of
#: them, including DeterministicScorer: its statements, its dated analyst history
#: and its FRED macro reads are tapped as of the second delivery, and the windows
#: they ask for come from recorded clock reads, so the replayed requests are the
#: recorded ones.
TAPE_SERVEABLE = ALL_IDS


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


def test_totals_cover_every_analysis(session):
    report = gather_tape.run(session)
    assert report.total == len(ALL_IDS)
    assert report.capability == ReplayStatus.CAPABILITY_GATHER_TAPE


# --------------------------------------------------------------------------- #
# 2. DeterministicScorer: a reproducible OHLCV identity, a declared gap after it
# --------------------------------------------------------------------------- #
def test_the_scorer_ohlcv_request_identity_is_reproducible_under_replay():
    """The window DS asks for must be the SAME window when the read is replayed.

    Both ends of that window used to be a raw ``datetime.now()``, and the window
    is part of the request identity -- so every recorded DS OHLCV frame was
    unmatchable by construction, no matter how complete the tape was. The read
    now goes through ``replay_now``, so the replayed request is identical.
    """
    from ba2_experts.DeterministicScorer import data

    frame = _scorer_frame(40)

    def _providers(provider):
        class _Bundle:
            def ohlcv(self):
                return provider
        return _Bundle()

    captured = TapedOHLCV(frame)
    capture = CaptureContext(
        analysis_meta={"analysis_id": "A", "attempt_id": "A", "session_id": "S",
                       "expert_class": "DeterministicScorer", "expert_instance_id": 1,
                       "symbol": "GOOG", "use_case": "enter_market",
                       "scheduled_at": None, "started_at": NOW},
        health=CaptureHealth())
    with use_capture_context(capture):
        capture.set_phase(ReplayStatus.PHASE_GATHER)
        data.reset_caches()
        data.fetch_ohlcv(_providers(captured), "GOOG", None)
    record = capture.build_record()
    assert record.clock_reads, "the OHLCV window must come from a RECORDED clock read"

    replayed = TapedOHLCV(frame)
    with use_capture_context(CaptureContext.for_replay(
            analysis_id="A", clock_reads=record.clock_reads,
            clock_read_phases=record.clock_read_phases,
            phase=ReplayStatus.PHASE_GATHER)):
        data.reset_caches()
        data.fetch_ohlcv(_providers(replayed), "GOOG", None)

    assert captured.windows == replayed.windows, (
        "the replayed OHLCV request is not the recorded one")
    # And the recorded observation carries that same identity, so the tape can
    # actually be keyed on it.
    recorded_identity = capture.observations[0].observation.request_identity
    assert recorded_identity["end_date"] == captured.windows[0]["end_date"].isoformat()
    assert recorded_identity["end_date"] == record.clock_reads[0], (
        "the window still ends at a wall clock nobody recorded")


def test_the_scorer_gather_is_served_from_the_tape_end_to_end(session):
    """Every DS boundary -- OHLCV, statements, analyst history, FRED -- replays.

    This row used to be a declared ``missing_capture`` naming the statements read:
    the boundaries under ``data.fetch_statements``/``fetch_macro_series`` carried
    no tap, so nothing about them was ever recorded. It is a MATCH now, which is
    the only honest way to say that the gap is closed.
    """
    result = _by_id(gather_tape.run(session))[SCORER_ID]
    assert result.status == ReplayStatus.COVERAGE_MATCH, result.detail


@pytest.mark.parametrize("method,named", [
    ("get_balance_sheet", "get_balance_sheet"),
    ("get_series_as_of", "get_series_as_of"),
    ("grades_historical", "grades_historical"),
])
def test_dropping_one_scorer_response_names_the_request_it_cannot_serve(
        bundle_copy, method, named):
    """A boundary whose response is gone is a NAMED miss, never a quiet fallback.

    Each of these is a read the scorer's gather makes through a different kind of
    seam -- a provider method, a module-level macro file, a module-level FMP
    fetcher -- so one parametrization per seam is one regression each.
    """
    assert drop_observations(bundle_copy, SCORER_ID, method) >= 1

    result = _by_id(gather_tape.run(bundle_copy))[SCORER_ID]
    assert result.status == ReplayStatus.COVERAGE_MISSING_CAPTURE, result.detail
    assert named in result.detail


def test_a_record_that_does_not_say_whether_the_scorer_had_an_fmp_key_is_a_miss(
        bundle_copy):
    """The analyst branch comes from the RECORD, like every other branch.

    Without the flag the replay would have to infer "was there an API key?" from
    whether the tape happens to hold an analyst response -- and a missing response
    would then reroute it down the no-coverage branch, whose empty grades/target
    lists compare as a plausible DIFFERENCE instead of the missing capture it is.
    """
    edit_analysis(bundle_copy, SCORER_ID, branch_flags={"as_of_is_none": True})

    result = _by_id(gather_tape.run(bundle_copy))[SCORER_ID]
    assert result.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "branch" in result.detail


# --------------------------------------------------------------------------- #
# 3. The branch comes from the RECORD, not from the tape
# --------------------------------------------------------------------------- #
def test_dropping_the_calendar_response_misses_instead_of_switching_branch(bundle_copy):
    """The hazard: the OTHER branch would run and its bundle would look like a match.

    ``_gather``'s calendar shortcut is chosen by the provider TYPE, and its body
    is wrapped in a best-effort ``except``. Infer the branch from the tape and a
    missing calendar response quietly reroutes the replay down the per-symbol
    branch; swallow the miss inside that ``except`` and the reroute is silent too.
    """
    assert drop_observations(bundle_copy, CALENDAR_ID, "earning_calendar") == 1

    results = _by_id(gather_tape.run(bundle_copy))
    calendar = results[CALENDAR_ID]
    assert calendar.status == ReplayStatus.COVERAGE_MISSING_CAPTURE, calendar.detail
    assert "earning_calendar" in calendar.detail
    assert calendar.status != ReplayStatus.COVERAGE_MATCH

    for analysis_id in TAPE_SERVEABLE:
        if analysis_id == CALENDAR_ID:
            continue
        assert results[analysis_id].status == ReplayStatus.COVERAGE_MATCH


def test_a_record_that_does_not_say_which_branch_ran_is_a_miss(bundle_copy):
    edit_analysis(bundle_copy, CALENDAR_ID, branch_flags={"use_case": "enter_market"})

    result = _by_id(gather_tape.run(bundle_copy))[CALENDAR_ID]
    assert result.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "branch" in result.detail


def test_a_recorded_backtest_branch_is_not_replayed_as_a_live_gather(bundle_copy):
    edit_analysis(bundle_copy, RATING_ID,
                  branch_flags={"fmp_rating_branch": "as_of_reconstruction"})

    result = _by_id(gather_tape.run(bundle_copy))[RATING_ID]
    assert result.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "LIVE snapshot branch only" in result.detail


# --------------------------------------------------------------------------- #
# 4. A missing response stops THAT comparison and nothing else
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
# 5. The tape itself
# --------------------------------------------------------------------------- #
def _tape(session, analysis_id):
    bundle = load_bundle(session)
    analysis = next(a for a in bundle.analyses if a.analysis_id == analysis_id)
    return ReplayTape(bundle, analysis)


def test_the_tape_serves_each_recorded_return_once_then_misses(session):
    tape = _tape(session, DRIFT_ID)
    identity = tape.identities("provider_cache", "past_earnings_get")[0]

    first = tape.take("provider_cache", "past_earnings_get", identity)
    assert first["earnings"], "the recorded payload came back empty"
    with pytest.raises(ReplayMiss) as excinfo:
        tape.take("provider_cache", "past_earnings_get", identity)
    assert "tape_exhausted" in str(excinfo.value), (
        "a second read must miss, not hand back the first response again")


def test_an_unknown_identity_misses_and_names_the_request(session):
    tape = _tape(session, DRIFT_ID)

    with pytest.raises(ReplayMiss) as excinfo:
        tape.take("provider_cache", "past_earnings_get", {"symbol": "NOT-RECORDED"})
    message = str(excinfo.value)
    assert "NOT-RECORDED" in message and DRIFT_ID in message


def test_a_non_finite_identity_value_is_a_miss_not_a_difference(session):
    """``NaN != NaN``: such a request can never be matched, so it is a coverage gap.

    Reporting it as a ``difference`` would claim the calculation disagreed, when
    in fact the lookup could never have succeeded.
    """
    tape = _tape(session, DRIFT_ID)

    with pytest.raises(ReplayMiss) as excinfo:
        tape.take("provider_cache", "past_earnings_get",
                  {"symbol": "MSFT", "lookback_periods": float("nan")})
    assert excinfo.value.kind == "tape_identity_non_finite"


def test_an_unrepresentable_identity_is_refused_not_repr_keyed(session):
    """No ``default=repr``: a key built from an object address is not an identity."""
    tape = _tape(session, DRIFT_ID)

    class _Opaque:
        __slots__ = ()

    # sanitize_identity renders an unknown object as its repr, which for a
    # __slots__ class without __repr__ carries a memory address -- the value the
    # tape must never key on. It is stable only within one process run, so an
    # accidental "match" would be an artefact of allocation.
    key_one = _Opaque()
    key_two = _Opaque()
    with pytest.raises(ReplayMiss):
        tape.take("provider_cache", "past_earnings_get", {"symbol": key_one})
    with pytest.raises(ReplayMiss):
        tape.take("provider_cache", "past_earnings_get", {"symbol": key_two})
