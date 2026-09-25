"""Task 1a (plan 2026-09-24): an option lot held ACROSS a stock split is valued in its OWN
(entry-day) share basis, and a bar that belongs to a DIFFERENT contract reusing the lot's OCC
string after the split counts as "no bar".

THE MEASURED DEFECT. Strikes are as traded; the price series is split-adjusted to today's
basis, and the option path converted the adjusted spot with the factor of the CURRENT bar. For
a lot opened before a split and still held after it that factor is 1, so a pre-split strike was
compared with a post-split spot: puts gained and calls lost by the split ratio. AAPL 4:1 on
2020-08-31: a pre-split P400 expiring 2020-09-18 was settled at 400 - 106.84 = 293.16/share
(~$29k/contract) although the adjusted close 106.84 is 427.36 in the contract's basis
(worthless). Five such puts produced +$27k..+$57k each in the O_LP grid job.

TASK 0 (ThetaData store): a pre-split OCC string has NO bars after the split, and some
strings are REUSED by a different contract after it (AAPL201016P00250000: 0.49 on 08-28, then
120.40 on 08-31). A held lot must never be marked, quoted, filled or settled against those.

The fixture is synthetic but shaped on that data: AAPL with a 4:1 split on 2020-08-31, adjusted
closes (pre-split raw / 4), the option chain AS TRADED, with the lot's contract carrying bars
only before the split unless a test adds a colliding post-split bar.
"""
from __future__ import annotations

from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD as _LEGACY_ZERO_SPREAD
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta

import pytest

from ba2_common.core.option_bs import bs_price
from ba2_common.core.option_types import OptionLeg, OptionQuote
from ba2_common.core.split_basis import CalendarSplit, SymbolSplitBasis
from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus

SPLIT = date(2020, 8, 31)
OPEN_DAY = date(2020, 8, 24)       # 5 sessions before the split
LAST_PRE = date(2020, 8, 28)       # the last pre-split session
EXPIRY = date(2020, 9, 18)         # 3 weeks after the split
PUT = "AAPL200918P00410000"
CALL = "AAPL200918C00300000"
SHORT_CALL = "AAPL200918C00500000"
PRE_SPLIT_EXPIRY = date(2020, 8, 28)
CONTROL_PUT = "AAPL200828P00410000"

CFG = {**_LEGACY_ZERO_SPREAD, "starting_cash": 1_000_000.0, "commission_per_trade": 0.0,
       "slippage_bps": 0.0, "fill_model": "same_bar_close"}
IV = 0.30


def _sessions():
    d, out = date(2020, 8, 17), []
    while d <= date(2020, 9, 25):
        if d.weekday() < 5 and d != date(2020, 9, 7):      # Labor Day
            out.append(d)
        d += timedelta(days=1)
    return out


def _closes(pre_raw=400.0, post_adj=110.0, overrides=None):
    """ADJUSTED closes: pre-split sessions are the raw price / 4 (FMP's back-adjustment)."""
    overrides = overrides or {}
    return [(d, overrides.get(d, (pre_raw / 4.0) if d < SPLIT else post_adj)) for d in _sessions()]


def _bar(close, volume=10_000):
    return {"open": close, "high": close, "low": close, "close": close, "volume": volume,
            "iv": IV}


class _Chain:
    """An AS-TRADED option store: one dict of (occ, day) -> bar."""

    def __init__(self, bars):
        self.bars = dict(bars)

    def get_bar(self, occ, as_of):
        b = self.bars.get((occ, as_of))
        return dict(b) if b else None

    def get_quote(self, occ, as_of, *, data_session=None):
        b = self.get_bar(occ, as_of)
        if b is None:
            return None
        return OptionQuote(symbol=occ, bid=b["close"], ask=b["close"], last=b["close"],
                           implied_volatility=b.get("iv"), volume=b.get("volume"))

    def get_chain(self, *a, **k):
        return []

    def get_atm_iv(self, *a, **k):
        return None

    def chain_staleness(self):
        return {}

    def delta_at_entry(self, underlying, occ_symbol, when):
        return None


def _basis():
    from app.services.backtest.option_split_basis import RunSplitBasis
    return RunSplitBasis({"AAPL": SymbolSplitBasis(
        "AAPL", (CalendarSplit(SPLIT, 4.0),), basis_date=date(2026, 1, 1))})


def _dt(d):
    return datetime.combine(d, time())


@contextmanager
def _harness(opt_bars, closes, cfg=CFG):
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    wire_backtest_seams()
    ctx = backtest_trading_db("optsplitcross")
    ctx.__enter__()
    try:
        seed_account_definition(1, cfg)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", [{"Date": _dt(d), "Open": c, "High": c, "Low": c, "Close": c,
                               "Volume": 1e6} for d, c in closes])
        ps.set_clock(_dt(OPEN_DAY))
        acct = BacktestAccount(1, ps, cfg, options_provider=_Chain(opt_bars),
                               split_basis=_basis())
        wire_backtest_seams().register_account(1, acct)
        engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
        engine.account = acct
        engine.price = ps
        engine.config = cfg
        yield engine, acct, ps
    finally:
        ctx.__exit__(None, None, None)


def _open(acct, contract, right, strike, expiry, side, strategy):
    leg = OptionLeg(contract_symbol=contract, side=side,
                    position_intent="buy_to_open" if side == OrderDirection.BUY else "sell_to_open",
                    option_type=right, strike=strike, expiry=expiry, underlying="AAPL")
    acct.submit_option_order(legs=[leg], quantity=1, order_type="market", option_strategy=strategy)
    acct.refresh_orders()
    acct.refresh_transactions()
    held = [p for p in acct.get_option_positions() if p.contract_symbol == contract]
    assert len(held) == 1, "the lot must open on the fixture's entry bar"
    return held[0]


def _pre_split_bars(contract, closes_by_day):
    return {(contract, d): _bar(c) for d, c in closes_by_day.items()}


def _expire(engine, acct, ps, day=EXPIRY):
    ps.set_clock(_dt(day))
    engine._apply_option_expiry(_dt(day))
    acct.refresh_transactions()


def _expiry_close(acct, contract):
    closes = [o for o in acct.get_orders()
              if o.comment == "option_expiry_close" and o.contract_symbol == contract]
    assert len(closes) == 1, closes
    return closes[0]


def _lot_mark(acct, contract):
    """The per-share mark the equity curve uses for the lot on the current bar."""
    lot = acct._option_positions[contract]
    before = acct._option_positions_mtm()
    return before / (lot.qty * lot.multiplier)


def _bs_lot_basis(spot_contract_basis, strike, day, right):
    from app.services.backtest.options_store import default_options_risk_free_rate
    px = bs_price(spot_contract_basis, strike, (EXPIRY - day).days, IV, right,
                  r=default_options_risk_free_rate())
    intrinsic = max(0.0, (strike - spot_contract_basis) if right == OptionRight.PUT
                    else (spot_contract_basis - strike))
    return max(px, intrinsic)


# =============================================================================================
# 1. long put across a 4:1 split, really OTM at expiry
# =============================================================================================
def test_a_long_put_held_across_the_split_that_is_really_otm_expires_worthless():
    """K=410 bought at 15.00 with the stock at 400. At expiry the adjusted close is 110, i.e.
    440 in the contract's basis: OTM. The pre-fix code compared 410 with 110 and sold the put
    at 300/share."""
    bars = _pre_split_bars(PUT, {OPEN_DAY: 15.0, LAST_PRE: 12.0})
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.BUY, "long_put")
        cash_after_entry = acct._cash
        assert cash_after_entry == pytest.approx(CFG["starting_cash"] - 1500.0)

        _expire(engine, acct, ps)

        close = _expiry_close(acct, PUT)
        assert close.open_price == pytest.approx(0.0)
        assert acct._cash == pytest.approx(cash_after_entry)          # P&L = -premium
        txn = acct._option_transaction_for_contract(PUT)
        assert txn is None or str(getattr(txn, "close_reason", "")).endswith("expired_otm")
        assert acct.get_option_positions() == []


# =============================================================================================
# 2. long call across a 4:1 split, really ITM at expiry
# =============================================================================================
def test_a_long_call_held_across_the_split_that_is_really_itm_settles_at_lot_basis_intrinsic():
    """K=300 bought at 101.00 with the stock at 400. Adjusted close at expiry 110 = 440 in the
    contract's basis: intrinsic 140, settled with no expiry bar at 140 x 100. Pre-fix: 110 vs
    300 -> 'OTM', worthless."""
    bars = _pre_split_bars(CALL, {OPEN_DAY: 101.0, LAST_PRE: 102.0})
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, CALL, OptionRight.CALL, 300.0, EXPIRY, OrderDirection.BUY, "long_call")
        cash_after_entry = acct._cash

        _expire(engine, acct, ps)

        close = _expiry_close(acct, CALL)
        assert close.open_price == pytest.approx(140.0)
        assert acct._cash == pytest.approx(cash_after_entry + 140.0 * 100)
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []


def test_expiry_settles_on_the_expiry_dates_close_not_the_settlement_bars():
    """The settlement bar can be LATER than the expiry date (an expiry the engine reaches on
    the next bar). The spot is the EXPIRY date's close: 110 (440 basis, ITM 140), not the next
    session's 60 (240 basis, which would make the call worthless)."""
    bars = _pre_split_bars(CALL, {OPEN_DAY: 101.0, LAST_PRE: 102.0})
    closes = _closes(overrides={date(2020, 9, 21): 60.0})
    with _harness(bars, closes) as (engine, acct, ps):
        _open(acct, CALL, OptionRight.CALL, 300.0, EXPIRY, OrderDirection.BUY, "long_call")
        cash_after_entry = acct._cash

        _expire(engine, acct, ps, day=date(2020, 9, 21))

        assert _expiry_close(acct, CALL).open_price == pytest.approx(140.0)
        assert acct._cash == pytest.approx(cash_after_entry + 140.0 * 100)


# =============================================================================================
# 3. the daily mark across the split
# =============================================================================================
def test_the_mark_on_the_split_bar_moves_only_by_the_real_price_move():
    """Pre-split close 100.5 adjusted (402 as traded), split-day close 101 adjusted (404 in
    the lot's basis): a $2 real move. The lot has no bar on the split day (Task 0), so it is
    marked by BS off its own last iv IN ITS OWN BASIS. Pre-fix the spot was 101 against a 410
    strike -> ~309/share of fake intrinsic (+$29.7k of equity in one bar)."""
    bars = _pre_split_bars(PUT, {OPEN_DAY: 15.0, LAST_PRE: 12.0})
    closes = _closes(overrides={LAST_PRE: 100.5, SPLIT: 101.0})
    with _harness(bars, closes) as (engine, acct, ps):
        _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.BUY, "long_put")
        ps.set_clock(_dt(LAST_PRE))
        eq_pre = acct.equity()
        assert _lot_mark(acct, PUT) == pytest.approx(12.0)

        ps.set_clock(_dt(SPLIT))
        eq_split = acct.equity()

        expected = _bs_lot_basis(404.0, 410.0, SPLIT, OptionRight.PUT)
        assert _lot_mark(acct, PUT) == pytest.approx(expected, abs=1e-9)
        assert eq_split - eq_pre == pytest.approx((expected - 12.0) * 100.0, abs=1e-6)
        assert abs(eq_split - eq_pre) < 1_000.0


def test_crossing_the_split_is_logged_once_per_lot(caplog):
    import logging
    bars = _pre_split_bars(PUT, {OPEN_DAY: 15.0, LAST_PRE: 12.0})
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.BUY, "long_put")
        with caplog.at_level(logging.WARNING):
            for d in (SPLIT, date(2020, 9, 1), date(2020, 9, 2)):
                ps.set_clock(_dt(d))
                acct.equity()
        msgs = [r.getMessage() for r in caplog.records if "ACROSS A SPLIT" in r.getMessage()]
        assert len(msgs) == 1, msgs
        assert PUT in msgs[0] and "2020-08-31" in msgs[0] and "4" in msgs[0]


# =============================================================================================
# 4. a post-split bar under the held lot's OCC string belongs to ANOTHER contract
# =============================================================================================
def _collision_bars():
    bars = _pre_split_bars(PUT, {OPEN_DAY: 15.0, LAST_PRE: 12.0})
    for d in _sessions():
        if SPLIT <= d <= EXPIRY:
            bars[(PUT, d)] = _bar(120.40)
    return bars


def test_a_reused_occ_string_is_not_used_to_mark_the_held_lot():
    closes = _closes(overrides={LAST_PRE: 100.5, SPLIT: 101.0})
    with _harness(_collision_bars(), closes) as (engine, acct, ps):
        _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.BUY, "long_put")
        ps.set_clock(_dt(LAST_PRE))
        acct.equity()                                    # last real iv observed on 08-28
        ps.set_clock(_dt(SPLIT))
        assert _lot_mark(acct, PUT) == pytest.approx(
            _bs_lot_basis(404.0, 410.0, SPLIT, OptionRight.PUT), abs=1e-9)


def test_a_reused_occ_string_is_not_quoted_for_the_held_lot():
    with _harness(_collision_bars(), _closes()) as (engine, acct, ps):
        _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.BUY, "long_put")
        ps.set_clock(_dt(LAST_PRE))
        assert acct.get_option_quote(PUT) is not None    # the lot's own pre-split bar
        ps.set_clock(_dt(SPLIT))
        assert acct.get_option_quote(PUT) is None


def test_a_reused_occ_string_does_not_fill_a_close_of_the_held_lot():
    with _harness(_collision_bars(), _closes()) as (engine, acct, ps):
        pos = _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.BUY, "long_put")
        ps.set_clock(_dt(SPLIT))
        cash = acct._cash
        acct.close_option_position(pos, order_type="market")
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct._option_positions[PUT].qty == 1
        assert acct._cash == pytest.approx(cash)
        closing = [o for o in acct.get_orders()
                   if o.contract_symbol == PUT and o.side == OrderDirection.SELL]
        assert closing and all(o.status != OrderStatus.FILLED for o in closing)


def test_a_reused_occ_string_does_not_settle_the_held_lot():
    """Really ITM in the lot's basis (adjusted 95 = 380 < 410: intrinsic 30). The expiry
    bar under the OCC string (120.40) is another contract's: settlement is the lot-basis
    intrinsic, 30. Pre-fix: spot 95 -> 'intrinsic' 315 and the junk bar clamped up to it."""
    closes = _closes(overrides={EXPIRY: 95.0})
    with _harness(_collision_bars(), closes) as (engine, acct, ps):
        _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.BUY, "long_put")
        cash_after_entry = acct._cash
        _expire(engine, acct, ps)
        assert _expiry_close(acct, PUT).open_price == pytest.approx(30.0)
        assert acct._cash == pytest.approx(cash_after_entry + 30.0 * 100)


def test_a_reused_occ_string_is_not_the_run_end_mark_of_the_held_lot():
    closes = _closes(overrides={date(2020, 9, 1): 101.0})
    with _harness(_collision_bars(), closes) as (engine, acct, ps):
        _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.BUY, "long_put")
        ps.set_clock(_dt(date(2020, 9, 1)))
        rows = [t for t in acct.get_round_trip_trades()
                if t.get("contract_symbol") == PUT]
        assert len(rows) == 1
        # 101 adjusted = 404 in the lot's basis -> intrinsic 6; the entry premium (15) stands.
        assert rows[0]["exit_price"] == pytest.approx(15.0)


# =============================================================================================
# 5. assignment across a split
# =============================================================================================
def test_a_short_put_assigned_across_the_split_books_shares_in_the_equity_basis():
    """Short P410 written at 15.00 pre-split; adjusted close at expiry 95 = 380 in the
    contract's basis: assigned. The contract delivers 100 PRE-split shares at 410 = 400
    adjusted shares at 102.50 -- the same $41,000. Pre-fix: 100 shares at 410."""
    bars = _pre_split_bars(PUT, {OPEN_DAY: 15.0, LAST_PRE: 12.0})
    closes = _closes(overrides={EXPIRY: 95.0})
    with _harness(bars, closes) as (engine, acct, ps):
        _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.SELL, "short_put")
        cash_after_entry = acct._cash
        assert cash_after_entry == pytest.approx(CFG["starting_cash"] + 1500.0)

        _expire(engine, acct, ps)

        assert _expiry_close(acct, PUT).open_price == pytest.approx(30.0)
        pos = acct._positions["AAPL"]
        assert pos.qty == pytest.approx(400.0)
        assert pos.avg_price == pytest.approx(102.5)
        assert acct._cash == pytest.approx(cash_after_entry - 41_000.0)


# =============================================================================================
# covered-call cover and the pledged-share lock across a split
# =============================================================================================
def test_a_short_call_written_pre_split_needs_its_own_basis_in_cover():
    """One pre-split call delivers 100 PRE-split shares = 400 adjusted. 100 adjusted shares
    held do not cover it (pre-fix: the bar's factor 1 said they did)."""
    bars = _pre_split_bars(SHORT_CALL, {OPEN_DAY: 2.0, LAST_PRE: 1.5})
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, SHORT_CALL, OptionRight.CALL, 500.0, EXPIRY, OrderDirection.SELL, "short_call")
        ps.set_clock(_dt(SPLIT))
        acct._update_position("AAPL", 100.0, 110.0)
        assert SHORT_CALL not in acct._covered_short_call_contracts()
        acct._update_position("AAPL", 300.0, 110.0)
        assert SHORT_CALL in acct._covered_short_call_contracts()


def test_the_pledged_share_lock_counts_a_pre_split_call_in_its_own_basis():
    """450 adjusted shares held, one pre-split call pledging 400 of them: a 100-share sale is
    clamped to the 50 free. Pre-fix the pledge read 100 and the whole sale went through."""
    bars = _pre_split_bars(SHORT_CALL, {OPEN_DAY: 2.0, LAST_PRE: 1.5})
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, SHORT_CALL, OptionRight.CALL, 500.0, EXPIRY, OrderDirection.SELL, "short_call")
        ps.set_clock(_dt(SPLIT))
        acct._update_position("AAPL", 450.0, 110.0)
        assert acct._pledged_share_lock("AAPL", 100.0, context="test") == pytest.approx(50.0)
        assert acct.pledged_shares_in_equity_units("AAPL", 100) == 400


# =============================================================================================
# intraday drawdown refinement: every 5m bar of a trade in the trade's entry basis
# =============================================================================================
def test_the_intraday_refinement_prices_a_split_spanning_trade_in_its_entry_basis(monkeypatch):
    import pandas as pd
    import app.services.backtest.intraday_drawdown as I
    import app.services.backtest.results as R

    with _harness({}, _closes()) as (engine, acct, ps):
        bars = pd.DataFrame({"Date": [pd.Timestamp("2020-08-27 10:00"),
                                      pd.Timestamp("2020-09-01 10:00")],
                             "Low": [99.0, 105.0], "High": [101.0, 111.0]})
        monkeypatch.setattr(R, "_get_5m_bars_cached", lambda *a, **k: bars)
        seen = {}

        def spy(trades, max_dd, **kw):
            seen["bars"] = kw["bars_5m_between"]("AAPL", pd.Timestamp("2020-08-24"),
                                                  pd.Timestamp("2020-09-18"))
            return max_dd

        monkeypatch.setattr(I, "refine_max_drawdown", spy)
        fn = R._build_refine_drawdown_fn(
            acct, {"account_settings": {"commission_per_trade": 0.0},
                   "start_date": date(2020, 8, 17), "end_date": date(2020, 9, 25)})
        fn([], 0.1)
        assert [b["Low"] for b in seen["bars"]] == pytest.approx([396.0, 420.0])
        assert [b["High"] for b in seen["bars"]] == pytest.approx([404.0, 444.0])


# =============================================================================================
# 6. control: a lot that never crosses a split is unchanged
# =============================================================================================
def test_a_lot_that_never_crosses_a_split_keeps_the_pre_fix_numbers():
    """AAPL P410 expiring 2020-08-28, the session BEFORE the split: marked at its bar close,
    then ITM at expiry (adjusted 101 = 404 as traded, intrinsic 6, no expiry bar)."""
    bars = _pre_split_bars(CONTROL_PUT, {OPEN_DAY: 15.0, date(2020, 8, 26): 11.0})
    closes = _closes(overrides={PRE_SPLIT_EXPIRY: 101.0})
    with _harness(bars, closes) as (engine, acct, ps):
        _open(acct, CONTROL_PUT, OptionRight.PUT, 410.0, PRE_SPLIT_EXPIRY, OrderDirection.BUY,
              "long_put")
        ps.set_clock(_dt(date(2020, 8, 26)))
        assert acct.equity() == pytest.approx(CFG["starting_cash"] - 1500.0 + 1100.0)
        _expire(engine, acct, ps, day=PRE_SPLIT_EXPIRY)
        assert _expiry_close(acct, CONTROL_PUT).open_price == pytest.approx(6.0)
        assert acct._cash == pytest.approx(CFG["starting_cash"] - 1500.0 + 600.0)


# =============================================================================================
# Review follow-ups: the GA default fill model (next_bar_open) -- a decision on bar D fills on
# D+1's open, so an order decided the session before the ex-date fills ON the ex-date.
# =============================================================================================
NBO = {**CFG, "fill_model": "next_bar_open"}
POST_PUT = "AAPL200918P00100000"          # the adjusted contract's own post-split string


def _order_rows(acct, contract):
    return [o for o in acct.get_orders() if o.contract_symbol == contract]


def test_nbo_a_close_decided_pre_split_does_not_fill_on_the_ex_date():
    """(a) Close decided 08-28 would fill on 08-31 (the ex-date) against the reused string's
    120.40: no raise, no fill, the lot unchanged. On 08-31 the MARKET close -- which the DAY
    sweep never ages -- is cancelled (its OCC string no longer names the contract)."""
    with _harness(_collision_bars(), _closes(), cfg=NBO) as (engine, acct, ps):
        ps.set_clock(_dt(date(2020, 8, 21)))
        pos = _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.BUY, "long_put")
        lot = acct._option_positions[PUT]
        assert (lot.basis_factor, lot.basis_date) == (4.0, OPEN_DAY)
        ps.set_clock(_dt(LAST_PRE))
        cash = acct._cash
        acct.close_option_position(pos, order_type="market")
        acct.refresh_orders()
        acct.refresh_transactions()
        assert lot.qty == 1 and acct._cash == pytest.approx(cash)
        closing = [o for o in _order_rows(acct, PUT) if o.side == OrderDirection.SELL]
        assert len(closing) == 1 and closing[0].status != OrderStatus.FILLED

        ps.set_clock(_dt(SPLIT))
        acct.refresh_orders()
        acct.refresh_transactions()
        assert lot.qty == 1 and acct._cash == pytest.approx(cash)
        closing = [o for o in _order_rows(acct, PUT) if o.side == OrderDirection.SELL]
        assert closing[0].status == OrderStatus.CANCELED


def test_nbo_an_ex_date_entry_takes_the_fill_days_basis_and_is_not_a_crossing(caplog):
    """(b) Decided ON the ex-date, filled the next session: basis = k(fill day) = 1, and the
    lot is marked from its own bars with no crossing warning."""
    import logging
    bars = {(POST_PUT, date(2020, 9, 1)): _bar(3.0), (POST_PUT, date(2020, 9, 2)): _bar(3.5)}
    with _harness(bars, _closes(), cfg=NBO) as (engine, acct, ps):
        ps.set_clock(_dt(SPLIT))
        with caplog.at_level(logging.WARNING):
            _open(acct, POST_PUT, OptionRight.PUT, 100.0, EXPIRY, OrderDirection.BUY, "long_put")
            lot = acct._option_positions[POST_PUT]
            assert (lot.basis_factor, lot.basis_date) == (1.0, date(2020, 9, 1))
            acct.equity()
            ps.set_clock(_dt(date(2020, 9, 2)))
            assert _lot_mark(acct, POST_PUT) == pytest.approx(3.5)
        assert not [r for r in caplog.records if "ACROSS A SPLIT" in r.getMessage()]


def test_a_day_before_the_lots_basis_day_is_not_its_bar_and_is_not_a_crossing(caplog):
    """(b, defensive) A lot whose basis day is AHEAD of the clock in another basis (booked at
    the clock, premium read on the next bar): that day's bar is not the lot's, but nothing has
    crossed -- no WARNING."""
    import logging
    from app.services.backtest.backtest_account import _OptionLot
    bars = {(POST_PUT, LAST_PRE): _bar(9.0)}
    with _harness(bars, _closes()) as (engine, acct, ps):
        ps.set_clock(_dt(LAST_PRE))
        acct._option_positions[POST_PUT] = _OptionLot(
            POST_PUT, qty=1, avg_price=3.0, underlying="AAPL", basis_factor=1.0, basis_date=SPLIT)
        with caplog.at_level(logging.WARNING):
            assert acct._option_bar(POST_PUT) is None
            assert acct.get_option_quote(POST_PUT) is None
        assert not [r for r in caplog.records if "ACROSS A SPLIT" in r.getMessage()]


def test_nbo_an_open_decided_before_the_split_is_refused_on_the_ex_date(caplog):
    """(c) A BUY decided 08-28 would fill on 08-31 at the REUSED string's 300.50 -- a
    different trade -- and a generous limit does not protect. Refused loudly, left pending,
    then expired by the DAY sweep."""
    import logging
    bars = {(PUT, d): _bar(300.5) for d in _sessions() if d >= SPLIT}
    with _harness(bars, _closes(), cfg=NBO) as (engine, acct, ps):
        ps.set_clock(_dt(LAST_PRE))
        leg = OptionLeg(contract_symbol=PUT, side=OrderDirection.BUY, position_intent="buy_to_open",
                        option_type=OptionRight.PUT, strike=410.0, expiry=EXPIRY, underlying="AAPL")
        cash = acct._cash
        with caplog.at_level(logging.WARNING):
            acct.submit_option_order(legs=[leg], quantity=1, order_type="limit", limit_price=400.0,
                                     option_strategy="long_put")
            acct.refresh_orders()
        refused = [r.getMessage() for r in caplog.records if "fill REFUSED" in r.getMessage()]
        assert len(refused) == 1, refused
        assert PUT in refused[0] and "2020-08-28" in refused[0] and "2020-08-31" in refused[0]
        assert "4:1" in refused[0]
        assert PUT not in acct._option_positions and acct._cash == pytest.approx(cash)
        (order,) = _order_rows(acct, PUT)
        assert order.status in OrderStatus.get_active_statuses()

        ps.set_clock(_dt(SPLIT))
        acct.refresh_orders()
        (order,) = _order_rows(acct, PUT)
        assert order.status == OrderStatus.EXPIRED
        assert PUT not in acct._option_positions


def test_a_fill_that_would_mix_two_bases_in_one_lot_raises_a_named_error():
    """(d) The ledger invariant, behind the fill engine's own refusal."""
    from app.services.backtest.backtest_account import OptionLotBasisMismatch
    bars = _pre_split_bars(PUT, {OPEN_DAY: 15.0})
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.BUY, "long_put")
        with pytest.raises(OptionLotBasisMismatch, match="a split lies between them"):
            acct._update_option_position(PUT, 1.0, 1.0, 100.0, underlying="AAPL",
                                         fill_day=SPLIT)
        assert issubclass(OptionLotBasisMismatch, RuntimeError)
        assert acct._option_positions[PUT].qty == 1


def test_a_combo_whose_legs_are_in_different_bases_refuses_the_run():
    """(e) One net payoff cannot be stated for such a combo: SplitBasisRefused, which the
    engine re-raises out of its per-expiry handler."""
    from ba2_common.core.split_basis import SplitBasisRefused
    long_leg = "AAPL200918P00400000"
    bars = {(PUT, OPEN_DAY): _bar(15.0), (long_leg, OPEN_DAY): _bar(10.0)}
    with _harness(bars, _closes()) as (engine, acct, ps):
        short = OptionLeg(contract_symbol=PUT, side=OrderDirection.SELL,
                          position_intent="sell_to_open", option_type=OptionRight.PUT,
                          strike=410.0, expiry=EXPIRY, underlying="AAPL")
        long_ = OptionLeg(contract_symbol=long_leg, side=OrderDirection.BUY,
                          position_intent="buy_to_open", option_type=OptionRight.PUT,
                          strike=400.0, expiry=EXPIRY, underlying="AAPL")
        acct.submit_option_order(legs=[short, long_], quantity=1, order_type="market",
                                 option_strategy="bull_put_spread")
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct._option_positions[PUT].qty == -1 and acct._option_positions[long_leg].qty == 1
        acct._option_positions[long_leg].basis_factor = 1.0      # corrupt ONE leg's basis
        ps.set_clock(_dt(EXPIRY))
        with pytest.raises(SplitBasisRefused, match="different share bases"):
            engine._apply_option_expiry(_dt(EXPIRY))


def test_an_underlying_with_no_bar_on_the_expiry_date_settles_on_the_forward_filled_close():
    """(f) The underlying has no bar ON the expiry date: settle on its last close (09-17:
    95 adjusted = 380 in the lot's basis, P410 intrinsic 30) instead of skipping the expiry."""
    bars = _pre_split_bars(PUT, {OPEN_DAY: 15.0, LAST_PRE: 12.0})
    closes = [(d, c) for d, c in _closes(overrides={date(2020, 9, 17): 95.0}) if d != EXPIRY]
    with _harness(bars, closes) as (engine, acct, ps):
        _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.BUY, "long_put")
        cash_after_entry = acct._cash
        _expire(engine, acct, ps)
        assert _expiry_close(acct, PUT).open_price == pytest.approx(30.0)
        assert acct._cash == pytest.approx(cash_after_entry + 30.0 * 100)


def test_the_round_trip_row_carries_the_entry_basis_and_the_refinement_uses_it(monkeypatch):
    """Review item 4: the row publishes the basis its entry traded in, and the intraday
    refinement prices the entry close and every 5m bar with THAT one factor, even when the
    row's entry date is in another basis."""
    import pandas as pd
    import app.services.backtest.intraday_drawdown as I
    import app.services.backtest.results as R

    bars = _pre_split_bars(PUT, {OPEN_DAY: 15.0, LAST_PRE: 12.0})
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, PUT, OptionRight.PUT, 410.0, EXPIRY, OrderDirection.BUY, "long_put")
        ps.set_clock(_dt(date(2020, 9, 1)))
        (row,) = [t for t in acct.get_round_trip_trades() if t.get("contract_symbol") == PUT]
        assert row["option_basis_factor"] == 4.0
        assert R._trade_row(row)["option_basis_factor"] == 4.0

        df = pd.DataFrame({"Date": [pd.Timestamp("2020-09-01 10:00")],
                           "Low": [105.0], "High": [111.0]})
        monkeypatch.setattr(R, "_get_5m_bars_cached", lambda *a, **k: df)
        seen = {}

        def spy(trades, max_dd, **kw):
            t = trades[0]
            seen["entry"] = kw["underlying_price_at"]("AAPL", t["entry_time"])
            seen["bars"] = kw["bars_5m_between"]("AAPL", t["entry_time"], t["exit_time"])
            return max_dd

        monkeypatch.setattr(I, "refine_max_drawdown", spy)
        fn = R._build_refine_drawdown_fn(
            acct, {"account_settings": {"commission_per_trade": 0.0},
                   "start_date": date(2020, 8, 17), "end_date": date(2020, 9, 25)})
        # An entry DATED on the ex-date (factor 1) whose recorded basis is 4: the basis wins.
        fn([{"contract_symbol": PUT, "underlying_symbol": "AAPL",
             "entry_time": "2020-08-31T00:00:00", "exit_time": "2020-09-02T00:00:00",
             "option_basis_factor": 4.0}], 0.1)
        assert seen["entry"] == pytest.approx(110.0 * 4)
        assert seen["bars"] == [{"Low": pytest.approx(420.0), "High": pytest.approx(444.0)}]
