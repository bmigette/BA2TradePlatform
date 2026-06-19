"""Plan-2 Task 6: options-strategy detection in the daily backtest handler.

``strategy_uses_options`` scans the run's exit/RM rules and returns True iff ANY rule's
``option_strategy`` / ``action_type`` / ``action`` is an option action (``is_option_action``).
A True result is what drives the handler to derive an ``options_cache_db`` (so the Plan-1
seam builds + injects the HistoricalOptionsProvider) and to validate the Feb-2024 window.
Equity-only runs (no option action anywhere) must be byte-identical to before — no
options_cache_db, no window validation."""
from datetime import datetime

from app.services.backtest.daily_backtest_handler import (
    _build_config,
    strategy_uses_options,
)


def test_detects_option_rule():
    assert strategy_uses_options({"exit_rules": [{"action": "buy_call"}]}) is True
    assert strategy_uses_options(
        {"exit_conditions": [{"option_strategy": "sell_covered_call"}]}
    ) is True


def test_detects_option_rule_via_action_type():
    # The canonical evaluator key is ``action_type``; an option there must be detected too.
    assert strategy_uses_options({"exit_rules": [{"action_type": "buy_protective_put"}]}) is True


def test_equity_rules_not_options():
    assert strategy_uses_options(
        {"exit_rules": [{"action": "close"}, {"action": "adjust_stop_loss"}]}
    ) is False
    assert strategy_uses_options({}) is False


def test_exit_rules_preferred_but_falls_back_to_exit_conditions():
    # When both keys are absent/empty the scan is a no-op (False), not a crash.
    assert strategy_uses_options({"exit_rules": []}) is False
    assert strategy_uses_options({"exit_rules": [], "exit_conditions": [{"action": "buy_put"}]}) is True


def test_ignores_non_dict_rules():
    assert strategy_uses_options({"exit_rules": ["close", None, 42]}) is False


# --- config-build integration ----------------------------------------------
# When the payload's exit rules contain an option action, _build_config must set a
# (non-None) ``options_cache_db`` so the Plan-1 run seam builds + injects the provider.
# An equity-only payload must leave ``options_cache_db`` None (no behaviour change).
_BASE_PAYLOAD = {
    "backtest_id": 1,
    "experts": ["FMPRating"],
    "start_date": "2024-06-01",
    "end_date": "2024-06-30",
    "initial_capital": 100000.0,
    "commission": 0.0,
    "slippage": 0.0,
    "fill_model": "next_open",
    "seed": 7,
    "enabled_instruments": ["AAPL"],
}


def test_build_config_sets_options_cache_db_for_option_rule():
    payload = {**_BASE_PAYLOAD, "exit_rules": [{"action": "buy_call"}]}
    cfg = _build_config(payload)
    assert cfg["options_cache_db"]  # truthy path was derived


def test_build_config_equity_only_leaves_options_cache_db_none():
    payload = {**_BASE_PAYLOAD, "exit_rules": [{"action": "close"}]}
    cfg = _build_config(payload)
    assert cfg["options_cache_db"] is None


def test_build_config_honours_explicit_options_cache_db():
    # An explicit cache path in the payload is never overridden by detection.
    payload = {
        **_BASE_PAYLOAD,
        "exit_rules": [{"action": "buy_call"}],
        "options_cache_db": "/tmp/explicit_options.sqlite",
    }
    cfg = _build_config(payload)
    assert cfg["options_cache_db"] == "/tmp/explicit_options.sqlite"


def test_build_config_pre_2024_option_run_rejected():
    import pytest

    payload = {
        **_BASE_PAYLOAD,
        "start_date": "2023-06-01",
        "exit_rules": [{"action": "buy_call"}],
    }
    with pytest.raises(ValueError):
        _build_config(payload)
