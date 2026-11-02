"""goal2027 item 2.5: one cell's walk-forward chain (folds -wf1..-wf3, then -refit for a passing
cell), resume by name, the fold-3 data-end clamp, the per-cell report and the grid summary.
Pure orchestration over a fake runner; no optimization, no DB."""
from __future__ import annotations

import json

import pytest

from app.services import walk_forward as WF
from app.services import wf_chain as C
from tests.test_walk_forward import D, FakeRunner, SEL, _oos, _pick

TH2 = WF.Thresholds(0.0, 1.5, 2)
BASE = ["--expert", "FMPRating", "--strategy", "S2", "--population", "200"]


# ------------------------------------------------------------------ folds


def test_default_goal2027_folds():
    folds = C.build_cell_folds(D("2020-01-01"), [2024, 2025, 2026], None)
    assert [(f.train_start, f.train_end, f.test_start, f.test_end) for f in folds] == [
        (D("2020-01-01"), D("2023-12-31"), D("2024-01-01"), D("2024-12-31")),
        (D("2020-01-01"), D("2024-12-31"), D("2025-01-01"), D("2025-12-31")),
        (D("2020-01-01"), D("2025-12-31"), D("2026-01-01"), D("2026-12-31"))]
    assert C.refit_window(folds) == (D("2020-01-01"), D("2026-12-31"))


def test_expert_floor_moves_the_train_start_of_every_fold_and_the_refit():
    folds = C.build_cell_folds(D("2020-01-01"), [2024, 2025, 2026], None, floor=D("2022-01-01"))
    assert {f.train_start for f in folds} == {D("2022-01-01")}
    assert folds[0].train_end == D("2023-12-31")           # fold 1 trains 2022-2023
    assert C.refit_window(folds)[0] == D("2022-01-01")
    # a floor EARLIER than the requested start changes nothing
    early = C.build_cell_folds(D("2023-01-01"), [2024], None, floor=D("2022-01-01"))
    assert early[0].train_start == D("2023-01-01")


def test_data_end_clamps_the_last_test_window_and_the_refit():
    folds = C.build_cell_folds(D("2020-01-01"), [2024, 2025, 2026], D("2026-10-12"))
    assert folds[-1].test_end == D("2026-10-12") and folds[0].test_end == D("2024-12-31")
    assert C.refit_window(folds)[1] == D("2026-10-12")


def test_data_end_after_the_last_year_changes_nothing():
    folds = C.build_cell_folds(D("2020-01-01"), [2024, 2025, 2026], D("2027-03-01"))
    assert folds[-1].test_end == D("2026-12-31")


@pytest.mark.parametrize("end", ["2025-12-31", "2026-01-01", "2025-06-30"])
def test_data_end_that_leaves_an_empty_test_window_is_refused(end):
    with pytest.raises(WF.WalkForwardError, match="empty"):
        C.build_cell_folds(D("2020-01-01"), [2024, 2025, 2026], D(end))


def test_floor_that_leaves_no_training_data_is_refused():
    with pytest.raises(WF.WalkForwardError):
        C.build_cell_folds(D("2020-01-01"), [2024], None, floor=D("2024-06-01"))


# ------------------------------------------------------------------ chain


class ChainRunner(FakeRunner):
    """FakeRunner whose train jobs and out-of-sample runs are recorded in order."""

    def __init__(self, oos_by_fold, prefix="cell", **kw):
        picks = {f"{prefix}-wf{k}": [_pick(1), _pick(2)] for k in (1, 2, 3)}
        oos = {(k, r): oos_by_fold[k - 1] for k in (1, 2, 3) for r in (1, 2)}
        super().__init__(is_picks=picks, oos=oos, **kw)
        self.events = []

    def run_train(self, cmd, name):
        self.events.append(("train", name))
        super().run_train(cmd, name)

    def run_oos(self, train_name, fold, picks, name_prefix, labels, parallel, data_args=None):
        self.events.append(("oos", train_name))
        return super().run_oos(train_name, fold, picks, name_prefix, labels, parallel)


GOOD, BAD = _oos(5.0, -10.0), _oos(-5.0, -10.0)


def _spec(**kw):
    a = dict(name="cell", optimize_args=BASE, launcher="ba2-test", train_start=D("2020-01-01"),
             test_years=[2024, 2025, 2026], data_end=None, first_generations=30,
             next_generations=15, top_n=2, thresholds=TH2, require_test_years=(2026,),
             test_parallel=1, selection=SEL, expert="FMPRating", strategy="S2", band="large")
    a.update(kw)
    return C.CellSpec(**a)


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_passing_cell_runs_folds_in_order_then_refit(tmp_path):
    r = ChainRunner([GOOD, GOOD, GOOD])
    res = C.run_cell(_spec(), r, str(tmp_path), log=lambda *_: None)
    assert r.events == [
        ("train", "cell-wf1"), ("oos", "cell-wf1"), ("train", "cell-wf2"), ("oos", "cell-wf2"),
        ("train", "cell-wf3"), ("oos", "cell-wf3"), ("train", "cell-refit")]
    cmds = dict(r.trained)
    assert "--chain-seed-from" not in cmds["cell-wf1"]
    gens = [_arg(cmds[n], "--generations") for n in ("cell-wf1", "cell-wf2", "cell-wf3", "cell-refit")]
    assert gens == ["30", "15", "15", "15"]
    assert _arg(cmds["cell-wf2"], "--chain-seed-from") == "cell-wf1"
    assert _arg(cmds["cell-wf3"], "--chain-seed-from") == "cell-wf2"
    rf = cmds["cell-refit"]
    assert _arg(rf, "--chain-seed-from") == "cell-wf3"
    assert _arg(rf, "--start") == "2020-01-01" and _arg(rf, "--end") == "2026-12-31"
    assert _arg(rf, "--name") == "cell-refit"
    assert res.verdict == "PASS" and res.refit_job == "cell-refit"


def test_failing_cell_gets_no_refit(tmp_path):
    r = ChainRunner([GOOD, GOOD, BAD])            # 2026 required and failed
    res = C.run_cell(_spec(), r, str(tmp_path), log=lambda *_: None)
    assert res.verdict == "FAIL" and res.refit_job is None
    assert ("train", "cell-refit") not in r.events
    assert res.walk_forward.required_failed == [2026]


class Boom(ChainRunner):
    def run_train(self, cmd, name):
        if name == "cell-wf3":
            raise WF.WalkForwardError("training job 'cell-wf3' exited with code 1")
        super().run_train(cmd, name)


def test_resume_skips_completed_folds_and_reuses_their_stored_report(tmp_path):
    spec = _spec()
    with pytest.raises(WF.WalkForwardError):
        C.run_cell(spec, Boom([GOOD, GOOD, GOOD]), str(tmp_path), log=lambda *_: None)
    assert (tmp_path / "cell" / "fold1.json").exists() and (tmp_path / "cell" / "fold2.json").exists()
    assert not (tmp_path / "cell" / "fold3.json").exists()
    # relaunch: the jobs of folds 1-2 are completed in the DB
    again = ChainRunner([GOOD, GOOD, GOOD], completed={"cell-wf1", "cell-wf2"})
    res = C.run_cell(spec, again, str(tmp_path), log=lambda *_: None)
    assert again.events == [("train", "cell-wf3"), ("oos", "cell-wf3"), ("train", "cell-refit")]
    assert res.verdict == "PASS"
    assert [g.fold for g in res.walk_forward.genomes if g.rank == 1] == [1, 2, 3]


def test_completed_train_without_stored_report_is_re_evaluated_not_retrained(tmp_path):
    r = ChainRunner([GOOD, GOOD, GOOD],
                    completed={"cell-wf1", "cell-wf2", "cell-wf3", "cell-refit"})
    res = C.run_cell(_spec(), r, str(tmp_path), log=lambda *_: None)
    assert r.trained == []                                    # nothing retrained
    assert [e[0] for e in r.events] == ["oos", "oos", "oos"]  # OOS rows are reused by the real runner
    assert res.verdict == "PASS" and res.refit_job == "cell-refit"


def test_stale_fold_report_with_other_thresholds_is_not_trusted(tmp_path):
    C.run_cell(_spec(), ChainRunner([GOOD, GOOD, GOOD]), str(tmp_path), log=lambda *_: None)
    r = ChainRunner([BAD, BAD, BAD], completed={"cell-wf1", "cell-wf2", "cell-wf3", "cell-refit"})
    res = C.run_cell(_spec(thresholds=WF.Thresholds(10.0, 1.5, 2)), r, str(tmp_path),
                     log=lambda *_: None)
    # re-evaluated, cache ignored (fold 3 is never reached: 0 of 2 passed, the cell is eliminated)
    assert [e[0] for e in r.events] == ["oos", "oos"]
    assert res.verdict == "FAIL"


def test_a_train_that_never_completes_stops_the_chain(tmp_path):
    class NoWf1(ChainRunner):
        def train_completed(self, name):
            return False if name == "cell-wf1" else super().train_completed(name)

        def run_train(self, cmd, name):           # fold 1 'finishes' but never reports completed
            self.trained.append((name, cmd))
    with pytest.raises(WF.WalkForwardError):
        C.run_cell(_spec(), NoWf1([GOOD, GOOD, GOOD]), str(tmp_path), log=lambda *_: None)


def test_data_end_flows_into_the_last_fold_and_the_refit(tmp_path):
    r = ChainRunner([GOOD, GOOD, GOOD])
    C.run_cell(_spec(data_end=D("2026-10-12")), r, str(tmp_path), log=lambda *_: None)
    assert _arg(dict(r.trained)["cell-wf3"], "--end") == "2025-12-31"
    assert r.tested[-1][1] == 3
    assert _arg(dict(r.trained)["cell-refit"], "--end") == "2026-10-12"


# ------------------------------------------------------------------ reports


class LineageRunner(ChainRunner):
    def seed_lineage(self, name):
        return {"cell-wf1": None, "cell-wf2": "cell-wf1", "cell-wf3": "cell-wf2",
                "cell-refit": "cell-wf3"}[name]


def test_cell_report_contents(tmp_path):
    C.run_cell(_spec(), LineageRunner([GOOD, GOOD, GOOD]), str(tmp_path), log=lambda *_: None)
    cell = json.loads((tmp_path / "cell" / "cell.json").read_text(encoding="utf-8"))
    assert cell["cell"] == "cell" and cell["verdict"] == "PASS" and cell["refit_job"] == "cell-refit"
    assert (cell["expert"], cell["strategy"], cell["band"]) == ("FMPRating", "S2", "large")
    assert [f["job"] for f in cell["folds"]] == ["cell-wf1", "cell-wf2", "cell-wf3"]
    f1 = cell["folds"][0]
    assert f1["train"] == ["2020-01-01", "2023-12-31"] and f1["test"] == ["2024-01-01", "2024-12-31"]
    assert f1["is"]["car"] == 25.0 and f1["oos"]["return"] == 5.0 and f1["passed"] is True
    assert f1["seeded_from"] is None and cell["folds"][1]["seeded_from"] == "cell-wf1"
    assert cell["refit"] == {"job": "cell-refit", "train": ["2020-01-01", "2026-12-31"],
                             "seeded_from": "cell-wf3"}
    assert cell["thresholds"]["require_test_years"] == [2026]
    assert (tmp_path / "cell" / "report.json").exists() and (tmp_path / "cell" / "report.md").exists()


def test_failing_cell_report_has_no_refit_block(tmp_path):
    C.run_cell(_spec(), ChainRunner([GOOD, GOOD, BAD]), str(tmp_path), log=lambda *_: None)
    cell = json.loads((tmp_path / "cell" / "cell.json").read_text(encoding="utf-8"))
    assert cell["verdict"] == "FAIL" and cell["refit"] is None and cell["refit_job"] is None
    assert cell["required_failed"] == [2026]


def test_grid_summary_collects_every_cell_and_survives_one_failing_cell(tmp_path):
    class Fails(ChainRunner):
        def run_train(self, cmd, name):
            raise WF.WalkForwardError("training job exited with code 1")

    def make(sp):
        if sp.name == "boom":
            return Fails([GOOD, GOOD, GOOD], prefix="boom")
        return ChainRunner([GOOD, GOOD, BAD if sp.name == "bad" else GOOD], prefix=sp.name)

    specs = [_spec(name=n) for n in ("ok", "bad", "boom")]
    out = C.run_cells(specs, make, str(tmp_path), log=lambda *_: None)
    assert [c["verdict"] for c in out] == ["PASS", "FAIL", "ERROR"]
    summ = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert [c["cell"] for c in summ["cells"]] == ["ok", "bad", "boom"]
    assert summ["cells"][0]["refit_job"] == "ok-refit" and summ["cells"][1]["refit_job"] is None
    assert "training job exited" in summ["cells"][2]["error"]
    md = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "| ok | PASS" in md and "| bad | FAIL" in md and "| boom | ERROR" in md


def test_exclusive_return_flows_through_the_cell_and_its_stored_fold_key(tmp_path):
    flat = _oos(0.0, -5.0)
    r = ChainRunner([flat, flat, flat])
    res = C.run_cell(_spec(exclusive_return=True), r, str(tmp_path), log=lambda *_: None)
    assert res.verdict == "FAIL"
    cell = json.loads((tmp_path / "cell" / "cell.json").read_text(encoding="utf-8"))
    assert cell["thresholds"]["exclusive_return"] is True
    # a stored report made under the inclusive rule must not be trusted by the strict one
    C.run_cell(_spec(), ChainRunner([flat, flat, flat]), str(tmp_path / "b"), log=lambda *_: None)
    r2 = ChainRunner([flat, flat, flat], completed={"cell-wf1", "cell-wf2", "cell-wf3"})
    res2 = C.run_cell(_spec(exclusive_return=True), r2, str(tmp_path / "b"), log=lambda *_: None)
    assert res2.verdict == "FAIL" and [e[0] for e in r2.events] == ["oos", "oos"]   # eliminated


# ------------------------------------------------------------------ review fixes


def test_a_non_walk_forward_exception_in_one_cell_does_not_stop_the_grid(tmp_path, caplog):
    class Crash(ChainRunner):
        def select(self, train_name, top_n):
            raise RuntimeError("db is locked")

    def make(sp):
        if sp.name == "boom":
            return Crash([GOOD, GOOD, GOOD], prefix="boom")
        return ChainRunner([GOOD, GOOD, GOOD], prefix=sp.name)

    with caplog.at_level("ERROR"):
        out = C.run_cells([_spec(name="boom"), _spec(name="fine")], make, str(tmp_path),
                          log=lambda *_: None)
    assert [c["verdict"] for c in out] == ["ERROR", "PASS"]
    assert out[0]["error"] == "RuntimeError: db is locked"
    rec = [r for r in caplog.records if r.levelname == "ERROR" and "boom" in r.getMessage()]
    assert rec and rec[0].exc_info is not None


class Identified(ChainRunner):
    """Train jobs carry an optimization id + completion time; `ident` can be changed to simulate a retrain."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ident = {}

    def train_identity(self, name):
        return self.ident.get(name, {"id": 1, "completed_at": "2026-10-25T10:00:00"})


def test_rejected_stored_fold_report_is_logged_with_the_reason(tmp_path, caplog):
    C.run_cell(_spec(), Identified([GOOD, GOOD, GOOD]), str(tmp_path), log=lambda *_: None)
    (tmp_path / "cell" / "fold2.json").write_text("{not json", encoding="utf-8")
    done = {"cell-wf1", "cell-wf2", "cell-wf3", "cell-refit"}
    with caplog.at_level("WARNING"):
        C.run_cell(_spec(), Identified([GOOD, GOOD, GOOD], completed=done), str(tmp_path),
                   log=lambda *_: None)
    msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("cell" in m and "fold2.json" in m and "unreadable" in m for m in msgs)


def test_a_retrained_fold_invalidates_its_own_and_every_later_stored_report(tmp_path, caplog):
    C.run_cell(_spec(), Identified([GOOD, GOOD, GOOD]), str(tmp_path), log=lambda *_: None)
    done = {"cell-wf1", "cell-wf2", "cell-wf3", "cell-refit"}
    r = Identified([GOOD, GOOD, GOOD], completed=done)
    r.ident = {"cell-wf2": {"id": 9, "completed_at": "2026-10-26T09:00:00"}}   # wf2 was retrained
    with caplog.at_level("WARNING"):
        C.run_cell(_spec(), r, str(tmp_path), log=lambda *_: None)
    # fold 1 is still trusted; folds 2 and 3 (seeded from the retrained job) are re-evaluated
    assert [e for e in r.events if e[0] == "oos"] == [("oos", "cell-wf2"), ("oos", "cell-wf3")]
    assert any("key mismatch" in x.getMessage() for x in caplog.records)


# ------------------------------------------------------------------ deferred last test (train now, test later)


def test_defer_keeps_every_fold_whole_and_marks_the_last_one():
    folds = C.build_cell_folds(D("2020-01-01"), [2024, 2025, 2026], D("2025-12-31"),
                               defer_last_test=True)
    assert folds[-1].test_end == D("2026-12-31")               # never clamped: the test runs later
    assert C.deferred_fold(folds, D("2025-12-31"), True).index == 3
    assert C.deferred_fold(folds, None, True) is None          # data reaches the test: run it
    assert C.deferred_fold(folds, D("2025-12-31"), False) is None


def test_defer_with_a_data_end_inside_the_last_test_year_is_refused():
    with pytest.raises(WF.WalkForwardError, match="inside the last test year"):
        C.build_cell_folds(D("2020-01-01"), [2024, 2025, 2026], D("2026-10-12"), defer_last_test=True)


def test_defer_is_refused_when_the_deferred_fold_cannot_train_yet():
    with pytest.raises(WF.WalkForwardError, match="trains to 2025-12-31"):
        C.build_cell_folds(D("2020-01-01"), [2024, 2025, 2026], D("2025-06-30"), defer_last_test=True)


def test_defer_of_a_single_fold_plan_is_refused():
    with pytest.raises(WF.WalkForwardError, match="nothing would be tested"):
        C.build_cell_folds(D("2020-01-01"), [2026], D("2025-12-31"), defer_last_test=True)


def test_without_defer_the_old_refusal_names_the_flag():
    with pytest.raises(WF.WalkForwardError, match="--wf-defer-last-test"):
        C.build_cell_folds(D("2020-01-01"), [2024, 2025, 2026], D("2025-12-31"))


def test_deferred_cell_trains_the_last_fold_tests_nothing_and_is_pending(tmp_path):
    r = LineageRunner([GOOD, GOOD, GOOD])
    res = C.run_cell(_spec(data_end=D("2025-12-31"), defer_last_test=True), r, str(tmp_path),
                     log=lambda *_: None)
    assert r.events == [("train", "cell-wf1"), ("oos", "cell-wf1"), ("train", "cell-wf2"),
                        ("oos", "cell-wf2"), ("train", "cell-wf3")]
    assert _arg(dict(r.trained)["cell-wf3"], "--chain-seed-from") == "cell-wf2"
    assert res.verdict == "PENDING" and res.refit_job is None
    cell = json.loads((tmp_path / "cell" / "cell.json").read_text(encoding="utf-8"))
    assert cell["verdict"] == "PENDING" and cell["refit"] is None
    assert [f["fold"] for f in cell["folds"]] == [1, 2]
    assert cell["deferred"] == {"fold": 3, "job": "cell-wf3", "train": ["2020-01-01", "2025-12-31"],
                                "test": ["2026-01-01", "2026-12-31"], "seeded_from": "cell-wf2"}
    assert not (tmp_path / "cell" / "fold3.json").exists()


def test_january_rerun_of_a_deferred_cell_only_tests_wf3_then_refits(tmp_path):
    C.run_cell(_spec(data_end=D("2025-12-31"), defer_last_test=True), ChainRunner([GOOD, GOOD, GOOD]),
               str(tmp_path), log=lambda *_: None)
    jan = ChainRunner([GOOD, GOOD, GOOD], completed={"cell-wf1", "cell-wf2", "cell-wf3"})
    res = C.run_cell(_spec(data_end=None, defer_last_test=True), jan, str(tmp_path), log=lambda *_: None)
    assert jan.events == [("oos", "cell-wf3"), ("train", "cell-refit")]
    assert res.verdict == "PASS"
    assert _arg(dict(jan.trained)["cell-refit"], "--end") == "2026-12-31"


def test_a_deferred_wf3_still_needs_wf2_completed(tmp_path):
    class NoWf2(ChainRunner):
        def train_completed(self, name):
            return False if name == "cell-wf2" else super().train_completed(name)
    r = NoWf2([GOOD, GOOD, GOOD], completed={"cell-wf1"})
    with pytest.raises(WF.WalkForwardError):
        C.run_cell(_spec(data_end=D("2025-12-31"), defer_last_test=True), r, str(tmp_path),
                   log=lambda *_: None)


# ------------------------------------------------------------------ early elimination


def test_a_cell_that_can_no_longer_pass_stops_before_the_next_fold(tmp_path):
    r = ChainRunner([BAD, BAD, GOOD])          # 0 of 2 passed, 1 left, 2 needed
    res = C.run_cell(_spec(), r, str(tmp_path), log=lambda *_: None)
    assert r.events == [("train", "cell-wf1"), ("oos", "cell-wf1"), ("train", "cell-wf2"),
                        ("oos", "cell-wf2")]
    assert res.verdict == "FAIL" and res.refit_job is None
    cell = json.loads((tmp_path / "cell" / "cell.json").read_text(encoding="utf-8"))
    assert cell["eliminated"]["before_fold"] == 3 and "0 fold(s) passed" in cell["eliminated"]["reason"]
    assert [f["fold"] for f in cell["folds"]] == [1, 2]


def test_elimination_also_skips_training_a_deferred_fold(tmp_path):
    r = ChainRunner([BAD, BAD, GOOD])
    res = C.run_cell(_spec(data_end=D("2025-12-31"), defer_last_test=True), r, str(tmp_path),
                     log=lambda *_: None)
    assert ("train", "cell-wf3") not in r.events and res.verdict == "FAIL"


def test_a_failed_required_fold_stops_the_chain_at_once(tmp_path):
    r = ChainRunner([BAD, GOOD, GOOD])
    res = C.run_cell(_spec(require_test_years=(2024, 2026)), r, str(tmp_path), log=lambda *_: None)
    assert r.events == [("train", "cell-wf1"), ("oos", "cell-wf1")]
    assert res.verdict == "FAIL"
    assert "2024" in json.loads((tmp_path / "cell" / "cell.json").read_text(encoding="utf-8")
                                )["eliminated"]["reason"]


def test_one_failed_fold_with_enough_left_carries_on(tmp_path):
    r = ChainRunner([BAD, GOOD, GOOD])
    assert C.run_cell(_spec(), r, str(tmp_path), log=lambda *_: None).verdict == "PASS"


def test_run_all_folds_disables_elimination(tmp_path):
    r = ChainRunner([BAD, BAD, GOOD])
    res = C.run_cell(_spec(run_all_folds=True), r, str(tmp_path), log=lambda *_: None)
    assert ("oos", "cell-wf3") in r.events and res.verdict == "FAIL"
    cell = json.loads((tmp_path / "cell" / "cell.json").read_text(encoding="utf-8"))
    assert cell["eliminated"] is None and len(cell["folds"]) == 3


def test_summary_marks_pending_and_eliminated_cells(tmp_path):
    def make(sp):
        return ChainRunner([BAD, BAD, GOOD] if sp.name == "gone" else [GOOD, GOOD, GOOD],
                           prefix=sp.name)
    specs = [_spec(name=n, data_end=D("2025-12-31"), defer_last_test=True) for n in ("wait", "gone")]
    C.run_cells(specs, make, str(tmp_path), log=lambda *_: None)
    md = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "| wait | PENDING" in md and "fold 3 test deferred" in md
    assert "| gone | FAIL" in md and "eliminated before fold 3" in md
