"""``tools/run_options_matrix.py`` forwards the market-condition profile to every job.

Two things are being pinned:

* the flags REACH the optimize command line. A driver flag that parses and then does not travel
  is the worst kind: the grid completes, every log says the profile is on, and the jobs ran
  ungated. (The same trap ``--options-store`` was given its own passthrough for.)
* the DISCOVERY IDENTITY DIGEST moves with them. The job name is also the backend checkpoint key,
  so a gated run must not resume -- or be SKIPped against -- an ungated completion of the same
  name, and a run pinned to a different snapshot is a different experiment, not a resume.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_DRIVER = os.path.join(_REPO, "tools", "run_options_matrix.py")

_spec = importlib.util.spec_from_file_location("run_options_matrix_mc", _DRIVER)
matrix = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("run_options_matrix_mc", matrix)
_spec.loader.exec_module(matrix)

_LAUNCHER = os.path.join(_REPO, "testplatform", "ba2test_launcher.py")
_ARGV = ["--profile", "discovery", "--strategies", "O_LC", "--experts", "FMPRating",
         "--launcher", _LAUNCHER, "--start", "2020-01-01", "--end", "2025-12-31",
         "--screener-gate-store", "store.parquet", "--max-stock-price", "0"]


def _args(extra=()):
    ap = matrix.build_parser()
    return matrix.resolve_args(ap, _ARGV + list(extra))


def _cmd(extra=()):
    return matrix.build_cmd(_args(extra), _LAUNCHER, "optm-x", "FMPRating", "O_LC", "AAPL,MSFT")


def _name(extra=()):
    return matrix.discovery_name(_args(extra), _LAUNCHER, "optm-x", "FMPRating", "O_LC", "AAPL,MSFT")


def test_the_default_is_off_and_adds_no_token():
    assert _args().market_condition_profile == "none"
    assert "--market-condition-profile" not in _cmd()
    assert "--market-condition-manifest" not in _cmd()


def test_the_profile_and_manifest_reach_the_optimize_command():
    cmd = _cmd(["--market-condition-profile", "ohlcv-v1",
                "--market-condition-manifest", "abc123"])
    assert cmd[cmd.index("--market-condition-profile") + 1] == "ohlcv-v1"
    assert cmd[cmd.index("--market-condition-manifest") + 1] == "abc123"


def test_the_manifest_is_omitted_when_only_a_profile_is_given():
    """The launcher owns that refusal (and names the warm step); the driver does not guess a
    digest of its own."""
    cmd = _cmd(["--market-condition-profile", "ohlcv-v1"])
    assert "--market-condition-profile" in cmd and "--market-condition-manifest" not in cmd


def test_the_job_name_digest_changes_with_the_profile_and_with_the_manifest():
    plain = _name()
    gated = _name(["--market-condition-profile", "ohlcv-v1",
                   "--market-condition-manifest", "abc123"])
    other = _name(["--market-condition-profile", "ohlcv-v1",
                   "--market-condition-manifest", "def456"])
    assert plain != gated != other and plain != other


def test_the_job_name_digest_is_stable_for_the_same_flags():
    a = _name(["--market-condition-profile", "ohlcv-v1", "--market-condition-manifest", "abc123"])
    b = _name(["--market-condition-profile", "ohlcv-v1", "--market-condition-manifest", "abc123"])
    assert a == b
    # ...and unchanged by the knobs that describe the MACHINE, not the experiment.
    assert a == _name(["--market-condition-profile", "ohlcv-v1",
                       "--market-condition-manifest", "abc123", "--parallel", "8"])


@pytest.mark.parametrize("flag", ["--market-condition-profile", "--market-condition-manifest"])
def test_both_flags_are_documented(flag):
    help_text = matrix.build_parser().format_help()
    assert flag in help_text
