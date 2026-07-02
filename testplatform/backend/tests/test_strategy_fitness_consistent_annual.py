"""consistent_annual_return fitness: ~30%/yr EVERY year, >=30 trades/yr, dd soft-cap 20%.

fitness = (adjusted) annualized_return x dd_guard x consistency(worst_year/mean_year).
The external fitness_trade_scale multiplier is a structural no-op for this metric.
"""
import pytest

from app.services.strategy_fitness import (
    ZERO_TRADE_SENTINEL,
    LOW_TRADE_SENTINEL,
    _calendar_year_returns,
    _consistency_factor,
    compute_fitness,
)


def _curve(points):
    return [{"date": d, "equity": e} for d, e in points]


def _r(**kw):
    """A healthy baseline result: 30%/yr, 100 trades/yr, -10% dd, no equity curve
    (fewer than 2 measurable years -> consistency factor 1.0)."""
    base = {
        "total_trades": 300,
        "avg_trades_per_year": 100.0,
        "annualized_return": 30.0,
        "max_drawdown": -10.0,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 1. Calendar-year return computation
# ---------------------------------------------------------------------------
def test_exact_three_calendar_years():
    curve = _curve([
        ("2020-01-02", 100_000.0),
        ("2020-06-30", 110_000.0),
        ("2020-12-31", 130_000.0),
        ("2021-06-30", 150_000.0),
        ("2021-12-31", 169_000.0),
        ("2022-06-30", 190_000.0),
        ("2022-12-30", 219_700.0),
    ])
    yrs = _calendar_year_returns(curve)
    assert yrs == pytest.approx([30.0, 30.0, 30.0])


def test_partial_start_year_under_6_months_merges_into_next():
    # Oct-Dec 2020 stub (~3 months) merges into 2021: 136_500/100_000 - 1 = 36.5%.
    curve = _curve([
        ("2020-10-01", 100_000.0),
        ("2020-12-31", 105_000.0),
        ("2021-06-30", 120_000.0),
        ("2021-12-31", 136_500.0),
        ("2022-12-30", 150_150.0),
    ])
    yrs = _calendar_year_returns(curve)
    assert len(yrs) == 2
    assert yrs[0] == pytest.approx(36.5)
    assert yrs[1] == pytest.approx(10.0)


def test_partial_start_year_over_6_months_kept():
    # Jun-Dec 2020 (~7 months) is its own year.
    curve = _curve([
        ("2020-06-01", 100_000.0),
        ("2020-12-31", 120_000.0),
        ("2021-12-31", 132_000.0),
    ])
    yrs = _calendar_year_returns(curve)
    assert yrs == pytest.approx([20.0, 10.0])


def test_partial_end_year_under_6_months_merges_into_previous():
    # Jan-Feb 2023 stub merges into 2022: 145_200/110_000 - 1 = 32%.
    curve = _curve([
        ("2021-01-04", 100_000.0),
        ("2021-12-31", 110_000.0),
        ("2022-12-30", 143_000.0),
        ("2023-02-15", 145_200.0),
    ])
    yrs = _calendar_year_returns(curve)
    assert len(yrs) == 2
    assert yrs[0] == pytest.approx(10.0)
    assert yrs[1] == pytest.approx(32.0)


def test_short_or_empty_curve_yields_no_years():
    assert _calendar_year_returns(None) == []
    assert _calendar_year_returns([]) == []
    assert _calendar_year_returns(_curve([("2022-01-03", 100.0)])) == []


# ---------------------------------------------------------------------------
# 2. Consistency factor
# ---------------------------------------------------------------------------
def test_equal_years_factor_is_one():
    assert _consistency_factor([30.0, 30.0, 30.0]) == pytest.approx(1.0)


def test_uneven_50_10_50_factor():
    # mean 36.67, worst 10 -> 0.2727 (above the 0.25 floor).
    assert _consistency_factor([50.0, 10.0, 50.0]) == pytest.approx(10.0 / (110.0 / 3.0))


def test_negative_year_with_positive_mean_clamps_to_floor():
    # (40, -10, 60): mean 30, worst/mean = -0.33 -> clamped to 0.25.
    assert _consistency_factor([40.0, -10.0, 60.0]) == pytest.approx(0.25)


def test_negative_mean_factor_is_one():
    # The low/negative base already sinks it; scaling would reward inconsistency.
    assert _consistency_factor([-10.0, -20.0]) == pytest.approx(1.0)


def test_single_year_factor_is_one():
    assert _consistency_factor([30.0]) == pytest.approx(1.0)
    assert _consistency_factor([]) == pytest.approx(1.0)


def test_uneven_years_rank_below_even_years_at_equal_base():
    even = _r(equity_curve=_curve([
        ("2020-01-02", 100_000.0), ("2020-12-31", 130_000.0),
        ("2021-12-31", 169_000.0), ("2022-12-30", 219_700.0),
    ]))
    uneven = _r(equity_curve=_curve([
        ("2020-01-02", 100_000.0), ("2020-12-31", 150_000.0),   # +50%
        ("2021-12-31", 165_000.0),                               # +10%
        ("2022-12-30", 247_500.0),                               # +50%
    ]))
    f_even = compute_fitness("consistent_annual_return", even)
    f_uneven = compute_fitness("consistent_annual_return", uneven)
    assert f_even == pytest.approx(30.0)  # factor 1.0
    assert f_uneven == pytest.approx(30.0 * (10.0 / (110.0 / 3.0)))  # ~8.18
    assert f_uneven < f_even


# ---------------------------------------------------------------------------
# 3. Drawdown guard
# ---------------------------------------------------------------------------
def test_dd_within_20_no_penalty():
    assert compute_fitness("consistent_annual_return", _r(max_drawdown=-12.0)) == pytest.approx(30.0)


def test_dd_exactly_20_no_penalty():
    assert compute_fitness("consistent_annual_return", _r(max_drawdown=-20.0)) == pytest.approx(30.0)


def test_dd_30_soft_penalty():
    assert compute_fitness("consistent_annual_return", _r(max_drawdown=-30.0)) == pytest.approx(30.0 * 20.0 / 30.0)


# ---------------------------------------------------------------------------
# 4. Trade gate (>= 30/yr)
# ---------------------------------------------------------------------------
def test_25_trades_per_year_disqualified():
    assert compute_fitness("consistent_annual_return", _r(avg_trades_per_year=25.0)) == LOW_TRADE_SENTINEL


def test_30_trades_per_year_passes():
    assert compute_fitness("consistent_annual_return", _r(avg_trades_per_year=30.0)) == pytest.approx(30.0)


def test_gate_derives_trades_per_year_from_curve_when_key_missing():
    curve = _curve([("2020-01-02", 100_000.0), ("2022-12-30", 219_700.0)])  # ~3 years
    r = _r(equity_curve=curve, total_trades=95)  # ~31.8/yr -> passes
    r.pop("avg_trades_per_year")
    assert compute_fitness("consistent_annual_return", r) > 0
    r2 = _r(equity_curve=curve, total_trades=60)  # ~20/yr -> disqualified
    r2.pop("avg_trades_per_year")
    assert compute_fitness("consistent_annual_return", r2) == LOW_TRADE_SENTINEL


def test_gate_underivable_disqualifies():
    r = _r()
    r.pop("avg_trades_per_year")  # no key, no equity curve -> no hidden default
    assert compute_fitness("consistent_annual_return", r) == LOW_TRADE_SENTINEL


def test_sentinels_are_distinct_and_ordered():
    # no-trade (existing top-of-function guard) is WORSE than a below-floor config.
    assert compute_fitness("consistent_annual_return", _r(total_trades=0)) == ZERO_TRADE_SENTINEL
    assert ZERO_TRADE_SENTINEL < LOW_TRADE_SENTINEL < 0.0


# ---------------------------------------------------------------------------
# 5. Adjusted-base switch under profit caps
# ---------------------------------------------------------------------------
def test_profit_cap_switches_base_to_adjusted():
    r = _r(annualized_return=80.0, adjusted_annualized_return=40.0, profit_cap_pct=2000.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(40.0)


def test_share_cap_also_switches_base_to_adjusted():
    r = _r(annualized_return=80.0, adjusted_annualized_return=40.0, profit_share_cap_pct=25.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(40.0)


def test_no_cap_uses_raw_annualized_return():
    r = _r(annualized_return=80.0, adjusted_annualized_return=40.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# 6. fitness_trade_scale is a no-op for this metric
# ---------------------------------------------------------------------------
def test_trade_scale_is_noop():
    # 50/yr would scale calmar-style fitness x0.5; this metric must be unaffected.
    r = _r(avg_trades_per_year=50.0, fitness_trade_scale=True, fitness_trade_scale_cap=100.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(30.0)
    assert compute_fitness("consistent_annual_return", r) == compute_fitness(
        "consistent_annual_return", _r(avg_trades_per_year=50.0))


# ---------------------------------------------------------------------------
# Misc: aliases, negative base, unknown-metric error message
# ---------------------------------------------------------------------------
def test_aliases_and_case():
    for name in ("consistent_annual_return", "car", "goal", "CAR", "Goal"):
        assert compute_fitness(name, _r()) == pytest.approx(30.0)


def test_negative_base_returned_unfactored():
    # penalties on a negative would IMPROVE it -> returned as-is (still gated on trades).
    r = _r(annualized_return=-15.0, max_drawdown=-35.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(-15.0)


def test_unknown_metric_error_lists_new_metric():
    with pytest.raises(ValueError) as ei:
        compute_fitness("not_a_metric", {"total_trades": 1})
    assert "consistent_annual_return" in str(ei.value)
