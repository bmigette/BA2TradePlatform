"""Equity/drawdown curve downsampling for the backtest detail response (UI charting).

A dense (e.g. 3yr × 5min) run produces ~58k curve points, which froze the recharts AreaChart on
load. ``Backtest.to_dict`` thins the curves to ~``_CHART_MAX_POINTS`` for display while the full
curves stay in the DB columns + CSV/JSON export.
"""
from app.models.backtest import _downsample_curves, _lttb_indices, _CHART_MAX_POINTS


def _curves(n, trough_at=None):
    eq = [{"date": f"2024-{i:06d}", "equity": 100000 + i * 0.1} for i in range(n)]
    dd = []
    peak = None
    for i, p in enumerate(eq):
        v = -45.0 if i == trough_at else -(i % 7) * 0.5
        dd.append({"date": p["date"], "drawdown": v})
    return eq, dd


def test_small_curve_unchanged():
    eq, dd = _curves(500)
    e2, d2 = _downsample_curves(eq, dd)
    assert e2 is eq and d2 is dd  # below threshold -> returned as-is


def test_downsamples_to_target():
    eq, dd = _curves(58000)
    e2, d2 = _downsample_curves(eq, dd, target=2000)
    assert len(e2) <= 2000 and len(d2) == len(e2)
    assert len(e2) >= 1990  # close to target


def test_preserves_endpoints_and_alignment():
    eq, dd = _curves(20000)
    e2, d2 = _downsample_curves(eq, dd, target=1500)
    assert e2[0] == eq[0] and e2[-1] == eq[-1]
    # equity & drawdown stay index-aligned on date
    assert all(a["date"] == b["date"] for a, b in zip(e2, d2))
    # dates remain strictly increasing
    assert all(e2[i]["date"] < e2[i + 1]["date"] for i in range(len(e2) - 1))


def test_keeps_max_drawdown_trough():
    eq, dd = _curves(58000, trough_at=40000)
    _e2, d2 = _downsample_curves(eq, dd, target=2000)
    worst = min(d2, key=lambda p: p["drawdown"])
    assert worst["drawdown"] == -45.0  # the worst-drawdown point is never dropped


def test_lttb_indices_bounds():
    idx = _lttb_indices([float(i) for i in range(10000)], 500)
    assert idx[0] == 0 and idx[-1] == 9999
    assert idx == sorted(idx) and len(set(idx)) == len(idx)  # sorted + unique
    assert 499 <= len(idx) <= 500  # target (minus at most a deduped trailing endpoint)


def test_default_threshold_is_reasonable():
    assert 500 <= _CHART_MAX_POINTS <= 5000
