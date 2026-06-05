"""Tests for wiring option assignment/expiry reconciliation into the account
refresh cycle (TradeManager._reconcile_account_option_activities).

The helper is called once per account during refresh_accounts(). For an
options-capable account that exposes the reconcile API it must:
  - fetch broker option lifecycle activities,
  - pass them to reconcile_option_assignments() when any are returned,
  - skip the reconcile call when there are no activities,
  - never propagate an exception (a broker hiccup must not break refresh),
  - skip accounts that are not options-capable.
"""
from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.interfaces.OptionsAccountInterface import OptionsAccountInterface


class _FakeOptionsAccount(OptionsAccountInterface):
    """Minimal options-capable double that records reconcile interactions."""

    def __init__(self, activities=None, raise_on_fetch=False):
        self.id = 999
        self._activities = activities or []
        self._raise_on_fetch = raise_on_fetch
        self.get_called = False
        self.reconciled_with = None

    # --- abstract OptionsAccountInterface surface (unused stubs) ---
    def get_option_chain(self, *a, **k): return []
    def get_option_quote(self, *a, **k): return None
    def get_atm_implied_volatility(self, *a, **k): return None
    def get_option_positions(self, *a, **k): return []
    def _submit_option_order_impl(self, *a, **k): return None
    def close_option_position(self, *a, **k): return None

    # --- reconcile API consumed by the helper ---
    def get_option_activities(self, after=None):
        self.get_called = True
        if self._raise_on_fetch:
            raise RuntimeError("broker activities endpoint down")
        return self._activities

    def reconcile_option_assignments(self, activities):
        self.reconciled_with = activities
        return [{"activity_id": a.get("id"), "result": "ok"} for a in activities]


class _PlainAccount:
    """Not options-capable and has no reconcile API."""
    id = 1


class TestReconcileAccountOptionActivities:
    def test_reconciles_when_activities_present(self):
        acct = _FakeOptionsAccount(activities=[{"id": "a1"}, {"id": "a2"}])
        TradeManager()._reconcile_account_option_activities(acct)
        assert acct.get_called is True
        assert acct.reconciled_with == [{"id": "a1"}, {"id": "a2"}]

    def test_skips_reconcile_when_no_activities(self):
        acct = _FakeOptionsAccount(activities=[])
        TradeManager()._reconcile_account_option_activities(acct)
        assert acct.get_called is True
        assert acct.reconciled_with is None  # reconcile_option_assignments not called

    def test_skips_non_options_account(self):
        acct = _PlainAccount()
        # Must be a no-op and must not raise.
        TradeManager()._reconcile_account_option_activities(acct)

    def test_swallows_fetch_exception(self):
        acct = _FakeOptionsAccount(raise_on_fetch=True)
        # A broker error must not propagate out of the helper.
        TradeManager()._reconcile_account_option_activities(acct)
        assert acct.reconciled_with is None
