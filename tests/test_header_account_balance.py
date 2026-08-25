"""The header's account-balance readout (the thing that replaced the LIVE badge).

The old badge was the string ``'LIVE'`` — hardcoded, reflecting nothing. What sits
there now is the selected account's ``net_liquidation``, and because this header is
built on EVERY page render the interesting properties are not "does it print a
number" but:

* the render path never touches a broker (a broker call here is a broker call per
  navigation, and a hanging one would hang the whole app);
* an unreadable broker renders a DASH, never ``$0.00`` — while a genuinely empty
  account still renders ``$0.00``;
* a value that outlived two refresh windows is labelled stale rather than shown as
  if it were current;
* under "All", one unreadable account makes the TOTAL unknown; it does not quietly
  shrink it (the unknown-reads-as-zero pattern).

Three notes on how it runs:

1. TIME IS INJECTED, never real and never today. Every cache function takes a
   ``utcnow`` callable; the tests pin it to 2019-03-04, years away from the system
   clock, so a test that accidentally depends on "now" cannot pass by coincidence.
   (There is no freezegun in this venv.)
2. RENDERING uses a bare ``nicegui.Client`` for its slot stack, as
   ``tests/test_portfolio_allocation_page.py`` does; ``nicegui.testing`` is used
   nowhere in this suite. The decisions themselves live in pure module-level
   functions and are unit-tested directly.
3. NO NETWORK. The account instance factory is monkeypatched; the doubles return
   canned ``AccountSnapshot``s or raise.

Never ``caplog``: ``logger.py`` sets ``propagate = False``.
"""
import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_trade_platform.ui import layout
from ba2_trade_platform.ui.layout import (
    HEADER_BALANCE_REFRESH_SECONDS,
    HEADER_BALANCE_STALE_AFTER_SECONDS,
    HEADER_BALANCE_STALE_SUFFIX,
    HEADER_BALANCE_UNAVAILABLE_TEXT,
    HeaderBalance,
    clear_header_balance_cache,
    combine_account_values,
    header_balance,
    header_balance_from_cache,
    refresh_header_balance_cache,
)
from tests.factories import create_account_definition


# ---------------------------------------------------------------------------
# Time. Pinned FAR from the system clock on purpose — this is cache-expiry logic
# and a test frozen to today would pass for the wrong reason.
# ---------------------------------------------------------------------------

T0 = datetime(2019, 3, 4, 10, 0, 0, tzinfo=timezone.utc)


class Clock:
    """An injectable ``utcnow``. ``clock.advance(seconds)`` moves it."""

    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


# ---------------------------------------------------------------------------
# Broker doubles
# ---------------------------------------------------------------------------

class _Broker:
    """An account whose ``get_account_snapshot`` is scripted.

    ``net_liquidation`` and ``equity`` are set INDEPENDENTLY so a test can pin
    which field is read.
    """

    def __init__(self, *, net_liquidation=None, equity=None, raises=None,
                 hangs=None):
        self.snapshot = AccountSnapshot(net_liquidation=net_liquidation,
                                        equity=equity)
        self.raises = raises
        self.hangs = hangs
        self.calls = 0
        self.threads = []

    def get_account_snapshot(self):
        self.calls += 1
        self.threads.append(threading.current_thread())
        if self.hangs:
            import time
            time.sleep(self.hangs)
        if self.raises is not None:
            raise self.raises
        return self.snapshot


def _use_brokers(monkeypatch, brokers):
    """Make the layout's account factory serve ``{account_id: broker}``."""
    import ba2_trade_platform.core.utils as core_utils
    monkeypatch.setattr(core_utils, 'get_account_instance_from_id',
                        lambda account_id: brokers[account_id])
    return brokers


@pytest.fixture(autouse=True)
def _clean_cache():
    """Isolate BOTH process-global caches this module renders through.

    ``clear_header_balance_cache`` is the obvious one. The second is
    ``account_filter_context``, which keeps a 60-second accounts cache AND a
    process-wide mirror of the selected account id (``_last_known_account_id``,
    the fallback for threaded callers that cannot reach ``app.storage.user``).
    Neither is reset by ``reset_test_db``, so a module that ran earlier and
    selected account 2 leaves that id behind; the next module's fresh database
    has no account 2, and ``_render_account_filter_dropdown`` dies on
    ``ValueError: Invalid value: 2`` before the page is drawn. That is a
    pre-existing leak between test modules, not a property of the header — this
    fixture just refuses to inherit it.
    """
    import ba2_trade_platform.ui.account_filter_context as afc
    clear_header_balance_cache()
    afc._last_known_account_id = None
    afc._accounts_cache['data'] = None
    afc._accounts_cache['timestamp'] = 0
    yield
    clear_header_balance_cache()
    afc._last_known_account_id = None
    afc._accounts_cache['data'] = None
    afc._accounts_cache['timestamp'] = 0


@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-header-balance'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _texts(root):
    return [el._text for el in root.descendants(include_self=True) if el._text]


def _timers(root, marker=None):
    """The header's ``ui.timer``s, selected by MARKER.

    Not by interval: the layout may grow other timers, and a test that silently
    picked one of those up would assert nothing.
    """
    from nicegui import ui
    out = [el for el in root.descendants() if isinstance(el, ui.timer)]
    if marker is None:
        return out
    return [el for el in out if marker in (el._markers or ())]


def _first_refresh_timer(root):
    from ba2_trade_platform.ui.layout import HEADER_BALANCE_FIRST_TIMER_MARKER
    timers = _timers(root, HEADER_BALANCE_FIRST_TIMER_MARKER)
    assert timers, 'the header must kick off a first refresh'
    return timers[0]


# ---------------------------------------------------------------------------
# The pure decision: what does the header SAY?
# ---------------------------------------------------------------------------

def test_a_fresh_value_prints_the_money():
    view = header_balance(value=2511.9, as_of=T0, now=T0 + timedelta(minutes=5))
    assert isinstance(view, HeaderBalance)
    assert view.text == '$2,511.90'
    assert view.available is True
    assert view.stale is False


def test_a_genuinely_zero_balance_prints_zero_and_is_available():
    """A fully withdrawn account is a real state, not an outage.

    This is the inverse of the bug below it, and it is just as wrong: suppressing
    a true zero as "unavailable" tells the user the broker is down when it is not.
    """
    view = header_balance(value=0.0, as_of=T0, now=T0)
    assert view.text == '$0.00'
    assert view.available is True


def test_an_unknown_balance_never_renders_as_zero():
    """A broker that will not answer is not an empty account."""
    view = header_balance(value=None, as_of=None, now=T0)
    assert view.text == HEADER_BALANCE_UNAVAILABLE_TEXT
    assert '$0.00' not in view.text
    assert '0' not in view.text
    assert view.available is False


def test_an_unknown_balance_says_unknown_not_zero_in_its_detail():
    view = header_balance(value=None, as_of=None, now=T0,
                          unreadable=['Alpaca Live (AlpacaAccount)'])
    assert 'Alpaca Live (AlpacaAccount)' in view.detail
    assert 'not zero' in view.detail


def test_a_negative_account_value_keeps_its_sign_outside_the_currency_symbol():
    """Reuses ``format_account_money``; an account underwater on margin is real."""
    view = header_balance(value=-1200.0, as_of=T0, now=T0)
    assert view.text == '-$1,200.00'
    assert view.available is True


def test_a_value_that_outlived_two_refresh_windows_is_marked_stale():
    """A confidently-displayed stale balance is worse than an honest dash."""
    view = header_balance(value=2511.9, as_of=T0,
                          now=T0 + timedelta(seconds=HEADER_BALANCE_STALE_AFTER_SECONDS + 1))
    assert view.stale is True
    assert view.text.endswith(HEADER_BALANCE_STALE_SUFFIX)
    assert view.text.startswith('$2,511.90')
    assert view.available is True          # the money is still shown, honestly labelled


def test_the_staleness_boundary_is_two_refresh_windows_exactly():
    """One missed refresh is normal scheduling jitter; two is a broker that stopped."""
    assert HEADER_BALANCE_STALE_AFTER_SECONDS == 2 * HEADER_BALANCE_REFRESH_SECONDS
    at_boundary = header_balance(
        value=1.0, as_of=T0,
        now=T0 + timedelta(seconds=HEADER_BALANCE_STALE_AFTER_SECONDS))
    past_boundary = header_balance(
        value=1.0, as_of=T0,
        now=T0 + timedelta(seconds=HEADER_BALANCE_STALE_AFTER_SECONDS + 1))
    assert at_boundary.stale is False
    assert past_boundary.stale is True


def test_a_value_one_refresh_window_old_is_not_yet_stale():
    view = header_balance(value=1.0, as_of=T0,
                          now=T0 + timedelta(seconds=HEADER_BALANCE_REFRESH_SECONDS + 1))
    assert view.stale is False
    assert HEADER_BALANCE_STALE_SUFFIX not in view.text


def test_the_detail_carries_the_as_of_timestamp_whenever_there_is_a_value():
    """A marker without a timestamp leaves the reader unable to judge HOW stale."""
    fresh = header_balance(value=1.0, as_of=T0, now=T0)
    stale = header_balance(value=1.0, as_of=T0,
                           now=T0 + timedelta(seconds=HEADER_BALANCE_STALE_AFTER_SECONDS + 1))
    for view in (fresh, stale):
        assert '2019-03-04 10:00' in view.detail
    assert 'stale' in stale.detail.lower()


def test_a_value_with_no_timestamp_cannot_be_vouched_for_and_is_stale():
    """We cannot claim freshness for a figure we cannot date."""
    view = header_balance(value=1.0, as_of=None, now=T0)
    assert view.stale is True


# ---------------------------------------------------------------------------
# "All accounts": summed, and one unreadable account poisons the total
# ---------------------------------------------------------------------------

def test_all_accounts_sums_the_readable_balances():
    value, unreadable = combine_account_values([('A', 1000.0), ('B', 2511.9)])
    assert value == pytest.approx(3511.9)
    assert unreadable == []


def test_one_unreadable_account_makes_the_total_partial_and_names_it():
    """THE rule, and it has been SHARPENED. ``1000 + unknown`` is not ``1000``.

    What must never happen is the unreadable leg quietly vanishing, leaving a
    confident, wrong, SMALLER total — the unknown-reads-as-zero pattern. This used
    to be prevented by making the whole total unknown, on the grounds that a bare
    badge has no room to explain itself.

    It now has room: under "All" the badge carries a per-account BREAKDOWN, and
    the total is marked ' (partial)' in its own TEXT (not only in the hover, per
    the same rule as ' (stale)') and names the account it excludes. That is
    strictly more information than a dash — the reader keeps the $1,000 they can
    actually see in the breakdown — and it says the same thing, the same way, as
    the 'Floating P/L Per Account' card's total.

    A total with NOTHING readable is still unknown; see the test below.
    """
    value, unreadable = combine_account_values([('Alpaca', 1000.0), ('Tasty', None)])
    assert value == pytest.approx(1000.0)
    assert unreadable == ['Tasty']


def test_a_total_with_nothing_readable_at_all_is_unknown():
    """There is no part to show, so there is no partial to mark."""
    value, unreadable = combine_account_values([('Alpaca', None), ('Tasty', None)])
    assert value is None
    assert unreadable == ['Alpaca', 'Tasty']


def test_a_partial_total_is_marked_in_the_text_and_explained_in_the_detail():
    view = header_balance(value=1000.0, as_of=T0, now=T0, unreadable=['Tasty'])
    assert view.text == '$1,000.00' + layout.HEADER_BALANCE_PARTIAL_SUFFIX
    assert view.partial is True
    assert view.available is True
    assert 'Tasty' in view.detail


def test_a_complete_total_is_not_marked_partial():
    view = header_balance(value=1000.0, as_of=T0, now=T0)
    assert view.text == '$1,000.00'
    assert view.partial is False


def test_a_partial_total_that_is_also_stale_says_both():
    view = header_balance(value=1000.0, as_of=T0, unreadable=['Tasty'],
                          now=T0 + timedelta(seconds=HEADER_BALANCE_STALE_AFTER_SECONDS + 1))
    assert layout.HEADER_BALANCE_PARTIAL_SUFFIX in view.text
    assert view.text.endswith(HEADER_BALANCE_STALE_SUFFIX)
    assert view.partial is True and view.stale is True


def test_a_total_made_only_of_genuine_zeroes_is_zero_not_unknown():
    value, unreadable = combine_account_values([('A', 0.0), ('B', 0.0)])
    assert value == 0.0
    assert unreadable == []


def test_no_accounts_at_all_is_unknown_rather_than_zero():
    """An app with no broker configured has no total; it does not have $0.00."""
    value, unreadable = combine_account_values([])
    assert value is None
    assert unreadable == []


def test_the_parts_are_not_rounded_before_they_are_summed():
    """Round once, at the end. Rounding the legs first drifts the total."""
    value, _ = combine_account_values([('A', 0.005), ('B', 0.005), ('C', 0.005)])
    assert value == pytest.approx(0.015)


# ---------------------------------------------------------------------------
# The cache: the render is served from it, and the broker is hit at most hourly
# ---------------------------------------------------------------------------

def test_reading_the_header_never_calls_the_broker():
    """The cache read is the render path. It must be pure dict work.

    A broker call here would be a broker call on EVERY page navigation.
    """
    broker = _Broker(net_liquidation=2511.9)
    accounts = [(1, 'Alpaca')]
    for _ in range(50):
        header_balance_from_cache(accounts, utcnow=Clock())
    assert broker.calls == 0


def test_a_refreshed_value_is_then_served_from_the_cache_without_a_second_call(monkeypatch):
    clock = Clock()
    broker = _Broker(net_liquidation=2511.9)
    _use_brokers(monkeypatch, {1: broker})
    accounts = [(1, 'Alpaca')]

    refresh_header_balance_cache([1], utcnow=clock)
    assert broker.calls == 1

    for _ in range(20):
        view = header_balance_from_cache(accounts, utcnow=clock)
    assert broker.calls == 1
    assert view.text == '$2,511.90'


def test_the_broker_is_not_re_read_before_the_refresh_interval_elapses(monkeypatch):
    clock = Clock()
    broker = _Broker(net_liquidation=100.0)
    _use_brokers(monkeypatch, {1: broker})

    refresh_header_balance_cache([1], utcnow=clock)
    clock.advance(HEADER_BALANCE_REFRESH_SECONDS - 1)
    refresh_header_balance_cache([1], utcnow=clock)
    assert broker.calls == 1, 'a refresh inside the window must be a no-op'

    clock.advance(1)
    refresh_header_balance_cache([1], utcnow=clock)
    assert broker.calls == 2, 'the refresh must actually happen once the window elapses'


def test_the_refresh_interval_is_hourly():
    assert HEADER_BALANCE_REFRESH_SECONDS == 3600.0


def test_the_field_read_is_net_liquidation_not_equity(monkeypatch):
    """Reuses ``account_value_from_snapshot``; ``equity`` alone is not enough.

    A broker that published only ``equity`` has, by the ``AccountSnapshot``
    contract, an adapter bug — and the header must report unknown rather than
    invent a second, divergent rule for what "the account's value" means.
    """
    clock = Clock()
    equity_only = _Broker(equity=999.0, net_liquidation=None)
    netliq_only = _Broker(equity=None, net_liquidation=5.0)
    _use_brokers(monkeypatch, {1: equity_only, 2: netliq_only})

    refresh_header_balance_cache([1, 2], utcnow=clock)
    assert header_balance_from_cache([(1, 'A')], utcnow=clock).available is False
    assert header_balance_from_cache([(2, 'B')], utcnow=clock).text == '$5.00'


def test_a_failed_refresh_keeps_the_last_known_value_and_never_zeroes_it(monkeypatch):
    clock = Clock()
    broker = _Broker(net_liquidation=2511.9)
    _use_brokers(monkeypatch, {1: broker})
    accounts = [(1, 'Alpaca')]

    refresh_header_balance_cache([1], utcnow=clock)
    assert header_balance_from_cache(accounts, utcnow=clock).text == '$2,511.90'

    broker.raises = RuntimeError('503 from the broker')
    clock.advance(HEADER_BALANCE_REFRESH_SECONDS)
    refresh_header_balance_cache([1], utcnow=clock)

    view = header_balance_from_cache(accounts, utcnow=clock)
    assert view.available is True
    assert view.text.startswith('$2,511.90'), 'the last known value must survive'
    assert '$0.00' not in view.text


def test_a_failed_refresh_does_not_advance_the_as_of_so_the_value_goes_stale(monkeypatch):
    """The whole point of keeping the stale value: it must LOOK stale."""
    clock = Clock()
    broker = _Broker(net_liquidation=2511.9)
    _use_brokers(monkeypatch, {1: broker})
    accounts = [(1, 'Alpaca')]

    refresh_header_balance_cache([1], utcnow=clock)
    broker.raises = RuntimeError('503')

    for _ in range(3):
        clock.advance(HEADER_BALANCE_REFRESH_SECONDS)
        refresh_header_balance_cache([1], utcnow=clock)

    view = header_balance_from_cache(accounts, utcnow=clock)
    assert view.stale is True
    assert view.text.endswith(HEADER_BALANCE_STALE_SUFFIX)
    assert '2019-03-04 10:00' in view.detail, 'the honest as-of, not the failed attempt'


def test_a_recovered_broker_clears_the_stale_marker(monkeypatch):
    clock = Clock()
    broker = _Broker(net_liquidation=2511.9)
    _use_brokers(monkeypatch, {1: broker})
    accounts = [(1, 'Alpaca')]

    refresh_header_balance_cache([1], utcnow=clock)
    broker.raises = RuntimeError('503')
    clock.advance(HEADER_BALANCE_STALE_AFTER_SECONDS + 1)
    refresh_header_balance_cache([1], utcnow=clock)
    assert header_balance_from_cache(accounts, utcnow=clock).stale is True

    broker.raises = None
    broker.snapshot = AccountSnapshot(net_liquidation=2600.0)
    clock.advance(HEADER_BALANCE_REFRESH_SECONDS)
    refresh_header_balance_cache([1], utcnow=clock)

    view = header_balance_from_cache(accounts, utcnow=clock)
    assert view.stale is False
    assert view.text == '$2,600.00'


def test_a_raising_broker_never_propagates_out_of_the_refresh(monkeypatch):
    clock = Clock()
    _use_brokers(monkeypatch, {1: _Broker(raises=RuntimeError('kaboom'))})
    refresh_header_balance_cache([1], utcnow=clock)     # must not raise
    assert header_balance_from_cache([(1, 'A')], utcnow=clock).available is False


def test_a_missing_account_factory_never_propagates(monkeypatch):
    """``get_account_instance_from_id`` itself can blow up (bad provider row)."""
    import ba2_trade_platform.core.utils as core_utils
    monkeypatch.setattr(core_utils, 'get_account_instance_from_id',
                        lambda account_id: (_ for _ in ()).throw(ValueError('no such provider')))
    refresh_header_balance_cache([1], utcnow=Clock())   # must not raise
    assert header_balance_from_cache([(1, 'A')], utcnow=Clock()).available is False


def test_one_broken_account_does_not_stop_the_others_being_read(monkeypatch):
    clock = Clock()
    good = _Broker(net_liquidation=10.0)
    _use_brokers(monkeypatch, {1: _Broker(raises=RuntimeError('down')), 2: good})
    refresh_header_balance_cache([1, 2], utcnow=clock)
    assert good.calls == 1
    assert header_balance_from_cache([(2, 'B')], utcnow=clock).text == '$10.00'


def test_under_all_a_single_dead_broker_makes_the_header_partial(monkeypatch):
    """End to end through the cache: 1000 + dead is '$1,000.00 (partial)'.

    Never a bare '$1,000.00' -- that is the whole point -- and the account that
    failed is named where the reader can find it.
    """
    clock = Clock()
    _use_brokers(monkeypatch, {1: _Broker(net_liquidation=1000.0),
                               2: _Broker(raises=RuntimeError('down'))})
    refresh_header_balance_cache([1, 2], utcnow=clock)

    view = header_balance_from_cache([(1, 'Alpaca'), (2, 'Tasty')], utcnow=clock)
    assert view.partial is True
    assert view.text == '$1,000.00' + layout.HEADER_BALANCE_PARTIAL_SUFFIX
    assert 'Tasty' in view.detail


def test_under_all_two_dead_brokers_leave_the_header_with_no_number_at_all(monkeypatch):
    clock = Clock()
    _use_brokers(monkeypatch, {1: _Broker(raises=RuntimeError('down')),
                               2: _Broker(raises=RuntimeError('down'))})
    refresh_header_balance_cache([1, 2], utcnow=clock)

    view = header_balance_from_cache([(1, 'Alpaca'), (2, 'Tasty')], utcnow=clock)
    assert view.available is False
    assert view.text == HEADER_BALANCE_UNAVAILABLE_TEXT
    assert 'Alpaca' in view.detail and 'Tasty' in view.detail


def test_a_single_unreadable_account_is_still_a_dash_and_never_a_partial_zero(monkeypatch):
    """The one-account case must be untouched by the partial rule.

    With one leg there is no "part" to show: nothing was readable, so the badge is
    a dash. '$0.00 (partial)' would be the unknown-reads-as-zero bug wearing a
    caveat.
    """
    clock = Clock()
    _use_brokers(monkeypatch, {1: _Broker(raises=RuntimeError('down'))})
    refresh_header_balance_cache([1], utcnow=clock)

    view = header_balance_from_cache([(1, 'Alpaca')], utcnow=clock)
    assert view.text == HEADER_BALANCE_UNAVAILABLE_TEXT
    assert view.partial is False
    assert view.available is False


def test_an_all_total_is_dated_by_its_OLDEST_leg(monkeypatch):
    """A total is only as fresh as its stalest component."""
    clock = Clock()
    a, b = _Broker(net_liquidation=1.0), _Broker(net_liquidation=2.0)
    _use_brokers(monkeypatch, {1: a, 2: b})

    refresh_header_balance_cache([1], utcnow=clock)          # account 1 read at T0
    clock.advance(HEADER_BALANCE_STALE_AFTER_SECONDS + 1)
    refresh_header_balance_cache([2], utcnow=clock)          # account 2 read now

    view = header_balance_from_cache([(1, 'A'), (2, 'B')], utcnow=clock)
    assert view.text.startswith('$3.00')
    assert view.stale is True, 'leg A is two windows old; the total is not fresh'
    assert '2019-03-04 10:00' in view.detail


def test_an_account_never_read_at_all_is_unknown_not_zero():
    view = header_balance_from_cache([(99, 'Never read')], utcnow=Clock())
    assert view.available is False
    assert view.text == HEADER_BALANCE_UNAVAILABLE_TEXT


def test_a_concurrent_refresh_of_the_same_account_does_not_stampede(monkeypatch):
    """Two page loads at once must not become two broker calls."""
    clock = Clock()
    broker = _Broker(net_liquidation=1.0, hangs=0.3)
    _use_brokers(monkeypatch, {1: broker})

    threads = [threading.Thread(target=refresh_header_balance_cache,
                                args=([1],), kwargs={'utcnow': clock})
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert broker.calls == 1


def test_a_losing_concurrent_refresher_does_not_wait_on_the_broker(monkeypatch):
    """The stampede guard is a NON-BLOCKING acquire, and that is the point.

    A plain blocking ``lock.acquire()`` would still produce ONE broker call — the
    second holder re-checks the window and finds the value fresh — so a
    call-count assertion alone cannot tell the two apart. What it would also do is
    PARK every other worker thread for a full broker round trip, which is how one
    slow account exhausts the pool that the rest of the UI offloads into. So the
    property under test is elapsed time, not call count: exactly one refresher
    pays the broker's latency.
    """
    clock = Clock()
    hang = 0.6
    broker = _Broker(net_liquidation=1.0, hangs=hang)
    _use_brokers(monkeypatch, {1: broker})

    elapsed = []

    def _timed():
        import time
        started = time.monotonic()
        refresh_header_balance_cache([1], utcnow=clock)
        elapsed.append(time.monotonic() - started)

    threads = [threading.Thread(target=_timed) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    waited = [e for e in elapsed if e >= hang * 0.7]
    assert len(waited) == 1, (
        f'only the refresher that actually calls the broker may block; got {elapsed}')


def test_a_dead_broker_is_retried_hourly_not_on_every_single_refresh(monkeypatch):
    """A failed attempt still COUNTS as an attempt.

    If the broker's exception escaped ``_refresh_one_balance``, no cache entry
    would ever be recorded for that account, so every page load and every timer
    tick would try again — a down broker would be hammered at exactly the moment
    it can least afford it, and each attempt would block a worker thread for the
    length of its timeout.
    """
    clock = Clock()
    broker = _Broker(raises=RuntimeError('503'))
    _use_brokers(monkeypatch, {1: broker})

    for _ in range(5):
        refresh_header_balance_cache([1], utcnow=clock)
    assert broker.calls == 1, 'a failing broker must not be re-read within the hour'

    clock.advance(HEADER_BALANCE_REFRESH_SECONDS)
    refresh_header_balance_cache([1], utcnow=clock)
    assert broker.calls == 2, 'but it must be retried once the hour is up'


# ---------------------------------------------------------------------------
# The header itself
# ---------------------------------------------------------------------------

def test_the_header_no_longer_shows_the_hardcoded_LIVE_badge(nicegui_client, monkeypatch):
    """It reflected nothing: not live-vs-paper, not a connection, not a state."""
    from nicegui import ui
    with nicegui_client:
        with layout.layout_render('Test'):
            ui.label('body')
    assert 'LIVE' not in _texts(nicegui_client.layout)


def test_the_header_renders_the_cached_balance(nicegui_client, monkeypatch):
    """The REAL clock here, deliberately, and it is not an expiry test.

    ``layout_render`` reads the cache through the production ``_utcnow``, so
    seeding the cache from the pinned 2019 clock would make the entry seven years
    old and the header would (correctly) draw it stale. Staleness has its own
    tests, with time injected on both sides; this one only asks whether the render
    path shows the cached money.
    """
    broker = _Broker(net_liquidation=2511.9)
    _use_brokers(monkeypatch, {1: broker})
    account = create_account_definition(name='Alpaca Live', provider='MockAccount')
    monkeypatch.setattr(layout, 'get_selected_account_id', lambda: account.id)
    monkeypatch.setattr(layout, 'get_accounts_for_filter',
                        lambda: [('All', None), ('Alpaca Live (MockAccount)', account.id)])

    refresh_header_balance_cache([account.id])

    from nicegui import ui
    with nicegui_client:
        with layout.layout_render('Test'):
            ui.label('body')

    assert '$2,511.90' in _texts(nicegui_client.layout)


def test_rendering_the_header_makes_no_broker_call_at_all(nicegui_client, monkeypatch):
    """THE constraint. Every page render builds this header."""
    broker = _Broker(net_liquidation=2511.9)
    _use_brokers(monkeypatch, {1: broker})
    account = create_account_definition(name='Alpaca Live', provider='MockAccount')
    monkeypatch.setattr(layout, 'get_selected_account_id', lambda: account.id)
    monkeypatch.setattr(layout, 'get_accounts_for_filter',
                        lambda: [('All', None), ('Alpaca Live (MockAccount)', account.id)])

    from nicegui import ui
    for _ in range(5):
        with nicegui_client:
            with layout.layout_render('Test'):
                ui.label('body')

    assert broker.calls == 0


def test_a_broker_that_hangs_for_ten_seconds_does_not_delay_the_render(
        nicegui_client, monkeypatch):
    import time
    broker = _Broker(net_liquidation=1.0, hangs=10.0)
    _use_brokers(monkeypatch, {1: broker})
    account = create_account_definition(name='Slow', provider='MockAccount')
    monkeypatch.setattr(layout, 'get_selected_account_id', lambda: account.id)
    monkeypatch.setattr(layout, 'get_accounts_for_filter',
                        lambda: [('All', None), ('Slow (MockAccount)', account.id)])

    from nicegui import ui
    started = time.monotonic()
    with nicegui_client:
        with layout.layout_render('Test'):
            ui.label('body')
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f'the header blocked the render for {elapsed:.1f}s'
    assert broker.calls == 0


def test_a_header_whose_scope_lookup_explodes_still_renders_the_page(
        nicegui_client, monkeypatch):
    """The header degrades; it does not take the app down."""
    monkeypatch.setattr(layout, 'accounts_in_scope',
                        lambda: (_ for _ in ()).throw(RuntimeError('accounts table is gone')))

    from nicegui import ui
    with nicegui_client:
        with layout.layout_render('Test'):
            ui.label('body')

    texts = _texts(nicegui_client.layout)
    assert 'body' in texts, 'the page content must still be drawn'
    assert HEADER_BALANCE_UNAVAILABLE_TEXT in texts


def test_a_header_whose_decision_explodes_still_renders_the_page(
        nicegui_client, monkeypatch):
    """A bug in the balance logic must cost the balance, not the application."""
    monkeypatch.setattr(layout, 'header_balance_from_cache',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('bad view')))

    from nicegui import ui
    with nicegui_client:
        with layout.layout_render('Test'):
            ui.label('body')

    texts = _texts(nicegui_client.layout)
    assert 'body' in texts
    assert HEADER_BALANCE_UNAVAILABLE_TEXT in texts


def test_the_header_schedules_an_hourly_refresh(nicegui_client, monkeypatch):
    account = create_account_definition(name='Alpaca', provider='MockAccount')
    monkeypatch.setattr(layout, 'get_selected_account_id', lambda: account.id)
    monkeypatch.setattr(layout, 'get_accounts_for_filter',
                        lambda: [('All', None), ('Alpaca (MockAccount)', account.id)])

    from nicegui import ui
    with nicegui_client:
        with layout.layout_render('Test'):
            ui.label('body')

    ticks = _timers(nicegui_client.layout, layout.HEADER_BALANCE_TICK_TIMER_MARKER)
    assert [t.interval for t in ticks] == [HEADER_BALANCE_REFRESH_SECONDS]


def test_the_refresh_runs_off_the_event_loop_thread(nicegui_client, monkeypatch):
    """``asyncio.to_thread``, per this repo's convention.

    If the broker read happened on the event loop thread, a slow broker would
    freeze every open page, not just this widget.
    """
    broker = _Broker(net_liquidation=42.0)
    _use_brokers(monkeypatch, {1: broker})
    account = create_account_definition(name='Alpaca', provider='MockAccount')
    monkeypatch.setattr(layout, 'get_selected_account_id', lambda: account.id)
    monkeypatch.setattr(layout, 'get_accounts_for_filter',
                        lambda: [('All', None), ('Alpaca (MockAccount)', account.id)])

    from nicegui import ui
    with nicegui_client:
        with layout.layout_render('Test'):
            ui.label('body')

    once = _first_refresh_timer(nicegui_client.layout)
    loop_thread = {}

    async def _drive():
        loop_thread['t'] = threading.current_thread()
        with nicegui_client:
            await once.callback()

    asyncio.run(_drive())

    assert broker.calls == 1
    assert broker.threads[0] is not loop_thread['t'], \
        'the broker read must not run on the event loop thread'


def test_a_refresh_that_raises_is_swallowed_by_the_header(nicegui_client, monkeypatch):
    """A refresh failure must never surface as an unhandled task exception."""
    account = create_account_definition(name='Alpaca', provider='MockAccount')
    monkeypatch.setattr(layout, 'get_selected_account_id', lambda: account.id)
    monkeypatch.setattr(layout, 'get_accounts_for_filter',
                        lambda: [('All', None), ('Alpaca (MockAccount)', account.id)])
    monkeypatch.setattr(layout, 'refresh_header_balance_cache',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))

    from nicegui import ui
    with nicegui_client:
        with layout.layout_render('Test'):
            ui.label('body')

    once = _first_refresh_timer(nicegui_client.layout)

    async def _drive():
        with nicegui_client:
            await once.callback()

    asyncio.run(_drive())        # must not raise


def test_the_refresh_updates_the_label_in_place(nicegui_client, monkeypatch):
    broker = _Broker(net_liquidation=42.0)
    _use_brokers(monkeypatch, {1: broker})
    account = create_account_definition(name='Alpaca', provider='MockAccount')
    monkeypatch.setattr(layout, 'get_selected_account_id', lambda: account.id)
    monkeypatch.setattr(layout, 'get_accounts_for_filter',
                        lambda: [('All', None), ('Alpaca (MockAccount)', account.id)])

    from nicegui import ui
    with nicegui_client:
        with layout.layout_render('Test'):
            ui.label('body')
    assert HEADER_BALANCE_UNAVAILABLE_TEXT in _texts(nicegui_client.layout)

    once = _first_refresh_timer(nicegui_client.layout)

    async def _drive():
        with nicegui_client:
            await once.callback()

    asyncio.run(_drive())

    assert '$42.00' in _texts(nicegui_client.layout)


# ---------------------------------------------------------------------------
# THE "ALL" BREAKDOWN
#
# Under "All" the badge is a single number standing for several accounts, and
# there was no way to see what it was made of -- which of two accounts the
# $3,200.68 belonged to, or whether one of them had failed to answer at all. The
# badge stays compact; the parts live in a menu hanging off it.
#
# Same three states as the Floating P/L card (commit 9738cd61), said the same
# way: a figure, a measured '$0.00', or '—' for could-not-read. A total missing
# a leg is marked ' (partial)' and names what it excludes.
# ---------------------------------------------------------------------------

def _render_header(monkeypatch, nicegui_client, options, selected):
    """Draw the whole layout with *options* in the dropdown and *selected* chosen."""
    monkeypatch.setattr(layout, 'get_selected_account_id', lambda: selected)
    monkeypatch.setattr(layout, 'get_accounts_for_filter', lambda: options)
    from nicegui import ui
    with nicegui_client:
        with layout.layout_render('Test'):
            ui.label('body')
    return _texts(nicegui_client.layout)


def _seed(monkeypatch, values):
    """Put real, freshly-read values in the cache for ``{account_id: value}``.

    The REAL clock, as ``test_the_header_renders_the_cached_balance`` explains:
    seeding from the pinned 2019 clock would make every entry seven years old and
    the header would correctly draw it stale. Staleness has its own tests.
    """
    _use_brokers(monkeypatch, {acc_id: _Broker(net_liquidation=v)
                               for acc_id, v in values.items()})
    refresh_header_balance_cache(list(values))


TWO_ACCOUNTS = [('All', None), ('Alpaca (X)', 1), ('Tasty (Y)', 2)]


def test_the_breakdown_lists_every_account_and_a_total(nicegui_client, monkeypatch):
    """THE ASK. 'All' shows a single number; this is what it is made of."""
    _seed(monkeypatch, {1: 1000.0, 2: 2200.68})

    texts = _render_header(monkeypatch, nicegui_client, TWO_ACCOUNTS, None)

    assert 'Alpaca (X)' in texts
    assert 'Tasty (Y)' in texts
    assert '$1,000.00' in texts
    assert '$2,200.68' in texts
    assert layout.HEADER_BALANCE_TOTAL_LABEL in texts
    assert texts.count('$3,200.68') == 2      # the badge, and the Total line


def test_the_breakdown_lists_each_account_exactly_once(nicegui_client, monkeypatch):
    """Double-counting a leg is invisible when the legs are equal -- so they are."""
    _seed(monkeypatch, {1: 1000.0, 2: 1000.0})

    texts = _render_header(monkeypatch, nicegui_client, TWO_ACCOUNTS, None)

    assert texts.count('Alpaca (X)') == 1
    assert texts.count('Tasty (Y)') == 1
    assert texts.count('$1,000.00') == 2      # one per account line, and no more
    assert texts.count('$2,000.00') == 2      # the badge and the Total


def test_no_account_is_dropped_from_the_breakdown(nicegui_client, monkeypatch):
    """An account that never answered is still one of the accounts."""
    _seed(monkeypatch, {1: 1000.0})           # account 2 was never read at all

    texts = _render_header(monkeypatch, nicegui_client, TWO_ACCOUNTS, None)

    assert 'Tasty (Y)' in texts
    assert HEADER_BALANCE_UNAVAILABLE_TEXT in texts
    assert '$1,000.00' in texts               # the one that DID answer, kept


def test_a_partial_total_is_marked_and_names_what_it_excludes(nicegui_client, monkeypatch):
    _seed(monkeypatch, {1: 1000.0})

    texts = _render_header(monkeypatch, nicegui_client, TWO_ACCOUNTS, None)

    assert f'$1,000.00{layout.HEADER_BALANCE_PARTIAL_SUFFIX}' in texts
    assert any('Tasty (Y)' in t and 'partial' in t for t in texts), texts


def test_a_single_selected_account_gets_no_breakdown(nicegui_client, monkeypatch):
    """Unchanged behaviour: there is nothing to break a single account down into."""
    _seed(monkeypatch, {1: 1000.0, 2: 2200.68})

    texts = _render_header(monkeypatch, nicegui_client, TWO_ACCOUNTS, 1)

    assert '$1,000.00' in texts
    assert layout.HEADER_BALANCE_TOTAL_LABEL not in texts
    assert '$2,200.68' not in texts
    assert '$3,200.68' not in texts


def test_the_breakdown_shows_a_genuinely_empty_account_as_zero(nicegui_client, monkeypatch):
    """The inverse error: a withdrawn account is not an unreadable one."""
    _seed(monkeypatch, {1: 1000.0, 2: 0.0})

    texts = _render_header(monkeypatch, nicegui_client, TWO_ACCOUNTS, None)

    assert '$0.00' in texts
    assert HEADER_BALANCE_UNAVAILABLE_TEXT not in texts
    # The badge, the Alpaca leg, and the Total -- the zero leg adds nothing to it.
    assert texts.count('$1,000.00') == 3
    assert layout.HEADER_BALANCE_PARTIAL_SUFFIX not in ''.join(texts)


def test_the_breakdown_repaints_when_the_refresh_lands(nicegui_client, monkeypatch):
    """The menu is drawn from the cache like the badge, so it must repaint too.

    A breakdown frozen at the empty pre-refresh state would show dashes forever
    beside a badge that had updated.
    """
    _use_brokers(monkeypatch, {1: _Broker(net_liquidation=1000.0),
                               2: _Broker(net_liquidation=2200.68)})
    monkeypatch.setattr(layout, 'get_selected_account_id', lambda: None)
    monkeypatch.setattr(layout, 'get_accounts_for_filter', lambda: TWO_ACCOUNTS)

    from nicegui import ui
    with nicegui_client:
        with layout.layout_render('Test'):
            ui.label('body')
    assert '$2,200.68' not in _texts(nicegui_client.layout)

    once = _first_refresh_timer(nicegui_client.layout)

    async def _drive():
        with nicegui_client:
            await once.callback()

    asyncio.run(_drive())

    texts = _texts(nicegui_client.layout)
    assert '$2,200.68' in texts
    assert texts.count('$3,200.68') == 2
    assert texts.count('Tasty (Y)') == 1, 'the repaint must replace the menu, not append'


def test_a_breakdown_that_explodes_still_renders_the_page(nicegui_client, monkeypatch):
    """Same contract as the badge: the header degrades, the app does not."""
    _seed(monkeypatch, {1: 1000.0, 2: 2200.68})
    monkeypatch.setattr(layout, 'header_balance_breakdown',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('bad lines')))

    texts = _render_header(monkeypatch, nicegui_client, TWO_ACCOUNTS, None)

    assert 'body' in texts


# --- the pure decision behind the breakdown -------------------------------

def test_the_breakdown_reads_one_line_per_account_from_the_cache(monkeypatch):
    clock = Clock()
    _use_brokers(monkeypatch, {1: _Broker(net_liquidation=1000.0),
                               2: _Broker(net_liquidation=2200.68)})
    refresh_header_balance_cache([1, 2], utcnow=clock)

    view = layout.header_balance_breakdown([(1, 'Alpaca'), (2, 'Tasty')], utcnow=clock)

    assert [label for label, _ in view.lines] == ['Alpaca', 'Tasty']
    assert [line.text for _, line in view.lines] == ['$1,000.00', '$2,200.68']
    assert view.total.text == '$3,200.68'
    assert view.total.available is True


def test_a_breakdown_line_for_an_unread_account_is_a_dash_not_a_zero(monkeypatch):
    clock = Clock()
    _use_brokers(monkeypatch, {1: _Broker(net_liquidation=1000.0)})
    refresh_header_balance_cache([1], utcnow=clock)

    view = layout.header_balance_breakdown([(1, 'Alpaca'), (2, 'Tasty')], utcnow=clock)

    unread = dict(view.lines)['Tasty']
    assert unread.text == HEADER_BALANCE_UNAVAILABLE_TEXT
    assert unread.available is False
    assert 'Tasty' in unread.detail and 'not zero' in unread.detail


def test_a_breakdown_line_that_is_genuinely_zero_is_available(monkeypatch):
    clock = Clock()
    _use_brokers(monkeypatch, {1: _Broker(net_liquidation=0.0)})
    refresh_header_balance_cache([1], utcnow=clock)

    line = dict(layout.header_balance_breakdown([(1, 'Alpaca')], utcnow=clock).lines)['Alpaca']
    assert line.text == '$0.00'
    assert line.available is True


def test_selecting_all_reads_every_account_not_just_the_first(monkeypatch):
    """"All" means the sum, so the scope helper must return every real account."""
    monkeypatch.setattr(layout, 'get_selected_account_id', lambda: None)
    monkeypatch.setattr(layout, 'get_accounts_for_filter',
                        lambda: [('All', None), ('A (X)', 1), ('B (Y)', 2)])
    assert layout.accounts_in_scope() == [(1, 'A (X)'), (2, 'B (Y)')]


def test_selecting_one_account_narrows_the_scope_to_it(monkeypatch):
    monkeypatch.setattr(layout, 'get_selected_account_id', lambda: 2)
    monkeypatch.setattr(layout, 'get_accounts_for_filter',
                        lambda: [('All', None), ('A (X)', 1), ('B (Y)', 2)])
    assert layout.accounts_in_scope() == [(2, 'B (Y)')]
