"""
Classification Metrics for Model Evaluation

Provides comprehensive metrics for evaluating binary classification models,
especially for imbalanced datasets where accuracy is misleading.
"""

import numpy as np
from typing import Dict, Any, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

# Try to import sklearn for AUC-ROC
try:
    from sklearn.metrics import roc_auc_score, precision_recall_curve, auc
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("sklearn not available. Some metrics will be unavailable.")


class ClassificationMetrics:
    """
    Comprehensive classification metrics calculator.

    For imbalanced datasets, prefer:
    - F1 Score: Balances precision and recall
    - AUC-ROC: Threshold-independent performance measure
    - Precision: When false positives are costly
    - Recall: When false negatives are costly (catching all positives)
    - Balanced Accuracy: Average of per-class recalls

    AVOID using standard Accuracy for imbalanced data!
    """

    @staticmethod
    def calculate_all(
        y_true: np.ndarray,
        y_pred_proba: np.ndarray,
        threshold: float = 0.5
    ) -> Dict[str, float]:
        """
        Calculate all classification metrics.

        Args:
            y_true: Ground truth labels (0 or 1)
            y_pred_proba: Predicted probabilities (0-1)
            threshold: Classification threshold

        Returns:
            Dictionary of all metrics
        """
        y_true = np.asarray(y_true).flatten()
        y_pred_proba = np.asarray(y_pred_proba).flatten()
        y_pred = (y_pred_proba >= threshold).astype(int)

        # Basic counts
        tp = np.sum((y_true == 1) & (y_pred == 1))
        tn = np.sum((y_true == 0) & (y_pred == 0))
        fp = np.sum((y_true == 0) & (y_pred == 1))
        fn = np.sum((y_true == 1) & (y_pred == 0))

        total = tp + tn + fp + fn
        positives = tp + fn
        negatives = tn + fp

        # Calculate metrics with safe division
        accuracy = (tp + tn) / total if total > 0 else 0.0
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        specificity = tn / (tn + fp) if (tn + fp) > 0 else 0.0
        f1_score = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        balanced_accuracy = (recall + specificity) / 2

        # Matthews Correlation Coefficient (good for imbalanced data)
        mcc_num = (tp * tn) - (fp * fn)
        mcc_denom = np.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
        mcc = mcc_num / mcc_denom if mcc_denom > 0 else 0.0

        # Confusion matrix: [[TN, FP], [FN, TP]]
        confusion_matrix = [
            [int(tn), int(fp)],
            [int(fn), int(tp)]
        ]

        metrics = {
            'accuracy': float(accuracy),
            'precision': float(precision),
            'recall': float(recall),
            'specificity': float(specificity),
            'f1_score': float(f1_score),
            'balanced_accuracy': float(balanced_accuracy),
            'mcc': float(mcc),
            'true_positives': int(tp),
            'true_negatives': int(tn),
            'false_positives': int(fp),
            'false_negatives': int(fn),
            'total_positives': int(positives),
            'total_negatives': int(negatives),
            'threshold': float(threshold),
            'confusion_matrix': confusion_matrix
        }

        # Add AUC-ROC if sklearn available and we have both classes
        if SKLEARN_AVAILABLE and len(np.unique(y_true)) > 1:
            try:
                metrics['auc_roc'] = float(roc_auc_score(y_true, y_pred_proba))

                # Also calculate PR-AUC (more informative for imbalanced data)
                precision_curve, recall_curve, _ = precision_recall_curve(y_true, y_pred_proba)
                metrics['auc_pr'] = float(auc(recall_curve, precision_curve))
            except Exception as e:
                logger.warning(f"Could not calculate AUC metrics: {e}")
                metrics['auc_roc'] = 0.0
                metrics['auc_pr'] = 0.0
        else:
            metrics['auc_roc'] = 0.0
            metrics['auc_pr'] = 0.0

        return metrics

    @staticmethod
    def get_fitness_score(
        y_true: np.ndarray,
        y_pred_proba: np.ndarray,
        fitness_metric: str = 'f1_score',
        threshold: float = 0.5
    ) -> float:
        """
        Get a single fitness score for genetic algorithm optimization.

        Args:
            y_true: Ground truth labels
            y_pred_proba: Predicted probabilities
            fitness_metric: One of 'accuracy', 'f1_score', 'precision', 'recall',
                           'auc_roc', 'balanced_accuracy', 'mcc'
            threshold: Classification threshold (not used for auc_roc)

        Returns:
            Fitness score (higher is better)
        """
        metrics = ClassificationMetrics.calculate_all(y_true, y_pred_proba, threshold)

        if fitness_metric not in metrics:
            raise ValueError(f"Unknown fitness metric: {fitness_metric}. "
                           f"Available: {list(metrics.keys())}")

        score = metrics[fitness_metric]
        logger.debug(f"Fitness score ({fitness_metric}): {score:.4f}")
        return score

    @staticmethod
    def find_optimal_threshold(
        y_true: np.ndarray,
        y_pred_proba: np.ndarray,
        optimize_for: str = 'f1_score'
    ) -> Tuple[float, float]:
        """
        Find optimal classification threshold for a given metric.

        Args:
            y_true: Ground truth labels
            y_pred_proba: Predicted probabilities
            optimize_for: Metric to optimize ('f1_score', 'balanced_accuracy', etc.)

        Returns:
            Tuple of (optimal_threshold, best_score)
        """
        best_threshold = 0.5
        best_score = 0.0

        for threshold in np.arange(0.1, 0.9, 0.05):
            metrics = ClassificationMetrics.calculate_all(y_true, y_pred_proba, threshold)
            score = metrics.get(optimize_for, 0.0)

            if score > best_score:
                best_score = score
                best_threshold = threshold

        logger.info(f"Optimal threshold for {optimize_for}: {best_threshold:.2f} (score: {best_score:.4f})")
        return best_threshold, best_score

    @staticmethod
    def get_class_distribution(y: np.ndarray) -> Dict[str, Any]:
        """
        Analyze class distribution and imbalance.

        Args:
            y: Label array

        Returns:
            Distribution statistics including imbalance warning
        """
        y = np.asarray(y).flatten()
        total = len(y)
        positive_count = int(np.sum(y == 1))
        negative_count = int(np.sum(y == 0))

        positive_pct = (positive_count / total * 100) if total > 0 else 0
        negative_pct = (negative_count / total * 100) if total > 0 else 0

        # Calculate imbalance ratio
        if positive_count > 0 and negative_count > 0:
            imbalance_ratio = max(positive_count, negative_count) / min(positive_count, negative_count)
        else:
            imbalance_ratio = float('inf')

        # Determine if severely imbalanced
        is_imbalanced = positive_pct < 10 or positive_pct > 90

        return {
            'positive_count': positive_count,
            'negative_count': negative_count,
            'positive_pct': round(positive_pct, 2),
            'negative_pct': round(negative_pct, 2),
            'total': total,
            'imbalance_ratio': round(imbalance_ratio, 2),
            'is_imbalanced': is_imbalanced,
            'recommended_loss': 'focal_loss' if is_imbalanced else 'cross_entropy',
            'recommended_metric': 'f1_score' if is_imbalanced else 'accuracy'
        }


# Convenience functions for common metrics
def f1_score(y_true: np.ndarray, y_pred_proba: np.ndarray, threshold: float = 0.5) -> float:
    """Calculate F1 score."""
    return ClassificationMetrics.get_fitness_score(y_true, y_pred_proba, 'f1_score', threshold)


def precision_score(y_true: np.ndarray, y_pred_proba: np.ndarray, threshold: float = 0.5) -> float:
    """Calculate precision."""
    return ClassificationMetrics.get_fitness_score(y_true, y_pred_proba, 'precision', threshold)


def recall_score(y_true: np.ndarray, y_pred_proba: np.ndarray, threshold: float = 0.5) -> float:
    """Calculate recall."""
    return ClassificationMetrics.get_fitness_score(y_true, y_pred_proba, 'recall', threshold)


def auc_roc_score(y_true: np.ndarray, y_pred_proba: np.ndarray) -> float:
    """Calculate AUC-ROC."""
    return ClassificationMetrics.get_fitness_score(y_true, y_pred_proba, 'auc_roc')


def balanced_accuracy_score(y_true: np.ndarray, y_pred_proba: np.ndarray, threshold: float = 0.5) -> float:
    """Calculate balanced accuracy."""
    return ClassificationMetrics.get_fitness_score(y_true, y_pred_proba, 'balanced_accuracy', threshold)
