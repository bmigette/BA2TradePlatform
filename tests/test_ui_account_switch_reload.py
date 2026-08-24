"""The header account selector must actually re-render the page it filters.

Reported 2026-08: the header read "Alcapa Live (Alpaca)" while the body still
showed Portfolio Allocation's ``GATE_NO_ACCOUNT`` banner -- which is only produced
for ``account_id is None`` -- and, on a TastyTrade account that owns ZERO experts,
the body named the two ENABLED experts of the *Alpaca* account. Both are the same
defect: the body was drawn for the PREVIOUS selection and never redrawn.

``ui/pages/portfolio_allocation.py:content()`` reads ``get_selected_account_id()``
exactly once, at page build, so the page depends entirely on being re-run. The
re-run is ``ui.run_javascript('window.location.reload()')`` in
``ui/layout.py:on_account_change``, deliberately NOT awaited: awaiting it would
wait for a reply from a document that is in the middle of tearing itself down.

In NiceGUI 3.8 that is exactly right. ``ui.run_javascript`` returns an
``AwaitableResponse`` whose ``__init__`` already schedules the send
(``venv/lib/python3.12/site-packages/nicegui/awaitable_response.py:21`` ->
``_fire`` -> ``fire_and_forget``), so an un-awaited call still reaches the browser
(``nicegui/client.py:245-253``).

What broke it was local: ``ui/main.py`` replaced ``Client.run_javascript`` with an
``async def`` wrapper to force a 5s JavaScript timeout. That turns every
``ui.run_javascript(...)`` return value into a bare coroutine. No coroutine, no
``AwaitableResponse``, no ``_fire`` task -- the un-awaited call enqueues NOTHING and
Python only mutters "coroutine ... was never awaited" on the console. The timeout
the wrapper existed to raise never applied anyway: ``ui.run_javascript`` passes
``timeout=`` explicitly on every call, so the wrapper's own default was dead.

These tests pin both halves: the NiceGUI contract we rely on, and the rule that no
UI module may wrap ``Client.run_javascript`` in a coroutine function again.
"""
import ast
import asyncio
from pathlib import Path

import pytest

UI_ROOT = Path(__file__).resolve().parents[1] / 'ba2_trade_platform' / 'ui'


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run(coro_fn):
    """Run ``coro_fn()`` with ``nicegui.core.loop`` pointing at the live loop.

    ``AwaitableResponse.__init__`` asserts on ``core.loop``; outside ``ui.run()``
    nothing sets it.
    """
    from nicegui import core

    async def _main():
        previous = core.loop
        core.loop = asyncio.get_running_loop()
        try:
            return await coro_fn()
        finally:
            core.loop = previous

    return asyncio.run(_main())


def _javascript_payloads(client):
    """Every ``run_javascript`` message sitting in the client's outbox."""
    return [data for _target, kind, data in list(client.outbox.messages)
            if kind == 'run_javascript']


def _javascript_messages(client):
    """Just the code of every ``run_javascript`` message in the outbox."""
    return [data['code'] for data in _javascript_payloads(client)]


def _assert_reloaded_fire_and_forget(client):
    """The reload was sent, and sent WITHOUT asking the browser to answer.

    ``request_id`` in the payload means the caller awaited the response
    (``nicegui/client.py:248-251``). The reloading document never answers, so an
    awaited reload blocks ``on_account_change`` until the timeout and then raises.
    """
    payloads = _javascript_payloads(client)
    assert [p['code'] for p in payloads] == ['window.location.reload()']
    assert 'request_id' not in payloads[0], 'the reload must be fire-and-forget'


@pytest.fixture
def nicegui_client():
    """A slot stack plus a real outbox, so sends can be observed without a browser."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-account-switch-reload'), request=None)
    yield client
    client.remove_elements(client.elements.values())


class _DictStorage:
    def __init__(self, data):
        self._data = data

    @property
    def user(self):
        return self._data


class _FakeApp:
    def __init__(self, storage):
        self.storage = storage


# ---------------------------------------------------------------------------
# the NiceGUI 3.8 contract layout.py relies on
# ---------------------------------------------------------------------------

def test_an_unawaited_run_javascript_still_reaches_the_browser(nicegui_client):
    """Stock NiceGUI 3.8: fire-and-forget is the DEFAULT, not an opt-in.

    A dependency bump that made ``ui.run_javascript`` inert unless awaited would
    silently kill the account switch again, so the contract is pinned here rather
    than assumed.
    """
    from nicegui import ui

    async def _scenario():
        with nicegui_client:
            ui.run_javascript('window.location.reload()')
        await asyncio.sleep(0)  # let AwaitableResponse's 'fire' task run

    _run(_scenario)
    assert _javascript_messages(nicegui_client) == ['window.location.reload()']


def test_wrapping_client_run_javascript_in_a_coroutine_swallows_the_send(nicegui_client):
    """The exact mechanism of the 2026-08 bug, reproduced.

    This is what ``ui/main.py`` used to do. It is kept as a test so the invariant
    below reads as a rule with a reason attached rather than a style preference.
    """
    from nicegui import ui
    from nicegui.client import Client

    original = Client.run_javascript

    async def wrapper(self, code, *, timeout=5.0):          # what main.py installed
        return await original(self, code, timeout=timeout)

    async def _scenario():
        Client.run_javascript = wrapper
        try:
            with nicegui_client:
                ui.run_javascript('window.location.reload()')
            await asyncio.sleep(0)
        finally:
            Client.run_javascript = original

    with pytest.warns(RuntimeWarning, match='never awaited'):
        _run(_scenario)
    assert _javascript_messages(nicegui_client) == []


def test_no_ui_module_wraps_client_run_javascript_in_a_coroutine():
    """No module under ``ba2_trade_platform/ui`` may make ``run_javascript`` async.

    ``ui/main.py`` patched it to force a JavaScript timeout that every call site
    already passes explicitly, and the only thing the patch actually achieved was
    breaking every un-awaited call: the header account switch
    (``ui/layout.py``) and TradingAgentsUI's post-save reload.

    A SYNCHRONOUS wrapper that returns the ``AwaitableResponse`` unchanged is still
    allowed -- it preserves both halves of the contract.
    """
    offenders = []
    for path in sorted(UI_ROOT.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        async_defs = {node.name for node in ast.walk(tree)
                      if isinstance(node, ast.AsyncFunctionDef)}
        for node in ast.walk(tree):
            # Client.run_javascript = <name>
            if isinstance(node, ast.Assign):
                assigned = [t for t in node.targets
                            if isinstance(t, ast.Attribute) and t.attr == 'run_javascript']
                if assigned and isinstance(node.value, ast.Name) and node.value.id in async_defs:
                    offenders.append(f'{path}:{node.lineno} -> async def {node.value.id}')
                if assigned and isinstance(node.value, (ast.Lambda,)):
                    continue
            # setattr(Client, 'run_javascript', <name>)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'setattr' and len(node.args) == 3
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == 'run_javascript'
                    and isinstance(node.args[2], ast.Name)
                    and node.args[2].id in async_defs):
                offenders.append(f'{path}:{node.lineno} -> setattr async def {node.args[2].id}')

    assert offenders == [], (
        'Client.run_javascript wrapped in a coroutine function; un-awaited '
        'ui.run_javascript() calls will silently do nothing:\n  ' + '\n  '.join(offenders))


# ---------------------------------------------------------------------------
# the header selector itself
# ---------------------------------------------------------------------------

def _render_selector(monkeypatch, nicegui_client, accounts, stored):
    """Draw the real header dropdown against a fake account list and storage."""
    import ba2_trade_platform.ui.account_filter_context as afc
    import ba2_trade_platform.ui.layout as layout

    monkeypatch.setattr(afc, 'app', _FakeApp(_DictStorage(stored)))
    monkeypatch.setattr(layout, 'get_accounts_for_filter', lambda: accounts)
    afc._last_known_account_id = None

    from nicegui import ui

    async def _scenario():
        with nicegui_client:
            layout._render_account_filter_dropdown()
            select = next(el for el in nicegui_client.layout.descendants()
                          if isinstance(el, ui.select))
        return select

    return _scenario


def test_switching_the_account_persists_the_choice_and_reloads_the_browser(
        monkeypatch, nicegui_client):
    """The whole contract of the header selector, end to end.

    Persisting without reloading is precisely the reported bug: the widget shows
    the new account, every page body still shows the old one.
    """
    import ba2_trade_platform.ui.account_filter_context as afc

    stored = {}
    accounts = [('All', None), ('Alcapa Live (Alpaca)', 1), ('Tasty (TastyTrade)', 2)]
    scenario = _render_selector(monkeypatch, nicegui_client, accounts, stored)

    async def _switch():
        select = await scenario()
        select.value = 2                      # user picks TastyTrade
        await asyncio.sleep(0.05)             # handler task, then the 'fire' task

    _run(_switch)

    assert stored[afc.ACCOUNT_FILTER_KEY] == 2
    _assert_reloaded_fire_and_forget(nicegui_client)


def test_switching_to_all_accounts_also_reloads(monkeypatch, nicegui_client):
    """'All' is the sentinel ``"all"`` in the widget and ``None`` in storage."""
    import ba2_trade_platform.ui.account_filter_context as afc

    stored = {afc.ACCOUNT_FILTER_KEY: 1}
    accounts = [('All', None), ('Alcapa Live (Alpaca)', 1), ('Tasty (TastyTrade)', 2)]
    scenario = _render_selector(monkeypatch, nicegui_client, accounts, stored)

    async def _switch():
        select = await scenario()
        select.value = 'all'
        await asyncio.sleep(0.05)

    _run(_switch)

    assert stored[afc.ACCOUNT_FILTER_KEY] is None
    _assert_reloaded_fire_and_forget(nicegui_client)


def test_the_selector_opens_on_the_account_that_is_actually_stored(
        monkeypatch, nicegui_client):
    """The widget must not disagree with the value the pages will read."""
    import ba2_trade_platform.ui.account_filter_context as afc

    stored = {afc.ACCOUNT_FILTER_KEY: 2}
    accounts = [('All', None), ('Alcapa Live (Alpaca)', 1), ('Tasty (TastyTrade)', 2)]
    scenario = _render_selector(monkeypatch, nicegui_client, accounts, stored)

    select = _run(scenario)
    assert select.value == 2
    assert afc.get_selected_account_id() == 2
