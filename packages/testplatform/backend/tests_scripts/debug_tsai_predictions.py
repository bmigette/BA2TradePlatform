#!/usr/bin/env python3
"""
Debug script to investigate why tsai models predict all zeros.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import torch

# Configuration
DATASET_PATH = "datasets/AAPL_1h_20260128_204556.csv"
ZIGZAG_DEVIATION = 1.0
PREDICTION_HORIZON = 3
TRAIN_RATIO = 0.7
EPOCHS = 10
SEQ_LEN = 24

print("=" * 80)
print("DEBUG TSAI PREDICTIONS")
print("=" * 80)

# 1. Load dataset
print("\n[1] Loading dataset...")
df = pd.read_csv(DATASET_PATH)
print(f"    Loaded {len(df)} rows")

# 2. Calculate targets
print(f"\n[2] Calculating ZigZag targets (deviation={ZIGZAG_DEVIATION}%)...")
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
print(f"    Target: {target_col}")
print(f"    Positives: {(df[target_col] == 1).sum()} / {len(df)} ({100*(df[target_col] == 1).sum()/len(df):.2f}%)")

# 3. Prepare features
print("\n[3] Preparing features...")
df['returns'] = df['Close'].pct_change()
df['volatility'] = df['returns'].rolling(10).std()
df_clean = df.dropna()
print(f"    Clean rows: {len(df_clean)}")

exclude_cols = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'ticker', target_col]
date_cols = [c for c in df_clean.columns if 'date' in c.lower()]
exclude_cols.extend(date_cols)
feature_cols = [c for c in df_clean.columns if c not in exclude_cols
                and not c.startswith('price_') and not c.startswith('reversal_')]
numeric_features = [c for c in feature_cols if df_clean[c].dtype in ['int64', 'float64']][:15]
print(f"    Features: {len(numeric_features)}")

# 4. Split data
split_idx = int(len(df_clean) * TRAIN_RATIO)
train_df = df_clean.iloc[:split_idx].copy()
test_df = df_clean.iloc[split_idx:].copy()

print(f"\n[4] Split: train={len(train_df)}, test={len(test_df)}")
print(f"    Train positives: {(train_df[target_col] == 1).sum()} ({100*(train_df[target_col] == 1).sum()/len(train_df):.2f}%)")
print(f"    Test positives: {(test_df[target_col] == 1).sum()} ({100*(test_df[target_col] == 1).sum()/len(test_df):.2f}%)")

# 5. Initialize services
print("\n[5] Initializing tsai services...")
from app.services.tsai_models import TSAIModelService, TSAI_AVAILABLE, DEVICE
from app.services.tsai_training import TSAITrainingService

model_service = TSAIModelService()
training_service = TSAITrainingService()

# 6. Prepare data
print("\n[6] Preparing tsai sequences...")
X_train, y_train = training_service.prepare_data(
    train_df, target_column=target_col, feature_columns=numeric_features,
    seq_len=SEQ_LEN, prediction_horizon=PREDICTION_HORIZON, prediction_mode='shift', fit_scaler=True
)
X_test, y_test = training_service.prepare_data(
    test_df, target_column=target_col, feature_columns=numeric_features,
    seq_len=SEQ_LEN, prediction_horizon=PREDICTION_HORIZON, prediction_mode='shift', fit_scaler=False
)

print(f"    X_train: {X_train.shape}, y_train: {y_train.shape}")
print(f"    X_test: {X_test.shape}, y_test: {y_test.shape}")
print(f"    Train positives in y: {(y_train == 1).sum()} / {len(y_train)}")
print(f"    Test positives in y: {(y_test == 1).sum()} / {len(y_test)}")

# 7. Train LSTM model
print("\n[7] Training LSTM with focal loss...")
model = model_service.create_model(
    model_type='lstm',
    params={'hidden_size': 64, 'n_layers': 2, 'dropout': 0.1},
    c_in=X_train.shape[1],
    c_out=2,
    seq_len=SEQ_LEN
)

# Get focal loss
loss_fn = training_service.get_loss_function('focal', prediction_mode='shift')
print(f"    Loss function: {type(loss_fn).__name__}")

result = training_service.train_model(
    model=model,
    train_data=(X_train, y_train),
    val_data=(X_test, y_test),
    epochs=EPOCHS,
    batch_size=32,
    learning_rate=0.001,
    loss_fn=loss_fn,
    prediction_mode='shift'
)

print(f"    Training status: {result.get('status')}")

if result.get('status') != 'success':
    print(f"    ERROR: {result.get('error')}")
    sys.exit(1)

# 8. Analyze predictions
print("\n[8] Analyzing predictions...")
learner = result.get('learner')
trained_model = result.get('model')

# Get raw model outputs
X_tensor = torch.tensor(X_test, dtype=torch.float32)
if DEVICE:
    X_tensor = X_tensor.to(DEVICE)
    trained_model = trained_model.to(DEVICE)

# Set model to evaluation mode
trained_model.train(False)
with torch.no_grad():
    outputs = trained_model(X_tensor)
    print(f"    Raw outputs shape: {outputs.shape}")
    print(f"    Raw outputs sample (first 5):\n{outputs[:5]}")

    # Apply softmax
    probs = torch.softmax(outputs, dim=1)
    print(f"\n    Softmax probs shape: {probs.shape}")
    print(f"    Softmax probs sample (first 5):\n{probs[:5]}")

    # Get positive class probability
    pos_probs = probs[:, 1].cpu().numpy()
    print(f"\n    Positive class probs:")
    print(f"        Min: {pos_probs.min():.6f}")
    print(f"        Max: {pos_probs.max():.6f}")
    print(f"        Mean: {pos_probs.mean():.6f}")
    print(f"        Std: {pos_probs.std():.6f}")

    # Distribution
    print(f"\n    Prob distribution:")
    for threshold in [0.1, 0.2, 0.3, 0.4, 0.5]:
        count = (pos_probs > threshold).sum()
        print(f"        > {threshold}: {count} samples ({100*count/len(pos_probs):.1f}%)")

# 9. Compare with actual labels
print("\n[9] Comparison with actuals...")
y_actual = y_test
pred_05 = (pos_probs > 0.5).astype(int)
pred_03 = (pos_probs > 0.3).astype(int)

print(f"    Actual positives: {(y_actual == 1).sum()}")
print(f"    Predicted (>0.5): {pred_05.sum()}")
print(f"    Predicted (>0.3): {pred_03.sum()}")

from sklearn.metrics import f1_score, confusion_matrix
f1_05 = f1_score(y_actual, pred_05, zero_division=0)
f1_03 = f1_score(y_actual, pred_03, zero_division=0)
print(f"\n    F1 (threshold=0.5): {f1_05:.4f}")
print(f"    F1 (threshold=0.3): {f1_03:.4f}")

cm = confusion_matrix(y_actual, pred_05)
print(f"\n    Confusion matrix (threshold=0.5):")
print(f"        TN={cm[0,0]}, FP={cm[0,1]}")
if cm.shape[0] > 1:
    print(f"        FN={cm[1,0]}, TP={cm[1,1]}")

# 10. Check if model is learning anything
print("\n[10] Check training vs validation loss trend...")
if hasattr(learner, 'recorder') and learner.recorder.values:
    vals = learner.recorder.values
    print(f"    Epochs recorded: {len(vals)}")
    for i, v in enumerate(vals[:5]):
        print(f"        Epoch {i}: train_loss={v[0]:.4f}, val_loss={v[1]:.4f}")
    if len(vals) > 5:
        print(f"        ...")
        for i, v in enumerate(vals[-2:], start=len(vals)-2):
            print(f"        Epoch {i}: train_loss={v[0]:.4f}, val_loss={v[1]:.4f}")

print("\n" + "=" * 80)
print("DEBUG COMPLETE")
print("=" * 80)
