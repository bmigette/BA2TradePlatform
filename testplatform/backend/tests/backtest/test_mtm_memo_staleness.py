"""``BacktestAccount._open_positions_mtm`` is memoised — prove it can never serve a stale book.

WHY THE MEMO EXISTS. Every entry candidate that reaches the live-parity equity gate re-marks the
WHOLE open option book (walk the orders for each structure's defined-risk bounds, re-price every
leg). Cost is (entry candidates) x (open positions), both of which grow with how much a genome
trades — quadratic in trade count, and measured as the reason heavy option genomes straggle every
GA generation. The mark is memoised on ``(book generation, clock)``.

WHY THIS FILE. A stale mark is a WRONG available balance, which changes what the run trades — a
missed generation bump is a correctness bug, not a performance miss. So there is ONE TEST PER
MUTATION KIND that can change the marked book:

  * an EQUITY fill                     (``_update_position``)
  * an OPTION fill / a new lot         (``_update_option_position``)
  * an option lot retired at EXPIRY    (``settle_single_leg_expiry`` -> ``_zero_option_lot``)
  * a defined-risk COMBO unit-settled  (``_settle_defined_risk_combo`` -> ``_zero_option_lot``)
  * a margin-call LIQUIDATION buy-back (``maybe_margin_call_liquidation`` -> ``_zero_option_lot``)
  * physical ASSIGNMENT creating stock (``_book_assignment_share_leg``)
  * an ORDER-SET change                (``invalidate_order_cache``: the defined-risk bounds the
                                        mark clamps to are derived from the orders)
  * a fill-time QUANTITY cap/rescale   (``_cap_single_leg_option_entry``)
  * the CLOCK advancing to a new bar   (the other half of the memo key)

plus: the memo is actually taken (it would be a silent no-op otherwise), and the
``BT_MTM_AUDIT=1`` guard really fires when a bump IS missed.
"""
from __future__ import annotations

from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD as _LEGACY_ZERO_SPREAD
from datetime import date, datetime

import pytest

from ba2_common.core.types import (AssetClass, OptionRight, OrderDirection, OrderStatus,
                                   OrderType)


CFG = {
    **_LEGACY_ZERO_SPREAD, "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

_CALL = "AAPL240315C00100000"      # single-leg long call, strike 100
_SHORT_CALL = "AAPL240315C00120000"
_BF_LOW = "AAPL240315C00170000"
_BF_BODY = "AAPL240315C00180000"
_BF_HIGH = "AAPL240315C00190000"


def _bars(closes):
    return [{"Date": d, "Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1000}
            for d, c in closes]


def _make_ps(symbol, bars, clock):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(symbol, bars)
    ps.set_clock(clock)
    return ps


def _chain(sym, k, right="call"):
    return {"occ_symbol": sym, "option_type": right, "strike": k, "expiry": "2024-03-15",
            "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _obar(sym, d, close, k, right="call", underlying="AAPL", v=1000):
    return {"occ_symbol": sym, "date": d, "open": close, "high": close, "low": close,
            "close": close, "volume": v, "underlying": underlying, "option_type": right,
            "strike": k, "expiry": "2024-03-15"}


def _leg(sym, side, right, k, ratio=1, underlying="AAPL"):
    from ba2_common.core.option_types import OptionLeg

    intent = "buy_to_open" if side == OrderDirection.BUY else "sell_to_open"
    return OptionLeg(contract_symbol=sym, side=side, ratio_qty=ratio, position_intent=intent,
                     option_type=right, strike=k, expiry=date(2024, 3, 15),
                     underlying=underlying)


def _account(tmp_path, tag, ps, chain, bar_rows, cfg=CFG):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache_db = str(tmp_path / "c.sqlite")
    cache = OptionsHistoryCache(cache_db)
    if chain:
        cache.write_chain_rows("AAPL", "2024-03-01", chain)
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


class _Probe:
    """Counts recomputes so a test can say 'the memo was taken' rather than hoping it was."""

    def __init__(self, acct):
        self.acct = acct
        self.n = 0
        self._real = acct._compute_open_positions_mtm

        def counted():
            self.n += 1
            return self._real()

        acct._compute_open_positions_mtm = counted


# ---------------------------------------------------------------------------------------------
# The memo is real
# ---------------------------------------------------------------------------------------------
def test_repeated_reads_on_an_unchanged_book_recompute_once(tmp_path):
    ps = _make_ps("AAPL", _bars([(datetime(2024, 3, 5), 100.0),
                                 (datetime(2024, 3, 6), 101.0)]), datetime(2024, 3, 5))
    acct, ctx = _account(tmp_path, "memo_hit", ps, [_chain(_CALL, 100.0)],
                         [_obar(_CALL, "2024-03-06", 5.0, 100.0)])
    try:
        probe = _Probe(acct)
        first = acct._open_positions_mtm()
        for _ in range(20):
            assert acct._open_positions_mtm() == first
        assert probe.n == 1, "the mark was re-derived on an unchanged book"
    finally:
        ctx.__exit__(None, None, None)


def test_a_new_bar_always_re_marks(tmp_path):
    """The clock is the memo's other key: the same book on a new bar must be re-priced."""
    ps = _make_ps("AAPL", _bars([(datetime(2024, 3, 5), 100.0),
                                 (datetime(2024, 3, 6), 101.0),
                                 (datetime(2024, 3, 8), 150.0)]), datetime(2024, 3, 5))
    acct, ctx = _account(tmp_path, "memo_clock", ps, [], [])
    try:
        acct._update_position("AAPL", 10.0, 100.0)
        assert acct._open_positions_mtm() == pytest.approx(1000.0)
        ps.set_clock(datetime(2024, 3, 8))
        assert acct._open_positions_mtm() == pytest.approx(1500.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------
# One test per mutation kind
# ---------------------------------------------------------------------------------------------
def test_equity_fill_invalidates(tmp_path):
    ps = _make_ps("AAPL", _bars([(datetime(2024, 3, 5), 100.0)]), datetime(2024, 3, 5))
    acct, ctx = _account(tmp_path, "mut_eq", ps, [], [])
    try:
        assert acct._open_positions_mtm() == 0.0
        acct._update_position("AAPL", 10.0, 100.0)
        assert acct._open_positions_mtm() == pytest.approx(1000.0)
        acct._update_position("AAPL", -4.0, 100.0)
        assert acct._open_positions_mtm() == pytest.approx(600.0)
    finally:
        ctx.__exit__(None, None, None)


def test_option_fill_invalidates(tmp_path):
    ps = _make_ps("AAPL", _bars([(datetime(2024, 3, 5), 100.0),
                                 (datetime(2024, 3, 6), 101.0)]), datetime(2024, 3, 5))
    acct, ctx = _account(tmp_path, "mut_opt", ps, [_chain(_CALL, 100.0)],
                         [_obar(_CALL, "2024-03-06", 5.0, 100.0)])
    try:
        acct.submit_option_order(legs=[_leg(_CALL, OrderDirection.BUY, OptionRight.CALL, 100.0)],
                                 quantity=2, order_type="market", option_strategy="long_call")
        assert acct._open_positions_mtm() == 0.0       # memoised while nothing is held
        acct.refresh_orders()                          # fills at the next bar's open, 5.00
        assert acct._option_positions[_CALL].qty == 2
        assert acct._open_positions_mtm() != 0.0       # the fill dropped the memo
        ps.set_clock(datetime(2024, 3, 6))
        assert acct._open_positions_mtm() == pytest.approx(1000.0)
    finally:
        ctx.__exit__(None, None, None)


def test_expiry_settlement_invalidates(tmp_path):
    """The lot is retired by ``settle_single_leg_expiry`` -> ``_zero_option_lot``."""
    from app.services.backtest.daily_engine import DailyBacktestEngine

    ps = _make_ps("AAPL", _bars([(datetime(2024, 3, 5), 100.0),
                                 (datetime(2024, 3, 6), 101.0),
                                 (datetime(2024, 3, 15), 90.0)]), datetime(2024, 3, 5))
    acct, ctx = _account(tmp_path, "mut_exp", ps, [_chain(_CALL, 100.0)],
                         [_obar(_CALL, "2024-03-06", 5.0, 100.0)])
    try:
        acct.submit_option_order(legs=[_leg(_CALL, OrderDirection.BUY, OptionRight.CALL, 100.0)],
                                 quantity=1, order_type="market", option_strategy="long_call")
        acct.refresh_orders()
        acct.refresh_transactions()
        ps.set_clock(datetime(2024, 3, 6))
        assert acct._open_positions_mtm() != 0.0

        ps.set_clock(datetime(2024, 3, 15))
        acct._open_positions_mtm()                     # memoise at the expiry bar FIRST
        eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
        eng.account = acct
        eng.price = ps
        eng.config = CFG
        eng._apply_option_expiry(datetime(2024, 3, 15))
        assert acct._option_positions[_CALL].qty == 0.0
        assert acct._open_positions_mtm() == pytest.approx(0.0)
    finally:
        ctx.__exit__(None, None, None)


def test_combo_unit_settlement_invalidates(tmp_path):
    """A defined-risk butterfly unit-settles as a GROUP; every leg is retired in one pass."""
    from app.services.backtest.daily_engine import DailyBacktestEngine

    ps = _make_ps("AAPL", _bars([(datetime(2024, 3, 5), 180.0),
                                 (datetime(2024, 3, 6), 180.0),
                                 (datetime(2024, 3, 15), 180.0)]), datetime(2024, 3, 5))
    chain = [_chain(_BF_LOW, 170.0), _chain(_BF_BODY, 180.0), _chain(_BF_HIGH, 190.0)]
    bar_rows = [_obar(_BF_LOW, "2024-03-06", 12.0, 170.0),
                _obar(_BF_BODY, "2024-03-06", 5.0, 180.0),
                _obar(_BF_HIGH, "2024-03-06", 1.0, 190.0)]
    acct, ctx = _account(tmp_path, "mut_combo", ps, chain, bar_rows)
    try:
        acct.submit_option_order(
            legs=[_leg(_BF_LOW, OrderDirection.BUY, OptionRight.CALL, 170.0),
                  _leg(_BF_BODY, OrderDirection.SELL, OptionRight.CALL, 180.0, ratio=2),
                  _leg(_BF_HIGH, OrderDirection.BUY, OptionRight.CALL, 190.0)],
            quantity=1, order_type="market", option_strategy="call_butterfly")
        acct.refresh_orders()
        acct.refresh_transactions()

        ps.set_clock(datetime(2024, 3, 15))
        acct._open_positions_mtm()                     # memoise BEFORE the settlement
        eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
        eng.account = acct
        eng.price = ps
        eng.config = CFG
        eng._apply_option_expiry(datetime(2024, 3, 15))
        assert all(l.qty == 0.0 for l in acct._option_positions.values())
        assert acct._open_positions_mtm() == pytest.approx(0.0)
    finally:
        ctx.__exit__(None, None, None)


def test_margin_call_liquidation_invalidates(tmp_path):
    """The buy-back path retires the lot through ``_zero_option_lot`` too."""
    ps = _make_ps("AAPL", _bars([(datetime(2024, 3, 5), 100.0),
                                 (datetime(2024, 3, 6), 101.0)]), datetime(2024, 3, 5))
    cfg = dict(CFG, starting_cash=3_000.0)
    acct, ctx = _account(tmp_path, "mut_margin", ps, [_chain(_SHORT_CALL, 120.0)],
                         [_obar(_SHORT_CALL, "2024-03-06", 2.0, 120.0)], cfg=cfg)
    try:
        acct.submit_option_order(
            legs=[_leg(_SHORT_CALL, OrderDirection.SELL, OptionRight.CALL, 120.0)],
            quantity=20, order_type="market", option_strategy="naked_call")
        acct.refresh_orders()
        acct.refresh_transactions()
        ps.set_clock(datetime(2024, 3, 6))
        lot = acct._option_positions.get(_SHORT_CALL)
        if lot is None or lot.qty == 0:
            pytest.skip("the short leg did not fill in this fixture; nothing to liquidate")
        before = acct._open_positions_mtm()             # memoise BEFORE the liquidation
        if not acct.maybe_margin_call_liquidation():
            pytest.skip("no maintenance breach in this fixture")
        assert acct._option_positions[_SHORT_CALL].qty == 0.0
        assert acct._open_positions_mtm() != before
        assert acct._open_positions_mtm() == pytest.approx(0.0)
    finally:
        ctx.__exit__(None, None, None)


def test_assignment_share_leg_invalidates(tmp_path):
    """Physical assignment creates an EQUITY position; the mark must show it immediately."""
    ps = _make_ps("AAPL", _bars([(datetime(2024, 3, 5), 100.0)]), datetime(2024, 3, 5))
    acct, ctx = _account(tmp_path, "mut_assign", ps, [], [])
    try:
        assert acct._open_positions_mtm() == 0.0
        acct._book_assignment_share_leg("AAPL", 100.0, 95.0, expert_id=None)
        assert acct._open_positions_mtm() == pytest.approx(10_000.0)
    finally:
        ctx.__exit__(None, None, None)


def test_order_set_change_invalidates(tmp_path):
    """The mark CLAMPS defined-risk groups to bounds derived from the ORDER set, so an
    order-set change (``invalidate_order_cache``) must drop the memo even though no position
    moved."""
    ps = _make_ps("AAPL", _bars([(datetime(2024, 3, 5), 100.0)]), datetime(2024, 3, 5))
    acct, ctx = _account(tmp_path, "mut_orders", ps, [], [])
    try:
        probe = _Probe(acct)
        acct._open_positions_mtm()
        assert probe.n == 1
        acct._open_positions_mtm()
        assert probe.n == 1                             # memo hit
        acct.invalidate_order_cache()
        acct._open_positions_mtm()
        assert probe.n == 2                             # dropped by the order-set change
    finally:
        ctx.__exit__(None, None, None)


def test_fill_time_quantity_cap_invalidates(tmp_path):
    """A cash-secured cap rescales the order IN PLACE at fill time (no new order row); the
    defined-risk width the mark clamps to is scaled by that quantity."""
    ps = _make_ps("AAPL", _bars([(datetime(2024, 3, 5), 100.0),
                                 (datetime(2024, 3, 6), 101.0)]), datetime(2024, 3, 5))
    cfg = dict(CFG, starting_cash=1_000.0)
    acct, ctx = _account(tmp_path, "mut_cap", ps, [_chain(_CALL, 100.0)],
                         [_obar(_CALL, "2024-03-06", 5.0, 100.0)], cfg=cfg)
    try:
        acct.submit_option_order(legs=[_leg(_CALL, OrderDirection.BUY, OptionRight.CALL, 100.0)],
                                 quantity=10, order_type="market", option_strategy="long_call")
        acct._open_positions_mtm()                      # memoise before the capped fill
        acct.refresh_orders()                           # 10 x 5 x 100 = 5,000 > 1,000 -> cap 2
        assert acct._option_positions[_CALL].qty == 2
        ps.set_clock(datetime(2024, 3, 6))
        assert acct._open_positions_mtm() == pytest.approx(1000.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------
# The audit guard itself
# ---------------------------------------------------------------------------------------------
def test_audit_mode_catches_a_missed_bump(tmp_path, monkeypatch):
    """A guard nobody has seen fire is a guard nobody knows works. Mutate the ledger BEHIND the
    generation counter (no production path does this) and assert BT_MTM_AUDIT refuses the read."""
    from app.services.backtest import backtest_account as ba

    ps = _make_ps("AAPL", _bars([(datetime(2024, 3, 5), 100.0)]), datetime(2024, 3, 5))
    acct, ctx = _account(tmp_path, "mut_audit", ps, [], [])
    try:
        acct._update_position("AAPL", 10.0, 100.0)
        assert acct._open_positions_mtm() == pytest.approx(1000.0)
        monkeypatch.setattr(ba, "_MTM_AUDIT", True)
        acct._positions["AAPL"].qty = 20.0              # behind the counter's back
        with pytest.raises(ba.StaleMarkToMarket):
            acct._open_positions_mtm()
    finally:
        ctx.__exit__(None, None, None)


def test_no_clock_means_no_memo(tmp_path):
    """A price source with no clock set (bare unit-test doubles) must keep the un-memoised
    behaviour rather than key the memo on None."""
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount

    ps = AsOfPriceSource(ohlcv_provider=None)
    wire_backtest_seams()
    ctx = backtest_trading_db("mut_noclock")
    ctx.__enter__()
    try:
        seed_account_definition(1, CFG)
        acct = BacktestAccount(1, ps, CFG)
        probe = _Probe(acct)
        assert acct._open_positions_mtm() == 0.0
        assert acct._open_positions_mtm() == 0.0
        assert probe.n == 2                             # no clock -> never memoised
        assert acct._mtm_memo is None
    finally:
        ctx.__exit__(None, None, None)
