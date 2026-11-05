"""``tools/prepare_option_universe.py`` -- the option-universe data pipeline's orchestration.

The steps themselves are the committed tools (each with its own tests); what is pinned here is
the wiring: the order, the arguments each step gets, which exit codes stop the pipeline, the
report, ``--from-step``/``--steps``/``--dry-run``. A fake runner stands in for the subprocesses,
so nothing is fetched. No network.
"""
import importlib
import json
import os

import pytest

prep = importlib.import_module("tools.prepare_option_universe")

ORDER = ["split-check", "dividends", "fmp-repair", "thetadata", "ds-prewarm", "gate-check",
         "plan-ohlcv-v1", "build-ohlcv-v1", "plan-ta-structure-v1", "build-ta-structure-v1", "preflight"]


class FakeRunner:
    """Records every step; answers with canned exit codes and stdout."""

    def __init__(self, rc=None, stdout=None):
        self.calls = []
        self.rc = rc or {}
        self.stdout = stdout or {}

    def __call__(self, argv, out_path, err_path):
        label = os.path.basename(out_path)[3:-4]
        self.calls.append((label, argv))
        body = self.stdout.get(label)
        if body is None and label.startswith("build-"):
            body = {"ok": True, "manifest_digest": f"digest-{label[6:]}", "excluded": {}, "counters": {}}
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(body) if body is not None else "")
        open(err_path, "w").close()
        if label == "preflight":
            with open(os.path.join(os.path.dirname(out_path), "preflight.json"), "w") as f:
                json.dump({"summary": {"blocking": 0}}, f)
        return self.rc.get(label, 0)

    def labels(self):
        return [c[0] for c in self.calls]

    def argv(self, label):
        return next(a for lab, a in self.calls if lab == label)


@pytest.fixture
def universe(tmp_path):
    p = tmp_path / "u.txt"
    p.write_text("AAA\nBBB\n", encoding="utf-8")
    return str(p)


def _args(universe, tmp_path, *extra):
    return ["--universe-file", universe, "--start", "2020-01-01", "--end", "2025-12-31",
            "--report-dir", str(tmp_path / "rep"), *extra]


def _report(tmp_path):
    return json.loads((tmp_path / "rep" / "prepare_report.json").read_text())


def test_every_step_runs_in_order_and_the_report_names_the_digests(universe, tmp_path):
    r = FakeRunner()
    assert prep.main(_args(universe, tmp_path), runner=r, log=lambda m: None) == 0
    assert r.labels() == ORDER
    rep = _report(tmp_path)
    assert rep["stopped"] is None
    assert rep["manifest_digests"] == {"ohlcv-v1": "digest-ohlcv-v1",
                                       "ta-structure-v1": "digest-ta-structure-v1"}
    # Each step is the committed tool it claims to be.
    assert r.argv("split-check")[1].endswith("warm_market_conditions.py") and \
        r.argv("split-check")[2] == "plan"
    assert r.argv("fmp-repair")[2:4] == ["repair", "--plan"]
    for label in ("split-check", "fmp-repair", "plan-ohlcv-v1", "build-ohlcv-v1", "plan-ta-structure-v1",
                  "build-ta-structure-v1"):
        a = r.argv(label)
        assert a[a.index("--ohlcv-history-source") + 1] == "fmp", label
    assert r.argv("dividends")[1].endswith("warm_dividend_calendars.py")
    theta = r.argv("thetadata")
    assert theta[1].endswith("warm_options_history.py") and "--wide" in theta
    assert theta[theta.index("--provider") + 1] == "thetadata"
    assert r.argv("ds-prewarm")[1].endswith("ba2test_launcher.py")
    assert r.argv("ds-prewarm")[2:5] == ["prewarm", "--experts", "DeterministicScorer"]
    assert "--cache-only" in r.argv("build-ohlcv-v1")
    assert r.argv("gate-check")[r.argv("gate-check").index("--sections") + 1] == "gate"
    assert r.argv("preflight")[1].endswith("option_universe_preflight.py")


def test_an_actionable_split_check_continues_into_the_repair(universe, tmp_path):
    r = FakeRunner(rc={"split-check": 1})
    assert prep.main(_args(universe, tmp_path), runner=r, log=lambda m: None) == 0
    assert r.labels() == ORDER


def test_a_name_still_refused_after_the_repair_stops_the_pipeline(universe, tmp_path):
    r = FakeRunner(rc={"fmp-repair": 1},
                   stdout={"fmp-repair": {"ok": False, "still_refused": {"SATS": [{"verdict": "drift"}]},
                                          "fetch_errors": {"SATS": "LESS history"}}})
    assert prep.main(_args(universe, tmp_path), runner=r, log=lambda m: None) == 1
    assert r.labels() == ["split-check", "dividends", "fmp-repair"]
    rep = _report(tmp_path)
    assert "SATS" in rep["stopped"] and "keep-newer-cached-tail" in rep["stopped"]
    assert rep["steps"][-1]["result"]["still_refused"] == {"SATS": [{"verdict": "drift"}]}


@pytest.mark.parametrize("label", ["dividends", "thetadata", "ds-prewarm", "gate-check",
                                   "build-ta-structure-v1", "preflight"])
def test_any_other_failing_step_stops_the_pipeline_there(universe, tmp_path, label):
    r = FakeRunner(rc={label: 1})
    assert prep.main(_args(universe, tmp_path), runner=r, log=lambda m: None) == 1
    assert r.labels() == ORDER[:ORDER.index(label) + 1]
    assert _report(tmp_path)["stopped"]


def test_from_step_and_steps_select_what_runs(universe, tmp_path):
    r = FakeRunner()
    assert prep.main(_args(universe, tmp_path, "--from-step", "thetadata"), runner=r, log=lambda m: None) == 0
    assert r.labels() == ORDER[3:]
    r = FakeRunner()
    assert prep.main(_args(universe, tmp_path, "--steps", "preflight,dividends"), runner=r,
                     log=lambda m: None) == 0
    assert r.labels() == ["dividends", "preflight"]


def test_the_operator_flags_reach_their_step(universe, tmp_path):
    r = FakeRunner()
    assert prep.main(_args(universe, tmp_path, "--keep-newer-cached-tail", "SATS", "--allow-exclusions",
                           "--db", "keys.sqlite", "--theta-end", "2026-09-25",
                           "--guard-starts", "2020-01-01,2023-01-01", "--procs", "3"),
                     runner=r, log=lambda m: None) == 0
    rep_argv = r.argv("fmp-repair")
    assert rep_argv[rep_argv.index("--keep-newer-cached-tail") + 1] == "SATS"
    assert "--allow-exclusions" in r.argv("build-ohlcv-v1")
    theta = r.argv("thetadata")
    assert theta[theta.index("--db") + 1] == "keys.sqlite"
    assert theta[theta.index("--end") + 1] == "2026-09-25"
    pf = r.argv("preflight")
    assert pf[pf.index("--guard-starts") + 1] == "2020-01-01,2023-01-01"
    assert pf[pf.index("--procs") + 1] == "3"


def test_dry_run_runs_nothing_and_lists_every_command(universe, tmp_path):
    r = FakeRunner()
    lines = []
    assert prep.main(_args(universe, tmp_path, "--dry-run"), runner=r, log=lines.append) == 0
    assert r.calls == []
    assert [ln.split(":", 1)[0] for ln in lines if ln.startswith("[dry-run]")] == \
        [f"[dry-run] {x}" for x in ORDER]


def test_configuration_errors_exit_two(universe, tmp_path):
    assert prep.main(_args(universe, tmp_path, "--steps", "bogus"), runner=FakeRunner(),
                     log=lambda m: None) == 2
    assert prep.main(["--universe-file", str(tmp_path / "none.txt"), "--start", "2020-01-01",
                      "--end", "2025-12-31", "--report-dir", str(tmp_path)], runner=FakeRunner(),
                     log=lambda m: None) == 2
    # The repair reads the split-check plan: without it, refuse up front.
    assert prep.main(_args(universe, tmp_path, "--steps", "fmp-repair"), runner=FakeRunner(),
                     log=lambda m: None) == 2


# ------------------------------------------------ review of 5178e625..90980f18
def test_allow_exclusions_lets_the_build_decide_on_an_actionable_plan(universe, tmp_path):
    """plan exits 1 exactly when a symbol cannot be warmed: with --allow-exclusions the build
    must still run (it records the exclusion); without it the pipeline stops."""
    r = FakeRunner(rc={"plan-ohlcv-v1": 1, "plan-ta-structure-v1": 1})
    assert prep.main(_args(universe, tmp_path, "--allow-exclusions"), runner=r, log=lambda m: None) == 0
    assert "build-ohlcv-v1" in r.labels() and "build-ta-structure-v1" in r.labels()
    r = FakeRunner(rc={"plan-ohlcv-v1": 1})
    assert prep.main(_args(universe, tmp_path), runner=r, log=lambda m: None) == 1
    assert "build-ohlcv-v1" not in r.labels()


def test_a_fatal_split_check_preflight_stops_before_the_repair(universe, tmp_path):
    body = {"plan": {}, "preflight_errors": ["source certification failed for fmp-daily: [...]"],
            "fatal_preflight_errors": ["source certification failed for fmp-daily: [...]"],
            "inventory": []}
    r = FakeRunner(rc={"split-check": 1}, stdout={"split-check": body})
    assert prep.main(_args(universe, tmp_path), runner=r, log=lambda m: None) == 1
    assert r.labels() == ["split-check"]
    assert "certification" in _report(tmp_path)["stopped"]


def test_an_open_session_repair_stops_the_pipeline_and_says_rerun(universe, tmp_path):
    r = FakeRunner(stdout={"fmp-repair": {"ok": True, "rerun_after_close_required": True}})
    assert prep.main(_args(universe, tmp_path, "--allow-open-session"), runner=r, log=lambda m: None) == 1
    assert r.labels() == ["split-check", "dividends", "fmp-repair"]
    assert "after the close" in _report(tmp_path)["stopped"]


def test_the_repair_stop_advises_a_restart_at_split_check(universe, tmp_path):
    r = FakeRunner(rc={"fmp-repair": 1}, stdout={"fmp-repair": {"ok": False, "still_refused": {"X": []}}})
    assert prep.main(_args(universe, tmp_path), runner=r, log=lambda m: None) == 1
    assert "--from-step split-check" in _report(tmp_path)["stopped"]
