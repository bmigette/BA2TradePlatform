"""``replay warm`` re-execs itself; the child must resolve paths where the USER stood.

The command has to set ``CACHE_FOLDER`` before import, so it re-runs this launcher as
a child process with the variable in its environment and the SAME argv. But
``_enter_backend()`` has already chdir'd into ``backend/`` by then, so the child was
spawned with the backend as its cwd -- and the child's own ``_CALLER_CWD`` (the
directory a relative command-line path is resolved against) was therefore the backend
too. ``ba2-test replay warm --plan plan.json`` opened the plan in the parent and then
failed in the child, looking for a file the user never named.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]
LAUNCHER = _BACKEND.parent / "ba2test_launcher.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("ba2test_launcher_warm_cwd_test", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def plan_dir(tmp_path):
    """A directory holding ``plan.json``, standing in for where the user ran from."""
    from ba2_providers.warm import planner

    root = tmp_path / "cache"
    root.mkdir()
    plan = planner.WarmPlan(
        created_at=datetime(2026, 9, 11, tzinfo=timezone.utc).isoformat(),
        roots=(str(root),), entries=())
    (tmp_path / "plan.json").write_text(plan.to_json(), encoding="utf-8")
    return tmp_path


def test_the_child_is_spawned_in_the_directory_the_user_ran_from(plan_dir, monkeypatch):
    launcher = _load_launcher()
    monkeypatch.setattr(launcher, "_CALLER_CWD", str(plan_dir))
    monkeypatch.delenv(launcher._WARM_CHILD_ENV, raising=False)
    spawned = {}

    def _call(argv, env=None, cwd=None):
        spawned.update(argv=argv, env=env, cwd=cwd)
        return 0

    monkeypatch.setattr(subprocess, "call", _call)
    monkeypatch.setattr(launcher.sys, "argv",
                        ["ba2-test", "replay", "warm", "--plan", "plan.json"])

    rc = launcher._cmd_replay_warm(argparse.Namespace(plan="plan.json"))

    assert rc == 0
    assert spawned["cwd"] == str(plan_dir), (
        "the child inherited the backend as its cwd, so its own _CALLER_CWD resolved "
        f"--plan against the wrong directory; got {spawned['cwd']!r}")
    assert os.path.isabs(spawned["env"]["CACHE_FOLDER"])


def test_a_relative_plan_path_resolves_against_that_directory(plan_dir, monkeypatch):
    """The parent half of the same contract, pinned so the two cannot drift apart."""
    launcher = _load_launcher()
    monkeypatch.setattr(launcher, "_CALLER_CWD", str(plan_dir))

    assert launcher._caller_path("plan.json") == str(plan_dir / "plan.json")
    assert json.loads(Path(launcher._caller_path("plan.json")).read_text())["roots"]
