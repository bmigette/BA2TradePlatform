"""Minimal concrete accounts for the account-tap tests (every abstract method stubbed)."""
from types import SimpleNamespace

from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.replay.context import CaptureHealth


class StubAccount(AccountInterface):
    """A concrete stock account; subclasses override the methods they want to observe."""

    def __init__(self, id=1):
        self.id = id
        self._settings_cache = None

    def _get_instrument_current_price_impl(self, *a, **k): return 100.0
    def _submit_order_impl(self, *a, **k): return None
    def adjust_sl(self, *a, **k): return True
    def adjust_tp(self, *a, **k): return True
    def adjust_tp_sl(self, *a, **k): return True
    def cancel_order(self, *a, **k): return None
    def get_account_info(self): return {}
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


class RecordingStore:
    def __init__(self):
        self.health = CaptureHealth()
        self.submitted = []

    def submit(self, record, objects=None, observations=()):
        self.submitted.append((record, dict(objects or {}), list(observations)))


def order_row(**over):
    fields = dict(id=11, symbol="XYZ", quantity=3.0)
    fields.update(over)
    return SimpleNamespace(**fields)
