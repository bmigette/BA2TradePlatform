"""Per-STRUCTURE intraday drawdown refinement (``intraday_drawdown.refine_max_drawdown``).

A trade row is one LEG. The refinement used to price every row alone, so a short body or wing leg
was re-priced linearly off its own delta as a NAKED short with the hedging legs ignored: a
butterfly that could lose at most $780 produced a $7,608 "worst leg" and a -40.3% reported
max_drawdown against a -12.9% daily curve (O_BF optimization 23, genome B).

Pure-function tests: all data access is faked with plain callables, like
``test_intraday_drawdown.py``.
"""
from __future__ import annotations

import random
from datetime import datetime

import pytest

import app.services.backtest.intraday_drawdown as idd
from app.services.backtest.intraday_drawdown import (
    estimate_worst_intraday_pnl,
    is_flagged_for_intraday_check,
    refine_max_drawdown,
)

ENTRY = datetime(2024, 4, 3)
EXIT = datetime(2024, 4, 4)
EQUITY = 20_000.0


def _row(strike, direction, size, premium, *, option_type="call", txn=1, entry=ENTRY,
         exit_=EXIT, contract=None, **kw):
    r = {
        "contract_symbol": contract or f"AAPL240419{'C' if option_type == 'call' else 'P'}{int(strike * 1000):08d}",
        "underlying_symbol": "AAPL", "entry_time": entry, "exit_time": exit_,
        "direction": direction, "entry_price": premium, "size": size, "multiplier": 100.0,
        "option_type": option_type, "strike": float(strike), "transaction_id": txn,
        "bars_held": 1, "pnl": 0.0,
    }
    r.update(kw)
    return r


def _refine(trades, bars, deltas, *, spot=110.0, max_drawdown=-0.5, commission=0.0,
            base=None, **kw):
    return refine_max_drawdown(
        trades, max_drawdown,
        equity_at=lambda dt: EQUITY, peak_at=lambda dt: EQUITY,
        daily_bar_low=lambda s, dt: 100.0, prior_daily_bar_low=lambda s, dt: 105.0,
        delta_at_entry=lambda u, c, dt: deltas.get(c),
        underlying_price_at=lambda s, dt: spot,
        bars_5m_between=lambda s, a, b: bars,
        commission_per_trade=commission, drawdown_base=base, **kw)


# --------------------------------------------------------------------------- butterfly
def _fly(txn=1, k=(100, 110, 120), prem=(12.0, 5.5, 2.5), qty=1):
    return [_row(k[0], "buy", qty, prem[0], txn=txn),
            _row(k[1], "sell", 2 * qty, prem[1], txn=txn),
            _row(k[2], "buy", qty, prem[2], txn=txn)]


def _deltas(rows, ds):
    return {r["contract_symbol"]: d for r, d in zip(rows, ds)}


def test_butterfly_dip_is_of_the_order_of_the_daily_curve_not_three_times_it():
    """Short body, small fly. The underlying prints 95..140 inside the window. Priced as a
    naked short the body alone reads -$3,000 (-15% of equity); the whole structure's worst
    sum over the same prints is -$200 (-1%), and it can lose at most its $350 debit."""
    fly = _fly()
    d = _deltas(fly, (0.7, 0.5, 0.3))
    refined = _refine(fly, [{"Low": 95.0, "High": 140.0}], d)
    assert refined == pytest.approx(-1.0)          # -200 / 20,000
    assert refined >= -1.75                        # never worse than the debit (350 / 20,000)


def test_butterfly_candidate_is_never_worse_than_its_true_max_loss():
    """Deltas that disagree with the payoff (a first-order estimate) sum to far more than the
    structure can lose; the estimate is capped at its true worst payoff."""
    fly = _fly()
    d = _deltas(fly, (0.1, 0.9, 0.1))              # huge short-body delta
    refined = _refine(fly, [{"Low": 95.0, "High": 140.0}], d)
    assert refined == pytest.approx(-350.0 / EQUITY * 100.0)    # == the debit, exactly


def test_cap_includes_the_commissions_paid():
    fly = _fly()
    d = _deltas(fly, (0.1, 0.9, 0.1))
    refined = _refine(fly, [{"Low": 95.0, "High": 140.0}], d, commission=5.0)
    assert refined == pytest.approx(-(350.0 + 15.0) / EQUITY * 100.0)


def test_unbalanced_butterfly_cap_is_above_its_debit():
    """Upper wing wider than the lower (100/110/130): above 130 it pays 10 - 20 = -10 a share,
    so the TRUE max loss (debit 3.5 + 10 = $1,350) exceeds the debit ($350)."""
    fly = _fly(k=(100, 110, 130), prem=(12.0, 5.5, 1.5))      # debit 12 - 11 + 1.5 = 2.5
    state, amount = idd.structure_max_loss(fly)
    assert state == "MEASURED"
    assert amount == pytest.approx((2.5 + 10.0) * 100.0)       # 1,250 > the 250 debit
    d = _deltas(fly, (0.1, 0.9, 0.1))
    refined = _refine(fly, [{"Low": 95.0, "High": 160.0}], d)
    assert refined == pytest.approx(-1250.0 / EQUITY * 100.0)


# --------------------------------------------------------------------------- verticals
def test_bull_call_spread_is_priced_with_its_long_leg():
    long_, short = _row(100, "buy", 1, 6.0), _row(110, "sell", 1, 2.5)
    d = _deltas([long_, short], (0.6, 0.4))
    # High 130: the short leg ALONE reads -(2.5+0.4*20-2.5)*100 = -$800 (-4%); the long leg's
    # +$1,200 offsets it. Debit 3.5 -> cap $350.
    refined = _refine([long_, short], [{"Low": 108.0, "High": 130.0}], d)
    assert refined >= -1.75 - 1e-9
    assert refined == pytest.approx(-0.5)           # worst point (the Low print) is inside the daily dip


def test_bull_put_spread_is_priced_with_its_long_leg():
    short = _row(100, "sell", 1, 5.0, option_type="put")
    long_ = _row(90, "buy", 1, 2.0, option_type="put")
    d = _deltas([short, long_], (-0.4, -0.2))
    # Low 80: short put re-prices to 13 (-$800 on its own, -4%); the long put's +$400 offsets
    # it. The structure's worst is -$400 (-2%); its max loss is $700.
    refined = _refine([short, long_], [{"Low": 80.0, "High": 101.0}], d, spot=100.0)
    assert refined == pytest.approx(-2.0)


# --------------------------------------------------------------------------- unbounded / IC
def test_iron_condor_with_unequal_wings_has_the_wider_wing_as_max_loss():
    ic = [_row(80, "buy", 1, 0.5, option_type="put"), _row(90, "sell", 1, 1.5, option_type="put"),
          _row(110, "sell", 1, 1.5), _row(125, "buy", 1, 0.5)]
    state, amount = idd.structure_max_loss(ic)
    assert state == "MEASURED"
    assert amount == pytest.approx((15.0 - 2.0) * 100.0)       # call wing 15, credit 2.0


def test_ratio_spread_is_unbounded_and_uncapped():
    """1x2 call ratio spread: a naked short call above the long. UNBOUNDED, never a finite
    wrong number, and the refinement applies no cap -- only the summed-legs estimate."""
    rows = [_row(100, "buy", 1, 10.0), _row(110, "sell", 2, 5.0)]
    assert idd.structure_max_loss(rows) == ("UNBOUNDED", None)
    d = _deltas(rows, (0.6, 0.5))
    bars = [{"Low": 100.0, "High": 200.0}]
    refined = _refine(rows, bars, d, spot=105.0)
    expect = idd.estimate_worst_structure_pnl(
        [{"entry_premium": 10.0, "delta": 0.6, "size": 1, "multiplier": 100.0, "direction_sign": 1.0},
         {"entry_premium": 5.0, "delta": 0.5, "size": 2, "multiplier": 100.0, "direction_sign": -1.0}],
        105.0, bars, 0.0)
    assert expect < -1000.0
    assert refined == pytest.approx(max(expect / EQUITY * 100.0, -100.0))


# --------------------------------------------------------------------------- coverage
def test_a_structure_with_one_leg_missing_its_delta_is_uncovered_as_a_whole(monkeypatch):
    fly = _fly()
    d = _deltas(fly, (0.7, 0.5, 0.3))
    d[fly[1]["contract_symbol"]] = None                         # body has no delta
    warnings = []
    monkeypatch.setattr(idd.logger, "warning", lambda m, *a, **k: warnings.append(m))
    refined = _refine(fly, [{"Low": 95.0, "High": 140.0}], d)
    assert refined == -0.5                                      # untouched: no partial pricing
    assert warnings and "100% uncovered" in warnings[0]


def test_legs_that_do_not_exit_together_are_uncovered_not_split(monkeypatch):
    fly = _fly()
    fly[0]["exit_time"] = datetime(2024, 4, 5)
    d = _deltas(fly, (0.7, 0.5, 0.3))
    warnings = []
    monkeypatch.setattr(idd.logger, "warning", lambda m, *a, **k: warnings.append(m))
    refined = _refine(fly, [{"Low": 95.0, "High": 140.0}], d)
    assert refined == -0.5
    assert warnings and "did not enter and exit together" in warnings[0]


def test_two_structures_are_independent_candidates():
    a, b = _fly(txn=1), _fly(txn=2)
    # Contract symbols repeat between the two structures; disambiguate them.
    for r in b:
        r["contract_symbol"] += "X"
    d = {**_deltas(a, (0.7, 0.5, 0.3)), **_deltas(b, (0.1, 0.9, 0.1))}
    refined = _refine(a + b, [{"Low": 95.0, "High": 140.0}], d)
    assert refined == pytest.approx(-350.0 / EQUITY * 100.0)    # the worse of the two, not the sum


# --------------------------------------------------------------------------- single leg identity
def _legacy_refine(trades, max_drawdown, *, equity_at, peak_at, daily_bar_low, prior_daily_bar_low,
                   delta_at_entry, underlying_price_at, bars_5m_between,
                   commission_per_trade=0.0, multiplier=100.0, drawdown_base=None):
    """The per-row loop exactly as it stood before the per-structure change (frozen copy)."""
    refined = max_drawdown
    for t in trades:
        contract = t.get("contract_symbol")
        underlying = t.get("underlying_symbol")
        if not contract or not underlying:
            continue
        try:
            exit_low = daily_bar_low(underlying, t.get("exit_time"))
            prior_low = prior_daily_bar_low(underlying, t.get("exit_time"))
            if not is_flagged_for_intraday_check(t, prior_low, exit_low):
                continue
            delta = delta_at_entry(underlying, contract, t.get("entry_time"))
            px = underlying_price_at(underlying, t.get("entry_time"))
            if delta is None or px is None:
                continue
            bars = bars_5m_between(underlying, t.get("entry_time"), t.get("exit_time"))
            sign = 1.0 if t.get("direction") == "buy" else -1.0
            worst_pnl = estimate_worst_intraday_pnl(
                entry_premium=t["entry_price"], entry_underlying_price=px, delta=delta,
                size=t["size"], multiplier=multiplier, commission=commission_per_trade,
                bars_5m=bars, direction_sign=sign)
            if worst_pnl is None:
                continue
            worst_loss = min(0.0, worst_pnl)
            if worst_loss == 0.0:
                continue
            equity = equity_at(t.get("entry_time"))
            peak = peak_at(t.get("entry_time"))
            if not equity or peak is None:
                continue
            peak = max(float(peak), float(equity))
            base = drawdown_base if drawdown_base is not None else peak
            if base <= 0:
                continue
            refined = min(refined, max((float(equity) + worst_loss - peak) / base * 100.0, -100.0))
        except Exception:
            continue
    return refined


@pytest.mark.parametrize("seed", range(40))
def test_single_leg_runs_are_bit_identical_to_the_legacy_function(seed):
    """Long calls, cash-secured puts and repeated fills of one contract, with and without a
    transaction id, random prices/deltas/equity: the result is EXACTLY (==) the old one."""
    rng = random.Random(seed)
    trades = []
    for i in range(rng.randint(1, 12)):
        is_put = rng.random() < 0.5
        buy = rng.random() < 0.5
        trades.append(_row(
            rng.choice([90, 95, 100, 105]), "buy" if buy else "sell", rng.randint(1, 5),
            round(rng.uniform(0.5, 12.0), 2), option_type="put" if is_put else "call",
            txn=(None if rng.random() < 0.3 else i // 2),     # some share a txn (distinct entries)
            entry=datetime(2024, 1, 1 + i), exit_=datetime(2024, 2, 1 + i),
            bars_held=rng.choice([1, 1, 4]), pnl=rng.uniform(-500, 500)))
    deltas = {t["contract_symbol"]: rng.choice([None, 0.2, 0.5, -0.4, -0.7]) for t in trades}
    for t in trades:
        deltas.setdefault(t["contract_symbol"], 0.5)
    bars = [{"Low": rng.uniform(70, 100), "High": rng.uniform(100, 140)} for _ in range(6)]
    eq = rng.uniform(10_000, 80_000)
    kw = dict(equity_at=lambda dt: eq, peak_at=lambda dt: eq * rng.choice([1.0, 1.0, 1.3]),
              daily_bar_low=lambda s, dt: 100.0, prior_daily_bar_low=lambda s, dt: 101.0,
              delta_at_entry=lambda u, c, dt: deltas[c], underlying_price_at=lambda s, dt: 100.0,
              bars_5m_between=lambda s, a, b: bars,
              commission_per_trade=rng.choice([0.0, 1.3]),
              drawdown_base=rng.choice([None, None, 25_000.0]))
    # peak_at draws from rng: make both calls see the same values.
    peaks = {}
    def peak_at(dt, _eq=eq, _k=kw):
        return peaks.setdefault(dt, _eq * rng.choice([1.0, 1.0, 1.3]))
    kw["peak_at"] = peak_at
    new = refine_max_drawdown(trades, -1.5, **kw)
    old = _legacy_refine(trades, -1.5, **kw)
    assert new == old


def test_same_contract_fills_in_one_transaction_stay_per_row():
    """Two fills of ONE contract (a partially closed single leg) share a transaction and an
    entry time. That is one leg, not a structure: priced per row, as before."""
    a = _row(100, "buy", 1, 5.0, txn=7, exit_=datetime(2024, 4, 4))
    b = _row(100, "buy", 1, 5.0, txn=7, exit_=datetime(2024, 4, 5), contract=a["contract_symbol"])
    d = {a["contract_symbol"]: 0.5}
    bars = [{"Low": 90.0, "High": 101.0}]
    new = _refine([a, b], bars, d, spot=100.0)
    old = _refine([a], bars, d, spot=100.0)
    assert new == old and new < -0.5
