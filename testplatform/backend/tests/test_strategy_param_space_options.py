from app.api.strategies import ExitCondition

# Valid ConditionBase literal: ConditionBase requires `id` and uses `operator`
# (AND/OR) + nested `conditions` list. (The plan's {"logic":...} sketch is not a
# valid ConditionBase, so we adapt it to the real schema.)
EMPTY_GROUP = {"id": "g1", "operator": "AND", "conditions": []}


def test_exitcondition_accepts_option_fields():
    ec = ExitCondition(id="x1", conditions=EMPTY_GROUP, action="buy_call",
        option_strategy="buy_call", option_strike_method="delta", option_strike_param=0.3,
        option_dte_min=20, option_dte_max=45, option_sizing=5.0,
        option_strike_param_optimize=True, option_strike_param_min=0.2,
        option_strike_param_max=0.4, option_strike_param_step=0.05,
        option_dte_optimize=True, option_dte_min_range=20, option_dte_max_range=45, option_dte_step=5)
    assert ec.option_strategy == "buy_call" and ec.option_strike_method == "delta"
    assert ec.option_strike_param == 0.3 and ec.option_dte_min == 20


def test_exitcondition_equity_unchanged():
    ec = ExitCondition(id="e1", conditions=EMPTY_GROUP, action="close")
    assert ec.option_strategy is None and ec.action == "close"
