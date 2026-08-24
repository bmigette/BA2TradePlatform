"""The wheel: an option assignment must leave a TradingOrder behind, not just a Transaction.

BUG (this file's reason to exist). ``AlpacaAccount._apply_option_activity`` handled an
OPASN on a short put by creating an equity ``Transaction`` and nothing else. But every
downstream consumer of "how many shares does this expert hold" reads filled equity
ORDER rows, not the transaction:

  * ``_OptionEntryAction._held_equity_shares`` (TradeActions) sums ``filled_qty`` over
    the orders attached to the expert's OPENED transactions -> saw 0.0 for assigned
    stock, so ``SellCoveredCallAction``/``BuyProtectivePutAction`` answered
    "Held equity below one contract lot ... (shares=0.0, 100 required per contract)"
    on every cycle, forever. The second leg of the wheel could never be written.
  * ``TradeManager``'s open-positions pass resolves ``existing_order`` as the oldest
    FILLED order on the transaction whose side matches -> None for assigned stock, which
    silently disables ``DaysOpenedCondition``, ``ProfitLossAmountCondition`` and
    ``ProfitLossPercentCondition`` (all three return False when there is no
    ``existing_order``) and pushes ``CloseAction`` onto its legacy broker-position branch,
    which creates a close order NOT linked to the transaction -- so
    ``has_pending_closing_order`` stays False and the manage pass re-submits a close every
    cycle.

Every test here is written against the reconciler's public entry point
(``reconcile_option_assignments``) with CANNED activity dicts -- no network, no broker
client (the AlpacaAccount is built via ``__new__`` exactly as tests/test_option_assignment.py
does it).

Time: no test asserts anything derived from the wall clock. Where a duration matters
(days_opened) both ends are pinned to fixed 2026 timestamps written into the rows.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlmodel import select

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.db import add_instance, get_db, get_instance, update_instance
from ba2_trade_platform.core.models import Transaction, TradingOrder
from ba2_trade_platform.core.option_types import OptionContract
from ba2_trade_platform.core.TradeActions import create_action
from ba2_trade_platform.core.types import (
    AssetClass, ExpertActionType, OptionRight, OrderDirection, OrderOpenType,
    OrderRecommendation, OrderStatus, OrderType, TransactionStatus,
)


# AAPL @ 150 PUT expiring 2026-01-16 / AAPL @ 160 CALL expiring 2026-01-16
PUT_OCC = "AAPL260116P00150000"
CALL_OCC = "AAPL260116C00160000"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def _make_alpaca(account_id):
    """An AlpacaAccount that can run the DB-backed reconciler with no network."""
    acct = AlpacaAccount.__new__(AlpacaAccount)
    acct.id = account_id
    acct._settings_cache = {"api_key": "k", "api_secret": "s",
                            "paper_account": True, "data_feed": "iex"}
    return acct


def _seed_short_option(account_id, expert_id, occ, right, strike, contracts=1,
                       side=OrderDirection.SELL, underlying="AAPL"):
    """The originating short option: a Transaction + its filled option TradingOrder."""
    opt_txn_id = add_instance(Transaction(
        symbol=occ, quantity=contracts, side=side, status=TransactionStatus.OPENED,
        open_price=2.5, open_date=datetime(2026, 1, 2, tzinfo=timezone.utc),
        expert_id=expert_id,
    ))
    add_instance(TradingOrder(
        account_id=account_id, symbol=occ, quantity=contracts, side=side,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=contracts,
        transaction_id=opt_txn_id, asset_class=AssetClass.OPTION, contract_symbol=occ,
        option_type=right, strike=strike, expiry=date(2026, 1, 16),
        underlying_symbol=underlying,
    ))
    return opt_txn_id


def _assign(acct, occ, contracts, activity_id):
    """Run the reconciler over one canned OPASN activity for ``occ``."""
    return acct.reconcile_option_assignments([{
        "id": activity_id, "activity_type": "OPASN", "symbol": occ,
        "qty": str(contracts), "price": "0",
    }])


def _equity_txns(expert_id, symbol="AAPL", status=TransactionStatus.OPENED):
    with get_db() as session:
        return session.exec(
            select(Transaction)
            .where(Transaction.symbol == symbol)
            .where(Transaction.expert_id == expert_id)
            .where(Transaction.status == status)
        ).all()


def _orders_for(txn_id):
    with get_db() as session:
        return session.exec(
            select(TradingOrder).where(TradingOrder.transaction_id == txn_id)
        ).all()


def _chain_call(strike, *, bid=2.0, ask=2.2, delta=0.30, oi=2000, dte=35):
    return OptionContract(
        symbol=f"AAPL{int(strike)}C", underlying="AAPL", option_type=OptionRight.CALL,
        strike=float(strike), expiry=date.today() + timedelta(days=dte),
        bid=bid, ask=ask, last=(bid + ask) / 2, implied_volatility=0.30, delta=delta,
        gamma=0.02, theta=-0.03, vega=0.1, open_interest=oi, volume=250)


def _chain_put(strike, *, bid=2.0, ask=2.2, delta=-0.30, oi=2000, dte=35):
    return OptionContract(
        symbol=f"AAPL{int(strike)}P", underlying="AAPL", option_type=OptionRight.PUT,
        strike=float(strike), expiry=date.today() + timedelta(days=dte),
        bid=bid, ask=ask, last=(bid + ask) / 2, implied_volatility=0.30, delta=delta,
        gamma=0.02, theta=-0.03, vega=0.1, open_interest=oi, volume=250)


def _capture_submit(monkeypatch, account):
    captured = {}

    def fake(legs, quantity, order_type="limit", limit_price=None, option_strategy=None,
             expert_recommendation_id=None, transaction_id=None):
        captured.update(called=True, legs=legs, quantity=quantity,
                        option_strategy=option_strategy, limit_price=limit_price)
        return TradingOrder(account_id=account.id, symbol="AAPL", quantity=quantity,
                            side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
                            status=OrderStatus.FILLED)

    monkeypatch.setattr(account, "submit_option_order", fake, raising=False)
    return captured


# ---------------------------------------------------------------------------
# 1. THE HEADLINE BUG: assigned shares must be sizeable by the covered-call leg
# ---------------------------------------------------------------------------
def test_assigned_shares_can_size_a_covered_call(monkeypatch, mock_account,
                                                 mock_expert_instance,
                                                 sample_recommendation):
    """A cash-secured put assigned for 2 contracts must let the wheel write 2 calls.

    Pre-fix this failed with ``shares=0.0`` -- the equity Transaction existed but no
    filled equity order did, and _held_equity_shares counts orders.
    """
    acct = _make_alpaca(mock_account.id)
    _seed_short_option(mock_account.id, mock_expert_instance.id, PUT_OCC,
                       OptionRight.PUT, strike=150.0, contracts=2)
    _assign(acct, PUT_OCC, 2, "act-cc-size")

    monkeypatch.setattr(mock_account, "get_option_chain",
                        lambda *a, **k: [_chain_call(160)], raising=False)
    cap = _capture_submit(monkeypatch, mock_account)
    action = create_action(
        action_type=ExpertActionType.SELL_COVERED_CALL, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.HOLD,
        existing_order=None, expert_recommendation=sample_recommendation,
        strike_method="delta", strike_param=0.30, dte_min=20, dte_max=45,
        min_open_interest=100, max_spread_pct=20.0)

    assert action._held_equity_shares() == 200.0, (
        "assignment left no filled equity order, so the covered-call leg sees 0 shares")

    res = action.execute()
    assert res["success"] is True, res["message"]
    assert cap["quantity"] == 2                      # floor(200 / 100)
    assert cap["option_strategy"] == "covered_call"


def test_assigned_shares_can_size_a_protective_put(monkeypatch, mock_account,
                                                   mock_expert_instance,
                                                   sample_recommendation):
    """BuyProtectivePutAction shares the same _held_equity_shares dependency."""
    acct = _make_alpaca(mock_account.id)
    _seed_short_option(mock_account.id, mock_expert_instance.id, PUT_OCC,
                       OptionRight.PUT, strike=150.0, contracts=3)
    _assign(acct, PUT_OCC, 3, "act-pp-size")

    monkeypatch.setattr(mock_account, "get_option_chain",
                        lambda *a, **k: [_chain_put(140)], raising=False)
    cap = _capture_submit(monkeypatch, mock_account)
    action = create_action(
        action_type=ExpertActionType.BUY_PROTECTIVE_PUT, instrument_name="AAPL",
        account=mock_account, order_recommendation=OrderRecommendation.HOLD,
        existing_order=None, expert_recommendation=sample_recommendation,
        strike_method="delta", strike_param=0.30, dte_min=20, dte_max=45,
        min_open_interest=100, max_spread_pct=20.0)

    res = action.execute()
    assert res["success"] is True, res["message"]
    assert cap["quantity"] == 3                      # floor(300 / 100)
    assert cap["option_strategy"] == "protective_put"


# ---------------------------------------------------------------------------
# 2. The synthetic order's SHAPE — it must not read as a real broker fill
# ---------------------------------------------------------------------------
def test_assignment_order_is_an_honest_synthetic_fill(mock_account, mock_expert_instance):
    acct = _make_alpaca(mock_account.id)
    _seed_short_option(mock_account.id, mock_expert_instance.id, PUT_OCC,
                       OptionRight.PUT, strike=150.0, contracts=2)
    _assign(acct, PUT_OCC, 2, "act-shape")

    txns = _equity_txns(mock_expert_instance.id)
    assert len(txns) == 1
    orders = _orders_for(txns[0].id)
    assert len(orders) == 1
    o = orders[0]

    assert o.account_id == mock_account.id
    assert o.symbol == "AAPL"
    assert o.side == OrderDirection.BUY
    assert o.quantity == 200.0 and o.filled_qty == 200.0   # complete the instant it is reported
    assert o.status == OrderStatus.FILLED
    assert o.open_price == 150.0                           # the STRIKE, not the market
    assert o.limit_price is None and o.stop_price is None  # no limit was ever placed
    assert o.order_type == OrderType.MARKET                # plain position-level order
    assert o.asset_class == AssetClass.EQUITY              # explicit: these are SHARES
    assert o.transaction_id == txns[0].id
    assert o.depends_on_order is None                      # this IS the entry
    # The two fields that stop anyone reading this as a trade we placed:
    assert o.open_type == OrderOpenType.EXTERNAL           # not AUTOMATIC, not MANUAL
    assert o.broker_order_id is None                       # no broker order exists to point at
    assert "csp_assignment" in o.comment
    assert PUT_OCC in o.comment and "act-shape" in o.comment
    assert "no broker order" in o.comment


def test_synthetic_order_is_invisible_to_broker_order_reconciliation(mock_account,
                                                                     mock_expert_instance):
    """A minted broker_order_id could collide with a real one; None cannot.

    ``AlpacaAccount.refresh_orders`` walks the BROKER's orders and matches each to a DB
    row by client_order_id or broker_order_id. A row with no broker_order_id can never be
    matched, which is exactly right for an order the broker never issued.
    """
    acct = _make_alpaca(mock_account.id)
    _seed_short_option(mock_account.id, mock_expert_instance.id, PUT_OCC,
                       OptionRight.PUT, strike=150.0, contracts=1)
    _assign(acct, PUT_OCC, 1, "act-nobrokerid")

    txns = _equity_txns(mock_expert_instance.id)
    with get_db() as session:
        matchable = session.exec(
            select(TradingOrder)
            .where(TradingOrder.transaction_id == txns[0].id)
            .where(TradingOrder.broker_order_id.is_not(None))
        ).all()
    assert matchable == []


# ---------------------------------------------------------------------------
# 3. IDEMPOTENCY — the 7-day lookback runs every 5 minutes
# ---------------------------------------------------------------------------
def test_reconciling_the_same_activity_twice_creates_one_order(mock_account,
                                                               mock_expert_instance):
    """The order creation must sit INSIDE the OptionActivity(account_id, activity_id) guard.

    ``reconcile_option_assignments`` is called on a 7-day lookback every 5 minutes, so the
    same OPASN is presented ~2000 times. A second order row would double the expert's
    apparent share count and let the wheel write twice the covered calls it can cover.
    """
    acct = _make_alpaca(mock_account.id)
    _seed_short_option(mock_account.id, mock_expert_instance.id, PUT_OCC,
                       OptionRight.PUT, strike=150.0, contracts=1)

    _assign(acct, PUT_OCC, 1, "act-idem")
    second = _assign(acct, PUT_OCC, 1, "act-idem")
    assert second[0]["result"] == "already_processed"

    txns = _equity_txns(mock_expert_instance.id)
    assert len(txns) == 1                      # one equity transaction
    assert len(_orders_for(txns[0].id)) == 1   # and exactly ONE order on it

    with get_db() as session:
        all_equity_orders = session.exec(
            select(TradingOrder)
            .where(TradingOrder.account_id == mock_account.id)
            .where(TradingOrder.symbol == "AAPL")
        ).all()
    assert len(all_equity_orders) == 1
    assert all_equity_orders[0].filled_qty == 100.0


# ---------------------------------------------------------------------------
# 4. The secondary breakages: everything that hangs off `existing_order`
# ---------------------------------------------------------------------------
def _assigned_position(mock_account, expert_id, occ=PUT_OCC, contracts=2,
                       activity_id="act-secondary"):
    """Run an assignment and return (equity_transaction, resolved_entry_order).

    ``resolve_entry_order`` is TradeManager's OWN resolution (extracted verbatim), so
    these tests exercise the production lookup, not a copy of it.
    """
    from ba2_trade_platform.core.TradeManager import resolve_entry_order

    acct = _make_alpaca(mock_account.id)
    _seed_short_option(mock_account.id, expert_id, occ, OptionRight.PUT,
                       strike=150.0, contracts=contracts)
    _assign(acct, occ, contracts, activity_id)
    txn = _equity_txns(expert_id)[0]
    with get_db() as session:
        return txn, resolve_entry_order(session, txn)


def test_trade_manager_can_resolve_an_entry_order_for_assigned_stock(mock_account,
                                                                     mock_expert_instance):
    """Pre-fix this was None, which is what silently disabled the three conditions below."""
    txn, entry = _assigned_position(mock_account, mock_expert_instance.id,
                                    activity_id="act-resolve")
    assert entry is not None
    assert entry.transaction_id == txn.id
    assert entry.side == txn.side == OrderDirection.BUY
    assert entry.status == OrderStatus.FILLED


def test_days_opened_condition_evaluates_on_assigned_stock(mock_account,
                                                           mock_expert_instance,
                                                           sample_recommendation):
    from ba2_trade_platform.core.TradeConditions import create_condition
    from ba2_trade_platform.core.types import ExpertEventType

    txn, entry = _assigned_position(mock_account, mock_expert_instance.id,
                                    activity_id="act-days")
    # Both ends of the interval are pinned; nothing here reads the wall clock.
    txn.open_date = datetime(2026, 3, 2, tzinfo=timezone.utc)
    update_instance(txn)
    sample_recommendation.created_at = datetime(2026, 3, 14, tzinfo=timezone.utc)

    cond = create_condition(ExpertEventType.N_DAYS_OPENED, mock_account, "AAPL",
                            sample_recommendation, entry, ">=", 10.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(12.0)

    stricter = create_condition(ExpertEventType.N_DAYS_OPENED, mock_account, "AAPL",
                                sample_recommendation, entry, ">=", 20.0)
    assert stricter.evaluate() is False       # evaluates, and says no — not "no order"


def test_profit_loss_conditions_evaluate_on_assigned_stock(mock_account,
                                                           mock_expert_instance,
                                                           sample_recommendation):
    from ba2_trade_platform.core.TradeConditions import create_condition
    from ba2_trade_platform.core.types import ExpertEventType

    txn, entry = _assigned_position(mock_account, mock_expert_instance.id,
                                    activity_id="act-pnl")
    # 200 shares put to us at 150; MockAccount marks AAPL at 150 -> move the basis so the
    # comparison is discriminating rather than an accidental zero.
    txn.open_price = 140.0
    update_instance(txn)

    amount = create_condition(ExpertEventType.N_PROFIT_LOSS_AMOUNT, mock_account, "AAPL",
                              sample_recommendation, entry, ">=", 1000.0)
    assert amount.evaluate() is True
    assert amount.calculated_value == pytest.approx(2000.0)   # (150 - 140) * 200

    percent = create_condition(ExpertEventType.N_PROFIT_LOSS_PERCENT, mock_account, "AAPL",
                               sample_recommendation, entry, ">=", 5.0)
    assert percent.evaluate() is True
    assert percent.calculated_value == pytest.approx(7.1429, abs=1e-4)

    # And they can say NO on their merits, rather than because there is no order.
    too_high = create_condition(ExpertEventType.N_PROFIT_LOSS_PERCENT, mock_account, "AAPL",
                                sample_recommendation, entry, ">=", 50.0)
    assert too_high.evaluate() is False
    assert too_high.calculated_value == pytest.approx(7.1429, abs=1e-4)


def test_close_action_links_its_close_order_to_the_assigned_transaction(
        mock_account, mock_expert_instance, sample_recommendation):
    """Without an entry order CloseAction took the broker-position branch, whose order has
    ``transaction_id = None`` — so ``has_pending_closing_order`` could never see it and the
    manage pass re-submitted a close on every cycle."""
    from ba2_trade_platform.core.models import Position

    txn, entry = _assigned_position(mock_account, mock_expert_instance.id,
                                    activity_id="act-close")
    # A broker position exists, so the legacy branch WOULD have been reachable.
    mock_account._positions = [Position(symbol="AAPL", qty=200.0, side=OrderDirection.BUY,
                                        avg_entry_price=150.0, current_price=150.0,
                                        market_value=30000.0, cost_basis=30000.0,
                                        unrealized_pl=0.0, unrealized_plpc=0.0,
                                        unrealized_intraday_pl=0.0, unrealized_intraday_plpc=0.0,
                                        lastday_price=150.0, change_today=0.0,
                                        qty_available=200.0, asset_class="us_equity",
                                        exchange="NASDAQ", account_id=mock_account.id)]

    action = create_action(
        action_type=ExpertActionType.CLOSE, instrument_name="AAPL", account=mock_account,
        order_recommendation=OrderRecommendation.SELL, existing_order=entry,
        expert_recommendation=sample_recommendation)
    res = action.execute()

    # Delegated to close_transaction(), which links the close to the transaction.
    assert res["data"].get("transaction_id") == txn.id
    with get_db() as session:
        orphans = session.exec(
            select(TradingOrder)
            .where(TradingOrder.account_id == mock_account.id)
            .where(TradingOrder.symbol == "AAPL")
            .where(TradingOrder.transaction_id.is_(None))
        ).all()
    assert orphans == [], (
        "CloseAction fell through to the broker-position branch and created a close order "
        "that is not attached to the transaction")


# ---------------------------------------------------------------------------
# 5. The called-away (short CALL assigned) side
# ---------------------------------------------------------------------------
def test_called_away_records_the_exit_order(mock_account, mock_expert_instance):
    acct = _make_alpaca(mock_account.id)
    _seed_short_option(mock_account.id, mock_expert_instance.id, PUT_OCC,
                       OptionRight.PUT, strike=150.0, contracts=1)
    _assign(acct, PUT_OCC, 1, "act-wheel-put")
    equity_txn_id = _equity_txns(mock_expert_instance.id)[0].id

    _seed_short_option(mock_account.id, mock_expert_instance.id, CALL_OCC,
                       OptionRight.CALL, strike=160.0, contracts=1)
    _assign(acct, CALL_OCC, 1, "act-wheel-call")

    closed = get_instance(Transaction, equity_txn_id)
    assert closed.status == TransactionStatus.CLOSED
    assert closed.close_reason == "called_away"
    assert closed.close_price == 160.0

    orders = sorted(_orders_for(equity_txn_id), key=lambda o: o.id)
    assert len(orders) == 2, "the shares left the book with no order to show for it"
    entry, exit_order = orders
    assert exit_order.side == OrderDirection.SELL
    assert exit_order.status == OrderStatus.FILLED
    assert exit_order.filled_qty == 100.0
    assert exit_order.open_price == 160.0                  # the CALL strike
    assert exit_order.open_type == OrderOpenType.EXTERNAL
    assert exit_order.broker_order_id is None
    assert exit_order.asset_class == AssetClass.EQUITY
    # Marked as a CLOSE so the oldest-first resolutions can never read it as the entry.
    assert exit_order.depends_on_order == entry.id
    assert "called_away" in exit_order.comment and CALL_OCC in exit_order.comment


def test_called_away_closes_the_assigned_lot_not_stock_bought_outright(mock_account,
                                                                       mock_expert_instance):
    """``_find_open_equity_long`` used to take the most recent open BUY, full stop.

    A wheel expert that also holds stock it bought itself would have had THAT position
    closed by an assignment on the wheel's covered call.
    """
    acct = _make_alpaca(mock_account.id)
    _seed_short_option(mock_account.id, mock_expert_instance.id, PUT_OCC,
                       OptionRight.PUT, strike=150.0, contracts=1)
    _assign(acct, PUT_OCC, 1, "act-pref-put")
    assigned_id = _equity_txns(mock_expert_instance.id)[0].id

    # Stock the same expert bought outright, opened AFTER the assignment (higher id, so
    # the old "most recent open BUY" heuristic would have picked this one).
    bought_id = add_instance(Transaction(
        symbol="AAPL", quantity=100.0, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=155.0,
        open_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        expert_id=mock_expert_instance.id))
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=100.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=100.0,
        open_price=155.0, transaction_id=bought_id, asset_class=AssetClass.EQUITY))

    _seed_short_option(mock_account.id, mock_expert_instance.id, CALL_OCC,
                       OptionRight.CALL, strike=160.0, contracts=1)
    _assign(acct, CALL_OCC, 1, "act-pref-call")

    assert get_instance(Transaction, assigned_id).status == TransactionStatus.CLOSED
    assert get_instance(Transaction, bought_id).status == TransactionStatus.OPENED


def test_called_away_of_part_of_a_lot_splits_it_instead_of_erasing_the_rest(
        monkeypatch, mock_account, mock_expert_instance):
    """One contract assigned against a 300-share transaction takes 100 and leaves 200.

    This test used to pin the OPPOSITE: ``_close_txn`` had no partial-close path, so all
    300 were closed and the residual 200 real shares vanished from the ledger while
    sitting at the broker — reported as a "Called-away lot mismatch" warning and nothing
    else. The split landed with tests/test_called_away_partial.py, which covers the
    remainder's basis/open_date/meta_data/entry-order and the over-assignment case; what
    is checked here is that the warning is GONE (writing one call against 300 shares is
    ordinary, not a discrepancy) and that the lot really did split.
    """
    # NOT caplog: ba2_trade_platform.logger installs its own handler with propagate=False,
    # so caplog's root handler never sees the record. Patch the module's own logger. The
    # package's __init__ rebinds the name `AlpacaAccount` to the CLASS, so the module has
    # to come out of sys.modules.
    import sys
    AA = sys.modules["ba2_trade_platform.modules.accounts.AlpacaAccount"]

    warnings = []
    monkeypatch.setattr(AA.logger, "warning",
                        lambda msg, *a, **k: warnings.append(str(msg)))

    acct = _make_alpaca(mock_account.id)
    held_id = add_instance(Transaction(
        symbol="AAPL", quantity=300.0, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=150.0,
        open_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        expert_id=mock_expert_instance.id))
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=300.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=300.0,
        open_price=150.0, transaction_id=held_id, asset_class=AssetClass.EQUITY))

    _seed_short_option(mock_account.id, mock_expert_instance.id, CALL_OCC,
                       OptionRight.CALL, strike=160.0, contracts=1)
    _assign(acct, CALL_OCC, 1, "act-mismatch")

    assert not any("mismatch" in w.lower() for w in warnings), warnings
    # The recorded exit order states the TRUE called-away quantity, not the whole lot.
    exits = [o for o in _orders_for(held_id) if o.side == OrderDirection.SELL]
    assert len(exits) == 1 and exits[0].filled_qty == 100.0
    # ...and the 200 shares that were NOT called away are still on the book.
    held_after = get_instance(Transaction, held_id)
    assert held_after.status == TransactionStatus.CLOSED and held_after.quantity == 100.0
    remaining = _equity_txns(mock_expert_instance.id)
    assert len(remaining) == 1 and remaining[0].quantity == 200.0
    assert remaining[0].id != held_id


# ---------------------------------------------------------------------------
# 6. Reading the assignment link back: has_assigned_shares
# ---------------------------------------------------------------------------
def test_has_assigned_shares_separates_assigned_stock_from_stock_we_bought(
        mock_account, mock_expert_instance, sample_recommendation):
    """``meta_data["origin"] = "csp_assignment"`` was written and never read.

    ``has_buy_position`` fires on ANY equity long the expert holds, so a wheel overlay
    hung off it writes calls over ordinary stock. This is the condition that tells them
    apart — note both cases below leave has_buy_position True.
    """
    from ba2_trade_platform.core.TradeConditions import create_condition
    from ba2_trade_platform.core.types import ExpertEventType

    def _flag(event_type, symbol):
        return create_condition(event_type, mock_account, symbol,
                                sample_recommendation).evaluate()

    # MSFT: bought outright by this expert.
    bought_id = add_instance(Transaction(
        symbol="MSFT", quantity=100.0, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=400.0,
        open_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        expert_id=mock_expert_instance.id))
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="MSFT", quantity=100.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=100.0,
        open_price=400.0, transaction_id=bought_id, asset_class=AssetClass.EQUITY))

    # AAPL: put to us by an assigned short put.
    acct = _make_alpaca(mock_account.id)
    _seed_short_option(mock_account.id, mock_expert_instance.id, PUT_OCC,
                       OptionRight.PUT, strike=150.0, contracts=1)
    _assign(acct, PUT_OCC, 1, "act-flag")

    assert _flag(ExpertEventType.F_HAS_BUY_POSITION, "MSFT") is True
    assert _flag(ExpertEventType.F_HAS_BUY_POSITION, "AAPL") is True
    # ...and only one of them is wheel stock:
    assert _flag(ExpertEventType.F_HAS_ASSIGNED_SHARES, "MSFT") is False
    assert _flag(ExpertEventType.F_HAS_ASSIGNED_SHARES, "AAPL") is True

    # Once called away, the flag goes out with the shares.
    _seed_short_option(mock_account.id, mock_expert_instance.id, CALL_OCC,
                       OptionRight.CALL, strike=160.0, contracts=1)
    _assign(acct, CALL_OCC, 1, "act-flag-call")
    assert _flag(ExpertEventType.F_HAS_ASSIGNED_SHARES, "AAPL") is False


def test_has_assigned_shares_is_a_usable_rule_trigger():
    """It has to survive the whole rule pipeline, not just create_condition()."""
    from ba2_common.core.rule_builders import FLAG_FIELD_EVENT
    from ba2_common.core.rules_convert import FLAG_EVENT_VALUES
    from ba2_trade_platform.core.rules_documentation import get_event_type_documentation
    from ba2_trade_platform.core.types import ExpertEventType

    assert FLAG_FIELD_EVENT["has_assigned_shares"] == ExpertEventType.F_HAS_ASSIGNED_SHARES
    assert "has_assigned_shares" in FLAG_EVENT_VALUES       # classified boolean, not numeric
    doc = get_event_type_documentation()["has_assigned_shares"]
    assert doc["type"] == "boolean" and doc["name"] and doc["description"] and doc["example"]
