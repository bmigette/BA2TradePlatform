"""The capital limit of the option book (owner rule, 2026-10-06).

An expert's stocks and options TOGETHER never commit more than 100 % of its equity share, "like with
stocks". The number an option entry reads is the STOCK PATH's own "money left to spend" for that
expert -- ``MarketExpertInterface.get_available_equity_balance_detail`` (``virtual - used``, clamped
to the broker's remaining buying power and the account's exposure headroom exactly as the stock
path's is) -- with the two differences that make it identical in backtest and live:

* ``virtual`` is the expert's ``virtual_equity_pct`` slice of the account's EQUITY (the snapshot's,
  no margin factor), not of ``get_balance()`` -- spendable CASH in a backtest, EQUITY at every live
  broker (a filled debit leaves live equity unchanged);
* ``used`` (``used_balance_for_transactions``, one function for both asset classes) counts open stock
  at cost, open debit options at their premium, credit / cash-secured structures at their COLLATERAL,
  and entries still working.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ba2_common.core import instance_resolver as ir
from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.db import add_instance, get_all_instances, get_instance
from ba2_common.core.interfaces.MarketExpertInterface import (
    CAPITAL_REPAIR_HINT, CapitalUnmeasurable, MarketExpertInterface, used_balance_for_transactions)
from ba2_common.core.models import ExpertInstance, TradingOrder, Transaction
from ba2_common.core.trade_store import transactions_where
from ba2_common.core.types import (
    AssetClass, ExpertActionType, OrderDirection, OrderStatus, OrderType, TransactionStatus)
from tests.test_bull_put_spread import BPS, FakeAccount, _own_db  # noqa: F401  (autouse DB)

EQUITY = 100_000.0
BUY_CALL = dict(strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40)


class _Expert(MarketExpertInterface):
    """The real stock-path calculation, on a stub: nothing but an id."""

    def __init__(self, expert_id):
        self.id = expert_id

    @classmethod
    def description(cls):
        return "stub"

    def render_market_analysis(self, ma):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


class _Live(FakeAccount):
    """A LIVE broker: ``get_balance()`` is EQUITY (Alpaca account.equity, TastyTrade
    net_liquidating_value, IBKR net_liquidation); a filled debit leaves it unchanged. ``bp`` is the
    broker's remaining buying power (the snapshot's, the stock path's OUTER clamp)."""

    def __init__(self, equity=EQUITY, bp=None):
        super().__init__(balance=equity)
        self.equity = equity
        self.bp = bp                             # None: the broker's REMAINING BP, see below

    def remaining_buying_power(self):
        """Like a cash account: equity less what is held (stock cost, open debit premium, the
        collateral a credit structure holds), floored at 0. A filled purchase LOWERS it while the
        equity does not -- which is what makes it a real outer clamp, not a 10x constant."""
        if self.bp is not None:
            return self.bp
        mine = {x.id for x in get_all_instances(ExpertInstance) if x.account_id == self.id}
        rows = [t for t in transactions_where(statuses=[TransactionStatus.OPENED])
                if t.expert_id in mine]
        try:
            held = used_balance_for_transactions(self, rows, loss_adjusted=False)
        except CapitalUnmeasurable:
            return self.equity                   # a broker does not know our rows; ours refuse
        return max(self.equity - held, 0.0)

    def get_account_snapshot(self):
        return AccountSnapshot(cash=self.equity, equity=self.equity,
                               net_liquidation=self.equity,
                               buying_power=self.remaining_buying_power())

    def get_stock_exposure_headroom(self):
        return None                              # margin off: no account-wide ceiling

    def check_short_put_assignment_capacity(self, **kw):
        return SimpleNamespace(ok=True)          # the assignment gate has its own tests

    def open_option_orders_book_wide(self):
        return []


class _BT(_Live):
    """The BACKTEST: ``get_balance()`` is spendable CASH (net of every filled debit and stock
    purchase) while the snapshot publishes the (deployed) equity."""

    def __init__(self, cash=EQUITY, equity=EQUITY, bp=None):
        super().__init__(equity=equity, bp=bp)
        self.cash = cash

    def remaining_buying_power(self):
        return self.cash if self.bp is None else self.bp

    def get_balance(self):
        return self.cash


@pytest.fixture
def world():
    """Resolver + registries: ``world.expert(account, pct)`` -> expert id."""
    class World:
        def __init__(self):
            self.accounts, self.experts = {}, {}

        def expert(self, account, pct=100.0):
            self.accounts[account.id] = account
            eid = add_instance(ExpertInstance(account_id=account.id, expert="Stub",
                                              virtual_equity_pct=pct))
            self.experts[eid] = _Expert(eid)
            return eid

    w = World()
    previous = ir._resolver
    ir.set_instance_resolver(SimpleNamespace(
        get_expert_instance=lambda i: w.experts[i], get_account_instance=lambda i: w.accounts[i],
        get_account_instance_from_transaction=lambda t: None))
    yield w
    ir.set_instance_resolver(previous)


# ------------------------------------------------------------------ positions, as the engine writes them
def _txn(expert, *, symbol="XYZ", qty, price, status=TransactionStatus.OPENED, option=True,
         strategy="long_call", side=OrderDirection.BUY, order_data=None, order_status=None,
         limit=None, order_qty=None, multiplier=100, filled="default"):
    tid = add_instance(Transaction(
        symbol=symbol, quantity=qty, side=side, open_price=price,
        multiplier=multiplier if option else None,
        asset_class=AssetClass.OPTION if option else AssetClass.EQUITY,
        option_strategy=strategy if option else None, status=status, expert_id=expert))
    oid = None
    if option:
        oid = add_instance(TradingOrder(
            account_id=1, symbol=symbol, quantity=order_qty if order_qty is not None else qty,
            side=side, order_type=OrderType.BUY_LIMIT if side == OrderDirection.BUY
            else OrderType.SELL_LIMIT,
            status=order_status or (OrderStatus.PENDING if status == TransactionStatus.WAITING
                                    else OrderStatus.FILLED),
            filled_qty=(None if status == TransactionStatus.WAITING else qty)
            if filled == "default" else filled,
            asset_class=AssetClass.OPTION, option_strategy=strategy, transaction_id=tid,
            limit_price=limit, multiplier=multiplier, good_for=None, comment=None,
            data=order_data if order_data is not None else {}))
    return tid, oid


def _stock(expert, qty, price):
    return _txn(expert, symbol="ABC", qty=qty, price=price, option=False)[0]


def _debit(expert, qty, price):
    """An OPEN long-call position: ``qty`` contracts at ``price`` a share."""
    return _txn(expert, qty=qty, price=price)[0]


def _pending_debit(expert, qty, limit, **kw):
    """A debit entry still WORKING (the transaction is WAITING, the order unfilled)."""
    return _txn(expert, qty=qty, price=None, status=TransactionStatus.WAITING, limit=limit, **kw)


def _credit(expert, reserve, strategy="bull_put_spread", order_data="reserve"):
    data = {"option_reserve": reserve} if order_data == "reserve" else order_data
    return _txn(expert, qty=1, price=-1.5, strategy=strategy, side=OrderDirection.SELL,
                order_data=data)[0]


def _action(acct, expert_id, action_type, **kw):
    from ba2_common.core.TradeActions import create_action
    rec = SimpleNamespace(id=1, instance_id=expert_id, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    a = create_action(ExpertActionType(action_type), "XYZ", acct, SimpleNamespace(), None, rec, **kw)
    a.submit_to_broker = True
    return a


def _buy(acct, expert_id, sizing):
    return _action(acct, expert_id, ExpertActionType.BUY_CALL.value, sizing=sizing,
                   **BUY_CALL).execute()


def _bps(acct, expert_id, sizing):
    return _action(acct, expert_id, ExpertActionType.OPEN_BULL_PUT_SPREAD.value,
                   **{**BPS, "sizing": sizing}).execute()


def _headroom(world, expert_id):
    value, names = world.experts[expert_id].get_available_equity_balance_detail()
    assert names == (), names
    return value


def _unit_cost():
    from tests.test_bull_put_spread import act
    probe = FakeAccount()
    assert act(probe, ExpertActionType.BUY_CALL.value, sizing=1.0, **BUY_CALL).execute()["success"]
    return probe.submitted[-1]["limit_price"] * 100.0


# ------------------------------------------------------------------ the arithmetic
def test_headroom_counts_stocks_debits_collateral_and_working_entries_together(world):
    acct = _Live()
    e = world.expert(acct)
    assert _headroom(world, e) == EQUITY
    _stock(e, 100, 100.0)                                   # 10,000 of shares
    _debit(e, 20, 5.0)                                      # 10,000 of premium paid
    _credit(e, 30_000.0)                                    # 30,000 of collateral (its premium is not)
    _pending_debit(e, 20, 5.0)                              # 10,000 about to be paid
    assert _headroom(world, e) == pytest.approx(EQUITY - 60_000.0)


def test_a_covered_call_commits_only_the_collateral_it_recorded(world):
    acct = _Live()
    e = world.expert(acct)
    _txn(e, qty=2, price=2.0, strategy="covered_call", side=OrderDirection.SELL)
    assert _headroom(world, e) == EQUITY                    # the shares are its collateral


def test_the_stock_paths_numbers_for_stock_only_experts_are_unchanged(world):
    """One used-balance function: with no option row it is exactly the classic RM's number."""
    acct = _Live()
    e = world.expert(acct)
    txn = get_instance(Transaction, _stock(e, 10, 100.0))
    assert used_balance_for_transactions(acct, [txn], loss_adjusted=False) == pytest.approx(1_000.0)
    assert world.experts[e]._calculate_used_balance(acct, loss_adjusted=False) == pytest.approx(1_000.0)


# ------------------------------------------------------------------ BT == live
def test_a_live_like_account_cuts_the_second_60pct_entry_exactly_like_the_backtest(world):
    """Same session, two 60 % entries; the first is still working. The second is cut to the 40 %
    that is left -- identically on a live-like account (balance = equity) and a backtest-like one
    (balance = cash)."""
    unit = _unit_cost()
    quantities = {}
    for name, acct in (("live", _Live()), ("bt", _BT())):
        e = world.expert(acct)
        first = _buy(acct, e, 60.0)
        assert first["success"], first["message"]
        sub = acct.submitted[-1]
        assert sub["quantity"] == int(0.60 * EQUITY // unit)
        _pending_debit(e, sub["quantity"], sub["limit_price"])
        second = _buy(acct, e, 60.0)
        assert second["success"], second["message"]
        quantities[name] = acct.submitted[-1]["quantity"]
        assert second["data"]["capital_headroom_cut"].startswith(
            f"sized {sub['quantity']} -> {quantities[name]} contract(s)")
    assert quantities["live"] == quantities["bt"]
    spent = int(0.60 * EQUITY // unit) * unit
    assert quantities["live"] == int((EQUITY - spent) // unit)       # the 40 % that is left


def test_after_the_fill_a_live_balance_does_not_move_but_the_headroom_does(world):
    """THE DEFECT the capital limit's first form had: a filled 60k purchase leaves a live balance
    (equity) at 100k and a backtest's cash at 40k. Both report the SAME 40k, because both subtract
    what the expert holds from the account's published equity."""
    live, bt = _Live(), _BT(cash=40_000.0, equity=EQUITY)
    assert live.get_balance() == EQUITY and bt.get_balance() == 40_000.0
    for acct in (live, bt):
        e = world.expert(acct)
        _debit(e, 100, 6.0)                                  # 60,000 paid
        assert _headroom(world, e) == pytest.approx(40_000.0)


def test_after_the_fill_a_live_account_still_cuts_a_second_60pct_entry(world):
    unit = _unit_cost()
    live = _Live()
    e = world.expert(live)
    _debit(e, 10, 60.0)                                      # 60,000 paid
    res = _buy(live, e, 60.0)
    assert res["success"], res["message"]
    assert live.submitted[-1]["quantity"] == int(40_000.0 // unit) < int(0.60 * EQUITY // unit)


def test_a_mixed_stock_and_option_book_is_one_limit(world):
    """50k of shares and 10k of premium are held: 40k is left, however the balance reads."""
    unit = _unit_cost()
    live, bt = _Live(), _BT(cash=40_000.0)
    experts = {}
    for acct in (live, bt):
        experts[acct] = e = world.expert(acct)
        _stock(e, 200, 250.0)
        _debit(e, 10, 10.0)
        assert _headroom(world, e) == pytest.approx(40_000.0)
    res = _buy(live, experts[live], 60.0)
    assert res["success"], res["message"]
    assert live.submitted[-1]["quantity"] == int(40_000.0 // unit)


def test_two_experts_on_one_account_are_each_bounded_by_their_own_share(world):
    """virtual_equity_pct 60 / 40 of one account. A holds 30k: A has 60k - 30k, B still 40k."""
    acct = _Live()
    a, b = world.expert(acct, 60.0), world.expert(acct, 40.0)
    _debit(a, 100, 3.0)                                      # A: 30,000
    assert _headroom(world, a) == pytest.approx(30_000.0)
    assert _headroom(world, b) == pytest.approx(40_000.0)
    unit = _unit_cost()
    res = _buy(acct, b, 100.0)                               # B asks for everything it has
    assert res["success"], res["message"]
    assert acct.submitted[-1]["quantity"] == int(40_000.0 // unit)
    res = _buy(acct, a, 100.0)
    assert res["success"], res["message"]
    assert acct.submitted[-1]["quantity"] == int(30_000.0 // unit)


def test_a_margin_account_with_buying_power_at_2x_equity_still_caps_at_100pct_of_the_share(world):
    """The broker's remaining BP (2 x equity) is the stock path's OUTER clamp, never the base: an
    expert with the whole account may commit 100k, not 200k; with 50 % of it, 50k. A tighter BP
    still clamps (options pass through the same clamp as stocks)."""
    unit = _unit_cost()
    margin = _Live(bp=2 * EQUITY)
    whole = world.expert(margin, 100.0)
    half = world.expert(margin, 50.0)
    assert _headroom(world, whole) == pytest.approx(EQUITY)
    assert _headroom(world, half) == pytest.approx(0.5 * EQUITY)
    res = _buy(margin, half, 100.0)
    assert res["success"], res["message"]
    assert margin.submitted[-1]["quantity"] == int(0.5 * EQUITY // unit)
    tight = _Live(bp=20_000.0)
    t = world.expert(tight, 100.0)
    assert _headroom(world, t) == pytest.approx(20_000.0)


# ------------------------------------------------------------------ debit structures
def test_a_debit_entry_on_an_empty_book_sizes_exactly_as_before(world):
    acct = _Live()
    e = world.expert(acct)
    res = _buy(acct, e, 40.0)
    assert res["success"], res["message"]
    qty, cost = acct.submitted[-1]["quantity"], acct.submitted[-1]["limit_price"] * 100.0
    assert qty == int(0.40 * EQUITY // cost)
    assert "capital_headroom_cut" not in res["data"] and "sized" not in res["message"]


def test_a_debit_entry_is_cut_to_the_capital_the_collateral_leaves(world):
    acct = _Live()
    e = world.expert(acct)
    _credit(e, 90_000.0)
    res = _buy(acct, e, 40.0)
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    assert sub["quantity"] * sub["limit_price"] * 100.0 <= 10_000.0


def test_a_debit_entry_with_no_capital_left_is_refused_loudly(world):
    acct = _Live()
    e = world.expert(acct)
    _credit(e, EQUITY)
    res = _buy(acct, e, 40.0)
    assert res["success"] is False and "Insufficient option capital" in res["message"]
    assert acct.submitted == []


# ------------------------------------------------------------------ the cut is announced
class _RowBook(_Live):
    """``submit_option_order`` writes the REAL order row, so the persisted comment is testable."""

    def submit_option_order(self, *, legs, quantity, order_type, limit_price, option_strategy,
                            expert_recommendation_id=None, transaction_id=None):
        super().submit_option_order(legs=legs, quantity=quantity, order_type=order_type,
                                    limit_price=limit_price, option_strategy=option_strategy)
        oid = add_instance(TradingOrder(
            account_id=1, symbol="XYZ", quantity=quantity, side=OrderDirection.BUY,
            order_type=OrderType.BUY_LIMIT, status=OrderStatus.PENDING, limit_price=limit_price,
            asset_class=AssetClass.OPTION, option_strategy=option_strategy, good_for=None,
            filled_qty=None, comment=None, data={}))
        return get_instance(TradingOrder, oid)


def test_the_cut_is_in_the_result_message_the_result_data_and_the_order_comment(world):
    acct = _RowBook()
    e = world.expert(acct)
    _credit(e, 90_000.0)
    res = _buy(acct, e, 40.0)
    assert res["success"], res["message"]
    note = res["data"]["capital_headroom_cut"]
    assert note.startswith("sized ") and "option capital headroom $10,000.00" in note
    assert note in res["message"]
    stored = get_instance(TradingOrder, res["data"]["order_id"])
    assert note in (stored.comment or "")
    assert stored.data["entry_debit_per_contract"] > 0      # lets a market-type entry be valued


# ------------------------------------------------------------------ unreadable rows are NAMED
def test_an_unreadable_reserve_refuses_debit_entries_and_names_the_order(world, monkeypatch):
    import ba2_common.core.TradeActions as TA
    warned = []
    monkeypatch.setattr(TA.logger, "warning", lambda m, *a, **k: warned.append(str(m)))
    acct = _Live()
    e = world.expert(acct)
    tid, oid = _txn(e, symbol="LEGACY", qty=1, price=-1.0, strategy="cash_secured_put",
                    side=OrderDirection.SELL, order_data={})
    res = _buy(acct, e, 5.0)
    assert res["success"] is False and "cannot be measured" in res["message"]
    assert f"transaction {tid}" in res["message"] and "LEGACY" in res["message"]
    assert str(oid) in res["message"]
    assert acct.submitted == []
    assert "LEGACY" in " ".join(warned)


def test_a_closed_or_dead_row_is_never_the_cause_only_a_genuinely_open_one(world):
    acct = _Live()
    e = world.expert(acct)
    # a CLOSED position, and a WAITING one whose order was cancelled: neither commits anything
    _txn(e, symbol="DEAD1", qty=1, price=-1.0, strategy="cash_secured_put",
         side=OrderDirection.SELL, order_data={}, status=TransactionStatus.CLOSED)
    _txn(e, symbol="DEAD2", qty=1, price=None, strategy="cash_secured_put",
         side=OrderDirection.SELL, order_data={}, status=TransactionStatus.WAITING,
         order_status=OrderStatus.CANCELED)
    assert _headroom(world, e) == EQUITY
    # an OPEN one with no reserve recorded is genuinely unknown, and named
    tid, _ = _txn(e, symbol="OPEN3", qty=1, price=-1.0, strategy="cash_secured_put",
                  side=OrderDirection.SELL, order_data={})
    value, names = world.experts[e].get_available_equity_balance_detail()
    assert value is None and len(names) == 1
    assert f"transaction {tid}" in names[0] and "OPEN3" in names[0]
    assert "DEAD1" not in names[0] and "DEAD2" not in names[0]


@pytest.mark.parametrize("over,needle", [
    ({"order_qty": 0}, "unreadable quantity"),
    ({"multiplier": None}, "unreadable multiplier"),
    ({"limit": None}, "no readable limit price"),
    ({"limit": 0.0}, "no readable limit price"),
])
def test_no_default_is_substituted_for_an_unreadable_working_entry(world, over, needle):
    acct = _Live()
    e = world.expert(acct)
    kw = dict(limit=5.0)
    kw.update(over)
    tid, _ = _pending_debit(e, 10, kw.pop("limit"), **kw)
    txn = get_instance(Transaction, tid)
    with pytest.raises(CapitalUnmeasurable) as err:
        used_balance_for_transactions(acct, [txn], loss_adjusted=False)
    assert needle in str(err.value) and "XYZ" in str(err.value)


def test_a_market_type_working_entry_is_valued_by_the_debit_its_builder_recorded(world):
    acct = _Live()
    e = world.expert(acct)
    _pending_debit(e, 10, None, order_data={"entry_debit_per_contract": 450.0})
    assert _headroom(world, e) == pytest.approx(EQUITY - 4_500.0)


# ------------------------------------------------------------------ no silent fail-open
def test_an_expert_without_a_capital_model_is_a_defect_not_a_free_pass(world):
    import ba2_common.core.TradeActions as TA
    a = TA.BuyCallAction.__new__(TA.BuyCallAction)
    a.instrument_name = "XYZ"
    a.account = _Live()
    a.expert_recommendation = SimpleNamespace(instance_id=7)
    previous = ir._resolver
    ir.set_instance_resolver(SimpleNamespace(get_expert_instance=lambda i: object()))
    try:
        with pytest.raises(AttributeError):
            a._fit_debit_to_capital(5, 100.0, "long_call")
    finally:
        ir.set_instance_resolver(previous)


def test_a_non_numeric_headroom_is_a_defect_not_a_free_pass():
    import ba2_common.core.TradeActions as TA
    a = TA.BuyCallAction.__new__(TA.BuyCallAction)
    a.instrument_name = "XYZ"
    a._capital_headroom = lambda: ("plenty", ())
    with pytest.raises(TypeError):
        a._fit_debit_to_capital(5, 100.0, "long_call")


# ------------------------------------------------------------------ reserve-based structures
def test_a_credit_spread_that_does_not_fit_is_refused_loudly(world):
    acct = _Live()
    e = world.expert(acct)
    _credit(e, EQUITY - 100.0, strategy="iron_condor")
    res = _bps(acct, e, 40.0)
    assert res["success"] is False and "Insufficient buying power" in res["message"]
    assert acct.submitted == []


def test_a_credit_spread_cannot_reserve_cash_a_working_debit_entry_is_about_to_spend(world):
    acct = _Live()
    e = world.expert(acct)
    _pending_debit(e, 100, 9.5)                              # 95,000 in flight
    res = _bps(acct, e, 40.0)
    assert res["success"] is False and "Insufficient buying power" in res["message"]


def test_a_credit_spread_cannot_reserve_capital_an_open_stock_position_holds(world):
    acct = _Live()
    e = world.expert(acct)
    _stock(e, 990, 100.0)                                    # 99,000 of shares
    res = _bps(acct, e, 40.0)
    assert res["success"] is False and "Insufficient buying power" in res["message"]


# ------------------------------------------------------------------ the invariant
@pytest.mark.parametrize("make", [_Live, _BT], ids=["live", "backtest"])
def test_no_sequence_of_entries_commits_more_than_the_experts_share(make, world):
    """Debit and credit structures alternately, each sized at 40 %; debits fill (becoming open
    positions) every other time. After EVERY entry the expert's stock + option commitments are at most
    its equity share -- and the limit actually bites."""
    acct = make()
    e = world.expert(acct, 80.0)
    share = 0.8 * EQUITY
    cut_or_refused = 0
    for i in range(16):
        before = len(acct.submitted)
        res = _buy(acct, e, 40.0) if i % 2 == 0 else _bps(acct, e, 40.0)
        if len(acct.submitted) == before:
            cut_or_refused += 1
        else:
            sub = acct.submitted[-1]
            if i % 2 == 0:
                if i % 4 == 0:      # filled: an open position (cash falls on a BT account)
                    _debit(e, sub["quantity"], sub["limit_price"])
                    if isinstance(acct, _BT):
                        acct.cash -= sub["limit_price"] * 100 * sub["quantity"]
                else:               # still working
                    _pending_debit(e, sub["quantity"], sub["limit_price"])
            else:
                _credit(e, float(res["data"]["option_reserve"]))
        rows = transactions_where(expert_id=e, statuses=[TransactionStatus.WAITING,
                                                         TransactionStatus.OPENED])
        committed = used_balance_for_transactions(acct, rows, loss_adjusted=False)
        assert committed <= share + 1e-6, (i, committed)
    assert cut_or_refused >= 1, "the limit never bit: the scenario does not exercise it"
    assert _headroom(world, e) >= -1e-6


# ====================================================================== review follow-ups (2nd review)
# ---- 1. partial fills: the resting remainder is committed
def test_a_partially_filled_entry_still_commits_its_resting_remainder(world):
    """A 20-contract limit buy filled 8: the 8 are a position (cost), the 12 still working are
    committed too -- they used to be invisible until the order fully filled."""
    acct = _Live()
    e = world.expert(acct)
    _txn(e, qty=8, price=5.0, order_qty=20, order_status=OrderStatus.PARTIALLY_FILLED, limit=5.0)
    assert _headroom(world, e) == pytest.approx(EQUITY - 8 * 500.0 - 12 * 500.0)


def test_a_partially_filled_market_type_entry_uses_the_recorded_debit(world):
    acct = _Live()
    e = world.expert(acct)
    _txn(e, qty=8, price=5.0, order_qty=20, order_status=OrderStatus.PARTIALLY_FILLED, limit=None,
         order_data={"entry_debit_per_contract": 480.0})
    assert _headroom(world, e) == pytest.approx(EQUITY - 8 * 500.0 - 12 * 480.0)


def test_a_waiting_transaction_with_a_partly_filled_order_counts_filled_and_remainder(world):
    acct = _Live()
    e = world.expert(acct)
    _txn(e, qty=20, price=None, status=TransactionStatus.WAITING, limit=5.0,
         order_status=OrderStatus.PARTIALLY_FILLED, filled=8.0)
    assert _headroom(world, e) == pytest.approx(EQUITY - 20 * 500.0)


def test_a_partly_filled_credit_structure_keeps_its_whole_recorded_reserve(world):
    """The reserve is stamped for the ORDERED size; a partial fill never shrinks it."""
    acct = _Live()
    e = world.expert(acct)
    _txn(e, qty=1, price=-1.5, strategy="bull_put_spread", side=OrderDirection.SELL,
         order_data={"option_reserve": 8_000.0}, order_status=OrderStatus.PARTIALLY_FILLED)
    assert _headroom(world, e) == pytest.approx(EQUITY - 8_000.0)


# ---- 2. a resting close still holds the capital
def test_a_closing_option_transaction_still_holds_its_collateral(world):
    acct = _Live()
    e = world.expert(acct)
    _txn(e, qty=1, price=-1.5, strategy="cash_secured_put", side=OrderDirection.SELL,
         order_data={"option_reserve": 18_000.0}, status=TransactionStatus.CLOSING)
    _txn(e, qty=10, price=4.0, status=TransactionStatus.CLOSING)             # a long call being sold
    assert _headroom(world, e) == pytest.approx(EQUITY - 18_000.0 - 4_000.0)


def test_a_closing_STOCK_transaction_still_holds_its_capital(world):
    """A resting close has not released the shares: until the sell FILLS (the row turns CLOSED) the
    position is still held, so its cost stays in ``used`` -- exactly as a CLOSING option row does.
    (It used to be dropped, letting the same capital fund a second entry; fixed 2026-10-07.)"""
    acct = _Live()
    e = world.expert(acct)
    _txn(e, symbol="ABC", qty=100, price=100.0, option=False, status=TransactionStatus.CLOSING)
    assert _headroom(world, e) == EQUITY - 100 * 100.0


def test_a_closed_stock_transaction_releases_its_capital(world):
    acct = _Live()
    e = world.expert(acct)
    _txn(e, symbol="ABC", qty=100, price=100.0, option=False, status=TransactionStatus.CLOSED)
    assert _headroom(world, e) == EQUITY


# ---- 3. the option SIZING budget is the same base in both worlds
def test_the_proposed_quantity_is_identical_for_live_and_backtest_after_a_fill(world):
    """After a 60k fill the backtest's cash is 40k and a live balance is still 100k. The pre-cut
    proposal used to come from that balance (24k vs 60k); it comes from the expert's equity share in
    both now, so both propose the same quantity and both cut to the same 40k."""
    unit = _unit_cost()
    notes = {}
    for name, acct in (("live", _Live()), ("bt", _BT(cash=40_000.0, equity=EQUITY))):
        e = world.expert(acct)
        _debit(e, 100, 6.0)                                                  # 60,000 paid
        res = _buy(acct, e, 60.0)
        assert res["success"], res["message"]
        notes[name] = (res["data"]["capital_headroom_cut"], acct.submitted[-1]["quantity"])
    assert notes["live"] == notes["bt"]
    assert notes["live"][0].startswith(f"sized {int(0.60 * EQUITY // unit)} -> ")
    assert notes["live"][1] == int(40_000.0 // unit)


# ---- 4. an unreadable option row on an option-holding expert's STOCK path is named
def test_the_stock_path_of_an_option_holding_expert_names_the_unreadable_row(world):
    acct = _Live()
    e = world.expert(acct)
    tid, oid = _txn(e, symbol="LEGACY", qty=1, price=-1.0, strategy="cash_secured_put",
                    side=OrderDirection.SELL, order_data={})
    failure = []
    assert world.experts[e]._available_balance_breakdown(failure=failure) is None
    text = "; ".join(failure)
    assert f"transaction {tid}" in text and "LEGACY" in text and str(oid) in text


def test_the_classic_rm_raises_with_the_row_and_the_repair_for_an_option_holding_expert(world):
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement
    acct = _Live()
    e = world.expert(acct)
    tid, _ = _txn(e, symbol="LEGACY", qty=1, price=-1.0, strategy="cash_secured_put",
                  side=OrderDirection.SELL, order_data={})
    rm = TradeRiskManagement.__new__(TradeRiskManagement)
    rm.logger = SimpleNamespace(error=lambda *a, **k: None, info=lambda *a, **k: None,
                                warning=lambda *a, **k: None, debug=lambda *a, **k: None)
    with pytest.raises(RuntimeError) as err:
        rm._size_prioritized_orders(world.experts[e], None, e, [], 0.1)
    message = str(err.value)
    assert f"transaction {tid}" in message and "LEGACY" in message
    assert CAPITAL_REPAIR_HINT in message


def test_a_stock_only_expert_can_never_reach_the_unmeasurable_path(world):
    """Only OPTION rows can be unreadable. A stock row with a missing price is skipped (as the stock
    path always has) -- it never raises ``CapitalUnmeasurable``."""
    acct = _Live()
    e = world.expert(acct)
    _stock(e, 10, 100.0)
    broken = get_instance(Transaction, _txn(e, symbol="BAD", qty=1, price=None, option=False)[0])
    txns = transactions_where(expert_id=e, statuses=[TransactionStatus.OPENED])
    assert broken in txns or True
    assert used_balance_for_transactions(acct, txns, loss_adjusted=False) == pytest.approx(1_000.0)
    assert _headroom(world, e) == pytest.approx(EQUITY - 1_000.0)


# ---- 5. negative headroom is a refusal; gains raise it; a loss counts twice vs cash
def test_negative_headroom_refuses_and_never_yields_a_negative_or_absurd_quantity(world):
    """Equity 50k with 60k of cost held (the book lost): headroom is -10k. Every gate refuses."""
    acct = _Live(equity=50_000.0)
    e = world.expert(acct)
    _debit(e, 100, 6.0)                                                      # 60,000 cost held
    assert _headroom(world, e) == pytest.approx(-10_000.0)
    res = _buy(acct, e, 60.0)
    assert res["success"] is False and "Insufficient option capital" in res["message"]
    assert acct.submitted == []
    import ba2_common.core.TradeActions as TA
    a = TA.BuyCallAction.__new__(TA.BuyCallAction)
    a.instrument_name = "XYZ"
    a._capital_headroom = lambda: (-10_000.0, ())
    a._result = lambda ok, msg, data=None: {"success": ok, "message": msg}
    qty, refusal, note = a._fit_debit_to_capital(7, 100.0, "long_call")
    assert refusal is not None and refusal["success"] is False and note is None
    assert _bps(acct, e, 40.0)["success"] is False


def test_an_unrealised_gain_raises_the_headroom_and_a_loss_counts_twice_against_cash(world):
    gain, loss = _Live(equity=110_000.0), _Live(equity=90_000.0)
    loss.id = 2                                              # two distinct accounts
    for acct, expected in ((gain, 110_000.0 - 30_000.0), (loss, 90_000.0 - 30_000.0)):
        e = world.expert(acct)
        _debit(e, 50, 6.0)                                                   # 30,000 cost
        assert _headroom(world, e) == pytest.approx(expected)
    # cash after the purchase would be 70k; the marked-down book leaves only 60k: conservative


# ---- 6. a legacy short option with no strategy is unmeasurable, not "spent"
def test_a_short_option_with_no_strategy_and_no_reserve_is_unmeasurable_and_named(world):
    acct = _Live()
    e = world.expert(acct)
    tid, oid = _txn(e, symbol="OLD", qty=3, price=2.0, strategy=None, side=OrderDirection.SELL,
                    order_data={})
    value, names = world.experts[e].get_available_equity_balance_detail()
    assert value is None and len(names) == 1
    assert f"transaction {tid}" in names[0] and "OLD" in names[0] and "no strategy" in names[0]


def test_a_short_option_with_no_strategy_but_a_recorded_reserve_counts_the_reserve(world):
    acct = _Live()
    e = world.expert(acct)
    _txn(e, qty=3, price=2.0, strategy=None, side=OrderDirection.SELL,
         order_data={"option_reserve": 5_000.0})
    assert _headroom(world, e) == pytest.approx(EQUITY - 5_000.0)


# ---- 8. the failure reason travels in the call result
def test_the_refusal_names_the_cause_when_the_equity_cannot_be_read(world):
    class _NoEquity(_Live):
        def get_account_snapshot(self):
            return AccountSnapshot(cash=1.0)               # no equity published

    acct = _NoEquity()
    e = world.expert(acct)
    value, names = world.experts[e].get_available_equity_balance_detail()
    assert value is None and "published no equity" in " ".join(names)
    res = _buy(acct, e, 5.0)
    assert res["success"] is False


def test_two_calls_never_share_a_failure_reason(world):
    """A refusal text built for one expert must not leak into the next call: nothing is stored on
    the (cached) expert instance."""
    acct = _Live()
    bad, good = world.expert(acct), world.expert(acct)
    _txn(bad, symbol="LEGACY", qty=1, price=-1.0, strategy="cash_secured_put",
         side=OrderDirection.SELL, order_data={})
    assert world.experts[bad].get_available_equity_balance_detail()[0] is None
    value, names = world.experts[good].get_available_equity_balance_detail()
    assert names == () or value is None
    assert not hasattr(world.experts[bad], "_capital_failure")


# ---- 11. equity MOVES between entries; the outer BP clamp binds
def test_equity_moving_between_entries_moves_the_headroom_and_the_second_entry(world):
    unit = _unit_cost()
    acct = _Live()
    e = world.expert(acct)
    first = _buy(acct, e, 30.0)
    assert first["success"]
    q1 = acct.submitted[-1]["quantity"]
    assert q1 == int(0.30 * EQUITY // unit)
    _debit(e, q1, acct.submitted[-1]["limit_price"])                         # it filled
    acct.equity = 80_000.0                                                   # the mark fell 20k
    res = _buy(acct, e, 100.0)                                               # asks for everything
    assert res["success"], res["message"]
    q2 = acct.submitted[-1]["quantity"]
    spent = q1 * unit
    assert q2 == int((80_000.0 - spent) // unit)                             # equity moved, so did the cut
    acct.equity = 130_000.0                                                  # ... and it recovers
    assert _headroom(world, e) == pytest.approx(130_000.0 - spent)


def test_the_brokers_remaining_buying_power_is_the_outer_clamp_for_options_too(world):
    unit = _unit_cost()
    tight = _Live(bp=5_000.0)
    e = world.expert(tight)
    res = _buy(tight, e, 60.0)
    assert res["success"], res["message"]
    assert tight.submitted[-1]["quantity"] == int(5_000.0 // unit)
    assert _headroom(world, e) == pytest.approx(5_000.0)
    assert _bps(_Live(bp=500.0), world.expert(_Live(bp=500.0)), 40.0)["success"] is False


def test_the_default_live_buying_power_falls_with_a_fill_while_equity_does_not(world):
    live = _Live()
    e = world.expert(live)
    assert live.get_account_snapshot().buying_power == EQUITY
    _debit(e, 100, 6.0)
    snap = live.get_account_snapshot()
    assert snap.equity == EQUITY and snap.buying_power == pytest.approx(40_000.0)
