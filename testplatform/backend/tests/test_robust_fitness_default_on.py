"""Robustness-adjusted fitness is ON BY DEFAULT (2026-09-17), and a checkpoint may not change it.

WHY. ``--robust-fitness`` existed for a year as an opt-in and NO grid driver ever passed it --
neither ``tools/stage1_run.sh`` nor ``tools/run_options_matrix.py`` -- so the gated option stage-1
discovery run ranked its entire search on the RAW metric: its elite reached 43-58%/yr at 61-94%
drawdown with nothing in the run asking whether that was an edge or two trades carrying the book.
That is the same concentration the 2026-08-16 audit found in 81 of 84 goal2020 results. The fix is
structural, in two halves, and this module pins both:

  1. the default flips, for ``optimize`` AND ``optimize-batch``, with ``--no-robust-fitness`` as
     the explicit way out, and the parsed value reaches the run config the GA actually reads;
  2. a GA checkpoint RECORDS the setting it was scored under and REFUSES to resume under a
     different one. Checkpoints are keyed on the job NAME, so without this a job whose population
     was ranked raw would quietly continue under the robust objective -- one population carrying
     two incomparable scales, with nothing in the row, the log or the results saying so. The
     gene-space ``fingerprint`` cannot catch it: the genes are identical, only their scores mean
     something different.

The CLI half drives the REAL argparse tree and the REAL config builders (same posture as
``test_equity_cap_launcher.py``, whose helpers this mirrors) -- not a hand-built namespace.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L  # noqa: E402

from app.services import strategy_optimization_handler as H  # noqa: E402
from app.services.strategy_fitness import compute_fitness  # noqa: E402

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

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
    """Ends ``_cmd_optimize_batch`` right after it has persisted the row we came to inspect."""


def _isolated_globals():
    """(cwd, ba2_common db path) -- the two process-globals ``_enter_backend`` mutates."""
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
# 1. The default, on both subcommands
# --------------------------------------------------------------------------- #
def test_optimize_defaults_to_robust_fitness_on():
    assert _parse(_BASE_ARGV).robust_fitness is True


def test_optimize_batch_defaults_to_robust_fitness_on():
    """Both subcommands or neither: a driver that dispatches through the batch command must not
    rank on a different objective from the same driver's per-job `optimize` calls."""
    assert _parse(_BATCH_ARGV, cmd_attr="_cmd_optimize_batch").robust_fitness is True


def test_the_explicit_flag_is_still_accepted_and_means_the_same():
    """Every banked command line that passes --robust-fitness must keep working unchanged."""
    assert _parse(_BASE_ARGV + ["--robust-fitness"]).robust_fitness is True
    assert _parse(_BATCH_ARGV + ["--robust-fitness"],
                  cmd_attr="_cmd_optimize_batch").robust_fitness is True


def test_no_robust_fitness_turns_it_off_on_both_subcommands():
    assert _parse(_BASE_ARGV + ["--no-robust-fitness"]).robust_fitness is False
    assert _parse(_BATCH_ARGV + ["--no-robust-fitness"],
                  cmd_attr="_cmd_optimize_batch").robust_fitness is False


def test_the_last_flag_wins_so_a_wrapper_can_override_a_default_argument_list():
    assert _parse(_BASE_ARGV + ["--no-robust-fitness", "--robust-fitness"]).robust_fitness is True
    assert _parse(_BASE_ARGV + ["--robust-fitness", "--no-robust-fitness"]).robust_fitness is False


# --------------------------------------------------------------------------- #
# 2. It reaches the run config the GA actually reads
# --------------------------------------------------------------------------- #
def test_the_default_reaches_the_optimize_run_config(monkeypatch):
    cfg = _run_optimize(_parse(_BASE_ARGV), monkeypatch)
    assert cfg["backtest"]["robust_fitness"] is True


def test_opting_out_reaches_the_optimize_run_config(monkeypatch):
    cfg = _run_optimize(_parse(_BASE_ARGV + ["--no-robust-fitness"]), monkeypatch)
    assert cfg["backtest"]["robust_fitness"] is False


def test_the_default_reaches_the_batch_run_config(monkeypatch):
    cfg = _run_optimize_batch(_parse(_BATCH_ARGV, cmd_attr="_cmd_optimize_batch"), monkeypatch)
    assert cfg["backtest"]["robust_fitness"] is True


def test_opting_out_reaches_the_batch_run_config(monkeypatch):
    cfg = _run_optimize_batch(
        _parse(_BATCH_ARGV + ["--no-robust-fitness"], cmd_attr="_cmd_optimize_batch"), monkeypatch)
    assert cfg["backtest"]["robust_fitness"] is False


def test_the_trial_config_whitelist_still_carries_the_key():
    """_build_daily_trial_config rebuilds the trial config KEY BY KEY: a knob absent from that
    whitelist is inert however correctly it was parsed, stored and echoed upstream."""
    import inspect

    src = inspect.getsource(H._build_daily_trial_config)
    assert '"robust_fitness": backtest_cfg.get("robust_fitness")' in src


# --------------------------------------------------------------------------- #
# 3. Both numbers are always stored (the existing contract, pinned here too)
# --------------------------------------------------------------------------- #
def _results(pnls, years=4, initial=100_000.0):
    trades, curve = [], []
    equity = initial
    per_year = max(1, len(pnls) // years)
    for i, p in enumerate(pnls):
        year = 2020 + min(i // per_year, years - 1)
        month = (i % per_year) * 12 // per_year + 1
        ts = f"{year}-{month:02d}-15T15:00:00"
        trades.append({"pnl": float(p), "pnl_pct": 100.0 * float(p) / equity,
                       "entry_price": 100.0, "size": 200.0, "exit_time": ts,
                       "direction": "long"})
        equity += float(p)
        curve.append({"date": ts, "equity": equity})
    annualized = ((max(equity, 1.0) / initial) ** (1.0 / years) - 1.0) * 100.0
    return {
        "total_trades": len(pnls),
        "winning_trades": sum(1 for p in pnls if p > 0),
        "losing_trades": sum(1 for p in pnls if p <= 0),
        "win_rate": 100.0 * sum(1 for p in pnls if p > 0) / max(1, len(pnls)),
        "annualized_return": annualized, "max_drawdown": -10.0,
        "total_return": (equity / initial - 1.0) * 100.0, "sharpe_ratio": 1.5,
        "calmar_ratio": 2.5, "avg_trades_per_year": len(pnls) / years,
        "initial_capital": initial, "trades": trades, "equity_curve": curve,
        "stress_spread_bps": 0.0,
    }


def test_results_carry_both_views_and_the_components_when_on():
    r = _results([40_000.0] + [200.0] * 60)     # one winner dominating the book
    r["robust_fitness"] = True
    adj = compute_fitness("consistent_annual_return", r)
    assert r["fitness_robust"] == adj
    assert r["fitness_raw"] > adj, "the raw view must survive alongside the adjusted one"
    for k in ("top1_pct", "top5_pct", "mc_p5", "mc_prob_neg",
              "conc_factor", "mc_factor", "spread_factor"):
        assert k in r["robustness"], f"{k} missing -- the score would not be decomposable"


def test_results_carry_a_None_robust_view_when_off():
    """The existing contract: a raw-ranked row still records fitness_raw, and fitness_robust is
    explicitly None rather than absent, so a row can always say which objective produced it."""
    r = _results([500.0] * 60)
    r["robust_fitness"] = False
    compute_fitness("consistent_annual_return", r)
    assert r["fitness_raw"] is not None
    assert r["fitness_robust"] is None


# --------------------------------------------------------------------------- #
# 4. THE CENTREPIECE: a checkpoint may not change objective mid-search
# --------------------------------------------------------------------------- #
def _guard(ckpt, robust_on, job="opt-FMPRating-S2-st1"):
    return H._assert_checkpoint_robustness_matches(ckpt, robust_on, job, f"opt:{job}")


def test_a_raw_checkpoint_refuses_to_resume_under_the_new_default():
    with pytest.raises(ValueError) as e:
        _guard({"generation": 4, "robust_fitness": False}, True)
    msg = str(e.value)
    assert "robust_fitness=False" in msg and "robust_fitness=True" in msg, msg
    assert "opt-FMPRating-S2-st1" in msg, "the message must name the job"
    assert "--no-robust-fitness" in msg, "one way out: match the checkpoint"
    assert "NEW name" in msg, "the other way out: start fresh"


def test_a_checkpoint_with_the_key_MISSING_is_read_as_raw_and_also_refuses():
    """Every checkpoint written before 2026-09-17 has no such key, and those runs really did rank
    raw -- so missing must mean False, loudly, not 'assume it matches'."""
    with pytest.raises(ValueError) as e:
        _guard({"generation": 4, "population": [[1, 2]]}, True)
    msg = str(e.value)
    assert "key absent" in msg and "RAW" in msg, msg
    assert "robust_fitness=False" in msg and "robust_fitness=True" in msg, msg


def test_a_robust_checkpoint_refuses_to_resume_raw():
    """Symmetric: the mix is just as incomparable in the other direction."""
    with pytest.raises(ValueError) as e:
        _guard({"generation": 9, "robust_fitness": True}, False)
    msg = str(e.value)
    assert "robust_fitness=True" in msg and "robust_fitness=False" in msg, msg


@pytest.mark.parametrize("setting", [True, False])
def test_a_matching_checkpoint_resumes_fine(setting):
    _guard({"generation": 4, "robust_fitness": setting}, setting)   # must not raise


def test_no_robust_fitness_lets_an_old_checkpoint_resume():
    """The documented escape hatch, in the form the operator uses it: an old (key-absent)
    checkpoint resumes under --no-robust-fitness, which is what that run was actually scored on."""
    _guard({"generation": 4, "population": [[1, 2]]}, False)        # must not raise


def test_the_guard_is_wired_into_the_resume_path_and_the_setting_is_written():
    """Wiring, not mechanism (the mechanism is the tests above). Asserted on the real source of
    ``handle_strategy_optimization`` because a correct guard that nothing calls -- or a compare
    against a key nothing writes -- is exactly the silent failure this feature exists to prevent.
    """
    import inspect

    src = inspect.getsource(H.handle_strategy_optimization)
    assert 'data["robust_fitness"] = robust_on' in src, \
        "the checkpoint writer must record the setting it was scored under"
    assert 'robust_on = bool(backtest_cfg.get("robust_fitness"))' in src, \
        "the effective setting must come from the run config the trials read"
    guard = src.index("_assert_checkpoint_robustness_matches(")
    resume = src.index("optimizer.resume_from_checkpoint(")
    assert guard < resume, "the refusal must come BEFORE anything is resumed"


# --------------------------------------------------------------------------- #
# 5. The drivers
# --------------------------------------------------------------------------- #
def _matrix():
    import importlib.util

    path = os.path.join(_ROOT, "tools", "run_options_matrix.py")
    spec = importlib.util.spec_from_file_location("run_options_matrix_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MATRIX_ARGV = [
    "--profile", "discovery",
    "--start", "2024-01-02", "--end", "2024-12-31",
    "--screener-gate-store", "/tmp/store", "--max-stock-price", "0",
]


def test_the_options_matrix_driver_passes_no_flag_by_default():
    """It inherits the launcher default; passing nothing keeps every existing job NAME (the
    discovery digest is computed from the command line) byte-identical."""
    m = _matrix()
    args = m.resolve_args(m.build_parser(), list(_MATRIX_ARGV))
    assert args.robust_fitness is True
    cmd = m.build_cmd(args, "ba2-test", "job", "FMPRating", "O_LC", "AAPL")
    assert "--robust-fitness" not in cmd and "--no-robust-fitness" not in cmd


def test_the_options_matrix_driver_forwards_the_opt_out_and_renames_the_job():
    """Opting out re-ranks the search, so it must reach every job AND change the identity digest:
    a raw-ranked run may not share a name -- and therefore a checkpoint -- with a robust one."""
    m = _matrix()
    on = m.resolve_args(m.build_parser(), list(_MATRIX_ARGV))
    off = m.resolve_args(m.build_parser(), list(_MATRIX_ARGV) + ["--no-robust-fitness"])
    assert off.robust_fitness is False
    assert "--no-robust-fitness" in m.build_cmd(off, "ba2-test", "job", "FMPRating", "O_LC", "AAPL")
    assert (m.discovery_name(on, "ba2-test", "j", "FMPRating", "O_LC", "AAPL")
            != m.discovery_name(off, "ba2-test", "j", "FMPRating", "O_LC", "AAPL"))


def test_the_options_matrix_driver_digest_is_unchanged_by_the_redundant_on_flag():
    """--robust-fitness restates the default, so it must not silently fork every job name."""
    m = _matrix()
    on = m.resolve_args(m.build_parser(), list(_MATRIX_ARGV))
    explicit = m.resolve_args(m.build_parser(), list(_MATRIX_ARGV) + ["--robust-fitness"])
    assert (m.discovery_name(on, "ba2-test", "j", "FMPRating", "O_LC", "AAPL")
            == m.discovery_name(explicit, "ba2-test", "j", "FMPRating", "O_LC", "AAPL"))


# --------------------------------------------------------------------------- #
# 6. The stage-1 wrapper guard (the shipped fragment, executed under bash)
# --------------------------------------------------------------------------- #
_STAGE1 = os.path.join(_ROOT, "tools", "stage1_run.sh")


def _stage1_text() -> str:
    with open(_STAGE1, encoding="utf-8") as f:
        return f.read()


def test_stage1_forwards_nothing_by_default_and_has_the_opt_out_wired():
    text = _stage1_text()
    assert 'STAGE1_ROBUST="${STAGE1_ROBUST:-}"' in text
    assert "ROBUST_ARGS=(--no-robust-fitness)" in text
    assert '${ROBUST_ARGS[@]+"${ROBUST_ARGS[@]}"}' in text


def test_stage1_refuses_to_re_rank_onto_the_raw_metric_without_its_own_suffix(tmp_path):
    """Same rule, and the same reason, as the STAGE1_FITNESS guard beside it: job names are the
    RESUME key, so a re-ranked search under the default '-st1' names would resume into
    checkpoints scored on a different objective. The shipped fragment is EXECUTED here (the
    pattern the market-condition guard test established), not grepped.
    """
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not available on this host")
    text = _stage1_text()
    start = text.index('STAGE1_FITNESS="${STAGE1_FITNESS:-}"')
    end = text.index("# STAGE1_START/END allow explicit shorter pilots")
    fragment = text[start:end] + '\necho "REACHED-THE-EXEC ${ROBUST_ARGS[@]+${ROBUST_ARGS[@]}}"\n'
    script = tmp_path / "guard.sh"
    script.write_text(fragment, encoding="utf-8", newline="\n")

    def run(env):
        return subprocess.run([bash, str(script)], capture_output=True, text=True,
                              env={**os.environ, **env})

    refused = run({"STAGE1_ROBUST": "0", "STAGE1_SUFFIX": ""})   # unset -> the default -st1
    assert refused.returncode == 1
    assert "STAGE1_ROBUST=0" in refused.stderr and "STAGE1_SUFFIX" in refused.stderr
    assert "REACHED-THE-EXEC" not in refused.stdout

    # With its own suffix it is allowed, and the opt-out really is forwarded.
    allowed = run({"STAGE1_ROBUST": "0", "STAGE1_SUFFIX": "-st1raw"})
    assert allowed.returncode == 0
    assert "--no-robust-fitness" in allowed.stdout

    # Unset (the default) forwards nothing, and the launch is the one it has always been.
    default = run({"STAGE1_ROBUST": "", "STAGE1_SUFFIX": ""})
    assert default.returncode == 0
    assert "REACHED-THE-EXEC" in default.stdout
    assert "--no-robust-fitness" not in default.stdout

    # An unrecognised value is a refusal, never a silent "not 0, so on".
    bogus = run({"STAGE1_ROBUST": "maybe", "STAGE1_SUFFIX": "-st1x"})
    assert bogus.returncode == 1
    assert "not a recognised value" in bogus.stderr
