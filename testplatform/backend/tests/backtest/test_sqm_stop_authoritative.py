"""goal2027 2.8: the SQM entry-stop gene must be LIVE in a GA trial.

At entry the strategy's ruleset stop and the RM's safeguard stop coexist and the TIGHTER wins
(``position_sizing.reconcile_protective_stop``; ``allow_ruleset_sl_loosen`` plays no part at entry). With ATR off the
safeguard distance is ``max(risk_per_trade_pct, min_stop_loss_pct)``, so under the default sizing genes (3%..15%) any
stop gene looser than that distance is dead. ``ba2test_launcher._STRATEGY_FIXED_SETTINGS`` pins SQM's floor at 15%,
looser than the loosest stop gene (-14%), so the ruleset stop is always the tighter one.

These tests drive the real ``DailyBacktestEngine._size_and_submit`` with the settings a SQM job really runs on.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime

import pytest

from tests.backtest.test_entry_bracket_engine import _precedence_setup

_TESTPLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_sqm_stop", os.path.join(_TESTPLATFORM, "ba2test_launcher.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_sqm_stop"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()


def _effective_stop(account_id, ruleset_sl_price, risk_pct, min_stop_pct):
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    engine, _account, txn_id, ctx = _precedence_setup(
        account_id=account_id, expert_id=account_id, ruleset_sl_price=ruleset_sl_price,
        risk_pct=risk_pct, min_stop_pct=min_stop_pct)
    try:
        engine._size_and_submit(account_id, indicator_provider=None, as_of_dt=datetime(2024, 1, 2))
        return get_instance(Transaction, txn_id).stop_loss
    finally:
        ctx.__exit__(None, None, None)


def _sqm_pins():
    s = _M._expert_run_settings(_M._EXPERT_OPT["DeterministicScorer"], ["AAPL"], None, strategy_kind="SQM")
    return s["risk_per_trade_pct"], s["min_stop_loss_pct"]


def test_sqm_jobs_pin_the_floor_above_the_loosest_stop_gene_and_drop_the_dead_rm_genes():
    risk, floor = _sqm_pins()
    assert floor == 15.0 and risk == 10.0
    block = _M._rm_opt_for("SQM", None)
    assert "min_stop_loss_pct" not in block and "risk_per_trade_pct" not in block
    assert "min_stop_loss_pct" in _M._rm_opt_for("S2", None) and "risk_per_trade_pct" in _M._rm_opt_for("S2", None)
    other = _M._expert_run_settings(_M._EXPERT_OPT["DeterministicScorer"], ["AAPL"], None, strategy_kind="S2")
    assert "min_stop_loss_pct" not in other and "risk_per_trade_pct" not in other      # other strategies unchanged


def test_two_genomes_differing_only_in_the_sqm_stop_get_different_effective_stops():
    risk, floor = _sqm_pins()
    tight = _effective_stop(911, 94.0, risk, floor)        # gene -6
    loose = _effective_stop(912, 86.0, risk, floor)        # gene -14
    assert tight == pytest.approx(94.0) and loose == pytest.approx(86.0)
    assert tight != loose


def test_the_gene_range_is_inside_the_pinned_floor():
    from app.services.strategy_param_space import collect_param_space
    sp = collect_param_space(_M._build_strategy("SQM", "n", "DeterministicScorer"))
    risk, floor = _sqm_pins()
    stop = sp["entry:buy:a2:action_value"]
    assert stop["max"] < 0 and abs(stop["min"]) < floor and max(risk, floor) == floor


def test_without_the_pin_the_default_sizing_genes_make_the_stop_gene_dead():
    # What the pin prevents: default sizing (risk 10 / floor 7 -> safeguard distance 10%) swallows -12 and -14 alike.
    a = _effective_stop(913, 88.0, 10.0, 7.0)              # gene -12
    b = _effective_stop(914, 86.0, 10.0, 7.0)              # gene -14
    assert a == pytest.approx(90.0) and b == pytest.approx(90.0)
