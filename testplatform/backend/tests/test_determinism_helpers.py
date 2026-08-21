import json
import logging
import numpy as np, random
from app.services.genetic import (
    GeneticOptimizer,
    _np_state_to_jsonable,
    _jsonable_to_np_state,
    _py_state_to_jsonable,
    _jsonable_to_py_state,
)
from app.services.strategy_fitness import compute_fitness, ZERO_TRADE_SENTINEL
from app.services.trial_memo import trial_key, TrialMemo


def _space():
    return {"a": {"type": "float", "min": 0, "max": 1, "step": 0.1}}


def _json_column_round_trip(cp):
    """Exactly what TaskQueue.checkpoint_data (a JSON column) does to a checkpoint dict.

    This is the whole point: in-memory the inner 625-int state is still a tuple and
    random.setstate() accepts it -- only the JSON round trip turns it into a list.
    """
    return json.loads(json.dumps(cp))


def test_np_state_roundtrip():
    np.random.seed(123); _ = np.random.rand(5)
    state = np.random.get_state()
    restored = _jsonable_to_np_state(_np_state_to_jsonable(state))
    np.random.set_state(restored)
    a = np.random.rand(3)
    np.random.set_state(state)
    b = np.random.rand(3)
    assert np.allclose(a, b)


def test_np_state_jsonable_is_json_serializable():
    import json
    np.random.seed(42); _ = np.random.rand(7)
    j = _np_state_to_jsonable(np.random.get_state())
    # Must survive a JSON round-trip (checkpoint persistence requirement).
    reparsed = json.loads(json.dumps(j))
    restored = _jsonable_to_np_state(reparsed)
    np.random.set_state(restored)
    a = np.random.rand(4)
    np.random.set_state(_jsonable_to_np_state(j))
    b = np.random.rand(4)
    assert np.allclose(a, b)


def test_checkpoint_includes_np_random_state():
    space = {"a": {"type": "float", "min": 0, "max": 1, "step": 0.1}}
    opt = GeneticOptimizer(param_ranges=space, population_size=3, n_generations=1)
    pop = opt.toolbox.population(n=3)
    cp = opt.get_checkpoint_data(0, pop)
    assert "np_random_state" in cp
    assert "random_state" in cp  # legacy field still present (backward compatible)


def test_resume_restores_np_random_state():
    space = {"a": {"type": "float", "min": 0, "max": 1, "step": 0.1}}
    opt = GeneticOptimizer(param_ranges=space, population_size=3, n_generations=1)
    np.random.seed(99); _ = np.random.rand(3)
    cp = opt.get_checkpoint_data(0, opt.toolbox.population(n=3))
    expected = np.random.rand(5)
    # Perturb numpy state, then resume from checkpoint should restore it.
    np.random.seed(1); _ = np.random.rand(50)
    opt.resume_from_checkpoint(cp)
    got = np.random.rand(5)
    assert np.allclose(expected, got)


def test_resume_backward_compatible_without_np_state():
    # Old checkpoints lack np_random_state: resume must not raise.
    space = {"a": {"type": "float", "min": 0, "max": 1, "step": 0.1}}
    opt = GeneticOptimizer(param_ranges=space, population_size=3, n_generations=1)
    legacy_cp = {
        "generation": 0,
        "population": [[0.5]],
        "best_individual": None,
        "best_fitness": None,
        "history": [],
        "random_state": list(random.getstate()),
    }
    start_gen, pop, fits = opt.resume_from_checkpoint(legacy_cp)
    assert start_gen == 1 and pop == [[0.5]]
    assert fits is None          # pre-'fitnesses' checkpoint: everything re-evaluates


# --------------------------------------------------------------------------------------------
# Python (stdlib ``random``) state -- the mirror of the numpy pair above.
#
# random.getstate() is (version, internal_state[625 ints], gauss_next). Storing it as
# list(random.getstate()) only converts the OUTER tuple; JSON then turns the INNER tuple into a
# list and random.setstate() rejects it with "state vector must be a tuple". The exception was
# caught and logged as a warning, so a resumed run silently continued on an un-restored RNG.
# --------------------------------------------------------------------------------------------

def test_py_state_roundtrip_through_json():
    random.seed(123); _ = [random.random() for _ in range(5)]
    state = random.getstate()
    restored = _jsonable_to_py_state(_json_column_round_trip(_py_state_to_jsonable(state)))
    random.setstate(restored)
    a = [random.random() for _ in range(3)]
    random.setstate(state)
    b = [random.random() for _ in range(3)]
    assert a == b


def test_py_state_jsonable_rebuilds_the_inner_tuple():
    """setstate() requires the 625-int vector to be a TUPLE; JSON hands back a list."""
    random.seed(7)
    rebuilt = _jsonable_to_py_state(_json_column_round_trip(_py_state_to_jsonable(random.getstate())))
    assert isinstance(rebuilt, tuple)
    assert isinstance(rebuilt[1], tuple), "inner state vector must be a tuple, not a list"


def test_resume_restores_py_random_state_through_a_json_column():
    """THE regression: a resumed run must draw the same numbers the interrupted one would have."""
    opt = GeneticOptimizer(param_ranges=_space(), population_size=3, n_generations=1)
    random.seed(99); _ = [random.random() for _ in range(3)]
    cp = _json_column_round_trip(opt.get_checkpoint_data(0, opt.toolbox.population(n=3)))
    expected = [random.random() for _ in range(5)]
    # Perturb the Python RNG, then resume from the checkpoint should restore it exactly.
    random.seed(1); _ = [random.random() for _ in range(50)]
    opt.resume_from_checkpoint(cp)
    got = [random.random() for _ in range(5)]
    assert got == expected


def test_resume_restores_py_random_state_without_warning(caplog):
    """A successful restore must not be reported as a failure (the bug was warning-only)."""
    opt = GeneticOptimizer(param_ranges=_space(), population_size=3, n_generations=1)
    cp = _json_column_round_trip(opt.get_checkpoint_data(0, opt.toolbox.population(n=3)))
    with caplog.at_level(logging.WARNING, logger="app.services.genetic"):
        opt.resume_from_checkpoint(cp)
    assert not [r for r in caplog.records if "random state" in r.message]


def test_resume_restores_legacy_py_random_state_checkpoint():
    """A checkpoint written by the OLD code (list(random.getstate()) -> JSON) is already in the
    database; it has the same shape, so it must restore rather than crash."""
    opt = GeneticOptimizer(param_ranges=_space(), population_size=3, n_generations=1)
    random.seed(4242); _ = [random.random() for _ in range(2)]
    legacy_cp = _json_column_round_trip({
        "generation": 0,
        "population": [[0.5]],
        "best_individual": None,
        "best_fitness": None,
        "history": [],
        "random_state": list(random.getstate()),      # exactly what the buggy writer produced
    })
    expected = [random.random() for _ in range(5)]
    random.seed(1); _ = [random.random() for _ in range(50)]
    opt.resume_from_checkpoint(legacy_cp)
    assert [random.random() for _ in range(5)] == expected


def test_resume_without_py_random_state_does_not_raise():
    """Missing key: nothing to restore, resume must still succeed."""
    opt = GeneticOptimizer(param_ranges=_space(), population_size=3, n_generations=1)
    start_gen, pop, fits = opt.resume_from_checkpoint(
        {"generation": 2, "population": [[0.5]], "history": []})
    assert start_gen == 3 and pop == [[0.5]] and fits is None


def test_resume_with_malformed_py_random_state_warns_and_continues(caplog):
    """Malformed value: degrade to a warning, never take the whole resume down."""
    opt = GeneticOptimizer(param_ranges=_space(), population_size=3, n_generations=1)
    with caplog.at_level(logging.WARNING, logger="app.services.genetic"):
        start_gen, pop, fits = opt.resume_from_checkpoint(
            {"generation": 2, "population": [[0.5]], "random_state": "not-a-state"})
    assert start_gen == 3 and pop == [[0.5]]
    assert [r for r in caplog.records if "Could not restore random state" in r.message]
    # ...and the RNG is still usable afterwards.
    random.random()


def test_fitness_max_drawdown_negated():
    assert compute_fitness("max_drawdown", {"total_trades": 4, "max_drawdown": 12.0}) == -12.0


def test_fitness_zero_trades_sentinel_distinct_from_zero():
    f = compute_fitness("sharpe", {"total_trades": 0, "sharpe_ratio": 2.0})
    assert f == ZERO_TRADE_SENTINEL and f != 0.0


def test_fitness_none_results_sentinel():
    assert compute_fitness("sharpe", None) == ZERO_TRADE_SENTINEL


def test_fitness_nan_inf_collapse_to_sentinel():
    assert compute_fitness("sharpe", {"total_trades": 3, "sharpe_ratio": float("nan")}) == ZERO_TRADE_SENTINEL
    assert compute_fitness("sharpe", {"total_trades": 3, "sharpe_ratio": float("inf")}) == ZERO_TRADE_SENTINEL


def test_fitness_maps_keys():
    assert compute_fitness("sharpe", {"total_trades": 1, "sharpe_ratio": 1.5}) == 1.5
    assert compute_fitness("return", {"total_trades": 1, "total_return": 33.0}) == 33.0
    assert compute_fitness("profit_factor", {"total_trades": 1, "profit_factor": 2.1}) == 2.1
    assert compute_fitness("win_rate", {"total_trades": 1, "win_rate": 55.0}) == 55.0
    assert compute_fitness("sortino", {"total_trades": 1, "sortino_ratio": 1.2}) == 1.2
    assert compute_fitness("calmar", {"total_trades": 1, "calmar_ratio": 0.9}) == 0.9
    assert compute_fitness("sqn", {"total_trades": 1, "sqn": 3.3}) == 3.3


def test_fitness_unknown_metric_raises():
    import pytest
    with pytest.raises(ValueError):
        compute_fitness("not_a_metric", {"total_trades": 1})


def test_trial_key_stable_and_order_independent():
    a = trial_key({"model_id": 1, "params": {"tp": 5, "sl": 2}})
    b = trial_key({"params": {"sl": 2, "tp": 5}, "model_id": 1})
    assert a == b


def test_memo_hit_miss():
    m = TrialMemo(); k = trial_key({"x": 1})
    assert m.get(k) is None and m.misses == 1
    m.put(k, 0.9)
    assert m.get(k) == 0.9 and m.hits == 1


def test_seeded_population_reproducible():
    space = {"a": {"type": "float", "min": 0, "max": 1, "step": 0.1}}
    random.seed(7); np.random.seed(7)
    p1 = [list(GeneticOptimizer(param_ranges=space, population_size=5, n_generations=1)
                .toolbox.individual()) for _ in range(5)]
    random.seed(7); np.random.seed(7)
    p2 = [list(GeneticOptimizer(param_ranges=space, population_size=5, n_generations=1)
                .toolbox.individual()) for _ in range(5)]
    assert p1 == p2
