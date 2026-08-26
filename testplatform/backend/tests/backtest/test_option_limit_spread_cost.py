"""Single-leg option LIMIT orders must cross the modeled bid-ask spread too (2026-08-24).

THE DEFECT THESE PIN
--------------------
``_option_fill_price`` applied ``_option_slip`` (execution slippage + the modeled half
bid-ask spread) only on its ``else`` branch — the branch a multi-leg CHILD takes, because
children carry no ``limit_price`` (the parent holds the combo's net limit). But EVERY
single-leg option order the platform builds carries a limit price (``TradeActions`` and
``PremiumSeller`` submit ``order_type="limit"``), so the entire wheel / 0DTE / long-option
branch filled at the raw bar premium and paid the spread on NEITHER end, while multi-leg
credit structures paid ~5% of premium per leg per side at the grid's default
``--option-spread-pct 5.0``. That is a systematic tilt in favour of exactly the single-leg
premium sellers the options grid exists to evaluate.

THE FIX THESE PIN
-----------------
A limit fill crosses the quote FIRST and is THEN re-tested against its limit:
  * BUY_LIMIT  -> lift the ASK, ``px + half``; fills only if ``px + half <= limit``.
  * SELL_LIMIT -> hit the BID,  ``px - half``; fills only if ``px - half >= limit``.
An order that no longer clears once the spread is crossed does NOT fill — it stays pending
and retries the next bar, exactly like a non-crossing limit always has.

Relationship to the EQUITY limit path (``_limit_trigger_price`` / ``_evaluate_fill``): the
same economics in a different shape, because an equity bar has a full [low, high] range
while an option "bar" contributes ONE reference premium. Equity widens the TRIGGER
threshold by the half-spread and fills at the limit; options cross the single reference
price and re-test against the limit. Both make a marginal limit HARDER to fill and neither
can produce a fill better than before the spread existed. Execution ``slippage_bps`` is
charged on NEITHER limit path — on both asset classes it is a market/stop cost (see
``_slip`` / ``_option_slip``); a limit's cost is having to cross the quote at all.
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.services.backtest.backtest_account import (
    BacktestAccount, _OPTION_SPREAD_THIN_MULT,
)
from ba2_common.core.types import (
    OptionRight, OrderDirection, OrderStatus, OrderType,
)

_OCC = "AAPL240315C00180000"
_FILL_DAY = date(2024, 3, 6)

# One reference premium and a liquid bar (volume >= _OPTION_SPREAD_LIQUID_VOLUME=100 so
# there is no thin-widening, and >= 10x the 1-contract order so the participation cap is
# not what rejects the fill).
_PX = 4.00
_VOL = 500.0
# 5% of a $4.00 premium = $0.20 full width -> $0.10 charged per fill, in the adverse
# direction. Spot 181 / strike 180 keeps every premium here arbitrage-consistent.
_HALF = 0.10
_SPOT = 181.0


# --------------------------------------------------------------------------- #
# Unit harness: _option_fill_price against a stub premium bar + underlying bar.
# --------------------------------------------------------------------------- #
class _StubOptions:
    """Minimal options provider: one premium bar for one contract."""

    def __init__(self, px=_PX, volume=_VOL):
        self._bar = {"open": px, "high": px, "low": px, "close": px, "volume": volume,
                     "strike": 180.0, "option_type": "call"}

    def get_bar(self, occ_symbol, day):
        return dict(self._bar)


class _StubPrice:
    """Minimal price source: a fixed next bar date and one underlying bar."""

    def __init__(self, spot=_SPOT):
        self._spot = spot

    def next_bar_date(self, symbol, as_of):
        return _FILL_DAY

    def bar_at(self, symbol, day):
        return {"open": self._spot, "high": self._spot, "low": self._spot,
                "close": self._spot}


def _acct(px=_PX, volume=_VOL, spot=_SPOT, **cfg):
    base = {"starting_cash": 100_000.0, "commission_per_trade": 0.0, "slippage_bps": 0.0,
            "fill_model": "next_bar_open"}
    base.update(cfg)
    a = BacktestAccount(id=1, price_source=_StubPrice(spot=spot), settings=base)
    a._options = _StubOptions(px=px, volume=volume)
    return a


def _order(order_type, side, limit=None, intent="buy_to_open", qty=1.0):
    return SimpleNamespace(
        id=1, symbol="AAPL", underlying_symbol="AAPL", contract_symbol=_OCC,
        order_type=order_type, limit_price=limit, side=side, quantity=qty,
        multiplier=100, strike=180.0, option_type=OptionRight.CALL,
        position_intent=intent, parent_order_id=None,
    )


_AS_OF = datetime(2024, 3, 5)


# =========================================================================== #
# 1. a single-leg long call BUY entry fills ABOVE the raw bar price
# =========================================================================== #
def test_single_leg_buy_limit_pays_the_ask_not_the_raw_bar_price():
    """The defect: this used to fill at exactly 4.00 — the spread cost nothing."""
    a = _acct(option_spread_pct=5.0)
    px = a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=10.0), _AS_OF)
    assert px == pytest.approx(_PX + _HALF)   # 4.10
    assert px > _PX                            # the direction, stated on its own


# =========================================================================== #
# 2. a single-leg SELL TO OPEN fills BELOW the raw bar price
# =========================================================================== #
def test_single_leg_sell_limit_receives_the_bid_not_the_raw_bar_price():
    """A short premium seller receives the BID. This used to credit the full 4.00."""
    a = _acct(option_spread_pct=5.0)
    px = a._option_fill_price(
        _order(OrderType.SELL_LIMIT, OrderDirection.SELL, limit=0.50,
               intent="sell_to_open"), _AS_OF)
    assert px == pytest.approx(_PX - _HALF)   # 3.90
    assert px < _PX


def test_the_two_sides_straddle_the_bar_price_by_the_full_spread():
    """Sign guard: a buy and a sell on the SAME bar must differ by the full spread, with
    the buy on the high side. An inverted crossing direction flatters both ends."""
    a = _acct(option_spread_pct=5.0)
    buy = a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=10.0), _AS_OF)
    sell = a._option_fill_price(
        _order(OrderType.SELL_LIMIT, OrderDirection.SELL, limit=0.50,
               intent="sell_to_open"), _AS_OF)
    assert buy - sell == pytest.approx(2 * _HALF)
    assert sell < _PX < buy


# =========================================================================== #
# 3. a limit that no longer clears once the spread is crossed does NOT fill
# =========================================================================== #
def test_buy_limit_that_the_ask_no_longer_clears_does_not_fill():
    """Limit 4.05: the raw bar price 4.00 crosses it, the ASK 4.10 does not. Filling here
    is the flattering outcome — the order must stay pending instead."""
    a = _acct(option_spread_pct=5.0)
    assert a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=4.05), _AS_OF) is None


def test_sell_limit_that_the_bid_no_longer_clears_does_not_fill():
    """Mirror: limit 3.95 is crossed by the raw 4.00 but not by the BID 3.90."""
    a = _acct(option_spread_pct=5.0)
    assert a._option_fill_price(
        _order(OrderType.SELL_LIMIT, OrderDirection.SELL, limit=3.95,
               intent="sell_to_open"), _AS_OF) is None


def test_limit_exactly_at_the_crossed_price_still_fills():
    """The re-test is a CROSS test, not a strict inequality: an ask exactly at the limit
    fills (and at the crossed price, never worse than the limit)."""
    a = _acct(option_spread_pct=5.0)
    assert a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=4.10), _AS_OF
    ) == pytest.approx(4.10)
    assert a._option_fill_price(
        _order(OrderType.SELL_LIMIT, OrderDirection.SELL, limit=3.90,
               intent="sell_to_open"), _AS_OF
    ) == pytest.approx(3.90)


def test_a_limit_the_raw_price_never_crossed_still_does_not_fill():
    """The pre-existing non-crossing behaviour is untouched (the fix can only ever make a
    limit harder to fill, never easier)."""
    a = _acct(option_spread_pct=5.0)
    assert a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=3.00), _AS_OF) is None
    assert a._option_fill_price(
        _order(OrderType.SELL_LIMIT, OrderDirection.SELL, limit=6.00,
               intent="sell_to_open"), _AS_OF) is None


# =========================================================================== #
# 4. multi-leg CHILDREN pay exactly what they paid before (no double-charge)
# =========================================================================== #
def test_multi_leg_child_with_no_limit_price_is_charged_exactly_once():
    """A child leg is LIMIT-typed but carries NO limit_price (the parent holds the net
    limit), so it keeps taking the market-style ``_option_slip`` branch. Values pinned
    from the PRE-FIX engine: buy 4.10, sell 3.90 — charged once, not twice."""
    a = _acct(option_spread_pct=5.0)
    buy_child = _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=None)
    buy_child.parent_order_id = 99
    sell_child = _order(OrderType.SELL_LIMIT, OrderDirection.SELL, limit=None,
                        intent="sell_to_open")
    sell_child.parent_order_id = 99
    assert a._option_fill_price(buy_child, _AS_OF) == pytest.approx(4.10)
    assert a._option_fill_price(sell_child, _AS_OF) == pytest.approx(3.90)
    # ...and identical to calling the (untouched) slip helper directly.
    bar = a._options.get_bar(_OCC, _FILL_DAY)
    assert a._option_fill_price(buy_child, _AS_OF) == a._option_slip(_PX, True, bar)
    assert a._option_fill_price(sell_child, _AS_OF) == a._option_slip(_PX, False, bar)


def test_multi_leg_child_still_pays_slippage_bps_on_top():
    """The market-style branch is byte-unchanged, slippage included: 4.00*1.01 + 0.10."""
    a = _acct(option_spread_pct=5.0, slippage_bps=100.0)
    child = _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=None)
    child.parent_order_id = 99
    assert a._option_fill_price(child, _AS_OF) == pytest.approx(4.00 * 1.01 + 0.10)


def test_a_market_single_leg_is_unchanged_too():
    """A MARKET option order has no limit and always took the slip branch — untouched."""
    a = _acct(option_spread_pct=5.0)
    assert a._option_fill_price(
        _order(OrderType.MARKET, OrderDirection.BUY, limit=None), _AS_OF
    ) == pytest.approx(4.10)


# =========================================================================== #
# 5. EQUITY fills are byte-identical
# =========================================================================== #
def _eq_acct(**cfg):
    base = {"starting_cash": 100_000.0, "commission_per_trade": 0.0, "slippage_bps": 12.5,
            "fill_model": "next_bar_open", "spread_bps": 20.0}
    base.update(cfg)
    return BacktestAccount(id=1, price_source=SimpleNamespace(), settings=base)


_EQ_BAR = {"open": 100.0, "high": 104.0, "low": 96.0, "close": 101.0}


def _equity_fills(a):
    """Every equity price the fill engine can produce, as raw floats."""
    return (
        a._slip(100.0, True), a._slip(100.0, False),
        a._limit_trigger_price(100.0, is_sell=True),
        a._limit_trigger_price(100.0, is_sell=False),
        a._gap_stop_fill(100.0, _EQ_BAR, is_sell=True),
        a._gap_stop_fill(100.0, _EQ_BAR, is_sell=False),
        a._gap_limit_fill(100.0, _EQ_BAR, is_sell=True),
        a._gap_limit_fill(100.0, _EQ_BAR, is_sell=False),
    )


def test_equity_fill_prices_are_bit_identical_with_and_without_the_option_spread():
    """The option spread model must not leak into the equity path. Compared as raw bytes,
    not approx: an equity result may not move by one ULP."""
    import struct

    plain = _equity_fills(_eq_acct())
    with_spread = _equity_fills(_eq_acct(option_spread_pct=5.0,
                                         option_spread_min_tick=0.02))
    assert [struct.pack("<d", x) for x in plain] == \
           [struct.pack("<d", x) for x in with_spread]


def test_equity_fill_prices_are_pinned_to_their_exact_values():
    """Absolute pin, so a future change to the equity path cannot pass merely by moving
    both sides of the comparison above."""
    a = _eq_acct()
    # slippage 12.5bps + half of 20bps spread = 22.5bps
    assert a._slip(100.0, True) == 100.0 * (1.0 + 0.00225)
    assert a._slip(100.0, False) == 100.0 * (1.0 - 0.00225)
    # limit TRIGGER widened by half the spread (10bps); the fill stays at the limit
    assert a._limit_trigger_price(100.0, is_sell=True) == 100.0 * 1.001
    assert a._limit_trigger_price(100.0, is_sell=False) == 100.0 * 0.999
    assert a._gap_stop_fill(100.0, _EQ_BAR, is_sell=True) == 100.0
    assert a._gap_limit_fill(100.0, _EQ_BAR, is_sell=True) == 100.0


# =========================================================================== #
# 6. option_spread_pct 0 charges nothing on EITHER path
# =========================================================================== #
def test_spread_pct_zero_is_an_exact_no_op_on_both_branches():
    """``--option-spread-pct 0`` must reproduce the raw bar premium on the limit branch and
    the market branch alike (pre-2026-07-25 fills, exactly)."""
    a = _acct(option_spread_pct=0.0, option_spread_min_tick=0.0)
    limit_fill = a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=10.0), _AS_OF)
    child = _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=None)
    child.parent_order_id = 99
    market_fill = a._option_fill_price(child, _AS_OF)
    assert limit_fill == _PX          # exact, not approx
    assert market_fill == _PX
    # ...and a limit the raw price crosses by a hair still fills, since nothing widened it.
    assert a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=4.00), _AS_OF) == _PX


def test_unset_spread_settings_are_an_exact_no_op_on_the_limit_branch():
    """The setting is optional; an old config that never set it must be untouched."""
    a = _acct()
    assert a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=10.0), _AS_OF) == _PX


def test_the_limit_branch_does_not_charge_execution_slippage():
    """Divergence pin: like the equity limit path, a limit fill pays the SPREAD (crossing
    the quote) but not ``slippage_bps`` (a market/stop cost). With the spread off, a limit
    fill is the raw bar premium even at an absurd slippage setting."""
    a = _acct(option_spread_pct=0.0, slippage_bps=100.0)
    assert a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=10.0), _AS_OF) == _PX


def test_a_thin_contract_is_charged_the_widened_spread_on_the_limit_branch():
    """The whole ``_option_half_spread`` model reaches the limit branch, thin-volume
    widening included — and it matters most there, because the far-OTM wheel strikes and
    0DTE contracts the single-leg branch trades are exactly the thin ones. (Volume 50 is
    below the liquid threshold of 100 but still absorbs a 1-contract order at the 10%
    participation cap, so it is the SPREAD model deciding here, not the liquidity guard.)"""
    a = _acct(volume=50.0, option_spread_pct=5.0)
    assert a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=10.0), _AS_OF
    ) == pytest.approx(_PX + _HALF * _OPTION_SPREAD_THIN_MULT)      # 4.20, not 4.10
    assert a._option_fill_price(
        _order(OrderType.SELL_LIMIT, OrderDirection.SELL, limit=0.50,
               intent="sell_to_open"), _AS_OF
    ) == pytest.approx(_PX - _HALF * _OPTION_SPREAD_THIN_MULT)      # 3.80, not 3.90


def test_the_min_tick_floor_applies_on_the_limit_branch_too():
    """Percent-of-premium alone under-charges cheap contracts, which is where fabricated
    edge concentrates. 5% of a $0.12 premium is a $0.003 half; the $0.02 floor gives $0.01.
    (Spot 170 keeps the 180 call OTM so the no-arbitrage guard is not what decides.)"""
    a = _acct(px=0.12, spot=170.0, option_spread_pct=5.0, option_spread_min_tick=0.02)
    assert a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=10.0), _AS_OF
    ) == pytest.approx(0.13)                                         # 0.12 + 0.01


def test_a_sell_fill_is_still_floored_at_zero():
    """A modeled spread wider than the premium must not pay the account to sell. (Spot 170
    keeps the 180 call OTM so the no-arbitrage guard — intrinsic 0 — is not what decides.)"""
    a = _acct(px=0.10, spot=170.0, option_spread_pct=300.0, option_spread_min_tick=5.0)
    px = a._option_fill_price(
        _order(OrderType.SELL_LIMIT, OrderDirection.SELL, limit=-1.0,
               intent="sell_to_open"), _AS_OF)
    assert px == 0.0


def test_the_crossed_price_is_what_the_no_arbitrage_guard_checks():
    """Ordering pin, unchanged from the market branch: the guard validates the price that
    would actually be booked, so a BID pushed below intrinsic is rejected (order stays
    pending) rather than filled at a premium no one would have paid. Spot 185 / strike 180
    -> intrinsic 5.00; a 5.00 print sells at 4.875 after a 5% spread, more than the 0.05
    tolerance below intrinsic."""
    a = _acct(px=5.00, spot=185.0, option_spread_pct=5.0)
    assert a._option_fill_price(
        _order(OrderType.SELL_LIMIT, OrderDirection.SELL, limit=0.50,
               intent="sell_to_open"), _AS_OF) is None
    # The BUY side crosses the other way and is comfortably above intrinsic -> still fills.
    assert a._option_fill_price(
        _order(OrderType.BUY_LIMIT, OrderDirection.BUY, limit=10.0), _AS_OF
    ) == pytest.approx(5.125)


# =========================================================================== #
# 7. a round trip pays the spread TWICE — end to end, through cash
# =========================================================================== #
CFG_E2E = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,   # isolate the spread in the cash delta
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
    "option_spread_pct": 5.0,
    "option_spread_min_tick": 0.0,
}

_OCC_SHORT = "AAPL240315C00190000"

_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 180, "High": 182, "Low": 178, "Close": 181,
     "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 181, "High": 184, "Low": 180, "Close": 183,
     "Volume": 1100},
    {"Date": datetime(2024, 3, 7), "Open": 181, "High": 184, "Low": 180, "Close": 183,
     "Volume": 1200},
]


def _bar_row(occ, px, opt_type, strike, day, volume=500):
    return {"occ_symbol": occ, "date": day, "open": px, "high": px, "low": px,
            "close": px, "volume": volume, "underlying": "AAPL",
            "option_type": opt_type, "strike": strike, "expiry": "2024-03-15"}


@pytest.fixture
def e2e_account(tmp_path):
    """A real BacktestAccount over a seeded temp options cache (no network, no real DB).

    The 180 call prints a flat 4.00 premium on both 2024-03-06 and 2024-03-07 so the round
    trip's cash delta is PURELY the modeled spread — the contract itself did not move.
    """
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import (
        HistoricalOptionsProvider, clear_worker_options_cache,
    )
    from app.services.backtest.price_source import AsOfPriceSource

    cache_db = str(tmp_path / "options_cache.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows("AAPL", "2024-03-01", [
        {"occ_symbol": _OCC, "option_type": "call", "strike": 180.0,
         "expiry": "2024-03-15", "bid": 3.9, "ask": 4.1, "last": 4.0, "iv": 0.25},
        {"occ_symbol": _OCC_SHORT, "option_type": "call", "strike": 190.0,
         "expiry": "2024-03-15", "bid": 1.4, "ask": 1.6, "last": 1.5, "iv": 0.22},
    ])
    cache.write_bar_rows([
        _bar_row(_OCC, 4.0, "call", 180.0, "2024-03-06"),
        _bar_row(_OCC, 4.0, "call", 180.0, "2024-03-07"),
        _bar_row(_OCC_SHORT, 1.5, "call", 190.0, "2024-03-06", volume=300),
    ])
    clear_worker_options_cache()

    wire_backtest_seams()
    ctx = backtest_trading_db("optlimitspread")
    ctx.__enter__()
    seed_account_definition(1, CFG_E2E)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG_E2E,
                           options_provider=HistoricalOptionsProvider(cache_db))
    wire_backtest_seams().register_account(1, acct)
    try:
        yield acct, ps
    finally:
        ctx.__exit__(None, None, None)
        clear_worker_options_cache()


def _single_leg(acct, side, intent, limit, strategy):
    from ba2_common.core.option_types import OptionLeg

    leg = OptionLeg(contract_symbol=_OCC, side=side, position_intent=intent,
                    option_type=OptionRight.CALL, strike=180.0,
                    expiry=date(2024, 3, 15), underlying="AAPL")
    return acct.submit_option_order(legs=[leg], quantity=1, order_type="limit",
                                    limit_price=limit, option_strategy=strategy)


def test_single_leg_round_trip_pays_the_full_spread_twice(e2e_account):
    """Sell to open then buy to close the SAME contract at the SAME 4.00 premium.

    A frictionless engine returns the account to its starting cash. With the spread
    modeled, the open credits the BID (3.90) and the close pays the ASK (4.10): a $0.20
    per-share round-trip cost = $20 per contract = the FULL modeled spread, paid once on
    each end. This is precisely what the single-leg branch has been escaping.
    """
    acct, ps = e2e_account
    cash_before = acct.get_balance()

    # sell to open, limit well below the market so it crosses
    _single_leg(acct, OrderDirection.SELL, "sell_to_open", 0.50, "naked_call")
    acct.refresh_orders()          # fill bar 2024-03-06, premium 4.00 -> credit 3.90
    assert acct.get_balance() == pytest.approx(cash_before + 3.90 * 100)

    # buy to close, limit well above the market so it crosses
    ps.set_clock(datetime(2024, 3, 6))
    _single_leg(acct, OrderDirection.BUY, "buy_to_close", 20.0, "close")
    acct.refresh_orders()          # fill bar 2024-03-07, premium 4.00 -> debit 4.10

    round_trip_cost = cash_before - acct.get_balance()
    assert round_trip_cost == pytest.approx(0.20 * 100)      # full spread, twice a half
    assert round_trip_cost == pytest.approx(
        2 * acct._option_half_spread(4.0, {"volume": 500.0}) * 100)
    assert acct.get_option_positions() == []                  # flat again


def test_multi_leg_round_trip_cost_is_unchanged_by_this_fix(e2e_account):
    """PIN (must hold before AND after): a two-leg combo's legs are priced through the
    untouched market-style branch, so the bull call spread's net debit stays 2.675 —
    long 180c at 4.00+0.10 = 4.10, short 190c at 1.50-0.0375 = 1.4625.
    """
    from ba2_common.core.option_types import OptionLeg

    acct, ps = e2e_account
    cash_before = acct.get_balance()
    legs = [
        OptionLeg(contract_symbol=_OCC, side=OrderDirection.BUY,
                  position_intent="buy_to_open", option_type=OptionRight.CALL,
                  strike=180.0, expiry=date(2024, 3, 15), underlying="AAPL"),
        OptionLeg(contract_symbol=_OCC_SHORT, side=OrderDirection.SELL,
                  position_intent="sell_to_open", option_type=OptionRight.CALL,
                  strike=190.0, expiry=date(2024, 3, 15), underlying="AAPL"),
    ]
    parent = acct.submit_option_order(legs=legs, quantity=1, order_type="limit",
                                      # 2.6375 is the net these legs actually reach once each
                                      # crosses its own spread; since OPT-S7 the engine will
                                      # not fill a combo through a tighter net limit, and the
                                      # subject here is the per-leg COST, not the limit.
                                      limit_price=2.6375,
                                      option_strategy="bull_call_spread")
    acct.refresh_orders()

    filled = acct.get_order(parent.id)
    assert filled.status == OrderStatus.FILLED
    assert filled.open_price == pytest.approx(4.10 - 1.4625)   # 2.6375
    assert acct.get_balance() == pytest.approx(
        cash_before - 4.10 * 100 + 1.4625 * 100)


def test_single_leg_limit_entry_pays_the_ask_end_to_end(e2e_account):
    """The buy side, through the real engine and real cash: 4.10 debited, not 4.00."""
    acct, ps = e2e_account
    cash_before = acct.get_balance()
    order = _single_leg(acct, OrderDirection.BUY, "buy_to_open", 20.0, "long_call")
    acct.refresh_orders()

    filled = acct.get_order(order.id)
    assert filled.status == OrderStatus.FILLED
    assert filled.open_price == pytest.approx(4.10)
    assert acct.get_balance() == pytest.approx(cash_before - 4.10 * 100)


def test_single_leg_limit_entry_that_the_ask_misses_stays_pending_end_to_end(e2e_account):
    """Limit 4.05 on a 4.00 bar: the raw price crossed it, the ask does not. No fill, no
    position, no cash movement — the order retries the next bar."""
    acct, ps = e2e_account
    cash_before = acct.get_balance()
    order = _single_leg(acct, OrderDirection.BUY, "buy_to_open", 4.05, "long_call")
    acct.refresh_orders()

    assert acct.get_order(order.id).status == OrderStatus.ACCEPTED
    assert acct.get_option_positions() == []
    assert acct.get_balance() == pytest.approx(cash_before)
