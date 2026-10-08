"""The decision-time gene (``schedule:time``): a categorical choice among the job's explicit times.

Pinned here, end to end: gene space shape + validation, GA operators (generation 0 stratified,
mutation / crossover can never leave the declared set, decode refuses a foreign index), decode ->
BOTH run schedules, the ``_build_daily_trial_config`` whitelist (a declared gene must reach the
trial), the engine really deciding at the chosen bar, the half-day / last-bar behaviour, the JSON
round trip a remote worker needs, and the launcher / driver plumbing.
"""
from __future__ import annotations

import json
import random
import types
from datetime import date, datetime

import pytest

from ba2_common.core.knowability import DEFAULT_DECISION_TIME, DEFAULT_DECISION_TIME_CHOICES
from ba2_common.core.schedule_genes import SCHEDULE_TIME_GENE, validate_decision_times

from app.services.genetic import GeneticOptimizer
from app.services.strategy_optimization_handler import (
    _build_daily_trial_config, checkpoint_fingerprint)
from app.services.strategy_param_space import collect_param_space, decode_params

TIMES = list(DEFAULT_DECISION_TIME_CHOICES)
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAYS_ON = {d: d in DAYS[:5] for d in DAYS}


def _strategy():
    return types.SimpleNamespace(entry_rules=None, exit_rules=None)


def _schedule_cfg(times=TIMES, day_genes=True):
    cfg = {d: {"optimize": True} for d in DAYS} if day_genes else {}
    cfg["time"] = {"optimize": True, "choices": list(times)}
    return cfg


def _space(times=TIMES):
    return collect_param_space(
        _strategy(), expert_cfg={"x": {"optimize": True, "min": 1.0, "max": 3.0, "step": 1.0,
                                       "type": "float"}},
        schedule_cfg=_schedule_cfg(times))


def test_the_owners_seven_values_are_the_shared_default_and_valid():
    assert TIMES == ["09:35", "09:40", "09:45", "10:00", "12:00", "15:30", "15:45"]
    assert validate_decision_times(TIMES, "5min") == TIMES       # 15:45 leaves two bars after it to fill on


# ---------------------------------------------------------------------------------------------
# gene space
# ---------------------------------------------------------------------------------------------
def test_the_gene_is_a_choice_after_the_day_genes():
    space = _space()
    keys = list(space)
    assert keys[-1] == SCHEDULE_TIME_GENE and keys.index("schedule:sunday") < keys.index(keys[-1])
    spec = space[SCHEDULE_TIME_GENE]
    assert spec["type"] == "choice" and spec["choices"] == TIMES
    assert (spec["min"], spec["max"]) == (0, len(TIMES) - 1) and spec["stratify"] is True


def test_adding_the_gene_does_not_move_any_other_gene():
    without = collect_param_space(
        _strategy(), expert_cfg={"x": {"optimize": True, "min": 1.0, "max": 3.0, "step": 1.0,
                                       "type": "float"}},
        schedule_cfg=_schedule_cfg(day_genes=True) | {"time": {"optimize": False}})
    assert list(_space())[:-1] == list(without)


@pytest.mark.parametrize("bad", [["10:00", "09:40"], ["09:40", "09:40"], ["09:40"], ["9:40", "10:00"]])
def test_a_malformed_list_is_refused_by_the_collector(bad):
    with pytest.raises(ValueError):
        collect_param_space(_strategy(), schedule_cfg=_schedule_cfg(bad))


def test_a_time_gene_on_a_bypass_expert_is_refused_not_dropped():
    with pytest.raises(ValueError, match="bypass"):
        collect_param_space(_strategy(), expert_cfg={"x": {"optimize": True, "min": 1.0, "max": 2.0,
                                                           "step": 1.0, "type": "float"}},
                            bypass=True, schedule_cfg=_schedule_cfg())


def test_the_list_is_part_of_the_checkpoint_identity():
    ga = {"populationSize": 82, "generations": 30}
    a = checkpoint_fingerprint(_space(TIMES), ga)
    b = checkpoint_fingerprint(_space(TIMES[:-1]), ga)
    c = checkpoint_fingerprint(collect_param_space(
        _strategy(), expert_cfg={"x": {"optimize": True, "min": 1.0, "max": 3.0, "step": 1.0,
                                       "type": "float"}},
        schedule_cfg=_schedule_cfg(day_genes=True) | {"time": {"optimize": False}}), ga)
    assert len({a, b, c}) == 3


# ---------------------------------------------------------------------------------------------
# GA operators
# ---------------------------------------------------------------------------------------------
def _optimizer(pop=82, seed=7):
    random.seed(seed)
    return GeneticOptimizer(param_ranges=_space(), population_size=pop, n_generations=3)


def test_generation_zero_is_stratified_over_the_values():
    opt = _optimizer(82)
    pop = opt._initial_population(82)
    i = list(opt.param_ranges).index(SCHEDULE_TIME_GENE)
    counts = [sum(1 for ind in pop if ind[i] == v) for v in range(len(TIMES))]
    assert len(pop) == 82 and min(counts) >= 82 // 7 == 11 and sum(counts) == 82


def test_stratification_at_pop_200_gives_28_per_value():
    opt = _optimizer(200)
    pop = opt._initial_population(200)
    i = list(opt.param_ranges).index(SCHEDULE_TIME_GENE)
    assert min(sum(1 for ind in pop if ind[i] == v) for v in range(7)) >= 28


def test_stratification_never_exceeds_the_population():
    opt = _optimizer(5)       # fewer individuals than values: left to the free draw, no crash
    assert len(opt._initial_population(5)) == 5


def test_other_genes_stay_free_and_a_job_without_a_stratified_gene_is_bit_identical():
    space = {"model:a": {"type": "float", "min": 0.0, "max": 1.0, "step": 0.1},
             "model:m": {"type": "choice", "choices": ["p", "q", "r"], "min": 0, "max": 2, "step": 1}}
    random.seed(11)
    a = GeneticOptimizer(param_ranges=space, population_size=20, n_generations=2)
    random.seed(11)
    b = GeneticOptimizer(param_ranges=space, population_size=20, n_generations=2)
    legacy = [list(x) for x in b.toolbox.population(n=20)]       # the pre-change code path
    assert [list(x) for x in a._initial_population(20)] == legacy


def test_mutation_and_crossover_never_leave_the_declared_set():
    opt = _optimizer(40, seed=3)
    i = list(opt.param_ranges).index(SCHEDULE_TIME_GENE)
    pop = opt._initial_population(40)
    rng = random.Random(5)
    for _ in range(3000):
        a, b = rng.sample(pop, 2)
        a, b = type(a)(list(a)), type(b)(list(b))
        if rng.random() < 0.7:
            opt.toolbox.mate(a, b)
        opt.toolbox.mutate(a, indpb=0.9)
        opt.toolbox.mutate(b, indpb=0.9)
        for ind in (a, b):
            assert ind[i] in range(len(TIMES)) and float(ind[i]).is_integer()
            assert opt.decode_individual(ind)[SCHEDULE_TIME_GENE] in TIMES
        pop[rng.randrange(len(pop))] = a


def test_decode_refuses_an_index_outside_the_declared_list():
    opt = _optimizer(4)
    ind = opt._initial_population(4)[0]
    i = list(opt.param_ranges).index(SCHEDULE_TIME_GENE)
    for bad in (len(TIMES), -1, 99):
        ind[i] = bad
        with pytest.raises(ValueError, match="outside its 7 declared choices"):
            opt.decode_individual(ind)


def test_encode_refuses_a_time_from_another_list_instead_of_picking_the_first():
    opt = _optimizer(4)
    flat = opt.decode_individual(opt._initial_population(4)[0])
    flat[SCHEDULE_TIME_GENE] = "11:00"
    with pytest.raises(ValueError, match="not in this job's declared choices"):
        opt.encode_params(flat)
    flat[SCHEDULE_TIME_GENE] = "15:30"
    assert opt.decode_individual(opt.encode_params(flat))[SCHEDULE_TIME_GENE] == "15:30"


# ---------------------------------------------------------------------------------------------
# decode -> both schedules; the whitelist
# ---------------------------------------------------------------------------------------------
def _bt_cfg(**overrides):
    cfg = {
        "backtest_id": 1, "name": "t", "start_date": "2024-01-01", "end_date": "2024-02-01",
        "enabled_instruments": ["AAPL"], "initial_capital": 10000.0, "warmup_days": 0,
        "seed": 42, "account_settings": {}, "execution_interval": "5min",
        "experts": [{"class": "FMPRating", "settings": {"sizing_mode": "risk_atr"}}],
        "run_schedule_override": {"days": dict(WEEKDAYS_ON), "times": [DEFAULT_DECISION_TIME]},
        "manage_schedule_override": {"days": dict(WEEKDAYS_ON), "times": [DEFAULT_DECISION_TIME]},
    }
    cfg.update(overrides)
    return cfg


def _flat(time=None, **extra):
    flat = {f"schedule:{d}": int(d in ("tuesday", "thursday")) for d in DAYS}
    if time is not None:
        flat[SCHEDULE_TIME_GENE] = time
    flat.update(extra)
    return flat


@pytest.mark.parametrize("time", TIMES)
def test_a_decoded_time_drives_BOTH_schedules(time):
    decoded = decode_params(_strategy(), _flat(time))
    assert decoded["schedule_time"] == time
    trial = _build_daily_trial_config(_bt_cfg(), decoded, option_trade_records=False)
    assert trial["run_schedule_override"]["times"] == [time]
    assert trial["manage_schedule_override"]["times"] == [time]
    assert [d for d, v in trial["run_schedule_override"]["days"].items() if v] == ["tuesday", "thursday"]
    assert trial["manage_schedule_override"]["days"] == WEEKDAYS_ON        # manage days untouched


def test_without_the_gene_the_trial_config_is_exactly_what_it_was():
    base = _bt_cfg()
    trial = _build_daily_trial_config(base, decode_params(_strategy(), _flat()),
                                      option_trade_records=False)
    assert trial["run_schedule_override"]["times"] == [DEFAULT_DECISION_TIME]
    assert trial["manage_schedule_override"] == base["manage_schedule_override"]
    assert decode_params(_strategy(), _flat())["schedule_time"] is None


def test_a_time_gene_without_manage_days_is_refused_not_left_on_another_time():
    with pytest.raises(ValueError, match="manage_schedule_override"):
        _build_daily_trial_config(_bt_cfg(manage_schedule_override=None),
                                  decode_params(_strategy(), _flat("10:00")),
                                  option_trade_records=False)


def test_a_time_gene_alone_takes_the_run_days():
    decoded = decode_params(_strategy(), {SCHEDULE_TIME_GENE: "12:00"})
    trial = _build_daily_trial_config(_bt_cfg(), decoded, option_trade_records=False)
    assert trial["run_schedule_override"] == {"days": WEEKDAYS_ON, "times": ["12:00"]}


def test_a_non_time_value_is_refused_by_decode():
    with pytest.raises(ValueError):
        decode_params(_strategy(), _flat("noon"))


def test_no_decoded_key_is_ever_silently_dropped_by_the_trial_config():
    """The whitelist guard: ``_build_daily_trial_config`` rebuilds the config key by key, so a
    decoded product it does not read is dead however well it was parsed. Every key of
    ``decode_params``' result must either change the trial config or be named here as read
    through a different seam (screener_overrides: needs a hoisted metric store, pinned by
    test_screener_genes)."""
    decoded = decode_params(_strategy(), _flat("15:30", **{"model:x": 2.0}))
    assert set(decoded) == {"expert_overrides", "screener_overrides", "schedule_days",
                            "schedule_time", "entry_rules", "exit_rules"}, \
        "decode_params grew a key: add its consumer check below"
    base = _build_daily_trial_config(_bt_cfg(), {**decoded, "schedule_days": None,
                                                 "schedule_time": None, "expert_overrides": {}},
                                     option_trade_records=False)
    full = _build_daily_trial_config(_bt_cfg(), decoded, option_trade_records=False)
    assert full["experts"][0]["settings"]["x"] == 2.0 and "x" not in base["experts"][0]["settings"]
    assert full["run_schedule_override"] != base["run_schedule_override"]       # schedule_days
    assert full["manage_schedule_override"] != base["manage_schedule_override"]  # schedule_time
    sentinel = [{"id": "sentinel-rule"}]
    out = _build_daily_trial_config(_bt_cfg(), {**decoded, "entry_rules": sentinel,
                                                "exit_rules": sentinel}, option_trade_records=False)
    assert out["entry_rules"] is sentinel and out["exit_rules"] is sentinel


def test_the_trial_config_and_genome_survive_the_trip_to_a_remote_worker():
    decoded = decode_params(_strategy(), _flat("15:45"))
    trial = _build_daily_trial_config(_bt_cfg(), decoded, option_trade_records=False)
    wire = json.loads(json.dumps(trial))
    assert wire["run_schedule_override"] == trial["run_schedule_override"]
    assert wire["manage_schedule_override"] == trial["manage_schedule_override"]
    assert wire["run_schedule_override"]["times"] == ["15:45"]
    genome = json.loads(json.dumps(_flat("15:45")))
    assert decode_params(_strategy(), genome)["schedule_time"] == "15:45"


# ---------------------------------------------------------------------------------------------
# the engine: the decision bar, half days, the last bars
# ---------------------------------------------------------------------------------------------
def _session(d, last_hhmm):
    """5-minute bars for session ``d`` from 09:30 through ``last_hhmm`` (the last bar's START)."""
    rows, px = [], 100.0 + d.day
    h, m = 9, 30
    while (h, m) <= (int(last_hhmm[:2]), int(last_hhmm[3:])):
        rows.append({"Date": datetime(d.year, d.month, d.day, h, m), "Open": px, "High": px + 0.5,
                     "Low": px - 0.5, "Close": px + 0.3, "Volume": 1000})
        px += 0.1
        m += 5
        if m == 60:
            h, m = h + 1, 0
    return rows


FULL, HALF = "15:55", "12:55"          # last bar start of a full day / of a 13:00 half day
SESSIONS = [(date(2023, 12, 29), FULL), (date(2024, 1, 2), FULL), (date(2024, 1, 3), FULL),
            (date(2024, 1, 4), HALF)]


def _rows():
    return [r for d, last in SESSIONS for r in _session(d, last)]


def _engine_run(run_times, manage_times, run_id):
    from app.services.backtest import price_source as ps_mod
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.price_source import AsOfPriceSource, MemoizedOHLCVProvider
    from app.services.backtest.seam_wiring import set_backtest_ohlcv_override, wire_backtest_seams
    from tests.backtest.test_intraday_daily_knowability import _FakeDaily, _ProbeExpert
    from tests.backtest.test_max_loss_stop_engine import CFG

    ps_mod.clear_ohlcv_memo()
    from ba2_experts.DeterministicScorer import data as ds_data
    ds_data._OHLCV_COVERAGE.clear()
    ds_data._OHLCV_VIEWS.clear()
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"decision-time-{run_id}")
    ctx.__enter__()
    try:
        seed_account_definition(run_id, CFG)
        ruleset_id = seed_enter_long_ruleset()
        seed_expert_instance(account_id=run_id, expert_class_name="_ProbeExpert",
                             enter_market_ruleset_id=ruleset_id, instance_id=run_id)
        memo = MemoizedOHLCVProvider(_FakeDaily(), datetime(2023, 12, 1), datetime(2024, 1, 31),
                                     interval="5min")
        ps = AsOfPriceSource(ohlcv_provider=None, interval="5min")
        memo.bind_price_source(ps)
        ps.load_bars("AAPL", _rows())
        account = BacktestAccount(run_id, ps, CFG)
        resolver.register_account(run_id, account)
        expert = _ProbeExpert(run_id, ps)
        expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                              "enable_buy": (True, "bool")})
        resolver.register_expert(run_id, expert)
        set_backtest_ohlcv_override(memo)
        manage_calls = []
        try:
            engine = DailyBacktestEngine(
                account=account, experts=[(expert, run_id, {}, ruleset_id)], price_source=ps,
                config={"start_date": datetime(2024, 1, 2), "end_date": datetime(2024, 1, 4, 23, 59),
                        "enabled_instruments": ["AAPL"], "seed": 42,
                        "run_schedule_override": {"days": dict(WEEKDAYS_ON), "times": list(run_times)},
                        "manage_schedule_override": {"days": dict(WEEKDAYS_ON),
                                                     "times": list(manage_times)}},
                indicator_provider=None)
            engine._indicator_provider = None
            orig = engine._manage_open_positions
            engine._manage_open_positions = lambda e, i, s, as_of: (manage_calls.append(as_of),
                                                                    orig(e, i, s, as_of))[1]
            engine.run()
        finally:
            set_backtest_ohlcv_override(None)
        return engine, expert.records, manage_calls, ps, account
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize("time", ["09:35", "10:00", "12:00", "15:30", "15:45"])
def test_the_engine_decides_and_manages_at_the_chosen_bar(time):
    """Decoded gene -> trial config -> engine: the entry decision and the manage pass both run at
    exactly that bar on every session that HAS the bar. (2024-01-04 is a 13:00 half day: only
    09:35 / 10:00 / 12:00 exist on it.)"""
    trial = _build_daily_trial_config(
        _bt_cfg(), decode_params(_strategy(), _flat(time)), option_trade_records=False)
    engine, records, manage, _ps, _acct = _engine_run(
        trial["run_schedule_override"]["times"], trial["manage_schedule_override"]["times"],
        run_id=810 + TIMES.index(time))
    on_half_day = time in ("09:35", "10:00", "12:00")
    expected_days = [date(2024, 1, 2), date(2024, 1, 3)] + ([date(2024, 1, 4)] if on_half_day else [])
    assert [r["as_of"].date() for r in records] == expected_days
    assert all(r["as_of"].strftime("%H:%M") == time for r in records)
    assert [m.date() for m in manage] == expected_days
    assert all(m.strftime("%H:%M") == time for m in manage)


def test_a_session_without_the_scheduled_time_is_skipped_counted_and_loud():
    """15:30 does not exist on a 13:00 half day: no entry that day (conservative), counted."""
    import logging
    from app.services.backtest import daily_engine

    seen = []

    class _H(logging.Handler):          # the project logger does not propagate to caplog
        def emit(self, record):
            seen.append(record.getMessage())

    handler = _H(level=logging.WARNING)
    daily_engine.logger.addHandler(handler)
    try:
        engine, records, manage, _ps, _a = _engine_run(["15:30"], ["15:30"], run_id=830)
    finally:
        daily_engine.logger.removeHandler(handler)
    assert [r["as_of"].date() for r in records] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert engine.sessions_without_decision_bar == {"entry": 1, "manage": 1}
    assert any("SESSIONS WITHOUT A DECISION BAR" in m for m in seen)


def test_a_mixed_list_is_judged_per_chosen_value_on_a_half_day():
    """12:00 exists on the half day, 15:30 and 15:45 do not: the counter is per trial."""
    e1, r1, *_ = _engine_run(["12:00"], ["12:00"], run_id=831)
    assert e1.sessions_without_decision_bar == {"entry": 0, "manage": 0} and len(r1) == 3
    e2, r2, *_ = _engine_run(["15:50"], ["15:50"], run_id=832)
    assert e2.sessions_without_decision_bar == {"entry": 1, "manage": 1} and len(r2) == 2


def test_no_counter_noise_for_an_ordinary_full_session_time():
    engine, *_ = _engine_run(["10:00"], ["10:00"], run_id=833)
    assert engine.sessions_without_decision_bar == {"entry": 0, "manage": 0}


def test_an_order_decided_at_15_50_fills_on_the_15_55_bar_and_one_at_15_55_overnight():
    """The fill is the first bar STRICTLY AFTER the decision bar: 15:50 -> the 15:55 open, the same
    session; 15:55 (the last bar, refused by validation) would be the NEXT session's 09:30 open."""
    from app.services.backtest.backtest_account import BacktestAccount
    from tests.backtest.test_max_loss_stop_engine import CFG

    _e, _r, _m, ps, _acct = _engine_run(["15:50"], ["15:50"], run_id=834)
    acct = BacktestAccount(835, ps, CFG)

    class _O:
        symbol = "AAPL"

    t_1550 = datetime(2024, 1, 2, 15, 50)
    t_1555 = datetime(2024, 1, 2, 15, 55)
    rows = {r["Date"]: r for r in _rows()}
    assert acct._bar_for_fill(_O(), t_1550)["open"] == rows[t_1555]["Open"]
    assert acct._bar_for_fill(_O(), t_1555)["open"] == rows[datetime(2024, 1, 3, 9, 30)]["Open"]
    # and the decision price at 15:50 is the close of the bar that ENDED at 15:50 (the 15:45 bar)
    ps.set_clock(t_1550)
    assert ps.decision_price("AAPL", t_1550) == rows[datetime(2024, 1, 2, 15, 45)]["Close"]


# ---------------------------------------------------------------------------------------------
# launcher + drivers
# ---------------------------------------------------------------------------------------------
def _launcher():
    import importlib.util
    import os
    import sys
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "ba2test_launcher.py")
    spec = importlib.util.spec_from_file_location("lch_decision_time", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_decision_time"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def test_launcher_resolution_and_refusals():
    L = _launcher()
    res = L._resolve_decision_times
    kw = dict(bypass_jobs=[], option_jobs=[])
    assert res(None, "optimize", interval="5min", name="x", **kw) is None        # plain optimize: fixed
    assert res("fixed", "optimize", interval="5min", name="x", **kw) is None
    assert res("default", "optimize", interval="5min", name="a-timegene", **kw) == TIMES
    assert res("10:00,09:35", "optimize", interval="5min", name="a-timegene", **kw) == ["09:35", "10:00"]
    for bad in (dict(raw="default", interval="1d", name="a-timegene"),            # daily clock
                dict(raw="default", interval="5min", name="a-plain"),             # no name token
                dict(raw="09:30,10:00", interval="5min", name="a-timegene"),      # first bar
                dict(raw="10:00,15:55", interval="5min", name="a-timegene")):     # last bar
        with pytest.raises(SystemExit):
            res(bad.pop("raw"), "optimize", **bad, **kw)
    with pytest.raises(SystemExit):
        res("default", "optimize", interval="5min", name="a-timegene",
            bypass_jobs=["FactorRanker"], option_jobs=[])
    with pytest.raises(SystemExit):
        res("default", "optimize", interval="5min", name="a-timegene",
            bypass_jobs=[], option_jobs=["FMPRating/O_LC"])
    assert L._decision_time_gene(None) == {}
    assert L._decision_time_gene(TIMES) == {"schedule:time": {"optimize": True, "choices": TIMES}}


def test_driver_helpers_default_on_fixed_and_daily():
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))), "tools"))
    import matrix_flags as mf

    times, header = mf.decision_times_plan(None, "5min")
    assert times == TIMES and "09:35" in header and "15:45" in header
    assert mf.decision_times_plan("fixed", "5min")[0] is None
    assert "fixed 10:00" in mf.decision_times_plan("fixed", "5min")[1]
    assert mf.decision_times_plan(None, "1d") == (None, "decision time: daily clock (not optimizable)")
    with pytest.raises(ValueError):
        mf.decision_times_plan("default", "1d")                    # explicit on a daily clock: refused
    assert mf.decision_times_tokens(TIMES) == ["--decision-times", ",".join(TIMES)]
    assert mf.decision_times_tokens(None) == []
    assert mf.with_decision_times_name("scr-mid-X-S1-goal2020", TIMES).endswith("-timegene")
    assert mf.with_decision_times_name("n", None) == "n"
    a = mf.job_name_with_digest("n", ["optimize", "--decision-times", ",".join(TIMES)])
    b = mf.job_name_with_digest("n", ["optimize", "--decision-times", ",".join(TIMES[:-1])])
    assert a != b          # two jobs differing only by the list never share a name
