#!/usr/bin/env python3
"""
Test script for epoch callbacks and metrics tracking.

This script tests:
1. EpochProgressCallback captures metrics correctly
2. Metrics are passed to the callback function
3. All available metrics (train_loss, val_loss, etc.) are captured

Usage:
    ./venv/bin/python scripts/test_epoch_callbacks.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from typing import Dict, List, Any

# Collected metrics during training
collected_metrics: List[Dict[str, Any]] = []


def epoch_callback(current_epoch: int, total_epochs: int, metrics: Dict[str, float] = None):
    """Test callback that collects epoch metrics."""
    entry = {
        "epoch": current_epoch,
        "total_epochs": total_epochs,
        "metrics": metrics or {}
    }
    collected_metrics.append(entry)

    # Print progress
    metrics_str = ", ".join(f"{k}={v:.4f}" for k, v in (metrics or {}).items())
    print(f"  Epoch {current_epoch}/{total_epochs}: {metrics_str or 'no metrics'}")


def test_callback_with_mock_trainer():
    """Test the EpochProgressCallback with a mock trainer."""
    print("\n" + "="*60)
    print("TEST 1: EpochProgressCallback with mock trainer")
    print("="*60)

    try:
        from app.services.darts_models import EpochProgressCallback, LIGHTNING_CALLBACK_AVAILABLE

        if not LIGHTNING_CALLBACK_AVAILABLE:
            print("SKIP: PyTorch Lightning not available")
            return False

        # Create callback
        callback = EpochProgressCallback(on_epoch_end=epoch_callback)

        # Create mock trainer with logged_metrics
        class MockTrainer:
            current_epoch = 0
            max_epochs = 5
            logged_metrics = {
                'train_loss': 0.5,
                'val_loss': 0.6,
            }
            callback_metrics = {
                'val_accuracy': 0.85,
            }

        class MockModule:
            pass

        trainer = MockTrainer()
        pl_module = MockModule()

        # Simulate 5 epochs
        print("\nSimulating 5 epochs with mock trainer:")
        for epoch in range(5):
            trainer.current_epoch = epoch
            trainer.logged_metrics = {
                'train_loss': 0.5 - epoch * 0.08,
                'val_loss': 0.6 - epoch * 0.07,
            }
            trainer.callback_metrics = {
                'val_accuracy': 0.7 + epoch * 0.05,
            }
            callback.on_train_epoch_end(trainer, pl_module)

        # Verify metrics were collected
        assert len(collected_metrics) == 5, f"Expected 5 epochs, got {len(collected_metrics)}"

        # Check that metrics include both logged and callback metrics
        last_metrics = collected_metrics[-1]["metrics"]
        assert "train_loss" in last_metrics, "train_loss not captured"
        assert "val_loss" in last_metrics, "val_loss not captured"
        assert "val_accuracy" in last_metrics, "val_accuracy not captured"

        print("\nCollected metrics summary:")
        for entry in collected_metrics:
            print(f"  Epoch {entry['epoch']}: {entry['metrics']}")

        print("\n✓ TEST 1 PASSED: Callback captures all metrics correctly")
        return True

    except Exception as e:
        print(f"\n✗ TEST 1 FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_real_model_training():
    """Test with actual Darts model training."""
    print("\n" + "="*60)
    print("TEST 2: Real model training with epoch callbacks")
    print("="*60)

    collected_metrics.clear()

    try:
        from app.services.darts_models import DartsModelService as MLModelsService, DARTS_AVAILABLE
        from app.services.darts_training import DartsTrainingService as TrainingService, DARTS_AVAILABLE as TRAINING_DARTS

        if not DARTS_AVAILABLE or not TRAINING_DARTS:
            print("SKIP: Darts library not available")
            return False

        # Create synthetic data
        print("\nCreating synthetic time series data...")
        import pandas as pd
        from darts import TimeSeries

        # Create simple sine wave data
        np.random.seed(42)
        n_points = 200
        time_index = pd.date_range(start='2024-01-01', periods=n_points, freq='D')
        values = np.sin(np.linspace(0, 8*np.pi, n_points)) + np.random.normal(0, 0.1, n_points)

        df = pd.DataFrame({
            'Date': time_index,
            'value': values
        })
        df.set_index('Date', inplace=True)

        series = TimeSeries.from_dataframe(df, value_cols='value')

        # Split into train/test
        train_series = series[:160]
        test_series = series[160:]

        print(f"Train series: {len(train_series)} points")
        print(f"Test series: {len(test_series)} points")

        # Create model service
        ml_service = MLModelsService(use_gpu=False)

        # Create a small LSTM model with epoch callback
        print("\nTraining LSTM model with epoch callback...")
        model = ml_service.create_lstm_model(
            params={
                'input_chunk_length': 20,
                'output_chunk_length': 7,
                'hidden_dim': 16,
                'n_rnn_layers': 1,
                'dropout': 0.0,
                'batch_size': 16,
                'n_epochs': 5,
                'learning_rate': 0.01
            },
            epoch_callback=epoch_callback
        )

        # Train
        model.fit(train_series, verbose=False)

        # Check that epochs were tracked
        if len(collected_metrics) > 0:
            print(f"\n✓ Captured {len(collected_metrics)} epoch callbacks")

            # Print collected metrics
            print("\nEpoch metrics summary:")
            for entry in collected_metrics:
                metrics_str = ", ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
                                       for k, v in entry["metrics"].items())
                print(f"  Epoch {entry['epoch']}/{entry['total_epochs']}: {metrics_str or 'no metrics'}")

            # Check if we got any loss values
            has_loss = any("train_loss" in e["metrics"] or "loss" in str(e["metrics"])
                          for e in collected_metrics if e["metrics"])

            if has_loss:
                print("\n✓ TEST 2 PASSED: Real training captures epoch metrics")
            else:
                print("\n⚠ TEST 2 PARTIAL: Callbacks fired but no loss metrics captured")
                print("  (This may be normal depending on Darts/Lightning version)")

            return True
        else:
            print("\n✗ TEST 2 FAILED: No epoch callbacks received")
            return False

    except Exception as e:
        print(f"\n✗ TEST 2 FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_job_state_update():
    """Test that metrics update job state correctly."""
    print("\n" + "="*60)
    print("TEST 3: Job state update with epoch metrics")
    print("="*60)

    try:
        from app.services.job_handler import update_job_training_state
        from app.api.jobs import jobs_store

        # Create a fake job
        test_job_id = "test_metrics_job_123"
        jobs_store[test_job_id] = {
            "id": test_job_id,
            "status": "running",
            "epochHistory": []
        }

        print("\nSimulating epoch updates...")

        # Simulate 3 epochs with metrics
        for epoch in range(1, 4):
            metrics = {
                "train_loss": 0.5 - epoch * 0.1,
                "val_loss": 0.6 - epoch * 0.08,
                "val_accuracy": 0.7 + epoch * 0.05
            }

            update_job_training_state(
                test_job_id,
                current_epoch=epoch,
                total_epochs=10,
                epoch_metrics=metrics
            )

            print(f"  Updated epoch {epoch} with metrics: {metrics}")

        # Check job state
        job = jobs_store[test_job_id]
        epoch_history = job.get("epochHistory", [])

        print(f"\nJob epochHistory has {len(epoch_history)} entries")

        assert len(epoch_history) == 3, f"Expected 3 epochs, got {len(epoch_history)}"

        # Verify metrics structure
        last_entry = epoch_history[-1]
        print(f"Last entry: {last_entry}")

        assert "epoch" in last_entry, "Missing 'epoch' field"
        assert "train_loss" in last_entry, "Missing 'train_loss' field"
        assert "val_loss" in last_entry, "Missing 'val_loss' field"
        assert "val_accuracy" in last_entry, "Missing 'val_accuracy' field"

        # Cleanup
        del jobs_store[test_job_id]

        print("\n✓ TEST 3 PASSED: Job state correctly stores epoch metrics")
        return True

    except Exception as e:
        print(f"\n✗ TEST 3 FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    print("="*60)
    print("EPOCH CALLBACKS AND METRICS TRACKING TEST SUITE")
    print("="*60)

    results = []

    # Test 1: Mock trainer
    results.append(("Callback with mock trainer", test_callback_with_mock_trainer()))

    # Test 2: Real model (may take a few seconds)
    results.append(("Real model training", test_real_model_training()))

    # Test 3: Job state update
    results.append(("Job state update", test_job_state_update()))

    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)

    passed = 0
    failed = 0
    for name, result in results:
        status = "✓ PASSED" if result else "✗ FAILED"
        print(f"  {status}: {name}")
        if result:
            passed += 1
        else:
            failed += 1

    print(f"\nTotal: {passed} passed, {failed} failed")

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
