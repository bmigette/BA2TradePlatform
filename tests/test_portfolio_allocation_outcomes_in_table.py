"""A submitted row reports itself IN the dry-run table: icon, marking, details.

There is no results popup any more (operator, 2026-10-03). The pure decisions
(``ui/utils/outcome_view.py``) are tested without NiceGUI; the wizard glue is driven
through a real bare client, like the other wizard tests.
"""
from datetime import datetime

import pytest

from ba2_trade_platform.core import portfolio_allocation_service as svc
from ba2_trade_platform.core.portfolio_allocation import (
    AllocationPlan, AllocationRow, BaseSnapshot, VALUATION_MODE_MARKET,
)
from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
from ba2_trade_platform.ui.utils import outcome_view as ov
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    MARKET_GATE_OPEN, MarketGateResult,
)

ALL_STATUSES = (svc.OUTCOME_SUBMITTED, svc.OUTCOME_PARTIAL, svc.OUTCOME_SKIPPED,
                svc.OUTCOME_FAILED, svc.OUTCOME_WASHTRADE_LOCKED,
                svc.OUTCOME_UNACTIONABLE)


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-pf-outcomes'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _outcome(symbol='AAPL', status=svc.OUTCOME_SUBMITTED, **kw):
    kw.setdefault('action', 'new')
    kw.setdefault('quantity', 10.0)
    return svc.RowOutcome(symbol=symbol, status=status, **kw)


# -- icon mapping: TOTAL -------------------------------------------------------

def test_every_service_status_has_its_own_icon():
    for status in ALL_STATUSES:
        icon = ov.outcome_icon(status)
        assert icon.icon and icon.colour.startswith('#') and icon.label
        assert not icon.label.startswith('unknown'), status
    assert set(ALL_STATUSES) | {ov.STATUS_PENDING, ov.STATUS_NOT_SENT,
                               ov.STATUS_UNKNOWN_CHECK_BROKER} == set(ov.KNOWN_STATUSES)
    icons = {ov.outcome_icon(s).icon for s in ov.KNOWN_STATUSES}
    assert len(icons) == len(ov.KNOWN_STATUSES)   # no two states share an icon


@pytest.mark.parametrize('status', ['brand_new_status', '', None])
def test_an_unknown_status_is_a_neutral_warning_not_a_crash_or_a_blank(status):
    icon = ov.outcome_icon(status)
    assert icon.icon == 'help_outline'
    assert icon.label.startswith('unknown status')
    if status:
        assert status in icon.label


def test_failure_is_red_and_the_not_sent_states_are_amber():
    assert ov.outcome_icon(svc.OUTCOME_FAILED).colour == ov.RED
    assert ov.outcome_icon(svc.OUTCOME_SUBMITTED).colour == ov.GREEN
    for status in (svc.OUTCOME_UNACTIONABLE, svc.OUTCOME_WASHTRADE_LOCKED,
                   svc.OUTCOME_PARTIAL):
        assert ov.outcome_icon(status).colour == ov.AMBER


# -- row marking: red for FAILED only -------------------------------------------

def test_only_failed_rows_are_marked_red():
    assert ov.row_mark_class(svc.OUTCOME_FAILED) == 'pf-row-failed'
    for status in ALL_STATUSES + (ov.STATUS_PENDING, 'mystery', None):
        if status != svc.OUTCOME_FAILED:
            assert ov.row_mark_class(status) != 'pf-row-failed', status


def test_unactionable_and_locked_rows_get_the_softer_amber_marking():
    for status in (svc.OUTCOME_UNACTIONABLE, svc.OUTCOME_WASHTRADE_LOCKED):
        assert ov.row_mark_class(status) == 'pf-row-alert'
    for status in (svc.OUTCOME_SUBMITTED, svc.OUTCOME_SKIPPED, svc.OUTCOME_PARTIAL):
        assert ov.row_mark_class(status) == ''


def test_every_icon_colour_has_a_quasar_palette_name():
    for status in ov.KNOWN_STATUSES:
        assert ov.outcome_icon(status).quasar_color != 'grey-5' or status in (
            ov.STATUS_PENDING, svc.OUTCOME_SKIPPED, ov.STATUS_NOT_SENT)


def test_the_marking_css_exists_and_is_not_phone_gated():
    css = wiz.ROW_STATE_CSS
    assert '.pf-row-failed' in css and '.pf-row-alert' in css
    assert 'rgba(239,68,68' in css and 'rgba(245,158,11' in css
    assert '@media' not in css


# -- details -------------------------------------------------------------------

def _as_dict(details):
    return dict(details)


def test_details_for_a_failed_row_carry_the_full_message_and_ids():
    outcome = _outcome(status=svc.OUTCOME_FAILED, path='whole', order_ids=[101, 102],
                       message='insufficient buying power: ' + 'x' * 400)
    d = _as_dict(ov.outcome_details(outcome, symbol='AAPL', run_id=7,
                                    when=datetime(2026, 10, 3, 9, 30, 1)))
    assert d['Symbol'] == 'AAPL' and d['Action'] == 'new'
    assert 'FAILED' in d['Status'] and svc.OUTCOME_FAILED in d['Status']
    assert d['Planned qty'] == '10.0000'
    assert d['Path'] == 'whole' and d['Order id(s)'] == '101, 102'
    assert d['Message'].endswith('x' * 400)            # never truncated
    assert d['Run'] == '7' and d['Time'] == '2026-10-03 09:30:01'


def test_an_unreported_fill_is_a_dash_never_zero():
    d = _as_dict(ov.outcome_details(_outcome(filled_quantity=None), symbol='AAPL',
                                    run_id=1))
    assert d['Filled qty'] == '-'
    d = _as_dict(ov.outcome_details(_outcome(filled_quantity=1.5), symbol='AAPL',
                                    run_id=1))
    assert d['Filled qty'] == '1.5000'
    d = _as_dict(ov.outcome_details(_outcome(filled_quantity=0.0), symbol='AAPL',
                                    run_id=1))
    assert d['Filled qty'] == '0.0000'                 # a real zero stays a zero


@pytest.mark.parametrize('status', ALL_STATUSES)
def test_details_exist_for_every_status_even_with_every_field_empty(status):
    outcome = svc.RowOutcome(symbol='X', action='', status=status)
    details = ov.outcome_details(outcome, symbol='X', run_id=None)
    d = _as_dict(details)
    assert d['Message'] == '-' and d['Path'] == '-' and d['Order id(s)'] == '-'
    assert d['Run'] == '-' and d['Time'] == '-'
    assert ov.outcome_details_text(details)             # copyable, non-empty


def test_unactionable_details_spell_out_why():
    outcome = _outcome(status=svc.OUTCOME_UNACTIONABLE, action=svc.ACTION_UNACTIONABLE,
                       transaction_ids=[41],
                       message=svc.UNACTIONABLE_OPTION_HOLDING_FMT.format(
                           quantity=100.0, symbol='AAPL', ids='41'))
    d = _as_dict(ov.outcome_details(outcome, symbol='AAPL', run_id=31))
    assert '100 share(s) of AAPL' in d['Message'] and 'transaction 41' in d['Message']
    assert d['Transaction id(s)'] == '41'


def test_details_never_raise_for_a_row_with_no_outcome():
    d = _as_dict(ov.outcome_details(None, symbol='AAPL', run_id=3))
    assert d['Symbol'] == 'AAPL' and 'yet' in d['Message']


def test_copy_text_is_one_label_value_per_line():
    text = ov.outcome_details_text([('Symbol', 'AAPL'), ('Filled qty', '-')])
    assert text == 'Symbol: AAPL\nFilled qty: -'


def test_the_copy_handler_runs_in_the_tap_and_works_over_plain_http():
    js = ov.copy_click_js('a "quoted"\nline')
    # synchronous execCommand inside the click (iOS honours only that), on a READONLY
    # textarea so the keyboard does not pop up, with an explicit selection range
    assert 'execCommand("copy")' in js
    assert 'setAttribute("readonly"' in js and 'setSelectionRange(0, t.length)' in js
    # the async API is only a fallback, and only in a secure context
    assert js.index('execCommand') < js.index('navigator.clipboard')
    assert 'isSecureContext' in js
    assert 'emit({ok: ' in js                      # the verdict goes back to the server
    assert '"a \\"quoted\\"\\nline"' in js
    assert js.count('{') == js.count('}') and js.count('(') == js.count(')')


@pytest.mark.parametrize('args,ok', [([{'ok': True}], True), ({'ok': True}, True),
                                     (True, True), ([{'ok': False}], False),
                                     ([], False), (None, False), ('yes', False)])
def test_copy_success_is_only_ever_an_explicit_true(args, ok):
    assert ov.copy_succeeded(args) is ok


# -- severity: the WORST outcome wins ---------------------------------------------

def test_severity_is_total_and_ordered_worst_first():
    order = [svc.OUTCOME_FAILED, ov.STATUS_UNKNOWN_CHECK_BROKER, svc.OUTCOME_UNACTIONABLE,
             svc.OUTCOME_WASHTRADE_LOCKED, svc.OUTCOME_PARTIAL, svc.OUTCOME_SUBMITTED,
             svc.OUTCOME_SKIPPED, ov.STATUS_NOT_SENT, ov.STATUS_PENDING]
    ranks = [ov.status_severity(s) for s in order]
    assert ranks == sorted(ranks, reverse=True) and len(set(ranks)) == len(order)
    # every status the module knows is ranked; an unknown one is just below FAILED
    assert set(order) == set(ov.KNOWN_STATUSES)
    for unknown in ('something_new', '', None):
        rank = ov.status_severity(unknown)
        assert ov.status_severity(svc.OUTCOME_FAILED) > rank > \
            ov.status_severity(svc.OUTCOME_UNACTIONABLE)


def test_the_worse_of_two_outcomes_for_one_symbol_wins():
    failed = _outcome('A', svc.OUTCOME_FAILED)
    sent = _outcome('A', svc.OUTCOME_SUBMITTED)
    assert ov.worst_outcome(failed, sent) is failed          # FAILED then SUBMITTED
    assert ov.worst_outcome(sent, failed) is failed          # SUBMITTED then FAILED
    assert ov.worst_outcome(None, sent) is sent
    assert ov.worst_outcome(sent, None) is sent
    later = _outcome('A', svc.OUTCOME_SUBMITTED, message='later')
    assert ov.worst_outcome(sent, later) is later            # a tie: the fresher one
    mystery = _outcome('A', 'mystery')
    assert ov.worst_outcome(sent, mystery) is mystery        # unknown outranks green


# -- the wizard glue -----------------------------------------------------------

def _plan():
    return AllocationPlan(
        rows=[AllocationRow(symbol=s, price=160.0, delta_quantity=10.0,
                            side=OrderDirection.BUY, estimated_value=1600.0,
                            bp_cost=1600.0, bp_factor=1.0)
              for s in ('AAPL', 'MSFT', 'KO')],
        base_notional=10_000.0, available_buying_power=10_000.0,
        required_buying_power=4800.0, bp_usage_pct=48.0, total_buy_value=4800.0)


def _open(nicegui_client):
    base = BaseSnapshot(available_buying_power=10_000.0, managed_value=0.0,
                        base_notional=10_000.0, default_bp_factor=1.0,
                        valuation_mode=VALUATION_MODE_MARKET, cash=10_000.0)
    gate = MarketGateResult(allowed=True, reason_code=MARKET_GATE_OPEN, message='')
    with nicegui_client:
        wizard = wiz.AllocationWizard(base, _plan(), market=gate,
                                      on_refresh=lambda f: (_plan(), gate),
                                      on_submit=lambda p: None)
        wizard.open()
    return wizard


def _classes(element):
    return ' '.join(element._classes)


def test_a_failed_row_is_marked_red_and_only_that_row(nicegui_client):
    wizard = _open(nicegui_client)
    with nicegui_client:
        wizard.begin_submit()
        wizard.set_row_outcome(_outcome('AAPL', svc.OUTCOME_SUBMITTED))
        wizard.set_row_outcome(_outcome('MSFT', svc.OUTCOME_FAILED, message='nope'))
        wizard.set_row_outcome(_outcome('KO', svc.OUTCOME_WASHTRADE_LOCKED))
    assert 'pf-row-failed' not in _classes(wizard._row_elements['AAPL'])
    assert 'pf-row-failed' in _classes(wizard._row_elements['MSFT'])
    assert 'pf-row-alert' in _classes(wizard._row_elements['KO'])
    assert 'pf-row-failed' not in _classes(wizard._row_elements['KO'])
    # each row's result cell says its own thing
    assert wizard._result_cells['MSFT'].text == 'FAILED'


def test_the_icon_is_hidden_until_submit_then_spins_then_shows_the_verdict(
        nicegui_client):
    wizard = _open(nicegui_client)
    button, tip = wizard._result_icons['AAPL']
    assert button.visible is False
    with nicegui_client:
        wizard.begin_submit()
    assert button.visible is True and 'loading' in button._props
    assert button._props['icon'] == ov.outcome_icon(ov.STATUS_PENDING).icon
    with nicegui_client:
        wizard.set_row_outcome(_outcome('AAPL', svc.OUTCOME_FAILED))
    assert 'loading' not in button._props
    assert button._props['icon'] == ov.outcome_icon(svc.OUTCOME_FAILED).icon
    assert button._props['color'] == 'negative'


def test_an_outcome_for_a_row_not_in_the_table_is_ignored(nicegui_client):
    wizard = _open(nicegui_client)
    with nicegui_client:
        wizard.set_row_outcome(_outcome('NOT_DRAWN', svc.OUTCOME_FAILED))


def _dialog_texts(client):
    from nicegui import ui
    return [el._text for el in client.layout.descendants()
            if isinstance(el, ui.label) and el._text]


def test_tapping_the_icon_opens_details_with_a_dash_for_an_unreported_fill(
        nicegui_client):
    wizard = _open(nicegui_client)
    with nicegui_client:
        wizard.begin_submit()
        wizard.set_row_outcome(_outcome('AAPL', svc.OUTCOME_SUBMITTED,
                                        order_ids=[101]), when=datetime(2026, 10, 3, 9, 30))
        wizard.finish_submit('Run 9: 1 sent.', run_id=9, outcomes=[], on_retry=None)
        button, _tip = wizard._result_icons['AAPL']
        listener = next(l for l in button._event_listeners.values()
                        if l.type.split('.')[0] == 'click')
        listener.handler(None)
    texts = _dialog_texts(nicegui_client)
    assert 'AAPL - sent' in texts
    assert '101' in texts and '9' in texts
    filled = [el._text for el in nicegui_client.layout.descendants()
              if wiz.MARKER_OUTCOME_FILLED in getattr(el, '_markers', [])]
    assert filled == ['-']
    assert any(getattr(el, '_props', {}).get('label') == 'Copy details'
               for el in nicegui_client.layout.descendants())


def test_details_open_for_a_row_that_has_no_outcome_yet(nicegui_client):
    wizard = _open(nicegui_client)
    with nicegui_client:
        wizard._open_outcome_details('AAPL')
        wizard._open_outcome_details('NOT_A_ROW')
    assert any('yet' in t for t in _dialog_texts(nicegui_client))


# -- retry in the dialog footer ----------------------------------------------------

def _retry_buttons(client):
    return [el for el in client.layout.descendants()
            if wiz.MARKER_OUTCOME_RETRY in getattr(el, '_markers', [])]


def test_retry_is_offered_only_when_something_failed(nicegui_client):
    wizard = _open(nicegui_client)
    with nicegui_client:
        wizard.finish_submit('Run 1', run_id=1, on_retry=lambda s: None,
                             outcomes=[_outcome('A', svc.OUTCOME_SUBMITTED),
                                       _outcome('B', svc.OUTCOME_WASHTRADE_LOCKED),
                                       _outcome('C', svc.OUTCOME_UNACTIONABLE)])
    assert _retry_buttons(nicegui_client) == []
    with nicegui_client:
        wizard.finish_submit('Run 2', run_id=2, on_retry=lambda s: None,
                             outcomes=[_outcome('A', svc.OUTCOME_FAILED),
                                       _outcome('B', svc.OUTCOME_FAILED)])
    buttons = _retry_buttons(nicegui_client)
    assert len(buttons) == 1
    assert buttons[0]._props['label'] == wiz.RETRY_FAILED_FMT.format(count=2)


def test_no_retry_without_a_callback(nicegui_client):
    wizard = _open(nicegui_client)
    with nicegui_client:
        wizard.finish_submit('Run 1', run_id=1, outcomes=[_outcome('A', svc.OUTCOME_FAILED)])
    assert _retry_buttons(nicegui_client) == []


def test_retry_closes_the_dialog_first_and_hands_back_the_failed_symbols(
        nicegui_client):
    wizard = _open(nicegui_client)
    order = []
    wizard.dialog.close = lambda: order.append('closed')
    with nicegui_client:
        wizard.finish_submit('Run 1', run_id=1,
                             on_retry=lambda symbols: order.append(list(symbols)),
                             outcomes=[_outcome('A', svc.OUTCOME_FAILED),
                                       _outcome('B', svc.OUTCOME_SUBMITTED)])
        button = _retry_buttons(nicegui_client)[0]
        next(l for l in button._event_listeners.values()
             if l.type.split('.')[0] == 'click').handler(None)
    assert order == ['closed', ['A']]


def test_a_second_finish_replaces_the_retry_button_rather_than_stacking(nicegui_client):
    wizard = _open(nicegui_client)
    for _ in range(2):
        with nicegui_client:
            wizard.finish_submit('x', run_id=1, on_retry=lambda s: None,
                                 outcomes=[_outcome('A', svc.OUTCOME_FAILED)])
    assert len(_retry_buttons(nicegui_client)) == 1


def test_the_module_has_no_results_popup_left():
    assert not hasattr(wiz, 'render_outcomes')
    assert callable(wiz.notify_outcomes)


# -- the REAL wizard: rows that were never sent, a dead run, a refresh ------------------

def _run_refresh(client, wizard, monkeypatch):
    monkeypatch.setattr(wiz.ui, 'notify', lambda *a, **k: None)
    async def go():
        with client:
            await wizard._refresh(False)
    asyncio.run(go())


import asyncio  # noqa: E402


def _submit_with(nicegui_client, untick=()):
    """Open the real wizard, un-tick ``untick`` and press the real Submit."""
    sent = []
    base = BaseSnapshot(available_buying_power=10_000.0, managed_value=0.0,
                        base_notional=10_000.0, default_bp_factor=1.0,
                        valuation_mode=VALUATION_MODE_MARKET, cash=10_000.0)
    gate = MarketGateResult(allowed=True, reason_code=MARKET_GATE_OPEN, message='')
    with nicegui_client:
        wizard = wiz.AllocationWizard(
            base, _plan(), market=gate, on_refresh=lambda f: (_plan(), gate),
            on_submit=lambda plan: sent.append([r.symbol for r in plan.rows]))
        wizard.open()
        for symbol in untick:
            wizard._toggle(symbol, False)
        wizard._submit()
    return wizard, sent


def test_only_the_rows_being_sent_spin(nicegui_client):
    wizard, sent = _submit_with(nicegui_client, untick=('KO',))
    assert sent == [['AAPL', 'MSFT']]
    for symbol in ('AAPL', 'MSFT'):
        button, _tip = wizard._result_icons[symbol]
        assert button.visible and 'loading' in button._props, symbol
        assert wizard._result_cells[symbol].text == wiz.SUBMIT_PENDING_TEXT
    ko_button, _ = wizard._result_icons['KO']
    assert ko_button.visible is False and 'loading' not in ko_button._props
    assert not wizard._result_cells['KO'].text


def test_an_unticked_row_reads_not_sent_after_the_run_and_is_never_red(nicegui_client):
    wizard, _sent = _submit_with(nicegui_client, untick=('KO',))
    with nicegui_client:
        for symbol in ('AAPL', 'MSFT'):
            wizard.set_row_outcome(_outcome(symbol, svc.OUTCOME_SUBMITTED))
        wizard.finish_submit('Run 1', run_id=1, outcomes=[], on_retry=None)
    # KO was never in the run: no spinner now or later, no marking
    button, _ = wizard._result_icons['KO']
    assert 'loading' not in button._props and button.visible is False
    assert 'pf-row-failed' not in _classes(wizard._row_elements['KO'])
    assert 'pf-row-alert' not in _classes(wizard._row_elements['KO'])
    # ...and no row is left spinning
    for symbol in ('AAPL', 'MSFT', 'KO'):
        assert 'loading' not in wizard._result_icons[symbol][0]._props, symbol


def test_a_run_the_gate_refused_leaves_no_row_spinning(nicegui_client):
    """``_do_submit`` finishes with the blocked reason and no outcomes at all."""
    wizard, _sent = _submit_with(nicegui_client)
    with nicegui_client:
        wizard.finish_submit('The market closed while this dialog was open.')
    for symbol in ('AAPL', 'MSFT', 'KO'):
        button, tip = wizard._result_icons[symbol]
        assert 'loading' not in button._props, symbol
        assert button._props['icon'] == ov.outcome_icon(ov.STATUS_NOT_SENT).icon
        assert wizard._result_cells[symbol].text == wiz.SUBMIT_NOT_SENT_TEXT
        assert 'pf-row-failed' not in _classes(wizard._row_elements[symbol])


def test_a_run_that_died_midway_says_check_the_broker_on_the_unreported_rows(
        nicegui_client):
    """The exception path. AAPL reported; MSFT and KO did not -- an order may or may
    not be at the broker for them."""
    wizard, _sent = _submit_with(nicegui_client)
    with nicegui_client:
        wizard.set_row_outcome(_outcome('AAPL', svc.OUTCOME_SUBMITTED))
        wizard.finish_submit('Submission failed: boom', interrupted=True)
    aapl, _ = wizard._result_icons['AAPL']
    assert aapl._props['color'] == 'positive'                       # what it reported
    for symbol in ('MSFT', 'KO'):
        button, tip = wizard._result_icons[symbol]
        assert 'loading' not in button._props
        assert button._props['icon'] == ov.outcome_icon(ov.STATUS_UNKNOWN_CHECK_BROKER).icon
        assert button._props['color'] == 'warning'
        assert wizard._result_cells[symbol].text == wiz.SUBMIT_UNKNOWN_TEXT
        assert 'pf-row-alert' in _classes(wizard._row_elements[symbol])
        assert 'pf-row-failed' not in _classes(wizard._row_elements[symbol])
    assert 'pf-row-alert' not in _classes(wizard._row_elements['AAPL'])
    # the details say what to do about it
    with nicegui_client:
        wizard._open_outcome_details('MSFT')
    assert any('check the broker' in t for t in _dialog_texts(nicegui_client))


def test_a_refresh_after_a_run_keeps_every_rows_result(nicegui_client, monkeypatch):
    wizard, _sent = _submit_with(nicegui_client, untick=('KO',))
    with nicegui_client:
        wizard.set_row_outcome(_outcome('AAPL', svc.OUTCOME_FAILED, message='refused'))
        wizard.set_row_outcome(_outcome('MSFT', svc.OUTCOME_SUBMITTED, order_ids=[7]))
        wizard.finish_submit('Run 3', run_id=3, outcomes=[_outcome('AAPL', svc.OUTCOME_FAILED)],
                             on_retry=lambda s: None)
    old_rows = dict(wizard._row_elements)

    _run_refresh(nicegui_client, wizard, monkeypatch)

    assert wizard._row_elements['AAPL'] is not old_rows['AAPL']      # really redrawn
    assert wizard._result_cells['AAPL'].text == 'FAILED'
    assert 'pf-row-failed' in _classes(wizard._row_elements['AAPL'])
    assert wizard._result_icons['AAPL'][0]._props['icon'] == \
        ov.outcome_icon(svc.OUTCOME_FAILED).icon
    assert wizard._result_cells['MSFT'].text == 'sent'
    assert 'pf-row-failed' not in _classes(wizard._row_elements['MSFT'])
    # KO was un-ticked: not in the run, so nothing is said about it -- no spinner, no icon
    assert not wizard._result_cells['KO'].text
    assert wizard._result_icons['KO'][0].visible is False
    # the details still open on the redrawn rows, and Submit stays off
    with nicegui_client:
        wizard._open_outcome_details('AAPL')
    assert 'AAPL - FAILED' in _dialog_texts(nicegui_client)
    assert wizard._submit_button.enabled is False


def test_a_redraw_mid_run_keeps_the_spinners_on_rows_still_in_flight(
        nicegui_client, monkeypatch):
    wizard, _sent = _submit_with(nicegui_client, untick=('KO',))
    with nicegui_client:
        wizard.set_row_outcome(_outcome('AAPL', svc.OUTCOME_SUBMITTED))
        wizard._render_rows()                  # e.g. a Select-all pressed mid-run
    assert wizard._result_cells['AAPL'].text == 'sent'
    assert 'loading' in wizard._result_icons['MSFT'][0]._props
    assert 'loading' not in wizard._result_icons['KO'][0]._props


def test_the_fractional_switch_is_locked_for_the_run_and_back_after(nicegui_client):
    wizard, _sent = _submit_with(nicegui_client)
    switch = wizard._fractional_switch
    assert switch is not None
    # the test base supports fractional shares, so it was enabled before the run
    assert switch.enabled is False
    with nicegui_client:
        wizard.finish_submit('Run 1', run_id=1)
    assert switch.enabled is bool(wizard.base.supports_fractional)


def test_the_worst_outcome_wins_on_the_row_too(nicegui_client):
    wizard, _sent = _submit_with(nicegui_client)
    with nicegui_client:
        wizard.set_row_outcome(_outcome('AAPL', svc.OUTCOME_FAILED, action='close',
                                        message='refused'))
        wizard.set_row_outcome(_outcome('AAPL', svc.OUTCOME_SUBMITTED, action='new'))
    assert 'pf-row-failed' in _classes(wizard._row_elements['AAPL'])
    assert wizard._result_cells['AAPL'].text == 'FAILED'
    assert wizard._outcomes['AAPL'].status == svc.OUTCOME_FAILED
    with nicegui_client:
        wizard.set_row_outcome(_outcome('MSFT', svc.OUTCOME_SUBMITTED))
        wizard.set_row_outcome(_outcome('MSFT', svc.OUTCOME_FAILED, message='late'))
    assert 'pf-row-failed' in _classes(wizard._row_elements['MSFT'])


def test_tapping_icons_reuses_one_details_dialog(nicegui_client, monkeypatch):
    from nicegui import ui
    wizard, _sent = _submit_with(nicegui_client)
    with nicegui_client:
        wizard.set_row_outcome(_outcome('AAPL', svc.OUTCOME_SUBMITTED))
        wizard.set_row_outcome(_outcome('MSFT', svc.OUTCOME_FAILED))
    count = lambda: len([e for e in nicegui_client.layout.descendants()
                         if isinstance(e, ui.dialog)])
    before = count()
    for symbol in ('AAPL', 'MSFT', 'AAPL', 'MSFT'):
        with nicegui_client:
            wizard._open_outcome_details(symbol)
    assert count() == before
    _run_refresh(nicegui_client, wizard, monkeypatch)
    with nicegui_client:
        wizard._open_outcome_details('MSFT')
    assert count() == before
    assert 'MSFT - FAILED' in _dialog_texts(nicegui_client)


def test_the_copy_button_copies_in_the_browser_and_reports_back(nicegui_client,
                                                                monkeypatch):
    toasts = []
    monkeypatch.setattr(wiz.ui, 'notify', lambda m, **k: toasts.append((m, k.get('type'))))
    wizard, _sent = _submit_with(nicegui_client)
    with nicegui_client:
        wizard.set_row_outcome(_outcome('AAPL', svc.OUTCOME_FAILED, message='nope'))
        wizard._open_outcome_details('AAPL')
    copy = next(e for e in nicegui_client.layout.descendants()
                if wiz.MARKER_OUTCOME_COPY in getattr(e, '_markers', []))
    listener = next(l for l in copy._event_listeners.values()
                    if l.type.split('.')[0] == 'click')
    assert 'execCommand("copy")' in listener.js_handler
    assert 'Message: nope' in listener.js_handler          # the text travels in the JS
    from nicegui.events import GenericEventArguments
    listener.handler(GenericEventArguments(sender=copy, client=None, args=[{'ok': True}]))
    listener.handler(GenericEventArguments(sender=copy, client=None, args=[{'ok': False}]))
    assert toasts == [('Details copied', 'positive'),
                      ('Could not copy - select the text', 'warning')]
