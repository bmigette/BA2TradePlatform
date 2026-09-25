"""DeterministicScorer ``macro_short_side``: "mirror" reaches OPTION grid trials, and only them.

Plan 2026-09-24 Task 10. The expert default ("same") reproduces every existing result; the
stage-1 option relaunch runs "mirror" as a FIXED setting (not a gene). The launcher's
DeterministicScorer spec is shared with the equity grids, so the setting lives under the spec's
``option_fixed_settings`` and ``_expert_run_settings`` layers it only for an option strategy
kind -- never for S1-S7 or the O_STK equity control arm.

"The launcher wrote it" is not evidence that a trial sees it: ``_build_daily_trial_config``
rebuilds each trial config key by key. So the chain is followed from the REAL CLI through the
persisted optimization row to the per-trial expert settings, and on to the backtest host's
decision-settings resolver that feeds ``_process``.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L  # noqa: E402

_DS = "DeterministicScorer"
_EQUITY_KINDS = ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "O_STK")
_OPTION_KINDS = sorted(L._OPTION_STRATEGY_KEYS - {"O_STK"})


# --------------------------------------------------------------------------- #
# _expert_run_settings: the one plumbing point
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", _OPTION_KINDS)
def test_option_kinds_get_mirror(kind):
    settings = L._expert_run_settings(L._EXPERT_OPT[_DS], ["AAPL"], strategy_kind=kind)
    assert settings["macro_short_side"] == "mirror"
    assert settings["sizing_mode"] == "risk_atr"  # fixed_settings still underneath


@pytest.mark.parametrize("kind", _EQUITY_KINDS + (None,))
def test_equity_kinds_and_no_kind_are_unchanged(kind):
    """THE GOLDEN NO-OP for the equity grids: byte-identical to the pre-Task-10 dict."""
    spec = L._EXPERT_OPT[_DS]
    settings = L._expert_run_settings(spec, ["AAPL"], strategy_kind=kind)
    assert "macro_short_side" not in settings
    assert settings == {**spec["fixed_settings"], **L._INERT_RM_TOGGLES}


def test_overrides_still_win_over_the_option_layer():
    settings = L._expert_run_settings(L._EXPERT_OPT[_DS], ["AAPL"],
                                      {"macro_short_side": "same"}, strategy_kind="O_LP")
    assert settings["macro_short_side"] == "same"


def test_no_other_expert_spec_carries_option_fixed_settings():
    """Every other expert's option jobs must produce the settings dict they always did."""
    for name, spec in L._EXPERT_OPT.items():
        if name == _DS:
            assert spec["option_fixed_settings"] == {"macro_short_side": "mirror"}
            continue
        assert "option_fixed_settings" not in spec, name
        for kind in _OPTION_KINDS:
            assert (L._expert_run_settings(spec, ["AAPL"], strategy_kind=kind)
                    == L._expert_run_settings(spec, ["AAPL"])), (name, kind)


# --------------------------------------------------------------------------- #
# The REAL CLI -> persisted block -> per-trial config -> _process settings
# (harness as in test_launcher_options_store.py)
# --------------------------------------------------------------------------- #
class _StopBeforeQueue(RuntimeError):
    pass


def _saved_globals():
    import ba2_common.core.db as bdb
    return os.getcwd(), bdb._db_file


def _restore(saved):
    import ba2_common.core.db as bdb
    os.chdir(saved[0])
    bdb._db_file = saved[1]


def _parse(argv, cmd_attr):
    captured = {}
    original = getattr(L, cmd_attr)
    setattr(L, cmd_attr, lambda args: (captured.__setitem__("args", args), 0)[1])
    saved = _saved_globals()
    try:
        assert L.main(list(argv)) == 0
    finally:
        _restore(saved)
        setattr(L, cmd_attr, original)
    return captured["args"]


def _latest_config():
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization
    db = SessionLocal()
    try:
        row = db.query(StrategyOptimization).order_by(StrategyOptimization.id.desc()).first()
        assert row is not None
        return row.optimization_config
    finally:
        db.close()


def _run_optimize(strategy, monkeypatch):
    import app.services.strategy_optimization_handler as SOH
    monkeypatch.setattr(SOH, "handle_strategy_optimization",
                        lambda task_id, payload: {"status": "completed"})
    monkeypatch.setattr(L, "_persist_top_backtests", lambda *a, **k: 0)
    args = _parse(["optimize", "--expert", _DS, "--strategy", strategy, "--universe", "AAPL",
                   "--start", "2024-03-01", "--end", "2024-04-01", "--population", "2",
                   "--generations", "1", "--rerun",
                   "--name", f"t10-{strategy}-{os.getpid()}"], "_cmd_optimize")
    saved = _saved_globals()
    try:
        assert L._cmd_optimize(args) == 0
    finally:
        _restore(saved)
    return _latest_config()["backtest"]


def _run_optimize_batch(strategies, monkeypatch):
    import app.services.task_queue as TQ

    class _Queue:
        def queue_task(self, **kwargs):
            raise _StopBeforeQueue()

    monkeypatch.setattr(TQ, "get_task_queue", lambda: _Queue())
    args = _parse(["optimize-batch", "--experts", _DS, "--strategies", strategies,
                   "--universe", "AAPL", "--start", "2024-03-01", "--end", "2024-04-01",
                   "--population", "2", "--generations", "1"], "_cmd_optimize_batch")
    saved = _saved_globals()
    try:
        with pytest.raises(_StopBeforeQueue):
            L._cmd_optimize_batch(args)
    finally:
        _restore(saved)
    return _latest_config()["backtest"]


def _trial_settings(block):
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    decoded = {"tp": 8.0, "sl": 3.0, "expert_overrides": {}, "buy_tree": None,
               "sell_tree": None, "exit_rules": []}
    wire = json.loads(json.dumps(block, default=str))  # what a remote worker receives
    trial = _build_daily_trial_config(wire, decoded, option_trade_records=False)
    return trial["experts"][0]["settings"]


def _process_settings(trial_settings):
    """The settings dict the backtest host hands to DeterministicScorer._process."""
    from app.services.backtest.daily_backtest_handler import _expert_decision_settings
    from ba2_experts.DeterministicScorer import DeterministicScorer
    return _expert_decision_settings(DeterministicScorer, trial_settings)


def test_option_job_carries_mirror_to_the_trial_and_process(monkeypatch):
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    block = _run_optimize("O_LP", monkeypatch)
    assert block["experts"][0]["settings"]["macro_short_side"] == "mirror"
    trial = _trial_settings(block)
    assert trial["macro_short_side"] == "mirror"
    assert _process_settings(trial)["macro_short_side"] == "mirror"


def test_equity_job_stays_on_the_expert_default(monkeypatch):
    block = _run_optimize("S2", monkeypatch)
    assert "macro_short_side" not in block["experts"][0]["settings"]
    assert _process_settings(_trial_settings(block))["macro_short_side"] == "same"


def test_batch_driver_scopes_it_per_job(monkeypatch):
    """optimize-batch persists one row per job; the first job's row is inspected."""
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    block = _run_optimize_batch("O_BEARCS", monkeypatch)
    assert _trial_settings(block)["macro_short_side"] == "mirror"
    block = _run_optimize_batch("S1", monkeypatch)
    assert "macro_short_side" not in _trial_settings(block)


# --------------------------------------------------------------------------- #
# Job identity: discovery name + GA checkpoint fingerprint
# --------------------------------------------------------------------------- #
def _driver():
    import importlib.util
    root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    spec = importlib.util.spec_from_file_location("rom_t10", os.path.join(root, "tools",
                                                                         "run_options_matrix.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _discovery_args(M):
    return M.resolve_args(M.build_parser(), ["--profile", "discovery", "--screener-gate-store",
                                             "test-store", "--max-stock-price", "0"])


def test_the_driver_reads_the_same_rule_the_launcher_applies():
    M = _driver()
    for kind in _OPTION_KINDS:
        assert M.option_fixed_settings("ba2-test", _DS, kind) == {"macro_short_side": "mirror"}
        assert M.option_fixed_settings("ba2-test", "FMPRating", kind) == {}
    for kind in _EQUITY_KINDS:
        assert M.option_fixed_settings("ba2-test", _DS, kind) == {}
    assert M.option_fixed_settings("ba2-test", "NoSuchExpert", "O_LC") == {}


def test_a_mirror_option_job_gets_a_different_discovery_name(monkeypatch):
    """Built with "mirror" it must not share a name -- a SKIP or a checkpoint resume -- with the
    same job built without it."""
    M = _driver()
    args = _discovery_args(M)
    mirror = M.discovery_name(args, "launcher.py", "optm-DS-O_LP-st1", _DS, "O_LP", "AAPL,F")
    mod = M._launcher_module("launcher.py")
    monkeypatch.setitem(mod._EXPERT_OPT[_DS], "option_fixed_settings", {})
    without = M.discovery_name(args, "launcher.py", "optm-DS-O_LP-st1", _DS, "O_LP", "AAPL,F")
    assert mirror != without


def test_names_of_jobs_without_option_fixed_settings_do_not_move(monkeypatch):
    """FMPRating jobs carry nothing, so their names -- and every existing checkpoint key -- must
    not depend on the new fold at all."""
    M = _driver()
    args = _discovery_args(M)
    before = [M.discovery_name(args, "launcher.py", f"optm-FMPRating-{k}", "FMPRating", k, "AAPL")
              for k in M._DISCOVERY_STRATEGIES]
    monkeypatch.setattr(M, "option_fixed_settings", lambda *a: {})
    after = [M.discovery_name(args, "launcher.py", f"optm-FMPRating-{k}", "FMPRating", k, "AAPL")
             for k in M._DISCOVERY_STRATEGIES]
    assert before == after


def test_a_worktree_launcher_path_is_the_one_read(tmp_path):
    """--launcher naming a ba2test_launcher.py is the code the job runs, so it is what the
    identity reads -- not this checkout's copy."""
    M = _driver()
    src = open(M._CHECKOUT_LAUNCHER, encoding="utf-8").read()
    marker = '"option_fixed_settings": {"macro_short_side": "mirror"}'
    assert marker in src
    other = tmp_path / "ba2test_launcher.py"
    other.write_text(src.replace(marker, '"option_fixed_settings": {}'), encoding="utf-8")
    assert M.option_fixed_settings(str(other), _DS, "O_LP") == {}
    assert M.option_fixed_settings("ba2-test", _DS, "O_LP") == {"macro_short_side": "mirror"}


_SPACE = {"model:w_technical": {"min": 0.0, "max": 0.8, "step": 0.1, "type": "float"},
          "model:theta_buy": {"min": 0.15, "max": 0.45, "step": 0.05, "type": "float"}}
_GA = {"populationSize": 200, "generations": 60}
#: checkpoint_fingerprint(_SPACE, _GA) computed with the PRE-change function (git 3609862b).
_PRE_CHANGE_FINGERPRINT = "fc066a60d6cf9511"


def _cfg(settings):
    return {"experts": [{"class": _DS, "settings": settings}]}


def test_fingerprint_unchanged_for_runs_without_a_non_default_value():
    from app.services import strategy_optimization_handler as H
    assert H.checkpoint_fingerprint(_SPACE, _GA) == _PRE_CHANGE_FINGERPRINT
    for settings in ({"sizing_mode": "risk_atr"}, {"macro_short_side": "same"}):
        ident = H.checkpoint_expert_settings_identity(_cfg(settings))
        assert ident == {}
        assert H.checkpoint_fingerprint(_SPACE, _GA, ident) == _PRE_CHANGE_FINGERPRINT


def test_fingerprint_separates_mirror_from_same():
    from app.services import strategy_optimization_handler as H
    ident = H.checkpoint_expert_settings_identity(_cfg({"macro_short_side": "mirror"}))
    assert ident == {"DeterministicScorer.macro_short_side": "mirror"}
    assert H.checkpoint_fingerprint(_SPACE, _GA, ident) != _PRE_CHANGE_FINGERPRINT


def test_every_option_fixed_setting_is_in_the_checkpoint_identity():
    """A new option_fixed_settings key missing from the fingerprint would let a checkpoint
    written without it seed a run with it. Defaults must match the expert's own."""
    from app.services import strategy_optimization_handler as H
    from ba2_experts.DeterministicScorer import DeterministicScorer
    for name, spec in L._EXPERT_OPT.items():
        for key in spec.get("option_fixed_settings") or {}:
            assert key in H.CHECKPOINT_IDENTITY_EXPERT_SETTINGS, (name, key)
    defs = DeterministicScorer.get_settings_definitions()
    for key, default in H.CHECKPOINT_IDENTITY_EXPERT_SETTINGS.items():
        assert defs[key]["default"] == default


def test_the_handler_folds_the_run_settings_into_its_checkpoint_fingerprint():
    """Read from source: the GA loop needs a full optimization to exercise end to end."""
    import inspect
    from app.services import strategy_optimization_handler as H
    src = inspect.getsource(H).replace("\r\n", "\n")
    assert ("checkpoint_fingerprint(\n            param_space, ga, "
            "checkpoint_expert_settings_identity(backtest_cfg))") in src


# --------------------------------------------------------------------------- #
# Deploy: an omitted macro_short_side is written as the default, not left stale
# --------------------------------------------------------------------------- #
def test_explicit_default_settings_helper():
    from ba2_common.core.deploy_parity import explicit_default_settings
    from ba2_experts.DeterministicScorer import DeterministicScorer
    assert explicit_default_settings(DeterministicScorer, {}) == {"macro_short_side": "same"}
    assert explicit_default_settings(DeterministicScorer, {"macro_short_side": "mirror"}) == {}

    class _NoList:
        pass
    assert explicit_default_settings(_NoList, {}) == {}


def _import(tmp_path, db_file, target, expert_params):
    import subprocess
    worktree = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    payload = [{
        "target_instance_id": target, "account_id": 1, "label": "t10-deploy",
        "backtest_id": 1, "expert_name": _DS,
        "ruleset": {"entry_rules": [], "exit_rules": []},
        "settings": {"settings": {"expert_params": expert_params}},
    }]
    pf = tmp_path / f"payload-{target}.json"
    pf.write_text(json.dumps(payload), encoding="utf-8")
    env = dict(os.environ, BA2_LIVE_DB=db_file, BA2_REPO=worktree, PYTHONPATH=os.pathsep.join(
        os.path.join(worktree, "packages", p) for p in ("common", "providers", "experts")))
    proc = subprocess.run([sys.executable, os.path.join(worktree, "tools",
                                                        "import_deploy_payload.py"), str(pf)],
                          env=env, capture_output=True, text=True, timeout=300,
                          cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_an_equity_redeploy_does_not_keep_a_stale_mirror(tmp_path):
    """Run the REAL importer (subprocess, throwaway live-schema DB): a mirror option genome,
    then an equity genome whose payload omits the key, onto the SAME instance."""
    from sqlalchemy import create_engine, text
    from sqlmodel import SQLModel
    import ba2_common.core.models  # noqa: F401 -- registers the tables

    db_file = str(tmp_path / "throwaway_live.sqlite")
    engine = create_engine(f"sqlite:///{db_file}")
    SQLModel.metadata.create_all(engine)

    def stored():
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT value_str FROM expertsetting WHERE key='macro_short_side'")).fetchall()
        return [r[0] for r in rows]

    _import(tmp_path, db_file, None, {"macro_short_side": "mirror", "theta_buy": 0.3})
    assert stored() == ["mirror"]
    with engine.connect() as conn:
        inst_id = conn.execute(text("SELECT id FROM expertinstance")).scalar()
    out = _import(tmp_path, db_file, inst_id, {"theta_buy": 0.3})
    assert stored() == ["same"], out
    assert "macro_short_side='same' (absent from the payload" in out
    engine.dispose()
