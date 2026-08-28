"""OPT-B2 / F7 — a short-option assignment must be BOOKED, not just netted in the ledger.

What the backtest did
---------------------
``BacktestAccount.settle_option_expiry``'s share-leg conversion moved cash and called
``_update_position``, which mutates the in-memory ``self._positions`` dict and NOTHING
else. No ``TradingOrder``, no ``Transaction`` write.

For a COVERED call that is called away, the 100 long shares net to zero in the ledger while
the equity ``Transaction`` stays OPENED carrying a FILLED BUY and no SELL. Everything that
reads transactions therefore still sees 100 shares that no longer exist:

  * ``_OptionEntryAction._held_equity_shares`` (the overlay's sizer) reports 100 forever, so
    the covered-call overlay writes another — NAKED — call every cycle;
  * a later equity exit sells 100 shares the account does not hold, opening a real short
    from ``qty=0``;
  * ``get_round_trip_trades`` has only the BUY, so the lot is reported ``open_at_end`` and
    marked at the last price instead of realised at the strike.

``process_pending_assignment_liquidations`` cannot rescue it: it short-circuits on
``held <= 0``, and the netted position IS zero.

For a NAKED assignment the mirror hole is F7: the assigned short lot is created with no
order and no transaction, and the next-bar liquidation order gets ``transaction_id=None``
(nothing resolves for it), so ``get_round_trip_trades`` drops it — the stock P&L lands in
the equity CURVE and is absent from the trade ROWS that every fitness metric is built from.

Live gets both right (``AlpacaAccount._settle_called_away`` /
``_apply_option_activity``'s ``csp_assignment`` branch).
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import (
    AssetClass,
    OptionRight,
    OrderDirection,
    OrderStatus,
    OrderType,
    TransactionStatus,
    TXN_ORIGIN_CSP_ASSIGNMENT,
)


CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,   # zero so P&L math is exact
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

_CALL_OCC = "AAPL240315C00180000"   # 180 call
_STRIKE = 180.0

# Expiry bar 2024-03-15 closes at 200 -> the 180 call is ITM by 20/share.
# 2024-03-18 is the next bar (assignment-liquidation bar), opening at 205.
_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 150, "High": 152, "Low": 148, "Close": 151, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 151, "High": 154, "Low": 150, "Close": 153, "Volume": 1100},
    {"Date": datetime(2024, 3, 15), "Open": 199, "High": 201, "Low": 198, "Close": 200, "Volume": 1200},
    {"Date": datetime(2024, 3, 18), "Open": 205, "High": 207, "Low": 204, "Close": 206, "Volume": 1300},
]


def _seed_cache(db_path: str) -> None:
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows(
        "AAPL",
        "2024-03-01",
        [{"occ_symbol": _CALL_OCC, "option_type": "call", "strike": _STRIKE,
          "expiry": "2024-03-15", "bid": 3.0, "ask": 3.2, "last": 3.1, "iv": 0.25}],
    )
    cache.write_bar_rows(
        [{"occ_symbol": _CALL_OCC, "date": "2024-03-06", "open": 4.0, "high": 4.8,
          "low": 3.9, "close": 4.5, "volume": 500, "underlying": "AAPL",
          "option_type": "call", "strike": _STRIKE, "expiry": "2024-03-15"}]
    )


def _build(tmp_path, account_id, *, with_equity_lot: bool, trim_shares: int = 0):
    """An account short one 180 call, optionally over a FILLED 100-share equity long.

    ``trim_shares`` sells that many of the 100 shares back AFTER the call is written (a
    plain market sell, no ``depends_on_order``, so the lot stays OPENED at the remainder) —
    the coverage-drift state OPT-L1 polices. The call itself must be written while the full
    round lot is held; the cover guard refuses otherwise.

    THE TRIM IS BOOKED DIRECTLY, NOT THROUGH ``_apply_fill``, and that is the point rather
    than a shortcut. Since the PLEDGED-COVER lock landed (OPT-L1's exit half —
    ``test_pledged_share_lock.py``) ``_apply_fill`` REFUSES exactly this sale: 100 shares
    held with 100 pledged leaves zero unpledged excess, so the trim order is CANCELED and
    the drift never happens. That is the correct behaviour of the fill path and the reason
    the O_CC arm no longer carries naked calls.

    The drift state itself is still reachable at runtime by routes the fill path does not
    own — a PARTIAL assignment on another contract eating the cover, a corporate action, or
    simply state persisted before the lock existed — and the assignment LEDGER (this file's
    actual subject) must still split the delivery correctly when it arrives. So the fixture
    reproduces the END STATE by hand: the same FILLED 60-share SELL row on the same equity
    transaction and the same ledger move, minus the guarded fill path. This mirrors the
    idiom ``test_options_review_fixes.py`` already uses to build partially-covered
    positions (``acct._update_position(...)`` plus a hand-written order row).
    """
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.price_source import AsOfPriceSource
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.option_types import OptionLeg

    cache_db = str(tmp_path / "options_cache.sqlite")
    _seed_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db(f"assign-ledger-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(datetime(2024, 3, 5))
    acct = BacktestAccount(account_id, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(account_id, acct)

    equity_txn_id = None
    if with_equity_lot:
        o = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=100,
                         side=OrderDirection.BUY, order_type=OrderType.MARKET,
                         status=OrderStatus.NEW, comment="equity-lot")
        acct.submit_order(o)
        persisted = acct.get_order(o.broker_order_id)
        equity_txn_id = persisted.transaction_id
        acct._apply_fill(persisted, 150.0, datetime(2024, 3, 5))   # 100 sh @ 150

    leg = OptionLeg(contract_symbol=_CALL_OCC, side=OrderDirection.SELL,
                    position_intent="sell_to_open", option_type=OptionRight.CALL,
                    strike=_STRIKE, expiry=date(2024, 3, 15), underlying="AAPL")
    acct.submit_option_order(legs=[leg], quantity=1, order_type="market",
                             option_strategy="covered_call" if with_equity_lot else "naked_call")
    acct.refresh_orders()          # next_bar_open -> the call fills off the 2024-03-06 bar
    acct.refresh_transactions()
    assert len(acct.get_option_positions()) == 1
    if with_equity_lot:
        assert [t.id for t in _open_equity_txns(acct)] == [equity_txn_id]

    if trim_shares:
        from ba2_common.core.db import add_instance, update_instance

        trim = TradingOrder(
            account_id=acct.id, symbol="AAPL", quantity=trim_shares,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            transaction_id=equity_txn_id, status=OrderStatus.NEW,
            broker_order_id=acct._next_broker_id(), comment="trim")
        add_instance(trim)
        acct.invalidate_order_cache()
        # The bookkeeping ``_apply_fill`` would do, minus the PLEDGED-COVER lock that now
        # (correctly) refuses this sale — see the docstring. Cash, ledger and the FILLED
        # order row are identical to what the fill path produced before the lock existed.
        booked = acct.get_order(trim.broker_order_id)
        acct._cash += float(trim_shares) * 160.0
        acct._update_position("AAPL", -float(trim_shares), 160.0)
        booked.filled_qty = float(trim_shares)
        booked.open_price = 160.0
        booked.status = OrderStatus.FILLED
        update_instance(booked)
        acct._fill_dates[booked.id] = datetime(2024, 3, 6)
        acct.invalidate_order_cache()
        acct.refresh_transactions()
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"][0]["qty"] == \
            pytest.approx(100.0 - trim_shares)
        assert [t.id for t in _open_equity_txns(acct)] == [equity_txn_id]

    engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
    engine.account = acct
    engine.price = ps
    engine.config = CFG
    return engine, acct, ps, ctx, equity_txn_id


@pytest.fixture
def called_away(tmp_path):
    """A COVERED call: 100 long shares @150 plus a short 180 call, assigned at expiry."""
    engine, acct, ps, ctx, eq_txn = _build(tmp_path, 41, with_equity_lot=True)
    try:
        yield engine, acct, ps, eq_txn
    finally:
        ctx.__exit__(None, None, None)


@pytest.fixture
def partly_covered(tmp_path):
    """The coverage the call was written against was TRIMMED to 40 shares before expiry.

    The 100-share delivery then splits: 40 come out of the held lot, 60 are conjured short.
    This is exactly the state OPT-L1 exists to police, and it is the only shape that can
    tell "book the offset" apart from "book the whole delivery".
    """
    engine, acct, ps, ctx, eq_txn = _build(tmp_path, 43, with_equity_lot=True,
                                           trim_shares=60)
    try:
        yield engine, acct, ps, eq_txn
    finally:
        ctx.__exit__(None, None, None)


@pytest.fixture
def naked_assignment(tmp_path):
    """A NAKED short 180 call assigned at expiry -> a short stock lot out of nothing."""
    engine, acct, ps, ctx, _ = _build(tmp_path, 42, with_equity_lot=False)
    try:
        yield engine, acct, ps
    finally:
        ctx.__exit__(None, None, None)


def _equity_orders(acct, txn_id):
    return [o for o in acct.get_orders()
            if o.transaction_id == txn_id
            and getattr(o, "asset_class", None) != AssetClass.OPTION]


def _equity_rows(acct, symbol="AAPL"):
    """Round-trip rows for the SHARE lots only (an option leg's row carries its OCC)."""
    return [t for t in acct.get_round_trip_trades()
            if t["symbol"] == symbol and t["contract_symbol"] is None]


def _open_equity_txns(acct, symbol="AAPL"):
    from ba2_common.core.trade_store import transactions_where

    return [t for t in transactions_where(status=TransactionStatus.OPENED, symbol=symbol)
            if t.asset_class != AssetClass.OPTION]


# ---------------------------------------------------------------------------
# OPT-B2 — called away
# ---------------------------------------------------------------------------
def test_called_away_shares_leave_no_open_equity_transaction(called_away):
    """The delivered lot must stop counting as held. It is the phantom-share source."""
    engine, acct, ps, eq_txn = called_away
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))
    acct.refresh_transactions()

    assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == [], \
        "the shares were delivered; the ledger must be flat"
    still_open = _open_equity_txns(acct)
    assert still_open == [], (
        f"equity transaction(s) {[t.id for t in still_open]} are still OPENED after the "
        f"shares were called away — every reader of the transaction table (the overlay's "
        f"share sizer, has_position, the exit sizer) now sees 100 shares that do not exist "
        f"(OPT-B2)."
    )


def test_called_away_writes_the_delivering_sell_at_the_strike(called_away):
    """The delivery is a real fill: a SELL of 100 @ strike on the equity transaction."""
    engine, acct, ps, eq_txn = called_away
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))

    sells = [o for o in _equity_orders(acct, eq_txn) if o.side == OrderDirection.SELL]
    assert len(sells) == 1, (
        f"expected exactly one delivering SELL on the equity transaction, got "
        f"{[(o.side, o.quantity, o.status) for o in _equity_orders(acct, eq_txn)]}"
    )
    sell = sells[0]
    assert sell.status == OrderStatus.FILLED
    assert float(sell.filled_qty) == pytest.approx(100.0)
    assert float(sell.open_price) == pytest.approx(_STRIKE), \
        "an assignment transacts stock at the STRIKE, not the market"


def test_called_away_lot_is_a_realised_round_trip_not_open_at_end(called_away):
    """F7: the assignment's stock P&L must be in the trade ROWS, not only the curve."""
    engine, acct, ps, eq_txn = called_away
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))
    acct.refresh_transactions()

    rows = _equity_rows(acct)
    assert len(rows) == 1, f"expected one equity round-trip for the called-away lot, got {rows}"
    row = rows[0]
    assert row["exit_reason"] != "open_at_end", \
        "the called-away lot is realised, not still open at run end"
    assert row["entry_price"] == pytest.approx(150.0)
    assert row["exit_price"] == pytest.approx(_STRIKE)
    assert row["pnl"] == pytest.approx((180.0 - 150.0) * 100.0)


def test_called_away_does_not_leave_a_pending_liquidation(called_away):
    """Nothing is left to clean up: the shares were delivered, not orphaned."""
    engine, acct, ps, eq_txn = called_away
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))

    ps.set_clock(datetime(2024, 3, 18))
    acct.process_pending_assignment_liquidations()

    assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == [], \
        "the liquidation pass must not conjure a position out of a fully delivered lot"


# ---------------------------------------------------------------------------
# F7 — naked assignment: the orphaned short lot
# ---------------------------------------------------------------------------
def test_naked_assignment_books_the_short_lot_as_a_transaction(naked_assignment):
    """The short stock created by the assignment must exist in the transaction table."""
    engine, acct, ps = naked_assignment
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))
    acct.refresh_transactions()

    aapl = [p for p in acct.get_positions() if p["symbol"] == "AAPL"]
    assert len(aapl) == 1 and aapl[0]["qty"] == -100          # short 100, as before

    opened = _open_equity_txns(acct)
    assert len(opened) == 1, (
        f"the assigned SHORT lot is not in the transaction table (got {opened}) — its P&L "
        f"can only reach the equity curve, never the trade rows (F7)."
    )
    txn = opened[0]
    assert txn.side == OrderDirection.SELL
    assert float(txn.quantity) == pytest.approx(100.0)
    assert float(txn.open_price) == pytest.approx(_STRIKE)
    assert (txn.meta_data or {}).get("origin") == TXN_ORIGIN_CSP_ASSIGNMENT


def test_naked_assignment_liquidation_produces_a_round_trip_row(naked_assignment):
    """F7's headline: the buy-back must pair with the assignment into one realised row."""
    engine, acct, ps = naked_assignment
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))
    acct.refresh_transactions()

    ps.set_clock(datetime(2024, 3, 18))
    assert acct.process_pending_assignment_liquidations() is True
    acct.refresh_transactions()

    assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
    rows = _equity_rows(acct)
    assert len(rows) == 1, (
        f"the assigned short lot and its next-bar buy-back produced no trade row: {rows}. "
        f"adj_final = final - excess (results.py) subtracts a ROW-derived quantity from a "
        f"CURVE-derived one, so a curve-only move corrupts every row metric (F7)."
    )
    row = rows[0]
    assert row["direction"] == "sell"
    assert row["entry_price"] == pytest.approx(_STRIKE)
    assert row["exit_price"] == pytest.approx(205.0)          # next bar's OPEN
    assert row["pnl"] == pytest.approx((180.0 - 205.0) * 100.0)


# ---------------------------------------------------------------------------
# The split case: part of the delivery closes a held lot, part opens a new one
# ---------------------------------------------------------------------------
def test_a_partly_covered_assignment_splits_the_delivery(partly_covered):
    """40 held shares are delivered; the other 60 are a NEW short lot, and only those 60
    are the orphan the next-bar liquidation is allowed to touch.

    Scheduling the whole 100 (what the code did before the split existed) makes the
    liquidation pass sell down whatever else the account happens to hold in that name.
    """
    engine, acct, ps, eq_txn = partly_covered
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))
    acct.refresh_transactions()

    aapl = [p for p in acct.get_positions() if p["symbol"] == "AAPL"]
    assert len(aapl) == 1 and aapl[0]["qty"] == pytest.approx(-60.0)

    # The remaining 40 shares were delivered at the STRIKE and the lot's transaction closed.
    delivered = [o for o in _equity_orders(acct, eq_txn)
                 if o.side == OrderDirection.SELL and o.comment == "option_assignment"]
    assert len(delivered) == 1
    assert float(delivered[0].filled_qty) == pytest.approx(40.0)
    assert float(delivered[0].open_price) == pytest.approx(_STRIKE)
    assert eq_txn not in [t.id for t in _open_equity_txns(acct)]

    # The conjured short is its own booked lot...
    opened = _open_equity_txns(acct)
    assert len(opened) == 1
    assert opened[0].side == OrderDirection.SELL
    assert float(opened[0].quantity) == pytest.approx(60.0)

    # ...and it is the ONLY thing scheduled for the orphan liquidation.
    assert acct._pending_assignment_sells == {"AAPL": pytest.approx(-60.0)}, (
        "the delivered 40 shares are not orphaned stock; scheduling them makes the "
        "next-bar pass liquidate an unrelated holding"
    )


def test_a_partly_covered_assignment_reports_both_lots_as_rows(partly_covered):
    """F7 again, on the split: both halves must be realised trade ROWS."""
    engine, acct, ps, eq_txn = partly_covered
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))
    acct.refresh_transactions()

    ps.set_clock(datetime(2024, 3, 18))
    assert acct.process_pending_assignment_liquidations() is True
    acct.refresh_transactions()

    assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
    rows = {t["size"]: t for t in _equity_rows(acct)}
    assert set(rows) == {100.0, 60.0}, f"expected one row per lot, got {rows}"
    # The original lot: bought 100 @150, exited 60 @160 (the trim) + 40 @180 (delivery).
    assert rows[100.0]["exit_price"] == pytest.approx((60 * 160.0 + 40 * 180.0) / 100.0)
    assert rows[100.0]["pnl"] == pytest.approx(60 * (160.0 - 150.0) + 40 * (180.0 - 150.0))
    # The conjured short: sold 60 @180 (strike), bought back @205 (next bar's open).
    assert rows[60.0]["pnl"] == pytest.approx((180.0 - 205.0) * 60.0)
