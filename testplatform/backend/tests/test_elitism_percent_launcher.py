"""``ba2-test optimize --elitism-percent``: F4 (option-program-review-findings.md, 2026-08-30).

The launcher hardcoded ``"elitismPercent": 0.1`` at both GA config sites (``_cmd_optimize`` and
``_cmd_optimize_batch``) -- the ENGINE default is 10.0 (``genetic.py``/``genetic_optimizer_base.py``/
``job_handler.py`` all default ``elitism_percent=10.0``), so 0.1% of any real population is
"round down to zero, floor at 1" -- exactly ONE elite, regardless of population size. The
engine honors whatever value it is given; the bug is entirely in the launcher's hardcoded
literal. Fixed by making it a CLI flag, defaulting to the engine's 10.0, wired at both sites.

Reuses the harness from ``test_equity_cap_launcher.py``: drives the REAL CLI/config-builder
path (``L.main`` -> ``_cmd_optimize``/``_cmd_optimize_batch``) with only the GA and top-N
persistence stubbed, and reads back the persisted ``optimization_config``.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))
sys.path.insert(0, os.path.dirname(__file__))

import ba2test_launcher as L  # noqa: E402

from test_equity_cap_launcher import (  # noqa: E402
    _BASE_ARGV,
    _BATCH_ARGV,
    _parse,
    _run_optimize,
    _run_optimize_batch,
)


# --------------------------------------------------------------------------- #
# The flag
# --------------------------------------------------------------------------- #
def test_elitism_percent_defaults_to_the_engine_default():
    assert _parse(_BASE_ARGV).elitism_percent == pytest.approx(10.0)


def test_elitism_percent_is_configurable():
    assert _parse(_BASE_ARGV + ["--elitism-percent", "25"]).elitism_percent == pytest.approx(25.0)


def test_the_batch_driver_accepts_the_same_flag_with_the_same_default():
    assert _parse(_BATCH_ARGV, cmd_attr="_cmd_optimize_batch").elitism_percent == pytest.approx(10.0)
    ns = _parse(_BATCH_ARGV + ["--elitism-percent", "25"], cmd_attr="_cmd_optimize_batch")
    assert ns.elitism_percent == pytest.approx(25.0)


# --------------------------------------------------------------------------- #
# It reaches the config the GA runs, at BOTH sites (single-job and batch)
# --------------------------------------------------------------------------- #
def test_default_elitism_reaches_the_optimize_config(monkeypatch):
    """The regression this closes: the config the GA actually reads must carry 10.0, not the
    old hardcoded 0.1 that floored every population to a single elite."""
    cfg = _run_optimize(_parse(_BASE_ARGV), monkeypatch)
    assert cfg["elitismPercent"] == pytest.approx(10.0)


def test_configured_elitism_reaches_the_optimize_config(monkeypatch):
    cfg = _run_optimize(_parse(_BASE_ARGV + ["--elitism-percent", "25"]), monkeypatch)
    assert cfg["elitismPercent"] == pytest.approx(25.0)


def test_default_elitism_reaches_the_batch_config(monkeypatch):
    cfg = _run_optimize_batch(
        _parse(_BATCH_ARGV, cmd_attr="_cmd_optimize_batch"), monkeypatch)
    assert cfg["elitismPercent"] == pytest.approx(10.0)


def test_configured_elitism_reaches_the_batch_config(monkeypatch):
    cfg = _run_optimize_batch(
        _parse(_BATCH_ARGV + ["--elitism-percent", "25"], cmd_attr="_cmd_optimize_batch"),
        monkeypatch)
    assert cfg["elitismPercent"] == pytest.approx(25.0)
