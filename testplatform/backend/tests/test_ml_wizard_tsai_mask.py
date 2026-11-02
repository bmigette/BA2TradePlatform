"""The tsai path must not see the daily coverage mask's NaN cells either: they get the train-block median (flag kept, an
all-masked group dropped) before the min-max scaling (tsai_training; the darts side is in test_ml_wizard_masked_fill)."""
import logging

import numpy as np
import pandas as pd
import pytest

from app.services import tsai_training as TT

N = 300
RATIO, HOLD = 0.7, 0.5
COLS = ["t_a", "f_a", "f_b", "t_missing", "f_missing"]
SEQ = 4


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(TT, "TSAI_AVAILABLE", True)      # the windowing and the scaler are numpy; tsai itself is not used
    return TT.TSAITrainingService(normalize=True)


def frame(seed=0, n=N, start=0, masked=(120, 200)):
    rng = np.random.default_rng(seed)
    d = pd.DataFrame({
        "Date": pd.date_range("2020-01-01", periods=n + start, freq="D")[start:],
        "y": rng.integers(0, 2, n), "t_a": rng.normal(size=n),
        "f_a": rng.normal(10, 1, n), "f_b": rng.normal(-5, 1, n), "t_missing": 0.0, "f_missing": 0.0})
    if masked:
        lo, hi = masked
        d.loc[lo:hi, ["f_a", "f_b"]] = np.nan
        d.loc[lo:hi, "f_missing"] = 1.0
    return d


def no_nan(out):
    return all(not np.isnan(out[k]).any() for k in out if k.startswith("X_"))


class TestNoMaskedNanReachesTheScaler:
    @pytest.mark.parametrize("common", [False, True])
    def test_a_masked_mid_span_year_is_filled_in_the_three_way_split(self, svc, common):
        frames = [frame(1), frame(2), frame(3, n=200, start=100, masked=(60, 120))]
        out = svc.prepare_split_three_way(frames, RATIO, HOLD, "y", COLS, seq_len=SEQ, label_horizon=3,
                                          common_dates=common)
        assert no_nan(out)
        # the masked columns and the f flag survive (t_missing is constant 0: the scaler drops zero-variance columns)
        assert {"t_a", "f_a", "f_b", "f_missing"} <= set(svc.data_prep.get_valid_columns())

    def test_without_the_fix_the_nan_would_reach_the_scaler(self, svc):
        """Sanity of the test data: the masked cells are really NaN in the raw frames."""
        assert frame(1)[["f_a", "f_b"]].isna().any().any()

    def test_the_masked_cells_hold_the_train_block_median_not_the_previous_value(self, monkeypatch):
        monkeypatch.setattr(TT, "TSAI_AVAILABLE", True)
        s = TT.TSAITrainingService(normalize=False)
        f = frame(4)
        out = s.prepare_split_three_way([f], RATIO, HOLD, "y", COLS, seq_len=1, label_horizon=3)
        n_train = int(N * RATIO) - 3
        train = f.iloc[:n_train]
        median = train.loc[~train["f_missing"].astype(bool), "f_a"].median()
        # the train windows are 1 row long: X_train[:, f_a, 0] is the train block's f_a column
        col = COLS.index("f_a")
        x = out["X_train"][:, col, 0]
        np.testing.assert_allclose(x[120:201], median, rtol=1e-5)
        assert not np.isclose(x[120], f["f_a"].iloc[119], rtol=1e-3)
        np.testing.assert_allclose(x[:120], f["f_a"].iloc[:120].to_numpy(), rtol=1e-5)

    def test_the_fill_is_fitted_on_the_train_block_only(self, monkeypatch):
        monkeypatch.setattr(TT, "TSAI_AVAILABLE", True)
        s = TT.TSAITrainingService(normalize=False)
        f = frame(5)
        f.loc[int(N * RATIO):, "f_a"] = 1000.0                   # a regime the train block never saw
        out = s.prepare_split_three_way([f], RATIO, HOLD, "y", COLS, seq_len=1, label_horizon=3)
        x = out["X_train"][:, COLS.index("f_a"), 0]
        assert x[120:201].max() < 100

    def test_a_group_masked_in_every_row_is_dropped_with_a_warning_and_keeps_its_flag(self, svc, caplog):
        frames = [frame(6), frame(7)]
        for f in frames:
            f["f_a"] = np.nan
            f["f_b"] = np.nan
            f["f_missing"] = 1.0
        with caplog.at_level(logging.WARNING):
            out = svc.prepare_split_three_way(frames, RATIO, HOLD, "y", COLS, seq_len=SEQ, label_horizon=3)
        assert no_nan(out)
        assert set(svc.data_prep.get_valid_columns()) == {"t_a"}   # f_a / f_b dropped; the constant flags are zero-variance
        assert any("masked in every row" in r.message for r in caplog.records)

    def test_a_group_masked_in_every_row_of_one_ticker_is_dropped_for_all(self, svc):
        a, b = frame(8), frame(9)
        b["f_a"] = np.nan
        b["f_b"] = np.nan
        b["f_missing"] = 1.0
        out = svc.prepare_split_three_way([a, b], RATIO, HOLD, "y", COLS, seq_len=SEQ, label_horizon=3)
        assert set(svc.data_prep.get_valid_columns()) == {"t_a", "f_missing"}      # one feature set for every ticker
        assert no_nan(out)

    def test_the_single_and_multi_dataset_splits_fill_too(self, svc):
        X_train, X_test, _, _ = svc.prepare_data_split(frame(10), RATIO, "y", COLS, seq_len=SEQ, label_horizon=3)
        assert not np.isnan(X_train).any() and not np.isnan(X_test).any()
        X_train, X_test, _, _ = svc.prepare_multi_dataset_split([frame(11), frame(12)], RATIO, "y", COLS, seq_len=SEQ,
                                                                label_horizon=3)
        assert not np.isnan(X_train).any() and not np.isnan(X_test).any()

    def test_prepare_data_fits_on_the_train_block_given_by_a_date(self, monkeypatch):
        monkeypatch.setattr(TT, "TSAI_AVAILABLE", True)
        s = TT.TSAITrainingService(normalize=False)
        f = frame(13)
        f.loc[201:, "f_a"] = 1000.0                              # after the masked year, outside the train block
        until = f["Date"].iloc[149]
        X, _ = s.prepare_data(f, "y", COLS, seq_len=1, fit_scaler=True, scaler_fit_until=until)
        head = f.iloc[:150]
        median = head.loc[~head["f_missing"].astype(bool), "f_a"].median()
        np.testing.assert_allclose(X[120:150, COLS.index("f_a"), 0], median, rtol=1e-5)

    def test_a_held_out_frame_with_masked_cells_is_refused_not_filled_with_its_own_median(self, svc):
        with pytest.raises(ValueError, match="masked"):
            svc.prepare_data(frame(14), "y", COLS, seq_len=SEQ, fit_scaler=False)

    def test_ordinary_datasets_are_untouched(self, svc):
        f = frame(15, masked=None)[["Date", "y", "t_a", "f_a"]]
        out = svc.prepare_split_three_way([f], RATIO, HOLD, "y", ["t_a", "f_a"], seq_len=SEQ, label_horizon=3)
        assert out["X_train"].shape[1] == 2 and no_nan(out)
