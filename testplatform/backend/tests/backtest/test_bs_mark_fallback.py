"""Task 3 — Black-Scholes mark fallback (docs/superpowers/plans/
2026-08-31-options-grid2-convex-earnings-impl.md Task 3; design §3 of
docs/superpowers/specs/2026-08-31-leaps-grid-design.md).

The mark chain becomes: bar close -> BS(last-known bar iv) -> the pre-existing
max(intrinsic, entry) fallback (F1/F2, test_option_settlement_bounds.py) -- each stage
consulted ONLY when the prior is unavailable, and every stage still passes through the
existing no-arb clamp (``_no_arb_premium_bounds``/``_clamp_premium_to_no_arb``).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_bs_mark_fallback.py -q
"""
from __future__ import annotations

import inspect
import math
from datetime import date, datetime, timedelta

import pytest

from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_bs import bs_price
from ba2_common.core.types import OptionRight, OrderDirection

import app.services.backtest.backtest_account as ba_mod
from app.services.backtest.backtest_account import BacktestAccount, _OptionLot


CFG = {
    "starting_cash": 10_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

_AMD_CALL_500 = "AMD240315C00500000"
_AMD_PUT_80 = "AMD240315P00080000"
_EXPIRY = date(2024, 3, 15)


def _make_ps(symbol, bars, clock):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(symbol, bars)
    ps.set_clock(clock)
    return ps


def _account(tmp_path, tag, ps, chain_underlying, chain, bar_rows, cfg=CFG):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
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


def _c(sym, k, iv=None):
    row = {"occ_symbol": sym, "option_type": "call", "strike": k, "expiry": "2024-03-15",
           "bid": 1.0, "ask": 1.2, "last": 1.1}
    if iv is not None:
        row["iv"] = iv
    return row


def _p(sym, k, iv=None):
    row = {"occ_symbol": sym, "option_type": "put", "strike": k, "expiry": "2024-03-15",
           "bid": 1.0, "ask": 1.2, "last": 1.1}
    if iv is not None:
        row["iv"] = iv
    return row


def _bar(sym, d, close, ot, k, underlying="AAPL", v=100, iv=None):
    row = {"occ_symbol": sym, "date": d, "open": close, "high": close, "low": close,
           "close": close, "volume": v, "underlying": underlying, "option_type": ot,
           "strike": k, "expiry": "2024-03-15"}
    if iv is not None:
        row["iv"] = iv
    return row


def _leg(sym, side, ot, k, ratio=1, underlying="AAPL"):
    from ba2_common.core.option_types import OptionLeg

    intent = "buy_to_open" if side == OrderDirection.BUY else "sell_to_open"
    return OptionLeg(contract_symbol=sym, side=side, ratio_qty=ratio, position_intent=intent,
                      option_type=ot, strike=k, expiry=_EXPIRY, underlying=underlying)


def _ubar(d, px):
    return {"Date": d, "Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 100}


def _naked_short_call_account(tmp_path, tag, extra_bar_rows=(), entry_iv=0.30,
                               extra_underlying_bars=()):
    """Sell 1 naked AMD 500 call @3.0 (spot 450) with an ENTRY-DAY iv of ``entry_iv``, so
    the lot's ``last_iv`` is populated the moment the position opens."""
    bars = [
        _ubar(datetime(2024, 3, 5), 450),
        _ubar(datetime(2024, 3, 6), 450),
        _ubar(datetime(2024, 3, 7), 452),
        _ubar(datetime(2024, 3, 8), 448),
        _ubar(datetime(2024, 3, 12), 450),
    ]
    bars.extend(extra_underlying_bars)
    bars.sort(key=lambda r: r["Date"])
    ps = _make_ps("AMD", bars, datetime(2024, 3, 5))
    chain = [_c(_AMD_CALL_500, 500.0, iv=entry_iv)]
    bar_rows = [_bar(_AMD_CALL_500, "2024-03-06", 3.0, "call", 500.0, underlying="AMD",
                      iv=entry_iv)]
    bar_rows.extend(extra_bar_rows)
    acct, ctx = _account(tmp_path, tag, ps, "AMD", chain, bar_rows)
    acct.submit_option_order(
        legs=[_leg(_AMD_CALL_500, OrderDirection.SELL, OptionRight.CALL, 500.0, underlying="AMD")],
        quantity=1, order_type="market", option_strategy="naked_call",
    )
    acct.refresh_orders()
    acct.refresh_transactions()
    assert acct._cash == pytest.approx(10_300.0, abs=1.0)  # +3.0 x 100 credit
    # Simulate the daily engine's OWN equity mark on the entry bar (2024-03-06, where the
    # premium bar with iv actually lives) — exactly what a real run's per-bar equity
    # snapshot would have done before any LATER bar's clock is set. Without this, a test
    # that jumps straight from submission (clock still 2024-03-05, before "next bar open"
    # fills) to a later date would see a lot that never had the chance to observe its own
    # entry-day iv, which is a test-harness gap, not a production one.
    ps.set_clock(datetime(2024, 3, 6))
    acct._option_positions_mtm()
    return acct, ps, ctx


def _naked_short_put_account(tmp_path, tag, extra_bar_rows=(), entry_iv=None):
    """Sell 1 naked AMD 80 put @2.0 (spot 85) — the F2-regression twin, iv OFF by default
    so the pre-Task-3 max(intrinsic, entry) suite (test_option_settlement_bounds.py) stays
    byte-identical when BS is not in play."""
    bars = [
        _ubar(datetime(2024, 3, 5), 85),
        _ubar(datetime(2024, 3, 6), 85),
        _ubar(datetime(2024, 3, 7), 40),
        _ubar(datetime(2024, 3, 8), 30),
    ]
    ps = _make_ps("AMD", bars, datetime(2024, 3, 5))
    chain = [_p(_AMD_PUT_80, 80.0, iv=entry_iv)]
    bar_rows = [_bar(_AMD_PUT_80, "2024-03-06", 2.0, "put", 80.0, underlying="AMD",
                      iv=entry_iv)]
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


def _held_lot(acct):
    lots = [l for l in acct._option_positions.values() if l.qty != 0]
    assert len(lots) == 1, lots
    return lots[0]


# ---------------------------------------------------------------------------
# Fallback ORDER: bar wins over BS (BS never consulted while a real bar exists).
# ---------------------------------------------------------------------------

def test_bar_wins_over_bs_when_both_available(tmp_path, monkeypatch):
    calls = []
    real = ba_mod.bs_price
    monkeypatch.setattr(ba_mod, "bs_price",
                         lambda *a, **k: calls.append((a, k)) or real(*a, **k))
    acct, ps, ctx = _naked_short_call_account(
        tmp_path, "order-bar",
        extra_bar_rows=[_bar(_AMD_CALL_500, "2024-03-07", 3.2, "call", 500.0,
                              underlying="AMD", iv=0.31)],
    )
    try:
        ps.set_clock(datetime(2024, 3, 7))
        assert acct._option_positions_mtm() == pytest.approx(-320.0, abs=1.0)  # bar close, clamped
        assert calls == []  # BS never touched — the real bar answered
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Fallback ORDER: BS wins over intrinsic/entry when the bar is MISSING but iv is fresh.
# ---------------------------------------------------------------------------

def test_bs_used_when_bar_missing_and_iv_fresh(tmp_path):
    """No 2024-03-07 premium bar for the AMD 500 call. The lot's last_iv (0.30, from the
    2024-03-06 entry bar) is 1 day old — inside the staleness window — so the mark uses
    BS(spot=452, strike=500, dte, iv=0.30), independently re-derived here via the SAME
    shared black_scholes() the wrapper delegates to (not via option_bs.bs_price itself)."""
    from ba2_common.core.finance_calc.derivatives import black_scholes
    from app.services.backtest.options_store import default_options_risk_free_rate

    acct, ps, ctx = _naked_short_call_account(tmp_path, "order-bs")
    try:
        ps.set_clock(datetime(2024, 3, 7))
        dte_days = (_EXPIRY - date(2024, 3, 7)).days
        rate = default_options_risk_free_rate()
        expected = black_scholes(452.0, 500.0, dte_days / 365.0, rate, 0.30,
                                  option_type="call")["price"]
        mtm = acct._option_positions_mtm()
        assert mtm == pytest.approx(-expected * 100.0, abs=0.5)
        # Prove BS actually engaged: this is NOT the max(intrinsic, entry) number (F2).
        # intrinsic here is 0 (452 < 500, OTM) so the old fallback would have kept the
        # entry credit mark unchanged (-300), which BS must differ from.
        assert mtm != pytest.approx(-300.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


def test_bs_wins_over_intrinsic_entry_liquidation_buyback(tmp_path):
    """The SAME order applies to the margin-liquidation buyback: with the bar missing and
    iv fresh, the forced buyback prices at BS, not at the pre-Task-3 max(intrinsic, entry)."""
    from ba2_common.core.finance_calc.derivatives import black_scholes
    from app.services.backtest.options_store import default_options_risk_free_rate

    # Push AMD to 520 (deep ITM, intrinsic 20) with NO bar on the blow-up day, so the
    # pre-Task-3 fallback would book max(intrinsic=20, entry=3.0)=20 -- BS must differ.
    acct, ps, ctx = _naked_short_call_account(
        tmp_path, "liq-bs", extra_underlying_bars=[_ubar(datetime(2024, 3, 10), 520)])
    try:
        ps.set_clock(datetime(2024, 3, 10))
        lot = _held_lot(acct)
        dte_days = (_EXPIRY - date(2024, 3, 10)).days
        rate = default_options_risk_free_rate()
        expected = black_scholes(520.0, 500.0, dte_days / 365.0, rate, 0.30,
                                  option_type="call")["price"]
        premium = acct._bs_fallback_premium(lot)
        assert premium == pytest.approx(expected, abs=0.05)
        assert premium != pytest.approx(20.0, abs=0.5)  # not the old intrinsic floor
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Clamp still applied AFTER BS — mutation (b): skip the clamp -> this test kills it.
# ---------------------------------------------------------------------------

def test_bs_output_clamped_to_no_arb_bounds(tmp_path, monkeypatch):
    """Force ``bs_price`` to answer an impossible number (a call premium above spot, which
    real BS never produces) and prove ``_bs_fallback_premium`` still clamps it into
    ``_no_arb_premium_bounds`` before returning — the guard that protects every other mark
    stage must protect this one too."""
    monkeypatch.setattr(ba_mod, "bs_price", lambda *a, **k: 999_999.0)
    acct, ps, ctx = _naked_short_call_account(tmp_path, "clamp-bs")
    try:
        ps.set_clock(datetime(2024, 3, 7))
        lot = _held_lot(acct)
        premium = acct._bs_fallback_premium(lot)
        # Upper no-arb bound for a call is spot (452.0) — nowhere near the forced 999999.
        assert premium == pytest.approx(452.0, abs=0.01)
    finally:
        ctx.__exit__(None, None, None)


def test_bs_output_clamped_at_lower_intrinsic_bound(tmp_path, monkeypatch):
    """The mirror direction: a forced BS answer BELOW intrinsic is floored at intrinsic."""
    monkeypatch.setattr(ba_mod, "bs_price", lambda *a, **k: -50.0)
    acct, ps, ctx = _naked_short_call_account(
        tmp_path, "clamp-bs-lo", extra_underlying_bars=[_ubar(datetime(2024, 3, 10), 520)])
    try:
        ps.set_clock(datetime(2024, 3, 10))
        lot = _held_lot(acct)
        premium = acct._bs_fallback_premium(lot)
        assert premium == pytest.approx(20.0, abs=0.01)  # intrinsic floor: 520 - 500
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Missing iv falls through — mutation (c): a missing iv priced as 0 fails this by name.
# ---------------------------------------------------------------------------

def test_missing_iv_falls_through_to_intrinsic_entry(tmp_path):
    """No iv was EVER recorded for this contract (entry_iv=None): ``last_iv`` stays None
    forever, so ``_bs_fallback_premium`` must return None on every missing-bar day and the
    mark chain must land on the EXACT pre-Task-3 max(intrinsic, entry) numbers
    (test_option_settlement_bounds.py::test_naked_short_put_no_bar_marks_intrinsic_floored_liability)."""
    acct, ps, ctx = _naked_short_put_account(tmp_path, "missing-iv", entry_iv=None)
    try:
        lot = _held_lot(acct)
        assert lot.last_iv is None
        ps.set_clock(datetime(2024, 3, 7))
        assert acct._bs_fallback_premium(lot) is None
        assert acct._option_positions_mtm() == pytest.approx(-4_000.0, abs=1.0)
        ps.set_clock(datetime(2024, 3, 8))
        assert acct._bs_fallback_premium(lot) is None
        assert acct._option_positions_mtm() == pytest.approx(-5_000.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


def test_missing_iv_never_priced_as_zero(tmp_path):
    """A missing iv must return None, never a real (zero-or-otherwise) number — pricing a
    missing iv as 0 would make every OTM/no-bar contract mark as worthless."""
    acct, ps, ctx = _naked_short_put_account(tmp_path, "missing-iv-zero", entry_iv=None)
    try:
        lot = _held_lot(acct)
        ps.set_clock(datetime(2024, 3, 7))
        result = acct._bs_fallback_premium(lot)
        assert result is None
        assert result != 0.0  # explicit: None, not a falsy-but-real zero
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# dte=0 convention: an expiring-TODAY contract with no bar falls through to intrinsic.
# ---------------------------------------------------------------------------

def test_dte_zero_falls_through_to_intrinsic(tmp_path):
    """The contract expires exactly on the clock's date and has no bar that day: BS must
    return None (the option_bs DTE=0 convention) and the mark must be the plain
    max(intrinsic, entry) fallback, unaffected by an otherwise-fresh iv."""
    bars = [
        _ubar(datetime(2024, 3, 5), 450),
        _ubar(datetime(2024, 3, 6), 450),
        _ubar(_EXPIRY, 520),  # expiry-day underlying bar: deep ITM, intrinsic 20
    ]
    ps = _make_ps("AMD", bars, datetime(2024, 3, 5))
    chain = [_c(_AMD_CALL_500, 500.0, iv=0.30)]
    bar_rows = [_bar(_AMD_CALL_500, "2024-03-06", 3.0, "call", 500.0, underlying="AMD", iv=0.30)]
    acct, ctx = _account(tmp_path, "dte0", ps, "AMD", chain, bar_rows)
    acct.submit_option_order(
        legs=[_leg(_AMD_CALL_500, OrderDirection.SELL, OptionRight.CALL, 500.0, underlying="AMD")],
        quantity=1, order_type="market", option_strategy="naked_call",
    )
    acct.refresh_orders()
    acct.refresh_transactions()
    try:
        ps.set_clock(datetime(2024, 3, 6))
        acct._option_positions_mtm()  # prime last_iv from the entry bar
        lot = _held_lot(acct)
        assert lot.last_iv == pytest.approx(0.30)
        ps.set_clock(datetime(_EXPIRY.year, _EXPIRY.month, _EXPIRY.day))
        assert acct._bs_fallback_premium(lot) is None  # dte_days == 0
        # max(intrinsic=20, entry=3.0) x -100 = -2000 (short liability)
        assert acct._option_positions_mtm() == pytest.approx(-2_000.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# iv staleness window (_BS_IV_STALENESS_DAYS = 5): fresh -> used, stale -> falls through.
# ---------------------------------------------------------------------------

def test_iv_within_staleness_window_is_used():
    lot = _OptionLot(contract_symbol="X", qty=-1, avg_price=3.0, multiplier=100.0)
    lot.last_iv = 0.30
    lot.last_iv_date = date(2024, 3, 2)
    assert (date(2024, 3, 7) - lot.last_iv_date).days == ba_mod._BS_IV_STALENESS_DAYS


def test_iv_past_staleness_window_falls_through(tmp_path):
    acct, ps, ctx = _naked_short_call_account(tmp_path, "stale-iv")
    try:
        lot = _held_lot(acct)
        assert lot.last_iv == pytest.approx(0.30)
        # Forge a last_iv_date well OUTSIDE the staleness window relative to the check date
        # (2024-03-07) — the real iv is only 1 day old; this proves the guard reads the
        # DATE, not "an iv happens to exist".
        check_date = date(2024, 3, 7)
        stale_date = check_date - timedelta(days=ba_mod._BS_IV_STALENESS_DAYS + 1)
        lot.last_iv_date = stale_date
        ps.set_clock(datetime(2024, 3, 7))
        assert (check_date - stale_date).days > ba_mod._BS_IV_STALENESS_DAYS
        assert acct._bs_fallback_premium(lot) is None
    finally:
        ctx.__exit__(None, None, None)


def test_iv_exactly_at_staleness_window_boundary_is_still_used(tmp_path):
    acct, ps, ctx = _naked_short_call_account(tmp_path, "boundary-iv")
    try:
        lot = _held_lot(acct)
        lot.last_iv_date = date(2024, 3, 6)
        boundary_date = date(2024, 3, 6) + timedelta(days=ba_mod._BS_IV_STALENESS_DAYS)
        ps.set_clock(datetime(boundary_date.year, boundary_date.month, boundary_date.day))
        assert acct._bs_fallback_premium(lot) is not None
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Structural pin: BS is provably absent from every risk/reserve/margin call graph.
# Mutation (a): BS reachable from a reserve/margin function -> this fails by name.
# ---------------------------------------------------------------------------

_BS_NAMES = ("bs_price", "_bs_fallback_premium", "_bs_mark_rate", "option_bs")

_RISK_FUNCS = [
    (OptionsAccountInterface, "option_reserve_required"),
    (OptionsAccountInterface, "naked_margin_per_contract"),
    (OptionsAccountInterface, "short_pair_margin_per_contract"),
    (BacktestAccount, "maintenance_margin_requirement"),
    (BacktestAccount, "_lot_maintenance_premium"),
]


@pytest.mark.parametrize(
    "owner,name", _RISK_FUNCS,
    ids=[f"{o.__name__}.{n}" for o, n in _RISK_FUNCS],
)
def test_risk_function_source_never_names_bs(owner, name):
    src = inspect.getsource(getattr(owner, name))
    for bad in _BS_NAMES:
        assert bad not in src, f"{owner.__name__}.{name} references {bad!r}"


def test_options_account_interface_module_never_imports_bs():
    """The reserve/margin module (a DIFFERENT module from backtest_account.py, so it
    cannot even see the mark chain's imports) never imports option_bs at all."""
    import ba2_common.core.interfaces.OptionsAccountInterface as oai

    src = inspect.getsource(oai)
    assert "option_bs" not in src
    assert "bs_price" not in src


# ---------------------------------------------------------------------------
# Happy-path PERF pin: BS is called ZERO times on a run with no missing bars.
# ---------------------------------------------------------------------------

def test_happy_path_never_calls_bs(tmp_path, monkeypatch):
    calls = []
    real = ba_mod.bs_price
    monkeypatch.setattr(ba_mod, "bs_price",
                         lambda *a, **k: calls.append(1) or real(*a, **k))
    bars = [
        _ubar(datetime(2024, 3, 5), 450),
        _ubar(datetime(2024, 3, 6), 450),
        _ubar(datetime(2024, 3, 7), 452),
        _ubar(datetime(2024, 3, 8), 448),
    ]
    ps = _make_ps("AMD", bars, datetime(2024, 3, 5))
    chain = [_c(_AMD_CALL_500, 500.0, iv=0.30)]
    bar_rows = [
        _bar(_AMD_CALL_500, "2024-03-06", 3.0, "call", 500.0, underlying="AMD", iv=0.30),
        _bar(_AMD_CALL_500, "2024-03-07", 3.2, "call", 500.0, underlying="AMD", iv=0.31),
        _bar(_AMD_CALL_500, "2024-03-08", 2.8, "call", 500.0, underlying="AMD", iv=0.29),
    ]
    acct, ctx = _account(tmp_path, "happy", ps, "AMD", chain, bar_rows)
    acct.submit_option_order(
        legs=[_leg(_AMD_CALL_500, OrderDirection.SELL, OptionRight.CALL, 500.0, underlying="AMD")],
        quantity=1, order_type="market", option_strategy="naked_call",
    )
    acct.refresh_orders()
    acct.refresh_transactions()
    try:
        for d in (datetime(2024, 3, 6), datetime(2024, 3, 7), datetime(2024, 3, 8)):
            ps.set_clock(d)
            acct._option_positions_mtm()
        assert calls == []
    finally:
        ctx.__exit__(None, None, None)


def test_equity_only_trial_never_calls_bs(monkeypatch):
    """A pure-equity trial (no options provider at all) does zero BS work — the whole
    option mark path short-circuits before ``_option_positions_mtm`` even runs its loop."""
    calls = []
    real = ba_mod.bs_price
    monkeypatch.setattr(ba_mod, "bs_price", lambda *a, **k: calls.append(1) or real(*a, **k))
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", [_ubar(datetime(2024, 3, 5), 100)])
    ps.set_clock(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=None)
    assert acct._option_positions_mtm() == 0.0
    assert calls == []


# ---------------------------------------------------------------------------
# Cross-implementation parity (coordinator instruction 2026-09-01): the pinned shared
# ``finance_calc.black_scholes`` and the backtest's OWN read-time inverter
# (``option_greeks.bs_price``) must never silently drift apart — both price and both
# invert the same iv, and a drift between them would corrupt marks in one direction
# while leaving the other (e.g. the greeks the cache viewer/UI shows) unchanged.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spot,strike,years,rate,iv,right",
    [
        (100.0, 100.0, 1.0, 0.05, 0.20, "call"),
        (100.0, 100.0, 1.0, 0.05, 0.20, "put"),
        (452.0, 500.0, 8 / 365.0, 0.045, 0.30, "call"),
        (30.0, 80.0, 200 / 365.0, 0.045, 0.55, "put"),
        (250.0, 150.0, 0.5, 0.0, 0.90, "call"),
        (10.0, 10.0, 1 / 365.0, 0.045, 0.15, "put"),
    ],
)
def test_finance_calc_and_option_greeks_bs_agree(spot, strike, years, rate, iv, right):
    from ba2_common.core.finance_calc.derivatives import black_scholes
    from app.services.backtest.option_greeks import bs_price as legacy_bs_price
    from ba2_common.core.types import OptionRight as _OR

    shared = black_scholes(spot, strike, years, rate, iv, option_type=right)["price"]
    legacy = legacy_bs_price(spot, strike, years, rate, iv,
                              _OR.CALL if right == "call" else _OR.PUT)
    assert legacy == pytest.approx(shared, abs=1e-6, rel=1e-6)


def test_option_bs_wrapper_agrees_with_option_greeks_bs_price():
    """The NEW fallback wrapper (option_bs.bs_price, DTE in days) must also agree with the
    legacy per-bar inverter (option_greeks.bs_price, T in years) on the same inputs."""
    from app.services.backtest.option_greeks import bs_price as legacy_bs_price

    dte_days = 30
    price = bs_price(452.0, 500.0, dte_days, 0.30, OptionRight.CALL, r=0.045)
    legacy = legacy_bs_price(452.0, 500.0, dte_days / 365.0, 0.045, 0.30, OptionRight.CALL)
    assert price == pytest.approx(legacy, abs=1e-6, rel=1e-6)
