"""The mask-fill medians are saved with the model (darts and tsai), prediction fills from them and never refits, a model saved
before this refuses a masked frame with a 409, and every ticker has the same feature width."""
import json
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

from app.services import masked_fill as MF
from app.services import tsai_training as TT
from app.services.darts_training import DartsTrainingService
from app.services.data_preparation import DataPreparationService

N = 300
RATIO, HOLD = 0.7, 0.5
COLS = ["t_a", "f_a", "f_b", "t_missing", "f_missing"]
MASK = (120, 200)


def frame(seed, ticker=None, n=N, masked=MASK, all_masked=False, scale=1.0):
    rng = np.random.default_rng(seed)
    d = pd.DataFrame({
        "Date": pd.date_range("2020-01-01", periods=n, freq="D"),
        "y": rng.integers(0, 2, n), "t_a": rng.normal(size=n),
        "f_a": rng.normal(10, 1, n) * scale, "f_b": rng.normal(-5, 1, n) * scale,
        "t_missing": 0.0, "f_missing": 0.0})
    if ticker is not None:
        d["ticker"] = ticker
    if all_masked:
        d[["f_a", "f_b"]] = np.nan
        d["f_missing"] = 1.0
    elif masked:
        lo, hi = masked
        d.loc[lo:hi, ["f_a", "f_b"]] = np.nan
        d.loc[lo:hi, "f_missing"] = 1.0
    return d


@pytest.fixture
def tsai(monkeypatch):
    monkeypatch.setattr(TT, "TSAI_AVAILABLE", True)
    return TT.TSAITrainingService(normalize=True)


# ---------------------------------------------------------------------------------------------- the helpers
class TestSavedFill:
    def params(self):
        return MF.build_masked_fill({"AAA#": {"f_a": 10.0, "f_b": -5.0}, "BBB#": {"f_a": 20.0, "f_b": -6.0}}, ["g_a"])

    def test_the_pooled_median_is_the_median_of_the_tickers(self):
        p = self.params()
        assert p["pooled"] == {"f_a": 15.0, "f_b": -5.5} and p["dropped_masked_columns"] == ["g_a"] and p["version"] == 1
        json.dumps(p)

    def test_masked_cells_take_the_saved_median_of_the_ticker_not_the_frame_s_own(self):
        pred = frame(1, scale=1000.0)                              # values a training median could never produce
        out, cols = MF.apply_saved_fill(pred, COLS + ["g_a"], self.params(), ticker="AAA")
        assert "g_a" not in cols
        np.testing.assert_array_equal(out.loc[MASK[0]:MASK[1], "f_a"].to_numpy(), 10.0)
        np.testing.assert_array_equal(out.loc[MASK[0]:MASK[1], "f_b"].to_numpy(), -5.0)
        pd.testing.assert_series_equal(out["f_a"].iloc[:MASK[0]], pred["f_a"].iloc[:MASK[0]])   # unmasked rows untouched
        assert pred["f_a"].isna().any()                            # the input frame is not modified

    def test_a_ticker_column_picks_each_rows_ticker(self):
        a, b = frame(2, "AAA"), frame(3, "BBB")
        out, _ = MF.apply_saved_fill(pd.concat([a, b], ignore_index=True), COLS, self.params())
        assert (out.loc[(out["ticker"] == "AAA") & (out["f_missing"] == 1), "f_a"] == 10.0).all()
        assert (out.loc[(out["ticker"] == "BBB") & (out["f_missing"] == 1), "f_a"] == 20.0).all()

    def test_an_unknown_ticker_gets_the_pooled_median(self):
        out, _ = MF.apply_saved_fill(frame(4), COLS, self.params(), ticker="ZZZ")
        assert (out.loc[MASK[0]:MASK[1], "f_a"] == 15.0).all()

    def test_a_model_trained_on_one_unnamed_dataset_serves_any_ticker(self):
        p = MF.build_masked_fill({MF.SINGLE_KEY: {"f_a": 7.0, "f_b": 8.0}}, [])
        out, _ = MF.apply_saved_fill(frame(5), COLS, p, ticker="WHATEVER")
        assert (out.loc[MASK[0]:MASK[1], "f_a"] == 7.0).all()

    def test_a_column_the_model_never_saw_masked_refuses(self):
        p = MF.build_masked_fill({"AAA#": {"f_a": 1.0}}, [])
        with pytest.raises(MF.StaleMaskedModel, match="retrain.*f_b"):
            MF.apply_saved_fill(frame(6), COLS, p, ticker="AAA")

    def test_an_old_model_refuses_a_masked_frame_and_passes_a_clean_one(self):
        with pytest.raises(MF.StaleMaskedModel, match="retrain: this model predates masked-cell handling"):
            MF.apply_saved_fill(frame(7), COLS, None, ticker="AAA")
        clean = frame(7, masked=None)
        out, cols = MF.apply_saved_fill(clean, COLS, None)
        assert out is clean and cols == COLS

    def test_ordinary_frames_pass_through_a_saved_fill(self):
        d = frame(8, masked=None)[["Date", "y", "t_a", "f_a"]]
        out, cols = MF.apply_saved_fill(d, ["t_a", "f_a"], self.params(), ticker="AAA")
        assert cols == ["t_a", "f_a"] and out["f_a"].equals(d["f_a"])

    def test_all_masked_columns_are_the_union_across_the_tickers(self):
        frames = [frame(9), frame(10, all_masked=True)]
        assert MF.all_masked_columns(frames, COLS) == ["f_a", "f_b"]
        assert MF.all_masked_columns([frame(9)], COLS) == []


# ---------------------------------------------------------------------------------------------- darts
class TestDartsPersistsAndKeepsOneWidth:
    def prep(self, frames, **kw):
        svc = DartsTrainingService()
        out = svc.prepare_split_three_way(frames, RATIO, HOLD, "y", COLS, timeframe="1d", label_horizon=3, **kw)
        return svc, out

    def test_the_medians_are_saved_per_ticker_with_the_scaler_params(self):
        a, b = frame(1, "AAA"), frame(2, "BBB")
        svc, _ = self.prep([a, b])
        mf = svc.masked_fill
        n_train = int(N * RATIO) - 3
        for key, f in (("AAA#", a), ("BBB#", b)):
            train = f.iloc[:int(N * RATIO)]
            median = train.loc[train["f_missing"] == 0, "f_a"].median()
            assert mf["medians"][key]["f_a"] == pytest.approx(median) and set(mf["medians"][key]) == {"f_a", "f_b"}
        assert mf["dropped_masked_columns"] == [] and set(mf["pooled"]) == {"f_a", "f_b"}
        params = svc.get_normalization_params()
        assert params["masked_fill"] == mf
        described = DartsTrainingService.describe_scalers(svc.scalers, svc.cov_scalers, masked_fill=mf)
        assert described["masked_fill"] == mf
        json.dumps(described)
        assert n_train > 0

    def test_a_group_masked_in_every_row_of_one_ticker_is_dropped_for_all(self):
        a, b = frame(3, "AAA"), frame(4, "BBB", all_masked=True)
        svc, out = self.prep([a, b])
        widths = {s.width for s in out["train_covariates"]}
        assert widths == {3}                                       # t_a, t_missing, f_missing for BOTH tickers
        assert svc.masked_fill["dropped_masked_columns"] == ["f_a", "f_b"]
        assert "BBB" not in svc.masked_fill["medians"] or svc.masked_fill["medians"]["BBB#"] == {}
        assert set(svc.masked_fill["medians"]["AAA#"]) == set()      # the dropped columns are not filled either

    def test_the_prediction_round_trip_fills_from_the_saved_medians_and_never_refits(self):
        a, b = frame(5, "AAA"), frame(6, "BBB")
        svc, _ = self.prep([a, b])
        saved = json.loads(json.dumps(svc.get_normalization_params()))["masked_fill"]
        pred = frame(7, scale=1000.0)
        filled, cols = MF.apply_saved_fill(pred, COLS, saved, ticker="AAA")
        np.testing.assert_array_equal(filled.loc[MASK[0]:MASK[1], "f_a"].to_numpy(), saved["medians"]["AAA#"]["f_a"])
        series, cov = DartsTrainingService().prepare_data(filled, "y", cols, "1d")
        assert not np.isnan(cov.values()).any() and len(series) == N


# ---------------------------------------------------------------------------------------------- tsai
class TestTsaiPersistsAndRoundTrips:
    def test_the_medians_are_saved_in_the_normalization_params(self, tsai):
        a, b = frame(1, "AAA"), frame(2, "BBB")
        tsai.prepare_split_three_way([a, b], RATIO, HOLD, "y", COLS, seq_len=4, label_horizon=3)
        params = json.loads(json.dumps(tsai.get_normalization_params()))
        mf = params["masked_fill"]
        assert set(mf["medians"]) == {"AAA#", "BBB#"} and mf["dropped_masked_columns"] == []
        train = a.iloc[:int(N * RATIO) - 3]
        assert mf["medians"]["AAA#"]["f_a"] == pytest.approx(train.loc[train["f_missing"] == 0, "f_a"].median())
        assert "valid_columns" in params                            # the scaler params are still there

    def test_the_dropped_columns_are_saved_and_the_feature_width_is_one(self, tsai):
        a, b = frame(3, "AAA"), frame(4, "BBB", all_masked=True)
        tsai.prepare_split_three_way([a, b], RATIO, HOLD, "y", COLS, seq_len=4, label_horizon=3)
        assert tsai.get_normalization_params()["masked_fill"]["dropped_masked_columns"] == ["f_a", "f_b"]
        assert set(tsai.data_prep.get_valid_columns()) == {"t_a", "f_missing"}

    def test_the_prediction_round_trip(self, tsai):
        a, b = frame(5, "AAA"), frame(6, "BBB")
        tsai.prepare_split_three_way([a, b], RATIO, HOLD, "y", COLS, seq_len=4, label_horizon=3)
        saved = json.loads(json.dumps(tsai.get_normalization_params()))
        # a fresh service, as the prediction endpoint builds it: scaler params from the record, nothing refitted
        pred_svc = TT.TSAITrainingService(normalize=True)
        pred_svc.data_prep = DataPreparationService()
        pred_svc.data_prep.load_params(saved)
        before = pred_svc.data_prep.export_params()["columns"]
        pred = frame(7, scale=1000.0)
        filled, cols = MF.apply_saved_fill(pred, COLS, saved["masked_fill"], ticker="AAA")
        np.testing.assert_array_equal(filled.loc[MASK[0]:MASK[1], "f_a"].to_numpy(), saved["masked_fill"]["medians"]["AAA#"]["f_a"])
        X, _ = pred_svc.prepare_data(filled, "y", cols, seq_len=4, fit_scaler=False)
        assert not np.isnan(X).any()
        assert pred_svc.data_prep.export_params()["columns"] == before

    def test_a_masked_prediction_frame_that_skips_the_saved_fill_is_refused(self, tsai):
        svc = tsai
        with pytest.raises(ValueError, match="masked"):
            svc.prepare_data(frame(8), "y", COLS, seq_len=4, fit_scaler=False)


# ---------------------------------------------------------------------------------------------- API
class TestPredictionApi:
    def test_the_helper_maps_a_stale_model_to_409(self):
        from app.api.models import apply_masked_fill_for_prediction
        with pytest.raises(HTTPException) as exc:
            apply_masked_fill_for_prediction(frame(1), COLS, {"valid_columns": []}, "AAA")
        assert exc.value.status_code == 409 and "retrain: this model predates masked-cell handling" in exc.value.detail
        out, cols = apply_masked_fill_for_prediction(
            frame(1), COLS, {"masked_fill": MF.build_masked_fill({"AAA#": {"f_a": 1.0, "f_b": 2.0}}, [])}, "AAA")
        assert not out[["f_a", "f_b"]].isna().any().any()
        out, cols = apply_masked_fill_for_prediction(frame(2, masked=None), COLS, None, "AAA")      # old model, clean frame
        assert cols == COLS

    def _call_endpoint(self, tmp_path, normalization_params):
        from unittest import mock
        from app.api import models as M
        csv = tmp_path / "ds.csv"
        frame(1).to_csv(csv, index=False)
        model_file = tmp_path / "m.pt"
        model_file.write_bytes(b"x")
        record = {"filePath": str(model_file), "datasetId": 1, "jobId": None, "modelType": "lstm",
                  "targetColumns": ["y"], "predictionHorizon": 1, "predictionMode": "shift",
                  "hyperparameters": {"featureColumns": COLS, "c_in": len(COLS), "seqLen": 4},
                  "normalizationParams": normalization_params}
        dataset = MagicMock(file_path=str(csv), ticker="AAA")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = dataset
        with mock.patch.object(M, "get_model_by_id", return_value=record):
            return M.run_model_predictions("m1", None, db)

    def test_the_endpoint_answers_409_not_500_for_a_model_saved_before(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            self._call_endpoint(tmp_path, {"valid_columns": COLS, "dropped_columns": [], "columns": {}})
        assert exc.value.status_code == 409
        assert "retrain: this model predates masked-cell handling" in exc.value.detail


# ---------------------------------------------------------------------------------------------- the job
class TestJobSavesTheFeatureListAfterTheDrop:
    def test_an_all_masked_group_of_one_ticker_leaves_the_feature_list_of_every_ticker(self, monkeypatch):
        from tests.test_ml_wizard_daily_targets import TestJobIntegration, daily_frame, pt
        frames = [daily_frame(10), daily_frame(11)]
        for f in frames:
            f["f_a"] = np.random.default_rng(0).normal(size=len(f))
            f["f_missing"] = 0.0
        frames[1]["f_a"] = np.nan
        frames[1]["f_missing"] = 1.0
        result, cap = TestJobIntegration()._job(frames, [pt("daily_up", horizonSessions=3)], monkeypatch)
        assert result["status"] == "dry_run_success", result
        assert cap["features"] == ["t_a", "f_missing"]            # f_a gone for both tickers, its flag stays


class TestBacktestHook:
    def _model(self, normalization_params):
        m = MagicMock()
        m.model_type = "lstm"
        m.model_id = "m1"
        m.hyperparameters = {"featureColumns": COLS, "c_in": len(COLS), "seqLen": 4}
        m.prediction_mode = "shift"
        m.threshold = 0.5
        m.file_path = None
        m.normalization_params = normalization_params
        return m

    def test_a_model_saved_before_fails_the_backtest_with_the_retrain_message(self, monkeypatch):
        from app.services import backtest_handler as BH
        monkeypatch.setattr(BH, "ensure_out_of_sample", lambda *a, **k: None)
        res = BH.run_backtest(self._model({"valid_columns": COLS}), frame(1), frame(1), {}, ticker="AAA")
        assert res["status"] == "failed" and "retrain: this model predates masked-cell handling" in res["error"]

    def test_a_new_model_gets_its_saved_medians(self, monkeypatch):
        from app.services import backtest_handler as BH
        monkeypatch.setattr(BH, "ensure_out_of_sample", lambda *a, **k: None)
        seen = {}

        def fake_ffill(df):
            seen["df"] = df.copy()
            raise RuntimeError("stop here")
        monkeypatch.setattr(BH, "ffill_sparse_indicators", fake_ffill)
        params = {"masked_fill": MF.build_masked_fill({"AAA#": {"f_a": 3.0, "f_b": 4.0}}, [])}
        with pytest.raises(RuntimeError, match="stop here"):
            BH.run_backtest(self._model(params), frame(1), frame(1), {}, ticker="AAA")
        assert (seen["df"].loc[MASK[0]:MASK[1], "f_a"] == 3.0).all()
