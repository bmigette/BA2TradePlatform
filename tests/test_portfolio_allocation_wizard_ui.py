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
# The INVEST_LABEL scope dialog -- what is LEFT of the old three-step dialog.
#
# Its steps 1 and 2 were the target editor and they are on the allocation PAGE
# now; a REBALANCE opens no dialog at all and goes straight to the dry run. An
# invest run is different in kind: it spends a specific amount on a single label,
# so the run has to be told which label and how much, and neither of those is a
# stored target of anything.
#
# There is no fractional switch here either. Exactly ONE execution control exists
# and it is at the gate, where toggling it re-solves the plan.
# ---------------------------------------------------------------------------


def _labels():
    return [
        LabelTarget("Growth", 60.0, [SymbolTarget("AAPL", 50.0), SymbolTarget("MSFT", 50.0)]),
        LabelTarget("Income", 40.0, [SymbolTarget("KO", 100.0)]),
    ]


def _open_scope(client, wiz, labels=None, **kwargs):
    calls = []
    kwargs.setdefault('on_dry_run', lambda **kw: calls.append(kw))
    # These tests predate the remembered choice and were written against a switch
    # that always opened OFF; passing False keeps them meaning what they meant.
    kwargs.setdefault('allow_fractional', False)
    with client:
        scope = wiz.open_invest_scope(
            _base(), labels if labels is not None else _labels(), **kwargs)
    return scope, calls


def _numbers(client, wiz, marker):
    return [d for d in client.layout.descendants()
            if marker in getattr(d, '_markers', [])]


def test_wizard_module_exposes_the_invest_scope_entry_point():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    assert callable(wiz.open_invest_scope)
    params = inspect.signature(wiz.open_invest_scope).parameters
    assert list(params)[:2] == ["base", "labels"]
    assert "on_dry_run" in params
    assert "invest_amount" in params
    # No ``mode``: there is only one mode left that opens a dialog at all.
    assert "mode" not in params


def test_the_invest_scope_carries_the_remembered_fractional_choice_without_offering_it():
    """It reaches ``on_dry_run`` so the first solve is sized on the account's own
    answer; the CONTROL is at the gate, where changing it re-solves."""
    from nicegui import ui
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    client = _fresh_client()
    try:
        scope, calls = _open_scope(client, wiz, allow_fractional=True,
                                   invest_amount=250.0)
        switches = [d for d in client.layout.descendants()
                    if isinstance(d, ui.switch)]
    finally:
        _drop_client(client)

    assert switches == []
    # ``_base()`` does not support fractional, so the remembered True is vetoed.
    assert scope.allow_fractional is False


def test_a_broker_that_CAN_split_shares_keeps_the_remembered_choice():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    base = _base()
    base.supports_fractional = True
    client = _fresh_client()
    try:
        with client:
            scope = wiz.open_invest_scope(base, _labels(),
                                          on_dry_run=lambda **kw: None,
                                          allow_fractional=True, invest_amount=10.0)
    finally:
        _drop_client(client)
    assert scope.allow_fractional is True



def test_invest_mode_draws_a_label_picker_and_an_amount(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    scope, _ = _open_scope(nicegui_client, wiz, invest_amount=250.0)
    texts = _rendered_texts(nicegui_client.layout)

    assert scope.scope_label == "Growth"
    assert scope.invest_amount == pytest.approx(250.0)
    assert "Invest into one label" in texts
    # No percentage editor of any kind. The amount IS the budget here, and the
    # label targets live on the page.
    from nicegui import ui as _ui
    boxes = [d for d in nicegui_client.layout.descendants()
             if isinstance(d, _ui.number)]
    assert [b._props.get('label') for b in boxes] == ['Amount']


def test_invest_mode_does_not_apply_the_labels_total_100_rule(nicegui_client):
    """A single label at 40% is legitimate on this path."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    scope, calls = _open_scope(nicegui_client, wiz,
                               labels=[LabelTarget("Income", 40.0,
                                                   [SymbolTarget("KO", 100.0)])],
                               invest_amount=250.0)
    with nicegui_client:
        assert scope._continue_button.enabled is True
        scope._continue()

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
    scope, calls = _open_scope(nicegui_client, wiz, labels=labels,
                               invest_amount=250.0)
    with nicegui_client:
        errors = _rendered_texts(scope._errors_container)
        assert scope._continue_button.enabled is False
        scope._continue()

    assert any("150.00" in t for t in errors)
    assert calls == []


def test_invest_mode_blocks_a_zero_amount(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    scope, calls = _open_scope(nicegui_client, wiz, invest_amount=0.0)
    with nicegui_client:
        assert scope._continue_button.enabled is False
        scope._continue()

    assert calls == []


def test_invest_mode_explains_but_does_not_block_an_amount_above_buying_power(nicegui_client):
    """The engine scales the plan down and the dry-run shows the result, which is
    more useful than refusing to compute it."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    scope, calls = _open_scope(nicegui_client, wiz, invest_amount=99_000.0)
    with nicegui_client:
        errors = _rendered_texts(scope._errors_container)
        assert scope._continue_button.enabled is True
        scope._continue()

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


def _income_table(root):
    from nicegui import ui
    return next(el for el in root.descendants() if isinstance(el, ui.table))


def test_the_income_panel_draws_every_event_with_what_is_left(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda amount: None)
        texts = _rendered_texts(nicegui_client.layout)
        rows = _income_table(nicegui_client.layout).rows

    # The rows live in a ``ui.table`` now, so they are DATA rather than labels; the
    # money is a raw float there and Quasar's ``format`` puts the separators in.
    assert {r["event_type"] for r in rows} == {"DIVIDEND", "DEPOSIT"}
    assert {r["event_date"] for r in rows} == {"2026-05-10", "2026-05-01"}
    assert {r["amount"] for r in rows} == {42.5, 5_000.0}
    # What is LEFT, not what arrived: the deposit is 5,000 with 1,500 open.
    assert {r["open_amount"] for r in rows} == {42.5, 1_500.0}
    assert any("1,542.50" in t for t in texts)


def test_the_income_panel_shows_a_dash_for_an_event_with_no_payer_symbol(nicegui_client):
    """A deposit has no symbol. Rendering a bare ``None`` in the column would be
    the string "None"."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        rows = _income_table(nicegui_client.layout).rows

    assert [r["symbol"] for r in rows] == ["AAPL", "-"]
    assert None not in {r["symbol"] for r in rows}


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
        svc.OUTCOME_UNACTIONABLE,
    }
    # ...and it must not be drawn in the grey the run uses for "nothing to do".
    assert wiz.OUTCOME_COLOURS[svc.OUTCOME_UNACTIONABLE] != \
        wiz.OUTCOME_COLOURS[svc.OUTCOME_SKIPPED]


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


def _unactionable_outcome():
    from ba2_trade_platform.core import portfolio_allocation_service as svc

    return svc.RowOutcome(
        symbol="AAPL", action=svc.ACTION_UNACTIONABLE,
        status=svc.OUTCOME_UNACTIONABLE, quantity=100.0, transaction_ids=[41],
        message=svc.UNACTIONABLE_OPTION_HOLDING_FMT.format(
            quantity=100.0, symbol="AAPL", ids="41"))


def test_the_outcome_table_does_not_congratulate_a_run_that_could_not_act(
        nicegui_client, notifications):
    """Nothing failed and nothing was refused, so the old chain fell straight
    through to the green "Allocation run submitted" -- for a run whose only row
    was a position it could not reach. The toast is what most people read before
    closing the dialog."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_outcomes([_unactionable_outcome()], run_id=31)

    assert notifications[-1][1] == 'warning'
    assert notifications[-1][1] != 'positive'
    assert 'could NOT be acted on' in notifications[-1][0]


def test_the_outcome_table_spells_out_why_a_row_could_not_be_acted_on(nicegui_client,
                                                                     notifications):
    """The Detail cell has to name the shares and the transaction ids: a status of
    "unactionable" on its own sends the operator hunting."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from ba2_trade_platform.core import portfolio_allocation_service as svc

    with nicegui_client:
        wiz.render_outcomes([_unactionable_outcome()], run_id=31)
        texts = _rendered_texts(nicegui_client.layout)

    assert svc.OUTCOME_UNACTIONABLE in texts
    assert any('100 share(s) of AAPL' in t for t in texts)
    assert any('transaction 41' in t for t in texts)
    assert not any('nothing to do' in t for t in texts)


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
# Continue WRITES, so the invest dialog has to say so.
#
# Until the write landed nothing on this screen touched the database and "Cancel
# really cancels" was a guarantee. It is not one any more: Continue persists the
# chosen label's symbol weights (that is what makes "load last" possible), so a
# dry run the user then abandons has already changed stored state.
# ---------------------------------------------------------------------------

def test_the_invest_dialog_says_that_continue_saves_the_weights(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_scope(nicegui_client, wiz, invest_amount=250.0)
    drawn = _marked_texts(nicegui_client.layout, wiz.MARKER_CONTINUE_SAVES)

    assert len(drawn) == 1
    assert 'SAVES' in drawn[0]
    assert 'Cancel abandons the run' in drawn[0]


def test_the_continue_note_promises_no_target_and_no_reserve(nicegui_client):
    """An invest run spends an explicit amount on one label, so the label's own
    percentage played no part and its reserve is a hard 0 -- writing either would
    record a choice the user never made."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    scope, calls = _open_scope(nicegui_client, wiz, invest_amount=250.0)
    with nicegui_client:
        scope._continue()

    assert calls[0]["unallocated_pct"] == 0.0
    assert wiz.CONTINUE_SAVES_NOTE.count('target') == 0


def test_the_invest_dialog_draws_no_reserve_box_at_all(nicegui_client):
    """The reserve is a share of the portfolio base; an invest run has a budget,
    not a base, so there is nothing for a reserve to be a share of."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    _open_scope(nicegui_client, wiz, invest_amount=1_000.0)
    texts = _rendered_texts(nicegui_client.layout)

    assert not any('Unallocated' in t for t in texts)


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
# The income panel is a REAL table now
#
# "This table is ugly, make it similar to others." It was hand-rolled out of
# ``ui.row()`` + ``ui.label()`` with hardcoded widths (``w-28``, ``w-24``, ...)
# while every other table on this page is a ``ui.table`` with
# ``.classes('w-full dark-pagination')`` -- so it had no shared header treatment,
# no alignment, no sorting and no pagination. Presentation only: the panel lists
# exactly the events it always did.
# ---------------------------------------------------------------------------

def test_the_income_panel_is_a_real_table(nicegui_client):
    from nicegui import ui
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        tables = [el for el in nicegui_client.layout.descendants()
                  if isinstance(el, ui.table)]

    assert len(tables) == 1


def test_the_income_table_is_styled_like_every_other_table_on_this_page(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        table = _income_table(nicegui_client.layout)

    assert 'w-full' in table._classes
    assert 'dark-pagination' in table._classes


def test_the_income_table_has_the_five_columns_it_always_showed(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        table = _income_table(nicegui_client.layout)

    assert [c['label'] for c in table.columns] == ['Date', 'Type', 'Symbol',
                                                   'Amount', 'Open']


def test_the_income_tables_money_columns_are_right_aligned(nicegui_client):
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        table = _income_table(nicegui_client.layout)

    by_name = {c['name']: c for c in table.columns}
    assert by_name['amount']['align'] == 'right'
    assert by_name['open_amount']['align'] == 'right'
    assert by_name['event_date']['align'] == 'left'


def test_the_income_table_sorts_on_the_RAW_number_and_formats_for_display(
        nicegui_client):
    """Pre-formatting "5,000.00" into the row would sort it as a STRING, putting
    5,000.00 before 42.50. The number stays a number and Quasar's ``format``
    renders it -- which is also how ``{:,.2f}`` survives the conversion."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        table = _income_table(nicegui_client.layout)

    by_name = {c['name']: c for c in table.columns}
    assert by_name['amount']['sortable'] is True
    assert table.rows[0]['amount'] == 42.5
    assert isinstance(table.rows[1]['amount'], float)
    assert 'minimumFractionDigits: 2' in by_name['amount'][':format']
    assert 'minimumFractionDigits: 2' in by_name['open_amount'][':format']


def test_the_income_table_lists_exactly_the_events_it_was_given(nicegui_client):
    """Presentation only -- the panel must not start filtering.

    A FULLY CONSUMED event is in the fixture on purpose: it is the one a "only show
    what is still open" filter would swallow, and it is exactly the row the user
    looks for when asking where last week's dividend went. A mutation adding
    ``if event['open_amount']`` to the comprehension survived a fixture without one.
    """
    from datetime import date
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    events = _income_events() + [
        {"id": 3, "external_id": "div-2", "event_date": date(2026, 4, 28),
         "event_type": "DIVIDEND", "symbol": "MSFT", "amount": 18.0,
         "open_amount": 0.0},
    ]
    with nicegui_client:
        wiz.render_income_panel(events, 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        table = _income_table(nicegui_client.layout)

    assert [r['external_id'] for r in table.rows] == ['div-1', 'csd-1', 'div-2']
    assert [r['open_amount'] for r in table.rows] == [42.5, 1_500.0, 0.0]


def test_the_income_table_still_shows_a_dash_for_an_event_with_no_payer_symbol(
        nicegui_client):
    """A deposit has no symbol, and a bare ``None`` in a table cell renders empty
    -- which reads as "we do not know" rather than "there is none"."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        table = _income_table(nicegui_client.layout)

    assert [r['symbol'] for r in table.rows] == ['AAPL', '-']


def test_the_income_table_renders_the_date_as_text_not_a_date_object(nicegui_client):
    """``date`` is not JSON-serialisable, and NiceGUI sends the rows to the browser
    as JSON -- the hand-rolled version got away with it because it called ``str()``."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        table = _income_table(nicegui_client.layout)

    assert [r['event_date'] for r in table.rows] == ['2026-05-10', '2026-05-01']
    assert all(isinstance(r['event_date'], str) for r in table.rows)


def test_the_income_panel_header_row_is_untouched(nicegui_client):
    """The title, the green unallocated total, Refresh and Invest stay exactly as
    they were: this change was about the table underneath them."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        texts = _rendered_texts(nicegui_client.layout)

    assert 'Income (last 30 days)' in texts
    assert any('Unallocated: 1,542.50' in t for t in texts)
    assert 'Refresh' in texts and 'Invest' in texts


def test_an_empty_income_panel_draws_no_table_at_all(nicegui_client):
    from nicegui import ui
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel([], 0.0, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        tables = [el for el in nicegui_client.layout.descendants()
                  if isinstance(el, ui.table)]
        texts = _rendered_texts(nicegui_client.layout)

    assert tables == []
    assert any('No deposits or dividends' in t for t in texts)


def test_the_income_table_is_keyed_on_the_brokers_idempotency_key(nicegui_client):
    """``row_key`` is Quasar's ``:key``. Keyed on anything that repeats -- the type,
    the date -- rows collapse into one another on a re-render."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    with nicegui_client:
        wiz.render_income_panel(_income_events(), 1_542.5, working_note=None,
                                on_sync=lambda: None, on_invest=lambda a: None)
        table = _income_table(nicegui_client.layout)

    assert table.row_key == 'external_id'
    keys = [r[table.row_key] for r in table.rows]
    assert len(set(keys)) == len(keys)


# ---------------------------------------------------------------------------
# THE MIGRATION: the wizard is a REVIEW-AND-COMMIT gate and nothing else
#
# "Rebalance - set targets" is gone. Every control that expresses INTENT -- the
# label targets, the symbol shares, the cash reserve, and the six buttons over
# them -- lives on the Portfolio Allocation page now. What is left here is what
# COMMITS: the resolved order list, cost versus value, which instruments are
# leveraged, the precheck warnings, one Submit, and exactly one execution control.
#
# The tests below are the guard that step 1 does not come back. A second place to
# type a target is a second answer to "what am I aiming at", and the two screens
# derived every one of those figures independently.
# ---------------------------------------------------------------------------

def _wiz():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    return wiz


def test_the_wizard_no_longer_offers_a_target_setting_STEP_at_all():
    """Structural, because a renderer nobody calls is one somebody will call."""
    wiz = _wiz()
    assert not hasattr(wiz, 'AllocationSteps')
    assert not hasattr(wiz, 'open_allocation_steps')


def test_the_wizard_draws_no_label_target_and_no_symbol_share_box():
    """The markers are the boxes. Their absence is the absence of the editor."""
    wiz = _wiz()
    for gone in ('MARKER_LABEL_PCT', 'MARKER_SYMBOL_PCT', 'MARKER_LABEL_TOTAL',
                 'MARKER_UNALLOCATED', 'MARKER_UNALLOCATED_PCT'):
        assert not hasattr(wiz, gone), gone


def test_the_wizard_carries_none_of_the_six_migrated_buttons():
    wiz = _wiz()
    for gone in ('MARKER_EVEN_SPLIT', 'MARKER_EVEN_SPLIT_SYMBOLS',
                 'MARKER_LOAD_LAST', 'MARKER_LOAD_LAST_SYMBOLS',
                 'MARKER_FILL_REST_SYMBOLS', 'MARKER_WIPE_SYMBOLS'):
        assert not hasattr(wiz, gone), gone


def test_the_wizard_imports_none_of_the_engines_target_editing_helpers():
    """An import is a dependency, and a dependency is an invitation. If the wizard
    still reached for ``even_split_targets`` it would be one edit away from having
    a target editor again."""
    import inspect as _inspect
    source = _inspect.getsource(_wiz())
    for gone in ('even_split_targets', 'load_previous_targets',
                 'even_split_symbol_weights', 'fill_remaining_symbol_weights',
                 'load_previous_symbol_weights', 'wipe_symbol_weights',
                 'has_previous_targets', 'has_previous_symbol_weights',
                 'can_even_split_symbols', 'can_fill_remaining_symbol_weights',
                 'can_wipe_symbol_weights', 'steps_validation_messages'):
        assert gone not in source, gone


def test_the_wizard_writes_no_target_anywhere():
    """``save_allocation_targets`` / ``set_managed_label`` / ``set_symbol_weight``
    are the three target writers. None of them belongs to a commit gate."""
    import inspect as _inspect
    source = _inspect.getsource(_wiz())
    for gone in ('save_allocation_targets', 'set_managed_label',
                 'set_symbol_weight', 'set_allocation_config'):
        assert gone not in source, gone


def test_the_dry_run_keeps_EXACTLY_ONE_execution_control(nicegui_client):
    """``allow fractional shares`` stays because it changes WHICH ORDERS are
    produced rather than what is being aimed at. Nothing else on this dialog may
    be an input."""
    from nicegui import ui

    wiz = _wiz()
    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _mixed_plan(), market=_open_market(),
                                      on_refresh=lambda f: (_mixed_plan(),
                                                            _open_market()),
                                      on_submit=lambda p: None)
        wizard.open()
    switches = [el for el in nicegui_client.layout.descendants()
                if isinstance(el, ui.switch)]
    inputs = [el for el in nicegui_client.layout.descendants()
              if isinstance(el, (ui.number, ui.slider, ui.select))]

    assert [s.text for s in switches] == ['Allow fractional shares']
    assert inputs == []
    assert wizard.allow_fractional is True


def test_a_broker_that_cannot_split_shares_disables_the_gates_fractional_switch(
        nicegui_client):
    """The veto used to live on the step-3 panel of the dialog that is gone.

    Offering a toggle the broker cannot honour produces a plan sized on a grid that
    does not exist: the engine silently falls back to whole shares, so the user
    would see quantities they never asked for. DISABLED rather than hidden, and the
    reason is said out loud -- a control that vanishes is one the user cannot learn
    exists.
    """
    from nicegui import ui

    wiz = _wiz()
    base = _base()
    base.supports_fractional = False
    with nicegui_client:
        wiz.AllocationWizard(base, _mixed_plan(), market=_open_market(),
                             on_refresh=lambda f: (_mixed_plan(), _open_market()),
                             on_submit=lambda p: None).open()
    switch = [el for el in nicegui_client.layout.descendants()
              if isinstance(el, ui.switch)][0]

    assert switch.enabled is False
    assert wiz.NO_FRACTIONAL_SUPPORT_NOTE in _rendered_texts(nicegui_client.layout)


def test_a_broker_that_CAN_split_shares_leaves_the_gates_switch_alone(nicegui_client):
    from nicegui import ui

    wiz = _wiz()
    base = _base()
    base.supports_fractional = True
    with nicegui_client:
        wiz.AllocationWizard(base, _mixed_plan(), market=_open_market(),
                             on_refresh=lambda f: (_mixed_plan(), _open_market()),
                             on_submit=lambda p: None).open()
    switch = [el for el in nicegui_client.layout.descendants()
              if isinstance(el, ui.switch)][0]

    assert switch.enabled is True
    assert wiz.NO_FRACTIONAL_SUPPORT_NOTE not in _rendered_texts(nicegui_client.layout)
