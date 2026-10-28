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
