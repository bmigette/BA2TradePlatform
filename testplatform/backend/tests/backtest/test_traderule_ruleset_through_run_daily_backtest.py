"""THE GA'S OWN ENTRY POINT, DRIVEN BY A TradeRule-SHAPED RULESET, ASSERTING FILLS.

WHY THIS FILE EXISTS. The unified rule model (migration 028) made ``entry_rules`` /
``exit_rules`` -- lists of TradeRule rows, each with an ``actions`` LIST -- the shape the GA
emits (``decode_params``) and the shape ``_build_daily_trial_config`` forwards per trial. Every
existing test covered ONE link of that chain and stopped short of the whole thing:

  * ``tests/backtest/test_grid2_engine_paths.py`` drives TradeRule rulesets end to end, but
    through ``DailyBacktestEngine`` constructed BY HAND (it seeds the rulesets itself via
    ``seed_entry_ruleset_from_rules`` and builds the account/price source directly). It never
    goes through ``run_daily_backtest``, so it cannot see a defect in the handler's own
    ``_build_experts`` seeding fork.
  * ``tests/backtest/test_daily_engine_e2e.py`` goes through the handler, but on the LEGACY
    path -- no ``entry_rules`` at all, so ``_seed_enter`` takes its "bullish + flat -> buy"
    convenience default.
  * ``tests/backtest/test_deploy_round_trip_parity.py`` compares the DECODED artefacts on both
    sides; it never RUNS a backtest.

So the one arrangement the GA actually uses in production -- a TradeRule-shaped ruleset going
through ``run_daily_backtest`` (the ``_trial_worker`` entry point) and coming out with FILLS --
had no test. A standalone harness run during the branch's perf pass produced ZERO orders in
exactly that arrangement, and the branch closed with that unresolved: either the handler's
``_is_trade_rules`` / ``_seed_enter`` fork silently drops the rules (a defect that would make
EVERY GA trial on the unified model trade nothing while the fitness still reported a number),
or the harness's own expert simply had no signal on its symbol. Those two have opposite
consequences and the STATE note could not tell them apart.

WHAT THIS PINS. Three runs over the same hermetic fixture cache, same expert, same window,
differing ONLY in the ruleset shape:

  A. control -- no ``entry_rules``: the legacy default path (the shape
     ``test_daily_engine_e2e`` already covers), establishing that the fixture DOES signal.
  B. the same intent expressed as TradeRule rows, produced by the shared converter
     ``ba2_common.core.rule_models.trade_rules_from_legacy`` -- the ``_is_trade_rules`` fork.
  C. the LAUNCHER'S OWN emitted rules for ``S1`` through ``decode_params`` -- byte-for-byte
     the artefact a real GA trial carries, including its multi-action entry rule
     (buy + adjust_take_profit + adjust_stop_loss) and its three exit rules.

A/B/C all filling is the proof; A filling while B or C does not would localise the defect to
the handler's seeding fork rather than to signal absence, which is exactly the discrimination
the STATE note's open question needed.

Deliberately ``>= 1`` rather than an exact count: this file's subject is REACHABILITY of the
fill path from the TradeRule shape, not a frozen equity curve. The exact-number baselines live
in ``test_equity_golden_run.py`` (fingerprint) and the frozen-fitness suites; duplicating a
count here would make an unrelated fixture refresh fail this test for a reason that has nothing
to do with what it is protecting. C is additionally asserted to be no WORSE than A: S1's gates
are permissive at template defaults, so a regression that silently pruned its entry rule down
to nothing would still leave the run trading via some other path, and only the comparison
catches that.

Run from the backend dir:
    python -m pytest tests/backtest/test_traderule_ruleset_through_run_daily_backtest.py -q
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from typing import Any, Dict

import pytest

from tests.backtest.fixtures.e2e_support import hermetic_providers
from tests.backtest.fixtures.hermetic_providers import (
    EARNINGS_DRIFT_SETTINGS,
    TRADE_END,
    TRADE_START,
    UNIVERSE,
)

# tests/backtest/ -> tests/ -> backend/ -> testplatform/, then the launcher beside backend/.
# Resolved off __file__, never off the CWD: this suite is run both from the backend dir and
# from the repo root, and a CWD-relative path silently resolves to the OTHER checkout.
_LAUNCHER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_traderule_rdb", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_traderule_rdb"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:  # the launcher's argparse main() runs at import
        pass
    return m


# The BacktestAccount's resolved config, the exact key set ``_build_config`` assembles. Spelled
# out rather than routed through ``_build_config`` so this file pins the shape ``run_daily_backtest``
# is CONTRACTED to accept (the GA builds it in ``_build_daily_trial_config``, not from a payload).
_ACCOUNT_SETTINGS: Dict[str, Any] = {
    "starting_cash": 100_000.0,
    "equity_cap": None,
    "commission_per_trade": 1.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
    "spread_bps": 0.0,
    "option_spread_pct": 0.0,
    "option_spread_min_tick": 0.0,
    "hold_assigned_stock": False,
}


def _cfg(tag: str) -> Dict[str, Any]:
    """The ``_build_daily_trial_config`` return shape, minus the optional per-trial knobs."""
    return {
        "backtest_id": f"traderule-rdb-{tag}",
        "name": f"traderule-rdb-{tag}",
        "start_date": TRADE_START.isoformat(),
        "end_date": TRADE_END.isoformat(),
        "enabled_instruments": list(UNIVERSE),
        "experts": [{"class": "FMPEarningsDrift", "settings": dict(EARNINGS_DRIFT_SETTINGS)}],
        "initial_capital": 100_000.0,
        "account_settings": dict(_ACCOUNT_SETTINGS),
        "warmup_days": 30,
        "seed": 42,
    }


def _run(cfg: Dict[str, Any], seeders: Dict[str, list] | None = None) -> Dict[str, Any]:
    """``run_daily_backtest`` under the fixture providers, with file logging off.

    ``logging.disable`` because a direct ``run_daily_backtest`` call bypasses the GA's own
    logging suppression -- a per-bar logging run is an order of magnitude slower for output
    nothing reads.

    ``seeders``: when given, a dict this fills with ``{seeder_name: [rules_arg, ...]}`` for
    every enter-ruleset seeding function ``_build_experts`` could have chosen. WHY A SPY AND
    NOT JUST THE TRADE COUNT: ``_seed_enter``'s LEGACY arm also accepts ``entry_rules`` (as
    ``seed_ruleset_from_tree(entry_actions=...)``), so a defect that made ``_is_trade_rules``
    answer False would silently reroute a TradeRule config down the legacy arm AND STILL FILL.
    Verified by mutation: forcing ``_is_trade_rules`` to return False left the trade counts
    unchanged, so the fills alone pin nothing about WHICH path ran. ``_build_experts`` does its
    ``from ... import`` INSIDE the function, so patching the module attribute is picked up at
    call time.
    """
    import app.services.backtest.default_rulesets as DR
    from app.services.backtest.daily_backtest_handler import run_daily_backtest

    watched = ("seed_entry_ruleset_from_rules", "seed_ruleset_from_tree",
               "seed_enter_long_ruleset", "seed_enter_long_short_ruleset")
    originals = {n: getattr(DR, n) for n in watched}
    if seeders is not None:
        for name in watched:
            seeders.setdefault(name, [])

            def _spy(*a, _n=name, _f=originals[name], **kw):
                seeders[_n].append(a[0] if a else kw.get("entry_rules"))
                return _f(*a, **kw)

            setattr(DR, name, _spy)

    prev = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with hermetic_providers():
            return run_daily_backtest(cfg)
    finally:
        logging.disable(prev)
        for name, fn in originals.items():
            setattr(DR, name, fn)


@pytest.fixture(scope="module")
def control_trades() -> int:
    """A: no ``entry_rules`` -- the legacy default seeding path. Establishes fixture signal."""
    return int(_run(_cfg("A-control"))["total_trades"])


def test_the_control_run_without_entry_rules_trades(control_trades):
    """Not the subject -- the CALIBRATION. Without it, a zero in B or C is unattributable:
    a fixture that stopped signalling and a handler that dropped the rules look identical."""
    assert control_trades >= 1


def test_a_converted_TradeRule_ruleset_fills_through_run_daily_backtest(control_trades):
    """B: the ``_is_trade_rules`` fork in ``_build_experts._seed_enter``.

    The rules come from the SHARED converter rather than a literal, so a change to the
    canonical TradeRule shape moves this test with the production code instead of leaving it
    asserting against a shape nothing emits any more.
    """
    from ba2_common.core.rule_models import trade_rules_from_legacy

    converted = trade_rules_from_legacy(
        buy_tree={"id": "root", "type": "AND", "conditions": [
            {"id": "gate_confidence", "field": "confidence", "op": ">", "value": 0}]},
        exit_conditions=[
            {"id": "tp", "enabled": True, "action_type": "close_position",
             "conditions": {"id": "r", "type": "AND", "conditions": [
                 {"id": "c1", "field": "profit_loss_percent", "op": ">", "value": 5}]}},
        ],
    )
    entry_rules = converted["entry_rules"]
    assert entry_rules, "the converter must emit at least one entry rule"
    # The shape the handler's fork keys on: a row whose ``actions`` is a LIST. If this ever
    # stops holding, ``_is_trade_rules`` returns False and the run silently takes the LEGACY
    # default ruleset -- the run would still trade, and the assert below would still pass,
    # while testing nothing. Pin the discriminator itself.
    assert isinstance(entry_rules[0].get("actions"), list)

    cfg = _cfg("B-traderule")
    cfg["entry_rules"] = entry_rules
    cfg["exit_rules"] = converted["exit_rules"]
    seeders: Dict[str, list] = {}
    res = _run(cfg, seeders)

    # The fork actually taken. Without this the test passes with _is_trade_rules broken.
    assert seeders["seed_entry_ruleset_from_rules"] == [entry_rules], (
        "the unified TradeRule seeder was not handed these rules -- _is_trade_rules did not "
        f"recognise the shape; seeders called: { {k: len(v) for k, v in seeders.items()} }")
    assert not seeders["seed_ruleset_from_tree"]
    assert not seeders["seed_enter_long_ruleset"]
    assert not seeders["seed_enter_long_short_ruleset"]

    assert res["total_trades"] >= 1, (
        "a TradeRule-shaped ruleset produced NO fills through run_daily_backtest while the "
        f"control run produced {control_trades} -- the handler's _seed_enter fork is dropping "
        "the rules, and every GA trial on the unified rule model is scoring a config that "
        "never trades")


def test_the_launchers_own_S1_rules_fill_through_run_daily_backtest(control_trades):
    """C: the real artefact -- ``_build_strategy('S1')`` -> ``decode_params`` -> the trial config.

    An empty flat-params dict is the GA's template point (no genes applied, nothing pruned),
    which is a real point in the searched space and the one every genome is a perturbation of.
    """
    from app.services.strategy_param_space import decode_params

    m = _launcher()
    strategy = m._build_strategy("S1", "traderule-rdb-S1", "FMPEarningsDrift")
    decoded = decode_params(strategy, {})

    entry_rules = decoded["entry_rules"]
    exit_rules = decoded["exit_rules"]
    assert entry_rules and exit_rules, "S1 must emit both entry and exit TradeRules"
    assert isinstance(entry_rules[0]["actions"], list)
    # S1's entry rule is MULTI-action (open + the TP/SL bracket). The legacy seeding path could
    # only carry one action per rule, so this is the property the unified model exists for --
    # assert it, or a regression that kept only the first action would still fill and pass.
    assert any(len(r["actions"]) > 1 for r in entry_rules), (
        "S1's entry rule must carry its TP/SL bracket alongside the open action")

    cfg = _cfg("C-S1")
    cfg["entry_rules"] = entry_rules
    cfg["exit_rules"] = exit_rules
    seeders: Dict[str, list] = {}
    res = _run(cfg, seeders)

    assert seeders["seed_entry_ruleset_from_rules"] == [entry_rules], (
        "S1's decoded entry rules did not reach the unified seeder -- the trial's ruleset was "
        "built by some other arm of _seed_enter, so the GA would be scoring a ruleset it did "
        "not emit")
    assert not seeders["seed_ruleset_from_tree"]

    assert res["total_trades"] >= 1, (
        "the launcher's own S1 ruleset produced NO fills through run_daily_backtest while the "
        f"control produced {control_trades} -- every S1 GA trial would be scoring a "
        "config that never trades")
    assert res["total_trades"] >= control_trades, (
        f"S1 traded {res['total_trades']} vs the control's {control_trades}: S1's gates are "
        "permissive at template defaults, so trading LESS than the ungated control means part "
        "of its entry rule was pruned or never reached the ruleset")
