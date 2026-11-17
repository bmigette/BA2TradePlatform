"""Walk-forward redesign (2026-11-17): restarts (K independent searches per fold, the best training
fitness wins), no chain seeding, the set-score verdict (the whole tested genome set, scored like the
CAR fitness) and dropping folds with too little training data. Pure logic over fake runners; nothing
is optimized. The defaults are pinned separately (test_wf_golden_default)."""
from __future__ import annotations

import json
import logging
import math

import pytest

from app.services import strategy_fitness as SF
from app.services import walk_forward as WF
from app.services import wf_chain as C
from app.services import wf_store as S
from tests.test_walk_forward import D, FakeRunner, SEL, TH, _oos, _pick

GOOD, BAD = _oos(5.0, -10.0), _oos(-5.0, -10.0)
FOLDS = WF.build_year_folds(D("2020-01-01"), [2024, 2025, 2026])


# ------------------------------------------------------------------ fakes


class SearchRunner(FakeRunner):
    """FakeRunner whose train jobs carry a best fitness and record a seed source (cold by default)."""

    def __init__(self, fitness=None, seeds=None, oos_by_fold=(GOOD, GOOD, GOOD), prefix="cell",
                 restarts=3, **kw):
        names = [WF.restart_job_name(prefix, k, j) for k in range(1, 6) for j in range(1, restarts + 1)]
        picks = {n: [_pick(1), _pick(2)] for n in names}
        oos = {(k, r): oos_by_fold[k - 1] for k in range(1, len(oos_by_fold) + 1) for r in (1, 2)}
        super().__init__(is_picks=picks, oos=oos, **kw)
        self.fitness = dict(fitness or {})
        self.seeds = dict(seeds or {})
        self.events = []
        self.fitness_reads = []

    def run_train(self, cmd, name):
        self.events.append(("train", name))
        super().run_train(cmd, name)

    def run_oos(self, train_name, fold, picks, name_prefix, labels, parallel, data_args=None):
        self.events.append(("oos", train_name))
        return super().run_oos(train_name, fold, picks, name_prefix, labels, parallel)

    def train_best_fitness(self, name):
        self.fitness_reads.append(name)
        return self.fitness.get(name, 1.0)

    def seed_lineage(self, name):
        return self.seeds.get(name)


def _plan(**kw):
    a = dict(test_parallel=1, skip_train=False, only_fold=None, embargo_days=0, selection=SEL)
    a.update(kw)
    return WF.make_plan("cell", FOLDS, ["--expert", "FMPRating"], "ba2-test", 2, TH, **a)


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


# ------------------------------------------------------------------ restart names, seeds, refusals


def test_restart_one_keeps_the_train_job_name_and_later_ones_get_a_suffix():
    assert WF.restart_job_name("cell", 2, 1) == "cell-wf2" == WF.train_job_name("cell", 2)
    assert WF.restart_job_name("cell", 2, 3) == "cell-wf2-r3"
    assert C.refit_restart_name("cell", 1) == "cell-refit" == C.refit_job_name("cell")
    assert C.refit_restart_name("cell", 2) == "cell-refit-r2"


def test_restart_one_command_is_unchanged_and_later_restarts_get_their_own_ga_seed():
    one = WF.train_command("ba2-test", ["--expert", "X"], "cell", FOLDS[0], restart=1)
    assert one == WF.train_command("ba2-test", ["--expert", "X"], "cell", FOLDS[0])
    assert "--seed" not in one
    two = WF.train_command("ba2-test", ["--expert", "X"], "cell", FOLDS[0], restart=2)
    three = WF.train_command("ba2-test", ["--expert", "X"], "cell", FOLDS[0], restart=3)
    assert _arg(two, "--name") == "cell-wf1-r2" and _arg(three, "--name") == "cell-wf1-r3"
    s2, s3 = int(_arg(two, "--seed")), int(_arg(three, "--seed"))
    assert s2 == WF.restart_seed("cell-wf1-r2") and s3 == WF.restart_seed("cell-wf1-r3")
    # distinct from each other and from the launcher's own --seed default (42, ba2test_launcher.py)
    assert len({s2, s3, 42}) == 3
    assert 0 <= s2 < 2 ** 31


@pytest.mark.parametrize("kw,match", [
    (dict(restarts=0), "--wf-restarts"),
    (dict(restarts=2, seed_chain=True), "--wf-no-chain-seed"),
    (dict(verdict_mode="set-score"), "--pass-min-oos-score"),
    (dict(min_oos_score=0.0), "--pass-min-oos-score"),
    (dict(verdict_mode="best"), "--wf-verdict"),
    (dict(verdict_mode="set-score", min_oos_score=float("nan")), "--pass-min-oos-score"),
])
def test_bad_restart_or_verdict_settings_are_refused_up_front(kw, match):
    with pytest.raises(WF.WalkForwardError, match=match):
        _plan(**kw)


def test_restarts_refuse_a_user_ga_seed():
    with pytest.raises(WF.WalkForwardError, match="--seed"):
        WF.make_plan("cell", FOLDS, ["--expert", "X", "--seed=7"], "ba2-test", 2, TH,
                     test_parallel=1, skip_train=False, only_fold=None, embargo_days=0,
                     selection=SEL, restarts=2)


def test_choose_search_takes_the_best_training_fitness_and_the_lowest_restart_on_a_tie():
    assert WF.choose_search([5.0, 7.0, 7.0]) == 2
    assert WF.choose_search([7.0, 7.0]) == 1
    assert WF.choose_search([-3.0]) == 1
    with pytest.raises(WF.MissingMetric):
        WF.choose_search([1.0, None])
    with pytest.raises(WF.MissingMetric):
        WF.choose_search([1.0, float("nan")])


# ------------------------------------------------------------------ restarts in a fold


def test_every_restart_trains_and_only_the_winner_is_selected_and_tested():
    r = SearchRunner(fitness={"cell-wf1": 6.4, "cell-wf1-r2": 36.7, "cell-wf1-r3": 19.2})
    plan = _plan(restarts=3, only_fold=1)
    WF.run_walk_forward(plan, r, log=lambda *_: None)
    assert [n for n, _ in r.trained] == ["cell-wf1", "cell-wf1-r2", "cell-wf1-r3"]
    assert [e for e in r.events if e[0] == "oos"] == [("oos", "cell-wf1-r2")]
    assert [t[0] for t in r.tested] == ["cell-wf1-r2"]
    cmds = dict(r.trained)
    assert all("--chain-seed-from" not in c and "--persist-final-population" not in c
               for c in cmds.values())


def test_a_resumed_fold_trains_only_the_missing_restart():
    r = SearchRunner(fitness={"cell-wf1": 9.0, "cell-wf1-r2": 3.0}, completed={"cell-wf1"})
    WF.run_walk_forward(_plan(restarts=2, only_fold=1), r, log=lambda *_: None)
    assert [n for n, _ in r.trained] == ["cell-wf1-r2"]
    assert [t[0] for t in r.tested] == ["cell-wf1"]           # restart 1 has the better fitness


def test_restarts_need_a_runner_that_reads_the_training_fitness():
    class NoFitness(FakeRunner):
        pass
    r = NoFitness(is_picks={"cell-wf1": [_pick(1)]}, oos={(1, 1): GOOD})
    with pytest.raises(WF.WalkForwardError, match="train_best_fitness"):
        WF.run_walk_forward(_plan(restarts=2, only_fold=1), r, log=lambda *_: None)


def test_the_winner_choice_never_reads_test_data():
    r = SearchRunner(fitness={"cell-wf1": 2.0, "cell-wf1-r2": 1.0})
    seen = []
    real = r.run_oos

    def watch(*a, **k):
        seen.append(list(r.fitness_reads))
        return real(*a, **k)
    r.run_oos = watch
    WF.run_walk_forward(_plan(restarts=2, only_fold=1), r, log=lambda *_: None)
    assert seen == [["cell-wf1", "cell-wf1-r2"]]              # every fitness read happened before the test


def test_dry_run_lists_every_restart_command():
    out = WF.render_dry_run(_plan(restarts=2))
    assert "--name cell-wf1-r2" in out and "--name cell-wf3-r2" in out and "restart 2" in out


# ------------------------------------------------------------------ lineage without chain seeding


def test_an_unchained_plan_refuses_a_job_that_recorded_a_seed():
    r = SearchRunner(seeds={"cell-wf2": "cell-wf1"})
    with pytest.raises(WF.WalkForwardError, match="cell-wf2.*expected None"):
        WF.run_walk_forward(_plan(), r, log=lambda *_: None)
    WF.run_walk_forward(_plan(), SearchRunner(), log=lambda *_: None)       # cold jobs pass


# ------------------------------------------------------------------ the set score


def _genome(fold, rank, ret, dd=-10.0, trades=10):
    return WF.GenomeFold(fold=fold, rank=rank, is_fitness=1.0, is_return=50.0, is_car=20.0,
                         is_max_dd=-15.0, is_trades=40, oos_return=ret, oos_car=ret, oos_max_dd=dd,
                         oos_trades=trades, oos_backtest_id=None, efficiency=None, passed=ret > 0,
                         fail_reasons=[])


def test_fold_set_statistics_cover_every_tested_genome():
    gs = [_genome(1, 1, 10.0, -8.0, 12), _genome(1, 2, -4.0, -20.0, 30), _genome(1, 3, 6.0, -5.0, 8),
          _genome(1, 4, 20.0, -12.0, 20)]
    st = WF.fold_set_stats(FOLDS[0], gs)
    assert st["n"] == 4
    assert st["oos_return_median"] == 8.0 and st["oos_return_mean"] == 8.0
    assert (st["oos_return_min"], st["oos_return_max"]) == (-4.0, 20.0)
    assert st["share_positive"] == 0.75
    assert st["oos_max_dd_median"] == 10.0 and st["oos_max_dd_worst"] == 20.0     # |dd|
    assert st["oos_trades_median"] == 16.0
    assert st["full_year"] is True and st["annual_return"] == 8.0             # a calendar year: as is


def test_a_partial_test_window_is_annualised():
    half = WF.Fold(1, D("2020-01-01"), D("2025-12-31"), D("2026-01-01"), D("2026-06-30"))
    st = WF.fold_set_stats(half, [_genome(1, 1, 10.0)])
    assert st["full_year"] is False
    # 180 days = 0.4928 years: 1.10 ** (365.25 / 180) - 1 = 21.34 %/yr
    assert st["annual_return"] == pytest.approx((1.10 ** (365.25 / 180) - 1) * 100)
    assert st["annual_return"] == pytest.approx(21.337, abs=1e-3)


def _stats(annual, dd):
    return [{"annual_return": a, "oos_max_dd_median": d} for a, d in zip(annual, dd)]


def test_set_score_uses_the_car_fitness_constants():
    assert (WF._CAR_DD_REFERENCE, WF._CAR_DD_FLOOR, WF._CAR_DD_GUARD_MAX, WF._CAR_CONSISTENCY_FLOOR) == (
        SF._CAR_DD_REFERENCE, SF._CAR_DD_FLOOR, SF._CAR_DD_GUARD_MAX, SF._CAR_CONSISTENCY_FLOOR)


def test_set_score_even_years():
    # base 20, worst/mean 10/20 = 0.5, dd_guard 20/10 = 2.0 -> 20 x 2.0 x 0.5 = 20
    s = WF.set_score(_stats([10.0, 20.0, 30.0], [5.0, 10.0, 8.0]))
    assert (s["base"], s["consistency"], s["dd_guard"], s["score"]) == (20.0, 0.5, 2.0, 20.0)
    assert s["annual_returns"] == [10.0, 20.0, 30.0] and s["worst_fold_median_dd"] == 10.0


def test_a_negative_year_hits_the_consistency_floor_but_does_not_block():
    # mean 15, worst -5: -5/15 = -0.33 -> 0.25; dd_guard 20/25 = 0.8 -> 15 x 0.8 x 0.25 = 3.0
    s = WF.set_score(_stats([-5.0, 20.0, 30.0], [10.0, 25.0, 12.0]))
    assert s["consistency"] == 0.25 and s["dd_guard"] == pytest.approx(0.8)
    assert s["score"] == pytest.approx(3.0)


def test_a_negative_base_is_returned_unfactored():
    s = WF.set_score(_stats([-10.0, 5.0], [40.0, 40.0]))
    assert s["base"] == -2.5 and s["score"] == -2.5


def test_a_single_fold_has_consistency_one():
    s = WF.set_score(_stats([12.0], [30.0]))
    assert s["consistency"] == 1.0 and s["dd_guard"] == pytest.approx(20 / 30)
    assert s["score"] == pytest.approx(8.0)


@pytest.mark.parametrize("dd,guard", [(5.0, 2.0), (0.5, 2.0), (10.0, 2.0), (40.0, 0.5)])
def test_the_drawdown_guard_is_capped_and_floored(dd, guard):
    assert WF.set_score(_stats([10.0, 10.0], [dd, 1.0]))["dd_guard"] == pytest.approx(guard)


def test_set_score_matches_the_fitness_on_the_same_inputs():
    """The fitness's own consistency helper and dd guard formula give the same factors."""
    annual = [12.0, -3.0, 25.0]
    s = WF.set_score(_stats(annual, [14.0, 9.0, 22.0]))
    assert s["consistency"] == SF._consistency_factor(annual)
    assert s["dd_guard"] == min(SF._CAR_DD_REFERENCE / max(22.0, SF._CAR_DD_FLOOR), SF._CAR_DD_GUARD_MAX)


def _set_result(rets_by_fold, x, folds=FOLDS, planned=3):
    genomes = [_genome(f.index, r, ret) for f, rets in zip(folds, rets_by_fold)
               for r, ret in enumerate(rets, start=1)]
    return WF.aggregate("p", folds, planned, 3, genomes, TH, verdict_mode="set-score",
                        min_oos_score=x)


def test_the_set_verdict_is_strictly_above_the_minimum():
    # medians 10 / 10 / 10, dd 10: score = 10 x 2.0 x 1.0 = 20
    res = _set_result([[10.0, 5.0, 15.0]] * 3, 20.0)
    assert res.set_score["score"] == pytest.approx(20.0) and res.overall_passed is False
    assert _set_result([[10.0, 5.0, 15.0]] * 3, 19.99).overall_passed is True


def test_the_set_verdict_ignores_the_rank_one_rule():
    # every rank-1 loses, the set median is positive in every year
    res = _set_result([[-1.0, 8.0, 9.0]] * 3, 0.0)
    assert res.winners_passed == 0 and res.overall_passed is True


def test_a_partial_set_run_has_no_verdict():
    res = _set_result([[10.0, 5.0, 15.0]] * 2, 0.0, folds=FOLDS[:2], planned=3)
    assert res.overall_passed is None and res.set_score["complete"] is False


def test_set_score_reports(tmp_path):
    res = _set_result([[10.0, 5.0, 15.0], [-4.0, -2.0, 1.0], [20.0, 30.0, 25.0]], 0.0)
    jp, mp = WF.write_report(res, str(tmp_path))
    data = json.loads(open(jp, encoding="utf-8").read())
    assert data["verdict_mode"] == "set-score" and data["min_oos_score"] == 0.0
    ss = data["set_score"]
    assert ss["annual_returns"] == [10.0, -2.0, 25.0] and len(ss["folds"]) == 3
    assert {"base", "dd_guard", "consistency", "score"} <= set(ss)
    for key in ("prefix", "thresholds", "winners_passed", "winner_stitch", "genomes", "folds"):
        assert key in data                                  # every existing key is kept
    md = open(mp, encoding="utf-8").read()
    assert "set-score" in md and "consistency" in md and "dd_guard" in md


def test_the_winner_report_has_no_set_keys(tmp_path):
    genomes = [WF.evaluate_genome(f, _pick(1), GOOD, TH) for f in FOLDS]
    jp, _ = WF.write_report(WF.aggregate("p", FOLDS, 3, 1, genomes, TH), str(tmp_path))
    data = json.loads(open(jp, encoding="utf-8").read())
    assert not {"verdict_mode", "set_score", "min_oos_score"} & set(data)


# ------------------------------------------------------------------ the chain: no seeding


def _spec(**kw):
    a = dict(name="cell", optimize_args=["--expert", "FMPRating", "--strategy", "S2"],
             launcher="ba2-test", train_start=D("2020-01-01"), test_years=[2024, 2025, 2026],
             data_end=None, first_generations=30, next_generations=15, top_n=2,
             thresholds=WF.Thresholds(0.0, 1.5, 2), require_test_years=(2026,), test_parallel=1,
             selection=SEL, expert="FMPRating", strategy="S2", band="large")
    a.update(kw)
    return C.CellSpec(**a)


def _read(tmp_path, *parts):
    return json.loads(tmp_path.joinpath(*parts).read_text(encoding="utf-8"))


def test_without_chain_seeding_every_job_starts_cold_on_the_first_budget(tmp_path):
    r = SearchRunner(restarts=1)
    res = C.run_cell(_spec(chain_seed=False), r, str(tmp_path), log=lambda *_: None)
    assert res.verdict == "PASS"
    cmds = dict(r.trained)
    assert list(cmds) == ["cell-wf1", "cell-wf2", "cell-wf3", "cell-refit"]
    for c in cmds.values():
        assert "--chain-seed-from" not in c and "--persist-final-population" not in c
        assert _arg(c, "--generations") == "30"
    cell = _read(tmp_path, "cell", "cell.json")
    assert [f["seeded_from"] for f in cell["folds"]] == [None, None, None]
    assert [f["generations"] for f in cell["folds"]] == ["30", "30", "30"]
    assert cell["refit"]["seeded_from"] is None and cell["chain_seed"] is False


def test_without_chain_seeding_a_seeded_fold_or_refit_is_refused(tmp_path):
    with pytest.raises(WF.WalkForwardError, match="cell-wf3.*expected None"):
        C.run_cell(_spec(chain_seed=False), SearchRunner(seeds={"cell-wf3": "cell-wf2"}),
                   str(tmp_path / "a"), log=lambda *_: None)
    with pytest.raises(WF.WalkForwardError, match="cell-refit.*expected None"):
        C.run_cell(_spec(chain_seed=False), SearchRunner(seeds={"cell-refit": "cell-wf3"}),
                   str(tmp_path / "b"), log=lambda *_: None)


def test_a_fold_stored_under_chain_seeding_is_not_reused_without_it(tmp_path, caplog):
    C.run_cell(_spec(), SearchRunner(seeds={"cell-wf2": "cell-wf1", "cell-wf3": "cell-wf2",
                                            "cell-refit": "cell-wf3"}),
               str(tmp_path), log=lambda *_: None)
    r = SearchRunner(completed={"cell-wf1", "cell-wf2", "cell-wf3", "cell-refit"})
    with caplog.at_level(logging.WARNING):
        C.run_cell(_spec(chain_seed=False), r, str(tmp_path), log=lambda *_: None)
    assert [e for e in r.events if e[0] == "oos"] == [("oos", f"cell-wf{k}") for k in (1, 2, 3)]
    assert any("key mismatch" in x.getMessage() for x in caplog.records)


# ------------------------------------------------------------------ the chain: restarts


FIT = {"cell-wf1": 6.4, "cell-wf1-r2": 19.2, "cell-wf2": 30.0, "cell-wf2-r2": 30.0,
       "cell-wf3": 1.0, "cell-wf3-r2": 2.0, "cell-refit": 5.0, "cell-refit-r2": 4.0}


def test_a_restart_cell_trains_every_search_and_tests_each_folds_winner(tmp_path):
    r = SearchRunner(fitness=FIT, restarts=2)
    res = C.run_cell(_spec(chain_seed=False, restarts=2), r, str(tmp_path), log=lambda *_: None)
    assert r.events == [
        ("train", "cell-wf1"), ("train", "cell-wf1-r2"), ("oos", "cell-wf1-r2"),
        ("train", "cell-wf2"), ("train", "cell-wf2-r2"), ("oos", "cell-wf2"),        # tie: restart 1
        ("train", "cell-wf3"), ("train", "cell-wf3-r2"), ("oos", "cell-wf3-r2"),
        ("train", "cell-refit"), ("train", "cell-refit-r2")]
    assert res.verdict == "PASS" and res.refit_job == "cell-refit"
    cell = _read(tmp_path, "cell", "cell.json")
    assert cell["restarts"] == 2
    assert [f["job"] for f in cell["folds"]] == ["cell-wf1-r2", "cell-wf2", "cell-wf3-r2"]
    f1 = cell["folds"][0]["restarts"]
    assert [(x["restart"], x["job"], x["best_fitness"], x["won"]) for x in f1] == [
        (1, "cell-wf1", 6.4, False), (2, "cell-wf1-r2", 19.2, True)]
    assert cell["refit"]["job"] == "cell-refit" and len(cell["refit"]["restarts"]) == 2
    fold1 = _read(tmp_path, "cell", "fold1.json")
    assert fold1["key"]["restarts"] == 2 and fold1["key"]["chain_seed"] is False
    assert [x["won"] for x in fold1["restarts"]] == [False, True]


def test_a_restart_cell_resumes_with_one_restart_done(tmp_path):
    r = SearchRunner(fitness=FIT, restarts=2, completed={"cell-wf1"})
    C.run_cell(_spec(chain_seed=False, restarts=2, require_test_years=()), r, str(tmp_path),
               log=lambda *_: None)
    assert r.events[:2] == [("train", "cell-wf1-r2"), ("oos", "cell-wf1-r2")]
    # a rerun with everything completed reuses every stored fold
    done = {n for n, _ in r.trained} | {"cell-wf1"}
    again = SearchRunner(fitness=FIT, restarts=2, completed=done)
    res = C.run_cell(_spec(chain_seed=False, restarts=2, require_test_years=()), again, str(tmp_path),
                     log=lambda *_: None)
    assert again.events == [] and res.verdict == "PASS"
    assert [f["job"] for f in res.report["folds"]] == ["cell-wf1-r2", "cell-wf2", "cell-wf3-r2"]


def test_restarts_with_chain_seeding_are_refused():
    with pytest.raises(WF.WalkForwardError, match="--wf-no-chain-seed"):
        C.make_cell_plan(_spec(restarts=2))


def test_a_deferred_fold_trains_every_restart_and_its_winner_is_tested_later(tmp_path):
    spec_a = _spec(chain_seed=False, restarts=2, data_end=D("2025-12-31"), defer_last_test=True)
    r = SearchRunner(fitness=FIT, restarts=2)
    res = C.run_cell(spec_a, r, str(tmp_path), log=lambda *_: None)
    assert res.verdict == "PENDING"
    assert ("train", "cell-wf3") in r.events and ("train", "cell-wf3-r2") in r.events
    assert not any(e == ("oos", "cell-wf3") or e == ("oos", "cell-wf3-r2") for e in r.events)
    assert [x["job"] for x in res.report["deferred"]["restarts"]] == ["cell-wf3", "cell-wf3-r2"]
    jan = SearchRunner(fitness=FIT, restarts=2, completed={n for n, _ in r.trained})
    res2 = C.run_cell(_spec(chain_seed=False, restarts=2, defer_last_test=True), jan, str(tmp_path),
                      log=lambda *_: None)
    assert [e for e in jan.events if e[0] == "oos"] == [("oos", "cell-wf3-r2")]
    assert res2.verdict == "PASS"


# ------------------------------------------------------------------ the chain: set-score verdict


def test_a_set_score_cell_runs_every_fold_and_is_judged_on_the_score(tmp_path):
    # rank 1 and 2 of every fold get the same result: fold medians -5, -5, 5
    r = SearchRunner(oos_by_fold=(BAD, BAD, GOOD), restarts=1)
    res = C.run_cell(_spec(chain_seed=False, verdict_mode="set-score", min_oos_score=0.0), r,
                     str(tmp_path), log=lambda *_: None)
    assert [e for e in r.events if e[0] == "oos"] == [("oos", f"cell-wf{k}") for k in (1, 2, 3)]
    assert res.verdict == "FAIL" and res.report["eliminated"] is None
    cell = _read(tmp_path, "cell", "cell.json")
    assert cell["verdict_mode"] == "set-score" and cell["min_oos_score"] == 0.0
    ss = cell["set_score"]
    assert ss["annual_returns"] == [-5.0, -5.0, 5.0] and ss["score"] == pytest.approx(-5.0 / 3)
    assert cell["folds"][0]["set"]["n"] == 2 and cell["folds"][0]["set"]["share_positive"] == 0.0
    md = (tmp_path / "cell" / "report.md").read_text(encoding="utf-8")
    assert "set-score" in md


def test_a_set_score_cell_with_one_losing_year_can_pass_and_refits(tmp_path):
    big = _oos(30.0, -10.0)
    r = SearchRunner(oos_by_fold=(big, BAD, big), restarts=1)
    res = C.run_cell(_spec(chain_seed=False, verdict_mode="set-score", min_oos_score=0.0), r,
                     str(tmp_path), log=lambda *_: None)
    # mean (30 - 5 + 30) / 3 = 18.33, consistency floor 0.25, dd_guard 2.0: 9.17 > 0
    assert res.report["set_score"]["score"] == pytest.approx(55 / 3 * 2.0 * 0.25)
    assert res.verdict == "PASS" and res.refit_job == "cell-refit"


def test_the_grid_summary_carries_the_set_score(tmp_path):
    def make(sp):
        return SearchRunner(oos_by_fold=(GOOD, GOOD, GOOD), restarts=1, prefix=sp.name)
    C.run_cells([_spec(name="s", chain_seed=False, verdict_mode="set-score", min_oos_score=0.0)],
                make, str(tmp_path), log=lambda *_: None)
    summ = _read(tmp_path, "summary.json")
    assert summ["cells"][0]["set_score"]["score"] == pytest.approx(10.0)
    md = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "set score" in md.lower() and "| s | PASS" in md and "10.00" in md


def test_a_winner_mode_summary_is_unchanged_by_a_five_fold_cell(tmp_path):
    spec = _spec(test_years=[2022, 2023, 2024, 2025, 2026], require_test_years=())
    r = SearchRunner(oos_by_fold=(GOOD,) * 5, seeds={f"cell-wf{k}": f"cell-wf{k - 1}" for k in range(2, 6)}
                     | {"cell-refit": "cell-wf5"}, restarts=1)
    C.run_cells([spec], lambda _s: r, str(tmp_path), log=lambda *_: None)
    md = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "| fold 5 |" in md                                # every fold has a column


# ------------------------------------------------------------------ min train years


def test_calendar_years_counts_whole_calendar_years_exactly():
    assert C.calendar_years(D("2022-01-01"), D("2023-12-31")) == 2.0
    assert C.calendar_years(D("2020-01-01"), D("2023-12-31")) == 4.0
    assert C.calendar_years(D("2022-01-01"), D("2022-12-31")) == 1.0
    assert C.calendar_years(D("2022-01-01"), D("2021-12-31")) == 0.0
    assert C.calendar_years(D("2022-07-01"), D("2023-12-31")) == pytest.approx(1 + 184 / 365.25)


def test_short_training_folds_are_dropped_and_the_rest_numbered_from_one():
    folds = C.build_cell_folds(D("2020-01-01"), [2022, 2023, 2024, 2025], None, floor=D("2022-01-01"),
                               min_train_years=2.0)
    assert [(f.index, f.test_start.year) for f in folds] == [(1, 2024), (2, 2025)]
    skipped = C.short_train_folds(D("2020-01-01"), [2022, 2023, 2024, 2025], D("2022-01-01"), 2.0)
    assert [(s["test_year"], s["train_years"]) for s in skipped] == [(2022, 0.0), (2023, 1.0)]


def test_without_min_train_years_nothing_is_dropped():
    assert C.short_train_folds(D("2020-01-01"), [2022, 2023], D("2022-01-01"), None) == []


def test_a_cell_left_with_no_fold_is_refused():
    with pytest.raises(WF.WalkForwardError, match="--wf-min-train-years"):
        C.build_cell_folds(D("2020-01-01"), [2022, 2023], None, floor=D("2022-01-01"),
                           min_train_years=5.0)


@pytest.mark.parametrize("y", [0.0, -1.0, float("nan")])
def test_a_meaningless_min_train_years_is_refused(y):
    with pytest.raises(WF.WalkForwardError, match="--wf-min-train-years"):
        C.build_cell_folds(D("2020-01-01"), [2024], None, min_train_years=y)


def test_the_cell_report_lists_the_dropped_folds(tmp_path, caplog):
    logs = []
    spec = _spec(test_years=[2022, 2023, 2024, 2025, 2026], floor=D("2022-01-01"),
                 min_train_years=2.0, chain_seed=False, require_test_years=())
    r = SearchRunner(restarts=1)
    res = C.run_cell(spec, r, str(tmp_path), log=logs.append)
    assert [n for n, _ in r.trained][:3] == ["cell-wf1", "cell-wf2", "cell-wf3"]
    assert _arg(dict(r.trained)["cell-wf1"], "--end") == "2023-12-31"     # fold 1 tests 2024
    cell = _read(tmp_path, "cell", "cell.json")
    assert cell["min_train_years"] == 2.0
    assert [s["test_year"] for s in cell["skipped_folds"]] == [2022, 2023]
    assert any("2022" in m and "dropped" in m for m in logs)
    assert res.verdict == "PASS"


# ------------------------------------------------------------------ DB store / spec


def test_spec_json_round_trips_the_new_settings_and_omits_them_at_defaults():
    spec = _spec(chain_seed=False, restarts=3, verdict_mode="set-score", min_oos_score=1.5,
                 min_train_years=2.0)
    js = S.spec_to_json(spec)
    assert (js["chain_seed"], js["restarts"], js["verdict_mode"], js["min_oos_score"],
            js["min_train_years"]) == (False, 3, "set-score", 1.5, 2.0)
    back = S.spec_from_json({**js, "name": "cell"}, "ba2-test")
    assert (back.chain_seed, back.restarts, back.verdict_mode, back.min_oos_score,
            back.min_train_years) == (False, 3, "set-score", 1.5, 2.0)
    plain = S.spec_to_json(_spec())
    assert not {"chain_seed", "restarts", "verdict_mode", "min_oos_score", "min_train_years"} & set(plain)
    old = S.spec_from_json({**plain, "name": "cell"}, "ba2-test")
    assert (old.chain_seed, old.restarts, old.verdict_mode, old.min_train_years) == (True, 1, "winner", None)


def test_the_imported_db_report_carries_restarts_and_the_set_score(tmp_path):
    r = SearchRunner(fitness=FIT, restarts=2)
    C.run_cell(_spec(chain_seed=False, restarts=2, verdict_mode="set-score", min_oos_score=0.0), r,
               str(tmp_path), log=lambda *_: None)
    _values, report = S.report_from_files("cell", str(tmp_path / "cell"), None, str(tmp_path))
    assert report["verdict_mode"] == "set-score" and report["set_score"]["score"] > 0
    assert report["restarts"] == 2 and report["chain_seed"] is False
    assert report["folds"][0]["train_job"] == "cell-wf1-r2"
    assert [x["won"] for x in report["folds"][0]["restarts"]] == [False, True]
    assert report["folds"][0]["set"]["n"] == 2


# ------------------------------------------------------------------ review fixes (2026-11-17)


@pytest.mark.parametrize("args", [["--seed", "7"], ["--seed=7"], ["--see", "7"], ["--se=7"],
                                  ["--expert", "X", "--seed"]])
def test_restarts_refuse_a_user_seed_in_any_spelling_argparse_would_read(args):
    with pytest.raises(WF.WalkForwardError, match="--seed"):
        WF.make_plan("cell", FOLDS, ["--expert", "X", *args], "ba2-test", 2, TH, test_parallel=1,
                     skip_train=False, only_fold=None, embargo_days=0, selection=SEL, restarts=2)


@pytest.mark.parametrize("args", [["--seed-genomes", "s.json"], ["--seed-population=b"],
                                  ["--search-sizing-mode"], ["--screener"], ["--strategy", "S2"],
                                  ["--slippage", "0.1"], ["--seed-check-only"]])
def test_flags_that_merely_start_like_seed_are_not_a_user_seed(args):
    assert not WF.user_sets_seed(args)
    WF.make_plan("cell", FOLDS, ["--expert", "X", *args], "ba2-test", 2, TH, test_parallel=1,
                 skip_train=False, only_fold=None, embargo_days=0, selection=SEL, restarts=2)


def test_without_restarts_a_user_seed_is_still_accepted():
    _plan_args = dict(test_parallel=1, skip_train=False, only_fold=None, embargo_days=0, selection=SEL)
    WF.make_plan("cell", FOLDS, ["--expert", "X", "--see", "7"], "ba2-test", 2, TH, **_plan_args)


@pytest.mark.parametrize("trades", [None, float("nan")])
def test_set_statistics_refuse_a_missing_trade_count(trades):
    g = _genome(1, 1, 10.0)
    g.oos_trades = trades
    with pytest.raises(WF.MissingMetric, match="trades"):
        WF.fold_set_stats(FOLDS[0], [_genome(1, 2, 5.0), g])


class _SiblingFails(SearchRunner):
    """Restart 1 is completed; restart 2's training fails (or ends without completing)."""

    def __init__(self, mode, **kw):
        super().__init__(restarts=2, completed={"cell-wf1"}, **kw)
        self.mode = mode

    def run_train(self, cmd, name):
        if name == "cell-wf1-r2":
            self.events.append(("train", name))
            if self.mode == "raises":
                raise WF.WalkForwardError(f"training job {name!r} exited with code 1")
            return                                   # 'finished' but never completed
        super().run_train(cmd, name)


@pytest.mark.parametrize("mode,match", [("raises", "cell-wf1-r2.*exited"),
                                        ("incomplete", "cell-wf1-r2.*did not reach")])
def test_a_failed_sibling_restart_stops_the_fold_without_choosing_a_winner(tmp_path, mode, match):
    r = _SiblingFails(mode, fitness={"cell-wf1": 50.0})
    with pytest.raises(WF.WalkForwardError, match=match):
        C.run_cell(_spec(chain_seed=False, restarts=2), r, str(tmp_path), log=lambda *_: None)
    assert not any(e[0] == "oos" for e in r.events) and r.fitness_reads == []
    assert not (tmp_path / "cell" / "fold1.json").exists()


def test_search_records_refuse_a_restart_that_is_not_completed():
    r = SearchRunner(restarts=2, completed={"cell-wf1"})
    with pytest.raises(WF.WalkForwardError, match="cell-wf1-r2.*not completed"):
        WF.search_records(_plan(restarts=2), r, FOLDS[0])
    assert r.fitness_reads == []


def _fake_launcher():
    from types import SimpleNamespace
    return SimpleNamespace(_resolve_estimates_source=lambda *a: None,
                           _assert_option_window_excludes_holdout=lambda *a: None,
                           _refuse_reused_estimates_source_name=lambda *a: None)


def test_ui_preflight_lists_every_restart_of_every_fold_and_of_the_refit():
    from app.services import wf_runs as R
    args = ["--expert", "FMPRating", "--universe", "AAPL", "--strategy", "S2"]
    out = R.preflight(_spec(optimize_args=args, chain_seed=False, restarts=2), _fake_launcher())
    f1 = out["folds"][0]
    assert f1["train_job"] == "cell-wf1" and "--seed" not in f1["command"]        # existing keys unchanged
    assert [x["job"] for x in f1["restarts"]] == ["cell-wf1", "cell-wf1-r2"]
    assert f1["restarts"][0]["command"] == f1["command"]
    r2 = f1["restarts"][1]["command"]
    assert r2[r2.index("--seed") + 1] == str(WF.restart_seed("cell-wf1-r2")) and r2[-1] == "cell-wf1-r2"
    refit = out["refit"]
    assert refit["job"] == "cell-refit" and [x["job"] for x in refit["restarts"]] == ["cell-refit", "cell-refit-r2"]
    rr = refit["restarts"][1]["command"]
    assert rr[rr.index("--seed") + 1] == str(WF.restart_seed("cell-refit-r2"))
    assert out["jobs"] == ["cell-wf1", "cell-wf1-r2", "cell-wf2", "cell-wf2-r2", "cell-wf3", "cell-wf3-r2",
                           "cell-refit", "cell-refit-r2"]


def test_ui_preflight_without_restarts_has_no_restarts_key():
    from app.services import wf_runs as R
    args = ["--expert", "FMPRating", "--universe", "AAPL", "--strategy", "S2"]
    out = R.preflight(_spec(optimize_args=args), _fake_launcher())
    assert all("restarts" not in f for f in out["folds"]) and "restarts" not in out["refit"]
