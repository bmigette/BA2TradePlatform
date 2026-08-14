"""Spread-stress fitness: off by default, penalty-only when on, config-driven.

The critical property is the FIRST test: with the stress off, fitness must be bit-identical
to what it was before this feature existed. Every historical score, every persisted
ga_fitness and the whole running grid depend on that -- a non-zero default would silently
rescale the entire results corpus (the 2026-08-04 dd_guard change is the cautionary case).
"""
import pytest

from app.services.strategy_fitness import (
    LOW_TRADE_SENTINEL,
    WIPED_OUT_SENTINEL,
    ZERO_TRADE_SENTINEL,
    compute_fitness,
    stressed_results,
)


def _results(n_trades=200, pnl_pct=0.30, entry=100.0, size=200.0, years=4):
    """A synthetic run: n_trades evenly spread over `years`, each the same size.

    The declared metrics are DERIVED from the synthetic trades rather than asserted
    independently. A fixture whose headline CAR disagrees with its own trade list makes the
    stress pass look inert -- min(base, stressed) correctly returns the base when the
    recomputed path is better than the declared one, which would silently pass a test that
    is supposed to prove the stress bites.
    """
    # size x entry = 20,000 against 100k equity -> 20% exposure, so a 40bps round trip costs
    # 0.16% of equity per trade. With a 1% exposure fixture the spread would cost 0.008% and
    # the stress would be invisible -- the test would pass while proving nothing.
    initial = 100_000.0
    trades, curve = [], []
    equity = initial
    per_year = max(1, n_trades // years)
    for i in range(n_trades):
        year = 2020 + min(i // per_year, years - 1)
        month = (i % per_year) * 12 // per_year + 1
        ts = f"{year}-{month:02d}-15T15:00:00"
        trades.append({"pnl_pct": pnl_pct, "pnl": equity * pnl_pct / 100.0,
                       "entry_price": entry, "size": size, "exit_time": ts,
                       "direction": "long"})
        equity *= 1 + pnl_pct / 100.0
        curve.append({"date": ts, "equity": equity})

    total_return = (equity / initial - 1.0) * 100.0
    annualized = ((equity / initial) ** (1.0 / years) - 1.0) * 100.0
    return {
        "total_trades": n_trades, "winning_trades": n_trades, "losing_trades": 0,
        "win_rate": 100.0, "annualized_return": annualized, "max_drawdown": -10.0,
        "total_return": total_return, "sharpe_ratio": 1.5, "calmar_ratio": 2.5,
        "avg_trades_per_year": n_trades / years, "initial_capital": initial,
        "trades": trades, "equity_curve": curve, "stress_spread_bps": 0.0,
    }


def test_off_by_default_is_bit_identical():
    """The no-op guarantee. If this fails, every stored fitness has been rescaled."""
    r = _results()
    assert compute_fitness("consistent_annual_return", r) == \
        compute_fitness("consistent_annual_return", r, stress_spread_bps=0.0)


def test_stress_can_only_lower_fitness():
    r = _results()
    base = compute_fitness("consistent_annual_return", r)
    stressed = compute_fitness("consistent_annual_return", r, stress_spread_bps=40.0)
    assert stressed < base, "a wider spread must not improve a genome"


def test_thin_edge_is_punished_far_harder_than_a_fat_one():
    """The whole point: two runs with the SAME headline metrics, different per-trade edge.

    The thin-edge book pays the same spread on a much smaller gain, so it must lose far more
    of its fitness -- this is what a plain CAR ranking cannot see.
    """
    thin = _results(n_trades=400, pnl_pct=0.12)
    fat = _results(n_trades=400, pnl_pct=1.2)
    thin_keep = (compute_fitness("consistent_annual_return", thin, 40.0)
                 / compute_fitness("consistent_annual_return", thin))
    fat_keep = (compute_fitness("consistent_annual_return", fat, 40.0)
                / compute_fitness("consistent_annual_return", fat))
    assert thin_keep < fat_keep


def test_level_is_read_from_the_run_config_when_not_passed():
    """How it reaches remote workers and top-N re-runs: via the config echoed into results,
    not a new argument threaded through the worker protocol."""
    r = _results()
    explicit = compute_fitness("consistent_annual_return", r, stress_spread_bps=40.0)
    r_cfg = dict(r, stress_spread_bps=40.0)
    assert compute_fitness("consistent_annual_return", r_cfg) == explicit


def test_sentinels_are_preserved():
    """A disqualified/wiped-out genome must keep its sentinel RANK, not be replaced by an
    ordinary negative number that would sort above a real losing config."""
    wiped = dict(_results(), account_wiped_out=True)
    assert compute_fitness("consistent_annual_return", wiped, 40.0) == WIPED_OUT_SENTINEL
    empty = dict(_results(), total_trades=0)
    assert compute_fitness("consistent_annual_return", empty, 40.0) == ZERO_TRADE_SENTINEL


def test_falls_back_gracefully_when_the_run_has_no_trades_to_restress():
    r = dict(_results(), trades=[])
    base = compute_fitness("consistent_annual_return", r)
    assert compute_fitness("consistent_annual_return", r, 40.0) == base
    assert stressed_results(r, 40.0) is None


def test_applies_to_non_car_metrics_too():
    r = _results()
    base = compute_fitness("total_return", r)
    assert compute_fitness("total_return", r, stress_spread_bps=40.0) <= base


def test_stressed_results_overwrites_the_adjusted_key_as_well():
    """Under a profit cap the CAR metric reads adjusted_annualized_return. Leaving that at its
    unstressed value would make the stress a silent no-op on the entire grid, which runs with
    caps on by default."""
    base = _results()
    r = dict(base, profit_cap_pct=2000.0, profit_share_cap_pct=25.0,
             adjusted_annualized_return=base["annualized_return"])
    out = stressed_results(r, 40.0)
    assert out is not None
    assert out["adjusted_annualized_return"] == out["annualized_return"]
    assert out["adjusted_annualized_return"] < base["annualized_return"]


# --- the wiring, not just the maths -------------------------------------------------------

def test_stress_level_survives_the_per_trial_config_whitelist():
    """_build_daily_trial_config REBUILDS the config key by key instead of copying it, so a
    knob absent from that whitelist is silently dead however correctly it was parsed upstream.

    That is not hypothetical: the first stressed grid ran with --stress-spread-bps reaching the
    optimize process and the run config, but not the trial config, so every "stressed" job was
    scored unstressed and only the results blob (stress_spread_bps=0.0) gave it away. The unit
    tests above all passed throughout, because they call compute_fitness directly and never
    cross the config boundary.
    """
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    backtest_cfg = {
        "backtest_id": "test-trial", "name": "unit",
        "start_date": "2020-01-01", "end_date": "2025-12-31",
        "initial_capital": 100000.0, "account_settings": {}, "warmup_days": 30, "seed": 42,
        "experts": [{"class": "FMPRating", "settings": {}}],
        "enabled_instruments": ["AAPL"],
        "profit_cap_pct": 2000.0, "profit_share_cap_pct": 25.0,
        "stress_spread_bps": 40.0,
    }
    cfg = _build_daily_trial_config(backtest_cfg, {})
    assert cfg.get("stress_spread_bps") == 40.0, (
        "stress_spread_bps was dropped by the per-trial config whitelist -- the GA would score "
        "every genome unstressed while the CLI, the run config and the logs all claim otherwise")
    # The sibling knobs prove the whitelist itself is working, so a failure above is specific.
    assert cfg.get("profit_share_cap_pct") == 25.0
