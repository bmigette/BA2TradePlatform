#!/usr/bin/env python3
"""
Diagnose prediction issues for a model.

Usage:
    cd backend
    ./venv/bin/python scripts/diagnose_predictions.py <model_id>

Example:
    ./venv/bin/python scripts/diagnose_predictions.py mdl-9caa5703
"""

import sys
import json
import numpy as np
import pandas as pd
import torch
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.database import SessionLocal
from app.models.model import TrainedModel
from app.services.tsai_models import TSAIModelService
from app.services.data_preparation import DataPreparationService


def diagnose_model(model_id: str):
    """Run diagnostics on a model's predictions."""

    print(f"\n{'='*60}")
    print(f"DIAGNOSING MODEL: {model_id}")
    print(f"{'='*60}\n")

    # 1. Load model from database
    print("1. Loading model from database...")
    db = SessionLocal()
    try:
        model = db.query(TrainedModel).filter(TrainedModel.model_id == model_id).first()
        if not model:
            print(f"   ERROR: Model {model_id} not found in database")
            return False

        print(f"   Model type: {model.model_type}")
        print(f"   File path: {model.file_path}")
        print(f"   Dataset ID: {model.dataset_id}")
        print(f"   Has normalization_params: {model.normalization_params is not None}")

        hyperparameters = model.hyperparameters or {}
        c_in = hyperparameters.get('c_in')
        c_out = hyperparameters.get('c_out', 2)
        seq_len = hyperparameters.get('seqLen', 24)
        model_params = hyperparameters.get('modelParams', {})
        feature_columns = hyperparameters.get('featureColumns', [])

        print(f"   c_in: {c_in}")
        print(f"   c_out: {c_out}")
        print(f"   seq_len: {seq_len}")
        print(f"   Feature columns count: {len(feature_columns)}")

    finally:
        db.close()

    # 2. Check model file
    print("\n2. Checking model file...")
    file_path = Path(model.file_path)
    if not file_path.exists():
        print(f"   ERROR: Model file not found: {file_path}")
        return False
    print(f"   File exists: {file_path}")
    print(f"   File size: {file_path.stat().st_size} bytes")

    # 3. Load and check model weights
    print("\n3. Loading model weights...")
    try:
        state_dict = torch.load(file_path, map_location='cpu', weights_only=True)

        total_params = 0
        nan_params = 0
        inf_params = 0

        for name, param in state_dict.items():
            param_np = param.numpy()
            total_params += param_np.size
            nan_params += np.isnan(param_np).sum()
            inf_params += np.isinf(param_np).sum()

        print(f"   Total parameters: {total_params}")
        print(f"   NaN parameters: {nan_params}")
        print(f"   Inf parameters: {inf_params}")

        if nan_params > 0 or inf_params > 0:
            print("   WARNING: Model weights contain invalid values!")
    except Exception as e:
        print(f"   ERROR loading weights: {e}")
        return False

    # 4. Check for metadata file
    print("\n4. Checking for metadata file...")
    meta_path = file_path.with_name(file_path.stem + '_meta.json')
    if meta_path.exists():
        print(f"   Found: {meta_path}")
        with open(meta_path) as f:
            meta = json.load(f)
        print(f"   c_in from meta: {meta.get('c_in')}")
        print(f"   feature_columns count: {len(meta.get('feature_columns', []))}")
        feature_columns = meta.get('feature_columns', feature_columns)
    else:
        print(f"   Not found: {meta_path}")

    # 5. Find and load dataset
    print("\n5. Loading dataset...")

    # Try to find dataset from job cache
    job_id = model.job_id
    cache_paths = [
        Path(f"datasets/cache/jobs/{job_id}/combined_dataset.csv"),
        Path(f"cache/jobs/{job_id}/combined_dataset.csv"),
    ]

    dataset_path = None
    for path in cache_paths:
        if path.exists():
            dataset_path = path
            break

    if not dataset_path:
        # Try to load from database dataset
        db = SessionLocal()
        try:
            from app.models import Dataset
            dataset = db.query(Dataset).filter(Dataset.id == model.dataset_id).first()
            if dataset and Path(dataset.file_path).exists():
                dataset_path = Path(dataset.file_path)
        finally:
            db.close()

    if not dataset_path or not dataset_path.exists():
        print(f"   ERROR: Could not find dataset")
        return False

    print(f"   Dataset path: {dataset_path}")

    df = pd.read_csv(dataset_path)
    print(f"   Dataset shape: {df.shape}")
    print(f"   Dataset NaN count: {df.isna().sum().sum()}")

    # 6. Check feature columns
    print("\n6. Checking feature columns...")
    if not feature_columns:
        print("   ERROR: No feature columns found")
        return False

    missing_cols = [c for c in feature_columns if c not in df.columns]
    if missing_cols:
        print(f"   ERROR: {len(missing_cols)} feature columns missing from dataset:")
        print(f"   {missing_cols[:10]}{'...' if len(missing_cols) > 10 else ''}")
        return False

    print(f"   All {len(feature_columns)} feature columns found in dataset")

    # 7. Extract features and check values
    print("\n7. Extracting features...")
    features_df = df[feature_columns]
    print(f"   Features shape: {features_df.shape}")
    print(f"   Features NaN count: {features_df.isna().sum().sum()}")
    print(f"   Features min: {features_df.min().min()}")
    print(f"   Features max: {features_df.max().max()}")

    # Check for zero-variance columns
    zero_var_cols = [c for c in feature_columns if features_df[c].std() == 0]
    if zero_var_cols:
        print(f"   WARNING: {len(zero_var_cols)} zero-variance columns: {zero_var_cols[:5]}")

    # Check for extreme values
    extreme_cols = []
    for col in feature_columns:
        if abs(features_df[col].max()) > 1e15 or abs(features_df[col].min()) > 1e15:
            extreme_cols.append(col)
    if extreme_cols:
        print(f"   WARNING: {len(extreme_cols)} columns with extreme values: {extreme_cols[:5]}")

    # 8. Normalize features
    print("\n8. Normalizing features...")
    data_prep = DataPreparationService(buffer_pct=0.35)
    try:
        features_norm = data_prep.fit_transform(features_df, feature_columns)
        if hasattr(features_norm, 'values'):
            features_norm = features_norm.values

        print(f"   Normalized shape: {features_norm.shape}")
        nan_count = np.isnan(features_norm).sum()
        print(f"   Normalized NaN count: {nan_count}")

        if nan_count > 0:
            print("   ERROR: Normalization produced NaN values!")
            # Find which columns have NaN
            nan_cols_idx = np.where(np.isnan(features_norm).any(axis=0))[0]
            nan_col_names = [feature_columns[i] for i in nan_cols_idx[:10]]
            print(f"   Columns with NaN: {nan_col_names}")
            return False

        print(f"   Normalized min: {features_norm.min():.4f}")
        print(f"   Normalized max: {features_norm.max():.4f}")

    except Exception as e:
        print(f"   ERROR during normalization: {e}")
        return False

    # 9. Create sequences
    print("\n9. Creating sequences...")
    n_samples = len(features_norm) - seq_len + 1
    if n_samples <= 0:
        print(f"   ERROR: Not enough data for seq_len={seq_len}")
        return False

    X = np.array([features_norm[i:i+seq_len] for i in range(n_samples)])
    X = X.transpose(0, 2, 1)  # (samples, features, seq_len)
    print(f"   X shape: {X.shape}")
    print(f"   X NaN count: {np.isnan(X).sum()}")

    # 10. Create model and run prediction
    print("\n10. Running model prediction...")
    try:
        model_service = TSAIModelService()
        model_obj = model_service.create_model(
            model_type=model.model_type.lower(),
            params=model_params,
            c_in=c_in,
            c_out=c_out,
            seq_len=seq_len
        )
        model_obj.load_state_dict(state_dict)
        # Set model to evaluation mode
        model_obj.train(False)

        # Run on first 10 samples
        with torch.no_grad():
            X_tensor = torch.tensor(X[:10], dtype=torch.float32)
            outputs = model_obj(X_tensor)
            probs = torch.softmax(outputs, dim=1).numpy()

        print(f"   Output shape: {outputs.shape}")
        print(f"   Probs shape: {probs.shape}")
        print(f"   Probs NaN count: {np.isnan(probs).sum()}")

        if np.isnan(probs).any():
            print("   ERROR: Model produced NaN predictions!")
            print(f"   First 5 outputs: {outputs[:5]}")
            return False

        print(f"   Probs min: {probs.min():.4f}")
        print(f"   Probs max: {probs.max():.4f}")
        print(f"   Probs mean: {probs.mean():.4f}")
        print(f"\n   First 5 predictions (class 0, class 1):")
        for i in range(5):
            print(f"     {i}: [{probs[i, 0]:.4f}, {probs[i, 1]:.4f}]")

    except Exception as e:
        print(f"   ERROR running model: {e}")
        import traceback
        traceback.print_exc()
        return False

    print(f"\n{'='*60}")
    print("DIAGNOSIS COMPLETE - No issues found")
    print(f"{'='*60}\n")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/diagnose_predictions.py <model_id>")
        print("Example: python scripts/diagnose_predictions.py mdl-9caa5703")
        sys.exit(1)

    model_id = sys.argv[1]
    success = diagnose_model(model_id)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
