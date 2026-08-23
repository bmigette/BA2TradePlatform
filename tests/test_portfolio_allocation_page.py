"""Behavioural tests for the Portfolio Allocation PAGE module.

``tests/test_portfolio_allocation_route.py`` is structural (does the route exist,
does ``content`` exist) and ``tests/test_portfolio_allocation_view.py`` covers the
pure view-model. Neither exercised a single line of
``ui/pages/portfolio_allocation.py``: eight of eight page mutations survived a
fully green suite, and each of them re-armed a named, previously-shipped bug --
the ``manual_trading_enabled`` gate, the expert gate, the ``get_positions() or []``
tri-state incident (five times now) and the c63d34c comment-zeroing bug. This file
is the missing half.

Three notes on how it runs:

1. IMPORTING THE PAGE. ``ui/pages/__init__.py`` pulls every page and through them
   the expert/LLM stack -- ~6s, once, for this whole module. That is why the pure
   logic lives in ``ui/utils/`` and why the route test refuses to import this; here
   the cost is paid deliberately, because the point is to test the page.
2. RENDERING. A bare ``nicegui.Client`` gives every ``ui.*`` call a slot stack, so
   the dialogs and the label body can be drawn and then read back out of the
   element tree without a browser.
3. ASYNC. The handlers are coroutines; they are driven with ``asyncio.run`` rather
   than a plugin, and ``asyncio.to_thread`` is run INLINE here — see
   ``run_to_thread_inline`` for why a worker thread cannot see the test database.

Never ``caplog``: ``logger.py`` sets ``propagate = False``, so the root handler
caplog installs never sees a record. Patch the module's own ``logger`` instead.
"""
import asyncio

import pytest

from ba2_trade_platform.core.portfolio_allocation import (
    VALUATION_MODE_COST, VALUATION_MODE_MARKET, PositionFetchFailed,
)
from ba2_trade_platform.core.portfolio_allocation_store import (
    get_managed_labels, get_symbol_rows, get_symbol_weights, remove_managed_label,
    replace_managed_labels, set_managed_label, set_symbol_weight,
)
from ba2_trade_platform.core.utils import add_label_to_instruments
from ba2_trade_platform.ui.pages import portfolio_allocation as page
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    GATE_HAS_EXPERTS, GATE_NOT_MANUAL, GATE_NO_ACCOUNT, GATE_OK, ManagedLabel,
    build_label_views,
)
from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_expert_instance


# ---------------------------------------------------------------------------
# Doubles and helpers
# ---------------------------------------------------------------------------

class _Account(MockAccount):
    """A MockAccount with a settings dict and a controllable position book.

    ``_settings_cache`` is the real ``ExtendableSettingsInterface`` cache, so
    ``get_setting_with_interface_default`` runs its REAL implementation over it --
    including the ``"None"``-string rule that separates it from ``settings.get()``.
    """

    def __init__(self, account_id, settings=None, positions=None, prices=None,
                 known_symbols=None):
        super().__init__(account_id)
        self._settings_cache = dict(settings or {})
        self._positions = positions
        self._prices = dict(prices or {})
        self._known_symbols = known_symbols
        self.quote_requests = []

    def get_positions(self):
        return self._positions

    def get_instrument_current_price(self, symbol_or_symbols):
        self.quote_requests.append(symbol_or_symbols)
        if isinstance(symbol_or_symbols, list):
            return {s: self._prices.get(s) for s in symbol_or_symbols}
        return self._prices.get(symbol_or_symbols)

    def symbols_exist(self, symbols):
        if self._known_symbols is None:
            return {s: True for s in symbols}
        return {s: s in self._known_symbols for s in symbols}


def _use_account(monkeypatch, account):
    """Make the page's lazily-imported account factory return ``account``."""
    import ba2_trade_platform.core.utils as core_utils
    monkeypatch.setattr(core_utils, 'get_account_instance_from_id',
                        lambda account_id: account)
    return account


def _capture_notifications(monkeypatch):
    """Collect ``ui.notify`` calls made by the page. Returns the growing list."""
    from nicegui import ui as nicegui_ui
    sent = []
    monkeypatch.setattr(nicegui_ui, 'notify',
                        lambda message, **kw: sent.append((str(message), kw.get('type'))))
    return sent


def _capture_errors(monkeypatch):
    """Collect ``logger.error`` messages from the page module. NOT caplog."""
    messages = []
    monkeypatch.setattr(page.logger, 'error',
                        lambda msg, *a, **k: messages.append(str(msg)))
    return messages


@pytest.fixture(autouse=True)
def run_to_thread_inline(monkeypatch):
    """Run ``asyncio.to_thread`` bodies on the calling thread, for THIS module only.

    The test engine is ``sqlite:///:memory:``, and SQLAlchemy gives a memory SQLite
    a ``SingletonThreadPool`` — one connection PER THREAD. A worker thread therefore
    opens its own, brand-new, empty in-memory database and every query fails with
    'no such table'. That is a property of the fixture, not of the page: production
    runs against a file, where the offload is exactly right. Running the bodies
    inline keeps the page's real ``await`` structure under test while letting the
    DB work see the rows the test just wrote.
    """
    async def _inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, 'to_thread', _inline)


@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-portfolio-allocation'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _texts(root):
    """Every text fragment rendered under ``root``, in document order."""
    return [el._text for el in root.descendants(include_self=True) if el._text]


def _run_in_client(client, coro_factory):
    """Await a page coroutine with the client's slot stack ACTIVE.

    NiceGUI keys its slot stack on the asyncio task, so entering ``with client:``
    on the caller's stack and then ``asyncio.run(...)`` puts the drawing inside a
    task that has no slot at all. The context has to be entered from within the
    coroutine that draws.
    """
    async def _run():
        with client:
            await coro_factory()

    asyncio.run(_run())


def _pos(symbol, qty, cost_basis, market_value=None, side='BUY'):
    return {'symbol': symbol, 'qty': qty, 'cost_basis': cost_basis,
            'market_value': market_value, 'side': side}


@pytest.fixture
def account_id():
    return create_account_definition(name='Manual', provider='MockAccount').id


# ---------------------------------------------------------------------------
# _load_gate -- Task 56's gate, which had no test at all
# ---------------------------------------------------------------------------

def test_load_gate_allows_a_manual_account_with_no_enabled_experts(monkeypatch, account_id):
    _use_account(monkeypatch, _Account(account_id, {'manual_trading_enabled': True}))
    gate = page._load_gate(account_id)
    assert gate.allowed is True
    assert gate.reason_code == GATE_OK


def test_load_gate_blocks_an_account_that_is_not_flagged_manual(monkeypatch, account_id):
    """Mutation ``manual = True`` kills the whole of Task 56; this is what it costs.

    Without the flag the page would let a hand-drawn allocation run against an
    account whose experts own the buying power.
    """
    _use_account(monkeypatch, _Account(account_id, {'manual_trading_enabled': False}))
    gate = page._load_gate(account_id)
    assert gate.allowed is False
    assert gate.reason_code == GATE_NOT_MANUAL


def test_load_gate_blocks_an_account_that_never_saved_the_flag(monkeypatch, account_id):
    _use_account(monkeypatch, _Account(account_id, {}))
    assert page._load_gate(account_id).reason_code == GATE_NOT_MANUAL


def test_load_gate_reads_the_flag_through_the_interface_default_not_settings_get(
        monkeypatch, account_id):
    """``settings.get('manual_trading_enabled', False)`` is the documented trap.

    A historical bug stored the STRING ``"None"`` for unset settings.
    ``get_setting_with_interface_default`` treats it as unset and falls back to the
    declared default of False; ``settings.get(...)`` hands back ``"None"``, which is
    a non-empty string and therefore truthy -- so the mutation opens the page on an
    expert-driven account.
    """
    _use_account(monkeypatch, _Account(account_id, {'manual_trading_enabled': 'None'}))
    account = _Account(account_id, {'manual_trading_enabled': 'None'})
    assert bool(account.settings.get('manual_trading_enabled', False)) is True   # the trap
    assert page._load_gate(account_id).reason_code == GATE_NOT_MANUAL


def test_load_gate_blocks_a_manual_account_that_still_has_an_enabled_expert(
        monkeypatch, account_id):
    """Mutation: pass ``[]`` for the enabled experts. The gate exists because the
    experts and this page would otherwise fight over the same buying power."""
    create_expert_instance(account_id=account_id, expert='MockExpert', enabled=True,
                           alias='Momentum #3')
    _use_account(monkeypatch, _Account(account_id, {'manual_trading_enabled': True}))
    gate = page._load_gate(account_id)
    assert gate.allowed is False
    assert gate.reason_code == GATE_HAS_EXPERTS
    assert gate.expert_names == ['Momentum #3']
    assert 'Momentum #3' in gate.message


def test_load_gate_ignores_a_disabled_expert(monkeypatch, account_id):
    create_expert_instance(account_id=account_id, expert='MockExpert', enabled=False,
                           alias='Retired')
    _use_account(monkeypatch, _Account(account_id, {'manual_trading_enabled': True}))
    assert page._load_gate(account_id).allowed is True


def test_load_gate_ignores_another_accounts_enabled_expert(monkeypatch, account_id):
    other = create_account_definition(name='Other', provider='MockAccount')
    create_expert_instance(account_id=other.id, expert='MockExpert', enabled=True,
                           alias='Someone elses')
    _use_account(monkeypatch, _Account(account_id, {'manual_trading_enabled': True}))
    assert page._load_gate(account_id).allowed is True


def test_load_gate_with_no_account_selected_asks_for_one(monkeypatch):
    """'All accounts' is account_id None, and no broker call may be made for it."""
    import ba2_trade_platform.core.utils as core_utils

    def _explode(account_id):
        raise AssertionError('the account must not be instantiated for "All accounts"')

    monkeypatch.setattr(core_utils, 'get_account_instance_from_id', _explode)
    gate = page._load_gate(None)
    assert gate.allowed is False
    assert gate.reason_code == GATE_NO_ACCOUNT


def test_load_gate_reports_an_uninstantiable_account_instead_of_crashing(
        monkeypatch, account_id):
    import ba2_trade_platform.core.utils as core_utils
    errors = _capture_errors(monkeypatch)

    def _boom(_id):
        raise RuntimeError('broker credentials rejected')

    monkeypatch.setattr(core_utils, 'get_account_instance_from_id', _boom)
    gate = page._load_gate(account_id)
    assert gate.allowed is False
    assert gate.reason_code == GATE_NOT_MANUAL
    assert any('broker credentials rejected' in m for m in errors)


# ---------------------------------------------------------------------------
# _load_view_payload -- the tri-state, the bulk quote, the membership
# ---------------------------------------------------------------------------

def test_load_view_payload_refuses_to_render_a_failed_position_fetch(
        monkeypatch, account_id):
    """``get_positions()`` is TRI-STATE: None is a FAILED fetch, [] is flat.

    ``get_positions() or []`` collapses the two and the page then reports a
    perfectly healthy account as holding nothing -- the same incident, five times
    over. It must raise instead.
    """
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    _use_account(monkeypatch, _Account(account_id, positions=None,
                                       prices={'AAPL': 200.0}))
    with pytest.raises(PositionFetchFailed):
        page._load_view_payload(account_id, VALUATION_MODE_COST)


def test_load_view_payload_treats_an_empty_position_list_as_genuinely_flat(
        monkeypatch, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    _use_account(monkeypatch, _Account(account_id, positions=[], prices={'AAPL': 200.0}))

    payload = page._load_view_payload(account_id, VALUATION_MODE_COST)
    view = payload['views'][0]
    assert view.label == 'ARK26'
    assert view.current_value == 0.0
    assert [r.symbol for r in view.rows] == ['AAPL']


def test_load_view_payload_builds_the_labels_rows_from_the_broker_book(
        monkeypatch, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=60.0, comment='core')
    add_label_to_instruments(['AAPL', 'MSFT'], 'ARK26')
    account = _use_account(monkeypatch, _Account(
        account_id,
        positions=[_pos('AAPL', 10, 6000.0, 8000.0), _pos('MSFT', 5, 2000.0, 2500.0)],
        prices={'AAPL': 800.0, 'MSFT': 500.0}))

    payload = page._load_view_payload(account_id, VALUATION_MODE_COST)
    view = payload['views'][0]
    assert view.target_pct == 60.0
    assert view.comment == 'core'
    assert view.current_value == 8000.0
    assert payload['symbols_by_label'] == {'ARK26': ['AAPL', 'MSFT']}
    assert payload['valuation_mode'] == VALUATION_MODE_COST
    # ONE bulk quote call for the whole page, with the distinct symbols.
    assert account.quote_requests == [['AAPL', 'MSFT']]


def test_load_view_payload_threads_the_valuation_mode_through(monkeypatch, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    _use_account(monkeypatch, _Account(account_id,
                                       positions=[_pos('AAPL', 10, 1000.0, 1100.0)],
                                       prices={'AAPL': 250.0}))

    cost = page._load_view_payload(account_id, VALUATION_MODE_COST)
    market = page._load_view_payload(account_id, VALUATION_MODE_MARKET)
    assert cost['views'][0].current_value == 1000.0
    assert market['views'][0].current_value == 2500.0
    assert market['valuation_mode'] == VALUATION_MODE_MARKET


def test_load_view_payload_attaches_the_stored_symbol_comments(monkeypatch, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=100.0, comment='trim')
    _use_account(monkeypatch, _Account(account_id,
                                       positions=[_pos('AAPL', 10, 1000.0, 1100.0)],
                                       prices={'AAPL': 110.0}))
    payload = page._load_view_payload(account_id, VALUATION_MODE_COST)
    assert payload['views'][0].rows[0].comment == 'trim'


def test_load_view_payload_renders_a_short_as_negative_exposure(monkeypatch, account_id):
    """The TastyTrade shape: abs quantity plus a side. Read raw it looks long."""
    set_managed_label(account_id, 'Hedges', target_pct=100.0)
    add_label_to_instruments(['TSLA'], 'Hedges')
    _use_account(monkeypatch, _Account(
        account_id, positions=[_pos('TSLA', 10, 3000.0, 3300.0, side='SELL')],
        prices={'TSLA': 330.0}))

    view = page._load_view_payload(account_id, VALUATION_MODE_COST)['views'][0]
    assert view.current_value == -3000.0
    assert view.rows[0].quantity == -10.0
    assert view.rows[0].pct_of_label == 100.0


def test_load_view_payload_raises_when_the_account_cannot_be_instantiated(
        monkeypatch, account_id):
    import ba2_trade_platform.core.utils as core_utils
    monkeypatch.setattr(core_utils, 'get_account_instance_from_id', lambda _id: None)
    with pytest.raises(RuntimeError):
        page._load_view_payload(account_id, VALUATION_MODE_COST)


# ---------------------------------------------------------------------------
# _save_symbol_comment -- the c63d34c comment-zeroing bug
# ---------------------------------------------------------------------------

def test_a_comment_only_edit_pins_the_symbols_current_effective_weight(account_id):
    """Bug c63d34c: writing the comment alone CREATES the row at weight_pct 0.0.

    The engine reads 0 as "hold none of this", so the next rebalance sells a
    position the user only wrote a note about. The symbol's effective weight (here
    the even split it was silently taking) has to be written alongside.
    """
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    page._write_symbol_comment(account_id, 'ARK26', 'AAPL', 'watch the gap',
                               ['AAPL', 'MSFT'])

    rows = get_symbol_rows(account_id, 'ARK26')
    assert rows['AAPL'].comment == 'watch the gap'
    assert rows['AAPL'].weight_pct == 50.0
    assert get_symbol_weights(account_id, 'ARK26', ['AAPL', 'MSFT']) == {
        'AAPL': 50.0, 'MSFT': 50.0}


def test_a_comment_only_edit_leaves_an_explicit_weight_exactly_where_it_was(account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=70.0)
    page._write_symbol_comment(account_id, 'ARK26', 'AAPL', 'still 70', ['AAPL', 'MSFT'])
    assert get_symbol_rows(account_id, 'ARK26')['AAPL'].weight_pct == 70.0


def test_a_comment_only_edit_respects_an_explicit_zero_weight(account_id):
    """0.0 is a legitimate 'hold none', never re-read as 'unstored'."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=0.0)
    page._write_symbol_comment(account_id, 'ARK26', 'AAPL', 'exited', ['AAPL', 'MSFT'])
    assert get_symbol_rows(account_id, 'ARK26')['AAPL'].weight_pct == 0.0


def test_clearing_a_symbol_comment_still_does_not_move_the_weight(account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=70.0, comment='old')
    page._write_symbol_comment(account_id, 'ARK26', 'AAPL', '', ['AAPL', 'MSFT'])
    row = get_symbol_rows(account_id, 'ARK26')['AAPL']
    assert row.comment == ''
    assert row.weight_pct == 70.0


def test_a_failed_symbol_comment_save_is_reported_not_raised(monkeypatch, account_id):
    sent = _capture_notifications(monkeypatch)
    errors = _capture_errors(monkeypatch)
    monkeypatch.setattr(page, '_write_symbol_comment',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db gone')))

    asyncio.run(page._save_symbol_comment(account_id, 'ARK26', 'AAPL', 'x', ['AAPL']))
    assert any(kind == 'negative' for _, kind in sent)
    assert any('db gone' in m for m in errors)


# ---------------------------------------------------------------------------
# _save_label_comment
# ---------------------------------------------------------------------------

def test_saving_a_label_comment_leaves_its_target_alone(account_id):
    set_managed_label(account_id, 'ARK26', target_pct=42.0)
    assert page._write_label_comment(account_id, 'ARK26', 'rebalance monthly') is True
    row = next(r for r in get_managed_labels(account_id) if r.label == 'ARK26')
    assert row.comment == 'rebalance monthly'
    assert row.target_pct == 42.0


def test_clearing_a_label_comment_stores_an_empty_string(account_id):
    set_managed_label(account_id, 'ARK26', target_pct=42.0, comment='old')
    page._write_label_comment(account_id, 'ARK26', '')
    row = next(r for r in get_managed_labels(account_id) if r.label == 'ARK26')
    assert row.comment == ''


def test_typing_in_a_stale_labels_comment_box_does_not_recreate_it(account_id):
    """``set_managed_label`` CREATES the row when absent, at target 0%.

    A page rendered before the label was unmanaged still shows its comment box, and
    the engine reads a 0% target as "hold none of this" -- so a stray keystroke
    would resurrect a label that then asks to sell everything under it.
    """
    assert page._write_label_comment(account_id, 'GhostBasket', 'oops') is False
    assert [r.label for r in get_managed_labels(account_id)] == []


def test_a_stale_label_comment_tells_the_user_instead_of_failing_silently(
        monkeypatch, account_id):
    sent = _capture_notifications(monkeypatch)
    asyncio.run(page._save_label_comment(account_id, 'GhostBasket', 'oops'))
    assert any(kind == 'warning' and 'GhostBasket' in msg for msg, kind in sent)


def test_a_failed_label_comment_save_is_reported_not_raised(monkeypatch, account_id):
    sent = _capture_notifications(monkeypatch)
    errors = _capture_errors(monkeypatch)
    monkeypatch.setattr(page, '_write_label_comment',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db gone')))
    asyncio.run(page._save_label_comment(account_id, 'ARK26', 'x'))
    assert any(kind == 'negative' for _, kind in sent)
    assert any('db gone' in m for m in errors)


# ---------------------------------------------------------------------------
# F-C1 -- the label picker may not silently unmanage anything
# ---------------------------------------------------------------------------

def test_nicegui_drops_a_selected_value_that_is_not_an_option(nicegui_client):
    """The mechanism F-C1 rests on, verified against the INSTALLED NiceGUI.

    ``Select._value_to_model_value`` suppresses the ``ValueError`` from
    ``self._values.index(item)``, so the ghost never reaches the browser at all,
    and ``_event_args_to_value`` ends with
    ``[arg for arg in args if arg in self._values]``, so the first change event
    reports the selection WITHOUT it. Feed that to ``replace_managed_labels`` and
    the label's row plus every per-symbol weight under it is deleted.
    """
    from nicegui import ui
    from nicegui.events import GenericEventArguments

    with nicegui_client:
        select = ui.select(['ARK26'], value=['ARK26', 'GhostBasket'], multiple=True)

    assert select._value_to_model_value(select.value) == [{'value': 0, 'label': 'ARK26'}]
    event = GenericEventArguments(sender=select, client=nicegui_client,
                                  args=[{'value': 0, 'label': 'ARK26'}])
    assert select._event_args_to_value(event) == ['ARK26']


def test_the_picker_lists_a_managed_label_no_instrument_carries_any_more(
        monkeypatch, nicegui_client, account_id):
    """The fix: the options are the UNION, so the ghost is a real, selected chip."""
    from nicegui import ui

    replace_managed_labels(account_id, ['ARK26', 'GhostBasket'])
    add_label_to_instruments(['AAPL'], 'ARK26')

    with nicegui_client:
        page._open_label_picker(account_id, lambda: None)
    select = next(el for el in nicegui_client.layout.descendants()
                  if isinstance(el, ui.select))

    assert 'GhostBasket' in select.options
    assert sorted(select.value) == ['ARK26', 'GhostBasket']
    # Nothing is dropped on the way to the browser, so nothing is dropped back.
    assert len(select._value_to_model_value(select.value)) == 2


def test_the_picker_lists_a_managed_machine_tag_even_with_the_filter_on(
        monkeypatch, nicegui_client, account_id):
    from nicegui import ui

    replace_managed_labels(account_id, ['penny-17'])
    add_label_to_instruments(['AAPL'], 'tech')

    with nicegui_client:
        page._open_label_picker(account_id, lambda: None)
    select = next(el for el in nicegui_client.layout.descendants()
                  if isinstance(el, ui.select))
    assert 'penny-17' in select.options
    assert select.value == ['penny-17']


def test_selecting_an_extra_label_persists_straight_away(monkeypatch, account_id):
    replace_managed_labels(account_id, ['ARK26'])
    _capture_notifications(monkeypatch)
    current = ['ARK26']

    asyncio.run(page._persist_managed_labels(account_id, current, ['ARK26', 'tech'],
                                             lambda: None))
    assert sorted(r.label for r in get_managed_labels(account_id)) == ['ARK26', 'tech']
    assert current == ['ARK26', 'tech']


def test_deselecting_a_label_destroys_nothing_until_it_is_confirmed(
        monkeypatch, account_id):
    """One mis-aimed click on a chip's ✕ used to delete a whole basket's weights.

    ``replace_managed_labels`` removes the label row AND every
    ``PortfolioAllocationSymbol`` beneath it. No undo, no audit row.
    """
    replace_managed_labels(account_id, ['ARK26', 'tech'])
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=70.0, comment='keep me')
    set_managed_label(account_id, 'ARK26', target_pct=55.0)
    _capture_notifications(monkeypatch)

    asked = {}
    monkeypatch.setattr(page, '_confirm_unmanage',
                        lambda labels, counts, on_confirm, on_cancel: asked.update(
                            labels=labels, counts=counts, confirm=on_confirm,
                            cancel=on_cancel))

    current = ['ARK26', 'tech']
    asyncio.run(page._persist_managed_labels(account_id, current, ['tech'], lambda: None))

    assert asked['labels'] == ['ARK26']
    assert asked['counts'] == {'ARK26': 1}
    assert sorted(r.label for r in get_managed_labels(account_id)) == ['ARK26', 'tech']
    assert get_symbol_rows(account_id, 'ARK26')['AAPL'].weight_pct == 70.0
    assert current == ['ARK26', 'tech']


def test_cancelling_the_confirmation_puts_the_chips_back(monkeypatch, account_id):
    replace_managed_labels(account_id, ['ARK26', 'tech'])
    _capture_notifications(monkeypatch)
    restored = []
    monkeypatch.setattr(page, '_confirm_unmanage',
                        lambda labels, counts, on_confirm, on_cancel: on_cancel())

    current = ['ARK26', 'tech']
    asyncio.run(page._persist_managed_labels(account_id, current, ['tech'],
                                             lambda: restored.append(True)))
    assert restored == [True]
    assert sorted(r.label for r in get_managed_labels(account_id)) == ['ARK26', 'tech']


def test_confirming_the_removal_does_delete_it(monkeypatch, account_id):
    replace_managed_labels(account_id, ['ARK26', 'tech'])
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=70.0)
    _capture_notifications(monkeypatch)

    confirmed = {}
    monkeypatch.setattr(page, '_confirm_unmanage',
                        lambda labels, counts, on_confirm, on_cancel:
                        confirmed.update(run=on_confirm))

    current = ['ARK26', 'tech']
    asyncio.run(page._persist_managed_labels(account_id, current, ['tech'], lambda: None))
    asyncio.run(confirmed['run']())

    assert [r.label for r in get_managed_labels(account_id)] == ['tech']
    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert current == ['tech']


def test_an_unchanged_selection_writes_nothing(monkeypatch, account_id):
    """The picker fires on every change event; re-saving the same set must be a
    no-op rather than a pointless write."""
    replace_managed_labels(account_id, ['ARK26'])
    calls = []
    monkeypatch.setattr(page, 'replace_managed_labels',
                        lambda *a, **k: calls.append(a))
    asyncio.run(page._persist_managed_labels(account_id, ['ARK26'], ['ARK26'],
                                             lambda: None))
    assert calls == []


def test_a_failed_label_write_restores_the_widget_and_says_so(monkeypatch, account_id):
    sent = _capture_notifications(monkeypatch)
    errors = _capture_errors(monkeypatch)
    restored = []
    monkeypatch.setattr(page, 'replace_managed_labels',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('disk full')))

    current = ['ARK26']
    asyncio.run(page._persist_managed_labels(account_id, current, ['ARK26', 'tech'],
                                             lambda: restored.append(True)))
    assert restored == [True]
    assert current == ['ARK26']
    assert any(kind == 'negative' for _, kind in sent)
    assert any('disk full' in m for m in errors)


def test_the_confirmation_dialog_names_the_labels_and_what_they_take_with_them(
        nicegui_client):
    with nicegui_client:
        page._confirm_unmanage(['ARK26'], {'ARK26': 3}, lambda: None, lambda: None)
    text = ' '.join(_texts(nicegui_client.layout))
    assert 'ARK26' in text
    assert '3 stored symbol weight/comment row(s)' in text
    assert 'cannot be undone' in text


# ---------------------------------------------------------------------------
# F-I2 -- the machine-tag families come from the live registry
# ---------------------------------------------------------------------------

def test_every_registered_expert_family_is_hidden_from_the_picker():
    """The rule, not a snapshot: ``shortname`` is ``<classname.lower()>-<id>``.

    Two live tags already leaked past the hardcoded three-family regex, and the
    next expert would have leaked too.
    """
    from ba2_trade_platform.modules.experts import experts
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import is_machine_label

    families = page._expert_label_families()
    for cls in experts:
        tag = f'{cls.__name__.lower()}-7'
        assert is_machine_label(tag, families) is True, tag


def test_the_derived_families_still_hide_the_renamed_penny_family():
    families = page._expert_label_families()
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import is_machine_label
    assert is_machine_label('penny-17', families) is True
    assert is_machine_label('Penny', families) is False


def test_a_registry_that_cannot_be_read_falls_back_instead_of_breaking_the_picker(
        monkeypatch):
    import builtins
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        DEFAULT_MACHINE_LABEL_FAMILIES,
    )
    real_import = builtins.__import__

    def _fail(name, *args, **kwargs):
        if name.endswith('modules.experts') or name == 'ba2_trade_platform.modules.experts':
            raise ImportError('registry unavailable')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', _fail)
    monkeypatch.setattr(page.logger, 'warning', lambda *a, **k: None)
    assert page._expert_label_families() == DEFAULT_MACHINE_LABEL_FAMILIES


# ---------------------------------------------------------------------------
# F-I5 -- "add symbol" may not invent global Instrument rows
# ---------------------------------------------------------------------------

def test_validate_symbols_splits_the_typo_off_from_the_real_ticker(
        monkeypatch, account_id):
    _use_account(monkeypatch, _Account(account_id, known_symbols={'AAPL'}))
    known, unknown = page._validate_symbols(account_id, ['AAPL', 'APPL'])
    assert known == ['AAPL']
    assert unknown == ['APPL']


def test_validate_symbols_refuses_when_the_account_cannot_be_instantiated(
        monkeypatch, account_id):
    import ba2_trade_platform.core.utils as core_utils
    monkeypatch.setattr(core_utils, 'get_account_instance_from_id', lambda _id: None)
    with pytest.raises(RuntimeError):
        page._validate_symbols(account_id, ['AAPL'])


def _instrument_names():
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import Instrument
    with get_db() as session:
        return sorted(i.name for i in session.exec(select(Instrument)).all())


def test_adding_a_typo_creates_no_instrument_row_and_says_why(monkeypatch, account_id):
    """``add_label_to_instruments`` creates a global ``Instrument`` for anything it
    is handed, so 'APPL' became a permanent row every account's picker then saw.

    The trailing comma is deliberate: it is the other half of the same bug report.
    """
    sent = _capture_notifications(monkeypatch)
    _use_account(monkeypatch, _Account(account_id, known_symbols={'AAPL'}))
    set_managed_label(account_id, 'ARK26', target_pct=100.0)

    asyncio.run(page._add_symbols_from_input(account_id, 'ARK26', 'AAPL, APPL,',
                                             _noop_refresh))

    assert _instrument_names() == ['AAPL']
    assert any(kind == 'warning' and 'APPL' in msg for msg, kind in sent)


def test_adding_only_unknown_symbols_writes_nothing_at_all(monkeypatch, account_id):
    sent = _capture_notifications(monkeypatch)
    _use_account(monkeypatch, _Account(account_id, known_symbols={'AAPL'}))
    refreshed = []

    async def _refresh():
        refreshed.append(True)

    asyncio.run(page._add_symbols_from_input(account_id, 'ARK26', 'APPL', _refresh))
    assert _instrument_names() == []
    assert refreshed == []
    assert any(kind == 'warning' for _, kind in sent)


def test_a_broker_check_that_fails_blocks_the_add_rather_than_guessing(
        monkeypatch, account_id):
    """No fallback: an unanswerable check must not write the row anyway."""
    sent = _capture_notifications(monkeypatch)
    errors = _capture_errors(monkeypatch)
    monkeypatch.setattr(page, '_validate_symbols',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('broker down')))

    asyncio.run(page._add_symbols_from_input(account_id, 'ARK26', 'AAPL', _noop_refresh))
    assert _instrument_names() == []
    assert any(kind == 'negative' for _, kind in sent)
    assert any('broker down' in m for m in errors)


def test_adding_known_symbols_labels_them_and_refreshes(monkeypatch, account_id):
    _capture_notifications(monkeypatch)
    _use_account(monkeypatch, _Account(account_id, known_symbols={'AAPL', 'MSFT'}))
    refreshed = []

    async def _refresh():
        refreshed.append(True)

    asyncio.run(page._add_symbols_from_input(account_id, 'ARK26', 'aapl, MSFT', _refresh))
    assert _instrument_names() == ['AAPL', 'MSFT']
    assert refreshed == [True]


async def _noop_refresh():
    return None


# ---------------------------------------------------------------------------
# F-I1 and the missing-quote banner -- what the headline actually says
# ---------------------------------------------------------------------------

def _payload(views, mode=VALUATION_MODE_COST):
    return {'views': views, 'symbols_by_label': {}, 'valuation_mode': mode}


def test_the_managed_value_headline_counts_a_two_label_symbol_once(nicegui_client):
    """The headline used to sum the per-label totals while every percentage beneath
    it was divided by the DISTINCT total -- two denominators, one screen."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    positions = positions_by_symbol([_pos('TSLA', 10, 6000.0, 6000.0),
                                     _pos('AAPL', 1, 1000.0, 1000.0)])
    views = build_label_views([ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
                              {'ARK26': ['TSLA', 'AAPL'], 'HighRisk': ['TSLA']},
                              positions, {}, valuation_mode=VALUATION_MODE_COST)
    with nicegui_client:
        page._render_labels(1, _payload(views), _noop_refresh)

    text = ' '.join(_texts(nicegui_client.layout))
    assert '$7,000.00' in text
    assert '$13,000.00' not in text


def test_market_mode_names_the_held_symbols_it_could_not_quote(nicegui_client):
    """A bulk-quote outage rendered every position at $0.00 with no explanation."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    positions = positions_by_symbol([_pos('AAPL', 10, 1000.0, 1100.0)])
    views = build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                              positions, {'AAPL': None},
                              valuation_mode=VALUATION_MODE_MARKET)
    with nicegui_client:
        page._render_labels(1, _payload(views, VALUATION_MODE_MARKET), _noop_refresh)

    text = ' '.join(_texts(nicegui_client.layout))
    assert 'No quote for 1 held symbol(s): AAPL' in text
    assert 'NOT the same' in text
    # W1 promoted this from an advisory to a REFUSAL: since market is the default
    # mode, this is no longer "your percentages are a bit off", it is "the
    # allocatable base is missing this position and Allocate will not run".
    assert 'Allocate is blocked' in text


def test_the_unpriced_holding_banner_is_drawn_as_a_danger_not_a_warning(nicegui_client):
    """``alert-banner warning`` is the same orange the page uses for footnotes. A
    refusal has to look like one, or the user reads past it and presses Allocate."""
    from nicegui import ui
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    positions = positions_by_symbol([_pos('AAPL', 10, 1000.0, 1100.0)])
    views = build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                              positions, {'AAPL': None},
                              valuation_mode=VALUATION_MODE_MARKET)
    with nicegui_client:
        page._render_labels(1, _payload(views, VALUATION_MODE_MARKET), _noop_refresh)
        classes = [' '.join(el._classes) for el in nicegui_client.layout.descendants()
                   if isinstance(el, ui.element)]

    assert any('alert-banner danger' in c for c in classes)


def test_cost_mode_does_not_warn_about_quotes_it_never_used(nicegui_client):
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    positions = positions_by_symbol([_pos('AAPL', 10, 1000.0, 1100.0)])
    views = build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                              positions, {'AAPL': None},
                              valuation_mode=VALUATION_MODE_COST)
    with nicegui_client:
        page._render_labels(1, _payload(views, VALUATION_MODE_COST), _noop_refresh)
    assert 'No quote for' not in ' '.join(_texts(nicegui_client.layout))


def test_content_draws_the_whole_page_for_a_manual_account(
        monkeypatch, nicegui_client, account_id):
    """End to end through the real ``content()``: gate, mode, payload, render."""
    monkeypatch.setattr(page, 'get_selected_account_id', lambda: account_id)
    _use_account(monkeypatch, _Account(account_id, {'manual_trading_enabled': True},
                                       positions=[_pos('AAPL', 10, 6000.0, 8000.0)],
                                       prices={'AAPL': 800.0}))
    set_managed_label(account_id, 'ARK26', target_pct=60.0)
    add_label_to_instruments(['AAPL'], 'ARK26')

    _run_in_client(nicegui_client, page.content)

    text = ' '.join(_texts(nicegui_client.layout))
    assert 'Portfolio Allocation' in text
    # MARKET is the default mode (W1): 10 shares at 800 is 8,000, not the 6,000
    # they cost. The headline names the mode it used right next to the number.
    assert '$8,000.00' in text
    assert 'market value (qty x price)' in text
    assert '$6,000.00' not in text
    assert 'Managed labels' in text


def test_content_renders_the_block_message_for_a_non_manual_account(
        monkeypatch, nicegui_client, account_id):
    monkeypatch.setattr(page, 'get_selected_account_id', lambda: account_id)
    _use_account(monkeypatch, _Account(account_id, {'manual_trading_enabled': False}))

    _run_in_client(nicegui_client, page.content)

    text = ' '.join(_texts(nicegui_client.layout))
    assert 'not available for this selection' in text
    assert 'Manually traded account' in text


def test_content_shows_the_broker_outage_banner_rather_than_an_empty_book(
        monkeypatch, nicegui_client, account_id):
    """A failed position fetch must not render as 'you hold nothing'."""
    _capture_errors(monkeypatch)
    monkeypatch.setattr(page, 'get_selected_account_id', lambda: account_id)
    _use_account(monkeypatch, _Account(account_id, {'manual_trading_enabled': True},
                                       positions=None))
    set_managed_label(account_id, 'ARK26', target_pct=60.0)
    add_label_to_instruments(['AAPL'], 'ARK26')

    _run_in_client(nicegui_client, page.content)

    text = ' '.join(_texts(nicegui_client.layout))
    assert 'Broker position fetch FAILED' in text
    assert 'a failed fetch and a flat account are not the same thing' in text.lower()


def test_content_reports_a_gate_loader_failure_instead_of_500ing_the_route(
        monkeypatch, nicegui_client):
    """``content()`` used to await both loaders outside any try, so a DB error left
    the user with a bare 500 and no message."""
    errors = _capture_errors(monkeypatch)
    monkeypatch.setattr(page, 'get_selected_account_id', lambda: 7)
    monkeypatch.setattr(page, '_load_gate',
                        lambda _id: (_ for _ in ()).throw(RuntimeError('db locked')))
    _run_in_client(nicegui_client, page.content)
    text = ' '.join(_texts(nicegui_client.layout))
    assert 'Could not load this account' in text
    assert 'db locked' in text
    assert any('db locked' in m for m in errors)


def test_content_reports_a_valuation_mode_failure_instead_of_500ing_the_route(
        monkeypatch, nicegui_client):
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import GateResult

    _capture_errors(monkeypatch)
    monkeypatch.setattr(page, 'get_selected_account_id', lambda: 7)
    monkeypatch.setattr(page, '_load_gate',
                        lambda _id: GateResult(allowed=True, reason_code=GATE_OK,
                                               message=''))
    monkeypatch.setattr(page, '_load_valuation_mode',
                        lambda _id: (_ for _ in ()).throw(RuntimeError('config gone')))
    _run_in_client(nicegui_client, page.content)
    text = ' '.join(_texts(nicegui_client.layout))
    assert 'Could not load the valuation mode' in text
    assert 'config gone' in text


def test_the_comment_inputs_are_debounced_rather_than_saving_per_keystroke(
        nicegui_client):
    """Every keystroke used to run SELECT + UPDATE + commit + refresh on the
    NiceGUI event loop, twice over for a symbol comment."""
    from nicegui import ui
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    positions = positions_by_symbol([_pos('AAPL', 10, 1000.0, 1100.0)])
    view = build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                             positions, {'AAPL': 110.0},
                             valuation_mode=VALUATION_MODE_MARKET)[0]
    with nicegui_client:
        page._render_label_body(1, view, _noop_refresh)

    comment_input = next(el for el in nicegui_client.layout.descendants()
                         if isinstance(el, ui.input))
    assert int(comment_input._props['debounce']) == page.COMMENT_DEBOUNCE_MS

    table = next(el for el in nicegui_client.layout.descendants()
                 if isinstance(el, ui.table))
    template = table.slots['body-cell-comment'].template
    assert f'debounce="{page.COMMENT_DEBOUNCE_MS}"' in template


# ---------------------------------------------------------------------------
# The page -> service seam, driven END TO END
#
# Everything above this line stops at the page's own helpers. Nothing exercised
# ``_solve_plan`` or the Allocate flow, so
# ``svc.precheck_plan(account, plan, available_buying_power=...)`` -- a call
# missing the REQUIRED keyword-only ``margin`` -- shipped, and every live dry run
# raised TypeError at the last line of the solve. A 2,300-test suite was green.
#
# Two guards, deliberately different in kind:
#   * these behavioural tests, which run the real page functions against a fake
#     broker and would have caught it as a broken dry run; and
#   * ``test_every_page_call_into_the_service_matches_its_real_signature``, which
#     binds every call site with ``inspect.signature`` and catches drift in a call
#     no behavioural test happens to reach.
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone            # noqa: E402

from ba2_trade_platform.core.account_types import (           # noqa: E402
    MARKET_HOURS_SOURCE_BROKER, AccountSnapshot, MarginInfo, MarketHours,
)
from ba2_trade_platform.core.portfolio_allocation import (    # noqa: E402
    ALLOCATION_MODE_INVEST_LABEL, ALLOCATION_MODE_REBALANCE, LabelTarget, SymbolTarget,
)

#: A frozen instant inside a regular session, so nothing here depends on when the
#: suite runs. 2026-01-05 is a Monday.
_FROZEN_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


class _AllocAccount(_Account):
    """``_Account`` plus every seam the allocation service actually reaches.

    Deliberately NOT a bespoke duck type: it inherits the page tests' own account
    double, so the gate, the label view and the allocation flow all run against
    one object -- which is what makes a signature drift between the page and the
    service show up as a failing behavioural test rather than as a TypeError in
    front of a user.
    """

    def __init__(self, account_id, settings=None, *, buying_power=50_000.0,
                 cash=50_000.0, market_open=True, **kwargs):
        super().__init__(account_id, settings, **kwargs)
        self.buying_power = buying_power
        self.cash = cash
        self.market_open = market_open
        self.margin = {}                 # symbol -> MarginInfo
        self.previewed = []              # [(symbol, quantity, is_closing_order)]
        self.margin_requests = []        # [[symbols]]
        self.market_hours_calls = 0
        self.cache_clears = 0
        self.submitted = []              # [(symbol, side, quantity)]
        self.refresh_calls = 0

    # -- ReadOnlyAccountInterface seams ---------------------------------------
    def get_account_snapshot(self):
        return AccountSnapshot(cash=self.cash, equity=self.cash,
                               net_liquidation=self.cash,
                               buying_power=self.buying_power,
                               margin_multiplier=1.0, is_margin_account=False,
                               supports_fractional=True)

    def get_symbol_margin_info(self, symbols):
        self.margin_requests.append(list(symbols))
        return {s: self.margin[s] for s in symbols if s in self.margin}

    def get_market_hours(self, *, now=None):
        self.market_hours_calls += 1
        return MarketHours(
            is_open=self.market_open, source=MARKET_HOURS_SOURCE_BROKER,
            as_of=_FROZEN_NOW,
            next_open=None if self.market_open else _FROZEN_NOW + timedelta(hours=18))

    def clear_market_hours_cache(self):
        self.cache_clears += 1

    def get_cash_transfers(self, start_date=None, end_date=None):
        return []

    # -- AccountInterface seams -----------------------------------------------
    def preview_order_impact(self, trading_order, is_closing_order=False):
        self.previewed.append((trading_order.symbol, trading_order.quantity,
                               is_closing_order))
        return None                      # "this broker has no precheck"

    def submit_order(self, order, is_closing_order=False):
        from ba2_trade_platform.core.db import add_instance
        from ba2_trade_platform.core.types import OrderStatus
        if order.id is None:
            order.status = OrderStatus.PENDING
            order.id = add_instance(order, expunge_after_flush=True)
        self.submitted.append((order.symbol, order.side, order.quantity))
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.open_price = self._prices.get(order.symbol, 100.0)
        return order

    def refresh_orders(self, heuristic_mapping=False, fetch_all=False):
        from ba2_trade_platform.core.db import get_db, update_instance
        from ba2_trade_platform.core.models import TradingOrder
        from ba2_trade_platform.core.types import OrderStatus
        from sqlmodel import select as sqlmodel_select
        self.refresh_calls += 1
        with get_db() as session:
            rows = list(session.exec(sqlmodel_select(TradingOrder).where(
                TradingOrder.account_id == self.id)).all())
            ids = [r.id for r in rows]
        for order_id in ids:
            from ba2_trade_platform.core.db import get_instance
            row = get_instance(TradingOrder, order_id)
            row.status = OrderStatus.FILLED
            row.filled_qty = row.quantity
            row.open_price = self._prices.get(row.symbol, 100.0)
            update_instance(row)
        return True


def _alloc_labels(label='ARK26', symbols=('AAPL',)):
    weight = 100.0 / len(symbols)
    return [LabelTarget(label=label, target_pct=100.0,
                        symbols=[SymbolTarget(symbol=s, weight_pct=weight)
                                 for s in symbols])]


def test_solve_plan_runs_the_whole_dry_run_against_a_broker(monkeypatch, account_id):
    """The live dry run, end to end. This is the test the CRITICAL bug needed.

    ``_solve_plan`` is the ONLY producer of the plan the wizard shows and of the
    market hours that gate its Submit button, and every one of its broker reads
    plus the service precheck runs on this path. Before the fix it raised
    ``TypeError: precheck_plan() missing 1 required keyword-only argument:
    'margin'`` on its very last statement, for every account and every mode.
    """
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 200.0})
    _use_account(monkeypatch, account)

    base, plan, current, hours = page._solve_plan(
        account_id, mode=ALLOCATION_MODE_REBALANCE, labels=_alloc_labels(),
        scope_label=None, amount=0.0, allow_fractional=True,
        valuation_mode=VALUATION_MODE_COST)

    assert base.available_buying_power == 50_000.0
    assert [r.symbol for r in plan.rows] == ['AAPL']
    assert plan.buy_rows and plan.buy_rows[0].delta_quantity > 0
    assert current['AAPL'].price == 200.0
    assert hours is not None and hours.is_open is True


def test_solve_plan_hands_the_precheck_the_margin_grid_the_plan_was_solved_on(
        monkeypatch, account_id):
    """``margin`` is not decoration: without it the re-solve rounds on the default
    4dp grid and loses ``min_trade_increment`` / ``min_fractional_notional``, so a
    plan that looks right up to submission is rejected by the broker."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 200.0})
    account.margin = {'AAPL': MarginInfo(symbol='AAPL', fractionable=True,
                                         bp_factor=1.0, min_trade_increment=0.25)}
    _use_account(monkeypatch, account)

    seen = {}
    real_precheck = page.svc.precheck_plan

    def _spy(acct, plan, **kwargs):
        seen.update(kwargs)
        return real_precheck(acct, plan, **kwargs)

    monkeypatch.setattr(page.svc, 'precheck_plan', _spy)
    page._solve_plan(account_id, mode=ALLOCATION_MODE_REBALANCE,
                     labels=_alloc_labels(), scope_label=None, amount=0.0,
                     allow_fractional=True, valuation_mode=VALUATION_MODE_COST)

    assert set(seen) == {'available_buying_power', 'margin'}
    assert seen['margin']['AAPL'].min_trade_increment == 0.25


def test_solve_plan_in_invest_mode_reaches_the_service_too(monkeypatch, account_id):
    """The income panel's Invest button takes the OTHER branch of ``_solve_plan``,
    and it lands on the same precheck call."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)

    base, plan, current, hours = page._solve_plan(
        account_id, mode=ALLOCATION_MODE_INVEST_LABEL, labels=_alloc_labels(),
        scope_label='ARK26', amount=1_000.0, allow_fractional=True,
        valuation_mode=VALUATION_MODE_COST)

    assert plan.buy_rows and plan.buy_rows[0].symbol == 'AAPL'
    assert hours.is_open is True


def test_the_allocate_flow_opens_the_wizard_and_submits_through_the_service(
        monkeypatch, nicegui_client, account_id):
    """Allocate -> steps -> dry run -> Submit, with only the broker faked.

    Drives the page's real closures (``_run_dry_run``, ``_on_dry_run``,
    ``_do_submit``) rather than re-implementing them, so a drift anywhere between
    the page, the wizard and the service surfaces here.
    """
    from ba2_trade_platform.core.portfolio_allocation_store import get_recent_runs

    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)
    _capture_notifications(monkeypatch)
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')

    # The two dialog openers are captured rather than drawn: what is under test is
    # the page's glue and the service beneath it, and the wizard's own rendering
    # already has tests/test_portfolio_allocation_wizard_ui.py.
    opened, steps, pending = {}, {}, []
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.update(kw, base=a[0], plan=a[1]))
    monkeypatch.setattr(page, 'open_allocation_steps',
                        lambda *a, **kw: steps.update(kw, base=a[0], labels=a[1]))
    # The page uses ui.timer only to hop off a sync handler; queue the callback
    # instead so the test can await it with the client's slot stack active.
    monkeypatch.setattr(page.ui, 'timer',
                        lambda _delay, callback, once=False: pending.append(callback))

    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_COST, _noop_refresh))

    # Step 1-3 opened with the account's real labels.
    assert [lt.label for lt in steps['labels']] == ['ARK26']

    # The user presses Continue.
    steps['on_dry_run'](mode=ALLOCATION_MODE_REBALANCE, labels=steps['labels'],
                        scope_label=None, amount=0.0, allow_fractional=True)
    _run_in_client(nicegui_client, pending.pop())

    assert opened['market'].allowed is True
    assert [r.symbol for r in opened['plan'].rows] == ['AAPL']

    # ...and Submit.
    opened['on_submit'](opened['plan'])
    _run_in_client(nicegui_client, pending.pop())

    assert [s[0] for s in account.submitted] == ['AAPL']
    runs = get_recent_runs(account_id)
    assert len(runs) == 1 and runs[0].order_ids


# ---------------------------------------------------------------------------
# W0: Continue PERSISTS the targets. Until this, the wizard was write-only to
# memory -- ``_on_dry_run`` saved the fractional switch and nothing else, so
# every ``target_pct`` stayed at the 0.0 the picker created it with.
# ---------------------------------------------------------------------------

def _drive_to_continue(monkeypatch, nicegui_client, account_id, *, labels_edit=None,
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None, amount=0.0):
    """Open the real flow and press Continue, returning the steps' labels."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0, 'MSFT': 100.0})
    _use_account(monkeypatch, account)
    _capture_notifications(monkeypatch)

    steps, pending = {}, []
    monkeypatch.setattr(page, 'open_allocation_wizard', lambda *a, **kw: None)
    monkeypatch.setattr(page, 'open_allocation_steps',
                        lambda *a, **kw: steps.update(kw, base=a[0], labels=a[1]))
    monkeypatch.setattr(page.ui, 'timer',
                        lambda _delay, callback, once=False: pending.append(callback))

    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_MARKET, _noop_refresh))
    if labels_edit is not None:
        labels_edit(steps['labels'])
    steps['on_dry_run'](mode=mode, labels=steps['labels'], scope_label=scope_label,
                        amount=amount, allow_fractional=True)
    _run_in_client(nicegui_client, pending.pop())
    return steps


def test_continue_persists_the_label_targets_the_user_typed(
        monkeypatch, nicegui_client, account_id):
    from ba2_trade_platform.core.portfolio_allocation_store import get_managed_labels

    set_managed_label(account_id, 'ARK26', target_pct=0.0)
    set_managed_label(account_id, 'TECH', target_pct=0.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    add_label_to_instruments(['MSFT'], 'TECH')

    def _edit(labels):
        by_label = {lt.label: lt for lt in labels}
        by_label['ARK26'].target_pct = 70.0
        by_label['TECH'].target_pct = 30.0

    _drive_to_continue(monkeypatch, nicegui_client, account_id, labels_edit=_edit)

    assert {row.label: row.target_pct for row in get_managed_labels(account_id)} == {
        'ARK26': 70.0, 'TECH': 30.0}


def test_continue_persists_the_symbol_weights_too(monkeypatch, nicegui_client,
                                                  account_id):
    from ba2_trade_platform.core.portfolio_allocation_store import get_symbol_weights

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL', 'MSFT'], 'ARK26')

    def _edit(labels):
        by_symbol = {st.symbol: st for st in labels[0].symbols}
        by_symbol['AAPL'].weight_pct = 80.0
        by_symbol['MSFT'].weight_pct = 20.0

    _drive_to_continue(monkeypatch, nicegui_client, account_id, labels_edit=_edit)

    assert get_symbol_weights(account_id, 'ARK26', ['AAPL', 'MSFT']) == {
        'AAPL': 80.0, 'MSFT': 20.0}


def test_an_invest_run_persists_the_weights_but_not_the_labels_percentage(
        monkeypatch, nicegui_client, account_id):
    """An INVEST_LABEL run spends an explicit amount; the label's percentage played
    no part in it and must not be recorded as a choice the user made."""
    from ba2_trade_platform.core.portfolio_allocation_store import (
        get_managed_labels, get_symbol_weights,
    )

    set_managed_label(account_id, 'ARK26', target_pct=25.0)
    add_label_to_instruments(['AAPL', 'MSFT'], 'ARK26')

    def _edit(labels):
        labels[0].target_pct = 99.0
        by_symbol = {st.symbol: st for st in labels[0].symbols}
        by_symbol['AAPL'].weight_pct = 90.0
        by_symbol['MSFT'].weight_pct = 10.0

    _drive_to_continue(monkeypatch, nicegui_client, account_id, labels_edit=_edit,
                       mode=ALLOCATION_MODE_INVEST_LABEL, scope_label='ARK26',
                       amount=1_000.0)

    assert get_managed_labels(account_id)[0].target_pct == 25.0
    assert get_symbol_weights(account_id, 'ARK26', ['AAPL', 'MSFT']) == {
        'AAPL': 90.0, 'MSFT': 10.0}


def test_a_label_unmanaged_mid_flight_is_reported_rather_than_dropped_silently(
        monkeypatch, nicegui_client, account_id):
    """The wizard holds the labels it opened with. If one is unmanaged in another
    tab meanwhile, ``save_allocation_targets`` refuses to re-create it -- correct,
    but the user must be told, or they come back tomorrow to numbers they did not
    choose and no record of why."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')

    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)
    notes = _capture_notifications(monkeypatch)
    steps, pending = {}, []
    monkeypatch.setattr(page, 'open_allocation_wizard', lambda *a, **kw: None)
    monkeypatch.setattr(page, 'open_allocation_steps',
                        lambda *a, **kw: steps.update(kw, base=a[0], labels=a[1]))
    monkeypatch.setattr(page.ui, 'timer',
                        lambda _delay, callback, once=False: pending.append(callback))

    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_MARKET, _noop_refresh))
    # ...and now the label goes away underneath the open dialog.
    remove_managed_label(account_id, 'ARK26')
    steps['on_dry_run'](mode=ALLOCATION_MODE_REBALANCE, labels=steps['labels'],
                        scope_label=None, amount=0.0, allow_fractional=True)
    _run_in_client(nicegui_client, pending.pop())

    assert any('no longer managed' in text for text, _ in notes), notes


def test_a_failed_target_save_does_not_stop_the_dry_run(monkeypatch, nicegui_client,
                                                        account_id):
    """Persisting is a convenience; SOLVING is what the user pressed Continue for.
    A DB error here must be reported and then got out of the way, not turned into
    "the wizard would not open"."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    monkeypatch.setattr(page, 'save_allocation_targets',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('disk full')))

    opened = {}
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)
    notes = _capture_notifications(monkeypatch)
    steps, pending = {}, []
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.update(kw, base=a[0], plan=a[1]))
    monkeypatch.setattr(page, 'open_allocation_steps',
                        lambda *a, **kw: steps.update(kw, base=a[0], labels=a[1]))
    monkeypatch.setattr(page.ui, 'timer',
                        lambda _delay, callback, once=False: pending.append(callback))

    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_MARKET, _noop_refresh))
    steps['on_dry_run'](mode=ALLOCATION_MODE_REBALANCE, labels=steps['labels'],
                        scope_label=None, amount=0.0, allow_fractional=True)
    _run_in_client(nicegui_client, pending.pop())

    assert [r.symbol for r in opened['plan'].rows] == ['AAPL']
    assert any('disk full' in str(n) for n in notes), notes


# ---------------------------------------------------------------------------
# Signature drift: bind EVERY page/wizard call into the service
# ---------------------------------------------------------------------------

def _service_call_sites():
    """``(path, lineno, name, node, module)`` for every cross-module call the page
    and the wizard make into the three modules they are glue for.

    Parses with ``ast`` rather than importing and poking, so a call inside a nested
    closure that no test happens to run is checked too. Handles the
    ``svc.<name>(...)`` alias and every ``from ... import <name>`` form.

    The service is the module the CRITICAL bug was in, but the wizard and the view
    are the same kind of seam and one of them (``open_allocation_wizard``'s
    ``on_refresh``) has already changed shape once; they cost nothing extra here.
    """
    import ast
    import inspect as _inspect

    from ba2_trade_platform.core import portfolio_allocation_service as service
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wizard_module
    from ba2_trade_platform.ui.utils import portfolio_allocation_view as view_module

    targets = {
        'portfolio_allocation_service': service,
        'portfolio_allocation_wizard': wizard_module,
        'portfolio_allocation_view': view_module,
    }
    out = []
    for module in (page, wizard_module):
        path = _inspect.getsourcefile(module)
        tree = ast.parse(open(path, encoding='utf-8').read(), path)
        aliases = {}
        direct = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            source = node.module or ''
            for suffix, target in targets.items():
                if source.endswith(suffix):
                    for item in node.names:
                        direct[item.asname or item.name] = (item.name, target)
            # ``from ...core import portfolio_allocation_service as svc``
            for item in node.names:
                if item.name in targets:
                    aliases[item.asname or item.name] = targets[item.name]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id in aliases):
                out.append((path, node.lineno, func.attr, node, aliases[func.value.id]))
            elif isinstance(func, ast.Name) and func.id in direct:
                name, target = direct[func.id]
                out.append((path, node.lineno, name, node, target))
    return out


def test_the_page_really_does_call_into_the_service():
    """Guards the guard: an AST walk that finds nothing would pass vacuously."""
    names = {name for _p, _l, name, _n, _m in _service_call_sites()}
    assert {'precheck_plan', 'run_allocation', 'build_position_states',
            'fetch_market_hours', 'clear_market_hours_cache'} <= names
    # ...and into the wizard and the view, whose contracts drift the same way.
    assert {'open_allocation_wizard', 'open_allocation_steps',
            'working_orders_notice', 'evaluate_market_gate'} <= names


def test_every_page_call_into_the_service_matches_its_real_signature():
    """Bind every page/wizard -> service call against ``inspect.signature``.

    ``precheck_plan`` gained a REQUIRED keyword-only ``margin`` and the page's call
    was never updated: every live dry run raised TypeError, and the whole suite
    stayed green because nothing drove the page's solve. Reading the signature
    from the module rather than from a hand-copied list means this keeps working
    when a parameter is added, renamed or made required.
    """
    import ast
    import inspect as _inspect

    problems = []
    for path, lineno, name, node, service in _service_call_sites():
        target = getattr(service, name, None)
        if target is None:
            problems.append(f"{path}:{lineno} svc.{name} does not exist on the service")
            continue
        if not callable(target):
            continue
        if (any(isinstance(a, ast.Starred) for a in node.args)
                or any(kw.arg is None for kw in node.keywords)):
            continue                      # */** unpacking: not statically bindable
        signature = _inspect.signature(target)
        try:
            signature.bind(*[None] * len(node.args),
                           **{kw.arg: None for kw in node.keywords})
        except TypeError as e:
            problems.append(f"{path}:{lineno} svc.{name}(...) -> {e} "
                            f"(real signature: {name}{signature})")
    assert not problems, "page/wizard call sites that no longer match the service:\n" \
                         + "\n".join(problems)


# ---------------------------------------------------------------------------
# I3 / I4: the wizard's Refresh has to move the CLOCK, and re-read it for real
# ---------------------------------------------------------------------------

def _open_the_wizard(monkeypatch, nicegui_client, account, account_id):
    """Run the Allocate flow up to the dry run and return the wizard's kwargs."""
    _use_account(monkeypatch, account)
    _capture_notifications(monkeypatch)
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    opened, steps, pending = {}, {}, []
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.update(kw, base=a[0], plan=a[1]))
    monkeypatch.setattr(page, 'open_allocation_steps',
                        lambda *a, **kw: steps.update(kw, base=a[0], labels=a[1]))
    monkeypatch.setattr(page.ui, 'timer',
                        lambda _delay, callback, once=False: pending.append(callback))
    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_COST, _noop_refresh))
    steps['on_dry_run'](mode=ALLOCATION_MODE_REBALANCE, labels=steps['labels'],
                        scope_label=None, amount=0.0, allow_fractional=True)
    _run_in_client(nicegui_client, pending.pop())
    return opened


def test_the_wizards_refresh_hands_back_a_fresh_market_gate_not_just_a_plan(
        monkeypatch, nicegui_client, account_id):
    """``_solve_plan`` reads the clock so that "ONE read feeds both the banner and
    the gate" -- and ``_on_refresh`` used to drop it into ``_``. A wizard opened
    before the bell then kept a disabled Submit all morning."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0}, market_open=False)
    opened = _open_the_wizard(monkeypatch, nicegui_client, account, account_id)
    assert opened['market'].allowed is False           # opened before the bell

    account.market_open = True                          # ...the bell goes
    plan, market = opened['on_refresh'](True)

    assert [r.symbol for r in plan.rows] == ['AAPL']
    assert market.allowed is True


def test_the_wizards_refresh_can_also_close_the_gate(monkeypatch, nicegui_client,
                                                     account_id):
    """The direction that costs money: the dialog sits open across 16:00."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0}, market_open=True)
    opened = _open_the_wizard(monkeypatch, nicegui_client, account, account_id)
    assert opened['market'].allowed is True

    account.market_open = False
    _plan, market = opened['on_refresh'](True)

    assert market.allowed is False
    assert 'Submit is disabled' in market.message


def test_the_wizards_refresh_drops_the_cached_market_hours_answer(
        monkeypatch, nicegui_client, account_id):
    """I4. ``get_market_hours`` caches for min(TTL, next session boundary), which is
    right for one render's several reads and wrong for a user pressing Refresh at
    09:31 precisely because they think the market has opened.
    ``clear_market_hours_cache`` calls itself "the EXPLICIT path, for a user who
    hits Refresh" and had no production caller at all."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    opened = _open_the_wizard(monkeypatch, nicegui_client, account, account_id)
    assert account.cache_clears == 0        # the first solve has nothing to drop

    opened['on_refresh'](True)

    assert account.cache_clears == 1
    # ...and the clock really was re-read afterwards, not answered from the cache.
    assert account.market_hours_calls >= 2


def test_the_first_solve_of_a_flow_does_not_clear_the_cache(monkeypatch, account_id):
    """Clearing on every solve would turn a per-render de-duplicator into a broker
    round trip per read."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)

    page._solve_plan(account_id, mode=ALLOCATION_MODE_REBALANCE,
                     labels=_alloc_labels(), scope_label=None, amount=0.0,
                     allow_fractional=True, valuation_mode=VALUATION_MODE_COST)

    assert account.cache_clears == 0


def test_clearing_the_market_hours_cache_never_takes_a_dry_run_down(monkeypatch,
                                                                    account_id):
    """A broker whose clear explodes costs an answer up to one TTL old -- which is
    what the caller had anyway. It must not cost the dry run."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    account.clear_market_hours_cache = lambda: (_ for _ in ()).throw(
        RuntimeError("cache is on fire"))
    _use_account(monkeypatch, account)
    _capture_errors(monkeypatch)

    _base_out, plan, _current, hours = page._solve_plan(
        account_id, mode=ALLOCATION_MODE_REBALANCE, labels=_alloc_labels(),
        scope_label=None, amount=0.0, allow_fractional=True,
        valuation_mode=VALUATION_MODE_COST, force_market_refresh=True)

    assert [r.symbol for r in plan.rows] == ['AAPL']
    assert hours.is_open is True
