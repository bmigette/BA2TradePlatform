"""Dataset wizard integration of the custom ML models: the alpaca_hist source, the panel_features option, the
wizard targets and the ban on the current-fundamentals broadcast as a model feature
(docs/plans/2026-10-27-custom-ml-models-design.md)."""
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

from app.services import ml_panel
from app.services import ml_wizard_targets as W


# ---------------------------------------------------------------------------------------------- helpers
def _bars(drift, n_sessions=120, seed=0, noise=0.002):
    """One symbol's wizard dataset: 7 bars per session stamped at the bar END (ET, naive), a t_ feature column."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2024-01-02", periods=n_sessions)
    ends = [d + pd.Timedelta(hours=h, minutes=m) for d in days for h, m in
            ((10, 30), (11, 30), (12, 30), (13, 30), (14, 30), (15, 30), (16, 0))]
    close = 100.0 * np.exp(np.cumsum(drift + rng.normal(0, noise, len(ends))))
    open_ = np.concatenate([[100.0], close[:-1]])
    return pd.DataFrame({
        "Date": ends, "Open": open_, "High": np.maximum(open_, close) * 1.001, "Low": np.minimum(open_, close) * 0.999,
        "Close": close, "Volume": 1000.0, "t_ret_1": np.log(close / open_),
    })


class _FakeDb:
    def __init__(self):
        self.added = []
        self.commit = MagicMock()
        self.refresh = MagicMock()
        self.rollback = MagicMock()

    def add(self, obj):
        obj.id = len(self.added) + 1
        self.added.append(obj)


# ---------------------------------------------------------------------------------------------- payload -> generation_config
class TestPanelFeaturesPayload:
    def _create(self, **overrides):
        from app.api.datasets import create_dataset
        from app.schemas.dataset import DatasetCreate
        body = dict(ticker="AAPL", timeframe="1h", start_date="2025-01-02", end_date="2025-06-30",
                    data_provider="alpaca_hist", panel_features=["daily", "market", "fundamentals"])
        body.update(overrides)
        db, executor = _FakeDb(), MagicMock()
        with patch("app.api.datasets._dataset_executor", executor):
            create_dataset(DatasetCreate(**body), db)
        return db, executor

    def test_create_persists_panel_features_in_generation_config_and_build_config(self):
        db, executor = self._create()
        assert db.added[0].generation_config["panel_features"] == ["daily", "market", "fundamentals"]
        assert db.added[0].generation_config["data_provider"] == "alpaca_hist"
        build_config = executor.submit.call_args[0][2]
        assert build_config["panel_features"] == ["daily", "market", "fundamentals"]

    def test_default_is_no_panel_features(self):
        db, _ = self._create(panel_features=None)
        assert db.added[0].generation_config["panel_features"] == []

    def test_batch_create_round_trip(self):
        from app.api.datasets import create_batch_datasets
        db, executor = _FakeDb(), MagicMock()
        with patch("app.api.datasets._dataset_executor", executor):
            create_batch_datasets({"symbols": ["AAPL", "MSFT"], "timeframe": "15m", "data_provider": "alpaca_hist",
                                   "panel_features": ["market"]}, db)
        assert [d.generation_config["panel_features"] for d in db.added] == [["market"], ["market"]]
        assert executor.submit.call_args[0][2]["panel_features"] == ["market"]

    @pytest.mark.parametrize("overrides, text", [
        (dict(data_provider="yfinance"), "need the alpaca_hist data provider"),
        (dict(timeframe="1d"), "1h', '15m'"),
        (dict(panel_features=["news"]), "unknown panel_features"),
        (dict(fundamentals_config={"enabled": True, "live_only_snapshot": True}), "live_only_snapshot"),
    ])
    def test_invalid_requests_are_refused(self, overrides, text):
        with pytest.raises(HTTPException) as exc:
            self._create(**overrides)
        assert exc.value.status_code == 400
        assert text in exc.value.detail

    def test_schemas_carry_the_field(self):
        from app.schemas.dataset import DatasetCreate, DatasetUpdate
        assert DatasetUpdate(panel_features=["daily"]).panel_features == ["daily"]
        assert DatasetUpdate().panel_features is None
        assert DatasetCreate(ticker="A", timeframe="1h").panel_features is None


# ---------------------------------------------------------------------------------------------- the single build helper
class TestFetchOhlcvFrame:
    def test_alpaca_hist_1h_reads_the_panel_not_the_network(self, monkeypatch):
        from app.services.ml_dataset_source import fetch_ohlcv_frame
        seen = {}

        def fake_ticker_frame(symbol, timeframe, first_day, last_day, groups, cache_root):
            seen.update(symbol=symbol, timeframe=timeframe, first=first_day, last=last_day, groups=list(groups),
                        root=cache_root)
            f = _bars(0.0, 3).set_index("Date")
            return f

        monkeypatch.setattr(ml_panel, "ticker_frame", fake_ticker_frame)
        with patch("app.api.datasets.get_ohlcv_provider", side_effect=AssertionError("network provider used")):
            df = fetch_ohlcv_frame("alpaca_hist", "aapl", "1h", datetime(2025, 1, 2), datetime(2025, 6, 30),
                                   ["daily", "market"])
        assert seen["symbol"] == "aapl" and seen["timeframe"] == "1h"
        assert seen["groups"] == ["technical", "daily", "market"]
        assert str(seen["first"]) == "2025-01-02" and str(seen["last"]) == "2025-06-30"
        assert "Date" in df.columns and "t_ret_1" in df.columns and len(df) == 21

    def test_other_providers_are_unchanged(self, monkeypatch):
        from app.services.ml_dataset_source import fetch_ohlcv_frame
        monkeypatch.setattr(ml_panel, "ticker_frame", lambda *a, **k: pytest.fail("panel used"))
        point = MagicMock(timestamp=datetime(2025, 1, 2), open=1.0, high=2.0, low=0.5, close=1.5, volume=10)
        provider = MagicMock()
        provider.get_data.return_value = [point]
        with patch("app.api.datasets.get_ohlcv_provider", return_value=provider):
            df = fetch_ohlcv_frame("yfinance", "AAPL", "1h", datetime(2025, 1, 1), datetime(2025, 1, 3))
        assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
        assert provider.get_data.call_args.kwargs["interval"] == "1h"

    def test_alpaca_hist_refuses_other_timeframes_and_the_live_snapshot(self):
        from app.services.ml_dataset_source import fetch_ohlcv_frame
        with pytest.raises(ValueError, match="1h', '15m'"):
            fetch_ohlcv_frame("alpaca_hist", "AAPL", "1d", datetime(2025, 1, 1), datetime(2025, 2, 1))
        with pytest.raises(ValueError, match="live_only_snapshot"):
            fetch_ohlcv_frame("alpaca_hist", "AAPL", "1h", datetime(2025, 1, 1), datetime(2025, 2, 1), [],
                              {"enabled": True, "live_only_snapshot": True})


class TestBuildPathsShareTheHelper:
    """create (background build), regenerate and duplicate all go through fetch_ohlcv_frame with the stored groups."""

    def _frame(self):
        return _bars(0.0, 5)

    def test_background_build(self, tmp_path):
        from app.api import datasets as D
        calls = []
        dataset = MagicMock(file_path=str(tmp_path / "ds.csv"))
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = dataset
        with patch.object(D, "SessionLocal", return_value=session), \
                patch.object(D, "fetch_ohlcv_frame", side_effect=lambda *a: calls.append(a) or self._frame()):
            D._build_dataset_in_background(1, {
                "ticker": "AAPL", "timeframe": "1h", "start_date": "2024-01-02", "end_date": "2024-01-10",
                "data_provider": "alpaca_hist", "panel_features": ["daily"], "technical_indicators": [],
                "sentiment_config": {}, "fundamentals_config": {}})
        assert calls and calls[0][0] == "alpaca_hist" and calls[0][5] == ["daily"]
        assert "t_ret_1" in pd.read_csv(tmp_path / "ds.csv").columns

    def test_regeneration_reads_panel_features_from_generation_config(self, tmp_path):
        from app.api import datasets as D
        from app.schemas.dataset import DatasetRegenerate
        calls = []
        session = MagicMock()
        with patch.object(D, "SessionLocal", return_value=session), \
                patch.object(D, "fetch_ohlcv_frame", side_effect=lambda *a: calls.append(a) or self._frame()):
            D._regenerate_dataset_in_background(1, {
                "regen_options": DatasetRegenerate(), "ticker": "AAPL", "timeframe": "1h",
                "file_path": str(tmp_path / "ds.csv"), "technical_indicators": None, "fundamentals_config": None,
                "sentiment_config": None, "start_date": datetime(2024, 1, 2), "end_date": datetime(2024, 1, 10),
                "gen_config": {"data_provider": "alpaca_hist", "panel_features": ["market", "fundamentals"]}})
        assert calls and calls[0][5] == ["market", "fundamentals"]
        assert "t_ret_1" in pd.read_csv(tmp_path / "ds.csv").columns

    def test_partial_regeneration_keeps_the_panel_columns(self, tmp_path):
        from app.api import datasets as D
        from app.schemas.dataset import DatasetRegenerate
        path = tmp_path / "ds.csv"
        self._frame().to_csv(path, index=False)
        session = MagicMock()
        with patch.object(D, "SessionLocal", return_value=session), \
                patch.object(D, "fetch_ohlcv_frame", side_effect=AssertionError("OHLCV not regenerated")):
            D._regenerate_dataset_in_background(1, {
                "regen_options": DatasetRegenerate(regenerate_ohlcv=False, regenerate_technical=True),
                "ticker": "AAPL", "timeframe": "1h", "file_path": str(path), "technical_indicators": None,
                "fundamentals_config": None, "sentiment_config": None, "start_date": datetime(2024, 1, 2),
                "end_date": datetime(2024, 1, 10),
                "gen_config": {"data_provider": "alpaca_hist", "panel_features": []}})
        assert "t_ret_1" in pd.read_csv(path).columns

    def test_duplicate(self, tmp_path):
        from app.api import datasets as D
        from app.models.dataset import Dataset
        from app.schemas.dataset import DatasetDuplicate
        calls = []
        original = Dataset(id=7, name="o", ticker="AAPL", timeframe="1h", start_date=datetime(2024, 1, 2),
                           end_date=datetime(2024, 1, 10), rows_count=1, file_path="x.csv", technical_indicators=None,
                           generation_config={"data_provider": "alpaca_hist", "panel_features": ["daily"],
                                              "original_start_date": "2024-01-02", "original_end_date": "2024-01-10"})
        db = _FakeDb()
        db.query = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = original
        with patch.object(D, "DATASETS_DIR", tmp_path), \
                patch.object(D, "fetch_ohlcv_frame", side_effect=lambda *a: calls.append(a) or self._frame()):
            new = D.duplicate_dataset(7, DatasetDuplicate(new_ticker="MSFT"), db)
        assert calls[0][:3] == ("alpaca_hist", "MSFT", "1h") and calls[0][5] == ["daily"]
        assert new.generation_config["panel_features"] == ["daily"]


class TestUpdatePath:
    def _update(self, generation_config, update, fetched):
        from app.api import datasets as D
        from app.models.dataset import Dataset
        from app.schemas.dataset import DatasetUpdate
        ds = Dataset(id=3, name="o", ticker="AAPL", timeframe="1h", start_date=datetime(2024, 1, 2),
                     end_date=datetime(2024, 1, 10), rows_count=1, file_path="x.csv", technical_indicators=None,
                     fundamentals_config=None, generation_config=generation_config)
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = ds
        queue = MagicMock()
        with patch("app.models.database.SessionLocal", return_value=session),                 patch("app.services.task_queue.get_task_queue", return_value=queue),                 patch.object(D, "SessionLocal", return_value=session),                 patch.object(D, "fetch_ohlcv_frame", side_effect=lambda *a: fetched.append(a) or _bars(0.0, 5)):
            D.update_dataset(3, DatasetUpdate(**update))
        return ds, queue

    def test_edit_keeps_and_updates_the_panel_features(self):
        fetched = []
        gc = {"data_provider": "alpaca_hist", "panel_features": ["daily"], "original_start_date": "2024-01-02",
              "original_end_date": "2024-01-10"}
        ds, queue = self._update(gc, {}, fetched)
        assert fetched[0][5] == ["daily"] and ds.generation_config["panel_features"] == ["daily"]
        ds, queue = self._update(gc, {"panel_features": ["market", "fundamentals"]}, fetched)
        assert ds.generation_config["panel_features"] == ["market", "fundamentals"]
        records = queue.queue_task.call_args.kwargs["payload"]["ohlcv_data"]
        assert "t_ret_1" in records[0] and isinstance(records[0]["Date"], str)

    def test_leaving_alpaca_hist_drops_the_inherited_groups(self):
        fetched = []
        gc = {"data_provider": "alpaca_hist", "panel_features": ["daily"], "original_start_date": "2024-01-02",
              "original_end_date": "2024-01-10"}
        with patch("app.api.datasets.get_ohlcv_provider"):
            ds, _ = self._update(gc, {"data_provider": "yfinance", "timeframe": "1d"}, fetched)
        assert ds.generation_config["panel_features"] == []


# ---------------------------------------------------------------------------------------------- wizard targets
def _tb(**kw):
    return {"type": "triple_barrier_atr", **kw}


class TestWizardTargets:
    def _job(self, frames, targets, ratio=0.7, provider="alpaca_hist", timeframe="1h"):
        from app.services.job_handler import build_prediction_targets
        return [build_prediction_targets(f.copy(), targets, timeframe, train_ratio=ratio,
                                         panel_source=provider == "alpaca_hist") for f in frames]

    def test_column_names(self):
        assert W.column_name(_tb()) == "tb_atr_h5_r1"
        assert W.column_name(_tb(horizonSessions=3, slRatio=1.5)) == "tb_atr_h3_r1.5"
        assert W.column_name(_tb(horizonSessions=2, tpAtr=1.25)) == "tb_atr_h2_r1_k1.25"
        assert W.column_name({"type": "direction_terciles"}) == "dir_terc_h3"
        assert W.column_name({"type": "forward_return", "horizonSessions": 10}) == "fwd_ret_h10"

    def test_triple_barrier_is_per_symbol_and_registers_the_lookahead(self):
        up, down = _bars(0.0015, seed=1), _bars(-0.0015, seed=2)
        (fa, col, cols, hz), (fb, _, _, _) = self._job([up, down], [_tb(horizonSessions=3, tpAtr=0.5)])
        assert col == "tb_atr_h3_r1_k0.5" and cols == [col]
        assert hz[col] == 3 * 7                                  # label horizon in bars: the split's purge length
        # each symbol is labelled against ITS OWN bars: the riser mostly wins, the faller mostly loses
        assert (fa[col] == 2).sum() > 0.7 * fa[col].notna().sum()
        assert (fb[col] == 0).sum() > 0.7 * fb[col].notna().sum()
        assert fa[col].isna().sum() >= 3 * 7                     # unobservable tail left as NaN, never fabricated

    def test_fitted_k_balances_the_timeout_share_on_the_train_block_only(self):
        walk = _bars(0.0, seed=3, noise=0.004)
        (f, col, _, hz), = self._job([walk], [_tb(horizonSessions=3)], ratio=0.7)
        assert col == "tb_atr_h3_r1"
        lab = f[col].dropna().reset_index(drop=True)
        n_train = int(len(lab) * 0.7) - hz[col]
        assert abs((lab.iloc[:n_train] == 1).mean() - 1 / 3) < 0.12      # k was chosen to make timeouts ~ a third
        assert set(lab.unique()) <= {0.0, 1.0, 2.0}

    def test_the_mask_fed_to_the_fit_is_the_split_train_block(self):
        obs = np.ones(100, dtype=bool)
        obs[:10] = False                                          # unlabelled leading rows (no ATR yet)
        m = W.train_block_mask(obs, 0.7, 5)
        assert np.flatnonzero(m).tolist() == list(range(10, 10 + int(90 * 0.7) - 5))
        with pytest.raises(W.WizardTargetError, match="too few"):
            W.train_block_mask(obs, 0.05, 5)

    def test_direction_terciles_are_fitted_on_the_train_rows(self):
        regime = pd.concat([_bars(0.0, 100, seed=4), _bars(0.004, 40, seed=5)], ignore_index=True)
        regime["Date"] = _bars(0.0, 140, seed=4)["Date"]          # one continuous series, calm then a strong rise
        (f, col, _, hz), = self._job([regime], [{"type": "direction_terciles", "horizonSessions": 2}], ratio=0.7)
        assert col == "dir_terc_h2" and hz[col] == 14
        lab = f[col].dropna().reset_index(drop=True)
        train = lab.iloc[:int(len(lab) * 0.7) - 14]
        assert max((train == c).mean() for c in (0, 1, 2)) < 0.4                # terciles on the training rows
        assert (lab.iloc[int(len(lab) * 0.7):] == 2).mean() > 0.5               # the later rise is NOT rebalanced away

    def test_forward_return_is_the_n_session_return_per_symbol(self):
        a, b = _bars(0.001, seed=6), _bars(-0.001, seed=7)
        (fa, col, _, hz), (fb, _, _, _) = self._job([a, b], [{"type": "forward_return", "horizonSessions": 2}])
        assert col == "fwd_ret_h2" and hz[col] == 14
        expected = a["Close"].shift(-14) / a["Close"] - 1
        np.testing.assert_allclose(fa[col].values, expected.values, equal_nan=True)
        assert (fb[col].dropna() < 0).mean() > 0.9

    @pytest.mark.parametrize("kwargs, text", [
        (dict(provider="yfinance"), "Alpaca history cache"),
        (dict(timeframe="1d"), "Alpaca history cache"),
    ])
    def test_refused_on_other_sources(self, kwargs, text):
        from app.services.job_handler import TargetBuildError
        for t in ({"type": "triple_barrier_atr"}, {"type": "direction_terciles"}, {"type": "forward_return"}):
            with pytest.raises(TargetBuildError, match=text):
                self._job([_bars(0.0, 40)], [t], **kwargs)

    def test_needs_the_train_ratio_and_a_valid_config(self):
        from app.services.job_handler import TargetBuildError, build_prediction_targets
        with pytest.raises(TargetBuildError, match="train ratio"):
            build_prediction_targets(_bars(0.0, 40), [_tb()], "1h", panel_source=True)
        with pytest.raises(TargetBuildError, match="horizonSessions"):
            self._job([_bars(0.0, 40)], [_tb(horizonSessions=0)])
        with pytest.raises(TargetBuildError, match="Unknown target type"):
            self._job([_bars(0.0, 40)], [{"type": "nope"}])

    def test_training_job_wires_the_ratio_and_the_source(self, monkeypatch):
        from app.services import job_handler
        frames = {1: (_bars(0.0015, seed=1), "AAA"), 2: (_bars(-0.0015, seed=2), "BBB")}
        captured = {}
        monkeypatch.setattr(job_handler, "ML_AVAILABLE", True)
        monkeypatch.setattr(job_handler, "load_dataset", lambda i, start_date=None, end_date=None: frames[i][0].copy())
        monkeypatch.setattr(job_handler, "get_dataset_info", lambda i: {
            "ticker": frames[i][1], "timeframe": "1h", "data_provider": "alpaca_hist"})
        monkeypatch.setattr(job_handler, "update_job_progress", lambda *a, **k: None)
        monkeypatch.setattr(job_handler, "save_training_datasets", lambda **kw: captured.update(
            combined=kw["combined_df"], train=kw["train_df"]))
        payload = {
            "dataset_ids": [1, 2], "job_type": "classification", "selected_models": ["lstm"], "train_test_split": 70,
            "prediction_horizon": 1, "prediction_modes": ["shift"], "genetic_config": {"populationSize": 2},
            "metrics_config": {"classificationMetric": "f1_score"},
            "prediction_targets": [{"type": "triple_barrier_atr", "horizonSessions": 3, "tpAtr": 0.5}],
        }
        result = job_handler.handle_training_job("t-wizard", payload, dry_run=True)
        assert result["status"] == "dry_run_success", result
        combined = captured["combined"]
        assert (combined.loc[combined["ticker"] == "AAA", "tb_atr_h3_r1_k0.5"] == 2).mean() > 0.7
        assert (combined.loc[combined["ticker"] == "BBB", "tb_atr_h3_r1_k0.5"] == 0).mean() > 0.7

    def test_preview_service_computes_the_wizard_targets(self):
        from app.services.darts_models import PredictionTargetService
        res = PredictionTargetService().calculate_all_targets(
            _bars(0.0, 60, seed=8), [_tb(horizonSessions=2), {"type": "forward_return", "horizonSessions": 2}],
            dataset_timeframe="1h", panel_source=True)
        assert [r["columnName"] for r in res] == ["tb_atr_h2_r1", "fwd_ret_h2"]
        assert res[0]["category"] == "multiclass_classification" and res[1]["category"] == "regression"
        bad = PredictionTargetService().calculate_all_targets(_bars(0.0, 60), [_tb()], dataset_timeframe="1h")
        assert bad[0]["category"] == "error"


# ---------------------------------------------------------------------------------------------- no live snapshot as a feature
class TestLiveSnapshotIsNeverAFeature:
    def test_live_snapshot_columns_are_excluded_from_model_features(self):
        from app.services.job_handler import select_feature_columns
        cols = ["Date", "Open", "High", "Low", "Close", "Volume", "t_rsi", "f_earn_yield",
                "live_snapshot_trailing_pe", "live_snapshot_market_cap", "tb_atr_h5_r1"]
        assert select_feature_columns(cols, ["tb_atr_h5_r1"], "tb_atr_h5_r1") == ["t_rsi", "f_earn_yield"]
