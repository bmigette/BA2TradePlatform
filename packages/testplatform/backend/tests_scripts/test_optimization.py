#!/usr/bin/env python3
"""
Test Optimization Script

Runs a quick optimization job with 1 epoch to verify all model types work correctly.
Uses the same job profile from the database but with minimal settings.

Usage:
    ./venv/bin/python scripts/test_optimization.py
"""

import sys
import os
import logging

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Reduce noise from other loggers
logging.getLogger('darts').setLevel(logging.WARNING)
logging.getLogger('pytorch_lightning').setLevel(logging.WARNING)
logging.getLogger('lightning').setLevel(logging.WARNING)


def run_test():
    """Run optimization test with minimal settings."""
    from app.services.job_handler import handle_training_job

    # Test payload - based on DB job but with minimal settings
    test_payload = {
        "dataset_ids": [1],
        "selected_models": ["lstm", "nbeats", "transformer"],
        "parameter_ranges": {
            "layersMin": 1,
            "layersMax": 2,
            "layersStep": 1,
            "layerSizeMin": 32,
            "layerSizeMax": 64,
            "layerSizeStep": 32,
            "learningRateMin": 0.001,
            "learningRateMax": 0.01,
            "learningRateStep": 0.009,
            "dropoutMin": 0.1,
            "dropoutMax": 0.2,
            "dropoutStep": 0.1,
        },
        "prediction_targets": [],  # Use Close price directly for simplicity
        "train_test_split": 80,
        "genetic_config": {
            "populationSize": 3,  # Small population
            "generations": 2,     # Few generations
            "elitismPercent": 10.0,
            "crossoverProb": 0.7,
            "mutationProb": 0.2,
            "earlyStoppingGenerations": 2,
            "trainingEpochs": 1   # 1 epoch only for quick testing
        },
        "metrics_config": {
            "optimizeMetric": "mape"
        }
    }

    task_id = "test-optimization-001"

    print("\n" + "=" * 60)
    print("OPTIMIZATION TEST")
    print("=" * 60)
    print(f"Models: {test_payload['selected_models']}")
    print(f"Population: {test_payload['genetic_config']['populationSize']}")
    print(f"Generations: {test_payload['genetic_config']['generations']}")
    print(f"Epochs: {test_payload['genetic_config']['trainingEpochs']}")
    print("=" * 60 + "\n")

    try:
        result = handle_training_job(task_id, test_payload)

        print("\n" + "=" * 60)
        print("RESULT")
        print("=" * 60)
        print(f"Status: {result.get('status')}")
        print(f"Best fitness: {result.get('best_fitness', 'N/A')}")
        print(f"Error count: {result.get('error_count', 0)}")
        print(f"Success count: {result.get('success_count', 0)}")

        if result.get('status') == 'failed':
            print(f"Error: {result.get('error')}")
            return False

        # Check individual results
        all_individuals = result.get('all_individuals', [])
        print(f"\nTotal individuals evaluated: {len(all_individuals)}")

        # Group by model type
        by_model = {}
        for ind in all_individuals:
            model_type = ind.get('model_type', 'unknown')
            if model_type not in by_model:
                by_model[model_type] = {'count': 0, 'successes': 0, 'best_fitness': 0}
            by_model[model_type]['count'] += 1
            if ind.get('fitness', 0) > 0:
                by_model[model_type]['successes'] += 1
                by_model[model_type]['best_fitness'] = max(
                    by_model[model_type]['best_fitness'],
                    ind.get('fitness', 0)
                )

        print("\nResults by model type:")
        for model_type, stats in by_model.items():
            print(f"  {model_type.upper()}: {stats['successes']}/{stats['count']} succeeded, best={stats['best_fitness']:.4f}")

        # Determine if test passed
        error_count = result.get('error_count', 0)
        success_count = result.get('success_count', 0)
        total = error_count + success_count

        if total == 0:
            print("\n[FAIL] No training attempts were made")
            return False

        success_rate = success_count / total * 100
        print(f"\nSuccess rate: {success_rate:.1f}% ({success_count}/{total})")

        if success_rate >= 50:
            print("\n[PASS] Optimization test passed!")
            return True
        else:
            print(f"\n[FAIL] Too many errors ({error_count} errors)")
            return False

    except Exception as e:
        print(f"\n[ERROR] Test failed with exception: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_individual_models():
    """Test each model type individually to isolate issues."""
    from app.services.darts_models import DartsModelService as MLModelsService
    from app.services.darts_training import DartsTrainingService as TrainingService
    from app.services.job_handler import load_dataset
    import pandas as pd

    print("\n" + "=" * 60)
    print("INDIVIDUAL MODEL TESTS")
    print("=" * 60)

    # Load dataset
    df = load_dataset(1)
    if df is None:
        print("[ERROR] Could not load dataset 1")
        return False

    print(f"Loaded dataset with {len(df)} rows")

    # Initialize services
    ml_service = MLModelsService()
    training_service = TrainingService()

    # Prepare data
    feature_cols = [c for c in df.columns if c not in ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']][:5]
    print(f"Feature columns: {feature_cols[:3]}...")

    # Get dataset timeframe from DB
    import sqlite3
    conn = sqlite3.connect('dl_forecasting.db')
    cursor = conn.cursor()
    cursor.execute('SELECT timeframe FROM datasets WHERE id=1')
    row = cursor.fetchone()
    timeframe = row[0] if row else '4h'
    conn.close()
    print(f"Dataset timeframe: {timeframe}")

    # Use prepare_data_split to ensure train/test share the same index space
    # This is required for Darts metric functions to work correctly
    train_series, test_series, train_cov, test_cov = training_service.prepare_data_split(
        df, train_ratio=0.8, target_column='Close', feature_columns=feature_cols, timeframe=timeframe
    )

    print(f"Train series length: {len(train_series)}, Test series length: {len(test_series)}")
    print(f"Covariates: {'Yes' if train_cov is not None else 'No'}")

    models_to_test = ['lstm', 'nbeats', 'transformer']
    results = {}

    for model_type in models_to_test:
        print(f"\n--- Testing {model_type.upper()} ---")
        try:
            # Create model with minimal params
            params = {
                'input_chunk_length': 20,
                'output_chunk_length': 7,
                'n_epochs': 1,
                'batch_size': 32,
                'hidden_dim': 32,
                'n_rnn_layers': 1,
                'learning_rate': 0.001,
                'dropout': 0.1,
                # NBEATS specific
                'num_stacks': 10,
                'num_blocks': 1,
                'num_layers': 2,
                'layer_widths': 32,
                # Transformer specific
                'd_model': 32,
                'nhead': 2,
                'num_encoder_layers': 1,
                'num_decoder_layers': 1,
                'dim_feedforward': 32,
            }

            model = ml_service.create_model(model_type, params)
            print(f"  Created model: OK")

            # Train
            train_result = training_service.train_model(
                model, train_series, covariates=train_cov, verbose=False
            )

            if train_result.get('status') == 'failed':
                print(f"  Training: FAILED - {train_result.get('error')}")
                results[model_type] = 'TRAIN_FAILED'
                continue

            print(f"  Training: OK ({train_result.get('training_time_seconds', 0):.1f}s)")

            # Evaluate
            eval_result = training_service.evaluate_model(model, test_series, covariates=test_cov)

            if 'error' in eval_result:
                print(f"  Evaluation: FAILED - {eval_result.get('error')}")
                results[model_type] = 'EVAL_FAILED'
                continue

            print(f"  Evaluation: OK (MAPE={eval_result.get('mape', 0):.4f})")
            results[model_type] = 'OK'

        except Exception as e:
            print(f"  Exception: {e}")
            results[model_type] = f'EXCEPTION: {e}'

    print("\n" + "-" * 40)
    print("Summary:")
    all_ok = True
    for model_type, status in results.items():
        icon = "[OK]" if status == 'OK' else "[FAIL]"
        print(f"  {icon} {model_type.upper()}: {status}")
        if status != 'OK':
            all_ok = False

    return all_ok


if __name__ == "__main__":
    print("\n" + "#" * 60)
    print("# TRAINING OPTIMIZATION TEST SUITE")
    print("#" * 60)

    # First test individual models
    individual_ok = test_individual_models()

    if not individual_ok:
        print("\n[!] Individual model tests failed, skipping full optimization test")
        sys.exit(1)

    # Then run full optimization
    optimization_ok = run_test()

    if optimization_ok:
        print("\n" + "#" * 60)
        print("# ALL TESTS PASSED")
        print("#" * 60)
        sys.exit(0)
    else:
        print("\n" + "#" * 60)
        print("# TESTS FAILED")
        print("#" * 60)
        sys.exit(1)
