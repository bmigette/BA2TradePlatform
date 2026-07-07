"""Locked parity scope (docs/plans/2026-07-02-live-backtest-engine-unification.md):
the backtest is scoped to classic-RM + bypass experts and must FAIL LOUD on a
Smart (agentic) risk-manager expert rather than silently modelling a different policy.
"""
import pytest

from app.services.backtest.daily_backtest_handler import assert_backtestable_risk_mode


def test_smart_risk_mode_is_rejected():
    with pytest.raises(ValueError, match="Smart"):
        assert_backtestable_risk_mode("FMPRating", {"risk_manager_mode": "smart"})


def test_smart_case_insensitive_and_whitespace():
    with pytest.raises(ValueError):
        assert_backtestable_risk_mode("FMPRating", {"risk_manager_mode": "  SMART "})


def test_classic_mode_passes():
    assert_backtestable_risk_mode("FMPRating", {"risk_manager_mode": "classic"}) is None


def test_absent_mode_defaults_to_classic_and_passes():
    # No risk_manager_mode key -> treated as classic -> allowed.
    assert_backtestable_risk_mode("FMPRating", {}) is None
    assert_backtestable_risk_mode("FMPRating", {"risk_manager_mode": None}) is None


def test_error_names_the_expert_and_points_to_the_fix():
    with pytest.raises(ValueError) as ei:
        assert_backtestable_risk_mode("FMPEarningsDrift", {"risk_manager_mode": "smart"})
    msg = str(ei.value)
    assert "FMPEarningsDrift" in msg
    assert "classic" in msg  # tells the user how to proceed
