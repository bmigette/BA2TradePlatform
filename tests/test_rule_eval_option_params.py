"""Rule Evaluation Details: an option entry shows its selection box, legibly."""
from ba2_trade_platform.ui.components.RuleEvaluationDisplay import (
    _build_action_params, _option_params_text,
)


def test_an_option_entry_lists_its_selection_box_without_float_noise():
    text = _option_params_text({
        "strike_method": "delta", "strike_param": 0.35000000000000003,
        "dte_min": 40, "dte_max": 60, "sizing": 3.0, "min_one_contract": False,
        "min_open_interest": 100, "max_spread_pct": 15.0,
        "w_premium": -2.0, "w_iv": -2.0, "w_rvol": 1.0,
    })

    assert text[:4] == ["Strike: delta", "param: 0.35", "DTE min: 40", "DTE max: 60"]
    assert "Sizing: 3%" in text and "Min 1 contract: no" in text
    assert "Max spread: 15%" in text and "W rel volume: 1" in text


def test_the_params_reach_the_action_line():
    params = _build_action_params({"option_params": {"strike_method": "delta", "dte_min": 40}})

    assert params == ["Strike: delta", "DTE min: 40"]


def test_an_older_evaluation_without_option_params_renders_as_before():
    assert _build_action_params({}) == []
