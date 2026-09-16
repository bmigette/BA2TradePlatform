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


# ---------------------------------------------------------------- the driver's own refusal (F2)
def test_a_manifest_without_a_profile_is_refused_by_the_DRIVER(capsys):
    """Review 2026-09-16, F2 -- reproduced through ``resolve_args``/``build_cmd``/
    ``discovery_name`` and fixed here.

    ``_market_condition_passthrough`` returned ``[]`` for ``--market-condition-manifest <digest>``
    with the default profile ``none``, so the manifest never reached the launcher and the
    launcher's OWN manifest-without-profile refusal could not fire. The 32 jobs then ran UNGATED
    under a discovery name byte-identical to the ordinary ungated job -- which also means they
    could be SKIPped against an existing ungated completion, while every listing afterwards read
    as though a snapshot had been pinned.

    The refusal therefore belongs in ``resolve_args``: before any command is built, before any
    name is generated, and on the ``--dry-run`` path too (which resolves the same args).
    """
    with pytest.raises(SystemExit):
        _args(["--market-condition-manifest", "abc123"])
    assert "without --market-condition-profile" in capsys.readouterr().err

    # It is the ARGUMENT RESOLUTION that refuses, so neither of the two consumers can be reached
    # with that combination -- the property the previous test suite could not state, because the
    # dropped argument left both of them looking perfectly ordinary.
    for build in (_cmd, _name):
        with pytest.raises(SystemExit):
            build(["--market-condition-manifest", "abc123"])


def test_the_dry_run_path_refuses_it_too(capsys):
    """``--dry-run`` PRINTS the commands it would run: printing an ungated one under a manifest
    the operator passed is the same lie, one step earlier."""
    with pytest.raises(SystemExit):
        matrix.main(_ARGV + ["--market-condition-manifest", "abc123", "--dry-run"])
    assert "without --market-condition-profile" in capsys.readouterr().err


def test_an_explicit_none_profile_with_a_manifest_is_refused_as_well(capsys):
    with pytest.raises(SystemExit):
        _args(["--market-condition-profile", "none", "--market-condition-manifest", "abc123"])
    assert "without --market-condition-profile" in capsys.readouterr().err


def test_both_flags_together_still_pass_and_still_gate():
    """The refusal must not have cost the supported configuration."""
    cmd = _cmd(["--market-condition-profile", "ohlcv-v1",
                "--market-condition-manifest", "ohlcv-v1=abc123"])
    assert cmd[cmd.index("--market-condition-manifest") + 1] == "ohlcv-v1=abc123"
