"""goal2027 2.4: the ONE definition of the same-minute pass order (manage before entry).

Read by the backtest engine and by the live ``ExpertPriority``; the parity tests that pin both callers
are testplatform/backend/tests/backtest/test_same_minute_pass_order_bt.py and
tests/test_same_minute_pass_order_live.py.
"""
import pytest

from ba2_common.core.schedule_genes import SAME_MINUTE_PASS_ORDER, order_same_minute_passes


def test_manage_runs_before_entry():
    assert SAME_MINUTE_PASS_ORDER == ("open_positions", "enter_market")


def test_orders_whatever_the_input_order():
    assert order_same_minute_passes(["enter_market", "open_positions"]) == ["open_positions", "enter_market"]
    assert order_same_minute_passes(["open_positions", "enter_market"]) == ["open_positions", "enter_market"]


def test_a_single_pass_is_unchanged_and_empty_is_empty():
    assert order_same_minute_passes(["enter_market"]) == ["enter_market"]
    assert order_same_minute_passes([]) == []


def test_unknown_pass_is_refused():
    with pytest.raises(ValueError):
        order_same_minute_passes(["enter_market", "close_all"])
