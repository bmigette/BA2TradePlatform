"""``benchmark_regime_gates`` survives ``_build_daily_trial_config`` (a WHITELIST: a run-level knob
it does not write is inert however correctly it was parsed upstream), adds NO key when absent (the
trial config of every existing run stays key-for-key identical), and the SPY mode genes the
launcher emits reach the decoded trial rules and the engine's trigger map.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services.strategy_optimization_handler import _build_daily_trial_config  # noqa: E402
from app.services.strategy_param_space import collect_param_space, decode_params  # noqa: E402
from tests.test_gene_to_artefact_audit import _backtest_cfg  # noqa: E402

_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
_spec = importlib.util.spec_from_file_location("ba2test_launcher_btc", _LAUNCHER)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _strategy(monkeypatch, on):
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", on)
    return mod._build_strategy("O_LC", "btc-O_LC", "FMPRating")


def _trial(cfg, strat, genome=None, **kw):
    decoded = decode_params(strat, genome or {})
    return decoded, _build_daily_trial_config(cfg, decoded, None, option_trade_records=False, **kw)


def test_flag_reaches_the_trial_config(monkeypatch):
    strat = _strategy(monkeypatch, True)
    _, cfg = _trial({**_backtest_cfg(strat, "FMPRating"), "benchmark_regime_gates": True}, strat)
    assert cfg["benchmark_regime_gates"] is True


def test_absent_flag_adds_no_key(monkeypatch):
    strat = _strategy(monkeypatch, False)
    _, cfg = _trial(_backtest_cfg(strat, "FMPRating"), strat)
    assert "benchmark_regime_gates" not in cfg


def test_a_false_flag_adds_no_key_either(monkeypatch):
    strat = _strategy(monkeypatch, False)
    _, cfg = _trial({**_backtest_cfg(strat, "FMPRating"), "benchmark_regime_gates": False}, strat)
    assert "benchmark_regime_gates" not in cfg


def test_flag_changes_nothing_else_in_the_trial_config(monkeypatch):
    strat = _strategy(monkeypatch, False)
    base = _backtest_cfg(strat, "FMPRating")
    _, off = _trial(base, strat)
    _, on = _trial({**base, "benchmark_regime_gates": True}, strat)
    random_ids = ("benchmark_regime_gates", "backtest_id", "name")     # per-call uuid suffixes
    assert ({k: v for k, v in on.items() if k not in random_ids}
            == {k: v for k, v in off.items() if k not in random_ids})


def test_stored_row_rerun_keeps_the_flag(monkeypatch):
    strat = _strategy(monkeypatch, True)
    _, cfg = _trial({**_backtest_cfg(strat, "FMPRating"), "benchmark_regime_gates": True}, strat,
                    stored_row=True)
    assert cfg["benchmark_regime_gates"] is True


def test_the_spy_genes_reach_the_decoded_rules_and_the_engine_triggers(monkeypatch):
    """A gene the GA searches but the trial never applies is the 'parsed upstream, inert' trap."""
    from ba2_common.core.rule_builders import triggers_from_condition_tree

    strat = _strategy(monkeypatch, True)
    space = collect_param_space(strat)
    genome = {"cond:o_lc-spy-trend:mode": "down", "cond:o_lc-spy-vol:mode": "stressed"}
    assert set(genome) <= set(space)
    decoded, cfg = _trial({**_backtest_cfg(strat, "FMPRating"), "benchmark_regime_gates": True},
                          strat, genome)
    leaves = {}

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            if n.get("field") in ("spy_trend_regime", "spy_vol_regime"):
                leaves[n["field"]] = float(n["value"])
            for v in n.values():
                walk(v)

    walk(cfg["entry_rules"])
    assert leaves == {"spy_trend_regime": 4.0, "spy_vol_regime": 3.0}      # down=4, stressed=3
    tree = [r for r in cfg["entry_rules"]
            if isinstance(r.get("conditions"), dict) and r["conditions"].get("conditions")][0]
    events = {t["event_type"] for t in triggers_from_condition_tree(tree["conditions"]).values()}
    assert {"spy_trend_regime", "spy_vol_regime"} <= events
