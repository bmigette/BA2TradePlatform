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

# ---------------------------------------------------------------------------
# The other expert-side readers of the same sleeve: the share-increase/decrease
# action's own equity figure, and the account-level per-instrument cap.
# ---------------------------------------------------------------------------

def _option_entry_action(account, expert_recommendation=None):
    """A bare BuyCallAction -- the shipped concrete _OptionEntryAction that owns
    _virtual_equity (the base itself is abstract). __init__ wants a whole
    recommendation graph; _virtual_equity reads only these two attributes."""
    from ba2_common.core.TradeActions import BuyCallAction

    action = BuyCallAction.__new__(BuyCallAction)
    action.account = account
    action.expert_recommendation = expert_recommendation
    return action


def test_trade_action_virtual_equity_agrees_with_the_expert():
    """PARITY PIN: TradeActions._virtual_equity and MarketExpertInterface.get_virtual_balance
    must be the same number for the same account -- one base (tradable), two callers."""
    account = _Account(1, balance=10_000.0, tradable=18_000.0, buying_power=20_000.0)
    action = _option_entry_action(account)  # no recommendation -> pct defaults to 100

    assert action._virtual_equity() == 18_000.0
    assert account.tradable_calls == 1


def test_trade_action_virtual_equity_is_none_not_a_number_when_tradable_raises():
    """An account that cannot say what it may deploy yields None -- never the
    unlevered balance, which would silently disagree with the expert's own sizing."""
    class _Broken(_Account):
        def get_tradable_balance(self):
            raise ValueError("account published no buying power")

    action = _option_entry_action(
        _Broken(1, balance=10_000.0, tradable=None, buying_power=None))

    assert action._virtual_equity() is None


# --- the account-level per-instrument cap ----------------------------------

class _ExpertWithMoney:
    """The expert's own available-balance guard is not under test in this section:
    it answers with more money than any order below, so only the per-instrument
    cap can produce an error."""

    def get_available_balance(self, exclude_transaction_id=None):
        return 10_000_000.0


class _ExpertOnlyResolver:
    def get_expert_instance(self, expert_id):
        return _ExpertWithMoney()

    def get_account_instance(self, account_id):
        raise NotImplementedError

    def get_account_instance_from_transaction(self, transaction):
        raise NotImplementedError


def _cap_errors(monkeypatch, factor, quantity, *, max_position_pct=10.0,
                equity=100_000.0):
    """Run ``_validate_position_size_limits`` for an order of ``quantity`` AAPL @ $100
    against an expert whose per-instrument cap is ``max_position_pct`` of a 100%
    sleeve, on an account whose ``effective_margin_factor()`` is ``factor`` (or, if
    ``factor`` is an exception, one that raises it). Returns the error list.

    The factor is stubbed rather than configured through margin settings so the cap
    is pinned against the contract it consumes, not against how margin is stored.
    """
    from ba2_trade_platform.core.models import ExpertSetting, TradingOrder
    from ba2_trade_platform.core.types import (
        OrderDirection, OrderStatus, OrderType, TransactionStatus,
    )
    from ba2_trade_platform.core.db import add_instance
    from tests.conftest import MockAccount
    from tests.factories import (
        create_account_definition, create_expert_instance, create_transaction,
    )

    class _FactorAccount(MockAccount):
        def effective_margin_factor(self):
            if isinstance(factor, Exception):
                raise factor
            return factor

    acct_def = create_account_definition()
    account = _FactorAccount(acct_def.id)
    account._balance = equity        # MockAccount's snapshot equity IS its balance
    account._prices["AAPL"] = 100.0  # round numbers: N shares == $N * 100

    expert_instance = create_expert_instance(
        account_id=acct_def.id, expert="MockExpert", virtual_equity_pct=100.0)
    add_instance(
        ExpertSetting(instance_id=expert_instance.id,
                      key="max_virtual_equity_per_instrument_percent",
                      value_str=None, value_float=float(max_position_pct)),
        expunge_after_flush=True,
    )
    monkeypatch.setattr("ba2_common.core.instance_resolver._resolver",
                        _ExpertOnlyResolver())

    transaction = create_transaction(
        symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
        status=TransactionStatus.WAITING, open_price=100.0,
        expert_id=expert_instance.id,
    )
    order = TradingOrder(
        account_id=acct_def.id, symbol="AAPL", quantity=float(quantity),
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.PENDING, transaction_id=transaction.id,
    )
    return account._validate_position_size_limits(order)


@pytest.mark.usefixtures("reset_test_db")
def test_position_size_limit_is_a_percent_of_the_scaled_denominator(monkeypatch):
    """max_virtual_equity_per_instrument_percent applies to equity x effective factor.

    $100k equity and a 10% cap is $10k unlevered. At a factor of 2.0 the cap is $20k,
    so a $15k order (1.5x the unlevered cap) must pass and a $25k one must not.
    """
    assert _cap_errors(monkeypatch, 2.0, quantity=150) == []

    errors = _cap_errors(monkeypatch, 2.0, quantity=250)
    assert any("exceeds expert's max allowed" in e for e in errors), errors


@pytest.mark.usefixtures("reset_test_db")
def test_position_size_limit_refuses_when_the_factor_is_unavailable(monkeypatch):
    """An unrun risk check is a refusal, not a pass: without a factor there is no
    denominator, exactly as with a missing equity."""
    errors = _cap_errors(
        monkeypatch, ValueError("broker published no multiplier"), quantity=10)

    assert errors, "an unavailable margin factor must not read as 'no problems'"
    assert any("margin factor" in e for e in errors), errors


@pytest.mark.usefixtures("reset_test_db")
def test_position_size_limit_reads_no_factor_with_margin_off(monkeypatch):
    """A factor of 1.0 (margin off) gives exactly the verdicts the cap gave before."""
    assert _cap_errors(monkeypatch, 1.0, quantity=90) == []

    errors = _cap_errors(monkeypatch, 1.0, quantity=110)
    assert any("exceeds expert's max allowed" in e for e in errors), errors
