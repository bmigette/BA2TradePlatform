"""
Job Training Handler

Background task handler for ML model training jobs.
Uses real ML services instead of simulation.
"""

import logging
import pandas as pd
import numpy as np
import os
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path

from app.models.database import SessionLocal
from app.models.dataset import Dataset

logger = logging.getLogger(__name__)

# Per-job cache directory (test bucket, app.paths — not the repo tree).
from app.paths import JOBS_CACHE_DIR as DATASET_CACHE_DIR


def get_job_cache_dir(task_id: str) -> Path:
    """Get the cache directory for a specific job."""
    cache_dir = DATASET_CACHE_DIR / task_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


# RNN models that only support output_chunk_length=1
RNN_MODELS = ['lstm', 'gru']

# Multi-step models that support output_chunk_length > 1
MULTISTEP_MODELS = ['nbeats', 'tcn', 'transformer', 'tft']

# Sparse indicators that should be forward-filled at training time
# These indicators have NaN between pivot points which can cause training issues
SPARSE_INDICATOR_PATTERNS = ['zigzag', 'zigzag_direction']


def split_datasets_by_role(dfs: list, dataset_ids: list, test_dataset_ids: list) -> tuple:
    """Split datasets into train and test groups based on manual assignment.

    Args:
        dfs: List of DataFrames
        dataset_ids: List of dataset IDs corresponding to each DataFrame
        test_dataset_ids: List of dataset IDs designated as test sets

    Returns:
        Tuple of (train_dfs, test_dfs)
    """
    train_dfs = [df for df, did in zip(dfs, dataset_ids) if did not in test_dataset_ids]
    test_dfs = [df for df, did in zip(dfs, dataset_ids) if did in test_dataset_ids]
    return train_dfs, test_dfs


def create_kfold_splits(dfs: list, dataset_ids: list) -> list:
    """Create K-fold splits where each dataset serves as the test set once.

    Args:
        dfs: List of DataFrames
        dataset_ids: List of dataset IDs corresponding to each DataFrame

    Returns:
        List of tuples: (train_dfs, test_dfs, test_dataset_ids)
    """
    folds = []
    for i, test_id in enumerate(dataset_ids):
        test_dfs = [dfs[i]]
        train_dfs = [df for j, df in enumerate(dfs) if j != i]
        folds.append((train_dfs, test_dfs, [test_id]))
    return folds


def ffill_sparse_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Forward-fill sparse indicators at training time.

    Sparse indicators like ZigZag have NaN values between pivot points.
    This is useful for visualization but causes issues during training.
    Forward-filling makes the data usable for ML while keeping the
    original sparse data in the dataset for chart visualization.

    Args:
        df: DataFrame with indicator columns

    Returns:
        DataFrame with sparse indicators forward-filled
    """
    df = df.copy()
    ffilled_cols = []

    for col in df.columns:
        col_lower = col.lower()
        if any(pattern in col_lower for pattern in SPARSE_INDICATOR_PATTERNS):
            nan_count_before = df[col].isna().sum()
            if nan_count_before > 0:
                # Forward-fill first, then backward-fill for leading NaNs
                df[col] = df[col].ffill().bfill()
                nan_count_after = df[col].isna().sum()
                if nan_count_before != nan_count_after:
                    ffilled_cols.append(col)
                    logger.debug(f"Filled {col}: {nan_count_before} -> {nan_count_after} NaNs")

    if ffilled_cols:
        logger.info(f"Forward-filled {len(ffilled_cols)} sparse indicator(s): {ffilled_cols}")

    return df


def add_target_features(df: pd.DataFrame, target_config: Dict[str, Any]) -> pd.DataFrame:
    """
    Add target-derived features based on target configuration.

    Creates additional features that can help the model learn:
    - Lagged indicator values (for indicator-based targets)
    - Bars-since-last-signal counter

    Args:
        df: DataFrame with indicator columns
        target_config: Target configuration dict with:
            - includeValues: bool - Include indicator values as features
            - valueLookback: int - How many bars of lagged values (default 5)
            - includeBarsSince: bool - Include bars-since-last-signal counter
            - indicator: str - Name of indicator (for indicator-based targets)

    Returns:
        DataFrame with added target features
    """
    df = df.copy()
    added_features = []

    # Check if we should include indicator values as features
    if target_config.get('includeValues', False):
        # Find the indicator column
        indicator = target_config.get('indicator')
        if indicator:
            # Look for columns matching the indicator
            indicator_cols = [c for c in df.columns if indicator.lower() in c.lower()]
            lookback = target_config.get('valueLookback', 5)

            for indicator_col in indicator_cols:
                # Add lagged values
                for lag in range(1, lookback + 1):
                    lag_col = f'{indicator_col}_lag_{lag}'
                    df[lag_col] = df[indicator_col].shift(lag).ffill()
                    added_features.append(lag_col)
                logger.debug(f"Added {lookback} lagged features for {indicator_col}")

    # Check if we should include bars-since-last-signal
    if target_config.get('includeBarsSince', False):
        # Look for the target column
        target_col = None
        for col in df.columns:
            if col.lower() == 'target' or col.startswith('target_'):
                target_col = col
                break

        if target_col and target_col in df.columns:
            df['bars_since_signal'] = calculate_bars_since_change(df[target_col])
            added_features.append('bars_since_signal')
            logger.debug(f"Added bars_since_signal feature from {target_col}")

    if added_features:
        logger.info(f"Added {len(added_features)} target-derived features: {added_features[:5]}{'...' if len(added_features) > 5 else ''}")

    return df


def calculate_bars_since_change(series: pd.Series) -> pd.Series:
    """
    Count bars since last value change in a series.

    Args:
        series: pandas Series with target values

    Returns:
        Series with bar counts since last change
    """
    changes = series != series.shift(1)
    groups = changes.cumsum()
    return series.groupby(groups).cumcount()


def create_rnn_target_columns(
    df: pd.DataFrame,
    target_column: str,
    prediction_horizon: int
) -> tuple[pd.DataFrame, List[str]]:
    """
    Create multiple shifted target columns for RNN models.

    For RNN models (LSTM/GRU) with output_chunk_length=1, we create
    separate target columns for each prediction step:
    - target_h1: target shifted by 1 bar (predict 1 bar ahead)
    - target_h2: target shifted by 2 bars (predict 2 bars ahead)
    - ...
    - target_hN: target shifted by N bars (predict N bars ahead)

    Args:
        df: DataFrame with the original target column
        target_column: Name of the base target column
        prediction_horizon: Number of bars ahead to predict

    Returns:
        Tuple of (modified DataFrame, list of new target column names)
    """
    result_df = df.copy()
    target_columns = []

    for h in range(1, prediction_horizon + 1):
        new_col = f"{target_column}_h{h}"
        # Shift target by h bars (negative shift = look ahead)
        result_df[new_col] = result_df[target_column].shift(-h)
        target_columns.append(new_col)
        logger.debug(f"Created RNN target column: {new_col} (shift={-h})")

    logger.info(f"Created {len(target_columns)} RNN target columns for horizon {prediction_horizon}")
    return result_df, target_columns


def save_training_datasets(
    task_id: str,
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    combined_df: pd.DataFrame,
    target_column: str,
    feature_columns: List[str],
    prediction_horizon: int
) -> Dict[str, Any]:
    """
    Save training datasets to cache for debugging and download.

    Creates two dataset versions:
    1. RNN datasets (for LSTM/GRU with output_chunk_length=1):
       - Multiple target columns (target_h1, target_h2, ..., target_hN)
       - Each shifted by 1, 2, ..., N bars respectively
    2. Multi-step datasets (for NBEATS/TCN/Transformer with output_chunk_length=horizon):
       - Single target column (no shift)
       - Model predicts next N values naturally

    Returns:
        Dictionary with paths to saved files and target column info
    """
    cache_dir = get_job_cache_dir(task_id)
    saved_files = {}

    try:
        # Create RNN datasets with shifted target columns
        train_rnn, rnn_target_cols = create_rnn_target_columns(train_df, target_column, prediction_horizon)
        test_rnn, _ = create_rnn_target_columns(test_df, target_column, prediction_horizon)
        combined_rnn, _ = create_rnn_target_columns(combined_df, target_column, prediction_horizon)

        # Save RNN train dataset
        train_rnn_path = cache_dir / 'train_rnn.csv'
        train_rnn.to_csv(train_rnn_path, index=False)
        saved_files['train_rnn'] = str(train_rnn_path)
        logger.info(f"Saved RNN train dataset: {train_rnn_path}")

        # Save RNN test dataset
        test_rnn_path = cache_dir / 'test_rnn.csv'
        test_rnn.to_csv(test_rnn_path, index=False)
        saved_files['test_rnn'] = str(test_rnn_path)
        logger.info(f"Saved RNN test dataset: {test_rnn_path}")

        # Save multi-step datasets (original target, no shift)
        # For multi-step models, we use the original target column
        # The model's output_chunk_length handles multi-step prediction
        train_multistep_path = cache_dir / 'train_multistep.csv'
        train_df.to_csv(train_multistep_path, index=False)
        saved_files['train_multistep'] = str(train_multistep_path)
        logger.info(f"Saved multi-step train dataset: {train_multistep_path}")

        test_multistep_path = cache_dir / 'test_multistep.csv'
        test_df.to_csv(test_multistep_path, index=False)
        saved_files['test_multistep'] = str(test_multistep_path)
        logger.info(f"Saved multi-step test dataset: {test_multistep_path}")

        # Save combined dataset (RNN version with all columns for debugging)
        combined_path = cache_dir / 'combined_dataset.csv'
        combined_rnn.to_csv(combined_path, index=False)
        saved_files['combined'] = str(combined_path)
        logger.info(f"Saved combined dataset: {combined_path}")

        # Save metadata
        import json
        metadata = {
            'task_id': task_id,
            'target_column': target_column,
            'rnn_target_columns': rnn_target_cols,
            'feature_columns': feature_columns,
            'prediction_horizon': prediction_horizon,
            'combined_rows': len(combined_df),
            'train_rows': len(train_df),
            'test_rows': len(test_df),
            'created_at': datetime.now().isoformat(),
            'dataset_types': {
                'rnn': {
                    'description': 'For LSTM/GRU models with output_chunk_length=1',
                    'target_columns': rnn_target_cols,
                    'files': ['train_rnn.csv', 'test_rnn.csv']
                },
                'multistep': {
                    'description': 'For NBEATS/TCN/Transformer models with output_chunk_length=horizon',
                    'target_column': target_column,
                    'files': ['train_multistep.csv', 'test_multistep.csv']
                }
            }
        }
        metadata_path = cache_dir / 'metadata.json'
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        saved_files['metadata'] = str(metadata_path)

        logger.info(f"Saved {len(saved_files)} dataset files for job {task_id}")

        # Return info needed for training
        return {
            'saved_files': saved_files,
            'rnn_target_columns': rnn_target_cols,
            'multistep_target_column': target_column
        }

    except Exception as e:
        logger.error(f"Failed to save training datasets: {e}")
        return {'saved_files': {}, 'error': str(e)}


def get_job_datasets(task_id: str) -> Dict[str, Any]:
    """
    Get information about saved datasets for a job.

    Returns:
        Dictionary with dataset info or None if not found
    """
    cache_dir = DATASET_CACHE_DIR / task_id

    if not cache_dir.exists():
        return None

    result = {
        'task_id': task_id,
        'files': []
    }

    # Check for all expected files (both old and new format)
    expected_files = [
        'combined_dataset.csv',
        'train_rnn.csv', 'test_rnn.csv',
        'train_multistep.csv', 'test_multistep.csv',
        # Legacy format
        'train_dataset.csv', 'test_dataset.csv'
    ]

    for filename in expected_files:
        filepath = cache_dir / filename
        if filepath.exists():
            stat = filepath.stat()
            result['files'].append({
                'name': filename,
                'size': stat.st_size,
                'modified': datetime.fromtimestamp(stat.st_mtime).isoformat()
            })

    # Load metadata if available
    metadata_path = cache_dir / 'metadata.json'
    if metadata_path.exists():
        import json
        with open(metadata_path, 'r') as f:
            result['metadata'] = json.load(f)

    return result


def get_dataset_file_path(task_id: str, filename: str) -> Optional[Path]:
    """
    Get the path to a specific dataset file for a job.

    Returns:
        Path to file or None if not found
    """
    cache_dir = DATASET_CACHE_DIR / task_id
    filepath = cache_dir / filename

    if filepath.exists() and filepath.is_file():
        return filepath

    return None

# Check for ML libraries
try:
    from app.services.darts_training import DartsTrainingService, DARTS_AVAILABLE
    from app.services.darts_models import DartsModelService, PredictionTargetService, DatasetSplitter
    from app.services.genetic import GeneticOptimizer, DEAP_AVAILABLE
    # Backwards compatibility aliases
    TrainingService = DartsTrainingService
    MLModelsService = DartsModelService
    ML_AVAILABLE = DARTS_AVAILABLE and DEAP_AVAILABLE
except ImportError as e:
    logger.warning(f"ML libraries not fully available: {e}")
    ML_AVAILABLE = False


def update_job_progress(task_id: str, progress: float, message: str):
    """Update job progress in the task queue and add to logs."""
    from app.services.task_queue import get_task_queue
    try:
        task_queue = get_task_queue()
        task_queue.update_progress(task_id, progress, message)
        logger.info(f"Job {task_id}: {message} ({progress:.1f}%)")
        # Also add to job_progress_data for UI logs
        add_job_log(task_id, message)
    except Exception as e:
        logger.warning(f"Failed to update job progress: {e}")


def add_job_log(task_id: str, message: str):
    """Add a log entry to the job's progress data."""
    from datetime import datetime
    try:
        # Import here to avoid circular imports
        from app.api.jobs import job_progress_data
        if task_id in job_progress_data:
            job_progress_data[task_id]["logs"].append(
                f"[{datetime.now().isoformat()}] {message}"
            )
    except Exception as e:
        # Silently fail if job_progress_data not available
        pass


def update_job_training_state(
    task_id: str,
    current_generation: int = None,
    total_generations: int = None,
    current_individual: int = None,
    population_size: int = None,
    current_model_type: str = None,
    current_epoch: int = None,
    total_epochs: int = None,
    best_fitness: float = None,
    error_count: int = None,
    success_count: int = None,
    current_model_params: Dict[str, Any] = None,
    epoch_metrics: Dict[str, float] = None,
    reset_epoch_history: bool = False
):
    """Update job training state for real-time progress tracking.

    Writes to BOTH the in-memory jobs_store (for same-process mode)
    AND the database checkpoint_data (for subprocess mode).
    """
    # Build state dict — all fields for in-memory, subset for DB
    state_update = {}
    if current_generation is not None: state_update["currentGeneration"] = current_generation
    if total_generations is not None: state_update["totalGenerations"] = total_generations
    if current_individual is not None: state_update["currentIndividual"] = current_individual
    if population_size is not None: state_update["populationSize"] = population_size
    if current_model_type is not None: state_update["currentModelType"] = current_model_type
    if current_epoch is not None: state_update["currentEpoch"] = current_epoch
    if total_epochs is not None: state_update["totalEpochs"] = total_epochs
    if best_fitness is not None: state_update["bestFitness"] = best_fitness
    if error_count is not None: state_update["errorCount"] = error_count
    if success_count is not None: state_update["successCount"] = success_count

    # Write to database (works across processes).
    # Persist on every call — epoch-level updates included for real-time UI.
    if state_update:
        try:
            from app.models.database import SessionLocal
            from app.models.task_queue import TaskQueue
            db = SessionLocal()
            try:
                task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
                if task:
                    # Copy to new dict — SQLAlchemy won't detect in-place
                    # mutations on the same JSON object reference
                    existing = dict(task.checkpoint_data or {})
                    existing.update(state_update)
                    # Also persist epoch history for loss charts
                    if epoch_metrics:
                        history = list(existing.get("epochHistory", []))
                        history.append({
                            "epoch": current_epoch or len(history) + 1,
                            **epoch_metrics
                        })
                        existing["epochHistory"] = history
                    if reset_epoch_history:
                        existing["epochHistory"] = []
                    task.checkpoint_data = existing
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"Failed to persist training state to DB: {e}")

    # Also update in-memory store (for same-process mode / backward compat)
    try:
        from app.api.jobs import jobs_store
        if task_id in jobs_store:
            job = jobs_store[task_id]
            job.update(state_update)
            if current_model_params is not None:
                job["currentModelParams"] = current_model_params
            # Reset epoch history when starting a new individual/model
            if reset_epoch_history:
                job["epochHistory"] = []
            if epoch_metrics:  # Check not None AND not empty dict
                # Append to epoch history for graphing
                if "epochHistory" not in job:
                    job["epochHistory"] = []
                epoch_entry = {
                    "epoch": current_epoch or len(job["epochHistory"]) + 1,
                    **epoch_metrics  # Include all metrics (train_loss, val_loss, etc.)
                }
                job["epochHistory"].append(epoch_entry)
                # Keep only last 100 epochs to prevent memory bloat
                if len(job["epochHistory"]) > 100:
                    job["epochHistory"] = job["epochHistory"][-100:]
    except Exception as e:
        logger.warning(f"Failed to update job training state: {e}")


def get_epoch_history(task_id: str) -> List[Dict[str, Any]]:
    """Get the current epoch history for an individual (before it's reset)."""
    try:
        from app.api.jobs import jobs_store
        if task_id in jobs_store:
            job = jobs_store[task_id]
            # Return a copy of the epoch history
            return list(job.get("epochHistory", []))
    except Exception as e:
        logger.warning(f"Failed to get epoch history: {e}")
    return []


def add_individual_to_job(task_id: str, individual_record: Dict[str, Any]):
    """Add an evaluated individual to the job store for real-time UI access."""
    # Persist to DB (for subprocess mode)
    try:
        from app.models.database import SessionLocal
        from app.models.task_queue import TaskQueue
        db = SessionLocal()
        try:
            task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if task:
                existing = dict(task.checkpoint_data or {})
                individuals = list(existing.get("allIndividuals", []))
                individuals.append(individual_record)
                existing["allIndividuals"] = individuals
                existing["individualsCount"] = len(individuals)
                task.checkpoint_data = existing
                db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"Failed to persist individual to DB: {e}")

    # Also update in-memory store
    try:
        from app.api.jobs import jobs_store
        if task_id in jobs_store:
            job = jobs_store[task_id]
            if "allIndividuals" not in job:
                job["allIndividuals"] = []
            job["allIndividuals"].append(individual_record)
            job["individualsCount"] = len(job["allIndividuals"])
    except Exception as e:
        logger.warning(f"Failed to add individual to job store: {e}")


def save_ga_checkpoint(task_id: str, checkpoint_data: Dict[str, Any]):
    """Save genetic algorithm checkpoint to database for crash recovery."""
    from app.models.task_queue import TaskQueue
    db = SessionLocal()
    try:
        task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        if task:
            task.checkpoint_data = checkpoint_data
            db.commit()
            logger.debug(f"Saved GA checkpoint for task {task_id}, gen {checkpoint_data.get('generation', 0)}")
    except Exception as e:
        logger.error(f"Failed to save GA checkpoint: {e}", exc_info=True)
        db.rollback()
    finally:
        db.close()


def load_ga_checkpoint(task_id: str) -> Optional[Dict[str, Any]]:
    """Load genetic algorithm checkpoint from database."""
    from app.models.task_queue import TaskQueue
    db = SessionLocal()
    try:
        task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        if task and task.checkpoint_data:
            logger.info(f"Found GA checkpoint for task {task_id}, gen {task.checkpoint_data.get('generation', 0)}")
            return task.checkpoint_data
        return None
    except Exception as e:
        logger.error(f"Failed to load GA checkpoint: {e}", exc_info=True)
        return None
    finally:
        db.close()


def clear_ga_checkpoint(task_id: str):
    """Clear checkpoint data after successful completion."""
    from app.models.task_queue import TaskQueue
    db = SessionLocal()
    try:
        task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        if task:
            task.checkpoint_data = None
            db.commit()
    except Exception as e:
        logger.error(f"Failed to clear GA checkpoint: {e}")
    finally:
        db.close()


def get_job_models_dir(task_id: str) -> Path:
    """Get the directory for storing job models (test bucket, app.paths)."""
    from app.paths import MODELS_DIR
    models_dir = MODELS_DIR / task_id
    models_dir.mkdir(parents=True, exist_ok=True)
    return models_dir


def save_generation_model(
    task_id: str,
    model: Any,
    generation: int,
    individual: int,
    model_type: str,
    fitness: float,
    params: Dict[str, Any],
    metrics: Dict[str, Any],
    training_service: Any,
    training_history: Optional[List[Dict[str, Any]]] = None,
    feature_columns: Optional[List[str]] = None,
    symbols: Optional[List[str]] = None,
    dataset_ids: Optional[List[int]] = None
) -> Optional[str]:
    """
    Save a model from a generation.

    Args:
        task_id: Job ID
        model: Trained model
        generation: Generation number
        individual: Individual number within generation
        model_type: Type of model (lstm, nbeats, etc.)
        fitness: Model fitness score
        params: Model parameters
        metrics: Evaluation metrics
        training_service: TrainingService instance for saving
        training_history: Epoch-by-epoch training history for visualization
        feature_columns: List of feature column names used during training

    Returns:
        Path to saved model or None if failed
    """
    try:
        models_dir = get_job_models_dir(task_id)
        model_filename = f"gen{generation:03d}_ind{individual:03d}_{model_type}_f{fitness:.4f}"
        full_path = models_dir / f"{model_filename}.pt"

        # Get normalization params from training service if available
        normalization_params = None
        if hasattr(training_service, 'get_normalization_params'):
            normalization_params = training_service.get_normalization_params()
        elif hasattr(training_service, 'data_prep') and training_service.data_prep:
            normalization_params = training_service.data_prep.export_params()

        metadata = {
            'task_id': task_id,
            'generation': generation,
            'individual': individual,
            'model_type': model_type,
            'fitness': fitness,
            'params': params,
            'metrics': metrics,
            'normalization_params': normalization_params,  # For inference consistency
            'training_history': training_history or [],  # Epoch-by-epoch history for visualization
            'feature_columns': feature_columns or [],  # Feature columns used during training
            'symbols': symbols or [],  # Symbols used during training
            'dataset_ids': dataset_ids or [],  # Dataset IDs used during training
        }

        # Note: Callbacks are now serializable (EpochProgressCallback implements
        # __getstate__/__setstate__), so no need to clear them before saving.

        # Save model directly to the job models directory
        model.save(str(full_path))
        logger.debug(f"Saved model: {full_path}")

        # Save metadata
        meta_path = models_dir / f"{model_filename}_meta.json"
        import json
        with open(meta_path, 'w') as f:
            json.dump(metadata, f, indent=2, default=str)

        model_path = str(full_path)
        logger.debug(f"Saved model: {model_path}")
        return model_path

    except Exception as e:
        logger.error(f"Failed to save generation model: {e}", exc_info=True)
        return None


def cleanup_generation_models(task_id: str, generation: int):
    """
    Remove all models from a specific generation.

    Args:
        task_id: Job ID
        generation: Generation number to clean up
    """
    try:
        models_dir = get_job_models_dir(task_id)
        pattern = f"gen{generation:03d}_*"

        import glob
        files_to_remove = list(models_dir.glob(pattern))

        for file_path in files_to_remove:
            try:
                file_path.unlink()
                logger.debug(f"Removed: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to remove {file_path}: {e}")

        if files_to_remove:
            logger.info(f"Cleaned up {len(files_to_remove)} files from generation {generation}")

    except Exception as e:
        logger.warning(f"Failed to cleanup generation {generation} models: {e}")


def cleanup_non_elite_models(
    task_id: str,
    all_individuals: List[Dict[str, Any]],
    elitism_percent: float,
    population_size: int
):
    """
    Remove all model files that are not in the elite set.

    Called after each generation to prevent disk from filling up.
    Keeps only the top N models based on fitness.

    Args:
        task_id: Job ID
        all_individuals: List of all evaluated individuals with their info
        elitism_percent: Percentage of population to keep as elite
        population_size: Size of population for calculating elite count
    """
    import re

    try:
        models_dir = get_job_models_dir(task_id)

        # Calculate number of elite models to keep
        elite_count = max(1, int((elitism_percent / 100.0) * population_size))
        # Keep at least 10 models for final selection
        elite_count = max(elite_count, 10)

        # Sort individuals by fitness (descending) and get elite set
        sorted_individuals = sorted(
            all_individuals,
            key=lambda x: x.get('fitness', 0),
            reverse=True
        )
        elite_individuals = sorted_individuals[:elite_count]

        # Build set of elite model keys (gen, individual, model_type) to keep
        elite_keys = set()
        for ind in elite_individuals:
            gen = ind.get('generation', 0)
            individual_num = ind.get('individual', 0)
            model_type = ind.get('model_type', 'unknown')
            elite_keys.add((gen, individual_num, model_type))

        # Find all model files and delete non-elite ones
        # Pattern: gen{gen:03d}_ind{ind:03d}_{model_type}_f{fitness:.4f}.pt or _meta.json
        all_model_files = list(models_dir.glob("gen*_ind*_*"))
        deleted_count = 0

        # Regex to parse filename: gen000_ind001_lstm_f0.1234.pt
        pattern = re.compile(r'^gen(\d{3})_ind(\d{3})_([^_]+)_f[\d.]+')

        for file_path in all_model_files:
            filename = file_path.stem
            if filename.endswith('_meta'):
                filename = filename[:-5]  # Remove _meta suffix

            match = pattern.match(filename)
            if not match:
                continue  # Skip files that don't match pattern

            file_gen = int(match.group(1))
            file_ind = int(match.group(2))
            file_model_type = match.group(3)

            file_key = (file_gen, file_ind, file_model_type)

            if file_key not in elite_keys:
                try:
                    file_path.unlink()
                    deleted_count += 1
                except Exception as e:
                    logger.warning(f"Failed to delete {file_path}: {e}")

        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} non-elite model files, kept {len(elite_keys)} elite models")

    except Exception as e:
        logger.warning(f"Failed to cleanup non-elite models: {e}", exc_info=True)


def save_best_model(
    task_id: str,
    model: Any,
    model_type: str,
    fitness: float,
    params: Dict[str, Any],
    metrics: Dict[str, Any],
    training_service: Any
) -> Optional[str]:
    """
    Save the best model to a permanent location.

    Args:
        task_id: Job ID
        model: Best trained model
        model_type: Type of model
        fitness: Model fitness score
        params: Model parameters
        metrics: Evaluation metrics
        training_service: TrainingService instance

    Returns:
        Path to saved model or None if failed
    """
    try:
        models_dir = get_job_models_dir(task_id)
        model_name = f"best_{model_type}_f{fitness:.4f}"

        # Get normalization params from training service if available
        normalization_params = None
        if hasattr(training_service, 'get_normalization_params'):
            normalization_params = training_service.get_normalization_params()
        elif hasattr(training_service, 'data_prep') and training_service.data_prep:
            normalization_params = training_service.data_prep.export_params()

        metadata = {
            'task_id': task_id,
            'model_type': model_type,
            'fitness': fitness,
            'params': params,
            'metrics': metrics,
            'is_best': True,
            'normalization_params': normalization_params  # For inference consistency
        }

        model_path = training_service.save_model(model, str(models_dir / model_name), metadata)
        logger.info(f"Saved best model: {model_path}")
        return model_path

    except Exception as e:
        logger.error(f"Failed to save best model: {e}", exc_info=True)
        return None


def cleanup_job_models(task_id: str, keep_best: bool = True):
    """
    Clean up all models for a job, optionally keeping the best model.

    Args:
        task_id: Job ID
        keep_best: If True, keep files starting with 'best_' or 'elite_'
    """
    try:
        models_dir = get_job_models_dir(task_id)

        for file_path in models_dir.iterdir():
            if keep_best and (file_path.name.startswith('best_') or file_path.name.startswith('elite_')):
                continue
            try:
                file_path.unlink()
            except Exception as e:
                logger.warning(f"Failed to remove {file_path}: {e}")

        logger.info(f"Cleaned up job {task_id} models (keep_best={keep_best})")

    except Exception as e:
        logger.warning(f"Failed to cleanup job models: {e}")


def save_elite_models(
    task_id: str,
    all_individuals: List[Dict[str, Any]],
    elitism_percent: float = 10.0,
    population_size: int = 20,
    default_elite_count: int = 10
) -> List[str]:
    """
    Rename/mark the top N models as elite models to preserve them.

    The number of elite models is determined by:
    - If elitism_percent > 0: (elitism_percent / 100) * population_size
    - Otherwise: default_elite_count (default 10)

    Args:
        task_id: Job ID
        all_individuals: List of all evaluated individuals with their info
        elitism_percent: Percentage of population to keep as elite
        population_size: Size of population for calculating elite count
        default_elite_count: Default number of models to keep if no elitism

    Returns:
        List of paths to elite models
    """
    try:
        models_dir = get_job_models_dir(task_id)

        # Calculate number of elite models to keep
        if elitism_percent > 0:
            elite_count = max(1, int((elitism_percent / 100.0) * population_size))
        else:
            elite_count = default_elite_count

        # Sort individuals by fitness (descending) and get top N
        sorted_individuals = sorted(
            all_individuals,
            key=lambda x: x.get('fitness', 0),
            reverse=True
        )
        elite_individuals = sorted_individuals[:elite_count]

        logger.info(f"Saving {len(elite_individuals)} elite models (elitism={elitism_percent}%, pop={population_size})")

        elite_paths = []
        for rank, ind in enumerate(elite_individuals, 1):
            gen = ind.get('generation', 0)
            individual_num = ind.get('individual', 0)
            model_type = ind.get('model_type', 'unknown')
            fitness = ind.get('fitness', 0)

            # Find the original model file
            original_pattern = f"gen{gen:03d}_ind{individual_num:03d}_{model_type}_*"
            matching_files = list(models_dir.glob(original_pattern))

            if not matching_files:
                logger.warning(f"Could not find model for gen{gen}_ind{individual_num}_{model_type}")
                continue

            for original_path in matching_files:
                # Create new elite filename with rank
                suffix = original_path.suffix
                if suffix == '.json':
                    new_name = f"elite_{rank:02d}_{model_type}_f{fitness:.4f}_meta.json"
                else:
                    new_name = f"elite_{rank:02d}_{model_type}_f{fitness:.4f}{suffix}"

                new_path = models_dir / new_name

                try:
                    # Rename to elite
                    original_path.rename(new_path)
                    if not suffix == '.json':
                        elite_paths.append(str(new_path))
                    logger.debug(f"Renamed {original_path.name} -> {new_name}")
                except Exception as e:
                    logger.warning(f"Failed to rename {original_path}: {e}")

        logger.info(f"Saved {len(elite_paths)} elite models")
        return elite_paths

    except Exception as e:
        logger.error(f"Failed to save elite models: {e}", exc_info=True)
        return []


def get_elite_models(task_id: str) -> List[Dict[str, Any]]:
    """
    Get list of elite models saved for a job.

    Args:
        task_id: Job ID

    Returns:
        List of elite model info dicts with rank, model_type, fitness, file_path, metrics
    """
    import json

    try:
        models_dir = get_job_models_dir(task_id)
        if not models_dir.exists():
            return []

        elite_models = []

        # Find elite model files (not metadata)
        for model_file in sorted(models_dir.glob("elite_*")):
            if model_file.suffix == '.json':
                continue  # Skip metadata files

            # Parse filename: elite_{rank}_{model_type}_f{fitness}.pkl or .pt
            name_parts = model_file.stem.split('_')
            if len(name_parts) >= 4:
                rank = int(name_parts[1])
                model_type = name_parts[2]
                fitness_str = name_parts[3]
                if fitness_str.startswith('f'):
                    fitness = float(fitness_str[1:])
                else:
                    fitness = 0.0

                # Try to load metadata file
                # First try direct path: elite_01_lstm_f0.9091_meta.json
                meta_file = model_file.parent / f"{model_file.stem}_meta.json"
                # Also try the pattern: elite_01_lstm_f*_meta.json (for when fitness precision differs)
                meta_pattern = f"elite_{name_parts[1]}_{model_type}_f*_meta.json"
                meta_files = list(model_file.parent.glob(meta_pattern))

                # Prioritize direct path if it exists
                if meta_file.exists() and meta_file not in meta_files:
                    meta_files.insert(0, meta_file)

                metrics = {}
                params = {}
                generation = None
                individual = None
                c_in = None
                c_out = None
                seq_len = None
                prediction_mode = None
                loss_function = None
                threshold = None
                feature_columns = None
                training_history = []
                normalization_params = None
                if meta_files:
                    try:
                        with open(meta_files[0], 'r') as f:
                            meta = json.load(f)
                            metrics = meta.get('metrics', {})
                            params = meta.get('params', {})
                            generation = meta.get('generation')
                            individual = meta.get('individual')
                            # Critical for model loading
                            c_in = meta.get('c_in')
                            c_out = meta.get('c_out')
                            seq_len = meta.get('seq_len')
                            prediction_mode = meta.get('prediction_mode')
                            loss_function = meta.get('loss_function')
                            threshold = meta.get('threshold')
                            feature_columns = meta.get('feature_columns')
                            training_history = meta.get('training_history', [])
                            # Critical for forward testing - normalization params
                            normalization_params = meta.get('normalization_params')
                    except Exception:
                        pass

                elite_models.append({
                    'rank': rank,
                    'model_type': model_type,
                    'fitness': fitness,
                    'file_path': str(model_file),
                    'file_name': model_file.name,
                    'metrics': metrics,
                    'params': params,
                    'generation': generation,
                    'individual': individual,
                    'c_in': c_in,
                    'c_out': c_out,
                    'seq_len': seq_len,
                    'prediction_mode': prediction_mode,
                    'loss_function': loss_function,
                    'threshold': threshold,
                    'feature_columns': feature_columns,
                    'training_history': training_history,
                    'normalization_params': normalization_params
                })

        return sorted(elite_models, key=lambda x: x['rank'])

    except Exception as e:
        logger.error(f"Failed to get elite models: {e}", exc_info=True)
        return []


def load_dataset(
    dataset_id: int,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> Optional[pd.DataFrame]:
    """
    Load dataset from file, optionally filtered by date range.

    Args:
        dataset_id: Dataset ID to load
        start_date: Optional start date filter (YYYY-MM-DD)
        end_date: Optional end date filter (YYYY-MM-DD)

    Returns:
        DataFrame with dataset data, optionally filtered by date
    """
    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            logger.error(f"Dataset {dataset_id} not found")
            return None

        if not dataset.file_path:
            logger.error(f"Dataset {dataset_id} has no file path")
            return None

        file_path = Path(dataset.file_path)
        if not file_path.exists():
            logger.error(f"Dataset file not found: {file_path}")
            return None

        df = pd.read_csv(file_path)
        original_rows = len(df)
        logger.info(f"Loaded dataset {dataset_id}: {original_rows} rows, {len(df.columns)} columns")

        # Forward-fill sparse indicators (e.g., zigzag) for training
        # These have NaN between pivots which can cause training issues
        df = ffill_sparse_indicators(df)

        # Apply date range filter if specified
        if start_date or end_date:
            # Find date column
            date_col = None
            for col in ['Date', 'date', 'datetime', 'Datetime', 'timestamp', 'Timestamp']:
                if col in df.columns:
                    date_col = col
                    break

            if date_col:
                # Parse dates
                df[date_col] = pd.to_datetime(df[date_col])

                if start_date:
                    start_dt = pd.to_datetime(start_date)
                    df = df[df[date_col] >= start_dt]
                    logger.info(f"Applied start date filter: {start_date}")

                if end_date:
                    end_dt = pd.to_datetime(end_date)
                    df = df[df[date_col] <= end_dt]
                    logger.info(f"Applied end date filter: {end_date}")

                logger.info(f"Date range filter: {original_rows} -> {len(df)} rows")
            else:
                logger.warning(f"No date column found in dataset {dataset_id}, cannot filter by date")

        return df

    finally:
        db.close()


def get_dataset_info(dataset_id: int) -> Dict[str, Any]:
    """Get dataset metadata including timeframe."""
    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if dataset:
            return {
                'id': dataset.id,
                'name': dataset.name,
                'ticker': dataset.ticker,
                'timeframe': dataset.timeframe or 'daily'
            }
        return {'timeframe': 'daily'}
    finally:
        db.close()


def load_datasets_separate(
    dataset_ids: list,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> Tuple[List[pd.DataFrame], List[Dict[str, Any]]]:
    """Load multiple datasets as separate DataFrames (not concatenated).

    Args:
        dataset_ids: List of dataset IDs to load
        start_date: Optional start date filter
        end_date: Optional end date filter

    Returns:
        Tuple of (list of DataFrames, list of dataset info dicts)

    Raises:
        ValueError: If any dataset fails to load
    """
    dataframes = []
    infos = []
    for ds_id in dataset_ids:
        df = load_dataset(ds_id, start_date=start_date, end_date=end_date)
        if df is None:
            raise ValueError(f"Failed to load dataset {ds_id}")
        info = get_dataset_info(ds_id)
        dataframes.append(df)
        infos.append(info)
        logger.info(f"Loaded dataset {ds_id} ({info.get('ticker', '?')}): {len(df)} rows")
    return dataframes, infos


def handle_training_job(task_id: str, payload: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """
    Handle ML training job.

    This is the main entry point for training jobs, called by TaskQueueService.

    Args:
        task_id: Unique task identifier
        payload: Job configuration containing:
            - dataset_ids: List of dataset IDs to train on
            - selected_models: Model types to train (lstm, nbeats, rnn)
            - parameter_ranges: Hyperparameter search ranges
            - prediction_targets: Target configurations
            - train_test_split: Train/test split percentage
            - genetic_config: Genetic algorithm configuration
            - metrics_config: Metrics configuration
        dry_run: If True, only validate config and prepare data without training

    Returns:
        Result dictionary with trained model info and metrics
    """
    # Dump full payload for debugging
    import json
    logger.info(f"=== JOB RECEIVED: {task_id} ===")
    logger.info(f"Full payload:\n{json.dumps(payload, indent=2, default=str)}")

    if not ML_AVAILABLE:
        return {
            'status': 'failed',
            'error': 'ML libraries not available. Install darts and deap.'
        }

    try:
        update_job_progress(task_id, 0, "Starting training job...")

        # Extract configuration - NO DEFAULTS for job config, fail early if missing
        dataset_ids = payload.get('dataset_ids', [])
        if not dataset_ids and payload.get('dataset_id'):
            dataset_ids = [payload['dataset_id']]

        if not dataset_ids:
            return {'status': 'failed', 'error': 'No datasets specified'}

        # Required config - fail early if missing
        job_type = payload.get('job_type')
        if not job_type:
            return {'status': 'failed', 'error': 'job_type is required (classification or regression)'}

        selected_models = payload.get('selected_models')
        if not selected_models:
            return {'status': 'failed', 'error': 'selected_models is required'}

        train_test_split = payload.get('train_test_split')
        if train_test_split is None:
            return {'status': 'failed', 'error': 'train_test_split is required'}

        prediction_horizon = payload.get('prediction_horizon')
        if prediction_horizon is None:
            return {'status': 'failed', 'error': 'prediction_horizon is required'}

        prediction_modes = payload.get('prediction_modes')
        if not prediction_modes:
            return {'status': 'failed', 'error': 'prediction_modes is required'}

        genetic_config = payload.get('genetic_config')
        if not genetic_config:
            return {'status': 'failed', 'error': 'genetic_config is required'}

        metrics_config = payload.get('metrics_config')
        if not metrics_config:
            return {'status': 'failed', 'error': 'metrics_config is required'}

        parameter_ranges = payload.get('parameter_ranges', {})
        prediction_targets = payload.get('prediction_targets', [])
        training_date_range = payload.get('training_date_range', {})

        # Extract date range for subset training
        train_start_date = training_date_range.get('startDate') if training_date_range else None
        train_end_date = training_date_range.get('endDate') if training_date_range else None

        # Load and combine datasets
        update_job_progress(task_id, 5, "Loading datasets...")
        combined_df = None
        dataset_infos = []

        for ds_id in dataset_ids:
            df = load_dataset(ds_id, start_date=train_start_date, end_date=train_end_date)
            if df is None:
                return {'status': 'failed', 'error': f'Failed to load dataset {ds_id}'}

            info = get_dataset_info(ds_id)
            dataset_infos.append(info)

            # Add ticker column if combining multiple datasets
            if len(dataset_ids) > 1:
                df['ticker'] = info.get('ticker', f'TICKER{ds_id}')

            if combined_df is None:
                combined_df = df
            else:
                combined_df = pd.concat([combined_df, df], ignore_index=True)

        # Sort by date
        combined_df = combined_df.sort_values('Date').reset_index(drop=True)
        update_job_progress(task_id, 10, f"Loaded {len(combined_df)} rows from {len(dataset_ids)} dataset(s)")

        # Get dataset timeframe for multi-timeframe target support
        dataset_timeframe = dataset_infos[0].get('timeframe', '1h') if dataset_infos else '1h'
        logger.info(f"Dataset timeframe: {dataset_timeframe}")

        # Calculate prediction targets
        if prediction_targets:
            update_job_progress(task_id, 15, "Calculating prediction targets...")

            # Check target type - new format vs legacy format
            first_target = prediction_targets[0] if prediction_targets else {}
            target_type = first_target.get('type')

            if target_type:
                # New target format (trend_reversal, directional, price_based, triple_barrier, volatility)
                # Use PredictionTargetService
                from app.services.darts_models import days_to_bars
                target_service = PredictionTargetService()

                target_column = None
                all_target_columns = []  # Collect all generated target column names for model metadata
                for pt in prediction_targets:
                    pt_type = pt.get('type')
                    # Support both old format (config) and new format (indicatorParams)
                    pt_config = pt.get('config') or pt.get('indicatorParams') or {}
                    direction = pt.get('direction', 'bullish')
                    threshold = pt.get('threshold', 30)
                    indicator = pt.get('indicator', 'zigzag')

                    # Check for multi-timeframe target
                    target_timeframe = pt.get('timeframe')
                    use_multi_tf = target_timeframe and target_timeframe != dataset_timeframe

                    # Prepare working_df for multi-timeframe (if applicable)
                    working_df = combined_df
                    if use_multi_tf:
                        try:
                            from app.services.indicators import resample_ohlcv_to_timeframe, align_higher_timeframe_to_lower
                            working_df = resample_ohlcv_to_timeframe(
                                combined_df, target_timeframe, source_timeframe=dataset_timeframe
                            )
                            logger.info(f"Resampled to {target_timeframe} for target: {len(combined_df)} -> {len(working_df)} bars")
                        except ValueError as e:
                            logger.warning(f"Cannot resample to {target_timeframe}: {e}. Using base timeframe.")
                            use_multi_tf = False
                            working_df = combined_df

                    if pt_type == 'trend_reversal':
                        # Use calculate_trend_reversal from PredictionTargetService
                        col_name = f"{indicator}_{direction}_reversal"
                        if use_multi_tf:
                            col_name = f"{col_name}_{target_timeframe}"
                        try:
                            target_series = target_service.calculate_trend_reversal(
                                working_df,
                                indicator=indicator,
                                indicator_params=pt_config,
                                threshold=threshold,
                                direction=direction
                            )

                            # Align back to original timeframe if multi-timeframe
                            if use_multi_tf:
                                higher_tf_data = working_df[['Date']].copy()
                                higher_tf_data['_target'] = target_series.values
                                aligned = align_higher_timeframe_to_lower(combined_df, higher_tf_data, ['_target'])
                                target_series = pd.Series(aligned['_target'].values, index=combined_df.index)
                                logger.info(f"Aligned {col_name} from {target_timeframe} to {dataset_timeframe}")

                            combined_df[col_name] = target_series
                            all_target_columns.append(col_name)
                            if target_column is None:
                                target_column = col_name
                            logger.info(f"Created trend reversal target: {col_name}, positives: {int(target_series.sum())}")
                        except Exception as e:
                            return {'status': 'failed', 'error': f'Failed to calculate trend_reversal target: {e}'}

                    elif pt_type == 'directional':
                        # Simple directional target
                        horizon = pt.get('horizon') or pt_config.get('horizon')
                        horizon_unit = pt.get('horizonUnit', 'bars')
                        if horizon is None:
                            return {'status': 'failed', 'error': f'directional target requires horizon. Got: {pt}'}
                        # Convert days to bars if needed
                        if horizon_unit == 'days':
                            horizon = days_to_bars(horizon, dataset_timeframe)
                        dir_label = 'up' if direction in ['up', 'bullish'] else 'down'
                        col_name = f"direction_{dir_label}_{horizon}bar"
                        if use_multi_tf:
                            col_name = f"{col_name}_{target_timeframe}"
                        # Directional is calculated on base timeframe (uses shift), no resampling needed
                        if direction in ['up', 'bullish']:
                            combined_df[col_name] = (combined_df['Close'].shift(-horizon) > combined_df['Close']).astype(int)
                        else:
                            combined_df[col_name] = (combined_df['Close'].shift(-horizon) < combined_df['Close']).astype(int)
                        all_target_columns.append(col_name)
                        if target_column is None:
                            target_column = col_name
                        logger.info(f"Created directional target: {col_name}")

                    elif pt_type == 'price_based':
                        # Price-based target with profit target, max drawdown, and time window
                        profit_pct = pt.get('profitPct')
                        max_dd_pct = pt.get('maxDrawdownPct')
                        time_bars = pt.get('timeBars')
                        time_unit = pt.get('timeBarsUnit', 'bars')
                        dir_label = pt.get('direction', 'up')

                        if profit_pct is None or max_dd_pct is None or time_bars is None:
                            return {'status': 'failed', 'error': f'price_based target requires profitPct, maxDrawdownPct, and timeBars. Got: {pt}'}

                        # Convert days to bars if needed
                        if time_unit == 'days':
                            time_bars = days_to_bars(time_bars, dataset_timeframe)

                        col_name = f"price_{dir_label}_{profit_pct}pct_{max_dd_pct}dd_{time_bars}b"
                        try:
                            target_series = target_service.calculate_prediction_targets(
                                combined_df,
                                [{
                                    'profit_pct': profit_pct,
                                    'max_dd': max_dd_pct,
                                    'days': time_bars,  # Now in bars
                                    'direction': dir_label
                                }]
                            )
                            # The service adds columns directly, extract the one we need
                            expected_col = f"price_{dir_label}_{profit_pct}pct_{max_dd_pct}dd_{time_bars}d"
                            if expected_col in target_series.columns:
                                combined_df[col_name] = target_series[expected_col]
                            else:
                                # Find the matching column
                                for c in target_series.columns:
                                    if c.startswith(f"price_{dir_label}"):
                                        combined_df[col_name] = target_series[c]
                                        break
                            all_target_columns.append(col_name)
                            if target_column is None:
                                target_column = col_name
                            positive_count = int(combined_df[col_name].sum()) if col_name in combined_df.columns else 0
                            logger.info(f"Created price_based target: {col_name}, positives: {positive_count}")
                        except Exception as e:
                            return {'status': 'failed', 'error': f'Failed to calculate price_based target: {e}'}

                    elif pt_type == 'triple_barrier':
                        # Triple barrier target: profit, stop, timeout
                        profit_pct = pt.get('profitPct')
                        stop_pct = pt.get('stopPct')
                        max_bars = pt.get('maxBars')
                        max_bars_unit = pt.get('maxBarsUnit', 'bars')

                        if profit_pct is None or stop_pct is None or max_bars is None:
                            return {'status': 'failed', 'error': f'triple_barrier target requires profitPct, stopPct, and maxBars. Got: {pt}'}

                        # Convert days to bars if needed
                        if max_bars_unit == 'days':
                            max_bars = days_to_bars(max_bars, dataset_timeframe)

                        col_name = f"triple_barrier_{profit_pct}p_{stop_pct}s_{max_bars}b"
                        try:
                            # Calculate triple barrier labels: 0=stop, 1=timeout, 2=profit
                            labels = []
                            close_prices = combined_df['Close'].values
                            for i in range(len(close_prices)):
                                if i + max_bars >= len(close_prices):
                                    labels.append(np.nan)
                                    continue
                                entry_price = close_prices[i]
                                profit_level = entry_price * (1 + profit_pct / 100)
                                stop_level = entry_price * (1 - stop_pct / 100)
                                label = 1  # Default: timeout
                                for j in range(1, max_bars + 1):
                                    future_price = close_prices[i + j]
                                    if future_price >= profit_level:
                                        label = 2  # Profit hit
                                        break
                                    elif future_price <= stop_level:
                                        label = 0  # Stop hit
                                        break
                                labels.append(label)
                            combined_df[col_name] = labels
                            all_target_columns.append(col_name)
                            if target_column is None:
                                target_column = col_name
                            logger.info(f"Created triple_barrier target: {col_name}")
                        except Exception as e:
                            return {'status': 'failed', 'error': f'Failed to calculate triple_barrier target: {e}'}

                    elif pt_type == 'volatility':
                        # Volatility regression target
                        horizon = pt.get('horizon', 5)
                        horizon_unit = pt.get('horizonUnit', 'bars')
                        method = pt.get('method', 'std')

                        # Convert days to bars if needed
                        if horizon_unit == 'days':
                            horizon = days_to_bars(horizon, dataset_timeframe)

                        col_name = f"volatility_{method}_{horizon}b"
                        try:
                            if method == 'std':
                                combined_df[col_name] = combined_df['Close'].pct_change().rolling(horizon).std().shift(-horizon)
                            elif method == 'range':
                                combined_df[col_name] = ((combined_df['High'].rolling(horizon).max() - combined_df['Low'].rolling(horizon).min()) / combined_df['Close']).shift(-horizon)
                            elif method == 'atr':
                                tr = np.maximum(
                                    combined_df['High'] - combined_df['Low'],
                                    np.maximum(
                                        abs(combined_df['High'] - combined_df['Close'].shift(1)),
                                        abs(combined_df['Low'] - combined_df['Close'].shift(1))
                                    )
                                )
                                combined_df[col_name] = tr.rolling(horizon).mean().shift(-horizon)
                            else:
                                return {'status': 'failed', 'error': f'Unknown volatility method: {method}'}
                            all_target_columns.append(col_name)
                            if target_column is None:
                                target_column = col_name
                            logger.info(f"Created volatility target: {col_name}")
                        except Exception as e:
                            return {'status': 'failed', 'error': f'Failed to calculate volatility target: {e}'}

                    else:
                        return {'status': 'failed', 'error': f'Unknown target type: {pt_type}'}

                if target_column is None:
                    return {'status': 'failed', 'error': 'No valid target was created from prediction_targets'}

                logger.info(f"Created {len(all_target_columns)} target columns: {all_target_columns}")

            else:
                all_target_columns = []  # For legacy format
                # Legacy price-based format
                target_service = PredictionTargetService()

                targets = []
                for pt in prediction_targets:
                    profit_pct = pt.get('profitPercent')
                    max_dd = pt.get('maxDrawdownPercent')
                    days = pt.get('timePeriodDays')

                    # All values are required for price-based targets
                    if profit_pct is None or max_dd is None or days is None:
                        return {'status': 'failed', 'error': f'Price-based target requires profitPercent, maxDrawdownPercent, and timePeriodDays. Got: {pt}'}

                    targets.append({
                        'profit_pct': profit_pct,
                        'max_dd': max_dd,
                        'days': days,
                        'direction': 'up'
                    })
                    # Add symmetric down target
                    targets.append({
                        'profit_pct': profit_pct,
                        'max_dd': max_dd,
                        'days': days,
                        'direction': 'down'
                    })

                if targets:
                    combined_df = target_service.calculate_prediction_targets(combined_df, targets)
                    target_column = f"price_up_{targets[0]['profit_pct']}pct_{targets[0]['max_dd']}dd_{targets[0]['days']}d"
                else:
                    return {'status': 'failed', 'error': 'No valid price-based targets configured'}
        else:
            # Default: use Close for regression
            target_column = 'Close'

        # Train/test split
        update_job_progress(task_id, 20, "Splitting train/test data...")
        train_ratio = train_test_split / 100.0
        train_df, test_df = DatasetSplitter.train_test_split(combined_df, train_ratio=train_ratio)

        logger.info(f"Train: {len(train_df)} rows, Test: {len(test_df)} rows")

        # Calculate target distribution for both train and test sets
        train_positives = 0
        test_positives = 0
        train_positives_pct = 0.0
        test_positives_pct = 0.0

        # Validate classification targets have samples in both sets
        if target_column.startswith('price_'):
            train_positives = int((train_df[target_column] == 1).sum())
            test_positives = int((test_df[target_column] == 1).sum())
            train_positives_pct = 100 * train_positives / len(train_df) if len(train_df) > 0 else 0
            test_positives_pct = 100 * test_positives / len(test_df) if len(test_df) > 0 else 0

            logger.info(f"Target '{target_column}': train={train_positives} ({train_positives_pct:.1f}%), "
                       f"test={test_positives} ({test_positives_pct:.1f}%) positive samples")

            if train_positives == 0:
                logger.warning(f"WARNING: No positive samples in training set for {target_column}!")
                logger.warning("Model cannot learn to predict positive cases. Consider less strict target criteria.")

            if test_positives == 0:
                logger.warning(f"WARNING: No positive samples in test set for {target_column}!")
                logger.warning("F1/precision/recall will be 0 since there are no positives to evaluate.")

        # Get feature columns (exclude Date, OHLCV, and target columns)
        exclude_cols = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'ticker']
        target_cols = [c for c in combined_df.columns if c.startswith('price_')]
        exclude_cols.extend(target_cols)
        feature_columns = [c for c in combined_df.columns if c not in exclude_cols]

        # Save datasets for debugging and download
        update_job_progress(task_id, 22, "Saving datasets to cache...")
        save_training_datasets(
            task_id=task_id,
            train_df=train_df,
            test_df=test_df,
            combined_df=combined_df,
            target_column=target_column,
            feature_columns=feature_columns,
            prediction_horizon=prediction_horizon
        )

        # Update dataset statistics (persisted to DB for subprocess mode)
        dataset_stats = {
            "trainRows": len(train_df),
            "testRows": len(test_df),
            "targetColumn": target_column,
            "trainPositives": train_positives,
            "testPositives": test_positives,
            "trainPositivesPct": round(train_positives / len(train_df) * 100, 2) if len(train_df) > 0 else 0,
            "testPositivesPct": round(test_positives / len(test_df) * 100, 2) if len(test_df) > 0 else 0,
        }
        # Write to DB checkpoint_data
        try:
            from app.models.database import SessionLocal
            from app.models.task_queue import TaskQueue
            _db = SessionLocal()
            try:
                _task = _db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
                if _task:
                    existing = dict(_task.checkpoint_data or {})
                    existing.update(dataset_stats)
                    _task.checkpoint_data = existing
                    _db.commit()
            finally:
                _db.close()
        except Exception as e:
            logger.warning(f"Failed to persist dataset stats to DB: {e}")
        # Also update in-memory store
        try:
            from app.api.jobs import jobs_store
            if task_id in jobs_store:
                jobs_store[task_id].update(dataset_stats)
                jobs_store[task_id]["targetColumns"] = all_target_columns
        except Exception:
            pass

        # Get timeframe from first dataset (for frequency inference)
        timeframe = dataset_infos[0].get('timeframe', 'daily') if dataset_infos else 'daily'

        # Dry run - return after data preparation without training
        if dry_run:
            logger.info(f"Dry run complete. Data prepared successfully.")
            target_stats = combined_df[target_column].value_counts().to_dict()
            return {
                'status': 'dry_run_success',
                'job_type': job_type,
                'dataset_rows': len(combined_df),
                'train_rows': len(train_df),
                'test_rows': len(test_df),
                'feature_count': len(feature_columns),
                'target_column': target_column,
                'target_distribution': target_stats,
                'selected_models': selected_models,
                'prediction_modes': prediction_modes,
                'prediction_horizon': prediction_horizon,
                'timeframe': timeframe,
            }

        # Route based on job type
        if job_type == 'classification':
            # Use tsai for classification
            update_job_progress(task_id, 25, f"Starting classification optimization across {len(selected_models)} model types...")

            try:
                model_result = train_classification_optimization(
                    task_id=task_id,
                    selected_models=selected_models,
                    full_df=combined_df,
                    train_ratio=train_ratio,
                    target_column=target_column,
                    feature_columns=feature_columns,
                    parameter_ranges=parameter_ranges,
                    genetic_config=genetic_config,
                    metrics_config=metrics_config,
                    prediction_horizon=prediction_horizon,
                    prediction_modes=prediction_modes,
                    progress_base=25,
                    progress_range=65,
                    timeframe=timeframe
                )
                results = [model_result]

            except Exception as e:
                logger.error(f"Failed classification optimization: {e}", exc_info=True)
                import traceback
                traceback.print_exc()
                results = [{
                    'model_type': 'classification',
                    'status': 'failed',
                    'error': str(e)
                }]
        else:
            # Use darts for regression (unified optimization)
            update_job_progress(task_id, 25, f"Starting unified optimization across {len(selected_models)} model types...")

            try:
                model_result = train_unified_optimization(
                    task_id=task_id,
                    selected_models=selected_models,
                    full_df=combined_df,
                    train_ratio=train_ratio,
                    target_column=target_column,
                    feature_columns=feature_columns,
                    parameter_ranges=parameter_ranges,
                    genetic_config=genetic_config,
                    metrics_config=metrics_config,
                    prediction_horizon=prediction_horizon,
                    progress_base=25,
                    progress_range=65,
                    timeframe=timeframe
                )
                results = [model_result]

            except Exception as e:
                logger.error(f"Failed unified optimization: {e}", exc_info=True)
                import traceback
                traceback.print_exc()
                results = [{
                    'model_type': 'unified',
                    'status': 'failed',
                    'error': str(e)
                }]

        # Find best model
        update_job_progress(task_id, 90, "Analyzing results...")

        successful_results = [r for r in results if r.get('status') == 'completed']
        best_result = None
        if successful_results:
            best_result = max(successful_results, key=lambda x: x.get('best_fitness', 0))

        # Determine overall job status (unified optimization counts as 1 model)
        total_models = 1

        # Check if job was cancelled
        cancelled_results = [r for r in results if r.get('status') == 'cancelled']
        if cancelled_results:
            update_job_progress(task_id, 100, "Training cancelled by user")
            return {
                'status': 'cancelled',
                'models_trained': 0,
                'total_models': total_models,
                'results': results,
                'datasets': dataset_infos,
                'train_rows': len(train_df),
                'test_rows': len(test_df),
                'target_column': target_column,
                'train_positives': train_positives,
                'test_positives': test_positives,
                'train_positives_pct': round(train_positives_pct, 2),
                'test_positives_pct': round(test_positives_pct, 2),
                'completed_at': datetime.now().isoformat()
            }

        if len(successful_results) == 0:
            # Training failed
            error_messages = [r.get('error', 'Unknown error') for r in results if r.get('status') == 'failed']
            combined_error = "; ".join(set(error_messages[:3]))  # Dedupe and limit
            logger.error(f"Training job {task_id} failed: {combined_error}")
            update_job_progress(task_id, 100, f"Training failed: {combined_error}")

            return {
                'status': 'failed',
                'error': f"Training failed: {combined_error}",
                'models_trained': 0,
                'total_models': total_models,
                'results': results,
                'datasets': dataset_infos,
                'train_rows': len(train_df),
                'test_rows': len(test_df),
                'target_column': target_column,
                'train_positives': train_positives,
                'test_positives': test_positives,
                'train_positives_pct': round(train_positives_pct, 2),
                'test_positives_pct': round(test_positives_pct, 2),
                'completed_at': datetime.now().isoformat()
            }
        else:
            # Training succeeded
            update_job_progress(task_id, 100, "Training completed successfully")

            # Include all_individuals and error/success counts for visualization
            all_individuals = []
            total_error_count = 0
            total_success_count = 0
            for r in results:
                if 'all_individuals' in r:
                    all_individuals.extend(r['all_individuals'])
                total_error_count += r.get('error_count', 0)
                total_success_count += r.get('success_count', 0)

            return {
                'status': 'completed',
                'models_trained': len(successful_results),
                'total_models': total_models,
                'results': results,
                'best_model': best_result,
                'all_individuals': all_individuals,  # For UI visualization
                'error_count': total_error_count,
                'success_count': total_success_count,
                'datasets': dataset_infos,
                'train_rows': len(train_df),
                'test_rows': len(test_df),
                'target_column': target_column,
                'train_positives': train_positives,
                'test_positives': test_positives,
                'train_positives_pct': round(train_positives_pct, 2),
                'test_positives_pct': round(test_positives_pct, 2),
                'completed_at': datetime.now().isoformat()
            }

    except Exception as e:
        logger.error(f"Training job {task_id} failed: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        return {
            'status': 'failed',
            'error': str(e)
        }


def train_single_model(
    task_id: str,
    model_type: str,
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    target_column: str,
    feature_columns: List[str],
    parameter_ranges: Dict[str, Any],
    genetic_config: Dict[str, Any],
    metrics_config: Dict[str, Any],
    progress_base: float,
    progress_range: float,
    timeframe: str = 'daily'
) -> Dict[str, Any]:
    """
    Train a single model type with genetic optimization.

    Provides real-time progress updates for:
    - Each generation of genetic optimization
    - Each individual model being trained within a generation

    Args:
        task_id: Task ID for progress updates
        model_type: Model type (lstm, nbeats, rnn)
        train_df: Training data
        test_df: Test data
        target_column: Column to predict
        feature_columns: Feature columns
        parameter_ranges: Hyperparameter ranges from job config
        genetic_config: Genetic algorithm config
        metrics_config: Metrics configuration
        progress_base: Base progress percentage
        progress_range: Progress range for this model
        timeframe: Dataset timeframe for frequency inference

    Returns:
        Training result dictionary
    """
    from app.services.task_queue import get_task_queue, get_training_task_queue

    # Initialize services
    ml_service = MLModelsService()
    training_service = TrainingService()

    # Prepare data for Darts FIRST to know data length for constraints
    try:
        train_series, train_covariates = training_service.prepare_data(
            train_df,
            target_column=target_column,
            feature_columns=feature_columns,
            timeframe=timeframe
        )
        test_series, test_covariates = training_service.prepare_data(
            test_df,
            target_column=target_column,
            feature_columns=feature_columns,
            timeframe=timeframe
        )
    except Exception as e:
        logger.error(f"Failed to prepare data for {model_type}: {e}", exc_info=True)
        return {
            'model_type': model_type,
            'status': 'failed',
            'error': f'Data preparation failed: {e}'
        }

    # Build parameter ranges with data length constraints
    # For RNN: training_length defaults to 3 * input_chunk_length
    # Ensure input_chunk_length <= train_series_length / 4 to have enough data
    train_length = len(train_series)
    max_input_chunk = min(60, max(10, train_length // 4))
    ga_param_ranges = build_param_ranges(model_type, parameter_ranges, max_input_chunk=max_input_chunk)

    # Get genetic config - required parameters (no defaults)
    required_ga_keys = ['populationSize', 'generations', 'crossoverProb', 'mutationProb',
                        'earlyStoppingGenerations', 'elitismPercent']
    for key in required_ga_keys:
        if key not in genetic_config:
            return {'model_type': model_type, 'status': 'failed', 'error': f'genetic_config.{key} is required'}

    population_size = genetic_config['populationSize']
    generations = genetic_config['generations']
    crossover_prob = genetic_config['crossoverProb']
    mutation_prob = genetic_config['mutationProb']
    early_stopping = genetic_config['earlyStoppingGenerations']
    elitism_percent = genetic_config['elitismPercent']
    parallel_individuals = genetic_config.get('parallelIndividuals', 1)

    # Optimize metric - required
    optimize_metric = metrics_config.get('optimizeMetric')
    if optimize_metric is None:
        return {'model_type': model_type, 'status': 'failed', 'error': 'metrics_config.optimizeMetric is required'}

    # Loss function configuration - required
    loss_function_type = metrics_config.get('lossFunction')
    if loss_function_type is None:
        return {'model_type': model_type, 'status': 'failed', 'error': 'metrics_config.lossFunction is required'}
    loss_fn = None

    # Create loss function if not using default MSE
    if loss_function_type and loss_function_type != 'mse':
        try:
            from app.services.losses import get_loss_function
            # Calculate class counts from training data for weighted loss
            train_vals = train_series.values().flatten()
            positive_count = int((train_vals == 1).sum())
            negative_count = int((train_vals == 0).sum())
            logger.info(f"Class distribution for loss: {positive_count} positive, {negative_count} negative")

            loss_fn = get_loss_function(
                loss_type=loss_function_type,
                positive_count=positive_count,
                negative_count=negative_count
            )
            logger.info(f"Using {loss_function_type} loss function for training")
        except Exception as e:
            logger.warning(f"Failed to create {loss_function_type} loss function: {e}. Using default MSE.")
            loss_fn = None

    # Progress tracking state - mutable to allow updates from nested functions
    progress_state = {
        'current_generation': 0,
        'current_individual': 0,
        'best_fitness': 0.0,
        'cancelled': False
    }

    # Best model tracking
    best_model = [None]
    best_metrics = [{}]

    def check_cancelled() -> bool:
        """Check if task was cancelled - uses short-lived DB session."""
        task_queue = get_training_task_queue()
        status = task_queue.get_task_status(task_id)
        # Treat deleted task (status is None) the same as cancelled
        if status is None or status.get('status') in ['cancelled', 'paused']:
            progress_state['cancelled'] = True
            return True
        return False

    def fitness_function(params: Dict) -> float:
        """
        Evaluate model with given parameters.
        Updates progress for each individual being trained.
        """
        # Check for cancellation before training
        if progress_state['cancelled'] or check_cancelled():
            raise InterruptedError("Task cancelled")

        # Update progress for this individual
        progress_state['current_individual'] += 1
        individual_num = progress_state['current_individual']
        gen = progress_state['current_generation']

        # Calculate fine-grained progress:
        # Each generation covers (progress_range / generations) percent
        # Within each generation, each individual covers a fraction of that
        gen_progress = (gen / generations) * progress_range * 0.9  # 90% for generations
        individual_progress = (individual_num / population_size) * (progress_range / generations) * 0.9

        current_progress = progress_base + gen_progress + individual_progress

        update_job_progress(
            task_id,
            current_progress,
            f"{model_type.upper()}: Gen {gen}/{generations}, Training individual {individual_num}/{population_size}"
        )

        # Update training state for real-time UI (reset epoch history for new model)
        update_job_training_state(
            task_id,
            current_generation=gen,
            total_generations=generations,
            current_individual=individual_num,
            population_size=population_size,
            current_model_type=model_type,
            current_epoch=0,
            reset_epoch_history=True
        )

        try:
            # Epoch callback to track training progress
            def epoch_callback(current_epoch: int, total_epochs: int, metrics: Dict[str, float] = None):
                update_job_training_state(
                    task_id,
                    current_epoch=current_epoch,
                    total_epochs=total_epochs,
                    current_model_params=params,
                    epoch_metrics=metrics
                )

            # Create model with epoch callback and custom loss function
            model = ml_service.create_model(model_type, params, epoch_callback=epoch_callback, loss_fn=loss_fn)

            # Train with validation series to get val_loss during training
            train_result = training_service.train_model(
                model,
                train_series,
                val_series=test_series,  # Use test series for validation metrics
                covariates=train_covariates,
                verbose=False
            )

            if train_result.get('status') == 'failed':
                return 0.0

            # Check cancellation after training
            if check_cancelled():
                raise InterruptedError("Task cancelled")

            # Evaluate with the selected optimization metric
            eval_result = training_service.evaluate_model(
                model,
                test_series,
                covariates=test_covariates,
                optimize_metric=optimize_metric
            )

            if 'error' in eval_result:
                return 0.0

            # Calculate fitness based on metric type
            if optimize_metric == 'mape':
                # Lower MAPE is better - convert to fitness (higher is better)
                mape_val = eval_result.get('mape', 100)
                if mape_val is None:
                    mape_val = 100
                fitness = 1.0 / (1.0 + mape_val / 100)
            elif optimize_metric in {'mae', 'rmse'}:
                # Lower is better for regression metrics
                metric_val = eval_result.get(optimize_metric, 100)
                fitness = 1.0 / (1.0 + metric_val)
            else:
                # Classification metrics (f1_score, accuracy, etc.) - higher is better
                fitness = eval_result.get(optimize_metric, 0.0)

            # Track best
            if best_model[0] is None or fitness > best_metrics[0].get('fitness', 0):
                best_model[0] = model
                best_metrics[0] = {**eval_result, 'fitness': fitness, 'params': params}
                progress_state['best_fitness'] = fitness

                # Update progress with new best
                update_job_progress(
                    task_id,
                    current_progress,
                    f"{model_type.upper()}: Gen {gen}/{generations}, New best fitness: {fitness:.4f}"
                )
                # Update jobs_store with new best fitness
                update_job_training_state(
                    task_id,
                    best_fitness=fitness
                )

            return fitness

        except InterruptedError:
            raise
        except Exception as e:
            logger.error(f"Fitness evaluation failed: {e}", exc_info=True)
            return 0.0

    def on_generation_start(generation: int):
        """Called BEFORE evaluating individuals in a generation."""
        progress_state['current_generation'] = generation
        progress_state['current_individual'] = 0

    def ga_callback(generation: int, best_fitness: float, best_params: Dict):
        """Called after each generation completes."""
        # Update progress at generation boundary
        progress = progress_base + ((generation + 1) / generations) * progress_range * 0.9
        update_job_progress(
            task_id,
            progress,
            f"{model_type.upper()}: Completed Gen {generation + 1}/{generations}, Best: {best_fitness:.4f}"
        )

        # Check if task is paused/cancelled
        if check_cancelled():
            raise InterruptedError("Task paused/cancelled")

    # Run genetic optimization
    logger.info(f"Starting genetic optimization for {model_type}")
    logger.info(f"Population: {population_size}, Generations: {generations}")

    update_job_progress(
        task_id,
        progress_base,
        f"{model_type.upper()}: Starting optimization (pop={population_size}, gens={generations})"
    )

    optimizer = GeneticOptimizer(
        param_ranges=ga_param_ranges,
        population_size=population_size,
        n_generations=generations,
        crossover_prob=crossover_prob,
        mutation_prob=mutation_prob,
        early_stopping_generations=early_stopping,
        elitism_percent=elitism_percent,
        parallel_individuals=parallel_individuals,
    )

    try:
        opt_result = optimizer.optimize(
            fitness_function=fitness_function,
            callback=ga_callback,
            on_generation_start=on_generation_start
        )
    except InterruptedError:
        update_job_progress(
            task_id,
            progress_base + progress_range * 0.5,
            f"{model_type.upper()}: Training interrupted"
        )
        return {
            'model_type': model_type,
            'status': 'paused',
            'generations_run': progress_state['current_generation'],
            'best_fitness': progress_state['best_fitness']
        }

    # Save best model if found
    model_path = None
    if best_model[0] is not None:
        update_job_progress(
            task_id,
            progress_base + progress_range * 0.95,
            f"{model_type.upper()}: Saving best model..."
        )
        try:
            model_name = f"job_{task_id}_{model_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            model_path = training_service.save_model(
                best_model[0],
                model_name,
                metadata={
                    'model_type': model_type,
                    'task_id': task_id,
                    'best_params': opt_result.get('best_params'),
                    'best_fitness': opt_result.get('best_fitness'),
                    'metrics': best_metrics[0]
                }
            )
            logger.info(f"Saved best {model_type} model to {model_path}")
        except Exception as e:
            logger.error(f"Failed to save model: {e}", exc_info=True)

    update_job_progress(
        task_id,
        progress_base + progress_range,
        f"{model_type.upper()}: Completed with fitness {opt_result.get('best_fitness', 0):.4f}"
    )

    return {
        'model_type': model_type,
        'status': 'completed',
        'best_params': opt_result.get('best_params'),
        'best_fitness': opt_result.get('best_fitness'),
        'generations_run': opt_result.get('generations_run'),
        'metrics': best_metrics[0],
        'model_path': model_path,
        'history': opt_result.get('history', [])[-5:]  # Last 5 generations
    }


def train_classification_optimization(
    task_id: str,
    selected_models: List[str],
    full_df: pd.DataFrame,
    train_ratio: float,
    target_column: str,
    feature_columns: List[str],
    parameter_ranges: Dict[str, Any],
    genetic_config: Dict[str, Any],
    metrics_config: Dict[str, Any],
    prediction_horizon: int,
    prediction_modes: List[str],
    progress_base: float,
    progress_range: float,
    timeframe: str = 'daily'
) -> Dict[str, Any]:
    """
    Classification optimization using tsai models.

    Uses TSAIModelService and TSAITrainingService for classification tasks.
    Supports shift and multistep prediction modes.
    """
    from app.services.task_queue import get_task_queue
    from app.services.tsai_models import TSAIModelService, TSAI_AVAILABLE
    from app.services.tsai_training import TSAITrainingService

    if not TSAI_AVAILABLE:
        return {
            'model_type': 'classification',
            'status': 'failed',
            'error': 'tsai library not available for classification'
        }

    # Get normalization buffer from parameter ranges (default 35% if not specified for old configs)
    normalization_buffer = parameter_ranges.get('normalizationBuffer', 35) / 100.0  # Convert % to decimal

    # Initialize services
    model_service = TSAIModelService()
    training_service = TSAITrainingService(buffer_pct=normalization_buffer)

    # Get genetic config - required parameters (no defaults)
    population_size = genetic_config.get('populationSize')
    if population_size is None:
        logger.error("Classification training failed: genetic_config.populationSize is required")
        return {'model_type': 'classification', 'status': 'failed', 'error': 'genetic_config.populationSize is required'}

    generations = genetic_config.get('generations')
    if generations is None:
        logger.error("Classification training failed: genetic_config.generations is required")
        return {'model_type': 'classification', 'status': 'failed', 'error': 'genetic_config.generations is required'}

    training_epochs = genetic_config.get('trainingEpochs')
    if training_epochs is None:
        logger.error("Classification training failed: genetic_config.trainingEpochs is required")
        return {'model_type': 'classification', 'status': 'failed', 'error': 'genetic_config.trainingEpochs is required'}

    optimize_metric = metrics_config.get('classificationMetric')
    if optimize_metric is None:
        logger.error("Classification training failed: metrics_config.classificationMetric is required")
        return {'model_type': 'classification', 'status': 'failed', 'error': 'metrics_config.classificationMetric is required'}

    # Support both single lossFunction (legacy) and lossFunctions array (new)
    loss_functions = metrics_config.get('lossFunctions')
    if loss_functions is None:
        # Fallback to single loss function for backwards compatibility
        single_loss = metrics_config.get('lossFunction')
        if single_loss is None:
            logger.error("Classification training failed: metrics_config.lossFunction or lossFunctions is required")
            return {'model_type': 'classification', 'status': 'failed', 'error': 'metrics_config.lossFunction or lossFunctions is required'}
        loss_functions = [single_loss]

    optimize_loss_function_flag = metrics_config.get('optimizeLossFunction', False)
    optimize_loss_function = optimize_loss_function_flag and len(loss_functions) > 1
    logger.info(f"Loss functions: {loss_functions}, optimizeLossFunction={optimize_loss_function_flag}, will_optimize={optimize_loss_function}")

    # Threshold optimization settings
    threshold_min = metrics_config.get('thresholdMin', 0.3)
    threshold_max = metrics_config.get('thresholdMax', 0.6)
    threshold_step = metrics_config.get('thresholdStep', 0.1)
    # Calculate number of threshold steps for GA
    n_thresholds = max(1, int(round((threshold_max - threshold_min) / threshold_step)) + 1)
    logger.info(f"Threshold optimization: min={threshold_min}, max={threshold_max}, step={threshold_step}, n_steps={n_thresholds}")

    # Handle seq_len - either fixed or optimizable range
    optimize_seq_len = parameter_ranges.get('optimizeSeqLen', False)
    if optimize_seq_len:
        seq_len_min = parameter_ranges.get('seqLenMin', 24)
        seq_len_max = parameter_ranges.get('seqLenMax', 48)
        seq_len_step = parameter_ranges.get('seqLenStep', 12)
        # Generate all possible seq_len values
        seq_len_values = list(range(seq_len_min, seq_len_max + 1, seq_len_step))
        if not seq_len_values:
            seq_len_values = [seq_len_min]
        seq_len = seq_len_values[0]  # Default for initial data prep
        logger.info(f"SeqLen optimization enabled: values={seq_len_values}")
    else:
        seq_len = parameter_ranges.get('seqLen')
        if seq_len is None:
            logger.error("Classification training failed: parameter_ranges.seqLen is required")
            return {'model_type': 'classification', 'status': 'failed', 'error': 'parameter_ranges.seqLen is required for classification'}
        seq_len_values = [seq_len]  # Only one value

    # Prepare data for each prediction mode and seq_len combination
    # When optimizing seq_len, we need data prepared for each value
    data_by_mode_and_seqlen = {}

    for current_seq_len in seq_len_values:
        for mode in prediction_modes:
            cache_key = (mode, current_seq_len)
            try:
                # NOTE: Target column is already pre-shifted during dataset generation
                # (e.g., directional target uses shift(-horizon) to look ahead)
                # So we pass prediction_horizon=0 here to avoid double-shifting
                X_train, X_test, y_train, y_test = training_service.prepare_data_split(
                    full_df,
                    train_ratio=train_ratio,
                    target_column=target_column,
                    feature_columns=feature_columns,
                    seq_len=current_seq_len,
                    prediction_horizon=0,  # Target already pre-shifted
                    prediction_mode=mode
                )
                c_out = 2 if mode == 'shift' else prediction_horizon
                # Get the actual valid columns used after dropping zero-variance columns
                valid_feature_columns = training_service.data_prep.get_valid_columns() if training_service.data_prep else feature_columns
                # Get normalization params for inference consistency
                normalization_params = training_service.get_normalization_params() if hasattr(training_service, 'get_normalization_params') else None
                data_by_mode_and_seqlen[cache_key] = {
                    'X_train': X_train, 'X_test': X_test,
                    'y_train': y_train, 'y_test': y_test,
                    'c_out': c_out,
                    'seq_len': current_seq_len,
                    'valid_feature_columns': valid_feature_columns,  # Store the actual columns used
                    'normalization_params': normalization_params  # Store normalization params for forward test
                }
                logger.info(f"Prepared {mode} data (seq_len={current_seq_len}): train={len(X_train)}, test={len(X_test)}, c_out={c_out}, features={len(valid_feature_columns)}")
            except Exception as e:
                logger.error(f"Failed to prepare {mode} data (seq_len={current_seq_len}): {e}")
                continue

    if not data_by_mode_and_seqlen:
        return {
            'model_type': 'classification',
            'status': 'failed',
            'error': 'Failed to prepare data for any prediction mode'
        }

    # For backwards compatibility, create data_by_mode using default seq_len
    data_by_mode = {}
    for mode in prediction_modes:
        cache_key = (mode, seq_len_values[0])
        if cache_key in data_by_mode_and_seqlen:
            data_by_mode[mode] = data_by_mode_and_seqlen[cache_key]

    # Progress tracking
    progress_state = {
        'current_generation': 0,
        'current_individual': 0,
        'best_fitness': 0.0,
        'best_model_type': None,
        'best_mode': None,
        'cancelled': False,
        'all_individuals': [],
        'error_count': 0,
        'success_count': 0
    }

    best_model = [None]
    best_metrics = [{}]
    best_params = [{}]

    # Initialize training state for UI
    update_job_training_state(
        task_id,
        current_generation=0,
        total_generations=generations,
        current_individual=0,
        population_size=population_size,
        current_epoch=0,
        total_epochs=training_epochs,
        error_count=0,
        success_count=0
    )

    def check_cancelled() -> bool:
        task_queue = get_task_queue()
        status = task_queue.get_task_status(task_id)
        if status and status.get('status') in ['cancelled', 'paused']:
            progress_state['cancelled'] = True
            return True
        return False

    def fitness_function(params: Dict) -> float:
        """Evaluate model with given parameters."""
        if progress_state['cancelled'] or check_cancelled():
            raise InterruptedError("Task cancelled")

        progress_state['current_individual'] += 1
        gen = progress_state['current_generation']
        individual_num = progress_state['current_individual']

        # Get model type from params
        model_type_idx = int(params.get('model_type_idx', 0))
        model_type = selected_models[model_type_idx % len(selected_models)]

        # Update individual progress for UI
        update_job_training_state(
            task_id,
            current_generation=gen,
            total_generations=generations,
            current_individual=progress_state['current_individual'],
            population_size=population_size,
            current_model_type=model_type,
            current_epoch=0,
            total_epochs=training_epochs,
            reset_epoch_history=True
        )

        # Get prediction mode from params (if multiple modes)
        mode_idx = int(params.get('prediction_mode_idx', 0))
        mode = prediction_modes[mode_idx % len(prediction_modes)]

        # Get seq_len from params (if optimizing seq_len)
        if 'seq_len_idx' in params and optimize_seq_len:
            seq_len_idx = int(params['seq_len_idx'])
            current_seq_len = seq_len_values[seq_len_idx % len(seq_len_values)]
        else:
            current_seq_len = seq_len_values[0]

        # Get data for this mode and seq_len combination
        cache_key = (mode, current_seq_len)
        mode_data = data_by_mode_and_seqlen.get(cache_key)
        if not mode_data:
            # Fallback to default seq_len
            cache_key = (mode, seq_len_values[0])
            mode_data = data_by_mode_and_seqlen.get(cache_key)
            if not mode_data:
                logger.warning(f"No data for mode {mode}, seq_len {current_seq_len}, skipping")
                return 0.0

        X_train = mode_data['X_train']
        X_test = mode_data['X_test']
        y_train = mode_data['y_train']
        y_test = mode_data['y_test']
        c_out = mode_data['c_out']
        actual_seq_len = mode_data['seq_len']

        # Extract hyperparameters (from genetic algorithm params - no defaults needed, GA generates them)
        hidden_size = int(params['hidden_size'])
        n_layers = int(params['n_layers'])
        dropout = float(params['dropout'])
        learning_rate = float(params['learning_rate'])

        # Get loss function from genes if optimizing, otherwise use first one
        if 'loss_function_idx' in params:
            loss_idx = int(params['loss_function_idx'])
            current_loss_function = loss_functions[loss_idx % len(loss_functions)]
            logger.debug(f"Loss function from GA: idx={loss_idx} -> {current_loss_function} (from {loss_functions})")
        else:
            current_loss_function = loss_functions[0]
            logger.debug(f"Loss function (no optimization): {current_loss_function}")

        # Get threshold from genes (decode discrete index to actual threshold)
        threshold_idx = int(params.get('threshold_idx', 0))
        current_threshold = threshold_min + threshold_idx * threshold_step
        current_threshold = min(current_threshold, threshold_max)  # Clamp to max

        try:
            # Create model
            model_params = {
                'hidden_size': hidden_size,
                'n_layers': n_layers,
                'dropout': dropout,
            }
            model = model_service.create_model(
                model_type, model_params,
                c_in=X_train.shape[1],
                c_out=c_out,
                seq_len=X_train.shape[2]
            )

            # Get loss function (use current_loss_function from GA params)
            loss_fn = training_service.get_loss_function(
                loss_type=current_loss_function.replace('_loss', '').replace('weighted_cross_entropy', 'weighted_ce'),
                prediction_mode=mode
            )

            # Create epoch callback for training progress
            def epoch_cb(epoch: int, metrics: Dict[str, float] = None):
                update_job_training_state(
                    task_id,
                    current_epoch=epoch + 1,  # epoch is 0-indexed
                    total_epochs=training_epochs,
                    current_individual=progress_state['current_individual'],
                    population_size=population_size,
                    current_model_type=model_type,
                    epoch_metrics=metrics
                )

            # Train
            result = training_service.train_model(
                model,
                (X_train, y_train),
                val_data=(X_test, y_test),
                epochs=training_epochs,
                learning_rate=learning_rate,
                loss_fn=loss_fn,
                prediction_mode=mode,
                epoch_callback=epoch_cb
            )

            if result['status'] != 'success':
                progress_state['error_count'] += 1
                return 0.0

            # Assess model with optimized threshold
            metrics = training_service.assess_model(
                result['model'],
                (X_test, y_test),
                prediction_mode=mode,
                learner=result.get('learner'),
                threshold=current_threshold
            )

            # Check if assessment failed (e.g., NaN predictions)
            if metrics.get('error'):
                logger.warning(f"Assessment failed: {metrics.get('error')}")
                progress_state['error_count'] += 1
                return 0.0

            fitness = metrics.get(optimize_metric, 0.0)

            # Track best
            if fitness > progress_state['best_fitness']:
                progress_state['best_fitness'] = fitness
                progress_state['best_model_type'] = model_type
                progress_state['best_mode'] = mode
                best_model[0] = result['model']
                best_metrics[0] = metrics
                best_params[0] = {
                    'model_type': model_type,
                    'prediction_mode': mode,
                    **model_params,
                    'learning_rate': learning_rate,
                    'loss_function': current_loss_function,
                    'threshold': current_threshold,
                    'seq_len': actual_seq_len
                }

            progress_state['success_count'] += 1

            # Save individual model for elite selection later
            import json
            import torch
            # Capture training history before it gets reset (must be before any other training starts)
            training_history = get_epoch_history(task_id)

            models_dir = get_job_models_dir(task_id)
            model_filename = f"gen{gen:03d}_ind{individual_num:03d}_{model_type}_f{fitness:.4f}"
            model_save_path = models_dir / f"{model_filename}.pt"
            meta_save_path = models_dir / f"{model_filename}_meta.json"
            try:
                torch.save(result['model'].state_dict(), model_save_path)
                # Save metadata for model reconstruction
                # Get target columns from job store
                from app.api.jobs import jobs_store as js
                job_target_columns = js.get(task_id, {}).get('targetColumns', [])

                metadata = {
                    'task_id': task_id,
                    'generation': gen,
                    'individual': individual_num,
                    'model_type': model_type,
                    'fitness': fitness,
                    'params': model_params,
                    'learning_rate': learning_rate,
                    'loss_function': current_loss_function,
                    'threshold': current_threshold,
                    'prediction_mode': mode,
                    'seq_len': actual_seq_len,
                    'c_in': X_train.shape[1],
                    'c_out': c_out,
                    'metrics': metrics,
                    # Save the ACTUAL feature columns used during training (after dropping zero-variance cols)
                    # This must match c_in for inference to work correctly
                    'feature_columns': mode_data.get('valid_feature_columns', feature_columns),
                    # Save training history for visualization after model is saved to inventory
                    'training_history': training_history,
                    # Save normalization params for forward test inference
                    'normalization_params': mode_data.get('normalization_params'),
                    # Save target column names for prediction matching
                    'target_columns': job_target_columns
                }
                with open(meta_save_path, 'w') as f:
                    json.dump(metadata, f, indent=2, default=str)
            except Exception as save_err:
                logger.warning(f"Failed to save individual model: {save_err}")

            # Record individual
            individual_record = {
                'generation': gen,
                'individual': individual_num,
                'model_type': model_type,
                'prediction_mode': mode,
                'params': model_params,
                'seq_len': actual_seq_len,
                'loss_function': current_loss_function,
                'threshold': current_threshold,
                'fitness': fitness,
                'metrics': metrics,
                'training_history': training_history
            }
            progress_state['all_individuals'].append(individual_record)

            # Add to jobs_store for real-time UI access
            add_individual_to_job(task_id, individual_record)

            # Update progress - always show loss function and threshold
            current_progress = progress_base + (gen / generations) * progress_range * 0.9
            # Shorten loss function name for display
            loss_short = current_loss_function.replace('_loss', '').replace('weighted_cross_entropy', 'weighted_ce').replace('cross_entropy', 'ce')
            seq_len_str = f" seq={actual_seq_len}" if optimize_seq_len else ""
            progress_msg = f"Gen {gen}: {model_type} ({mode}) fitness={fitness:.4f} loss={loss_short} thresh={current_threshold:.2f}{seq_len_str}"
            update_job_progress(task_id, current_progress, progress_msg)

            return fitness

        except Exception as e:
            logger.error(f"Training error: {e}")
            progress_state['error_count'] += 1
            return 0.0

    def on_generation_start(gen: int):
        """Called BEFORE evaluating individuals in a generation."""
        progress_state['current_generation'] = gen
        progress_state['current_individual'] = 0

    def generation_callback(gen: int, best_fitness: float, pop_fitness: list):
        """Called AFTER all individuals in a generation are evaluated."""
        # Update job training state for UI (generation complete)
        update_job_training_state(
            task_id,
            current_generation=gen,
            total_generations=generations,
            current_individual=0,
            population_size=population_size,
            best_fitness=progress_state['best_fitness'],
            error_count=progress_state['error_count'],
            success_count=progress_state['success_count'],
            reset_epoch_history=True
        )

        # Cleanup non-elite models to save disk space
        elitism_pct = genetic_config.get('elitismPercent', 10.0)
        cleanup_non_elite_models(
            task_id=task_id,
            all_individuals=progress_state['all_individuals'],
            elitism_percent=elitism_pct,
            population_size=population_size
        )

    # Build parameter ranges for genetic algorithm - all required (no defaults)
    required_param_keys = ['layerSizeMin', 'layerSizeMax', 'layersMin', 'layersMax',
                           'dropoutMin', 'dropoutMax', 'learningRateMin', 'learningRateMax']
    for key in required_param_keys:
        if key not in parameter_ranges:
            return {'model_type': 'classification', 'status': 'failed', 'error': f'parameter_ranges.{key} is required'}

    ga_param_ranges = {
        'model_type_idx': {'type': 'int', 'min': 0, 'max': len(selected_models) - 1},
        'hidden_size': {'type': 'int', 'min': parameter_ranges['layerSizeMin'],
                        'max': parameter_ranges['layerSizeMax']},
        'n_layers': {'type': 'int', 'min': parameter_ranges['layersMin'],
                     'max': parameter_ranges['layersMax']},
        'dropout': {'type': 'float', 'min': parameter_ranges['dropoutMin'],
                    'max': parameter_ranges['dropoutMax']},
        'learning_rate': {'type': 'float', 'min': parameter_ranges['learningRateMin'],
                          'max': parameter_ranges['learningRateMax']},
    }

    # Add prediction mode to genes if multiple modes
    if len(prediction_modes) > 1:
        ga_param_ranges['prediction_mode_idx'] = {'type': 'int', 'min': 0, 'max': len(prediction_modes) - 1}

    # Add loss function to GA if optimization enabled
    if optimize_loss_function:
        ga_param_ranges['loss_function_idx'] = {'type': 'int', 'min': 0, 'max': len(loss_functions) - 1}
        logger.info(f"Loss function optimization enabled with {len(loss_functions)} options: {loss_functions}")

    # Add threshold to GA (discrete steps)
    ga_param_ranges['threshold_idx'] = {'type': 'int', 'min': 0, 'max': n_thresholds - 1}

    # Run genetic optimization - get required GA params
    from app.services.genetic import GeneticOptimizer, DEAP_AVAILABLE

    if not DEAP_AVAILABLE:
        return {
            'model_type': 'classification',
            'status': 'failed',
            'error': 'DEAP library not available for genetic optimization'
        }

    required_ga_keys = ['crossoverProb', 'mutationProb', 'elitismPercent', 'earlyStoppingGenerations']
    for key in required_ga_keys:
        if key not in genetic_config:
            return {'model_type': 'classification', 'status': 'failed', 'error': f'genetic_config.{key} is required'}

    optimizer = GeneticOptimizer(
        param_ranges=ga_param_ranges,
        population_size=population_size,
        n_generations=generations,
        crossover_prob=genetic_config['crossoverProb'],
        mutation_prob=genetic_config['mutationProb'],
        elitism_percent=genetic_config['elitismPercent'],
        early_stopping_generations=genetic_config['earlyStoppingGenerations'],
        parallel_individuals=genetic_config.get('parallelIndividuals', 1),
    )

    try:
        opt_result = optimizer.optimize(
            fitness_function,
            callback=generation_callback,
            on_generation_start=on_generation_start
        )
    except InterruptedError:
        return {
            'model_type': 'classification',
            'status': 'cancelled',
            'error': 'Job cancelled by user'
        }

    # Save best model
    model_path = None
    if best_model[0] is not None:
        import torch
        models_dir = get_job_models_dir(task_id)
        model_path = str(models_dir / f"best_classification_model.pt")
        torch.save(best_model[0].state_dict(), model_path)
        logger.info(f"Saved best model to {model_path}")

    # Save elite models (top N based on elitism percent or default 10)
    elitism_percent = genetic_config.get('elitismPercent', 10.0)
    elite_paths = save_elite_models(
        task_id=task_id,
        all_individuals=progress_state['all_individuals'],
        elitism_percent=elitism_percent,
        population_size=population_size,
        default_elite_count=10
    )
    logger.info(f"Saved {len(elite_paths)} elite models for classification job")

    update_job_progress(
        task_id,
        100.0,  # Ensure 100% progress on completion
        f"Classification: Completed with fitness {opt_result.get('best_fitness', 0):.4f}"
    )

    return {
        'model_type': 'classification',
        'status': 'completed',
        'best_params': best_params[0],
        'best_fitness': opt_result.get('best_fitness'),
        'generations_run': opt_result.get('generations_run'),
        'metrics': best_metrics[0],
        'model_path': model_path,
        'elite_model_paths': elite_paths,
        'all_individuals': progress_state['all_individuals'],
        'error_count': progress_state['error_count'],
        'success_count': progress_state['success_count'],
        'history': opt_result.get('history', [])[-5:]
    }


def train_unified_optimization(
    task_id: str,
    selected_models: List[str],
    full_df: pd.DataFrame,
    train_ratio: float,
    target_column: str,
    feature_columns: List[str],
    parameter_ranges: Dict[str, Any],
    genetic_config: Dict[str, Any],
    metrics_config: Dict[str, Any],
    prediction_horizon: int,
    progress_base: float,
    progress_range: float,
    timeframe: str = 'daily'
) -> Dict[str, Any]:
    """
    Unified optimization where model type is an optimization parameter.

    Each individual in the population can be a different model type,
    allowing the GA to compare and optimize across model architectures.

    Dataset handling by model type:
    - RNN models (LSTM/GRU): Use shifted target column (target_hN), output_chunk_length=1
      These models predict a single value N bars ahead.
    - Multi-step models (NBEATS/TCN/Transformer/TFT): Use original target, output_chunk_length=horizon
      These models predict the next N values in a single forward pass.
    """
    from app.services.task_queue import get_task_queue

    # Initialize services
    ml_service = MLModelsService()
    training_service = TrainingService()

    # Prepare data for BOTH model types
    # RNN models: use shifted target column for the furthest horizon
    # Multi-step models: use original target column
    #
    # NOTE: If prediction_horizon=0, targets are assumed to be pre-shifted (the look-ahead
    # is already built into the target column). In this case:
    # - RNN models: use original target (no shift) with output_chunk_length=1
    # - Multi-step models: use original target with output_chunk_length=1
    try:
        # Create shifted target column for RNN models (skip if prediction_horizon=0)
        rnn_df = full_df.copy()
        if prediction_horizon > 0:
            rnn_target_column = f"{target_column}_h{prediction_horizon}"
            rnn_df[rnn_target_column] = rnn_df[target_column].shift(-prediction_horizon)
        else:
            # No shift needed - target already has built-in look-ahead
            rnn_target_column = target_column

        # Prepare RNN data (shifted target, will use output_chunk_length=1)
        rnn_train_series, rnn_test_series, rnn_train_cov, rnn_test_cov = training_service.prepare_data_split(
            rnn_df,
            train_ratio=train_ratio,
            target_column=rnn_target_column,
            feature_columns=feature_columns,
            timeframe=timeframe
        )
        logger.info(f"RNN data prepared: train={len(rnn_train_series)}, test={len(rnn_test_series)} samples (target: {rnn_target_column})")

        # Prepare multi-step data (original target, will use output_chunk_length=horizon)
        ms_train_series, ms_test_series, ms_train_cov, ms_test_cov = training_service.prepare_data_split(
            full_df,
            train_ratio=train_ratio,
            target_column=target_column,
            feature_columns=feature_columns,
            timeframe=timeframe
        )
        logger.info(f"Multi-step data prepared: train={len(ms_train_series)}, test={len(ms_test_series)} samples (target: {target_column})")

        # Store both datasets for use in fitness function
        data_by_model_type = {
            'rnn': {
                'train_series': rnn_train_series,
                'test_series': rnn_test_series,
                'train_covariates': rnn_train_cov,
                'test_covariates': rnn_test_cov,
                'target_column': rnn_target_column,
                'output_chunk_length': 1
            },
            'multistep': {
                'train_series': ms_train_series,
                'test_series': ms_test_series,
                'train_covariates': ms_train_cov,
                'test_covariates': ms_test_cov,
                'target_column': target_column,
                # output_chunk_length must be >= 1; when prediction_horizon=0,
                # target already has built-in look-ahead so use 1
                'output_chunk_length': max(1, prediction_horizon)
            }
        }

        # Validate series lengths
        min_train_length = 100
        min_test_length = 50

        for dtype, dinfo in data_by_model_type.items():
            if len(dinfo['train_series']) < min_train_length:
                logger.warning(f"{dtype} train series ({len(dinfo['train_series'])}) is short.")
            if len(dinfo['test_series']) < min_test_length:
                logger.warning(f"{dtype} test series ({len(dinfo['test_series'])}) is very short.")

    except Exception as e:
        logger.error(f"Failed to prepare data: {e}", exc_info=True)
        return {
            'model_type': 'unified',
            'status': 'failed',
            'error': f'Data preparation failed: {e}'
        }

    # Build unified parameter ranges including model_type_idx
    train_length = len(ms_train_series)
    max_input_chunk = min(60, max(10, train_length // 4))
    ga_param_ranges = build_unified_param_ranges(selected_models, parameter_ranges, max_input_chunk)

    # Get genetic config - required parameters (no defaults)
    required_ga_keys = ['populationSize', 'generations', 'crossoverProb', 'mutationProb',
                        'earlyStoppingGenerations', 'elitismPercent', 'trainingEpochs']
    for key in required_ga_keys:
        if key not in genetic_config:
            return {'model_type': 'unified', 'status': 'failed', 'error': f'genetic_config.{key} is required'}

    population_size = genetic_config['populationSize']
    generations = genetic_config['generations']
    crossover_prob = genetic_config['crossoverProb']
    mutation_prob = genetic_config['mutationProb']
    early_stopping = genetic_config['earlyStoppingGenerations']
    elitism_percent = genetic_config['elitismPercent']
    training_epochs = genetic_config['trainingEpochs']
    parallel_individuals = genetic_config.get('parallelIndividuals', 1)

    optimize_metric = metrics_config.get('optimizeMetric')
    if optimize_metric is None:
        return {'model_type': 'unified', 'status': 'failed', 'error': 'metrics_config.optimizeMetric is required'}

    # Loss function configuration
    loss_function_type = metrics_config.get('lossFunction')
    if loss_function_type is None:
        return {'model_type': 'unified', 'status': 'failed', 'error': 'metrics_config.lossFunction is required'}
    loss_fn = None

    # Create loss function if not using default MSE
    if loss_function_type and loss_function_type != 'mse':
        try:
            from app.services.losses import get_loss_function
            # Calculate class counts from training data for weighted loss
            # Use train_series which should be available at this point
            train_vals = train_series.values().flatten()
            positive_count = int((train_vals == 1).sum())
            negative_count = int((train_vals == 0).sum())
            logger.info(f"Class distribution for loss: {positive_count} positive, {negative_count} negative")

            loss_fn = get_loss_function(
                loss_type=loss_function_type,
                positive_count=positive_count,
                negative_count=negative_count
            )
            logger.info(f"Using {loss_function_type} loss function for training")
        except Exception as e:
            logger.warning(f"Failed to create {loss_function_type} loss function: {e}. Using default MSE.")
            loss_fn = None

    # Progress tracking
    progress_state = {
        'current_generation': 0,
        'current_individual': 0,
        'best_fitness': 0.0,
        'best_model_type': None,
        'best_model_params': {},
        'cancelled': False,
        'all_individuals': [],  # Track all evaluated individuals for visualization
        'error_count': 0,  # Track training/evaluation errors
        'success_count': 0  # Track successful evaluations
    }

    best_model = [None]
    best_metrics = [{}]

    def check_cancelled() -> bool:
        task_queue = get_task_queue()
        status = task_queue.get_task_status(task_id)
        if status and status.get('status') in ['cancelled', 'paused']:
            progress_state['cancelled'] = True
            return True
        return False

    def fitness_function(params: Dict) -> float:
        """Evaluate model with given parameters including model type."""
        if progress_state['cancelled'] or check_cancelled():
            raise InterruptedError("Task cancelled")

        progress_state['current_individual'] += 1
        individual_num = progress_state['current_individual']
        gen = progress_state['current_generation']

        # Get model type from params
        model_type_idx = int(params.get('model_type_idx', 0))
        model_type = selected_models[model_type_idx % len(selected_models)]

        # Select appropriate data based on model type
        # RNN models (LSTM/GRU) use shifted target with output_chunk_length=1
        # Multi-step models use original target with output_chunk_length=horizon
        is_rnn = model_type in RNN_MODELS
        data_key = 'rnn' if is_rnn else 'multistep'
        model_data = data_by_model_type[data_key]

        train_series = model_data['train_series']
        test_series = model_data['test_series']
        train_covariates = model_data['train_covariates']
        test_covariates = model_data['test_covariates']
        model_output_chunk = model_data['output_chunk_length']

        # Calculate progress
        gen_progress = (gen / generations) * progress_range
        individual_progress = (individual_num / population_size) * (progress_range / generations) * 0.8
        current_progress = progress_base + gen_progress + individual_progress

        # Update training state for real-time UI (reset epoch history for new model)
        update_job_training_state(
            task_id,
            current_generation=gen,
            total_generations=generations,
            current_individual=individual_num,
            population_size=population_size,
            current_model_type=model_type,
            current_epoch=0,
            total_epochs=10,  # Will be updated during training
            best_fitness=progress_state.get('best_fitness'),
            error_count=progress_state.get('error_count', 0),
            success_count=progress_state.get('success_count', 0),
            reset_epoch_history=True
        )

        update_job_progress(
            task_id,
            current_progress,
            f"Gen {gen}/{generations}, {model_type.upper()} #{individual_num}/{population_size}"
        )

        try:
            # Get model-specific params with correct output_chunk_length
            model_params = get_model_params(model_type, params, training_epochs, model_output_chunk)
            n_epochs = model_params.get('n_epochs', 10)

            # Update epoch info and current model params before training
            update_job_training_state(
                task_id,
                current_epoch=0,
                total_epochs=n_epochs,
                current_model_type=model_type,
                current_model_params=model_params
            )

            # Create epoch callback to update UI during training with metrics
            def epoch_callback(current_epoch: int, total_epochs: int, metrics: Dict[str, float] = None):
                update_job_training_state(
                    task_id,
                    current_epoch=current_epoch,
                    total_epochs=total_epochs,
                    current_model_params=model_params,
                    epoch_metrics=metrics
                )

            model = ml_service.create_model(model_type, model_params, epoch_callback=epoch_callback, loss_fn=loss_fn)

            # Train with validation series to get val_loss during training
            training_result = training_service.train_model(
                model, train_series,
                val_series=test_series,  # Use test series for validation metrics
                covariates=train_covariates,
                verbose=False
            )

            # Update epoch to complete after training
            update_job_training_state(task_id, current_epoch=n_epochs)

            if training_result.get('status') == 'failed':
                logger.error(f"Training failed: {training_result.get('error')}")
                progress_state['error_count'] += 1
                update_job_training_state(
                    task_id,
                    error_count=progress_state['error_count'],
                    success_count=progress_state['success_count']
                )
                return 0.0

            # Evaluate with the selected optimization metric
            eval_result = training_service.evaluate_model(
                model, test_series,
                covariates=test_covariates,
                optimize_metric=optimize_metric
            )

            if 'error' in eval_result:
                progress_state['error_count'] += 1
                update_job_training_state(
                    task_id,
                    error_count=progress_state['error_count'],
                    success_count=progress_state['success_count']
                )
                return 0.0

            # Calculate fitness based on metric type
            if optimize_metric == 'mape':
                # Lower MAPE is better - convert to fitness (higher is better)
                mape_val = eval_result.get('mape', 100)
                if mape_val is None:
                    mape_val = 100
                fitness = 1.0 / (1.0 + mape_val / 100)
            elif optimize_metric in {'mae', 'rmse'}:
                # Lower is better for regression metrics
                metric_val = eval_result.get(optimize_metric, 100)
                fitness = 1.0 / (1.0 + metric_val)
            else:
                # Classification metrics (f1_score, accuracy, etc.) - higher is better
                fitness = eval_result.get(optimize_metric, 0.0)

            # Capture training history before it gets reset
            training_history = get_epoch_history(task_id)

            # Track this individual for visualization
            individual_record = {
                'generation': gen,
                'individual': individual_num,
                'model_type': model_type,
                'params': model_params,
                'loss_function': loss_function_type,
                'fitness': fitness,
                'metrics': eval_result,
                'training_history': training_history
            }
            progress_state['all_individuals'].append(individual_record)

            # Also add to jobs_store for real-time UI access
            add_individual_to_job(task_id, individual_record)

            # Save model for this generation
            save_result = save_generation_model(
                task_id=task_id,
                model=model,
                generation=gen,
                individual=individual_num,
                model_type=model_type,
                fitness=fitness,
                params=model_params,
                metrics=eval_result,
                training_service=training_service,
                training_history=training_history,
                feature_columns=feature_columns
            )
            if save_result is None:
                # Model save failed - increment error counter
                progress_state['error_count'] += 1
                update_job_training_state(
                    task_id,
                    error_count=progress_state['error_count'],
                    success_count=progress_state['success_count']
                )

            # Track best
            if fitness > progress_state['best_fitness']:
                progress_state['best_fitness'] = fitness
                progress_state['best_model_type'] = model_type
                progress_state['best_model_params'] = model_params
                best_model[0] = model
                best_metrics[0] = eval_result
                update_job_progress(
                    task_id, current_progress,
                    f"Gen {gen}/{generations}, New best: {model_type.upper()} fitness={fitness:.4f}"
                )
                # Update jobs_store with new best fitness
                update_job_training_state(
                    task_id,
                    best_fitness=fitness
                )

            progress_state['success_count'] += 1
            update_job_training_state(
                task_id,
                error_count=progress_state['error_count'],
                success_count=progress_state['success_count']
            )
            return fitness

        except Exception as e:
            logger.error(f"Fitness evaluation failed for {model_type}: {e}", exc_info=True)
            progress_state['error_count'] += 1
            update_job_training_state(
                task_id,
                error_count=progress_state['error_count'],
                success_count=progress_state['success_count']
            )
            return 0.0

    def on_generation_start(gen: int):
        """Called BEFORE evaluating individuals in a generation."""
        progress_state['current_generation'] = gen
        progress_state['current_individual'] = 0

    def ga_callback(gen: int, best_fitness: float, best_params: Dict):
        """Called after each generation completes."""
        if check_cancelled():
            raise InterruptedError("Task cancelled")

        # Cleanup non-elite models to save disk space
        elitism_pct = genetic_config.get('elitismPercent', 10.0)
        cleanup_non_elite_models(
            task_id=task_id,
            all_individuals=progress_state['all_individuals'],
            elitism_percent=elitism_pct,
            population_size=population_size
        )

        # Update training state for UI
        update_job_training_state(
            task_id,
            current_generation=gen,
            current_individual=0,
            best_fitness=best_fitness
        )

        update_job_progress(
            task_id,
            progress_base + ((gen + 1) / generations) * progress_range,
            f"Gen {gen + 1}/{generations} complete, best fitness: {best_fitness:.4f}"
        )

    def checkpoint_callback(gen: int, population: list):
        """Save checkpoint after each generation for crash recovery."""
        checkpoint_data = optimizer.get_checkpoint_data(gen, population)
        checkpoint_data['all_individuals'] = progress_state['all_individuals']
        save_ga_checkpoint(task_id, checkpoint_data)

    # Check for existing checkpoint (for resume)
    checkpoint = load_ga_checkpoint(task_id)
    start_generation = 0
    initial_population = None

    if checkpoint:
        logger.info(f"Resuming from checkpoint: gen {checkpoint.get('generation', 0)}")
        update_job_progress(task_id, progress_base, f"Resuming from generation {checkpoint.get('generation', 0)}")
        progress_state['all_individuals'] = checkpoint.get('all_individuals', [])
        start_generation = checkpoint.get('generation', 0) + 1
        initial_population = checkpoint.get('population', [])
    else:
        update_job_progress(task_id, progress_base, f"Starting unified optimization (pop={population_size}, gens={generations})")

    optimizer = GeneticOptimizer(
        param_ranges=ga_param_ranges,
        population_size=population_size,
        n_generations=generations,
        crossover_prob=crossover_prob,
        mutation_prob=mutation_prob,
        early_stopping_generations=early_stopping,
        elitism_percent=elitism_percent,
        parallel_individuals=parallel_individuals,
    )

    # Restore optimizer state if resuming
    if checkpoint:
        optimizer.resume_from_checkpoint(checkpoint)

    try:
        opt_result = optimizer.optimize(
            fitness_function=fitness_function,
            callback=ga_callback,
            start_generation=start_generation,
            initial_population=initial_population,
            checkpoint_callback=checkpoint_callback,
            on_generation_start=on_generation_start
        )
        # Clear checkpoint on successful completion
        clear_ga_checkpoint(task_id)
    except InterruptedError:
        logger.info(f"Unified optimization cancelled for task {task_id}")
        return {
            'model_type': 'unified',
            'status': 'cancelled',
            'best_fitness': progress_state['best_fitness'],
            'all_individuals': progress_state['all_individuals']
        }

    # Get best model type from best params
    best_params = opt_result.get('best_params', {})
    best_model_type_idx = int(best_params.get('model_type_idx', 0))
    best_model_type = selected_models[best_model_type_idx % len(selected_models)]

    update_job_progress(
        task_id,
        progress_base + progress_range,
        f"Unified optimization complete. Best: {best_model_type.upper()} fitness={opt_result.get('best_fitness', 0):.4f}"
    )

    # Save elite models (top N based on elitism percent or default 10)
    elitism_percent = genetic_config.get('elitismPercent', 10.0)
    elite_paths = save_elite_models(
        task_id=task_id,
        all_individuals=progress_state['all_individuals'],
        elitism_percent=elitism_percent,
        population_size=population_size,
        default_elite_count=10
    )

    # Cleanup all generation models, keep only elite models
    cleanup_job_models(task_id, keep_best=True)

    # Get path to the best model (elite_01)
    model_path = elite_paths[0] if elite_paths else None

    return {
        'model_type': best_model_type,
        'status': 'completed',
        'best_params': best_params,
        'best_fitness': opt_result.get('best_fitness'),
        'generations_run': opt_result.get('generations_run'),
        'metrics': best_metrics[0],
        'model_path': model_path,
        'history': opt_result.get('history', [])[-5:],
        'all_individuals': progress_state['all_individuals'],  # For UI visualization
        'error_count': progress_state.get('error_count', 0),
        'success_count': progress_state.get('success_count', 0)
    }


def get_model_params(model_type: str, params: Dict, training_epochs: int = 10, output_chunk_length: int = 3) -> Dict:
    """Extract model-specific parameters from unified params.

    Args:
        model_type: Type of model (lstm, nbeats, etc.)
        params: Unified parameters from genetic optimization
        training_epochs: Number of epochs for training (from geneticConfig)
        output_chunk_length: Model's output chunk length
            - For RNN models (LSTM/GRU): Always 1 (single step prediction)
            - For multi-step models: prediction_horizon (multi-step prediction)
    """
    model_params = {
        'input_chunk_length': int(params.get('input_chunk_length', 24)),
        'output_chunk_length': output_chunk_length,
        'n_epochs': training_epochs,
        'batch_size': int(params.get('batch_size', 32)),
        'learning_rate': params.get('learning_rate', 0.001),
        'dropout': params.get('dropout', 0.1),
    }

    model_type_lower = model_type.lower()

    if model_type_lower in ['lstm', 'gru']:
        # RNN models use hidden_dim (single int)
        model_params['hidden_dim'] = int(params.get('hidden_dim_layer_1', 128))
        model_params['n_rnn_layers'] = int(params.get('n_rnn_layers', 2))
    elif model_type_lower == 'nbeats':
        model_params['num_stacks'] = int(params.get('num_stacks', 30))
        model_params['num_blocks'] = int(params.get('num_blocks', 1))
        model_params['num_layers'] = int(params.get('num_layers', 4))
        model_params['layer_widths'] = int(params.get('hidden_dim_layer_1', 256))
    elif model_type_lower == 'tcn':
        model_params['kernel_size'] = int(params.get('kernel_size', 3))
        model_params['num_filters'] = int(params.get('num_filters', 64))
        model_params['dilation_base'] = int(params.get('dilation_base', 2))
    elif model_type_lower == 'transformer':
        model_params['d_model'] = int(params.get('d_model', 64))
        model_params['nhead'] = int(params.get('nhead', 4))
        model_params['num_encoder_layers'] = int(params.get('num_encoder_layers', 2))
        model_params['num_decoder_layers'] = int(params.get('num_decoder_layers', 2))
        model_params['dim_feedforward'] = int(params.get('hidden_dim_layer_1', 128))
    elif model_type_lower == 'tft':
        model_params['hidden_size'] = int(params.get('hidden_dim_layer_1', 64))
        model_params['lstm_layers'] = int(params.get('n_rnn_layers', 1))
        model_params['num_attention_heads'] = int(params.get('nhead', 4))

    return model_params


def build_unified_param_ranges(
    selected_models: List[str],
    ranges: Dict[str, Any],
    max_input_chunk: int = 60
) -> Dict[str, Dict]:
    """
    Build parameter ranges for unified optimization including model_type_idx.
    """
    layers_min = ranges.get('layersMin', 1)
    layers_max = ranges.get('layersMax', 4)
    layer_size_min = ranges.get('layerSizeMin', 32)
    layer_size_max = ranges.get('layerSizeMax', 256)
    lr_min = ranges.get('learningRateMin', 0.0001)
    lr_max = ranges.get('learningRateMax', 0.01)
    dropout_min = ranges.get('dropoutMin', 0.0)
    dropout_max = ranges.get('dropoutMax', 0.5)

    # input_chunk_length constraints (RNN models now set training_length dynamically)
    input_chunk_max = max_input_chunk
    input_chunk_min = min(10, input_chunk_max)

    param_ranges = {
        # Model type selection (index into selected_models)
        'model_type_idx': {'min': 0, 'max': len(selected_models) - 1, 'step': 1, 'type': 'int'},
        # Common parameters
        'n_rnn_layers': {'min': layers_min, 'max': layers_max, 'step': 1, 'type': 'int'},
        'dropout': {'min': dropout_min, 'max': dropout_max, 'step': 0.1, 'type': 'float'},
        'learning_rate': {'min': lr_min, 'max': lr_max, 'step': 0.0001, 'type': 'float'},
        'batch_size': {'min': 16, 'max': 128, 'step': 16, 'type': 'int'},
        'input_chunk_length': {'min': input_chunk_min, 'max': input_chunk_max, 'step': 5, 'type': 'int'},
    }

    # Add hidden dim layers (scaled appropriately per model during evaluation)
    for i in range(1, 5):
        param_ranges[f'hidden_dim_layer_{i}'] = {
            'min': layer_size_min,
            'max': layer_size_max,
            'step': 16,
            'type': 'int'
        }

    # Model-specific params (used by respective models)
    param_ranges['num_stacks'] = {'min': 10, 'max': 50, 'step': 10, 'type': 'int'}
    param_ranges['num_blocks'] = {'min': 1, 'max': 3, 'step': 1, 'type': 'int'}
    param_ranges['num_layers'] = {'min': 2, 'max': 6, 'step': 1, 'type': 'int'}
    param_ranges['kernel_size'] = {'min': 2, 'max': 7, 'step': 1, 'type': 'int'}
    param_ranges['num_filters'] = {'min': 32, 'max': 128, 'step': 16, 'type': 'int'}
    param_ranges['dilation_base'] = {'min': 2, 'max': 4, 'step': 1, 'type': 'int'}
    param_ranges['d_model'] = {'min': 32, 'max': 256, 'step': 32, 'type': 'int'}
    param_ranges['nhead'] = {'min': 2, 'max': 8, 'step': 2, 'type': 'int'}
    param_ranges['num_encoder_layers'] = {'min': 1, 'max': 4, 'step': 1, 'type': 'int'}
    param_ranges['num_decoder_layers'] = {'min': 1, 'max': 4, 'step': 1, 'type': 'int'}

    return param_ranges


def build_param_ranges(model_type: str, ranges: Dict[str, Any], max_input_chunk: int = 60) -> Dict[str, Dict]:
    """
    Build genetic algorithm parameter ranges from job config.

    Layer size scaling from base (user specifies Transformer/TCN/TFT base size):
    - LSTM/GRU: 4x base (e.g., base=128 -> hidden_dim=512)
    - N-BEATS: 2x base (e.g., base=128 -> layer_widths=256)
    - Transformer/TCN/TFT: 1x base (e.g., base=128 -> d_model/num_filters/hidden_size=128)

    Args:
        model_type: Model type
        ranges: Parameter ranges from job config
        max_input_chunk: Maximum allowed input_chunk_length based on data length

    Returns:
        Parameter ranges for GeneticOptimizer
    """
    # Extract ranges from job config
    layers_min = ranges.get('layersMin', 1)
    layers_max = ranges.get('layersMax', 4)
    layer_size_min = ranges.get('layerSizeMin', 32)
    layer_size_max = ranges.get('layerSizeMax', 256)
    lr_min = ranges.get('learningRateMin', 0.0001)
    lr_max = ranges.get('learningRateMax', 0.01)
    dropout_min = ranges.get('dropoutMin', 0.0)
    dropout_max = ranges.get('dropoutMax', 0.5)

    # Constrain input_chunk_length based on data length and model type
    # For RNN models (LSTM/GRU): training_length defaults to 24, so input_chunk_length must be <= 24
    model_type_lower = model_type.lower()
    if model_type_lower in ['lstm', 'gru']:
        # RNNModel has training_length=24 by default, input_chunk must be <= training_length
        input_chunk_max = min(max_input_chunk, 24)
    else:
        input_chunk_max = min(max_input_chunk, 60)
    input_chunk_min = min(10, input_chunk_max)

    # Apply model-specific layer size scaling
    # User specifies layer size for Transformer (base), then:
    # - Transformer/TCN/TFT: use as-is (1x)
    # - N-BEATS: multiply by 2 (2x)
    # - LSTM/GRU: multiply by 4 (4x)
    if model_type_lower in ['lstm', 'gru']:
        # LSTM/GRU: 4x the base layer size
        layer_size_multiplier = 4
    elif model_type_lower in ['nbeats']:
        # N-BEATS: 2x the base layer size
        layer_size_multiplier = 2
    else:
        # Transformer/TCN/TFT: use base layer size (1x)
        layer_size_multiplier = 1

    effective_layer_size_min = layer_size_min * layer_size_multiplier
    effective_layer_size_max = layer_size_max * layer_size_multiplier

    param_ranges = {
        'n_rnn_layers': {'min': layers_min, 'max': layers_max, 'step': 1, 'type': 'int'},
        'dropout': {'min': dropout_min, 'max': dropout_max, 'step': 0.1, 'type': 'float'},
        'learning_rate': {'min': lr_min, 'max': lr_max, 'step': 0.0001, 'type': 'float'},
        'batch_size': {'min': 16, 'max': 128, 'step': 16, 'type': 'int'},
        'input_chunk_length': {'min': input_chunk_min, 'max': input_chunk_max, 'step': 5, 'type': 'int'}
    }

    # Add per-layer hidden dimensions with model-appropriate sizes
    for i in range(1, 5):  # Up to 4 layers
        param_ranges[f'hidden_dim_layer_{i}'] = {
            'min': effective_layer_size_min,
            'max': effective_layer_size_max,
            'step': 16,
            'type': 'int'
        }

    # Model-specific adjustments
    if model_type_lower == 'nbeats':
        param_ranges['num_stacks'] = {'min': 10, 'max': 50, 'step': 10, 'type': 'int'}
        param_ranges['num_blocks'] = {'min': 1, 'max': 3, 'step': 1, 'type': 'int'}
        param_ranges['num_layers'] = {'min': 2, 'max': 6, 'step': 1, 'type': 'int'}
    elif model_type_lower == 'tcn':
        param_ranges['kernel_size'] = {'min': 2, 'max': 7, 'step': 1, 'type': 'int'}
        param_ranges['num_filters'] = {'min': 32, 'max': 128, 'step': 16, 'type': 'int'}
        param_ranges['dilation_base'] = {'min': 2, 'max': 4, 'step': 1, 'type': 'int'}
    elif model_type_lower == 'transformer':
        param_ranges['d_model'] = {'min': 32, 'max': 256, 'step': 32, 'type': 'int'}
        param_ranges['nhead'] = {'min': 2, 'max': 8, 'step': 2, 'type': 'int'}
        param_ranges['num_encoder_layers'] = {'min': 1, 'max': 4, 'step': 1, 'type': 'int'}
        param_ranges['num_decoder_layers'] = {'min': 1, 'max': 4, 'step': 1, 'type': 'int'}

    # Add seq_len optimization if enabled
    if ranges.get('optimizeSeqLen', False):
        seq_min = ranges.get('seqLenMin', 24)
        seq_max = ranges.get('seqLenMax', 48)
        seq_step = ranges.get('seqLenStep', 12)

        # Calculate number of discrete seq_len values
        n_seq_values = int((seq_max - seq_min) / seq_step) + 1
        if n_seq_values > 1:
            # Store as index that will be decoded to actual seq_len
            # seq_len = seq_min + seq_len_idx * seq_step
            param_ranges['seq_len_idx'] = {
                'min': 0,
                'max': n_seq_values - 1,
                'step': 1,
                'type': 'int'
            }
            # Store the range info for decoding later
            param_ranges['_seq_len_config'] = {
                'min': seq_min,
                'max': seq_max,
                'step': seq_step,
                'type': 'meta'  # Not a real parameter, just config
            }
            logger.info(f"SeqLen optimization enabled: {seq_min}-{seq_max} step {seq_step} ({n_seq_values} values)")

    return param_ranges
