"""Tests for the Portfolio Allocation NiceGUI wizard module.

Three layers, cheapest first:

1. IMPORT smoke -- the module imports cleanly (catching syntax errors, bad
   relative imports and names that drifted from the pure engine) and its entry
   points have the signature the allocation page calls them with.
2. CONSTRUCTOR -- the wizard deliberately does no drawing in ``__init__``, so
   which rows start ticked is reachable with no client at all. Worth pinning:
   getting it wrong pre-ticks an order the broker has already refused.
3. RENDERING -- a bare ``nicegui.Client`` gives every ``ui.*`` call a slot stack
   to build into, with no browser and no event loop. The drawing code therefore
   really runs, and a bad f-string or a wrong element keyword fails here instead
   of in front of the user. The plan called this layer eyeball-only; it is not.

No test here re-checks arithmetic -- that lives in
``packages/common/tests/test_portfolio_allocation_wizard.py``, which needs
neither NiceGUI nor the ~8s ``ba2_trade_platform.ui.pages`` import (that package
``__init__`` pulls every page module and through them the expert/LLM stack).
"""
import inspect

import pytest

from ba2_trade_platform.core.portfolio_allocation import (
    REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT,
    AllocationPlan,
    AllocationRow,
    BaseSnapshot,
    VALUATION_MODE_MARKET,
)
from ba2_trade_platform.core.types import OrderDirection


def test_wizard_module_imports_and_exposes_its_entry_points():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    assert hasattr(wiz, "AllocationWizard")
    assert callable(wiz.open_allocation_wizard)
    assert callable(wiz.render_income_panel)
    assert callable(wiz.render_outcomes)


def test_open_allocation_wizard_accepts_the_page_call_signature():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    params = inspect.signature(wiz.open_allocation_wizard).parameters
    assert list(params)[:2] == ["base", "plan"]
    assert "on_refresh" in params
    assert "on_submit" in params


def _base():
    return BaseSnapshot(available_buying_power=10_000.0, managed_value=0.0,
                        base_notional=10_000.0, default_bp_factor=1.0,
                        valuation_mode=VALUATION_MODE_MARKET, cash=10_000.0)


def _plan_with_a_suppressed_row():
    """One sendable buy, one order the broker's $5 fractional floor killed."""
    return AllocationPlan(
        rows=[
            AllocationRow(symbol="AAPL", price=160.0, delta_quantity=10.0,
                          side=OrderDirection.BUY, estimated_value=1600.0,
                          bp_cost=1600.0, bp_factor=1.0),
            AllocationRow(
                symbol="PENNY", price=3.0, delta_quantity=0.0, side=None,
                fractional=True,
                reasons=[REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.format(
                    value=1.95, minimum=5.0)]),
        ],
        base_notional=10_000.0, available_buying_power=10_000.0,
        required_buying_power=1600.0, bp_usage_pct=16.0, total_buy_value=1600.0)


def test_wizard_does_not_pre_tick_an_order_the_broker_will_refuse():
    """A suppressed row has ``skipped is False`` -- it was never skipped, its
    order was killed after the fact. Ticking it by default would put a row the
    broker has already refused into what Submit sends."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    wizard = wiz.AllocationWizard(_base(), _plan_with_a_suppressed_row(),
                                  on_refresh=lambda frac: pytest.fail("not called"),
                                  on_submit=lambda plan: pytest.fail("not called"))

    assert wizard.selected == {"AAPL"}


def test_wizard_carries_the_plans_fractional_setting_into_the_toggle():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    plan = _plan_with_a_suppressed_row()
    plan.allow_fractional = True
    wizard = wiz.AllocationWizard(_base(), plan, on_refresh=lambda f: plan,
                                  on_submit=lambda p: None)
    assert wizard.allow_fractional is True


# ---------------------------------------------------------------------------
# Rendering. NiceGUI needs a client context but not a browser: a bare Client
# gives every ui.* call a slot stack to build into, so the drawing code really
# runs and a bad f-string or a wrong element keyword fails here instead of in
# front of the user.
# ---------------------------------------------------------------------------


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page

    client = Client(nicegui_page('/test-allocation-wizard'), request=None)
    try:
        yield client
    finally:
        Client.instances.pop(client.id, None)


def _rendered_texts(element) -> list:
    """Every label/button caption drawn under ``element``, in document order."""
    return [d.text for d in element.descendants()
            if getattr(d, 'text', None)]


def _marked_texts(element, marker: str) -> list:
    """The captions of the elements carrying ``marker``, in document order."""
    return [d.text for d in element.descendants()
            if marker in getattr(d, '_markers', [])]


def _mixed_plan():
    """Every shape the Order column has to distinguish, in one plan."""
    return AllocationPlan(
        rows=[
            AllocationRow(symbol="AAPL", price=160.0, delta_quantity=10.0,
                          side=OrderDirection.BUY, estimated_value=1600.0,
                          bp_cost=1600.0, bp_factor=1.0),
            AllocationRow(symbol="FRAC", price=300.0, delta_quantity=1.66666,
                          side=OrderDirection.BUY, estimated_value=500.0,
                          bp_cost=500.0, bp_factor=1.0, fractional=True,
                          reasons=["fractional"]),
            # Sized on the fractional grid but landing on exactly 4 shares: the
            # broker receives an ordinary whole-share order.
            AllocationRow(symbol="ONGRID", price=100.0, delta_quantity=4.0,
                          side=OrderDirection.BUY, estimated_value=400.0,
                          bp_cost=400.0, bp_factor=1.0, fractional=True,
                          reasons=["fractional"]),
            AllocationRow(symbol="MSFT", price=400.0, delta_quantity=-5.0,
                          side=OrderDirection.SELL, estimated_value=2000.0),
            AllocationRow(
                symbol="PENNY", price=3.0, delta_quantity=0.0, side=None,
                fractional=True,
                reasons=[REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.format(
                    value=1.95, minimum=5.0)]),
        ],
        base_notional=15_000.0, available_buying_power=10_000.0,
        required_buying_power=2500.0, bp_usage_pct=25.0,
        total_buy_value=2500.0, total_sell_value=2000.0, allow_fractional=True)


def test_the_dry_run_dialog_draws(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(),
                                      on_refresh=lambda f: _mixed_plan(),
                                      on_submit=lambda p: None)
        wizard.open()
        texts = _rendered_texts(nicegui_client.layout)

    for symbol in ("AAPL", "FRAC", "ONGRID", "MSFT", "PENNY"):
        assert symbol in texts
    assert "Submit" in texts and "Cancel" in texts


def test_the_dry_run_says_per_symbol_whether_the_order_is_fractional(nicegui_client):
    """The user must be able to see, per row, what the broker will receive: a
    fractional order (market-only, and subject to the $5 notional floor) or an
    ordinary whole-share one."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.AllocationWizard(_base(), _mixed_plan(), on_refresh=lambda f: None,
                             on_submit=lambda p: None).open()
        texts = _rendered_texts(nicegui_client.layout)
        order_kinds = _marked_texts(nicegui_client.layout, wiz.MARKER_ORDER_KIND)

    # Row order is plan order: AAPL, FRAC, ONGRID, MSFT, PENNY. ONGRID is the
    # one that separates "sized on the fractional grid" from "IS a fractional
    # order" -- it was sized fractionally and landed on exactly 4 shares.
    assert order_kinds == ["whole shares", "fractional", "whole shares",
                           "whole shares", "no order"]
    assert wiz.FRACTIONAL_IS_MARKET_ONLY_NOTE in texts


def test_a_suppressed_row_cannot_be_ticked(nicegui_client):
    """Greying it out is not enough -- an enabled box is still clickable, and a
    ticked suppressed row would put a refused order into what Submit sends."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.AllocationWizard(_base(), _mixed_plan(), on_refresh=lambda f: None,
                             on_submit=lambda p: None).open()
        ticks = [d for d in nicegui_client.layout.descendants()
                 if wiz.MARKER_ROW_TICK in getattr(d, '_markers', [])]

    # Plan order: AAPL, FRAC, ONGRID, MSFT, PENNY -- PENNY is the suppressed one.
    assert [t.enabled for t in ticks] == [True, True, True, True, False]


def test_the_dry_run_shows_the_brokers_own_suppression_reason(nicegui_client):
    """Not 'rounds to zero' -- the actual rule, with the actual dollar figures."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.AllocationWizard(_base(), _mixed_plan(), on_refresh=lambda f: None,
                             on_submit=lambda p: None).open()
        texts = _rendered_texts(nicegui_client.layout)

    assert REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.format(
        value=1.95, minimum=5.0) in texts


def test_submitting_hands_on_only_the_ticked_rows(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    submitted = []
    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(),
                                      on_refresh=lambda f: None,
                                      on_submit=submitted.append)
        wizard.open()
        wizard._toggle("FRAC", False)
        wizard._toggle("ONGRID", False)
        wizard._submit()

    assert len(submitted) == 1
    assert [r.symbol for r in submitted[0].rows] == ["AAPL", "MSFT"]
    assert submitted[0].total_buy_value == pytest.approx(1600.0)


def test_submitting_nothing_does_not_call_on_submit(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(
            _base(), _mixed_plan(),
            on_refresh=lambda f: None,
            on_submit=lambda p: pytest.fail("submitted an empty plan"))
        wizard.open()
        for symbol in ("AAPL", "FRAC", "ONGRID", "MSFT"):
            wizard._toggle(symbol, False)
        wizard._submit()


def test_a_failing_refresh_keeps_the_previous_plan(nicegui_client):
    """The broker call behind Refresh can fail. Losing the plan the user was
    reviewing (or crashing the dialog) is worse than saying so."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    original = _mixed_plan()

    def _boom(_allow_fractional):
        raise RuntimeError("broker said no")

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), original, on_refresh=_boom,
                                      on_submit=lambda p: None)
        wizard.open()
        wizard._refresh(True)

    assert wizard.plan is original
    assert wizard.selected == {"AAPL", "FRAC", "ONGRID", "MSFT"}


def test_a_plan_with_no_cash_balance_says_so_instead_of_guessing(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    base = _base()
    base.cash = None
    with nicegui_client:
        wiz.AllocationWizard(base, _mixed_plan(), on_refresh=lambda f: None,
                             on_submit=lambda p: None).open()
        texts = _rendered_texts(nicegui_client.layout)

    assert any("Est. cash after: unknown" in t for t in texts)
    assert not any("Est. cash after: 0" in t for t in texts)
