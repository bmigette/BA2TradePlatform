"""SPY regime gates on the way to a LIVE instance (plan 2026-10-24 Task 12).

* a decoded genome's spy leaf exports as ``spy_trend_regime == <code>`` (never dropped, never a
  template); mode ``off`` exports no such trigger;
* the benchmark leaves need NO market-condition profile on the target expert, while an ordinary
  market leaf with the same empty setting is still refused;
* a server that predates the field refuses (strict name) instead of deploying ungated;
* the payload records the job's SPY source (``benchmark_regime``) only when the run was gated, and
  the importer WARNS (does not refuse) when that source is not the live one (FMP);
* the exported trigger, built into the live condition, answers like the backtest leaf.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date, datetime
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ba2_common.core import benchmark_regime as br
from ba2_common.core import rule_builders
from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_rules import (
    assert_market_conditions_resolved, assert_market_fields_served)
from ba2_common.core.rules_convert import trade_rules_to_live_export
from ba2_common.export import backtest_export as be

from app.services.strategy_param_space import collect_param_space, decode_params

_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
_spec = importlib.util.spec_from_file_location("ba2test_launcher_deploy_br", _LAUNCHER)
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)

TREND_GENE = "cond:o_csp-spy-trend:mode"
VOL_GENE = "cond:o_csp-spy-vol:mode"


def decoded(trend="above_200", vol="off"):
    saved = launcher._BENCHMARK_REGIME_GATES
    launcher._BENCHMARK_REGIME_GATES = True
    try:
        strategy = launcher._build_strategy("O_CSP", "deploy-br", "FMPRating")
    finally:
        launcher._BENCHMARK_REGIME_GATES = saved
    space = collect_param_space(strategy)
    flat = {TREND_GENE: trend, VOL_GENE: vol}
    assert not [g for g in flat if g not in space], "the launcher no longer emits the spy genes"
    out = decode_params(strategy, flat)
    return out["entry_rules"], out["exit_rules"]


def _triggers(entry_rules, exit_rules=()):
    export = trade_rules_to_live_export(entry_rules, list(exit_rules), name="gated")
    enter = [r for r in export["rulesets"] if r["subtype"] == "enter_market"]
    return export, [t for rs in enter for rule in rs["rules"] for t in rule["triggers"].values()]


# ------------------------------------------------------------------ 1. export
def test_a_decoded_spy_mode_exports_as_an_equality_on_its_code():
    entry, exits = decoded("above_200", "off")
    assert_market_conditions_resolved(entry, "entry_rules")
    export, triggers = _triggers(entry, exits)
    spy = [t for t in triggers if t["event_type"] in (br.TREND_FIELD, br.VOL_FIELD)]
    assert spy == [{"event_type": "spy_trend_regime", "operator": "==", "value": 5.0}]
    json.loads(json.dumps(export))  # round-trips


def test_both_modes_export_their_own_codes_and_off_exports_nothing():
    entry, _ = decoded("down", "not_stressed")
    _, triggers = _triggers(entry)
    got = {t["event_type"]: t["value"] for t in triggers if t["event_type"].startswith("spy_")}
    assert got == {"spy_trend_regime": 4.0, "spy_vol_regime": 4.0}
    entry_off, _ = decoded("off", "off")
    _, off_triggers = _triggers(entry_off)
    assert not [t for t in off_triggers if t["event_type"].startswith("spy_")]


# ------------------------------------------------------------------ 2. profile
def test_benchmark_leaves_need_no_profile_but_an_ohlcv_leaf_still_does():
    entry, _ = decoded("up", "calm")
    assert_market_fields_served(entry, [], where="entry")  # empty profile setting: fine
    ohlcv = [{"id": "x", "name": "x", "actions": [{"action_type": "buy"}],
              "conditions": {"id": "r", "operator": "AND", "conditions": [
                  {"id": "x-adx", "field": "underlying_adx_14", "op": "<", "value": 25.0}]}}]
    with pytest.raises(ValueError):
        assert_market_fields_served(ohlcv, [], where="entry")


# ------------------------------------------------------------------ 3. older server
def test_a_server_that_predates_the_field_refuses_rather_than_dropping_the_gate(monkeypatch):
    entry, _ = decoded("up", "off")
    monkeypatch.setattr(rule_builders, "FIELD_EVENT", {
        k: v for k, v in rule_builders.FIELD_EVENT.items()
        if k not in (br.TREND_FIELD, br.VOL_FIELD)})
    with pytest.raises(ValueError, match="no event type for"):
        trade_rules_to_live_export(entry, [])


# ------------------------------------------------------------------ 4. payload carries the source
def _bt():
    return SimpleNamespace(id=7, name="top1", expert_name="FMPRating", engine_type="daily_expert",
                           strategy_params={"model:profit_ratio": 1.2},
                           start_date=datetime(2024, 1, 1), end_date=datetime(2024, 6, 1),
                           initial_capital=10_000.0)


def test_payload_records_the_spy_source_only_for_a_gated_run():
    gated = {"benchmark_regime_gates": True, "ohlcv_history_source": "alpaca",
             "benchmark_regime": {"symbol": "SPY", "history_source": "alpaca"}}
    p = be.derive_export_payload(_bt(), "expert_settings", opt_backtest_block=gated)
    assert p["benchmark_regime"] == {"symbol": "SPY", "history_source": "alpaca"}
    plain = be.derive_export_payload(_bt(), "expert_settings",
                                     opt_backtest_block={"ohlcv_history_source": "alpaca"})
    assert "benchmark_regime" not in plain
    assert "benchmark_regime" not in be.derive_export_payload(_bt(), "expert_settings")


def test_a_gated_run_without_a_stamp_is_recorded_as_the_legacy_source():
    p = be.derive_export_payload(_bt(), "expert_settings",
                                 opt_backtest_block={"benchmark_regime_gates": True})
    assert p["benchmark_regime"] == {"symbol": "SPY", "history_source": "fmp"}


def test_the_importer_warns_on_a_non_live_source_and_stays_silent_on_fmp():
    entry, _ = decoded("up", "off")
    warns = br.benchmark_import_warnings({"symbol": "SPY", "history_source": "alpaca"}, entry, [])
    assert len(warns) == 1 and "alpaca" in warns[0] and "fmp" in warns[0].lower()
    assert br.benchmark_import_warnings({"symbol": "SPY", "history_source": "fmp"}, entry, []) == []
    # no gate in the rules: nothing to warn about, whatever the block says
    plain, _ = decoded("off", "off")
    assert br.benchmark_import_warnings(
        {"symbol": "SPY", "history_source": "alpaca"}, plain, []) == []
    # gated rules but the payload carries no block: the source is unknown, said loudly
    unknown = br.benchmark_import_warnings(None, entry, [])
    assert len(unknown) == 1 and "unknown" in unknown[0].lower()


# ------------------------------------------------------------------ 5. deployed-rule parity
def test_the_exported_trigger_builds_the_live_condition_and_answers_like_the_leaf():
    from ba2_common.core.TradeConditions import CONDITION_MAP
    from ba2_common.core.types import ExpertEventType

    entry, _ = decoded("above_200", "off")
    _, triggers = _triggers(entry)
    trig, = [t for t in triggers if t["event_type"] == br.TREND_FIELD]
    cls = CONDITION_MAP.get(ExpertEventType(trig["event_type"]))
    assert cls is not None
    cond = cls(account=None, instrument_name="AAPL", expert_recommendation=None,
               operator_str=trig["operator"], value=float(trig["value"]))
    dates = list(regular_sessions_ending_at(date(2026, 6, 30), 400))
    rising = [100.0 + i * 0.5 for i in range(len(dates))]
    br.reset_benchmark_regime()
    try:
        for closes, want in ((rising, True), (list(reversed(rising)), False)):
            br.set_benchmark_regime(br.classify_benchmark_regime(closes))
            assert cond.evaluate() is want
        br.reset_benchmark_regime()
        assert cond.evaluate() is False and cond.last_status == "no_regime"
    finally:
        br.reset_benchmark_regime()
