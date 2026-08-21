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
    DEFAULT_MACHINE_LABEL_FAMILIES, GATE_HAS_EXPERTS, GATE_NOT_MANUAL, GATE_NO_ACCOUNT,
    GATE_OK, LEGACY_MACHINE_LABEL_FAMILIES,
    ManagedLabel, build_label_views, collect_managed_symbols, diff_managed_labels,
    evaluate_gate, expert_shortname_families, filter_selectable_labels, is_machine_label,
    managed_total_value, missing_quote_symbols, picker_options, positions_by_symbol,
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


def test_build_label_views_defaults_to_cost_mode():
    """The DB default is 'cost' (spec 5a); the helper agrees so an omitted argument
    can never silently reinterpret the page."""
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)},
                              {'AAPL': 250.0})
    assert views[0].current_value == 1000.0


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
    )
    assert sum(v.current_value for v in views) == 13000.0     # the double count
    assert managed_total_value(views) == 7000.0


def test_managed_total_value_is_the_denominator_pct_of_total_was_computed_with():
    views = build_label_views(
        [ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
        {'ARK26': ['TSLA', 'AAPL'], 'HighRisk': ['TSLA']},
        {'TSLA': _pos('TSLA', 10, 6000.0), 'AAPL': _pos('AAPL', 1, 1000.0)},
        {},
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
                              {})
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
                              {'Mixed': ['AAPL', 'TSLA']}, positions, {})
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
                              {'Flat': ['AAPL', 'TSLA']}, positions, {})
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
                              {'AAPL': _pos('AAPL', 1, 100.0)}, {})
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
