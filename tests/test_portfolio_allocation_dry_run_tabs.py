"""The dry-run dialog: two tabs, a wrapped Reasons column, a signed BP column.

THREE COMPLAINTS FROM ONE SCREENSHOT, all about reading the thing.

* "better to make 2 tabs here as it's unreadable, one tab with table and one with
  the text etc." Notices, the fractional switch, the market-order caveat and six
  warnings sat above the table and the totals below it, so the table -- the reason
  the dialog exists -- got a band in the middle.
* Reasons was one long line per row and pushed the table off screen sideways.
* Every SELL read ``BP COST 0.00``. A sale does not cost buying power, it frees
  it, and the engine now says so (``AllocationRow.bp_released`` /
  ``bp_effect``); this file is the display half.

WHAT STAYS OUT OF THE TABS, and why it is not an oversight. Submit, Cancel and
Refresh are the point of the dialog and are drawn as siblings of the panels, so
they are on screen whichever tab is open. So is the market-hours banner and the
``held symbol has no quote`` block: those two do not qualify the numbers, they say
Submit is OFF, and a blocker behind a tab the user has not opened is a blocker
that was not shown.

The fractional switch is on the ORDERS tab. It RE-SOLVES -- the table, the totals
and what Submit sends all move together -- so it belongs beside the thing it
visibly changes; putting it on the notices tab would make the user flip tabs to
see what they just did.

Colour is asserted through the paint registry, never by class alone: on this build
``text-green-500`` renders WHITE (see ``tests/test_ui_colour_classes_paint.py``).

MEASURED IN HEADLESS CHROME at 1600x1000, because none of what follows is visible
from Python: the table's viewport takes 72.6% of the dialog (it took ~63.8%),
Refresh/Cancel/Submit sit at y=955 of a 1000px viewport and are on screen from
BOTH tabs, the notices panel is 850px with nothing clipped
(``scrollHeight == clientHeight``), the release cell computes to
``rgb(34, 197, 94)`` and the charge to plain white, and the Reasons cell computes
to ``white-space: normal; -webkit-line-clamp: 3; max-width: 448px`` against a
1,178px unwrapped natural width.
"""
import pytest

from ba2_trade_platform.core.portfolio_allocation import (
    MARGIN_SOURCE_ASSET,
    VALUATION_MODE_MARKET,
    AllocationPlan,
    AllocationRow,
    BaseSnapshot,
)
from ba2_trade_platform.core.types import OrderDirection


def _run(coro):
    """Run one of the wizard's async click handlers to completion.

    ``_validate`` and ``_refresh`` are async because their broker call goes to a
    thread -- run inline on the event loop they outlived the websocket heartbeat
    and cost the user the dialog. Called from a sync test the coroutine would
    never start, and every assertion after it would pass on an unchanged page.

    THE SLOT IS RE-ENTERED INSIDE THE NEW TASK, which is not ceremony: NiceGUI
    keys its slot stack on the current TASK (``context.slot``), so a coroutine run
    by ``asyncio.run`` starts with an empty one and a bare ``ui.notify`` raises
    "the slot stack for this task is empty". That is exactly what
    ``events.handle_event`` does for a real click
    (``_await_and_handle_in_context``), so running it any other way here would
    test a context the browser never produces.
    """
    import asyncio
    from contextlib import nullcontext

    from nicegui import context

    try:
        slot = context.slot
    except RuntimeError:
        slot = nullcontext()

    async def _in_slot():
        with slot:
            return await coro

    return asyncio.run(_in_slot())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _wiz():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    return wiz


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page

    client = Client(nicegui_page('/test-dry-run-tabs'), request=None)
    try:
        yield client
    finally:
        Client.instances.pop(client.id, None)


def _open_market():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        MARKET_GATE_OPEN, MarketGateResult,
    )
    return MarketGateResult(allowed=True, reason_code=MARKET_GATE_OPEN, message='')


def _base():
    return BaseSnapshot(available_buying_power=1_000.0, managed_value=4_000.0,
                        base_notional=5_000.0, default_bp_factor=1.0,
                        valuation_mode=VALUATION_MODE_MARKET, cash=1_000.0,
                        supports_fractional=True)


LONG_REASON = (
    'rounded down to whole shares, target 34.48 buys 0.9170 shares at 37.60 - '
    'BUMPED UP to 1 share(s), 109% of target, scaled x0.30 to fit buying power, '
    'weight adjusted +1.0000 -> +2.0000 shares to keep label ‘Core’ on target')


def _plan(*, warnings=()):
    """One buy, one sell that funds it, and a very long reason on the buy.

    Both rows are sized FRACTIONALLY on purpose, so the plan produces no notice of
    its own: every notice in these tests is one the test put there, which is what
    lets the badge count be asserted exactly.
    """
    return AllocationPlan(
        rows=[
            AllocationRow(symbol='AAPL', price=160.0, delta_quantity=10.0,
                          target_quantity=10.0, side=OrderDirection.BUY,
                          estimated_value=1_600.0, bp_cost=1_600.0, bp_factor=1.0,
                          target_notional=1_600.0, initial_margin_rate=0.5,
                          margin_source=MARGIN_SOURCE_ASSET, fractional=True,
                          reasons=[LONG_REASON]),
            AllocationRow(symbol='MSFT', price=400.0, current_quantity=10.0,
                          current_cost_basis=3_000.0, delta_quantity=-5.0,
                          target_quantity=5.0, side=OrderDirection.SELL,
                          estimated_value=2_000.0, bp_released=2_000.0,
                          bp_factor=1.0, target_notional=2_000.0,
                          initial_margin_rate=0.5, fractional=True,
                          margin_source=MARGIN_SOURCE_ASSET),
        ],
        base_notional=5_000.0, available_buying_power=1_000.0,
        released_buying_power=2_000.0, required_buying_power=1_600.0,
        bp_usage_pct=1_600.0 / 3_000.0 * 100.0,
        total_buy_value=1_600.0, total_sell_value=2_000.0, allow_fractional=True,
        valuation_mode=VALUATION_MODE_MARKET, warnings=list(warnings))


def _draw(client, plan=None):
    wiz = _wiz()
    plan = _plan() if plan is None else plan
    with client:
        wiz.AllocationWizard(_base(), plan, market=_open_market(),
                             on_refresh=lambda f: (plan, _open_market()),
                             on_submit=lambda p: None).open()
    return client.layout


def _marked(root, marker):
    return [d for d in root.descendants()
            if marker in getattr(d, '_markers', [])]


def _marked_texts(root, marker):
    return [d.text for d in _marked(root, marker)]


def _texts(root):
    return [d.text for d in root.descendants() if getattr(d, 'text', None)]


def _only(root, marker):
    hits = _marked(root, marker)
    assert len(hits) == 1, f'{marker}: {len(hits)}'
    return hits[0]


def _ancestors(element):
    out = []
    node = element.parent_slot.parent if element.parent_slot else None
    while node is not None:
        out.append(node)
        node = node.parent_slot.parent if node.parent_slot else None
    return out


# ---------------------------------------------------------------------------
# ITEM 3 -- TWO TABS
# ---------------------------------------------------------------------------

def test_the_dialog_has_exactly_two_tabs(nicegui_client):
    from nicegui import ui

    root = _draw(nicegui_client)
    tabs = [el for el in root.descendants() if isinstance(el, ui.tab)]

    assert len(tabs) == 2
    assert [el._props.get('name') for el in tabs] == [
        _wiz().TAB_ORDERS, _wiz().TAB_NOTICES]


def test_the_order_table_lives_on_the_orders_tab(nicegui_client):
    from nicegui import ui

    wiz = _wiz()
    root = _draw(nicegui_client)
    viewport = _only(root, wiz.MARKER_ROWS_VIEWPORT)
    panels = [el for el in _ancestors(viewport) if isinstance(el, ui.tab_panel)]

    assert [el._props.get('name') for el in panels] == [wiz.TAB_ORDERS]


def test_the_notices_and_the_totals_live_on_the_OTHER_tab(nicegui_client):
    from nicegui import ui

    wiz = _wiz()
    root = _draw(nicegui_client, _plan(warnings=['label A has no symbols']))
    for marker in (wiz.MARKER_PLAN_WARNING, wiz.MARKER_BP_BUDGET):
        element = _only(root, marker)
        panels = [el for el in _ancestors(element) if isinstance(el, ui.tab_panel)]
        assert [el._props.get('name') for el in panels] == [wiz.TAB_NOTICES], marker


def test_the_three_buttons_are_reachable_from_BOTH_tabs(nicegui_client):
    """They are the point of the dialog. Drawn as siblings of the panels, so no
    tab can hide them -- and drawn ONCE, so there is no second Submit to press.

    The SELECTION buttons ('Select all', 'Deselect all' and the per-label pairs)
    are deliberately not in this list: they act on the order table and live inside
    the orders panel with it. What the assertion protects is that the three
    buttons which END the dialog are outside every panel and appear once each.
    """
    from nicegui import ui

    buttons = [el for el in _draw(nicegui_client).descendants()
               if isinstance(el, ui.button)]
    outside = [el for el in buttons
               if not [a for a in _ancestors(el) if isinstance(a, ui.tab_panel)]]

    assert [el.text for el in outside] == ['Refresh', 'Cancel', 'Submit']


def test_the_notices_tab_says_how_many_there_are_without_being_opened(
        nicegui_client):
    """A tab is a place things hide. The badge is what stops six warnings hiding
    behind one."""
    wiz = _wiz()
    warnings = [f"label 'L{i}' has no symbols" for i in range(6)]
    root = _draw(nicegui_client, _plan(warnings=warnings))
    drawn = (len(_marked_texts(root, wiz.MARKER_PLAN_WARNING))
             + len(_marked_texts(root, wiz.MARKER_PLAN_NOTICE)))

    assert drawn == 6
    assert _marked_texts(root, wiz.MARKER_NOTICE_COUNT) == ['6']


def test_the_badge_is_not_drawn_when_the_plan_has_nothing_to_say(nicegui_client):
    """A "0" on the tab is noise, and noise is what hides the real case."""
    wiz = _wiz()
    root = _draw(nicegui_client, _plan())

    assert _marked_texts(root, wiz.MARKER_PLAN_NOTICE) == []
    assert _marked_texts(root, wiz.MARKER_PLAN_WARNING) == []
    assert _marked_texts(root, wiz.MARKER_NOTICE_COUNT) == []


def test_the_badge_survives_a_refresh(nicegui_client):
    """The count is rebuilt with the notices it counts; a stale badge over a new
    solve is the same defect the plan warnings had before they moved."""
    wiz = _wiz()
    first = _plan(warnings=[f'w{i}' for i in range(6)])
    second = _plan(warnings=['only one now'])
    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), first, market=_open_market(),
                                      on_refresh=lambda f: (second, _open_market()),
                                      on_submit=lambda p: None)
        wizard.open()
        _run(wizard._refresh(False))

    assert _marked_texts(nicegui_client.layout, wiz.MARKER_NOTICE_COUNT) == ['1']


def test_the_fractional_switch_is_on_the_tab_it_changes(nicegui_client):
    """It RE-SOLVES. Put it with the notices and the user has to change tabs to
    see what they just did to the orders."""
    from nicegui import ui

    wiz = _wiz()
    root = _draw(nicegui_client)
    switch = [el for el in root.descendants() if isinstance(el, ui.switch)]

    assert len(switch) == 1
    panels = [el for el in _ancestors(switch[0]) if isinstance(el, ui.tab_panel)]
    assert [el._props.get('name') for el in panels] == [wiz.TAB_ORDERS]


def test_a_blocking_banner_is_NOT_behind_a_tab(nicegui_client):
    """``held symbol has no quote`` does not qualify the numbers, it says Submit
    is off. Behind an unopened tab it says nothing at all."""
    from nicegui import ui

    wiz = _wiz()
    base = _base()
    base.unpriced_held_symbols = ['MSFT']
    plan = _plan()
    with nicegui_client:
        wiz.AllocationWizard(base, plan, market=_open_market(),
                             on_refresh=lambda f: (plan, _open_market()),
                             on_submit=lambda p: None).open()
    block = _only(nicegui_client.layout, wiz.MARKER_BASE_BLOCK)

    assert not [el for el in _ancestors(block) if isinstance(el, ui.tab_panel)]


# ---------------------------------------------------------------------------
# ITEM 4 -- THE REASONS COLUMN WRAPS, AND KEEPS ALL ITS TEXT
# ---------------------------------------------------------------------------

def test_the_reasons_cell_wraps_instead_of_running_off_the_screen(nicegui_client):
    """The bound on the WIDTH is what actually stops the row: every row is
    ``min-w-max``, so without one the cell grows to fit the longest reason and
    takes the table off the right-hand edge with it. Measured in headless Chrome
    at 1600x1000: 1,178px unwrapped against the 448px cap, and the table's own
    horizontal scroll width falls from ~3,030px to 2,300px.

    Written as INLINE CSS. Tailwind's layout utilities DO reach this build
    (measured: ``w-24`` -> 96px) even though its colour utilities do not
    (``text-orange-400`` -> white, see ``tests/test_ui_colour_classes_paint.py``),
    so a class would have worked -- but ``line-clamp`` needs the ``-webkit-`` pair,
    which is not a utility, and this is the one rule in the dialog whose failure is
    invisible: an uncapped cell just looks like a wide table."""
    wiz = _wiz()
    root = _draw(nicegui_client)
    cell = _marked(root, wiz.MARKER_ROW_REASONS)[0]
    style = ' '.join(f'{k}: {v};' for k, v in cell._style.items())

    assert 'white-space: normal' in style
    assert 'max-width' in style
    assert 'overflow-wrap' in style


def test_the_reasons_cell_is_clamped_rather_than_left_to_grow_the_row(
        nicegui_client):
    """Three lines of a four-line reason on every row is a table nobody can scan.
    Clamped with an ellipsis, so the truncation is VISIBLE."""
    wiz = _wiz()
    root = _draw(nicegui_client)
    style = ' '.join(f'{k}: {v};'
                     for k, v in _marked(root, wiz.MARKER_ROW_REASONS)[0]._style.items())

    assert 'line-clamp' in style
    assert 'overflow: hidden' in style


def test_the_full_reason_is_one_hover_away_and_never_truncated(nicegui_client):
    """Clamping may not LOSE the explanation -- that is the column's whole
    purpose. The tooltip carries every character of it."""
    wiz = _wiz()
    root = _draw(nicegui_client)
    cell = _marked(root, wiz.MARKER_ROW_REASONS)[0]
    tips = [d.text for d in cell.descendants() if getattr(d, 'text', None)]

    assert LONG_REASON in tips


def test_a_row_with_no_reason_gets_no_empty_tooltip(nicegui_client):
    """An empty tooltip is a mystery box that opens onto nothing."""
    wiz = _wiz()
    root = _draw(nicegui_client)
    cell = _marked(root, wiz.MARKER_ROW_REASONS)[1]          # MSFT has no reasons

    assert cell.text == ''
    assert [d for d in cell.descendants() if getattr(d, 'text', None)] == []


# ---------------------------------------------------------------------------
# ITEM 1 (display half) -- A SELL FREES BUYING POWER AND THE COLUMN SAYS SO
# ---------------------------------------------------------------------------

def test_the_column_is_no_longer_called_a_cost(nicegui_client):
    """"BP cost 0.00" on a sale is not a rounding blemish, it is the wrong word:
    it reads as "this trade does nothing to your buying power"."""
    root = _draw(nicegui_client)
    texts = _texts(root)

    assert 'BP effect' in texts
    assert 'BP cost' not in texts


def test_a_sell_shows_the_buying_power_it_FREES_with_a_plus(nicegui_client):
    wiz = _wiz()
    root = _draw(nicegui_client)

    assert _marked_texts(root, wiz.MARKER_BP_EFFECT) == ['-1,600.00', '+2,000.00']


def test_the_release_is_painted_and_the_charge_is_not(nicegui_client):
    """Green is the cue that this row hands money back. The charge is the
    ordinary case; painting every buy would be noise, not a signal.

    Through the registry, because ``text-green-500`` paints NOTHING on this build
    and a class-only fix would render the release in plain white."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        UNPAINTED_CLASS_COLORS,
    )

    wiz = _wiz()
    root = _draw(nicegui_client)
    charge, release = _marked(root, wiz.MARKER_BP_EFFECT)

    assert release._style.get('color') == \
        UNPAINTED_CLASS_COLORS['text-green-500'] + ' !important'
    assert 'color' not in charge._style


def test_every_bp_effect_cell_explains_itself(nicegui_client):
    """A signed money column with no legend is a column half the readers will get
    backwards."""
    wiz = _wiz()
    root = _draw(nicegui_client)
    charge, release = _marked(root, wiz.MARKER_BP_EFFECT)

    assert any('frees' in (d.text or '') for d in release.descendants())
    assert any('charges' in (d.text or '') for d in charge.descendants())


def test_the_footer_names_the_budget_and_where_it_came_from(nicegui_client):
    """"Required BP: 359.26 / 394.87 (91.0%)" was measured against the money the
    broker had published BEFORE the sells. The denominator is the budget now, and
    the line shows both halves of it rather than a number the user cannot find on
    any other screen."""
    wiz = _wiz()
    root = _draw(nicegui_client)
    line = _marked_texts(root, wiz.MARKER_BP_BUDGET)

    assert len(line) == 1
    assert '1,600.00' in line[0]        # required
    assert '3,000.00' in line[0]        # budget = 1,000 on hand + 2,000 freed
    assert '1,000.00' in line[0] and '2,000.00' in line[0]
    assert '53.3%' in line[0]


def test_a_plan_with_no_sells_does_not_invent_a_breakdown(nicegui_client):
    """"1,000.00 on hand + 0.00 freed" on every buy-only plan is noise."""
    wiz = _wiz()
    plan = _plan()
    plan.rows = [r for r in plan.rows if r.symbol == 'AAPL']
    plan.released_buying_power = 0.0
    plan.total_sell_value = 0.0
    root = _draw(nicegui_client, plan)
    line = _marked_texts(root, wiz.MARKER_BP_BUDGET)[0]

    assert 'freed' not in line
    assert '1,000.00' in line


def test_unticking_the_funding_sell_is_visible_in_the_footer(nicegui_client):
    """The dry run's answer to "what if that close does not happen?" -- the buys
    were sized against money this row was going to free."""
    wiz = _wiz()
    plan = _plan()
    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), plan, market=_open_market(),
                                      on_refresh=lambda f: (plan, _open_market()),
                                      on_submit=lambda p: None)
        wizard.open()
        wizard._toggle('MSFT', False)
    line = _marked_texts(nicegui_client.layout, wiz.MARKER_BP_BUDGET)[-1]

    assert 'freed' not in line
    assert '1,000.00' in line
    assert wiz.BP_OVER_BUDGET_NOTE in _texts(nicegui_client.layout)
