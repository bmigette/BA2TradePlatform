"""Walk-forward UI review fixes: forbidden optimize flags vs argparse abbreviations (measured against the
REAL launcher parser), names made of dots, the importer's per-chain errors and 'incomplete' status, the
plan size of an imported ERROR cell, no overall pass while PENDING, and kill_tree on AccessDenied."""
from __future__ import annotations

import argparse
import json
import os

import pytest

from app.services import walk_forward as WF
from app.services import wf_chain as C
from app.services import wf_runs as R
from app.services import wf_store as S
from tests.test_walk_forward import D
from tests.test_wf_matrix_chain import GOOD, ChainRunner, _spec

BASE = ["--expert", "FMPRating", "--universe", "AAPL", "--start", "2020-01-01", "--end", "2020-12-31"]


class _Parsed(Exception):
    pass


@pytest.fixture(scope="module")
def optimize_parser():
    """The launcher's REAL ``optimize`` sub-parser, captured from ``main`` without running anything."""
    import ba2test_launcher as L
    real = argparse.ArgumentParser.parse_args
    cwd = os.getcwd()

    def capture(self, args=None, namespace=None):
        raise _Parsed(self)

    argparse.ArgumentParser.parse_args = capture
    try:
        L.main(["optimize"])
    except _Parsed as e:
        top = e.args[0]
    finally:
        argparse.ArgumentParser.parse_args = real
        os.chdir(cwd)
    sub = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction))
    return sub.choices["optimize"]


def _accepts(parser, tokens):
    """The parsed namespace when the real parser accepts ``tokens``, else None."""
    try:
        return parser.parse_args(tokens)
    except SystemExit:
        return None


# ------------------------------------------------------------------ item 4: abbreviations


def test_the_real_optimize_parser_accepts_abbreviations_of_forbidden_flags(optimize_parser):
    """The evidence: argparse's default allow_abbrev lets these through as the forbidden flags."""
    assert optimize_parser.allow_abbrev is True
    assert _accepts(optimize_parser, BASE + ["--warm-start-f", "7"]).warm_start_from == 7
    assert _accepts(optimize_parser, BASE + ["--subm"]).submit is True
    assert _accepts(optimize_parser, BASE + ["--rer"]).rerun is True
    assert _accepts(optimize_parser, BASE + ["--chain-seed-f", "x"]).chain_seed_from == "x"
    assert _accepts(optimize_parser, BASE + ["--resume-int"]).resume_interrupted is True


def test_every_abbreviation_the_parser_resolves_to_a_forbidden_flag_is_refused(optimize_parser):
    """Every prefix the parser resolves to a forbidden flag is refused by the check (and the check
    refuses the remaining prefixes too, which the parser rejects as ambiguous anyway)."""
    resolved = 0
    for flag in WF.FORBIDDEN_OPTIMIZE_FLAGS:
        action = optimize_parser._option_string_actions[flag]
        for n in range(3, len(flag) + 1):
            abbr = flag[:n]
            if abbr in WF.EXACT_FLAGS_SHADOWING_FORBIDDEN:
                continue        # --seed: an exact real flag (the RNG seed), which argparse prefers
            toks = [abbr] if action.nargs == 0 else [abbr, "2019-06-30" if flag in ("--start", "--end") else "7"]
            # (BASE has --start/--end/--name already: a later abbreviation overrides them)
            ns = _accepts(optimize_parser, BASE + toks)
            if ns is not None and getattr(ns, action.dest) not in (None, False, "2020-01-01", "2020-12-31"):
                resolved += 1
            with pytest.raises(WF.WalkForwardError, match="must not contain"):
                WF.check_user_optimize_args(toks)
            with pytest.raises(WF.WalkForwardError, match="must not contain"):
                WF.check_user_optimize_args([f"{abbr}=7"])
    assert resolved > 20                                # the parser really accepts many of them


def test_no_valid_optimize_flag_is_refused(optimize_parser):
    flags = sorted({s for a in optimize_parser._actions for s in a.option_strings if s.startswith("--")})
    allowed = [f for f in flags if f not in WF.FORBIDDEN_OPTIMIZE_FLAGS and f != "--help"]
    assert len(allowed) > 50
    for f in allowed:
        WF.check_user_optimize_args([f, "x"])           # no exact flag is a prefix of a forbidden one
    WF.check_user_optimize_args(["--", "-5", "S2", "--population=300"])


# ------------------------------------------------------------------ item 6: names


@pytest.mark.parametrize("bad", [".", "..", "...", "a/b", ""])
def test_names_of_only_dots_or_with_a_path_are_refused(bad):
    with pytest.raises(WF.WalkForwardError, match="--prefix"):
        C.make_cell_plan(_spec(name=bad))


@pytest.mark.parametrize("ok", ["fr-large-S2-goal2027-dspin-time-a1b2c3d4", "cell", "a.b", ".x", "x.."])
def test_grid_style_names_stay_valid(ok):
    C.make_cell_plan(_spec(name=ok))


# ------------------------------------------------------------------ item 7: kill_tree


def test_kill_tree_survives_access_denied(monkeypatch):
    import psutil

    class Proc:
        pid = 4242

        def __init__(self, pid):
            pass

        def children(self, recursive=False):
            raise psutil.AccessDenied(4242)

        def terminate(self):
            raise psutil.AccessDenied(4242)

        def kill(self):
            raise psutil.AccessDenied(4242)

    monkeypatch.setattr(psutil, "Process", Proc)
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout: ([], list(procs)))
    R.kill_tree(4242)                                   # logged, not raised


# ------------------------------------------------------------------ item 7: importer


@pytest.fixture
def wfdb(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.database import Base
    import app.models  # noqa: F401
    eng = create_engine(f"sqlite:///{tmp_path / 'wf.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield sessionmaker(bind=eng)
    eng.dispose()


def _row(Sf, name):
    from app.models.walk_forward_run import WalkForwardRun
    db = Sf()
    try:
        return db.query(WalkForwardRun).filter(WalkForwardRun.name == name).first()
    finally:
        db.close()


def _grid(tmp_path):
    out = tmp_path / "grid"

    class Boom(ChainRunner):
        def run_train(self, cmd, name):
            if name == "errcell-wf3":
                raise WF.WalkForwardError("training job 'errcell-wf3' exited with code 1")
            super().run_train(cmd, name)

    runners = {"okcell": ChainRunner([GOOD] * 3, prefix="okcell"),
               "errcell": Boom([GOOD] * 3, prefix="errcell"),
               "badcell": ChainRunner([GOOD] * 3, prefix="badcell")}
    C.run_cells([_spec(name=n) for n in runners], lambda s: runners[s.name], str(out),
                log=lambda *_: None)
    return out


def test_a_corrupt_fold_file_is_an_import_error_and_the_others_import(tmp_path, wfdb):
    out = _grid(tmp_path)
    (out / "badcell" / "fold2.json").write_text("{not json", encoding="utf-8")
    (out / "okcell2").mkdir()
    (out / "okcell2" / "fold1.json").write_text(json.dumps({"genomes": []}), encoding="utf-8")
    got = dict(S.import_report_dir(str(out), wfdb))
    assert got["okcell"] == "created" and got["errcell"] == "created"
    assert got["badcell"].startswith("error (") and "not imported" in got["badcell"]
    assert got["okcell2"].startswith("error (KeyError")
    assert _row(wfdb, "badcell") is None and _row(wfdb, "okcell").status == "pass"


def test_the_import_tool_exits_non_zero_after_a_corrupt_chain(tmp_path, capsys):
    import importlib.util
    path = os.path.join(R.REPO_ROOT, "tools", "import_wf_reports.py")
    spec = importlib.util.spec_from_file_location("import_wf_reports", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    out = _grid(tmp_path)
    (out / "badcell" / "fold1.json").write_text("{", encoding="utf-8")
    assert tool.main([str(out)]) == 2
    printed = capsys.readouterr().out
    assert "error (" in printed and "created" in printed


def test_an_imported_error_cell_has_an_unknown_plan_size_and_a_partial_stitch(tmp_path, wfdb):
    out = _grid(tmp_path)
    S.import_report_dir(str(out), wfdb)
    row = _row(wfdb, "errcell")
    assert row.status == "error" and row.folds_done == 2
    assert row.planned_folds is None and row.report["planned_folds_known"] is False
    assert row.stitch_partial is True and row.stitched_return == pytest.approx(WF.compound([5.0, 5.0]))
    ok = _row(wfdb, "okcell")
    assert ok.planned_folds == 3 and ok.stitch_partial is False


def test_files_with_no_verdict_and_no_summary_entry_import_as_incomplete(tmp_path, wfdb):
    out = _grid(tmp_path)
    lone = tmp_path / "lone"
    lone.mkdir()
    for f in ("fold1.json", "fold2.json"):
        (lone / f).write_text((out / "errcell" / f).read_text(encoding="utf-8"), encoding="utf-8")
    assert S.import_report_dir(str(lone), wfdb) == [("lone", "created")]
    row = _row(wfdb, "lone")
    assert row.status == "incomplete" and row.report["verdict"] is None and row.folds_done == 2


def test_a_pending_chain_never_reports_an_overall_pass(tmp_path, wfdb):
    spec = _spec(data_end=D("2025-12-31"), defer_last_test=True, require_test_years=())
    C.run_cell(spec, ChainRunner([GOOD] * 3), str(tmp_path), log=lambda *_: None)
    S.import_report_dir(str(tmp_path / "cell"), wfdb)
    row = _row(wfdb, "cell")
    assert row.status == "deferred-pending" and row.report["overall_passed"] is None
    rep = S.build_report(name="x", windows=[], genomes=[], states={},
                         cell={"verdict": "PENDING"}, walk_forward={"overall_passed": True})
    assert rep["overall_passed"] is None


def test_no_optimize_flag_but_seed_itself_is_a_prefix_of_seed(optimize_parser):
    """``user_sets_seed`` (restarts refuse a user GA seed) reads any prefix of --seed as --seed: that
    is only safe while no other optimize flag is such a prefix (--seed-genomes etc. are longer)."""
    flags = {s for a in optimize_parser._actions for s in a.option_strings if s.startswith("--")}
    assert [f for f in flags if "--seed".startswith(f)] == ["--seed"]
    assert not any(WF.user_sets_seed([f, "x"]) for f in flags if f != "--seed")
    for abbr in ("--see", "--seed"):                       # what the real parser does with them
        assert _accepts(optimize_parser, BASE + [abbr, "7"]) is None or abbr == "--seed"
