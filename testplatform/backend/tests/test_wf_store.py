"""Walk-forward results in the DB (app.services.wf_store): the chain's ``walk_forward_runs`` row is written
as folds complete, IN ADDITION to the report files (which stay byte-identical), the two-phase deferred
state, failures are recorded and loud, a second driver never overwrites a live row, and a report
directory imports idempotently. Fake runners only; nothing is optimized."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from app.services import optimization_resume as R
from app.services import walk_forward as WF
from app.services import wf_chain as C
from app.services import wf_store as S
from tests.test_walk_forward import D
from tests.test_wf_matrix_chain import BAD, GOOD, ChainRunner, _spec


@pytest.fixture
def wfdb(tmp_path):
    """A session factory on a fresh DB with every model table."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.database import Base
    import app.models  # noqa: F401
    eng = create_engine(f"sqlite:///{tmp_path / 'wf.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield sessionmaker(bind=eng)
    eng.dispose()


def _row(Sf, name="cell"):
    from app.models.walk_forward_run import WalkForwardRun
    db = Sf()
    try:
        return db.query(WalkForwardRun).filter(WalkForwardRun.name == name).first()
    finally:
        db.close()


class StoreRunner(ChainRunner):
    """ChainRunner that offers the DB writer, like the real runner, plus a hook to look at the row
    while the chain runs."""

    def __init__(self, oos_by_fold, Sf, on_oos=None, **kw):
        super().__init__(oos_by_fold, **kw)
        self.Sf, self.on_oos = Sf, on_oos

    def wf_store(self, spec, report_dir):
        return S.RunStore(spec.name, source="cli", token="tok", report_dir=report_dir,
                          session_factory=self.Sf)

    def train_identity(self, name):
        return {"id": 100 + int(name.rsplit("wf", 1)[1]), "completed_at": "2026-11-01T00:00:00"}

    def run_oos(self, train_name, fold, picks, name_prefix, labels, parallel, data_args=None):
        if self.on_oos:
            self.on_oos(fold)
        return super().run_oos(train_name, fold, picks, name_prefix, labels, parallel)


def _files(d):
    out = {}
    for root, _, files in os.walk(d):
        for f in files:
            if f.endswith(".json") or f.endswith(".md"):
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    out[os.path.relpath(os.path.join(root, f), d)] = fh.read()
    return out


class IdRunner(ChainRunner):
    def train_identity(self, name):
        return {"id": 100 + int(name.rsplit("wf", 1)[1]), "completed_at": "2026-11-01T00:00:00"}


# ------------------------------------------------------------------ live writer


def test_passing_cell_writes_its_row_and_the_files_stay_byte_identical(tmp_path, wfdb):
    plain, stored = tmp_path / "plain", tmp_path / "stored"
    C.run_cell(_spec(), IdRunner([GOOD, GOOD, GOOD]), str(plain), log=lambda *_: None)
    res = C.run_cell(_spec(), StoreRunner([GOOD, GOOD, GOOD], wfdb), str(stored), log=lambda *_: None)
    assert _files(plain) == _files(stored)             # the DB row is IN ADDITION to the files
    row = _row(wfdb)
    assert row.status == "pass" and row.source == "cli" and row.driver_token == "tok"
    assert (row.planned_folds, row.folds_done, row.current_fold) == (3, 3, None)
    assert row.expert == "FMPRating" and row.strategy == "S2" and row.band == "large"
    assert row.spec["optimize_args"] == _spec().optimize_args and row.spec["top_n"] == 2
    rep = row.report
    assert rep["final"] is True and rep["verdict"] == "PASS" and rep["refit_job"] == "cell-refit"
    assert [f["state"] for f in rep["folds"]] == ["done", "done", "done"]
    assert [f["train_optimization_id"] for f in rep["folds"]] == [101, 102, 103]
    assert rep["folds"][1]["generations"] == "15" and rep["folds"][1]["seeded_from"] == "cell-wf1"
    f1 = rep["folds"][0]
    assert (f1["train_start"], f1["train_end"], f1["test_start"], f1["test_end"]) == (
        "2020-01-01", "2023-12-31", "2024-01-01", "2024-12-31")
    assert [g["rank"] for g in f1["ranks"]] == [1, 2]
    from dataclasses import asdict
    want = [asdict(g) for g in res.walk_forward.genomes if g.fold == 1]
    assert f1["ranks"] == json.loads(json.dumps(want))
    with open(stored / "cell" / "cell.json", encoding="utf-8") as fh:
        cell = json.load(fh)
    assert rep["winner_stitch"] == cell["winner_stitch"]
    assert row.stitched_return == pytest.approx(cell["winner_stitch"]["total_return"])
    assert row.worst_oos_dd == pytest.approx(cell["winner_stitch"]["worst_fold_dd"])
    assert row.stitched_trades == cell["winner_stitch"]["trades"]
    assert row.completed_at is not None and row.error_message is None


def test_row_shows_progress_while_the_chain_runs(tmp_path, wfdb):
    seen = {}

    def look(fold):
        r = _row(wfdb)
        seen[fold.index] = (r.status, r.current_fold, r.folds_done,
                            [f["state"] for f in r.report["folds"]], r.report["stitch_partial"],
                            r.stitched_return)

    C.run_cell(_spec(), StoreRunner([GOOD, GOOD, GOOD], wfdb, on_oos=look), str(tmp_path),
               log=lambda *_: None)
    assert seen[1] == ("running", 1, 0, ["running", "planned", "planned"], False, None)
    st, cur, done, states, partial, ret = seen[2]
    assert (st, cur, done, states, partial) == ("running", 2, 1, ["done", "running", "planned"], True)
    assert ret == pytest.approx(5.0)                   # fold 1's winner, stitched so far


def test_two_phase_deferred_then_tested_updates_the_same_row(tmp_path, wfdb):
    phase_a = _spec(data_end=D("2025-12-31"), defer_last_test=True)
    r = StoreRunner([GOOD, GOOD, GOOD], wfdb)
    res = C.run_cell(phase_a, r, str(tmp_path), log=lambda *_: None)
    assert res.verdict == "PENDING"
    row = _row(wfdb)
    first_id = row.id
    assert row.status == "deferred-pending" and (row.folds_done, row.planned_folds) == (2, 3)
    assert [f["state"] for f in row.report["folds"]] == ["done", "done", "deferred"]
    assert row.report["deferred"]["fold"] == 3 and row.report["refit_job"] is None
    assert row.spec["data_end"] == "2025-12-31" and row.spec["defer_last_test"] is True
    # phase B: the same command with the full data; every train job is completed
    phase_b = _spec(defer_last_test=True)
    again = StoreRunner([GOOD, GOOD, GOOD], wfdb, completed={"cell-wf1", "cell-wf2", "cell-wf3"})
    assert C.run_cell(phase_b, again, str(tmp_path), log=lambda *_: None).verdict == "PASS"
    row = _row(wfdb)
    assert row.id == first_id and row.status == "pass" and row.folds_done == 3
    assert [f["state"] for f in row.report["folds"]] == ["done", "done", "done"]
    assert row.report["deferred"] is None and row.spec["data_end"] is None


def test_eliminated_folds_are_marked_and_the_verdict_is_fail(tmp_path, wfdb):
    spec = _spec(thresholds=WF.Thresholds(0.0, 1.5, 3), require_test_years=())
    C.run_cell(spec, StoreRunner([BAD, GOOD, GOOD], wfdb), str(tmp_path), log=lambda *_: None)
    row = _row(wfdb)
    assert row.status == "fail"
    assert [f["state"] for f in row.report["folds"]] == ["done", "eliminated", "eliminated"]
    assert row.report["eliminated"]["before_fold"] == 2


def test_refit_off_passes_without_training_the_refit(tmp_path, wfdb):
    r = StoreRunner([GOOD, GOOD, GOOD], wfdb)
    res = C.run_cell(_spec(refit=False), r, str(tmp_path), log=lambda *_: None)
    assert res.verdict == "PASS" and res.refit_job is None
    assert ("train", "cell-refit") not in r.events
    assert _row(wfdb).status == "pass" and _row(wfdb).report["refit_job"] is None


class BoomStore(StoreRunner):
    def run_train(self, cmd, name):
        if name == "cell-wf2":
            raise WF.WalkForwardError("training job 'cell-wf2' exited with code 1")
        super().run_train(cmd, name)


def test_a_chain_error_is_recorded_on_the_row_and_re_raised(tmp_path, wfdb):
    with pytest.raises(WF.WalkForwardError, match="exited with code 1"):
        C.run_cell(_spec(), BoomStore([GOOD, GOOD, GOOD], wfdb), str(tmp_path), log=lambda *_: None)
    row = _row(wfdb)
    assert row.status == "error" and "exited with code 1" in row.error_message
    assert [f["state"] for f in row.report["folds"]] == ["done", "running", "planned"]
    assert (tmp_path / "cell" / "fold1.json").exists()


def test_a_failed_db_write_is_loud(tmp_path, wfdb):
    class Broken:
        def __init__(self):
            self.db = wfdb()

        def __getattr__(self, n):
            return getattr(self.db, n)

        def commit(self):
            raise RuntimeError("disk I/O error")

    r = StoreRunner([GOOD, GOOD, GOOD], wfdb)
    r.Sf = Broken
    with pytest.raises(RuntimeError, match="disk I/O error"):
        C.run_cell(_spec(), r, str(tmp_path), log=lambda *_: None)
    assert r.trained == []                             # nothing ran without its row


def test_a_live_driver_row_is_not_taken_over_and_a_dead_one_is(tmp_path, wfdb):
    from app.models.walk_forward_run import WalkForwardRun
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ident = R.process_identity(live.pid)
        db = wfdb()
        db.add(WalkForwardRun(name="cell", source="ui", status="running", driver_token="other",
                              driver_host=ident["host"], driver_pid=ident["pid"],
                              driver_pid_started=ident["pid_started"],
                              driver_identity=ident["proc_identity"]))
        db.commit()
        db.close()
        r = StoreRunner([GOOD, GOOD, GOOD], wfdb)
        with pytest.raises(WF.WalkForwardError, match="another driver"):
            C.run_cell(_spec(), r, str(tmp_path), log=lambda *_: None)
        row = _row(wfdb)
        assert (row.status, row.driver_token, row.driver_pid) == ("running", "other", live.pid)
        assert r.trained == []
    finally:
        live.kill()
        live.wait()
    # the same row once its driver is dead: taken over, run to the end
    C.run_cell(_spec(), StoreRunner([GOOD, GOOD, GOOD], wfdb), str(tmp_path), log=lambda *_: None)
    row = _row(wfdb)
    assert row.status == "pass" and row.driver_token == "tok" and row.source == "ui"


def test_spec_json_round_trips_to_the_same_cell_spec():
    spec = _spec(data_end=D("2025-12-31"), defer_last_test=True, floor=D("2022-01-01"),
                 thresholds=WF.Thresholds(0.0, 2.5, 2, 35.0), refit=False)
    back = S.spec_from_json({**S.spec_to_json(spec), "name": "cell"}, "ba2-test")
    assert back == spec
    with pytest.raises(WF.WalkForwardError, match="no stored optimize args"):
        S.spec_from_json({**S.spec_to_json(spec), "name": "cell", "optimize_args": None}, "x")


# ------------------------------------------------------------------ import


def _grid(tmp_path):
    """A grid report dir like reports/goal2027_wf: a passing cell, a pending one, an ERROR one."""
    out = tmp_path / "goal2027_wf"
    ok = _spec(name="okcell")
    pend = _spec(name="pendcell", data_end=D("2025-12-31"), defer_last_test=True)
    boom = _spec(name="boomcell")

    def make(spec):
        if spec.name == "boomcell":
            class B(IdRunner):
                def run_train(self, cmd, name):
                    if name == "boomcell-wf3":
                        raise WF.WalkForwardError("training job 'boomcell-wf3' exited with code 2")
                    super().run_train(cmd, name)
            return B([GOOD, GOOD, GOOD], prefix=spec.name)
        return IdRunner([GOOD, GOOD, GOOD], prefix=spec.name)

    C.run_cells([ok, pend, boom], make, str(out), log=lambda *_: None)
    return out


def test_import_loads_a_grid_dir_and_is_idempotent(tmp_path, wfdb):
    out = _grid(tmp_path)
    got = dict(S.import_report_dir(str(out), wfdb))
    assert got == {"okcell": "created", "pendcell": "created", "boomcell": "created"}
    ok, pend, boom = _row(wfdb, "okcell"), _row(wfdb, "pendcell"), _row(wfdb, "boomcell")
    assert ok.source == "import" and ok.status == "pass" and ok.folds_done == 3
    assert ok.report["refit_job"] == "okcell-refit" and ok.spec["optimize_args"] is None
    assert [f["train_optimization_id"] for f in ok.report["folds"]] == [101, 102, 103]
    with open(out / "okcell" / "cell.json", encoding="utf-8") as fh:
        assert ok.report["winner_stitch"] == json.load(fh)["winner_stitch"]
    assert pend.status == "deferred-pending"
    assert [f["state"] for f in pend.report["folds"]] == ["done", "done", "deferred"]
    assert boom.status == "error" and "exited with code 2" in boom.error_message
    assert boom.folds_done == 2 and boom.report["folds"][0]["ranks"]
    # again: nothing changes
    assert set(dict(S.import_report_dir(str(out), wfdb)).values()) == {"unchanged"}
    assert _row(wfdb, "okcell").id == ok.id
    # a changed file is picked up
    p = out / "okcell" / "cell.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["band"] = "mid"
    p.write_text(json.dumps(doc), encoding="utf-8")
    assert dict(S.import_report_dir(str(out), wfdb))["okcell"] == "updated"
    assert _row(wfdb, "okcell").band == "mid"


def test_import_never_overwrites_a_live_row_unless_asked(tmp_path, wfdb):
    out = _grid(tmp_path)
    C.run_cell(_spec(name="okcell"), StoreRunner([GOOD, GOOD, GOOD], wfdb, prefix="okcell"),
               str(tmp_path / "live"), log=lambda *_: None)
    got = dict(S.import_report_dir(str(out), wfdb))
    assert got["okcell"].startswith("skipped") and _row(wfdb, "okcell").source == "cli"
    assert dict(S.import_report_dir(str(out), wfdb, replace_live=True))["okcell"] == "updated"
    assert _row(wfdb, "okcell").source == "import"


def test_import_of_one_chain_dir_and_of_a_plain_walk_forward_report(tmp_path, wfdb):
    out = _grid(tmp_path)
    assert S.import_report_dir(str(out / "okcell"), wfdb) == [("okcell", "created")]
    # tools/run_walk_forward.py writes report.json + report.md only
    plan = WF.make_plan("plainwf", WF.build_year_folds(D("2020-01-01"), [2024, 2025]),
                        ["--expert", "FMPRating"], "ba2-test", 2, WF.Thresholds(0.0, 1.5, 1),
                        test_parallel=1, skip_train=False, only_fold=None, embargo_days=0,
                        selection={})
    r = ChainRunner([GOOD, GOOD, GOOD], prefix="plainwf")
    res = WF.run_walk_forward(plan, r, log=lambda *_: None)
    WF.write_report(res, str(tmp_path / "plainwf"))
    assert S.import_report_dir(str(tmp_path / "plainwf"), wfdb) == [("plainwf", "created")]
    row = _row(wfdb, "plainwf")
    assert row.status == "pass" and row.folds_done == 2
    assert row.stitched_return == pytest.approx(res.winner_stitch.total_return)


def test_import_dry_run_writes_nothing(tmp_path, wfdb):
    out = _grid(tmp_path)
    assert set(dict(S.import_report_dir(str(out), wfdb, dry_run=True)).values()) == {"created"}
    assert _row(wfdb, "okcell") is None
