"""Early stopping with a MINIMUM RELATIVE IMPROVEMENT (``earlyStoppingMinRelativeImprovement``).

THE DECISION (operator, 2026-09-26). The GA must not spend ~40 generations for a +0.1 fitness
gain. A generation resets the patience counter only when its best beats the best at the LAST
COUNTED improvement by at least ``min_rel`` (``best >= baseline * (1 + min_rel)`` for a positive
baseline; the magnitude for a negative one; any strict gain over a zero one). A smaller gain
still becomes the best individual -- only the patience clock ignores it.

ABSENT (or None) IS THE LEGACY RULE, BYTE FOR BYTE: any strict improvement resets, the stored
optimization_config / checkpoint / checkpoint fingerprint / discovery job name are unchanged.

Pinned here, layer by layer:
  1. the rule itself (genetic.early_stop_counts / early_stop_threshold / validation);
  2. the live GA loop on the measured stage-1 shape (2.95, 8.8, 23.83, 23.9, 23.95, ...);
  3. resume mid-patience: the counter AND the baseline survive (the c9d83b4d pattern: derived
     from history, never stored-and-trusted), and the baseline is NOT the running best;
  4. the legacy rule is untouched when the key is absent;
  5. plumbing through the REAL handler and the REAL launcher CLI (the trial-config-whitelist trap:
     a knob that parses but never reaches the loop), refusal of invalid values everywhere;
  6. the grid driver / stage1_run.sh wiring and the job-name identity.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import shutil
import subprocess
import sys

import pytest

from app.services import genetic as G
from app.services.genetic import (
    EARLY_STOP_MIN_REL_KEY,
    GeneticOptimizer,
    early_stop_counts,
    early_stop_threshold,
    validate_early_stop_min_rel,
)

# sibling test modules (test_strategy_optimization_handler / test_equity_cap_launcher) are
# imported for their fixtures, the way test_ga_lattice_anchor does
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(os.path.dirname(_BACKEND))
_DRIVER = os.path.join(_ROOT, "tools", "run_options_matrix.py")
_STAGE1 = os.path.join(_ROOT, "tools", "stage1_run.sh")

#: The measured shape that motivated the rule: two big jumps, then a creep of sub-1% gains.
_CREEP = [2.95, 8.8, 23.83, 23.9, 23.95, 24.0, 24.05, 24.06, 24.07, 24.08, 24.09, 24.1,
          24.11, 24.12, 24.13, 24.14]


# --------------------------------------------------------------------------------------------
# 1. The rule
# --------------------------------------------------------------------------------------------
def test_the_key_name_is_the_documented_one():
    assert EARLY_STOP_MIN_REL_KEY == "earlyStoppingMinRelativeImprovement"


def test_positive_baseline_needs_the_relative_gain():
    assert early_stop_threshold(23.83, 0.01) == pytest.approx(24.0683)
    assert not early_stop_counts(23.9, 23.83, 0.01)
    assert not early_stop_counts(24.068, 23.83, 0.01)
    assert early_stop_counts(24.0683 + 1e-12, 23.83, 0.01)
    assert early_stop_counts(30.0, 23.83, 0.01)


def test_the_threshold_itself_counts():
    """``>=``: reaching exactly the bar counts (100 -> 101 at 1%)."""
    assert early_stop_threshold(100.0, 0.01) == 101.0
    assert early_stop_counts(101.0, 100.0, 0.01)


def test_negative_baseline_uses_the_magnitude():
    """-10 at 1% needs -9.9 ("less bad by 1% of the magnitude"); multiplying by (1 + min_rel)
    would have demanded -10.1, i.e. counted a WORSE generation."""
    assert early_stop_threshold(-10.0, 0.01) == pytest.approx(-9.9)
    assert not early_stop_counts(-9.95, -10.0, 0.01)
    assert early_stop_counts(-9.9, -10.0, 0.01)
    assert early_stop_counts(5.0, -10.0, 0.01), "crossing zero is always a counted gain"
    assert not early_stop_counts(-10.5, -10.0, 0.01), "a worse generation never counts"


def test_the_failure_sentinel_is_left_behind_by_any_real_score():
    """A generation of failed/zero-trade trials scores -1e9; the next real score must count."""
    assert early_stop_counts(-500.0, G.FITNESS_EVALUATION_FAILED, 0.01)
    assert early_stop_counts(0.0, G.FITNESS_EVALUATION_FAILED, 0.01)


def test_zero_baseline_counts_any_strict_gain_and_never_an_equal_one():
    assert early_stop_threshold(0.0, 0.01) == 0.0
    assert early_stop_counts(1e-9, 0.0, 0.01)
    assert not early_stop_counts(0.0, 0.0, 0.01), "an equal fitness must never reset patience"
    assert not early_stop_counts(-1e-9, 0.0, 0.01)


def test_the_first_generation_always_counts():
    for min_rel in (None, 0.01, 0.5):
        assert early_stop_counts(-3.0, None, min_rel)


def test_none_is_the_strict_rule():
    assert early_stop_counts(23.8300001, 23.83, None)
    assert not early_stop_counts(23.83, 23.83, None)
    assert not early_stop_counts(23.0, 23.83, None)


@pytest.mark.parametrize("value,expected", [(None, None), (0.01, 0.01), ("0.01", 0.01),
                                            (1e-6, 1e-6), (0.999, 0.999)])
def test_valid_values(value, expected):
    assert validate_early_stop_min_rel(value) == expected


@pytest.mark.parametrize("value", [0, 0.0, -0.0, "0", -0.01, -1e-12, 1.0, 1.5, 100,
                                   float("nan"), float("inf"), float("-inf"), True, False, "abc",
                                   "", [0.01], {"x": 1}])
def test_invalid_values_are_refused(value):
    with pytest.raises(ValueError, match=EARLY_STOP_MIN_REL_KEY):
        validate_early_stop_min_rel(value)


@pytest.mark.parametrize("zero", [0, 0.0, "0"])
def test_zero_is_refused_with_a_pointer_to_the_legacy_rule(zero):
    """0 behaves exactly like the legacy rule but would give the job a new name/fingerprint --
    a legacy-equivalent run under a different identity. Omit the key instead."""
    with pytest.raises(ValueError, match="omit it"):
        validate_early_stop_min_rel(zero)


def test_the_optimizer_refuses_an_invalid_value_at_construction():
    with pytest.raises(ValueError, match=EARLY_STOP_MIN_REL_KEY):
        GeneticOptimizer(param_ranges=_SPACE, population_size=4, n_generations=2,
                         early_stopping_min_rel=float("nan"))


# --------------------------------------------------------------------------------------------
# 2. Patience on the measured shape -- derived from history and in the LIVE loop
# --------------------------------------------------------------------------------------------
def _hist(*fitnesses):
    return [{"generation": i, "best_fitness": f} for i, f in enumerate(fitnesses)]


def test_history_derivation_on_the_creep():
    derive = GeneticOptimizer.patience_state_from_history
    assert derive(_hist(*_CREEP[:3]), 0.01) == (0, 23.83)
    # 23.9, 23.95, 24.0, 24.05, 24.06: five non-qualifying generations, baseline still 23.83
    assert derive(_hist(*_CREEP[:8]), 0.01) == (5, 23.83)
    # the legacy rule on the same history: every creep step is a strict record
    assert derive(_hist(*_CREEP[:8]), None) == (0, 24.06)


def test_legacy_derivation_is_the_old_function():
    """no_improvement_from_history(h) with no min_rel is exactly the pre-change count."""
    for fits in ([], [1.0], [1.0, 2.0, 2.0, 2.0], [1.0, 5.0, 4.0, 4.5], _CREEP,
                 [3.0, None, 3.0, 2.0]):
        h = _hist(*fits)
        assert GeneticOptimizer.no_improvement_from_history(h) == _old_no_improvement(h)


def _old_no_improvement(history):
    """Verbatim copy of the pre-change GeneticOptimizer.no_improvement_from_history (7fd6bba9)."""
    running = None
    last_improved = -1
    for i, entry in enumerate(history or []):
        try:
            fitness = entry["best_fitness"]
        except (KeyError, TypeError):
            continue
        if fitness is None:
            continue
        if running is None or fitness > running:
            running, last_improved = fitness, i
    if last_improved < 0:
        return 0
    return max(0, len(history) - 1 - last_improved)


_SPACE = {"x": {"min": 0, "max": 50, "step": 1, "type": "int"},
          "y": {"min": 0.0, "max": 1.0, "step": 0.1, "type": "float"}}


def _optimizer(min_rel, patience=5, generations=16, seed=7):
    random.seed(seed)
    kw = {} if min_rel == "absent" else {"early_stopping_min_rel": min_rel}
    # crossover/mutation certain: every offspring is re-evaluated each generation, so a
    # generation's best is exactly seq[gen] whatever the seed draws.
    return GeneticOptimizer(param_ranges=_SPACE, population_size=6, n_generations=generations,
                            crossover_prob=1.0, mutation_prob=1.0,
                            early_stopping_generations=patience, **kw)


def _run(opt, seq, *, start_generation=0, initial_population=None, restored_fitnesses=None,
         checkpoints=None):
    """Drive the REAL optimize() loop with every generation's newly evaluated individuals scoring
    ``seq[gen]`` -- so the generation best is exactly ``seq[gen]`` for a non-decreasing seq."""
    state = {"gen": None}

    def on_start(gen):
        state["gen"] = gen

    def batch(param_dicts):
        return [seq[state["gen"]]] * len(param_dicts)

    def ckpt(gen, population, partial=False):
        if checkpoints is not None and not partial:
            # through JSON, like the real checkpoint column
            checkpoints[gen] = json.loads(json.dumps(opt.get_checkpoint_data(gen, population)))

    return opt.optimize(lambda p: 0.0, start_generation=start_generation,
                        initial_population=initial_population,
                        restored_fitnesses=restored_fitnesses, checkpoint_callback=ckpt,
                        on_generation_start=on_start, batch_fitness=batch)


def test_the_live_loop_stops_after_five_non_qualifying_generations():
    opt = _optimizer(0.01)
    res = _run(opt, _CREEP)
    gens = [h["best_fitness"] for h in res["history"]]
    # 2.95, 8.8, 23.83 count; 23.9 .. 24.06 are the 5 non-qualifying generations -> stop at gen 7
    assert gens == _CREEP[:8]
    assert res["generations_run"] == 8
    # the best individual still followed the sub-threshold gains
    assert res["best_fitness"] == 24.06
    assert opt._es_baseline == 23.83


def test_the_legacy_rule_keeps_creeping_on_the_same_sequence():
    res = _run(_optimizer(None), _CREEP)
    assert res["generations_run"] == len(_CREEP), "every creep step is a strict record"


def test_a_qualifying_gain_resets_and_moves_the_baseline():
    seq = [2.95, 8.8, 23.83, 23.9, 23.95, 24.0, 24.1, 24.1, 24.1, 24.1, 24.1, 24.1, 24.1, 24.1]
    opt = _optimizer(0.01)
    res = _run(opt, seq)
    # 24.1 >= 24.0683 counts at gen 6, then 5 flat generations -> stop at gen 11
    assert res["generations_run"] == 12
    assert opt._es_baseline == 24.1


def test_every_generation_logs_counted_and_the_patience_clock(caplog):
    caplog.set_level(logging.INFO, logger=G.logger.name)
    _run(_optimizer(0.01), _CREEP)
    lines = [r.getMessage() for r in caplog.records if "patience" in r.getMessage()
             and r.getMessage().startswith("Gen ")]
    assert len(lines) == 8
    assert lines[2].startswith("Gen 2: improvement counted")
    assert "patience 0/5 (best 23.8300, needs >= 24.0683" in lines[2]
    assert lines[5].startswith("Gen 5: no counted improvement (gen best 24.0000)")
    assert "patience 3/5 (best 23.8300, needs >= 24.0683, running best 24.0000" in lines[5]
    assert "min_rel 0.01" in lines[5]
    assert any("no improvement of at least 1.00% over 23.8300 for 5 generations" in r.getMessage()
               for r in caplog.records)


def test_the_checkpoint_publishes_the_clock_and_the_baseline():
    cps = {}
    _run(_optimizer(0.01), _CREEP, checkpoints=cps)
    cp = cps[5]  # history 2.95 .. 24.0
    assert cp["no_improvement_count"] == 3
    assert cp["early_stop_min_rel"] == 0.01
    assert cp["early_stop_baseline"] == 23.83
    assert cp["early_stop_needs"] == pytest.approx(24.0683)


def test_the_live_state_equals_the_history_derivation_at_every_generation():
    """Resume re-derives from history, so the two must agree everywhere (one rule, two callers)."""
    cps = {}
    opt = _optimizer(0.01)
    _run(opt, _CREEP, checkpoints=cps)
    live = []
    baseline, count = None, 0
    for f in _CREEP[:8]:
        if early_stop_counts(f, baseline, 0.01):
            baseline, count = f, 0
        else:
            count += 1
        live.append((count, baseline))
    for gen, cp in cps.items():
        assert (cp["no_improvement_count"], cp["early_stop_baseline"]) == live[gen], gen


# --------------------------------------------------------------------------------------------
# 3. Resume mid-patience
# --------------------------------------------------------------------------------------------
def test_resume_mid_patience_restores_the_counter_and_the_baseline_not_the_running_best():
    """Checkpoint at gen 5: patience 3/5, baseline 23.83 while the running best is 24.0.

    The discriminating step is 24.1 at gen 7: it clears 23.83 * 1.01 = 24.0683 but NOT
    24.0 * 1.01 = 24.24. A resume that took the running best as the baseline would stop at gen 7
    (patience 5/5); the correct one resets there and stops five flat generations later.
    """
    seq = [2.95, 8.8, 23.83, 23.9, 23.95, 24.0, 24.05, 24.1] + [24.1] * 8
    cps = {}
    first = _optimizer(0.01, seed=11)
    _run(first, seq, checkpoints=cps)
    ckpt = cps[5]

    resumed = _optimizer(0.01, seed=999)          # a different process: nothing carried over
    start, pop, fits = resumed.resume_from_checkpoint(ckpt)
    assert start == 6
    assert resumed._resumed_no_improvement == 3
    assert resumed._es_baseline == 23.83
    assert resumed.best_fitness != resumed._es_baseline, "precondition: creep has happened"

    res = _run(resumed, seq, start_generation=start, initial_population=pop,
               restored_fitnesses=fits)
    last = res["history"][-1]["generation"]
    assert last > 7, "the resumed run measured against the running best, not the baseline"
    assert resumed._es_baseline == 24.1
    # the checkpointed generation's population is re-scored as generation 6 (no new trials), so
    # gen 6 is non-counted (4/5), 24.1 counts at gen 7, then gens 8..12 are flat -> stop at 12.
    assert last == 12


def test_resume_of_a_legacy_checkpoint_under_the_rule_derives_the_baseline():
    """A checkpoint carries no baseline key unless written under the rule; derivation still
    produces it (and a stored one is never trusted over history)."""
    resumed = _optimizer(0.01)
    ck = {"history": _hist(*_CREEP[:6]), "best_fitness": 24.0, "best_individual": None,
          "generation": 5, "early_stop_baseline": 999.0, "no_improvement_count": 0}
    resumed.resume_from_checkpoint(ck)
    assert (resumed._resumed_no_improvement, resumed._es_baseline) == (3, 23.83)


def test_resume_without_the_rule_is_the_legacy_clock():
    resumed = _optimizer(None)
    ck = {"history": _hist(*_CREEP[:6]), "best_fitness": 24.0, "best_individual": None,
          "generation": 5}
    resumed.resume_from_checkpoint(ck)
    assert resumed._resumed_no_improvement == 0   # 24.0 is a strict record at gen 5


def test_resume_replays_the_checkpointed_generation_costing_one_generation_of_patience():
    """PINS A KNOWN QUIRK (deliberately unfixed 2026-09-26 -- job 1 of stage 1 is running on the
    legacy rule and a fix would change what a resume of it does).

    The checkpoint is written after a generation is EVALUATED but before it REPRODUCES, and a
    resume starts at checkpoint.generation + 1 with that same, fully evaluated population. So the
    first resumed generation evaluates NOTHING: it re-records the checkpointed generation's best
    under the next generation number, counts as a non-improvement, and only then breeds. Each
    resume therefore costs exactly ONE generation of the budget and of patience, under either
    rule. A later fix must update this test on purpose.
    """
    for min_rel in (None, 0.01):
        seq = _CREEP
        cps, straight_evals = {}, []
        opt = _optimizer(min_rel, patience=5, seed=21)
        _run_counting(opt, seq, straight_evals, checkpoints=cps)

        resumed = _optimizer(min_rel, patience=5, seed=21)
        start, pop, fits = resumed.resume_from_checkpoint(cps[4])
        clock_at_ckpt = resumed._resumed_no_improvement
        resumed_evals = []
        res = _run_counting(resumed, seq, resumed_evals, start_generation=start,
                            initial_population=pop, restored_fitnesses=fits)
        hist = res["history"]
        # the replayed generation: no trials, the checkpointed best re-recorded under gen 5
        assert dict(resumed_evals)[5] == 0, min_rel
        assert hist[5]["generation"] == 5 and hist[5]["best_fitness"] == hist[4]["best_fitness"]
        # ...and it costs one generation of patience
        assert GeneticOptimizer.no_improvement_from_history(hist[:6], min_rel) == clock_at_ckpt + 1
        # the uninterrupted run evaluated new offspring at gen 5; the resumed one did not
        assert dict(straight_evals)[5] > 0
        after = lambda evals, g0: sum(1 for g, n in evals if g > g0 and n > 0)  # noqa: E731
        stop_straight = max(g for g, _ in straight_evals)
        stop_resumed = hist[-1]["generation"]
        assert (after(resumed_evals, 4), stop_resumed) == (
            after(straight_evals, 4) - 1, stop_straight), \
            "the resumed run must do exactly one generation of new trials fewer"


def _run_counting(opt, seq, evals, **kw):
    """_run, also recording (generation, number of individuals evaluated) per generation."""
    state = {"gen": None}

    def on_start(gen):
        state["gen"] = gen
        evals.append((gen, 0))

    def batch(param_dicts):
        evals[-1] = (state["gen"], len(param_dicts))
        return [seq[state["gen"]]] * len(param_dicts)

    cps = kw.pop("checkpoints", None)

    def ckpt(gen, population, partial=False):
        if cps is not None and not partial:
            cps[gen] = json.loads(json.dumps(opt.get_checkpoint_data(gen, population)))

    return opt.optimize(lambda p: 0.0, checkpoint_callback=ckpt, on_generation_start=on_start,
                        batch_fitness=batch, **kw)


# --------------------------------------------------------------------------------------------
# 4. Absent flag = the legacy GA, byte for byte
# --------------------------------------------------------------------------------------------
_LEGACY_CKPT_KEYS = {"generation", "population", "fitnesses", "best_individual", "best_fitness",
                     "history", "no_improvement_count", "random_state", "np_random_state",
                     "ga_random_state"}


@pytest.mark.parametrize("absent", ["absent", None])
def test_absent_or_none_leaves_the_search_and_its_checkpoints_byte_identical(absent):
    """The key absent and explicitly None are the same legacy search: identical history, best and
    checkpoints, and the checkpoints carry no new key."""
    seq = [1.0, 2.0, 2.0, 2.5, 2.5, 2.6, 2.6, 2.6, 2.6, 2.6, 2.6, 2.6, 2.6, 2.6, 2.6, 2.6]
    ref_cps, cps = {}, {}
    ref = _run(_optimizer("absent", patience=3), seq, checkpoints=ref_cps)
    got = _run(_optimizer(absent, patience=3), seq, checkpoints=cps)
    assert json.dumps(got, sort_keys=True, default=str) == json.dumps(ref, sort_keys=True,
                                                                      default=str)
    assert json.dumps(cps, sort_keys=True) == json.dumps(ref_cps, sort_keys=True)
    assert all(set(cp) == _LEGACY_CKPT_KEYS for cp in cps.values())
    # legacy stopping: 2.6 at gen 5 is a strict record; gens 6..8 flat -> stop at 8
    assert got["generations_run"] == 9


# --------------------------------------------------------------------------------------------
# 5. Plumbing: the real handler, the real launcher CLI
# --------------------------------------------------------------------------------------------
def _handler_env(monkeypatch):
    import test_strategy_optimization_handler as T
    from app.models.database import Base, engine
    from app.services import strategy_optimization_handler as H

    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(H, "_run_trial_backtest", T._deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    return T, H


def _spy_optimizer(monkeypatch, H, seen):
    Real = H.GeneticOptimizer

    class _Spy(Real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen["min_rel"] = self.early_stopping_min_rel
            seen["optimizer"] = self

    monkeypatch.setattr(H, "GeneticOptimizer", _Spy)


def _spy_rule(monkeypatch):
    """Record every early_stop_counts call's min_rel. The handler suppresses logging while it
    runs trials, so the loop's log lines cannot be asserted through it -- the rule calls can."""
    calls = []
    real = G.early_stop_counts

    def spy(best_fit, baseline, min_rel):
        calls.append(min_rel)
        return real(best_fit, baseline, min_rel)

    monkeypatch.setattr(G, "early_stop_counts", spy)
    return calls


def test_the_handler_runs_the_ga_loop_under_the_configured_rule(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    T, H = _handler_env(monkeypatch)
    seen = {}
    _spy_optimizer(monkeypatch, H, seen)
    calls = _spy_rule(monkeypatch)
    sid = T._seed_strategy()
    opt_id = T._seed_opt(sid, config=T._ga_config(
        populationSize=4, generations=3, **{EARLY_STOP_MIN_REL_KEY: 0.01}))
    out = H.handle_strategy_optimization("t-minrel-on", {"optimization_id": opt_id})
    assert out["status"] == "completed", out
    assert seen["min_rel"] == 0.01
    # ...and it is what the LOOP applied, not just a stored attribute
    assert calls and set(calls) == {0.01}
    assert seen["optimizer"]._es_baseline is not None, "the loop never ran the min_rel branch"
    # the monitor line survives the handler's global logging.disable(INFO)
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("Gen ") and "patience" in r.getMessage()]
    assert len(lines) >= 1 and all("needs >= " in m and "min_rel 0.01" in m for m in lines)


def test_the_handler_without_the_key_runs_the_legacy_rule(monkeypatch):
    T, H = _handler_env(monkeypatch)
    seen = {}
    _spy_optimizer(monkeypatch, H, seen)
    calls = _spy_rule(monkeypatch)
    sid = T._seed_strategy()
    opt_id = T._seed_opt(sid, config=T._ga_config(populationSize=4, generations=2))
    out = H.handle_strategy_optimization("t-minrel-off", {"optimization_id": opt_id})
    assert out["status"] == "completed", out
    assert seen["min_rel"] is None
    assert set(calls) <= {None}
    assert seen["optimizer"]._es_baseline is None, "the legacy loop must not touch the baseline"


@pytest.mark.parametrize("bad", [0, 0.0, -0.01, 1.0, 2, float("nan"), True, "abc"])
def test_the_handler_fails_a_job_with_an_invalid_value(monkeypatch, bad):
    T, H = _handler_env(monkeypatch)
    sid = T._seed_strategy()
    opt_id = T._seed_opt(sid, config=T._ga_config(**{EARLY_STOP_MIN_REL_KEY: bad}))
    out = H.handle_strategy_optimization(f"t-minrel-bad-{bad!r}", {"optimization_id": opt_id})
    assert out["status"] == "failed"
    assert EARLY_STOP_MIN_REL_KEY in out["error"]


class _PausingQueue:
    """The real task queue, except ``is_task_paused`` turns True once the watched optimizer has
    recorded ``pause_at`` generations -- checked by the handler at the END of ga_callback, i.e.
    before that generation's checkpoint, so the checkpoint left behind is generation
    ``pause_at - 2``. (fitness_function also asks, but during a generation's evaluation the
    history is one entry shorter, so it never trips there.)"""

    def __init__(self, real, seen, pause_at):
        self._real, self._seen, self._pause_at = real, seen, pause_at

    def is_task_paused(self, task_id):
        opt = self._seen.get("optimizer")
        return bool(opt is not None and self._pause_at is not None
                    and len(opt.history) >= self._pause_at)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _seed_named_opt(T, sid, name, config):
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization
    db = SessionLocal()
    try:
        row = StrategyOptimization(strategy_id=sid, name=name, fitness_metric="sharpe",
                                   optimization_type="genetic", optimization_config=config,
                                   status="pending")
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _handler_run(monkeypatch, H, T, sid, name, config, pause_at, tag):
    seen, resumed = {}, {}
    Real = H.GeneticOptimizer

    class _Spy(Real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen["optimizer"] = self

        def resume_from_checkpoint(self, checkpoint):
            out = super().resume_from_checkpoint(checkpoint)
            resumed.update(ckpt=checkpoint, start=out[0],
                           clock=self._resumed_no_improvement, baseline=self._es_baseline)
            return out

    monkeypatch.setattr(H, "GeneticOptimizer", _Spy)
    real_q = H.get_task_queue()
    monkeypatch.setattr(H, "get_task_queue", lambda: _PausingQueue(real_q, seen, pause_at))
    opt_id = _seed_named_opt(T, sid, name, config)
    out = H.handle_strategy_optimization(f"t-{tag}", {"optimization_id": opt_id})
    return out, seen, resumed


def test_resume_through_the_handlers_checkpoint_store_restores_the_rule_state(monkeypatch):
    """END TO END under the rule: a real handler run is paused mid-search, its checkpoint goes
    through _save_checkpoint/_load_checkpoint (the TaskQueue JSON column), and a relaunch of the
    same job name resumes with the patience clock AND the baseline derived from that history."""
    T, H = _handler_env(monkeypatch)
    name = "minrel-resume-e2e"
    H._clear_checkpoint(H.checkpoint_task_id(name, 0))
    sid = T._seed_strategy()
    cfg = T._ga_config(populationSize=6, generations=10, earlyStoppingGenerations=50,
                       **{EARLY_STOP_MIN_REL_KEY: 0.01})

    out, _, resumed = _handler_run(monkeypatch, H, T, sid, name, cfg, pause_at=7, tag="p1")
    assert out["status"] == "paused", out
    assert not resumed, "the first run must start fresh"
    stored = H._load_checkpoint(H.checkpoint_task_id(name, 0))
    assert stored and stored["generation"] == 5 and stored["early_stop_min_rel"] == 0.01
    expect = GeneticOptimizer.patience_state_from_history(stored["history"], 0.01)
    assert expect == (stored["no_improvement_count"], stored["early_stop_baseline"])
    assert expect[0] > 0, "precondition: the pause must land mid-patience"

    out, seen, resumed = _handler_run(monkeypatch, H, T, sid, name, cfg, pause_at=None, tag="p2")
    assert out["status"] == "completed", out
    assert resumed["start"] == 6
    assert (resumed["clock"], resumed["baseline"]) == expect
    assert seen["optimizer"].history[0]["generation"] == 0, "history was carried over"
    assert H._load_checkpoint(H.checkpoint_task_id(name, 0)) is None, "cleared on completion"


def test_a_legacy_checkpoint_is_discarded_not_resumed_under_the_rule(monkeypatch):
    """Fingerprint mismatch: the checkpoint of the same job name written under the legacy rule is
    DISCARDED by _load_checkpoint and the rule-run starts from generation 0."""
    T, H = _handler_env(monkeypatch)
    name = "minrel-legacy-ckpt"
    H._clear_checkpoint(H.checkpoint_task_id(name, 0))
    sid = T._seed_strategy()
    legacy = T._ga_config(populationSize=6, generations=10, earlyStoppingGenerations=50)
    out, _, _ = _handler_run(monkeypatch, H, T, sid, name, legacy, pause_at=7, tag="l1")
    assert out["status"] == "paused", out
    assert H._load_checkpoint(H.checkpoint_task_id(name, 0))["generation"] == 5

    rule = {**legacy, EARLY_STOP_MIN_REL_KEY: 0.01}
    out, seen, resumed = _handler_run(monkeypatch, H, T, sid, name, rule, pause_at=None, tag="l2")
    assert out["status"] == "completed", out
    assert not resumed, "a legacy-rule checkpoint must never be resumed under the rule"
    assert seen["optimizer"].history[0]["generation"] == 0


def test_the_fingerprint_is_unchanged_without_the_rule_and_differs_with_it():
    from app.services import strategy_optimization_handler as H
    space = {"x": {"type": "float", "min": 3.0, "max": 20.0, "step": 2.0}}
    ga = {"populationSize": 40, "generations": 8}
    legacy = H.checkpoint_fingerprint(space, ga)
    import hashlib
    payload = {"genes": [["x", sorted(space["x"].items())]], "population": 40, "generations": 8}
    assert legacy == hashlib.sha1(json.dumps(payload, sort_keys=False, default=str)
                                  .encode("utf-8")).hexdigest()[:16]
    assert H.checkpoint_fingerprint(space, {**ga, EARLY_STOP_MIN_REL_KEY: None}) == legacy
    with_rule = H.checkpoint_fingerprint(space, {**ga, EARLY_STOP_MIN_REL_KEY: 0.01})
    assert with_rule != legacy
    assert with_rule != H.checkpoint_fingerprint(space, {**ga, EARLY_STOP_MIN_REL_KEY: 0.02})


def test_launcher_cli_to_handler_to_ga_loop_end_to_end(monkeypatch):
    """THE WHITELIST TRAP: the flag must survive the launcher's key-by-key config build AND reach
    the optimizer the handler builds. Launcher -> persisted optimization_config -> real handler."""
    from app.services import strategy_optimization_handler as H
    real_handle = H.handle_strategy_optimization   # _run_optimize stubs it on the module
    from test_equity_cap_launcher import _BASE_ARGV, _parse, _run_optimize

    args = _parse(_BASE_ARGV + ["--early-stop-min-rel", "0.01"])
    assert args.early_stop_min_rel == 0.01
    cfg = _run_optimize(args, monkeypatch)
    assert cfg[EARLY_STOP_MIN_REL_KEY] == 0.01
    monkeypatch.setattr(H, "handle_strategy_optimization", real_handle)

    T, H = _handler_env(monkeypatch)
    seen = {}
    _spy_optimizer(monkeypatch, H, seen)
    sid = T._seed_strategy()
    ga_keys = {k: cfg[k] for k in ("earlyStoppingGenerations", EARLY_STOP_MIN_REL_KEY)}
    opt_id = T._seed_opt(sid, config=T._ga_config(populationSize=4, generations=2, **ga_keys))
    out = H.handle_strategy_optimization("t-minrel-e2e", {"optimization_id": opt_id})
    assert out["status"] == "completed", out
    assert seen["min_rel"] == 0.01
    assert seen["optimizer"].early_stopping_generations == cfg["earlyStoppingGenerations"]


def test_launcher_without_the_flag_writes_no_new_key(monkeypatch):
    from test_equity_cap_launcher import _BASE_ARGV, _parse, _run_optimize
    args = _parse(_BASE_ARGV)
    assert args.early_stop_min_rel is None
    assert EARLY_STOP_MIN_REL_KEY not in _run_optimize(args, monkeypatch)


def test_launcher_batch_carries_the_flag(monkeypatch):
    from test_equity_cap_launcher import _BATCH_ARGV, _parse, _run_optimize_batch
    cfg = _run_optimize_batch(_parse(_BATCH_ARGV + ["--early-stop-min-rel", "0.02"],
                                     cmd_attr="_cmd_optimize_batch"), monkeypatch)
    assert cfg[EARLY_STOP_MIN_REL_KEY] == 0.02
    cfg = _run_optimize_batch(_parse(_BATCH_ARGV, cmd_attr="_cmd_optimize_batch"), monkeypatch)
    assert EARLY_STOP_MIN_REL_KEY not in cfg


@pytest.mark.parametrize("bad", ["0", "0.0", "-0.01", "1", "1.0", "nan", "inf", "abc"])
def test_launcher_refuses_an_invalid_value_at_parse_time(bad):
    import test_equity_cap_launcher as E
    with pytest.raises(SystemExit):
        E.L.main(list(E._BASE_ARGV) + ["--early-stop-min-rel", bad])


# --------------------------------------------------------------------------------------------
# 6. Grid driver + stage1_run.sh + identity
# --------------------------------------------------------------------------------------------
def _driver():
    spec = importlib.util.spec_from_file_location("rom_minrel", _DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_GRID_ARGV = ["--profile", "discovery", "--start", "2020-01-01", "--end", "2025-12-31",
              "--screener-gate-store", "/x/metric_store", "--max-stock-price", "0",
              "--early-stop", "8"]


def _name_and_cmd(mod, *extra, strat="O_LP"):
    a = mod.resolve_args(mod.build_parser(), _GRID_ARGV + list(extra))
    name = mod.discovery_name(a, _DRIVER, "optm-X-" + strat, "FMPRating", strat, "AAPL",
                              "legacy")
    return name, mod.build_cmd(a, _DRIVER, name, "FMPRating", strat, "AAPL", "legacy")


def test_driver_forwards_nothing_when_absent_and_the_value_when_given():
    mod = _driver()
    _, cmd = _name_and_cmd(mod)
    assert "--early-stop-min-rel" not in cmd
    _, cmd = _name_and_cmd(mod, "--early-stop-min-rel", "0.01")
    assert cmd[cmd.index("--early-stop-min-rel") + 1] == "0.01"


def test_driver_digest_takes_the_flag_and_not_the_strategy_list():
    mod = _driver()
    legacy, _ = _name_and_cmd(mod)
    on, _ = _name_and_cmd(mod, "--early-stop-min-rel", "0.01")
    assert on != legacy, "a different stopping rule must never share a name (checkpoint key)"
    assert on != _name_and_cmd(mod, "--early-stop-min-rel", "0.02")[0]
    # --strategies selects WHICH jobs run; it is not a per-job identity token
    assert _name_and_cmd(mod, "--strategies", "O_LP,O_VERT")[0] == legacy
    assert (_name_and_cmd(mod, "--early-stop-min-rel", "0.01", "--strategies", "O_LP")[0] == on)
    # spelling does not matter, the float does
    assert _name_and_cmd(mod, "--early-stop-min-rel", "1e-2")[0] == on


@pytest.mark.parametrize("bad", ["0", "0.0", "-0.01", "1", "1.5", "nan", "inf"])
def test_driver_refuses_invalid_values(bad):
    mod = _driver()
    with pytest.raises(SystemExit):
        mod.resolve_args(mod.build_parser(), _GRID_ARGV + ["--early-stop-min-rel", bad])


def _stage1_text():
    with open(_STAGE1, encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


def test_stage1_run_sh_forwards_the_env_var_only_when_set(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not available on this host")
    text = _stage1_text()
    assert '${MINREL_ARGS[@]+"${MINREL_ARGS[@]}"}' in text
    start = text.index('STAGE1_EARLY_STOP_MIN_REL="${STAGE1_EARLY_STOP_MIN_REL:-}"')
    end = text.index("fi\n", start) + 3
    script = tmp_path / "minrel.sh"
    script.write_text("set -euo pipefail\n" + text[start:end]
                      + 'printf "<%s>" ${MINREL_ARGS[@]+"${MINREL_ARGS[@]}"}; echo END\n',
                      encoding="utf-8", newline="\n")

    def run(env):
        e = {k: v for k, v in os.environ.items() if k != "STAGE1_EARLY_STOP_MIN_REL"}
        return subprocess.run([bash, str(script)], capture_output=True, text=True,
                              env={**e, **env})

    unset = run({})
    assert unset.returncode == 0 and unset.stdout.strip() == "<>END", unset
    empty = run({"STAGE1_EARLY_STOP_MIN_REL": ""})
    assert empty.stdout.strip() == "<>END"
    on = run({"STAGE1_EARLY_STOP_MIN_REL": "0.01"})
    assert on.stdout.strip() == "<--early-stop-min-rel><0.01>END"


def test_stage1_run_sh_keeps_its_launch_line_when_unset():
    """The only change to the exec line is the (empty-when-unset) MINREL_ARGS expansion."""
    text = _stage1_text()
    exec_line = text[text.index("exec /opt/ba2worker"):]
    assert "--early-stop 8" in exec_line
    assert exec_line.index("MINREL_ARGS") < exec_line.index('"$@"'), \
        "caller args must come last so `--early-stop 5` still overrides"
