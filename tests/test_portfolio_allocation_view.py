"""Pure view-model logic for the Portfolio Allocation page.

Nothing here imports NiceGUI, opens a database or talks to a broker: the page
hands plain data to these functions and draws whatever comes back.
"""
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core.portfolio_allocation import PositionFetchFailed
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    GATE_HAS_EXPERTS, GATE_NOT_MANUAL, GATE_NO_ACCOUNT, GATE_OK,
    ManagedLabel, build_label_views, evaluate_gate, filter_selectable_labels,
    is_machine_label, positions_by_symbol,
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


def _pos(symbol, quantity, cost_basis, market_value=None):
    """A PositionState as positions_by_symbol would have produced it."""
    return positions_by_symbol([{'symbol': symbol, 'qty': quantity,
                                 'cost_basis': cost_basis,
                                 'market_value': market_value}])[symbol]


def test_build_label_views_computes_pct_of_label_and_pct_of_total():
    managed = [ManagedLabel('ARK26', 40.0), ManagedLabel('NASDAQ30', 60.0)]
    symbols_by_label = {'ARK26': ['AAPL', 'MSFT'], 'NASDAQ30': ['NVDA']}
    positions = {'AAPL': _pos('AAPL', 10, 6000.0),
                 'MSFT': _pos('MSFT', 5, 2000.0),
                 'NVDA': _pos('NVDA', 4, 2000.0)}
    views = build_label_views(managed, symbols_by_label, positions, {})

    ark = views[0]
    assert ark.label == 'ARK26'
    assert ark.current_value == 8000.0
    assert ark.pct_of_total == 80.0
    aapl = next(r for r in ark.rows if r.symbol == 'AAPL')
    assert aapl.pct_of_label == 75.0
    assert aapl.pct_of_total == 60.0
    msft = next(r for r in ark.rows if r.symbol == 'MSFT')
    assert msft.pct_of_label == 25.0
    assert msft.pct_of_total == 20.0

    nasdaq = views[1]
    assert nasdaq.rows[0].pct_of_label == 100.0
    assert nasdaq.rows[0].pct_of_total == 20.0


def test_build_label_views_symbol_with_no_position_is_listed_with_zeroes():
    """Symbols with no position must still appear — they are editable targets."""
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'TSLA']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)},
                              {})
    tsla = next(r for r in views[0].rows if r.symbol == 'TSLA')
    assert tsla.quantity == 0.0
    assert tsla.cost_basis == 0.0
    assert tsla.pct_of_label == 0.0


def test_build_label_views_symbol_in_two_labels_is_flagged_and_counted_once():
    """Decision 7: targets sum, but the managed total must not double count."""
    views = build_label_views(
        [ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
        {'ARK26': ['TSLA'], 'HighRisk': ['TSLA']},
        {'TSLA': _pos('TSLA', 10, 6000.0)},
        {},
    )
    row = views[0].rows[0]
    assert row.multi_label is True
    assert row.labels == ['ARK26', 'HighRisk']
    # Counted once: TSLA is 100% of total, not 50%.
    assert row.pct_of_total == 100.0


def test_build_label_views_empty_label_has_no_rows_and_zero_current_value():
    views = build_label_views([ManagedLabel('EMPTY', 25.0)], {'EMPTY': []}, {}, {})
    assert views[0].rows == []
    assert views[0].current_value == 0.0
    assert views[0].pct_of_total == 0.0


def test_build_label_views_uses_live_price_for_market_value():
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0, market_value=1100.0)},
                              {'AAPL': 250.0})
    assert views[0].rows[0].price == 250.0
    assert views[0].rows[0].market_value == 2500.0


def test_build_label_views_missing_price_falls_back_to_broker_market_value():
    """No price is NOT a guessed price: the broker's own market value is real data,
    and a symbol with neither reports None."""
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'TSLA']},
                              {'AAPL': _pos('AAPL', 10, 1000.0, market_value=1100.0)},
                              {'AAPL': None, 'TSLA': None})
    aapl = next(r for r in views[0].rows if r.symbol == 'AAPL')
    tsla = next(r for r in views[0].rows if r.symbol == 'TSLA')
    assert aapl.price is None and aapl.market_value == 1100.0
    assert tsla.price is None and tsla.market_value is None


def test_build_label_views_rows_are_ordered_by_current_value_descending():
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'MSFT', 'NVDA']},
                              {'AAPL': _pos('AAPL', 1, 100.0),
                               'MSFT': _pos('MSFT', 1, 900.0),
                               'NVDA': _pos('NVDA', 1, 500.0)},
                              {})
    assert [r.symbol for r in views[0].rows] == ['MSFT', 'NVDA', 'AAPL']


def test_build_label_views_attaches_per_symbol_comments():
    views = build_label_views([ManagedLabel('ARK26', 100.0, comment='core basket')],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 1, 100.0)},
                              {},
                              symbol_comments={('ARK26', 'AAPL'): 'trim on strength'})
    assert views[0].comment == 'core basket'
    assert views[0].rows[0].comment == 'trim on strength'


def test_build_label_views_current_value_is_purchase_value_not_market_value():
    """The user asked for the default view to measure the book at COST. A position
    whose market value has doubled must still show its purchase value."""
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'MSFT']},
                              {'AAPL': _pos('AAPL', 10, 1000.0, market_value=9000.0),
                               'MSFT': _pos('MSFT', 10, 1000.0, market_value=100.0)},
                              {'AAPL': 900.0, 'MSFT': 10.0})
    aapl = next(r for r in views[0].rows if r.symbol == 'AAPL')
    assert aapl.current_value == 1000.0
    assert aapl.market_value == 9000.0
    # Both cost 1000, so cost-based weights are 50/50 despite 90/10 market values.
    assert aapl.pct_of_label == 50.0
    assert views[0].current_value == 2000.0


# The label set actually present in the live database on 2026-08-20.
LIVE_LABELS = [
    'auto_added', 'expert_selected', 'Penny', 'penny-17', 'sp500', 'ARK26',
    'ai_selected', 'fmprating-18', 'penny-4', 'NASDAQ30', 'HighRisk', 'not_found',
    'tradingagents-16', 'ai_selector', 'tech', 'megacap',
]


def test_filter_selectable_labels_hides_the_four_machine_tags():
    out = filter_selectable_labels(LIVE_LABELS)
    for tag in ('auto_added', 'expert_selected', 'ai_selected', 'not_found'):
        assert tag not in out


def test_filter_selectable_labels_hides_the_numbered_expert_families():
    out = filter_selectable_labels(LIVE_LABELS)
    for tag in ('penny-17', 'penny-4', 'fmprating-18', 'tradingagents-16'):
        assert tag not in out


def test_filter_selectable_labels_keeps_user_labels_including_bare_penny():
    """'Penny' with no -N index is a user basket, not a machine tag."""
    out = filter_selectable_labels(LIVE_LABELS)
    assert set(out) == {'Penny', 'sp500', 'ARK26', 'NASDAQ30', 'HighRisk',
                        'ai_selector', 'tech', 'megacap'}


def test_filter_selectable_labels_show_all_is_the_escape_hatch():
    out = filter_selectable_labels(LIVE_LABELS, show_all=True)
    assert 'auto_added' in out
    assert 'penny-17' in out
    assert len(out) == len(LIVE_LABELS)


def test_filter_selectable_labels_is_sorted_case_insensitively_and_deduped():
    out = filter_selectable_labels(['zeta', 'ARK26', 'alpha', 'ARK26', '  ', None])
    assert out == ['alpha', 'ARK26', 'zeta']


def test_is_machine_label_is_case_insensitive():
    assert is_machine_label('AUTO_ADDED') is True
    assert is_machine_label('Penny-4') is True
    assert is_machine_label('Penny') is False
    assert is_machine_label('') is False
