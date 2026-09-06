"""Regressions for the four ML leakage defects found in the 2026-09-06 source audit.

Each test names the concrete way a model could score well while having learned
nothing, so a future refactor that reintroduces the shortcut fails here instead of
producing a plausible-looking F1 score.

  F1  the label itself was one of the input features
  F3  the normalization scaler was fitted on the held-out rows too
  F4  training labels at the split boundary were decided by validation-period bars
  F5  outcomes that had not happened yet were stored as negatives

These run WITHOUT tsai/darts installed. `TSAITrainingService.prepare_data*` only
uses pandas/numpy and `DataPreparationService`; the `TSAI_AVAILABLE` guard exists
because the *training* half of the class needs torch. Monkeypatching that one flag
is what keeps these regressions live in an environment where every real tsai test
skips -- which is exactly the environment the leaks survived in.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import tsai_training
from app.services.data_preparation import purged_train_row_count
from app.services.job_handler import (
    build_directional_target,
    drop_unobservable_target_rows,
    select_feature_columns,
)
from app.services.tsai_training import TSAITrainingService


FEATURE_COLUMNS = ['SMA_20', 'RSI_14', 'Returns']


def make_frame(n_rows: int = 200, seed: int = 7) -> pd.DataFrame:
    """A prepared training frame: OHLCV, causal indicators, and a directional label."""
    rng = np.random.RandomState(seed)
    close = 100 + rng.randn(n_rows).cumsum()
    df = pd.DataFrame({
        'Date': pd.date_range('2023-01-01', periods=n_rows, freq='h'),
        'Open': close + rng.randn(n_rows) * 0.5,
        'High': close + abs(rng.randn(n_rows)),
        'Low': close - abs(rng.randn(n_rows)),
        'Close': close,
        'Volume': (rng.rand(n_rows) * 1e6).astype(int),
        'SMA_20': close + rng.randn(n_rows) * 2,
        'RSI_14': 50 + rng.randn(n_rows) * 15,
        'Returns': pd.Series(close).pct_change().fillna(0.0).values,
    })
    return df


@pytest.fixture
def tsai_prep(monkeypatch):
    """A TSAITrainingService whose data-prep half is usable without the torch stack."""
    monkeypatch.setattr(tsai_training, 'TSAI_AVAILABLE', True)
    return TSAITrainingService(normalize=True, buffer_pct=0.35)


# ---------------------------------------------------------------------------
# F1 -- the classifier must not be handed the answer as an input column
# ---------------------------------------------------------------------------

class TestTargetsNeverBecomeFeatures:
    """Feature selection excluded only the legacy `price_*` targets.

    Every other generated family -- `direction_up_5bar`, `zigzag_bullish_reversal`,
    `triple_barrier_*`, `volatility_*` -- therefore stayed in the feature list,
    including the column being predicted. Since targets are pre-shifted at build
    time the caller passes prediction_horizon=0, so the final timestep of each input
    sequence carried the exact label. A saved job's metadata.json from before the fix
    shows it plainly: target `direction_up_1bar`, and `direction_up_1bar` sitting in
    its own 91-column feature list.
    """

    def test_the_predicted_column_is_not_a_feature(self):
        columns = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume',
                   'SMA_20', 'RSI_14', 'direction_up_5bar']
        features = select_feature_columns(
            columns, target_columns=['direction_up_5bar'], selected_target='direction_up_5bar'
        )
        assert 'direction_up_5bar' not in features

    def test_non_selected_targets_are_excluded_too(self):
        """A target the job did not select is just as future-derived and just as
        unavailable at inference; only the selected one used to be the concern."""
        columns = ['Date', 'Close', 'RSI_14',
                   'direction_up_5bar', 'direction_down_5bar', 'zigzag_bullish_reversal']
        targets = ['direction_up_5bar', 'direction_down_5bar', 'zigzag_bullish_reversal']
        features = select_feature_columns(columns, target_columns=targets,
                                          selected_target='direction_up_5bar')
        assert features == ['RSI_14']

    def test_every_generated_target_family_is_excluded(self):
        """One family per branch of the target builder. The old rule caught only the
        first of these, because it matched on the `price_` prefix alone."""
        targets = [
            'price_up_10pct_5dd_7b',        # price_based
            'direction_up_5bar',            # directional
            'zigzag_bullish_reversal',      # trend_reversal (no shared prefix)
            'triple_barrier_2p_1s_10b',     # triple_barrier
            'volatility_std_5b',            # volatility
        ]
        columns = ['Date', 'Close', 'ATR_14'] + targets
        features = select_feature_columns(columns, target_columns=targets,
                                          selected_target='direction_up_5bar')
        assert features == ['ATR_14']

    def test_legacy_price_targets_are_excluded_without_registration(self):
        """The legacy path builds `price_*` columns inside PredictionTargetService and
        does not register them, so the name-prefix rule still has to hold."""
        columns = ['Date', 'Close', 'MACD', 'price_up_10pct_5dd_7d', 'price_down_10pct_5dd_7d']
        features = select_feature_columns(columns, target_columns=[],
                                          selected_target='price_up_10pct_5dd_7d')
        assert features == ['MACD']

    def test_causal_indicators_are_kept(self):
        """The fix must not swing the other way: ordinary indicators are the features."""
        columns = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'ticker',
                   'SMA_20', 'RSI_14', 'MACD_signal', 'direction_up_5bar']
        features = select_feature_columns(columns, target_columns=['direction_up_5bar'],
                                          selected_target='direction_up_5bar')
        assert features == ['SMA_20', 'RSI_14', 'MACD_signal']

    def test_label_is_absent_from_the_last_timestep_of_every_sequence(self, tsai_prep):
        """The audit's reproduction, end to end.

        With the label in the feature list, `X[:, label_channel, -1] == y` held for
        every sample -- the model only had to copy one input channel. Normalization
        is off here so the sequences carry raw feature values and the comparison is
        the identity the probe checked; with it on, the leaked column is merely
        rescaled (0/1 -> 0.259/0.740), which is exactly as learnable and would slip
        past an equality check.
        """
        df = make_frame()
        df['direction_up_2bar'] = build_directional_target(df['Close'], 2, 'up')
        df, _ = drop_unobservable_target_rows(df, 'direction_up_2bar')

        features = select_feature_columns(
            list(df.columns),
            target_columns=['direction_up_2bar'],
            selected_target='direction_up_2bar',
        )
        raw = TSAITrainingService(normalize=False)
        X_train, X_test, y_train, y_test = raw.prepare_data_split(
            df, train_ratio=0.8, target_column='direction_up_2bar',
            feature_columns=features, seq_len=10,
            prediction_horizon=0, prediction_mode='shift', label_horizon=2,
        )

        for channel in range(X_train.shape[1]):
            assert not np.array_equal(X_train[:, channel, -1], y_train.astype(np.float32)), (
                f"feature channel {channel} ({features[channel]}) reproduces the label exactly"
            )

    def test_the_saved_inference_column_list_contains_no_target(self, tsai_prep):
        """`data_prep.get_valid_columns()` is stored with the model and is what a live
        prediction is expected to supply. A target column in there is unbuildable at
        inference time -- it is derived from a bar that has not printed yet."""
        df = make_frame()
        targets = ['direction_up_2bar', 'direction_down_2bar']
        df['direction_up_2bar'] = build_directional_target(df['Close'], 2, 'up')
        df['direction_down_2bar'] = build_directional_target(df['Close'], 2, 'down')
        df, _ = drop_unobservable_target_rows(df, 'direction_up_2bar')

        features = select_feature_columns(list(df.columns), target_columns=targets,
                                          selected_target='direction_up_2bar')
        tsai_prep.prepare_data_split(
            df, train_ratio=0.8, target_column='direction_up_2bar',
            feature_columns=features, seq_len=10,
            prediction_horizon=0, prediction_mode='shift', label_horizon=2,
        )
        saved = tsai_prep.data_prep.get_valid_columns()
        assert not (set(saved) & set(targets)), f"target columns saved as model inputs: {saved}"


# ---------------------------------------------------------------------------
# F3 -- the training transform must not see the held-out rows
# ---------------------------------------------------------------------------

class TestNormalizationIsFittedOnTrainingRowsOnly:
    """`prepare_data_split` fitted the scaler on the full frame before splitting,
    with a comment saying that kept test values inside the normalization range --
    while its own docstring promised a train-only fit. The buffer (35%) plus
    transform()'s clip is what provides that headroom; the test rows are not needed
    for it, and using them means the validation block is not out of sample and the
    training scale differs from the one a live model would have.
    """

    def test_a_test_only_extreme_does_not_move_the_training_arrays(self, tsai_prep, monkeypatch):
        df = make_frame()
        df['target'] = build_directional_target(df['Close'], 1, 'up')
        df, _ = drop_unobservable_target_rows(df, 'target')

        kwargs = dict(train_ratio=0.8, target_column='target',
                      feature_columns=FEATURE_COLUMNS, seq_len=10,
                      prediction_horizon=0, prediction_mode='shift', label_horizon=1)

        X_train_base, _, y_train_base, _ = tsai_prep.prepare_data_split(df.copy(), **kwargs)
        params_base = tsai_prep.get_normalization_params()

        # A spike that exists only in the held-out block. Under a full-frame fit this
        # rewrites buffered_max for RSI_14 and rescales every training row with it.
        spiked = df.copy()
        spiked.loc[spiked.index[-1], 'RSI_14'] = 10_000.0

        service2 = TSAITrainingService(normalize=True, buffer_pct=0.35)
        X_train_spiked, _, y_train_spiked, _ = service2.prepare_data_split(spiked, **kwargs)
        params_spiked = service2.get_normalization_params()

        np.testing.assert_array_equal(
            X_train_base, X_train_spiked,
            err_msg="a value that exists only in the test block changed the training inputs"
        )
        np.testing.assert_array_equal(y_train_base, y_train_spiked)
        assert params_base['columns']['RSI_14'] == params_spiked['columns']['RSI_14'], (
            "the scaler learned RSI_14's range from a held-out row"
        )

    def test_multi_dataset_fit_excludes_every_dataset_s_test_block(self, tsai_prep):
        """The multi-dataset path concatenated ALL rows of ALL datasets to fit. One
        symbol's future extreme then set the scale for every other symbol's training
        rows."""
        frames = []
        for seed in (1, 2):
            d = make_frame(n_rows=120, seed=seed)
            d['target'] = build_directional_target(d['Close'], 1, 'up')
            d, _ = drop_unobservable_target_rows(d, 'target')
            frames.append(d)

        kwargs = dict(train_ratio=0.8, target_column='target',
                      feature_columns=FEATURE_COLUMNS, seq_len=10, label_horizon=1)

        X_train_base, _, _, _ = tsai_prep.prepare_multi_dataset_split(
            [f.copy() for f in frames], **kwargs
        )

        spiked = [f.copy() for f in frames]
        spiked[1].loc[spiked[1].index[-1], 'SMA_20'] = 10_000.0

        service2 = TSAITrainingService(normalize=True, buffer_pct=0.35)
        X_train_spiked, _, _, _ = service2.prepare_multi_dataset_split(spiked, **kwargs)

        np.testing.assert_array_equal(
            X_train_base, X_train_spiked,
            err_msg="dataset 2's test-block extreme leaked into the shared training scale"
        )


# ---------------------------------------------------------------------------
# F4 -- labels that need a validation bar must not be trained on
# ---------------------------------------------------------------------------

class TestBoundaryLabelPurge:
    """Targets are computed over the whole frame and only then cut at split_idx.
    A 2-bar label on the last training row is decided by the second row of the
    validation block, so training on it feeds validation outcomes into the fit.
    Passing prediction_horizon=0 to the sequencer stops the double shift but leaves
    no horizon-based purge behind.
    """

    def test_keeps_all_rows_for_a_causal_label(self):
        # rsi/macd/sar reversals read no future bar at all -- horizon 0, nothing purged.
        assert purged_train_row_count(100, 0) == 100

    def test_drops_exactly_the_rows_whose_outcome_lands_past_the_boundary(self):
        # The audit's arithmetic: 12 rows, 80% split at row 9, a 2-bar label on
        # training row 8 reads row 10, which is validation data. Rows 0..6 survive.
        assert purged_train_row_count(9, 2) == 7

    def test_a_horizon_that_swallows_the_block_yields_zero_not_a_negative_slice(self):
        # `df.iloc[:-h]` with h > len silently returns an EMPTY frame; `df.iloc[:n-h]`
        # with a negative n-h silently returns rows from the wrong end. Neither may
        # happen quietly, so the count clamps at 0 and the caller refuses the job.
        assert purged_train_row_count(5, 9) == 0

    @pytest.mark.parametrize("horizon", [-1, 2.5, None, "3"])
    def test_an_unusable_horizon_is_refused_not_treated_as_zero(self, horizon):
        # Zero means "causal label, purge nothing". Coercing an unknown or malformed
        # horizon to it would restore the leak while every log line claimed a purge.
        with pytest.raises(ValueError):
            purged_train_row_count(100, horizon)

    def test_prepare_data_split_drops_the_boundary_windows(self, tsai_prep):
        df = make_frame(n_rows=100)
        df['target'] = build_directional_target(df['Close'], 3, 'up')
        df, _ = drop_unobservable_target_rows(df, 'target')

        kwargs = dict(train_ratio=0.8, target_column='target',
                      feature_columns=FEATURE_COLUMNS, seq_len=10,
                      prediction_horizon=0, prediction_mode='shift')

        X_leaky, _, _, _ = tsai_prep.prepare_data_split(df.copy(), label_horizon=0, **kwargs)
        X_purged, _, y_purged, _ = TSAITrainingService(
            normalize=True, buffer_pct=0.35
        ).prepare_data_split(df.copy(), label_horizon=3, **kwargs)

        assert X_purged.shape[0] == X_leaky.shape[0] - 3, (
            "one training window per look-ahead bar must be dropped at the boundary"
        )

        # The surviving last training label must be decided strictly before the split.
        split_idx = int(len(df) * 0.8)
        last_kept_row = split_idx - 3 - 1
        assert last_kept_row + 3 <= split_idx - 1
        assert y_purged[-1] == df['target'].iloc[last_kept_row]


# ---------------------------------------------------------------------------
# F5 -- an outcome that has not happened is not a negative
# ---------------------------------------------------------------------------

class TestUnobservableOutcomesAreNotNegatives:
    """`(Close.shift(-h) > Close).astype(int)` compared the shift's trailing NaN,
    got False, and cast it to 0. The last h rows -- where no future price exists --
    were stored as confirmed "did not go up", entering both the class balance and
    the held-out precision/recall as observed data.
    """

    def test_the_tail_is_missing_not_zero(self):
        close = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0])
        labels = build_directional_target(close, horizon=2, direction='up')

        # The audit's reproduction: the final two labels used to come back [0, 0]
        # although neither future price is in the data at all.
        assert labels.iloc[-2:].isna().all()
        # Everything with an observable outcome is still labelled, and correctly:
        # this series rises throughout, so every knowable label is 1.
        assert list(labels.iloc[:-2]) == [1.0, 1.0, 1.0, 1.0]

    def test_a_real_negative_is_still_a_zero(self):
        """Missing must mean unobservable only -- an observed non-move stays 0."""
        close = pd.Series([10.0, 9.0, 8.0, 7.0])
        labels = build_directional_target(close, horizon=1, direction='up')
        assert list(labels.iloc[:-1]) == [0.0, 0.0, 0.0]
        assert pd.isna(labels.iloc[-1])

    def test_down_direction_marks_the_same_tail_unknown(self):
        close = pd.Series([10.0, 9.0, 8.0, 7.0, 6.0])
        labels = build_directional_target(close, horizon=2, direction='down')
        assert list(labels.iloc[:-2]) == [1.0, 1.0, 1.0]
        assert labels.iloc[-2:].isna().all()

    def test_unlabelled_rows_are_removed_before_the_split(self):
        df = make_frame(n_rows=50)
        df['target'] = build_directional_target(df['Close'], 4, 'up')
        cleaned, n_dropped = drop_unobservable_target_rows(df, 'target')

        assert n_dropped == 4
        assert len(cleaned) == 46
        assert not cleaned['target'].isna().any()
        # Contiguous index afterwards, or the positional split cuts the wrong rows.
        assert list(cleaned.index) == list(range(46))

    def test_a_fully_observable_target_is_left_alone(self):
        df = make_frame(n_rows=20)
        df['target'] = 1.0
        cleaned, n_dropped = drop_unobservable_target_rows(df, 'target')
        assert n_dropped == 0
        assert len(cleaned) == 20

    def test_nan_labels_never_reach_the_int_cast(self, tsai_prep):
        """`prepare_data` does `df[target].values.astype(np.int64)`. A NaN there does
        not raise -- it becomes -9223372036854775808 and trains as a class id. This
        is why the unlabelled rows have to be dropped rather than merely left NaN.
        """
        df = make_frame(n_rows=80)
        df['target'] = build_directional_target(df['Close'], 3, 'up')
        cleaned, _ = drop_unobservable_target_rows(df, 'target')

        _, _, y_train, y_test = tsai_prep.prepare_data_split(
            cleaned, train_ratio=0.8, target_column='target',
            feature_columns=FEATURE_COLUMNS, seq_len=10,
            prediction_horizon=0, prediction_mode='shift', label_horizon=3,
        )
        assert set(np.unique(y_train)) <= {0, 1}
        assert set(np.unique(y_test)) <= {0, 1}
