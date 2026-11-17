"""Without the restarts / no-chain-seed / set-score / min-train-years options, the walk-forward chain
and the matrix driver behave exactly as before them: the same job names, optimize commands, dry-run
text, report files (cell.json, report.json, report.md, fold<k>.json, summary), imported DB report and
stored spec. The golden file was written by ``wf_golden_scenarios.run_all`` from TEST 2026.11.0205
(dev fc1e1c5c), before those options existed."""
from __future__ import annotations

import json
import os

import pytest

from tests.wf_golden_scenarios import run_all

_GOLDEN = os.path.join(os.path.dirname(__file__), "data", "wf_golden_default.json")


@pytest.fixture(scope="module")
def now():
    return run_all()


@pytest.fixture(scope="module")
def golden():
    with open(_GOLDEN, encoding="utf-8") as fh:
        return json.load(fh)


def test_the_scenario_set_is_the_golden_one(now, golden):
    assert sorted(now) == sorted(golden)


@pytest.mark.parametrize("scenario", ["pass", "fail_required", "eliminated", "floor", "deferred",
                                      "plan_default", "plan_floor_defer", "matrix_ds",
                                      "matrix_fr_defer", "matrix_ds_years"])
def test_default_behaviour_is_unchanged(now, golden, scenario):
    got, want = now[scenario], golden[scenario]
    assert sorted(got) == sorted(want)
    for key in want:
        if key == "files":
            assert sorted(got["files"]) == sorted(want["files"])
            for fn in want["files"]:
                assert got["files"][fn] == want["files"][fn], f"{scenario}: {fn} changed"
        else:
            assert got[key] == want[key], f"{scenario}: {key} changed"
