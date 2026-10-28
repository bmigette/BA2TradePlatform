"""Code-quality review fixes of the daily wizard (masked fill, heterogeneous frames, sparse targets, UI payloads)."""
from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from app.services import masked_fill as MF
from tests.test_ml_wizard_masked_fill_persist import COLS, MASK, N, frame


# ---------------------------------------------------------------------------------------------- 1. stale backtest
class TestStaleModelBacktestIsFailed:
    def _run(self, tmp_path, monkeypatch, normalization_params):
        from app.services import backtest_handler as BH
        monkeypatch.setattr(BH, "ensure_out_of_sample", lambda *a, **k: None)
        csv = tmp_path / "pred.csv"
        frame(1).to_csv(csv, index=False)
        model = MagicMock(model_type="lstm", model_id="m1", prediction_mode="shift", threshold=0.5, file_path=None,
                          hyperparameters={"featureColumns": COLS, "c_in": len(COLS), "seqLen": 4},
                          normalization_params=normalization_params)
        backtest = MagicMock(status="pending", model_id=1, prediction_dataset_id=1, execution_dataset_id=1,
                             strategy_id=None, strategy_params={}, start_date=None, end_date=None,
                             initial_capital=1000.0, position_sizing_type="fixed", position_sizing_value=100.0,
                             commission=0.0, slippage=0.0, error_message=None)
        dataset = MagicMock(file_path=str(csv), ticker="AAA")
        db = MagicMock()

        def query(cls):
            q = MagicMock()
            name = getattr(cls, "__name__", str(cls))
            q.filter.return_value.first.return_value = {"Backtest": backtest, "TrainedModel": model}.get(name, dataset)
            return q
        db.query.side_effect = query
        with patch.object(BH, "SessionLocal", return_value=db):
            result = BH.handle_backtest("t1", {"backtest_id": 1})
        return result, backtest

    def test_a_model_saved_before_masked_handling_fails_the_backtest_with_the_error(self, tmp_path, monkeypatch):
        result, backtest = self._run(tmp_path, monkeypatch, {"valid_columns": COLS})
        assert result["status"] == "failed" and "retrain: this model predates masked-cell handling" in result["error"]
        assert backtest.status == "failed"
        assert "retrain" in backtest.error_message
        assert backtest.status != "completed"


# ---------------------------------------------------------------------------------------------- 2. heterogeneous frames
class TestFramesMissingAColumn:
    def hetero(self):
        a = frame(1, "AAA")
        b = frame(2, "BBB").drop(columns=["f_b"])
        return a, b

    def test_the_helpers_skip_a_column_a_frame_does_not_have(self):
        a, b = self.hetero()
        assert MF.all_masked_columns([a, b], COLS) == []
        c = frame(3, "CCC", all_masked=True).drop(columns=["f_b"])
        assert MF.all_masked_columns([a, c], COLS) == ["f_a"]
        out, cols = MF.apply_saved_fill(b, COLS, MF.build_masked_fill({"BBB": {"f_a": 1.0}}, []), "BBB")
        assert "f_b" not in cols and not out["f_a"].isna().any()

    def test_an_old_model_with_a_missing_column_still_refuses_only_for_masked_cells(self):
        a, b = self.hetero()
        clean = frame(4, masked=None).drop(columns=["f_b"])
        out, cols = MF.apply_saved_fill(clean, COLS, None)
        assert out is clean and cols == [c for c in COLS if c != "f_b"]
        with pytest.raises(MF.StaleMaskedModel):
            MF.apply_saved_fill(b, COLS, None)

    def test_darts_prepares_frames_that_do_not_share_all_columns(self):
        from app.services.darts_training import DartsTrainingService
        a, b = self.hetero()
        svc = DartsTrainingService()
        out = svc.prepare_split_three_way([a, b], 0.7, 0.5, "y", COLS, timeframe="1d", label_horizon=3)
        assert out["train_covariates"][0].width == 5 and out["train_covariates"][1].width == 4

    def test_the_job_path_does_not_raise_on_frames_missing_a_feature(self, monkeypatch):
        from tests.test_ml_wizard_daily_targets import TestJobIntegration, daily_frame, pt
        frames = [daily_frame(10), daily_frame(11)]
        for f in frames:
            f["f_a"] = np.random.default_rng(0).normal(size=len(f))
            f["f_b"] = np.random.default_rng(1).normal(size=len(f))
            f["f_missing"] = 0.0
        frames[1] = frames[1].drop(columns=["f_b"])
        result, cap = TestJobIntegration()._job(frames, [pt("daily_up", horizonSessions=3)], monkeypatch)
        assert result["status"] == "dry_run_success", result
        assert "f_b" in cap["features"] and "f_a" in cap["features"]


# ---------------------------------------------------------------------------------------------- 3. edit intraday -> daily
class TestEditIntradayToDaily:
    GC = {"data_provider": "alpaca_hist", "panel_features": ["daily", "market"], "original_start_date": "2023-01-03",
          "original_end_date": "2024-12-31"}

    def _update(self, update):
        from tests.test_ml_wizard_daily import TestDailyUpdatePath
        fetched = []
        ds, _ = TestDailyUpdatePath()._update(dict(self.GC), update, fetched, timeframe="1h")
        return ds, fetched

    def test_the_inherited_intraday_list_is_ignored_when_the_timeframe_becomes_1d(self):
        ds, fetched = self._update({"timeframe": "1d", "daily_groups": ["t"]})
        gc = ds.generation_config
        assert gc["panel_features"] == [] and gc["daily_groups"] == ["t"] and gc["daily_universe"] == ["AAA"]
        assert fetched[0][5] == [] and fetched[0][7]["daily_groups"] == ["t"]

    def test_an_explicit_intraday_list_with_1d_is_still_refused(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._update({"timeframe": "1d", "daily_groups": ["t"], "panel_features": ["daily"]})
        assert exc.value.status_code == 400

    def test_daily_back_to_intraday_keeps_working(self):
        from tests.test_ml_wizard_daily import TestDailyUpdatePath
        gc = {"data_provider": "alpaca_hist", "panel_features": [], "daily_groups": ["t"], "daily_min_share": 0.7,
              "daily_universe": ["AAA"], "original_start_date": "2023-01-03", "original_end_date": "2024-12-31"}
        ds, _ = TestDailyUpdatePath()._update(gc, {"timeframe": "1h", "panel_features": ["market"]}, [], timeframe="1d")
        assert ds.generation_config["panel_features"] == ["market"] and "daily_groups" not in ds.generation_config


# ---------------------------------------------------------------------------------------------- 4. sparse targets
def sparse_frame(n=120, seed=0, keep_every=3):
    """Consecutive sessions; t_a is the row number (so a window's continuity is visible); only every 3rd label exists."""
    d = pd.DataFrame({"Date": pd.bdate_range("2023-01-02", periods=n), "t_a": np.arange(n, dtype=float),
                      "y": np.random.default_rng(seed).integers(0, 2, n).astype(float)})
    d.loc[[i for i in range(n) if i % keep_every != 0], "y"] = np.nan
    return d


class TestSparseTargetsKeepSessionContinuity:
    def tsai(self, monkeypatch):
        from app.services import tsai_training as TT
        monkeypatch.setattr(TT, "TSAI_AVAILABLE", True)
        return TT.TSAITrainingService(normalize=False)

    def test_a_window_covers_exactly_seq_len_consecutive_sessions(self, monkeypatch):
        svc = self.tsai(monkeypatch)
        f = sparse_frame()
        X, y = svc.prepare_data(f, "y", ["t_a"], seq_len=5, fit_scaler=False)
        assert X.shape[1:] == (1, 5)
        assert (np.diff(X[:, 0, :], axis=1) == 1.0).all()               # five consecutive sessions, no gap
        ends = X[:, 0, -1].astype(int)
        assert (ends % 3 == 0).all() and len(X) == len([i for i in range(4, 120) if i % 3 == 0])   # only labelled windows
        assert y.dtype == np.int64 and set(np.unique(y)) <= {0, 1}
        np.testing.assert_array_equal(y, f["y"].to_numpy()[ends].astype(np.int64))

    def test_the_three_way_split_windows_each_block_on_full_sessions(self, monkeypatch):
        svc = self.tsai(monkeypatch)
        out = svc.prepare_split_three_way([sparse_frame(seed=1), sparse_frame(seed=2)], 0.7, 0.5, "y", ["t_a"], seq_len=5,
                                          label_horizon=3)
        for k in ("X_train", "X_val", "X_holdout"):
            assert (np.diff(out[k][:, 0, :], axis=1) == 1.0).all() and len(out[k]) > 0

    def test_a_dense_target_is_unchanged(self, monkeypatch):
        svc = self.tsai(monkeypatch)
        f = sparse_frame(keep_every=1)
        X, y = svc.prepare_data(f, "y", ["t_a"], seq_len=5, fit_scaler=False)
        assert len(X) == 116 and y.dtype == np.int64

    def test_only_the_edges_are_trimmed_from_a_job_frame(self):
        from app.services.job_handler import trim_unlabelled_edges
        f = sparse_frame()
        f.loc[:4, "y"] = np.nan
        f.loc[f.index[-6:], "y"] = np.nan
        out, n_edge, n_interior = trim_unlabelled_edges(f, "y")
        assert out["y"].iloc[0] == out["y"].dropna().iloc[0] and not np.isnan(out["y"].iloc[0]) and not np.isnan(out["y"].iloc[-1])
        assert (np.diff(out["t_a"]) == 1).all()                         # consecutive sessions kept
        assert n_edge + len(out) == len(f) and n_interior == int(out["y"].isna().sum()) > 0

    def test_a_quintile_job_keeps_every_session_of_every_ticker(self, monkeypatch):
        from tests.test_ml_wizard_daily_targets import TestJobIntegration, job_frames, pt
        result, cap = TestJobIntegration()._job(job_frames(10), [pt("daily_quintile", horizonSessions=3)], monkeypatch)
        assert result["status"] == "dry_run_success", result
        comb = cap["combined"]
        assert comb["quintile_h3"].isna().sum() > 0.4 * len(comb)       # the middle fifths are NaN rows, and they are kept
        for _, g in comb.groupby("ticker"):
            d = pd.to_datetime(g["Date"]).sort_values()
            assert (d.diff().dropna().dt.days <= 3).all()               # business days only: no session dropped inside

    def test_darts_refuses_a_target_with_gaps_inside_the_series(self, monkeypatch):
        from tests.test_ml_wizard_daily_targets import TestJobIntegration, job_frames, pt
        result, _ = TestJobIntegration()._job(job_frames(10), [pt("daily_quintile", horizonSessions=3)], monkeypatch,
                                              job_type="regression")
        assert result["status"] == "failed"
        assert "quintile_h3" in result["error"] and "classification" in result["error"] and "gaps" in result["error"]

    def test_darts_trains_a_dense_target_as_before(self, monkeypatch):
        from tests.test_ml_wizard_daily_targets import TestJobIntegration, job_frames, pt
        result, _ = TestJobIntegration()._job(job_frames(6), [pt("daily_rel_return", horizonSessions=3)], monkeypatch,
                                              job_type="regression")
        assert result["status"] == "dry_run_success", result
