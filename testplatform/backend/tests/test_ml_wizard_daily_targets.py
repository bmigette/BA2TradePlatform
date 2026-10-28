"""Daily wizard targets (Phase 7 / Task 7.1): rel_return, quintile, beats (cross-sectional, computed across the job's
datasets, per session), up, volatility and multistep (per symbol), on top of ml_targets.daily_targets / fit_daily_target."""
import numpy as np
import pandas as pd
import pytest

from app.services import ml_targets as T
from app.services import ml_wizard_targets as W

N = 160
SESSIONS = pd.bdate_range("2023-01-02", periods=260)
TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]


def daily_frame(seed, drift=0.0004, noise=0.012, n=N, filled_at=(), start=0):
    """A daily wizard dataset as the job handler loads it from the CSV: Date as text, bars, ``filled``, one feature."""
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(drift + rng.normal(0, noise, n)))
    open_ = np.r_[100.0, close[:-1]] * np.exp(rng.normal(0, 0.003, n))
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
    df = pd.DataFrame({"Date": SESSIONS[start:start + n].strftime("%Y-%m-%d"), "Open": open_, "High": hi, "Low": lo,
                       "Close": close, "Volume": 1e6, "filled": False, "t_a": rng.normal(size=n)})
    for i in filled_at:
        df.loc[i, "filled"] = True
    return df


def job_frames(n_tickers=6, **kw):
    return [daily_frame(10 + i, **kw) for i in range(n_tickers)]


def pt(type_, **kw):
    return {"type": type_, **kw}


def cross(frames, targets, tickers=None, timeframe="1d", provider=True):
    tickers = tickers or TICKERS[:len(frames)]
    return W.build_cross_sectional_daily_targets(
        frames, tickers, targets, [timeframe] * len(frames), [provider] * len(frames))


# ---------------------------------------------------------------------------------------------- naming and config
class TestNaming:
    @pytest.mark.parametrize("config, col", [
        (pt("daily_rel_return"), "rel_ret_h5"),
        (pt("daily_quintile", horizonSessions=10), "quintile_h10"),
        (pt("daily_beats", horizonSessions=1), "beats_h1"),
        (pt("daily_up", horizonSessions=3), "up_h3"),
        (pt("daily_volatility", horizonSessions=20), "realvol_h20"),
        (pt("daily_multistep", horizons=[20, 5, 1]), "up_h1"),
    ])
    def test_column_names(self, config, col):
        assert W.column_name(config) == col

    def test_multistep_sorts_its_horizons_so_the_shortest_is_trained(self):
        assert W.column_names(pt("daily_multistep", horizons=[5, 1, 20])) == ["up_h1", "up_h5", "up_h20"]
        assert W.daily_horizons(pt("daily_multistep", horizons=[20, 1])) == [1, 20]
        assert W.column_name(pt("daily_multistep", horizons=[20, 1])) == "up_h1"

    @pytest.mark.parametrize("config, text", [
        (pt("daily_up", horizonSessions=4), "horizon"),
        (pt("daily_up", horizonSessions=0), "horizon"),
        (pt("daily_up", horizonSessions=2.5), "horizon"),
        (pt("daily_multistep"), "horizons"),
        (pt("daily_multistep", horizons=[]), "horizons"),
        (pt("daily_multistep", horizons=[5, 5]), "distinct"),
        (pt("daily_multistep", horizons=[5, 7]), "horizon"),
        (pt("daily_volatility", horizonSessions=1), "at least 2"),
    ])
    def test_invalid_configs_are_refused(self, config, text):
        with pytest.raises(W.WizardTargetError, match=text):
            W.column_names(config)

    def test_the_horizon_choices_are_the_exploration_s(self):
        assert W.DAILY_HORIZON_CHOICES == T.DAILY_HORIZONS == (1, 3, 5, 10, 20)

    def test_type_registries(self):
        assert set(W.CROSS_SECTIONAL_DAILY_TYPES) == {"daily_rel_return", "daily_quintile", "daily_beats"}
        assert set(W.DAILY_TARGET_TYPES) == set(W.CROSS_SECTIONAL_DAILY_TYPES) | {
            "daily_up", "daily_volatility", "daily_multistep"}
        assert set(W.ALL_WIZARD_TARGET_TYPES) == set(W.WIZARD_TARGET_TYPES) | set(W.DAILY_TARGET_TYPES)


# ---------------------------------------------------------------------------------------------- per symbol
class TestPerSymbolTargets:
    def build(self, frame, config, ratio=0.7, timeframe="1d", provider=True):
        return W.build_daily_target(frame.copy(), config, ratio, timeframe, provider)

    def test_up_is_the_entry_open_to_exit_close_direction_and_registers_the_horizon(self):
        f = daily_frame(1, drift=0.002)
        (series, col, look), = self.build(f, pt("daily_up", horizonSessions=3))
        assert col == "up_h3" and look == 3 and series.index.equals(f.index)
        ret = f["Close"].shift(-3) / f["Open"].shift(-1) - 1
        expected = (ret > 0).astype(float).where(ret.notna())
        np.testing.assert_array_equal(series.to_numpy(), expected.to_numpy())
        assert series.iloc[-3:].isna().all()                  # the unobservable tail is NaN, never a fabricated 0
        assert set(series.dropna().unique()) <= {0.0, 1.0}

    def test_a_filled_entry_or_exit_bar_has_no_label(self):
        f = daily_frame(2, filled_at=(40,))
        (series, _, _), = self.build(f, pt("daily_up", horizonSessions=3))
        # decisions whose entry (S+1) or exit (S+3) is the fabricated bar 40: S = 39 and S = 37
        assert np.isnan(series.iloc[39]) and np.isnan(series.iloc[37]) and not np.isnan(series.iloc[36])

    def test_up_refuses_a_train_block_with_a_single_class(self):
        falling = daily_frame(3, drift=-0.02, noise=0.001)
        with pytest.raises(W.WizardTargetError, match="single class"):
            self.build(falling, pt("daily_up", horizonSessions=3))

    def test_volatility_is_the_std_of_the_daily_log_returns_of_the_next_sessions(self):
        f = daily_frame(4, noise=0.02)
        (series, col, look), = self.build(f, pt("daily_volatility", horizonSessions=5))
        assert col == "realvol_h5" and look == 5
        lr = np.log(f["Close"]).diff()
        s = 50
        assert series.iloc[s] == pytest.approx(lr.iloc[s + 1:s + 6].std(ddof=1))
        assert series.iloc[-5:].isna().all() and series.dropna().min() > 0

    def test_multistep_makes_a_column_per_horizon_all_purged_by_the_longest(self):
        f = daily_frame(5)
        out = self.build(f, pt("daily_multistep", horizons=[1, 5, 20]))
        assert [c for _, c, _ in out] == ["up_h1", "up_h5", "up_h20"]
        assert [look for _, _, look in out] == [20, 20, 20]
        assert out[0][0].notna().sum() == N - 1 and out[2][0].notna().sum() == N - 20
        ret1 = f["Close"].shift(-1) / f["Open"].shift(-1) - 1
        np.testing.assert_array_equal(out[0][0].dropna().to_numpy(), (ret1 > 0).astype(float).where(ret1.notna()).dropna().to_numpy())

    def test_up_and_multistep_agree(self):
        f = daily_frame(6)
        (a, _, _), = self.build(f, pt("daily_up", horizonSessions=5))
        out = self.build(f, pt("daily_multistep", horizons=[5, 10]))
        pd.testing.assert_series_equal(a, out[0][0])

    def test_labels_are_fitted_on_train_rows_only(self, monkeypatch):
        seen = {}
        real = T.fit_daily_target

        def spy(df, spec, train_mask, excursions=None):
            seen.update(n=len(df), mask=np.asarray(train_mask).copy(), kind=spec.kind, h=spec.horizon)
            return real(df, spec, train_mask, excursions)
        monkeypatch.setattr(T, "fit_daily_target", spy)
        self.build(daily_frame(7), pt("daily_up", horizonSessions=5), ratio=0.7)
        mask = seen["mask"]
        assert seen["kind"] == "binary_direction" and seen["h"] == 5 and seen["n"] == N
        labelled = N - 5
        assert mask.sum() == int(labelled * 0.7) - 5          # the train block of the split, minus the purge
        assert mask[:mask.sum()].all() and not mask[mask.sum():].any()

    @pytest.mark.parametrize("kwargs", [dict(timeframe="1h"), dict(provider=False)])
    def test_refused_off_the_daily_source(self, kwargs):
        with pytest.raises(W.WizardTargetError, match="daily point-in-time feature store"):
            self.build(daily_frame(8), pt("daily_up"), **kwargs)

    def test_refuses_a_dataset_without_the_filled_flag_or_with_unordered_sessions(self):
        f = daily_frame(9).drop(columns=["filled"])
        with pytest.raises(W.WizardTargetError, match="filled"):
            self.build(f, pt("daily_up"))
        g = daily_frame(9).iloc[::-1].reset_index(drop=True)
        with pytest.raises(W.WizardTargetError, match="increasing"):
            self.build(g, pt("daily_up"))

    def test_cross_sectional_types_do_not_build_on_one_frame(self):
        with pytest.raises(W.WizardTargetError, match="across the job's tickers"):
            self.build(daily_frame(10), pt("daily_rel_return"))


# ---------------------------------------------------------------------------------------------- cross-sectional
class TestCrossSectionalTargets:
    def test_rel_return_is_the_return_minus_the_session_mean_of_the_job(self):
        frames = job_frames(6)
        out = cross(frames, [pt("daily_rel_return", horizonSessions=3)])
        assert all(set(o) == {"rel_ret_h3"} for o in out)
        rets = pd.DataFrame({t: (f["Close"].shift(-3) / f["Open"].shift(-1) - 1) for t, f in zip(TICKERS, frames)})
        s = 80                                              # a session far from both ends (the ATR warm-up is 14)
        expected = rets.iloc[s] - rets.iloc[s].mean()
        for t, o in zip(TICKERS, out):
            series, look = o["rel_ret_h3"]
            assert look == 3 and series.index.equals(frames[0].index)
            assert series.iloc[s] == pytest.approx(expected[t])
        assert abs(sum(o["rel_ret_h3"][0].iloc[s] for o in out)) < 1e-12        # a session's relative returns sum to 0

    def test_quintile_marks_the_top_and_bottom_fifth_of_each_session(self):
        frames = job_frames(10)
        tickers = [f"T{i}" for i in range(10)]
        out = cross(frames, [pt("daily_quintile", horizonSessions=5)], tickers)
        s = 90
        vals = np.array([o["quintile_h5"][0].iloc[s] for o in out])
        rets = np.array([(f["Close"].iloc[s + 5] / f["Open"].iloc[s + 1] - 1) for f in frames])
        order = np.argsort(rets)
        assert (vals[order[:2]] == 0).all() and (vals[order[-2:]] == 1).all()
        assert np.isnan(vals[order[2:-2]]).all()            # the middle three fifths carry no label

    def test_beats_is_the_sign_of_the_relative_return(self):
        frames = job_frames(6)
        out = cross(frames, [pt("daily_beats", horizonSessions=5)])
        s = 70
        vals = np.array([o["beats_h5"][0].iloc[s] for o in out])
        rets = np.array([(f["Close"].iloc[s + 5] / f["Open"].iloc[s + 1] - 1) for f in frames])
        np.testing.assert_array_equal(vals, (rets > rets.mean()).astype(float))

    def test_each_target_registers_its_horizon(self):
        out = cross(job_frames(6), [pt("daily_rel_return", horizonSessions=10), pt("daily_beats", horizonSessions=1)])
        assert out[0]["rel_ret_h10"][1] == 10 and out[0]["beats_h1"][1] == 1

    def test_no_label_where_the_entry_bar_is_a_fill(self):
        frames = job_frames(6)
        frames[2] = daily_frame(12, filled_at=(60,))
        out = cross(frames, [pt("daily_rel_return", horizonSessions=3)])
        assert np.isnan(out[2]["rel_ret_h3"][0].iloc[59])
        assert not np.isnan(out[0]["rel_ret_h3"][0].iloc[59])       # the others still have a label there

    def test_tickers_that_start_later_are_aligned_on_the_session(self):
        frames = job_frames(5)
        late = daily_frame(13, n=100, start=60)
        frames.append(late)
        out = cross(frames, [pt("daily_rel_return", horizonSessions=3)])
        series = out[5]["rel_ret_h3"][0]
        assert len(series) == 100
        # on the late ticker's own 30th row the cross-section holds all six
        rets = [(f["Close"].shift(-3) / f["Open"].shift(-1) - 1) for f in frames[:5]]
        day = 60 + 30
        vals = [r.iloc[day] for r in rets] + [late["Close"].iloc[30 + 3] / late["Open"].iloc[31] - 1]
        assert series.iloc[30] == pytest.approx(vals[-1] - np.mean(vals))

    def test_no_cross_sectional_target_requested_computes_nothing(self):
        out = cross(job_frames(2), [pt("daily_up")])
        assert out == [{}, {}]

    @pytest.mark.parametrize("n", [1, 2, 4])
    def test_refuses_a_job_with_too_few_tickers(self, n):
        for typ in W.CROSS_SECTIONAL_DAILY_TYPES:
            with pytest.raises(W.WizardTargetError, match=r"at least 5 .*got %d" % n):
                cross(job_frames(n), [pt(typ)])

    def test_a_single_ticker_is_named(self):
        with pytest.raises(W.WizardTargetError, match="single ticker|got 1"):
            cross(job_frames(1), [pt("daily_rel_return")])

    def test_refuses_duplicate_tickers_and_other_sources(self):
        with pytest.raises(W.WizardTargetError, match="duplicate"):
            cross(job_frames(6), [pt("daily_beats")], tickers=["A", "A", "C", "D", "E", "F"])
        with pytest.raises(W.WizardTargetError, match="daily point-in-time feature store"):
            cross(job_frames(6), [pt("daily_beats")], timeframe="1h")
        with pytest.raises(W.WizardTargetError, match="daily point-in-time feature store"):
            cross(job_frames(6), [pt("daily_beats")], provider=False)

    def test_refuses_when_no_session_has_enough_tickers(self):
        # six tickers that never overlap enough: the sessions are shared by at most 3
        frames = [daily_frame(20 + i, n=40, start=i * 30) for i in range(6)]
        with pytest.raises(W.WizardTargetError, match="no session"):
            cross(frames, [pt("daily_rel_return", horizonSessions=1)])


# ---------------------------------------------------------------------------------------------- the job handler
class TestJobIntegration:
    def _job(self, frames, targets, monkeypatch, ratio=70, timeframe="1d", provider="alpaca_hist"):
        from app.services import job_handler
        captured = {}
        monkeypatch.setattr(job_handler, "ML_AVAILABLE", True)
        monkeypatch.setattr(job_handler, "load_dataset", lambda i, start_date=None, end_date=None: frames[i].copy())
        monkeypatch.setattr(job_handler, "get_dataset_info", lambda i: {
            "ticker": TICKERS[i], "timeframe": timeframe, "data_provider": provider})
        monkeypatch.setattr(job_handler, "update_job_progress", lambda *a, **k: None)
        monkeypatch.setattr(job_handler, "save_training_datasets", lambda **kw: captured.update(
            combined=kw["combined_df"], train=kw["train_df"], features=kw["feature_columns"],
            target=kw["target_column"]))
        payload = {
            "dataset_ids": list(range(len(frames))), "job_type": "classification", "selected_models": ["lstm"],
            "train_test_split": ratio, "prediction_horizon": 1, "prediction_modes": ["shift"],
            "genetic_config": {"populationSize": 2}, "metrics_config": {"classificationMetric": "f1_score"},
            "prediction_targets": targets}
        return job_handler.handle_training_job("t-daily", payload, dry_run=True), captured

    def test_rel_return_job_labels_across_the_tickers_and_keeps_targets_out_of_the_features(self, monkeypatch):
        from ba2_providers.macro import fred_series as fs

        def boom(*a, **k):
            raise AssertionError("FRED was read by a target build")
        monkeypatch.setattr(fs, "get_series_as_of", boom)
        result, cap = self._job(job_frames(6), [pt("daily_rel_return", horizonSessions=5)], monkeypatch)
        assert result["status"] == "dry_run_success", result
        assert cap["target"] == "rel_ret_h5"
        comb = cap["combined"]
        assert set(comb["ticker"]) == set(TICKERS)
        assert comb["rel_ret_h5"].notna().all()                          # unobservable rows were dropped
        assert cap["features"] == ["t_a"]                                # filled, the bars and every target are not features
        per_session = comb.groupby("Date")["rel_ret_h5"].agg(["sum", "count"])
        assert (per_session.loc[per_session["count"] == 6, "sum"].abs() < 1e-9).all()

    def test_a_single_dataset_job_with_a_cross_sectional_target_fails_clearly(self, monkeypatch):
        result, _ = self._job(job_frames(1), [pt("daily_beats")], monkeypatch)
        assert result["status"] == "failed" and "at least 5" in result["error"] and "got 1" in result["error"]

    def test_per_symbol_targets_work_on_one_dataset_and_purge_by_the_horizon(self, monkeypatch):
        result, cap = self._job(job_frames(1, drift=0.0005), [pt("daily_multistep", horizons=[3, 10])], monkeypatch)
        assert result["status"] == "dry_run_success", result
        assert cap["target"] == "up_h3"
        assert {"up_h3", "up_h10"}.isdisjoint(cap["features"])           # the other horizon is a target too, not a feature
        assert cap["features"] == ["t_a"]
        tr = cap["train"]
        assert len(tr) <= int(0.7 * (N - 3)) - 10                        # purged by the longest horizon, not by 3
        assert len(tr) < int(0.7 * (N - 3)) - 3

    def test_multistep_horizons_listed_longest_first_still_train_the_shortest(self, monkeypatch):
        result, cap = self._job(job_frames(1), [pt("daily_multistep", horizons=[20, 1])], monkeypatch)
        assert result["status"] == "dry_run_success", result
        assert cap["target"] == "up_h1"

    def test_the_old_wizard_targets_are_still_refused_on_the_daily_source(self, monkeypatch):
        result, _ = self._job(job_frames(2), [{"type": "triple_barrier_atr"}], monkeypatch)
        assert result["status"] == "failed" and "Alpaca history cache" in result["error"]

    def test_daily_targets_are_refused_on_other_sources(self, monkeypatch):
        result, _ = self._job(job_frames(6), [pt("daily_beats")], monkeypatch, provider="yfinance")
        assert result["status"] == "failed" and "daily point-in-time feature store" in result["error"]

    def test_a_cross_sectional_target_with_a_per_symbol_one_trains_on_the_first_and_registers_both(self, monkeypatch):
        result, cap = self._job(job_frames(6), [pt("daily_beats", horizonSessions=3), pt("daily_up", horizonSessions=3)],
                                monkeypatch)
        assert result["status"] == "dry_run_success", result
        assert cap["target"] == "beats_h3" and "up_h3" in cap["combined"].columns
        assert cap["features"] == ["t_a"]


# ---------------------------------------------------------------------------------------------- preview and model listing
class TestPreviewAndListing:
    def test_preview_service_computes_the_per_symbol_types(self):
        from app.services.darts_models import PredictionTargetService
        res = PredictionTargetService().calculate_all_targets(
            daily_frame(1), [pt("daily_up", horizonSessions=3), pt("daily_volatility", horizonSessions=5)],
            dataset_timeframe="1d", panel_source=True)
        assert [r["columnName"] for r in res] == ["up_h3", "realvol_h5"]
        assert res[0]["category"] == "binary_classification" and res[1]["category"] == "regression"
        assert res[0]["stats"]["positiveCount"] > 0

    def test_preview_explains_why_a_cross_sectional_target_cannot_be_shown(self):
        from app.services.darts_models import PredictionTargetService
        res = PredictionTargetService().calculate_all_targets(
            daily_frame(1), [pt("daily_rel_return")], dataset_timeframe="1d", panel_source=True)
        assert res[0]["category"] == "error" and "across the job's tickers" in res[0]["stats"]["error"]

    def test_the_model_target_listing_knows_the_daily_columns(self):
        from app.api import models
        assert models.ALL_WIZARD_TARGET_TYPES == W.ALL_WIZARD_TARGET_TYPES
