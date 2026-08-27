"""The four ACCOUNT-LEVEL dashboard widgets must work for an account with no experts.

A manual account (TastyTrade, or any hand-traded Alpaca account) has transactions,
positions and P&L but ZERO ``ExpertInstance`` rows. Four Overview widgets show data
that belongs to the ACCOUNT, not to an expert:

    * 📈 Trade Performance                    (``overview._load_trade_performance_data``)
    * 📊 Floating P/L Per Account             (``FloatingPLPerAccountWidget``)
    * 📊 Position Distribution by Labels      (``overview._load_position_distribution_async``)
    * 📊 Position Distribution by Categories  (idem, ``grouping_field='categories'``)

Three of them used to route the account filter through
``get_expert_ids_for_account`` and then filter ``Transaction.expert_id.in_(...)``.
For an account with no experts that list is ``[]``, the query short-circuits, and the
widget renders as though the account had never traded -- a MEASURED-LOOKING ZERO over
real money. "This account has no experts" is not "this account has no data".

THAT WAS ONLY HALF OF IT. Fixing the QUERY still left ``Floating P/L Per Account``
building its rows out of the transactions that came back, so an account with
nothing open produced no group, no row, and no statement about itself at all --
which is how TastyTrade (manual, and flat) stayed missing from the card after the
expert filter was gone. An account-level card lists the ACCOUNTS, and each one is
in exactly one of three states: measured, measured at zero, or unmeasurable. The
block near ``test_floating_pl_per_account_lists_a_flat_account_at_a_measured_zero``
pins all three, in both directions.

``📊 Floating P/L Per Expert`` is deliberately NOT in this file's remit: it is
genuinely per-expert and an account with no experts correctly shows nothing there.
``test_the_per_expert_widget_stays_per_expert`` pins that, so a future "fix" cannot
quietly make it account-scoped.

HOW IT RUNS
    * ``nicegui.testing`` is used nowhere in ``tests/``; a bare ``nicegui.Client``
      gives every ``ui.*`` call a slot stack, so the widget bodies really draw and
      the element tree can be read back without a browser. Same harness as
      ``tests/test_portfolio_allocation_page.py``.
    * The query decision ALSO lives in a pure helper
      (``ui/components/account_scope.py``), unit-tested directly below, so the
      "which rows" question is provable without a render at all. Both routes are
      used: the pure test pins the clause, the render tests pin what the user sees.
    * ``asyncio.to_thread`` and ``loop.run_in_executor`` are run INLINE: the test
      engine is ``sqlite:///:memory:`` on a ``SingletonThreadPool``, so a worker
      thread opens a brand-new EMPTY database and every query fails with
      'no such table'.
    * TIME IS FROZEN, and deliberately NOT to today. These widgets compare 7/14/30/60
      day windows; a test frozen to ``datetime.now()`` passes for the wrong reason the
      moment a window boundary moves.
    * Never ``caplog``: ``logger.py`` sets ``propagate = False``, so caplog's root
      handler never sees a record. ``_capture_errors`` patches the module's own logger.
"""
import asyncio
import importlib
from datetime import datetime, timedelta

import pytest

from ba2_trade_platform.core.models import Instrument, Transaction, TradingOrder
from ba2_trade_platform.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.core.db import add_instance, get_db
import ba2_trade_platform.ui.account_filter_context as afc
from ba2_trade_platform.ui.components.FloatingPLPerAccountWidget import (
    FloatingPLPerAccountWidget,
)
# ``ui/components/__init__.py`` re-exports the CLASS under the module's own name, so
# ``from ...components import FloatingPLPerAccountWidget`` hands back the class and
# ``monkeypatch.setattr(that, 'asyncio', ...)`` fails. Ask for the module explicitly.
fpl_mod = importlib.import_module(
    'ba2_trade_platform.ui.components.FloatingPLPerAccountWidget')
from ba2_trade_platform.ui.components.FloatingPLPerExpertWidget import (
    FloatingPLPerExpertWidget,
)
fpl_expert_mod = importlib.import_module(
    'ba2_trade_platform.ui.components.FloatingPLPerExpertWidget')
from ba2_trade_platform.ui.components.account_scope import scope_transactions_to_account
from ba2_trade_platform.ui.pages import overview
from tests.factories import (
    create_account_definition, create_expert_instance, create_trading_order,
    create_transaction,
)


# A Tuesday, five months before this file was written. NOT today, on purpose.
FROZEN_NOW = datetime(2026, 3, 17, 10, 30, 0)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def freeze_clock(monkeypatch):
    """Pin ``overview.datetime.now()`` to :data:`FROZEN_NOW`.

    The trade-performance widget slices at now-7d/-14d/-30d/-60d. With a live clock
    a test row placed "3 days ago" drifts relative to a row written at collection
    time, and the 30d/60d comparison buckets silently swap over month boundaries.
    """
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_NOW if tz is None else FROZEN_NOW.replace(tzinfo=tz)

    monkeypatch.setattr(overview, 'datetime', _FrozenDateTime)


@pytest.fixture(autouse=True)
def run_offloads_inline(monkeypatch):
    """Run ``asyncio.to_thread`` and ``loop.run_in_executor`` bodies on this thread.

    SQLAlchemy hands an in-memory SQLite a ``SingletonThreadPool`` -- one connection
    PER THREAD -- so an offloaded body opens its own empty database. That is a
    property of the fixture, not of the widgets: production runs against a file,
    where the offload is exactly right.
    """
    async def _inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, 'to_thread', _inline)

    class _InlineLoop:
        def run_in_executor(self, executor, func, *args):
            async def _call():
                return func(*args)
            return _call()

    class _AsyncioProxy:
        get_event_loop = staticmethod(lambda: _InlineLoop())
        create_task = staticmethod(asyncio.create_task)

    monkeypatch.setattr(fpl_mod, 'asyncio', _AsyncioProxy)


@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-overview-account-widgets'), request=None)
    yield client
    client.remove_elements(client.elements.values())


class _DictStorage:
    def __init__(self, d):
        self._d = d

    @property
    def user(self):
        return self._d


class _FakeApp:
    def __init__(self, storage):
        self.storage = storage


@pytest.fixture
def select_account(monkeypatch):
    """Drive the header account dropdown through its REAL setter.

    ``app.storage.user`` needs a live request, so the storage object is a plain
    dict; everything else (coercion, the process-wide mirror the threaded callers
    read, the expert-id cache) is the production code path.
    """
    monkeypatch.setattr(afc, 'app', _FakeApp(_DictStorage({})))

    def _select(account_id):
        afc.set_selected_account_id(account_id)

    return _select


def _capture_errors(monkeypatch, module):
    """Collect ``logger.error`` messages from *module*. NOT caplog."""
    messages = []
    monkeypatch.setattr(module.logger, 'error',
                        lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def _capture_warnings(monkeypatch, module):
    """Collect ``logger.warning`` messages from *module*. NOT caplog."""
    messages = []
    monkeypatch.setattr(module.logger, 'warning',
                        lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def _run_in_client(client, coro_factory):
    """Await a widget coroutine with the client's slot stack ACTIVE.

    NiceGUI keys its slot stack on the asyncio task, so entering ``with client:``
    on the caller's stack and then ``asyncio.run(...)`` puts the drawing inside a
    task that has no slot at all.
    """
    async def _run():
        with client:
            await coro_factory()

    asyncio.run(_run())


def _texts(root):
    """Every text fragment rendered under *root*, in document order."""
    return [el._text for el in root.descendants(include_self=True) if el._text]


def _table_rows(root):
    """Every row of every ``ui.table`` under *root*."""
    from nicegui import ui as nicegui_ui
    rows = []
    for el in root.descendants():
        if isinstance(el, nicegui_ui.table):
            rows.extend(el.rows)
    return rows


def _metric(texts, label):
    """The value rendered immediately after *label*.

    Every row in these widgets draws its caption and then its number, so the
    number is the next text fragment. Raises if the caption is absent, which is
    what makes "the widget rendered its empty state instead" a loud failure.
    """
    return texts[texts.index(label) + 1]


# ---------------------------------------------------------------------------
# Fixtures that build the two shapes that matter
# ---------------------------------------------------------------------------

def _closed_trade(account_id, symbol, pnl_per_share, days_ago, expert_id=None,
                  qty=10.0, open_price=100.0):
    """A CLOSED transaction attributed to *account_id* via its order."""
    txn = create_transaction(
        symbol=symbol, quantity=qty, side=OrderDirection.BUY,
        status=TransactionStatus.CLOSED, open_price=open_price,
        close_price=open_price + pnl_per_share,
        close_date=FROZEN_NOW - timedelta(days=days_ago),
        expert_id=expert_id,
    )
    create_trading_order(
        account_id=account_id, symbol=symbol, quantity=qty,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, transaction_id=txn.id,
        filled_qty=qty, open_price=open_price,
    )
    return txn


def _open_trade(account_id, symbol, expert_id=None, qty=10.0, open_price=100.0):
    """An OPENED transaction attributed to *account_id* via its filled order."""
    txn = create_transaction(
        symbol=symbol, quantity=qty, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=open_price,
        expert_id=expert_id,
    )
    create_trading_order(
        account_id=account_id, symbol=symbol, quantity=qty,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, transaction_id=txn.id,
        filled_qty=qty, open_price=open_price,
    )
    return txn


@pytest.fixture
def manual_account():
    """A TastyTrade-shaped account: real trades, NO expert instances."""
    return create_account_definition(name='Manual', provider='TastyTrade').id


@pytest.fixture
def expert_account():
    """An account driven by an expert, as the dashboard was originally written for."""
    account_id = create_account_definition(name='Automated', provider='Alpaca').id
    expert_id = create_expert_instance(account_id=account_id, alias='Alpha').id
    return account_id, expert_id


# ===========================================================================
# The pure helper: which transactions belong to an account
# ===========================================================================

def _scoped_ids(account_id):
    with get_db() as session:
        query = scope_transactions_to_account(
            __import__('sqlmodel').select(Transaction), account_id)
        return sorted(t.id for t in session.exec(query).all())


def test_the_scope_helper_selects_only_the_named_accounts_transactions(
        manual_account, expert_account):
    other_account, expert_id = expert_account
    mine = _open_trade(manual_account, 'AAPL')
    theirs = _open_trade(other_account, 'MSFT', expert_id=expert_id)

    assert _scoped_ids(manual_account) == [mine.id]
    assert _scoped_ids(other_account) == [theirs.id]


def test_the_scope_helper_does_not_care_whether_an_expert_is_attached(manual_account):
    """The whole point: attribution is the ORDER's account, never the expert."""
    txn = _open_trade(manual_account, 'AAPL', expert_id=None)
    assert _scoped_ids(manual_account) == [txn.id]


def test_the_scope_helper_returns_nothing_for_an_account_that_never_traded(
        manual_account, expert_account):
    other_account, expert_id = expert_account
    _open_trade(other_account, 'MSFT', expert_id=expert_id)
    assert _scoped_ids(manual_account) == []


def test_no_account_selected_means_every_account_not_none_of_them(
        manual_account, expert_account):
    """``None`` is the header's "All" -- and is the ONLY value that widens the query."""
    other_account, expert_id = expert_account
    a = _open_trade(manual_account, 'AAPL')
    b = _open_trade(other_account, 'MSFT', expert_id=expert_id)
    assert _scoped_ids(None) == sorted([a.id, b.id])


def test_a_transaction_with_no_orders_belongs_to_no_account(manual_account):
    """It cannot be attributed, so it must not leak into a filtered view."""
    create_transaction(symbol='ORPHAN', status=TransactionStatus.OPENED)
    assert _scoped_ids(manual_account) == []


# ===========================================================================
# 📈 Trade Performance
# ===========================================================================

def _render_trade_performance(client):
    tab = overview.OverviewTab.__new__(overview.OverviewTab)
    from nicegui import ui

    holder = {}

    def _factory():
        with ui.column() as root:
            holder['root'] = root
            loading = ui.label('🔄 Loading...')
            content = ui.column()
        return tab._load_trade_performance_data(loading, content)

    async def _run():
        with client:
            await _factory()

    asyncio.run(_run())
    return _texts(holder['root'])


def test_trade_performance_shows_real_trades_for_an_account_with_no_experts(
        nicegui_client, select_account, manual_account):
    """THE BUG. Expert-id filtering turned a traded account into a row of zeros."""
    _open_trade(manual_account, 'AAPL')
    _closed_trade(manual_account, 'MSFT', pnl_per_share=10.0, days_ago=3)

    select_account(manual_account)
    texts = _render_trade_performance(nicegui_client)

    assert _metric(texts, 'Open Trades') == '1'
    assert _metric(texts, 'Closed (30d)') == '1'
    assert _metric(texts, 'Winning') == '1'
    assert _metric(texts, 'Losing') == '0'
    assert _metric(texts, 'P&L (7d)') == '$100.00'
    assert _metric(texts, 'P&L (30d)') == '$100.00'


def test_trade_performance_for_an_account_with_experts_still_works(
        nicegui_client, select_account, expert_account):
    """No regression: the shape the dashboard was written for keeps working."""
    account_id, expert_id = expert_account
    _open_trade(account_id, 'AAPL', expert_id=expert_id)
    _closed_trade(account_id, 'MSFT', pnl_per_share=5.0, days_ago=3, expert_id=expert_id)

    select_account(account_id)
    texts = _render_trade_performance(nicegui_client)

    assert _metric(texts, 'Open Trades') == '1'
    assert _metric(texts, 'Closed (30d)') == '1'
    assert _metric(texts, 'P&L (30d)') == '$50.00'


def test_trade_performance_never_shows_another_accounts_trades(
        nicegui_client, select_account, manual_account, expert_account):
    """Mutation guard: drop the account filter and this is what leaks."""
    other_account, expert_id = expert_account
    _closed_trade(manual_account, 'AAPL', pnl_per_share=10.0, days_ago=3)
    _closed_trade(other_account, 'MSFT', pnl_per_share=5.0, days_ago=3, expert_id=expert_id)

    select_account(manual_account)
    texts = _render_trade_performance(nicegui_client)
    assert _metric(texts, 'P&L (30d)') == '$100.00'
    assert '$150.00' not in texts and '$50.00' not in texts

    select_account(other_account)
    texts = _render_trade_performance(nicegui_client)
    assert _metric(texts, 'P&L (30d)') == '$50.00'
    assert '$150.00' not in texts and '$100.00' not in texts


def test_trade_performance_on_a_genuinely_empty_account_says_so(
        nicegui_client, select_account, manual_account, expert_account):
    """An account with neither experts nor trades must NOT render a measured zero.

    '$0.00' next to 'P&L (30d)' is a MEASUREMENT: it says the account traded and
    broke even. Nothing was measured here.
    """
    other_account, expert_id = expert_account
    _closed_trade(other_account, 'MSFT', pnl_per_share=5.0, days_ago=3, expert_id=expert_id)

    select_account(manual_account)
    texts = _render_trade_performance(nicegui_client)

    assert overview.NO_TRADES_FOR_ACCOUNT in texts
    assert 'Open Trades' not in texts
    assert 'P&L (30d)' not in texts
    assert '$0.00' not in texts
    assert '🔄 Loading...' not in texts   # not a spinner either


def test_trade_performance_with_all_accounts_and_no_data_anywhere_says_so_generically(
        nicegui_client, select_account, manual_account):
    """"All" + nothing is still an empty state -- but it is not "this account"."""
    select_account(None)
    texts = _render_trade_performance(nicegui_client)

    assert overview.NO_TRADES_AT_ALL in texts
    assert overview.NO_TRADES_FOR_ACCOUNT not in texts
    assert 'Open Trades' not in texts


def test_trade_performance_with_all_accounts_selected_aggregates_everything(
        nicegui_client, select_account, manual_account, expert_account):
    other_account, expert_id = expert_account
    _closed_trade(manual_account, 'AAPL', pnl_per_share=10.0, days_ago=3)
    _closed_trade(other_account, 'MSFT', pnl_per_share=5.0, days_ago=3, expert_id=expert_id)

    select_account(None)
    texts = _render_trade_performance(nicegui_client)

    assert _metric(texts, 'Closed (30d)') == '2'
    assert _metric(texts, 'P&L (30d)') == '$150.00'


def test_trade_performance_compares_against_the_previous_period(
        nicegui_client, select_account, manual_account):
    """The 7d/30d deltas are why the clock is frozen away from today."""
    _closed_trade(manual_account, 'AAPL', pnl_per_share=10.0, days_ago=3)    # this week
    _closed_trade(manual_account, 'MSFT', pnl_per_share=4.0, days_ago=10)    # last week

    select_account(manual_account)
    texts = _render_trade_performance(nicegui_client)

    assert _metric(texts, 'P&L (7d)') == '$100.00'
    assert '▲ +$60.00' in texts          # 100 this week vs 40 last week
    assert _metric(texts, 'P&L (30d)') == '$140.00'


def test_trade_performance_ignores_trades_closed_outside_the_window(
        nicegui_client, select_account, manual_account):
    _closed_trade(manual_account, 'AAPL', pnl_per_share=10.0, days_ago=3)
    _closed_trade(manual_account, 'OLD', pnl_per_share=99.0, days_ago=200)

    select_account(manual_account)
    texts = _render_trade_performance(nicegui_client)

    assert _metric(texts, 'Closed (30d)') == '1'
    assert _metric(texts, 'P&L (30d)') == '$100.00'


def test_trade_performance_open_count_is_scoped_to_the_account_too(
        nicegui_client, select_account, manual_account, expert_account):
    """The open-trade count is a separate query; it leaks separately."""
    other_account, expert_id = expert_account
    _open_trade(manual_account, 'AAPL')
    _open_trade(other_account, 'MSFT', expert_id=expert_id)
    _open_trade(other_account, 'GOOGL', expert_id=expert_id)

    select_account(manual_account)
    assert _metric(_render_trade_performance(nicegui_client), 'Open Trades') == '1'


# ===========================================================================
# 📊 Floating P/L Per Account
# ===========================================================================

_UNSET = object()


class _Broker:
    """An account interface stand-in with a controllable position book.

    ``positions=None`` means the FETCH FAILED and is stored as ``None``, not
    normalised to ``[]`` -- normalising it here would make the double violate the
    tri-state contract it exists to test, and the fetch-failure test would pass
    against a broken widget.
    """

    def __init__(self, positions=_UNSET, balance=None):
        self._positions = [] if positions is _UNSET else positions
        self._balance = balance

    def get_balance(self):
        return self._balance

    def get_positions(self):
        return self._positions


def _use_brokers(monkeypatch, brokers):
    """Point the widget's account factory at *brokers* (``{account_id: _Broker}``)."""
    monkeypatch.setattr(fpl_mod, 'get_account_instance_from_id',
                        lambda account_id, session=None: brokers.get(account_id))


def _price(symbol, current_price, unrealized_pl=None):
    """A broker position dict.

    ``unrealized_pl`` is what the MANUAL-account path (``_rows_for_manual_account``)
    sums directly; ``current_price`` is what the expert-driven, local-reconstruction
    path (``_transaction_pl``) multiplies against the platform's own recorded fills.
    A manual-account test that wants a specific figure must set ``unrealized_pl``
    explicitly -- ``current_price`` alone no longer produces one for that account.
    """
    d = {'symbol': symbol, 'current_price': current_price}
    if unrealized_pl is not None:
        d['unrealized_pl'] = unrealized_pl
    return d


def _render_floating_pl(client, widget_cls):
    widget = widget_cls.__new__(widget_cls)
    from nicegui import ui

    holder = {}

    def _factory():
        with ui.column() as root:
            holder['root'] = root
            loading = ui.label('🔄 Calculating floating P/L...')
            content = ui.column()
        return widget._load_data_async(loading, content)

    async def _run():
        with client:
            await _factory()

    asyncio.run(_run())
    return _texts(holder['root'])


def test_floating_pl_per_account_shows_pl_for_an_account_with_no_experts(
        nicegui_client, select_account, monkeypatch, manual_account):
    """THE BUG. ``_calculate_pl_sync`` bailed out on the empty expert list.

    A manual account is priced from the BROKER's own ``unrealized_pl`` (see
    ``_rows_for_manual_account``), not from this local trade's cost basis -- the
    local trade exists here only to prove it is NOT what drives the number.
    """
    _open_trade(manual_account, 'AAPL', qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {manual_account: _Broker(
        [_price('AAPL', 110.0, unrealized_pl=100.0)], balance=25_000.0)})

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Manual' in texts
    assert '$100.00' in texts
    assert 'Bal: $25,000.00' in texts
    assert 'No open positions' not in texts


def test_floating_pl_per_account_for_an_account_with_experts_still_works(
        nicegui_client, select_account, monkeypatch, expert_account):
    account_id, expert_id = expert_account
    _open_trade(account_id, 'AAPL', expert_id=expert_id, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {account_id: _Broker([_price('AAPL', 105.0)])})

    select_account(account_id)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Automated' in texts
    assert '$50.00' in texts


def test_floating_pl_per_account_never_shows_another_accounts_positions(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    other_account, expert_id = expert_account
    _open_trade(manual_account, 'AAPL', qty=10.0, open_price=100.0)
    _open_trade(other_account, 'MSFT', expert_id=expert_id, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {
        manual_account: _Broker([_price('AAPL', 110.0, unrealized_pl=100.0)]),
        other_account: _Broker([_price('MSFT', 105.0)]),
    })

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)
    assert 'Manual' in texts
    assert 'Automated' not in texts
    assert '$50.00' not in texts

    select_account(other_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)
    assert 'Automated' in texts
    assert 'Manual' not in texts
    assert '$100.00' not in texts


def test_floating_pl_per_account_on_a_genuinely_empty_account_measures_it_at_zero(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    """SUPERSEDED RULE, kept as a test so the reasoning is not lost.

    This used to assert the whole card said 'No open positions' and drew NO row
    and no total. That was defensible while the card was a list of positions --
    but it is a list of ACCOUNTS, and the selected account exists, was asked, and
    answered '[]'. Saying nothing about it is strictly less than saying '$0.00',
    and it is the same silence that hid TastyTrade.

    What survives from the old test is the part that was always right: the other
    account's money stays out of it, and the spinner is gone.
    """
    other_account, expert_id = expert_account
    _open_trade(other_account, 'MSFT', expert_id=expert_id, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {
        manual_account: _Broker([]),
        other_account: _Broker([_price('MSFT', 105.0)]),
    })

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Manual' in texts
    assert texts.count('$0.00') == 2        # the row, and a total that is also zero
    assert 'Automated' not in texts
    assert '$50.00' not in texts
    assert 'No open positions' not in texts
    assert '🔄 Calculating floating P/L...' not in texts


def test_floating_pl_per_account_with_all_accounts_selected_shows_both(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    other_account, expert_id = expert_account
    _open_trade(manual_account, 'AAPL', qty=10.0, open_price=100.0)
    _open_trade(other_account, 'MSFT', expert_id=expert_id, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {
        manual_account: _Broker([_price('AAPL', 110.0, unrealized_pl=100.0)]),
        other_account: _Broker([_price('MSFT', 105.0)]),
    })

    select_account(None)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Manual' in texts and 'Automated' in texts
    assert '$150.00' in texts      # the total


def test_floating_pl_per_account_includes_a_manual_trade_in_an_expert_account(
        nicegui_client, select_account, monkeypatch, expert_account):
    """A hand-placed trade on an expert-driven account is still the ACCOUNT's P/L.

    The expert-id filter hid these too -- the real database has open transactions
    with ``expert_id IS NULL`` sitting on expert-driven accounts.
    """
    account_id, expert_id = expert_account
    _open_trade(account_id, 'AAPL', expert_id=expert_id, qty=10.0, open_price=100.0)
    _open_trade(account_id, 'MSFT', expert_id=None, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {account_id: _Broker([_price('AAPL', 110.0),
                                                    _price('MSFT', 105.0)])})

    select_account(account_id)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert '$150.00' in texts      # 100 from the expert trade + 50 from the manual one


# ===========================================================================
# 📊 Floating P/L Per Account: EVERY account in scope gets a row, in ONE OF
# THREE states
#
# The card lists ACCOUNTS, so the list of accounts is the list of accounts --
# read from ``AccountDefinition``, not inferred from whatever happens to have an
# open transaction (and certainly not from the experts). An account that is
# missing from the card makes NO STATEMENT about itself, and the three statements
# the card can actually make are all different:
#
#   * a measured figure        -> '$52.43'      (positions read, priced)
#   * a measured ZERO          -> '$0.00'       (broker answered [] -- flat)
#   * could not be measured    -> 'P/L unknown' (broker answered None / no
#                                                account instance / raised)
#
# TastyTrade shows 'Open: 0' in the Orders widget and had no row here at all:
# grouping the rows out of the transaction table meant an account with nothing
# open produced no key, so it vanished -- the same defect as dropping it for
# having no experts, one layer further down.
# ===========================================================================

def _flat(balance=None):
    """A broker that answers 'I hold nothing' -- ``[]``, not ``None``."""
    return _Broker([], balance=balance)


def _unreadable(balance=None):
    """A broker whose position fetch FAILED -- ``None``, per the tri-state."""
    return _Broker(None, balance=balance)


def test_floating_pl_per_account_lists_a_flat_account_at_a_measured_zero(
        nicegui_client, select_account, monkeypatch, manual_account):
    """THE BUG. 'Open: 0' is a measurement; no row at all is not.

    The account has no experts AND no open transactions, which is exactly the
    shape that produced no row. The broker answered ``[]``, so the floating P/L
    is measured, and it is $0.00.
    """
    _use_brokers(monkeypatch, {manual_account: _flat(balance=1_234.56)})

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Manual' in texts
    assert '$0.00' in texts
    assert 'Bal: $1,234.56' in texts
    assert 'P/L unknown' not in texts
    assert 'No open positions' not in texts


def test_floating_pl_per_account_lists_a_flat_account_next_to_a_trading_one(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    """THE SCREENSHOT: 'All' selected, one account flat, one holding. TWO rows.

    The flat account's balance counts towards the total balance too -- the header
    row read 'Bal: $717.37' when that was only one of the two accounts.
    """
    other_account, expert_id = expert_account
    _open_trade(other_account, 'MSFT', expert_id=expert_id, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {
        manual_account: _flat(balance=717.37),
        other_account: _Broker([_price('MSFT', 105.0)], balance=1_000.00),
    })

    select_account(None)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Manual' in texts and 'Automated' in texts
    assert '$0.00' in texts                 # the flat account, measured
    assert 'Bal: $717.37' in texts
    assert 'Bal: $1,717.37' in texts        # the TOTAL, including the flat account
    assert texts.count('$50.00') == 2       # the trading row, and the total


def test_floating_pl_per_account_lists_each_account_exactly_once(
        nicegui_client, select_account, monkeypatch, manual_account):
    """Seeding the row from the account table must not double the rows of an
    account that ALSO has open transactions."""
    _open_trade(manual_account, 'AAPL', qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {manual_account: _Broker(
        [_price('AAPL', 110.0, unrealized_pl=100.0)])})

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert texts.count('Manual') == 1
    assert texts.count('$100.00') == 2      # the row and the total, nothing more


def test_floating_pl_per_account_does_not_list_an_account_outside_the_selection(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    """The seed honours the dropdown: 'All' is the ONLY selection that widens it."""
    other_account, _ = expert_account
    _use_brokers(monkeypatch, {manual_account: _flat(), other_account: _flat()})

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Manual' in texts
    assert 'Automated' not in texts


def test_floating_pl_per_account_says_unknown_when_the_position_book_could_not_be_read(
        nicegui_client, select_account, monkeypatch, manual_account):
    """``get_positions()`` returns ``None`` on FAILURE -- never ``[]``.

    Drawing '$0.00' for a broker outage tells the user their account is flat;
    drawing nothing tells them nothing. Both are worse than saying so.
    """
    _open_trade(manual_account, 'AAPL', qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {manual_account: _unreadable(balance=500.0)})
    errors = _capture_errors(monkeypatch, fpl_mod)

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Manual' in texts
    assert texts.count('P/L unknown') == 2   # the row, and the total it poisoned
    assert '$0.00' not in texts
    assert 'Bal: $500.00' in texts           # the balance WAS readable; keep it
    assert errors                            # and the failure was logged


def test_floating_pl_per_account_says_unknown_when_the_account_cannot_be_instantiated(
        nicegui_client, select_account, monkeypatch, manual_account):
    """No broker object at all is the same class of failure as ``None`` positions.

    The log line is asserted on because it is the only thing that separates this
    from letting ``None.get_positions()`` throw: both reach the user's 'unknown',
    but the AttributeError sends whoever reads the log hunting an outage that
    never happened.
    """
    _use_brokers(monkeypatch, {})
    errors = _capture_errors(monkeypatch, fpl_mod)

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Manual' in texts
    assert 'P/L unknown' in texts
    assert '$0.00' not in texts
    assert any('Could not build an account instance' in e for e in errors), errors


def test_floating_pl_per_account_says_unknown_when_the_broker_raises(
        nicegui_client, select_account, monkeypatch, manual_account):
    """An exception is a failed measurement, not a zero one."""
    class _Exploding:
        def get_balance(self):
            raise RuntimeError('balance boom')

        def get_positions(self):
            raise RuntimeError('positions boom')

    _use_brokers(monkeypatch, {manual_account: _Exploding()})
    errors = _capture_errors(monkeypatch, fpl_mod)

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Manual' in texts
    assert 'P/L unknown' in texts
    assert '$0.00' not in texts
    assert errors


def test_floating_pl_per_account_tells_a_measured_zero_apart_from_an_unknown(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    """BOTH DIRECTIONS, on one screen.

    Collapsing either way is a lie: the flat account is not unknown, and the
    unreadable one is not flat.
    """
    other_account, _ = expert_account
    _use_brokers(monkeypatch, {manual_account: _flat(), other_account: _unreadable()})
    _capture_errors(monkeypatch, fpl_mod)

    select_account(None)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Manual' in texts and 'Automated' in texts
    assert texts.count('$0.00') == 1         # ONLY the flat one
    assert texts.count('P/L unknown') == 1   # ONLY the unreadable one


def test_floating_pl_per_account_total_is_partial_and_names_what_it_left_out(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    """``50 + unknown`` is not ``50``.

    The total may still show the part it could add, but it must not present that
    as THE total, and it must say whose money is missing from it.
    """
    other_account, expert_id = expert_account
    _open_trade(other_account, 'MSFT', expert_id=expert_id, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {
        manual_account: _unreadable(),
        other_account: _Broker([_price('MSFT', 105.0)]),
    })
    _capture_errors(monkeypatch, fpl_mod)

    select_account(None)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert '$50.00 (partial)' in texts
    assert texts.count('$50.00') == 1        # the row only; the total is marked
    assert any('Manual' in t and 'could not be measured' in t for t in texts), texts


def test_floating_pl_per_account_total_is_unknown_when_nothing_could_be_read(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    other_account, _ = expert_account
    _use_brokers(monkeypatch, {
        manual_account: _unreadable(),
        other_account: _unreadable(),
    })
    _capture_errors(monkeypatch, fpl_mod)

    select_account(None)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert texts.count('P/L unknown') == 3   # two rows and the total
    assert '$0.00' not in texts
    assert '(partial)' not in ''.join(texts)


def test_floating_pl_per_account_says_when_a_balance_could_not_be_read(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    """The balance is a second, independent read -- and its own tri-state.

    A balance that would not come back must not silently drop out of the total
    balance, which is how 'Bal: $717.37' came to be presented as both accounts'.
    """
    other_account, _ = expert_account
    _use_brokers(monkeypatch, {
        manual_account: _flat(balance=None),        # balance unreadable
        other_account: _flat(balance=1_000.00),
    })

    select_account(None)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Bal: unknown' in texts
    assert 'Bal: $1,000.00' in texts                # the row that DID answer
    assert 'Bal: $1,000.00 (partial)' in texts      # the total, honestly marked
    assert any('Manual' in t and 'balance' in t.lower() for t in texts), texts


def test_floating_pl_per_account_total_balance_is_unknown_when_none_could_be_read(
        nicegui_client, select_account, monkeypatch, manual_account):
    _use_brokers(monkeypatch, {manual_account: _flat(balance=None)})

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert texts.count('Bal: unknown') == 2         # the row and the total
    assert 'Bal: $0.00' not in texts


def test_floating_pl_per_account_keeps_a_genuinely_zero_balance(
        nicegui_client, select_account, monkeypatch, manual_account):
    """The inverse error. ``0.0`` is a real balance and must print as one."""
    _use_brokers(monkeypatch, {manual_account: _flat(balance=0.0)})

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert texts.count('Bal: $0.00') == 2           # the row and the total
    assert 'Bal: unknown' not in texts


def test_floating_pl_per_account_marks_a_manual_row_partial_when_a_position_has_no_broker_pl(
        nicegui_client, select_account, monkeypatch, manual_account):
    """A broker position with no ``unrealized_pl`` is a MISSING LEG, not a zero one.

    Silently skipping it (what the widget did before ``_rows_for_manual_account``
    existed) understated the account's P/L by however much that position is worth,
    with nothing on screen to say so. Local transactions play no part here: a
    manual account's completeness is judged entirely by what the BROKER returned
    (see ``_is_manual_account``), not by what the platform happens to have recorded.
    """
    _use_brokers(monkeypatch, {manual_account: _Broker([
        _price('AAPL', 110.0, unrealized_pl=100.0),
        {'symbol': 'MSFT', 'current_price': 105.0},   # no unrealized_pl
    ])})

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    # BOTH the row and the total. Marking only one of them leaves a bare
    # '$100.00' on screen that reads as the whole answer.
    assert texts.count('$100.00 (partial)') == 2
    assert '$100.00' not in texts
    assert any('MSFT' in t for t in texts), texts


def test_floating_pl_per_account_treats_a_position_the_broker_did_not_price_as_unpriced(
        nicegui_client, select_account, monkeypatch, manual_account):
    """A manual-account position with no ``unrealized_pl`` has no measured P/L.

    Coercing that to 0.0 does not merely lose the leg -- it invents one, and the
    invented one is catastrophic: a $1,000 holding marked to zero prints a
    $1,000 loss the account never took. The local trade below is a red herring
    on purpose: a manual account's P/L is priced from the broker's own book (see
    ``_rows_for_manual_account``), so it must not influence the result either way.
    """
    _open_trade(manual_account, 'AAPL', qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {manual_account: _Broker(
        [{'symbol': 'AAPL', 'current_price': None}])})

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert '-$1,000.00' not in texts
    assert texts.count('$0.00 (partial)') == 2
    assert any('AAPL' in t for t in texts), texts


def test_floating_pl_per_account_does_not_call_a_pending_order_unpriced(
        nicegui_client, select_account, monkeypatch, manual_account):
    """A WAITING transaction holds NO position, so its P/L is a measured zero.

    'No net position' and 'no price for a position we hold' are different, and
    conflating them would mark every account with a resting order as partial.
    """
    txn = create_transaction(symbol='AAPL', quantity=10.0, side=OrderDirection.BUY,
                             status=TransactionStatus.WAITING, open_price=100.0)
    create_trading_order(account_id=manual_account, symbol='AAPL', quantity=10.0,
                         side=OrderDirection.BUY, order_type=OrderType.MARKET,
                         status=OrderStatus.PENDING, transaction_id=txn.id,
                         filled_qty=0.0)
    _use_brokers(monkeypatch, {manual_account: _flat()})

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert '$0.00' in texts
    assert '(partial)' not in ''.join(texts)
    assert 'P/L unknown' not in texts


def test_floating_pl_per_account_with_no_accounts_at_all_says_so(
        nicegui_client, select_account, monkeypatch):
    """No accounts configured is its own statement -- and not 'no positions'."""
    _use_brokers(monkeypatch, {})

    select_account(None)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'No accounts configured' in texts
    assert '$0.00' not in texts
    assert 'Total P/L:' not in texts


def test_the_per_expert_widget_is_not_seeded_with_accounts(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    """The per-EXPERT card lists experts, so the account seed must not reach it.

    Seeding it would put an account name in a list of experts and, for a manual
    account, resurrect exactly the row 'Floating P/L Per Expert' is supposed not
    to have.
    """
    other_account, _ = expert_account
    _use_brokers(monkeypatch, {manual_account: _flat(), other_account: _flat()})

    select_account(None)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerExpertWidget)

    assert texts == ['No open positions']


def test_the_per_expert_widget_reports_an_unreadable_broker_as_unknown(
        nicegui_client, select_account, monkeypatch, expert_account):
    """Same tri-state, same card body: a failed fetch is not a flat expert."""
    account_id, expert_id = expert_account
    _open_trade(account_id, 'AAPL', expert_id=expert_id, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {account_id: _unreadable()})
    _capture_errors(monkeypatch, fpl_mod)

    select_account(account_id)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerExpertWidget)

    assert f'Alpha-{expert_id}' in texts
    assert 'P/L unknown' in texts
    assert '$0.00' not in texts


def test_the_per_expert_widget_shows_no_balance_row(
        nicegui_client, select_account, monkeypatch, expert_account):
    """``_show_balance`` is False there: an expert has no broker balance."""
    account_id, expert_id = expert_account
    _open_trade(account_id, 'AAPL', expert_id=expert_id, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {account_id: _Broker([_price('AAPL', 110.0)],
                                                   balance=9_999.0)})

    select_account(account_id)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerExpertWidget)

    assert not [t for t in texts if t.startswith('Bal:')], texts


def test_the_per_expert_widget_stays_per_expert(
        nicegui_client, select_account, monkeypatch, manual_account):
    """OUT OF SCOPE BY DESIGN -- pinned so nobody "fixes" it into an account view.

    'Floating P/L Per Expert' answers "how is each expert doing". An account with
    no experts has no answer, and inventing one by falling back to the account
    would make the two widgets duplicates.
    """
    _open_trade(manual_account, 'AAPL', qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {manual_account: _Broker([_price('AAPL', 110.0)])})

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerExpertWidget)

    assert 'No open positions' in texts
    assert 'Manual' not in texts


def test_the_per_expert_widget_still_reports_its_experts(
        nicegui_client, select_account, monkeypatch, expert_account):
    account_id, expert_id = expert_account
    _open_trade(account_id, 'AAPL', expert_id=expert_id, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {account_id: _Broker([_price('AAPL', 110.0)])})

    select_account(account_id)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerExpertWidget)

    assert f'Alpha-{expert_id}' in texts
    assert '$100.00' in texts


def test_the_per_expert_widget_ignores_unattributed_trades_even_across_all_accounts(
        nicegui_client, select_account, monkeypatch, expert_account):
    """With "All" selected there is no expert-id list to filter on at all.

    What keeps a hand-placed trade out of the per-expert view then is
    ``_get_extra_filters``' ``expert_id IS NOT NULL``. Without it the widget would
    have to invent an owner for a trade nobody's expert placed.
    """
    account_id, expert_id = expert_account
    _open_trade(account_id, 'AAPL', expert_id=expert_id, qty=10.0, open_price=100.0)
    _open_trade(account_id, 'MSFT', expert_id=None, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {account_id: _Broker([_price('AAPL', 110.0),
                                                    _price('MSFT', 105.0)])})
    warnings = _capture_warnings(monkeypatch, fpl_expert_mod)

    select_account(None)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerExpertWidget)

    assert f'Alpha-{expert_id}' in texts
    assert '$100.00' in texts       # only the expert's trade
    assert '$150.00' not in texts   # the manual one is not the expert's

    # ...and it is filtered IN SQL, not by failing to look the expert up. Dropping
    # the ``expert_id IS NOT NULL`` clause happens to render the same thing, because
    # ``session.get(ExpertInstance, None)`` finds nothing -- but it turns every
    # hand-placed trade into a 'expert not found in database' WARNING, which is the
    # message a genuinely dangling expert reference needs to be visible in.
    assert not [w for w in warnings if 'expert not found' in w]


def test_the_per_expert_widget_does_not_show_another_accounts_expert_under_a_manual_account(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    """An empty expert list must mean NO ROWS -- never "then show me everything".

    This is the trap the account-level fix must not fall into: the moment ``[]``
    widens the query, the manual account's card fills up with the OTHER account's
    experts, and the numbers look plausible.
    """
    other_account, expert_id = expert_account
    _open_trade(manual_account, 'AAPL', qty=10.0, open_price=100.0)
    _open_trade(other_account, 'MSFT', expert_id=expert_id, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {
        manual_account: _Broker([_price('AAPL', 110.0)]),
        other_account: _Broker([_price('MSFT', 105.0)]),
    })

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerExpertWidget)

    assert texts == ['No open positions']


def test_the_per_expert_widget_shows_nothing_for_an_account_whose_expert_lives_elsewhere(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    """A trade booked on THIS account but owned by ANOTHER account's expert.

    Real shape: an ExpertInstance's ``account_id`` was changed after it had already
    traded. The selected account still has no experts of its own, so the per-expert
    card has nothing to say -- and must not answer by falling back to "everything
    booked on this account", which would put another account's expert in the list
    and duplicate the per-ACCOUNT widget besides.
    """
    other_account, expert_id = expert_account
    txn = create_transaction(symbol='AAPL', quantity=10.0, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=100.0,
                             expert_id=expert_id)
    create_trading_order(account_id=manual_account, symbol='AAPL', quantity=10.0,
                         side=OrderDirection.BUY, order_type=OrderType.MARKET,
                         status=OrderStatus.FILLED, transaction_id=txn.id,
                         filled_qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {manual_account: _Broker([_price('AAPL', 110.0)])})

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerExpertWidget)

    assert texts == ['No open positions']
    assert f'Alpha-{expert_id}' not in texts


def test_the_per_account_widget_does_show_that_same_trade(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    """The counterpart of the test above -- and the whole point of the split.

    Same row, same account: invisible to the per-EXPERT card (no expert of this
    account's owns it) and fully visible to the per-ACCOUNT card (the money is
    this account's). The local transaction below only proves the account is
    picked up; its own cost basis is not what prices the row -- ``unrealized_pl``
    on the broker position is (``_rows_for_manual_account``).
    """
    _, expert_id = expert_account
    txn = create_transaction(symbol='AAPL', quantity=10.0, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=100.0,
                             expert_id=expert_id)
    create_trading_order(account_id=manual_account, symbol='AAPL', quantity=10.0,
                         side=OrderDirection.BUY, order_type=OrderType.MARKET,
                         status=OrderStatus.FILLED, transaction_id=txn.id,
                         filled_qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {manual_account: _Broker(
        [_price('AAPL', 110.0, unrealized_pl=100.0)])})

    select_account(manual_account)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerAccountWidget)

    assert 'Manual' in texts
    assert '$100.00' in texts


def test_the_per_expert_widget_never_shows_another_accounts_experts(
        nicegui_client, select_account, monkeypatch, expert_account):
    """``get_expert_ids_for_account`` losing its ``account_id`` filter looks like this."""
    account_id, expert_id = expert_account
    other_account = create_account_definition(name='Second', provider='Alpaca').id
    other_expert = create_expert_instance(account_id=other_account, alias='Beta').id
    _open_trade(account_id, 'AAPL', expert_id=expert_id, qty=10.0, open_price=100.0)
    _open_trade(other_account, 'MSFT', expert_id=other_expert, qty=10.0, open_price=100.0)
    _use_brokers(monkeypatch, {
        account_id: _Broker([_price('AAPL', 110.0)]),
        other_account: _Broker([_price('MSFT', 105.0)]),
    })

    select_account(account_id)
    texts = _render_floating_pl(nicegui_client, FloatingPLPerExpertWidget)

    assert f'Alpha-{expert_id}' in texts
    assert f'Beta-{other_expert}' not in texts


# ===========================================================================
# 📊 Position Distribution by Labels / Categories
# ===========================================================================

def _instrument(symbol, labels=None, categories=None):
    add_instance(Instrument(name=symbol, labels=labels or [],
                            categories=categories or []),
                 expunge_after_flush=True)


def _render_distribution(client, grouping_field, brokers, monkeypatch):
    monkeypatch.setattr(overview, 'get_account_instance_from_id',
                        lambda account_id: brokers.get(account_id))
    tab = overview.OverviewTab.__new__(overview.OverviewTab)
    from nicegui import ui

    holder = {}

    def _factory():
        with ui.column() as root:
            holder['root'] = root
            loading = ui.label('🔄 Loading positions...')
            chart = ui.column()
        return tab._load_position_distribution_async(loading, chart, grouping_field)

    async def _run():
        with client:
            await _factory()

    asyncio.run(_run())
    return holder['root']


def _position(symbol, market_value):
    return {'symbol': symbol, 'market_value': market_value, 'qty': 1}


@pytest.mark.parametrize('grouping_field,expected', [
    ('labels', 'Growth'),
    ('categories', 'Tech'),
])
def test_position_distribution_shows_positions_for_an_account_with_no_experts(
        nicegui_client, select_account, monkeypatch, manual_account,
        grouping_field, expected):
    _instrument('AAPL', labels=['Growth'], categories=['Tech'])
    select_account(manual_account)

    root = _render_distribution(
        nicegui_client, grouping_field,
        {manual_account: _Broker([_position('AAPL', 1000.0)])}, monkeypatch)
    texts = _texts(root)

    assert 'No open positions found.' not in texts
    assert 'Total Market Value: $1,000.00' in texts
    assert [r['category'] for r in _table_rows(root)] == [expected]


def test_position_distribution_for_an_account_with_experts_still_works(
        nicegui_client, select_account, monkeypatch, expert_account):
    account_id, _ = expert_account
    _instrument('MSFT', labels=['Value'])
    select_account(account_id)

    root = _render_distribution(
        nicegui_client, 'labels',
        {account_id: _Broker([_position('MSFT', 500.0)])}, monkeypatch)

    assert 'Total Market Value: $500.00' in _texts(root)
    assert [r['category'] for r in _table_rows(root)] == ['Value']


def test_position_distribution_never_shows_another_accounts_positions(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    other_account, _ = expert_account
    _instrument('AAPL', labels=['Growth'])
    _instrument('MSFT', labels=['Value'])
    brokers = {
        manual_account: _Broker([_position('AAPL', 1000.0)]),
        other_account: _Broker([_position('MSFT', 500.0)]),
    }

    select_account(manual_account)
    root = _render_distribution(nicegui_client, 'labels', brokers, monkeypatch)
    assert [r['category'] for r in _table_rows(root)] == ['Growth']

    select_account(other_account)
    root = _render_distribution(nicegui_client, 'labels', brokers, monkeypatch)
    assert [r['category'] for r in _table_rows(root)] == ['Value']


def test_position_distribution_on_a_genuinely_empty_account_says_so(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    other_account, _ = expert_account
    _instrument('MSFT', labels=['Value'])
    select_account(manual_account)

    root = _render_distribution(nicegui_client, 'labels', {
        manual_account: _Broker([]),
        other_account: _Broker([_position('MSFT', 500.0)]),
    }, monkeypatch)
    texts = _texts(root)

    assert 'No open positions found.' in texts
    assert '🔄 Loading positions...' not in texts
    assert 'Total Market Value: $0.00' not in texts


def test_position_distribution_with_all_accounts_selected_shows_both(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    other_account, _ = expert_account
    _instrument('AAPL', labels=['Growth'])
    _instrument('MSFT', labels=['Value'])
    select_account(None)

    root = _render_distribution(nicegui_client, 'labels', {
        manual_account: _Broker([_position('AAPL', 1000.0)]),
        other_account: _Broker([_position('MSFT', 500.0)]),
    }, monkeypatch)

    assert sorted(r['category'] for r in _table_rows(root)) == ['Growth', 'Value']
    assert 'Total Market Value: $1,500.00' in _texts(root)


def test_position_distribution_reports_a_fetch_failure_instead_of_an_empty_chart(
        nicegui_client, select_account, monkeypatch, manual_account):
    """``get_positions()`` returns ``None`` on a FETCH FAILURE -- never ``[]``.

    Rendering 'No open positions found.' for a broker outage tells the user their
    account is flat. It is the display half of the 2026-07-03 tri-state incident.
    """
    select_account(manual_account)
    errors = _capture_errors(monkeypatch, overview)

    root = _render_distribution(nicegui_client, 'labels',
                                {manual_account: _Broker(None)}, monkeypatch)
    texts = _texts(root)

    assert 'No open positions found.' not in texts
    assert any('Could not load positions' in t for t in texts)
    assert errors      # and it was logged, not swallowed


def test_position_distribution_reports_an_account_it_could_not_even_instantiate(
        nicegui_client, select_account, monkeypatch, manual_account):
    """No broker object at all is the same class of failure as ``None`` positions.

    Skipping it silently produced the identical lie -- an empty pie for an account
    whose book was never read.

    The log line is asserted on because it is the ONLY thing that distinguishes
    this from letting ``None.get_positions()`` throw into the generic handler: both
    reach the user's warning banner, but 'NoneType has no attribute get_positions'
    sends whoever reads the log hunting for a broker outage that never happened.
    """
    select_account(manual_account)
    errors = _capture_errors(monkeypatch, overview)

    root = _render_distribution(nicegui_client, 'labels', {}, monkeypatch)
    texts = _texts(root)

    assert 'No open positions found.' not in texts
    assert any('Could not load positions' in t for t in texts)
    assert any('Could not build an account instance' in e for e in errors), errors


def test_position_distribution_still_charts_the_accounts_that_did_answer(
        nicegui_client, select_account, monkeypatch, manual_account, expert_account):
    """One broker down must not blank out the other's real positions."""
    other_account, _ = expert_account
    _instrument('MSFT', labels=['Value'])
    select_account(None)
    _capture_errors(monkeypatch, overview)

    root = _render_distribution(nicegui_client, 'labels', {
        manual_account: _Broker(None),
        other_account: _Broker([_position('MSFT', 500.0)]),
    }, monkeypatch)
    texts = _texts(root)

    assert any('Could not load positions' in t for t in texts)
    assert 'Total Market Value: $500.00' in texts
    assert [r['category'] for r in _table_rows(root)] == ['Value']
