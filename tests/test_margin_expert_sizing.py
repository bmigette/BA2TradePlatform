"""With margin on, every expert-side figure starts from the account's TRADABLE
balance, not its balance -- the whole point is to trade above what was invested.
"""
import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

from tests import factories


_UNSET = object()


class _Account:
    """Only what get_virtual_balance / get_available_balance read."""

    def __init__(self, id_val, *, balance, tradable, buying_power,
                 option_tradable=_UNSET):
        self.id = id_val
        self._balance, self._tradable, self._bp = balance, tradable, buying_power
        # The OPTION sleeve's base is its own figure (balance x the option multiplier,
        # 1.0 at every broker today), so a double that answered the STOCK number for
        # both could not tell the two callers apart. Defaults to the plain balance.
        self._option_tradable = balance if option_tradable is _UNSET else option_tradable
        self.tradable_calls = 0
        self.option_tradable_calls = 0

    def get_balance(self):
        return self._balance

    def get_tradable_balance(self):
        self.tradable_calls += 1
        return self._tradable

    def get_option_tradable_balance(self):
        self.option_tradable_calls += 1
        return self._option_tradable

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
def test_a_NON_benign_virtual_balance_error_propagates(monkeypatch):
    """The counterpart of the test above, and the reason the handler names ValueError
    rather than absorbing everything: ValueError is the NAMED "unknown balance / bad
    margin factor" signal the tradable-balance path raises. A TypeError is a DEFECT,
    and swallowing it would size every entry this expert makes off a silent None --
    exactly how the ATR tz bug stayed invisible for months."""
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_Expert", virtual_equity_pct=100.0)

    class _Defective(_Account):
        def get_tradable_balance(self):
            raise TypeError("defect")

    account = _Defective(acct_def.id, balance=10_000.0, tradable=None, buying_power=None)

    with pytest.raises(TypeError, match="defect"):
        _with_account(account, _Expert(inst.id).get_virtual_balance)


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
# The other expert-side readers of the same sleeve: the OPTION entry action's own
# equity figure, and the account-level per-instrument cap. (The share-increase /
# decrease action is not one of them: it sizes off the expert's
# get_virtual_balance, already on the tradable balance above.)
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


def test_option_entry_virtual_equity_is_the_option_tradable_balance_times_pct():
    """An option entry sizes off the OPTION tradable balance, not the stock one: long
    options are cash-settled, so with the stock factor at 1.8 (18k) and the option
    multiplier at 1.0 the sleeve is still the plain 10k. Reading the stock figure here
    would size every option entry 1.8x too big."""
    account = _Account(1, balance=10_000.0, tradable=18_000.0, buying_power=20_000.0,
                       option_tradable=10_000.0)
    action = _option_entry_action(account)  # no recommendation -> pct defaults to 100

    assert action._virtual_equity() == 10_000.0
    assert account.option_tradable_calls == 1
    assert account.tradable_calls == 0, "the stock tradable balance is not the option base"


def test_trade_action_virtual_equity_is_none_not_a_number_when_tradable_raises():
    """An account that cannot say what it may deploy in options yields None -- never the
    unlevered balance, which would silently disagree with the expert's own sizing."""
    class _Broken(_Account):
        def get_option_tradable_balance(self):
            raise ValueError("account published no buying power")

    action = _option_entry_action(
        _Broken(1, balance=10_000.0, tradable=None, buying_power=None))

    assert action._virtual_equity() is None


def test_trade_action_virtual_equity_propagates_a_NON_benign_error(monkeypatch):
    """ValueError is the NAMED "unknown balance" signal and yields None. A TypeError is a
    DEFECT, and absorbing it would size every option entry off a silent None instead of
    surfacing the bug. Under enforce it must come straight back out."""
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")

    class _Defective(_Account):
        def get_option_tradable_balance(self):
            raise TypeError("defect")

    action = _option_entry_action(
        _Defective(1, balance=10_000.0, tradable=None, buying_power=None))

    with pytest.raises(TypeError, match="defect"):
        action._virtual_equity()


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
    sleeve, on an account whose ``effective_margin_factor_from()`` is ``factor`` (or, if
    ``factor`` is an exception, one that raises it) -- the cap derives the factor from
    the snapshot it already took for equity. Returns the error list.

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
        def effective_margin_factor_from(self, snapshot):
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
