"""B10 regression: an individually-closed leg must NOT orphan the rest of the structure.

Run 760 (OS2-v7 re-run) showed 3 short strangles (BABA/C/MRVL 240503) whose CALL leg was
bought back mid-life by the per-leg margin-call liquidation (``_liquidate_option_lot`` —
the only engine path that closes ONE leg of a structure independently) while the PUT leg
was then held 551-560 bars with NO management: never TP/time-managed, never expired
(recorded ``open_at_end`` at the entry premium ≈ 0% instead of expiring worthless at the
full credit — materially understating credit-strategy P&L).

Root cause chain (code-proven):

  1. The individual leg close rides the shared transaction as a STANDALONE option order
     (no ``parent_order_id``) — ``close_option_position`` and the margin-liquidation
     synthetic close both take this shape.
  2. ``ReadOnlyAccountInterface.refresh_transactions`` computes ``position_balanced`` from
     parent-level buy/sell sums that EXCLUDE multi-leg child legs but COUNT standalone
     option closes: the one-leg close (BUY 1) offsets the entry parent (SELL 1 structure)
     and the whole strangle transaction is marked CLOSED.
  3. A CLOSED transaction is invisible to ``get_option_positions`` /
     ``_apply_option_expiry`` / ``_option_transaction_for_contract`` (all filter
     ``TransactionStatus.OPENED``) — the surviving leg can never settle or be managed.

These tests drive the REAL engine path: short strangle filled -> one leg closed
individually -> refresh -> the transaction must stay OPENED with the surviving leg still
held -> the engine's per-bar expiry must then settle the survivor at expiry.

Run from the backend dir:
    ../../.venv/Scripts/python.exe -m pytest tests/backtest/test_spread_orphan_leg.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection, TransactionStatus

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 1.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

_CALL = "AAPL240315C00210000"
_PUT = "AAPL240315P00180000"

# Spot ~195 during the run; 200 on the 2024-03-15 expiry bar -> the surviving short 180
# put expires OTM (worthless), exactly the BABA/C pattern from run 760.
_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 195, "High": 196, "Low": 194, "Close": 195, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 195, "High": 196, "Low": 194, "Close": 195, "Volume": 1000},
    {"Date": datetime(2024, 3, 7), "Open": 195, "High": 196, "Low": 194, "Close": 195, "Volume": 1000},
    {"Date": datetime(2024, 3, 15), "Open": 200, "High": 201, "Low": 199, "Close": 200, "Volume": 1000},
]


def _seed_cache(db_path: str) -> None:
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    rows = []
    for occ, right, strike, prem in (
        (_CALL, "call", 210.0, 1.0),
        (_PUT, "put", 180.0, 1.0),
    ):
        # Entry fill bar (2024-03-06) for BOTH legs (all-or-none parent fill).
        rows.append({
            "occ_symbol": occ, "date": "2024-03-06",
            "open": prem, "high": prem, "low": prem, "close": prem, "volume": 500,
            "underlying": "AAPL", "option_type": right, "strike": strike,
            "expiry": "2024-03-15",
        })
    # Call buy-back fill bar (2024-03-07): the individually-closed leg.
    rows.append({
        "occ_symbol": _CALL, "date": "2024-03-07",
        "open": 0.5, "high": 0.5, "low": 0.5, "close": 0.5, "volume": 500,
        "underlying": "AAPL", "option_type": "call", "strike": 210.0,
        "expiry": "2024-03-15",
    })
    cache.write_bar_rows(rows)


def _make_price_source(clock: datetime):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(clock)
    return ps


@pytest.fixture
def engine_with_strangle(tmp_path):
    """Account holding a FILLED short strangle (sell 210C + sell 180P, exp 2024-03-15)."""
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.daily_engine import DailyBacktestEngine

    cache_db = str(tmp_path / "options_cache.sqlite")
    _seed_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("orphanleg")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_price_source(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)

    legs = [
        OptionLeg(contract_symbol=_CALL, side=OrderDirection.SELL,
                  position_intent="sell_to_open", option_type=OptionRight.CALL,
                  strike=210.0, expiry=date(2024, 3, 15), underlying="AAPL"),
        OptionLeg(contract_symbol=_PUT, side=OrderDirection.SELL,
                  position_intent="sell_to_open", option_type=OptionRight.PUT,
                  strike=180.0, expiry=date(2024, 3, 15), underlying="AAPL"),
    ]
    acct.submit_option_order(legs=legs, quantity=1, order_type="market",
                             option_strategy="short_strangle")
    acct.refresh_orders()      # fills on 2024-03-06 (next bar open)
    acct.refresh_transactions()
    assert len(acct.get_option_positions()) == 2

    engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
    engine.account = acct
    engine.price = ps
    engine.config = CFG

    try:
        yield engine, acct, ps
    finally:
        ctx.__exit__(None, None, None)


def test_individual_leg_close_does_not_orphan_sibling(engine_with_strangle):
    """Close ONLY the call leg -> the transaction must stay OPENED, the put must remain a
    held position, and the engine must settle the put at expiry (worthless here)."""
    engine, acct, ps = engine_with_strangle

    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.trade_store import transactions_where
    from ba2_common.core.types import OrderStatus

    txn = transactions_where(status=TransactionStatus.OPENED)[0]

    # Close the CALL leg individually (the margin-liquidation / close_option_position
    # shape: a standalone single-leg close riding the same transaction).
    ps.set_clock(datetime(2024, 3, 6))
    call_pos = next(p for p in acct.get_option_positions() if p.contract_symbol == _CALL)
    acct.close_option_position(call_pos, order_type="market")
    acct.refresh_orders()      # the buy-back fills on 2024-03-07
    acct.refresh_transactions()

    # THE BUG: pre-fix the one-leg close offsets the parent's structure count in
    # refresh_transactions' balance sums and the whole transaction is marked CLOSED.
    txn = get_instance(Transaction, txn.id)
    assert txn.status == TransactionStatus.OPENED, (
        f"strangle txn was closed by an individual leg close (status={txn.status}) — "
        f"the surviving put leg is now orphaned"
    )

    # The surviving put must still be a held position (visible to management/expiry).
    held = [p.contract_symbol for p in acct.get_option_positions()]
    assert held == [_PUT]

    # At expiry the engine must settle the survivor: spot 200 -> 180 put OTM -> worthless.
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))

    assert acct.get_option_positions() == []
    put_closes = [
        o for o in acct.get_orders()
        if o.comment == "option_expiry_close" and o.contract_symbol == _PUT
    ]
    assert len(put_closes) == 1
    assert put_closes[0].status == OrderStatus.FILLED
    assert put_closes[0].open_price == pytest.approx(0.0)  # worthless

    # Both legs resolved -> the shared transaction is now closed exactly once.
    txn = get_instance(Transaction, txn.id)
    assert txn.status == TransactionStatus.CLOSED


def test_a_leg_settled_as_a_DEPENDENT_close_does_not_orphan_its_sibling(
        engine_with_strangle):
    """OPT-S8: the SAME orphaning, through the other door in ``refresh_transactions``.

    B10 (above) came in via the mixed-unit balance sums, and the per-contract
    ``contract_net`` recompute closed it. This one walks straight past that fix.

    A one-leg MARGIN LIQUIDATION records its buy-back through
    ``_record_option_expiry_close``, which links the synthetic close to the
    transaction's ENTRY via ``depends_on_order`` (deliberately — so the sim-dated
    row is never mistaken for the entry). That makes it a DEPENDENT order, and
    ``refresh_transactions``' "OPENED -> CLOSED: filled closing order (TP/SL)"
    arm fires on any filled dependent order, BEFORE the ``position_balanced``
    arm is ever consulted. The whole strangle was closed as ``tp_sl_filled`` and
    the surviving put became invisible to ``get_option_positions`` and
    ``_option_transaction_for_contract`` — while its ``_OptionLot`` stayed in the
    ledger, still charged maintenance margin every bar.

    An expiry settlement and an assignment take the same shape and reached the
    same end; the liquidation is used here because it is the one path that
    settles ONE leg of a live structure on a bar of its own choosing.
    """
    engine, acct, ps = engine_with_strangle

    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.trade_store import transactions_where

    txn = transactions_where(status=TransactionStatus.OPENED)[0]

    ps.set_clock(datetime(2024, 3, 7))
    call_lot = acct._option_positions[_CALL]
    assert call_lot.qty < 0                       # the short call, still held

    assert acct._liquidate_option_lot(call_lot) is True
    acct.refresh_transactions()

    fresh = get_instance(Transaction, txn.id)
    assert fresh.status == TransactionStatus.OPENED, (
        f"the strangle was closed by ONE leg's settlement fill (close_reason="
        f"{fresh.close_reason!r}) — the short put is orphaned, unmanageable, and "
        f"its lot keeps accruing maintenance margin"
    )

    held = [p.contract_symbol for p in acct.get_option_positions()]
    assert held == [_PUT], (
        "the surviving put must still be a held position — get_option_positions "
        "only ever finds legs through an OPENED transaction"
    )
    assert acct._option_transaction_for_contract(_PUT) is not None

    # ...and the survivor still settles at expiry, closing the structure once.
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))

    assert acct.get_option_positions() == []
    assert get_instance(Transaction, txn.id).status == TransactionStatus.CLOSED
