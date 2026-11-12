"""Per-calendar-year trade floor of consistent_annual_return (design 2026-11-10, section 1)."""
import pytest

from app.services.strategy_fitness import (
    _calendar_year_returns,
    _calendar_year_segments,
    _year_gate,
    compute_fitness,
)

# two full years, equal 30%/yr returns -> consistency 1.0
CURVE_2Y = [
    {"date": "2022-01-03", "equity": 100_000.0},
    {"date": "2022-12-30", "equity": 130_000.0},
    {"date": "2023-12-29", "equity": 169_000.0},
]


def _trade(entry, tid, contract=None):
    t = {"entry_time": entry, "exit_time": entry, "pnl": 1.0, "transaction_id": tid}
    if contract:
        t["contract_symbol"] = contract
    return t


def _trades(year, n, start_id):
    return [_trade(f"{year}-03-{1 + (i % 27):02d}", start_id + i) for i in range(n)]


def _r(trades, curve=CURVE_2Y, **kw):
    base = {"total_trades": len(trades), "avg_trades_per_year": 100.0,
            "annualized_return": 30.0, "max_drawdown": -20.0,
            "equity_curve": curve, "trades": trades}
    base.update(kw)
    return base


def test_balanced_years_full_score():
    r = _r(_trades(2022, 14, 0) + _trades(2023, 12, 100))
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(30.0)
    assert r["fitness_year_gate"] == 1.0


def test_silent_year_cut_to_zero():
    r = _r(_trades(2022, 40, 0))
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(0.0)
    assert r["fitness_year_gate"] == 0.0
    assert [b["structures"] for b in r["trades_by_year"]] == [40, 0]


def test_weakest_year_at_half_floor():
    r = _r(_trades(2022, 30, 0) + _trades(2023, 6, 100))
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(15.0)
    assert r["fitness_year_gate"] == pytest.approx(0.5)


def test_results_keys_written():
    r = _r(_trades(2022, 12, 0) + _trades(2023, 12, 100))
    compute_fitness("consistent_annual_return", r)
    assert r["trades_by_year"][0]["label"] == "2022"
    assert r["trades_by_year"][1]["label"] == "2023"
    assert "fitness_year_gate" in r


def test_structures_not_legs():
    # 2023: 12 legs of 3 option structures (4 legs each) = 3 structures -> 0.25
    legs = [_trade("2023-04-03", 1000 + i // 4, contract=f"C{i}") for i in range(12)]
    r = _r(_trades(2022, 20, 0) + legs)
    compute_fitness("consistent_annual_return", r)
    assert r["trades_by_year"][1]["structures"] == 3
    assert r["fitness_year_gate"] == pytest.approx(0.25)


def test_negative_base_unfactored():
    r = _r(_trades(2022, 40, 0), annualized_return=-5.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(-5.0)


def test_override_setting():
    trades = _trades(2022, 10, 0) + _trades(2023, 10, 100)
    assert compute_fitness("consistent_annual_return", _r(trades)) == pytest.approx(30.0 * 10 / 12)
    r = _r(trades, car_min_trades_each_year=20.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(15.0)
    r = _r(trades, car_min_trades_each_year=5.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(30.0)


def test_partial_first_year_merged_into_next():
    # Nov-Dec 2021 stub (< 6 months) merges into 2022: ONE bucket spanning ~1.16 years
    curve = [{"date": "2021-11-01", "equity": 100_000.0},
             {"date": "2021-12-30", "equity": 101_000.0},
             {"date": "2022-12-30", "equity": 130_000.0},
             {"date": "2023-12-29", "equity": 169_000.0}]
    r = _r(_trades(2021, 2, 0) + _trades(2022, 8, 50) + _trades(2023, 12, 100), curve=curve)
    compute_fitness("consistent_annual_return", r)
    b = r["trades_by_year"]
    assert [x["label"] for x in b] == ["2021-2022", "2023"]
    assert b[0]["structures"] == 10
    assert b[0]["years"] == pytest.approx(1.16, abs=0.02)
    assert r["fitness_year_gate"] == pytest.approx(10 / (12 * b[0]["years"]))


def test_partial_last_year_over_six_months_prorated():
    curve = [{"date": "2022-01-03", "equity": 100_000.0},
             {"date": "2022-12-30", "equity": 130_000.0},
             {"date": "2023-09-29", "equity": 150_000.0}]
    r = _r(_trades(2022, 12, 0) + _trades(2023, 6, 100), curve=curve)
    compute_fitness("consistent_annual_return", r)
    b = r["trades_by_year"]
    assert b[1]["years"] == pytest.approx(0.75, abs=0.01)
    assert r["fitness_year_gate"] == pytest.approx(6 / (12 * b[1]["years"]), abs=1e-3)


def test_segments_shared_with_returns():
    segs = _calendar_year_segments(CURVE_2Y)
    assert len(segs) == 2
    assert _calendar_year_returns(CURVE_2Y) == pytest.approx([30.0, 30.0])


def test_no_trade_list_means_gate_not_applied():
    r = {"total_trades": 300, "avg_trades_per_year": 100.0, "annualized_return": 30.0,
         "max_drawdown": -20.0}
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(30.0)
    assert r["fitness_year_gate"] is None


def test_rescore_uses_stored_buckets_without_trades():
    r = _r(_trades(2022, 30, 0) + _trades(2023, 6, 100))
    compute_fitness("consistent_annual_return", r)
    stored = {k: v for k, v in r.items() if k != "trades"}
    assert _year_gate(stored) == pytest.approx(0.5)


def test_unparseable_entry_time_raises():
    r = _r([{"entry_time": "garbage", "transaction_id": 1}] * 40)
    with pytest.raises(ValueError):
        compute_fitness("consistent_annual_return", r)


def _stress_trade(entry, exit_, tid):
    return {"entry_time": entry, "exit_time": exit_, "pnl": 300.0, "pnl_pct": 0.3,
            "entry_price": 100.0, "exit_price": 103.0, "quantity": 100.0, "transaction_id": tid}


def test_stressed_pass_keeps_the_runs_year_buckets():
    """The spread stress changes no trade, so its year gate is the unstressed run's (2026-11-11).

    Two exits five minutes apart used to make a zero-length bucket on the curve the stress pass
    synthesizes from trade exits (ZeroDivisionError), and a silent year vanished from that curve.
    """
    trades = [_stress_trade("2022-03-01T10:00:00", "2022-03-02T10:00:00", 1),
              _stress_trade("2022-03-01T10:05:00", "2022-03-02T10:05:00", 2)]
    r = _r(trades, initial_capital=100_000.0, stress_spread_bps=4.0, avg_trades_per_year=100.0)
    fit = compute_fitness("consistent_annual_return", r, 4.0)
    assert fit == pytest.approx(0.0)  # 2023 is silent in the run -> gate 0 in both passes
    assert [b["structures"] for b in r["trades_by_year"]] == [2, 0]
