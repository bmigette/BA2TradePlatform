"""The concrete broker seams on ReadOnlyAccountInterface / AccountInterface.

They are CONCRETE, never @abstractmethod: ReadOnlyAccountInterface already has 12
abstract methods, and adding a 13th would break instantiation of IBKRAccount,
TastyTradeAccount and every stub in the test suite.

_DictAccount below is the IBKR / TastyTrade shape: get_account_info() returns a
plain dict. AlpacaAccount's pydantic shape is covered in tests/test_alpaca_account_snapshot.py.
"""
from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface


class _DictAccount(ReadOnlyAccountInterface):
    """A broker whose get_account_info() returns a dict (IBKR / TastyTrade shape)."""

    def __init__(self, id, info):
        self.id = id
        self._info = info
        self._settings_cache = None

    def get_account_info(self):
        return self._info

    def get_balance(self):
        return None

    def get_positions(self):
        return []

    def get_orders(self, status=None):
        return []

    def symbols_exist(self, symbols):
        return {}

    def _get_instrument_current_price_impl(self, *a, **k):
        return None

    def get_balance_history(self, *a, **k):
        return []

    def get_dividends(self, *a, **k):
        return []

    def get_filled_trades(self, *a, **k):
        return []

    def get_order(self, *a, **k):
        return None

    def refresh_orders(self, *a, **k):
        return True

    def refresh_positions(self, *a, **k):
        return True


def test_snapshot_from_a_dict_broker_reads_the_tastytrade_field_names():
    """TastyTrade names them cash_balance / net_liquidating_value / equity_buying_power."""
    acct = _DictAccount(1, {
        "cash_balance": "12000.25",
        "net_liquidating_value": "48000.00",
        "equity_buying_power": "96000.00",
        "margin_multiplier": "2",
    })
    snap = acct.get_account_snapshot()
    assert snap.cash == 12000.25
    assert snap.net_liquidation == 48000.00
    assert snap.buying_power == 96000.00
    assert snap.margin_multiplier == 2.0


def test_snapshot_from_a_dict_broker_reads_the_plain_field_names():
    acct = _DictAccount(1, {
        "cash": "500.00",
        "equity": "10000.00",
        "buying_power": "10000.00",
        "long_market_value": "9500.00",
        "short_market_value": "0",
        "multiplier": "1",
    })
    snap = acct.get_account_snapshot()
    assert snap.cash == 500.0
    assert snap.equity == 10000.0
    assert snap.buying_power == 10000.0
    assert snap.long_market_value == 9500.0
    assert snap.short_market_value == 0.0


def test_snapshot_multiplier_above_one_marks_the_account_as_margin():
    assert _DictAccount(1, {"multiplier": "4"}).get_account_snapshot().is_margin_account is True


def test_snapshot_multiplier_of_one_is_a_cash_account():
    assert _DictAccount(1, {"multiplier": "1"}).get_account_snapshot().is_margin_account is False


def test_snapshot_of_a_broker_returning_none_is_all_unknown_not_all_zero():
    """None means "the broker told us nothing". A 0.0 here would let a caller
    plan against an account it cannot see."""
    snap = _DictAccount(1, None).get_account_snapshot()
    assert snap == AccountSnapshot()
    assert snap.buying_power is None


def test_snapshot_leaves_a_non_numeric_field_as_none_rather_than_guessing():
    snap = _DictAccount(1, {"buying_power": "n/a", "cash": "100"}).get_account_snapshot()
    assert snap.buying_power is None
    assert snap.cash == 100.0


def test_snapshot_from_an_attribute_broker_uses_the_getattr_branch():
    """The other half of the tolerant probe: an object, not a dict.

    This is the shape that broke TradeActions.py:1493 -- ``.get()`` on a pydantic
    TradeAccount raises AttributeError. Task 31 tests AlpacaAccount's OVERRIDE, so
    without this the base's ``getattr`` branch would have no coverage at all.
    ``raw`` stays {} because only a dict can be copied into it.
    """
    class _Attrs:
        cash = "500.00"
        equity = "10000.00"
        buying_power = "20000.00"
        long_market_value = "9500.00"
        short_market_value = "-250.00"
        multiplier = "2"

    snap = _DictAccount(1, _Attrs()).get_account_snapshot()
    assert snap.cash == 500.0
    assert snap.equity == 10000.0
    assert snap.buying_power == 20000.0
    assert snap.long_market_value == 9500.0
    assert snap.short_market_value == -250.0
    assert snap.margin_multiplier == 2.0
    assert snap.is_margin_account is True
    assert snap.raw == {}
    # A field the object simply does not carry stays unknown, never 0.0.
    assert snap.pending_transfer_in is None
    assert snap.non_marginable_buying_power is None


def test_snapshot_survives_a_broker_that_raises():
    class _Boom(_DictAccount):
        def get_account_info(self):
            raise RuntimeError("connection reset")

    assert _Boom(1, None).get_account_snapshot() == AccountSnapshot()


def test_get_cash_transfers_defaults_to_empty_for_a_broker_that_does_not_implement_it():
    """[] by default so no existing broker breaks. Alpaca and TastyTrade override it."""
    assert _DictAccount(1, {}).get_cash_transfers() == []


def test_get_cash_transfers_accepts_a_date_window_without_complaining():
    from datetime import date
    acct = _DictAccount(1, {})
    assert acct.get_cash_transfers(start_date=date(2026, 8, 1),
                                   end_date=date(2026, 8, 31)) == []


def test_get_symbol_margin_info_defaults_to_empty_so_the_caller_falls_back():
    """A symbol the broker cannot describe is OMITTED, never defaulted here -- the
    caller substitutes the conservative bp_factor = account multiplier."""
    assert _DictAccount(1, {}).get_symbol_margin_info(["AAPL", "MSFT"]) == {}
