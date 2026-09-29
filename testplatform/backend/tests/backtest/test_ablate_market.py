"""``tools/strategy_research/atr_grid/ablate_market.py`` -- the goal2027atr market-off ablation.

Pinned here:

* pure logic (no DB, no engine): genome forcing touches ONLY ``market:enabled``; naming; the
  refusal cases (no market-condition profile on the optimization; a genome with no master gene).
* the DB-touching orchestration, with a STUBBED backtest runner (the real
  ``decode_params``/``_build_daily_trial_config`` reconstruction runs for real -- this tool reuses
  the platform's existing re-run path and writes no new backtest runner, only the ENGINE call
  itself is stubbed): resume skip (an existing ``ABL-MKTOFF-...`` row is not re-run), the
  persisted row's name/labels/metadata, and that ONLY ``market:enabled`` differs between the
  genome the original TOP-N row ran with and the one the ablation run decodes.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- registers every model class on Base.metadata
from app.models.database import Base
from app.models.backtest import Backtest
from app.models.strategy import Strategy as StrategyModel
from app.models.strategy_optimization import StrategyOptimization

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOL_PATH = os.path.normpath(os.path.join(
    _HERE, "..", "..", "..", "..", "tools", "strategy_research", "atr_grid", "ablate_market.py"))

_spec = importlib.util.spec_from_file_location("ablate_market", _TOOL_PATH)
AM = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AM)


# ==================================================================================================
# pure logic
# ==================================================================================================
def test_market_condition_profiles_reads_the_resolved_list():
    assert AM.market_condition_profiles(None) == []
    assert AM.market_condition_profiles({}) == []
    assert AM.market_condition_profiles({"backtest": {}}) == []
    assert AM.market_condition_profiles(
        {"backtest": {"market_condition_profiles": ["ohlcv-v1", "ta-structure-v1"]}}
    ) == ["ohlcv-v1", "ta-structure-v1"]


def test_require_market_profile_refuses_an_ungated_optimization():
    with pytest.raises(ValueError, match="nothing to ablate"):
        AM.require_market_profile("plain-opt", {"backtest": {}})
    with pytest.raises(ValueError, match="nothing to ablate"):
        AM.require_market_profile("plain-opt", None)
    AM.require_market_profile("gated-opt", {"backtest": {"market_condition_profiles": ["ohlcv-v1"]}})


def test_topn_rank_and_ablation_name():
    assert AM.topn_rank("TOP3-sen-S1-atr27") == 3
    assert AM.topn_rank("TOP12-x") == 12
    assert AM.topn_rank("BEST-sen-S1-atr27") is None
    assert AM.topn_rank("random") is None
    assert AM.ablation_name("TOP3-sen-S1-atr27") == "ABL-MKTOFF-TOP3-sen-S1-atr27"


def test_force_market_off_changes_only_that_one_key():
    genome = {"model:risk_per_trade_pct": 1.5, "cond:s1-market-adx:mode": "above",
             "cond:s1-market-adx:value": 30.0, "market:enabled": 1, "schedule:monday": 1}
    out = AM.force_market_off(genome)
    assert out["market:enabled"] == 0
    # every other key is UNCHANGED, and no key was added or removed.
    without_master = {k: v for k, v in out.items() if k != "market:enabled"}
    expected_without_master = {k: v for k, v in genome.items() if k != "market:enabled"}
    assert without_master == expected_without_master
    assert set(out) == set(genome)
    # the input is not mutated.
    assert genome["market:enabled"] == 1


def test_force_market_off_refuses_a_genome_without_the_master_gene():
    with pytest.raises(ValueError, match="market:enabled"):
        AM.force_market_off({"model:risk_per_trade_pct": 1.5, "cond:s1-market-adx:mode": "above"})
    with pytest.raises(ValueError):
        AM.force_market_off({})


def test_delta_row_computes_the_five_metric_deltas():
    original = {"fitness": 2.0, "car": 10.0, "max_drawdown": -5.0, "total_return": 40.0, "trades": 20}
    ablated = {"fitness": 1.5, "car": 8.0, "max_drawdown": -4.0, "total_return": 30.0, "trades": 15}
    row = AM.delta_row("TOP1-x", 1, original, ablated)
    assert row["fitness"] == 2.0 and row["fitness_off"] == 1.5
    assert row["fitness_delta"] == pytest.approx(-0.5)
    assert row["car_delta"] == pytest.approx(-2.0)
    assert row["max_drawdown_delta"] == pytest.approx(1.0)
    assert row["total_return_delta"] == pytest.approx(-10.0)
    assert row["trades_delta"] == -5


def test_delta_row_handles_missing_metrics_without_raising():
    row = AM.delta_row("TOP1-x", 1, {"fitness": None, "car": None, "max_drawdown": None,
                                     "total_return": None, "trades": None},
                       {"fitness": 1.0, "car": None, "max_drawdown": None, "total_return": None,
                        "trades": None})
    assert row["fitness_delta"] is None  # original was None
    assert row["car_delta"] is None


def test_render_csv_and_markdown_and_print_table(capsys):
    rows = [AM.delta_row("TOP1-x", 1, {"fitness": 2.0, "car": 10.0, "max_drawdown": -5.0,
                                       "total_return": 40.0, "trades": 20},
                         {"fitness": 1.5, "car": 8.0, "max_drawdown": -4.0, "total_return": 30.0,
                          "trades": 15})]
    csv_text = AM.render_csv(rows)
    assert "source,rank,fitness" in csv_text.splitlines()[0]
    assert "TOP1-x" in csv_text
    md_text = AM.render_markdown(rows)
    assert md_text.startswith("| source |")
    assert "TOP1-x" in md_text
    AM.print_table(rows)
    out = capsys.readouterr().out
    assert "TOP1-x" in out
    AM.print_table([])
    assert "no rows" in capsys.readouterr().out


def test_write_report_picks_format_from_extension(tmp_path):
    rows = [AM.delta_row("TOP1-x", 1, {"fitness": 2.0, "car": 10.0, "max_drawdown": -5.0,
                                       "total_return": 40.0, "trades": 20},
                         {"fitness": 1.5, "car": 8.0, "max_drawdown": -4.0, "total_return": 30.0,
                          "trades": 15})]
    csv_path = tmp_path / "out.csv"
    AM.write_report(rows, str(csv_path))
    assert csv_path.read_text(encoding="utf-8").startswith("source,rank,fitness")
    md_path = tmp_path / "out.md"
    AM.write_report(rows, str(md_path))
    assert md_path.read_text(encoding="utf-8").startswith("| source |")


# ==================================================================================================
# DB-touching orchestration, with a stubbed backtest runner
# ==================================================================================================
def _market_leaf():
    return {"id": "s1-market-adx", "field": "underlying_adx_14", "op": "<", "comparison": "<",
            "value": 25.0, "optimize": True, "value_min": 10.0, "value_max": 40.0,
            "value_step": 5.0, "mode_optimize": True, "mode_choices": ["off", "below", "above"]}


def _entry_rules():
    return [{"id": "s1-entry", "name": "S1-entry", "continue_processing": False,
            "actions": [{"action_type": "buy"}],
            "conditions": {"id": "s1-root", "type": "AND",
                           "conditions": [_market_leaf()]}}]


def _bt_block(strat, expert="FMPRating"):
    return {
        "backtest_id": "ablation-test", "start_date": "2024-02-01", "end_date": "2024-06-01",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": expert, "settings": {"allow_automated_trade_opening": True}}],
        "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
        "entry_action": getattr(strat, "entry_action", None),
        "options_store": "parquet", "execution_interval": "1d",
        "market_condition_profiles": ["ohlcv-v1"],
        "market_condition_manifests": {"ohlcv-v1": "a" * 64},
    }


@pytest.fixture
def db(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("ablatedb") / "ablate.sqlite"
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()
    yield session
    session.close()
    eng.dispose()


def _seed(db, *, gated=True, master_gene=True, n_top=2):
    """A completed optimization + N completed TOP-N rows, gated with an ohlcv-v1 market leaf."""
    strat = StrategyModel(name="ablation-strat", entry_rules=_entry_rules(), exit_rules=[])
    db.add(strat)
    db.commit()
    db.refresh(strat)

    bt_block = _bt_block(strat)
    if not gated:
        bt_block.pop("market_condition_profiles")
        bt_block.pop("market_condition_manifests")
    opt = StrategyOptimization(
        strategy_id=strat.id, name="sen-S1-atr27-test", fitness_metric="consistent_annual_return",
        optimization_type="genetic", status="completed",
        optimization_config={"populationSize": 10, "generations": 1, "seed": 1, "backtest": bt_block},
    )
    db.add(opt)
    db.commit()
    db.refresh(opt)

    rows = []
    for k in range(1, n_top + 1):
        genome = {"cond:s1-market-adx:mode": "above", "cond:s1-market-adx:value": 30.0}
        if master_gene:
            genome["market:enabled"] = 1
        bt = Backtest(
            name=f"TOP{k}-sen-S1-atr27-test", expert_name="FMPRating", engine_type="daily_expert",
            optimization_id=opt.id, status="completed", strategy_params=genome,
            start_date=datetime(2024, 2, 1), end_date=datetime(2024, 6, 1),
            initial_capital=20_000.0, ga_fitness=2.0 - 0.1 * k, annualized_return=15.0 - k,
            max_drawdown=-5.0 - k, total_return=40.0 - k, total_trades=20 - k,
        )
        db.add(bt)
        db.commit()
        db.refresh(bt)
        rows.append(bt)
    return strat, opt, rows


def _stub_runner(results):
    """A fake ``run_daily_backtest``: ignores the trial config, returns a canned results dict."""
    calls = []

    def runner(trial_cfg):
        calls.append(trial_cfg)
        return dict(results)

    runner.calls = calls
    return runner


_RESULTS = {
    "total_trades": 12, "winning_trades": 7, "losing_trades": 5, "win_rate": 0.583,
    "total_return": 22.0, "sharpe_ratio": 1.1, "max_drawdown": -3.0, "profit_factor": 1.4,
    "avg_trade_duration": 5.0, "final_equity": 24_400.0, "equity_curve": [], "drawdown_curve": [],
    "trades": [], "annualized_return": 9.0,
}


def test_find_target_optimizations_filters_by_pattern_and_status(db):
    _strat, opt, _rows = _seed(db)
    other = StrategyOptimization(
        strategy_id=opt.strategy_id, name="unrelated-job", fitness_metric="consistent_annual_return",
        optimization_type="genetic", status="completed",
        optimization_config={"backtest": {}},
    )
    running = StrategyOptimization(
        strategy_id=opt.strategy_id, name="sen-S2-atr27-running", fitness_metric="x",
        optimization_type="genetic", status="running", optimization_config={"backtest": {}},
    )
    db.add_all([other, running])
    db.commit()
    found = AM.find_target_optimizations(db, "%atr27%")
    assert [o.id for o in found] == [opt.id]
    assert AM.find_target_optimizations(db, "%nomatch%") == []
    assert AM.find_target_optimizations(db, "%atr27%", opt_ids=[opt.id + 999]) == []


def test_find_topn_rows_orders_by_rank_and_ignores_non_topn(db):
    strat, opt, rows = _seed(db, n_top=3)
    other = Backtest(name="BEST-sen-S1-atr27-test", expert_name="FMPRating",
                     engine_type="daily_expert", optimization_id=opt.id, status="completed",
                     start_date=datetime(2024, 2, 1), end_date=datetime(2024, 6, 1),
                     initial_capital=20_000.0)
    db.add(other)
    db.commit()
    found = AM.find_topn_rows(db, opt.id)
    assert [b.name for b in found] == [r.name for r in rows]


def test_ablate_optimization_refuses_an_ungated_job(db):
    _strat, opt, _rows = _seed(db, gated=False)
    with pytest.raises(ValueError, match="nothing to ablate"):
        AM.ablate_optimization(db, opt, runner=_stub_runner(_RESULTS))


def test_ablate_optimization_refuses_a_row_with_no_master_gene_but_continues_other_rows(db, capsys):
    """One TOP-N row predates the master gene (no genome key); the gated optimization itself is
    fine, so its OTHER rows must still be ablated -- a single bad row is not a whole-job refusal."""
    strat, opt, rows = _seed(db, n_top=2)
    rows[0].strategy_params = {"cond:s1-market-adx:mode": "above"}  # no market:enabled
    db.commit()
    runner = _stub_runner(_RESULTS)
    result = AM.ablate_optimization(db, opt, runner=runner)
    assert len(result) == 1  # only rows[1] ablated
    assert result[0]["source"] == rows[1].name
    assert "refusing" in capsys.readouterr().out
    assert len(runner.calls) == 1


def test_ablate_optimization_persists_a_row_named_and_labelled_correctly(db):
    strat, opt, rows = _seed(db, n_top=1)
    source = rows[0]
    runner = _stub_runner(_RESULTS)
    result = AM.ablate_optimization(db, opt, runner=runner)
    assert len(result) == 1
    persisted = db.query(Backtest).filter(Backtest.name == "ABL-MKTOFF-TOP1-sen-S1-atr27-test").first()
    assert persisted is not None
    assert persisted.status == "completed"
    assert persisted.optimization_id == opt.id
    assert AM.ABLATION_LABEL in (persisted.labels or [])
    assert f"ablation-source:{source.id}" in (persisted.labels or [])
    assert persisted.strategy_params["ablation_source_backtest_id"] == source.id
    assert persisted.strategy_params["ablation_source_backtest_name"] == source.name
    assert persisted.strategy_params["market:enabled"] == 0
    assert persisted.total_return == _RESULTS["total_return"]
    assert persisted.max_drawdown == _RESULTS["max_drawdown"]
    assert persisted.total_trades == _RESULTS["total_trades"]
    assert persisted.ga_fitness is not None


def test_only_market_enabled_differs_between_the_original_genome_and_the_ablated_decode(db):
    """The core correctness property: the trial config the STUBBED runner receives decodes from a
    genome equal to the source row's genome except for ``market:enabled`` -- proven by decoding
    both independently and diffing the flat genome dicts the config was built from."""
    from app.services.backtest.rerun_handler import _gene_params
    from app.services.strategy_param_space import decode_params

    strat, opt, rows = _seed(db, n_top=1)
    source = rows[0]
    runner = _stub_runner(_RESULTS)
    AM.ablate_optimization(db, opt, runner=runner)
    assert len(runner.calls) == 1

    original_genome = _gene_params(source.strategy_params)
    ablated_genome = AM.force_market_off(original_genome)
    only_diff = {k for k in set(original_genome) | set(ablated_genome)
                if original_genome.get(k) != ablated_genome.get(k)}
    assert only_diff == {"market:enabled"}

    # and the trial config the runner received really did decode market:enabled=0 (the market
    # leaf gone from the entry tree) -- not merely a genome difference that happens to be inert.
    trial_cfg = runner.calls[0]
    leaves = trial_cfg["entry_rules"][0]["conditions"]["conditions"]
    assert [lf["id"] for lf in leaves] == []  # the only leaf WAS the market leaf


def test_resume_skips_an_already_ablated_row_without_calling_the_runner(db):
    strat, opt, rows = _seed(db, n_top=1)
    source = rows[0]
    # Pre-existing ablation row, as if a previous run already persisted it.
    existing = Backtest(
        name="ABL-MKTOFF-TOP1-sen-S1-atr27-test", expert_name="FMPRating",
        engine_type="daily_expert", optimization_id=opt.id, status="completed",
        start_date=datetime(2024, 2, 1), end_date=datetime(2024, 6, 1), initial_capital=20_000.0,
        ga_fitness=1.7, annualized_return=13.0, max_drawdown=-6.0, total_return=38.0,
        total_trades=19, labels=[AM.ABLATION_LABEL],
    )
    db.add(existing)
    db.commit()

    runner = _stub_runner(_RESULTS)
    result = AM.ablate_optimization(db, opt, runner=runner)
    assert len(result) == 1
    assert runner.calls == []  # never re-run
    assert result[0]["fitness_off"] == existing.ga_fitness


def test_dry_run_persists_nothing_and_never_calls_the_runner(db):
    strat, opt, rows = _seed(db, n_top=2)
    runner = _stub_runner(_RESULTS)
    result = AM.ablate_optimization(db, opt, runner=runner, dry_run=True)
    assert result == []
    assert runner.calls == []
    assert db.query(Backtest).filter(Backtest.name.like("ABL-MKTOFF-%")).count() == 0


def test_find_existing_ablation(db):
    strat, opt, rows = _seed(db, n_top=1)
    assert AM.find_existing_ablation(db, "ABL-MKTOFF-TOP1-sen-S1-atr27-test") is None
    AM.ablate_optimization(db, opt, runner=_stub_runner(_RESULTS))
    found = AM.find_existing_ablation(db, "ABL-MKTOFF-TOP1-sen-S1-atr27-test")
    assert found is not None and found.name == "ABL-MKTOFF-TOP1-sen-S1-atr27-test"
