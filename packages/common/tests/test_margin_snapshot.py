"""AccountSnapshot.option_buying_power: the broker's option (derivative) buying power.

None means the broker did not publish one -- never 0.0. The base tolerant probe
reads it from `options_buying_power` (Alpaca) or `derivative_buying_power`
(TastyTrade); nothing else is guessed at.
"""
from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface


class _Probe(ReadOnlyAccountInterface):
    """Concrete ReadOnlyAccountInterface with every abstract method filled in;
    only get_account_info() matters, the rest is what instantiation demands."""

    def __init__(self, info):
        self.id = 1
        self._info = info

    @classmethod
    def get_settings_definitions(cls):
        return {}

    def get_balance(self):
        return None

    def get_account_info(self):
        return self._info

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


def test_default_is_none():
    assert AccountSnapshot().option_buying_power is None


def test_probe_reads_alpaca_name():
    snap = _Probe({"options_buying_power": "1234.5"}).get_account_snapshot()
    assert snap.option_buying_power == 1234.5


def test_probe_reads_tastytrade_name():
    snap = _Probe({"derivative_buying_power": 99.0}).get_account_snapshot()
    assert snap.option_buying_power == 99.0


def test_probe_leaves_none_when_broker_publishes_neither():
    snap = _Probe({"buying_power": 10.0}).get_account_snapshot()
    assert snap.option_buying_power is None
