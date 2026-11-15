"""An option STYLE genome (``--strategy O_STYLE``) survives the walk-forward out-of-sample re-run, whole.

The style walk-forward cell (tools/run_option_style_wf.py, ``option_style_job.sh --walk-forward``) tests
each fold's picks through the same path as every walk-forward: ``run_walk_forward.RealRunner.select``
(the genome raw from the train job's ``all_results``) -> ``_persist_top_backtests(window=)`` ->
``decode_params`` + ``_build_daily_trial_config``. A style genome is the widest the GA searches: every
member's entry/exit genes (its toggle and structure share included), the shared option-selection
weights (``optsel``), the market-condition gates of BOTH stage-2 profiles and the SPY regime gates
(``cond:`` genes the launcher adds when ``--market-condition-profile`` / ``--benchmark-regime-gates``
are on), and the run-level screener genes. Re-runs have dropped genes before (the ``optsel`` weights
until 2026-10-13, stored row pins over the genome), so this pins, for the stage-2 style ``long10k``
built by the launcher with both market-condition profiles and the SPY gates on, and EVERY searched gene
off its default:

* the out-of-sample trial config is the one the GA builds for that genome on the test window;
* every searched gene moves that trial config (none is dropped on the way);
* the run-level screener genes reach the trial build with the run's hoisted screen state;
* the persisted out-of-sample row carries the whole genome.

Only the engine call (``_persist_trial_worker``) and the screener's hoisted state are replaced;
nothing is backtested.
"""
from __future__ import annotations

import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from app.services import walk_forward as WF
from tests.test_walk_forward import D

_TOOLS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

STYLES = os.path.join(_TOOLS, "option_styles.stage2.json")
STYLE = "long10k"
PROFILES = ["ohlcv-v1", "ta-structure-v1"]
TRAIN = ("2020-01-02", "2023-12-29")
TEST = (D("2024-01-01"), D("2024-06-28"))
_RANDOM_KEYS = ("backtest_id", "name", "persist_trading_db", "ga_fitness")


def _off_default(spec):
    """The far end of the gene's range, as the GA STORES it (a choice gene as its choice VALUE)."""
    if spec["type"] == "choice":
        return spec["choices"][-1]
    return spec["max"]


def _alt(spec, value):
    if spec["type"] == "choice":
        return spec["choices"][0] if value != spec["choices"][0] else spec["choices"][-1]
    return spec["min"] if value != spec["min"] else spec["max"]


@pytest.fixture
def world(monkeypatch):
    import app.models  # noqa: F401
    from app.models.database import Base, SessionLocal, engine
    from app.models.strategy_optimization import StrategyOptimization
    from app.services import strategy_optimization_handler as soh
    from app.services import sync_client
    from app.services.strategy_param_space import collect_param_space
    from tests.test_gene_to_artefact_audit import _backtest_cfg
    import run_walk_forward as RWF

    Base.metadata.create_all(bind=engine)
    runner = RWF.RealRunner(SimpleNamespace(min_trade_rows=30, min_return_rel_pct=5.0, min_dd_pts=2.0,
                                            min_trades_rel_pct=5.0, min_free_gb=0.0, force_memory=True))
    L = runner.launcher_module()            # the launcher the driver really uses
    # what --style-config/--style, --market-condition-profile and --benchmark-regime-gates set at launch
    monkeypatch.setattr(L, "_STYLE_ACTIVE", L._load_style_spec(STYLES, STYLE))
    monkeypatch.setattr(L, "_MARKET_CONDITION_PROFILES", list(PROFILES))
    monkeypatch.setattr(L, "_BENCHMARK_REGIME_GATES", True)
    monkeypatch.setattr(sync_client, "push_backtest", lambda bt, db: None)
    seen, built_with = [], []

    def worker(cfg):
        seen.append(cfg)
        return {"ok": True, "results": {
            "total_trades": 6, "winning_trades": 4, "losing_trades": 2, "win_rate": 66.7,
            "total_return": 7.5, "annualized_return": 15.0, "buy_hold_return": 0.0,
            "sharpe_ratio": 1.0, "sortino_ratio": 1.2, "calmar_ratio": 0.8, "volatility": 9.0,
            "max_drawdown": -6.0, "avg_drawdown": -2.0, "max_drawdown_duration": 1.0,
            "profit_factor": 2.0, "expectancy": 1.0, "sqn": 0.5, "avg_trade": 1.0,
            "best_trade": 4.0, "worst_trade": -2.0, "avg_trade_duration": 9.0,
            "exposure_time": 30.0, "final_equity": 10_750.0, "equity_peak": 11_000.0,
            "equity_curve": [{"date": "2024-01-02", "equity": 10_000.0}],
            "drawdown_curve": [{"date": "2024-01-02", "drawdown": 0.0}], "trades": []}}
    monkeypatch.setattr(soh, "_persist_trial_worker", worker)
    real_build = soh._build_daily_trial_config

    def spy_build(block, decoded, hoisted=None, **kw):
        built_with.append((decoded, hoisted))
        return real_build(block, decoded, hoisted, **kw)
    monkeypatch.setattr(soh, "_build_daily_trial_config", spy_build)
    hoisted = {"marker": "the run's hoisted screen state"}         # no screener_store: no screen is computed
    monkeypatch.setattr(soh, "_build_hoisted_state", lambda block: hoisted)
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(L, "_new_local_pool", lambda n: ThreadPoolExecutor(max_workers=n))

    built = L._build_strategy("O_STYLE", f"wfstyle-{STYLE}", "DeterministicScorer")
    space = collect_param_space(built)
    screener = {f"screener:{k}": v for k, v in L._SCREENER_OPT.items()}
    genome = {gene: _off_default(spec) for gene, spec in {**space, **screener}.items()}
    # a screen block WITHOUT the superset rule: the test-window universe recompute is not under test here
    block = dict(_backtest_cfg(built, "DeterministicScorer"), start_date=TRAIN[0], end_date=TRAIN[1],
                 labels=["optwf-style-test"], estimates_source="fmp",
                 screener_opt={"store": "S", "base_settings": {"market_cap_min": 1e10}},
                 market_condition_profiles=list(PROFILES))
    name = f"optwf-test-O_STYLE-{STYLE}-{uuid.uuid4().hex[:8]}-wf1"     # one row per name: the DB is shared
    db = SessionLocal()
    try:
        db.add(built); db.commit(); db.refresh(built)
        opt = StrategyOptimization(
            strategy_id=built.id, name=name, fitness_metric="option_long_dd40_50",
            optimization_type="genetic", optimization_config={"backtest": block},
            parameter_ranges={**space, **screener},
            all_results=[{"params": dict(genome), "fitness": 2.5, "total_return": 40.0,
                          "max_drawdown": -12.0, "trades": 80, "key": "k-genome"}],
            best_params=dict(genome), best_fitness=2.5, status="completed")
        db.add(opt); db.commit(); db.refresh(opt)
        opt_id = opt.id
    finally:
        db.close()
    return SimpleNamespace(L=L, runner=runner, seen=seen, built_with=built_with, genome=genome, space=space,
                           screener=screener, block=block, name=name, opt_id=opt_id, hoisted=hoisted,
                           real_build=real_build)


def _ga_trial(world, genome, hoisted=None):
    """What the GA's own assembly builds for ``genome`` on the test window, from a FRESH launcher-built
    style (not the DB copy the re-run reads)."""
    from app.services.strategy_param_space import decode_params
    fresh = world.L._build_strategy("O_STYLE", f"wfstyle-{STYLE}", "DeterministicScorer")
    block = dict(world.block, start_date=TEST[0].isoformat(), end_date=TEST[1].isoformat())
    return world.real_build(block, decode_params(fresh, genome), hoisted, option_trade_records=True)


def _strip(cfg):
    return {k: v for k, v in cfg.items() if k not in _RANDOM_KEYS}


def _rerun(world, n=3):
    picks = world.runner.select(world.name, n)
    fold = WF.Fold(1, D(TRAIN[0]), D(TRAIN[1]), *TEST)
    return picks, world.runner.run_oos(world.name, fold, picks, WF.oos_name_prefix(1), WF.oos_labels(1), 1)


def test_the_style_genome_is_wide_and_off_default(world):
    """Guard for the tests below: the genome spans every gene family of a stage-2 style job."""
    g = world.genome
    assert any(k.startswith("optsel:") for k in g)                                     # selection weights
    assert any(k.startswith("entry:") for k in g) and any(k.startswith("exit:") for k in g)
    assert len({k.split(":")[1].split("-")[0] for k in g if k.startswith("entry:")}) > 1   # several members
    assert sum(1 for k in g if k.startswith("cond:")) > 100                            # + MC and SPY gates
    assert set(world.screener) <= set(g)
    assert _strip(_ga_trial(world, g)) != _strip(_ga_trial(world, {}))


def test_the_out_of_sample_rerun_builds_the_gas_trial_on_the_test_window(world):
    picks, out = _rerun(world)
    assert [p.rank for p in picks] == [1] and picks[0].params == world.genome
    assert set(out) == {1} and out[1].trades == 6 and out[1].reused is False
    assert len(world.seen) == 1
    cfg = world.seen[0]
    assert (cfg["start_date"], cfg["end_date"]) == (TEST[0].isoformat(), TEST[1].isoformat())
    assert _strip(cfg) == _strip(_ga_trial(world, world.genome, world.hoisted))
    weights = {g.split(":")[-1]: v for g, v in world.genome.items() if g.startswith("optsel:")}
    actions = [a for r in cfg["entry_rules"] for a in r["actions"] if "option_w_premium" in a]
    assert actions and all(a[f"option_{w}"] == v for a in actions for w, v in weights.items())


def test_the_screener_genes_reach_the_trial_build_with_the_runs_hoisted_state(world):
    _rerun(world)
    decoded, hoisted = world.built_with[-1]
    assert hoisted is world.hoisted
    assert decoded["screener_overrides"] == {k[len("screener:"):]: v for k, v in world.genome.items()
                                             if k.startswith("screener:")}


def test_every_searched_gene_moves_the_out_of_sample_trial(world):
    """No strategy gene is dropped between the stored genome and the trial (a gene inert BY DESIGN under
    another gene's choice is checked under a second genome with every choice at its first value)."""
    first = {g: (s["choices"][0] if s["type"] == "choice" else world.genome[g]) for g, s in world.space.items()}
    base = {g: world.genome[g] for g in world.space}
    inert = []
    for gene, spec in world.space.items():
        if not any(_strip(_ga_trial(world, dict(g, **{gene: _alt(spec, g[gene])})))
                   != _strip(_ga_trial(world, g)) for g in (base, first)):
            inert.append(gene)
    assert not inert, f"genes with no effect on the out-of-sample trial config: {inert}"


def test_the_persisted_out_of_sample_row_carries_the_whole_genome(world):
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    _picks, out = _rerun(world, 1)
    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter_by(id=out[1].backtest_id).one()
        assert bt.name == WF.oos_backtest_name(1, 1, world.name)
        assert {g: bt.strategy_params[g] for g in world.genome} == world.genome
        assert bt.labels == ["optwf-style-test", "WalkForward", "wf-fold-1", "OOS"]
        assert bt.ga_fitness == 2.5
    finally:
        db.close()
