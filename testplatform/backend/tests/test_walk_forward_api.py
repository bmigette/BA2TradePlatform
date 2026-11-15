"""Walk-forward API (/api/walk-forward): preview / create validate through the CLI's path (wf_chain spec,
check_user_optimize_args, fold + threshold validation, the launcher's estimates-source and holdout
rails) and print refusals verbatim; a created run executes through the chain (fake runner here) and
is listed / shown; cancel, resume, the task handler and the stitched out-of-sample equity.

Uses the module-level engine (the package conftest points DATABASE_URL at a throwaway sqlite): the
routes, the store and the handler all open their own sessions on it, as in production."""
from __future__ import annotations

import sys
import textwrap
from datetime import datetime

import pytest

from app.services import walk_forward as WF
from app.services import wf_runs as R
from app.services import wf_store as S
from tests.test_wf_matrix_chain import BAD, GOOD, ChainRunner

BASE = ["--expert", "FMPRating", "--strategy", "S2", "--universe", "AAPL,MSFT", "--fitness",
        "consistent_annual_return", "--population", "20", "--estimates-source", "fmp"]


@pytest.fixture(scope="module")
def _tables():
    from app.models.database import Base, engine
    import app.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    yield engine


@pytest.fixture
def client(_tables):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)          # no lifespan: no queue workers, nothing runs in the background


@pytest.fixture
def db(_tables):
    from app.models.database import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _req(name, **kw):
    r = dict(name=name, optimize_args=list(BASE), train_start="2020-01-01", test_years=[2024, 2025, 2026],
             data_end=None, first_generations=30, next_generations=20, top_n=2, test_parallel=1,
             min_oos_return=0.0, exclusive_return=True, max_dd_mult=2.5, max_oos_dd=35.0, min_folds=2,
             require_test_years=[2026], defer_last_test=False, run_all_folds=False, refit=True)
    r.update(kw)
    return r


def _row(db, run_id):
    from app.models.walk_forward_run import WalkForwardRun
    db.expire_all()
    return db.query(WalkForwardRun).filter(WalkForwardRun.id == run_id).first()


# ------------------------------------------------------------------ options / preview


def test_options_come_from_the_launcher(client):
    o = client.get("/api/walk-forward/options").json()
    assert "FMPRating" in o["experts"] and "S2" in o["strategies"]
    assert o["estimatesSources"] == ["fmp", "alphavantage"]
    assert "--start" in o["forbiddenFlags"] and o["prefill"]["max_dd_mult"] == 2.5


def test_preview_returns_the_folds_and_the_exact_chain_commands(client):
    r = client.post("/api/walk-forward/preview", json=_req("wfapi-prev"))
    assert r.status_code == 200, r.text
    p = r.json()
    assert [(f["train_start"], f["train_end"], f["test_start"], f["test_end"]) for f in p["folds"]] == [
        ("2020-01-01", "2023-12-31", "2024-01-01", "2024-12-31"),
        ("2020-01-01", "2024-12-31", "2025-01-01", "2025-12-31"),
        ("2020-01-01", "2025-12-31", "2026-01-01", "2026-12-31")]
    c1, c2 = p["folds"][0]["command"], p["folds"][1]["command"]
    assert c1[c1.index("--generations") + 1] == "30" and "--persist-final-population" in c1
    assert c2[c2.index("--chain-seed-from") + 1] == "wfapi-prev-wf1"
    assert c2[c2.index("--name") + 1] == "wfapi-prev-wf2" and c2[c2.index("--end") + 1] == "2024-12-31"
    assert p["refit"]["job"] == "wfapi-prev-refit" and p["refit"]["train_end"] == "2026-12-31"
    assert p["jobs"] == ["wfapi-prev-wf1", "wfapi-prev-wf2", "wfapi-prev-wf3", "wfapi-prev-refit"]
    assert p["existingJobs"] == [] and p["nameTaken"] is False


def test_preview_of_a_deferred_last_fold(client):
    p = client.post("/api/walk-forward/preview",
                    json=_req("wfapi-defer", data_end="2025-12-31", defer_last_test=True)).json()
    assert p["deferred_fold"] == 3 and p["folds"][2]["deferred"] is True and p["refit"] is None


def _option_strategy():
    L = R.launcher_module()
    return sorted(s for s in L._PURE_OPTION_STRATEGIES if s in L._STRATEGY_BUILDERS)[0]


@pytest.mark.strict_estimates_source
@pytest.mark.parametrize("change,needle", [
    (dict(optimize_args=BASE + ["--start", "2020-01-01"]), "optimize args must not contain --start"),
    (dict(optimize_args=BASE + ["--warm-start-from", "7"]), "look-ahead"),
    (dict(optimize_args=[a for a in BASE if a not in ("--estimates-source", "fmp")]),
     "--estimates-source {fmp,alphavantage} is required"),
    (dict(optimize_args=BASE[2:]), "optimize args lack --expert"),
    (dict(test_years=[2025, 2024]), "strictly ascending"),
    (dict(min_folds=4), "--pass-min-folds 4 must be between 1 and the number of folds"),
    (dict(require_test_years=[2023]), "no fold tests that year"),
    (dict(data_end="2025-06-30"), "would be empty"),
    (dict(name="bad name"), "use letters, digits"),
    (dict(top_n=0), "top_n must be >= 1"),
])
def test_refusals_are_400_with_the_engines_own_message(client, change, needle):
    req = {**_req("wfapi-bad"), **change}
    r = client.post("/api/walk-forward/preview", json=req)
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]
    r2 = client.post("/api/walk-forward/runs", json=req)
    assert r2.status_code == 400 and needle in r2.json()["detail"]


def test_the_launchers_option_holdout_rail_refuses_a_train_window_into_2026(client):
    args = [a if a != "S2" else _option_strategy() for a in BASE]
    r = client.post("/api/walk-forward/preview",
                    json=_req("wfapi-opt", optimize_args=args, test_years=[2026, 2027],
                              require_test_years=[], min_folds=1))
    assert r.status_code == 400 and "reserved walk-forward holdout" in r.json()["detail"]
    # the refit window is checked too: a fold set that stops before 2026 still refits into it
    r = client.post("/api/walk-forward/preview",
                    json=_req("wfapi-opt", optimize_args=args, test_years=[2024, 2025, 2026],
                              require_test_years=[], min_folds=1))
    assert r.status_code == 400 and "reserved walk-forward holdout" in r.json()["detail"]


def test_a_job_name_completed_under_another_estimates_source_is_refused(client, db):
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    st = Strategy(name="wfapi-est-s", entry_rules=[], exit_rules=[])
    db.add(st)
    db.commit()
    db.add(StrategyOptimization(strategy_id=st.id, name="wfapi-est-wf1", fitness_metric="sharpe",
                                optimization_type="genetic", status="completed",
                                optimization_config={"backtest": {"estimates_source": "alphavantage"}}))
    db.commit()
    r = client.post("/api/walk-forward/preview", json=_req("wfapi-est"))
    assert r.status_code == 400 and "one name must not stand for two estimate sources" in r.json()["detail"]


# ------------------------------------------------------------------ create -> drive -> view


class ApiRunner(ChainRunner):
    """A fake chain runner that keeps the row like the real one (default session = the module DB)."""

    def wf_store(self, spec, report_dir):
        return S.RunStore(spec.name, source=self.wf_source, token=self.wf_token,
                          task_id=self.wf_task_id, report_dir=report_dir)

    wf_source, wf_token, wf_task_id = "cli", None, None


def test_create_queues_a_pending_ui_run_and_refuses_a_taken_name(client, db):
    from app.models.task_queue import TaskQueue
    r = client.post("/api/walk-forward/runs", json=_req("wfapi-new"))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "pending" and d["source"] == "ui" and d["plannedFolds"] == 3
    assert d["spec"]["optimize_args"] == BASE and d["spec"]["thresholds"]["max_oos_dd"] == 35.0
    task = db.query(TaskQueue).filter(TaskQueue.task_id == d["taskId"]).first()
    assert task.task_type == "walk_forward" and task.status == "queued" and task.max_retries == 0
    assert task.payload["run_id"] == d["id"] and task.payload["token"] == _row(db, d["id"]).driver_token
    again = client.post("/api/walk-forward/runs", json=_req("wfapi-new"))
    assert again.status_code == 409 and "already exists" in again.json()["detail"]


def test_a_created_run_driven_through_the_chain_is_listed_and_shown(client, db, tmp_path):
    d = client.post("/api/walk-forward/runs", json=_req("wfapi-run")).json()
    row = _row(db, d["id"])
    runner = ApiRunner([GOOD, GOOD, GOOD], prefix="wfapi-run")
    res = R.drive(d["id"], row.driver_token, runner, task_id=d["taskId"], report_dir=str(tmp_path),
                  log=lambda *_: None)
    assert res.verdict == "PASS"
    assert (tmp_path / "wfapi-run" / "cell.json").exists()          # the files, as always
    lst = client.get("/api/walk-forward/runs").json()["runs"]
    mine = next(x for x in lst if x["id"] == d["id"])
    assert mine["status"] == "pass" and (mine["foldsDone"], mine["plannedFolds"]) == (3, 3)
    assert mine["stitchedReturn"] == pytest.approx(WF.compound([5.0, 5.0, 5.0]))
    assert "report" not in mine and "spec" not in mine                # light payload
    det = client.get(f"/api/walk-forward/runs/{d['id']}").json()
    assert det["source"] == "ui" and det["report"]["verdict"] == "PASS"
    assert [f["state"] for f in det["report"]["folds"]] == ["done"] * 3
    assert det["report"]["folds"][0]["ranks"][0]["oos_return"] == 5.0
    assert det["jobs"] == {} and det["backtests"] == {"1": False}   # id 1 is not this run's row
    assert det["canResume"] is False and det["canCancel"] is False


def _task_done(task_id):
    from app.models.database import SessionLocal
    from app.models.task_queue import TaskQueue
    s = SessionLocal()
    s.query(TaskQueue).filter(TaskQueue.task_id == task_id).update({"status": "completed"})
    s.commit()
    s.close()


def test_a_stale_start_never_drives_the_run(client, db):
    d = client.post("/api/walk-forward/runs", json=_req("wfapi-stale")).json()
    with pytest.raises(WF.WalkForwardError, match="token mismatch"):
        R.drive(d["id"], "not-the-token", ApiRunner([GOOD, GOOD, GOOD], prefix="wfapi-stale"),
                log=lambda *_: None)


def test_cancel_a_queued_run_then_resume_it(client, db):
    from app.models.task_queue import TaskQueue
    d = client.post("/api/walk-forward/runs", json=_req("wfapi-cancel")).json()
    c = client.post(f"/api/walk-forward/runs/{d['id']}/cancel")
    assert c.status_code == 200, c.text
    assert c.json()["status"] == "cancelled" and c.json()["canResume"] is True
    assert db.query(TaskQueue.status).filter(TaskQueue.task_id == d["taskId"]).scalar() == "cancelled"
    old_token = _row(db, d["id"]).driver_token
    r = client.post(f"/api/walk-forward/runs/{d['id']}/resume")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending" and r.json()["taskId"] != d["taskId"]
    assert _row(db, d["id"]).driver_token != old_token
    # queued again: a second resume is refused, a cancel works
    assert client.post(f"/api/walk-forward/runs/{d['id']}/resume").status_code == 409
    assert client.post(f"/api/walk-forward/runs/{d['id']}/cancel").json()["status"] == "cancelled"


def test_resume_of_a_deferred_run_with_the_full_data_tests_the_last_fold(client, db, tmp_path):
    d = client.post("/api/walk-forward/runs",
                    json=_req("wfapi-2ph", data_end="2025-12-31", defer_last_test=True)).json()
    runner = ApiRunner([GOOD, GOOD, GOOD], prefix="wfapi-2ph")
    R.drive(d["id"], _row(db, d["id"]).driver_token, runner, report_dir=str(tmp_path),
            log=lambda *_: None)
    assert _row(db, d["id"]).status == "deferred-pending"
    _task_done(d["taskId"])                     # what the handler does when the driver exits
    r = client.post(f"/api/walk-forward/runs/{d['id']}/resume",
                    json={"change_data_end": True, "data_end": None})
    assert r.status_code == 200, r.text
    row = _row(db, d["id"])
    assert row.spec["data_end"] is None and row.status == "pending"
    again = ApiRunner([GOOD, GOOD, GOOD], prefix="wfapi-2ph", completed=set(runner.completed))
    assert R.drive(d["id"], row.driver_token, again, report_dir=str(tmp_path),
                   log=lambda *_: None).verdict == "PASS"
    assert _row(db, d["id"]).status == "pass"


def test_imported_and_cli_rows_are_view_only(client, db, tmp_path):
    from app.models.walk_forward_run import WalkForwardRun
    for name, source in (("wfapi-imp", "import"), ("wfapi-cli", "cli")):
        db.add(WalkForwardRun(name=name, source=source, status="error", spec={"optimize_args": None},
                              report={"folds": []}))
    db.commit()
    imp = db.query(WalkForwardRun).filter(WalkForwardRun.name == "wfapi-imp").first()
    cli = db.query(WalkForwardRun).filter(WalkForwardRun.name == "wfapi-cli").first()
    r = client.post(f"/api/walk-forward/runs/{imp.id}/resume")
    assert r.status_code == 400 and "imported" in r.json()["detail"]
    r = client.post(f"/api/walk-forward/runs/{cli.id}/resume")
    assert r.status_code == 400 and "CLI driver" in r.json()["detail"]
    assert client.get(f"/api/walk-forward/runs/{imp.id}").json()["canResume"] is False


# ------------------------------------------------------------------ handler


def _script(tmp_path, body):
    p = tmp_path / "fake_driver.py"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


def test_handler_marks_a_refused_driver_as_error(client, db, tmp_path, monkeypatch):
    d = client.post("/api/walk-forward/runs", json=_req("wfapi-h-err")).json()
    row = _row(db, d["id"])
    row.report_dir = str(tmp_path)
    db.commit()
    monkeypatch.setattr(R, "DRIVER_SCRIPT", _script(tmp_path, """
        import sys
        print("run_wf_run: refused for a reason", file=sys.stderr)
        sys.exit(2)
    """))
    out = R.handle_walk_forward(d["taskId"], {"run_id": d["id"], "token": row.driver_token})
    assert out["status"] == "failed" and "exited with code 2 (refused)" in out["error"]
    row = _row(db, d["id"])
    assert row.status == "error" and "code 2" in row.error_message
    det = client.get(f"/api/walk-forward/runs/{d['id']}").json()
    assert any("refused for a reason" in ln for ln in det["logTail"])


def test_handler_kills_the_driver_tree_on_cancel(client, db, tmp_path, monkeypatch):
    import threading
    from app.services.task_queue import get_wf_task_queue
    d = client.post("/api/walk-forward/runs", json=_req("wfapi-h-cancel")).json()
    row = _row(db, d["id"])
    row.report_dir = str(tmp_path)
    db.commit()
    pidfile = tmp_path / "child.pid"
    monkeypatch.setattr(R, "DRIVER_SCRIPT", _script(tmp_path, f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open(r"{pidfile}", "w").write(str(child.pid))
        time.sleep(120)
    """))
    from app.models.database import SessionLocal
    from app.models.task_queue import TaskQueue
    s = SessionLocal()
    s.query(TaskQueue).filter(TaskQueue.task_id == d["taskId"]).update({"status": "running"})
    s.commit()
    s.close()
    out = {}
    t = threading.Thread(target=lambda: out.update(
        R.handle_walk_forward(d["taskId"], {"run_id": d["id"], "token": row.driver_token})))
    t.start()
    import time
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.1)
    get_wf_task_queue().cancel_task(d["taskId"])
    t.join(timeout=60)
    assert out["status"] == "cancelled"
    import psutil
    assert not psutil.pid_exists(int(pidfile.read_text())) or \
        psutil.Process(int(pidfile.read_text())).status() == psutil.STATUS_ZOMBIE
    assert _row(db, d["id"]).status == "cancelled"


def test_startup_recovery_marks_an_orphaned_ui_run_as_error(db):
    from app.models.walk_forward_run import WalkForwardRun
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    from app.services.optimization_resume import this_host
    db.add(WalkForwardRun(name="wfapi-orphan", source="ui", status="running", task_id="gone",
                          driver_host=this_host(), driver_pid=p.pid, driver_pid_started=1.0))
    db.commit()
    assert R.recover_interrupted(db) >= 1
    db.expire_all()
    row = db.query(WalkForwardRun).filter(WalkForwardRun.name == "wfapi-orphan").first()
    assert row.status == "error" and "interrupted" in row.error_message


# ------------------------------------------------------------------ stitched equity


def test_oos_equity_chains_the_fold_winners_curves(client, db):
    from app.models.backtest import Backtest
    from app.models.walk_forward_run import WalkForwardRun
    ids = []
    for k, (a, b) in enumerate([(10000, 11000), (10000, 9000)], start=1):
        bt = Backtest(name=f"WF{k}-OOS-R1-wfapi-eq-wf{k}", engine_type="daily_expert",
                      start_date=datetime(2023 + k, 1, 1), end_date=datetime(2023 + k, 12, 31),
                      initial_capital=10000.0, status="completed",
                      equity_curve=[{"date": f"{2023 + k}-01-02", "equity": a},
                                    {"date": f"{2023 + k}-12-31", "equity": b}])
        db.add(bt)
        db.commit()
        ids.append(bt.id)

    def fold(k, bid):
        return {"index": k, "state": "done", "train_job": f"wfapi-eq-wf{k}",
                "test_start": f"{2023 + k}-01-01", "test_end": f"{2023 + k}-12-31",
                "ranks": [{"fold": k, "rank": 1, "oos_backtest_id": bid}]}

    row = WalkForwardRun(name="wfapi-eq", source="import", status="pass",
                         report={"folds": [fold(1, ids[0]), fold(2, ids[1])]})
    db.add(row)
    db.commit()
    e = client.get(f"/api/walk-forward/runs/{row.id}/oos-equity").json()
    assert e["available"] is True and e["missing"] == []
    assert [p["equity"] for p in e["points"]] == pytest.approx([10000, 11000, 11000, 9900])
    # a report whose id is another DB's row (different name) is not charted
    row.report = {"folds": [fold(1, ids[1])]}
    db.commit()
    e = client.get(f"/api/walk-forward/runs/{row.id}/oos-equity").json()
    assert e["available"] is False and e["missing"] == ["fold 1"]
