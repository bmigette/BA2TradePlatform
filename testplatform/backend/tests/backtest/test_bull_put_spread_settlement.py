"""The bull put credit spread in the BACKTEST: MTM clamp + unit combo-expiry settlement.

Both behaviours are selected BY STRATEGY NAME, from
``BacktestAccount.DEFINED_RISK_SHORT_STRATEGIES``. A structure missing from that set is
not rejected — it is simply treated as undefined-risk, and BOTH of the following silently
revert to the pre-2026 behaviour that produced the O_BF -473% drawdown and the id=424
"$17k loss on a $10k account":

* **mid-life MTM** is marked leg-by-leg off the sparse options cache, so ONE outlier
  premium print (x100 x contracts) swings recorded equity/drawdown far outside anything
  the structure can be worth. A short 175 put printing $800 on a $2 option is
  -$80,000 of "loss" on a spread whose entire risk is $1,000.
* **expiry** settles leg-by-leg into SHARES at each strike. For a put vertical that means
  being PUT 100 shares at 175 and separately exercising a 165 put — gross flows that
  dwarf the account before they net back, and, if only one leg resolves, an unhedged
  stock position that never existed in the trade.

This is precisely why ``PremiumSeller.structures.build_put_credit_spread`` was not a
drop-in: it never carried the ``bull_put_spread`` tag into this set.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_bull_put_spread_settlement.py -q
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

# Bull put spread on AAPL: SHORT the 175 put, LONG the 165 wing. Width = 10.
_SP = "AAPL240315P00175000"        # short leg — the HIGHER strike
_LP = "AAPL240315P00165000"        # long wing — the LOWER strike
WIDTH = 10.0
#: sell 175p @ 2.00, buy 165p @ 0.50 -> net credit 1.50/share = $150 per structure.
CREDIT_PER_SHARE = 1.5
MAX_LOSS = WIDTH * 100.0 - CREDIT_PER_SHARE * 100.0      # 1000 - 150 = 850


def _bars(expiry_close, outlier_bar=False):
    rows = [
        {"Date": datetime(2024, 3, 5), "Open": 180, "High": 181, "Low": 179, "Close": 180,
         "Volume": 1000},
        {"Date": datetime(2024, 3, 6), "Open": 180, "High": 181, "Low": 179, "Close": 180,
         "Volume": 1000},
    ]
    if outlier_bar:
        rows.append({"Date": datetime(2024, 3, 8), "Open": 180, "High": 181, "Low": 179,
                     "Close": 180, "Volume": 1200})
    rows.append({"Date": datetime(2024, 3, 15), "Open": expiry_close, "High": expiry_close + 1,
                 "Low": expiry_close - 1, "Close": expiry_close, "Volume": 1300})
    rows.append({"Date": datetime(2024, 3, 18), "Open": expiry_close, "High": expiry_close + 1,
                 "Low": expiry_close - 1, "Close": expiry_close, "Volume": 1300})
    return rows


def _chain_put(sym, strike):
    return {"occ_symbol": sym, "option_type": "put", "strike": strike,
            "expiry": "2024-03-15", "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _bar_row(sym, d, px, strike):
    return {"occ_symbol": sym, "date": d, "open": px, "high": px, "low": px, "close": px,
            "volume": 100, "underlying": "AAPL", "option_type": "put", "strike": strike,
            "expiry": "2024-03-15"}


def _account(tmp_path, bar_rows, bars):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.price_source import AsOfPriceSource

    cache_db = str(tmp_path / "bps.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows("AAPL", "2024-03-01",
                           [_chain_put(_LP, 165.0), _chain_put(_SP, 175.0)])
    cache.write_bar_rows(bar_rows)
    prov = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("bullputspread")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", bars)
    ps.set_clock(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=prov)
    wire_backtest_seams().register_account(1, acct)
    eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
    eng.account = acct
    eng.price = ps
    eng.config = CFG
    return acct, ps, eng, ctx


def _open_spread(acct, quantity=1):
    from ba2_common.core.option_types import OptionLeg

    short = OptionLeg(contract_symbol=_SP, side=OrderDirection.SELL,
                      position_intent="sell_to_open", option_type=OptionRight.PUT,
                      strike=175.0, expiry=date(2024, 3, 15), underlying="AAPL")
    long_ = OptionLeg(contract_symbol=_LP, side=OrderDirection.BUY,
                      position_intent="buy_to_open", option_type=OptionRight.PUT,
                      strike=165.0, expiry=date(2024, 3, 15), underlying="AAPL")
    acct.submit_option_order(legs=[short, long_], quantity=quantity, order_type="market",
                             option_strategy="bull_put_spread")
    acct.refresh_orders()
    acct.refresh_transactions()


# ==========================================================================
# the tag itself
# ==========================================================================
def test_the_strategy_is_tagged_defined_risk_SHORT():
    """SHORT, not LONG: the clamp direction follows this membership. In the LONG set a
    credit combo's negative MTM is floored at 0 — the structure's entire downside
    disappears from the equity curve."""
    from app.services.backtest.backtest_account import BacktestAccount

    assert "bull_put_spread" in BacktestAccount.DEFINED_RISK_SHORT_STRATEGIES
    assert "bull_put_spread" not in BacktestAccount.DEFINED_RISK_LONG_STRATEGIES


def test_the_defined_risk_width_of_a_two_strike_put_vertical_is_the_gap():
    from app.services.backtest.backtest_account import BacktestAccount

    w = BacktestAccount._defined_risk_width_per_structure
    assert w("bull_put_spread", [165.0, 175.0]) == pytest.approx(WIDTH)
    assert w("bull_put_spread", [175.0]) is None          # unboundable


# ==========================================================================
# mid-life MTM
# ==========================================================================
@pytest.fixture
def outlier_account(tmp_path):
    bars = _bars(180, outlier_bar=True)
    bar_rows = [
        _bar_row(_SP, "2024-03-06", 2.0, 175.0),
        _bar_row(_LP, "2024-03-06", 0.5, 165.0),
        # MID-LIFE 2024-03-08: the SHORT leg prints an absurd $800 on a ~$2 option.
        _bar_row(_SP, "2024-03-08", 800.0, 175.0),
        _bar_row(_LP, "2024-03-08", 0.5, 165.0),
    ]
    acct, ps, eng, ctx = _account(tmp_path, bar_rows, bars)
    _open_spread(acct, quantity=3)
    try:
        yield acct, ps
    finally:
        ctx.__exit__(None, None, None)


def test_an_outlier_premium_cannot_swing_the_mark_past_the_structures_risk(outlier_account):
    """A CREDIT combo is worth ``[-width x 100 x structures, 0]`` while its short leg is
    held. Unclamped this mark is about -$240,000 on a $10,000 account."""
    acct, ps = outlier_account
    ps.set_clock(datetime(2024, 3, 8))

    bound = WIDTH * 100.0 * 3
    mtm = acct._option_positions_mtm()
    assert -bound - 1e-6 <= mtm <= 0.0 + 1e-6
    assert mtm == pytest.approx(-bound)          # the outlier pins it AT the floor
    # ...and the unclamped leg-by-leg mark it replaced was two orders of magnitude worse.
    assert -800.0 * 100 * 3 < -bound

    eq = acct.equity()
    assert 0.0 < eq < CFG["starting_cash"] + 1e-6


def test_the_clamp_is_a_FLOOR_not_a_rewrite(tmp_path):
    """A normal bar must pass through untouched — a clamp that always returns the bound
    would hide every real move as well as the outliers."""
    bars = _bars(180, outlier_bar=True)
    bar_rows = [
        _bar_row(_SP, "2024-03-06", 2.0, 175.0),
        _bar_row(_LP, "2024-03-06", 0.5, 165.0),
        _bar_row(_SP, "2024-03-08", 1.2, 175.0),      # sane premiums: the spread is winning
        _bar_row(_LP, "2024-03-08", 0.3, 165.0),
    ]
    acct, ps, eng, ctx = _account(tmp_path, bar_rows, bars)
    try:
        _open_spread(acct, quantity=1)
        ps.set_clock(datetime(2024, 3, 8))
        # -1 x 1.2 x 100 + 1 x 0.3 x 100 = -90, comfortably inside [-1000, 0].
        assert acct._option_positions_mtm() == pytest.approx(-90.0)
    finally:
        ctx.__exit__(None, None, None)


# ==========================================================================
# expiry — as a UNIT
# ==========================================================================
def test_a_fully_breached_spread_settles_as_a_unit_at_its_max_loss(tmp_path):
    """Spot 150: BOTH puts ITM. Leg-by-leg this is "buy 100 shares at 175" against a
    separately-exercised 165 put; as a unit it is one bounded -$1,000 payoff, and the
    realised loss is exactly max loss = width x 100 - credit."""
    bars = _bars(150)
    bar_rows = [_bar_row(_SP, "2024-03-06", 2.0, 175.0),
                _bar_row(_LP, "2024-03-06", 0.5, 165.0)]
    acct, ps, eng, ctx = _account(tmp_path, bar_rows, bars)
    try:
        _open_spread(acct, quantity=1)
        assert acct._cash == pytest.approx(10_000.0 + CREDIT_PER_SHARE * 100, abs=1.0)

        ps.set_clock(datetime(2024, 3, 15))
        eng._apply_option_expiry(datetime(2024, 3, 15))

        # No shares were ever delivered, and no lot survives.
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
        assert acct.get_option_positions() == []
        assert acct._cash >= -1.0
        assert acct.equity() == pytest.approx(CFG["starting_cash"] - MAX_LOSS, abs=5.0)
        # ...and the loss can never exceed the structure's defined risk.
        assert acct.equity() >= CFG["starting_cash"] - MAX_LOSS - 1.0

        # It stays put on the next bar (no phantom re-settlement / stock marking).
        ps.set_clock(datetime(2024, 3, 18))
        assert acct.equity() == pytest.approx(CFG["starting_cash"] - MAX_LOSS, abs=5.0)
    finally:
        ctx.__exit__(None, None, None)


def test_a_spread_that_expires_above_the_short_strike_keeps_the_whole_credit(tmp_path):
    """The bullish thesis paying off: spot 180 > the 175 short strike, both legs expire
    worthless and the credit is the profit. Max gain for a credit spread is the credit."""
    bars = _bars(180)
    bar_rows = [_bar_row(_SP, "2024-03-06", 2.0, 175.0),
                _bar_row(_LP, "2024-03-06", 0.5, 165.0)]
    acct, ps, eng, ctx = _account(tmp_path, bar_rows, bars)
    try:
        _open_spread(acct, quantity=1)
        ps.set_clock(datetime(2024, 3, 15))
        eng._apply_option_expiry(datetime(2024, 3, 15))

        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
        assert acct.get_option_positions() == []
        assert acct.equity() == pytest.approx(
            CFG["starting_cash"] + CREDIT_PER_SHARE * 100, abs=5.0)
    finally:
        ctx.__exit__(None, None, None)


def test_a_spread_expiring_BETWEEN_the_strikes_leaves_no_naked_stock_leg(tmp_path):
    """The dangerous middle: the SHORT 175 put is assigned and the LONG 165 wing is not.
    Leg-by-leg that buys 100 shares at 175 and leaves them sitting in a backtest that
    never asked to own stock. As a unit it is a bounded -$500 payoff and no position."""
    bars = _bars(170)
    bar_rows = [_bar_row(_SP, "2024-03-06", 2.0, 175.0),
                _bar_row(_LP, "2024-03-06", 0.5, 165.0)]
    acct, ps, eng, ctx = _account(tmp_path, bar_rows, bars)
    try:
        _open_spread(acct, quantity=1)
        ps.set_clock(datetime(2024, 3, 15))
        eng._apply_option_expiry(datetime(2024, 3, 15))

        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == [], \
            "a unit-settled combo must never deliver shares"
        assert acct.get_option_positions() == []
        # short 175p intrinsic 5, long 165p 0 -> -500; plus the 150 credit = -350.
        assert acct.equity() == pytest.approx(CFG["starting_cash"] - 350.0, abs=5.0)
    finally:
        ctx.__exit__(None, None, None)


def test_the_expiry_payoff_is_bounded_even_when_the_cache_lies(tmp_path):
    """The safety clamp: three structures cannot realise more than 3 x width x 100."""
    bars = _bars(50)          # a catastrophic gap far below BOTH strikes
    bar_rows = [_bar_row(_SP, "2024-03-06", 2.0, 175.0),
                _bar_row(_LP, "2024-03-06", 0.5, 165.0)]
    acct, ps, eng, ctx = _account(tmp_path, bar_rows, bars)
    try:
        _open_spread(acct, quantity=3)
        ps.set_clock(datetime(2024, 3, 15))
        eng._apply_option_expiry(datetime(2024, 3, 15))
        # Even at spot 50 the spread is a $10-wide put vertical: -1000 x 3, not -125 x 300.
        assert acct.equity() == pytest.approx(
            CFG["starting_cash"] - 3 * MAX_LOSS, abs=10.0)
        assert acct._cash >= -1.0
    finally:
        ctx.__exit__(None, None, None)


def test_the_combo_is_recognised_as_defined_risk_by_the_leg_lookup(tmp_path):
    """``defined_risk_combo_strategy`` is what routes a leg to unit settlement at all;
    it answers None for anything outside the two defined-risk sets."""
    bars = _bars(150)
    bar_rows = [_bar_row(_SP, "2024-03-06", 2.0, 175.0),
                _bar_row(_LP, "2024-03-06", 0.5, 165.0)]
    acct, ps, eng, ctx = _account(tmp_path, bar_rows, bars)
    try:
        _open_spread(acct, quantity=1)
        held = acct.get_option_positions()
        assert held, "the spread must be open before this can be asked"
        for pos in held:
            assert acct.defined_risk_combo_strategy(pos) == "bull_put_spread"
    finally:
        ctx.__exit__(None, None, None)


def test_its_short_leg_is_not_liquidated_in_isolation_by_the_margin_path(tmp_path):
    """A defined-risk combo's short leg is COVERED by its long wing, so it carries no
    naked-margin requirement. Charged as naked, a $10-wide spread on a $10k account
    triggers a false margin call that breaks the combo and orphans the long put."""
    bars = _bars(150)
    bar_rows = [_bar_row(_SP, "2024-03-06", 2.0, 175.0),
                _bar_row(_LP, "2024-03-06", 0.5, 165.0)]
    acct, ps, eng, ctx = _account(tmp_path, bar_rows, bars)
    try:
        _open_spread(acct, quantity=1)
        covered = acct._defined_risk_contracts()
        assert _SP in covered and _LP in covered
    finally:
        ctx.__exit__(None, None, None)
