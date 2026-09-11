"""Inventory and the CLI: every recorded analysis is counted, nothing is dropped.

"Include HOLD/skipped/failed analyses in totals; never report 100% by dropping
unavailable rows." (spec section 8) The skip and the error row are the ones a
counting bug loses first, so they are asserted by name.

And a capability that HAS been run must stop reading as ``not_run``: a replay
result nobody wrote back is a report that disagrees with the session it came
from, one command later.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ba2_common.core.replay import ReplayStatus

from app.services.replay import expert_replay, gather_tape, inventory
from app.services.replay.report import STATUS_ORDER
from tests.replay import ALL_IDS, capture_session

#: repo/testplatform/backend/tests/replay/<this file>
LAUNCHER = Path(__file__).resolve().parents[3] / "ba2test_launcher.py"
REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(scope="module")
def session(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("inventory")
    return capture_session(root / "store", root / "export")


@pytest.fixture
def bundle_copy(session, tmp_path) -> Path:
    """A private copy: running a capability WRITES coverage back into the bundle."""
    target = tmp_path / "bundle"
    shutil.copytree(session, target)
    return target


# --------------------------------------------------------------------------- #
# 1. Inventory counts everything
# --------------------------------------------------------------------------- #
def test_totals_include_the_skip_and_the_error(session):
    report = inventory.run(session)

    assert report["totals"]["analyses"] == len(ALL_IDS)
    assert report["analyses_by_outcome"] == {
        ReplayStatus.OUTCOME_ERROR: 1,
        ReplayStatus.OUTCOME_RECOMMENDATION: 6,
        ReplayStatus.OUTCOME_SKIP: 1,
    }
    assert sum(report["analyses_by_expert"].values()) == len(ALL_IDS)
    assert report["analyses_by_expert"] == {
        "DeterministicScorer": 1, "FMPEarningsDrift": 2,
        "FMPInsiderClusterBuy": 2, "FMPRating": 3}
    assert report["skip_reasons"] == {"no consensus data": 1}


def test_capture_status_observations_and_clock_reads_are_broken_out(session):
    report = inventory.run(session)

    assert report["bundle_capture_status"] == {ReplayStatus.CAPTURE_CAPTURED: len(ALL_IDS)}
    methods = report["observations_by_provider_method"]
    assert methods["broker.get_instrument_current_price"] == len(ALL_IDS) - 1, (
        "every analysis reads a quote except DeterministicScorer, whose "
        "current_price is the last close of the OHLCV frame it already fetched")
    assert methods["provider_cache.insider_get"] == 2
    assert methods["market_data.get_ohlcv_data"] == 2, (
        "the DeterministicScorer symbol and its benchmark are two recorded reads")
    assert methods["fmp.earning_calendar"] == 1
    assert sum(methods.values()) == report["totals"]["observations"]
    assert report["observations_by_provenance"][ReplayStatus.PROVENANCE_NETWORK] == (
        len(ALL_IDS) - 1), "one fresh broker quote per analysis that reads one"

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
# 2. Running a capability is written back as coverage
# --------------------------------------------------------------------------- #
def test_running_experts_then_inventory_reports_the_capability_as_run(bundle_copy):
    """The two commands must agree about the same bundle, in that order."""
    before = inventory.run(bundle_copy)["coverage_by_capability"]
    assert before[ReplayStatus.CAPABILITY_RECORDED_EXPERT][
        ReplayStatus.COVERAGE_NOT_RUN] == len(ALL_IDS)

    report = expert_replay.run(bundle_copy)
    after = inventory.run(bundle_copy)["coverage_by_capability"]
    rows = after[ReplayStatus.CAPABILITY_RECORDED_EXPERT]

    assert rows[ReplayStatus.COVERAGE_MATCH] == report.counts()[ReplayStatus.COVERAGE_MATCH]
    assert rows[ReplayStatus.COVERAGE_NOT_RUN] == 0, (
        "inventory still says not_run for a capability that has just been run")
    assert sum(rows.values()) == len(ALL_IDS)


def test_the_two_capabilities_keep_their_own_coverage(bundle_copy):
    expert_replay.run(bundle_copy)
    gather_tape.run(bundle_copy)
    coverage = inventory.run(bundle_copy)["coverage_by_capability"]

    assert coverage[ReplayStatus.CAPABILITY_RECORDED_EXPERT][
        ReplayStatus.COVERAGE_MATCH] == len(ALL_IDS)
    gather_rows = coverage[ReplayStatus.CAPABILITY_GATHER_TAPE]
    assert gather_rows[ReplayStatus.COVERAGE_MATCH] == len(ALL_IDS), (
        "every recorded gather is tape-serveable now, including the scorer's")
    assert sum(gather_rows.values()) == len(ALL_IDS), (
        "the gather-tape capability keeps its own row per analysis")
    assert coverage[ReplayStatus.CAPABILITY_HISTORICAL][
        ReplayStatus.COVERAGE_NOT_RUN] == len(ALL_IDS)


def test_the_report_payload_declares_the_shape_it_actually_writes(bundle_copy):
    """The JSON schema tag must move when the shape does.

    A consumer keys on ``schema`` to know what the file holds. This payload changed
    twice after ``/1`` was minted -- stage rows carry ``capabilities`` (plural, a
    list) where they carried a single ``capability``, and a field diff now carries
    the numeric deltas -- so a reader written against ``/1`` and handed one of these
    silently reads a shape that is not the one it was written for.
    """
    report = expert_replay.run(bundle_copy)
    payload = report.to_mapping()

    assert payload["schema"] == "ba2_replay_report/2"
    assert all("capabilities" in row and "capability" not in row
               for row in payload["stages"]), (
        "the stage rows are the /2 shape; the tag must say so")
    diff_keys = {key for result in payload["results"]
                 for diff in result["field_diffs"] for key in diff}
    assert not diff_keys or "abs_delta" in diff_keys


def test_a_second_run_replaces_its_own_rows_rather_than_appending(bundle_copy):
    expert_replay.run(bundle_copy)
    expert_replay.run(bundle_copy)
    rows = inventory.run(bundle_copy)["coverage_by_capability"][
        ReplayStatus.CAPABILITY_RECORDED_EXPERT]
    assert sum(rows.values()) == len(ALL_IDS), "coverage rows accumulated across runs"


# --------------------------------------------------------------------------- #
# 3. The CLI (in a subprocess: main() chdirs, loads .env and points a DB engine)
# --------------------------------------------------------------------------- #
def _run_cli(*args, cwd) -> subprocess.CompletedProcess:
    """Drive the launcher out-of-process.

    ``main()`` calls ``_enter_backend()``, which chdirs, loads ``.env`` and
    rebinds the shared DB engine -- all process-wide. Importing and calling it
    inside pytest would leave those side effects behind for every test that runs
    afterwards, so the CLI is exercised where it actually lives: its own process.
    """
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            str(REPO_ROOT / "packages" / name) for name in ("common", "providers", "experts"))},
    )


@pytest.mark.parametrize("command", ["inventory", "experts", "gather"])
def test_the_cli_parses_and_runs_each_replay_command(command, bundle_copy, tmp_path):
    out = tmp_path / command
    result = _run_cli("replay", command, "--bundle", str(bundle_copy), "--out", str(out),
                      cwd=tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert out.is_dir()
    if command == "inventory":
        assert (out / inventory.INVENTORY_NAME).is_file()
        assert "Replay session inventory" in result.stdout
    else:
        capability = (ReplayStatus.CAPABILITY_RECORDED_EXPERT if command == "experts"
                      else ReplayStatus.CAPABILITY_GATHER_TAPE)
        assert (out / f"{capability}.md").is_file()
        assert (out / f"{capability}.json").is_file()
        assert "Capability run:" in result.stdout


def test_the_cli_takes_a_bundle_path_relative_to_where_it_was_run(bundle_copy):
    """``main()`` chdirs into backend/; a typed path must still mean what it said."""
    result = _run_cli("replay", "inventory", "--bundle", bundle_copy.name,
                      cwd=bundle_copy.parent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "S-REPLAY-TEST" in result.stdout


def test_the_cli_refuses_a_bundle_that_is_not_there(tmp_path):
    result = _run_cli("replay", "inventory", "--bundle", str(tmp_path / "nope"),
                      cwd=tmp_path)
    assert result.returncode != 0
    assert "not a directory" in (result.stdout + result.stderr)
