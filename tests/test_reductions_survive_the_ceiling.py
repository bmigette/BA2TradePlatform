"""A REDUCTION is never refused for lack of capital (2026-09-09 final review, C1/C2).

The account stock-exposure ceiling clamps an expert's available balance to the
account's remaining headroom, and that headroom goes NEGATIVE the moment the account
is past ``balance x margin_factor`` -- which is precisely the state an operator needs
to trade OUT of. Before this fix the negative number leaked into the checks that ask
"can this expert afford it?":

  * ``TransactionHelper`` creates a partial-close trim with the comment "Partial close
    order (triggered by TP/SL cancel)". ``TradeManager``'s dependent-order path derived
    ``is_closing`` from ``'closing' in comment``, which does NOT match "Partial close
    order", so the trim was validated as an OPEN and the expert available-balance check
    ran on it. Its new-position branch refuses any order priced above the available
    balance -- and a clamped-negative balance is below EVERY price.
  * The wash-trade retry derived ``is_closing`` from the transaction alone, so an
    untracked close (``portfolio_allocation_service._sell_untracked_symbol``, which has
    no transaction by design) came back False and was gated as a new short.

Two fixes, pinned here: the validator itself skips any order whose side is opposite to
its transaction's, and ``TradeManager._is_closing_order`` is ONE derivation covering
both shapes.

Margin ON and OVER the ceiling throughout -- that is the only state in which the bug
exists, and a test run under a healthy account would pass without the fix.

Log assertions monkeypatch the module's own ``logger``: ``ba2_common.logger`` sets
``propagate = False``, so ``caplog`` sees nothing and every log pin would pass vacuously.
"""
import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.db import add_instance
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.models import ExpertSetting, TradingOrder
from ba2_common.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus,
)

from tests import factories


PRICE = 100.0


class _Acct(AccountInterface):
    """A REAL ``AccountInterface`` with every abstract stubbed and a canned broker
    snapshot, built bare (no ``__init__`` chain) -- the same shape
    ``packages/common/tests/test_stock_exposure_gate.py::_Acct`` uses. Real, because the
    arithmetic under test IS the interface's: a hand-written double that returned
    "valid" would pin nothing."""

    def __init__(self, *, id_val, balance, snapshot, settings):
        self.id = id_val
        self._balance = balance
        self._snap = snapshot
        self._stored = settings
        self._positions = []

    @property
    def settings(self):
        return self._stored

    @classmethod
    def get_settings_definitions(cls):
        return {}

    # --- read-only surface --------------------------------------------------
    def get_account_snapshot(self):
        return self._snap

    def get_balance(self):
        return self._balance

    def get_account_info(self):
        return {"buying_power": self._snap.buying_power}

    def get_instrument_current_price(self, symbol_or_symbols, price_type="bid"):
        # Overridden above the caching layer: that cache is global and would leak a
        # price between tests.
        if isinstance(symbol_or_symbols, (list, tuple, set)):
            return {s: PRICE for s in symbol_or_symbols}
        return PRICE

    def get_positions(self):
        return self._positions

    def get_orders(self, status=None):
        return []

    def get_order(self, order_id):
        return None

    def symbols_exist(self, symbols):
        return {s: True for s in symbols}

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type="bid"):
        return PRICE

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

    # --- trading surface ----------------------------------------------------
    def _submit_order_impl(self, trading_order, tp_price=None, sl_price=None,
                           is_closing_order=False, use_complex_order=False):
        return trading_order

    def cancel_order(self, order_id):
        return None

    def modify_order(self, order_id):
        return None

    def adjust_tp(self, transaction, new_tp_price, source=""):
        return True

    def adjust_sl(self, transaction, new_sl_price, source=""):
        return True

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        return True


class _Expert(MarketExpertInterface):
    def __init__(self, id_val):
        self.id = id_val
        self._settings_cache = None

    @classmethod
    def description(cls):
        return "reduction-under-the-ceiling test expert"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


def _with_instances(account, expert, fn):
    from ba2_common.core.instance_resolver import (
        get_instance_resolver, set_instance_resolver)

    class _R:
        def get_account_instance(self, account_id):
            return account

        def get_expert_instance(self, expert_id):
            return expert

    prev = get_instance_resolver()
    try:
        set_instance_resolver(_R())
        return fn()
    finally:
        set_instance_resolver(prev)


def _over_the_ceiling_world():
    """An account $4,000 PAST its own ceiling, holding one open 10-share long.

    balance 10,000 x min(margin_factor 1.8, broker multiplier 2.0) = 18,000 ceiling;
    the broker marks 22,000 of stock, so headroom is -4,000 and the expert's available
    balance is clamped to it. Returns (account, expert, transaction).
    """
    acct_def = factories.create_account_definition()
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_Expert", virtual_equity_pct=100.0)
    # A per-instrument cap MUST exist, or _validate_position_size_limits returns before
    # it ever reaches the expert available-balance check this test is about.
    add_instance(ExpertSetting(instance_id=inst.id,
                               key="max_virtual_equity_per_instrument_percent",
                               value_float=100.0))
    txn = factories.create_transaction(
        symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=PRICE, expert_id=inst.id)
    factories.create_trading_order(
        account_id=acct_def.id, symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=txn.id)
    snapshot = AccountSnapshot(equity=10_000.0, margin_multiplier=2.0,
                              buying_power=30_000.0,
                              long_market_value=22_000.0, short_market_value=0.0)
    account = _Acct(id_val=acct_def.id, balance=10_000.0, snapshot=snapshot,
                    settings={"margin_enabled": True, "margin_factor": 1.8})
    return account, _Expert(inst.id), txn


def _order(account, *, side, qty, transaction_id=None, comment=None):
    return TradingOrder(account_id=account.id, symbol="AAPL", quantity=qty, side=side,
                        order_type=OrderType.MARKET, transaction_id=transaction_id,
                        comment=comment)


# --------------------------------------------------------------------------
# The premise: the account really is over its ceiling and the expert really is
# clamped negative. Without this the two tests below would pass vacuously.
# --------------------------------------------------------------------------

@pytest.mark.usefixtures("reset_test_db")
def test_the_over_deployed_account_clamps_the_expert_to_a_negative_balance():
    account, expert, _ = _over_the_ceiling_world()
    assert account.get_stock_exposure_headroom() == pytest.approx(-4_000.0)
    available = _with_instances(account, expert, expert.get_available_balance)
    assert available == pytest.approx(-4_000.0), (
        "the premise of this file: past the ceiling, 'available' is a negative number "
        "and every order costs more than it")


# --------------------------------------------------------------------------
# (a) a trim passes; (b) an entry is still refused.
# --------------------------------------------------------------------------

@pytest.mark.usefixtures("reset_test_db")
def test_a_partial_close_passes_validation_on_a_maxed_out_account():
    """The C1 shape: SELL 5 against an OPEN 10-share long. It REDUCES the position, so
    neither the expert's available balance nor the account ceiling applies to it."""
    account, expert, txn = _over_the_ceiling_world()
    order = _order(account, side=OrderDirection.SELL, qty=5.0, transaction_id=txn.id,
                   comment="Partial close order (triggered by TP/SL cancel)")

    result = _with_instances(account, expert,
                             lambda: account._validate_trading_order(order))

    assert result["errors"] == [], result["errors"]
    assert result["is_valid"] is True


@pytest.mark.usefixtures("reset_test_db")
def test_a_one_share_entry_is_still_refused_by_the_ceiling():
    """The ceiling has to keep biting on the way IN, or the fix above would have
    disabled it. One share, $100, against -$4,000 of headroom."""
    account, expert, _ = _over_the_ceiling_world()
    order = _order(account, side=OrderDirection.BUY, qty=1.0)

    result = _with_instances(account, expert,
                             lambda: account._validate_trading_order(order))

    assert result["is_valid"] is False
    assert any("stock exposure ceiling" in e for e in result["errors"]), result["errors"]


@pytest.mark.usefixtures("reset_test_db")
def test_a_top_up_of_the_same_position_is_still_refused():
    """Opposite side is the whole test: a SAME-side order against the same transaction
    ADDS exposure and must still be refused, or the skip would be a hole."""
    account, expert, txn = _over_the_ceiling_world()
    order = _order(account, side=OrderDirection.BUY, qty=1.0, transaction_id=txn.id)

    result = _with_instances(account, expert,
                             lambda: account._validate_trading_order(order))

    assert result["is_valid"] is False
    assert any("available balance" in e or "stock exposure ceiling" in e
               for e in result["errors"]), result["errors"]


# --------------------------------------------------------------------------
# (c) ONE derivation of "is this order closing?"
# --------------------------------------------------------------------------

@pytest.mark.usefixtures("reset_test_db")
@pytest.mark.parametrize("comment", [
    "Partial close order (triggered by TP/SL cancel)",
    "Closing position after TP/SL cancellation",
])
def test_is_closing_order_matches_a_partial_close_comment_without_a_transaction(comment):
    """The C1 defect verbatim: the old heuristic looked for 'closing' and this comment
    says 'close'. No transaction, so rule 1 cannot answer it either."""
    from ba2_trade_platform.core.TradeManager import TradeManager

    acct_def = factories.create_account_definition()
    order = factories.create_trading_order(
        account_id=acct_def.id, side=OrderDirection.SELL, quantity=5.0,
        order_type=OrderType.MARKET,
        comment=comment)

    assert TradeManager._is_closing_order(order) is True


@pytest.mark.usefixtures("reset_test_db")
def test_is_closing_order_reads_an_opposite_side_transaction():
    from ba2_trade_platform.core.TradeManager import TradeManager

    acct_def = factories.create_account_definition()
    txn = factories.create_transaction(side=OrderDirection.BUY,
                                       status=TransactionStatus.OPENED)
    order = factories.create_trading_order(
        account_id=acct_def.id, side=OrderDirection.SELL, quantity=5.0,
        order_type=OrderType.MARKET, transaction_id=txn.id)

    assert TradeManager._is_closing_order(order) is True


@pytest.mark.usefixtures("reset_test_db")
@pytest.mark.parametrize("comment", ["Entry order", "Entry close to moving average"])
def test_is_closing_order_says_no_for_a_same_side_order(comment):
    """An OPEN mislabelled as a close would skip the risk checks entirely, so the
    default has to be False."""
    from ba2_trade_platform.core.TradeManager import TradeManager

    acct_def = factories.create_account_definition()
    txn = factories.create_transaction(side=OrderDirection.BUY,
                                       status=TransactionStatus.OPENED)
    order = factories.create_trading_order(
        account_id=acct_def.id, side=OrderDirection.BUY, quantity=5.0,
        order_type=OrderType.MARKET, transaction_id=txn.id,
        comment=comment)

    assert TradeManager._is_closing_order(order) is False
