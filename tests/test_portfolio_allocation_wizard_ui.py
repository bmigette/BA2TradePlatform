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
    LabelTarget,
    SymbolTarget,
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


# ---------------------------------------------------------------------------
# Task 71: steps 1-3 (rebalance) and the INVEST_LABEL mode.
# ---------------------------------------------------------------------------


def _labels():
    return [
        LabelTarget("Growth", 60.0, [SymbolTarget("AAPL", 50.0), SymbolTarget("MSFT", 50.0)]),
        LabelTarget("Income", 40.0, [SymbolTarget("KO", 100.0)]),
    ]


def _open_steps(client, wiz, labels=None, **kwargs):
    calls = []
    kwargs.setdefault('on_dry_run', lambda **kw: calls.append(kw))
    with client:
        steps = wiz.open_allocation_steps(_base(), labels if labels is not None else _labels(),
                                          **kwargs)
    return steps, calls


def _numbers(client, wiz, marker):
    return [d for d in client.layout.descendants()
            if marker in getattr(d, '_markers', [])]


def test_wizard_module_exposes_the_steps_entry_point():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    assert callable(wiz.open_allocation_steps)
    params = inspect.signature(wiz.open_allocation_steps).parameters
    assert list(params)[:2] == ["base", "labels"]
    assert "on_dry_run" in params
    assert "mode" in params
    assert "invest_amount" in params


def test_the_steps_dialog_draws_both_rebalance_steps(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _ = _open_steps(nicegui_client, wiz)
    texts = _rendered_texts(nicegui_client.layout)

    for expected in ("Growth", "Income", "Even split", "Continue to dry run", "Cancel"):
        assert expected in texts
    assert steps._continue_button.enabled is True


def test_the_steps_dialog_edits_a_copy_so_cancelling_changes_nothing(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    original = _labels()
    steps, _ = _open_steps(nicegui_client, wiz, labels=original)
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)[0].set_value(10.0)

    assert steps.labels[0].target_pct == pytest.approx(10.0)
    assert original[0].target_pct == pytest.approx(60.0)
    assert original[0].symbols[0].weight_pct == pytest.approx(50.0)


def test_each_label_box_edits_its_own_label(nicegui_client):
    """The classic NiceGUI closure bug: without a default-argument capture every
    on_change writes to the LAST label in the loop."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _ = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)[0].set_value(70.0)

    assert [lt.target_pct for lt in steps.labels] == [70.0, 40.0]


def test_each_symbol_box_edits_its_own_symbol(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _ = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_SYMBOL_PCT)[0].set_value(30.0)

    assert [st.weight_pct for st in steps.labels[0].symbols] == [30.0, 50.0]
    assert [st.weight_pct for st in steps.labels[1].symbols] == [100.0]


def test_a_label_total_that_is_not_100_blocks_continue(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)[0].set_value(70.0)
        errors = _rendered_texts(steps._errors_container)
        assert steps._continue_button.enabled is False
        steps._continue()

    assert any("must total 100%" in t for t in errors)
    assert calls == []


def test_a_symbol_weight_total_that_is_not_100_blocks_continue(nicegui_client):
    """Step 2's rule, not just step 1's."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_SYMBOL_PCT)[0].set_value(30.0)
        assert steps._continue_button.enabled is False
        steps._continue()

    assert calls == []


def test_even_split_rewrites_the_label_percentages_to_total_exactly_100(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    labels = [LabelTarget(name, 0.0, [SymbolTarget(name * 2, 100.0)])
              for name in ("A", "B", "C")]
    steps, _ = _open_steps(nicegui_client, wiz, labels=labels)
    with nicegui_client:
        steps._even_split()

    assert [lt.target_pct for lt in steps.labels] == [33.33, 33.33, 33.34]
    assert steps._continue_button.enabled is True


def test_continue_hands_the_edited_targets_to_the_dry_run(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        steps._continue()

    assert len(calls) == 1
    call = calls[0]
    assert call["mode"] == wiz.ALLOCATION_MODE_REBALANCE
    assert [lt.label for lt in call["labels"]] == ["Growth", "Income"]
    assert call["scope_label"] is None
    assert call["amount"] == 0.0
    assert call["allow_fractional"] is False


def test_fractional_is_off_by_default_and_unavailable_without_broker_support(nicegui_client):
    """Opt-in per run (decision 12), and only offerable when the broker splits
    shares at all."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _ = _open_steps(nicegui_client, wiz)
    assert steps.allow_fractional is False
    assert steps._fractional_switch.enabled is False   # _base() has no support

    base = _base()
    base.supports_fractional = True
    with nicegui_client:
        supported = wiz.open_allocation_steps(base, _labels(), on_dry_run=lambda **kw: None)
    assert supported.allow_fractional is False
    assert supported._fractional_switch.enabled is True


# -- INVEST_LABEL -----------------------------------------------------------


def test_invest_mode_draws_a_label_picker_and_an_amount(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _ = _open_steps(nicegui_client, wiz, mode=wiz.ALLOCATION_MODE_INVEST_LABEL,
                           invest_amount=250.0)
    texts = _rendered_texts(nicegui_client.layout)

    assert steps.scope_label == "Growth"
    assert steps.invest_amount == pytest.approx(250.0)
    assert "Invest into one label" in texts
    # Step 1's percentage editor belongs to REBALANCE only: the amount IS the
    # budget here, so a label's target_pct is meaningless.
    assert _numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT) == []


def test_invest_mode_does_not_apply_the_labels_total_100_rule(nicegui_client):
    """A single label at 40% is legitimate on this path."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, calls = _open_steps(nicegui_client, wiz,
                               labels=[LabelTarget("Income", 40.0,
                                                   [SymbolTarget("KO", 100.0)])],
                               mode=wiz.ALLOCATION_MODE_INVEST_LABEL,
                               invest_amount=250.0)
    with nicegui_client:
        assert steps._continue_button.enabled is True
        steps._continue()

    assert calls[0]["mode"] == wiz.ALLOCATION_MODE_INVEST_LABEL
    assert [lt.label for lt in calls[0]["labels"]] == ["Income"]
    assert calls[0]["scope_label"] == "Income"
    assert calls[0]["amount"] == pytest.approx(250.0)


def test_invest_mode_blocks_a_symbol_weight_set_that_does_not_total_100(nicegui_client):
    """compute_label_investment multiplies the weights straight through, so a
    150% set would spend 375 of a 250 budget with nothing stopping it."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    labels = [LabelTarget("Income", 40.0, [SymbolTarget("KO", 100.0),
                                           SymbolTarget("PEP", 50.0)])]
    steps, calls = _open_steps(nicegui_client, wiz, labels=labels,
                               mode=wiz.ALLOCATION_MODE_INVEST_LABEL,
                               invest_amount=250.0)
    with nicegui_client:
        errors = _rendered_texts(steps._errors_container)
        assert steps._continue_button.enabled is False
        steps._continue()

    assert any("150.00" in t for t in errors)
    assert calls == []


def test_invest_mode_blocks_a_zero_amount(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, calls = _open_steps(nicegui_client, wiz,
                               mode=wiz.ALLOCATION_MODE_INVEST_LABEL,
                               invest_amount=0.0)
    with nicegui_client:
        assert steps._continue_button.enabled is False
        steps._continue()

    assert calls == []


def test_invest_mode_explains_but_does_not_block_an_amount_above_buying_power(nicegui_client):
    """The engine scales the plan down and the dry-run shows the result, which is
    more useful than refusing to compute it."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, calls = _open_steps(nicegui_client, wiz,
                               mode=wiz.ALLOCATION_MODE_INVEST_LABEL,
                               invest_amount=99_000.0)
    with nicegui_client:
        errors = _rendered_texts(steps._errors_container)
        assert steps._continue_button.enabled is True
        steps._continue()

    assert any("exceeds available buying power" in t for t in errors)
    assert len(calls) == 1
    assert calls[0]["amount"] == pytest.approx(99_000.0)


# ---------------------------------------------------------------------------
# Task 74: the income panel. It NEVER polls -- it is refreshed by the page on
# load and by the Refresh button, and nothing else, so the page issues no
# background broker calls.
# ---------------------------------------------------------------------------


def _click(element):
    """Fire an element's click handler with no browser and no event loop."""
    fired = 0
    for listener in element._event_listeners.values():
        if listener.type == 'click':
            listener.handler(None)
            fired += 1
    assert fired, 'element has no click handler'


def _by_text(client, caption):
    return [d for d in client.layout.descendants()
            if getattr(d, 'text', None) == caption]


def _income_events():
    from datetime import date
    return [
        {"id": 2, "external_id": "div-1", "event_date": date(2026, 5, 10),
         "event_type": "DIVIDEND", "symbol": "AAPL", "amount": 42.5,
         "open_amount": 42.5},
        {"id": 1, "external_id": "csd-1", "event_date": date(2026, 5, 1),
         "event_type": "DEPOSIT", "symbol": None, "amount": 5_000.0,
         "open_amount": 1_500.0},
    ]


def test_the_income_panel_draws_every_event_with_what_is_left(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5,
                                on_sync=lambda: None, on_invest=lambda amount: None)
        texts = _rendered_texts(nicegui_client.layout)

    assert "DIVIDEND" in texts and "DEPOSIT" in texts
    assert "2026-05-10" in texts and "2026-05-01" in texts
    assert "42.50" in texts and "5,000.00" in texts
    # What is LEFT, not what arrived: the deposit is 5,000 with 1,500 open.
    assert "1,500.00" in texts
    assert any("1,542.50" in t for t in texts)


def test_the_income_panel_shows_a_dash_for_an_event_with_no_payer_symbol(nicegui_client):
    """A deposit has no symbol. Rendering a bare ``None`` in the column would be
    the string "None"."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5,
                                on_sync=lambda: None, on_invest=lambda a: None)
        texts = _rendered_texts(nicegui_client.layout)

    assert "AAPL" in texts
    assert "-" in texts
    assert "None" not in texts


def test_the_income_panel_says_so_when_there_is_no_income(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel([], 0.0, on_sync=lambda: None, on_invest=lambda a: None)
        texts = _rendered_texts(nicegui_client.layout)

    assert any("No deposits or dividends" in t for t in texts)


def test_the_income_panel_refresh_button_calls_the_sync(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    synced = []
    invested = []
    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5,
                                on_sync=lambda: synced.append(True),
                                on_invest=invested.append)
        _click(_by_text(nicegui_client, 'Refresh')[0])

    assert synced == [True]
    assert invested == []


def test_the_income_panel_invest_button_hands_over_the_unallocated_total(nicegui_client):
    """The Invest shortcut pre-fills an INVEST_LABEL run with the OPEN total, not
    with the sum of the amounts -- the consumed part is already spent."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    invested = []
    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5,
                                on_sync=lambda: None, on_invest=invested.append)
        _click(_by_text(nicegui_client, 'Invest')[0])

    assert invested == [pytest.approx(1_542.5)]


def test_the_income_panel_cannot_invest_when_there_is_nothing_unallocated(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel([], 0.0, on_sync=lambda: None,
                                on_invest=lambda a: pytest.fail("nothing to invest"))
        invest = _by_text(nicegui_client, 'Invest')[0]

    assert invest.enabled is False


def test_the_income_panel_never_polls(nicegui_client):
    """Decision: the ledger syncs on page load and on explicit Refresh only. A
    ``ui.timer`` here would put a broker call on a background schedule."""
    from nicegui.elements.timer import Timer

    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5,
                                on_sync=lambda: pytest.fail("synced without being asked"),
                                on_invest=lambda a: None)
        timers = [d for d in nicegui_client.layout.descendants() if isinstance(d, Timer)]

    assert timers == []


def test_the_income_panel_shows_no_consumption_percentage(nicegui_client):
    """``consumed_amount > amount`` is reachable -- a DIVNRA tax leg restates a
    dividend below what a run already spent of it -- so any naive
    consumed/amount fraction renders above 100%. The panel shows absolute
    figures instead."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    over_consumed = [{"id": 1, "external_id": "div-1", "event_date": __import__('datetime').date(2026, 5, 10),
                      "event_type": "DIVIDEND", "symbol": "KO", "amount": 85.0,
                      "open_amount": 0.0}]
    with nicegui_client:
        wiz.render_income_panel(over_consumed, 0.0, on_sync=lambda: None,
                                on_invest=lambda a: None)
        texts = _rendered_texts(nicegui_client.layout)
        bars = [d for d in nicegui_client.layout.descendants()
                if getattr(d, 'tag', '') == 'q-linear-progress']

    assert not any('%' in t for t in texts)
    assert bars == []


# ---------------------------------------------------------------------------
# Task 75: the per-row outcome table. Partial failure is NORMAL -- a failed row
# sits next to a filled one and nothing is rolled back -- so the table has to
# describe what happened at the broker, not what was intended.
# ---------------------------------------------------------------------------


def _outcomes():
    from ba2_trade_platform.core import portfolio_allocation_service as svc

    return [
        svc.RowOutcome(symbol="MSFT", action="close", status=svc.OUTCOME_SUBMITTED,
                       quantity=5.0, filled_quantity=5.0, transaction_ids=[7]),
        svc.RowOutcome(symbol="AAPL", action="new", status=svc.OUTCOME_SUBMITTED,
                       quantity=10.0, path="whole", order_ids=[101]),
        svc.RowOutcome(symbol="NVDA", action="new", status=svc.OUTCOME_PARTIAL,
                       quantity=4.0, filled_quantity=1.5, path="fractional",
                       order_ids=[102], message="partially filled: 1.5 of 4.0"),
        svc.RowOutcome(symbol="TSLA", action="new", status=svc.OUTCOME_FAILED,
                       quantity=2.0, path="whole", order_ids=[103],
                       message="insufficient buying power"),
        svc.RowOutcome(symbol="KO", action="new", status=svc.OUTCOME_WASHTRADE_LOCKED,
                       quantity=3.0, order_ids=[104],
                       message="wash-trade gate locked this symbol"),
        svc.RowOutcome(symbol="SCHD", action="skip", status=svc.OUTCOME_SKIPPED,
                       message="below the broker's $5 fractional minimum"),
    ]


@pytest.fixture
def notifications(monkeypatch):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    seen = []
    monkeypatch.setattr(wiz.ui, 'notify',
                        lambda message, **kwargs: seen.append((message, kwargs.get('type'))))
    return seen


def test_the_outcome_table_lists_every_row_with_its_own_status(nicegui_client, notifications):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_outcomes(_outcomes(), run_id=21)
        texts = _rendered_texts(nicegui_client.layout)

    for symbol in ("MSFT", "AAPL", "NVDA", "TSLA", "KO", "SCHD"):
        assert symbol in texts
    for status in ("submitted", "partially_filled", "failed", "washtrade_locked", "skipped"):
        assert status in texts
    assert any("21" in t for t in texts)


def test_the_outcome_table_shows_a_failed_row_next_to_a_submitted_one(nicegui_client,
                                                                     notifications):
    """Nothing is rolled back, so the table is not "the run worked" or "the run
    failed" -- it is per row, with the broker's own words on the failure."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_outcomes(_outcomes(), run_id=21)
        texts = _rendered_texts(nicegui_client.layout)

    assert "insufficient buying power" in texts
    assert "below the broker's $5 fractional minimum" in texts


def test_the_outcome_table_shows_what_filled_when_it_differs_from_what_was_sent(
        nicegui_client, notifications):
    """A 4-share order that filled 1.5 is not a 4-share result. Showing only the
    submitted quantity is the run reporting its intention as its outcome."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_outcomes(_outcomes(), run_id=21)
        filled = _marked_texts(nicegui_client.layout, wiz.MARKER_OUTCOME_FILLED)

    # Row order is outcome order: MSFT, AAPL, NVDA, TSLA, KO, SCHD.
    assert filled == ["5.0000", "-", "1.5000", "-", "-", "-"]


def test_the_outcome_table_says_unknown_rather_than_zero_for_an_unreported_fill(
        nicegui_client, notifications):
    """``filled_quantity is None`` means the broker said nothing, which is not the
    same as "nothing filled" -- an accepted market order before the open looks
    exactly like this."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from ba2_trade_platform.core import portfolio_allocation_service as svc

    with nicegui_client:
        wiz.render_outcomes([svc.RowOutcome(symbol="AAPL", action="new",
                                            status=svc.OUTCOME_SUBMITTED, quantity=10.0)])
        filled = _marked_texts(nicegui_client.layout, wiz.MARKER_OUTCOME_FILLED)

    assert filled == ["-"]


def test_the_outcome_colour_map_covers_every_outcome_the_service_can_produce():
    """The colours and the failure count are keyed on the service's own status
    strings. Duplicating them as literals here is how a renamed constant silently
    turns a run in which everything failed into a green "submitted"."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from ba2_trade_platform.core import portfolio_allocation_service as svc

    assert set(wiz.OUTCOME_COLOURS) == {
        svc.OUTCOME_SUBMITTED, svc.OUTCOME_PARTIAL, svc.OUTCOME_SKIPPED,
        svc.OUTCOME_FAILED, svc.OUTCOME_WASHTRADE_LOCKED,
    }


def test_the_outcome_table_warns_when_a_row_failed(nicegui_client, notifications):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_outcomes(_outcomes(), run_id=21)

    assert notifications[-1][1] == 'warning'
    assert '1 row(s) failed' in notifications[-1][0]


def test_the_outcome_table_confirms_a_run_in_which_nothing_failed(nicegui_client,
                                                                  notifications):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from ba2_trade_platform.core import portfolio_allocation_service as svc

    with nicegui_client:
        wiz.render_outcomes([svc.RowOutcome(symbol="AAPL", action="new",
                                            status=svc.OUTCOME_SUBMITTED, quantity=10.0)])

    assert notifications[-1][1] == 'positive'


def test_the_outcome_table_says_when_a_symbol_is_wash_trade_locked(nicegui_client,
                                                                   notifications):
    """Not a failure -- the order is PENDING and gets retried -- but silence would
    leave the user believing the symbol was traded."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from ba2_trade_platform.core import portfolio_allocation_service as svc

    with nicegui_client:
        wiz.render_outcomes([svc.RowOutcome(symbol="KO", action="new",
                                            status=svc.OUTCOME_WASHTRADE_LOCKED,
                                            quantity=3.0)])

    assert notifications[-1][1] == 'warning'
    assert 'wash-trade' in notifications[-1][0]
