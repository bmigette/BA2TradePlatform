import types
from app.services.genetic import GeneticOptimizer
from app.services.strategy_param_space import collect_param_space, decode_params


def _strategy():
    entry_rules = [{
        "id": "r1",
        "conditions": {"operator": "AND", "conditions": [
            {"id": "c1", "field": "model:probability", "comparison": ">=", "value": 0.6,
             "optimize": True, "value_min": 0.5, "value_max": 0.9, "value_step": 0.1}]},
        "actions": [
            {"action_type": "buy"},
            {"action_type": "adjust_take_profit", "reference_value": "order_open_price",
             "action_value": 5.0, "action_value_optimize": True,
             "action_value_min": 2.0, "action_value_max": 10.0, "action_value_step": 1.0},
        ],
        "continue_processing": False,
    }]
    return types.SimpleNamespace(entry_rules=entry_rules, exit_rules=[])


def test_collect_decode_through_genetic_optimizer():
    s = _strategy()
    space = collect_param_space(s)  # {'cond:c1:value':..., 'entry:r1:a1:action_value':...}
    opt = GeneticOptimizer(param_ranges=space, population_size=4, n_generations=1)
    ind = opt.toolbox.individual()
    flat = opt.decode_individual(ind)  # quantized {name: value}
    decoded = decode_params(s, flat)
    rule = decoded["entry_rules"][0]
    assert 0.5 <= rule["conditions"]["conditions"][0]["value"] <= 0.9
    assert 2.0 <= rule["actions"][1]["action_value"] <= 10.0


def test_encode_params_round_trips_choice_genes():
    """encode_params must invert decode_individual for a choice-type gene (a string value ->
    its index in config['choices']), not just pass the raw value through -- otherwise a
    warm-started chromosome carries a string in a slot every other GA op (mutate/decode)
    treats as a numeric index, breaking immediately."""
    space = {
        "model:mode": {"type": "choice", "choices": ["static", "dynamic"], "min": 0, "max": 1, "step": 1},
        "model:scale": {"type": "float", "min": 0.0, "max": 2.0, "step": 0.25},
    }
    opt = GeneticOptimizer(param_ranges=space, population_size=4, n_generations=1)

    ind = opt.encode_params({"model:mode": "dynamic", "model:scale": 1.0})
    assert ind[0] == 1  # index of "dynamic" in choices
    assert ind[1] == 1.0
    assert opt.decode_individual(ind)["model:mode"] == "dynamic"

    ind0 = opt.encode_params({"model:mode": "static", "model:scale": 0.5})
    assert ind0[0] == 0
    assert opt.decode_individual(ind0)["model:mode"] == "static"

    # Unrecognised choice value (e.g. source param space differs) falls back to index 0
    # instead of raising or corrupting the chromosome.
    ind_unknown = opt.encode_params({"model:mode": "not-a-real-choice", "model:scale": 0.0})
    assert ind_unknown[0] == 0
