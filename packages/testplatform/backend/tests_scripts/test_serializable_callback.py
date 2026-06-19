#!/usr/bin/env python3
"""Test the updated EpochProgressCallback is serializable."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
from pathlib import Path
import numpy as np

from darts import TimeSeries
from darts.models import RNNModel
from app.services.darts_models import EpochProgressCallback, LIGHTNING_CALLBACK_AVAILABLE

def test_serializable_callback():
    """Test that EpochProgressCallback can be serialized with model."""
    print("Testing serializable EpochProgressCallback...")
    print(f"LIGHTNING_CALLBACK_AVAILABLE: {LIGHTNING_CALLBACK_AVAILABLE}")

    if not LIGHTNING_CALLBACK_AVAILABLE:
        print("Lightning callbacks not available, skipping test")
        return

    # Create dummy series
    values = np.random.randn(200).cumsum() + 100
    series = TimeSeries.from_values(values.reshape(-1, 1))
    train = series[:150]

    # Create callback with a function reference
    epoch_metrics = []
    def on_epoch(epoch, max_epochs, metrics):
        epoch_metrics.append({'epoch': epoch, 'metrics': metrics})
        print(f"  Epoch {epoch}/{max_epochs}: {metrics}")

    callback = EpochProgressCallback(on_epoch_end=on_epoch)

    # Create and train model
    model = RNNModel(
        model='LSTM',
        input_chunk_length=10,
        output_chunk_length=5,
        n_epochs=3,
        batch_size=16,
        hidden_dim=16,
        n_rnn_layers=1,
        pl_trainer_kwargs={
            'accelerator': 'cpu',
            'enable_progress_bar': False,
            'callbacks': [callback]
        }
    )

    print("\nTraining model...")
    model.fit(train, verbose=False)
    print(f"Training complete. Epoch callback was called {len(epoch_metrics)} times")
    print(f"Callback metrics_history has {len(callback.metrics_history)} entries")

    # Try to save the model
    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = Path(tmpdir) / "model.pt"
        print(f"\nSaving model to {model_path}...")
        try:
            model.save(str(model_path))
            print("✓ Model saved successfully!")

            # Try loading
            print("Loading model...")
            loaded = RNNModel.load(str(model_path))
            print("✓ Model loaded successfully!")

            # Verify predictions work
            pred = loaded.predict(n=5)
            print(f"✓ Predictions work: {len(pred)} values")

            print("\n" + "="*50)
            print("SUCCESS! Serializable callback works correctly.")
            print("="*50)
            return True

        except Exception as e:
            print(f"✗ FAILED: {e}")
            import traceback
            traceback.print_exc()
            return False

if __name__ == "__main__":
    success = test_serializable_callback()
    sys.exit(0 if success else 1)
