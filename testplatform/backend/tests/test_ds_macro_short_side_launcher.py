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
