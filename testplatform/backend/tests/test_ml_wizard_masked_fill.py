"""The coverage mask of the daily source must survive the covariate preparation: a masked (group, year) is NaN with
``<prefix>missing`` = 1 and must NOT be forward-filled with the previous year's last value (darts_training.prepare_data)."""
import logging

import numpy as np
import pandas as pd
import pytest

from app.services.darts_training import DartsTrainingService as TrainingService

N = 400
TRAIN = 0.7


def frame(n=N):
    rng = np.random.default_rng(0)
    d = pd.DataFrame({"Date": pd.date_range("2020-01-01", periods=n, freq="D"), "y": rng.integers(0, 2, n).astype(float),
                      "Close": 100.0 + np.arange(n) * 0.1,
                      "t_a": rng.normal(size=n), "f_a": rng.normal(10, 1, n), "f_b": rng.normal(-5, 1, n),
                      "mtf_a": rng.normal(3, 1, n), "t_missing": 0.0, "f_missing": 0.0, "mtf_missing": 0.0})
    return d


def raw_covariates(svc, cov):
    """The covariates back in their own units (the Scaler min-maxes them)."""
    return svc.cov_scaler.inverse_transform(cov).to_dataframe()


def prep(d, cols, ratio=TRAIN):
    svc = TrainingService()
    series, cov = svc.prepare_data(d, "y", cols, "1d", scaler_fit_ratio=ratio)
    return svc, series, cov


COLS = ["t_a", "f_a", "f_b", "mtf_a", "t_missing", "f_missing", "mtf_missing"]


class TestMaskedCellsAreNeverCarriedOver:
    def test_a_masked_mid_span_group_year_gets_the_train_median_not_the_last_value(self):
        d = frame()
        lo, hi = 150, 250
        d.loc[lo:hi, ["f_a", "f_b"]] = np.nan
        d.loc[lo:hi, "f_missing"] = 1.0
        svc, series, cov = prep(d, COLS)
        raw = raw_covariates(svc, cov)
        train = d.iloc[: int(N * TRAIN)]
        valid = train.drop(train.index[(train.index >= lo) & (train.index <= hi)])
        for c in ("f_a", "f_b"):
            masked = raw[c].iloc[lo:hi + 1].to_numpy()
            np.testing.assert_allclose(masked, valid[c].median(), rtol=1e-5)
            assert not np.allclose(masked, d[c].iloc[lo - 1], rtol=1e-3)     # not the carried-over value
        assert (raw["f_missing"].iloc[lo:hi + 1] == 1.0).all()                # the flag stays a feature
        np.testing.assert_allclose(raw["f_a"].iloc[:lo].to_numpy(), d["f_a"].iloc[:lo].to_numpy(), rtol=1e-5)
        assert len(series) == N                                               # no row lost

    def test_the_neutral_value_is_fitted_on_the_train_block_only(self):
        d = frame()
        d.loc[150:250, ["f_a", "f_b"]] = np.nan
        d.loc[150:250, "f_missing"] = 1.0
        d.loc[int(N * TRAIN):, "f_a"] = 1000.0                                  # a regime the train block never saw
        svc, series, cov = prep(d, COLS)
        raw = raw_covariates(svc, cov)
        train = d.iloc[: int(N * TRAIN)]
        expected = train["f_a"].drop(train.index[150:251]).median()
        np.testing.assert_allclose(raw["f_a"].iloc[150:251].to_numpy(), expected, rtol=1e-5)

    def test_a_masked_first_year_loses_no_row_and_does_not_hide_other_groups(self):
        d = frame()
        d.loc[:99, "mtf_a"] = np.nan
        d.loc[:99, "mtf_missing"] = 1.0
        d.loc[:4, "t_a"] = np.nan                                               # a plain warm-up NaN, not masked
        svc, series, cov = prep(d, COLS)
        raw = raw_covariates(svc, cov)
        assert len(series) == N - 5                                             # only the real warm-up is dropped
        valid = d.iloc[5:5 + int((N - 5) * TRAIN)].loc[100:, "mtf_a"]     # the scalers window: first 70% of the 395 built points
        np.testing.assert_allclose(raw["mtf_a"].iloc[:95].to_numpy(), valid.median(), rtol=1e-5)
        np.testing.assert_allclose(raw["t_a"].to_numpy(), d["t_a"].iloc[5:].to_numpy(), rtol=1e-5)

    def test_a_group_masked_in_every_year_is_dropped_with_a_warning_and_keeps_its_flag(self, caplog):
        d = frame()
        d["f_a"] = np.nan
        d["f_b"] = np.nan
        d["f_missing"] = 1.0
        with caplog.at_level(logging.WARNING):
            svc, series, cov = prep(d, COLS)
        raw = raw_covariates(svc, cov)
        assert "f_a" not in raw.columns and "f_b" not in raw.columns
        assert "f_missing" in raw.columns and "t_a" in raw.columns and "mtf_a" in raw.columns
        assert any("masked in every row" in r.message and "f_a" in r.message for r in caplog.records)
        assert len(series) == N

    def test_nothing_changes_without_a_missing_flag(self):
        """An ordinary dataset keeps the past-only forward fill and the leading-row drop."""
        d = frame()[["Date", "y", "Close", "t_a", "f_a"]]
        d.loc[:2, "t_a"] = np.nan
        d.loc[100:110, "f_a"] = np.nan
        svc, series, cov = prep(d, ["t_a", "f_a"])
        raw = raw_covariates(svc, cov)
        assert len(series) == N - 3
        assert (raw["f_a"].iloc[100 - 3:110 - 3 + 1].round(5) == round(float(d["f_a"].iloc[99]), 5)).all()

    def test_a_column_that_is_empty_without_a_mask_still_raises(self):
        d = frame()[["Date", "y", "Close", "t_a", "f_a"]]
        d["f_a"] = np.nan
        with pytest.raises(ValueError, match="without any value"):
            prep(d, ["t_a", "f_a"])

    def test_a_flag_of_a_foreign_prefix_is_not_a_daily_mask(self):
        """Only the daily store's group prefixes count: a user column named foo_missing keeps the old behaviour."""
        d = frame()[["Date", "y", "Close", "t_a"]].copy()
        d["foo_a"] = np.random.default_rng(1).normal(size=N)
        d["foo_missing"] = 0.0
        d.loc[100:110, "foo_a"] = np.nan
        d.loc[100:110, "foo_missing"] = 1.0
        svc, series, cov = prep(d, ["t_a", "foo_a", "foo_missing"])
        raw = raw_covariates(svc, cov)
        assert (raw["foo_a"].iloc[100:111].round(5) == round(float(d["foo_a"].iloc[99]), 5)).all()
