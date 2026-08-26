"""OPT-S13 — a long butterfly's defined-risk bound is the LOWER gap, not ``min(gaps)``.

``_defined_risk_width_per_structure`` returned ``min(k2-k1, k3-k2)`` for a
``call_butterfly``. A long 1-2-1 fly's maximum expiry payoff is the LOWER gap ``k2 - k1``
(realised at spot == k2, where the lower long is worth k2-k1 and both other legs are
worthless). Whenever the UPPER wing is narrower, ``min(gaps)`` is strictly below the
attainable payoff.

The clamp is not cosmetic. ``settle_defined_risk_combo_expiry`` applies
``net_payoff = max(-bound, min(net_payoff, bound))`` immediately before
``self._cash += net_payoff``, so a bound set too low DESTROYS simulated cash. The same
width also caps the mid-life mark in ``_open_positions_mtm``.

The truncating orientation is produced DETERMINISTICALLY, not by accident: the lower-wing
picker tie-breaks to the FARTHER strike (``TradeActions.OpenCallButterflyAction``) while
``select_wing`` tie-breaks to the NEARER one (``option_selector``), so the lower gap comes
out wider than the upper. On a $5 grid with a $100 body, ``wing_width=7.5%`` yields
90/100/105 — a $500 bound against a true $1,000 max payoff, a 50% truncation — and 12.5%
yields 85/100/110, a 33% truncation. Both widths are in the searched grid
(``ba2test_launcher.py``).

Every butterfly test in the suite used EQUAL wings, where ``min(gaps) == gaps[0]``, and the
one broken-wing assertion (``test_options_review_fixes.py``, ``[170, 180, 205]``) is the
MIRROR orientation — a wider upper wing — where ``min(gaps)`` is coincidentally correct. So
nothing caught it.

Widening the bound cannot let a payoff through that the structure could not really produce:
above k3 a long fly pays ``(k2-k1) - (k3-k2)``, whose magnitude never exceeds ``k2-k1``
unless the upper wing is more than twice the lower — and in THAT orientation the old and
new bounds are identical.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import OptionRight, OrderDirection


CFG = {
    "starting_cash": 50_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

# 90 / 100 / 105 — the shape wing_width=7.5% produces on a $5 grid with a $100 body.
# Lower gap 10 (the max payoff), upper gap 5 (what min(gaps) returned).
_LOW = "AAPL240315C00090000"
_BODY = "AAPL240315C00100000"
_HIGH = "AAPL240315C00105000"

_ENTRY_BAR = datetime(2024, 3, 5)
_FILL_BAR = datetime(2024, 3, 6)
_EXPIRY_BAR = datetime(2024, 3, 15)

# Spot pins to 100 at expiry — exactly the body strike, where the fly pays its maximum.
_AAPL_BARS = [
    {"Date": _ENTRY_BAR, "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000},
    {"Date": _FILL_BAR, "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000},
    {"Date": _EXPIRY_BAR, "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000},
]


# =========================================================================== #
# The width rule itself
# =========================================================================== #
def test_a_long_fly_is_bounded_by_its_LOWER_gap():
    from app.services.backtest.backtest_account import BacktestAccount

    w = BacktestAccount._defined_risk_width_per_structure

    # The truncating orientation: upper wing NARROWER than the lower.
    assert w("call_butterfly", [90, 100, 105]) == 10.0, (
        "a long 1-2-1 fly pays k2-k1 at the body; min(gaps) returns the upper wing (5) "
        "and truncates half of it (OPT-S13)"
    )
    assert w("call_butterfly", [85, 100, 110]) == 15.0   # the 12.5% grid width

    # Unchanged shapes: equal wings, and the mirror (wider upper wing) where min(gaps)
    # already agreed with the lower gap.
    assert w("call_butterfly", [170, 180, 190]) == 10.0
    assert w("call_butterfly", [170, 180, 205]) == 10.0

    # Nothing else moves.
    assert w("iron_condor", [90, 95, 105, 110]) == 5.0
    assert w("iron_condor", [90, 95, 105, 112]) == 7.0
    assert w("bull_call_spread", [100, 110]) == 10.0
    assert w("short_strangle", [90, 95, 105, 110]) == 10.0
    assert w("call_butterfly", [100]) is None


# =========================================================================== #
# ...and the cash it moves
# =========================================================================== #
@pytest.fixture
def broken_wing_fly(tmp_path):
    """A 1-2-1 call butterfly 90/100/105, ONE structure, held into expiry at spot 100."""
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from ba2_common.core.option_types import OptionLeg

    terms = {_LOW: 90.0, _BODY: 100.0, _HIGH: 105.0}
    cache_db = str(tmp_path / "bwfly.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows("AAPL", "2024-03-01", [
        {"occ_symbol": occ, "option_type": "call", "strike": k, "expiry": "2024-03-15",
         "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}
        for occ, k in terms.items()])
    # Entry-day premiums: 11.00 / 2.00 / 0.50 -> net debit 11.00 - 2*2.00 + 0.50 = 7.50.
    cache.write_bar_rows([
        {"occ_symbol": occ, "date": "2024-03-06", "open": px, "high": px, "low": px,
         "close": px, "volume": 500, "underlying": "AAPL", "option_type": "call",
         "strike": terms[occ], "expiry": "2024-03-15"}
        for occ, px in ((_LOW, 11.00), (_BODY, 2.00), (_HIGH, 0.50))])

    wire_backtest_seams()
    ctx = backtest_trading_db("bwfly")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(_ENTRY_BAR)
    acct = BacktestAccount(1, ps, CFG, options_provider=HistoricalOptionsProvider(cache_db))
    wire_backtest_seams().register_account(1, acct)

    def _leg(occ, side, ratio):
        return OptionLeg(contract_symbol=occ, side=side, ratio_qty=ratio,
                         position_intent=("buy_to_open" if side == OrderDirection.BUY
                                          else "sell_to_open"),
                         option_type=OptionRight.CALL, strike=terms[occ],
                         expiry=date(2024, 3, 15), underlying="AAPL")

    acct.submit_option_order(
        legs=[_leg(_LOW, OrderDirection.BUY, 1),
              _leg(_BODY, OrderDirection.SELL, 2),
              _leg(_HIGH, OrderDirection.BUY, 1)],
        quantity=1, order_type="market", option_strategy="call_butterfly")
    acct.refresh_orders()
    acct.refresh_transactions()
    assert len(acct.get_option_positions()) == 3

    engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
    engine.account = acct
    engine.price = ps
    engine.config = CFG
    try:
        yield engine, acct, ps
    finally:
        ctx.__exit__(None, None, None)


def test_the_bound_does_not_shrink_the_flys_own_max_payoff(broken_wing_fly):
    engine, acct, ps = broken_wing_fly
    txn = acct._option_transaction_for_contract(_BODY)
    positions = acct.get_option_positions()

    bound = acct._combo_expiry_bound(txn, positions, [90.0, 100.0, 105.0])
    assert bound == pytest.approx(10.0 * 100.0 * 1), (
        "the expiry clamp is set below the payoff the structure can actually reach"
    )


def test_the_expiry_clamp_does_not_destroy_the_flys_payoff(broken_wing_fly):
    """At spot 100 the fly pays its maximum: the 90 call is worth 10, the rest zero.

    That is $1,000 of REAL simulated cash. Under ``min(gaps)`` the clamp kept $500 of it.
    """
    engine, acct, ps = broken_wing_fly
    cash_before = acct._cash

    ps.set_clock(_EXPIRY_BAR)
    engine._apply_option_expiry(_EXPIRY_BAR)

    assert acct.get_option_positions() == []
    assert acct._cash - cash_before == pytest.approx(1_000.0), (
        "the expiry payoff was clamped to the narrower wing — this is cash, not a mark"
    )


def test_the_mid_life_mark_is_not_capped_below_the_flys_value(broken_wing_fly):
    """The same width caps the mid-life MTM. At spot 100 the book is worth $1,000."""
    engine, acct, ps = broken_wing_fly
    ps.set_clock(_EXPIRY_BAR)   # spot 100; the legs have no premium bar -> intrinsic marks

    assert acct._option_positions_mtm() == pytest.approx(1_000.0)
