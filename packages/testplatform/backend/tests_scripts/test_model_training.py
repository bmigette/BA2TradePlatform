#!/usr/bin/env python3
"""
Test script to debug F1=0 issue with model training.

Tests LSTM and NBEATS models with ZigZag prediction targets.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from datetime import datetime

# Configuration
DATASET_PATH = "datasets/AAPL_1h_20260128_204556.csv"
ZIGZAG_DEVIATION = 2.0  # 2% - lower to get more signals
PREDICTION_HORIZON = 3
TRAIN_RATIO = 0.5  # 50/50 split to have more test data
EPOCHS = 30  # More epochs to learn better discrimination
INPUT_CHUNK_LENGTH = 24

print("=" * 80)
print("MODEL TRAINING TEST SCRIPT")
print("=" * 80)

# 1. Load dataset
print("\n[1] Loading dataset...")
df = pd.read_csv(DATASET_PATH)
print(f"    Loaded {len(df)} rows")
print(f"    Date range: {df['Date'].iloc[0]} to {df['Date'].iloc[-1]}")

# 2. Calculate prediction targets using calculate_all_targets
print(f"\n[2] Calculating ZigZag trend reversal targets (deviation={ZIGZAG_DEVIATION}%)...")
from app.services.darts_models import PredictionTargetService
target_service = PredictionTargetService()

# Calculate bullish and bearish zigzag targets
targets_config = [
    {
        'type': 'trend_reversal',
        'indicator': 'zigzag',
        'indicatorParams': {'deviationPct': ZIGZAG_DEVIATION},
        'threshold': 0,
        'direction': 'bullish'
    },
    {
        'type': 'trend_reversal',
        'indicator': 'zigzag',
        'indicatorParams': {'deviationPct': ZIGZAG_DEVIATION},
        'threshold': 0,
        'direction': 'bearish'
    }
]

# Use calculate_all_targets which handles all target types
target_results = target_service.calculate_all_targets(df.copy(), targets_config)

print(f"    Calculated {len(target_results)} targets:")
for result in target_results:
    col_name = result.get('columnName', 'unknown')  # Note: camelCase
    stats = result.get('stats', {})
    print(f"    - {col_name}: positives={stats.get('positiveCount', 0)}, "
          f"total={stats.get('validRows', 0)}, "
          f"positive_pct={stats.get('positivePct', 0):.2f}%")

# Add target columns to DataFrame
df_with_targets = df.copy()
target_cols = []
for result in target_results:
    col_name = result.get('columnName')
    data = result.get('data', [])  # This is a list of {date, value} dicts
    if col_name and len(data) == len(df_with_targets):
        # Extract values from the data list
        values = [d.get('value', 0) if d.get('value') is not None else 0 for d in data]
        df_with_targets[col_name] = values
        target_cols.append(col_name)
        print(f"    Added column: {col_name}")

print(f"    Target columns: {target_cols}")

# Show where targets are positive
for col in target_cols:
    pos_indices = df_with_targets[df_with_targets[col] == 1].index.tolist()
    print(f"    {col} positive at {len(pos_indices)} indices, first 10: {pos_indices[:10]}")

# 3. Prepare features
print("\n[3] Preparing features...")
from app.services.darts_models import DatasetSplitter
from app.services.indicators import IndicatorService

indicator_service = IndicatorService()

# Add some additional features
df_with_targets['returns'] = df_with_targets['Close'].pct_change()
df_with_targets['log_returns'] = np.log(df_with_targets['Close'] / df_with_targets['Close'].shift(1))
df_with_targets['volatility'] = df_with_targets['returns'].rolling(10).std()

# Drop NaN rows
df_clean = df_with_targets.dropna()
print(f"    After dropping NaN: {len(df_clean)} rows (dropped {len(df_with_targets) - len(df_clean)})")

# Re-check target distribution after dropping NaN
for col in target_cols:
    positives = (df_clean[col] == 1).sum()
    print(f"    {col} after cleanup: {positives} positives ({100*positives/len(df_clean):.2f}%)")

# Feature columns - use existing features in dataset
exclude_cols = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'ticker'] + target_cols
# Also exclude date columns
date_cols = [c for c in df_clean.columns if 'date' in c.lower()]
exclude_cols.extend(date_cols)
feature_cols = [c for c in df_clean.columns if c not in exclude_cols and not c.startswith('price_') and not c.startswith('reversal_')]
print(f"    Feature columns ({len(feature_cols)}): {feature_cols[:15]}...")

# 4. Train/test split
print("\n[4] Splitting data...")
train_df, test_df = DatasetSplitter.train_test_split(df_clean, train_ratio=TRAIN_RATIO)
print(f"    Train: {len(train_df)} rows, Test: {len(test_df)} rows")

# Check target distribution in train/test
for col in target_cols:
    train_pos = (train_df[col] == 1).sum()
    test_pos = (test_df[col] == 1).sum()
    print(f"    {col}: train={train_pos} ({100*train_pos/len(train_df):.2f}%), test={test_pos} ({100*test_pos/len(test_df):.2f}%)")

if not target_cols:
    print("\n    ERROR: No target columns found! Check target calculation.")
    sys.exit(1)

# 5. Prepare Darts TimeSeries
print("\n[5] Preparing Darts TimeSeries...")
from app.services.darts_training import DartsTrainingService as TrainingService, DARTS_AVAILABLE

if not DARTS_AVAILABLE:
    print("    ERROR: Darts not available!")
    sys.exit(1)

training_service = TrainingService()

# Use first target column for this test
target_col = target_cols[0]
print(f"    Using target: {target_col}")

# Prepare data split - use only numeric feature columns
numeric_features = [c for c in feature_cols if df_clean[c].dtype in ['int64', 'float64']][:10]
print(f"    Using {len(numeric_features)} numeric features: {numeric_features}")

train_series, test_series, train_cov, test_cov = training_service.prepare_data_split(
    df_clean,
    train_ratio=TRAIN_RATIO,
    target_column=target_col,
    feature_columns=numeric_features,
    timeframe='1h'
)

print(f"    Train series: {len(train_series)} samples")
print(f"    Test series: {len(test_series)} samples")

# Check target values in series
train_vals = train_series.values().flatten()
test_vals = test_series.values().flatten()
print(f"    Train target values: min={train_vals.min()}, max={train_vals.max()}, mean={train_vals.mean():.4f}")
print(f"    Train unique values: {np.unique(train_vals)}")
print(f"    Train positives in series: {(train_vals == 1).sum()} / {len(train_vals)}")
print(f"    Test target values: min={test_vals.min()}, max={test_vals.max()}, mean={test_vals.mean():.4f}")
print(f"    Test unique values: {np.unique(test_vals)}")
print(f"    Test positives in series: {(test_vals == 1).sum()} / {len(test_vals)}")

# Check class balance
total_positives = (train_vals == 1).sum() + (test_vals == 1).sum()
total_samples = len(train_vals) + len(test_vals)
print(f"\n    CLASS BALANCE: {total_positives} positives / {total_samples} total ({100*total_positives/total_samples:.2f}%)")

if total_positives == 0:
    print("    WARNING: No positive samples! Model cannot learn anything useful.")
    print("    Try adjusting ZigZag deviation or using a different target type.")

# 6. Create and train models
print("\n[6] Training models...")
from app.services.darts_models import DartsModelService as MLModelsService
from app.services.losses import get_loss_function

ml_service = MLModelsService()

# Create focal loss function for imbalanced classification
positive_count = (train_vals == 1).sum()
negative_count = (train_vals == 0).sum()
print(f"    Creating FocalLoss with positive={positive_count}, negative={negative_count}")
loss_fn = get_loss_function(
    loss_type='focal_loss',
    positive_count=positive_count,
    negative_count=negative_count
)
print(f"    Loss function: {type(loss_fn).__name__}")

models_to_test = [
    ('lstm', {
        'input_chunk_length': INPUT_CHUNK_LENGTH,
        'output_chunk_length': 1,  # RNN uses shifted target
        'hidden_dim': 64,
        'n_rnn_layers': 2,
        'dropout': 0.1,
        'n_epochs': EPOCHS,
        'batch_size': 32,
        'learning_rate': 0.001,
    }),
]

for model_type, params in models_to_test:
    print(f"\n    --- Training {model_type.upper()} ---")
    print(f"    Params: input_chunk={params['input_chunk_length']}, output_chunk={params['output_chunk_length']}, epochs={params['n_epochs']}")

    try:
        # Create model with focal loss
        model = ml_service.create_model(model_type, params, loss_fn=loss_fn)

        # Train
        print(f"    Training for {EPOCHS} epochs...")
        training_result = training_service.train_model(
            model, train_series,
            val_series=test_series,
            covariates=train_cov,
            verbose=True
        )

        if training_result.get('status') == 'failed':
            print(f"    ERROR: Training failed - {training_result.get('error')}")
            continue

        print(f"    Training completed!")

        # Evaluate
        print(f"    Evaluating...")
        eval_result = training_service.evaluate_model(
            model, test_series,
            covariates=test_cov,
            optimize_metric='f1_score'
        )

        if 'error' in eval_result:
            print(f"    ERROR: Evaluation failed - {eval_result.get('error')}")
            continue

        print(f"    Evaluation results:")
        for metric, value in eval_result.items():
            if isinstance(value, (int, float)) and not pd.isna(value):
                print(f"        {metric}: {value:.4f}")

        # Get predictions for analysis
        print(f"    Analyzing predictions...")
        try:
            predictions = model.predict(n=len(test_series), series=train_series)
            pred_vals = predictions.values().flatten()

            print(f"        Prediction shape: {pred_vals.shape}")
            print(f"        Prediction range: [{pred_vals.min():.4f}, {pred_vals.max():.4f}]")
            print(f"        Prediction mean: {pred_vals.mean():.4f}")
            print(f"        Prediction std: {pred_vals.std():.4f}")

            # Compare with actual test values
            actual = test_vals[:len(pred_vals)]
            print(f"        Actual positives: {(actual == 1).sum()} / {len(actual)}")

            # Binary classification threshold
            threshold = 0.5
            pred_binary = (pred_vals > threshold).astype(int)
            print(f"        Predicted positives (>{threshold}): {pred_binary.sum()} / {len(pred_binary)}")

            # Confusion matrix
            from sklearn.metrics import confusion_matrix, f1_score, precision_score, recall_score
            cm = confusion_matrix(actual, pred_binary)
            print(f"        Confusion matrix:")
            print(f"            TN={cm[0,0]}, FP={cm[0,1]}")
            if cm.shape[0] > 1:
                print(f"            FN={cm[1,0]}, TP={cm[1,1]}")

            # Try different thresholds
            print(f"        Threshold analysis:")
            for t in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]:
                pred_t = (pred_vals > t).astype(int)
                if len(np.unique(pred_t)) > 1 or len(np.unique(actual)) > 1:
                    f1 = f1_score(actual, pred_t, zero_division=0)
                    prec = precision_score(actual, pred_t, zero_division=0)
                    rec = recall_score(actual, pred_t, zero_division=0)
                    print(f"            t={t}: pred_pos={pred_t.sum()}, F1={f1:.4f}, P={prec:.4f}, R={rec:.4f}")

        except Exception as e:
            print(f"        Error getting predictions: {e}")
            import traceback
            traceback.print_exc()

    except Exception as e:
        print(f"    ERROR: {e}")
        import traceback
        traceback.print_exc()

print("\n" + "=" * 80)
print("TEST COMPLETE")
print("=" * 80)
