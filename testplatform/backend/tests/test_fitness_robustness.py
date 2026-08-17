"""Robustness-adjusted fitness: concentration + monte carlo + spread, opt-in, decomposable.

WHY THIS EXISTS (2026-08-16). The goal2020 FMPRating corpus was audited by hand: 81 of 84 results
had their top-5 trades supplying more than 40% of net P&L, 23 of them more than 100% (i.e. the rest
of the book LOST money), and the small-band headline CARs were routinely one position at 76-79% of
net. A raw CAR ranking cannot see any of that -- it rewards a big winner that will not repeat.

The FIRST test is the no-op guarantee, exactly as in test_fitness_spread_stress.py: with the flag
off, fitness must be bit-identical to what it was before this feature existed, or every stored
ga_fitness silently changes meaning (the 2026-08-04 dd_guard rescale is the cautionary case).
"""
import pytest

from app.services.strategy_fitness import (
    LOW_TRADE_SENTINEL,
    WIPED_OUT_SENTINEL,
    ZERO_TRADE_SENTINEL,
    compute_fitness,
    robust_fitness,
    robustness_metrics,
)


def _run(pnls, years=4, initial=100_000.0):
    """A synthetic run from an explicit per-trade pnl list, so concentration is exactly known.

    pnl_pct is derived from pnl against the running equity, so the monte-carlo screen (which
    resamples pnl_pct) sees a sequence consistent with the pnl list the concentration screen reads.
    A fixture where the two disagree would let one screen pass while the other is measuring
    something else entirely.
    """
    trades, curve = [], []
    equity = initial
    per_year = max(1, len(pnls) // years)
    for i, p in enumerate(pnls):
        year = 2020 + min(i // per_year, years - 1)
        month = (i % per_year) * 12 // per_year + 1
        ts = f"{year}-{month:02d}-15T15:00:00"
        trades.append({"pnl": float(p), "pnl_pct": 100.0 * float(p) / equity,
                       "entry_price": 100.0, "size": 200.0, "exit_time": ts,
                       "direction": "long"})
        equity += float(p)
        curve.append({"date": ts, "equity": equity})
    total_return = (equity / initial - 1.0) * 100.0
    annualized = ((max(equity, 1.0) / initial) ** (1.0 / years) - 1.0) * 100.0
    return {
        "total_trades": len(pnls),
        "winning_trades": sum(1 for p in pnls if p > 0),
        "losing_trades": sum(1 for p in pnls if p <= 0),
        "win_rate": 100.0 * sum(1 for p in pnls if p > 0) / max(1, len(pnls)),
        "annualized_return": annualized, "max_drawdown": -10.0,
        "total_return": total_return, "sharpe_ratio": 1.5, "calmar_ratio": 2.5,
        "avg_trades_per_year": len(pnls) / years, "initial_capital": initial,
        "trades": trades, "equity_curve": curve, "stress_spread_bps": 0.0,
    }


def _even(n=120, each=500.0):
    """A diversified book: n equal winners, so top5 is 5/n of net -- nothing to penalise."""
    return _run([each] * n)


def _concentrated(n=120):
    """One trade supplies ~80% of net P&L. This is the shape the audit kept finding."""
    rest = [100.0] * (n - 1)          # 11,900 total
    return _run([48_000.0] + rest)    # top1 ~80% of 59,900


# ---------------------------------------------------------------------------------------
# The no-op guarantee
# ---------------------------------------------------------------------------------------
def test_off_by_default_is_bit_identical():
    """Flag off -> the historical value, unchanged. Guards the whole stored corpus."""
    r = _even()
    before = compute_fitness("consistent_annual_return", r)
    after = compute_fitness("consistent_annual_return", r, robust=False)
    assert before == after


def test_results_flag_drives_it_when_the_argument_is_absent():
    """The flag reaches remote workers and top-N re-runs through `results`, not an argument --
    same mechanism as stress_spread_bps. If this regresses, a run advertised as robustness-ranked
    scores raw on every path except the master's."""
    r = _concentrated()
    raw = compute_fitness("consistent_annual_return", r)
    r["robust_fitness"] = True
    adj = compute_fitness("consistent_annual_return", r)
    assert adj < raw


# ---------------------------------------------------------------------------------------
# Both views are stored
# ---------------------------------------------------------------------------------------
def test_both_numbers_are_recorded_on_the_results_dict():
    r = _concentrated()
    r["robust_fitness"] = True
    adj = compute_fitness("consistent_annual_return", r)
    assert r["fitness_robust"] == adj
    assert r["fitness_raw"] > adj, "the raw view must survive alongside the adjusted one"
    comp = r["robustness"]
    for k in ("top1_pct", "top5_pct", "mc_p5", "mc_prob_neg",
              "conc_factor", "mc_factor", "spread_factor"):
        assert k in comp, f"{k} missing -- the score would not be decomposable"


def test_raw_is_recorded_even_when_the_adjustment_is_off():
    """So a row from a non-robust run is still comparable to one from a robust run's RAW view."""
    r = _even()
    compute_fitness("consistent_annual_return", r)
    assert r["fitness_raw"] is not None
    assert r["fitness_robust"] is None


# ---------------------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------------------
def test_a_big_unrepeatable_winner_is_not_rewarded():
    """The stated goal: catching a big winner is fine, being RANKED on it is not.

    Same net profit, same trade count, same window -- only the distribution differs. The
    concentrated book must score strictly worse."""
    conc, even = _concentrated(), _even()
    # make the headline metric comparable: both must have a positive raw fitness to start
    assert compute_fitness("consistent_annual_return", conc) > 0
    c_adj, c_comp = robust_fitness(compute_fitness("consistent_annual_return", conc), conc)
    e_adj, e_comp = robust_fitness(compute_fitness("consistent_annual_return", even), even)
    assert c_comp["top1_pct"] > 70.0
    assert e_comp["top5_pct"] < 40.0
    assert e_comp["conc_factor"] == 1.0, "a diversified book must not be penalised at all"
    assert c_comp["conc_factor"] < 0.5
    assert c_adj < c_comp["conc_factor"] * 1.01 * compute_fitness("consistent_annual_return", conc)


def test_missing_equity_curve_raises_instead_of_inflating_silently():
    """THE trap that produced a 4x overstatement on 2026-08-16.

    `backtests.results` excludes the equity curve (own column), so re-scoring a stored row by
    handing the blob straight back gives no curve -- and the consistency factor then defaults to
    1.0, i.e. no penalty, silently. The measured damage was 19.04 reported against a true 4.76.
    A missing KEY must be loud; a curve that is merely too short stays at the documented 1.0.
    """
    r = _even()
    del r["equity_curve"]
    with pytest.raises(ValueError, match="equity_curve"):
        compute_fitness("consistent_annual_return", r)


def test_a_short_but_present_curve_is_still_allowed():
    """Only the ABSENT key is an error -- a sub-year run legitimately carries no consistency
    information and must keep scoring."""
    r = _even()
    r["equity_curve"] = [{"date": "2022-01-15T15:00:00", "equity": 100_000.0}]
    compute_fitness("consistent_annual_return", r)     # must not raise


def test_rest_of_book_negative_cannot_win_however_high_its_return():
    """top5 >= 100% means the book is net NEGATIVE without its five best trades. Measured: the
    Monte Carlo does NOT catch this (bt 918 scored mc 0.95 while being -1,777 ex-top-5), so the
    concentration screen is the only one that can, and a mere floor was not enough -- bt 918's
    raw fitness was 37% higher than its cleaner rival and it won the band anyway."""
    lottery = _run([48_000.0] + [-60.0] * 120)      # top5 > 100%, huge headline
    clean = _run([700.0] * 120)                     # modest, diversified
    l_adj, l_c = robust_fitness(compute_fitness("consistent_annual_return", lottery), lottery)
    c_adj, c_c = robust_fitness(compute_fitness("consistent_annual_return", clean), clean)
    assert l_c["top5_pct"] > 100.0
    assert l_c["conc_factor"] == 0.0, "negative ex-top-5 leaves nothing to rank"
    assert l_adj < c_adj, "a book that loses money ex-top-5 must never outrank a clean one"


def test_the_penalty_is_continuous_with_no_cliff():
    """No discontinuity anywhere: two books a hair apart in concentration must score a hair apart.

    Both earlier shapes failed this -- a hard zero at 100%, then a hard gate at 90% -- and a cliff
    is what lets a genome's score collapse on a rounding error rather than on its merits.
    """
    def factor(top5_target):
        top5 = 200.0 * top5_target / 100.0
        return robustness_metrics(_run([top5 / 5] * 5 + [(200.0 - top5) / 60] * 60))["conc_factor"]

    for t in (55, 70, 85, 89):
        a, b = factor(t), factor(t + 1)
        assert abs(a - b) < 0.10, f"jump at {t}% -> {t+1}%: {a:.3f} vs {b:.3f} (cliff)"


def test_fitness_tends_to_zero_as_top5_tends_to_100():
    """The stated requirement: rip the score off as the book approaches 'negative without its top
    five trades', but get there smoothly."""
    def factor(top5_target):
        top5 = 200.0 * top5_target / 100.0
        return robustness_metrics(_run([top5 / 5] * 5 + [(200.0 - top5) / 60] * 60))["conc_factor"]

    # >=90% of the score removed by the 90% mark. The exact value tracks BT_CONC_EXP (0.068 at
    # the configured 1.5, 0.028 at 2.0) -- the REQUIREMENT is the haircut, not the constant, so
    # this asserts the property and not a number that silently pins the exponent.
    assert factor(90) < 0.10, "by 90% at least 90% of the score must be gone"
    assert factor(95) < factor(90) < factor(80) < factor(65), "strictly decreasing"
    assert factor(101) == 0.0, "at/after the sign change there is nothing left to rank"


def test_monte_carlo_is_deterministic():
    """A seeded bootstrap. A GA cannot search on a fitness that changes between evaluations of
    the same genome -- it would reward noise."""
    r = _concentrated()
    assert robustness_metrics(r) == robustness_metrics(r)


def test_a_coin_flip_book_is_punished_by_the_monte_carlo_screen():
    """Barely-positive expectancy: many reorderings end underwater, so the path was luck."""
    lucky = _run(([1_000.0, -950.0] * 40) + [1_200.0])
    comp = robustness_metrics(lucky)
    assert comp["mc_prob_neg"] > 0.0
    assert comp["mc_factor"] < 1.0


# ---------------------------------------------------------------------------------------
# Spread
# ---------------------------------------------------------------------------------------
def test_spread_factor_is_inert_at_zero_bps():
    comp = robustness_metrics(_even(), spread_bps=0.0)
    assert comp["spread_factor"] == 1.0
    assert comp["spread_keep_pct"] is None


def test_spread_factor_bites_when_the_edge_is_thin():
    thin = _run([40.0] * 200)      # tiny gain per trade against a 20k position
    comp = robustness_metrics(thin, spread_bps=40.0)
    assert comp["spread_factor"] < 1.0


# ---------------------------------------------------------------------------------------
# Sentinels and sign
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("sentinel", [ZERO_TRADE_SENTINEL, LOW_TRADE_SENTINEL, WIPED_OUT_SENTINEL])
def test_sentinels_pass_through_untouched(sentinel):
    """Sentinels are ORDERING markers, not scores. Multiplying one by 0.3 would move a wiped-out
    genome above a zero-trade one and silently reorder the bottom of the population."""
    adj, _ = robust_fitness(sentinel, _concentrated())
    assert adj == sentinel


def test_a_losing_genome_is_never_improved_by_the_adjustment():
    """Scaling a negative fitness toward zero would PROMOTE it -- the same sign trap the
    trade-frequency scale documents."""
    adj, _ = robust_fitness(-12.5, _concentrated())
    assert adj == -12.5


def test_a_run_with_too_few_trades_is_left_alone():
    """No distribution to measure -> factors stay 1.0 rather than fabricating a penalty."""
    comp = robustness_metrics(_run([500.0]))
    assert (comp["conc_factor"], comp["mc_factor"], comp["spread_factor"]) == (1.0, 1.0, 1.0)


# ---------------------------------------------------------------------------------------
# The whitelist trap
# ---------------------------------------------------------------------------------------
def test_robust_flag_survives_the_trial_config_whitelist():
    """_build_daily_trial_config rebuilds the per-trial config KEY BY KEY. A knob missing there is
    inert while every log upstream still claims the run is robustness-ranked -- exactly how the
    first stressed grid scored every job unstressed."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    cfg = _build_daily_trial_config(
        {"robust_fitness": True,
         "start_date": "2022-01-01", "end_date": "2025-12-31",
         "initial_capital": 100000.0, "backtest_id": "t-robust",
         "experts": [{"class": "FMPRating", "settings": {}}],
         "enabled_instruments": ["AAPL"], "account_settings": {},
         "warmup_days": 30, "seed": 42},
        {}, {})
    assert cfg.get("robust_fitness") is True


# ---------------------------------------------------------------------------------------
# The worker contract (this is what makes a REMOTE trial as inspectable as a local one)
# ---------------------------------------------------------------------------------------
def _fake_results():
    """Enough of a run for compute_fitness + the robustness screens to be meaningful."""
    r = _concentrated()
    r["robust_fitness"] = True
    return r


def test_worker_returns_both_views(monkeypatch):
    """`_trial_worker` runs INSIDE the pool worker -- local or remote, it is the same function,
    and its return dict is what the remote worker JSON-serializes back verbatim. If it carried
    only the ranked scalar, a remote-run genome would arrive at the master as a discounted number
    with no raw value and no components: unexplainable after the fact, and unreproducible without
    re-running the trial on the master."""
    from app.services import strategy_optimization_handler as H

    monkeypatch.setattr(
        "app.services.backtest.daily_backtest_handler.run_daily_backtest",
        lambda cfg, **kw: _fake_results(),
    )
    out = H._trial_worker({"backtest_id": 1}, "consistent_annual_return")
    assert out["ok"] is True
    assert out["fitness_raw"] is not None
    assert out["fitness"] < out["fitness_raw"], "the ranked value must be the ADJUSTED one"
    assert out["robustness"]["conc_factor"] < 1.0


def test_worker_payload_is_json_serialisable():
    """The remote path returns this dict through FastAPI as JSON. A numpy float32 or an ndarray
    in the robustness block would 500 the poller on the worker, killing the trial for a reason
    that has nothing to do with the genome."""
    import json
    comp = robustness_metrics(_concentrated(), spread_bps=40.0)
    json.dumps(comp)   # raises TypeError if any numpy scalar leaked out


def test_worker_omits_nothing_when_robustness_is_off(monkeypatch):
    """Flag off -> fitness_raw still travels (so both views exist on every path) and robustness
    is None rather than a fabricated all-ones block."""
    from app.services import strategy_optimization_handler as H

    monkeypatch.setattr(
        "app.services.backtest.daily_backtest_handler.run_daily_backtest",
        lambda cfg, **kw: _even(),
    )
    out = H._trial_worker({"backtest_id": 1}, "consistent_annual_return")
    assert out["fitness_raw"] == out["fitness"]
    assert out["robustness"] is None
