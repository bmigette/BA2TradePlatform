"""The Global / Local toggle among an expanded label's controls on the Portfolio Allocation page.

It decides which money the symbol table's Target value, Target qty and the change printed under them are measured
against: GLOBAL = the label's portfolio target (what Review and Submit solves), LOCAL = the money the label holds now
(what "Rebalance label" solves, engine ``compute_label_rebalance``). Display only: nothing is stored or ordered."""
import pytest

import tests.test_portfolio_allocation_page as pagetests
from tests.test_portfolio_allocation_page import (  # noqa: F401 -- fixtures
    account_id, nicegui_client, run_to_thread_inline)
from tests.test_portfolio_allocation_rebalance_reminder_ui import _quiet
from ba2_trade_platform.core import portfolio_allocation as engine
from ba2_trade_platform.core.portfolio_allocation import (
    VALUATION_MODE_MARKET, LabelTarget, PositionState, SymbolTarget)
from ba2_trade_platform.ui.pages import portfolio_allocation as page
from ba2_trade_platform.ui.utils.portfolio_allocation_view import label_local_target_values


# -- the arithmetic (pure) ---------------------------------------------------------------------

def test_local_targets_split_the_labels_own_money_by_its_shares():
    assert label_local_target_values({'A': 50.0, 'B': 50.0}, {'A': 600.0, 'B': 400.0}) == \
        {'A': pytest.approx(500.0), 'B': pytest.approx(500.0)}


def test_shares_under_100_are_scaled_like_the_label_run_scales_them():
    assert label_local_target_values({'A': 60.0, 'B': 20.0}, {'A': 600.0, 'B': 400.0}) == \
        {'A': pytest.approx(750.0), 'B': pytest.approx(250.0)}


def test_shares_over_100_are_left_as_typed():
    assert label_local_target_values({'A': 80.0, 'B': 80.0}, {'A': 600.0, 'B': 400.0}) == \
        {'A': pytest.approx(800.0), 'B': pytest.approx(800.0)}


def test_a_frozen_symbol_keeps_its_value_and_the_others_share_the_rest():
    assert label_local_target_values({'A': 25.0, 'B': 25.0, 'C': 50.0},
                                     {'A': 600.0, 'B': 400.0, 'C': 1000.0}, frozen={'C'}) == \
        {'A': pytest.approx(500.0), 'B': pytest.approx(500.0), 'C': pytest.approx(1000.0)}


def test_with_every_share_at_zero_there_is_no_local_target():
    assert label_local_target_values({'A': 0.0, 'B': 0.0}, {'A': 600.0, 'B': 400.0}) == {'A': None, 'B': None}


def test_a_label_holding_nothing_has_local_targets_of_zero():
    assert label_local_target_values({'A': 50.0, 'B': 50.0}, {}) == {'A': 0.0, 'B': 0.0}


@pytest.mark.parametrize('weights, values, frozen', [
    ({'A': 50.0, 'B': 50.0}, {'A': 600.0, 'B': 400.0}, set()),
    ({'A': 60.0, 'B': 20.0}, {'A': 600.0, 'B': 400.0}, set()),
    ({'A': 0.0, 'B': 100.0}, {'A': 600.0, 'B': 400.0}, set()),
    ({'A': 25.0, 'B': 25.0, 'C': 50.0}, {'A': 600.0, 'B': 400.0, 'C': 1000.0}, {'C'}),
    ({'A': 30.0, 'B': 30.0, 'C': 20.0}, {'A': 600.0, 'B': 400.0, 'C': 1000.0}, {'A'}),
])
def test_the_local_targets_are_the_label_runs_own_targets(weights, values, frozen):
    """The column must print what "Rebalance label" would aim at, so it is checked against the engine itself."""
    current = {s: PositionState(symbol=s, quantity=v / 10.0, cost_basis=v, price=10.0, transaction_ids=[1])
               for s, v in values.items()}
    label = LabelTarget('core', 30.0, [SymbolTarget(s, w) for s, w in weights.items()])
    plan = engine.compute_label_rebalance(
        label, current, {}, available_buying_power=0.0, allow_fractional=True, default_bp_factor=1.0,
        valuation_mode=VALUATION_MODE_MARKET, frozen_symbols=frozen)
    local = label_local_target_values(weights, values, frozen=frozen)
    assert {r.symbol: pytest.approx(r.target_notional) for r in plan.rows} == local


# -- the toggle on the page ----------------------------------------------------------------------

def _two_labels(account_id):
    """ARK26: 60% of a 10,000 base (6,000) but HOLDING 2,000 (AAPL 1,500 + MSFT 500), shares 50/50."""
    return pagetests._views(
        [pagetests.ManagedLabel('ARK26', 60.0), pagetests.ManagedLabel('Income', 40.0)],
        {'ARK26': ['AAPL', 'MSFT'], 'Income': ['JEPI']},
        weights={'ARK26': {'AAPL': 50.0, 'MSFT': 50.0}, 'Income': {'JEPI': 100.0}},
        positions=[pagetests._pos('AAPL', 15, 1500.0, 1500.0), pagetests._pos('MSFT', 5, 500.0, 500.0),
                   pagetests._pos('JEPI', 10, 1000.0, 1000.0)],
        prices={'AAPL': 100.0, 'MSFT': 100.0, 'JEPI': 100.0})


def _draw(client, account_id, **kwargs):
    with client:
        page._render_labels(account_id, pagetests._payload(_two_labels(account_id), VALUATION_MODE_MARKET,
                                                           base_notional=10_000.0, available_buying_power=1_000.0,
                                                           unallocated_pct=0.0),
                            pagetests._noop_refresh, **kwargs)
    return client.layout


def _table(root, symbol):
    """The table holding ``symbol``: the LAST one drawn, so a second draw into the same client reads the redraw."""
    return [t for t in pagetests._tables(root) if any(r['symbol'] == symbol for r in t.rows)][-1]


def _rows(root, symbol):
    return {r['symbol']: r for r in _table(root, symbol).rows}


def _toggle(root, label_index=0):
    return pagetests._marked(root, page.MARKER_CHANGE_SCOPE)[label_index]


def _toggle_of(root, symbol):
    """The toggle that sits in the same expanded label as ``symbol``'s table."""
    body = _table(root, symbol).parent_slot.parent
    return next(el for el in body.descendants() if page.MARKER_CHANGE_SCOPE in (el._markers or []))


def test_every_expanded_label_has_the_toggle_and_it_starts_on_global(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id)
    toggles = pagetests._marked(root, page.MARKER_CHANGE_SCOPE)
    assert len(toggles) == 2
    assert [t.value for t in toggles] == [page.CHANGE_SCOPE_GLOBAL] * 2
    assert page.CHANGE_SCOPE_TOOLTIP in pagetests._tooltip_texts(root)
    rows = _rows(root, 'AAPL')
    assert rows['AAPL']['target_value'] == 3000.0 and rows['MSFT']['target_value'] == 3000.0
    assert rows['AAPL']['target_quantity'] == 30.0 and rows['AAPL']['value_delta'] == '+1,500.00'


def test_local_measures_target_and_change_against_the_money_the_label_holds(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id)
    with nicegui_client:
        pagetests._drive_value(_toggle_of(root, 'AAPL'), page.CHANGE_SCOPE_LOCAL)
    rows = _rows(root, 'AAPL')
    # 2,000 in the label, 50/50 -> 1,000 each: sell 5 AAPL, buy 5 MSFT
    assert rows['AAPL']['target_value'] == 1000.0 and rows['MSFT']['target_value'] == 1000.0
    assert rows['AAPL']['target_quantity'] == 10.0 and rows['MSFT']['target_quantity'] == 10.0
    assert rows['AAPL']['value_delta'] == '-500.00' and rows['MSFT']['value_delta'] == '+500.00'
    assert rows['AAPL']['qty_delta'] == '-5.0000' and rows['MSFT']['qty_delta'] == '+5.0000'
    # the other label is still measured globally
    assert _rows(root, 'JEPI')['JEPI']['target_value'] == 4000.0


def test_switching_back_to_global_restores_the_portfolio_figures(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id)
    toggle = _toggle_of(root, 'AAPL')
    with nicegui_client:
        pagetests._drive_value(toggle, page.CHANGE_SCOPE_LOCAL)
        pagetests._drive_value(toggle, page.CHANGE_SCOPE_GLOBAL)
    rows = _rows(root, 'AAPL')
    assert rows['AAPL']['target_value'] == 3000.0 and rows['AAPL']['value_delta'] == '+1,500.00'
    assert rows['AAPL']['target_quantity'] == 30.0


def test_an_edit_made_while_local_is_shown_locally_and_survives_the_switch_back(nicegui_client, account_id):
    from ba2_trade_platform.core.portfolio_allocation_store import set_managed_label
    set_managed_label(account_id, 'ARK26', target_pct=60.0)
    root = _draw(nicegui_client, account_id)
    toggle = _toggle_of(root, 'AAPL')
    table = next(t for t in pagetests._tables(root) if any(r['symbol'] == 'AAPL' for r in t.rows))
    with nicegui_client:
        pagetests._drive_value(toggle, page.CHANGE_SCOPE_LOCAL)
        pagetests._emit(table, 'weightChange', ['MSFT', 25.0])       # shares now 50 / 25 -> scaled to 2/3, 1/3
    rows = _rows(root, 'AAPL')
    assert rows['AAPL']['target_value'] == pytest.approx(1333.33) and rows['MSFT']['target_value'] == pytest.approx(666.67)
    with nicegui_client:
        pagetests._drive_value(toggle, page.CHANGE_SCOPE_GLOBAL)
    rows = _rows(root, 'AAPL')
    assert rows['AAPL']['target_value'] == 3000.0 and rows['MSFT']['target_value'] == 1500.0    # 6,000 x 50% / 25%


def test_the_choice_survives_a_redraw_of_the_label_list(nicegui_client, account_id):
    """A refresh (every Submit ends with one) rebuilds the list: the page keeps the set of local labels and hands
    it back, so a label left on Local comes back on Local."""
    chosen = set()
    root = _draw(nicegui_client, account_id, local_change=chosen)
    with nicegui_client:
        pagetests._drive_value(_toggle_of(root, 'AAPL'), page.CHANGE_SCOPE_LOCAL)
    assert chosen == {'ARK26'}
    root = _draw(nicegui_client, account_id, local_change=chosen)      # the redraw, with the page's own set
    assert _toggle_of(root, 'AAPL').value == page.CHANGE_SCOPE_LOCAL
    assert _toggle_of(root, 'JEPI').value == page.CHANGE_SCOPE_GLOBAL
    assert _rows(root, 'AAPL')['AAPL']['target_value'] == 1000.0


def test_the_page_hands_every_refresh_the_same_set(monkeypatch, nicegui_client, account_id):
    _quiet(monkeypatch)
    seen, captured = [], {}
    real = page._render_labels

    def _spy(*args, **kwargs):
        seen.append(kwargs.get('local_change'))
        return real(*args, **kwargs)

    async def _flow(acct, valuation, refresh, **kwargs):
        captured['refresh'] = refresh

    monkeypatch.setattr(page, '_render_labels', _spy)
    monkeypatch.setattr(page, '_open_allocation_flow', _flow)
    root = pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    pagetests._press(pagetests._review_button(root))
    pagetests._run_in_client(nicegui_client, captured['refresh'])
    assert len(seen) == 2 and isinstance(seen[0], set) and seen[0] is seen[1]
