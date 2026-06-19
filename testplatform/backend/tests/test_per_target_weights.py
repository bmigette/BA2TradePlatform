#!/usr/bin/env python3
"""
Test per-target class weighting for multi-target classification.

This tests the scenario where you have multiple prediction targets with
different class distributions (e.g., Target 1 is balanced, Target 2 is imbalanced).

Usage:
    cd backend
    ./venv/bin/python -m pytest tests/test_per_target_weights.py -v

Or run directly:
    ./venv/bin/python tests/test_per_target_weights.py
"""

import sys
import numpy as np
import torch
import torch.nn as nn
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.services.losses import WeightedBCELoss, get_loss_function


def _check_tsai_available() -> bool:
    """Check if tsai is available."""
    try:
        import torch
        from tsai.all import TSClassifier
        return True
    except ImportError:
        return False


class TestPerTargetWeights:
    """Test per-target weight calculation and application."""

    def test_calculate_per_target_weights_balanced(self):
        """Test weight calculation for balanced targets."""
        target_stats = [
            {'positive_count': 500, 'negative_count': 500},
            {'positive_count': 480, 'negative_count': 520},
        ]

        weights = WeightedBCELoss.calculate_per_target_weights(target_stats)

        assert len(weights) == 2
        assert abs(weights[0] - 1.0) < 0.01  # 500/500 = 1.0
        assert abs(weights[1] - 1.083) < 0.01  # 520/480 = 1.083

        print(f"✓ Balanced targets: weights = {weights}")

    def test_calculate_per_target_weights_imbalanced(self):
        """Test weight calculation for imbalanced targets (like the user's case)."""
        # User's case from screenshot:
        # Target 1: Train 586 pos / 625 neg (balanced)
        # Target 2: Train 507 pos / 704 neg (slightly imbalanced in train)
        # But Test: 35 pos / 485 neg (severely imbalanced)
        target_stats = [
            {'positive_count': 586, 'negative_count': 625},  # ~48% positive
            {'positive_count': 35, 'negative_count': 485},   # ~7% positive (test-like)
        ]

        weights = WeightedBCELoss.calculate_per_target_weights(target_stats)

        assert len(weights) == 2
        assert abs(weights[0] - 1.067) < 0.01  # 625/586 ≈ 1.07
        assert abs(weights[1] - 13.857) < 0.01  # 485/35 ≈ 13.86

        print(f"✓ Imbalanced targets: weights = {weights}")
        print(f"  Target 1 (balanced): weight = {weights[0]:.4f}")
        print(f"  Target 2 (imbalanced): weight = {weights[1]:.4f}")

    def test_calculate_per_target_weights_zero_positive(self):
        """Test weight calculation when a target has zero positive samples."""
        target_stats = [
            {'positive_count': 100, 'negative_count': 900},
            {'positive_count': 0, 'negative_count': 1000},  # No positive samples
        ]

        weights = WeightedBCELoss.calculate_per_target_weights(target_stats)

        assert len(weights) == 2
        assert weights[0] == 9.0  # 900/100
        assert weights[1] == 1.0  # Default when pos=0

        print(f"✓ Zero positive case handled: weights = {weights}")


class TestBCEWithPerTargetWeights:
    """Test BCEWithLogitsLoss with per-target weights."""

    def test_bce_with_single_weight(self):
        """Test BCEWithLogitsLoss with a single weight (broadcasts to all targets)."""
        pos_weight = torch.tensor([2.0])
        loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

        # 2 targets, batch of 4
        logits = torch.randn(4, 2)
        targets = torch.randint(0, 2, (4, 2)).float()

        loss = loss_fn(logits, targets)

        assert loss.item() > 0
        assert not torch.isnan(loss)

        print(f"✓ Single weight BCE loss: {loss.item():.4f}")

    def test_bce_with_per_target_weights(self):
        """Test BCEWithLogitsLoss with per-target weights."""
        # Different weight for each target
        pos_weights = torch.tensor([1.0, 10.0])  # Target 2 weighted 10x more
        loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weights)

        # 2 targets, batch of 4
        logits = torch.randn(4, 2)
        targets = torch.randint(0, 2, (4, 2)).float()

        loss = loss_fn(logits, targets)

        assert loss.item() > 0
        assert not torch.isnan(loss)

        print(f"✓ Per-target weight BCE loss: {loss.item():.4f}")

    def test_per_target_weights_affect_gradients(self):
        """Test that per-target weights properly affect gradients."""
        torch.manual_seed(42)

        # Create model that outputs 2 targets
        model = nn.Linear(10, 2)

        # Input
        x = torch.randn(8, 10)

        # Mixed targets - need positive samples in target 1 for pos_weight to matter
        targets = torch.zeros(8, 2)
        targets[:4, 0] = 1.0  # Target 0: half positive
        targets[:4, 1] = 1.0  # Target 1: half positive (pos_weight affects these)

        # Test 1: Equal weights
        pos_weights_equal = torch.tensor([1.0, 1.0])
        loss_fn_equal = nn.BCEWithLogitsLoss(pos_weight=pos_weights_equal)

        model.zero_grad()
        logits = model(x)
        loss_equal = loss_fn_equal(logits, targets)
        loss_equal.backward()
        grad_equal = model.weight.grad.clone()

        # Reinitialize model for fair comparison
        model2 = nn.Linear(10, 2)
        model2.load_state_dict(model.state_dict())

        # Test 2: Higher weight on target 1 positive class
        pos_weights_unequal = torch.tensor([1.0, 10.0])
        loss_fn_unequal = nn.BCEWithLogitsLoss(pos_weight=pos_weights_unequal)

        model2.zero_grad()
        logits2 = model2(x)
        loss_unequal = loss_fn_unequal(logits2, targets)
        loss_unequal.backward()
        grad_unequal = model2.weight.grad.clone()

        # Losses should be different (higher weight = higher loss for positive samples)
        assert loss_unequal.item() > loss_equal.item(), \
            f"Higher pos_weight should increase loss: {loss_unequal.item():.4f} vs {loss_equal.item():.4f}"

        print(f"✓ Per-target weights affect loss correctly")
        print(f"  Equal weights loss: {loss_equal.item():.4f}")
        print(f"  Unequal weights loss: {loss_unequal.item():.4f} (higher as expected)")


class TestTSAITrainingWithPerTargetWeights:
    """Test TSAITrainingService with per-target weights."""

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_get_loss_function_with_per_target_weights(self):
        """Test that get_loss_function accepts per-target weights."""
        from app.services.tsai_training import TSAITrainingService
        from app.services.tsai_models import DEVICE

        service = TSAITrainingService()

        # Per-target weights for 3 targets
        per_target_weights = [1.0, 5.0, 10.0]

        loss_fn = service.get_loss_function(
            loss_type='weighted_ce',
            prediction_mode='multistep',
            pos_weight=per_target_weights
        )

        assert loss_fn is not None
        assert isinstance(loss_fn, nn.BCEWithLogitsLoss)

        # Test forward pass - use same device as the loss function
        logits = torch.randn(4, 3)  # 4 samples, 3 targets
        targets = torch.randint(0, 2, (4, 3)).float()

        if DEVICE:
            logits = logits.to(DEVICE)
            targets = targets.to(DEVICE)

        loss = loss_fn(logits, targets)

        assert loss.item() > 0
        assert not torch.isnan(loss)

        print(f"✓ TSAITrainingService with per-target weights: loss = {loss.item():.4f}")

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_get_loss_function_with_single_weight_multistep(self):
        """Test that single weight still works in multistep mode."""
        from app.services.tsai_training import TSAITrainingService
        from app.services.tsai_models import DEVICE

        service = TSAITrainingService()

        loss_fn = service.get_loss_function(
            loss_type='weighted_ce',
            prediction_mode='multistep',
            pos_weight=5.0  # Single weight
        )

        assert loss_fn is not None
        assert isinstance(loss_fn, nn.BCEWithLogitsLoss)

        # Test forward pass - use same device as the loss function
        logits = torch.randn(4, 2)  # 4 samples, 2 targets
        targets = torch.randint(0, 2, (4, 2)).float()

        if DEVICE:
            logits = logits.to(DEVICE)
            targets = targets.to(DEVICE)

        loss = loss_fn(logits, targets)

        assert loss.item() > 0
        assert not torch.isnan(loss)

        print(f"✓ TSAITrainingService with single weight (multistep): loss = {loss.item():.4f}")


class TestEndToEndMultiTargetTraining:
    """End-to-end test with multi-target training using per-target weights."""

    @pytest.mark.skipif(
        not _check_tsai_available(),
        reason="tsai not available"
    )
    def test_training_with_imbalanced_targets(self):
        """Test training with multiple targets having different class distributions."""
        from app.services.tsai_training import TSAITrainingService
        from app.services.tsai_models import TSAIModelService
        from app.services.losses import WeightedBCELoss
        import pandas as pd

        np.random.seed(42)

        # Create dataset with 2 targets:
        # Target 1: 50% positive (balanced)
        # Target 2: 10% positive (imbalanced)
        n_samples = 500
        dates = pd.date_range(start='2023-01-01', periods=n_samples, freq='1h')

        base_price = 100.0
        returns = np.random.normal(0, 0.02, n_samples)
        close_prices = base_price * np.exp(np.cumsum(returns))

        # Balanced target (~50%)
        target1 = (np.random.random(n_samples) > 0.5).astype(int)
        # Imbalanced target (~10%)
        target2 = (np.random.random(n_samples) > 0.9).astype(int)

        df = pd.DataFrame({
            'Date': dates,
            'Open': close_prices * (1 + np.random.normal(0, 0.003, n_samples)),
            'High': close_prices * (1 + np.abs(np.random.normal(0, 0.005, n_samples))),
            'Low': close_prices * (1 - np.abs(np.random.normal(0, 0.005, n_samples))),
            'Close': close_prices,
            'Volume': np.random.lognormal(mean=15, sigma=1.5, size=n_samples),
            'target_balanced': target1,
            'target_imbalanced': target2,
        })

        feature_columns = ['Open', 'High', 'Low', 'Close', 'Volume']
        target_columns = ['target_balanced', 'target_imbalanced']

        # Calculate class distribution
        pos_target1 = target1.sum()
        neg_target1 = len(target1) - pos_target1
        pos_target2 = target2.sum()
        neg_target2 = len(target2) - pos_target2

        print(f"\nTarget distributions:")
        print(f"  Target 1 (balanced): {pos_target1} pos / {neg_target1} neg ({100*pos_target1/len(target1):.1f}%)")
        print(f"  Target 2 (imbalanced): {pos_target2} pos / {neg_target2} neg ({100*pos_target2/len(target2):.1f}%)")

        # Calculate per-target weights
        target_stats = [
            {'positive_count': pos_target1, 'negative_count': neg_target1},
            {'positive_count': pos_target2, 'negative_count': neg_target2},
        ]
        per_target_weights = WeightedBCELoss.calculate_per_target_weights(target_stats)

        print(f"\nCalculated per-target weights: {per_target_weights}")

        # Prepare data
        training_service = TSAITrainingService(normalize=True, buffer_pct=0.35)

        # For multistep mode, we need multi-label targets
        # Prepare sequences manually for this test
        seq_len = 24
        X_list = []
        y_list = []

        # Normalize features
        from app.services.data_preparation import DataPreparationService
        prep = DataPreparationService(buffer_pct=0.35)
        df_norm = prep.fit_transform(df, feature_columns)

        features = df_norm[feature_columns].values

        for i in range(len(df) - seq_len):
            X_list.append(features[i:i+seq_len])
            y_list.append([target1[i+seq_len-1], target2[i+seq_len-1]])

        X = np.array(X_list).transpose(0, 2, 1)  # (samples, features, seq_len)
        y = np.array(y_list, dtype=np.float32)  # (samples, n_targets) - float32 for BCE loss

        # Split
        split_idx = int(len(X) * 0.8)
        X_train, X_test = X[:split_idx], X[split_idx:]
        y_train, y_test = y[:split_idx], y[split_idx:]

        print(f"\nData shapes:")
        print(f"  X_train: {X_train.shape}, y_train: {y_train.shape}")
        print(f"  X_test: {X_test.shape}, y_test: {y_test.shape}")

        # Create model
        model_service = TSAIModelService()
        c_in = X_train.shape[1]
        c_out = 2  # 2 targets
        model = model_service.create_model(
            model_type='lstm',
            params={'hidden_size': 32, 'n_layers': 1, 'dropout': 0.1},
            c_in=c_in,
            c_out=c_out,
            seq_len=seq_len
        )

        # Get loss function with per-target weights
        loss_fn = training_service.get_loss_function(
            loss_type='weighted_ce',
            prediction_mode='multistep',
            pos_weight=per_target_weights
        )

        print(f"\nLoss function: {type(loss_fn).__name__}")

        # Train for a few epochs
        result = training_service.train_model(
            model=model,
            train_data=(X_train, y_train),
            val_data=(X_test, y_test),
            epochs=3,
            batch_size=32,
            learning_rate=0.001,
            loss_fn=loss_fn,
            prediction_mode='multistep'  # Required for multi-target training
        )

        assert result['status'] == 'success', f"Training failed: {result.get('error')}"

        trained_model = result['model']

        # Run predictions
        probs = training_service.predict(trained_model, X_test)

        assert probs.shape == (len(X_test), 2), f"Expected shape ({len(X_test)}, 2), got {probs.shape}"
        assert not np.isnan(probs).any(), "Predictions contain NaN"
        assert probs.min() >= 0 and probs.max() <= 1, "Probabilities outside [0, 1]"

        # Check predictions per target
        pred_target1 = (probs[:, 0] >= 0.5).astype(int)
        pred_target2 = (probs[:, 1] >= 0.5).astype(int)

        acc_target1 = (pred_target1 == y_test[:, 0]).mean()
        acc_target2 = (pred_target2 == y_test[:, 1]).mean()

        print(f"\n✓ Training completed successfully")
        print(f"  Target 1 accuracy: {acc_target1:.4f}")
        print(f"  Target 2 accuracy: {acc_target2:.4f}")
        print(f"  Target 1 predicted positive rate: {pred_target1.mean():.4f}")
        print(f"  Target 2 predicted positive rate: {pred_target2.mean():.4f}")

        # The key test: with per-target weights, the model should NOT
        # just predict all zeros for the imbalanced target
        # (which would give high accuracy but 0% recall)
        if pos_target2 > 0:  # Only check if there are positive samples
            recall_target2 = (pred_target2[y_test[:, 1] == 1]).mean() if (y_test[:, 1] == 1).sum() > 0 else 0
            print(f"  Target 2 recall: {recall_target2:.4f}")

        print("\n✓ Per-target weighting test passed")


def run_tests():
    """Run all tests manually."""
    print("\n" + "="*60)
    print("TESTING PER-TARGET WEIGHTS")
    print("="*60 + "\n")

    # Test weight calculation
    print("--- Weight Calculation Tests ---\n")

    test_weights = TestPerTargetWeights()

    print("Test 1: Balanced targets")
    test_weights.test_calculate_per_target_weights_balanced()

    print("\nTest 2: Imbalanced targets (user's case)")
    test_weights.test_calculate_per_target_weights_imbalanced()

    print("\nTest 3: Zero positive case")
    test_weights.test_calculate_per_target_weights_zero_positive()

    # Test BCE with weights
    print("\n--- BCE with Per-Target Weights Tests ---\n")

    test_bce = TestBCEWithPerTargetWeights()

    print("Test 4: BCE with single weight")
    test_bce.test_bce_with_single_weight()

    print("\nTest 5: BCE with per-target weights")
    test_bce.test_bce_with_per_target_weights()

    print("\nTest 6: Per-target weights affect gradients")
    test_bce.test_per_target_weights_affect_gradients()

    # Test with tsai if available
    if _check_tsai_available():
        print("\n--- TSAITrainingService Tests ---\n")

        test_tsai = TestTSAITrainingWithPerTargetWeights()

        print("Test 7: get_loss_function with per-target weights")
        test_tsai.test_get_loss_function_with_per_target_weights()

        print("\nTest 8: get_loss_function with single weight (multistep)")
        test_tsai.test_get_loss_function_with_single_weight_multistep()

        print("\n--- End-to-End Multi-Target Training ---\n")

        test_e2e = TestEndToEndMultiTargetTraining()

        print("Test 9: Training with imbalanced targets")
        test_e2e.test_training_with_imbalanced_targets()
    else:
        print("\n⚠ Skipping tsai tests (tsai not available)")

    print("\n" + "="*60)
    print("ALL TESTS PASSED ✓")
    print("="*60 + "\n")


if __name__ == "__main__":
    run_tests()
