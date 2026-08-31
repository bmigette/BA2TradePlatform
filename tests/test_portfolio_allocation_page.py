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
    get_allocation_config, get_managed_labels, get_symbol_rows, get_symbol_weights,
    remove_managed_label, replace_managed_labels, set_allocation_config,
    set_managed_label, set_symbol_weight,
)
from ba2_trade_platform.core.utils import add_label_to_instruments
from ba2_trade_platform.ui.pages import portfolio_allocation as page
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    DEFAULT_LABEL_ICON_COLOR as _DEFAULT_ICON_COLOR,
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


def _button_labels(root):
    """The caption of every ``ui.button``.

    ``_texts`` misses them: ``q-btn`` carries its caption in ``_props['label']``,
    not ``_text``, so an assertion that a button was NOT drawn is vacuous unless it
    looks here.
    """
    from nicegui import ui as nicegui_ui
    return [el._props.get('label', '') for el in root.descendants()
            if isinstance(el, nicegui_ui.button)]


def _tooltip_texts(root):
    """Every ``ui.tooltip``'s text. The label header's long clause lives here now."""
    from nicegui import ui
    return [el._text for el in root.descendants() if isinstance(el, ui.tooltip)]


def _expansion_headers(root):
    """The caption of every ``ui.expansion``.

    ``_texts`` misses them: an expansion keeps its caption in ``_props['label']``
    rather than in ``_text``, so the label group headers -- which is where the
    current-versus-target comparison is drawn -- are invisible to it.
    """
    from nicegui import ui as nicegui_ui
    return [el._props.get('label', '') for el in root.descendants()
            if isinstance(el, nicegui_ui.expansion)]


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


def _held(symbol, qty, cost_basis, market_value=None):
    """The same position as an OBJECT.

    ``portfolio_allocation_service.build_position_states`` reads attributes
    (``getattr(position, 'qty')``), while the page's own view path goes through
    ``positions_by_symbol``, which also accepts dicts. A dict handed to the service
    path silently reads as a FLAT position, so the wizard flow needs this one.
    """
    from types import SimpleNamespace
    return SimpleNamespace(symbol=symbol, qty=qty, cost_basis=cost_basis,
                           market_value=market_value)


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


def test_load_gate_for_an_expert_free_account_never_names_another_brokers_experts(
        monkeypatch):
    """The 2026-08 report, exactly: TastyTrade selected, TastyTrade owns ZERO
    experts, and the banner named the two ENABLED experts of the *Alpaca* account.

    Both accounts are built here, with Alpaca carrying the reported mix of enabled
    and disabled experts, so a gate that leaked across accounts would have to leak
    the right names to pass.
    """
    alpaca = create_account_definition(name='Alcapa Live', provider='MockAccount')
    tasty = create_account_definition(name='Tasty', provider='MockAccount')
    create_expert_instance(account_id=alpaca.id, expert='FMPPScreener', enabled=True)
    create_expert_instance(account_id=alpaca.id, expert='MockExpert', enabled=False,
                           alias='goal6-small_ED_S2')
    create_expert_instance(account_id=alpaca.id, expert='MockExpert', enabled=True,
                           alias='goal6-small_ED_S1top1')
    _use_account(monkeypatch, _Account(tasty.id, {'manual_trading_enabled': True}))

    gate = page._load_gate(tasty.id)

    assert gate.allowed is True
    assert gate.reason_code == GATE_OK
    assert gate.expert_names == []
    assert 'FMPPScreener' not in gate.message
    assert 'goal6-small_ED_S1top1' not in gate.message

    # ... and the account that DOES own them still reports exactly its own enabled two.
    _use_account(monkeypatch, _Account(alpaca.id, {'manual_trading_enabled': True}))
    owner_gate = page._load_gate(alpaca.id)
    assert owner_gate.reason_code == GATE_HAS_EXPERTS
    assert sorted(owner_gate.expert_names) == ['FMPPScreener', 'goal6-small_ED_S1top1']


def test_content_for_an_expert_free_account_never_draws_another_brokers_experts(
        monkeypatch, nicegui_client):
    """The same case through the real ``content()``, i.e. what the user actually saw.

    ``content()`` reads the selection ONCE, at build; given the TastyTrade id it
    must never render Alpaca's expert names. (When the page shows them anyway, the
    id it was built with was the previous one -- see
    ``tests/test_ui_account_switch_reload.py``.)
    """
    alpaca = create_account_definition(name='Alcapa Live', provider='MockAccount')
    tasty = create_account_definition(name='Tasty', provider='MockAccount')
    create_expert_instance(account_id=alpaca.id, expert='FMPPScreener', enabled=True)
    create_expert_instance(account_id=alpaca.id, expert='MockExpert', enabled=True,
                           alias='goal6-small_ED_S1top1')
    monkeypatch.setattr(page, 'get_selected_account_id', lambda: tasty.id)
    _use_account(monkeypatch, _Account(tasty.id, {'manual_trading_enabled': True},
                                       positions=[]))

    _run_in_client(nicegui_client, page.content)

    text = ' '.join(_texts(nicegui_client.layout))
    assert 'FMPPScreener' not in text
    assert 'goal6-small_ED_S1top1' not in text
    assert 'has enabled experts' not in text
    assert 'No labels are managed for this account yet' in text   # got past the gate


def test_expert_management_and_the_allocation_gate_read_the_same_enabled_field(
        monkeypatch, nicegui_client, account_id):
    """Ruled out, not dropped: the ENABLED tick and the gate are one field.

    The first hypothesis for the report was that Settings' Expert Management table
    and ``_enabled_expert_names`` disagreed about what "enabled" means. They do not
    -- the table row is ``dict(ExpertInstance)``, the column renders that row's
    ``enabled`` key, and the gate filters ``ExpertInstance.enabled == True`` -- so a
    green tick and a named expert can never contradict each other. The whole table
    is rendered here rather than just the row builder, because the column-to-field
    mapping is the half that decides which value the tick actually shows.
    """
    from ba2_trade_platform.ui.pages import settings as settings_page

    create_expert_instance(account_id=account_id, expert='FMPPScreener', enabled=True)
    create_expert_instance(account_id=account_id, expert='MockExpert', enabled=False,
                           alias='Retired')
    _use_account(monkeypatch, _Account(account_id, {'manual_trading_enabled': True}))

    tab = object.__new__(settings_page.ExpertSettingsTab)
    with nicegui_client:
        settings_page.ExpertSettingsTab.render(tab)

    column = next(c for c in tab.experts_table.columns if c['name'] == 'enabled')
    assert column['field'] == 'enabled'          # the tick reads ExpertInstance.enabled

    # ... and a TRUE value is the green tick, not the red cross. Inverting this
    # template is the one way the screen could contradict the gate.
    template = ' '.join(tab.experts_table.slots['body-cell-enabled'].template.split())
    assert "props.value ? 'check_circle' : 'cancel'" in template
    assert "props.value ? 'green' : 'red'" in template

    ticked = {(r['alias'] or r['expert']) for r in tab.experts_table.rows
              if r['account_id'] == account_id and r[column['field']]}

    assert ticked == set(page._enabled_expert_names(account_id)) == {'FMPPScreener'}
    assert 'Retired' not in ticked
    assert 'Retired' not in page._enabled_expert_names(account_id)


def test_load_gate_with_no_account_selected_asks_for_one(monkeypatch):
    """'All accounts' is account_id None, and no broker call may be made for it.

    Raising from the factory is not enough on its own: ``_load_gate`` catches
    everything the factory throws and still ends up at GATE_NO_ACCOUNT, so the call
    is counted rather than only booby-trapped.
    """
    import ba2_trade_platform.core.utils as core_utils
    calls = []

    def _explode(account_id):
        calls.append(account_id)
        raise AssertionError('the account must not be instantiated for "All accounts"')

    monkeypatch.setattr(core_utils, 'get_account_instance_from_id', _explode)
    gate = page._load_gate(None)
    assert gate.allowed is False
    assert gate.reason_code == GATE_NO_ACCOUNT
    assert calls == []


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

def _payload(views, mode=VALUATION_MODE_COST, *, base_notional=None,
             available_buying_power=None, unallocated_pct=0.0,
             account_value=None):
    return {'views': views, 'symbols_by_label': {}, 'valuation_mode': mode,
            'base_notional': base_notional,
            'available_buying_power': available_buying_power,
            'account_value': account_value,
            'unallocated_pct': unallocated_pct}


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
    """A blocked gate renders the banner and NOTHING else.

    Not merely cosmetic: without the early ``return`` the page goes on to load the
    valuation mode and refresh, which asks the broker for positions on an account
    the gate just refused to manage.
    """
    monkeypatch.setattr(page, 'get_selected_account_id', lambda: account_id)
    account = _use_account(monkeypatch, _Account(account_id,
                                                 {'manual_trading_enabled': False},
                                                 positions=[_pos('AAPL', 10, 6000.0)],
                                                 prices={'AAPL': 800.0}))
    set_managed_label(account_id, 'ARK26', target_pct=60.0)
    add_label_to_instruments(['AAPL'], 'ARK26')

    _run_in_client(nicegui_client, page.content)

    text = ' '.join(_texts(nicegui_client.layout))
    assert 'not available for this selection' in text
    assert 'Manually traded account' in text
    assert page.REVIEW_BUTTON_LABEL not in _button_labels(
        nicegui_client.layout)                                      # no toolbar
    assert 'ARK26' not in text                                      # no body
    assert account.quote_requests == []      # and the broker was never asked


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
    """Allocate -> dry run -> Submit, with only the broker faked.

    NO target step in between: it moved onto the page, so pressing Allocate solves
    at once against what the page last saved. Drives the page's real closures
    (``_run_dry_run``, ``_do_submit``) rather than re-implementing them, so a drift
    anywhere between the page, the wizard and the service surfaces here.
    """
    from ba2_trade_platform.core.portfolio_allocation_store import get_recent_runs

    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)
    _capture_notifications(monkeypatch)
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=100.0)

    # The dialog opener is captured rather than drawn: what is under test is the
    # page's glue and the service beneath it, and the wizard's own rendering
    # already has tests/test_portfolio_allocation_wizard_ui.py.
    opened, pending = {}, []
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.update(kw, base=a[0], plan=a[1]))
    # The page uses ui.timer only to hop off a sync handler; queue the callback
    # instead so the test can await it with the client's slot stack active.
    monkeypatch.setattr(page.ui, 'timer',
                        lambda _delay, callback, once=False: pending.append(callback))

    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_COST, _noop_refresh))

    assert opened['market'].allowed is True
    assert [r.symbol for r in opened['plan'].rows] == ['AAPL']

    # ...and Submit.
    opened['on_submit'](opened['plan'])
    _run_in_client(nicegui_client, pending.pop())

    assert [s[0] for s in account.submitted] == ['AAPL']
    runs = get_recent_runs(account_id)
    assert len(runs) == 1 and runs[0].order_ids


def test_pressing_allocate_opens_NO_dialog_before_the_dry_run(monkeypatch,
                                                              nicegui_client,
                                                              account_id):
    """The guard that step 1 does not come back through the page.

    A second place to type a target is a second answer to "what am I aiming at",
    and the two screens derived every one of those figures independently.
    """
    from nicegui import ui

    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)
    _capture_notifications(monkeypatch)
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=100.0)
    opened = {}
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.update(kw, base=a[0], plan=a[1]))

    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_COST, _noop_refresh))

    # The dry run was reached...
    assert 'plan' in opened
    # ...and nothing was drawn on the way: no dialog, no percentage box.
    drawn = list(nicegui_client.layout.descendants())
    assert [el for el in drawn if isinstance(el, ui.dialog)] == []
    assert [el for el in drawn if isinstance(el, ui.number)] == []
    assert not hasattr(page, 'open_allocation_steps')


# ---------------------------------------------------------------------------
# THE RUN PERSISTS WHAT IT WAS LAUNCHED WITH
#
# There is no Continue any more: the target step moved onto the page and pressing
# Allocate solves at once. The three writes it used to make on Continue happen at
# the moment the DRY RUN opens instead, which is the same instant for the same
# reason -- "last" means the numbers the user last chose to allocate with, whether
# or not they went through with the orders.
#
# On a REBALANCE the page has already persisted the targets inline, so these are
# normally a re-write of what is stored. What the run still earns is the EXPLICIT
# symbol rows.
# ---------------------------------------------------------------------------

def _drive_the_flow(monkeypatch, nicegui_client, account_id, *,
                    mode=ALLOCATION_MODE_REBALANCE, scope_label=None, amount=0.0,
                    valuation_mode=VALUATION_MODE_MARKET, invest_edit=None,
                    positions=None, prices=None):
    """Press Allocate and let the flow run to the dry run. Returns its kwargs.

    A REBALANCE opens no dialog at all. An INVEST run still opens the scope dialog,
    which is captured rather than drawn and driven through its ``on_dry_run``.
    """
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=list(positions or []),
                            prices=dict(prices or {'AAPL': 100.0, 'MSFT': 100.0}))
    _use_account(monkeypatch, account)
    _capture_notifications(monkeypatch)

    opened, scope, pending = {}, {}, []
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.update(kw, base=a[0], plan=a[1]))
    monkeypatch.setattr(page, 'open_invest_scope',
                        lambda *a, **kw: scope.update(kw, base=a[0], labels=a[1]))
    monkeypatch.setattr(page.ui, 'timer',
                        lambda _delay, callback, once=False: pending.append(callback))

    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, valuation_mode, _noop_refresh, mode=mode, invest_amount=amount))
    if mode == ALLOCATION_MODE_INVEST_LABEL:
        if invest_edit is not None:
            invest_edit(scope['labels'])
        scope['on_dry_run'](mode=mode, labels=scope['labels'],
                            scope_label=scope_label, amount=amount,
                            allow_fractional=True, unallocated_pct=0.0)
        _run_in_client(nicegui_client, pending.pop())
    return opened


def test_the_run_keeps_the_targets_the_page_saved(monkeypatch, nicegui_client,
                                                  account_id):
    """ONE source of truth. The page wrote 70/30; opening the dry run must not
    restate them as anything else."""
    from ba2_trade_platform.core.portfolio_allocation_store import get_managed_labels

    set_managed_label(account_id, 'ARK26', target_pct=70.0)
    set_managed_label(account_id, 'TECH', target_pct=30.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    add_label_to_instruments(['MSFT'], 'TECH')

    _drive_the_flow(monkeypatch, nicegui_client, account_id)

    assert {row.label: row.target_pct for row in get_managed_labels(account_id)} == {
        'ARK26': 70.0, 'TECH': 30.0}


def test_the_run_makes_a_silently_defaulted_symbol_weight_EXPLICIT(
        monkeypatch, nicegui_client, account_id):
    """A symbol with no stored row is showing a DEFAULT -- its actual share of the
    label. The user has just allocated real money with that number, so the run
    writes it down, and what it writes is what the page was showing."""
    from ba2_trade_platform.core.portfolio_allocation_store import get_symbol_rows

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL', 'MSFT'], 'ARK26')
    assert get_symbol_rows(account_id, 'ARK26') == {}

    _drive_the_flow(monkeypatch, nicegui_client, account_id,
                    positions=[_held('AAPL', 10, 600.0, 600.0),
                               _held('MSFT', 10, 400.0, 400.0)],
                    prices={'AAPL': 60.0, 'MSFT': 40.0})

    stored = get_symbol_rows(account_id, 'ARK26')
    assert {s: row.weight_pct for s, row in stored.items()} == {'AAPL': 60.0,
                                                                'MSFT': 40.0}


def test_the_run_does_NOT_consume_a_generation_when_nothing_changed(
        monkeypatch, nicegui_client, account_id):
    """``save_allocation_targets`` shifts the previous generation on a CHANGE only.
    A run launched against numbers the page already stored changes nothing, so
    "Load last" keeps pointing at the run before it rather than at itself."""
    from ba2_trade_platform.core.portfolio_allocation import LabelTarget, SymbolTarget
    from ba2_trade_platform.core.portfolio_allocation_store import (
        get_managed_labels, save_allocation_targets)

    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    save_allocation_targets(account_id, [LabelTarget(
        'ARK26', 70.0, [SymbolTarget('AAPL', 100.0)])])
    assert get_managed_labels(account_id)[0].previous_target_pct == 40.0

    _drive_the_flow(monkeypatch, nicegui_client, account_id)

    assert get_managed_labels(account_id)[0].previous_target_pct == 40.0


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

    _drive_the_flow(monkeypatch, nicegui_client, account_id,
                    mode=ALLOCATION_MODE_INVEST_LABEL, scope_label='ARK26',
                    amount=1_000.0, invest_edit=_edit)

    assert get_managed_labels(account_id)[0].target_pct == 25.0
    assert get_symbol_weights(account_id, 'ARK26', ['AAPL', 'MSFT']) == {
        'AAPL': 90.0, 'MSFT': 10.0}


def test_a_label_unmanaged_mid_flight_is_reported_rather_than_dropped_silently(
        monkeypatch, nicegui_client, account_id):
    """The flow holds the labels it loaded. If one is unmanaged in another tab
    meanwhile, ``save_allocation_targets`` refuses to re-create it -- correct, but
    the user must be told, or they come back tomorrow to numbers they did not
    choose and no record of why.

    The window is narrow now that there is no dialog in the middle of it, so the
    race is injected at the one point inside it that the flow calls first.
    """
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')

    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)
    notes = _capture_notifications(monkeypatch)
    monkeypatch.setattr(page, 'open_allocation_wizard', lambda *a, **kw: None)
    # ``_persist_choices`` calls this first; the label goes away underneath it.
    monkeypatch.setattr(page.svc, 'remember_fractional_choice',
                        lambda _account_id, _flag: remove_managed_label(
                            account_id, 'ARK26'))

    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_MARKET, _noop_refresh))

    assert any('no longer managed' in text for text, _ in notes), notes


def test_a_failed_target_save_does_not_stop_the_dry_run(monkeypatch, nicegui_client,
                                                        account_id):
    """Persisting is a convenience; SOLVING is what the user pressed Allocate for.
    A DB error here must be reported and then got out of the way, not turned into
    "the dry run would not open"."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    monkeypatch.setattr(page, 'save_allocation_targets',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('disk full')))

    opened = {}
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)
    notes = _capture_notifications(monkeypatch)
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.update(kw, base=a[0], plan=a[1]))

    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_MARKET, _noop_refresh))

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
    assert {'open_allocation_wizard', 'open_invest_scope',
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

def _open_the_wizard(monkeypatch, nicegui_client, account, account_id, *,
                     unallocated_pct=0.0):
    """Run the Allocate flow up to the dry run and return the wizard's kwargs.

    The reserve is STORED, not typed into a dialog on the way past: the box moved
    onto the page, so the flow reads it back out of the config row.
    """
    from ba2_trade_platform.core.portfolio_allocation_store import set_allocation_config

    _use_account(monkeypatch, account)
    _capture_notifications(monkeypatch)
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    # A SAVED share. The book here is flat, and an unsaved share now defaults to
    # the symbol's ACTUAL one -- which on a flat book is a real 0%, so the plan
    # would correctly buy nothing and there would be no dry run to inspect.
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=100.0)
    if unallocated_pct:
        set_allocation_config(account_id, unallocated_pct=unallocated_pct)
    opened = {}
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.update(kw, base=a[0], plan=a[1]))
    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_COST, _noop_refresh))
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


# ---------------------------------------------------------------------------
# W2: the wizard opens knowing what the last run allocated with.
# ---------------------------------------------------------------------------

def test_the_flow_opens_with_the_previous_generation_attached(monkeypatch, account_id):
    """``_load_flow_inputs`` still carries it, even though nothing in the flow
    reads it any more: it travels on the same ``LabelTarget`` the solve uses, and
    the PAGE's own Load-last reads the identical columns through
    ``_load_view_payload``. A loader that dropped it here would be the first half
    of dropping it there."""
    from ba2_trade_platform.core.portfolio_allocation_store import save_allocation_targets
    from ba2_trade_platform.core.portfolio_allocation import LabelTarget, SymbolTarget

    set_managed_label(account_id, 'ARK26', target_pct=60.0)
    add_label_to_instruments(['AAPL', 'MSFT'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=30.0)
    set_symbol_weight(account_id, 'ARK26', 'MSFT', weight_pct=70.0)
    save_allocation_targets(account_id, [LabelTarget(
        'ARK26', 80.0, [SymbolTarget('AAPL', 55.0), SymbolTarget('MSFT', 45.0)])])

    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0, 'MSFT': 100.0})
    _use_account(monkeypatch, account)

    _base, labels, _frac, _reserve = page._load_flow_inputs(
        account_id, VALUATION_MODE_MARKET)

    assert labels[0].target_pct == 80.0
    assert labels[0].previous_target_pct == 60.0
    by_symbol = {st.symbol: st for st in labels[0].symbols}
    assert (by_symbol['AAPL'].weight_pct, by_symbol['AAPL'].previous_weight_pct) == (55.0, 30.0)
    assert (by_symbol['MSFT'].weight_pct, by_symbol['MSFT'].previous_weight_pct) == (45.0, 70.0)


def test_a_label_with_no_history_loads_with_no_previous_target(monkeypatch, account_id):
    """NULL travels as None, so "never allocated" stays distinguishable from
    "allocated nothing" all the way to the page's Load-last button."""
    set_managed_label(account_id, 'ARK26', target_pct=60.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)

    _base, labels, _frac, _reserve = page._load_flow_inputs(
        account_id, VALUATION_MODE_MARKET)

    assert labels[0].previous_target_pct is None
    assert labels[0].symbols[0].previous_weight_pct is None


def test_the_flow_no_longer_computes_the_dialogs_display_only_maps(monkeypatch,
                                                                   account_id):
    """``symbol_values`` and ``positions`` existed for the wizard's step-1/step-2
    captions and had no other consumer. Those captions are on the PAGE now, built
    from ``_load_view_payload``'s own read, so a display-only value threaded
    through the SOLVE path is a passenger waiting to be mistaken for an input."""
    import inspect as _inspect

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[_held('AAPL', 10, 1000.0, 1100.0)],
                            prices={'AAPL': 250.0})
    _use_account(monkeypatch, account)

    loaded = page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)

    assert len(loaded) == 4
    assert 'symbol_values' not in _inspect.getsource(page._load_flow_inputs).split(
        '"""')[2]


# ---------------------------------------------------------------------------
# W3: the page learns about buying power.
# ---------------------------------------------------------------------------

def test_the_page_reads_the_account_snapshot_so_it_can_show_free_buying_power(
        monkeypatch, account_id):
    """``_load_view_payload`` fetched only positions and prices, so the page could
    not show cash or buying power at all -- and therefore could not show the
    unallocated group requirement 3 asks for."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            buying_power=7_500.0,
                            positions=[_pos('AAPL', 10, 1000.0, 2500.0)],
                            prices={'AAPL': 250.0})
    _use_account(monkeypatch, account)

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    assert payload['available_buying_power'] == 7_500.0
    assert payload['base_notional'] == 10_000.0          # 7,500 + 2,500 managed


def test_the_view_payload_reads_the_stored_reserve_and_scales_the_targets_by_it(
        monkeypatch, account_id):
    """``_load_view_payload`` is the ONLY place the reserve reaches the page's own
    table. Without it every target money figure on screen is the un-reserved one,
    and the page disagrees with the dry run about the same plan."""
    from ba2_trade_platform.core.portfolio_allocation_store import set_allocation_config

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=100.0)
    set_allocation_config(account_id, unallocated_pct=20.0)
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[_pos('AAPL', 10, 1000.0, 2500.0)],
                            prices={'AAPL': 250.0}, buying_power=7_500.0)
    _use_account(monkeypatch, account)

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    assert payload['unallocated_pct'] == 20.0
    assert payload['base_notional'] == 10_000.0          # GROSS, unchanged
    view = payload['views'][0]
    assert view.target_pct == 100.0                      # what the user typed
    assert view.target_value == 8_000.0                  # ...of the 8,000 left
    assert view.rows[0].target_value == 8_000.0          # and the symbol column too
    assert view.pct_of_base == 25.0                      # current 2,500 / GROSS base


def test_an_account_that_reserves_nothing_reports_a_zero_not_a_missing_key(
        monkeypatch, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 250.0})
    _use_account(monkeypatch, account)

    assert page._load_view_payload(account_id, VALUATION_MODE_MARKET)['unallocated_pct'] == 0.0


def test_a_snapshot_the_broker_will_not_give_leaves_the_page_usable(monkeypatch,
                                                                    account_id):
    """The label table is the page's job; buying power is a bonus on top of it. A
    broker that cannot answer must cost the reserve row, not the whole page."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[_pos('AAPL', 10, 1000.0, 2500.0)],
                            prices={'AAPL': 250.0})
    account.get_account_snapshot = lambda: (_ for _ in ()).throw(RuntimeError('503'))
    _use_account(monkeypatch, account)

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    assert payload['available_buying_power'] is None
    assert payload['base_notional'] is None
    assert [v.label for v in payload['views']] == ['ARK26']


def test_the_page_draws_the_STORED_reserve_first_with_percent_and_dollars(
        nicegui_client):
    """The row reports ``unallocated_pct``, not ``100 - sum(targets)``. Labels
    totalling 100 with a 25% reserve is the ordinary case, and a derived row would
    report 0 on it."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    positions = positions_by_symbol([_pos('AAPL', 10, 1000.0, 2500.0)])
    views = build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                              positions, {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0, unallocated_pct=25.0)
    with nicegui_client:
        page._render_labels(1, _payload(views, VALUATION_MODE_MARKET,
                                        base_notional=10_000.0,
                                        available_buying_power=7_500.0,
                                        unallocated_pct=25.0),
                            _noop_refresh)
        texts = _texts(nicegui_client.layout)

    # 'free buying power' rather than 'Unallocated': the editable reserve CARD in
    # the stat row is also called "Unallocated reserve", and it is a different thing
    # (a control) from this line (a measurement).
    row = next(t for t in texts if 'free buying power' in t)
    assert '25.00%' in row              # the STORED reserve
    assert '2,500.00' in row            # 25% of the base
    assert '7,500.00' in row            # what is ACTUALLY uninvested


def test_the_page_label_header_targets_what_the_reserve_LEFT(nicegui_client):
    """The header's money must be the money the engine would use, or the page and
    the dry run disagree about the same plan."""
    views = build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                              {}, {}, valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0, unallocated_pct=25.0)
    with nicegui_client:
        page._render_labels(1, _payload(views, VALUATION_MODE_MARKET,
                                        base_notional=10_000.0,
                                        available_buying_power=7_500.0,
                                        unallocated_pct=25.0),
                            _noop_refresh)
        header = ' | '.join(_expansion_headers(nicegui_client.layout))

        tips = _tooltip_texts(nicegui_client.layout)

    # The header leads with what the user TYPED and marks the of-base figure as
    # derived: 100% of what a 25% reserve leaves IS 75% of the base, and the two
    # are printed in that order so the primary number is the one they can act on.
    assert 'target 100.0% (real 75.0%)' in header
    # The clause the header used to repeat on every row moved to the ⓘ tooltip. It
    # did not go away: the percentage the user typed and the money it comes to are
    # both still one hover away.
    tip = next(t for t in tips if 'Portfolio target' in t)
    assert '100.0% of what the reserve leaves' in tip
    assert '$7,500.00' in tip


def test_the_page_shows_free_buying_power_as_a_third_stat_card(nicegui_client):
    views = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                              {}, {}, valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0)
    with nicegui_client:
        page._render_labels(1, _payload(views, VALUATION_MODE_MARKET,
                                        base_notional=10_000.0,
                                        available_buying_power=7_500.0),
                            _noop_refresh)
        texts = ' | '.join(_texts(nicegui_client.layout))

    assert 'Free buying power' in texts
    assert '$7,500.00' in texts


# ---------------------------------------------------------------------------
# THE ACCOUNT VALUE CARD
#
# 'Managed value' is the market value of the managed positions, which on a
# margin account exceeds the account's own equity -- $4,853.48 of positions
# against roughly $2,400 of account value on the reporting user's book. The
# page showed only the first, so it described an account twice its real size.
#
# The decisions (which snapshot field, and what an unknown renders as) are in
# ``ui/utils/portfolio_allocation_view.py``; these are the wiring tests.
# ---------------------------------------------------------------------------

def test_the_view_payload_carries_the_accounts_own_value(monkeypatch, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            cash=2_511.90, buying_power=170.31,
                            positions=[_pos('AAPL', 10, 1000.0, 4853.48)],
                            prices={'AAPL': 485.348})
    _use_account(monkeypatch, account)

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    # ``_AllocAccount`` mirrors net_liquidation onto ``cash`` exactly as both live
    # adapters mirror equity onto net_liquidation.
    assert payload['account_value'] == 2_511.90
    # ...and it is NOT the managed value, which is the whole complaint.
    assert payload['account_value'] != 4_853.48


def test_the_account_value_costs_no_second_broker_call(monkeypatch, account_id):
    """One snapshot per render. ``_load_view_payload`` already reads it for buying
    power; a second ``get_account_snapshot()`` here would double the REST cost of
    every refresh (and on Alpaca the second call is two endpoints, not one)."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[_pos('AAPL', 10, 1000.0, 2500.0)],
                            prices={'AAPL': 250.0})
    calls = []
    inner = account.get_account_snapshot
    account.get_account_snapshot = lambda: (calls.append(1), inner())[1]
    _use_account(monkeypatch, account)

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    assert len(calls) == 1
    assert payload['account_value'] == 50_000.0


def test_a_snapshot_the_broker_will_not_give_leaves_the_account_value_unknown(
        monkeypatch, account_id):
    """``None``, never 0.0 -- the page turns that into "n/a", and a 0.0 here would
    reach the card as a perfectly formatted $0.00."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[_pos('AAPL', 10, 1000.0, 2500.0)],
                            prices={'AAPL': 250.0})
    account.get_account_snapshot = lambda: (_ for _ in ()).throw(RuntimeError('503'))
    _use_account(monkeypatch, account)

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    assert payload['account_value'] is None
    assert [v.label for v in payload['views']] == ['ARK26']


def test_the_account_value_survives_a_base_notional_that_blows_up(monkeypatch,
                                                                  account_id):
    """The snapshot read and the base arithmetic share one ``try``. The account
    value is extracted FIRST, so a failure in the arithmetic below costs the
    reserve row -- which it always has -- and not the card as well."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            cash=2_511.90,
                            positions=[_pos('AAPL', 10, 1000.0, 2500.0)],
                            prices={'AAPL': 250.0})
    _use_account(monkeypatch, account)
    monkeypatch.setattr(page, 'compute_base_notional',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('boom')))

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    assert payload['account_value'] == 2_511.90
    assert payload['base_notional'] is None


def _account_value_views(managed_value):
    """One label holding one symbol worth exactly ``managed_value`` at cost."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol
    positions = positions_by_symbol([_pos('AAPL', 10, managed_value, managed_value)])
    return build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                             positions, {}, valuation_mode=VALUATION_MODE_COST)


def test_the_page_draws_the_account_value_beside_the_managed_value(nicegui_client):
    """Both numbers, in the same row, so the leverage is visible rather than
    implied. The reporting user's own figures."""
    with nicegui_client:
        page._render_labels(1, _payload(_account_value_views(4_853.48),
                                        account_value=2_511.90,
                                        available_buying_power=170.31),
                            _noop_refresh)
        texts = ' | '.join(_texts(nicegui_client.layout))

    assert 'Account value' in texts
    assert '$2,511.90' in texts
    assert '$4,853.48' in texts          # the managed card is untouched
    # And the multiple between them, said out loud -- that IS the user's question.
    assert '1.93x' in texts


def test_an_unknown_account_value_renders_n_a_with_a_reason_never_zero(
        nicegui_client):
    """THE regression. ``$0.00`` under 'Account value' reads as an account with
    nothing in it; this project has just fixed 25 instances of that pattern."""
    with nicegui_client:
        page._render_labels(1, _payload(_account_value_views(4_853.48),
                                        account_value=None),
                            _noop_refresh)
        texts = _texts(nicegui_client.layout)

    joined = ' | '.join(texts)
    assert 'Account value' in joined            # the line is still drawn
    assert '$0.00' not in joined
    # Read the figure's own three lines positionally, so nothing elsewhere on the
    # page can satisfy -- or spoil -- the assertion.
    index = texts.index('Account value')
    assert texts[index + 1] == _view_mod().FIGURE_UNKNOWN_TEXT == 'unknown'
    assert '$' not in texts[index + 1]
    assert '0' not in texts[index + 1]
    # ...and it says WHY, rather than leaving a bare "n/a".
    assert 'net liquidating value' in texts[index + 2]
    assert 'x this' not in joined               # and no leverage multiple


def test_an_account_genuinely_worth_zero_still_prints_the_zero(nicegui_client):
    """The inverse error: a fully withdrawn account IS worth $0.00, and hiding
    that behind "unavailable" reports an outage that did not happen."""
    with nicegui_client:
        page._render_labels(1, _payload(_account_value_views(0.0),
                                        account_value=0.0),
                            _noop_refresh)
        joined = ' | '.join(_texts(nicegui_client.layout))

    assert 'Account value' in joined
    assert '$0.00' in joined
    assert 'n/a' not in joined


def test_the_account_value_card_is_not_the_managed_value_again(nicegui_client):
    """A card wired to ``managed_total_value`` would look right on every fixture
    where the two happen to agree. They do not agree here, by construction."""
    with nicegui_client:
        page._render_labels(1, _payload(_account_value_views(4_853.48),
                                        account_value=2_511.90),
                            _noop_refresh)
        texts = _texts(nicegui_client.layout)

    # The card's own three lines, read positionally: caption, money, detail.
    index = texts.index('Account value')
    assert texts[index + 1] == '$2,511.90'
    assert texts[index + 1] != '$4,853.48'


def test_the_whole_page_shows_the_account_value_end_to_end(
        monkeypatch, nicegui_client, account_id):
    """Through the real ``content()``: snapshot -> payload -> card."""
    monkeypatch.setattr(page, 'get_selected_account_id', lambda: account_id)
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            cash=2_511.90, buying_power=170.31,
                            positions=[_pos('AAPL', 10, 1000.0, 4853.48)],
                            prices={'AAPL': 485.348})
    _use_account(monkeypatch, account)
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')

    _run_in_client(nicegui_client, page.content)

    text = ' '.join(_texts(nicegui_client.layout))
    assert 'Account value' in text
    assert '$2,511.90' in text
    assert '$4,853.48' in text


def test_the_page_omits_the_unallocated_group_when_the_broker_gave_no_base(
        nicegui_client):
    """Better a missing row than one measured against a guessed base."""
    views = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                              {}, {}, valuation_mode=VALUATION_MODE_MARKET)
    with nicegui_client:
        page._render_labels(1, _payload(views, VALUATION_MODE_MARKET), _noop_refresh)
        texts = ' | '.join(_texts(nicegui_client.layout))

    # The MEASUREMENT row is omitted. The editable reserve CARD stays -- it is a
    # control over a stored setting, not a figure derived from a base we do not
    # have, and it says so instead of printing a dollar amount.
    assert 'free buying power' not in texts
    assert 'no base notional yet' in texts


def test_the_reserve_and_the_labels_do_not_print_two_denominators_as_one_column(
        nicegui_client):
    """Base 10,000, reserve 25%, ONE label at 100%: the page rendered "target
    25.00%" directly above "target 100.0%", in identical grammar, in one list.

    They are not the same measurement. The reserve's is a share of the GROSS base;
    a label's is a RELATIVE weight on what the reserve LEFT. Read as one column
    they sum to 125% of a book that is fully described by 25 + 75. The wording now
    names each denominator, and the label additionally states the share of base its
    weight works out to -- which is the number that IS comparable with the reserve
    row above it and with its own "% of base" holding.
    """
    views = build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                              {}, {}, valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0, unallocated_pct=25.0)
    with nicegui_client:
        page._render_labels(1, _payload(views, VALUATION_MODE_MARKET,
                                        base_notional=10_000.0,
                                        available_buying_power=7_500.0,
                                        unallocated_pct=25.0),
                            _noop_refresh)
        reserve_row = next(t for t in _texts(nicegui_client.layout)
                           if 'free buying power' in t)
        header = ' | '.join(_expansion_headers(nicegui_client.layout))
        tips = _tooltip_texts(nicegui_client.layout)

    assert 'target 25.00% of base' in reserve_row, reserve_row
    # The reserve row is the ONE line still measured against the gross base, and it
    # says so; every label below divides the investable pool and says THAT. The two
    # denominators are NAMED rather than left to be inferred from matching grammar.
    assert 'of base' in reserve_row
    assert 'of investable' in header, header
    assert 'target 100.0% (real 75.0%)' in header, header
    assert any('100.0% of what the reserve leaves' in t for t in tips), tips


def test_a_base_of_exactly_zero_omits_the_reserve_row_instead_of_crashing(
        nicegui_client):
    """A brand-new or fully-withdrawn account renders the page, not a 500.

    The guard used to be ``base_notional is not None``, which a base of EXACTLY
    0.0 passes -- while ``unallocated_row`` sets ``pct_of_base`` to None for any
    FALSY base, because 0 is not a denominator. The two disagreed by one value and
    the row's ``f'{None:.1f}'`` raised, taking the whole page with it. 0.0 and None
    mean the same thing here (there is no base to divide by) and must be treated
    the same, exactly as ``build_label_views`` already treats them.
    """
    views = build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                              {}, {}, valuation_mode=VALUATION_MODE_MARKET)
    with nicegui_client:
        page._render_labels(1, _payload(views, VALUATION_MODE_MARKET,
                                        base_notional=0.0,
                                        available_buying_power=0.0,
                                        unallocated_pct=10.0),
                            _noop_refresh)
        texts = ' | '.join(_texts(nicegui_client.layout))

    assert 'free buying power' not in texts
    # ...and the rest of the page is still there.
    assert 'Managed labels' in texts


def test_the_label_header_measures_current_and_target_against_ONE_denominator(
        nicegui_client):
    """The defect: the header read ``(X% of managed, target Y%)`` while
    ``pct_of_total`` divides by the managed value and ``target_pct`` divides by
    buying power PLUS managed value. With 7,500 of buying power those are 100% and
    40% of two different things, and the user is invited to compare them."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    positions = positions_by_symbol([_pos('AAPL', 10, 1000.0, 2500.0)])
    views = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                              positions, {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0)
    with nicegui_client:
        page._render_labels(1, _payload(views, VALUATION_MODE_MARKET,
                                        base_notional=10_000.0,
                                        available_buying_power=7_500.0),
                            _noop_refresh)
        header = ' | '.join(_expansion_headers(nicegui_client.layout))
        tips = _tooltip_texts(nicegui_client.layout)

    assert '25.0% of investable' in header   # 2,500 of a 10,000 pool (no reserve)
    assert 'target 40.0%' in header
    assert 'real' not in header              # at a 0% reserve the two coincide
    assert any('$4,000.00' in t for t in tips), tips   # the target, as money
    assert 'of managed' not in header


def test_the_header_falls_back_to_percent_of_managed_when_there_is_no_base(
        nicegui_client):
    """Without a base there is no comparable pair to draw, so it says which
    denominator it DID use rather than inventing one."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    positions = positions_by_symbol([_pos('AAPL', 10, 1000.0, 2500.0)])
    views = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                              positions, {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_MARKET)
    with nicegui_client:
        page._render_labels(1, _payload(views, VALUATION_MODE_MARKET), _noop_refresh)
        header = ' | '.join(_expansion_headers(nicegui_client.layout))

    assert 'of managed' in header
    assert 'of base' not in header


def test_the_symbol_table_shows_a_target_percentage_and_a_target_value(nicegui_client):
    """Requirement 2 at the instrument level. The page showed a target exactly once,
    on the group header, and never per symbol."""
    from nicegui import ui
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    positions = positions_by_symbol([_pos('AAPL', 10, 1000.0, 2500.0)])
    view = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                             positions, {'AAPL': 250.0},
                             valuation_mode=VALUATION_MODE_MARKET,
                             base_notional=10_000.0,
                             symbol_weights={'ARK26': {'AAPL': 100.0}})[0]
    with nicegui_client:
        page._render_label_body(1, view, _noop_refresh)
        table = next(el for el in nicegui_client.layout.descendants()
                     if isinstance(el, ui.table))

    columns = {c['name'] for c in table.columns}
    assert {'weight_pct', 'target_value'} <= columns
    assert table.rows[0]['weight_pct'] == 100.0
    assert table.rows[0]['target_value'] == 4_000.0


def test_a_symbol_with_no_saved_target_shows_its_ACTUAL_share(nicegui_client):
    """It used to show a blank, and before that the fair share. The lone member of
    a label holds all of it."""
    from nicegui import ui
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    positions = positions_by_symbol([_pos('AAPL', 10, 1000.0, 2500.0)])
    view = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                             positions, {'AAPL': 250.0},
                             valuation_mode=VALUATION_MODE_MARKET)[0]
    with nicegui_client:
        page._render_label_body(1, view, _noop_refresh)
        table = next(el for el in nicegui_client.layout.descendants()
                     if isinstance(el, ui.table))

    assert table.rows[0]['weight_pct'] == 100.0


def test_an_UNMEASURABLE_symbol_still_shows_a_blank_rather_than_a_zero(nicegui_client):
    """The blank did not go away, it moved to the case that still needs it: a
    price outage may not quietly write 0%, which is an instruction to sell out."""
    from nicegui import ui
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    positions = positions_by_symbol([_pos('DARK', 10, 1000.0, None)])
    view = build_label_views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['DARK']},
                             positions, {'DARK': None},
                             valuation_mode=VALUATION_MODE_MARKET)[0]
    with nicegui_client:
        page._render_label_body(1, view, _noop_refresh)
        table = next(el for el in nicegui_client.layout.descendants()
                     if isinstance(el, ui.table))

    assert table.rows[0]['weight_pct'] is None
    assert table.rows[0]['target_value'] is None


def test_the_page_feeds_the_stored_symbol_weights_into_the_view(monkeypatch,
                                                                account_id):
    """``_load_view_payload`` read only the COMMENTS off the symbol rows, so the
    weights the user typed never reached the table they are supposed to appear in."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    add_label_to_instruments(['AAPL', 'MSFT'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=75.0)
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            buying_power=7_500.0,
                            positions=[_pos('AAPL', 10, 1000.0, 2500.0)],
                            prices={'AAPL': 250.0, 'MSFT': 100.0})
    _use_account(monkeypatch, account)

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    by_symbol = {r.symbol: r for r in payload['views'][0].rows}
    assert by_symbol['AAPL'].weight_pct == 75.0
    assert by_symbol['AAPL'].target_value == 3_000.0     # 75% of 40% of 10,000


# ---------------------------------------------------------------------------
# W8: the editable reserve, end to end through the page's own glue.
# ---------------------------------------------------------------------------

def test_the_flow_solves_against_the_stored_reserve(monkeypatch, account_id):
    """``_load_flow_inputs`` is the only place it can reach the solve. Without it
    every run silently deploys the cash the page is holding back."""
    from ba2_trade_platform.core.portfolio_allocation_store import set_allocation_config

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_allocation_config(account_id, unallocated_pct=15.0)
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)

    *_rest, reserve = page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)

    assert reserve == 15.0


def test_solve_plan_sizes_a_rebalance_against_the_reserved_base(monkeypatch, account_id):
    """THE WORKED EXAMPLE, through the live page path rather than the engine alone.
    A 50,000 buying-power account with a 10% reserve deploys 45,000."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)

    _base, plan, _current, _hours = page._solve_plan(
        account_id, mode=ALLOCATION_MODE_REBALANCE, labels=_alloc_labels(),
        scope_label=None, amount=0.0, allow_fractional=False,
        valuation_mode=VALUATION_MODE_COST, unallocated_pct=10.0)

    assert plan.base_notional == 50_000.0
    assert plan.reserved_pct == 10.0
    assert plan.reserved_notional == 5_000.0
    assert plan.investable_notional == 45_000.0
    assert plan.total_buy_value == 45_000.0


def test_solve_plan_in_invest_mode_ignores_the_reserve_entirely(monkeypatch, account_id):
    """An invest run spends the amount the user named. 1,000 means 1,000, whatever
    the portfolio-level reserve says."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0})
    _use_account(monkeypatch, account)

    _base, plan, _current, _hours = page._solve_plan(
        account_id, mode=ALLOCATION_MODE_INVEST_LABEL, labels=_alloc_labels(),
        scope_label='ARK26', amount=1_000.0, allow_fractional=False,
        valuation_mode=VALUATION_MODE_COST, unallocated_pct=40.0)

    assert plan.total_buy_value == 1_000.0
    assert plan.reserved_pct == 0.0


def test_the_run_keeps_the_reserve_the_page_saved(monkeypatch, nicegui_client,
                                                  account_id):
    """The reserve is one of the numbers the run was launched with, so it is still
    written beside the targets and the fractional switch -- and since the box is
    on the page, what it writes is what the page already stored."""
    from ba2_trade_platform.core.portfolio_allocation_store import (
        get_allocation_config, set_allocation_config)

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_allocation_config(account_id, unallocated_pct=20.0)

    _drive_the_flow(monkeypatch, nicegui_client, account_id)

    assert get_allocation_config(account_id).unallocated_pct == 20.0


def test_an_invest_run_never_writes_a_reserve(monkeypatch, nicegui_client, account_id):
    """Its ``unallocated_pct`` is a hard 0, and storing that would ZERO a reserve
    the user set on the rebalance side -- the same reason an invest run does not
    save the label targets."""
    from ba2_trade_platform.core.portfolio_allocation_store import (
        get_allocation_config, set_allocation_config,
    )

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=100.0)
    set_allocation_config(account_id, unallocated_pct=25.0)

    _drive_the_flow(monkeypatch, nicegui_client, account_id,
                    mode=ALLOCATION_MODE_INVEST_LABEL, scope_label='ARK26',
                    amount=500.0)

    assert get_allocation_config(account_id).unallocated_pct == 25.0


def test_the_reserve_the_run_solves_with_is_the_stored_one(monkeypatch,
                                                           nicegui_client, account_id):
    """Read back at the moment the dry run opens, not carried in a dialog. A run
    that reset it to 0 would deploy a reserve the user set yesterday."""
    from ba2_trade_platform.core.portfolio_allocation_store import set_allocation_config

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_allocation_config(account_id, unallocated_pct=35.0)

    opened = _drive_the_flow(monkeypatch, nicegui_client, account_id)

    assert opened['plan'].reserved_pct == 35.0


def test_the_dry_run_the_wizard_receives_is_solved_with_the_reserve(
        monkeypatch, nicegui_client, account_id):
    """The reserve has to survive the hop from the stored config into the plan the
    user reviews. ``_load_flow_inputs`` reads it; ``_run_dry_run`` is the only
    thing that carries it into ``_solve_plan``."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0},
                            buying_power=10_000.0)
    opened = _open_the_wizard(monkeypatch, nicegui_client, account, account_id,
                              unallocated_pct=10.0)

    plan = opened['plan']
    assert plan.base_notional == 10_000.0
    assert plan.reserved_pct == 10.0
    assert plan.reserved_notional == 1_000.0
    assert plan.total_buy_value == 9_000.0


def test_the_wizards_refresh_re_solves_with_the_SAME_reserve(monkeypatch,
                                                             nicegui_client, account_id):
    """Refresh re-prices the book; it does not re-open the question of how much
    cash to hold. A re-solve that forgot the reserve would silently deploy it, and
    the row the user then submits would not be the one they approved."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0},
                            buying_power=10_000.0)
    opened = _open_the_wizard(monkeypatch, nicegui_client, account, account_id,
                              unallocated_pct=10.0)

    plan, _market = opened['on_refresh'](True)

    assert plan.reserved_pct == 10.0
    assert plan.reserved_notional == 1_000.0
    assert plan.total_buy_value == 9_000.0


# ---------------------------------------------------------------------------
# INLINE TARGET EDITING -- the page IS where targets are set now
#
# Before this, targets were only reachable inside the Allocate wizard, so on an
# account nobody had run the wizard on every label sat at target_pct = 0 while the
# symbol table cheerfully printed "TARGET % 20" (get_symbol_weights' even-split
# default) resolving to a TARGET VALUE of $0.00 -- 20% of a 0% label. The page
# displayed a plausible number that meant nothing.
#
# Everything here persists ON CHANGE. There is no Save button on this page and
# there cannot be one: switching the global account hard-reloads the document.
#
# The tests drive the REAL ``_render_labels``, not the label body in isolation,
# because the base notional and the cash reserve live on the page and the boxes
# live in the table -- an edit is only correct if the whole composition agrees.
# ---------------------------------------------------------------------------

def _swatch_for(root, hex_value):
    """The preset chip for one colour. Its whole content IS its background."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        resolve_label_icon_color)
    wanted = resolve_label_icon_color(hex_value).upper()
    for element in _marked(root, page.MARKER_COLOR_SWATCH):
        if (element._style or {}).get('background', '').upper() == wanted:
            return element
    raise AssertionError(f'no {hex_value} swatch was drawn')


def _tables(root):
    from nicegui import ui
    return [el for el in root.descendants() if isinstance(el, ui.table)]


def _numbers(root):
    from nicegui import ui
    return [el for el in root.descendants() if isinstance(el, ui.number)]


def _sliders(root):
    from nicegui import ui
    return [el for el in root.descendants() if isinstance(el, ui.slider)]


def _icons(root, name=None):
    from nicegui import ui
    return [el for el in root.descendants()
            if isinstance(el, ui.icon) and (name is None or el._props.get('name') == name)]


def _expansions(root):
    from nicegui import ui
    return [el for el in root.descendants() if isinstance(el, ui.expansion)]


def _drive(handler, arguments):
    """Call an event handler and, if it is a coroutine, run it to completion.

    NiceGUI schedules a coroutine handler on the RUNNING loop; there is none in a
    unit test, so ``handle_event`` would create the task and drop it, and the
    persistence under test would silently never happen. The arity rule is
    ``handle_event``'s own: a handler whose parameters all have defaults is called
    with none. ``asyncio.to_thread`` is inline here (``run_to_thread_inline``), so
    the DB work sees the test's database.
    """
    import inspect
    expects = any(p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
                  and p.default is p.empty
                  for p in inspect.signature(handler).parameters.values())
    result = handler(arguments) if expects else handler()
    if inspect.isawaitable(result):
        asyncio.run(_await(result))


async def _await(awaitable):
    return await awaitable


def _emit(element, event_name, args):
    """Fire one of an element's Quasar slot events, the way the browser would."""
    from nicegui.events import GenericEventArguments
    for listener in element._event_listeners.values():
        if listener.type == event_name:
            _drive(listener.handler,
                   GenericEventArguments(sender=element, client=None, args=args))
            return
    raise AssertionError(f"{event_name!r} is not wired on {element}")


def _fire(element, event_type='click'):
    """Invoke an element's own listener for ``event_type``, modifiers and all."""
    from nicegui.events import GenericEventArguments
    for listener in element._event_listeners.values():
        if listener.type.split('.')[0] == event_type:
            _drive(listener.handler,
                   GenericEventArguments(sender=element, client=None, args={}))
            return listener
    raise AssertionError(f"no {event_type!r} listener on {element}")


def _press(button):
    """Click a ``ui.button`` and run its (async) handler to completion.

    ``ui.button(on_click=...)`` does NOT store the callback as its listener:
    ``Button.on_click`` wraps it in ``handle_event``, which -- finding no running
    loop -- hands the coroutine to ``app.on_startup`` and returns. In a unit test it
    would therefore never run, and every assertion after the click would pass on the
    UNCHANGED page. ``handle_event`` also routes exceptions to
    ``app.handle_exception``, which swallows them, so a handler that blew up would
    look identical to one that did nothing.

    Both are intercepted here for the duration of the click: the queued coroutine is
    run, and an exception is re-raised instead of being logged into the void.
    """
    from nicegui import app, core

    queued, failures = [], []
    original_startup, original_handler = app.on_startup, core.app.handle_exception
    app.on_startup = queued.append
    core.app.handle_exception = failures.append
    try:
        _fire(button)
    finally:
        app.on_startup = original_startup
        core.app.handle_exception = original_handler
    for coro in queued:
        asyncio.run(_await(coro))
    if failures:
        raise failures[0]


def _drive_value(widget, value):
    """Type into a ``ui.number`` / drag a ``ui.slider``, and run what it fires.

    ``ValueElement`` does dispatch its change handlers synchronously, but they are
    coroutines here and NiceGUI hands those to ``background_tasks.create``, which
    needs a running loop -- so in a unit test the coroutine would be created and
    dropped and the persistence under test would silently never happen. The real
    setter still runs (so the widget ends up in the state the browser would leave it
    in); the handlers are then driven explicitly, exactly as ``_drive`` does for the
    table's slot events.
    """
    from nicegui.events import ValueChangeEventArguments

    previous = widget.value
    handlers = list(widget._change_handlers)
    widget._change_handlers = []
    try:
        widget.set_value(value)
    finally:
        widget._change_handlers = handlers
    for handler in handlers:
        _drive(handler, ValueChangeEventArguments(sender=widget, client=None,
                                                  value=widget.value,
                                                  previous_value=previous))


def _listener_types(element):
    return {listener.type for listener in element._event_listeners.values()}


def _views(labels, symbols_by_label, *, base=10_000.0, reserve=0.0, weights=None,
           prices=None, positions=None, previous_weights=None, company_names=None):
    """Build LabelViews the way ``_load_view_payload`` does, with live prices."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    every = sorted({s for syms in symbols_by_label.values() for s in syms})
    book = positions if positions is not None else [_pos(s, 10, 1000.0, 2500.0)
                                                    for s in every]
    quotes = prices if prices is not None else {s: 250.0 for s in every}
    return build_label_views(labels, symbols_by_label, positions_by_symbol(book),
                             quotes, valuation_mode=VALUATION_MODE_MARKET,
                             base_notional=base, unallocated_pct=reserve,
                             symbol_weights=weights,
                             symbol_previous_weights=previous_weights,
                             company_names=company_names)


def _draw(client, account_id, views, *, base=10_000.0, buying_power=1_000.0,
          reserve=0.0):
    """Render the label section exactly as ``_refresh`` does, and hand back the tree."""
    with client:
        page._render_labels(account_id,
                            _payload(views, VALUATION_MODE_MARKET, base_notional=base,
                                     available_buying_power=buying_power,
                                     unallocated_pct=reserve),
                            _noop_refresh)
    return client.layout


def _one_label(account_id, label='ARK26', target=40.0, symbols=('AAPL', 'MSFT'),
               *, base=10_000.0, reserve=0.0, weights=None, color=None,
               company_names=None):
    return _views([ManagedLabel(label, target, color=color)],
                  {label: list(symbols)}, base=base, reserve=reserve,
                  weights={label: dict(weights or {s: 100.0 / len(symbols)
                                                   for s in symbols})},
                  company_names=company_names)


# -- the symbol table's TARGET % column is an input --------------------------

def test_the_symbol_tables_target_column_is_editable(nicegui_client, account_id):
    """The headline change: "Edit should be inline, we should not click allocate,
    tables target should be editable"."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    table = _tables(root)[0]

    assert 'body-cell-weight_pct' in table.slots
    assert 'weightChange' in _listener_types(table)


def test_editing_a_symbols_target_persists_it_immediately(nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    _emit(_tables(root)[0], 'weightChange', ['AAPL', 75.0])

    assert get_symbol_rows(account_id, 'ARK26')['AAPL'].weight_pct == 75.0


def test_a_symbol_target_is_written_to_the_symbol_that_was_edited(nicegui_client,
                                                                  account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    _emit(_tables(root)[0], 'weightChange', ['MSFT', 75.0])

    stored = get_symbol_rows(account_id, 'ARK26')
    assert stored['MSFT'].weight_pct == 75.0
    assert 'AAPL' not in stored


def test_a_symbol_target_is_written_to_the_label_whose_table_it_came_from(
        nicegui_client, account_id):
    """Two labels holding the SAME symbol is the normal case here (the ⚠ row), so
    a table writing to the wrong label would be silently plausible."""
    set_managed_label(account_id, 'ARK26', target_pct=50.0)
    set_managed_label(account_id, 'HighRisk', target_pct=50.0)
    views = _views([ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
                   {'ARK26': ['AAPL'], 'HighRisk': ['AAPL']},
                   weights={'ARK26': {'AAPL': 100.0}, 'HighRisk': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    _emit(_tables(root)[1], 'weightChange', ['AAPL', 62.5])

    assert get_symbol_rows(account_id, 'HighRisk')['AAPL'].weight_pct == 62.5
    assert get_symbol_rows(account_id, 'ARK26') == {}


def test_the_edited_rows_target_value_recomputes_and_ONLY_the_edited_rows(
        nicegui_client, account_id):
    """"TARGET VALUE ... must update live as the user types" -- without this the
    user edits a number and nothing on screen moves. But ONLY that row moves now:
    "do not automatically recalculate ... Do not change other numbers."
    """
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    table = _tables(root)[0]
    before = {r['symbol']: r['target_value'] for r in table.rows}

    _emit(table, 'weightChange', ['AAPL', 75.0])

    assert before == {'AAPL': 2_000.0, 'MSFT': 2_000.0}
    assert {r['symbol']: r['target_value'] for r in table.rows} == \
        {'AAPL': 3_000.0, 'MSFT': 2_000.0}


def test_the_other_rows_displayed_target_does_NOT_follow_the_edit_any_more(
        nicegui_client, account_id):
    """THE inversion. Storing one weight changes what the unstored siblings would
    RESOLVE to, and the page used to re-read and redraw them -- so the user edited
    one number and the rest of the row rearranged itself. The set is now allowed to
    stop totalling 100, and "Fill 100%" is the deliberate way to put it back."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    table = _tables(root)[0]

    _emit(table, 'weightChange', ['AAPL', 75.0])

    assert {r['symbol']: r['weight_pct'] for r in table.rows} == \
        {'AAPL': 75.0, 'MSFT': 50.0}        # MSFT stayed exactly where it was


def test_a_symbol_target_edit_is_scaled_by_the_reserve_the_page_is_showing(
        nicegui_client, account_id):
    """The recompute must divide what the reserve LEFT, exactly as the first render
    did -- one factor, applied once."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id,
                 _one_label(account_id, target=100.0, reserve=20.0), reserve=20.0)
    table = _tables(root)[0]

    _emit(table, 'weightChange', ['AAPL', 75.0])

    # 75% of 80% of 10,000 for the edited row; MSFT keeps the 50% it was showing.
    assert {r['symbol']: r['target_value'] for r in table.rows} == \
        {'AAPL': 6_000.0, 'MSFT': 4_000.0}


def test_a_non_numeric_symbol_target_is_refused_and_nothing_is_written(
        monkeypatch, nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', 'abc'])

    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert any('not a number' in text for text, _type in sent)


def test_a_negative_symbol_target_is_refused(monkeypatch, nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', -5.0])

    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert any('below 0%' in text for text, _type in sent)


def test_a_symbol_target_above_100_is_refused(monkeypatch, nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', 150.0])

    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert any('above 100%' in text for text, _type in sent)


def test_a_cleared_symbol_target_is_refused_rather_than_stored_as_zero(
        monkeypatch, nicegui_client, account_id):
    """0 is "hold none of this" -- i.e. a sell. An emptied box is not that."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', None])

    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert any('empty' in text for text, _type in sent)


def test_a_refused_symbol_target_puts_the_cell_back_instead_of_leaving_it_dirty(
        monkeypatch, nicegui_client, account_id):
    """A rejected edit must not leave a number on screen the database does not
    have -- that is the exact defect this feature removes. The q-input is keyed on
    a per-row token, so bumping it remounts the cell from the row data."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    table = _tables(root)[0]
    before = dict(table.rows[0])

    _emit(table, 'weightChange', [before['symbol'], 150.0])

    assert table.rows[0]['weight_pct'] == before['weight_pct']
    assert table.rows[0]['target_value'] == before['target_value']
    assert table.rows[0]['weight_key'] == before['weight_key'] + 1
    assert ':key="props.row.weight_key"' in table.slots['body-cell-weight_pct'].template


def test_a_symbol_target_typed_into_a_stale_label_does_not_resurrect_it(
        monkeypatch, nicegui_client, account_id):
    """The label was unmanaged in another tab. Writing the weight would leave an
    orphan allocation row under a label the account no longer manages."""
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', 75.0])

    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert any('no longer managed' in text for text, _type in sent)


def test_a_failed_symbol_target_write_is_reported_not_raised(
        monkeypatch, nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    _capture_errors(monkeypatch)
    monkeypatch.setattr(page, 'set_symbol_weight',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('disk full')))
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', 75.0])

    assert any('disk full' in text for text, _type in sent)


def test_editing_a_symbol_target_does_not_touch_its_comment(nicegui_client, account_id):
    """The two writers share the row. A weight save that passed ``comment=''``
    would wipe the note on every keystroke."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=10.0, comment='keep')
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', 75.0])

    row = get_symbol_rows(account_id, 'ARK26')['AAPL']
    assert (row.weight_pct, row.comment) == (75.0, 'keep')


# -- the LABEL's own target, inline ------------------------------------------

def _target_box(root, label):
    """The ``Portfolio target %`` ``ui.number`` inside one label's body."""
    from nicegui import ui
    boxes = [el for el in root.descendants()
             if isinstance(el, ui.number)
             and el._props.get('label') == 'Portfolio target %']
    assert boxes, 'no label target box was drawn'
    return boxes[label] if isinstance(label, int) else boxes[0]


def test_the_label_header_carries_an_editable_target_box(nicegui_client, account_id):
    """"to edit label allocation" -- the box that used to be reachable only from
    inside the Allocate wizard."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    assert _target_box(root, 0).value == 40.0


def test_editing_a_label_target_persists_it_immediately(nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _drive_value(_target_box(root, 0), 55.0)

    assert get_managed_labels(account_id)[0].target_pct == 55.0


def test_a_label_target_is_written_to_the_label_that_was_edited(nicegui_client,
                                                                account_id):
    """Neither label is called ARK26, and the FIRST box is the one driven.

    Both choices are load-bearing and they close different holes. Driving the first
    box catches a closure that captured the loop variable and writes to the LAST
    label. Naming neither of them ARK26 -- the name every other test in this file
    uses -- catches a writer that has a label hardcoded: with an ARK26 in the
    fixture, "wrote to ARK26" and "wrote to the right one" are the same assertion.
    """
    set_managed_label(account_id, 'Alpha', target_pct=50.0)
    set_managed_label(account_id, 'Bravo', target_pct=50.0)
    views = _views([ManagedLabel('Alpha', 50.0), ManagedLabel('Bravo', 50.0)],
                   {'Alpha': ['AAPL'], 'Bravo': ['MSFT']},
                   weights={'Alpha': {'AAPL': 100.0}, 'Bravo': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    _drive_value(_target_box(root, 0), 30.0)

    stored = {row.label: row.target_pct for row in get_managed_labels(account_id)}
    assert stored == {'Alpha': 30.0, 'Bravo': 50.0}


def test_editing_a_label_target_recomputes_every_symbol_row_under_it(
        nicegui_client, account_id):
    """The user's example: set the label to 22% and all its symbols at 20% each must
    immediately show their share of THAT money."""
    set_managed_label(account_id, 'WHEEL_L1_HR', target_pct=0.0)
    symbols = ('A', 'B', 'C', 'D', 'E')
    views = _views([ManagedLabel('WHEEL_L1_HR', 0.0)], {'WHEEL_L1_HR': list(symbols)},
                   weights={'WHEEL_L1_HR': {s: 20.0 for s in symbols}})
    root = _draw(nicegui_client, account_id, views)
    table = _tables(root)[0]
    assert {r['target_value'] for r in table.rows} == {0.0}     # the reported defect

    _drive_value(_target_box(root, 0), 22.0)

    # 20% of 22% of a 10,000 base = 440 each.
    assert {r['target_value'] for r in table.rows} == {440.0}


def test_editing_a_label_target_recomputes_its_header_and_its_bar(
        nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _drive_value(_target_box(root, 0), 80.0)

    header = ' | '.join(_expansion_headers(root))
    assert 'target 80.0%' in header
    assert 'tgt 80.0%' in ' | '.join(_texts(root))


def test_a_label_target_that_would_take_the_set_over_100_is_refused(
        monkeypatch, nicegui_client, account_id):
    """THE guard, on the inline path. Without it the page saves a set the engine
    will not run and the user finds out two screens later."""
    set_managed_label(account_id, 'ARK26', target_pct=50.0)
    set_managed_label(account_id, 'HighRisk', target_pct=50.0)
    sent = _capture_notifications(monkeypatch)
    views = _views([ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
                   {'ARK26': ['AAPL'], 'HighRisk': ['MSFT']},
                   weights={'ARK26': {'AAPL': 100.0}, 'HighRisk': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    _drive_value(_target_box(root, 0), 60.0)

    stored = {row.label: row.target_pct for row in get_managed_labels(account_id)}
    assert stored == {'ARK26': 50.0, 'HighRisk': 50.0}
    assert any('over 100%' in text for text, _t in sent)


def test_a_refused_label_target_puts_the_box_back_to_the_stored_number(
        monkeypatch, nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=50.0)
    set_managed_label(account_id, 'HighRisk', target_pct=50.0)
    _capture_notifications(monkeypatch)
    views = _views([ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
                   {'ARK26': ['AAPL'], 'HighRisk': ['MSFT']},
                   weights={'ARK26': {'AAPL': 100.0}, 'HighRisk': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)
    box = _target_box(root, 0)

    _drive_value(box, 60.0)

    assert box.value == 50.0


def test_lowering_a_label_below_its_own_previous_value_is_never_refused(
        monkeypatch, nicegui_client, account_id):
    """The label being edited must not be counted twice, or an over-target set
    could never be brought back down."""
    set_managed_label(account_id, 'ARK26', target_pct=90.0)
    set_managed_label(account_id, 'HighRisk', target_pct=50.0)
    _capture_notifications(monkeypatch)
    views = _views([ManagedLabel('ARK26', 90.0), ManagedLabel('HighRisk', 50.0)],
                   {'ARK26': ['AAPL'], 'HighRisk': ['MSFT']},
                   weights={'ARK26': {'AAPL': 100.0}, 'HighRisk': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    _drive_value(_target_box(root, 0), 50.0)

    assert get_managed_labels(account_id)[0].target_pct == 50.0


def test_a_label_target_typed_into_a_stale_label_does_not_resurrect_it(
        monkeypatch, nicegui_client, account_id):
    """``set_managed_label`` CREATES the row. An edit made after the label was
    unmanaged elsewhere would bring it back holding money."""
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _drive_value(_target_box(root, 0), 55.0)

    assert get_managed_labels(account_id) == []
    assert any('no longer managed' in text for text, _t in sent)


def test_a_failed_label_target_write_is_reported_not_raised(monkeypatch,
                                                            nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    _capture_errors(monkeypatch)
    monkeypatch.setattr(page, 'set_managed_label',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('disk full')))
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _drive_value(_target_box(root, 0), 55.0)

    assert any('disk full' in text for text, _t in sent)


def test_editing_a_label_target_does_not_touch_its_comment(nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0, comment='growth sleeve')
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _drive_value(_target_box(root, 0), 55.0)

    row = get_managed_labels(account_id)[0]
    assert (row.target_pct, row.comment) == (55.0, 'growth sleeve')


def test_editing_a_label_target_does_not_touch_its_colour(nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0, color='#56B4E9')
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _drive_value(_target_box(root, 0), 55.0)

    assert get_managed_labels(account_id)[0].color == '#56B4E9'


def test_the_page_reports_a_label_set_that_does_not_total_100_without_a_dry_run(
        nicegui_client, account_id):
    """The wizard has always said this at step 1. The boxes moved to the page, so
    the advisory had to move with them."""
    views = _views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                   weights={'ARK26': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    assert any('under 100%' in t for t in _texts(root))


def test_the_running_total_follows_an_inline_edit(nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    views = _views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                   weights={'ARK26': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)
    assert any('under 100%' in t for t in _texts(root))

    _drive_value(_target_box(root, 0), 100.0)

    assert not any('under 100%' in t for t in _texts(root))


# -- the edit affordance beside the chevron ----------------------------------

def test_the_label_header_carries_an_edit_icon_beside_the_chevron(nicegui_client,
                                                                  account_id):
    """"to edit label allocation, add an icon aside the fold / unfold"."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    assert len(_icons(root, 'edit')) == 1


def test_the_edit_icon_opens_the_label_and_focuses_its_target_box(nicegui_client,
                                                                  account_id):
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    expansion = _expansions(root)[0]
    assert expansion.value is False

    _fire(_icons(root, 'edit')[0])

    assert expansion.value is True


def test_the_edit_icon_does_not_TOGGLE_the_fold(nicegui_client, account_id):
    """It is "edit this label", not a second chevron: pressing it on an already
    open section must not close it."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    icon = _icons(root, 'edit')[0]

    _fire(icon)
    _fire(icon)

    assert _expansions(root)[0].value is True


def test_the_edit_icons_click_is_stopped_from_reaching_the_header(nicegui_client,
                                                                  account_id):
    """Without ``.stop`` the same click bubbles to the header, Quasar folds the
    section, and the pencil appears to do nothing at all."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    listener = _fire(_icons(root, 'edit')[0])
    assert listener.type == 'click.stop'


def test_only_the_chevron_folds_the_header_now_that_it_holds_a_control(
        nicegui_client, account_id):
    """``expand-icon-toggle``: the header carries the pencil, and Quasar's default
    is that a click ANYWHERE on it toggles. Belt and braces with ``click.stop``."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    assert _expansions(root)[0]._props.get('expand-icon-toggle') is True


def test_the_edit_icon_targets_ITS_OWN_label(nicegui_client, account_id):
    views = _views([ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
                   {'ARK26': ['AAPL'], 'HighRisk': ['MSFT']},
                   weights={'ARK26': {'AAPL': 100.0}, 'HighRisk': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    # The FIRST pencil, deliberately: a closure that captured the loop variable
    # instead of the label would open the LAST section, and driving the last row
    # cannot tell the two apart.
    _fire(_icons(root, 'edit')[0])

    assert [e.value for e in _expansions(root)] == [True, False]


# -- the LABELS column, removed ----------------------------------------------

def test_the_symbol_table_no_longer_carries_a_labels_column(nicegui_client, account_id):
    """Redundant: you are inside that label's section and every row repeated the
    same value."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    assert 'labels' not in {c['name'] for c in _tables(root)[0].columns}


def test_the_row_still_CARRIES_its_labels_so_the_warning_tooltip_still_works(
        nicegui_client, account_id):
    """The ⚠ cell's ``:title`` reads ``props.row.labels`` and is now the ONLY place
    a symbol's other managed labels are named. Dropping the field with the column
    would have silently emptied it."""
    views = _views([ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
                   {'ARK26': ['AAPL'], 'HighRisk': ['AAPL']},
                   weights={'ARK26': {'AAPL': 100.0}, 'HighRisk': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)
    table = _tables(root)[0]

    assert table.rows[0]['labels'] == 'ARK26, HighRisk'
    assert table.rows[0]['flag'] == '⚠'
    assert 'props.row.labels' in table.slots['body-cell-flag'].template


def test_the_target_column_is_renamed_so_it_cannot_be_read_as_the_label_target():
    """7a. "TARGET % 20" under "target 0.0%" made the user ask which was wrong.
    Neither was: one is a share of the label, the other of the portfolio."""
    from ba2_trade_platform.ui.pages import portfolio_allocation as module
    import inspect
    source = inspect.getsource(module._render_label_body)
    assert "'label': 'Share of label %'" in source
    assert "'label': 'Target %'" not in source


# -- label colours -----------------------------------------------------------

def test_the_label_header_icon_is_tinted_with_the_labels_own_colour(nicegui_client,
                                                                    account_id):
    root = _draw(nicegui_client, account_id,
                 _one_label(account_id, color='#56B4E9'))
    icon = _icons(root, 'label')[0]
    assert '#56B4E9' in (icon._style.get('color') or '')


def test_a_label_with_no_colour_draws_the_neutral_default(nicegui_client, account_id):
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        DEFAULT_LABEL_ICON_COLOR)

    root = _draw(nicegui_client, account_id, _one_label(account_id))
    icon = _icons(root, 'label')[0]
    assert DEFAULT_LABEL_ICON_COLOR.lower() in (icon._style.get('color') or '').lower()


def test_two_labels_get_their_own_colours_not_the_last_one_seen(nicegui_client,
                                                                account_id):
    """The classic default-argument-capture bug: without ``c=view.color`` every icon
    would take the LAST label's colour."""
    views = _views([ManagedLabel('ARK26', 50.0, color='#56B4E9'),
                    ManagedLabel('HighRisk', 50.0, color='#D55E00')],
                   {'ARK26': ['AAPL'], 'HighRisk': ['MSFT']},
                   weights={'ARK26': {'AAPL': 100.0}, 'HighRisk': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    colours = [(i._style.get('color') or '') for i in _icons(root, 'label')]
    assert '#56B4E9' in colours[0]
    assert '#D55E00' in colours[1]


def test_the_bar_fill_is_tinted_with_the_labels_own_colour(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id,
                 _one_label(account_id, color='#009E73'))
    fills = [el for el in root.descendants()
             if '#009E73' in (el._style.get('background') or '')]
    assert fills, 'the mini-bar fill did not take the label colour'


def test_the_view_payload_carries_the_stored_colour_to_the_page(monkeypatch,
                                                                account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0, color='#F0E442')
    add_label_to_instruments(['AAPL'], 'ARK26')
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            buying_power=7_500.0,
                            positions=[_pos('AAPL', 10, 1000.0, 2500.0)],
                            prices={'AAPL': 250.0})
    _use_account(monkeypatch, account)

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    assert payload['views'][0].color == '#F0E442'


def test_the_picker_lists_each_managed_label_with_a_colour_control(monkeypatch,
                                                                   nicegui_client,
                                                                   account_id):
    from nicegui import ui

    set_managed_label(account_id, 'ARK26', target_pct=40.0, color='#56B4E9')
    with nicegui_client:
        page._open_label_picker(account_id, _noop_refresh)
        root = nicegui_client.layout
        swatches = _marked(root, page.MARKER_COLOR_SWATCH)
        customs = [el for el in root.descendants()
                   if isinstance(el, ui.color_input)]

    # SWATCHES, not a list of names: "Bluish green" beside "Vermillion" told the
    # user nothing about what either would look like.
    assert len(swatches) == len(page.LABEL_COLOR_PALETTE)
    assert customs and customs[0].value == '#56B4E9'
    assert _marked(root, page.MARKER_COLOR_CLEAR)      # the way back to no colour


def test_choosing_a_colour_in_the_picker_persists_it(monkeypatch, nicegui_client,
                                                     account_id):
    from nicegui import ui

    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    _capture_notifications(monkeypatch)
    with nicegui_client:
        page._open_label_picker(account_id, _noop_refresh)
        _fire(_swatch_for(nicegui_client.layout, '#D55E00'))

    assert get_managed_labels(account_id)[0].color == '#D55E00'


def test_a_colour_is_written_to_the_label_it_was_chosen_for(monkeypatch,
                                                            nicegui_client, account_id):
    """The FIRST row, and neither label is called ARK26 -- see the matching note on
    the label-target test. One catches a late-bound closure, the other a hardcoded
    label name."""
    from nicegui import ui

    set_managed_label(account_id, 'Alpha', target_pct=50.0)
    set_managed_label(account_id, 'Bravo', target_pct=50.0)
    _capture_notifications(monkeypatch)
    with nicegui_client:
        page._open_label_picker(account_id, _noop_refresh)
        # The FIRST label's chooser. Without the default-argument capture on every
        # handler, this click would recolour the last label instead.
        _fire(_marked(nicegui_client.layout, page.MARKER_COLOR_SWATCH)[5])

    stored = {row.label: row.color for row in get_managed_labels(account_id)}
    assert stored == {'Alpha': '#D55E00', 'Bravo': None}


def test_clearing_a_colour_in_the_picker_really_clears_it(monkeypatch, nicegui_client,
                                                          account_id):
    """``set_managed_label(color=None)`` means LEAVE UNCHANGED, so "No colour" has
    to travel as ``''`` -- otherwise the swatch is un-removable."""
    from nicegui import ui

    set_managed_label(account_id, 'ARK26', target_pct=40.0, color='#56B4E9')
    _capture_notifications(monkeypatch)
    with nicegui_client:
        page._open_label_picker(account_id, _noop_refresh)
        _fire(_marked(nicegui_client.layout, page.MARKER_COLOR_CLEAR)[0])

    assert get_managed_labels(account_id)[0].color is None


def test_choosing_a_colour_does_not_disturb_the_target(monkeypatch, nicegui_client,
                                                       account_id):
    from nicegui import ui

    set_managed_label(account_id, 'ARK26', target_pct=40.0, comment='growth')
    _capture_notifications(monkeypatch)
    with nicegui_client:
        page._open_label_picker(account_id, _noop_refresh)
        _fire(_swatch_for(nicegui_client.layout, '#D55E00'))

    row = get_managed_labels(account_id)[0]
    assert (row.target_pct, row.comment) == (40.0, 'growth')


def test_a_failed_colour_write_is_reported_not_raised(monkeypatch, nicegui_client,
                                                      account_id):
    from nicegui import ui

    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    _capture_errors(monkeypatch)
    with nicegui_client:
        page._open_label_picker(account_id, _noop_refresh)
        monkeypatch.setattr(page, 'set_managed_label',
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('nope')))
        _fire(_swatch_for(nicegui_client.layout, '#D55E00'))

    assert any('nope' in text for text, _t in sent)


# ---------------------------------------------------------------------------
# THE EDITABLE RESERVE
#
# "We also need an editable field here for the value we want to keep and not
# allocate, or maybe a slider like total allocation share." It was settable only
# inside the Allocate wizard -- the same complaint as the label targets -- so it is
# solved the same way: inline, persist-on-change, and everything it re-bases
# recomputes as it moves.
# ---------------------------------------------------------------------------

def _reserve_controls(root):
    from nicegui import ui
    slider = next(el for el in root.descendants() if isinstance(el, ui.slider))
    number = next(el for el in root.descendants()
                  if isinstance(el, ui.number) and el._props.get('suffix') == '%'
                  and el._props.get('label') != 'Portfolio target %')
    return slider, number


def test_the_reserve_is_editable_on_the_page_itself(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=25.0),
                 reserve=25.0)
    slider, number = _reserve_controls(root)
    assert slider.value == 25.0
    assert number.value == 25.0


def test_the_reserve_shows_its_dollar_figure_beside_the_slider(nicegui_client,
                                                               account_id):
    """The user asked for "the value we want to keep". The stored field is a
    PERCENT, so the money is derived and shown next to it."""
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=25.0),
                 reserve=25.0)
    assert any('$2,500.00 held back' in t for t in _texts(root))
    assert any('$7,500.00 investable' in t for t in _texts(root))


def test_dragging_the_reserve_slider_persists_it(nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id, target=100.0))
    slider, _number = _reserve_controls(root)

    _drive_value(slider, 40.0)

    assert get_allocation_config(account_id).unallocated_pct == 40.0


def test_typing_the_reserve_into_the_box_persists_it(nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id, target=100.0))
    _slider, number = _reserve_controls(root)

    _drive_value(number, 12.5)

    assert get_allocation_config(account_id).unallocated_pct == 12.5


def test_the_slider_and_the_box_follow_each_other(nicegui_client, account_id):
    """One stored field, two controls: editing either must move the other, and the
    echo must not write a second time."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id, target=100.0))
    slider, number = _reserve_controls(root)

    _drive_value(slider, 30.0)
    assert number.value == 30.0

    _drive_value(number, 12.5)
    assert slider.value == 12.5
    assert get_allocation_config(account_id).unallocated_pct == 12.5


def test_moving_the_reserve_recomputes_the_reserve_line(nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id, target=100.0))
    slider, _number = _reserve_controls(root)

    _drive_value(slider, 40.0)

    row = next(t for t in _texts(root) if 'free buying power' in t)
    assert 'target 40.00% of base = $4,000.00' in row
    assert any('$4,000.00 held back' in t for t in _texts(root))


def test_moving_the_reserve_recomputes_every_label_header(nicegui_client, account_id):
    """Raising the reserve re-bases every label's target. A header that did not
    follow would keep asserting a comparison that had stopped being true."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id, target=100.0))
    assert 'target 100.0%' in ' | '.join(_expansion_headers(root))
    slider, _number = _reserve_controls(root)

    _drive_value(slider, 40.0)

    # The TYPED target is unchanged -- it is a share of the pool, and the pool is
    # what shrank. What the header must follow is the derived figure beside it.
    assert 'target 100.0% (real 60.0%)' in ' | '.join(_expansion_headers(root))


def test_moving_the_reserve_recomputes_every_symbol_target_value(nicegui_client,
                                                                 account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id, target=100.0))
    table = _tables(root)[0]
    assert {r['target_value'] for r in table.rows} == {5_000.0}

    _drive_value(_reserve_controls(root)[0], 40.0)

    assert {r['target_value'] for r in table.rows} == {3_000.0}


def test_moving_the_reserve_moves_the_notch_and_the_delta(nicegui_client,
                                                          account_id):
    """A notch that does not move when the reserve is dragged is the stale-figure
    bug in visual form -- and so is a delta that does not."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id, target=100.0))
    assert 'tgt 100.0%' in ' | '.join(_texts(root))
    assert 'under by 50.0pp ($5,000.00)' in _texts(root)

    _drive_value(_reserve_controls(root)[0], 50.0)

    # The typed target is still 100% of a pool that is now $5,000 -- which is
    # exactly what is held, so the label lands on target.
    assert 'tgt 100.0% (real 50.0%)' in ' | '.join(_texts(root))
    assert 'on target' in _texts(root)


def test_a_reserve_above_100_is_refused_and_the_controls_go_back(
        monkeypatch, nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    sent = _capture_notifications(monkeypatch)
    errors = _capture_errors(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id, target=100.0))
    _slider, number = _reserve_controls(root)

    _drive_value(number, 140.0)

    assert get_allocation_config(account_id).unallocated_pct == 0.0
    assert number.value == 0.0
    # The EXACT pair, and the type matters: a typo is a ``warning`` carrying the
    # engine's own range sentence. The store refuses an out-of-range reserve too, so
    # a page that skipped its own validation would still not corrupt the config --
    # it would raise, and the user would get "Could not save the reserve: ..." as a
    # ``negative`` with a stack trace in the log. Same outcome, wrong diagnosis; a
    # mutation deleting the page's check survived a looser assertion than this.
    assert ('unallocated 140% is outside 0-100% - it is the share of the base to '
            'leave undeployed', 'warning') in sent
    assert errors == []


def test_a_negative_reserve_is_refused_because_it_would_INFLATE_the_base(
        monkeypatch, nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    sent = _capture_notifications(monkeypatch)
    errors = _capture_errors(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id, target=100.0))
    _slider, number = _reserve_controls(root)

    _drive_value(number, -20.0)

    assert get_allocation_config(account_id).unallocated_pct == 0.0
    assert ('unallocated -20% is outside 0-100% - it is the share of the base to '
            'leave undeployed', 'warning') in sent
    assert errors == []


def test_a_reserve_of_exactly_100_is_accepted_and_divides_by_nothing(
        nicegui_client, account_id):
    """100% -- allocate nothing this cycle -- is a legitimate setting, and the
    conversion that would blow up at r = 100 is deliberately never performed."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id, target=100.0))

    _drive_value(_reserve_controls(root)[0], 100.0)

    assert get_allocation_config(account_id).unallocated_pct == 100.0
    text = ' | '.join(_texts(root) + _expansion_headers(root))
    assert 'nan' not in text.lower()
    assert 'inf' not in text.lower()
    # There is no investable pool at all, so there is no share of one to print.
    assert 'no investable base' in text
    assert {r['target_value'] for r in _tables(root)[0].rows} == {0.0}


def test_a_reserve_of_exactly_zero_is_the_identity(nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id, target=100.0,
                                                        reserve=25.0), reserve=25.0)

    _drive_value(_reserve_controls(root)[1], 0.0)

    assert get_allocation_config(account_id).unallocated_pct == 0.0
    assert {r['target_value'] for r in _tables(root)[0].rows} == {5_000.0}
    assert any('$0.00 held back' in t for t in _texts(root))


def test_a_failed_reserve_write_is_reported_and_the_controls_go_back(
        monkeypatch, nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    sent = _capture_notifications(monkeypatch)
    _capture_errors(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id, target=100.0))
    monkeypatch.setattr(page, 'set_allocation_config',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('disk full')))
    slider, number = _reserve_controls(root)

    _drive_value(slider, 40.0)

    assert any('disk full' in text for text, _t in sent)
    assert slider.value == 0.0
    assert number.value == 0.0


def test_the_over_100_label_guard_still_fires_at_a_non_zero_reserve(
        monkeypatch, nicegui_client, account_id):
    """The label targets are RELATIVE weights on what the reserve leaves, so the
    100% rule is on the weights themselves and does not move with the reserve."""
    set_managed_label(account_id, 'ARK26', target_pct=50.0)
    set_managed_label(account_id, 'HighRisk', target_pct=50.0)
    sent = _capture_notifications(monkeypatch)
    views = _views([ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
                   {'ARK26': ['AAPL'], 'HighRisk': ['MSFT']}, reserve=60.0,
                   weights={'ARK26': {'AAPL': 100.0}, 'HighRisk': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views, reserve=60.0)

    _drive_value(_target_box(root, 0), 60.0)

    assert {r.label: r.target_pct for r in get_managed_labels(account_id)} == \
        {'ARK26': 50.0, 'HighRisk': 50.0}
    assert any('over 100%' in text for text, _t in sent)


# ---------------------------------------------------------------------------
# THE MINI-BAR ROW, SORTING AND THE TOTALS FOOTER
# ---------------------------------------------------------------------------

def test_the_labels_are_drawn_biggest_holding_first(nicegui_client, account_id):
    """The 39.5% row used to sit between two 1-5% rows."""
    views = _views([ManagedLabel('small', 5.0), ManagedLabel('big', 25.0)],
                   {'small': ['AAPL'], 'big': ['MSFT', 'TSLA']},
                   weights={'small': {'AAPL': 100.0},
                            'big': {'MSFT': 50.0, 'TSLA': 50.0}})
    root = _draw(nicegui_client, account_id, views)

    headers = _expansion_headers(root)
    assert headers[0].startswith('big')
    assert headers[1].startswith('small')


def test_an_empty_label_is_still_fully_drawn(nicegui_client, account_id):
    """Dimming or collapsing zero-value labels was considered and declined."""
    views = _views([ManagedLabel('full', 100.0), ManagedLabel('empty', 0.0)],
                   {'full': ['AAPL'], 'empty': []},
                   weights={'full': {'AAPL': 100.0}, 'empty': {}})
    root = _draw(nicegui_client, account_id, views)

    assert len(_expansions(root)) == 2
    assert any(h.startswith('empty') for h in _expansion_headers(root))


def test_each_label_row_states_its_verdict_in_WORDS(nicegui_client, account_id):
    """Not colour alone -- that excludes exactly the readers the palette was chosen
    for. The bare status word is gone; the delta sentence carries it."""
    views = _views([ManagedLabel('over_row', 10.0), ManagedLabel('under_row', 90.0)],
                   {'over_row': ['AAPL'], 'under_row': ['MSFT']},
                   weights={'over_row': {'AAPL': 100.0}, 'under_row': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    texts = _texts(root)
    assert any(t.startswith('over by ') for t in texts)
    assert any(t.startswith('under by ') for t in texts)


def test_the_totals_footer_accounts_for_the_labels_and_the_reserve(nicegui_client,
                                                                   account_id):
    views = _views([ManagedLabel('A', 60.0), ManagedLabel('B', 40.0)],
                   {'A': ['AAPL'], 'B': ['MSFT']}, reserve=10.0,
                   weights={'A': {'AAPL': 100.0}, 'B': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views, reserve=10.0)

    footer = _marked(root, page.MARKER_ALLOCATION_FOOTER)[0]._text
    assert '100.00% of what the reserve leaves' in footer
    assert '90.00% of base' in footer
    assert '10.00% reserve' in footer
    assert '= 100.00% of base' in footer


def test_the_totals_footer_turns_red_the_moment_the_labels_pass_100(nicegui_client,
                                                                    account_id):
    """A database written by the wizard can already hold an over-100 set; the page
    has to say so before the user presses Allocate."""
    views = _views([ManagedLabel('A', 60.0), ManagedLabel('B', 45.0)],
                   {'A': ['AAPL'], 'B': ['MSFT']},
                   weights={'A': {'AAPL': 100.0}, 'B': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    footer = _marked(root, page.MARKER_ALLOCATION_FOOTER)[0]
    assert 'text-red-400' in footer._classes


def test_the_totals_footer_follows_an_inline_edit(nicegui_client, account_id):
    set_managed_label(account_id, 'A', target_pct=60.0)
    views = _views([ManagedLabel('A', 60.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    _drive_value(_target_box(root, 0), 100.0)

    footer = _marked(root, page.MARKER_ALLOCATION_FOOTER)[0]._text
    assert '100.00% of what the reserve leaves' in footer


def test_the_totals_footer_follows_the_reserve(nicegui_client, account_id):
    set_managed_label(account_id, 'A', target_pct=100.0)
    views = _views([ManagedLabel('A', 100.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    _drive_value(_reserve_controls(root)[0], 40.0)

    footer = _marked(root, page.MARKER_ALLOCATION_FOOTER)[0]._text
    assert '60.00% of base, + 40.00% reserve = 100.00% of base' in footer


def test_a_colour_chosen_for_a_stale_label_does_not_resurrect_it(monkeypatch,
                                                                 nicegui_client,
                                                                 account_id):
    """``set_managed_label`` CREATES the row, so recolouring a label that was
    unmanaged in another tab would bring it back at ``target_pct=0`` -- which the
    engine reads as "hold none of this"."""
    from nicegui import ui

    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    with nicegui_client:
        page._open_label_picker(account_id, _noop_refresh)
        remove_managed_label(account_id, 'ARK26')
        _fire(_swatch_for(nicegui_client.layout, '#D55E00'))

    assert get_managed_labels(account_id) == []
    assert any('no longer managed' in text for text, _t in sent)


# ---------------------------------------------------------------------------
# ONE SOURCE OF TRUTH: the inline boxes and the Allocate wizard
#
# The wizard is now for EXECUTING against saved targets (dry run, precheck,
# submit) rather than for entering them, but it still edits and still writes. Both
# sides go through the same rows, so what has to hold is that the wizard reads the
# database at the moment it OPENS -- never a snapshot taken before the inline edit.
# ---------------------------------------------------------------------------

def test_the_allocate_flow_opens_with_the_target_the_page_just_saved(monkeypatch,
                                                                     account_id):
    """``_load_flow_inputs`` re-reads the store, so a fresh inline edit is what the
    wizard opens with. A cached payload here is how a dry run would silently solve
    the OLD number."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            buying_power=7_500.0,
                            positions=[_held('AAPL', 10, 1000.0, 2500.0)],
                            prices={'AAPL': 250.0})
    _use_account(monkeypatch, account)

    # ...the page persists an inline edit...
    assert page._write_label_target(account_id, 'ARK26', 88.0) is True

    _base, labels, *_rest = page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)

    assert [lt.target_pct for lt in labels] == [88.0]


def test_the_allocate_flow_opens_with_the_reserve_the_page_just_saved(monkeypatch,
                                                                      account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            buying_power=7_500.0,
                            positions=[_held('AAPL', 10, 1000.0, 2500.0)],
                            prices={'AAPL': 250.0})
    _use_account(monkeypatch, account)
    set_allocation_config(account_id, unallocated_pct=35.0)

    *_rest, unallocated_pct = page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)

    assert unallocated_pct == 35.0


def test_the_page_and_the_wizard_write_the_SAME_target_column(account_id):
    """One column, two writers, and they have to be the same one -- otherwise
    "target 40%" means different things on the two screens."""
    from ba2_trade_platform.core.portfolio_allocation import LabelTarget, SymbolTarget
    from ba2_trade_platform.core.portfolio_allocation_store import save_allocation_targets

    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    page._write_label_target(account_id, 'ARK26', 70.0)
    assert get_managed_labels(account_id)[0].target_pct == 70.0

    save_allocation_targets(account_id, [LabelTarget(
        label='ARK26', target_pct=55.0,
        symbols=[SymbolTarget(symbol='AAPL', weight_pct=100.0)])])

    assert get_managed_labels(account_id)[0].target_pct == 55.0
    # ...and the wizard's write recorded the INLINE value as the previous
    # generation, so Load-last restores what the page had.
    assert get_managed_labels(account_id)[0].previous_target_pct == 70.0


def test_the_wizards_write_never_touches_the_colour_or_the_comment(account_id):
    """``save_allocation_targets`` writes four columns and only those four. A
    wizard run must not clear a swatch or a note the page owns."""
    from ba2_trade_platform.core.portfolio_allocation import LabelTarget, SymbolTarget
    from ba2_trade_platform.core.portfolio_allocation_store import save_allocation_targets

    set_managed_label(account_id, 'ARK26', target_pct=40.0, comment='growth',
                      color='#56B4E9')

    save_allocation_targets(account_id, [LabelTarget(
        label='ARK26', target_pct=55.0,
        symbols=[SymbolTarget(symbol='AAPL', weight_pct=100.0)])])

    row = get_managed_labels(account_id)[0]
    assert (row.comment, row.color) == ('growth', '#56B4E9')


def test_the_page_and_the_wizard_write_the_SAME_symbol_weight_column(account_id):
    from ba2_trade_platform.core.portfolio_allocation import LabelTarget, SymbolTarget
    from ba2_trade_platform.core.portfolio_allocation_store import save_allocation_targets

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    page._write_symbol_weight(account_id, 'ARK26', 'AAPL', 62.5)
    assert get_symbol_rows(account_id, 'ARK26')['AAPL'].weight_pct == 62.5

    save_allocation_targets(account_id, [LabelTarget(
        label='ARK26', target_pct=100.0,
        symbols=[SymbolTarget(symbol='AAPL', weight_pct=80.0),
                 SymbolTarget(symbol='MSFT', weight_pct=20.0)])])

    rows = get_symbol_rows(account_id, 'ARK26')
    assert rows['AAPL'].weight_pct == 80.0
    assert rows['AAPL'].previous_weight_pct == 62.5


def test_an_inline_weight_edit_does_not_shift_the_previous_generation(account_id):
    """``previous_weight_pct`` is written by ``save_allocation_targets`` and by
    NOTHING else. An inline box that shifted it would grind the wizard's Load-last
    history away one edit at a time -- the same reason the comment path must not."""
    from ba2_trade_platform.core.portfolio_allocation import LabelTarget, SymbolTarget
    from ba2_trade_platform.core.portfolio_allocation_store import save_allocation_targets

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    save_allocation_targets(account_id, [LabelTarget(
        label='ARK26', target_pct=100.0,
        symbols=[SymbolTarget(symbol='AAPL', weight_pct=30.0)])])
    save_allocation_targets(account_id, [LabelTarget(
        label='ARK26', target_pct=100.0,
        symbols=[SymbolTarget(symbol='AAPL', weight_pct=40.0)])])
    assert get_symbol_rows(account_id, 'ARK26')['AAPL'].previous_weight_pct == 30.0

    page._write_symbol_weight(account_id, 'ARK26', 'AAPL', 90.0)

    row = get_symbol_rows(account_id, 'ARK26')['AAPL']
    assert row.weight_pct == 90.0
    assert row.previous_weight_pct == 30.0        # untouched by the inline path


def test_an_inline_label_target_edit_does_not_shift_the_previous_generation(account_id):
    from ba2_trade_platform.core.portfolio_allocation import LabelTarget, SymbolTarget
    from ba2_trade_platform.core.portfolio_allocation_store import save_allocation_targets

    set_managed_label(account_id, 'ARK26', target_pct=10.0)
    save_allocation_targets(account_id, [LabelTarget(
        label='ARK26', target_pct=20.0,
        symbols=[SymbolTarget(symbol='AAPL', weight_pct=100.0)])])
    assert get_managed_labels(account_id)[0].previous_target_pct == 10.0

    page._write_label_target(account_id, 'ARK26', 65.0)

    row = get_managed_labels(account_id)[0]
    assert (row.target_pct, row.previous_target_pct) == (65.0, 10.0)


def test_the_allocate_button_is_still_there_for_executing_the_saved_targets(
        monkeypatch, nicegui_client, account_id):
    """It is no longer where targets are ENTERED, but it is still the only way to
    dry-run, precheck and submit them."""
    monkeypatch.setattr(page, 'get_selected_account_id', lambda: account_id)
    _use_account(monkeypatch, _Account(account_id, {'manual_trading_enabled': True},
                                       positions=[], prices={}))
    _run_in_client(nicegui_client, page.content)

    assert page.REVIEW_BUTTON_LABEL in _button_labels(nicegui_client.layout)


def test_the_toolbar_button_no_longer_CLAIMS_to_allocate(monkeypatch,
                                                         nicegui_client, account_id):
    """It opens a review gate; nothing is ordered until Submit is pressed inside it.
    The old caption named an action the button stopped taking when the target steps
    moved onto this page."""
    monkeypatch.setattr(page, 'get_selected_account_id', lambda: account_id)
    _use_account(monkeypatch, _Account(account_id, {'manual_trading_enabled': True},
                                       positions=[], prices={}))
    _run_in_client(nicegui_client, page.content)

    assert page.REVIEW_BUTTON_LABEL == 'Review and Submit'
    assert 'Allocate' not in _button_labels(nicegui_client.layout)


# -- the mini-bar's rendered geometry, not just its arithmetic ---------------

def _marked(root, marker):
    """Every element carrying ``.mark(marker)``, in document order."""
    return [el for el in root.descendants() if marker in (el._markers or [])]


def _bars(root):
    """``[(fill, notch), ...]`` in the order the label rows were drawn."""
    return list(zip(_marked(root, page.MARKER_BAR_FILL),
                    _marked(root, page.MARKER_BAR_NOTCH)))


def test_the_rendered_bar_and_notch_carry_their_positions_as_styles(nicegui_client,
                                                                    account_id):
    views = _views([ManagedLabel('A', 50.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)
    fill, notch = _bars(root)[0]

    assert fill._style['width'] == '50.00%'      # 2,500 held of a 10,000 base
    assert notch._style['left'] == '100.00%'     # the 50% target sets the scale
    # ...and both are still DRAWN. Restyling with the position alone drops the
    # absolute positioning and the colour, which leaves an invisible notch sitting
    # at a perfectly correct offset.
    assert fill._style['position'] == 'absolute'
    assert fill._style['background']
    assert notch._style['position'] == 'absolute'
    assert notch._style['background']


def test_the_rendered_NOTCH_moves_when_the_reserve_does(nicegui_client, account_id):
    """A notch drawn once and never moved is the stale-figure bug in visual form --
    and a mutation that stopped restyling it survived every assertion about text.

    ``BIG`` holds the whole 10,000 base and has no target of its own, so IT sets the
    track's scale and A's notch is free to move within it. Without a fixed anchor
    A's own target sets the scale and the notch sits at 100% whatever the reserve
    is -- which is a true statement about the geometry and a useless test.
    """
    set_managed_label(account_id, 'A', target_pct=100.0)
    views = _views([ManagedLabel('A', 100.0), ManagedLabel('BIG', 0.0)],
                   {'A': ['AAPL'], 'BIG': ['MSFT', 'TSLA', 'NVDA', 'AMZN']},
                   weights={'A': {'AAPL': 100.0},
                            'BIG': {'MSFT': 25.0, 'TSLA': 25.0,
                                    'NVDA': 25.0, 'AMZN': 25.0}})
    root = _draw(nicegui_client, account_id, views)
    index = [h.split(' ')[0] for h in _expansion_headers(root)].index('A')
    before = _bars(root)[index][1]._style['left']
    assert before == '100.00%'

    _drive_value(_reserve_controls(root)[0], 50.0)

    assert _bars(root)[index][1]._style['left'] == '50.00%'


def test_the_rendered_BAR_carries_the_money_and_the_share_beside_it(nicegui_client,
                                                                    account_id):
    """TWO labels, so no figure on a bar row coincides with the managed-value stat
    card above it -- with one label the two are the same number and a mutation that
    blanked the bar's own money survived on the stat card's copy of it.
    """
    views = _views([ManagedLabel('A', 40.0), ManagedLabel('B', 60.0)],
                   {'A': ['AAPL'], 'B': ['MSFT', 'TSLA']},
                   weights={'A': {'AAPL': 100.0},
                            'B': {'MSFT': 50.0, 'TSLA': 50.0}})
    root = _draw(nicegui_client, account_id, views)

    texts = _texts(root)
    assert '$7,500.00' in texts          # the managed-value stat card
    assert '$5,000.00' in texts          # B's own money, on its bar row
    assert '$2,500.00' in texts          # A's
    assert '50.0%' in texts and '25.0%' in texts
    assert 'tgt 60.0%' in texts and 'tgt 40.0%' in texts


def test_the_rendered_bar_figures_follow_a_label_target_edit(nicegui_client,
                                                             account_id):
    set_managed_label(account_id, 'A', target_pct=50.0)
    views = _views([ManagedLabel('A', 50.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    _drive_value(_target_box(root, 0), 20.0)

    texts = _texts(root)
    assert 'tgt 20.0%' in texts
    assert '$2,500.00' in texts       # the holding did not move
    assert '25.0%' in texts
    assert 'over by 5.0pp ($500.00)' in texts   # 25% held against a 20% target


def test_a_label_appears_exactly_once_in_the_registry(nicegui_client, account_id):
    """Both the bar row and the label body touch the registry. Registering a view
    twice doubles the bar list and the "Managed labels" count silently."""
    live = page._new_live_state(base_notional=10_000.0, unallocated_pct=0.0)
    view = _one_label(account_id)[0]
    page._register_view(live, view)
    page._register_view(live, view)

    assert [v.label for v in live['views']] == ['ARK26']


# -- the Quasar templates the tests otherwise never execute ------------------

def test_the_target_cell_emits_ITS_OWN_rows_symbol(nicegui_client, account_id):
    """The event carries the symbol, and the template is where it is picked. The
    Python side never runs this string, so it is asserted directly."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    template = _tables(root)[0].slots['body-cell-weight_pct'].template

    assert "$emit('weightChange', props.row.symbol, val)" in template


def test_both_target_inputs_are_debounced(nicegui_client, account_id):
    """Without a debounce the "1" on the way to "15" is judged, accepted and
    PERSISTED as 1% -- and a rejected edit is put back, so the box would fight the
    user mid-number."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    assert f'debounce="{page.TARGET_DEBOUNCE_MS}"' in \
        _tables(root)[0].slots['body-cell-weight_pct'].template
    assert _target_box(root, 0)._props.get('debounce') == str(page.TARGET_DEBOUNCE_MS)


def test_the_label_rows_use_tabular_figures_so_the_columns_line_up(nicegui_client,
                                                                   account_id):
    """7c.3. Proportional digits put 39.5% and 1.4% at different widths, so the
    decimal points wander and the column stops being readable as a column."""
    views = _views([ManagedLabel('A', 40.0), ManagedLabel('B', 60.0)],
                   {'A': ['AAPL'], 'B': ['MSFT']},
                   weights={'A': {'AAPL': 100.0}, 'B': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    rows = _marked(root, page.MARKER_BAR_ROW)
    assert len(rows) == 2
    for row in rows:
        assert (row._style or {}).get('font-variant-numeric') == 'tabular-nums'


def test_the_money_and_the_percentages_keep_a_consistent_precision(nicegui_client,
                                                                   account_id):
    """7c.3: money to 2dp, percentages to 1dp, everywhere on the row."""
    views = _views([ManagedLabel('A', 40.0), ManagedLabel('B', 60.0)],
                   {'A': ['AAPL'], 'B': ['MSFT']},
                   weights={'A': {'AAPL': 100.0}, 'B': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    texts = _texts(root)
    assert '$2,500.00' in texts          # money: always two decimals
    assert '25.0%' in texts              # percentages: always one
    assert 'tgt 40.0%' in texts


def test_choosing_a_colour_retints_the_swatch_beside_the_row(monkeypatch,
                                                             nicegui_client,
                                                             account_id):
    """The dialog does not reload, so a swatch that never follows the picker leaves
    the user unable to see what they just chose."""
    from nicegui import ui

    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    _capture_notifications(monkeypatch)
    with nicegui_client:
        page._open_label_picker(account_id, _noop_refresh)
        swatch = _icons(nicegui_client.layout, 'label')[0]
        before = (swatch._style or {}).get('color')
        _fire(_swatch_for(nicegui_client.layout, '#D55E00'))

    assert before.lower().startswith(_DEFAULT_ICON_COLOR.lower())
    assert '#D55E00' in (swatch._style or {}).get('color', '').upper()


def test_the_swatch_only_ever_takes_a_PARSED_colour(monkeypatch, nicegui_client,
                                                    account_id):
    """The value lands in a CSS ``style`` attribute, so it goes through the strict
    ``#rrggbb`` parse rather than straight from the widget. The palette is no longer
    the whitelist -- the PARSE is -- and this is what keeps that from being a
    loosening."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    with nicegui_client:
        page._open_label_picker(account_id, _noop_refresh)
        swatch = _icons(nicegui_client.layout, 'label')[0]
        custom = _marked(nicegui_client.layout, page.MARKER_COLOR_CUSTOM)[0]
        # Not a colour: a hand-edited value, or a future bug in the caller.
        _drive_value(custom, 'red;content:"x"')

    assert get_managed_labels(account_id)[0].color is None
    # ``!important``, because ``styles.css`` greys icons with one of its own.
    assert (swatch._style or {}).get('color') == _view_mod(
        ).important_color_style(_DEFAULT_ICON_COLOR).removeprefix('color: ')
    assert 'content' not in (swatch._style or {}).get('color', '')
    assert any('not a colour' in text for text, _t in sent)


def test_the_reserve_controls_can_express_a_two_decimal_reserve(nicegui_client,
                                                                account_id):
    """The slider steps in whole percent because dragging a 10,000-notch track is
    unusable; the BOX is what makes 12.5 reachable, so its step has to allow it."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    slider, number = _reserve_controls(root)

    assert slider._props['step'] == 1
    assert number._props['step'] == 0.01
    assert (slider._props['min'], slider._props['max']) == (0, 100)
    assert (number._props['min'], number._props['max']) == (0, 100)


def test_a_recomputed_target_value_is_rounded_to_cents(nicegui_client, account_id):
    """The column is money. An unrounded float renders 1233.3333333333335."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id,
                 _one_label(account_id, target=100.0, symbols=('AAPL', 'MSFT', 'TSLA'),
                            base=3_700.0), base=3_700.0)
    table = _tables(root)[0]

    _emit(table, 'weightChange', ['AAPL', 33.33])

    for row in table.rows:
        assert row['target_value'] == round(row['target_value'], 2)
    assert table.rows[0]['target_value'] == 1233.21          # 33.33% of 3,700


def test_an_unmeasurable_row_keeps_its_blank_through_a_RECOMPUTE(nicegui_client,
                                                                 account_id):
    """``weight_pct is None`` means "there is no answer", and the recompute must
    leave it alone rather than writing a 0.00 that reads as a decision.

    One unpriced member blanks every UNSAVED share in the label -- the label's
    total is the denominator and nobody knows it -- so editing the measurable one
    gives it a saved target while its neighbour stays honestly blank.
    """
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import positions_by_symbol

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'DARK']},
                              positions_by_symbol([_pos('AAPL', 10, 1000.0, 2500.0),
                                                   _pos('DARK', 10, 1000.0, None)]),
                              {'AAPL': 250.0, 'DARK': None},
                              valuation_mode=VALUATION_MODE_MARKET,
                              base_notional=10_000.0)
    root = _draw(nicegui_client, account_id, views)
    table = _tables(root)[0]
    assert [r['target_value'] for r in table.rows] == [None, None]

    _emit(table, 'weightChange', ['AAPL', 60.0])

    assert {r['symbol']: r['weight_pct'] for r in table.rows} == \
        {'AAPL': 60.0, 'DARK': None}


def test_an_edit_touches_the_EDITED_labels_own_map_and_no_others(nicegui_client,
                                                                 account_id):
    """A symbol carried by two managed labels has an independent weight in each.
    Writing through the wrong label's map would redraw this table with numbers
    belonging to a different basket -- and, now that the maps are the page's only
    record of what is on screen, would corrupt what Fill 100% then reads."""
    set_managed_label(account_id, 'ARK26', target_pct=50.0)
    set_managed_label(account_id, 'HighRisk', target_pct=50.0)
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=90.0)
    set_symbol_weight(account_id, 'ARK26', 'MSFT', weight_pct=10.0)
    views = _views([ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
                   {'ARK26': ['AAPL', 'MSFT'], 'HighRisk': ['AAPL', 'MSFT']},
                   weights={'ARK26': {'AAPL': 90.0, 'MSFT': 10.0},
                            'HighRisk': {'AAPL': 50.0, 'MSFT': 50.0}})
    root = _draw(nicegui_client, account_id, views)
    high_risk = _tables(root)[1]

    _emit(high_risk, 'weightChange', ['AAPL', 30.0])

    assert {r['symbol']: r['weight_pct'] for r in high_risk.rows} == \
        {'AAPL': 30.0, 'MSFT': 50.0}        # its own sibling, unmoved
    assert {r['symbol']: r['weight_pct'] for r in _tables(root)[0].rows} == \
        {'AAPL': 90.0, 'MSFT': 10.0}        # the OTHER label, untouched


def test_a_target_typed_as_TEXT_is_stored_as_the_parsed_number(nicegui_client,
                                                               account_id):
    """Quasar's ``q-input`` hands ``@update:model-value`` whatever the field holds,
    which is a STRING -- and the box carries ``suffix='%'``. What reaches the store
    has to be the number ``parse_pct`` read out of it, not the raw text: a mutation
    passing ``raw`` straight through survived every test that emitted a float.
    """
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', ' 75.5 % '])

    row = get_symbol_rows(account_id, 'ARK26')['AAPL']
    assert row.weight_pct == 75.5
    assert isinstance(row.weight_pct, float)


def test_the_label_target_box_never_hands_the_writer_raw_text(nicegui_client,
                                                              account_id):
    """Where the string can and cannot arrive, pinned.

    ``ui.number`` coerces before any handler runs -- ``_value_to_model_value``
    raises on '62.5%' -- so the label box always delivers a float or ``None``. The
    TABLE cell is different: it is a bare Quasar ``q-input`` emitting through
    ``$emit``, so it delivers whatever the field holds, which is why the writers go
    through ``parse_pct`` and pass ``edit.value`` rather than the raw argument.
    """
    import pytest as _pytest

    root = _draw(nicegui_client, account_id, _one_label(account_id))
    with _pytest.raises(ValueError):
        _target_box(root, 0)._value_to_model_value('62.5%')


# ---------------------------------------------------------------------------
# THE SIX THINGS THE USER ASKED FOR, ON THE PAGE
#
# 1. the caption under the target input told a lie whenever a reserve was set
# 2. editing one symbol's share moved every other number in the label
# 3. the row showed current and target but never the GAP -- the actionable number
# 4/7/8. the colour was set two screens away, the row's icon read grey, and the
#        picker was a list of colour NAMES
# 5. the ⓘ tooltip was a single unwrapped line in a tiny font
# 6. the summary cards were ragged and the last one was clipped off the viewport
# ---------------------------------------------------------------------------

def _view_mod():
    from ba2_trade_platform.ui.utils import portfolio_allocation_view as v
    return v


def _table_rows(root, index=0):
    return {r['symbol']: dict(r) for r in _tables(root)[index].rows}


def _fill_button(root, index=0):
    from nicegui import ui
    buttons = [el for el in root.descendants()
               if isinstance(el, ui.button) and el._props.get('label') == 'Fill 100%']
    assert buttons, 'no Fill 100% button was drawn'
    return buttons[index]


# -- 1. the caption ----------------------------------------------------------

def test_the_caption_under_the_target_box_no_longer_claims_the_whole_portfolio(
        nicegui_client, account_id):
    """It is FALSE whenever a reserve is set: 15 typed under a 10% reserve is 13.5%
    of the portfolio, and the row beside it said exactly that."""
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=10.0),
                 reserve=10.0)
    texts = _texts(root)

    assert not any('share of the whole portfolio' in t for t in texts)
    assert _view_mod().LABEL_TARGET_CAPTION in texts


def test_the_page_names_both_denominators_ONCE_rather_than_on_every_row():
    """The row is terse ("tgt 15.0% (real 13.5%)") because the legend carries the
    explanation. Three copies of one fact is three things that can disagree."""
    assert _view_mod().BASIS_LEGEND


def test_the_reserve_row_is_marked_as_the_one_row_on_the_other_denominator(
        nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=10.0),
                 reserve=10.0)
    assert _view_mod().RESERVE_BASIS_NOTE in _texts(root)


# -- 2. no auto-recalculation, and Fill 100% instead -------------------------

def _three_symbol_label(account_id, label='ARK26', weights=None):
    return _views([ManagedLabel(label, 100.0)],
                  {label: ['AAPL', 'MSFT', 'TSLA']},
                  weights={label: dict(weights or {'AAPL': 33.33, 'MSFT': 33.33,
                                                   'TSLA': 33.34})})


def test_editing_one_symbols_share_leaves_every_SIBLING_untouched(nicegui_client,
                                                                  account_id):
    """"do not automatically recalculate when I adjust share of label within label.
    Do not change other numbers." It used to re-read the whole label, because the
    unstored siblings resolve to a share of what is left of 100."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, _three_symbol_label(account_id))
    before = _table_rows(root)

    _emit(_tables(root)[0], 'weightChange', ['AAPL', 10.0])

    after = _table_rows(root)
    assert after['MSFT']['weight_pct'] == before['MSFT']['weight_pct'] == 33.33
    assert after['TSLA']['weight_pct'] == before['TSLA']['weight_pct'] == 33.34
    assert after['MSFT']['target_value'] == before['MSFT']['target_value']
    assert after['TSLA']['target_value'] == before['TSLA']['target_value']


def test_the_edited_symbol_DOES_move_because_that_is_the_edits_own_consequence(
        nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, _three_symbol_label(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', 10.0])

    row = _table_rows(root)['AAPL']
    assert row['weight_pct'] == 10.0
    assert row['target_value'] == 1_000.0        # 10% of a 100% label on a 10k base


def test_the_no_recalc_edit_does_not_re_read_the_labels_weight_map(monkeypatch,
                                                                   nicegui_client,
                                                                   account_id):
    """THE mutation: putting ``get_symbol_weights`` back into the write path. It
    re-derives every sibling from the store, which is the recalculation itself."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, _three_symbol_label(account_id))
    calls = []
    monkeypatch.setattr(page, 'get_symbol_weights',
                        lambda *a, **k: calls.append(a) or {})

    _emit(_tables(root)[0], 'weightChange', ['AAPL', 10.0])

    assert calls == []


def test_every_label_gets_its_own_Fill_100_button(nicegui_client, account_id):
    views = _views([ManagedLabel('A', 50.0), ManagedLabel('B', 50.0)],
                   {'A': ['AAPL'], 'B': ['MSFT']},
                   weights={'A': {'AAPL': 100.0}, 'B': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    from nicegui import ui
    assert len([el for el in root.descendants()
                if isinstance(el, ui.button)
                and el._props.get('label') == 'Fill 100%']) == 2


def test_Fill_100_shares_the_shortfall_between_the_EMPTY_symbols_and_persists_it(
        monkeypatch, nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbol_label(account_id,
                                     weights={'AAPL': 30.0, 'MSFT': 0.0, 'TSLA': 0.0}))

    _press(_fill_button(root))

    stored = get_symbol_weights(account_id, 'ARK26', ['AAPL', 'MSFT', 'TSLA'])
    assert stored == {'AAPL': 30.0, 'MSFT': 35.0, 'TSLA': 35.0}
    assert _table_rows(root)['MSFT']['weight_pct'] == 35.0
    assert any('empty' in m.lower() for m, _t in sent)


def test_Fill_100_scales_an_over_allocated_label_DOWN(monkeypatch, nicegui_client,
                                                      account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbol_label(account_id,
                                     weights={'AAPL': 100.0, 'MSFT': 50.0,
                                              'TSLA': 50.0}))

    _press(_fill_button(root))

    stored = get_symbol_weights(account_id, 'ARK26', ['AAPL', 'MSFT', 'TSLA'])
    assert stored == {'AAPL': 50.0, 'MSFT': 25.0, 'TSLA': 25.0}


def test_Fill_100_scales_an_under_allocated_label_UP_when_nothing_is_empty(
        monkeypatch, nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbol_label(account_id,
                                     weights={'AAPL': 10.0, 'MSFT': 20.0,
                                              'TSLA': 20.0}))

    _press(_fill_button(root))

    stored = get_symbol_weights(account_id, 'ARK26', ['AAPL', 'MSFT', 'TSLA'])
    assert round(sum(stored.values()), 2) == 100.0
    assert stored['MSFT'] == stored['TSLA'] == 40.0
    assert stored['AAPL'] == 20.0


def test_Fill_100_at_exactly_100_says_so_and_writes_NOTHING(monkeypatch,
                                                            nicegui_client,
                                                            account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _three_symbol_label(account_id))

    _press(_fill_button(root))

    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert any('already' in m.lower() for m, _t in sent)


def test_Fill_100_reads_the_SAME_live_map_the_no_recalc_edit_writes(
        monkeypatch, nicegui_client, account_id):
    """The proof that the two agree about what "empty" means. The edit mutates ONE
    key of the label's live map; Fill 100% reads that map and nothing else. If it
    re-read the store instead, the edit above would be invisible to it and the two
    features would contradict each other on every row."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _three_symbol_label(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', 0.0])      # 0 == EMPTY
    _press(_fill_button(root))

    stored = get_symbol_weights(account_id, 'ARK26', ['AAPL', 'MSFT', 'TSLA'])
    # 33.33 + 33.34 typed, so AAPL -- the only empty slot -- takes the 33.33 left.
    assert stored == {'AAPL': 33.33, 'MSFT': 33.33, 'TSLA': 33.34}


def test_Fill_100_refuses_to_write_under_a_label_that_is_no_longer_managed(
        monkeypatch, nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbol_label(account_id,
                                     weights={'AAPL': 30.0, 'MSFT': 0.0, 'TSLA': 0.0}))
    remove_managed_label(account_id, 'ARK26')

    _press(_fill_button(root))

    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert any('no longer managed' in m for m, _t in sent)


# -- 3. the delta from actual ------------------------------------------------

def test_the_row_states_how_far_from_target_it_is_in_POINTS_and_DOLLARS(
        nicegui_client, account_id):
    """"aside the label target, include the delta from actual (% + $)". $2,500 held
    against a 10% target of a $10,000 pool is $1,500 and 15 points over."""
    views = _views([ManagedLabel('A', 10.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    assert 'over by 15.0pp ($1,500.00)' in _texts(root)


def test_the_row_says_UNDER_when_it_is_under(nicegui_client, account_id):
    views = _views([ManagedLabel('A', 50.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    assert 'under by 25.0pp ($2,500.00)' in _texts(root)


def test_the_bare_status_WORD_is_gone_now_that_the_delta_carries_it(nicegui_client,
                                                                    account_id):
    """Three renderings of one fact -- the word, the notch and the delta -- and two
    of them content-free. The word survives inside the delta sentence."""
    views = _views([ManagedLabel('A', 10.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    texts = _texts(_draw(nicegui_client, account_id, views))

    assert 'over' not in texts               # not as a cell of its own
    assert any(t.startswith('over by ') for t in texts)


def test_the_delta_keeps_the_status_COLOUR_so_the_row_still_scans(nicegui_client,
                                                                  account_id):
    views = _views([ManagedLabel('A', 10.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)
    element = next(el for el in root.descendants()
                   if (el._text or '').startswith('over by '))

    assert page.LABEL_STATUS_CLASSES['over'] in ' '.join(element._classes)


def _delta_paint(root, text):
    """The inline colour of the delta sentence rendering exactly ``text``.

    By the WHOLE sentence, never by a prefix: three deltas are drawn on one page
    -- the label row's, its symbol-share bar's and the reserve card's -- and they
    share a vocabulary, so 'under by ' finds whichever came first.
    """
    element = next(el for el in root.descendants() if el._text == text)
    return element._style.get('color')


def test_a_label_far_below_target_reads_ORANGE_like_the_bar_beside_it(
        nicegui_client, account_id):
    """The threshold is the BAR's -- ``within_target_band``, 20 percentage points
    -- so the sentence and the track change together. One holding of $2,500 against
    a 60% target of a $10,000 pool is 35 points short, well outside the band."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import STATUS_OVER_COLOR

    views = _views([ManagedLabel('A', 60.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    assert _delta_paint(root, 'under by 35.0pp ($3,500.00)') == \
        f'{STATUS_OVER_COLOR} !important'


def test_a_label_only_a_little_short_is_left_alone(nicegui_client, account_id):
    """Inside the band. $2,500 held against a 30% target of a $10,000 pool is 5
    points short -- drift, not a decision, and colouring it would leave the page
    permanently amber."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import NEUTRAL_TEXT_COLOR

    views = _views([ManagedLabel('A', 30.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    assert _delta_paint(root, 'under by 5.0pp ($500.00)') == \
        f'{NEUTRAL_TEXT_COLOR} !important'


def test_a_label_with_no_measurable_share_is_neither_orange_nor_green(
        nicegui_client, account_id):
    """No base notional means no pool, so no share, no gap and no verdict. The
    distinction is load-bearing across this page -- 'unknown' is not 'short' -- and
    a whole page of orange on an account the broker did not answer for would be
    reporting a problem the user does not have."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        LABEL_DELTA_NONE, NEUTRAL_TEXT_COLOR,
    )
    views = _views([ManagedLabel('A', 60.0)], {'A': ['AAPL']}, base=None)
    with nicegui_client:
        page._render_labels(account_id,
                            _payload(views, VALUATION_MODE_MARKET,
                                     base_notional=None,
                                     available_buying_power=None), _noop_refresh)
    root = nicegui_client.layout

    # BY CLASS, not by text: the dash is also what the current-share cell beside
    # it prints, and that one is deliberately unpainted.
    deltas = [el for el in root.descendants()
              if el._text == LABEL_DELTA_NONE and 'text-xs' in el._classes]
    assert deltas, 'the label row drew no verdict at all'
    assert {el._style.get('color') for el in deltas} == \
        {f'{NEUTRAL_TEXT_COLOR} !important'}
    # No share is measurable, so no row may claim a gap in either direction. (The
    # label-total readout is still allowed its own orange: "these targets do not
    # add up to 100" is a judgement about the TYPED numbers and needs no pool.)
    verdicts = [el._style.get('color') for el in root.descendants()
                if (el._text or '').startswith(('under by ', 'over by '))]
    assert verdicts == []


def test_the_delta_follows_a_label_target_edit(nicegui_client, account_id):
    set_managed_label(account_id, 'A', target_pct=10.0)
    views = _views([ManagedLabel('A', 10.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    _drive_value(_target_box(root, 0), 25.0)

    assert 'on target' in _texts(root)


def test_the_delta_follows_the_reserve(nicegui_client, account_id):
    """Raising the reserve shrinks the pool, so the same holding is a bigger share
    of it and the gap widens. A delta that did not move would be the stale-figure
    bug in the one number the user acts on."""
    views = _views([ManagedLabel('A', 25.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views)
    assert 'on target' in _texts(root)

    _drive_value(_reserve_controls(root)[0], 50.0)

    assert 'over by 25.0pp ($1,250.00)' in _texts(root)


def test_the_row_shows_the_typed_target_FIRST_and_the_derived_one_in_brackets(
        nicegui_client, account_id):
    views = _views([ManagedLabel('A', 15.0)], {'A': ['AAPL']}, reserve=10.0,
                   weights={'A': {'AAPL': 100.0}})
    root = _draw(nicegui_client, account_id, views, reserve=10.0)

    assert 'tgt 15.0% (real 13.5%)' in _texts(root)


def test_a_zero_reserve_row_prints_the_target_once(nicegui_client, account_id):
    views = _views([ManagedLabel('A', 15.0)], {'A': ['AAPL']},
                   weights={'A': {'AAPL': 100.0}})
    texts = _texts(_draw(nicegui_client, account_id, views))

    assert 'tgt 15.0%' in texts
    assert not any('real' in t for t in texts if t.startswith('tgt'))


# -- 4 / 7 / 8. the colour ---------------------------------------------------

def _row_icon(root, index=0):
    return _marked(root, page.MARKER_LABEL_ICON)[index]


def _bar_fill(root, index=0):
    return _marked(root, page.MARKER_BAR_FILL)[index]


def _swatches(root):
    return _marked(root, page.MARKER_COLOR_SWATCH)


def test_the_icon_the_bar_and_the_dialog_swatch_all_agree_for_a_COLOURED_label(
        monkeypatch, nicegui_client, account_id):
    """One resolver, three renderings. The user's complaint was a yellow bar beside
    a grey tag icon."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        resolve_label_icon_color)
    set_managed_label(account_id, 'ARK26', target_pct=40.0, color='#F0E442')
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id, color='#F0E442'))
    expected = resolve_label_icon_color('#F0E442')

    assert expected in (_row_icon(root)._style or {}).get('color', '')
    assert _bar_fill(root)._style['background'].upper() == expected.upper()

    with nicegui_client:
        page._open_label_picker(account_id, _noop_refresh)
    dialog_swatch = _icons(nicegui_client.layout, 'label')[-1]
    assert expected in (dialog_swatch._style or {}).get('color', '')


def test_the_icon_and_the_bar_agree_for_an_UNCOLOURED_label_too(nicegui_client,
                                                                account_id):
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        DEFAULT_LABEL_ICON_COLOR)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    assert DEFAULT_LABEL_ICON_COLOR in (_row_icon(root)._style or {}).get('color', '')
    assert (_bar_fill(root)._style['background'].upper()
            == DEFAULT_LABEL_ICON_COLOR.upper())


def test_the_colour_is_reachable_from_the_label_ROW(monkeypatch, nicegui_client,
                                                    account_id):
    """"the color picker already exists in Manage labels — the user simply could not
    find it"."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    assert len(_swatches(root)) >= len(page.LABEL_COLOR_PALETTE)


def test_picking_a_colour_on_the_row_persists_it_and_retints_BOTH_the_icon_and_the_bar(
        monkeypatch, nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    swatch = next(s for s in _swatches(root)
                  if (s._style or {}).get('background', '').upper() == '#D55E00')

    _fire(swatch)

    assert get_managed_labels(account_id)[0].color == '#D55E00'
    assert '#D55E00' in (_row_icon(root)._style or {}).get('color', '').upper()
    assert _bar_fill(root)._style['background'].upper() == '#D55E00'


def test_the_row_picker_can_clear_a_colour_back_to_NO_colour(monkeypatch,
                                                             nicegui_client,
                                                             account_id):
    """A palette with no way out means a colour, once set, can never be removed."""
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        DEFAULT_LABEL_ICON_COLOR)
    set_managed_label(account_id, 'ARK26', target_pct=40.0, color='#D55E00')
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id, color='#D55E00'))

    _fire(_marked(root, page.MARKER_COLOR_CLEAR)[0])

    assert get_managed_labels(account_id)[0].color is None
    assert DEFAULT_LABEL_ICON_COLOR in (_row_icon(root)._style or {}).get('color', '')


def test_the_presets_are_drawn_as_COLOURS_and_not_as_a_list_of_names(monkeypatch,
                                                                     nicegui_client,
                                                                     account_id):
    """"Make a color picker then." Naming a colour is not showing it."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    drawn = {(s._style or {}).get('background', '').upper() for s in _swatches(root)}

    for _name, hex_value in page.LABEL_COLOR_PALETTE:
        assert hex_value.upper() in drawn


def test_a_CUSTOM_colour_is_saved_through_the_same_writer(monkeypatch,
                                                          nicegui_client,
                                                          account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    _capture_notifications(monkeypatch)
    with nicegui_client:
        page._render_color_choices(account_id, 'ARK26', None, lambda *_a: None)
        custom = _marked(nicegui_client.layout, page.MARKER_COLOR_CUSTOM)[0]
        _drive_value(custom, '#a1b2c3')

    assert get_managed_labels(account_id)[0].color == '#A1B2C3'


def test_a_low_contrast_custom_colour_WARNS_and_is_still_saved(monkeypatch,
                                                               nicegui_client,
                                                               account_id):
    """Warn, do not block: they read the palette argument and asked anyway."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    with nicegui_client:
        page._render_color_choices(account_id, 'ARK26', None, lambda *_a: None)
        _drive_value(_marked(nicegui_client.layout, page.MARKER_COLOR_CUSTOM)[0],
                     '#101010')

    assert get_managed_labels(account_id)[0].color == '#101010'
    assert any('contrast' in m.lower() and t == 'warning' for m, t in sent)


def test_a_readable_custom_colour_draws_no_contrast_warning(monkeypatch,
                                                            nicegui_client,
                                                            account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    with nicegui_client:
        page._render_color_choices(account_id, 'ARK26', None, lambda *_a: None)
        _drive_value(_marked(nicegui_client.layout, page.MARKER_COLOR_CUSTOM)[0],
                     '#FFFFFF')

    assert get_managed_labels(account_id)[0].color == '#FFFFFF'
    assert not any('contrast' in m.lower() for m, _t in sent)


def test_a_custom_value_that_is_not_a_colour_never_reaches_the_store(monkeypatch,
                                                                     nicegui_client,
                                                                     account_id):
    """The value is interpolated into a CSS ``style`` attribute."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    with nicegui_client:
        page._render_color_choices(account_id, 'ARK26', None, lambda *_a: None)
        _drive_value(_marked(nicegui_client.layout, page.MARKER_COLOR_CUSTOM)[0],
                     '#a1b2c3;background:url(x)')

    assert get_managed_labels(account_id)[0].color is None
    assert any('not a colour' in m for m, _t in sent)


# -- 5. the tooltip ----------------------------------------------------------

def test_the_info_tooltip_is_wrapped_and_legibly_sized(nicegui_client, account_id):
    """"The info text is too small." A tooltip is HTML: without a max-width a long
    sentence renders as one line wider than the viewport, clipped at both ends."""
    from nicegui import ui
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    tip = next(el for el in root.descendants()
               if isinstance(el, ui.tooltip) and 'Portfolio target' in (el._text or ''))
    style = ' '.join(f'{k}: {v}' for k, v in (tip._style or {}).items())

    assert 'max-width' in style
    assert 'pre-line' in style
    assert 'font-size' in style


def test_the_tooltip_style_is_the_conventions_and_not_a_second_one():
    """``ExpertDataExportInterface.DETAIL_TOOLTIP_STYLE`` is what landed for the
    expert cards; this reuses its two rules rather than inventing a third."""
    from ba2_common.core.interfaces.ExpertDataExportInterface import (
        DETAIL_TOOLTIP_STYLE)
    style = _view_mod().LABEL_TOOLTIP_STYLE
    for rule in DETAIL_TOOLTIP_STYLE.split(';'):
        if rule.strip():
            assert rule.strip() in style


# -- 6. the summary cards ----------------------------------------------------

def _summary_cards(root):
    row = _marked(root, page.MARKER_SUMMARY_ROW)[0]
    return [el for el in row.descendants()
            if 'stat-card' in (el._classes or [])]


def test_the_summary_cards_are_equal_height_and_wrap_instead_of_clipping(
        nicegui_client, account_id):
    """Five cards no longer fit on one line: the reserve card was cut off at the
    right edge of the viewport."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    row = _marked(root, page.MARKER_SUMMARY_ROW)[0]
    classes = ' '.join(row._classes)

    assert 'items-stretch' in classes
    assert 'flex-wrap' in classes
    for card in _summary_cards(root):
        assert 'flex-1' in ' '.join(card._classes)


def test_the_reserve_control_is_no_longer_wedged_into_the_summary_row(
        nicegui_client, account_id):
    """It is the widest card (a slider, an input and a caption), it is the only
    CONTROL among read-only stats, and it governs the label list -- so it sits on
    its own line directly above it."""
    from nicegui import ui
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    row = _marked(root, page.MARKER_SUMMARY_ROW)[0]

    assert not [el for el in row.descendants() if isinstance(el, ui.slider)]
    assert _marked(root, page.MARKER_RESERVE_CARD)


# -- gaps the mutation run found ---------------------------------------------

def test_the_basis_legend_is_actually_DRAWN_and_not_merely_defined(nicegui_client,
                                                                   account_id):
    """Survivor: the page not drawing ``BASIS_LEGEND``.

    The rows are terse ("tgt 15.0% (real 13.5%)") only because this line carries
    the explanation. A constant nothing renders is the explanation missing.
    """
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    assert _view_mod().BASIS_LEGEND in _texts(root)


def test_an_edit_in_one_label_cannot_corrupt_what_FILL_100_reads_in_another(
        monkeypatch, nicegui_client, account_id):
    """Survivor: the edit writing its symbol into EVERY label's live map.

    Invisible on screen -- only the edited label is redrawn -- and therefore only
    observable through the button that reads those maps. A symbol carried by two
    managed labels has an independent weight in each; ARK26 is at 90/10 and must
    still report "already 100%" after HighRisk's AAPL is edited to 30.
    """
    set_managed_label(account_id, 'ARK26', target_pct=50.0)
    set_managed_label(account_id, 'HighRisk', target_pct=50.0)
    sent = _capture_notifications(monkeypatch)
    views = _views([ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
                   {'ARK26': ['AAPL', 'MSFT'], 'HighRisk': ['AAPL', 'MSFT']},
                   weights={'ARK26': {'AAPL': 90.0, 'MSFT': 10.0},
                            'HighRisk': {'AAPL': 50.0, 'MSFT': 50.0}})
    root = _draw(nicegui_client, account_id, views)

    _emit(_tables(root)[1], 'weightChange', ['AAPL', 30.0])
    _press(_fill_button(root, 0))               # ARK26's own button

    assert get_symbol_rows(account_id, 'ARK26') == {}     # nothing was written
    assert any('already' in m.lower() for m, _t in sent)


def test_the_tag_icon_is_left_UNCOLOURED_until_the_bar_loop_paints_it(nicegui_client,
                                                                      account_id):
    """Survivor: the icon's initial colour set at render time as well.

    Harmless-looking and unfalsifiable: ``_apply_bars`` runs at the end of
    ``_render_labels`` and overwrites it, so a WRONG colour there is invisible --
    which is exactly how the icon and the bar came to be painted by two different
    rules in the first place. The icon is therefore drawn with no colour at all,
    and this is what says so: uncoloured after the row is built, correct after the
    loop runs, and equal to the fill. ONE writer, checked rather than asserted in
    a comment.
    """
    live = page._new_live_state(base_notional=10_000.0, unallocated_pct=0.0)
    view = _one_label(account_id, color='#F0E442')[0]
    with nicegui_client:
        page._render_label_bar_row(account_id, live, view, _noop_refresh)
    icon = _marked(nicegui_client.layout, page.MARKER_LABEL_ICON)[0]

    assert not (icon._style or {}).get('color')

    page._apply_bars(live)

    fill = _marked(nicegui_client.layout, page.MARKER_BAR_FILL)[0]
    assert '#F0E442' in (icon._style or {}).get('color', '').upper()
    assert fill._style['background'].upper() == '#F0E442'


def test_the_Fill_100_no_op_is_reported_as_INFORMATION_not_as_a_success(
        monkeypatch, nicegui_client, account_id):
    """Survivor: the no-op notified as ``positive``.

    A green "done" toast over a set nothing happened to is the "silently does
    nothing" complaint wearing a different hat -- the user learns the button works
    and stops reading the sentence.
    """
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _three_symbol_label(account_id))

    _press(_fill_button(root))

    assert [t for m, t in sent if 'already' in m.lower()] == ['info']


def test_the_symbol_cells_are_rounded_onto_the_CENT_grid(nicegui_client, account_id):
    """Survivor: the ``round(..., 2)`` on the redrawn Share-of-label cell.

    ``get_symbol_weights`` resolves an unstored symbol to FOUR decimals, so the live
    map genuinely holds them; printing 33.3333 in a column whose input steps by 0.01
    offers the user a number they cannot type back.
    """
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id,
                 _one_label(account_id, target=100.0,
                            weights={'AAPL': 33.3333, 'MSFT': 66.6667}))

    _drive_value(_reserve_controls(root)[0], 10.0)      # forces the redraw

    assert {r['symbol']: r['weight_pct'] for r in _tables(root)[0].rows} == \
        {'AAPL': 33.33, 'MSFT': 66.67}


def test_a_custom_colour_retints_the_dialog_swatch_with_the_PARSED_value(
        monkeypatch, nicegui_client, account_id):
    """Survivor: ``on_saved`` handed the raw widget value instead of the resolved
    one. Identical for a preset click, and for a custom colour it puts an unparsed
    string straight into a CSS ``style`` attribute.
    """
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    _capture_notifications(monkeypatch)
    with nicegui_client:
        page._open_label_picker(account_id, _noop_refresh)
        swatch = _icons(nicegui_client.layout, 'label')[0]
        _drive_value(_marked(nicegui_client.layout, page.MARKER_COLOR_CUSTOM)[0],
                     '#a1b2c3')

    assert (swatch._style or {}).get('color') == _view_mod(
        ).important_color_style('#A1B2C3').removeprefix('color: ')


def test_clearing_from_the_row_puts_NONE_back_in_the_registry_not_an_empty_string(
        monkeypatch, nicegui_client, account_id):
    """Survivor: ``view.color = stored`` keeping the ``''``.

    Visually identical -- both resolve to the neutral grey -- and a lie about the
    one distinction the whole colour path is careful with: ``None`` is "the user has
    not chosen", ``''`` is a value that happens to render the same way today.
    """
    set_managed_label(account_id, 'ARK26', target_pct=40.0, color='#D55E00')
    _capture_notifications(monkeypatch)
    views = _one_label(account_id, color='#D55E00')
    root = _draw(nicegui_client, account_id, views)

    _fire(_marked(root, page.MARKER_COLOR_CLEAR)[0])

    assert views[0].color is None


def test_recolouring_a_label_the_render_no_longer_knows_about_does_not_explode(
        nicegui_client, account_id):
    """Survivor: the missing-view guard. Unreachable through the UI today, and the
    kind of thing that becomes reachable the moment the row is rebuilt while a
    menu is open."""
    live = page._new_live_state(base_notional=10_000.0, unallocated_pct=0.0)
    page._recolour_label(live, 'gone', '#D55E00')       # must not raise


def test_the_reserve_card_spans_its_own_line(nicegui_client, account_id):
    """Survivor: the ``w-full``. Moving the card out of the stat row and then
    leaving it content-width makes it a lone stub, which is worse than where it
    started."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    card = _marked(root, page.MARKER_RESERVE_CARD)[0]

    assert 'w-full' in card._classes


def test_the_Fill_100_group_keeps_the_LEFT_of_its_row_for_its_siblings(
        nicegui_client, account_id):
    """Survivor: ``justify-end`` on the button row.

    The wizard's step-2 controls -- Even split, Fill rest, Load last, Wipe -- are
    moving onto this page beside Fill 100%. A right-aligned group pressed up against
    the destructive Remove button is a row that has to be re-laid-out to take them,
    and it puts four harmless buttons next to the one that deletes things.
    """
    from nicegui import ui
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    row = _fill_button(root).parent_slot.parent

    assert 'justify-end' not in ' '.join(row._classes)
    assert [el for el in row.descendants() if isinstance(el, ui.space)]


# ---------------------------------------------------------------------------
# THE SYMBOL INFO PANEL, WIRED INTO THE PAGE
#
# ``ui/components/symbol_info_panel.py`` shipped finished and UNREACHABLE: a
# 1,000-line component, 83 tests of its own, and not one caller. These are the two
# entry points that give it one -- an ⓘ on every symbol row, and Compare over a
# ticked selection -- plus the two guards they share.
#
# THE DOUBLE. ``page.open_symbol_info`` is replaced rather than driven for real:
# the panel fetches from FMP over the network, and no unit test here does that.
# What the double does NOT relax is the CALL: it binds every one against the real
# function's ``inspect.signature``, so a renamed keyword, a missing ``as_of`` or a
# positional/keyword mix-up fails in this file instead of in a browser.
#
# THE CLOCK is frozen and is deliberately NOT today's date: ``as_of=date.today()``
# asserted against ``date.today()`` is a tautology that would survive the page
# passing any other clock at all.
# ---------------------------------------------------------------------------

from datetime import date as _date                             # noqa: E402

#: The page's frozen "today". A Tuesday, and pointedly not the day these tests
#: were written on.
PANEL_AS_OF = _date(2026, 3, 17)


def _panel_calls(monkeypatch):
    """Record every ``open_symbol_info`` call, CHECKED against the real signature."""
    import inspect
    from ba2_trade_platform.ui.components.symbol_info_panel import (
        open_symbol_info as real_open_symbol_info)

    signature = inspect.signature(real_open_symbol_info)
    opened = []

    def _record(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)     # TypeError on a wrong call
        bound.apply_defaults()
        opened.append(dict(bound.arguments))
        return object()

    monkeypatch.setattr(page, 'open_symbol_info', _record)
    return opened


def _fmp_key(monkeypatch, value='FMP-TEST-KEY'):
    """Answer the page's app-setting read, and record WHICH key it asked for."""
    asked = []

    def _get(key, default=None):
        asked.append(key)
        return value

    monkeypatch.setattr(page, 'get_app_setting', _get)
    return asked


def _freeze_today(monkeypatch, day=PANEL_AS_OF):
    """Freeze the page's own clock, so ``as_of`` is a value and not a tautology."""
    class _FrozenDate(_date):
        @classmethod
        def today(cls):
            return day

    monkeypatch.setattr(page, 'date', _FrozenDate)
    return day


def _compare_button(root, index=0):
    from nicegui import ui
    buttons = [el for el in root.descendants()
               if isinstance(el, ui.button) and el._props.get('label') == 'Compare']
    assert buttons, 'no Compare button was drawn'
    return buttons[index]


def _tick(table, *symbols):
    """Tick rows in the table's own checkbox selection, in the order given."""
    by_symbol = {r['symbol']: r for r in table.rows}
    table.selected = [by_symbol[s] for s in symbols]
    return table


# -- the ⓘ on every symbol row -----------------------------------------------

def test_every_symbol_row_carries_an_info_control(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    table = _tables(root)[0]

    assert 'info' in {c['name'] for c in table.columns}
    assert 'body-cell-info' in table.slots
    assert 'symbolInfo' in _listener_types(table)


def test_the_info_control_emits_the_symbol_of_the_row_it_sits_in(nicegui_client,
                                                                  account_id):
    """``props.row.symbol``, not a value captured when the table was built. The
    Vue template is the only thing that knows which row was clicked."""
    root = _draw(nicegui_client, account_id, _three_symbol_label(account_id))

    assert 'props.row.symbol' in _tables(root)[0].slots['body-cell-info'].template


def test_the_info_icon_opens_the_panel_for_ITS_OWN_row(monkeypatch, nicegui_client,
                                                        account_id):
    """THE late-binding test. Three rows are drawn and EACH is clicked in turn: a
    handler that closed over the render loop's variable, or over the label's symbol
    list, opens the last symbol every time and passes a one-row test."""
    opened = _panel_calls(monkeypatch)
    _fmp_key(monkeypatch)
    root = _draw(nicegui_client, account_id, _three_symbol_label(account_id))
    table = _tables(root)[0]

    for symbol in ('AAPL', 'MSFT', 'TSLA'):
        _emit(table, 'symbolInfo', [symbol])

    assert [call['symbols'] for call in opened] == [['AAPL'], ['MSFT'], ['TSLA']]


def test_the_info_control_says_what_it_will_show(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    template = _tables(root)[0].slots['body-cell-info'].template

    assert 'Holdings, dividends and total return' in template


def test_the_info_tooltip_names_the_symbol_and_the_instrument(nicegui_client, account_id):
    """The ⓘ hover identifies WHICH row it belongs to. Reading the company name off
    ``props.row`` (not a second lookup) is what lets one rendered slot serve every row --
    the same constraint that makes ``symbolInfo`` carry the symbol in its emit."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    template = _tables(root)[0].slots['body-cell-info'].template

    assert 'props.row.symbol' in template
    assert 'props.row.company_name' in template


def test_the_info_tooltip_is_readable_rather_than_the_quasar_default(nicegui_client,
                                                                    account_id):
    """Quasar's default tooltip is ~10px. This one carries three lines and has to be
    legible, so it sets its own size and a width to wrap a long company name against."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    template = _tables(root)[0].slots['body-cell-info'].template

    assert 'font-size' in template
    assert 'max-width' in template


def test_an_unnamed_instrument_shows_no_blank_name_line(nicegui_client, account_id):
    """``v-if`` on the name, so a row whose instrument has none draws two lines rather
    than one empty one -- the tooltip must not print ``null`` or a stray gap."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    template = _tables(root)[0].slots['body-cell-info'].template

    assert 'v-if="props.row.company_name"' in template


def test_the_company_name_reaches_the_row_the_tooltip_reads(nicegui_client, account_id):
    """End to end through the payload: a name supplied to the view builder must arrive
    on the table's row data, because the template can only read what is there."""
    views = _one_label(account_id, symbols=('AAPL',),
                       company_names={'AAPL': 'Apple Inc.'})
    root = _draw(nicegui_client, account_id, views)
    row = _tables(root)[0].rows[0]

    assert row['symbol'] == 'AAPL'
    assert row['company_name'] == 'Apple Inc.'


def test_an_unnamed_instrument_carries_an_empty_name_not_none(nicegui_client, account_id):
    """Quasar prints a bare ``null`` if handed one, so the row normalises to ''."""
    views = _one_label(account_id, symbols=('AAPL',), company_names={})
    root = _draw(nicegui_client, account_id, views)
    row = _tables(root)[0].rows[0]

    assert row['company_name'] == ''


def test_every_label_gets_its_own_info_column(nicegui_client, account_id):
    """One table per label, so one wiring per label -- exactly as ``weightChange``
    and ``commentChange`` are wired."""
    views = _views([ManagedLabel('A', 50.0), ManagedLabel('B', 50.0)],
                   {'A': ['AAPL'], 'B': ['MSFT']},
                   weights={'A': {'AAPL': 100.0}, 'B': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    assert all('symbolInfo' in _listener_types(t) for t in _tables(root))


# -- Compare, over the ticked rows -------------------------------------------

def test_every_label_gets_its_own_Compare_button(nicegui_client, account_id):
    views = _views([ManagedLabel('A', 50.0), ManagedLabel('B', 50.0)],
                   {'A': ['AAPL'], 'B': ['MSFT']},
                   weights={'A': {'AAPL': 100.0}, 'B': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)

    from nicegui import ui
    assert len([el for el in root.descendants()
                if isinstance(el, ui.button)
                and el._props.get('label') == 'Compare']) == 2


def test_Compare_passes_every_ticked_symbol_IN_ORDER(monkeypatch, nicegui_client,
                                                      account_id):
    """All of them, and in the order the table hands them over -- the comparison
    columns are laid out left to right in exactly that order."""
    opened = _panel_calls(monkeypatch)
    _fmp_key(monkeypatch)
    root = _draw(nicegui_client, account_id, _three_symbol_label(account_id))
    _tick(_tables(root)[0], 'TSLA', 'AAPL', 'MSFT')

    _press(_compare_button(root))

    assert [call['symbols'] for call in opened] == [['TSLA', 'AAPL', 'MSFT']]


def test_Compare_reads_the_SAME_ticked_rows_the_remove_button_does(
        monkeypatch, nicegui_client, account_id):
    """One selection mechanism, not two. The checkbox column already exists
    (``selection='multiple'``) and "Remove selected from label" already reads it."""
    from ba2_trade_platform.core.utils import get_symbols_by_label
    from nicegui import ui

    opened = _panel_calls(monkeypatch)
    _fmp_key(monkeypatch)
    _capture_notifications(monkeypatch)
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL', 'MSFT', 'TSLA'], 'ARK26')
    root = _draw(nicegui_client, account_id, _three_symbol_label(account_id))
    _tick(_tables(root)[0], 'MSFT')

    _press(_compare_button(root))
    remove = [el for el in root.descendants()
              if isinstance(el, ui.button)
              and el._props.get('label') == 'Remove selected from label'][0]
    _press(remove)

    assert [call['symbols'] for call in opened] == [['MSFT']]
    assert get_symbols_by_label(['ARK26'])['ARK26'] == ['AAPL', 'TSLA']


def test_ONE_reader_of_the_ticked_rows_serves_BOTH_buttons():
    """Survivor: ``_remove_selected`` going back to its own inline
    ``table.selected`` read.

    Behaviourally identical today, which is exactly why it needs saying: two
    readers of one selection is how the two buttons come to disagree about what
    "selected" means -- an ``or []`` on one side, a ``.get('symbol')`` on the
    other. There is one reader, and neither caller touches ``table.selected``.
    """
    import inspect
    source = inspect.getsource(page._render_label_body)

    assert source.count('_selected_symbols(') == 2, source.count('_selected_symbols(')
    assert 'table.selected' not in source


def test_Compare_never_reaches_into_ANOTHER_labels_selection(monkeypatch,
                                                              nicegui_client,
                                                              account_id):
    """Two labels, two tables, two selections. A Compare wired to "the first table"
    rather than to its own would open B's tick from A's button."""
    opened = _panel_calls(monkeypatch)
    _fmp_key(monkeypatch)
    sent = _capture_notifications(monkeypatch)
    views = _views([ManagedLabel('A', 50.0), ManagedLabel('B', 50.0)],
                   {'A': ['AAPL'], 'B': ['MSFT']},
                   weights={'A': {'AAPL': 100.0}, 'B': {'MSFT': 100.0}})
    root = _draw(nicegui_client, account_id, views)
    _tick(_tables(root)[1], 'MSFT')

    _press(_compare_button(root, 0))
    assert opened == []
    assert any('at least one symbol' in m.lower() for m, _t in sent)

    _press(_compare_button(root, 1))
    assert [call['symbols'] for call in opened] == [['MSFT']]


def test_Compare_with_nothing_ticked_says_so_and_opens_NOTHING(monkeypatch,
                                                                nicegui_client,
                                                                account_id):
    """A dialog titled "Symbol info — " over an empty comparison is worse than a
    refusal: it looks like the fetch failed."""
    opened = _panel_calls(monkeypatch)
    _fmp_key(monkeypatch)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _three_symbol_label(account_id))

    _press(_compare_button(root))

    assert opened == []
    assert ('Select at least one symbol first', 'warning') in sent


def test_Compare_sits_with_the_harmless_buttons_and_not_beside_Remove(
        nicegui_client, account_id):
    """The row is "harmless actions | space | the one that deletes things", and it
    is sized for the wizard's step-2 siblings still to come. Compare is a READ; it
    belongs on the left of the space, not wedged against the destructive button."""
    from nicegui import ui
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    row = _fill_button(root).parent_slot.parent
    children = list(row.default_slot.children)
    names = [c._props.get('label', type(c).__name__) for c in children]
    space_at = next(i for i, c in enumerate(children) if isinstance(c, ui.space))

    assert names.index('Compare') < space_at, names
    assert space_at < names.index('Remove selected from label'), names


# -- the two guards, shared by both entry points -----------------------------

def test_a_missing_FMP_key_stops_the_INFO_ICON_and_says_where_to_set_it(
        monkeypatch, nicegui_client, account_id):
    """No silent no-op and no crash: the panel's every figure comes from FMP, so
    with no key there is nothing to show and a reason worth printing."""
    opened = _panel_calls(monkeypatch)
    _fmp_key(monkeypatch, value=None)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'symbolInfo', ['AAPL'])

    assert opened == []
    assert any('FMP_API_KEY' in m and 'Settings' in m for m, _t in sent), sent
    assert [t for _m, t in sent] == ['warning']


def test_a_missing_FMP_key_stops_COMPARE_too(monkeypatch, nicegui_client, account_id):
    """ONE helper behind both entry points -- the guards cannot drift apart."""
    opened = _panel_calls(monkeypatch)
    _fmp_key(monkeypatch, value=None)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _three_symbol_label(account_id))
    _tick(_tables(root)[0], 'AAPL')

    _press(_compare_button(root))

    assert opened == []
    assert any('FMP_API_KEY' in m for m, _t in sent), sent


def test_a_BLANK_FMP_key_is_a_missing_one(monkeypatch, nicegui_client, account_id):
    """The Settings page stores what was typed; an empty box is an empty string,
    and ``get_app_setting`` returns it rather than None."""
    opened = _panel_calls(monkeypatch)
    _fmp_key(monkeypatch, value='')
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'symbolInfo', ['AAPL'])

    assert opened == []
    assert any('FMP_API_KEY' in m for m, _t in sent), sent


def test_the_missing_key_is_reported_even_when_nothing_is_ticked(monkeypatch,
                                                                  nicegui_client,
                                                                  account_id):
    """Which is the actionable half: ticking a row would not have helped."""
    _panel_calls(monkeypatch)
    _fmp_key(monkeypatch, value=None)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _press(_compare_button(root))

    assert any('FMP_API_KEY' in m for m, _t in sent), sent


# -- what the panel is actually handed ---------------------------------------

def test_the_page_reads_the_key_the_SETTINGS_page_writes(monkeypatch, nicegui_client,
                                                          account_id):
    asked = _fmp_key(monkeypatch)
    _panel_calls(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'symbolInfo', ['AAPL'])

    assert asked == ['FMP_API_KEY']


def test_the_key_really_comes_from_the_app_settings_ROW(monkeypatch, nicegui_client,
                                                         account_id):
    """No stub on ``get_app_setting`` here. The row the Settings page writes is the
    row this page has to read, and only an unstubbed read proves the two agree."""
    from ba2_trade_platform.core.db import add_instance
    from ba2_trade_platform.core.models import AppSetting

    add_instance(AppSetting(key='FMP_API_KEY', value_str='ROW-KEY'))
    opened = _panel_calls(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'symbolInfo', ['AAPL'])

    assert [call['api_key'] for call in opened] == ['ROW-KEY']


def test_no_app_settings_row_at_all_is_a_refusal_not_a_crash(monkeypatch,
                                                              nicegui_client,
                                                              account_id):
    """Unstubbed again, against an empty settings table."""
    opened = _panel_calls(monkeypatch)
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'symbolInfo', ['AAPL'])

    assert opened == []
    assert any('FMP_API_KEY' in m for m, _t in sent), sent


def test_the_panel_is_given_the_pages_own_clock_and_the_key(monkeypatch,
                                                             nicegui_client,
                                                             account_id):
    """``as_of`` is REQUIRED by the panel and is the only clock it has."""
    day = _freeze_today(monkeypatch)
    opened = _panel_calls(monkeypatch)
    _fmp_key(monkeypatch)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'symbolInfo', ['AAPL'])

    assert opened[0]['as_of'] == day
    assert opened[0]['api_key'] == 'FMP-TEST-KEY'


def test_the_panel_is_NOT_built_while_the_page_renders(monkeypatch, nicegui_client,
                                                        account_id):
    """It fetches from FMP -- one round trip per symbol. Building it at render time
    would put that behind every refresh of this page, for every managed symbol."""
    opened = _panel_calls(monkeypatch)
    asked = _fmp_key(monkeypatch)

    _draw(nicegui_client, account_id, _three_symbol_label(account_id))

    assert opened == []
    assert asked == []          # not even the key is read until something is clicked


def test_the_panel_is_imported_directly_and_not_through_the_components_package():
    """It is deliberately absent from ``ui/components/__init__.py`` so the eager
    import graph does not grow -- the same convention as ``symbol_chart_data`` and
    ``echart_theme``. Re-exporting it there would undo that."""
    import inspect
    import ba2_trade_platform.ui.components as components

    assert not hasattr(components, 'open_symbol_info')
    assert ('from ..components.symbol_info_panel import open_symbol_info'
            in inspect.getsource(page))


# ---------------------------------------------------------------------------
# THE MIGRATION: the Allocate wizard's target-setting step moves onto the page
#
# Step 1 ("Rebalance - set targets") is gone. Everything that expresses INTENT is
# here; the modal keeps only what commits. This section is the page half.
#
# GROUP 1: the per-row `last` target and the unrealised P&L. Both were only
# readable inside the wizard's step 1 / step 2 captions, and once the boxes moved
# onto the page the wizard was the only screen that could still answer "what did I
# have here before".
# ---------------------------------------------------------------------------

def _label_with_history(account_id, *, previous_target=25.0,
                        previous_weights=None):
    return _views([ManagedLabel('ARK26', 40.0, previous_target_pct=previous_target)],
                  {'ARK26': ['AAPL', 'MSFT']},
                  weights={'ARK26': {'AAPL': 60.0, 'MSFT': 40.0}},
                  previous_weights={'ARK26': dict(previous_weights or {})})


def test_the_label_row_shows_the_target_the_last_run_used(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _label_with_history(account_id))
    assert 'last 25.00%' in _texts(root)


def test_a_label_with_no_history_shows_a_dash_and_never_a_zero(nicegui_client,
                                                               account_id):
    """"never allocated" and "last time this got nothing" are different facts."""
    root = _draw(nicegui_client, account_id,
                 _label_with_history(account_id, previous_target=None))
    assert 'last -' in _texts(root)
    assert 'last 0.00%' not in _texts(root)


def test_the_label_row_shows_its_unrealised_pnl_in_money_and_percent(nicegui_client,
                                                                     account_id):
    """Two symbols bought for $1,000 each and worth $2,500 each: +$3,000, +150%."""
    root = _draw(nicegui_client, account_id, _label_with_history(account_id))
    assert 'P&L +3,000.00 (+150.00%)' in _texts(root)


def test_the_label_pnl_is_coloured_as_an_accent_and_not_as_the_message(
        nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _label_with_history(account_id))
    pnl = [el for el in _marked(root, page.MARKER_LABEL_PNL)]
    assert len(pnl) == 1
    assert 'text-green-500' in ' '.join(pnl[0]._classes)


def test_the_symbol_table_shows_the_weight_the_last_run_used(nicegui_client,
                                                             account_id):
    root = _draw(nicegui_client, account_id,
                 _label_with_history(account_id,
                                     previous_weights={'AAPL': 70.0}))
    rows = _table_rows(root)
    assert rows['AAPL']['previous_weight_pct'] == 70.0
    assert rows['MSFT']['previous_weight_pct'] is None


def test_the_symbol_table_carries_a_last_and_a_pnl_column(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _label_with_history(account_id))
    names = {c['name'] for c in _tables(root)[0].columns}
    assert 'previous_weight_pct' in names
    assert 'pnl' in names


def test_the_symbol_table_shows_each_rows_pnl_in_money_and_percent(nicegui_client,
                                                                   account_id):
    root = _draw(nicegui_client, account_id, _label_with_history(account_id))
    assert _table_rows(root)['AAPL']['pnl'] == '+1,500.00 (+150.00%)'


def test_the_page_reads_the_previous_generation_out_of_the_store(monkeypatch,
                                                                 account_id):
    """End to end: the numbers the last RUN was launched with, not the current ones.

    ``save_allocation_targets`` is the only writer of the previous generation, and
    it shifts on a CHANGE -- so writing 40 over a stored 25 is what puts 25 behind
    "last".
    """
    from ba2_trade_platform.core.portfolio_allocation import LabelTarget, SymbolTarget
    from ba2_trade_platform.core.portfolio_allocation_store import (
        save_allocation_targets)

    set_managed_label(account_id, 'ARK26', target_pct=25.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=70.0)
    save_allocation_targets(account_id, [LabelTarget(
        'ARK26', 40.0, [SymbolTarget('AAPL', 100.0)])])
    _use_account(monkeypatch, _Account(account_id,
                                       positions=[_pos('AAPL', 10, 1000.0, 2500.0)],
                                       prices={'AAPL': 250.0}))

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)
    view = payload['views'][0]
    assert view.previous_target_pct == 25.0
    assert view.rows[0].previous_weight_pct == 70.0


def test_the_migrated_last_figure_is_NOT_restated_against_the_gross_base(
        nicegui_client, account_id):
    """The wizard's step-1 caption read "now X (Y% of base) - target Z% of base".

    Moving that wording across would reintroduce exactly the confusion the
    2026-08-25 rework removed: the page treats the INVESTABLE pool as 100% and the
    of-base figure is the parenthetical "(real N%)". A stored target is typed on
    the investable basis, so ``last`` needs no restating at all.
    """
    root = _draw(nicegui_client, account_id,
                 _label_with_history(account_id), reserve=10.0)
    # The LABEL ROW only. The reserve line above it is deliberately still of-base
    # -- it IS the part held back -- and restating it against what it leaves would
    # be circular, so a whole-page text search would be asserting the wrong thing.
    row = ' '.join(_texts(_marked(root, page.MARKER_BAR_ROW)[0]))

    assert 'last 25.00%' in row
    assert 'of base' not in row
    assert 'of investable' in ' '.join(_expansion_headers(root))


# ---------------------------------------------------------------------------
# GROUP 2: the LABEL-LEVEL button group -- "Even split" and "Load last"
#
# The wizard's step 1 owned these. They are on the page now, above the list they
# rewrite, and they persist on press like everything else here: this page has no
# Save button and cannot have one (switching the global account hard-reloads the
# document).
# ---------------------------------------------------------------------------

def _marked_buttons(root, marker):
    from nicegui import ui
    return [el for el in _marked(root, marker) if isinstance(el, ui.button)]


def _label_targets_now(account_id):
    return {row.label: row.target_pct for row in get_managed_labels(account_id)}


def _two_labels(account_id, *, a=70.0, b=30.0, previous=(None, None)):
    for name, target in (('ARK26', a), ('TECH', b)):
        set_managed_label(account_id, name, target_pct=target)
    return _views([ManagedLabel('ARK26', a, previous_target_pct=previous[0]),
                   ManagedLabel('TECH', b, previous_target_pct=previous[1])],
                  {'ARK26': ['AAPL'], 'TECH': ['MSFT']},
                  weights={'ARK26': {'AAPL': 100.0}, 'TECH': {'MSFT': 100.0}})


# ---------------------------------------------------------------------------
# GROUP 3: the PER-LABEL button group -- Even split / Fill rest / Load last / Wipe
#
# The wizard's step 2 owned these. They join ``Fill 100%`` and ``Compare`` in the
# left-aligned group that was deliberately sized for them (346fdabb / cd3e1646),
# on the harmless side of the ``ui.space()`` that separates them from Remove.
# ---------------------------------------------------------------------------

def _three_symbols(account_id, label='ARK26', weights=None, previous=None):
    set_managed_label(account_id, label, target_pct=100.0)
    return _views([ManagedLabel(label, 100.0)], {label: ['AAPL', 'MSFT', 'TSLA']},
                  weights={label: dict(weights or {'AAPL': 33.33, 'MSFT': 33.33,
                                                   'TSLA': 33.34})},
                  previous_weights={label: dict(previous or {})})


def _two_labelled_baskets(account_id, *, a_weights, b_weights):
    for name in ('ARK26', 'TECH'):
        set_managed_label(account_id, name, target_pct=50.0)
    return _views([ManagedLabel('ARK26', 50.0), ManagedLabel('TECH', 50.0)],
                  {'ARK26': ['AAPL', 'MSFT'], 'TECH': ['NVDA', 'AMD']},
                  weights={'ARK26': dict(a_weights), 'TECH': dict(b_weights)})


def _stored(account_id, label='ARK26', symbols=('AAPL', 'MSFT', 'TSLA')):
    return get_symbol_weights(account_id, label, list(symbols))


def test_every_label_gets_the_four_migrated_buttons(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id,
                 _two_labelled_baskets(account_id,
                                       a_weights={'AAPL': 50.0, 'MSFT': 50.0},
                                       b_weights={'NVDA': 50.0, 'AMD': 50.0}))

    for marker in (page.MARKER_EVEN_SPLIT_SYMBOLS, page.MARKER_FILL_REST_SYMBOLS,
                   page.MARKER_LOAD_LAST_SYMBOLS, page.MARKER_WIPE_SYMBOLS):
        assert len(_marked_buttons(root, marker)) == 2, marker


def test_the_symbol_even_split_writes_an_equal_share_and_persists_it(
        monkeypatch, nicegui_client, account_id):
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbols(account_id,
                                weights={'AAPL': 90.0, 'MSFT': 10.0, 'TSLA': 0.0}))

    _press(_marked_buttons(root, page.MARKER_EVEN_SPLIT_SYMBOLS)[0])

    assert _stored(account_id) == {'AAPL': 33.33, 'MSFT': 33.33, 'TSLA': 33.34}
    assert _table_rows(root)['AAPL']['weight_pct'] == 33.33


def test_fill_rest_fills_only_the_empty_slots_and_leaves_the_typed_ones_alone(
        monkeypatch, nicegui_client, account_id):
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbols(account_id,
                                weights={'AAPL': 30.0, 'MSFT': 0.0, 'TSLA': 0.0}))

    _press(_marked_buttons(root, page.MARKER_FILL_REST_SYMBOLS)[0])

    assert _stored(account_id) == {'AAPL': 30.0, 'MSFT': 35.0, 'TSLA': 35.0}


def test_fill_rest_REFUSES_an_over_allocated_label_where_fill_100_scales_it(
        monkeypatch, nicegui_client, account_id):
    """The two buttons are not duplicates: one repairs a set, the other only fills
    the gaps in one. A Fill rest that scaled would rewrite weights the user typed."""
    sent = _capture_notifications(monkeypatch)
    over = {'AAPL': 80.0, 'MSFT': 80.0, 'TSLA': 0.0}
    root = _draw(nicegui_client, account_id, _three_symbols(account_id, weights=over))

    _press(_marked_buttons(root, page.MARKER_FILL_REST_SYMBOLS)[0])
    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert any('nothing to fill' in m for m, _t in sent)

    _press(_fill_button(root))
    assert round(sum(_stored(account_id).values()), 2) == 100.0


def test_symbol_load_last_restores_the_shares_of_the_last_run(monkeypatch,
                                                              nicegui_client,
                                                              account_id):
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbols(account_id,
                                weights={'AAPL': 90.0, 'MSFT': 5.0, 'TSLA': 5.0},
                                previous={'AAPL': 10.0, 'MSFT': 10.0,
                                          'TSLA': 80.0}))

    _press(_marked_buttons(root, page.MARKER_LOAD_LAST_SYMBOLS)[0])

    assert _stored(account_id) == {'AAPL': 10.0, 'MSFT': 10.0, 'TSLA': 80.0}


def test_symbol_load_last_reads_the_PREVIOUS_generation_not_the_live_one(
        monkeypatch, nicegui_client, account_id):
    """Mutation: hand it ``live['weights']``. The press then reports success and
    changes nothing, which is the one failure this button cannot have."""
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbols(account_id,
                                weights={'AAPL': 90.0, 'MSFT': 5.0, 'TSLA': 5.0},
                                previous={'AAPL': 33.33, 'MSFT': 33.33,
                                          'TSLA': 33.34}))

    _press(_marked_buttons(root, page.MARKER_LOAD_LAST_SYMBOLS)[0])

    assert _stored(account_id) == {'AAPL': 33.33, 'MSFT': 33.33, 'TSLA': 33.34}
    assert any('restored' in m.lower() for m, _t in sent)


def test_symbol_load_last_with_no_history_says_so_and_writes_nothing(
        monkeypatch, nicegui_client, account_id):
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _three_symbols(account_id))

    _press(_marked_buttons(root, page.MARKER_LOAD_LAST_SYMBOLS)[0])

    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert any('nothing to load' in m for m, _t in sent)


def test_wipe_clears_its_own_labels_shares_to_zero(monkeypatch, nicegui_client,
                                                   account_id):
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _three_symbols(account_id))

    _press(_marked_buttons(root, page.MARKER_WIPE_SYMBOLS)[0])

    assert _stored(account_id) == {'AAPL': 0.0, 'MSFT': 0.0, 'TSLA': 0.0}


def test_wipe_clears_NOTHING_outside_its_own_label(monkeypatch, nicegui_client,
                                                   account_id):
    """The mutation: ``for label in live['weights']`` instead of the one label. The
    user presses Wipe on one basket and every basket on the page empties."""
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _two_labelled_baskets(account_id,
                                       a_weights={'AAPL': 60.0, 'MSFT': 40.0},
                                       b_weights={'NVDA': 70.0, 'AMD': 30.0}))

    _press(_marked_buttons(root, page.MARKER_WIPE_SYMBOLS)[0])

    assert _stored(account_id, 'ARK26', ('AAPL', 'MSFT')) == {'AAPL': 0.0,
                                                              'MSFT': 0.0}
    assert get_symbol_rows(account_id, 'TECH') == {}


def test_each_migrated_button_writes_to_the_label_whose_row_it_sits_in(
        monkeypatch, nicegui_client, account_id):
    """The classic NiceGUI closure bug: without a default-argument capture every
    button on the page rewrites the LAST label drawn."""
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _two_labelled_baskets(account_id,
                                       a_weights={'AAPL': 90.0, 'MSFT': 10.0},
                                       b_weights={'NVDA': 70.0, 'AMD': 30.0}))

    _press(_marked_buttons(root, page.MARKER_EVEN_SPLIT_SYMBOLS)[0])

    assert _stored(account_id, 'ARK26', ('AAPL', 'MSFT')) == {'AAPL': 50.0,
                                                              'MSFT': 50.0}
    assert get_symbol_rows(account_id, 'TECH') == {}


def test_the_SECOND_labels_button_writes_to_the_SECOND_label(monkeypatch,
                                                             nicegui_client,
                                                             account_id):
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _two_labelled_baskets(account_id,
                                       a_weights={'AAPL': 90.0, 'MSFT': 10.0},
                                       b_weights={'NVDA': 70.0, 'AMD': 30.0}))

    _press(_marked_buttons(root, page.MARKER_WIPE_SYMBOLS)[1])

    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert _stored(account_id, 'TECH', ('NVDA', 'AMD')) == {'NVDA': 0.0, 'AMD': 0.0}


def test_a_wipe_leaves_the_history_so_load_last_is_still_its_undo(
        monkeypatch, nicegui_client, account_id):
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbols(account_id,
                                previous={'AAPL': 50.0, 'MSFT': 30.0, 'TSLA': 20.0}))

    _press(_marked_buttons(root, page.MARKER_WIPE_SYMBOLS)[0])
    _press(_marked_buttons(root, page.MARKER_LOAD_LAST_SYMBOLS)[0])

    assert _stored(account_id) == {'AAPL': 50.0, 'MSFT': 30.0, 'TSLA': 20.0}


def test_the_migrated_buttons_never_touch_the_labels_own_target(monkeypatch,
                                                                nicegui_client,
                                                                account_id):
    """Shares WITHIN a label are a different denominator from the label's share of
    the investable pool. A button that moved both would be mixing the two."""
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _three_symbols(account_id))

    _press(_marked_buttons(root, page.MARKER_WIPE_SYMBOLS)[0])

    assert _label_targets_now(account_id) == {'ARK26': 100.0}


def test_the_migrated_buttons_never_touch_the_cash_reserve(monkeypatch,
                                                           nicegui_client,
                                                           account_id):
    _capture_notifications(monkeypatch)
    set_allocation_config(account_id, unallocated_pct=15.0)
    root = _draw(nicegui_client, account_id, _three_symbols(account_id), reserve=15.0)

    _press(_marked_buttons(root, page.MARKER_EVEN_SPLIT_SYMBOLS)[0])

    assert get_allocation_config(account_id).unallocated_pct == 15.0


def test_a_migrated_button_refuses_to_write_under_an_unmanaged_label(
        monkeypatch, nicegui_client, account_id):
    sent = _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbols(account_id,
                                weights={'AAPL': 90.0, 'MSFT': 5.0, 'TSLA': 5.0}))
    remove_managed_label(account_id, 'ARK26')

    _press(_marked_buttons(root, page.MARKER_EVEN_SPLIT_SYMBOLS)[0])

    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert any('no longer managed' in m for m, _t in sent)


def test_the_migrated_buttons_read_the_SAME_live_map_the_inline_edit_writes(
        monkeypatch, nicegui_client, account_id):
    """One source of truth for what is on screen. If a button re-read the store it
    would be blind to the edit the user just made, and the two would contradict
    each other on every row."""
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id, _three_symbols(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', 0.0])      # 0 == EMPTY
    _press(_marked_buttons(root, page.MARKER_FILL_REST_SYMBOLS)[0])

    assert _stored(account_id) == {'AAPL': 33.33, 'MSFT': 33.33, 'TSLA': 33.34}


def test_the_four_migrated_buttons_land_LEFT_of_the_space_beside_Fill_100(
        nicegui_client, account_id):
    """The group was sized for exactly these four siblings (346fdabb). Everything
    constructive sits before the ``ui.space()``; only Remove is after it."""
    from nicegui import ui
    root = _draw(nicegui_client, account_id, _three_symbols(account_id))
    row = _fill_button(root).parent_slot.parent

    order = [el for el in row.descendants()
             if isinstance(el, (ui.button, ui.space))]
    captions = [el._props.get('label', '<space>') if isinstance(el, ui.button)
                else '<space>' for el in order]
    assert captions[:7] == ['Fill 100%', 'Even split', 'Fill rest', 'Load last',
                            'Load current', 'Wipe', 'Compare']
    assert captions[7] == '<space>'
    assert captions[8] == 'Remove selected from label'


# ---------------------------------------------------------------------------
# GROUP 5: ONE SOURCE OF TRUTH, and the one execution control that stays
#
# The page and the wizard used to derive the same figures independently -- the
# wizard's "target 13.50% of base" and the page's "tgt 13.5%" were one number
# computed twice, on two denominators. There is one now, and these are the tests
# that say so end to end: a target typed on the page is what the dry run solves
# against, with no re-derivation in between.
#
# ``allow fractional shares`` is the ONE control left at the gate, because it
# changes WHICH ORDERS are produced rather than what is being aimed at -- and
# toggling it has to recompute the plan, not merely record a preference.
# ---------------------------------------------------------------------------

def _fractional_account(account_id, *, price=300.0):
    """A book whose target does not land on a whole share: 100% of a 50,000 base
    at 300 a share is 166.66 shares fractionally and 166 whole."""
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': price})
    account.margin = {'AAPL': MarginInfo(symbol='AAPL', fractionable=True,
                                         bp_factor=1.0)}
    return account


def test_a_target_typed_on_the_page_is_what_the_dry_run_solves_against(
        monkeypatch, nicegui_client, account_id):
    """The inline box writes ``portfolio_allocation_label.target_pct``;
    ``_load_flow_inputs`` reads that same column back when the gate opens. Nothing
    in between restates it, so 60% typed is 60% solved -- $30,000 of a $50,000
    base, not the $50,000 an un-read target would have deployed."""
    set_managed_label(account_id, 'ARK26', target_pct=0.0)
    set_managed_label(account_id, 'CASHY', target_pct=40.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    add_label_to_instruments(['MSFT'], 'CASHY')
    # Saved shares: the book here is flat, and an unsaved share now defaults to
    # the symbol's ACTUAL one, which on a flat book is a real 0%.
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=100.0)
    set_symbol_weight(account_id, 'CASHY', 'MSFT', weight_pct=100.0)
    account = _AllocAccount(account_id, {'manual_trading_enabled': True},
                            positions=[], prices={'AAPL': 100.0, 'MSFT': 100.0})
    _use_account(monkeypatch, account)
    _capture_notifications(monkeypatch)

    root = _draw(nicegui_client, account_id,
                 _views([ManagedLabel('ARK26', 0.0), ManagedLabel('CASHY', 40.0)],
                        {'ARK26': ['AAPL'], 'CASHY': ['MSFT']},
                        weights={'ARK26': {'AAPL': 100.0},
                                 'CASHY': {'MSFT': 100.0}}))
    # The user types 60 into ARK26's box. Nothing else is touched.
    box = [n for n in _numbers(root)
           if n._props.get('label') == 'Portfolio target %'][0]
    _drive_value(box, 60.0)
    assert _label_targets_now(account_id)['ARK26'] == 60.0

    opened = _drive_the_flow(monkeypatch, nicegui_client, account_id)

    by_symbol = {r.symbol: r for r in opened['plan'].rows}
    assert opened['plan'].base_notional == 50_000.0
    assert by_symbol['AAPL'].target_notional == 30_000.0


def test_the_dry_run_does_not_RE_DERIVE_the_target_it_was_given(monkeypatch,
                                                                nicegui_client,
                                                                account_id):
    """The two screens print one number. Whatever the page's own header says the
    label is aiming at, the plan aims at the same thing."""
    from ba2_trade_platform.core.portfolio_allocation_store import set_allocation_config

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=100.0)
    set_allocation_config(account_id, unallocated_pct=10.0)

    opened = _drive_the_flow(monkeypatch, nicegui_client, account_id)

    # The page's own pure layer, asked the same question about the same account.
    views = _views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                   base=opened['plan'].base_notional, reserve=10.0,
                   weights={'ARK26': {'AAPL': 100.0}})
    assert views[0].target_value == opened['plan'].investable_notional
    assert opened['plan'].rows[0].target_notional == views[0].target_value


def test_toggling_the_fractional_switch_RE_SOLVES_the_plan(monkeypatch,
                                                           nicegui_client,
                                                           account_id):
    """Not "a handler fired": the QUANTITIES change. 100% of a 50,000 base at 300 a
    share is 166.66666 fractional shares and 166 whole ones, so the plan the user
    is about to submit is a different plan."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from nicegui import ui

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=100.0)
    account = _fractional_account(account_id)
    _use_account(monkeypatch, account)
    _capture_notifications(monkeypatch)

    opened = {}
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.update(kw, base=a[0], plan=a[1]))
    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_COST, _noop_refresh))

    with nicegui_client:
        wizard = wiz.AllocationWizard(opened['base'], opened['plan'],
                                      market=opened['market'],
                                      on_refresh=opened['on_refresh'],
                                      on_submit=lambda p: None)
        wizard.open()
        before = wizard.plan.rows[0].delta_quantity
        switch = [el for el in nicegui_client.layout.descendants()
                  if isinstance(el, ui.switch)][0]
        switch.set_value(False)
        after = wizard.plan.rows[0].delta_quantity

    assert before != after
    assert before == pytest.approx(166.6666, abs=1e-3)
    assert after == 166.0


def test_the_re_solved_plan_is_what_SUBMIT_would_send(monkeypatch, nicegui_client,
                                                      account_id):
    """A toggle that redrew the table but left ``self.plan`` behind would show the
    user one plan and submit another."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from nicegui import ui

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=100.0)
    _use_account(monkeypatch, _fractional_account(account_id))
    _capture_notifications(monkeypatch)

    opened, submitted = {}, []
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.update(kw, base=a[0], plan=a[1]))
    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_COST, _noop_refresh))

    with nicegui_client:
        wizard = wiz.AllocationWizard(opened['base'], opened['plan'],
                                      market=opened['market'],
                                      on_refresh=opened['on_refresh'],
                                      on_submit=submitted.append)
        wizard.open()
        switch = [el for el in nicegui_client.layout.descendants()
                  if isinstance(el, ui.switch)][0]
        switch.set_value(False)
        wizard._submit()

    assert [r.delta_quantity for r in submitted[0].rows] == [166.0]


def test_the_fractional_toggle_is_remembered_for_the_next_run(monkeypatch,
                                                              nicegui_client,
                                                              account_id):
    """It is the account's answer, not the dialog's. ``_on_refresh`` persists it."""
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
    from nicegui import ui

    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=100.0)
    _use_account(monkeypatch, _fractional_account(account_id))
    _capture_notifications(monkeypatch)

    opened = {}
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.update(kw, base=a[0], plan=a[1]))
    _run_in_client(nicegui_client, lambda: page._open_allocation_flow(
        account_id, VALUATION_MODE_COST, _noop_refresh))

    with nicegui_client:
        wizard = wiz.AllocationWizard(opened['base'], opened['plan'],
                                      market=opened['market'],
                                      on_refresh=opened['on_refresh'],
                                      on_submit=lambda p: None)
        wizard.open()
        switch = [el for el in nicegui_client.layout.descendants()
                  if isinstance(el, ui.switch)][0]
        switch.set_value(False)

    assert get_allocation_config(account_id).allow_fractional is False


# ---------------------------------------------------------------------------
# THE COMMA, END TO END
#
# The live page renders share cells as "11,11", and Quasar hands the raw string
# back on change. Stripping the comma as a grouping mark made "0,5" arrive as 5.0
# -- in range, silently accepted, ten times what was typed.
# ---------------------------------------------------------------------------

def test_a_comma_decimal_typed_into_a_share_cell_is_stored_as_typed(
        nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', '26,78'])

    assert get_symbol_rows(account_id, 'ARK26')['AAPL'].weight_pct == 26.78


def test_the_silently_accepted_comma_no_longer_stores_ten_times_the_share(
        nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    _emit(_tables(root)[0], 'weightChange', ['AAPL', '0,5'])

    assert get_symbol_rows(account_id, 'ARK26')['AAPL'].weight_pct == 0.5


def test_the_share_cell_is_the_one_control_that_can_hand_back_a_RAW_string(
        nicegui_client, account_id):
    """Which is why the comma is a table-cell problem and not a page-wide one.

    The label target and the reserve are ``ui.number``s: NiceGUI coerces their
    model value and a comma never reaches the parser through them (a string put
    into one raises inside ``Number._value_to_model_value``). The share cell is a
    hand-written Quasar ``q-input`` in a table slot that emits whatever it is
    holding, so it is the only widget on this page whose handler must survive
    "26,78" -- and the parse is shared, so fixing it there fixed all three.
    """
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    root = _draw(nicegui_client, account_id, _one_label(account_id))

    assert 'weightChange' in _listener_types(_tables(root)[0])
    box = [n for n in _numbers(root)
           if n._props.get('label') == 'Portfolio target %'][0]
    with pytest.raises(Exception):
        box.set_value('13,5')


# ---------------------------------------------------------------------------
# THE LABEL-TOTAL CARD, ON THE PAGE
#
# It was a sentence under the stat cards that only appeared when the set was
# wrong. It is a card in the same row now, built from the SAME ``card_classes``
# as "Managed labels" -- one card component, not two.
#
# REAL TIME means the DISPLAYED TOTAL recomputes as you type. It does NOT mean
# targets recalculate: the user rejected auto-recalculation outright earlier in
# this project, and nothing about this card may cause a re-solve, a re-plan or a
# write beyond the one the edited box was already making.
# ---------------------------------------------------------------------------

def _card_titles(root):
    return [el._text for el in _marked(root, page.MARKER_SUMMARY_ROW)[0].descendants()
            if 'text-xs text-secondary-custom' in ' '.join(el._classes)
            and el._text]


def _total_card_texts(root, index=0):
    """Every text under the "Managed labels" card, which now HOLDS the total bar."""
    row = _marked(root, page.MARKER_TOTAL_BAR_ROW)[index]
    return [el._text for el in row.parent_slot.parent.descendants() if el._text]


def test_the_label_total_is_a_FILL_BAR_inside_the_managed_labels_card(
        nicegui_client, account_id):
    """Not a fourth card beside it: ONE card answering both halves of one question
    -- how many labels, and how much of the pool they add up to."""
    root = _draw(nicegui_client, account_id, _two_labels(account_id, a=70.0, b=30.0))
    rows = _marked(root, page.MARKER_TOTAL_BAR_ROW)

    assert len(rows) == 1
    assert len(_marked(root, page.MARKER_TOTAL_BAR_FILL)) == 1
    # ...inside the summary row, and inside the card that carries the COUNT.
    assert rows[0] in list(_marked(root, page.MARKER_SUMMARY_ROW)[0].descendants())
    texts = _total_card_texts(root)
    assert 'Managed labels' in texts and '2' in texts


def test_the_total_bar_names_its_own_denominator(nicegui_client, account_id):
    """Three bars on this page divide three different things."""
    root = _draw(nicegui_client, account_id, _two_labels(account_id))

    assert _view_mod().LABEL_TOTAL_BAR_CAPTION in _total_card_texts(root)


def test_the_total_bar_FILLS_in_proportion_to_the_total(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _two_labels(account_id, a=40.0, b=35.0))

    assert _marked(root, page.MARKER_TOTAL_BAR_FILL)[0]._style['width'] == '75.00%'


def test_the_summary_row_keeps_ONE_card_style(nicegui_client, account_id):
    """A forked second style is how the row goes ragged again the next time any of
    them is touched.

    TWO cards now, not four: the three money figures share one box (see
    ``summary_figures``). The invariant is unchanged -- whatever is in the row is
    the same box."""
    root = _draw(nicegui_client, account_id, _two_labels(account_id))
    summary = _marked(root, page.MARKER_SUMMARY_ROW)[0]
    cards = [el for el in summary.descendants()
             if 'stat-card' in ' '.join(el._classes)]
    styles = {' '.join(sorted(el._classes)) for el in cards}

    assert len(cards) >= 2
    assert len(styles) == 1, styles


def test_the_total_bar_is_the_SAME_component_as_the_other_two(nicegui_client,
                                                              account_id):
    """Three scopes of one idea. Three tracks drawn three ways is three things for
    the user to learn."""
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=10.0),
                 base=5_260.9, buying_power=245.50, reserve=10.0)

    tracks = [el.parent_slot.parent for marker in (page.MARKER_TOTAL_BAR_FILL,
                                                   page.MARKER_SYMBOL_BAR_FILL,
                                                   page.MARKER_RESERVE_BAR_FILL)
              for el in _marked(root, marker)]
    assert len(tracks) == 3
    assert {page.BAR_TRACK_STYLE.rstrip(';')} == {
        ';'.join(f'{k}:{v}' for k, v in t._style.items()) for t in tracks}


def test_the_total_bar_shows_the_figure_even_when_the_set_is_RIGHT(nicegui_client,
                                                                    account_id):
    """The old sentence appeared only when the set was wrong, so the running total
    was missing at exactly the moment the user was typing towards it."""
    root = _draw(nicegui_client, account_id, _two_labels(account_id, a=70.0, b=30.0))

    assert '100.00%' in _total_card_texts(root)


def test_the_total_bar_carries_the_engines_shortfall_sentence(nicegui_client,
                                                               account_id):
    root = _draw(nicegui_client, account_id, _two_labels(account_id, a=40.0, b=35.0))
    texts = _total_card_texts(root)

    assert '75.00%' in texts
    assert any('under 100%' in t and 'Unallocated box' in t for t in texts)


def test_the_total_bar_keeps_the_guidance_reachable_at_every_state(nicegui_client,
                                                                    account_id):
    """The "use the Unallocated box" clause rides inside the SHORTFALL sentence, so
    it vanishes the moment the set is right or over. The tooltip carries it at
    every state, which is when a user decides to leave a gap on purpose."""
    root = _draw(nicegui_client, account_id, _two_labels(account_id, a=70.0, b=30.0))
    card = _marked(root, page.MARKER_TOTAL_BAR_ROW)[0].parent_slot.parent
    tips = _tooltip_texts(card)

    assert len(tips) == 1
    assert 'Unallocated reserve' in tips[0]


def test_the_total_bar_follows_an_inline_target_edit_IN_REAL_TIME(nicegui_client,
                                                                   account_id):
    root = _draw(nicegui_client, account_id, _two_labels(account_id, a=40.0, b=30.0))
    assert '70.00%' in _total_card_texts(root)

    _drive_value(_target_box(root, 0), 70.0)

    assert '100.00%' in _total_card_texts(root)
    assert '70.00%' not in _total_card_texts(root)


def test_the_total_bar_does_NOT_move_for_a_SYMBOL_level_press(monkeypatch,
                                                               nicegui_client,
                                                               account_id):
    """Fill 100% and Wipe rewrite shares WITHIN a label; the label's own target is
    a different denominator and does not move. The live readout for those is the
    per-label symbol bar, not this card -- a card that twitched on a symbol edit
    would be claiming the portfolio split had changed when it had not."""
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbols(account_id,
                                weights={'AAPL': 30.0, 'MSFT': 0.0, 'TSLA': 0.0}))
    before = _total_card_texts(root)

    _press(_fill_button(root))
    _press(_marked_buttons(root, page.MARKER_WIPE_SYMBOLS)[0])

    assert _total_card_texts(root) == before


def test_the_total_bars_verdict_is_COLOURED_by_its_severity(nicegui_client,
                                                              account_id):
    """Orange short, red over. An over-100 set is not reachable through the inline
    box -- ``validate_label_target_edit`` refuses the keystroke -- but the DATABASE
    can already hold one, written by the wizard before the boxes moved here, and
    the card has to say so before the user presses Allocate."""
    short = _draw(nicegui_client, account_id, _two_labels(account_id, a=40.0, b=30.0))
    assert 'text-orange-400' in ' '.join(
        _marked(short, page.MARKER_TOTAL_BAR_DETAIL)[0]._classes)

    over = _draw(nicegui_client, account_id, _two_labels(account_id, a=70.0, b=48.0))
    detail = _marked(over, page.MARKER_TOTAL_BAR_DETAIL)[-1]
    assert 'text-red-400' in ' '.join(detail._classes)
    assert '118.00%' in _total_card_texts(over, -1)


def test_the_total_bars_verdict_goes_QUIET_once_the_set_is_right(nicegui_client,
                                                                  account_id):
    root = _draw(nicegui_client, account_id, _two_labels(account_id, a=40.0, b=30.0))
    detail = _marked(root, page.MARKER_TOTAL_BAR_DETAIL)[0]
    assert 'text-orange-400' in ' '.join(detail._classes)

    _drive_value(_target_box(root, 0), 70.0)

    assert 'text-orange-400' not in ' '.join(detail._classes)
    assert 'text-red-400' not in ' '.join(detail._classes)


def test_editing_a_target_NEVER_re_solves_or_re_plans(monkeypatch, nicegui_client,
                                                      account_id):
    """The user rejected auto-recalculation. "Real time" is the DISPLAYED total
    recomputing, not the plan being rebuilt behind them -- a page that quietly
    solved on every keystroke would also be issuing broker calls per character."""
    solves, opened = [], []
    monkeypatch.setattr(page, '_solve_plan',
                        lambda *a, **kw: solves.append(kw) or pytest.fail(
                            'the page re-solved on an edit'))
    monkeypatch.setattr(page, 'open_allocation_wizard',
                        lambda *a, **kw: opened.append(kw))
    root = _draw(nicegui_client, account_id, _two_labels(account_id, a=40.0, b=30.0))

    _drive_value(_target_box(root, 0), 70.0)

    assert solves == [] and opened == []
    assert '100.00%' in _total_card_texts(root)


def test_editing_one_target_leaves_every_SIBLING_target_where_it_was(
        nicegui_client, account_id):
    """The other half of "no auto-recalculation": the card recomputes, the numbers
    do not. Nothing may rebalance the siblings to make the total come out at 100."""
    root = _draw(nicegui_client, account_id, _two_labels(account_id, a=40.0, b=30.0))

    _drive_value(_target_box(root, 0), 55.0)

    assert _label_targets_now(account_id) == {'ARK26': 55.0, 'TECH': 30.0}
    assert '85.00%' in _total_card_texts(root)


# ---------------------------------------------------------------------------
# THE SHARE DEFAULT, END TO END: actual, not fair share
# ---------------------------------------------------------------------------

def test_the_page_defaults_an_unsaved_share_to_the_symbols_ACTUAL_share(
        monkeypatch, account_id):
    """The live screen showed nine symbols in one label all reading 11.11 while
    their real shares were 26.78 / 22.19 / ... -- a target nobody chose, in a box
    that trades."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL', 'MSFT', 'TSLA'], 'ARK26')
    _use_account(monkeypatch, _Account(
        account_id,
        positions=[_pos('AAPL', 10, 500.0, 5000.0), _pos('MSFT', 10, 300.0, 3000.0),
                   _pos('TSLA', 10, 200.0, 2000.0)],
        prices={'AAPL': 500.0, 'MSFT': 300.0, 'TSLA': 200.0}))

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    assert {r.symbol: r.weight_pct for r in payload['views'][0].rows} == {
        'AAPL': 50.0, 'MSFT': 30.0, 'TSLA': 20.0}


def test_the_page_no_longer_shows_the_FAIR_SHARE_default(monkeypatch, account_id):
    """The exact number being removed: three unsaved symbols at 33.33 apiece."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL', 'MSFT', 'TSLA'], 'ARK26')
    _use_account(monkeypatch, _Account(
        account_id,
        positions=[_pos('AAPL', 10, 500.0, 5000.0), _pos('MSFT', 10, 300.0, 3000.0),
                   _pos('TSLA', 10, 200.0, 2000.0)],
        prices={'AAPL': 500.0, 'MSFT': 300.0, 'TSLA': 200.0}))

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    assert [r.weight_pct for r in payload['views'][0].rows] != [33.33, 33.33, 33.34]


def test_a_SAVED_share_still_wins_over_the_actual_one(monkeypatch, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL', 'MSFT'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=90.0)
    _use_account(monkeypatch, _Account(
        account_id,
        positions=[_pos('AAPL', 1, 100.0, 100.0), _pos('MSFT', 10, 900.0, 900.0)],
        prices={'AAPL': 100.0, 'MSFT': 90.0}))

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    by_symbol = {r.symbol: r for r in payload['views'][0].rows}
    assert by_symbol['AAPL'].weight_pct == 90.0
    assert by_symbol['AAPL'].weight_source == _view_mod().WEIGHT_SOURCE_SAVED
    assert by_symbol['MSFT'].weight_source == _view_mod().WEIGHT_SOURCE_ACTUAL


def test_a_saved_share_of_ZERO_is_not_mistaken_for_an_absent_row(monkeypatch,
                                                                 account_id):
    """The inverse mutation. Reading a stored 0.0 as "unsaved" would default it
    back to the position's actual share -- buying back something the user sold out
    of on purpose."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL', 'MSFT'], 'ARK26')
    set_symbol_weight(account_id, 'ARK26', 'AAPL', weight_pct=0.0)
    _use_account(monkeypatch, _Account(
        account_id,
        positions=[_pos('AAPL', 10, 5000.0, 5000.0), _pos('MSFT', 10, 5000.0, 5000.0)],
        prices={'AAPL': 500.0, 'MSFT': 500.0}))

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)
    by_symbol = {r.symbol: r for r in payload['views'][0].rows}

    assert by_symbol['AAPL'].weight_pct == 0.0
    assert by_symbol['AAPL'].weight_source == _view_mod().WEIGHT_SOURCE_SAVED


def test_a_price_outage_cannot_quietly_zero_a_labels_targets(monkeypatch, account_id):
    """The house bug, in the one place it would be most expensive: 0% is "hold
    none of this" and the plan sells the whole position out."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL', 'DARK'], 'ARK26')
    _use_account(monkeypatch, _Account(
        account_id,
        positions=[_pos('AAPL', 10, 5000.0, 5000.0), _pos('DARK', 10, 1000.0, None)],
        prices={'AAPL': 500.0, 'DARK': None}))

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)

    assert [r.weight_pct for r in payload['views'][0].rows] == [None, None]
    assert {r.weight_source for r in payload['views'][0].rows} == {
        _view_mod().WEIGHT_SOURCE_UNKNOWN}


def test_a_symbol_that_is_genuinely_FLAT_defaults_to_a_real_zero(monkeypatch,
                                                                 account_id):
    """The inverse of the outage: nothing held is perfectly measurable. It must
    not be reported as unknown, or the page would cry outage on every new symbol."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL', 'CAS'], 'ARK26')
    _use_account(monkeypatch, _Account(
        account_id, positions=[_pos('AAPL', 10, 5000.0, 5000.0)],
        prices={'AAPL': 500.0, 'CAS': 12.0}))

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)
    by_symbol = {r.symbol: r for r in payload['views'][0].rows}

    assert by_symbol['CAS'].weight_pct == 0.0
    assert by_symbol['CAS'].weight_source == _view_mod().WEIGHT_SOURCE_ACTUAL


def test_the_page_SAYS_why_a_share_is_showing_zero_rather_than_leaving_it_odd(
        nicegui_client, account_id):
    """Under fair share a newly added symbol would have been bought. Under actual
    it sits at 0 and will not be -- which is correct, and has to be legible."""
    views = _views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL', 'CAS']},
                   positions=[_pos('AAPL', 10, 5000.0, 5000.0)],
                   prices={'AAPL': 500.0, 'CAS': 12.0})
    root = _draw(nicegui_client, account_id, views)

    assert _view_mod().SHARE_DEFAULT_NOTE in _texts(root)


def test_the_solve_path_defaults_the_SAME_WAY_the_page_displays(monkeypatch,
                                                                account_id):
    """One source of truth, again. A page showing 50/30/20 while the plan solved
    33.33/33.33/33.34 is the two-screens bug with a fresh coat of paint."""
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    add_label_to_instruments(['AAPL', 'MSFT', 'TSLA'], 'ARK26')
    account = _AllocAccount(
        account_id, {'manual_trading_enabled': True},
        positions=[_held('AAPL', 10, 500.0, 5000.0), _held('MSFT', 10, 300.0, 3000.0),
                   _held('TSLA', 10, 200.0, 2000.0)],
        prices={'AAPL': 500.0, 'MSFT': 300.0, 'TSLA': 200.0})
    _use_account(monkeypatch, account)

    payload = page._load_view_payload(account_id, VALUATION_MODE_MARKET)
    _base, labels, _frac, _reserve = page._load_flow_inputs(
        account_id, VALUATION_MODE_MARKET)

    assert {st.symbol: st.weight_pct for st in labels[0].symbols} == \
        {r.symbol: r.weight_pct for r in payload['views'][0].rows}


def test_every_label_gets_a_LOAD_CURRENT_button(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id,
                 _two_labelled_baskets(account_id,
                                       a_weights={'AAPL': 50.0, 'MSFT': 50.0},
                                       b_weights={'NVDA': 50.0, 'AMD': 50.0}))

    assert len(_marked_buttons(root, page.MARKER_LOAD_CURRENT_SYMBOLS)) == 2


def test_load_current_writes_the_actual_shares_and_persists_them(monkeypatch,
                                                                 nicegui_client,
                                                                 account_id):
    _capture_notifications(monkeypatch)
    views = _views([ManagedLabel('ARK26', 100.0)],
                   {'ARK26': ['AAPL', 'MSFT', 'TSLA']},
                   weights={'ARK26': {'AAPL': 33.33, 'MSFT': 33.33, 'TSLA': 33.34}},
                   positions=[_pos('AAPL', 10, 500.0, 5000.0),
                              _pos('MSFT', 10, 300.0, 3000.0),
                              _pos('TSLA', 10, 200.0, 2000.0)],
                   prices={'AAPL': 500.0, 'MSFT': 300.0, 'TSLA': 200.0})
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id, views)

    _press(_marked_buttons(root, page.MARKER_LOAD_CURRENT_SYMBOLS)[0])

    assert _stored(account_id) == {'AAPL': 50.0, 'MSFT': 30.0, 'TSLA': 20.0}


def test_load_current_writes_to_its_OWN_label_and_no_other(monkeypatch,
                                                           nicegui_client,
                                                           account_id):
    """The closure bug and the all-labels bug in one: without the capture it
    rewrites the LAST basket drawn, and a loop rewrites every one of them."""
    _capture_notifications(monkeypatch)
    for name in ('ARK26', 'TECH'):
        set_managed_label(account_id, name, target_pct=50.0)
    views = _views([ManagedLabel('ARK26', 50.0), ManagedLabel('TECH', 50.0)],
                   {'ARK26': ['AAPL', 'MSFT'], 'TECH': ['NVDA', 'AMD']},
                   weights={'ARK26': {'AAPL': 50.0, 'MSFT': 50.0},
                            'TECH': {'NVDA': 50.0, 'AMD': 50.0}},
                   positions=[_pos('AAPL', 10, 800.0, 8000.0),
                              _pos('MSFT', 10, 200.0, 2000.0),
                              _pos('NVDA', 10, 700.0, 7000.0),
                              _pos('AMD', 10, 300.0, 3000.0)],
                   prices={'AAPL': 800.0, 'MSFT': 200.0, 'NVDA': 700.0,
                           'AMD': 300.0})
    root = _draw(nicegui_client, account_id, views)

    _press(_marked_buttons(root, page.MARKER_LOAD_CURRENT_SYMBOLS)[0])

    assert _stored(account_id, 'ARK26', ('AAPL', 'MSFT')) == {'AAPL': 80.0,
                                                              'MSFT': 20.0}
    assert get_symbol_rows(account_id, 'TECH') == {}


def test_load_current_REFUSES_a_label_whose_value_cannot_be_measured(
        monkeypatch, nicegui_client, account_id):
    """A price outage may not rewrite real targets."""
    sent = _capture_notifications(monkeypatch)
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    views = _views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL', 'DARK']},
                   weights={'ARK26': {'AAPL': 60.0, 'DARK': 40.0}},
                   positions=[_pos('AAPL', 10, 500.0, 5000.0),
                              _pos('DARK', 10, 100.0, None)],
                   prices={'AAPL': 500.0, 'DARK': None})
    root = _draw(nicegui_client, account_id, views)

    _press(_marked_buttons(root, page.MARKER_LOAD_CURRENT_SYMBOLS)[0])

    assert get_symbol_rows(account_id, 'ARK26') == {}
    assert any('no current shares to load' in m for m, _t in sent)


def test_load_current_joins_the_group_and_stays_on_the_harmless_side(nicegui_client,
                                                                     account_id):
    from nicegui import ui
    root = _draw(nicegui_client, account_id, _three_symbols(account_id))
    row = _fill_button(root).parent_slot.parent

    captions = [el._props.get('label', '<space>') if isinstance(el, ui.button)
                else '<space>' for el in row.descendants()
                if isinstance(el, (ui.button, ui.space))]
    assert captions == ['Fill 100%', 'Even split', 'Fill rest', 'Load last',
                        'Load current', 'Wipe', 'Compare', '<space>',
                        'Remove selected from label']


# ---------------------------------------------------------------------------
# THE TWO NEW BARS: the symbol-share total per label, and the reserve
#
# Both are the SAME component as the label header bar -- one track, one fill, one
# notch, one tolerance band, one over/under vocabulary. Both update as their
# numbers are edited, and neither recalculates anything.
#
# The per-label one is the SYMBOL shares summed within that label. The label's own
# share of the portfolio already has a bar, in the panel header directly above it;
# two bars, two denominators, and a caption on each says which.
# ---------------------------------------------------------------------------

def _bar_geometry(root, fill_marker, notch_marker, index=0):
    fill = _marked(root, fill_marker)[index]
    notch = _marked(root, notch_marker)[index]
    return (fill._style.get('width'), notch._style.get('left'))


def test_every_label_panel_gets_a_symbol_share_total_bar(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id,
                 _two_labelled_baskets(account_id,
                                       a_weights={'AAPL': 50.0, 'MSFT': 50.0},
                                       b_weights={'NVDA': 50.0, 'AMD': 50.0}))

    assert len(_marked(root, page.MARKER_SYMBOL_BAR_FILL)) == 2
    assert len(_marked(root, page.MARKER_SYMBOL_BAR_NOTCH)) == 2


def test_the_symbol_bar_measures_the_shares_WITHIN_its_own_label(nicegui_client,
                                                                 account_id):
    """66.77% of 100, not a share of the portfolio and not a sum across labels."""
    root = _draw(nicegui_client, account_id,
                 _views([ManagedLabel('ARK26', 50.0), ManagedLabel('TECH', 50.0)],
                        {'ARK26': ['AAPL', 'MSFT'], 'TECH': ['NVDA', 'AMD']},
                        weights={'ARK26': {'AAPL': 40.0, 'MSFT': 26.77},
                                 'TECH': {'NVDA': 50.0, 'AMD': 50.0}}))

    assert '66.8%' in _texts(_marked(root, page.MARKER_SYMBOL_BAR_ROW)[0])
    assert '100.0%' in _texts(_marked(root, page.MARKER_SYMBOL_BAR_ROW)[1])


def test_the_symbol_bar_says_under_when_the_shares_do_not_reach_100(nicegui_client,
                                                                    account_id):
    root = _draw(nicegui_client, account_id,
                 _views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL', 'MSFT']},
                        weights={'ARK26': {'AAPL': 40.0, 'MSFT': 35.0}}))

    assert 'under by 25.0pp' in _texts(_marked(root, page.MARKER_SYMBOL_BAR_ROW)[0])


def test_the_symbol_bar_FOLLOWS_an_inline_share_edit(nicegui_client, account_id):
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id,
                 _views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL', 'MSFT']},
                        weights={'ARK26': {'AAPL': 40.0, 'MSFT': 35.0}}))
    assert 'under by 25.0pp' in _texts(_marked(root, page.MARKER_SYMBOL_BAR_ROW)[0])

    _emit(_tables(root)[0], 'weightChange', ['AAPL', 65.0])

    assert 'on target' in _texts(_marked(root, page.MARKER_SYMBOL_BAR_ROW)[0])


def test_the_symbol_bar_follows_FILL_100_and_WIPE(monkeypatch, nicegui_client,
                                                  account_id):
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbols(account_id,
                                weights={'AAPL': 30.0, 'MSFT': 0.0, 'TSLA': 0.0}))

    _press(_fill_button(root))
    assert 'on target' in _texts(_marked(root, page.MARKER_SYMBOL_BAR_ROW)[0])

    _press(_marked_buttons(root, page.MARKER_WIPE_SYMBOLS)[0])
    assert 'under by 100.0pp' in _texts(_marked(root, page.MARKER_SYMBOL_BAR_ROW)[0])


def test_the_symbol_bar_of_ONE_label_does_not_move_when_ANOTHER_is_edited(
        monkeypatch, nicegui_client, account_id):
    """Summing across labels instead of within one is the mutation this kills."""
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _two_labelled_baskets(account_id,
                                       a_weights={'AAPL': 40.0, 'MSFT': 35.0},
                                       b_weights={'NVDA': 50.0, 'AMD': 50.0}))
    before = _texts(_marked(root, page.MARKER_SYMBOL_BAR_ROW)[1])

    _emit(_tables(root)[0], 'weightChange', ['AAPL', 65.0])

    assert _texts(_marked(root, page.MARKER_SYMBOL_BAR_ROW)[1]) == before


def test_an_unmeasurable_share_leaves_the_symbol_bar_without_a_verdict(
        nicegui_client, account_id):
    """A blank share is not a zero, so the total is unknown rather than short."""
    root = _draw(nicegui_client, account_id,
                 _views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL', 'DARK']},
                        positions=[_pos('AAPL', 10, 500.0, 5000.0),
                                   _pos('DARK', 10, 100.0, None)],
                        prices={'AAPL': 500.0, 'DARK': None}))
    texts = _texts(_marked(root, page.MARKER_SYMBOL_BAR_ROW)[0])

    assert '—' in texts
    assert not any('under by' in t for t in texts)


def test_the_unallocated_row_gets_a_bar_beside_its_sentence(nicegui_client,
                                                            account_id):
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=10.0),
                 base=5_260.9, buying_power=245.50, reserve=10.0)

    assert len(_marked(root, page.MARKER_RESERVE_BAR_FILL)) == 1
    # ...and every word of the row is still there.
    texts = _texts(root)
    assert any('Unallocated (free buying power)' in t for t in texts)
    assert _view_mod().RESERVE_BASIS_NOTE in texts


def test_the_allocation_bar_reads_OVER_when_the_cash_is_short_of_the_target(
        nicegui_client, account_id):
    """INVERTED: the bar's subject is what is ALLOCATED. $245.50 free of a
    $5,260.90 base is 95.3% invested against a 90% target -- over-allocated, which
    is the same fact as under-reserved and agrees with the warning beside it that
    closing the gap means selling."""
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=10.0),
                 base=5_260.9, buying_power=245.50, reserve=10.0)
    texts = _texts(_marked(root, page.MARKER_RESERVE_BAR_ROW)[0])

    assert any(t.startswith('over by ') for t in texts)
    assert not any(t.startswith('under by ') for t in texts)


def test_the_allocation_bar_FILLS_with_the_allocated_share(nicegui_client,
                                                           account_id):
    """5.3% reserve reads as a bar 94.7% full, not 5.3% full."""
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=10.0),
                 base=5_260.9, buying_power=279.25, reserve=10.0)

    width = float(_marked(root, page.MARKER_RESERVE_BAR_FILL)[0]
                  ._style['width'].removesuffix('%'))
    assert width == pytest.approx(94.69, abs=0.05)


def test_the_allocation_bars_TICK_is_inverted_with_it(nicegui_client, account_id):
    """A 10% reserve target sits at 90% along, or the tick lands on the wrong side
    of the fill and the bar contradicts its own caption."""
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=10.0),
                 base=5_260.9, buying_power=279.25, reserve=10.0)

    left = float(_marked(root, page.MARKER_RESERVE_BAR_NOTCH)[0]
                 ._style['left'].removesuffix('%'))
    assert left == pytest.approx(90.0, abs=0.05)


def test_the_allocation_bar_lives_INSIDE_the_reserve_card(nicegui_client,
                                                          account_id):
    """The amendment: fold it into the widget that already owns the concept rather
    than adding a panel. The blue callout is gone."""
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=10.0),
                 base=5_260.9, buying_power=279.25, reserve=10.0)
    card = _marked(root, page.MARKER_RESERVE_CARD)[0]

    assert _marked(root, page.MARKER_RESERVE_BAR_ROW)[0] in list(card.descendants())
    assert [el for el in root.descendants()
            if 'alert-banner info' in ' '.join(el._classes)] == []


def test_the_reserve_card_keeps_every_word_the_blue_panel_carried(nicegui_client,
                                                                  account_id):
    """The money line, the gross-base denominator and the sell warning. Losing any
    of the three is losing something load-bearing."""
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=10.0),
                 base=5_260.9, buying_power=279.25, reserve=10.0)
    texts = _texts(_marked(root, page.MARKER_RESERVE_CARD)[0])

    assert any('Unallocated (free buying power)' in t for t in texts)
    assert any('of base, target 10.00% of base' in t for t in texts)
    assert _view_mod().RESERVE_BASIS_NOTE in texts
    assert _view_mod().RESERVE_SELL_WARNING in texts
    assert _view_mod().ALLOCATION_BAR_LEGEND in texts


def test_the_reserve_bar_FOLLOWS_the_reserve_control(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=10.0),
                 base=5_260.9, buying_power=245.50, reserve=10.0)
    assert any(t.startswith('over by ')
               for t in _texts(_marked(root, page.MARKER_RESERVE_BAR_ROW)[0]))

    _drive_value(_reserve_controls(root)[0], 2.0)

    # A 2% reserve target is a 98% allocated target, and 95.3% invested is now
    # UNDER it -- the verdict flips with the control, in the inverted sense.
    texts = _texts(_marked(root, page.MARKER_RESERVE_BAR_ROW)[0])
    assert any(t.startswith('under by ') for t in texts)


def test_the_reserve_card_draws_an_EMPTY_bar_when_there_is_no_base(nicegui_client,
                                                                   account_id):
    """There is no denominator, so there is no verdict -- and no money line
    either. The card and its control still draw: the reserve is settable on an
    account the broker has not answered for yet."""
    root = _draw(nicegui_client, account_id, _one_label(account_id),
                 base=0.0, buying_power=None)

    card = _marked(root, page.MARKER_RESERVE_CARD)[0]
    assert card
    # The bar is drawn but never painted: no base, no fraction, no verdict.
    assert _marked(root, page.MARKER_RESERVE_BAR_FILL)[0]._style.get('width') is None
    # ...and the money line is EMPTY rather than a share of a number nobody has.
    assert not any('of base, target' in (el._text or '') for el in card.descendants())


def test_all_three_bars_share_one_track_style(nicegui_client, account_id):
    """One component, one visual language. Three tracks drawn three ways is how a
    page stops reading as one page."""
    root = _draw(nicegui_client, account_id, _one_label(account_id, reserve=10.0),
                 base=5_260.9, buying_power=245.50, reserve=10.0)

    tracks = [el.parent_slot.parent for marker in (page.MARKER_BAR_FILL,
                                                   page.MARKER_SYMBOL_BAR_FILL,
                                                   page.MARKER_RESERVE_BAR_FILL)
              for el in _marked(root, marker)]
    assert len(tracks) == 3
    assert {page.BAR_TRACK_STYLE.rstrip(';')} == {
        ';'.join(f'{k}:{v}' for k, v in t._style.items()) for t in tracks}


# ---------------------------------------------------------------------------
# THE MANAGE-LABELS DIALOG, after the user looked at it
# ---------------------------------------------------------------------------

def _picker(monkeypatch, nicegui_client, account_id, labels):
    _capture_notifications(monkeypatch)
    for name, color in labels.items():
        set_managed_label(account_id, name, target_pct=0.0, color=color or '')
        add_label_to_instruments(['AAPL'], name)
    with nicegui_client:
        page._open_label_picker(account_id, _noop_refresh)
    return nicegui_client.layout


def test_the_dialog_no_longer_repeats_the_colour_blindness_paragraph(
        monkeypatch, nicegui_client, account_id):
    """It was rendered once PER LABEL, so a dozen labels made the dialog mostly
    that paragraph. The rationale stays in the code, where it explains why the
    palette is what it is; the behaviour it described -- a hard-to-see colour is
    flagged, not refused -- is self-evident when the flag appears."""
    root = _picker(monkeypatch, nicegui_client, account_id,
                   {'BAST_TECH_CARS': '#E69F00', 'WHEEL_L1_HR': None})
    text = ' '.join(_texts(root))

    assert 'Okabe' not in text
    assert 'colour blindness' not in text
    assert 'flagged, not' not in text


def test_the_flagging_BEHAVIOUR_survives_the_deleted_copy(monkeypatch,
                                                          nicegui_client,
                                                          account_id):
    """The paragraph went; the warning it described did not."""
    set_managed_label(account_id, 'ARK26', target_pct=40.0)
    sent = _capture_notifications(monkeypatch)
    with nicegui_client:
        page._render_color_choices(account_id, 'ARK26', None, lambda *_a: None)
        _drive_value(_marked(nicegui_client.layout, page.MARKER_COLOR_CUSTOM)[0],
                     '#101010')

    assert get_managed_labels(account_id)[0].color == '#101010'
    assert any('contrast' in m.lower() and t == 'warning' for m, t in sent)


def test_every_label_block_in_the_dialog_starts_at_the_SAME_x(monkeypatch,
                                                              nicegui_client,
                                                              account_id):
    """The visible symptom: swatch rows indented differently between labels. A
    fixed name column is what puts them on one grid."""
    from nicegui import ui
    root = _picker(monkeypatch, nicegui_client, account_id,
                   {'BAST_TECH_ROBOT': '#E69F00', 'WHEEL_L1_HR': None,
                    'X': '#0072B2'})
    names = [el for el in root.descendants()
             if isinstance(el, ui.label) and el._text in ('BAST_TECH_ROBOT',
                                                          'WHEEL_L1_HR', 'X')]

    assert len(names) == 3
    widths = {(el._style or {}).get('width') for el in names}
    # An explicit width, and the SAME one. Before this the names carried none at
    # all -- which is also a set of size one, so the assertion has to say that a
    # width exists or it passes on the broken layout it was written for.
    assert len(widths) == 1, widths
    assert widths != {None}
    assert widths.pop().endswith('ch')


def test_the_name_column_fits_the_longest_managed_label(monkeypatch,
                                                        nicegui_client,
                                                        account_id):
    from nicegui import ui
    root = _picker(monkeypatch, nicegui_client, account_id,
                   {'BAST_TECH_ROBOT': None, 'X': None})
    name = next(el for el in root.descendants()
                if isinstance(el, ui.label) and el._text == 'BAST_TECH_ROBOT')

    width = int((name._style or {})['width'].removesuffix('ch'))
    assert width >= len('BAST_TECH_ROBOT')


def test_the_dialog_is_wide_enough_for_a_whole_label_block(monkeypatch,
                                                           nicegui_client,
                                                           account_id):
    """At 520px the block wrapped by a different amount per label, which is what
    made the swatches overlap the next label's row."""
    from nicegui import ui
    root = _picker(monkeypatch, nicegui_client, account_id,
                   {'BAST_TECH_ROBOT': None})
    card = next(el for el in root.descendants()
                if isinstance(el, ui.card) and any(
                    c.startswith('min-w-[') for c in el._classes))
    classes = ' '.join(card._classes)

    assert 'min-w-[520px]' not in classes
    assert 'max-w-[95vw]' in classes


def test_the_dialog_lays_the_swatches_and_the_custom_field_on_ONE_line(
        monkeypatch, nicegui_client, account_id):
    """Inline in the dialog, stacked in the label row's menu -- the same chooser,
    because a second one is how the two come to disagree."""
    from nicegui import ui
    root = _picker(monkeypatch, nicegui_client, account_id,
                   {'BAST_TECH_ROBOT': None})
    custom = _marked(root, page.MARKER_COLOR_CUSTOM)[0]

    assert isinstance(custom.parent_slot.parent, ui.row)
    assert 'shrink-0' in ' '.join(custom._classes)


def test_the_custom_field_still_shows_a_live_hex(monkeypatch, nicegui_client,
                                                 account_id):
    root = _picker(monkeypatch, nicegui_client, account_id,
                   {'BAST_TECH_ROBOT': '#F0E442'})
    custom = _marked(root, page.MARKER_COLOR_CUSTOM)[0]

    assert custom.value == '#F0E442'


# -- the tag icon, and the two states that share one grey --------------------

def test_the_label_rows_icon_and_bar_are_written_from_ONE_value(nicegui_client,
                                                                account_id):
    """Not two lookups of the same stored colour: one ``LabelBar.color``, painted
    onto both in the same loop. The reported symptom was a yellow bar beside a
    grey icon, with no way to tell which was right."""
    root = _draw(nicegui_client, account_id, _one_label(account_id, color='#F0E442'))

    icon_colour = (_row_icon(root)._style or {}).get('color', '')
    assert _bar_fill(root)._style['background'].upper() in icon_colour.upper()


def test_an_uncoloured_label_keeps_the_neutral_grey_on_BOTH(nicegui_client,
                                                            account_id):
    """"No colour chosen" is a real state, not a missing one."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    grey = _view_mod().DEFAULT_LABEL_ICON_COLOR

    assert grey.lower() in (_row_icon(root)._style or {}).get('color', '').lower()
    assert _bar_fill(root)._style['background'].upper() == grey.upper()


def test_the_icons_tooltip_separates_NO_COLOUR_from_an_unreadable_one(
        nicegui_client, account_id):
    """Both draw the same grey -- the parse decides what reaches a CSS ``style``
    attribute and that is not negotiable -- so the tooltip carries the difference
    the pixels cannot. Only one of the two is something the user can fix."""
    none_tip = _tooltip_texts(
        _draw(nicegui_client, account_id, _one_label(account_id)))
    assert any('No colour chosen' in t for t in none_tip)

    broken = _draw(nicegui_client, account_id,
                   _one_label(account_id, color='rgb(1,2,3)'))
    assert any('IGNORED' in t for t in _tooltip_texts(broken))


# ---------------------------------------------------------------------------
# THE ICON, THE P&L AND THE DELTA ARE PAINTED SO THE STYLESHEET CANNOT WIN
#
# Reported twice, and the second time with a screenshot: a cyan bar beside a grey
# tag icon on the same row. The page's Python state was correct throughout -- the
# icon carried ``style={'color': '#56B4E9'}`` -- which is why it looked right in
# source and in every harness test. The defect is in the CASCADE:
#
#     ui/static/styles.css:  .q-expansion-item .q-icon { color: #a0aec0 !important }
#
# A stylesheet ``!important`` beats a plain inline style, the label icon lives in
# the expansion HEADER, and #a0aec0 is exactly the grey that was reported. The
# bar's fill is a bare div painted with ``background``, which that rule does not
# touch -- hence one row, two answers.
#
# These tests read the real stylesheet, so they stay tied to the real cause: if
# the offending rule is ever removed the first one fails and says the workaround
# can go.
# ---------------------------------------------------------------------------

def _styles_css():
    from pathlib import Path
    return (Path(page.__file__).resolve().parents[1] / 'static' / 'styles.css'
            ).read_text(encoding='utf-8')


def test_the_stylesheet_really_does_grey_every_icon_inside_an_expansion():
    """Guards the guard. If this rule goes, the ``!important`` below is no longer
    needed and this test is where that gets noticed."""
    import re
    css = _styles_css()
    rule = re.search(r'\.q-expansion-item\s+\.q-icon\s*\{([^}]*)\}', css)

    assert rule is not None, 'the rule the icon fix exists for is gone'
    assert 'color' in rule.group(1) and '!important' in rule.group(1)


def test_the_label_icon_is_painted_with_an_inline_IMPORTANT(nicegui_client,
                                                            account_id):
    """The fix. Only an inline ``!important`` outranks a stylesheet one."""
    root = _draw(nicegui_client, account_id, _one_label(account_id, color='#56B4E9'))
    declared = (_row_icon(root)._style or {}).get('color', '')

    assert '#56B4E9' in declared
    assert '!important' in declared


def test_the_label_rows_icon_and_bar_are_written_from_ONE_value(nicegui_client,
                                                                account_id):
    """Not two lookups of the same stored colour: one ``LabelBar.color``, painted
    onto both in the same loop -- and now painted so both actually show it."""
    root = _draw(nicegui_client, account_id, _one_label(account_id, color='#F0E442'))

    icon_colour = (_row_icon(root)._style or {}).get('color', '')
    assert _bar_fill(root)._style['background'].upper() in icon_colour.upper()
    assert '!important' in icon_colour


def test_an_uncoloured_labels_icon_is_still_pinned_to_the_neutral_grey(
        nicegui_client, account_id):
    """It resolves to the same grey the stylesheet would have forced, but it says
    so itself -- so "no colour chosen" is a decision the page made, not one that
    happened to it."""
    root = _draw(nicegui_client, account_id, _one_label(account_id))
    grey = _view_mod().DEFAULT_LABEL_ICON_COLOR

    declared = (_row_icon(root)._style or {}).get('color', '')
    assert grey.lower() in declared.lower() and '!important' in declared


def _pnl_and_delta(root, index=0):
    row = _marked(root, page.MARKER_BAR_ROW)[index]
    pnl = _marked(row, page.MARKER_LABEL_PNL)[0]
    delta = next(el for el in row.descendants()
                 if el._text and (el._text.startswith(('over by', 'under by'))
                                  or el._text == 'on target'))
    return pnl, delta


def _two_verdicts(account_id):
    """One label far OVER its target and up on the day, one UNDER and down."""
    return _views([ManagedLabel('OVER', 5.0), ManagedLabel('UNDER', 90.0)],
                  {'OVER': ['WIN'], 'UNDER': ['LOSE']},
                  weights={'OVER': {'WIN': 100.0}, 'UNDER': {'LOSE': 100.0}},
                  positions=[_pos('WIN', 10, 1000.0, 2500.0),
                             _pos('LOSE', 10, 1000.0, 500.0)],
                  prices={'WIN': 250.0, 'LOSE': 50.0})


def test_a_positive_label_PNL_is_painted_GREEN(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _two_verdicts(account_id))
    pnl, _delta = _pnl_and_delta(root, 0)

    assert '+1,500.00' in pnl._text
    assert (pnl._style or {}).get('color', '') == _view_mod().important_color_style(
        _view_mod().PNL_POSITIVE_COLOR).removeprefix('color: ')


def test_a_negative_label_PNL_is_painted_RED(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _two_verdicts(account_id))
    pnl, _delta = _pnl_and_delta(root, 1)

    assert '-500.00' in pnl._text
    assert _view_mod().PNL_NEGATIVE_COLOR in (pnl._style or {}).get('color', '')


def test_a_flat_or_unmeasurable_label_PNL_stays_NEUTRAL(nicegui_client, account_id):
    """The epsilon band, kept exactly as it was. Break-even is not a verdict, and
    neither is "could not be measured"."""
    root = _draw(nicegui_client, account_id,
                 _views([ManagedLabel('FLAT', 100.0)], {'FLAT': ['AAPL']},
                        weights={'FLAT': {'AAPL': 100.0}},
                        positions=[_pos('AAPL', 10, 2500.0, 2500.0)],
                        prices={'AAPL': 250.0}))
    pnl, _delta = _pnl_and_delta(root)

    assert _view_mod().NEUTRAL_TEXT_COLOR in (pnl._style or {}).get('color', '')


def test_OVER_BY_is_painted_ORANGE(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _two_verdicts(account_id))
    _pnl, delta = _pnl_and_delta(root, 0)

    assert delta._text.startswith('over by ')
    assert _view_mod().STATUS_OVER_COLOR in (delta._style or {}).get('color', '')


def test_UNDER_BY_is_ORANGE_once_it_leaves_the_target_band(nicegui_client,
                                                           account_id):
    """It used to be neutral at any distance -- the user asked about "over" only,
    and that was reported rather than assumed. They then asked for the other half:
    a label 20 percentage points or more BELOW its target is a decision too, and
    the threshold is the bar's own (``within_target_band``) so the sentence and the
    track change colour together. This row is 85 points short."""
    root = _draw(nicegui_client, account_id, _two_verdicts(account_id))
    _pnl, delta = _pnl_and_delta(root, 1)

    assert delta._text.startswith('under by 85.0pp')
    assert _view_mod().STATUS_OVER_COLOR in (delta._style or {}).get('color', '')


def test_every_painted_verdict_carries_the_IMPORTANT(nicegui_client, account_id):
    """One mechanism for all three, or the next stylesheet rule picks them off one
    at a time."""
    root = _draw(nicegui_client, account_id, _two_verdicts(account_id))
    pnl, delta = _pnl_and_delta(root, 0)

    for element in (pnl, delta, _row_icon(root)):
        assert '!important' in (element._style or {}).get('color', ''), element


# ---------------------------------------------------------------------------
# THE GLOBAL "All labels" TOOLBAR IS GONE
#
# The user: "Event split / load last should be per label, we don't need to have
# this globally." What went with it is the ability to divide the pool evenly
# ACROSS labels -- an even split of ONE label's target is not a meaningful
# operation, so there is nothing per-label to replace it with. Flagged, not
# quietly reinstated.
# ---------------------------------------------------------------------------

def test_the_page_draws_NO_global_even_split_or_load_last_row(nicegui_client,
                                                              account_id):
    root = _draw(nicegui_client, account_id, _two_labels(account_id))

    for gone in ('MARKER_LABEL_TOOLS', 'MARKER_EVEN_SPLIT_LABELS',
                 'MARKER_LOAD_LAST_LABELS'):
        assert not hasattr(page, gone), gone
    assert 'All labels' not in _texts(root)


def test_the_page_carries_no_label_level_target_writer_at_all(nicegui_client,
                                                              account_id):
    """Structural: a writer nobody calls is one somebody will call."""
    for gone in ('_render_label_tools', '_even_split_labels', '_load_last_labels',
                 '_run_label_target_button', '_write_label_targets',
                 '_apply_label_targets'):
        assert not hasattr(page, gone), gone


def test_even_split_and_load_last_survive_PER_LABEL(nicegui_client, account_id):
    """One of each per label, and nothing global."""
    root = _draw(nicegui_client, account_id,
                 _two_labelled_baskets(account_id,
                                       a_weights={'AAPL': 50.0, 'MSFT': 50.0},
                                       b_weights={'NVDA': 50.0, 'AMD': 50.0}))

    assert len(_marked_buttons(root, page.MARKER_EVEN_SPLIT_SYMBOLS)) == 2
    assert len(_marked_buttons(root, page.MARKER_LOAD_LAST_SYMBOLS)) == 2
    assert len(_marked_buttons(root, page.MARKER_LOAD_CURRENT_SYMBOLS)) == 2


def test_per_label_LOAD_LAST_restores_the_last_SAVED_shares(monkeypatch,
                                                            nicegui_client,
                                                            account_id):
    """"they should load last saved". ``previous_weight_pct`` is written by
    ``save_allocation_targets`` and by nothing else, so that is what this reads --
    not the values currently in the boxes."""
    _capture_notifications(monkeypatch)
    root = _draw(nicegui_client, account_id,
                 _three_symbols(account_id,
                                weights={'AAPL': 90.0, 'MSFT': 5.0, 'TSLA': 5.0},
                                previous={'AAPL': 20.0, 'MSFT': 30.0,
                                          'TSLA': 50.0}))

    _press(_marked_buttons(root, page.MARKER_LOAD_LAST_SYMBOLS)[0])

    assert _stored(account_id) == {'AAPL': 20.0, 'MSFT': 30.0, 'TSLA': 50.0}


def test_per_label_LOAD_CURRENT_loads_the_brokers_actual_allocation(
        monkeypatch, nicegui_client, account_id):
    """The other half of the pair, confirmed live: what is actually held now."""
    _capture_notifications(monkeypatch)
    set_managed_label(account_id, 'ARK26', target_pct=100.0)
    root = _draw(nicegui_client, account_id,
                 _views([ManagedLabel('ARK26', 100.0)],
                        {'ARK26': ['AAPL', 'MSFT', 'TSLA']},
                        weights={'ARK26': {'AAPL': 33.33, 'MSFT': 33.33,
                                           'TSLA': 33.34}},
                        positions=[_pos('AAPL', 10, 500.0, 5000.0),
                                   _pos('MSFT', 10, 300.0, 3000.0),
                                   _pos('TSLA', 10, 200.0, 2000.0)],
                        prices={'AAPL': 500.0, 'MSFT': 300.0, 'TSLA': 200.0}))

    _press(_marked_buttons(root, page.MARKER_LOAD_CURRENT_SYMBOLS)[0])

    assert _stored(account_id) == {'AAPL': 50.0, 'MSFT': 30.0, 'TSLA': 20.0}


def test_the_managed_labels_bar_says_what_it_IS(nicegui_client, account_id):
    """The card is titled "Managed labels" and shows a COUNT. A bar under it with
    no name is a rectangle the reader has to guess at."""
    root = _draw(nicegui_client, account_id, _two_labels(account_id))
    texts = _total_card_texts(root)

    assert _view_mod().LABEL_TOTAL_BAR_LEGEND in texts
    # ...and the denominator caption is still doing its own, different job.
    assert _view_mod().LABEL_TOTAL_BAR_CAPTION in texts


# ---------------------------------------------------------------------------
# BOTH BARS ARE COLOURED BY ONE BAND RULE
#
# "above target, or more than 20 points below it" -> yellow, otherwise green. The
# label-total bar's "green 80 to 100" is that rule against a target of 100.
# Verified in the RENDERED style rather than by trusting a class name -- the
# stylesheet has eaten class-based colour on this page before.
# ---------------------------------------------------------------------------

def _fill_colour(root, marker, index=0):
    return _marked(root, marker)[index]._style.get('background', '')


def _totals(account_id, a, b):
    for name, target in (('ARK26', a), ('TECH', b)):
        set_managed_label(account_id, name, target_pct=target)
    return _views([ManagedLabel('ARK26', a), ManagedLabel('TECH', b)],
                  {'ARK26': ['AAPL'], 'TECH': ['MSFT']},
                  weights={'ARK26': {'AAPL': 100.0}, 'TECH': {'MSFT': 100.0}})


def test_the_total_bar_is_GREEN_exactly_on_100(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _totals(account_id, 70.0, 30.0))
    assert _fill_colour(root, page.MARKER_TOTAL_BAR_FILL) == \
        _view_mod().BAND_OK_COLOR


def test_the_total_bar_is_GREEN_exactly_on_80(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _totals(account_id, 50.0, 30.0))
    assert _fill_colour(root, page.MARKER_TOTAL_BAR_FILL) == \
        _view_mod().BAND_OK_COLOR


def test_the_total_bar_is_YELLOW_a_hundredth_below_80(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _totals(account_id, 49.99, 30.0))
    assert _fill_colour(root, page.MARKER_TOTAL_BAR_FILL) == \
        _view_mod().BAND_OFF_COLOR


def test_the_total_bar_is_YELLOW_a_hundredth_above_100(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _totals(account_id, 70.01, 30.0))
    assert _fill_colour(root, page.MARKER_TOTAL_BAR_FILL) == \
        _view_mod().BAND_OFF_COLOR


def test_the_total_bars_COLOUR_and_its_TEXT_never_disagree(nicegui_client,
                                                           account_id):
    """A green bar beside orange text is worse than no colour at all."""
    green = _draw(nicegui_client, account_id, _totals(account_id, 55.0, 30.0))
    assert _fill_colour(green, page.MARKER_TOTAL_BAR_FILL) == \
        _view_mod().BAND_OK_COLOR
    assert 'text-orange-400' not in ' '.join(
        _marked(green, page.MARKER_TOTAL_BAR_DETAIL)[0]._classes)

    yellow = _draw(nicegui_client, account_id, _totals(account_id, 40.0, 30.0))
    assert _fill_colour(yellow, page.MARKER_TOTAL_BAR_FILL, -1) == \
        _view_mod().BAND_OFF_COLOR
    assert 'text-orange-400' in ' '.join(
        _marked(yellow, page.MARKER_TOTAL_BAR_DETAIL)[-1]._classes)


def _reserve_page(nicegui_client, account_id, *, buying_power, reserve):
    return _draw(nicegui_client, account_id, _one_label(account_id, reserve=reserve),
                 base=1_000.0, buying_power=buying_power, reserve=reserve)


def test_the_allocation_bar_is_GREEN_exactly_on_its_target(nicegui_client,
                                                           account_id):
    """A 10% reserve is a 90% allocated target; $100 free of $1,000 is exactly 90%
    allocated."""
    root = _reserve_page(nicegui_client, account_id, buying_power=100.0, reserve=10.0)
    assert _fill_colour(root, page.MARKER_RESERVE_BAR_FILL) == \
        _view_mod().BAND_OK_COLOR


def test_the_allocation_bar_is_YELLOW_a_hundredth_ABOVE_target(nicegui_client,
                                                               account_id):
    root = _reserve_page(nicegui_client, account_id, buying_power=99.9, reserve=10.0)
    assert _fill_colour(root, page.MARKER_RESERVE_BAR_FILL) == \
        _view_mod().BAND_OFF_COLOR


def test_the_allocation_bar_is_GREEN_exactly_twenty_points_below_target(
        nicegui_client, account_id):
    """Target 90% allocated, slack 20pp, so 70% allocated -- $300 of $1,000 free."""
    root = _reserve_page(nicegui_client, account_id, buying_power=300.0, reserve=10.0)
    assert _fill_colour(root, page.MARKER_RESERVE_BAR_FILL) == \
        _view_mod().BAND_OK_COLOR


def test_the_allocation_bar_is_YELLOW_a_hundredth_below_THAT(nicegui_client,
                                                             account_id):
    root = _reserve_page(nicegui_client, account_id, buying_power=300.1, reserve=10.0)
    assert _fill_colour(root, page.MARKER_RESERVE_BAR_FILL) == \
        _view_mod().BAND_OFF_COLOR


def test_the_allocation_bar_RE_COLOURS_when_the_reserve_moves(nicegui_client,
                                                              account_id):
    """The target moves with the slider, so the band moves with it."""
    root = _reserve_page(nicegui_client, account_id, buying_power=100.0, reserve=10.0)
    assert _fill_colour(root, page.MARKER_RESERVE_BAR_FILL) == \
        _view_mod().BAND_OK_COLOR

    _drive_value(_reserve_controls(root)[0], 40.0)

    # 90% allocated against a 60% target is above it -- out of the band.
    assert _fill_colour(root, page.MARKER_RESERVE_BAR_FILL) == \
        _view_mod().BAND_OFF_COLOR


def test_an_unmeasurable_bar_is_NEUTRAL_rather_than_yellow(nicegui_client,
                                                           account_id):
    """No verdict is not a warning.

    The broker published no buying power, so the reserve bar has NOTHING to divide
    -- ``allocation_bar`` returns None and the fill is never painted. What matters
    is the colour it is not: yellow there would be reporting a problem the user
    does not have, on an account nobody has measured."""
    root = _draw(nicegui_client, account_id, _one_label(account_id),
                 base=10_000.0, buying_power=None)
    fill = _marked(root, page.MARKER_RESERVE_BAR_FILL)[0]._style.get('background', '')

    assert fill != _view_mod().BAND_OFF_COLOR
    assert fill != _view_mod().BAND_OK_COLOR


# ---------------------------------------------------------------------------
# THE MERGED MONEY CARD, ON THE PAGE
#
# One box for managed value, account value and free buying power. The last of the
# three used to be drawn inside ``if buying_power is not None`` and DISAPPEARED on
# a broker outage -- unknown-as-zero in the form a user cannot notice, because
# there is nothing on screen to notice. The card is unconditional now and so is
# every line in it.
# ---------------------------------------------------------------------------

def _money_card(root):
    return _marked(root, page.MARKER_MONEY_CARD)[0]


def _money_figures(root):
    """The three figure texts, in reading order."""
    return [el._text for el in _marked(root, page.MARKER_MONEY_FIGURE)]


def test_the_three_money_figures_share_ONE_card(nicegui_client, account_id):
    """Three boxes each sized to its own caption put the three figures a reader
    compares left to right at three different heights."""
    root = _draw(nicegui_client, account_id, _one_label(account_id),
                 buying_power=1_000.0)

    cards = _marked(root, page.MARKER_MONEY_CARD)
    assert len(cards) == 1
    texts = _texts(cards[0])
    assert 'Managed value — market value (qty x price)' in texts
    assert 'Account value' in texts
    assert 'Free buying power' in texts


def test_the_money_card_renders_with_a_REAL_buying_power(nicegui_client, account_id):
    root = _draw(nicegui_client, account_id, _one_label(account_id),
                 buying_power=1_234.56)

    assert '$1,234.56' in _money_figures(root)


def test_the_money_card_renders_a_MEASURED_ZERO_buying_power(nicegui_client,
                                                             account_id):
    """Every dollar deployed is a real state, and it is exactly why the next plan
    will buy nothing. It is a figure, not an outage."""
    root = _draw(nicegui_client, account_id, _one_label(account_id),
                 buying_power=0.0)
    texts = _texts(_money_card(root))
    index = texts.index('Free buying power')

    assert texts[index + 1] == '$0.00'
    assert _view_mod().BUYING_POWER_UNAVAILABLE_DETAIL not in texts


def test_the_money_card_STILL_RENDERS_when_the_broker_will_not_answer(
        nicegui_client, account_id):
    """THE folded-in defect. The buying-power card used to vanish here, and a user
    who cannot see the widget cannot tell the figure is missing."""
    root = _draw(nicegui_client, account_id, _one_label(account_id),
                 buying_power=None)
    texts = _texts(_money_card(root))
    index = texts.index('Free buying power')

    assert texts[index + 1] == _view_mod().FIGURE_UNKNOWN_TEXT == 'unknown'
    assert texts[index + 2] == _view_mod().BUYING_POWER_UNAVAILABLE_DETAIL
    assert '$0.00' not in texts[index + 1]


def test_an_UNREADABLE_buying_power_does_not_blank_its_two_NEIGHBOURS(
        nicegui_client, account_id):
    """The property the merge had to preserve: three figures in one box, three
    independent states. A shared box must not become a shared failure."""
    views = _views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                   positions=[_pos('AAPL', 10, 1000.0, 4_853.48)],
                   prices={'AAPL': 485.348})
    with nicegui_client:
        page._render_labels(account_id,
                            _payload(views, VALUATION_MODE_MARKET,
                                     base_notional=10_000.0,
                                     available_buying_power=None,
                                     account_value=2_511.90),
                            _noop_refresh)
    figures = _money_figures(nicegui_client.layout)

    assert figures == ['$4,853.48', '$2,511.90', 'unknown']


def test_an_UNKNOWN_figure_is_painted_NEUTRAL_and_not_as_a_number(
        nicegui_client, account_id):
    """Verified in the rendered STYLE, and with ``!important``: this page's own
    stylesheet has silently eaten class-based colour before, so a colour that is
    merely set is not a colour that paints."""
    root = _draw(nicegui_client, account_id, _one_label(account_id),
                 buying_power=None)
    figures = _marked(root, page.MARKER_MONEY_FIGURE)
    unknown = [el for el in figures if el._text == 'unknown']
    measured = [el for el in figures if el._text != 'unknown']

    # This fixture publishes neither an account value nor a buying power, so BOTH
    # are unknown -- and the managed value beside them is not.
    assert len(unknown) == 2 and len(measured) == 1
    for element in unknown:
        assert element._style['color'] == \
            f'{_view_mod().NEUTRAL_TEXT_COLOR} !important'
    # ...and a MEASURED figure keeps the money styling: the grey is the signal, so
    # painting it on everything would be no signal at all.
    assert 'color' not in measured[0]._style


def test_the_managed_value_caption_still_names_WHICH_valuation(nicegui_client,
                                                               account_id):
    """The parenthetical is a definition, not decoration -- the Valuation selector
    sits two inches away and changes what the figure means."""
    views = _views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']})
    with nicegui_client:
        page._render_labels(account_id, _payload(views, VALUATION_MODE_COST),
                            _noop_refresh)

    assert 'Managed value — cost basis (what you paid)' in \
        _texts(_money_card(nicegui_client.layout))


def test_the_leverage_line_still_sits_under_the_account_value(nicegui_client,
                                                              account_id):
    """"managed positions are 2.02x this" is what tells the user they are levered,
    and "this" only means anything directly beneath the figure it divides by."""
    views = _views([ManagedLabel('ARK26', 40.0)], {'ARK26': ['AAPL']},
                   positions=[_pos('AAPL', 10, 1000.0, 5_073.71)],
                   prices={'AAPL': 507.371})
    with nicegui_client:
        page._render_labels(account_id,
                            _payload(views, VALUATION_MODE_MARKET,
                                     account_value=2_511.90),
                            _noop_refresh)
    texts = _texts(_money_card(nicegui_client.layout))
    index = texts.index('Account value')

    assert texts[index + 1] == '$2,511.90'
    assert texts[index + 2] == 'managed positions are 2.02x this'
