#!/usr/bin/env python3
"""
Fix feature_columns mismatch in model metadata and database.

This script:
1. Loads the training data from cache
2. Uses DataPreparationService to determine valid feature columns (after dropping zero-variance)
3. Updates the metadata JSON files in the models folder
4. Updates the database entries

Usage:
    cd backend
    ./venv/bin/python scripts/fix_feature_columns.py <job_id>

Example:
    ./venv/bin/python scripts/fix_feature_columns.py 6ced0e15-309
"""

import sys
import json
import pandas as pd
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.database import SessionLocal
from app.models.model import TrainedModel
from app.services.data_preparation import DataPreparationService


def get_valid_feature_columns(train_df: pd.DataFrame, feature_columns: list) -> list:
    """
    Get valid feature columns using DataPreparationService.

    This matches what happens during actual training - zero-variance columns are dropped.
    """
    data_prep = DataPreparationService(buffer_pct=0.35)
    data_prep.fit_transform(train_df, feature_columns, method='minmax_buffered')
    valid_cols = data_prep.get_valid_columns()
    return valid_cols


def fix_job_models(job_id: str):
    """Fix feature_columns for all models in a job."""

    # Paths - try multiple possible cache locations
    cache_dir = None
    for cache_base in ["datasets/cache/jobs", "cache/jobs"]:
        candidate = Path(cache_base) / job_id
        if candidate.exists():
            cache_dir = candidate
            break

    models_dir = Path("trained_models") / job_id

    if not cache_dir:
        print(f"Error: Cache directory not found for job {job_id}")
        print("  Tried: datasets/cache/jobs/{job_id}, cache/jobs/{job_id}")
        return False

    print(f"Found cache directory: {cache_dir}")

    if not models_dir.exists():
        print(f"Error: Models directory not found: {models_dir}")
        return False

    # Load training data to determine valid columns - try multiple filenames
    train_file = None
    for train_name in ["train_rnn.csv", "train_multistep.csv", "train_data.csv", "combined_dataset.csv"]:
        candidate = cache_dir / train_name
        if candidate.exists():
            train_file = candidate
            break

    if not train_file:
        print(f"Error: Training data not found in {cache_dir}")
        return False

    print(f"Loading training data from {train_file}...")
    train_df = pd.read_csv(train_file)
    print(f"  Loaded {len(train_df)} rows, {len(train_df.columns)} columns")

    # Determine feature columns (same logic as job_handler.py)
    exclude_cols = {'Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'ticker'}
    target_cols = {c for c in train_df.columns if c.startswith('price_') or 'target' in c.lower()}
    exclude_cols.update(target_cols)
    feature_columns = [c for c in train_df.columns if c not in exclude_cols]
    print(f"  Initial feature columns: {len(feature_columns)}")

    # Get valid columns using DataPreparationService (same as training)
    valid_feature_columns = get_valid_feature_columns(train_df, feature_columns)
    print(f"  Valid feature columns (after dropping zero-variance): {len(valid_feature_columns)}")

    # Save valid columns for reference
    valid_cols_file = cache_dir / "valid_feature_columns.json"
    with open(valid_cols_file, 'w') as f:
        json.dump(valid_feature_columns, f, indent=2)
    print(f"  Saved valid columns to {valid_cols_file}")

    # Update all metadata JSON files
    meta_files = list(models_dir.glob("*_meta.json"))
    print(f"\nFound {len(meta_files)} metadata files to update")

    updated_files = 0
    for meta_file in meta_files:
        try:
            with open(meta_file, 'r') as f:
                meta = json.load(f)

            old_count = len(meta.get('feature_columns', []))
            c_in = meta.get('c_in')

            if old_count != len(valid_feature_columns):
                meta['feature_columns'] = valid_feature_columns
                with open(meta_file, 'w') as f:
                    json.dump(meta, f, indent=2, default=str)
                print(f"  Updated {meta_file.name}: {old_count} -> {len(valid_feature_columns)} (c_in={c_in})")
                updated_files += 1
            else:
                print(f"  Skipped {meta_file.name}: already correct ({old_count})")
        except Exception as e:
            print(f"  Error updating {meta_file.name}: {e}")

    print(f"\nUpdated {updated_files} metadata files")

    # Update database entries
    print("\nUpdating database entries...")
    db = SessionLocal()
    try:
        # Find models from this job
        models = db.query(TrainedModel).filter(TrainedModel.job_id == job_id).all()
        print(f"Found {len(models)} models in database for job {job_id}")

        updated_db = 0
        for model in models:
            if model.hyperparameters:
                hyperparams = dict(model.hyperparameters)
                old_features = hyperparams.get('featureColumns', [])
                old_count = len(old_features) if old_features else 0

                if old_count != len(valid_feature_columns):
                    hyperparams['featureColumns'] = valid_feature_columns
                    model.hyperparameters = hyperparams
                    print(f"  Updated {model.model_id}: {old_count} -> {len(valid_feature_columns)}")
                    updated_db += 1
                else:
                    print(f"  Skipped {model.model_id}: already correct ({old_count})")

        if updated_db > 0:
            db.commit()
            print(f"\nCommitted {updated_db} database updates")
        else:
            print("\nNo database updates needed")

    except Exception as e:
        print(f"Database error: {e}")
        db.rollback()
        return False
    finally:
        db.close()

    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/fix_feature_columns.py <job_id>")
        print("Example: python scripts/fix_feature_columns.py 6ced0e15-309")
        sys.exit(1)

    job_id = sys.argv[1]
    print(f"Fixing feature_columns for job: {job_id}")
    print("=" * 60)

    success = fix_job_models(job_id)

    if success:
        print("\n" + "=" * 60)
        print("Done! Models should now work correctly for backtesting.")
    else:
        print("\nFailed to fix models.")
        sys.exit(1)


if __name__ == "__main__":
    main()
