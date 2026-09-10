"""The September 10 bootstrap: a PARTIAL session, and a report that says so.

September 10 predates live capture, so the only thing recoverable is what the
trading database persisted. The one property this tool must have is that it
cannot make that look like a replayable session: every analysis it writes is
``not_attempted``, and ``replay experts`` on the result reports every one of them
as ``missing_capture`` and none as ``match``.

The fixture below is a small SYNTHETIC ``live_inputs.json``: the real file holds
production rows, is not in git, and a test that needed it would be a test that
only runs on one machine.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from ba2_common.core.replay import ReplayStatus, load_bundle

from app.services.replay import expert_replay, gather_tape, inventory

BOOTSTRAP = Path(__file__).resolve().parents[4] / "tools" / "replay_bootstrap_2026_09_10.py"


def _bootstrap_module():
    spec = importlib.util.spec_from_file_location("replay_bootstrap_under_test", BOOTSTRAP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONSENSUS = {"symbol": "RARE", "targetHigh": 60, "targetLow": 16,
             "targetConsensus": 27.69, "targetMedian": 25, "targetCount": 18}
UPGRADES = [{"symbol": "RARE", "strongBuy": 0, "buy": 26, "hold": 6, "sell": 1,
             "strongSell": 0, "consensus": "Buy"}]


@pytest.fixture
def live_inputs(tmp_path) -> Path:
    """A miniature of the real export: two completed analyses and one skip."""
    payload = {
        "captured_at_utc": "2026-09-10T18:52:33.173332+00:00",
        "day": "2026-09-10",
        "production_read_only": True,
        "experts": [
            {"id": 12, "expert": "FMPRating", "account_id": 1},
            {"id": 9, "expert": "FMPEarningsDrift", "account_id": 1},
        ],
        "expert_settings": [],
        "analyses": [
            {"id": 5372, "symbol": "RARE", "expert_instance_id": 12, "status": "COMPLETED",
             "subtype": "ENTER_MARKET", "state": "{}",
             "created_at": "2026-09-10 13:30:01.730645"},
            {"id": 5369, "symbol": "ANAB", "expert_instance_id": 9, "status": "COMPLETED",
             "subtype": "OPEN_POSITIONS", "state": "{}",
             "created_at": "2026-09-10 13:30:01.730645"},
            {"id": 5400, "symbol": "NOPE", "expert_instance_id": 12, "status": "SKIPPED",
             "subtype": "ENTER_MARKET",
             "state": "{\"skip_reason\": \"no_analyst_coverage\"}",
             "created_at": "2026-09-10 13:31:00.000000"},
        ],
        "recommendations": [
            {"id": 1690, "instance_id": 12, "market_analysis_id": 5372, "symbol": "RARE",
             "recommended_action": "BUY", "expected_profit_percent": 61.5,
             "price_at_date": 14.14, "details": "FMP Analyst Price Target Consensus Analysis",
             "confidence": 100.0, "data": "null", "target_price": None,
             "created_at": "2026-09-10 13:30:08.467967"},
            {"id": 1689, "instance_id": 9, "market_analysis_id": 5369, "symbol": "ANAB",
             "recommended_action": "BUY", "expected_profit_percent": 19.5,
             "price_at_date": 55.31, "details": "Post-Earnings-Drift Analysis for ANAB",
             "confidence": 90.0, "data": "null", "target_price": None,
             "created_at": "2026-09-10 13:30:08.467967"},
        ],
        "analysis_outputs": [
            {"id": 23601, "market_analysis_id": 5372, "name": "FMP Consensus API Response",
             "type": "fmp_consensus_response", "text": json.dumps(CONSENSUS)},
            {"id": 23602, "market_analysis_id": 5372, "name": "FMP Upgrade/Downgrade Data",
             "type": "fmp_upgrade_downgrade", "text": json.dumps(UPGRADES)},
            {"id": 23597, "market_analysis_id": 5369, "name": "Earnings Drift Analysis",
             "type": "earnings_drift_analysis", "text": "Post-Earnings-Drift Analysis"},
        ],
        "limitations": "Current transaction state is not an opening-bell snapshot.",
    }
    path = tmp_path / "live_inputs.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def bootstrap_bundle(live_inputs, tmp_path) -> Path:
    return _bootstrap_module().build_bootstrap(live_inputs, tmp_path / "export")


# --------------------------------------------------------------------------- #
# 1. The export
# --------------------------------------------------------------------------- #
def test_every_analysis_is_marked_as_having_no_captured_bundle(bootstrap_bundle):
    bundle = load_bundle(bootstrap_bundle)

    assert len(bundle.analyses) == 3
    assert {a.bundle_capture_status for a in bundle.analyses} == {
        ReplayStatus.CAPTURE_NOT_ATTEMPTED}
    assert all(a.bundle_object is None for a in bundle.analyses)
    assert all(a.clock_reads == () for a in bundle.analyses)
    assert bundle.session.capabilities["partial"] is True
    assert bundle.session.capabilities["normalized_bundles"] is False


def test_the_recommendations_and_the_skip_survive_as_recorded_outcomes(bootstrap_bundle):
    bundle = load_bundle(bootstrap_bundle)
    by_id = {a.analysis_id: a for a in bundle.analyses}

    assert by_id["5372"].outcome == ReplayStatus.OUTCOME_RECOMMENDATION
    recommendation = bundle.decode(by_id["5372"].recommendation_object)
    assert recommendation.signal.name == "BUY"
    assert recommendation.confidence == 100.0
    assert recommendation.current_price == 14.14

    assert by_id["5400"].outcome == ReplayStatus.OUTCOME_SKIP
    assert by_id["5400"].skip_reason == "no_analyst_coverage"
    assert by_id["5369"].use_case == "open_positions"


def test_the_fmprating_payloads_are_observations_with_unknown_provenance(bootstrap_bundle):
    bundle = load_bundle(bootstrap_bundle)
    observations = bundle.observations_for("5372")

    by_method = {o.method: o for o in observations}
    assert set(by_method) == {"price_target_consensus", "upgrade_downgrade_consensus"}
    for observation in observations:
        assert observation.provenance == ReplayStatus.PROVENANCE_UNKNOWN, (
            "a payload read out of a DB row long after the fetch cannot claim a source")
        assert observation.request_identity == {"symbol": "RARE"}
        assert observation.fetched_at is None and observation.published_at is None
    assert bundle.decode(by_method["price_target_consensus"].payload_object) == CONSENSUS
    assert bundle.decode(by_method["upgrade_downgrade_consensus"].payload_object) == UPGRADES
    # Only FMPRating persists its provider payloads: the EarningsDrift analysis has
    # rendered text, which is not a response and is not recorded as one.
    assert bundle.observations_for("5369") == ()


# --------------------------------------------------------------------------- #
# 2. What replay says about it
# --------------------------------------------------------------------------- #
def test_replay_experts_reports_every_analysis_as_missing_capture(bootstrap_bundle):
    report = expert_replay.run(bootstrap_bundle)
    counts = report.counts()

    assert report.total == 3
    assert counts[ReplayStatus.COVERAGE_MISSING_CAPTURE] == 3
    assert counts[ReplayStatus.COVERAGE_MATCH] == 0
    assert counts[ReplayStatus.COVERAGE_DIFFERENCE] == 0
    for result in report.results:
        assert ReplayStatus.CAPTURE_NOT_ATTEMPTED in result.detail


def test_replay_gather_cannot_use_it_either(bootstrap_bundle):
    report = gather_tape.run(bootstrap_bundle)
    assert report.counts()[ReplayStatus.COVERAGE_MISSING_CAPTURE] == 3
    assert report.counts()[ReplayStatus.COVERAGE_MATCH] == 0


def test_the_inventory_shows_exactly_what_september_10_can_support(bootstrap_bundle):
    report = inventory.run(bootstrap_bundle)

    assert report["bundle_capture_status"] == {ReplayStatus.CAPTURE_NOT_ATTEMPTED: 3}
    assert report["analyses_by_expert"] == {"FMPEarningsDrift": 1, "FMPRating": 2}
    assert report["observations_by_provider_method"] == {
        "fmp.price_target_consensus": 1, "fmp.upgrade_downgrade_consensus": 1}
    assert report["clock_reads"]["total"] == 0
    assert report["coverage_by_capability"][
        ReplayStatus.CAPABILITY_RECORDED_EXPERT][ReplayStatus.COVERAGE_NOT_RUN] == 3
