"""DEPLOY ROUND TRIP: what the live ExpertInstance gets == what the backtest engine ran.

The operator principle says a GA gene may live only in the emitted ruleset or the expert
settings, because those two artefacts are what a deploy carries from a scored Backtest row to a
live ExpertInstance. This test walks that carriage end to end for a grid-2 key (``O_ERN`` under
``FMPEarningsEvent``) and for a convex member (``O_CONVEXC``, inside the ``O_CONVEX`` group) and
asserts the two sides are IDENTICAL:

  BACKTEST SIDE   genome -> decode_params -> _build_daily_trial_config -> the trial's
                  entry_rules/exit_rules and expert settings, and -- one step further, to the
                  artefact the ENGINE actually evaluates -- ``default_rulesets``' seeded
                  Ruleset/EventAction rows.
  LIVE SIDE       the same genome persisted on a Backtest row exactly as
                  ``ba2test_launcher._persist_top_backtests`` writes it, then through the REAL
                  deploy tooling: ``tools/export_deploy_payload.py``'s engine
                  (``app.api.backtests._derive_export_payload``, for both the ``ruleset`` and
                  the ``expert_settings`` kinds) and ``tools/import_deploy_payload.py``'s
                  conversion (``rules_convert.trade_rules_to_live_export``).

WHY THE TOOLS' ``main()`` IS NOT CALLED. Both scripts are thin CLI wrappers that ``os.chdir``
into a fixed ``BA2_REPO`` and reconfigure the PROCESS-GLOBAL db module (the export against the
test DB, the import against the LIVE trade DB -- deliberately two processes, per their own
docstrings). Calling either inside pytest would repoint this process's DB at a real platform
database. So the test calls the exact functions they call, and
``test_the_tools_still_route_through_the_functions_this_test_pins`` reads their SOURCE to prove
that is still the route -- the drift guard that makes the substitution honest.

Run from the backend dir:
    python -m pytest tests/backtest/test_deploy_round_trip_parity.py -v
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session as SQLModelSession

import app.models  # noqa: F401 -- registers every model class on Base.metadata
from app.models.database import Base
from app.models.backtest import Backtest
from app.models.strategy import Strategy as StrategyModel
from app.models.strategy_optimization import StrategyOptimization

# tests/backtest/ -> tests/ -> backend/ -> testplatform/, then the launcher beside backend/.
_TESTPLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_LAUNCHER_PATH = os.path.join(_TESTPLATFORM, "ba2test_launcher.py")
_TOOLS = os.path.join(os.path.dirname(_TESTPLATFORM), "tools")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_deploy", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_deploy"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()

#: (strategy key, expert, the rule id whose per-member round trip is asserted).
CASES = [
    ("O_ERN", "FMPEarningsEvent", "o_ern-entry"),
    ("O_CONVEX", "FMPRating", "o_convexc-entry"),
]


# ==================================================================================================
# fixtures
# ==================================================================================================
@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("deploydb") / "deploy.sqlite"
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


# ==================================================================================================
# the two sides
# ==================================================================================================
def _genome_and_strategy(key, expert):
    """The launcher's own gene space for this key, and a NON-DEFAULT genome over it (every value
    gene at its domain max, categoricals at the last choice, toggles ON) -- the same construction
    tests/test_gene_to_artefact_audit.py uses, so both tests audit the same genome shape."""
    from app.services.strategy_param_space import collect_param_space

    strat = _M._build_strategy(key, f"deploy-{key}", expert)
    cfg = {**_M._EXPERT_OPT[expert]["expert_params"], **_M._rm_opt_for(key),
           **{f"schedule:{k}": v for k, v in _M._SCHEDULE_DAY_OPT.items()}}
    model_cfg = {k: v for k, v in cfg.items() if not k.startswith("schedule:")}
    schedule_cfg = {k[len("schedule:"):]: v for k, v in cfg.items() if k.startswith("schedule:")}
    space = collect_param_space(strat, expert_cfg=model_cfg, schedule_cfg=schedule_cfg)

    def _val(d):
        if d["type"] == "choice":
            return d["choices"][-1]
        return int(d["max"]) if d["type"] == "int" else float(d["max"])

    return strat, {g: _val(d) for g, d in space.items()}


def _bt_block(strat, expert):
    """The run-level ``optimization_config['backtest']`` block the launcher writes and both
    ``_build_daily_trial_config`` (backtest) and ``_opt_backtest_block`` (deploy export) read."""
    return {
        "backtest_id": "deploy-parity", "start_date": "2024-02-01", "end_date": "2024-06-01",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": expert, "settings": {"allow_automated_trade_opening": True}}],
        "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
        "entry_action": getattr(strat, "entry_action", None),
        "options_store": "parquet", "execution_interval": "1d",
    }


def _backtest_side(key, expert):
    """decode -> trial config: the rules and settings the ENGINE receives."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params

    strat, genome = _genome_and_strategy(key, expert)
    decoded = decode_params(strat, genome)
    trial = _build_daily_trial_config(_bt_block(strat, expert), decoded, None)
    return strat, genome, decoded, trial


def _persisted_row(db, key, expert, strat, genome, decoded):
    """A Backtest row written EXACTLY as ``_persist_top_backtests`` writes a top-N row: the raw
    genes as ``strategy_params``, plus the concrete decoded ``entryRules``/``exitRules``, linked
    to the StrategyOptimization whose ``optimization_config['backtest']`` is the run block."""
    srow = StrategyModel(name=f"deploy-{key}", entry_rules=strat.entry_rules,
                         exit_rules=strat.exit_rules)
    db.add(srow)
    db.commit()
    db.refresh(srow)

    opt = StrategyOptimization(
        strategy_id=srow.id, name=f"opt-{key}", fitness_metric="option_car",
        optimization_type="genetic", status="completed",
        optimization_config={"populationSize": 10, "generations": 1, "seed": 1,
                             "backtest": _bt_block(strat, expert)},
    )
    db.add(opt)
    db.commit()
    db.refresh(opt)

    strategy_params = dict(genome)
    strategy_params["entryRules"] = decoded["entry_rules"]
    strategy_params["exitRules"] = decoded["exit_rules"]
    bt = Backtest(
        name=f"TOP1-{key}", expert_name=expert, engine_type="daily_expert", status="completed",
        start_date=datetime(2024, 2, 1), end_date=datetime(2024, 6, 1),
        initial_capital=20_000.0, optimization_id=opt.id, strategy_params=strategy_params,
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)
    return bt


def _live_side(db, bt, label):
    """The deploy payload, through the tools' own two functions."""
    from app.api.backtests import _derive_export_payload
    from ba2_common.core.rules_convert import trade_rules_to_live_export

    ruleset = _derive_export_payload(bt, "ruleset", db)
    settings = _derive_export_payload(bt, "expert_settings", db)
    live_export = trade_rules_to_live_export(ruleset["entry_rules"], ruleset["exit_rules"],
                                             name=label)
    return live_export, settings["settings"]["expert_params"]


# ==================================================================================================
# THE ROUND TRIP
# ==================================================================================================
@pytest.mark.parametrize("key,expert,rule_id", CASES,
                         ids=[f"{k}|{e}" for k, e, _ in CASES])
def test_the_deployed_ruleset_is_the_ruleset_the_backtest_ran(db, key, expert, rule_id):
    from ba2_common.core.rules_convert import trade_rules_to_live_export

    strat, genome, decoded, trial = _backtest_side(key, expert)
    bt = _persisted_row(db, key, expert, strat, genome, decoded)
    label = f"deploy-{key}"
    live_export, _live_settings = _live_side(db, bt, label)

    # The BACKTEST's own rules put through the same converter: this is the comparison that
    # matters, because it is the shape both runtimes consume (EventAction triggers + actions).
    bt_export = trade_rules_to_live_export(trial["entry_rules"], trial["exit_rules"], name=label)
    assert live_export == bt_export, (
        f"{key}: the deploy payload's ruleset differs from the one the backtest engine ran")

    # And the member's own rule really is in there (not an empty export that trivially matches).
    names = {r["name"] for rs in live_export["rulesets"] for r in rs["rules"]}
    assert any(rule_id.replace("-entry", "").upper() in n.upper() for n in names), (
        f"{key}: no exported rule for {rule_id!r}; exported {sorted(names)}")


@pytest.mark.parametrize("key,expert,rule_id", CASES,
                         ids=[f"{k}|{e}" for k, e, _ in CASES])
def test_the_deployed_expert_settings_are_the_settings_the_backtest_ran(db, key, expert, rule_id):
    strat, genome, decoded, trial = _backtest_side(key, expert)
    bt = _persisted_row(db, key, expert, strat, genome, decoded)
    _live_export, live_settings = _live_side(db, bt, f"deploy-{key}")

    assert live_settings == trial["experts"][0]["settings"], (
        f"{key}: the deploy payload's expert settings differ from the trial's")
    # Non-vacuous: every optimized model:* gene is in there at its optimized value.
    for gene, value in genome.items():
        if gene.startswith("model:"):
            assert live_settings[gene[len("model:"):]] == value


@pytest.mark.parametrize("key,expert,rule_id", CASES,
                         ids=[f"{k}|{e}" for k, e, _ in CASES])
def test_the_seeded_backtest_ruleset_matches_the_deploy_payload_action_for_action(
        db, key, expert, rule_id):
    """One step further than the export/export comparison: the BACKTEST engine does not read the
    export, it reads Ruleset/EventAction rows ``default_rulesets`` seeds. Assert those rows carry
    the same triggers and the same actions, in the same order, as the deploy payload's rules --
    i.e. the live ExpertInstance evaluates exactly what the trial evaluated.

    DB fixture pattern from tests/backtest/test_sl_ml_authored_off_parity.py."""
    from app.services.backtest import default_rulesets as dr
    from app.services.backtest.backtest_db import backtest_trading_db
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.models import Ruleset
    from ba2_common.core.types import AnalysisUseCase
    import ba2_common.core.db as cdb

    strat, genome, decoded, trial = _backtest_side(key, expert)
    bt = _persisted_row(db, key, expert, strat, genome, decoded)
    live_export, _ = _live_side(db, bt, f"deploy-{key}")

    wire_backtest_seams()
    with backtest_trading_db(f"deploy-parity-{key}"):
        seeded = {}
        for use_case, rules in ((AnalysisUseCase.ENTER_MARKET, trial["entry_rules"]),
                                (AnalysisUseCase.OPEN_POSITIONS, trial["exit_rules"])):
            rid = dr.seed_ruleset_from_rules(rules, use_case, name=f"seed-{key}")
            with SQLModelSession(cdb.get_engine()) as s:
                rs = s.get(Ruleset, rid)
                seeded[use_case.value] = [(ea.triggers, ea.actions) for ea in rs.event_actions]

    for ruleset in live_export["rulesets"]:
        deployed = [(r["triggers"], r["actions"]) for r in ruleset["rules"]]
        assert deployed == seeded[ruleset["subtype"]], (
            f"{key}/{ruleset['subtype']}: the seeded backtest EventActions and the deployed "
            f"ruleset's rules are not action-for-action identical")


# ==================================================================================================
# the drift guard on the substitution above
# ==================================================================================================
def test_the_tools_still_route_through_the_functions_this_test_pins():
    """If either tool stops calling these, the round trip above is testing a path nobody deploys
    through. Source-level because the tools' ``main()`` cannot be executed in-process (see the
    module docstring)."""
    exp = open(os.path.join(_TOOLS, "export_deploy_payload.py"), encoding="utf-8").read()
    imp = open(os.path.join(_TOOLS, "import_deploy_payload.py"), encoding="utf-8").read()
    assert "_derive_export_payload(bt, \"ruleset\", db)" in exp
    assert "_derive_export_payload(bt, \"expert_settings\", db)" in exp
    assert "trade_rules_to_live_export(entry_rules, exit_rules, name=label)" in imp
    assert "save_settings" in imp, "the import tool must write settings through save_settings"


def test_derive_export_payload_is_the_function_the_export_tool_imports():
    """Name-level pin: the tool imports it from app.api.backtests, which is where this test gets
    it from too."""
    from app.api import backtests as api

    assert inspect.isfunction(api._derive_export_payload)
    exp = open(os.path.join(_TOOLS, "export_deploy_payload.py"), encoding="utf-8").read()
    assert "from app.api.backtests import _derive_export_payload" in exp
