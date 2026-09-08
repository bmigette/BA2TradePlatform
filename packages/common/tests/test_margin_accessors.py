"""get_stock_margin_multiplier / get_option_margin_multiplier / get_buying_power /
get_option_buying_power -- broker facts, read off the AccountSnapshot.

A missing stock multiplier or buying power RAISES: these feed position sizing and
a fabricated number there is a fabricated order. The option figures default
conservatively (multiplier 1.0 = cash-settled; option BP None = unknown, the
caller skips its check and says so).

No DB and no broker: every read is served from a hand-built AccountSnapshot.
"""
import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface


class _Stub(ReadOnlyAccountInterface):
    """Concrete ReadOnlyAccountInterface serving one canned snapshot.

    Every abstract method is filled in exactly as
    test_manual_trading_setting.StubAccount does, so instantiating needs no DB.
    """

    def __init__(self, snapshot):
        self.id = 7
        self._snap = snapshot

    @classmethod
    def get_settings_definitions(cls):
        return {}

    def get_account_snapshot(self):
        return self._snap

    def get_balance(self):
        return self._snap.equity

    def get_account_info(self):
        return {}

    def get_positions(self):
        return []

    def get_orders(self, status=None):
        return []

    def get_order(self, order_id):
        return None

    def symbols_exist(self, symbols):
        return {s: True for s in symbols}

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
        return None

    def refresh_positions(self):
        return True

    def refresh_orders(self):
        return True

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_balance_history(self, start_date=None, end_date=None):
        return []


def test_stock_multiplier_is_the_snapshot_multiplier():
    assert _Stub(AccountSnapshot(margin_multiplier=2.0)).get_stock_margin_multiplier() == 2.0


def test_stock_multiplier_raises_when_broker_publishes_none():
    with pytest.raises(ValueError, match="account 7"):
        _Stub(AccountSnapshot()).get_stock_margin_multiplier()


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_stock_multiplier_raises_on_a_non_positive_value(bad):
    """0 and negatives are not leverage figures a broker can mean. Returning one would
    size every margin order to zero (or to a negative) in silence, so they are treated
    as unpublished and raise alongside None."""
    with pytest.raises(ValueError, match="account 7"):
        _Stub(AccountSnapshot(margin_multiplier=bad)).get_stock_margin_multiplier()


def test_option_multiplier_defaults_to_one():
    assert _Stub(AccountSnapshot(margin_multiplier=4.0)).get_option_margin_multiplier() == 1.0


def test_buying_power_is_the_snapshot_buying_power():
    assert _Stub(AccountSnapshot(buying_power=1500.0)).get_buying_power() == 1500.0


def test_buying_power_raises_when_broker_publishes_none():
    with pytest.raises(ValueError, match="buying power"):
        _Stub(AccountSnapshot(equity=10.0)).get_buying_power()


def test_option_buying_power_may_be_none():
    assert _Stub(AccountSnapshot()).get_option_buying_power() is None
    assert _Stub(AccountSnapshot(option_buying_power=3.0)).get_option_buying_power() == 3.0
