"""No weekend entry-scan genes under ANY ``--rm-toggle-policy``, end to end through the REAL launcher.

Every backtest bar is a weekday, so ``schedule:saturday`` / ``schedule:sunday`` can never change a
daily-clock trial (a stage-2 genome re-run with both flipped was identical: 425 trades, +424.55%).
They used to be dropped only under the ATR-searched policies; ``pinned`` (the default) still
searched all 7 days, i.e. 2 dead genes per job. The weekday genes stay.
"""
from __future__ import annotations

import json

import pytest

from tests.test_equity_cap_launcher import (
    _BASE_ARGV, _BATCH_ARGV, _parse, _run_optimize, _run_optimize_batch,
)

_WEEKEND = ("schedule:saturday", "schedule:sunday")
_WEEKDAYS = ("schedule:monday", "schedule:tuesday", "schedule:wednesday", "schedule:thursday",
             "schedule:friday")


def _assert_weekdays_only(cfg):
    blob = json.dumps(cfg)
    for gene in _WEEKEND:
        assert gene not in blob
    genes = cfg["expert_params"]
    for gene in _WEEKDAYS:
        assert genes[gene]["optimize"] is True


@pytest.mark.parametrize("policy_argv", [[], ["--rm-toggle-policy", "pinned"]])
def test_optimize_under_pinned_has_no_weekend_genes(monkeypatch, policy_argv):
    _assert_weekdays_only(_run_optimize(_parse(_BASE_ARGV + policy_argv), monkeypatch))


def test_optimize_batch_under_pinned_has_no_weekend_genes(monkeypatch):
    _assert_weekdays_only(_run_optimize_batch(
        _parse(_BATCH_ARGV, cmd_attr="_cmd_optimize_batch"), monkeypatch))
