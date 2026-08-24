"""A manual submit from the UI must carry the protective stop, or refuse to submit.

THE BUG, as reported from production. A funded WSC entry failed to reach the broker,
so the user re-submitted it by hand from the Account Overview pending-orders table.
What the broker ended up holding was::

    465  WSC  BUY   MARKET      2.0              FILLED   <- the entry
    466  WSC  SELL  SELL_LIMIT  2.0  tp 22.9959  NEW      <- a take-profit
                                                          <- and NO stop

The risk manager HAD computed the safeguard stop ("safeguard SL for WSC: $19.01") and
stamped it onto ``order.stop_price``. The manual submit called
``provider_obj.submit_order(order)`` with no ``sl_price=``, so the protective leg was
never created and the user held an unprotected position.

The automated path does not have this defect -- ``TradeManager`` submits with
``sl_price=fo.stop_price`` -- and neither does the wash-trade unlock path, which
re-threads the very same value (``TradeManager._check_all_washtrade_locked_orders``:
"for a MARKET entry, order.stop_price carries the risk manager's safeguard SL"). Only
the hand-driven UI buttons dropped it.

The policy these tests pin is the one ``AccountInterface.submit_order`` already
established for ``supports_protective_legs`` (see
``tests/test_protective_leg_capability.py``): *either the stop exists, or nothing was
opened*. No second policy is invented here -- when the broker cannot place legs at all
the interface's own gate is what fires, and the UI merely has to show it.

Three notes on how these run:

1. IMPORTING THE PAGES. ``ui/pages/__init__.py`` pulls every page and through them the
   expert/LLM stack -- several seconds, once, for this whole module. Paid deliberately:
   the point is to test the pages.
2. RENDERING. A bare ``nicegui.Client`` gives every ``ui.*`` call a slot stack, so the
   handlers can draw and notify without a browser. ``nicegui.testing`` is used nowhere
   in this suite and is not introduced here.
3. NEVER ``caplog``: ``logger.py`` sets ``propagate = False``, so the root handler
   caplog installs never sees a record. Patch the module's own ``logger`` instead
   (``_capture_errors``).
"""
import datetime as _datetime_module
from datetime import datetime, timezone

import pytest

from ba2_trade_platform.core.db import get_all_instances, get_instance
from ba2_trade_platform.core.interfaces.AccountInterface import AccountInterface
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.ui.pages import marketanalysis as ma_page
from ba2_trade_platform.ui.pages import overview as page
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_expert_instance, create_recommendation,
    create_trading_order, create_transaction,
)

# A fixed instant, deliberately NOT "now": every timestamp these tests observe is this
# one, so nothing can pass only because it happens to be run today.
FROZEN_NOW = datetime(2026, 2, 17, 14, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Doubles and helpers
# ---------------------------------------------------------------------------

class _RecordingAccount(MockAccount):
    """Records what the UI actually asked the broker for.

    ``MockAccount.submit_order`` deliberately does NOT accept ``tp_price``/``sl_price``;
    this double takes the full ``AccountInterface.submit_order`` signature so a dropped
    stop shows up as ``sl_price=None`` rather than as a ``TypeError``.
    """

    supports_protective_legs = True
    # Class-level, because the risk-management handler constructs its OWN account
    # instance (``account_class(account_def.id)``) and the test never sees it.
    constructed = []

    def __init__(self, account_id):
        super().__init__(account_id)
        self.submissions = []
        _RecordingAccount.constructed.append(self)

    def submit_order(self, order, tp_price=None, sl_price=None, is_closing_order=False):
        self.submissions.append({
            'order_id': order.id,
            'symbol': order.symbol,
            'tp_price': tp_price,
            'sl_price': sl_price,
            'is_closing_order': is_closing_order,
        })
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        return order


class _NoProtectiveLegsAccount(MockAccount):
    """A broker that cannot attach protective legs at all (TastyTrade's shape).

    ``submit_order`` delegates to the REAL ``AccountInterface`` template method, because
    that is where the ``supports_protective_legs`` capability gate lives -- the same
    trick ``tests/test_protective_leg_capability.py`` and ``test_washtrade_lock.py`` use.
    """

    supports_protective_legs = False

    def __init__(self, account_id):
        super().__init__(account_id)
        self.impl_calls = []

    def submit_order(self, order, tp_price=None, sl_price=None, is_closing_order=False):
        return AccountInterface.submit_order(
            self, order, tp_price=tp_price, sl_price=sl_price,
            is_closing_order=is_closing_order)

    def _submit_order_impl(self, trading_order, tp_price=None, sl_price=None,
                           is_closing_order=False, use_complex_order=False):
        self.impl_calls.append({'tp': tp_price, 'sl': sl_price})
        return super()._submit_order_impl(
            trading_order, tp_price=tp_price, sl_price=sl_price,
            is_closing_order=is_closing_order, use_complex_order=use_complex_order)

    def adjust_sl(self, transaction, new_sl_price, source=""):
        raise NotImplementedError("TastyTrade cannot place a protective stop")

    def adjust_tp(self, transaction, new_tp_price, source=""):
        raise NotImplementedError("TastyTrade cannot place a take-profit")

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        raise NotImplementedError("TastyTrade cannot place protective legs")


def _use_account(monkeypatch, module, account):
    """Point a page module's account factory at ``account``.

    Both pages bind ``get_account_instance_from_id`` into their own namespace at import
    time, so patching ``core.utils`` alone would not be seen.
    """
    monkeypatch.setattr(module, 'get_account_instance_from_id',
                        lambda account_id: account)
    return account


def _capture_notifications(monkeypatch):
    """Collect ``ui.notify`` calls. Returns the growing list of (message, type)."""
    from nicegui import ui as nicegui_ui
    sent = []
    monkeypatch.setattr(nicegui_ui, 'notify',
                        lambda message, **kw: sent.append((str(message), kw.get('type'))))
    return sent


def _capture_errors(monkeypatch, module=page):
    """Collect ``logger.error`` messages from a page module. NOT caplog."""
    messages = []
    monkeypatch.setattr(module.logger, 'error',
                        lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def _messages(notifications, *types):
    return [m for m, t in notifications if t in types]


def _refusal_text(notifications):
    """Everything the user was warned about, joined -- refusals may arrive as
    ``negative`` (a single submit) or ``warning`` (a batch summary)."""
    return " | ".join(_messages(notifications, 'negative', 'warning'))


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Freeze both pages' ``datetime.now()`` at ``FROZEN_NOW``.

    ``marketanalysis._place_order`` stamps ``datetime.now()`` into the order comment; a
    test that reads it back must not depend on the wall clock.
    """
    class _FrozenDatetime(_datetime_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_NOW if tz else FROZEN_NOW.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return FROZEN_NOW.replace(tzinfo=None)

    monkeypatch.setattr(page, 'datetime', _FrozenDatetime)
    monkeypatch.setattr(ma_page, 'datetime', _FrozenDatetime)


@pytest.fixture(autouse=True)
def _reset_constructed_accounts():
    _RecordingAccount.constructed = []
    yield
    _RecordingAccount.constructed = []


@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-manual-order-submit'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _wsc_entry(account_id, stop_price=19.01, txn_kwargs=None,
               status=OrderStatus.PENDING, **kwargs):
    """The production shape: a funded, unsubmitted MARKET entry with a safeguard stop."""
    txn = create_transaction(symbol="WSC", quantity=2.0, side=OrderDirection.BUY,
                             status=TransactionStatus.WAITING, open_price=21.90,
                             **(txn_kwargs or {}))
    return create_trading_order(
        account_id=account_id, symbol="WSC", quantity=2.0,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=status, transaction_id=txn.id,
        stop_price=stop_price, created_at=FROZEN_NOW, **kwargs)


def _overview_tab():
    """An ``OverviewTab`` without its (expensive, irrelevant) render."""
    return page.OverviewTab.__new__(page.OverviewTab)


def _recommendations_tab():
    """An ``OrderRecommendationsTab`` without its render; ``refresh_data`` stubbed."""
    tab = ma_page.OrderRecommendationsTab.__new__(ma_page.OrderRecommendationsTab)
    tab.refresh_data = lambda: None
    return tab


class _Dialog:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# 1. The headline: the pending-orders "Submit" button (overview.py:2005)
# ---------------------------------------------------------------------------

class TestPendingOrderSubmitButton:

    def test_the_submit_button_carries_the_safeguard_stop_to_the_broker(
            self, monkeypatch, nicegui_client):
        """THE BUG. Clicking Submit on order 465 must ask for the $19.01 stop.

        Before the fix the account was handed ``sl_price=None`` and the UI reported
        success, which is exactly how the live WSC position ended up with a take-profit
        and no stop.
        """
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        order = _wsc_entry(acct_def.id)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(order)

        assert len(acct.submissions) == 1, (
            f"expected exactly one submission, got {acct.submissions}")
        assert acct.submissions[0]['sl_price'] == 19.01, (
            f"the entry reached the broker with sl_price="
            f"{acct.submissions[0]['sl_price']!r} -- an unprotected position. "
            f"notifications={notifications}")

    def test_submit_is_refused_when_no_stop_can_be_derived(
            self, monkeypatch, nicegui_client):
        """No stop anywhere -> nothing is sent, and the user is told why."""
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        order = _wsc_entry(acct_def.id, stop_price=None)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(order)

        assert acct.submissions == [], "an unprotected entry reached the broker"
        assert _messages(notifications, 'positive') == [], (
            "the UI reported success for an order it never submitted")
        refusal = _refusal_text(notifications)
        assert 'WSC' in refusal and 'stop' in refusal.lower(), (
            f"the refusal must name the symbol and the missing stop; got {notifications}")

    def test_a_zero_stop_price_is_not_a_stop(self, monkeypatch, nicegui_client):
        """``stop_price = 0.0`` means "no stop", not "a stop at zero".

        A stop at $0 can never trigger; submitting it would be the original defect with
        one extra step.
        """
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        order = _wsc_entry(acct_def.id, stop_price=0.0)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(order)

        assert acct.submissions == [], (
            f"a zero stop was treated as protection: {acct.submissions}")
        assert _messages(notifications, 'positive') == []

    def test_a_negative_stop_price_is_not_a_stop(self, monkeypatch, nicegui_client):
        """Same for a negative price -- an impossible stop is not protection."""
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        order = _wsc_entry(acct_def.id, stop_price=-5.0)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(order)

        assert acct.submissions == []
        assert _messages(notifications, 'positive') == []

    def test_the_stop_falls_back_to_the_transactions_stop_loss(
            self, monkeypatch, nicegui_client):
        """When the order row carries no stop, the transaction's SL is the next source."""
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        _capture_notifications(monkeypatch)
        order = _wsc_entry(acct_def.id, stop_price=None,
                           txn_kwargs={'stop_loss': 18.50})

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(order)

        assert len(acct.submissions) == 1
        assert acct.submissions[0]['sl_price'] == 18.50

    def test_a_known_take_profit_is_carried_too(self, monkeypatch, nicegui_client):
        """"...and the take-profit where one is known." The transaction's TP rides along."""
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        _capture_notifications(monkeypatch)
        order = _wsc_entry(acct_def.id, txn_kwargs={'take_profit': 22.9959})

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(order)

        assert acct.submissions[0]['sl_price'] == 19.01
        assert acct.submissions[0]['tp_price'] == 22.9959

    def test_an_existing_take_profit_leg_is_not_duplicated(
            self, monkeypatch, nicegui_client):
        """The WSC shape exactly: a TP leg already exists (order 466).

        Passing ``tp_price`` as well would ask the broker for a SECOND take-profit
        against the same position.
        """
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        _capture_notifications(monkeypatch)
        order = _wsc_entry(acct_def.id, txn_kwargs={'take_profit': 22.9959})
        create_trading_order(
            account_id=acct_def.id, symbol="WSC", quantity=2.0,
            side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
            status=OrderStatus.NEW, transaction_id=order.transaction_id,
            depends_on_order=order.id, limit_price=22.9959, created_at=FROZEN_NOW)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(order)

        assert acct.submissions[0]['sl_price'] == 19.01
        assert acct.submissions[0]['tp_price'] is None, (
            "a second take-profit was requested on top of the live one")

    def test_an_entry_already_covered_by_a_live_stop_leg_is_submitted_unchanged(
            self, monkeypatch, nicegui_client):
        """THE INVERSE DEFECT. A protected entry must not be blocked.

        Here the ruleset already staged a WAITING_TRIGGER protective SELL_STOP against
        this entry, so the order row itself carries no ``stop_price``. Refusing it -- or
        adding a second stop -- would both be wrong.
        """
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        order = _wsc_entry(acct_def.id, stop_price=None)
        create_trading_order(
            account_id=acct_def.id, symbol="WSC", quantity=2.0,
            side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
            status=OrderStatus.WAITING_TRIGGER, transaction_id=order.transaction_id,
            depends_on_order=order.id, stop_price=19.01, created_at=FROZEN_NOW)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(order)

        assert len(acct.submissions) == 1, (
            f"a legitimately protected entry was blocked: {notifications}")
        assert acct.submissions[0]['sl_price'] is None, (
            "a duplicate stop was requested on top of the staged leg")

    def test_a_top_up_into_a_position_that_already_has_a_stop_is_allowed(
            self, monkeypatch, nicegui_client):
        """THE INVERSE DEFECT, second shape: the live stop hangs off the FIRST entry.

        Adding to an open position creates a second same-side entry whose protective leg
        does not exist yet and never will -- the position's OCO already covers the whole
        holding. The cover has to be found by TRANSACTION, not only by
        ``depends_on_order``.
        """
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        txn = create_transaction(symbol="WSC", quantity=2.0, side=OrderDirection.BUY,
                                 status=TransactionStatus.OPENED, open_price=21.90)
        first_entry = create_trading_order(
            account_id=acct_def.id, symbol="WSC", quantity=2.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, transaction_id=txn.id, created_at=FROZEN_NOW)
        create_trading_order(
            account_id=acct_def.id, symbol="WSC", quantity=2.0,
            side=OrderDirection.SELL, order_type=OrderType.OCO,
            status=OrderStatus.NEW, transaction_id=txn.id,
            depends_on_order=first_entry.id, stop_price=19.01, limit_price=22.9959,
            created_at=FROZEN_NOW)
        top_up = create_trading_order(
            account_id=acct_def.id, symbol="WSC", quantity=1.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=txn.id, created_at=FROZEN_NOW)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(top_up)

        assert len(acct.submissions) == 1, (
            f"a top-up into an already-stopped position was blocked: {notifications}")
        assert acct.submissions[0]['sl_price'] is None

    def test_a_cancelled_stop_leg_does_not_count_as_protection(
            self, monkeypatch, nicegui_client):
        """A dead leg protects nothing -- the same failure mode as the zero-quantity
        TP legs the broker silently cancelled."""
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        order = _wsc_entry(acct_def.id, stop_price=None)
        create_trading_order(
            account_id=acct_def.id, symbol="WSC", quantity=2.0,
            side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
            status=OrderStatus.CANCELED, transaction_id=order.transaction_id,
            depends_on_order=order.id, stop_price=19.01, created_at=FROZEN_NOW)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(order)

        assert acct.submissions == []
        assert _messages(notifications, 'positive') == []

    def test_a_stop_leg_with_no_stop_price_does_not_count_as_protection(
            self, monkeypatch, nicegui_client):
        """A SELL_STOP with no price on it is a shape, not a stop.

        The same failure mode as the zero-QUANTITY take-profits this codebase already
        refuses ("a zero-quantity TP/SL is cancelled by the broker and leaves the position
        unprotected"): the order list *looks* covered, and nothing is.
        """
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        order = _wsc_entry(acct_def.id, stop_price=None)
        create_trading_order(
            account_id=acct_def.id, symbol="WSC", quantity=2.0,
            side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
            status=OrderStatus.WAITING_TRIGGER, transaction_id=order.transaction_id,
            depends_on_order=order.id, stop_price=None, created_at=FROZEN_NOW)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(order)

        assert acct.submissions == [], (
            "a priceless stop leg was accepted as protection")
        assert _messages(notifications, 'positive') == []

    def test_a_closing_order_is_not_required_to_carry_a_stop(
            self, monkeypatch, nicegui_client):
        """THE INVERSE DEFECT. Closing a long does not open exposure to protect."""
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        txn = create_transaction(symbol="WSC", quantity=2.0, side=OrderDirection.BUY,
                                 status=TransactionStatus.OPENED, open_price=21.90)
        closing = create_trading_order(
            account_id=acct_def.id, symbol="WSC", quantity=2.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=txn.id, created_at=FROZEN_NOW)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(closing)

        assert len(acct.submissions) == 1, (
            f"a position CLOSE was blocked for having no stop: {notifications}")
        assert acct.submissions[0]['sl_price'] is None

    def test_a_dependent_protective_leg_is_not_required_to_carry_a_stop(
            self, monkeypatch, nicegui_client):
        """THE INVERSE DEFECT. The leg IS the protection; it needs none of its own."""
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        entry = _wsc_entry(acct_def.id)
        leg = create_trading_order(
            account_id=acct_def.id, symbol="WSC", quantity=2.0,
            side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
            status=OrderStatus.PENDING, transaction_id=entry.transaction_id,
            depends_on_order=entry.id, stop_price=19.01, created_at=FROZEN_NOW)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(leg)

        assert len(acct.submissions) == 1, (
            f"a protective leg was blocked for having no protective leg: {notifications}")
        assert acct.submissions[0]['sl_price'] is None

    def test_an_entry_trigger_stop_price_is_not_mistaken_for_a_protective_stop(
            self, monkeypatch, nicegui_client):
        """On a BUY_STOP, ``stop_price`` is the ENTRY TRIGGER, not a stop-loss.

        Passing it as ``sl_price`` would place a "protective" stop at, or above, the
        entry -- an instant stop-out dressed up as protection. This is the same
        restriction ``TradeManager._check_all_washtrade_locked_orders`` states in
        prose ("on stop/stop-limit types stop_price is the entry TRIGGER").
        """
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        txn = create_transaction(symbol="WSC", quantity=2.0, side=OrderDirection.BUY,
                                 status=TransactionStatus.WAITING, open_price=21.90)
        breakout = create_trading_order(
            account_id=acct_def.id, symbol="WSC", quantity=2.0,
            side=OrderDirection.BUY, order_type=OrderType.BUY_STOP,
            status=OrderStatus.PENDING, transaction_id=txn.id,
            stop_price=25.00, created_at=FROZEN_NOW)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(breakout)

        assert acct.submissions == [], (
            f"the entry trigger was submitted as a stop-loss: {acct.submissions}")
        assert _messages(notifications, 'positive') == []
        assert 'WSC' in _refusal_text(notifications), notifications

    def test_broker_without_protective_legs_refuses_per_the_existing_contract(
            self, monkeypatch, nicegui_client):
        """Requirement 3: no second policy for the same question.

        ``AccountInterface.submit_order`` already refuses to open a position it cannot
        protect. The UI must let that refusal happen and show it -- not swallow it, and
        not report success.
        """
        acct_def = create_account_definition(provider="TastyTrade")
        acct = _use_account(monkeypatch, page, _NoProtectiveLegsAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        errors = _capture_errors(monkeypatch)
        order = _wsc_entry(acct_def.id)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(order)

        assert acct.impl_calls == [], (
            "an unprotectable entry was sent to the broker anyway")
        assert _messages(notifications, 'positive') == [], (
            "the UI reported success for a refused submission")
        assert 'supports_protective_legs' in (_refusal_text(notifications) + " ".join(errors)), (
            f"the capability refusal never reached the user: {notifications} {errors}")
        assert get_instance(TradingOrder, order.id).status != OrderStatus.FILLED


# ---------------------------------------------------------------------------
# 2. "Retry Selected Orders" (overview.py:2098)
# ---------------------------------------------------------------------------

class TestRetrySelectedOrders:

    def test_retry_carries_the_safeguard_stop(self, monkeypatch, nicegui_client):
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        _capture_notifications(monkeypatch)
        order = _wsc_entry(acct_def.id, status=OrderStatus.ERROR)

        with nicegui_client:
            page.AccountOverviewTab()._confirm_retry_orders([order.id], _Dialog())

        assert len(acct.submissions) == 1
        assert acct.submissions[0]['sl_price'] == 19.01

    def test_retry_is_refused_when_no_stop_can_be_derived(
            self, monkeypatch, nicegui_client):
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        order = _wsc_entry(acct_def.id, stop_price=None, status=OrderStatus.ERROR)

        with nicegui_client:
            page.AccountOverviewTab()._confirm_retry_orders([order.id], _Dialog())

        assert acct.submissions == [], "an unprotected entry was retried to the broker"
        assert _messages(notifications, 'positive') == [], (
            "the batch reported a successful retry it never made")
        assert 'stop' in _refusal_text(notifications).lower(), notifications


# ---------------------------------------------------------------------------
# 3. "Run Risk Management" auto-submit (overview.py:1171)
# ---------------------------------------------------------------------------

class TestRiskManagementAutoSubmit:
    """``OverviewTab._auto_submit_after_risk_management`` -- the loop that used to sit
    inline at ``overview.py:1171`` and called ``account.submit_order(order)`` bare.

    Tested through the extracted method rather than through
    ``_handle_risk_management_from_overview``, because that outer handler currently
    cannot reach the loop at all: it shadows ``get_instance`` with a function-local
    ``from ...core.db import get_instance`` inside its ``smart`` branch, so the classic
    branch's ``get_instance(AccountDefinition, ...)`` raises ``UnboundLocalError`` and the
    whole auto-submit block is swallowed by its ``except``. That is a separate bug, left
    alone deliberately -- "fixing" it would silently switch bulk auto-submission ON. The
    gate is put in place now so that whenever it IS switched on, it cannot open a naked
    position.
    """

    def _order(self, stop_price):
        acct_def = create_account_definition(provider="Alpaca")
        expert = create_expert_instance(account_id=acct_def.id)
        rec = create_recommendation(instance_id=expert.id, symbol="WSC")
        return acct_def, _wsc_entry(acct_def.id, stop_price=stop_price,
                                    expert_recommendation_id=rec.id)

    def test_auto_submit_carries_the_safeguard_stop(self, monkeypatch, nicegui_client):
        acct_def, order = self._order(stop_price=19.01)
        notifications = _capture_notifications(monkeypatch)
        acct = _RecordingAccount(acct_def.id)

        with nicegui_client:
            submitted = _overview_tab()._auto_submit_after_risk_management(acct, [order])

        assert _refusal_text(notifications) == ""
        assert submitted == 1
        assert acct.submissions[0]['sl_price'] == 19.01

    def test_auto_submit_is_refused_and_the_user_is_told(
            self, monkeypatch, nicegui_client):
        """A refusal nobody renders is the original defect with the volume turned down,
        so the notification is part of this unit, not of its caller."""
        acct_def, order = self._order(stop_price=None)
        notifications = _capture_notifications(monkeypatch)
        acct = _RecordingAccount(acct_def.id)

        with nicegui_client:
            submitted = _overview_tab()._auto_submit_after_risk_management(acct, [order])

        assert acct.submissions == [], "an unprotected entry was auto-submitted"
        assert submitted == 0
        assert 'WSC' in _refusal_text(notifications), notifications

    def test_one_refused_order_does_not_stop_the_protected_ones(
            self, monkeypatch, nicegui_client):
        """A batch must not be all-or-nothing in either direction."""
        acct_def, naked = self._order(stop_price=None)
        protected = _wsc_entry(acct_def.id, stop_price=19.01)
        protected.symbol = "GNTX"
        notifications = _capture_notifications(monkeypatch)
        acct = _RecordingAccount(acct_def.id)

        with nicegui_client:
            submitted = _overview_tab()._auto_submit_after_risk_management(
                acct, [naked, protected])

        assert submitted == 1
        assert [s['sl_price'] for s in acct.submissions] == [19.01]
        assert 'WSC' in _refusal_text(notifications)


# ---------------------------------------------------------------------------
# 4. Trade Recommendations -> submit the EXISTING pending order
#    (marketanalysis.py:4161)
# ---------------------------------------------------------------------------

class TestRecommendationExistingPendingOrder:

    def _setup(self, monkeypatch, stop_price):
        acct_def = create_account_definition(provider="Alpaca")
        expert = create_expert_instance(account_id=acct_def.id)
        rec = create_recommendation(instance_id=expert.id, symbol="WSC")
        order = _wsc_entry(acct_def.id, stop_price=stop_price,
                           expert_recommendation_id=rec.id)
        acct = _use_account(monkeypatch, ma_page, _RecordingAccount(acct_def.id))
        return rec, order, acct

    def test_submitting_the_existing_pending_order_carries_the_stop(
            self, monkeypatch, nicegui_client):
        rec, order, acct = self._setup(monkeypatch, stop_price=19.01)
        _capture_notifications(monkeypatch)

        with nicegui_client:
            _recommendations_tab()._handle_place_order_recommendation(rec.id)

        assert len(acct.submissions) == 1
        assert acct.submissions[0]['sl_price'] == 19.01

    def test_submitting_the_existing_pending_order_is_refused_without_a_stop(
            self, monkeypatch, nicegui_client):
        rec, order, acct = self._setup(monkeypatch, stop_price=None)
        notifications = _capture_notifications(monkeypatch)

        with nicegui_client:
            _recommendations_tab()._handle_place_order_recommendation(rec.id)

        assert acct.submissions == []
        assert _messages(notifications, 'positive') == []
        assert 'stop' in _refusal_text(notifications).lower(), notifications


# ---------------------------------------------------------------------------
# 5. Trade Recommendations -> "Place Order" dialog (marketanalysis.py:4372)
# ---------------------------------------------------------------------------

class TestPlaceOrderDialog:

    def _wire(self, monkeypatch):
        import ba2_trade_platform.core.utils as core_utils
        acct_def = create_account_definition(provider="Alpaca")
        monkeypatch.setattr(core_utils, 'get_account_id_for_recommendation',
                            lambda rec_id: acct_def.id)
        acct = _use_account(monkeypatch, ma_page, _RecordingAccount(acct_def.id))
        return acct_def, acct

    def test_the_user_supplied_stop_is_passed_through(self, monkeypatch, nicegui_client):
        """REGRESSION GUARD: the A4 bracket this dialog already supports still works."""
        acct_def, acct = self._wire(monkeypatch)
        _capture_notifications(monkeypatch)
        dialog = _Dialog()

        with nicegui_client:
            _recommendations_tab()._place_order(
                symbol="WSC", side="buy", quantity=2.0, order_type=OrderType.MARKET,
                limit_price=None, dialog=dialog, sl_price=19.01, tp_price=22.9959)

        assert len(acct.submissions) == 1
        assert acct.submissions[0]['sl_price'] == 19.01
        assert acct.submissions[0]['tp_price'] == 22.9959

    def test_a_blank_stop_is_refused_and_no_order_row_is_created(
            self, monkeypatch, nicegui_client):
        """The dialog's Stop-Loss field was "optional"; leaving it blank opened a naked
        position. Nothing is submitted AND nothing is persisted."""
        acct_def, acct = self._wire(monkeypatch)
        notifications = _capture_notifications(monkeypatch)
        dialog = _Dialog()

        with nicegui_client:
            _recommendations_tab()._place_order(
                symbol="WSC", side="buy", quantity=2.0, order_type=OrderType.MARKET,
                limit_price=None, dialog=dialog, sl_price=None, tp_price=None)

        assert acct.submissions == []
        assert _messages(notifications, 'positive') == []
        assert 'stop' in _refusal_text(notifications).lower(), notifications
        assert get_all_instances(TradingOrder) == [], (
            "a naked order row was left behind after the refusal")

    def test_a_zero_stop_from_the_dialog_is_refused(self, monkeypatch, nicegui_client):
        """The number input's default is ``0``, not blank -- it must not read as a stop."""
        acct_def, acct = self._wire(monkeypatch)
        notifications = _capture_notifications(monkeypatch)

        with nicegui_client:
            _recommendations_tab()._place_order(
                symbol="WSC", side="buy", quantity=2.0, order_type=OrderType.MARKET,
                limit_price=None, dialog=_Dialog(), sl_price=0.0, tp_price=0.0)

        assert acct.submissions == []
        assert _messages(notifications, 'positive') == []


# ---------------------------------------------------------------------------
# 6. The deliberate exemption
# ---------------------------------------------------------------------------

class TestOptionOrdersAreExempt:

    def test_an_option_order_is_not_refused_for_lacking_a_stop(
            self, monkeypatch, nicegui_client):
        """DOCUMENTED EXEMPTION, not an oversight.

        This platform never places protective stop legs on option contracts: option
        exits are managed by ``option_lifecycle_service`` (time/greeks), and
        ``adjust_sl`` on an option transaction would place a stop on the UNDERLYING.
        Refusing every manual option submit would block a working feature while
        protecting nothing. Pinned here so the exemption stays a decision.
        """
        acct_def = create_account_definition(provider="Alpaca")
        acct = _use_account(monkeypatch, page, _RecordingAccount(acct_def.id))
        notifications = _capture_notifications(monkeypatch)
        txn = create_transaction(symbol="WSC", quantity=1.0, side=OrderDirection.BUY,
                                 status=TransactionStatus.WAITING, open_price=1.20,
                                 asset_class=AssetClass.OPTION)
        order = create_trading_order(
            account_id=acct_def.id, symbol="WSC", quantity=1.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=txn.id,
            asset_class=AssetClass.OPTION, contract_symbol="WSC260220C00025000",
            created_at=FROZEN_NOW)

        with nicegui_client:
            page.AccountOverviewTab()._submit_order_to_broker(order)

        assert len(acct.submissions) == 1, (
            f"the option submit was blocked: {notifications}")
        assert acct.submissions[0]['sl_price'] is None
