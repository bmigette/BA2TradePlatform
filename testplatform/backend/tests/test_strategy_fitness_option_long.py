"""``option_long`` -- the long-horizon option-book fitness (owner-approved 2026-11-07).

Formula, in this literal order: wipeout sentinel -> unreadable return -> return term
(``total_return``, as ``option_convex``) -> DD factor (free <= 25%, linear to 0 at 60%) ->
negative-calendar-year factor (product of max(0.5, 1 + r/100) over losing years) -> breadth
floor (>= 5 structures/yr AND >= 8 underlyings) -> a non-positive return passes through RAW.
"""
import os
import sys

import pytest

from app.services.strategy_fitness import (
    LOW_TRADE_SENTINEL,
    METRICS_CATALOG,
    WIPED_OUT_SENTINEL,
    ZERO_TRADE_SENTINEL,
    assert_catalog_complete,
    compute_fitness,
    scores_option_structures,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))
import ba2test_launcher as L  # noqa: E402
from tests.test_option_style_registration import _with_style  # noqa: E402

LONG = "option_long"


def _trades(n=36, n_under=12):
    return [{"symbol": f"U{i % n_under:02d}", "underlying_symbol": f"U{i % n_under:02d}",
             "pnl": 10.0, "pnl_pct": 1.0, "exit_time": f"2023-{1 + i % 12:02d}-15"}
            for i in range(n)]


def _curve(*year_end_levels, start=100.0):
    """Equity points: an opening point and one 31-Dec point per year starting 2021."""
    pts = [{"date": "2021-01-04", "equity": start}]
    for k, lvl in enumerate(year_end_levels):
        pts.append({"date": f"{2021 + k}-12-31", "equity": lvl})
    return pts


def _res(ret=50.0, dd=-10.0, curve=None, n=36, n_under=12, tpy=None, **kw):
    r = {"total_return": ret, "max_drawdown": dd, "total_trades": n,
         "avg_trades_per_year": float(n) / 3.0 if tpy is None else tpy,
         "trades": _trades(n, n_under),
         "equity_curve": curve if curve is not None else _curve(110, 125, 150)}
    r.update(kw)
    return r


def _score(**kw):
    return compute_fitness(LONG, _res(**kw))


def test_wipeout_is_worst_and_checked_first():
    # unreadable return AND thin book would otherwise return a higher sentinel
    assert _score(dd=-100.0, ret=None, n=1, n_under=1) == WIPED_OUT_SENTINEL
    assert _score(dd=-150.0) == WIPED_OUT_SENTINEL
    assert WIPED_OUT_SENTINEL < ZERO_TRADE_SENTINEL < LOW_TRADE_SENTINEL


def test_zero_trade_and_unreadable_return():
    assert compute_fitness(LONG, None) == ZERO_TRADE_SENTINEL
    assert compute_fitness(LONG, _res(n=0)) == ZERO_TRADE_SENTINEL
    assert _score(ret=None) == ZERO_TRADE_SENTINEL


def test_missing_drawdown_raises():
    r = _res()
    r["max_drawdown"] = None
    with pytest.raises(ValueError):
        compute_fitness(LONG, r)


@pytest.mark.parametrize("dd,factor", [(-20, 1.0), (-25, 1.0), (-42.5, 0.5), (-60, 0.0),
                                       (-70, 0.0), (42.5, 0.5)])
def test_dd_factor(dd, factor):
    assert _score(ret=80.0, dd=dd) == pytest.approx(80.0 * factor)


def test_dd_zero_factor_is_a_scored_zero_above_every_disqualification():
    s = _score(ret=80.0, dd=-70.0)
    assert s == 0.0 and s > LOW_TRADE_SENTINEL


def test_one_negative_year_costs_ten_percent():
    # 100 -> 90 (-10%) -> 99 (+10%) -> 120
    s = _score(ret=20.0, curve=_curve(90, 99, 120))
    assert s == pytest.approx(20.0 * 0.9)


def test_negative_year_is_floored_at_half():
    s = _score(ret=20.0, curve=_curve(40, 50, 120))   # -60% year
    assert s == pytest.approx(20.0 * 0.5)


def test_two_negative_years_multiply():
    s = _score(ret=20.0, curve=_curve(90, 81, 120))   # -10%, -10%
    assert s == pytest.approx(20.0 * 0.9 * 0.9)


def test_no_year_series_means_no_invented_factor():
    assert _score(ret=20.0, curve=[]) == pytest.approx(20.0)


def test_breadth_floor_structures_per_year():
    assert _score(tpy=4.9) == LOW_TRADE_SENTINEL
    assert _score(tpy=5.0) > 0


def test_breadth_floor_structures_not_legs():
    # 4-leg structures: 48 leg rows = 12 structures over 3y = 4/yr < 5
    rows = []
    for i in range(48):
        s = i // 4
        rows.append({"symbol": f"U{s % 12:02d}", "underlying_symbol": f"U{s % 12:02d}",
                     "contract_symbol": f"U{s % 12:02d}250117C00100000", "transaction_id": s,
                     "pnl": 1.0, "exit_time": "2023-05-15"})
    r = _res(n=48)
    r["trades"] = rows
    assert compute_fitness(LONG, r) == LOW_TRADE_SENTINEL


def test_breadth_floor_underlyings():
    assert _score(n_under=7) == LOW_TRADE_SENTINEL
    assert _score(n_under=8) > 0


def test_negative_return_passes_through_raw_and_ranks_below_small_positive():
    neg = _score(ret=-12.0, dd=-40.0, curve=_curve(80, 70, 88))
    assert neg == -12.0
    pos = _score(ret=0.5, dd=-59.0)
    assert 0.0 < pos < 1.0 and neg < pos


def test_robustness_factors_applied_under_robust_flag():
    r = _res(ret=50.0)
    r["trades"] = _trades(36, 12)
    r["trades"][0]["pnl"] = 5000.0   # one trade dominates net P&L -> concentration factor < 1
    plain = compute_fitness(LONG, dict(r), robust=False)
    robust_r = dict(r)
    adj = compute_fitness(LONG, robust_r, robust=True)
    assert plain == pytest.approx(50.0)
    assert robust_r["fitness_raw"] == pytest.approx(50.0)
    assert robust_r["robustness"]["conc_factor"] < 1.0
    assert adj < plain


def test_telemetry_recorded_not_scored():
    r = _res(ret=20.0, dd=-30.0, curve=_curve(90, 99, 120))
    s = compute_fitness(LONG, r)
    t = r["option_long_telemetry"]
    for k in ("hit_rate_pct", "top1_pct", "top5_pct", "tickets_per_year", "distinct_underlyings",
              "structures_per_year", "dd_factor", "neg_year_factor", "year_returns"):
        assert k in t
    assert t["dd_factor"] == pytest.approx(1 - 5 / 35)
    assert t["neg_year_factor"] == pytest.approx(0.9)
    assert s == pytest.approx(20.0 * t["dd_factor"] * 0.9)


def test_catalog_lists_option_long_and_is_complete():
    assert_catalog_complete()
    entry = [m for m in METRICS_CATALOG if m["key"] == LONG]
    assert len(entry) == 1 and entry[0]["aliases"] == []
    assert scores_option_structures(LONG)


# --- launcher ---------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _style_reset(monkeypatch):
    monkeypatch.setattr(L, "_STYLE_ACTIVE", None)


def test_launcher_guard_accepts_style_with_convex_member_under_option_long():
    L._STYLE_ACTIVE = _with_style("leaps_convex")
    L._refuse_convex_fitness_mismatch("optimize", "O_STYLE", LONG)   # no SystemExit, no waiver


def test_launcher_guard_still_refuses_other_fitness_without_waiver():
    L._STYLE_ACTIVE = _with_style("leaps_convex")
    with pytest.raises(SystemExit, match="Q5"):
        L._refuse_convex_fitness_mismatch("optimize", "O_STYLE", "option_car_target_soft30")
    with pytest.raises(SystemExit, match="O_CONVEX-only"):
        L._refuse_convex_fitness_mismatch("optimize", "O_STYLE", "option_convex")


def test_option_long_is_never_a_default_and_o_convex_still_needs_option_convex():
    assert L._resolve_fitness(None, "O_STYLE", "calmar_ratio") == "option_consistent_annual_return"
    assert L._resolve_fitness(LONG, "O_STYLE", "calmar_ratio") == LONG
    with pytest.raises(SystemExit):
        L._refuse_convex_fitness_mismatch("optimize", "O_CONVEX", LONG)


# --- option_long_dd40_50 (owner 2026-11-08, $10k job L10): same metric, DD free 40% / 0 at 50% ---
DD4050 = "option_long_dd40_50"


@pytest.mark.parametrize("dd,factor", [(-25, 1.0), (-40, 1.0), (-45, 0.5), (-50, 0.0), (-55, 0.0)])
def test_dd40_50_band(dd, factor):
    assert compute_fitness(DD4050, _res(ret=80.0, dd=dd)) == pytest.approx(80.0 * factor)


def test_dd40_50_is_its_own_catalog_key_and_scores_structures():
    assert_catalog_complete()
    entry = [m for m in METRICS_CATALOG if m["key"] == DD4050]
    assert len(entry) == 1 and entry[0]["aliases"] == []
    assert scores_option_structures(DD4050)


def test_dd40_50_leaves_option_long_unchanged():
    # 30% dd: option_long charges it (1 - 5/35), the 40/50 variant does not
    assert compute_fitness(LONG, _res(ret=70.0, dd=-30)) == pytest.approx(70.0 * (1 - 5 / 35))
    assert compute_fitness(DD4050, _res(ret=70.0, dd=-30)) == pytest.approx(70.0)


def test_launcher_accepts_dd40_50_for_a_style_with_a_convex_member():
    L._STYLE_ACTIVE = _with_style("leaps_convex")
    L._refuse_convex_fitness_mismatch("optimize", "O_STYLE", DD4050)
    with pytest.raises(SystemExit):
        L._refuse_convex_fitness_mismatch("optimize", "O_CONVEX", DD4050)
