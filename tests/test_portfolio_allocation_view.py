"""Pure view-model logic for the Portfolio Allocation page.

Nothing here imports NiceGUI, opens a database or talks to a broker: the page
hands plain data to these functions and draws whatever comes back.
"""
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core.portfolio_allocation import PositionFetchFailed
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    GATE_HAS_EXPERTS, GATE_NOT_MANUAL, GATE_NO_ACCOUNT, GATE_OK,
    evaluate_gate, positions_by_symbol,
)


def test_gate_no_account_selected_is_blocked_with_no_account_reason():
    """The header selector on 'All accounts' yields account_id None."""
    gate = evaluate_gate(None, True, [])
    assert gate.allowed is False
    assert gate.reason_code == GATE_NO_ACCOUNT
    assert gate.message


def test_gate_manual_flag_off_is_blocked_with_not_manual_reason():
    gate = evaluate_gate(7, False, [])
    assert gate.allowed is False
    assert gate.reason_code == GATE_NOT_MANUAL
    assert gate.expert_names == []


def test_gate_enabled_experts_block_and_are_named_in_the_message():
    gate = evaluate_gate(7, True, ["TradingAgents #3", "PennyMomentum"])
    assert gate.allowed is False
    assert gate.reason_code == GATE_HAS_EXPERTS
    assert gate.expert_names == ["TradingAgents #3", "PennyMomentum"]
    assert "TradingAgents #3" in gate.message
    assert "PennyMomentum" in gate.message


def test_gate_manual_account_with_no_enabled_experts_is_allowed():
    gate = evaluate_gate(7, True, [])
    assert gate.allowed is True
    assert gate.reason_code == GATE_OK
    assert gate.expert_names == []


def test_gate_no_account_takes_precedence_over_every_other_problem():
    """'Pick an account' is the only actionable message when nothing is selected —
    we cannot even know whether the account is manual or has experts."""
    gate = evaluate_gate(None, False, ["SomeExpert"])
    assert gate.reason_code == GATE_NO_ACCOUNT


def test_gate_blank_expert_names_are_dropped_and_do_not_block():
    gate = evaluate_gate(7, True, ["", None])
    assert gate.allowed is True
    assert gate.reason_code == GATE_OK


def test_positions_by_symbol_none_raises_position_fetch_failed():
    """None from get_positions() is a FETCH FAILURE, never a flat account."""
    with pytest.raises(PositionFetchFailed):
        positions_by_symbol(None)


def test_positions_by_symbol_raises_the_shared_engine_class_not_a_local_one():
    """The UI and the live service must raise ONE class, or a service-side
    ``except PositionFetchFailed`` would not catch the page's outage."""
    from ba2_common.core.portfolio_allocation import (
        PositionFetchFailed as EnginePositionFetchFailed,
    )
    assert PositionFetchFailed is EnginePositionFetchFailed
    with pytest.raises(EnginePositionFetchFailed):
        positions_by_symbol(None)


def test_positions_by_symbol_empty_list_is_a_genuinely_flat_account():
    assert positions_by_symbol([]) == {}


def test_positions_by_symbol_reads_broker_objects_and_normalises_symbols():
    raw = [SimpleNamespace(symbol=' aapl ', qty=10.0, cost_basis=1500.0, market_value=1800.0)]
    out = positions_by_symbol(raw)
    assert list(out) == ['AAPL']
    assert out['AAPL'].quantity == 10.0
    assert out['AAPL'].cost_basis == 1500.0
    assert out['AAPL'].market_value == 1800.0


def test_positions_by_symbol_reads_dicts_as_well_as_objects():
    out = positions_by_symbol([{'symbol': 'MSFT', 'qty': 3, 'cost_basis': 900,
                                'market_value': 1000}])
    assert out['MSFT'].quantity == 3.0
    assert out['MSFT'].cost_basis == 900.0


def test_positions_by_symbol_sums_duplicate_rows_for_one_symbol():
    out = positions_by_symbol([
        {'symbol': 'NVDA', 'qty': 2, 'cost_basis': 200, 'market_value': 260},
        {'symbol': 'NVDA', 'qty': 3, 'cost_basis': 330, 'market_value': 390},
    ])
    assert out['NVDA'].quantity == 5.0
    assert out['NVDA'].cost_basis == 530.0
    assert out['NVDA'].market_value == 650.0


def test_positions_by_symbol_missing_quantity_raises_rather_than_defaulting():
    """Platform rule: no fallback values for quantities/balances — raise instead."""
    with pytest.raises(ValueError) as exc:
        positions_by_symbol([{'symbol': 'AAPL', 'cost_basis': 100, 'market_value': 120}])
    assert 'AAPL' in str(exc.value)


def test_positions_by_symbol_missing_cost_basis_raises_rather_than_defaulting():
    with pytest.raises(ValueError) as exc:
        positions_by_symbol([{'symbol': 'AAPL', 'qty': 1, 'market_value': 120}])
    assert 'AAPL' in str(exc.value)
