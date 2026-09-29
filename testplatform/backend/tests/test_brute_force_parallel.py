"""Brute-force grids evaluated in parallel / on remote workers (exploration phase 1).

``optimization_type == "brute_force"`` used to be a serial in-process loop, whatever the job's
``parallelIndividuals`` / ``worker_ids`` said. Pinned here:

* parallelIndividuals <= 1 and no worker: the ORIGINAL serial loop, byte-identical (best_params,
  best_fitness, all_results in product order), and no pool is ever built;
* parallelIndividuals = 4 through the local slot pools: the SAME result as serial, although the
  trials complete out of order (the tie at the optimum is settled by product order, as serially);
* remote-only and local+remote through DistributedEvaluator (the remote worker faked the way
  test_strategy_optimization_handler's remote-only test fakes it): the same result again;
* a paused/cancelled task stops dispatching;
* ``_evaluate_brute_force_batch`` dedupes combos that share a trial key (as the serial memo does)
  and re-sorts completion-order rows into product order.

The trial itself is the REAL ``_trial_worker`` / ``_run_trial_backtest`` over a deterministic
``run_daily_backtest`` stub, so the serial and the batched path meet the same seam.
"""
from __future__ import annotations

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.services.distributed_eval as de
from app.models import Worker
from app.models.database import Base, SessionLocal, engine
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization
from app.services import strategy_optimization_handler as H

TP = "entry:bracket:a1:action_value"
SL = "entry:bracket:a2:action_value"
TP_VALUES = [float(v) for v in range(2, 13)]        # 2..12
SL_VALUES = [float(v) for v in range(-6, 0)]        # -6..-1
PRODUCT = [{TP: tp, SL: sl} for tp, sl in itertools.product(TP_VALUES, SL_VALUES)]


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    Base.metadata.create_all(bind=engine)
    yield


# --------------------------------------------------------------------------- the trial seam
class _Seam:
    """``run_daily_backtest`` stand-in. The score depends on TP only, so the six combos at TP 8
    TIE at the optimum; the first of them in product order (SL -6) is the serial winner. That
    combo is also made the SLOWEST trial, so a batch sees it land after its tied neighbours."""

    def __init__(self, delay=True):
        self.delay = delay
        # When set, the winner (8, -6) blocks until its tied neighbour (8, -5) has FINISHED, so a
        # batch deterministically sees the winner land after it (a sleep alone raced).
        self.hold_winner = False
        self.neighbour_done = threading.Event()
        self.lock = threading.Lock()
        self.started = []
        self.finished = []

    def __call__(self, config, progress_cb=None, **_kw):
        tp, sl = _tp_sl(config["entry_rules"])
        with self.lock:
            self.started.append((tp, sl))
        if self.hold_winner and (tp, sl) == (8.0, -6.0):
            assert self.neighbour_done.wait(10), "tied neighbour (8, -5) never finished"
        elif self.delay:
            time.sleep(0.03 if sl == -6.0 else 0.001)
        score = 10.0 - abs(tp - 8.0)
        with self.lock:
            self.finished.append((tp, sl))
        if (tp, sl) == (8.0, -5.0):
            self.neighbour_done.set()
        return {"total_trades": 5, "sharpe_ratio": score, "max_drawdown": 5.0,
                "total_return": score, "profit_factor": 1.5, "win_rate": 55.0}


def _tp_sl(entry_rules):
    actions = {a["action_type"]: a for rule in entry_rules for a in rule["actions"]}
    return (actions["adjust_take_profit"]["action_value"],
            actions["adjust_stop_loss"]["action_value"])


class _ThreadPoolAsProcessPool(ThreadPoolExecutor):
    """The handler's ProcessPoolExecutor, in threads: same submit/shutdown contract, no spawn, no
    worker initializer, so the monkeypatched seam is visible to every 'worker'."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        super().__init__(max_workers=max_workers)


@pytest.fixture
def seam(monkeypatch):
    import concurrent.futures
    from app.services.backtest import daily_backtest_handler

    s = _Seam()
    monkeypatch.setattr(daily_backtest_handler, "run_daily_backtest", s)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    # A real psutil snapshot could trip the memory governor on a busy box; none is needed here.
    monkeypatch.setattr(H, "_trial_memory_snapshot", lambda: {})
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _ThreadPoolAsProcessPool)
    return s


@pytest.fixture
def fake_remote(monkeypatch):
    """The remote worker: the same fakes as the handler's remote-only test, with run_trial doing
    what the worker server does -- the real _trial_worker on the trial config."""
    seen = []

    def fake_run_trial(worker, config, metric, **kw):
        seen.append(worker["name"])
        return H._trial_worker(config, metric)

    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "push_secrets", lambda w, s, **k: {"set": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 3})
    monkeypatch.setattr(de.worker_client, "run_trial", fake_run_trial)
    return seen


# --------------------------------------------------------------------------- DB helpers
def _seed_strategy() -> int:
    db = SessionLocal()
    try:
        s = Strategy(
            name="bf-parallel",
            entry_rules=[{
                "id": "bracket", "conditions": None, "continue_processing": False,
                "actions": [
                    {"action_type": "buy"},
                    {"id": "e_tp", "action_type": "adjust_take_profit",
                     "reference_value": "order_open_price", "action_value": 5.0,
                     "action_value_optimize": True, "action_value_min": 2.0,
                     "action_value_max": 12.0, "action_value_step": 1.0},
                    {"id": "e_sl", "action_type": "adjust_stop_loss",
                     "reference_value": "order_open_price", "action_value": -2.0,
                     "action_value_optimize": True, "action_value_min": -6.0,
                     "action_value_max": -1.0, "action_value_step": 1.0},
                ],
            }],
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id
    finally:
        db.close()


def _worker(name: str, enabled: bool = True) -> int:
    db = SessionLocal()
    try:
        w = Worker(name=name, url="http://x:8100", password="p", worker_type="remote",
                   is_local=False, is_enabled=enabled, status="offline")
        db.add(w)
        db.commit()
        db.refresh(w)
        return w.id
    finally:
        db.close()


def _run(parallel=None, worker_ids=None, task_id="bf"):
    cfg = {"populationSize": 8, "generations": 4, "crossoverProb": 0.7, "mutationProb": 0.2,
           "earlyStoppingGenerations": 4, "elitismPercent": 10.0, "seed": 42,
           "backtest": {"engine": "daily", "backtest_id": 7, "start_date": "2024-01-02",
                        "end_date": "2024-01-08", "seed": 42, "experts": [],
                        "enabled_instruments": ["AAPL"], "warmup_days": 30,
                        "initial_capital": 100000.0,
                        "account_settings": {"starting_cash": 100000.0}}}
    if parallel is not None:
        cfg["parallelIndividuals"] = parallel
    db = SessionLocal()
    try:
        row = StrategyOptimization(strategy_id=_seed_strategy(), name=f"bf-{task_id}",
                                   fitness_metric="sharpe", optimization_type="brute_force",
                                   optimization_config=cfg, worker_ids=worker_ids,
                                   status="pending")
        db.add(row)
        db.commit()
        opt_id = row.id
    finally:
        db.close()
    out = H.handle_strategy_optimization(task_id, {"optimization_id": opt_id})
    db = SessionLocal()
    try:
        row = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
        return out, {"status": row.status, "best_params": row.best_params,
                     "best_fitness": row.best_fitness, "all_results": row.all_results}
    finally:
        db.close()


def _signature(row):
    return (row["best_params"], row["best_fitness"], row["all_results"])


@pytest.fixture
def serial_reference(seam):
    """The serial loop's result, computed once per test on the same seam."""
    out, row = _run(parallel=1, task_id="bf-serial-ref")
    assert out["status"] == "completed", out
    seam.started.clear()
    seam.finished.clear()
    return row


# --------------------------------------------------------------------------- serial pin
def test_serial_brute_force_is_the_original_loop(seam, monkeypatch):
    """parallelIndividuals=1, no worker: product order, first-of-ties winner, no pool at all."""
    import concurrent.futures

    def no_pool(*a, **k):
        raise AssertionError("the serial brute force built a process pool")

    def no_batch(*a, **k):
        raise AssertionError("the serial brute force took the batched path")

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", no_pool)
    monkeypatch.setattr(H, "_evaluate_brute_force_batch", no_batch)
    seam.delay = False
    for parallel in (None, 1):    # the key absent, and explicitly 1
        seam.started.clear()
        out, row = _run(parallel=parallel, task_id=f"bf-serial-{parallel}")
        assert out["status"] == "completed", out
        assert row["best_params"] == {TP: 8.0, SL: -6.0}
        assert row["best_fitness"] == pytest.approx(10.0)
        assert [r["params"] for r in row["all_results"]] == PRODUCT
        assert [r["fitness"] for r in row["all_results"]] == [
            pytest.approx(10.0 - abs(p[TP] - 8.0)) for p in PRODUCT]
        assert seam.started == [(p[TP], p[SL]) for p in PRODUCT]   # one trial per combo, in order


def test_combos_are_itertools_product_order():
    space = {"a": {"type": "int", "min": 1, "max": 3, "step": 1},
             "b": {"type": "float", "min": 0.1, "max": 0.3, "step": 0.1}}
    assert H._brute_force_combos(space) == [
        {"a": a, "b": b} for a, b in itertools.product([1, 2, 3], [0.1, 0.2, 0.3])]


# --------------------------------------------------------------------------- local pool
def test_parallel_4_local_pool_equals_serial(seam, serial_reference):
    seam.hold_winner = True
    seam.neighbour_done.clear()      # the serial reference run already set it
    out, row = _run(parallel=4, task_id="bf-local4")
    assert out["status"] == "completed", out
    assert out["best_params"] == serial_reference["best_params"] == {TP: 8.0, SL: -6.0}
    assert _signature(row) == _signature(serial_reference)
    # It really ran as a batch, out of product order -- the winner landed after its tied peers.
    assert sorted(seam.finished) == sorted((p[TP], p[SL]) for p in PRODUCT)
    assert seam.finished != [(p[TP], p[SL]) for p in PRODUCT]
    assert seam.finished.index((8.0, -6.0)) > seam.finished.index((8.0, -5.0))


# --------------------------------------------------------------------------- remote
def test_remote_only_equals_serial(seam, fake_remote, serial_reference):
    wid = _worker("bf-remote-only")
    out, row = _run(parallel=0, worker_ids=[wid], task_id="bf-remote0")
    assert out["status"] == "completed", out
    assert _signature(row) == _signature(serial_reference)
    assert len(fake_remote) == len(PRODUCT) and set(fake_remote) == {"bf-remote-only"}


def test_local_plus_remote_equals_serial(seam, fake_remote, serial_reference):
    wid = _worker("bf-remote-mixed")
    out, row = _run(parallel=2, worker_ids=[wid], task_id="bf-mixed")
    assert out["status"] == "completed", out
    assert _signature(row) == _signature(serial_reference)
    assert len(seam.finished) == len(PRODUCT)          # every combo ran exactly once


def test_selected_worker_unavailable_falls_back_to_serial(seam, serial_reference):
    wid = _worker("bf-disabled", enabled=False)
    out, row = _run(parallel=1, worker_ids=[wid], task_id="bf-disabled")
    assert out["status"] == "completed", out
    assert _signature(row) == _signature(serial_reference)


# --------------------------------------------------------------------------- cancellation
class _PausingQueue:
    """Task queue whose pause flag rises once *after* trials have landed."""

    def __init__(self, seam, after):
        self.seam, self.after = seam, after

    def is_task_paused(self, task_id):
        return len(self.seam.finished) >= self.after

    def update_progress(self, *a, **k):
        pass


def test_a_cancelled_grid_stops_dispatching(seam, monkeypatch):
    seam.delay = False
    monkeypatch.setattr(H, "get_task_queue", lambda: _PausingQueue(seam, after=3))
    out, _row = _run(parallel=2, task_id="bf-cancel")
    assert out == {"status": "paused"}
    # The flag is read after every landed trial: at most the landed ones plus the two in flight
    # were ever started, never the rest of the 66-combo grid.
    assert len(seam.started) <= 3 + 2 + 1
    assert len(seam.started) < len(PRODUCT)


def test_a_grid_paused_before_it_starts_dispatches_nothing(seam, monkeypatch):
    monkeypatch.setattr(H, "get_task_queue", lambda: _PausingQueue(seam, after=0))
    out, _row = _run(parallel=4, task_id="bf-cancel0")
    assert out == {"status": "paused"}
    assert seam.started == []


# --------------------------------------------------------------------------- the assembler
def test_batch_assembler_dedupes_keys_and_restores_product_order():
    """Two combos with one canonical trial key run ONCE (the serial memo's behaviour) and the
    rows the batch appends in completion order come back in product order."""
    combos = [{"x": i} for i in range(6)]
    key_for = lambda flat: f"k{flat['x'] // 2}"          # (0,1) (2,3) (4,5) share keys
    fitness_of_key = {"k0": 1.0, "k1": 3.0, "k2": 3.0}  # a tie between k1 and k2
    sent = []
    all_results = [{"params": {"seed": True}, "fitness": 0.0, "key": "earlier"}]

    def batch_fitness(param_dicts):
        sent.extend(param_dicts)
        for flat in reversed(param_dicts):               # completion order: reversed
            all_results.append({"params": flat, "fitness": fitness_of_key[key_for(flat)],
                                "key": key_for(flat)})
        return [fitness_of_key[key_for(p)] for p in param_dicts]

    result = H._evaluate_brute_force_batch(combos, key_for, batch_fitness, all_results)
    assert sent == [{"x": 0}, {"x": 2}, {"x": 4}]        # first occurrence of each key only
    assert [r["key"] for r in all_results] == ["earlier", "k0", "k1", "k2"]
    assert result == {"best_params": {"x": 2}, "best_fitness": 3.0,   # first of the tie wins
                      "unmeasured": 0, "n_unique": 3}


def test_batch_assembler_counts_crashed_and_stalled_combos_as_unmeasured():
    """A crashed trial writes no row; a stalled one writes a ``status: stalled`` row with a finite
    sentinel. Neither is a measurement, and the caller fails the grid on either."""
    from app.services.strategy_fitness import STALLED_SENTINEL, ZERO_TRADE_SENTINEL
    combos = [{"x": i} for i in range(4)]
    key_for = lambda flat: f"k{flat['x']}"
    all_results = []

    def batch_fitness(param_dicts):
        all_results.append({"params": {"x": 0}, "fitness": 1.0, "key": "k0"})
        all_results.append({"params": {"x": 2}, "fitness": STALLED_SENTINEL, "key": "k2",
                            "status": "stalled"})
        all_results.append({"params": {"x": 3}, "fitness": ZERO_TRADE_SENTINEL, "key": "k3"})
        # x=1 crashed: no row, sentinel fitness
        return [1.0, ZERO_TRADE_SENTINEL, STALLED_SENTINEL, ZERO_TRADE_SENTINEL]

    result = H._evaluate_brute_force_batch(combos, key_for, batch_fitness, all_results)
    # the measured zero-trade row (k3) counts; the crash (k1) and the stall (k2) do not
    assert (result["unmeasured"], result["n_unique"]) == (2, 4)


# --------------------------------------------------------------------------- failures + state
def test_a_crashed_combo_fails_the_batched_grid(seam, monkeypatch):
    """The serial loop raises on a failed trial; the batched grid must not report a best over the
    survivors instead."""
    seam.delay = False
    real = seam.__call__

    def crashing(config, progress_cb=None, **kw):
        if _tp_sl(config["entry_rules"]) == (3.0, -2.0):
            raise RuntimeError("synthetic trial crash")
        return real(config, progress_cb, **kw)

    from app.services.backtest import daily_backtest_handler
    monkeypatch.setattr(daily_backtest_handler, "run_daily_backtest", crashing)
    out, row = _run(parallel=4, task_id="bf-crash")
    assert out["status"] == "failed", out
    assert "brute-force grid incomplete: 1 of 66 combo(s)" in out["error"]
    assert row["status"] == "failed"


def test_a_stalled_combo_fails_the_batched_grid(seam, monkeypatch):
    seam.delay = False
    real_trial = H._trial_worker

    def stalling_trial(config, metric, *a, **kw):
        if _tp_sl(config["entry_rules"]) == (5.0, -4.0):
            return {"ok": False, "stalled": True, "error": "synthetic stall", "secs": 1.0,
                    "slot": 0}
        return real_trial(config, metric, *a, **kw)

    monkeypatch.setattr(H, "_trial_worker", stalling_trial)
    out, row = _run(parallel=4, task_id="bf-stall")
    assert out["status"] == "failed", out
    assert "brute-force grid incomplete: 1 of 66 combo(s)" in out["error"]
    assert "1 stalled" in out["error"]


def test_brute_force_neither_reads_nor_clears_a_checkpoint(seam, serial_reference):
    """A GA checkpoint under the same task id is foreign to a grid: it is not resumed from and
    it survives the grid's completion."""
    ckpt = {"generation": 2, "population": [[1.0]], "fitnesses": [0.5], "all_results": [],
            "fingerprint": "someone-elses-search"}
    H._save_checkpoint("bf-ckpt", ckpt)
    try:
        out, row = _run(parallel=4, task_id="bf-ckpt")
        assert out["status"] == "completed", out
        assert _signature(row) == _signature(serial_reference)
        assert H._load_checkpoint("bf-ckpt") == ckpt
    finally:
        H._clear_checkpoint("bf-ckpt")


def test_brute_force_never_requests_full_results(seam):
    """n_gens is 1 for a grid, so the GA's `is_last_gen` expression would be True and ask every
    combo for its full-results blob; the brute-force path pins it False."""
    out, _row = _run(parallel=4, task_id="bf-nofull")
    assert out["status"] == "completed", out
    assert out["optimization_id"] not in H._last_gen_full_results_by_opt
