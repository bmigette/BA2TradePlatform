"""The shared header must survive a selected account that no longer exists.

THE BUG. ``_render_account_filter_dropdown`` builds its options from the accounts
that currently exist, and hands ``ui.select`` whatever id is in
``app.storage.user`` (or, outside a UI context, the process-global
``_last_known_account_id`` mirror, which nothing clears). Delete the selected
account and the stored id outlives it: it is no longer one of the options, so
NiceGUI raises ``ValueError: Invalid value: 2``. That happens in the header shared
by EVERY route, BEFORE the page body is drawn — so one deleted account 500s the
whole application, on every page, until storage is cleared by hand.

WHAT THE FIX MUST NOT DO is throw away a selection that is merely *unknown to this
render*. Two ways that could happen, both tested here:

* the accounts listing is cached for 60 seconds, so an account created moments ago
  is legitimately absent from the cached options while being a perfectly valid
  choice — the listing is re-read before an id is declared dead;
* the database read itself can fail, and a failed read is not evidence of a
  deletion — the header degrades to "All" for that render but must NOT persist the
  correction.

TIME IS FROZEN and pinned far from the system clock (2019-03-04). The 60-second
TTL above is a real branch in these tests; letting the wall clock run would make
"is the listing still warm?" a race. (There is no freezegun in this venv.)

Never ``caplog``: ``logger.py`` sets ``propagate = False``.
"""
import pytest
from nicegui import ui

import ba2_trade_platform.ui.account_filter_context as afc
from ba2_trade_platform.core.db import delete_instance
from ba2_trade_platform.ui import layout
from tests.factories import create_account_definition


#: 2019-03-04 10:00:00 UTC. Years from today on purpose.
FROZEN_EPOCH = 1551693600.0


class _FrozenClock:
    """Stand-in for the ``time`` module inside ``account_filter_context``."""

    def __init__(self, now=FROZEN_EPOCH):
        self.now = now

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class _DictStorage:
    """``app.storage`` stand-in whose ``.user`` is a plain dict (a UI context)."""

    def __init__(self, d):
        self._d = d

    @property
    def user(self):
        return self._d


class _FakeApp:
    def __init__(self, storage):
        self.storage = storage


@pytest.fixture
def storage(monkeypatch):
    """A per-session storage dict the header really reads and writes."""
    store = {}
    monkeypatch.setattr(afc, 'app', _FakeApp(_DictStorage(store)))
    afc._last_known_account_id = None
    yield store
    afc._last_known_account_id = None


@pytest.fixture
def clock(monkeypatch):
    frozen = _FrozenClock()
    monkeypatch.setattr(afc, 'time', frozen)
    return frozen


@pytest.fixture(autouse=True)
def _cold_listing():
    """Start every test with no cached accounts listing, and leave none behind."""
    _cool_the_listing()
    yield
    _cool_the_listing()


def _new_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    return Client(nicegui_page('/test-header-account-filter'), request=None)


@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw."""
    client = _new_client()
    yield client
    client.remove_elements(client.elements.values())


@pytest.fixture
def second_client():
    """A SECOND page load. One client per render keeps "which select is this?"
    from being a question — two renders into one client leave two of them."""
    client = _new_client()
    yield client
    client.remove_elements(client.elements.values())


def _cool_the_listing():
    afc._accounts_cache['data'] = None
    afc._accounts_cache['timestamp'] = 0


def _warm_the_listing():
    """Populate the 60-second accounts cache from the database as it is now."""
    _cool_the_listing()
    return afc.get_accounts_for_filter()


def _texts(root):
    return [el._text for el in root.descendants(include_self=True) if el._text]


def _render_a_page(client):
    """Draw the frame EVERY route is wrapped in, plus a page body."""
    with client:
        with layout.layout_render('Overview'):
            ui.label('page body')


def _account_select(client):
    return next(el for el in client.layout.descendants()
                if isinstance(el, ui.select))


def _capture_warnings(monkeypatch):
    """Collect ``logger.warning`` messages from the layout module. NOT caplog."""
    messages = []
    monkeypatch.setattr(layout.logger, 'warning',
                        lambda msg, *a, **k: messages.append(str(msg)))
    return messages


# ---------------------------------------------------------------------------
# The headline: the account is deleted while it is selected.
# ---------------------------------------------------------------------------

def test_an_account_deleted_while_selected_does_not_take_down_every_page(
        nicegui_client, storage, clock):
    """The reported crash, end to end.

    The user picks Tasty, deletes Tasty in Settings, and navigates. The stored id
    is now a dangling reference in a header that every single route renders.
    """
    create_account_definition(name='Alpaca Live', provider='MockAccount')
    doomed = create_account_definition(name='Tasty', provider='MockAccount')
    afc.set_selected_account_id(doomed.id)
    _warm_the_listing()                      # the user saw it in the dropdown

    delete_instance(doomed)                  # ... and then deleted it
    clock.advance(120)                       # the 60s listing cache has aged out

    _render_a_page(nicegui_client)           # must not raise

    select = _account_select(nicegui_client)
    assert select.value == 'all'
    assert select.options[select.value] == 'All'
    assert 'page body' in _texts(nicegui_client.layout)
    # ... and the dropdown does not still OFFER the account that is gone: the
    # listing that proved it dead is the one the widget is built from, so the user
    # cannot re-pick it and land straight back in the crash.
    assert doomed.id not in select.options


def test_every_page_keeps_rendering_on_the_renders_that_follow(
        nicegui_client, storage, clock):
    """Once is not enough: the correction has to STICK.

    A header that coerces the value for the widget but leaves the dead id in
    storage crashes again on the very next navigation — the user experience is
    identical to no fix at all.
    """
    doomed = create_account_definition(name='Tasty', provider='MockAccount')
    afc.set_selected_account_id(doomed.id)
    delete_instance(doomed)

    for _ in range(3):
        _cool_the_listing()
        _render_a_page(nicegui_client)
        assert _account_select(nicegui_client).value == 'all'


def test_the_dead_selection_is_cleared_from_storage_and_from_the_mirror(
        nicegui_client, storage, clock):
    """Both writers, through the one setter.

    ``app.storage.user`` is what the next page build reads; the process-global
    mirror is what threaded widgets read when they cannot reach storage. Clearing
    only one of them leaves the other resurrecting the dead id — the mirror in
    particular is never cleared by anything else in the process.
    """
    doomed = create_account_definition(name='Tasty', provider='MockAccount')
    afc.set_selected_account_id(doomed.id)
    assert storage[afc.account_filter_storage_key()] == doomed.id      # precondition
    delete_instance(doomed)

    _render_a_page(nicegui_client)

    assert storage[afc.account_filter_storage_key()] is None
    assert afc._last_known_account_id is None
    assert afc.get_selected_account_id() is None


def test_switching_a_users_account_behind_their_back_is_logged_at_warning(
        nicegui_client, storage, clock, monkeypatch):
    """Silently changing someone's account filter must leave a trace.

    If it starts happening repeatedly — an id that keeps coming back, a listing
    that keeps under-reporting — the log is the only place anyone would see it.
    The message has to name the id, or it says nothing useful.
    """
    doomed = create_account_definition(name='Tasty', provider='MockAccount')
    afc.set_selected_account_id(doomed.id)
    delete_instance(doomed)
    warnings = _capture_warnings(monkeypatch)

    _render_a_page(nicegui_client)

    hits = [m for m in warnings if str(doomed.id) in m]
    assert hits, f'no WARNING naming the vanished account: {warnings}'


# ---------------------------------------------------------------------------
# The inverse defect: quietly resetting a selection that was fine.
# ---------------------------------------------------------------------------

def test_a_valid_selection_is_preserved_exactly(nicegui_client, storage, clock):
    """The quiet failure mode, and the more annoying one of the two."""
    create_account_definition(name='Alpaca Live', provider='MockAccount')
    tasty = create_account_definition(name='Tasty', provider='MockAccount')
    afc.set_selected_account_id(tasty.id)

    _render_a_page(nicegui_client)

    select = _account_select(nicegui_client)
    assert select.value == tasty.id
    assert select.options[select.value] == 'Tasty (MockAccount)'
    assert storage[afc.account_filter_storage_key()] == tasty.id
    assert afc._last_known_account_id == tasty.id


def test_all_stays_all_and_is_never_reported_as_a_vanished_account(
        nicegui_client, storage, clock, monkeypatch):
    """``None`` is a selection, not a missing one."""
    create_account_definition(name='Alpaca Live', provider='MockAccount')
    afc.set_selected_account_id(None)
    warnings = _capture_warnings(monkeypatch)

    _render_a_page(nicegui_client)

    assert _account_select(nicegui_client).value == 'all'
    assert warnings == []


def test_an_account_created_inside_the_60s_cache_window_is_not_treated_as_dead(
        nicegui_client, storage, clock, monkeypatch):
    """A brand-new account is ABSENT from the cached listing and perfectly valid.

    The accounts listing is cached for 60 seconds. "Not in the options" therefore
    means "not in a listing that may be up to a minute old", which is not the same
    fact as "deleted". Coercing a just-created account back to All — and wiping the
    user's brand-new selection to make it stick — would be a worse bug than the
    crash, because it is silent and it happens on the happy path.
    """
    create_account_definition(name='Alpaca Live', provider='MockAccount')
    _warm_the_listing()                       # cached BEFORE the new account exists

    newborn = create_account_definition(name='Tasty', provider='MockAccount')
    afc.set_selected_account_id(newborn.id)
    warnings = _capture_warnings(monkeypatch)

    _render_a_page(nicegui_client)            # still inside the 60s window

    select = _account_select(nicegui_client)
    assert select.value == newborn.id
    assert select.options[select.value] == 'Tasty (MockAccount)'
    assert storage[afc.account_filter_storage_key()] == newborn.id
    assert warnings == []


def test_a_database_that_will_not_answer_never_erases_the_selection(
        nicegui_client, second_client, storage, clock, monkeypatch):
    """A failed read is not evidence of a deletion.

    ``get_accounts_for_filter`` swallows its own errors and answers with just
    ("All", None). If that were taken at face value, one hiccup in the accounts
    query would permanently clear a selection that was never invalid. The page
    still has to render, so the widget falls back to All — but nothing is
    persisted, and the next healthy render brings the account back.
    """
    tasty = create_account_definition(name='Tasty', provider='MockAccount')
    afc.set_selected_account_id(tasty.id)

    real = afc.get_all_instances
    broken = {'now': True}

    def _maybe_explode(model):
        if broken['now']:
            raise RuntimeError('accounts table is gone')
        return real(model)

    monkeypatch.setattr(afc, 'get_all_instances', _maybe_explode)

    _render_a_page(nicegui_client)            # must not raise

    assert _account_select(nicegui_client).value == 'all'
    assert storage[afc.account_filter_storage_key()] == tasty.id     # NOT cleared
    assert afc._last_known_account_id == tasty.id

    # ... and the next healthy page load restores it, no user action needed.
    broken['now'] = False
    _cool_the_listing()
    _render_a_page(second_client)

    assert _account_select(second_client).value == tasty.id


def test_a_valid_selection_costs_no_extra_database_read(
        nicegui_client, storage, clock, monkeypatch):
    """The re-read is the RARE path, not the render path.

    Re-reading the accounts table on every page build to prove the selection is
    alive would make the 60-second cache pointless — this header is drawn on every
    navigation.
    """
    tasty = create_account_definition(name='Tasty', provider='MockAccount')
    afc.set_selected_account_id(tasty.id)
    _warm_the_listing()

    real = afc.get_all_instances
    reads = []
    monkeypatch.setattr(afc, 'get_all_instances',
                        lambda model: (reads.append(model), real(model))[1])

    _render_a_page(nicegui_client)

    assert _account_select(nicegui_client).value == tasty.id
    assert reads == [], f'{len(reads)} accounts read(s) on a warm, valid render'


# ---------------------------------------------------------------------------
# The listing helper the header leans on.
# ---------------------------------------------------------------------------

def test_refreshing_the_listing_bypasses_the_cache(clock):
    create_account_definition(name='Alpaca Live', provider='MockAccount')
    _warm_the_listing()
    tasty = create_account_definition(name='Tasty', provider='MockAccount')

    assert afc.get_accounts_for_filter() == [('All', None),
                                             ('Alpaca Live (MockAccount)', 1)]
    assert afc.refresh_accounts_for_filter() == [
        ('All', None), ('Alpaca Live (MockAccount)', 1), ('Tasty (MockAccount)', tasty.id)]
    # ... and the refreshed listing becomes the cached one.
    assert afc.get_accounts_for_filter()[-1] == ('Tasty (MockAccount)', tasty.id)


def test_a_failed_listing_read_is_reported_as_unknown_not_as_no_accounts(
        clock, monkeypatch):
    """``None`` means "could not tell", which is what callers must branch on.

    An empty list would be indistinguishable from "every account was deleted".
    """
    monkeypatch.setattr(afc, 'get_all_instances',
                        lambda _m: (_ for _ in ()).throw(RuntimeError('db down')))

    assert afc.refresh_accounts_for_filter() is None


def test_a_failed_read_serves_the_last_known_listing_rather_than_an_empty_one(
        clock, monkeypatch):
    create_account_definition(name='Alpaca Live', provider='MockAccount')
    warm = _warm_the_listing()
    clock.advance(120)                        # the cache has expired

    monkeypatch.setattr(afc, 'get_all_instances',
                        lambda _m: (_ for _ in ()).throw(RuntimeError('db down')))

    assert afc.get_accounts_for_filter() == warm
