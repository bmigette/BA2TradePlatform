"""robust_fitness x stress_spread_bps: the robustness factors apply ONCE, on the outside.

THE DEFECT (fixed 2026-11-09). Every metric branch of ``compute_fitness`` returns
``_maybe_robust(_min_with_stressed(raw, ...), ...)``. ``_min_with_stressed`` re-scores the
``stressed_results`` copy through ``compute_fitness`` with ``robust=None``, and the copy still
carried ``robust_fitness=True``, so the INNER ``_maybe_robust`` multiplied the stressed score by
concentration x Monte Carlo before the min, and the OUTER one multiplied the min by
concentration x Monte Carlo x spread again. Whenever the stressed arm won the min the run scored

    min(base, stressed * conc * mc) * conc * mc * spread      (conc and mc twice)

instead of the documented "concentration x monte-carlo x spread" adjustment of the stress-min:

    min(base, stressed) * conc * mc * spread

Fitness with BOTH flags on is not comparable across this fix (docs/REPRODUCE-BACKTESTS.md
6.3). With either flag off nothing moves, and the pins below hold that byte for byte.

The expected values are COMPUTED from the component functions (``stressed_results``,
``robustness_metrics``) rather than hard-coded, so this asserts the composition, not a number.
"""
import pytest

from app.services.strategy_fitness import compute_fitness, robustness_metrics, stressed_results

STRESS_BPS = 10.0


def _run(pnls, years=4, initial=100_000.0):
    """A synthetic run whose declared CAR and max drawdown agree with its own trade path, so the
    unstressed and stressed passes are scored on the same footing."""
    trades, curve = [], []
    equity = initial
    per_year = max(1, len(pnls) // years)
    for i, p in enumerate(pnls):
        year = 2020 + min(i // per_year, years - 1)
        month = (i % per_year) * 12 // per_year + 1
        ts = f"{year}-{month:02d}-15T15:00:00"
        trades.append({"pnl": float(p), "pnl_pct": 100.0 * float(p) / equity,
                       "entry_price": 100.0, "size": 200.0, "exit_time": ts,
                       "entry_time": ts, "direction": "long"})
        equity += float(p)
        curve.append({"date": ts, "equity": equity})
    peak, mdd = initial, 0.0
    for pt in curve:
        peak = max(peak, pt["equity"])
        mdd = min(mdd, (pt["equity"] - peak) / peak * 100.0)
    annualized = ((max(equity, 1.0) / initial) ** (1.0 / years) - 1.0) * 100.0
    return {
        "total_trades": len(pnls),
        "winning_trades": sum(1 for p in pnls if p > 0),
        "losing_trades": sum(1 for p in pnls if p <= 0),
        "win_rate": 100.0 * sum(1 for p in pnls if p > 0) / max(1, len(pnls)),
        "annualized_return": annualized, "max_drawdown": mdd,
        "total_return": (equity / initial - 1.0) * 100.0, "sharpe_ratio": 1.5,
        "calmar_ratio": 2.5, "avg_trades_per_year": len(pnls) / years,
        "initial_capital": initial, "trades": trades, "equity_curve": curve,
        "stress_spread_bps": 0.0,
    }


def _book():
    """Five large winners over a near-coin-flip body: every factor strictly inside (0, 1)
    (conc ~0.485, mc 0.906, spread ~0.940 at 10 bps) and the stressed arm wins the min, which
    is exactly the region where the factors used to be applied twice."""
    return _run([3000.0, 2500.0, 2000.0, 1500.0, 1200.0] + [900.0, -800.0] * 60)


# (metric, option_structures) -- an equity CAR branch, the generic-key branch, and two option
# branches (which pass option_structures=True to the screen).
CASES = [
    ("consistent_annual_return", False),
    ("total_return", False),
    ("option_consistent_annual_return", True),
    ("option_car_target_soft30", True),
]


def _raw(metric, results):
    """The metric with neither wrapper: no stress, no robustness."""
    return compute_fitness(metric, dict(results, robust_fitness=False, stress_spread_bps=0.0),
                           0.0, robust=False)


@pytest.mark.parametrize("metric,option_structures", CASES)
def test_factors_apply_once_when_the_stressed_arm_wins(metric, option_structures):
    r = _book()
    base_raw = _raw(metric, r)
    stressed_raw = _raw(metric, stressed_results(dict(r), STRESS_BPS))
    comp = robustness_metrics(r, STRESS_BPS, option_structures=option_structures)

    # Preconditions: the test only discriminates if the stressed arm wins and every factor bites.
    assert 0.0 < stressed_raw < base_raw
    assert 0.0 < comp["conc_factor"] < 1.0
    assert 0.0 < comp["mc_factor"] < 1.0
    assert 0.0 < comp["spread_factor"] < 1.0

    expected = stressed_raw * (comp["conc_factor"] * comp["mc_factor"] * comp["spread_factor"])

    run = dict(r, robust_fitness=True, stress_spread_bps=STRESS_BPS)
    got = compute_fitness(metric, run)

    assert got == expected, (
        f"{metric}: {got!r} != stressed_raw x conc x mc x spread = {expected!r} "
        f"(ratio {got / expected:.6f}; conc x mc = "
        f"{comp['conc_factor'] * comp['mc_factor']:.6f} means they were applied twice)")
    # The ORIGINAL dict holds the outer view: raw = the stress-min, robust = the ranked value.
    assert run["fitness_raw"] == stressed_raw
    assert run["fitness_robust"] == got
    assert run["robustness"] == comp


def test_factors_apply_once_when_the_base_arm_wins():
    """The doubling was NOT confined to a binding stress: the inner robustness-adjusted copy beat
    the raw base whenever stressed x conc x mc < base. sharpe_ratio is not restated by
    ``stressed_results`` (stressed == base), so it used to score base x (conc x mc)^2 x spread."""
    metric = "sharpe_ratio"
    r = _book()
    base_raw = _raw(metric, r)
    assert _raw(metric, stressed_results(dict(r), STRESS_BPS)) == base_raw
    comp = robustness_metrics(r, STRESS_BPS)
    run = dict(r, robust_fitness=True, stress_spread_bps=STRESS_BPS)
    got = compute_fitness(metric, run)
    assert got == base_raw * (comp["conc_factor"] * comp["mc_factor"] * comp["spread_factor"])
    assert run["fitness_raw"] == base_raw


@pytest.mark.parametrize("metric,option_structures", CASES)
def test_explicit_arguments_compose_the_same_way(metric, option_structures):
    """The flags passed as arguments (master path) and read off the results (worker/top-N path)
    must give the same number."""
    r = _book()
    via_results = compute_fitness(metric, dict(r, robust_fitness=True,
                                               stress_spread_bps=STRESS_BPS))
    via_args = compute_fitness(metric, dict(r), STRESS_BPS, robust=True)
    assert via_args == via_results


def test_the_inner_rescore_leaves_the_stressed_copy_unadjusted():
    """The stressed copy must not ask for robustness, the same way it must not ask for stress:
    the outer ``_maybe_robust`` is the only place the factors are applied."""
    r = dict(_book(), robust_fitness=True, stress_spread_bps=STRESS_BPS)
    st = stressed_results(r, STRESS_BPS)
    assert not st.get("robust_fitness")
    assert st["stress_spread_bps"] == 0.0
    assert r["robust_fitness"] is True, "the caller's dict must not be modified"
    assert r["stress_spread_bps"] == STRESS_BPS


# --- byte-identical when either flag is off ----------------------------------------------
# Captured from the implementation BEFORE the fix (worktree base 987fea27). With only one of
# the two flags on, the inner re-score either does not run (stress off) or runs on a copy that
# never asked for robustness (robust off), so nothing here may move.
PINNED = {
    ("consistent_annual_return", "robust_only"): "1.6389385469782631",
    ("consistent_annual_return", "stress_only"): "1.7981775169760073",
    ("consistent_annual_return", "neither"): "3.7299842146420494",
    ("option_consistent_annual_return", "robust_only"): "13.111508375826105",
    ("option_consistent_annual_return", "stress_only"): "14.385420135808058",
    ("option_consistent_annual_return", "neither"): "29.839873717136395",
    ("option_car_target_soft30", "robust_only"): "0.08955400246850291",
    ("option_car_target_soft30", "stress_only"): "0.09238406807308568",
    ("option_car_target_soft30", "neither"): "0.2038118001321017",
}

_FLAGS = {"robust_only": {"robust_fitness": True},
          "stress_only": {"stress_spread_bps": STRESS_BPS},
          "neither": {}}


@pytest.mark.parametrize("key", sorted(PINNED))
def test_one_flag_off_is_byte_identical_to_before(key):
    metric, flags = key
    got = repr(compute_fitness(metric, dict(_book(), **_FLAGS[flags])))
    assert got == PINNED[key]
