"""Phone layout helpers of the Settings page (expert list cards, dialog sections, tabs)."""
import inspect
import re

import pytest

from ba2_trade_platform.ui.utils import phone_sections as ps
from ba2_trade_platform.ui.utils import responsive as rsp

TABLE_COLUMNS = ['id', 'expert', 'alias', 'enabled', 'priority', 'virtual_equity_pct',
                 'account_id', 'enter_market_ruleset_name', 'open_positions_ruleset_name',
                 'actions']
PHONE_MARK = f'@media (max-width: {rsp.PHONE_MAX_WIDTH_PX}px)'


def _phone_css():
    return ps.phone_css().split(PHONE_MARK, 1)[1]


def test_the_card_accounts_for_every_table_column():
    ps.check_card_covers_table(TABLE_COLUMNS)


def test_a_table_column_without_a_phone_decision_is_refused():
    with pytest.raises(ValueError, match='new_col'):
        ps.check_card_covers_table(TABLE_COLUMNS + ['new_col'])
    with pytest.raises(ValueError, match='priority'):
        ps.check_card_covers_table([c for c in TABLE_COLUMNS if c != 'priority'])


def test_the_card_template_shares_the_desktop_events_and_selection():
    t = ps.expert_card_template()
    assert 'v-model="props.selected"' in t
    for event in ("'edit'", "'duplicate'", "'del'"):
        assert f"$parent.$emit({event}, props)" in t
    for key, _ in ps.CARD_TILES:
        assert f'props.row.{key}' in t
    for field in ('alias', 'expert', 'enabled', 'id'):
        assert f'props.row.{field}' in t
    assert t.count('<div') == t.count('</div>')


def test_the_forced_open_body_is_desktop_only():
    css = ps.phone_css()
    assert f'@media (min-width: {rsp.PHONE_MAX_WIDTH_PX + 1}px)' in css
    desk = css.split(PHONE_MARK)[0]
    assert 'display: block !important' in desk
    # on a phone it would beat Quasar's inline display:none and the sections could not fold
    section_lines = [l for l in _phone_css().splitlines() if '.bm-sec' in l or 'expansion-item__content' in l]
    assert section_lines and not any('display: block' in l or 'display: revert' in l for l in section_lines)
    assert css.count('{') == css.count('}')


def test_a_phone_section_shows_its_header_and_hides_the_in_body_title():
    phone = _phone_css()
    assert '.bm-sec > .q-expansion-item__container > .q-item' in phone
    assert re.search(r'\.bm-sec-title, \.bm-sec-intro, \.bm-sec-sep \{ display: none', phone)


def test_the_dialog_is_full_screen_on_a_phone():
    phone = _phone_css()
    assert 'width: 100vw !important' in phone and 'height: 100dvh !important' in phone
    assert 'border-radius: 0' in phone


def test_every_dialog_tab_has_a_short_phone_label():
    names = ['General Settings', 'Instruments', 'Expert Settings', 'Import/Export', 'Actions']
    labels = dict(ps.tab_short_labels(names))
    assert all(len(v) <= 11 for v in labels.values())
    with pytest.raises(KeyError):
        ps.tab_short_labels(['Brand New Tab'])


def test_rows_that_truncate_side_by_side_stack_one_field_per_row():
    assert re.search(r'\.bm-stack\.bm-stack \{ flex-direction: column', _phone_css())


def test_the_settings_page_uses_the_helpers():
    from ba2_trade_platform.ui.pages import settings as page
    src = inspect.getsource(page.ExpertSettingsTab)
    assert 'check_card_covers_table(' in src
    assert "add_slot('item', expert_card_template())" in src
    assert 'phone_grid_prop()' in src and 'with phone_section(' in src
    assert src.count("dialog_tab('") == 5
    assert 'bm-stack' in src


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-settings-phone'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def test_the_expert_list_is_a_card_mode_table_registered_for_breakpoint_changes(nicegui_client):
    from nicegui import ui
    from ba2_trade_platform.ui.pages import settings as page
    registry = rsp.PhoneTableRegistry()
    with nicegui_client:
        tab = page.ExpertSettingsTab(registry)
    table = tab.experts_table
    assert isinstance(table, ui.table)
    assert table._props[':grid'] == rsp.phone_grid_expression()
    for slot in ('item', 'body-cell-enabled', 'body-cell-actions', 'top-left'):
        assert slot in table.slots
    assert len(registry) == 1
    registry.on_width(844)
    assert table._props[':grid'] == 'false'
    registry.on_width(390)
    assert table._props[':grid'] == 'true'
