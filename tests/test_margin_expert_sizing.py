"""With margin on, every expert-side figure starts from the account's TRADABLE
balance, not its balance -- the whole point is to trade above what was invested.
"""
import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

from tests import factories


class _Account:
    """Only what get_virtual_balance / get_available_balance read."""

    def __init__(self, id_val, *, balance, tradable, buying_power):
        self.id = id_val
        self._balance, self._tradable, self._bp = balance, tradable, buying_power
        self.tradable_calls = 0

    def get_balance(self):
        return self._balance

    def get_tradable_balance(self):
        self.tradable_calls += 1
        return self._tradable

    def get_account_info(self):
        return {"buying_power": self._bp}

    def get_instrument_current_price(self, symbol_or_list, price_type="bid"):
        return {} if isinstance(symbol_or_list, (list, tuple, set)) else None


class _Expert(MarketExpertInterface):
    def __init__(self, id_val):
        self.id = id_val
        self._settings_cache = None

    @classmethod
    def description(cls):
        return "margin sizing test expert"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


def _with_account(account, fn):
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver

    class _R:
        def get_account_instance(self, account_id):
            return account

    prev = get_instance_resolver()
    try:
        set_instance_resolver(_R())
        return fn()
    finally:
        set_instance_resolver(prev)


@pytest.mark.usefixtures("reset_test_db")
def test_virtual_balance_is_tradable_times_pct():
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_Expert", virtual_equity_pct=50.0)
    account = _Account(acct_def.id, balance=10_000.0, tradable=18_000.0, buying_power=20_000.0)
    assert _with_account(account, _Expert(inst.id).get_virtual_balance) == 9_000.0
    assert account.tradable_calls == 1


@pytest.mark.usefixtures("reset_test_db")
def test_available_balance_still_clamps_to_broker_buying_power():
    """The factor widens the base; the broker's remaining BP still caps the result."""
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_Expert", virtual_equity_pct=100.0)
    account = _Account(acct_def.id, balance=10_000.0, tradable=18_000.0, buying_power=5_000.0)
    assert _with_account(account, _Expert(inst.id).get_available_balance) == 5_000.0


@pytest.mark.usefixtures("reset_test_db")
def test_a_tradable_balance_error_yields_none_not_a_number():
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_Expert", virtual_equity_pct=100.0)

    class _Broken(_Account):
        def get_tradable_balance(self):
            raise ValueError("account published no buying power")

    account = _Broken(acct_def.id, balance=10_000.0, tradable=None, buying_power=None)
    assert _with_account(account, _Expert(inst.id).get_virtual_balance) is None


@pytest.mark.usefixtures("reset_test_db")
def test_available_balance_is_the_levered_figure_when_the_clamp_does_not_bind():
    """The counterpart pin: with broker BP above the levered base, the available
    balance IS the levered figure -- so the clamp test above cannot pass merely
    because something upstream capped the result back to the plain balance."""
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_Expert", virtual_equity_pct=100.0)
    account = _Account(acct_def.id, balance=10_000.0, tradable=18_000.0, buying_power=20_000.0)
    assert _with_account(account, _Expert(inst.id).get_available_balance) == 18_000.0
