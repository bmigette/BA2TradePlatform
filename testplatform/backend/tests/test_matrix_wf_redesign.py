"""``run_screener_capband_matrix.py --walk-forward`` flags of the 2026-11-17 redesign: --wf-no-chain-seed,
--wf-restarts, --wf-verdict set-score + --pass-min-oos-score, --wf-min-train-years. They enter the cell
name only when given (the defaults are pinned by test_wf_golden_default). Fake runner; nothing runs."""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from test_matrix_walk_forward import BAD, GOOD, Fake, _argv, _cmds, _flag, env  # noqa: E402,F401
import run_screener_capband_matrix as M  # noqa: E402  (tools/ is on the path from here on)


class Fit(Fake):
    """Fake whose training jobs report a best fitness: the -r2 search of every fold wins."""

    def train_best_fitness(self, name):
        return 2.0 if name.endswith("-r2") else 1.0


@pytest.fixture
def fit(env, monkeypatch):
    f = Fit()
    monkeypatch.setattr(M, "_make_wf_runner", lambda args: f)
    env.fake = f
    return env


def _run(env, monkeypatch, *extra, experts=("DeterministicScorer",), require="2026"):
    env.fake.order.clear()
    env.fake.completed.clear()
    env.fake.trained.clear()
    env.fake.tested.clear()
    argv = _argv(env, *extra, experts=experts)
    argv[argv.index("--pass-require-fold") + 1] = require
    monkeypatch.setattr(sys, "argv", argv)
    return M.main()


def _cell(env):
    return env.fake.order[0].split("-wf1")[0]


NEW = ["--wf-no-chain-seed"]


def test_no_chain_seed_runs_every_job_cold_on_the_first_budget(fit, monkeypatch):
    assert _run(fit, monkeypatch, *NEW) == 0
    cell = _cell(fit)
    assert fit.fake.order == [f"{cell}-wf1", f"{cell}-wf2", f"{cell}-wf3", f"{cell}-refit"]
    for name, cmd in _cmds(fit.fake).items():
        assert _flag(cmd, "--chain-seed-from") is None and "--persist-final-population" not in cmd
        assert _flag(cmd, "--generations") == "30", name


def test_restarts_train_every_search_and_test_the_winner(fit, monkeypatch):
    assert _run(fit, monkeypatch, *NEW, "--wf-restarts", "2") == 0
    cell = _cell(fit)
    assert fit.fake.order == [f"{cell}-wf1", f"{cell}-wf1-r2", f"{cell}-wf2", f"{cell}-wf2-r2",
                              f"{cell}-wf3", f"{cell}-wf3-r2", f"{cell}-refit", f"{cell}-refit-r2"]
    assert [t[0] for t in fit.fake.tested] == [f"{cell}-wf{k}-r2" for k in (1, 2, 3)]
    c = _cmds(fit.fake)
    assert _flag(c[f"{cell}-wf1"], "--seed") is None and _flag(c[f"{cell}-wf1-r2"], "--seed")
    summ = json.loads(open(os.path.join(fit.report, "summary.json"), encoding="utf-8").read())
    assert summ["cells"][0]["refit_job"] == f"{cell}-refit-r2"


def test_restarts_without_no_chain_seed_are_refused(fit, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _run(fit, monkeypatch, "--wf-restarts", "2")
    assert "--wf-no-chain-seed" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        _run(fit, monkeypatch, *NEW, "--wf-restarts", "0")


def test_set_score_needs_its_minimum_and_the_minimum_needs_set_score(fit, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _run(fit, monkeypatch, "--wf-verdict", "set-score")
    assert "--pass-min-oos-score" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        _run(fit, monkeypatch, "--pass-min-oos-score", "0")
    assert "--wf-verdict set-score" in capsys.readouterr().err


def test_set_score_runs_every_fold_and_judges_on_the_score(fit, monkeypatch):
    fit.fake.oos_by_fold = (BAD, BAD, GOOD)          # the winner rule would eliminate after fold 2
    assert _run(fit, monkeypatch, "--wf-verdict", "set-score", "--pass-min-oos-score", "-5") == 0
    assert [t[1] for t in fit.fake.tested] == [1, 2, 3]
    summ = json.loads(open(os.path.join(fit.report, "summary.json"), encoding="utf-8").read())
    c = summ["cells"][0]
    assert c["verdict_mode"] == "set-score" and c["set_score"]["score"] == pytest.approx(-5 / 3)
    assert c["verdict"] == "PASS"                     # -1.67 > -5


def test_min_train_years_drops_the_short_folds_of_a_floored_expert(fit, monkeypatch, capsys):
    assert _run(fit, monkeypatch, "--wf-test-years", "2022,2023,2024,2025", "--wf-min-train-years", "2",
                experts=("FMPRating",), require="2025") == 0
    cell = _cell(fit)
    assert [n[len(cell):] for n in fit.fake.order] == ["-wf1", "-wf2", "-refit"]
    c = _cmds(fit.fake)
    assert _flag(c[f"{cell}-wf1"], "--end") == "2023-12-31" and _flag(c[f"{cell}-wf2"], "--end") == "2024-12-31"
    out = capsys.readouterr().out
    assert "2022" in out and "2023" in out and "dropped" in out


def test_min_train_years_leaving_no_fold_is_refused(fit, monkeypatch):
    with pytest.raises(SystemExit, match="--wf-min-train-years"):
        _run(fit, monkeypatch, "--wf-test-years", "2022,2023", "--wf-min-train-years", "5",
             "--pass-min-folds", "1", experts=("FMPRating",), require="2023")


@pytest.mark.parametrize("flags", [
    ["--wf-no-chain-seed"], ["--wf-restarts", "2", "--wf-verdict", "set-score", "--pass-min-oos-score", "0"],
    ["--wf-verdict", "set-score", "--pass-min-oos-score", "0"], ["--wf-min-train-years", "2"],
    ["--pass-min-oos-score", "0"], ["--wf-restarts", "1"],
])
def test_the_new_flags_need_walk_forward(env, monkeypatch, flags, capsys):
    argv = _argv(env, experts=("DeterministicScorer",))
    i = argv.index("--walk-forward")
    monkeypatch.setattr(sys, "argv", argv[:i] + flags)
    with pytest.raises(SystemExit):
        M.main()
    assert "need --walk-forward" in capsys.readouterr().err


def test_the_cell_name_changes_only_with_a_non_default_setting(fit, monkeypatch):
    assert _run(fit, monkeypatch) == 0
    base = _cell(fit)
    # stating a default explicitly keeps the name
    assert _run(fit, monkeypatch, "--wf-restarts", "1", "--wf-verdict", "winner") == 0
    assert _cell(fit) == base
    names = set()
    for flags in (NEW, NEW + ["--wf-restarts", "2"], ["--wf-verdict", "set-score", "--pass-min-oos-score", "0"],
                  ["--wf-verdict", "set-score", "--pass-min-oos-score", "1"], ["--wf-min-train-years", "2"]):
        assert _run(fit, monkeypatch, *flags) == 0
        names.add(_cell(fit))
        assert _cell(fit) != base, flags
        assert _run(fit, monkeypatch, *flags) == 0             # and is stable
        assert _cell(fit) in names
    assert len(names) == 5


def test_the_owner_example_runs(fit, monkeypatch):
    """Test years 2022-2025 (+2026 deferred), top-n 30, 2 restarts, no chain seed, set score > 0, min
    train years 2: DeterministicScorer (no floor) keeps 2022 (trains 2020-2021 = 2.0 years)."""
    fit.fake.oos_by_fold = (GOOD,) * 5
    rc = _run(fit, monkeypatch, "--wf-test-years", "2022,2023,2024,2025,2026", "--wf-defer-last-test",
              "--wf-data-end", "2025-12-31", "--wf-restarts", "2", "--wf-no-chain-seed",
              "--wf-verdict", "set-score", "--pass-min-oos-score", "0", "--wf-min-train-years", "2")
    assert rc == 0
    cell = _cell(fit)
    assert [n[len(cell):] for n in fit.fake.order] == [
        f"-wf{k}{r}" for k in range(1, 6) for r in ("", "-r2")]
    assert [t[1] for t in fit.fake.tested] == [1, 2, 3, 4]       # wf5 (2026) is deferred
    summ = json.loads(open(os.path.join(fit.report, "summary.json"), encoding="utf-8").read())
    assert summ["cells"][0]["verdict"] == "PENDING"
