"""Group F (point-in-time fundamentals) of the daily feature store."""
import json
import shutil
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.services import ml_features as F
from app.services import ml_panel as P
from tests.test_ml_features import (FIRST, LAST, STORE_SCANS, _assert_cut_identical, _build, _raw, _sessions,  # noqa: F401
                                    _store_rows, cache)

SCALE = 1e10


def _earnings(sym, start=date(2020, 10, 27), n=14):
    out = []
    for i in range(n):
        out.append({"date": str(start + timedelta(days=91 * i)), "symbol": sym, "eps": 1.0 + 0.1 * i, "epsEstimated": 1.0,
                    "time": "amc" if i % 2 else "bmo", "revenue": 1e9 * (1 + 0.05 * i), "revenueEstimated": 1e9})
    out.append({"date": "2030-01-30", "symbol": sym, "eps": None, "epsEstimated": 2.0, "time": "amc"})
    return out


def _statements(sym, income_accepted=None):
    """Three fiscal years of hand-made annual statements (FY2020 .. FY2023), numbers in units of ``SCALE``."""
    inc, bs, cf = [], [], []
    for k, fy in enumerate((2020, 2021, 2022, 2023)):
        acc = f"{fy + 1}-02-15 16:30:00"
        rev = (6 + 2 * k) * SCALE
        inc.append({"date": f"{fy}-12-31", "acceptedDate": (income_accepted or {}).get(fy, acc), "revenue": rev,
                    "grossProfit": rev * 0.4, "operatingIncome": rev * 0.2})
        bs.append({"date": f"{fy}-12-31", "acceptedDate": acc, "totalLiabilities": (4 + k) * SCALE,
                   "totalAssets": 10 * SCALE, "totalCurrentAssets": (3 + 0.5 * k) * SCALE,
                   "totalCurrentLiabilities": 2 * SCALE})
        cf.append({"date": f"{fy}-12-31", "acceptedDate": acc, "freeCashFlow": (1 + 0.5 * k) * SCALE,
                   "commonStockRepurchased": -(0.4 + 0.1 * k) * SCALE, "stockBasedCompensation": 0.3 * SCALE,
                   "capitalExpenditure": -(0.5 + 0.1 * k) * SCALE})
    return inc, bs, cf


def _write_fmp(root, sym, income_accepted=None):
    d = root / "fmp_history"
    d.mkdir(parents=True, exist_ok=True)
    inc, bs, cf = _statements(sym, income_accepted)
    for kind, rows in (("past_earnings_quarterly", _earnings(sym)), ("income_statement_annual", inc),
                       ("balance_sheet_annual", bs), ("cashflow_statement_annual", cf)):
        (d / f"{kind}__{sym}.json").write_text(json.dumps(rows), encoding="utf-8")


@pytest.fixture(scope="module")
def fund_cache(cache, tmp_path_factory):
    root = tmp_path_factory.mktemp("fundcache")
    shutil.copytree(cache, root, dirs_exist_ok=True)
    _write_fmp(root, "AAA")
    _write_fmp(root, "BBB", income_accepted={2022: "2023-03-01 07:00:00"})     # BBB's FY2022 income statement is filed later
    return root


def _fbuild(root, symbols=("AAA", "BBB"), **kw):
    return _build(root, ["f"], symbols=symbols, **kw)


def _mcap(sym, day):
    store = _store_rows().set_index(["date", "symbol"])
    scan = max(x for x in STORE_SCANS if x <= day)
    return float(store.loc[(scan, sym), "market_cap"])


# ------------------------------------------------------------------------------------------ columns
def test_f_columns_and_registry(fund_cache):
    assert F.GROUPS["f"].prefix == "f_" and not F.GROUPS["f"].needs_sector
    assert list(F.F_COLUMNS) == ["f_eps_surprise", "f_rev_surprise", "f_eps_yoy", "f_rev_yoy", "f_gross_margin",
                                 "f_op_margin", "f_annual_rev_growth", "f_days_since_earn", "f_earn_yield",
                                 "f_debt_assets", "f_current_ratio", "f_fcf_margin", "f_fcf_yield", "f_buyback_yield",
                                 "f_sbc_rev", "f_capex_rev"]
    f = _fbuild(fund_cache)
    assert F.feature_columns(f) == list(F.F_COLUMNS)
    assert f[list(F.F_COLUMNS)].notna().all().all()
    assert f.attrs["meta"]["source"]["store"]["newest_scan"] == STORE_SCANS[-1]


def test_the_earnings_features_are_the_existing_ones(fund_cache):
    """Reused, not re-implemented: equal to ml_panel's table on the same sessions."""
    f = _fbuild(fund_cache).loc["AAA"]
    sess = _sessions()
    table = P.fundamentals_table("AAA", [t.date() for t in sess], fund_cache)
    b = pd.DataFrame({"session": [t.date() for t in sess], "close": _raw(fund_cache, "AAA")["Close"].values}, index=sess)
    ref = P.fundamental_features(b, table)
    old = [c for c in F.F_COLUMNS[:9]]
    pd.testing.assert_frame_equal(f[old], ref[old].reindex(f.index), check_exact=False, rtol=1e-12)


# ------------------------------------------------------------------------------------------ visibility and values
def test_balance_sheet_and_cash_flow_become_visible_the_session_after_accepted_date(fund_cache):
    f = _fbuild(fund_cache).loc["AAA"]
    # FY2022 is accepted 2023-02-15 16:30: the 15th still shows FY2021, the 16th shows FY2022
    assert f.loc["2023-02-14", "f_debt_assets"] == pytest.approx(5 / 10) and f.loc["2023-02-15", "f_debt_assets"] == pytest.approx(5 / 10)
    assert f.loc["2023-02-16", "f_debt_assets"] == pytest.approx(6 / 10)
    assert f.loc["2023-02-15", "f_current_ratio"] == pytest.approx(3.5 / 2) and f.loc["2023-02-16", "f_current_ratio"] == pytest.approx(4 / 2)
    assert f.loc["2023-02-15", "f_fcf_margin"] != f.loc["2023-02-16", "f_fcf_margin"]


def test_ratios_on_hand_made_statements(fund_cache):
    f = _fbuild(fund_cache).loc["AAA"]
    day = pd.Timestamp("2023-06-01")                                     # FY2022 (k = 2) is the latest
    row = f.loc[day]
    rev, fcf, buyback, sbc, capex = 10 * SCALE, 2 * SCALE, 0.6 * SCALE, 0.3 * SCALE, 0.7 * SCALE
    mc = _mcap("AAA", "2023-06-01")
    assert row["f_debt_assets"] == pytest.approx(6 / 10) and row["f_current_ratio"] == pytest.approx(4 / 2)
    assert row["f_fcf_margin"] == pytest.approx(fcf / rev) and row["f_sbc_rev"] == pytest.approx(sbc / rev)
    assert row["f_capex_rev"] == pytest.approx(capex / rev)              # -capitalExpenditure / revenue
    assert row["f_fcf_yield"] == pytest.approx(fcf / mc) and row["f_buyback_yield"] == pytest.approx(buyback / mc)


def test_revenue_ratios_wait_for_the_income_statement_of_the_same_fiscal_date(fund_cache):
    """BBB's FY2022 cash flow is public on 2023-02-16 but its FY2022 income statement only on 2023-03-02."""
    f = _fbuild(fund_cache).loc["BBB"]
    fy21_margin = 1.5 * SCALE / (8 * SCALE)
    fy22_margin = 2.0 * SCALE / (10 * SCALE)
    assert f.loc["2023-02-16", "f_fcf_margin"] == pytest.approx(fy21_margin)           # still FY2021 (no FY2022 revenue yet)
    assert f.loc["2023-03-01", "f_fcf_margin"] == pytest.approx(fy21_margin)
    assert f.loc["2023-03-02", "f_fcf_margin"] == pytest.approx(fy22_margin)
    # the yields need only the cash flow: FY2022 from 2023-02-16
    mc = _mcap("BBB", "2023-02-16")
    assert f.loc["2023-02-15", "f_fcf_yield"] == pytest.approx(1.5 * SCALE / _mcap("BBB", "2023-02-15"))
    assert f.loc["2023-02-16", "f_fcf_yield"] == pytest.approx(2.0 * SCALE / mc)


def test_market_cap_is_the_store_scan_visible_that_session_and_nan_before_it(fund_cache):
    f = _fbuild(fund_cache, first=date(2021, 5, 3)).loc["AAA"]
    first_scan = pd.Timestamp(STORE_SCANS[0])
    pre = f.index < first_scan
    assert pre.sum() > 10 and f.loc[pre, ["f_fcf_yield", "f_buyback_yield"]].isna().all().all()
    assert f.loc[~pre, "f_fcf_yield"].notna().all() and f.loc[pre, "f_debt_assets"].notna().all()


def test_zero_or_negative_denominators_are_nan_not_inf():
    num = np.array([1.0, 1.0, 1.0, np.nan, 1.0])
    den = np.array([2.0, 0.0, -3.0, 5.0, np.nan])
    out = F._safe_ratio(num, den)
    assert out[0] == 0.5 and np.isnan(out[1:]).all()
    np.testing.assert_array_equal(F._safe_ratio(np.array([100.0]), np.array([1.0])), [5.0])         # clipped like ml_panel


def test_f_refuses_a_symbol_without_statements(fund_cache):
    with pytest.raises(P.PanelDataError, match="CCC"):
        _fbuild(fund_cache, symbols=("AAA", "CCC"))
    path = fund_cache / "fmp_history" / "balance_sheet_annual__BBB.json"
    saved = path.read_text()
    try:
        path.unlink()
        with pytest.raises(P.PanelDataError, match="balance_sheet_annual"):
            _fbuild(fund_cache, symbols=("BBB",))
    finally:
        path.write_text(saved)


# ------------------------------------------------------------------------------------------ the cut test
def _truncate_fmp(trunc, day):
    """Remove every record that was not public at the close of ``day``: earnings announced after it, and annual
    statements accepted ON or after it (a filing accepted that day is first visible the next session)."""
    for p in (trunc / "fmp_history").glob("*.json"):
        rows = json.loads(p.read_text())
        if p.name.startswith("past_earnings"):
            kept = [r for r in rows if str(r["date"])[:10] <= str(day)]
        else:
            kept = [r for r in rows if str(r["acceptedDate"])[:10] < str(day)]
        p.write_text(json.dumps(kept))


@pytest.mark.parametrize("day", [date(2023, 11, 15), date(2023, 2, 15), date(2023, 2, 16)])
def test_cut_test_f(fund_cache, tmp_path, day):
    """Includes the cut ON the acceptance day of FY2022 (invisible that day) and the day after (visible)."""
    full = _assert_cut_identical(fund_cache, tmp_path, ["f"], day=day, truncate_extra=_truncate_fmp)
    assert full["f_debt_assets"].nunique() > 2


# ------------------------------------------------------------------------------------------ review fixes
def _fresh(fund_cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(fund_cache, root)
    return root


def _edit_records(path, fn):
    rows = json.loads(path.read_text())
    path.write_text(json.dumps(fn(rows)))


def test_a_needed_key_absent_from_every_record_refuses(fund_cache, tmp_path):
    root = _fresh(fund_cache, tmp_path)
    p = root / "fmp_history" / "balance_sheet_annual__AAA.json"
    _edit_records(p, lambda rows: [{k: v for k, v in r.items() if k != "totalLiabilities"} for r in rows])
    with pytest.raises(P.PanelDataError, match=r"AAA.*balance_sheet_annual.*totalLiabilities"):
        _fbuild(root, symbols=("AAA",))
    # a key missing from SOME records is a NaN, not a refusal
    (tmp_path / "second").mkdir()
    root2 = _fresh(fund_cache, tmp_path / "second")
    p2 = root2 / "fmp_history" / "cashflow_statement_annual__AAA.json"
    _edit_records(p2, lambda rows: [{k: v for k, v in r.items() if not (k == "stockBasedCompensation" and r["date"] == "2022-12-31")}
                                    for r in rows])
    f = _fbuild(root2, symbols=("AAA",)).loc["AAA"]
    assert f.loc["2023-06-01", "f_sbc_rev"] != f.loc["2023-06-01", "f_sbc_rev"]            # NaN for the FY2022 record
    assert f.loc["2022-06-01", "f_sbc_rev"] == pytest.approx(0.3 / 8)                      # FY2021 is intact


def test_an_empty_statement_file_refuses(fund_cache, tmp_path):
    root = _fresh(fund_cache, tmp_path)
    (root / "fmp_history" / "cashflow_statement_annual__AAA.json").write_text("[]")
    with pytest.raises(P.PanelDataError, match=r"AAA.*cashflow_statement_annual.*no records"):
        _fbuild(root, symbols=("AAA",))


def test_f_on_an_etf_refuses(fund_cache):
    with pytest.raises(P.PanelDataError, match="SPY"):
        _fbuild(fund_cache, symbols=("SPY",))


def test_f_with_an_ended_member(fund_cache, tmp_path):
    root = _fresh(fund_cache, tmp_path)
    s = _sessions()
    from tests.test_ml_features import _bars, _write
    _write(root, "GONE", _bars(s[s <= pd.Timestamp("2023-06-30")], 55))
    _write_fmp(root, "GONE")
    f = _fbuild(root, symbols=("AAA", "GONE")).loc["GONE"]
    after = f.index > pd.Timestamp("2023-06-30")
    assert f.loc[after, "ended"].all() and f.loc[after, list(F.F_COLUMNS)].isna().all().all()
    before = f.loc[~after]
    no_store = [c for c in F.F_COLUMNS if c not in ("f_fcf_yield", "f_buyback_yield")]          # GONE is not in the store
    assert before[no_store].notna().all().all() and not before["ended"].any()
    assert f.loc[~after].index[-1] == pd.Timestamp("2023-06-30") and bool(f.loc[pd.Timestamp("2023-06-30"), "label_bar_filled"])


def test_f_refuses_a_stale_store(fund_cache, tmp_path):
    from ba2_providers.screener.metric_store import MetricStoreStaleError
    root = _fresh(fund_cache, tmp_path)
    for p in (root / "screener" / "metric_store_alpaca_20261022").glob("ym=*/*.parquet"):
        df = pd.read_parquet(p)
        df[df["date"] <= "2023-12-27"].to_parquet(p, index=False)       # the store ends 3 months before LAST
    shutil.rmtree(root / "screener" / "_derived", ignore_errors=True)
    with pytest.raises(MetricStoreStaleError):
        _fbuild(root)


def test_provenance_lists_every_fmp_file_read_and_the_store(fund_cache):
    f = _fbuild(fund_cache)
    src = f.attrs["meta"]["source"]
    for sym in ("AAA", "BBB"):
        for kind in ("past_earnings_quarterly", "income_statement_annual", "balance_sheet_annual", "cashflow_statement_annual"):
            fp = src["fmp_history"][f"{kind}__{sym}.json"]
            assert fp["size"] > 0 and fp["mtime_ns"] > 0
    assert len(src["fmp_history"]) == 8
    store = src["store"]
    assert store["newest_scan"] == STORE_SCANS[-1] and store["dir"].endswith("metric_store_alpaca_20261022")
    assert store["fingerprint"]["files"] > 0 and store["fingerprint"]["bytes"] > 0


def test_meta_reports_the_nan_rate_of_every_column(fund_cache):
    f = _fbuild(fund_cache, first=date(2021, 5, 3))
    rate = f.attrs["meta"]["nan_rate"]
    assert set(rate) == set(F.F_COLUMNS)
    assert all(0.0 <= v <= 1.0 for v in rate.values())
    assert rate["f_fcf_yield"] > 0 and rate["f_debt_assets"] == 0.0            # yields need the store (from 2021-06-02)


def test_usable_symbols_say_why_a_member_would_be_refused(fund_cache, tmp_path):
    from tests.test_ml_features import _bars, _write
    root = _fresh(fund_cache, tmp_path)
    s = _sessions()
    _write(root, "HOLE", _bars(s, 3).drop(index=[300, 301, 302]))               # a run of 3 missing sessions
    _write(root, "STAL", _bars(s[:-5], 4))                                        # 5 sessions short of the cache end
    usable, refused = F.usable_symbols(["AAA", "BBB", "CCC", "HOLE", "STAL", "NOPE"], FIRST, LAST, root)
    assert usable == ["AAA", "BBB", "CCC"]
    assert "consecutive" in refused["HOLE"] and "too short to count as ended" in refused["STAL"]
    assert "NOPE" in refused["NOPE"] and "no cached daily bars" in refused["NOPE"]
    usable_f, refused_f = F.usable_symbols(["AAA", "BBB", "CCC", "SPY"], FIRST, LAST, root, groups=["f"])
    assert usable_f == ["AAA", "BBB"] and "cached FMP" in refused_f["CCC"] and "SPY" in refused_f["SPY"]
