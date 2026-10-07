"""Review follow-ups of the decision-time gene: stratified mutation, warm-start seeds without the
gene, the persisted counter, robustness variants of a gene row, tools and the name-reuse refusal."""
from __future__ import annotations

import logging
import random
import types
from datetime import datetime

import pytest

from ba2_common.core.schedule_genes import (
    SCHEDULE_TIME_GENE, live_deploy_time_refusal, schedule_weekday_enabled)

from app.services import genetic as G
from app.services.genetic import GeneticOptimizer
from app.services.strategy_param_space import collect_param_space

TIMES = ["09:35", "09:40", "09:45", "10:00", "12:00", "15:30", "15:50"]


def _space(stratify=True):
    space = {"model:x": {"type": "float", "min": 0.0, "max": 1.0, "step": 0.1}}
    spec = {"type": "choice", "choices": list(TIMES), "min": 0, "max": 6, "step": 1}
    if stratify:
        spec["stratify"] = True
    space[SCHEDULE_TIME_GENE] = spec
    return space


def _opt(pop=82, seed=5, stratify=True):
    random.seed(seed)
    return GeneticOptimizer(param_ranges=_space(stratify), population_size=pop, n_generations=3)


def _capture(logger):
    seen = []

    class H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())
    h = H(level=logging.DEBUG)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(h)

    def undo():
        logger.removeHandler(h)
        logger.setLevel(old_level)
    return seen, undo


# ------------------------------------------------------------------ mutation
def test_a_stratified_gene_mutates_by_uniform_redraw_among_the_OTHER_values():
    opt = _opt()
    i = 1
    seen_values = set()
    for start in range(7):
        for _ in range(300):
            ind = G.creator.Individual([0.5, start])
            opt._mutate_individual(ind, indpb=1.0)
            assert ind[i] != start and 0 <= ind[i] <= 6
            seen_values.add((start, ind[i]))
    assert len(seen_values) == 7 * 6        # every other value is reachable from every value


def test_a_plain_choice_gene_keeps_the_gaussian_nudge_bit_for_bit():
    opt = _opt(stratify=False)
    state = opt._rng.getstate()
    ind = G.creator.Individual([0.5, 3])
    opt._mutate_individual(ind, indpb=1.0)
    ref = random.Random()
    ref.setstate(state)
    ref.random()                                    # the gate of gene 0
    sigma0 = (1.0 - 0.0) / 6
    exp_x = min(max(0.5 + ref.gauss(0, sigma0), 0.0), 1.0)
    ref.random()                                    # the gate of gene 1
    exp_c = int(min(max(round(3 + ref.gauss(0, max(1.0, 6 / 6))), 0), 6))
    assert ind[1] == exp_c and ind[0] == pytest.approx(exp_x)


# ------------------------------------------------------------------ seeds
def _seeds(n, with_value=None):
    opt = _opt()
    out = []
    for k in range(n):
        out.append([0.1 * (k % 9), with_value] if with_value is not None else [0.1 * (k % 9), None])
    return opt, out


def test_seeds_without_the_gene_are_spread_not_all_index_zero():
    opt, enc = _seeds(20)
    seen, undo = _capture(G.logger)
    try:
        pop = opt.seeded_population(enc, 82)
    finally:
        undo()
    assert len(pop) == 82 and all(ind[1] is not None for ind in pop)
    counts = [sum(1 for ind in pop if ind[1] == v) for v in range(7)]
    assert min(counts) >= 82 // 7 and sum(counts) == 82
    assert sum(1 for ind in pop[:20] if ind[1] == 0) < 20                 # not silently index 0
    assert any("20 of 20 seed(s) carried no" in m for m in seen)          # ONE line, with the count


def test_seeds_that_carry_the_gene_keep_it_and_the_whole_generation_is_balanced():
    opt, enc = _seeds(30, with_value=6)                                   # all seeded on 15:50
    pop = opt.seeded_population(enc, 82)
    assert [ind[1] for ind in pop[:30]] == [6] * 30                       # seeds untouched
    counts = [sum(1 for ind in pop if ind[1] == v) for v in range(7)]
    assert all(counts[v] >= (82 - 30) // 6 for v in range(6)) and counts[6] == 30   # even shortfall


def test_without_a_stratified_gene_the_seeded_path_is_bit_identical():
    plain = {"model:x": {"type": "float", "min": 0.0, "max": 1.0, "step": 0.1},
             "model:m": {"type": "choice", "choices": ["a", "b", "c"], "min": 0, "max": 2, "step": 1}}
    random.seed(9)
    a = GeneticOptimizer(param_ranges=plain, population_size=12, n_generations=2)
    random.seed(9)
    b = GeneticOptimizer(param_ranges=plain, population_size=12, n_generations=2)
    enc = [[0.3, 1], [0.6, 2]]
    legacy = [G.creator.Individual(list(e)) for e in enc]
    while len(legacy) < 12:
        legacy.append(b.toolbox.individual())
    assert [list(x) for x in a.seeded_population(enc, 12)] == [list(x) for x in legacy]


def test_encode_refuses_a_missing_stratified_gene_unless_asked_to_leave_it_for_completion():
    opt = _opt()
    with pytest.raises(ValueError, match="missing from the params"):
        opt.encode_params({"model:x": 0.5})
    assert opt.encode_params({"model:x": 0.5}, allow_missing_stratified=True)[1] is None


def test_the_warm_start_builder_completes_seeds_of_a_gene_less_source():
    from app.services.strategy_optimization_handler import _build_warm_start_population
    opt = _opt()
    source = types.SimpleNamespace(all_results=[{"params": {"model:x": 0.1 * (k % 9)}} for k in range(10)])
    pop = _build_warm_start_population(source, opt, 82)
    assert len(pop) == 82 and all(ind[1] in range(7) for ind in pop)
    assert min(sum(1 for ind in pop if ind[1] == v) for v in range(7)) >= 11


# ------------------------------------------------------------------ counter persistence
def test_the_results_blob_records_the_chosen_times_and_the_counter():
    from app.services.backtest.daily_backtest_handler import _decision_time_record
    engine = types.SimpleNamespace(sessions_without_decision_bar={"entry": 3, "manage": 3})
    cfg = {"execution_interval": "5min", "run_schedule_override": {"times": ["15:30"]},
           "manage_schedule_override": {"times": ["15:30"]}}
    assert _decision_time_record(engine, cfg) == {
        "entry_times": ["15:30"], "manage_times": ["15:30"],
        "sessions_without_decision_bar": {"entry": 3, "manage": 3}}
    assert _decision_time_record(engine, {**cfg, "execution_interval": "1d"}) is None
    assert _decision_time_record(engine, {"execution_interval": "5min"}) is None


def test_the_master_warns_once_per_job_from_the_trial_counters():
    from app.services import strategy_optimization_handler as H
    results = [{"dt_missing": {"entry": 1, "manage": 1}} for _ in range(500)] + [{"fitness": 1.0}]
    seen, undo = _capture(H.logger)
    try:
        H._warn_decision_time_missing("job", results)
        H._warn_decision_time_missing("job", [{"fitness": 1.0}])
    finally:
        undo()
    assert len(seen) == 1 and "500 of 501 trials" in seen[0]
    assert H._decision_time_missing({"decision_time": {"sessions_without_decision_bar":
                                                       {"entry": 0, "manage": 0}}}) == {}
    assert H._decision_time_missing({"decision_time": {"sessions_without_decision_bar":
                                                       {"entry": 2, "manage": 1}}}) == {
        "dt_missing": {"entry": 2, "manage": 1}}


@pytest.mark.parametrize("ga_trial, level", [(True, logging.DEBUG), (False, logging.WARNING)])
def test_a_ga_trial_logs_the_counter_at_debug_and_a_single_run_at_warning(ga_trial, level):
    import app.services.backtest.daily_engine as DE
    eng = DE.DailyBacktestEngine.__new__(DE.DailyBacktestEngine)
    sched = {"days": {d: True for d in DE._WEEKDAYS}, "times": ["15:30"]}
    eng.config = {"_ga_trial": True} if ga_trial else {}
    eng.price = types.SimpleNamespace(is_intraday=True)
    eng.experts = [(object(), 1, {}, 1)]
    eng._entry_schedule = lambda e: sched
    eng._manage_schedule = lambda e: sched
    full = [datetime(2025, 12, 3, 9, 30), datetime(2025, 12, 3, 15, 30)]
    half = [datetime(2025, 11, 28, 9, 30), datetime(2025, 11, 28, 12, 55)]
    levels = []

    class H(logging.Handler):
        def emit(self, record):
            if "SESSIONS WITHOUT" in record.getMessage():
                levels.append(record.levelno)
    h = H(level=logging.DEBUG)
    DE.logger.addHandler(h)
    try:
        eng._count_sessions_without_decision_bar(full + half)
    finally:
        DE.logger.removeHandler(h)
    assert eng.sessions_without_decision_bar == {"entry": 1, "manage": 1}
    assert levels == [level]


def test_weekday_enabled_is_the_one_reading_absent_means_enabled():
    assert schedule_weekday_enabled({}, "monday") and schedule_weekday_enabled(None, "friday")
    assert not schedule_weekday_enabled({"monday": False}, "monday")
    assert schedule_weekday_enabled({"monday": False}, "tuesday")


# ------------------------------------------------------------------ robustness
def test_time_variants_are_skipped_for_a_gene_row_and_day_variants_keep_the_time():
    from app.services.robustness_handler import _schedule_variants
    params = {"day_variants": True, "time_variants": ["10:30", "12:30", "15:00"]}
    out = _schedule_variants(params, ["15:30"], time_is_gene=True)
    assert [v["variant"] for v in out] == ["day-monday", "day-tuesday", "day-wednesday",
                                           "day-thursday", "day-friday"]
    assert all(v["override"]["times"] == ["15:30"] for v in out)
    legacy = _schedule_variants(params, ["09:30"])
    assert [v["variant"] for v in legacy][-3:] == ["time-10:30", "time-12:30", "time-15:00"]


def test_a_standalone_rerun_carries_the_stored_manage_schedule():
    import app.services.backtest.rerun_handler as RH
    captured = {}
    orig = RH._build_config
    RH._build_config = lambda payload: captured.setdefault("p", payload) or payload
    try:
        bt = types.SimpleNamespace(
            id=1, name="v", expert_name="FMPRating", start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 2, 1), initial_capital=1000.0, commission=0.0, slippage=0.0,
            strategy_params={"universe": {"symbols": ["AAPL"]}, "executionInterval": "5min",
                             "runScheduleOverride": {"days": {"monday": True}, "times": ["15:30"]},
                             "manageScheduleOverride": {"days": {"monday": True}, "times": ["15:30"]}})
        RH._build_standalone_rerun_config(bt)
    finally:
        RH._build_config = orig
    assert captured["p"]["manage_schedule_override"]["times"] == ["15:30"]
    assert captured["p"]["run_schedule_override"]["times"] == ["15:30"]


# ------------------------------------------------------------------ tools / launcher
def test_fix_live_schedules_does_not_read_the_time_gene_as_a_weekday():
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))), "tools", "fix_live_schedules.py")
    spec = importlib.util.spec_from_file_location("fix_live_schedules_t", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    days = m._genome_days({"schedule:monday": 1, "schedule:thursday": 0, SCHEDULE_TIME_GENE: "15:30"})
    assert set(days) == set(m.DAYS) and days["monday"] is True and "time" not in days


def test_live_deploy_bound_is_one_constant_with_its_measurement():
    assert live_deploy_time_refusal("10:00") is None
    assert live_deploy_time_refusal("15:30") is None
    assert "after the 16:00 close" in live_deploy_time_refusal("15:50")
    assert live_deploy_time_refusal("15:40") is None          # 15:40 + 8.5 min + 10 min = 15:58:30
    assert live_deploy_time_refusal("15:45") is not None


def test_a_reused_timegene_name_with_a_different_list_is_refused(monkeypatch):
    import app.models  # noqa: F401
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.models import database as D
    from app.models.database import Base
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    import importlib.util
    import os
    import sys
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(D, "SessionLocal", Session)
    s = Session()
    strat = Strategy(name="s")
    s.add(strat)
    s.commit()
    s.add(StrategyOptimization(
        strategy_id=strat.id, name="job-timegene", status="completed", fitness_metric="x",
        optimization_type="genetic",
        optimization_config={"expert_params": {"schedule:time": {"optimize": True, "choices": TIMES}}}))
    s.commit()
    s.close()
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "ba2test_launcher.py")
    spec = importlib.util.spec_from_file_location("lch_review", path)
    L = importlib.util.module_from_spec(spec)
    sys.modules["lch_review"] = L
    try:
        spec.loader.exec_module(L)
    except SystemExit:
        pass
    L._refuse_reused_timegene_name("job-timegene", TIMES, "optimize")            # same list: fine
    L._refuse_reused_timegene_name("another-timegene", TIMES[:-1], "optimize")   # unused name: fine
    with pytest.raises(SystemExit, match="different --name"):
        L._refuse_reused_timegene_name("job-timegene", TIMES[:-1], "optimize")
