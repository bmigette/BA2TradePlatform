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


_IDS = iter(range(1000, 2000))


def _effective_stop(ruleset_sl_price, risk_pct, min_stop_pct, sizing_mode="risk_atr"):
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    account_id = next(_IDS)
    engine, _account, txn_id, ctx = _precedence_setup(
        account_id=account_id, expert_id=account_id, ruleset_sl_price=ruleset_sl_price,
        risk_pct=risk_pct, min_stop_pct=min_stop_pct, sizing_mode=sizing_mode)
    try:
        engine._size_and_submit(account_id, indicator_provider=None, as_of_dt=datetime(2024, 1, 2))
        return get_instance(Transaction, txn_id).stop_loss
    finally:
        ctx.__exit__(None, None, None)


def _sqm_floor():
    s = _M._expert_run_settings(_M._EXPERT_OPT["DeterministicScorer"], ["AAPL"], None, strategy_kind="SQM")
    assert "risk_per_trade_pct" not in s            # the risk gene is NOT pinned
    return s["min_stop_loss_pct"]


def _risk_values():
    g = _M._rm_opt_for("SQM", None)["risk_per_trade_pct"]
    lo, hi, st = g["min"], g["max"], g["step"]
    vals = []
    v = lo
    while v <= hi + 1e-9:
        vals.append(round(v, 6))
        v += st
    return vals


def test_sqm_pins_only_the_floor_and_keeps_the_risk_and_sizing_genes():
    assert _sqm_floor() == 15.0
    block = _M._rm_opt_for("SQM", None)
    assert "min_stop_loss_pct" not in block
    for kept in ("risk_per_trade_pct", "atr_risk_budget_pct", "max_virtual_equity_per_instrument_percent"):
        assert kept in block, kept
    # other strategies: nothing pinned, nothing dropped
    assert "min_stop_loss_pct" in _M._rm_opt_for("S2", None) and "risk_per_trade_pct" in _M._rm_opt_for("S2", None)
    other = _M._expert_run_settings(_M._EXPERT_OPT["DeterministicScorer"], ["AAPL"], None, strategy_kind="S2")
    assert "min_stop_loss_pct" not in other


def test_the_floor_clears_every_value_of_the_risk_gene_and_the_loosest_stop_gene():
    from app.services.strategy_param_space import collect_param_space
    floor = _sqm_floor()
    risks = _risk_values()
    assert max(risks) < floor and min(risks) > 0
    stop = collect_param_space(_M._build_strategy("SQM", "n", "DeterministicScorer"))["entry:buy:a2:action_value"]
    assert stop["max"] < 0 and abs(stop["min"]) < floor
    # the atr-searched policy uses the same risk range
    assert _M._rm_opt_for("SQM", None, atr_searched=True)["risk_per_trade_pct"]["max"] < floor


@pytest.mark.parametrize("sizing_mode", ["notional", "risk_atr"])
@pytest.mark.parametrize("risk", [0.5, 5.0, 10.0])
def test_genomes_differing_only_in_the_sqm_stop_get_different_effective_stops(risk, sizing_mode):
    assert risk in _risk_values()
    floor = _sqm_floor()
    tight = _effective_stop(94.0, risk, floor, sizing_mode)        # stop gene -6
    mid = _effective_stop(88.0, risk, floor, sizing_mode)          # -12 (the recipe)
    loose = _effective_stop(86.0, risk, floor, sizing_mode)        # -14
    assert (tight, mid, loose) == (pytest.approx(94.0), pytest.approx(88.0), pytest.approx(86.0))


def test_every_risk_gene_value_leaves_the_stop_gene_live():
    floor = _sqm_floor()
    for risk in _risk_values():
        assert _effective_stop(92.0, risk, floor) == pytest.approx(92.0), risk      # -8 survives at every risk value


def test_without_the_floor_pin_the_default_sizing_genes_make_the_stop_gene_dead():
    # What the pin prevents: default sizing (risk 10 / floor 7 -> safeguard distance 10%) swallows -12 and -14 alike.
    assert _effective_stop(88.0, 10.0, 7.0) == pytest.approx(90.0)
    assert _effective_stop(86.0, 10.0, 7.0) == pytest.approx(90.0)


@pytest.mark.parametrize("atr", [None, 0.2, 1.0, 5.0])
@pytest.mark.parametrize("risk", [0.5, 5.0, 10.0])
def test_the_safeguard_distance_is_the_floor_in_atr_mode_too(risk, atr):
    # use_atr_stop on: candidate = min(risk%, atr_mult * ATR), then floored at min_stop_pct -> 15% for every input
    from ba2_common.core.position_sizing import synthesize_safeguard_stop
    sl = synthesize_safeguard_stop(100.0, True, risk, atr=atr, atr_multiplier=4.0, min_stop_pct=_sqm_floor())
    assert sl == pytest.approx(85.0)
