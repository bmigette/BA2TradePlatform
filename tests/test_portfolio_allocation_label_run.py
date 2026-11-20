"""The per-label "rebalance this label only" run of the Portfolio Allocation page: the button among each expanded
label's own buttons, the same dry run and Submit as Review and Submit, scoped to ONE label whose current money is re-split by its symbol
shares. The label's portfolio target, the other labels and the cash reserve are not touched. Page-test harness: a
fake broker account, a bare NiceGUI client, ``asyncio.to_thread`` inline."""
import pytest

import tests.test_portfolio_allocation_page as pagetests
from tests.test_portfolio_allocation_page import (  # noqa: F401 -- fixtures
    account_id, nicegui_client, run_to_thread_inline)
from tests.test_portfolio_allocation_rebalance_reminder_ui import _capture_timers, _fire_once_timers, _quiet
from ba2_trade_platform.core import portfolio_allocation as engine
from ba2_trade_platform.core.allocator_rebalance_reminder import last_rebalance_run_at
from ba2_trade_platform.core.portfolio_allocation import (
    ALLOCATION_MODE_REBALANCE_LABEL, VALUATION_MODE_MARKET, LabelTarget, SymbolTarget)
from ba2_trade_platform.core.portfolio_allocation_store import (
    get_allocation_config, get_managed_labels, get_recent_runs, set_allocation_config, set_managed_label,
    set_symbol_weight)
from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.core.utils import add_label_to_instruments
from ba2_trade_platform.ui.pages import portfolio_allocation as page
from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wizard_module


def _seed(monkeypatch, account_id, *, shared=False):
    """ARK26 (40% of the account) holds AAPL 1,500 + MSFT 500 and wants them 50/50; Income (60%) holds JEPI 1,000."""
    account = pagetests._AllocAccount(
        account_id, {'manual_trading_enabled': True},
        positions=[pagetests._held('AAPL', 15, 1500.0, 1500.0), pagetests._held('MSFT', 5, 500.0, 500.0),
                   pagetests._held('JEPI', 10, 1000.0, 1000.0)],
        prices={'AAPL': 100.0, 'MSFT': 100.0, 'JEPI': 100.0})
    pagetests._use_account(monkeypatch, account)
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    set_managed_label(account_id, 'Income', target_pct=60.0)
    add_label_to_instruments(['AAPL', 'MSFT'], 'ARK26')
    add_label_to_instruments(['JEPI', 'MSFT'] if shared else ['JEPI'], 'Income')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=50.0)
    set_symbol_weight(account_id, 'ARK26', 'MSFT', weight_pct=50.0)
    set_symbol_weight(account_id, 'Income', 'JEPI', weight_pct=50.0 if shared else 100.0)
    if shared:
        set_symbol_weight(account_id, 'Income', 'MSFT', weight_pct=50.0)
    return account


def _labels():
    return [LabelTarget('ARK26', 40.0, [SymbolTarget('AAPL', 50.0), SymbolTarget('MSFT', 50.0)]),
            LabelTarget('Income', 60.0, [SymbolTarget('JEPI', 100.0)])]


def _solve(account_id, labels, scope_label='ARK26'):
    return page._solve_plan(account_id, mode=ALLOCATION_MODE_REBALANCE_LABEL, labels=labels,
                            scope_label=scope_label, amount=0.0, allow_fractional=True,
                            valuation_mode=VALUATION_MODE_MARKET, unallocated_pct=25.0)


# -- the solve ---------------------------------------------------------------------------------

def test_the_solve_re_splits_the_labels_own_money_and_leaves_the_rest_of_the_account_alone(monkeypatch, account_id):
    _seed(monkeypatch, account_id)
    base, plan, current, hours = _solve(account_id, _labels())

    assert plan.base_notional == pytest.approx(2_000.0)         # AAPL 1,500 + MSFT 500, not 40% of the account
    assert plan.reserved_pct == 0.0                             # the account's reserve plays no part
    rows = {r.symbol: r for r in plan.rows}
    assert sorted(rows) == ['AAPL', 'MSFT']                     # Income's JEPI is not in the plan at all
    assert rows['AAPL'].side == OrderDirection.SELL and rows['AAPL'].delta_quantity == pytest.approx(-5.0)
    assert rows['MSFT'].side == OrderDirection.BUY and rows['MSFT'].delta_quantity == pytest.approx(5.0)
    assert hours.is_open is True and 'AAPL' in current
    assert base.base_notional == pytest.approx(53_000.0)        # the BASE is still the account's own figure


def test_the_solve_refuses_a_label_that_is_no_longer_managed(monkeypatch, account_id):
    _seed(monkeypatch, account_id)
    with pytest.raises(ValueError, match="no longer managed"):
        _solve(account_id, _labels(), scope_label='Gone')


def test_the_solve_refuses_a_label_that_shares_a_symbol_with_another_label(monkeypatch, account_id):
    _seed(monkeypatch, account_id)
    labels = _labels()
    labels[1].symbols.append(SymbolTarget('MSFT', 0.0))
    with pytest.raises(ValueError, match="cannot be rebalanced alone"):
        _solve(account_id, labels)


def test_the_solve_refuses_a_label_member_that_has_no_price(monkeypatch, account_id):
    """The label's money cannot be split without it: the others would trade against the account's cash."""
    account = _seed(monkeypatch, account_id)
    del account._prices['MSFT']
    with pytest.raises(ValueError, match="no price for MSFT"):
        _solve(account_id, _labels())


# -- the flow: dry run, Submit, what is recorded ------------------------------------------------

def _open_label_flow(monkeypatch, nicegui_client, account_id, label='ARK26'):
    opened, pending = {}, []
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.update(kw, base=a[0], plan=a[1]))
    monkeypatch.setattr(page.ui, 'timer', lambda _delay, callback, once=False: pending.append(callback))
    pagetests._run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_MARKET, pagetests._noop_refresh,
        mode=ALLOCATION_MODE_REBALANCE_LABEL, scope_label=label))
    return opened, pending


def test_the_label_flow_opens_the_same_dry_run_scoped_to_the_label(monkeypatch, nicegui_client, account_id):
    _seed(monkeypatch, account_id)
    pagetests._capture_notifications(monkeypatch)
    opened, _pending = _open_label_flow(monkeypatch, nicegui_client, account_id)

    assert sorted(r.symbol for r in opened['plan'].rows) == ['AAPL', 'MSFT']
    assert opened['scope_label'] == 'ARK26'
    assert 'ARK26' in opened['title']
    assert callable(opened['on_refresh']) and callable(opened['on_submit']) and callable(opened['on_validate'])
    # Refresh re-solves the SAME scope, not the whole account
    plan, _market = opened['on_refresh'](True)
    assert sorted(r.symbol for r in plan.rows) == ['AAPL', 'MSFT'] and plan.base_notional == pytest.approx(2_000.0)


def test_submitting_trades_the_label_only_and_records_a_label_run(monkeypatch, nicegui_client, account_id):
    account = _seed(monkeypatch, account_id)
    pagetests._capture_notifications(monkeypatch)
    set_allocation_config(account_id, unallocated_pct=25.0)
    opened, pending = _open_label_flow(monkeypatch, nicegui_client, account_id)

    opened['on_submit'](opened['plan'])
    pagetests._run_in_client(nicegui_client, pending.pop())

    assert sorted((symbol, side, quantity) for symbol, side, quantity in account.submitted) == \
        [('AAPL', OrderDirection.SELL, 5.0), ('MSFT', OrderDirection.BUY, 5.0)]
    runs = get_recent_runs(account_id)
    assert len(runs) == 1
    assert runs[0].mode == ALLOCATION_MODE_REBALANCE_LABEL and runs[0].scope_label == 'ARK26'
    assert runs[0].base_notional == pytest.approx(2_000.0)      # the money the run split, not the account's base
    # nothing the label run did not own was rewritten
    assert {r.label: r.target_pct for r in get_managed_labels(account_id)} == {'ARK26': 40.0, 'Income': 60.0}
    assert get_allocation_config(account_id).unallocated_pct == 25.0
    # ...and it is not "the last full rebalance" the reminder counts from
    assert last_rebalance_run_at(account_id) is None


def test_the_label_flow_refuses_an_unknown_label_before_solving(monkeypatch, nicegui_client, account_id):
    _seed(monkeypatch, account_id)
    sent = pagetests._capture_notifications(monkeypatch)
    opened, _pending = _open_label_flow(monkeypatch, nicegui_client, account_id, label='Gone')
    assert opened == {}
    assert any('Gone' in message and kind == 'warning' for message, kind in sent)


def test_the_label_flow_refuses_a_shared_symbol_and_names_it(monkeypatch, nicegui_client, account_id):
    _seed(monkeypatch, account_id, shared=True)
    sent = pagetests._capture_notifications(monkeypatch)
    opened, _pending = _open_label_flow(monkeypatch, nicegui_client, account_id)
    assert opened == {}
    assert any('MSFT (Income)' in message and kind == 'warning' for message, kind in sent)


# -- the button among each expanded label's buttons ------------------------------------------------

def _two_labels(account_id):
    return pagetests._views([pagetests.ManagedLabel('ARK26', 60.0), pagetests.ManagedLabel('Income', 40.0)],
                            {'ARK26': ['AAPL', 'MSFT'], 'Income': ['JEPI']},
                            weights={'ARK26': {'AAPL': 50.0, 'MSFT': 50.0}, 'Income': {'JEPI': 100.0}})


def _draw_with_run(client, account_id, views, on_label_run):
    with client:
        page._render_labels(account_id, pagetests._payload(views, VALUATION_MODE_MARKET, base_notional=10_000.0,
                                                           available_buying_power=1_000.0, unallocated_pct=0.0),
                            pagetests._noop_refresh, on_label_run=on_label_run)
    return client.layout


def test_every_expanded_label_carries_the_label_run_button_and_it_names_its_label(nicegui_client, account_id):
    from nicegui import ui
    pressed = []
    root = _draw_with_run(nicegui_client, account_id, _two_labels(account_id), pressed.append)
    buttons = pagetests._marked(root, page.MARKER_LABEL_RUN)
    assert len(buttons) == 2 and all(isinstance(b, ui.button) for b in buttons)
    assert [b.text for b in buttons] == [page.LABEL_RUN_BUTTON_LABEL] * 2
    # WITH the label's other buttons (the row that holds "Fetch data"), not on the header line
    for button in buttons:
        siblings = list(button.parent_slot.parent.descendants())
        assert any(page.MARKER_FETCH_STATS in (el._markers or []) for el in siblings)
    for header in pagetests._marked(root, page.MARKER_BAR_ROW):
        assert not [el for el in header.descendants() if page.MARKER_LABEL_RUN in (el._markers or [])]
    assert page.LABEL_RUN_TOOLTIP in pagetests._tooltip_texts(root)
    with nicegui_client:
        for button in buttons:
            pagetests._press(button)
    assert sorted(pressed) == ['ARK26', 'Income']


def test_without_a_run_handler_no_label_run_button_is_drawn(nicegui_client, account_id):
    root = pagetests._draw(nicegui_client, account_id, _two_labels(account_id))
    assert pagetests._marked(root, page.MARKER_LABEL_RUN) == []


def test_the_page_button_runs_the_label_flow_deferred_out_of_the_label_list(monkeypatch, nicegui_client, account_id):
    """The button lives in the label list, which every refresh CLEARS: the dry run must be started from the
    never-cleared dialog host (like the reminder's Review), or the refresh that ends a Submit would delete it."""
    _quiet(monkeypatch)
    root = pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    calls = []

    async def _spy(acct, valuation, refresh, **kwargs):
        calls.append((acct, valuation, kwargs.get('mode'), kwargs.get('scope_label')))

    monkeypatch.setattr(page, '_open_allocation_flow', _spy)
    button = pagetests._marked(root, page.MARKER_LABEL_RUN)[0]
    timers = _capture_timers(monkeypatch)
    with nicegui_client:
        pagetests._press(button)
    assert calls == []                                          # deferred
    assert len(timers) == 1
    host = timers[0].parent
    assert button not in host.descendants() and host not in button.ancestors()
    _fire_once_timers(nicegui_client, timers)
    assert calls == [(account_id, VALUATION_MODE_MARKET, ALLOCATION_MODE_REBALANCE_LABEL, 'ARK26')]


def test_the_page_button_obeys_the_what_if_interlock(monkeypatch, nicegui_client, account_id):
    _quiet(monkeypatch)
    root = pagetests._drawn_page(monkeypatch, nicegui_client, account_id)
    sent = pagetests._capture_notifications(monkeypatch)
    calls = []

    async def _spy(*a, **k):
        calls.append(a)

    monkeypatch.setattr(page, '_open_allocation_flow', _spy)
    with nicegui_client:
        pagetests._drive_value(pagetests._marked(root, page.MARKER_SIM_INPUT)[0], 50_000.0)
        pagetests._drive_value(pagetests._marked(root, page.MARKER_SIM_TOGGLE)[0], True)
    timers = _capture_timers(monkeypatch)
    with nicegui_client:
        pagetests._press(pagetests._marked(nicegui_client.layout, page.MARKER_LABEL_RUN)[0])
    _fire_once_timers(nicegui_client, timers)
    assert calls == []
    assert any(kind == 'warning' for _message, kind in sent)


# -- the dry run says what money it is splitting ---------------------------------------------------

def test_the_dry_run_states_the_labels_money_above_the_orders(monkeypatch, nicegui_client, account_id):
    _seed(monkeypatch, account_id)
    base, plan, _current, hours = _solve(account_id, _labels())
    with nicegui_client:
        wizard_module.open_allocation_wizard(
            base, plan, market=page._market_gate_for(hours), on_refresh=lambda fractional: (plan, None),
            on_submit=lambda selected: None, scope_label='ARK26')
    notes = pagetests._marked(nicegui_client.layout, wizard_module.MARKER_LABEL_RUN_NOTE)
    assert [n._text for n in notes] == [wizard_module.LABEL_RUN_NOTE_FMT.format(label='ARK26', money=2_000.0)]


def test_a_full_run_draws_no_label_note(monkeypatch, nicegui_client, account_id):
    _seed(monkeypatch, account_id)
    base, plan, _current, hours = page._solve_plan(
        account_id, mode=engine.ALLOCATION_MODE_REBALANCE, labels=_labels(), scope_label=None, amount=0.0,
        allow_fractional=True, valuation_mode=VALUATION_MODE_MARKET)
    with nicegui_client:
        wizard_module.open_allocation_wizard(
            base, plan, market=page._market_gate_for(hours), on_refresh=lambda fractional: (plan, None),
            on_submit=lambda selected: None)
    assert pagetests._marked(nicegui_client.layout, wizard_module.MARKER_LABEL_RUN_NOTE) == []
