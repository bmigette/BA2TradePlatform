"""``--rm-toggle-policy`` is wired onto BOTH ``optimize`` and ``optimize-batch`` (design §3.2
item 1: "A new launcher flag ... on the optimize and optimize-batch commands").

Exercises the REAL argparse setup inside ``ba2test_launcher.main()``: the dispatch to
``_cmd_optimize``/``_cmd_optimize_batch`` is monkeypatched to a stub that just records ``args``,
so this proves the CLI actually parses and defaults the flag without touching a DB or running a
job -- a wiring check on the real parser, not a re-statement of the unit-level policy tests in
tests/backtest/test_rm_toggle_policy_atr27.py.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.abspath(__file__))                       # testplatform/backend/tests
_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "..", "ba2test_launcher.py"))


@pytest.fixture
def launcher():
    spec = importlib.util.spec_from_file_location("lch_cli_wiring", _LAUNCHER)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_cli_wiring"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


@pytest.mark.parametrize("cmd", ["optimize", "optimize-batch"])
def test_the_flag_is_on_the_command_with_the_right_choices(launcher, cmd, capsys):
    with pytest.raises(SystemExit) as exc:
        launcher.main([cmd, "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--rm-toggle-policy" in out
    assert "pinned" in out and "atr-searched" in out


def _captured_args(launcher, monkeypatch, cmd_attr, argv):
    captured = {}

    def _stub(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(launcher, cmd_attr, _stub)
    rc = launcher.main(argv)
    assert rc == 0
    return captured["args"]


def test_optimize_defaults_to_pinned(launcher, monkeypatch):
    args = _captured_args(launcher, monkeypatch, "_cmd_optimize", [
        "optimize", "--expert", "FMPRating", "--universe", "AAPL",
        "--start", "2024-01-01", "--end", "2024-02-01"])
    assert args.rm_toggle_policy == "pinned"


def test_optimize_accepts_atr_searched(launcher, monkeypatch):
    args = _captured_args(launcher, monkeypatch, "_cmd_optimize", [
        "optimize", "--expert", "FMPRating", "--universe", "AAPL",
        "--start", "2024-01-01", "--end", "2024-02-01",
        "--rm-toggle-policy", "atr-searched"])
    assert args.rm_toggle_policy == "atr-searched"


def test_an_unknown_policy_value_is_refused_by_argparse(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "_cmd_optimize", lambda args: 0)
    with pytest.raises(SystemExit) as exc:
        launcher.main(["optimize", "--expert", "FMPRating", "--universe", "AAPL",
                      "--start", "2024-01-01", "--end", "2024-02-01",
                      "--rm-toggle-policy", "bogus"])
    assert exc.value.code != 0


def test_optimize_batch_defaults_to_pinned(launcher, monkeypatch):
    args = _captured_args(launcher, monkeypatch, "_cmd_optimize_batch", [
        "optimize-batch", "--universe", "AAPL", "--start", "2024-01-01", "--end", "2024-02-01"])
    assert args.rm_toggle_policy == "pinned"


def test_optimize_batch_accepts_atr_searched(launcher, monkeypatch):
    args = _captured_args(launcher, monkeypatch, "_cmd_optimize_batch", [
        "optimize-batch", "--universe", "AAPL", "--start", "2024-01-01", "--end", "2024-02-01",
        "--rm-toggle-policy", "atr-searched"])
    assert args.rm_toggle_policy == "atr-searched"
