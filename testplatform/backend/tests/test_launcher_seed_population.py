"""``ba2-test optimize --seed-population BUNDLE [--seed-check-only]`` (stage-2 job A, T7).

Drives the REAL ``_cmd_optimize`` (GA stubbed) on a two-member style written to ``tmp_path``. The bundle is
built in-test from each member's own ``authored_genome`` plus the style's shared genes read off the space the
launcher computed (captured through a stub of ``_assemble_seed_population``).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L  # noqa: E402

from tests.test_equity_cap_launcher import _BASE_ARGV, _isolated_globals, _parse, _restore_globals, _run_optimize  # noqa: E402

_REPO = Path(__file__).resolve().parents[3]
_STAGE2 = json.loads((_REPO / "tools" / "option_styles.stage2.json").read_text())
_MEMBERS = ("O_LC", "O_BULLCS")


@pytest.fixture(autouse=True)
def _no_style_leaks(monkeypatch):
    monkeypatch.setattr(L, "_STYLE_ACTIVE", None)
    monkeypatch.setattr(L, "_MARKET_CONDITION_PROFILES", L._MARKET_CONDITION_PROFILES)
    yield


@pytest.fixture
def style_cfg(tmp_path):
    mid = _STAGE2["styles"]["mid"]
    spec = {"schema": 1, "styles": {"tst": {
        "status": "approved", "note": "test style", "price_cap_policy": "none",
        "members": [{"key": k} for k in _MEMBERS], "risk": mid["risk"]}}}
    p = tmp_path / "styles.json"
    p.write_text(json.dumps(spec))
    return str(p)


def _argv(style_cfg, *extra, name="seedtest", style="tst", strategy="O_STYLE"):
    argv = ["optimize", "--expert", "FMPRating", "--strategy", strategy, "--universe", "AAPL",
            "--start", "2024-03-01", "--end", "2024-04-01", "--population", "10",
            "--generations", "1", "--rerun", "--name", f"{name}-{os.getpid()}"]
    if style_cfg:
        argv += ["--style-config", style_cfg, "--style", style]
    return argv + list(extra)


def _go(argv, monkeypatch, check_only_expected=False):
    """Run the real command with the GA stubbed -> (rc, persisted optimization_config or None)."""
    import app.services.strategy_optimization_handler as SOH
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization

    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    monkeypatch.setattr(SOH, "handle_strategy_optimization", lambda t, p: {"status": "completed"})
    monkeypatch.setattr(L, "_persist_top_backtests", lambda *a, **k: 0)
    args = _parse(argv)
    saved = _isolated_globals()
    try:
        rc = L._cmd_optimize(args)
    finally:
        _restore_globals(saved)
    db = SessionLocal()
    try:
        row = db.query(StrategyOptimization).order_by(StrategyOptimization.id.desc()).first()
        return rc, (row.optimization_config if row is not None else None)
    finally:
        db.close()


def _counts():
    from app.models.database import SessionLocal
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    db = SessionLocal()
    try:
        return db.query(Strategy).count(), db.query(StrategyOptimization).count()
    finally:
        db.close()


def _space_of(style_cfg, monkeypatch):
    """The (strat, space, anchor) the launcher computes for this style, captured via a stub."""
    seen = {}

    def _capture(bundle, strat, space, anchor, population):
        seen.update(strat=strat, space=space, anchor=anchor)
        raise SystemExit("captured")

    monkeypatch.setattr(L, "_assemble_seed_population", _capture)
    dummy = Path(style_cfg).with_name("dummy.json")
    dummy.write_text(json.dumps({"style": "tst", "individuals": [{"contributions": []}]}))
    with pytest.raises(SystemExit, match="captured"):
        _go(_argv(style_cfg, "--seed-population", str(dummy), "--seed-check-only"), monkeypatch)
    monkeypatch.undo()
    return seen


def _bundle(style_cfg, tmp_path, monkeypatch, *, individuals=None, style="tst", fill=None, out_of_range="clamp"):
    """A bundle whose member genes are each member's own authored genome (member key space) and whose shared
    genes (model:/schedule:) come from the space's first value."""
    from app.services.strategy_param_space import authored_genome, collect_param_space
    seen = _space_of(style_cfg, monkeypatch)
    space = seen["space"]
    shared = {}
    for k, cfg in space.items():
        if k.partition(":")[0] in ("model", "schedule"):
            shared[k] = cfg["choices"][0] if cfg["type"] == "choice" else cfg["min"]
    contribs = {}
    for m in _MEMBERS:
        src = L._STRATEGY_BUILDERS[m](m)
        msp = collect_param_space(src)
        genes = authored_genome(src, msp)
        contribs[m] = {"member": m, "genes": {**genes, **shared},
                       "resolved": {"opt_id": 1, "opt_name": "x", "status": "completed", "pick": "best",
                                    "rank": None, "fitness": 1.0, "trades": None, "params_key": None,
                                    "backtest_id": None}}
    inds = individuals or [
        {"contributions": [contribs["O_LC"]], "shared_from": 0, "other_members": "off"},
        {"contributions": [contribs["O_BULLCS"]], "shared_from": 0, "other_members": "off"},
        {"contributions": [contribs["O_LC"], contribs["O_BULLCS"]], "shared_from": 0, "other_members": "authored"}]
    b = {"schema": 1, "style": style, "individuals": inds, "fill": fill if fill is not None else {},
         "out_of_range": out_of_range, "policy_sha256": "0" * 64}
    p = tmp_path / "bundle.json"
    p.write_text(json.dumps(b))
    return p, space


def test_a_bundle_becomes_seedPopulation_provenance_and_the_bundle_hash(style_cfg, tmp_path, monkeypatch):
    fill = _fill_for(style_cfg, monkeypatch)
    path, space = _bundle(style_cfg, tmp_path, monkeypatch, fill=fill)
    rc, cfg = _go(_argv(style_cfg, "--seed-population", str(path)), monkeypatch)
    assert rc == 0
    assert len(cfg["seedPopulation"]) == 3
    assert all(set(g) == set(space) for g in cfg["seedPopulation"])           # full coverage, nothing extra
    assert cfg["backtest"]["seed_bundle_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert cfg["seedProvenance"][0]["contributions"][0]["member"] == "O_LC"
    assert cfg["seedProvenance"][2]["other_members"] == "authored"
    assert cfg["requireAnEntryRule"] is True
    # every assembled seed is strict-encodable by the optimizer the handler builds
    from app.services.genetic import GeneticOptimizer
    opt = GeneticOptimizer(param_ranges=space, population_size=10, n_generations=1)
    for g in cfg["seedPopulation"]:
        opt.encode_params_strict(g)


def _fill_for(style_cfg, monkeypatch):
    """Fill patterns for the style's risk genes (absent from the stage-1 style of the bundle)."""
    seen = _space_of(style_cfg, monkeypatch)
    return {k: seen["space"][k]["max"] for k in seen["space"] if k.startswith("model:max_deployment")}


def test_check_only_prints_the_report_and_writes_nothing(style_cfg, tmp_path, monkeypatch, capsys):
    path, _ = _bundle(style_cfg, tmp_path, monkeypatch)
    before = _counts()
    rc, _cfg = _go(_argv(style_cfg, "--seed-population", str(path), "--seed-check-only", "--name", "chk"),
                   monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0 and _counts() == before
    assert "seed population: 3 seeds over" in out and "seed #2: members=O_LC,O_BULLCS" in out
    assert "--seed-check-only: bundle accepted; nothing was written" in out


def test_check_only_does_not_skip_an_already_completed_name(style_cfg, tmp_path, monkeypatch, capsys):
    path, _ = _bundle(style_cfg, tmp_path, monkeypatch)
    argv = [a for a in _argv(style_cfg, "--seed-population", str(path), "--seed-check-only") if a != "--rerun"]
    rc, _c = _go(argv, monkeypatch)
    assert rc == 0 and "SKIP" not in capsys.readouterr().out


def test_an_unfilled_shared_gene_is_a_launch_refusal_not_a_trial_failure(style_cfg, tmp_path, monkeypatch):
    path, _ = _bundle(style_cfg, tmp_path, monkeypatch)       # then drop a risk gene: nothing answers it
    b = json.loads(path.read_text())
    for ind in b["individuals"]:
        for c in ind["contributions"]:
            del c["genes"]["model:max_deployment_pct"]
    path.write_text(json.dumps(b))
    before = _counts()
    with pytest.raises(SystemExit, match=r"seed bundle refused: seed #0: .*model:max_deployment_pct"):
        _go(_argv(style_cfg, "--seed-population", str(path)), monkeypatch)
    assert _counts() == before


def test_an_unplaceable_seed_key_is_a_launch_refusal_naming_seed_and_key(style_cfg, tmp_path, monkeypatch):
    path, _ = _bundle(style_cfg, tmp_path, monkeypatch, fill=_fill_for(style_cfg, monkeypatch))
    b = json.loads(path.read_text())
    b["individuals"][1]["contributions"][0]["genes"]["cond:nonexistent-leaf:value"] = 1.0
    path.write_text(json.dumps(b))
    with pytest.raises(SystemExit, match=r"seed #1: O_BULLCS: cannot place gene key 'cond:nonexistent-leaf:value'"):
        _go(_argv(style_cfg, "--seed-population", str(path)), monkeypatch)


def test_seed_population_requires_o_style(style_cfg, tmp_path, monkeypatch):
    path, _ = _bundle(style_cfg, tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="applies only to --strategy O_STYLE"):
        _go(_argv(None, "--seed-population", str(path), strategy="S2"), monkeypatch)


@pytest.mark.parametrize("flag", [["--warm-start-from", "1"], ["--chain-seed-from", "x"]])
def test_seed_population_is_exclusive_with_warm_and_chain_seeds(style_cfg, tmp_path, monkeypatch, flag):
    path, _ = _bundle(style_cfg, tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="mutually exclusive"):
        _go(_argv(style_cfg, "--seed-population", str(path), *flag), monkeypatch)


def test_a_bundle_for_another_style_is_refused(style_cfg, tmp_path, monkeypatch):
    path, _ = _bundle(style_cfg, tmp_path, monkeypatch, style="premium")
    with pytest.raises(SystemExit, match="built for style 'premium', this job is --style 'tst'"):
        _go(_argv(style_cfg, "--seed-population", str(path)), monkeypatch)


def test_check_only_without_a_bundle_is_refused(style_cfg, monkeypatch):
    with pytest.raises(SystemExit, match="--seed-check-only needs --seed-population"):
        _go(_argv(style_cfg, "--seed-check-only"), monkeypatch)


def test_more_seeds_than_population_is_refused(style_cfg, tmp_path, monkeypatch):
    path, _ = _bundle(style_cfg, tmp_path, monkeypatch, fill=_fill_for(style_cfg, monkeypatch))
    argv = _argv(style_cfg, "--seed-population", str(path))
    argv[argv.index("--population") + 1] = "2"
    with pytest.raises(SystemExit, match="3 seeds exceed --population 2"):
        _go(argv, monkeypatch)


def test_o_style_without_seeds_still_requires_an_entry_rule(style_cfg, monkeypatch):
    rc, cfg = _go(_argv(style_cfg), monkeypatch)
    assert rc == 0 and cfg["requireAnEntryRule"] is True
    assert "seedPopulation" not in cfg and "seed_bundle_sha256" not in cfg["backtest"]


def test_non_style_job_configs_are_unchanged(monkeypatch):
    cfg = _run_optimize(_parse(_BASE_ARGV), monkeypatch)
    assert "requireAnEntryRule" not in cfg and "seedPopulation" not in cfg and "seedProvenance" not in cfg
    assert "seed_bundle_sha256" not in cfg["backtest"]


# --- the extracted gene-space code, and the checkpoint identity ------------------------------------------------
@pytest.mark.parametrize("kind", ["S2", "O_LC", "OS1", "O_WHEEL"])
def test_job_param_space_equals_the_old_inline_construction(kind):
    from app.services.strategy_optimization_handler import job_param_space
    from app.services.strategy_param_space import collect_param_space
    strat = L._build_strategy(kind, f"pin-{kind}", "FMPRating")
    expert_cfg = {**L._EXPERT_OPT["FMPRating"]["expert_params"],
                  "screener:max_stocks": {"optimize": True, "type": "int", "min": 5, "max": 20, "step": 5},
                  "schedule:monday": {"optimize": True, "type": "choice", "choices": [0, 1]}}
    # the handler's former inline code, verbatim
    model_cfg = {k: v for k, v in expert_cfg.items() if not k.startswith("screener:") and not k.startswith("schedule:")}
    screener_cfg = {k[len("screener:"):]: v for k, v in expert_cfg.items() if k.startswith("screener:")} or None
    schedule_cfg = {k[len("schedule:"):]: v for k, v in expert_cfg.items() if k.startswith("schedule:")} or None
    old = collect_param_space(strat, expert_cfg=model_cfg, bypass=False, screener_cfg=screener_cfg,
                              schedule_cfg=schedule_cfg)
    assert job_param_space(strat, expert_cfg, False) == old
    assert list(job_param_space(strat, expert_cfg, False)) == list(old)         # gene ORDER is part of identity
    assert job_param_space(strat, None, False) == collect_param_space(strat)


def test_the_checkpoint_fingerprint_covers_the_seed_population_only_when_present():
    from app.services.strategy_optimization_handler import checkpoint_fingerprint
    space = {"a": {"type": "int", "min": 0, "max": 5, "step": 1}}
    ga = {"populationSize": 10, "generations": 3}
    base = checkpoint_fingerprint(space, ga)
    assert checkpoint_fingerprint(space, {**ga, "requireAnEntryRule": True}) == base          # existing jobs unchanged
    s1 = checkpoint_fingerprint(space, {**ga, "seedPopulation": [{"a": 1}]})
    s2 = checkpoint_fingerprint(space, {**ga, "seedPopulation": [{"a": 2}]})
    assert len({base, s1, s2}) == 3
    assert checkpoint_fingerprint(space, {**ga, "seedPopulation": [{"a": 1}]}) == s1           # deterministic
