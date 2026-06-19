import numpy as np, random
from app.services.genetic import GeneticOptimizer, _np_state_to_jsonable, _jsonable_to_np_state
from app.services.strategy_fitness import compute_fitness, ZERO_TRADE_SENTINEL
from app.services.trial_memo import trial_key, TrialMemo


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
    start_gen, pop = opt.resume_from_checkpoint(legacy_cp)
    assert start_gen == 1 and pop == [[0.5]]


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
