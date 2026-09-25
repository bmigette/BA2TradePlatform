"""``--short-borrow-rate-pa`` reaches the run config on BOTH optimize subcommands (plan
2026-09-24 equity short selling, S4).

The rate lives in the run's ``account_settings``, which ``_build_daily_trial_config`` carries
whole into every trial (pinned in tests/backtest/test_short_borrow_cost.py). This pins the CLI
hop: the REAL argparse tree and the REAL config builders (the helpers of
``test_robust_fitness_default_on``), not a hand-built namespace. The rate is written EXPLICITLY
(0.005 when not passed), so a stored run says what it charged.
"""
from __future__ import annotations

from tests.test_robust_fitness_default_on import (
    _BASE_ARGV, _BATCH_ARGV, _parse, _run_optimize, _run_optimize_batch)


def test_optimize_writes_the_default_rate(monkeypatch):
    cfg = _run_optimize(_parse(_BASE_ARGV), monkeypatch)
    assert cfg["backtest"]["account_settings"]["short_borrow_rate_pa"] == 0.005


def test_optimize_writes_a_given_rate(monkeypatch):
    cfg = _run_optimize(_parse(_BASE_ARGV + ["--short-borrow-rate-pa", "0.03"]), monkeypatch)
    assert cfg["backtest"]["account_settings"]["short_borrow_rate_pa"] == 0.03


def test_optimize_batch_writes_the_default_rate(monkeypatch):
    cfg = _run_optimize_batch(_parse(_BATCH_ARGV, cmd_attr="_cmd_optimize_batch"), monkeypatch)
    assert cfg["backtest"]["account_settings"]["short_borrow_rate_pa"] == 0.005


def test_optimize_batch_writes_a_given_rate(monkeypatch):
    cfg = _run_optimize_batch(
        _parse(_BATCH_ARGV + ["--short-borrow-rate-pa", "0"], cmd_attr="_cmd_optimize_batch"),
        monkeypatch)
    assert cfg["backtest"]["account_settings"]["short_borrow_rate_pa"] == 0.0
