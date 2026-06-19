import types
from app.services.genetic import GeneticOptimizer
from app.services.strategy_param_space import collect_param_space, decode_params


def _strategy():
    buy = {"operator": "AND", "conditions": [
        {"id": "c1", "field": "model:probability", "comparison": ">=", "value": 0.6,
         "optimize": True, "value_min": 0.5, "value_max": 0.9, "value_step": 0.1}]}
    return types.SimpleNamespace(
        initial_tp_percent=5.0, initial_sl_percent=2.0,
        initial_tp_optimize=True, initial_tp_min=2.0, initial_tp_max=10.0, initial_tp_step=1.0,
        initial_sl_optimize=False, initial_sl_min=None, initial_sl_max=None, initial_sl_step=None,
        buy_entry_conditions=buy, sell_entry_conditions=None, entry_conditions=None,
        exit_conditions=[])


def test_collect_decode_through_genetic_optimizer():
    s = _strategy()
    space = collect_param_space(s)            # {'tp':..., 'cond:c1:value':...}
    opt = GeneticOptimizer(param_ranges=space, population_size=4, n_generations=1)
    ind = opt.toolbox.individual()
    flat = opt.decode_individual(ind)         # quantized {name: value}
    decoded = decode_params(s, flat)
    assert 2.0 <= decoded["tp"] <= 10.0
    assert 0.5 <= decoded["buy_tree"]["conditions"][0]["value"] <= 0.9
