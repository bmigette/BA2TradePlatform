"""UI construction tests of the allocator TP/SL control: the pure view helpers, the symbol table and
card (desktop + phone), the banner, and the dialog. No browser, no broker (a fake complex-order API)."""
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core import allocator_protection_service as aps
from ba2_trade_platform.core import portfolio_allocation_service as svc
from ba2_trade_platform.core.allocator_protection_models import AllocatorProtection
from ba2_trade_platform.core.portfolio_allocation import (
    VALUATION_MODE_MARKET, AllocationPlan, AllocationRow,
)
from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.ui.pages import allocator_protection_dialog as dlg
from ba2_trade_platform.ui.pages import portfolio_allocation as page
from ba2_trade_platform.ui.utils import allocator_protection_view as view
from ba2_trade_platform.ui.utils import responsive as rsp
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    ManagedLabel, build_label_views, positions_by_symbol,
)
from tests.allocator_protection_fakes import FakeTastyBroker, make_account, patch_equity

T = ap.TpTarget


# ============================================================================ pure view helpers

def _status(**over):
    kw = dict(enabled=True, pending_replace=False,
              pending_replace_since=None, slice_states=[("LIVE", 10.0)], position_quantity=10.0)
    kw.update(over)
    return ap.protection_status(**kw)


def test_unsupported_broker_draws_nothing():
    f = view.row_fields(False, None)
    assert f["prot_on"] is False and f["prot_tip"] == "" and "prot_label" not in f and "prot_note" not in f


def test_off_symbol_shows_a_grey_shield_with_an_invitation_and_no_text():
    f = view.row_fields(True, _status(enabled=False, slice_states=[]))
    assert f["prot_on"] and "Set TP/SL" in f["prot_tip"] and f["prot_color"] == "grey"
    assert f["prot_code"] == ap.STATUS_OFF and "prot_label" not in f


@pytest.mark.parametrize("status,color", [
    (_status(), "green"),
    (_status(position_quantity=14.0), "amber"),
    (_status(slice_states=[]), "red"),
    (_status(pending_replace=True, slice_states=[]), "amber"),
    (_status(enabled=False, slice_states=[]), "grey"),
    (_status(position_quantity=0.0, slice_states=[]), "grey"),
])
def test_the_shield_colour_per_status(status, color):
    f = view.row_fields(True, status)
    assert f["prot_color"] == color and f["prot_hex"] == view.ICON_COLORS[color]


def test_every_status_code_has_a_shield_colour_and_a_tooltip():
    codes = [ap.STATUS_OFF, ap.STATUS_NO_POSITION, ap.STATUS_PROTECTED, ap.STATUS_PARTIAL,
             ap.STATUS_REPLACING, ap.STATUS_UNPROTECTED, ap.STATUS_CANCEL_UNCONFIRMED]
    for code in codes:
        assert view.protection_icon_color(code) in view.ICON_COLORS
        assert view.protection_icon_color(code, failure_alert=True) == "red"
    assert view.protection_icon_color("SOMETHING_NEW") == "grey"                    # total
    assert set(view._STATUS_ICON) == set(codes)


def test_a_failure_alert_turns_any_shield_red_but_a_warning_does_not():
    ok = _status()
    assert view.row_fields(True, ok, None, "PLACEMENT_REFUSED", "refused")["prot_color"] == "red"
    assert view.row_fields(True, ok, None, ap.CODE_GTC_EXPIRING, "expires soon")["prot_color"] == "green"
    assert view.row_fields(True, ok, None, None, None)["prot_color"] == "green"


def test_the_tooltip_carries_counts_the_fill_note_and_the_alert():
    status = _status(position_quantity=2.0, slice_states=[("LIVE", 1.0)])
    f = view.row_fields(True, status, "TP1 filled 2026-10-03: share 6% -> 3%", "PLACEMENT_REFUSED",
                        "1 share has no stop")
    tip = f["prot_tip"]
    assert "1 of 2 shares protected" in tip and "TP1 filled 2026-10-03: share 6% -> 3%" in tip
    assert "1 share has no stop" in tip and tip.rstrip().endswith("Click to set, change or switch off")


def test_the_fill_note_is_in_the_tooltip_not_beside_the_icon():
    s = _status(last_fill_note="TP1 filled 2026-10-03: share 6% -> 3%")
    f = view.row_fields(True, s, "TP1 filled 2026-10-03: share 6% -> 3%")
    assert "TP1 filled 2026-10-03: share 6% -> 3%" in f["prot_tip"] and "prot_note" not in f


def _p(symbol, **kw):
    return SimpleNamespace(symbol=symbol, alert_message=kw.get("alert_message"))


def test_banner_lists_only_the_alarms_there_is_no_hold_notice():
    unprotected = _status(slice_states=[])
    ok = _status()
    lines = view.banner_lines([(_p("ZZZ"), ok), (_p("BBB", alert_message="the stop expired"), unprotected)])
    assert len(lines) == 1 and lines[0].startswith("BBB: UNPROTECTED") and "the stop expired" in lines[0]


def test_banner_is_empty_when_everything_is_fine():
    assert view.banner_lines([(_p("A"), _status())]) == []


def test_a_size_mismatch_is_in_the_banner():
    mismatch = _status(position_quantity=14.0)
    (line,) = view.banner_lines([(_p("AAA"), mismatch)])
    assert line.startswith("AAA: SIZE MISMATCH")


def test_parse_target_rows_turns_percent_into_fraction():
    targets, errors = view.parse_target_rows([{"price": "60", "pct": "50"}, {"price": 65, "pct": 50.0}])
    assert errors == [] and targets == [T(60.0, 0.5), T(65.0, 0.5)]


def test_parse_target_rows_accepts_a_decimal_comma():
    targets, errors = view.parse_target_rows([{"price": "60,5", "pct": "100"}])
    assert targets[0].price == 60.5 and errors == []


def test_blank_or_garbage_cells_are_errors_never_defaults():
    targets, errors = view.parse_target_rows([{"price": None, "pct": 50}, {"price": "abc", "pct": ""}])
    assert targets == [T(float("nan"), 0)] or targets == []
    assert len(errors) == 4 or len(errors) == 3
    assert any("enter a price" in e for e in errors)


@pytest.mark.parametrize("n", range(1, 9))
def test_even_percentages_sum_to_exactly_100(n):
    pcts = view.even_percentages(n)
    assert len(pcts) == n and round(sum(pcts), 2) == 100.0


def test_even_percentages_of_nothing():
    assert view.even_percentages(0) == []


# ============================================================================ the page

def _views():
    book = [SimpleNamespace(symbol='ABC', qty=10.0, cost_basis=500.0, market_value=500.0, side=None),
            SimpleNamespace(symbol='XYZ', qty=5.0, cost_basis=100.0, market_value=100.0, side=None)]
    return build_label_views(
        [ManagedLabel('ARK26', 100.0)], {'ARK26': ['ABC', 'XYZ']},
        positions_by_symbol(book), {'ABC': 50.0, 'XYZ': 20.0}, {},
        valuation_mode=VALUATION_MODE_MARKET, base_notional=10_000.0,
        symbol_weights={'ARK26': {'ABC': 60.0, 'XYZ': 40.0}})


async def _noop():
    return None


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-pf-protect'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _protection_payload(items=None, quantities=None, supported=True):
    return {'supported': supported, 'items': items or {}, 'quantities': quantities or {'ABC': 10.0, 'XYZ': 5.0}}


def _render(nicegui_client, protection):
    from nicegui import ui
    payload = {'views': _views(), 'symbols_by_label': {}, 'valuation_mode': VALUATION_MODE_MARKET,
               'base_notional': 10_000.0, 'available_buying_power': 1_000.0, 'account_value': None,
               'unallocated_pct': 0.0}
    if protection is not None:
        payload['protection'] = protection
    with nicegui_client:
        page._render_labels(1, payload, _noop)
    return next(el for el in nicegui_client.layout.descendants() if isinstance(el, ui.table))


def _marked(client, marker):
    return [el for el in client.layout.descendants() if marker in getattr(el, '_markers', [])]


def test_the_card_spec_and_the_table_agree_and_have_no_protect_or_exclude_column():
    names = [c['name'] for c in page.symbol_table_columns()]
    assert 'protect' not in names and 'exclude' not in names
    rsp.check_card_columns(page.SYMBOL_CARD, names)
    assert not hasattr(page, 'PROTECT_COLUMN') and not hasattr(page, 'EXCLUDE_COLUMN')


def test_a_supporting_account_gets_the_click_handler_and_the_icon_fields_but_no_cell(nicegui_client):
    table = _render(nicegui_client, _protection_payload())
    assert 'body-cell-protect' not in table.slots and 'body-cell-exclude' not in table.slots
    assert 'protect' not in [c['name'] for c in table.columns]
    assert any(l.type == 'protectClick' for l in table._event_listeners.values())
    row = next(r for r in table.rows if r['symbol'] == 'ABC')
    assert row['prot_on'] is True and 'prot_hex' in row and 'prot_tip' in row and 'prot_label' not in row


def test_a_broker_without_the_feature_gets_no_shield(nicegui_client):
    for payload in (None, _protection_payload(supported=False)):
        table = _render(nicegui_client, payload)
        assert all(r['prot_on'] is False for r in table.rows)
        assert not any(l.type == 'protectClick' for l in table._event_listeners.values())


def test_the_icons_live_in_the_symbol_cell_group_with_the_info_icon():
    chips = page.SYMBOL_CHIPS_TEMPLATE
    assert "$emit('protectClick', props.row.symbol)" in chips and "$emit('excludeToggle', props.row.symbol)" in chips
    assert chips.index('icon="info"') < chips.index('icon="shield"') < chips.index('icon="visibility"')
    assert chips.count('size="sm"') == 3 and 'pf-icons' in chips                       # all three the same size
    template = page.symbol_card_template()
    assert 'props.row.prot_on' in template and template.index('props.row.prot_on') < template.index('class="pf-tiles"')


def test_there_is_no_text_beside_the_icons():
    chips = page.SYMBOL_CHIPS_TEMPLATE
    for field in ('prot_label', 'prot_note', 'excl_badge'):
        assert field not in chips and field not in page.symbol_card_template()


def test_the_icon_colour_is_inline_hex_not_a_quasar_class():
    assert ":style=\"{ color: props.row.prot_hex }\"" in page.SYMBOL_CHIPS_TEMPLATE
    assert ":style=\"{ color: props.row.excl_hex }\"" in page.SYMBOL_CHIPS_TEMPLATE
    assert 'text-negative' not in page.SYMBOL_CHIPS_TEMPLATE


def test_protection_status_reaches_the_rows(nicegui_client):
    p = AllocatorProtection(id=1, account_id=1, symbol='ABC', enabled=True, sl_price=45.0,
                            tp_targets=[T(60, 1.0).to_dict()])
    live_slice = SimpleNamespace(state='LIVE', quantity=10, filled_qty=0.0, closed_at=None)
    table = _render(nicegui_client, _protection_payload({'ABC': (p, [live_slice])}))
    by = {r['symbol']: r for r in table.rows}
    assert by['ABC']['prot_code'] == ap.STATUS_PROTECTED and by['ABC']['prot_color'] == 'green'
    assert by['XYZ']['prot_code'] == ap.STATUS_OFF and by['XYZ']['prot_color'] == 'grey'


def test_an_unprotected_symbol_raises_the_red_banner(nicegui_client):
    p = AllocatorProtection(id=1, account_id=1, symbol='ABC', enabled=True, sl_price=45.0,
                            tp_targets=[T(60, 1.0).to_dict()], alert_code='LOST_EXPIRED',
                            alert_message='slice 1 expired')
    lost = SimpleNamespace(state='LOST_EXPIRED', quantity=10, filled_qty=0.0, closed_at=None)
    _render(nicegui_client, _protection_payload({'ABC': (p, [lost])}))
    assert len(_marked(nicegui_client, page.MARKER_PROTECT_ALERT)) == 1
    texts = [getattr(el, 'text', '') for el in nicegui_client.layout.descendants()]
    assert any('TP/SL ALERT' in t for t in texts) and any('slice 1 expired' in t for t in texts)


def test_a_filled_symbol_is_not_held_and_shows_its_note_on_the_row(nicegui_client):
    from datetime import datetime
    p = AllocatorProtection(id=1, account_id=1, symbol='ABC', enabled=True, sl_price=45.0,
                            tp_targets=[T(60, 1.0).to_dict()], last_fill_at=datetime(2026, 10, 3),
                            last_fill_note='TP1 filled 2026-10-03: share 6% -> 3%')
    live_slice = SimpleNamespace(state='LIVE', quantity=10, filled_qty=0.0, closed_at=None)
    table = _render(nicegui_client, _protection_payload({'ABC': (p, [live_slice])}))
    row = next(r for r in table.rows if r['symbol'] == 'ABC')
    assert 'TP1 filled 2026-10-03: share 6% -> 3%' in row['prot_tip'] and row['prot_code'] == ap.STATUS_PROTECTED
    assert _marked(nicegui_client, page.MARKER_PROTECT_ALERT) == []
    assert not hasattr(page, 'MARKER_PROTECT_HELD')


def test_no_banner_when_all_is_well(nicegui_client):
    _render(nicegui_client, _protection_payload())
    assert _marked(nicegui_client, page.MARKER_PROTECT_ALERT) == []


# ---------------------------------------------------------------- css + payload

def test_the_stylesheet_carries_the_protect_rules_and_the_static_file_is_current():
    css = page.page_phone_css()
    assert '.pf-seg' in css and '.pf-tp-row' in css and '.pf-prot-dialog' in css and '.pf-icons' in css
    assert page.page_css_path().read_text(encoding='utf-8') == css


def test_the_phone_rules_are_inside_a_media_query():
    assert page.PROTECT_PHONE_CSS.strip().startswith('@media')


def test_payload_of_a_mock_account_is_unsupported():
    out = page._load_protection_payload(MagicMock(), 1, {})
    assert out == {'supported': False, 'items': {}, 'quantities': {}}


def test_payload_of_a_supporting_account_is_display_only_and_reads_quantities(monkeypatch):
    calls = []
    monkeypatch.setattr(aps, 'reconcile_account', lambda account: calls.append(account))
    account = SimpleNamespace(id=1, supports_allocator_protection=True)
    positions = {'ABC': SimpleNamespace(quantity=10.0), 'XYZ': SimpleNamespace(quantity=5.0)}
    out = page._load_protection_payload(account, 1, positions)
    assert calls == [] and out['supported'] is True                  # NO reconcile on page load
    assert out['quantities'] == {'ABC': 10.0, 'XYZ': 5.0} and out['items'] == {}


def test_a_failing_reconcile_still_draws_the_stored_state(monkeypatch):
    def boom(account):
        raise RuntimeError('broker down')
    monkeypatch.setattr(aps, 'reconcile_account', boom)
    out = page._load_protection_payload(SimpleNamespace(id=1, supports_allocator_protection=True), 1, {})
    assert out['supported'] is True


# ---------------------------------------------------------------- the dry run

def _plan_with(rows):
    return AllocationPlan(rows=rows, available_buying_power=10_000.0)


def _row(symbol, delta, price=50.0):
    row = AllocationRow(symbol=symbol, price=price, current_quantity=30.0, delta_quantity=delta,
                        side=OrderDirection.SELL if delta < 0 else OrderDirection.BUY,
                        estimated_value=abs(delta) * price, bp_cost=0.0 if delta < 0 else abs(delta) * price,
                        bp_released=abs(delta) * price if delta < 0 else 0.0, target_quantity=30.0 + delta)
    return row


def test_a_tp_fill_does_not_change_the_dry_run_the_symbol_stays_in_the_plan():
    broker = FakeTastyBroker()
    broker.positions['ABC'] = Decimal(30)
    broker.prices['ABC'] = 50.0
    with patch_equity(broker):
        acct = make_account(broker)
        assert aps.save_protection(acct, 'ABC', 45.0, [T(60, 1.0)]).ok
        broker.fill(aps.get_slices(aps.get_protection(1, 'ABC').id)[0].complex_order_id, 'TP', qty=3)
        aps.reconcile_account(acct)
        plan = _plan_with([_row('ABC', -10.0), _row('XYZ', 5.0, 20.0)])
        out = svc.mark_excluded_rows_skipped(acct, plan)
    assert out is plan                                      # nothing excluded: the plan is untouched
    assert not out.rows[0].skipped and out.rows[0].delta_quantity == -10.0


# ============================================================================ the dialog

def _data(*, quantity=10.4, protection=None, price=50.0, slices=None, is_long=True):
    status = aps.status_for(protection, quantity, slices or [])
    return {'account': SimpleNamespace(id=1), 'symbol': 'ABC', 'quantity': quantity, 'is_long': is_long,
            'price': price, 'ticks': None, 'protection': protection, 'slices': slices or [], 'status': status}


def _labels(client):
    return [getattr(el, 'text', None) or getattr(el, '_props', {}).get('label') or ''
            for el in client.layout.descendants()]


def _find(client, marker):
    found = _marked(client, marker)
    assert found, marker
    return found[0]


def test_the_dialog_opens_on_a_blank_form_with_save_disabled(nicegui_client):
    with nicegui_client:
        dlg._build_dialog(1, _data(), _noop)
    labels = _labels(nicegui_client)
    assert 'TP/SL protection: ABC' in labels
    assert any('Stop-loss price' in l for l in labels) and any('TP 1 price' in l for l in labels)
    assert any('fractional' in l for l in labels)                       # 10.4 shares: 0.4 unprotected
    assert not _find(nicegui_client, dlg.MARKER_SAVE).enabled
    assert not hasattr(dlg, 'MARKER_REENABLE')
    assert _marked(nicegui_client, dlg.MARKER_SWITCH_OFF) == []         # nothing to switch off yet


def test_a_valid_form_enables_save_and_previews_the_orders(nicegui_client):
    from nicegui import ui
    with nicegui_client:
        dlg._build_dialog(1, _data(quantity=10.0), _noop)
    numbers = [el for el in nicegui_client.layout.descendants() if isinstance(el, ui.number)]
    sl = next(n for n in numbers if n._props.get('label') == 'Stop-loss price')
    tp = next(n for n in numbers if n._props.get('label') == 'TP 1 price')
    sl.set_value(45.0)
    tp.set_value(60.0)
    assert _find(nicegui_client, dlg.MARKER_SAVE).enabled
    labels = _labels(nicegui_client)
    assert any(l.startswith('OCO 1: sell 10 sh') and 'limit 60' in l and 'stop 45' in l for l in labels)


def test_an_invalid_form_names_the_problem_and_keeps_save_disabled(nicegui_client):
    from nicegui import ui
    with nicegui_client:
        dlg._build_dialog(1, _data(quantity=10.0), _noop)
    numbers = [el for el in nicegui_client.layout.descendants() if isinstance(el, ui.number)]
    next(n for n in numbers if n._props.get('label') == 'Stop-loss price').set_value(55.0)   # above 50
    next(n for n in numbers if n._props.get('label') == 'TP 1 price').set_value(60.0)
    assert not _find(nicegui_client, dlg.MARKER_SAVE).enabled
    assert any('BELOW the current price' in l for l in _labels(nicegui_client))


def test_adding_a_target_splits_the_percentages_evenly(nicegui_client):
    from nicegui import ui
    with nicegui_client:
        dlg._build_dialog(1, _data(quantity=10.0), _noop)
    _find(nicegui_client, dlg.MARKER_ADD_TARGET).run_method  # the button exists
    add = _find(nicegui_client, dlg.MARKER_ADD_TARGET)
    add._handle_click = None
    for handler in add._event_listeners.values():
        if handler.type == 'click':
            handler.handler(SimpleNamespace(args=None, sender=add, client=nicegui_client))
    pcts = [n.value for n in nicegui_client.layout.descendants()
            if isinstance(n, ui.number) and n._props.get('label') == '% of position']
    assert pcts == [50.0, 50.0]


def test_the_dialog_says_a_rebalance_replaces_the_orders_and_excluding_does_not_cancel_them(nicegui_client):
    with nicegui_client:
        dlg._build_dialog(1, _data(), _noop)
    labels = _labels(nicegui_client)
    assert any('cancels these orders' in l and 're-places them at the new quantity' in l
               and 'Excluding the symbol from allocation does not cancel or change them' in l for l in labels)


def test_the_last_fill_note_is_shown_in_the_dialog(nicegui_client):
    p = AllocatorProtection(id=1, account_id=1, symbol='ABC', enabled=True, sl_price=45.0,
                            tp_targets=[T(60, 1.0).to_dict()],
                            last_fill_note='SL hit 2026-10-03: share 5% -> 0%')
    with nicegui_client:
        dlg._build_dialog(1, _data(protection=p, quantity=10.0), _noop)
    assert any('Last fill: SL hit 2026-10-03: share 5% -> 0%' in l for l in _labels(nicegui_client))


def test_an_enabled_protection_offers_replace_and_switch_off(nicegui_client):
    p = AllocatorProtection(id=1, account_id=1, symbol='ABC', enabled=True, sl_price=45.0,
                            tp_targets=[T(60, 0.5).to_dict(), T(65, 0.5).to_dict()])
    s = SimpleNamespace(slice_index=0, quantity=10, tp_price=60.0, sl_price=45.0, state='LIVE',
                        complex_order_id=9000, sl_order_id=None, external_tag='ba2prot:1:0', gtc_date=None, gtc_date_assumed=False, closed_at=None,
                        filled_qty=0.0)
    with nicegui_client:
        dlg._build_dialog(1, _data(protection=p, quantity=10.0, slices=[s]), _noop)
    assert _marked(nicegui_client, dlg.MARKER_REPLACE) and _marked(nicegui_client, dlg.MARKER_SWITCH_OFF)
    assert any(getattr(el, '_props', {}).get('label') == 'Resize protection'
               for el in nicegui_client.layout.descendants())
    # the stored targets open as rows, percentages restored
    from nicegui import ui
    pcts = [n.value for n in nicegui_client.layout.descendants()
            if isinstance(n, ui.number) and n._props.get('label') == '% of position']
    assert pcts == [50.0, 50.0]


def test_an_alert_on_the_protection_is_shown_in_the_dialog(nicegui_client):
    p = AllocatorProtection(id=1, account_id=1, symbol='ABC', enabled=True, sl_price=45.0,
                            tp_targets=[T(60, 1.0).to_dict()], alert_code='PLACEMENT_REFUSED',
                            alert_message='the broker said no')
    with nicegui_client:
        dlg._build_dialog(1, _data(protection=p, quantity=10.0), _noop)
    assert any('ALERT PLACEMENT_REFUSED: the broker said no' in l for l in _labels(nicegui_client))


def test_a_short_position_is_flagged_and_cannot_be_saved(nicegui_client):
    with nicegui_client:
        dlg._build_dialog(1, _data(quantity=10.0, is_long=False), _noop)
    assert any('SHORT' in l for l in _labels(nicegui_client))
    assert not _find(nicegui_client, dlg.MARKER_SAVE).enabled


def test_initial_rows_default_to_one_blank_target_at_100_percent():
    assert dlg.initial_rows(None) == [{'price': None, 'pct': 100.0}]


def test_slice_rows_hide_cancelled_history():
    from datetime import datetime
    gone = SimpleNamespace(slice_index=0, quantity=3, tp_price=60, sl_price=45, state='CANCELLED_BY_US',
                           complex_order_id=1, sl_order_id=None, external_tag='t', gtc_date=None, gtc_date_assumed=False,
                           closed_at=datetime(2026, 10, 4))
    live = SimpleNamespace(slice_index=1, quantity=7, tp_price=61, sl_price=45, state='LIVE',
                           complex_order_id=2, sl_order_id=None, external_tag='t2', gtc_date=datetime(2027, 1, 3), gtc_date_assumed=True,
                           closed_at=None)
    rows = dlg.slice_rows([gone, live])
    assert [r['n'] for r in rows] == [2] and rows[0]['gtc'] == '2027-01-03'


def test_load_dialog_data_reads_the_broker_and_the_store(monkeypatch):
    broker = FakeTastyBroker()
    broker.positions['ABC'] = Decimal('10.4')
    broker.prices['ABC'] = 50.0
    with patch_equity(broker):
        acct = make_account(broker)
        monkeypatch.setattr('ba2_trade_platform.core.utils.get_account_instance_from_id', lambda i: acct)
        data = dlg.load_dialog_data(1, 'abc')
    assert data['symbol'] == 'ABC' and data['quantity'] == pytest.approx(10.4) and data['price'] == 50.0
    assert data['is_long'] and data['protection'] is None and data['status'].code == ap.STATUS_OFF


def test_load_dialog_data_refuses_an_account_without_the_capability(monkeypatch):
    monkeypatch.setattr('ba2_trade_platform.core.utils.get_account_instance_from_id',
                        lambda i: SimpleNamespace(id=1))
    with pytest.raises(RuntimeError, match='does not support'):
        dlg.load_dialog_data(1, 'ABC')


def test_load_dialog_data_does_not_open_on_an_unreadable_price(monkeypatch):
    broker = FakeTastyBroker()
    broker.positions['ABC'] = Decimal(10)                                  # no price at all
    with patch_equity(broker):
        acct = make_account(broker)
        monkeypatch.setattr('ba2_trade_platform.core.utils.get_account_instance_from_id', lambda i: acct)
        with pytest.raises(aps.BrokerReadError):
            dlg.load_dialog_data(1, 'ABC')


def test_the_dialog_module_installs_no_css_of_its_own():
    source = Path(dlg.__file__).read_text(encoding='utf8')
    assert 'add_css' not in source and 'add_head_html' not in source


# ============================================================================ presets in the dialog

def _numbers(client, label):
    from nicegui import ui
    return [n for n in client.layout.descendants() if isinstance(n, ui.number) and n._props.get('label') == label]


def _click(client, marker):
    button = _find(client, marker)
    for handler in button._event_listeners.values():
        if handler.type == 'click':
            handler.handler(SimpleNamespace(args=None, sender=button, client=client))
            return
    raise AssertionError(marker)


def _preset_dialog(client, *, average_cost=100.0, quantity=30.0, price=100.0):
    data = _data(quantity=quantity, price=price)
    data['average_cost'] = average_cost
    with client:
        dlg._build_dialog(1, data, _noop)


def test_there_is_one_button_per_requested_preset(nicegui_client):
    _preset_dialog(nicegui_client)
    for spec in ap.PRESETS:
        assert _marked(nicegui_client, dlg.MARKER_PRESET_PREFIX + spec.key), spec.key
    labels = [getattr(el, '_props', {}).get('label') for el in nicegui_client.layout.descendants()]
    for text in ("Double-up: take half at 2x", "Ladder +25/+50/+100%", "2R scale-out", "Income: stop only"):
        assert text in labels


def test_double_up_fills_the_form_and_previews_a_runner_stop(nicegui_client):
    _preset_dialog(nicegui_client)
    _click(nicegui_client, dlg.MARKER_PRESET_PREFIX + 'double_up')
    sl = _numbers(nicegui_client, 'Stop-loss price')[0]
    assert sl.value == 75.0
    assert [n.value for n in _numbers(nicegui_client, 'TP 1 price')] == [200.0]
    assert [n.value for n in _numbers(nicegui_client, '% of position')] == [50.0]
    texts = _labels(nicegui_client)
    assert any(t.startswith('2 orders: OCO 15 sh TP 200 / SL 75, STOP 15 sh @ 75') for t in texts)
    assert _find(nicegui_client, dlg.MARKER_SAVE).enabled


def test_ladder_fills_three_targets_summing_to_exactly_100(nicegui_client):
    _preset_dialog(nicegui_client)
    _click(nicegui_client, dlg.MARKER_PRESET_PREFIX + 'ladder')
    assert _numbers(nicegui_client, 'Stop-loss price')[0].value == 85.0
    prices = [n.value for n in _numbers(nicegui_client, 'TP 1 price')] + \
             [n.value for n in _numbers(nicegui_client, 'TP 2 price')] + \
             [n.value for n in _numbers(nicegui_client, 'TP 3 price')]
    assert prices == [125.0, 150.0, 200.0]
    assert [n.value for n in _numbers(nicegui_client, '% of position')] == [33.33, 33.33, 33.34]
    assert any(t.startswith('3 orders: OCO 10 sh TP 125 / SL 85, OCO 10 sh TP 150') for t in _labels(nicegui_client))


def test_two_r_stops_ten_percent_and_takes_half_at_plus_twenty(nicegui_client):
    _preset_dialog(nicegui_client)
    _click(nicegui_client, dlg.MARKER_PRESET_PREFIX + 'two_r')
    assert _numbers(nicegui_client, 'Stop-loss price')[0].value == 90.0
    assert [n.value for n in _numbers(nicegui_client, 'TP 1 price')] == [120.0]
    assert [n.value for n in _numbers(nicegui_client, '% of position')] == [50.0]


def test_income_stop_only_removes_every_take_profit_row_and_previews_one_stop(nicegui_client):
    _preset_dialog(nicegui_client)
    _click(nicegui_client, dlg.MARKER_PRESET_PREFIX + 'income_stop')
    assert _numbers(nicegui_client, 'Stop-loss price')[0].value == 80.0
    assert _numbers(nicegui_client, '% of position') == []
    texts = _labels(nicegui_client)
    assert 'No take-profit: the whole position gets a stop only.' in texts
    assert any(t == '1 order: STOP 30 sh @ 80' for t in texts)
    assert _find(nicegui_client, dlg.MARKER_SAVE).enabled


def test_presets_use_the_average_cost_not_the_price(nicegui_client):
    _preset_dialog(nicegui_client, average_cost=40.0, price=100.0)
    _click(nicegui_client, dlg.MARKER_PRESET_PREFIX + 'double_up')
    assert _numbers(nicegui_client, 'Stop-loss price')[0].value == 30.0         # 40 x 0.75
    assert [n.value for n in _numbers(nicegui_client, 'TP 1 price')] == [80.0]   # 40 x 2.0


def test_a_preset_that_does_not_fit_the_price_shows_the_usual_validation_and_stays_editable(nicegui_client):
    _preset_dialog(nicegui_client, average_cost=100.0, price=70.0)        # deep under water
    _click(nicegui_client, dlg.MARKER_PRESET_PREFIX + 'double_up')         # stop 75 > price 70
    assert not _find(nicegui_client, dlg.MARKER_SAVE).enabled
    assert any('BELOW the current price' in t for t in _labels(nicegui_client))
    _numbers(nicegui_client, 'Stop-loss price')[0].set_value(60.0)         # the operator edits it
    assert _find(nicegui_client, dlg.MARKER_SAVE).enabled


def test_presets_are_disabled_when_the_average_cost_is_unknown(nicegui_client):
    _preset_dialog(nicegui_client, average_cost=None)
    for spec in ap.PRESETS:
        assert not _find(nicegui_client, dlg.MARKER_PRESET_PREFIX + spec.key).enabled
    assert any('presets unavailable' in t for t in _labels(nicegui_client))


def test_a_stored_stop_only_protection_opens_with_no_take_profit_rows(nicegui_client):
    p = AllocatorProtection(id=1, account_id=1, symbol='ABC', enabled=True, sl_price=80.0, tp_targets=[])
    assert dlg.initial_rows(p) == []
    with nicegui_client:
        dlg._build_dialog(1, _data(protection=p, quantity=10.0), _noop)
    assert _numbers(nicegui_client, '% of position') == []


def test_adding_a_target_after_a_runner_preset_gives_it_the_remaining_share(nicegui_client):
    _preset_dialog(nicegui_client)
    _click(nicegui_client, dlg.MARKER_PRESET_PREFIX + 'double_up')         # one TP at 50%, runner 50%
    _click(nicegui_client, dlg.MARKER_ADD_TARGET)
    assert [n.value for n in _numbers(nicegui_client, '% of position')] == [50.0, 50.0]


def test_the_stored_targets_of_a_runner_protection_keep_their_percentages(nicegui_client):
    p = AllocatorProtection(id=1, account_id=1, symbol='ABC', enabled=True, sl_price=75.0,
                            tp_targets=[T(200, 0.5).to_dict()])
    assert dlg.initial_rows(p) == [{'price': 200, 'pct': 50.0}]


def test_fractions_to_percentages_never_leave_a_hidden_runner():
    assert view.fractions_to_percentages([1 / 3, 1 / 3, 1 / 3]) == [33.33, 33.33, 33.34]
    assert view.fractions_to_percentages([0.5]) == [50.0]
    assert view.fractions_to_percentages([]) == []
