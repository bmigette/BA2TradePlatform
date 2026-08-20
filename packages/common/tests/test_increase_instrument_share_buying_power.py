"""IncreaseInstrumentShareAction on an ALPACA-SHAPED account.

TradeActions.py:1493 called .get('buying_power', 0) on the result of
get_account_info(), which on Alpaca is a pydantic TradeAccount with no .get().
absorb_if_benign() re-raises anything that is not an InstanceNotFound, so the
AttributeError escaped execute() and this action has never once produced an order
on Alpaca. The fix routes the read through the broker-agnostic
get_account_snapshot() seam.
"""
from uuid import uuid4

from alpaca.trading.enums import AccountStatus
from alpaca.trading.models import TradeAccount

from ba2_common.core import instance_resolver
from ba2_common.core.TradeActions import IncreaseInstrumentShareAction
from ba2_common.core.interfaces.AccountInterface import AccountInterface


class _AlpacaShapedAccount(AccountInterface):
    """get_account_info() returns a real pydantic TradeAccount, exactly like Alpaca.
    Every other abstract method is a stub purely to satisfy ABC instantiation."""

    def __init__(self, id, buying_power="50000"):
        self.id = id
        self._buying_power = buying_power
        self._settings_cache = None

    def get_account_info(self):
        return TradeAccount(id=uuid4(), account_number="PA1", status=AccountStatus.ACTIVE,
                            cash="10000", equity="60000", buying_power=self._buying_power,
                            non_marginable_buying_power="10000", multiplier="2",
                            long_market_value="50000", short_market_value="0")

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


def _action(account):
    action = IncreaseInstrumentShareAction.__new__(IncreaseInstrumentShareAction)
    action.instrument_name = "AAPL"
    action.account = account
    action.order_recommendation = None
    action.existing_order = None
    action.expert_recommendation = type("Rec", (), {"instance_id": 42, "id": 7})()
    action.target_percent = 15.0
    action.submit_to_broker = False
    return action


def _run(action):
    previous = instance_resolver.get_instance_resolver()
    instance_resolver.set_instance_resolver(_FakeResolver())
    try:
        return action.execute()
    finally:
        instance_resolver.set_instance_resolver(previous)


def test_increase_share_creates_an_order_on_an_alpaca_shaped_account():
    """15% of a 10000 virtual equity at a 100 price, flat to start = 15 shares."""
    result = _run(_action(_AlpacaShapedAccount(1)))

    assert result["success"] is True, result["message"]
    assert result["data"]["quantity"] == 15.0
    assert result["data"]["side"] == "BUY"
    assert result["data"]["order_id"] is not None


def test_increase_share_clamps_the_order_to_the_available_buying_power():
    """Buying power of 500 cannot fund a 1500 target: 5 shares, not 15."""
    result = _run(_action(_AlpacaShapedAccount(1, buying_power="500")))

    assert result["success"] is True, result["message"]
    assert result["data"]["quantity"] == 5.0


def test_increase_share_refuses_to_size_when_buying_power_is_unknown():
    """No fabricated balance: an unreadable buying power blocks the order."""
    class _Blind(_AlpacaShapedAccount):
        def get_account_info(self):
            return None

    result = _run(_action(_Blind(1)))

    assert result["success"] is False
    assert "buying power" in result["message"].lower()
