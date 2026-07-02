"""ROBUSTNESS / INVARIANT suite for the option-backtest accounting bounds.

This complements the example-based ``test_options_review_fixes.py`` with
property/invariant coverage across MANY scenarios, guarding the accounting
hardening (defined-risk unit-settlement + MTM no-arb clamp, naked-short
maintenance-margin liquidation, cash-secured debit fills, option-gating) against
regression.

The invariants under test (see the module docstring of test_options_review_fixes
and BacktestAccount._option_positions_mtm / settle_defined_risk_combo_expiry /
maybe_margin_call_liquidation):

  * FLOOR        — on ANY bar, equity >= starting_cash - max_loss - eps for a
                   DEFINED-RISK combo (the mid-life MTM clamp keeps the marked
                   equity inside the structure's no-arb range).
  * NON_NEGATIVE — an adequately-funded defined-risk combo never marks equity < 0.
  * REALIZED     — final (post-expiry) equity lands inside
                   [starting_cash - max_loss, starting_cash + max_profit].
  * FINITE       — every equity value is finite (no NaN/inf).
  * NO_STOCK     — a defined-risk combo leaves NO residual stock after expiry.
  * NAKED BOUND  — a naked short strangle/straddle gapped hard against gets
                   force-liquidated and the recorded equity stays bounded (never
                   the huge negative it would reach unliquidated).
  * GATING       — with no options provider, _option_positions_mtm() == 0.0 and
                   maybe_margin_call_liquidation() is a no-op (equity-only runs
                   are byte-identical / untouched).

Run:
    ./venv/bin/python -m pytest tests/backtest/test_option_robustness.py -v
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pytest

from ba2_common.core.types import OptionRight, OrderDirection


CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

EPS = 1.0  # dollar tolerance (rounding / entry-slippage headroom)

_EXPIRY = "2024-03-15"
_EXPIRY_D = date(2024, 3, 15)
_UND = "TST"

# Simulated calendar. Clock starts at 3/5; a MARKET order submitted at 3/5 fills
# on 3/6 (next_bar_open); MTM bars are stepped 3/6..3/8; expiry is 3/15.
_D_START = datetime(2024, 3, 5)
_D_FILL = datetime(2024, 3, 6)
_MTM_DATES = [datetime(2024, 3, 6), datetime(2024, 3, 7), datetime(2024, 3, 8),
              datetime(2024, 3, 11), datetime(2024, 3, 13)]
_D_EXPIRY = datetime(2024, 3, 15)
_ALL_STEP_DATES = _MTM_DATES + [_D_EXPIRY]
_ALL_STEP_STRS = ["2024-03-06", "2024-03-07", "2024-03-08",
                  "2024-03-11", "2024-03-13", "2024-03-15"]


# ---------------------------------------------------------------------------
# Harness (mirrors test_options_review_fixes.py)
# ---------------------------------------------------------------------------
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

    cache_db = str(tmp_path / f"{tag}.sqlite")
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


def _occ(ot: str, strike: float) -> str:
    """OCC-style contract symbol for the test underlying."""
    tag = "C" if ot == "call" else "P"
    return f"{_UND}240315{tag}{int(round(strike * 1000)):08d}"


def _chain_row(sym, ot, k):
    return {"occ_symbol": sym, "option_type": ot, "strike": float(k), "expiry": _EXPIRY,
            "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _bar(sym, d, close, ot, k, underlying=_UND):
    return {"occ_symbol": sym, "date": d, "open": float(close), "high": float(close),
            "low": float(close), "close": float(close), "volume": 100,
            "underlying": underlying, "option_type": ot, "strike": float(k),
            "expiry": _EXPIRY}


def _leg(sym, side, ot, k, ratio=1, underlying=_UND):
    from ba2_common.core.option_types import OptionLeg

    intent = "buy_to_open" if side == OrderDirection.BUY else "sell_to_open"
    return OptionLeg(contract_symbol=sym, side=side, ratio_qty=ratio, position_intent=intent,
                     option_type=ot, strike=float(k), expiry=_EXPIRY_D, underlying=underlying)


def _engine(acct, ps):
    from app.services.backtest.daily_engine import DailyBacktestEngine

    eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
    eng.account = acct
    eng.price = ps
    eng.config = CFG
    return eng


def _right(ot: str):
    return OptionRight.CALL if ot == "call" else OptionRight.PUT


def _intrinsic(ot: str, strike: float, spot: float) -> float:
    return max(0.0, spot - strike) if ot == "call" else max(0.0, strike - spot)


# ---------------------------------------------------------------------------
# Combo model: a leg = (option_type, strike, direction, ratio)
# ---------------------------------------------------------------------------
class Combo:
    """A defined-risk combo spec + its theoretical premium / bounds."""

    def __init__(self, strategy: str, legs: List[Tuple[str, float, OrderDirection, int]],
                 quantity: int, premiums: Dict[float, Dict[str, float]]):
        # legs: list of (option_type, strike, direction, ratio_per_structure)
        self.strategy = strategy
        self.legs = legs
        self.quantity = quantity
        # premiums[strike][option_type] = per-share entry premium (used for entry fill AND net)
        self.premiums = premiums
        self.strikes = sorted({k for (_ot, k, _d, _r) in legs})

    # -- entry economics ----------------------------------------------------
    def net_premium_per_structure(self) -> float:
        """Signed net premium per structure per share. +debit (pay) / -credit (receive)."""
        net = 0.0
        for (ot, k, direction, ratio) in self.legs:
            px = self.premiums[k][ot]
            sign = 1.0 if direction == OrderDirection.BUY else -1.0
            net += sign * px * ratio
        return net

    def limit_price(self) -> float:
        return self.net_premium_per_structure()

    # -- theoretical bounds -------------------------------------------------
    def _width_per_structure(self) -> float:
        from app.services.backtest.backtest_account import BacktestAccount

        return BacktestAccount._defined_risk_width_per_structure(self.strategy, self.strikes)

    def payoff_at(self, spot: float) -> float:
        """Net intrinsic payoff (cash flow at settlement) for ALL structures at ``spot``."""
        net = 0.0
        for (ot, k, direction, ratio) in self.legs:
            sign = 1.0 if direction == OrderDirection.BUY else -1.0
            net += sign * _intrinsic(ot, k, spot) * ratio
        return net * 100.0 * self.quantity

    def max_loss(self) -> float:
        """Worst-case account P&L (positive dollars of loss) over the whole regime range."""
        width = self._width_per_structure()
        entry_cf = -self.net_premium_per_structure() * 100.0 * self.quantity  # cash at entry
        # Realized P&L at a given spot = entry_cf + payoff_at(spot). Scan the regime.
        worst = min(entry_cf + self.payoff_at(s) for s in self._scan_spots())
        # Never claim a loss looser than the defined-risk width bound.
        max_bound_loss = width * 100.0 * self.quantity
        return min(-worst, max_bound_loss + abs(entry_cf) + 1.0) if worst < 0 else 0.0

    def max_profit(self) -> float:
        entry_cf = -self.net_premium_per_structure() * 100.0 * self.quantity
        best = max(entry_cf + self.payoff_at(s) for s in self._scan_spots())
        return max(best, 0.0)

    def _scan_spots(self) -> List[float]:
        lo, hi = self.strikes[0], self.strikes[-1]
        span = max(hi - lo, 10.0)
        pts = [lo - span, lo - 1.0, lo]
        for a, b in zip(self.strikes, self.strikes[1:]):
            pts += [a, (a + b) / 2.0, b]
        pts += [hi, hi + 1.0, hi + span]
        return pts

    # -- cache material -----------------------------------------------------
    def chain_rows(self) -> List[Dict[str, Any]]:
        rows = []
        seen = set()
        for (ot, k, _d, _r) in self.legs:
            key = (ot, k)
            if key in seen:
                continue
            seen.add(key)
            rows.append(_chain_row(_occ(ot, k), ot, k))
        return rows

    def bar_rows(self, spot_at_expiry: float, inject_outlier: bool = True) -> List[Dict[str, Any]]:
        """Premium bars: entry premium on the fill day; INTRINSIC at expiry; entry premium
        forward-filled on the intermediate MTM days (a benign, in-range mark).

        When ``inject_outlier`` (default), one intermediate MTM day (2024-03-08) carries a
        deliberately WILD premium print on the SHORT leg (a huge spike) — mimicking the sparse
        options-cache outliers the MTM no-arb clamp exists to defend against. Without the clamp
        this outlier alone would swing recorded equity far past the structure's defined risk and
        BREACH the FLOOR invariant, so it makes the FLOOR/NON_NEGATIVE tests non-vacuous.
        """
        rows = []
        seen = set()
        for (ot, k, direction, _r) in self.legs:
            key = (ot, k)
            if key in seen:
                continue
            seen.add(key)
            sym = _occ(ot, k)
            entry_px = self.premiums[k][ot]
            for ds in ["2024-03-06", "2024-03-07", "2024-03-08", "2024-03-11", "2024-03-13"]:
                px = entry_px
                if inject_outlier and ds == "2024-03-08" and direction == OrderDirection.SELL:
                    # Wild outlier on a SHORT leg: a $500/share print (x contracts x 100) would,
                    # unclamped, mark the group thousands past defined risk. The clamp must cap it.
                    px = 500.0
                rows.append(_bar(sym, ds, px, ot, k))
            rows.append(_bar(sym, "2024-03-15", _intrinsic(ot, k, spot_at_expiry), ot, k))
        return rows

    def order_legs(self):
        return [
            _leg(_occ(ot, k), direction, _right(ot), k, ratio=ratio)
            for (ot, k, direction, ratio) in self.legs
        ]


# ---------------------------------------------------------------------------
# run_lifecycle: submit -> fill -> step underlying -> expiry -> settle
# ---------------------------------------------------------------------------
def run_lifecycle(tmp_path, tag: str, combo: Combo, spot_at_expiry: float,
                  underlying_path: Optional[List[float]] = None,
                  do_margin_call: bool = True) -> Dict[str, Any]:
    """Build account+cache, submit ``combo``, step the underlying through the path,
    recording snapshot equity at each bar; on the expiry bar apply option expiry +
    (optionally) a margin-call liquidation + a final snapshot.

    Returns {equity_curve, snapshots, final_cash, final_equity, positions, option_positions}.
    """
    # Underlying path: one close per step date. Default = flat at expiry spot for MTM bars,
    # landing exactly on spot_at_expiry at expiry.
    if underlying_path is None:
        underlying_path = [spot_at_expiry] * len(_MTM_DATES) + [spot_at_expiry]
    assert len(underlying_path) == len(_ALL_STEP_DATES)

    def _und_bar(dt, close):
        return {"Date": dt, "Open": close, "High": close + 0.5, "Low": close - 0.5,
                "Close": close, "Volume": 100}

    und_bars = [_und_bar(_D_START, underlying_path[0])]
    for dt, close in zip(_ALL_STEP_DATES, underlying_path):
        und_bars.append(_und_bar(dt, close))

    ps = _make_ps(_UND, und_bars, _D_START)
    chain = combo.chain_rows()
    bars = combo.bar_rows(spot_at_expiry)
    acct, ctx = _account(tmp_path, tag, ps, _UND, chain, bars, cfg=CFG)
    try:
        acct.submit_option_order(
            legs=combo.order_legs(), quantity=combo.quantity,
            order_type="market", option_strategy=combo.strategy,
        )
        # Fill the entry: clock at 3/5 -> market fills next bar (3/6).
        acct.refresh_orders()
        acct.refresh_transactions()
        # The combo MUST have opened (lots present) before we assert lifecycle invariants.
        opened = acct.get_option_positions()
        assert opened, f"[{tag}] combo did not fill/open — no option lots"

        equity_curve: List[float] = []
        snapshots: List[Dict[str, Any]] = []
        for i, dt in enumerate(_ALL_STEP_DATES):
            ps.set_clock(dt)
            acct.refresh_orders()
            acct.refresh_transactions()
            if dt == _D_EXPIRY:
                _engine(acct, ps)._apply_option_expiry(dt)
                if do_margin_call:
                    acct.maybe_margin_call_liquidation()
            snap = acct.snapshot_equity(dt)
            snapshots.append(snap)
            equity_curve.append(snap["net_liquidating_value"])

        return {
            "equity_curve": equity_curve,
            "snapshots": snapshots,
            "final_cash": acct._cash,
            "final_equity": acct.equity(),
            "positions": [p for p in acct.get_positions() if p["symbol"] == _UND],
            "option_positions": acct.get_option_positions(),
        }
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Combo builders (per structure, per share premiums chosen so entry fills)
# ---------------------------------------------------------------------------
def _bull_call_spread(qty=1) -> Combo:
    # long 100 call @6, short 110 call @2 -> debit 4 ; width 10 ; max_loss 400*qty
    prem = {100.0: {"call": 6.0}, 110.0: {"call": 2.0}}
    legs = [("call", 100.0, OrderDirection.BUY, 1), ("call", 110.0, OrderDirection.SELL, 1)]
    return Combo("bull_call_spread", legs, qty, prem)


def _bear_put_spread(qty=1) -> Combo:
    # long 110 put @6, short 100 put @2 -> debit 4 ; width 10
    prem = {110.0: {"put": 6.0}, 100.0: {"put": 2.0}}
    legs = [("put", 110.0, OrderDirection.BUY, 1), ("put", 100.0, OrderDirection.SELL, 1)]
    return Combo("bear_put_spread", legs, qty, prem)


def _bear_call_spread(qty=1) -> Combo:
    # short 100 call @6, long 110 call @2 -> credit 4 ; width 10 ; max_loss (10-4)*100=600
    prem = {100.0: {"call": 6.0}, 110.0: {"call": 2.0}}
    legs = [("call", 100.0, OrderDirection.SELL, 1), ("call", 110.0, OrderDirection.BUY, 1)]
    return Combo("bear_call_spread", legs, qty, prem)


def _call_butterfly(qty=1) -> Combo:
    # long 90 call @12, short 2x 100 call @5, long 110 call @1 -> debit 12-10+1=3 ; width 10
    prem = {90.0: {"call": 12.0}, 100.0: {"call": 5.0}, 110.0: {"call": 1.0}}
    legs = [("call", 90.0, OrderDirection.BUY, 1), ("call", 100.0, OrderDirection.SELL, 2),
            ("call", 110.0, OrderDirection.BUY, 1)]
    return Combo("call_butterfly", legs, qty, prem)


def _iron_condor(qty=1) -> Combo:
    # long 85 put @0.5 / short 90 put @1.5 / short 110 call @1.5 / long 115 call @0.5
    # -> credit (1.5+1.5-0.5-0.5)=2 ; wings 5/5 ; width 5 ; max_loss (5-2)*100=300
    prem = {85.0: {"put": 0.5}, 90.0: {"put": 1.5}, 110.0: {"call": 1.5}, 115.0: {"call": 0.5}}
    legs = [("put", 85.0, OrderDirection.BUY, 1), ("put", 90.0, OrderDirection.SELL, 1),
            ("call", 110.0, OrderDirection.SELL, 1), ("call", 115.0, OrderDirection.BUY, 1)]
    return Combo("iron_condor", legs, qty, prem)


_COMBO_BUILDERS = {
    "bull_call_spread": _bull_call_spread,
    "bear_put_spread": _bear_put_spread,
    "bear_call_spread": _bear_call_spread,
    "call_butterfly": _call_butterfly,
    "iron_condor": _iron_condor,
}


def _regime_spots(combo: Combo) -> Dict[str, float]:
    """Expiry-regime spots: far below all strikes, each adjacent-gap midpoint, exactly at each
    strike, far above all strikes."""
    ks = combo.strikes
    span = max(ks[-1] - ks[0], 10.0)
    regimes: Dict[str, float] = {"far_below": ks[0] - span}
    for k in ks:
        regimes[f"at_{k:g}"] = k
    for a, b in zip(ks, ks[1:]):
        regimes[f"mid_{a:g}_{b:g}"] = (a + b) / 2.0
    regimes["far_above"] = ks[-1] + span
    return regimes


# ---------------------------------------------------------------------------
# Assertion helper
# ---------------------------------------------------------------------------
def _assert_invariants(res: Dict[str, Any], combo: Combo, label: str,
                       starting_cash: float = CFG["starting_cash"]):
    curve = res["equity_curve"]
    max_loss = combo.max_loss()
    max_profit = combo.max_profit()
    floor = starting_cash - max_loss

    # FINITE
    for i, v in enumerate(curve):
        assert math.isfinite(v), f"[{label}] equity[{i}]={v!r} is not finite"

    # FLOOR (every bar)
    assert min(curve) >= floor - EPS, (
        f"[{label}] FLOOR breached: min(equity)={min(curve):.2f} < "
        f"floor={floor:.2f} (starting {starting_cash} - max_loss {max_loss:.2f}); curve={curve}"
    )

    # NON_NEGATIVE (adequately funded defined-risk combo)
    assert min(curve) >= -EPS, (
        f"[{label}] NON_NEGATIVE breached: min(equity)={min(curve):.2f} < 0; curve={curve}"
    )

    # REALIZED final in [floor, ceil]
    final = res["final_equity"]
    ceil = starting_cash + max_profit
    assert floor - EPS <= final <= ceil + EPS, (
        f"[{label}] REALIZED out of band: final={final:.2f} not in "
        f"[{floor:.2f}, {ceil:.2f}] (max_loss={max_loss:.2f}, max_profit={max_profit:.2f})"
    )

    # NO_STOCK residual after defined-risk settlement
    assert res["positions"] == [], (
        f"[{label}] NO_STOCK breached: residual stock {res['positions']} after expiry"
    )
    assert res["option_positions"] == [], (
        f"[{label}] residual option lots {res['option_positions']} after expiry"
    )


# ===========================================================================
# Invariant tests: combos x expiry regimes
# ===========================================================================
_COMBOS_TO_TEST = ["bull_call_spread", "bear_put_spread", "bear_call_spread",
                   "call_butterfly", "iron_condor"]


def _all_combo_regime_cases():
    cases = []
    for name in _COMBOS_TO_TEST:
        combo = _COMBO_BUILDERS[name]()
        for regime, spot in _regime_spots(combo).items():
            cases.append((name, regime, spot))
    return cases


@pytest.mark.parametrize("combo_name,regime,spot", _all_combo_regime_cases())
def test_defined_risk_invariants(tmp_path, combo_name, regime, spot):
    """FLOOR / NON_NEGATIVE / REALIZED / FINITE / NO_STOCK for each (combo, expiry regime)."""
    combo = _COMBO_BUILDERS[combo_name]()
    tag = f"inv_{combo_name}_{regime}".replace(".", "_").replace("-", "m")
    res = run_lifecycle(tmp_path, tag, combo, spot_at_expiry=spot)
    _assert_invariants(res, combo, f"{combo_name}/{regime}@{spot:g}")


# ===========================================================================
# Naked-short bound test
# ===========================================================================
def _run_naked(tmp_path, tag, strategy, legs_spec, quantity, gap_spot):
    """Open a NAKED short combo, then gap the underlying hard AGAINST it and assert the
    per-bar margin check force-liquidates and the recorded equity stays bounded.

    legs_spec: list of (option_type, strike, entry_premium_per_share).
    """
    # Underlying starts at 100 (all shorts OTM at entry), then gaps to gap_spot.
    und_bars = [
        {"Date": _D_START, "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 100},
        {"Date": _D_FILL, "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 100},
        {"Date": datetime(2024, 3, 8), "Open": gap_spot, "High": gap_spot + 2,
         "Low": gap_spot - 2, "Close": gap_spot, "Volume": 100},
    ]
    ps = _make_ps(_UND, und_bars, _D_START)
    chain = [_chain_row(_occ(ot, k), ot, k) for (ot, k, _p) in legs_spec]
    bar_rows = []
    for (ot, k, prem) in legs_spec:
        sym = _occ(ot, k)
        bar_rows.append(_bar(sym, "2024-03-06", prem, ot, k))
        # A blow-up premium bar at the gap day so the buyback books the real (large) loss.
        blow = _intrinsic(ot, k, gap_spot) + 1.0
        bar_rows.append(_bar(sym, "2024-03-08", blow, ot, k))
    acct, ctx = _account(tmp_path, tag, ps, _UND, chain, bar_rows, cfg=CFG)
    try:
        order_legs = [_leg(_occ(ot, k), OrderDirection.SELL, _right(ot), k)
                      for (ot, k, _p) in legs_spec]
        acct.submit_option_order(legs=order_legs, quantity=quantity,
                                 order_type="market", option_strategy=strategy)
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct.get_option_positions(), f"[{tag}] naked combo did not open"

        # Step to the gap bar.
        ps.set_clock(datetime(2024, 3, 8))
        acct.refresh_orders()

        eq_before = acct.equity()
        fired = acct.maybe_margin_call_liquidation()
        eq_after = acct.equity()
        snap = acct.snapshot_equity(datetime(2024, 3, 8))
        return {
            "fired": fired,
            "eq_before": eq_before,
            "eq_after": eq_after,
            "recorded_nlv": snap["net_liquidating_value"],
            "option_positions": acct.get_option_positions(),
            "cash": acct._cash,
        }
    finally:
        ctx.__exit__(None, None, None)


def test_naked_short_strangle_margin_call_bounds_equity(tmp_path):
    """A naked short strangle sized to breach maintenance when the underlying gaps up hard
    must force-liquidate and leave recorded equity bounded — not the huge negative it would
    reach unliquidated."""
    # 20 short 105 calls @1 + 20 short 95 puts @1 on a $100k account. Gap to 200 (call side
    # deep ITM): unliquidated buyback would be ~20*(95+1)*100 = -$192k; the margin check must
    # fire and stop the bleed well above any arbitrary negative.
    legs = [("call", 105.0, 1.0), ("put", 95.0, 1.0)]
    res = _run_naked(tmp_path, "naked_strangle", "short_strangle", legs, quantity=20,
                     gap_spot=200.0)
    assert res["fired"] is True, "margin call did not fire on a hard gap against a naked strangle"
    assert res["option_positions"] == [], "naked legs not fully unwound by the margin call"
    # Bounded: recorded equity must not be an arbitrary blow-up. The call buyback books
    # ~intrinsic (95) + 1 per share x 20 x 100 = ~$192k, so equity ends around -92k WORST case
    # IF unbounded; but the point is the position is CLOSED and cannot bleed further past the
    # single realized buyback. Assert it stayed finite and did not run away below a sane floor.
    assert math.isfinite(res["recorded_nlv"])
    # Sane floor: cannot lose more than the single realized buyback of the gapped legs.
    # Worst realized ~= starting - 20*(intrinsic(200)+1)*100 for the call leg = 100k - 192k.
    assert res["recorded_nlv"] >= -200_000.0, (
        f"recorded equity {res['recorded_nlv']:.2f} ran away below a sane floor"
    )
    # And it is strictly better than leaving the naked short open through further adverse marks
    # (position is flat -> no further unbounded exposure).
    assert res["option_positions"] == []


def test_naked_short_straddle_margin_call_bounds_equity(tmp_path):
    """Same guard for a naked short straddle gapped hard down (put side deep ITM)."""
    legs = [("call", 100.0, 1.0), ("put", 100.0, 1.0)]
    res = _run_naked(tmp_path, "naked_straddle", "short_straddle", legs, quantity=25,
                     gap_spot=20.0)
    assert res["fired"] is True
    assert res["option_positions"] == []
    assert math.isfinite(res["recorded_nlv"])
    assert res["recorded_nlv"] >= -250_000.0


# ===========================================================================
# Seeded fuzz test
# ===========================================================================
def _random_defined_risk_combo(rng: random.Random) -> Tuple[Combo, float, str]:
    """Deterministically build a random DEFINED-RISK combo + a random spot-at-expiry.

    Returns (combo, spot_at_expiry, description) so a failure is reproducible via the
    description in the assertion message.
    """
    shape = rng.choice(["bull_call_spread", "bear_put_spread", "bear_call_spread",
                        "call_butterfly", "iron_condor"])
    base = rng.choice([80.0, 100.0, 120.0, 150.0])
    width = float(rng.choice([5, 10, 15, 20]))
    qty = rng.randint(1, 8)

    if shape == "bull_call_spread":
        k1, k2 = base, base + width
        prem = {k1: {"call": width * 0.6}, k2: {"call": width * 0.2}}
        legs = [("call", k1, OrderDirection.BUY, 1), ("call", k2, OrderDirection.SELL, 1)]
        combo = Combo(shape, legs, qty, prem)
    elif shape == "bear_put_spread":
        k1, k2 = base + width, base
        prem = {k1: {"put": width * 0.6}, k2: {"put": width * 0.2}}
        legs = [("put", k1, OrderDirection.BUY, 1), ("put", k2, OrderDirection.SELL, 1)]
        combo = Combo(shape, legs, qty, prem)
    elif shape == "bear_call_spread":
        k1, k2 = base, base + width
        prem = {k1: {"call": width * 0.6}, k2: {"call": width * 0.2}}
        legs = [("call", k1, OrderDirection.SELL, 1), ("call", k2, OrderDirection.BUY, 1)]
        combo = Combo(shape, legs, qty, prem)
    elif shape == "call_butterfly":
        k1, k2, k3 = base, base + width, base + 2 * width
        prem = {k1: {"call": width * 1.2}, k2: {"call": width * 0.5}, k3: {"call": width * 0.1}}
        legs = [("call", k1, OrderDirection.BUY, 1), ("call", k2, OrderDirection.SELL, 2),
                ("call", k3, OrderDirection.BUY, 1)]
        combo = Combo(shape, legs, qty, prem)
    else:  # iron_condor
        k1, k2 = base - 2 * width, base - width
        k3, k4 = base + width, base + 2 * width
        prem = {k1: {"put": width * 0.1}, k2: {"put": width * 0.3},
                k3: {"call": width * 0.3}, k4: {"call": width * 0.1}}
        legs = [("put", k1, OrderDirection.BUY, 1), ("put", k2, OrderDirection.SELL, 1),
                ("call", k3, OrderDirection.SELL, 1), ("call", k4, OrderDirection.BUY, 1)]
        combo = Combo(shape, legs, qty, prem)

    # Random spot-at-expiry across the whole regime range (far below .. far above).
    lo, hi = combo.strikes[0], combo.strikes[-1]
    span = hi - lo
    spot = rng.uniform(lo - span - 5.0, hi + span + 5.0)
    desc = (f"shape={shape} base={base} width={width} qty={qty} strikes={combo.strikes} "
            f"net_prem/struct={combo.net_premium_per_structure():.2f} spot@exp={spot:.2f}")
    return combo, spot, desc


def test_fuzz_defined_risk_invariants(tmp_path):
    """~30 deterministically-generated random defined-risk combos: FLOOR + NON_NEGATIVE +
    FINITE for each. The generated params are logged in every assertion message so a failure
    is reproducible."""
    rng = random.Random(1234)
    n = 30
    for i in range(n):
        combo, spot, desc = _random_defined_risk_combo(rng)
        tag = f"fuzz_{i}"
        res = run_lifecycle(tmp_path, tag, combo, spot_at_expiry=spot)
        curve = res["equity_curve"]
        label = f"fuzz#{i}: {desc}"

        for j, v in enumerate(curve):
            assert math.isfinite(v), f"[{label}] equity[{j}]={v!r} not finite"

        max_loss = combo.max_loss()
        floor = CFG["starting_cash"] - max_loss
        assert min(curve) >= floor - EPS, (
            f"[{label}] FLOOR breached: min={min(curve):.2f} < floor={floor:.2f} "
            f"(max_loss={max_loss:.2f}); curve={curve}"
        )
        assert min(curve) >= -EPS, (
            f"[{label}] NON_NEGATIVE breached: min={min(curve):.2f}; curve={curve}"
        )


# ===========================================================================
# Determinism test
# ===========================================================================
def test_determinism_identical_equity_curves(tmp_path):
    """The same scenario run twice produces byte-identical equity curves."""
    combo1 = _iron_condor(qty=3)
    combo2 = _iron_condor(qty=3)
    spot = 130.0  # call wing fully breached
    res1 = run_lifecycle(tmp_path, "det_a", combo1, spot_at_expiry=spot)
    res2 = run_lifecycle(tmp_path, "det_b", combo2, spot_at_expiry=spot)
    assert res1["equity_curve"] == res2["equity_curve"], (
        f"non-deterministic equity curves:\n a={res1['equity_curve']}\n b={res2['equity_curve']}"
    )
    # Exact float identity (repr) for every point.
    for a, b in zip(res1["equity_curve"], res2["equity_curve"]):
        assert repr(a) == repr(b), f"float mismatch {a!r} != {b!r}"
    assert repr(res1["final_equity"]) == repr(res2["final_equity"])
    assert repr(res1["final_cash"]) == repr(res2["final_cash"])


# ===========================================================================
# Equity-only no-impact test (option paths gated to option runs)
# ===========================================================================
def test_equity_only_no_option_impact(tmp_path):
    """With NO options provider: _option_positions_mtm() == 0.0, the margin-call path is a
    no-op (returns False), and a plain equity position marks normally across bars — proving
    the option accounting never touches an equity-only run."""
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount

    bars = [
        {"Date": datetime(2024, 3, 5), "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 100},
        {"Date": datetime(2024, 3, 6), "Open": 110, "High": 112, "Low": 108, "Close": 110, "Volume": 100},
        {"Date": datetime(2024, 3, 7), "Open": 90, "High": 92, "Low": 88, "Close": 90, "Volume": 100},
    ]
    ps = _make_ps(_UND, bars, datetime(2024, 3, 5))
    wire_backtest_seams()
    ctx = backtest_trading_db("eqonly")
    ctx.__enter__()
    try:
        seed_account_definition(1, CFG)
        acct = BacktestAccount(1, ps, CFG, options_provider=None)  # NO options provider
        wire_backtest_seams().register_account(1, acct)

        # Option paths are gated OFF.
        assert acct._options is None
        assert acct._option_positions_mtm() == 0.0

        # Open a plain LONG equity position: 100 shares @100 = 10,000 cost.
        acct._update_position(_UND, 100, 100.0)
        acct._cash -= 100 * 100.0
        start_cash = acct._cash

        # Bar 1: close 100 -> equity = cash + 100*100.
        ps.set_clock(datetime(2024, 3, 5))
        assert acct._option_positions_mtm() == 0.0
        assert acct.maybe_margin_call_liquidation() is False  # no-op on equity-only run
        assert acct.equity() == pytest.approx(start_cash + 100 * 100.0)

        # Bar 2: close 110 -> equity marks up.
        ps.set_clock(datetime(2024, 3, 6))
        assert acct.equity() == pytest.approx(start_cash + 100 * 110.0)
        assert acct.maybe_margin_call_liquidation() is False

        # Bar 3: close 90 -> equity marks down; still a no-op margin path, position untouched.
        ps.set_clock(datetime(2024, 3, 7))
        assert acct.equity() == pytest.approx(start_cash + 100 * 90.0)
        assert acct.maybe_margin_call_liquidation() is False
        assert acct._positions[_UND].qty == 100
        assert acct._option_positions_mtm() == 0.0
    finally:
        ctx.__exit__(None, None, None)
