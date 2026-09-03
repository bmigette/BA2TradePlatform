"""The dry run's SELECTION toolbar, its column FOOTER, and the label row's
zero-share badge. All three arrived from live use on 2026-09-03:

    "In the table for review, I want to confirm that unchecked items won't
    trigger orders. I also want to add totals in the bottom of the dry run table,
    and buttons on top to select / select all and one button per label to select
    / deselect all orders from a label."

    "near the label logo, put a orange badge with amount of symbols that is 0 or
    null share. No badge if all are >0%"

The first sentence is the one with money in it: un-ticking a row must be
provably the same thing as not sending that order. ``filter_plan_rows`` has
always been what Submit consumes, so the tests here pin the whole path -- the
toolbar moves the selection, the boxes and the footer follow it, and Submit hands
on exactly what is ticked.

Kept out of ``test_portfolio_allocation_wizard_ui.py`` only because that file is
already 2,200 lines; the fixtures are deliberately the same shape.
"""
import pytest

from ba2_trade_platform.core.portfolio_allocation import (
    REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT,
    AllocationPlan,
    AllocationRow,
    BaseSnapshot,
    VALUATION_MODE_MARKET,
)
from ba2_trade_platform.core.types import OrderDirection


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _base():
    return BaseSnapshot(available_buying_power=10_000.0, managed_value=0.0,
                        base_notional=15_000.0, default_bp_factor=1.0,
                        valuation_mode=VALUATION_MODE_MARKET, cash=10_000.0)


def _open_market():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        MARKET_GATE_OPEN, MarketGateResult,
    )
    return MarketGateResult(allowed=True, reason_code=MARKET_GATE_OPEN, message="")


def _labelled_plan():
    """Two labels, one symbol in BOTH of them, one suppressed row.

    BOTH is the case the per-label buttons have to get right, and PENNY is the one
    'Select all' must not resurrect: the broker's $5 fractional floor already
    refused it, so there is no order to send.
    """
    return AllocationPlan(
        rows=[
            AllocationRow(symbol="AAPL", labels=["ARK26"], price=160.0,
                          current_quantity=2.0, current_cost_basis=300.0,
                          target_notional=1920.0, delta_quantity=10.0,
                          side=OrderDirection.BUY, estimated_value=1600.0,
                          bp_cost=1600.0, bp_factor=1.0),
            AllocationRow(symbol="BOTH", labels=["ARK26", "NASDAQ30"], price=100.0,
                          current_quantity=1.0, current_cost_basis=90.0,
                          target_notional=500.0, delta_quantity=4.0,
                          side=OrderDirection.BUY, estimated_value=400.0,
                          bp_cost=400.0, bp_factor=1.0),
            AllocationRow(symbol="MSFT", labels=["NASDAQ30"], price=400.0,
                          current_quantity=8.0, current_cost_basis=2800.0,
                          target_notional=1200.0, delta_quantity=-5.0,
                          side=OrderDirection.SELL, estimated_value=2000.0,
                          bp_released=2000.0),
            AllocationRow(
                symbol="PENNY", labels=["NASDAQ30"], price=3.0, delta_quantity=0.0,
                side=None, fractional=True,
                reasons=[REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.format(
                    value=1.95, minimum=5.0)]),
        ],
        base_notional=15_000.0, available_buying_power=10_000.0,
        required_buying_power=2000.0, bp_usage_pct=20.0,
        total_buy_value=2000.0, total_sell_value=2000.0)


def _fresh_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page

    return Client(nicegui_page('/test-allocation-selection'), request=None)


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client

    client = _fresh_client()
    try:
        yield client
    finally:
        Client.instances.pop(client.id, None)


def _texts(element):
    return [d.text for d in element.descendants() if getattr(d, 'text', None)]


def _marked(root, marker):
    return [d for d in root.descendants() if marker in getattr(d, '_markers', [])]


def _buttons_marked(root, marker):
    from nicegui import ui

    return [el for el in _marked(root, marker) if isinstance(el, ui.button)]


def _open(nicegui_client, submitted=None, plan=None):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    wizard = wiz.AllocationWizard(
        _base(), plan if plan is not None else _labelled_plan(),
        market=_open_market(),
        on_refresh=lambda f: pytest.fail("refresh not expected"),
        on_submit=(submitted.append if submitted is not None else (lambda p: None)))
    wizard.open()
    return wiz, wizard


# ---------------------------------------------------------------------------
# the toolbar
# ---------------------------------------------------------------------------

def test_the_dry_run_draws_select_all_deselect_all_and_one_pair_per_label(nicegui_client):
    with nicegui_client:
        wiz, _wizard = _open(nicegui_client)
        root = nicegui_client.layout
        select_all = _buttons_marked(root, wiz.MARKER_SELECT_ALL)
        deselect_all = _buttons_marked(root, wiz.MARKER_DESELECT_ALL)
        per_label = _buttons_marked(root, wiz.MARKER_LABEL_SELECT)
        texts = _texts(root)

    assert len(select_all) == 1 and len(deselect_all) == 1
    # ARK26 and NASDAQ30, an ``all`` and a ``none`` each. PENNY's label adds no
    # third pair: it is NASDAQ30, and a label whose only row is suppressed
    # contributes nothing to tick.
    assert [b._props['label'] for b in per_label] == ['all', 'none', 'all', 'none']
    assert 'ARK26:' in texts and 'NASDAQ30:' in texts


def test_deselect_all_then_submit_sends_nothing(nicegui_client):
    """THE point of the toolbar: an un-ticked row is not an order."""
    submitted = []
    with nicegui_client:
        _wiz, wizard = _open(nicegui_client, submitted)
        assert wizard.selected == {"AAPL", "BOTH", "MSFT"}
        wizard._select_all(False)
        wizard._submit()

    assert wizard.selected == set()
    assert submitted == []


def test_select_all_restores_only_the_rows_that_have_an_order(nicegui_client):
    with nicegui_client:
        _wiz, wizard = _open(nicegui_client)
        wizard._select_all(False)
        wizard._select_all(True)

    # PENNY stays out: the broker already refused it, and 'Select all' must not
    # put a refused order back into what Submit sends.
    assert wizard.selected == {"AAPL", "BOTH", "MSFT"}


def test_the_per_label_none_button_unticks_every_order_carrying_that_label(nicegui_client):
    submitted = []
    with nicegui_client:
        _wiz, wizard = _open(nicegui_client, submitted)
        wizard._select_label("ARK26", False)
        assert wizard.selected == {"MSFT"}
        wizard._submit()

    # BOTH goes with ARK26 although it also carries NASDAQ30: "none for ARK26"
    # means no ARK26 order leaves, whatever else the symbol belongs to.
    assert [r.symbol for r in submitted[0].rows] == ["MSFT"]
    assert submitted[0].total_buy_value == pytest.approx(0.0)
    assert submitted[0].total_sell_value == pytest.approx(2000.0)


def test_the_per_label_all_button_ticks_back_only_that_labels_orders(nicegui_client):
    with nicegui_client:
        _wiz, wizard = _open(nicegui_client)
        wizard._select_all(False)
        wizard._select_label("NASDAQ30", True)

    assert wizard.selected == {"BOTH", "MSFT"}


def test_the_row_boxes_follow_a_toolbar_selection(nicegui_client):
    """The toolbar moves ``selected``; the boxes have to show it, or the user reads
    one selection off the screen while Submit sends another."""
    with nicegui_client:
        wiz, wizard = _open(nicegui_client)
        wizard._select_label("ARK26", False)
        ticks = _marked(nicegui_client.layout, wiz.MARKER_ROW_TICK)

    # The rows are redrawn, so the LAST four boxes are the live ones, in plan
    # order: AAPL, BOTH, MSFT, PENNY.
    assert [t.value for t in ticks[-4:]] == [False, False, True, False]


def test_a_plan_with_nothing_to_send_draws_no_toolbar(nicegui_client):
    """A toolbar whose every button is a no-op is furniture."""
    plan = AllocationPlan(
        rows=[AllocationRow(
            symbol="PENNY", labels=["ARK26"], price=3.0, delta_quantity=0.0,
            side=None, fractional=True,
            reasons=[REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.format(
                value=1.95, minimum=5.0)])],
        base_notional=15_000.0, available_buying_power=10_000.0)
    with nicegui_client:
        wiz, _wizard = _open(nicegui_client, plan=plan)
        root = nicegui_client.layout

        assert _buttons_marked(root, wiz.MARKER_SELECT_ALL) == []
        assert _buttons_marked(root, wiz.MARKER_LABEL_SELECT) == []


# ---------------------------------------------------------------------------
# the footer
# ---------------------------------------------------------------------------

def test_the_table_footer_totals_the_ticked_rows(nicegui_client):
    with nicegui_client:
        wiz, _wizard = _open(nicegui_client)
        foot = _marked(nicegui_client.layout, wiz.MARKER_TABLE_FOOT)[-1]
        texts = _texts(foot)

    # Three sendable rows, all ticked: cost 300 + 90 + 2800 = 3,190; buys
    # 1,600 + 400; sells 2,000; target 1,920 + 500 + 1,200 = 3,620; BP effect
    # -1,600 - 400 + 2,000 = 0.
    assert wiz.FOOTER_CAPTION_FMT.format(ticked=3, sendable=3) in texts
    assert "3,190.00" in texts
    assert "B 2,000.00" in texts and "S 2,000.00" in texts
    assert "3,620.00" in texts
    assert "0.00" in texts


def test_the_footer_follows_a_tick(nicegui_client):
    with nicegui_client:
        wiz, wizard = _open(nicegui_client)
        wizard._toggle("MSFT", False)
        foot = _marked(nicegui_client.layout, wiz.MARKER_TABLE_FOOT)[-1]
        texts = _texts(foot)

    # The sell and its 2,800 of cost basis leave the footer; the buys stay, and
    # the BP effect becomes the charge with nothing freeing it.
    assert wiz.FOOTER_CAPTION_FMT.format(ticked=2, sendable=3) in texts
    assert "390.00" in texts
    assert "B 2,000.00" in texts and "S 0.00" in texts
    assert "-2,000.00" in texts


def test_the_footer_and_the_toolbar_are_rebuilt_not_stacked_on_refresh(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(
            _base(), _labelled_plan(), market=_open_market(),
            on_refresh=lambda f: (_labelled_plan(), _open_market()),
            on_submit=lambda p: None)
        wizard.open()
        wizard._refresh(True)
        root = nicegui_client.layout
        foots = _marked(root, wiz.MARKER_TABLE_FOOT)
        selects = _buttons_marked(root, wiz.MARKER_SELECT_ALL)

    assert len(foots) == 1
    assert len(selects) == 1
    assert wizard.selected == {"AAPL", "BOTH", "MSFT"}


def test_the_footer_leaves_an_unpriced_row_out_of_its_totals_and_says_so(nicegui_client):
    """An unpriced holding has no value to add; summing it as 0.00 would report a
    smaller basis as a fact."""
    plan = _labelled_plan()
    plan.rows[0].price = None
    with nicegui_client:
        wiz, _wizard = _open(nicegui_client, plan=plan)
        foot = _marked(nicegui_client.layout, wiz.MARKER_TABLE_FOOT)[-1]
        texts = _texts(foot)

    assert any(t.endswith(' *') for t in texts)
    assert any('unpriced' in t for t in texts)


# ---------------------------------------------------------------------------
# the label row's zero-share badge
# ---------------------------------------------------------------------------

def test_count_zero_share_symbols_counts_an_unset_share_with_a_zero_one():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        count_zero_share_symbols,
    )

    members = ['AAPL', 'MSFT', 'NVDA', 'TSLA']
    # MSFT was typed to 0, NVDA has no stored share at all: both get nothing.
    weights = {'AAPL': 60.0, 'MSFT': 0.0, 'TSLA': 40.0}

    assert count_zero_share_symbols(members, weights) == 2
    assert count_zero_share_symbols(members, {s: 25.0 for s in members}) == 0
    assert count_zero_share_symbols([], weights) == 0
    assert count_zero_share_symbols(members, None) == 4


def test_count_zero_share_symbols_reads_a_hundredth_of_a_percent_as_a_share():
    """The boxes step by 0.01, so 0.01 is a share somebody typed -- not a zero."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        count_zero_share_symbols,
    )

    assert count_zero_share_symbols(['AAPL'], {'AAPL': 0.01}) == 0
    assert count_zero_share_symbols(['AAPL'], {'AAPL': 0.0001}) == 1
