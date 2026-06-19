from app.api.strategies import ExitCondition
_C = {"id":"g","operator":"AND","conditions":[]}
def test_toggle_optimize_and_reference_value_accepted():
    ec = ExitCondition(id="r1", conditions=_C, action="adjust_stop_loss",
        reference_value="order_open_price", toggle_optimize=True, action_value=-10.0)
    assert ec.toggle_optimize is True and ec.reference_value == "order_open_price"
def test_defaults_for_equity_rule():
    ec = ExitCondition(id="r2", conditions=_C, action="close")
    assert ec.toggle_optimize is False and ec.reference_value is None
