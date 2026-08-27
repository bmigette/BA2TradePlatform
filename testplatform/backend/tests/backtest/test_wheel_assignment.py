"""The wheel needs assigned stock to SURVIVE the bar after assignment (plan Task 10).

Why this file exists
--------------------
``BacktestAccount`` physically assigns every ITM short option and then schedules the
resulting stock for a full liquidation at the NEXT bar's open
(``process_pending_assignment_liquidations`` — the "no orphaned stock" policy). That is
correct for every option strategy whose exits are all ``close_option``: stock nobody
manages would otherwise ride unmanaged to the end of the run (the OS1 blow-up).

The wheel is the first strategy for which it is wrong, and the ORDER makes it worse than
inert::

    daily_engine step 3      (~:718)  _manage_open_positions -> cc_sell sees
                                      has_assigned_shares and writes a covered call
    daily_engine step 4a-pre (~:740)  process_pending_assignment_liquidations -> "closes
                                      ALL of it at the NEXT bar's OPEN" (its own docstring)

The call is written and the shares backing it are sold on the SAME bar, so every wheel
position the engine opens is a NAKED SHORT CALL wearing a wheel's name.

The fix is a per-run switch, ``hold_assigned_stock``, DEFAULT OFF. Off, everything below
behaves exactly as it always has (the control test here, and the pinning test in
``test_option_orphan_stock_and_arb_guards.py``). On, the newly-opened assignment lot is
not scheduled for liquidation and the shares are still there on the next bar for the
overlay to write a call against.

WHAT CLOSES THE HELD SHARES (traced, not assumed): the covered call being ASSIGNED ITM at
its own expiry — ``_book_assignment_share_leg``'s ``closing`` branch books the delivery
against the held lot and schedules nothing. Pinned by
``test_the_only_exit_is_the_covered_call_being_assigned`` below. If the call keeps
expiring worthless the shares are never sold by any rule in the wheel's exit list (they
are all ``close_option``) and there is no end-of-run flatten — see that test's docstring.

Run from the repo root::

    PYTHONPATH=packages/common:packages/providers:packages/experts \\
      ./venv/bin/python -m pytest testplatform/backend/tests/backtest/test_wheel_assignment.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import (
    AssetClass,
    OptionRight,
    OrderDirection,
    TransactionStatus,
    TXN_ORIGIN_CSP_ASSIGNMENT,
)


# Zero commission/slippage so every cash assertion below is exact.
_BASE_CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

_PUT180 = "AAPL240308P00180000"      # the cash-secured put — expires ITM on 2024-03-08
_CALL185 = "AAPL240328C00185000"     # the covered call written over the assigned shares
_CALL170 = "AAPL240328C00170000"     # an ITM-at-expiry call (the wheel's actual exit)
_EXP_PUT = date(2024, 3, 8)
_EXP_CALL = date(2024, 3, 28)

# 2024-03-08 closes 164 < 180 -> the put is ITM -> 100 shares delivered at 180.
# 2024-03-11 is the bar on which the old policy sold them (open 165).
# 2024-03-12 is the covered call's fill bar (open 166).
# 2024-03-28 closes 190 -> the 170 call is ITM (called away), the 185 call is not.
_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 150, "High": 152, "Low": 149, "Close": 151, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 160, "High": 162, "Low": 159, "Close": 161, "Volume": 1100},
    {"Date": datetime(2024, 3, 7), "Open": 161, "High": 163, "Low": 160, "Close": 162, "Volume": 1200},
    {"Date": datetime(2024, 3, 8), "Open": 163, "High": 165, "Low": 162, "Close": 164, "Volume": 1300},
    {"Date": datetime(2024, 3, 11), "Open": 165, "High": 167, "Low": 164, "Close": 166, "Volume": 1400},
    {"Date": datetime(2024, 3, 12), "Open": 166, "High": 168, "Low": 165, "Close": 167, "Volume": 1500},
    {"Date": datetime(2024, 3, 13), "Open": 167, "High": 169, "Low": 166, "Close": 168, "Volume": 1600},
    {"Date": datetime(2024, 3, 28), "Open": 189, "High": 191, "Low": 188, "Close": 190, "Volume": 1700},
    {"Date": datetime(2024, 4, 1), "Open": 191, "High": 193, "Low": 190, "Close": 192, "Volume": 1800},
]


def _chain_row(occ, ot, strike, expiry):
    return {"occ_symbol": occ, "option_type": ot, "strike": strike,
            "expiry": expiry.isoformat(), "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _bar_row(occ, d, px, ot, strike, expiry):
    return {"occ_symbol": occ, "date": d, "open": px, "high": px, "low": px,
            "close": px, "volume": 100, "underlying": "AAPL",
            "option_type": ot, "strike": strike, "expiry": expiry.isoformat()}


def _build(tmp_path, name, account_id, cfg):
    """A BacktestAccount over a seeded temp options cache + the AAPL bar series."""
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource

    cache_db = str(tmp_path / f"{name}_options_cache.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows("AAPL", "2024-03-01", [
        _chain_row(_PUT180, "put", 180.0, _EXP_PUT),
        _chain_row(_CALL185, "call", 185.0, _EXP_CALL),
        _chain_row(_CALL170, "call", 170.0, _EXP_CALL),
    ])
    cache.write_bar_rows([
        _bar_row(_PUT180, "2024-03-06", 21.0, "put", 180.0, _EXP_PUT),
        _bar_row(_CALL185, "2024-03-12", 3.0, "call", 185.0, _EXP_CALL),
        _bar_row(_CALL170, "2024-03-12", 6.0, "call", 170.0, _EXP_CALL),
    ])
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db(name)
    ctx.__enter__()
    seed_account_definition(account_id, cfg)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(datetime(2024, 3, 5))
    acct = BacktestAccount(account_id, ps, cfg, options_provider=provider)
    wire_backtest_seams().register_account(account_id, acct)
    return acct, ps, ctx


def _engine(acct, ps, cfg):
    """A minimal engine bound to the account so ``_apply_option_expiry`` can be driven."""
    from app.services.backtest.daily_engine import DailyBacktestEngine

    eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
    eng.account = acct
    eng.price = ps
    eng.config = cfg
    return eng


def _leg(occ, side, intent, ot, strike, expiry):
    from ba2_common.core.option_types import OptionLeg

    return OptionLeg(contract_symbol=occ, side=side, position_intent=intent,
                     option_type=ot, strike=strike, expiry=expiry, underlying="AAPL")


def _sell_the_put(acct):
    acct.submit_option_order(
        legs=[_leg(_PUT180, OrderDirection.SELL, "sell_to_open",
                   OptionRight.PUT, 180.0, _EXP_PUT)],
        quantity=1, order_type="market", option_strategy="naked_put")
    acct.refresh_orders()
    acct.refresh_transactions()


def _write_the_call(acct, occ=_CALL185, strike=185.0):
    acct.submit_option_order(
        legs=[_leg(occ, OrderDirection.SELL, "sell_to_open",
                   OptionRight.CALL, strike, _EXP_CALL)],
        quantity=1, order_type="market", option_strategy="covered_call")
    acct.refresh_orders()
    acct.refresh_transactions()


def _shares(acct, symbol="AAPL"):
    rows = [p for p in acct.get_positions() if p["symbol"] == symbol]
    return float(rows[0]["qty"]) if rows else 0.0


def _open_equity_txns(acct, symbol="AAPL"):
    from ba2_common.core.trade_store import transactions_where

    return [t for t in transactions_where(status=TransactionStatus.OPENED, symbol=symbol)
            if t.asset_class != AssetClass.OPTION]


def _assign_the_put(acct, ps, cfg):
    """Drive the put to its ITM expiry; returns with the shares in the book."""
    ps.set_clock(datetime(2024, 3, 8))
    _engine(acct, ps, cfg)._apply_option_expiry(datetime(2024, 3, 8))
    acct.refresh_transactions()


# ---------------------------------------------------------------------------
# The plan's named test
# ---------------------------------------------------------------------------
def test_assigned_shares_survive_to_the_next_bar_so_a_call_can_be_written(tmp_path):
    """Short ITM put assigns 100 shares. They must still be held next bar.

    With ``hold_assigned_stock`` on, the next bar's step 4a-pre must be a no-op and the
    shares must still be there — held, booked, and recognised as cover for the call the
    overlay writes against them.
    """
    cfg = {**_BASE_CFG, "hold_assigned_stock": True}
    acct, ps, ctx = _build(tmp_path, "wheelhold", 71, cfg)
    try:
        _sell_the_put(acct)
        assert acct._cash == pytest.approx(102_100.0)          # 100k + 21.00 x 100

        _assign_the_put(acct, ps, cfg)
        assert _shares(acct) == pytest.approx(100.0)
        assert acct._cash == pytest.approx(84_100.0)           # 102,100 - 100 x 180
        assert acct._pending_assignment_sells == {}, (
            "hold_assigned_stock is on: the assignment must schedule NO liquidation, or "
            "the covered call written on the next bar is naked by the end of that bar"
        )

        # --- the next bar: the engine's step 4a-pre runs, and must do nothing ----------
        ps.set_clock(datetime(2024, 3, 11))
        assert acct.process_pending_assignment_liquidations() is False
        assert _shares(acct) == pytest.approx(100.0), \
            "the assigned shares were sold at the next bar's open — the wheel has no stock"
        assert acct._cash == pytest.approx(84_100.0)
        assert [o for o in acct.get_orders() if o.comment == "assignment_liquidation"] == []

        # ...and they are BOOKED, which is what has_assigned_shares reads (the wheel's
        # cc_sell gate) and what the overlay's share sizer counts.
        opened = _open_equity_txns(acct)
        assert len(opened) == 1
        assert opened[0].side == OrderDirection.BUY
        assert float(opened[0].quantity) == pytest.approx(100.0)
        assert float(opened[0].open_price) == pytest.approx(180.0)   # the STRIKE
        assert (opened[0].meta_data or {}).get("origin") == TXN_ORIGIN_CSP_ASSIGNMENT

        # --- and a call written against them is genuinely COVERED ---------------------
        _write_the_call(acct)
        assert _shares(acct) == pytest.approx(100.0), \
            "the shares must survive writing the call, not be sold out from under it"
        assert acct._cash == pytest.approx(84_400.0)           # + 3.00 x 100 credit
        assert _CALL185 in acct._covered_short_call_contracts(), (
            "the short call is not recognised as covered by the assigned shares — it is "
            "a naked short call, which is a different strategy"
        )
    finally:
        ctx.__exit__(None, None, None)


def test_the_held_lot_is_visible_to_the_EXPERT_that_wrote_the_put(tmp_path):
    """The link between "shares are held" and "the wheel can see them", pinned separately.

    Holding is useless if the manage pass cannot find the lot. Both readers key on the
    expert: ``DailyBacktestEngine._held_transactions`` selects
    ``transactions_where(expert_id=...)`` and ``HasAssignedSharesCondition`` queries
    ``open_transactions(expert_id=self.expert_recommendation.instance_id, ...)`` for the
    ``csp_assignment`` origin. So the assignment lot MUST inherit the option transaction's
    ``expert_id`` — if it came out None the shares would be held and invisible, and cc_sell
    would never fire.
    """
    from ba2_common.core.db import update_instance
    from ba2_common.core.trade_store import transactions_where

    cfg = {**_BASE_CFG, "hold_assigned_stock": True}
    acct, ps, ctx = _build(tmp_path, "wheelexpertid", 77, cfg)
    try:
        _sell_the_put(acct)
        # The engine's option-entry path stamps expert_id from the recommendation
        # (AccountInterface._create_transaction_for_order); this harness submits directly,
        # so pin it here and assert the assignment CARRIES IT FORWARD.
        opt_txn = [t for t in transactions_where() if t.asset_class == AssetClass.OPTION][0]
        opt_txn.expert_id = 909
        update_instance(opt_txn)

        _assign_the_put(acct, ps, cfg)
        ps.set_clock(datetime(2024, 3, 11))
        acct.process_pending_assignment_liquidations()

        held = [t for t in transactions_where(expert_id=909,
                                              status=TransactionStatus.OPENED)
                if t.asset_class != AssetClass.OPTION]
        assert len(held) == 1, (
            "the assigned lot is invisible to the expert that wrote the put — "
            "_held_transactions selects by expert_id, so the wheel's manage pass never "
            "sees the shares and cc_sell never fires"
        )
        assert (held[0].meta_data or {}).get("origin") == TXN_ORIGIN_CSP_ASSIGNMENT
    finally:
        ctx.__exit__(None, None, None)


def test_default_run_still_liquidates_the_same_assignment(tmp_path):
    """The control: the SAME book without the switch behaves exactly as it always has.

    Same fixture, same bars, same order flow — only ``hold_assigned_stock`` differs. This
    is what makes the test above evidence about the switch rather than about the fixture.
    """
    cfg = dict(_BASE_CFG)                                       # no hold_assigned_stock key
    acct, ps, ctx = _build(tmp_path, "wheeldefault", 72, cfg)
    try:
        _sell_the_put(acct)
        _assign_the_put(acct, ps, cfg)
        assert _shares(acct) == pytest.approx(100.0)
        assert acct._pending_assignment_sells == {"AAPL": 100.0}

        ps.set_clock(datetime(2024, 3, 11))
        assert acct.process_pending_assignment_liquidations() is True
        assert _shares(acct) == pytest.approx(0.0)
        assert acct._cash == pytest.approx(84_100.0 + 100.0 * 165.0)   # next bar's OPEN
        closes = [o for o in acct.get_orders() if o.comment == "assignment_liquidation"]
        assert len(closes) == 1 and closes[0].side == OrderDirection.SELL
    finally:
        ctx.__exit__(None, None, None)


def test_hold_assigned_stock_false_is_explicitly_the_old_behaviour(tmp_path):
    """An EXPLICIT False must behave identically to the key being absent."""
    cfg = {**_BASE_CFG, "hold_assigned_stock": False}
    acct, ps, ctx = _build(tmp_path, "wheelexplicitoff", 73, cfg)
    try:
        _sell_the_put(acct)
        _assign_the_put(acct, ps, cfg)
        assert acct._pending_assignment_sells == {"AAPL": 100.0}
        ps.set_clock(datetime(2024, 3, 11))
        assert acct.process_pending_assignment_liquidations() is True
        assert _shares(acct) == pytest.approx(0.0)
    finally:
        ctx.__exit__(None, None, None)


def test_held_shares_are_never_rescheduled_on_a_later_bar(tmp_path):
    """Holding is not "deferred by one bar": nothing re-arms the liquidation later."""
    cfg = {**_BASE_CFG, "hold_assigned_stock": True}
    acct, ps, ctx = _build(tmp_path, "wheelnoresched", 74, cfg)
    try:
        _sell_the_put(acct)
        _assign_the_put(acct, ps, cfg)
        for d in (datetime(2024, 3, 11), datetime(2024, 3, 12), datetime(2024, 3, 13),
                  datetime(2024, 3, 28), datetime(2024, 4, 1)):
            ps.set_clock(d)
            assert acct.process_pending_assignment_liquidations() is False
            assert acct._pending_assignment_sells == {}
        assert _shares(acct) == pytest.approx(100.0)
    finally:
        ctx.__exit__(None, None, None)


def test_the_only_exit_is_the_covered_call_being_assigned(tmp_path):
    """The wheel's exit, traced: the shares leave ONLY when the call finishes ITM.

    An ITM short call at expiry delivers the held shares at the strike —
    ``_book_assignment_share_leg`` books that as a CLOSING fill on the assigned lot's own
    transaction and schedules nothing (there is no new orphan). That is the whole exit
    path; the sibling test below covers the call finishing OTM instead.
    """
    cfg = {**_BASE_CFG, "hold_assigned_stock": True}
    acct, ps, ctx = _build(tmp_path, "wheelcalledaway", 75, cfg)
    try:
        _sell_the_put(acct)
        _assign_the_put(acct, ps, cfg)
        ps.set_clock(datetime(2024, 3, 11))
        acct.process_pending_assignment_liquidations()
        _write_the_call(acct, occ=_CALL170, strike=170.0)
        assert acct._cash == pytest.approx(84_100.0 + 600.0)

        # 2024-03-28 closes 190 > 170 -> the call is assigned, the shares are called away.
        ps.set_clock(datetime(2024, 3, 28))
        _engine(acct, ps, cfg)._apply_option_expiry(datetime(2024, 3, 28))
        acct.refresh_transactions()

        assert _shares(acct) == pytest.approx(0.0), \
            "the assigned call must deliver the held shares — that IS the wheel's exit"
        assert acct._cash == pytest.approx(84_700.0 + 100.0 * 170.0)
        assert acct._pending_assignment_sells == {}, \
            "a delivered lot is not orphaned stock; scheduling it would sell an unrelated lot"
        assert _open_equity_txns(acct) == [], \
            "the called-away lot must stop counting as held (the phantom-share source)"
    finally:
        ctx.__exit__(None, None, None)


def test_a_worthless_call_leaves_the_shares_held_with_no_exit(tmp_path):
    """The accumulation case, pinned as the KNOWN limitation it is.

    A covered call that finishes OTM resolves worthless and leaves the stock exactly where
    it was. Nothing else can sell it: every rule in the wheel's exit list is a
    ``close_option`` (``opt_tp``/``opt_time``/``opt_dte``/``opt_sl``), the covered-call
    guard halts the chain while a call is open, ``maybe_margin_call_liquidation`` only
    unwinds SHORT positions, and ``DailyBacktestEngine.run`` has no end-of-run flatten. So
    the shares ride to the end of the run and are reported ``open_at_end``, marked to
    market. That is the documented cost of ``hold_assigned_stock`` — the wheel's only real
    exit is the call being ASSIGNED (the test above).
    """
    cfg = {**_BASE_CFG, "hold_assigned_stock": True}
    acct, ps, ctx = _build(tmp_path, "wheelworthless", 76, cfg)
    try:
        _sell_the_put(acct)
        _assign_the_put(acct, ps, cfg)
        ps.set_clock(datetime(2024, 3, 11))
        acct.process_pending_assignment_liquidations()
        _write_the_call(acct)                                   # 185 call, credit 3.00

        # Settle the 185 call OTM by driving expiry against a spot BELOW the strike: the
        # 2024-03-13 close is 168 < 185. (settle_single_leg_expiry keys off the spot it is
        # handed, so this is the worthless branch exactly.)
        ps.set_clock(datetime(2024, 3, 13))
        lot = [p for p in acct.get_option_positions() if p.contract_symbol == _CALL185][0]
        assert acct.settle_single_leg_expiry(lot, 168.0) is True
        acct.refresh_transactions()

        assert acct.get_option_positions() == []                # the call is gone
        assert _shares(acct) == pytest.approx(100.0), \
            "a worthless call leaves the stock exactly where it was"
        assert acct._pending_assignment_sells == {}, \
            "nothing re-arms the liquidation once the switch is on"

        # No rule can sell them and there is no end-of-run flatten, so the lot is reported
        # open_at_end. This is the documented cost of the switch, not a passing detail.
        rows = [t for t in acct.get_round_trip_trades()
                if t["symbol"] == "AAPL" and t["contract_symbol"] is None]
        assert len(rows) == 1 and rows[0]["exit_reason"] == "open_at_end"
    finally:
        ctx.__exit__(None, None, None)
