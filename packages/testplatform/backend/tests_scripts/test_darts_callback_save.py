#!/usr/bin/env python3
"""
Test script to verify Darts callback and model saving behavior.

Tests:
1. Model with callback - can it be saved?
2. Different save methods (save vs save weights only)
3. Proper callback patterns that work with pickling
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import tempfile
import traceback
from pathlib import Path

# Import Darts
from darts import TimeSeries
from darts.models import RNNModel, NBEATSModel

# Import PyTorch Lightning callback
from pytorch_lightning.callbacks import Callback


class SimpleCallback(Callback):
    """A simple callback WITHOUT a stored function reference."""

    def __init__(self):
        super().__init__()
        self.epoch_count = 0

    def on_train_epoch_end(self, trainer, pl_module):
        self.epoch_count += 1
        print(f"  Epoch {self.epoch_count}/{trainer.max_epochs} completed")


class CallbackWithFunction(Callback):
    """A callback WITH a stored function reference (the problematic pattern)."""

    def __init__(self, on_epoch_end: callable):
        super().__init__()
        self.on_epoch_end_fn = on_epoch_end  # This is the problem!

    def on_train_epoch_end(self, trainer, pl_module):
        if self.on_epoch_end_fn:
            self.on_epoch_end_fn(trainer.current_epoch + 1, trainer.max_epochs)


class SerializableCallback(Callback):
    """A callback that IS serializable by not storing the function."""

    def __init__(self):
        super().__init__()
        self.metrics = []

    def on_train_epoch_end(self, trainer, pl_module):
        epoch = trainer.current_epoch + 1
        metrics = {}
        if trainer.logged_metrics:
            for key, value in trainer.logged_metrics.items():
                try:
                    metrics[key] = float(value.item() if hasattr(value, 'item') else value)
                except:
                    pass
        self.metrics.append({'epoch': epoch, 'metrics': metrics})
        print(f"  Epoch {epoch}/{trainer.max_epochs} - metrics: {metrics}")

    def __getstate__(self):
        # Pickle-friendly: just return the metrics
        return {'metrics': self.metrics}

    def __setstate__(self, state):
        self.metrics = state.get('metrics', [])


def create_dummy_series(length=200):
    """Create dummy time series for testing."""
    values = np.random.randn(length).cumsum() + 100
    return TimeSeries.from_values(values.reshape(-1, 1))


def test_save_without_callback():
    """Test 1: Model without callback saves fine."""
    print("\n" + "="*60)
    print("TEST 1: Model WITHOUT callback")
    print("="*60)

    series = create_dummy_series()
    train, test = series[:150], series[150:]

    model = RNNModel(
        model='LSTM',
        input_chunk_length=10,
        output_chunk_length=5,
        n_epochs=2,
        batch_size=16,
        hidden_dim=16,
        n_rnn_layers=1,
        pl_trainer_kwargs={'accelerator': 'cpu', 'enable_progress_bar': False}
    )

    print("Training model...")
    model.fit(train, verbose=False)

    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = Path(tmpdir) / "model.pt"
        try:
            model.save(str(model_path))
            print(f"✓ Model saved successfully to {model_path}")

            # Try loading
            loaded = RNNModel.load(str(model_path))
            print(f"✓ Model loaded successfully")
            return True
        except Exception as e:
            print(f"✗ FAILED: {e}")
            traceback.print_exc()
            return False


def test_save_with_simple_callback():
    """Test 2: Model with simple callback (no function reference)."""
    print("\n" + "="*60)
    print("TEST 2: Model WITH simple callback (no function ref)")
    print("="*60)

    series = create_dummy_series()
    train, test = series[:150], series[150:]

    callback = SimpleCallback()

    model = RNNModel(
        model='LSTM',
        input_chunk_length=10,
        output_chunk_length=5,
        n_epochs=2,
        batch_size=16,
        hidden_dim=16,
        n_rnn_layers=1,
        pl_trainer_kwargs={
            'accelerator': 'cpu',
            'enable_progress_bar': False,
            'callbacks': [callback]
        }
    )

    print("Training model...")
    model.fit(train, verbose=False)
    print(f"Callback recorded {callback.epoch_count} epochs")

    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = Path(tmpdir) / "model.pt"
        try:
            model.save(str(model_path))
            print(f"✓ Model saved successfully to {model_path}")
            return True
        except Exception as e:
            print(f"✗ FAILED: {e}")
            traceback.print_exc()
            return False


def test_save_with_function_callback():
    """Test 3: Model with function callback (the problematic pattern)."""
    print("\n" + "="*60)
    print("TEST 3: Model WITH function callback (PROBLEMATIC)")
    print("="*60)

    series = create_dummy_series()
    train, test = series[:150], series[150:]

    # This is the pattern we're currently using
    def epoch_handler(epoch, total):
        print(f"  Epoch {epoch}/{total}")

    callback = CallbackWithFunction(on_epoch_end=epoch_handler)

    model = RNNModel(
        model='LSTM',
        input_chunk_length=10,
        output_chunk_length=5,
        n_epochs=2,
        batch_size=16,
        hidden_dim=16,
        n_rnn_layers=1,
        pl_trainer_kwargs={
            'accelerator': 'cpu',
            'enable_progress_bar': False,
            'callbacks': [callback]
        }
    )

    print("Training model...")
    model.fit(train, verbose=False)

    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = Path(tmpdir) / "model.pt"
        try:
            model.save(str(model_path))
            print(f"✓ Model saved successfully (unexpected!)")
            return True
        except Exception as e:
            print(f"✗ FAILED (as expected): {e}")
            return False


def test_save_with_serializable_callback():
    """Test 4: Model with serializable callback (__getstate__/__setstate__)."""
    print("\n" + "="*60)
    print("TEST 4: Model WITH serializable callback")
    print("="*60)

    series = create_dummy_series()
    train, test = series[:150], series[150:]

    callback = SerializableCallback()

    model = RNNModel(
        model='LSTM',
        input_chunk_length=10,
        output_chunk_length=5,
        n_epochs=2,
        batch_size=16,
        hidden_dim=16,
        n_rnn_layers=1,
        pl_trainer_kwargs={
            'accelerator': 'cpu',
            'enable_progress_bar': False,
            'callbacks': [callback]
        }
    )

    print("Training model...")
    model.fit(train, verbose=False)
    print(f"Callback recorded metrics: {callback.metrics}")

    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = Path(tmpdir) / "model.pt"
        try:
            model.save(str(model_path))
            print(f"✓ Model saved successfully to {model_path}")
            return True
        except Exception as e:
            print(f"✗ FAILED: {e}")
            traceback.print_exc()
            return False


def test_clear_callbacks_before_save():
    """Test 5: Clear callbacks from pl_trainer_kwargs before saving."""
    print("\n" + "="*60)
    print("TEST 5: Clear callbacks before saving (current workaround)")
    print("="*60)

    series = create_dummy_series()
    train, test = series[:150], series[150:]

    def epoch_handler(epoch, total):
        print(f"  Epoch {epoch}/{total}")

    callback = CallbackWithFunction(on_epoch_end=epoch_handler)

    model = RNNModel(
        model='LSTM',
        input_chunk_length=10,
        output_chunk_length=5,
        n_epochs=2,
        batch_size=16,
        hidden_dim=16,
        n_rnn_layers=1,
        pl_trainer_kwargs={
            'accelerator': 'cpu',
            'enable_progress_bar': False,
            'callbacks': [callback]
        }
    )

    print("Training model...")
    model.fit(train, verbose=False)

    # Clear callbacks before saving
    print("Clearing callbacks from pl_trainer_kwargs...")
    if hasattr(model, 'pl_trainer_kwargs') and model.pl_trainer_kwargs:
        model.pl_trainer_kwargs.pop('callbacks', None)
    if hasattr(model, 'trainer_params') and model.trainer_params:
        model.trainer_params.pop('callbacks', None)

    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = Path(tmpdir) / "model.pt"
        try:
            model.save(str(model_path))
            print(f"✓ Model saved successfully to {model_path}")

            # Verify it can be loaded
            loaded = RNNModel.load(str(model_path))
            print(f"✓ Model loaded successfully")

            # Verify predictions work
            pred = loaded.predict(n=5)
            print(f"✓ Predictions work: {len(pred)} values")
            return True
        except Exception as e:
            print(f"✗ FAILED: {e}")
            traceback.print_exc()
            return False


def test_remove_callback_reference():
    """Test 6: Remove the function reference from callback before saving."""
    print("\n" + "="*60)
    print("TEST 6: Remove function reference from callback before saving")
    print("="*60)

    series = create_dummy_series()
    train, test = series[:150], series[150:]

    def epoch_handler(epoch, total):
        print(f"  Epoch {epoch}/{total}")

    callback = CallbackWithFunction(on_epoch_end=epoch_handler)

    model = RNNModel(
        model='LSTM',
        input_chunk_length=10,
        output_chunk_length=5,
        n_epochs=2,
        batch_size=16,
        hidden_dim=16,
        n_rnn_layers=1,
        pl_trainer_kwargs={
            'accelerator': 'cpu',
            'enable_progress_bar': False,
            'callbacks': [callback]
        }
    )

    print("Training model...")
    model.fit(train, verbose=False)

    # Clear the function reference from the callback
    print("Clearing function reference from callback...")
    callback.on_epoch_end_fn = None

    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = Path(tmpdir) / "model.pt"
        try:
            model.save(str(model_path))
            print(f"✓ Model saved successfully to {model_path}")
            return True
        except Exception as e:
            print(f"✗ FAILED: {e}")
            traceback.print_exc()
            return False


if __name__ == "__main__":
    print("Darts Callback & Model Save Test")
    print("="*60)

    results = {}

    results['no_callback'] = test_save_without_callback()
    results['simple_callback'] = test_save_with_simple_callback()
    results['function_callback'] = test_save_with_function_callback()
    results['serializable_callback'] = test_save_with_serializable_callback()
    results['clear_callbacks'] = test_clear_callbacks_before_save()
    results['remove_function_ref'] = test_remove_callback_reference()

    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    for test_name, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {test_name}: {status}")

    print("\n" + "="*60)
    print("CONCLUSION")
    print("="*60)

    if results['serializable_callback']:
        print("BEST SOLUTION: Make callback serializable with __getstate__/__setstate__")
        print("This allows callbacks to work AND model to save without hacks.")
    elif results['clear_callbacks']:
        print("WORKAROUND: Clear callbacks from pl_trainer_kwargs before saving")
        print("This works but loses callback info in saved model.")
    elif results['remove_function_ref']:
        print("ALT WORKAROUND: Set callback.on_epoch_end_fn = None before saving")
    else:
        print("No working solution found!")
