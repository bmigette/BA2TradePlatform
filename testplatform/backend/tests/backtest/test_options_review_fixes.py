"""Review fixes F1-F5 from reports/options_backtest_review_2026-07-01.md.

  F1  strategy-aware defined-risk widths: an iron condor's bound is the wider WING
      (max(k2-k1, k4-k3)), not the widest adjacent gap (usually the body); a
      broken-wing butterfly binds on its NARROWER wing (min gap).
  F2  a short CALL fully covered by long underlying shares carries ~zero classic
      maintenance — exempt from the requirement AND the liquidation candidates.
  F3  the combo expiry safety bound scales by the PARENT order's structure count
      (max(leg qty) counted a 1-2-1 fly's body as 2x structures) and the legs'
      multiplier (was hardcoded 100).
  F4  a forced STOCK liquidation persists a synthetic FILLED closing order.
  F5  an option-lot liquidation with no premium bar books max(intrinsic, entry),
      never break-even at the very moment of a margin blow-up.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_options_review_fixes.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus


CFG = {
    "starting_cash": 10_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

# Asymmetric-body iron condor on AAPL (spot 100): long 90 put / short 95 put ;
# short 105 call / long 110 call. Wings = 5, BODY gap = 10 (the widest adjacent gap).
_IC_LP = "AAPL240315P00090000"
_IC_SP = "AAPL240315P00095000"
_IC_SC = "AAPL240315C00105000"
_IC_LC = "AAPL240315C00110000"

# Call butterfly 170/180/190 on AAPL (spot 180) for the F3 bound tests.
_BF_LOW = "AAPL240315C00170000"
_BF_BODY = "AAPL240315C00180000"
_BF_HIGH = "AAPL240315C00190000"

# Single-leg short calls for the covered-call (F2) tests.
_CC_A = "AAPL240315C00200000"
_CC_B = "AAPL240315C00210000"

# Naked short call for the F5 fallback tests (no premium bar on the blow-up day).
_AMD_CALL = "AMD240315C00500000"


def _make_ps(symbol, bars, clock):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(symbol, bars)
    ps.set_clock(clock)
    return ps


def _account(tmp_path, tag, ps, chain_underlying, chain, bar_rows):
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
    seed_account_definition(1, CFG)
    acct = BacktestAccount(1, ps, CFG, options_provider=prov)
    wire_backtest_seams().register_account(1, acct)
    return acct, ctx


def _c(sym, k):
    return {"occ_symbol": sym, "option_type": "call", "strike": k, "expiry": "2024-03-15",
            "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _p(sym, k):
    return {"occ_symbol": sym, "option_type": "put", "strike": k, "expiry": "2024-03-15",
            "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _bar(sym, d, close, ot, k, underlying="AAPL"):
    return {"occ_symbol": sym, "date": d, "open": close, "high": close, "low": close,
            "close": close, "volume": 100, "underlying": underlying, "option_type": ot,
            "strike": k, "expiry": "2024-03-15"}


def _leg(sym, side, ot, k, ratio=1, underlying="AAPL"):
    from ba2_common.core.option_types import OptionLeg

    intent = "buy_to_open" if side == OrderDirection.BUY else "sell_to_open"
    return OptionLeg(contract_symbol=sym, side=side, ratio_qty=ratio, position_intent=intent,
                     option_type=ot, strike=k, expiry=date(2024, 3, 15), underlying=underlying)


# ---------------------------------------------------------------------------
# F1 — strategy-aware defined-risk width
# ---------------------------------------------------------------------------
def test_width_per_structure_strategy_aware():
    from app.services.backtest.backtest_account import BacktestAccount

    w = BacktestAccount._defined_risk_width_per_structure
    assert w("iron_condor", [90, 95, 105, 110]) == 5.0     # wings 5/5; body 10 is NOT risk
    assert w("iron_condor", [90, 95, 105, 112]) == 7.0     # the WIDER wing binds
    assert w("call_butterfly", [170, 180, 190]) == 10.0    # equal wings unchanged
    assert w("call_butterfly", [170, 180, 205]) == 10.0    # broken wing: min gap binds
    assert w("bull_call_spread", [100, 110]) == 10.0       # vertical: the single gap
    assert w("short_strangle", [90, 95, 105, 110]) == 10.0  # unknown shape -> widest gap
    assert w("iron_condor", [100]) is None                 # < 2 strikes: unboundable


@pytest.fixture
def acct_asym_iron_condor(tmp_path):
    """Iron condor 90/95/105/110 (spot 100), entry credit 2.0/share, with a mid-life
    OUTLIER close on the short call that would mark the group at -800 unclamped."""
    bars = [
        {"Date": datetime(2024, 3, 5), "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 100},
        {"Date": datetime(2024, 3, 6), "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 100},
        {"Date": datetime(2024, 3, 8), "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 100},
        {"Date": datetime(2024, 3, 15), "Open": 130, "High": 131, "Low": 129, "Close": 130, "Volume": 100},
    ]
    chain = [_p(_IC_LP, 90.0), _p(_IC_SP, 95.0), _c(_IC_SC, 105.0), _c(_IC_LC, 110.0)]
    bar_rows = [
        # entry-fill day: sell 95p@1.5 + sell 105c@1.5 - buy 90p@0.5 - buy 110c@0.5 = +2.0 credit
        _bar(_IC_LP, "2024-03-06", 0.5, "put", 90.0),
        _bar(_IC_SP, "2024-03-06", 1.5, "put", 95.0),
        _bar(_IC_SC, "2024-03-06", 1.5, "call", 105.0),
        _bar(_IC_LC, "2024-03-06", 0.5, "call", 110.0),
        # mid-life outlier on the short call: group net = (0.2-0.5-8.0+0.3)*100 = -800
        _bar(_IC_LP, "2024-03-08", 0.2, "put", 90.0),
        _bar(_IC_SP, "2024-03-08", 0.5, "put", 95.0),
        _bar(_IC_SC, "2024-03-08", 8.0, "call", 105.0),
        _bar(_IC_LC, "2024-03-08", 0.3, "call", 110.0),
    ]
    ps = _make_ps("AAPL", bars, datetime(2024, 3, 5))
    acct, ctx = _account(tmp_path, "f1ic", ps, "AAPL", chain, bar_rows)
    acct.submit_option_order(
        legs=[
            _leg(_IC_LP, OrderDirection.BUY, OptionRight.PUT, 90.0),
            _leg(_IC_SP, OrderDirection.SELL, OptionRight.PUT, 95.0),
            _leg(_IC_SC, OrderDirection.SELL, OptionRight.CALL, 105.0),
            _leg(_IC_LC, OrderDirection.BUY, OptionRight.CALL, 110.0),
        ],
        quantity=1, order_type="market", option_strategy="iron_condor",
    )
    acct.refresh_orders()
    acct.refresh_transactions()
    try:
        yield acct, ps
    finally:
        ctx.__exit__(None, None, None)


def test_iron_condor_mtm_clamps_to_wing_not_body(acct_asym_iron_condor):
    """The mid-life clamp must bound the condor at the WING width (5 -> -$500), not the
    body gap (10 -> -$1000): the unclamped -800 group mark is cut at -500."""
    acct, ps = acct_asym_iron_condor
    assert acct._cash == pytest.approx(10_200.0, abs=1.0)  # +200 credit collected

    ps.set_clock(datetime(2024, 3, 8))
    mtm = acct._option_positions_mtm()
    assert mtm == pytest.approx(-500.0, abs=1e-6)  # wing bound; the old body bound left -800


def test_iron_condor_expiry_max_loss_not_over_clamped(acct_asym_iron_condor):
    """The tighter wing bound must still admit the condor's legitimate max loss at expiry
    (call side fully breached at spot 130: net -5/share = -$500, exactly the bound)."""
    from app.services.backtest.daily_engine import DailyBacktestEngine

    acct, ps = acct_asym_iron_condor
    eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
    eng.account = acct
    eng.price = ps
    eng.config = CFG

    ps.set_clock(datetime(2024, 3, 15))
    eng._apply_option_expiry(datetime(2024, 3, 15))

    # credit 200 - wing loss 500 = 9700; no stock legs, all lots resolved.
    assert acct.get_option_positions() == []
    assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
    assert acct.equity() == pytest.approx(9_700.0, abs=5.0)


# ---------------------------------------------------------------------------
# F3 — combo expiry bound: parent structure count + leg multiplier
# ---------------------------------------------------------------------------
@pytest.fixture
def acct_butterfly_x5(tmp_path):
    """1-2-1 call butterfly 170/180/190, quantity=5 structures (legs 5 / 10 / 5)."""
    bars = [
        {"Date": datetime(2024, 3, 5), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 100},
        {"Date": datetime(2024, 3, 6), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 100},
    ]
    chain = [_c(_BF_LOW, 170.0), _c(_BF_BODY, 180.0), _c(_BF_HIGH, 190.0)]
    bar_rows = [
        _bar(_BF_LOW, "2024-03-06", 12.0, "call", 170.0),
        _bar(_BF_BODY, "2024-03-06", 5.0, "call", 180.0),
        _bar(_BF_HIGH, "2024-03-06", 1.0, "call", 190.0),
    ]
    ps = _make_ps("AAPL", bars, datetime(2024, 3, 5))
    acct, ctx = _account(tmp_path, "f3bf", ps, "AAPL", chain, bar_rows)
    acct.submit_option_order(
        legs=[
            _leg(_BF_LOW, OrderDirection.BUY, OptionRight.CALL, 170.0),
            _leg(_BF_BODY, OrderDirection.SELL, OptionRight.CALL, 180.0, ratio=2),
            _leg(_BF_HIGH, OrderDirection.BUY, OptionRight.CALL, 190.0),
        ],
        quantity=5, order_type="market", option_strategy="call_butterfly",
    )
    acct.refresh_orders()
    acct.refresh_transactions()
    try:
        yield acct
    finally:
        ctx.__exit__(None, None, None)


def test_butterfly_expiry_bound_uses_parent_structures(acct_butterfly_x5):
    """The bound scales by the PARENT's 5 structures ($5,000), not the body leg's 10
    contracts ($10,000 under the old max(leg qty) rule)."""
    acct = acct_butterfly_x5
    txn = acct._option_transaction_for_contract(_BF_BODY)
    positions = acct.get_option_positions()
    assert sorted(p.quantity for p in positions) == [5, 5, 10]

    bound = acct._combo_expiry_bound(txn, positions, [170.0, 180.0, 190.0])
    assert bound == pytest.approx(10.0 * 100.0 * 5)


def test_butterfly_expiry_bound_uses_leg_multiplier(acct_butterfly_x5):
    """The bound uses the legs' multiplier, not a hardcoded 100 (mini contracts x10)."""
    from ba2_common.core.option_types import OptionPosition

    acct = acct_butterfly_x5
    txn = acct._option_transaction_for_contract(_BF_BODY)
    minis = [
        OptionPosition(contract_symbol=p.contract_symbol, underlying=p.underlying,
                       option_type=p.option_type, strike=p.strike, expiry=p.expiry,
                       side=p.side, quantity=p.quantity, avg_entry_price=p.avg_entry_price,
                       multiplier=10)
        for p in acct.get_option_positions()
    ]
    bound = acct._combo_expiry_bound(txn, minis, [170.0, 180.0, 190.0])
    assert bound == pytest.approx(10.0 * 10.0 * 5)


def test_expiry_bound_fallback_min_leg_qty(acct_butterfly_x5):
    """Unresolvable parent (unknown transaction) -> structures fall back to min(leg qty),
    never max (which counts a fly's body as 2x structures)."""
    from ba2_common.core.models import Transaction
    from ba2_common.core.option_types import OptionPosition

    acct = acct_butterfly_x5
    orphan_txn = Transaction(id=987_654)  # no such transaction -> no entry order resolves
    legs = [
        OptionPosition(contract_symbol=_BF_LOW, underlying="AAPL", option_type=OptionRight.CALL,
                       strike=170.0, expiry=date(2024, 3, 15), side=OrderDirection.BUY,
                       quantity=3, avg_entry_price=1.0, multiplier=100),
        OptionPosition(contract_symbol=_BF_BODY, underlying="AAPL", option_type=OptionRight.CALL,
                       strike=180.0, expiry=date(2024, 3, 15), side=OrderDirection.SELL,
                       quantity=10, avg_entry_price=1.0, multiplier=100),
    ]
    bound = acct._combo_expiry_bound(orphan_txn, legs, [170.0, 180.0])
    assert bound == pytest.approx(10.0 * 100.0 * 3)  # min leg qty (3), not max (10)


# ---------------------------------------------------------------------------
# F2 — covered short calls: no naked margin, never liquidated while covered
# ---------------------------------------------------------------------------
def _aapl_180_bars():
    return [
        {"Date": datetime(2024, 3, 5), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 100},
        {"Date": datetime(2024, 3, 6), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 100},
        {"Date": datetime(2024, 3, 8), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 100},
    ]


def _sell_calls(acct, sym, strike, contracts):
    acct.submit_option_order(
        legs=[_leg(sym, OrderDirection.SELL, OptionRight.CALL, strike)],
        quantity=contracts, order_type="market", option_strategy="covered_call",
    )
    acct.refresh_orders()
    acct.refresh_transactions()


def test_covered_call_zero_margin_and_never_liquidated(tmp_path):
    """100 long shares fully cover 1 short call: zero maintenance requirement, and even a
    hard equity breach must not buy the covered call back."""
    ps = _make_ps("AAPL", _aapl_180_bars(), datetime(2024, 3, 5))
    acct, ctx = _account(tmp_path, "f2cc", ps, "AAPL",
                         [_c(_CC_A, 200.0)], [_bar(_CC_A, "2024-03-06", 3.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", 100, 180.0)  # the covering long shares
        _sell_calls(acct, _CC_A, 200.0, 1)
        ps.set_clock(datetime(2024, 3, 8))

        assert acct._covered_short_call_contracts() == {_CC_A}
        assert acct.maintenance_margin_requirement() == pytest.approx(0.0, abs=1e-6)

        # Force a hard breach (equity < 0): the covered call is NOT a liquidation candidate.
        acct._cash = -100_000.0
        assert acct.maybe_margin_call_liquidation() is False
        assert acct._option_positions[_CC_A].qty == -1
        assert acct._positions["AAPL"].qty == 100
    finally:
        ctx.__exit__(None, None, None)


def test_partially_covered_call_still_charged_and_liquidated(tmp_path):
    """150 shares against 2 short calls (need 200) is NOT covered: full naked margin is
    charged and a breach buys the calls back."""
    ps = _make_ps("AAPL", _aapl_180_bars(), datetime(2024, 3, 5))
    acct, ctx = _account(tmp_path, "f2pc", ps, "AAPL",
                         [_c(_CC_A, 200.0)], [_bar(_CC_A, "2024-03-06", 3.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", 150, 180.0)
        _sell_calls(acct, _CC_A, 200.0, 2)
        ps.set_clock(datetime(2024, 3, 6))

        assert acct._covered_short_call_contracts() == set()
        expected = 2 * acct.naked_margin_per_contract(200.0, spot=180.0)
        assert acct.maintenance_margin_requirement() == pytest.approx(expected)

        acct._cash = -100_000.0
        assert acct.maybe_margin_call_liquidation() is True
        assert acct._option_positions[_CC_A].qty == 0
    finally:
        ctx.__exit__(None, None, None)


def test_covered_call_greedy_cover_no_double_count(tmp_path):
    """100 shares cannot cover BOTH a 2-lot and a 1-lot short call: greedy (largest first)
    covers neither the 2-lot (needs 200) nor double-counts — only the 1-lot is exempt."""
    ps = _make_ps("AAPL", _aapl_180_bars(), datetime(2024, 3, 5))
    chain = [_c(_CC_A, 200.0), _c(_CC_B, 210.0)]
    bar_rows = [_bar(_CC_A, "2024-03-06", 3.0, "call", 200.0),
                _bar(_CC_B, "2024-03-06", 2.0, "call", 210.0)]
    acct, ctx = _account(tmp_path, "f2gr", ps, "AAPL", chain, bar_rows)
    try:
        acct._update_position("AAPL", 100, 180.0)
        _sell_calls(acct, _CC_A, 200.0, 1)
        _sell_calls(acct, _CC_B, 210.0, 2)
        ps.set_clock(datetime(2024, 3, 8))

        assert acct._covered_short_call_contracts() == {_CC_A}
        expected = 2 * acct.naked_margin_per_contract(210.0, spot=180.0)
        assert acct.maintenance_margin_requirement() == pytest.approx(expected)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# F4 — forced STOCK liquidation persists a synthetic FILLED closing order
# ---------------------------------------------------------------------------
def test_stock_liquidation_records_filled_closing_order(tmp_path):
    """A margin-called short-stock buyback must leave a FILLED closing TradingOrder
    (comment margin_call_liquidation), not just a silent cash move."""
    bars = [
        {"Date": datetime(2024, 3, 5), "Open": 450, "High": 452, "Low": 448, "Close": 450, "Volume": 100},
        {"Date": datetime(2024, 3, 6), "Open": 450, "High": 452, "Low": 448, "Close": 450, "Volume": 100},
    ]
    ps = _make_ps("AMD", bars, datetime(2024, 3, 6))
    acct, ctx = _account(tmp_path, "f4st", ps, "AMD", [], [])
    try:
        # Short 100 AMD @450 (e.g. from an assignment): equity 10k < 30% x 45k = 13.5k -> breach.
        acct._update_position("AMD", -100, 450.0)
        acct._cash += 100 * 450.0

        assert acct.maybe_margin_call_liquidation() is True
        assert acct._positions["AMD"].qty == 0
        assert acct._cash == pytest.approx(10_000.0, abs=1.0)

        closes = [o for o in acct.get_orders() if o.comment == "margin_call_liquidation"]
        assert len(closes) == 1
        o = closes[0]
        assert o.status == OrderStatus.FILLED
        assert o.side == OrderDirection.BUY          # buy-to-cover the short
        assert o.filled_qty == pytest.approx(100.0)
        assert o.open_price == pytest.approx(450.0)
        assert o.transaction_id is None              # no OPENED txn resolves -> none invented
        assert acct._fill_dates[o.id] == datetime(2024, 3, 6)  # sim-dated fill
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# F5 — option-lot liquidation with no premium bar books max(intrinsic, entry)
# ---------------------------------------------------------------------------
def _amd_short_call_account(tmp_path, tag, blowup_close):
    """Short 3 naked AMD 500 calls @3.0 with NO premium bar after the entry day, then move
    the underlying to ``blowup_close`` so the margin check breaches."""
    bars = [
        {"Date": datetime(2024, 3, 5), "Open": 450, "High": 452, "Low": 448, "Close": 450, "Volume": 100},
        {"Date": datetime(2024, 3, 6), "Open": 450, "High": 455, "Low": 449, "Close": 452, "Volume": 100},
        {"Date": datetime(2024, 3, 10), "Open": blowup_close, "High": blowup_close + 2,
         "Low": blowup_close - 2, "Close": blowup_close, "Volume": 100},
    ]
    ps = _make_ps("AMD", bars, datetime(2024, 3, 5))
    chain = [_c(_AMD_CALL, 500.0)]
    # ONLY the entry-fill day has a premium bar — the blow-up day 2024-03-10 has none.
    bar_rows = [_bar(_AMD_CALL, "2024-03-06", 3.0, "call", 500.0, underlying="AMD")]
    acct, ctx = _account(tmp_path, tag, ps, "AMD", chain, bar_rows)
    acct.submit_option_order(
        legs=[_leg(_AMD_CALL, OrderDirection.SELL, OptionRight.CALL, 500.0, underlying="AMD")],
        quantity=3, order_type="market", option_strategy="naked_call",
    )
    acct.refresh_orders()
    acct.refresh_transactions()
    assert acct._cash == pytest.approx(10_900.0, abs=1.0)  # +3x3.0x100 credit
    return acct, ps, ctx


def test_option_liquidation_no_bar_books_intrinsic(tmp_path):
    """AMD 520 / strike 500 with no premium bar: the buyback books intrinsic 20.0 (not the
    3.0 entry premium, which understated the blow-up by $5,100)."""
    acct, ps, ctx = _amd_short_call_account(tmp_path, "f5itm", 520)
    try:
        ps.set_clock(datetime(2024, 3, 10))
        assert acct.maybe_margin_call_liquidation() is True
        assert acct.get_option_positions() == []
        # 10,900 - 3 x 20.0 x 100 = 4,900 (entry-premium fallback left 10,000).
        assert acct._cash == pytest.approx(4_900.0, abs=1.0)
        closes = [o for o in acct.get_orders() if o.comment == "option_expiry_close"]
        assert len(closes) == 1
        assert closes[0].open_price == pytest.approx(20.0)
    finally:
        ctx.__exit__(None, None, None)


def test_option_liquidation_no_bar_floored_at_entry(tmp_path):
    """OTM at liquidation (intrinsic 0): the buyback is floored at the ENTRY premium — a
    forced buyback is never booked below entry."""
    acct, ps, ctx = _amd_short_call_account(tmp_path, "f5otm", 460)
    try:
        ps.set_clock(datetime(2024, 3, 10))
        # Still breaches: naked margin on 3 x 500-strike contracts dwarfs the $10k account.
        assert acct.maybe_margin_call_liquidation() is True
        assert acct.get_option_positions() == []
        # max(intrinsic 0, entry 3.0) = 3.0 -> 10,900 - 3 x 3.0 x 100 = 10,000.
        assert acct._cash == pytest.approx(10_000.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)
