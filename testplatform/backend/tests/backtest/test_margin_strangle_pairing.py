"""Maintenance margin prices a short strangle/straddle as a Reg-T PAIR, not a sum.

Operator decision 2026-08-31 (follow-up to review finding F10): the requirement for a
short strangle/straddle is::

    (the GREATER leg's naked margin) + (the OTHER leg's current premium x 100)

per paired contract — TRUE Reg-T — replacing the F10 per-leg SUM the maintenance loop
charged (``naked_margin_per_contract`` for EVERY held short lot). The maintenance model
pairs the two short legs through the order-derived grouping
(``_option_group_bounds``: parent order id + ``option_strategy``) and prices the pair
through the SAME shared formula the entry reserve uses
(``OptionsAccountInterface.short_pair_margin_per_contract``), so entry and maintenance
move together and a just-opened position still cannot instantly breach.

Hand arithmetic for the fixture (AAPL flat at 180, fraction 0.20, floor 0.10):
  * strangle put 170: OTM 10 -> max(0.20*180 - 10, 0.10*180) = max(26, 18) = 26 -> $2,600/ct
  * strangle call 210: OTM 30 -> max(36 - 30, 18) = 18                          -> $1,800/ct
  * marks on the check bar: put close 3.00, call close 1.00
  * pair = 2,600 (put, the greater) + 1.00*100 = $2,700/ct
    (the F10 sum was 2,600 + 1,800 = $4,400/ct — the mutation target)

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_margin_strangle_pairing.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import OptionRight, OrderDirection


CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

_STG_P = "AAPL240315P00170000"
_STG_C = "AAPL240315C00210000"
_STD_P = "AAPL240315P00180000"
_STD_C = "AAPL240315C00180000"

_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1100},
    {"Date": datetime(2024, 3, 8), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1200},
]


def _make_ps(clock):
    from app.services.backtest.price_source import AsOfPriceSource
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(clock)
    return ps


def _account(tmp_path, chain, bar_rows, tag):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache_db = str(tmp_path / "c.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows("AAPL", "2024-03-01", chain)
    cache.write_bar_rows(bar_rows)
    prov = HistoricalOptionsProvider(cache_db)
    wire_backtest_seams()
    ctx = backtest_trading_db(tag)
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_ps(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=prov)
    wire_backtest_seams().register_account(1, acct)
    return acct, ps, ctx


def _c(sym, k, bid, ask):
    return {"occ_symbol": sym, "option_type": "call", "strike": k, "expiry": "2024-03-15",
            "bid": bid, "ask": ask, "last": bid, "iv": 0.25}


def _p(sym, k, bid, ask):
    return {"occ_symbol": sym, "option_type": "put", "strike": k, "expiry": "2024-03-15",
            "bid": bid, "ask": ask, "last": bid, "iv": 0.25}


def _bar(sym, d, px, ot, k):
    return {"occ_symbol": sym, "date": d, "open": px, "high": px, "low": px, "close": px,
            "volume": 100, "underlying": "AAPL", "option_type": ot, "strike": k,
            "expiry": "2024-03-15"}


def _open_pair(tmp_path, tag, strategy, put_sym, put_k, put_px, call_sym, call_k, call_px, qty):
    """Open a short call + short put pair; bars are FLAT (open == close) on both the
    fill bar (03-06) and the check bar (03-08) so the marks are pinned by hand."""
    from ba2_common.core.option_types import OptionLeg
    chain = [_c(call_sym, call_k, call_px, call_px), _p(put_sym, put_k, put_px, put_px)]
    bars = []
    for d in ("2024-03-06", "2024-03-08"):
        bars.append(_bar(call_sym, d, call_px, "call", call_k))
        bars.append(_bar(put_sym, d, put_px, "put", put_k))
    acct, ps, ctx = _account(tmp_path, chain, bars, tag)
    call_leg = OptionLeg(contract_symbol=call_sym, side=OrderDirection.SELL,
                         position_intent="sell_to_open", option_type=OptionRight.CALL,
                         strike=call_k, expiry=date(2024, 3, 15), underlying="AAPL")
    put_leg = OptionLeg(contract_symbol=put_sym, side=OrderDirection.SELL,
                        position_intent="sell_to_open", option_type=OptionRight.PUT,
                        strike=put_k, expiry=date(2024, 3, 15), underlying="AAPL")
    acct.submit_option_order(legs=[call_leg, put_leg], quantity=qty, order_type="market",
                             option_strategy=strategy)
    acct.refresh_orders()
    acct.refresh_transactions()
    return acct, ps, ctx


def test_strangle_maintenance_is_the_reg_t_pair_not_the_sum(tmp_path):
    """2 strangles (put 170 / call 210 at spot 180, marks 3.00 / 1.00): the requirement
    is 2 x (2,600 + 100) = $5,400 — NOT the per-leg sum 2 x 4,400 = $8,800."""
    acct, ps, ctx = _open_pair(tmp_path, "mstgp1", "short_strangle",
                               _STG_P, 170.0, 3.0, _STG_C, 210.0, 1.0, qty=2)
    try:
        ps.set_clock(datetime(2024, 3, 8))
        req = acct.maintenance_margin_requirement()
        assert req == pytest.approx(2 * 2_700.0)
        assert req != pytest.approx(2 * 4_400.0)  # the F10 sum: the mutation target
    finally:
        ctx.__exit__(None, None, None)


def test_strangle_maintenance_moves_with_the_shared_pair_formula(tmp_path):
    """Lockstep pin (the re-pinned F10 invariant): maintenance equals
    ``short_pair_margin_per_contract`` on the SAME inputs the entry reserve prices —
    so at the fill bar (marks == fill premiums) maintenance equals the entry reserve
    exactly, and a just-opened strangle cannot instantly breach."""
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

    acct, ps, ctx = _open_pair(tmp_path, "mstgp2", "short_strangle",
                               _STG_P, 170.0, 3.0, _STG_C, 210.0, 1.0, qty=2)
    try:
        ps.set_clock(datetime(2024, 3, 6))  # the fill bar: marks == fill premiums
        pair = OptionsAccountInterface.short_pair_margin_per_contract(
            put_strike=170.0, call_strike=210.0, spot=180.0,
            put_premium=3.0, call_premium=1.0)
        entry_reserve = OptionsAccountInterface.option_reserve_required(
            "short_strangle", 2, strike=170.0, call_strike=210.0, spot=180.0,
            put_premium=3.0, call_premium=1.0)
        req = acct.maintenance_margin_requirement()
        assert req == pytest.approx(2 * pair)
        assert req == pytest.approx(entry_reserve)
    finally:
        ctx.__exit__(None, None, None)


def test_straddle_maintenance_is_the_reg_t_pair_not_the_sum(tmp_path):
    """3 straddles at the 180 strike (spot 180): both brackets tie at $3,600, so the
    conservative tie-break adds the LARGER premium (call 5.00): 3 x (3,600 + 500) =
    $12,300 — NOT the per-leg sum 3 x 7,200 = $21,600."""
    acct, ps, ctx = _open_pair(tmp_path, "mstgp3", "short_straddle",
                               _STD_P, 180.0, 4.0, _STD_C, 180.0, 5.0, qty=3)
    try:
        ps.set_clock(datetime(2024, 3, 8))
        req = acct.maintenance_margin_requirement()
        assert req == pytest.approx(3 * 4_100.0)
        assert req != pytest.approx(3 * 7_200.0)  # the F10 sum
    finally:
        ctx.__exit__(None, None, None)


def test_a_lone_surviving_leg_reverts_to_single_naked_margin(tmp_path):
    """Once one leg is gone (bought back / liquidated) the pair no longer exists: the
    surviving short call must be charged its own naked margin, 2 x $1,800 = $3,600 —
    neither the pair formula nor zero."""
    acct, ps, ctx = _open_pair(tmp_path, "mstgp4", "short_strangle",
                               _STG_P, 170.0, 3.0, _STG_C, 210.0, 1.0, qty=2)
    try:
        ps.set_clock(datetime(2024, 3, 8))
        # Remove the put lot the way a buyback leaves the book: the lot is gone, the
        # opening orders (and the strangle group) remain.
        del acct._option_positions[_STG_P]
        req = acct.maintenance_margin_requirement()
        assert req == pytest.approx(2 * 1_800.0)
    finally:
        ctx.__exit__(None, None, None)
