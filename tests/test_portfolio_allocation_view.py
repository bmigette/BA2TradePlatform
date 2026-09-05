"""Pure view-model logic for the Portfolio Allocation page.

Nothing here imports NiceGUI, opens a database or talks to a broker: the page
hands plain data to these functions and draws whatever comes back.
"""
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core.portfolio_allocation import (
    VALUATION_MODE_COST, VALUATION_MODE_MARKET, PositionFetchFailed,
)
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    ACCOUNT_VALUE_TITLE, ACCOUNT_VALUE_UNAVAILABLE_DETAIL,
    ACCOUNT_VALUE_UNAVAILABLE_TEXT, account_value_card, account_value_from_snapshot,
    BUYING_POWER_TITLE, BUYING_POWER_UNAVAILABLE_DETAIL, FIGURE_UNKNOWN_TEXT,
    DEFAULT_MACHINE_LABEL_FAMILIES, GATE_HAS_EXPERTS, GATE_NOT_MANUAL, GATE_NO_ACCOUNT,
    GATE_OK, LEGACY_MACHINE_LABEL_FAMILIES,
    EDIT_BLANK, EDIT_LABELS_OVER_100, EDIT_NEGATIVE, EDIT_NOT_A_NUMBER, EDIT_OK,
    EDIT_OVER_100,
    ManagedLabel, build_label_views, collect_managed_symbols, diff_managed_labels,
    evaluate_gate, expert_shortname_families, filter_selectable_labels, is_machine_label,
    managed_total_value, missing_quote_symbols, parse_pct, picker_options,
    DEFAULT_LABEL_ICON_COLOR, LABEL_COLOR_PALETTE, LABEL_STATUS_NONE,
    LABEL_STATUS_OK, LABEL_STATUS_OVER, LABEL_STATUS_TOLERANCE_PCT,
    LABEL_STATUS_UNDER, LabelView, NO_LABEL_COLOR,
    bar_scale_pct, build_label_bars, format_allocation_footer,
    sort_label_views,
    format_label_header, format_label_target_tooltip, format_label_total_notice,
    delta_color,
    format_base_composition,
    format_delta,
    format_reserve_caption,
    symbol_delta,
    format_reserve_row,
    label_color_options, normalise_label_color, resolve_label_icon_color,
    store_color_value,
    positions_by_symbol, reserve_dollars, symbol_target_values,
    validate_label_target_edit, validate_reserve_edit, validate_symbol_weight_edit,
    FILL_ALREADY_100, FILL_FILLED_EMPTY, FILL_NO_SYMBOLS, FILL_SCALED_DOWN,
    FILL_SCALED_UP, fill_label_to_100,
    BASIS_LEGEND, LABEL_TARGET_CAPTION, RESERVE_BASIS_NOTE,
    MIN_GRAPHICAL_CONTRAST, SURFACE_COLOR, contrast_ratio, format_label_delta,
    label_color_contrast_warning, relative_luminance,
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
    views = build_label_views(managed, symbols_by_label, positions, {},
                              valuation_mode=VALUATION_MODE_COST)

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
                              {}, valuation_mode=VALUATION_MODE_COST)
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
        valuation_mode=VALUATION_MODE_COST,
    )
    row = views[0].rows[0]
    assert row.multi_label is True
    assert row.labels == ['ARK26', 'HighRisk']
    # Counted once: TSLA is 100% of total, not 50%.
    assert row.pct_of_total == 100.0


def test_build_label_views_empty_label_has_no_rows_and_zero_current_value():
    views = build_label_views([ManagedLabel('EMPTY', 25.0)], {'EMPTY': []}, {}, {},
                              valuation_mode=VALUATION_MODE_COST)
    assert views[0].rows == []
    assert views[0].current_value == 0.0
    assert views[0].pct_of_total == 0.0


def test_build_label_views_uses_live_price_for_market_value():
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0, market_value=1100.0)},
                              {'AAPL': 250.0}, valuation_mode=VALUATION_MODE_COST)
    assert views[0].rows[0].price == 250.0
    assert views[0].rows[0].market_value == 2500.0


def test_build_label_views_missing_price_falls_back_to_broker_market_value():
    """No price is NOT a guessed price: the broker's own market value is real data,
    and a symbol with neither reports None."""
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'TSLA']},
                              {'AAPL': _pos('AAPL', 10, 1000.0, market_value=1100.0)},
                              {'AAPL': None, 'TSLA': None},
                              valuation_mode=VALUATION_MODE_COST)
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
                              {}, valuation_mode=VALUATION_MODE_COST)
    assert [r.symbol for r in views[0].rows] == ['MSFT', 'NVDA', 'AAPL']


def test_build_label_views_attaches_per_symbol_comments():
    views = build_label_views([ManagedLabel('ARK26', 100.0, comment='core basket')],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 1, 100.0)},
                              {},
                              symbol_comments={('ARK26', 'AAPL'): 'trim on strength'},
                              valuation_mode=VALUATION_MODE_COST)
    assert views[0].comment == 'core basket'
    assert views[0].rows[0].comment == 'trim on strength'


def test_build_label_views_current_value_is_purchase_value_not_market_value():
    """In COST mode a position whose market value has doubled still shows its
    purchase value. Cost is no longer the default (market is, see W1) but it stays a
    supported mode, and this is what it means."""
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'MSFT']},
                              {'AAPL': _pos('AAPL', 10, 1000.0, market_value=9000.0),
                               'MSFT': _pos('MSFT', 10, 1000.0, market_value=100.0)},
                              {'AAPL': 900.0, 'MSFT': 10.0},
                              valuation_mode=VALUATION_MODE_COST)
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


# --- the bulk-quote request list -------------------------------------------

def test_collect_managed_symbols_dedupes_across_labels():
    """A symbol in two managed labels must be quoted once, not twice."""
    out = collect_managed_symbols({'ARK26': ['TSLA', 'ROKU'], 'HighRisk': ['TSLA']})
    assert out == ['ROKU', 'TSLA']


def test_collect_managed_symbols_normalises_and_sorts():
    out = collect_managed_symbols({'ARK26': [' tsla ', 'aapl']})
    assert out == ['AAPL', 'TSLA']


def test_collect_managed_symbols_drops_blanks_and_empty_labels():
    out = collect_managed_symbols({'ARK26': ['AAPL', '', None], 'EMPTY': []})
    assert out == ['AAPL']


def test_collect_managed_symbols_of_nothing_is_empty():
    assert collect_managed_symbols({}) == []
    assert collect_managed_symbols(None) == []


def test_collect_managed_symbols_normalises_before_it_dedupes():
    """Two spellings of one symbol are ONE quote, not two.

    Normalising after the de-duplication (``sorted({s.strip() for ...})`` then
    upper-casing) still returns two entries for a legacy ``tsla`` instrument row
    and a modern ``TSLA`` one. That double-quotes the broker, and -- worse --
    ``build_label_views`` keys ``prices`` on the NORMALISED symbol, so the
    surviving lower-case entry would be silently unpriced and the row would fall
    back to the broker's stale market value.
    """
    assert collect_managed_symbols({'ARK26': ['tsla'], 'HighRisk': [' TSLA ']}) == ['TSLA']


# --- the label picker's eager-persistence diff -----------------------------

def test_diff_managed_labels_reports_additions_and_removals():
    to_add, to_remove = diff_managed_labels(['ARK26', 'HighRisk'], ['ARK26', 'NASDAQ30'])
    assert to_add == ['NASDAQ30']
    assert to_remove == ['HighRisk']


def test_diff_managed_labels_unchanged_selection_is_two_empty_lists():
    """Eager persistence fires on every change event; an unchanged selection must
    be a no-op rather than a pointless write."""
    assert diff_managed_labels(['ARK26'], ['ARK26']) == ([], [])


def test_diff_managed_labels_is_order_independent():
    assert diff_managed_labels(['A', 'B'], ['B', 'A']) == ([], [])


def test_diff_managed_labels_from_nothing_adds_everything():
    to_add, to_remove = diff_managed_labels([], ['ARK26', 'HighRisk'])
    assert to_add == ['ARK26', 'HighRisk']
    assert to_remove == []


def test_diff_managed_labels_ignores_blank_and_none_entries():
    to_add, to_remove = diff_managed_labels(['ARK26', None], ['ARK26', '  '])
    assert (to_add, to_remove) == ([], [])


def test_diff_managed_labels_is_case_sensitive_because_labels_are():
    """'ark26' and 'ARK26' are two DIFFERENT baskets, not one typed two ways.

    Instrument labels are matched raw by ``get_symbols_by_label``, so folding
    case here would make the picker report "no change" while the account ends up
    managing a label that resolves to no instruments at all. This is the same
    deliberate asymmetry as ``filter_selectable_labels``, which sorts
    case-insensitively but de-duplicates case-sensitively.
    """
    to_add, to_remove = diff_managed_labels(['ARK26'], ['ark26'])
    assert (to_add, to_remove) == (['ark26'], ['ARK26'])


def test_label_views_in_cost_mode_measure_positions_at_their_cost_basis():
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)},
                              {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_COST)
    assert views[0].current_value == 1000.0
    assert views[0].rows[0].current_value == 1000.0
    assert views[0].rows[0].market_value == 2500.0     # still reported, just not the basis


def test_label_views_in_market_mode_measure_positions_at_quantity_times_price():
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)},
                              {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_MARKET)
    assert views[0].current_value == 2500.0
    assert views[0].rows[0].current_value == 2500.0
    assert views[0].rows[0].cost_basis == 1000.0       # still reported


def test_market_mode_changes_the_percentages_a_doubled_position_reports():
    """AAPL doubled, MSFT flat. In cost mode they are 50/50; in market mode 67/33."""
    positions = {'AAPL': _pos('AAPL', 10, 1000.0), 'MSFT': _pos('MSFT', 10, 1000.0)}
    prices = {'AAPL': 200.0, 'MSFT': 100.0}
    symbols = {'ARK26': ['AAPL', 'MSFT']}

    cost = build_label_views([ManagedLabel('ARK26', 100.0)], symbols, positions, prices,
                             valuation_mode=VALUATION_MODE_COST)
    market = build_label_views([ManagedLabel('ARK26', 100.0)], symbols, positions, prices,
                               valuation_mode=VALUATION_MODE_MARKET)

    cost_by = {r.symbol: r for r in cost[0].rows}
    market_by = {r.symbol: r for r in market[0].rows}
    assert cost_by['AAPL'].pct_of_label == 50.0
    assert market_by['AAPL'].pct_of_label == pytest.approx(66.67, abs=0.01)
    assert market_by['MSFT'].pct_of_label == pytest.approx(33.33, abs=0.01)


def test_market_mode_without_a_price_reports_zero_current_value_not_a_guess():
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)},
                              {'AAPL': None},
                              valuation_mode=VALUATION_MODE_MARKET)
    assert views[0].rows[0].current_value == 0.0
    assert views[0].rows[0].price is None


def test_build_label_views_requires_an_explicit_valuation_mode():
    """No default at all, matching ``build_base_snapshot`` and the three solvers.

    A default here is worth nothing and costs a lot: whichever way it points, a
    caller that forgets the keyword gets a page whose percentages are measured on a
    different definition of "current value" from the plan the wizard then solves.
    Omitting it must be a loud ``TypeError``, not a silent reinterpretation.
    """
    with pytest.raises(TypeError):
        build_label_views([ManagedLabel('ARK26', 100.0)],
                          {'ARK26': ['AAPL']},
                          {'AAPL': _pos('AAPL', 10, 1000.0)},
                          {'AAPL': 250.0})


def test_build_label_views_rejects_an_unknown_valuation_mode():
    with pytest.raises(ValueError):
        build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                          {'AAPL': _pos('AAPL', 10, 1000.0)}, {},
                          valuation_mode='marketish')


def test_market_mode_leaves_the_labels_cost_basis_reporting_cost():
    """``LabelView.cost_basis`` is the COST column and must not follow the mode.

    Before the mode existed the two were the same number and the field was filled
    from the same local, so a mode-aware total that forgot this field would make
    the label's "cost basis" silently become its market value -- and the row-level
    ``cost_basis`` beside it would then disagree with the label total above it.
    """
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'MSFT']},
                              {'AAPL': _pos('AAPL', 10, 1000.0),
                               'MSFT': _pos('MSFT', 10, 1000.0)},
                              {'AAPL': 200.0, 'MSFT': 100.0},
                              valuation_mode=VALUATION_MODE_MARKET)
    assert views[0].current_value == 3000.0
    assert views[0].cost_basis == 2000.0
    assert sum(r.cost_basis for r in views[0].rows) == 2000.0


def test_market_mode_counts_a_two_label_symbol_once_in_pct_of_total():
    """The distinct-value denominator has to survive the mode change too.

    AAPL carries both labels; if the market-mode total summed per label instead of
    per distinct symbol it would count AAPL twice, and every pct_of_total on the
    page would be quietly deflated.
    """
    views = build_label_views([ManagedLabel('ARK26', 50.0), ManagedLabel('TECH', 50.0)],
                              {'ARK26': ['AAPL'], 'TECH': ['AAPL', 'MSFT']},
                              {'AAPL': _pos('AAPL', 10, 100.0),
                               'MSFT': _pos('MSFT', 10, 100.0)},
                              {'AAPL': 30.0, 'MSFT': 10.0},
                              valuation_mode=VALUATION_MODE_MARKET)
    # Distinct managed market value is 300 + 100 = 400, NOT 300 + 300 + 100.
    aapl = next(r for r in views[0].rows if r.symbol == 'AAPL')
    assert aapl.pct_of_total == 75.0
    assert views[0].pct_of_total == 75.0
    assert views[1].pct_of_total == 100.0


def test_market_mode_never_values_a_position_at_the_brokers_own_market_value():
    """No price means 0, even when the broker stamped a market value on the row.

    ``current_value``'s docstring is explicit that ``PositionState.market_value``
    is not consulted: it can be stamped at a different price from the live quote,
    and the base, the percentages and every delta must be measured with the SAME
    price. The display column may still show the broker's figure -- the BASIS may
    not be it.
    """
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0, market_value=9000.0)},
                              {'AAPL': None},
                              valuation_mode=VALUATION_MODE_MARKET)
    row = views[0].rows[0]
    assert row.current_value == 0.0
    assert row.market_value == 9000.0      # displayed, never used as the basis
    assert views[0].current_value == 0.0


# ---------------------------------------------------------------------------
# F-C1 -- the picker's option list must contain every MANAGED label
# ---------------------------------------------------------------------------

def test_picker_options_contain_a_managed_label_that_no_instrument_carries():
    """The bug this pins is silent destruction of configuration.

    NiceGUI's ``Select._event_args_to_value`` ends with
    ``[arg for arg in args if arg in self._values]`` and
    ``_value_to_model_value`` skips anything ``self._values.index()`` cannot find,
    so a selected value that is not an OPTION is invisible to the browser and is
    dropped from the very first change event. The picker then reports a selection
    with that label missing, and ``replace_managed_labels`` deletes its row plus
    every per-symbol weight and comment beneath it.

    A managed label whose last instrument was removed is exactly that case, so the
    options must be the UNION of the selectable labels and the managed ones.
    """
    out = picker_options(['ARK26', 'tech'], managed=['ARK26', 'GhostBasket'])
    assert 'GhostBasket' in out
    assert out == ['ARK26', 'GhostBasket', 'tech']


def test_picker_options_keep_a_managed_machine_tag_even_though_it_is_hidden():
    """Hiding a machine tag from the CHOICES must not delete it from the SELECTION.

    Someone who deliberately managed 'penny-17' (the picker's 'show all' escape
    hatch exists for exactly that) would otherwise lose it the moment they
    re-opened the picker with the switch off.
    """
    out = picker_options(['penny-17', 'tech'], managed=['penny-17'])
    assert out == ['penny-17', 'tech']


def test_picker_options_do_not_duplicate_a_managed_label_that_is_also_in_use():
    out = picker_options(['ARK26', 'tech'], managed=['ARK26'])
    assert out.count('ARK26') == 1


def test_picker_options_show_all_still_hides_nothing():
    out = picker_options(['auto_added', 'tech'], managed=['GhostBasket'], show_all=True)
    assert out == ['auto_added', 'GhostBasket', 'tech']


def test_picker_options_ignore_blank_and_none_managed_entries():
    assert picker_options(['tech'], managed=['', None, '  ']) == ['tech']


def test_picker_options_strip_a_padded_managed_label():
    """``get_all_instrument_labels`` strips; the managed side has to agree or the
    padded spelling shows up as a second, unselectable option."""
    assert picker_options(['tech'], managed=[' tech ']) == ['tech']


# ---------------------------------------------------------------------------
# F-I1 -- the headline total must use the SAME denominator as the rows
# ---------------------------------------------------------------------------

def test_managed_total_value_counts_a_two_label_symbol_once():
    """``sum(v.current_value for v in views)`` double-counts every shared symbol.

    ``build_label_views`` computes ``pct_of_total`` against the DISTINCT membership
    set (decision 7), so summing the per-label totals for the headline puts a
    different denominator above the rows it explains.
    """
    views = build_label_views(
        [ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
        {'ARK26': ['TSLA', 'AAPL'], 'HighRisk': ['TSLA']},
        {'TSLA': _pos('TSLA', 10, 6000.0), 'AAPL': _pos('AAPL', 1, 1000.0)},
        {},
        valuation_mode=VALUATION_MODE_COST,
    )
    assert sum(v.current_value for v in views) == 13000.0     # the double count
    assert managed_total_value(views) == 7000.0


def test_managed_total_value_is_the_denominator_pct_of_total_was_computed_with():
    views = build_label_views(
        [ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
        {'ARK26': ['TSLA', 'AAPL'], 'HighRisk': ['TSLA']},
        {'TSLA': _pos('TSLA', 10, 6000.0), 'AAPL': _pos('AAPL', 1, 1000.0)},
        {},
        valuation_mode=VALUATION_MODE_COST,
    )
    total = managed_total_value(views)
    row = next(r for v in views for r in v.rows if r.symbol == 'TSLA')
    assert row.pct_of_total == pytest.approx(row.current_value / total * 100.0)


def test_managed_total_value_of_no_views_is_zero():
    assert managed_total_value([]) == 0.0


# ---------------------------------------------------------------------------
# F-I2 -- the machine-tag families come from the rule, not from a snapshot
# ---------------------------------------------------------------------------

def test_expert_shortname_families_derives_the_prefix_shortname_generates():
    """``MarketExpertInterface.shortname`` is
    ``f"{self.__class__.__name__.lower()}-{self.id}"``, so the family of every
    expert class is just its lower-cased class name."""
    class WeeklyOptionsScalper:
        pass

    class FMPRating:
        pass

    assert expert_shortname_families([WeeklyOptionsScalper, FMPRating]) == frozenset(
        {'weeklyoptionsscalper', 'fmprating'})


def test_expert_shortname_families_of_nothing_is_empty():
    assert expert_shortname_families([]) == frozenset()
    assert expert_shortname_families(None) == frozenset()


def test_a_newly_registered_expert_family_is_hidden_without_editing_the_regex():
    """The literal snapshot could not know about this class; the rule can."""
    class WeeklyOptionsScalper:
        pass

    families = expert_shortname_families([WeeklyOptionsScalper])
    assert is_machine_label('weeklyoptionsscalper-9', families) is True
    assert filter_selectable_labels(['weeklyoptionsscalper-9', 'tech'],
                                    machine_families=families) == ['tech']


def test_legacy_families_survive_a_registry_derived_set():
    """'penny-17' is on live instrument rows but no class is named ``Penny`` any
    more (it was renamed ``PennyMomentumTrader``). A purely registry-derived set
    would un-hide those two live tags."""
    assert 'penny' in LEGACY_MACHINE_LABEL_FAMILIES
    assert 'penny' in DEFAULT_MACHINE_LABEL_FAMILIES

    class FMPRating:
        pass

    families = expert_shortname_families([FMPRating])
    assert is_machine_label('penny-17', families) is True


def test_the_machine_family_pattern_is_end_anchored():
    """Dropping the ``$`` would classify the user label 'penny-17-core' as a
    machine tag and silently remove it from the picker."""
    assert is_machine_label('penny-17-core') is False
    assert is_machine_label('penny-17') is True
    assert filter_selectable_labels(['penny-17-core']) == ['penny-17-core']


def test_the_machine_family_pattern_is_start_anchored():
    assert is_machine_label('my-penny-17') is False


def test_a_family_name_is_matched_literally_not_as_a_pattern():
    """The families are interpolated straight into a regex, so they must be escaped.

    Class names cannot contain a metacharacter today, but the set is now DERIVED
    from a registry rather than hand-written, so nothing here gets to assume that.
    """
    families = frozenset({'fmp.ating'})
    assert is_machine_label('fmp.ating-3', families) is True
    assert is_machine_label('fmpXating-3', families) is False


# ---------------------------------------------------------------------------
# F-I3 -- shorts: ONE signed representation, whichever broker reported them
# ---------------------------------------------------------------------------

def test_positions_by_symbol_signs_a_tastytrade_short_negative():
    """TastyTrade stamps ``qty=abs_qty`` and records the direction in ``side``
    (``TastyTradeAccount.py:520-547``). Read raw, a short reads as a long."""
    out = positions_by_symbol([{'symbol': 'TSLA', 'qty': 10, 'cost_basis': 1500.0,
                                'market_value': 1800.0, 'side': 'SELL'}])
    assert out['TSLA'].quantity == -10.0
    assert out['TSLA'].cost_basis == -1500.0
    assert out['TSLA'].market_value == -1800.0


def test_positions_by_symbol_does_not_flip_an_already_signed_alpaca_short():
    """Alpaca passes the broker's own negative signs straight through
    (``alpaca_position_to_position``), so normalising must be idempotent."""
    out = positions_by_symbol([{'symbol': 'TSLA', 'qty': -10, 'cost_basis': -1500.0,
                                'market_value': -1800.0, 'side': 'SELL'}])
    assert out['TSLA'].quantity == -10.0
    assert out['TSLA'].cost_basis == -1500.0
    assert out['TSLA'].market_value == -1800.0


def test_the_same_short_from_either_broker_renders_the_same_page():
    alpaca = positions_by_symbol([{'symbol': 'TSLA', 'qty': -10, 'cost_basis': -1500.0,
                                   'market_value': -1800.0, 'side': 'SELL'}])
    tastytrade = positions_by_symbol([{'symbol': 'TSLA', 'qty': 10, 'cost_basis': 1500.0,
                                       'market_value': 1800.0, 'side': 'SELL'}])
    assert alpaca['TSLA'] == tastytrade['TSLA']


def test_positions_by_symbol_reads_the_side_enum_as_well_as_the_string():
    from ba2_trade_platform.core.types import OrderDirection
    out = positions_by_symbol([{'symbol': 'TSLA', 'qty': 10, 'cost_basis': 1500.0,
                                'market_value': 1800.0, 'side': OrderDirection.SELL}])
    assert out['TSLA'].quantity == -10.0


def test_positions_by_symbol_leaves_a_long_exactly_as_the_broker_reported_it():
    out = positions_by_symbol([{'symbol': 'AAPL', 'qty': 10, 'cost_basis': 1500.0,
                                'market_value': 1800.0, 'side': 'BUY'}])
    assert out['AAPL'].quantity == 10.0
    assert out['AAPL'].cost_basis == 1500.0


def test_positions_by_symbol_will_not_re_sign_a_long_from_its_side_field():
    """The sign rule is one-way: a SHORT forces its numbers negative, a LONG
    forces nothing.

    Pinned with a CONTRADICTORY row -- ``side`` says long, the numbers say short --
    because on an ordinary long "leave it alone" and "force it positive" agree, so
    nothing else here can tell the two rules apart. Mutation-checked: making the
    side authoritative in BOTH directions passed the whole allocation suite.
    """
    out = positions_by_symbol([{'symbol': 'AAPL', 'qty': -10, 'cost_basis': -1500.0,
                                'market_value': -1800.0, 'side': 'BUY'}])
    assert out['AAPL'].quantity == -10.0
    assert out['AAPL'].cost_basis == -1500.0
    assert out['AAPL'].market_value == -1800.0


def test_positions_by_symbol_without_a_side_trusts_the_signs_it_was_given():
    """Not every source stamps a side; an unknown side must not silently rewrite
    the numbers."""
    out = positions_by_symbol([{'symbol': 'TSLA', 'qty': -10, 'cost_basis': -1500.0,
                                'market_value': -1800.0}])
    assert out['TSLA'].quantity == -10.0


def test_a_long_and_a_short_of_one_symbol_net_out():
    out = positions_by_symbol([
        {'symbol': 'TSLA', 'qty': 10, 'cost_basis': 1000.0, 'market_value': 1200.0,
         'side': 'BUY'},
        {'symbol': 'TSLA', 'qty': 4, 'cost_basis': 400.0, 'market_value': 480.0,
         'side': 'SELL'},
    ])
    assert out['TSLA'].quantity == 6.0
    assert out['TSLA'].cost_basis == 600.0
    assert out['TSLA'].market_value == 720.0


def test_a_short_only_label_reports_percentages_rather_than_zeroes():
    """``if label_value > 0`` zeroes EVERY percentage of a net-short label, so the
    page showed a real position as 0% of a label worth -3000."""
    views = build_label_views([ManagedLabel('Hedges', 100.0)],
                              {'Hedges': ['TSLA']},
                              {'TSLA': positions_by_symbol(
                                  [{'symbol': 'TSLA', 'qty': 10, 'cost_basis': 3000.0,
                                    'market_value': 3300.0, 'side': 'SELL'}])['TSLA']},
                              {}, valuation_mode=VALUATION_MODE_COST)
    assert views[0].current_value == -3000.0
    assert views[0].rows[0].pct_of_label == 100.0
    assert views[0].rows[0].pct_of_total == 100.0
    assert views[0].pct_of_total == 100.0


def test_a_short_reduces_the_labels_value_instead_of_inflating_it():
    """Mutation P11 (``+= abs(...)``) survived because no test modelled a short."""
    positions = positions_by_symbol([
        {'symbol': 'AAPL', 'qty': 10, 'cost_basis': 5000.0, 'market_value': 5000.0,
         'side': 'BUY'},
        {'symbol': 'TSLA', 'qty': 10, 'cost_basis': 2000.0, 'market_value': 2000.0,
         'side': 'SELL'},
    ])
    views = build_label_views([ManagedLabel('Mixed', 100.0)],
                              {'Mixed': ['AAPL', 'TSLA']}, positions, {},
                              valuation_mode=VALUATION_MODE_COST)
    assert views[0].current_value == 3000.0
    assert views[0].cost_basis == 3000.0


def test_a_label_whose_value_nets_to_exactly_zero_reports_zero_percentages():
    """A zero denominator is the one case with no meaningful answer."""
    positions = positions_by_symbol([
        {'symbol': 'AAPL', 'qty': 10, 'cost_basis': 2000.0, 'market_value': 2000.0,
         'side': 'BUY'},
        {'symbol': 'TSLA', 'qty': 10, 'cost_basis': 2000.0, 'market_value': 2000.0,
         'side': 'SELL'},
    ])
    views = build_label_views([ManagedLabel('Flat', 100.0)],
                              {'Flat': ['AAPL', 'TSLA']}, positions, {},
                              valuation_mode=VALUATION_MODE_COST)
    assert views[0].current_value == 0.0
    assert all(r.pct_of_label == 0.0 for r in views[0].rows)


def test_a_short_in_market_mode_is_valued_at_a_negative_quantity_times_price():
    positions = positions_by_symbol([{'symbol': 'TSLA', 'qty': 10, 'cost_basis': 3000.0,
                                      'market_value': 3300.0, 'side': 'SELL'}])
    views = build_label_views([ManagedLabel('Hedges', 100.0)], {'Hedges': ['TSLA']},
                              positions, {'TSLA': 330.0},
                              valuation_mode=VALUATION_MODE_MARKET)
    assert views[0].rows[0].current_value == -3300.0
    assert views[0].rows[0].market_value == -3300.0


# ---------------------------------------------------------------------------
# Previously unpinned: target_pct, the blank-symbol row, the missing-quote list
# ---------------------------------------------------------------------------

def test_label_view_carries_the_stored_target_pct_through_untouched():
    """``LabelView.target_pct`` is what Section G's engine reads as
    ``LabelTarget.target_pct``; mutation B24 (-> 0.0) survived the whole suite."""
    views = build_label_views([ManagedLabel('ARK26', 37.5), ManagedLabel('EMPTY', 62.5)],
                              {'ARK26': ['AAPL'], 'EMPTY': []},
                              {'AAPL': _pos('AAPL', 1, 100.0)}, {},
                              valuation_mode=VALUATION_MODE_COST)
    assert views[0].target_pct == 37.5
    assert views[1].target_pct == 62.5          # an empty label keeps its target too


def test_positions_by_symbol_refuses_a_row_with_no_symbol():
    """It already refuses a missing quantity and a missing cost basis; dropping a
    nameless row silently loses money from every total on the page."""
    with pytest.raises(ValueError):
        positions_by_symbol([{'symbol': '', 'qty': 1, 'cost_basis': 100.0}])


def test_missing_quote_symbols_names_the_positions_a_quote_outage_zeroed():
    """In market mode an unpriced position contributes 0, which is indistinguishable
    from 'flat' on screen. The page has to be able to say which ones."""
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'MSFT', 'TSLA']},
                              {'AAPL': _pos('AAPL', 10, 1000.0),
                               'MSFT': _pos('MSFT', 10, 1000.0)},
                              {'AAPL': 250.0, 'MSFT': None},
                              valuation_mode=VALUATION_MODE_MARKET)
    # MSFT is held but unpriced; TSLA is unpriced AND flat, so it is not a loss.
    assert missing_quote_symbols(views) == ['MSFT']


def test_missing_quote_symbols_is_empty_when_every_position_is_priced():
    views = build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)}, {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_MARKET)
    assert missing_quote_symbols(views) == []


def test_missing_quote_symbols_reports_each_symbol_once_across_labels():
    views = build_label_views([ManagedLabel('ARK26', 50.0), ManagedLabel('TECH', 50.0)],
                              {'ARK26': ['MSFT'], 'TECH': ['MSFT']},
                              {'MSFT': _pos('MSFT', 10, 1000.0)}, {'MSFT': None},
                              valuation_mode=VALUATION_MODE_MARKET)
    assert missing_quote_symbols(views) == ['MSFT']


# ---------------------------------------------------------------------------
# The import-purity gate this module exists to satisfy
# ---------------------------------------------------------------------------

def test_the_view_module_imports_without_nicegui_the_db_or_the_expert_stack():
    """This module was split out of the page precisely so it stays cheap.

    ``ui/pages/__init__.py`` pulls every page and through them langchain/openai/
    torch — ~6s and a dozen heavy roots. A stray ``from nicegui import ui`` or a
    DB/broker import here would drag that back and make the pure suite slow and
    order-dependent. Checked in a SUBPROCESS: by the time this test runs in a full
    suite, another module has long since imported nicegui.
    """
    import os
    import subprocess
    import sys
    import time

    script = (
        "import sys, time\n"
        "t = time.time()\n"
        "import ba2_trade_platform.ui.utils.portfolio_allocation_view\n"
        "elapsed = time.time() - t\n"
        "banned = [m for m in ('nicegui', 'langchain_core', 'openai', 'torch',\n"
        "                      'transformers', 'sqlalchemy')\n"
        "          if m in sys.modules]\n"
        "print(f'{elapsed:.3f}|{banned}')\n"
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    proc = subprocess.run([sys.executable, '-c', script], capture_output=True,
                          text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    elapsed, banned = proc.stdout.strip().splitlines()[-1].split('|')
    assert banned == '[]', f"heavy imports leaked into the pure module: {banned}"
    assert float(elapsed) < 2.0, f"import took {elapsed}s — something heavy crept in"


# ---------------------------------------------------------------------------
# Market-hours gate (Submit only; the page and the dry run always render)
# ---------------------------------------------------------------------------
from datetime import datetime, timedelta, timezone

from ba2_common.core.account_types import (
    MARKET_HOURS_SOURCE_BROKER,
    MARKET_HOURS_SOURCE_FALLBACK,
    MARKET_HOURS_SOURCE_UNAVAILABLE,
)
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    MARKET_BANNER_CLASSES,
    MARKET_GATE_CLOSED,
    MARKET_GATE_OPEN,
    MARKET_GATE_UNKNOWN,
    MARKET_SOURCE_BROKER,
    MARKET_SOURCE_FALLBACK,
    MARKET_SOURCE_UNAVAILABLE,
    evaluate_market_gate,
    format_countdown,
    format_market_time,
    working_orders_notice,
)

# Frozen explicitly, always: 2026-08-20 22:00 UTC == Thu 18:00 ET. The next regular
# open is Fri 21 Aug 2026 09:30 ET == 13:30 UTC, 15h30m later.
FROZEN_NOW = datetime(2026, 8, 20, 22, 0, tzinfo=timezone.utc)
NEXT_OPEN = datetime(2026, 8, 21, 13, 30, tzinfo=timezone.utc)


def test_the_source_constants_agree_with_the_interfaces_own():
    """Re-spelling "broker" locally is how the banner ends up describing a broker
    answer as a fallback one.

    Deliberately EQUALITY, not ``is``: CPython interns short identifier-like string
    literals, so ``"broker" is MARKET_HOURS_SOURCE_BROKER`` is already True and an
    identity assertion here can never fail. The behavioural half is the test below,
    which feeds a real ``MarketHours.source`` straight into the gate.
    """
    assert MARKET_SOURCE_BROKER == MARKET_HOURS_SOURCE_BROKER
    assert MARKET_SOURCE_FALLBACK == MARKET_HOURS_SOURCE_FALLBACK
    assert MARKET_SOURCE_UNAVAILABLE == MARKET_HOURS_SOURCE_UNAVAILABLE
    assert len({MARKET_SOURCE_BROKER, MARKET_SOURCE_FALLBACK,
                MARKET_SOURCE_UNAVAILABLE}) == 3


def test_a_real_market_hours_source_is_understood_by_the_gate_as_published():
    """The end the drift actually shows up at: the gate is fed
    ``MarketHours.source`` verbatim, so a locally re-spelled constant that differs
    from the interface's by so much as a capital letter reports every broker answer
    as a fallback one."""
    from ba2_common.core.account_types import MarketHours

    broker = MarketHours(is_open=False, as_of=FROZEN_NOW, next_open=NEXT_OPEN,
                         source=MARKET_HOURS_SOURCE_BROKER)
    fallback = MarketHours(is_open=False, as_of=FROZEN_NOW, next_open=NEXT_OPEN,
                           source=MARKET_HOURS_SOURCE_FALLBACK)
    assert evaluate_market_gate(is_open=broker.is_open, next_open=broker.next_open,
                                source=broker.source, now=FROZEN_NOW
                                ).from_fallback is False
    assert evaluate_market_gate(is_open=fallback.is_open, next_open=fallback.next_open,
                                source=fallback.source, now=FROZEN_NOW
                                ).from_fallback is True


def test_market_gate_open_allows_submit_and_says_nothing():
    gate = evaluate_market_gate(is_open=True, next_open=None,
                                source=MARKET_SOURCE_BROKER, now=FROZEN_NOW)
    assert gate.allowed is True
    assert gate.reason_code == MARKET_GATE_OPEN
    assert gate.message == ""


def test_market_gate_closed_blocks_and_names_the_next_open_in_eastern_time():
    gate = evaluate_market_gate(is_open=False, next_open=NEXT_OPEN,
                                source=MARKET_SOURCE_BROKER, now=FROZEN_NOW)
    assert gate.allowed is False
    assert gate.reason_code == MARKET_GATE_CLOSED
    assert gate.next_open_text == "Fri 21 Aug 2026 09:30 ET"
    assert "Fri 21 Aug 2026 09:30 ET" in gate.message
    assert "15h 30m" in gate.message
    assert "still refresh and review this dry run" in gate.message
    assert gate.severity == "warning"


def test_market_gate_closed_from_the_fallback_calendar_says_where_the_time_came_from():
    gate = evaluate_market_gate(is_open=False, next_open=NEXT_OPEN,
                                source=MARKET_SOURCE_FALLBACK, now=FROZEN_NOW)
    assert gate.from_fallback is True
    assert "built-in NYSE calendar" in gate.message
    assert "regular session" in gate.message


def test_market_gate_closed_from_the_broker_does_not_mention_the_calendar():
    gate = evaluate_market_gate(is_open=False, next_open=NEXT_OPEN,
                                source=MARKET_SOURCE_BROKER, now=FROZEN_NOW)
    assert gate.from_fallback is False
    assert "NYSE calendar" not in gate.message


def test_market_gate_closed_with_no_next_open_still_blocks_and_admits_it():
    gate = evaluate_market_gate(is_open=False, next_open=None,
                                source=MARKET_SOURCE_BROKER, now=FROZEN_NOW)
    assert gate.allowed is False
    assert gate.reason_code == MARKET_GATE_CLOSED
    assert gate.next_open_text == ""
    assert "No next-open time was published" in gate.message


def test_market_gate_unknown_blocks_rather_than_assuming_open():
    """An unanswered market-hours call is not permission to send orders -- and it is
    reported as UNKNOWN, not CLOSED: the two have different fixes."""
    gate = evaluate_market_gate(is_open=None, next_open=None,
                                source=MARKET_SOURCE_UNAVAILABLE, now=FROZEN_NOW)
    assert gate.allowed is False
    assert gate.reason_code == MARKET_GATE_UNKNOWN
    assert gate.severity == "negative"
    assert "could not be confirmed open" in gate.message


def test_market_gate_next_open_already_in_the_past_drops_the_countdown():
    """A stale next_open must not render "in -2h 0m"."""
    gate = evaluate_market_gate(is_open=False,
                                next_open=FROZEN_NOW - timedelta(hours=2),
                                source=MARKET_SOURCE_BROKER, now=FROZEN_NOW)
    assert gate.countdown_text == ""
    assert "from now" not in gate.message
    assert gate.next_open_text


def test_market_gate_rejects_a_naive_next_open_rather_than_guessing_its_zone():
    """Unreachable through the seam -- MarketHours.__post_init__ raises on a naive
    field -- and kept anyway, because a caller can hand-build these scalars."""
    with pytest.raises(ValueError):
        evaluate_market_gate(is_open=False, next_open=datetime(2026, 8, 21, 13, 30),
                             source=MARKET_SOURCE_BROKER, now=FROZEN_NOW)


def test_market_gate_rejects_a_naive_now():
    with pytest.raises(ValueError):
        evaluate_market_gate(is_open=True, next_open=None,
                             source=MARKET_SOURCE_BROKER,
                             now=datetime(2026, 8, 20, 22, 0))


def test_format_market_time_is_locale_proof_and_in_eastern_time():
    assert format_market_time(datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)) == \
        "Mon 05 Jan 2026 09:30 ET"


def test_format_countdown_uses_days_hours_then_minutes():
    assert format_countdown(timedelta(days=2, hours=3, minutes=40)) == "2d 3h"
    assert format_countdown(timedelta(hours=15, minutes=30)) == "15h 30m"
    assert format_countdown(timedelta(minutes=42)) == "42m"
    assert format_countdown(timedelta(seconds=-1)) == ""


def test_market_banner_classes_map_each_severity_to_a_real_stylesheet_class():
    """styles.css defines warning / danger / info / success only."""
    assert MARKET_BANNER_CLASSES["warning"] == "alert-banner warning"
    assert MARKET_BANNER_CLASSES["negative"] == "alert-banner danger"
    assert MARKET_BANNER_CLASSES["info"] == "alert-banner info"


def test_working_orders_notice_is_none_when_the_run_settled():
    assert working_orders_notice(settled=True, working_order_ids=[]) is None


def test_working_orders_notice_names_the_count_when_orders_are_still_working():
    text, severity = working_orders_notice(settled=False, working_order_ids=[7, 9])
    assert "2 order(s) still working" in text
    assert "income" in text.lower()
    assert severity == "warning"


def test_working_orders_notice_is_honest_when_it_cannot_count_the_orders():
    """settled=False with no ids is still unconsumed income; saying nothing would
    hide it, and inventing a count would be worse."""
    text, severity = working_orders_notice(settled=False, working_order_ids=[])
    assert "still working" in text
    assert severity == "warning"


def test_an_unknown_gate_never_claims_the_broker_answered():
    """``from_fallback`` means the same thing in all three branches. An unanswered
    lookup certainly did not come from the broker, and saying otherwise would let
    the banner cite a provenance nothing supplied."""
    unknown = evaluate_market_gate(is_open=None, next_open=None,
                                   source=MARKET_SOURCE_UNAVAILABLE, now=FROZEN_NOW)
    assert unknown.from_fallback is True
    # And a broker that DID answer, with "open", is not a fallback.
    assert evaluate_market_gate(is_open=True, next_open=None,
                                source=MARKET_SOURCE_BROKER,
                                now=FROZEN_NOW).from_fallback is False


# ---------------------------------------------------------------------------
# I5: an ALLOW built on the offline calendar has to say so.
#
# ``from_fallback`` was set on all three branches and read by nobody. The CLOSED
# branch at least folds MARKET_NOTE_FALLBACK into its own message; the ALLOW
# branch had nowhere to put it, so a broker whose get_clock() failed could have
# the built-in NYSE calendar wave a submission through -- and the calendar knows
# nothing about an unscheduled halt, a broker-side trading suspension or the
# account's own restrictions.
# ---------------------------------------------------------------------------

def test_no_provenance_notice_when_the_broker_itself_said_the_market_is_open():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        market_provenance_notice,
    )
    gate = evaluate_market_gate(is_open=True, next_open=None,
                                source=MARKET_SOURCE_BROKER, now=FROZEN_NOW)
    assert market_provenance_notice(gate) is None


def test_an_allow_from_the_offline_calendar_warns_that_the_broker_never_answered():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        market_provenance_notice,
    )
    gate = evaluate_market_gate(is_open=True, next_open=None,
                                source=MARKET_SOURCE_FALLBACK, now=FROZEN_NOW)
    assert gate.allowed is True

    text, severity = market_provenance_notice(gate)
    assert "did not answer" in text
    assert "halt" in text.lower()
    assert severity == "warning"
    assert severity in MARKET_BANNER_CLASSES


def test_a_blocked_gate_gets_no_second_provenance_notice():
    """The CLOSED and UNKNOWN branches already carry their own provenance wording
    inside ``message``; a second banner saying it again is noise, and the one the
    user must not miss is the one that let a submission THROUGH."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        MARKET_NOTE_FALLBACK, market_provenance_notice,
    )
    closed = evaluate_market_gate(is_open=False, next_open=NEXT_OPEN,
                                  source=MARKET_SOURCE_FALLBACK, now=FROZEN_NOW)
    assert MARKET_NOTE_FALLBACK in closed.message
    assert market_provenance_notice(closed) is None

    unknown = evaluate_market_gate(is_open=None, next_open=None,
                                   source=MARKET_SOURCE_UNAVAILABLE, now=FROZEN_NOW)
    assert market_provenance_notice(unknown) is None


def test_a_failed_broker_refresh_is_not_reported_as_zero_orders_working():
    """MINOR: ``measure_run_fills`` forces ``settled=False`` on a failed refresh
    while ``working_order_ids`` stays EMPTY, so the count line read "0 order(s)
    still working" -- i.e. "nothing outstanding" -- for the one case where orders
    may have reached the broker and nobody can say."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        REFRESH_FAILED_NOTICE,
    )
    text, severity = working_orders_notice(settled=False, working_order_ids=[],
                                           refresh_failed=True)
    assert text == REFRESH_FAILED_NOTICE
    assert "0 order(s)" not in text
    assert "FAILED" in text and "check the broker" in text.lower()
    assert severity == "negative"


def test_the_refresh_failure_outranks_a_settled_looking_run():
    """A failed refresh with rows that LOOK final is exactly the dangerous shape:
    settled would be True on our own numbers, and they are not evidence."""
    assert working_orders_notice(settled=True, working_order_ids=[],
                                 refresh_failed=True) is not None


def test_a_working_refresh_keeps_the_old_two_sentences():
    assert working_orders_notice(settled=True, working_order_ids=[],
                                 refresh_failed=False) is None
    text, severity = working_orders_notice(settled=False, working_order_ids=[7],
                                           refresh_failed=False)
    assert "1 order(s) still working" in text
    assert severity == "warning"


# ---------------------------------------------------------------------------
# W3: the page learns about buying power -- the reserve row, and ONE denominator.
# ---------------------------------------------------------------------------

def test_a_label_view_reports_its_share_of_the_base_when_it_is_given_one():
    """``pct_of_total`` divides by the MANAGED value; ``target_pct`` is a share of
    ``base_notional`` (buying power PLUS managed value). Whenever buying power is
    non-zero those two are not comparable, and the page header printed them side by
    side. ``pct_of_base`` is the one that IS comparable with the target."""
    views = build_label_views([ManagedLabel('ARK26', 40.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)},
                              {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0)
    assert views[0].current_value == 2_500.0
    assert views[0].pct_of_total == 100.0        # 100% of the managed value
    assert views[0].pct_of_base == 25.0          # 25% of the allocatable base
    assert views[0].target_value == 4_000.0      # 40% of 10,000


def test_a_label_view_without_a_base_reports_none_rather_than_zero():
    """No fallback for a number the caller did not supply: 0.00% of base would be a
    fact, and a wrong one."""
    views = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)}, {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_MARKET)
    assert views[0].pct_of_base is None
    assert views[0].target_value is None


def test_a_zero_base_reports_none_rather_than_dividing_by_it():
    views = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                              {}, {}, valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=0.0)
    assert views[0].pct_of_base is None
    assert views[0].target_value is None


def test_a_symbol_row_carries_its_target_weight_and_the_money_that_implies():
    """Requirement 2's "target" half at the instrument level: the page showed a
    target once, on the group header, and never per symbol."""
    views = build_label_views([ManagedLabel('ARK26', 40.0)],
                              {'ARK26': ['AAPL', 'MSFT']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)},
                              {'AAPL': 250.0, 'MSFT': 100.0},
                              valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0,
                              symbol_weights={'ARK26': {'AAPL': 75.0, 'MSFT': 25.0}})
    by_symbol = {r.symbol: r for r in views[0].rows}
    assert by_symbol['AAPL'].weight_pct == 75.0
    # 40% of 10,000 is 4,000 for the label; 75% of that is 3,000.
    assert by_symbol['AAPL'].target_value == 3_000.0
    assert by_symbol['MSFT'].target_value == 1_000.0


def test_a_symbol_row_without_a_stored_weight_falls_back_to_its_ACTUAL_share():
    """It used to report ``None`` here, and before that the fair share. The lone
    member of a label holds all of it, so its actual share is 100%."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHT_SOURCE_ACTUAL)

    views = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)}, {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0)
    row = views[0].rows[0]
    assert row.weight_pct == 100.0
    assert row.weight_source == WEIGHT_SOURCE_ACTUAL
    # 40% of 10,000 is 4,000 for the label, and this row is the whole of it.
    assert row.target_value == 4_000.0


def test_the_unallocated_row_shows_the_STORED_reserve_in_percent_and_dollars():
    """EDITABLE and STORED now, not derived from a label shortfall: the row reports
    ``unallocated_pct`` against the SAME base the targets divide. Its "current" is
    the account's free buying power, which is what is actually uninvested today."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import unallocated_row

    views = build_label_views([ManagedLabel('ARK26', 70.0), ManagedLabel('TECH', 30.0)],
                              {'ARK26': ['AAPL'], 'TECH': ['MSFT']}, {}, {},
                              valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0, unallocated_pct=30.0)

    row = unallocated_row(base_notional=10_000.0, available_buying_power=2_500.0,
                          unallocated_pct=30.0)

    assert row.target_pct == 30.0
    assert row.target_value == 3_000.0
    assert row.current_value == 2_500.0
    assert row.pct_of_base == 25.0


def test_the_unallocated_row_does_NOT_read_the_label_totals_any_more():
    """The reversal, pinned. Labels totalling 100 with a 10% reserve is the normal
    case: a row that derived itself from the shortfall would report 0."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import unallocated_row

    views = build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']}, {},
                              {}, valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0, unallocated_pct=10.0)

    row = unallocated_row(base_notional=10_000.0, available_buying_power=0.0,
                          unallocated_pct=10.0)

    assert (row.target_pct, row.target_value) == (10.0, 1_000.0)


def test_the_unallocated_row_of_an_account_reserving_nothing_is_zero_not_absent():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import unallocated_row

    views = build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']}, {},
                              {}, valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0)

    row = unallocated_row(base_notional=10_000.0, available_buying_power=100.0,
                          unallocated_pct=0.0)

    assert (row.target_pct, row.target_value) == (0.0, 0.0)
    assert row.current_value == 100.0


def test_the_unallocated_row_tolerates_a_zero_base():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import unallocated_row

    row = unallocated_row(base_notional=0.0, available_buying_power=0.0,
                          unallocated_pct=100.0)

    assert row.target_pct == 100.0
    assert row.target_value == 0.0
    assert row.pct_of_base is None


def test_the_unallocated_row_never_reports_a_reserve_outside_zero_to_one_hundred():
    """The row's own clamp, restored after W8 dropped the test that held it.

    The wizard blocks an out-of-range reserve before a plan is ever solved, but this
    row is also drawn on the PAGE, straight from ``portfolio_allocation_config``,
    where a value written by an older build or by hand can be anything. A -20 that
    reached the screen would print "target -20.00% held back" over a row whose money
    says 0.00 -- two numbers on one line disagreeing about the same fact.
    """
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import unallocated_row

    low = unallocated_row(base_notional=10_000.0, available_buying_power=0.0,
                          unallocated_pct=-20.0)
    high = unallocated_row(base_notional=10_000.0, available_buying_power=0.0,
                           unallocated_pct=140.0)

    assert (low.target_pct, low.target_value) == (0.0, 0.0)
    assert (high.target_pct, high.target_value) == (100.0, 10_000.0)


def test_the_unallocated_row_has_no_overshoot_field_any_more():
    """``over_pct`` reported label targets summing past 100 -- a LABEL error, which
    the validator now names directly. Reporting it on the reserve row conflated two
    independent numbers."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import UnallocatedRow

    assert not hasattr(UnallocatedRow(), "over_pct")


def test_a_label_target_value_is_a_share_of_what_the_reserve_LEFT():
    """THE REQUIREMENT, at the view layer. Base 10,000, reserve 10%, labels 50/30/20
    -> 4,500 / 2,700 / 1,800 -- and the percentages on screen stay 50/30/20."""
    views = build_label_views(
        [ManagedLabel('A', 50.0), ManagedLabel('B', 30.0), ManagedLabel('C', 20.0)],
        {'A': ['AAA'], 'B': ['BBB'], 'C': ['CCC']}, {}, {},
        valuation_mode=VALUATION_MODE_MARKET,
        base_notional=10_000.0, unallocated_pct=10.0)

    assert [v.target_pct for v in views] == [50.0, 30.0, 20.0]
    assert [v.target_value for v in views] == [4_500.0, 2_700.0, 1_800.0]
    assert sum(v.target_value for v in views) == 9_000.0


def test_pct_of_base_stays_on_the_GROSS_base_so_the_reserve_row_compares():
    """Two denominators is the defect this page already had once. ``pct_of_base``
    and the reserve row's ``pct_of_base`` must divide the same number, or a fully
    invested book reads as 111% of a 90% base."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import unallocated_row

    views = build_label_views([ManagedLabel('A', 100.0)], {'A': ['AAA']},
                              {'AAA': _pos('AAA', 900, 10.0)}, {'AAA': 10.0},
                              valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0, unallocated_pct=10.0)
    row = unallocated_row(base_notional=10_000.0, available_buying_power=1_000.0,
                          unallocated_pct=10.0)

    assert views[0].pct_of_base == 90.0          # 9,000 of a 10,000 GROSS base
    assert row.pct_of_base == 10.0               # 1,000 of the SAME 10,000
    assert views[0].pct_of_base + row.pct_of_base == 100.0


def test_a_symbol_target_value_is_scaled_by_the_reserve_too():
    """The symbol column divides its label's target, which the reserve already
    scaled -- so it must NOT apply the factor a second time."""
    views = build_label_views([ManagedLabel('A', 100.0)], {'A': ['AAA', 'BBB']}, {}, {},
                              valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0, unallocated_pct=10.0,
                              symbol_weights={'A': {'AAA': 75.0, 'BBB': 25.0}})

    by_symbol = {r.symbol: r.target_value for r in views[0].rows}
    assert by_symbol == {'AAA': 6_750.0, 'BBB': 2_250.0}


def test_no_reserve_leaves_every_target_value_exactly_where_it_was():
    """The default path must be untouched: 0 is an identity, not an approximation."""
    views = build_label_views([ManagedLabel('A', 40.0)], {'A': ['AAA']}, {}, {},
                              valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0)
    assert views[0].target_value == 4_000.0


# ---------------------------------------------------------------------------
# INLINE TARGET EDITING -- the page itself is now where targets are set, so the
# validation that used to live behind the Allocate wizard has to exist here as a
# pure, testable decision. Every target box on the page goes through these.
# ---------------------------------------------------------------------------

def test_parse_pct_reads_a_plain_number():
    edit = parse_pct(42.5)
    assert edit.accepted is True
    assert edit.value == 42.5
    assert edit.reason_code == EDIT_OK


def test_parse_pct_reads_a_typed_string_with_its_percent_sign():
    """``ui.number`` normally yields a float, but a Quasar input can hand back the
    raw string it is holding -- and the box carries ``suffix='%'``."""
    assert parse_pct(' 42.5 % ').value == 42.5


def test_parse_pct_refuses_a_blank_box_rather_than_calling_it_zero():
    """Clearing a ``ui.number`` yields None. Persisting that as 0.0 would tell the
    engine to hold NONE of this label -- a sell order from a cleared box."""
    edit = parse_pct(None)
    assert edit.accepted is False
    assert edit.reason_code == EDIT_BLANK
    assert edit.value is None


def test_parse_pct_refuses_an_empty_string_for_the_same_reason():
    assert parse_pct('').reason_code == EDIT_BLANK
    assert parse_pct('   ').reason_code == EDIT_BLANK


def test_parse_pct_refuses_text():
    edit = parse_pct('abc')
    assert edit.accepted is False
    assert edit.reason_code == EDIT_NOT_A_NUMBER


def test_parse_pct_itself_carries_no_message_because_it_knows_no_box():
    """The wording names the box ('label ARK26', 'ARK26 / AAPL', 'the unallocated
    reserve'), so it belongs to the three validators, not to the number reader."""
    assert parse_pct('abc').message == ''
    assert parse_pct(None).message == ''


def test_the_named_validators_quote_the_text_that_was_refused():
    """'That is not a number' without saying WHICH is a message the user cannot act
    on -- especially on a page holding one box per label plus one per symbol."""
    assert 'abc' in validate_symbol_weight_edit(label='ARK26', symbol='AAPL',
                                                raw='abc').message
    assert 'AAPL' in validate_symbol_weight_edit(label='ARK26', symbol='AAPL',
                                                 raw='abc').message
    assert 'abc' in validate_label_target_edit(label='ARK26', raw='abc',
                                               other_targets={}).message
    assert 'ARK26' in validate_label_target_edit(label='ARK26', raw='abc',
                                                 other_targets={}).message


def test_parse_pct_refuses_a_bool_which_python_would_otherwise_read_as_a_number():
    """``float(True)`` is 1.0 and ``isinstance(True, int)`` is True, so a checkbox
    wired to the wrong handler would silently store a 1% target."""
    assert parse_pct(True).reason_code == EDIT_NOT_A_NUMBER
    assert parse_pct(False).reason_code == EDIT_NOT_A_NUMBER


def test_parse_pct_refuses_nan_and_infinity():
    """``float('nan') < 0`` and ``float('nan') > 100`` are BOTH False, so a bare
    range check waves NaN straight through and every derived figure becomes NaN."""
    assert parse_pct(float('nan')).reason_code == EDIT_NOT_A_NUMBER
    assert parse_pct(float('inf')).reason_code == EDIT_NOT_A_NUMBER
    assert parse_pct(float('-inf')).reason_code == EDIT_NOT_A_NUMBER
    assert parse_pct('nan').reason_code == EDIT_NOT_A_NUMBER


def test_parse_pct_does_not_range_check_it_only_reads_the_number():
    """The range belongs to the three validators, which disagree about it: the
    reserve uses the ENGINE's message, a label target also has to fit under 100
    alongside its siblings, and a symbol weight is only bounded 0-100."""
    assert parse_pct(140.0).accepted is True
    assert parse_pct(-5.0).accepted is True


# -- one symbol's weight within its label ------------------------------------

def test_a_symbol_weight_edit_accepts_a_number_in_range():
    edit = validate_symbol_weight_edit(label='ARK26', symbol='AAPL', raw=62.5)
    assert edit.accepted is True
    assert edit.value == 62.5


def test_a_symbol_weight_edit_refuses_a_negative_weight():
    edit = validate_symbol_weight_edit(label='ARK26', symbol='AAPL', raw=-1.0)
    assert edit.accepted is False
    assert edit.reason_code == EDIT_NEGATIVE
    assert 'AAPL' in edit.message


def test_a_symbol_weight_edit_refuses_more_than_100():
    edit = validate_symbol_weight_edit(label='ARK26', symbol='AAPL', raw=101.0)
    assert edit.accepted is False
    assert edit.reason_code == EDIT_OVER_100
    assert 'AAPL' in edit.message


def test_a_symbol_weight_of_exactly_zero_or_exactly_100_is_legal():
    """0 is an explicit "hold none of this" and 100 is a single-symbol label."""
    assert validate_symbol_weight_edit(label='A', symbol='X', raw=0.0).accepted is True
    assert validate_symbol_weight_edit(label='A', symbol='X', raw=100.0).accepted is True


def test_a_symbol_weight_edit_refuses_a_blank_box():
    assert validate_symbol_weight_edit(label='A', symbol='X', raw=None).reason_code == EDIT_BLANK


# -- one label's own target, against its siblings ----------------------------

def test_a_label_target_edit_accepts_a_set_that_still_fits_under_100():
    edit = validate_label_target_edit(label='ARK26', raw=40.0,
                                      other_targets={'HighRisk': 50.0})
    assert edit.accepted is True
    assert edit.value == 40.0


def test_a_label_target_edit_accepts_a_set_that_lands_exactly_on_100():
    edit = validate_label_target_edit(label='ARK26', raw=50.0,
                                      other_targets={'HighRisk': 50.0})
    assert edit.accepted is True


def test_a_label_target_edit_refuses_a_set_that_would_pass_100():
    """THE guard the Allocate wizard has always had, now on the inline path too.
    Without it the page persists a set the engine will refuse, and the user only
    finds out at the dry run."""
    edit = validate_label_target_edit(label='ARK26', raw=60.0,
                                      other_targets={'HighRisk': 50.0})
    assert edit.accepted is False
    assert edit.reason_code == EDIT_LABELS_OVER_100
    assert 'ARK26' in edit.message
    assert '110.00' in edit.message              # the total
    assert '10.00' in edit.message               # the overshoot


def test_a_label_target_edit_uses_the_engines_own_over_100_sentence():
    """One rule, one wording: the inline refusal must quote the same sentence
    ``validate_label_targets`` produces, or the page and the dry run describe the
    same defect in two ways."""
    from ba2_trade_platform.core.portfolio_allocation import ERROR_LABEL_TOTAL_FMT

    edit = validate_label_target_edit(label='ARK26', raw=60.0,
                                      other_targets={'HighRisk': 50.0})
    assert ERROR_LABEL_TOTAL_FMT.format(total=110.0, over=10.0) in edit.message


def test_a_label_target_edit_shares_the_engines_tolerance():
    """``LABEL_TOTAL_TOLERANCE_PCT`` is 0.01pp and the two-decimal splits the page
    offers land a hair either side of 100. An inline guard with no tolerance would
    refuse a set the engine accepts."""
    from ba2_trade_platform.core.portfolio_allocation import LABEL_TOTAL_TOLERANCE_PCT

    just_inside = 100.0 + LABEL_TOTAL_TOLERANCE_PCT / 2.0
    assert validate_label_target_edit(label='A', raw=just_inside,
                                      other_targets={}).accepted is True
    assert validate_label_target_edit(label='A', raw=100.0 + 1.0,
                                      other_targets={}).accepted is False


def test_a_label_target_edit_ignores_the_labels_OWN_previous_value():
    """``other_targets`` is the OTHER labels. Counting the label being edited twice
    would make lowering an over-target label impossible."""
    edit = validate_label_target_edit(label='ARK26', raw=30.0,
                                      other_targets={'ARK26': 90.0, 'HighRisk': 50.0})
    assert edit.accepted is True


def test_a_label_target_edit_allows_a_set_that_is_UNDER_100():
    """Under 100 is an error at SUBMIT, not at edit time: the user has to be able
    to pass through 40/0 on the way to 40/60."""
    assert validate_label_target_edit(label='A', raw=40.0, other_targets={'B': 0.0}) \
        .accepted is True


def test_a_label_target_edit_refuses_a_negative_and_a_blank():
    assert validate_label_target_edit(label='A', raw=-1.0,
                                      other_targets={}).reason_code == EDIT_NEGATIVE
    assert validate_label_target_edit(label='A', raw=None,
                                      other_targets={}).reason_code == EDIT_BLANK


def test_a_label_target_edit_refuses_over_100_even_with_no_siblings():
    """The plain box message, not the "lower another label first" one: there IS no
    other label to lower, and telling the user to go and do that is a dead end."""
    edit = validate_label_target_edit(label='A', raw=140.0, other_targets={})
    assert edit.accepted is False
    assert edit.reason_code == EDIT_OVER_100
    assert 'Lower another label' not in edit.message


def test_a_symbol_weight_edit_shares_the_engines_tolerance_too():
    """``validate_symbol_weights`` measures the label total at 0.01pp, so a lone
    symbol at 100.005 is engine-legal and must not be refused on the way in."""
    from ba2_trade_platform.core.portfolio_allocation import LABEL_TOTAL_TOLERANCE_PCT

    just_inside = 100.0 + LABEL_TOTAL_TOLERANCE_PCT / 2.0
    assert validate_symbol_weight_edit(label='A', symbol='X',
                                       raw=just_inside).accepted is True
    assert validate_symbol_weight_edit(label='A', symbol='X', raw=101.0).accepted is False


# -- the cash reserve --------------------------------------------------------

def test_a_reserve_edit_accepts_both_ends_of_the_range():
    """100% is a legitimate setting: allocate nothing this cycle."""
    assert validate_reserve_edit(0.0).accepted is True
    assert validate_reserve_edit(100.0).accepted is True
    assert validate_reserve_edit(100.0).value == 100.0


def test_a_reserve_edit_quotes_the_engines_own_range_sentence():
    from ba2_trade_platform.core.portfolio_allocation import validate_unallocated_pct

    edit = validate_reserve_edit(140.0)
    assert edit.accepted is False
    assert edit.message == validate_unallocated_pct(140.0)[0]


def test_a_reserve_edit_refuses_a_negative_which_would_INFLATE_the_base():
    edit = validate_reserve_edit(-20.0)
    assert edit.accepted is False
    assert '-20' in edit.message


def test_a_reserve_edit_refuses_a_blank_and_a_nan():
    assert validate_reserve_edit(None).reason_code == EDIT_BLANK
    assert validate_reserve_edit(float('nan')).reason_code == EDIT_NOT_A_NUMBER


# ---------------------------------------------------------------------------
# LIVE DERIVED FIGURES
#
# Inline editing is only worth having if the CONSEQUENCE of an edit is visible
# immediately, so the label header line and the symbol table's TARGET VALUE column
# are built by pure functions with two callers each: the first render, and the
# in-place update after a box changes. One formatter, so a typed number and a
# reloaded page can never disagree.
# ---------------------------------------------------------------------------

def test_the_label_header_names_the_denominator_of_the_target_it_prints():
    """7a: two different quantities were both called "target" -- the label's share
    of the PORTFOLIO (0.0%) and a symbol's share of ITS LABEL (20). Both numbers
    were right; neither said its denominator, and the user asked why one said 0
    while the column beside it said 20. The header names its own out loud, because
    it is the expansion's caption and is read on its own."""
    text = format_label_header(label='ARK26', current_value=9_000.0, target_pct=50.0,
                               pct_of_investable=100.0, pct_of_total=100.0,
                               delta_text='over by 50.0pp ($4,500.00)',
                               unallocated_pct=10.0)
    assert text == ('ARK26 — $9,000.00 (100.0% of investable, target 50.0% '
                    '(real 45.0%) — over by 50.0pp ($4,500.00))')


def test_the_label_header_states_the_holding_and_the_target_on_ONE_scale():
    """Both divide the INVESTABLE pool now, so they are directly comparable and the
    delta between them is their plain difference. Printed on two denominators -- as
    they were -- the comparison the row invites is false at every non-zero reserve."""
    text = format_label_header(label='A', current_value=4_500.0, target_pct=50.0,
                               pct_of_investable=50.0, pct_of_total=100.0,
                               delta_text='on target', unallocated_pct=10.0)
    assert '50.0% of investable' in text
    assert 'target 50.0% (real 45.0%)' in text


def test_the_label_header_no_longer_repeats_the_reserve_clause_on_every_row():
    """It was identical on all eight rows and made the line ~100 characters. The
    information moved to the tooltip and to the page's one basis legend; it did not
    go away."""
    text = format_label_header(label='A', current_value=1.0, target_pct=50.0,
                               pct_of_investable=1.0, pct_of_total=1.0,
                               delta_text='under by 49.0pp ($4,410.00)',
                               unallocated_pct=10.0)
    assert 'what the reserve leaves' not in text
    assert 'splits that money' not in text


def test_the_label_header_falls_back_to_percent_of_managed_with_no_pool():
    """``% of managed`` is a THIRD denominator, so it is only ever printed where it
    is NAMED. That is here, and nowhere else."""
    text = format_label_header(label='ARK26', current_value=9_000.0, target_pct=50.0,
                               pct_of_investable=None, pct_of_total=75.0,
                               delta_text='—', unallocated_pct=0.0)
    assert text == ('ARK26 — $9,000.00 (75.0% of managed, target unavailable — '
                    'no investable base)')


def test_the_label_header_at_a_100_percent_reserve_reads_a_dash_not_nan():
    """100% is a legitimate setting -- allocate nothing. There is then no investable
    pool, so there is no share of one; the conversion that would blow up (a share of
    base back to a label weight needs /(1 - r/100)) is deliberately performed
    nowhere."""
    text = format_label_header(label='A', current_value=1.0, target_pct=100.0,
                               pct_of_investable=None, pct_of_total=100.0,
                               delta_text='—', unallocated_pct=100.0)
    assert 'no investable base' in text
    assert 'nan' not in text.lower()
    assert 'inf' not in text.lower()


# -- the tooltip that took the long clause -----------------------------------

def test_the_tooltip_keeps_the_money_the_header_stopped_printing():
    """The of-base restatement moved to the ROW ("(real 45.0%)"), so the tooltip no
    longer carries it -- one fact, one place. The MONEY is still only here."""
    tip = format_label_target_tooltip(target_pct=50.0, base_notional=10_000.0,
                                      unallocated_pct=10.0)
    assert '50.0% of what the reserve leaves' in tip
    assert '$4,500.00' in tip
    assert '45.0% of the base' not in tip


def test_the_tooltip_explains_the_two_things_called_target():
    """7a's actual fix: the header target is a share of the PORTFOLIO, the column
    below is a share of THE LABEL, and the tooltip is where that is said."""
    tip = format_label_target_tooltip(target_pct=50.0, base_notional=10_000.0,
                                      unallocated_pct=0.0)
    assert 'share of the label' in tip.lower()


def test_the_tooltip_says_there_is_no_dollar_figure_rather_than_showing_zero():
    tip = format_label_target_tooltip(target_pct=50.0, base_notional=None,
                                      unallocated_pct=0.0)
    assert '$' not in tip
    assert 'no base notional' in tip


def test_the_tooltip_money_follows_the_reserve():
    at_zero = format_label_target_tooltip(target_pct=100.0, base_notional=10_000.0,
                                          unallocated_pct=0.0)
    at_forty = format_label_target_tooltip(target_pct=100.0, base_notional=10_000.0,
                                           unallocated_pct=40.0)
    assert '$10,000.00' in at_zero
    assert '$6,000.00' in at_forty


# -- the symbol table's TARGET VALUE column ----------------------------------

def test_symbol_target_values_split_the_labels_money_by_weight():
    values = symbol_target_values({'AAA': 75.0, 'BBB': 25.0}, label_target_pct=40.0,
                                  base_notional=10_000.0, unallocated_pct=0.0)
    assert values == {'AAA': 3_000.0, 'BBB': 1_000.0}


def test_symbol_target_values_are_scaled_by_the_reserve_exactly_once():
    values = symbol_target_values({'AAA': 75.0, 'BBB': 25.0}, label_target_pct=100.0,
                                  base_notional=10_000.0, unallocated_pct=10.0)
    assert values == {'AAA': 6_750.0, 'BBB': 2_250.0}


def test_symbol_target_values_are_None_without_a_base_rather_than_zero():
    """A page with no base notional has no answer, and 0.00 there is a claim."""
    values = symbol_target_values({'AAA': 75.0}, label_target_pct=40.0,
                                  base_notional=None, unallocated_pct=0.0)
    assert values == {'AAA': None}


def test_symbol_target_values_treat_a_zero_base_as_absent_too():
    values = symbol_target_values({'AAA': 75.0}, label_target_pct=40.0,
                                  base_notional=0.0, unallocated_pct=0.0)
    assert values == {'AAA': None}


def test_symbol_target_values_agree_with_the_view_the_page_first_rendered():
    """The in-place update after a keystroke must land on the same numbers a full
    reload would -- otherwise editing a box drifts the table away from the DB."""
    view = build_label_views([ManagedLabel('A', 40.0)], {'A': ['AAA', 'BBB']}, {}, {},
                             valuation_mode=VALUATION_MODE_MARKET,
                             base_notional=10_000.0, unallocated_pct=10.0,
                             symbol_weights={'A': {'AAA': 62.5, 'BBB': 37.5}})[0]
    live = symbol_target_values({'AAA': 62.5, 'BBB': 37.5}, label_target_pct=40.0,
                                base_notional=10_000.0, unallocated_pct=10.0)
    assert {r.symbol: r.target_value for r in view.rows} == live


def test_symbol_target_values_at_a_100_percent_reserve_are_zero_not_nan():
    values = symbol_target_values({'AAA': 100.0}, label_target_pct=100.0,
                                  base_notional=10_000.0, unallocated_pct=100.0)
    assert values == {'AAA': 0.0}


# -- the reserve's own money -------------------------------------------------

def test_reserve_dollars_is_what_the_engine_holds_back():
    assert reserve_dollars(10_000.0, 25.0) == 2_500.0
    assert reserve_dollars(10_000.0, 0.0) == 0.0
    assert reserve_dollars(10_000.0, 100.0) == 10_000.0


def test_reserve_dollars_without_a_base_is_None_not_zero():
    assert reserve_dollars(None, 25.0) is None
    assert reserve_dollars(0.0, 25.0) is None


def test_the_reserve_caption_states_the_money_and_what_is_left():
    assert format_reserve_caption(10_000.0, 25.0) == \
        '25% of the $10,000.00 base = $2,500.00 held back — $7,500.00 investable out of $10,000.00'


def test_the_reserve_caption_names_the_base_it_is_a_percentage_OF():
    """The reported confusion: "$536.98 held back, $4,832.81 investable" invites the
    obvious sanity check, and that check is the wrong one -- 10% of the 4,832.81 you can
    SEE is 483, not 537, so the page looks broken. It is not: the reserve is a share of
    the GROSS base, and reserved is DEFINED as ``base - investable``, so the two halves
    sum to the base exactly. Naming the base makes the division checkable on screen.

    A reserve measured against the REMAINDER would be circular -- the remainder is what
    is left after the reserve -- and would break that identity."""
    text = format_reserve_caption(5_369.79, 10.0)

    assert '$5,369.79 base' in text, "the caption must name the base it divides"
    assert '$536.98 held back' in text
    assert '$4,832.81 investable out of $5,369.79' in text
    # The whole point: the reader can now verify it without leaving the line.
    assert round(5_369.79 * 0.10, 2) == 536.98
    assert round(536.98 + 4_832.81, 2) == 5_369.79


def test_the_reserve_caption_says_so_when_there_is_no_base_instead_of_showing_zero():
    text = format_reserve_caption(None, 25.0)
    assert '$' not in text
    assert 'no' in text.lower()


def test_the_reserve_row_line_is_the_string_the_page_has_always_drawn():
    text = format_reserve_row(base_notional=10_000.0, available_buying_power=1_000.0,
                              unallocated_pct=25.0)
    assert text == ('Unallocated (free buying power) — now $1,000.00 (10.0% of base) '
                    '· target $2,500.00 (25.00% of base)')


def test_each_percentage_sits_beside_the_dollars_it_describes():
    """The reported bug. The old line read
    "$542.61 (10.1% of base, target 10.00% of base = $536.98)", which put the ACTUAL
    percentage next to the TARGET dollars -- and directly under a caption that had just
    said "$536.98 held back". It read as "$536.98 is 10.1%", and the page looked like it
    could not add up. It could: base 5,369.79, target 10.00% = 536.98, actual 542.61 =
    10.1049%. Nothing was wrong except which number the eye bound the percentage to.
    """
    text = format_reserve_row(base_notional=5_369.79, available_buying_power=542.61,
                              unallocated_pct=10.0)

    # Each amount is immediately followed by ITS OWN percentage.
    assert '$542.61 (10.1% of base)' in text
    assert '$536.98 (10.00% of base)' in text
    # And each is labelled as measured or wanted.
    assert 'now $542.61' in text
    assert 'target $536.98' in text


def test_the_reserve_row_line_is_None_when_there_is_nothing_to_divide_by():
    assert format_reserve_row(base_notional=0.0, available_buying_power=1_000.0,
                              unallocated_pct=25.0) is None
    assert format_reserve_row(base_notional=None, available_buying_power=1_000.0,
                              unallocated_pct=25.0) is None
    assert format_reserve_row(base_notional=10_000.0, available_buying_power=None,
                              unallocated_pct=25.0) is None


def test_the_reserve_row_line_follows_the_reserve_box():
    text = format_reserve_row(base_notional=10_000.0, available_buying_power=1_000.0,
                              unallocated_pct=60.0)
    assert 'target $6,000.00 (60.00% of base)' in text


# ---------------------------------------------------------------------------
# LABEL COLOURS
#
# A FIXED palette, never a free colour picker: this UI is dark-themed and an
# arbitrary picker produces unreadable choices (and, since the value ends up
# interpolated into a CSS ``style`` attribute, an unbounded one is a place to put
# something other than a colour).
# ---------------------------------------------------------------------------

def test_the_palette_is_the_okabe_ito_colour_universal_design_set():
    """Okabe-Ito is the published colour-universal-design set: it is validated for
    protanopia, deuteranopia and tritanopia, and it deliberately contains no
    red-versus-green pair that carries meaning on its own."""
    assert [hex_value for _name, hex_value in LABEL_COLOR_PALETTE] == [
        '#E69F00', '#56B4E9', '#009E73', '#F0E442',
        '#0072B2', '#D55E00', '#CC79A7',
    ]


def test_the_palette_leaves_out_okabe_itos_black_because_the_ui_is_dark():
    assert '#000000' not in {hex_value for _n, hex_value in LABEL_COLOR_PALETTE}


def test_every_swatch_clears_the_non_text_contrast_floor_on_this_background():
    """WCAG 1.4.11 wants 3:1 for a graphical object. The page's dark surface is
    #1E1E1E; a swatch below that floor is a colour the user cannot see they chose."""
    def _luminance(hex_value):
        parts = [int(hex_value[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                  for c in parts]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    background = _luminance('#1E1E1E')
    for name, hex_value in LABEL_COLOR_PALETTE:
        contrast = (_luminance(hex_value) + 0.05) / (background + 0.05)
        assert contrast >= 3.0, f'{name} {hex_value} is only {contrast:.2f}:1'


def test_every_swatch_is_distinguishable_from_every_other_by_lightness_or_hue():
    """Two swatches a colour-blind user reads as the same colour AND the same
    lightness are one swatch with two names."""
    seen = {hex_value for _n, hex_value in LABEL_COLOR_PALETTE}
    assert len(seen) == len(LABEL_COLOR_PALETTE)
    names = {name for name, _h in LABEL_COLOR_PALETTE}
    assert len(names) == len(LABEL_COLOR_PALETTE)


def test_the_options_offer_an_explicit_no_colour_entry():
    """"No colour" is a choice the user has to be able to make and to go BACK to;
    without it a colour, once set, could never be cleared."""
    options = label_color_options()
    assert NO_LABEL_COLOR in options
    assert options[NO_LABEL_COLOR] == 'No colour'
    for _name, hex_value in LABEL_COLOR_PALETTE:
        assert hex_value in options


def test_the_no_colour_option_is_the_empty_string_not_None():
    """NiceGUI's ``Select`` drops a value that is not in its options and treats
    ``None`` as "nothing selected", so the cleared state needs a real value."""
    assert NO_LABEL_COLOR == ''


def test_normalising_a_palette_colour_returns_its_canonical_hex():
    assert normalise_label_color('#e69f00') == '#E69F00'
    assert normalise_label_color(' #E69F00 ') == '#E69F00'


def test_normalising_no_colour_gives_None_which_is_what_NULL_means():
    assert normalise_label_color(None) is None
    assert normalise_label_color('') is None
    assert normalise_label_color('   ') is None


def test_normalising_refuses_anything_that_is_not_a_COLOUR():
    """The write path. The SET is open -- the user asked for a picker -- but the
    PARSE is not: an unbounded value here is a CSS ``style`` injection. ``#123456``
    is now a legitimate custom colour; the other two never were."""
    assert normalise_label_color('#123456') == '#123456'
    with pytest.raises(ValueError):
        normalise_label_color('red')
    with pytest.raises(ValueError):
        normalise_label_color('#E69F00; background:url(x)')


def test_resolving_a_stored_colour_gives_the_hex_to_draw_with():
    assert resolve_label_icon_color('#E69F00') == '#E69F00'
    assert resolve_label_icon_color('#e69f00') == '#E69F00'


def test_resolving_no_colour_gives_the_neutral_default_not_an_empty_style():
    """NULL means "no colour chosen", which is a different fact from a stored
    default -- but something still has to be drawn."""
    assert resolve_label_icon_color(None) == DEFAULT_LABEL_ICON_COLOR
    assert resolve_label_icon_color('') == DEFAULT_LABEL_ICON_COLOR


def test_resolving_a_value_that_is_not_a_COLOUR_falls_back_rather_than_drawing_it():
    """The READ path is tolerant where the write path refuses: a hand-edited row
    must not take the page down, and it must not reach the style attribute either.
    A well-formed custom hex is drawn; anything else is grey."""
    assert resolve_label_icon_color('#123456') == '#123456'
    assert resolve_label_icon_color('red; content:"x"') == DEFAULT_LABEL_ICON_COLOR


def test_a_managed_label_carries_its_colour_into_the_view():
    views = build_label_views([ManagedLabel('ARK26', 40.0, color='#56B4E9')],
                              {'ARK26': []}, {}, {},
                              valuation_mode=VALUATION_MODE_COST)
    assert views[0].color == '#56B4E9'


def test_a_label_with_no_colour_reaches_the_view_as_None_not_as_a_default():
    views = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': []}, {}, {},
                              valuation_mode=VALUATION_MODE_COST)
    assert views[0].color is None


# ---------------------------------------------------------------------------
# The running label total, live on the page
#
# The page has to say "these do not add up" WITHOUT a dry run -- that is the
# validation the Allocate wizard has always done at step 1, and moving the boxes
# onto the page moves the advisory with them.
# ---------------------------------------------------------------------------

def test_a_label_set_that_totals_100_says_nothing():
    assert format_label_total_notice({'A': 60.0, 'B': 40.0}) is None


def test_a_label_set_under_100_is_a_warning_in_the_engines_words():
    """OUTSIDE the band, which is what the advisory now reports. 90% is a shortfall
    the card still states in words, but it is close enough not to be shouted about
    -- see ``within_target_band``."""
    from ba2_trade_platform.core.portfolio_allocation import ERROR_LABEL_UNDER_FMT

    text, severity = format_label_total_notice({'A': 60.0, 'B': 10.0})
    assert text == ERROR_LABEL_UNDER_FMT.format(total=70.0, under=30.0)
    assert severity == 'warning'


def test_a_shortfall_INSIDE_the_band_goes_quiet_rather_than_warning():
    """The reconciliation: the card is green at 90%, so the advisory beside it may
    not be orange. The figure itself is never hidden -- the card prints it."""
    assert format_label_total_notice({'A': 60.0, 'B': 30.0}) is None


def test_a_label_set_over_100_is_an_error_in_the_engines_words():
    """Unreachable through the inline boxes, which refuse it -- but reachable from
    a database written before this page had boxes at all, and from the wizard."""
    from ba2_trade_platform.core.portfolio_allocation import ERROR_LABEL_TOTAL_FMT

    text, severity = format_label_total_notice({'A': 60.0, 'B': 60.0})
    assert text == ERROR_LABEL_TOTAL_FMT.format(total=120.0, over=20.0)
    # STILL 'negative'. The band decides the BAR's colour and the shortfall side's
    # tone; over 100 is a money-safety signal that predates it and keeps its red.
    assert severity == 'negative'


def test_the_engines_tolerance_still_chooses_the_SENTENCE():
    """Two different jobs, kept apart. The engine's 0.01pp tolerance decides which
    WORDS -- on target versus a shortfall -- and the band decides how alarmed the
    page looks. 99.00% is inside the band (green, quiet) and is still described in
    words as being one point short."""
    from ba2_trade_platform.core.portfolio_allocation import LABEL_TOTAL_TOLERANCE_PCT
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        LABEL_TOTAL_ON_TARGET, judge_label_total)

    inside = 100.0 - LABEL_TOTAL_TOLERANCE_PCT / 2.0
    assert judge_label_total({'A': inside})[2] == LABEL_TOTAL_ON_TARGET
    assert 'under 100%' in judge_label_total({'A': 99.0})[2]
    # ...and both are quiet, because both are inside the band.
    assert format_label_total_notice({'A': inside}) is None
    assert format_label_total_notice({'A': 99.0}) is None


def test_an_account_with_no_managed_labels_is_not_told_it_is_10_percent_short():
    """Nothing is managed, so there is no set to be wrong. The empty-state banner
    already says what to do."""
    assert format_label_total_notice({}) is None


def test_the_running_total_reports_the_default_all_zero_state_the_page_shipped_with():
    """THE defect: labels have only ever been settable in the Allocate wizard, so on
    an untouched account every target is 0 and the page said nothing about it."""
    text, severity = format_label_total_notice({'A': 0.0, 'B': 0.0})
    assert '100.00%' in text
    assert severity == 'warning'


def test_the_store_value_for_a_chosen_colour_is_its_hex():
    assert store_color_value('#e69f00') == '#E69F00'


def test_the_store_value_for_no_colour_is_the_empty_string_not_None():
    """``set_managed_label(color=None)`` means LEAVE UNCHANGED, so handing it the
    ``None`` that ``normalise_label_color`` returns would make "clear the colour"
    silently do nothing -- the swatch would be un-removable."""
    assert store_color_value(NO_LABEL_COLOR) == ''
    assert store_color_value(None) == ''
    assert store_color_value('') == ''


def test_the_store_value_still_refuses_a_value_that_is_not_a_colour():
    assert store_color_value('#123456') == '#123456'
    with pytest.raises(ValueError):
        store_color_value('chartreuse')


# ---------------------------------------------------------------------------
# THE LABEL MINI-BAR ROW
#
# One bar per label showing the CURRENT share, tinted with the label's own colour,
# plus a NOTCH marking the target and a status WORD. The bar and the notch share
# ONE scale -- if they did not, "over"/"under" would be a lie that no reader could
# spot by eye.
# ---------------------------------------------------------------------------

def _view(label, current, target, *, base=10_000.0, reserve=0.0, color=None):
    """A LabelView with the two numbers the bar reads, and nothing else."""
    return LabelView(label=label, target_pct=target, current_value=current,
                     pct_of_base=(current / base * 100.0) if base else None,
                     pct_of_total=0.0, color=color)


def test_the_bar_scale_is_the_largest_of_every_current_and_every_target():
    """Scaling to 100% of base leaves every bar stubby on a diversified book;
    scaling to the largest figure on the page makes the biggest bar fill the track
    and keeps every row comparable against it."""
    views = [_view('A', 3_950.0, 25.0), _view('B', 2_200.0, 30.0)]
    assert bar_scale_pct(views, base_notional=10_000.0, unallocated_pct=0.0) == 39.5


def test_the_bar_scale_can_be_set_by_a_TARGET_rather_than_a_holding():
    """A label that is entirely un-bought has a target far above every current
    value, and its notch has to fit on the track."""
    views = [_view('A', 500.0, 80.0)]
    assert bar_scale_pct(views, base_notional=10_000.0, unallocated_pct=0.0) == 80.0


def test_the_bar_scale_measures_holdings_on_the_SAME_denominator_as_targets():
    """Both divide the INVESTABLE pool. A 100% target IS the top of the track, and
    $1,000 held against a $5,000 pool is the 20% underneath it -- where the old
    of-base reading made the same holding 10% and the same target 50%."""
    views = [_view('A', 1_000.0, 100.0)]
    assert bar_scale_pct(views, base_notional=10_000.0, unallocated_pct=50.0) == 100.0
    bar = build_label_bars(views, base_notional=10_000.0, unallocated_pct=50.0)[0]
    assert bar.current_pct == pytest.approx(20.0)


def test_the_bar_scale_never_returns_zero_to_divide_by():
    """Every label at zero with no target is a real state (a fresh account)."""
    assert bar_scale_pct([_view('A', 0.0, 0.0)], base_notional=10_000.0,
                         unallocated_pct=0.0) > 0.0
    assert bar_scale_pct([], base_notional=10_000.0, unallocated_pct=0.0) > 0.0
    # ...including when there is no pool at all to divide by.
    assert bar_scale_pct([_view('A', 5.0, 5.0)], base_notional=None,
                         unallocated_pct=0.0) > 0.0


def test_a_short_labels_negative_value_does_not_stretch_the_scale():
    """Shorts carry a NEGATIVE current value (see the page docstring). Feeding one
    to a max() is harmless, but it must not become the scale."""
    views = [_view('SHORT', -5_000.0, 10.0), _view('LONG', 2_000.0, 10.0)]
    assert bar_scale_pct(views, base_notional=10_000.0, unallocated_pct=0.0) == 20.0


def test_the_bar_and_the_notch_are_computed_on_ONE_scale():
    """THE mutation this section exists for: a notch on a different scale silently
    inverts over/under and no reader would spot it."""
    views = [_view('A', 4_000.0, 40.0), _view('B', 2_000.0, 80.0)]
    bars = {bar.label: bar for bar in build_label_bars(views, base_notional=10_000.0,
                                                       unallocated_pct=0.0)}
    scale = bar_scale_pct(views, base_notional=10_000.0, unallocated_pct=0.0)   # 80.0
    assert bars['A'].bar_fraction == pytest.approx(40.0 / scale)
    assert bars['A'].notch_fraction == pytest.approx(40.0 / scale)
    assert bars['B'].bar_fraction == pytest.approx(20.0 / scale)
    assert bars['B'].notch_fraction == pytest.approx(80.0 / scale)


def test_the_biggest_row_fills_the_whole_track():
    views = [_view('A', 3_950.0, 25.0), _view('B', 500.0, 5.0)]
    bars = {bar.label: bar for bar in build_label_bars(views, base_notional=10_000.0,
                                                       unallocated_pct=0.0)}
    assert bars['A'].bar_fraction == pytest.approx(1.0)


def test_one_label_at_100_percent_fills_the_track_and_notches_at_the_end():
    views = [_view('A', 10_000.0, 100.0)]
    bar = build_label_bars(views, base_notional=10_000.0, unallocated_pct=0.0)[0]
    assert bar.bar_fraction == pytest.approx(1.0)
    assert bar.notch_fraction == pytest.approx(1.0)


def test_every_label_at_zero_draws_empty_bars_rather_than_dividing_by_zero():
    views = [_view('A', 0.0, 0.0), _view('B', 0.0, 0.0)]
    bars = build_label_bars(views, base_notional=10_000.0, unallocated_pct=0.0)
    assert [bar.bar_fraction for bar in bars] == [0.0, 0.0]
    assert [bar.notch_fraction for bar in bars] == [0.0, 0.0]


def test_a_negative_current_value_clamps_the_BAR_but_not_the_NUMBER():
    """A short label's value is negative. Rendered as a fraction of a track it must
    not come out as a giant positive bar; the figure beside it stays negative."""
    views = [_view('SHORT', -5_000.0, 10.0), _view('LONG', 2_000.0, 10.0)]
    bar = build_label_bars(views, base_notional=10_000.0, unallocated_pct=0.0)[0]
    assert bar.bar_fraction == 0.0
    assert bar.current_pct == -50.0


def test_a_bar_fraction_never_exceeds_the_track():
    views = [_view('A', 20_000.0, 10.0)]
    assert build_label_bars(views, base_notional=10_000.0,
                            unallocated_pct=0.0)[0].bar_fraction <= 1.0


def test_the_notch_moves_when_the_reserve_does():
    """A notch that does not move when the reserve is dragged is the stale-figure
    bug in visual form."""
    # A second, bigger holding pins the track so the notch is measured against
    # something that does NOT move -- otherwise the notch sets its own scale and
    # sits at 1.0 whatever the reserve is.
    views = [_view('A', 4_000.0, 100.0), _view('BIG', 10_000.0, 0.0)]
    at_zero = build_label_bars(views, base_notional=10_000.0, unallocated_pct=0.0)[0]
    at_sixty = build_label_bars(views, base_notional=10_000.0, unallocated_pct=60.0)[0]
    # The TYPED target does not move with the reserve any more -- it is a share of
    # the pool, and the pool is what shrank. What moves is the holding's share of it.
    assert at_zero.target_pct == at_sixty.target_pct == 100.0
    assert at_zero.effective_pct == 100.0
    assert at_sixty.effective_pct == pytest.approx(40.0)
    assert at_zero.current_pct == pytest.approx(40.0)
    assert at_sixty.current_pct == pytest.approx(100.0)
    # BIG holds the whole $10,000 and pins the track: 100% of base at a 0% reserve,
    # 250% of the $4,000 pool at 60%. A's notch is read against that.
    assert at_zero.notch_fraction == pytest.approx(1.0)
    assert at_sixty.notch_fraction == pytest.approx(0.4)


def test_each_bar_carries_its_own_labels_colour():
    views = [_view('A', 100.0, 10.0, color='#56B4E9'), _view('B', 100.0, 10.0)]
    bars = build_label_bars(views, base_notional=10_000.0, unallocated_pct=0.0)
    assert bars[0].color == '#56B4E9'
    assert bars[1].color == DEFAULT_LABEL_ICON_COLOR


# -- the status word ---------------------------------------------------------

def test_a_label_on_target_reads_ok():
    views = [_view('A', 2_500.0, 25.0)]
    assert build_label_bars(views, base_notional=10_000.0,
                            unallocated_pct=0.0)[0].status == LABEL_STATUS_OK


def test_a_label_above_its_target_reads_over():
    views = [_view('A', 3_950.0, 25.0)]
    assert build_label_bars(views, base_notional=10_000.0,
                            unallocated_pct=0.0)[0].status == LABEL_STATUS_OVER


def test_a_label_below_its_target_reads_under():
    views = [_view('A', 1_400.0, 30.0)]
    assert build_label_bars(views, base_notional=10_000.0,
                            unallocated_pct=0.0)[0].status == LABEL_STATUS_UNDER


def test_the_ok_band_is_a_tolerance_not_an_equality_and_holds_at_both_edges():
    """Floating point makes exact equality meaningless and a fraction of a
    percentage point is not actionable, so ``ok`` is a band -- pinned here at both
    edges so widening or narrowing it is a deliberate act."""
    edge = LABEL_STATUS_TOLERANCE_PCT
    base = 10_000.0

    just_inside_high = _view('A', (25.0 + edge * 0.99) / 100.0 * base, 25.0)
    just_inside_low = _view('A', (25.0 - edge * 0.99) / 100.0 * base, 25.0)
    just_outside_high = _view('A', (25.0 + edge * 1.01) / 100.0 * base, 25.0)
    just_outside_low = _view('A', (25.0 - edge * 1.01) / 100.0 * base, 25.0)

    def _status(view):
        return build_label_bars([view], base_notional=base,
                                unallocated_pct=0.0)[0].status

    assert _status(just_inside_high) == LABEL_STATUS_OK
    assert _status(just_inside_low) == LABEL_STATUS_OK
    assert _status(just_outside_high) == LABEL_STATUS_OVER
    assert _status(just_outside_low) == LABEL_STATUS_UNDER


def test_a_label_with_no_position_and_no_target_reads_as_a_dash():
    """Not 'ok'. Nothing has been asked of it and nothing is held; calling that
    "on target" would put a tick beside a label the user has not configured."""
    views = [_view('A', 0.0, 0.0)]
    assert build_label_bars(views, base_notional=10_000.0,
                            unallocated_pct=0.0)[0].status == LABEL_STATUS_NONE


def test_the_status_words_are_words_and_not_only_colours():
    """Colour alone excludes the readers change 4's palette was chosen for."""
    assert {LABEL_STATUS_OVER, LABEL_STATUS_UNDER, LABEL_STATUS_OK,
            LABEL_STATUS_NONE} == {'over', 'under', 'ok', '—'}


def test_with_no_base_the_status_is_unknown_rather_than_a_false_comparison():
    """"% of managed" and "% of base" are different denominators. Without a base
    there is nothing to compare the target against, and guessing would produce an
    over/under nobody can act on."""
    views = [LabelView(label='A', target_pct=50.0, current_value=9_000.0,
                       pct_of_base=None, pct_of_total=90.0)]
    bar = build_label_bars(views, base_notional=None, unallocated_pct=0.0)[0]
    assert bar.status == LABEL_STATUS_NONE
    assert bar.notch_fraction is None


# -- display order and the totals footer -------------------------------------

def test_labels_are_sorted_by_current_value_largest_first():
    """The 39.5% row used to sit between two 1-5% rows."""
    views = [_view('small', 72.63, 5.0), _view('big', 2_021.84, 25.0),
             _view('mid', 1_126.88, 30.0)]
    assert [v.label for v in sort_label_views(views)] == ['big', 'mid', 'small']


def test_labels_with_equal_value_are_ordered_by_name_so_the_page_is_stable():
    views = [_view('Zulu', 0.0, 0.0), _view('Alpha', 0.0, 0.0)]
    assert [v.label for v in sort_label_views(views)] == ['Alpha', 'Zulu']


def test_sorting_does_not_dim_or_drop_an_empty_label():
    """Explicitly declined by the user: zero-value labels stay fully visible."""
    views = [_view('empty', 0.0, 0.0), _view('full', 100.0, 100.0)]
    assert len(sort_label_views(views)) == 2
    assert 'empty' in {v.label for v in sort_label_views(views)}


def test_sorting_leaves_the_caller_s_list_alone():
    views = [_view('a', 1.0, 0.0), _view('b', 2.0, 0.0)]
    sort_label_views(views)
    assert [v.label for v in views] == ['a', 'b']


def test_the_footer_accounts_for_the_labels_AND_the_reserve():
    text, severity = format_allocation_footer({'A': 60.0, 'B': 40.0},
                                              unallocated_pct=10.0)
    assert '100.00%' in text            # the label total
    assert '90.00%' in text             # what that is as a share of the base
    assert '10.00%' in text             # the reserve
    assert severity == 'ok'


def test_the_footer_turns_red_the_moment_the_labels_pass_100():
    """Unchanged by the band, and deliberately so: the band governs the shortfall
    side and the bar's fill. The footer still shares the card's one verdict -- it
    just no longer computes it a second time."""
    text, severity = format_allocation_footer({'A': 60.0, 'B': 45.0},
                                              unallocated_pct=0.0)
    assert severity == 'negative'
    assert '105.00%' in text


def test_the_footer_flags_a_shortfall_as_well():
    text, severity = format_allocation_footer({'A': 60.0}, unallocated_pct=0.0)
    assert severity == 'warning'
    assert '60.00%' in text


def test_the_footer_goes_quiet_inside_the_band_like_everything_else():
    _text, severity = format_allocation_footer({'A': 90.0}, unallocated_pct=0.0)
    assert severity == 'ok'


def test_the_footer_of_an_account_managing_nothing_says_so_without_crying_wolf():
    text, severity = format_allocation_footer({}, unallocated_pct=0.0)
    assert severity == 'ok'
    assert 'no managed labels' in text.lower()


def test_the_footer_adds_up_to_100_of_base_at_every_reserve():
    for reserve in (0.0, 10.0, 50.0, 100.0):
        text, severity = format_allocation_footer({'A': 100.0},
                                                  unallocated_pct=reserve)
        assert severity == 'ok', reserve
        assert '= 100.00% of base' in text, reserve


def test_the_ok_band_is_exactly_half_a_percentage_point():
    """Pinned as a LITERAL, not derived from the constant.

    The edge test above computes its inputs FROM ``LABEL_STATUS_TOLERANCE_PCT``, so
    widening the band to 5pp moves the test with it and the change passes unseen --
    a mutation proved exactly that. This is the assertion the band cannot slide
    past: changing it has to be a deliberate edit of a number in a test.
    """
    assert LABEL_STATUS_TOLERANCE_PCT == 0.5


def test_the_status_flips_between_a_0_4_and_a_0_6_point_drift():
    """The same band, stated in absolute figures rather than in terms of itself."""
    base = 10_000.0

    def _status(current_pct):
        view = LabelView(label='A', target_pct=25.0,
                         current_value=current_pct / 100.0 * base,
                         pct_of_base=current_pct, pct_of_total=0.0)
        return build_label_bars([view], base_notional=base,
                                unallocated_pct=0.0)[0].status

    assert _status(25.4) == LABEL_STATUS_OK
    assert _status(24.6) == LABEL_STATUS_OK
    assert _status(25.6) == LABEL_STATUS_OVER
    assert _status(24.4) == LABEL_STATUS_UNDER


def test_the_bar_geometry_clamps_at_both_ends_of_the_track():
    """``_bar_fraction`` direct, because neither clamp is reachable through
    ``build_label_bars`` today -- the scale IS the page maximum, so nothing can
    exceed it, and the lower clamp is only hit by a short.

    They are defence-in-depth against a caller measuring against a scale computed
    over a different set (a cached one, a second page section), which is precisely
    the mistake that silently inverts a bar. A mutation removing the upper clamp
    survived everything else in this file.
    """
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import _bar_fraction

    assert _bar_fraction(50.0, 100.0) == 0.5
    assert _bar_fraction(-50.0, 100.0) == 0.0      # a short is empty, not full
    assert _bar_fraction(150.0, 100.0) == 1.0      # never past the end of the track
    assert _bar_fraction(0.0, 100.0) == 0.0


def test_the_footer_clamps_an_out_of_range_reserve_instead_of_printing_it():
    """The controls refuse one, but a database written before they existed can hold
    it. Printing "+ 140.00% reserve = 140.00% of base" would be arithmetic nobody
    can act on."""
    text, _severity = format_allocation_footer({'A': 100.0}, unallocated_pct=140.0)
    assert '100.00% reserve' in text
    assert '140' not in text


def test_a_bar_with_no_pool_reports_NO_share_rather_than_a_third_denominator():
    """It used to fall back to ``pct_of_total`` -- the share of MANAGED value -- in
    the same cell that otherwise holds a share of the pool. Silently swapping the
    denominator inside one column is the defect the investable rule removes; the
    money is still printed, and the header names "% of managed" where it says so."""
    views = [LabelView(label='A', target_pct=50.0, current_value=9_000.0,
                       pct_of_base=None, pct_of_total=90.0)]
    bar = build_label_bars(views, base_notional=None, unallocated_pct=0.0)[0]
    assert bar.current_pct is None
    assert bar.current_text == '—'
    assert bar.current_value == 9_000.0


# ---------------------------------------------------------------------------
# THE ACCOUNT VALUE CARD
#
# The summary row showed MANAGED value -- the market value of the managed
# positions -- and nothing else in dollars. On a margin account that number
# exceeds the account's own equity (the reporting user's book: $4,853.48 of
# managed positions against roughly $2,400 of account value), so the page read
# as if it were describing an account twice its real size.
#
# ``net_liquidation`` is the field, per ``AccountSnapshot``'s own contract
# ("report ``net_liquidation`` as the account's headline total value"), and the
# UNAVAILABLE case is the one that matters: a card that renders $0.00 for
# "the broker did not answer" is a claim about money.
# ---------------------------------------------------------------------------

def _snapshot(**kw):
    """A real ``AccountSnapshot``, so the field names are the contract's own."""
    from ba2_common.core.account_types import AccountSnapshot
    return AccountSnapshot(**kw)


def test_the_account_value_is_read_from_net_liquidation():
    assert account_value_from_snapshot(
        _snapshot(net_liquidation=2_511.90, equity=2_511.90)) == 2_511.90


def test_the_account_value_is_net_liquidation_and_not_any_neighbouring_balance():
    """Five nearby quantities, none of them interchangeable on a margin account.

    ``cash`` is NEGATIVE while a margin loan is outstanding, ``long_market_value``
    is the very figure the 'Managed value' card already shows, and
    ``buying_power`` is the free cash the third card shows. Picking any of them
    would put a plausible number under the right caption.
    """
    snapshot = _snapshot(cash=-2_341.58, equity=1.0, net_liquidation=2_511.90,
                         buying_power=170.31, long_market_value=4_853.48,
                         non_marginable_buying_power=0.0)
    assert account_value_from_snapshot(snapshot) == 2_511.90


def test_a_snapshot_that_published_no_net_liquidation_is_unknown_not_zero():
    assert account_value_from_snapshot(_snapshot(buying_power=170.31)) is None


def test_no_snapshot_at_all_is_unknown_not_zero():
    assert account_value_from_snapshot(None) is None


def test_an_uncoerced_broker_decimal_is_made_a_float_rather_than_taking_the_page_down():
    """``AccountSnapshot`` says every money field is a plain float and the ADAPTER
    coerces -- but TastyTrade's balances arrive as ``Decimal`` and it is one
    forgotten ``float()`` away from shipping one. It formats fine, so the card
    would look right; the LEVERAGE clause then does ``float / Decimal`` and
    raises TypeError, 500-ing the whole page from a cosmetic caption.

    A mutation removing the coercion survived every other test in this file.
    """
    from decimal import Decimal

    value = account_value_from_snapshot(_snapshot(net_liquidation=Decimal('2511.90')))
    assert isinstance(value, float)
    assert value == 2_511.90
    # ...and the clause it feeds is computable rather than a TypeError.
    assert account_value_card(account_value=value,
                              managed_value=4_853.48).leverage is not None


def test_the_account_value_card_prints_the_money_the_broker_reported():
    card = account_value_card(account_value=2_511.90, managed_value=4_853.48)
    assert card.available is True
    assert card.text == '$2,511.90'
    assert card.title == ACCOUNT_VALUE_TITLE


def test_the_account_value_card_says_n_a_rather_than_zero_when_it_is_unknown():
    """THE regression this card exists to avoid. ``$0.00`` under 'Account value'
    reads as an account with nothing in it, which is a statement about money the
    broker never made."""
    card = account_value_card(account_value=None, managed_value=4_853.48)
    assert card.available is False
    assert card.text == ACCOUNT_VALUE_UNAVAILABLE_TEXT
    assert '0.00' not in card.text
    assert '$' not in card.text
    assert '0.00' not in card.detail
    assert '$' not in card.detail
    # ...and it says WHY, in the manner of the expert cards' "n/a — <reason>".
    assert card.detail == ACCOUNT_VALUE_UNAVAILABLE_DETAIL
    assert card.detail


def test_an_account_genuinely_worth_zero_is_reported_as_zero_not_as_unknown():
    """The INVERSE error. A fully withdrawn account really is worth $0.00, and
    suppressing that as "unavailable" hides a real state behind an outage."""
    card = account_value_card(account_value=0.0, managed_value=0.0)
    assert card.available is True
    assert card.text == '$0.00'
    assert card.detail != ACCOUNT_VALUE_UNAVAILABLE_DETAIL


def test_the_card_states_the_leverage_the_managed_value_is_carrying():
    """The user's actual point: 'managed value (which is leveraged)'. The two
    numbers sit side by side and the multiple between them is said out loud."""
    card = account_value_card(account_value=2_511.90, managed_value=4_853.48)
    assert card.leverage == pytest.approx(4_853.48 / 2_511.90)
    assert '1.93x' in card.detail


def test_an_unleveraged_book_says_so_rather_than_going_quiet():
    card = account_value_card(account_value=5_000.0, managed_value=5_000.0)
    assert card.leverage == pytest.approx(1.0)
    assert '1.00x' in card.detail


def test_the_leverage_clause_is_dropped_rather_than_dividing_by_zero():
    """A $0.00 account value is a real state and must still render the $0.00 --
    but ``managed / 0`` is not a number, and ``inf x`` is not a caption."""
    card = account_value_card(account_value=0.0, managed_value=4_853.48)
    assert card.available is True
    assert card.text == '$0.00'
    assert card.leverage is None
    assert 'x' not in card.detail


def test_a_negative_account_value_is_shown_but_carries_no_multiple():
    """An account underwater on its margin loan. The figure is a fact worth
    printing; a NEGATIVE multiple of it is arithmetic nobody can act on."""
    card = account_value_card(account_value=-1_200.0, managed_value=4_853.48)
    assert card.available is True
    assert card.text == '-$1,200.00'
    assert card.leverage is None


def test_a_net_short_managed_book_reports_its_negative_multiple():
    """Shorts are signed negative on this page, so the managed value legitimately
    is. The multiple follows the sign rather than being hidden."""
    card = account_value_card(account_value=2_000.0, managed_value=-1_000.0)
    assert card.leverage == pytest.approx(-0.5)
    assert '-0.50x' in card.detail


def test_the_unavailable_card_carries_no_multiple_either():
    card = account_value_card(account_value=None, managed_value=4_853.48)
    assert card.leverage is None


# ---------------------------------------------------------------------------
# "FILL 100%" -- the per-label button that replaced automatic recalculation
#
# Editing one symbol's share used to re-read the whole label, because the symbols
# with no stored row resolve to a share of whatever is left of 100: typing in one
# box moved every other number on the row. The user asked for that to stop ("do not
# automatically recalculate when I adjust share of label within label. Do not change
# other numbers"), which leaves the set free to stop totalling 100 -- so there has
# to be a deliberate way to put it back.
#
# "EMPTY" MEANS A SHARE OF ZERO, and that definition is the hinge the whole feature
# turns on. It is the engine's own (``_symbol_weight``: "'Unset' and 0 are one fact
# here"), and it is the only one that can agree with the no-recalc rule: after the
# recalculation is gone, the number ON SCREEN is the only weight there is, so
# "empty" has to be a property of that number and not of whether a database row
# happens to exist behind it.
# ---------------------------------------------------------------------------

def test_fill_100_splits_the_shortfall_between_the_EMPTY_symbols_only():
    """Case 1. The two typed weights are left EXACTLY as typed."""
    result = fill_label_to_100('ARK26', {'AAA': 30.0, 'BBB': 0.0, 'CCC': 0.0})
    assert result.changed is True
    assert result.reason_code == FILL_FILLED_EMPTY
    assert result.weights == {'AAA': 30.0, 'BBB': 35.0, 'CCC': 35.0}


def test_fill_100_scales_an_over_allocated_label_DOWN_proportionally():
    """Case 2. 150/50 is 3:1, so it stays 3:1 at 75/25 -- not clipped, not evened."""
    result = fill_label_to_100('ARK26', {'AAA': 150.0, 'BBB': 50.0})
    assert result.reason_code == FILL_SCALED_DOWN
    assert result.weights == {'AAA': 75.0, 'BBB': 25.0}


def test_fill_100_scales_an_under_allocated_label_UP_proportionally():
    """Case 3: under 100 with NOTHING empty. 10/20 stays 1:2 at 33.33/66.67."""
    result = fill_label_to_100('ARK26', {'AAA': 10.0, 'BBB': 20.0})
    assert result.reason_code == FILL_SCALED_UP
    assert result.weights == {'AAA': 33.33, 'BBB': 66.67}


def test_an_over_allocated_label_SCALES_even_when_a_symbol_is_empty():
    """The three cases are ordered, and over-100 wins. Filling an empty slot out of
    a NEGATIVE remainder is the alternative, and there is no such thing."""
    result = fill_label_to_100('ARK26', {'AAA': 150.0, 'BBB': 50.0, 'CCC': 0.0})
    assert result.reason_code == FILL_SCALED_DOWN
    assert result.weights['CCC'] == 0.0
    assert round(sum(result.weights.values()), 2) == 100.0


def test_fill_100_at_exactly_100_says_so_rather_than_silently_doing_nothing():
    """A button that visibly does nothing is indistinguishable from a broken one."""
    result = fill_label_to_100('ARK26', {'AAA': 60.0, 'BBB': 40.0})
    assert result.changed is False
    assert result.reason_code == FILL_ALREADY_100
    assert result.weights == {'AAA': 60.0, 'BBB': 40.0}
    assert '100' in result.message and 'ARK26' in result.message


def test_the_already_100_verdict_uses_the_ENGINES_tolerance_not_equality():
    """33.33 + 33.33 + 33.34 is exactly 100 in decimal and 99.99999999999999 in
    binary. A hard equality here would offer to "fix" the engine's own even split."""
    result = fill_label_to_100('ARK26', {'A': 33.33, 'B': 33.33, 'C': 33.34})
    assert result.reason_code == FILL_ALREADY_100


def test_fill_100_on_a_label_with_no_symbols_says_so():
    result = fill_label_to_100('EMPTY', {})
    assert result.changed is False
    assert result.reason_code == FILL_NO_SYMBOLS
    assert result.weights == {}
    assert 'EMPTY' in result.message


def test_fill_100_gives_a_single_EMPTY_symbol_the_whole_100():
    result = fill_label_to_100('ARK26', {'AAA': 0.0})
    assert result.reason_code == FILL_FILLED_EMPTY
    assert result.weights == {'AAA': 100.0}


def test_fill_100_scales_a_single_NON_empty_symbol_to_100():
    assert fill_label_to_100('ARK26', {'AAA': 40.0}).weights == {'AAA': 100.0}
    assert fill_label_to_100('ARK26', {'AAA': 250.0}).weights == {'AAA': 100.0}


def test_fill_100_on_an_ALL_EMPTY_label_is_the_even_split():
    """Every symbol empty is case 1 with a remainder of the whole 100, which is
    ``split_pct_across(100, n)`` -- the same numbers ``get_symbol_weights`` hands an
    untouched label. Not a coincidence: it is the same function."""
    from ba2_trade_platform.core.portfolio_allocation import split_pct_across
    result = fill_label_to_100('ARK26', {'A': 0.0, 'B': 0.0, 'C': 0.0})
    assert result.reason_code == FILL_FILLED_EMPTY
    assert list(result.weights.values()) == split_pct_across(100.0, 3)


def test_fill_100_treats_a_MISSING_weight_as_empty_exactly_like_a_zero():
    """A cleared ``ui.number`` yields None. "Unset" and 0 are one fact here -- the
    engine's ``_symbol_weight`` says so, and Fill 100% must not disagree with it."""
    assert (fill_label_to_100('L', {'A': 40.0, 'B': None}).weights
            == fill_label_to_100('L', {'A': 40.0, 'B': 0.0}).weights)


def test_fill_100_always_sums_to_EXACTLY_100_never_99_99():
    """The whole reason the rounding is delegated: 99.99 by ACCIDENT is a set the
    engine's 0.01pp tolerance would still reject one hundredth further out, and a
    button that produced one would leave a label that cannot be submitted."""
    from decimal import Decimal
    cases = [
        {'A': 0.0, 'B': 0.0, 'C': 0.0},
        {'A': 10.0, 'B': 0.0, 'C': 0.0, 'D': 0.0},
        {'A': 1.0, 'B': 1.0, 'C': 1.0, 'D': 1.0, 'E': 1.0, 'F': 1.0},
        {'A': 150.0, 'B': 50.0, 'C': 33.0},
        {'A': 7.0, 'B': 11.0, 'C': 13.0},
        {'A': 99.97, 'B': 0.0},
        {'A': 33.3333, 'B': 33.3333, 'C': 0.0},
        {'A': 8.29, 'B': 0.0, 'C': 0.0, 'D': 0.0},
        {chr(65 + i): float(i + 1) for i in range(7)},
    ]
    for weights in cases:
        result = fill_label_to_100('L', weights)
        assert result.changed, weights          # every case here is out of the band
        total = sum(Decimal(str(v)) for v in result.weights.values())
        assert total == Decimal('100'), (weights, result.weights)


def test_fill_100_preserves_the_symbol_ORDER_it_was_given():
    """The display order is the caller's; the residual lands by VALUE, not by
    position, so a reordered map must not change which symbol absorbs it."""
    result = fill_label_to_100('L', {'Z': 10.0, 'A': 20.0, 'M': 0.0})
    assert list(result.weights) == ['Z', 'A', 'M']


def test_fill_100_never_promotes_a_symbol_the_user_set_to_zero_while_scaling():
    """Scaling down is proportional, and 0 x anything is 0. A rounding residual
    landing on the empty slot would turn "sell this out" into a position."""
    result = fill_label_to_100('L', {'A': 100.0, 'B': 50.0, 'C': 0.0})
    assert result.weights['C'] == 0.0


def test_fill_100_reports_which_case_it_took_in_words():
    """The three cases move different numbers; a single "done" message would leave
    the user unable to tell a proportional rescale from a fill of the gaps."""
    filled = fill_label_to_100('L', {'A': 30.0, 'B': 0.0})
    down = fill_label_to_100('L', {'A': 150.0, 'B': 50.0})
    up = fill_label_to_100('L', {'A': 10.0, 'B': 20.0})
    assert filled.message != down.message != up.message
    assert 'empty' in filled.message.lower()
    assert 'down' in down.message.lower()
    assert 'up' in up.message.lower()
    assert all(r.changed for r in (filled, down, up))


# ---------------------------------------------------------------------------
# THE INVESTABLE POOL IS 100%
#
# The user's rule: "The reserve is really for the tool to make the math but we
# should assume the available money is 100% for the allocation, for clarity." So
# every PRIMARY percentage on a label row -- current, target, the delta between
# them, the bar fill and the notch -- divides the INVESTABLE pool (what the reserve
# leaves). The share-of-gross-base figure is secondary and is shown in parentheses
# and marked as derived.
#
# Before this, ``current`` was a share of the gross base and ``target`` was
# relative: two different denominators printed with the same '%' sign, so the
# difference between them meant nothing. THAT is what these tests pin.
# ---------------------------------------------------------------------------

def test_the_current_share_divides_the_INVESTABLE_pool_not_the_gross_base():
    """$4,500 of a $10,000 base is 45% of base -- but under a 10% reserve only
    $9,000 is in play, so the row reads 50%. The mutation is to divide by the base."""
    bar = build_label_bars([_view('A', 4_500.0, 30.0)], base_notional=10_000.0,
                           unallocated_pct=10.0)[0]
    assert bar.current_pct == pytest.approx(50.0)


def test_the_target_shown_first_is_the_one_the_user_TYPED():
    """"put something like 15% (real 13.5%) so we know" -- what they typed leads."""
    bar = build_label_bars([_view('A', 4_500.0, 30.0)], base_notional=10_000.0,
                           unallocated_pct=10.0)[0]
    assert bar.target_pct == 30.0
    assert bar.effective_pct == pytest.approx(27.0)
    assert bar.target_text == 'tgt 30.0% (real 27.0% — $2,700.00)'


def test_a_zero_reserve_prints_the_target_ONCE_not_twice():
    """At a 0% reserve the two figures coincide, and "tgt 30.0% (real 30.0%)" makes
    the common case noisier in order to explain the uncommon one."""
    bar = build_label_bars([_view('A', 4_500.0, 30.0)], base_notional=10_000.0,
                           unallocated_pct=0.0)[0]
    assert bar.target_text == 'tgt 30.0% ($3,000.00)'
    assert 'real' not in bar.target_text


def test_the_parenthetical_is_the_DERIVED_of_base_figure_not_the_typed_one():
    """The obvious inversion: printing "tgt 27.0% (real 30.0%)" is the same two
    numbers the wrong way round, and reads as though the reserve INFLATED the
    target."""
    bar = build_label_bars([_view('A', 0.0, 40.0)], base_notional=10_000.0,
                           unallocated_pct=25.0)[0]
    assert bar.target_text == 'tgt 40.0% (real 30.0% — $3,000.00)'


def test_the_delta_is_the_plain_difference_between_two_numbers_on_ONE_scale():
    """50% held against a 30% target is 20 percentage points over -- and $1,800,
    which is 20% of the $9,000 investable pool. Both halves, one denominator."""
    bar = build_label_bars([_view('A', 4_500.0, 30.0)], base_notional=10_000.0,
                           unallocated_pct=10.0)[0]
    assert bar.delta_pct == pytest.approx(20.0)
    assert bar.delta_value == pytest.approx(1_800.0)
    assert bar.delta_text == 'over by 20.0pp ($1,800.00)'


def test_the_delta_MONEY_and_the_delta_POINTS_describe_the_same_gap():
    """delta_pct of the investable pool IS delta_value. If one is computed against
    the gross base and the other against the pool they disagree by the reserve, and
    a reader cannot tell which of the two to act on."""
    for reserve in (0.0, 10.0, 25.0, 90.0):
        bar = build_label_bars([_view('A', 4_500.0, 30.0)], base_notional=10_000.0,
                               unallocated_pct=reserve)[0]
        pool = 10_000.0 * (100.0 - reserve) / 100.0
        assert bar.delta_value == pytest.approx(bar.delta_pct / 100.0 * pool)


def test_a_label_UNDER_its_target_says_under_and_carries_the_same_sign():
    bar = build_label_bars([_view('A', 900.0, 30.0)], base_notional=10_000.0,
                           unallocated_pct=10.0)[0]
    assert bar.delta_pct == pytest.approx(-20.0)
    assert bar.delta_value == pytest.approx(-1_800.0)
    assert bar.delta_text == 'under by 20.0pp ($1,800.00)'


def test_the_delta_direction_cannot_be_inverted_without_a_test_failing():
    """The named mutation: current - target flipped to target - current. The WORD
    and the SIGN both move, so either alone would let it survive."""
    over = build_label_bars([_view('A', 9_000.0, 50.0)], base_notional=10_000.0,
                            unallocated_pct=0.0)[0]
    under = build_label_bars([_view('A', 1_000.0, 50.0)], base_notional=10_000.0,
                             unallocated_pct=0.0)[0]
    assert over.delta_pct > 0 and over.delta_text.startswith('over')
    assert under.delta_pct < 0 and under.delta_text.startswith('under')


def test_a_label_inside_the_tolerance_reads_ON_TARGET_and_not_a_signed_zero():
    """"over by 0.0pp ($0.12)" is noise on a row that is, for every purpose the user
    has, exactly where it should be."""
    bar = build_label_bars([_view('A', 2_500.0, 25.0)], base_notional=10_000.0,
                           unallocated_pct=0.0)[0]
    assert bar.status == LABEL_STATUS_OK
    assert bar.delta_text == 'on target'


def test_the_delta_and_the_status_verdict_cannot_disagree():
    """They are ONE computation: the word in the delta text IS the status. A second
    threshold would let the bar's notch and the sentence beside it tell different
    stories about the same row."""
    for held, target in ((9_000.0, 50.0), (1_000.0, 50.0), (2_500.0, 25.0),
                         (0.0, 0.0), (5_000.0, 49.9)):
        bar = build_label_bars([_view('A', held, target)], base_notional=10_000.0,
                               unallocated_pct=0.0)[0]
        expected = {LABEL_STATUS_OVER: 'over', LABEL_STATUS_UNDER: 'under',
                    LABEL_STATUS_OK: 'on target', LABEL_STATUS_NONE: '—'}[bar.status]
        assert bar.delta_text.startswith(expected), (held, target, bar.delta_text)


def test_an_unconfigured_label_has_no_delta_rather_than_a_zero_one():
    """Nothing held and nothing asked for. "on target" would be a tick beside a
    decision nobody made."""
    bar = build_label_bars([_view('A', 0.0, 0.0)], base_notional=10_000.0,
                           unallocated_pct=0.0)[0]
    assert bar.status == LABEL_STATUS_NONE
    assert bar.delta_pct is None and bar.delta_value is None
    assert bar.delta_text == '—'


def test_a_100_percent_reserve_leaves_no_pool_to_divide_and_says_so():
    """A legitimate setting -- allocate nothing this cycle. There is no investable
    pool, so a share of it is undefined; the row must not print inf, nan or 0.0%."""
    bar = build_label_bars([_view('A', 4_500.0, 30.0)], base_notional=10_000.0,
                           unallocated_pct=100.0)[0]
    assert bar.current_pct is None
    assert bar.current_text == '—'
    assert bar.delta_pct is None
    assert bar.status == LABEL_STATUS_NONE
    assert 'nan' not in bar.target_text.lower() and 'inf' not in bar.target_text.lower()


def test_a_current_share_ABOVE_100_percent_is_kept_honest():
    """A margin book legitimately holds more than the investable pool. The number is
    a true and useful statement, so it is not clamped -- the TRACK stretches to it
    (it is the page maximum) and the bar simply fills."""
    bar = build_label_bars([_view('A', 12_000.0, 50.0)], base_notional=10_000.0,
                           unallocated_pct=10.0)[0]
    assert bar.current_pct == pytest.approx(133.333, abs=0.01)
    assert bar.current_text == '133.3%'
    assert bar.bar_fraction == pytest.approx(1.0)


def test_the_bar_scale_spans_the_investable_basis_figures():
    """Current and target are on ONE scale now, so the track is the largest of them
    on that scale -- not a mixture of an of-base current and a relative target."""
    views = [_view('A', 4_500.0, 30.0), _view('B', 900.0, 80.0)]
    assert bar_scale_pct(views, base_notional=10_000.0, unallocated_pct=10.0) == 80.0


def test_the_bar_and_the_notch_still_share_ONE_scale_on_the_new_basis():
    views = [_view('A', 4_500.0, 30.0), _view('B', 900.0, 80.0)]
    bars = {b.label: b for b in build_label_bars(views, base_notional=10_000.0,
                                                 unallocated_pct=10.0)}
    scale = bar_scale_pct(views, base_notional=10_000.0, unallocated_pct=10.0)
    assert bars['A'].bar_fraction == pytest.approx(50.0 / scale)
    assert bars['A'].notch_fraction == pytest.approx(30.0 / scale)
    assert bars['B'].bar_fraction == pytest.approx(10.0 / scale)
    assert bars['B'].notch_fraction == pytest.approx(80.0 / scale)


# -- the wording the row and its caption carry -------------------------------

def test_the_target_caption_no_longer_claims_a_share_of_the_WHOLE_portfolio():
    """The sentence the user was reading under the input. It is false whenever a
    reserve is set: 15 typed under a 10% reserve is 13.5% of the portfolio."""
    assert 'share of the whole portfolio' not in LABEL_TARGET_CAPTION
    assert 'investable' in LABEL_TARGET_CAPTION.lower()


def test_the_target_caption_keeps_the_sentence_that_was_right():
    """The second half explained the OTHER denominator and was the useful part."""
    assert 'Share of label %' in LABEL_TARGET_CAPTION
    assert 'different denominator' in LABEL_TARGET_CAPTION


def test_the_basis_legend_names_BOTH_denominators_once_for_the_whole_page():
    """The row is terse ("tgt 15.0% (real 13.5%)") precisely because the legend
    carries the explanation -- said once, not on eight rows."""
    assert 'investable' in BASIS_LEGEND.lower()
    assert 'base' in BASIS_LEGEND.lower()
    assert 'real' in BASIS_LEGEND.lower()


def test_the_reserve_row_is_marked_as_the_one_row_on_the_OTHER_denominator():
    """It IS the part held back, so it cannot be restated against what it leaves
    without becoming circular. A reader has to be able to tell."""
    assert 'base' in RESERVE_BASIS_NOTE.lower()
    assert 'label' in RESERVE_BASIS_NOTE.lower()


def test_the_reserve_row_itself_still_divides_the_GROSS_base():
    """The named mutation: converting this row to the investable basis. 10% of a
    $10,000 base is $1,000 held back, whatever the labels below are measured on."""
    text = format_reserve_row(base_notional=10_000.0, available_buying_power=500.0,
                              unallocated_pct=10.0)
    assert '10.00% of base' in text
    assert '$1,000.00' in text


# -- the header line and the tooltip -----------------------------------------

def test_the_header_states_the_holding_on_the_investable_basis():
    text = format_label_header(label='ARK26', current_value=4_500.0, target_pct=30.0,
                               pct_of_investable=50.0, pct_of_total=100.0,
                               delta_text='over by 20.0pp ($1,800.00)',
                               unallocated_pct=10.0)
    assert text == ('ARK26 — $4,500.00 (50.0% of investable, target 30.0% '
                    '(real 27.0%) — over by 20.0pp ($1,800.00))')


def test_the_header_carries_the_delta_so_the_collapsed_row_still_says_what_to_do():
    """The header is the expansion's own caption -- what a screen reader reads and
    what a collapsed section shows. Dropping the delta there would make it the one
    place the actionable number is missing."""
    text = format_label_header(label='A', current_value=1.0, target_pct=30.0,
                               pct_of_investable=0.01, pct_of_total=1.0,
                               delta_text='under by 30.0pp ($2,700.00)',
                               unallocated_pct=10.0)
    assert 'under by 30.0pp ($2,700.00)' in text


def test_the_tooltip_no_longer_repeats_the_of_base_figure_the_row_now_prints():
    """One fact, one place. The row carries "(real 27.0%)"; the tooltip carries what
    the row cannot -- the money, and which denominator the table below uses."""
    tip = format_label_target_tooltip(target_pct=30.0, base_notional=10_000.0,
                                      unallocated_pct=10.0)
    assert '$2,700.00' in tip
    assert 'share of the label' in tip.lower()
    assert 'of the base' not in tip


# ---------------------------------------------------------------------------
# THE COLOUR PICKER: seven presets, plus a custom colour the user asked for
#
# The palette argument still holds and is still rendered in the dialog -- on a
# near-black surface an arbitrary colour is one you cannot read back, and the seven
# Okabe & Ito hues stay distinguishable under the common forms of colour blindness.
# The user read that argument and asked for a picker anyway ("Make a color picker
# then"), so a custom colour is accepted and its contrast is WARNED about rather
# than forbidden. It is their UI.
#
# What is NOT relaxed is the parse. The value is interpolated into a CSS ``style``
# attribute, so "render whatever the database says" is an injection: a custom colour
# is a strict ``#rrggbb`` and nothing else, on the way in AND on the way out.
# ---------------------------------------------------------------------------

def test_a_custom_six_digit_hex_is_accepted_and_canonicalised_to_upper_case():
    assert normalise_label_color('#a1b2c3') == '#A1B2C3'
    assert normalise_label_color('  #A1B2C3  ') == '#A1B2C3'


def test_the_seven_presets_still_normalise_to_their_published_spelling():
    for _name, hex_value in LABEL_COLOR_PALETTE:
        assert normalise_label_color(hex_value.lower()) == hex_value


@pytest.mark.parametrize('raw', [
    '#abc',                     # 3-digit shorthand: a colour, but not the one form
    '#a1b2c3d4',                # 8-digit with alpha
    '#a1b2c',                   # 5 digits
    'a1b2c3',                   # no hash
    'red',                      # a named colour
    'rgb(255,0,0)',             # a function
    '#GGGGGG',                  # not hex
    '#a1b2c3;background:url(x)',   # the injection this parse exists to refuse
    'url(javascript:alert(1))',
    '#a1b2c3 !important',
])
def test_anything_that_is_not_a_strict_six_digit_hex_is_REFUSED(raw):
    with pytest.raises(ValueError):
        normalise_label_color(raw)


@pytest.mark.parametrize('raw', [
    '#a1b2c3d4', 'red', 'rgb(255,0,0)', '#GGGGGG', '#a1b2c3;background:url(x)',
    '#abc', 'a1b2c3', '#a1b2c3 !important',
])
def test_the_RENDER_path_refuses_the_same_strings_by_falling_back_to_grey(raw):
    """Asymmetric on purpose: a row hand-edited in sqlite must not take the page
    down, and it must not reach the ``style`` attribute either."""
    assert resolve_label_icon_color(raw) == DEFAULT_LABEL_ICON_COLOR


def test_the_render_path_accepts_a_stored_custom_colour():
    assert resolve_label_icon_color('#a1b2c3') == '#A1B2C3'


def test_everything_the_renderer_emits_is_a_hash_and_six_hex_digits():
    """The property that makes the CSS interpolation safe, checked over the whole
    input space the store can hold rather than over a list of examples."""
    import re as _re
    for raw in [None, '', '   ', 'red', '#abc', '#A1B2C3', '#a1b2c3', '#0072b2',
                '#a1b2c3;x:y', 'rgb(0,0,0)', 0, 1.5, '#GGGGGG', '#' + 'a' * 100]:
        assert _re.fullmatch(r'#[0-9A-F]{6}', resolve_label_icon_color(raw)), raw


def test_no_colour_still_means_no_colour_and_is_not_black():
    assert normalise_label_color(NO_LABEL_COLOR) is None
    assert normalise_label_color(None) is None
    assert store_color_value(NO_LABEL_COLOR) == ''
    assert normalise_label_color('#000000') == '#000000'   # black is a CHOICE


def test_a_custom_colour_travels_the_same_write_path_as_a_preset():
    assert store_color_value('#a1b2c3') == '#A1B2C3'


# -- contrast: warn, never block ---------------------------------------------

def test_the_contrast_ratio_is_wcags_own_arithmetic():
    """White on black is 21:1 and a colour against itself is 1:1. Those two pin the
    formula; anything else is a table nobody can check by eye."""
    assert contrast_ratio('#FFFFFF', '#000000') == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio('#000000', '#FFFFFF') == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio('#777777', '#777777') == pytest.approx(1.0, abs=0.001)


def test_every_palette_colour_clears_the_graphical_object_floor():
    """WCAG 1.4.11 asks 3:1 for a graphical object. The palette was chosen against
    exactly this threshold, and this is what stops a "nicer" hex being substituted
    into the tuple without anyone noticing it fell below it."""
    for name, hex_value in LABEL_COLOR_PALETTE:
        assert contrast_ratio(hex_value, SURFACE_COLOR) >= MIN_GRAPHICAL_CONTRAST, name


def test_a_dark_custom_colour_is_WARNED_about_and_still_returned():
    """Warn, do not block. The warning names the ratio and the floor, because
    "poor contrast" alone leaves the user unable to tell how far off they are."""
    warning = label_color_contrast_warning('#101010')
    assert warning is not None
    assert '3' in warning
    assert normalise_label_color('#101010') == '#101010'    # still accepted


def test_a_readable_custom_colour_draws_no_warning():
    assert label_color_contrast_warning('#FFFFFF') is None
    assert label_color_contrast_warning('#E69F00') is None


def test_no_colour_and_an_unparseable_one_draw_no_contrast_warning():
    """There is nothing to measure. The parse refusal is a different message and it
    is the one that should be shown."""
    assert label_color_contrast_warning(None) is None
    assert label_color_contrast_warning('') is None
    assert label_color_contrast_warning('not a colour') is None


def test_the_contrast_floor_is_the_one_the_palette_was_chosen_against():
    """Both numbers pinned, so moving either is a deliberate act."""
    assert MIN_GRAPHICAL_CONTRAST == 3.0
    assert SURFACE_COLOR == '#1A1F2E'


# ---------------------------------------------------------------------------
# GAPS FOUND BY THE MUTATION RUN (99 mutations, 11 survivors)
#
# Each of these pins a rule that a surviving mutant was free to break. They are
# grouped here rather than beside their siblings above because what they have in
# common is how they were found.
# ---------------------------------------------------------------------------

def test_fill_100_leaves_a_set_the_ENGINE_already_accepts_alone():
    """Survivor: ``abs(total - 100) <= tolerance`` weakened to ``== 100``.

    ``validate_symbol_weights`` accepts 100.01 and 99.99 -- a 2dp split of three
    ways genuinely produces them -- so a button that "fixed" such a set would move
    the user's numbers to achieve nothing. The band is the engine's, not a taste.
    """
    over = fill_label_to_100('L', {'A': 50.0, 'B': 50.01})
    under = fill_label_to_100('L', {'A': 50.0, 'B': 49.99})
    assert over.reason_code == FILL_ALREADY_100
    assert under.reason_code == FILL_ALREADY_100
    assert over.weights == {'A': 50.0, 'B': 50.01}
    assert under.weights == {'A': 50.0, 'B': 49.99}
    # ...and one hundredth further out IS acted on, so the band is a band.
    assert fill_label_to_100('L', {'A': 50.0, 'B': 50.02}).reason_code == FILL_SCALED_DOWN


def test_fill_100_hands_the_splitter_a_CLEAN_two_decimal_remainder():
    """Survivor: ``remainder = round(100 - total, 2)`` with the round dropped.

    Binary subtraction leaves dust below the cent, and ``split_pct_across`` FLOORS:
    fed 91.71000000000001 it returns three exact 30.57s, fed 91.71 it returns the
    documented 30.56 / 30.56 / 30.59. Both total 100, so no sum check can catch it
    -- what changes is WHICH slot carries the residual, i.e. whether this button
    and the wizard's "Fill rest" agree on the same label. 2,524 totals diverge.
    """
    result = fill_label_to_100('L', {'A': 8.29, 'B': 0.0, 'C': 0.0, 'D': 0.0})
    assert result.weights == {'A': 8.29, 'B': 30.56, 'C': 30.56, 'D': 30.59}


def test_fill_100_rounds_a_MORE_THAN_TWO_DECIMAL_weight_onto_the_cent_grid():
    """Survivor: the ``round(..., 2)`` on the way in.

    ``get_symbol_weights`` resolves an unstored symbol to FOUR decimals
    (``build_symbol_targets`` rounds a scaled leftover to 4dp), so the live map the
    button reads genuinely holds them. Without the round the untouched weights are
    emitted at 4dp and the shortfall is computed at 2dp, so the set misses 100 --
    the exact "99.99 instead of 100" defect, arrived at from the other side.
    """
    from decimal import Decimal
    result = fill_label_to_100('L', {'A': 33.3333, 'B': 0.0})
    assert result.weights == {'A': 33.33, 'B': 66.67}
    assert sum(Decimal(str(v)) for v in result.weights.values()) == Decimal('100')


def test_the_delta_sentence_follows_the_STATUS_and_not_the_sign():
    """Survivor: ``LABEL_DELTA_OVER_FMT if delta_pct > 0``.

    Equivalent for everything ``build_label_bars`` produces today, and that is the
    trap: the two would part company the moment any caller applied the tolerance
    band differently, and the whole reason this reads the status is that the band
    is applied ONCE. Called directly with a mismatched pair, the status wins.
    """
    assert format_label_delta(status=LABEL_STATUS_OVER, delta_pct=-5.0,
                              delta_value=-100.0) == 'over by 5.0pp ($100.00)'
    assert format_label_delta(status=LABEL_STATUS_UNDER, delta_pct=5.0,
                              delta_value=100.0) == 'under by 5.0pp ($100.00)'


def test_the_bar_colour_goes_through_the_RESOLVER_and_not_the_stored_string():
    """Survivor: ``color=(view.color or DEFAULT)``.

    Identical for a palette hex stored in canonical case -- and different for a
    lower-cased one (two spellings of one colour) and for a hand-edited row, which
    would then reach a CSS ``style`` attribute unparsed.
    """
    assert build_label_bars([_view('A', 100.0, 10.0, color='#e69f00')],
                            base_notional=10_000.0, unallocated_pct=0.0)[0].color \
        == '#E69F00'
    assert build_label_bars([_view('A', 100.0, 10.0, color='red;content:"x"')],
                            base_notional=10_000.0, unallocated_pct=0.0)[0].color \
        == DEFAULT_LABEL_ICON_COLOR


def test_relative_luminance_uses_WCAGS_channel_WEIGHTS():
    """Survivor: ``sum(channels) / 3``.

    A plain average agrees with the weighted formula on greys and on white/black,
    so every ratio test above passed under it. The primaries are where it cannot
    hide: WCAG weights blue at 0.0722 and green at 0.7152, a factor of ten apart,
    and an average calls them both 1/3.
    """
    assert relative_luminance('#FF0000') == pytest.approx(0.2126, abs=1e-6)
    assert relative_luminance('#00FF00') == pytest.approx(0.7152, abs=1e-6)
    assert relative_luminance('#0000FF') == pytest.approx(0.0722, abs=1e-6)


def test_a_saturated_blue_is_WARNED_about_where_an_average_would_wave_it_through():
    """The same survivor, at the level that matters: a colour the user can pick.

    ``#0000CC`` is 1.46:1 against this surface and unreadable; the average makes it
    3.74:1 and silent. Blue is exactly where the two rules diverge, and blue is
    exactly what someone reaches for.
    """
    assert contrast_ratio('#0000CC', SURFACE_COLOR) < MIN_GRAPHICAL_CONTRAST
    assert label_color_contrast_warning('#0000CC') is not None


# -- round two of the mutation run -------------------------------------------

def test_a_label_holding_money_with_NO_target_reads_over_not_a_dash():
    """Survivor: the unconfigured check weakened to "the target is 0".

    A label you have money in and have set no target for is the single most
    actionable row on the page -- the plan will sell all of it -- and reporting it
    as "nothing has been asked of this" is the inverse of what the user needs. The
    dash is for a label that is BOTH empty and untargeted.
    """
    bar = build_label_bars([_view('A', 2_500.0, 0.0)], base_notional=10_000.0,
                           unallocated_pct=0.0)[0]
    assert bar.status == LABEL_STATUS_OVER
    assert bar.delta_text == 'over by 25.0pp ($2,500.00)'


def test_the_delta_MONEY_can_be_written_either_way_and_lands_on_one_number():
    """Not a gap -- a PROOF, and the reason a whole class of mutation is harmless.

    ``current - pool x target/100`` and ``current - base x effective/100`` are the
    same quantity, because ``effective`` IS ``target`` scaled by the reserve. If
    they ever stopped agreeing, one of the two denominators would have moved.
    """
    for reserve in (0.0, 10.0, 25.0, 90.0):
        bar = build_label_bars([_view('A', 4_500.0, 30.0)], base_notional=10_000.0,
                               unallocated_pct=reserve)[0]
        via_base = 4_500.0 - 10_000.0 * bar.effective_pct / 100.0
        assert bar.delta_value == pytest.approx(via_base)


# ---------------------------------------------------------------------------
# THE `last` GENERATION AND THE UNREALISED P&L, ON THE PAGE
#
# Both were trapped in the Allocate wizard's step 1 / step 2 captions. They are
# facts about a LABEL and a SYMBOL, not about a run, so they belong on the screen
# the user reads them from -- and once the target boxes moved onto the page, the
# wizard was the only place left that could answer "what did I have here before".
#
# The wizard's own captions are NOT ported. They said "% of base" (see
# ``LABEL_CURRENT_FMT`` in the wizard module), which is the denominator the page's
# 2026-08-25 rework deliberately demoted to a parenthetical.
# ---------------------------------------------------------------------------

def test_a_label_view_carries_the_target_the_last_run_used():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import build_label_views

    views = build_label_views(
        [ManagedLabel('ARK26', 40.0, previous_target_pct=25.0)],
        {'ARK26': ['AAPL']}, {'AAPL': _pos('AAPL', 10, 1000.0)}, {'AAPL': 250.0},
        valuation_mode=VALUATION_MODE_MARKET, base_notional=10_000.0)

    assert views[0].previous_target_pct == 25.0


def test_a_label_that_has_never_run_carries_None_and_not_a_zero():
    """"never allocated" and "last time this got nothing" are different facts, and
    0.0 is a legitimate value of the second."""
    views = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                              {}, {}, valuation_mode=VALUATION_MODE_MARKET)
    assert views[0].previous_target_pct is None


def test_a_symbol_row_carries_the_weight_the_last_run_used():
    views = build_label_views(
        [ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL', 'MSFT']},
        {'AAPL': _pos('AAPL', 10, 1000.0)}, {'AAPL': 250.0, 'MSFT': 100.0},
        valuation_mode=VALUATION_MODE_MARKET, base_notional=10_000.0,
        symbol_weights={'ARK26': {'AAPL': 75.0, 'MSFT': 25.0}},
        symbol_previous_weights={'ARK26': {'AAPL': 60.0}})

    by_symbol = {r.symbol: r for r in views[0].rows}
    assert by_symbol['AAPL'].previous_weight_pct == 60.0
    assert by_symbol['MSFT'].previous_weight_pct is None


def test_a_symbol_row_carries_its_unrealised_pnl_in_money_and_percent():
    """$2,500 of AAPL bought for $1,000 is +$1,500, i.e. +150%."""
    views = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)}, {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0)
    pnl = views[0].rows[0].pnl
    assert pnl.amount == pytest.approx(1_500.0)
    assert pnl.pct == pytest.approx(150.0)


def test_the_pnl_is_the_SAME_in_cost_mode_as_in_market_mode():
    """The defect this guards: in COST valuation ``current_value`` IS the cost
    basis, so a P&L derived from it reads 0.00 on every row of the account's
    default mode. ``unrealised_pnl`` takes no valuation mode for that reason."""
    args = ([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
            {'AAPL': _pos('AAPL', 10, 1000.0)}, {'AAPL': 250.0})
    cost = build_label_views(*args, valuation_mode=VALUATION_MODE_COST)
    market = build_label_views(*args, valuation_mode=VALUATION_MODE_MARKET)

    assert cost[0].rows[0].pnl.amount == market[0].rows[0].pnl.amount == 1_500.0


def test_a_label_view_carries_the_TOTAL_pnl_of_its_symbols():
    """ONE call over the whole membership, so the percentage is money-weighted --
    averaging the symbols' own percentages would weight a $1,000 holding as
    heavily as the $90,000 beside it."""
    views = build_label_views([ManagedLabel('ARK26', 40.0)],
                              {'ARK26': ['AAPL', 'MSFT']},
                              {'AAPL': _pos('AAPL', 10, 1000.0),
                               'MSFT': _pos('MSFT', 1, 90_000.0)},
                              {'AAPL': 250.0, 'MSFT': 90_000.0},
                              valuation_mode=VALUATION_MODE_MARKET)
    pnl = views[0].pnl
    assert pnl.amount == pytest.approx(1_500.0)
    assert pnl.pct == pytest.approx(1_500.0 / 91_000.0 * 100.0)


def test_an_unpriced_holding_is_excluded_from_the_pnl_rather_than_valued_at_zero():
    views = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)}, {'AAPL': None},
                              valuation_mode=VALUATION_MODE_MARKET)
    pnl = views[0].rows[0].pnl
    assert pnl.amount is None
    assert pnl.unpriced == 1


def test_the_last_figure_reads_as_a_dash_when_there_is_no_last():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        NO_PREVIOUS_MARK, format_last_pct)

    assert format_last_pct(None) == NO_PREVIOUS_MARK
    assert format_last_pct(0.0) == '0.00%'
    assert format_last_pct(60.0) == '60.00%'


def test_the_last_caption_names_itself_so_a_bare_percentage_cannot_be_misread():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import format_last_target

    assert format_last_target(60.0) == 'last 60.00%'
    assert format_last_target(None) == 'last -'


def test_the_pnl_caption_carries_the_engines_own_wording_and_nothing_else():
    from ba2_trade_platform.core.portfolio_allocation import unrealised_pnl
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import format_pnl_caption

    state = _pos('AAPL', 10, 1000.0)
    state.price = 250.0
    assert format_pnl_caption(unrealised_pnl([state])) == 'P&L +1,500.00 (+150.00%)'


def test_the_pnl_colour_is_an_accent_and_never_the_message():
    """Grey covers three different things on purpose -- nothing measurable,
    nothing held, and a genuine flat 0.00 -- because none of them is a verdict,
    and painting "break-even" red is inventing one.

    Both directions are asserted on every case: a survivor of the mutation run
    dropped the epsilon band, which turned an exact 0.00 red while still keeping
    "green" out of the string.
    """
    from ba2_trade_platform.core.portfolio_allocation import MONEY_EPSILON, UnrealisedPnL
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import pnl_classes

    assert 'text-green-500' in pnl_classes(UnrealisedPnL(amount=1.0))
    assert 'text-red-500' in pnl_classes(UnrealisedPnL(amount=-1.0))
    for neutral in (UnrealisedPnL(amount=0.0),
                    UnrealisedPnL(amount=MONEY_EPSILON / 2.0),
                    UnrealisedPnL(amount=-MONEY_EPSILON / 2.0),
                    UnrealisedPnL(amount=None),
                    None):
        classes = pnl_classes(neutral)
        assert 'green' not in classes and 'red' not in classes, neutral


def test_the_label_bar_states_the_last_target_and_the_pnl_beside_the_delta():
    """One writer for the row: the bar. The header, the notch, the delta, the last
    generation and the P&L all come out of ``build_label_bars`` together, so no
    two of them can describe different states of the same label."""
    view = LabelView(label='A', target_pct=30.0, current_value=4_500.0,
                     pct_of_total=0.0, previous_target_pct=25.0)
    bar = build_label_bars([view], base_notional=10_000.0, unallocated_pct=0.0)[0]

    assert bar.last_text == 'last 25.00%'
    assert bar.pnl_text == 'P&L -'


def test_the_label_bars_last_text_is_a_dash_when_the_label_has_no_history():
    bar = build_label_bars([_view('A', 4_500.0, 30.0)], base_notional=10_000.0,
                           unallocated_pct=0.0)[0]
    assert bar.last_text == 'last -'


# ---------------------------------------------------------------------------
# THE MIGRATED BUTTON GROUPS -- the DECISION half
#
# The Allocate wizard's step 1 carried "Even split" and "Load last" over the
# LABEL targets; its step 2 carried "Even split", "Fill rest", "Load last" and
# "Wipe" over one label's symbol weights. All six move onto the page, and the
# arithmetic stays exactly where it was -- in the ENGINE
# (``even_split_targets``, ``load_previous_targets``, ``even_split_symbol_weights``,
# ``fill_remaining_symbol_weights``, ``load_previous_symbol_weights``,
# ``wipe_symbol_weights``). These wrappers exist to translate between the page's
# plain ``{name: pct}`` maps and the engine's ``LabelTarget``, and to decide
# whether anything actually changed; they do no arithmetic of their own.
# ---------------------------------------------------------------------------

def _pure():
    from ba2_trade_platform.ui.utils import portfolio_allocation_view as v
    return v


def test_an_even_split_of_the_labels_uses_the_engines_own_splitter():
    """Three ways is 33.33 / 33.33 / 33.34 -- the remainder on the LAST label, so
    the set totals exactly 100. A hand-rolled ``round(100/n, 2)`` agrees at n=3
    and parts company at n=6, producing a set the submit gate refuses."""
    result = _pure().even_split_label_targets({'A': 10.0, 'B': 20.0, 'C': 0.0})

    assert result.changed is True
    assert result.targets == {'A': 33.33, 'B': 33.33, 'C': 33.34}
    assert sum(result.targets.values()) == 100.0


def test_an_even_split_of_an_already_even_set_writes_NOTHING_and_says_so():
    """A button that silently does nothing when pressed is indistinguishable from
    a broken one."""
    result = _pure().even_split_label_targets({'A': 50.0, 'B': 50.0})

    assert result.changed is False
    assert result.targets == {'A': 50.0, 'B': 50.0}
    assert result.message


def test_an_even_split_with_no_labels_at_all_is_refused_rather_than_dividing_by_zero():
    result = _pure().even_split_label_targets({})

    assert result.changed is False
    assert result.reason_code == _pure().TARGETS_NO_LABELS
    assert result.targets == {}


def test_an_even_split_always_splits_the_WHOLE_hundred_whatever_the_reserve():
    """The reserve is its own stored field and the labels divide what it leaves,
    so any total but 100 here would produce a set the validator refuses."""
    result = _pure().even_split_label_targets({'A': 0.0, 'B': 0.0})
    assert result.targets == {'A': 50.0, 'B': 50.0}


def test_load_last_restores_the_targets_the_previous_run_used():
    result = _pure().load_last_label_targets({'A': 70.0, 'B': 30.0},
                                             {'A': 60.0, 'B': 40.0})

    assert result.changed is True
    assert result.targets == {'A': 60.0, 'B': 40.0}


def test_load_last_reads_the_PREVIOUS_generation_and_never_the_current_one():
    """The mutation this exists for: swapping ``previous`` for the live map turns
    Load last into a no-op that still reports success, and the user believes their
    last allocation has been restored when nothing moved."""
    result = _pure().load_last_label_targets({'A': 70.0, 'B': 30.0},
                                             {'A': 10.0, 'B': 90.0})
    assert result.targets == {'A': 10.0, 'B': 90.0}


def test_a_label_with_no_history_keeps_the_target_it_already_has():
    """A partial history is the ORDINARY state -- a label added yesterday has none
    -- and zeroing those would silently unallocate a real basket."""
    result = _pure().load_last_label_targets({'A': 70.0, 'B': 30.0},
                                             {'A': 60.0, 'B': None})

    assert result.targets == {'A': 60.0, 'B': 30.0}


def test_a_previous_target_of_zero_is_restored_and_not_read_as_no_history():
    """0.0 is a real prior state -- the engine reads it as "hold none of this" --
    and refusing to restore it would be refusing to undo the user's last change."""
    result = _pure().load_last_label_targets({'A': 70.0}, {'A': 0.0})

    assert result.changed is True
    assert result.targets == {'A': 0.0}


def test_load_last_with_no_history_anywhere_writes_nothing_and_says_why():
    result = _pure().load_last_label_targets({'A': 70.0, 'B': 30.0},
                                             {'A': None, 'B': None})

    assert result.changed is False
    assert result.reason_code == _pure().TARGETS_NO_PREVIOUS
    assert result.targets == {'A': 70.0, 'B': 30.0}
    assert result.message


def test_load_last_that_would_change_nothing_reports_it_rather_than_writing():
    result = _pure().load_last_label_targets({'A': 60.0}, {'A': 60.0})

    assert result.changed is False
    assert result.reason_code == _pure().TARGETS_UNCHANGED


def test_the_label_target_helpers_preserve_the_order_they_were_given():
    """The page hands these its DISPLAY order and writes the result straight back
    onto the rows; a dict that came back re-sorted would move the remainder onto a
    different label from the one the user is looking at."""
    result = _pure().even_split_label_targets({'Z': 0.0, 'A': 0.0, 'M': 0.0})
    assert list(result.targets) == ['Z', 'A', 'M']
    assert result.targets['M'] == 33.34


# -- the PER-LABEL group: Even split / Fill rest / Load last / Wipe -----------

def test_an_even_split_of_a_labels_symbols_uses_the_engines_own_splitter():
    result = _pure().even_split_symbol_shares('ARK26',
                                              {'AAPL': 90.0, 'MSFT': 10.0, 'TSLA': 0.0})

    assert result.changed is True
    assert result.weights == {'AAPL': 33.33, 'MSFT': 33.33, 'TSLA': 33.34}


def test_an_even_split_of_a_label_that_is_already_even_writes_nothing():
    result = _pure().even_split_symbol_shares('ARK26', {'AAPL': 50.0, 'MSFT': 50.0})

    assert result.changed is False
    assert result.message


def test_an_even_split_of_a_label_with_no_symbols_is_refused():
    result = _pure().even_split_symbol_shares('EMPTY', {})

    assert result.changed is False
    assert result.reason_code == _pure().WEIGHTS_NO_SYMBOLS


def test_fill_rest_shares_the_remainder_between_the_EMPTY_slots_only():
    """"Type the two you care about, let the rest sort themselves out". Every
    non-zero weight is left EXACTLY as typed -- not re-normalised, not nudged."""
    result = _pure().fill_rest_symbol_shares('ARK26', {'AAPL': 30.0, 'MSFT': 0.0,
                                                       'TSLA': 0.0})

    assert result.changed is True
    assert result.weights == {'AAPL': 30.0, 'MSFT': 35.0, 'TSLA': 35.0}


def test_fill_rest_is_NOT_the_same_button_as_fill_100_and_never_scales():
    """``Fill 100%`` scales an over-allocated label down; ``Fill rest`` refuses.

    That is the whole distinction between the two, and it is why both are on the
    row: one repairs a set, the other only ever fills the gaps in one.
    """
    over = {'AAPL': 80.0, 'MSFT': 80.0, 'TSLA': 0.0}
    assert _pure().fill_rest_symbol_shares('ARK26', over).changed is False
    assert _pure().fill_label_to_100('ARK26', over).changed is True


def test_fill_rest_with_no_empty_slot_writes_nothing_and_says_so():
    result = _pure().fill_rest_symbol_shares('ARK26', {'AAPL': 60.0, 'MSFT': 40.0})

    assert result.changed is False
    assert result.reason_code == _pure().WEIGHTS_NOTHING_TO_FILL
    assert result.message


def test_filling_a_completely_empty_label_lands_exactly_on_the_even_split():
    """Not a coincidence to be re-checked: ``fill_remaining_symbol_weights`` is
    ``split_pct_across`` is ``even_split_pct``, called with a total of 100."""
    empty = {'A': 0.0, 'B': 0.0, 'C': 0.0, 'D': 0.0, 'E': 0.0, 'F': 0.0}
    assert (_pure().fill_rest_symbol_shares('L', empty).weights
            == _pure().even_split_symbol_shares('L', empty).weights)


def test_load_last_restores_one_labels_symbol_weights():
    result = _pure().load_last_symbol_shares('ARK26', {'AAPL': 60.0, 'MSFT': 40.0},
                                             {'AAPL': 50.0, 'MSFT': 50.0})

    assert result.changed is True
    assert result.weights == {'AAPL': 50.0, 'MSFT': 50.0}


def test_load_last_for_symbols_reads_the_PREVIOUS_generation_and_not_the_current():
    result = _pure().load_last_symbol_shares('ARK26', {'AAPL': 60.0, 'MSFT': 40.0},
                                             {'AAPL': 10.0, 'MSFT': 90.0})
    assert result.weights == {'AAPL': 10.0, 'MSFT': 90.0}


def test_a_symbol_with_no_history_keeps_the_weight_it_already_has():
    result = _pure().load_last_symbol_shares('ARK26', {'AAPL': 60.0, 'MSFT': 40.0},
                                             {'AAPL': 50.0, 'MSFT': None})

    assert result.weights == {'AAPL': 50.0, 'MSFT': 40.0}


def test_load_last_for_a_label_with_no_history_at_all_writes_nothing():
    result = _pure().load_last_symbol_shares('ARK26', {'AAPL': 60.0},
                                             {'AAPL': None})

    assert result.changed is False
    assert result.reason_code == _pure().WEIGHTS_NO_PREVIOUS


def test_wipe_clears_every_weight_in_the_label_to_zero():
    """0.0, never ``None``: every solver does arithmetic on ``weight_pct``, and 0.0
    IS "empty" in this model -- which is what makes Fill rest coherent."""
    result = _pure().wipe_symbol_shares('ARK26', {'AAPL': 60.0, 'MSFT': 40.0})

    assert result.changed is True
    assert result.weights == {'AAPL': 0.0, 'MSFT': 0.0}


def test_wipe_reports_a_label_that_is_already_clear_rather_than_rewriting_it():
    result = _pure().wipe_symbol_shares('ARK26', {'AAPL': 0.0, 'MSFT': 0.0})

    assert result.changed is False
    assert result.reason_code == _pure().WEIGHTS_ALREADY_CLEAR


def test_wipe_is_available_on_exactly_the_set_fill_rest_refuses():
    """The user is never cornered: an over-allocated label disables the fill and
    Wipe is always the way out of it."""
    over = {'AAPL': 80.0, 'MSFT': 80.0, 'TSLA': 0.0}
    assert _pure().fill_rest_symbol_shares('L', over).changed is False
    assert _pure().wipe_symbol_shares('L', over).changed is True


def test_the_symbol_share_helpers_preserve_the_order_they_were_given():
    result = _pure().even_split_symbol_shares('L', {'Z': 0.0, 'A': 0.0, 'M': 0.0})
    assert list(result.weights) == ['Z', 'A', 'M']
    assert result.weights['M'] == 33.34


# ---------------------------------------------------------------------------
# THE COMMA. A percentage box is 0-100, so a comma in it is a DECIMAL POINT.
#
# Reported off the live screen: the share cells render "11,11". The old parse
# stripped every comma as a thousands separator, which turns a decimal comma into
# a number a hundred times too big -- and the range check only catches HALF of
# those. "11,11" becomes 1111 and is refused (visible, annoying); "0,5" becomes 5
# and is ACCEPTED (silent, and ten times what was typed). The second is the one
# that costs money.
#
# No legitimate value in a 0-100 box needs a thousands separator, so a lone comma
# with no decimal point is read as the decimal point. A comma ALONGSIDE a dot
# keeps its old meaning -- "1,234.5" is unambiguous in every locale that writes
# it that way -- and is refused by the range check on its own merits.
# ---------------------------------------------------------------------------

def test_a_lone_comma_in_a_percentage_box_is_a_DECIMAL_point():
    assert parse_pct('11,11').value == pytest.approx(11.11)
    assert parse_pct('0,5').value == pytest.approx(0.5)
    assert parse_pct(' 26,78 % ').value == pytest.approx(26.78)


def test_the_silent_half_of_the_comma_bug_is_the_one_that_was_accepted():
    """"0,5" used to parse as 5.0 -- in range, so nothing complained, and the
    symbol got ten times the share the user typed."""
    assert parse_pct('0,5').value != 5.0


def test_a_comma_beside_a_decimal_point_keeps_its_thousands_meaning():
    """Unambiguous, and out of range on its own merits rather than by accident."""
    assert parse_pct('1,234.5').value == pytest.approx(1234.5)


def test_two_commas_are_a_thousands_grouping_and_not_two_decimal_points():
    assert parse_pct('1,234,567').value == pytest.approx(1234567.0)


def test_a_comma_typed_into_a_symbol_share_box_survives_the_round_trip():
    """The whole point: the value the user typed is the value that is stored."""
    edit = validate_symbol_weight_edit(label='ARK26', symbol='AAPL', raw='26,78')
    assert edit.accepted is True
    assert edit.value == pytest.approx(26.78)


def test_a_comma_typed_into_a_label_target_box_survives_it_too():
    edit = validate_label_target_edit(label='ARK26', raw='13,5', other_targets={})
    assert edit.accepted is True
    assert edit.value == pytest.approx(13.5)


def test_a_comma_typed_into_the_reserve_box_survives_it_too():
    assert validate_reserve_edit('7,25').value == pytest.approx(7.25)


def test_a_comma_value_that_is_genuinely_out_of_range_is_still_refused():
    """The comma fix must not become a way past the 0-100 guard."""
    edit = validate_symbol_weight_edit(label='ARK26', symbol='AAPL', raw='1,234.5')
    assert edit.accepted is False
    assert edit.reason_code == EDIT_OVER_100


# ---------------------------------------------------------------------------
# THE LABEL-TOTAL CARD
#
# It was a sentence under the stat cards that appeared only when the set was
# WRONG -- so the one moment the user wanted the running total (while typing
# towards 100) was the one moment it said nothing. As a card it always carries
# the figure, and the verdict rides along as its detail.
#
# The verdict is the SAME one ``format_label_total_notice`` and
# ``format_allocation_footer`` reach, from one shared predicate, so three
# readouts of one set cannot disagree.
# ---------------------------------------------------------------------------

def test_the_label_total_readout_always_carries_the_figure_even_when_it_is_right():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        LABEL_TOTAL_TITLE, label_total_readout)

    card = label_total_readout({'A': 60.0, 'B': 40.0})

    assert card.title == LABEL_TOTAL_TITLE
    assert card.text == '100.00%'
    assert card.severity == 'ok'
    assert card.detail


def test_the_label_total_readout_sums_and_never_averages():
    """The mutation this exists for. Three labels at 25 total 75, not 25."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import label_total_readout

    assert label_total_readout({'A': 25.0, 'B': 25.0, 'C': 25.0}).text == '75.00%'


def test_the_label_total_readout_speaks_the_engines_own_shortfall_sentence():
    """Verbatim, so the card, the footer and the submit gate describe one defect
    one way -- and so the "use the Unallocated box" guidance survives the move."""
    from ba2_trade_platform.core.portfolio_allocation import ERROR_LABEL_UNDER_FMT
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import label_total_readout

    card = label_total_readout({'A': 75.0})

    assert card.text == '75.00%'
    assert card.severity == 'warning'
    assert card.detail == ERROR_LABEL_UNDER_FMT.format(total=75.0, under=25.0)
    assert 'Unallocated box' in card.detail


def test_the_label_total_readout_speaks_the_engines_own_over_sentence_too():
    from ba2_trade_platform.core.portfolio_allocation import ERROR_LABEL_TOTAL_FMT
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import label_total_readout

    card = label_total_readout({'A': 70.0, 'B': 48.0})

    assert card.text == '118.00%'
    assert card.severity == 'negative'
    assert card.detail == ERROR_LABEL_TOTAL_FMT.format(total=118.0, over=18.0)


def test_the_label_total_readout_agrees_with_the_notice_and_the_footer():
    """One predicate, three readouts. A card that called 118% 'ok' while the
    footer called it an error would be the two-screens bug at one screen's
    distance."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        format_allocation_footer, format_label_total_notice, label_total_readout)

    for targets in ({'A': 118.0}, {'A': 75.0}, {'A': 100.0}, {'A': 99.995}):
        card = label_total_readout(targets)
        notice = format_label_total_notice(targets)
        _footer_text, footer_severity = format_allocation_footer(targets, 0.0)
        assert card.severity == footer_severity, targets
        assert (notice is None) == (card.severity == 'ok'), targets
        if notice is not None:
            assert notice[0] == card.detail, targets


def test_the_label_total_readout_measures_the_INVESTABLE_pool_not_the_gross_base():
    """The reserve does not enter it. The labels divide what the reserve LEAVES,
    so 100 typed across them is 100 whatever the reserve says -- an off-by-one
    that netted the reserve out would call a correct set 'under by 10'."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import label_total_readout

    for _reserve in (0.0, 10.0, 90.0):
        assert label_total_readout({'A': 60.0, 'B': 40.0}).text == '100.00%'


def test_the_label_total_readout_of_an_account_managing_nothing_says_so():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import label_total_readout

    card = label_total_readout({})

    assert card.text == '0.00%'
    assert card.severity == 'ok'
    assert card.detail


# ---------------------------------------------------------------------------
# THE SHARE-OF-LABEL DEFAULT IS THE SYMBOL'S ACTUAL SHARE
#
# The column used to default every symbol to FAIR SHARE -- nine symbols in one
# label all reading 11.11 while their real shares were 26.78 / 22.19 / 17.8 /
# 16.65 / 16.57 / 0 / 0 / ... A default nobody chose, sitting in an editable box,
# is a target the user never set and the plan will trade towards.
#
# A SAVED target always wins. The actual share is only a default.
#
# UNKNOWN IS NOT ZERO. A symbol whose price is unavailable has an UNMEASURABLE
# share, and 0% means "hold none of this" -- the plan sells the position. Worse,
# one unpriced member makes the LABEL's total unknown, so every unsaved share in
# it is a fraction of a denominator nobody has. The whole label's unsaved
# defaults go blank rather than being quietly restated against a smaller total.
#
# A GENUINE ZERO IS A REAL 0%. A symbol the account does not hold has an actual
# share of exactly 0, and that is the honest default -- it just has to be
# visible, because the old fair-share default would have bought it.
# ---------------------------------------------------------------------------

def test_the_default_share_is_the_symbols_ACTUAL_share_of_its_label():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHT_SOURCE_ACTUAL, resolve_symbol_weights)

    resolved = resolve_symbol_weights(['AAPL', 'MSFT', 'TSLA'], saved={},
                                      values={'AAPL': 500.0, 'MSFT': 300.0,
                                              'TSLA': 200.0},
                                      unmeasurable=())

    assert {s: r.weight_pct for s, r in resolved.items()} == {'AAPL': 50.0,
                                                              'MSFT': 30.0,
                                                              'TSLA': 20.0}
    assert {r.source for r in resolved.values()} == {WEIGHT_SOURCE_ACTUAL}


def test_the_default_is_NOT_the_fair_share_it_replaces():
    """The exact behaviour being removed: nine symbols all reading 11.11."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        resolve_symbol_weights)

    resolved = resolve_symbol_weights(['A', 'B', 'C'], saved={},
                                      values={'A': 800.0, 'B': 100.0, 'C': 100.0},
                                      unmeasurable=())

    assert resolved['A'].weight_pct == 80.0
    assert resolved['A'].weight_pct != pytest.approx(100.0 / 3.0, abs=0.01)


def test_a_SAVED_target_wins_over_the_actual_share():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHT_SOURCE_ACTUAL, WEIGHT_SOURCE_SAVED, resolve_symbol_weights)

    resolved = resolve_symbol_weights(['AAPL', 'MSFT'], saved={'AAPL': 90.0},
                                      values={'AAPL': 100.0, 'MSFT': 900.0},
                                      unmeasurable=())

    assert (resolved['AAPL'].weight_pct, resolved['AAPL'].source) == (
        90.0, WEIGHT_SOURCE_SAVED)
    assert (resolved['MSFT'].weight_pct, resolved['MSFT'].source) == (
        90.0, WEIGHT_SOURCE_ACTUAL)


def test_a_saved_target_of_ZERO_still_wins():
    """0 is an explicit "hold none of this", not an absent row. Reading it as
    absent would have the default quietly buy back a position the user sold out
    of on purpose."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHT_SOURCE_SAVED, resolve_symbol_weights)

    resolved = resolve_symbol_weights(['AAPL'], saved={'AAPL': 0.0},
                                      values={'AAPL': 5_000.0}, unmeasurable=())

    assert (resolved['AAPL'].weight_pct, resolved['AAPL'].source) == (
        0.0, WEIGHT_SOURCE_SAVED)


def test_a_symbol_the_account_does_not_hold_defaults_to_a_REAL_zero():
    """CAS and GIAX hold nothing, so their actual share IS 0% -- and they will not
    be bought. That is the correct reading of "default to actual"."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHT_SOURCE_ACTUAL, resolve_symbol_weights)

    resolved = resolve_symbol_weights(['AAPL', 'CAS'], saved={},
                                      values={'AAPL': 1_000.0, 'CAS': 0.0},
                                      unmeasurable=())

    assert (resolved['CAS'].weight_pct, resolved['CAS'].source) == (
        0.0, WEIGHT_SOURCE_ACTUAL)
    assert resolved['AAPL'].weight_pct == 100.0


def test_an_UNMEASURABLE_symbol_is_blank_and_never_zero():
    """A price outage may not quietly zero a target: 0% means SELL IT ALL."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHT_SOURCE_UNKNOWN, resolve_symbol_weights)

    resolved = resolve_symbol_weights(['AAPL', 'DARK'], saved={},
                                      values={'AAPL': 1_000.0},
                                      unmeasurable=['DARK'])

    assert resolved['DARK'].weight_pct is None
    assert resolved['DARK'].source == WEIGHT_SOURCE_UNKNOWN


def test_ONE_unmeasurable_member_makes_every_unsaved_share_in_the_label_unknown():
    """The denominator is the label's whole value. With one member unpriced, that
    total is unknown -- so no share OF it is knowable, and restating the others
    against the measured remainder would overstate every one of them."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHT_SOURCE_UNKNOWN, resolve_symbol_weights)

    resolved = resolve_symbol_weights(['AAPL', 'MSFT', 'DARK'], saved={},
                                      values={'AAPL': 600.0, 'MSFT': 400.0},
                                      unmeasurable=['DARK'])

    assert [r.weight_pct for r in resolved.values()] == [None, None, None]
    assert {r.source for r in resolved.values()} == {WEIGHT_SOURCE_UNKNOWN}


def test_a_SAVED_target_survives_an_unmeasurable_neighbour():
    """It was never derived from the denominator, so losing the denominator cannot
    take it away."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHT_SOURCE_SAVED, WEIGHT_SOURCE_UNKNOWN, resolve_symbol_weights)

    resolved = resolve_symbol_weights(['AAPL', 'DARK'], saved={'AAPL': 60.0},
                                      values={'AAPL': 600.0},
                                      unmeasurable=['DARK'])

    assert (resolved['AAPL'].weight_pct, resolved['AAPL'].source) == (
        60.0, WEIGHT_SOURCE_SAVED)
    assert resolved['DARK'].source == WEIGHT_SOURCE_UNKNOWN


def test_a_label_that_holds_NOTHING_defaults_every_symbol_to_zero():
    """Not unknown: nothing is held, which is perfectly measurable. It means the
    label will buy nothing until a share is typed or a fill button is pressed --
    which the page has to make visible rather than surprising."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHT_SOURCE_ACTUAL, resolve_symbol_weights)

    resolved = resolve_symbol_weights(['A', 'B'], saved={},
                                      values={'A': 0.0, 'B': 0.0}, unmeasurable=())

    assert [r.weight_pct for r in resolved.values()] == [0.0, 0.0]
    assert {r.source for r in resolved.values()} == {WEIGHT_SOURCE_ACTUAL}


def test_the_actual_shares_are_rounded_onto_the_stored_cent_grid():
    """The boxes step by 0.01 and the store keeps two places; a default carrying
    four would be refused by nothing and stored as something else."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        resolve_symbol_weights)

    resolved = resolve_symbol_weights(['A', 'B', 'C'], saved={},
                                      values={'A': 1.0, 'B': 1.0, 'C': 1.0},
                                      unmeasurable=())

    assert [r.weight_pct for r in resolved.values()] == [33.33, 33.33, 33.33]


def test_a_SHORT_leg_keeps_its_sign_out_of_the_denominator():
    """A net-short member makes the label's signed total smaller than its parts,
    so a share of it can exceed 100 or flip sign. The denominator is the GROSS
    money at work, which is the same choice ``UnrealisedPnL.pct`` makes."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        resolve_symbol_weights)

    resolved = resolve_symbol_weights(['LONG', 'SHORT'], saved={},
                                      values={'LONG': 900.0, 'SHORT': -100.0},
                                      unmeasurable=())

    assert resolved['LONG'].weight_pct == 90.0
    assert resolved['SHORT'].weight_pct == -10.0


def test_the_page_names_what_an_unsaved_share_is_showing():
    """"Default to actual" changes what happens to a newly added symbol: fair
    share would have bought it, an actual share of 0% will not. That is correct
    and it has to be legible, or it is discovered later and by surprise."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        SHARE_DEFAULT_NOTE)

    assert 'actual' in SHARE_DEFAULT_NOTE.lower()
    assert '0%' in SHARE_DEFAULT_NOTE
    assert 'Load current' in SHARE_DEFAULT_NOTE


def test_load_current_replaces_every_unmeasurable_free_share_with_the_actual_one():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHTS_LOADED_CURRENT, load_current_symbol_shares)

    result = load_current_symbol_shares(
        'ARK26', {'AAPL': 33.33, 'MSFT': 33.33, 'TSLA': 33.34},
        {'AAPL': 500.0, 'MSFT': 300.0, 'TSLA': 200.0}, unmeasurable=())

    assert result.changed is True
    assert result.reason_code == WEIGHTS_LOADED_CURRENT
    assert result.weights == {'AAPL': 50.0, 'MSFT': 30.0, 'TSLA': 20.0}


def test_load_current_overwrites_a_SAVED_share_because_that_is_what_it_is_for():
    """Unlike the DEFAULT, where a saved target wins. This is the button that says
    "forget what I typed, start from where I actually am"."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        load_current_symbol_shares)

    result = load_current_symbol_shares('ARK26', {'AAPL': 90.0, 'MSFT': 10.0},
                                        {'AAPL': 200.0, 'MSFT': 800.0},
                                        unmeasurable=())

    assert result.weights == {'AAPL': 20.0, 'MSFT': 80.0}


def test_load_current_refuses_a_label_whose_value_cannot_be_measured():
    """There is no "current" to load. Writing 0 for the unpriced one and a
    restated share for the rest would be a price outage rewriting real targets."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHTS_NOTHING_MEASURED, load_current_symbol_shares)

    result = load_current_symbol_shares('ARK26', {'AAPL': 60.0, 'DARK': 40.0},
                                        {'AAPL': 600.0}, unmeasurable=['DARK'])

    assert result.changed is False
    assert result.reason_code == WEIGHTS_NOTHING_MEASURED
    assert result.weights == {'AAPL': 60.0, 'DARK': 40.0}
    assert result.message


def test_load_current_on_a_label_that_holds_nothing_says_so_rather_than_zeroing_it():
    """Every actual share is a genuine 0 here, so the button would wipe the label.
    Wipe is the control for that, and it is one button along."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHTS_NOTHING_MEASURED, load_current_symbol_shares)

    result = load_current_symbol_shares('ARK26', {'AAPL': 60.0, 'MSFT': 40.0},
                                        {'AAPL': 0.0, 'MSFT': 0.0}, unmeasurable=())

    assert result.changed is False
    assert result.reason_code == WEIGHTS_NOTHING_MEASURED
    assert result.weights == {'AAPL': 60.0, 'MSFT': 40.0}


def test_load_current_that_would_change_nothing_reports_it_rather_than_writing():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHTS_UNCHANGED, load_current_symbol_shares)

    result = load_current_symbol_shares('ARK26', {'AAPL': 60.0, 'MSFT': 40.0},
                                        {'AAPL': 600.0, 'MSFT': 400.0},
                                        unmeasurable=())

    assert result.changed is False
    assert result.reason_code == WEIGHTS_UNCHANGED


def test_a_SAVED_target_does_not_mask_an_unmeasurable_holding():
    """The trap ``SymbolRow.measurable`` exists for. "Saved wins" short-circuits
    ``weight_source``, so a saved row with no price reports SAVED and looks
    perfectly measurable -- and "Load current" would then restate every share in
    the label against a total nobody knows."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        WEIGHT_SOURCE_SAVED)

    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'DARK']},
                              {'AAPL': _pos('AAPL', 10, 500.0),
                               'DARK': _pos('DARK', 10, 100.0)},
                              {'AAPL': 500.0, 'DARK': None},
                              valuation_mode=VALUATION_MODE_MARKET,
                              symbol_weights={'ARK26': {'AAPL': 60.0,
                                                        'DARK': 40.0}})
    by_symbol = {r.symbol: r for r in views[0].rows}

    assert by_symbol['DARK'].weight_source == WEIGHT_SOURCE_SAVED
    assert by_symbol['DARK'].measurable is False
    assert by_symbol['AAPL'].measurable is True


def test_a_FLAT_position_ROW_with_no_quote_is_still_measurable():
    """Survivor of the mutation run: dropping the ``not state.quantity`` guard.

    A broker that returns a zero-quantity row for a symbol you have sold out of --
    and no quote for it, because it is not worth quoting -- is worth EXACTLY
    NOTHING, and that is a measurement. Calling it unmeasurable would blank every
    unsaved share in the label around it and refuse "Load current", on the
    strength of a position that does not exist.
    """
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'GONE']},
                              {'AAPL': _pos('AAPL', 10, 500.0),
                               'GONE': _pos('GONE', 0, 0.0)},
                              {'AAPL': 500.0, 'GONE': None},
                              valuation_mode=VALUATION_MODE_MARKET)
    by_symbol = {r.symbol: r for r in views[0].rows}

    assert by_symbol['GONE'].measurable is True
    # ...so the label keeps its verdict, and the flat row keeps its honest 0%.
    assert by_symbol['AAPL'].weight_pct == 100.0
    assert by_symbol['GONE'].weight_pct == 0.0


def test_a_FLAT_holding_is_measurable_and_an_unpriced_one_is_not():
    """Nothing held is worth exactly nothing, which is a measurement. Only a
    holding with no price is unmeasurable, and only in MARKET mode -- the cost
    basis is always published."""
    args = ({'ARK26': ['FLAT', 'DARK']},
            {'DARK': _pos('DARK', 10, 100.0)},
            {'FLAT': 12.0, 'DARK': None})
    market = build_label_views([ManagedLabel('ARK26', 100.0)], *args,
                               valuation_mode=VALUATION_MODE_MARKET)
    cost = build_label_views([ManagedLabel('ARK26', 100.0)], *args,
                             valuation_mode=VALUATION_MODE_COST)

    assert {r.symbol: r.measurable for r in market[0].rows} == {'FLAT': True,
                                                                'DARK': False}
    assert all(r.measurable for r in cost[0].rows)


# ---------------------------------------------------------------------------
# THE SHARE BAR -- one bar component, three places
#
# The label header has carried a current-versus-target bar since the rework. The
# same geometry now answers two more questions: how the SYMBOL SHARES inside one
# label add up against their 100, and how the account's free cash sits against
# the reserve it is targeting. One builder, one tolerance band, one over/under
# vocabulary -- so the whole page reads as one visual language and no bar can
# disagree with the sentence beside it.
# ---------------------------------------------------------------------------

def _bar(**kw):
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import build_share_bar
    return build_share_bar(**kw)


def test_a_share_bar_puts_the_fill_and_the_notch_on_ONE_scale():
    """The load-bearing property: if they did not share a scale, "over" and
    "under" would be a lie no reader could catch by eye."""
    bar = _bar(current_pct=25.0, target_pct=100.0)

    assert bar.fraction == pytest.approx(0.25)
    assert bar.notch_fraction == pytest.approx(1.0)


def test_a_share_bar_that_is_SHORT_of_its_target_says_under():
    bar = _bar(current_pct=75.0, target_pct=100.0)

    assert bar.status == LABEL_STATUS_UNDER
    assert bar.delta_text == 'under by 25.0pp'


def test_a_share_bar_that_is_OVER_its_target_says_over():
    bar = _bar(current_pct=118.0, target_pct=100.0)

    assert bar.status == LABEL_STATUS_OVER
    assert bar.delta_text == 'over by 18.0pp'


def test_a_share_bar_inside_the_tolerance_band_is_on_target():
    bar = _bar(current_pct=100.0, target_pct=100.0)

    assert bar.status == LABEL_STATUS_OK
    assert bar.delta_text == 'on target'


def test_a_share_bar_over_100_stretches_the_track_rather_than_clipping():
    """The notch stays visible and the fill stays honest -- the same choice the
    label bars make, where a margin book legitimately holds more than the pool."""
    bar = _bar(current_pct=150.0, target_pct=100.0)

    assert bar.fraction == pytest.approx(1.0)
    assert bar.notch_fraction == pytest.approx(2.0 / 3.0)
    assert bar.current_text == '150.0%'


def test_a_share_bar_states_its_delta_in_MONEY_too_when_there_is_money():
    """The reserve row has both; a sum of symbol shares has only points."""
    bar = _bar(current_pct=4.7, target_pct=10.0, current_value=245.50,
               target_value=526.09)

    assert bar.status == LABEL_STATUS_UNDER
    assert bar.delta_text == 'under by 5.3pp ($280.59)'


def test_a_share_bar_with_no_measurable_current_draws_no_verdict():
    bar = _bar(current_pct=None, target_pct=100.0)

    assert bar.status == LABEL_STATUS_NONE
    assert bar.fraction == 0.0
    assert bar.current_text == LABEL_STATUS_NONE
    assert bar.delta_text == LABEL_STATUS_NONE


def test_a_share_bar_never_renders_a_negative_current_as_a_full_track():
    """A net-short set is genuinely negative and the figure says so; the bar
    clamps to empty rather than wrapping round to full."""
    bar = _bar(current_pct=-30.0, target_pct=100.0)

    assert bar.fraction == 0.0
    assert bar.current_text == '-30.0%'


def test_the_points_only_delta_is_unreachable_from_the_LABEL_bars():
    """``format_label_delta`` grew a money-less branch for the share bars. The
    label bars set their two figures together, so they can never take it -- which
    is what keeps the money out of exactly one of the page's bars by accident."""
    for reserve in (0.0, 25.0):
        for view in (_view('A', 4_500.0, 30.0), _view('B', 0.0, 0.0)):
            bar = build_label_bars([view], base_notional=10_000.0,
                                   unallocated_pct=reserve)[0]
            assert (bar.delta_pct is None) == (bar.delta_value is None)


def test_the_symbol_total_bar_sums_the_shares_WITHIN_one_label():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import symbol_total_bar

    bar = symbol_total_bar({'AAPL': 26.78, 'MSFT': 22.19, 'TSLA': 17.8})

    assert bar.current_pct == pytest.approx(66.77)
    assert bar.target_pct == 100.0
    assert bar.status == LABEL_STATUS_UNDER


def test_the_symbol_total_bar_sums_and_never_averages():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import symbol_total_bar

    assert symbol_total_bar({'A': 25.0, 'B': 25.0, 'C': 25.0}).current_pct == 75.0


def test_the_symbol_total_bar_leaves_an_UNMEASURABLE_share_out_and_says_so():
    """A blank share is not a zero. Counting it as 0 would report a label whose
    total cannot be known as 25 points short of 100."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import symbol_total_bar

    bar = symbol_total_bar({'AAPL': 60.0, 'DARK': None})

    assert bar.current_pct is None
    assert bar.status == LABEL_STATUS_NONE


def test_the_symbol_total_bar_of_a_label_with_no_symbols_is_not_a_verdict():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import symbol_total_bar

    assert symbol_total_bar({}).status == LABEL_STATUS_NONE


# ---------------------------------------------------------------------------
# THE MANAGE-LABELS DIALOG: its grid, and what a colour state is CALLED
# ---------------------------------------------------------------------------

def test_the_label_column_is_wide_enough_for_the_longest_name():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        label_column_width_ch)

    assert label_column_width_ch(['BAST_TECH_ROBOT', 'WHEEL_L1_HR']) >= len(
        'BAST_TECH_ROBOT')


def test_the_label_column_has_a_floor_so_short_names_still_line_up():
    """Every block starts at the same x or the dialog reads as broken -- which is
    the actual complaint. A column that shrank to the longest of two short names
    would jump the moment a third was managed."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        LABEL_COLUMN_MIN_CH, label_column_width_ch)

    assert label_column_width_ch(['A', 'B']) == LABEL_COLUMN_MIN_CH
    assert label_column_width_ch([]) == LABEL_COLUMN_MIN_CH
    assert label_column_width_ch(None) == LABEL_COLUMN_MIN_CH


def test_the_label_column_is_CAPPED_so_one_absurd_name_cannot_blow_the_dialog_out():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        LABEL_COLUMN_MAX_CH, label_column_width_ch)

    assert label_column_width_ch(['X' * 200]) == LABEL_COLUMN_MAX_CH


def test_a_label_with_no_colour_is_DESCRIBED_differently_from_an_unreadable_one():
    """Both render the same neutral grey -- the parse decides what reaches a CSS
    ``style`` attribute and it is not negotiable -- but they are different facts,
    and the one the user can act on is the second."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        describe_label_color, resolve_label_icon_color)

    assert resolve_label_icon_color(None) == resolve_label_icon_color('not-a-colour')
    assert describe_label_color(None) != describe_label_color('not-a-colour')


def test_a_chosen_colour_is_described_by_its_own_hex():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        describe_label_color)

    assert '#F0E442' in describe_label_color('#F0E442')


def test_an_unreadable_stored_colour_says_it_is_being_IGNORED():
    """Not "no colour": the row has something in it, the renderer refused it, and
    the user is the only one who can put it right."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        describe_label_color)

    said = describe_label_color('rgb(1,2,3)')
    assert 'ignored' in said.lower()


# ---------------------------------------------------------------------------
# PAINTING AGAINST A STYLESHEET THAT FIGHTS BACK
#
# ``ui/static/styles.css`` is loaded on every page and is written almost entirely
# in ``!important``. Two of its rules land squarely on the label row:
#
#   .q-expansion-item .q-icon { color: #a0aec0 !important; }
#   p, span, div, label, ...  { color: #ffffff; }
#
# The first BEATS AN INLINE STYLE -- a stylesheet ``!important`` outranks a plain
# inline declaration -- so the tag icon rendered grey beside a correctly coloured
# bar, which is exactly what the user reported and what no Python-level test could
# see: the element carried ``style={'color': '#56B4E9'}`` the whole time.
#
# So colour on this page is written as an inline ``!important``, which is the top
# of the cascade, and NOT as a Tailwind class name whose paint depends on a
# stylesheet we do not control and on the Tailwind JIT having generated it.
# ---------------------------------------------------------------------------

def test_a_colour_this_page_paints_is_written_so_the_stylesheet_cannot_win():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        important_color_style)

    assert important_color_style('#56B4E9') == 'color: #56B4E9 !important'


def test_over_is_ORANGE_and_a_row_inside_the_band_stays_neutral():
    """"over by ..." is the actionable warning state at any distance. A row that
    is merely a little SHORT is drift, not a decision, and stays neutral."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        NEUTRAL_TEXT_COLOR, label_status_color)

    over = label_status_color(LABEL_STATUS_OVER, value_pct=61.0, target_pct=60.0)
    assert over != NEUTRAL_TEXT_COLOR
    assert 'F' in over.upper()   # a real hex
    for quiet in (LABEL_STATUS_UNDER, LABEL_STATUS_OK, LABEL_STATUS_NONE):
        assert label_status_color(quiet, value_pct=59.0,
                                  target_pct=60.0) == NEUTRAL_TEXT_COLOR


def test_a_row_that_has_fallen_out_of_the_target_band_reads_ORANGE():
    """"under by 20pp or more" is the second coloured state, and it is the BAR's
    own threshold rather than a second copy of 20: the sentence goes orange in the
    same step the track beside it goes yellow."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        STATUS_OVER_COLOR, band_color, label_status_color, within_target_band)

    assert label_status_color(LABEL_STATUS_UNDER, value_pct=39.0,
                              target_pct=60.0) == STATUS_OVER_COLOR
    # ...and it really is the same predicate, asked of the same two figures.
    assert not within_target_band(39.0, 60.0)
    assert band_color(39.0, 60.0) != band_color(45.0, 60.0)


@pytest.mark.parametrize('value_pct,orange', [
    (60.00, False),   # exactly on target
    (59.99, False),   # one hundredth under
    (40.00, False),   # exactly 20pp under -- the band is INCLUSIVE at this edge
    (39.99, True),    # one hundredth beyond it: the first orange
])
def test_the_under_threshold_is_pinned_at_both_of_its_edges(value_pct, orange):
    """Four boundaries, because a band that quietly moves by a hundredth is a band
    nobody can rely on -- and because the inclusive edge is what makes "green from
    80 to 100" come out exactly right against a target of 100."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        BAND_OFF_COLOR, NEUTRAL_TEXT_COLOR, STATUS_OVER_COLOR, band_color,
        label_status_color)

    status = (LABEL_STATUS_UNDER if value_pct < 60.0 - LABEL_STATUS_TOLERANCE_PCT
              else LABEL_STATUS_OK)
    expected = STATUS_OVER_COLOR if orange else NEUTRAL_TEXT_COLOR
    assert label_status_color(status, value_pct=value_pct,
                              target_pct=60.0) == expected
    # The bar changes colour at exactly the same hundredth.
    assert (band_color(value_pct, 60.0) == BAND_OFF_COLOR) is orange


def test_an_unmeasurable_figure_is_neither_orange_nor_green():
    """The distinction the whole page turns on. ``None`` is not 0.0% and not a
    shortfall: an account the broker has not answered for has no gap to be 20
    points short of, and painting one would report a problem nobody has."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        NEUTRAL_TEXT_COLOR, band_color, label_status_color)

    assert label_status_color(LABEL_STATUS_NONE, value_pct=None,
                              target_pct=60.0) == NEUTRAL_TEXT_COLOR
    assert label_status_color(LABEL_STATUS_UNDER, value_pct=None,
                              target_pct=60.0) == NEUTRAL_TEXT_COLOR
    # ...exactly as the bar already refuses to judge one.
    assert band_color(None, 60.0) == NEUTRAL_TEXT_COLOR


def test_the_two_figures_are_required_so_a_call_site_cannot_forget_them():
    """No default, deliberately. A default would let a renderer keep the old
    status-only behaviour silently, which is how a paint goes missing."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        label_status_color)

    with pytest.raises(TypeError):
        label_status_color(LABEL_STATUS_UNDER)


def test_the_pnl_colour_is_green_up_red_down_and_neutral_in_the_band():
    """The same epsilon band ``pnl_classes`` already had, as a paintable colour.
    A flat 0.00 stays neutral and an unmeasurable P&L stays neutral -- neither is
    a verdict, and painting break-even red invents one."""
    from ba2_trade_platform.core.portfolio_allocation import MONEY_EPSILON, UnrealisedPnL
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        NEUTRAL_TEXT_COLOR, pnl_color)

    assert pnl_color(UnrealisedPnL(amount=124.43)) != NEUTRAL_TEXT_COLOR
    assert pnl_color(UnrealisedPnL(amount=-29.58)) != NEUTRAL_TEXT_COLOR
    assert pnl_color(UnrealisedPnL(amount=124.43)) != pnl_color(
        UnrealisedPnL(amount=-29.58))
    for quiet in (UnrealisedPnL(amount=0.0),
                  UnrealisedPnL(amount=MONEY_EPSILON / 2.0),
                  UnrealisedPnL(amount=None), None):
        assert pnl_color(quiet) == NEUTRAL_TEXT_COLOR, quiet


def test_the_pnl_colour_and_the_pnl_CLASSES_cannot_disagree():
    """Two renderings of one verdict. The classes stay for anything reading the
    DOM; the inline colour is what actually paints."""
    from ba2_trade_platform.core.portfolio_allocation import UnrealisedPnL
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        NEUTRAL_TEXT_COLOR, pnl_classes, pnl_color)

    for pnl in (UnrealisedPnL(amount=1.0), UnrealisedPnL(amount=-1.0),
                UnrealisedPnL(amount=0.0), UnrealisedPnL(amount=None)):
        coloured = pnl_color(pnl) != NEUTRAL_TEXT_COLOR
        assert coloured == ('green' in pnl_classes(pnl) or 'red' in pnl_classes(pnl))


# ---------------------------------------------------------------------------
# THE UNALLOCATED BAR, INVERTED AND RE-HOMED
#
# It filled to the RESERVE -- 5.3% of a bar, with 94.7% of the account's money
# represented by empty track. The user asked for it the other way round: the fill
# is what is ALLOCATED, and the reserve is the gap left at the right-hand end.
#
# The target tick has to travel with it. A 10% reserve target is a 90% ALLOCATED
# target, so the tick sits at 90% along; leaving it at 10% would put it on the
# wrong side of the fill and make the bar contradict its own caption.
# ---------------------------------------------------------------------------

def test_the_allocation_bar_fills_with_what_is_ALLOCATED():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import allocation_bar

    bar = allocation_bar(base_notional=5_296.0, available_buying_power=279.25,
                         unallocated_pct=10.0)

    assert bar.current_pct == pytest.approx(94.73, abs=0.01)
    assert bar.fraction == pytest.approx(0.9473, abs=0.0001)


def test_the_allocation_bars_TICK_moves_with_the_inversion():
    """A 10% reserve target is a 90% allocated target. A tick left at 10% would
    sit on the wrong side of the fill and contradict the caption."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import allocation_bar

    bar = allocation_bar(base_notional=5_296.0, available_buying_power=279.25,
                         unallocated_pct=10.0)

    assert bar.target_pct == pytest.approx(90.0)
    assert bar.notch_fraction == pytest.approx(0.90, abs=0.0001)


def test_the_allocation_bar_and_the_reserve_row_describe_ONE_account():
    """Inverted, not re-derived: the two come from the same ``unallocated_row``,
    so the bar and the sentence beside it cannot drift apart."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        allocation_bar, unallocated_row)

    row = unallocated_row(base_notional=5_296.0, available_buying_power=279.25,
                          unallocated_pct=10.0)
    bar = allocation_bar(base_notional=5_296.0, available_buying_power=279.25,
                         unallocated_pct=10.0)

    assert bar.current_pct == pytest.approx(100.0 - row.pct_of_base)
    assert bar.target_pct == pytest.approx(100.0 - row.target_pct)


def test_being_UNDER_the_reserve_target_reads_as_OVER_allocated():
    """The same fact from the bar's own subject. Holding less cash than the target
    IS being more invested than the target, and "over" is the state the page
    paints orange -- which agrees with the sub-line warning that closing the gap
    means selling."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import allocation_bar

    bar = allocation_bar(base_notional=5_296.0, available_buying_power=279.25,
                         unallocated_pct=10.0)

    assert bar.status == LABEL_STATUS_OVER
    assert bar.delta_text.startswith('over by ')


def test_holding_MORE_cash_than_the_target_reads_as_UNDER_allocated():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import allocation_bar

    bar = allocation_bar(base_notional=1_000.0, available_buying_power=300.0,
                         unallocated_pct=10.0)

    assert bar.current_pct == pytest.approx(70.0)
    assert bar.status == LABEL_STATUS_UNDER


def test_the_allocation_bar_is_absent_when_there_is_no_base_to_divide_by():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import allocation_bar

    assert allocation_bar(base_notional=0.0, available_buying_power=100.0,
                          unallocated_pct=10.0) is None
    assert allocation_bar(base_notional=None, available_buying_power=100.0,
                          unallocated_pct=10.0) is None
    assert allocation_bar(base_notional=1_000.0, available_buying_power=None,
                          unallocated_pct=10.0) is None


def test_the_bars_that_can_be_MISREAD_as_their_opposite_carry_a_legend():
    """A card titled "Unallocated reserve" whose bar fills with ALLOCATED money is
    a contradiction unless the legend says which is which. Same principle as the
    label-total bar, which is a bar in a card about label COUNTS."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        ALLOCATION_BAR_LEGEND, LABEL_TOTAL_BAR_LEGEND)

    assert 'llocated' in ALLOCATION_BAR_LEGEND
    assert 'reserve' in ALLOCATION_BAR_LEGEND.lower()
    assert LABEL_TOTAL_BAR_LEGEND


def test_the_reserve_keeps_BOTH_things_the_blue_panel_was_saying():
    """Moving it into the card must not drop either: the gross-base denominator
    (this page's one exception, and unexplained it is just two silently different
    denominators) or the consequence warning on the slider."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        RESERVE_BASIS_NOTE, RESERVE_SELL_WARNING)

    assert 'gross account base' in RESERVE_BASIS_NOTE
    assert 'SELL orders' in RESERVE_SELL_WARNING


def test_the_reserve_note_no_longer_points_BELOW_itself():
    """It is a top-row card now; the labels are not "below" it in any useful
    sense."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        RESERVE_BASIS_NOTE)

    assert 'below' not in RESERVE_BASIS_NOTE.lower()


# ---------------------------------------------------------------------------
# THE TARGET BAND -- ONE rule, both bars
#
# "if we are above or 20% below target, yellow, otherwise green". The label-total
# bar's "green 80 to 100" is the same rule against a target of 100, which is why
# there is one predicate and not two: the two bars sit on one screen and a
# disagreement between them would be obvious and unexplainable.
#
# 20 PERCENTAGE POINTS, not 20% relative. That is what makes "80 to 100" against a
# 100% target come out exactly right; relative would give 80 there but 72 against
# a 90% target rather than 70.
#
# BOTH FIGURES ARE ROUNDED TO THE CENT FIRST, and that is load-bearing rather than
# tidiness: an even split of three labels is 33.33 + 33.33 + 33.34, which is
# exactly 100 in decimal and 100.00000000000001 in binary. Comparing raw would
# paint the engine's own splitter yellow.
# ---------------------------------------------------------------------------

def _band(value, target=100.0):
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import within_target_band
    return within_target_band(value, target)


def test_exactly_ON_target_is_inside_the_band():
    assert _band(100.0, 100.0) is True
    assert _band(90.0, 90.0) is True


def test_a_hundredth_ABOVE_target_falls_out_of_the_band():
    assert _band(100.01, 100.0) is False
    assert _band(90.01, 90.0) is False


def test_exactly_twenty_points_BELOW_target_is_still_inside():
    assert _band(80.0, 100.0) is True
    assert _band(70.0, 90.0) is True


def test_a_hundredth_below_THAT_falls_out_too():
    assert _band(79.99, 100.0) is False
    assert _band(69.99, 90.0) is False


def test_the_band_is_twenty_POINTS_and_not_twenty_percent_relative():
    """Against a 90% target, 20 points is 70 and 20% relative would be 72."""
    assert _band(70.0, 90.0) is True
    assert _band(71.0, 90.0) is True


def test_the_engines_own_even_split_is_never_painted_off_target():
    """33.33 + 33.33 + 33.34 is exactly 100 in decimal and a hair over it in
    binary. Comparing raw would call the splitter's own output off target."""
    assert _band(33.33 + 33.33 + 33.34, 100.0) is True


def test_an_unmeasurable_value_is_not_claimed_to_be_in_the_band():
    assert _band(None, 100.0) is False


def test_the_band_colour_is_green_inside_and_yellow_outside():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        BAND_OFF_COLOR, BAND_OK_COLOR, band_color)

    assert band_color(95.0, 100.0) == BAND_OK_COLOR
    assert band_color(100.0, 100.0) == BAND_OK_COLOR
    assert band_color(80.0, 100.0) == BAND_OK_COLOR
    assert band_color(100.01, 100.0) == BAND_OFF_COLOR
    assert band_color(79.99, 100.0) == BAND_OFF_COLOR
    assert BAND_OK_COLOR != BAND_OFF_COLOR


def test_an_unmeasurable_bar_is_painted_NEUTRAL_and_not_yellow():
    """No verdict is not a warning. A yellow bar on an account the broker has not
    answered for would be reporting a problem the user does not have."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        BAND_OFF_COLOR, NEUTRAL_TEXT_COLOR, band_color)

    assert band_color(None, 100.0) == NEUTRAL_TEXT_COLOR
    assert band_color(None, 100.0) != BAND_OFF_COLOR


def test_the_label_total_severity_follows_the_SAME_band_as_its_colour():
    """Item 2's "make sure it agrees with the colour". The sentence stays the
    engine's -- it is the arithmetic -- but how alarmed the page looks on the
    SHORTFALL side is the band's call, so a GREEN bar can never sit beside orange
    text. Anywhere the bar is green, the text is quiet."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        BAND_OK_COLOR, band_color, label_total_readout)

    for total in (80.0, 85.0, 100.0):
        readout = label_total_readout({'A': total})
        assert readout.severity == 'ok', total
        assert band_color(readout.bar.current_pct,
                          readout.bar.target_pct) == BAND_OK_COLOR, total
    assert label_total_readout({'A': 79.99}).severity == 'warning'


def test_over_100_keeps_its_RED_although_the_bar_beside_it_is_yellow():
    """The one place the band deliberately does NOT drive the words.

    Over-allocating claims more of the pool than exists; the inline boxes refuse it
    outright and only a wizard-era database row can produce it. Painting it the
    same yellow as "a little under target" would say the two are equally fine. The
    bar is yellow (not green), so the pair never contradicts -- the words just
    carry more weight than the geometry, which is the safe direction."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        BAND_OFF_COLOR, band_color, label_total_readout)

    readout = label_total_readout({'A': 118.0})
    assert readout.severity == 'negative'
    assert band_color(readout.bar.current_pct,
                      readout.bar.target_pct) == BAND_OFF_COLOR


def test_the_hundredth_wide_sliver_above_100_stays_QUIET_in_words():
    """The one seam between the band and the engine's tolerance, pinned.

    The band is exclusive above target; ``LABEL_TOTAL_TOLERANCE_PCT`` is 0.01pp
    wide, so 100.01 -- reachable from a two-decimal three-way split -- is OUT of the
    band and INSIDE the tolerance. The bar goes a hair yellow there; the sentence is
    literally the words "on target" and must not be shouted, because "on target" in
    warning orange is a sentence arguing with itself."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        BAND_OFF_COLOR, LABEL_TOTAL_ON_TARGET, band_color, label_total_readout)

    readout = label_total_readout({'A': 100.01})
    assert readout.detail == LABEL_TOTAL_ON_TARGET
    assert readout.severity == 'ok'
    assert band_color(readout.bar.current_pct,
                      readout.bar.target_pct) == BAND_OFF_COLOR


def test_the_sentence_still_states_the_arithmetic_inside_the_band():
    """Green does not mean silent. At 85% the card still says exactly how much of
    the pool the labels leave undeployed -- the colour says "acceptable", the
    words say where in the band you are."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        label_total_readout)

    readout = label_total_readout({'A': 85.0})
    assert readout.severity == 'ok'
    assert 'under 100%' in readout.detail


def test_the_card_the_notice_and_the_footer_still_share_one_verdict():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        format_allocation_footer, format_label_total_notice, label_total_readout)

    for targets in ({'A': 118.0}, {'A': 75.0}, {'A': 100.0}, {'A': 85.0}):
        readout = label_total_readout(targets)
        _text, footer_severity = format_allocation_footer(targets, 0.0)
        notice = format_label_total_notice(targets)
        assert readout.severity == footer_severity, targets
        assert (notice is None) == (readout.severity == 'ok'), targets


# ---------------------------------------------------------------------------
# THE MERGED MONEY CARD -- three figures, one box, three states each
#
# The three used to be three cards, and the buying-power one was drawn inside an
# ``if buying_power is not None``: on a broker outage it VANISHED. That is
# unknown-as-zero in the form a user cannot even notice, because there is nothing
# on screen to notice. ``summary_figures`` always returns three.
# ---------------------------------------------------------------------------

def _figures(**kwargs):
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import summary_figures

    defaults = {'managed_value': 4_853.48, 'valuation_mode': VALUATION_MODE_MARKET,
                'account_value': 2_511.90, 'available_buying_power': 170.31}
    defaults.update(kwargs)
    return summary_figures(**defaults)


def test_the_money_card_always_has_exactly_three_figures():
    """Whatever the broker did or did not answer. The count is what makes "the card
    always renders" structural rather than a rule a renderer has to remember."""
    for kwargs in ({}, {'account_value': None}, {'available_buying_power': None},
                   {'managed_value': None},
                   {'account_value': None, 'available_buying_power': None,
                    'managed_value': None}):
        assert len(_figures(**kwargs)) == 3, kwargs


def test_buying_power_STATE_ONE_a_real_figure():
    figure = _figures(available_buying_power=170.31)[2]
    assert figure.title == BUYING_POWER_TITLE
    assert figure.available is True
    assert figure.text == '$170.31'


def test_buying_power_STATE_TWO_a_measured_zero_is_printed_as_zero():
    """Every dollar deployed is a REAL state and it is the reason the next plan
    will not buy anything. Suppressing it as "unavailable" reports an outage that
    did not happen -- the inverse of the bug this card is careful about."""
    figure = _figures(available_buying_power=0.0)[2]
    assert figure.available is True
    assert figure.text == '$0.00'
    assert figure.detail == ''


def test_buying_power_STATE_THREE_unknown_says_so_AND_says_why():
    """The defect being folded in: the card used to disappear here."""
    figure = _figures(available_buying_power=None)[2]
    assert figure.available is False
    assert figure.text == FIGURE_UNKNOWN_TEXT == 'unknown'
    assert figure.detail == BUYING_POWER_UNAVAILABLE_DETAIL
    assert figure.detail
    # The reason may not itself contain the string the line promises never to print.
    assert '0.00' not in figure.text and '$' not in figure.text
    assert '0.00' not in figure.detail and '$' not in figure.detail


def test_ONE_unreadable_figure_does_not_blank_its_TWO_NEIGHBOURS():
    """The property the merge has to preserve. A broker that publishes no buying
    power still publishes a net liquidating value, and the managed value is ours."""
    managed, account, buying_power = _figures(available_buying_power=None)

    assert buying_power.available is False
    assert managed.available is True and managed.text == '$4,853.48'
    assert account.available is True and account.text == '$2,511.90'


def test_an_unreadable_ACCOUNT_VALUE_does_not_blank_the_other_two_either():
    managed, account, buying_power = _figures(account_value=None)

    assert account.available is False and account.text == FIGURE_UNKNOWN_TEXT
    assert managed.available is True and managed.text == '$4,853.48'
    assert buying_power.available is True and buying_power.text == '$170.31'


def test_every_caption_survives_the_merge_including_the_parentheticals():
    """They are DEFINITIONS, not decoration. "Managed value" alone does not say
    whether it is what you paid or what it is worth, and this page carries a
    Valuation selector two inches away."""
    market = _figures(valuation_mode=VALUATION_MODE_MARKET)
    cost = _figures(valuation_mode=VALUATION_MODE_COST)

    assert market[0].title == 'Managed value — market value (qty x price)'
    assert cost[0].title == 'Managed value — cost basis (what you paid)'
    assert market[1].title == ACCOUNT_VALUE_TITLE == 'Account value'
    assert market[2].title == BUYING_POWER_TITLE == 'Free buying power'


def test_the_leverage_LINE_survives_the_merge_and_stays_with_its_own_figure():
    """"managed positions are 2.02x this" is telling the user they are levered, and
    "this" only means anything directly under the account value."""
    account = _figures(managed_value=5_073.71, account_value=2_511.90)[1]

    assert '2.02x this' in account.detail


def test_a_managed_value_of_zero_is_a_figure_and_not_an_outage():
    figure = _figures(managed_value=0.0)[0]
    assert figure.available is True
    assert figure.text == '$0.00'


# ---------------------------------------------------------------------------
# COLLAPSING THE DRY RUN'S PLAN WARNINGS
#
# ``precheck_plan`` asks the broker to price each candidate BUY without sending
# it; ``apply_order_impacts`` swaps the planner's estimated buying-power cost for
# the broker's measured one and files ONE warning PER SYMBOL wherever the two
# differ by over half a cent. On a margin book that is most rows.
# ---------------------------------------------------------------------------

def _precheck(*symbols):
    from ba2_trade_platform.core.portfolio_allocation import (
        WARNING_PRECHECK_DISAGREED_FMT)

    return [WARNING_PRECHECK_DISAGREED_FMT.format(symbol=s) for s in symbols]


def test_the_precheck_parse_is_built_FROM_the_engines_own_format_string():
    """Not from a hand-copied literal. A reworded engine warning must stop matching
    and fall through as an ordinary line -- visible and odd -- rather than being
    silently swallowed by a regex that no longer means what it did."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        precheck_resolved_symbols)

    assert precheck_resolved_symbols(_precheck('NLR', 'BWXT')) == ['NLR', 'BWXT']
    assert precheck_resolved_symbols(
        ['broker precheck disagreed on NLR - re-solved AND SOMETHING ELSE']) == []


def test_eight_precheck_warnings_collapse_to_one_line():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        plan_warning_lines)

    lines = plan_warning_lines(_precheck(*'ABCDEFGH'))

    assert len(lines) == 1


def test_the_collapsed_line_is_INFO_when_no_quantity_moved():
    """A re-solve that does not scale the plan changes nothing the user chose. It
    is a normal self-correcting step, so it is said once, quietly."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        plan_warning_lines)

    (text, severity), = plan_warning_lines(_precheck('NLR', 'CCJ'))

    assert severity == 'info'
    assert 'NLR, CCJ' in text
    assert 'quantities are unchanged' in text


def test_the_collapsed_line_ESCALATES_when_the_plan_was_SCALED():
    """The materially different outcome, which must survive the collapse: the
    broker's corrected costs go back through the pro-rata buying-power scaler, and
    when they no longer fit, every buy shrinks."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        plan_warning_lines)

    (text, severity), = plan_warning_lines(_precheck('NLR'), scale_factor=0.62)

    assert severity == 'warning'
    assert '0.62' in text
    assert 'smaller than your targets' in text
    assert 'Reasons' in text


def test_a_scale_factor_of_exactly_one_is_NOT_an_escalation():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        plan_warning_lines)

    assert plan_warning_lines(_precheck('NLR'), scale_factor=1.0)[0][1] == 'info'


def test_the_collapsed_line_names_no_internal_step():
    """"broker precheck disagreed on NLR - re-solved" names a step in our own code
    and reports an outcome the reader cannot act on."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        plan_warning_lines)

    text = plan_warning_lines(_precheck('NLR'))[0][0].lower()

    for jargon in ('precheck', 're-solve', 'disagreed', 'bp_cost', 'impact'):
        assert jargon not in text, jargon


def test_a_LONG_symbol_list_is_summarised_rather_than_run_as_a_paragraph():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        PRECHECK_SYMBOLS_NAMED, plan_warning_lines)

    symbols = [f'SYM{i}' for i in range(25)]
    text = plan_warning_lines(_precheck(*symbols))[0][0]

    assert 'SYM0' in text
    assert f'and {25 - PRECHECK_SYMBOLS_NAMED} more' in text
    assert 'SYM24' not in text
    assert '25 of these buys' in text      # the COUNT is never summarised away


def test_a_duplicate_precheck_warning_names_its_symbol_once():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        precheck_resolved_symbols)

    assert precheck_resolved_symbols(_precheck('NLR', 'NLR', 'CCJ')) == ['NLR', 'CCJ']


def test_every_OTHER_warning_survives_verbatim_and_keeps_the_top():
    """An empty label and an unconverged residual are each about one specific
    thing. Only the per-symbol repetition is collapsed."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        plan_warning_lines)

    others = ["label 'CRYPTO' has no symbols - its 12.00% weight is unallocated",
              'residual did not converge']
    lines = plan_warning_lines(others + _precheck('NLR', 'CCJ'))

    assert [text for text, _s in lines[:2]] == others
    assert all(severity == 'warning' for _t, severity in lines[:2])
    assert len(lines) == 3


def test_no_warnings_produce_no_lines():
    """Alpaca has no order-preview endpoint, so none of this ever fires there."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        plan_warning_lines)

    assert plan_warning_lines([]) == []
    assert plan_warning_lines(None) == []


# ---------------------------------------------------------------------------
# THE BASE, SPELLED OUT
#
# The card states a dozen figures "of base" and never showed the base. Both
# reported confusions came from that one omission:
#   1. "10% of total investable 4832 is 482, not 537" -- the reserve is 10% of the
#      BASE (5,369.79), not of the remainder it leaves.
#   2. "we have $4,827.18 managed and $4,832.81 investable ... total investable
#      should be managed + BP" -- managed + BP IS the base (5,369.79); the 4,832.81
#      is that base MINUS the reserve. The two differ by exactly the reserve, and
#      neither the base nor the reserve was on screen to check against.
# ---------------------------------------------------------------------------

def test_the_base_line_shows_the_addition_every_percentage_divides():
    text = format_base_composition(base_notional=5_369.79, available_buying_power=542.61)

    assert text == 'base $5,369.79 = $4,827.18 managed + $542.61 buying power'


def test_the_managed_term_is_derived_so_the_addition_cannot_disagree():
    """base - buying_power, never a second sum of the positions. Re-summing them here
    would be a second computation of a number the page already has, and the two would
    drift the first time a valuation mode changed on one side only."""
    for base, bp in ((10_000.0, 2_500.0), (5_369.79, 542.61), (1_000.0, 1_000.0)):
        text = format_base_composition(base_notional=base,
                                       available_buying_power=bp)
        managed = float(text.split('= $')[1].split(' managed')[0].replace(',', ''))

        assert round(managed + bp, 2) == round(base, 2)


def test_the_base_line_is_absent_rather_than_guessed_when_a_term_is_unknown():
    """0.00 for an unreported buying power would publish a base short by that amount --
    and every percentage on the page divides this number, so a wrong base makes all of
    them wrong AND plausible."""
    assert format_base_composition(base_notional=10_000.0,
                                   available_buying_power=None) is None
    assert format_base_composition(base_notional=None,
                                   available_buying_power=500.0) is None
    assert format_base_composition(base_notional=0.0,
                                   available_buying_power=500.0) is None


def test_the_reserve_and_the_base_line_reconcile_on_screen():
    """The end-to-end property the reader needs: base = managed + free, reserve is a
    share of THAT base, and reserve + to-allocate is the base again."""
    base, bp = 5_369.79, 542.61
    base_line = format_base_composition(base_notional=base, available_buying_power=bp)
    caption = format_reserve_caption(base, 10.0)

    assert '$5,369.79' in base_line and '$5,369.79 base' in caption
    assert '$536.98 held back' in caption
    assert '$4,832.81 investable out of $5,369.79' in caption
    assert round(536.98 + 4_832.81, 2) == round(base, 2)


# ---------------------------------------------------------------------------
# LIVE DELTAS
#
# What each row would CHANGE BY if the current targets were executed. Reported:
# "putting same numbers in target share than current share gives different
# amount" -- AAOI held 38.34% of the label and 38.34% was typed, yet the target
# value came out $184.83 against $529.30 held. Nothing is wrong: the two
# percentages divide different pots (what the label HOLDS vs what it is
# TARGETED), and that label was over its target. The deltas say so on the row:
# share 0.00pp, value -344.47, qty -3.2540.
# ---------------------------------------------------------------------------

def test_the_same_share_can_still_move_real_money():
    """The reported case. A zero share delta beside a large negative money delta is
    the correct reading of an over-allocated label, and it is the pair that explains
    it -- either number alone looks like a bug."""
    d = symbol_delta(weight_pct=38.34, pct_of_label=38.34,
                     target_value=184.83, current_value=529.30,
                     quantity=5, price=105.86)

    assert d.share == pytest.approx(0.0)
    assert d.value == pytest.approx(-344.47)
    assert d.quantity == pytest.approx(184.83 / 105.86 - 5)


def test_the_share_delta_is_in_percentage_POINTS():
    """26.25% -> 27.09% is +0.84 POINTS. Reporting it as +3.2% (a percentage of a
    percentage) would be a third denominator on a page that already has two."""
    d = symbol_delta(weight_pct=27.09, pct_of_label=26.25, target_value=None,
                     current_value=None, quantity=None, price=None)

    assert d.share == pytest.approx(0.84)


def test_the_quantity_delta_is_derived_from_the_money_the_column_prints():
    """target_value / price, not share x something: the money is what the engine
    solves for, so dividing the same number the TARGET VALUE cell shows is what keeps
    the two columns from disagreeing by a rounding step."""
    d = symbol_delta(weight_pct=50.0, pct_of_label=50.0, target_value=1_000.0,
                     current_value=800.0, quantity=8.0, price=100.0)

    assert d.quantity == pytest.approx(2.0)      # 10 target shares - 8 held


def test_an_unmeasurable_delta_is_None_and_never_zero():
    """A symbol with no price has NO quantity delta even though its money delta is
    perfectly well defined. A 0.00 there would claim the position keeps its size while
    its value moves, which cannot happen."""
    d = symbol_delta(weight_pct=50.0, pct_of_label=40.0, target_value=1_000.0,
                     current_value=800.0, quantity=8.0, price=None)

    assert d.share == pytest.approx(10.0)
    assert d.value == pytest.approx(200.0)
    assert d.quantity is None


def test_a_zero_price_does_not_divide():
    d = symbol_delta(weight_pct=50.0, pct_of_label=50.0, target_value=1_000.0,
                     current_value=800.0, quantity=8.0, price=0.0)

    assert d.quantity is None


def test_a_delta_prints_its_sign_but_a_zero_does_not():
    """The sign is what the colour is keyed on, so an unsigned delta beside a number
    is indistinguishable from a second column of that number. A signed ZERO, though,
    invites the reader to look for a direction it is not claiming."""
    assert format_delta(12.3456) == '+12.35'
    assert format_delta(-12.3456) == '-12.35'
    assert format_delta(0.0) == '0.00'
    assert format_delta(None) == ''


def test_an_unmeasurable_delta_draws_nothing_at_all():
    """Empty, not a dash: a dash in a numeric column reads as a value."""
    assert format_delta(None) == ''
    assert format_delta(None, places=4) == ''


def test_green_is_up_red_is_down_and_neither_claims_a_direction_it_lacks():
    assert delta_color(1.0) == 'positive'
    assert delta_color(-1.0) == 'negative'
    assert delta_color(0.0) == 'grey-5'
    assert delta_color(None) == 'grey-5', "unmeasurable must not be coloured as a move"


# ---------------------------------------------------------------------------
# BUG FIX 2026-09-05: "% of label" divided the label's OWN held total, so it
# summed to exactly 100% across the held rows however far the label was from
# its target -- a label holding twice its target read as perfectly balanced.
# ``pct_of_label_target`` divides the label's TARGET money instead.
# ---------------------------------------------------------------------------

def test_pct_of_label_target_sums_past_100_when_the_label_is_over_subscribed():
    """The operator's live case: a label targeting 15% of the pool that actually
    holds ~28% of it. Its three held symbols must read MORE than 100% between
    them, because they hold more than the label's target money."""
    managed = [ManagedLabel('TECH', 10.0), ManagedLabel('REST', 90.0)]
    # base 10,000, no reserve -> TECH's target is 10% = 1,000. It holds 2,000.
    positions = {'AAA': _pos('AAA', 10, 1200.0), 'BBB': _pos('BBB', 10, 800.0)}
    views = build_label_views(managed, {'TECH': ['AAA', 'BBB'], 'REST': []},
                              positions, {}, valuation_mode=VALUATION_MODE_COST,
                              base_notional=10_000.0)

    tech = views[0]
    by = {r.symbol: r for r in tech.rows}
    # The OLD figure, unchanged and still summing to exactly 100.
    assert by['AAA'].pct_of_label == 60.0
    assert by['BBB'].pct_of_label == 40.0
    # The NEW one: 1,200 and 800 against a 1,000 target.
    assert by['AAA'].pct_of_label_target == 120.0
    assert by['BBB'].pct_of_label_target == 80.0
    assert (by['AAA'].pct_of_label_target
            + by['BBB'].pct_of_label_target) == 200.0


def test_pct_of_label_target_reads_under_100_when_the_label_is_under_invested():
    managed = [ManagedLabel('TECH', 50.0), ManagedLabel('REST', 50.0)]
    positions = {'AAA': _pos('AAA', 10, 1000.0)}
    views = build_label_views(managed, {'TECH': ['AAA'], 'REST': []},
                              positions, {}, valuation_mode=VALUATION_MODE_COST,
                              base_notional=10_000.0)

    # Target 50% of 10,000 = 5,000; holding 1,000 is a fifth of it.
    assert views[0].rows[0].pct_of_label_target == 20.0
    # ...while the old column still insists this symbol IS the whole label.
    assert views[0].rows[0].pct_of_label == 100.0


def test_pct_of_label_target_follows_the_cash_reserve():
    """The target money is a share of what the reserve LEAVES, so raising the
    reserve raises every symbol's percentage of it -- same money, smaller pool."""
    managed = [ManagedLabel('TECH', 100.0)]
    positions = {'AAA': _pos('AAA', 10, 1000.0)}
    no_reserve = build_label_views(managed, {'TECH': ['AAA']}, positions, {},
                                   valuation_mode=VALUATION_MODE_COST,
                                   base_notional=10_000.0)
    with_reserve = build_label_views(managed, {'TECH': ['AAA']}, positions, {},
                                     valuation_mode=VALUATION_MODE_COST,
                                     base_notional=10_000.0, unallocated_pct=50.0)

    assert no_reserve[0].rows[0].pct_of_label_target == 10.0     # 1,000 of 10,000
    assert with_reserve[0].rows[0].pct_of_label_target == 20.0   # 1,000 of 5,000


def test_pct_of_label_target_is_zero_when_there_is_no_target_to_divide_by():
    """No base at all, and a label targeting 0%: neither has a denominator, and a
    percentage of nothing is not a number to invent."""
    positions = {'AAA': _pos('AAA', 10, 1000.0)}
    no_base = build_label_views([ManagedLabel('TECH', 100.0)], {'TECH': ['AAA']},
                                positions, {}, valuation_mode=VALUATION_MODE_COST)
    zero_target = build_label_views([ManagedLabel('TECH', 0.0)], {'TECH': ['AAA']},
                                    positions, {}, valuation_mode=VALUATION_MODE_COST,
                                    base_notional=10_000.0)

    assert no_base[0].rows[0].pct_of_label_target == 0.0
    assert zero_target[0].rows[0].pct_of_label_target == 0.0


def test_the_share_point_hint_still_reads_the_COMPOSITION_percentage():
    """``symbol_delta`` subtracts the typed share from ``pct_of_label``; feeding it
    the target-based figure would turn a composition delta into nonsense. Pinned
    because the two fields now sit side by side and are easy to swap."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import symbol_delta

    # 25% typed against a 60% actual share of the label's own money.
    d = symbol_delta(weight_pct=25.0, pct_of_label=60.0, target_value=None,
                     current_value=None, quantity=None, price=None)
    assert d.share == -35.0
