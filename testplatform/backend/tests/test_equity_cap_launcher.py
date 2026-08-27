"""``ba2-test optimize --equity-cap``: a RUN-LEVEL parameter, never a gene.

Drives the REAL CLI, not a hand-built namespace:

  * ``L.main(argv)`` parses through the genuine ``argparse`` tree (the launcher has no
    ``build_parser()``; the parser is constructed inline inside ``main``), with
    ``_cmd_optimize`` swapped for a capture so nothing runs;
  * the captured namespace is then fed to the REAL ``_cmd_optimize`` /
    ``_cmd_optimize_batch`` (the two config builders — the launcher has no
    ``_build_optimization_config``), with only the GA itself stubbed out, and the
    ``StrategyOptimization`` row they persist is read back.

That means the cap is asserted where the GA actually reads it, not where a test built it.

``main()`` calls ``_enter_backend()``, which ``chdir``s into ``backend/`` and re-points the
shared ``ba2_common`` DB at ``DATABASE_URL``. Both are process-global, so both are saved and
restored around every call — otherwise this module would silently reconfigure the rest of the
suite.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L  # noqa: E402


_BASE_ARGV = [
    "optimize",
    "--expert", "FMPRating",
    "--universe", "AAPL",
    "--start", "2024-01-02",
    "--end", "2024-02-01",
    "--population", "2",
    "--generations", "1",
]

_BATCH_ARGV = [
    "optimize-batch",
    "--experts", "FMPRating",
    "--strategies", "S2",
    "--universe", "AAPL",
    "--start", "2024-01-02",
    "--end", "2024-02-01",
    "--population", "2",
    "--generations", "1",
]


class _StopBeforeQueue(RuntimeError):
    """Raised from the stubbed task queue to end ``_cmd_optimize_batch`` right after it has
    persisted the optimization row (which is what we came to inspect) and before its
    indefinite poll loop."""


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
    """Parse ``argv`` through the launcher's real CLI and return the resulting namespace."""
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


def _run_optimize(args, monkeypatch):
    """Run the REAL ``_cmd_optimize`` with the GA + top-N persistence stubbed; return the
    ``optimization_config`` it wrote."""
    import app.services.strategy_optimization_handler as SOH
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization

    monkeypatch.setattr(SOH, "handle_strategy_optimization",
                        lambda task_id, payload: {"status": "completed"})
    monkeypatch.setattr(L, "_persist_top_backtests", lambda *a, **k: 0)

    saved = _isolated_globals()
    try:
        assert L._cmd_optimize(args) == 0
    finally:
        _restore_globals(saved)

    db = SessionLocal()
    try:
        row = (db.query(StrategyOptimization)
                 .order_by(StrategyOptimization.id.desc()).first())
        assert row is not None, "_cmd_optimize persisted no StrategyOptimization"
        return row.optimization_config
    finally:
        db.close()


def _run_optimize_batch(args, monkeypatch):
    """Run the REAL ``_cmd_optimize_batch`` up to the moment it enqueues (the row is committed
    first), then stop; return the ``optimization_config`` it wrote."""
    import app.services.task_queue as TQ
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization

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

    db = SessionLocal()
    try:
        row = (db.query(StrategyOptimization)
                 .order_by(StrategyOptimization.id.desc()).first())
        assert row is not None, "_cmd_optimize_batch persisted no StrategyOptimization"
        return row.optimization_config
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The flag
# --------------------------------------------------------------------------- #
def test_the_launcher_accepts_an_equity_cap():
    assert _parse(_BASE_ARGV + ["--equity-cap", "20000"]).equity_cap == 20_000.0


def test_the_cap_defaults_to_off():
    """Off by default. A numeric default would turn the feature on for every existing run."""
    assert _parse(_BASE_ARGV).equity_cap is None


def test_the_batch_driver_accepts_the_same_flag():
    ns = _parse(_BATCH_ARGV + ["--equity-cap", "20000"], cmd_attr="_cmd_optimize_batch")
    assert ns.equity_cap == 20_000.0
    assert _parse(_BATCH_ARGV, cmd_attr="_cmd_optimize_batch").equity_cap is None


# --------------------------------------------------------------------------- #
# It reaches the config the GA runs
# --------------------------------------------------------------------------- #
def test_the_cap_reaches_account_settings(monkeypatch):
    cfg = _run_optimize(_parse(_BASE_ARGV + ["--equity-cap", "20000"]), monkeypatch)
    assert cfg["backtest"]["account_settings"]["equity_cap"] == 20_000.0


def test_no_flag_leaves_the_cap_absent_from_account_settings(monkeypatch):
    cfg = _run_optimize(_parse(_BASE_ARGV), monkeypatch)
    assert cfg["backtest"]["account_settings"]["equity_cap"] is None


def test_the_cap_reaches_the_batch_driver_config(monkeypatch):
    cfg = _run_optimize_batch(
        _parse(_BATCH_ARGV + ["--equity-cap", "20000"], cmd_attr="_cmd_optimize_batch"),
        monkeypatch)
    assert cfg["backtest"]["account_settings"]["equity_cap"] == 20_000.0


# --------------------------------------------------------------------------- #
# It is NOT a gene
# --------------------------------------------------------------------------- #
def test_the_cap_is_NOT_a_gene(monkeypatch):
    """A gene would optimise the CAPITAL rather than the strategy -- the opposite of the point --
    and every individual would then be scored against a different denominator."""
    cfg = _run_optimize(_parse(_BASE_ARGV + ["--equity-cap", "20000"]), monkeypatch)
    genes = cfg["expert_params"]
    assert not any("equity_cap" in k for k in genes), \
        [k for k in genes if "equity_cap" in k]


def test_the_cap_is_not_a_gene_in_the_batch_driver_either(monkeypatch):
    cfg = _run_optimize_batch(
        _parse(_BATCH_ARGV + ["--equity-cap", "20000"], cmd_attr="_cmd_optimize_batch"),
        monkeypatch)
    assert not any("equity_cap" in k for k in cfg["expert_params"])


def test_no_strategy_the_launcher_builds_exposes_an_equity_cap_gene():
    """The other half of "not a gene": the GA also collects genes off the Strategy row, so the
    cap must be absent from THAT space too, for every strategy variant the CLI can build."""
    from app.services.strategy_param_space import collect_param_space

    for kind in sorted(L._STRATEGY_BUILDERS):
        strategy = L._build_strategy(kind, f"gene-space-{kind}", "FMPRating")
        space = collect_param_space(strategy)
        assert not any("equity_cap" in k for k in space), \
            f"{kind}: {[k for k in space if 'equity_cap' in k]}"
