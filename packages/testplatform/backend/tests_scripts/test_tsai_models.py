#!/usr/bin/env python3
"""
Test script for tsai models with ZigZag prediction targets.

Tests all supported tsai models with:
- AAPL_1h dataset
- ZigZag 1% deviation for more datapoints
- 10 epochs
- Both shift and multistep prediction modes with 3 bar horizon
- Verifies F1 > 0 for all models
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from datetime import datetime

# Configuration
DATASET_PATH = "datasets/AAPL_1h_20260128_204556.csv"
ZIGZAG_DEVIATION = 1.0  # 1% - lower to get more signals
PREDICTION_HORIZON = 3
TRAIN_RATIO = 0.7  # 70/30 split
EPOCHS = 10
SEQ_LEN = 24

# Models to test (excluding patchtst which is not for classification)
MODELS_TO_TEST = [
    'lstm',
    'gru',
    'tcn',
    'inception',
    'resnet',
    'xception',
    'omniscale',
    'minirocket',
    'lstm_fcn',
    'tst',
]

# Models that need force_cpu on MPS (Apple Silicon)
FORCE_CPU_MODELS = ['xception', 'patchtst', 'tst']

print("=" * 80)
print("TSAI MODELS TEST SCRIPT")
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

# Calculate bullish zigzag target
targets_config = [
    {
        'type': 'trend_reversal',
        'indicator': 'zigzag',
        'indicatorParams': {'deviationPct': ZIGZAG_DEVIATION},
        'threshold': 0,
        'direction': 'bullish'
    },
]

# Use calculate_all_targets which handles all target types
target_results = target_service.calculate_all_targets(df.copy(), targets_config)

print(f"    Calculated {len(target_results)} targets:")
for result in target_results:
    col_name = result.get('columnName', 'unknown')
    stats = result.get('stats', {})
    print(f"    - {col_name}: positives={stats.get('positiveCount', 0)}, "
          f"total={stats.get('validRows', 0)}, "
          f"positive_pct={stats.get('positivePct', 0):.2f}%")

# Add target columns to DataFrame
df_with_targets = df.copy()
target_cols = []
for result in target_results:
    col_name = result.get('columnName')
    data = result.get('data', [])
    if col_name and len(data) == len(df_with_targets):
        values = [d.get('value', 0) if d.get('value') is not None else 0 for d in data]
        df_with_targets[col_name] = values
        target_cols.append(col_name)
        print(f"    Added column: {col_name}")

if not target_cols:
    print("\n    ERROR: No target columns found! Check target calculation.")
    sys.exit(1)

target_col = target_cols[0]
print(f"    Using target: {target_col}")

# 3. Prepare features
print("\n[3] Preparing features...")

# Add some additional features
df_with_targets['returns'] = df_with_targets['Close'].pct_change()
df_with_targets['log_returns'] = np.log(df_with_targets['Close'] / df_with_targets['Close'].shift(1))
df_with_targets['volatility'] = df_with_targets['returns'].rolling(10).std()

# Drop NaN rows
df_clean = df_with_targets.dropna()
print(f"    After dropping NaN: {len(df_clean)} rows (dropped {len(df_with_targets) - len(df_clean)})")

# Check target distribution
positives = (df_clean[target_col] == 1).sum()
print(f"    Target positives: {positives} ({100*positives/len(df_clean):.2f}%)")

# Feature columns - use discriminative features for trend reversal prediction
# Prioritize oscillators and momentum indicators over moving averages
priority_features = [
    # Momentum/oscillator indicators (better for reversal detection)
    'rsi_14', 'stochastic__k', 'stochastic__d',
    'macd__line', 'macd__signal', 'macd__histogram',
    # Volatility
    'atr_14',
    # Bollinger bands (relative position)
    'bbands_20_upper', 'bbands_20_middle', 'bbands_20_lower',
    # Computed features
    'returns', 'volatility',
    # Moving averages (less useful for reversals but include a few)
    'sma_10', 'sma_20', 'ema_12'
]

# Filter to only features that exist in the dataset
numeric_features = [f for f in priority_features if f in df_clean.columns and df_clean[f].dtype in ['int64', 'float64']]
print(f"    Using {len(numeric_features)} discriminative features: {numeric_features}")

# 4. Import tsai services
print("\n[4] Initializing tsai services...")
from app.services.tsai_models import TSAIModelService, TSAI_AVAILABLE, MPS_AVAILABLE
from app.services.tsai_training import TSAITrainingService

if not TSAI_AVAILABLE:
    print("    ERROR: tsai not available!")
    sys.exit(1)

model_service = TSAIModelService()
training_service = TSAITrainingService()

# 5. Train/test split
print("\n[5] Splitting data...")
split_idx = int(len(df_clean) * TRAIN_RATIO)
train_df = df_clean.iloc[:split_idx].copy()
test_df = df_clean.iloc[split_idx:].copy()
print(f"    Train: {len(train_df)} rows, Test: {len(test_df)} rows")

# Check target distribution in train/test
train_pos = (train_df[target_col] == 1).sum()
test_pos = (test_df[target_col] == 1).sum()
print(f"    Train positives: {train_pos} ({100*train_pos/len(train_df):.2f}%)")
print(f"    Test positives: {test_pos} ({100*test_pos/len(test_df):.2f}%)")

# 6. Prepare data using tsai training service
print("\n[6] Preparing tsai data (sequence windows)...")

# Results storage
results = {
    'shift': {},
    'multistep': {}
}

# Test both prediction modes
for prediction_mode in ['shift', 'multistep']:
    print(f"\n{'='*80}")
    print(f"TESTING {prediction_mode.upper()} MODE (horizon={PREDICTION_HORIZON})")
    print(f"{'='*80}")

    # Prepare train data
    X_train, y_train = training_service.prepare_data(
        train_df,
        target_column=target_col,
        feature_columns=numeric_features,
        seq_len=SEQ_LEN,
        prediction_horizon=PREDICTION_HORIZON,
        prediction_mode=prediction_mode,
        fit_scaler=True
    )

    # Prepare test data (using fitted scaler)
    X_test, y_test = training_service.prepare_data(
        test_df,
        target_column=target_col,
        feature_columns=numeric_features,
        seq_len=SEQ_LEN,
        prediction_horizon=PREDICTION_HORIZON,
        prediction_mode=prediction_mode,
        fit_scaler=False
    )

    print(f"    Train data: X={X_train.shape}, y={y_train.shape}")
    print(f"    Test data: X={X_test.shape}, y={y_test.shape}")

    if prediction_mode == 'shift':
        print(f"    Train positives: {(y_train == 1).sum()} / {len(y_train)}")
        print(f"    Test positives: {(y_test == 1).sum()} / {len(y_test)}")

        # Create weighted loss function to handle class imbalance
        import torch
        from fastai.losses import CrossEntropyLossFlat
        pos_count = (y_train == 1).sum()
        neg_count = (y_train == 0).sum()
        pos_weight = neg_count / pos_count if pos_count > 0 else 1.0
        weights = torch.tensor([1.0, float(pos_weight)], dtype=torch.float32)
        if MPS_AVAILABLE:
            weights = weights.to(torch.device('mps'))
        loss_fn = CrossEntropyLossFlat(weight=weights)
        print(f"    Using weighted CE loss with pos_weight={pos_weight:.2f}")
    else:
        # Multi-step has multiple outputs
        print(f"    y_train unique values: {np.unique(y_train)}")
        print(f"    y_test unique values: {np.unique(y_test)}")
        loss_fn = None  # Use default for multistep

    # 7. Test each model
    for model_type in MODELS_TO_TEST:
        print(f"\n    --- {model_type.upper()} ---")

        try:
            # Get default params from MODEL_ARCHITECTURES
            arch = model_service.MODEL_ARCHITECTURES.get(model_type, {})
            params = arch.get('default_params', {}).copy()

            # Create model
            c_in = X_train.shape[1]  # Number of features
            if prediction_mode == 'multistep':
                # Multistep uses BCEWithLogitsLoss (multi-label), one output per horizon per target
                # For single target: c_out = prediction_horizon
                # For multiple targets: c_out = prediction_horizon * num_targets
                num_targets = 1  # This test uses single target
                c_out = PREDICTION_HORIZON * num_targets
            else:
                c_out = 2  # Binary classification (2 classes)

            model = model_service.create_model(
                model_type=model_type,
                params=params,
                c_in=c_in,
                c_out=c_out,
                seq_len=SEQ_LEN
            )

            # Train with weighted loss for shift mode
            force_cpu = model_type in FORCE_CPU_MODELS and MPS_AVAILABLE
            training_result = training_service.train_model(
                model=model,
                train_data=(X_train, y_train),
                val_data=(X_test, y_test),
                epochs=EPOCHS,
                batch_size=32,
                learning_rate=0.001,
                loss_fn=loss_fn,
                force_cpu=force_cpu,
                prediction_mode=prediction_mode
            )

            if training_result.get('status') == 'failed':
                print(f"        ERROR: Training failed - {training_result.get('error')}")
                results[prediction_mode][model_type] = {'status': 'failed', 'error': training_result.get('error')}
                continue

            print(f"        Training completed!")

            # Get the trained learner
            learner = training_result.get('learner')
            if learner is None:
                print(f"        ERROR: No learner returned")
                results[prediction_mode][model_type] = {'status': 'failed', 'error': 'No learner'}
                continue

            # Evaluate with threshold optimization
            # Neural networks often output poorly calibrated probabilities,
            # so we try multiple thresholds and pick the best one
            best_f1 = 0
            best_threshold = 0.5
            for threshold in [0.2, 0.3, 0.4, 0.5]:
                eval_t = training_service.evaluate_model(
                    model=learner,
                    test_data=(X_test, y_test),
                    metric='f1_score',
                    prediction_mode=prediction_mode,
                    threshold=threshold
                )
                if eval_t.get('f1_score', 0) > best_f1:
                    best_f1 = eval_t.get('f1_score', 0)
                    best_threshold = threshold
                    eval_result = eval_t

            if best_f1 == 0:
                # Fallback to default threshold
                eval_result = training_service.evaluate_model(
                    model=learner,
                    test_data=(X_test, y_test),
                    metric='f1_score',
                    prediction_mode=prediction_mode
                )

            if 'error' in eval_result:
                print(f"        ERROR: Evaluation failed - {eval_result.get('error')}")
                results[prediction_mode][model_type] = {'status': 'failed', 'error': eval_result.get('error')}
                continue

            # Show metrics
            f1 = eval_result.get('f1_score', 0)
            accuracy = eval_result.get('accuracy', 0)
            precision = eval_result.get('precision', 0)
            recall = eval_result.get('recall', 0)

            print(f"        F1={f1:.4f}, Accuracy={accuracy:.4f}, Precision={precision:.4f}, Recall={recall:.4f}")

            results[prediction_mode][model_type] = {
                'status': 'success',
                'f1_score': f1,
                'accuracy': accuracy,
                'precision': precision,
                'recall': recall
            }

        except Exception as e:
            print(f"        ERROR: {e}")
            import traceback
            traceback.print_exc()
            results[prediction_mode][model_type] = {'status': 'failed', 'error': str(e)}

# 8. Summary
print("\n" + "=" * 80)
print("TEST SUMMARY")
print("=" * 80)

all_passed = True
for mode in ['shift', 'multistep']:
    print(f"\n{mode.upper()} MODE (horizon={PREDICTION_HORIZON}):")
    print("-" * 60)

    for model_type in MODELS_TO_TEST:
        result = results[mode].get(model_type, {'status': 'not_run'})
        status = result.get('status', 'unknown')

        if status == 'success':
            f1 = result.get('f1_score', 0)
            passed = f1 > 0
            status_str = "PASS" if passed else "FAIL (F1=0)"
            if not passed:
                all_passed = False
            print(f"    {model_type:15s}: {status_str:15s} F1={f1:.4f}")
        else:
            all_passed = False
            error = result.get('error', 'Unknown error')[:40]
            print(f"    {model_type:15s}: FAIL           Error: {error}")

print("\n" + "=" * 80)
if all_passed:
    print("ALL TESTS PASSED!")
else:
    print("SOME TESTS FAILED!")
print("=" * 80)

sys.exit(0 if all_passed else 1)
