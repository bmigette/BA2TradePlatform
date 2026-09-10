"""Inventory and the CLI: every recorded analysis is counted, nothing is dropped.

"Include HOLD/skipped/failed analyses in totals; never report 100% by dropping
unavailable rows." (spec section 8) The skip and the error row are the ones a
counting bug loses first, so they are asserted by name.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from ba2_common.core.replay import ReplayStatus

from app.services.replay import inventory
from app.services.replay.report import STATUS_ORDER
from tests.replay import ALL_IDS, capture_session

#: repo/testplatform/backend/tests/replay/<this file>
LAUNCHER = Path(__file__).resolve().parents[3] / "ba2test_launcher.py"


@pytest.fixture(scope="module")
def session(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("inventory")
    return capture_session(root / "store", root / "export")


def _launcher():
    spec = importlib.util.spec_from_file_location("ba2test_launcher_under_test", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 1. Inventory counts everything
# --------------------------------------------------------------------------- #
def test_totals_include_the_skip_and_the_error(session):
    report = inventory.run(session)

    assert report["totals"]["analyses"] == len(ALL_IDS)
    assert report["analyses_by_outcome"] == {
        ReplayStatus.OUTCOME_ERROR: 1,
        ReplayStatus.OUTCOME_RECOMMENDATION: 4,
        ReplayStatus.OUTCOME_SKIP: 1,
    }
    assert sum(report["analyses_by_expert"].values()) == len(ALL_IDS)
    assert report["analyses_by_expert"] == {
        "DeterministicScorer": 1, "FMPEarningsDrift": 1,
        "FMPInsiderClusterBuy": 2, "FMPRating": 2}
    assert report["skip_reasons"] == {"no consensus data": 1}


def test_capture_status_observations_and_clock_reads_are_broken_out(session):
    report = inventory.run(session)

    assert report["bundle_capture_status"] == {ReplayStatus.CAPTURE_CAPTURED: len(ALL_IDS)}
    methods = report["observations_by_provider_method"]
    assert methods["broker.get_instrument_current_price"] == 5
    assert methods["provider_cache.insider_get"] == 2
    assert methods["fmp.price_target_consensus"] == 2
    assert sum(methods.values()) == report["totals"]["observations"]
    assert report["observations_by_provenance"][ReplayStatus.PROVENANCE_NETWORK] == 5

    clock = report["clock_reads"]
    assert clock["analyses_with_reads"] + clock["analyses_without_reads"] == len(ALL_IDS)
    assert clock["total"] >= clock["analyses_with_reads"] >= 1


def test_a_capability_with_no_rows_is_not_run_for_every_analysis(session):
    report = inventory.run(session)
    coverage = report["coverage_by_capability"]

    for capability in (ReplayStatus.CAPABILITY_HISTORICAL, ReplayStatus.CAPABILITY_DECISION):
        assert coverage[capability][ReplayStatus.COVERAGE_NOT_RUN] == len(ALL_IDS), (
            f"{capability} has no rows and must read not_run, not vanish")
        assert set(coverage[capability]) == set(STATUS_ORDER)


def test_the_written_inventory_names_the_session_and_the_gaps(session, tmp_path):
    out = tmp_path / "inv"
    report = inventory.run(session, out)

    payload = json.loads((out / inventory.INVENTORY_NAME).read_text(encoding="utf-8"))
    assert payload == report
    markdown = (out / "inventory.md").read_text(encoding="utf-8")
    assert "Replay session inventory" in markdown
    assert "S-REPLAY-TEST" in markdown
    assert "recommendation, skip and error" in markdown


# --------------------------------------------------------------------------- #
# 2. The CLI
# --------------------------------------------------------------------------- #
@pytest.fixture
def launcher_cwd():
    """``main()`` chdirs into backend/; put the caller's cwd back afterwards."""
    original = os.getcwd()
    try:
        yield
    finally:
        os.chdir(original)


@pytest.mark.parametrize("command", ["inventory", "experts", "gather"])
def test_the_cli_parses_and_runs_each_replay_command(command, session, tmp_path,
                                                     launcher_cwd, capsys):
    module = _launcher()
    out = tmp_path / command
    code = module.main(["replay", command, "--bundle", str(session), "--out", str(out)])
    printed = capsys.readouterr().out

    assert code == 0, printed
    assert out.is_dir()
    if command == "inventory":
        assert (out / inventory.INVENTORY_NAME).is_file()
        assert "Replay session inventory" in printed
    else:
        capability = (ReplayStatus.CAPABILITY_RECORDED_EXPERT if command == "experts"
                      else ReplayStatus.CAPABILITY_GATHER_TAPE)
        assert (out / f"{capability}.md").is_file()
        assert (out / f"{capability}.json").is_file()
        assert "Capability run:" in printed


def test_the_cli_refuses_a_bundle_that_is_not_there(tmp_path, launcher_cwd):
    module = _launcher()
    with pytest.raises(SystemExit):
        module.main(["replay", "inventory", "--bundle", str(tmp_path / "nope")])
