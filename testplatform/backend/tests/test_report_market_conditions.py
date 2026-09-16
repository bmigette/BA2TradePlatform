"""``tools/report_market_conditions.py`` -- the per-job market-condition report (design 7).

What is pinned here is the REPORT'S ARITHMETIC and its refusals, on a fabricated optimization
row: the bin edges are the declared ones and nothing lands in a bin it does not belong to, a
value outside the edges is reported as outside rather than folded into the nearest bucket, the
attribution unit is a STRUCTURE and not a leg, concentration is a share of net P&L, the
versions come from the PERSISTED block rather than this process's registry, and no filtered
subset of trades is ever annualised.

The last one is the reason this file exists at all. Every other number here would still be
roughly right if it were slightly wrong; a per-bin "CAR" would be a plausible-looking figure
for a thing that has no capital, and it is exactly the figure a reader would quote.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(_REPO, "tools"))

import report_market_conditions as R  # noqa: E402

SLOPE = "underlying_trend_slope_50_atr14"
ADX = "underlying_adx_14"
RV = "underlying_realized_vol_ratio_5_20"

_BLOCK = {
    "profiles": ["ohlcv-v1"],
    "manifest": "sha256:" + "a" * 64,
    "source_profile": "fmp-daily-split-adjusted-v1",
    "timing_policy": "prior_session_v1",
    "calendar_version": "4.4.1",
    "calc_version": "ohlcv-v1/calc-1",
    "calc_versions": {"ohlcv-v1": "ohlcv-v1/calc-1"},
    "window_start": "2022-01-03",
    "window_end": "2023-12-29",
    "gene_count": 6,
    "genes": ["cond:o_lc-market-adx:mode", "cond:o_lc-market-adx:value"],
    "fields": [
        {"name": SLOPE, "kind": "numeric", "short": "slope", "searched": True,
         "value_min": -0.3, "value_max": 0.3, "value_step": 0.05, "anchor_op": ">",
         "anchor_value": 0.0, "codes": None, "ui_name": "Trend slope"},
        {"name": ADX, "kind": "numeric", "short": "adx", "searched": True,
         "value_min": 10.0, "value_max": 40.0, "value_step": 2.5, "anchor_op": "<",
         "anchor_value": 25.0, "codes": None, "ui_name": "ADX"},
        {"name": RV, "kind": "numeric", "short": "rv", "searched": True,
         "value_min": 0.5, "value_max": 2.0, "value_step": 0.25, "anchor_op": "<",
         "anchor_value": 1.0, "codes": None, "ui_name": "RV ratio"},
    ],
}


def _state(session, slope, adx, rv, adx_status="valid"):
    return {"session": session, "prior_session": session,
            "values": {SLOPE: {"value": slope, "status": "valid"},
                       ADX: {"value": adx, "status": adx_status},
                       RV: {"value": rv, "status": "valid"}}}


def _leg(symbol, session, pnl, *, txn=None, contract=None, slope=0.05, adx=20.0, rv=0.9,
         adx_status="valid"):
    return {"symbol": contract or symbol, "underlying_symbol": symbol if contract else None,
            "contract_symbol": contract, "transaction_id": txn,
            "entry_time": f"{session}T14:30:00", "exit_time": f"{session}T21:00:00",
            "pnl": pnl, "entry_state": _state(session, slope, adx, rv, adx_status)}


@pytest.fixture
def db(tmp_path):
    """A test database with exactly the two tables and columns the report reads."""
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE strategy_optimizations (id INTEGER PRIMARY KEY, name TEXT, "
                "status TEXT, optimization_config TEXT, all_results TEXT, best_params TEXT, "
                "best_fitness REAL)")
    con.execute("CREATE TABLE backtests (id INTEGER PRIMARY KEY, name TEXT, optimization_id INT, "
                "initial_capital REAL, final_equity REAL, total_return REAL, "
                "annualized_return REAL, max_drawdown REAL, calmar_ratio REAL, "
                "total_trades INT, win_rate REAL, ga_fitness REAL, results TEXT, trades TEXT, "
                "equity_curve TEXT, drawdown_curve TEXT, start_date TEXT, end_date TEXT)")
    trades = [
        # One two-leg structure: ONE unit, net +300, in the ADX [15, 25) bin.
        _leg("AAA", "2022-03-01", 500.0, txn=11, contract="AAA220401C00100000", adx=20.0),
        _leg("AAA", "2022-03-01", -200.0, txn=11, contract="AAA220401C00110000", adx=20.0),
        # Two single-leg structures in [25, 40).
        _leg("BBB", "2022-06-01", 1000.0, txn=12, contract="BBB220701C00050000", adx=30.0),
        _leg("CCC", "2022-09-01", -100.0, txn=13, contract="CCC221001C00070000", adx=30.0),
        # An ADX the edges do not cover -- reported as outside, never binned.
        _leg("DDD", "2023-01-03", 50.0, txn=14, contract="DDD230201C00020000", adx=140.0),
        # A recorded-but-UNKNOWN measurement: counted by reason, never given a value.
        _leg("EEE", "2023-02-01", 25.0, txn=15, contract="EEE230301C00030000", adx=None,
             adx_status="insufficient_history"),
    ]
    equity = [{"date": f"2022-01-0{i}", "equity": 100000.0 + i * 100} for i in (1, 2, 3)]
    equity += [{"date": "2022-12-30", "equity": 110000.0}, {"date": "2023-12-29", "equity": 95000.0}]
    results = {"market_condition": {
        "profile": "ohlcv-v1", "manifest": _BLOCK["manifest"],
        "calc_version": "ohlcv-v1/calc-1", "timing_policy": "prior_session_v1",
        "stats": {"eligible_recommendations": 400, "market_evaluated": 380,
                  "market_gate_passed": 120, "market_gate_rejected": 240,
                  "market_unknown_recommendations": 20, "market_leaf_evaluations": 380,
                  "entries_staged": 8, "entry_read_failures": 0,
                  "market_unknown_input_by_reason": {"insufficient_history": 20}}}}
    con.execute(
        "INSERT INTO strategy_optimizations VALUES (?,?,?,?,?,?,?)",
        (7, "opt-ohlcv-O_LC", "completed",
         json.dumps({"backtest": {"enabled_instruments": ["AAA", "BBB", "CCC"],
                                  "market_condition": _BLOCK}}),
         json.dumps([
             {"fitness": 1.5, "params": {"cond:o_lc-market-adx:mode": "below",
                                         "cond:o_lc-market-adx:value": 27.5,
                                         "cond:o_lc-market-slope:mode": "off"}},
             {"fitness": 1.2, "params": {"cond:o_lc-market-adx:mode": 2,
                                         "cond:o_lc-market-adx:value": 15.0}},
             {"fitness": 1.5, "params": {"cond:o_lc-market-adx:mode": "above"}},
         ]),
         json.dumps({}), 1.5))
    con.execute(
        "INSERT INTO backtests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (900, "BEST-opt-ohlcv-O_LC", 7, 100000.0, 95000.0, -5.0, -2.5, 12.0, 0.4,
         len(trades), 50.0, 1.5, json.dumps(results), json.dumps(trades),
         json.dumps(equity), json.dumps([{"date": e["date"], "drawdown": -1.0} for e in equity]),
         "2022-01-01", "2023-12-29"))
    con.commit()
    con.close()
    return str(path)


# --------------------------------------------------------------------------- bins
def test_the_declared_bin_edges_are_the_ones_the_task_specified():
    assert R.BIN_EDGES[SLOPE] == (float("-inf"), -0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3,
                                  float("inf"))
    assert R.BIN_EDGES[ADX] == (0.0, 15.0, 25.0, 40.0, 100.0)
    assert R.BIN_EDGES[RV] == (0.0, 0.75, 1.0, 1.5, 2.0, float("inf"))


@pytest.mark.parametrize("value,index", [
    (0.0, 0), (14.999, 0), (15.0, 1), (24.999, 1), (25.0, 2), (39.999, 2), (40.0, 3), (100.0, 3)])
def test_a_value_lands_in_the_bin_its_edges_declare(value, index):
    assert R.bin_of(value, R.BIN_EDGES[ADX]) == index


@pytest.mark.parametrize("value", [-0.001, 100.001, 140.0])
def test_a_value_outside_the_edges_is_not_silently_binned(value):
    """An ADX of 140 is a data problem. Folded into "[40, 100]" it becomes a finding."""
    assert R.bin_of(value, R.BIN_EDGES[ADX]) is None


def test_an_unlisted_field_gets_bins_from_the_PERSISTED_spec_not_this_registry():
    edges, labels = R.field_bins("structure_dist_support_atr",
                                 {"value_min": 0.0, "value_max": 8.0})
    assert len(labels) == R._FALLBACK_BINS
    assert edges[0] == float("-inf") and edges[-1] == float("inf")
    assert R.field_bins("whatever", None) == ((), ["all values"])


# --------------------------------------------------------------------------- genes
def test_the_winning_modes_read_tokens_and_raw_indexes_alike():
    rows = R.market_gene_rows(
        {"cond:o_lc-market-adx:mode": "below", "cond:o_lc-market-adx:value": 27.5,
         "cond:o_lc-market-slope:mode": 2, "cond:o_lc-market-slope:value": 0.1,
         "cond:o_lc-market-rv:mode": "off", "cond:o_lc-market-rv:value": 1.25,
         "exit:5:a0:option_dte": 40}, _BLOCK)
    assert ("o_lc", "adx", "below", "27.5") in rows
    assert ("o_lc", "slope", "above", "0.1") in rows          # index 2 -> "above"
    # An OFF leaf is removed by the decode, so its threshold gene means nothing; showing the
    # number would read as "this genome gates RV at 1.25", which is the opposite of the truth.
    assert ("o_lc", "rv", "off", "-") in rows
    assert len(rows) == 3


# --------------------------------------------------------------------------- attribution
def test_the_attribution_unit_is_a_structure_not_a_leg(db):
    con = R.open_db(db)
    run = R.persisted_runs(con, 7)[0]
    data = R.attribution(run["trades"], _BLOCK)
    assert data["units"] == 5          # six legs, one of them a two-leg structure
    assert data["inconsistent"] == 0
    rows = {r["bin"]: r for r in data["fields"][ADX]["rows"]}
    assert rows["[15, 25)"]["units"] == 1
    assert rows["[15, 25)"]["net_pnl"] == pytest.approx(300.0)   # +500 and -200 netted
    assert rows["[15, 25)"]["issuers"] == 1
    assert rows["[15, 25)"]["dates"] == 1
    assert rows["[25, 40)"]["units"] == 2
    assert rows["[25, 40)"]["issuers"] == 2
    assert rows["[25, 40)"]["net_pnl"] == pytest.approx(900.0)


def test_an_out_of_range_measurement_gets_its_own_row(db):
    data = R.attribution(R.persisted_runs(R.open_db(db), 7)[0]["trades"], _BLOCK)
    outside = [r for r in data["fields"][ADX]["rows"] if r["bin"].startswith("outside")]
    assert len(outside) == 1 and outside[0]["units"] == 1


def test_an_unknown_measurement_is_counted_by_reason_and_never_binned(db):
    data = R.attribution(R.persisted_runs(R.open_db(db), 7)[0]["trades"], _BLOCK)
    assert data["fields"][ADX]["unknown"] == {"insufficient_history": 1}
    assert sum(r["units"] for r in data["fields"][ADX]["rows"]) == 4


def test_concentration_is_a_share_of_net_and_refuses_to_divide_by_nothing():
    net, top1, top5 = R.concentration([100.0, 50.0, -20.0])
    assert net == pytest.approx(130.0)
    assert top1 == pytest.approx(100.0 / 130.0 * 100.0)
    assert top5 == pytest.approx(100.0)
    net, top1, top5 = R.concentration([50.0, -50.0])
    assert net == 0.0 and math.isnan(top1) and math.isnan(top5)


# --------------------------------------------------------------------------- output
def test_the_report_quotes_the_persisted_versions_and_the_counters(db):
    con = R.open_db(db)
    opt = R.optimizations(con, opt_id=7)[0]
    text = R.render(opt, R.persisted_runs(con, 7), top=3)
    assert "fmp-daily-split-adjusted-v1" in text
    assert "prior_session_v1" in text
    assert _BLOCK["manifest"] in text
    assert "eligible_recommendations       400" in text
    assert "market_gate_rejected           240" in text
    assert "insufficient_history" in text
    assert "TOP1" in text and "below" in text


def test_the_report_never_annualises_a_filtered_trade_subset(db):
    """Design 7: "Do not annualize a filtered subset of overlapping trades as if it were a
    funded account." The bin table must carry no annualised column, and must SAY so."""
    con = R.open_db(db)
    opt = R.optimizations(con, opt_id=7)[0]
    text = R.render(opt, R.persisted_runs(con, 7), top=3)
    section = text.split("### entry-state attribution", 1)[1]
    assert "A BIN IS NOT AN ACCOUNT" in section
    # The bin table's own columns, explicitly: dollars, counts and shares -- nothing rate-like.
    headers = [line for line in section.splitlines() if "issuers" in line]
    assert headers, section
    for header in headers:
        assert header.split() == ["bin", "units", "issuers", "dates", "net", "P&L", "top-1",
                                  "top-5"], header
    # ...and no rate-like figure in any DATA row of those tables either: every cell is a
    # count, a dollar amount or a share, and a "%" only ever appears in the two share columns.
    for line in section.splitlines():
        if line.strip().startswith("[") or line.strip().startswith("outside"):
            assert line.count("%") <= 2, line


def test_a_job_with_no_market_condition_block_says_the_gates_were_off(db):
    con = sqlite3.connect(db)
    con.execute("UPDATE strategy_optimizations SET optimization_config = ? WHERE id = 7",
                (json.dumps({"backtest": {"enabled_instruments": ["AAA"]}}),))
    con.commit()
    con.close()
    con = R.open_db(db)
    opt = R.optimizations(con, opt_id=7)[0]
    text = R.render(opt, R.persisted_runs(con, 7), top=1)
    assert "ran with the gates OFF" in text


def test_the_per_year_block_is_the_whole_account(db):
    con = R.open_db(db)
    years = R.per_year(R.persisted_runs(con, 7)[0])
    assert years, "the fixture curve spans two calendar years"
    assert all("returnPct" in y and "maxDrawdownPct" in y for y in years)
    assert all("profit" in y for y in years)


def test_the_cli_reports_a_missing_job_rather_than_printing_nothing(db):
    with pytest.raises(SystemExit):
        R.main(["--opt", "999", "--db", db])


def test_the_cli_writes_the_file_it_was_asked_for(db, tmp_path):
    out = tmp_path / "r.md"
    assert R.main(["--opt", "7", "--db", db, "--out", str(out)]) == 0
    assert "market conditions -- optimization 7" in out.read_text(encoding="utf-8")
