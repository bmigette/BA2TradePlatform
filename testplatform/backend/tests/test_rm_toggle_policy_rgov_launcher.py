"""``--rm-toggle-policy atr-regime-searched`` end to end through the REAL launcher.

The CLI parses the flag, and the real ``_cmd_optimize`` / ``_cmd_optimize_batch`` persist a run
config that (a) records both toggles as unpinned, so every re-run of the run sees the policy, and
(b) searches the regime overlay gene and its three scales next to the ATR-searched block. The
unit-level policy tests are tests/backtest/test_rm_toggle_policy_rgov.py.
"""
from __future__ import annotations

import pytest

from tests.test_equity_cap_launcher import (
    L, _BASE_ARGV, _BATCH_ARGV, _parse, _run_optimize, _run_optimize_batch,
)

_POLICY = ["--rm-toggle-policy", "atr-regime-searched"]
_UNPINNED = ["use_atr_stop", "regime_overlay_enabled"]
_SCALES = ("regime_risk_scale", "regime_stop_scale", "regime_tp_scale")


def _assert_rgov_space(cfg):
    assert cfg["backtest"]["rm_toggles_unpinned"] == _UNPINNED
    genes = cfg["expert_params"]
    assert genes["regime_overlay_enabled"]["optimize"] is True
    assert genes["use_atr_stop"]["optimize"] is True
    for gene in _SCALES:
        assert genes[gene]["optimize"] is True


def test_optimize_persists_the_policy_and_the_genes(monkeypatch):
    cfg = _run_optimize(_parse(_BASE_ARGV + _POLICY + ["--name", "t-S1-atr27-rgov"]), monkeypatch)
    _assert_rgov_space(cfg)


def test_optimize_batch_persists_the_policy_and_the_genes(monkeypatch):
    cfg = _run_optimize_batch(
        _parse(_BATCH_ARGV + _POLICY + ["--name-prefix", "p-atr27-rgov"],
               cmd_attr="_cmd_optimize_batch"),
        monkeypatch)
    _assert_rgov_space(cfg)


def test_optimize_refuses_a_name_without_rgov():
    with pytest.raises(SystemExit):
        L._cmd_optimize(_parse(_BASE_ARGV + _POLICY + ["--name", "t-S1-atr27"]))


def test_atr_searched_still_drops_the_overlay_genes(monkeypatch):
    cfg = _run_optimize(_parse(_BASE_ARGV + ["--rm-toggle-policy", "atr-searched",
                                             "--name", "t-S1-atr27"]), monkeypatch)
    assert cfg["backtest"]["rm_toggles_unpinned"] == ["use_atr_stop"]
    genes = cfg["expert_params"]
    assert genes["regime_overlay_enabled"]["optimize"] is False
    for gene in _SCALES:
        assert gene not in genes
