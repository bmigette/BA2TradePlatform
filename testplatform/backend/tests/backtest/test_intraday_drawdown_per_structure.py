"""Per-STRUCTURE intraday drawdown refinement (``intraday_drawdown.refine_max_drawdown``).

A trade row is one LEG. The refinement used to price every row alone, so a short body or wing leg
was re-priced linearly off its own delta as a NAKED short with the hedging legs ignored: a
butterfly that could lose at most $780 produced a $7,608 "worst leg" and a -40.3% reported
max_drawdown against a -12.9% daily curve (O_BF optimization 23, genome B).

A multi-leg structure is now re-priced with Black-Scholes (the engine's own pricer) on the same
5-minute prints, so GAMMA is in the estimate: a linear-delta sum read a near-delta-neutral
short-premium structure (iron condor, strangle) as almost flat.

Pure-function tests: all data access is faked with plain callables, like
``test_intraday_drawdown.py``.
"""
from __future__ import annotations

import datetime as dt
import random
from datetime import datetime

import pytest

import app.services.backtest.intraday_drawdown as idd
from app.services.backtest.intraday_drawdown import (
    estimate_worst_intraday_pnl,
    is_flagged_for_intraday_check,
    refine_max_drawdown,
)
from ba2_common.core.option_bs import bs_price
from ba2_common.core.types import OptionRight

ENTRY = datetime(2024, 4, 3)
EXIT = datetime(2024, 4, 4)
EQUITY = 20_000.0
RATE = 0.04
IV = 0.3


def _row(strike, direction, size, premium, *, option_type="call", txn=1, entry=ENTRY,
         exit_=EXIT, contract=None, **kw):
    r = {
        "contract_symbol": contract or f"AAPL240419{'C' if option_type == 'call' else 'P'}{int(strike * 1000):08d}",
        "underlying_symbol": "AAPL", "entry_time": entry, "exit_time": exit_,
        "direction": direction, "entry_price": premium, "size": size, "multiplier": 100.0,
        "option_type": option_type, "strike": float(strike), "transaction_id": txn,
        "expiry": "2024-04-19", "bars_held": 1, "pnl": 0.0,
    }
    r.update(kw)
    return r


def _refine(trades, bars, *, spot=110.0, max_drawdown=-0.5, commission=0.0, base=None,
            deltas=None, ivs=None, stats=None, bars_fn=None, **kw):
    """``ivs``: contract -> iv (default: IV for every contract); ``deltas`` only feeds the
    single-leg path."""
    ivs = ivs if ivs is not None else {t["contract_symbol"]: IV for t in trades
                                       if t.get("contract_symbol")}
    deltas = deltas if deltas is not None else {t["contract_symbol"]: 0.5 for t in trades
                                                if t.get("contract_symbol")}
    return refine_max_drawdown(
        trades, max_drawdown,
        equity_at=lambda d: EQUITY, peak_at=lambda d: EQUITY,
        daily_bar_low=lambda s, d: 100.0, prior_daily_bar_low=lambda s, d: 105.0,
        delta_at_entry=lambda u, c, d: deltas.get(c),
        underlying_price_at=lambda s, d: spot,
        bars_5m_between=bars_fn or (lambda s, a, b: bars),
        commission_per_trade=commission, drawdown_base=base,
        iv_at_entry=lambda u, c, d: ivs.get(c), risk_free_rate=lambda d: RATE,
        stats=stats, **kw)


def _legs_of(rows, iv=IV):
    out = []
    for r in rows:
        out.append({"contract": r["contract_symbol"], "entry_premium": r["entry_price"], "iv": iv,
                    "size": r["size"], "multiplier": 100.0,
                    "direction_sign": 1.0 if r["direction"] == "buy" else -1.0,
                    "strike": r["strike"],
                    "right": OptionRight.CALL if r["option_type"] == "call" else OptionRight.PUT,
                    "expiry": dt.date(2024, 4, 19)})
    return out


def _linear_worst(rows, spot, bars, iv=IV):
    """The OLD estimate for a structure: linear delta (finite difference) per print, summed."""
    legs = _legs_of(rows, iv)
    worst = None
    for b in bars:
        for px in (b["Low"], b["High"]):
            tot = 0.0
            for leg in legs:
                v0 = bs_price(spot, leg["strike"], 16, iv, leg["right"], r=RATE)
                d = (bs_price(spot + 0.01, leg["strike"], 16, iv, leg["right"], r=RATE) - v0) / 0.01
                implied = max(leg["entry_premium"] + d * (px - spot), 0.0)
                tot += (implied - leg["entry_premium"]) * leg["size"] * 100 * leg["direction_sign"]
            worst = tot if worst is None or tot < worst else worst
    return worst


# --------------------------------------------------------------------------- butterfly
def _fly(txn=1, k=(100, 110, 120), prem=(12.0, 5.5, 2.5), qty=1):
    return [_row(k[0], "buy", qty, prem[0], txn=txn),
            _row(k[1], "sell", 2 * qty, prem[1], txn=txn),
            _row(k[2], "buy", qty, prem[2], txn=txn)]


def test_butterfly_dip_is_of_the_order_of_the_daily_curve_not_three_times_it():
    """Short body, small fly, the underlying prints 95..140 inside the window. Priced as a
    naked short the body alone reads -$3,000 (-15% of equity); the whole structure can lose at
    most its $350 debit (-1.75%)."""
    fly = _fly()
    stats = {}
    refined = _refine(fly, [{"Low": 95.0, "High": 140.0}], stats=stats)
    assert -1.75 - 1e-9 <= refined < -0.5
    assert stats["structures"] == 1


def test_butterfly_candidate_is_never_worse_than_its_true_max_loss():
    """The model's worst print here is -$492, more than the $350 the structure can lose: capped."""
    fly = _fly()
    stats = {}
    refined = _refine(fly, [{"Low": 95.0, "High": 140.0}], stats=stats)
    assert refined == pytest.approx(-350.0 / EQUITY * 100.0)    # == the debit, exactly
    assert stats["capped"] == 1


def test_cap_includes_the_commissions_paid():
    fly = _fly()
    refined = _refine(fly, [{"Low": 95.0, "High": 140.0}], commission=5.0)
    assert refined == pytest.approx(-(350.0 + 15.0) / EQUITY * 100.0)


def test_unbalanced_butterfly_cap_is_above_its_debit():
    """Upper wing wider than the lower (100/110/130): above 130 it pays 10 - 20 = -10 a share,
    so the TRUE max loss (debit 2.5 + 10 = $1,250) exceeds the debit ($250)."""
    fly = _fly(k=(100, 110, 130), prem=(12.0, 5.5, 1.5))
    state, amount = idd.structure_max_loss(fly)
    assert state == "MEASURED"
    assert amount == pytest.approx(1250.0)
    refined = _refine(fly, [{"Low": 95.0, "High": 160.0}])
    assert refined >= -1250.0 / EQUITY * 100.0 - 1e-9


# --------------------------------------------------------------------------- verticals
def test_bull_call_spread_is_priced_with_its_long_leg():
    long_, short = _row(100, "buy", 1, 6.0), _row(110, "sell", 1, 2.5)
    # High 130: the short leg ALONE reads -$800 (-4%); the long leg offsets it.
    refined = _refine([long_, short], [{"Low": 108.0, "High": 130.0}])
    assert refined >= -1.75 - 1e-9              # never worse than the $350 debit


def test_bull_put_spread_is_priced_with_its_long_leg_and_capped():
    short = _row(100, "sell", 1, 5.0, option_type="put")
    long_ = _row(90, "buy", 1, 2.0, option_type="put")
    # Low 80: the short put alone reads -$800 (-4%); the model's structure worst is -$761,
    # above the $700 max loss (width 10 - credit 3): capped at -3.5%.
    refined = _refine([short, long_], [{"Low": 80.0, "High": 101.0}], spot=100.0)
    assert refined == pytest.approx(-3.5)


# --------------------------------------------------------------------------- gamma
def _ic():
    return [_row(80, "buy", 1, 0.5, option_type="put"), _row(90, "sell", 1, 1.5, option_type="put"),
            _row(110, "sell", 1, 1.5), _row(125, "buy", 1, 0.5)]


def test_short_iron_condor_dip_includes_gamma():
    """A move from 100 to the 110 short call: the linear-delta estimate reads the (near delta
    neutral) condor as ~$20 of loss; re-priced with Black-Scholes it costs ~$226."""
    ic = _ic()
    bars = [{"Low": 99.0, "High": 110.0}]
    ivs = {r["contract_symbol"]: 0.25 for r in ic}
    linear = _linear_worst(ic, 100.0, bars, iv=0.25)
    assert linear > -50.0
    refined = _refine(ic, bars, spot=100.0, ivs=ivs, max_drawdown=-0.01)
    bs_loss = -refined / 100.0 * EQUITY
    assert bs_loss > 150.0 and bs_loss > 5 * -linear
    assert bs_loss <= 1300.0                    # the true max loss


def test_short_strangle_like_structure_dip_includes_gamma_and_is_uncapped():
    """Naked short put + short call: UNBOUNDED, no cap, and the dip is the gamma-aware sum."""
    rows = [_row(90, "sell", 1, 1.5, option_type="put"), _row(110, "sell", 1, 1.5)]
    bars = [{"Low": 99.0, "High": 112.0}]
    stats = {}
    ivs = {r["contract_symbol"]: 0.25 for r in rows}
    assert idd.structure_max_loss(rows) == ("UNBOUNDED", None)
    linear = _linear_worst(rows, 100.0, bars, iv=0.25)
    refined = _refine(rows, bars, spot=100.0, ivs=ivs, stats=stats, max_drawdown=-0.01)
    assert stats["unbounded"] == 1 and stats["capped"] == 0
    assert -refined / 100.0 * EQUITY > 5 * -linear


def test_ratio_spread_is_unbounded_and_uncapped():
    rows = [_row(100, "buy", 1, 10.0), _row(110, "sell", 2, 5.0)]
    assert idd.structure_max_loss(rows) == ("UNBOUNDED", None)
    bars = [{"Low": 100.0, "High": 200.0}]
    stats = {}
    refined = _refine(rows, bars, spot=105.0, stats=stats)
    expect = idd.estimate_worst_structure_pnl(_legs_of(rows), 105.0, ENTRY, bars, 0.0, RATE)
    assert expect < -1000.0
    assert stats["unbounded"] == 1
    assert refined == pytest.approx(max(expect / EQUITY * 100.0, -100.0))


def test_iron_condor_with_unequal_wings_has_the_wider_wing_as_max_loss():
    state, amount = idd.structure_max_loss(_ic())
    assert state == "MEASURED"
    assert amount == pytest.approx((15.0 - 2.0) * 100.0)       # call wing 15, credit 2.0


# --------------------------------------------------------------------------- coverage
def test_a_structure_with_one_leg_missing_its_iv_is_uncovered_as_a_whole(monkeypatch):
    fly = _fly()
    ivs = {r["contract_symbol"]: IV for r in fly}
    ivs[fly[1]["contract_symbol"]] = None                       # body has no iv
    warnings, stats = [], {}
    monkeypatch.setattr(idd.logger, "warning", lambda m, *a, **k: warnings.append(m))
    refined = _refine(fly, [{"Low": 95.0, "High": 140.0}], ivs=ivs, stats=stats)
    assert refined == -0.5                                      # untouched: no partial pricing
    assert stats["uncovered"] == 1 and stats["no_iv"] == 1
    assert warnings and "100% uncovered" in warnings[0]


def test_a_run_without_the_iv_seam_never_falls_back_to_linear_delta():
    fly = _fly()
    stats = {}
    refined = refine_max_drawdown(
        fly, -0.5, equity_at=lambda d: EQUITY, peak_at=lambda d: EQUITY,
        daily_bar_low=lambda s, d: 100.0, prior_daily_bar_low=lambda s, d: 105.0,
        delta_at_entry=lambda u, c, d: 0.5, underlying_price_at=lambda s, d: 110.0,
        bars_5m_between=lambda s, a, b: [{"Low": 95.0, "High": 140.0}], stats=stats)
    assert refined == -0.5
    assert stats["uncovered"] == 1 and stats["no_iv"] == 1


def test_an_unpriceable_iv_is_uncovered_not_zero():
    fly = _fly()
    stats = {}
    ivs = {r["contract_symbol"]: -1.0 for r in fly}             # bs_price refuses it
    refined = _refine(fly, [{"Low": 95.0, "High": 140.0}], ivs=ivs, stats=stats)
    assert refined == -0.5 and stats["uncovered"] == 1


def test_legs_that_exit_apart_are_priced_over_the_all_legs_held_window():
    fly = _fly()
    fly[0]["exit_time"] = datetime(2024, 4, 5)                  # the long wing exits a day later
    asked, stats = [], {}

    def bars_fn(s, a, b):
        asked.append((a, b))
        return [{"Low": 95.0, "High": 140.0}]

    refined = _refine(fly, None, stats=stats, bars_fn=bars_fn)
    assert asked == [(ENTRY, EXIT)]                             # entry until the FIRST exit
    assert stats["staggered"] == 1 and stats["structures"] == 1 and stats["uncovered"] == 0
    assert refined < -0.5                                       # priced, not dropped


def test_a_leg_without_an_exit_time_is_uncovered_with_its_own_counter():
    fly = _fly()
    fly[1]["exit_time"] = None
    stats = {}
    refined = _refine(fly, [{"Low": 95.0, "High": 140.0}], stats=stats)
    assert refined == -0.5
    assert stats["no_exit"] == 1 and stats["uncovered"] == 1 and stats["errored"] == 0


def test_time_decay_is_not_charged_as_a_dip():
    """A long straddle whose underlying sits exactly where it entered, ten days later, has
    lost time value in a model that lets time run -- but that decay is already in the daily
    marks the refinement sits on. Time is frozen at entry, so no extra dip appears."""
    rows = [_row(100, "buy", 1, 4.0), _row(100, "buy", 1, 4.0, option_type="put")]
    bars = [{"Date": datetime(2024, 4, 13), "Low": 100.0, "High": 100.0}]
    refined = _refine(rows, bars, spot=100.0)
    assert refined == -0.5


def test_the_estimate_prices_a_session_range_not_every_print(monkeypatch):
    """78 five-minute bars in one session cost a handful of Black-Scholes evaluations per leg
    (ends + interior grid + strikes inside), not 156 per leg -- and the session's worst point
    over the range is found: here an interior strike where a short-gamma structure is worst."""
    import app.services.backtest.intraday_drawdown as mod
    calls = []
    real = mod._leg_value

    def counting(leg, spot, when, rate):
        calls.append(spot)
        return real(leg, spot, when, rate)

    monkeypatch.setattr(mod, "_leg_value", counting)
    legs = _legs_of([_row(110, "sell", 1, 1.5), _row(90, "sell", 1, 1.5, option_type="put")],
                    iv=0.25)
    day = datetime(2024, 4, 3)
    bars = [{"Date": day, "Low": 100.0 - 0.1 * (i % 10), "High": 100.0 + 0.1 * (i % 10) + (12 if i == 5 else 0)}
            for i in range(78)]
    worst = mod.estimate_worst_structure_pnl(legs, 100.0, day, bars, 0.0, RATE)
    assert worst is not None and worst < 0
    assert len(calls) <= 2 * (1 + 2 + 4 + 2)          # anchors + ends + interior + strikes, per leg


def test_two_structures_are_independent_candidates():
    a, b = _fly(txn=1), _fly(txn=2)
    for r in b:
        r["contract_symbol"] += "X"
    refined = _refine(a + b, [{"Low": 95.0, "High": 140.0}])
    assert refined == pytest.approx(-350.0 / EQUITY * 100.0)    # the worse of the two, not the sum


def test_two_different_contracts_at_one_transaction_and_entry_are_one_structure():
    """NOT identical to the legacy per-row path: two contracts entered together are a structure."""
    long_, short = _row(100, "buy", 1, 6.0), _row(110, "sell", 1, 2.5)
    bars = [{"Low": 108.0, "High": 130.0}]
    stats = {}
    new = _refine([long_, short], bars, stats=stats)
    legacy = _legacy_refine(
        [long_, short], -0.5, equity_at=lambda d: EQUITY, peak_at=lambda d: EQUITY,
        daily_bar_low=lambda s, d: 100.0, prior_daily_bar_low=lambda s, d: 105.0,
        delta_at_entry=lambda u, c, d: {long_["contract_symbol"]: 0.6,
                                        short["contract_symbol"]: 0.4}[c],
        underlying_price_at=lambda s, d: 110.0, bars_5m_between=lambda s, a, b: bars)
    assert stats["structures"] == 1
    assert legacy < -3.0 and new > legacy


def test_a_late_leg_of_a_multi_contract_transaction_is_counted_as_rolled():
    """A contract entered LATER into a transaction that holds other contracts stays on the
    single-leg path (priced alone) -- and is counted, not silent."""
    first = _row(100, "buy", 1, 6.0, txn=9)
    late = _row(110, "sell", 1, 2.5, txn=9, entry=datetime(2024, 4, 10), exit_=datetime(2024, 4, 11))
    stats = {}
    bars = [{"Low": 100.0, "High": 125.0}]
    new = _refine([first, late], bars, stats=stats)
    legacy = _legacy_refine(
        [first, late], -0.5, equity_at=lambda d: EQUITY, peak_at=lambda d: EQUITY,
        daily_bar_low=lambda s, d: 100.0, prior_daily_bar_low=lambda s, d: 105.0,
        delta_at_entry=lambda u, c, d: 0.5, underlying_price_at=lambda s, d: 110.0,
        bars_5m_between=lambda s, a, b: bars)
    assert new == legacy                                        # priced alone, as documented
    assert stats["rolled"] == 2 and stats["structures"] == 0


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
    bars = [{"Low": rng.uniform(70, 100), "High": rng.uniform(100, 140)} for _ in range(6)]
    eq = rng.uniform(10_000, 80_000)
    peaks = {}

    def peak_at(d):
        return peaks.setdefault(d, eq * rng.choice([1.0, 1.0, 1.3]))

    kw = dict(equity_at=lambda d: eq, peak_at=peak_at,
              daily_bar_low=lambda s, d: 100.0, prior_daily_bar_low=lambda s, d: 101.0,
              delta_at_entry=lambda u, c, d: deltas[c], underlying_price_at=lambda s, d: 100.0,
              bars_5m_between=lambda s, a, b: bars,
              commission_per_trade=rng.choice([0.0, 1.3]),
              drawdown_base=rng.choice([None, None, 25_000.0]))
    new = refine_max_drawdown(trades, -1.5, **kw)
    old = _legacy_refine(trades, -1.5, **kw)
    assert new == old


def test_same_contract_fills_in_one_transaction_stay_per_row():
    """Two fills of ONE contract (a partially closed single leg) share a transaction and an
    entry time. That is one leg, not a structure: priced per row, as before."""
    a = _row(100, "buy", 1, 5.0, txn=7, exit_=datetime(2024, 4, 4))
    b = _row(100, "buy", 1, 5.0, txn=7, exit_=datetime(2024, 4, 5), contract=a["contract_symbol"])
    bars = [{"Low": 90.0, "High": 101.0}]
    new = _refine([a, b], bars, spot=100.0)
    old = _refine([a], bars, spot=100.0)
    assert new == old and new < -0.5


def test_a_multiplierless_option_row_is_refused_not_defaulted():
    """``results._trade_row`` used to default a missing multiplier to 1.0, which would cap a
    structure's max loss 100x too small. An option leg now refuses; equity rows keep 1.0."""
    from app.services.backtest.results import _trade_row
    base = {"symbol": "AAPL", "entry_time": "2024-04-03T00:00:00+00:00",
            "exit_time": "2024-04-04T00:00:00+00:00", "direction": "buy", "entry_price": 5.0,
            "exit_price": 6.0, "size": 1.0, "pnl": 100.0, "bars_held": 1}
    with pytest.raises(ValueError):
        _trade_row({**base, "contract_symbol": "AAPL240419C00100000"})
    assert _trade_row(base)["multiplier"] == 1.0
    assert _trade_row({**base, "contract_symbol": "AAPL240419C00100000",
                       "multiplier": 100.0})["multiplier"] == 100.0
