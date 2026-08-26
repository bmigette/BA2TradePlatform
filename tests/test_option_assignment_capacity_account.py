"""The account-level SECOND VIEW: could this account pay for every assignment at once?

``OptionsAccountInterface.reserved_option_buying_power()`` answers "how much
cash/margin is already spoken for by open short-premium structures?". It is real and
wired — all seven credit builders feed it — but it charges wildly different things per
strategy: a ``cash_secured_put`` reserves the full ``strike x 100``, a
``short_strangle`` reserves Reg-T naked margin (~20% of notional).

``short_put_assignment_exposure()`` answers a different question entirely: **if every
short put on this account were assigned tomorrow, could we pay for the shares?** It is
a SECOND VIEW of the same order rows, not an addition to the reserve pool — the same
CSP appears in both totals, and that is correct, because each total is compared against
its own independently-measured budget and neither subtracts the other.

Also pinned here: an OPEN reserving order whose ``data["option_reserve"]`` has gone
missing used to contribute **0** to the reserve pool, so an unknown reserve *freed*
buying power. Unknown is not zero.
"""
import pytest

from ba2_common.core import trade_store as ts
from ba2_trade_platform.core.db import add_instance, get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
def open_txn(symbol="AAPL"):
    return add_instance(Transaction(symbol=symbol, quantity=1, open_price=1.0,
                                    status=TransactionStatus.OPENED,
                                    side=OrderDirection.SELL))


def short_put_order(account, *, strike=225.0, qty=1, txn_id=None, symbol="AAPL",
                    status=OrderStatus.FILLED, reserve=None, contract=None,
                    option_type=OptionRight.PUT):
    data = {} if reserve is None else {"option_reserve": reserve}
    return add_instance(TradingOrder(
        account_id=account.id, symbol=symbol, underlying_symbol=symbol, quantity=qty,
        filled_qty=(qty if status == OrderStatus.FILLED else None),
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT, status=status,
        asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol=(contract or f"{symbol}260101P{int(strike * 1000):08d}"),
        option_type=option_type, strike=strike, transaction_id=txn_id, data=data))


def buy_to_close_order(account, *, strike=225.0, qty=1, txn_id=None, symbol="AAPL",
                       status=OrderStatus.FILLED, contract=None,
                       option_type=OptionRight.PUT):
    return add_instance(TradingOrder(
        account_id=account.id, symbol=symbol, underlying_symbol=symbol, quantity=qty,
        filled_qty=(qty if status == OrderStatus.FILLED else None),
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT, status=status,
        asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol=(contract or f"{symbol}260101P{int(strike * 1000):08d}"),
        option_type=option_type, strike=strike, transaction_id=txn_id, data={}))


# ==========================================================================
# THE HEADLINE, at the account level
# ==========================================================================
def test_three_short_puts_each_affordable_are_together_more_than_the_account_holds(mock_account):
    """Each 225-strike put is $22,500 of delivery against a $100,000 account. Three of
    them are $67,500 — still fine. A fourth takes the book to $90,000, and a fifth to
    $112,500, which the account simply cannot pay. Nothing summed this before."""
    mock_account._balance = 100_000.0
    for i, strike in enumerate((225.0, 225.0, 225.0), start=1):
        short_put_order(mock_account, strike=strike, txn_id=open_txn(f"SYM{i}"),
                        symbol=f"SYM{i}", reserve=strike * 100.0)

    exposure = mock_account.short_put_assignment_exposure()
    assert exposure.cost == pytest.approx(67_500.0)
    assert exposure.contracts == pytest.approx(3.0)

    assert mock_account.check_assignment_capacity(22_500.0) is True    # 90,000
    assert mock_account.check_assignment_capacity(45_000.0) is False   # 112,500


def test_a_short_call_does_not_consume_put_assignment_capacity(mock_account):
    """A covered call delivers SHARES on assignment and pays cash IN. Charging it to a
    cash-capacity total would refuse trades for an obligation that does not exist."""
    mock_account._balance = 100_000.0
    short_put_order(mock_account, strike=225.0, txn_id=open_txn("AAPL"),
                    option_type=OptionRight.CALL, contract="AAPL260101C00225000")
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(0.0)


def test_a_long_put_does_not_consume_capacity(mock_account):
    """Buying a put is buying a right. Nobody can put shares to us with it."""
    mock_account._balance = 100_000.0
    buy_to_close_order(mock_account, strike=225.0, txn_id=open_txn("AAPL"))
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(0.0)


def test_a_short_put_bought_back_releases_its_capacity(mock_account):
    """Netting per contract symbol, exactly as the close paths do."""
    mock_account._balance = 100_000.0
    txn = open_txn("AAPL")
    short_put_order(mock_account, strike=225.0, txn_id=txn)
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(22_500.0)
    buy_to_close_order(mock_account, strike=225.0, txn_id=txn)
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(0.0)


def test_a_closed_structure_owes_nothing(mock_account):
    """The obligation belongs to the POSITION, exactly as the reserve does."""
    mock_account._balance = 100_000.0
    txn_id = open_txn("AAPL")
    short_put_order(mock_account, strike=225.0, txn_id=txn_id)
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(22_500.0)

    stored = get_instance(Transaction, txn_id)
    stored.status = TransactionStatus.CLOSED
    update_instance(stored)
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(0.0)


def test_an_unfilled_sell_to_open_still_counts_because_it_can_fill_any_moment(mock_account):
    """In flight is not absent — the same rule the reserve pool applies to a submitted
    structure whose capital 'is genuinely in flight'."""
    mock_account._balance = 100_000.0
    short_put_order(mock_account, strike=225.0, txn_id=open_txn("AAPL"), qty=2,
                    status=OrderStatus.PENDING)
    exposure = mock_account.short_put_assignment_exposure()
    assert exposure.cost == pytest.approx(45_000.0)
    assert exposure.contracts == pytest.approx(2.0)


def test_a_short_put_with_no_recorded_quantity_is_unmeasurable_not_free(mock_account):
    """``float(o.quantity or 0.0)`` would price an obligation of unknown size at
    nothing — the identical fail-open one field over from the reserve one."""
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", underlying_symbol="AAPL", quantity=0,
        filled_qty=None, side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.FILLED, asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol="AAPL260101P00225000", option_type=OptionRight.PUT, strike=225.0,
        transaction_id=open_txn("AAPL"), data={}))
    exposure = mock_account.short_put_assignment_exposure()
    assert exposure.cost is None
    assert any("quantity" in u for u in exposure.unmeasurable)
    assert mock_account.check_assignment_capacity(0.0) is False


def test_an_adjusted_contract_delivers_its_own_number_of_shares(mock_account):
    """A post-split / special-dividend contract can deliver something other than 100
    shares, and the row records it. Hardcoding 100 misprices delivery on exactly the
    contracts that are already unusual."""
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", underlying_symbol="AAPL", quantity=1,
        filled_qty=1, side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.FILLED, asset_class=AssetClass.OPTION, multiplier=17,
        contract_symbol="AAPL1260101P00225000", option_type=OptionRight.PUT, strike=225.0,
        transaction_id=open_txn("AAPL"), data={}))
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(3_825.0)


def test_an_unfilled_buy_to_close_does_not_release_capacity_early(mock_account):
    """A close that has not filled has not closed anything. Netting it off would hand
    back capacity for a short we still carry."""
    mock_account._balance = 100_000.0
    txn = open_txn("AAPL")
    short_put_order(mock_account, strike=225.0, txn_id=txn)
    buy_to_close_order(mock_account, strike=225.0, txn_id=txn, status=OrderStatus.PENDING)
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(22_500.0)


def test_a_partial_fill_owes_its_WHOLE_submitted_size(mock_account):
    """3 contracts submitted, 1 filled: 1 is held and 2 are still WORKING. All three.

    CORRECTED 2026-08 — this test previously asserted 22,500 on the grounds that
    "the account is short ONE put". It is short one put and ALSO carries a live sell
    order for two more, and the rule for that half is the one
    ``test_an_unfilled_sell_to_open_still_counts_because_it_can_fill_any_moment``
    pins directly above: a submitted-but-unfilled SELL can fill at any moment and can
    only ever ADD an obligation. The two were incoherent — three unfilled contracts
    owed 67,500, and then the FIRST fill dropped the same book to 22,500, i.e.
    executing part of the order made the account look 45,000 LESS exposed.
    ``PARTIALLY_FILLED`` is an EXECUTED status, so ``filled_qty if filled_qty else
    quantity`` read the filled part and dropped the remainder on the floor; during
    that window the next structure is admitted on capacity the following fill
    consumes.

    The equity-side twin of the same line — a partially filled sell-to-open pledging
    100 shares of cover instead of 300 — is fixed in the same commit. The two views
    deliberately consume ONE query and must not disagree about this window.
    """
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", underlying_symbol="AAPL", quantity=3,
        filled_qty=1, side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.PARTIALLY_FILLED, asset_class=AssetClass.OPTION,
        multiplier=100, contract_symbol="AAPL260101P00225000",
        option_type=OptionRight.PUT, strike=225.0, transaction_id=open_txn("AAPL"),
        data={}))
    exposure = mock_account.short_put_assignment_exposure()
    assert exposure.cost == pytest.approx(67_500.0)
    # 3, not 4: the filled contract is counted ONCE (netted) and the remainder ONCE
    # (in flight). Charging the ordered size ON TOP of the filled size is the other
    # way to get this wrong and would read 90,000 here.
    assert exposure.contracts == pytest.approx(3.0)


def test_buying_back_the_filled_part_leaves_the_rest_of_the_order_owing(mock_account):
    """The remainder lives in the in-flight total, never in the netted one.

    Buying back the ONE contract that filled releases exactly that contract; the two
    still working at the broker are beyond a buy-to-close's reach — the same
    asymmetry as ``test_an_unfilled_buy_to_close_does_not_release_capacity_early``.
    """
    mock_account._balance = 100_000.0
    txn = open_txn("AAPL")
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", underlying_symbol="AAPL", quantity=3,
        filled_qty=1, side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.PARTIALLY_FILLED, asset_class=AssetClass.OPTION,
        multiplier=100, contract_symbol="AAPL260101P00225000",
        option_type=OptionRight.PUT, strike=225.0, transaction_id=txn, data={}))
    buy_to_close_order(mock_account, strike=225.0, txn_id=txn)
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(45_000.0)


def test_a_multi_leg_parent_is_not_billed_alongside_its_legs(mock_account):
    """``submit_option_order`` writes a parent with NO contract symbol, no strike and
    no right — the legs carry the identity, one row each. Counting the parent as well
    would double-bill a spread, and it has nothing to bill it on."""
    mock_account._balance = 100_000.0
    txn = open_txn("AAPL")
    parent_id = add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", underlying_symbol="AAPL", quantity=1,
        filled_qty=1, side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.FILLED, asset_class=AssetClass.OPTION, multiplier=100,
        option_strategy="put_credit_spread", contract_symbol=None, option_type=None,
        strike=None, transaction_id=txn, data={"option_reserve": 500.0}))
    short_put_order(mock_account, strike=100.0, txn_id=txn,
                    contract="AAPL260101P00100000")
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL260101P00095000",
        underlying_symbol="AAPL", quantity=1, filled_qty=1, side=OrderDirection.BUY,
        order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol="AAPL260101P00095000", option_type=OptionRight.PUT, strike=95.0,
        parent_order_id=parent_id, transaction_id=txn, data={}))

    exposure = mock_account.short_put_assignment_exposure()
    assert exposure.unmeasurable == ()
    # The full short strike, once. Not the 5-wide wing, and not doubled by the parent.
    assert exposure.cost == pytest.approx(10_000.0)


def test_an_order_not_yet_linked_to_a_transaction_still_owes(mock_account):
    """Submitted, filling, no transaction row yet: that obligation is genuinely in
    flight and it is exactly the window in which a second entry would be sized. The
    reserve pool has always counted such a row; the capacity view must too — and the
    linked structure alongside it is what makes the 'is it linked?' branch the one
    under test rather than the early return above it."""
    mock_account._balance = 100_000.0
    short_put_order(mock_account, strike=225.0, txn_id=open_txn("AAPL"),
                    symbol="AAPL", reserve=22_500.0,
                    contract="AAPL260101P00225000")
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="MSFT", underlying_symbol="MSFT", quantity=1,
        filled_qty=1, side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.FILLED, asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol="MSFT260101P00300000", option_type=OptionRight.PUT, strike=300.0,
        option_strategy="cash_secured_put", transaction_id=None,
        data={"option_reserve": 30_000.0}))

    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(52_500.0)
    assert mock_account.reserved_option_buying_power() == pytest.approx(52_500.0)


def test_a_terminal_order_owes_nothing(mock_account):
    """A cancel that printed NOTHING releases everything — ``short_put_order``
    leaves ``filled_qty`` NULL for a non-FILLED status, which is what makes this
    an ordinary cancel rather than the raced one pinned below."""
    mock_account._balance = 100_000.0
    order = short_put_order(mock_account, strike=225.0, txn_id=open_txn("AAPL"),
                            status=OrderStatus.CANCELED)
    assert get_instance(TradingOrder, order).filled_qty is None
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# a CANCEL that raced a live FILL -- against the REAL store
#
# ``open_option_orders_book_wide`` filtered on ``not_statuses=terminal`` and
# CANCELED is terminal, so a sell-to-open of 3 that filled 1 before the cancel
# landed left the book entirely -- taking with it the one contract that genuinely
# traded and can still be assigned. The platform knows this state occurs:
# ``AlpacaAccount`` handles "a cancel that raced a live fill leaves the order
# CANCELED with filled_qty > 0" explicitly.
#
# The accessor-level arithmetic is pinned in
# packages/common/tests/test_option_collateral_pledge.py. THESE tests are about
# the QUERY: whether the row is in the list at all, driven through the real store
# and reaching all three views that share it.
# ---------------------------------------------------------------------------
def canceled_after_one_filled(account, *, strike=225.0, ordered=3, filled=1,
                              reserve=None, txn_id=None):
    return add_instance(TradingOrder(
        account_id=account.id, symbol="AAPL", underlying_symbol="AAPL",
        quantity=ordered, filled_qty=filled, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.CANCELED,
        asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol=f"AAPL260101P{int(strike * 1000):08d}",
        option_type=OptionRight.PUT, strike=strike,
        option_strategy=None if reserve is None else "cash_secured_put",
        transaction_id=txn_id if txn_id is not None else open_txn("AAPL"),
        data={} if reserve is None else {"option_reserve": reserve}))


def test_a_cancel_that_raced_a_fill_stays_in_the_book(mock_account):
    """The query, on its own. One contract traded; the row that records it must
    survive its own order's death, exactly as a FILLED row does."""
    order_id = canceled_after_one_filled(mock_account)
    book = mock_account.open_option_orders_book_wide()
    assert [o.id for o in book] == [order_id], \
        "the one contract that genuinely traded left the book with the cancel"


def test_a_cancel_that_raced_a_fill_still_owes_the_contract_it_sold(mock_account):
    """0 was the fail-open answer: the account looked able to take on more
    delivery obligation than it can pay for, because a put it really is short
    stopped counting."""
    mock_account._balance = 100_000.0
    canceled_after_one_filled(mock_account)
    exposure = mock_account.short_put_assignment_exposure()
    assert exposure.cost == pytest.approx(22_500.0)     # ONE strike, not three
    assert exposure.contracts == pytest.approx(1.0)


def test_the_cancelled_remainder_is_not_charged(mock_account):
    """The other direction, and the one that is easy to get wrong. Those two
    contracts were never sold and this order will never sell them; charging
    67,500 would refuse structures the account can plainly afford."""
    mock_account._balance = 100_000.0
    canceled_after_one_filled(mock_account)
    assert mock_account.short_put_assignment_exposure().cost != pytest.approx(67_500.0)


def test_the_RESERVE_POOL_pro_rates_a_cancel_that_raced_a_fill(mock_account):
    """The third view of the same row, and it needs the same discrimination.

    The stored reserve was sized for THREE contracts. One traded, so one strike
    is still committed and two were released by the broker the moment the cancel
    landed. Counting the whole 67,500 would reserve capital against contracts
    that do not exist; counting 0 (the old behaviour) frees capital that is
    genuinely spoken for.
    """
    mock_account._balance = 100_000.0
    canceled_after_one_filled(mock_account, reserve=67_500.0)
    assert mock_account.reserved_option_buying_power() == pytest.approx(22_500.0)


def test_a_cancel_whose_transaction_has_CLOSED_is_gone_for_good(mock_account):
    """"Still open until something closes them" is the whole rule. Once the
    position itself is closed the row must leave the book like any other, or a
    long-dead raced cancel would consume capacity forever."""
    mock_account._balance = 100_000.0
    txn = open_txn("AAPL")
    canceled_after_one_filled(mock_account, reserve=67_500.0, txn_id=txn)
    row = get_instance(Transaction, txn)
    row.status = TransactionStatus.CLOSED
    update_instance(row)

    assert mock_account.open_option_orders_book_wide() == []
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(0.0)
    assert mock_account.reserved_option_buying_power() == pytest.approx(0.0)


def test_another_accounts_short_puts_are_not_this_accounts_problem(mock_account):
    mock_account._balance = 100_000.0
    other = add_instance(TradingOrder(
        account_id=mock_account.id + 999, symbol="AAPL", underlying_symbol="AAPL",
        quantity=1, filled_qty=1, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol="AAPL260101P00225000", option_type=OptionRight.PUT,
        strike=225.0, transaction_id=open_txn("AAPL"), data={}))
    assert other is not None
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(0.0)


def test_the_exposure_is_visible_in_the_in_memory_backtest_store(mock_account):
    """Same defect class as the reserve pool's: a raw SQL read sees an EMPTY table
    while the SQL-less dict-trades store is active, and every gate passes as if the
    book were flat."""
    mock_account._balance = 100_000.0
    with ts.inmem_trades():
        ts.store_add(Transaction(symbol="AAPL", quantity=1, open_price=1.0,
                                 status=TransactionStatus.OPENED,
                                 side=OrderDirection.SELL, id=4242))
        ts.store_add(TradingOrder(
            account_id=mock_account.id, symbol="AAPL", underlying_symbol="AAPL",
            quantity=2, filled_qty=2, side=OrderDirection.SELL,
            order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
            asset_class=AssetClass.OPTION, multiplier=100,
            contract_symbol="AAPL260101P00225000", option_type=OptionRight.PUT,
            strike=225.0, transaction_id=4242, data={}))
        assert mock_account.short_put_assignment_exposure().cost == pytest.approx(45_000.0)


# ==========================================================================
# NO DOUBLE CHARGING
# ==========================================================================
def test_a_csp_reserving_the_full_strike_is_not_charged_twice(mock_account):
    """The SAME order appears in both views at the SAME 22,500 — and each view is
    correct, because each is compared against its own budget.

    The buying-power view: 100,000 balance - 22,500 reserved = 77,500 available.
    The capacity view:     22,500 owed against the 100,000 the account actually holds.

    What must NEVER happen is the capacity total being deducted from available buying
    power (or vice versa): that would report 55,000 available on an account that has
    one fully cash-secured put, and refuse the next trade for money nobody has spent.
    """
    mock_account._balance = 100_000.0
    short_put_order(mock_account, strike=225.0, txn_id=open_txn("AAPL"),
                    reserve=22_500.0)

    assert mock_account.reserved_option_buying_power() == pytest.approx(22_500.0)
    assert mock_account.available_option_buying_power() == pytest.approx(77_500.0)
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(22_500.0)

    # The capacity gate measures against CASH, not against post-reserve buying power.
    # 77,500 of "available BP" would refuse a second 22,500 put at 45,000 owed; the
    # account can plainly afford it.
    assert mock_account.check_assignment_capacity(22_500.0) is True
    assert mock_account.check_assignment_capacity(77_500.0) is True     # exactly 100,000
    assert mock_account.check_assignment_capacity(77_500.01) is False


def test_a_strangle_reserves_a_fraction_of_what_it_would_owe_on_assignment(mock_account):
    """The gap this feature exists to close, measured on one account: the reserve pool
    holds back 2,500 of Reg-T naked margin for a short put that costs 22,500 to take
    delivery on."""
    from ba2_trade_platform.core.interfaces.OptionsAccountInterface import (
        OptionsAccountInterface as OAI)

    mock_account._balance = 100_000.0
    reserve = OAI.option_reserve_required("short_strangle", 1, strike=225.0, spot=250.0,
                                          option_type=OptionRight.PUT)
    short_put_order(mock_account, strike=225.0, txn_id=open_txn("AAPL"), reserve=reserve)

    assert mock_account.reserved_option_buying_power() == pytest.approx(2_500.0)
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(22_500.0)


def test_both_views_read_the_same_orders(mock_account):
    """One book-wide query, two questions. If the two views drifted onto different
    order sets, 'the CSP is in both totals' would stop being provable."""
    mock_account._balance = 100_000.0
    txn = open_txn("AAPL")
    short_put_order(mock_account, strike=225.0, txn_id=txn, reserve=22_500.0)
    ids = {o.id for o in mock_account.open_option_orders_book_wide()}
    assert len(ids) == 1


# ==========================================================================
# THE BOUNDARY
# ==========================================================================
def test_cash_exactly_equal_to_the_assignment_bill_is_allowed(mock_account):
    """Same decision as the pure rail: exactly equal ADMITS. The money is there, and
    every other cap in the option risk path admits at its line."""
    mock_account._balance = 45_000.0
    short_put_order(mock_account, strike=225.0, txn_id=open_txn("AAPL"))
    assert mock_account.check_assignment_capacity(22_500.0) is True
    assert mock_account.check_assignment_capacity(22_500.01) is False


def test_a_negative_additional_cost_cannot_buy_capacity(mock_account):
    mock_account._balance = 45_000.0
    short_put_order(mock_account, strike=225.0, txn_id=open_txn("AAPL"))
    assert mock_account.check_assignment_capacity(-1_000_000.0) is False


def test_an_unknown_additional_cost_refuses_rather_than_crashing(mock_account):
    """A caller who could not price the structure it is about to open has not shown
    that it fits. Refusing is the answer; a TypeError out of a risk gate is not."""
    mock_account._balance = 45_000.0
    assert mock_account.check_assignment_capacity(None) is False


# ==========================================================================
# UNMEASURABLE -- never a permissive default
# ==========================================================================
def test_a_short_put_with_no_strike_is_unmeasurable_not_free(mock_account):
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", underlying_symbol="AAPL", quantity=1,
        filled_qty=1, side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.FILLED, asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol="AAPL260101P00225000", option_type=OptionRight.PUT,
        strike=None, transaction_id=open_txn("AAPL"), data={}))

    exposure = mock_account.short_put_assignment_exposure()
    assert exposure.cost is None
    assert exposure.unmeasurable
    assert any("strike" in u for u in exposure.unmeasurable)
    assert mock_account.check_assignment_capacity(0.0) is False


def test_a_short_option_of_unknown_right_is_unmeasurable_not_a_call(mock_account):
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", underlying_symbol="AAPL", quantity=1,
        filled_qty=1, side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.FILLED, asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol="AAPL260101X00225000", option_type=None, strike=225.0,
        transaction_id=open_txn("AAPL"), data={}))

    exposure = mock_account.short_put_assignment_exposure()
    assert exposure.cost is None
    assert any("option type" in u or "right" in u for u in exposure.unmeasurable)


def test_an_unknown_cash_balance_declines_rather_than_assuming(mock_account):
    mock_account._balance = None
    short_put_order(mock_account, strike=225.0, txn_id=open_txn("AAPL"))
    assert mock_account.check_assignment_capacity(0.0) is False


def test_an_unknown_balance_declines_even_on_an_empty_book(mock_account):
    """The case that separates ``None`` from ``or 0.0``: with nothing owed and nothing
    requested, a balance coerced to 0 would answer 'yes, 0 fits in 0'. We do not know
    what the account holds, so we do not know that it can take delivery of anything."""
    mock_account._balance = None
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(0.0)
    assert mock_account.check_assignment_capacity(0.0) is False


def test_a_sizeless_buy_row_neither_relieves_a_short_nor_poisons_the_book(mock_account):
    """A BUY row with no quantity is a defect, but it is a defect in the SAFE
    direction: failing to net it off leaves the short standing, which overstates the
    bill. Inventing a contract for it would hand back capacity for a close that never
    happened; refusing the whole account over it would be an over-correction that
    switches the gate off wholesale."""
    mock_account._balance = 100_000.0
    txn = open_txn("AAPL")
    short_put_order(mock_account, strike=225.0, txn_id=txn)
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", underlying_symbol="AAPL", quantity=0,
        filled_qty=None, side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
        status=OrderStatus.FILLED, asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol="AAPL260101P00225000", option_type=OptionRight.PUT, strike=225.0,
        transaction_id=txn, data={}))

    exposure = mock_account.short_put_assignment_exposure()
    assert exposure.is_measurable is True
    assert exposure.cost == pytest.approx(22_500.0)


def test_a_data_column_that_is_not_a_mapping_reads_as_unknown_not_as_a_crash(mock_account):
    """``(o.data or {}).get(...)`` on a string raises. The pool is consulted from every
    buying-power gate, so a single malformed row would take out the whole entry path
    with an AttributeError instead of refusing one account cleanly."""
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        transaction_id=open_txn("AAPL"), data="22500"))
    pool = mock_account.reserved_option_buying_power_detail()
    assert pool.is_measurable is False
    assert mock_account.check_option_buying_power(1.0) is False


def test_the_exposure_names_the_order_it_could_not_measure(mock_account):
    """'Something is unmeasurable' is not actionable; 'order 7 has no strike' is.

    Asserted as ``"order <id>"`` rather than as a bare id substring: an OCC contract
    symbol is full of digits, so a bare ``str(order_id) in detail`` passes by accident
    for every small id and pins nothing at all."""
    mock_account._balance = 100_000.0
    order_id = add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", underlying_symbol="AAPL", quantity=1,
        filled_qty=1, side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.FILLED, asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol="AAPL260101P00225000", option_type=OptionRight.PUT,
        strike=None, transaction_id=open_txn("AAPL"), data={}))
    exposure = mock_account.short_put_assignment_exposure()
    assert exposure.is_measurable is False
    assert any(f"order {order_id}" in u for u in exposure.unmeasurable)
    assert any("AAPL260101P00225000" in u for u in exposure.unmeasurable)


def test_a_negative_cash_balance_cannot_fund_anything(mock_account):
    """A margin debit is not cash with the sign filed off. An account $5,000 in the
    hole can fund no delivery at all, not $5,000 of it."""
    mock_account._balance = -5_000.0
    assert mock_account.short_put_assignment_exposure().cost == pytest.approx(0.0)
    assert mock_account.check_assignment_capacity(0.0) is False
    assert mock_account.check_assignment_capacity(1.0) is False


def test_a_fully_closed_short_put_with_a_broken_row_does_not_disarm_the_gate(mock_account):
    """Fail-closed has a limit: a position that is NETTED FLAT cannot be assigned, so a
    missing strike on its rows is a stale data defect and not a live unknown. Treating
    it as unmeasurable would let one bad historical row refuse every future trade on
    the account, which is how a safety control gets switched off wholesale."""
    mock_account._balance = 100_000.0
    txn = open_txn("AAPL")
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", underlying_symbol="AAPL", quantity=1,
        filled_qty=1, side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.FILLED, asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol="AAPL260101P00225000", option_type=OptionRight.PUT, strike=None,
        transaction_id=txn, data={}))
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", underlying_symbol="AAPL", quantity=1,
        filled_qty=1, side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
        status=OrderStatus.FILLED, asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol="AAPL260101P00225000", option_type=OptionRight.PUT, strike=None,
        transaction_id=txn, data={}))

    exposure = mock_account.short_put_assignment_exposure()
    assert exposure.is_measurable is True
    assert exposure.cost == pytest.approx(0.0)
    assert mock_account.check_assignment_capacity(10_000.0) is True


# ==========================================================================
# THE RESERVE POOL'S OWN FAIL-OPEN: an unknown reserve must not FREE buying power
# ==========================================================================
def test_an_open_reserving_order_that_lost_its_reserve_does_not_free_buying_power(mock_account):
    """THE DEFECT: ``float((o.data or {}).get("option_reserve", 0) or 0)``.

    An OPEN ``cash_secured_put`` whose ``data["option_reserve"]`` went missing —
    a failed ``update_instance`` in ``_submit_option_order``'s persist step, a row
    written by an older build, a manual repair — contributed **0**. The account then
    reported the full balance as available and the next structure was waved through on
    money that is already committed to an assignment.

    Unknown is not zero. The gate must refuse."""
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        transaction_id=open_txn("AAPL"), data={}))

    pool = mock_account.reserved_option_buying_power_detail()
    assert pool.is_measurable is False
    assert any("cash_secured_put" in u for u in pool.unmeasurable)
    assert mock_account.check_option_buying_power(1.0) is False
    assert mock_account.available_option_buying_power() is None


def test_a_zero_reserve_strategy_with_no_reserve_key_is_genuinely_zero(mock_account):
    """A long call reserves nothing BY NAME. It must not be dragged into the unknown
    bucket, or the whole debit arm would be permanently unmeasurable."""
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="long_call",
        transaction_id=open_txn("AAPL"), data={}))
    pool = mock_account.reserved_option_buying_power_detail()
    assert pool.is_measurable is True
    assert pool.total == pytest.approx(0.0)
    assert mock_account.available_option_buying_power() == pytest.approx(100_000.0)
    assert mock_account.check_option_buying_power(50_000.0) is True


def test_a_closing_leg_carries_no_reserve_and_that_is_not_unknown(mock_account):
    """``option_strategy="close"`` describes what is being DONE. Both close paths submit
    offsetting legs tagged that way and none of them reserves anything."""
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="close",
        transaction_id=open_txn("AAPL"), data={}))
    assert mock_account.reserved_option_buying_power_detail().is_measurable is True


def test_a_multi_leg_child_carries_no_reserve_and_that_is_not_unknown(mock_account):
    """``submit_option_order`` writes the reserve on the PARENT only; the leg children
    have no ``option_strategy`` at all. Flagging them would make every spread unknown."""
    mock_account._balance = 100_000.0
    txn = open_txn("AAPL")
    parent_id = add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="bear_call_spread",
        transaction_id=txn, data={"option_reserve": 350.0}))
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL260101C00230000", quantity=1,
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
        status=OrderStatus.FILLED, asset_class=AssetClass.OPTION,
        contract_symbol="AAPL260101C00230000", option_type=OptionRight.CALL,
        strike=230.0, parent_order_id=parent_id, transaction_id=txn, data={}))

    pool = mock_account.reserved_option_buying_power_detail()
    assert pool.is_measurable is True
    assert pool.total == pytest.approx(350.0)


@pytest.mark.parametrize("stored", [
    "lots",                 # a string: float("22500") would "work", which is the trap
    {"amount": 22_500.0},   # a nested shape from some future writer
    None,                   # the key exists but holds nothing
    True,                   # a bool IS an int subclass and would price at $1
    float("nan"),           # NaN compares False against every threshold
    float("inf"),
])
def test_a_reserve_stored_as_something_that_is_not_a_number_is_unknown(mock_account, stored):
    """None of these is a number of dollars, and none of them is zero."""
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        transaction_id=open_txn("AAPL"), data={"option_reserve": stored}))
    assert mock_account.reserved_option_buying_power_detail().is_measurable is False
    assert mock_account.check_option_buying_power(1.0) is False


def test_a_reserve_recorded_without_a_priced_strategy_name_is_still_honoured(mock_account):
    """A row that says it reserved 30,000 has reserved 30,000, whatever it calls itself.
    Only the ABSENCE of a reserve is decided by strategy name — a recorded one is money
    that is spoken for, and dropping it would free buying power just as surely."""
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy=None,
        transaction_id=open_txn("AAPL"), data={"option_reserve": 30_000.0}))
    pool = mock_account.reserved_option_buying_power_detail()
    assert pool.is_measurable is True
    assert pool.total == pytest.approx(30_000.0)


def test_an_unreadable_reserve_is_reported_loudly_not_only_returned(mock_account, monkeypatch):
    """'Make it loud.' A caller that never inspects the detail object must still find
    out: this is a repairable data fault that silently disarms every buying-power gate
    on the account until someone notices."""
    import ba2_common.logger as ba2_logger

    shouted = []
    monkeypatch.setattr(ba2_logger.logger, "error",
                        lambda msg, *a, **kw: shouted.append(str(msg)))

    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        transaction_id=open_txn("AAPL"), data={}))
    mock_account.reserved_option_buying_power_detail()

    assert shouted, "an unreadable reserve was swallowed"
    assert "cash_secured_put" in shouted[0]
    assert "option_reserve" in shouted[0]


def test_a_healthy_pool_says_nothing(mock_account, monkeypatch):
    """The corollary: no false alarms, or the real one gets tuned out."""
    import ba2_common.logger as ba2_logger

    shouted = []
    monkeypatch.setattr(ba2_logger.logger, "error",
                        lambda msg, *a, **kw: shouted.append(str(msg)))
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        transaction_id=open_txn("AAPL"), data={"option_reserve": 22_500.0}))
    mock_account.reserved_option_buying_power_detail()
    assert shouted == []


def test_an_explicit_zero_reserve_on_a_reserving_order_is_still_unknown(mock_account):
    """A priced reserving strategy cannot honestly need 0: every branch of
    ``option_reserve_required`` that returns 0 for a RESERVING name does so because a
    sizing input was missing. Reading a stored 0 as 'free' is the same fail-open one
    layer down."""
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        transaction_id=open_txn("AAPL"), data={"option_reserve": 0.0}))
    assert mock_account.reserved_option_buying_power_detail().is_measurable is False


def test_a_measurable_pool_behaves_exactly_as_before(mock_account):
    """The fix must not move a single number on the healthy path."""
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=2, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        transaction_id=open_txn("AAPL"), data={"option_reserve": 30_000.0}))
    assert mock_account.reserved_option_buying_power() == pytest.approx(30_000.0)
    assert mock_account.available_option_buying_power() == pytest.approx(70_000.0)
    assert mock_account.check_option_buying_power(70_000.0) is True
    assert mock_account.check_option_buying_power(70_000.01) is False


def test_an_unmeasurable_reserve_on_a_CLOSED_structure_is_not_the_accounts_problem(mock_account):
    """A released reserve is released whether we could read it or not."""
    mock_account._balance = 100_000.0
    txn_id = open_txn("AAPL")
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        transaction_id=txn_id, data={}))
    stored = get_instance(Transaction, txn_id)
    stored.status = TransactionStatus.CLOSED
    update_instance(stored)
    assert mock_account.reserved_option_buying_power_detail().is_measurable is True


def test_a_zero_requirement_still_passes_the_gate_even_when_the_pool_is_unknown(mock_account):
    """Reserving nothing needs no capacity: refusing here would break every long/debit
    structure the moment one unrelated row lost its reserve."""
    mock_account._balance = 100_000.0
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        transaction_id=open_txn("AAPL"), data={}))
    assert mock_account.check_option_buying_power(0.0) is True
    assert mock_account.check_option_buying_power(0.01) is False


def test_an_unknown_balance_makes_available_buying_power_unknown_not_zero(mock_account):
    """``get_balance() or 0.0`` turned an unreadable balance into a real number. It
    happened to fail closed, but 'we could not read the balance' and 'the balance is
    zero' are still different facts and only one of them is true."""
    mock_account._balance = None
    assert mock_account.available_option_buying_power() is None
    assert mock_account.check_option_buying_power(1.0) is False
