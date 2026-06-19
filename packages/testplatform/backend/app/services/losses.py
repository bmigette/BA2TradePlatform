"""
Custom Loss Functions for Class Imbalance Handling

Provides Focal Loss and other loss functions designed for imbalanced classification.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional
import numpy as np
import logging

logger = logging.getLogger(__name__)


class FocalLoss(nn.Module):
    """
    Focal Loss for handling class imbalance in binary/multi-class classification.

    From paper: "Focal Loss for Dense Object Detection" (Lin et al., 2017)

    FL(p_t) = -alpha_t * (1 - p_t)^gamma * log(p_t)

    Where:
    - p_t is the model's estimated probability for the correct class
    - gamma is the focusing parameter (higher = more focus on hard examples)
    - alpha is the class weight (balances positive/negative classes)

    For imbalanced datasets:
    - gamma=2.0 works well in practice
    - alpha should be set based on class frequency (or auto-calculated)
    """

    def __init__(
        self,
        alpha: Optional[float] = None,
        gamma: float = 2.0,
        reduction: str = 'mean',
        pos_weight: Optional[float] = None
    ):
        """
        Initialize Focal Loss.

        Args:
            alpha: Weighting factor for positive class (0-1).
                   If None, no class weighting is applied.
                   Higher alpha = more weight on positive class.
            gamma: Focusing parameter. Higher values focus more on hard examples.
                   gamma=0 is equivalent to cross-entropy.
                   gamma=2.0 is recommended for most cases.
            reduction: 'none', 'mean', or 'sum'
            pos_weight: Alternative to alpha - weight for positive class.
                        If both alpha and pos_weight are provided, alpha is used.
        """
        super().__init__()
        self.alpha = alpha
        self.gamma = gamma
        self.reduction = reduction
        self.pos_weight = pos_weight

        logger.info(f"FocalLoss initialized: alpha={alpha}, gamma={gamma}")

    def forward(self, inputs: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
        """
        Calculate Focal Loss.

        For Darts time series models:
        - inputs: shape (batch, seq_len) or (batch, seq_len, 1) - predicted values
        - targets: shape (batch, seq_len) - ground truth binary labels (0 or 1)

        For standard classification:
        - inputs: shape (N,) or (N, C) for multi-class
        - targets: shape (N,) class labels

        Returns:
            Focal loss value
        """
        # Ensure inputs and targets are float type (Darts may pass Long tensors)
        inputs = inputs.float()
        targets = targets.float()

        # Flatten for binary classification (treat all timesteps independently)
        # This is correct for time series with binary targets (0/1)
        inputs_flat = inputs.view(-1)
        targets_flat = targets.view(-1)

        return self._binary_focal_loss(inputs_flat, targets_flat)

    def _binary_focal_loss(self, inputs: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
        """Binary focal loss for single-output classification."""
        # Apply sigmoid to get probabilities
        p = torch.sigmoid(inputs)

        # Calculate cross-entropy term
        ce_loss = F.binary_cross_entropy_with_logits(inputs, targets, reduction='none')

        # Calculate p_t (probability of correct class)
        p_t = p * targets + (1 - p) * (1 - targets)

        # Calculate focal weight
        focal_weight = (1 - p_t) ** self.gamma

        # Apply alpha weighting if specified
        if self.alpha is not None:
            alpha_t = self.alpha * targets + (1 - self.alpha) * (1 - targets)
            focal_weight = alpha_t * focal_weight

        # Calculate focal loss
        focal_loss = focal_weight * ce_loss

        # Apply reduction
        if self.reduction == 'mean':
            return focal_loss.mean()
        elif self.reduction == 'sum':
            return focal_loss.sum()
        else:
            return focal_loss

    def _multiclass_focal_loss(self, inputs: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
        """Multi-class focal loss."""
        num_classes = inputs.size(1)

        # Convert targets to one-hot
        targets_one_hot = F.one_hot(targets.long(), num_classes).float()

        # Apply softmax to get probabilities
        p = F.softmax(inputs, dim=1)

        # Calculate cross-entropy
        ce_loss = F.cross_entropy(inputs, targets.long(), reduction='none')

        # Calculate p_t for each sample
        p_t = (p * targets_one_hot).sum(dim=1)

        # Calculate focal weight
        focal_weight = (1 - p_t) ** self.gamma

        # Apply alpha weighting if specified (for multi-class, alpha should be a tensor)
        if self.alpha is not None:
            if isinstance(self.alpha, (int, float)):
                # For multi-class with single alpha, we apply to all classes equally
                focal_weight = self.alpha * focal_weight

        # Calculate focal loss
        focal_loss = focal_weight * ce_loss

        # Apply reduction
        if self.reduction == 'mean':
            return focal_loss.mean()
        elif self.reduction == 'sum':
            return focal_loss.sum()
        else:
            return focal_loss

    @staticmethod
    def calculate_alpha_from_class_counts(
        positive_count: int,
        negative_count: int,
        method: str = 'inverse'
    ) -> float:
        """
        Calculate optimal alpha based on class distribution.

        Args:
            positive_count: Number of positive samples
            negative_count: Number of negative samples
            method: 'inverse' (inverse frequency) or 'balanced' (sklearn-style)

        Returns:
            Alpha value for positive class (0-1)
        """
        total = positive_count + negative_count

        if method == 'inverse':
            # Inverse frequency: more weight to minority class
            alpha = negative_count / total
        elif method == 'balanced':
            # Balanced: weight = n_samples / (n_classes * n_samples_per_class)
            alpha = total / (2 * positive_count) if positive_count > 0 else 0.5
            alpha = min(alpha, 0.99)  # Cap at 0.99
        else:
            alpha = 0.5

        logger.info(f"Calculated alpha={alpha:.4f} from counts: pos={positive_count}, neg={negative_count}")
        return alpha


class WeightedBCELoss(nn.Module):
    """
    Weighted Binary Cross-Entropy Loss.

    Simple class weighting without the focal term.
    Good baseline for imbalanced classification.
    """

    def __init__(self, pos_weight: Optional[float] = None, reduction: str = 'mean'):
        """
        Initialize Weighted BCE Loss.

        Args:
            pos_weight: Weight for positive class. If None, auto-calculate recommended.
            reduction: 'none', 'mean', or 'sum'
        """
        super().__init__()
        self.pos_weight = pos_weight
        self.reduction = reduction

    def forward(self, inputs: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
        """Calculate weighted BCE loss."""
        pos_weight_tensor = None
        if self.pos_weight is not None:
            pos_weight_tensor = torch.tensor([self.pos_weight], device=inputs.device)

        return F.binary_cross_entropy_with_logits(
            inputs.view(-1),
            targets.view(-1).float(),
            pos_weight=pos_weight_tensor,
            reduction=self.reduction
        )

    @staticmethod
    def calculate_pos_weight(positive_count: int, negative_count: int) -> float:
        """
        Calculate positive class weight.

        Args:
            positive_count: Number of positive samples
            negative_count: Number of negative samples

        Returns:
            Weight for positive class
        """
        if positive_count == 0:
            return 1.0
        return negative_count / positive_count

    @staticmethod
    def calculate_per_target_weights(
        target_stats: list
    ) -> list:
        """
        Calculate per-target positive class weights for multi-target classification.

        This is used when you have multiple prediction targets with different
        class distributions. Each target gets its own weight based on its
        positive/negative ratio.

        Args:
            target_stats: List of dicts with 'positive_count' and 'negative_count' per target.
                Example: [
                    {'positive_count': 586, 'negative_count': 625},  # Target 1: balanced
                    {'positive_count': 35, 'negative_count': 485},   # Target 2: imbalanced
                ]

        Returns:
            List of weights, one per target.
            Example: [1.07, 13.86] - Target 2 gets much higher weight
        """
        weights = []
        for stats in target_stats:
            pos = stats.get('positive_count', 0)
            neg = stats.get('negative_count', 0)
            if pos == 0:
                weight = 1.0
            else:
                weight = neg / pos
            weights.append(weight)
            logger.debug(f"Target weight: pos={pos}, neg={neg}, weight={weight:.4f}")

        logger.info(f"Calculated per-target weights: {[f'{w:.4f}' for w in weights]}")
        return weights


def get_loss_function(
    loss_type: str,
    positive_count: Optional[int] = None,
    negative_count: Optional[int] = None,
    gamma: float = 2.0,
    alpha: Optional[float] = None
) -> nn.Module:
    """
    Factory function to get loss function by name.

    Args:
        loss_type: One of 'cross_entropy', 'weighted_cross_entropy', 'focal_loss'
        positive_count: Number of positive samples (for auto-weighting)
        negative_count: Number of negative samples (for auto-weighting)
        gamma: Focal loss gamma parameter
        alpha: Optional alpha override

    Returns:
        PyTorch loss module
    """
    if loss_type == 'cross_entropy':
        logger.info("Using standard Cross-Entropy loss")
        return nn.BCEWithLogitsLoss()

    elif loss_type == 'weighted_cross_entropy':
        if positive_count and negative_count:
            pos_weight = WeightedBCELoss.calculate_pos_weight(positive_count, negative_count)
        else:
            pos_weight = 1.0
        logger.info(f"Using Weighted Cross-Entropy loss with pos_weight={pos_weight:.4f}")
        return WeightedBCELoss(pos_weight=pos_weight)

    elif loss_type == 'focal_loss':
        if alpha is None and positive_count and negative_count:
            alpha = FocalLoss.calculate_alpha_from_class_counts(positive_count, negative_count)
        logger.info(f"Using Focal Loss with alpha={alpha}, gamma={gamma}")
        return FocalLoss(alpha=alpha, gamma=gamma)

    else:
        raise ValueError(f"Unknown loss type: {loss_type}. Supported: cross_entropy, weighted_cross_entropy, focal_loss")
