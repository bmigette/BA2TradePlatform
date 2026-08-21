"""`manual_trading_enabled` — the Portfolio Allocation page's per-account gate.

Declared once on ReadOnlyAccountInterface so every broker inherits it and the
generic settings dialog renders/saves it with no UI code.

These tests touch no database and no broker: they exercise the settings-definition
merge and the never-saved-key read path only. The load-bearing behaviour is that
`settings.get(key, default)` does NOT work here (the settings property seeds every
declared key to None, so the default never applies) while
`get_setting_with_interface_default` does.
"""
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface


class StubAccount(ReadOnlyAccountInterface):
    """Concrete ReadOnlyAccountInterface with every abstract method filled in and a
    settings dict supplied by the test, so no DB is needed."""

    def __init__(self, stored_settings):
        self.id = 1
        self._stored = stored_settings

    @property
    def settings(self):
        return self._stored

    @classmethod
    def get_settings_definitions(cls):
        return {}

    def get_balance(self):
        return 0.0

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


def test_manual_trading_enabled_is_declared_as_bool_defaulting_false():
    defs = StubAccount.get_merged_settings_definitions()
    assert "manual_trading_enabled" in defs, f"declared settings: {sorted(defs)}"
    assert defs["manual_trading_enabled"]["type"] == "bool"
    assert defs["manual_trading_enabled"]["default"] is False


def test_manual_trading_enabled_never_saved_reads_false_not_none():
    """The settings property seeds every DECLARED key to None, so
    `settings.get(key, False)` yields None — the trap. Only
    get_setting_with_interface_default falls back to the declared default."""
    acct = StubAccount({"manual_trading_enabled": None})
    assert acct.settings.get("manual_trading_enabled", False) is None
    assert acct.get_setting_with_interface_default(
        "manual_trading_enabled", log_warning=False) is False


def test_manual_trading_enabled_saved_true_is_returned():
    acct = StubAccount({"manual_trading_enabled": True})
    assert acct.get_setting_with_interface_default(
        "manual_trading_enabled", log_warning=False) is True


def test_manual_trading_enabled_saved_false_is_returned_not_treated_as_unset():
    """A deliberately-saved False must survive: False is not None."""
    acct = StubAccount({"manual_trading_enabled": False})
    assert acct.get_setting_with_interface_default(
        "manual_trading_enabled", log_warning=False) is False


def test_manual_trading_enabled_string_none_is_treated_as_unset():
    """str(None) was historically written to the settings table; the literal
    string 'None' must not read back as a truthy flag."""
    acct = StubAccount({"manual_trading_enabled": "None"})
    assert acct.get_setting_with_interface_default(
        "manual_trading_enabled", log_warning=False) is False
