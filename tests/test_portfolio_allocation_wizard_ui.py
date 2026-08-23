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


def _open_market():
    """An OPEN market gate. ``market`` is a REQUIRED keyword on the wizard -- a
    default would let a caller submit into a closed market by omission -- so every
    test states which world it is in, and these predate the gate: they are all
    about the table, not about the clock."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        MARKET_GATE_OPEN, MarketGateResult,
    )
    return MarketGateResult(allowed=True, reason_code=MARKET_GATE_OPEN, message="")


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
                                  market=_open_market(), on_refresh=lambda frac: pytest.fail("not called"),
                                  on_submit=lambda plan: pytest.fail("not called"))

    assert wizard.selected == {"AAPL"}


def test_wizard_carries_the_plans_fractional_setting_into_the_toggle():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    plan = _plan_with_a_suppressed_row()
    plan.allow_fractional = True
    wizard = wiz.AllocationWizard(_base(), plan, market=_open_market(), on_refresh=lambda f: plan,
                                  on_submit=lambda p: None)
    assert wizard.allow_fractional is True


# ---------------------------------------------------------------------------
# Rendering. NiceGUI needs a client context but not a browser: a bare Client
# gives every ui.* call a slot stack to build into, so the drawing code really
# runs and a bad f-string or a wrong element keyword fails here instead of in
# front of the user.
# ---------------------------------------------------------------------------


def _fresh_client():
    """A SECOND client, for a test that has to draw the same dialog twice.

    The fixture yields one client per test, and everything drawn into it stays
    there; a test comparing two renders of the same dialog would otherwise read
    both sets of markers out of one layout and could not tell them apart.
    """
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page

    return Client(nicegui_page('/test-allocation-wizard'), request=None)


def _drop_client(client):
    from nicegui.client import Client

    Client.instances.pop(client.id, None)


@pytest.fixture
def nicegui_client():
    client = _fresh_client()
    try:
        yield client
    finally:
        _drop_client(client)


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
                                      market=_open_market(), on_refresh=lambda f: _mixed_plan(),
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
        wiz.AllocationWizard(_base(), _mixed_plan(), market=_open_market(), on_refresh=lambda f: None,
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
        wiz.AllocationWizard(_base(), _mixed_plan(), market=_open_market(), on_refresh=lambda f: None,
                             on_submit=lambda p: None).open()
        ticks = [d for d in nicegui_client.layout.descendants()
                 if wiz.MARKER_ROW_TICK in getattr(d, '_markers', [])]

    # Plan order: AAPL, FRAC, ONGRID, MSFT, PENNY -- PENNY is the suppressed one.
    assert [t.enabled for t in ticks] == [True, True, True, True, False]


def test_the_dry_run_shows_the_brokers_own_suppression_reason(nicegui_client):
    """Not 'rounds to zero' -- the actual rule, with the actual dollar figures."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.AllocationWizard(_base(), _mixed_plan(), market=_open_market(), on_refresh=lambda f: None,
                             on_submit=lambda p: None).open()
        texts = _rendered_texts(nicegui_client.layout)

    assert REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.format(
        value=1.95, minimum=5.0) in texts


# ---------------------------------------------------------------------------
# W6: COST and VALUE per row, and the leverage flag.
#
# Everything here is located by MARKER, never by rendered text: the money figures
# repeat across columns and "not marginable" already appears verbatim in the
# free-text Reasons column of the very rows under test.
# ---------------------------------------------------------------------------

from ba2_trade_platform.core.portfolio_allocation import (  # noqa: E402
    MARGIN_SOURCE_ASSET, MARGIN_SOURCE_DEFAULT, MARGIN_SOURCE_POSITION,
)


def _leverage_plan():
    """One row of every shape the BP x column has to tell apart.

    The four rows are the four live cases, with the numbers the adapters really
    produce (see ``bp_leverage``): neutral, buying-power-penalised, no published
    rate, and a sell.
    """
    return AllocationPlan(
        rows=[
            # Ordinary marginable stock, Reg-T 2:1 -> 0.5 x 2 = 1.0. NEUTRAL, and
            # held: 10 shares, paid 1,200, now worth 1,600.
            AllocationRow(symbol="AAPL", price=160.0, current_quantity=10.0,
                          current_cost_basis=1_200.0, target_notional=3_200.0,
                          target_quantity=20.0, delta_quantity=10.0,
                          side=OrderDirection.BUY, estimated_value=1_600.0,
                          bp_cost=1_600.0, bp_factor=1.0,
                          initial_margin_rate=0.5,
                          margin_source=MARGIN_SOURCE_ASSET),
            # LAZR, verified live: initial margin 98.9% -> x1.978 of buying power.
            AllocationRow(symbol="LAZR", price=10.0, current_quantity=0.0,
                          current_cost_basis=0.0, target_notional=1_000.0,
                          target_quantity=100.0, delta_quantity=100.0,
                          side=OrderDirection.BUY, estimated_value=1_000.0,
                          bp_cost=1_978.0, bp_factor=1.978, marginable=False,
                          initial_margin_rate=0.989,
                          margin_source=MARGIN_SOURCE_POSITION),
            # A first-time TastyTrade buy. Unheld, so the adapter published no
            # per-symbol rate and fell back to the account multiplier.
            AllocationRow(symbol="NEWBIE", price=50.0, current_quantity=0.0,
                          current_cost_basis=0.0, target_notional=500.0,
                          target_quantity=10.0, delta_quantity=10.0,
                          side=OrderDirection.BUY, estimated_value=500.0,
                          bp_cost=1_000.0, bp_factor=2.0,
                          initial_margin_rate=None,
                          margin_source=MARGIN_SOURCE_DEFAULT),
            AllocationRow(symbol="MSFT", price=400.0, current_quantity=10.0,
                          current_cost_basis=3_000.0, target_notional=2_000.0,
                          target_quantity=5.0, delta_quantity=-5.0,
                          side=OrderDirection.SELL, estimated_value=2_000.0,
                          bp_cost=0.0, bp_factor=1.0, initial_margin_rate=0.5,
                          margin_source=MARGIN_SOURCE_ASSET),
        ],
        base_notional=20_000.0, available_buying_power=10_000.0,
        required_buying_power=4_578.0, bp_usage_pct=45.78,
        total_buy_value=3_100.0, total_sell_value=2_000.0,
        valuation_mode=VALUATION_MODE_MARKET)


def _open_leverage_wizard(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.AllocationWizard(_base(), _leverage_plan(), market=_open_market(),
                             on_refresh=lambda f: None, on_submit=lambda p: None).open()
    return wiz


def test_the_dry_run_shows_what_is_held_what_it_cost_and_what_it_is_worth(
        nicegui_client):
    """The basis the user is trading against. Without it the only holding figure in
    the table is the post-trade projection, so "am I topping up a winner or
    averaging down?" is unanswerable from the dry run."""
    wiz = _open_leverage_wizard(nicegui_client)

    assert _marked_texts(nicegui_client.layout, wiz.MARKER_ROW_HELD) == [
        "10", "0", "0", "10"]
    assert _marked_texts(nicegui_client.layout, wiz.MARKER_ROW_COST) == [
        "1,200.00", "0.00", "0.00", "3,000.00"]
    assert _marked_texts(nicegui_client.layout, wiz.MARKER_ROW_VALUE) == [
        "1,600.00", "0.00", "0.00", "4,000.00"]


def test_an_unpriced_holding_shows_a_dash_and_is_left_out_of_the_value_total(
        nicegui_client):
    """A holding with no quote is NOT worth 0.00. The cell says ``-`` and the
    footer total says how many it had to leave out, rather than quietly reporting a
    smaller basis as a fact. Cost survives: it is a recorded figure, not a quote."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    plan = _leverage_plan()
    plan.rows[0].price = None
    with nicegui_client:
        wiz.AllocationWizard(_base(), plan, market=_open_market(),
                             on_refresh=lambda f: None, on_submit=lambda p: None).open()
        values = _marked_texts(nicegui_client.layout, wiz.MARKER_ROW_VALUE)
        texts = _rendered_texts(nicegui_client.layout)

    assert values[0] == "-"
    # AAPL's 1,600 is gone from the total, and the footer says why rather than
    # silently reporting 4,000.
    assert "Held value: 4,000.00 (1 unpriced, excluded)" in texts
    assert _marked_texts(nicegui_client.layout, wiz.MARKER_ROW_COST)[0] == "1,200.00"


def test_the_dry_run_flags_a_buying_power_penalised_instrument(nicegui_client):
    """LAZR charges x1.98 of buying power per dollar of stock. An ordinary
    marginable name charges exactly x1.00, so 1.00 is NEUTRAL and must not be
    dressed up as a finding."""
    wiz = _open_leverage_wizard(nicegui_client)
    cells = _marked_texts(nicegui_client.layout, wiz.MARKER_LEVERAGE)

    # Plan order: AAPL, LAZR, NEWBIE, MSFT.
    assert cells[0] == "×1.00"
    assert cells[1] == "×1.98"


def test_an_unheld_tastytrade_buy_is_not_painted_as_leveraged(nicegui_client):
    """THE REGRESSION THIS FEATURE WOULD OTHERWISE SHIP. TastyTrade publishes no
    per-symbol margin requirement for a symbol the account does not already hold,
    so its adapter returns bp_factor = multiplier = 2.0 with no rate and
    ``source = MARGIN_SOURCE_DEFAULT``. A first-time buy is unheld BY DEFINITION,
    so an unguarded ``ratio > 1.0`` would flag every new position on that broker.

    The cell says ``?``, not ``×2.00``, and it is not coloured as a finding."""
    wiz = _open_leverage_wizard(nicegui_client)
    cells = [d for d in nicegui_client.layout.descendants()
             if wiz.MARKER_LEVERAGE in getattr(d, '_markers', [])]

    newbie = cells[2]
    assert newbie.text == wiz.LEVERAGE_UNKNOWN_MARK
    assert "×2" not in newbie.text
    assert "orange" not in " ".join(newbie._classes)
    assert "red" not in " ".join(newbie._classes)
    # And it says WHY, rather than leaving a bare question mark.
    tips = [d.text for d in newbie.descendants() if getattr(d, 'text', None)]
    assert any("no margin rate" in t for t in tips), tips


def test_a_sell_states_no_leverage_in_the_table(nicegui_client):
    """A sell FREES buying power; ``bp_cost`` is 0.0 for one by construction, so
    any ratio computed for it is an artefact of that zero."""
    wiz = _open_leverage_wizard(nicegui_client)
    assert _marked_texts(nicegui_client.layout, wiz.MARKER_LEVERAGE)[3] == "-"


def test_the_leverage_tooltip_separates_lending_from_buying_power(nicegui_client):
    """"The broker lends against this" is ``initial margin < 100%``, which is a
    DIFFERENT statement from ``x1.98 buying power`` and, on LAZR, the opposite
    one: at a 98.9% initial margin it is nearly cash-collateralised. A bare red
    x1.98 says the reverse, so the honest fact goes in the tooltip."""
    wiz = _open_leverage_wizard(nicegui_client)
    cells = [d for d in nicegui_client.layout.descendants()
             if wiz.MARKER_LEVERAGE in getattr(d, '_markers', [])]
    tips = [d.text for d in cells[1].descendants() if getattr(d, 'text', None)]

    assert any("98.9%" in t for t in tips), tips
    assert any(MARGIN_SOURCE_POSITION in t for t in tips), tips


def test_the_dry_run_names_the_valuation_mode_on_the_projected_column(
        nicegui_client):
    """``Projected`` silently means post-trade COST BASIS in cost mode and
    ``target quantity x price`` in market mode. An unlabelled column that changes
    meaning with a toggle elsewhere on the page is worse than no column."""
    wiz = _open_leverage_wizard(nicegui_client)
    texts = _rendered_texts(nicegui_client.layout)
    assert f"Projected ({VALUATION_MODE_MARKET})" in texts
    assert "Projected" not in texts


def test_the_dry_run_totals_add_up_the_cost_and_the_value_being_traded_against(
        nicegui_client):
    """Per-row cost and value with no total leaves the user adding a column of
    figures by eye to answer "what basis am I moving?"."""
    wiz = _open_leverage_wizard(nicegui_client)
    texts = _rendered_texts(nicegui_client.layout)

    # 1,200 + 0 + 0 + 3,000 held cost; 1,600 + 0 + 0 + 4,000 held value.
    assert "Held cost: 4,200.00" in texts
    assert "Held value: 5,600.00" in texts


def test_the_dry_run_totals_say_that_buying_power_is_charged_not_bought(
        nicegui_client):
    """Requirement 1b, which is a LABELLING defect and not an arithmetic one. The
    engine is already right -- ``bp_factor`` provably moves no target and no
    quantity -- but a bare ``BP cost 4,578`` beside a ``Buy value 3,100`` reads as
    if leverage had inflated the plan. The totals must name the two apart."""
    wiz = _open_leverage_wizard(nicegui_client)
    note = _marked_texts(nicegui_client.layout, wiz.MARKER_BP_NOTE)

    assert len(note) == 1
    assert "3,100.00" in note[0] and "4,578.00" in note[0]
    assert "CHARGED" in note[0]
    # And it says the thing the user actually got wrong: the ratio buys nothing.
    assert "buys no extra share" in note[0]


def test_the_buying_power_note_is_absent_when_the_plan_buys_nothing(
        nicegui_client):
    """A sell-only plan charges no buying power at all, and a sentence explaining a
    charge that is not being made is noise."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    plan = _leverage_plan()
    plan.rows = [r for r in plan.rows if r.symbol == "MSFT"]
    with nicegui_client:
        wiz.AllocationWizard(_base(), plan, market=_open_market(),
                             on_refresh=lambda f: None, on_submit=lambda p: None).open()
        note = _marked_texts(nicegui_client.layout, wiz.MARKER_BP_NOTE)

    assert note == []


def test_the_dry_run_totals_show_the_prechecks_fee_estimate_when_there_is_one(
        nicegui_client):
    """Fees are literally cost, and the precheck's own figure was being captured
    onto the row and then dropped at the display boundary."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    plan = _leverage_plan()
    plan.rows[0].estimated_fees = 1.37
    plan.rows[1].estimated_fees = 0.63
    with nicegui_client:
        wiz.AllocationWizard(_base(), plan, market=_open_market(),
                             on_refresh=lambda f: None, on_submit=lambda p: None).open()
        texts = _rendered_texts(nicegui_client.layout)
    assert "Est. fees: 2.00" in texts


def test_an_unprechecked_plan_shows_no_fee_figure_at_all(nicegui_client):
    """``estimated_fees is None`` means "not prechecked", NEVER "free". A 0.00 here
    would be a broker figure nobody published."""
    wiz = _open_leverage_wizard(nicegui_client)
    texts = _rendered_texts(nicegui_client.layout)
    assert not any(t.startswith("Est. fees") for t in texts)


def test_the_leverage_cell_covers_every_verdict_the_engine_can_return():
    """A verdict the cell does not know about would render as a blank column
    rather than fail, so the mapping is pinned against the engine's own list. No
    branch may return an empty text or an empty tooltip: a bare ``?`` or ``-`` in a
    money column is a question the screen cannot answer."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from ba2_trade_platform.core.portfolio_allocation import LEVERAGE_VERDICTS

    for verdict in LEVERAGE_VERDICTS:
        text, _css, tooltip = wiz._leverage_cell(
            {"symbol": "X", "leverage": verdict, "bp_ratio": 1.5,
             "initial_margin_rate": 0.75, "margin_source": MARGIN_SOURCE_ASSET})
        assert text, verdict
        assert tooltip, verdict


def _capture_errors(monkeypatch):
    """Collect ``logger.error`` messages from the wizard module. NOT caplog."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    messages = []
    monkeypatch.setattr(wiz.logger, 'error',
                        lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def test_a_broker_that_publishes_no_rate_is_a_normal_state_not_a_drift_error(
        monkeypatch):
    """LEVERAGE_UNKNOWN has its OWN branch, and it is not the last-resort
    "the engine grew a verdict this table has never heard of" path -- which logs.

    The two happen to render identically, so without this the guard could be
    deleted and the fallback would silently cover for it. They are different
    facts: one is the everyday case of a broker that says nothing about a symbol
    you do not hold, the other is a code defect, and logging the first would put an
    error in the log on every first-time buy."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from ba2_trade_platform.core.portfolio_allocation import LEVERAGE_UNKNOWN

    errors = _capture_errors(monkeypatch)
    text, _css, tooltip = wiz._leverage_cell(
        {"symbol": "NEWBIE", "leverage": LEVERAGE_UNKNOWN, "bp_ratio": 2.0,
         "initial_margin_rate": None, "margin_source": MARGIN_SOURCE_DEFAULT})

    assert text == wiz.LEVERAGE_UNKNOWN_MARK
    assert "no margin rate" in tooltip
    assert errors == []

    # ... whereas a verdict the table really does not know about does log.
    wiz._leverage_cell({"symbol": "X", "leverage": "invented", "bp_ratio": 1.0,
                        "initial_margin_rate": 0.5,
                        "margin_source": MARGIN_SOURCE_ASSET})
    assert len(errors) == 1
    assert "invented" in errors[0]


def test_submitting_hands_on_only_the_ticked_rows(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    submitted = []
    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(),
                                      market=_open_market(), on_refresh=lambda f: None,
                                      on_submit=submitted.append)
        wizard.open()
        wizard._toggle("FRAC", False)
        wizard._toggle("ONGRID", False)
        wizard._submit()

    assert len(submitted) == 1
    assert [r.symbol for r in submitted[0].rows] == ["AAPL", "MSFT"]
    assert submitted[0].total_buy_value == pytest.approx(1600.0)


def test_a_second_click_on_submit_does_not_run_the_allocation_twice(nicegui_client):
    """I5. NiceGUI calls a SYNC handler directly on the event loop
    (nicegui/events.py:444-448), so nothing -- not even ``dialog.close()`` --
    reaches the browser until ``on_submit`` returns, and ``on_submit`` blocks for
    as long as the broker takes. The Submit button therefore stays visible and
    clickable for the whole run, and a second click submits the WHOLE allocation
    again: every buy placed twice.

    The latch is deliberately one-shot and is never released. Queued clicks are
    dispatched sequentially, so a flag cleared in a ``finally`` would be back to
    False by the time the second one ran -- and there is nothing to re-submit
    anyway once the plan has gone."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    submitted = []
    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(),
                                      market=_open_market(), on_refresh=lambda f: None,
                                      on_submit=submitted.append)
        wizard.open()
        wizard._submit()
        wizard._submit()
        wizard._submit()

    assert len(submitted) == 1


def test_a_submit_that_raises_still_blocks_a_second_run(nicegui_client):
    """The broker call behind Submit can fail half way through -- with orders
    already placed. Re-running the whole plan on top of that is the worst
    possible response, so the latch is set BEFORE ``on_submit`` is called."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    calls = []

    def _boom(plan):
        calls.append(plan)
        raise RuntimeError("broker connection reset mid-run")

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(),
                                      market=_open_market(), on_refresh=lambda f: None, on_submit=_boom)
        wizard.open()
        with pytest.raises(RuntimeError):
            wizard._submit()
        wizard._submit()

    assert len(calls) == 1


def test_submitting_nothing_leaves_the_wizard_usable(nicegui_client):
    """An empty submit is not a submission: it must not latch the wizard shut,
    or ticking a row and pressing Submit again would do nothing forever."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    submitted = []
    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(),
                                      market=_open_market(), on_refresh=lambda f: None,
                                      on_submit=submitted.append)
        wizard.open()
        for symbol in ("AAPL", "FRAC", "ONGRID", "MSFT"):
            wizard._toggle(symbol, False)
        wizard._submit()
        wizard._toggle("AAPL", True)
        wizard._submit()

    assert len(submitted) == 1
    assert [r.symbol for r in submitted[0].rows] == ["AAPL"]


def test_submitting_nothing_does_not_call_on_submit(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(
            _base(), _mixed_plan(),
            market=_open_market(), on_refresh=lambda f: None,
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
        wizard = wiz.AllocationWizard(_base(), original, market=_open_market(), on_refresh=_boom,
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
        wiz.AllocationWizard(base, _mixed_plan(), market=_open_market(), on_refresh=lambda f: None,
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
    # These tests predate the remembered choice and were written against a switch
    # that always opened OFF; passing False keeps them meaning what they meant.
    kwargs.setdefault('allow_fractional', False)
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


def test_a_label_total_ABOVE_100_blocks_continue(nicegui_client):
    """The only hard rule left on the label totals. ``_labels()`` is 60/40, so
    pushing the first to 70 makes 110."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)[0].set_value(70.0)
        errors = _rendered_texts(steps._errors_container)
        assert steps._continue_button.enabled is False
        steps._continue()

    assert any("over 100% by 10.00%" in t for t in errors), errors
    assert calls == []


def test_a_label_total_BELOW_100_blocks_continue_again(nicegui_client):
    """THE REVERSAL. Under-allocating was briefly the way to hold cash; the reserve
    box is now that way, so a shortfall is back to being a mistake -- and it blocks
    LIVE, on the keystroke, without a dry run."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        # 60/40 -> 30/40, so the set totals 70.
        _numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)[0].set_value(30.0)
        messages = _rendered_texts(steps._errors_container)
        assert steps._continue_button.enabled is False
        steps._continue()

    assert any("under 100% by 30.00%" in t for t in messages), messages
    assert calls == []


def test_the_shortfall_message_points_at_the_box_that_does_want_a_number(
        nicegui_client):
    """Being told off without being told what to do is how a user re-types the same
    thing. The error names the Unallocated box."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)[0].set_value(30.0)
        messages = _rendered_texts(steps._errors_container)

    assert any("Use the Unallocated box to hold money back." in t for t in messages), messages


def test_both_sides_of_the_label_total_rule_are_drawn_as_errors(nicegui_client):
    """``_revalidate`` prefixes a blocking message with the red cross. Neither half
    may come back as orange advice -- that is what made the shortfall ambiguous."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz)
    for value in (30.0, 90.0):        # 70 total, then 130 total
        with nicegui_client:
            _numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)[0].set_value(value)
            messages = _rendered_texts(steps._errors_container)
        assert any(t.startswith("\u2716 ") for t in messages), (value, messages)
        assert not any(t.startswith("\u26a0 ") for t in messages), (value, messages)


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


def test_fractional_follows_the_caller_and_is_unavailable_without_broker_support(nicegui_client):
    """The switch opens on the choice the CALLER passes (the account's remembered
    one), and is only offerable when the broker splits shares at all."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _ = _open_steps(nicegui_client, wiz)
    assert steps.allow_fractional is False
    assert steps._fractional_switch.enabled is False   # _base() has no support

    base = _base()
    base.supports_fractional = True
    with nicegui_client:
        supported = wiz.open_allocation_steps(base, _labels(), on_dry_run=lambda **kw: None,
                                              allow_fractional=False)
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
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
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
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        texts = _rendered_texts(nicegui_client.layout)

    assert "AAPL" in texts
    assert "-" in texts
    assert "None" not in texts


def test_the_income_panel_says_so_when_there_is_no_income(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel([], 0.0, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        texts = _rendered_texts(nicegui_client.layout)

    assert any("No deposits or dividends" in t for t in texts)


def test_the_income_panel_refresh_button_calls_the_sync(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    synced = []
    invested = []
    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5,
                                on_sync=lambda: synced.append(True),
                                on_invest=invested.append, working_note=None)
        _click(_by_text(nicegui_client, 'Refresh')[0])

    assert synced == [True]
    assert invested == []


def test_the_income_panel_invest_button_hands_over_the_unallocated_total(nicegui_client):
    """The Invest shortcut pre-fills an INVEST_LABEL run with the OPEN total, not
    with the sum of the amounts -- the consumed part is already spent."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    invested = []
    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=invested.append)
        _click(_by_text(nicegui_client, 'Invest')[0])

    assert invested == [pytest.approx(1_542.5)]


def test_the_income_panel_cannot_invest_when_there_is_nothing_unallocated(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel([], 0.0, on_sync=lambda: None,
                                on_invest=lambda a: pytest.fail("nothing to invest"),
                                working_note=None)
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
                                on_invest=lambda a: None, working_note=None)
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
                                on_invest=lambda a: None, working_note=None)
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


def test_open_allocation_steps_requires_an_explicit_fractional_default():
    """The hardcoded ``self.allow_fractional = False`` is gone; the caller passes
    the account's remembered choice, which now defaults ON."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    params = inspect.signature(wiz.open_allocation_steps).parameters
    assert "allow_fractional" in params
    assert params["allow_fractional"].default is inspect.Parameter.empty

    init_params = inspect.signature(wiz.AllocationSteps.__init__).parameters
    assert "allow_fractional" in init_params
    assert init_params["allow_fractional"].default is inspect.Parameter.empty


def test_the_steps_dialog_opens_on_the_remembered_fractional_choice(nicegui_client):
    """ON when the account remembers ON and the broker can split shares."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    base = _base()
    base.supports_fractional = True
    with nicegui_client:
        steps = wiz.open_allocation_steps(base, _labels(), on_dry_run=lambda **kw: None,
                                          allow_fractional=True)
    assert steps.allow_fractional is True
    assert steps._fractional_switch.value is True


def test_a_broker_that_cannot_split_shares_still_vetoes_the_remembered_choice(nicegui_client):
    """``supports_fractional`` wins: offering a grid the broker does not have would
    plan orders it cannot accept."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    base = _base()
    base.supports_fractional = False
    with nicegui_client:
        steps = wiz.open_allocation_steps(base, _labels(), on_dry_run=lambda **kw: None,
                                          allow_fractional=True)
    assert steps.allow_fractional is False
    assert steps._fractional_switch.enabled is False


# ---------------------------------------------------------------------------
# Task 95: Submit gated on market hours; bumps, rounding and residuals prominent
# ---------------------------------------------------------------------------

def _closed_market():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        MARKET_GATE_CLOSED, MarketGateResult,
    )
    return MarketGateResult(
        allowed=False, reason_code=MARKET_GATE_CLOSED, severity="warning",
        message="Market closed — Submit is disabled. The next regular session "
                "opens Fri 21 Aug 2026 09:30 ET (15h 30m from now).",
        next_open_text="Fri 21 Aug 2026 09:30 ET", countdown_text="15h 30m")


def _bumped_plan():
    """A bumped row, a redistributed row and a row that gets no order at all."""
    import ba2_trade_platform.core.portfolio_allocation as pa
    return AllocationPlan(
        rows=[
            AllocationRow(symbol="AAPL", price=160.0, target_notional=1_600.0,
                          target_quantity=10.0, delta_quantity=10.0,
                          side=OrderDirection.BUY, estimated_value=1_600.0,
                          bp_cost=1_600.0, bp_factor=1.0, fractional=True,
                          redistributed=True, reasons=["weight adjusted"]),
            AllocationRow(symbol="BUMPY", price=300.0, target_notional=200.0,
                          target_quantity=1.0, delta_quantity=1.0,
                          side=OrderDirection.BUY, estimated_value=300.0,
                          bp_cost=300.0, bp_factor=1.0,
                          sizing_outcome=pa.SIZING_OUTCOME_BUMPED,
                          reasons=["BUMPED UP to 1 share(s), 150% of target"]),
            AllocationRow(symbol="BRKA", price=650_000.0, target_notional=260_000.0,
                          delta_quantity=0.0, side=None, unmet_notional=260_000.0,
                          sizing_outcome=pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE,
                          reasons=["no order; over the 150% bump limit"]),
        ],
        base_notional=262_800.0, available_buying_power=300_000.0,
        allow_fractional=True, valuation_mode=VALUATION_MODE_MARKET)


def test_wizard_requires_the_market_gate_and_will_not_default_it():
    """A default would let a caller submit into a closed market by omission."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    params = inspect.signature(wiz.open_allocation_wizard).parameters
    assert "market" in params
    assert params["market"].default is inspect.Parameter.empty

    init_params = inspect.signature(wiz.AllocationWizard.__init__).parameters
    assert "market" in init_params
    assert init_params["market"].default is inspect.Parameter.empty


def test_wizard_module_exposes_the_notice_renderers():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    assert hasattr(wiz.AllocationWizard, "_render_market_banner")
    assert hasattr(wiz.AllocationWizard, "_render_notices")
    assert hasattr(wiz.AllocationWizard, "_render_no_order_rows")


def test_wizard_submit_bails_on_the_gate_before_it_touches_anything_else(nicegui_client):
    """The gate check is the FIRST thing ``_submit`` does.

    Proven on an instance built with ``object.__new__``: it has no ``_submitted``
    latch, no ``plan`` and no ``dialog``, so reading, filtering or closing any of
    them ahead of the gate raises ``AttributeError`` instead of quietly returning.
    ``base`` IS supplied, because ``_base_block`` reads it and is deliberately
    checked even earlier -- see the mirror test below.

    A client context is entered because ``ui.notify`` needs a slot -- which it
    always has in production, where this only ever runs from a click handler. The
    plan's version omitted it and passed only when an earlier test happened to
    leave a slot on the stack.
    """
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    submitted = []
    obj = object.__new__(wiz.AllocationWizard)
    obj.base = _base()
    obj.market = _closed_market()
    obj.on_submit = submitted.append

    with nicegui_client:
        obj._submit()

    assert submitted == []
    assert not hasattr(obj, "_submitted")     # the latch was never even reached


def test_wizard_submit_bails_on_an_unpriced_holding_before_anything_else_too(
        nicegui_client):
    """The mirror of the test above for the OTHER refusal, and it is checked FIRST:
    a held symbol with no quote is something the user can act on now, whereas a
    closed market is something they can only wait out.

    Same ``object.__new__`` instance with no latch, no plan and no dialog, and the
    market gate deliberately OPEN so nothing but the base block can be doing the
    refusing.
    """
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    submitted = []
    obj = object.__new__(wiz.AllocationWizard)
    obj.base = _base_with_an_unpriced_holding()
    obj.market = _open_market()
    obj.on_submit = submitted.append

    with nicegui_client:
        obj._submit()

    assert submitted == []
    assert not hasattr(obj, "_submitted")


def test_wizard_imports_the_engine_summary_helpers():
    """The prominence work is engine-side and pure; the wizard only draws it."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    assert callable(wiz.fractional_summary)
    assert callable(wiz.whole_share_notice)
    assert callable(wiz.no_order_notice)
    assert callable(wiz.bump_notice)
    assert callable(wiz.redistribution_notice)
    assert callable(wiz.no_order_rows)


def test_render_income_panel_requires_the_working_orders_note():
    """D3: deferral is the common case, so the panel has to say so -- and a DEFAULT
    would let the page glue drop the one fact D3 exists to surface, showing an
    "unallocated" figure with no explanation of why it did not go down. That is
    exactly what the optional version allowed: the page never passed it.

    The DECISION is pure (``unconsumed_income_notice`` in the engine, or
    ``working_orders_notice`` in the view module for the post-submit line); this
    module only draws whichever one it is handed."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    params = inspect.signature(wiz.render_income_panel).parameters
    assert "working_note" in params
    assert params["working_note"].default is inspect.Parameter.empty
    assert params["working_note"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_income_panel_draws_the_engines_unconsumed_income_notice_too():
    """The panel takes ``(text, severity)``, so it draws the ENGINE's deferred-income
    sentence and the view's post-submit one through the same slot."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    assert callable(wiz.unconsumed_income_notice)


def test_the_dry_run_disables_submit_and_shows_why_when_the_market_is_shut(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(), market=_closed_market(),
                                      on_refresh=lambda f: _mixed_plan(),
                                      on_submit=lambda p: None)
        wizard.open()
        texts = _rendered_texts(nicegui_client.layout)

    assert wizard._submit_button.enabled is False
    assert any("Fri 21 Aug 2026 09:30 ET" in t for t in texts)
    # Everything else stays live: planning outside market hours is the normal case.
    assert "Refresh" in texts and "Cancel" in texts


def test_the_dry_run_draws_no_market_banner_at_all_when_the_market_is_open(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(), market=_open_market(),
                                      on_refresh=lambda f: _mixed_plan(),
                                      on_submit=lambda p: None)
        wizard.open()
        texts = _rendered_texts(nicegui_client.layout)

    assert wizard._submit_button.enabled is True
    assert not any("Submit is disabled" in t for t in texts)


def test_the_dry_run_shows_the_bump_the_rounding_and_the_weight_move(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _bumped_plan(), market=_open_market(),
                                      on_refresh=lambda f: _bumped_plan(),
                                      on_submit=lambda p: None)
        wizard.open()
        texts = _rendered_texts(nicegui_client.layout)

    joined = " | ".join(texts)
    assert "BUMPED UP" in joined                 # the notice, up top
    assert "bumped-to-1" in texts                # the Outcome cell
    assert "share count adjusted" in joined      # the redistribution notice
    assert "NO order at all" in joined           # the no-order notice
    assert any("→" in t for t in texts)          # asked -> actual weight


def test_the_dry_run_lists_a_symbol_that_gets_no_order_with_its_unallocated_money(nicegui_client):
    """A refused target used to vanish from the review screen entirely."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _bumped_plan(), market=_open_market(),
                                      on_refresh=lambda f: _bumped_plan(),
                                      on_submit=lambda p: None)
        wizard.open()
        texts = _rendered_texts(nicegui_client.layout)

    assert "BRKA" in texts
    assert any("Not traded (1)" in t and "260,000.00" in t for t in texts)


def test_submitting_into_a_closed_market_hands_nothing_on(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    submitted = []
    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(), market=_closed_market(),
                                      on_refresh=lambda f: _mixed_plan(),
                                      on_submit=submitted.append)
        wizard.open()
        wizard._submit()

    assert submitted == []
    assert wizard._submitted is False      # nothing was sent, so nothing is latched


def test_the_market_banner_is_drawn_only_when_the_gate_blocks(nicegui_client):
    """Located by MARKER, not by text: the gate's message is also the Submit
    button's tooltip, so a text search finds it whether or not the banner exists."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.AllocationWizard(_base(), _mixed_plan(), market=_closed_market(),
                             on_refresh=lambda f: None, on_submit=lambda p: None).open()
        shut = _marked_texts(nicegui_client.layout, wiz.MARKER_MARKET_BANNER)
    assert len(shut) == 1
    assert "Fri 21 Aug 2026 09:30 ET" in shut[0]

    with nicegui_client:
        wiz.AllocationWizard(_base(), _mixed_plan(), market=_open_market(),
                             on_refresh=lambda f: None, on_submit=lambda p: None).open()
    # An open market draws NO banner at all -- not an empty one, which would leave
    # a stray box on a screen that has nothing to say.
    banners = [d for d in nicegui_client.layout.descendants()
               if wiz.MARKER_MARKET_BANNER in getattr(d, '_markers', [])]
    assert len(banners) == 1, "the open-market render added a second banner"


def test_all_four_plan_notices_reach_the_screen(nicegui_client):
    """Each is located by marker and matched on wording unique to the NOTICE: every
    one of them quotes a phrase that also appears in some row's reasons cell, so a
    plain text search cannot tell a missing notice from a present one."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    plan = _bumped_plan()
    plan.rows[1].fractional = False          # so the whole-share notice fires too
    with nicegui_client:
        wiz.AllocationWizard(_base(), plan, market=_open_market(),
                             on_refresh=lambda f: None, on_submit=lambda p: None).open()
        notices = _marked_texts(nicegui_client.layout, wiz.MARKER_PLAN_NOTICE)

    joined = " | ".join(notices)
    assert len(notices) == 4, notices
    assert "cannot trade fractionally" in joined     # whole_share_notice
    assert "over-allocates them by" in joined        # bump_notice
    assert "get NO order at all" in joined           # no_order_notice
    assert "share count adjusted" in joined          # redistribution_notice


def test_the_income_panel_draws_the_working_orders_line_it_is_given(nicegui_client):
    """D3: the run's income is deliberately unconsumed while orders are working,
    and with a quarter of the book on whole shares that is the common outcome."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import working_orders_notice

    note = working_orders_notice(settled=False, working_order_ids=[7, 9])
    with nicegui_client:
        wiz.render_income_panel([], 0.0, on_sync=lambda: None, on_invest=lambda a: None,
                                working_note=note)
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_WORKING_ORDERS)
    assert len(drawn) == 1
    assert "2 order(s) still working" in drawn[0]


def test_the_income_panel_draws_no_working_orders_line_when_the_run_settled(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel([], 0.0, on_sync=lambda: None, on_invest=lambda a: None,
                                working_note=None)
        assert _marked_texts(nicegui_client.layout, wiz.MARKER_WORKING_ORDERS) == []


# ---------------------------------------------------------------------------
# I3 / I5: Refresh has to re-read the CLOCK, not just the plan.
#
# ``_solve_plan``'s docstring says "ONE read feeds both the banner and the gate",
# and the page's ``_on_refresh`` threw the market hours away into ``_`` while
# ``AllocationWizard._refresh`` never touched ``self.market``, the banner or the
# Submit button. So a wizard opened before the bell kept a disabled Submit all
# morning, and -- the dangerous direction -- one opened while OPEN kept an ENABLED
# Submit right through the close, sending orders the server gate then refuses with
# a screen of unexplained per-row failures.
# ---------------------------------------------------------------------------

def _fallback_open_market():
    """OPEN, but on the OFFLINE calendar's word: the broker's clock never answered."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        MARKET_GATE_OPEN, MarketGateResult,
    )
    return MarketGateResult(allowed=True, reason_code=MARKET_GATE_OPEN, message="",
                            from_fallback=True)


def test_a_refresh_that_returns_only_a_plan_is_refused_not_half_applied(nicegui_client):
    """The contract is ``(plan, market)``: one call, two answers, one solve.

    A caller still on the old plan-only contract must fail LOUDLY. Unpacking is
    what makes that automatic -- the TypeError lands in the same handler a broker
    outage does, so the dialog keeps the plan and the gate it already had rather
    than adopting half of a new one.
    """
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    original = _mixed_plan()
    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), original, market=_closed_market(),
                                      on_refresh=lambda f: _mixed_plan(),
                                      on_submit=lambda p: None)
        wizard.open()
        wizard._refresh(wizard.allow_fractional)

    assert wizard.plan is original
    assert wizard.market.allowed is False
    assert wizard._submit_button.enabled is False


def test_refreshing_after_the_bell_re_enables_submit_and_drops_the_banner(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(
            _base(), _mixed_plan(), market=_closed_market(),
            on_refresh=lambda f: (_mixed_plan(), _open_market()),
            on_submit=lambda p: None)
        wizard.open()
        assert wizard._submit_button.enabled is False
        assert len(_marked_texts(nicegui_client.layout, wiz.MARKER_MARKET_BANNER)) == 1

        wizard._refresh(wizard.allow_fractional)

        assert wizard.market.allowed is True
        assert wizard._submit_button.enabled is True
        assert _marked_texts(nicegui_client.layout, wiz.MARKER_MARKET_BANNER) == []


def test_refreshing_after_the_close_disables_submit_and_raises_the_banner(nicegui_client):
    """The direction that costs money: the dialog can sit open across 16:00."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(
            _base(), _mixed_plan(), market=_open_market(),
            on_refresh=lambda f: (_mixed_plan(), _closed_market()),
            on_submit=lambda p: None)
        wizard.open()
        assert wizard._submit_button.enabled is True

        wizard._refresh(wizard.allow_fractional)

        assert wizard.market.allowed is False
        assert wizard._submit_button.enabled is False
        banner = _marked_texts(nicegui_client.layout, wiz.MARKER_MARKET_BANNER)
        assert len(banner) == 1
        assert "Fri 21 Aug 2026 09:30 ET" in banner[0]


def test_a_refresh_that_closed_the_market_also_refuses_the_next_submit(nicegui_client):
    """The banner and the button are display; ``_submit``'s own check is the one
    that has to be reading the SAME gate."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    submitted = []
    with nicegui_client:
        wizard = wiz.AllocationWizard(
            _base(), _mixed_plan(), market=_open_market(),
            on_refresh=lambda f: (_mixed_plan(), _closed_market()),
            on_submit=submitted.append)
        wizard.open()
        wizard._refresh(wizard.allow_fractional)
        wizard._submit()

    assert submitted == []
    assert wizard._submitted is False


def test_a_failed_refresh_leaves_the_gate_exactly_where_it_was(nicegui_client):
    """``on_refresh`` raising must not silently unlock Submit -- nor lock it."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    def _boom(_allow_fractional):
        raise RuntimeError("broker down")

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(), market=_closed_market(),
                                      on_refresh=_boom, on_submit=lambda p: None)
        wizard.open()
        wizard._refresh(wizard.allow_fractional)

    assert wizard.market.allowed is False
    assert wizard._submit_button.enabled is False


def test_an_open_market_the_broker_never_confirmed_is_flagged_on_screen(nicegui_client):
    """I5: the gate ALLOWS on the offline calendar's word. Saying nothing lets a
    submission go out on a timetable that cannot see an unscheduled halt."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(),
                                      market=_fallback_open_market(),
                                      on_refresh=lambda f: (_mixed_plan(),
                                                            _fallback_open_market()),
                                      on_submit=lambda p: None)
        wizard.open()
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_MARKET_BANNER)

    # Submit still works -- this is a caveat, not a block.
    assert wizard._submit_button.enabled is True
    assert len(drawn) == 1
    assert "did not answer" in drawn[0]


def test_a_broker_confirmed_open_market_draws_no_caveat(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.AllocationWizard(_base(), _mixed_plan(), market=_open_market(),
                             on_refresh=lambda f: (_mixed_plan(), _open_market()),
                             on_submit=lambda p: None).open()
        assert _marked_texts(nicegui_client.layout, wiz.MARKER_MARKET_BANNER) == []


def test_refreshing_onto_a_fallback_open_raises_the_caveat_that_was_not_there(
        nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(
            _base(), _mixed_plan(), market=_open_market(),
            on_refresh=lambda f: (_mixed_plan(), _fallback_open_market()),
            on_submit=lambda p: None)
        wizard.open()
        assert _marked_texts(nicegui_client.layout, wiz.MARKER_MARKET_BANNER) == []

        wizard._refresh(wizard.allow_fractional)

        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_MARKET_BANNER)
    assert len(drawn) == 1
    assert "did not answer" in drawn[0]


# ---------------------------------------------------------------------------
# W1: a HELD symbol with no quote blocks a MARKET-mode submission.
#
# In market mode such a position contributes 0 to the allocatable base, so every
# label's target shrinks by its share of the missing money -- and the dry run
# cannot show it, because every row is consistently too small. Disabling Submit
# here is the courtesy half; ``run_allocation`` re-derives it server-side.
# ---------------------------------------------------------------------------

def _base_with_an_unpriced_holding():
    return BaseSnapshot(available_buying_power=5_000.0, managed_value=0.0,
                        base_notional=5_000.0, default_bp_factor=1.0,
                        valuation_mode=VALUATION_MODE_MARKET, cash=5_000.0,
                        unpriced_held_symbols=["DARK"])


def test_an_unpriced_holding_disables_submit_even_with_the_market_open(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base_with_an_unpriced_holding(), _mixed_plan(),
                                      market=_open_market(),
                                      on_refresh=lambda f: (_mixed_plan(), _open_market()),
                                      on_submit=lambda p: pytest.fail("must not submit"))
        wizard.open()
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_BASE_BLOCK)

    assert wizard._submit_button.enabled is False
    assert len(drawn) == 1
    assert "DARK" in drawn[0]


def test_an_unpriced_holding_refuses_the_submit_click_itself(nicegui_client):
    """The disabled button is a mirror and a mirror can be stale (a keyboard
    activation, a stale client). The handler re-checks."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    calls = []
    with nicegui_client:
        wizard = wiz.AllocationWizard(_base_with_an_unpriced_holding(), _mixed_plan(),
                                      market=_open_market(),
                                      on_refresh=lambda f: (_mixed_plan(), _open_market()),
                                      on_submit=lambda p: calls.append(p))
        wizard.open()
        wizard._submit()

    assert calls == []
    # NOT latched: nothing was sent, so a user who fixes the quote and reopens must
    # still be able to submit for real.
    assert wizard._submitted is False


def test_a_fully_priced_book_draws_no_block_and_leaves_submit_alone(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(), market=_open_market(),
                                      on_refresh=lambda f: (_mixed_plan(), _open_market()),
                                      on_submit=lambda p: None)
        wizard.open()
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_BASE_BLOCK)

    assert drawn == []
    assert wizard._submit_button.enabled is True


def test_the_unpriced_holding_block_survives_a_closed_market(nicegui_client):
    """Two independent reasons to refuse. Whichever is shown, Submit stays off."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base_with_an_unpriced_holding(), _mixed_plan(),
                                      market=_closed_market(),
                                      on_refresh=lambda f: (_mixed_plan(), _open_market()),
                                      on_submit=lambda p: pytest.fail("must not submit"))
        wizard.open()

    assert wizard._submit_button.enabled is False


# ---------------------------------------------------------------------------
# W0: Continue now WRITES, so the dialog has to say so.
#
# Until W0 nothing on this screen touched the database and "Cancel really
# cancels" was a guarantee. It is not one any more: Continue persists the label
# targets and the symbol weights (that is what makes "load last" possible), so a
# dry run the user then abandons has already changed stored state.
# ---------------------------------------------------------------------------

def test_the_steps_dialog_says_that_continue_saves_the_targets(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz)
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_CONTINUE_SAVES)

    assert len(drawn) == 1
    assert 'Cancel' in drawn[0]          # names what stopped being true


def test_the_continue_saves_note_is_shown_in_invest_mode_too(nicegui_client):
    """An INVEST run persists the chosen label's symbol weights, so the same
    warning applies -- only the label percentage is left alone."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from ba2_trade_platform.core.portfolio_allocation import ALLOCATION_MODE_INVEST_LABEL

    _open_steps(nicegui_client, wiz, mode=ALLOCATION_MODE_INVEST_LABEL,
                invest_amount=1_000.0)

    assert len(_marked_texts(nicegui_client.layout, wiz.MARKER_CONTINUE_SAVES)) == 1


def test_the_steps_docstring_no_longer_promises_that_nothing_is_written():
    """The class docstring said "Nothing is written here ... so Cancel really
    cancels". After W0 that is false, and a docstring that lies about a write is
    worse than none."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    doc = wiz.AllocationSteps.__doc__
    assert 'Nothing is written here' not in doc
    assert 'Continue' in doc


# ---------------------------------------------------------------------------
# W2: "Load last", and CURRENT next to TARGET.
#
# Every new widget here is a ``ui.label``, never a second marked ``ui.number``.
# The tests above index positionally into ``_numbers(..., MARKER_LABEL_PCT)`` and
# ``MARKER_SYMBOL_PCT``, so an extra number under either marker would silently
# retarget them rather than fail.
# ---------------------------------------------------------------------------

def _labels_with_history():
    return [
        LabelTarget("Growth", 70.0,
                    [SymbolTarget("AAPL", 60.0, previous_weight_pct=50.0),
                     SymbolTarget("MSFT", 40.0, previous_weight_pct=50.0)],
                    previous_target_pct=60.0),
        LabelTarget("Income", 30.0, [SymbolTarget("KO", 100.0)],
                    previous_target_pct=40.0),
    ]


def _buttons(client, marker):
    from nicegui import ui as nicegui_ui
    return [d for d in client.layout.descendants()
            if isinstance(d, nicegui_ui.button) and marker in getattr(d, '_markers', [])]


def test_step_one_offers_load_last_when_there_is_a_last(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, labels=_labels_with_history())

    found = _buttons(nicegui_client, wiz.MARKER_LOAD_LAST)
    assert len(found) == 1
    assert found[0].enabled is True


def test_step_one_disables_load_last_when_no_label_has_a_history(nicegui_client):
    """Disabled, not hidden: the user has to be able to see the feature exists and
    learn that this account has never run an allocation."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz)          # _labels() carries no history

    found = _buttons(nicegui_client, wiz.MARKER_LOAD_LAST)
    assert len(found) == 1
    assert found[0].enabled is False


def test_pressing_load_last_restores_the_percentages_of_the_last_run(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_labels_with_history())
    with nicegui_client:
        steps._load_last()

    assert [lt.target_pct for lt in steps.labels] == [60.0, 40.0]


def test_load_last_redraws_the_boxes_rather_than_leaving_them_stale(nicegui_client):
    """``_even_split`` re-draws for exactly this reason: a ``ui.number`` does not
    follow the object it was built from, so a silent model change leaves the user
    typing over numbers that are no longer what will be submitted."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_labels_with_history())
    with nicegui_client:
        steps._load_last()
        drawn = [n.value for n in _numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)]

    assert drawn == [60.0, 40.0]


def test_step_one_shows_the_last_percentage_beside_each_target(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, labels=_labels_with_history())
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_CURRENT)

    assert len(drawn) == 2
    assert 'last 60.00%' in drawn[0]
    assert 'last 40.00%' in drawn[1]


def test_a_label_with_no_history_says_so_rather_than_showing_a_number(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz)
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_CURRENT)

    assert len(drawn) == 2
    assert all('last -' in text for text in drawn), drawn


def test_step_one_shows_each_labels_current_value_against_the_same_base(nicegui_client):
    """The percentage beside a target has to be a share of ``base_notional``, the
    SAME denominator the target itself divides. The page's own header used to put
    "% of managed" next to "target %" -- two denominators, invited comparison."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    # base_notional is 10,000 (see ``_base``); Growth holds 2,500 of it.
    _open_steps(nicegui_client, wiz, labels=_labels_with_history(),
                symbol_values={'AAPL': 1_500.0, 'MSFT': 1_000.0, 'KO': 500.0})
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_CURRENT)

    assert 'now 2,500.00' in drawn[0]
    assert '25.00% of base' in drawn[0]
    assert 'now 500.00' in drawn[1]
    assert '5.00% of base' in drawn[1]


def _fifty_fifty_labels():
    """Two labels at 50/50, each holding exactly half of the 10,000 base."""
    return [LabelTarget("A", 50.0, [SymbolTarget("AAA", 100.0)]),
            LabelTarget("B", 50.0, [SymbolTarget("BBB", 100.0)])]


def test_the_caption_states_the_target_as_a_share_of_base_not_only_as_a_weight(
        nicegui_client):
    """THE MISLEADING COMPARISON. Base 10,000 fully held, two labels at 50/50 each
    holding exactly 5,000, reserve 10%.

    The caption read ``now 5,000.00 (50.00% of base)`` beside a target box reading
    ``50``, and those two 50s divide DIFFERENT denominators: the caption the gross
    base, the box the investable remainder. So the row looked perfectly on target
    while the plan will sell it down to 4,500. The caption now states the target in
    the caption's OWN denominator -- 50% of what the reserve leaves IS 45% of the
    base -- so the pair on the line is comparable without arithmetic.
    """
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, labels=_fifty_fifty_labels(),
                symbol_values={'AAA': 5_000.0, 'BBB': 5_000.0}, unallocated_pct=10.0)
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_CURRENT)

    assert 'now 5,000.00 (50.00% of base)' in drawn[0], drawn[0]
    assert '45.00% of base' in drawn[0], drawn[0]


def test_with_no_reserve_the_target_share_of_base_is_the_number_in_the_box(
        nicegui_client):
    """The other end of the same statement: at 0% reserve the relative weight and
    the share of base coincide, and the caption must say so rather than going
    quiet -- a clause that appears only under a reserve is one the user has to
    learn about at the worst moment."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, labels=_fifty_fifty_labels(),
                symbol_values={'AAA': 5_000.0, 'BBB': 5_000.0})
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_CURRENT)

    assert 'now 5,000.00 (50.00% of base)' in drawn[0], drawn[0]
    assert '50.00% of base' in drawn[0].split('(50.00% of base)')[1], drawn[0]


def test_the_target_share_of_base_follows_the_reserve_box(nicegui_client):
    """It is derived from TWO live inputs, so a caption drawn once and never
    refreshed is worse than none: it would keep asserting 50% of base while the
    reserve the user just typed made it 25%."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, labels=_fifty_fifty_labels(),
                symbol_values={'AAA': 5_000.0, 'BBB': 5_000.0})
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_UNALLOCATED_PCT)[0].set_value(50.0)
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_CURRENT)

    assert '25.00% of base' in drawn[0], drawn[0]
    assert '25.00% of base' in drawn[1], drawn[1]


def test_the_target_share_of_base_follows_the_label_box(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, labels=_fifty_fifty_labels(),
                symbol_values={'AAA': 5_000.0, 'BBB': 5_000.0}, unallocated_pct=10.0)
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)[0].set_value(80.0)
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_CURRENT)

    assert '72.00% of base' in drawn[0], drawn[0]      # 80% of the 90% left
    assert '45.00% of base' in drawn[1], drawn[1]      # B did not move


def test_step_two_offers_a_load_last_per_label(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, labels=_labels_with_history())

    found = _buttons(nicegui_client, wiz.MARKER_LOAD_LAST_SYMBOLS)
    assert len(found) == 2
    assert [b.enabled for b in found] == [True, False]   # Income has no per-symbol history


def test_pressing_a_labels_load_last_restores_only_that_labels_weights(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_labels_with_history())
    with nicegui_client:
        steps._load_last_symbols(steps.labels[0])

    assert [st.weight_pct for st in steps.labels[0].symbols] == [50.0, 50.0]
    assert [st.weight_pct for st in steps.labels[1].symbols] == [100.0]
    # The label's own target is step 1's business and must not move.
    assert steps.labels[0].target_pct == 70.0


# ---------------------------------------------------------------------------
# Step 2's per-label "Even split" -- the symbol-level pair to step 1's button.
# ---------------------------------------------------------------------------


def _six_symbol_labels():
    """One label of SIX symbols, one of two.

    Six is not arbitrary: it is the smallest count at which ``even_split_pct`` and
    a hand-rolled ``round(100 / n, 2)`` disagree (16.66 with 16.70 on the last slot
    against 16.67 with 16.65), so a button that re-implemented the split instead of
    calling the engine fails here and passes at two, three or five.
    """
    return [
        LabelTarget("Growth", 60.0,
                    [SymbolTarget(f"S{i}", 100.0 / 6.0) for i in range(6)]),
        LabelTarget("Income", 40.0,
                    [SymbolTarget("KO", 70.0), SymbolTarget("PEP", 30.0)]),
    ]


def test_step_two_offers_an_even_split_per_label(nicegui_client):
    """One per label, and DISABLED where it would be meaningless -- Income holds a
    single symbol, which already owns the whole 100. Disabled, never hidden, on the
    same terms as the Load-last buttons beside it."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, labels=_labels_with_history())

    found = _buttons(nicegui_client, wiz.MARKER_EVEN_SPLIT_SYMBOLS)
    assert len(found) == 2
    assert [b.enabled for b in found] == [True, False]


def test_step_two_still_draws_a_disabled_even_split_for_a_label_with_no_symbols(nicegui_client):
    """A label nothing carries draws the button too. Hiding it would make the
    feature look absent rather than inapplicable."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    labels = [LabelTarget("Growth", 100.0, [SymbolTarget("AAPL", 50.0),
                                            SymbolTarget("MSFT", 50.0)]),
              LabelTarget("Empty", 0.0, [])]
    _open_steps(nicegui_client, wiz, labels=labels)

    found = _buttons(nicegui_client, wiz.MARKER_EVEN_SPLIT_SYMBOLS)
    assert len(found) == 2
    assert [b.enabled for b in found] == [True, False]


def test_pressing_a_labels_even_split_touches_only_that_label(nicegui_client):
    """The scoping proof, and it goes through the BUTTON rather than the method so
    the ``t=lt`` default-argument capture is under test too: without it every one of
    these buttons would re-split the LAST label. Growth's six symbols are re-split;
    Income's two keep the 70/30 the user typed, and Growth's own target stays
    step 1's business."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_six_symbol_labels())
    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_EVEN_SPLIT_SYMBOLS)[0])

    assert [st.weight_pct for st in steps.labels[0].symbols] == [16.66, 16.66, 16.66,
                                                                 16.66, 16.66, 16.7]
    assert [st.weight_pct for st in steps.labels[1].symbols] == [70.0, 30.0]
    assert steps.labels[0].target_pct == 60.0
    assert steps.labels[1].target_pct == 40.0


def test_an_even_split_of_six_symbols_uses_the_engines_own_splitter(nicegui_client):
    """Byte-identical to ``even_split_pct(6)``, totalling exactly 100. A hand-rolled
    two-decimal split lands on 16.67 x 5 + 16.65 here and the wizard would then
    disagree with the stored default for the very same six symbols."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from ba2_trade_platform.core.portfolio_allocation import even_split_pct

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_six_symbol_labels())
    with nicegui_client:
        steps._even_split_symbols(steps.labels[0])

    weights = [st.weight_pct for st in steps.labels[0].symbols]
    assert weights == even_split_pct(6)
    assert sum(weights) == 100.0


def test_even_split_symbols_redraws_the_boxes_rather_than_leaving_them_stale(nicegui_client):
    """A ``ui.number`` does not follow the object it was built from -- the reason
    ``_even_split`` and ``_load_last_symbols`` both redraw. Without it the user
    types over numbers that are no longer what Continue will submit."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_six_symbol_labels())
    with nicegui_client:
        steps._even_split_symbols(steps.labels[1])
        drawn = [n.value for n in _numbers(nicegui_client, wiz, wiz.MARKER_SYMBOL_PCT)]

    # Growth's six are untouched; Income's two are the new 50/50.
    assert drawn[6:] == [50.0, 50.0]
    assert drawn[:6] == [st.weight_pct for st in steps.labels[0].symbols]


def test_even_split_symbols_revalidates_so_continue_follows(nicegui_client):
    """The live total chip and Continue are driven by ``_revalidate``. Repairing a
    broken label without it leaves Submit barred on a set that is now legal."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    labels = [LabelTarget("Growth", 100.0,
                          [SymbolTarget("AAPL", 90.0), SymbolTarget("MSFT", 90.0)])]
    steps, _calls = _open_steps(nicegui_client, wiz, labels=labels)
    assert steps._continue_button.enabled is False       # 180%, blocked

    with nicegui_client:
        steps._even_split_symbols(steps.labels[0])

    assert [st.weight_pct for st in steps.labels[0].symbols] == [50.0, 50.0]
    assert steps._continue_button.enabled is True


def test_the_symbol_even_split_leaves_the_reserve_and_the_label_total_alone(nicegui_client):
    """Symbol weights have always been relative to their OWN label. The reserve and
    the label percentages divide the base above this level, so a split down here
    must not reach either."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_six_symbol_labels(),
                                unallocated_pct=25.0)
    with nicegui_client:
        steps._even_split_symbols(steps.labels[0])

    assert steps.unallocated_pct == 25.0
    assert [lt.target_pct for lt in steps.labels] == [60.0, 40.0]
    assert steps._continue_button.enabled is True


def test_step_one_still_has_exactly_one_label_level_even_split(nicegui_client):
    """Two labels now draw an "Even split" each in step 2. The step 1 button that
    splits the LABELS must stay singular, and must stay the one that moves them."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_six_symbol_labels())

    found = _buttons(nicegui_client, wiz.MARKER_EVEN_SPLIT)
    assert len(found) == 1
    with nicegui_client:
        _click(found[0])

    assert [lt.target_pct for lt in steps.labels] == [50.0, 50.0]


# ---------------------------------------------------------------------------
# Step 2's per-label "Fill rest" and "Wipe" -- define a few by hand, fill the
# remainder evenly across what is left; wipe to start the label over.
# ---------------------------------------------------------------------------


def _partly_filled_labels():
    """Growth: 60 spoken for and three empty slots. Income: fully allocated.

    So Growth's Fill rest is enabled and Income's is not, while BOTH can be wiped
    -- the two predicates are independent and this fixture separates them.
    """
    return [
        LabelTarget("Growth", 60.0,
                    [SymbolTarget("MANUAL", 60.0)]
                    + [SymbolTarget(f"S{i}", 0.0) for i in range(3)]),
        LabelTarget("Income", 40.0, [SymbolTarget("KO", 70.0), SymbolTarget("PEP", 30.0)]),
    ]


def test_step_two_offers_a_fill_rest_per_label(nicegui_client):
    """One per label, DISABLED where there is nothing left to fill -- Income's two
    weights already spend its whole 100. Disabled, never hidden, on the same terms
    as the Even-split and Load-last buttons beside it."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, labels=_partly_filled_labels())

    found = _buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)
    assert len(found) == 2
    assert [b.enabled for b in found] == [True, False]


def test_step_two_offers_a_wipe_per_label(nicegui_client):
    """One per label, DISABLED where every weight is already 0 -- there is nothing
    to destroy. A label nothing carries draws it too, so the feature reads as
    inapplicable rather than absent."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    labels = [LabelTarget("Growth", 100.0, [SymbolTarget("AAPL", 50.0),
                                            SymbolTarget("MSFT", 50.0)]),
              LabelTarget("Blank", 0.0, [SymbolTarget("AAA", 0.0),
                                         SymbolTarget("BBB", 0.0)]),
              LabelTarget("Empty", 0.0, [])]
    _open_steps(nicegui_client, wiz, labels=labels)

    found = _buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)
    assert len(found) == 3
    assert [b.enabled for b in found] == [True, False, False]


def test_pressing_a_labels_fill_rest_touches_only_that_label(nicegui_client):
    """The scoping proof, through the BUTTON so the ``t=lt`` default-argument
    capture is under test: without it every one of these would rewrite the LAST
    label. Growth's three empty slots divide the 40 that is left; the 60 the user
    typed is untouched, Income keeps its 70/30, and neither label's own target
    moves."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_partly_filled_labels())
    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0])

    assert [st.weight_pct for st in steps.labels[0].symbols] == [60.0, 13.33, 13.33, 13.34]
    assert [st.weight_pct for st in steps.labels[1].symbols] == [70.0, 30.0]
    assert [lt.target_pct for lt in steps.labels] == [60.0, 40.0]


def test_pressing_a_labels_wipe_touches_only_that_label(nicegui_client):
    """The same scoping proof for Wipe: one mis-scoped lambda and a click on
    Growth's wipe would clear Income."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_partly_filled_labels())
    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0])

    assert [st.weight_pct for st in steps.labels[0].symbols] == [0.0, 0.0, 0.0, 0.0]
    assert [st.weight_pct for st in steps.labels[1].symbols] == [70.0, 30.0]
    assert [lt.target_pct for lt in steps.labels] == [60.0, 40.0]


def test_filling_an_untouched_label_lands_exactly_on_the_even_split(nicegui_client):
    """With nothing spoken for, "fill what is left" IS "split the 100 evenly", and
    SIX is the count that catches a re-implementation: the engine's splitter floors
    to 16.66 and puts 16.70 on the last slot, a hand-rolled ``round(100 / n, 2)``
    produces 16.67 x 5 + 16.65. Pressed through both buttons on identical labels,
    the two must agree symbol for symbol."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from ba2_trade_platform.core.portfolio_allocation import even_split_pct

    labels = [LabelTarget("Fill", 50.0, [SymbolTarget(f"S{i}", 0.0) for i in range(6)]),
              LabelTarget("Split", 50.0, [SymbolTarget(f"S{i}", 0.0) for i in range(6)])]
    steps, _calls = _open_steps(nicegui_client, wiz, labels=labels)
    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0])
        _click(_buttons(nicegui_client, wiz.MARKER_EVEN_SPLIT_SYMBOLS)[1])

    filled = [st.weight_pct for st in steps.labels[0].symbols]
    assert filled == [st.weight_pct for st in steps.labels[1].symbols]
    assert filled == even_split_pct(6)
    assert filled == [16.66, 16.66, 16.66, 16.66, 16.66, 16.7]
    assert sum(filled) == 100.0


def test_fill_rest_redraws_the_boxes_rather_than_leaving_them_stale(nicegui_client):
    """A ``ui.number`` does not follow the object it was built from -- the reason
    every step-2 control redraws. Without it the user types over numbers that are
    no longer what Continue will submit."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_partly_filled_labels())
    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0])
        drawn = [n.value for n in _numbers(nicegui_client, wiz, wiz.MARKER_SYMBOL_PCT)]

    assert drawn[:4] == [60.0, 13.33, 13.33, 13.34]
    assert drawn[4:] == [70.0, 30.0]


def test_wipe_redraws_the_boxes_rather_than_leaving_them_stale(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_partly_filled_labels())
    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0])
        drawn = [n.value for n in _numbers(nicegui_client, wiz, wiz.MARKER_SYMBOL_PCT)]

    assert drawn[:4] == [0.0, 0.0, 0.0, 0.0]
    assert drawn[4:] == [70.0, 30.0]


def test_fill_rest_revalidates_so_continue_follows(nicegui_client):
    """The live total chip and Continue are driven by ``_revalidate``. Repairing a
    broken label without it leaves Continue barred on a set that is now legal."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    labels = [LabelTarget("Growth", 100.0, [SymbolTarget("AAPL", 30.0),
                                            SymbolTarget("MSFT", 0.0)])]
    steps, _calls = _open_steps(nicegui_client, wiz, labels=labels)
    assert steps._continue_button.enabled is False       # 30%, blocked

    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0])

    assert [st.weight_pct for st in steps.labels[0].symbols] == [30.0, 70.0]
    assert steps._continue_button.enabled is True


def test_wipe_revalidates_so_continue_stops_following(nicegui_client):
    """The re-validate has to run in the DESTRUCTIVE direction too: a wipe takes a
    legal label to 0% and Continue must go with it. Without it the user submits a
    label they have just emptied."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    labels = [LabelTarget("Growth", 100.0, [SymbolTarget("AAPL", 50.0),
                                            SymbolTarget("MSFT", 50.0)])]
    steps, _calls = _open_steps(nicegui_client, wiz, labels=labels)
    assert steps._continue_button.enabled is True

    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0])

    assert steps._continue_button.enabled is False


def test_fill_rest_and_wipe_leave_the_reserve_and_the_label_total_alone(nicegui_client):
    """Symbol weights have always been relative to their OWN label. The reserve and
    the label percentages divide the base above this level, so neither control down
    here may reach them."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_partly_filled_labels(),
                                unallocated_pct=25.0)
    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0])
        _click(_buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0])

    assert steps.unallocated_pct == 25.0
    assert [lt.target_pct for lt in steps.labels] == [60.0, 40.0]
    assert steps._continue_button.enabled is True


def test_the_wipe_type_fill_workflow_end_to_end(nicegui_client):
    """The feature as the user described it: wipe the label, type the couple that
    matter, let the rest share what is left."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    labels = [LabelTarget("Growth", 100.0,
                          [SymbolTarget(f"S{i}", 100.0 / 6.0) for i in range(6)])]
    steps, _calls = _open_steps(nicegui_client, wiz, labels=labels)
    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0])
        # Through the boxes the user actually types in -- the wipe has just redrawn
        # them, so these are the fresh ones.
        boxes = _numbers(nicegui_client, wiz, wiz.MARKER_SYMBOL_PCT)
        boxes[0].set_value(25.0)
        boxes[1].set_value(15.0)
        _click(_buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0])

    # 40 typed, 60 shared four ways.
    assert [st.weight_pct for st in steps.labels[0].symbols] == [25.0, 15.0, 15.0,
                                                                 15.0, 15.0, 15.0]
    assert steps._continue_button.enabled is True


def test_fill_rest_disables_itself_once_the_label_is_full(nicegui_client):
    """The enabled state has to follow the weights or it is decoration. Fill the
    label and there is nothing left to fill, so the button greys out in place --
    without a redraw of the row, which would rebuild boxes under the cursor."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_partly_filled_labels())
    fill = _buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0]
    assert fill.enabled is True

    with nicegui_client:
        _click(fill)

    assert _buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0].enabled is False
    # ... and Wipe, which was already live, stays live.
    assert _buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0].enabled is True


def test_wipe_disables_itself_once_the_label_is_clear(nicegui_client):
    """Symmetrically: nothing left to destroy, and now everything to fill."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_partly_filled_labels())
    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0])

    assert _buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0].enabled is False
    assert _buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0].enabled is True
    # Income's pair is unaffected -- the refresh is per-label like everything else.
    assert _buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[1].enabled is True
    assert _buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[1].enabled is False


def test_typing_a_weight_follows_through_to_the_two_new_buttons(nicegui_client):
    """Both predicates depend on the numbers in the boxes, so typing has to refresh
    them too -- otherwise a user who types 100 into the last empty box is still
    offered a Fill rest that can only write zeros.

    Driven through ``set_value`` on the marked box, so the weight box's own
    per-label capture is under test: a refresh aimed at the wrong label would leave
    Growth's pair stale and silently retarget Income's.
    """
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    labels = [LabelTarget("Growth", 60.0, [SymbolTarget("AAPL", 0.0),
                                           SymbolTarget("MSFT", 0.0)]),
              LabelTarget("Income", 40.0, [SymbolTarget("KO", 70.0),
                                           SymbolTarget("PEP", 30.0)])]
    steps, _calls = _open_steps(nicegui_client, wiz, labels=labels)
    assert _buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0].enabled is True
    assert _buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0].enabled is False

    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_SYMBOL_PCT)[0].set_value(100.0)

    assert steps.labels[0].symbols[0].weight_pct == 100.0
    assert _buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0].enabled is False
    assert _buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0].enabled is True
    # Income never moved, so its pair reads exactly as it did.
    assert _buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[1].enabled is False
    assert _buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[1].enabled is True


def test_an_over_allocated_label_disables_fill_rest_but_never_wipe(nicegui_client):
    """The escape hatch. 70 + 50 leaves nothing to hand out, so Fill rest is
    disabled rather than a click that writes zeros or negatives -- and Wipe is
    enabled in exactly that case, so the user is never cornered into retyping every
    box by hand."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    labels = [LabelTarget("Growth", 100.0, [SymbolTarget("AAPL", 70.0),
                                            SymbolTarget("MSFT", 50.0),
                                            SymbolTarget("NVDA", 0.0)])]
    steps, _calls = _open_steps(nicegui_client, wiz, labels=labels)

    assert _buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0].enabled is False
    assert _buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0].enabled is True
    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0])
        _click(_buttons(nicegui_client, wiz.MARKER_FILL_REST_SYMBOLS)[0])

    assert [st.weight_pct for st in steps.labels[0].symbols] == [33.33, 33.33, 33.34]
    assert steps._continue_button.enabled is True


def test_wipe_does_not_ask_for_confirmation(nicegui_client):
    """A deliberate NO, and this pins it.

    ``_confirm_unmanage`` on the allocation page asks first because an unmanage
    writes to the database at once and deletes stored weights and comments with no
    undo. A wipe is the opposite on every count: it edits the dialog's own COPY of
    the labels, nothing reaches the database until Submit two steps and a dry run
    later, Cancel discards the lot, and Load last / Even split / Fill rest sit
    beside it as one-click undos. Confirming both would teach the user to click
    through confirmations, which is how the one that matters gets lost.
    """
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from nicegui import ui as nicegui_ui

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_partly_filled_labels())
    before = len([d for d in nicegui_client.layout.descendants()
                  if isinstance(d, nicegui_ui.dialog)])
    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0])

    after = len([d for d in nicegui_client.layout.descendants()
                 if isinstance(d, nicegui_ui.dialog)])
    assert after == before
    # The weights are gone in the same click, not after an await.
    assert [st.weight_pct for st in steps.labels[0].symbols] == [0.0, 0.0, 0.0, 0.0]


def test_wipe_keeps_load_last_available_as_its_undo(nicegui_client):
    """The wipe carries ``previous_weight_pct`` across, so the control that undoes
    it stays enabled. That is a large part of why it needs no confirmation."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, labels=_labels_with_history())
    with nicegui_client:
        _click(_buttons(nicegui_client, wiz.MARKER_WIPE_SYMBOLS)[0])
    assert [st.weight_pct for st in steps.labels[0].symbols] == [0.0, 0.0]

    last = _buttons(nicegui_client, wiz.MARKER_LOAD_LAST_SYMBOLS)[0]
    assert last.enabled is True
    with nicegui_client:
        _click(last)

    assert [st.weight_pct for st in steps.labels[0].symbols] == [50.0, 50.0]


def test_step_two_draws_all_four_controls_for_every_label(nicegui_client):
    """One row, four buttons, one set per label -- and step 1's own pair stays
    singular. A marker collision here is how a test starts asserting against the
    wrong button."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, labels=_partly_filled_labels())

    for marker in (wiz.MARKER_EVEN_SPLIT_SYMBOLS, wiz.MARKER_FILL_REST_SYMBOLS,
                   wiz.MARKER_WIPE_SYMBOLS, wiz.MARKER_LOAD_LAST_SYMBOLS):
        assert len(_buttons(nicegui_client, marker)) == 2, marker
    assert len(_buttons(nicegui_client, wiz.MARKER_EVEN_SPLIT)) == 1
    assert len(_buttons(nicegui_client, wiz.MARKER_LOAD_LAST)) == 1


def test_step_two_shows_the_last_weight_beside_each_symbol(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, labels=_labels_with_history(),
                symbol_values={'AAPL': 1_500.0, 'MSFT': 1_000.0, 'KO': 500.0})
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_SYMBOL_CURRENT)

    assert len(drawn) == 3
    assert 'now 1,500.00' in drawn[0] and 'last 50.00%' in drawn[0]
    assert 'last -' in drawn[2]              # KO has never run


def test_the_percentage_boxes_are_still_the_only_marked_numbers(nicegui_client):
    """The regression this whole section is written around: the landed suite indexes
    positionally into these two marker sets, so any new numeric widget under either
    would silently retarget six assertions instead of failing."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, labels=_labels_with_history())

    assert len(_numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)) == 2
    assert len(_numbers(nicegui_client, wiz, wiz.MARKER_SYMBOL_PCT)) == 3


# ---------------------------------------------------------------------------
# W8: the Unallocated row is an EDITABLE, STORED reserve.
#
# It stays FIRST in the label list, but its percentage is now an input with a live
# dollar figure beside it. The running total chip reads the LABEL total only -- the
# reserve is never added into it, or the user is back to doing arithmetic.
# ---------------------------------------------------------------------------

def test_step_one_draws_the_unallocated_row_as_an_editable_number(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, unallocated_pct=10.0)
    boxes = _numbers(nicegui_client, wiz, wiz.MARKER_UNALLOCATED_PCT)

    assert len(boxes) == 1
    assert boxes[0].value == 10.0


def test_the_unallocated_row_shows_its_dollar_value_beside_the_box(nicegui_client):
    """A percentage alone does not tell the user whether they are holding back 300
    or 30,000."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, unallocated_pct=10.0)
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_UNALLOCATED)

    assert len(drawn) == 1
    assert '1,000.00' in drawn[0]          # 10% of the 10,000 base
    assert '9,000.00' in drawn[0]          # ...leaving this to invest


def test_the_reserve_caption_says_which_half_is_the_reserve_and_which_is_left(
        nicegui_client):
    """The two money halves must not be interchangeable.

    Both numbers appear in one sentence, so ``'1,000.00' in caption and '9,000.00'
    in caption`` is satisfied just as happily by a caption that has them the wrong
    way round -- reading a 10% reserve on a 10,000 base as 9,000 held back and
    1,000 left to invest, which is the exact inverse of the truth. The reserve is
    named FIRST, and 1,000 != 9,000 on purpose: a fixture where the two halves
    could be equal cannot pin an ordering at all.
    """
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, unallocated_pct=10.0)
    caption = _marked_texts(nicegui_client.layout, wiz.MARKER_UNALLOCATED)[0]

    assert '1,000.00' in caption and '9,000.00' in caption
    assert caption.index('1,000.00') < caption.index('9,000.00'), caption


def test_the_reserve_caption_does_not_promise_cash_on_a_margin_account(nicegui_client):
    """"held as cash" is not true of a reserve measured off ``base_notional``.

    The base is buying power PLUS the value of the book, so reserving 10% of it
    means 10% of the base is left UNDEPLOYED -- on a margin account that is unused
    buying power, and the cash balance can be lower still (``estimated_cash_after``
    is reachably negative). The sentence has to be true on both account types.
    """
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, unallocated_pct=10.0)
    caption = _marked_texts(nicegui_client.layout, wiz.MARKER_UNALLOCATED)[0]

    assert 'cash' not in caption.lower(), caption


def test_the_dollar_value_follows_every_keystroke_in_the_reserve_box(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        box = _numbers(nicegui_client, wiz, wiz.MARKER_UNALLOCATED_PCT)[0]
        box.set_value(25.0)
        assert '2,500.00' in _marked_texts(nicegui_client.layout, wiz.MARKER_UNALLOCATED)[0]
        box.set_value(40.0)
        assert '4,000.00' in _marked_texts(nicegui_client.layout, wiz.MARKER_UNALLOCATED)[0]


def test_editing_the_reserve_records_it_on_the_dialog(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_UNALLOCATED_PCT)[0].set_value(15.0)

    assert steps.unallocated_pct == 15.0


def test_the_unallocated_row_is_still_the_first_thing_in_the_label_list(nicegui_client):
    """At the TOP, because it is the row that says how much of the book is even in
    play. Below the labels it reads as a footnote."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        texts = _rendered_texts(steps._labels_container)

    assert 'Unallocated' in texts[0], texts[:4]


def test_the_reserve_row_is_drawn_at_zero_too(nicegui_client):
    """A row that appears only when it is non-zero teaches the user nothing about
    where the number went when it disappears -- and it is the box they need to find
    when the validator tells them to use it."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz)          # no reserve

    assert len(_numbers(nicegui_client, wiz, wiz.MARKER_UNALLOCATED_PCT)) == 1
    assert '0.00' in _marked_texts(nicegui_client.layout, wiz.MARKER_UNALLOCATED)[0]


def test_editing_the_reserve_does_NOT_rewrite_a_single_label_percentage(nicegui_client):
    """THE WHOLE POINT. The label boxes are relative weights; the reserve scales
    what they divide. Nothing the user typed is ever changed behind them."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_UNALLOCATED_PCT)[0].set_value(35.0)
        assert steps._continue_button.enabled is True
        steps._continue()

    assert [lt.target_pct for lt in steps.labels] == [60.0, 40.0]
    assert [b.value for b in _numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)] == [60.0, 40.0]
    assert calls[0]["unallocated_pct"] == 35.0


def test_the_running_total_chip_reads_the_LABEL_total_and_ignores_the_reserve(
        nicegui_client):
    """If the reserve were folded into this number the user would be doing the
    subtraction the whole feature exists to remove."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz, unallocated_pct=30.0)
    with nicegui_client:
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_TOTAL)
        assert 'Total: 100.00%' in drawn[0]
        _numbers(nicegui_client, wiz, wiz.MARKER_UNALLOCATED_PCT)[0].set_value(50.0)
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_TOTAL)

    assert 'Total: 100.00%' in drawn[0]


def test_the_running_total_chip_still_follows_the_label_boxes(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)[0].set_value(30.0)
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_TOTAL)

    assert 'Total: 70.00%' in drawn[0]


def test_a_reserve_outside_zero_to_one_hundred_blocks_continue(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        _numbers(nicegui_client, wiz, wiz.MARKER_UNALLOCATED_PCT)[0].set_value(140.0)
        messages = _rendered_texts(steps._errors_container)
        assert steps._continue_button.enabled is False
        steps._continue()

    assert any('outside 0-100%' in t for t in messages), messages
    assert calls == []


def test_the_step_one_heading_says_the_labels_must_total_one_hundred(nicegui_client):
    """Both levels again, and the heading has to say so -- it said "up to 100%"
    while a shortfall was legal."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz)
    with nicegui_client:
        texts = _rendered_texts(nicegui_client.layout)

    step1 = next(t for t in texts if t.startswith('1. Label targets'))
    step2 = next(t for t in texts if t.startswith('2. Symbol weights'))
    assert 'must total 100%' in step1
    assert 'up to 100%' not in step1
    assert 'must total 100%' in step2


def test_even_split_always_splits_the_whole_hundred_whatever_the_reserve(nicegui_client):
    """No longer "split what is currently allocated": the reserve is stored apart,
    so there is nothing to preserve and any other total is unsubmittable."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    labels = [LabelTarget("A", 40.0, [SymbolTarget("AA", 100.0)]),
              LabelTarget("B", 30.0, [SymbolTarget("BB", 100.0)])]
    steps, _calls = _open_steps(nicegui_client, wiz, labels=labels, unallocated_pct=30.0)
    with nicegui_client:
        steps._even_split()

    assert [lt.target_pct for lt in steps.labels] == [50.0, 50.0]
    assert steps.unallocated_pct == 30.0          # untouched by the split
    assert steps._continue_button.enabled is True


def test_even_split_of_an_untouched_book_deploys_the_whole_hundred(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    labels = [LabelTarget(n, 0.0, [SymbolTarget(n * 2, 100.0)]) for n in ("A", "B")]
    steps, _calls = _open_steps(nicegui_client, wiz, labels=labels)
    with nicegui_client:
        steps._even_split()

    assert [lt.target_pct for lt in steps.labels] == [50.0, 50.0]


def test_the_invest_flow_draws_no_reserve_box_at_all(nicegui_client):
    """An invest run deploys a specific amount the user named. There is no
    portfolio base to take a share of, so there is nothing to reserve."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, _calls = _open_steps(nicegui_client, wiz,
                                mode=wiz.ALLOCATION_MODE_INVEST_LABEL, invest_amount=1_000.0,
                                unallocated_pct=30.0)

    assert _numbers(nicegui_client, wiz, wiz.MARKER_UNALLOCATED_PCT) == []


def test_the_invest_flow_hands_over_no_reserve(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    steps, calls = _open_steps(nicegui_client, wiz,
                               mode=wiz.ALLOCATION_MODE_INVEST_LABEL, invest_amount=1_000.0,
                               unallocated_pct=30.0)
    with nicegui_client:
        steps._continue()

    assert calls[0]["unallocated_pct"] == 0.0


def _reserved_plan():
    return AllocationPlan(
        rows=[AllocationRow(symbol="AAPL", price=160.0, delta_quantity=10.0,
                            side=OrderDirection.BUY, estimated_value=1_600.0,
                            bp_cost=1_600.0, bp_factor=1.0)],
        base_notional=10_000.0, available_buying_power=10_000.0,
        required_buying_power=1_600.0, total_buy_value=1_600.0,
        reserved_pct=30.0, reserved_notional=3_000.0,
        valuation_mode=VALUATION_MODE_MARKET)


def test_the_dry_run_base_panel_names_the_reserve(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.AllocationWizard(_base(), _reserved_plan(), market=_open_market(),
                             on_refresh=lambda f: (_reserved_plan(), _open_market()),
                             on_submit=lambda p: None).open()
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_RESERVED)

    assert len(drawn) == 1
    assert '3,000.00' in drawn[0] and '30.00%' in drawn[0]


def test_the_dry_run_says_nothing_about_a_reserve_of_zero(nicegui_client):
    """A "Reserved: 0.00" chip on every fully-allocated plan is noise, and noise is
    what makes the non-zero case invisible."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.AllocationWizard(_base(), _mixed_plan(), market=_open_market(),
                             on_refresh=lambda f: (_mixed_plan(), _open_market()),
                             on_submit=lambda p: None).open()
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_RESERVED)

    assert drawn == []


def test_the_dry_run_footer_compares_the_cash_it_expects_against_the_reserve(
        nicegui_client):
    """Requirement 3's arithmetic check, in the one place the user can act on it.
    The reserve is a share of ``base_notional`` -- buying power PLUS managed value --
    so on a fully invested account raising it generates SELL orders to free the
    cash. Correct, and it has to be obvious rather than inferred."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.AllocationWizard(_base(), _reserved_plan(), market=_open_market(),
                             on_refresh=lambda f: (_reserved_plan(), _open_market()),
                             on_submit=lambda p: None).open()
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_CASH_VS_RESERVE)

    # ``_base()`` carries 10,000 cash; the plan buys 1,600, so 8,400 is left
    # against a 3,000 reserve.
    assert len(drawn) == 1
    assert '8,400.00' in drawn[0] and '3,000.00' in drawn[0]


def test_the_cash_versus_reserve_line_says_the_two_are_not_the_same_measurement(
        nicegui_client):
    """It puts a broker CASH balance beside a reserve measured off ``base_notional``
    -- buying power PLUS the book -- and invites the user to read the difference as
    a shortfall to fund.

    On a cash account they very nearly are the same thing. On a margin account they
    are not: the base counts borrowing capacity the cash balance never held, and
    ``estimated_cash_after`` is reachably NEGATIVE on a plan whose reserve is fully
    satisfied. The line has to say which two things it is comparing.
    """
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.AllocationWizard(_base(), _reserved_plan(), market=_open_market(),
                             on_refresh=lambda f: (_reserved_plan(), _open_market()),
                             on_submit=lambda p: None).open()
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_CASH_VS_RESERVE)

    assert len(drawn) == 1
    assert 'not a cash balance' in drawn[0], drawn[0]


def test_the_footer_says_nothing_about_a_reserve_of_zero(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.AllocationWizard(_base(), _mixed_plan(), market=_open_market(),
                             on_refresh=lambda f: (_mixed_plan(), _open_market()),
                             on_submit=lambda p: None).open()
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_CASH_VS_RESERVE)

    assert drawn == []


def test_the_cash_versus_reserve_line_follows_the_ticked_rows(nicegui_client):
    """It is drawn from the FILTERED plan, like every other total: un-ticking the
    only buy leaves all the cash, and the line has to say so."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wizard = wiz.AllocationWizard(
            _base(), _reserved_plan(), market=_open_market(),
            on_refresh=lambda f: (_reserved_plan(), _open_market()),
            on_submit=lambda p: None)
        wizard.open()
        wizard._toggle('AAPL', False)
        drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_CASH_VS_RESERVE)

    assert '10,000.00' in drawn[0]


# ---------------------------------------------------------------------------
# Unrealised P&L in the steps dialog: per symbol in step 2, per label in step 1.
#
# The arithmetic is pinned without NiceGUI in
# ``packages/common/tests/test_portfolio_allocation_wizard.py``. What is pinned
# HERE is that the dialog reaches for the mode-independent figure rather than for
# the ``symbol_values`` map sitting right next to it -- which is the mistake that
# renders 0.00 on every row in cost mode.
# ---------------------------------------------------------------------------


def _pnl_positions():
    """One winner, one loser, one profitable short, across the standard labels.

    Growth: AAPL 10 @ cost 1,500 now 1,600 (+100); MSFT 5 @ cost 2,000 now 1,900
    (-100). Income: KO short 10 @ cost -600 now -500 (+100 on a short).
    """
    from ba2_trade_platform.core.portfolio_allocation import PositionState

    return {
        'AAPL': PositionState(symbol='AAPL', quantity=10.0, cost_basis=1_500.0, price=160.0),
        'MSFT': PositionState(symbol='MSFT', quantity=5.0, cost_basis=2_000.0, price=380.0),
        'KO': PositionState(symbol='KO', quantity=-10.0, cost_basis=-600.0, price=50.0),
    }


def _values_for(positions, mode):
    """``{SYMBOL: current value}`` exactly as ``_load_flow_inputs`` builds it."""
    from ba2_trade_platform.core.portfolio_allocation import current_value

    return {s: current_value(state, mode) for s, state in positions.items()}


def test_step_two_shows_each_symbols_unrealised_pnl_in_money_and_percent(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    positions = _pnl_positions()
    _open_steps(nicegui_client, wiz,
                symbol_values=_values_for(positions, 'market'), positions=positions)
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_SYMBOL_PNL)

    assert len(drawn) == 3
    assert '+100.00' in drawn[0] and '+6.67%' in drawn[0]      # AAPL
    assert '-100.00' in drawn[1] and '-5.00%' in drawn[1]      # MSFT
    assert '+100.00' in drawn[2] and '+16.67%' in drawn[2]     # KO, a SHORT


def test_step_one_shows_each_labels_TOTAL_unrealised_pnl(nicegui_client):
    """Growth is +100 on AAPL and -100 on MSFT: zero dollars on 3,500 of cost."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    positions = _pnl_positions()
    _open_steps(nicegui_client, wiz,
                symbol_values=_values_for(positions, 'market'), positions=positions)
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_PNL)

    assert len(drawn) == 2
    assert '+0.00 (+0.00%)' in drawn[0], drawn[0]
    assert '+100.00 (+16.67%)' in drawn[1], drawn[1]


def test_the_pnl_is_IDENTICAL_in_cost_and_market_valuation(nicegui_client):
    """THE defect this feature is easiest to ship with.

    In cost mode ``current_value`` IS the cost basis, so a P&L taken from the
    ``symbol_values`` map beside it is exactly 0.00 on every row -- silently
    useless, and useless in the direction that reads as a fact. The P&L must come
    from the true market value whichever mode the account is on, and the "now"
    figures moving between the two runs is what proves the modes really differed.
    """
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    positions = _pnl_positions()
    in_mode = {}
    for mode in ('cost', 'market'):
        client = _fresh_client()
        try:
            _open_steps(client, wiz, symbol_values=_values_for(positions, mode),
                        positions=positions)
            in_mode[mode] = (
                _marked_texts(client.layout, wiz.MARKER_SYMBOL_PNL),
                _marked_texts(client.layout, wiz.MARKER_LABEL_PNL),
                _marked_texts(client.layout, wiz.MARKER_SYMBOL_CURRENT),
            )
        finally:
            _drop_client(client)

    assert in_mode['cost'][0] == in_mode['market'][0]      # per symbol
    assert in_mode['cost'][1] == in_mode['market'][1]      # per label
    # ...and the modes really were different: "now" is the cost basis in one and
    # the live value in the other.
    assert in_mode['cost'][2] != in_mode['market'][2]
    assert 'now 1,500.00' in in_mode['cost'][2][0]
    assert 'now 1,600.00' in in_mode['market'][2][0]


def test_a_profitable_short_is_not_rendered_as_a_loss(nicegui_client):
    """Sold KO at 60, now 50: +100 on 600 of basis. Dividing by the SIGNED basis
    turns that into -16.67% -- a winner painted red."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    positions = _pnl_positions()
    _open_steps(nicegui_client, wiz,
                symbol_values=_values_for(positions, 'market'), positions=positions)
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_SYMBOL_PNL)

    assert '+16.67%' in drawn[2], drawn[2]
    assert '-16.67%' not in drawn[2], drawn[2]


def _unpriced_positions():
    """Growth holds a priced AAPL and an unquotable MSFT; Income holds nothing."""
    from ba2_trade_platform.core.portfolio_allocation import PositionState

    return {
        'AAPL': PositionState(symbol='AAPL', quantity=10.0, cost_basis=1_500.0, price=160.0),
        'MSFT': PositionState(symbol='MSFT', quantity=5.0, cost_basis=2_000.0, price=None),
    }


def test_an_unpriced_holding_renders_BLANK_rather_than_zero(nicegui_client):
    """A failed quote reading as "flat" or "break-even" has caused real incidents
    here. The dry run's Value column already draws '-'; this matches it."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    positions = _unpriced_positions()
    _open_steps(nicegui_client, wiz,
                symbol_values=_values_for(positions, 'market'), positions=positions)
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_SYMBOL_PNL)

    assert '0.00' not in drawn[1], drawn[1]
    assert drawn[1].endswith('- (no price)'), drawn[1]


def test_an_unpriced_holding_is_excluded_from_its_labels_total_and_COUNTED(
        nicegui_client):
    """Exactly the dry-run totals' rule: summing the unpriced row at 0 would report
    the whole of its 2,000 cost as a loss."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    positions = _unpriced_positions()
    _open_steps(nicegui_client, wiz,
                symbol_values=_values_for(positions, 'market'), positions=positions)
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_PNL)

    # +100 on AAPL's 1,500 alone. NOT -1,900, which is what including MSFT at 0 gives.
    assert '+100.00 (+6.67%, 1 unpriced excluded)' in drawn[0], drawn[0]


def test_a_label_that_holds_nothing_shows_a_dash_not_a_zero(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, positions=_unpriced_positions())
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_PNL)

    assert drawn[1].endswith('-'), drawn[1]        # Income holds no KO at all
    assert '0.00' not in drawn[1], drawn[1]


def test_a_zero_cost_holding_shows_the_money_and_no_percentage(nicegui_client):
    """A gifted or fully written-down basis makes the return undefined. The money
    is still a fact and is still shown; the percentage is not invented."""
    from ba2_trade_platform.core.portfolio_allocation import PositionState
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    positions = {'KO': PositionState(symbol='KO', quantity=10.0, cost_basis=0.0,
                                     price=50.0)}
    _open_steps(nicegui_client, wiz, positions=positions)
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_SYMBOL_PNL)

    assert '+500.00 (no cost basis)' in drawn[2], drawn[2]


def test_the_wizard_without_positions_draws_no_pnl_rather_than_a_zero(nicegui_client):
    """``positions`` is optional and the page is the only caller that has one. A
    caller that supplies none has no answer, and 0.00 would be a wrong one."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz)
    for marker in (wiz.MARKER_LABEL_PNL, wiz.MARKER_SYMBOL_PNL):
        drawn = _marked_texts(nicegui_client.layout, marker)
        assert drawn, marker
        assert all('0.00' not in text for text in drawn), (marker, drawn)


def test_the_percentage_boxes_are_still_the_only_marked_numbers_with_pnl_drawn(
        nicegui_client):
    """The P&L is a ``ui.label``, for the reason the "now" captions are: the landed
    suite indexes positionally into these two marker sets."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_steps(nicegui_client, wiz, positions=_pnl_positions())

    assert len(_numbers(nicegui_client, wiz, wiz.MARKER_LABEL_PCT)) == 2
    assert len(_numbers(nicegui_client, wiz, wiz.MARKER_SYMBOL_PCT)) == 3


def test_a_labels_percentage_is_money_weighted_not_a_mean_of_its_symbols(nicegui_client):
    """A doubled 1,000 beside a flat 9,000 is +10% of the label, not +50%.

    Averaging the rows weights the smallest holding exactly as heavily as the one
    that dominates the label -- and on a label whose winners and losers cancel it
    reports a return on a P&L of exactly zero dollars.
    """
    from ba2_trade_platform.core.portfolio_allocation import PositionState
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    positions = {
        'AAPL': PositionState(symbol='AAPL', quantity=10.0, cost_basis=1_000.0,
                              price=200.0),                      # 2,000: +100%
        'MSFT': PositionState(symbol='MSFT', quantity=90.0, cost_basis=9_000.0,
                              price=100.0),                      # 9,000: +0%
    }
    _open_steps(nicegui_client, wiz, positions=positions)
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_LABEL_PNL)

    assert '+1,000.00 (+10.00%)' in drawn[0], drawn[0]
    assert '50.00%' not in drawn[0], drawn[0]
