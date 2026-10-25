"""Walk-forward engine extensions for goal2027 (item 2.5): the seeding hook, the no-look-ahead
assertion, per-fold optimize args, ``--pass-require-fold``. Pure logic + the fake runner of
test_walk_forward; nothing is optimized."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import walk_forward as WF
from tests.test_walk_forward import D, FakeRunner, SEL, TH, _oos, _pick

FOLDS = WF.build_year_folds(D("2020-01-01"), [2024, 2025, 2026])


def _plan(**kw):
    args = dict(test_parallel=1, skip_train=False, only_fold=None, embargo_days=0, selection=SEL)
    args.update(kw)
    return WF.make_plan("cell", FOLDS, ["--expert", "FMPRating"], "ba2-test", 2, TH, **args)


# ------------------------------------------------------------------ seeding hook


def test_fold_one_has_no_seed_and_later_folds_seed_from_the_previous_job():
    assert WF.initial_population_for_fold(FOLDS[0]) is None
    assert WF.initial_population_for_fold(FOLDS[0], None, "cell") is None
    seed = WF.initial_population_for_fold(FOLDS[1], FOLDS[0], "cell")
    assert seed.source_job == "cell-wf1" and seed.source_train_end == D("2023-12-31")


def test_seed_that_could_have_seen_the_test_year_is_refused():
    leaky = WF.Fold(1, D("2020-01-01"), D("2024-06-30"), D("2024-07-01"), D("2024-12-31"))
    nxt = WF.Fold(2, D("2020-01-01"), D("2024-12-31"), D("2025-01-01"), D("2025-12-31"))
    # previous train ends before ITS test start but AFTER this fold's test start: impossible for
    # ordered folds, so build the violation directly
    bad_prev = WF.Fold(1, D("2020-01-01"), D("2025-03-31"), D("2025-04-01"), D("2025-06-30"))
    with pytest.raises(WF.WalkForwardError, match="look-ahead"):
        WF.initial_population_for_fold(nxt, bad_prev, "cell")
    WF.initial_population_for_fold(nxt, leaky, "cell")       # a legal predecessor


def test_train_command_renders_the_chain_seed_only_when_chaining():
    cmd1 = WF.train_command("ba2-test", ["--expert", "X"], "cell", FOLDS[1], prev=FOLDS[0],
                            seed_chain=True, extra_args=["--generations", "15"])
    assert cmd1 == ["ba2-test", "optimize", "--expert", "X", "--generations", "15",
                    "--chain-seed-from", "cell-wf1", "--start", "2020-01-01", "--end",
                    "2024-12-31", "--name", "cell-wf2"]
    plain = WF.train_command("ba2-test", ["--expert", "X"], "cell", FOLDS[1], prev=FOLDS[0])
    assert "--chain-seed-from" not in plain
    first = WF.train_command("ba2-test", [], "cell", FOLDS[0], prev=None, seed_chain=True)
    assert "--chain-seed-from" not in first


def test_per_fold_args_and_chain_reach_the_run_commands():
    plan = _plan(seed_chain=True, fold_extra_args={1: ["--generations", "30"],
                                                   2: ["--generations", "15"],
                                                   3: ["--generations", "15"]})
    picks = {f"cell-wf{k}": [_pick(1), _pick(2)] for k in (1, 2, 3)}
    oos = {(k, r): _oos(10.0, -10.0) for k in (1, 2, 3) for r in (1, 2)}
    r = FakeRunner(is_picks=picks, oos=oos)
    WF.run_walk_forward(plan, r, log=lambda *_: None)
    cmds = {n: c for n, c in r.trained}
    assert "--chain-seed-from" not in cmds["cell-wf1"]
    assert cmds["cell-wf1"][cmds["cell-wf1"].index("--generations") + 1] == "30"
    assert cmds["cell-wf2"][cmds["cell-wf2"].index("--chain-seed-from") + 1] == "cell-wf1"
    assert cmds["cell-wf3"][cmds["cell-wf3"].index("--chain-seed-from") + 1] == "cell-wf2"
    assert cmds["cell-wf3"][cmds["cell-wf3"].index("--generations") + 1] == "15"


def test_only_fold_with_chain_needs_the_previous_job_completed():
    plan = _plan(seed_chain=True, only_fold=2)
    with pytest.raises(WF.WalkForwardError, match="cell-wf1"):
        WF.run_walk_forward(plan, FakeRunner(), log=lambda *_: None)


def test_dry_run_shows_the_seed_flag():
    out = WF.render_dry_run(_plan(seed_chain=True))
    assert "--chain-seed-from cell-wf1" in out and "--chain-seed-from cell-wf2" in out


# ------------------------------------------------------------------ pass rule


def _result(oos_by_fold, th=TH, require=()):
    genomes = [WF.evaluate_genome(f, _pick(1), o, th) for f, o in zip(FOLDS, oos_by_fold)]
    return WF.aggregate("p", FOLDS, 3, 1, genomes, th, require_test_years=require)


GOOD, BAD = _oos(5.0, -10.0), _oos(-5.0, -10.0)


def test_required_fold_must_pass_even_when_min_folds_is_met():
    ok = _result([GOOD, GOOD, BAD])                        # 2 of 3 pass
    assert ok.overall_passed is True
    req = _result([GOOD, GOOD, BAD], require=(2026,))
    assert req.overall_passed is False and req.required_failed == [2026]
    assert _result([BAD, GOOD, GOOD], require=(2026,)).overall_passed is True


def test_required_folds_are_repeatable_and_all_must_pass():
    assert _result([GOOD, BAD, GOOD], require=(2024, 2026)).overall_passed is True
    r = _result([GOOD, BAD, GOOD], require=(2025, 2026))
    assert r.overall_passed is False and r.required_failed == [2025]


def test_required_fold_with_zero_oos_trades_fails():
    r = _result([GOOD, GOOD, _oos(50.0, -1.0, trades=0)], require=(2026,))
    assert r.overall_passed is False


def test_require_year_not_in_the_plan_is_refused_up_front():
    with pytest.raises(WF.WalkForwardError, match="2027"):
        _plan(require_test_years=(2027,))


def test_partial_run_has_no_verdict_even_with_required_folds():
    r = WF.aggregate("p", FOLDS[:1], 3, 1, [WF.evaluate_genome(FOLDS[0], _pick(1), GOOD, TH)], TH,
                     require_test_years=(2026,))
    assert r.overall_passed is None


def test_report_states_required_folds(tmp_path):
    res = _result([GOOD, GOOD, BAD], require=(2026,))
    jp, mp = WF.write_report(res, str(tmp_path))
    md = Path(mp).read_text(encoding="utf-8")
    assert "`--pass-require-fold` 2026" in md and "FAIL" in md
    data = json.loads(Path(jp).read_text(encoding="utf-8"))
    assert data["require_test_years"] == [2026] and data["required_failed"] == [2026]
    assert data["thresholds"] == {"min_oos_return": 0.0, "max_dd_mult": 1.5, "min_folds": 2}


def test_run_walk_forward_passes_the_plan_requirement_through():
    plan = _plan(require_test_years=(2026,))
    picks = {f"cell-wf{k}": [_pick(1), _pick(2)] for k in (1, 2, 3)}
    oos = {(k, r): (BAD if k == 3 else GOOD) for k in (1, 2, 3) for r in (1, 2)}
    res = WF.run_walk_forward(plan, FakeRunner(is_picks=picks, oos=oos), log=lambda *_: None)
    assert res.overall_passed is False and res.required_failed == [2026]


# ------------------------------------------------------------------ lineage is checked, not assumed


class _Seeded(FakeRunner):
    def __init__(self, recorded, **kw):
        super().__init__(**kw)
        self.recorded = recorded

    def seed_lineage(self, name):
        return self.recorded.get(name)


def _chain_inputs():
    picks = {f"cell-wf{k}": [_pick(1), _pick(2)] for k in (1, 2, 3)}
    oos = {(k, r): _oos(10.0, -10.0) for k in (1, 2, 3) for r in (1, 2)}
    return dict(is_picks=picks, oos=oos)


def test_recorded_lineage_matching_the_plan_is_accepted():
    r = _Seeded({"cell-wf1": None, "cell-wf2": "cell-wf1", "cell-wf3": "cell-wf2"},
                **_chain_inputs())
    WF.run_walk_forward(_plan(seed_chain=True), r, log=lambda *_: None)


def test_a_job_that_did_not_record_its_seed_stops_the_chain():
    r = _Seeded({"cell-wf1": None, "cell-wf2": None}, **_chain_inputs())     # wf2 ran cold
    with pytest.raises(WF.WalkForwardError, match="cell-wf2.*recorded seed source None"):
        WF.run_walk_forward(_plan(seed_chain=True), r, log=lambda *_: None)


def test_cli_dry_run_renders_chain_seed_and_required_fold(capsys):
    import importlib.util
    from tests.test_walk_forward import _tool
    tool = _tool()
    rc = tool.main(["--prefix", "wfx", "--train-start", "2020-01-01", "--test-years",
                    "2024,2025,2026", "--top-n", "2", "--pass-min-oos-return", "0",
                    "--pass-max-dd-mult", "1.5", "--pass-min-folds", "2", "--pass-require-fold",
                    "2026", "--chain-seed", "--launcher", "ba2-test", "--dry-run", "--",
                    "--expert", "FMPRating"], runner=FakeRunner())
    out = capsys.readouterr().out
    assert rc == 0
    assert "--chain-seed-from wfx-wf1 --start 2020-01-01 --end 2024-12-31 --name wfx-wf2" in out
    assert "the 2026 fold(s) MUST pass" in out


def test_cli_refuses_a_required_year_outside_the_plan(capsys):
    from tests.test_walk_forward import _tool
    rc = _tool().main(["--prefix", "wfx", "--train-start", "2020-01-01", "--test-years", "2024",
                       "--top-n", "2", "--pass-min-oos-return", "0", "--pass-max-dd-mult", "1.5",
                       "--pass-min-folds", "1", "--pass-require-fold", "2031", "--dry-run", "--",
                       "--expert", "FMPRating"], runner=FakeRunner())
    assert rc == 2 and "2031" in capsys.readouterr().err


# ------------------------------------------------------------------ strict (exclusive) return rule


def test_exclusive_return_needs_a_return_strictly_above_the_threshold():
    f = FOLDS[0]
    flat = _oos(0.0, -5.0)
    assert WF.evaluate_genome(f, _pick(1), flat, TH).passed is True               # >= (unchanged)
    g = WF.evaluate_genome(f, _pick(1), flat, TH, exclusive_return=True)
    assert g.passed is False and "<=" in g.fail_reasons[0]
    assert WF.evaluate_genome(f, _pick(1), _oos(0.01, -5.0), TH, exclusive_return=True).passed


def test_exclusive_flag_reaches_the_plan_the_verdict_and_the_report(tmp_path):
    plan = _plan(exclusive_return=True)
    picks = {f"cell-wf{k}": [_pick(1), _pick(2)] for k in (1, 2, 3)}
    oos = {(k, r): _oos(0.0, -5.0) for k in (1, 2, 3) for r in (1, 2)}
    res = WF.run_walk_forward(plan, FakeRunner(is_picks=picks, oos=oos), log=lambda *_: None)
    assert res.overall_passed is False and res.exclusive_return is True
    _jp, mp = WF.write_report(res, str(tmp_path))
    assert "--pass-min-oos-return-exclusive" in Path(mp).read_text(encoding="utf-8")


def test_every_job_of_a_chain_is_told_to_persist_its_population():
    first = WF.train_command("ba2-test", [], "p", FOLDS[0], prev=None, seed_chain=True)
    assert "--persist-final-population" in first and "--chain-seed-from" not in first
    later = WF.train_command("ba2-test", [], "p", FOLDS[1], prev=FOLDS[0], seed_chain=True)
    assert "--chain-seed-from" in later and "--persist-final-population" not in later   # implied
    assert "--persist-final-population" not in WF.train_command("ba2-test", [], "p", FOLDS[0])
