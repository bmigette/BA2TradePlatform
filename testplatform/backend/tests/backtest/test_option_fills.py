"""Phase 1 Task 6: bar-based single-leg option leg fills + premium marking.

The fill engine FILLS a single-leg option order off the cached premium bar for its
contract (per ``fill_model``), exactly mirroring how the equity branch chooses its
fill bar (``next_bar_open`` -> the contract's next bar open; ``same_bar_close`` -> the
current bar close). Open option positions are MARKED each bar at the current premium
close x open qty x multiplier (100), so the equity curve includes options.

Equity fill/marking behaviour is untouched: this only adds an OPTION-only branch and
guards the equity branch to skip OPTION orders.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_option_fills.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import OrderDirection, OrderStatus

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 1.0,
    "slippage_bps": 0.0,  # no slippage -> fill premium == bar open exactly (deterministic)
    "fill_model": "next_bar_open",
}

_OCC = "AAPL240315C00180000"

# Multi-leg (bull call spread) contracts: long the 180 call, short the 190 call.
_OCC_LONG = "AAPL240315C00180000"   # == _OCC (the 180 call)
_OCC_SHORT = "AAPL240315C00190000"  # the 190 call

# Two underlying bars so there IS a "next bar" relative to the submit/clock bar.
_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 180, "High": 182, "Low": 178, "Close": 181, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 181, "High": 184, "Low": 180, "Close": 183, "Volume": 1100},
]


def _seed_cache(db_path: str) -> None:
    """Seed the CALL chain (2024-03-01) AND a premium bar for the contract on 2024-03-06.

    The fill bar (next_bar_open relative to the 2024-03-05 clock) is 2024-03-06: the
    order fills at that bar's OPEN premium (4.0). The same bar's CLOSE (4.5) is what
    the per-bar marking values the open position at.
    """
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows(
        "AAPL",
        "2024-03-01",
        [
            {
                "occ_symbol": _OCC,
                "option_type": "call",
                "strike": 180.0,
                "expiry": "2024-03-15",
                "bid": 3.0,
                "ask": 3.2,
                "last": 3.1,
                "iv": 0.25,
            },
        ],
    )
    cache.write_bar_rows(
        [
            {
                "occ_symbol": _OCC,
                "date": "2024-03-06",
                "open": 4.0,
                "high": 4.8,
                "low": 3.9,
                "close": 4.5,
                "volume": 500,
                "underlying": "AAPL",
                "option_type": "call",
                "strike": 180.0,
                "expiry": "2024-03-15",
            },
        ]
    )


def _make_price_source(clock: datetime):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)  # no provider; bars pre-seeded
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(clock)
    return ps


@pytest.fixture
def options_account(tmp_path):
    """A BacktestAccount wired to a HistoricalOptionsProvider over a seeded temp cache.

    Mirrors the Task-4 ``options_account`` construction (fresh per-run trading DB + seam
    wiring + seeded account definition + injected options provider). The price source is
    returned with the account so the test can step the clock to the fill bar.
    """
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider

    cache_db = str(tmp_path / "options_cache.sqlite")
    _seed_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("optfills")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_price_source(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)
    try:
        yield acct, ps
    finally:
        ctx.__exit__(None, None, None)


def _submit_market_buy_call(acct):
    from ba2_common.core.option_types import OptionLeg

    leg = OptionLeg(
        contract_symbol=_OCC,
        side=OrderDirection.BUY,
        position_intent="buy_to_open",
        underlying="AAPL",
    )
    return acct.submit_option_order(
        legs=[leg], quantity=1, order_type="market", option_strategy="long_call"
    )


def test_single_leg_market_call_fills_at_next_bar_open_premium(options_account):
    acct, ps = options_account
    order = _submit_market_buy_call(acct)
    assert order is not None
    assert order.status == OrderStatus.ACCEPTED  # staged, not yet filled on submit bar

    # next_bar_open convention (mirrors the equity branch): with the clock on the submit
    # bar (2024-03-05), refresh_orders fills at the NEXT bar (2024-03-06) open premium.
    acct.refresh_orders()
    acct.refresh_transactions()

    filled = acct.get_order(order.id)
    assert filled.status == OrderStatus.FILLED
    # next_bar_open with zero slippage -> the fill bar's OPEN premium (4.0), per share.
    assert filled.open_price == pytest.approx(4.0)
    assert filled.filled_qty == 1

    # the held option position shows up with qty 1 on the contract
    positions = acct.get_option_positions()
    assert len(positions) == 1
    assert positions[0].contract_symbol == _OCC
    assert positions[0].quantity == 1


def test_option_does_not_fill_on_entry_bar(options_account):
    """No look-ahead: with the clock on the LAST bar there is no next bar, so no fill."""
    acct, ps = options_account
    order = _submit_market_buy_call(acct)
    ps.set_clock(datetime(2024, 3, 6))  # last bar -> next_bar is None
    acct.refresh_orders()
    assert acct.get_order(order.id).status == OrderStatus.ACCEPTED


def test_option_fill_marks_premium_times_multiplier_in_equity(options_account):
    acct, ps = options_account
    cash_before = acct.get_balance()

    _submit_market_buy_call(acct)
    acct.refresh_orders()  # fills at 2024-03-06 open premium (4.0)
    acct.refresh_transactions()

    commission = CFG["commission_per_trade"]
    # Bought 1 contract @ 4.0 premium x 100 multiplier = $400 debit, + $1 commission.
    assert acct.get_balance() == pytest.approx(cash_before - 4.0 * 100 - commission)

    # snapshot_equity marks the OPEN option at the current bar's CLOSE premium. Step the
    # clock to the day that HAS the premium bar (2024-03-06): close = 4.5.
    ps.set_clock(datetime(2024, 3, 6))
    snap = acct.snapshot_equity(datetime(2024, 3, 6))
    assert snap["equity_value"] == pytest.approx(4.5 * 100)
    assert snap["net_liquidating_value"] == pytest.approx(acct.get_balance() + 4.5 * 100)


def test_option_position_value_falls_back_to_open_price_without_bar(options_account):
    """No premium bar on the marking day -> value the open option at its fill premium."""
    acct, ps = options_account
    _submit_market_buy_call(acct)
    acct.refresh_orders()  # fills at 2024-03-06 open premium (4.0)
    acct.refresh_transactions()

    # Step to a day with NO premium bar for the contract: marking falls back to open_price (4.0).
    ps.load_bars(
        "AAPL",
        _AAPL_BARS
        + [{"Date": datetime(2024, 3, 7), "Open": 183, "High": 185, "Low": 182, "Close": 184, "Volume": 900}],
    )
    ps.set_clock(datetime(2024, 3, 7))
    snap = acct.snapshot_equity(datetime(2024, 3, 7))
    assert snap["equity_value"] == pytest.approx(4.0 * 100)


# ======================================================================
# Phase 1 Task 8: multi-leg (spread/straddle) ALL-OR-NONE fills
# ======================================================================
def _seed_short_leg_bar(db_path: str) -> None:
    """Add the SHORT leg (190 call) to the chain + a premium bar on the fill day (03-06).

    The 180 call (long leg) is already seeded by ``_seed_cache``. With next_bar_open and
    zero slippage the legs fill at their bar OPENs: long 180c @ 4.0, short 190c @ 1.5,
    so the spread's net debit is 4.0 - 1.5 = 2.5 per share.
    """
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows(
        "AAPL",
        "2024-03-01",
        [
            {
                "occ_symbol": _OCC_SHORT,
                "option_type": "call",
                "strike": 190.0,
                "expiry": "2024-03-15",
                "bid": 1.4,
                "ask": 1.6,
                "last": 1.5,
                "iv": 0.22,
            },
        ],
    )
    cache.write_bar_rows(
        [
            {
                "occ_symbol": _OCC_SHORT,
                "date": "2024-03-06",
                "open": 1.5,
                "high": 1.8,
                "low": 1.4,
                "close": 1.7,
                "volume": 300,
                "underlying": "AAPL",
                "option_type": "call",
                "strike": 190.0,
                "expiry": "2024-03-15",
            },
        ]
    )


def _bull_call_spread_legs():
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OptionRight

    long_leg = OptionLeg(
        contract_symbol=_OCC_LONG,
        side=OrderDirection.BUY,
        position_intent="buy_to_open",
        option_type=OptionRight.CALL,
        strike=180.0,
        expiry=date(2024, 3, 15),
        underlying="AAPL",
    )
    short_leg = OptionLeg(
        contract_symbol=_OCC_SHORT,
        side=OrderDirection.SELL,
        position_intent="sell_to_open",
        option_type=OptionRight.CALL,
        strike=190.0,
        expiry=date(2024, 3, 15),
        underlying="AAPL",
    )
    return long_leg, short_leg


def test_multi_leg_spread_fills_all_legs_at_net_debit(options_account, tmp_path):
    """Both legs of a bull call spread fill on the bar where both have a premium bar.

    Parent FILLED at open_price == net debit = long premium (4.0) - short premium (1.5) = 2.5.
    Both contracts become tracked positions.
    """
    acct, ps = options_account
    # The fixture seeds only the long-leg cache; add the short leg to the SAME cache db.
    _seed_short_leg_bar(str(tmp_path / "options_cache.sqlite"))
    cash_before = acct.get_balance()

    long_leg, short_leg = _bull_call_spread_legs()
    parent = acct.submit_option_order(
        legs=[long_leg, short_leg],
        quantity=1,
        order_type="limit",
        limit_price=2.0,
        option_strategy="bull_call_spread",
    )
    assert parent is not None
    assert parent.status == OrderStatus.ACCEPTED

    # clock on the submit bar (2024-03-05) -> next_bar_open is 2024-03-06 (both legs have a bar).
    acct.refresh_orders()
    acct.refresh_transactions()

    filled_parent = acct.get_order(parent.id)
    assert filled_parent.status == OrderStatus.FILLED
    assert filled_parent.filled_qty == 1
    # net per-share debit = buy premium - sell premium = 4.0 - 1.5 = 2.5
    assert filled_parent.open_price == pytest.approx(2.5)

    positions = {p.contract_symbol for p in acct.get_option_positions()}
    assert _OCC_LONG in positions
    assert _OCC_SHORT in positions

    # Cash moved per leg (NOT double-counted on the parent): buy 180c @4.0 = -$400,
    # sell 190c @1.5 = +$150, two $1 commissions. Net = -(400) + 150 - 2 = -252.
    commission = CFG["commission_per_trade"]
    assert acct.get_balance() == pytest.approx(
        cash_before - 4.0 * 100 + 1.5 * 100 - 2 * commission
    )


def test_multi_leg_spread_is_all_or_none_when_one_leg_lacks_a_bar(options_account, tmp_path):
    """If only one leg has a premium bar on the fill day, NEITHER leg fills (retry next bar)."""
    acct, ps = options_account
    # Do NOT seed the short leg -> only the long leg (180 call) has a bar on 2024-03-06.
    long_leg, short_leg = _bull_call_spread_legs()
    parent = acct.submit_option_order(
        legs=[long_leg, short_leg],
        quantity=1,
        order_type="limit",
        limit_price=2.0,
        option_strategy="bull_call_spread",
    )

    cash_before = acct.get_balance()
    acct.refresh_orders()
    acct.refresh_transactions()

    # All-or-none: parent still ACCEPTED, no positions opened, cash untouched.
    assert acct.get_order(parent.id).status == OrderStatus.ACCEPTED
    assert acct.get_option_positions() == []
    assert acct.get_balance() == pytest.approx(cash_before)

    # The 180-call CHILD has a bar but MUST NOT fill on its own (double-fill guard): every
    # child of the parent stays ACCEPTED until ALL legs can price together.
    children = [o for o in acct.get_orders() if o.parent_order_id == parent.id]
    assert len(children) == 2
    assert all(c.status == OrderStatus.ACCEPTED for c in children)


# ======================================================================
# B3: ratio-weighted parent open_price for ratio'd structures (1-2-1, 1-2)
# ======================================================================
_OCC_BF_LOW = "AAPL240315C00170000"   # butterfly lower wing (170 call)
_OCC_PR_LONG = "AAPL240315P00180000"  # put-ratio long leg (180 put)
_OCC_PR_SHORT = "AAPL240315P00170000"  # put-ratio short leg (170 put)


def _seed_extra_bars(db_path: str, rows) -> None:
    from app.services.backtest.options_cache import OptionsHistoryCache

    OptionsHistoryCache(db_path).write_bar_rows(rows)


def _bar_row(occ, o, ot, strike, d="2024-03-06", c=None, h=None, l=None):
    return {
        "occ_symbol": occ,
        "date": d,
        "open": o,
        "high": h if h is not None else o,
        "low": l if l is not None else o,
        "close": c if c is not None else o,
        "volume": 100,
        "underlying": "AAPL",
        "option_type": ot,
        "strike": strike,
        "expiry": "2024-03-15",
    }


def test_butterfly_parent_open_price_is_ratio_weighted(options_account, tmp_path):
    """1-2-1 call butterfly: parent open_price = Σ(sign x premium x ratio) PER STRUCTURE.

    Legs fill at their bar opens: buy 1x 170c @8.0, sell 2x 180c @4.0, buy 1x 190c @1.5.
    Ratio-weighted net debit = 8.0 - 2*4.0 + 1.5 = 1.5 per share per structure (the OLD
    unweighted math gave 8.0 - 4.0 + 1.5 = 5.5). Cash still moves PER LEG, so the total
    debit equals open_price x 100 x structures.
    """
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OptionRight

    acct, ps = options_account
    _seed_short_leg_bar(str(tmp_path / "options_cache.sqlite"))  # 190c bar (open 1.5)
    _seed_extra_bars(str(tmp_path / "options_cache.sqlite"),
                     [_bar_row(_OCC_BF_LOW, 8.0, "call", 170.0)])
    cash_before = acct.get_balance()

    def _leg(sym, side, ratio, strike):
        return OptionLeg(
            contract_symbol=sym, side=side, ratio_qty=ratio,
            position_intent=("buy_to_open" if side == OrderDirection.BUY else "sell_to_open"),
            option_type=OptionRight.CALL, strike=strike, expiry=date(2024, 3, 15),
            underlying="AAPL",
        )

    parent = acct.submit_option_order(
        legs=[_leg(_OCC_BF_LOW, OrderDirection.BUY, 1, 170.0),
              _leg(_OCC, OrderDirection.SELL, 2, 180.0),
              _leg(_OCC_SHORT, OrderDirection.BUY, 1, 190.0)],
        quantity=1, order_type="market", option_strategy="call_butterfly",
    )
    acct.refresh_orders()
    acct.refresh_transactions()

    filled = acct.get_order(parent.id)
    assert filled.status == OrderStatus.FILLED
    # ratio-weighted: 8.0 - 2*4.0 + 1.5 = 1.5 (NOT 5.5)
    assert filled.open_price == pytest.approx(1.5)
    # Per-leg cash: -800 + 2*400 - 150 = -150 == -open_price*100, plus 3 flat commissions.
    commission = CFG["commission_per_trade"]
    assert acct.get_balance() == pytest.approx(cash_before - 1.5 * 100 - 3 * commission)


def test_put_ratio_parent_open_price_is_ratio_weighted(options_account, tmp_path):
    """1-2 put ratio spread: net = 5.0 - 2*3.0 = -1.0 per structure (a CREDIT).

    The OLD unweighted math gave 5.0 - 3.0 = +2.0 — wrong sign AND magnitude.
    """
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OptionRight

    acct, ps = options_account
    _seed_extra_bars(str(tmp_path / "options_cache.sqlite"), [
        _bar_row(_OCC_PR_LONG, 5.0, "put", 180.0),
        _bar_row(_OCC_PR_SHORT, 3.0, "put", 170.0),
    ])
    cash_before = acct.get_balance()

    def _leg(sym, side, ratio, strike):
        return OptionLeg(
            contract_symbol=sym, side=side, ratio_qty=ratio,
            position_intent=("buy_to_open" if side == OrderDirection.BUY else "sell_to_open"),
            option_type=OptionRight.PUT, strike=strike, expiry=date(2024, 3, 15),
            underlying="AAPL",
        )

    parent = acct.submit_option_order(
        legs=[_leg(_OCC_PR_LONG, OrderDirection.BUY, 1, 180.0),
              _leg(_OCC_PR_SHORT, OrderDirection.SELL, 2, 170.0)],
        quantity=1, order_type="market", option_strategy="put_ratio_spread",
    )
    acct.refresh_orders()
    acct.refresh_transactions()

    filled = acct.get_order(parent.id)
    assert filled.status == OrderStatus.FILLED
    # ratio-weighted: 5.0 - 2*3.0 = -1.0 (credit; NOT +2.0)
    assert filled.open_price == pytest.approx(-1.0)
    # Per-leg cash: -500 + 2*300 = +100 == -open_price*100, plus 2 flat commissions.
    commission = CFG["commission_per_trade"]
    assert acct.get_balance() == pytest.approx(cash_before + 1.0 * 100 - 2 * commission)


# ======================================================================
# B5: LIMIT option orders fill only when the bar premium crosses the limit
# ======================================================================
def _submit_limit_buy_call(acct, limit):
    from ba2_common.core.option_types import OptionLeg

    leg = OptionLeg(
        contract_symbol=_OCC, side=OrderDirection.BUY, position_intent="buy_to_open",
        underlying="AAPL",
    )
    return acct.submit_option_order(
        legs=[leg], quantity=1, order_type="limit", limit_price=limit,
        option_strategy="long_call",
    )


def _extend_to_0307(ps, db_path, option_open_0307):
    """Add an AAPL bar AND a contract premium bar for 2024-03-07 (the next fill day)."""
    ps.load_bars(
        "AAPL",
        _AAPL_BARS
        + [{"Date": datetime(2024, 3, 7), "Open": 183, "High": 185, "Low": 182, "Close": 184, "Volume": 900}],
    )
    _seed_extra_bars(db_path, [_bar_row(_OCC, option_open_0307, "call", 180.0, d="2024-03-07")])
    # The provider memoizes per-contract bar histories process-wide (worker cache); the
    # mid-test reseed above is only visible after an explicit reset.
    from app.services.backtest.options_provider import clear_worker_options_cache
    clear_worker_options_cache()


def test_limit_buy_above_market_does_not_fill_then_fills_on_cross(options_account, tmp_path):
    """BUY_LIMIT 3.0 vs bar open 4.0: NO fill (stays working, cash untouched). When the
    next fill bar opens at 2.5 (<= limit), it fills AT THE BAR PRICE (better than limit)."""
    acct, ps = options_account
    cache_db = str(tmp_path / "options_cache.sqlite")
    order = _submit_limit_buy_call(acct, 3.0)
    cash_before = acct.get_balance()

    # Fill bar = 2024-03-06, premium open 4.0 > limit 3.0 -> the limit is NOT crossed.
    acct.refresh_orders()
    acct.refresh_transactions()
    assert acct.get_order(order.id).status == OrderStatus.ACCEPTED
    assert acct.get_option_positions() == []
    assert acct.get_balance() == pytest.approx(cash_before)

    # Next bar (2024-03-07) opens at 2.5 <= 3.0 -> crosses -> fills at 2.5 (bar price is
    # better than the limit), NOT at the limit and NOT at 4.0.
    _extend_to_0307(ps, cache_db, 2.5)
    ps.set_clock(datetime(2024, 3, 6))
    acct.refresh_orders()
    acct.refresh_transactions()
    filled = acct.get_order(order.id)
    assert filled.status == OrderStatus.FILLED
    assert filled.open_price == pytest.approx(2.5)
    commission = CFG["commission_per_trade"]
    assert acct.get_balance() == pytest.approx(cash_before - 2.5 * 100 - commission)


def test_limit_sell_below_market_does_not_fill_then_fills_on_cross(options_account, tmp_path):
    """SELL_LIMIT 5.0 vs bar open 4.0: NO fill. When the next fill bar opens at 5.5
    (>= limit), it fills AT THE BAR PRICE 5.5 (better than the limit)."""
    from ba2_common.core.option_types import OptionLeg

    acct, ps = options_account
    cache_db = str(tmp_path / "options_cache.sqlite")
    leg = OptionLeg(
        contract_symbol=_OCC, side=OrderDirection.SELL, position_intent="sell_to_open",
        underlying="AAPL",
    )
    order = acct.submit_option_order(
        legs=[leg], quantity=1, order_type="limit", limit_price=5.0,
        option_strategy="naked_call",
    )
    cash_before = acct.get_balance()

    # Fill bar = 2024-03-06, premium open 4.0 < limit 5.0 -> NOT crossed -> stays working.
    acct.refresh_orders()
    acct.refresh_transactions()
    assert acct.get_order(order.id).status == OrderStatus.ACCEPTED
    assert acct.get_option_positions() == []
    assert acct.get_balance() == pytest.approx(cash_before)

    # Next bar opens at 5.5 >= 5.0 -> crosses -> fills at 5.5 (credit at the bar price).
    _extend_to_0307(ps, cache_db, 5.5)
    ps.set_clock(datetime(2024, 3, 6))
    acct.refresh_orders()
    acct.refresh_transactions()
    filled = acct.get_order(order.id)
    assert filled.status == OrderStatus.FILLED
    assert filled.open_price == pytest.approx(5.5)
    commission = CFG["commission_per_trade"]
    assert acct.get_balance() == pytest.approx(cash_before + 5.5 * 100 - commission)
