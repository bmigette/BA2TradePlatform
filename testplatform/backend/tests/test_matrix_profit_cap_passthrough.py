"""The GA matrix drivers must FORWARD ``--profit-cap-pct 0`` instead of dropping it.

All three drivers document "Pass 0 to disable" on ``--profit-cap-pct`` /
``--profit-share-cap-pct``, but guarded the passthrough with a TRUTHINESS test::

    if args.profit_cap_pct and args.profit_cap_pct > 0:      # 0.0 is falsy -> omitted

so ``--profit-cap-pct 0`` omitted the flag entirely and ``ba2test_launcher`` re-applied its
OWN default (2000.0 / 25.0). The help text was therefore a lie: 0 did not disable the cap,
it silently restored it. The launcher already maps a falsy value to ``None`` (no cap), so
forwarding the ``0`` is all that is needed.

These are real end-to-end tests of each driver's ``main()``: ``subprocess.run`` is captured
and ``DB_FILE`` points at a nonexistent sqlite (each driver's ``_completed_names()`` swallows
the error and returns an empty set), so the full ``ba2-test optimize`` argv is built and
inspected without launching anything or touching a database.
"""
from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "tools"))

import matrix_flags  # noqa: E402
import run_options_matrix  # noqa: E402
import run_screener_capband_matrix  # noqa: E402
import run_senate_matrix  # noqa: E402


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Capture every ``subprocess.run`` argv a driver's ``main()`` would launch."""
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    # No such DB -> _completed_names() returns an empty set, so no job is skipped.
    monkeypatch.setenv("DB_FILE", str(tmp_path / "no-such-optimizations.db"))
    return calls


def _flag_value(cmd: list[str], flag: str):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


# --------------------------------------------------------------------------- #
# The shared helper
# --------------------------------------------------------------------------- #
def test_cap_passthrough_forwards_zero_because_zero_means_disable():
    out = matrix_flags.cap_passthrough(
        SimpleNamespace(profit_cap_pct=0.0, profit_share_cap_pct=0.0))
    assert out == ["--profit-cap-pct", "0.0", "--profit-share-cap-pct", "0.0"]


def test_cap_passthrough_forwards_normal_values():
    out = matrix_flags.cap_passthrough(
        SimpleNamespace(profit_cap_pct=2000.0, profit_share_cap_pct=25.0))
    assert out == ["--profit-cap-pct", "2000.0", "--profit-share-cap-pct", "25.0"]


def test_cap_passthrough_disables_each_cap_independently():
    out = matrix_flags.cap_passthrough(
        SimpleNamespace(profit_cap_pct=0.0, profit_share_cap_pct=25.0))
    assert out == ["--profit-cap-pct", "0.0", "--profit-share-cap-pct", "25.0"]


def test_cap_passthrough_omits_only_a_genuinely_unset_cap():
    """``None`` (never provided, no default) is the ONLY reason to omit the flag."""
    out = matrix_flags.cap_passthrough(
        SimpleNamespace(profit_cap_pct=None, profit_share_cap_pct=None))
    assert out == []


# --------------------------------------------------------------------------- #
# Each driver, end to end
# --------------------------------------------------------------------------- #
def test_options_matrix_forwards_zero_caps(captured, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "run_options_matrix.py", "--experts", "FMPRating", "--strategies", "O_LC",
        "--profit-cap-pct", "0", "--profit-share-cap-pct", "0",
    ])
    assert run_options_matrix.main() == 0
    assert captured, "the driver launched no optimize job"
    cmd = captured[0]
    assert _flag_value(cmd, "--profit-cap-pct") == "0.0"
    assert _flag_value(cmd, "--profit-share-cap-pct") == "0.0"


def test_options_matrix_still_forwards_the_default_caps(captured, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "run_options_matrix.py", "--experts", "FMPRating", "--strategies", "O_LC",
    ])
    assert run_options_matrix.main() == 0
    cmd = captured[0]
    assert _flag_value(cmd, "--profit-cap-pct") == "2000.0"
    assert _flag_value(cmd, "--profit-share-cap-pct") == "25.0"


def test_senate_matrix_forwards_zero_caps(captured, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "run_senate_matrix.py", "--strategies", "S2",
        "--profit-cap-pct", "0", "--profit-share-cap-pct", "0",
    ])
    assert run_senate_matrix.main() == 0
    assert captured, "the driver launched no optimize job"
    cmd = captured[0]
    assert _flag_value(cmd, "--profit-cap-pct") == "0.0"
    assert _flag_value(cmd, "--profit-share-cap-pct") == "0.0"


def test_screener_capband_matrix_forwards_zero_caps(captured, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "run_screener_capband_matrix.py", "--bands", "large", "--strategies", "S1",
        "--profit-cap-pct", "0", "--profit-share-cap-pct", "0",
    ])
    assert run_screener_capband_matrix.main() == 0
    assert captured, "the driver launched no optimize job"
    cmd = captured[0]
    assert _flag_value(cmd, "--profit-cap-pct") == "0.0"
    assert _flag_value(cmd, "--profit-share-cap-pct") == "0.0"


def test_every_driver_help_promise_that_zero_disables_is_now_true():
    """All three advertise "Pass 0 to disable"; none may reintroduce the truthiness guard."""
    import inspect

    for mod in (run_options_matrix, run_senate_matrix, run_screener_capband_matrix):
        src = inspect.getsource(mod.main)
        assert "Pass 0 to disable" in src, f"{mod.__name__} dropped the documented promise"
        assert "args.profit_cap_pct and" not in src, (
            f"{mod.__name__} reintroduced the falsy-zero guard")
        assert "args.profit_share_cap_pct and" not in src, (
            f"{mod.__name__} reintroduced the falsy-zero guard")
