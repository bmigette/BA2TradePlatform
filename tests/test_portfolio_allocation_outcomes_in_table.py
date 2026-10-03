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
    assert set(ALL_STATUSES) | {ov.STATUS_PENDING} == set(ov.KNOWN_STATUSES)
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
            ov.STATUS_PENDING, svc.OUTCOME_SKIPPED)


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


def test_copy_text_is_one_label_value_per_line_and_the_js_has_an_http_fallback():
    text = ov.outcome_details_text([('Symbol', 'AAPL'), ('Filled qty', '-')])
    assert text == 'Symbol: AAPL\nFilled qty: -'
    js = ov.copy_to_clipboard_js('a "quoted"\nline')
    # the app is served over plain http on the LAN, where navigator.clipboard is absent
    assert 'execCommand("copy")' in js and 'isSecureContext' in js
    assert '"a \\"quoted\\"\\nline"' in js


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
