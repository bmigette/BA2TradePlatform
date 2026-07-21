"""Regression: MarketExpertInterface.get_available_balance() must clamp the per-expert
virtual-equity figure to the account's ACTUAL available balance (2026-07-21 fix).

Without this, an expert's own virtual-equity bookkeeping has no visibility into (a) other
experts on the SAME account oversubscribing their own virtual slices (virtual_equity_pct is
allowed to sum past 100% across an account's experts), or (b) a manual trade placed outside any
expert's tracking -- both silently consume REAL account cash the expert's own math never learns
about, letting it believe it can afford more than the account actually has.
"""
import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

from tests import factories


class _FakeAccount:
    """Exposes only what get_available_balance()'s new clamp step reads."""

    def __init__(self, id_val, balance, account_info):
        self.id = id_val
        self._balance = balance
        self._account_info = account_info

    def get_balance(self):
        return self._balance

    def get_account_info(self):
        return self._account_info

    def get_instrument_current_price(self, symbol_or_list, price_type="bid"):
        return {} if isinstance(symbol_or_list, (list, tuple, set)) else None


class _BalanceExpert(MarketExpertInterface):
    def __init__(self, id_val):
        self.id = id_val
        self._settings_cache = None

    @classmethod
    def description(cls):
        return "balance-clamp test expert"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


def _resolver_for(account):
    class _R:
        def get_account_instance(self, account_id):
            return account
    return _R()


@pytest.mark.usefixtures("reset_test_db")
def test_available_balance_clamped_when_account_has_less_than_virtual_slice():
    """virtual_equity_pct=100 says the expert can spend the WHOLE 100k account balance, but the
    account's ACTUAL buying power is only 10k (another expert's oversubscribed fills, or a
    manual trade, already spent the rest) -- the real figure must win."""
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver

    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_BalanceExpert", virtual_equity_pct=100.0)
    expert = _BalanceExpert(inst.id)
    account = _FakeAccount(acct_def.id, balance=100_000.0,
                           account_info={"buying_power": 10_000.0})

    prev = get_instance_resolver()
    try:
        set_instance_resolver(_resolver_for(account))
        available = expert.get_available_balance()
    finally:
        set_instance_resolver(prev)

    assert available == 10_000.0  # clamped, not the naive 100_000.0 virtual figure


@pytest.mark.usefixtures("reset_test_db")
def test_available_balance_untouched_when_actual_is_higher():
    """The clamp only ever LOWERS the figure -- when the account has plenty of real buying
    power, the expert's own (tighter) virtual-equity number stands unchanged."""
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver

    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_BalanceExpert", virtual_equity_pct=10.0)
    expert = _BalanceExpert(inst.id)
    account = _FakeAccount(acct_def.id, balance=100_000.0,
                           account_info={"buying_power": 90_000.0})

    prev = get_instance_resolver()
    try:
        set_instance_resolver(_resolver_for(account))
        available = expert.get_available_balance()
    finally:
        set_instance_resolver(prev)

    assert available == 10_000.0  # the expert's own virtual figure, untouched


# ---------------------------------------------------------------------------
# _get_actual_available_balance: field-name fallback order, in isolation
# ---------------------------------------------------------------------------
class _InfoObj:
    """Attribute-style account info (mirrors the raw Alpaca SDK object)."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_actual_balance_prefers_buying_power_attr():
    account = _FakeAccount(1, balance=999.0, account_info=_InfoObj(buying_power=5_000.0))
    assert MarketExpertInterface._get_actual_available_balance(account) == 5_000.0


def test_actual_balance_falls_back_through_known_field_names():
    # no buying_power -> cash -> cash_balance -> equity_buying_power, in that order
    account = _FakeAccount(1, balance=999.0, account_info={"cash_balance": 42.0})
    assert MarketExpertInterface._get_actual_available_balance(account) == 42.0

    account = _FakeAccount(1, balance=999.0, account_info={"equity_buying_power": 7.0})
    assert MarketExpertInterface._get_actual_available_balance(account) == 7.0


def test_actual_balance_falls_back_to_get_balance_when_no_known_field():
    account = _FakeAccount(1, balance=250.0, account_info={"account_number": "abc"})
    assert MarketExpertInterface._get_actual_available_balance(account) == 250.0


def test_actual_balance_falls_back_to_get_balance_when_account_info_raises():
    class _BrokenInfoAccount(_FakeAccount):
        def get_account_info(self):
            raise RuntimeError("broker hiccup")

    account = _BrokenInfoAccount(1, balance=88.0, account_info=None)
    assert MarketExpertInterface._get_actual_available_balance(account) == 88.0
