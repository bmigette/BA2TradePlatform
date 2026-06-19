"""OPTIONS OPTIMIZATION wiring: the genetic optimizer's per-trial configs must inject the
options provider when the strategy uses option actions, so the option selection-param genes
(``exit:<id>:option_delta`` / ``option_dte``) actually drive REAL options backtests across
trials.

THE GAP this proves closed: the single-run path (``daily_backtest_handler._build_config``)
derives ``options_cache_db`` via ``strategy_uses_options`` and ``run_daily_backtest`` injects
the ``HistoricalOptionsProvider`` when it is set. But the optimizer builds each trial's config
via ``strategy_optimization_handler._build_daily_trial_config`` which did NOT set
``options_cache_db`` — so optimization trials ran WITHOUT the options provider and an option
rule could not fetch a chain.

Two hermetic, deap-free layers are tested here:

  1. ``_build_daily_trial_config`` forwards/derives ``options_cache_db`` per trial:
       * an option exit rule (``action: buy_call``)            -> non-None derived path,
       * an equity-only exit rule (``action: close``)          -> None (byte-identical),
       * an explicit run-level ``options_cache_db`` in the cfg -> forwarded as-is.
  2. The PARAM-SPACE -> DECODE chain: ``collect_param_space`` emits the option genes and
     ``decode_params`` writes them onto the trial's exit rule — proving the genes flow into
     the rule that the trial backtest then runs with the provider.

Run from the backend dir (no deap needed for these):
    ./venv/bin/python -m pytest tests/test_options_optimization_wiring.py -q
"""
from __future__ import annotations

import types

from app.services import strategy_optimization_handler as H
from app.services.strategy_param_space import collect_param_space, decode_params


# --------------------------------------------------------------------------- #
# A minimal valid run-level backtest_cfg with the fields _build_daily_trial_config reads:
# backtest_id, name?, start_date, end_date, enabled_instruments, experts, initial_capital,
# account_settings, warmup_days, seed (+ optional subtype/run_schedule_override/etc.).
# start_date is >= the 2024-02-01 options-history floor so an options trial validates.
# --------------------------------------------------------------------------- #
def _backtest_cfg(**over):
    cfg = {
        "backtest_id": 7,
        "start_date": "2024-02-01",
        "end_date": "2024-02-29",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": "FMPEarningsDrift", "settings": {}}],
        "initial_capital": 100000.0,
        "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30,
        "seed": 42,
    }
    cfg.update(over)
    return cfg


def _decoded(exit_rules):
    return {
        "tp": 8.0,
        "sl": 3.0,
        "expert_overrides": {},
        "buy_tree": None,
        "sell_tree": None,
        "exit_rules": exit_rules,
    }


# --------------------------------------------------------------------------- #
# 1. _build_daily_trial_config derives/forwards options_cache_db per trial
# --------------------------------------------------------------------------- #
def test_trial_config_derives_options_cache_for_option_rule():
    """An option exit rule (buy_call) -> the trial config carries a non-None options_cache_db
    so run_daily_backtest builds + injects the HistoricalOptionsProvider for the trial."""
    decoded = _decoded([{"id": "o1", "action": "buy_call", "option_strike_param": 0.3}])
    cfg = H._build_daily_trial_config(_backtest_cfg(), decoded)
    assert cfg["options_cache_db"] is not None
    assert str(cfg["options_cache_db"]).endswith(".sqlite") or str(cfg["options_cache_db"])


def test_trial_config_options_cache_none_for_equity_only():
    """An equity-only exit rule (close) -> options_cache_db is None, so the trial runs WITHOUT
    the options provider (byte-identical to the equity-only path)."""
    decoded = _decoded([{"id": "e1", "action": "close"}])
    cfg = H._build_daily_trial_config(_backtest_cfg(), decoded)
    assert cfg["options_cache_db"] is None


def test_trial_config_options_cache_none_for_no_exit_rules():
    """No exit rules at all -> options_cache_db is None (equity-only, unchanged)."""
    cfg = H._build_daily_trial_config(_backtest_cfg(), _decoded([]))
    assert cfg["options_cache_db"] is None


def test_trial_config_forwards_explicit_run_level_options_cache():
    """An explicit run-level backtest_cfg['options_cache_db'] is forwarded as-is to the trial,
    overriding the derive-from-rules path (e.g. a fixture cache pinned by the caller)."""
    decoded = _decoded([{"id": "o1", "action": "buy_call", "option_strike_param": 0.3}])
    cfg = H._build_daily_trial_config(_backtest_cfg(options_cache_db="/x.db"), decoded)
    assert cfg["options_cache_db"] == "/x.db"


def test_trial_config_forwards_explicit_cache_even_for_equity_only():
    """An explicit run-level options_cache_db is honoured even when the decoded rules are
    equity-only (the caller pinned a cache deliberately)."""
    decoded = _decoded([{"id": "e1", "action": "close"}])
    cfg = H._build_daily_trial_config(_backtest_cfg(options_cache_db="/x.db"), decoded)
    assert cfg["options_cache_db"] == "/x.db"


# --------------------------------------------------------------------------- #
# 2. param-space -> decode chain: the option genes flow to the trial's exit rule
# --------------------------------------------------------------------------- #
_OPTION_EXIT = {
    "id": "o1", "action": "buy_call", "option_strategy": "buy_call",
    "option_strike_param": 0.3,
    "option_strike_param_optimize": True,
    "option_strike_param_min": 0.2, "option_strike_param_max": 0.4,
    "option_strike_param_step": 0.05,
    "option_dte_optimize": True,
    "option_dte_min_range": 20, "option_dte_max_range": 45, "option_dte_step": 5,
}


def _strategy(exit_conditions):
    return types.SimpleNamespace(
        initial_tp_optimize=False, initial_tp_min=None, initial_tp_max=None,
        initial_tp_step=None,
        initial_sl_optimize=False, initial_sl_min=None, initial_sl_max=None,
        initial_sl_step=None,
        buy_entry_conditions=None, sell_entry_conditions=None, entry_conditions=None,
        initial_tp_percent=None, initial_sl_percent=None,
        exit_conditions=exit_conditions,
    )


def test_option_genes_emitted_and_decode_to_trial_rule():
    """collect_param_space emits exit:<id>:option_delta/option_dte AND decode_params writes
    them back onto the exit rule (option_strike_param, and a DTE *window* centered on the
    tuned value). The option_dte gene tunes the window CENTER; the decoded [min, max] must
    span >= 14 days so it covers a real (weekly) expiry instead of a single impossible day
    (min == max almost never matches a discrete expiry -> 0 fills). This is the gene ->
    trial-rule flow that _build_daily_trial_config then runs with the provider."""
    strategy = _strategy([dict(_OPTION_EXIT)])

    space = collect_param_space(strategy)
    assert "exit:o1:option_delta" in space
    assert "exit:o1:option_dte" in space

    decoded = decode_params(
        strategy, {"exit:o1:option_delta": 0.35, "exit:o1:option_dte": 30}
    )
    rule = decoded["exit_rules"][0]
    assert rule["option_strike_param"] == 0.35
    assert rule["option_dte_min"] <= 30 <= rule["option_dte_max"]
    assert rule["option_dte_max"] - rule["option_dte_min"] >= 14


def test_decoded_option_rule_drives_options_cache_in_trial_config():
    """End of the chain: the decoded option rule (from the genes) makes _build_daily_trial_config
    derive a non-None options_cache_db — i.e. the genes -> rule -> provider injection holds."""
    strategy = _strategy([dict(_OPTION_EXIT)])
    decoded = decode_params(
        strategy, {"exit:o1:option_delta": 0.35, "exit:o1:option_dte": 30}
    )
    cfg = H._build_daily_trial_config(_backtest_cfg(), decoded)
    assert cfg["options_cache_db"] is not None
    assert cfg["exit_rules"][0]["option_strike_param"] == 0.35
    # DTE decodes to a window centered on 30 (not a single impossible day).
    assert cfg["exit_rules"][0]["option_dte_min"] <= 30 <= cfg["exit_rules"][0]["option_dte_max"]
