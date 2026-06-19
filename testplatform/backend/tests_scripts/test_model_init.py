#!/usr/bin/env python
"""
Test script for model initialization with Darts.

Tests that all ML models can be initialized and trained with a subset of data.
Uses only 1 month of data for faster testing.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import logging
from datetime import datetime, timedelta

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_model_initialization():
    """Test that all models can be initialized and trained."""

    # Find the dataset file
    dataset_path = "datasets/AAPL_4h_20260126_182735.csv"
    if not os.path.exists(dataset_path):
        # Try alternative paths
        for path in ["backend/datasets/AAPL_4h_20260126_182735.csv", "../datasets/AAPL_4h_20260126_182735.csv"]:
            if os.path.exists(path):
                dataset_path = path
                break
        else:
            logger.error("Dataset file not found. Please provide AAPL_4h_20260126_182735.csv")
            return False

    logger.info(f"Loading dataset from: {dataset_path}")
    df = pd.read_csv(dataset_path)
    df['Date'] = pd.to_datetime(df['Date'])

    logger.info(f"Full dataset: {len(df)} rows, date range: {df['Date'].min()} to {df['Date'].max()}")

    # Take only 1 month of data (last month)
    one_month_ago = df['Date'].max() - timedelta(days=30)
    df_subset = df[df['Date'] >= one_month_ago].copy()
    logger.info(f"Subset (1 month): {len(df_subset)} rows, date range: {df_subset['Date'].min()} to {df_subset['Date'].max()}")

    if len(df_subset) < 50:
        logger.warning(f"Very few data points ({len(df_subset)}), using last 200 rows instead")
        df_subset = df.tail(200).copy()
        logger.info(f"Using last 200 rows: {df_subset['Date'].min()} to {df_subset['Date'].max()}")

    # Test imports
    logger.info("Testing imports...")
    try:
        from darts import TimeSeries
        from darts.dataprocessing.transformers import Scaler
        logger.info("✓ Darts imported successfully")
    except ImportError as e:
        logger.error(f"✗ Failed to import Darts: {e}")
        return False

    try:
        import torch
        gpu_available = torch.cuda.is_available()
        if gpu_available:
            logger.info(f"✓ PyTorch with GPU: {torch.cuda.get_device_name(0)}")
        else:
            logger.info("✓ PyTorch (CPU only)")
    except ImportError as e:
        logger.error(f"✗ Failed to import PyTorch: {e}")
        return False

    # Test TrainingService
    logger.info("\nTesting TrainingService.prepare_data()...")
    try:
        from app.services.darts_training import DartsTrainingService as TrainingService
        training_service = TrainingService()

        target_series, covariates = training_service.prepare_data(
            df_subset,
            target_column='Close',
            timeframe='4h'  # Important: specify the timeframe
        )

        logger.info(f"✓ Data prepared successfully")
        logger.info(f"  Target series length: {len(target_series)}")
        logger.info(f"  Target series freq: {target_series.freq_str}")

    except Exception as e:
        logger.error(f"✗ Failed to prepare data: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Import models
    from darts.models import RNNModel, NBEATSModel, TCNModel, TransformerModel

    # Test each model initialization
    logger.info("\nTesting model initialization...")

    results = {}

    # Convert target series to float32 for MPS compatibility
    target_series = target_series.astype('float32')

    # Test LSTM
    logger.info("\n--- Testing LSTM ---")
    try:
        model = RNNModel(model="LSTM", input_chunk_length=24, output_chunk_length=1, n_epochs=1, force_reset=True)
        logger.info("✓ LSTM initialized")
        logger.info("  Fitting LSTM (1 epoch)...")
        model.fit(target_series)
        logger.info("✓ LSTM fit successfully")
        pred = model.predict(1)
        logger.info(f"✓ LSTM prediction: {pred.values()[0][0]:.4f}")
        results['LSTM'] = 'SUCCESS'
    except Exception as e:
        logger.error(f"✗ LSTM failed: {e}")
        import traceback
        traceback.print_exc()
        results['LSTM'] = f'FAILED: {str(e)[:100]}'

    # Test GRU
    logger.info("\n--- Testing GRU ---")
    try:
        model = RNNModel(model="GRU", input_chunk_length=24, output_chunk_length=1, n_epochs=1, force_reset=True)
        logger.info("✓ GRU initialized")
        logger.info("  Fitting GRU (1 epoch)...")
        model.fit(target_series)
        logger.info("✓ GRU fit successfully")
        pred = model.predict(1)
        logger.info(f"✓ GRU prediction: {pred.values()[0][0]:.4f}")
        results['GRU'] = 'SUCCESS'
    except Exception as e:
        logger.error(f"✗ GRU failed: {e}")
        import traceback
        traceback.print_exc()
        results['GRU'] = f'FAILED: {str(e)[:100]}'

    # Test NBEATS
    logger.info("\n--- Testing NBEATS ---")
    try:
        model = NBEATSModel(input_chunk_length=24, output_chunk_length=1, n_epochs=1)
        logger.info("✓ NBEATS initialized")
        logger.info("  Fitting NBEATS (1 epoch)...")
        model.fit(target_series)
        logger.info("✓ NBEATS fit successfully")
        pred = model.predict(1)
        logger.info(f"✓ NBEATS prediction: {pred.values()[0][0]:.4f}")
        results['NBEATS'] = 'SUCCESS'
    except Exception as e:
        logger.error(f"✗ NBEATS failed: {e}")
        import traceback
        traceback.print_exc()
        results['NBEATS'] = f'FAILED: {str(e)[:100]}'

    # Test TCN
    logger.info("\n--- Testing TCN ---")
    try:
        model = TCNModel(input_chunk_length=24, output_chunk_length=1, n_epochs=1)
        logger.info("✓ TCN initialized")
        logger.info("  Fitting TCN (1 epoch)...")
        model.fit(target_series)
        logger.info("✓ TCN fit successfully")
        pred = model.predict(1)
        logger.info(f"✓ TCN prediction: {pred.values()[0][0]:.4f}")
        results['TCN'] = 'SUCCESS'
    except Exception as e:
        logger.error(f"✗ TCN failed: {e}")
        import traceback
        traceback.print_exc()
        results['TCN'] = f'FAILED: {str(e)[:100]}'

    # Test Transformer
    logger.info("\n--- Testing Transformer ---")
    try:
        model = TransformerModel(input_chunk_length=24, output_chunk_length=1, n_epochs=1)
        logger.info("✓ Transformer initialized")
        logger.info("  Fitting Transformer (1 epoch)...")
        model.fit(target_series)
        logger.info("✓ Transformer fit successfully")
        pred = model.predict(1)
        logger.info(f"✓ Transformer prediction: {pred.values()[0][0]:.4f}")
        results['Transformer'] = 'SUCCESS'
    except Exception as e:
        logger.error(f"✗ Transformer failed: {e}")
        import traceback
        traceback.print_exc()
        results['Transformer'] = f'FAILED: {str(e)[:100]}'

    # Summary
    logger.info("\n" + "="*60)
    logger.info("SUMMARY")
    logger.info("="*60)

    success_count = sum(1 for v in results.values() if v == 'SUCCESS')
    total_count = len(results)

    for model_name, status in results.items():
        icon = "✓" if status == 'SUCCESS' else "✗"
        logger.info(f"  {icon} {model_name}: {status}")

    logger.info(f"\nResult: {success_count}/{total_count} models passed")

    return success_count == total_count


if __name__ == "__main__":
    success = test_model_initialization()
    sys.exit(0 if success else 1)
