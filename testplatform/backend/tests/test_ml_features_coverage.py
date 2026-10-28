"""Coverage report and masking of the daily feature store (Task 1.10)."""
import numpy as np
import pandas as pd
import pytest

from app.services import ml_features as F


def _frame():
    """Two symbols over 2020-2022. Group t: 2020 complete, 2021 60% complete, 2022 complete except unlisted rows.
    Group x: always complete. Group s: its structural columns are NaN by design."""
    idx = pd.MultiIndex.from_product([["AAA", "BBB"], pd.bdate_range("2020-01-01", "2022-12-30")], names=["symbol", "session"])
    n = len(idx)
    year = idx.get_level_values("session").year.to_numpy()
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"listed": True, "t_ret_1": rng.normal(size=n), "t_rsi": rng.normal(size=n),
                       "x_rk_rsi": rng.uniform(size=n), "s_adx": rng.uniform(size=n), "s_dist_support_atr": np.nan,
                       "s_bars_since_bos": np.nan}, index=idx)
    pos = np.arange(n)
    holes = (year == 2021) & (pos % 10 < 4)                     # 40% of the 2021 rows lack t_rsi -> 60% complete
    df.loc[holes, "t_rsi"] = np.nan
    unlisted = (year == 2022) & (df.index.get_level_values("symbol") == "BBB")
    df.loc[unlisted, ["listed", "t_ret_1", "t_rsi", "x_rk_rsi"]] = [False, np.nan, np.nan, np.nan]
    df["listed"] = df["listed"].astype(bool)
    return df


def test_coverage_report_is_the_share_of_listed_rows_with_every_feature(monkeypatch):
    df = _frame()
    rep = F.coverage_report(df, ["t", "x"])
    assert list(rep.index.names) == ["group", "year"] and list(rep.columns) == ["rows", "complete", "share"]
    assert rep.loc[("t", 2020), "share"] == 1.0
    y21 = df[(df.index.get_level_values("session").year == 2021)]
    assert rep.loc[("t", 2021), "share"] == pytest.approx(y21["t_rsi"].notna().mean())
    assert 0.55 < rep.loc[("t", 2021), "share"] < 0.65
    assert rep.loc[("t", 2022), "share"] == 1.0                 # the unlisted rows are not in the denominator
    assert (rep.xs("x", level="group")["share"] == 1.0).all()


def test_the_unlisted_rows_are_not_counted(monkeypatch):
    df = _frame()
    rep = F.coverage_report(df, ["t"])
    n22 = (df.index.get_level_values("session").year == 2022)
    listed22 = (df["listed"].to_numpy() & n22).sum()
    assert rep.loc[("t", 2022), "rows"] == listed22 and listed22 < n22.sum()


def test_a_group_may_declare_the_columns_that_define_its_coverage():
    """Structure columns that are NaN by design (no pivot, no target) do not make a group "uncovered"."""
    df = _frame()
    full = F.coverage_report(df, ["s"])
    assert F.GROUPS["s"].coverage_columns is not None and "s_dist_support_atr" not in F.GROUPS["s"].coverage_columns
    assert "s_adx" in F.GROUPS["s"].coverage_columns
    assert (full["share"] == 1.0).all()                         # judged on s_adx only in this frame
    assert F.GROUPS["at"].coverage_columns == ("at_target_gap",)                  # the main signal, not the analyst count
    assert "ag_net_upgrades_30" in F.GROUPS["ag"].coverage_columns and "ag_covered" not in F.GROUPS["ag"].coverage_columns
    assert F.GROUPS["t"].coverage_columns is None


def test_mask_nans_the_group_year_under_the_threshold_and_flags_it():
    df = _frame()
    rep = F.coverage_report(df, ["t", "x"])
    out = F.apply_coverage_mask(df, rep, min_share=0.7)
    year = out.index.get_level_values("session").year
    t_cols = ["t_ret_1", "t_rsi"]
    assert out.loc[year == 2021, t_cols].isna().all().all()                    # 2021 is 60% < 70%
    pd.testing.assert_frame_equal(out.loc[year != 2021, t_cols], df.loc[year != 2021, t_cols])     # other years untouched
    pd.testing.assert_series_equal(out["x_rk_rsi"], df["x_rk_rsi"])                                 # other groups untouched
    assert (out.loc[year == 2021, "t_missing"] == 1.0).all() and (out.loc[year != 2021, "t_missing"] == 0.0).all()
    assert "x_missing" in out.columns and (out["x_missing"] == 0.0).all()
    assert out.attrs["coverage_masked"] == [("t", 2021)]
    assert df["t_rsi"].notna().sum() > out["t_rsi"].notna().sum()               # the input frame is not modified


def test_threshold_is_a_parameter_and_masking_is_idempotent():
    df = _frame()
    rep = F.coverage_report(df, ["t"])
    strict = F.apply_coverage_mask(df, rep, min_share=1.01)
    assert strict[["t_ret_1", "t_rsi"]].isna().all().all()
    loose = F.apply_coverage_mask(df, rep, min_share=0.5)
    pd.testing.assert_frame_equal(loose[["t_ret_1", "t_rsi"]], df[["t_ret_1", "t_rsi"]])
    assert (loose["t_missing"] == 0.0).all()
    once = F.apply_coverage_mask(df, rep, min_share=0.7)
    twice = F.apply_coverage_mask(once, F.coverage_report(once, ["t"]), min_share=0.7)
    pd.testing.assert_frame_equal(once.drop(columns="t_missing"), twice.drop(columns="t_missing"))
    pd.testing.assert_series_equal(once["t_missing"], twice["t_missing"])        # the flag is recomputed from the new report


def test_the_missing_flag_is_not_itself_a_covered_feature():
    df = _frame()
    out = F.apply_coverage_mask(df, F.coverage_report(df, ["t"]), min_share=0.7)
    rep2 = F.coverage_report(out, ["t"])
    assert "t_missing" not in F._coverage_columns(out, "t")
    assert rep2.loc[("t", 2021), "share"] == 0.0 and rep2.loc[("t", 2020), "share"] == 1.0


def test_column_coverage_gives_the_nan_rate_of_every_column_by_year():
    df = _frame()
    cc = F.column_coverage(df, ["t", "s"])
    assert cc.loc[("t_rsi", 2021), "share"] == pytest.approx(df.loc[df.index.get_level_values("session").year == 2021, "t_rsi"].notna().mean())
    assert cc.loc[("s_dist_support_atr", 2020), "share"] == 0.0 and cc.loc[("s_adx", 2020), "share"] == 1.0


def test_unknown_group_raises_and_missing_columns_are_reported():
    df = _frame()
    with pytest.raises(ValueError, match="unknown"):
        F.coverage_report(df, ["nope"])
    with pytest.raises(ValueError, match="no columns"):
        F.coverage_report(df, ["mtf"])                                              # none of its columns in the frame


def test_columns_that_are_nan_by_design_do_not_define_coverage():
    """m_rs_sector_* is NaN for a sectorless symbol or before a sector ETF lists; mac_baa10y_* has a reviewed stall."""
    assert F.GROUPS["m"].coverage_columns is None                                   # all of M ...
    assert F.GROUPS["m"].sectorless_exempt == ("m_rs_sector_5", "m_rs_sector_20")   # ... except m_rs_sector_* of a sectorless symbol
    mac = F.GROUPS["mac"].coverage_columns
    assert "mac_vixcls_lvl" in mac and not any(c.startswith("mac_baa10y") for c in mac) and len(mac) == len(F.MAC_COLUMNS) - 3


def _m_frame():
    """A stock whose sector ETF lists mid-2020 (m_rs_sector NaN before) and a sectorless ETF (always NaN there)."""
    idx = pd.MultiIndex.from_product([["AAA", "SPY"], pd.bdate_range("2020-01-01", "2020-12-31")], names=["symbol", "session"])
    df = pd.DataFrame({"listed": True, "m_spy_ret_1": 0.01, "m_rs_spy_5": 0.0, "m_rs_sector_5": 0.0, "m_rs_sector_20": 0.0}, index=idx)
    sess = idx.get_level_values("session")
    sym = idx.get_level_values("symbol")
    df.loc[(sym == "AAA") & (sess < "2020-07-01"), ["m_rs_sector_5", "m_rs_sector_20"]] = np.nan
    df.loc[sym == "SPY", ["m_rs_sector_5", "m_rs_sector_20"]] = np.nan
    return df


def test_the_sector_columns_are_exempt_only_for_declared_sectorless_symbols():
    df = _m_frame()
    sess = df.index.get_level_values("session")
    strict = F.coverage_report(df, ["m"]).loc[("m", 2020), "share"]
    assert strict == pytest.approx(((sess >= "2020-07-01") & (df.index.get_level_values("symbol") == "AAA")).sum() / len(df))
    relaxed = F.coverage_report(df, ["m"], sectorless=["SPY"]).loc[("m", 2020), "share"]
    n_aaa_ok = ((sess >= "2020-07-01") & (df.index.get_level_values("symbol") == "AAA")).sum()
    n_spy = (df.index.get_level_values("symbol") == "SPY").sum()
    assert relaxed == pytest.approx((n_aaa_ok + n_spy) / len(df))              # the stock's hole still counts against it
    df.attrs["meta"] = {"sectorless": ["SPY"]}                                  # recorded by build_features / ensure
    assert F.coverage_report(df, ["m"]).loc[("m", 2020), "share"] == pytest.approx(relaxed)


def test_the_column_coverage_keeps_the_exempt_holes_visible():
    df = _m_frame()
    cc = F.column_coverage(df, ["m"])
    assert cc.loc[("m_rs_sector_5", 2020), "share"] == pytest.approx(0.25, abs=0.01)     # AAA's second half only: SPY is NaN throughout
