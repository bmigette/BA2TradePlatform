"""``ba2-test optimize [--no-]fr-top-n-below-pool`` (operator decision 2026-09-29, "ranking
inert" trap): DEFAULT ON as of the same day (a later decision than the flag's own
introduction) -- persisted on ``optimization_config.backtest.fr_top_n_below_pool`` -- never an
env var -- ONLY for a bypass expert's (FactorRanker) job, never for a classic expert's, whatever
the flag says.

Drives the REAL CLI, not a hand-built namespace (same approach as test_equity_cap_launcher.py):

  * ``L.main(argv)`` parses through the genuine ``argparse`` tree, with ``_cmd_optimize`` swapped
    for a capture so nothing runs;
  * the captured namespace is then fed to the REAL ``_cmd_optimize``, with only the GA/top-N
    persist stubbed out, and the ``StrategyOptimization`` row it persists is read back.

``main()`` calls ``_enter_backend()``, which ``chdir``s into ``backend/`` and re-points the shared
``ba2_common`` DB at ``DATABASE_URL``. Both are process-global, so both are saved and restored
around every call.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L  # noqa: E402


_FMPRATING_ARGV = [  # classic (non-bypass) expert -- must NEVER get the key
    "optimize",
    "--expert", "FMPRating",
    "--universe", "AAPL",
    "--start", "2024-01-02",
    "--end", "2024-02-01",
    "--population", "2",
    "--generations", "1",
]

_FACTORRANKER_ARGV = [  # bypass expert -- the only one this flag can ever affect
    "optimize",
    "--expert", "FactorRanker",
    "--universe", "AAPL",
    "--start", "2024-01-02",
    "--end", "2024-02-01",
    "--population", "2",
    "--generations", "1",
]


def _isolated_globals():
    import ba2_common.core.db as bdb

    return os.getcwd(), bdb._db_file


def _restore_globals(saved):
    import ba2_common.core.db as bdb

    cwd, db_file = saved
    os.chdir(cwd)
    bdb._db_file = db_file


def _parse(argv):
    """Parse ``argv`` through the launcher's real CLI and return the resulting namespace."""
    captured = {}
    original = L._cmd_optimize
    L._cmd_optimize = lambda args: (captured.__setitem__("args", args), 0)[1]
    saved = _isolated_globals()
    try:
        assert L.main(list(argv)) == 0
    finally:
        _restore_globals(saved)
        L._cmd_optimize = original
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


# --------------------------------------------------------------------------- #
# CLI wiring: DEFAULT ON, --no-fr-top-n-below-pool opts out
# --------------------------------------------------------------------------- #
def test_the_flag_is_on_optimize_help(capsys):
    with pytest.raises(SystemExit) as exc:
        L.main(["optimize", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--fr-top-n-below-pool" in out
    assert "--no-fr-top-n-below-pool" in out


def test_the_flag_defaults_to_true():
    """DEFAULT ON (operator decision 2026-09-29): omitting the flag leaves it True."""
    assert _parse(_FACTORRANKER_ARGV).fr_top_n_below_pool is True


def test_explicit_affirmation_flag_is_still_accepted():
    assert _parse(_FACTORRANKER_ARGV + ["--fr-top-n-below-pool"]).fr_top_n_below_pool is True


def test_the_opt_out_flag_sets_it_false():
    assert _parse(_FACTORRANKER_ARGV + ["--no-fr-top-n-below-pool"]).fr_top_n_below_pool is False


# --------------------------------------------------------------------------- #
# It reaches the persisted run config -- ONLY for a bypass expert (FactorRanker)
# --------------------------------------------------------------------------- #
def test_default_run_writes_the_key_true_for_a_bypass_expert(monkeypatch):
    cfg = _run_optimize(_parse(_FACTORRANKER_ARGV), monkeypatch)
    assert cfg["backtest"]["fr_top_n_below_pool"] is True


def test_opt_out_leaves_the_key_absent_for_a_bypass_expert(monkeypatch):
    """Absent, not False -- _build_daily_trial_config/derive_export_payload read a missing key
    as off, matching every FactorRanker run scored before this flag existed."""
    cfg = _run_optimize(_parse(_FACTORRANKER_ARGV + ["--no-fr-top-n-below-pool"]), monkeypatch)
    assert "fr_top_n_below_pool" not in cfg["backtest"]


def test_a_classic_expert_never_gets_the_key_even_by_default(monkeypatch):
    """'Non-FactorRanker jobs never get the key' (operator decision) -- the default-on CLI
    value is True for every job, but the write is gated on the expert being a bypass expert."""
    cfg = _run_optimize(_parse(_FMPRATING_ARGV), monkeypatch)
    assert "fr_top_n_below_pool" not in cfg["backtest"]


def test_a_classic_expert_never_gets_the_key_even_if_explicitly_passed(monkeypatch):
    """The flag is harmless-but-inert on a non-bypass job: still never written."""
    cfg = _run_optimize(_parse(_FMPRATING_ARGV + ["--fr-top-n-below-pool"]), monkeypatch)
    assert "fr_top_n_below_pool" not in cfg["backtest"]


# --------------------------------------------------------------------------- #
# It is NOT a gene
# --------------------------------------------------------------------------- #
def test_the_flag_is_not_a_gene(monkeypatch):
    cfg = _run_optimize(_parse(_FACTORRANKER_ARGV), monkeypatch)
    genes = cfg["expert_params"]
    assert not any("fr_top_n_below_pool" in k for k in genes), \
        [k for k in genes if "fr_top_n_below_pool" in k]
