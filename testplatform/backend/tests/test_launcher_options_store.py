"""``ba2-test optimize --options-store``: the run STATES which option store it reads.

WHY A FLAG AT ALL. ``resolve_options_store`` reads ``config["options_store"]`` and falls back to
``$BACKTEST_OPTIONS_STORE``. For a launcher-driven grid the config key was never set, so the env
var was the ONLY route — and it is exactly the route that does not reach a remote worker: a trial
ships ``{config, fitness_metric, cache_root, inmem_trades}`` (``worker_client.run_trial``) and no
environment goes with it. The worker re-resolved, found nothing, and fell back to the sqlite
default. Silently, because that fallback is a VALID store: the job ran, produced numbers, and
read Alpaca history for a run the master reported as parquet.

So the property under test is not "argparse accepts a flag" but "the persisted
``optimization_config`` carries the DECISION" — resolvable by a process that shares no
environment with the launcher.

Same harness as ``test_equity_cap_launcher``: parse through the REAL CLI, then run the REAL
``_cmd_optimize`` / ``_cmd_optimize_batch`` with only the GA stubbed, and read back the
``StrategyOptimization`` row they persist.

Run:
    ./venv/bin/python -m pytest tests/test_launcher_options_store.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L  # noqa: E402

from app.services.backtest.options_store import (  # noqa: E402
    OPTIONS_STORES,
    PARQUET,
    SQLITE,
    resolve_options_store,
)


_BASE_ARGV = [
    "optimize",
    "--expert", "FMPRating",
    "--universe", "AAPL",
    "--start", "2024-03-01",
    "--end", "2024-04-01",
    "--population", "2",
    "--generations", "1",
]

_BATCH_ARGV = [
    "optimize-batch",
    "--experts", "FMPRating",
    "--strategies", "S2",
    "--universe", "AAPL",
    "--start", "2024-03-01",
    "--end", "2024-04-01",
    "--population", "2",
    "--generations", "1",
]


class _StopBeforeQueue(RuntimeError):
    """Ends ``_cmd_optimize_batch`` right after it persists the row we came to inspect and
    before its indefinite poll loop."""


def _isolated_globals():
    """(cwd, ba2_common db path) — the two process-globals ``_enter_backend`` mutates."""
    import ba2_common.core.db as bdb

    return os.getcwd(), bdb._db_file


def _restore_globals(saved):
    import ba2_common.core.db as bdb

    cwd, db_file = saved
    os.chdir(cwd)
    bdb._db_file = db_file


def _parse(argv, cmd_attr="_cmd_optimize"):
    captured = {}
    original = getattr(L, cmd_attr)
    setattr(L, cmd_attr, lambda args: (captured.__setitem__("args", args), 0)[1])
    saved = _isolated_globals()
    try:
        assert L.main(list(argv)) == 0
    finally:
        _restore_globals(saved)
        setattr(L, cmd_attr, original)
    return captured["args"]


def _latest_config():
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization

    db = SessionLocal()
    try:
        row = (db.query(StrategyOptimization)
                 .order_by(StrategyOptimization.id.desc()).first())
        assert row is not None, "no StrategyOptimization was persisted"
        return row.optimization_config
    finally:
        db.close()


def _run_optimize(args, monkeypatch):
    """The REAL ``_cmd_optimize`` with the GA + top-N persistence stubbed."""
    import app.services.strategy_optimization_handler as SOH

    monkeypatch.setattr(SOH, "handle_strategy_optimization",
                        lambda task_id, payload: {"status": "completed"})
    monkeypatch.setattr(L, "_persist_top_backtests", lambda *a, **k: 0)

    saved = _isolated_globals()
    try:
        assert L._cmd_optimize(args) == 0
    finally:
        _restore_globals(saved)
    return _latest_config()


def _run_optimize_batch(args, monkeypatch):
    """The REAL ``_cmd_optimize_batch`` up to the moment it enqueues (the row is committed
    first)."""
    import app.services.task_queue as TQ

    class _Queue:
        def queue_task(self, **kwargs):
            raise _StopBeforeQueue()

    monkeypatch.setattr(TQ, "get_task_queue", lambda: _Queue())

    saved = _isolated_globals()
    try:
        with pytest.raises(_StopBeforeQueue):
            L._cmd_optimize_batch(args)
    finally:
        _restore_globals(saved)
    return _latest_config()


def _across_the_wire(config):
    """What a worker actually resolves against: the config as JSON, and nothing else."""
    return json.loads(json.dumps(config, default=str))


# --------------------------------------------------------------------------- #
# The flag
# --------------------------------------------------------------------------- #
def test_the_launcher_accepts_an_options_store():
    assert _parse(_BASE_ARGV + ["--options-store", "parquet"]).options_store == "parquet"
    assert _parse(_BASE_ARGV + ["--options-store", "sqlite"]).options_store == "sqlite"


def test_the_flag_defaults_to_unset_so_the_env_and_then_sqlite_still_decide():
    assert _parse(_BASE_ARGV).options_store is None


def test_the_choices_come_from_the_seam_not_a_second_list():
    """A third store must not be able to exist without the CLI offering it."""
    for store in OPTIONS_STORES:
        assert _parse(_BASE_ARGV + ["--options-store", store]).options_store == store
    with pytest.raises(SystemExit):
        _parse(_BASE_ARGV + ["--options-store", "sqlight"])


def test_the_batch_driver_accepts_the_same_flag():
    ns = _parse(_BATCH_ARGV + ["--options-store", "parquet"], cmd_attr="_cmd_optimize_batch")
    assert ns.options_store == "parquet"
    assert _parse(_BATCH_ARGV, cmd_attr="_cmd_optimize_batch").options_store is None


# --------------------------------------------------------------------------- #
# It reaches the config the GA runs — and is the RESOLVED value
# --------------------------------------------------------------------------- #
def test_the_flag_reaches_the_backtest_block(monkeypatch):
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    cfg = _run_optimize(_parse(_BASE_ARGV + ["--options-store", "parquet"]), monkeypatch)
    assert cfg["backtest"]["options_store"] == PARQUET


def test_the_flag_reaches_the_batch_driver_config(monkeypatch):
    """The batch driver is the one that fans out to workers, so missing it here would defeat
    the point of having the flag at all."""
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    cfg = _run_optimize_batch(
        _parse(_BATCH_ARGV + ["--options-store", "parquet"], cmd_attr="_cmd_optimize_batch"),
        monkeypatch)
    assert cfg["backtest"]["options_store"] == PARQUET


def test_the_env_var_is_RESOLVED_into_the_block_rather_than_left_implicit(monkeypatch):
    """THE regression. Without a flag the store came from the environment — and the environment
    is precisely what does not travel. The block must record the answer, not the absence of
    one."""
    monkeypatch.setenv("BACKTEST_OPTIONS_STORE", "parquet")
    cfg = _run_optimize(_parse(_BASE_ARGV), monkeypatch)
    assert cfg["backtest"]["options_store"] == PARQUET

    on_the_worker = _across_the_wire(cfg["backtest"])
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    assert resolve_options_store(on_the_worker) == PARQUET


def test_the_env_var_is_resolved_into_the_batch_block_too(monkeypatch):
    monkeypatch.setenv("BACKTEST_OPTIONS_STORE", "parquet")
    cfg = _run_optimize_batch(_parse(_BATCH_ARGV, cmd_attr="_cmd_optimize_batch"), monkeypatch)
    assert cfg["backtest"]["options_store"] == PARQUET

    on_the_worker = _across_the_wire(cfg["backtest"])
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    assert resolve_options_store(on_the_worker) == PARQUET


def test_the_flag_beats_the_env_var(monkeypatch):
    """A command line that says sqlite must not be overruled by a stale exported variable."""
    monkeypatch.setenv("BACKTEST_OPTIONS_STORE", "parquet")
    cfg = _run_optimize(_parse(_BASE_ARGV + ["--options-store", "sqlite"]), monkeypatch)
    assert cfg["backtest"]["options_store"] == SQLITE


def test_an_unflagged_run_records_sqlite_which_is_what_it_already_read(monkeypatch):
    """Recording the decision must not CHANGE it: with no flag and no env, every existing job
    resolved to sqlite and still does."""
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    cfg = _run_optimize(_parse(_BASE_ARGV), monkeypatch)
    assert cfg["backtest"]["options_store"] == SQLITE


# --------------------------------------------------------------------------- #
# End of the chain: run-level block -> per-trial config -> what a worker reads
# --------------------------------------------------------------------------- #
def test_the_store_survives_into_the_per_trial_config(monkeypatch):
    """A pure-option STRATEGY (``--strategy O_LC``) runs a CLASSIC expert, so the options-expert
    seam (``_apply_options_seam``) is a no-op for it — while it is exactly the job that needs
    the parquet store, because the sqlite one holds no 2023 at all. Follows the block all the
    way through the GA's own trial builder, which is what is posted to a worker."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    argv = [a for a in _BASE_ARGV] + ["--strategy", "O_LC", "--options-store", "parquet"]
    block = _run_optimize(_parse(argv), monkeypatch)["backtest"]
    assert block["options_store"] == PARQUET

    decoded = {"tp": 8.0, "sl": 3.0, "expert_overrides": {}, "buy_tree": None,
               "sell_tree": None, "exit_rules": []}
    trial = _build_daily_trial_config(_across_the_wire(block), decoded)
    assert trial["options_store"] == PARQUET
    assert resolve_options_store(_across_the_wire(trial)) == PARQUET
