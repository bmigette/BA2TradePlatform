"""OPT-S3: one settled leg must not close a whole multi-leg option structure.

THE LIVE DOOR, and the only one that matters for a real account. A broker
assignment / exercise / expiry arrives as an ACTIVITY and is applied by
``AlpacaAccount._apply_option_activity``. All four of its settlement branches used
to call ``_close_txn(opt_txn, ...)``, which sets ``Transaction.status = CLOSED`` on
the row and persists it — the WHOLE structure's row, on the strength of ONE
contract settling. The surviving legs (including the protective long of a spread)
then belonged to a CLOSED transaction, and every ledger accessor, the lifecycle
pass and every exit rule reach an option position only through an OPENED one: they
could not be seen, managed or closed again. Silent, unrecoverable, real money.

This is NOT the arm guarded in ``refresh_transactions`` (that is OPT-S8, the
backtest half, pinned by ``tests/test_refresh_transactions_partial_fill.py`` — a
live settlement never reaches it). The two share ONE piece of arithmetic,
``ba2_common.core.utils.option_contract_net``.

WHAT KILLS WHAT — each behaviour is pinned by at least one test no other
behaviour can carry:

* close-only-when-flat: ``test_one_assigned_leg_of_a_strangle_leaves_it_OPENED``
  (drop the predicate -> it closes). The sibling-still-open fixture alone does NOT
  pin the recording, because a structure with an unrecorded settled leg is also
  "not flat" — hence the next bullet.
* the settled leg IS recorded: ``test_the_settled_leg_is_recorded_as_a_closing_row``
  and ``test_the_LAST_leg_settling_DOES_close_the_structure`` (skip the write and
  the settled contract stays short forever, so the structure never closes).
* not stranding a single leg: ``test_a_SINGLE_LEG_option_still_closes_on_its_own_assignment``
  (freeze the transaction open and it fails).
* the closing side: ``..._still_closes_on_its_own_assignment`` again — a settlement
  written on the WRONG side doubles the leg instead of flattening it.
* the over-settlement cap: ``test_a_broker_over_report_is_capped_at_the_open_leg``.
* the synthetic row is not mistaken for the originating order:
  ``test_a_SECOND_partial_assignment_is_still_read_as_a_SHORT_leg``.
"""
from datetime import date, datetime, timezone

from sqlmodel import select

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.db import get_db, add_instance, get_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderOpenType, OrderStatus, OrderType,
    TransactionStatus,
)
from ba2_common.core.utils import option_contract_net


PUT_OCC = "AAPL260116P00150000"
CALL_OCC = "AAPL260116C00160000"
EXPIRY = date(2026, 1, 16)


def _make_alpaca(account_id):
    """An AlpacaAccount that can run DB-backed reconcile with no network."""
    acct = AlpacaAccount.__new__(AlpacaAccount)
    acct.id = account_id
    acct._settings_cache = {"api_key": "k", "api_secret": "s",
                            "paper_account": True, "data_feed": "iex"}
    return acct


def _leg(account_id, txn_id, parent_id, occ, right, strike, side, contracts):
    return add_instance(TradingOrder(
        account_id=account_id, symbol=occ, quantity=contracts, side=side,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=contracts,
        transaction_id=txn_id, asset_class=AssetClass.OPTION, contract_symbol=occ,
        option_type=right, strike=strike, expiry=EXPIRY, underlying_symbol="AAPL",
        parent_order_id=parent_id, multiplier=100, open_price=2.5,
    ))


def _seed_structure(account_id, expert_id, legs, *, contracts=1.0,
                    strategy="short_strangle"):
    """The row shape ``OptionsAccountInterface.submit_option_order`` writes for 2-4
    legs: ONE transaction, a net-only PARENT order (OPTION, no ``contract_symbol``)
    and one contract-carrying CHILD per leg, joined by ``parent_order_id``.

    ``legs`` is ``[(occ, right, strike, side)]``.
    """
    txn_id = add_instance(Transaction(
        symbol="AAPL", quantity=contracts, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, open_price=2.5,
        open_date=datetime.now(timezone.utc), expert_id=expert_id,
        asset_class=AssetClass.OPTION, multiplier=100,
    ))
    parent_id = add_instance(TradingOrder(
        account_id=account_id, symbol="AAPL", quantity=contracts,
        side=OrderDirection.SELL, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, filled_qty=contracts, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, option_strategy=strategy, multiplier=100,
        underlying_symbol="AAPL", expiry=EXPIRY, open_price=2.5,
    ))
    for occ, right, strike, side in legs:
        _leg(account_id, txn_id, parent_id, occ, right, strike, side, contracts)
    return txn_id, parent_id


def _seed_single_leg(account_id, expert_id, occ, right, strike, side, contracts=1.0):
    """A SINGLE-leg option: one contract-carrying order and NO net-only parent."""
    txn_id = add_instance(Transaction(
        symbol=occ, quantity=contracts, side=side, status=TransactionStatus.OPENED,
        open_price=2.5, open_date=datetime.now(timezone.utc), expert_id=expert_id,
        asset_class=AssetClass.OPTION, multiplier=100,
    ))
    add_instance(TradingOrder(
        account_id=account_id, symbol=occ, quantity=contracts, side=side,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=contracts,
        transaction_id=txn_id, asset_class=AssetClass.OPTION, contract_symbol=occ,
        option_type=right, strike=strike, expiry=EXPIRY, underlying_symbol="AAPL",
        multiplier=100, open_price=2.5,
    ))
    return txn_id


def _orders_for(txn_id):
    with get_db() as session:
        return session.exec(
            select(TradingOrder).where(TradingOrder.transaction_id == txn_id)
        ).all()


def _net_for(txn_id):
    return option_contract_net(_orders_for(txn_id))


def _activity(activity_id, atype, occ, qty="1"):
    return {"id": activity_id, "activity_type": atype, "symbol": occ,
            "qty": qty, "price": "0"}


STRANGLE = [(PUT_OCC, OptionRight.PUT, 150.0, OrderDirection.SELL),
            (CALL_OCC, OptionRight.CALL, 160.0, OrderDirection.SELL)]


# ---------------------------------------------------------------------------
# THE HEADLINE: one leg settling leaves the rest manageable
# ---------------------------------------------------------------------------
def test_one_assigned_leg_of_a_strangle_leaves_it_OPENED(
        mock_account_def, mock_expert_instance):
    """The short PUT is assigned. The short CALL is still live at the broker, so the
    transaction that carries it must stay OPENED — closing it here is what made the
    call unmanageable while it kept accruing risk."""
    acct = _make_alpaca(mock_account_def.id)
    txn_id, _ = _seed_structure(mock_account_def.id, mock_expert_instance.id, STRANGLE)

    acct.reconcile_option_assignments([_activity("mls-strangle-put", "OPASN", PUT_OCC)])

    txn = get_instance(Transaction, txn_id)
    assert txn.status == TransactionStatus.OPENED, (
        f"one assigned leg closed the whole strangle (close_reason={txn.close_reason!r}) "
        f"— the short {CALL_OCC} is now orphaned")
    assert txn.close_reason is None


def test_the_surviving_leg_is_still_reachable_through_an_OPENED_transaction(
        mock_account_def, mock_expert_instance):
    """WHY IT MATTERS, as the property every option accessor depends on: a leg is
    findable only through an OPENED transaction."""
    acct = _make_alpaca(mock_account_def.id)
    txn_id, _ = _seed_structure(mock_account_def.id, mock_expert_instance.id, STRANGLE)

    acct.reconcile_option_assignments([_activity("mls-reachable", "OPASN", PUT_OCC)])

    survivors = [o for o in _orders_for(txn_id) if o.contract_symbol == CALL_OCC]
    assert len(survivors) == 1
    assert get_instance(
        Transaction, survivors[0].transaction_id).status == TransactionStatus.OPENED
    assert _net_for(txn_id)[CALL_OCC] == -1.0, (
        "the surviving short call must still read as one open short contract")


def test_the_settled_leg_is_recorded_as_a_closing_row(
        mock_account_def, mock_expert_instance):
    """LEAVING IT OPEN IS ONLY HALF THE FIX. The assigned put is gone at the broker;
    if nothing records that, the ledger keeps a short put that no longer exists —
    the structure can never balance, and the cover arithmetic keeps charging for an
    obligation the OCC has already extinguished."""
    acct = _make_alpaca(mock_account_def.id)
    txn_id, _ = _seed_structure(mock_account_def.id, mock_expert_instance.id, STRANGLE)

    acct.reconcile_option_assignments([_activity("mls-recorded", "OPASN", PUT_OCC)])

    closes = [o for o in _orders_for(txn_id)
              if o.contract_symbol == PUT_OCC and o.side == OrderDirection.BUY]
    assert len(closes) == 1, "the settled put leg was not recorded"
    row = closes[0]
    assert row.status == OrderStatus.FILLED
    assert row.filled_qty == 1.0 and row.quantity == 1.0
    assert row.asset_class == AssetClass.OPTION
    assert row.open_type == OrderOpenType.EXTERNAL   # book-keeping, not a trade we placed
    assert row.broker_order_id is None
    assert row.open_price == 0.0                     # a MEASURED zero premium
    assert row.depends_on_order is not None, (
        "a settlement is a CLOSING row; without depends_on_order it can be resolved "
        "as the transaction's entry order")
    assert _net_for(txn_id)[PUT_OCC] == 0.0, "the settled contract must net flat"


def test_the_LAST_leg_settling_DOES_close_the_structure(
        mock_account_def, mock_expert_instance):
    """THE INVERSE, and the proof this is not a freeze. Both contracts accounted for
    means the structure is flat, and it closes with the LAST settlement's reason."""
    acct = _make_alpaca(mock_account_def.id)
    txn_id, _ = _seed_structure(mock_account_def.id, mock_expert_instance.id, STRANGLE)

    acct.reconcile_option_assignments([_activity("mls-last-put", "OPASN", PUT_OCC)])
    assert get_instance(Transaction, txn_id).status == TransactionStatus.OPENED

    acct.reconcile_option_assignments([_activity("mls-last-call", "OPEXP", CALL_OCC)])

    txn = get_instance(Transaction, txn_id)
    assert txn.status == TransactionStatus.CLOSED
    assert txn.close_reason == "expired"
    assert txn.close_price == 0.0
    assert _net_for(txn_id) == {PUT_OCC: 0.0, CALL_OCC: 0.0}


def test_the_equity_side_of_the_assignment_still_happens(
        mock_account_def, mock_expert_instance):
    """Holding the STRUCTURE open changes nothing about the shares the assignment put
    to us: the equity long and its entry order are written exactly as before."""
    acct = _make_alpaca(mock_account_def.id)
    _seed_structure(mock_account_def.id, mock_expert_instance.id, STRANGLE)

    acct.reconcile_option_assignments([_activity("mls-equity", "OPASN", PUT_OCC)])

    with get_db() as session:
        longs = session.exec(
            select(Transaction)
            .where(Transaction.symbol == "AAPL")
            .where(Transaction.side == OrderDirection.BUY)
            .where(Transaction.expert_id == mock_expert_instance.id)
        ).all()
    assert len(longs) == 1
    assert longs[0].quantity == 100.0 and longs[0].open_price == 150.0
    assert longs[0].status == TransactionStatus.OPENED


# ---------------------------------------------------------------------------
# NOT HELD BACK: a single leg has no sibling to wait on
# ---------------------------------------------------------------------------
def test_a_SINGLE_LEG_option_still_closes_on_its_own_assignment(
        mock_account_def, mock_expert_instance):
    """THE MIRROR BUG. A lone short put has nothing left once it is assigned, and
    stranding it OPENED would be exactly as bad as closing a structure early. It also
    pins the SIDE of the settlement row: written as a SELL it would double the leg to
    -2 instead of flattening it, and this transaction would never close."""
    acct = _make_alpaca(mock_account_def.id)
    txn_id = _seed_single_leg(mock_account_def.id, mock_expert_instance.id, PUT_OCC,
                              OptionRight.PUT, 150.0, OrderDirection.SELL)

    acct.reconcile_option_assignments([_activity("mls-single", "OPASN", PUT_OCC)])

    txn = get_instance(Transaction, txn_id)
    assert txn.status == TransactionStatus.CLOSED
    assert txn.close_reason == "assigned"
    assert _net_for(txn_id) == {PUT_OCC: 0.0}


def test_a_PARTIALLY_assigned_single_leg_stays_OPENED_until_the_rest_settles(
        mock_account_def, mock_expert_instance):
    """"Every contract accounted for" is per CONTRACT COUNT, not per contract symbol.
    Two short puts, one assigned: one is still short and the transaction must stay
    manageable. A short contract on a CLOSED transaction is the same orphan as a
    stranded spread leg, only smaller.

    THE REFRESH PASS IS PART OF THE CLAIM. This is the case the OPT-S8 guard used to
    exclude: it keyed on the multi-leg parent, which a lone short put does not have,
    so one pass later the closing arm shut the transaction as ``tp_sl_filled`` with a
    contract still short. Both doors now ask the same question of the same rows."""
    acct = _make_alpaca(mock_account_def.id)
    txn_id = _seed_single_leg(mock_account_def.id, mock_expert_instance.id, PUT_OCC,
                              OptionRight.PUT, 150.0, OrderDirection.SELL, contracts=2.0)

    acct.reconcile_option_assignments([_activity("mls-partial-1", "OPASN", PUT_OCC)])

    assert get_instance(Transaction, txn_id).status == TransactionStatus.OPENED
    assert _net_for(txn_id) == {PUT_OCC: -1.0}

    acct.refresh_transactions()

    txn = get_instance(Transaction, txn_id)
    assert txn.status == TransactionStatus.OPENED, (
        f"the refresh pass closed a transaction with one contract still short "
        f"({txn.close_reason!r})")
    assert _net_for(txn_id) == {PUT_OCC: -1.0}


def test_a_SECOND_partial_assignment_is_still_read_as_a_SHORT_leg(
        mock_account_def, mock_expert_instance):
    """The synthetic settlement row must not be mistaken for the ORIGINATING order.
    ``_find_option_order_for_contract`` takes the most recent row for the contract; if
    it picked the buy-to-close this fix writes, the second OPASN would read the leg as
    LONG and refuse itself with "OPASN on non-short option", leaving the remaining
    contract short forever."""
    acct = _make_alpaca(mock_account_def.id)
    txn_id = _seed_single_leg(mock_account_def.id, mock_expert_instance.id, PUT_OCC,
                              OptionRight.PUT, 150.0, OrderDirection.SELL, contracts=2.0)

    acct.reconcile_option_assignments([_activity("mls-second-1", "OPASN", PUT_OCC)])
    results = acct.reconcile_option_assignments(
        [_activity("mls-second-2", "OPASN", PUT_OCC)])

    assert "non-short" not in results[0]["result"], results[0]["result"]
    txn = get_instance(Transaction, txn_id)
    assert txn.status == TransactionStatus.CLOSED
    assert txn.close_reason == "assigned"
    assert _net_for(txn_id) == {PUT_OCC: 0.0}


def test_a_broker_over_report_is_capped_at_the_open_leg(
        mock_account_def, mock_expert_instance):
    """An uncapped settlement would drive the contract net THROUGH zero into a phantom
    long — the option-side twin of the over-assignment ``_settle_called_away``
    refuses. One contract is open, so one contract settles."""
    acct = _make_alpaca(mock_account_def.id)
    txn_id = _seed_single_leg(mock_account_def.id, mock_expert_instance.id, PUT_OCC,
                              OptionRight.PUT, 150.0, OrderDirection.SELL)

    acct.reconcile_option_assignments(
        [_activity("mls-over", "OPASN", PUT_OCC, qty="5")])

    assert _net_for(txn_id) == {PUT_OCC: 0.0}, "the leg was settled past flat"
    assert get_instance(Transaction, txn_id).status == TransactionStatus.CLOSED


# ---------------------------------------------------------------------------
# EXPIRY and EXERCISE behave exactly like an assignment
# ---------------------------------------------------------------------------
def test_one_EXPIRING_leg_of_a_strangle_leaves_it_OPENED(
        mock_account_def, mock_expert_instance):
    """An expiry publishes no quantity at all, so the whole open leg settles — and it
    is still only ONE leg. The unexpired sibling keeps the structure open."""
    acct = _make_alpaca(mock_account_def.id)
    txn_id, _ = _seed_structure(mock_account_def.id, mock_expert_instance.id, STRANGLE)

    acct.reconcile_option_assignments([
        {"id": "mls-exp-leg", "activity_type": "OPEXP", "symbol": PUT_OCC,
         "qty": None, "price": "0"}])

    txn = get_instance(Transaction, txn_id)
    assert txn.status == TransactionStatus.OPENED
    assert txn.close_reason is None
    assert _net_for(txn_id) == {PUT_OCC: 0.0, CALL_OCC: -1.0}


def test_one_EXERCISED_leg_of_a_long_spread_leaves_it_OPENED(
        mock_account_def, mock_expert_instance):
    """Exercise settles a LONG leg, so the closing row is a SELL. The short leg of the
    spread survives and must stay reachable."""
    acct = _make_alpaca(mock_account_def.id)
    txn_id, _ = _seed_structure(
        mock_account_def.id, mock_expert_instance.id,
        [(CALL_OCC, OptionRight.CALL, 160.0, OrderDirection.BUY),
         (PUT_OCC, OptionRight.PUT, 150.0, OrderDirection.SELL)],
        strategy="jade_lizard")

    acct.reconcile_option_assignments([
        {"id": "mls-exc-leg", "activity_type": "OPEXC", "symbol": CALL_OCC,
         "qty": None, "price": "0"}])

    txn = get_instance(Transaction, txn_id)
    assert txn.status == TransactionStatus.OPENED
    assert txn.close_reason is None
    assert _net_for(txn_id) == {CALL_OCC: 0.0, PUT_OCC: -1.0}


def test_a_SINGLE_LEG_expiry_and_exercise_still_close_their_transaction(
        mock_account_def, mock_expert_instance):
    """The single-leg inverse for the other two activity types, so no one of the three
    branches can be frozen open without a failure."""
    acct = _make_alpaca(mock_account_def.id)

    expired = _seed_single_leg(mock_account_def.id, mock_expert_instance.id, PUT_OCC,
                               OptionRight.PUT, 150.0, OrderDirection.SELL)
    acct.reconcile_option_assignments([
        {"id": "mls-single-exp", "activity_type": "OPEXP", "symbol": PUT_OCC,
         "qty": None, "price": "0"}])
    txn = get_instance(Transaction, expired)
    assert txn.status == TransactionStatus.CLOSED
    assert txn.close_reason == "expired" and txn.close_price == 0.0

    exercised = _seed_single_leg(mock_account_def.id, mock_expert_instance.id, CALL_OCC,
                                 OptionRight.CALL, 160.0, OrderDirection.BUY)
    acct.reconcile_option_assignments([
        {"id": "mls-single-exc", "activity_type": "OPEXC", "symbol": CALL_OCC,
         "qty": None, "price": "0"}])
    txn = get_instance(Transaction, exercised)
    assert txn.status == TransactionStatus.CLOSED
    assert txn.close_reason == "exercised"


def test_the_REFRESH_pass_does_not_undo_it(mock_account_def, mock_expert_instance):
    """THE TWO DOORS MUST AGREE, and after this fix they finally meet.

    Recording the settled leg gives the structure a FILLED *dependent* OPTION order —
    exactly the row shape the OPT-S8 guard in ``refresh_transactions`` was written
    for, and a shape no live path produced before. That guard is therefore no longer
    theoretical for a live account: without it, the very next refresh pass would close
    the structure as ``tp_sl_filled`` and re-orphan the surviving leg through the other
    door. Both readings come from one ``option_contract_net``, which is why they
    cannot drift apart."""
    acct = _make_alpaca(mock_account_def.id)
    txn_id, _ = _seed_structure(mock_account_def.id, mock_expert_instance.id, STRANGLE)

    acct.reconcile_option_assignments([_activity("mls-refresh", "OPASN", PUT_OCC)])
    assert get_instance(Transaction, txn_id).status == TransactionStatus.OPENED

    settled = [o for o in _orders_for(txn_id)
               if o.contract_symbol == PUT_OCC and o.side == OrderDirection.BUY]
    assert settled and settled[0].depends_on_order is not None, (
        "the fixture is only interesting while the settlement row is a DEPENDENT "
        "filled order — that is what reaches the refresh arm")

    acct.refresh_transactions()

    txn = get_instance(Transaction, txn_id)
    assert txn.status == TransactionStatus.OPENED, (
        f"refresh_transactions re-closed the structure ({txn.close_reason!r}) — the "
        f"live fix and the shared guard disagree")


def test_a_CALLED_AWAY_leg_of_a_structure_leaves_it_OPENED(
        mock_account_def, mock_expert_instance):
    """The short-CALL branch has its own path (it splits the equity lot on the way),
    and it must hold the structure open on the same terms. The equity long is still
    settled at the strike."""
    acct = _make_alpaca(mock_account_def.id)
    expert_id = mock_expert_instance.id
    held_id = add_instance(Transaction(
        symbol="AAPL", quantity=100.0, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=140.0,
        open_date=datetime.now(timezone.utc), expert_id=expert_id,
    ))
    txn_id, _ = _seed_structure(mock_account_def.id, expert_id, STRANGLE)

    acct.reconcile_option_assignments([_activity("mls-called", "OPASN", CALL_OCC)])

    txn = get_instance(Transaction, txn_id)
    assert txn.status == TransactionStatus.OPENED, (
        "the short put leg of the strangle is now orphaned")
    assert _net_for(txn_id) == {CALL_OCC: 0.0, PUT_OCC: -1.0}

    held = get_instance(Transaction, held_id)
    assert held.status == TransactionStatus.CLOSED
    assert held.close_reason == "called_away" and held.close_price == 160.0
