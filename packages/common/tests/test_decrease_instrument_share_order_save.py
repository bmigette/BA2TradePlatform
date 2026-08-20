"""DecreaseInstrumentShareAction double-saves its order record.

create_order_record() ALREADY persists the TradingOrder and returns its integer id
(see SellAction, which uses it correctly). DecreaseInstrumentShareAction then fed
that int straight back into add_instance(), which raises

    sqlalchemy.orm.exc.UnmappedInstanceError: Class 'builtins.int' is not mapped

and absorb_if_benign() re-raises it (UnmappedInstanceError is not an
InstanceNotFound, and BA2_ERROR_MODE defaults to "enforce"), so execute() blew up
before it could ever return an order. This is the same defect fixed in
IncreaseInstrumentShareAction -- but unlike the increase path, this one needs no
buying-power read to reach the crash, so it has never produced an order on ANY
broker, not just Alpaca.

NOTE ON THE ABSENT BALANCE GUARD: the decrease path deliberately reads no broker
balance at all -- no get_account_info(), no get_account_snapshot(), no buying
power. The only balance it touches is the expert's own virtual equity via
expert.get_virtual_balance(), which already has its own `is None or <= 0` guard.
So there is no equivalent of the increase path's buying-power `is None` guard to
add here. test_decrease_share_reads_no_broker_balance below pins that down so the
claim stays true rather than merely being asserted in a comment.
"""
from ba2_common.core import instance_resolver
from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance
from ba2_common.core.models import Transaction
from ba2_common.core.TradeActions import DecreaseInstrumentShareAction
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.types import OrderDirection, TransactionStatus


class _StubAccount(AccountInterface):
    """The decrease path only ever needs an id and a price. get_account_info() is
    booby-trapped rather than stubbed: if the action ever starts reading the broker
    balance, the AssertionError propagates (absorb_if_benign re-raises it) and the
    no-broker-balance test below fails loudly instead of quietly going stale."""

    def __init__(self, id=1):
        self.id = id
        self._settings_cache = None

    def get_account_info(self):
        raise AssertionError(
            "DecreaseInstrumentShareAction is not expected to read the broker balance"
        )

    def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
        return 100.0

    def _get_instrument_current_price_impl(self, *a, **k): return 100.0
    def _submit_order_impl(self, *a, **k): return None
    def adjust_sl(self, *a, **k): return None
    def adjust_tp(self, *a, **k): return None
    def adjust_tp_sl(self, *a, **k): return None
    def cancel_order(self, *a, **k): return None
    def get_balance(self): return 60000.0
    def get_balance_history(self, *a, **k): return []
    def get_dividends(self, *a, **k): return []
    def get_filled_trades(self, *a, **k): return []
    def get_order(self, *a, **k): return None
    def get_orders(self, status=None): return []
    def get_positions(self): return []
    def modify_order(self, *a, **k): return None
    def refresh_orders(self, *a, **k): return True
    def refresh_positions(self, *a, **k): return True
    def symbols_exist(self, symbols): return {}


class _FakeExpert:
    settings = {"max_virtual_equity_per_instrument_percent": 20.0}

    def get_virtual_balance(self):
        return 10000.0


class _FakeResolver:
    def get_expert_instance(self, expert_id): return _FakeExpert()
    def get_account_instance(self, account_id): return None
    def get_account_instance_from_transaction(self, transaction): return None


def _action(target_percent=5.0):
    action = DecreaseInstrumentShareAction.__new__(DecreaseInstrumentShareAction)
    action.instrument_name = "AAPL"
    action.account = _StubAccount(1)
    action.order_recommendation = None
    action.existing_order = None
    action.expert_recommendation = type("Rec", (), {"instance_id": 42, "id": 7})()
    action.target_percent = target_percent
    action.submit_to_broker = False
    return action


def _run_holding(quantity, side):
    """Run a decrease against a real open Transaction, so get_expert_position() reads the
    position through its genuine code path instead of being monkeypatched."""
    action = _action()
    previous = instance_resolver.get_instance_resolver()
    instance_resolver.set_instance_resolver(_FakeResolver())
    try:
        with ts.inmem_trades():
            add_instance(Transaction(symbol="AAPL", quantity=quantity, side=side,
                                     status=TransactionStatus.OPENED, expert_id=42))
            return action.execute()
    finally:
        instance_resolver.set_instance_resolver(previous)


def test_decrease_share_creates_a_sell_order_to_trim_a_long():
    """20 shares @ 100 = 2000, target 5% of 10000 = 500, so sell 15 and keep 5."""
    result = _run_holding(20.0, OrderDirection.BUY)

    assert result["success"] is True, result["message"]
    assert result["data"]["quantity"] == 15.0
    assert result["data"]["side"] == "SELL"
    assert result["data"]["order_id"] is not None
    assert result["data"]["remaining_qty"] == 5.0


def test_decrease_share_covers_a_short_with_a_buy():
    """The mirror path: a 20-share SHORT is trimmed by BUYing 15 back."""
    result = _run_holding(20.0, OrderDirection.SELL)

    assert result["success"] is True, result["message"]
    assert result["data"]["quantity"] == 15.0
    assert result["data"]["side"] == "BUY"
    assert result["data"]["order_id"] is not None


def test_decrease_share_reads_no_broker_balance():
    """Pins the finding that this path consults no broker balance whatsoever: the stub's
    get_account_info() raises, and a successful decrease proves it was never called."""
    result = _run_holding(20.0, OrderDirection.BUY)

    assert result["success"] is True, result["message"]
