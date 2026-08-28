"""Zero-coercion defects that corrupt backtest metrics (commission / multiplier / NaN).

Five families of "a missing value silently becomes a harmless-looking number" bugs, all of
which move the numbers the GA optimises against without moving any real capital:

  1. ``results._build_refine_drawdown_fn`` read ``config["commission_per_trade"]`` — a key that
     does NOT exist at that level (it lives under ``config["account_settings"]``) — behind an
     ``or 0.0``, so the intraday-drawdown refinement always priced its worst case with ZERO
     commission. Understated drawdown -> overstated calmar. FLATTERS.
  2. ``BacktestAccount.get_round_trip_trades`` charged a flat ``commission * 2`` per round-trip
     no matter how many fills it actually took, while the CASH ledger (``_apply_fill``) charges
     one commission PER FILL. Any scaled entry (rebalance ADD) or multi-fill exit was
     undercharged in the trade rows. FLATTERS.
  3. The OPTION branch of that same P&L line fell back to ``multiplier or 1`` while the cash
     ledger, the MTM equity curve and eight other call sites all fall back to ``or 100``. A
     NULL multiplier therefore books round-trip P&L 100x too small against an equity curve that
     valued it at 100x. (The ``else 1`` on the EQUITY branch is correct and must stay.)
  4. A non-finite (NaN/Inf) net-liquidating-value was recorded into the equity curve unchecked
     and then coerced away by ``_safe_float`` at the metric boundary — so a run that produced
     nonsense scored as a flawless flat run instead of being rejected.

All tests are hermetic: fixed simulated dates (never wall-clock "today"), no network, no real
cache files, no DB writes outside the per-test in-memory backtest DB.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_zero_coercion_defects.py -v
"""
from __future__ import annotations

import math
from datetime import datetime
from types import SimpleNamespace

import pytest

from tests.backtest.test_round_trip_trades import (
    D1,
    D2,
    D3,
    D4,
    _acct,
    _attach_order,
    _fill,
    _open_entry,
)

# A realistic flat per-fill options commission ($0.65/contract x 2 for a 2-lot, say).
COMMISSION = 1.30

CFG_COMM = {
    "starting_cash": 100_000.0,
    "commission_per_trade": COMMISSION,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}


# ===========================================================================
# Item 1 — results.py: commission read from the WRONG config level
# ===========================================================================
class _FakePrice:
    """Minimal AsOfPriceSource stand-in for the refinement wiring."""

    def bar_at(self, symbol, dt):
        return {"low": 95.0, "high": 105.0, "close": 100.0}

    def prev_bar(self, symbol, dt):
        return {"low": 99.0, "high": 105.0, "close": 100.0}

    def close_at(self, symbol, dt=None):
        return 100.0

    def now(self):
        return D4


class _RefineAccountStub:
    """Account stub carrying the private attrs ``_build_refine_drawdown_fn`` requires.

    ``_options`` is the REAL sqlite reader over the test's tiny cache, not a namespace faking
    its internals: the refinement's option seam is the named ``delta_at_entry`` method (see
    ``results._build_refine_drawdown_fn``), so a stub that only carried a ``.cache`` attribute
    would exercise a shape neither backend actually has.
    """

    _wiped_out = False

    def __init__(self, snaps, trades, cache_db_path):
        from app.services.backtest.options_provider import HistoricalOptionsProvider

        self._snaps = snaps
        self._trades = trades
        self._price = _FakePrice()
        self._options = HistoricalOptionsProvider(cache_db_path)

    def get_balance_history(self):
        return self._snaps

    def get_round_trip_trades(self):
        return self._trades

    def _equity_at(self, dt):
        return 20_000.0


def _snap(d, nlv):
    return {"date": d, "net_liquidating_value": nlv, "cash_balance": 0.0, "equity_value": nlv}


@pytest.fixture
def refine_cache(tmp_path):
    """A tiny options chain cache carrying a delta for the refinement's ``_delta_at_entry``."""
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import _WORKER_CHAIN_CACHE

    _WORKER_CHAIN_CACHE.clear()
    db = str(tmp_path / "refine_chain.sqlite")
    cache = OptionsHistoryCache(db)
    cache.write_chain_rows(
        "AAPL",
        "2024-01-01",
        [{"occ_symbol": "AAPL240315C00180000", "option_type": "call", "strike": 180.0,
          "expiry": "2024-03-15", "bid": 4.9, "ask": 5.1, "last": 5.0, "iv": 0.25,
          "delta": 0.5}],
    )
    yield db
    _WORKER_CHAIN_CACHE.clear()


@pytest.fixture
def fake_5m_provider(monkeypatch):
    """Replace ``ba2_providers.get_provider`` so no real FMP provider / cache is touched."""
    import pandas as pd
    import ba2_providers

    frame = pd.DataFrame(
        [
            {"Date": D2, "Low": 90.0, "High": 101.0},
            {"Date": D3, "Low": 92.0, "High": 100.0},
        ]
    )

    class _P:
        def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval=None):
            return frame

    monkeypatch.setattr(ba2_providers, "get_provider", lambda *a, **k: _P())
    from app.services.backtest.results import clear_worker_5m_bars_cache

    clear_worker_5m_bars_cache()
    yield
    clear_worker_5m_bars_cache()


def _refine_trade():
    return {
        "symbol": "AAPL",
        "contract_symbol": "AAPL240315C00180000",
        "underlying_symbol": "AAPL",
        "entry_time": D2,
        "exit_time": D3,
        "direction": "buy",
        "entry_price": 5.0,
        "exit_price": 6.0,
        "size": 1.0,
        "pnl": 100.0,
        "pnl_pct": 0.5,
        "bars_held": 1,
        "exit_reason": "exit",
    }


def _refine_config(commission):
    return {
        "initial_capital": 20_000.0,
        "start_date": D1,
        "end_date": D4,
        "account_settings": {
            "starting_cash": 20_000.0,
            "commission_per_trade": commission,
            "slippage_bps": 0.0,
            "fill_model": "next_bar_open",
        },
    }


def test_refinement_commission_comes_from_account_settings(monkeypatch, refine_cache):
    """The commission handed to ``refine_max_drawdown`` must be the run's real
    ``account_settings.commission_per_trade``, NOT 0.0.

    Pre-fix the code read ``config.get("commission_per_trade")`` — a key that only ever exists
    one level down, under ``account_settings`` (see ``daily_backtest_handler._build_config``) —
    so the ``or 0.0`` fired on EVERY run and the worst-case intraday P&L was priced free.
    """
    import ba2_providers
    from app.services.backtest import intraday_drawdown
    from app.services.backtest.results import _build_refine_drawdown_fn

    monkeypatch.setattr(ba2_providers, "get_provider",
                        lambda *a, **k: SimpleNamespace(get_ohlcv_data=lambda *a, **k: None))
    seen = {}

    def _spy(trades, max_drawdown, **kwargs):
        seen.update(kwargs)
        return max_drawdown

    monkeypatch.setattr(intraday_drawdown, "refine_max_drawdown", _spy)

    acct = _RefineAccountStub([_snap(D1, 20_000.0)], [], refine_cache)
    fn = _build_refine_drawdown_fn(acct, _refine_config(COMMISSION))
    assert fn is not None
    fn([], -2.0)
    assert seen["commission_per_trade"] == pytest.approx(COMMISSION)


def test_account_settings_present_but_commission_key_missing_still_raises(monkeypatch, refine_cache):
    """MUTATION KILLER: swapping the explicit index for
    ``config["account_settings"].get("commission_per_trade", 0.0)`` re-introduces exactly the
    silent zero this item is about, one level down, and the "wrong level" test would not see it."""
    import ba2_providers
    from app.services.backtest.results import _build_refine_drawdown_fn

    monkeypatch.setattr(ba2_providers, "get_provider",
                        lambda *a, **k: SimpleNamespace(get_ohlcv_data=lambda *a, **k: None))
    acct = _RefineAccountStub([_snap(D1, 20_000.0)], [], refine_cache)
    cfg = {"initial_capital": 20_000.0, "account_settings": {"starting_cash": 20_000.0}}
    with pytest.raises(KeyError):
        _build_refine_drawdown_fn(acct, cfg)


def test_missing_account_settings_raises_instead_of_defaulting_to_zero(monkeypatch, refine_cache):
    """House rule (backend/CLAUDE.md): config access is explicit — a missing commission key must
    raise, never silently price the run at zero cost."""
    import ba2_providers
    from app.services.backtest.results import _build_refine_drawdown_fn

    monkeypatch.setattr(ba2_providers, "get_provider",
                        lambda *a, **k: SimpleNamespace(get_ohlcv_data=lambda *a, **k: None))
    acct = _RefineAccountStub([_snap(D1, 20_000.0)], [], refine_cache)
    with pytest.raises(KeyError):
        _build_refine_drawdown_fn(acct, {"initial_capital": 20_000.0})


def test_commission_changes_the_drawdown_and_calmar_a_run_reports(refine_cache, fake_5m_provider):
    """SYNTHETIC BOOK. Two otherwise byte-identical runs that differ ONLY in
    ``account_settings.commission_per_trade`` must NOT produce identical risk metrics.

    Pre-fix they did — the commission never reached the refinement — which is what made the
    defect invisible: a costlier book scored exactly like a free one.
    """
    from app.services.backtest.results import build_results

    snaps = [_snap(D1, 20_000.0), _snap(D2, 19_600.0), _snap(D3, 20_100.0), _snap(D4, 20_100.0)]
    free = build_results(_RefineAccountStub(snaps, [_refine_trade()], refine_cache),
                         _refine_config(0.0))
    paid = build_results(_RefineAccountStub(snaps, [_refine_trade()], refine_cache),
                         _refine_config(COMMISSION))

    # Commission makes the modelled worst case WORSE -> drawdown more negative, calmar smaller.
    assert paid["max_drawdown"] < free["max_drawdown"]
    assert abs(paid["calmar_ratio"]) <= abs(free["calmar_ratio"])


# ===========================================================================
# Item 2 — backtest_account.py: commission undercounted on scaled entries
# ===========================================================================
def test_scaled_entry_charges_one_commission_per_fill():
    """3 fills (2 buys + 1 sell) must cost 3 commissions, not the flat 2 the recorder charged.

    The CASH ledger already charges one commission per fill (``_apply_fill``: ``self._cash -=
    commission``), so the flat ``commission * 2`` made the trade rows disagree with the equity
    curve for every rebalance ADD.
    """
    from ba2_common.core.types import OrderDirection

    acct, ctx, ps = _acct(account_id=201, cfg=CFG_COMM)
    try:
        buy1, txn = _open_entry(acct, qty=10, side=OrderDirection.BUY)
        _fill(acct, buy1, 100.0, D2)
        buy2 = _attach_order(acct, txn, OrderDirection.BUY, qty=10)
        _fill(acct, buy2, 110.0, D3)
        sell = _attach_order(acct, txn, OrderDirection.SELL, qty=20)
        _fill(acct, sell, 130.0, D4)

        t = acct.get_round_trip_trades()[0]
        gross = (130.0 - 105.0) * 20.0  # +500
        assert t["pnl"] == pytest.approx(gross - 3 * COMMISSION)
    finally:
        ctx.__exit__(None, None, None)


def test_multi_fill_exit_charges_one_commission_per_fill():
    """1 buy + 2 partial sells = 3 fills = 3 commissions."""
    from ba2_common.core.types import OrderDirection

    acct, ctx, ps = _acct(account_id=202, cfg=CFG_COMM)
    try:
        buy, txn = _open_entry(acct, qty=20, side=OrderDirection.BUY)
        _fill(acct, buy, 100.0, D2)
        s1 = _attach_order(acct, txn, OrderDirection.SELL, qty=10)
        _fill(acct, s1, 120.0, D3)
        s2 = _attach_order(acct, txn, OrderDirection.SELL, qty=10)
        _fill(acct, s2, 120.0, D4)

        t = acct.get_round_trip_trades()[0]
        assert t["pnl"] == pytest.approx((120.0 - 100.0) * 20.0 - 3 * COMMISSION)
    finally:
        ctx.__exit__(None, None, None)


def test_open_at_end_charges_one_commission_per_entry_fill():
    """A still-open scaled position has paid TWO entry commissions, not one."""
    from ba2_common.core.types import OrderDirection

    acct, ctx, ps = _acct(account_id=203, last_close=150.0, cfg=CFG_COMM)
    try:
        buy1, txn = _open_entry(acct, qty=10, side=OrderDirection.BUY)
        _fill(acct, buy1, 100.0, D2)
        buy2 = _attach_order(acct, txn, OrderDirection.BUY, qty=10)
        _fill(acct, buy2, 100.0, D3)
        ps.set_clock(D4)

        t = acct.get_round_trip_trades()[0]
        assert t["exit_reason"] == "open_at_end"
        assert t["pnl"] == pytest.approx((150.0 - 100.0) * 20.0 - 2 * COMMISSION)
    finally:
        ctx.__exit__(None, None, None)


def test_simple_two_fill_round_trip_commission_is_unchanged():
    """REGRESSION guard for the mutation "double-count instead of undercount": the dominant
    buy-once/sell-once case must still cost exactly 2 commissions."""
    from ba2_common.core.types import OrderDirection

    acct, ctx, ps = _acct(account_id=204, cfg=CFG_COMM)
    try:
        buy, txn = _open_entry(acct, qty=10, side=OrderDirection.BUY)
        _fill(acct, buy, 100.0, D2)
        sell = _attach_order(acct, txn, OrderDirection.SELL, qty=10)
        _fill(acct, sell, 120.0, D3)

        t = acct.get_round_trip_trades()[0]
        assert t["pnl"] == pytest.approx((120.0 - 100.0) * 10.0 - 2 * COMMISSION)
    finally:
        ctx.__exit__(None, None, None)


def test_round_trip_commission_matches_the_cash_ledger():
    """SYNTHETIC BOOK. The commission the trade rows report must equal the commission the CASH
    ledger actually charged over the same fills — that equality is the whole point."""
    from ba2_common.core.types import OrderDirection

    acct, ctx, ps = _acct(account_id=205, cfg=CFG_COMM)
    try:
        cash0 = acct.get_balance()
        buy1, txn = _open_entry(acct, qty=10, side=OrderDirection.BUY)
        _fill(acct, buy1, 100.0, D2)
        buy2 = _attach_order(acct, txn, OrderDirection.BUY, qty=10)
        _fill(acct, buy2, 100.0, D3)
        buy3 = _attach_order(acct, txn, OrderDirection.BUY, qty=10)
        _fill(acct, buy3, 100.0, D3)
        sell = _attach_order(acct, txn, OrderDirection.SELL, qty=30)
        _fill(acct, sell, 100.0, D4)

        # Flat prices -> the ONLY thing that moved cash is commission.
        ledger_commission = cash0 - acct.get_balance()
        t = acct.get_round_trip_trades()[0]
        reported_commission = -t["pnl"]  # gross is exactly 0 at flat prices
        assert ledger_commission == pytest.approx(4 * COMMISSION)
        assert reported_commission == pytest.approx(ledger_commission)
    finally:
        ctx.__exit__(None, None, None)


# ===========================================================================
# Item 3 — backtest_account.py: the option multiplier outlier (`or 1`)
# ===========================================================================
def _set_multiplier(acct, contract_symbol, value):
    """Force the multiplier on every option order carrying ``contract_symbol`` (simulates any
    write path that fails to stamp it — the DB column is ``int | None``, default None)."""
    from ba2_common.core.db import update_instance
    from ba2_common.core.types import AssetClass

    touched = 0
    for o in acct.get_orders():
        if getattr(o, "asset_class", None) == AssetClass.OPTION and o.contract_symbol == contract_symbol:
            o.multiplier = value
            update_instance(o)
            touched += 1
    acct.invalidate_order_cache()
    return touched


def test_option_round_trip_falls_back_to_100_not_1_when_multiplier_is_null(
    option_round_trip_account_comm,
):
    """A NULL multiplier on an OPTION round-trip must be valued at the standard 100, matching
    the cash ledger (``_apply_option_fill``: ``float(order.multiplier or 100)``) and the MTM
    equity curve. The ``or 1`` outlier booked it at 1x — a 100x understatement of the trade's
    P&L against an equity curve that had already moved by the full 100x amount."""
    acct = option_round_trip_account_comm
    assert _set_multiplier(acct, _OPT_OCC, None) >= 1

    rt = [t for t in acct.get_round_trip_trades() if t["exit_reason"] != "open_at_end"]
    assert rt, "expected a closed option round-trip"
    t = rt[0]
    gross = (1.5 - 1.0) * 1.0 * 100.0  # premium move x contracts x multiplier
    assert t["pnl"] == pytest.approx(gross - 2 * COMMISSION)
    assert t["pnl"] > 0, "a 0.50 premium gain on a 1-lot is a WINNER, not a $2.10 loser"


def test_option_round_trip_treats_a_zero_multiplier_as_100_too(option_round_trip_account_comm):
    """MUTATION KILLER / edge: the column is ``int | None`` but a 0 is just as falsy as a None,
    and ``or`` catches both. A zero multiplier must land on 100, not on 1 and not on 0."""
    acct = option_round_trip_account_comm
    assert _set_multiplier(acct, _OPT_OCC, 0) >= 1

    rt = [t for t in acct.get_round_trip_trades() if t["exit_reason"] != "open_at_end"]
    assert rt
    assert rt[0]["pnl"] == pytest.approx((1.5 - 1.0) * 100.0 - 2 * COMMISSION)


def test_equity_round_trip_is_never_scaled_by_a_contract_multiplier():
    """REGRESSION guard for the mutation "fix the multiplier in the wrong direction": equities
    have NO contract multiplier, so the ``else 1`` on the equity branch must stay 1."""
    from ba2_common.core.types import OrderDirection

    acct, ctx, ps = _acct(account_id=206, cfg=CFG_COMM)
    try:
        buy, txn = _open_entry(acct, qty=10, side=OrderDirection.BUY)
        _fill(acct, buy, 100.0, D2)
        sell = _attach_order(acct, txn, OrderDirection.SELL, qty=10)
        _fill(acct, sell, 120.0, D3)

        t = acct.get_round_trip_trades()[0]
        assert t["pnl"] == pytest.approx(200.0 - 2 * COMMISSION)  # NOT 20_000
    finally:
        ctx.__exit__(None, None, None)


# ===========================================================================
# Item 3 (siblings) — "capital deployed" reconstructed WITHOUT the multiplier
# ===========================================================================
# The round-trip row carried premium-per-share (``entry_price``) and CONTRACTS (``size``) but
# NOT the contract multiplier, so every downstream consumer that rebuilds "the capital deployed
# in this trade" as ``entry_price * size`` was 100x too small for an option. Two consumers:
#   * results._compute_metrics' per-trade PROFIT CAP  -> cap 100x too TIGHT  -> PENALISES;
#   * monte_carlo.apply_spread_cost's notional        -> cost 100x too SMALL -> FLATTERS.
def _opt_trades(n, premium, exit_premium, contracts, mult=100.0):
    return [{"symbol": "AAPL", "contract_symbol": f"X{i}", "underlying_symbol": "AAPL",
             "entry_time": D1, "exit_time": D2, "direction": "buy",
             "entry_price": premium, "exit_price": exit_premium, "size": contracts,
             "multiplier": mult,
             "pnl": (exit_premium - premium) * contracts * mult,
             "pnl_pct": (exit_premium - premium) * contracts * mult / 100_000.0 * 100.0,
             "bars_held": 5, "exit_reason": "exit"} for i in range(n)]


def test_round_trip_row_carries_the_contract_multiplier(option_round_trip_account_comm):
    """The row must publish the multiplier it used, so no consumer has to guess it."""
    acct = option_round_trip_account_comm
    rt = [t for t in acct.get_round_trip_trades() if t["exit_reason"] != "open_at_end"]
    assert rt and rt[0]["multiplier"] == pytest.approx(100.0)


def test_equity_round_trip_row_reports_multiplier_one():
    from ba2_common.core.types import OrderDirection

    acct, ctx, ps = _acct(account_id=211, cfg=CFG_COMM)
    try:
        buy, txn = _open_entry(acct, qty=10, side=OrderDirection.BUY)
        _fill(acct, buy, 100.0, D2)
        sell = _attach_order(acct, txn, OrderDirection.SELL, qty=10)
        _fill(acct, sell, 120.0, D3)
        assert acct.get_round_trip_trades()[0]["multiplier"] == pytest.approx(1.0)
    finally:
        ctx.__exit__(None, None, None)


def test_profit_cap_basis_is_capital_deployed_not_bare_premium():
    """SYNTHETIC BOOK. 20 option round-trips: 2 contracts at a 2.00 premium (= $400 of capital
    each) closed at 3.00 (= +$200, a 50% return on capital). At the launcher's DEFAULT
    ``--profit-cap-pct 2000`` nothing should be capped — 50% is nowhere near 2000%.

    Pre-fix the basis was ``2.00 * 2 = $4.00``, so the "2000%" cap was $80 against a $200 gain
    and 60% of the book's profit was deducted: adjusted_total_return 4.0% -> 1.6%,
    adjusted_calmar 3.99 -> 1.60. Every option trade returning more than 20% on capital was
    silently truncated. This one PENALISES.
    """
    from app.services.backtest.results import build_results

    trades = _opt_trades(20, 2.0, 3.0, 2.0)
    snaps = [_snap(D1, 100_000.0), _snap(D2, 104_000.0), _snap(D3, 104_000.0)]
    base = build_results(_PlainStub(snaps, trades), {"initial_capital": 100_000.0})
    capped = build_results(_PlainStub(snaps, trades),
                           {"initial_capital": 100_000.0, "profit_cap_pct": 2000.0})
    assert base["total_return"] == pytest.approx(4.0)
    assert capped["adjusted_total_return"] == pytest.approx(base["total_return"])
    assert capped["adjusted_calmar_ratio"] == pytest.approx(base["adjusted_calmar_ratio"])


def test_profit_cap_still_bites_when_the_gain_really_does_exceed_the_cap():
    """REGRESSION guard for the mutation "multiplier applied in the wrong direction": the cap
    must still fire on a genuine mega-winner. 1 contract, 1.00 premium ($100 deployed), closed
    at 26.00 (+$2,500 = 2500% of capital) with a 2000% cap -> capped at $2,000."""
    from app.services.backtest.results import build_results

    trades = _opt_trades(1, 1.0, 26.0, 1.0)
    snaps = [_snap(D1, 100_000.0), _snap(D2, 102_500.0), _snap(D3, 102_500.0)]
    capped = build_results(_PlainStub(snaps, trades),
                           {"initial_capital": 100_000.0, "profit_cap_pct": 2000.0})
    # excess = 2500 - 2000 = 500 deducted from final equity -> 102_000 -> +2.0%
    assert capped["adjusted_total_return"] == pytest.approx(2.0)


def test_profit_cap_basis_for_equities_is_unchanged():
    """LEGITIMATE: shares have no contract multiplier, so an equity row's basis stays
    ``entry_price * size`` (a missing/1 multiplier must be an exact no-op)."""
    from app.services.backtest.results import build_results

    trades = [{"symbol": "AAA", "entry_time": D1, "exit_time": D2, "direction": "buy",
               "entry_price": 10.0, "exit_price": 40.0, "size": 100.0, "pnl": 3_000.0,
               "pnl_pct": 3.0, "bars_held": 5, "exit_reason": "exit"}]
    snaps = [_snap(D1, 100_000.0), _snap(D2, 103_000.0), _snap(D3, 103_000.0)]
    capped = build_results(_PlainStub(snaps, trades),
                           {"initial_capital": 100_000.0, "profit_cap_pct": 100.0})
    # basis 10*100 = $1,000; cap 100% = $1,000; gain $3,000 -> excess $2,000 deducted.
    assert capped["adjusted_total_return"] == pytest.approx(1.0)


def test_spread_stress_notional_uses_capital_deployed_for_options():
    """The spread-stress robustness haircut charges ``notional * bps * 2``. With the notional
    100x too small the modelled cost was 100x too small — a genome whose edge barely clears the
    real spread sailed through the stress test. This one FLATTERS."""
    from app.services.backtest.monte_carlo import apply_spread_cost

    trade = _opt_trades(1, 2.0, 3.0, 2.0)[0]
    out = apply_spread_cost([trade], initial=100_000.0, spread_bps=50.0)
    # notional = 2.00 x 2 contracts x 100 = $400; round-trip cost = 400 * 0.005 * 2 = $4.00
    # -> 4.00 / 100_000 * 100 = 0.004 pct points deducted.
    assert out[0] == pytest.approx(trade["pnl_pct"] - 0.004)


def test_spread_stress_notional_unchanged_for_equities():
    from app.services.backtest.monte_carlo import apply_spread_cost

    trade = {"entry_price": 10.0, "size": 100.0, "pnl_pct": 1.0}
    out = apply_spread_cost([trade], initial=100_000.0, spread_bps=50.0)
    # notional = $1,000; cost = 1000 * 0.005 * 2 = $10 -> 0.01 pct points.
    assert out[0] == pytest.approx(1.0 - 0.01)


# ===========================================================================
# Item 4 — a NaN run must be rejected, not scored as flawless
# ===========================================================================
def test_snapshot_equity_rejects_non_finite_nlv():
    """A NaN net-liquidating-value is nonsense, not "flat" — recording it lets the run finish
    and score, since ``_safe_float`` later swaps every NaN point for the initial capital."""
    acct, ctx, ps = _acct(account_id=207, cfg=CFG_COMM)
    try:
        acct._open_positions_mtm = lambda: float("nan")
        with pytest.raises(ValueError):
            acct.snapshot_equity(D2)
    finally:
        ctx.__exit__(None, None, None)


def test_snapshot_equity_rejects_infinite_nlv():
    acct, ctx, ps = _acct(account_id=208, cfg=CFG_COMM)
    try:
        acct._open_positions_mtm = lambda: float("inf")
        with pytest.raises(ValueError):
            acct.snapshot_equity(D2)
    finally:
        ctx.__exit__(None, None, None)


def test_snapshot_equity_still_accepts_a_legitimate_zero_and_negative():
    """LEGITIMATE ZERO guard: a wiped-out account (nlv <= 0) is a real, meaningful state and must
    keep clamping to 0.0 + setting ``_wiped_out``, never raise."""
    acct, ctx, ps = _acct(account_id=209, cfg=CFG_COMM)
    try:
        acct._cash = 0.0
        acct._open_positions_mtm = lambda: 0.0
        snap = acct.snapshot_equity(D2)
        assert snap["net_liquidating_value"] == 0.0
        assert acct._wiped_out is True

        acct._cash = -500.0
        snap = acct.snapshot_equity(D3)
        assert snap["net_liquidating_value"] == 0.0
    finally:
        ctx.__exit__(None, None, None)


def test_snapshot_equity_accepts_an_ordinary_positive_value():
    acct, ctx, ps = _acct(account_id=210, cfg=CFG_COMM)
    try:
        snap = acct.snapshot_equity(D2)
        assert snap["net_liquidating_value"] == pytest.approx(100_000.0)
        assert acct._wiped_out is False
    finally:
        ctx.__exit__(None, None, None)


class _PlainStub:
    _wiped_out = False

    def __init__(self, snaps, trades):
        self._snaps = snaps
        self._trades = trades

    def get_balance_history(self):
        return self._snaps

    def get_filled_trades(self):
        return self._trades


def test_build_results_rejects_a_nan_equity_point():
    """A NaN in the equity curve was silently replaced by ``initial`` — turning a broken run
    into a flat, riskless one (drawdown 0, calmar = annualised_return). Reject it."""
    snaps = [_snap(D1, 100_000.0), _snap(D2, float("nan")), _snap(D3, 105_000.0)]
    with pytest.raises(ValueError):
        _build(snaps, [])


def test_build_results_rejects_a_nan_trade_pnl():
    """A NaN trade P&L became 0.0 — an invented scratch trade that quietly improves win_rate's
    denominator, profit_factor and SQN."""
    trades = [{"symbol": "AAA", "side": "buy", "pnl": float("nan"), "pnl_pct": 1.0,
               "bars_held": 1, "date": D1, "price": 10.0, "qty": 5}]
    with pytest.raises(ValueError):
        _build([_snap(D1, 100_000.0), _snap(D2, 100_500.0)], trades)


def test_build_results_rejects_a_nan_trade_price():
    trades = [{"symbol": "AAA", "side": "buy", "pnl": 100.0, "pnl_pct": 1.0,
               "bars_held": 1, "date": D1, "price": float("nan"), "qty": 5}]
    with pytest.raises(ValueError, match="entry_price"):
        _build([_snap(D1, 100_000.0), _snap(D2, 100_500.0)], trades)


@pytest.mark.parametrize(
    "field, key",
    [("trade.size", "qty"), ("trade.pnl_pct", "pnl_pct"), ("trade.exit_price", "exit_price")],
)
def test_build_results_rejects_a_nan_in_any_trade_money_field(field, key):
    """Every money/price/quantity field on the row gets the same guard — a NaN size silently
    became 0.0 (a zero-quantity "trade"), which also zeroes the profit cap's cost basis and so
    exempts that trade from the per-trade cap entirely."""
    trade = {"symbol": "AAA", "side": "buy", "pnl": 100.0, "pnl_pct": 1.0,
             "bars_held": 1, "date": D1, "price": 10.0, "qty": 5, "exit_price": 12.0}
    trade[key] = float("nan")
    with pytest.raises(ValueError, match=field.replace(".", r"\.")):
        _build([_snap(D1, 100_000.0), _snap(D2, 100_500.0)], [trade])


def test_build_results_accepts_a_legitimate_zero_pnl_scratch_trade():
    """LEGITIMATE ZERO guard: a genuine scratch trade (pnl exactly 0.0) is real and must pass."""
    trades = [{"symbol": "AAA", "side": "buy", "pnl": 0.0, "pnl_pct": 0.0,
               "bars_held": 1, "date": D1, "price": 10.0, "qty": 5}]
    r = _build([_snap(D1, 100_000.0), _snap(D2, 100_000.0)], trades)
    assert r["total_trades"] == 1
    assert r["trades"][0]["pnl"] == 0.0


def test_build_results_accepts_legitimate_zero_and_negative_equity():
    """LEGITIMATE ZERO guard: a wiped-out curve (equity 0.0) and a losing book must still be
    scored — the isfinite guard must not reject them."""
    r = _build([_snap(D1, 100_000.0), _snap(D2, 40_000.0), _snap(D3, 0.0)], [])
    assert r["final_equity"] == pytest.approx(0.0)
    assert r["total_return"] == pytest.approx(-100.0)
    assert r["max_drawdown"] < 0


def test_build_results_accepts_a_legitimate_negative_pnl():
    trades = [{"symbol": "AAA", "side": "buy", "pnl": -250.0, "pnl_pct": -2.5,
               "bars_held": 1, "date": D1, "price": 10.0, "qty": 5}]
    r = _build([_snap(D1, 100_000.0), _snap(D2, 99_750.0)], trades)
    assert r["trades"][0]["pnl"] == pytest.approx(-250.0)
    assert r["losing_trades"] == 1


def test_nan_run_does_not_score_as_flawless():
    """The headline: pre-fix a NaN-corrupted curve produced a clean metric blob (every value
    finite, zero drawdown) that the GA would happily rank. It must fail loudly instead."""
    snaps = [_snap(D1, 100_000.0), _snap(D2, float("nan")), _snap(D3, float("nan")),
             _snap(D4, float("nan"))]
    with pytest.raises(ValueError):
        _build(snaps, [])


def _build(snaps, trades):
    from app.services.backtest.results import build_results

    return build_results(_PlainStub(snaps, trades), {"initial_capital": 100_000.0})


# --- the METRIC boundary specifically (not the input boundaries) ------------
# ``_compute_metrics`` is called directly by whatif.recompute_curves as well as by
# build_results, and a curve whose points are individually FINITE can still overflow the
# risk-metric arithmetic (a ratio of ~1e400 between two adjacent points saturates to inf,
# and inf - inf in the variance sum is NaN). Pre-fix ``_safe_float`` mapped that NaN Sharpe
# and inf volatility to 0.0 -- "no risk at all" -- which is precisely the shape of a
# flawless-looking result nobody re-reads.
_EXTREME_CURVE = [
    {"date": D1, "equity": 1e-200},
    {"date": D2, "equity": 1e200},
    {"date": D3, "equity": 1e-200},
]
_FLAT_DD = [{"date": p["date"], "drawdown": 0.0} for p in _EXTREME_CURVE]


def test_metric_boundary_rejects_a_metric_that_overflowed_to_nan():
    from app.services.backtest.results import _compute_metrics

    # Name the metric, not just "something raised": with only a generic match, reverting ONE
    # metric line is masked by the next one that is also non-finite, and the mutation survives.
    with pytest.raises(ValueError, match="sharpe_ratio is not finite"):
        _compute_metrics(_EXTREME_CURVE, _FLAT_DD, [], 1e-200, 1e-200, {})


def test_metric_boundary_would_otherwise_have_reported_zero_risk():
    """Documents WHAT the coercion produced, so the fix is not just 'it raises now':
    ``_safe_float`` turns the same NaN Sharpe / inf volatility into 0.0 and 0.0."""
    from app.services.backtest.metrics_utils import _safe_float
    from app.services.backtest.results import _sharpe, _std, _step_returns

    equities = [p["equity"] for p in _EXTREME_CURVE]
    steps = _step_returns(equities)
    sharpe = _sharpe(steps, 252.0)
    vol = _std(steps) * (252.0 ** 0.5) * 100.0
    assert not math.isfinite(sharpe) and not math.isfinite(vol)
    assert _safe_float(sharpe) == 0.0      # "zero risk-adjusted return"
    assert _safe_float(vol) == 0.0         # "zero volatility"


def test_metric_boundary_passes_an_ordinary_curve():
    from app.services.backtest.results import _compute_metrics

    curve = [{"date": D1, "equity": 100_000.0}, {"date": D2, "equity": 90_000.0},
             {"date": D3, "equity": 105_000.0}]
    dd = [{"date": D1, "drawdown": 0.0}, {"date": D2, "drawdown": -10.0},
          {"date": D3, "drawdown": 0.0}]
    m = _compute_metrics(curve, dd, [], 100_000.0, 105_000.0, {})
    assert math.isfinite(m["sharpe_ratio"])
    assert m["max_drawdown"] == pytest.approx(-10.0)


def test_a_nan_drawdown_point_is_rejected_rather_than_silently_skipped():
    """A NaN drawdown never becomes a NaN metric — ``min()`` and ``< 0`` both quietly IGNORE
    it — so the trough simply vanishes and max_drawdown is UNDERSTATED. Rejecting the point at
    the curve boundary is the only place this is visible."""
    from app.services.backtest.results import _compute_metrics

    dd_with_nan = [{"date": D1, "drawdown": 0.0}, {"date": D2, "drawdown": float("nan")},
                   {"date": D3, "drawdown": -1.0}]
    curve = [{"date": D1, "equity": 100_000.0}, {"date": D2, "equity": 50_000.0},
             {"date": D3, "equity": 99_000.0}]
    silently_ignored = _compute_metrics(curve, dd_with_nan, [], 100_000.0, 99_000.0, {})
    assert silently_ignored["max_drawdown"] == pytest.approx(-1.0)  # the -50% trough vanished

    # build_results computes the drawdown curve itself and now rejects the NaN at source.
    with pytest.raises(ValueError):
        _build([_snap(D1, 100_000.0), _snap(D2, float("nan")), _snap(D3, 99_000.0)], [])


# ---------------------------------------------------------------------------
# Shared option fixture with a NON-zero commission (the round-trip module's own
# fixture uses commission 0.0, which cannot distinguish the commission defects).
# ---------------------------------------------------------------------------
_OPT_OCC = "AAPL240315C00180000"


@pytest.fixture
def option_round_trip_account_comm(tmp_path):
    """1 call bought @1.00 (D2 open) and sold @1.50 (D3 open) through the real fill engine,
    with a non-zero per-fill commission."""
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OrderDirection
    from tests.backtest.test_round_trip_trades import _OPT_UNDERLYING, _seed_option_cache

    cache_db = str(tmp_path / "opt_comm_cache.sqlite")
    _seed_option_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("opt-zero-coercion")
    ctx.__enter__()
    seed_account_definition(1, CFG_COMM)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _OPT_UNDERLYING)
    ps.set_clock(D1)
    acct = BacktestAccount(1, ps, CFG_COMM, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)
    try:
        leg = OptionLeg(contract_symbol=_OPT_OCC, side=OrderDirection.BUY,
                        position_intent="buy_to_open", underlying="AAPL")
        parent = acct.submit_option_order(legs=[leg], quantity=1, order_type="market",
                                          option_strategy="long_call")
        open_txn = acct.get_order(parent.id).transaction_id
        acct.refresh_orders()
        acct.refresh_transactions()

        close_leg = OptionLeg(contract_symbol=_OPT_OCC, side=OrderDirection.SELL,
                              position_intent="sell_to_close", underlying="AAPL")
        acct.submit_option_order(legs=[close_leg], quantity=1, order_type="market",
                                 option_strategy="close", transaction_id=open_txn)
        ps.set_clock(D2)
        acct.refresh_orders()
        acct.refresh_transactions()
        yield acct
    finally:
        ctx.__exit__(None, None, None)


def test_option_entry_split_across_two_fills_charges_three_commissions(tmp_path):
    """A THIN-VOLUME contract is the realistic >2-fill option case: the entry cannot be absorbed
    in one print, so it arrives as two buy-to-open fills on one transaction and closes in one.
    Three fills = three commissions, and the P&L still scales by the 100x multiplier.

    Pre-fix this trade reported the flat two commissions AND (with a NULL multiplier) 1x — the
    two defects compound on exactly the contracts the option grid trades most.
    """
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OrderDirection
    from tests.backtest.test_round_trip_trades import _OPT_UNDERLYING, _seed_option_cache

    cache_db = str(tmp_path / "opt_split_cache.sqlite")
    _seed_option_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("opt-split-fill")
    ctx.__enter__()
    try:
        seed_account_definition(1, CFG_COMM)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", _OPT_UNDERLYING)
        ps.set_clock(D1)
        acct = BacktestAccount(1, ps, CFG_COMM, options_provider=provider)
        wire_backtest_seams().register_account(1, acct)

        def _leg(side, intent):
            return OptionLeg(contract_symbol=_OPT_OCC, side=side, position_intent=intent,
                             underlying="AAPL")

        first = acct.submit_option_order(legs=[_leg(OrderDirection.BUY, "buy_to_open")],
                                         quantity=1, order_type="market",
                                         option_strategy="long_call")
        txn = acct.get_order(first.id).transaction_id
        # SECOND slice of the same entry, same transaction (the split fill).
        acct.submit_option_order(legs=[_leg(OrderDirection.BUY, "buy_to_open")], quantity=1,
                                 order_type="market", option_strategy="long_call",
                                 transaction_id=txn)
        acct.refresh_orders()
        acct.refresh_transactions()

        acct.submit_option_order(legs=[_leg(OrderDirection.SELL, "sell_to_close")], quantity=2,
                                 order_type="market", option_strategy="close",
                                 transaction_id=txn)
        ps.set_clock(D2)
        acct.refresh_orders()
        acct.refresh_transactions()

        rt = [t for t in acct.get_round_trip_trades() if t["exit_reason"] != "open_at_end"]
        assert len(rt) == 1
        t = rt[0]
        assert t["size"] == pytest.approx(2.0)
        assert t["pnl"] == pytest.approx((1.5 - 1.0) * 2.0 * 100.0 - 3 * COMMISSION)
    finally:
        ctx.__exit__(None, None, None)


def test_option_round_trip_with_a_real_multiplier_is_unaffected(option_round_trip_account_comm):
    """Sanity/regression: the ordinary multiplier=100 case is unchanged by the fix."""
    acct = option_round_trip_account_comm
    rt = [t for t in acct.get_round_trip_trades() if t["exit_reason"] != "open_at_end"]
    assert rt
    assert rt[0]["pnl"] == pytest.approx((1.5 - 1.0) * 100.0 - 2 * COMMISSION)


# ===========================================================================
# Item 5 (sibling) — whatif.py fabricated a $10,000 capital base
# ===========================================================================
def _whatif_args(initial_capital):
    return {
        "initial_capital": initial_capital,
        "trades": [{"symbol": "AAA", "entry_time": D1.isoformat(), "exit_time": D2.isoformat(),
                    "direction": "buy", "entry_price": 10.0, "size": 5.0, "pnl": 100.0,
                    "pnl_pct": 1.0, "bars_held": 1}],
        "equity_curve": [{"date": D1.isoformat(), "equity": 100_000.0},
                         {"date": D2.isoformat(), "equity": 100_100.0},
                         {"date": D3.isoformat(), "equity": 100_100.0}],
    }


@pytest.mark.parametrize("bad_capital", [None, 0.0])
def test_whatif_refuses_to_invent_a_capital_base(bad_capital):
    """``float(initial_capital or 0.0) or 10000.0`` silently substituted a $10,000 book for a
    missing/zero one, so EVERY percentage the what-if returned (total_return, annualized_return,
    calmar) was computed against a capital base the run never had. Money fields get no defaults."""
    from app.services.backtest.whatif import recompute_curves

    with pytest.raises(ValueError):
        recompute_curves(**_whatif_args(bad_capital))


def test_whatif_still_works_with_a_real_capital_base():
    from app.services.backtest.whatif import recompute_curves

    res = recompute_curves(**_whatif_args(100_000.0))
    assert res["final_equity"] == pytest.approx(100_100.0)


def test_metric_blob_is_finite_for_a_healthy_run():
    """The isfinite guard must not make a normal run raise."""
    r = _build([_snap(D1, 100_000.0), _snap(D2, 101_000.0), _snap(D3, 100_500.0)],
               [{"symbol": "AAA", "side": "buy", "pnl": 500.0, "pnl_pct": 0.5, "bars_held": 1,
                 "date": D1, "price": 10.0, "qty": 5}])
    for k, v in r.items():
        if isinstance(v, float):
            assert math.isfinite(v), f"{k} is not finite: {v}"
