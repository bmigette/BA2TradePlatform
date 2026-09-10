"""The live account refresh force-closes a position whose stop was breached but never filled.

WHY THIS EXISTS. Alpaca exits go out as an OCO whose stop leg is a stop-LIMIT: the limit
sits ``OCO_STOP_LIMIT_CUSHION`` (0.5%) *through* the stop so a normal trigger still fills.
On a GAP that jumps the stop AND the cushion in one print, the triggered leg becomes a
limit sell above the market: it rests unfilled and the position is left open with no stop
under it (prod 2026-09-10: RARE/ENOV). The backtest models every stop as a MARKET stop
that fills at the stop or at the gap open
(``backtest_account.py::_gap_stop_fill``), so live and simulated diverge exactly on the
days that matter most.

``TradeManager._force_close_breached_stops`` is the reconciliation: on each periodic
account refresh, any OPENED equity transaction whose price is beyond its stop by more
than the SAME cushion the OCO leg used is force-closed at market. One constant, two
readers — the check and the leg cannot drift apart.

Log assertions record ``TradeManager.logger`` directly: ``ba2_common.logger`` sets
``propagate = False``, so ``caplog`` sees nothing and every log pin would pass vacuously.
"""
import pytest
from unittest.mock import MagicMock

from ba2_trade_platform.core.TradeManager import TradeManager, OCO_STOP_LIMIT_CUSHION
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from tests.factories import (
    create_account_definition, create_trading_order, create_transaction,
)


pytestmark = pytest.mark.usefixtures("reset_test_db")

STOP = 100.0
# A long is breached only BELOW this; a short only ABOVE its mirror.
LONG_TRIGGER = STOP * (1.0 - OCO_STOP_LIMIT_CUSHION)   # 99.50
SHORT_TRIGGER = STOP * (1.0 + OCO_STOP_LIMIT_CUSHION)  # 100.50


class _LogRecorder:
    """Records what the manager logged, per level."""

    def __init__(self):
        self.warning_msgs = []
        self.error_msgs = []
        self.info_msgs = []

    def warning(self, msg, *a, **k):
        self.warning_msgs.append(str(msg))

    def error(self, msg, *a, **k):
        self.error_msgs.append(str(msg))

    def info(self, msg, *a, **k):
        self.info_msgs.append(str(msg))

    def debug(self, msg, *a, **k):
        pass


class _FakeAccount:
    """A live-shaped account double that records every call the safety net makes."""

    def __init__(self, account_id, prices, positions, raise_on_close=()):
        self.id = account_id
        self._prices = prices          # symbol -> price (None == unavailable)
        self._positions = positions    # symbol -> signed qty (None == unreadable book)
        self._raise_on_close = set(raise_on_close)
        self.price_calls = []
        self.position_calls = []
        self.closed = []

    def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
        self.price_calls.append(symbol_or_symbols)
        if isinstance(symbol_or_symbols, str):
            return self._prices[symbol_or_symbols]
        return {s: self._prices[s] for s in symbol_or_symbols}

    def get_signed_position_quantity(self, symbol):
        self.position_calls.append(symbol)
        return self._positions[symbol]

    def close_transaction(self, transaction_id):
        if transaction_id in self._raise_on_close:
            raise RuntimeError("broker refused the cancel")
        self.closed.append(transaction_id)
        return {
            'success': True,
            'message': 'closing',
            'canceled_count': 1,
            'deleted_count': 0,
            'close_order_id': 4242,
        }


def _position(account, symbol, side=OrderDirection.BUY, stop_loss=STOP,
              status=TransactionStatus.OPENED, **txn_kwargs):
    """An OPENED transaction of ``account`` (the link is through its order)."""
    txn = create_transaction(symbol=symbol, side=side, stop_loss=stop_loss,
                             status=status, quantity=10.0, open_price=STOP,
                             **txn_kwargs)
    create_trading_order(account_id=account.id, symbol=symbol, side=side,
                         quantity=10.0, transaction_id=txn.id,
                         order_type=OrderType.MARKET, status=OrderStatus.FILLED)
    return txn


def _run(account, monkeypatch=None):
    """Run the safety net with a recording logger; returns (manager, recorder)."""
    tm = TradeManager()
    rec = _LogRecorder()
    tm.logger = rec
    tm._force_close_breached_stops(account)
    return tm, rec


# --------------------------------------------------------------------------- #
# 1-4: the breach rule itself
# --------------------------------------------------------------------------- #

def test_long_gapped_through_stop_and_cushion_is_force_closed(monkeypatch):
    acct_def = create_account_definition()
    txn = _position(acct_def, "RARE")
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER - 0.01}, {"RARE": 10.0})

    logged = []
    monkeypatch.setattr("ba2_common.core.utils.log_close_order_activity",
                        lambda **kw: logged.append(kw))

    _, rec = _run(account)

    assert account.closed == [txn.id]
    assert len(logged) == 1
    assert logged[0]["transaction"].id == txn.id
    assert logged[0]["account_id"] == acct_def.id
    assert logged[0]["success"] is True
    assert logged[0]["close_order_id"] == 4242
    assert any("RARE" in m for m in rec.warning_msgs)
    # The activity carries the reason the operator has to be able to search for.
    assert get_instance(Transaction, txn.id).close_reason == "stop_breached_forced_close"


def test_long_inside_the_cushion_is_left_alone():
    """The triggered stop-limit can still fill here — do not pre-empt the broker."""
    acct_def = create_account_definition()
    _position(acct_def, "RARE")
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER + 0.01}, {"RARE": 10.0})

    _run(account)

    assert account.closed == []


def test_long_above_its_stop_is_left_alone():
    acct_def = create_account_definition()
    _position(acct_def, "RARE")
    account = _FakeAccount(acct_def.id, {"RARE": STOP + 5.0}, {"RARE": 10.0})

    _run(account)

    assert account.closed == []
    assert account.position_calls == []  # no broker round-trip for a healthy position


def test_short_gapped_up_through_stop_and_cushion_is_force_closed():
    acct_def = create_account_definition()
    txn = _position(acct_def, "ENOV", side=OrderDirection.SELL)
    account = _FakeAccount(acct_def.id, {"ENOV": SHORT_TRIGGER + 0.01}, {"ENOV": -10.0})

    _run(account)

    assert account.closed == [txn.id]


# --------------------------------------------------------------------------- #
# 5-9: refusing to act on anything unverified
# --------------------------------------------------------------------------- #

def test_position_no_longer_held_at_broker_is_not_closed():
    acct_def = create_account_definition()
    _position(acct_def, "RARE")
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER - 1.0}, {"RARE": 0.0})

    _, rec = _run(account)

    assert account.closed == []
    assert any("RARE" in m for m in rec.warning_msgs)


def test_unreadable_position_book_is_not_closed_on_a_guess():
    acct_def = create_account_definition()
    _position(acct_def, "RARE")
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER - 1.0}, {"RARE": None})

    _, rec = _run(account)

    assert account.closed == []
    assert any("RARE" in m for m in rec.warning_msgs)


@pytest.mark.parametrize("bad_price", [None, 0.0, -3.0, float("nan")])
def test_an_unusable_price_skips_that_symbol_and_still_processes_the_rest(bad_price):
    """UNKNOWN IS NOT SAFE — but it is also not a close signal. Skip loudly, carry on."""
    acct_def = create_account_definition()
    _position(acct_def, "AAA")
    breached = _position(acct_def, "BBB")
    account = _FakeAccount(
        acct_def.id,
        {"AAA": bad_price, "BBB": LONG_TRIGGER - 1.0},
        {"AAA": 10.0, "BBB": 10.0},
    )

    _, rec = _run(account)

    assert account.closed == [breached.id]
    assert any("AAA" in m for m in rec.warning_msgs)


def test_a_transaction_already_closing_is_untouched():
    """close_transaction moved it to CLOSING on the previous refresh — idempotent."""
    acct_def = create_account_definition()
    _position(acct_def, "RARE", status=TransactionStatus.CLOSING)
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER - 1.0}, {"RARE": 10.0})

    _run(account)

    assert account.closed == []


def test_option_transactions_are_skipped():
    """An option Transaction's symbol is the UNDERLYING and its stop is not a share price."""
    acct_def = create_account_definition()
    _position(acct_def, "RARE", asset_class=AssetClass.OPTION, multiplier=100)
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER - 1.0}, {"RARE": 10.0})

    _run(account)

    assert account.closed == []
    assert account.price_calls == []  # nothing to watch -> no price fetch at all


def test_a_transaction_without_a_stop_is_skipped():
    acct_def = create_account_definition()
    _position(acct_def, "RARE", stop_loss=None)
    account = _FakeAccount(acct_def.id, {"RARE": 1.0}, {"RARE": 10.0})

    _run(account)

    assert account.closed == []


def test_one_failing_transaction_does_not_abort_the_others():
    acct_def = create_account_definition()
    boom = _position(acct_def, "AAA")
    survivor = _position(acct_def, "BBB")
    account = _FakeAccount(
        acct_def.id,
        {"AAA": LONG_TRIGGER - 1.0, "BBB": LONG_TRIGGER - 1.0},
        {"AAA": 10.0, "BBB": 10.0},
        raise_on_close=[boom.id],
    )

    _, rec = _run(account)

    assert account.closed == [survivor.id]
    assert any("AAA" in m for m in rec.error_msgs)


def test_prices_are_fetched_in_one_bulk_call():
    acct_def = create_account_definition()
    _position(acct_def, "AAA")
    _position(acct_def, "BBB")
    account = _FakeAccount(acct_def.id, {"AAA": 200.0, "BBB": 200.0},
                           {"AAA": 10.0, "BBB": 10.0})

    _run(account)

    assert len(account.price_calls) == 1
    assert sorted(account.price_calls[0]) == ["AAA", "BBB"]


def test_other_accounts_positions_are_not_touched():
    mine = create_account_definition(name="Mine")
    theirs = create_account_definition(name="Theirs")
    _position(theirs, "RARE")
    account = _FakeAccount(mine.id, {"RARE": LONG_TRIGGER - 1.0}, {"RARE": 10.0})

    _run(account)

    assert account.closed == []


# --------------------------------------------------------------------------- #
# 10: ONE cushion, TWO readers
# --------------------------------------------------------------------------- #

def test_the_check_and_the_oco_leg_read_the_same_constant():
    # NOTE: `import ...accounts.AlpacaAccount as m` yields the CLASS here — the package
    # __init__ re-exports it under the submodule's own name — so import the name itself.
    from ba2_trade_platform.modules.accounts.AlpacaAccount import (
        OCO_STOP_LIMIT_CUSHION as alpaca_cushion,
    )

    assert OCO_STOP_LIMIT_CUSHION is alpaca_cushion
    assert OCO_STOP_LIMIT_CUSHION == 0.005


@pytest.mark.parametrize("side,factor", [
    (OrderDirection.SELL, 1.0 - OCO_STOP_LIMIT_CUSHION),  # long exit: limit BELOW the stop
    (OrderDirection.BUY, 1.0 + OCO_STOP_LIMIT_CUSHION),   # short exit: limit ABOVE the stop
])
def test_the_oco_stop_leg_limit_is_the_stop_moved_by_the_cushion(side, factor):
    """Pins the OTHER reader: the submitted OCO request itself."""
    from alpaca.trading.enums import OrderClass
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    acct_def = create_account_definition(provider="Alpaca")
    order = create_trading_order(
        account_id=acct_def.id, symbol="RARE", side=side, quantity=10.0,
        order_type=OrderType.OCO, status=OrderStatus.PENDING,
        limit_price=140.0 if side == OrderDirection.SELL else 60.0,
        stop_price=STOP, good_for="gtc",
    )

    account = object.__new__(AlpacaAccount)
    account.id = acct_def.id
    account.client = MagicMock()
    account._margin_info_cache = {}

    captured = []

    def _capture(request):
        captured.append(request)
        raise RuntimeError("stop before the broker call")

    account.client.submit_order.side_effect = _capture

    account._submit_order_impl(order)

    assert len(captured) == 1
    request = captured[0]
    assert request.order_class == OrderClass.OCO
    assert float(request.stop_loss.stop_price) == STOP
    assert float(request.stop_loss.limit_price) == round(STOP * factor, 2)
