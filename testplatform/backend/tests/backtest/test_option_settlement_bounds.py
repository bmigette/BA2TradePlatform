"""Findings F1/F2 from docs/superpowers/specs/2026-08-30-option-program-review-findings.md.

  F1  Every NON-fill consumer of the sparse premium cache must clamp a bar close to the
      no-arbitrage bounds the arb guard (``_arb_fill_reject_reason``) already encodes —
      floor at intrinsic, cap at the upper bound (spot for a call, strike for a put):
        * long-ITM expiry settlement (``settle_single_leg_expiry``) — a $0.01 junk print
          against $50 of intrinsic must NOT be realised into cash;
        * the margin-liquidation buyback (``_liquidate_option_lot``) when a bar EXISTS
          (the no-bar branch already books max(intrinsic, entry));
        * the per-tick mark (``_option_positions_mtm``) when a bar exists but is junk.
  F2  The NO-bar mark fallback: non-defined-risk option lots (CSP, strangle, straddle,
      jade-lizard/ratio short legs) were marked at their ENTRY premium when the cache had
      no bar — a deep-ITM short liability frozen at its entry credit for weeks, the only
      found path around the dd>=100 wipeout sentinel. The intrinsic fallback (floor for
      shorts: liability = max(intrinsic, entry)) extends to ALL option lots, and the
      run-end ``get_round_trip_trades`` open_at_end rows use the same fallback mark.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_option_settlement_bounds.py -q
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

# Deep-ITM long call for the F1 expiry-settlement tests (strike 90, spot 100 -> 140).
_AAPL_CALL_90 = "AAPL240315C00090000"
# Naked short call for the F1 margin-liquidation junk-print tests (strike 500).
_AMD_CALL_500 = "AMD240315C00500000"
# Naked short put for the F2 mark tests (strike 80, spot 85 -> 40 -> 30).
_AMD_PUT_80 = "AMD240315P00080000"


def _make_ps(symbol, bars, clock):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(symbol, bars)
    ps.set_clock(clock)
    return ps


def _account(tmp_path, tag, ps, chain_underlying, chain, bar_rows, cfg=CFG):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache_db = str(tmp_path / "c.sqlite")
    cache = OptionsHistoryCache(cache_db)
    if chain:
        cache.write_chain_rows(chain_underlying, "2024-03-01", chain)
    if bar_rows:
        cache.write_bar_rows(bar_rows)
    prov = HistoricalOptionsProvider(cache_db)
    wire_backtest_seams()
    ctx = backtest_trading_db(tag)
    ctx.__enter__()
    seed_account_definition(1, cfg)
    acct = BacktestAccount(1, ps, cfg, options_provider=prov)
    wire_backtest_seams().register_account(1, acct)
    return acct, ctx


def _c(sym, k):
    return {"occ_symbol": sym, "option_type": "call", "strike": k, "expiry": "2024-03-15",
            "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _p(sym, k):
    return {"occ_symbol": sym, "option_type": "put", "strike": k, "expiry": "2024-03-15",
            "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _bar(sym, d, close, ot, k, underlying="AAPL", v=100):
    return {"occ_symbol": sym, "date": d, "open": close, "high": close, "low": close,
            "close": close, "volume": v, "underlying": underlying, "option_type": ot,
            "strike": k, "expiry": "2024-03-15"}


def _leg(sym, side, ot, k, ratio=1, underlying="AAPL"):
    from ba2_common.core.option_types import OptionLeg

    intent = "buy_to_open" if side == OrderDirection.BUY else "sell_to_open"
    return OptionLeg(contract_symbol=sym, side=side, ratio_qty=ratio, position_intent=intent,
                     option_type=ot, strike=k, expiry=date(2024, 3, 15), underlying=underlying)


def _ubar(d, px):
    return {"Date": d, "Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 100}


# ---------------------------------------------------------------------------
# F1 — long-ITM expiry settlement is clamped to [intrinsic, upper bound]
# ---------------------------------------------------------------------------
def _long_itm_call_account(tmp_path, tag, expiry_close):
    """Buy 1 AAPL 90 call @10.5 (spot 100). Spot 140 at expiry (intrinsic 50); the expiry
    bar close is ``expiry_close`` (None -> no expiry-day premium bar at all)."""
    bars = [
        _ubar(datetime(2024, 3, 5), 100),
        _ubar(datetime(2024, 3, 6), 100),
        _ubar(datetime(2024, 3, 8), 140),
        _ubar(datetime(2024, 3, 15), 140),
    ]
    ps = _make_ps("AAPL", bars, datetime(2024, 3, 5))
    chain = [_c(_AAPL_CALL_90, 90.0)]
    bar_rows = [_bar(_AAPL_CALL_90, "2024-03-06", 10.5, "call", 90.0)]
    if expiry_close is not None:
        bar_rows.append(_bar(_AAPL_CALL_90, "2024-03-15", expiry_close, "call", 90.0))
    acct, ctx = _account(tmp_path, tag, ps, "AAPL", chain, bar_rows)
    acct.submit_option_order(
        legs=[_leg(_AAPL_CALL_90, OrderDirection.BUY, OptionRight.CALL, 90.0)],
        quantity=1, order_type="market", option_strategy="long_call",
    )
    acct.refresh_orders()
    acct.refresh_transactions()
    assert acct._cash == pytest.approx(8_950.0, abs=1.0)  # 10,000 - 10.5 x 100 debit
    return acct, ps, ctx


def test_long_itm_expiry_junk_low_close_settles_at_intrinsic(tmp_path):
    """A $0.01 junk expiry print against $50 of intrinsic must be floored at intrinsic:
    the sell-to-close credits 50 x 100 = $5,000, not $1 (the arb guard's own documented
    junk-print class, realised directly by the old settlement path)."""
    acct, ps, ctx = _long_itm_call_account(tmp_path, "f1lo", expiry_close=0.01)
    try:
        ps.set_clock(datetime(2024, 3, 15))
        pos = acct.get_option_positions()[0]
        assert acct.settle_single_leg_expiry(pos, spot=140.0) is True
        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(8_950.0 + 5_000.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


def test_long_itm_expiry_junk_high_close_capped_at_spot(tmp_path):
    """The mirror junk print: a call premium ABOVE the spot (a call can never cost more
    than the stock) is capped at the upper bound — 140 x 100, not 500 x 100."""
    acct, ps, ctx = _long_itm_call_account(tmp_path, "f1hi", expiry_close=500.0)
    try:
        ps.set_clock(datetime(2024, 3, 15))
        pos = acct.get_option_positions()[0]
        assert acct.settle_single_leg_expiry(pos, spot=140.0) is True
        assert acct._cash == pytest.approx(8_950.0 + 14_000.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


def test_long_itm_expiry_sane_close_unchanged(tmp_path):
    """Regression: a SANE expiry print inside the bounds settles at the print, exactly as
    before (50.75 is above intrinsic 50 and below spot 140)."""
    acct, ps, ctx = _long_itm_call_account(tmp_path, "f1ok", expiry_close=50.75)
    try:
        ps.set_clock(datetime(2024, 3, 15))
        pos = acct.get_option_positions()[0]
        assert acct.settle_single_leg_expiry(pos, spot=140.0) is True
        assert acct._cash == pytest.approx(8_950.0 + 5_075.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# F1 twin — margin-liquidation buyback under a junk print is bounded
# ---------------------------------------------------------------------------
def _amd_short_call_account(tmp_path, tag, blowup_premium_close):
    """Short 3 naked AMD 500 calls @3.0, then AMD 520 on 2024-03-10 (intrinsic 20) with a
    junk premium bar ``blowup_premium_close`` on the blow-up day."""
    bars = [
        _ubar(datetime(2024, 3, 5), 450),
        _ubar(datetime(2024, 3, 6), 450),
        _ubar(datetime(2024, 3, 10), 520),
    ]
    ps = _make_ps("AMD", bars, datetime(2024, 3, 5))
    chain = [_c(_AMD_CALL_500, 500.0)]
    bar_rows = [
        _bar(_AMD_CALL_500, "2024-03-06", 3.0, "call", 500.0, underlying="AMD"),
        _bar(_AMD_CALL_500, "2024-03-10", blowup_premium_close, "call", 500.0, underlying="AMD"),
    ]
    acct, ctx = _account(tmp_path, tag, ps, "AMD", chain, bar_rows)
    acct.submit_option_order(
        legs=[_leg(_AMD_CALL_500, OrderDirection.SELL, OptionRight.CALL, 500.0, underlying="AMD")],
        quantity=3, order_type="market", option_strategy="naked_call",
    )
    acct.refresh_orders()
    acct.refresh_transactions()
    assert acct._cash == pytest.approx(10_900.0, abs=1.0)  # +3 x 3.0 x 100 credit
    return acct, ps, ctx


def test_liquidation_buyback_junk_low_print_floored_at_intrinsic(tmp_path):
    """The forced buyback of a short 500 call with AMD at 520 must cost at least the
    intrinsic 20/share even when the blow-up bar prints $0.01: 10,900 - 3 x 20 x 100 =
    4,900 (the raw print booked 10,897 — a free escape from the blow-up)."""
    acct, ps, ctx = _amd_short_call_account(tmp_path, "f1mlo", blowup_premium_close=0.01)
    try:
        ps.set_clock(datetime(2024, 3, 10))
        assert acct.maybe_margin_call_liquidation() is True
        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(4_900.0, abs=1.0)
        closes = [o for o in acct.get_orders() if o.comment == "option_expiry_close"]
        assert len(closes) == 1
        assert closes[0].open_price == pytest.approx(20.0)
    finally:
        ctx.__exit__(None, None, None)


def test_liquidation_buyback_junk_high_print_capped_at_spot(tmp_path):
    """The mirror bound: a buyback print of 900 against spot 520 (a call can never cost
    more than the stock) is capped at 520/share."""
    acct, ps, ctx = _amd_short_call_account(tmp_path, "f1mhi", blowup_premium_close=900.0)
    try:
        ps.set_clock(datetime(2024, 3, 10))
        assert acct.maybe_margin_call_liquidation() is True
        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(10_900.0 - 3 * 520.0 * 100.0, abs=1.0)
        closes = [o for o in acct.get_orders() if o.comment == "option_expiry_close"]
        assert len(closes) == 1
        assert closes[0].open_price == pytest.approx(520.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# F2 — no-bar marks: the intrinsic fallback extends to ALL option lots
# ---------------------------------------------------------------------------
def _naked_short_put_account(tmp_path, tag, extra_bar_rows=()):
    """Sell 1 naked AMD 80 put @2.0 (spot 85), then the spot dives 40 -> 30 with NO
    premium bars after the entry day (unless ``extra_bar_rows`` adds junk ones)."""
    bars = [
        _ubar(datetime(2024, 3, 5), 85),
        _ubar(datetime(2024, 3, 6), 85),
        _ubar(datetime(2024, 3, 7), 40),
        _ubar(datetime(2024, 3, 8), 30),
    ]
    ps = _make_ps("AMD", bars, datetime(2024, 3, 5))
    chain = [_p(_AMD_PUT_80, 80.0)]
    bar_rows = [_bar(_AMD_PUT_80, "2024-03-06", 2.0, "put", 80.0, underlying="AMD")]
    bar_rows.extend(extra_bar_rows)
    acct, ctx = _account(tmp_path, tag, ps, "AMD", chain, bar_rows)
    acct.submit_option_order(
        legs=[_leg(_AMD_PUT_80, OrderDirection.SELL, OptionRight.PUT, 80.0, underlying="AMD")],
        quantity=1, order_type="market", option_strategy="naked_put",
    )
    acct.refresh_orders()
    acct.refresh_transactions()
    assert acct._cash == pytest.approx(10_200.0, abs=1.0)  # +2.0 x 100 credit
    return acct, ps, ctx


def test_naked_short_put_no_bar_marks_intrinsic_floored_liability(tmp_path):
    """A deep-ITM naked short put with NO premium bar must be marked at its intrinsic
    liability (max(intrinsic, entry) x -100), not frozen at the $200 entry credit: the
    old flat-equity-at-entry-credit behaviour is the only found path around the dd>=100
    wipeout sentinel and must be dead."""
    acct, ps, ctx = _naked_short_put_account(tmp_path, "f2mark")
    try:
        ps.set_clock(datetime(2024, 3, 7))          # spot 40, intrinsic 40, no premium bar
        assert acct._option_positions_mtm() == pytest.approx(-4_000.0, abs=1.0)
        assert acct.equity() == pytest.approx(10_200.0 - 4_000.0, abs=1.0)
        assert acct.equity() != pytest.approx(10_000.0, abs=1.0)  # old behaviour: -entry credit

        ps.set_clock(datetime(2024, 3, 8))          # spot 30, intrinsic 50 — drawdown GROWS
        assert acct._option_positions_mtm() == pytest.approx(-5_000.0, abs=1.0)
        assert acct.equity() == pytest.approx(10_200.0 - 5_000.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


def test_naked_short_put_no_bar_otm_keeps_entry_credit_mark(tmp_path):
    """While the short put is still OTM (spot 85 > strike 80, intrinsic 0) a missing bar
    keeps the entry-credit mark — the intrinsic is a FLOOR on the liability, not a
    replacement (an OTM short marked at 0 would overstate equity)."""
    acct, ps, ctx = _naked_short_put_account(tmp_path, "f2otm")
    try:
        ps.set_clock(datetime(2024, 3, 6, 23, 59))  # after entry, spot still 85, no 3/6.5 bar
        assert acct._option_positions_mtm() == pytest.approx(-200.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


def test_long_call_no_bar_marks_intrinsic_floor(tmp_path):
    """The fallback extends to LONG non-defined-risk lots too: a deep-ITM long call with
    no premium bar marks at max(intrinsic, entry) = 50, not the 10.5 entry premium."""
    acct, ps, ctx = _long_itm_call_account(tmp_path, "f2long", expiry_close=None)
    try:
        ps.set_clock(datetime(2024, 3, 8))          # spot 140, no premium bar this day
        assert acct._option_positions_mtm() == pytest.approx(5_000.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# F1 at the per-tick mark — a bar EXISTS but is junk: clamp to [intrinsic, upper]
# ---------------------------------------------------------------------------
def test_mark_with_junk_low_bar_clamped_to_intrinsic(tmp_path):
    """Deep-ITM short put (spot 40, strike 80): a $0.05 premium print marks the liability
    at intrinsic 40 (-$4,000), not at the junk print (-$5)."""
    acct, ps, ctx = _naked_short_put_account(
        tmp_path, "f1jlo",
        extra_bar_rows=[_bar(_AMD_PUT_80, "2024-03-07", 0.05, "put", 80.0, underlying="AMD")],
    )
    try:
        ps.set_clock(datetime(2024, 3, 7))
        assert acct._option_positions_mtm() == pytest.approx(-4_000.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


def test_mark_with_junk_high_bar_capped_at_strike(tmp_path):
    """A put can never be worth more than its strike: a 150 premium print against strike
    80 marks at -$8,000, not -$15,000."""
    acct, ps, ctx = _naked_short_put_account(
        tmp_path, "f1jhi",
        extra_bar_rows=[_bar(_AMD_PUT_80, "2024-03-08", 150.0, "put", 80.0, underlying="AMD")],
    )
    try:
        ps.set_clock(datetime(2024, 3, 8))          # spot 30, strike 80 -> upper bound 80
        assert acct._option_positions_mtm() == pytest.approx(-8_000.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


def test_mark_with_sane_bar_unchanged(tmp_path):
    """Regression: a sane in-bounds print (45, intrinsic 40, strike 80) marks at the
    print exactly as before."""
    acct, ps, ctx = _naked_short_put_account(
        tmp_path, "f1jok",
        extra_bar_rows=[_bar(_AMD_PUT_80, "2024-03-07", 45.0, "put", 80.0, underlying="AMD")],
    )
    try:
        ps.set_clock(datetime(2024, 3, 7))
        assert acct._option_positions_mtm() == pytest.approx(-4_500.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# F2 twin — run-end open_at_end trade rows use the same fallback mark
# ---------------------------------------------------------------------------
def test_open_at_end_trade_row_uses_intrinsic_fallback(tmp_path):
    """A run ending with the deep-ITM naked short put still open (no premium bar) must
    mark the open_at_end row at the intrinsic-floored premium 50, not at the 2.0 entry
    premium (which booked the unrealised blow-up as flat)."""
    acct, ps, ctx = _naked_short_put_account(tmp_path, "f2rows")
    try:
        ps.set_clock(datetime(2024, 3, 8))          # spot 30, intrinsic 50
        rows = [t for t in acct.get_round_trip_trades() if t["exit_reason"] == "open_at_end"]
        assert len(rows) == 1
        row = rows[0]
        assert row["exit_price"] == pytest.approx(50.0)
        assert row["pnl"] == pytest.approx((2.0 - 50.0) * 100.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)
