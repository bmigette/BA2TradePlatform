"""Collapsible sections that exist ONLY on a phone, and the phone CSS of the Settings page.

WHY. The Edit Expert dialog's General tab is one ~5400px column of "Section: / Configure ..."
header pairs. On a phone the operator wants a heading per section that folds. On a desktop
the same page must look exactly as it did, so a section is built ONCE, as a Quasar
expansion, and the CSS decides what it is:

* above the breakpoint the expansion's header is hidden and its body is forced open, so the
  original in-body title and intro labels (still in the DOM) read exactly as before;
* at or below it the header (the section's title) is shown, the body folds, and the
  now-redundant in-body title / intro labels are hidden.

Every input inside a section is built by the same code as before, in the same order, bound
to the same attributes: a folded body is ``display:none``, not removed, so values, bindings
and handlers are untouched. This module draws nothing and decides nothing about settings;
it is layout only.

NiceGUI drops ``ui.add_css`` / ``ui.add_head_html`` made after a page's first ``await``;
``settings.content()`` is synchronous and installs ``SETTINGS_PHONE_CSS`` before building
anything (pinned by ``tests/test_settings_phone_css_before_await.py``).
"""
from contextlib import contextmanager
from typing import List, Sequence, Tuple

from .responsive import PHONE_MAX_WIDTH_PX, phone_media

#: Classes (the CSS addresses these, never a position).
SECTION_CLASS = 'bm-sec'
SECTION_TITLE_CLASS = 'bm-sec-title'     # in-body title label: hidden on a phone
SECTION_INTRO_CLASS = 'bm-sec-intro'     # in-body "Configure ..." label: hidden on a phone
SECTION_SEP_CLASS = 'bm-sec-sep'         # separator between sections: hidden on a phone
STACK_ROW_CLASS = 'bm-stack'             # a row whose children go one per line on a phone
FLAT_CLASS = 'bm-flat'                   # a card nested in a section: no frame on a phone
DIALOG_CLASS = 'bm-xdlg'                 # the Edit Expert dialog's card
TABS_CLASS = 'bm-xtabs'
FOOT_CLASS = 'bm-xfoot'
TAB_FULL_CLASS = 'bm-t-full'
TAB_SHORT_CLASS = 'bm-t-short'
LIST_CLASS = 'bm-xlist'                  # the expert list's q-table
LIST_TOP_CLASS = 'bm-xtop'               # its toolbar row

#: The expert dialog's tabs: (tab name = panel value, short phone label). The tab NAME is
#: the panel's value and must not change; only the phone's visible label is shortened.
DIALOG_TAB_SHORT_LABELS = {
    'General Settings': 'General',
    'Instruments': 'Instruments',
    'Expert Settings': 'Expert',
    'Import/Export': 'Import',
    'Actions': 'Actions',
}


def _ui():
    """The NiceGUI ``ui`` module (a function so a test can hand the helpers a fake)."""
    from nicegui import ui
    return ui


@contextmanager
def phone_section(title: str, *, open_: bool = False, icon: str = None, ui=None):
    """A section that folds on a phone and is a plain block on a desktop. Use as
    ``with phone_section('Position Sizing'):``; whatever is built inside is the body."""
    ui = ui or _ui()
    expansion = ui.expansion(title, value=open_, icon=icon).classes(f'w-full {SECTION_CLASS}')
    expansion.props('header-class=bm-sec-head dense')
    with expansion:
        yield expansion


def dialog_tab(name: str, icon: str, ui=None):
    """``ui.tab`` whose DOM is the original (full label from the prop) plus a hidden-on-
    desktop short label the phone CSS shows instead."""
    ui = ui or _ui()
    tab = ui.tab(name, icon=icon)
    with tab:
        ui.element('span').classes(f'q-tab__label {TAB_SHORT_CLASS}').props(
            f'data-short="{DIALOG_TAB_SHORT_LABELS[name]}"')
    return tab


def tab_short_labels(tab_names: Sequence[str]) -> List[Tuple[str, str]]:
    """``[(name, short label)]`` for the dialog's tabs; raises KeyError for a tab with no
    phone label (a new tab must make a decision). Pure."""
    return [(n, DIALOG_TAB_SHORT_LABELS[n]) for n in tab_names]


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

#: Desktop half: neutralise the expansion so a section reads as the plain block it was.
#: ``!important`` because styles.css paints every .q-expansion-item container/content with
#: its own translucent panel, also with !important.
_DESKTOP_RULES = f'''
    .{SECTION_CLASS}, .{SECTION_CLASS} .q-expansion-item__container,
    .{SECTION_CLASS} > .q-expansion-item__container > .q-expansion-item__content,
    .{SECTION_CLASS} > .q-expansion-item__container > div > .q-expansion-item__content {{
        background: transparent !important; border-radius: 0 !important; }}
    .{SECTION_CLASS} > .q-expansion-item__container > .q-item {{ display: none !important; }}
    .{SECTION_CLASS} > .q-expansion-item__container > .q-expansion-item__content,
    .{SECTION_CLASS} > .q-expansion-item__container > div {{ display: block !important;
        height: auto !important; overflow: visible !important; }}
    .{SECTION_CLASS} > .q-expansion-item__container > .q-expansion-item__content,
    .{SECTION_CLASS} > .q-expansion-item__container > div > .q-expansion-item__content {{
        padding: 0 !important; }}
    .{SECTION_CLASS} > .q-expansion-item__container > .q-expansion-item__content
        > .nicegui-expansion-content {{ padding: 0 !important; }}
'''

#: Above the breakpoint ONLY: forcing the body open with ``display:block !important`` would also
#: beat Quasar's inline ``display:none`` on a phone and stop the sections folding.
DESKTOP_CSS = (f'@media (min-width: {PHONE_MAX_WIDTH_PX + 1}px) {{' + _DESKTOP_RULES + '}'
               + ' .' + TAB_SHORT_CLASS + ' { display: none; }')



def _phone_dialog_css() -> str:
    return f'''
    /* THE EDIT EXPERT DIALOG fills the screen: the 90vw card wasted ~10% each side. The
       card is a flex column (title, tabs, scrolling panels, footer), so header and footer
       stay put while the panels scroll. */
    .q-dialog__inner:has(.{DIALOG_CLASS}) {{ padding: 0 !important; }}
    .q-dialog__inner > .{DIALOG_CLASS}.{DIALOG_CLASS} {{
        width: 100vw !important; max-width: 100vw !important; min-width: 0 !important;
        height: 100vh !important; height: 100dvh !important;
        max-height: 100vh !important; max-height: 100dvh !important;
        margin: 0 !important; border-radius: 0 !important; padding: 8px 10px !important; }}
    .{DIALOG_CLASS} > .text-h6 {{ font-size: 1.1rem; line-height: 1.4; padding: 2px 4px; }}
    /* TABS: icon over a SHORT label, equal share of the width, so all of them are
       reachable and the active one carries the indicator and the accent colour. */
    .{TABS_CLASS}.q-tabs {{ width: 100%; min-height: 48px; }}
    .{TABS_CLASS} .q-tabs__content {{ flex: 1 1 auto; width: 100%; min-width: 0; }}
    .{TABS_CLASS} .q-tab {{ min-height: 48px; flex: 1 1 0; min-width: 0 !important; padding: 0 2px !important; }}
    .{TABS_CLASS} .q-tab__content {{ min-width: 0; padding: 0 !important; }}
    .{TABS_CLASS} .q-tab > .q-tab__content > .q-tab__label:not(.{TAB_SHORT_CLASS}) {{
        display: none !important; }}
    .{TABS_CLASS} .q-tab .{TAB_SHORT_CLASS} {{ display: block !important; font-size: 11px;
        line-height: 1.2; letter-spacing: 0; white-space: nowrap; }}
    .{TABS_CLASS} .q-tab .{TAB_SHORT_CLASS}::after {{ content: attr(data-short); }}
    .{TABS_CLASS} .q-tab__icon {{ font-size: 22px !important; }}
    /* ONE FIELD PER ROW (Expert Type / Alias truncated side by side). */
    .{STACK_ROW_CLASS}.{STACK_ROW_CLASS} {{ flex-direction: column !important;
        flex-wrap: nowrap !important; align-items: stretch !important; gap: 8px !important; }}
    .{STACK_ROW_CLASS}.{STACK_ROW_CLASS} > * {{ width: 100% !important; max-width: 100% !important;
        flex: 0 0 auto !important; }}
    /* SECTIONS FOLD. The header (the section's title) replaces the in-body title and the
       "Configure ..." intro, which are hidden. */
    .{SECTION_CLASS}.{SECTION_CLASS} {{ margin: 0 0 8px 0 !important;
        border: 1px solid rgba(255,255,255,0.16); border-radius: 10px;
        background: rgba(255,255,255,0.04) !important; }}
    .{SECTION_CLASS}.{SECTION_CLASS} > .q-expansion-item__container {{
        background: transparent !important; border-radius: 10px !important; box-shadow: none !important; }}
    .{SECTION_CLASS}.{SECTION_CLASS} > .q-expansion-item__container > .q-item {{
        display: flex !important; min-height: 48px; padding: 0 12px !important;
        background: transparent !important; box-shadow: none !important; }}
    .{DIALOG_CLASS} .q-expansion-item.mb-4 {{ margin-bottom: 8px !important; }}
    .{DIALOG_CLASS} .{SECTION_CLASS} .nicegui-expansion-content {{ gap: 8px; }}
    .{SECTION_CLASS} .bm-sec-head .q-item__label {{ font-weight: 600; font-size: 1rem; }}
    .{SECTION_CLASS} > .q-expansion-item__container > .q-expansion-item__content,
    .{SECTION_CLASS} > .q-expansion-item__container > div > .q-expansion-item__content {{
        padding: 4px 12px 12px !important; }}
    .{SECTION_TITLE_CLASS}, .{SECTION_INTRO_CLASS}, .{SECTION_SEP_CLASS} {{ display: none !important; }}
    /* HELP TEXT: smaller, muted, flush left (the 1.5rem indent was for desktop checkboxes). */
    .{DIALOG_CLASS} .text-grey-7, .{DIALOG_CLASS} .text-grey-6 {{ font-size: 12px !important;
        line-height: 1.35 !important; margin-left: 0 !important; color: #94a3b8 !important; }}
    /* NESTED CARDS add padding and a frame the section already provides. */
    .{DIALOG_CLASS} .{FLAT_CLASS}.{FLAT_CLASS} {{ padding: 0 !important; box-shadow: none !important;
        background: transparent !important; border: 0 !important; }}
    .{DIALOG_CLASS} .nicegui-expansion-content {{ padding-left: 0 !important; }}
    .{DIALOG_CLASS} .q-expansion-item .q-expansion-item .q-expansion-item__content {{
        padding: 0 !important; }}
    /* TEXTAREAS: grow with their text (no inner scrollbar fighting the panel's), no resize
       grip; a very long text scrolls inside a 9rem cap. */
    .{DIALOG_CLASS} textarea {{ resize: none !important; field-sizing: content;
        max-height: 9rem; overscroll-behavior: contain; }}
    .{DIALOG_CLASS} .q-field.w-28, .{DIALOG_CLASS} .q-field.w-32 {{ width: calc(50% - 6px) !important;
        display: inline-flex; }}
    /* THE EXPERT-SPECIFIC SETTINGS GRID (55% label / 38% input / 7% reset) overflowed the
       dialog: label on its own line, input + reset button under it. */
    .bm-setgrid.bm-setgrid {{ grid-template-columns: minmax(0, 1fr) auto !important;
        row-gap: 4px !important; }}
    .bm-setgrid.bm-setgrid > :first-child {{ grid-column: 1 / -1; }}
    .bm-setgrid .q-field, .bm-setgrid .nicegui-select {{ min-width: 0; max-width: 100%; }}
    /* FOOTER: Cancel / Save are always reachable, big targets, clear of the iOS home bar. */
    .{FOOT_CLASS}.{FOOT_CLASS} {{ flex-wrap: nowrap !important; gap: 8px !important;
        margin-top: 6px !important; padding-bottom: env(safe-area-inset-bottom, 0); }}
    .{FOOT_CLASS} .q-btn {{ flex: 1 1 0; min-height: 44px !important; }}
    '''


def _phone_list_css() -> str:
    return f'''
    /* THE EXPERT LIST is one card per expert (q-table grid mode). */
    .{LIST_CLASS} .q-table__grid-content {{ padding: 0 !important; }}
    .{LIST_CLASS} .q-table__top {{ padding: 4px 0 !important; }}
    .{LIST_TOP_CLASS}.{LIST_TOP_CLASS} {{ width: 100%; gap: 8px !important; flex-wrap: wrap !important; }}
    .{LIST_TOP_CLASS} .q-btn {{ min-height: 40px !important; }}
    .bm-xcard {{ border: 1px solid rgba(255,255,255,0.16); border-radius: 12px;
        padding: 8px 10px 10px; margin: 0 0 12px; width: 100%; box-sizing: border-box;
        background: #232a3d; box-shadow: 0 1px 3px rgba(0,0,0,0.35); }}
    .bm-xcard--sel {{ border-color: #00d4aa; }}
    .bm-xcard-head {{ display: flex; align-items: center; gap: 6px; padding-bottom: 6px;
        margin-bottom: 8px; border-bottom: 1px solid rgba(255,255,255,0.10); }}
    .bm-xcard-id {{ color: #94a3b8; font-variant-numeric: tabular-nums; }}
    .bm-xcard-type {{ font-size: 1.1rem; font-weight: 700; line-height: 1.2; min-width: 0;
        overflow-wrap: anywhere; }}
    .bm-xcard-alias {{ font-size: 0.95rem; overflow-wrap: anywhere; margin-bottom: 8px; }}
    .bm-xcard-tiles {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
    .bm-xcard-tile {{ min-width: 0; padding: 6px 10px; border-radius: 8px;
        background: rgba(255,255,255,0.04); }}
    .bm-xcard-tile--wide {{ grid-column: 1 / -1; }}
    .bm-xcard-k {{ font-size: 11px; line-height: 1.2; letter-spacing: 0.05em;
        text-transform: uppercase; color: #94a3b8; }}
    .bm-xcard-v {{ font-size: 0.95rem; overflow-wrap: anywhere; }}
    .bm-xcard-actions {{ display: flex; justify-content: flex-end; gap: 4px; margin-top: 8px; }}
    .bm-xcard-actions .q-btn {{ min-width: 44px; min-height: 40px; }}
    '''


def phone_css() -> str:
    """Everything the Settings page's expert list and dialog need, in one string. Pure."""
    return '\n'.join((DESKTOP_CSS, phone_media(_phone_dialog_css()), phone_media(_phone_list_css())))


# ---------------------------------------------------------------------------
# The expert list card (q-table ``item`` slot)
# ---------------------------------------------------------------------------

#: Row fields a card shows, besides the head. (key, caption). Every column of the table is
#: either here, in the head (id, expert, enabled, selection) or in the actions.
CARD_TILES: Tuple[Tuple[str, str], ...] = (
    ('priority', 'Priority'),
    ('virtual_equity_pct', 'Virtual equity %'),
    ('account_id', 'Account ID'),
    ('enter_market_ruleset_name', 'Enter market ruleset'),
    ('open_positions_ruleset_name', 'Open positions ruleset'),
)
CARD_WIDE = ('enter_market_ruleset_name', 'open_positions_ruleset_name')
#: The head and action columns, by table column name.
CARD_HEAD_COLUMNS = ('id', 'expert', 'alias', 'enabled')
CARD_ACTION_COLUMNS = ('actions',)


def expert_card_template() -> str:
    """The Vue ``item`` slot of the expert list in card mode. Pure.

    Same row dict as the desktop table; the checkbox is the row's own selection
    (``props.selected``), and the three buttons emit the SAME events with the SAME
    ``props`` as the desktop actions cell, so the handlers are shared."""
    tiles = ''.join(
        f'<div class="bm-xcard-tile{" bm-xcard-tile--wide" if key in CARD_WIDE else ""}">'
        f'<div class="bm-xcard-k">{caption}</div>'
        f'<div class="bm-xcard-v">{{{{ props.row.{key} }}}}</div></div>'
        for key, caption in CARD_TILES)
    return (
        '<div class="col-12" style="width:100%">'
        '<div class="bm-xcard" :class="{\'bm-xcard--sel\': props.selected}">'
        '<div class="bm-xcard-head">'
        '<q-checkbox v-model="props.selected" />'
        '<span class="bm-xcard-id">#{{ props.row.id }}</span>'
        '<span class="bm-xcard-type">{{ props.row.expert }}</span>'
        '<q-space />'
        '<q-icon :name="props.row.enabled ? \'check_circle\' : \'cancel\'" '
        ':color="props.row.enabled ? \'green\' : \'red\'" size="sm">'
        '<q-tooltip>{{ props.row.enabled ? \'Enabled\' : \'Disabled\' }}</q-tooltip></q-icon>'
        '</div>'
        '<div class="bm-xcard-alias" v-if="props.row.alias">{{ props.row.alias }}</div>'
        f'<div class="bm-xcard-tiles">{tiles}</div>'
        '<div class="bm-xcard-actions">'
        '<q-btn @click="$parent.$emit(\'edit\', props)" icon="edit" flat color="blue" label="Edit" />'
        '<q-btn @click="$parent.$emit(\'duplicate\', props)" icon="content_copy" flat color="green" />'
        '<q-btn @click="$parent.$emit(\'del\', props)" icon="delete" flat color="red" />'
        '</div></div></div>')


def check_card_covers_table(table_columns: Sequence[str]) -> None:
    """Refuse a list card that does not account for exactly the table's columns (a column
    added to the table without a phone decision would silently be missing from the card).
    Pure; raises ``ValueError``."""
    declared = (list(CARD_HEAD_COLUMNS) + [k for k, _ in CARD_TILES] + list(CARD_ACTION_COLUMNS))
    wanted = list(table_columns)
    missing = [c for c in wanted if c not in declared]
    unknown = [c for c in declared if c not in wanted]
    if missing or unknown:
        raise ValueError(f'columns with no phone decision: {missing}; '
                         f'card shows columns the table lacks: {unknown}')


__all__ = ['PHONE_MAX_WIDTH_PX', 'phone_section', 'dialog_tab', 'phone_css',
           'expert_card_template', 'check_card_covers_table', 'tab_short_labels']
