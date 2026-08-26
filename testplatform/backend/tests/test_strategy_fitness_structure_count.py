"""``avg_trades_per_year`` counts option LEGS; the trade-frequency gate must count STRUCTURES.

``BacktestAccount.get_round_trip_trades`` keys on ``(transaction_id, contract_symbol)`` — one
row per LEG — so an iron condor is FOUR trades and a vertical is TWO. ``results.py`` then
divides that leg count by the calendar years spanned, so ``avg_trades_per_year`` is the LEG
rate, and ``consistent_annual_return``'s ``trade_gate`` (hard floor 12/yr, full credit at
30/yr) reads it as if it were the number of independent bets.

Consequences before this file existed:

* **three iron condors a year cleared disqualification** (3 x 4 = 12 legs >= the 12/yr floor),
* **7.5 condors a year earned FULL credit** (30 legs), and
* the inflation is worst exactly on the multi-leg credit structures whose per-bet means are
  least estimable — the ones the floor exists to exclude.

``results.py`` lives under ``services/backtest/`` (owned elsewhere), so the fix is applied at
the CONSUMING end: ``strategy_fitness`` re-derives the rate from ``results["trades"]``, which
``build_results`` always publishes, using the same "option legs sharing a transaction_id are
ONE economic bet" partition ``results._cap_groups`` already uses for the profit cap.
"""
import pytest

from app.services.strategy_fitness import (
    LOW_TRADE_SENTINEL,
    _structure_count,
    compute_fitness,
)

# One calendar year -> fewer than 2 measurable years -> consistency factor 1.0, so these tests
# isolate trade_gate. (A `trades` list with no `equity_curve` raises by design.)
_CURVE = [{"date": "2020-01-02", "equity": 100_000.0},
          {"date": "2020-12-31", "equity": 130_000.0}]


def _leg(txn, contract="AAPL240119C00100000"):
    """One OPTION leg row, shaped like ``results._trade_row``."""
    return {"symbol": "AAPL", "contract_symbol": contract, "transaction_id": txn,
            "pnl": 10.0, "pnl_pct": 0.1, "exit_time": "2020-06-01"}


def _equity_row(symbol="AAPL"):
    """One EQUITY round-trip row: no contract_symbol, so never joined to a structure."""
    return {"symbol": symbol, "contract_symbol": None, "transaction_id": 99,
            "pnl": 10.0, "pnl_pct": 0.1, "exit_time": "2020-06-01"}


def _condors(n):
    """``n`` four-leg iron condors, each its own transaction."""
    out = []
    for t in range(n):
        out += [_leg(t, f"C{t}L{k}") for k in range(4)]
    return out


def _r(trades, **kw):
    """A result whose ONLY sub-1.0 factor is trade_gate: base 30%/yr, dd at the -20%
    reference (dd_guard exactly 1.0), one calendar year (consistency 1.0)."""
    base = {
        "total_trades": len(trades),
        "avg_trades_per_year": float(len(trades)),  # a 1-year run: legs/yr == legs
        "annualized_return": 30.0,
        "max_drawdown": -20.0,
        "trades": trades,
        "equity_curve": _CURVE,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 1. The partition itself
# ---------------------------------------------------------------------------
def test_a_four_leg_condor_is_one_structure():
    assert _structure_count(_condors(1)) == 1


def test_three_condors_are_three_structures_not_twelve():
    assert _structure_count(_condors(3)) == 3


def test_equity_rows_are_each_their_own_structure():
    assert _structure_count([_equity_row("AAPL"), _equity_row("MSFT")]) == 2


def test_an_equity_leg_sharing_a_transaction_with_an_option_is_not_absorbed():
    """A covered call books shares + a short call under ONE transaction. The shares are a
    separate bet with their own capital at risk (same carve-out ``_cap_groups`` makes), so the
    pair is TWO structures — collapsing them would under-count the equity side."""
    rows = [{"symbol": "AAPL", "contract_symbol": None, "transaction_id": 7},
            {"symbol": "AAPL", "contract_symbol": "AAPL240119C00100000", "transaction_id": 7}]
    assert _structure_count(rows) == 2


def test_option_legs_with_no_transaction_id_are_not_merged():
    """UNKNOWN structure identity must not collapse into one bet. Two legs with ``None`` ids
    are two bets we cannot join, not one bet — merging on None would under-count arbitrarily
    (and would silently disqualify a perfectly active genome on a blob that lost the column)."""
    assert _structure_count([_leg(None, "X1"), _leg(None, "X2")]) == 2


def test_structure_count_is_none_when_there_is_no_trades_list():
    """Absent is not zero: the caller must fall back, not disqualify."""
    assert _structure_count(None) is None


# ---------------------------------------------------------------------------
# 2. The gate
# ---------------------------------------------------------------------------
def test_three_condors_a_year_is_disqualified():
    """THE headline case. 3 condors = 12 legs, which clears the 12/yr hard floor exactly —
    so before the fix this scored a real fitness. Three bets a year cannot evidence an edge."""
    assert compute_fitness("consistent_annual_return", _r(_condors(3))) == LOW_TRADE_SENTINEL


def test_thirty_legs_of_condors_do_not_earn_full_credit():
    """32 legs is FULL credit on the leg count (>= the 30/yr ramp target) — but it is only 8
    condors, which is below the 12/yr hard floor, so the genome is disqualified outright."""
    r = _r(_condors(8))
    assert r["avg_trades_per_year"] == 32.0  # would be FULL credit on the leg count
    assert compute_fitness("consistent_annual_return", r) == LOW_TRADE_SENTINEL


def test_the_ramp_itself_is_measured_in_structures():
    """Above the floor the RAMP must also count bets: 20 condors = 80 legs (long past the
    30/yr full-credit point on legs) earns 20/30 of the credit, not all of it."""
    r = _r(_condors(20))
    assert r["avg_trades_per_year"] == 80.0
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(30.0 * (20.0 / 30.0))


def test_thirty_condors_a_year_still_earns_full_credit():
    """The fix must not make the gate unreachable for a genuinely active option genome."""
    assert compute_fitness("consistent_annual_return", _r(_condors(30))) == pytest.approx(30.0)


def test_an_equity_only_run_is_byte_identical():
    """Every equity trade is its own structure, so the equity path must not move at all."""
    trades = [_equity_row(f"S{i}") for i in range(20)]
    with_trades = compute_fitness("consistent_annual_return", _r(trades))
    without = compute_fitness("consistent_annual_return", {
        "total_trades": 20, "avg_trades_per_year": 20.0, "annualized_return": 30.0,
        "max_drawdown": -20.0, "equity_curve": _CURVE})
    assert with_trades == without == pytest.approx(30.0 * (20.0 / 30.0))


def test_a_results_blob_with_no_trades_list_keeps_the_leg_rate():
    """A re-scored DB row has no ``trades``. Falling back to the published rate is today's
    behaviour and must be preserved (degraded, not broken) — the 20+ existing CAR tests all
    take this path."""
    r = {"total_trades": 20, "avg_trades_per_year": 20.0, "annualized_return": 30.0,
         "max_drawdown": -20.0}
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(30.0 * (20.0 / 30.0))


def test_the_rate_is_scaled_not_recomputed_from_the_curve():
    """The structure rate is (structures / legs) x the published leg rate, so a multi-year run
    keeps its real calendar denominator instead of silently re-deriving one."""
    # 40 legs = 10 condors over 4 years -> 10 legs/yr published, 2.5 structures/yr.
    r = _r(_condors(10), avg_trades_per_year=10.0)
    assert compute_fitness("consistent_annual_return", r) == LOW_TRADE_SENTINEL
    # Same legs, same structures, but a run credited with 80 legs/yr -> 20 structures/yr.
    r2 = _r(_condors(10), avg_trades_per_year=80.0)
    assert compute_fitness("consistent_annual_return", r2) == pytest.approx(30.0 * (20.0 / 30.0))


# ---------------------------------------------------------------------------
# 3. The other consumer of the rate
# ---------------------------------------------------------------------------
def test_fitness_trade_scale_also_counts_structures():
    """``fitness_trade_scale`` multiplies by min(rate, cap)/target. It inherits the same leg
    inflation, so a condor book buys 4x the frequency credit it earned."""
    r = _r(_condors(5), total_return=100.0, fitness_trade_scale=True,
           fitness_trade_scale_cap=100.0, fitness_trade_scale_target=100.0)
    r["total_return"] = 100.0
    # 20 legs -> 5 structures. Scale must be 5/100, not 20/100.
    assert compute_fitness("total_return", r) == pytest.approx(100.0 * (5.0 / 100.0))
