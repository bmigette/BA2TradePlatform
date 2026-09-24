"""``tools/persist_distinct_topn.py`` -- behaviour-distinct TOP-N selection + persist.

Pins the CLI contract on a synthetic ``strategy_optimizations`` row in the suite's throwaway DB
(conftest points DATABASE_URL at a temp sqlite): ``--dry-run`` prints the selection and writes
nothing; ``--skip-already-persisted`` does not re-run a pick an existing Backtest of the same
optimization already covers; a non-completed optimization is refused without
``--allow-running``; and persisting goes through the EXISTING
``ba2test_launcher._persist_top_backtests`` (monkeypatched here) with the selected params -- the
tool reimplements no persistence. One further test drives the real ``_persist_top_backtests``
with an explicit candidate list through its no-re-run (buffered result) branch, pinning the
keyword arguments the tool relies on.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import ba2test_launcher as L  # noqa: E402

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "tools" / "persist_distinct_topn.py"


def _tool(monkeypatch):
    spec = importlib.util.spec_from_file_location("persist_distinct_topn", str(_SCRIPT))
    m = importlib.util.module_from_spec(spec)
    sys.modules["persist_distinct_topn"] = m
    spec.loader.exec_module(m)
    # Never _enter_backend() inside the suite: it chdirs and re-points the shared DB.
    monkeypatch.setattr(m, "_bootstrap", lambda: L)
    # Memory floor: deterministic "unknown" (as on Windows) unless a test says otherwise.
    monkeypatch.setattr(m, "_mem_available_gb", lambda: None)
    return m


@pytest.fixture
def host_db():
    import app.models  # noqa: F401
    from app.models.database import Base, SessionLocal, engine
    Base.metadata.create_all(engine)
    return SessionLocal


def _rec(fit, ret, dd, trades, gene):
    return {"params": {"g": gene}, "fitness": fit, "key": f"k{gene}", "trades": trades,
            "total_return": ret, "max_drawdown": dd}


_ALL_RESULTS = [
    _rec(14.129, 1588.04, -34.3, 351, 1),
    _rec(14.129, 1588.04, -34.3, 351, 2),    # inert-gene clone of gene 1
    _rec(14.129, 1588.04, -34.3, 351, 3),    # another clone
    _rec(12.0, 900.0, -28.0, 280, 4),
    _rec(11.9, 905.0, -28.4, 281, 5),        # near-duplicate of gene 4
    _rec(9.5, 400.0, -15.0, 150, 6),
    _rec(-1.0e9, 0.0, 0.0, 0, 7),            # zero-trade sentinel
    _rec(30.0, 5000.0, -10.0, 12, 8),        # below the 30-trade gate
]


def _make_opt(SessionLocal, status="completed", worker_ids=None, window=None):
    from app.models.strategy_optimization import StrategyOptimization
    db = SessionLocal()
    try:
        row = StrategyOptimization(
            strategy_id=1, name=f"O_LC-test-{uuid.uuid4().hex[:8]}", status=status,
            fitness_metric="option_car_target", optimization_type="genetic",
            optimization_config={"backtest": {
                **(window if window is not None
                   else {"start_date": "2020-01-01", "end_date": "2025-12-31"}),
                "initial_capital": 20000.0, "labels": ["stage1"],
                "experts": [{"class": "FMPRating", "settings": {}}]}},
            all_results=_ALL_RESULTS, best_params={"g": 1}, best_fitness=14.129,
            worker_ids=worker_ids)
        db.add(row); db.commit(); db.refresh(row)
        return row.id, row.name
    finally:
        db.close()


def _count_backtests(SessionLocal, opt_id):
    from app.models.backtest import Backtest
    db = SessionLocal()
    try:
        return db.query(Backtest).filter(Backtest.optimization_id == opt_id).count()
    finally:
        db.close()


def _add_backtest(SessionLocal, opt_id, name, params, *, ret, dd, trades, ga_fitness=None,
                  equity_curve=None, bt_trades=None, labels=None, results=None):
    from app.models.backtest import Backtest
    db = SessionLocal()
    try:
        bt = Backtest(name=name, engine_type="daily_expert", expert_name="FMPRating",
                      optimization_id=opt_id, strategy_params=params, labels=labels,
                      start_date=datetime(2020, 1, 1), end_date=datetime(2025, 12, 31),
                      initial_capital=20000.0, status="completed", total_return=ret,
                      max_drawdown=dd, total_trades=trades, ga_fitness=ga_fitness,
                      equity_curve=equity_curve or [], trades=bt_trades or [], is_saved=True,
                      results=results)
        db.add(bt); db.commit(); db.refresh(bt)
        return bt.id
    finally:
        db.close()


def _forbid_persist(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("_persist_top_backtests must not be called")
    monkeypatch.setattr(L, "_persist_top_backtests", _boom)


def test_dry_run_prints_selection_and_writes_nothing(host_db, monkeypatch, capsys):
    tool = _tool(monkeypatch)
    opt_id, _ = _make_opt(host_db)
    _forbid_persist(monkeypatch)
    before = _count_backtests(host_db, opt_id)
    assert tool.main(["--opt-id", str(opt_id), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert _count_backtests(host_db, opt_id) == before == 0
    assert "--dry-run: nothing re-run, nothing written." in out
    # Three picks: the gene-1 clone group (2 clones), gene 4 (absorbing its near-duplicate gene
    # 5) and gene 6. The sentinel and the low-trade genome are never picks.
    lines = [ln for ln in out.splitlines() if ln[:3].strip().isdigit()]
    assert len(lines) == 3, out
    assert lines[0].split()[1] == "14.1290" and lines[0].split()[6] == "2"   # clones
    assert lines[1].split()[1] == "12.0000" and lines[1].split()[7] == "1"   # near-dup
    assert "distinct behaviours: 4" in out
    assert "1 sentinel/unmeasured, 1 below 30 round-trip rows" in out
    assert "CAR(GA)" in out


def test_refuses_running_optimization_without_allow_running(host_db, monkeypatch, capsys):
    tool = _tool(monkeypatch)
    opt_id, _ = _make_opt(host_db, status="running")
    _forbid_persist(monkeypatch)
    assert tool.main(["--opt-id", str(opt_id), "--dry-run"]) == 2
    assert "not 'completed'" in capsys.readouterr().err
    assert tool.main(["--opt-id", str(opt_id), "--dry-run", "--allow-running"]) == 0


def test_select_by_name(host_db, monkeypatch, capsys):
    tool = _tool(monkeypatch)
    _opt_id, name = _make_opt(host_db)
    _forbid_persist(monkeypatch)
    assert tool.main(["--name", name, "--dry-run", "--n", "1"]) == 0
    out = capsys.readouterr().out
    assert len([ln for ln in out.splitlines() if ln[:3].strip().isdigit()]) == 1


def _fake_persist(host_db, calls):
    """Stand-in for _persist_top_backtests: records the call and writes one completed row per
    candidate (as the real one would), reporting (rank, id) through ``persisted_ids``."""
    def fake(opt_id, expert, n=5, parallel=1, last_gen_full_results=None, **kw):
        calls.append(dict(opt_id=opt_id, expert=expert, n=n, parallel=parallel, **kw))
        curve = [{"date": "2020-01-02", "equity": 20000.0},
                 {"date": "2020-12-31", "equity": 24000.0},
                 {"date": "2021-12-31", "equity": 30000.0},
                 {"date": "2022-12-30", "equity": 27000.0}]
        trades = [{"pnl": p} for p in (4000.0, 2000.0, 1000.0, 500.0, 300.0, 200.0)]
        for (params, _key, fit), rank in zip(kw["candidates"], kw["ranks"]):
            bid = _add_backtest(host_db, opt_id, f"{kw['name_prefix']}{rank}-x", dict(params),
                                ret=35.0, dd=-12.0, trades=6, ga_fitness=fit,
                                equity_curve=curve, bt_trades=trades,
                                labels=kw["extra_labels"])
            kw["persisted_ids"].append((rank, bid))
        return len(kw["candidates"])
    return fake


def test_persist_calls_existing_persist_function_with_selected_params(host_db, monkeypatch,
                                                                       capsys):
    tool = _tool(monkeypatch)
    opt_id, _ = _make_opt(host_db)
    calls: list = []
    monkeypatch.setattr(L, "_persist_top_backtests", _fake_persist(host_db, calls))
    assert tool.main(["--opt-id", str(opt_id), "--labels", "extra1"]) == 0
    # --parallel 1 (default): one call per pick, in rank order, local only.
    assert [c["ranks"] for c in calls] == [[1], [2], [3]]
    assert [c["candidates"][0][0] for c in calls] == [{"g": 1}, {"g": 4}, {"g": 6}]
    assert [c["candidates"][0][2] for c in calls] == [14.129, 12.0, 9.5]
    for c in calls:
        assert c["expert"] == "FMPRating" and c["n"] == 1 and c["parallel"] == 1
        assert c["name_prefix"] == "DTOP" and c["use_remote_workers"] is False
        assert c["extra_labels"] == ["TopNDistinct", "extra1"]
    out = capsys.readouterr().out
    assert "SUMMARY" in out
    summary = out.split("SUMMARY", 1)[1]
    assert "2020" in summary and "2021" in summary and "2022" in summary
    # 2020: 20000 -> 24000 = +20%; top1 = 4000/8000 = 50%, top5 = 7800/8000 = 98%.
    assert "20.0%" in summary and "50%" in summary and "98%" in summary


def test_parallel_batches_candidates(host_db, monkeypatch):
    tool = _tool(monkeypatch)
    opt_id, _ = _make_opt(host_db)
    calls: list = []
    monkeypatch.setattr(L, "_persist_top_backtests", _fake_persist(host_db, calls))
    assert tool.main(["--opt-id", str(opt_id), "--parallel", "2"]) == 0
    assert [c["ranks"] for c in calls] == [[1, 2], [3]]
    assert all(c["parallel"] == 2 for c in calls)


def test_skip_already_persisted(host_db, monkeypatch, capsys):
    tool = _tool(monkeypatch)
    opt_id, _ = _make_opt(host_db)
    # Rank 1's genome was persisted by the end-of-job TOP-N (strategy_params carries the added
    # rule keys); rank 3's BEHAVIOUR was persisted under a different (clone) genome.
    b1 = _add_backtest(host_db, opt_id, "TOP1-x", {"g": 1, "entryRules": [], "exitRules": []},
                       ret=1588.04, dd=-34.3, trades=351)
    b3 = _add_backtest(host_db, opt_id, "TOP4-x", {"g": 99}, ret=400.0, dd=-15.0, trades=150)
    calls: list = []
    monkeypatch.setattr(L, "_persist_top_backtests", _fake_persist(host_db, calls))
    assert tool.main(["--opt-id", str(opt_id), "--skip-already-persisted"]) == 0
    assert [c["ranks"] for c in calls] == [[2]]
    out = capsys.readouterr().out
    assert f"bt#{b1} TOP1-x (key match)" in out
    assert f"bt#{b3} TOP4-x (fingerprint match)" in out
    summary = out.split("SUMMARY", 1)[1]
    assert f"{b1:>6}" in summary and f"{b3:>6}" in summary   # reported from existing rows
    # Without the flag the existing rows are only displayed; every pick is re-run.
    calls.clear()
    assert tool.main(["--opt-id", str(opt_id)]) == 0
    assert [c["ranks"] for c in calls] == [[1], [2], [3]]


def _stub_persist_internals(monkeypatch):
    import app.services.strategy_optimization_handler as SOH
    import app.services.strategy_param_space as SPS
    import app.services.sync_client as SC
    monkeypatch.setattr(SPS, "decode_params", lambda strat, params: {})
    monkeypatch.setattr(SOH, "_build_daily_trial_config",
                        lambda bt_block, decoded, hoisted, **kw: {})
    monkeypatch.setattr(SC, "push_backtest", lambda bt, db: None)
    return SOH


_ENGINE_RESULTS = {"total_trades": 40, "winning_trades": 25, "losing_trades": 15,
                   "win_rate": 62.5, "total_return": 120.0, "sharpe_ratio": 1.1,
                   "max_drawdown": -18.0, "profit_factor": 1.6, "avg_trade_duration": 5.0,
                   "final_equity": 44000.0, "equity_curve": [], "drawdown_curve": [],
                   "trades": []}


def test_real_persist_top_backtests_accepts_explicit_candidates(host_db, monkeypatch, capsys):
    """The keyword arguments the tool relies on, through the REAL _persist_top_backtests: the
    candidate list replaces the fitness ranking, ranks/prefix name the rows (and the log lines),
    extra labels are merged, remote workers are skipped and (rank, id) is reported. The
    buffered-result branch (key present in last_gen_full_results) keeps it free of re-runs and
    process pools."""
    from app.models.backtest import Backtest
    SOH = _stub_persist_internals(monkeypatch)
    opt_id, opt_name = _make_opt(host_db, worker_ids=[5])

    def _no_rank(*a, **k):
        raise AssertionError("explicit candidates must bypass _rank_measured_candidates")
    monkeypatch.setattr(L, "_rank_measured_candidates", _no_rank)

    def _no_workers(*a, **k):
        raise AssertionError("use_remote_workers=False must not resolve workers")
    monkeypatch.setattr(SOH, "_resolve_workers", _no_workers)

    ids: list = []
    n = L._persist_top_backtests(
        opt_id, "FMPRating", n=1, parallel=1,
        last_gen_full_results={"k6": dict(_ENGINE_RESULTS)},
        candidates=[({"g": 6}, "k6", 9.5)], ranks=[3], name_prefix="DTOP",
        extra_labels=["TopNDistinct", "stage1"], use_remote_workers=False, persisted_ids=ids)
    assert n == 1 and len(ids) == 1 and ids[0][0] == 3
    assert "persisted DTOP3 (1/1) [no re-run]" in capsys.readouterr().out
    db = host_db()
    try:
        bt = db.query(Backtest).filter(Backtest.id == ids[0][1]).first()
        assert bt.name == f"DTOP3-{opt_name}"
        assert bt.labels == ["stage1", "TopNDistinct"]
        assert bt.ga_fitness == 9.5 and bt.is_saved and bt.status == "completed"
        assert bt.strategy_params == {"g": 6}
        assert bt.total_return == 120.0
    finally:
        db.close()


def test_real_persist_top_backtests_default_call_unchanged(host_db, monkeypatch, capsys):
    """WITHOUT the new keyword arguments (the grid's own call) the persist path is what it was:
    fitness ranking via _rank_measured_candidates, TOP prefix, the optimization's labels
    verbatim, remote workers resolved from opt.worker_ids, TOP<n> in the log lines."""
    from app.models.backtest import Backtest
    SOH = _stub_persist_internals(monkeypatch)
    opt_id, opt_name = _make_opt(host_db, worker_ids=[5])
    seen = []

    def _resolve(db, worker_ids):
        seen.append(worker_ids)
        return []
    monkeypatch.setattr(SOH, "_resolve_workers", _resolve)

    def _no_pool(*a, **k):
        raise AssertionError("TOP1 must come from the buffered result, not a real re-run")
    monkeypatch.setattr(L, "_new_local_pool", _no_pool)
    # The grid's ranker has no trade floor, so its TOP1 is gene 8 (fitness 30, 12 rows) -- the
    # genome the distinct selection excludes.
    n = L._persist_top_backtests(opt_id, "FMPRating", n=1, parallel=1,
                                 last_gen_full_results={"k8": dict(_ENGINE_RESULTS)})
    assert n == 1
    assert seen == [[5]]
    assert "persisted TOP1 (1/1) [no re-run]" in capsys.readouterr().out
    db = host_db()
    try:
        bt = (db.query(Backtest).filter(Backtest.optimization_id == opt_id)
                .order_by(Backtest.id.desc()).first())
        assert bt.name == f"TOP1-{opt_name}"
        assert bt.labels == ["stage1"]
        assert bt.strategy_params == {"g": 8} and bt.ga_fitness == 30.0
    finally:
        db.close()


def test_diverged_rows_are_not_fingerprint_matched(host_db, monkeypatch, capsys):
    """A persisted re-run flagged ga_fitness_divergence did not reproduce the GA's score: its
    metrics must not stand in for a pick's behaviour (a genome/key match still counts)."""
    tool = _tool(monkeypatch)
    opt_id, _ = _make_opt(host_db)
    _add_backtest(host_db, opt_id, "TOP4-x", {"g": 99}, ret=400.0, dd=-15.0, trades=150,
                  results={"ga_fitness_divergence": 0.4})
    calls: list = []
    monkeypatch.setattr(L, "_persist_top_backtests", _fake_persist(host_db, calls))
    assert tool.main(["--opt-id", str(opt_id), "--skip-already-persisted"]) == 0
    assert [c["ranks"] for c in calls] == [[1], [2], [3]]
    assert "TOP4-x" not in capsys.readouterr().out.split("SUMMARY")[0]


def test_memory_floor_refuses_unless_forced(host_db, monkeypatch, capsys):
    tool = _tool(monkeypatch)
    opt_id, _ = _make_opt(host_db)
    calls: list = []
    monkeypatch.setattr(L, "_persist_top_backtests", _fake_persist(host_db, calls))
    monkeypatch.setattr(tool, "_mem_available_gb", lambda: 5.0)
    assert tool.main(["--opt-id", str(opt_id)]) == 2
    assert "MemAvailable 5.0 GB < --min-free-gb 20" in capsys.readouterr().err
    assert calls == []
    # The dry-run never checks (it re-runs nothing).
    assert tool.main(["--opt-id", str(opt_id), "--dry-run"]) == 0
    assert tool.main(["--opt-id", str(opt_id), "--min-free-gb", "4"]) == 0
    assert len(calls) == 3
    calls.clear()
    assert tool.main(["--opt-id", str(opt_id), "--force-memory"]) == 0
    assert len(calls) == 3


def test_min_trade_rows_flag_and_alias(host_db, monkeypatch, capsys):
    tool = _tool(monkeypatch)
    opt_id, _ = _make_opt(host_db)
    _forbid_persist(monkeypatch)
    for flag in ("--min-trade-rows", "--min-trades"):
        assert tool.main(["--opt-id", str(opt_id), "--dry-run", flag, "0"]) == 0
        out = capsys.readouterr().out
        rows = [ln for ln in out.splitlines() if ln[:3].strip().isdigit()]
        assert rows[0].split()[1] == "30.0000", out   # the 12-row genome is now allowed


def test_n_below_one_rejected(monkeypatch):
    tool = _tool(monkeypatch)
    with pytest.raises(SystemExit) as e:
        tool.main(["--opt-id", "1", "--n", "0", "--dry-run"])
    assert e.value.code == 2


def test_missing_window_is_refused(host_db, monkeypatch, capsys):
    tool = _tool(monkeypatch)
    opt_id, _ = _make_opt(host_db, window={"start_date": "2020-01-01"})
    _forbid_persist(monkeypatch)
    assert tool.main(["--opt-id", str(opt_id), "--dry-run"]) == 2
    assert "has no end_date" in capsys.readouterr().err
    opt_id, _ = _make_opt(host_db, window={"start_date": "2020-01-01", "end_date": "garbage"})
    assert tool.main(["--opt-id", str(opt_id), "--dry-run"]) == 2
    assert "unparseable date" in capsys.readouterr().err
