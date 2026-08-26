"""A colour class that does not paint is a lie the source keeps telling.

``ui/static/styles.css`` hand-writes nine Tailwind-shaped colour classes for the
dark theme and nothing generates the rest. NiceGUI 3.8 ships a Tailwind 4 browser
build -- which is why this looked safe -- but that build only compiles a
``<style type="text/tailwindcss">`` source block and this app has none, so it
emits no utilities at all. With no rule anywhere, ``styles.css``'s own
``p, span, div, label, ... { color: #ffffff }`` is the last thing standing and the
class computes to PLAIN WHITE.

Measured in headless Chrome against the real page head, one synthetic div per
class::

    text-gray-400   -> rgb(176, 190, 197)     (painted by styles.css)
    text-orange-600 -> rgb(255, 169, 77)      (painted by styles.css)
    text-orange-400 -> rgb(255, 255, 255)     WHITE
    text-green-500  -> rgb(255, 255, 255)     WHITE
    text-red-500    -> rgb(255, 255, 255)     WHITE
    text-red-400    -> rgb(255, 255, 255)     WHITE
    text-blue-400   -> rgb(255, 255, 255)     WHITE
    text-sky-400    -> rgb(255, 255, 255)     WHITE
    text-yellow-500 -> rgb(255, 255, 255)     WHITE

That had made the dry-run dialog's BUY green, its SELL red, its leverage verdicts,
its off-target residual and the allocation page's "raising the reserve sells"
warning indistinguishable from ordinary body text since the day each was written.
Three separate attempts to fix this class of defect looked right in Python and did
not paint, because nothing in Python can see the cascade.

THIS FILE IS WHAT MAKES IT VISIBLE FROM PYTHON. It does not re-measure the browser
-- it pins the measurement:

1. ``STYLESHEET_COLOR_CLASSES`` is checked against ``styles.css`` itself, so
   deleting a rule there fails here rather than silently whitening a screen.
2. Every colour class the two allocation screens NAME must be either in that set
   or in ``UNPAINTED_CLASS_COLORS``, so a new one cannot be introduced with no
   colour behind it at all.
3. Both screens are RENDERED, and every element wearing an unpaintable class must
   also carry an inline ``color: ... !important``. This is the one that catches
   the real mistake, which is not "an unknown class" but "a known class and
   nobody painted it".

Why inline and why ``!important``: ``styles.css`` greys ``.q-expansion-item
.q-icon`` with an ``!important`` of its own, and a stylesheet ``!important`` beats
a plain inline declaration. An inline ``!important`` is the top of the cascade and
is the only thing that reliably wins here.
"""
import re
from pathlib import Path

import pytest

from ba2_trade_platform.core.portfolio_allocation import (
    MARGIN_SOURCE_ASSET,
    MARGIN_SOURCE_DEFAULT,
    MARGIN_SOURCE_POSITION,
    REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT,
    SIZING_OUTCOME_BUMPED,
    VALUATION_MODE_MARKET,
    AllocationPlan,
    AllocationRow,
    BaseSnapshot,
    PositionState,
)
from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    STYLESHEET_COLOR_CLASSES,
    UNPAINTED_CLASS_COLORS,
    ManagedLabel,
    MarketGateResult,
    MARKET_GATE_OPEN,
    build_label_views,
    class_color_style,
    color_classes,
    positions_by_symbol,
)

UI_DIR = Path(__file__).resolve().parents[1] / 'ba2_trade_platform' / 'ui'
STYLES_CSS = UI_DIR / 'static' / 'styles.css'

#: The files this guard covers. Deliberately NOT every screen: the rest of the UI
#: names these classes too (see the module docstring of the batch that added this)
#: and repainting it is a separate decision. Widening this tuple is the way to
#: bring another screen under the guard.
GUARDED_SOURCES = (
    UI_DIR / 'pages' / 'portfolio_allocation.py',
    UI_DIR / 'pages' / 'portfolio_allocation_wizard.py',
    UI_DIR / 'utils' / 'portfolio_allocation_view.py',
)

#: A colour class as it appears in a CSS selector or in a Python class string.
_CSS_SELECTOR_RE = re.compile(r'\.(text-[a-z]+-\d+)\s*(?:,|\{)')
_SOURCE_RE = re.compile(r'\btext-[a-z]+-\d+\b')


# ---------------------------------------------------------------------------
# 1. The stylesheet is the source of truth for what paints itself
# ---------------------------------------------------------------------------

def test_the_registry_lists_exactly_the_colour_classes_styles_css_defines():
    """Pinned against the file, not against a memory of it.

    Both directions matter. A class deleted from ``styles.css`` and left in the
    registry means an element the code believes is painted and is not; a class
    added there and left out means the code paints over the theme with Tailwind's
    hue instead of the dark theme's.
    """
    defined = set(_CSS_SELECTOR_RE.findall(STYLES_CSS.read_text(encoding='utf-8')))
    # Quasar ships its own ``.text-grey-N`` palette and wins over these; they are
    # not Tailwind-shaped names the Python side ever writes, so they are not part
    # of the contract.
    defined = {name for name in defined if not name.startswith('text-grey-')}
    assert defined == set(STYLESHEET_COLOR_CLASSES)


def test_no_class_is_claimed_both_painted_and_unpainted():
    """A class in both collections would make ``class_color_style`` ambiguous."""
    assert not (set(STYLESHEET_COLOR_CLASSES) & set(UNPAINTED_CLASS_COLORS))


def test_every_unpainted_colour_is_a_real_hex():
    for name, color in UNPAINTED_CLASS_COLORS.items():
        assert re.fullmatch(r'#[0-9A-Fa-f]{6}', color), f'{name} -> {color!r}'


def test_a_class_the_stylesheet_paints_is_left_to_the_stylesheet():
    """Deferring is the point: ``.text-gray-400`` is ``#b0bec5`` in this theme and
    ``#9ca3af`` in Tailwind, so restating it here would fork the dark theme."""
    assert class_color_style('text-xs text-gray-400') == ''
    assert class_color_style('') == ''
    assert class_color_style('text-sm font-bold') == ''


def test_a_class_the_stylesheet_cannot_paint_gets_an_inline_important():
    assert class_color_style('text-xs text-orange-400') == 'color: #FB923C !important'


def test_an_unknown_colour_class_is_refused_rather_than_left_white():
    """The loud failure is the feature. A silent '' would put the element back to
    white, which is the entire defect."""
    with pytest.raises(KeyError):
        class_color_style('text-fuchsia-300')


# ---------------------------------------------------------------------------
# 2. Nothing on these two screens names a colour nobody has a value for
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('source', GUARDED_SOURCES, ids=lambda p: p.name)
def test_every_colour_class_named_by_the_allocation_screens_has_a_colour(source):
    named = set(_SOURCE_RE.findall(source.read_text(encoding='utf-8')))
    known = set(STYLESHEET_COLOR_CLASSES) | set(UNPAINTED_CLASS_COLORS)
    assert named <= known, f'{sorted(named - known)} would render white'


# ---------------------------------------------------------------------------
# 3. ...and every element that WEARS one is actually painted
# ---------------------------------------------------------------------------

def _unpainted(root):
    """Elements carrying a colour class the stylesheet cannot paint, unpainted.

    Reads the two things the browser reads: the class list and the inline style.
    A ``color`` without ``!important`` does not count -- ``styles.css`` is written
    almost entirely in ``!important`` and beats a plain inline declaration.
    """
    offenders = []
    for element in root.descendants(include_self=True):
        classes = ' '.join(element._classes)
        needs = [name for name in color_classes(classes)
                 if name not in STYLESHEET_COLOR_CLASSES]
        if not needs:
            continue
        painted = '!important' in (element._style.get('color') or '')
        if not painted:
            offenders.append((needs, classes, element._text))
    return offenders


def _painted_color(root, text):
    """The inline colour of the first element rendering exactly ``text``."""
    for element in root.descendants(include_self=True):
        if element._text == text:
            return element._style.get('color')
    raise AssertionError(f'nothing rendered {text!r}')


@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw. No browser."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-ui-colour-classes'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _colourful_plan():
    """One plan that reaches every coloured branch of the dry-run table.

    A BUY and a SELL (green/red), a buying-power-penalised row and a genuinely
    leveraged one, a row with no published rate, a fractional row that was also
    bumped and redistributed, and a suppressed row -- plus a reserve, a residual
    over 1% and a buying-power requirement above what is available, which are the
    three coloured lines in the totals.
    """
    rows = [
        AllocationRow(symbol='AAPL', price=160.0, delta_quantity=10.0,
                      side=OrderDirection.BUY, estimated_value=1_600.0,
                      bp_cost=800.0, bp_factor=0.5, target_notional=1_600.0,
                      current_quantity=10.0, current_cost_basis=1_200.0,
                      initial_margin_rate=0.25, margin_source=MARGIN_SOURCE_ASSET),
        AllocationRow(symbol='MSFT', price=400.0, delta_quantity=-5.0,
                      side=OrderDirection.SELL, estimated_value=2_000.0,
                      bp_cost=0.0, bp_factor=1.0, target_notional=2_000.0,
                      current_quantity=10.0, current_cost_basis=3_000.0,
                      initial_margin_rate=0.5, margin_source=MARGIN_SOURCE_ASSET),
        AllocationRow(symbol='LAZR', price=10.0, delta_quantity=1.66666,
                      side=OrderDirection.BUY, estimated_value=16.67,
                      bp_cost=33.0, bp_factor=1.978, marginable=False,
                      target_notional=16.67, fractional=True,
                      initial_margin_rate=0.989,
                      margin_source=MARGIN_SOURCE_POSITION,
                      sizing_outcome=SIZING_OUTCOME_BUMPED, redistributed=True,
                      reasons=['fractional']),
        AllocationRow(symbol='NEWBIE', price=50.0, delta_quantity=10.0,
                      side=OrderDirection.BUY, estimated_value=500.0,
                      bp_cost=1_000.0, bp_factor=2.0, target_notional=500.0,
                      initial_margin_rate=None,
                      margin_source=MARGIN_SOURCE_DEFAULT),
        AllocationRow(symbol='PENNY', price=3.0, delta_quantity=0.0, side=None,
                      fractional=True, target_notional=4.0, unmet_notional=1.95,
                      reasons=[REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.format(
                          value=1.95, minimum=5.0)]),
    ]
    return AllocationPlan(rows=rows, base_notional=15_000.0,
                          available_buying_power=1_000.0,
                          required_buying_power=1_833.0, bp_usage_pct=183.3,
                          total_buy_value=2_116.67, total_sell_value=2_000.0,
                          allow_fractional=True,
                          valuation_mode=VALUATION_MODE_MARKET,
                          reserved_pct=5.0, reserved_notional=750.0,
                          warnings=['plan warning one', 'plan warning two',
                                    'plan warning three'])


def _colourful_base():
    return BaseSnapshot(available_buying_power=1_000.0, managed_value=5_000.0,
                        base_notional=15_000.0, default_bp_factor=1.0,
                        valuation_mode=VALUATION_MODE_MARKET, cash=100.0,
                        supports_fractional=True,
                        warnings=['a base warning'])


def _open_dry_run(client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    market = MarketGateResult(allowed=True, reason_code=MARKET_GATE_OPEN, message='')
    plan = _colourful_plan()
    with client:
        wiz.AllocationWizard(_colourful_base(), plan, market=market,
                             on_refresh=lambda f: (plan, market),
                             on_submit=lambda p: None).open()
    return client.layout


def test_every_coloured_element_in_the_dry_run_dialog_is_painted(nicegui_client):
    offenders = _unpainted(_open_dry_run(nicegui_client))
    assert offenders == [], (
        'these dry-run elements wear a colour class that paints nothing on this '
        f'build and carry no inline colour, so they render white: {offenders}')


def test_the_dry_run_paints_buy_green_and_sell_red(nicegui_client):
    """The user's own two examples, and the reason the whole dialog was repainted:
    the column that says which way real money moves read as plain text."""
    layout = _open_dry_run(nicegui_client)
    assert _painted_color(layout, 'BUY') == UNPAINTED_CLASS_COLORS['text-green-500'] \
        + ' !important'
    assert _painted_color(layout, 'SELL') == UNPAINTED_CLASS_COLORS['text-red-500'] \
        + ' !important'


def test_the_income_panel_paints_its_unallocated_total(nicegui_client):
    """Drawn on the allocation page, so it is in this batch's scope."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel([], 1_234.0, on_sync=lambda: None,
                                on_invest=lambda amount: None, working_note=None)
    assert _unpainted(nicegui_client.layout) == []


# ---------------------------------------------------------------------------
# ...and the page itself
# ---------------------------------------------------------------------------

def _pos(symbol, quantity, cost, value):
    return PositionState(symbol=symbol, quantity=float(quantity),
                         cost_basis=float(cost), market_value=float(value))


def _render_page_body(client, *, targets, reserve, buying_power=1_000.0):
    from ba2_trade_platform.ui.pages import portfolio_allocation as page

    symbols_by_label = {label: ['AAPL'] for label, _pct in targets}
    book = positions_by_symbol([_pos('AAPL', 10, 1_000.0, 2_500.0)])
    views = build_label_views([ManagedLabel(label, pct) for label, pct in targets],
                              symbols_by_label, book, {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0, unallocated_pct=reserve,
                              symbol_weights={label: {'AAPL': 100.0}
                                              for label, _pct in targets})
    payload = {'views': views, 'symbols_by_label': symbols_by_label,
               'valuation_mode': VALUATION_MODE_MARKET, 'base_notional': 10_000.0,
               'available_buying_power': buying_power, 'account_value': 12_000.0,
               'unallocated_pct': reserve}

    async def _noop():
        pass

    with client:
        page._render_labels(1, payload, _noop)
    return client.layout


@pytest.mark.parametrize('targets,reserve', [
    # On target: the totals footer and the label-total readout read 'ok'.
    ([('ARK26', 60.0), ('TECH', 30.0)], 10.0),
    # Over 100: both go 'negative', which is the red that never painted.
    ([('ARK26', 70.0), ('TECH', 48.0)], 0.0),
    # Well under: both go 'warning'.
    ([('ARK26', 20.0)], 0.0),
], ids=['ok', 'over-100', 'under'])
def test_every_coloured_element_on_the_allocation_page_is_painted(
        nicegui_client, targets, reserve):
    offenders = _unpainted(_render_page_body(nicegui_client, targets=targets,
                                             reserve=reserve))
    assert offenders == [], (
        'these allocation-page elements wear a colour class that paints nothing '
        f'and carry no inline colour, so they render white: {offenders}')


def test_the_reserve_sell_warning_is_painted(nicegui_client):
    """The one sentence saying that dragging the reserve up SELLS. It is the whole
    reason the control is safe to have on this page, and it was white."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        RESERVE_SELL_WARNING,
    )
    layout = _render_page_body(nicegui_client, targets=[('ARK26', 60.0)],
                               reserve=10.0)
    assert _painted_color(layout, RESERVE_SELL_WARNING) == \
        UNPAINTED_CLASS_COLORS['text-orange-400'] + ' !important'


def test_every_severity_the_totals_can_report_has_a_colour():
    """The three readouts are rewritten IN PLACE as the user types, so a severity
    with no colour would leave the previous severity's paint on screen."""
    from ba2_trade_platform.ui.pages import portfolio_allocation as page
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        LABEL_TOTAL_CLASSES, LABEL_TOTAL_COLORS,
    )
    assert set(LABEL_TOTAL_COLORS) >= set(LABEL_TOTAL_CLASSES)
    assert set(LABEL_TOTAL_COLORS) >= set(page.FOOTER_CLASSES)
