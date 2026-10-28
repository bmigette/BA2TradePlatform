"""Cross-sectional daily targets compare the job's tickers on each session, so the train / validation / holdout cut must be
ONE set of boundary dates for all the tickers (a per-ticker row-count cut puts a ticker's train rows on dates that are
another ticker's test dates, and the relative return of those rows is decided by the test period)."""
import numpy as np
import pandas as pd
import pytest

from app.services.data_preparation import common_session_bounds, frame_bounds, three_way_bounds
from tests.test_ml_wizard_daily_targets import TICKERS, daily_frame, pt

RATIO, HOLD = 0.7, 0.5


def uneven_frames():
    """Six tickers: five full 160-session histories and one that lists 60 sessions later."""
    frames = [daily_frame(10 + i) for i in range(5)]
    frames.append(daily_frame(99, n=100, start=60))
    return frames


class TestFrameBounds:
    def test_row_count_bounds_are_the_per_ticker_ones_by_default(self):
        frames = uneven_frames()
        got = frame_bounds(frames, RATIO, HOLD, 5)
        assert got == [three_way_bounds(len(f), RATIO, HOLD, 5) for f in frames]

    def test_common_dates_cut_every_ticker_at_the_same_dates(self):
        frames = uneven_frames()
        bounds = frame_bounds(frames, RATIO, HOLD, 5, common_dates=True)
        sessions = pd.DatetimeIndex(sorted(set().union(*(pd.to_datetime(f["Date"]) for f in frames))))
        cb = three_way_bounds(len(sessions), RATIO, HOLD, 5)
        train_last, val_first = sessions[cb["train_end"] - 1], sessions[cb["val_start"]]
        val_last, hold_first = sessions[cb["val_end"] - 1], sessions[cb["holdout_start"]]
        for f, b in zip(frames, bounds):
            d = pd.to_datetime(f["Date"])
            assert d.iloc[b["train_end"] - 1] <= train_last and (b["train_end"] == len(f) or d.iloc[b["train_end"]] > train_last)
            assert d.iloc[b["val_start"]] >= val_first and d.iloc[b["val_start"] - 1] < val_first
            assert d.iloc[b["val_end"] - 1] <= val_last
            assert d.iloc[b["holdout_start"]] >= hold_first and d.iloc[b["holdout_start"] - 1] < hold_first

    def test_no_ticker_trains_on_a_date_another_ticker_is_tested_on(self):
        frames = uneven_frames()
        bounds = frame_bounds(frames, RATIO, HOLD, 5, common_dates=True)
        dates = [pd.to_datetime(f["Date"]) for f in frames]
        last_train = max(d.iloc[b["train_end"] - 1] for d, b in zip(dates, bounds))
        first_val = min(d.iloc[b["val_start"]] for d, b in zip(dates, bounds))
        last_val = max(d.iloc[b["val_end"] - 1] for d, b in zip(dates, bounds))
        first_hold = min(d.iloc[b["holdout_start"]] for d, b in zip(dates, bounds))
        assert last_train < first_val and last_val < first_hold
        # the same cut by row counts overlaps (the late ticker's rows are fewer, so its boundaries fall on later dates)
        rows = [three_way_bounds(len(f), RATIO, HOLD, 5) for f in frames]
        row_last_train = max(d.iloc[b["train_end"] - 1] for d, b in zip(dates, rows))
        row_first_val = min(d.iloc[b["val_start"]] for d, b in zip(dates, rows))
        assert row_last_train >= row_first_val

    def test_the_purge_is_the_label_horizon_in_sessions(self):
        frames = [daily_frame(10 + i) for i in range(6)]
        cb = common_session_bounds(frames, RATIO, HOLD, 7)
        assert cb["val_start"] - cb["train_end"] == 7
        assert cb["holdout_start"] - cb["val_end"] == 7

    def test_a_ticker_with_no_rows_in_a_block_is_refused(self):
        frames = [daily_frame(10 + i) for i in range(5)]
        frames.append(daily_frame(99, n=20, start=140))             # lists in the holdout period only
        with pytest.raises(ValueError, match=r"frame 5.*train"):
            frame_bounds(frames, RATIO, HOLD, 5, common_dates=True)

    def test_unsorted_dates_are_refused(self):
        frames = [daily_frame(10 + i) for i in range(5)]
        frames[0] = frames[0].iloc[::-1].reset_index(drop=True)
        with pytest.raises(ValueError, match="ascending"):
            frame_bounds(frames, RATIO, HOLD, 5, common_dates=True)


class TestJobSplitsOnCommonDates:
    def _job(self, frames, targets, monkeypatch):
        from app.services import job_handler
        cap = {}
        monkeypatch.setattr(job_handler, "ML_AVAILABLE", True)
        monkeypatch.setattr(job_handler, "load_dataset", lambda i, start_date=None, end_date=None: frames[i].copy())
        monkeypatch.setattr(job_handler, "get_dataset_info", lambda i: {
            "ticker": TICKERS[i], "timeframe": "1d", "data_provider": "alpaca_hist"})
        monkeypatch.setattr(job_handler, "update_job_progress", lambda *a, **k: None)
        monkeypatch.setattr(job_handler, "save_training_datasets", lambda **kw: cap.update(
            train=kw["train_df"], test=kw["test_df"]))
        payload = {"dataset_ids": list(range(len(frames))), "job_type": "classification", "selected_models": ["lstm"],
                   "train_test_split": 70, "prediction_horizon": 1, "prediction_modes": ["shift"],
                   "genetic_config": {"populationSize": 2}, "metrics_config": {"classificationMetric": "f1_score"},
                   "prediction_targets": targets}
        result = job_handler.handle_training_job("t-split", payload, dry_run=True)
        assert result["status"] == "dry_run_success", result
        return cap

    def test_a_cross_sectional_target_never_trains_on_test_dates(self, monkeypatch):
        cap = self._job(uneven_frames(), [pt("daily_rel_return", horizonSessions=5)], monkeypatch)
        assert pd.to_datetime(cap["train"]["Date"]).max() < pd.to_datetime(cap["test"]["Date"]).min()

    def test_a_per_symbol_target_keeps_the_per_ticker_row_split(self, monkeypatch):
        cap = self._job(uneven_frames(), [pt("daily_up", horizonSessions=5)], monkeypatch)
        # each ticker is cut on its own rows: the late ticker's train rows run past the others' first test date
        assert pd.to_datetime(cap["train"]["Date"]).max() >= pd.to_datetime(cap["test"]["Date"]).min()


class TestDartsPreparesTheCommonCut:
    def test_prepare_split_three_way_cuts_the_series_at_common_dates(self):
        from app.services.darts_training import DartsTrainingService
        frames = []
        for i in range(3):
            n, start = (200, 0) if i < 2 else (120, 80)
            d = pd.DataFrame({"Date": pd.date_range("2020-01-01", periods=300, freq="D")[start:start + n],
                              "y": np.random.default_rng(i).integers(0, 2, n).astype(float), "t_a": np.arange(n, dtype=float)})
            frames.append(d)
        svc = DartsTrainingService()
        out = svc.prepare_split_three_way(frames, RATIO, HOLD, "y", ["t_a"], timeframe="1d", label_horizon=5,
                                          common_dates=True)
        train_end = max(s.end_time() for s in out["train_series"])
        val_start = min(s.start_time() for s in out["val_series"])
        val_end = max(s.end_time() for s in out["val_series"])
        hold_start = min(s.start_time() for s in out["holdout_series"])
        assert train_end < val_start and val_end < hold_start
        row_wise = svc.prepare_split_three_way(frames, RATIO, HOLD, "y", ["t_a"], timeframe="1d", label_horizon=5)
        assert max(s.end_time() for s in row_wise["train_series"]) >= min(s.start_time() for s in row_wise["val_series"])


class TestTsaiPreparesTheCommonCut:
    def test_prepare_split_three_way_passes_the_common_blocks(self, monkeypatch):
        from app.services import tsai_training as TT
        monkeypatch.setattr(TT, "TSAI_AVAILABLE", True)
        svc = TT.TSAITrainingService(normalize=False)
        seen = {}

        def fake_prepare(df, *a, **k):
            seen.setdefault("blocks", []).append((df["Date"].min(), df["Date"].max(), len(df)))
            return np.zeros((len(df), 1, 1)), np.zeros(len(df))
        monkeypatch.setattr(svc, "prepare_data", fake_prepare)
        frames = uneven_frames()
        for f in frames:
            f["Date"] = pd.to_datetime(f["Date"])
        svc.prepare_split_three_way(frames, RATIO, HOLD, "y", ["t_a"], label_horizon=5, common_dates=True)
        n = len(frames)
        train, val, hold = seen["blocks"][:n], seen["blocks"][n:2 * n], seen["blocks"][2 * n:]
        assert max(b[1] for b in train) < min(b[0] for b in val) and max(b[1] for b in val) < min(b[0] for b in hold)
