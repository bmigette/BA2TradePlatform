"""GA budget of the exploration grid (plan 2026-09-24, Task A4 revision).

Pinned here:

* genetic mode sizes each job from the genes its FINAL manifest searches (after the market entry
  gates and market exits are attached): generations 25, or 30 above 20 genes; early stop 8;
  population clamp(4 x genes, 24, 120);
* the count is the GA's own collector (``strategy_param_space.collect_param_space``, loaded by
  path so building a manifest never imports ``app``), minus one-point ranges, and it includes the
  genes B5/B6 record in ``market_condition``/``market_exit`` exactly once;
* explicit ``--population``/``--generations``/``--early-stop`` always win; an early stop outside
  1..generations is refused, and so is ``--early-stop`` in grid mode;
* grid mode is byte-identical (both pinned fingerprints), and only genetic jobs carry
  ``geneCount``/``budgetSource``;
* the preview and launch lines print each job's genes and budget.

No broker, provider request, database or grid run.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.strategy_research.exploration import profiles as P
from tools.strategy_research.exploration import run_exploration as D
from app.services.strategy_param_space import collect_param_space

DEFAULT_FINGERPRINT = "798c8787f90a6e1215f964edc453d3c58a29138fec845cefab1f7d2873bb2fa9"
PULLBACK_RSI_FINGERPRINT = "b9524f745111e7ecfd4f17c7cbbc8c952df545879612e1359456b5c14aee6720"
PINS = {"ohlcv-v1": "a" * 64, "ta-structure-v1": "b" * 64}
BOTH = "ohlcv-v1,ta-structure-v1"
BUDGET_KEYS = ("populationSize", "generations", "earlyStoppingGenerations")


def manifest(profile="none", families=P.ALL_FAMILIES, **kwargs):
    kwargs.setdefault("search", "genetic")
    if profile != "none":
        kwargs.update(market_condition_profile=profile,
                      market_condition_manifest=",".join(f"{p}={PINS[p]}" for p in profile.split(",")))
    return P.build_manifest(families=families, **kwargs)


def by_key(m):
    return {(j["family"], j["variant"]): j for j in m["jobs"]}


def budget(job):
    return tuple(job["optimization_config"][k] for k in BUDGET_KEYS)


def collector_genes(job):
    """What the handler's GA searches: the REAL collector on the stored strategy and params."""
    oc = job["optimization_config"]
    space = collect_param_space(SimpleNamespace(**job["strategy"]), oc["expert_params"])
    return sorted(name for name, spec in space.items() if spec["min"] != spec["max"])


FULL = dict(profile=BOTH, market_exit=("exit", "stop", "tp"))


# --------------------------------------------------------------------------- the rule
@pytest.mark.parametrize("genes,expected", [
    (0, (24, 25, 8)), (1, (24, 25, 8)), (3, (24, 25, 8)), (6, (24, 25, 8)), (7, (28, 25, 8)),
    (20, (80, 25, 8)), (21, (84, 30, 8)), (30, (120, 30, 8)), (40, (120, 30, 8))])
def test_budget_rule(genes, expected):
    resolved, source = P.ga_budget(genes)
    assert tuple(resolved[k] for k in BUDGET_KEYS) == expected
    assert source == dict.fromkeys(BUDGET_KEYS, "auto")


def test_a_small_job_without_market_options_gets_the_floor():
    job = by_key(manifest(families=("pullback_rsi",)))[("pullback_rsi", "long_sma5")]
    oc = job["optimization_config"]
    assert job["optimization_type"] == "genetic" and not job["fixed"]
    assert oc["geneCount"] == 3 == len(collector_genes(job))
    assert budget(job) == (24, 25, 8)
    assert oc["budgetSource"] == dict.fromkeys(BUDGET_KEYS, "auto")


def test_both_profiles_and_market_exits_count_every_gene_once():
    plain, full = by_key(manifest()), by_key(manifest(**FULL))
    assert plain.keys() == full.keys()
    for key, job in full.items():
        oc, bt = job["optimization_config"], job["optimization_config"]["backtest"]
        base = plain[key]["optimization_config"]["geneCount"]
        added = bt["market_condition"]["gene_count"] + bt["market_exit"]["gene_count"]
        # The recorded market genes are among the collector's, so the total adds, never doubles.
        assert set(bt["market_condition"]["genes"]) | set(bt["market_exit"]["genes"]) <= set(collector_genes(job))
        assert oc["geneCount"] == base + added == len(collector_genes(job)), key
        assert oc["geneCount"] > 20, key
        assert budget(job) == (min(max(4 * oc["geneCount"], 24), 120), 30, 8), key
    # Reference counts: +15 per entry rule with both profiles, ~9 (7 with the stop omitted) exits.
    job = full[("large_ds", "quality_momentum_70_30")]
    assert job["optimization_config"]["geneCount"] == 25 and budget(job) == (100, 30, 8)
    assert full[("small_earnings", "control")]["optimization_config"]["geneCount"] == 22


def test_a_forty_gene_job_is_capped_at_population_120():
    job = by_key(manifest(**FULL))[("mid_insider", "timeout")]
    assert job["optimization_config"]["geneCount"] == 38
    assert budget(job) == (120, 30, 8)


def test_a_fixed_recipe_searches_nothing():
    """A fixed job's one-point sizing range is not a gene; it is evaluated once (brute force)."""
    job = by_key(manifest(families=("mid_ds",)))[("mid_ds", "control")]
    assert job["fixed"] and job["optimization_type"] == "brute_force"
    oc = job["optimization_config"]
    assert len(collect_param_space(SimpleNamespace(**job["strategy"]), oc["expert_params"])) == 1
    assert oc["geneCount"] == 0 and collector_genes(job) == []
    assert "fixed recipe evaluated once" in P.budget_text(job)


@pytest.mark.parametrize("kwargs", [{}, FULL, dict(profile="ohlcv-v1"), dict(profile="ta-structure-v1"),
                                    dict(profile=BOTH, market_exit=("exit", "tp")),
                                    dict(profile="ta-structure-v1", market_exit=("stop",),
                                         families=("mid_ds", "pullback_rsi"))])
def test_the_count_equals_the_real_collector_for_every_job(kwargs):
    for job in manifest(**kwargs)["jobs"]:
        assert P.searched_genes(job["strategy"], job["optimization_config"]["expert_params"]) \
            == collector_genes(job), (job["family"], job["variant"])
        assert job["optimization_config"]["geneCount"] == len(collector_genes(job))


def test_no_job_uses_a_bypass_expert():
    """A bypass expert's handler drops the rule genes; the count assumes none does."""
    from app.services.strategy_optimization_handler import _is_bypass_expert
    for job in manifest(**FULL)["jobs"]:
        assert not _is_bypass_expert(job["optimization_config"]["backtest"]), job["expert"]


def test_recorded_market_genes_the_ga_would_not_search_are_refused(monkeypatch):
    monkeypatch.setattr(P, "searched_genes", lambda strategy, params: [])
    with pytest.raises(ValueError, match="recorded market genes the GA would not search"):
        manifest(families=("mid_ds",), profile="ohlcv-v1")


# --------------------------------------------------------------------------- overrides
def test_explicit_values_win():
    for job in manifest(population=50, generations=12, early_stop=5, **FULL)["jobs"]:
        assert budget(job) == (50, 12, 5)
        assert job["optimization_config"]["budgetSource"] == dict.fromkeys(BUDGET_KEYS, "explicit")
    job = by_key(manifest(generations=40, **FULL))[("mid_insider", "timeout")]
    assert budget(job) == (120, 40, 8)
    assert job["optimization_config"]["budgetSource"] == {
        "populationSize": "auto", "generations": "explicit", "earlyStoppingGenerations": "auto"}
    # Generations below the default early stop cap it (only the generations were passed).
    job = by_key(manifest(families=("pullback_rsi",), generations=5))[("pullback_rsi", "long_sma5")]
    assert budget(job) == (24, 5, 5)
    job = by_key(manifest(families=("pullback_rsi",), population=30))[("pullback_rsi", "long_sma5")]
    assert budget(job) == (30, 25, 8)


def test_the_budget_is_part_of_the_job_identity():
    a = by_key(manifest(families=("pullback_rsi",)))
    b = by_key(manifest(families=("pullback_rsi",), early_stop=4))
    for key in a:
        D.verify_job(a[key])
        D.verify_job(b[key])
        assert a[key]["name"] != b[key]["name"]


@pytest.mark.parametrize("kwargs,match", [
    (dict(generations=10, early_stop=11), "Early stop must be between 1 and the generations"),
    (dict(early_stop=0), "Early stop must be between 1 and the generations"),
    (dict(early_stop=28), r"pullback_rsi/long_sma5 \(3 genes\): early stop 28 must be between 1 and the generations \(25\)"),
    (dict(search="grid", early_stop=8), "--early-stop applies to --search genetic only"),
    (dict(population=1), "Invalid search budget"),
    (dict(generations=0), "Invalid search budget"),
])
def test_an_invalid_budget_is_refused(kwargs, match):
    with pytest.raises(ValueError, match=match):
        manifest(families=("pullback_rsi",), **kwargs)


# --------------------------------------------------------------------------- grid mode
def test_grid_mode_is_byte_identical():
    default = P.build_manifest()
    assert P.fingerprint(default) == DEFAULT_FINGERPRINT
    assert P.fingerprint(P.build_manifest(families=["pullback_rsi"])) == PULLBACK_RSI_FINGERPRINT
    for job in default["jobs"]:
        oc = job["optimization_config"]
        assert budget(job) == (24, 4, 4)
        assert "geneCount" not in oc and "budgetSource" not in oc
        assert P.budget_text(job) == ""
    # Explicit grid values are written as before (early stop = generations).
    job = P.build_manifest(families=["mid_ds"], population=30, generations=6)["jobs"][0]
    assert budget(job) == (30, 6, 6) and "geneCount" not in job["optimization_config"]


# --------------------------------------------------------------------------- CLI
def test_cli_flags_default_to_not_passed():
    args = D.parser().parse_args([])
    assert (args.population, args.generations, args.early_stop) == (None, None, None)
    assert "--early-stop" in D.parser().format_help()


def test_cli_dry_run_prints_every_jobs_budget(tmp_path, capsys):
    argv = ["--families", "pullback_rsi", "--search", "genetic", "--dry-run", "--output-dir", str(tmp_path)]
    assert D.main(argv) == 0
    out = capsys.readouterr().out
    written = json.loads((tmp_path / "manifest.json").read_text())
    assert written == manifest(families=("pullback_rsi",))
    assert out.count("budget: genes=3 population=24 generations=25 early_stop=8") == 4
    assert out.count("budget: genes=4 population=24 generations=25 early_stop=8") == 1
    assert D.main(argv + ["--generations", "40", "--early-stop", "10"]) == 0
    out = capsys.readouterr().out
    assert out.count("population=24 generations=40 early_stop=10 explicit=generations,earlyStoppingGenerations") == 5
    argv = ["--families", "mid_insider", "--search", "genetic", "--market-condition-profile", BOTH,
            "--market-condition-manifest", ",".join(f"{p}={d}" for p, d in PINS.items()),
            "--market-exit", "exit,stop,tp", "--variants", "timeout", "--dry-run", "--output-dir", str(tmp_path)]
    assert D.main(argv) == 0
    assert ("budget: genes=38 (incl. market entry 30, market exit 7) population=120 generations=30 early_stop=8"
            in capsys.readouterr().out)


def test_cli_refuses_an_early_stop_above_the_generations(tmp_path, capsys):
    argv = ["--families", "pullback_rsi", "--search", "genetic", "--generations", "10", "--early-stop", "11",
            "--dry-run", "--output-dir", str(tmp_path)]
    assert D.main(argv) == 1
    assert "Early stop must be between 1 and the generations" in capsys.readouterr().err
    assert D.main(["--families", "mid_ds", "--early-stop", "8", "--dry-run", "--output-dir", str(tmp_path)]) == 1
    assert "--early-stop applies to --search genetic only" in capsys.readouterr().err


def test_the_launch_line_prints_the_budget(tmp_path, monkeypatch, capsys):
    jobs = manifest(families=("pullback_rsi",))["jobs"][:1]
    monkeypatch.setattr(D, "check_database", lambda path: tmp_path / "db.sqlite")
    monkeypatch.setattr(D.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    args = SimpleNamespace(cache_dir=tmp_path, resume=False, db_file=tmp_path / "db.sqlite")
    assert D.run_jobs(jobs, tmp_path, args) == 0
    out = capsys.readouterr().out
    assert f"[1/1] {jobs[0]['name']}\n  budget: genes=3 population=24 generations=25 early_stop=8\n  Log:" in out


_NO_BACKEND_SCRIPT = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.strategy_research.exploration import profiles as P
m = P.build_manifest(families=P.ALL_FAMILIES, search="genetic",
                     market_condition_profile="ohlcv-v1,ta-structure-v1",
                     market_condition_manifest="ohlcv-v1=" + "a" * 64 + ",ta-structure-v1=" + "b" * 64,
                     market_exit=("exit", "stop", "tp"))
assert all(j["optimization_config"]["geneCount"] > 20 for j in m["jobs"])
print("BACKEND:", sorted(n for n in sys.modules if n == "app" or n.startswith("app.")))
"""


def test_counting_genes_never_imports_the_backend():
    """A fresh interpreter, so this module's own app imports cannot mask one."""
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    done = subprocess.run([sys.executable, "-c", _NO_BACKEND_SCRIPT, str(ROOT)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-4000:]
    assert "BACKEND: []" in done.stdout, done.stdout[-2000:]
