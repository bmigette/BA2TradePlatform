"""``tools/grid_goal2027.sh``: the walk-forward plan / verdict settings travel from the environment to
the driver flags. Unset, every step launches exactly the argv it did before them (golden captured
from TEST 2026.11.0205); set, they add the redesign flags, and a bad value refuses the step. Runs the
wrapper's own ``run_step`` under bash with a stub interpreter (grid_wrapper_harness); nothing runs."""
from __future__ import annotations

import json
import os

import pytest

from tests.grid_wrapper_harness import SH, bash, run_step

pytestmark = pytest.mark.skipif(bash() is None, reason="bash is not available on this host")

_GOLDEN = os.path.join(os.path.dirname(__file__), "data", "grid_goal2027_default_args.json")
with open(_GOLDEN, encoding="utf-8") as _fh:
    GOLDEN = json.load(_fh)


def _flag(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def _all(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_unset_settings_launch_exactly_the_old_argv(tmp_path, key):
    phase, step, *over = key.split(":")
    env = {"PHASE": phase}
    if over:
        env.update({"WF_TOP_N": "5", "WF_NEXT_GENERATIONS": "25", "DS_WEIGHTS": "search"})
    rc, argv, err = run_step(str(tmp_path), step, env)
    assert rc == 0, err
    assert argv == GOLDEN[key]


def test_stating_the_defaults_explicitly_changes_nothing(tmp_path):
    env = {"WF_TEST_YEARS": "2024,2025,2026", "PASS_MIN_FOLDS": "2", "PASS_REQUIRE_FOLD": "2026",
           "WF_RESTARTS": "1", "WF_CHAIN_SEED": "1", "WF_VERDICT": "winner", "WF_TEST_PARALLEL": "1"}
    rc, argv, err = run_step(str(tmp_path), "ds-large", env)
    assert rc == 0, err
    assert argv == GOLDEN["A:ds-large"]


def test_the_owner_redesign_settings_reach_the_driver(tmp_path):
    env = {"WF_TEST_YEARS": "2022,2023,2024,2025,2026", "WF_TOP_N": "30", "WF_RESTARTS": "2",
           "WF_CHAIN_SEED": "0", "WF_VERDICT": "set-score", "PASS_MIN_OOS_SCORE": "0",
           "WF_MIN_TRAIN_YEARS": "2", "WF_NEXT_GENERATIONS": "30"}
    rc, argv, err = run_step(str(tmp_path), "ds-large", env)
    assert rc == 0, err
    assert _flag(argv, "--wf-test-years") == "2022,2023,2024,2025,2026"
    assert _flag(argv, "--wf-top-n") == "30" and _flag(argv, "--wf-next-generations") == "30"
    assert _flag(argv, "--wf-restarts") == "2" and "--wf-no-chain-seed" in argv
    assert _flag(argv, "--wf-verdict") == "set-score" and _flag(argv, "--pass-min-oos-score") == "0"
    assert _flag(argv, "--wf-min-train-years") == "2"
    assert "--wf-defer-last-test" in argv and _flag(argv, "--wf-data-end") == "2025-12-31"   # phase A
    assert _all(argv, "--pass-require-fold") == ["2026"]
    # nothing else moved: without the new flags it is the old argv with the three changed values
    rest, k = [], 0
    while k < len(argv):
        if argv[k] == "--wf-no-chain-seed":
            k += 1
        elif argv[k] in ("--wf-restarts", "--wf-verdict", "--pass-min-oos-score", "--wf-min-train-years"):
            k += 2
        else:
            rest.append(argv[k])
            k += 1
    want = list(GOLDEN["A:ds-large"])
    for flag, value in (("--wf-test-years", "2022,2023,2024,2025,2026"), ("--wf-top-n", "30"),
                        ("--wf-next-generations", "30")):
        want[want.index(flag) + 1] = value
    assert rest == want


def test_required_folds_are_a_list_or_none(tmp_path):
    rc, argv, _ = run_step(str(tmp_path), "fr-mid", {"PASS_REQUIRE_FOLD": "2025,2026", "PASS_MIN_FOLDS": "1"})
    assert rc == 0 and _all(argv, "--pass-require-fold") == ["2025", "2026"]
    assert _flag(argv, "--pass-min-folds") == "1"
    rc, argv, _ = run_step(str(tmp_path), "fr-mid", {"PASS_REQUIRE_FOLD": "none"})
    assert rc == 0 and _all(argv, "--pass-require-fold") == []


@pytest.mark.parametrize("env,match", [
    ({"WF_RESTARTS": "2"}, "WF_CHAIN_SEED=0"),
    ({"WF_RESTARTS": "0", "WF_CHAIN_SEED": "0"}, "WF_RESTARTS"),
    ({"WF_RESTARTS": "two", "WF_CHAIN_SEED": "0"}, "WF_RESTARTS"),
    ({"WF_CHAIN_SEED": "no"}, "WF_CHAIN_SEED"),
    ({"WF_VERDICT": "set-score"}, "PASS_MIN_OOS_SCORE"),
    ({"PASS_MIN_OOS_SCORE": "0"}, "WF_VERDICT=set-score"),
    ({"WF_VERDICT": "best"}, "WF_VERDICT"),
    ({"PASS_REQUIRE_FOLD": "last"}, "PASS_REQUIRE_FOLD"),
    ({"PASS_MIN_FOLDS": "x"}, "PASS_MIN_FOLDS"),
    ({"WF_TEST_YEARS": "2024 2025"}, "WF_TEST_YEARS"),
    ({"WF_TEST_PARALLEL": "0"}, "WF_TEST_PARALLEL"),
    ({"WF_TEST_PARALLEL": "four"}, "WF_TEST_PARALLEL"),
    ({"WF_TEST_PARALLEL": "-2"}, "WF_TEST_PARALLEL"),
])
def test_a_bad_setting_refuses_the_step(tmp_path, env, match):
    rc, argv, err = run_step(str(tmp_path), "ds-large", env)
    assert rc == 2 and argv == [] and match in err


def test_the_settings_travel_to_the_systemd_unit():
    with open(SH, encoding="utf-8") as fh:
        line = next(ln for ln in fh.read().splitlines() if "for v in PHASE ESTIMATES_SOURCE" in ln)
    names = set(line.split("for v in", 1)[1].split(";")[0].split())
    assert {"WF_TEST_YEARS", "PASS_MIN_FOLDS", "PASS_REQUIRE_FOLD", "WF_RESTARTS", "WF_CHAIN_SEED",
            "WF_VERDICT", "PASS_MIN_OOS_SCORE", "WF_MIN_TRAIN_YEARS", "WF_TOP_N",
            "WF_FIRST_GENERATIONS", "WF_NEXT_GENERATIONS", "WF_TEST_PARALLEL"} <= names


def test_the_out_of_sample_test_parallelism_reaches_the_driver(tmp_path):
    """WF_TEST_PARALLEL (default 1 = the argv above) sets --wf-test-parallel and nothing else."""
    rc, argv, err = run_step(str(tmp_path), "ds-large", {"WF_TEST_PARALLEL": "6"})
    assert rc == 0, err
    assert _all(argv, "--wf-test-parallel") == ["6"]
    want = list(GOLDEN["A:ds-large"])
    want[want.index("--wf-test-parallel") + 1] = "6"
    assert argv == want


def test_the_banner_shows_the_test_parallelism():
    with open(SH, encoding="utf-8") as fh:
        banner = next(ln for ln in fh.read().splitlines() if "=== walk-forward:" in ln)
    assert "$WF_TEST_PARALLEL" in banner


def test_preflight_validates_the_settings_before_launching():
    with open(SH, encoding="utf-8") as fh:
        src = fh.read()
    pre = src[src.index("preflight() {"):src.index("\n}\n", src.index("preflight() {"))]
    assert "wf_plan_args || exit 2" in pre


# ------------------------------------------------------------------ phase B: the daily screener panel


def _run_body(tmp_path, env, tail):
    """(exit code, stdout lines, stderr) of the wrapper's functions followed by ``tail`` (nothing runs)."""
    import subprocess

    from tests import grid_wrapper_harness as H
    with open(SH, encoding="utf-8") as fh:
        source = fh.read()
    path = os.path.join(str(tmp_path), "grid_body.sh")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("stubpy() { printf '%s\n' \"$@\"; }\nPY=stubpy\n"
                 + source[:source.index('\ncase "$CMD" in')] + "\n" + tail + "\n")
    base = {k: v for k, v in os.environ.items() if k not in H.WRAPPER_VARS}
    r = subprocess.run([bash(), path], capture_output=True, text=True, env={**base, **H.BASE_ENV, **env})
    return r.returncode, r.stdout.splitlines(), r.stderr


def test_the_stated_panel_reaches_the_driver_and_nothing_else_changes(tmp_path):
    rc, argv, err = run_step(str(tmp_path), "ds-large", {"PHASE": "B", "SCREENER_PANEL": "/c/daily_panel/P2027"})
    assert rc == 0, err
    i = argv.index("--screener-panel")
    assert argv[i + 1] == "/c/daily_panel/P2027"
    assert argv[:i] + argv[i + 2:] == GOLDEN["B:ds-large"]


@pytest.mark.parametrize("env,rc", [
    ({"PHASE": "B", "STEPS": "rank-large ds-large"}, 1),          # a daily-panel step and no panel stated
    ({"PHASE": "B", "STEPS": "s9-fr-small"}, 1),
    ({"PHASE": "B", "STEPS": "rank-large senate"}, 0),            # weekly store / no screen: no panel read
    ({"PHASE": "B", "STEPS": "ds-large", "SCREENER_PANEL": "/c/daily_panel/P2027"}, 0),
    ({"PHASE": "A", "STEPS": "ds-large"}, 0),                     # phase A trains on the newest panel
])
def test_phase_b_refuses_to_start_without_the_rebuilt_panel(tmp_path, env, rc):
    """Phase A's jobs name phase A's panel, which does not reach the 2026 test window; a stored job is
    re-pointed only at a panel the command names. Unstated, every classic cell would fail its deferred
    test after the whole chain had run."""
    got, out, err = _run_body(tmp_path, env, 'require_phase_b_panel; echo "rc=$?"')
    assert out[-1] == f"rc={rc}", err
    assert ("SCREENER_PANEL" in err) == (rc == 1)


def test_every_setting_travels_to_the_systemd_unit_even_an_empty_worker_list(tmp_path):
    """`start` on Linux hands the settings to a transient unit. Four of them were not on the list, so
    the unit ran on the defaults; and WORKERS="" (local-only) was dropped as empty, so the unit took
    the default worker list."""
    env = {"WORKERS": "", "PARALLEL": "4", "G27_MARKET_EXIT": "exit,stop", "G27_REGIME_SPLIT": "entry",
           "PASS_DD_MULT": "3", "PASS_MAX_OOS_DD": "40", "SCREENER_PANEL": "/c/daily_panel/P2027"}
    rc, out, err = _run_body(tmp_path, env, 'unit_env_args; printf "%s\n" "${local_env[@]}"')
    assert rc == 0, err
    for want in ("--setenv=WORKERS=", "--setenv=PARALLEL=4", "--setenv=G27_MARKET_EXIT=exit,stop",
                 "--setenv=G27_REGIME_SPLIT=entry", "--setenv=PASS_DD_MULT=3", "--setenv=PASS_MAX_OOS_DD=40",
                 "--setenv=SCREENER_PANEL=/c/daily_panel/P2027", "--setenv=PHASE=A"):
        assert want in out, (want, out)
    assert not [ln for ln in out if ln.startswith("--setenv=MC_SEN_OHLCV=")]     # unset stays unset
