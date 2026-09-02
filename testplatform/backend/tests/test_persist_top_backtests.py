"""Task 6 — top-N backtest sync: _persist_top_backtests must push each persisted TOP-N
Backtest row to synced remote workers via sync_client.push_backtest (not just create it).

``_persist_top_backtests`` (and its nested ``_persist_one``) import ``push_backtest`` and
``_persist_trial_worker`` via FUNCTION-LOCAL ``from ... import ...`` statements (matching the
existing style of that function's other ``app.services.*`` imports), executed at CALL time.
So monkeypatching must target the ORIGIN modules (``app.services.sync_client.push_backtest`` /
``app.services.strategy_optimization_handler._persist_trial_worker``) — patching attributes on
the loaded ``ba2test_launcher`` module itself (``mod.push_backtest``) would be a no-op (that
name is never bound at module scope) and would in fact raise AttributeError since the module
doesn't have that attribute in the first place.
"""
import importlib.util
import os
import sys

# Load the launcher module by path (it lives at testplatform/ba2test_launcher.py), same
# pattern as test_option_strategy_builders.py.
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

import pytest

from app.models.database import Base, SessionLocal, engine
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization
from app.services import strategy_optimization_handler as _soh
from app.services import sync_client as _sync_client
from app.services import worker_client as _worker_client


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    """Ensure the host tables exist on the default engine (same pattern as
    test_strategy_optimization_handler.py's ``_host_db``) — SessionLocal()/engine here is the
    conftest-isolated throwaway sqlite, but its tables are never created unless something calls
    create_all; without this fixture every db.add/commit below fails with
    ``OperationalError: no such table``."""
    Base.metadata.create_all(bind=engine)
    yield


# A full results blob shape (mirrors tests/backtest/test_daily_backtest_handler.py's _RESULTS) —
# _persist_results reads several REQUIRED keys (total_trades, winning_trades, losing_trades,
# win_rate, total_return, sharpe_ratio, max_drawdown, profit_factor, avg_trade_duration,
# final_equity, equity_curve, drawdown_curve, trades) and KeyErrors on a thin dict.
_RESULTS = {
    "total_trades": 3, "winning_trades": 2, "losing_trades": 1, "win_rate": 66.67,
    "total_return": 5.0, "annualized_return": 12.3, "buy_hold_return": 0.0,
    "sharpe_ratio": 1.1, "sortino_ratio": 1.4, "calmar_ratio": 0.9, "volatility": 8.2,
    "max_drawdown": -4.55, "avg_drawdown": -2.1, "max_drawdown_duration": 1.0,
    "profit_factor": 4.0, "expectancy": 2.0, "sqn": 0.7, "avg_trade": 2.0,
    "best_trade": 5.0, "worst_trade": -2.0,
    "avg_trade_duration": 2.0, "exposure_time": 33.3,
    "final_equity": 105_000.0, "equity_peak": 110_000.0,
    "equity_curve": [{"date": "2024-01-02", "equity": 100_000.0},
                     {"date": "2024-01-04", "equity": 105_000.0}],
    "drawdown_curve": [{"date": "2024-01-02", "drawdown": 0.0},
                       {"date": "2024-01-04", "drawdown": -4.55}],
    "trades": [{"symbol": "AAPL", "entry_time": "2024-01-02", "exit_time": None,
                "direction": "buy", "entry_price": 100.0, "exit_price": 0.0,
                "size": 10.0, "pnl": 500.0, "pnl_pct": 5.0, "bars_held": 2,
                "exit_reason": "unknown"}],
}


def _seed_opt_for_top_n(entry_rules=None, expert_settings=None):
    db = SessionLocal()
    try:
        strat = Strategy(name="top-n-strat", entry_rules=entry_rules or [], exit_rules=[])
        db.add(strat); db.commit(); db.refresh(strat)
        opt = StrategyOptimization(
            strategy_id=strat.id, name="top-n-opt",
            fitness_metric="sharpe", optimization_type="genetic",
            optimization_config={
                "backtest": {
                    # _build_daily_trial_config requires all of these (see
                    # tests/test_options_optimization_wiring.py's _backtest_cfg for the same
                    # minimal-required-fields template) — start/end/initial_capital ALONE
                    # (the plan draft's original guess) is not enough; it KeyErrors on
                    # backtest_id/experts/enabled_instruments/account_settings/warmup_days/seed.
                    "backtest_id": 1,
                    "start_date": "2024-01-01", "end_date": "2024-06-01",
                    "enabled_instruments": ["AAPL"],
                    "experts": [{"class": "FMPRating", "settings": expert_settings or {}}],
                    "initial_capital": 10000.0,
                    "account_settings": {"starting_cash": 10000.0},
                    "warmup_days": 30,
                    "seed": 42,
                }
            },
            all_results=[{"params": {}, "fitness": 1.23, "trades": 10}],
            best_params={}, best_fitness=1.23, status="completed",
        )
        db.add(opt); db.commit(); db.refresh(opt)
        return opt.id
    finally:
        db.close()


def test_persist_one_pushes_backtest_after_save(monkeypatch):
    opt_id = _seed_opt_for_top_n()
    calls = []
    monkeypatch.setattr(_sync_client, "push_backtest", lambda bt, db: calls.append(bt.name))
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: {"ok": True, "results": dict(_RESULTS)},
    )

    persisted = mod._persist_top_backtests(opt_id, "FMPRating", n=1, parallel=1)

    assert persisted == 1
    assert len(calls) == 1
    assert calls[0] == "TOP1-top-n-opt"


def test_persist_one_mirrors_entry_rules_into_strategy_params(monkeypatch):
    """Regression (unified rule model): TOP-N persist mirrors the CONCRETE decoded TradeRule
    lists into strategy_params (entryRules/exitRules) for Load/export — historically the
    entry-side bracket was forgotten, leaving the persisted display copy empty even though
    the run itself used it."""
    entry_rules = [{
        "id": "bracket", "conditions": None, "continue_processing": False,
        "actions": [
            {"action_type": "buy"},
            {"id": "s4_tp", "action_type": "adjust_take_profit",
             "reference_value": "expert_target_price", "action_value": -6.0},
        ],
    }]
    opt_id = _seed_opt_for_top_n(entry_rules=entry_rules)
    monkeypatch.setattr(_sync_client, "push_backtest", lambda bt, db: None)
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: {"ok": True, "results": dict(_RESULTS)},
    )

    persisted = mod._persist_top_backtests(opt_id, "FMPRating", n=1, parallel=1)
    assert persisted == 1

    db = SessionLocal()
    try:
        from app.models.backtest import Backtest
        bt = db.query(Backtest).filter_by(optimization_id=opt_id).one()
        mirrored = bt.strategy_params.get("entryRules")
        assert mirrored and len(mirrored) == 1
        kinds = [a["action_type"] for a in mirrored[0]["actions"]]
        assert kinds == ["buy", "adjust_take_profit"]
        # This fixture's Strategy has NO exit_rules template (exit_rules=[] at seed time, i.e.
        # unset) -> decode_params correctly yields None (not []) for "no unified-model template
        # here", so the mirror key is never written. See daily_backtest_handler._seed_enter's
        # None-vs-[] fix: only a PRUNED-to-zero template (a real template that existed) should
        # ever surface as an explicit [].
        assert bt.strategy_params.get("exitRules") is None
    finally:
        db.close()


def test_persist_one_mirrors_fixed_settings_into_strategy_params(monkeypatch):
    """Regression: the expert's FIXED (non-GA-tunable) settings from the optimization's
    bt_block (e.g. sizing_mode=risk_atr) must be mirrored onto strategy_params as
    expertFixedSettings, so a later export doesn't depend on the StrategyOptimization row
    surviving (it can be pruned by db-cleanup while the "starred" Backtest stays) -- without
    this, sizing_mode silently vanishes from a live deploy and the position ends up with no
    safeguard stop-loss."""
    opt_id = _seed_opt_for_top_n(expert_settings={"sizing_mode": "risk_atr"})
    monkeypatch.setattr(_sync_client, "push_backtest", lambda bt, db: None)
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: {"ok": True, "results": dict(_RESULTS)},
    )

    persisted = mod._persist_top_backtests(opt_id, "FMPRating", n=1, parallel=1)
    assert persisted == 1

    db = SessionLocal()
    try:
        from app.models.backtest import Backtest
        bt = db.query(Backtest).filter_by(optimization_id=opt_id).one()
        assert bt.strategy_params.get("expertFixedSettings") == {"sizing_mode": "risk_atr"}
    finally:
        db.close()


def test_persist_one_omits_fixed_settings_key_when_none_configured(monkeypatch):
    """No fixed_settings on the expert spec (the common case for non-RM experts) -> no
    expertFixedSettings key at all, not an empty dict -- keeps strategy_params clean."""
    opt_id = _seed_opt_for_top_n(expert_settings={})
    monkeypatch.setattr(_sync_client, "push_backtest", lambda bt, db: None)
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: {"ok": True, "results": dict(_RESULTS)},
    )

    persisted = mod._persist_top_backtests(opt_id, "FMPRating", n=1, parallel=1)
    assert persisted == 1

    db = SessionLocal()
    try:
        from app.models.backtest import Backtest
        bt = db.query(Backtest).filter_by(optimization_id=opt_id).one()
        assert "expertFixedSettings" not in bt.strategy_params
    finally:
        db.close()


def test_persist_top_uses_buffered_full_results_without_rerun(monkeypatch):
    """When every top-N individual's full results are already in the buffer (the common case —
    they came from the GA's own last generation), _persist_top_backtests must NOT invoke
    _persist_trial_worker (the re-run path) at all."""
    opt_id = _seed_opt_for_top_n()
    # Overwrite all_results with an entry carrying a real trial `key` so the buffer lookup works.
    db = SessionLocal()
    try:
        opt = db.query(StrategyOptimization).filter_by(id=opt_id).first()
        opt.all_results = [{"params": {}, "fitness": 1.23, "trades": 10, "key": "trial-key-1"}]
        db.commit()
    finally:
        db.close()

    calls = []
    monkeypatch.setattr(_sync_client, "push_backtest", lambda bt, db: calls.append(bt.name))
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: (_ for _ in ()).throw(AssertionError("must not re-run — buffer had this key")),
    )

    persisted = mod._persist_top_backtests(
        opt_id, "FMPRating", n=1, parallel=1,
        last_gen_full_results={"trial-key-1": dict(_RESULTS)},
    )

    assert persisted == 1
    assert calls == ["TOP1-top-n-opt"]


def test_persist_top_reruns_only_the_missing_members(monkeypatch, capsys):
    """Two top-N individuals: one's key is in the buffer (persist directly), the other's is not
    (e.g. an elite reused via the GA memo, never freshly evaluated in the last generation) — only
    the missing one goes through the re-run path."""
    opt_id = _seed_opt_for_top_n()
    db = SessionLocal()
    try:
        opt = db.query(StrategyOptimization).filter_by(id=opt_id).first()
        # Params must be valid namespaced genes (decode_params rejects unknown-namespace keys
        # like a bare "a") -- "model:*" is stripped of its prefix into expert_overrides and
        # accepts any param name, so it's the simplest way to get two distinct-but-valid genomes.
        opt.all_results = [
            {"params": {"model:x": 1}, "fitness": 2.0, "trades": 10, "key": "buffered-key"},
            {"params": {"model:x": 2}, "fitness": 1.0, "trades": 5, "key": "missing-key"},
        ]
        db.commit()
    finally:
        db.close()

    rerun_calls = []
    monkeypatch.setattr(_sync_client, "push_backtest", lambda bt, db: None)
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: rerun_calls.append(cfg) or {"ok": True, "results": dict(_RESULTS)},
    )

    persisted = mod._persist_top_backtests(
        opt_id, "FMPRating", n=2, parallel=1,
        last_gen_full_results={"buffered-key": dict(_RESULTS)},
    )

    assert persisted == 2
    assert len(rerun_calls) == 1  # only the "missing-key" individual was re-run

    # Regression: the progress print for the re-run (specs) dispatch loop must report the total
    # across BOTH groups (len(ranked) == 2 -- 1 buffered + 1 re-run), not just len(specs) (== 1,
    # the re-run-only count). Before the fix this printed the nonsensical "(2/1)" -- persisted
    # exceeding its own printed "total".
    out = capsys.readouterr().out
    assert "persisted TOP1 (1/2) [no re-run]" in out
    assert "persisted TOP2 (2/2)" in out
    assert "(2/1)" not in out


def test_persist_top_no_buffer_falls_back_to_full_rerun(monkeypatch):
    """Backward compatibility: calling without last_gen_full_results (or with None) behaves
    exactly like today — every top-N individual is re-run."""
    opt_id = _seed_opt_for_top_n()
    calls = []
    monkeypatch.setattr(_sync_client, "push_backtest", lambda bt, db: calls.append(bt.name))
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: {"ok": True, "results": dict(_RESULTS)},
    )

    persisted = mod._persist_top_backtests(opt_id, "FMPRating", n=1, parallel=1)

    assert persisted == 1
    assert calls == ["TOP1-top-n-opt"]


# --- remote-dispatch retry + local fallback (2026-08-27) --------------------------------------
# Before this, a top-N re-run round-robined onto a remote worker got exactly one shot: any
# failure (a leaked pool slot, a worker mid self-update returning 503, a dropped connection)
# permanently dropped that rank -- goal2020 opt 361, 362, and 364 all lost top-N members this
# way and had to be recovered by hand. _remote_then_local gives a transient remote failure one
# retry, then falls back to running the trial directly rather than losing the rank.

_FAKE_WORKER = {"id": 1, "name": "remote1", "url": "http://remote1:8100", "password": "x"}


@pytest.fixture(autouse=True)
def _no_real_retry_backoff(monkeypatch):
    """Every test here drives its own fake failures; a real sleep between retries would just
    slow the suite down."""
    monkeypatch.setattr(mod, "_REMOTE_RETRY_BACKOFF_S", 0.0)


def test_remote_then_local_succeeds_on_first_try_no_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(_worker_client, "run_trial_full",
                        lambda w, tc, fm: calls.append("remote") or {"ok": True, "results": {}})
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: (_ for _ in ()).throw(AssertionError("must not fall back on first success")),
    )

    out = mod._remote_then_local(_FAKE_WORKER, {"name": "TOP1"}, "sharpe")

    assert out == {"ok": True, "results": {}}
    assert calls == ["remote"]


def test_remote_then_local_retries_once_then_succeeds(monkeypatch):
    attempts = []

    def flaky(w, tc, fm):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("503 Service Unavailable (worker mid self-update)")
        return {"ok": True, "results": {"from": "retry"}}

    monkeypatch.setattr(_worker_client, "run_trial_full", flaky)
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: (_ for _ in ()).throw(AssertionError("must not fall back — retry succeeded")),
    )

    out = mod._remote_then_local(_FAKE_WORKER, {"name": "TOP1"}, "sharpe")

    assert out == {"ok": True, "results": {"from": "retry"}}
    assert len(attempts) == 2


def test_remote_then_local_falls_back_after_two_remote_failures(monkeypatch):
    attempts = []

    def always_fails(w, tc, fm):
        attempts.append(1)
        raise RuntimeError("worker unreachable")

    monkeypatch.setattr(_worker_client, "run_trial_full", always_fails)
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: {"ok": True, "results": {"from": "local-fallback"}},
    )

    out = mod._remote_then_local(_FAKE_WORKER, {"name": "TOP1"}, "sharpe")

    assert out == {"ok": True, "results": {"from": "local-fallback"}}
    assert len(attempts) == 2, "must try remote exactly twice before falling back"


# --- local re-run retry (2026-08-28) -----------------------------------------------------------
# _persist_trial_worker used to give up on the FIRST failure of run_daily_backtest, whether it
# was on a local slot or the remote-fallback's local attempt. goal2020 opt 372 and opt 377 both
# lost a top-N rank this way to a transient sqlite3.OperationalError('disk I/O error') writing
# the trial's own persist_trading_db file -- a one-off disk hiccup, not a real defect in the
# genome, that a single retry would very likely have ridden out. Mirrors _remote_then_local's
# retry-before-giving-up shape, one layer in from the remote path.

_DAILY_BACKTEST_HANDLER = "app.services.backtest.daily_backtest_handler"


@pytest.fixture(autouse=True)
def _no_real_local_retry_backoff(monkeypatch):
    """Every test here drives its own fake failures; a real sleep would just slow the suite."""
    monkeypatch.setattr(_soh, "_LOCAL_RETRY_BACKOFF_S", 0.0)


def test_persist_trial_worker_succeeds_first_try_no_retry(monkeypatch):
    calls = []

    def run_ok(cfg):
        calls.append(1)
        return {"equity_curve": []}

    monkeypatch.setattr(f"{_DAILY_BACKTEST_HANDLER}.run_daily_backtest", run_ok)

    out = _soh._persist_trial_worker({"name": "TOP1"})

    assert out == {"ok": True, "results": {"equity_curve": []}}
    assert len(calls) == 1


def test_persist_trial_worker_retries_once_then_succeeds(monkeypatch):
    attempts = []

    def flaky(cfg):
        attempts.append(1)
        if len(attempts) == 1:
            import sqlite3
            raise sqlite3.OperationalError("disk I/O error")
        return {"equity_curve": [1]}

    monkeypatch.setattr(f"{_DAILY_BACKTEST_HANDLER}.run_daily_backtest", flaky)

    out = _soh._persist_trial_worker({"name": "TOP1"})

    assert out == {"ok": True, "results": {"equity_curve": [1]}}
    assert len(attempts) == 2


def test_persist_trial_worker_returns_failure_after_six_local_failures(monkeypatch):
    attempts = []

    def always_fails(cfg):
        attempts.append(1)
        import sqlite3
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(f"{_DAILY_BACKTEST_HANDLER}.run_daily_backtest", always_fails)

    out = _soh._persist_trial_worker({"name": "TOP1"})

    assert out["ok"] is False
    assert "disk I/O error" in out["error"]
    assert len(attempts) == 6, "must try exactly 6 times before giving up (existing contract: ok=False, never raises)"


def test_persist_trial_worker_retry_backoff_doubles_each_attempt(monkeypatch):
    """opt 379/380 (2026-08-28) re-hit the SAME disk I/O error on the single 5s retry the prior
    fix gave it -- sustained contention, not a one-off blip. opt 424 (2026-09-02, two concurrent
    grids sharing the box) then re-hit it a THIRD time on the 4-attempt/35s-total schedule that
    fix landed with, so the budget was widened again: 6 attempts, doubling out to 5s..160s
    (155s total) gives sustained contention from a second concurrent grid real room to clear."""
    attempts = []
    sleeps = []

    def always_fails(cfg):
        attempts.append(1)
        import sqlite3
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(f"{_DAILY_BACKTEST_HANDLER}.run_daily_backtest", always_fails)
    monkeypatch.setattr(_soh, "_LOCAL_RETRY_BACKOFF_S", 5.0)  # override the autouse 0.0 for this test
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    out = _soh._persist_trial_worker({"name": "TOP1"})

    assert out["ok"] is False
    assert len(attempts) == 6
    assert sleeps == [5.0, 10.0, 20.0, 40.0, 80.0]
