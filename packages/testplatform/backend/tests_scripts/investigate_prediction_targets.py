#!/usr/bin/env python3
"""
Investigate prediction targets and fitness calculation.

This script analyzes:
1. Dataset structure and prediction target columns
2. Calculate prediction targets like the job does
3. Train/test split and class distribution
4. Model predictions and metric calculation
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from pathlib import Path

# Dataset to investigate
DATASET_FILE = "datasets/AAPL_4h_20260126_182735.csv"
TRAIN_RATIO = 0.70  # 70/30 split

# Prediction target parameters (from user: 20% profit, 10% DD, 30 or 45 days)
PROFIT_PCT = 20
MAX_DD = 10
DAYS = 30


def main():
    print("="*70)
    print("PREDICTION TARGET INVESTIGATION")
    print("="*70)

    # Load dataset
    print(f"\n1. Loading dataset: {DATASET_FILE}")
    df = pd.read_csv(DATASET_FILE)
    print(f"   Total rows: {len(df)}")

    # Check if prediction targets exist
    target_cols = [c for c in df.columns if c.startswith('price_')]
    if target_cols:
        print(f"   Found {len(target_cols)} existing target columns: {target_cols}")
    else:
        print(f"   No prediction target columns found - need to calculate them")

    # Calculate prediction targets like the job does
    print(f"\n2. Calculating prediction targets...")
    print(f"   Parameters: profit={PROFIT_PCT}%, max_dd={MAX_DD}%, days={DAYS}")

    from app.services.darts_models import PredictionTargetService
    target_service = PredictionTargetService()

    targets = [
        {'profit_pct': PROFIT_PCT, 'max_dd': MAX_DD, 'days': DAYS, 'direction': 'up'},
        {'profit_pct': PROFIT_PCT, 'max_dd': MAX_DD, 'days': DAYS, 'direction': 'down'}
    ]

    df_with_targets = target_service.calculate_prediction_targets(df, targets)

    # Find the target columns
    target_col_up = f"price_up_{PROFIT_PCT}pct_{MAX_DD}dd_{DAYS}d"
    target_col_down = f"price_down_{PROFIT_PCT}pct_{MAX_DD}dd_{DAYS}d"

    print(f"   Created target columns: {target_col_up}, {target_col_down}")

    # Analyze target distribution
    print(f"\n3. Target column analysis...")
    for col in [target_col_up, target_col_down]:
        if col in df_with_targets.columns:
            values = df_with_targets[col]
            positives = (values == 1).sum()
            negatives = (values == 0).sum()
            total = len(values)

            print(f"\n   {col}:")
            print(f"      Total: {total}")
            print(f"      Positive (1): {positives} ({100*positives/total:.2f}%)")
            print(f"      Negative (0): {negatives} ({100*negatives/total:.2f}%)")

            # Show where positives are
            if positives > 0:
                positive_indices = np.where(values == 1)[0]
                print(f"      Positive indices (first 20): {positive_indices[:20]}")
        else:
            print(f"   ERROR: Column {col} not found!")

    # Train/test split
    print(f"\n4. Train/Test split (ratio: {TRAIN_RATIO})...")
    split_idx = int(len(df_with_targets) * TRAIN_RATIO)
    train_df = df_with_targets.iloc[:split_idx]
    test_df = df_with_targets.iloc[split_idx:]

    print(f"   Train: {len(train_df)} rows (indices 0 to {split_idx-1})")
    print(f"   Test: {len(test_df)} rows (indices {split_idx} to {len(df_with_targets)-1})")

    for col in [target_col_up, target_col_down]:
        if col in df_with_targets.columns:
            train_pos = (train_df[col] == 1).sum()
            train_neg = (train_df[col] == 0).sum()
            test_pos = (test_df[col] == 1).sum()
            test_neg = (test_df[col] == 0).sum()

            print(f"\n   {col}:")
            print(f"      Train: {train_pos} positive ({100*train_pos/len(train_df):.1f}%), {train_neg} negative")
            print(f"      Test:  {test_pos} positive ({100*test_pos/len(test_df):.1f}%), {test_neg} negative")

            if train_pos == 0:
                print(f"      WARNING: No positive samples in TRAIN set!")
            if test_pos == 0:
                print(f"      WARNING: No positive samples in TEST set!")

    # Simulate data preparation for training
    print(f"\n5. Simulating TrainingService data preparation...")

    from app.services.darts_training import DartsTrainingService as TrainingService
    ts = TrainingService()

    # Prepare data with the target column (like the job does)
    try:
        train_series, test_series, train_cov, test_cov = ts.prepare_data_split(
            df_with_targets,
            train_ratio=TRAIN_RATIO,
            target_column=target_col_up,
            timeframe='4h'
        )

        print(f"   Train series: {len(train_series)} points")
        print(f"   Test series: {len(test_series)} points")

        # Check scaled values
        train_values = train_series.values().flatten()
        test_values = test_series.values().flatten()

        print(f"\n   Scaled train values:")
        print(f"      min={train_values.min():.4f}, max={train_values.max():.4f}, mean={train_values.mean():.4f}")
        print(f"      unique values: {np.unique(train_values)[:10]}")

        print(f"\n   Scaled test values:")
        print(f"      min={test_values.min():.4f}, max={test_values.max():.4f}, mean={test_values.mean():.4f}")

        # Inverse transform to check original values
        if ts.scaler:
            train_orig = ts.scaler.inverse_transform(train_series).values().flatten()
            test_orig = ts.scaler.inverse_transform(test_series).values().flatten()

            print(f"\n   After inverse transform:")
            print(f"      Train: min={train_orig.min():.4f}, max={train_orig.max():.4f}")
            print(f"      Test: min={test_orig.min():.4f}, max={test_orig.max():.4f}")

            # Check binary distribution after inverse transform
            train_binary = np.round(train_orig).astype(int)
            test_binary = np.round(test_orig).astype(int)

            print(f"\n   Binary distribution after inverse transform:")
            print(f"      Train: {(train_binary==0).sum()} zeros, {(train_binary==1).sum()} ones")
            print(f"      Test: {(test_binary==0).sum()} zeros, {(test_binary==1).sum()} ones")

            # THE KEY CHECK: Do the inverse-transformed values match the original?
            print(f"\n   Verifying inverse transform accuracy:")
            original_train = df_with_targets.iloc[:split_idx][target_col_up].values
            print(f"      Original train unique: {np.unique(original_train)}")
            print(f"      Inverse train unique: {np.unique(train_binary)}")

            if not np.allclose(train_binary, original_train[:len(train_binary)]):
                print(f"      WARNING: Inverse transform doesn't match original values!")
                print(f"      First 10 original: {original_train[:10]}")
                print(f"      First 10 inverse:  {train_binary[:10]}")

    except Exception as e:
        print(f"   ERROR in data preparation: {e}")
        import traceback
        traceback.print_exc()

    # Check what happens with classification metrics
    print(f"\n6. Testing classification metrics calculation...")

    from app.services.metrics import ClassificationMetrics

    # Get raw test values from dataframe
    test_actuals = df_with_targets.iloc[split_idx:][target_col_up].values
    n_test = min(50, len(test_actuals))
    test_sample = test_actuals[:n_test]

    print(f"   Test sample ({n_test} values):")
    print(f"      Values: {test_sample}")
    print(f"      Positives: {(test_sample==1).sum()}, Negatives: {(test_sample==0).sum()}")

    if (test_sample == 1).sum() == 0:
        print(f"\n   PROBLEM: No positive samples in test sample!")
        print(f"   This explains why F1/precision/recall are all 0.0")
        print(f"   Accuracy=1.0 because predicting all 0s is correct when all actuals are 0")

    # Try with perfect predictions
    metrics = ClassificationMetrics.calculate_all(test_sample, test_sample.astype(float), threshold=0.5)
    print(f"\n   Metrics with perfect predictions:")
    print(f"      Accuracy: {metrics['accuracy']:.4f}")
    print(f"      Precision: {metrics['precision']:.4f}")
    print(f"      Recall: {metrics['recall']:.4f}")
    print(f"      F1: {metrics['f1_score']:.4f}")

    # Check price movement to understand why no targets are being triggered
    print(f"\n7. Price analysis to understand target calculation...")
    close_prices = df_with_targets['Close'].values

    print(f"   Overall price range: ${close_prices.min():.2f} to ${close_prices.max():.2f}")

    # Check max gains within the lookforward period
    print(f"   Checking potential gains in {DAYS}-day windows...")

    max_gains = []
    max_drawdowns = []
    for i in range(len(close_prices) - DAYS):
        entry = close_prices[i]
        future = close_prices[i+1:i+DAYS+1]
        max_price = future.max()
        min_price = future.min()

        gain = (max_price - entry) / entry * 100
        dd = (entry - min_price) / entry * 100

        max_gains.append(gain)
        max_drawdowns.append(dd)

    max_gains = np.array(max_gains)
    max_drawdowns = np.array(max_drawdowns)

    print(f"   Max gains in {DAYS}-day windows:")
    print(f"      Min: {max_gains.min():.2f}%")
    print(f"      Max: {max_gains.max():.2f}%")
    print(f"      Mean: {max_gains.mean():.2f}%")
    print(f"      Gains >= {PROFIT_PCT}%: {(max_gains >= PROFIT_PCT).sum()}")

    print(f"   Max drawdowns in {DAYS}-day windows:")
    print(f"      Min: {max_drawdowns.min():.2f}%")
    print(f"      Max: {max_drawdowns.max():.2f}%")
    print(f"      Mean: {max_drawdowns.mean():.2f}%")

    # Check how many meet both criteria
    meets_criteria = (max_gains >= PROFIT_PCT) & (max_drawdowns <= MAX_DD)
    print(f"\n   Windows meeting BOTH criteria (gain >= {PROFIT_PCT}% AND dd <= {MAX_DD}%):")
    print(f"      Count: {meets_criteria.sum()} out of {len(meets_criteria)}")

    if meets_criteria.sum() > 0:
        indices = np.where(meets_criteria)[0]
        print(f"      Indices: {indices[:20]}")
    else:
        print(f"      NONE! The target criteria are too strict for this dataset.")
        print(f"      Consider:")
        print(f"         - Lower profit target (e.g., 10% instead of {PROFIT_PCT}%)")
        print(f"         - Higher drawdown tolerance (e.g., 15% instead of {MAX_DD}%)")
        print(f"         - Longer time window (e.g., 60 days instead of {DAYS} days)")

    print("\n" + "="*70)
    print("INVESTIGATION COMPLETE")
    print("="*70)


if __name__ == "__main__":
    main()
