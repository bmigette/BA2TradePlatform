#!/usr/bin/env python3
"""
Debug script to check tsai data preparation.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

DATASET_PATH = "datasets/AAPL_1h_20260128_204556.csv"
ZIGZAG_DEVIATION = 1.0
SEQ_LEN = 24
PREDICTION_HORIZON = 3

print("=" * 80)
print("DEBUG TSAI DATA PREPARATION")
print("=" * 80)

# 1. Load and prepare data
print("\n[1] Loading dataset...")
df = pd.read_csv(DATASET_PATH)

# Calculate target
from app.services.darts_models import PredictionTargetService
target_service = PredictionTargetService()
targets_config = [{
    'type': 'trend_reversal',
    'indicator': 'zigzag',
    'indicatorParams': {'deviationPct': ZIGZAG_DEVIATION},
    'threshold': 0,
    'direction': 'bullish'
}]
target_results = target_service.calculate_all_targets(df.copy(), targets_config)
target_col = target_results[0]['columnName']
target_data = target_results[0]['data']
df[target_col] = [d.get('value', 0) if d.get('value') is not None else 0 for d in target_data]

# Prepare features
df['returns'] = df['Close'].pct_change()
df['volatility'] = df['returns'].rolling(10).std()
df_clean = df.dropna()

exclude_cols = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'ticker', target_col]
date_cols = [c for c in df_clean.columns if 'date' in c.lower()]
exclude_cols.extend(date_cols)
feature_cols = [c for c in df_clean.columns if c not in exclude_cols
                and not c.startswith('price_') and not c.startswith('reversal_')]
numeric_features = [c for c in feature_cols if df_clean[c].dtype in ['int64', 'float64']][:15]

print(f"    Features used: {numeric_features}")

# Split data
split_idx = int(len(df_clean) * 0.7)
train_df = df_clean.iloc[:split_idx].copy()

# 2. Check raw feature values
print("\n[2] Raw feature statistics (before normalization):")
for feat in numeric_features[:5]:
    vals = train_df[feat].values
    print(f"    {feat}: min={vals.min():.4f}, max={vals.max():.4f}, mean={vals.mean():.4f}, std={vals.std():.4f}")

# 3. Check normalized data
print("\n[3] Preparing data with normalization...")
from app.services.tsai_training import TSAITrainingService

training_service = TSAITrainingService()
X_train, y_train = training_service.prepare_data(
    train_df, target_column=target_col, feature_columns=numeric_features,
    seq_len=SEQ_LEN, prediction_horizon=PREDICTION_HORIZON, prediction_mode='shift', fit_scaler=True
)

print(f"    X_train shape: {X_train.shape}")  # (samples, features, seq_len)
print(f"    y_train shape: {y_train.shape}")

# 4. Check normalized feature values
print("\n[4] Normalized feature statistics:")
for i, feat in enumerate(numeric_features[:5]):
    # X_train is (samples, features, seq_len)
    vals = X_train[:, i, :].flatten()
    print(f"    {feat}: min={vals.min():.4f}, max={vals.max():.4f}, mean={vals.mean():.4f}, std={vals.std():.4f}")

# 5. Check variation across samples
print("\n[5] Checking variation across samples (first feature):")
sample_means = X_train[:, 0, :].mean(axis=1)  # Mean of first feature for each sample
print(f"    Sample means: min={sample_means.min():.4f}, max={sample_means.max():.4f}, std={sample_means.std():.4f}")

# 6. Check variation across sequence positions
print("\n[6] Checking variation across sequence positions (first 5 samples):")
for i in range(5):
    seq = X_train[i, 0, :]  # First feature, all seq positions
    print(f"    Sample {i}: min={seq.min():.4f}, max={seq.max():.4f}, range={seq.max()-seq.min():.4f}")

# 7. Check target distribution in prepared data
print("\n[7] Target distribution after sequence preparation:")
print(f"    Positives: {(y_train == 1).sum()} / {len(y_train)} ({100*(y_train == 1).sum()/len(y_train):.2f}%)")

# 8. Check if positive samples have distinct features
print("\n[8] Feature comparison: positive vs negative samples")
pos_idx = np.where(y_train == 1)[0]
neg_idx = np.where(y_train == 0)[0]

for i, feat in enumerate(numeric_features[:5]):
    pos_vals = X_train[pos_idx, i, :].flatten()
    neg_vals = X_train[neg_idx, i, :].flatten()
    print(f"    {feat}:")
    print(f"        Positive mean={pos_vals.mean():.4f}, std={pos_vals.std():.4f}")
    print(f"        Negative mean={neg_vals.mean():.4f}, std={neg_vals.std():.4f}")
    print(f"        Difference: {abs(pos_vals.mean() - neg_vals.mean()):.6f}")

# 9. Check scaler parameters
print("\n[9] Scaler parameters:")
if training_service.data_prep:
    params = training_service.data_prep.get_scaler_params()
    print(f"    Method: {params.get('method')}")
    print(f"    Columns: {len(params.get('columns', []))}")
    for col in params.get('columns', [])[:5]:
        p = params['column_params'][col]
        print(f"    {col}: min_val={p['min_val']:.4f}, max_val={p['max_val']:.4f}")

print("\n" + "=" * 80)
print("DEBUG COMPLETE")
print("=" * 80)
