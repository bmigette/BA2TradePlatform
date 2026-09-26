"""Stage-2 ``O_STYLE`` registration (plan docs/plans/2026-09-25-option-stage2-build.md, Task 7,
as revised by "Added after Tasks 3-4" and "Tasks 5-6 as built").

``O_STYLE`` is ONE launchable key whose strategy is built from a style spec
(``--style-config F --style NAME``). What this file pins:

* the key is a pure-option key (option fitness, holdout rail, market conditions, option RM
  ceiling, option fixed settings) but NEVER a grid-1 group, so the ``_OPTION_GROUPS`` premises
  of the older tests keep holding;
* the member-scoped hold: a style with a wheel member writes ``hold_assigned_stock_tags`` (the
  wheel's TAG VALUES from ``_STYLE_MEMBER_TAGS``, never rule ids) and ``hold_assigned_stock:
  False`` -- a run may not state both -- and the hold reaches every TRIAL config (the
  ``_build_daily_trial_config`` whitelist trap), the account's policy, and the deploy-parity
  refusal (live never liquidates assigned stock, so such a run has no honest export);
* the resolved style is recorded on the run config and reaches the trial config too;
* the per-run rules decided from the members: trade floor, price cap (Q3), convex fitness (Q5),
  lattice anchor -- each "any member" or refused by name;
* the CLI: the two flags are required with ``O_STYLE`` and refused without it; the batch
  command refuses the key.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L  # noqa: E402

from tests.test_equity_cap_launcher import _parse, _restore_globals, _isolated_globals  # noqa: E402

_CAND = str(Path(__file__).resolve().parents[3] / "tools/option_styles.candidates.json")
_WHEEL_TAG = 14


@pytest.fixture(autouse=True)
def _no_style_leaks(monkeypatch):
    """``_cmd_optimize`` sets the module's ``_STYLE_ACTIVE``; restore it after every test."""
    monkeypatch.setattr(L, "_STYLE_ACTIVE", None)
    monkeypatch.setattr(L, "_MARKET_CONDITION_PROFILES", L._MARKET_CONDITION_PROFILES)
    yield


def _argv(style, *extra, expert="FMPRating", strategy="O_STYLE", config=_CAND):
    argv = ["optimize", "--expert", expert, "--strategy", strategy, "--universe", "AAPL",
            "--start", "2024-03-01", "--end", "2024-04-01", "--population", "2",
            "--generations", "1", "--rerun", "--name", f"t7-{style}-{os.getpid()}"]
    if config is not None:
        argv += ["--style-config", config]
    if style is not None:
        argv += ["--style", style]
    return argv + list(extra)


def _run(argv, monkeypatch):
    """The REAL ``_cmd_optimize`` with the GA stubbed; the persisted optimization_config."""
    import app.services.strategy_optimization_handler as SOH
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization

    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    monkeypatch.setattr(SOH, "handle_strategy_optimization",
                        lambda task_id, payload: {"status": "completed"})
    monkeypatch.setattr(L, "_persist_top_backtests", lambda *a, **k: 0)
    args = _parse(argv)
    saved = _isolated_globals()
    try:
        assert L._cmd_optimize(args) == 0
    finally:
        _restore_globals(saved)
    db = SessionLocal()
    try:
        row = db.query(StrategyOptimization).order_by(StrategyOptimization.id.desc()).first()
        return row.id, row.optimization_config
    finally:
        db.close()


def _trial(block):
    """The per-trial config a remote worker receives (JSON round trip, as on the wire)."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    decoded = {"expert_overrides": {}, "entry_rules": [], "exit_rules": []}
    wire = json.loads(json.dumps(block, default=str))
    return _build_daily_trial_config(wire, decoded, option_trade_records=False)


def _with_style(name, **changes):
    style = L._load_style_spec(_CAND, name)
    style.update(changes)
    return style


# --- the key ----------------------------------------------------------------------------------
def test_o_style_is_a_pure_option_key_but_never_a_grid1_group():
    assert "O_STYLE" in L._STRATEGY_BUILDERS and "O_STYLE" in L._PURE_OPTION_STRATEGIES
    assert "O_STYLE" in L._OPTION_STRATEGY_KEYS and "O_STYLE" in L._MARKET_CONDITION_STRATEGIES
    assert "O_STYLE" in L._OPTION_CAR_STRATEGIES
    assert "O_STYLE" not in L._OPTION_GROUPS and "O_STYLE" not in L._OPTION_GROUPS_ALL
    assert "O_STYLE" in L._SPEC_DRIVEN_STRATEGIES
    # a style is never a member of a style
    assert "O_STYLE" not in L._style_admissible_members()


# --- the member-scoped hold (E2) --------------------------------------------------------------
def test_hold_tags_are_the_TAG_VALUES_of_the_members_that_hold():
    L._STYLE_ACTIVE = _with_style("premium")
    assert L._hold_assigned_stock("O_STYLE") is False        # never the run-level flag
    assert L._hold_assigned_stock_tags("O_STYLE") == [_WHEEL_TAG]
    assert _WHEEL_TAG == L._STYLE_MEMBER_TAGS["O_WHEEL"]
    L._STYLE_ACTIVE = _with_style("short")
    assert L._hold_assigned_stock_tags("O_STYLE") is None
    # every other key: no member-scoped hold, whatever the style state
    for kind in ("O_WHEEL", "O_CSP", "OS1", "S2", None):
        assert L._hold_assigned_stock_tags(kind) is None


def test_hold_tags_refuse_a_style_key_with_no_style_loaded():
    L._STYLE_ACTIVE = None
    with pytest.raises(SystemExit, match="--style-config"):
        L._hold_assigned_stock_tags("O_STYLE")


def test_premium_run_writes_the_wheels_tag_and_no_run_level_hold(monkeypatch):
    _, cfg = _run(_argv("premium"), monkeypatch)
    acct = cfg["backtest"]["account_settings"]
    assert acct["hold_assigned_stock"] is False
    assert acct["hold_assigned_stock_tags"] == [_WHEEL_TAG]


def test_a_style_without_the_wheel_writes_no_member_tags(monkeypatch):
    _, cfg = _run(_argv("short"), monkeypatch)
    acct = cfg["backtest"]["account_settings"]
    assert acct["hold_assigned_stock"] is False
    assert "hold_assigned_stock_tags" not in acct


def test_the_hold_tags_reach_the_trial_config_and_the_accounts_policy(monkeypatch):
    """``_build_daily_trial_config`` rebuilds each trial config key by key; the account block
    is carried whole, so the tags must arrive -- pinned, not assumed."""
    from ba2_common.core.assignment_hold import resolve_assigned_stock_hold_policy
    _, cfg = _run(_argv("premium"), monkeypatch)
    trial = _trial(cfg["backtest"])
    assert trial["account_settings"]["hold_assigned_stock_tags"] == [_WHEEL_TAG]
    policy = resolve_assigned_stock_hold_policy(trial["account_settings"])
    assert policy.member_tags == frozenset({_WHEEL_TAG}) and policy.run_level is False


def test_a_member_scoped_run_is_refused_by_deploy_parity_on_both_reads(monkeypatch):
    """The trial read (the handler's run facts) and the export read (the persisted
    optimization block) both see the member-scoped hold; the export refuses (409)."""
    from datetime import datetime
    from fastapi import HTTPException
    from app.api.backtests import _derive_export_payload
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest.daily_backtest_handler import _run_facts
    from ba2_common.core.deploy_parity import UndeployableRunRefused, refuse_undeployable

    opt_id, cfg = _run(_argv("premium"), monkeypatch)
    facts = _run_facts(_trial(cfg["backtest"]))
    assert facts.hold_assigned_stock_tags == (_WHEEL_TAG,)
    with pytest.raises(UndeployableRunRefused, match="hold_assigned_stock_tags"):
        refuse_undeployable(facts)
    bt = Backtest(name="TOP1-t7", expert_name="FMPRating", engine_type="daily_expert",
                  strategy_params={}, start_date=datetime(2024, 3, 1),
                  end_date=datetime(2024, 4, 1), initial_capital=20_000.0,
                  optimization_id=opt_id)
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc:
            _derive_export_payload(bt, "expert_settings", db=db)
    finally:
        db.close()
    assert exc.value.status_code == 409 and "hold_assigned_stock_tags" in str(exc.value.detail)


# --- the recorded style -------------------------------------------------------------------------
def test_the_run_config_records_the_resolved_style_and_the_trial_carries_it(monkeypatch):
    _, cfg = _run(_argv("short"), monkeypatch)
    rec = cfg["backtest"]["option_style"]
    expected = L._load_style_spec(_CAND, "short")
    assert rec == {**expected, "spec_path": _CAND}
    assert rec["name"] == "short" and [x["key"] for x in rec["members"]][:2] == ["O_LC", "O_LP"]
    assert len(rec["spec_sha256"]) == 64
    # the whitelist trap: a run-config key missing from the trial config is dead
    assert _trial(cfg["backtest"])["option_style"] == rec


def test_a_non_style_run_config_and_trial_have_no_style_key(monkeypatch):
    """Only style jobs write the key: every other stored and trial config keeps its shape."""
    _, cfg = _run(_argv(None, strategy="O_LC", config=None), monkeypatch)
    assert "option_style" not in cfg["backtest"]
    assert "option_style" not in _trial(cfg["backtest"])
    assert "hold_assigned_stock_tags" not in cfg["backtest"]["account_settings"]


def test_the_entry_action_is_the_first_option_members(monkeypatch):
    _, cfg = _run(_argv("premium"), monkeypatch)
    assert cfg["backtest"]["entry_action"]["action_type"] == L._OPTION_STRATS["O_BULLPS"][
        "action_type"]


def test_option_fixed_settings_reach_a_style_job(monkeypatch):
    """DeterministicScorer's ``macro_short_side=mirror`` is layered for option kinds; O_STYLE
    is one, and the setting reaches the trial's expert settings."""
    _, cfg = _run(_argv("short", expert="DeterministicScorer"), monkeypatch)
    assert cfg["backtest"]["experts"][0]["settings"]["macro_short_side"] == "mirror"
    assert _trial(cfg["backtest"])["experts"][0]["settings"]["macro_short_side"] == "mirror"


def test_the_option_rm_ceiling_applies_to_a_style():
    assert L._rm_opt_for("O_STYLE") == L._rm_opt_for("O_LC")


# --- per-run rules decided from the members -----------------------------------------------------
def test_price_cap_policy_none_gives_no_price_gate_and_group_min_the_lowest():
    L._STYLE_ACTIVE = _with_style("premium")
    assert L._STYLE_ACTIVE["price_cap_policy"] == "none"
    assert L._screener_gate_base_for_strategy("O_STYLE") == {}
    L._STYLE_ACTIVE = _with_style("premium", price_cap_policy="group_min")
    assert L._screener_gate_base_for_strategy("O_STYLE") == {"price_max": 100.0}
    # members that all agree: their common base, whatever the policy
    L._STYLE_ACTIVE = _with_style("short")
    assert L._screener_gate_base_for_strategy("O_STYLE") == {}


def test_the_price_gate_needs_a_loaded_style():
    L._STYLE_ACTIVE = None
    with pytest.raises(SystemExit, match="--style-config"):
        L._screener_gate_base_for_strategy("O_STYLE")


def _gate_args(max_price):
    return SimpleNamespace(screener_gate_store="store.parquet", screener=False,
                           max_stock_price=max_price, screener_base_json=None,
                           screener_cadence_days=7)


def test_policy_none_under_a_blanket_price_cap_is_refused():
    """``none`` promises NO price gate; ``--max-stock-price`` would put one back silently."""
    L._STYLE_ACTIVE = _with_style("premium")
    with pytest.raises(SystemExit, match="price_cap_policy 'none'.*--max-stock-price"):
        L._screener_gate_opt_block(_gate_args(100.0), "O_STYLE")
    blk = L._screener_gate_opt_block(_gate_args(0), "O_STYLE")
    assert "price_max" not in blk["base_settings"]
    L._STYLE_ACTIVE = _with_style("premium", price_cap_policy="group_min")
    blk = L._screener_gate_opt_block(_gate_args(300.0), "O_STYLE")
    assert blk["base_settings"]["price_max"] == 100.0


def test_a_convex_member_under_a_car_fitness_is_refused_without_a_waiver():
    L._STYLE_ACTIVE = _with_style("leaps_convex")
    with pytest.raises(SystemExit, match="Q5"):
        L._refuse_convex_fitness_mismatch("optimize", "O_STYLE", "option_car_target_soft30")
    with pytest.raises(SystemExit, match="O_CONVEX-only"):
        L._refuse_convex_fitness_mismatch("optimize", "O_STYLE", "option_convex")
    L._STYLE_ACTIVE = _with_style("leaps_convex",
                                  convex_fitness_waiver="user: accepted 2026-10-xx")
    L._refuse_convex_fitness_mismatch("optimize", "O_STYLE", "option_car_target_soft30")
    # a style with no convex member is untouched by the Q5 rule
    L._STYLE_ACTIVE = _with_style("short")
    L._refuse_convex_fitness_mismatch("optimize", "O_STYLE", "option_car_target_soft30")


def test_the_leaps_convex_candidate_refuses_to_launch_under_the_default_fitness(monkeypatch):
    with pytest.raises(SystemExit, match="Q5"):
        _run(_argv("leaps_convex"), monkeypatch)


def test_the_low_trade_floor_applies_when_any_member_is_long_dated():
    L._STYLE_ACTIVE = _with_style("leaps_convex")
    blk = {}
    L._apply_option_trade_floor("O_STYLE", blk)
    assert blk == L._OPTION_LOW_TRADE_FLOOR
    L._STYLE_ACTIVE = _with_style("premium")
    blk = {}
    L._apply_option_trade_floor("O_STYLE", blk)
    assert blk == {}


def test_the_lattice_anchor_is_min_when_any_member_is_grid2():
    """The LEAPS DTE genes decode below their 365 floor on the zero lattice (Q12)."""
    L._STYLE_ACTIVE = _with_style("leaps_convex")
    assert L._lattice_anchor_for("O_STYLE", None) == "min"
    assert L._lattice_anchor_for("O_STYLE", "zero") == "zero"    # an explicit override wins
    L._STYLE_ACTIVE = _with_style("short")
    assert L._lattice_anchor_for("O_STYLE", None) == "zero"


# --- the CLI ------------------------------------------------------------------------------------
@pytest.mark.parametrize("argv, msg", [
    (_argv("short", strategy="S2"), "--style-config/--style.*only with --strategy O_STYLE"),
    (_argv(None, strategy="O_LC"), "--style-config/--style.*only with --strategy O_STYLE"),
    (_argv(None), "O_STYLE needs both --style-config"),
    (_argv("short", config=None), "O_STYLE needs both --style-config"),
    (_argv("nope"), "no style 'nope'"),
    (_argv("short", expert="FactorRanker"), "bypass"),
])
def test_style_flags_are_refused_without_o_style_and_required_with_it(argv, msg, monkeypatch):
    with pytest.raises(SystemExit, match=msg):
        _run(argv, monkeypatch)


def test_a_builder_refusal_reaches_the_cli_naming_style_and_member(tmp_path, monkeypatch):
    """A refusal the loader cannot see (an override merged with the authored band) is a clean
    exit naming the style and the member (in the builder's message, once), not a traceback."""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"schema": 1, "styles": {"x": {"status": "candidate", "members": [
        {"key": "O_LC", "action_overrides": {"option_dte_min": 50}}]}}}))
    with pytest.raises(SystemExit, match=r"^optimize: style 'x' member O_LC: .*option_dte_min=50"
                       r"[^\[]*$"):
        _run(_argv("x", config=str(p)), monkeypatch)


def test_optimize_batch_refuses_o_style(monkeypatch):
    import app.services.task_queue as TQ

    class _Queue:          # never poll a real queue: a missed refusal must fail, not hang
        def queue_task(self, **kwargs):
            raise AssertionError("optimize-batch enqueued a job instead of refusing O_STYLE")
    monkeypatch.setattr(TQ, "get_task_queue", lambda: _Queue())
    args = _parse(["optimize-batch", "--experts", "FMPRating", "--strategies", "S2,O_STYLE",
                   "--universe", "AAPL", "--start", "2024-03-01", "--end", "2024-04-01",
                   "--population", "2", "--generations", "1"], "_cmd_optimize_batch")
    saved = _isolated_globals()
    try:
        with pytest.raises(SystemExit, match="O_STYLE.*`optimize`"):
            L._cmd_optimize_batch(args)
    finally:
        _restore_globals(saved)
