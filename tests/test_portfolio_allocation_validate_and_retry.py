"""The two live halves of "test the plan first, then continue without the bad ones":

  * ``portfolio_allocation_service.validate_plan`` -- re-reads the broker's
    per-symbol facts, runs its order preview WHERE ONE EXISTS, and reports what
    would be refused. Sends nothing.
  * the wizard's Validate button and its "un-tick the flagged ones" follow-up,
    and the results table's "retry the N that failed".

The asymmetry between brokers is the thing to keep in view while reading these:
TastyTrade answers for itself through ``preview_order_impact``; Alpaca publishes
no order-preview endpoint at all, so on the live account ``prechecked`` is always
0 and every finding is a local one. The UI has to SAY that rather than let a
clean result read as a broker sign-off.
"""
import pytest

from ba2_trade_platform.core import portfolio_allocation_service as svc
from ba2_trade_platform.core.account_types import MarginInfo, OrderImpact
from ba2_trade_platform.core.portfolio_allocation import (
    AllocationPlan, AllocationRow,
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


class _Account:
    """The two seams ``validate_plan`` uses, and nothing else.

    ``preview_order_impact`` is ABSENT unless a test asks for it, which is how a
    broker without one presents itself (``getattr(account, ..., None)``) -- the
    Alpaca shape.
    """

    def __init__(self, account_id=1, margin=None, impacts=None,
                 has_preview=False, margin_raises=False, preview_raises=False):
        self.id = account_id
        self._margin = margin or {}
        self._impacts = impacts or {}
        self._margin_raises = margin_raises
        self._preview_raises = preview_raises
        self.previewed = []
        if has_preview:
            self.preview_order_impact = self._preview

    def get_symbol_margin_info(self, symbols):
        if self._margin_raises:
            raise RuntimeError("assets endpoint down")
        return {s: self._margin[s] for s in symbols if s in self._margin}

    def _preview(self, trading_order, is_closing_order=False):
        self.previewed.append((trading_order.symbol, trading_order.quantity,
                               is_closing_order))
        if self._preview_raises:
            raise RuntimeError("preview exploded")
        return self._impacts.get(trading_order.symbol)


def _row(symbol="AAA", *, quantity=10.0, price=100.0, side=OrderDirection.BUY):
    return AllocationRow(symbol=symbol, price=price, delta_quantity=quantity,
                         side=side, estimated_value=abs(quantity) * price,
                         bp_cost=abs(quantity) * price, bp_factor=1.0)


def _plan(*rows, **kwargs):
    kwargs.setdefault('available_buying_power', 100_000.0)
    return AllocationPlan(rows=list(rows), **kwargs)


# -- validate_plan: the service layer ----------------------------------------

def test_validate_plan_is_clean_when_the_broker_describes_a_tradable_symbol():
    account = _Account(margin={"AAA": MarginInfo(symbol="AAA", bp_factor=1.0,
                                                 tradable=True, fractionable=True)})

    report = svc.validate_plan(account, _plan(_row("AAA")))

    assert report['findings'] == []
    assert report['symbols'] == []
    assert report['budget'] is None


def test_validate_plan_names_the_symbols_to_un_tick():
    """``symbols`` is what the wizard's drop button acts on, so it must be the
    DISTINCT set, not one entry per finding."""
    account = _Account(margin={
        "DEAD": MarginInfo(symbol="DEAD", bp_factor=1.0, tradable=False,
                           fractionable=False),
        "AAA": MarginInfo(symbol="AAA", bp_factor=1.0, tradable=True,
                          fractionable=True)})

    report = svc.validate_plan(account, _plan(_row("DEAD", quantity=1.5), _row("AAA")))

    assert report['symbols'] == ["DEAD"]
    assert len(report['findings']) == 2          # not tradable AND fractional


def test_validate_plan_sends_nothing_and_writes_nothing():
    """The whole promise of the button. Only the two read seams are touched, and
    a broker with no preview endpoint is not asked for one."""
    account = _Account(margin={"AAA": MarginInfo(symbol="AAA", bp_factor=1.0)})

    svc.validate_plan(account, _plan(_row("AAA")))

    assert account.previewed == []
    assert not hasattr(account, 'submitted')


def test_validate_plan_reports_zero_prechecked_on_a_broker_without_a_preview():
    """The Alpaca shape, and the number the UI needs to avoid implying a broker
    sign-off: no endpoint means nothing was broker-tested, however clean it looks."""
    account = _Account(margin={"AAA": MarginInfo(symbol="AAA", bp_factor=1.0,
                                                 tradable=True)})

    report = svc.validate_plan(account, _plan(_row("AAA")))

    assert report['prechecked'] == 0
    assert report['buy_rows'] == 1


def test_validate_plan_runs_the_brokers_own_preview_when_it_has_one():
    """The TastyTrade shape. The preview is a DRY RUN and is passed
    ``is_closing_order=False`` explicitly, matching what submission would send."""
    impact = OrderImpact(symbol="AAA", change_in_buying_power=-1000.0, accepted=True)
    account = _Account(has_preview=True, impacts={"AAA": impact},
                       margin={"AAA": MarginInfo(symbol="AAA", bp_factor=1.0)})

    report = svc.validate_plan(account, _plan(_row("AAA")))

    assert account.previewed == [("AAA", 10.0, False)]
    assert report['prechecked'] == 1
    assert report['findings'] == []


def test_validate_plan_surfaces_a_broker_refusal():
    impact = OrderImpact(symbol="AAA", change_in_buying_power=-1000.0,
                         accepted=False, errors=["not enough buying power"])
    account = _Account(has_preview=True, impacts={"AAA": impact})

    report = svc.validate_plan(account, _plan(_row("AAA")))

    assert report['symbols'] == ["AAA"]
    assert "not enough buying power" in report['findings'][0][1]


def test_validate_plan_previews_buys_only():
    """A sell preview cannot change the answer and a flaky one refusing a close
    is how a position the user asked to exit quietly stays open -- the same rule
    ``precheck_plan`` states at length."""
    account = _Account(has_preview=True)

    svc.validate_plan(account, _plan(_row("AAA"),
                                     _row("SELLME", quantity=-5.0,
                                          side=OrderDirection.SELL)))

    assert [s for s, _q, _c in account.previewed] == ["AAA"]


def test_validate_plan_survives_a_broker_that_cannot_describe_its_symbols():
    """A failed margin read is "nobody said", not a wall of false refusals."""
    account = _Account(margin_raises=True)

    report = svc.validate_plan(account, _plan(_row("AAA")))

    assert report['findings'] == []


def test_validate_plan_survives_a_preview_that_raises():
    """One symbol's exploding preview must not take the whole validation down --
    it simply goes unchecked, which is the same state Alpaca is in for every row."""
    account = _Account(has_preview=True, preview_raises=True)

    report = svc.validate_plan(account, _plan(_row("AAA")))

    assert report['prechecked'] == 0
    assert report['findings'] == []


def test_validate_plan_carries_the_budget_advisory():
    account = _Account()
    plan = _plan(_row("AAA"), required_buying_power=9_000.0,
                 available_buying_power=1_000.0)

    report = svc.validate_plan(account, plan)

    assert report['budget'] is not None
    assert "buying power" in report['budget']


# -- the wizard: Validate, then drop -----------------------------------------

def _wiz():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    return wiz


def _base():
    from ba2_trade_platform.core.portfolio_allocation import (
        BaseSnapshot, VALUATION_MODE_MARKET,
    )
    return BaseSnapshot(available_buying_power=10_000.0, managed_value=0.0,
                        base_notional=10_000.0, default_bp_factor=1.0,
                        valuation_mode=VALUATION_MODE_MARKET, cash=10_000.0)


def _open_market():
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        MARKET_GATE_OPEN, MarketGateResult,
    )
    return MarketGateResult(allowed=True, reason_code=MARKET_GATE_OPEN, message="")


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page

    client = Client(nicegui_page('/test-allocation-validate'), request=None)
    try:
        yield client
    finally:
        Client.instances.pop(client.id, None)


def _texts(root):
    return [d.text for d in root.descendants() if getattr(d, 'text', None)]


def _marked(root, marker):
    return [d for d in root.descendants() if marker in getattr(d, '_markers', [])]


def _two_row_plan():
    return _plan(_row("AAA"), _row("DEAD", quantity=5.0))


def _open_wizard(nicegui_client, report, submitted=None, plan=None):
    wiz = _wiz()
    wizard = wiz.AllocationWizard(
        _base(), plan if plan is not None else _two_row_plan(),
        market=_open_market(),
        on_refresh=lambda f: pytest.fail("refresh not expected"),
        on_submit=(submitted.append if submitted is not None else (lambda p: None)),
        on_validate=lambda selected: report)
    wizard.open()
    return wiz, wizard


def test_the_dry_run_offers_a_validate_button_when_it_has_a_validator(nicegui_client):
    with nicegui_client:
        wiz, _wizard = _open_wizard(nicegui_client, {'findings': [], 'symbols': [],
                                                     'budget': None, 'prechecked': 0,
                                                     'buy_rows': 2})
        buttons = _marked(nicegui_client.layout, wiz.MARKER_VALIDATE_BUTTON)

    assert len(buttons) == 1


def test_a_wizard_with_no_validator_draws_no_validate_button(nicegui_client):
    """Dead controls are worse than absent ones."""
    wiz = _wiz()
    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _two_row_plan(), market=_open_market(),
                                      on_refresh=lambda f: None,
                                      on_submit=lambda p: None)
        wizard.open()
        buttons = _marked(nicegui_client.layout, wiz.MARKER_VALIDATE_BUTTON)

    assert buttons == []


# -- off the event loop ------------------------------------------------------
#
# "Clicking validate here is hanging ui and eventually close the dry run dialog".
# ``on_validate`` re-reads margin for every symbol in the plan -- on Alpaca a REST
# round trip each -- and NiceGUI runs a sync click handler directly on the event
# loop, so nothing reaches the browser until it returns. On a 40-row plan that
# outlasted the websocket heartbeat, and the reconnect took the dialog with it.


def test_the_broker_call_does_not_run_on_the_EVENT_LOOP(nicegui_client):
    """The fix, pinned where it can be seen: the callback runs on a worker thread.

    Asserting the handler is merely ``async`` would not catch the regression --
    an async handler that awaits nothing blocks the loop exactly as hard.
    """
    import threading
    seen = {}

    def _slow_broker_call(_selected):
        seen['main_thread'] = threading.current_thread() is threading.main_thread()
        return {'findings': [], 'symbols': [], 'budget': None, 'prechecked': 0,
                'buy_rows': 2}

    with nicegui_client:
        wiz = _wiz()
        wizard = wiz.AllocationWizard(
            _base(), _two_row_plan(), market=_open_market(),
            on_refresh=lambda f: pytest.fail("refresh not expected"),
            on_submit=lambda p: None, on_validate=_slow_broker_call)
        wizard.open()
        _run(wizard._validate())

    assert seen['main_thread'] is False


def test_the_RESOLVE_does_not_run_on_the_event_loop_either(nicegui_client):
    """Same defect, same dialog: Refresh re-reads positions and quotes and solves
    the whole plan again."""
    import threading
    seen = {}

    def _slow_resolve(_allow_fractional):
        seen['main_thread'] = threading.current_thread() is threading.main_thread()
        return _two_row_plan(), _open_market()

    with nicegui_client:
        wiz = _wiz()
        wizard = wiz.AllocationWizard(
            _base(), _two_row_plan(), market=_open_market(),
            on_refresh=_slow_resolve, on_submit=lambda p: None)
        wizard.open()
        _run(wizard._refresh(False))

    assert seen['main_thread'] is False


def test_a_second_click_while_a_validation_is_in_flight_is_ignored(nicegui_client):
    """Running the broker call off the loop keeps the dialog RESPONSIVE, which
    means the button is still there to be pressed again -- and two concurrent
    margin sweeps would race their verdicts into the same container."""
    calls = []

    def _on_validate(_selected):
        calls.append(1)
        return {'findings': [], 'symbols': [], 'budget': None, 'prechecked': 0,
                'buy_rows': 2}

    with nicegui_client:
        wiz = _wiz()
        wizard = wiz.AllocationWizard(
            _base(), _two_row_plan(), market=_open_market(),
            on_refresh=lambda f: pytest.fail("refresh not expected"),
            on_submit=lambda p: None, on_validate=_on_validate)
        wizard.open()
        wizard._validating = True          # as the in-flight press leaves it
        _run(wizard._validate())

    assert calls == []


def test_a_clean_validation_says_so_without_promising_a_fill(nicegui_client):
    with nicegui_client:
        wiz, wizard = _open_wizard(nicegui_client,
                                   {'findings': [], 'symbols': [], 'budget': None,
                                    'prechecked': 0, 'buy_rows': 2})
        _run(wizard._validate())
        result = _marked(nicegui_client.layout, wiz.MARKER_VALIDATION_RESULT)
        texts = ' '.join(_texts(nicegui_client.layout))

    assert len(result) == 1
    # The honest caveat, and the broker-was-not-asked line.
    assert 'No check can promise a fill' in texts
    assert wiz.VALIDATION_NO_PRECHECK in texts


def test_a_validation_that_found_something_lists_every_finding(nicegui_client):
    findings = [("DEAD", "DEAD: the broker does not accept orders for this symbol"),
                ("DEAD", "DEAD: 5 is a fractional quantity and the broker does not "
                         "split this symbol")]
    with nicegui_client:
        wiz, wizard = _open_wizard(nicegui_client,
                                   {'findings': findings, 'symbols': ["DEAD"],
                                    'budget': None, 'prechecked': 0, 'buy_rows': 2})
        _run(wizard._validate())
        drawn = _marked(nicegui_client.layout, wiz.MARKER_VALIDATION_FINDING)

    assert len(drawn) == 2


def test_the_drop_button_unticks_exactly_the_flagged_symbols(nicegui_client):
    submitted = []
    with nicegui_client:
        wiz, wizard = _open_wizard(nicegui_client,
                                   {'findings': [("DEAD", "DEAD: no")],
                                    'symbols': ["DEAD"], 'budget': None,
                                    'prechecked': 0, 'buy_rows': 2},
                                   submitted=submitted)
        assert wizard.selected == {"AAA", "DEAD"}
        _run(wizard._validate())
        drop = _marked(nicegui_client.layout, wiz.MARKER_VALIDATION_DROP)
        assert len(drop) == 1
        wizard._drop(["DEAD"])
        wizard._submit()

    assert wizard.selected == {"AAA"}
    assert [r.symbol for r in submitted[0].rows] == ["AAA"]


def test_dropping_clears_the_verdict_it_came_from(nicegui_client):
    """The panel names orders that are no longer going to be sent the moment they
    are un-ticked."""
    with nicegui_client:
        wiz, wizard = _open_wizard(nicegui_client,
                                   {'findings': [("DEAD", "DEAD: no")],
                                    'symbols': ["DEAD"], 'budget': None,
                                    'prechecked': 0, 'buy_rows': 2})
        _run(wizard._validate())
        wizard._drop(["DEAD"])
        result = _marked(nicegui_client.layout, wiz.MARKER_VALIDATION_RESULT)

    assert result == []


def test_a_clean_validation_offers_no_drop_button(nicegui_client):
    with nicegui_client:
        wiz, wizard = _open_wizard(nicegui_client,
                                   {'findings': [], 'symbols': [], 'budget': None,
                                    'prechecked': 2, 'buy_rows': 2})
        _run(wizard._validate())
        drop = _marked(nicegui_client.layout, wiz.MARKER_VALIDATION_DROP)

    assert drop == []


def test_validation_only_ever_tests_the_TICKED_rows(nicegui_client):
    """Validating an un-ticked row would report a problem with an order that is
    not going to be sent, and the drop button would have nothing to drop."""
    seen = {}

    wiz = _wiz()
    with nicegui_client:
        wizard = wiz.AllocationWizard(
            _base(), _two_row_plan(), market=_open_market(),
            on_refresh=lambda f: None, on_submit=lambda p: None,
            on_validate=lambda selected: seen.setdefault(
                'symbols', [r.symbol for r in selected.rows]) and {} or {
                    'findings': [], 'symbols': [], 'budget': None,
                    'prechecked': 0, 'buy_rows': 1})
        wizard.open()
        wizard._toggle("DEAD", False)
        _run(wizard._validate())

    assert seen['symbols'] == ["AAA"]


def test_a_validation_that_raises_is_reported_and_changes_nothing(nicegui_client):
    wiz = _wiz()

    def _boom(_plan):
        raise RuntimeError("broker unreachable")

    with nicegui_client:
        wizard = wiz.AllocationWizard(_base(), _two_row_plan(), market=_open_market(),
                                      on_refresh=lambda f: None,
                                      on_submit=lambda p: None, on_validate=_boom)
        wizard.open()
        _run(wizard._validate())
        result = _marked(nicegui_client.layout, wiz.MARKER_VALIDATION_RESULT)

    assert result == []
    assert wizard.selected == {"AAA", "DEAD"}


def test_the_budget_advisory_is_drawn_even_with_no_row_findings(nicegui_client):
    """No row can be un-ticked to fix it, so it must not hide behind an empty
    findings list."""
    with nicegui_client:
        wiz, wizard = _open_wizard(
            nicegui_client,
            {'findings': [], 'symbols': [], 'prechecked': 0, 'buy_rows': 2,
             'budget': 'the selected orders need $9,000.00 of buying power'})
        _run(wizard._validate())
        drawn = _marked(nicegui_client.layout, wiz.MARKER_VALIDATION_FINDING)

    assert len(drawn) == 1


def test_a_refresh_clears_a_stale_verdict(nicegui_client):
    """A verdict describes ONE exact set of orders; a re-solve makes it a
    statement about a plan that no longer exists."""
    wiz = _wiz()
    with nicegui_client:
        wizard = wiz.AllocationWizard(
            _base(), _two_row_plan(), market=_open_market(),
            on_refresh=lambda f: (_two_row_plan(), _open_market()),
            on_submit=lambda p: None,
            on_validate=lambda p: {'findings': [("DEAD", "DEAD: no")],
                                   'symbols': ["DEAD"], 'budget': None,
                                   'prechecked': 0, 'buy_rows': 2})
        wizard.open()
        _run(wizard._validate())
        assert _marked(nicegui_client.layout, wiz.MARKER_VALIDATION_RESULT)
        _run(wizard._refresh(False))
        result = _marked(nicegui_client.layout, wiz.MARKER_VALIDATION_RESULT)

    assert result == []


# -- the results table: retry just the failures ------------------------------

def _outcome(symbol, status):
    return svc.RowOutcome(symbol=symbol, action='new', status=status, quantity=1.0)


def test_retryable_outcomes_takes_the_failures_only():
    """A wash-trade lock is still armed and TradeManager re-sends it -- retrying
    would queue a SECOND order for the symbol and both would fill. An
    unactionable row cannot be helped by a retry, and a partial may still be
    working at the broker."""
    wiz = _wiz()
    outcomes = [_outcome("A", svc.OUTCOME_FAILED),
                _outcome("B", svc.OUTCOME_SUBMITTED),
                _outcome("C", svc.OUTCOME_WASHTRADE_LOCKED),
                _outcome("D", svc.OUTCOME_UNACTIONABLE),
                _outcome("E", svc.OUTCOME_PARTIAL),
                _outcome("F", svc.OUTCOME_SKIPPED)]

    assert wiz.retryable_outcomes(outcomes) == ["A"]


def test_retryable_outcomes_de_duplicates_a_symbol_that_failed_twice():
    wiz = _wiz()
    outcomes = [_outcome("A", svc.OUTCOME_FAILED), _outcome("A", svc.OUTCOME_FAILED)]
    assert wiz.retryable_outcomes(outcomes) == ["A"]


def test_the_results_table_offers_a_retry_when_something_failed(nicegui_client):
    wiz = _wiz()
    retried = []
    with nicegui_client:
        wiz.render_outcomes([_outcome("A", svc.OUTCOME_FAILED),
                             _outcome("B", svc.OUTCOME_SUBMITTED)],
                            run_id=7, on_retry=retried.append)
        buttons = _marked(nicegui_client.layout, wiz.MARKER_OUTCOME_RETRY)

    assert len(buttons) == 1


def test_the_retry_hands_back_exactly_the_failed_symbols(nicegui_client):
    wiz = _wiz()
    retried = []
    with nicegui_client:
        wiz.render_outcomes([_outcome("A", svc.OUTCOME_FAILED),
                             _outcome("B", svc.OUTCOME_SUBMITTED)],
                            run_id=7, on_retry=retried.append)
        button = _marked(nicegui_client.layout, wiz.MARKER_OUTCOME_RETRY)[0]
        for listener in button._event_listeners.values():
            if listener.type.split('.')[0] == 'click':
                # ``Button.on_click`` wraps the callback in a lambda taking the
                # event, so the handler is invoked the way NiceGUI would.
                listener.handler(None)
                break

    assert retried == [["A"]]


def test_a_run_with_nothing_to_retry_draws_no_retry_button(nicegui_client):
    wiz = _wiz()
    with nicegui_client:
        wiz.render_outcomes([_outcome("A", svc.OUTCOME_SUBMITTED)], run_id=7,
                            on_retry=lambda symbols: None)
        buttons = _marked(nicegui_client.layout, wiz.MARKER_OUTCOME_RETRY)

    assert buttons == []


def test_no_retry_button_without_a_retry_callback(nicegui_client):
    """The caller that cannot re-solve must not be offered a button that does
    nothing."""
    wiz = _wiz()
    with nicegui_client:
        wiz.render_outcomes([_outcome("A", svc.OUTCOME_FAILED)], run_id=7)
        buttons = _marked(nicegui_client.layout, wiz.MARKER_OUTCOME_RETRY)

    assert buttons == []
