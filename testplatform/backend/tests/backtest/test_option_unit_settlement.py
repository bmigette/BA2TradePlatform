"""Unit settlement of DEFINED-RISK option combos at expiry.

Leg-by-leg expiry settlement exercised/assigned each leg to SHARES independently at
its strike (e.g. exercising a deep-ITM long call = buy 100 sh @ strike = big cash
outflow). For a defined-risk combo the offsetting legs' cash flows did NOT net back
to the combo's bounded payoff, so cash/equity blew past the defined-risk bound
(O_BF id=424: butterfly "lost" ~$17k on a $10k account; final -$7,110).

Fix: settle the WHOLE combo AS A UNIT at expiry — compute the net cash payoff from
the legs' intrinsic values (bounded to the structure's [-net_debit, +max_profit] /
credit range), apply it to cash, close the transaction, zero all lots, and create NO
per-leg stock positions. This test pins that behaviour.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_option_unit_settlement.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import OptionRight, OrderDirection


CFG = {
    "starting_cash": 10_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

# Call butterfly strikes 170/180/190 (wing width 10).
_BF_LOW = "AAPL240315C00170000"
_BF_BODY = "AAPL240315C00180000"
_BF_HIGH = "AAPL240315C00190000"

# Iron condor: short 175 put / long 165 put ; short 185 call / long 195 call (wing 10).
_IC_LP = "AAPL240315P00165000"
_IC_SP = "AAPL240315P00175000"
_IC_SC = "AAPL240315C00185000"
_IC_LC = "AAPL240315C00195000"


def _bars(expiry_close):
    return [
        {"Date": datetime(2024, 3, 5), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1000},
        {"Date": datetime(2024, 3, 6), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1000},
        {"Date": datetime(2024, 3, 7), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1000},
        {"Date": datetime(2024, 3, 15), "Open": expiry_close, "High": expiry_close + 1,
         "Low": expiry_close - 1, "Close": expiry_close, "Volume": 1300},
        {"Date": datetime(2024, 3, 18), "Open": expiry_close, "High": expiry_close + 1,
         "Low": expiry_close - 1, "Close": expiry_close, "Volume": 1300},
    ]


def _make_ps(bars, clock):
    from app.services.backtest.price_source import AsOfPriceSource
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", bars)
    ps.set_clock(clock)
    return ps


def _account(tmp_path, chain, bar_rows, bars):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.daily_engine import DailyBacktestEngine

    cache_db = str(tmp_path / "c.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows("AAPL", "2024-03-01", chain)
    cache.write_bar_rows(bar_rows)
    prov = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("unitsettle")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_ps(bars, datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=prov)
    wire_backtest_seams().register_account(1, acct)
    eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
    eng.account = acct
    eng.price = ps
    eng.config = CFG
    return acct, ps, eng, ctx


def _chain_call(sym, strike):
    return {"occ_symbol": sym, "option_type": "call", "strike": strike, "expiry": "2024-03-15",
            "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _chain_put(sym, strike):
    return {"occ_symbol": sym, "option_type": "put", "strike": strike, "expiry": "2024-03-15",
            "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _bar_row(sym, o, ot, strike):
    return {"occ_symbol": sym, "date": "2024-03-06", "open": o, "high": o, "low": o, "close": o,
            "volume": 100, "underlying": "AAPL", "option_type": ot, "strike": strike,
            "expiry": "2024-03-15"}


# ---------------------------------------------------------------------------
# Call butterfly, ALL strikes ITM at expiry (deep move) -> unit-settled, bounded
# ---------------------------------------------------------------------------
def test_butterfly_all_itm_unit_settled_bounded(tmp_path):
    from ba2_common.core.option_types import OptionLeg
    # Entry debit: buy 170@12 + buy 190@1 - 2x sell 180@5 = 13 - 10 = 3.0/share -> $300 for 1 struct.
    bars = _bars(250)  # expiry spot 250: ALL three calls deep ITM
    chain = [_chain_call(_BF_LOW, 170.0), _chain_call(_BF_BODY, 180.0), _chain_call(_BF_HIGH, 190.0)]
    bar_rows = [_bar_row(_BF_LOW, 12.0, "call", 170.0), _bar_row(_BF_BODY, 5.0, "call", 180.0),
                _bar_row(_BF_HIGH, 1.0, "call", 190.0)]
    acct, ps, eng, ctx = _account(tmp_path, chain, bar_rows, bars)
    try:
        low = OptionLeg(contract_symbol=_BF_LOW, side=OrderDirection.BUY, ratio_qty=1,
                        position_intent="buy_to_open", option_type=OptionRight.CALL,
                        strike=170.0, expiry=date(2024, 3, 15), underlying="AAPL")
        body = OptionLeg(contract_symbol=_BF_BODY, side=OrderDirection.SELL, ratio_qty=2,
                         position_intent="sell_to_open", option_type=OptionRight.CALL,
                         strike=180.0, expiry=date(2024, 3, 15), underlying="AAPL")
        high = OptionLeg(contract_symbol=_BF_HIGH, side=OrderDirection.BUY, ratio_qty=1,
                         position_intent="buy_to_open", option_type=OptionRight.CALL,
                         strike=190.0, expiry=date(2024, 3, 15), underlying="AAPL")
        acct.submit_option_order(legs=[low, body, high], quantity=1, order_type="market",
                                 option_strategy="call_butterfly")
        acct.refresh_orders()
        acct.refresh_transactions()
        cash_after_entry = acct._cash
        assert cash_after_entry == pytest.approx(9700.0, abs=1.0)  # -300 debit

        ps.set_clock(datetime(2024, 3, 15))
        eng._apply_option_expiry(datetime(2024, 3, 15))

        # Butterfly payoff at spot >= high strike is 0 (all legs ITM cancel): long 170 (+80),
        # short 2x 180 (-140), long 190 (+60) -> net intrinsic 0. So the combo expires worth 0;
        # the loss is exactly the debit paid (-$300). NO stock positions, cash bounded.
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
        assert acct.get_option_positions() == []
        assert acct._cash >= -1.0  # never blows negative
        # Realized loss == net debit (300), so equity ~ 9700, within [10000-300, 10000+maxprofit].
        eq = acct.equity()
        assert eq == pytest.approx(9700.0, abs=5.0)

        # And it stays put on the next bar (no phantom re-settlement / stock marking).
        ps.set_clock(datetime(2024, 3, 18))
        assert acct.equity() == pytest.approx(9700.0, abs=5.0)
    finally:
        ctx.__exit__(None, None, None)


def test_butterfly_at_body_unit_settled_max_profit(tmp_path):
    """Spot == body strike at expiry -> butterfly max profit = wing_width - debit."""
    from ba2_common.core.option_types import OptionLeg
    bars = _bars(180)  # spot at body 180
    chain = [_chain_call(_BF_LOW, 170.0), _chain_call(_BF_BODY, 180.0), _chain_call(_BF_HIGH, 190.0)]
    bar_rows = [_bar_row(_BF_LOW, 12.0, "call", 170.0), _bar_row(_BF_BODY, 5.0, "call", 180.0),
                _bar_row(_BF_HIGH, 1.0, "call", 190.0)]
    acct, ps, eng, ctx = _account(tmp_path, chain, bar_rows, bars)
    try:
        low = OptionLeg(contract_symbol=_BF_LOW, side=OrderDirection.BUY, ratio_qty=1,
                        position_intent="buy_to_open", option_type=OptionRight.CALL,
                        strike=170.0, expiry=date(2024, 3, 15), underlying="AAPL")
        body = OptionLeg(contract_symbol=_BF_BODY, side=OrderDirection.SELL, ratio_qty=2,
                         position_intent="sell_to_open", option_type=OptionRight.CALL,
                         strike=180.0, expiry=date(2024, 3, 15), underlying="AAPL")
        high = OptionLeg(contract_symbol=_BF_HIGH, side=OrderDirection.BUY, ratio_qty=1,
                         position_intent="buy_to_open", option_type=OptionRight.CALL,
                         strike=190.0, expiry=date(2024, 3, 15), underlying="AAPL")
        acct.submit_option_order(legs=[low, body, high], quantity=1, order_type="market",
                                 option_strategy="call_butterfly")
        acct.refresh_orders()
        acct.refresh_transactions()

        ps.set_clock(datetime(2024, 3, 15))
        eng._apply_option_expiry(datetime(2024, 3, 15))

        # At spot 180: long 170 intrinsic 10, body/high 0 -> combo worth 10/share = $1000.
        # Net = payoff 1000 - debit 300 = +700 -> equity ~ 10700 (== max profit wing_width-debit).
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
        assert acct._cash >= -1.0
        assert acct.equity() == pytest.approx(10_700.0, abs=5.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Iron condor fully breached on the call side -> realized loss <= max_loss
# ---------------------------------------------------------------------------
def test_iron_condor_call_side_breach_bounded(tmp_path):
    from ba2_common.core.option_types import OptionLeg
    bars = _bars(250)  # spot 250: both calls ITM (breach), puts OTM
    chain = [_chain_put(_IC_LP, 165.0), _chain_put(_IC_SP, 175.0),
             _chain_call(_IC_SC, 185.0), _chain_call(_IC_LC, 195.0)]
    # Entry credit: sell 175p@2 + sell 185c@2 - buy 165p@0.5 - buy 195c@0.5 = 4 - 1 = 3.0 credit.
    bar_rows = [_bar_row(_IC_LP, 0.5, "put", 165.0), _bar_row(_IC_SP, 2.0, "put", 175.0),
                _bar_row(_IC_SC, 2.0, "call", 185.0), _bar_row(_IC_LC, 0.5, "call", 195.0)]
    acct, ps, eng, ctx = _account(tmp_path, chain, bar_rows, bars)
    try:
        lp = OptionLeg(contract_symbol=_IC_LP, side=OrderDirection.BUY, position_intent="buy_to_open",
                       option_type=OptionRight.PUT, strike=165.0, expiry=date(2024, 3, 15), underlying="AAPL")
        sp = OptionLeg(contract_symbol=_IC_SP, side=OrderDirection.SELL, position_intent="sell_to_open",
                       option_type=OptionRight.PUT, strike=175.0, expiry=date(2024, 3, 15), underlying="AAPL")
        sc = OptionLeg(contract_symbol=_IC_SC, side=OrderDirection.SELL, position_intent="sell_to_open",
                       option_type=OptionRight.CALL, strike=185.0, expiry=date(2024, 3, 15), underlying="AAPL")
        lc = OptionLeg(contract_symbol=_IC_LC, side=OrderDirection.BUY, position_intent="buy_to_open",
                       option_type=OptionRight.CALL, strike=195.0, expiry=date(2024, 3, 15), underlying="AAPL")
        acct.submit_option_order(legs=[lp, sp, sc, lc], quantity=1, order_type="market",
                                 option_strategy="iron_condor")
        acct.refresh_orders()
        acct.refresh_transactions()
        cash_after_entry = acct._cash
        assert cash_after_entry == pytest.approx(10_300.0, abs=1.0)  # +300 credit

        ps.set_clock(datetime(2024, 3, 15))
        eng._apply_option_expiry(datetime(2024, 3, 15))

        # Call side fully breached (spot 250): short 185c intrinsic 65, long 195c intrinsic 55 ->
        # net loss = (65 - 55) = 10/share = $1000 (the wing width). Puts expire worthless.
        # Realized net = credit 300 - 1000 = -700. Max loss = width(10)*100 - credit(300) = 700.
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
        assert acct.get_option_positions() == []
        eq = acct.equity()
        max_loss = 10.0 * 100.0 - 300.0  # width*100 - credit = 700
        assert eq >= CFG["starting_cash"] - max_loss - 1.0   # loss bounded by max_loss
        assert eq == pytest.approx(9_300.0, abs=5.0)
        assert acct._cash >= -1.0
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 2-LEG VERTICAL (bear_put_spread) — the O_VERT regression: spot BETWEEN strikes
# at expiry (long higher-strike put ITM, short lower-strike put OTM). Must unit-settle
# to the bounded net, NEVER leave an unhedged short-stock leg. Not special-cased by
# leg count — a 2-leg vertical settles exactly like the 3-4-leg combos.
# ---------------------------------------------------------------------------
_V_LONG = "AAPL240315P00180000"   # long 180 put (higher strike)
_V_SHORT = "AAPL240315P00170000"  # short 170 put (lower strike); width 10


def _open_bear_put_spread(acct, qty):
    from ba2_common.core.option_types import OptionLeg
    long_leg = OptionLeg(contract_symbol=_V_LONG, side=OrderDirection.BUY, position_intent="buy_to_open",
                         option_type=OptionRight.PUT, strike=180.0, expiry=date(2024, 3, 15), underlying="AAPL")
    short_leg = OptionLeg(contract_symbol=_V_SHORT, side=OrderDirection.SELL, position_intent="sell_to_open",
                          option_type=OptionRight.PUT, strike=170.0, expiry=date(2024, 3, 15), underlying="AAPL")
    acct.submit_option_order(legs=[long_leg, short_leg], quantity=qty, order_type="market",
                             option_strategy="bear_put_spread")
    acct.refresh_orders()
    acct.refresh_transactions()


def test_bear_put_spread_between_strikes_unit_settled(tmp_path):
    """spot 175 at expiry (between 170 and 180): long 180 put ITM 5/sh, short 170 put OTM.
    Must unit-settle to net +5/sh with NO unhedged short stock; equity bounded and >= 0."""
    bars = _bars(175)
    chain = [_chain_put(_V_LONG, 180.0), _chain_put(_V_SHORT, 170.0)]
    # Entry debit: buy 180p @5 - sell 170p @2 = 3/share -> $300/structure. 5 structures -> $1500.
    bar_rows = [_bar_row(_V_LONG, 5.0, "put", 180.0), _bar_row(_V_SHORT, 2.0, "put", 170.0)]
    acct, ps, eng, ctx = _account(tmp_path, chain, bar_rows, bars)
    try:
        _open_bear_put_spread(acct, 5)
        assert acct._cash == pytest.approx(8500.0, abs=1.0)  # -1500 debit

        ps.set_clock(datetime(2024, 3, 15))
        eng._apply_option_expiry(datetime(2024, 3, 15))

        # NO stock (the O_VERT bug was an unhedged short-stock leg from exercising the long put),
        # combo fully resolved, bounded.
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
        assert acct.get_option_positions() == []
        assert acct._cash >= -1.0
        # net payoff = +5/sh * 100 * 5 = +2500; equity = 8500 + 2500 = 11000 (within
        # [entry-debit-loss, entry+max_width_profit]).
        eq = acct.equity()
        assert eq == pytest.approx(11_000.0, abs=5.0)
        # persists next bar (no phantom stock marking)
        ps.set_clock(datetime(2024, 3, 18))
        assert acct.equity() == pytest.approx(11_000.0, abs=5.0)
    finally:
        ctx.__exit__(None, None, None)


def test_bear_put_spread_worst_case_bounded(tmp_path):
    """spot 250 at expiry (above both strikes): both puts OTM -> the spread expires worthless,
    realizing exactly the debit loss (-$1500 on 5 structures). Never worse than -debit, no stock."""
    bars = _bars(250)
    chain = [_chain_put(_V_LONG, 180.0), _chain_put(_V_SHORT, 170.0)]
    bar_rows = [_bar_row(_V_LONG, 5.0, "put", 180.0), _bar_row(_V_SHORT, 2.0, "put", 170.0)]
    acct, ps, eng, ctx = _account(tmp_path, chain, bar_rows, bars)
    try:
        _open_bear_put_spread(acct, 5)
        ps.set_clock(datetime(2024, 3, 15))
        eng._apply_option_expiry(datetime(2024, 3, 15))
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
        assert acct.get_option_positions() == []
        eq = acct.equity()
        # worst case = lose the debit only: 10000 - 1500 = 8500. Bounded, positive.
        assert eq == pytest.approx(8500.0, abs=5.0)
        assert eq >= CFG["starting_cash"] - 1500.0 - 1.0
    finally:
        ctx.__exit__(None, None, None)


def test_bear_put_spread_closed_midlife_no_unhedged_stock(tmp_path):
    """A vertical CLOSED mid-life (exit rule -> reversed multi-leg close order) must close BOTH
    legs atomically the same bar, leaving NO unhedged stock and bounded cash."""
    from ba2_common.core.option_types import OptionLeg
    # bars: entry 03-06, mid-life close on 03-08, next bar 03-09 (premiums present on all).
    bars = [
        {"Date": datetime(2024, 3, 5), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 100},
        {"Date": datetime(2024, 3, 6), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 100},
        {"Date": datetime(2024, 3, 8), "Open": 175, "High": 176, "Low": 174, "Close": 175, "Volume": 100},
        {"Date": datetime(2024, 3, 9), "Open": 175, "High": 176, "Low": 174, "Close": 175, "Volume": 100},
    ]
    chain = [_chain_put(_V_LONG, 180.0), _chain_put(_V_SHORT, 170.0)]
    bar_rows = []
    for d in ("2024-03-06", "2024-03-08", "2024-03-09"):
        bar_rows.append({"occ_symbol": _V_LONG, "date": d, "open": 6.0, "high": 6.0, "low": 6.0, "close": 6.0,
                         "volume": 100, "underlying": "AAPL", "option_type": "put", "strike": 180.0, "expiry": "2024-03-15"})
        bar_rows.append({"occ_symbol": _V_SHORT, "date": d, "open": 2.5, "high": 2.5, "low": 2.5, "close": 2.5,
                         "volume": 100, "underlying": "AAPL", "option_type": "put", "strike": 170.0, "expiry": "2024-03-15"})
    acct, ps, eng, ctx = _account(tmp_path, chain, bar_rows, bars)
    try:
        _open_bear_put_spread(acct, 5)
        # Locate the spread parent + build a reversed closing combo (mirrors CloseOptionAction._close_multi_leg).
        from ba2_common.core.models import TradingOrder
        from ba2_common.core.db import get_db
        from sqlmodel import select, Session
        with Session(get_db().bind) as sn:
            parent = [o for o in sn.exec(select(TradingOrder)).all()
                      if o.parent_order_id is None and o.option_strategy == "bear_put_spread"][0]
            pq = int(parent.quantity)
            txn_id = parent.transaction_id

        ps.set_clock(datetime(2024, 3, 8))
        close_legs = [
            OptionLeg(contract_symbol=_V_LONG, side=OrderDirection.SELL, position_intent="sell_to_close",
                      option_type=OptionRight.PUT, strike=180.0, expiry=date(2024, 3, 15), underlying="AAPL"),
            OptionLeg(contract_symbol=_V_SHORT, side=OrderDirection.BUY, position_intent="buy_to_close",
                      option_type=OptionRight.PUT, strike=170.0, expiry=date(2024, 3, 15), underlying="AAPL"),
        ]
        acct.submit_option_order(legs=close_legs, quantity=pq, order_type="limit", limit_price=-3.5,
                                 option_strategy="close", transaction_id=txn_id)
        acct.refresh_orders()
        acct.refresh_transactions()

        # BOTH legs closed the same bar -> no option lots, NO unhedged stock, equity bounded.
        assert [l for l in acct._option_positions.values() if l.qty] == []
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
        assert acct._cash >= -1.0
        eq = acct.equity()
        assert eq >= 0.0
        assert eq == pytest.approx(10_000.0, abs=100.0)  # closed near flat (small round-trip)
        ps.set_clock(datetime(2024, 3, 9))
        acct.refresh_orders()
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
    finally:
        ctx.__exit__(None, None, None)
