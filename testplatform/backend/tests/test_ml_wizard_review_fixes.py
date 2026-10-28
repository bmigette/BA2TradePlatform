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
